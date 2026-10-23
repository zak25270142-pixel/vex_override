"""协议层自测（无需串口）。运行: python -m debug.test_protocol

parser 是纯解帧器：只测帧边界（长度试算/校验/重同步），
业务语义（目录解析/值解码）的测试在 debug.test_hub_proxy。
"""

from __future__ import annotations

import struct

from app.protocol import (
    FRAME_HEAD,
    CmdGet,
    CmdPost,
    MAX_PAYLOAD,
    ValueType,
    FrameParser,
    build_response,
    decode_response,
    decode_value,
    encode_value,
    is_ack_tracked,
    ping,
    request_monitor,
    resp_is_ok,
    resp_is_reject,
    resp_is_retry,
    resp_reason_text,
    resp_str_len,
    subscribe,
)


def _rec(frames: list):
    """收集 (cmd, payload) 的回调。"""
    return lambda cmd, payload: frames.append((cmd, payload))


def _frame(cmd: int, payload: bytes) -> bytes:
    xor = cmd
    for b in payload:
        xor ^= b
    return bytes([FRAME_HEAD, cmd]) + payload + bytes([xor & 0xFF])


def test_build_ping():
    f = ping()
    assert list(f) == [0xA5, 0xFF, 0xFF]


def test_subscribe_frame():
    f = subscribe(3, True, True)
    assert f[1] == CmdGet.Subscribe and f[2] == 3 and f[3] == 0xC0  # SUB|FAST
    assert f[4] == (f[1] ^ f[2] ^ f[3])


def test_encode_float():
    b = encode_value(ValueType.Float, 1.5)
    assert len(b) == 4
    assert abs(decode_value(b, 0, ValueType.Float) - 1.5) < 1e-6


def test_encode_rejects_non_numeric():
    """数值字段收到字符串/None/列表一律抛错，绝不静默编 0。"""
    for bad in ("6.0", None, [1], {"a": 1}):
        try:
            encode_value(ValueType.Float, bad)
            raise AssertionError(f"应拒绝 {bad!r}")
        except TypeError:
            pass
    # bool 当电压/转速传也是笔误，仅开关字段放行
    try:
        encode_value(ValueType.Float, True)
        raise AssertionError("bool 应被数值字段拒绝")
    except TypeError:
        pass
    assert encode_value(ValueType.Bool, True)[0] == 1
    assert encode_value(ValueType.Bool, False)[0] == 0


def test_encode_color():
    assert encode_value(ValueType.Color, "#ff8000")[:3] == bytes([0xFF, 0x80, 0x00])
    assert encode_value(ValueType.Color, "ff8000")[:3] == bytes([0xFF, 0x80, 0x00])  # 不带 # 也认
    for bad in (0xFF8000, "#ff80", "#gg8000", None):
        try:
            encode_value(ValueType.Color, bad)
            raise AssertionError(f"应拒绝 {bad!r}")
        except (TypeError, ValueError):
            pass


