"""
Hub 代理层进程内自测（不连真车）：
  - Ping 拦截回 Pong，不转发下位机
  - 目录缓存回放：上行 burst 扫出原始帧 → UI 请求收到同样的帧
  - Subscribe 聚合：UI+脚本并集 / fast 最高档 / 客户端断开清理
  - cmd 参数个数严格校验
运行：python -m app.test_hub_proxy
"""

from __future__ import annotations

import asyncio
import struct

from .hub import Hub
from .protocol import (
    CmdPost,
    build_frame,
    encode_value,
    ping,
    request_monitor,
    subscribe,
)


class FakeWS:
    """最小 WebSocket 桩：收集 send_bytes / send_json。"""

    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.sent_json: list[dict] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(bytes(data))

    async def send_json(self, obj: dict) -> None:
        self.sent_json.append(obj)


def make_monitor_dir_frame(index: int, name: str, unit: str, value: float) -> bytes:
    """造一条合法监控目录帧：Float(8) 类型。"""
    nb = name.encode("utf-8")
    ub = unit.encode("utf-8")
    payload = bytes([index, 8, 0, len(nb)]) + nb + bytes([len(ub)]) + ub + encode_value(8, value)
    return build_frame(CmdPost.MonitorDirectory, payload)


def drain_tx(hub: Hub) -> list[bytes]:
    out = []
    while hub._tx_q:
        out.append(hub._tx_q.popleft()[2])
    return out


def main() -> None:
    hub = Hub(port="__none__")
    hub._running = True
    hub.set_loop(asyncio.new_event_loop())

    # --- 造目录缓存：模拟下位机 burst 上行 ---
    f0 = make_monitor_dir_frame(0, "x", "m", 1.5)
    f1 = make_monitor_dir_frame(1, "yaw", "deg", 90.0)
    hub._on_serial_raw(f0 + f1)
    assert len(hub._dir_cache.get(CmdPost.MonitorDirectory, {})) == 2, "目录缓存应收到 2 帧"
    print("[ok] 目录 burst 缓存")

    # --- 1. Ping 拦截：from_ui 收到 Ping 应回 Pong，且不进入下行队列 ---
    ws = FakeWS()
    hub.from_ui(ping(), ws)  # type: ignore[arg-type]
    # run_coroutine_threadsafe 排进 loop，得让 loop 跑一拍
    hub._loop.run_until_complete(asyncio.sleep(0.01))
    assert ws.sent_bytes and ws.sent_bytes[0] == ping(), "Ping 应回 Pong"
    assert not hub._tx_q, "Ping 不应转发下位机"
    print("[ok] Ping 拦截回 Pong")

    # --- 2. 目录回放：UI 请求监控目录，应只收到缓存的两帧 ---
    ws.sent_bytes.clear()
    hub.from_ui(request_monitor(), ws)  # type: ignore[arg-type]
    hub._loop.run_until_complete(asyncio.sleep(0.01))
    got = ws.sent_bytes
    assert got == [f0, f1], f"目录回放帧不一致: {[g.hex() for g in got]}"
    assert not hub._tx_q, "缓存命中时不应转发下位机"
    print("[ok] 目录缓存回放")

    # --- 2.1 同 index 再来一帧应覆盖不叠加，回放按 index 排序 ---
    f0b = make_monitor_dir_frame(0, "x", "m", 2.5)
    hub._on_serial_raw(f0b)
    assert len(hub._dir_cache[CmdPost.MonitorDirectory]) == 2, "同 index 应覆盖不叠加"
    ws.sent_bytes.clear()
    hub.from_ui(request_monitor(), ws)  # type: ignore[arg-type]
    hub._loop.run_until_complete(asyncio.sleep(0.01))
    assert ws.sent_bytes == [f0b, f1], "回放应按 index 排序且用新帧"
    hub._on_serial_raw(f0)  # 还原成 f0，方便后续用例
    print("[ok] 同 index 覆盖 + 按 index 排序回放")

    # --- 2.2 半帧跨 chunk：先到一半再补齐，缓存仍应完整 ---
    hub._dir_cache.clear()
    hub._dir_buf.clear()
    hub._on_serial_raw(f0[:3])  # A5|Cmd|index 半帧
    assert CmdPost.MonitorDirectory not in hub._dir_cache, "半帧不应入缓存"
    hub._on_serial_raw(f0[3:] + f1)
    assert hub._dir_frames_list(CmdPost.MonitorDirectory) == [f0, f1], "跨 chunk 应拼齐入缓存"
    print("[ok] 半帧跨 chunk 拼接")

    # --- 2.3 主动拉表应先清该表缓存，避免新旧混叠 ---
    hub._request_dir(CmdPost.MonitorDirectory, request_monitor())
    assert CmdPost.MonitorDirectory not in hub._dir_cache, "主动拉表应先清缓存"
    assert drain_tx(hub) == [request_monitor()], "拉表请求应入队"
    print("[ok] 拉表前清缓存")

    # --- 3. Subscribe 聚合：UI 低速 + 脚本高速 → 有效高速；UI 断开 → 保留脚本意图 ---
    drain_tx(hub)
    hub.from_ui(subscribe(3, True, False), ws)  # type: ignore[arg-type]  UI 低速订阅 idx=3
    sent = drain_tx(hub)
    assert sent == [subscribe(3, True, False)], "首个订阅应直接下发"

    hub.sub(3, "push", on=True, fast=True)  # 脚本高速订阅同一项
    sent = drain_tx(hub)
    assert sent == [subscribe(3, True, True)], "脚本高速应升级下发"

    # UI 断开：清掉 ui:* 意图，脚本意图还在 → 不应发退订
    hub._drop_client_intent(id(ws))
    sent = drain_tx(hub)
    assert sent == [], "UI 断开但脚本还订着，不应退订"

    # 脚本也退订 → 有效态 (False,*) → 发退订帧
    hub.sub(3, "push", on=False)
    sent = drain_tx(hub)
    assert sent == [subscribe(3, False, False)], "全部退订应发退订帧"
    print("[ok] Subscribe 聚合与客户端清理")

    # --- 4. cmd 参数个数严格校验 ---
    hub.cmd_dir[0x80] = {
        "index": 0x80,
        "tag": 0,
        "name": "停车",
        "fields": [],
    }
    hub._name_to_cmd["停车"] = 0x80
    hub.cmd_dir[0x81] = {
        "index": 0x81,
        "tag": (2 << 4),
        "name": "直行",
        "fields": [{"type": 8, "name": "速度"}, {"type": 8, "name": "时间"}],
    }
    hub._name_to_cmd["直行"] = 0x81
    r = hub.cmd("停车")  # 0 参命令 OK
    assert r["ok"]
    try:
        hub.cmd("直行", 0.1)  # 只给 1 个 → 应报错
        raise AssertionError("参数不足应报错")
    except ValueError as e:
        assert "需要 2 个参数" in str(e)
    try:
        hub.cmd("直行", 0.1, 2.0, 99)  # 多给 → 应报错
        raise AssertionError("参数超量应报错")
    except ValueError:
        pass
    print("[ok] cmd 参数严格校验")

    # --- 5. 停车命令识别 ---
    hub._stop_cmd_index = 0x80
    hub.from_ui(build_frame(0x80), ws)  # type: ignore[arg-type]
    assert hub.abort_event.is_set(), "停车帧应置 abort"
    assert hub._tx_q and hub._tx_q[0][0] == 0, "停车帧应最高优先级"
    hub.clear_abort()
    drain_tx(hub)
    print("[ok] 停车帧拦截")

    print("\nall hub proxy tests passed")


if __name__ == "__main__":
    main()
