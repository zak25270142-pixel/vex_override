"""
VEX V5 User 口目录协议子系统：线上字节契约 + 目录项内存形状 + 上位机侧订阅状态。

帧格式 A5 | Cmd | Payload | XOR，XOR 从 Cmd 起逐字节异或；多字节数值一律小端。
三端同步（frontend/src/services/protocol.ts、include/communication.h、my_main.h）
"""

import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, NamedTuple

from .config import CONFIG

logger = logging.getLogger("vex.protocol")

# 帧头、帧长上限等协议适配参数一律直接读 CONFIG，不在本模块另起别名；
# 改固件只改 config.json，不动源码。

# ---------- 命令字 ----------
# 方向视角写死在类名上：CmdPost = 主控→上位机（上行），CmdGet = 上位机→主控（下行）。

class CmdPost(IntEnum):
    """主控 → 上位机"""

    TunableDirectory = 0  # 调参项目录（一项一帧，burst 下发）
    MonitorDirectory = 1  # 监控项目录（一项一帧，burst 下发）
    MonitorValue = 2  # 监控值推送（订阅后按档位周期推）
    TunableEcho = 3  # 调参项写回确认：上位机 SetTunable 后主控回这个，带真实生效值
    CMDDirectory = 4  # 动作命令目录（一项一帧）
    Response = 5  # 通用响应帧：payload = [L][原话cmd][原话payload][status][尾字节]
    #   L=payload 长度位（= payload[1:] 的总字节数），帧自带，解析只看它，不查目录推长度。
    #   status 定位看"声明尾长是否正好吃完剩余字节"：内定err/裸拒尾长 0 → status 是最后一字节；
    #   OK 的尾字节是二进制附加信息（如目录项数）、串模式的尾字节是原因串（长度=低6位）。
    KeyDirectory = 6  # 按键目录（一项一帧，payload: [index][name_len][name]）
    Discovery = 0xFE  # 发现/登记帧（发现期 1Hz 自报，连上后可带名字登记一次）
    Pong = 0xFF

class CmdGet(IntEnum):
    """上位机 → 主控"""

    RequestTunable = 0  # 请求调参目录
    RequestMonitor = 1  # 请求监控目录
    Subscribe = 2  # 订阅/退订监控项（payload: [index][tag]）
    SetTunable = 3  # 写调参项（payload: [index][8B 定长值]）
    RequestCommand = 4  # 请求动作命令目录
    KeyEvent = 5  # 按键事件：payload [index][state]。state 是"接下来的期望电平"（1=按下 / 0=抬起）
    RequestKeyDir = 6  # 请求按键目录（payload: [len][index...]，len=0 全量）；与上行 KeyDirectory 同号
    Ping = 0xFF

class ValueType(IntEnum):
    """值类型 与 my_main.h VALUE_TYPE 一致。"""
    UInt8 = 0
    UInt16 = 1
    UInt32 = 2
    UInt64 = 3
    Int8 = 4
    Int16 = 5
    Int32 = 6
    Int64 = 7
    Float = 8  # 监控值绝大多数是这个
    Double = 9
    Bool = 11  # 按 1 字节收
    OnOff = 12  # 与 Bool 同宽，语义上表示"开关"而非"真假"
    Color = 13  # 3 字节 RGB


# ---------- 监控 tag 位域 ----------
# 一个字节塞了四件事，上位机只有 SUB / FAST 两个 bit 有写权限：
#   bit7  SUB   订阅开关（0=不推 1=推）
#   bit6  FAST  推送档位（0=低速 1=高速）
#   bit5  GETTER 下位机内部标记（取值方式），上位机不得改
#   bit4~3 预留
#   bit2~0 KIND 该项语义类别（1=x 2=y 3=yaw），上位机不得改

MONITOR_TAG_SUB = 0x80
MONITOR_TAG_FAST = 0x40
MONITOR_TAG_GETTER = 0x20
MONITOR_TAG_KIND_MASK = 0x07

# ---------- 动作命令 tag 位域 ----------
#   bit7   RANGE  1=本帧在参数定义后面附带各参数的 (min,max)，宽度按该参数类型的 value_size 定宽
#   bit6~4 ARGC   参数个数（0~7）
#   bit3~0 SPEC   命令类别（0=普通 1=停车）
CMD_TAG_RANGE = 0x80
CMD_TAG_ARGC_MASK = 0x70
CMD_TAG_ARGC_SHIFT = 4
CMD_TAG_SPEC_MASK = 0x0F


class CmdSpec(IntEnum):
    Normal = 0
    Stop = 1  # 停车命令，Backend 据此识别并把它的帧提到最高优先级


# ---------- 目录项的内存形状 ----------
# 四张目录表 index → 目录项（表本体在 hub.py）；回放帧由 build_dir_frame 现场重建，
# 不再存原帧副本。index 由所属表的 dict key 表达，不在项里重复存。


