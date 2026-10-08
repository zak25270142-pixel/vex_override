"""
VEX V5 User 口二进制协议（与 frontend/src/services/protocol.ts 及
include/communication.h 严格一一对应）

帧格式: A5 | Cmd | Payload... | XOR
  XOR = Cmd 起逐字节异或（不含帧头）
多字节数值一律小端（V5 ARM 与 PC x86 同为小端）
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, List, Tuple, Union

FRAME_HEAD = 0xA5
MAX_PAYLOAD = 445

# ---------- 命令字 ----------


class CmdPost(IntEnum):
    """主控 → 上位机"""

    TunableDirectory = 0
    MonitorDirectory = 1
    MonitorValue = 2
    TunableEcho = 3
    CMDDirectory = 4
    Pong = 0xFF


class CmdGet(IntEnum):
    """上位机 → 主控"""

    RequestTunable = 0
    RequestMonitor = 1
    Subscribe = 2
    SetTunable = 3
    RequestCommand = 4
    Ping = 0xFF


# ---------- 值类型（编号必须与 my_main.h VALUE_TYPE 一致）----------


class ValueType(IntEnum):
    UInt8 = 0
    UInt16 = 1
    UInt32 = 2
    UInt64 = 3
    Int8 = 4
    Int16 = 5
    Int32 = 6
    Int64 = 7
    Float = 8
    Double = 9
    Str = 10
    Bool = 11
    OnOff = 12
    Color = 13
    Other = 14


# ---------- 监控 tag 位域 ----------

MONITOR_TAG_SUB = 0x80
MONITOR_TAG_FAST = 0x40
MONITOR_TAG_GETTER = 0x20
MONITOR_TAG_KIND_MASK = 0x07


def tag_subscribed(tag: int) -> bool:
    return (tag & MONITOR_TAG_SUB) != 0


def tag_fast(tag: int) -> bool:
    return (tag & MONITOR_TAG_FAST) != 0


def tag_kind(tag: int) -> int:
    return tag & MONITOR_TAG_KIND_MASK


# ---------- 动作命令 tag 位域 ----------

CMD_TAG_RANGE = 0x80
CMD_TAG_ARGC_MASK = 0x70
CMD_TAG_ARGC_SHIFT = 4
CMD_TAG_SPEC_MASK = 0x0F


class CmdSpec(IntEnum):
    Normal = 0
    Stop = 1


def cmd_spec(tag: int) -> int:
    return tag & CMD_TAG_SPEC_MASK


def cmd_argc(tag: int) -> int:
    return (tag & CMD_TAG_ARGC_MASK) >> CMD_TAG_ARGC_SHIFT


def cmd_has_range(tag: int) -> bool:
    return (tag & CMD_TAG_RANGE) != 0


# ---------- 值编解码 ----------

CellValue = Union[int, float, str, bool]


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
    if type_ == ValueType.Int8:
        return struct.unpack_from("<b", data, off)[0]
    if type_ == ValueType.UInt16:
        return struct.unpack_from("<H", data, off)[0]
    if type_ == ValueType.Int16:
        return struct.unpack_from("<h", data, off)[0]
    if type_ == ValueType.UInt32:
        return struct.unpack_from("<I", data, off)[0]
    if type_ == ValueType.Int32:
        return struct.unpack_from("<i", data, off)[0]
    if type_ == ValueType.Float:
        return struct.unpack_from("<f", data, off)[0]
    if type_ == ValueType.Double:
        return struct.unpack_from("<d", data, off)[0]
    if type_ == ValueType.UInt64:
        return struct.unpack_from("<Q", data, off)[0]
    if type_ == ValueType.Int64:
        return struct.unpack_from("<q", data, off)[0]
    if type_ in (ValueType.Bool, ValueType.OnOff):
        return data[off]
    if type_ == ValueType.Color:
        r = data[off]
        g = data[off + 1] if off + 1 < len(data) else 0
        b = data[off + 2] if off + 2 < len(data) else 0
        return f"#{r:02x}{g:02x}{b:02x}"
    return 0


def encode_value(type_: int, value: CellValue) -> bytes:
    """按真实宽度编码（动作命令字段用，紧凑无填充）。"""
    size = value_size(type_)
    if size == 0:
        return b""
    return encode_value8(type_, value)[:size]


def encode_value8(type_: int, value: CellValue) -> bytes:
    """定长 8B 小端（SetTunable 用），不足高位为 0。"""
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
    xor = cmd
    for b in payload:
        xor ^= b
    return bytes([FRAME_HEAD, cmd & 0xFF]) + payload + bytes([xor & 0xFF])


def request_tunable() -> bytes:
    return build_frame(CmdGet.RequestTunable)


def request_monitor() -> bytes:
    return build_frame(CmdGet.RequestMonitor)


def request_command() -> bytes:
    return build_frame(CmdGet.RequestCommand)


def subscribe(index: int, sub: bool, fast: bool) -> bytes:
    tag = (MONITOR_TAG_SUB if sub else 0) | (MONITOR_TAG_FAST if fast else 0)
    return build_frame(CmdGet.Subscribe, bytes([index & 0xFF, tag]))


def ping() -> bytes:
    return build_frame(CmdGet.Ping)


def set_tunable(index: int, type_: int, value: CellValue) -> bytes:
    return build_frame(CmdGet.SetTunable, bytes([index & 0xFF]) + encode_value8(type_, value))


def command_send(cmd_index: int, field_types: List[int], values: List[CellValue]) -> bytes:
    payload = bytearray()
    for t, v in zip(field_types, values):
        payload.extend(encode_value(t, v))
    return build_frame(cmd_index, bytes(payload))


# ---------- 目录数据结构（解析结果用 dict，JSON 直接可序列化）----------


@dataclass
class CmdField:
    type: int
    name: str
    min: Union[float, None] = None
    max: Union[float, None] = None


@dataclass
class CmdDirItem:
    index: int
    tag: int
    name: str
    fields: List[CmdField] = field(default_factory=list)


# ---------- 帧解析状态机 ----------


class _RxState(IntEnum):
    WaitHead = 0
    WaitCmd = 1
    WaitData = 2
    WaitXor = 3


def _dir_len(p: bytes, length: int) -> int:
    """试算调参/监控目录帧 payload 总长；不够返回 -1。"""
    if length < 4:
        return -1
    name_len = p[3]
    unit_len_at = 4 + name_len
    if length < unit_len_at + 1:
        return -1
    return 5 + name_len + p[unit_len_at] + value_size(p[1])


def _cmd_dir_len(p: bytes, length: int) -> int:
    """试算动作命令目录帧 payload 总长；不够返回 -1。
    [命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[bit7=1时:(min,max)×参数量]"""
    if length < 3:
        return -1
    off = 3 + p[2]
    if length < off + 1:
        return -1
    field_num = p[off]
    off += 1
    for _ in range(field_num):
        if length < off + 2:
            return -1
        off += 2 + p[off + 1]
    if p[1] & CMD_TAG_RANGE:
        off += 8 * field_num
    return off


def scan_dir_frames(chunk: bytes) -> Tuple[List[Tuple[int, bytes]], int]:
    """从原始字节流里挑完整的上行目录帧。
    返回 ([(cmd, 整帧含帧头校验)], 已安全消费的前缀长度)；
    半帧/粘包尾巴不算已消费，由调用方保留到下一批再喂。"""
    out: List[Tuple[int, bytes]] = []
    i = 0
    n = len(chunk)
    while i + 4 <= n:
        if chunk[i] != FRAME_HEAD:
            i += 1
            continue
        cmd = chunk[i + 1]
        if cmd == CmdPost.Pong:
            # Pong 固定 3 字节（A5 FF FF）
            i += 3
            continue
        if cmd in (CmdPost.TunableDirectory, CmdPost.MonitorDirectory):
            total = _dir_len(chunk[i + 2 :], n - i - 2)
        elif cmd == CmdPost.CMDDirectory:
            total = _cmd_dir_len(chunk[i + 2 :], n - i - 2)
        else:
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
    """与 TS FrameParser 同构的逐字节状态机：喂串口 chunk，吐解析事件 dict。"""

    def __init__(self) -> None:
        self.state = _RxState.WaitHead
        self.cmd = 0
        self.expect = -1
        self.len = 0
        self.xor = 0
        self.payload = bytearray(MAX_PAYLOAD)
        # monitor/tunable 每项 value 类型靠各自目录帧登记，收推送时按 index 查
        self.monitor_type = bytearray(256)
        self.tunable_type = bytearray(256)
        self.stats = {
            "bytes": 0,
            "frames": 0,
            "badChecksum": 0,
            "unknownCmd": 0,
            "overflow": 0,
        }

    def feed(self, chunk: bytes, on_event: Callable[[dict], None]) -> None:
        self.stats["bytes"] += len(chunk)
        for b in chunk:
            if self.state == _RxState.WaitHead:
                if b == FRAME_HEAD:
                    self.state = _RxState.WaitCmd
            elif self.state == _RxState.WaitCmd:
                if b == CmdPost.Pong:
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
                    self.stats["unknownCmd"] += 1
                    self.state = _RxState.WaitHead
            elif self.state == _RxState.WaitData:
                if self.len >= MAX_PAYLOAD:
                    self.stats["overflow"] += 1
                    self.state = _RxState.WaitHead
                    continue
                self.payload[self.len] = b
                self.len += 1
                self.xor ^= b

                if self.expect < 0:
                    if self.cmd in (CmdPost.MonitorValue, CmdPost.TunableEcho):
                        types = (
                            self.monitor_type
                            if self.cmd == CmdPost.MonitorValue
                            else self.tunable_type
                        )
                        if self.len >= 1:
                            self.expect = 1 + value_size(types[self.payload[0]])
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
                    self.stats["frames"] += 1
                    self._dispatch(on_event)
                else:
                    self.stats["badChecksum"] += 1

    def _dispatch(self, on_event: Callable[[dict], None]) -> None:
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
            table = "tunable" if cmd == CmdPost.TunableDirectory else "monitor"
            if table == "monitor":
                self.monitor_type[index] = type_
            else:
                self.tunable_type[index] = type_
            on_event(
                {
                    "kind": "directory",
                    "table": table,
                    "item": {
                        "index": index,
                        "type": type_,
                        "tag": tag,
                        "name": name,
                        "unit": unit,
                        "value": value,
                    },
                }
            )
            return

        if cmd in (CmdPost.MonitorValue, CmdPost.TunableEcho):
            if self.len < 1:
                return
            index = p[0]
            types = self.monitor_type if cmd == CmdPost.MonitorValue else self.tunable_type
            type_ = types[index]
            value = decode_value(p, 1, type_) if value_size(type_) else 0
            on_event(
                {
                    "kind": "value" if cmd == CmdPost.MonitorValue else "echo",
                    "index": index,
                    "value": value,
                }
            )
            return

        if cmd == CmdPost.CMDDirectory:
            if self.len < 3:
                return
            index = p[0]
            tag = p[1]
            name_len = p[2]
            off = 3 + name_len
            if self.len < off + 1:
                return
            name = p[3:off].decode("utf-8", errors="replace")
            field_num = p[off]
            off += 1
            fields: List[dict] = []
            for _ in range(field_num):
                if self.len < off + 2:
                    return
                ftype = p[off]
                fname_len = p[off + 1]
                off += 2
                fname = p[off : off + fname_len].decode("utf-8", errors="replace")
                off += fname_len
                fields.append({"type": ftype, "name": fname})
            if tag & CMD_TAG_RANGE:
                for i in range(field_num):
                    if self.len < off + 8:
                        break
                    mn, mx = struct.unpack_from("<ff", p, off)
                    fields[i]["min"] = mn
                    fields[i]["max"] = mx
                    off += 8
            on_event(
                {
                    "kind": "cmdDirectory",
                    "item": {
                        "index": index,
                        "tag": tag,
                        "name": name,
                        "fields": fields,
                    },
                }
            )
            return

        if cmd == CmdPost.Pong:
            on_event({"kind": "pong"})
            return
