"""协议层自测（无需串口）。运行: python -m app.test_protocol"""

from __future__ import annotations

import struct

from .protocol import (
    FRAME_HEAD,
    CmdGet,
    CmdPost,
    MAX_PAYLOAD,
    ValueType,
    build_frame,
    decode_value,
    encode_value,
    ping,
    subscribe,
    FrameParser,
    request_monitor,
)


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


def test_parser_pong():
    events = []
    FrameParser().feed(bytes([0xA5, 0xFF, 0xFF]), events.append)
    assert len(events) == 1 and events[0]["kind"] == "pong"


def test_parser_monitor_value_after_directory():
    """喂一条监控目录登记类型，再喂推送值，验证按目录类型解码。"""
    events = []
    p = FrameParser()
    # 目录 payload: [index=2][type=float][tag=0][名长1 'v'][单位长0][float初值0]
    dir_payload = bytes([2, ValueType.Float, 0, 1]) + b"v" + bytes([0]) + struct.pack("<f", 0.0)
    p.feed(_frame(CmdPost.MonitorDirectory, dir_payload), events.append)
    assert events[-1]["item"]["name"] == "v"

    val_payload = bytes([2]) + struct.pack("<f", 1.0)
    p.feed(_frame(CmdPost.MonitorValue, val_payload), events.append)
    assert events[-1]["kind"] == "value"
    assert events[-1]["index"] == 2
    assert abs(events[-1]["value"] - 1.0) < 1e-6


def test_parser_cmd_directory_with_ranges():
    """命令目录带 bit7 范围：[cmd][tag][名长][名][参数量][字段...][min,max]"""
    events = []
    p = FrameParser()
    tag = 0x80 | (1 << 4)  # bit7 范围 + argc=1 + spec=0
    payload = (
        bytes([0x80, tag, 2]) + b"mk" + bytes([1, ValueType.Float, 1]) + b"a"
        + struct.pack("<ff", -10.0, 10.0)
    )
    p.feed(_frame(CmdPost.CMDDirectory, payload), events.append)
    item = events[-1]["item"]
    assert item["index"] == 0x80 and item["name"] == "mk"
    assert item["fields"][0]["min"] == -10.0 and item["fields"][0]["max"] == 10.0


def test_parser_bad_checksum_and_half_frame():
    events = []
    p = FrameParser()
    good = ping()
    p.feed(bytes([0xA5, 0xFF, 0x00]), events.append)  # 校验错
    assert not events and p.stats["badChecksum"] == 1
    # 半帧分两次喂也能拼起来
    p.feed(good[:1], events.append)
    p.feed(good[1:], events.append)
    assert events and events[0]["kind"] == "pong"


def test_max_payload_constant():
    assert MAX_PAYLOAD == 445


def test_request_monitor():
    f = request_monitor()
    assert f[0] == 0xA5 and f[1] == 0x01 and f[2] == 0x01


if __name__ == "__main__":
    test_build_ping()
    test_subscribe_frame()
    test_encode_float()
    test_parser_pong()
    test_parser_monitor_value_after_directory()
    test_parser_cmd_directory_with_ranges()
    test_parser_bad_checksum_and_half_frame()
    test_max_payload_constant()
    test_request_monitor()
    print("all protocol tests passed")
