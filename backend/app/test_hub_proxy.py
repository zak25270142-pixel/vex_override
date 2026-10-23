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

from .dataclass import DirTable, DirectoryEvent, MenuItem, MonitorSample, ValueEvent
from .hub import Hub
from .protocol import (
    CmdDirItem,
    CmdField,
    CmdPost,
    MenuItem,
    ValueType,
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
    hub.cmd_dir[0x80] = CmdDirItem(index=0x80, tag=0, name="停车", fields=[])
    hub._name_to_cmd["停车"] = 0x80
    hub.cmd_dir[0x81] = CmdDirItem(
        index=0x81, tag=(2 << 4), name="直行",
        fields=[CmdField(type=8, name="速度"), CmdField(type=8, name="时间")],
    )
    hub._name_to_cmd["直行"] = 0x81
    r = hub.cmd("停车")  # 0 参命令 OK
    assert r.ok
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

    # --- 4.1 参数类型严格校验：字符串/布尔不进编帧层，明确报错 ---
    try:
        hub.cmd("直行", "0.1", 2.0)
        raise AssertionError("字符串参数应报错")
    except ValueError as e:
        assert "需要数值" in str(e), e
    try:
        hub.cmd("直行", True, 2.0)
        raise AssertionError("布尔参数应报错")
    except ValueError:
        pass
    r = hub.cmd("直行", 1, 2)  # int 也放行（数值家族）
    assert r.ok and r.args == [1, 2]
    print("[ok] cmd 参数类型严格校验")

    # --- 4.2 set_tunable 类型校验 + resolve_tunable 吃数字字符串 ---
    hub.tunable_dir[7] = MenuItem(index=7, type=ValueType.Float, tag=0, name="kp", unit="", value=1.0)
    hub._name_to_tun["kp"] = 7
    assert hub.resolve_tunable("7") == 7, "数字字符串应按 index 解析（与 resolve_monitor 一致）"
    assert hub.resolve_tunable("kp") == 7
    try:
        hub.set_tunable_value("kp", "1.5")
        raise AssertionError("字符串值应报错")
    except ValueError as e:
        assert "需要数值" in str(e), e
    r = hub.set_tunable_value("kp", 1.5)
    assert r.ok and r.index == 7
    print("[ok] set_tunable 类型校验 + resolve_tunable 数字字符串")

    # --- 4.3 calc 白名单：常量只收数值，字符串/布尔/None 明确拒绝 ---
    hub.monitor_dir[0] = MenuItem(index=0, type=ValueType.Float, tag=0, name="x", unit="m", value=0.0)
    hub._name_to_mon["x"] = 0
    try:
        hub.calc("x + 'abc'")
        raise AssertionError("字符串常量应报错")
    except ValueError as e:
        assert "只允许数值常量" in str(e), e
    for bad in ("x + None", "x + True", "x + [1]", "x if 1 else 0", "len(x)"):
        try:
            hub.calc(bad)
            raise AssertionError(f"{bad!r} 应报错")
        except ValueError:
            pass
    # 数值常量放行：纯常量没有监控项名，报的是"没有监控项"而非常量错误
    try:
        hub.calc("1 + 2")
        raise AssertionError("纯常量应因无监控项报错")
    except ValueError as e:
        assert "没有监控项" in str(e), e
    # 名字不存在仍由 resolve_monitor 抛 KeyError（删掉的白名单死条件不背这个锅）
    try:
        hub.calc("不存在的项 + 1")
        raise AssertionError("不存在的项应报 KeyError")
    except KeyError:
        pass
    print("[ok] calc 白名单常量校验")

    # --- 4.4 calc 端到端：运算符/同拍配对/window/agg 全链路 ---
    hub.monitor_dir[1] = MenuItem(index=1, type=ValueType.Float, tag=0, name="y", unit="m", value=0.0)
    hub._name_to_mon["y"] = 1
    hub.monitor_dir[2] = MenuItem(index=2, type=ValueType.Float, tag=0, name="z", unit="m", value=0.0)
    hub._name_to_mon["z"] = 2
    hub.monitor_latest[2] = MonitorSample(1.0, None)  # z 无历史序列，只有当前值
    hub.log_reg.update({0, 1})
    hub._stamp_ms = 1000
    hub.start_log()
    hub._record(0, 10.0, 1000)
    hub._record(1, 2.0, 1000)
    hub._record(0, 20.0, 1010)
    hub._record(1, 4.0, 1010)
    hub.end_log()
    r = hub.calc("x - y", agg="mean")
    assert r.value == 12.0 and r.n == 2, r            # (10-2 + 20-4)/2
    r = hub.calc("x - y")
    assert r.agg == "none" and r.series == [(0, 8.0), (10, 16.0)], r
    r = hub.calc("x - y", agg="sum")
    assert r.value == 24.0, r
    r = hub.calc("x - y", agg="mean", window=[0, 5])
    assert r.value == 8.0, r                          # 窗口只留 t=0 那一拍
    r = hub.calc("x + z", agg="mean")
    assert r.value == 16.0 and "z" in r.missing, r    # z 全程用当前值 1.0
    try:
        hub.calc("x - y", agg="avg")
        raise AssertionError("不支持的 agg 应报错")
    except ValueError as e:
        assert "不支持的聚合" in str(e), e
    print("[ok] calc 端到端（运算/配对/window/agg/单值项）")

    # --- 4.5 calc 运算符白名单收紧：% // 位运算必须 400，不能到 eval 才 KeyError ---
    # 放行 ast.operator 基类时它们能过校验，然后 ops 表查不到 → KeyError → 路由当 404
    # （伪装成"监控项不存在"）。这里连 KeyError 一起断言，防止以后又改成放行基类。
    for bad in ("x % 2", "x // 2", "x & 1", "x | 1", "x ^ 1", "x << 1", "x >> 1"):
        try:
            hub.calc(bad)
            raise AssertionError(f"{bad!r} 应报错")
        except KeyError:
            raise AssertionError(f"{bad!r} 漏过白名单，到 eval 才 KeyError（会被路由当 404）")
        except ValueError as e:
            assert "只允许监控项名" in str(e), e
    print("[ok] calc 未支持的运算符一律 400（% // 位运算不再伪装 404）")

    # --- 4.6 calc 结果收口：复数/溢出就地 400，不留到 JSON 序列化时才炸 ---
    for bad, why in (("(-x) ** 0.5", "复数"), ("x ** 10000", "溢出")):
        try:
            hub.calc(bad)
            raise AssertionError(f"{bad!r} 应报{why}")
        except ValueError as e:
            assert "不是有限实数" in str(e) or "溢出" in str(e), e
    print("[ok] calc 复数/溢出结果就地报错")

    # --- 4.7 window 形状校验：长度不足不能 500，反序要明说 ---
    for bad, kw in (([100], "元素"), ([400, 100], "反")):
        try:
            hub.calc("x - y", window=bad)
            raise AssertionError(f"window={bad} 应报错")
        except ValueError as e:
            assert kw in str(e), e
    print("[ok] calc window 形状校验（长度/反序都 400，不再 IndexError 500）")

    # --- 4.8 "在目录里但从没收到过值" 必须 400，不能伪装 404 ---
    hub.monitor_dir[3] = MenuItem(index=3, type=ValueType.Float, tag=0, name="nov",
                                  unit="m", value=0.0)
    hub._name_to_mon["nov"] = 3     # 故意不放 monitor_latest：从没收到过任何值
    for expr in ("nov", "nov + z"):
        try:
            hub.calc(expr)
            raise AssertionError(f"{expr!r} 应报错")
        except KeyError:
            raise AssertionError(f"{expr!r} 漏出 KeyError（会被路由当 404「项不存在」）")
        except ValueError as e:
            assert "还没有任何取值" in str(e), e
    print("[ok] calc 无取值项报 400（不再伪装 404）")

    # --- 4.9 agg 校验先于配点：拼错的 agg 不能被"没有可配对的有效点"盖掉 ---
    try:
        hub.calc("x - y", agg="avg", window=[900, 999])
        raise AssertionError("拼错的 agg 应报错")
    except ValueError as e:
        assert "不支持的聚合" in str(e), e
    print("[ok] calc agg 校验先于配点")

    # --- 4.10 有效点数 = 各项同拍交集，与谁是基准项无关 ---
    # 基准项取 sorted(seq)[0]（按项名）。让"样本少的项"当基准，点数仍应是交集大小 3。
    hub.monitor_dir[4] = MenuItem(index=4, type=ValueType.Float, tag=0, name="a1",
                                  unit="m", value=0.0)
    hub._name_to_mon["a1"] = 4
    hub.log_reg.update({0, 4})
    hub.start_log()
    for t in (0, 10, 20, 30, 40):          # x 5 点
        hub._record(0, float(t), 3000 + t)
    for t in (0, 10, 20):                  # a1 只有 3 点，且名字排在 x 前 → 当基准
        hub._record(4, 5.0, 3000 + t)
    hub.end_log()
    r = hub.calc("a1 + x", agg="mean")
    assert r.n == 3, r
    print("[ok] calc 有效点数取各项同拍交集（与基准项是谁无关）")

    # --- 4.11 lag_ms：固件时间轴上的新鲜度主判据（看门狗用）---
    # 直接注事件（帧解码由 test_protocol 覆盖，这里测 Hub 侧消费）：
    # ValueEvent.t_ms 是固件原始 16bit 戳；DirectoryEvent 建目录项。
    for i, nm in ((0, "x"), (1, "yaw")):
        hub._on_serial_event(DirectoryEvent(
            table=DirTable.monitor,
            item=MenuItem(index=i, type=ValueType.Float, tag=0, name=nm, unit="m", value=0.0)))
    hub._on_serial_event(DirectoryEvent(
        table=DirTable.monitor,
        item=MenuItem(index=5, type=ValueType.Float, tag=0, name="w", unit="m", value=0.0)))
    assert hub.read_item("w").lag_ms is None, "只有目录初值（无推送史）lag 应为 None"

    hub._on_serial_event(ValueEvent(index=0, value=1.0, t_ms=1000))
    hub._on_serial_event(ValueEvent(index=1, value=2.0, t_ms=1000))
    assert hub.read_item("x").lag_ms == 0.0 and hub.read_item("yaw").lag_ms == 0.0

    hub._on_serial_event(ValueEvent(index=0, value=1.1, t_ms=1100))   # 只有 x 的新帧
    assert hub.read_item("x").lag_ms == 0.0
    assert hub.read_item("yaw").lag_ms == 100.0, hub.read_item("yaw").lag_ms

    hub._on_serial_event(ValueEvent(index=0, value=1.2, t_ms=2000))
    assert hub.read_item("yaw").lag_ms == 1000.0, "停推项应落后 1000ms（>300 即旧）"

    # 回绕：65530 → 6 实际只差 12ms（先断流跳到 65530，再连续回绕）
    hub._fw_frontier_at -= 2.0
    hub._on_serial_event(ValueEvent(index=0, value=1.3, t_ms=65530))
    hub._on_serial_event(ValueEvent(index=1, value=2.3, t_ms=65530))
    hub._on_serial_event(ValueEvent(index=0, value=1.4, t_ms=6))
    assert hub._fw_frontier == 6, hub._fw_frontier
    assert hub.read_item("yaw").lag_ms == 12.0, hub.read_item("yaw").lag_ms

    # 断流 >1s 后第一帧无条件采纳为前沿：mod 意义下"落后"也可能是掉线恢复的新流
    hub._fw_frontier_at -= 2.0
    hub._on_serial_event(ValueEvent(index=0, value=1.5, t_ms=40000))
    assert hub._fw_frontier == 40000, hub._fw_frontier
    assert hub.read_item("x").lag_ms == 0.0
    # 跳变后还没拿到新帧的项：mod 差巨大 → 正确判旧
    assert hub.read_item("yaw").lag_ms == 40006.0, hub.read_item("yaw").lag_ms
    # 连续流中"落后"的帧只可能是重复帧：不推进前沿
    hub._on_serial_event(ValueEvent(index=1, value=2.9, t_ms=39000))
    assert hub._fw_frontier == 40000, "重复帧不应推动前沿"
    assert hub.read_item("yaw").lag_ms == 1000.0, hub.read_item("yaw").lag_ms
    print("[ok] lag_ms 新鲜度判据（无推送史/同帧/停推/回绕/断流跳变/重复帧）")

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
