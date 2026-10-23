"""
数据类集中地：协议解析产物 + 接口返回体 + 串口设备描述符。

本文件只放被动数据结构（dataclass / NamedTuple）与紧绑它们的类型别名，
不含任何逻辑；protocol / serial_bridge 现在就从这里 import，hub / main 的接口
返回体也以这里为真源。
字段名即对外 JSON 键名（FastAPI 自动转），改动等于改接口契约。
"""

from dataclasses import dataclass, field
from typing import Any, NamedTuple

# 监控/调参项能取的值形态。注意这是"Python 侧解出来的类型"，
# 与 protocol.ValueType 不是一一对应（多个 ValueType 都解成 int）；
# str 只出现在 Color（解成 "#RRGGBB" 供前端直接显示）。
CellValue = int | float | bool | str


# ---------- 目录数据结构（dataclass；/api/column 直接返回，FastAPI 自动转 JSON）----------


@dataclass
class CmdField:
    """动作命令的一个参数。

    min/max 只在 tag 的 bit7(RANGE) 置位时才有值，且**按本字段的 type 解码、与字段同型**：
    整型字段解成 int、float/double 解成 float，不再一律当 float32。
    """
    type: int
    name: str
    min: float | int | None = None
    max: float | int | None = None


@dataclass
class CmdDirItem:
    """动作命令目录的一项。index 同时也是下发时的命令字。"""
    index: int
    tag: int
    name: str
    fields: list[CmdField] = field(default_factory=list)


@dataclass
class MenuItem:
    """监控/调参目录项，两张表结构相同共用。value 是目录帧自带的当前值。"""
    index: int
    type: int   # 值类型码（ValueType）：hub 解后续 value/echo 帧的唯一依据
    tag: int    # 位标志（MONITOR_TAG_*：订阅档位、GETTER 等）
    name: str
    unit: str
    value: Any  # tunable 项的 value 会被 echo 回显覆盖成真实生效值


@dataclass
class DirEntry:
    """一张目录表的一项（index 作键）：解析结果 + 完整原始帧。

    frame 是下位机发来的原帧（A5|Cmd|Payload|XOR，解帧时按 payload 重建，
    字节一致），前端请求目录时由后端直接回放——不必重编码，也不必再打扰
    下位机。类型只认 item.type 一份，别处不另存类型表。
    """
    item: MenuItem | CmdDirItem
    frame: bytes


@dataclass
class PendingAck:
    """一条待确认的下行帧（_pending_acks 的值，按命令字作键）。

    固件对需要确认的下行帧回 Response（CmdPost.Response）：OK 清除、
    需补发立即重发、明确拒绝只记录。超时未收到响应则由 _sweep_acks 原帧重发。
    同一命令字连续下发只保留最后一帧——同命令字 payload 长度恒定（字段表
    固定），且设目标类命令终态一致，覆盖无害（相对量命令的重复执行风险
    见协议注释，当前调试平台可接受）。
    """
    frame: bytes      # 待确认的完整原帧（重发就发它）
    priority: int     # 入队时的优先级（重发保持原优先级）
    timeout: float    # 单次确认超时（秒）：目录请求 1.0，其余 0.1
    deadline: float   # 本次超时时刻（time.monotonic 基准）
    retries: int      # 剩余重发次数（OK/拒绝结算或用尽后清零丢弃）


# ---------- 串口设备描述 ----------


@dataclass
class PortInfo:
    """一个串口设备的信息（pyserial 原始描述符的裁剪版，/api/ports 直接返回）。"""
    device: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str


# ---------- 最新样本快照 ----------
class MonitorSample(NamedTuple):
    """
    某监控项的最新一个样本：值 + 两个时刻，一对不可分的量放一起。
    value : 最新值
    t_recv: 收到该值的 time.monotonic() 秒（单调时钟，不受改时间影响）；
            None = 只有目录帧带来的初值、还没收到过推送。
            → age_s（距上次收到推送）：到达新近度，诊断量。
    t_fw  : 该值所在监控帧的下位机原始 16bit 时间戳（帧头那 2B，未展开）；
            None = 只有目录帧初值、还没收到过推送。
            → lag_ms（落后最新监控帧多少）：新鲜度主判据。
            存 raw 不存展开值：lag 只需要差值，mod 65536 相减对任意间隔都精确；
            _expand_stamp 的展开假设相邻帧 <32.7s，长掉线会解错（日志会话那条
            时间轴另用，两者互不依赖）。

    ⚠ age_s 的语义边界：它量的是"到达新近度"，不是"样本年龄"。恒定传输延迟下
    每个值都会看起来新鲜（age_s≈0）实则旧了一整个延迟——那种传输方式下它是
    主动误导而非不完整，故只配当诊断量。新鲜度主判 lag_ms：固件是全量推送，
    正常项最多 80ms（低速相位数×拍）必出现一次，落后前沿 >300ms 即旧；恒定
    传输延迟下全体同时延、相对差值不变，故免疫。USB 直连两者都可忽略；
    若换成桥接/WiFi/中转上位机，age_s 降级为纯诊断、lag_ms 顶上。
    链路整体冻结（掉线/固件卡死）时 lag 的前沿钉在最后帧不动，但 read 时按
    PC 时钟外推（前沿戳 + 距其到达时长，封顶 60s 防 mod 绕圈）——冻结期间
    lag 持续增长，300ms 后同样报旧；外推的 1:1 时钟假设每遇领先帧即重锚，
    只在帧间隔内有效。心跳 300ms 判掉线与之互为见证，不是替代关系。
    接收侧观测不到单向传输延迟——任何方案要么带不可验证的假设（NTP 对称性）
    要么只给无假设下界（样本锚点），评估见解析文档。
    """
    value: Any
    t_recv: float | None
    t_fw: int | None = None