@dataclass
class CmdField:
    """动作命令的一个参数。

    min/max 只在 tag 的 bit7(RANGE) 置位时才有值，且**按本字段的 type 解码、与字段同型**：
    整型字段解成 int、float/double 解成 float，不再一律当 float32。
    """
    type: int                               # ValueType 码；决定 value 与取值范围按什么类型解码
    name: str                               # 参数中文名（目录帧带来）
    min: float | int | None = None          # 取值下限；tag bit7(RANGE) 未置位时为 None，按 type 同型解码
    max: float | int | None = None          # 取值上限；与 min 成对，同上


@dataclass
class CmdTableItem:
    """动作命令目录的一项。命令字由所属表的 dict key 表达，不在项里重复存。"""
    tag: int
    name: str
    fields: list[CmdField] = field(default_factory=list)  
    def spec(self) -> int:
        """SPEC 位域"""
        return self.tag & CMD_TAG_SPEC_MASK

    def has_range(self) -> bool:
        """RANGE 位：参数定义后是否附带各参数的 (min,max)。"""
        return (self.tag & CMD_TAG_RANGE) != 0


@dataclass
class MenuItem:
    """监控/调参目录项，两张表共用同一个类。

    index 由所属表的 dict key 表达（目录请求、订阅、写调参都按 key 寻址）。
    value 按表分两种形状：monitor 表存 MonitorSample（最新值 + 固件戳，整体替换
    保证原子性）；tunable 表存裸值，会被 echo 回显覆盖成真实生效值。
    """
    type: int   # 值类型码（ValueType）：hub 解后续 value/echo 帧的唯一依据
    tag: int    # 位标志（MONITOR_TAG_*：订阅档位、GETTER 等）
    name: str                               # 目录项名（中文，脚本按名寻址用；协议可见）
    unit: str                               # 单位字符串（m、deg/s…），可为空串
    value: Any  # monitor: MonitorSample / tunable: 裸值（echo 回显覆盖）


class MonitorItem:
    """monitor 表的运行时实体（index -> 这个）：协议目录项 + 各源订阅意图。"""

    def __init__(self) -> None:
        self.item: MenuItem | None = None          # 协议目录项；None=目录还没拉到这一项
        self.tagdict: dict[str, tuple[bool, bool]] = {}  # source -> (sub, fast)

    @staticmethod
    def split_tag(tag: int) -> tuple[bool, bool]:
        """裸 tag 字节 → (sub, fast)。

        帧入口与目录解析那一刻只有字节、还没有实例，走这里；目录项到场后走 is_*()。
        """
        return ((tag & MONITOR_TAG_SUB) != 0, (tag & MONITOR_TAG_FAST) != 0)

    def merge_tag(self, incoming: int) -> None:
        """只合并上位机的订阅/速率位，保留本机 GETTER 与 KIND（及预留位）。

        与下位机 merge_subscribe_tag（communication.cpp）逐字对应；C 里
        static_cast<uint8_t>(~host_bits) 在 Python 就是 ~host_bits & 0xFF。

        目录项没到就没有可写的 tag，直接丢弃——订阅先于目录时，等目录帧到达，
        固件报回来的 tag 已经含这次订阅的结果，不需要在这儿暂存。
        """
        if self.item is None:
            return
        host_bits = MONITOR_TAG_SUB | MONITOR_TAG_FAST
        self.item.tag = (incoming & host_bits) | (self.item.tag & (~host_bits & 0xFF))

    def is_subscribed(self) -> bool:
        return self.item is not None and (self.item.tag & MONITOR_TAG_SUB) != 0

    def is_fast(self) -> bool:
        return self.item is not None and (self.item.tag & MONITOR_TAG_FAST) != 0

    def aggregate(self) -> tuple[bool, bool]:
        """多来源意图 → 单一档位"""
        any_sub = any(v[0] for v in self.tagdict.values())
        any_fast = any(v[1] for v in self.tagdict.values() if v[0])
        return (any_sub, any_fast)


# ---------- 最新样本快照 ----------
class MonitorSample(NamedTuple):
    """monitor 表 MenuItem.value 的元素：一个监控项的最新样本"""
    value: Any # 最新值
    stamp_raw: int | None = None  # 该帧下位机原始 16bit 戳（不展开）；None = 只有目录初值、没推送过


# ---------- 动作命令段 ----------
# 动作命令段默认 0x80~0x9F（config.cmd_action_min/max），目录帧按需动态下发，
# 命令字不在小枚举里——协议层按段判定。目录补发上限是 config.req_list_max，
# 超出固件整帧拒绝，主机端应改走全量重拉。这些值一律直接读 CONFIG，不另起别名。

