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
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .protocol import (
    CMD_TAG_RANGE,
    CmdGet,
    CmdPost,
    CmdSpec,
    ValueType,
    build_frame,
    cmd_spec,
    command_send,
    decode_value,
    ping,
    request_command,
    request_monitor,
    request_tunable,
    set_tunable,
    subscribe,
    tag_fast,
    tag_subscribed,
    value_size,
)
from .dataclass import (
    AggregateStats,
    CalcResult,
    CmdDirItem,
    CmdField,
    CmdResult,
    DirEntry,
    EndLogResult,
    LogFileMeta,
    LogRow,
    LogRowsResult,
    LogStatsItem,
    LogStatsResult,
    MenuItem,
    MonitorReading,
    MonitorSample,
    OkResult,
    PrintLogResult,
    PushState,
    RegisteredItem,
    SetTunableResult,
    Snapshot,
    StartLogResult,
    StatusMessage,
    StopResult,
    SubResult,
)
from .serial_bridge import PortInfo, SerialBridge, Status

logger = logging.getLogger("vex.hub")

# 新鲜度判据：监控项落后最新监控帧超过这么多 ms 即算旧（看门狗用）。
# 300ms = 固件 LINK_PATROL_TICKS(30 拍) 的掉线巡视周期——全系统同一个时间尺：
# 固件 300ms 没见 Ping 就停推，后端就敢把 300ms 没更新的项判旧。
# 余量：低速项 80ms 才刷新一次（SLOW_PHASES=8 × 10ms 拍），300ms 是其 3.75 倍，
# 正常流转不会被误判；真停推/丢帧时 300ms 内必现形。阈值策略留给消费方，这里只定数。
FW_LAG_STALE_MS = 300

# lag 外推量封顶（ms）。前沿 = 最后采纳帧的固件戳 + 此后流逝的 PC 时间；
# 封顶防持续掉线 65.5s 后 mod 65536 绕圈，把"极旧"伪装成"新鲜"。
# 60s 远大于任何合理重连周期（5s 一轮），也不会压缩正常判据（300ms）。
FW_LAG_EXTRAPOLATE_CAP_MS = 60000

# ---------- 下行优先级 ----------
PRI_STOP = 0 # 0停车帧。永远第一
PRI_UI = 10 #上位机：网页按钮、目录请求、订阅。人工操作，比脚本优先
PRI_SCRIPT = 20 #脚本下发的命令/调参

# ---------- 重连节奏 ----------
# 掉线后每 5s 试一轮：一轮里每个 VEX 候选口只开一次、最多等 120ms（见 SerialBridge），
# 单个口每轮最多被占 120ms。再快只会频繁打扰下位机和其他用口的进程（如 VS Code 烧录）
RECONNECT_INTERVAL_S = 5.0

# ---------- 日志缓冲上限 ----------
# 脚本忘了 end_log 会一直攒到内存吃满。超出后"丢新不丢旧"：
# 保留已记录的时间窗完整，事后按窗口统计仍有意义
MAX_LOG_SAMPLES = 500_000

# 默认 pytest 输出目录（相对 backend/）
DEFAULT_PYTEST_DIR = Path(__file__).resolve().parent.parent / "pytest"


def _aggregate(vals: list[float], thresholds=(), span_ms: float = 0.0) -> AggregateStats:
    """区间样本的统计量；无样本时除 n 外全为 None。"""
    n = len(vals)
    if n == 0:
        return AggregateStats(0, None, None, None, None, None, None, None, {})
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return AggregateStats(
        n=n,
        mean=mean,
        abs_mean=sum(abs(v) for v in vals) / n,
        min=min(vals),
        max=max(vals),
        abs_max=max(abs(v) for v in vals),
        std=var ** 0.5,
        rate_hz=round(n / (span_ms / 1000.0), 2) if span_ms > 0 else None,
        ratio_ge={str(th): sum(1 for v in vals if abs(v) >= th) / n for th in thresholds},
    )


def _check_field_type(what: str, ftype: int, value: Any) -> None:
    """按目录字段声明类型校验一个要下发/写入的值；不符抛 ValueError（路由层转 400）。

    为什么在边界挡而不留给 protocol 编帧：编帧是最后一站，出错时"这是哪个
    命令的哪个参数"已经丢了；且旧实现对非数值静默编 0——笔误（如把 "6.0"
    当电压传给车）会伪装成"指令生效且值为 0"，是最难排查的一类故障。
    数值字段（含 Bool/OnOff）只收 int/float；Color 字段只收 '#RRGGBB' 字符串。
    bool 是 int 的子类，但把 true 当电压/转速传几乎总是笔误，仅开关字段放行。
    """
    if ftype == ValueType.Color:
        if not isinstance(value, str):
            raise ValueError(
                f"{what} 需要颜色（'#RRGGBB' 字符串），实际给了 {type(value).__name__}: {value!r}"
            )
        return  # 十六进制格式由 encode_value8 校验（int(s, 16) 抛 ValueError）
    if isinstance(value, bool):
        if ftype not in (ValueType.Bool, ValueType.OnOff):
            raise ValueError(f"{what} 需要数值，实际给了布尔值 {value!r}")
    elif not isinstance(value, (int, float)):
        raise ValueError(f"{what} 需要数值(int/float)，实际给了 {type(value).__name__}: {value!r}")