def test_parser_pong():
    frames: list[tuple[int, bytes]] = []
    FrameParser().feed(bytes([0xA5, 0xFF, 0xFF]), _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames


def test_parser_monitor_value_zero_len_resyncs():
    """payloadlen==0 非法（固件永不发）：应直接回找帧头，不吞后续字节。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    # 错位场景：A5 02 00 之后粘一个正常 Pong，应立即重同步并解出 Pong
    p.feed(bytes([0xA5, CmdPost.MonitorValue, 0x00]) + ping(), _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames


def _monitor_value_frame(index: int, value: float, t_ms: int = 0x1234) -> bytes:
    """监控值帧: [len 1B][time 2B 小端][index 1B][值 4B float]"""
    body = struct.pack("<H", t_ms & 0xFFFF) + bytes([index]) + struct.pack("<f", value)
    return _frame(CmdPost.MonitorValue, bytes([len(body)]) + body)


def test_parser_monitor_value_framing():
    """1B 长度前缀定长：多帧粘包、半帧分喂，帧边界都不偏（无需目录先行）。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    f = _monitor_value_frame(2, 1.0)
    want = (CmdPost.MonitorValue, f[2:-1])
    p.feed(f + f, _rec(frames))  # 两帧粘一起
    assert frames == [want, want], frames
    frames.clear()
    p.feed(f[:5], _rec(frames))  # 半帧
    assert frames == []
    p.feed(f[5:], _rec(frames))
    assert frames == [want], frames


def _echo_frame(type_: int, index: int, value: float) -> bytes:
    """调参回显帧: [type 1B][index 1B][值 4B float]——type 前缀自带宽度"""
    return _frame(CmdPost.TunableEcho, bytes([type_, index]) + struct.pack("<f", value))


def test_parser_echo_type_prefix():
    """回显帧 [type][index][value]：收 1 字节即定长，解帧不查目录。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    f = _echo_frame(ValueType.Float, 7, 1.5)
    p.feed(f + ping(), _rec(frames))
    assert frames == [(CmdPost.TunableEcho, f[2:-1]), (CmdPost.Pong, b"")], frames


def test_parser_echo_bad_type_resyncs():
    """type 字节不是合法值类型（无宽度）：当错误帧重找 A5，不吞后续字节。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    bad = _frame(CmdPost.TunableEcho, bytes([10, 7]) + b"\x00\x00\x00\x00")  # type=10 无宽度
    p.feed(bad + ping(), _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames


def _monitor_dir_frame(index: int, name: str, unit: str, value: float) -> bytes:
    """监控目录帧: [index][type][tag][名长][名][单位长][单位][值]"""
    nb = name.encode("utf-8")
    ub = unit.encode("utf-8")
    payload = (
        bytes([index, ValueType.Float, 0, len(nb)]) + nb + bytes([len(ub)]) + ub
        + struct.pack("<f", value)
    )
    return _frame(CmdPost.MonitorDirectory, payload)


def test_parser_directory_framing():
    """目录帧变长：_dir_len 边收边试算，半帧跨 chunk 拼齐后边界不偏。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    f = _monitor_dir_frame(2, "左0转速", "rpm", 0.0)
    p.feed(f[:6], _rec(frames))  # 名字都没收全：定不了长
    assert frames == []
    p.feed(f[6:] + ping(), _rec(frames))
    assert frames == [(CmdPost.MonitorDirectory, f[2:-1]), (CmdPost.Pong, b"")], frames


def test_parser_cmd_directory_framing():
    """命令目录头 2B 即内容长度：收够 2B 定长，无需遍历字段走查。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    tag = 0x80 | (1 << 4)  # bit7 范围 + argc=1
    content = (
        bytes([0x80, tag, 2]) + b"mk" + bytes([1, ValueType.Float, 1]) + b"a"
        + struct.pack("<ff", -10.0, 10.0)
    )
    f = _frame(CmdPost.CMDDirectory, len(content).to_bytes(2, "little") + content)
    p.feed(f[:4], _rec(frames))
    assert frames == []
    p.feed(f[4:], _rec(frames))
    assert frames == [(CmdPost.CMDDirectory, f[2:-1])], frames


def test_parser_unknown_cmd_resyncs():
    """不认识的命令字：回找帧头，不猜长度硬跳（后续正常帧不受影响）。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    p.feed(bytes([FRAME_HEAD, 0x07, 0x00]) + ping(), _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames


def test_parser_overflow_resyncs():
    """payload 超过 MAX_PAYLOAD：回找帧头，不硬吞（后续正常帧不受影响）。"""
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    # 目录帧试算出 519B 总长（>445）：名长/单位长都顶格
    payload = bytes([0, ValueType.Float, 0, 255]) + b"n" * 255 + bytes([255]) + b"u" * 255
    payload += bytes(200)  # 继续灌，逼 len 撞 445
    p.feed(bytes([FRAME_HEAD, CmdPost.MonitorDirectory]) + payload + ping(), _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames


def test_parser_bad_checksum_and_half_frame():
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    good = ping()
    p.feed(bytes([0xA5, 0xFF, 0x00]), _rec(frames))  # 校验错
    assert not frames
    # 半帧分两次喂也能拼起来
    p.feed(good[:1], _rec(frames))
    p.feed(good[1:], _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames


def test_max_payload_constant():
    assert MAX_PAYLOAD == 445


# ---------- Response 帧（通用响应：对下行命令的回执） ----------


def test_build_response_roundtrip():
    """组帧→解帧往返：payloadlen/回显/status/原因串都对得上。"""
    echo = bytes([0x82, 1, 2])  # 动作命令字 + 2B 回显payload
    f = build_response(0x82, echo, 0x00)
    assert f[0] == 0xA5 and f[1] == CmdPost.Response
    assert f[2] == 5, "payloadlen = 原话cmd1B + 回显3B + status1B"
    frames: list[tuple[int, bytes]] = []
    FrameParser().feed(f, _rec(frames))
    assert frames == [(CmdPost.Response, f[2:-1])], frames
    assert decode_response(f[2:-1], 3) == (0x82, echo, 0x00, "")


def test_parser_response_framing():
    """长度前缀定长：粘包/半帧边界不偏。"""
    f = build_response(0x82, bytes([1, 2]), 0x00)
    want = (CmdPost.Response, f[2:-1])
    frames: list[tuple[int, bytes]] = []
    p = FrameParser()
    p.feed(f + f, _rec(frames))
    assert frames == [want, want], frames
    frames.clear()
    p.feed(f[:3], _rec(frames))
    assert frames == []
    p.feed(f[3:], _rec(frames))
    assert frames == [want], frames


def test_parser_response_short_len_resyncs():
    """payloadlen<2（0/1）= 流错位：回找帧头，不吞后续字节（同监控值帧 payloadlen==0 规则）。"""
    frames: list[tuple[int, bytes]] = []
    FrameParser().feed(bytes([0xA5, CmdPost.Response, 0x00]) + ping(), _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames
    frames.clear()
    FrameParser().feed(bytes([0xA5, CmdPost.Response, 0x01, 0x82]) + ping(), _rec(frames))
    assert frames == [(CmdPost.Pong, b"")], frames


def test_decode_response_consistency():
    """一致性校验：payloadlen 必须 = 2 + 回显长 + 串长；对不上返回 None（不猜）。"""
    echo = bytes([1, 2, 3])
    f = build_response(0x82, echo, 0x00)
    assert decode_response(f[2:-1], 3) == (0x82, echo, 0x00, "")
    assert decode_response(f[2:-1], 2) is None, "回显长度猜错：status 落错位置"
    assert decode_response(f[2:-1], 4) is None
    # 串模式：长度取低6位（"忙" 3B UTF-8 → status 0x83 = 拒绝+串长3）
    f2 = build_response(0x82, echo, 0x83, "忙")
    assert decode_response(f2[2:-1], 3) == (0x82, echo, 0x83, "忙")
    # 内定err模式：不带原因串（0xC3 = 拒绝·忙）
    f3 = build_response(0x82, echo, 0xC3)
    assert decode_response(f3[2:-1], 3) == (0x82, echo, 0xC3, "")


def test_response_status_helpers():
    """status 位域 helper 与内定err中文名（与固件 communication.h 同口径）。"""
    assert resp_is_ok(0x00) and not resp_is_ok(0x01)
    assert resp_is_retry(0x01) and resp_is_retry(0x41) and not resp_is_retry(0x00)
    assert resp_is_reject(0x80) and resp_is_reject(0xC3) and not resp_is_reject(0x40)
    assert resp_str_len(0x00) == 0 and resp_str_len(0x83) == 3 and resp_str_len(0xC3) == 0
    assert resp_reason_text(0x41, "") == "Checksum(内定err#1)"
    assert resp_reason_text(0xC3, "") == "Busy(内定err#3)"
    assert resp_reason_text(0x83, "下标超表") == "下标超表"


def test_is_ack_tracked():
    """跟踪面：目录请求/订阅/调参写入 + 动作命令段；Ping 不跟踪（Pong 即确认）。"""
    for c in (CmdGet.RequestTunable, CmdGet.RequestMonitor, CmdGet.RequestCommand,
              CmdGet.Subscribe, CmdGet.SetTunable, 0x80, 0x9F):
        assert is_ack_tracked(c), hex(c)
    for c in (CmdGet.Ping, 0x7F, 0xA0):
        assert not is_ack_tracked(c), hex(c)


def test_request_monitor():
    f = request_monitor()
    assert f[0] == 0xA5 and f[1] == 0x01 and f[2] == 0x01


if __name__ == "__main__":
    test_build_ping()
    test_subscribe_frame()
    test_encode_float()
    test_encode_rejects_non_numeric()
    test_encode_color()
    test_parser_pong()
    test_parser_monitor_value_zero_len_resyncs()
    test_parser_monitor_value_framing()
    test_parser_echo_type_prefix()
    test_parser_echo_bad_type_resyncs()
    test_parser_directory_framing()
    test_parser_cmd_directory_framing()
    test_parser_unknown_cmd_resyncs()
    test_parser_overflow_resyncs()
    test_parser_bad_checksum_and_half_frame()
    test_max_payload_constant()
    test_build_response_roundtrip()
    test_parser_response_framing()
    test_parser_response_short_len_resyncs()
    test_decode_response_consistency()
    test_response_status_helpers()
    test_is_ack_tracked()
    test_request_monitor()
    print("all protocol tests passed")