# ---------- 通用响应帧（CmdPost.Response）----------
# 帧格式: A5 | 05 | L | 原话cmd | status | 附加段 | 原话payload | XOR
#   L 覆盖本字节之后到校验前的全部字节（最小 2 = 原话cmd + status），收 1B 即定帧长。
#   status 固定在 p[2]。附加段长度由 status 自己给出，不用反推：
#     0x00                   OK 无附加段（传完通知 / 通用 OK）
#     0x80（OK+info）/ 内定err类       [len][data...]，len=0 即不带内容
#     串类                   裸串 data...，长度 = status & 0x3F（低 6 位就是串长）
#   原话payload 在帧尾、只做 ACK 匹配，长度由 L 反推；长度为 0 = 匹配该 cmd 全部。
# status 按范围分四类 + 两个 OK 特殊点（位运算同构）：
#   0x00      OK，无 info（目录传完通知 / 通用 OK）
#   0x80      OK + info（整表请求收到即回，info = 1B 表内总项数）
#   0x01~0x3F 需补发+串类      0x40~0x7F 需补发+内定err类
#   0x81~0xBF 明确拒绝+串类    0xC0~0xFF 明确拒绝+内定err类
#   OK 族 = (status & 0x7F) == 0；其余 &0x80 判类别、&0x40 判串/err。
#   低 6 位两类都有实义：串类 = 串长(1~63)，内定err类 = err号(0~63)。

RESP_OK = 0x00
RESP_OK_INFO = 0x80               # OK + info（整表请求收到即回，带表项数）
RESP_OK_FAMILY_MASK = 0x7F       # status & 0x7F == 0 即 OK 族（0x00 / 0x80）
RESP_CLASS_MASK = 0x80           # 非 OK 族时 bit7 类别
RESP_MODE_MASK = 0x40            # 非 OK 族时 bit6 串类/内定err类
RESP_PAYLOAD_MASK = 0x3F


class RespErrRetry(IntEnum):
    """需补发类内定err（status = 0x40 | n）"""

    Generic = 0  # 通用重试
    Checksum = 1  # 校验失败（回显字节可疑，仅作线索）
    PoolFull = 2  # 目录补发池空间不足：info[0]=当前空余字节数，按空余量等待重发


class RespErrReject(IntEnum):
    """拒绝类内定err（status = 0xC0 | n）"""

    Generic = 0  # 通用拒绝
    IndexRange = 1  # 下标超表（Subscribe/SetTunable）——该做的是刷目录
    Busy = 2  # 动作命令被 is_busy 拒绝——该等当前动作跑完
    UnknownCmd = 3  # 命令字未注册——重发无意义，收到即终局
    InFlight = 4  # 同表全量/增量在途：这条增量无意义，不重发（info[0]=当前空余字节数）


# 内定err → 中文名（status 全字节为键；与 communication.h / protocol.ts 同步）
# 内定err 类的附加段是 [len][data]：0x42 / 0xC4 带 1B 池空余，其余 len=0 不带内容；
# 不带该 info 的固件按原逻辑处理，info 为空即视为"没有该能力"。
_RESP_ERR_NAMES_ZH = {
    0x40 | RespErrRetry.Generic: "通用重试",
    0x40 | RespErrRetry.Checksum: "校验失败",
    0x40 | RespErrRetry.PoolFull: "目录补发池满",
    0xC0 | RespErrReject.Generic: "通用拒绝",
    0xC0 | RespErrReject.IndexRange: "下标超表",
    0xC0 | RespErrReject.Busy: "忙",
    0xC0 | RespErrReject.UnknownCmd: "命令字未注册",
    0xC0 | RespErrReject.InFlight: "同表目录在途",
}


def decode_response(p: bytes) -> tuple[int, bytes, int, bytes] | None:
    """解 Response 帧 payload：[L][原话cmd][status][附加段][原话payload]。

    status 固定在 p[2]，附加段长度由 status 自己给出：
      0x00                 无
      0x80（OK+info）/ 内定err类     [len][data...]（返回 data，不含长度字节）
      串类                 裸串（长度 = status & 0x3F，返回整段）
    原话payload 在帧尾、长度由 L 反推；长度 0 表示「无回显 = 匹配该 cmd 全部」。
    不看命令字、不读原话payload 的内容，整帧自描述。L 与实际长度不符返回 None。
    """
    if len(p) < 3 or p[0] < 2 or p[0] != len(p) - 1:
        return None
    echo_cmd = p[1]
    status = p[2]
    off = 3
    info = b""
    if status != RESP_OK:
        if status == RESP_OK_INFO or (status & RESP_MODE_MASK):
            if off >= len(p):
                return None  # 缺 len
            n = p[off]
            off += 1 + n
            if off > len(p):
                return None  # len 越出帧外
            info = bytes(p[4:off])
        else:
            off += status & RESP_PAYLOAD_MASK  # 串类：低 6 位就是串长，不带长度前缀
            if off > len(p):
                return None
            info = bytes(p[3:off])
    return (echo_cmd, bytes(p[off:]), status, info)



