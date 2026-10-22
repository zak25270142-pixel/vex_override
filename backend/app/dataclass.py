"""
数据类集中地：协议解析产物 + 接口返回体 + 串口设备描述符。

本文件只放被动数据结构（dataclass / NamedTuple）与紧绑它们的枚举、类型别名，
不含任何逻辑；protocol / serial_bridge / hub / main 共同引用。
字段名即对外 JSON 键名（FastAPI 自动转），改动等于改接口契约。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, NamedTuple

# 监控/调参项能取的值形态。注意这是"Python 侧解出来的类型"，
# 与 protocol.ValueType 不是一一对应（多个 ValueType 都解成 int）。
CellValue = int | float | bool


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
    type: int   # 值类型码（ValueType）：FrameParser 靠它解后续 value/echo 帧
    tag: int    # 位标志（MONITOR_TAG_*：订阅档位、GETTER 等）
    name: str
    unit: str
    value: Any  # tunable 项的 value 会被 echo 回显覆盖成真实生效值


# ---------- 解析事件类型（FrameParser 产出 / Hub 消费）----------


class EventKind(Enum):
    """FrameParser 解出的一个事件属于哪一类。"""

    directory = "directory"        # 监控/调参目录项 → 填三张目录表
    cmdDirectory = "cmdDirectory"  # 动作命令目录项 → 填命令表，顺带认出停车命令
    value = "value"                # 监控值推送 → 更新最新值快照 + 按需记日志
    echo = "echo"                  # 调参写回确认 → 回写调参项真实生效值
    pong = "pong"                  # 心跳应答 → 时间戳在 SerialBridge 里已更新


class DirTable(Enum):
    """目录项属于哪张表（只对 EventKind.directory 有效）。"""

    monitor = "monitor"
    tunable = "tunable"


# ---------- FrameParser 产出的事件（Hub 按类型消费）----------
# 每种事件一个 dataclass；kind 是类级常量，用 isinstance 分派，语义比 dict 明确。


@dataclass
class RxEvent:
    """事件基类。kind 标出具体类别（ClassVar，不进构造参数）。"""
    kind: ClassVar[EventKind]


@dataclass
class DirectoryEvent(RxEvent):
    """监控/调参目录项 → 填三张目录表。"""
    kind: ClassVar[EventKind] = EventKind.directory
    table: DirTable
    item: MenuItem


@dataclass
class CmdDirectoryEvent(RxEvent):
    """动作命令目录项 → 填命令表，顺带认出停车命令。"""
    kind: ClassVar[EventKind] = EventKind.cmdDirectory
    item: CmdDirItem


@dataclass
class ValueEvent(RxEvent):
    """监控值推送 → 更新最新值快照 + 按需记日志。"""
    kind: ClassVar[EventKind] = EventKind.value
    index: int
    value: CellValue
    t_ms: int = 0
    '''下位机采样时刻：毫秒时钟低16位(uint16)，65.536s回绕；消费侧按无符号差分展开'''


@dataclass
class EchoEvent(RxEvent):
    """调参写回确认 → 回写调参项真实生效值。"""
    kind: ClassVar[EventKind] = EventKind.echo
    index: int
    value: CellValue


@dataclass
class PongEvent(RxEvent):
    """心跳应答 → 时间戳在 SerialBridge 里已更新。"""
    kind: ClassVar[EventKind] = EventKind.pong


@dataclass
class RxStats:
    """链路诊断计数（FrameParser 累计，UI/脚本直接读）。"""
    bytes: int = 0        # 已收字节数
    frames: int = 0       # 已收帧数
    badChecksum: int = 0  # 校验失败：线路干扰 / 波特率不匹配 / 帧边界错位
    unknownCmd: int = 0   # 收到不认识的命令字
    overflow: int = 0     # payload 超过 MAX_PAYLOAD，状态机强制回到找帧头


# ---------- 串口设备描述 ----------


@dataclass
class PortInfo:
    """一个串口设备的信息（pyserial 原始描述符的裁剪版，/api/ports 直接返回）。"""
    device: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str


# ---------- 区间统计（脚本标定用）----------
# 把一批样本压成常用统计量，脚本不用自己攒样本算。各字段的用途：
#   mean        算术均值。看"有没有稳态偏置"用这个
#   abs_mean    取绝对值后的均值。看"抖动/误差幅度"必须用这个——
#               来回震荡时正负抵消，mean 会接近 0 而 abs_mean 真实反映幅度
#   abs_max     最大绝对偏差。看最坏情况
#   std         标准差。注意是总体标准差（除以 n），不是样本标准差（除以 n-1）
#   rate_hz     样本数 / 区间秒数。反映下位机实际推送率，用来验证是否真按 100Hz 在推
#   ratio_ge    |值| >= 阈值的样本占比。判断"超差时间占比"这类指标（thresholds 传入阈值列表）
@dataclass
class AggregateStats:
    """一批样本压成的统计量（各字段用途见上）。"""
    n: int
    mean: float | None
    abs_mean: float | None
    min: float | None
    max: float | None
    abs_max: float | None
    std: float | None
    rate_hz: float | None
    ratio_ge: dict[str, float]  # 阈值字符串 → |值|>=阈值的占比（阈值动态传入，故保留 dict）


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
    age_s: float  # 距上次收到推送的秒数


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
    """/api/log/window 返回。"""
    rows: list[LogRow]


@dataclass
class LogStatsItem(AggregateStats):
    """一项的整段统计；直接摊平成 index/name + 各统计量，保持原有 JSON 结构。"""
    index: int
    name: str


@dataclass
class LogStatsResult:
    """/api/log/stats 返回。"""
    items: list[LogStatsItem]


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