class Hub:
    """整车调试的状态中枢：独占一个 SerialBridge，向上管 WebSocket 上位机，向下管脚本 API。

    职责边界：
        · 收：串口字节 → 原样广播给 WS 上位机 + 喂 FrameParser 更新目录/快照/日志
        · 发：所有下行帧经 enqueue 进唯一优先级队列（STOP < 上位机 < 脚本）串行写串口
        · 管：目录缓存与名字反查、订阅意图聚合、记录会话、紧急停车/脚本中止
    单例：整个进程只应有一个（main.py 里是模块级 hub），别在别处再 new。
    """

    def __init__(
        self,
        port: str | None = None, # 串口端口
        baud: int = 115200, # usb 中仅占位
        pytest_dir: Path | None = None, # pytest 输出目录
    ) -> None:
        """建 Hub：组装 SerialBridge（回调指回本类），建目录缓存/队列/锁等全部内部状态。

        port      : 串口名；None=交给 SerialBridge 自动探测
        baud      : 波特率（CDC 虚拟口仅占位）
        pytest_dir: print_log 不传 path 时的默认落盘目录；None 用 backend/pytest
        注意：这里只建对象不开串口，真正连接由 start()/ensure_connected() 触发。
        """
        self.bridge = SerialBridge(
            port=port,
            baud=baud,
            on_frame=self._on_frame, # 完整帧回调 (cmd, payload)
            on_status=self._on_serial_status, # 串口状态回调
            on_raw=self._on_serial_raw, # 串口原始数据回调
        )
        '''串口桥：本 Hub 独占，收发的唯一落点（连接/探测/心跳都由它管）'''
        self.pytest_dir = Path(pytest_dir) if pytest_dir else DEFAULT_PYTEST_DIR
        '''print_log 不传 path 时的默认落盘目录；构造时确保存在'''
        self.pytest_dir.mkdir(parents=True, exist_ok=True)

        # 三张目录表 index → DirEntry（item=解析结果，frame=完整原始帧供回放）
        self.tunable_dir: dict[int, DirEntry] = {}
        '''可调参数目录 {index: DirEntry}'''
        self.monitor_dir: dict[int, DirEntry] = {}
        '''监控项目目录 {index: DirEntry}'''
        self.cmd_dir: dict[int, DirEntry] = {}
        '''命令目录 {index: DirEntry}'''

        # name → index 反查（脚本可用中文名寻址）
        self._name_to_mon: dict[str, int] = {}
        '''监控项中文名 → index'''
        self._name_to_tun: dict[str, int] = {}
        '''可调参数中文名 → index '''
        self._name_to_cmd: dict[str, int] = {}
        '''命令中文名 → index '''

        self.monitor_latest: dict[int, MonitorSample] = {}
        # 最新监控帧的下位机原始戳（rx 线程维护；HTTP 线程只读单属性，GIL 原子）。
        # None=还没收到过任何监控值帧。采纳规则：mod 65536 下"领先即采纳"；
        # 断流 >1s 后的第一帧无条件采纳——长掉线/重启恢复时新流的戳在 mod 意义下
        # 可能"落后"，但它是我们手里最新的东西，必须当前沿。连续流中"落后"的帧
        # 只可能是重复帧，不采纳（保守地让各项保持自己的戳）。
        self._fw_frontier: int | None = None
        self._fw_frontier_at: float = 0.0
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

        # t_ms:相对 _log_stamp0 的下位机毫秒（不是到达时刻）,每条按时间升序 append；
        self.log_dir: dict[int, list[tuple[int, Any]]] = {}
        '''记录样本缓冲 {index: [(t_ms:int, value:Any), ...]}'''

        # >0 说明这轮记录不完整；end_log / print_log / snapshot 都会带上它。
        self.log_dropped = 0
        '''到 MAX_LOG_SAMPLES后 丢弃的新样本数'''

        self._stamp_raw = -1
        '''下位机时间戳展开：上一帧的原始 uint16；-1=还没收到过（与前端 expandStamp 同构）'''

        self._stamp_ms = 0
        '''下位机时间戳展开：累加出的连续时刻（毫秒）'''

        self._log_stamp0 = 0
        '''start_log 那一刻的下位机时刻；样本里的 t_ms 都相对它'''

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

        # 停车命令的 index。从命令目录 tag 的 SPEC=Stop 位认出来（_on_frame），
        # 固件改了命令表顺序也不怕；目录没到时兜底用 0x80（见 _stop_index）。
        self._stop_cmd_index: int | None = None
        '''停车命令 index：命令目录 tag 的 SPEC=Stop 自动认出；None=目录未到（兜底 0x80）'''

        # ---------- 自动重连（Backend ↔ 下位机） ----------

        # "用户想不想连"的开关，与"实际连没连上"是两回事。
        # True：掉线自动重连；False：重连线程停手（比如用户点了断开）。
        self._want_connected = False
        '''连接意愿开关：True=掉线自动重连（与实际连没连上是两回事）'''

        # 重连后台线程（_reconnect_loop：每 5s 试一次直到连上或不再想连）。
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
        """start(auto_connect=True) 时开的一次性线程：探测连上串口并拉三张表。

        串口探测要逐口开机关机 + 发 Ping 等应答，有阻塞，所以放线程里，
        FastAPI 的 startup 才能立刻返回、HTTP 接口马上可用。连上后开心跳 + 拉目录。
        """
        if self.bridge.connect():
            self.bridge.start_heartbeat(0.08)
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
        """重连线程主循环：每 5s 试一次，直到连上或用户不再想连（_want_connected=False）。

        一轮 connect() 里每个 VEX 候选口只开一次、最多等 120ms，所以单个口每 5s
        最多被抢占 120ms；间隔取 3~10s 的中庸值，兼顾掉线自愈速度和少打扰。
        重连成功必须做三件事：清目录缓存 + 重拉三张表 + 补发订阅——
        下位机重启后目录内容可能变了，且它的订阅表是空的。做完即退出本线程。
        """
        while self._want_connected and not self.bridge.connected:
            if self.bridge.connect():
                self.bridge.start_heartbeat(0.08)
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
            time.sleep(RECONNECT_INTERVAL_S)

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

    # ---------- 目录请求与回放 ----------

    def _request_dir(self, table: dict[int, DirEntry], names: dict[str, int], request_frame: bytes) -> None:
        """主动拉某张表前先清它的目录项和名字反查，避免新旧两轮 burst 混叠。

        比如目录从 20 项变成 18 项：不清的话新 burst 只覆盖前 18 个 index，
        第 19、20 项会作为陈旧残留留在表里，前端就会看到两个已经不存在的项。
        （连上/重连时三张表整体已清，这里管的是运行中主动重拉。）
        """
        table.clear()
        names.clear()
        self.enqueue(request_frame, PRI_UI)

    def _request_all_dirs(self) -> None:
        """拉三张表：调参目录、监控目录、命令目录。连上和重连后都要调。"""
        self._request_dir(self.tunable_dir, self._name_to_tun, request_tunable())
        self._request_dir(self.monitor_dir, self._name_to_mon, request_monitor())
        self._request_dir(self.cmd_dir, self._name_to_cmd, request_command())

    def refresh_directories(self) -> OkResult:
        """清空三张目录表并重新向车请求（表结构变更或怀疑缓存过期时用）。

        与被动更新的区别：目录是下位机启动/订阅时推的，平时不会主动重推。
        改了固件里的表定义后必须调这个，否则后端一直用旧表解析，字段会对不上。
        """
        self._request_all_dirs()
        return OkResult(ok=True)

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
        self._broadcast_json(StatusMessage(op="status", state=state.value, message=message))
        # 连上（含重连）时丢掉旧的展开基准：断连期间下位机时钟与旧基准已不连续
        if state is Status.connected:
            self._stamp_raw = -1
        # 掉线了但用户还想连着 → 自动重连
        if state is Status.disconnected and self._want_connected and self._running:
            self.ensure_connected()

    def _on_serial_raw(self, chunk: bytes) -> None:
        """串口收到一批原始字节的回调（SerialBridge 注入，在 rx 线程里跑）。

        chunk: 本次读到的原始字节（可能含半帧/多帧）
        只做一件事：原样广播给 WS 上位机——前端 FrameParser 自己拆帧，
        前端代码与"直连串口"时代完全一致。后端自己的目录/快照/日志走
        另一条已解帧的通道（_on_frame），不在这股字节里旁路扫描。
        """
        self._broadcast_bytes(chunk)

    def _on_frame(self, cmd: int, payload: bytes) -> None:
        """FrameParser 解出一完整帧就调这里（在串口 rx 线程里执行）。

        帧边界与校验已由 protocol.FrameParser 完成；这里按命令字做语义解析。
        类型真源是三张目录表（item.type），不另存类型表；目录项同时带
        完整原始帧，供前端请求目录时回放。
        """
        p = payload

        if cmd in (CmdPost.TunableDirectory, CmdPost.MonitorDirectory):
            # [index][type][tag][名长][名][单位长][单位][值]（布局见 protocol._dir_len）
            if len(p) < 4:
                return
            index = p[0]
            type_ = p[1]
            tag = p[2]
            name_len = p[3]
            unit_len_at = 4 + name_len
            if len(p) < unit_len_at + 1:
                return
            unit_len = p[unit_len_at]
            value_at = 5 + name_len + unit_len
            if len(p) < value_at:
                return
            name = p[4:unit_len_at].decode("utf-8", errors="replace")
            unit = p[unit_len_at + 1 : value_at].decode("utf-8", errors="replace")
            value = decode_value(p, value_at, type_) if value_size(type_) else 0
            # 完整原帧随 payload 重建（字节一致），目录请求回放用
            frame = build_frame(cmd, p)
            item = MenuItem(index=index, type=type_, tag=tag, name=name, unit=unit, value=value)
            if cmd == CmdPost.MonitorDirectory:
                self.monitor_dir[index] = DirEntry(item, frame)
                self._name_to_mon[name] = index  # 中文名反查，脚本用名字寻址
                self.monitor_latest[index] = MonitorSample(value, None)  # 目录帧初值：还没收到过推送
                # 目录帧的 tag 反映下位机当前订阅档位；但已有聚合意图的项以意图为准。
                # 原因：重连后目录先到、补发的 Subscribe 后到——
                # 若这里无条件采信目录 tag，会把"已生效的订阅"倒退成"未订阅"，
                # 等补发到达前这段时间推送是断的。所以有意图时让意图说话。
                if index not in self._sub_intent:
                    self.fw_sub[index] = (tag_subscribed(tag), tag_fast(tag))
            else:
                self.tunable_dir[index] = DirEntry(item, frame)
                self._name_to_tun[name] = index

        elif cmd == CmdPost.CMDDirectory:
            # payload 头 2B 是内容长度，内容从 off=2 起：[命令字][tag][名长][名][参数量][字段...]
            if len(p) < 5:
                return
            off = 2
            index = p[off]
            tag = p[off + 1]
            name_len = p[off + 2]
            name_at = off + 3
            off = name_at + name_len
            if len(p) < off + 1:
                return
            name = p[name_at:off].decode("utf-8", errors="replace")
            field_num = p[off]
            off += 1
            fields: list[CmdField] = []
            for _ in range(field_num):
                if len(p) < off + 2:
                    return
                ftype = p[off]
                fname_len = p[off + 1]
                off += 2
                fname = p[off : off + fname_len].decode("utf-8", errors="replace")
                off += fname_len
                fields.append(CmdField(type=ftype, name=fname))
            # RANGE 置位：后面紧跟每个参数一对 (min,max)，宽度与类型与该字段一致
            if tag & CMD_TAG_RANGE:
                for f in fields:
                    sz = value_size(f.type)
                    if sz == 0 or len(p) < off + 2 * sz:
                        break  # str/other 无范围；或帧被截断
                    f.min = decode_value(p, off, f.type)
                    f.max = decode_value(p, off + sz, f.type)
                    off += 2 * sz
            self.cmd_dir[index] = DirEntry(
                CmdDirItem(index=index, tag=tag, name=name, fields=fields), build_frame(cmd, p)
            )
            self._name_to_cmd[name] = index
            # 停车命令的字号不固定写死，而是从命令目录的 tag SPEC 位认出来。
            # 这样固件改了命令表顺序，后端也能自动找到正确的停车命令。
            if cmd_spec(tag) == CmdSpec.Stop:
                self._stop_cmd_index = index

        elif cmd == CmdPost.MonitorValue:
            # [payloadlen][time 2B]([index][值])×N：类型查监控目录；
            # 遇目录里没有的 index 丢剩余（前缀已解出的照常发）
            if len(p) < 3:
                return
            t_ms = p[1] | (p[2] << 8)
            stamp_ms = self._expand_stamp(t_ms)  # 下位机采样时刻（回绕已展开）
            # 前沿推进（lag_ms 的"最新帧"基准）：mod 65536 下领先即采纳；
            # 断流 >1s 后的第一帧无条件采纳（掉线/重启恢复的新流在 mod 意义下
            # 可能"落后"，但是手上最新）；连续流中"落后"的帧只可能是重复帧。
            fw = t_ms
            if (self._fw_frontier is None
                    or (fw - self._fw_frontier) & 0xFFFF < 0x8000
                    or time.monotonic() - self._fw_frontier_at > 1.0):
                self._fw_frontier = fw
                self._fw_frontier_at = time.monotonic()
            # 顺序过 payload：目录里有的项按登记类型解，遇没有的 index 丢剩余
            off = 3
            while off < len(p):
                idx = p[off]
                entry = self.monitor_dir.get(idx)
                if entry is None:
                    break
                type_ = entry.item.type
                value = decode_value(p, off + 1, type_)
                self.monitor_latest[idx] = MonitorSample(value, time.monotonic(), t_ms)  # t_fw 供 lag_ms 判新鲜度
                # 记录中 且 该被登记过 → 才落样本。未登记的高频项不会撑爆内存
                if self.logging and idx in self.log_reg:
                    self._record(idx, value, stamp_ms)
                off += 1 + value_size(type_)

        elif cmd == CmdPost.TunableEcho:
            # [type][index][value]：type 前缀自带宽度，不查目录。
            # 下位机把"实际生效值"发回来，用它覆盖目录里的 value——界面显示的
            # 是车里真实的值，而不是上位机以为自己设的那个（可能被限幅/取整/拒绝）。
            if len(p) < 2 or value_size(p[0]) == 0:
                return
            entry = self.tunable_dir.get(p[1])
            if entry is not None:
                entry.item.value = decode_value(p, 2, p[0])
        # Pong：时间戳在 SerialBridge 里已更新，这里无事

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
            asdict(StatusMessage(op="status", state=self.status.value, message=self.status_msg))
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

        # 2) 目录请求 → 回放目录项里的完整帧（N 个页面要目录，下位机只答一轮 burst）
        dir_map = {
            CmdGet.RequestTunable: (self.tunable_dir, self._name_to_tun, request_tunable),
            CmdGet.RequestMonitor: (self.monitor_dir, self._name_to_mon, request_monitor),
            CmdGet.RequestCommand: (self.cmd_dir, self._name_to_cmd, request_command),
        }
        if cmd in dir_map:
            table, names, request_fn = dir_map[cmd]
            frames = [e.frame for _, e in sorted(table.items())]
            if frames:
                try:
                    loop = self._loop
                    if loop:
                        for frame in frames:
                            asyncio.run_coroutine_threadsafe(ws.send_bytes(frame), loop)
                except Exception as e:
                    logger.debug("目录回放失败: %s", e)
            else:
                # 表空了（还没拉过）：真去拉一次。
                # 下位机应答的 burst 会广播到达所有客户端，不用单独回。
                self._request_dir(table, names, request_fn())
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
        """停车命令用的 index：有目录以目录认出的为准，没到则兜底 0x80。

        返回: 停车命令字（命令目录 tag SPEC=Stop 认出的 index，或目录未到时的硬编码 0x80）
        """
        # 目录未到时的兜底：本车停车固定是命令表第一项 0x80。
        # 有了目录后以目录为准（从 tag SPEC=Stop 认出来），硬编码只在早期生效。
        return self._stop_cmd_index if self._stop_cmd_index is not None else 0x80

    def _broadcast_json(self, msg: StatusMessage) -> None:
        """给所有 WS 客户端推一条状态 JSON 消息（StatusMessage → JSON）。

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
        text = json.dumps(asdict(msg), ensure_ascii=False, default=str)

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
        """把"index 或中文名"解析成调参项 index；名字最后忽略大小写容错。

        index_or_name: int（直接返回）或 str（数字按 index、否则按调参目录中文名查）
        返回: 调参项 index
        抛错: KeyError 名字不在调参目录里
        """
        if isinstance(index_or_name, int):
            return index_or_name
        name = str(index_or_name)
        # HTTP 传过来的 index 是字符串数字（如 "3"），先按数字解释（与 resolve_monitor 一致）
        if name.isdigit() and int(name) in self.tunable_dir:
            return int(name)
        if name in self._name_to_tun:
            return self._name_to_tun[name]
        for n, i in self._name_to_tun.items():
            if n.lower() == name.lower():
                return i
        raise KeyError(f"调参项不存在: {index_or_name}")

    def resolve_cmd(self, index_or_name) -> CmdDirItem:
        """把"命令字或中文名"解析成命令目录项；名字最后忽略大小写容错。

        index_or_name: int（命令字，必须是命令目录里的 index）或 str（命令中文名）
        返回: 该命令的 CmdDirItem（含字段类型/名字/范围，供 Hub.cmd 组帧与校验）
        抛错: KeyError 命令字/名字不在命令目录里
        """
        if isinstance(index_or_name, int):
            if index_or_name not in self.cmd_dir:
                raise KeyError(f"命令字不存在: 0x{index_or_name:02X}")
            return self.cmd_dir[index_or_name].item
        name = str(index_or_name)
        if name in self._name_to_cmd:
            return self.cmd_dir[self._name_to_cmd[name]].item
        for n, i in self._name_to_cmd.items():
            if n.lower() == name.lower():
                return self.cmd_dir[i].item
        raise KeyError(f"命令不存在: {index_or_name}")

    def get_column(self, type_: str) -> list[MenuItem | CmdDirItem]:
        """按类型取整张目录列表（/api/column 用）。

        type_: "monitor"/"mon"（实参表）、"tunable"/"tun"（调参表）、"cmd"/"command"/"commands"（命令表）
        返回: 对应目录的项列表（monitor/tunable 是 MenuItem，cmd 是 CmdDirItem）
        抛错: ValueError 类型不认识
        """
        t = type_.lower()
        if t in ("monitor", "mon"):
            return [e.item for e in self.monitor_dir.values()]
        if t in ("tunable", "tun"):
            return [e.item for e in self.tunable_dir.values()]
        if t in ("cmd", "command", "commands"):
            return [e.item for e in self.cmd_dir.values()]
        raise ValueError("type 必须是 monitor | tunable | cmd")

    def _mon_name(self, idx: int) -> str:
        """监控项显示名：目录里有就用中文名，目录还没到就用 index 字符串兜底。"""
        entry = self.monitor_dir.get(idx)
        return entry.item.name if entry else str(idx)

    def read_item(self, index_or_name) -> MonitorReading:
        """单项当前值（看门狗用）：index(int) 或目录中文名均可。

        前提：该项必须正在被推送（订阅过 push/log/both 任一），
        否则 monitor_latest 里没有它，只能抛 KeyError——
        Backend 不会为了读一次值就临时去订一下（那样会打乱档位管理）。

        新鲜度两个量，分工不同（"有值"和"值还新鲜"是两回事——推送断了以后
        旧值会一直留在缓存里）：
          lag_ms：落后最新监控帧多少 ms（下位机时间轴）。**看门狗主判据**——
                  固件全量推送，正常项最多 80ms 必出现一次；lag_ms is None
                  （只有目录初值）或 > FW_LAG_STALE_MS(300) 即旧。对恒定传输
                  延迟和 PC 侧调度抖动免疫（相消/不进这条时间轴）。
          age_s ：距上次收到推送的秒数（PC 时间轴）。到达新近度，恒定传输延迟
                  下会骗人（全体看起来都新鲜）——降级为诊断量，排查"是不是
                  PC/传输侧卡了"看它。
        ⚠ 链路整体冻结（掉线/固件卡死）时 lag 怎么办：前沿本身钉在最后帧不动，
        但 read 时按 PC 时钟外推（前沿戳 + 距其到达时刻的时长，封顶 60s）——
        冻结期间 lag 持续增长，300ms 后同样报旧，不存在"没人报"的窗口。
        外推假设 PC 与固件时钟 1:1，只在相邻帧间隔内有效：每来一个领先帧就
        重锚（_fw_frontier_at 随之刷新），晶振 ppm 级误差 × 秒级间隔 = 微秒级，
        可忽略。心跳（300ms 无 Pong 判掉线）仍是链路活性判据，与 lag 报旧
        互为见证，不是替代关系。
        """
        idx = self.resolve_monitor(index_or_name)
        if idx not in self.monitor_latest:
            raise KeyError(f"监控项尚无数据（先 push 订阅再 read）: {index_or_name}")
        entry = self.monitor_dir.get(idx)
        s = self.monitor_latest[idx]
        # lag_ms：落后前沿多少 ms（mod 65536 相减，回绕/长间隔都精确）。
        # 前沿 = 最后采纳帧的固件戳 + 此后流逝的 PC 时间（外推，封顶 60s 防绕圈）：
        # 正常流里前沿每 10ms 就被新帧重锚，外推量 ≈0，lag 是纯相对差 ∈[0,80]
        # （低速项上限），免疫传输延迟；链路冻结时前沿钉住不动，外推让 lag
        # 随 PC 时间增长——300ms 后报旧，age_s 当初的唯一正确用途并进来了。
        lag_ms = None
        if s.t_fw is not None and self._fw_frontier is not None:
            elapsed_ms = min(
                (time.monotonic() - self._fw_frontier_at) * 1000.0,
                FW_LAG_EXTRAPOLATE_CAP_MS,
            )
            lag_ms = float((self._fw_frontier + int(elapsed_ms) - s.t_fw) & 0xFFFF)
        return MonitorReading(
            index=idx,
            name=entry.item.name if entry else str(idx),
            value=s.value,
            unit=entry.item.unit if entry else "",
            # t_recv=None（只有目录初值、没推送过）按极旧处理，看门狗自然当过期
            age_s=round(time.monotonic() - (s.t_recv or 0.0), 3),
            lag_ms=lag_ms,
        )

    def read_items(self, keys: list, missing_ok: bool = False) -> list[MonitorReading]:
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

    def sub(
        self,
        index_or_name,
        kind: str = "both",
        *,
        on: bool = True,
        fast: bool | None = None,
    ) -> SubResult:
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
        entry = self.monitor_dir.get(idx)
        result = SubResult(index=idx, name=entry.item.name if entry else None, push=None, log=None)

        if kind in ("push", "both"):
            use_fast = bool(fast) if fast is not None else on
            with self._sub_lock:
                intent = self._sub_intent.setdefault(idx, {})
                intent["script"] = (on, use_fast)
                self._recompute_and_emit({idx})
            result.push = PushState(sub=on, fast=use_fast)

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
                        result.push = PushState(sub=True, fast=True)
            else:
                self.log_reg.discard(idx)
            result.log = on

        return result

    def emergency_stop(self) -> StopResult:
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
        return StopResult(ok=True, cmd_index=idx)

    def clear_abort(self) -> None:
        """清掉中止旗标：脚本被停车打断后，再跑新脚本前必须先调它，否则 cmd() 直接抛错。"""
        self.abort_event.clear()

    def check_abort(self) -> None:
        """中止检查点：abort 置位就抛 RuntimeError，供脚本在命令/等待处调用。

        抛错: RuntimeError 已停车（上位机按了停车），需 clear_abort() 才能继续
        """
        if self.abort_event.is_set():
            raise RuntimeError("已紧急停车 / 脚本被中止（调 clear_abort 后才能继续）")

    def cmd(self, index_or_name, *args) -> CmdResult:
        """按命令目录发一条命令（脚本主力写接口）。

        三道严格校验：
          · 参数个数必须与目录完全一致 —— 少给不静默补 0（补 0 可能触发未预期动作），
            多给说明调用方写错了
          · 参数类型必须符合目录字段声明 —— 数值字段只收 int/float，Color 字段只收
            "#RRGGBB" 字符串；字符串/None/列表/布尔一律 400，不静默编 0
          · 目录里带取值范围（tag bit7）的字段做越界拦截 —— 别把车开到量程外
        这几处报错请照着改，不要绕过去。
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
        for f, v in zip(fields, values):
            # 类型校验对每个字段生效（不止带 RANGE 的）：字符串/None/列表/布尔
            # 一律 400 说清楚，绝不带到编帧层静默编 0
            _check_field_type(f"命令 {item.name} 的参数 {f.name}", f.type, v)
            # 目录里带取值范围（tag bit7）的字段查越界，别把车开到量程外。
            # Color 的 min/max 解出来是颜色串、不比大小（本车命令表也没有 Color 字段）
            if f.type != ValueType.Color:
                lo, hi = f.min, f.max
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    raise ValueError(f"命令 {item.name} 的参数 {f.name}={v} 超出范围（{lo}~{hi}）")
        self.enqueue(command_send(item.index, types, values), PRI_SCRIPT)
        return CmdResult(ok=True, index=item.index, name=item.name, args=values)

    def set_tunable_value(self, index_or_name, value) -> SetTunableResult:
        """写一个调参项（脚本用）：类型从目录查、值按 8B 定长组帧、脚本优先级入队。

        index_or_name: 调参项 index(int) 或中文名(str)
        value        : 要写入的值，最终以下位机回显(TunableEcho 帧)为准覆盖显示值
        返回: SetTunableResult(ok, index, value)
        抛错: abort 中抛 RuntimeError；名字不存在抛 KeyError；值类型不符抛 ValueError
        """
        self.check_abort()
        idx = self.resolve_tunable(index_or_name)
        item = self.tunable_dir[idx].item
        # 与 hub.cmd 同一把尺子按目录声明类型校验：数值项只收 int/float、
        # Color 项只收 "#RRGGBB" 字符串。字符串/None/列表一律 400，
        # 不再静默编 0——静默 0 会把笔误伪装成"生效且值为 0"
        _check_field_type(f"调参项[{item.name}]", item.type, value)
        self.enqueue(set_tunable(idx, item.type, value), PRI_SCRIPT)
        return SetTunableResult(ok=True, index=idx, value=value)

    # ---------- 记录会话 ----------
    # 一段 = 一次 start_log → end_log。脚本在段内发命令、段末取整段数据；
    # 后端不切窗口，样本时间戳是"相对本段零点（start_log）的毫秒"，零点取的是下位机时刻：
    # 用采样时刻而非 PC 到达时刻，扫参/微分才不受 USB 批量到达与本机调度抖动影响。
    # 相对时间自足：脚本关心的是"发完命令后这 0.4 秒怎么样"，也免了跟系统时钟、NTP 打交道。

    def _expand_stamp(self, raw: int) -> int:
        """把下位机 uint16 时间戳展开成连续毫秒（与前端 expandStamp 同构）。

        相邻帧取无符号差分累加：差分小于半圈算前进，否则算回退一整圈。
        只要相邻两帧间隔 < 32.7s 就成立（掉线收车的几百毫秒远够）。
        """
        if self._stamp_raw >= 0:
            diff = (raw - self._stamp_raw) & 0xFFFF
            self._stamp_ms += diff if diff < 0x8000 else diff - 0x10000
        self._stamp_raw = raw
        return self._stamp_ms

    def _record(self, idx: int, value: Any, stamp_ms: int) -> None:
        """记一条样本；超上限丢新样（防脚本忘 end_log 把内存吃光）。

        丢新不丢旧：保留的是从 start_log 开始的完整一段，
        对"扫参看趋势"更有用；丢尾部只是少记后面一点。
        """
        with self._log_lock:
            # logging / _log_stamp0 都必须在锁内读：start_log 会在锁内重置零点并清空缓冲，
            # 若在锁外算 t，就可能拿旧零点算出巨大时间戳落进新一段——
            # 那条样本会排到列表最前，破坏"时间升序"。
            if not self.logging:
                return
            # 时间轴零点：start_log 那一刻的下位机时刻。样本里的 t 都是相对它的毫秒数
            t = stamp_ms - self._log_stamp0
            if self.log_samples() >= MAX_LOG_SAMPLES:
                self.log_dropped += 1
                return
            self.log_dir.setdefault(idx, []).append((t, value))

    def log_samples(self) -> int:
        """样本总数。⚠️ 内部无锁遍历 dict，调用方必须持有 _log_lock
        （_record 就是持锁调它的——Lock 不可重入，别把锁加进本方法内部）。
        """
        return sum(len(v) for v in self.log_dir.values())

    def start_log(self) -> StartLogResult:
        """开始记录：清掉上一段缓冲，并以此刻为本段零点。

        记哪些项由脚本先用 sub(x, 'log'/'both') 登记；
        记录期间新登记/退订即时生效（不用重启记录）。
        """
        with self._log_lock:
            # logging 也要在锁内打开：rx 线程按它判断样本归哪一段，
            # "重置零点 + 开始收样本"必须是同一个原子步骤
            self.log_dir.clear()
            self.log_dropped = 0
            self._log_stamp0 = self._stamp_ms  # 零点取当前下位机时刻，样本 t 都相对它
            self.logging = True
        return StartLogResult(
            ok=True,
            registered=[RegisteredItem(i, self._mon_name(i)) for i in sorted(self.log_reg)],
        )

    def end_log(self) -> EndLogResult:
        """结束记录：锁内一并关掉收样本并数样本，返回本段总数与丢弃数。"""
        # 两件事一起在锁内做完：关掉收样本、数样本。
        # rx 线程可能正卡在"过了 logging 检查、等锁落盘"那一步，锁住数才不会边遍历边改字典
        with self._log_lock:
            self.logging = False
            return EndLogResult(ok=True, samples=self.log_samples(), dropped=self.log_dropped)

    def _log_indices(self, names) -> list[int]:
        """把查询参数解析成一组 index：names=None 取全部已记录项，否则逐项 resolve。

        names: None（全部）/ 单个 name 或 index / name、index 混合的列表
        返回: index 列表（names=None 时按 index 升序）
        """
        if names is None:
            with self._log_lock:  # 防遍历 keys 时 rx 线程 setdefault 出新 key
                return sorted(self.log_dir)
        if isinstance(names, (str, int)):
            names = [names]
        return [self.resolve_monitor(n) for n in names]

    def _samples(self, idx: int) -> list[tuple[int, Any]]:
        """取某项整段样本的副本 [(t_ms, 值), ...]（按时间升序）。

        在锁内复制一份指针列表再放锁：既避免遍历时 rx 线程 setdefault 出新 key，
        也不把锁持有到统计/导出走完。
        """
        with self._log_lock:
            return list(self.log_dir.get(idx) or ())

    def log_rows(self, names=None) -> LogRowsResult:
        """取整段原始样本（长表，按时间排序），供脚本自己画图、落盘或自行统计。"""
        idxs = self._log_indices(names)
        rows: list[LogRow] = []
        for idx in idxs:
            name = self._mon_name(idx)
            for t, v in self._samples(idx):
                rows.append(LogRow(t=t, index=idx, name=name, value=v))
        rows.sort(key=lambda r: r.t)
        return LogRowsResult(rows=rows)

    # 时间对齐容差 ms。登记的项都会被提升到高速推送(10ms)，同一拍发出的项时刻一致；
    # 差 2ms 以上说明不是同一拍，不配对——所以这里只做"同拍精确配对"，不做插值/最近邻。
    ALIGN_TOL_MS = 2.0

    def calc(self, expr: str, agg: str = "", window=None) -> CalcResult:
        """对本段记录做表达式计算：输入项名做逐点算术，再按需聚合。

        expr：只含监控项名（中文）+ 算术运算符，如 "左0电流 - 左1电流"、
              "(左0电流 + 左1电流 + 左2电流) / 3"
        agg ：mean / sum / rms / std / max / min / abs_mean / count / median；
              空串则返回逐点序列 [[t_ms, value], ...]
        window：可选 [t0_ms, t1_ms]，只算这段时间内的点

        项的取值来源：有历史序列的（已登记 + 本段有样本）逐点参与运算；
        没有历史的（未登记的低速项）只有当前值，全程用这一个值，并在 missing 里标出。

        解析走 ast 白名单，不做 eval 任意代码。
        """
        if window is not None:
            if len(window) != 2:
                raise ValueError("window 必须是 [t0_ms, t1_ms] 两个元素")
            if window[0] > window[1]:
                raise ValueError("window 区间反了：[%s, %s]" % (window[0], window[1]))

        import ast
        import math
        import operator as op

        ops = {
            ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
            ast.Div: op.truediv, ast.Pow: op.pow,
            ast.USub: op.neg, ast.UAdd: op.pos,
        }
        aggs = {"mean", "sum", "rms", "std", "max", "min", "abs_mean", "count", "median"}

        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            raise ValueError("表达式解析失败：%s"
                             "（以数字开头或含空格的监控项名写不进表达式）" % e)

        # 白名单校验：只允许 名称/数值常量/ops 表里的算术运算
        # 注意 ast.walk 连运算符（Add/Sub...）和上下文（Load）节点都会吐出来，
        # 这两类必须放行，否则任何带运算符的表达式都过不了校验。
        # 运算符只放行 ops 里真正实现的那几个：放行 ast.operator 基类会让 % // & 等
        # 混过校验，到 eval_node 才 KeyError —— 而这个 KeyError 会被路由当成"项不存在"
        # 回 404，语义全错。这里挡掉才是 400。
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Constant):
                # 常量只收数值：字符串/None/布尔放过去要到 eval_node 才炸，错误离现场太远
                if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                    raise ValueError("表达式里只允许数值常量，实际给了 %r" % (node.value,))
            elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Expression,
                                   ast.expr_context, *ops)):
                continue
            else:
                raise ValueError("表达式只允许监控项名、数字和 + - * / ** 运算")
        if not names:
            raise ValueError("表达式里没有监控项")
        # agg 在这里就验：放到配点之后，一旦窗口内没点就会先报"没有可配对的有效点"，
        # 把 agg 拼错这件事盖掉（用户看到的是个不相干的错误）。
        if agg and agg not in aggs:
            raise ValueError("不支持的聚合：%s（可用 %s）" % (agg, "/".join(sorted(aggs))))
        for n in names:
            self.resolve_monitor(n)   # 不存在会抛 KeyError

        def as_real(v):
            """运算结果收口：复数/inf/nan 就地转 400，别留到 JSON 序列化时才炸。

            复数来自负数的分数次幂（(-8) ** 0.5），inf/nan 来自过大的幂。
            除零不在这里挡——那是数据问题（某一拍分母为 0），跳点比整体报错合适。
            """
            if isinstance(v, complex) or not math.isfinite(v):
                raise ValueError("运算结果不是有限实数（检查负数的分数次幂与过大的指数）")
            return v

        def eval_node(node, env):
            if isinstance(node, ast.Constant):
                return float(node.value)
            if isinstance(node, ast.Name):
                return env[node.id]
            if isinstance(node, ast.UnaryOp):
                return as_real(ops[type(node.op)](eval_node(node.operand, env)))
            if isinstance(node, ast.BinOp):
                try:
                    v = ops[type(node.op)](eval_node(node.left, env),
                                           eval_node(node.right, env))
                except OverflowError:
                    raise ValueError("运算结果溢出（幂运算底数或指数太大）")
                return as_real(v)
            raise ValueError("不支持的语法节点")

        # 取各项：有历史的用序列，没有的用当前值
        seq, single, missing = {}, {}, {}
        t_index: dict[int, dict[str, float]] = {}
        for n in set(names):
            idx = self.resolve_monitor(n)
            pts = self._samples(idx)
            if pts:
                seq[n] = pts
            else:
                s = self.monitor_latest.get(idx)
                if s is None:
                    missing[n] = "没有任何取值"
                else:
                    single[n] = float(s.value)
                    missing[n] = "无历史序列，全程用当前值"
        if missing and all(v.startswith("没有任何") for v in missing.values()):
            # 项在目录里、但一个值都没收到过 → 400"还没取值"，不是 404"不存在"。
            # 别抛 KeyError：路由把 KeyError 一律当 404，会让"没数据"伪装成"名字错了"。
            raise ValueError("监控项还没有任何取值：" + "、".join(missing))

        if not seq:
            # 全是单点项：算一次即可
            env = {n: single[n] for n in set(names) if n in single}
            # 混进一个"完全没值"的项时 env 会缺它，别让 eval_node 漏出裸 KeyError
            if not all(n in env for n in names):
                raise ValueError("监控项还没有任何取值："
                                 + "、".join(n for n in names if n not in env))
            try:
                v = eval_node(tree.body, env)
            except ZeroDivisionError:
                raise ValueError("除零")
            return CalcResult(ok=True, expr=expr, agg=agg or "none", value=v,
                              n=1, series=[], missing=missing)

        # 以第一项的时刻为基准，其余项按 ±ALIGN_TOL_MS 同拍配对
        base_name = sorted(seq)[0]
        for t, v in seq[base_name]:
            if window and not (window[0] <= t <= window[1]):
                continue
            t_index[int(round(t))] = {base_name: v}
        for n in sorted(seq):
            if n == base_name:
                continue
            for t, v in seq[n]:
                k = int(round(t))
                for cand in (k - 2, k - 1, k, k + 1, k + 2):
                    if cand in t_index and abs(t - cand) <= self.ALIGN_TOL_MS:
                        t_index[cand][n] = v
                        break

        out = []
        for t in sorted(t_index):
            env = dict(t_index[t])
            env.update(single)
            if not all(n in env for n in names):
                continue    # 本拍缺项就跳过
            try:
                out.append((t, eval_node(tree.body, env)))
            except ZeroDivisionError:
                continue
        if not out:
            raise ValueError("没有可配对的有效点（窗口内无样本 / 各项未落在同一拍 /"
                             " 某项完全没有取值）")

        vals = [v for _, v in out]
        if not agg:
            return CalcResult(ok=True, expr=expr, agg="none", value=None, n=len(vals),
                              series=out, missing=missing)
        n = len(vals)
        mean = sum(vals) / n
        table = {
            "mean": mean,
            "sum": sum(vals),
            "rms": math.sqrt(sum(v * v for v in vals) / n),
            "std": math.sqrt(sum((v - mean) ** 2 for v in vals) / n) if n > 1 else 0.0,
            "max": max(vals),
            "min": min(vals),
            "abs_mean": sum(abs(v) for v in vals) / n,
            "count": float(n),
            "median": sorted(vals)[n // 2] if n % 2 else
                      (sorted(vals)[n // 2 - 1] + sorted(vals)[n // 2]) / 2.0,
        }
        return CalcResult(ok=True, expr=expr, agg=agg, value=table[agg], n=n,
                          series=[], missing=missing)

    def log_stats(self, names=None, thresholds=()) -> LogStatsResult:
        """整段统计（后端算好，脚本不用自己攒样本）。

        thresholds 里每个 th 额外给出 ratio_ge[th] = |值| >= th 的样本占比。
        这个指标专门为标定设计：比如"超速时间占比 30%"比"平均超了多少"
        更能说明增益给大了。取绝对值是因为正负超调都算问题。
        """
        idxs = self._log_indices(names)
        items: list[LogStatsItem] = []
        for idx in idxs:
            name = self._mon_name(idx)
            samples = self._samples(idx)
            vals = [v for _, v in samples]
            # rate_hz 用样本自身跨度算（首个到末个时间戳），不依赖任何窗口
            span_ms = samples[-1][0] - samples[0][0] if len(samples) >= 2 else 0.0
            # 统计量摊平成 {index,name,n,mean,...}：/api/log/stats 的 JSON 契约不能变
            items.append(LogStatsItem(index=idx, name=name, **asdict(_aggregate(vals, thresholds, span_ms))))
        return LogStatsResult(items=items)

    def print_log(self, path: str | None = None, index_or_name=None) -> PrintLogResult:
        """导出记录，格式由 path 的后缀决定（只写一种，不再一次落两份）：

          path 后缀   导出内容
          .json       全量 JSON（无后缀时的默认）
          .csv        宽表 CSV：一列一项，行首 t_ms（见 _write_csv）
          .png/.jpg   按监控项作图，每项一条曲线（x=相对本段零点秒）
          .pdf/.svg   同上，矢量输出（pdf 内嵌字体，中文最稳；svg 依赖查看端字体）
          其它后缀    当 JSON 处理

        index_or_name：只导出这几项（中文名或 index，可混）；None = 导出全部登记项。
        记录缓冲在 end_log 之后仍在（下次 start_log 才清），所以可以换组合反复导出，
        每次给不同 path 即可。

        path 里带子目录（如 "probe_data/l0/start.csv"）会自动创建；
        不传 path 则默认落 pytest/probe_data/log_<时间戳>.json。
        """
        if path:
            target = Path(path)
            if not target.suffix:  # 没标后缀 → 按默认 json
                target = target.with_suffix(".json")
            if not target.is_absolute():
                # 相对路径一律归到 probe_data，不随后端进程的工作目录漂移
                target = self.pytest_dir / "probe_data" / target
        else:
            target = self.pytest_dir / "probe_data" / f"log_{datetime.now():%Y%m%d_%H%M%S}.json"
        target.parent.mkdir(parents=True, exist_ok=True)

        rows = self.log_rows(index_or_name).rows
        suffix = target.suffix.lower()
        columns: list[str] = []
        missing: dict[str, int] = {}
        if suffix == ".csv":
            columns, missing = self._write_csv(target, rows)
        elif suffix in (".png", ".jpg", ".jpeg", ".pdf", ".svg"):
            self._write_plot(target, rows)
        else:
            self._write_json(target, rows)

        return PrintLogResult(
            ok=True,
            path=str(target),
            format=suffix.lstrip("."),
            samples=len(rows),
            dropped=self.log_dropped,
            columns=columns,
            missing=missing,
        )

    def _write_json(self, target: Path, rows: list[LogRow]) -> None:
        """全量 JSON：元信息 + 原始样本长表。"""
        meta = LogFileMeta(
            created=datetime.now().isoformat(timespec="seconds"),
            samples=len(rows),
            dropped=self.log_dropped,
            registered=[RegisteredItem(i, self._mon_name(i)) for i in sorted(self.log_reg)],
            data=rows,
        )
        with open(target, "w", encoding="utf-8") as f:
            json.dump(asdict(meta), f, ensure_ascii=False, indent=2, default=str)

    # 宽表分桶宽度 ms：登记记录会把该项提升到高速推送（100Hz），按这个周期对齐
    LOG_BUCKET_MS = 10

    def _write_csv(self, target: Path, rows: list[LogRow]) -> tuple[list[str], dict[str, int]]:
        """宽表 CSV：一列一项，行首 t_ms = 该行各非空样本时刻的均值。

        为什么宽表而不是长表：导出给 AI/脚本按列算（比值、相关性、差分），长表每行
        重复中文名、行数与 token 都膨胀 N 倍，还得先 pivot 才能算。

        对齐：log 登记会把该项提升到高速推送，故按 LOG_BUCKET_MS 分桶，桶内每项取
        最后一个样本。桶只由"到达了样本"的 10ms 窗口产生，没有样本的时段不出行；
        桶内某列缺样本就留空单元格并计入 missing——空单元格的位置就是丢包位置
        （pandas 一样读成 NaN，但省 token）。

        返回 (列名, {列名: 空单元格数})。utf-8-sig 带 BOM，Excel 打开中文不乱码。
        """
        buckets: dict[int, dict[str, Any]] = {}
        t_of: dict[int, dict[str, float]] = {}
        idx_of: dict[str, int] = {}
        for row in rows:
            b = int(row.t // self.LOG_BUCKET_MS)
            buckets.setdefault(b, {})[row.name] = row.value   # 桶内取最后
            t_of.setdefault(b, {})[row.name] = row.t
            idx_of.setdefault(row.name, row.index)
        cols = sorted(idx_of, key=lambda n: idx_of[n])        # 列序按 index，多次导出一致

        missing = {c: 0 for c in cols}
        with open(target, "w", encoding="utf-8-sig", newline="") as f:
            f.write("t_ms," + ",".join(cols) + "\n")
            for b in sorted(buckets):
                slot = buckets[b]
                if not slot:
                    continue
                t_mean = sum(t_of[b][c] for c in slot) / len(slot)
                cells = [f"{t_mean:.1f}"]
                for c in cols:
                    v = slot.get(c)
                    if v is None:
                        cells.append("")
                        missing[c] += 1
                    elif isinstance(v, float):
                        cells.append(f"{v:.6g}")
                    else:
                        cells.append(str(v))
                f.write(",".join(cells) + "\n")
        return cols, missing

    def _write_plot(self, target: Path, rows: list[LogRow]) -> None:
        """按监控项作图并导出图片，横轴为相对本段零点的秒。

        一个单位一个子图、各自独立 y 轴：电压(0~12V)和电流(0~0.5A)共用一个 y 轴时，
        电流会被压成贴底的直线，看不出任何起伏。子图上下排列、共享 x 轴，便于对齐看相位。

        matplotlib 延迟导入——不画图的后端无需安装它。
        """
        import matplotlib

        matplotlib.use("Agg")  # 后端无 GUI，必须用非交互后端
        import matplotlib.pyplot as plt

        # 监控项多是中文名，Windows 常见中文字体兜底，避免图例变成方块
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        # 按单位分桶（保序：目录里先出现的单位在上）
        by_unit: dict[str, dict[str, list[tuple[float, float]]]] = {}
        order: list[str] = []
        for r in rows:
            u = self._mon_unit(r.index) or "值"
            if u not in by_unit:
                by_unit[u] = {}
                order.append(u)
            by_unit[u].setdefault(r.name, []).append((r.t / 1000.0, r.value))

        n = max(1, len(order))
        fig, axes = plt.subplots(n, 1, figsize=(12, 3.0 * n), sharex=True, squeeze=False)
        for i, u in enumerate(order):
            ax = axes[i][0]
            for name, pts in by_unit[u].items():
                ax.plot([p[0] for p in pts], [p[1] for p in pts], linewidth=1, label=name)
            ax.set_ylabel(u)
            ax.grid(True, alpha=0.3)
            ax.legend(loc="upper right", fontsize=7, ncol=3)
        axes[-1][0].set_xlabel("t / s（相对本段零点）")
        fig.tight_layout()
        fig.savefig(target, dpi=120)
        plt.close(fig)

    def _mon_unit(self, idx: int) -> str:
        """监控项单位（目录里带的，如 V / A / pct）；查不到返回空串。"""
        entry = self.monitor_dir.get(idx)
        return entry.item.unit if entry else ""

    def snapshot(self) -> Snapshot:
        """/api/status 的返回体：一眼看清 Backend 全貌，调试脚本连不上的第一站。

        logging / log_samples / log_dropped 三个一起看能判断记录是否健康：
        dropped > 0 说明撞了 50 万上限，这次记录不完整。
        """
        with self._log_lock:  # 记录中被轮询时 rx 在 append，数样本防字典变尺寸
            log_samples = self.log_samples()
        return Snapshot(
            status=self.status.value,
            message=self.status_msg,
            connected=self.bridge.connected,
            monitor_count=len(self.monitor_dir),
            tunable_count=len(self.tunable_dir),
            cmd_count=len(self.cmd_dir),
            log_reg=sorted(self.log_reg),
            logging=self.logging,
            log_samples=log_samples,
            log_dropped=self.log_dropped,
            abort=self.abort_event.is_set(),
            stop_cmd=self._stop_cmd_index,
            ports=SerialBridge.list_ports(),
        )