def is_ack_tracked(cmd: int) -> bool:
    """哪些下行帧需要 Response 确认：目录请求/订阅/调参写入/按键事件 + 整个动作命令段。

    Ping 有 Pong、SetTunable 成功有 TunableEcho（显式回应）不跟踪；但调参被拒时
    无回显兜底，固件会补拒绝帧，所以 SetTunable 也在表里（TunableEcho 兼作确认）。
    按键事件（KeyEvent）与动作命令同等待遇：固件处理完必须回 Response，
    否则后端按 ACK_TIMEOUT_CMD_S 原帧重发（见 docs/代码补充注释/07-Backend-传输与回执.md）。
    """
    return cmd in (
        CmdGet.RequestTunable,
        CmdGet.RequestMonitor,
        CmdGet.Subscribe,
        CmdGet.SetTunable,
        CmdGet.RequestCommand,
        CmdGet.RequestKeyDir,
        CmdGet.KeyEvent,
    ) or (CONFIG.cmd_action_min <= cmd <= CONFIG.cmd_action_max)

# ---------- 值编解码 ----------

def value_size(type_: int) -> int:
    """各类型在帧里占的字节数；str/other 不上报，返回 0（与 TS valueSize 一致）"""
    if type_ in (ValueType.UInt8, ValueType.Int8, ValueType.Bool, ValueType.OnOff):
        return 1
    if type_ in (ValueType.UInt16, ValueType.Int16):
        return 2
    if type_ in (ValueType.UInt32, ValueType.Int32, ValueType.Float):
        return 4
    if type_ in (ValueType.UInt64, ValueType.Int64, ValueType.Double):
        return 8
    if type_ == ValueType.Color:
        return 3
    return 0 


def decode_value(data: bytes, off: int, type_: int) -> Any:
    """按类型从小端字节流解码；调用方保证剩余字节足够。"""
    if type_ == ValueType.UInt8:
        return data[off]
    elif type_ == ValueType.Int8:
        return struct.unpack_from("<b", data, off)[0]
    elif type_ == ValueType.UInt16:
        return struct.unpack_from("<H", data, off)[0]
    elif type_ == ValueType.Int16:
        return struct.unpack_from("<h", data, off)[0]
    elif type_ == ValueType.UInt32:
        return struct.unpack_from("<I", data, off)[0]
    elif type_ == ValueType.Int32:  
        return struct.unpack_from("<i", data, off)[0]
    elif type_ == ValueType.Float:
        return struct.unpack_from("<f", data, off)[0]
    elif type_ == ValueType.Double:
        return struct.unpack_from("<d", data, off)[0]
    elif type_ == ValueType.UInt64:
        return struct.unpack_from("<Q", data, off)[0]
    elif type_ == ValueType.Int64:
        return struct.unpack_from("<q", data, off)[0]
    elif type_ in (ValueType.Bool, ValueType.OnOff):
        # 直接用整字节当布尔：下位机写 0/1，不判 !=0 是为了与 TS 行为一致
        return data[off]
    elif type_ == ValueType.Color:
        r = data[off]
        g = data[off + 1] if off + 1 < len(data) else 0
        b = data[off + 2] if off + 2 < len(data) else 0
        return f"#{r:02x}{g:02x}{b:02x}"  # 解成 CSS 色串，前端直接能显示
    return 0

def encode_value(type_: int, value: Any) -> bytes:
    """按真实宽度编码（动作命令字段用，紧凑无填充）。"""
    size = value_size(type_)
    if size == 0:
        return b""
    return encode_value8(type_, value)[:size]

