"""
VEX V5 User 口二进制协议（与 frontend/src/services/protocol.ts 及
include/communication.h 严格一一对应）

帧格式: A5 | Cmd | Payload... | XOR
  XOR = Cmd 起逐字节异或（不含帧头）
多字节数值一律小端（V5 ARM 与 PC x86 同为小端）

本文件是帧编解码的唯一真源：上位机(TS)、后端(本文件)、下位机(C++)三处
必须同步修改，任何一边改了编号或位域而另外两边没跟上，链路会静默错乱
（表现为"值对不上"而不是"报错"），改这里要三处一起改。
"""

import struct
from collections.abc import Callable
from enum import IntEnum

from .dataclass import (
    CellValue,
    CmdDirItem,
    CmdDirectoryEvent,
    CmdField,
    DirTable,
    DirectoryEvent,
    EchoEvent,
    MenuItem,
    PongEvent,
    RxEvent,
    RxStats,
    ValueEvent,
)

FRAME_HEAD = 0xA5 # 帧头

MAX_PAYLOAD = 445 # payload 上限，与下位机 USB 收发缓冲一致

# ---------- 命令字 ----------
# 方向视角写死在类名上：CmdPost = 主控→上位机（上行），CmdGet = 上位机→主控（下行）。

class CmdPost(IntEnum):
    """主控 → 上位机"""

    TunableDirectory = 0  # 调参项目录（一项一帧，burst 下发）
    MonitorDirectory = 1  # 监控项目录（一项一帧，burst 下发）
    MonitorValue = 2  # 监控值推送（订阅后按档位周期推）
    TunableEcho = 3  # 调参项写回确认：上位机 SetTunable 后主控回这个，带真实生效值
    CMDDirectory = 4  # 动作命令目录（一项一帧）
    Pong = 0xFF  

class CmdGet(IntEnum):
    """上位机 → 主控"""

    RequestTunable = 0  # 请求调参目录
    RequestMonitor = 1  # 请求监控目录
    Subscribe = 2  # 订阅/退订监控项（payload: [index][tag]）
    SetTunable = 3  # 写调参项（payload: [index][8B 定长值]）
    RequestCommand = 4  # 请求命令目录
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
# 因此 Subscribe 下发时下位机只把 SUB/FAST 合并进来，保留本机的 GETTER 和 KIND；
# 上位机若整字节覆盖会把下位机的类别信息抹掉。见 communication.h 的合并逻辑。

MONITOR_TAG_SUB = 0x80
MONITOR_TAG_FAST = 0x40
MONITOR_TAG_GETTER = 0x20
MONITOR_TAG_KIND_MASK = 0x07

def tag_subscribed(tag: int) -> bool:
    """从tag提取是否订阅"""
    return (tag & MONITOR_TAG_SUB) != 0


def tag_fast(tag: int) -> bool:
    """从tag提取推送挡位"""
    return (tag & MONITOR_TAG_FAST) != 0


# ---------- 动作命令 tag 位域 ----------
#   bit7   RANGE  1=本帧在参数定义后面附带各参数的 (min,max)，宽度按该参数类型的 value_size 定宽
#   bit6~4 ARGC   参数个数（0~7）—— 这是参数个数的唯一来源，不是靠 payload 长度反推
#   bit3~0 SPEC   命令类别（0=普通 1=停车）
# 参数个数写进 tag 是因为命令帧 payload 是紧凑拼接的，收到帧时无法靠长度反推
CMD_TAG_RANGE = 0x80
CMD_TAG_ARGC_MASK = 0x70
CMD_TAG_ARGC_SHIFT = 4
CMD_TAG_SPEC_MASK = 0x0F


class CmdSpec(IntEnum):
    Normal = 0
    Stop = 1  # 停车命令，Backend 据此识别并把它的帧提到最高优先级

def cmd_spec(tag: int) -> int:
    """从 tag 提取命令类别"""
    return tag & CMD_TAG_SPEC_MASK

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


def decode_value(data: bytes, off: int, type_: int) -> CellValue:
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

def encode_value(type_: int, value: CellValue) -> bytes:
    """按真实宽度编码（动作命令字段用，紧凑无填充）。"""
    size = value_size(type_)
    if size == 0:
        return b""
    return encode_value8(type_, value)[:size]

