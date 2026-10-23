"""
Hub 代理层进程内自测（不连真车）：
  - Ping 拦截回 Pong，不转发下位机
  - 目录帧经 _on_frame 入库（解析结果 + 完整原帧），UI 请求回放同样的帧
  - 监控值帧按目录类型解码；遇目录里没有的 index 丢剩余
  - 调参回显 [type][index][value] 直接覆盖目录值
  - Subscribe 聚合：UI+脚本并集 / fast 最高档 / 客户端断开清理
  - cmd 参数个数/类型严格校验
  - 命令目录帧解析：字段表 + RANGE min/max 宽度跟字段类型 + 停车命令从 tag SPEC 认出
运行: python -m debug.test_hub_proxy
"""

from __future__ import annotations

import asyncio
import struct

from app.dataclass import (
    CmdDirItem,
    CmdField,
    DirEntry,
    MenuItem,
    MonitorSample,
)
from app.hub import Hub
from app.protocol import (
    CmdPost,
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


def make_dir_frame(cmd: int, index: int, name: str, unit: str, value: float) -> bytes:
    """造一条合法目录帧（Float 类型）。"""
    nb = name.encode("utf-8")
    ub = unit.encode("utf-8")
    payload = (
        bytes([index, ValueType.Float, 0, len(nb)]) + nb + bytes([len(ub)]) + ub
        + encode_value(ValueType.Float, value)
    )
    return build_frame(cmd, payload)


def make_monitor_dir_frame(index: int, name: str, unit: str, value: float) -> bytes:
    return make_dir_frame(CmdPost.MonitorDirectory, index, name, unit, value)


def make_cmd_dir_frame(index: int, tag: int, name: str,
                       fields: list[tuple[int, str]],
                       ranges: list[tuple[float, float]] | None = None) -> bytes:
    """造一条动作命令目录帧：[内容长度2B][命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N
    ranges 非 None 时按 RANGE 位语义附带每参 (min,max)，宽度按字段类型定（与固件一致）。"""
    nb = name.encode("utf-8")
    content = bytes([index, tag, len(nb)]) + nb + bytes([len(fields)])
    for ftype, fname in fields:
        fb = fname.encode("utf-8")
        content += bytes([ftype, len(fb)]) + fb
    if ranges is not None:
        for (ftype, _), (lo, hi) in zip(fields, ranges):
            content += encode_value(ftype, lo) + encode_value(ftype, hi)
    return build_frame(CmdPost.CMDDirectory, len(content).to_bytes(2, "little") + content)


def feed_dir(hub: Hub, frame: bytes) -> None:
    """把一整条目录帧经 _on_frame 喂进去（与真实链路同一入口）。"""
    hub._on_frame(frame[1], frame[2:-1])


def feed_monitor_value(hub: Hub, items: list[tuple[int, float]], t_ms: int) -> None:
    """经 _on_frame 喂一条监控值帧：[len][time 2B]([index][float 4B])×N。"""
    body = struct.pack("<H", t_ms & 0xFFFF)
    for idx, v in items:
        body += bytes([idx]) + struct.pack("<f", v)
    hub._on_frame(CmdPost.MonitorValue, bytes([len(body)]) + body)


def feed_echo(hub: Hub, type_: int, index: int, value: float) -> None:
    """经 _on_frame 喂一条调参回显帧：[type][index][value]。"""
    payload = bytes([type_, index]) + struct.pack("<f", value)
    hub._on_frame(CmdPost.TunableEcho, payload)


def mon_entry(index: int, name: str, value: float = 0.0) -> DirEntry:
    """测试用监控目录项（Float）。"""
    return DirEntry(
        MenuItem(index=index, type=ValueType.Float, tag=0, name=name, unit="m", value=value), b""
    )


def drain_tx(hub: Hub) -> list[bytes]:
    out = []
    while hub._tx_q:
        out.append(hub._tx_q.popleft()[2])
    return out


def main() -> None:
    hub = Hub(port="__none__")
    hub._running = True
    hub.set_loop(asyncio.new_event_loop())

    # --- 造目录：模拟下位机 burst 上行经 _on_frame 入库 ---
    f0 = make_monitor_dir_frame(0, "x", "m", 1.5)
    f1 = make_monitor_dir_frame(1, "yaw", "deg", 90.0)
    feed_dir(hub, f0)
    feed_dir(hub, f1)
    assert len(hub.monitor_dir) == 2, "目录应入库 2 项"
    assert hub.monitor_dir[0].frame == f0 and hub.monitor_dir[1].frame == f1, "入库应带完整原帧"
    assert hub.monitor_dir[0].item.name == "x"
    print("[ok] 目录 burst 入库（解析结果 + 完整帧）")

    # --- 1. Ping 拦截：from_ui 收到 Ping 应回 Pong，且不进入下行队列 ---
    ws = FakeWS()
    hub.from_ui(ping(), ws)  # type: ignore[arg-type]
    # run_coroutine_threadsafe 排进 loop，得让 loop 跑一拍
    hub._loop.run_until_complete(asyncio.sleep(0.01))
    assert ws.sent_bytes and ws.sent_bytes[0] == ping(), "Ping 应回 Pong"
    assert not hub._tx_q, "Ping 不应转发下位机"
    print("[ok] Ping 拦截回 Pong")

    # --- 2. 目录回放：UI 请求监控目录，应只收到入库的两帧 ---
    ws.sent_bytes.clear()
    hub.from_ui(request_monitor(), ws)  # type: ignore[arg-type]
    hub._loop.run_until_complete(asyncio.sleep(0.01))
    got = ws.sent_bytes
    assert got == [f0, f1], f"目录回放帧不一致: {[g.hex() for g in got]}"
    assert not hub._tx_q, "回放命中时不应转发下位机"
    print("[ok] 目录回放（完整原帧，按 index 排序）")

    # --- 2.1 同 index 再来一帧应覆盖不叠加，回放用新帧 ---
    f0b = make_monitor_dir_frame(0, "x", "m", 2.5)
    feed_dir(hub, f0b)
    assert len(hub.monitor_dir) == 2, "同 index 应覆盖不叠加"
    ws.sent_bytes.clear()
    hub.from_ui(request_monitor(), ws)  # type: ignore[arg-type]
    hub._loop.run_until_complete(asyncio.sleep(0.01))
    assert ws.sent_bytes == [f0b, f1], "回放应按 index 排序且用新帧"
    feed_dir(hub, f0)  # 还原成 f0，方便后续用例
    print("[ok] 同 index 覆盖 + 按 index 排序回放")

    # --- 2.2 主动拉表先清该表（含名字反查），避免新旧 burst 混叠 ---
    hub._request_dir(hub.monitor_dir, hub._name_to_mon, request_monitor())
    assert not hub.monitor_dir and not hub._name_to_mon, "主动拉表应先清表和名字反查"
    assert drain_tx(hub) == [request_monitor()], "拉表请求应入队"
    # 重新灌回来，后续用例依赖
    feed_dir(hub, f0)
    feed_dir(hub, f1)
    print("[ok] 主动拉表清表（含名字反查）")

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
    hub.cmd_dir[0x80] = DirEntry(CmdDirItem(index=0x80, tag=0, name="停车", fields=[]), b"")
    hub._name_to_cmd["停车"] = 0x80
    hub.cmd_dir[0x81] = DirEntry(
        CmdDirItem(
            index=0x81, tag=(2 << 4), name="直行",
            fields=[CmdField(type=8, name="速度"), CmdField(type=8, name="时间")],
        ),
        b"",
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
    hub.tunable_dir[7] = DirEntry(
        MenuItem(index=7, type=ValueType.Float, tag=0, name="kp", unit="", value=1.0), b""
    )
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

    # --- 4.3 调参回显 [type][index][value]：type 前缀自带宽度，覆盖目录值 ---
    feed_dir(hub, make_dir_frame(CmdPost.TunableDirectory, 7, "kp", "", 1.0))
    assert hub.tunable_dir[7].item.value == 1.0
    feed_echo(hub, ValueType.Float, 7, 3.5)
    assert hub.tunable_dir[7].item.value == 3.5, "回显应覆盖成真实生效值"
    feed_echo(hub, ValueType.Float, 99, 1.0)  # 目录里没有的 index：静默丢弃
    print("[ok] 调参回显 type 前缀 + 未知 index 丢弃")

    # --- 4.4 calc 白名单：常量只收数值，字符串/布尔/None 明确拒绝 ---
    hub.monitor_dir[0] = mon_entry(0, "x")
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

    # --- 4.5 calc 端到端：运算符/同拍配对/window/agg 全链路 ---
    hub.monitor_dir[1] = mon_entry(1, "y")
    hub._name_to_mon["y"] = 1
    hub.monitor_dir[2] = mon_entry(2, "z")
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

    # --- 4.6 calc 运算符白名单收紧：% // 位运算必须 400，不能到 eval 才 KeyError ---
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

    # --- 4.7 calc 结果收口：复数/溢出就地 400，不留到 JSON 序列化时才炸 ---
    for bad, why in (("(-x) ** 0.5", "复数"), ("x ** 10000", "溢出")):
        try:
            hub.calc(bad)
            raise AssertionError(f"{bad!r} 应报{why}")
        except ValueError as e:
            assert "不是有限实数" in str(e) or "溢出" in str(e), e
    print("[ok] calc 复数/溢出结果就地报错")

    # --- 4.8 window 形状校验：长度不足不能 500，反序要明说 ---
    for bad, kw in (([100], "元素"), ([400, 100], "反")):
        try:
            hub.calc("x - y", window=bad)
            raise AssertionError(f"window={bad} 应报错")
        except ValueError as e:
            assert kw in str(e), e
    print("[ok] calc window 形状校验（长度/反序都 400，不再 IndexError 500）")

    # --- 4.9 "在目录里但从没收到过值" 必须 400，不能伪装 404 ---
    hub.monitor_dir[3] = mon_entry(3, "nov")
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

    # --- 4.10 agg 校验先于配点：拼错的 agg 不能被"没有可配对的有效点"盖掉 ---
    try:
        hub.calc("x - y", agg="avg", window=[900, 999])
        raise AssertionError("拼错的 agg 应报错")
    except ValueError as e:
        assert "不支持的聚合" in str(e), e
    print("[ok] calc agg 校验先于配点")

    # --- 4.11 有效点数 = 各项同拍交集，与谁是基准项无关 ---
    # 基准项取 sorted(seq)[0]（按项名）。让"样本少的项"当基准，点数仍应是交集大小 3。
    hub.monitor_dir[4] = mon_entry(4, "a1")
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

    # --- 4.12 监控值帧：类型查目录顺序解；遇目录里没有的 index 丢剩余 ---
    feed_dir(hub, make_monitor_dir_frame(10, "a", "m", 0.0))
    feed_dir(hub, make_monitor_dir_frame(11, "b", "m", 0.0))
    feed_monitor_value(hub, [(10, 1.0), (99, 5.0), (11, 2.0)], 500)
    assert hub.monitor_latest[10].value == 1.0, "未知 index 之前的项应照常解出"
    assert 99 not in hub.monitor_latest, "未知 index 不应入快照"
    assert hub.monitor_latest[11].value == 0.0, "未知 index 之后的项应被丢弃（保持目录初值）"
    feed_monitor_value(hub, [(10, 2.0), (11, 3.0)], 510)  # 全已知：正常解
    assert hub.monitor_latest[10].value == 2.0 and hub.monitor_latest[11].value == 3.0
    assert hub.monitor_latest[10].t_fw == 510 and hub.monitor_latest[11].t_fw == 510, "同帧共享一个时间戳"
    print("[ok] 监控值帧按目录解码 + 未知 index 丢剩余")

    # --- 4.13 lag_ms：固件时间轴上的新鲜度主判据（看门狗用）---
    # 直接喂帧（帧边界由 test_protocol 覆盖，这里测 Hub 侧消费）：
    # 目录帧建目录项；监控值帧的 time 2B 是固件原始 16bit 戳。
    for i, nm in ((0, "x"), (1, "yaw")):
        feed_dir(hub, make_monitor_dir_frame(i, nm, "m", 0.0))
    feed_dir(hub, make_monitor_dir_frame(5, "w", "m", 0.0))
    assert hub.read_item("w").lag_ms is None, "只有目录初值（无推送史）lag 应为 None"

    feed_monitor_value(hub, [(0, 1.0), (1, 2.0)], 1000)
    # 外推会引入微秒级 elapsed（注入到 read 之间的墙钟时间），断言带 50ms 容差
    assert abs(hub.read_item("x").lag_ms - 0.0) < 50 and abs(hub.read_item("yaw").lag_ms - 0.0) < 50

    feed_monitor_value(hub, [(0, 1.1)], 1100)   # 只有 x 的新帧
    assert abs(hub.read_item("x").lag_ms - 0.0) < 50
    assert abs(hub.read_item("yaw").lag_ms - 100.0) < 50, hub.read_item("yaw").lag_ms

    feed_monitor_value(hub, [(0, 1.2)], 2000)
    assert abs(hub.read_item("yaw").lag_ms - 1000.0) < 50, "停推项应落后 1000ms（>300 即旧）"

    # 回绕：65530 → 6 实际只差 12ms（先断流跳到 65530，再连续回绕）
    hub._fw_frontier_at -= 2.0
    feed_monitor_value(hub, [(0, 1.3), (1, 2.3)], 65530)
    feed_monitor_value(hub, [(0, 1.4)], 6)
    assert hub._fw_frontier == 6, hub._fw_frontier
    assert abs(hub.read_item("yaw").lag_ms - 12.0) < 50, hub.read_item("yaw").lag_ms

    # 断流 >1s 后第一帧无条件采纳为前沿：mod 意义下"落后"也可能是掉线恢复的新流
    hub._fw_frontier_at -= 2.0
    feed_monitor_value(hub, [(0, 1.5)], 40000)
    assert hub._fw_frontier == 40000, hub._fw_frontier
    assert abs(hub.read_item("x").lag_ms - 0.0) < 50
    # 跳变后还没拿到新帧的项：mod 差巨大 → 正确判旧
    assert abs(hub.read_item("yaw").lag_ms - 40006.0) < 50, hub.read_item("yaw").lag_ms
    # 连续流中"落后"的帧只可能是重复帧：不推进前沿
    feed_monitor_value(hub, [(1, 2.9)], 39000)
    assert hub._fw_frontier == 40000, "重复帧不应推动前沿"
    assert abs(hub.read_item("yaw").lag_ms - 1000.0) < 50, hub.read_item("yaw").lag_ms

    # 外推：前沿冻结时 lag 随 PC 时间增长——链路冻结场景不再"看起来新鲜"
    hub._fw_frontier_at -= 0.4   # 假装前沿是 400ms 前到达的（x 的戳就是前沿戳）
    lag_frozen = hub.read_item("x").lag_ms
    assert 300 < lag_frozen < 600, f"冻结 400ms 后 lag 应≈400ms（已超 300 判据），实际 {lag_frozen}"
    # 封顶：持续冻结 100s 后外推量封 60s，mod 65536 不绕圈（不会伪装成新鲜）
    hub._fw_frontier_at -= 100.0
    lag_capped = hub.read_item("x").lag_ms
    assert 59000 < lag_capped < 65600, f"封顶后 lag 应≈60s 且不绕圈，实际 {lag_capped}"
    print("[ok] lag_ms 新鲜度判据（无推送史/同帧/停推/回绕/断流跳变/重复帧/冻结外推/封顶）")

    # --- 4.14 命令目录帧解析：字段表 + RANGE min/max 宽度跟字段类型 + 停车命令识别 ---
    # （语义解析随"目录即真源"重构从 parser 移到 hub，这两条覆盖面从 test_protocol 迁到这里）
    fcmd = make_cmd_dir_frame(0x82, 0x80 | (1 << 4), "mk", [(ValueType.Float, "a")], [(-10.0, 10.0)])
    feed_dir(hub, fcmd)
    item = hub.cmd_dir[0x82].item
    assert item.name == "mk" and len(item.fields) == 1
    assert item.fields[0].type == ValueType.Float and item.fields[0].name == "a"
    assert item.fields[0].min == -10.0 and item.fields[0].max == 10.0
    assert hub.cmd_dir[0x82].frame == fcmd, "命令目录项也应带完整原帧供回放"
    assert hub._stop_cmd_index is None, "SPEC=Normal 不应被认成停车命令"

    # min/max 的类型与宽度必须跟字段走：uint8_t 字段 1B、float 字段 4B
    fcmd2 = make_cmd_dir_frame(0x84, 0x80 | (2 << 4), "mv",
                               [(ValueType.UInt8, "idx"), (ValueType.Float, "vol")],
                               [(0, 7), (0.0, 12.0)])
    feed_dir(hub, fcmd2)
    idx_f, vol_f = hub.cmd_dir[0x84].item.fields
    assert idx_f.type == ValueType.UInt8 and idx_f.min == 0 and idx_f.max == 7
    assert isinstance(idx_f.min, int), "min/max 与字段同型：整型字段解成 int"
    assert vol_f.type == ValueType.Float and vol_f.min == 0.0 and vol_f.max == 12.0

    # 停车命令从 tag SPEC 位（bit3~0=1）认出，不写死字号：固件改命令表顺序也能跟上
    fstop = make_cmd_dir_frame(0x90, 0x01, "停", [], None)  # argc=0 spec=Stop，无字段无 RANGE
    feed_dir(hub, fstop)
    assert hub._name_to_cmd["停"] == 0x90
    assert hub._stop_cmd_index == 0x90, hub._stop_cmd_index
    print("[ok] 命令目录帧解析（字段表/RANGE 宽度/停车命令识别）")

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