def encode_value8(type_: int, value: Any) -> bytes:
    """定长 8B 小端（SetTunable 用），不足高位为 0。

    为什么 SetTunable 用定长 8B：不然下位机在只知道cmd的情况下
    仍不能推断长度，处理会很麻烦，是一种妥协。

    类型纪律：这是"值→字节"的最后一站，value 必须已经是目录字段声明类型的
    Python 对应物——数值字段 int/float、Color 字段 "#RRGGBB" 字符串。类型不符
    直接抛错，绝不静默编 0：静默 0 会让笔误（如把 "6.0" 当电压传给车）伪装成
    "指令生效且值为 0"，是最难排查的一类故障。第一道校验在
    hub.cmd / hub.set_tunable_value（边界处按目录字段类型挡，以 400 呈现），
    这里是纵深防御，正常不可达。
    """
    buf = bytearray(8)
    if type_ == ValueType.Color:
        if not isinstance(value, str):
            raise TypeError(f"Color 字段需要 '#RRGGBB' 字符串，收到 {type(value).__name__}: {value!r}")
        s = value[1:] if value.startswith("#") else value
        if len(s) != 6:
            raise ValueError(f"Color 需要 6 位十六进制（如 '#ff0000'），收到 {value!r}")
        rgb = int(s, 16)  # 非十六进制字符在此抛 ValueError
        buf[0] = (rgb >> 16) & 0xFF
        buf[1] = (rgb >> 8) & 0xFF
        buf[2] = rgb & 0xFF
        return bytes(buf)
    # 其余均为数值型（含 Bool/OnOff）。bool 是 int 的子类，但把 true 当
    # 电压/转速传几乎总是笔误——仅开关字段放行布尔
    if isinstance(value, bool):
        if type_ not in (ValueType.Bool, ValueType.OnOff):
            raise TypeError(f"数值字段收到布尔值 {value!r}（类型码 {type_}）")
    elif not isinstance(value, (int, float)):
        raise TypeError(f"数值字段需要 int/float，收到 {type(value).__name__}: {value!r}")
    n = float(value)
    if type_ == ValueType.UInt8:
        buf[0] = int(n) & 0xFF
    elif type_ == ValueType.Int8:
        struct.pack_into("<b", buf, 0, int(n))
    elif type_ == ValueType.UInt16:
        struct.pack_into("<H", buf, 0, int(n) & 0xFFFF)
    elif type_ == ValueType.Int16:
        struct.pack_into("<h", buf, 0, int(n))
    elif type_ == ValueType.UInt32:
        struct.pack_into("<I", buf, 0, int(n) & 0xFFFFFFFF)
    elif type_ == ValueType.Int32:
        struct.pack_into("<i", buf, 0, int(n))
    elif type_ == ValueType.Float:
        struct.pack_into("<f", buf, 0, float(n))
    elif type_ == ValueType.Double:
        struct.pack_into("<d", buf, 0, float(n))
    elif type_ == ValueType.UInt64:
        struct.pack_into("<Q", buf, 0, int(n) & 0xFFFFFFFFFFFFFFFF)
    elif type_ == ValueType.Int64:
        struct.pack_into("<q", buf, 0, int(n))
    elif type_ in (ValueType.Bool, ValueType.OnOff):
        buf[0] = 1 if n else 0
    return bytes(buf)

# ---------- 组帧 ----------

def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """组一帧：A5 | Cmd | Payload | XOR。XOR 从 Cmd 起（不含 A5）逐字节异或。"""
    xor = cmd
    for b in payload:
        xor ^= b
    return bytes([CONFIG.frame_head, cmd & 0xFF]) + payload + bytes([xor & 0xFF])

# ---------- 目录帧重建（WS 目录回放用；目录表不再存原帧字节）----------
# 四张目录表的下行请求命令字与上行目录帧命令字同号（0/1/4/6），重建时直接拿
# req_cmd 当上行帧 cmd，不需要映射表；将来若出现异号表再补映射。


def build_dir_frame(req_cmd: int, index: int, item) -> bytes:
    """按目录项重建上行目录帧，供 WS 回放（前端 FrameParser 按布局自己解长度）。

    req_cmd: 下行目录请求命令字；index: 表 key（cmd 表即命令字 0x80~0x9F）；
    item: MenuItem / CmdTableItem / str（按键表直接存名字）。目录表只存解析结果这一份真源，
    重建只在页面拉目录时发生，帧布局与 _on_frame 的解析分支一一对应。
    monitor 项的 value 是 MonitorSample，回放时编它的 .value；tunable 项是裸值。
    """
    if req_cmd in (CmdPost.TunableDirectory, CmdPost.MonitorDirectory):
        nb = item.name.encode("utf-8")
        ub = item.unit.encode("utf-8")
        v = item.value.value if req_cmd == CmdPost.MonitorDirectory else item.value
        payload = (
            bytes([index, item.type, item.tag, len(nb)]) + nb
            + bytes([len(ub)]) + ub + encode_value(item.type, v)
        )
    elif req_cmd == CmdPost.CMDDirectory:
        nb = item.name.encode("utf-8")
        content = bytearray([index, item.tag, len(nb)]) + nb + bytes([len(item.fields)])
        for f in item.fields:
            fb = f.name.encode("utf-8")
            content += bytes([f.type, len(fb)]) + fb
        if item.has_range():
            for f in item.fields:
                # 与解析侧一致：无宽度的类型 / 帧截断导致范围缺失时，整段范围不再往下写
                if value_size(f.type) == 0 or f.min is None or f.max is None:
                    break
                content += encode_value(f.type, f.min) + encode_value(f.type, f.max)
        payload = len(content).to_bytes(2, "little") + bytes(content)
    else:  # CmdPost.KeyDirectory：表值就是名字字符串
        nb = item.encode("utf-8")
        payload = bytes([index, len(nb)]) + nb
    return build_frame(req_cmd, payload)


# ---------- 上位机内部段：后端 → 前端 WS 的帧（不下发主控）----------
# 0xE0~0xEF 只在上位机两端使用：这些帧由后端自己造，永远不到下位机，
# 因此不写进 include/communication.h，下位机不需要认识。
# 帧仍走 A5 | Cmd | Payload | XOR，但作为独立 WS 二进制消息发送，
# 不混进下位机原始字节流（前端 onmessage 按 Cmd 先截下来）。
CMD_UI_STATUS = 0xE0
CMD_UI_ALERT = 0xE1
CMD_UI_CHART_SUGGEST = 0xE2