def encode_value8(type_: int, value: CellValue) -> bytes:
    """定长 8B 小端（SetTunable 用），不足高位为 0。

    为什么 SetTunable 用定长 8B：不然下位机在只知道cmd的情况下
    仍不能推断长度，处理会很麻烦，是一种妥协。
    """
    buf = bytearray(8)
    n = float(value) if isinstance(value, (int, float)) else 0.0
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
    elif type_ == ValueType.Color:
        s = str(value)
        if s.startswith("#"):
            s = s[1:]
        if len(s) == 6:
            rgb = int(s, 16)
            buf[0] = (rgb >> 16) & 0xFF
            buf[1] = (rgb >> 8) & 0xFF
            buf[2] = rgb & 0xFF
    return bytes(buf)

# ---------- 组帧 ----------

def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """组一帧：A5 | Cmd | Payload | XOR。XOR 从 Cmd 起（不含 A5）逐字节异或。"""
    xor = cmd
    for b in payload:
        xor ^= b
    return bytes([FRAME_HEAD, cmd & 0xFF]) + payload + bytes([xor & 0xFF])

def request_tunable() -> bytes:
    """请求调参目录"""
    return build_frame(CmdGet.RequestTunable)

def request_monitor() -> bytes:
    """请求监控目录"""
    return build_frame(CmdGet.RequestMonitor)

def request_command() -> bytes:
    """请求动作命令目录"""
    return build_frame(CmdGet.RequestCommand)

def subscribe(index: int, sub: bool, fast: bool) -> bytes:
    """订阅帧。只带 SUB/FAST 两个 bit，下位机会保留它自己的 GETTER/KIND。

    index=0xFF 是广播语义：对全部监控项套用这个 tag（Backend 初始化时用）。
    """
    tag = (MONITOR_TAG_SUB if sub else 0) | (MONITOR_TAG_FAST if fast else 0)
    return build_frame(CmdGet.Subscribe, bytes([index & 0xFF, tag]))


def ping() -> bytes:
    return build_frame(CmdGet.Ping)

def set_tunable(index: int, type_: int, value: CellValue) -> bytes:
    """写调参项。值固定 8 字节，类型由上位机从目录里查到后传入。"""
    return build_frame(CmdGet.SetTunable, bytes([index & 0xFF]) + encode_value8(type_, value))

def command_send(cmd_index: int, field_types: list[int], values: list[CellValue]) -> bytes:
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


def _dir_len(p: bytes, length: int) -> int:
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


def _cmd_dir_len(p: bytes, length: int) -> int:
    """动作命令目录帧 payload 总长；不够返回 -1。

    帧头 2B 就是"后续内容长度"（小端 uint16），所以直接 2 + 该值，不用遍历字段走查：
    [内容长度2B][命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[bit7=1时:(min,max)×N]
    之所以由下位机给长度：min/max 改成按字段类型定宽后，长度要按各字段 value_size 累加，
    让上位机再实现一遍这套走查纯属折磨（且容易与下位机不一致）。
    """
    if length < 2:
        return -1
    return 2 + int.from_bytes(p[0:2], "little")


