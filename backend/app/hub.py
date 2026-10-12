"""
状态中枢：
  - 串口上行原始字节 → 原样广播给 WebSocket 上位机（前端 FrameParser 零改动）
  - 同一份字节再喂 Python FrameParser → 目录缓存 / 监控快照 / 日志（供脚本 API）
  - 上位机与脚本的下行帧统一走优先级队列：STOP(0) < 上位机(10) < 脚本(20)
  - 停车帧会清空脚本队列并置 abort，正在跑的脚本在 cmd()/sleep() 处立即中断
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from fastapi import WebSocket

from .protocol import (
    CmdDirItem,
    CmdGet,
    CmdPost,
    CmdSpec,
    DirTable,
    EventKind,
    MenuItem,
    build_frame,
    cmd_spec,
    command_send,
    ping,
    request_command,
    request_monitor,
    request_tunable,
    scan_dir_frames,
    set_tunable,
    subscribe,
    tag_fast,
    tag_subscribed,
)
from .serial_bridge import SerialBridge, Status

logger = logging.getLogger("vex.hub")

# ---------- 下行优先级 ----------
PRI_STOP = 0 # 0停车帧。永远第一
PRI_UI = 10 #上位机：网页按钮、目录请求、订阅。人工操作，比脚本优先
PRI_SCRIPT = 20 #脚本下发的命令/调参

# ---------- 日志缓冲上限 ----------
# 脚本忘了 end_log 会一直攒到内存吃满。超出后"丢新不丢旧"：
# 保留已记录的时间窗完整，事后按窗口统计仍有意义
MAX_LOG_SAMPLES = 500_000

# 默认 pytest 输出目录（相对 backend/）
DEFAULT_PYTEST_DIR = Path(__file__).resolve().parent.parent / "pytest"


# ---------- 区间统计（脚本标定用）----------
# 把一批样本压成常用统计量，脚本不用自己攒样本算。各字段的用途：
#   mean        算术均值。看"有没有稳态偏置"用这个
#   abs_mean    取绝对值后的均值。看"抖动/误差幅度"必须用这个——
#               来回震荡时正负抵消，mean 会接近 0 而 abs_mean 真实反映幅度
#   abs_max     最大绝对偏差。看最坏情况
#   std         标准差。注意是总体标准差（除以 n），不是样本标准差（除以 n-1）
#   rate_hz     样本数 / 区间秒数。反映下位机实际推送率，用来验证是否真按 100Hz 在推
#   ratio_ge    |值| >= 阈值的样本占比。判断"超差时间占比"这类指标（thresholds 传入阈值列表）
def _aggregate(vals: list[float], thresholds=(), span_ms: float = 0.0) -> dict:
    """区间样本的统计量；无样本时除 n 外全为 None。"""
    n = len(vals)
    if n == 0:
        return {
            "n": 0, "mean": None, "abs_mean": None, "min": None, "max": None,
            "abs_max": None, "std": None, "rate_hz": None, "ratio_ge": {},
        }
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return {
        "n": n,
        "mean": mean,
        "abs_mean": sum(abs(v) for v in vals) / n,
        "min": min(vals),
        "max": max(vals),
        "abs_max": max(abs(v) for v in vals),
        "std": var ** 0.5,
        "rate_hz": round(n / (span_ms / 1000.0), 2) if span_ms > 0 else None,
        "ratio_ge": {str(th): sum(1 for v in vals if abs(v) >= th) / n for th in thresholds},
    }


# ---------- 最新样本快照 ----------
class MonitorSample(NamedTuple):
    """
    某监控项的最新一个样本：值 + 何时收到，一对不可分的量放一起。
    value : 最新值
    t_recv: 收到该值的 time.monotonic() 秒（单调时钟，不受改时间影响）；
            None = 只有目录帧带来的初值、还没收到过推送。
    """
    value: Any
    t_recv: float | None

class Hub:

    def __init__(
        self,
        port: str | None = None, # 串口端口
        baud: int = 115200, # usb 中仅占位
        pytest_dir: Path | None = None, # pytest 输出目录
    ) -> None:
        self.bridge = SerialBridge(
            port=port,
            baud=baud,
            on_event=self._on_serial_event, # 串口事件回调
            on_status=self._on_serial_status, # 串口状态回调
            on_raw=self._on_serial_raw, # 串口原始数据回调
        )
        self.pytest_dir = Path(pytest_dir) if pytest_dir else DEFAULT_PYTEST_DIR
        self.pytest_dir.mkdir(parents=True, exist_ok=True)

        # 三张目录缓存 index → item（item 是 protocol.py 里的 dataclass）
        self.tunable_dir: dict[int, MenuItem] = {}
        '''可调参数目录缓存 {index: MenuItem}'''
        self.monitor_dir: dict[int, MenuItem] = {}
        '''监控项目录缓存 {index: MenuItem}'''
        self.cmd_dir: dict[int, CmdDirItem] = {}
        '''命令目录缓存 {index: CmdDirItem}'''

        # name → index 反查（脚本可用中文名寻址）
        self._name_to_mon: dict[str, int] = {}
        '''监控项中文名 → index'''
        self._name_to_tun: dict[str, int] = {}
        '''可调参数中文名 → index '''
        self._name_to_cmd: dict[str, int] = {}
        '''命令中文名 → index '''

        self._dir_cache: dict[int, dict[int, bytes]] = {}
        '''目录原始帧缓存 {cmd: {index: 完整帧bytes}}，同 index 覆盖不叠加'''

        self._dir_buf = bytearray()
        '''粘包尾部缓冲：目录 burst 跨 chunk 时剩下的不完整帧尾'''

        self.monitor_latest: dict[int, MonitorSample] = {}
        '''最新样本快照 {index: MonSample(value, t_recv)} 只存每项最新一个'''

        # ---------- 订阅状态----------

        # _recompute_and_emit 把聚合意图和它比对，不一致才发 Subscribe 帧
        self.fw_sub: dict[int, tuple] = {} 
        '''下位机实际推送档位 {index -> (sub:bool, fast:bool)}'''

        # 脚本 sub(x, 'log'/'both') 写入
        # value 推送只有在 logging 且 idx 在集合里时才进 log_dir
        self.log_reg: set[int] = set() 
        '''要监听打印的数据index {index1,index2,...}'''

        # source 标识"谁想要"：ui:{conn_id}（某个网页连接）或 "script"（脚本进程）。
        # sub 取并集，fast 取已订阅者中的最高档。
        # 按来源分开记的意义：A 断开时 只删除A的意图，不会把 B 一起退订掉。
        self._sub_intent: dict[int, dict[str, tuple]] = {} 
        '''订阅意图聚合表 {index -> {source: (sub:bool, fast:bool)}}'''
        
        # 三个并发来源：事件循环;HTTP 线程池;重连线池
        # 没有锁就会互相覆盖意图。
        self._sub_lock = threading.Lock()
        '''保护 _sub_intent / fw_sub 的读写锁'''

        # ---------- 记录会话：脚本标定用的时间序列（start_log → end_log 为一轮） ----------

        # rx 线程每收到一个 value 都先查它，True 才继续判断 log_reg 并落样本。
        self.logging = False
        '''是否正在记录。start_log 置 True,end_log 置 False'''

        # t_ms:相对 _log_t0 的毫秒,每条按时间升序 append；
        self.log_dir: dict[int, list[tuple[float, Any]]] = {}
        '''记录样本缓冲 {index: [(t_ms:float, value:Any), ...]}'''

        # >0 说明这轮记录不完整；end_log / print_log / snapshot 都会带上它。
        self.log_dropped = 0
        '''到 MAX_LOG_SAMPLES后 丢弃的新样本数'''

        self._log_t0 = 0.0
        '''start_log 时的 time.monotonic() 秒；样本里的 t_ms 都相对它'''

        # 守护整份记录缓冲：rx 线程 100Hz 在 _record 里 append，
        # HTTP 线程同时在 _samples（取副本）/ start_log（清空）——不锁就会边读边改。
        self._log_lock = threading.Lock()
        '''锁：护 log_dir（rx 100Hz append vs HTTP 线程取样本 / start_log 清空）'''

        # ---------- WebSocket 上位机：广播目标名单 ----------

        # 当前在线的网页连接。_broadcast_bytes / _broadcast_json 的发送对象。
        self._ws_clients: set[WebSocket] = set()
        '''在线网页连接 {ws1, ws2, ...}：_broadcast_bytes/_broadcast_json 的发送目标'''

        # 守护客户端集合：attach_ui 的 add/discard 在事件循环里，
        # 广播却从 rx 线程发起（先 list() 拍快照再放锁）——
        # 不锁的话一边迭代 set 一边 add 就是 "Set changed size during iteration"。
        self._ws_lock = threading.Lock()
        '''锁：护 _ws_clients（事件循环 add/discard vs rx 线程广播前 list() 拍快照）'''

        # ---------- 下行帧队列：串口只有一个写者，所有来源在这里排队串行化 ----------

        # 待发帧队列，元素 (priority, seq, frame)，按 (priority, seq) 排序：
        #   priority 管谁重要：STOP(0) < UI(10) < SCRIPT(20)（取值见文件头常量）
        #   seq      管同级谁先来：多线程入队会交错，靠它给出确定的 FIFO
        # 唯一消费者是 _tx_loop；断线时 STOP 帧塞回队首重试，其余帧直接丢。
        # 用 collections.deque（PEP 585：没有 collections.abc.Deque 这个对应物）。
        self._tx_q: deque[tuple[int, int, bytes]] = deque()
        '''下行帧队列 [(priority:int, seq:int, frame:bytes), ...]，按 (priority, seq) 出队'''

        # seq 发号器：每入队一帧 +1，单调递增，永不回绕参与比较。
        self._tx_seq = 0
        '''帧发号器：每入队一帧 +1，同级 FIFO 依据，单调递增'''
        # 身兼两职：守护 _tx_q 的锁 + tx 线程的睡眠/唤醒。
        # enqueue / emergency_stop 入队后 notify；_tx_loop 没帧时 wait 挂起不空转；
        # stop() 靠 notify_all 把它叫醒收尾。
        
        self._tx_cv = threading.Condition()
        '''Condition：既是 _tx_q 的锁，又是 tx 线程的睡眠/唤醒（wait/notify）'''
        # 唯一串口写线程（start() 里创建，stop() 里 join 收尾）。
        self._tx_thread: threading.Thread | None = None
        '''唯一串口写线程 vex-tx 的 handle（None=未启动；stop() 里 join 收尾）'''
        # tx 线程主循环开关：start() 置 True；stop() 置 False 并 notify，线程才退得出 wait。
        self._running = False
        '''tx 线程主循环开关：start()→True；stop()→False 并 notify'''

        # ---------- 紧急停车 / 脚本中止 ----------

        # 中止旗标。emergency_stop 置位后，脚本在下一次 cmd()/sleep() 的
        # check_abort 处抛错——协作式中止，不杀线程。clear_abort 复位。
        self.abort_event = threading.Event()
        '''脚本协作式中止旗标：emergency_stop 置位 → cmd()/sleep() 的 check_abort 抛错；clear_abort 复位'''

        # 停车命令的 index。从命令目录 tag 的 SPEC=Stop 位认出来（_on_serial_event），
        # 固件改了命令表顺序也不怕；目录没到时兜底用 0x80（见 _stop_index）。
        self._stop_cmd_index: int | None = None
        '''停车命令 index：命令目录 tag 的 SPEC=Stop 自动认出；None=目录未到（兜底 0x80）'''

        # ---------- 自动重连（Backend ↔ 下位机） ----------

        # "用户想不想连"的开关，与"实际连没连上"是两回事。
        # True：掉线自动重连；False：重连线程停手（比如用户点了断开）。
        self._want_connected = False
        '''连接意愿开关：True=掉线自动重连（与实际连没连上是两回事）'''

        # 重连后台线程（_reconnect_loop：每 2s 试一次直到连上或不再想连）。
        # 留 handle 是防重复开线程（ensure_connected 先看它还活着没）。
        self._reconnect_thread: threading.Thread | None = None
        '''重连线程 _reconnect_loop 的 handle（None=未启动；防重复开线程）'''

        # ---------- 对外可见的状态 ----------

        # 由 _on_serial_status 更新；/api/status 和 WS 推的 status 消息都取它的 .value。
        self.status: Status = Status.idle
        '''串口连接状态（Status 枚举；只在对前端输出时转字符串）'''

        # 上一条状态的补充说明（给人看的），比如"串口被占用""已连接 COM5"。
        self.status_msg = "未启动"
        '''status 的人读补充说明（如"串口被占用""已连接 COM5"）'''

        # FastAPI 主事件循环（set_loop 在 lifespan 里注入）。
        # rx 线程不能 await：所有 WS 发送都靠 run_coroutine_threadsafe
        # 把协程"投"给这个循环代跑——投完就返回，网页慢不拖收数。
        self._loop: asyncio.AbstractEventLoop | None = None
        '''FastAPI 主事件循环（set_loop 注入）：rx 线程经 run_coroutine_threadsafe 投 WS 发送'''

    # ---------- 生命周期 ----------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """FastAPI lifespan 里注入主事件循环，供串口线程跨线程广播。"""
        # 串口 rx 线程不在事件循环里，没法直接 await ws.send()。
        # 只能把协程"投"给这个 loop 代跑（run_coroutine_threadsafe），
        self._loop = loop

    def start(self, auto_connect: bool = True) -> None:
        """启动常驻的 tx 线程；auto_connect 时另开一条一次性线程去探测串口。"""
        self._running = True
        self._tx_thread = threading.Thread(target=self._tx_loop, name="vex-tx", daemon=True)
        '''target=self._tx_loop:指定线程启动后要执行的函数/方法
           daemon=True:将线程设置为守护线程(Daemon Thread)。
           当程序中所有非守护线程(通常是主线程)都执行完毕时，守护线程会被强制终止'''
        self._tx_thread.start()
        if auto_connect:
            threading.Thread(target=self._auto_connect, name="vex-connect", daemon=True).start()

    def _auto_connect(self) -> None:
        # 串口探测要逐口发 Ping 等应答，可能阻塞数秒。
        # 放在线程里，FastAPI 的 startup 才能立刻返回、HTTP 接口马上可用。
        if self.bridge.connect():
            self.bridge.start_heartbeat(1.0)
            # 脚本没连上位机时也要有目录：连上自动拉三张表
            self._request_all_dirs()

    def ensure_connected(self) -> None:
        """保证 Backend ↔ 下位机有连接意愿，断线由重连线程兜底。

        _want_connected 是"用户想不想连"的开关，与实际是否连上无关：
        它为 True 时掉线会自动重连；置 False 后重连线程会自己停手（比如用户点了断开）。
        """
        self._want_connected = True
        if self.bridge.connected:
            return
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, name="vex-reconnect", daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        # 断线后每 2s 重试，直到连上或用户不想连了（_want_connected 置 False 就退出）
        # 重连成功必须做三件事：清目录缓存、重拉三张表、补发订阅——
        # 下位机重启后目录内容可能变了，且它的订阅表是空的。
        while self._want_connected and not self.bridge.connected:
            if self.bridge.connect():
                self.bridge.start_heartbeat(1.0)
                # 重连后目录可能已变：缓存和解析表都清掉重新拉
                self.tunable_dir.clear()
                self.monitor_dir.clear()
                self.cmd_dir.clear()
                self._name_to_mon.clear()
                self._name_to_tun.clear()
                self._name_to_cmd.clear()
                self._request_all_dirs()
                # 把聚合后的订阅意图补发给下位机（重连不丢订阅）
                self._restore_subscriptions()
                return
            time.sleep(2.0)

    def _restore_subscriptions(self) -> None:
        """重连后按聚合意图补发订阅。

        下位机重启/重连后自己的订阅表是空的（推送档位不持久化），
        但上位机各页面和脚本的意图还在 _sub_intent 里，这里把它重新下发一遍，
        做到"重连不丢订阅"，用户无感。
        """
        with self._sub_lock:
            for idx, intent in self._sub_intent.items():
                if not intent:
                    continue
                sub, fast = self._aggregate(intent)
                self.fw_sub[idx] = (sub, fast)
                if sub:
                    self.enqueue(subscribe(idx, True, fast), PRI_UI)

    @staticmethod
    def _aggregate(intent: dict[str, tuple]) -> tuple:
        """多来源订阅意图 → 单一档位。

        规则：sub 取并集（任一来源要订就订）；fast 取"已订阅者中"的最高档（True > False）。
        注意第二个 any 只在 v[0] 为真时才统计——没订阅的人要什么档位都不算数。
        这样 A 页面退订时，B 页面正在看的高速推送不会被降级。
        """
        any_sub = any(v[0] for v in intent.values())
        any_fast = any(v[1] for v in intent.values() if v[0])
        return (any_sub, any_fast if any_sub else False)

    def set_want_connected(self, want: bool) -> None:
        """连接意愿开关：True 时掉线自动重连，False 时重连线程停手。"""
        self._want_connected = bool(want)

    # ---------- 目录缓存 ----------

    def _cache_dir_frame(self, cmd: int, frame: bytes) -> None:
        """目录一项一帧（payload[0]=index），同 cmd+index 覆盖不叠加。

        下位机发目录是一轮 burst（每项一帧，8 项一批）。用 index 作键覆盖式缓存，
        这样重复拉表时是"替换"而不是"追加"，不会攒出一份新旧混叠的目录。
        """
        if len(frame) >= 3:
            self._dir_cache.setdefault(cmd, {})[frame[2]] = frame

    def _dir_frames_list(self, post_cmd: int) -> list[bytes]:
        """回放用：按 index 排序输出整帧列表。"""
        bucket = self._dir_cache.get(post_cmd) or {}
        return [bucket[k] for k in sorted(bucket.keys())]

    def _request_dir(self, post_cmd: int, request_frame: bytes) -> None:
        """主动拉某张目录前先清该表缓存，避免新旧两轮 burst 混叠成半旧半新。

        比如目录从 20 项变成 18 项：不清缓存的话新 burst 只覆盖前 18 个 index，
        第 19、20 项会作为陈旧残留留在表里，前端就会看到两个已经不存在的监控项。
        """
        self._dir_cache.pop(post_cmd, None)
        self.enqueue(request_frame, PRI_UI)

    def _request_all_dirs(self) -> None:
        """拉三张表：调参目录、监控目录、命令目录。连上和重连后都要调。"""
        self._request_dir(CmdPost.TunableDirectory, request_tunable())
        self._request_dir(CmdPost.MonitorDirectory, request_monitor())
        self._request_dir(CmdPost.CMDDirectory, request_command())

    def refresh_directories(self) -> dict:
        """清空三张目录缓存并重新向车请求（表结构变更或怀疑缓存过期时用）。

        与被动更新的区别：目录是下位机启动/订阅时推的，平时不会主动重推。
        改了固件里的表定义后必须调这个，否则后端一直用旧表解析，字段会对不上。
        """
        self._dir_cache.clear()
        self._dir_buf.clear()
        self._request_all_dirs()
        return {"ok": True}

    def stop(self) -> None:
        """收尾。顺序有讲究：先置标志 → 再唤醒 tx 线程 → 再关串口 → 最后 join。

        少了 notify_all，tx 线程会睡在 Condition.wait 里（虽有 0.1s 超时兜底，但会多卡）。
        """
        self._running = False
        self._want_connected = False
        with self._tx_cv:
            self._tx_cv.notify_all()
        self.bridge.disconnect()
        if self._tx_thread:
            self._tx_thread.join(timeout=1.0)

    def reconnect(self) -> bool:
        """手动重连：断 → 连 → 清目录缓存 → 重拉三张表。

        注意这里没有调 _restore_subscriptions：手动重连走的是 bridge.connect 成功路径，
        订阅意图仍在 _sub_intent 里，下次有页面/脚本操作时会自然补发。
        """
        self.bridge.disconnect()
        self._dir_buf.clear()
        ok = self.bridge.connect()
        if ok:
            self.bridge.start_heartbeat(1.0)
            self.tunable_dir.clear()
            self.monitor_dir.clear()
            self.cmd_dir.clear()
            self._name_to_mon.clear()
            self._name_to_tun.clear()
            self._name_to_cmd.clear()
            self._request_all_dirs()
        return ok

    # ---------- 串口回调 ----------

    def _on_serial_status(self, state: Status, message: str) -> None:
        """串口状态变化回调（SerialBridge 注入）。

        disconnected 时若用户仍想连（_want_connected）就触发自动重连——
        这是整条"掉线自愈"链路的起点。
        """
        self.status = state
        self.status_msg = message
        logger.info("串口状态 %s: %s", state.value, message)
        # JSON 出口才转成字符串——前端按 state 的字符串值决定指示灯
        self._broadcast_json({"op": "status", "state": state.value, "message": message})
        # 掉线了但用户还想连着 → 自动重连
        if state is Status.disconnected and self._want_connected and self._running:
            self.ensure_connected()

    def _on_serial_raw(self, chunk: bytes) -> None:
        # 关键设计：上位机看到的串口和脚本看到的是同一股原始字节。
        # 原样广播给 WS（前端 FrameParser 自己拆帧，因此前端代码零改动），
        # 同一份字节再喂 Python FrameParser 做目录缓存/快照/日志（供脚本）。
        self._broadcast_bytes(chunk)
        # 从字节流里挑目录帧缓存，供目录代理回放；未收完的半帧尾巴留到下一批
        self._dir_buf.extend(chunk)
        frames, consumed = scan_dir_frames(bytes(self._dir_buf))
        for cmd, frame in frames:
            self._cache_dir_frame(cmd, frame)
        del self._dir_buf[:consumed]
        # 异常兜底：正常情况半帧尾巴不超过一帧长度（几百字节）。
        # 涨破 8KB 说明解析对不上号（比如波特率错、协议版本不匹配），
        # 保留末尾 4KB 截掉旧数据，宁可丢一点也不能让缓冲无限膨胀。
        if len(self._dir_buf) > 8192:
            del self._dir_buf[:-4096]

    def _on_serial_event(self, event: dict) -> None:
        """FrameParser 解出一个事件就调这里（在串口 rx 线程里执行）。

        event["kind"] 是 EventKind（详见 protocol.py 里各分支的注释）。
        """
        kind = event.get("kind")

        if kind is EventKind.directory:
            table = event["table"]
            item = event["item"]
            idx = item.index
            if table is DirTable.monitor:
                self.monitor_dir[idx] = item
                self._name_to_mon[item.name] = idx  # 中文名反查，脚本用名字寻址
                self.monitor_latest[idx] = MonitorSample(item.value, None)  # 目录帧初值：还没收到过推送
                # 目录帧里带的初值：让 /api/values 在还没收到推送时也有东西可显示
                # 目录帧的 tag 反映下位机当前订阅档位；但已有聚合意图的项以意图为准。
                # 原因：重连后目录先到、补发的 Subscribe 后到——
                # 若这里无条件采信目录 tag，会把"已生效的订阅"倒退成"未订阅"，
                # 等补发到达前这段时间推送是断的。所以有意图时让意图说话。
                if idx not in self._sub_intent:
                    self.fw_sub[idx] = (tag_subscribed(item.tag), tag_fast(item.tag))
            elif table is DirTable.tunable:
                self.tunable_dir[idx] = item
                self._name_to_tun[item.name] = idx

        elif kind is EventKind.cmdDirectory:
            item = event["item"]
            idx = item.index
            self.cmd_dir[idx] = item
            self._name_to_cmd[item.name] = idx
            # 停车命令的字号不固定写死，而是从命令目录的 tag SPEC 位认出来。
            # 这样固件改了命令表顺序，后端也能自动找到正确的停车命令。
            if cmd_spec(item.tag) == CmdSpec.Stop:
                self._stop_cmd_index = idx

        elif kind is EventKind.value:
            idx = event["index"]
            val = event["value"]
            # 只存"最新值"不存历史：历史由 log_dir 负责（且仅登记项才记）
            self.monitor_latest[idx] = MonitorSample(val, time.monotonic())  # 供 age_s 判新鲜度
            # 记录中 且 该被登记过 → 才落样本。未登记的高频项不会撑爆内存
            if self.logging and idx in self.log_reg:
                self._record(idx, val)

        elif kind is EventKind.echo:
            # 调参回显：下位机把"实际生效值"发回来。用它覆盖目录里的 value，
            # 这样界面显示的是车里真实的值，而不是上位机以为自己设的那个
            # （下位机可能做了限幅、取整、拒绝）。
            idx = event["index"]
            if idx in self.tunable_dir:
                self.tunable_dir[idx].value = event["value"]

    # ---------- 下行优先级队列 ----------

    def enqueue(self, frame: bytes, priority: int = PRI_UI) -> None:
        """所有下行帧的唯一入口（上位机透传、脚本命令、订阅、停车都走这里）。

        统一入队的意义：串口只能被一个线程写，多个来源的帧在这里串行化，
        不会出现两个来源同时 write 导致撕帧。
        """
        with self._tx_cv:
            self._tx_seq += 1
            self._tx_q.append((priority, self._tx_seq, frame))
            # 插入后按 (优先级, 序号) 归位：STOP 永远排最前，同级保序
            # 每次入队全排一遍是 O(n log n)。队列通常只有几帧无所谓，
            # 但 set_volt/set_spin 这类命令若高频下发，这里就是白白的内耗。
            self._tx_q = deque(sorted(self._tx_q, key=lambda x: (x[0], x[1])))
            self._tx_cv.notify()

    def _tx_loop(self) -> None:
        """唯一的串口写线程。没事干时睡在 Condition 上，来帧就被唤醒。"""
        while self._running:
            with self._tx_cv:
                while self._running and not self._tx_q:
                    self._tx_cv.wait(timeout=0.1)
                if not self._running:
                    break
                if not self._tx_q:
                    continue
                pri, _seq, frame = self._tx_q.popleft()
            try:
                if self.bridge.connected:
                    self.bridge.send(frame)
                elif pri == PRI_STOP:
                    # 停车帧在断线时不能丢：塞回队首，等重连后立刻补发。
                    # 注意副作用：断线期间这条线程被停车帧独占（每 0.5s 重试一次），
                    # 直到连上把它发出去——这是有意的，停车比别的都重要。
                    # 其余优先级的帧在断线时直接丢弃（不报错，也不重试）。
                    with self._tx_cv:
                        self._tx_q.appendleft((pri, _seq, frame))
                    time.sleep(0.5)
            except Exception as e:
                logger.error("写串口失败: %s", e)

    # ---------- WebSocket ----------

    async def attach_ui(self, ws: WebSocket) -> None:
        """WebSocket 上位机接入。

        注意：接入时只推一条 status 消息，不主动推目录——
        目录由前端连上后自己发请求拿（后端用缓存回放），
        这样前端行为跟"直连串口"时代完全一致，代码不用改。
        """
        await ws.accept()
        with self._ws_lock:
            self._ws_clients.add(ws)
        # 只推连接状态；目录由前端连上后自己发请求（与直连串口行为一致）
        await ws.send_json(
            {"op": "status", "state": self.status.value, "message": self.status_msg}
        )
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"] is not None:
                    self.from_ui(msg["bytes"], ws)
                elif "text" in msg and msg["text"] is not None:
                    try:
                        obj = json.loads(msg["text"])
                    except Exception as e:
                        logger.warning("UI JSON 解析失败: %s", e)
                        continue
                    op = obj.get("op")
                    if op == "stop":
                        self.emergency_stop()
                    elif op == "raw" and "payload" in obj:
                        self.from_ui(bytes(obj["payload"]), ws)
        finally:
            with self._ws_lock:
                self._ws_clients.discard(ws)
            # 该客户端断开后清掉它的订阅意图
            self._drop_client_intent(id(ws))

    def _drop_client_intent(self, conn_id: int) -> None:
        """某页面断开：清掉它的订阅意图，重新聚合后下发 diff。

        这就是为什么订阅要按 source 分开记——否则一个页面关掉，
        会把别的页面正在看的推送一起退订掉。
        """
        source = f"ui:{conn_id}"
        with self._sub_lock:
            changed = []
            for idx, intent in list(self._sub_intent.items()):
                if source in intent:
                    del intent[source]
                    if not intent:
                        del self._sub_intent[idx]
                    changed.append(idx)
            if changed:
                self._recompute_and_emit(set(changed))

    def clear_script_intents(self) -> None:
        """脚本进程结束时调用：去掉 script 侧订阅意图并下发 diff，不影响各 UI 客户端。

        与 emergency_stop 的区别：这里只退订，不停车、不置 abort。
        脚本正常跑完时用它把带宽还给上位机。
        """
        with self._sub_lock:
            changed = []
            for idx, intent in list(self._sub_intent.items()):
                if "script" in intent:
                    del intent["script"]
                    if not intent:
                        del self._sub_intent[idx]
                    changed.append(idx)
            if changed:
                self._recompute_and_emit(set(changed))

    def _recompute_and_emit(self, indices: set[int]) -> None:
        """按当前意图重新聚合，只有与下位机档位不同时才真正发包（diff 后发）。

        为什么要 diff：目录请求/订阅是稀疏操作，但页面频繁开关时若每次都发，
        会给下位机灌一堆无效帧。diff 也能避免把"已经是这个档位"的重发一遍。
        """
        for idx in indices:
            intent = self._sub_intent.get(idx, {})
            new_sub, new_fast = self._aggregate(intent) if intent else (False, False)
            old = self.fw_sub.get(idx, (False, False))
            if (new_sub, new_fast) != old:
                self.fw_sub[idx] = (new_sub, new_fast)
                if new_sub:
                    self.enqueue(subscribe(idx, True, new_fast), PRI_UI)
                else:
                    self.enqueue(subscribe(idx, False, False), PRI_UI)

    def from_ui(self, data: bytes, ws: WebSocket) -> None:
        """上位机发来的原始帧（在 asyncio 事件循环里执行，因为来自 WebSocket）。

        五条处理路径，顺序不能随意换：
          1. Ping    → 直接回 Pong，不转发下位机（语义变了，见下）
          2. 目录请求 → 用缓存回放，不重复打扰下位机
          3. Subscribe → 聚合 + diff 后才发
          4. 停车命令 → 最高优先级 + 中止脚本
          5. 其余     → 原样透传

        ⚠️ 重要：Ping 不再转发下位机。前端的 Ping 现在只验证"Backend 活着"，
        不再验证"下位机活着"。下位机真实状态要看 WS 推的 JSON status 消息。
        """
        if len(data) < 2 or data[0] != 0xA5:
            return
        cmd = data[1]

        # 1) Ping → 直接回 Pong（本进程代答，不下发给下位机）
        #    下位机的心跳由 SerialBridge 自己的 1s Ping 负责，两套心跳互不干扰
        if cmd == CmdGet.Ping:
            try:
                loop = self._loop
                if loop:
                    asyncio.run_coroutine_threadsafe(ws.send_bytes(ping()), loop)
            except Exception as e:
                logger.debug("Ping 回复失败: %s", e)
            return

        # 2) 目录请求 → 缓存回放（N 个页面要目录，下位机只答一轮 burst）
        dir_map = {
            CmdGet.RequestTunable: (CmdPost.TunableDirectory, request_tunable),
            CmdGet.RequestMonitor: (CmdPost.MonitorDirectory, request_monitor),
            CmdGet.RequestCommand: (CmdPost.CMDDirectory, request_command),
        }
        if cmd in dir_map:
            post_cmd, request_fn = dir_map[cmd]
            frames = self._dir_frames_list(post_cmd)
            if frames:
                try:
                    loop = self._loop
                    if loop:
                        for frame in frames:
                            asyncio.run_coroutine_threadsafe(ws.send_bytes(frame), loop)
                except Exception as e:
                    logger.debug("目录回放失败: %s", e)
            else:
                # 缓存空了（还没拉过 / 刚被 refresh 清掉）：真去拉一次。
                # 下位机应答的 burst 会经 _on_serial_raw 广播到达该客户端，不用单独回。
                self._request_dir(post_cmd, request_fn())
            return

        # 3) Subscribe → 聚合处理（防多页面互相退订）
        #    帧格式：A5 | Cmd | index | tag | XOR，tag 的 bit7=SUB bit6=FAST
        if cmd == CmdGet.Subscribe and len(data) >= 5:
            idx = data[2]
            tag = data[3]
            sub = tag_subscribed(tag)
            fast = tag_fast(tag)
            source = f"ui:{id(ws)}"
            with self._sub_lock:
                intent = self._sub_intent.setdefault(idx, {})
                intent[source] = (sub, fast)
                self._recompute_and_emit({idx})
            return

        # 4) 停车命令：插队 + 清空脚本队列 + 置 abort
        if cmd == self._stop_index():
            self.emergency_stop()
            return

        # 5) 其余透传
        self.enqueue(data, PRI_UI)

    def _stop_index(self) -> int:
        # 目录未到时的兜底：本车停车固定是命令表第一项 0x80。
        # 有了目录后以目录为准（从 tag SPEC=Stop 认出来），硬编码只在早期生效。
        return self._stop_cmd_index if self._stop_cmd_index is not None else 0x80

    def _broadcast_json(self, obj: dict) -> None:
        """给所有 WS 客户端推一条 JSON（状态类消息用这个）。

        与 _broadcast_bytes 的区别：字节通道走的是"下位机原样数据"，
        JSON 通道走的是"Backend 自己的状态"（连接状态等），两者互不混淆。
        """
        loop = self._loop
        if loop is None:
            return
        with self._ws_lock:
            clients = list(self._ws_clients)
        if not clients:
            return
        text = json.dumps(obj, ensure_ascii=False, default=str)

        async def _send_all() -> None:
            dead = []
            for ws in clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            if dead:
                with self._ws_lock:
                    for w in dead:
                        self._ws_clients.discard(w)

        try:
            asyncio.run_coroutine_threadsafe(_send_all(), loop)
        except Exception as e:
            logger.debug("JSON 广播失败: %s", e)

    def _broadcast_bytes(self, data: bytes) -> None:
        """把下位机原始字节原样推给所有 WS 客户端。

        投给事件循环后立刻返回，不等发送完成——某个网页慢不会拖住 rx 线程。
        发送失败的连接会被摘除，下次不再给它推。
        """
        loop = self._loop
        if loop is None:
            return
        with self._ws_lock:
            clients = list(self._ws_clients)
        if not clients:
            return

        async def _send_all() -> None:
            dead = []
            for ws in clients:
                try:
                    await ws.send_bytes(data)
                except Exception:
                    dead.append(ws)
            if dead:
                with self._ws_lock:
                    for w in dead:
                        self._ws_clients.discard(w)

        try:
            asyncio.run_coroutine_threadsafe(_send_all(), loop)
        except Exception as e:
            logger.debug("字节广播失败: %s", e)

    # ---------- 脚本 API ----------

    def resolve_monitor(self, index_or_name) -> int:
        """把"index 或中文名"统一解析成 index。

        三种合法输入（HTTP 传进来的就是这么乱）：
            5          int
            "5"        数字字符串（HTTP query 里没有类型）
            "左0转速"   目录里的中文名
        名字匹配最后还有一次忽略大小写的容错，因为界面手打容易拼错。
        """
        if isinstance(index_or_name, int):
            return index_or_name
        name = str(index_or_name)
        # HTTP 传过来的 index 是字符串数字（如 "5"），先按数字解释
        if name.isdigit() and int(name) in self.monitor_dir:
            return int(name)
        if name in self._name_to_mon:
            return self._name_to_mon[name]
        for n, i in self._name_to_mon.items():  # 容错：忽略大小写
            if n.lower() == name.lower():
                return i
        raise KeyError(f"监控项不存在: {index_or_name}")

    def resolve_tunable(self, index_or_name) -> int:
        if isinstance(index_or_name, int):
            return index_or_name
        name = str(index_or_name)
        if name in self._name_to_tun:
            return self._name_to_tun[name]
        for n, i in self._name_to_tun.items():
            if n.lower() == name.lower():
                return i
        raise KeyError(f"调参项不存在: {index_or_name}")

    def resolve_cmd(self, index_or_name) -> CmdDirItem:
        if isinstance(index_or_name, int):
            if index_or_name not in self.cmd_dir:
                raise KeyError(f"命令字不存在: 0x{index_or_name:02X}")
            return self.cmd_dir[index_or_name]
        name = str(index_or_name)
        if name in self._name_to_cmd:
            return self.cmd_dir[self._name_to_cmd[name]]
        for n, i in self._name_to_cmd.items():
            if n.lower() == name.lower():
                return self.cmd_dir[i]
        raise KeyError(f"命令不存在: {index_or_name}")

    def get_column(self, type_: str) -> list[MenuItem | CmdDirItem]:
        t = type_.lower()
        if t in ("monitor", "mon"):
            return list(self.monitor_dir.values())
        if t in ("tunable", "tun"):
            return list(self.tunable_dir.values())
        if t in ("cmd", "command", "commands"):
            return list(self.cmd_dir.values())
        raise ValueError("type 必须是 monitor | tunable | cmd")

    def _mon_name(self, idx: int) -> str:
        """监控项显示名：目录里有就用中文名，目录还没到就用 index 字符串兜底。"""
        it = self.monitor_dir.get(idx)
        return it.name if it else str(idx)

    def read_item(self, index_or_name) -> dict:
        """单项当前值（看门狗用）：index(int) 或目录中文名均可。

        前提：该项必须正在被推送（订阅过 push/log/both 任一），
        否则 monitor_latest 里没有它，只能抛 KeyError——
        Backend 不会为了读一次值就临时去订一下（那样会打乱档位管理）。

        age_s：距上次收到值的秒数。看门狗要判这个值，
        因为"有值"和"值还新鲜"是两回事——推送断了以后旧值会一直留在缓存里。
        """
        idx = self.resolve_monitor(index_or_name)
        if idx not in self.monitor_latest:
            raise KeyError(f"监控项尚无数据（先 push 订阅再 read）: {index_or_name}")
        item = self.monitor_dir.get(idx)
        s = self.monitor_latest[idx]
        return {
            "index": idx,
            "name": item.name if item else str(idx),
            "value": s.value,
            "unit": item.unit if item else "",
            # t_recv=None（只有目录初值、没推送过）按极旧处理，看门狗自然当过期
            "age_s": round(time.monotonic() - (s.t_recv or 0.0), 3),
        }

    def read_items(self, keys: list, missing_ok: bool = False) -> list[dict]:
        """多项当前值，一次返回（看门狗一轮要看 x/y/yaw，避免连发三个请求）。
        missing_ok=True 时跳过还没有数据的项（不抛 404），供批量轮询用。"""
        if not missing_ok:
            return [self.read_item(k) for k in keys]
        out = []
        for k in keys:
            try:
                out.append(self.read_item(k))
            except KeyError:
                continue
        return out

    def read(self, *index_or_name):
        """最便捷读法：单项直接拿数值（hub.read('x')）；
        多项一次返回 {名字: 值}（hub.read('x','y','yaw')）。

        多项一次取的意义：看门狗一轮通常要同时看 x/y/yaw，
        一次请求拿全避免三次往返带来的时间偏差（三个值不是同一时刻的）。
        """
        if len(index_or_name) == 1:
            return self.read_item(index_or_name[0])["value"]
        return {it["name"]: it["value"] for it in self.read_items(list(index_or_name))}

    def sub(
        self,
        index_or_name,
        kind: str = "both",
        *,
        on: bool = True,
        fast: bool | None = None,
    ) -> dict:
        """
        两种订阅可叠加：
          push - 只改下位机推送档位（Subscribe 帧），用于砍带宽，fast 指定高/低速
          log  - 只在后端登记/取消记录；登记时若该项不是高速推送则自动提升成高速，
                 取消登记不动下位机档位（避免把上位机正在看的推送关掉）
          both - 两者都做

        fast 只在 kind 含 "push" 时有意义，不给就跟随 on（订阅即高速、取消即低速），
        所以默认的 sub(x)（both）本身就是高速，可直接 100Hz 采样。
        kind=="log" 分支不看 fast：登记时若该项还不是高速推送，会自动补一次高速。
        """
        idx = self.resolve_monitor(index_or_name)
        kind = kind.lower()
        it = self.monitor_dir.get(idx)
        result = {
            "index": idx,
            "name": it.name if it else None,
            "push": None,
            "log": None,
        }

        if kind in ("push", "both"):
            use_fast = bool(fast) if fast is not None else on
            with self._sub_lock:
                intent = self._sub_intent.setdefault(idx, {})
                intent["script"] = (on, use_fast)
                self._recompute_and_emit({idx})
            result["push"] = {"sub": on, "fast": use_fast}

        if kind in ("log", "both"):
            if on:
                self.log_reg.add(idx)
                # 日志要有数据可记：不是高速推送就自动提升（前提/提升二选一，统一提升）
                if kind == "log":
                    cur = self.fw_sub.get(idx, (False, False))
                    if not cur[1]:
                        with self._sub_lock:
                            intent = self._sub_intent.setdefault(idx, {})
                            intent["script"] = (True, True)
                            self._recompute_and_emit({idx})
                        result["push"] = {"sub": True, "fast": True}
            else:
                self.log_reg.discard(idx)
            result["log"] = on

        return result

    def emergency_stop(self) -> dict:
        """最高优先级停车：发停车帧、清脚本队列、置 abort 打断脚本。

        三步缺一不可：
          1. 置 abort → 脚本在 cmd()/sleep() 处立即中断（不发新命令了）
          2. 清队列里所有 PRI_SCRIPT 帧 → 已排队但还没发的命令全部作废
             （保留 PRI_UI，上位机自己的请求/订阅不该被停车波及）
          3. 以 PRI_STOP 插队发停车帧 → 排在所有帧之前
        """
        self.abort_event.set()
        with self._tx_cv:
            # 只留比脚本优先级高的帧（上位机的请求/订阅不丢），脚本待发帧全清
            self._tx_q = deque(x for x in self._tx_q if x[0] < PRI_SCRIPT)
            self._tx_cv.notify()

        idx = self._stop_index()
        self.enqueue(build_frame(idx, b""), PRI_STOP)
        return {"ok": True, "cmd_index": idx}

    def clear_abort(self) -> None:
        self.abort_event.clear()

    def check_abort(self) -> None:
        if self.abort_event.is_set():
            raise RuntimeError("已紧急停车 / 脚本被中止（调 clear_abort 后才能继续）")

    def cmd(self, index_or_name, *args) -> dict:
        """按命令目录发一条命令（脚本主力写接口）。

        两道严格校验：
          · 参数个数必须与目录完全一致 —— 少给不静默补 0（补 0 可能触发未预期动作），
            多给说明调用方写错了
          · 目录里带取值范围（tag bit7）的字段做越界拦截 —— 别把车开到量程外
        这两处报错请照着改，不要绕过去。
        """
        self.check_abort()
        item = self.resolve_cmd(index_or_name)
        fields = item.fields
        types = [f.type for f in fields]
        values = list(args)
        # 参数个数必须和目录完全一致：少给静默补 0 可能触发未预期动作，多给更是写错
        if len(values) != len(types):
            raise ValueError(
                f"命令 {item.name} 需要 {len(types)} 个参数"
                f"（{[f.name for f in fields]}），实际给了 {len(values)} 个"
            )
        # 目录里带取值范围（tag bit7）的字段顺手校验，越界直接拦，别把车开到量程外
        for f, v in zip(fields, values):
            lo, hi = f.min, f.max
            if lo is None and hi is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"命令 {item.name} 的参数 {f.name} 需要数值，实际给了 {v!r}")
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ValueError(f"命令 {item.name} 的参数 {f.name}={v} 超出范围（{lo}~{hi}）")
        self.enqueue(command_send(item.index, types, values), PRI_SCRIPT)
        return {
            "ok": True,
            "index": item.index,
            "name": item.name,
            "args": values,
        }

    def set_tunable_value(self, index_or_name, value) -> dict:
        self.check_abort()
        idx = self.resolve_tunable(index_or_name)
        item = self.tunable_dir[idx]
        self.enqueue(set_tunable(idx, item.type, value), PRI_SCRIPT)
        return {"ok": True, "index": idx, "value": value}

    # ---------- 记录会话 ----------
    # 一段 = 一次 start_log → end_log。脚本在段内发命令、段末取整段数据；
    # 后端不切窗口，样本时间戳是"相对本段零点（start_log）的毫秒"。
    #
    # 为什么用相对时间而不是墙上时钟：脚本关心的是"发完命令后这 0.4 秒怎么样"，
    # 相对时间自足，也免了跟系统时钟、时区、NTP 校时打交道。
    # 基准用 time.monotonic()，不受系统改时间影响。

    def _record(self, idx: int, value: Any) -> None:
        """记一条样本；超上限丢新样（防脚本忘 end_log 把内存吃光）。

        丢新不丢旧：保留的是从 start_log 开始的完整一段，
        对"扫参看趋势"更有用；丢尾部只是少记后面一点。
        """
        with self._log_lock:
            # logging / _log_t0 都必须在锁内读：start_log 会在锁内重置零点并清空缓冲，
            # 若在锁外算 t，就可能拿旧零点算出巨大时间戳落进新一段——
            # 那条样本会排到列表最前，破坏"时间升序"。
            if not self.logging:
                return
            # 时间轴零点：start_log 那一刻。样本里的 t 都是相对它的毫秒数
            t = round((time.monotonic() - self._log_t0) * 1000, 2)
            if self.log_samples() >= MAX_LOG_SAMPLES:
                self.log_dropped += 1
                return
            self.log_dir.setdefault(idx, []).append((t, value))

    def log_samples(self) -> int:
        """样本总数。⚠️ 内部无锁遍历 dict，调用方必须持有 _log_lock
        （_record 就是持锁调它的——Lock 不可重入，别把锁加进本方法内部）。
        """
        return sum(len(v) for v in self.log_dir.values())

    def start_log(self) -> dict:
        """开始记录：清掉上一段缓冲，并以此刻为本段零点。

        记哪些项由脚本先用 sub(x, 'log'/'both') 登记；
        记录期间新登记/退订即时生效（不用重启记录）。
        """
        with self._log_lock:
            # logging 也要在锁内打开：rx 线程按它判断样本归哪一段，
            # "重置零点 + 开始收样本"必须是同一个原子步骤
            self.log_dir.clear()
            self.log_dropped = 0
            self._log_t0 = time.monotonic()
            self.logging = True
        return {
            "ok": True,
            "registered": [
                {"index": i, "name": self._mon_name(i)}
                for i in sorted(self.log_reg)
            ],
        }

    def end_log(self) -> dict:
        # 两件事一起在锁内做完：关掉收样本、数样本。
        # rx 线程可能正卡在"过了 logging 检查、等锁落盘"那一步，锁住数才不会边遍历边改字典
        with self._log_lock:
            self.logging = False
            return {"ok": True, "samples": self.log_samples(), "dropped": self.log_dropped}

    def _log_indices(self, names) -> list[int]:
        if names is None:
            with self._log_lock:  # 防遍历 keys 时 rx 线程 setdefault 出新 key
                return sorted(self.log_dir)
        if isinstance(names, (str, int)):
            names = [names]
        return [self.resolve_monitor(n) for n in names]

    def _samples(self, idx: int) -> list[tuple[float, Any]]:
        """取某项整段样本的副本 [(t_ms, 值), ...]（按时间升序）。

        在锁内复制一份指针列表再放锁：既避免遍历时 rx 线程 setdefault 出新 key，
        也不把锁持有到统计/导出走完。
        """
        with self._log_lock:
            return list(self.log_dir.get(idx) or ())

    def log_rows(self, names=None) -> dict:
        """取整段原始样本（长表，按时间排序），供脚本自己画图、落盘或自行统计。"""
        idxs = self._log_indices(names)
        rows: list[dict] = []
        for idx in idxs:
            name = self._mon_name(idx)
            for t, v in self._samples(idx):
                rows.append({"t": t, "index": idx, "name": name, "value": v})
        rows.sort(key=lambda r: r["t"])
        return {"rows": rows}

    def log_stats(self, names=None, thresholds=()) -> dict:
        """整段统计（后端算好，脚本不用自己攒样本）。

        thresholds 里每个 th 额外给出 ratio_ge[th] = |值| >= th 的样本占比。
        这个指标专门为标定设计：比如"超速时间占比 30%"比"平均超了多少"
        更能说明增益给大了。取绝对值是因为正负超调都算问题。
        """
        idxs = self._log_indices(names)
        items = []
        for idx in idxs:
            name = self._mon_name(idx)
            samples = self._samples(idx)
            vals = [v for _, v in samples]
            # rate_hz 用样本自身跨度算（首个到末个时间戳），不依赖任何窗口
            span_ms = samples[-1][0] - samples[0][0] if len(samples) >= 2 else 0.0
            items.append({"index": idx, "name": name, **_aggregate(vals, thresholds, span_ms)})
        return {"items": items}

    def print_log(self, path: str | None = None) -> dict:
        """导出记录，格式由 path 的后缀决定（只写一种，不再一次落两份）：

          path 后缀  导出内容
          .json      全量 JSON（无后缀时的默认）
          .csv       长表 CSV（t_ms,index,name,value），Excel/pandas 直读
          .png/.jpg  按监控项作图，每项一条曲线（x=相对本段零点秒）
          其它后缀   当 JSON 处理

        path 里带子目录（如 "pytest/l0/start.csv"）会自动创建；
        不传 path 则默认落 pytest/log_<时间戳>.json。
        """
        if path:
            target = Path(path)
            if not target.suffix:  # 没标后缀 → 按默认 json
                target = target.with_suffix(".json")
        else:
            target = self.pytest_dir / f"log_{datetime.now():%Y%m%d_%H%M%S}.json"
        target.parent.mkdir(parents=True, exist_ok=True)

        rows = self.log_rows()["rows"]
        suffix = target.suffix.lower()
        if suffix == ".csv":
            self._write_csv(target, rows)
        elif suffix in (".png", ".jpg", ".jpeg"):
            self._write_plot(target, rows)
        else:
            self._write_json(target, rows)

        return {
            "ok": True,
            "path": str(target),
            "format": suffix.lstrip("."),
            "samples": len(rows),
            "dropped": self.log_dropped,
        }

    def _write_json(self, target: Path, rows: list[dict]) -> None:
        """全量 JSON：元信息 + 原始样本长表。"""
        meta = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "samples": len(rows),
            "dropped": self.log_dropped,
            "registered": [
                {"index": i, "name": self._mon_name(i)}
                for i in sorted(self.log_reg)
            ],
            "data": rows,
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, default=str)

    def _write_csv(self, target: Path, rows: list[dict]) -> None:
        """长表 CSV：每条推送一行；utf-8-sig 带 BOM，Excel 打开中文不乱码。"""
        with open(target, "w", encoding="utf-8-sig") as f:
            f.write("t_ms,index,name,value\n")
            for row in rows:
                f.write(f"{row['t']},{row['index']},{row['name']},{row['value']}\n")

    def _write_plot(self, target: Path, rows: list[dict]) -> None:
        """按监控项作图并导出图片：每项一条曲线，横轴为相对本段零点的秒。

        matplotlib 延迟导入——不画图的后端无需安装它。
        """
        import matplotlib

        matplotlib.use("Agg")  # 后端无 GUI，必须用非交互后端
        import matplotlib.pyplot as plt

        # 监控项多是中文名，Windows 常见中文字体兜底，避免图例变成方块
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        series: dict[str, list[tuple[float, float]]] = {}
        for r in rows:
            series.setdefault(r["name"], []).append((r["t"] / 1000.0, r["value"]))

        fig, ax = plt.subplots(figsize=(12, 6))
        for name, pts in series.items():
            ax.plot([p[0] for p in pts], [p[1] for p in pts], linewidth=1, label=name)
        ax.set_xlabel("t / s")
        ax.set_ylabel("value")
        ax.grid(True, alpha=0.3)
        if series:
            ax.legend(loc="best", fontsize=8)
        fig.tight_layout()
        fig.savefig(target, dpi=120)
        plt.close(fig)

    def snapshot(self) -> dict:
        """/api/status 的返回体：一眼看清 Backend 全貌，调试脚本连不上的第一站。

        logging / log_samples / log_dropped 三个一起看能判断记录是否健康：
        dropped > 0 说明撞了 50 万上限，这次记录不完整。
        """
        with self._log_lock:  # 记录中被轮询时 rx 在 append，数样本防字典变尺寸
            log_samples = self.log_samples()
        return {
            "status": self.status.value,
            "message": self.status_msg,
            "connected": self.bridge.connected,
            "monitor_count": len(self.monitor_dir),
            "tunable_count": len(self.tunable_dir),
            "cmd_count": len(self.cmd_dir),
            "log_reg": sorted(self.log_reg),
            "logging": self.logging,
            "log_samples": log_samples,
            "log_dropped": self.log_dropped,
            "abort": self.abort_event.is_set(),
            "stop_cmd": self._stop_cmd_index,
            "ports": SerialBridge.list_ports(),
        }