# status 三态：前端把 idle/connecting/disconnected 都归一成"连接中"
UI_STATE_CONNECTING = 0
UI_STATE_CONNECTED = 1
UI_STATE_ERROR = 2


def _ui_tail(extra: bytes) -> bytes:
    """[L:1B][extra…]；L 封顶 255。L=0 表示前端用本地文案，不传中文。"""
    extra = extra[:255]
    return bytes([len(extra)]) + extra


def build_ui_status(state: int) -> bytes:
    """A5 E0 <state:1B>[L=0] XOR。文案由前端按 state 造。"""
    return build_frame(CMD_UI_STATUS, bytes([state & 0xFF]) + _ui_tail(b""))


def build_ui_alert(cmd: int) -> bytes:
    """A5 E1 <cmd:1B>[L=0] XOR。level 恒 giveup（后端只推前端看不见的结论），不占字节。"""
    return build_frame(CMD_UI_ALERT, bytes([cmd & 0xFF]) + _ui_tail(b""))


# 曲线建议帧容量：与前端 protocol.ts 的 UI_SUGGEST_MAX_* 同名同值，
# 也对应 config.json 的 max_groups / max_items（桌面端 4 通道 × 4 条 = 16）。
# 手机端是单通道 5 条，不消费建议帧。
UI_SUGGEST_MAX_GROUPS = 4
UI_SUGGEST_MAX_ITEMS = 4


def build_ui_chart_suggest(groups, source: str = "", note: str = "") -> bytes:
    """A5 E2 <len1><idx…> … <len4><idx…> <extralen><extra> XOR。

    groups: [[index, ...], ...]，与前端 number[][] 同构。
    变长 TLV：固定 4 组，每组 1 个长度字节 + 该组 index（上限 UI_SUGGEST_MAX_ITEMS）；
    len=0 表示该通道清空。组数与每组上限由前后端同名常数决定。
    extra = [src_len][source UTF-8][note_len][note UTF-8]，说明建议来源与理由。
    """
    payload = bytearray()
    used = min(len(groups), UI_SUGGEST_MAX_GROUPS)
    for g in groups[:used]:
        items = [int(it) & 0xFF for it in g[:UI_SUGGEST_MAX_ITEMS]]
        payload.append(len(items))
        payload.extend(items)
    payload.extend(b"\x00" * (UI_SUGGEST_MAX_GROUPS - used))  # 补足空组
    src = source.encode("utf-8")[:100]
    nt = note.encode("utf-8")[:100]
    extra = bytes([len(src)]) + src + bytes([len(nt)]) + nt
    payload.extend(_ui_tail(extra))
    return build_frame(CMD_UI_CHART_SUGGEST, bytes(payload))


def decode_discovery(payload: bytes) -> tuple[bool, str | None] | None:
    """解 0xFE payload，返回 (是否清除登记, 设备名)；非法返回 None（不抛异常）。

    三种合法输入：HDU+0x00（广播）、HDU+1..16+名字（登记）、HDU+0xFF（清除）。
    XOR 已由 FrameParser 校验。
    """
    if len(payload) < 4 or payload[:3] != CONFIG.discovery_magic.encode("ascii"):
        return None
    n = payload[3]
    if n == CONFIG.discovery_len_clear:
        return (True, None) if len(payload) == 4 else None
    if n > CONFIG.discovery_name_max or len(payload) != 4 + n:
        return None
    if n == 0:
        return (False, None)
    return (False, payload[4:].decode("utf-8", errors="replace"))


def _req_payload(indices: list[int] | None) -> bytes:
    """目录请求 payload：[len 1B][index...]；len=0 全量，非0=只补发指定下标。"""
    idx = list(indices or [])
    if len(idx) > CONFIG.req_list_max:
        raise ValueError(f"补发列表 {len(idx)} 项超上限 {CONFIG.req_list_max}，应改全量重拉")
    return bytes([len(idx)]) + bytes(i & 0xFF for i in idx)

def request_tunable(indices: list[int] | None = None) -> bytes:
    """请求调参目录。indices=None/空=全量；非空=只补发这些下标（不清已收项）"""
    return build_frame(CmdGet.RequestTunable, _req_payload(indices))

def request_monitor(indices: list[int] | None = None) -> bytes:
    """请求监控目录（indices 语义同 request_tunable）"""
    return build_frame(CmdGet.RequestMonitor, _req_payload(indices))

def request_command(indices: list[int] | None = None) -> bytes:
    """请求动作命令目录（indices 语义同 request_tunable）"""
    return build_frame(CmdGet.RequestCommand, _req_payload(indices))

def request_key_table(indices: list[int] | None = None) -> bytes:
    """请求按键目录（indices 语义同 request_tunable）"""
    return build_frame(CmdGet.RequestKeyDir, _req_payload(indices))