def scan_dir_frames(chunk: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """从原始字节流里挑完整的上行目录帧。
    返回 ([(cmd, 整帧含帧头校验)], 已安全消费的前缀长度)；

    用途：Backend 用它从广播给上位机的原始字节里"旁路"摘出目录帧做缓存，
    这样上位机要目录时可以由后端回放，不必每次都打扰下位机。
    """
    out: list[tuple[int, bytes]] = []
    i = 0
    n = len(chunk)
    while i + 4 <= n:
        if chunk[i] != FRAME_HEAD:
            i += 1
            continue
        cmd = chunk[i + 1]
        if cmd == CmdPost.Pong:
            # Pong 固定 3 字节（A5 FF FF），直接跳过不进结果
            i += 3
            continue
        if cmd in (CmdPost.TunableDirectory, CmdPost.MonitorDirectory):
            total = _dir_len(chunk[i + 2 :], n - i - 2)
        elif cmd == CmdPost.CMDDirectory:
            total = _cmd_dir_len(chunk[i + 2 :], n - i - 2)
        else:
            # 非目录帧（监控值推送等）不关心，但也不能当成 A5 误判往前跳一字节了事：
            # 这里只跳 1，靠下一轮重新找 A5，可能把 payload 里的 A5 当帧头，
            # 但随后长度试算/校验会失败，最坏是漏掉帧，不会错位解析出脏数据。
            i += 1
            continue
        if total < 0:
            break  # 帧还没收全，等下一批字节
        end = i + 2 + total + 1
        if end > n:
            break
        frame = chunk[i:end]
        xor = 0
        for b in frame[1:-1]:
            xor ^= b
        if xor == frame[-1]:
            out.append((cmd, bytes(frame)))
        i = end
    return out, i


class FrameParser:
    """与 TS FrameParser 同构的逐字节状态机：喂串口 chunk，吐解析事件（RxEvent）。"""

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

        self.payload = bytearray(MAX_PAYLOAD)
        '''当前 payload 缓冲区'''
        
        # monitor/tunable 每项 value 类型靠各自目录帧登记，收推送时按 index 查。
        # 这是关键：监控值推送帧里只有一串 [index][值]，既没有类型字节也没有项数，
        # 必须先收到目录帧才知道每项该按几个字节、什么类型解（靠宽度推进偏移到 payloadlen 末尾）。
        # 所以目录没拉到之前收到的监控值帧会被解成 0（value_size(0)=1 → UInt8）。
        # 两张表不能合并：index 是各自目录数组的下标（固件里 tunable_items 与
        # monitor_items 是两个独立数组），同号 index 在两张表里是毫不相干的两项、
        # 类型可能不同，合并会让两者互相覆盖。
        self.monitor_type = bytearray(256)
        '''监控值类型缓存'''
        
        self.tunable_type = bytearray(256)
        '''调参值类型缓存'''
        
        self.stats = RxStats()
        '''链路诊断计数'''

    def feed(self, chunk: bytes, on_event: Callable[[RxEvent], None]) -> None:
        """
        喂一批串口字节，逐字节走状态机，解出完整帧就调 on_event。
        chunk   : 本次从串口读到的原始字节（可含半帧/多帧，状态机自己处理粘包拆包）
        on_event: 解析完整帧回调函数
        """
        self.stats.bytes += len(chunk)
        for b in chunk:
            if self.state == _RxState.WaitHead:
                if b == FRAME_HEAD:
                    self.state = _RxState.WaitCmd
            elif self.state == _RxState.WaitCmd:
                if b == CmdPost.Pong:
                    # Pong 无 payload，直接进等校验
                    self.cmd = b
                    self.len = 0
                    self.xor = b
                    self.state = _RxState.WaitXor
                elif b in (
                    CmdPost.TunableDirectory,
                    CmdPost.MonitorDirectory,
                    CmdPost.MonitorValue,
                    CmdPost.TunableEcho,
                    CmdPost.CMDDirectory,
                ):
                    self.cmd = b
                    self.expect = -1
                    self.len = 0
                    self.xor = b
                    self.state = _RxState.WaitData
                else:
                    # 不认识的命令字：丢弃，回找帧头。
                    # 不去"猜"长度是因为无法判定该跳多远，硬跳会造成连锁错位。
                    self.stats.unknownCmd += 1
                    self.state = _RxState.WaitHead
            elif self.state == _RxState.WaitData:
                if self.len >= MAX_PAYLOAD:
                    self.stats.overflow += 1
                    self.state = _RxState.WaitHead
                    continue
                self.payload[self.len] = b
                self.len += 1
                self.xor ^= b

                if self.expect < 0:
                    # 边收边试算长度：收到足够字节能确定总长就定下来。
                    # 目录帧要收完名字长度、单位长度才能算；调参回显收 1 字节 index 查类型表；
                    # 监控值帧自带 1B 长度前缀，收 1 字节就能定长（不依赖先拿到目录）。
                    if self.cmd == CmdPost.MonitorValue:
                        if self.payload[0] > 0:
                            self.expect = 1 + self.payload[0] # len 只算它后面那些字节，加上 len 自身才是总长
                    elif self.cmd == CmdPost.TunableEcho:
                        self.expect = 1 + value_size(self.tunable_type[self.payload[0]])
                    elif self.cmd == CmdPost.CMDDirectory:
                        total = _cmd_dir_len(bytes(self.payload[: self.len]), self.len)
                        if total >= 0:
                            self.expect = total
                    else:
                        total = _dir_len(bytes(self.payload[: self.len]), self.len)
                        if total >= 0:
                            self.expect = total
                if self.expect > 0 and self.len >= self.expect:
                    self.state = _RxState.WaitXor
            elif self.state == _RxState.WaitXor:
                self.state = _RxState.WaitHead
                if (self.xor ^ b) == 0:
                    self.stats.frames += 1
                    self._dispatch(on_event)
                else:
                    self.stats.badChecksum += 1

    def _dispatch(self, on_event: Callable[[RxEvent], None]) -> None:
        """
        一帧校验通过后调用：按命令字把 payload 解成对应 RxEvent 并回调 on_event。
        on_event: 事件出口（SerialBridge 注入 Hub._on_serial_event）
        按 self.cmd 分五种：
            Tunable/MonitorDirectory → DirectoryEvent（顺带登记该项类型到类型表）
            MonitorValue             → ValueEvent
            TunableEcho              → EchoEvent
            CMDDirectory             → CmdDirectoryEvent
            Pong                     → PongEvent
        """
        p = bytes(self.payload[: self.len])
        cmd = self.cmd

        if cmd in (CmdPost.TunableDirectory, CmdPost.MonitorDirectory):
            if self.len < 4:
                return
            index = p[0]
            type_ = p[1]
            tag = p[2]
            name_len = p[3]
            unit_len_at = 4 + name_len
            if self.len < unit_len_at + 1:
                return
            unit_len = p[unit_len_at]
            value_at = 5 + name_len + unit_len
            if self.len < value_at:
                return
            name = p[4:unit_len_at].decode("utf-8", errors="replace")
            unit = p[unit_len_at + 1 : value_at].decode("utf-8", errors="replace")
            value = decode_value(p, value_at, type_) if value_size(type_) else 0
            table = DirTable.tunable if cmd == CmdPost.TunableDirectory else DirTable.monitor
            # 登记类型：后续监控值推送帧靠这个表才知道怎么解
            if table is DirTable.monitor:
                self.monitor_type[index] = type_
            else:
                self.tunable_type[index] = type_
            on_event(
                DirectoryEvent(
                    table=table,
                    item=MenuItem(
                        index=index,
                        type=type_,
                        tag=tag,
                        name=name,
                        unit=unit,
                        value=value,  # 目录帧自带一次当前值，省得再单独取
                    ),
                )
            )
            return

        if cmd == CmdPost.MonitorValue:
            # [payloadlen][time 2B]([index][值])×N：没有项数字段，靠类型表推进偏移走到payload末尾
            if self.len < 3:
                return
            t_ms = p[1] | (p[2] << 8)
            # 先走一趟核对偏移：必须正好落在末尾，对不上说明类型表与发送方不一致，整帧放弃（不产脏事件）
            off = 3
            ok = True
            while off < self.len:
                sz = value_size(self.monitor_type[p[off]])
                if sz == 0:
                    ok = False
                    break
                off += 1 + sz
            if not (ok and off == self.len):
                return
            off = 3
            while off < self.len:
                index = p[off]
                type_ = self.monitor_type[index]
                value = decode_value(p, off + 1, type_)
                on_event(ValueEvent(index=index, value=value, t_ms=t_ms))
                off += 1 + value_size(type_)
            return

        if cmd == CmdPost.TunableEcho:
            if self.len < 1:
                return
            index = p[0]
            type_ = self.tunable_type[index]
            value = decode_value(p, 1, type_) if value_size(type_) else 0
            on_event(EchoEvent(index=index, value=value))
            return

        if cmd == CmdPost.CMDDirectory:
            # payload 头 2B 是内容长度，内容从 off=2 起：[命令字][tag][名长][名][参数量][字段...]
            if self.len < 5:
                return
            off = 2
            index = p[off]
            tag = p[off + 1]
            name_len = p[off + 2]
            name_at = off + 3
            off = name_at + name_len
            if self.len < off + 1:
                return
            name = p[name_at:off].decode("utf-8", errors="replace")
            field_num = p[off]
            off += 1
            fields: list[CmdField] = []
            for _ in range(field_num):
                if self.len < off + 2:
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
                    if sz == 0 or self.len < off + 2 * sz:
                        break  # str/other 无范围；或帧被截断
                    f.min = decode_value(p, off, f.type)
                    f.max = decode_value(p, off + sz, f.type)
                    off += 2 * sz
            on_event(
                CmdDirectoryEvent(
                    item=CmdDirItem(index=index, tag=tag, name=name, fields=fields),
                )
            )
            return

        if cmd == CmdPost.Pong:
            on_event(PongEvent())
            return
