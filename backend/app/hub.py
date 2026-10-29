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
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from fastapi import WebSocket

from .protocol import (
    CMD_ACTION_MIN,
    CMD_TAG_RANGE,
    CmdGet,
    CmdPost,
    CmdSpec,
    ValueType,
    build_frame,
    cmd_spec,
    command_send,
    decode_response,
    decode_value,
    is_ack_tracked,
    ping,
    request_command,
    request_monitor,
    request_tunable,
    REQ_LIST_MAX,
    resp_is_ok,
    resp_is_retry,
    resp_reason_text,
    set_tunable,
    subscribe,
    tag_fast,
    tag_subscribed,
    value_size,
)
from .dataclass import (
    AlertLevel,
    AlertMessage,
    CalcResult,
    ChartSuggestItem,
    ChartSuggestMessage,
    ChartSuggestResult,
    CmdDirItem,
    CmdField,
    CmdResult,
    DirEntry,
    EndLogResult,
    LeaseResult,
    LogFileMeta,
    LogSeries,
    MenuItem,
    MonitorReading,
    MonitorSample,
    OkResult,
    PendingAck,
    PrintLogResult,
    PushState,
    RegisteredItem,
    SetTunableResult,
    Snapshot,
    StartLogResult,
    StatusMessage,
    StopResult,
    SubResult,
    WsOp,
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

# ---------- Response 确认（下行帧 → 固件回执）----------
# 固件对需要确认的下行帧回 CmdPost.Response：OK / 需补发 / 明确拒绝。
# 上位机按 cmd 键维护待确认表：响应到达即结算，超时则原帧重发。
# 超时口径：响应延迟 ≤1 拍（10ms，rx 入队→下一 tx_tick 发出），动作命令/订阅/
# 调参取 100ms（10 倍余量）。
# 目录请求为什么给到 1s——两件事要分清，原注释把它们混成了一句：
#   ① 发目录分批是**被迫**的：固件 handle_command 里不直接发（一次性几百帧会阻塞
#      串口 read），只置"还剩几批"的倒计时，由 tx_tick 每拍发 DIR_BATCH=8 项
#      （命令目录 CMD_DIR_BATCH=4）。目录数据本来就在固件数组里，没有"去取"的延迟。
#   ② 回 OK 排在末批是**选择**：回 OK 就几个字节，收到时立刻入队完全不阻塞。固件
#      注释写的是"末批发完=目录传完"——**OK 的语义是"目录传完了"，不是"收到了"**。
#      上位机因此能拿 OK 当"目录齐了"的信号；若改成收到即回，OK 就退化成纯链路确认，
#      burst 半路断了（固件复位 / 串口卡死）上位机再也无从察觉。
# 余量的真实口径（原注释按"单个 burst"算，错了）：后端 :541-543 连续发三个目录请求，
#   而固件 tx_tick 是 if/else if **串行**处理（tunable→monitor→command），所以第三个
#   请求的 OK 要等三个 burst 之和。极限 255+255+32 项：32+32+8 = 72 拍 ≈ 720ms，
#   1s 只留约 1.4 倍余量，不是 3 倍。够用，但没原注释写的那么宽裕。
ACK_TIMEOUT_DIR_S = 1.0
ACK_TIMEOUT_CMD_S = 0.1
ACK_MAX_RETRIES = 3  # 超时/需补发的重发上限；用尽后放弃并记日志（防版本不匹配无限重发）
# 目录"对照→补发"外循环的轮次上限。每个补发帧自带 ACK_MAX_RETRIES 次重发，
# 但外循环每轮发的都是新帧、预算永远烧不完——不单独数轮次会滚雪球。
DIR_MAX_ROUNDS = 3

# ---------- 曲线配置建议（脚本 → 上位机）----------
# 上限是脚本侧的业务约定，不是前端容量（前端 4 通道 × 5 槽）：组数=通道数，
# 每组留 1 槽空余，好让操作者自己再补一条对比曲线。
CHART_SUGGEST_MAX_GROUPS = 4
CHART_SUGGEST_MAX_ITEMS = 4

# 内置下行命令的中文名（Response 回显/alert 提示用）。动作命令名不在表里——
# 查 cmd_dir（下位机命令目录发的中文名），见 Hub._cmd_label。
BUILTIN_CMD_NAMES = {
    CmdGet.RequestTunable: "请求调参目录",
    CmdGet.RequestMonitor: "请求监控目录",
    CmdGet.RequestCommand: "请求命令目录",
    CmdGet.Subscribe: "订阅监控项",
    CmdGet.SetTunable: "写调参项",
    CmdGet.Ping: "心跳",
}


def _ack_timeout_s(cmd: int) -> float:
    """目录请求等整个 burst，给 1s；其余（动作命令/订阅/调参）100ms。"""
    if cmd in (CmdGet.RequestTunable, CmdGet.RequestMonitor, CmdGet.RequestCommand):
        return ACK_TIMEOUT_DIR_S
    return ACK_TIMEOUT_CMD_S

# ---------- 日志缓冲上限 ----------
# 脚本忘了 end_log 会一直攒到内存吃满。超出后"丢新不丢旧"：
# 保留已记录的时间窗完整，事后按窗口统计仍有意义。
#
# 上限按"拍"（宽表的行）而不是按样本条数：条数会随登记项数漂移（记 20 项能撑 250s、
# 记 50 项只有 100s），语义说不清；帧数才是"最长记多久"。100Hz 下 5000 帧 = 50 秒。
# 内存不再是约束：宽表一格 8 B，50 列 × 5000 帧 = 2 MB（旧的分桶 list-of-tuple 是
# 120 B/样本，同样规模要 30 MB），给到 3 万帧也才 12 MB。
MAX_LOG_FRAMES = 5000

# 默认 pytest 输出目录（相对 backend/）
DEFAULT_PYTEST_DIR = Path(__file__).resolve().parent.parent / "pytest"

# ---------- 脚本写权租约 ----------
# 一次只允许一个脚本写。租约靠"持续有动作"续期，所以脚本崩了也不会把机器锁死——
# 超过这个时长没动静就视为放弃，别人可以接管。
# 取值权衡：太大 = 崩掉的脚本要等很久才能重跑；太小 = 长时间不给车发命令的
# 采数脚本会被莫名踢掉。60s 对"扫压标定一档要采样几秒"的节奏是安全的。
SCRIPT_LEASE_TTL_S = 60.0


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
        bridge_factory: Callable[..., SerialBridge] | None = None, # 建桥工厂：None=真串口
    ) -> None:
        """建 Hub：组装串口桥（回调指回本类），建目录缓存/队列/锁等全部内部状态。

        port      : 串口名；None=交给 SerialBridge 自动探测
        baud      : 波特率（CDC 虚拟口仅占位）
        pytest_dir: print_log 不传 path 时的默认落盘目录；None 用 backend/pytest
        bridge_factory: 建桥工厂；None=默认 SerialBridge（真串口）。演示模式
                        （V5_MOCK=1）由 main.py 注入 mock_v5.MockV5Bridge——
                        只换"字节怎么进出设备"，Hub/WS/脚本 API 其余一律不动。
        注意：这里只建对象不开串口，真正连接由 start()/ensure_connected() 触发。
        """
        factory = bridge_factory or SerialBridge
        self.bridge = factory(
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
        # value 推送只有在 logging 且 idx 在集合里时才进记录矩阵
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

        # 记录缓冲是一张宽表：行 = 一拍，列 = 监控项 index，格 = 该拍该项的值，
        # NaN = 这一拍没收到（空单元格的位置就是丢包位置）。
        #
        # 为什么是宽表而不是"按项分桶的 list"：
        #   · 写入是 O(1) 的标量赋值，不是每样本造一个 tuple+int+float（实测 120 B/样本）
        #   · 常驻内存降到 8 B/格，50 列 × 5000 帧 = 2 MB（同样规模旧结构 30 MB）
        #   · 行在写入时就按拍对齐好，导出/统计不用再打散重组
        # 行 = 拍的依据：下位机一帧带一个时间戳 + N 个监控项，同帧内时刻完全相同。
        self._log_mat: np.ndarray | None = None
        '''记录矩阵 (MAX_LOG_FRAMES, 列数) float64，NaN 表示该拍缺样本'''

        self._log_t: np.ndarray | None = None
        '''每行的时刻（毫秒，相对 _log_stamp0）；长度同 MAX_LOG_FRAMES'''

        self._log_row = 0
        '''下一个待写行号；数值上等于本段已写入的行数'''

        # 列 ↔ 监控项 index 的映射，start_log 那一刻按 log_reg 定死（本段内不再变）。
        # 为什么不直接用 index 当列号（直接寻址）：那样列宽得按"目录里最大的 index+1"
        # 预分配，只登记几个高 index 项时会白占上百列；而按登记集合紧凑排，
        # 列宽 = len(log_reg) 精确，也顺带让"记录期间新登记"自然落到集合外。
        # 关键是**本段内不 grow**：矩阵只在列数变化时重建，而重建只发生在 start_log
        # （HTTP 线程）里。绝不能在 _record（rx 线程，100Hz 每帧 10ms）里扩列——
        # 实测 5000 行扩一次 1.99~2.71 ms，头几帧新 index 集中到达会直接打穿帧预算。
        self._log_col_idx: list[int] = []
        '''本段列号 → 监控项 index（按 index 升序）'''

        self._log_col_of: dict[int, int] = {}
        '''本段监控项 index → 列号；不在表里 = 本段没登记它'''

        # >0 说明这轮记录不完整；end_log / print_log / snapshot 都会带上它。
        self.log_dropped = 0
        '''到 MAX_LOG_FRAMES 后 丢弃的帧数'''

        self._stamp_raw = -1
        '''下位机时间戳展开：上一帧的原始 uint16；-1=还没收到过（与前端 expandStamp 同构）'''

        self._stamp_ms = 0
        '''下位机时间戳展开：累加出的连续时刻（毫秒）'''

        self._log_stamp0 = 0
        '''start_log 那一刻的下位机时刻；样本里的 t_ms 都相对它'''

        # 守护整份记录缓冲：rx 线程 100Hz 在 _record 里写格子，
        # HTTP 线程同时在取宽表副本 / start_log 复位写指针——不锁就会边读边改。
        self._log_lock = threading.Lock()
        '''锁：护记录宽表（rx 100Hz 写格 vs HTTP 线程取整段 / start_log 复位）'''

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

        # ---------- Response 待确认表 ----------
        # cmd → PendingAck。_send_tracked 入队即登记，_on_frame 收到 Response 结算，
        # _sweep_acks（_tx_loop 里每轮扫）超时重发。断线期间不烧重试预算。
        # 写方有两个线程（rx 结算 / tx 扫描重发），照 _sub_lock 的先例加锁；
        # 临界区只碰表，不做任何 I/O（重发的入队在锁外）。
        self._ack_lock = threading.Lock()
        '''护 _pending_acks 的读写锁（rx 线程结算 vs tx 线程扫描重发）'''
        self._pending_acks: dict[int, PendingAck] = {}
        '''待确认下行帧 {命令字: PendingAck}，重连时清空'''
        self._dir_rounds: dict[int, int] = {}
        '''目录补发轮次 {命令字: 已补发轮数}；目录齐了/重连时清除（外循环上限见 _check_dir_complete）'''

        # 确认过程计数（诊断用：日志/后续 UI；目前只累加不对外出口）
        self.ack_ok = 0
        '''Response OK 结算次数'''
        self.ack_retry = 0
        '''因超时/需补发而重发的次数'''
        self.ack_reject = 0
        '''固件明确拒绝的次数（不重发）'''
        self.ack_timeout = 0
        '''重试预算用尽仍无确认，放弃的次数'''
        self.ack_unexpected = 0
        '''收到无法结算的 Response（无待确认帧/解析失败）的次数'''
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

        # ---------- 脚本会话互斥（一次只允许一个脚本写） ----------

        # 写权租约。为什么非有不可：abort 闩锁是全局的且没有所有者，
        # 两个脚本同时在跑时，"晚来的那个调 connect → clear_abort" 会把早来脚本
        # 刚刚吃到的停车解掉——它就在那儿以为自己被停了，实际又开始发命令了。
        # 所以第一步是把"第二个脚本"挡在门外，而不是去修 clear_abort。
        self._lease_lock = threading.Lock()
        '''护租约三件套的锁（HTTP 路由可能并发进来）'''
        self._lease_token: str | None = None
        '''当前持有写权的脚本 token；None=没人持有'''
        self._lease_at: float = 0.0
        '''上次"这个人还在"的时刻（monotonic）；超过 SCRIPT_LEASE_TTL_S 视为放弃，防脚本崩了锁死机器'''

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
                # 旧链路的在途帧永远等不到响应了：待确认表作废，重拉从头计时
                self._pending_acks.clear()
                self._dir_rounds.clear()
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
        self._send_tracked(request_frame, PRI_UI)  # 登记待确认：burst 末批的 OK 才算拉到

    # ---------- 目录完整性对照（OK 带总项数 → 缺项补发） ----------

    # 目录请求命令字 → 请求构建器（全量与补发同一个构建器，indices 区分）
    _DIR_REQUEST_BUILDERS = {
        CmdGet.RequestTunable: request_tunable,
        CmdGet.RequestMonitor: request_monitor,
        CmdGet.RequestCommand: request_command,
    }

    def _dir_table(self, cmd: int) -> tuple[dict[int, DirEntry], dict[str, int], int] | None:
        """目录请求命令字 → (目录表, 名字反查表, 下标基准)；不是目录请求返回 None。

        下标基准：tunable/monitor 的表下标就是寻键（0 起）；cmd_dir 按命令字寻键
        （0x80 起），而补发帧按表下标寻址（固件 req_idx 直接索引 cmd_items）——
        两者差一个 CMD_ACTION_MIN，对照和补发都要换算。
        """
        if cmd == CmdGet.RequestTunable:
            return self.tunable_dir, self._name_to_tun, 0
        if cmd == CmdGet.RequestMonitor:
            return self.monitor_dir, self._name_to_mon, 0
        if cmd == CmdGet.RequestCommand:
            return self.cmd_dir, self._name_to_cmd, CMD_ACTION_MIN
        return None

    def _check_dir_complete(self, cmd: int, info: bytes) -> None:
        """目录请求的 OK 带回 1B 表内总项数：和实收目录项对照，缺哪项补发哪项。

        在 _handle_response 锁外调用（补发要入队新帧）。三种结局：
          无缺失        → 清轮次计数，完事
          缺失 ≤16 项   → 部分补发（不清表，补齐帧按 index upsert 进现有目录）
          缺失更多       → 全量重拉（清表重启：一次丢这么多项，多半是链路问题）
          轮次超过上限   → 推 GiveUp alert 并放弃——每个补发帧自带 ACK_MAX_RETRIES
                           次重发，但"对照→补发→对照"的外循环每轮都是新帧、预算
                           永远烧不完，必须单独数轮次防滚雪球
        """
        pair = self._dir_table(cmd)
        if pair is None or not info:
            return
        table, names, base = pair
        count = info[0]
        # 期望键集 = 下标基准起连续 count 个（命令目录按命令字，其余按表下标）
        missing = set(range(base, base + count)) - table.keys()
        if not missing:
            self._dir_rounds.pop(cmd, None)
            return
        rounds = self._dir_rounds.get(cmd, 0) + 1
        label = self._cmd_label(cmd)
        if rounds > DIR_MAX_ROUNDS:
            self._dir_rounds.pop(cmd, None)
            logger.warning(
                "%s 补发 %d 轮仍缺 %d 项（如 %s），放弃自动补发，请手动刷新目录",
                label, DIR_MAX_ROUNDS, len(missing), sorted(missing)[:8],
            )
            self._push_alert(
                AlertLevel.GiveUp, cmd, label,
                f"目录补发 {DIR_MAX_ROUNDS} 轮仍缺 {len(missing)} 项，请手动刷新目录",
            )
            return
        self._dir_rounds[cmd] = rounds
        builder = self._DIR_REQUEST_BUILDERS[cmd]
        if len(missing) <= REQ_LIST_MAX:
            logger.info(
                "%s 应收 %d 项、实收 %d 项，补发缺失 %d 项（第 %d 轮）",
                label, count, len(table), len(missing), rounds,
            )
            # 补发帧按表下标寻址（固件 req_idx 直接索引表），命令目录要减掉基准
            self._send_tracked(builder(sorted(k - base for k in missing)), PRI_UI)
        else:
            logger.info(
                "%s 缺 %d 项超过补发上限 %d，改全量重拉（第 %d 轮）",
                label, len(missing), REQ_LIST_MAX, rounds,
            )
            self._request_dir(table, names, builder())  # 全量重拉：清表重启

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
            # 回显即 Set_Tunable 成功路径的确认，清掉待确认表里那帧
            # （失败路径固件补拒绝 Response，见下面的 Response 分支）
            self._pending_acks.pop(CmdGet.SetTunable, None)
        elif cmd == CmdPost.Response:
            # 通用响应帧：[payloadlen][原话cmd][原话payload][status][原因串]
            self._handle_response(p)
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

    def _send_tracked(self, frame: bytes, priority: int = PRI_UI) -> None:
        """入队并登记待确认：所有固件会回 Response 的下行帧走这里。

        与 enqueue 只差一件事：按 cmd 键在 _pending_acks 记一帧（原帧/优先级/
        deadline/重试预算）。Response 到达或超时都据此结算，重发发原帧。
        同一 cmd 连续下发只保留最后一帧——同命令字 payload 长度恒定（字段表
        固定），且设目标类命令终态一致，覆盖无害。
        """
        self.enqueue(frame, priority)
        if len(frame) >= 2 and is_ack_tracked(frame[1]):
            cmd = frame[1]
            timeout = _ack_timeout_s(cmd)
            with self._ack_lock:
                self._pending_acks[cmd] = PendingAck(
                    frame=frame,
                    priority=priority,
                    timeout=timeout,
                    deadline=time.monotonic() + timeout,
                    retries=ACK_MAX_RETRIES,
                )

    def _expected_echo_len(self, echo_cmd: int, entry: PendingAck | None) -> int | None:
        """Response 里回显 payload 的应有字节数。

        优先用待确认表里的原帧（len-3，最可靠）；未登记的命令字按内置命令定长
        或命令目录字段累推。同一命令字的 payload 长度恒定，是不变量。
        """
        if entry is not None:
            return len(entry.frame) - 3  # A5 + cmd + XOR
        builtin = {
            CmdGet.RequestTunable: 1,  # [len]：全量请求的回显就1B；补发帧的回显长度由待确认表里的原帧给出
            CmdGet.RequestMonitor: 1,
            CmdGet.RequestCommand: 1,
            CmdGet.Subscribe: 2,
            CmdGet.SetTunable: 9,
            CmdGet.Ping: 0,
        }
        if echo_cmd in builtin:
            return builtin[echo_cmd]
        dir_entry = self.cmd_dir.get(echo_cmd)
        if dir_entry is not None and isinstance(dir_entry.item, CmdDirItem):
            return sum(value_size(f.type) for f in dir_entry.item.fields)
        return None

    def _cmd_label(self, cmd: int, echo: bytes = b"") -> str:
        """回显命令字 → 带中文名的描述（alert/日志用）。

        动作命令名取命令目录（下位机发的中文名）；SetTunable/Subscribe 的回显
        payload 首字节是 index，顺势解析出具体项名——"写调参项「左轮kp」被拒绝"
        比"命令 0x03 被拒绝"有用得多。查不出一律退回 0xXX，不猜。
        """
        if cmd == CmdGet.SetTunable and echo:
            entry = self.tunable_dir.get(echo[0])
            if entry is not None:
                return f"写调参项「{entry.item.name}」"
        if cmd == CmdGet.Subscribe and echo:
            entry = self.monitor_dir.get(echo[0])
            if entry is not None:
                return f"订阅监控项「{entry.item.name}」"
        dir_entry = self.cmd_dir.get(cmd)
        if dir_entry is not None and isinstance(dir_entry.item, CmdDirItem):
            return f"动作命令「{dir_entry.item.name}」"
        return BUILTIN_CMD_NAMES.get(cmd, f"命令 0x{cmd:02X}")

    def _push_alert(self, level: AlertLevel, cmd: int, name: str, text: str) -> None:
        """推一条 WS alert 给所有前端（_broadcast_json 的 alert 专用壳）。

        与 logger 双写不重复：logger 给后台排查，alert 给操作者即时提示。
        没有客户端时 _broadcast_json 自己空转，这里不判断。
        """
        self._broadcast_json(
            AlertMessage(op=WsOp.Alert, level=level, cmd=cmd, name=name, text=text)
        )

    def _handle_response(self, p: bytes) -> None:
        """结算一帧 Response（_on_frame 的 Response 分支，rx 线程里执行）。

        三种结局：
          OK        → 清除待确认（命令送达且处理成功）
          需补发     → 固片说这帧没成（校验失败/命令字未注册）：立即原帧重发，
                      计入同一份重试预算——永久性原因（如版本不匹配）不会无限重发
          明确拒绝   → 不重发（重发无意义）：内定err号/原因串进日志。
                      拒绝类是给机器决策的（INDEX_RANGE 该去刷目录、BUSY 该等待）

        非 OK 且前端看不出来的结局，另推 WS alert（op="alert"）弹提示：
          giveup = 补发预算耗尽 / 超时放弃 / 响应帧无法结算——命令状态不明。
        拒绝与"正在补发"不在这里推：Response 帧本身随串口字节透传给前端，
        它自己解析后弹提示（见前端 store 的 response 分支），两端都推会双弹。
        目录请求的 OK 附带 1B 表内总项数：锁外做一次完整性对照，缺项自动补发
        （见 _check_dir_complete）。
        表操作持 _ack_lock；入队/广播/日志在锁外（enqueue 另持 _tx_cv，不嵌套）。
        """
        if len(p) < 2:
            self.ack_unexpected += 1
            self._push_alert(
                AlertLevel.GiveUp, 0, "响应帧", f"响应帧过短（{len(p)}B），无法结算"
            )
            return
        echo_cmd = p[1]
        # alert: (级别, 命令名, 说明)。None = 这帧前端自己看得见，不双弹
        alert: tuple[AlertLevel, str, str] | None = None
        # 目录 OK 的对照+补发：(命令字, info字节)。持锁内只登记，锁外才执行（要入队新帧）
        dir_check: tuple[int, bytes] | None = None
        with self._ack_lock:
            # 先取不 pop：坏响应（一致性校验失败）不弄丢待确认帧，留给超时重发自愈
            entry = self._pending_acks.get(echo_cmd)
            echo_len = self._expected_echo_len(echo_cmd, entry)
            if echo_len is None:
                self.ack_unexpected += 1
                log = f"Response 无法解析：回显命令字 0x{echo_cmd:02X} 无待确认帧且长度不可知"
                alert = (
                    AlertLevel.GiveUp,
                    self._cmd_label(echo_cmd),
                    log,
                )
                resend = None
            else:
                decoded = decode_response(p, echo_len)
                if decoded is None:
                    self.ack_unexpected += 1
                    log = "Response 一致性校验失败（payloadlen 与回显长度不符），丢弃"
                    alert = (
                        AlertLevel.GiveUp,
                        self._cmd_label(echo_cmd, p[2 : 2 + echo_len]),
                        log,
                    )
                    resend = None
                else:
                    _, echo, status, reason = decoded
                    label = self._cmd_label(echo_cmd, echo)
                    reason_text = resp_reason_text(status, reason)
                    if resp_is_ok(status):
                        self._pending_acks.pop(echo_cmd, None)
                        self.ack_ok += 1
                        log = None
                        resend = None
                        # 目录请求的 OK 带 1B 总项数：登记，锁外对照补发
                        if echo_cmd in self._DIR_REQUEST_BUILDERS and reason:
                            dir_check = (echo_cmd, reason)
                    elif not resp_is_retry(status):  # 明确拒绝
                        self._pending_acks.pop(echo_cmd, None)
                        self.ack_reject += 1
                        log = f"命令 0x{echo_cmd:02X} 被固件拒绝：{reason_text}"
                        resend = None
                    elif entry is None:  # 需补发但无帧可发
                        self.ack_unexpected += 1
                        log = f"命令 0x{echo_cmd:02X} 需补发但无待确认帧可发：{reason_text}"
                        alert = (
                            AlertLevel.GiveUp,
                            label,
                            f"需补发但无原帧可重发：{reason_text}",
                        )
                        resend = None
                    elif entry.retries <= 0:  # 预算耗尽
                        self._pending_acks.pop(echo_cmd, None)
                        self.ack_timeout += 1
                        log = f"命令 0x{echo_cmd:02X} 补发预算已用尽：{reason_text}"
                        alert = (
                            AlertLevel.GiveUp,
                            label,
                            f"补发 {ACK_MAX_RETRIES} 次仍失败：{reason_text}",
                        )
                        resend = None
                    else:  # 需补发：扣预算、顺延 deadline、原帧重发
                        entry.retries -= 1
                        entry.deadline = time.monotonic() + entry.timeout
                        self.ack_retry += 1
                        log = (
                            f"命令 0x{echo_cmd:02X} 需补发（{reason_text}），"
                            f"立即重发（剩余 {entry.retries} 次）"
                        )
                        resend = (entry.frame, entry.priority)
        if log is not None:
            if resend is None:
                logger.warning(log)  # 解析失败/拒绝/预算耗尽：都该被看见
            else:
                logger.info(log)  # 补发重试：正常流转，info 就够
        if resend is not None:
            self.enqueue(*resend)
        if dir_check is not None:
            self._check_dir_complete(*dir_check)
        if alert is not None:
            self._push_alert(alert[0], echo_cmd, alert[1], alert[2])

    def _sweep_acks(self) -> None:
        """扫待确认帧：超时未收到 Response 就原帧重发（_tx_loop 每轮调，空转时 0.1s 一拍）。

        断线期间不烧重试预算：掉线/重启时所有在途帧都得不到响应，无差别重发只会
        把预算耗在重连前；deadline 顺延，连上后重新计时（重连成功本就会清空
        待确认表并重拉目录，见 _reconnect_loop）。
        表操作持 _ack_lock；入队/日志在锁外。
        """
        with self._ack_lock:
            if not self._pending_acks:
                return
            now = time.monotonic()
            resends: list[tuple[bytes, int]] = []
            giveups: list[tuple[int, bytes]] = []  # (命令字, 原帧)：放弃的要推 alert
            logs: list[tuple[int, str]] = []
            if not self.bridge.connected:
                for e in self._pending_acks.values():
                    e.deadline = now + e.timeout
                return
            for cmd, e in list(self._pending_acks.items()):
                if now < e.deadline:
                    continue
                if e.retries <= 0:
                    del self._pending_acks[cmd]
                    self.ack_timeout += 1
                    logs.append((
                        logging.WARNING,
                        f"命令 0x{cmd:02X} 重试 {ACK_MAX_RETRIES} 次均无确认，"
                        f"放弃（帧 {e.frame.hex()}）",
                    ))
                    giveups.append((cmd, e.frame))
                    continue
                e.retries -= 1
                e.deadline = now + e.timeout
                self.ack_retry += 1
                logs.append((
                    logging.INFO,
                    f"命令 0x{cmd:02X} 超时无确认，重发（剩余 {e.retries} 次）",
                ))
                resends.append((e.frame, e.priority))
        for level, text in logs:
            logger.log(level, text)
        for frame, pri in resends:
            self.enqueue(frame, pri)
        for cmd, frame in giveups:
            # 超时放弃只有后端知道——前端在等一帧永远不会到的回执，不推 alert
            # 的话操作者看到的就是"点了没反应"且没有任何提示
            self._push_alert(
                AlertLevel.GiveUp,
                cmd,
                self._cmd_label(cmd, frame[3:-1]),
                f"补发 {ACK_MAX_RETRIES} 次均无固件确认，已放弃（帧 {frame.hex()}）",
            )

    def _tx_loop(self) -> None:
        """唯一的串口写线程。没事干时睡在 Condition 上，来帧就被唤醒。"""
        while self._running:
            self._sweep_acks()  # 每轮扫一次待确认超时（空转时 0.1s 一拍，来了帧也扫）
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
            asdict(StatusMessage(op=WsOp.Status, state=self.status.value, message=self.status_msg))
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
                    self._send_tracked(subscribe(idx, True, new_fast), PRI_UI)
                else:
                    self._send_tracked(subscribe(idx, False, False), PRI_UI)

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
            # list() 快照：本方法跑在事件循环线程，而 rx 线程正在目录 burst 里插 key，
            # 直接迭代/排序会撞上 "dictionary changed size during iteration"（R1）。
            frames = [e.frame for _, e in sorted(list(table.items()))]
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

        # 5) 其余透传（动作命令/调参写入等：登记待确认，固件回 Response 结算）
        self._send_tracked(data, PRI_UI)

    def _stop_index(self) -> int:
        """停车命令用的 index：有目录以目录认出的为准，没到则兜底 0x80。

        返回: 停车命令字（命令目录 tag SPEC=Stop 认出的 index，或目录未到时的硬编码 0x80）
        """
        # 目录未到时的兜底：本车停车固定是命令表第一项 0x80。
        # 有了目录后以目录为准（从 tag SPEC=Stop 认出来），硬编码只在早期生效。
        return self._stop_cmd_index if self._stop_cmd_index is not None else 0x80

    def _broadcast_json(self, msg: StatusMessage | AlertMessage) -> None:
        """给所有 WS 客户端推一条 JSON 文本消息（StatusMessage / AlertMessage）。

        与 _broadcast_bytes 的区别：字节通道走的是"下位机原样数据"，
        JSON 通道走的是"Backend 自己的状态与决策"（连接状态、回执异常），
        两者互不混淆；前端按 op 字段分流（status / alert）。
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
        # list() 快照：rx 线程目录 burst 插 key 时迭代会 RuntimeError（R1）
        for n, i in list(self._name_to_mon.items()):  # 容错：忽略大小写
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
        for n, i in list(self._name_to_tun.items()):   # list() 快照，同 resolve_monitor（R1）
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
        for n, i in list(self._name_to_cmd.items()):   # list() 快照，同上（R1）
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
        # 三处的 list() 快照同理：HTTP 线程读、rx 线程目录 burst 写（R1）
        if t in ("monitor", "mon"):
            return [e.item for e in list(self.monitor_dir.values())]
        if t in ("tunable", "tun"):
            return [e.item for e in list(self.tunable_dir.values())]
        if t in ("cmd", "command", "commands"):
            return [e.item for e in list(self.cmd_dir.values())]
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
                # 正在记录时新登记的项不进本段（本段列集合在 start_log 已定死，见那里）。
                # 不抛错——sub 的返回值没有承载这个信息的字段，抛错会打断本就正确的
                # 订阅动作；但静默不记太坑，所以在后端日志里留一条。
                if self.logging and idx not in self._log_col_of:
                    logger.warning("记录期间新登记 %s：不进本段（列集合在 start_log 已定死），"
                                   "要记它请重新 start_log", self._mon_name(idx))
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

    def suggest_chart(self, groups, note: str = "", source: str = "") -> ChartSuggestResult:
        """把脚本推荐的曲线配置广播给所有上位机（只中继，不留存、不判过期）。

        groups: [[index_or_name, ...], ...]，第 i 组 = 第 i 个通道的追踪项；空组=该通道清空
        note/source: 脚本自带的一句话说明与来源标识，原样透传给前端展示
        返回: ChartSuggestResult（回显解析后的 index，便于脚本自检有没有订错槽）
        抛错: ValueError 组数/项数超限或整份为空；KeyError 名字不在监控目录里

        为什么不留存最后一条：建议的有效期就是弹出来那几秒，页面刷新后补推一条
        几分钟前的建议只会误导。脚本要提示就在推的时候让页面开着。
        """
        if not isinstance(groups, (list, tuple)):
            raise ValueError(f"groups 必须是列表的列表，收到 {type(groups).__name__}")
        if len(groups) > CHART_SUGGEST_MAX_GROUPS:
            raise ValueError(f"最多 {CHART_SUGGEST_MAX_GROUPS} 组，收到 {len(groups)} 组")
        parsed: list[list[ChartSuggestItem]] = []
        for g in groups:
            if not isinstance(g, (list, tuple)):
                raise ValueError(f"每组必须是列表，收到 {type(g).__name__}")
            if len(g) > CHART_SUGGEST_MAX_ITEMS:
                raise ValueError(f"每组最多 {CHART_SUGGEST_MAX_ITEMS} 项，收到 {len(g)} 项")
            row = []
            for item in g:
                idx = self.resolve_monitor(item)
                entry = self.monitor_dir.get(idx)
                row.append(
                    ChartSuggestItem(
                        index=idx,
                        name=entry.item.name if entry else str(idx),
                        unit=entry.item.unit if entry else "",
                    )
                )
            parsed.append(row)
        if not any(parsed):
            raise ValueError("空建议（所有组都是空的）不推：那只会把上位机的曲线清光")

        sug_id = uuid.uuid4().hex[:8]
        self._broadcast_json(
            ChartSuggestMessage(
                op=WsOp.ChartSuggest,
                id=sug_id,
                groups=parsed,
                source=source,
                note=note,
                ts=time.time(),
            )
        )
        logger.info("推曲线建议 %s：%d 组（source=%s）", sug_id, len(parsed), source or "-")
        return ChartSuggestResult(id=sug_id, groups=[[it.index for it in row] for row in parsed])

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
        self._send_tracked(build_frame(idx, b""), PRI_STOP)  # 停车帧也要确认：收不到必须重发
        return StopResult(ok=True, cmd_index=idx)

    def clear_abort(self) -> None:
        """复位中止闩锁，让下一次写不再抛错——**只做这一件事**。

        不清发送队列、不重发也不撤回已发出的停车帧、不动电机状态、不改订阅。
        它是「允许继续」的解锁，**不是「取消刚才那次停车」**：停过的车不会因为
        调它就自己走起来，已经丢掉的脚本队列也回不来。

        为什么必须有人显式清：旗标是**全局的、没有所有者**，后端不知道上一个脚本
        是不是已经死了——Ctrl+C/异常退出时就那么闩着，下一个脚本第一条 cmd()
        直接吃 409。BackendLink.connect() 里已经自动清一次（见那里注释），
        用 with BackendLink(...) 的正常脚本不必自己调这个函数。
        """
        self.abort_event.clear()

    # ---------- 脚本写权租约（一次一个脚本） ----------

    def claim_script(self, token: str, steal: bool = False,
                     ttl_s: float = SCRIPT_LEASE_TTL_S) -> LeaseResult:
        """领/续写权租约。同一个 token 重复调 = 续租，不冲突。

        steal=True 会踢掉别人，并顺手置 abort——被踢的那个脚本下一次 cmd()/sleep()
        立刻抛错，而不是继续往串口里塞命令。拿到写权的人自己调 clear_abort 解锁。

        返回: LeaseResult；ok=False 时 held_age_s 是对方已持有的秒数
        """
        now = time.monotonic()
        with self._lease_lock:
            holder, age = self._lease_holder()
            if holder is None or holder == token:
                self._lease_token, self._lease_at = token, now
                return LeaseResult(ok=True, token=token, expires_in_s=ttl_s)
            if not steal:
                return LeaseResult(ok=False, token=token, expires_in_s=0.0,
                                   held_age_s=age)
            self._lease_token, self._lease_at = token, now
        # 出锁再置：abort 跟租约护的不是同一把锁，别在持锁时扩大临界区
        self.abort_event.set()
        return LeaseResult(ok=True, token=token, expires_in_s=ttl_s, evicted=True)

    def release_script(self, token: str) -> None:
        """还写权。不是自己的就什么都不做——够用了，这里不需要报错来提醒谁。"""
        with self._lease_lock:
            if self._lease_token == token:
                self._lease_token, self._lease_at = None, 0.0

    def require_script(self, token: str | None) -> None:
        """写路径准入：每次被保护的请求都要过这一关。

        三种结果：没人持有 → 顺手把写权给这个 token（第一个写的人就是持有者）；
        自己就是持有者 → 续租放行；别人持有 → 抛 PermissionError（路由层转 409）。
        """
        now = time.monotonic()
        with self._lease_lock:
            if self._lease_token is None:
                if token is None:
                    return          # 匿名 + 无人持有：放行（curl 手测不受影响）
                self._lease_token, self._lease_at = token, now
                return
            if self._lease_token == token:
                self._lease_at = now
                return
            age = now - self._lease_at
            if age > SCRIPT_LEASE_TTL_S:
                # 租约过期 = 对方十有八九崩了。接管的不是"抢"，是收尸
                self._lease_token, self._lease_at = token, now
                return
            raise PermissionError(
                "写权被另一个脚本持有（已 %.0fs，token=%s）。"
                "确认对方已退出后重跑，或用 steal 抢占" % (age, self._lease_token))

    def _lease_holder(self) -> tuple[str | None, float]:
        """当前持有者与已持有秒数；租约过期则当无人持有。调用方须持 _lease_lock。"""
        if self._lease_token is None:
            return None, 0.0
        age = time.monotonic() - self._lease_at
        if age > SCRIPT_LEASE_TTL_S:
            return None, 0.0
        return self._lease_token, age

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
        self._send_tracked(command_send(item.index, types, values), PRI_SCRIPT)
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
        self._send_tracked(set_tunable(idx, item.type, value), PRI_SCRIPT)
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

    # 同拍合并容差 ms。下位机一帧带一个时间戳 + N 个监控项，同帧内时刻完全相同；
    # 同一拍若被拆成多帧发，帧间时刻差也远小于 2ms——超过 2ms 就算下一拍。
    # 不插值、不最近邻：这是"同拍精确配对"，与 calc 过去的口径一致。
    ALIGN_TOL_MS = 2.0

    def _record(self, idx: int, value: Any, stamp_ms: int) -> None:
        """记一条样本到宽表；超过 MAX_LOG_FRAMES 丢新帧（防脚本忘 end_log 吃光内存）。

        丢新不丢旧：保留的是从 start_log 开始的完整一段，
        对"扫参看趋势"更有用；丢尾部只是少记后面一点。

        行定位：与本行时刻相差 ≤ ALIGN_TOL_MS 就并入本行（同一拍），否则新开一行。
        这样"行 = 拍"在写入时就定死了，导出和统计都不用再对齐。
        """
        col = self._log_col_of.get(idx)
        if col is None:
            # 本段没登记这一项（记录期间新登记的不进本段，见 start_log）。
            # 调用点已按 log_reg 过滤过，走到这里只会是"记录中新登记"这一种情况。
            return
        with self._log_lock:
            # logging / _log_stamp0 都必须在锁内读：start_log 会在锁内重置零点并复位写指针，
            # 若在锁外算 t，就可能拿旧零点算出巨大时间戳落进新一段。
            if not self.logging:
                return
            # 时间轴零点：start_log 那一刻的下位机时刻。样本里的 t 都是相对它的毫秒数
            t = stamp_ms - self._log_stamp0
            # 矩阵只装数：Color 解码成 "#rrggbb" 字符串，这里转回 0xRRGGBB 整数。
            # decode_value 本身不改——那个字符串是给前端直接显示的（三端契约）。
            if isinstance(value, str):
                value = int(value[1:], 16) if value[:1] == "#" else float("nan")
            row = self._log_row - 1
            if row < 0 or abs(t - self._log_t[row]) > self.ALIGN_TOL_MS:
                if self._log_row >= MAX_LOG_FRAMES:
                    self.log_dropped += 1
                    return
                row = self._log_row
                self._log_row += 1
                self._log_t[row] = t
            self._log_mat[row, col] = value

    def log_samples(self) -> int:
        """本段非空样本数（宽表里非 NaN 的格子数）。

        读的是 numpy 数组，不存在"遍历时 rx 线程插新 key"的问题，
        所以不再要求调用方持锁——拿到的瞬间值判健康度足够。
        """
        if self._log_row == 0:
            return 0
        return int(np.count_nonzero(~np.isnan(self._log_mat[: self._log_row])))

    def start_log(self) -> StartLogResult:
        """开始记录：按此刻的 log_reg 定死本段列集合，复位写指针，并以此刻为零点。

        **一段记哪些项，在 start_log 这一刻就定死**，之后 sub(x,'log') 新登记的项
        **不进本段**（只影响下一段）。换项要重新 start_log——现有脚本本来就是
        "sub 一次 → 循环里反复 start_log"，所以这条不增加任何负担。

        为什么不允许中途加列：加列要 np.concatenate 整块拷贝矩阵，而 _record 跑在
        rx 线程上、100Hz 每帧只有 10ms。实测 5000 行（= MAX_LOG_FRAMES）规模单次
        grow 1.99~2.71 ms，头几帧新 index 集中到达时一帧能占掉 13.5 ms（帧预算的
        135%）——直接打穿串口接收。把建表放到 start_log（HTTP 线程）里，热路径上
        就只剩一次 dict 查列号 + 一次标量赋值。
        """
        with self._log_lock:
            if not self.log_reg:
                # 空登记集合会建出 0 列的矩阵，整段什么都记不到且毫无提示——
                # 与其让脚本拿到一份空 csv 再猜，不如在这里就报错。
                raise ValueError("还没登记要记录的项：先 sub(项名, 'log'/'both') 再 start_log")
            # logging 也要在锁内打开：rx 线程按它判断样本归哪一段，
            # "定列 + 置零点 + 复位写指针 + 开始收样本"必须是同一个原子步骤
            self._log_col_idx = sorted(self.log_reg)
            self._log_col_of = {i: c for c, i in enumerate(self._log_col_idx)}
            self.log_dropped = 0
            self._log_stamp0 = self._stamp_ms  # 零点取当前下位机时刻，样本 t 都相对它
            self.logging = True
            # 缓冲复用：只在列数变了才重建。上一段的数据留在行号 ≥ _log_row 的位置，
            # 永远读不到，所以不用清——清一遍 5000×N 是纯浪费。重建走 HTTP 线程。
            width = len(self._log_col_idx)
            if self._log_mat is None or self._log_mat.shape[1] != width:
                self._log_mat = np.full((MAX_LOG_FRAMES, width), np.nan)
                self._log_t = np.zeros(MAX_LOG_FRAMES)
            self._log_row = 0
        return StartLogResult(
            ok=True,
            registered=[RegisteredItem(i, self._mon_name(i)) for i in self._log_col_idx],
        )

    def end_log(self) -> EndLogResult:
        """结束记录：锁内一并关掉收样本并数样本，返回本段总数与丢弃数。"""
        # 两件事一起在锁内做完：关掉收样本、数样本。
        # rx 线程可能正卡在"过了 logging 检查、等锁写格子"那一步，锁住数才不会边读边写
        with self._log_lock:
            self.logging = False
            return EndLogResult(ok=True, samples=self.log_samples(), dropped=self.log_dropped)

    def _log_indices(self, names) -> list[int]:
        """把查询参数解析成一组**列号**（不是 index）：列号 ↔ index 见 _log_col_idx。

        names: None（本段有数据的全部列）/ 单个 name 或 index / name、index 混合的列表
        本段没登记的项查不到列号，静默跳过——导出一份"没记的项"没有意义。
        """
        if names is None:
            with self._log_lock:
                return self._log_used_cols()
        if isinstance(names, (str, int)):
            names = [names]
        out = []
        for n in names:
            col = self._log_col_of.get(self.resolve_monitor(n))
            if col is not None:
                out.append(col)
        return out

    def _log_used_cols(self) -> list[int]:
        """本段至少有一个非空样本的**列号**（升序）。调用方须持 _log_lock。"""
        if self._log_row == 0:
            return []
        used = ~np.isnan(self._log_mat[: self._log_row]).all(axis=0)
        return [int(c) for c in np.nonzero(used)[0]]

    def log_frame(self, names=None):
        """取整段记录为 pandas 宽表：行 = 拍，列 = 监控项中文名，格 = 值，NaN = 该拍缺。

        这是记录数据的唯一取用形态。行在 _record 写入时就按拍对齐好了，
        所以 csv / 图 / calc 都直接吃它，不再有"打散成长表 → 再分桶重组"的中间层。
        实测（50 项 × 5000 帧 = 250k 样本）取这一步：旧 57.9 ms（物化 250k 个
        LogRow 再排序分桶）→ 新 2.2 ms（矩阵切片 + DataFrame）。注意这只是"取数"，
        落盘那 117 ms（to_csv 250k 个格子）新旧都要付，省不掉。

        names: None（本段有数据的全部项）/ 单个名字或 index / 混合列表。
        无 HTTP 路由——脚本取数据走 /api/log/print。
        """
        import pandas as pd  # 延迟导入：与 matplotlib 同例，不导出就没人付这 368 ms

        idxs = None if names is None else self._log_indices(names)  # 名字解析在锁外
        with self._log_lock:
            n = self._log_row
            t = self._log_t[:n].copy() if n else np.zeros(0)
            mat = self._log_mat[:n, :].copy() if n else np.zeros((0, 0))
            if idxs is None:
                idxs = self._log_used_cols()
            col_idx = list(self._log_col_idx)     # 本段列号 → index，锁内快照
        idxs = [c for c in idxs if c < len(col_idx)]
        cols = [self._mon_name(col_idx[c]) for c in idxs]
        if n == 0 or not cols:
            return pd.DataFrame(np.zeros((0, len(cols))), columns=cols,
                                index=pd.Index([], name="t_ms"))

        df = pd.DataFrame(mat[:, idxs], columns=cols)
        df["_t"] = t
        # 行合并兜底：_record 只跟"上一行"比，同一拍的样本若到达顺序被打乱
        # （时间戳回退，或同一拍被拆成多帧且中间插进了别的拍），就会开成两行。
        # 这里按 t 排序后把相邻且相差 ≤ALIGN_TOL_MS 的行并成一行（两行的非空格子
        # 互相填充），使"同一拍 → 同一行"这个不变量与到达顺序无关。
        # 正常的单调流没有相邻近时刻行，这个分支整段跳过，不付 groupby 的开销。
        near = df["_t"].diff() <= self.ALIGN_TOL_MS
        if near.any():
            df = df.sort_values("_t", kind="stable").reset_index(drop=True)
            g = df.groupby((df["_t"].diff() > self.ALIGN_TOL_MS).cumsum(), sort=False)
            df, t = g.mean().drop(columns="_t"), g["_t"].min().to_numpy()
        else:
            t = df["_t"].to_numpy()
            df = df.drop(columns="_t")
        return df.set_index(pd.Index(t, name="t_ms"))

    def calc(self, expr: str, agg: str = "", window=None) -> CalcResult:
        """对本段记录做表达式计算：输入项名做逐点算术，再聚合成一个标量。

        agg 必填，只支持 mean / sum。

        【 preferred path —— 写给未来的维护者（包括 AI）】
        复杂分析（派生量之外的滤波/分位/布尔过滤/ratio_ge 类指标/画图）一律
        print_log 落 csv 后 pandas 回读，不要给本函数加 agg、加语法、加数据出口：
        API 面是棘轮，加功能十行、删功能破裂契约，而每条"捷径"都会招募下一个补丁。
        本函数故意保持最小：无人也决策的一行断言（"这段均值 < 阈值吗"）用它，
        有人看数据的世界属于 csv + pandas。

        expr：只含监控项名（中文）+ 算术运算符，如 "左0电流 - 左1电流"、
              "(左0电流 + 左1电流 + 左2电流) / 3"
        agg ：mean / sum（必填；没有"逐点序列"选项——要原始点就 print_log 导 csv）
        window：可选 [t0_ms, t1_ms]，只算这段时间内的点（t 轴零点 = start_log 时刻）

        逐点对齐不用操心：记录宽表的行就是"拍"，同一拍的各项已经在同一行上
        （_record 按 ±ALIGN_TOL_MS 定行），所以这里直接在整列上做算术。
        某一拍缺项 → 该拍算出 NaN → 聚合时跳过，不整段报错。

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
            ast.Div: op.truediv,
            ast.USub: op.neg, ast.UAdd: op.pos,
        }
        # agg 只保留 mean/sum：其余统计量（rms/std/max/min/abs_mean/count/median）
        # 在 pandas 里全是一行，堆在这里就是给 API 棘轮上挂钩。逐点序列也不给——
        # 数据出口只有 print_log 的 csv/json。
        aggs = {"mean", "sum"}

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
                raise ValueError("表达式只允许监控项名、数字和 + - * / 运算")
        if not names:
            raise ValueError("表达式里没有监控项")
        # agg 在这里就验：放到配点之后，一旦窗口内没点就会先报"没有可配对的有效点"，
        # 把 agg 拼错这件事盖掉（用户看到的是个不相干的错误）。
        if agg not in aggs:
            raise ValueError("不支持的聚合：%r（只支持 mean/sum；"
                             "要逐点序列或更复杂的统计，print_log 导 csv 回读）" % (agg,))
        for n in names:
            self.resolve_monitor(n)   # 不存在会抛 KeyError

        def _is_seq(v) -> bool:
            """是否逐点序列（pandas Series / numpy 数组），而不是一个标量。"""
            return isinstance(v, np.ndarray) or hasattr(v, "to_numpy")

        def as_real(v):
            """标量运算结果收口：inf/nan 就地转 400，别留到 JSON 序列化时才炸。

            inf/nan 来自加减乘除溢出（1e300 * 1e300）——复数曾来自负数的分数次幂，
            随 ** 一起删了，这里不再需要挡。
            除零不在这里挡——那是数据问题（某一拍分母为 0），跳点比整体报错合适。
            Series 原样放过：它的逐点有限性由最后聚合后的 isfinite 统一收口，
            中间拍出现 inf（如分母为 0）应当跳点而不是整段报错。
            """
            if _is_seq(v):
                return v
            if not math.isfinite(v):
                raise ValueError("运算结果不是有限实数（检查过大的乘除与指数）")
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

        # 宽表的行已经是"拍"，各项天然对齐，不需要再配对——
        # 旧实现在这里按 ±ALIGN_TOL_MS 做双指针对齐，那是因为存储是分桶的、行要事后拼。
        df = self.log_frame()
        if window is not None:
            df = df[(df.index >= window[0]) & (df.index <= window[1])]

        # 取各项：本段有序列的用整列，没有的用当前值
        seq, single, missing = {}, {}, {}
        for n in set(names):
            if n in df.columns:
                seq[n] = df[n]
            else:
                s = self.monitor_latest.get(self.resolve_monitor(n))
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
            return CalcResult(ok=True, expr=expr, agg=agg, value=v,
                              n=1, missing=missing)

        # 逐点算：Series 之间按行对齐（index 相同），缺项的拍算出 NaN，聚合时自动跳过
        env = {}
        for n in set(names):
            env[n] = seq[n] if n in seq else single[n]
        res = eval_node(tree.body, env)

        if _is_seq(res):
            arr = np.asarray(res, dtype=float)
            # inf 是溢出/除零，是算错了，整段报错；NaN 是这一拍缺输入，跳过这一拍
            # （等价于旧实现"本拍缺项就跳过"）——两者不能混为一谈。
            if np.isinf(arr).any():
                raise ValueError("运算结果不是有限实数（检查过大的乘除与指数）")
            arr = arr[~np.isnan(arr)]
            if arr.size == 0:
                raise ValueError("没有可配对的有效点（窗口内无样本 / 各项未落在同一拍 /"
                                 " 某项完全没有取值）")
            n, total = int(arr.size), float(arr.sum())
        else:
            n, total = 1, float(res)
        v = total / n if agg == "mean" else total
        if not math.isfinite(v):
            raise ValueError("运算结果不是有限实数（检查过大的乘除与指数）")
        return CalcResult(ok=True, expr=expr, agg=agg, value=v, n=n,
                          missing=missing)

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

        df = self.log_frame(index_or_name)
        suffix = target.suffix.lower()
        columns: list[str] = []
        missing: dict[str, int] = {}
        if suffix == ".csv":
            columns, missing = self._write_csv(target, df)
        elif suffix in (".png", ".jpg", ".jpeg", ".pdf", ".svg"):
            self._write_plot(target, df)
        else:
            self._write_json(target, df)

        return PrintLogResult(
            ok=True,
            path=str(target),
            format=suffix.lstrip("."),
            samples=int(df.notna().to_numpy().sum()),
            dropped=self.log_dropped,
            columns=columns,
            missing=missing,
        )

    def _write_json(self, target: Path, df) -> None:
        """全量 JSON：元信息 + 时间轴 + 每项一条非空点列。

        长表（每行重复中文名）已废：同样的点，长表体积膨胀 N 倍且读回来还得 pivot。
        这里改成"一根共享时间轴 + 每列只存非空点"，体积约为长表的 40%。
        """
        t = df.index.to_numpy()
        series = []
        for i, name in enumerate(df.columns):
            v = df[name].to_numpy()
            keep = ~np.isnan(v)
            series.append(LogSeries(
                index=self._name_to_mon.get(name, -1),
                name=name,
                unit=self._mon_unit(self._name_to_mon.get(name, -1)),
                points=[[float(a), float(b)] for a, b in zip(t[keep], v[keep])],
            ))
        meta = LogFileMeta(
            created=datetime.now().isoformat(timespec="seconds"),
            samples=sum(len(s.points) for s in series),
            dropped=self.log_dropped,
            registered=[RegisteredItem(i, self._mon_name(i)) for i in sorted(self.log_reg)],
            t_ms=[float(x) for x in t],
            series=series,
        )
        with open(target, "w", encoding="utf-8") as f:
            json.dump(asdict(meta), f, ensure_ascii=False, indent=2, default=str)

    def _write_csv(self, target: Path, df) -> tuple[list[str], dict[str, int]]:
        """宽表 CSV：一列一项，行首 t_ms = 该拍的时刻。

        宽表而不是长表：导出给 AI/脚本按列算（比值、相关性、差分），长表每行重复
        中文名、行数与 token 都膨胀 N 倍，还得先 pivot 才能算。

        行就是"拍"，写入时已按 ±ALIGN_TOL_MS 对齐好，所以这里只是把 DataFrame 落盘：
        某列这一拍缺样本 → 空单元格，计入 missing——空单元格的位置就是丢包位置
        （pandas 一样读成 NaN，但省 token）。

        返回 (列名, {列名: 空单元格数})。utf-8-sig 带 BOM，Excel 打开中文不乱码。
        """
        missing = {c: int(n) for c, n in df.isna().sum().items()}
        # %.10g：10 位有效数字能无损表示 uint32 上限（4294967295 恰好 10 位），
        # 整数值又不会拖出 ".0" 尾巴；再短会丢计数类监控项的精度。
        df.to_csv(target, encoding="utf-8-sig", na_rep="", float_format="%.10g")
        return list(df.columns), missing

    def _write_plot(self, target: Path, df) -> None:
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

        # 按单位分组（保序：目录里先出现的单位在上）
        by_unit: dict[str, list[str]] = {}
        for name in df.columns:
            u = self._mon_unit(self._name_to_mon.get(name, -1)) or "值"
            by_unit.setdefault(u, []).append(name)

        n = max(1, len(by_unit))
        fig, axes = plt.subplots(n, 1, figsize=(12, 3.0 * n), sharex=True, squeeze=False)
        t = df.index.to_numpy() / 1000.0
        for i, (u, cols) in enumerate(by_unit.items()):
            ax = axes[i][0]
            for c in cols:
                # 缺样本的拍是 NaN，matplotlib 会断线而不是连成直线——正是要的效果
                ax.plot(t, df[c].to_numpy(), linewidth=1, label=c)
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
        dropped > 0 说明撞了 MAX_LOG_FRAMES（5000 帧），这次记录不完整。
        """
        # 锁只为让这三个数取自同一个瞬间（不再有"字典变尺寸"的问题——存储是矩阵）
        with self._log_lock:
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
            ports=self.bridge.list_ports(),
        )