def subscribe(index: int, sub: bool, fast: bool) -> bytes:
    """订阅帧。只带 SUB/FAST 两个 bit，下位机会保留它自己的 GETTER/KIND。

    固件支持 index=0xFF 对全部监控项套同一 tag（communication.cpp），
    但后端不用：订阅档位是 per-index diff 管理的，一帧广播会抹掉项间差异。
    """
    tag = (MONITOR_TAG_SUB if sub else 0) | (MONITOR_TAG_FAST if fast else 0)
    return build_frame(CmdGet.Subscribe, bytes([index & 0xFF, tag]))


def ping() -> bytes:
    return build_frame(CmdGet.Ping)

def set_tunable(index: int, type_: int, value: Any) -> bytes:
    """写调参项。值固定 8 字节，类型由调用方从目录里查到后传入。"""
    return build_frame(CmdGet.SetTunable, bytes([index & 0xFF]) + encode_value8(type_, value))

def command_send(cmd_index: int, field_types: list[int], values: list[Any]) -> bytes:
    """发动作命令。cmd_index 就是命令字本身。

    field_types 必须来自目录帧解析出的字段类型表，且与 values 一一对应；
    个数对不上会导致下位机按 ARGC 切出错误的参数（Backend 在 hub.cmd 里已做校验）。
    """
    payload = bytearray()
    for t, v in zip(field_types, values):
        payload.extend(encode_value(t, v))
    return build_frame(cmd_index, bytes(payload))


# ---------- 帧解析状态机 ----------

class _RxState(IntEnum):
    WaitHead = 0  # 找 A5
    WaitCmd = 1  # 收命令字
    WaitData = 2  # 收 payload（长度靠试算）
    WaitXor = 3  # 收校验字节


def _table_frame_len(p: bytes, length: int) -> int:
    """试算调参/监控目录帧 payload 总长；不够返回 -1。

    payload 布局（下位机 communication.h 的 send_tunable/monitor_dir 对应）：
        [0] index
        [1] type     值类型（ValueType 编号）
        [2] tag      监控项才有意义：SUB/FAST/GETTER/KIND 位域
        [3] name_len
        [4..] name   UTF-8 中文名，如"左0转速"
        [..] unit_len
        [..] unit    单位串，如"rpm"
        [..] value   按 type 宽度（value_size）
    返回 -1 = 字节还不够算出总长，调用方继续等下一批。
    """
    if length < 4:
        return -1
    name_len = p[3]
    unit_len_at = 4 + name_len
    if length < unit_len_at + 1:
        return -1
    return 5 + name_len + p[unit_len_at] + value_size(p[1])

def _cmd_table_frame_len(p: bytes, length: int) -> int:
    """动作命令目录帧 payload 总长；不够返回 -1。

    帧头 2B 就是"后续内容长度"（小端 uint16），所以直接 2 + 该值，不用遍历字段走查：
    [内容长度2B][命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[bit7=1时:(min,max)×N]
    之所以由下位机给长度：min/max 改成按字段类型定宽后，长度要按各字段 value_size 累加，
    让上位机再实现一遍这套走查纯属折磨（且容易与下位机不一致）。
    """
    if length < 2:
        return -1
    return 2 + int.from_bytes(p[0:2], "little")


