"""Mock V5 内核自测（不连服务、不起 WS）：四张目录、回执、Ping/Pong、坏帧。

运行: python -m debug.test_mock_v5
"""

from __future__ import annotations

import struct

from app.protocol import (
    CmdGet,
    CmdPost,
    ValueType,
    command_send,
    decode_response,
    ping,
    request_command,
    request_key_table,
    request_monitor,
    request_tunable,
    set_tunable,
    subscribe,
)
from debug.mock_v5 import _MockV5Core

passed = 0
failed = 0
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        failures.append(f"{name}" + (f" —— {detail}" if detail else ""))


def main() -> None:
    frames: list[tuple[int, bytes]] = []  # 内核发出的 (cmd, payload)
    core = _MockV5Core(lambda f: frames.append((f[1], f[2:-1])))
    tick = lambda n: [core.tick() for _ in range(n)]  # noqa: E731

    # ① 四张目录：全量请求 → 快照 + 末批 OK 尾字节带表内总项数
    core.receive(request_tunable())
    core.receive(request_monitor())
    core.receive(request_command())
    core.receive(request_key_table())
    tick(60)

    tun = [p for c, p in frames if c == CmdPost.TunableDirectory]
    mon = [p for c, p in frames if c == CmdPost.MonitorDirectory]
    cmd = [p for c, p in frames if c == CmdPost.CMDDirectory]
    key = [p for c, p in frames if c == CmdPost.KeyDirectory]
    check("调参目录 8 项", len(tun) == 8, f"got {len(tun)}")
    check("监控目录 15 项", len(mon) == 15, f"got {len(mon)}")
    check("命令目录 5 项", len(cmd) == 5, f"got {len(cmd)}")
    check("按键目录 7 项", len(key) == 7, f"got {len(key)}")

    ok_infos = []
    for c, p in frames:
        if c != CmdPost.Response:
            continue
        dec = decode_response(p)  # 整表请求收到即回：status=0x80 + 1B 表项数
        if dec and dec[2] == 0x80 and len(dec[3]) == 1:
            ok_infos.append(dec[3][0])
    check("四张表各有 OK 项数回执", sorted(ok_infos) == [5, 7, 8, 15], f"got {sorted(ok_infos)}")

    # ② Ping → Pong
    n_pong = sum(1 for c, _ in frames if c == CmdPost.Pong)
    core.receive(ping())
    tick(1)
    check("Ping → Pong", sum(1 for c, _ in frames if c == CmdPost.Pong) == n_pong + 1)

    # ③ Subscribe → OK 回执（回显 [index][tag]），随后 FAST 项每拍推值
    core.receive(subscribe(0, True, True))
    tick(10)
    sub_ok = any(
        (dec := decode_response(p)) and dec[0] == CmdGet.Subscribe and dec[2] == 0x00
        for c, p in frames
        if c == CmdPost.Response
    )
    check("Subscribe → OK 回执", sub_ok)
    check("监控值帧在流", sum(1 for c, _ in frames if c == CmdPost.MonitorValue) > 8)

    # ④ Set_Tunable → TunableEcho（真实生效值）
    n_echo = sum(1 for c, _ in frames if c == CmdPost.TunableEcho)
    core.receive(set_tunable(0, ValueType.Float, 2.5))
    tick(1)
    echo = [p for c, p in frames if c == CmdPost.TunableEcho][n_echo:]
    check("调参回显一帧", len(echo) == 1, f"got {len(echo)}")
    if echo:
        p = echo[0]
        check("回显 type/index", p[0] == ValueType.Float and p[1] == 0)
        check("回显值 2.5", struct.unpack_from("<f", p, 2)[0] == 2.5)

    # ⑤ 动作命令：停车 OK；原地转向永远 BUSY 拒绝
    core.receive(command_send(0x84, [], []))
    core.receive(command_send(0x83, [ValueType.Float], [90.0]))
    tick(1)
    stop_ok = busy = False
    for c, p in frames:
        if c != CmdPost.Response:
            continue
        dec = decode_response(p)
        if not dec:
            continue
        if dec[0] == 0x84 and dec[2] == 0x00:
            stop_ok = True
        if dec[0] == 0x83 and dec[2] == 0xC2:
            busy = True
    check("停车 → OK", stop_ok)
    check("原地转向 → BUSY(0xC2)", busy)

    # ⑥ 坏帧静默丢弃（XOR 改坏一个字节，不应产生 Pong）
    n_pong = sum(1 for c, _ in frames if c == CmdPost.Pong)
    bad = bytearray(ping())
    bad[-1] ^= 0xFF
    core.receive(bytes(bad))
    tick(1)
    check("坏帧无 Pong", sum(1 for c, _ in frames if c == CmdPost.Pong) == n_pong)

    print("---- 结果 ----")
    print(f"通过 {passed} / {passed + failed}")
    if failures:
        print(f"失败 {failed} 项：")
        for f in failures:
            print("  ✗ " + f)
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
