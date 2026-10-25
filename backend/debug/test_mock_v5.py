"""
Mock V5 演示源自测（不起服务、不连真串口）：
  - connect 即连上（无探测），10ms 拍线程起跑
  - 三张目录请求 → burst 逐帧下发 + 末批 OK 尾字节带表内总项数
  - Ping → Pong；Subscribe → OK 回执；Set_Tunable → TunableEcho
  - 动作命令 → Response（停车 OK；原地转向永远 BUSY 拒绝）
  - 坏帧（XOR 错）静默丢弃；停 Ping 300ms 后停推（link 巡视）
  - 父类心跳线程自ping 自答：Pong 流通、不误判掉线
  - on_raw 广播与 on_frame 解码同源同量（WS 透传路径）
运行: python -m debug.test_mock_v5
"""

from __future__ import annotations

import time

from app.mock_v5 import MockV5Bridge
from app.protocol import (
    CmdGet,
    CmdPost,
    ValueType,
    command_send,
    decode_response,
    ping,
    request_command,
    request_monitor,
    request_tunable,
    set_tunable,
    subscribe,
)
from app.serial_bridge import Status

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
    frames: list[tuple[int, bytes]] = []  # on_frame 解出的 (cmd, payload)
    raws: list[bytes] = []  # on_raw 广播的原始字节（WS 透传路径）
    statuses: list[tuple[Status, str]] = []

    def on_frame(cmd: int, payload: bytes) -> None:
        frames.append((cmd, bytes(payload)))

    def on_raw(data: bytes) -> None:
        raws.append(bytes(data))

    def on_status(s: Status, msg: str) -> None:
        statuses.append((s, msg))

    bridge = MockV5Bridge(on_frame=on_frame, on_status=on_status, on_raw=on_raw)

    # ① 端口枚举报假口
    ports = MockV5Bridge.list_ports()
    check("list_ports 假口", len(ports) == 1 and ports[0].device == "MOCK-V5", str(ports))

    # ② connect 即连上（无探测），status=connected
    check("connect 返回 True", bridge.connect() is True)
    check("connected 属性", bridge.connected is True)
    check("上报 connected", any(s == Status.connected for s, _ in statuses))

    # ③ 拉三张表：burst 逐帧 + 末批 OK 尾字节带总项数
    bridge.send(request_tunable())
    bridge.send(request_monitor())
    bridge.send(request_command())
    time.sleep(0.4)  # 28 项 × 10ms 拍 ≈ 0.3s 发完

    tun = [p for c, p in frames if c == CmdPost.TunableDirectory]
    mon = [p for c, p in frames if c == CmdPost.MonitorDirectory]
    cmd = [p for c, p in frames if c == CmdPost.CMDDirectory]
    check("调参目录 9 项", len(tun) == 9, f"got {len(tun)}")
    check("监控目录 14 项", len(mon) == 14, f"got {len(mon)}")
    check("命令目录 5 项", len(cmd) == 5, f"got {len(cmd)}")

    # 目录完成回执：OK + 1B 总项数（回显 = 全量请求的 [len=0]）
    ok_infos = []
    for c, p in frames:
        if c != CmdPost.Response:
            continue
        dec = decode_response(p, 1)  # 目录请求回显 1B
        if dec and dec[2] == 0x00 and len(dec[3]) == 1:
            ok_infos.append(dec[3][0])
    check("三张表各有 OK 项数回执", sorted(ok_infos) == [5, 9, 14], f"got {sorted(ok_infos)}")

    # str 目录项：值宽 0，帧到单位段即止（[index][type][tag][名长][名][单位长][单位]）
    str_items = [p for p in tun if p[1] == 10]  # str 类型编号 10
    check("str 目录项存在", len(str_items) == 1)
    if str_items:
        p = str_items[0]
        name_len = p[3]
        unit_at = 4 + name_len
        check("str 帧到单位段即止", len(p) == unit_at + 1 + p[unit_at], f"len={len(p)}")

    # ④ Ping → Pong
    n_pong = sum(1 for c, _ in frames if c == CmdPost.Pong)
    bridge.send(ping())
    time.sleep(0.05)
    check("Ping → Pong", sum(1 for c, _ in frames if c == CmdPost.Pong) == n_pong + 1)

    # ⑤ Subscribe → OK 回执（回显 [index][tag]）
    bridge.send(subscribe(0, True, True))
    time.sleep(0.05)
    sub_ok = False
    for c, p in frames:
        if c == CmdPost.Response:
            dec = decode_response(p, 2)
            if dec and dec[0] == CmdGet.Subscribe and dec[2] == 0x00:
                sub_ok = True
    check("Subscribe → OK 回执", sub_ok)

    # ⑥ Set_Tunable → TunableEcho（真实生效值）
    bridge.send(set_tunable(0, ValueType.Float, 2.5))
    time.sleep(0.05)
    echo = [p for c, p in frames if c == CmdPost.TunableEcho]
    check("调参回显一帧", len(echo) == 1, f"got {len(echo)}")
    if echo:
        p = echo[0]
        check("回显 type/index", p[0] == ValueType.Float and p[1] == 0)
        import struct

        check("回显值 2.5", struct.unpack_from("<f", p, 2)[0] == 2.5)

    # ⑦ 动作命令：停车 OK；原地转向永远 BUSY 拒绝
    bridge.send(command_send(0x84, [], []))  # 停车（无参）
    bridge.send(command_send(0x83, [ValueType.Float], [90.0]))  # 原地转向
    time.sleep(0.1)
    stop_ok = busy = False
    for c, p in frames:
        if c != CmdPost.Response:
            continue
        dec = decode_response(p, 4 if p[1] == 0x83 else 0)
        if not dec:
            continue
        if dec[0] == 0x84 and dec[2] == 0x00:
            stop_ok = True
        if dec[0] == 0x83 and dec[2] == 0xC3:
            busy = True
    check("停车 → OK", stop_ok)
    check("原地转向 → BUSY(0xC3)", busy)

    # ⑧ 坏帧静默丢弃（XOR 改坏一个字节，不应产生 Pong）
    n_pong = sum(1 for c, _ in frames if c == CmdPost.Pong)
    bad = bytearray(ping())
    bad[-1] ^= 0xFF
    bridge.send(bytes(bad))
    time.sleep(0.05)
    check("坏帧无 Pong", sum(1 for c, _ in frames if c == CmdPost.Pong) == n_pong)

    # ⑨ on_raw 广播与 on_frame 同源同量（WS 透传路径）
    check("on_raw 覆盖全部帧", len(raws) >= len(frames), f"raw={len(raws)} frames={len(frames)}")

    # ⑩ 值帧在流（FAST 订阅项 100Hz）
    n_val = sum(1 for c, _ in frames if c == CmdPost.MonitorValue)
    check("监控值帧在流", n_val > 20, f"got {n_val}")

    # ⑪ 父类心跳线程自 ping 自答：500ms 内 Pong 持续、不误判掉线
    bridge.start_heartbeat(0.08)
    time.sleep(0.5)
    pong_now = sum(1 for c, _ in frames if c == CmdPost.Pong)
    check("心跳期间 Pong 持续", pong_now > n_pong + 3, f"delta={pong_now - n_pong}")
    check("心跳期间未掉线", all(s != Status.disconnected for s, _ in statuses))

    # ⑫ 停心跳 + 不再手发 Ping：300ms 巡视后停推
    bridge.stop_heartbeat()
    time.sleep(0.45)  # 先过巡视期，让 link_ok 落闸
    n_val2 = sum(1 for c, _ in frames if c == CmdPost.MonitorValue)
    time.sleep(0.3)
    n_val3 = sum(1 for c, _ in frames if c == CmdPost.MonitorValue)
    check("停 Ping 后监控值停推", n_val3 == n_val2, f"{n_val2} → {n_val3}")

    # ⑬ Ping 恢复即续传
    bridge.send(ping())
    time.sleep(0.1)
    check("Ping 恢复后续传", sum(1 for c, _ in frames if c == CmdPost.MonitorValue) > n_val3)

    # ⑭ disconnect：报 idle，拍线程退出
    bridge.disconnect()
    check("disconnect 后 connected=False", bridge.connected is False)
    check("上报 idle", any(s == Status.idle for s, _ in statuses))
    time.sleep(0.05)
    check("拍线程已退出", bridge._tick_thread is None or not bridge._tick_thread.is_alive())

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