class FrameParser:
    """与 TS FrameParser 同构的逐字节状态机：喂串口 chunk，吐 (cmd, payload)。

    只做帧边界：校验通过后回调 on_frame(cmd, payload)，payload 不含帧头/
    命令字/校验字节。目录解析、监控值/回显解码等业务语义在 hub.py——那里的
    目录表是类型真源，本类不存任何类型状态，也不认识任何业务结构。
    """

    def __init__(self) -> None:
        self.state = _RxState.WaitHead
        '''当前状态'''
        
        self.cmd = 0
        '''当前命令字'''

        self.expect = -1
        '''本帧 payload 应有长度；-1 = 还没算出来'''
        
        self.len = 0
        '''已收 payload 字节数'''
        
        self.xor = 0 
        '''滚动校验，从 Cmd 起'''

        self.payload = bytearray(CONFIG.max_payload)
        '''当前 payload 缓冲区'''

    def feed(self, chunk: bytes, on_frame: Callable[[int, bytes], None]) -> None:
        """
        喂一批串口字节，逐字节走状态机，解出完整帧就调 on_frame(cmd, payload)。
        chunk   : 本次从串口读到的原始字节（可含半帧/多帧，状态机自己处理粘包拆包）
        on_frame: 完整帧出口 (cmd, payload)，payload 已剥掉帧头/命令字/校验
        """
        for b in chunk:
            if self.state == _RxState.WaitHead:
                if b == CONFIG.frame_head:
                    self.state = _RxState.WaitCmd
            elif self.state == _RxState.WaitCmd:
                self.cmd = b
                self.len = 0
                self.xor = b
                # CmdPost 枚举就是合法上行命令字的全集（Python 3.12+ 支持值成员检查）。
                # 不在枚举里的字节立刻回找帧头，不能当"有 payload 的帧"收——否则
                # expect=-1 会一路吞到 config.max_payload，把后面几帧真数据一起吃掉。
                if b not in CmdPost:
                    self.state = _RxState.WaitHead
                elif b == CmdPost.Pong:
                    self.state = _RxState.WaitXor    # 唯一零 payload 帧：A5 FF FF
                else:
                    self.expect = -1                 # 其余都有 payload，长度边收边试算
                    self.state = _RxState.WaitData
            elif self.state == _RxState.WaitData:
                if self.len >= CONFIG.max_payload:
                    self.state = _RxState.WaitHead
                    continue
                self.payload[self.len] = b
                self.len += 1
                self.xor ^= b

                if self.expect < 0:
                    # 边收边试算长度：收到足够字节能确定总长就定下来。
                    # 监控值帧自带 1B 长度前缀，收 1 字节就能定长；
                    # 调参回显 [type][index][value] 的 type 前缀自带宽度；
                    # 响应帧 [L][原话cmd][status][info段][原话payload] 同理，
                    #   L 覆盖之后全部字节（含 info 段与原话payload），收 1 字节即定长；
                    #   L 最小 2（原话cmd+status），0/1 只可能是流错位；
                    # 目录帧要收完名字长度、单位长度才能算（_table_frame_len/_cmd_table_frame_len）。
                    if self.cmd == CmdPost.Discovery:
                        # 0xFE 定长：body 必须是 HDU 魔数 + len + name，前 3 字节逐一核对魔数。
                        if self.len <= 3 and self.payload[self.len - 1] != ord(CONFIG.discovery_magic[self.len - 1]):
                            self.state = _RxState.WaitHead
                            logger.warning("丢弃非法发现帧：魔数不匹配 payload=%r",
                                           bytes(self.payload[: self.len]))
                        elif self.len == 4:
                            name_len = self.payload[3]
                            if name_len == CONFIG.discovery_len_clear:
                                self.expect = 4  # 清除登记帧不带名字字节
                            elif name_len <= CONFIG.discovery_name_max:
                                self.expect = 4 + name_len
                            else:
                                self.state = _RxState.WaitHead
                                logger.warning("丢弃非法发现帧：名长非法 %d payload=%r",
                                               name_len, bytes(self.payload[: self.len]))
                    elif self.cmd == CmdPost.MonitorValue:
                        # payloadlen==0 对监控值帧非法：固件 monitor_tick 只在 pack_num>0
                        # 时 flush 且每帧必含 2B 时间戳（communication.cpp），真实帧
                        # payloadlen>=2。收到 0 只可能是流错位把数据字节当成了长度字节
                        # ——直接回找帧头，不猜长度（猜 expect=1 会多吞 2 字节；
                        # 旧代码留着 expect=-1 更是一路吞到 config.max_payload 才重同步）
                        if self.payload[0] == 0:
                            self.state = _RxState.WaitHead
                        else:
                            self.expect = 1 + self.payload[0] # len 只算它后面那些字节，加上 len 自身才是总长
                    elif self.cmd == CmdPost.TunableEcho:
                        # [type][index][value]：type 前缀即宽度，解帧不查目录
                        sz = value_size(self.payload[0])
                        if sz == 0:
                            self.state = _RxState.WaitHead  # 不认识的值类型：当错误帧重找 A5
                        else:
                            self.expect = 2 + sz
                    elif self.cmd == CmdPost.KeyDirectory:
                        # [index][name_len][name]：name_len 1~16，收够 2B 即可定长。
                        # 必须在下面的 _table_frame_len 兜底之前判定——按键帧布局与调参/监控目录不同
                        if self.len >= 2:
                            name_len = self.payload[1]
                            if name_len == 0 or name_len > 16:
                                self.state = _RxState.WaitHead
                            else:
                                self.expect = 2 + name_len
                    elif self.cmd == CmdPost.CMDDirectory:
                        total = _cmd_table_frame_len(bytes(self.payload[: self.len]), self.len)
                        if total >= 0:
                            self.expect = total
                    elif self.cmd == CmdPost.Response:
                        # payloadlen 最小 2（原话cmd+status），0/1 = 流错位，回找帧头不猜长度
                        if self.payload[0] < 2:
                            self.state = _RxState.WaitHead
                        else:
                            self.expect = 1 + self.payload[0]
                    else:
                        total = _table_frame_len(bytes(self.payload[: self.len]), self.len)
                        if total >= 0:
                            self.expect = total
                if self.expect > 0 and self.len >= self.expect:
                    self.state = _RxState.WaitXor
            elif self.state == _RxState.WaitXor:
                self.state = _RxState.WaitHead
                if (self.xor ^ b) == 0:
                    on_frame(self.cmd, bytes(self.payload[: self.len]))
                elif self.cmd == CmdPost.Discovery:
                    logger.warning("丢弃非法发现帧：校验失败 payload=%r",
                                   bytes(self.payload[: self.len]))