# ---------- 接口返回体（dataclass；FastAPI 自动转 JSON，字段名即 JSON 键名）----------


@dataclass
class OkResult:
    """只有 ok 的通用成功返回。"""
    ok: bool = True


@dataclass
class MonitorReading:
    """监控项当前值 + 新鲜度（/api/value 一项）。"""
    index: int
    name: str
    value: Any
    unit: str
    age_s: float  # 距上次收到推送的秒数（到达新近度；恒定传输延迟下会骗人，诊断量）
    lag_ms: float | None  # 落后最新监控帧多少 ms（固件时间轴，wrap-aware 相减 + 冻结外推）；
    # None=无推送史（目录初值）。看门狗主判据：None 或 >FW_LAG_STALE_MS(300) 即旧。
    # 对传输延迟/PC 抖动免疫；链路冻结时前沿钉住但 read 外推（封顶 60s）照常报旧。


@dataclass
class ValueSnapshot:
    """监控项当前值快照（/api/values 用，不含新鲜度）。"""
    index: int
    name: str
    value: Any
    unit: str


@dataclass
class PushState:
    """某项的下位机推送档位。"""
    sub: bool
    fast: bool


@dataclass
class SubResult:
    """/api/sub 返回：push/log 为 None 表示该项未走这条分支。"""
    index: int
    name: str | None
    push: PushState | None
    log: bool | None


@dataclass
class CmdResult:
    """/api/cmd 返回。"""
    ok: bool
    index: int
    name: str
    args: list


@dataclass
class SetTunableResult:
    """/api/tunable 返回。"""
    ok: bool
    index: int
    value: Any


@dataclass
class StopResult:
    """/api/stop 返回。"""
    ok: bool
    cmd_index: int


@dataclass
class LeaseResult:
    """/api/script/claim 返回：脚本写权租约的领取结果。

    ok=False 说明写权被别的脚本占着，此时所有写路径都回 409
    （held_age_s 告诉你对方持有了多少秒，据此判断是"还在跑"还是"留下了尸体"）。
    evicted=True 只可能出现在 steal=True 时：意味着本次把别人踢下来了。
    """
    ok: bool
    token: str
    expires_in_s: float      # 本次领取/续租后还能撑多久（不写就到期，届时别人可接管）
    evicted: bool = False    # 本次是否抢占了别人
    held_age_s: float | None = None  # ok=False 时：对方已持有的秒数


@dataclass
class RegisteredItem:
    """一个被登记记录的监控项（start_log / 导出元信息用）。"""
    index: int
    name: str


@dataclass
class StartLogResult:
    """/api/log/start 返回。"""
    ok: bool
    registered: list[RegisteredItem]


@dataclass
class EndLogResult:
    """/api/log/end 返回。"""
    ok: bool
    samples: int
    dropped: int


@dataclass
class LogRow:
    """一条原始样本（长表的一行）。"""
    t: float  # 相对本段零点的毫秒
    index: int
    name: str
    value: Any


@dataclass
class LogRowsResult:
    """整段原始样本长表（log_rows() 与 print_log 内部使用；对外取原始点请用
    /api/log/print 导出，或 /api/log/calc 传 window 做子区间计算）。"""
    rows: list[LogRow]


@dataclass
class LogFileMeta:
    """导出 JSON 的元信息 + 原始样本长表。"""
    created: str
    samples: int
    dropped: int
    registered: list[RegisteredItem]
    data: list[LogRow]


@dataclass
class PrintLogResult:
    """/api/log/print 返回。columns/missing 只对 csv 有意义。"""
    ok: bool
    path: str
    format: str
    samples: int
    dropped: int
    columns: list[str] = field(default_factory=list)
    '''csv 宽表的列名（按 index 升序）'''
    missing: dict[str, int] = field(default_factory=dict)
    '''csv 每列的空单元格数（丢包数）；全空的桶不出行，只计这里'''


@dataclass
class Snapshot:
    """/api/status 返回：一眼看清 Backend 全貌。"""
    status: str
    message: str
    connected: bool
    monitor_count: int
    tunable_count: int
    cmd_count: int
    log_reg: list[int]
    logging: bool
    log_samples: int
    log_dropped: int
    abort: bool
    stop_cmd: int | None
    ports: list[PortInfo]


@dataclass
class CalcResult:
    """/api/log/calc 返回：对本段记录做表达式计算的结果。

    agg 只支持 mean/sum，value 是聚合后的单值（刻意没有逐点序列——
    要原始点用 print_log 导 csv/json）。
    missing 说明哪些项没有历史序列、退回了当前值（或完全没取值）。
    """
    ok: bool
    expr: str
    agg: str
    value: float | None
    n: int
    missing: dict[str, str]


@dataclass
class ConnectResult:
    """/api/connect 返回。"""
    ok: bool
    snapshot: Snapshot


@dataclass
class StatusMessage:
    """WS 文本通道推给前端的连接状态消息（前端按 op=="status" 处理）。"""
    op: str
    state: str
    message: str
