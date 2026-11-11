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
import logging
import struct
import time
from dataclasses import fields
from types import SimpleNamespace

from app.config import CONFIG
from app.hub import (
    PRI_SCRIPT,
    PRI_STOP,
    PRI_UI,
    Hub,
    PendingAck,
)
from app.protocol import (
    CmdField,
    CmdGet,
    CmdPost,
    CmdTableItem,
    MenuItem,
    MonitorItem,
    MonitorSample,
    ValueType,
    build_dir_frame,
    build_frame,
    encode_value,
    ping,
    request_command,
    request_key_table,
    request_monitor,
    request_tunable,
    set_tunable,
    subscribe,
)

from debug.mock_v5 import build_response


class FakeWS:
    """最小 WebSocket 桩：只收集二进制帧。"""

    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(bytes(data))


class LogCapture(logging.Handler):
    """把 vex.hub 的日志收进列表：用于断言"无待确认帧的回执也被解析并命名"。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def make_table_frame(cmd: int, index: int, name: str, unit: str, value: float) -> bytes:
    """造一条合法目录帧（Float 类型）。"""
    nb = name.encode("utf-8")
    ub = unit.encode("utf-8")
    payload = (
        bytes([index, ValueType.Float, 0, len(nb)]) + nb + bytes([len(ub)]) + ub
        + encode_value(ValueType.Float, value)
    )
    return build_frame(cmd, payload)


def make_monitor_table_frame(index: int, name: str, unit: str, value: float) -> bytes:
    return make_table_frame(CmdPost.MonitorDirectory, index, name, unit, value)


def make_cmd_table_frame(index: int, tag: int, name: str,
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


def feed_table(hub: Hub, frame: bytes) -> None:
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


def feed_response(hub: Hub, echo_cmd: int, echo_payload: bytes = b"",
                  status: int = 0x00, info: bytes = b"") -> None:
    """经 _on_frame 喂一帧 Response（与真实链路同一入口）。"""
    f = build_response(echo_cmd, echo_payload, status, info)
    hub._on_frame(f[1], f[2:-1])


def feed_dir_round(hub: Hub, echo_cmd: int, echo_payload: bytes, count: bytes) -> None:
    """喂一整轮目录回执：收到即回（0x80 + 表项数）→ 末批传完通知（0x00、无回显）。"""
    feed_response(hub, echo_cmd, echo_payload, 0x80, count)
    feed_response(hub, echo_cmd, b"", 0x00)


def mon_entry(index: int, name: str, value: float = 0.0) -> MonitorItem:
    """测试用监控目录项（Float）：monitor 表存容器，内层是协议项。"""
    c = MonitorItem()
    c.item = MenuItem(type=ValueType.Float, tag=0, name=name, unit="m",
                      value=MonitorSample(value, stamp_raw=None))
    return c


def drain_tx(hub: Hub) -> list[bytes]:
    out = []
    while hub._tx_q:
        out.append(hub._tx_q.popleft()[2])
    return out


def main() -> None:
    hub = Hub(port="__none__")
    hub._running = True
    hub.set_loop(asyncio.new_event_loop())
    # 不建真链路：标成已选中的串口，"已连接"分支（目录请求 / 下行出队）才走得通
    hub.bridge._active = "serial"
    hub.bridge._ser = SimpleNamespace(is_open=True)

    # --- 造目录：模拟下位机 burst 上行经 _on_frame 入库 ---
    f0 = make_monitor_table_frame(0, "x", "m", 1.5)
    f1 = make_monitor_table_frame(1, "yaw", "deg", 90.0)
    feed_table(hub, f0)
    feed_table(hub, f1)
    assert len(hub.monitor_table) == 2, "目录应入库 2 项"
    assert build_dir_frame(CmdGet.RequestMonitor, 0, hub.monitor_table[0].item) == f0
    assert build_dir_frame(CmdGet.RequestMonitor, 1, hub.monitor_table[1].item) == f1
    assert hub.monitor_table[0].item.name == "x"
    print("[ok] 目录 burst 入库（只存解析结果；回放帧现场重建且字节一致）")

    # --- 1.0b 现场重建覆盖：中文名 / 调参表 / 按键目录 / Color 值 ---
    h2 = Hub(port="__none__")
    fc = make_monitor_table_frame(5, "左0电流", "A", -0.25)
    feed_table(h2, fc)
    assert build_dir_frame(CmdGet.RequestMonitor, 5, h2.monitor_table[5].item) == fc, "中文名+单位应字节一致"
    ft = make_table_frame(CmdPost.TunableDirectory, 2, "kp", "", 1.5)
    feed_table(h2, ft)
    assert build_dir_frame(CmdGet.RequestTunable, 2, h2.tunable_table[2]) == ft, "调参表命令字映射"
    nk = "确认".encode()
    fk = build_frame(CmdPost.KeyDirectory, bytes([1, len(nk)]) + nk)
    feed_table(h2, fk)
    assert build_dir_frame(CmdGet.RequestKeyDir, 1, h2.key_table[1]) == fk, "按键目录回放帧（请求与上行同号 0x06）"
    nc = "灯色".encode()
    fp = bytes([7, ValueType.Color, 0, len(nc)]) + nc + bytes([0]) + encode_value(ValueType.Color, "#a1b2c3")
    fcolor = build_frame(CmdPost.MonitorDirectory, fp)
    feed_table(h2, fcolor)
    assert build_dir_frame(CmdGet.RequestMonitor, 7, h2.monitor_table[7].item) == fcolor, "Color 往返编码"
    print("[ok] 回放帧重建：中文 / 调参 / 按键 / Color 全部字节一致")

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
    f0b = make_monitor_table_frame(0, "x", "m", 2.5)
    feed_table(hub, f0b)
    assert len(hub.monitor_table) == 2, "同 index 应覆盖不叠加"
    ws.sent_bytes.clear()
    hub.from_ui(request_monitor(), ws)  # type: ignore[arg-type]
    hub._loop.run_until_complete(asyncio.sleep(0.01))
    assert ws.sent_bytes == [f0b, f1], "回放应按 index 排序且用新帧"
    feed_table(hub, f0)  # 还原成 f0，方便后续用例
    print("[ok] 同 index 覆盖 + 按 index 排序回放")

    # --- 2.2 主动拉表先清该表，避免新旧 burst 混叠 ---
    hub._request_table(hub.monitor_table, request_monitor())
    assert not any(c.item for c in hub.monitor_table.values()), "主动拉表应清目录项（容器留意图）"
    assert drain_tx(hub) == [request_monitor()], "拉表请求应入队"
    # 重新灌回来，后续用例依赖
    feed_table(hub, f0)
    feed_table(hub, f1)
    print("[ok] 主动拉表清表")

    # --- 2.3 目录丢帧自愈：收到即回(表项数) → 传完通知触发对照 → 自动补发（不清表） ---
    hub._request_table(hub.monitor_table, request_monitor())
    assert drain_tx(hub) == [request_monitor()], "全量请求（len=0）应入队"
    f2 = make_monitor_table_frame(2, "z", "rad", 0.0)
    feed_table(hub, f0)
    feed_table(hub, f1)  # #2 丢失：旧行为里静默缺项，主机无从察觉
    feed_dir_round(hub, CmdGet.RequestMonitor, b"\x00", b"\x03")  # 0x80+count=3 → 传完通知
    sent = drain_tx(hub)
    assert sent == [request_monitor([2])], f"应自动补发缺失的 #2: {[s.hex() for s in sent]}"
    assert len(hub.monitor_table) == 2, "补发不清表：已收两项要留着"
    assert hub._table_resend_rounds[CmdGet.RequestMonitor] == 1
    # 补齐帧到达后再来一轮：无缺失，不再补发、轮次清零
    # （回显要对上补发请求的 payload [len=1][idx=2]，不是全量请求的 [0x00]）
    feed_table(hub, f2)
    feed_dir_round(hub, CmdGet.RequestMonitor, b"\x01\x02", b"\x03")
    assert drain_tx(hub) == [], "补齐后不应再补发"
    assert CmdGet.RequestMonitor not in hub._table_resend_rounds, "齐了应清轮次"
    print("[ok] 目录丢帧对照补发（双 Response → 缺项 → 部分补发 → 齐活）")

    # --- 2.3b 命令目录同样对照补发：cmd_table 按命令字(0x80+)寻键，补发帧按表下标 ---
    hub._request_table(hub.cmd_table, request_command())
    assert drain_tx(hub) == [request_command()]
    # 命令目录帧到达（[内容长度2B][命令字][tag][名长][名][参数量0]），按命令字入库
    name = "停车".encode()
    content = bytes([0x80, 0x00, len(name)]) + name + bytes([0])
    hub._on_frame(CmdPost.CMDDirectory, len(content).to_bytes(2, "little") + content)
    assert 0x80 in hub.cmd_table, "命令目录帧应按命令字入库"
    feed_dir_round(hub, CmdGet.RequestCommand, b"\x00", bytes([3]))  # 表3项，只到1项
    sent = drain_tx(hub)
    assert sent == [request_command([1, 2])], f"命令目录应按表下标补发缺失两项: {[s.hex() for s in sent]}"
    hub.cmd_table.clear()
    print("[ok] 命令目录对照补发（命令字寻键 / 表下标寻址的基准换算）")

    # --- 2.4 缺项超上限改全量重拉；外循环轮次有上限，不会滚雪球 ---
    hub._request_table(hub.monitor_table, request_monitor())
    drain_tx(hub)
    # 表项数按配置动态构造：已收项 + 上限 + 1 → 缺项恰好比补发上限多 1，
    # req_list_max 调大后本用例仍覆盖"超上限走全量重拉"这条分支。
    count = len(hub.monitor_table) + CONFIG.req_list_max + 1
    assert count <= 255, "目录表项数是 1B，构造值不能超 255"
    resp_count = bytes([count])
    feed_dir_round(hub, CmdGet.RequestMonitor, b"\x00", resp_count)
    assert drain_tx(hub) == [request_monitor()], "缺项超补发上限，应全量重拉（清表重启）"
    for _ in range(CONFIG.table_resend_rounds - 1):  # 第2、3轮：仍全缺，继续重拉
        feed_dir_round(hub, CmdGet.RequestMonitor, b"\x00", resp_count)
        assert drain_tx(hub) == [request_monitor()]
    feed_dir_round(hub, CmdGet.RequestMonitor, b"\x00", resp_count)  # 第4轮：放弃
    assert drain_tx(hub) == [], "超过轮次上限应放弃，不再发帧（防滚雪球）"
    assert CmdGet.RequestMonitor not in hub._table_resend_rounds, "放弃后清轮次"
    # 还原成两张表，后续用例依赖
    feed_table(hub, f0)
    feed_table(hub, f1)
    # 上面的对照喂了好几个 OK 响应：ACK 记账用例（第6节）按绝对值断言，这里把计数归零
    hub.ack_ok = hub.ack_retry = hub.ack_reject = hub.ack_timeout = hub.ack_unexpected = 0
    print("[ok] 缺项超上限全量重拉 + 补发轮次上限")

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

    # --- 3.1 档位按回执提交：下发未确认前 tag 不变，意图可以先于目录项存在 ---
    # 用独立 Hub：不从主用例的 ack_* 计数里借回执
    h5 = Hub(port="__none__")
    h5.from_ui(subscribe(4, True, False), ws)  # type: ignore[arg-type]
    c4 = h5.monitor_table[4]
    assert c4.item is None, "意图可以先于目录项存在"
    assert list(c4.tagdict.values()) == [(True, False)], "意图已记账"
    assert not c4.is_subscribed(), "未确认前档位不应改变"
    feed_response(h5, CmdGet.Subscribe, bytes([4, 0x80]), 0x00)
    assert not c4.is_subscribed(), \
        "目录项没到，回执无处落：丢弃，等目录帧带来固件真值"

    # 目录项在场：回执 OK 才把 SUB/FAST 合进 item.tag，GETTER/KIND 本机位不动
    h5.monitor_table[5] = mon_entry(5, "y")
    h5.monitor_table[5].item.tag = 0x01       # 模拟 KIND 本机位
    h5.from_ui(subscribe(5, True, True), ws)  # type: ignore[arg-type]
    c5 = h5.monitor_table[5]
    assert c5.item.tag == 0x01, "未确认前不动 item.tag"
    feed_response(h5, CmdGet.Subscribe, bytes([5, 0xC0]), 0x00)
    assert (c5.item.tag, c5.is_subscribed(), c5.is_fast()) == (0xC1, True, True), \
        "回执 OK 合并进 item.tag，保留 KIND 位"
    print("[ok] 档位按回执提交（无目录项时回执丢弃，有目录项时写 item.tag）")

    # --- 4. cmd 参数个数严格校验 ---
    hub.cmd_table[0x80] = CmdTableItem(tag=0, name="停车", fields=[])
    hub.cmd_table[0x81] = CmdTableItem(
            tag=(2 << 4), name="直行",
            fields=[CmdField(type=8, name="速度"), CmdField(type=8, name="时间")],
        )
    hub.cmd("停车")  # 0 参命令 OK
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
    hub.cmd("直行", 1, 2)  # int 也放行（数值家族）
    print("[ok] cmd 参数类型严格校验")

    # --- 4.2 set_tunable 类型校验 + resolve_tunable 吃数字字符串 ---
    hub.tunable_table[7] = MenuItem(type=ValueType.Float, tag=0, name="kp", unit="", value=1.0)
    assert hub.resolve_tunable("7") == 7, "数字字符串应按 index 解析（与 resolve_monitor 一致）"
    assert hub.resolve_tunable("kp") == 7
    try:
        hub.set_tunable_value("kp", "1.5")
        raise AssertionError("字符串值应报错")
    except ValueError as e:
        assert "需要数值" in str(e), e
    hub.set_tunable_value("kp", 1.5)
    print("[ok] set_tunable 类型校验 + resolve_tunable 数字字符串")

    # --- 4.3 调参回显 [type][index][value]：type 前缀自带宽度，覆盖目录值 ---
    feed_table(hub, make_table_frame(CmdPost.TunableDirectory, 7, "kp", "", 1.0))
    assert hub.tunable_table[7].value == 1.0
    feed_echo(hub, ValueType.Float, 7, 3.5)
    assert hub.tunable_table[7].value == 3.5, "回显应覆盖成真实生效值"
    feed_echo(hub, ValueType.Float, 99, 1.0)  # 目录里没有的 index：静默丢弃
    print("[ok] 调参回显 type 前缀 + 未知 index 丢弃")

    # --- 4.4 calc 白名单：常量只收数值，字符串/布尔/None 明确拒绝 ---
    hub.monitor_table[0] = mon_entry(0, "x")
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
        hub.calc("不存在的项 + 1", agg="mean")
        raise AssertionError("不存在的项应报 KeyError")
    except KeyError:
        pass
    print("[ok] calc 白名单常量校验")

    # --- 4.5 calc 端到端：运算符/同拍配对/agg 全链路 ---
    hub.monitor_table[1] = mon_entry(1, "y")
    hub.monitor_table[2] = mon_entry(2, "z")
    hub.monitor_table[2].item.value = MonitorSample(1.0, stamp_raw=500)  # z 推送过、但无历史序列
    hub.log_reg.update({0, 1})
    hub._stamp_ms = 1000
    hub.start_log()
    hub._record(0, 10.0, 1000)
    hub._record(1, 2.0, 1000)
    hub._record(0, 20.0, 1010)
    hub._record(1, 4.0, 1010)
    hub.end_log()
    r = hub.calc("x - y", agg="mean")
    assert r["value"] == 12.0 and r["n"] == 2, r            # (10-2 + 20-4)/2
    # agg 刻意不收空串：没有"逐点序列"这个选项了，要原始点走 print_log 导 csv
    try:
        hub.calc("x - y")
        raise AssertionError("agg 为空应报错")
    except ValueError as e:
        assert "不支持的聚合" in str(e), e
    r = hub.calc("x - y", agg="sum")
    assert r["value"] == 24.0, r
    r = hub.calc("x + z", agg="mean")
    assert r["value"] == 16.0 and "z" in r["missing"], r    # z 全程用当前值 1.0
    try:
        hub.calc("x - y", agg="avg")
        raise AssertionError("不支持的 agg 应报错")
    except ValueError as e:
        assert "不支持的聚合" in str(e), e
    print("[ok] calc 端到端（运算/配对/agg/单值项）")

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

    # --- 4.7 calc：** 已从白名单删除（复数来源随之消失），溢出仍就地 400 ---
    # ** 删了就不必再挡"负数的分数次幂产生复数"；这里断言它连校验都过不去
    for bad in ("(-x) ** 0.5", "x ** 10000"):
        try:
            hub.calc(bad, agg="mean")
            raise AssertionError(f"{bad!r} 应被白名单拒绝")
        except ValueError as e:
            assert "只允许监控项名" in str(e), e
    # inf 仍要就地挡：加减乘除溢出（x=10 → 10*1e308 = inf）
    try:
        hub.calc("x * 1e308", agg="mean")
        raise AssertionError("溢出应报错")
    except ValueError as e:
        assert "不是有限实数" in str(e), e
    print("[ok] calc ** 白名单拒绝 + 溢出就地报错")

    # --- 4.8 "在目录里但从没收到过值" 必须 400，不能伪装 404 ---
    hub.monitor_table[3] = mon_entry(3, "nov")
    for expr in ("nov", "nov + z"):
        try:
            hub.calc(expr, agg="mean")
            raise AssertionError(f"{expr!r} 应报错")
        except KeyError:
            raise AssertionError(f"{expr!r} 漏出 KeyError（会被路由当 404「项不存在」）")
        except ValueError as e:
            assert "还没有任何取值" in str(e), e
    print("[ok] calc 无取值项报 400（不再伪装 404）")

    # --- 4.9 agg 校验先于配点：拼错的 agg 不能被"没有可配对的有效点"盖掉 ---
    try:
        hub.calc("x - y", agg="avg")
        raise AssertionError("拼错的 agg 应报错")
    except ValueError as e:
        assert "不支持的聚合" in str(e), e
    print("[ok] calc agg 校验先于配点")

    # --- 4.11 有效点数 = 各项同拍交集，与谁是基准项无关 ---
    # 基准项取 sorted(seq)[0]（按项名）。让"样本少的项"当基准，点数仍应是交集大小 3。
    hub.monitor_table[4] = mon_entry(4, "a1")
    hub.log_reg.update({0, 4})
    hub.start_log()
    for t in (0, 10, 20, 30, 40):          # x 5 点
        hub._record(0, float(t), 3000 + t)
    for t in (0, 10, 20):                  # a1 只有 3 点，且名字排在 x 前 → 当基准
        hub._record(4, 5.0, 3000 + t)
    hub.end_log()
    r = hub.calc("a1 + x", agg="mean")
    assert r["n"] == 3, r
    print("[ok] calc 有效点数取各项同拍交集（与基准项是谁无关）")

    # --- 4.12 监控值帧：类型查目录顺序解；遇目录里没有的 index 丢剩余 ---
    feed_table(hub, make_monitor_table_frame(10, "a", "m", 0.0))
    feed_table(hub, make_monitor_table_frame(11, "b", "m", 0.0))
    feed_monitor_value(hub, [(10, 1.0), (99, 5.0), (11, 2.0)], 500)
    assert hub.monitor_table[10].item.value.value == 1.0, "未知 index 之前的项应照常解出"
    assert 99 not in hub.monitor_table, "未知 index 不应入目录表"
    assert hub.monitor_table[11].item.value.value == 0.0, "未知 index 之后的项应被丢弃（保持目录初值）"
    feed_monitor_value(hub, [(10, 2.0), (11, 3.0)], 510)  # 全已知：正常解
    assert hub.monitor_table[10].item.value.value == 2.0 and hub.monitor_table[11].item.value.value == 3.0
    assert hub.monitor_table[10].item.value.stamp_raw == 510 and hub.monitor_table[11].item.value.stamp_raw == 510, "同帧共享一个时间戳"
    print("[ok] 监控值帧按目录解码 + 未知 index 丢剩余")

    # --- 4.13 lag_ms：固件时间轴上的新鲜度主判据（看门狗用）---
    # 直接喂帧（帧边界由 test_protocol 覆盖，这里测 Hub 侧消费）：
    # 目录帧建目录项；监控值帧的 time 2B 是固件原始 16bit 戳。
    for i, nm in ((0, "x"), (1, "yaw")):
        feed_table(hub, make_monitor_table_frame(i, nm, "m", 0.0))
    feed_table(hub, make_monitor_table_frame(5, "w", "m", 0.0))
    assert hub.read_item("w")["lag_ms"] is None, "只有目录初值（无推送史）lag 应为 None"

    feed_monitor_value(hub, [(0, 1.0), (1, 2.0)], 1000)
    # 外推会引入微秒级 elapsed（注入到 read 之间的墙钟时间），断言带 50ms 容差
    assert abs(hub.read_item("x")["lag_ms"] - 0.0) < 50 and abs(hub.read_item("yaw")["lag_ms"] - 0.0) < 50

    feed_monitor_value(hub, [(0, 1.1)], 1100)   # 只有 x 的新帧
    assert abs(hub.read_item("x")["lag_ms"] - 0.0) < 50
    assert abs(hub.read_item("yaw")["lag_ms"] - 100.0) < 50, hub.read_item("yaw")["lag_ms"]

    feed_monitor_value(hub, [(0, 1.2)], 2000)
    assert abs(hub.read_item("yaw")["lag_ms"] - 1000.0) < 50, "停推项应落后 1000ms（>300 即旧）"

    # 回绕：65530 → 6 实际只差 12ms（先断流跳到 65530，再连续回绕）
    hub.latest_stamp_at -= 2.0
    feed_monitor_value(hub, [(0, 1.3), (1, 2.3)], 65530)
    feed_monitor_value(hub, [(0, 1.4)], 6)
    assert hub.latest_stamp_raw == 6, hub.latest_stamp_raw
    assert abs(hub.read_item("yaw")["lag_ms"] - 12.0) < 50, hub.read_item("yaw")["lag_ms"]

    # 断流 >1s 后第一帧无条件采纳为前沿：mod 意义下"落后"也可能是掉线恢复的新流
    hub.latest_stamp_at -= 2.0
    feed_monitor_value(hub, [(0, 1.5)], 40000)
    assert hub.latest_stamp_raw == 40000, hub.latest_stamp_raw
    assert abs(hub.read_item("x")["lag_ms"] - 0.0) < 50
    # 跳变后还没拿到新帧的项：mod 差巨大 → 正确判旧
    assert abs(hub.read_item("yaw")["lag_ms"] - 40006.0) < 50, hub.read_item("yaw")["lag_ms"]
    # 连续流中"落后"的帧只可能是重复帧：不推进前沿
    feed_monitor_value(hub, [(1, 2.9)], 39000)
    assert hub.latest_stamp_raw == 40000, "重复帧不应推动前沿"
    assert abs(hub.read_item("yaw")["lag_ms"] - 1000.0) < 50, hub.read_item("yaw")["lag_ms"]

    # 外推：前沿冻结时 lag 随 PC 时间增长——链路冻结场景不再"看起来新鲜"
    hub.latest_stamp_at -= 0.4   # 假装前沿是 400ms 前到达的（x 的戳就是前沿戳）
    lag_frozen = hub.read_item("x")["lag_ms"]
    assert 300 < lag_frozen < 600, f"冻结 400ms 后 lag 应≈400ms（已超 300 判据），实际 {lag_frozen}"
    # 封顶：持续冻结 100s 后外推量封 60s，mod 65536 不绕圈（不会伪装成新鲜）
    hub.latest_stamp_at -= 100.0
    lag_capped = hub.read_item("x")["lag_ms"]
    assert 59000 < lag_capped < 65600, f"封顶后 lag 应≈60s 且不绕圈，实际 {lag_capped}"
    print("[ok] lag_ms 新鲜度判据（无推送史/同帧/停推/回绕/断流跳变/重复帧/冻结外推/封顶）")

    # --- 4.14 命令目录帧解析：字段表 + RANGE min/max 宽度跟字段类型 + 停车命令识别 ---
    # （语义解析随"目录即真源"重构从 parser 移到 hub，这两条覆盖面从 test_protocol 迁到这里）
    fcmd = make_cmd_table_frame(0x82, 0x80 | (1 << 4), "mk", [(ValueType.Float, "a")], [(-10.0, 10.0)])
    feed_table(hub, fcmd)
    item = hub.cmd_table[0x82]
    assert item.name == "mk" and len(item.fields) == 1
    assert item.fields[0].type == ValueType.Float and item.fields[0].name == "a"
    assert item.fields[0].min == -10.0 and item.fields[0].max == 10.0
    assert build_dir_frame(CmdGet.RequestCommand, 0x82, hub.cmd_table[0x82]) == fcmd, "命令表 RANGE 帧重建应字节一致"
    assert hub._stop_cmd_index is None, "SPEC=Normal 不应被认成停车命令"

    # min/max 的类型与宽度必须跟字段走：uint8_t 字段 1B、float 字段 4B
    fcmd2 = make_cmd_table_frame(0x84, 0x80 | (2 << 4), "mv",
                               [(ValueType.UInt8, "idx"), (ValueType.Float, "vol")],
                               [(0, 7), (0.0, 12.0)])
    feed_table(hub, fcmd2)
    idx_f, vol_f = hub.cmd_table[0x84].fields
    assert idx_f.type == ValueType.UInt8 and idx_f.min == 0 and idx_f.max == 7
    assert isinstance(idx_f.min, int), "min/max 与字段同型：整型字段解成 int"
    assert vol_f.type == ValueType.Float and vol_f.min == 0.0 and vol_f.max == 12.0

    # 停车命令从 tag SPEC 位（bit3~0=1）认出，不写死字号：固件改命令表顺序也能跟上
    fstop = make_cmd_table_frame(0x90, 0x01, "停", [], None)  # argc=0 spec=Stop，无字段无 RANGE
    feed_table(hub, fstop)
    assert hub._name_index(hub.cmd_table, "停") == 0x90
    assert hub._stop_cmd_index == 0x90, hub._stop_cmd_index
    print("[ok] 命令目录帧解析（字段表/RANGE 宽度/停车命令识别）")

    # --- 5. 停车命令识别（A2：按"脚本在不在跑"分流）---
    # 4.14 已认出 0x90 是 SPEC=Stop；再登记 0x80 作第二个停车按钮，覆盖"多个按钮各管各的"
    hub._stop_cmd_indices[:] = [0x80, 0x90]
    hub._stop_cmd_index = 0x80
    hub.abort_event.clear()
    drain_tx(hub)  # 前面的用例在队列里留了帧，先清干净再看停车的入队结果

    # 5.1 没有脚本在跑：只发按钮对应的那一条，不置 abort、不清队列
    with hub._tx_cv:
        hub._tx_seq += 1
        hub._tx_q.append((PRI_SCRIPT, hub._tx_seq, build_frame(0x82)))
    hub.from_ui(build_frame(0x80), ws)  # type: ignore[arg-type]
    assert not hub.abort_event.is_set(), "没有脚本在跑时按停车不应置 abort"
    sent = drain_tx(hub)
    assert sent == [build_frame(0x80), build_frame(0x82)], (
        f"只发这一条（停车帧 PRI_STOP 排最前），脚本帧保留: {[s.hex() for s in sent]}"
    )
    assert build_frame(0x90) not in sent, "第二条按钮不能被顺带发出去"

    # 5.2 脚本在跑（占着写权）：杀脚本 + 全量停车
    hub.require_script("S")
    with hub._tx_cv:
        hub._tx_seq += 1
        hub._tx_q.append((PRI_SCRIPT, hub._tx_seq, build_frame(0x82)))
    hub.from_ui(build_frame(0x90), ws)  # type: ignore[arg-type]  按第二个按钮也要能掐脚本
    assert hub.abort_event.is_set(), "脚本在跑时按停车应先杀脚本（置 abort）"
    sent = drain_tx(hub)
    assert sent == [build_frame(0x80), build_frame(0x90)], (
        f"脚本在跑时应全量停车，脚本帧被清: {[s.hex() for s in sent]}"
    )
    hub.clear_abort()
    hub.release_script("S")
    print("[ok] 停车帧分流（脚本没在跑=单条 / 在跑=杀脚本+全量）")

    # --- 6. Response 确认与补发（下行帧 → 固件回执 → 待确认表结算）---
    hub._pending_acks.clear()

    # 6.1 入队即登记；OK 响应清除待确认（回显对上原请求 payload [len=0]；无 info=纯 OK）
    hub._send_tracked(request_monitor(), PRI_UI)
    assert CmdGet.RequestMonitor in hub._pending_acks, "入队即登记待确认"
    assert drain_tx(hub) == [request_monitor()]
    feed_response(hub, CmdGet.RequestMonitor, b"\x00")
    assert CmdGet.RequestMonitor not in hub._pending_acks, "OK 响应应清除待确认"
    assert hub.ack_ok == 1
    print("[ok] Response OK 结算清除待确认")

    # 6.2 需补发：立即原帧重发，计入同一份重试预算；明确拒绝：不重发
    hub._send_tracked(subscribe(1, True, False), PRI_UI)
    drain_tx(hub)
    feed_response(hub, CmdGet.Subscribe, bytes([1, 0x80]), 0x41)  # 需补发·校验失败
    assert drain_tx(hub) == [subscribe(1, True, False)], "需补发应立即原帧重发"
    assert hub._pending_acks[CmdGet.Subscribe][bytes([1, 0x80])].retries == CONFIG.max_retries - 1
    assert hub.ack_retry == 1
    feed_response(hub, CmdGet.Subscribe, bytes([1, 0x80]), 0xC1)  # 拒绝·下标超表
    assert not hub._tx_q, "明确拒绝不应重发"
    assert CmdGet.Subscribe not in hub._pending_acks
    assert hub.ack_reject == 1
    print("[ok] 需补发立即重发 / 明确拒绝不重发")

    # 6.3 超时重发与预算耗尽：回过 0x41 后仍静默，按同一份预算重发
    mv = build_frame(0x82, struct.pack("<f", 1.0))
    hub._send_tracked(mv, PRI_SCRIPT)
    drain_tx(hub)
    feed_response(hub, 0x82, struct.pack("<f", 1.0), 0x41)  # retries 3 → 2，并立即重发
    drain_tx(hub)
    hub._pending_acks[0x82][struct.pack("<f", 1.0)].deadline = time.monotonic() - 1
    hub._sweep_acks()
    assert drain_tx(hub) == [mv], "超时应原帧重发"
    assert hub._pending_acks[0x82][struct.pack("<f", 1.0)].retries == CONFIG.max_retries - 2
    for _ in range(CONFIG.max_retries - 1):
        hub._pending_acks[0x82][struct.pack("<f", 1.0)].deadline = time.monotonic() - 1
        hub._sweep_acks()
        drain_tx(hub)
    assert 0x82 not in hub._pending_acks, "重试耗尽应放弃"
    assert hub.ack_timeout == 1
    print("[ok] 超时重发 + 预算耗尽放弃")

    # 6.4 断线期间不烧预算（deadline 顺延、retries 不动、不发帧）
    hub._send_tracked(build_frame(0x83), PRI_UI)
    drain_tx(hub)
    hub.bridge._active = None
    hub._pending_acks[0x83][b""].deadline = time.monotonic() - 1
    hub._sweep_acks()
    assert not hub._tx_q, "断线时不应重发"
    assert hub._pending_acks[0x83][b""].retries == CONFIG.max_retries, "断线不烧重试预算"
    hub.bridge._active = "serial"
    print("[ok] 断线期间顺延不计费")

    # 6.5 TunableEcho 兼作 Set_Tunable 确认（成功路径不重复回 Response）
    hub._send_tracked(set_tunable(7, ValueType.Float, 1.5), PRI_SCRIPT)
    drain_tx(hub)
    feed_echo(hub, ValueType.Float, 7, 1.5)
    assert CmdGet.SetTunable not in hub._pending_acks, "回显即确认"

    # 6.6 无待确认帧的响应照常解析（L 自带长度，不再"长度不可知"）；只有 L 不符的坏帧记 unexpected
    ok_before, unexpected_before = hub.ack_ok, hub.ack_unexpected
    feed_response(hub, 0x99)  # 从未下发过的命令字，L=2 的 OK 回执
    assert hub.ack_ok == ok_before + 1, "无待确认的 OK 也照常记账"
    assert hub.ack_unexpected == unexpected_before, "可解析的响应不应再记 unexpected"
    # L 与实际长度不符的坏帧：记 unexpected，且不弄丢待确认帧（留给超时重发自愈）
    hub._pending_acks.clear()
    hub._send_tracked(build_frame(0x82, struct.pack("<f", 2.0)), PRI_SCRIPT)
    drain_tx(hub)
    good = build_response(0x82, struct.pack("<f", 2.0), 0x00)
    bad_payload = bytes([good[2] + 2]) + good[3:-1]  # L 比真实长度多 2
    hub._on_frame(CmdPost.Response, bad_payload)
    assert 0x82 in hub._pending_acks, "坏响应不应弄丢待确认帧"
    assert hub.ack_unexpected == unexpected_before + 1, hub.ack_unexpected
    hub._pending_acks.clear()
    print("[ok] 无待确认帧照常解析；L 不符的坏帧只记录不崩溃")

    # 6.7 前端看不出来的结局 → 推 E1 giveup 帧让前端弹提示
    fake = FakeWS()
    hub._ws_clients.add(fake)
    loop = hub._loop

    def drain_alerts() -> list[dict]:
        """_broadcast_bytes 经 run_coroutine_threadsafe 排在 loop 上，跑一拍收 E1 帧。

        只摘 E1（giveup 结论帧），下位机透传帧留在 sent_bytes 里不动。
        """
        loop.run_until_complete(asyncio.sleep(0.02))
        out, keep = [], []
        for b in fake.sent_bytes:
            if len(b) >= 5 and b[0] == 0xA5 and b[1] == 0xE1:
                out.append({"cmd": b[2]})
            else:
                keep.append(b)
        fake.sent_bytes[:] = keep
        return out

    # 拒绝/正在补发不推 alert——那两帧前端自己解析透传字节就能看到，双弹反而吵
    hub._pending_acks.clear()
    hub._send_tracked(subscribe(0, True, False), PRI_UI)
    drain_tx(hub)
    feed_response(hub, CmdGet.Subscribe, bytes([0, 0x80]), 0xC2)  # 拒绝·忙（0xC2）
    assert drain_alerts() == [], "拒绝不应推 alert（前端自己看得到这帧）"

    # 需补发但无待确认帧可发 → giveup，且解析得出具体项名（回显首字节=index 0="x"）
    feed_response(hub, CmdGet.Subscribe, bytes([0, 0x80]), 0x41)  # 需补发·校验失败，表里没有
    alerts = drain_alerts()
    assert len(alerts) == 1, alerts
    assert alerts[0]["cmd"] == CmdGet.Subscribe, alerts

    # 超时预算耗尽 → giveup，带动作命令中文名（0x82="mk"）；重发帧照常入队
    mv = build_frame(0x82, struct.pack("<f", 3.0))
    hub._send_tracked(mv, PRI_SCRIPT)
    drain_tx(hub)
    feed_response(hub, 0x82, struct.pack("<f", 3.0), 0x41)
    drain_tx(hub)
    for _ in range(CONFIG.max_retries):
        hub._pending_acks[0x82][struct.pack("<f", 3.0)].deadline = time.monotonic() - 1
        hub._sweep_acks()
        drain_tx(hub)
    alerts = drain_alerts()
    assert len(alerts) == 1, alerts
    assert alerts[0]["cmd"] == 0x82, alerts

    # Response 帧 L 与实际长度不符（流错位）→ giveup 丢弃，且不弄丢待确认帧
    hub._pending_acks.clear()
    hub._send_tracked(set_tunable(7, ValueType.Float, 1.5), PRI_SCRIPT)
    drain_tx(hub)
    good = build_response(CmdGet.SetTunable, b"\x07\x00\x00\x00\x00", 0x00)
    bad_payload = bytes([good[2] + 2]) + good[3:-1]  # L 比真实长度多 2
    hub._on_frame(CmdPost.Response, bad_payload)
    alerts = drain_alerts()
    assert len(alerts) == 1, alerts
    assert alerts[0]["cmd"] == CmdGet.SetTunable, alerts
    assert CmdGet.SetTunable in hub._pending_acks, "坏帧不应弄丢待确认帧"
    print("[ok] giveup 结局推 E1 帧（只带 cmd，文案前端造；拒绝/补发不双弹）")

    # 6.8 Reject+UnknownCmd（0xC3，L=2）：整组拒绝——摘掉该命令字下全部待确认，
    #     不重发、不扣预算、不推 giveup；不做"本次连接内拉黑"，下次下发照常入队
    hub._pending_acks.clear()
    hub._send_tracked(request_key_table(), PRI_UI)
    assert CmdGet.RequestKeyDir in hub._pending_acks
    assert drain_tx(hub) == [request_key_table()]
    feed_response(hub, CmdGet.RequestKeyDir, b"", 0xC3)  # L=2 查无此命令
    assert CmdGet.RequestKeyDir not in hub._pending_acks, "UnknownCmd 应摘掉该命令字整组待确认"
    assert not hub._tx_q, "UnknownCmd 不应重发"
    assert drain_alerts() == [], "UnknownCmd 是已知版本差异，不该弹 giveup"

    hub._send_tracked(request_key_table(), PRI_UI)
    assert drain_tx(hub) == [request_key_table()], "不拉黑：下次仍照常下发"

    # 0x41（校验失败）维持原逻辑：三次补发后放弃并推 giveup
    hub._send_tracked(request_monitor(), PRI_UI)
    drain_tx(hub)
    for i in range(CONFIG.max_retries):
        feed_response(hub, CmdGet.RequestMonitor, b"\x00", 0x41)
        assert drain_tx(hub) == [request_monitor()], f"第 {i + 1} 次应照旧重发"
    feed_response(hub, CmdGet.RequestMonitor, b"\x00", 0x41)  # 第四次：预算已用尽
    assert CmdGet.RequestMonitor not in hub._pending_acks
    assert not hub._tx_q, "预算用尽后不应再发"
    alerts = drain_alerts()
    assert len(alerts) == 1, alerts

    # 6.8b 无待确认帧 + L=2 回执：照常解析、按 status 命名并落日志（0x33 不在跟踪表）
    cap = LogCapture()
    hub_logger = logging.getLogger("vex.hub")
    hub_logger.addHandler(cap)
    try:
        hub._pending_acks.clear()
        drain_tx(hub)
        feed_response(hub, 0x33, b"", 0xC3)  # 回显 0x33、status=命令字未注册
    finally:
        hub_logger.removeHandler(cap)
    assert not hub._tx_q, "未跟踪命令字不应重发"
    assert drain_alerts() == [], "UnknownCmd 不推 giveup"
    assert any("0x33" in ln and "查无此命令" in ln and "整组拒绝" in ln for ln in cap.lines), cap.lines
    print("[ok] 无待确认帧回执也能解析并命名（0x33 → 查无此命令·整组拒绝）")

    # 重连后待确认表作废并重新拉目录（旧链路的在途帧不再等待）
    hub._resync_link()
    loop.run_until_complete(asyncio.sleep(0.02))  # 收掉 E0 状态帧广播，别留未 await 的协程
    assert CmdGet.RequestKeyDir in hub._pending_acks, "重连后目录重拉，重新登记待确认"
    hub._pending_acks.clear()
    drain_tx(hub)
    print("[ok] UnknownCmd 整组拒绝（不重发、不拉黑）")

    # 6.9 静默不回：没有"一次超时即终局"的特判，一律按同一份重试预算重发
    hub._pending_acks.clear()
    hub._send_tracked(subscribe(5, True, False), PRI_UI)
    drain_tx(hub)
    with hub._ack_lock:
        hub._pending_acks[CmdGet.Subscribe][bytes([5, 0x80])].deadline = time.monotonic() - 1
    hub._sweep_acks()
    assert CmdGet.Subscribe in hub._pending_acks, "静默超时应留在待确认里等重发"
    assert drain_tx(hub) == [subscribe(5, True, False)], "静默超时应原帧重发"
    assert hub._pending_acks[CmdGet.Subscribe][bytes([5, 0x80])].retries == CONFIG.max_retries - 1

    # 停车帧无回执同样按预算重发（不再有"没回执也继续发"的特判）
    hub._pending_acks.clear()
    stop_frame = build_frame(0x83)
    hub._send_tracked(stop_frame, PRI_STOP)
    drain_tx(hub)
    with hub._ack_lock:
        hub._pending_acks[0x83][b""].deadline = time.monotonic() - 1
    hub._sweep_acks()
    assert drain_tx(hub) == [stop_frame], "停车帧无回执也按预算重发"
    print("[ok] 静默超时按预算重发（无连接级拉黑）")

    # 6.10 同命令字多帧并发：键 (cmd, payload) 各自独立，回执/超时各管各的
    hub._pending_acks.clear()
    drain_tx(hub)
    f1, f2, f3 = (set_tunable(i, ValueType.Float, float(i)) for i in (1, 2, 3))
    for f in (f1, f2, f3):
        hub._send_tracked(f, PRI_SCRIPT)
    drain_tx(hub)
    inner = hub._pending_acks[CmdGet.SetTunable]
    assert set(inner) == {f[2:-1] for f in (f1, f2, f3)}, "三条同命令字帧应并存，不再覆盖"
    feed_response(hub, CmdGet.SetTunable, f2[2:-1], 0x00)
    assert set(hub._pending_acks[CmdGet.SetTunable]) == {f1[2:-1], f3[2:-1]}, "只清回显 payload 那条"
    for payload in (f1[2:-1], f3[2:-1]):
        hub._pending_acks[CmdGet.SetTunable][payload].deadline = time.monotonic() - 1
    hub._sweep_acks()
    assert drain_tx(hub) == [f1, f3], "剩下的两条各自按自己的 payload 重发"

    # 6.10b TunableEcho 只按回显 index 清对应那条
    hub._pending_acks.clear()
    drain_tx(hub)
    hub._send_tracked(set_tunable(2, ValueType.Float, 1.0), PRI_SCRIPT)
    hub._send_tracked(set_tunable(3, ValueType.Float, 1.0), PRI_SCRIPT)
    drain_tx(hub)
    feed_echo(hub, ValueType.Float, 2, 1.0)
    assert set(hub._pending_acks[CmdGet.SetTunable]) == {
        set_tunable(3, ValueType.Float, 1.0)[2:-1]
    }, "idx=2 的回显只清 idx=2 那条"

    # 6.11 L=2 且 0xC3 → 查无此命令：整个命令字下全部待确认一次摘掉
    hub._pending_acks.clear()
    drain_tx(hub)
    for i in (4, 5, 6):
        hub._send_tracked(set_tunable(i, ValueType.Float, float(i)), PRI_SCRIPT)
    drain_tx(hub)
    assert len(hub._pending_acks[CmdGet.SetTunable]) == 3
    cap = LogCapture()
    hub_logger = logging.getLogger("vex.hub")
    hub_logger.addHandler(cap)
    try:
        feed_response(hub, CmdGet.SetTunable, b"", 0xC3)
    finally:
        hub_logger.removeHandler(cap)
    assert CmdGet.SetTunable not in hub._pending_acks, "整组拒绝应清空该命令字全部待确认"
    assert not hub._tx_q, "整组拒绝不重发"
    assert any("查无此命令" in ln and "整组拒绝" in ln for ln in cap.lines), cap.lines

    # 6.12 L=2 的零参命令回执按 (cmd, 空 payload) 精确结算，不当查无此命令
    hub._pending_acks.clear()
    drain_tx(hub)
    zero = build_frame(0x86)  # 零参动作命令，payload 空
    hub._send_tracked(zero, PRI_SCRIPT)
    drain_tx(hub)
    assert b"" in hub._pending_acks[0x86]
    reject_before = hub.ack_reject
    feed_response(hub, 0x86, b"", 0xC2)  # BUSY（0xC2）
    assert 0x86 not in hub._pending_acks, "零参 L=2 拒绝只清 (cmd, 空 payload) 那条"
    assert hub.ack_reject == reject_before + 1

    # Pending 只保留随帧变化的两个字段（frame/timeout/priority 都由 cmd+payload 推出）
    assert [f.name for f in fields(PendingAck)] == ["deadline", "retries"]
    print("[ok] 同命令字多帧独立结算 / 整组拒绝 / 零参 L=2 精确结算")

    hub._ws_clients.discard(fake)

    # --- 7. 脚本写权：一次只允许一个脚本写 ---
    hub._lease_token = None

    # 7.1 第一个写的人自动成为持有者，他的后续写放行
    hub.require_script("A")
    assert hub._lease_token == "A", "无人持有时第一个写的就是持有者"
    hub.require_script("A")

    # 7.2 第二个脚本：明确拒绝，且报错里带得干活的信息
    try:
        hub.require_script("B")
        raise AssertionError("B 应被拒绝")
    except PermissionError as e:
        assert "A" in str(e), e
    assert hub._lease_token == "A", "被拒的请求不能抢走写权"

    # 7.3 claim：别人占着就是不给；自己的 claim 只是确认
    try:
        hub.claim_script("B")
        raise AssertionError("别人占着写权时 claim 应抛 RuntimeError")
    except RuntimeError:
        pass
    assert hub._lease_token == "A"
    hub.claim_script("A")  # 自己的 token 重复领不报错

    # 7.4 release 只认自己人：乱指认不会把别人踢掉
    hub.release_script("A")
    assert hub._lease_token is None, "持有者自己 release 应释放"
    hub.require_script("C")
    assert hub._lease_token == "C"

    # 7.5 写权不自动过期：没人 release 就一直占着
    try:
        hub.claim_script("E")
        raise AssertionError("不 release 就不该被接管")
    except RuntimeError:
        pass

    # 7.6 停车中止会放掉写权：被杀掉的脚本不能继续占门
    hub.abort_event.clear()
    hub.emergency_stop()
    assert hub.abort_event.is_set()
    assert hub._lease_token is None, "中止应释放写权"
    hub.claim_script("E")  # 中止后别人能立刻领写权
    hub.abort_event.clear()
    print("[ok] 脚本写权（独占/claim/release/中止释放/不自动过期）")

    # --- 8. refresh_tables 清目录时同步清停车索引 ---
    # 停车索引从命令目录的 SPEC=Stop 位哄出来，目录一清不跟着清，
    # 重拉期间 snapshot 与全量停车会继续用旧索引。
    hub._stop_cmd_indices[:] = [0x80, 0x90]
    hub._stop_cmd_index = 0x80
    hub.refresh_tables()
    assert hub._stop_cmd_indices == [], "重拉命令目录应先清空停车索引"
    assert hub._stop_cmd_index is None, "第一条停车索引也要清"
    assert drain_tx(hub) == [request_tunable(), request_monitor(), request_command(), request_key_table()]
    print("[ok] refresh_tables 同步清停车索引")

    # --- 9. 按键表：/api/column type=key + 虚拟按键事件 ---
    def keyframe(idx: int, name: str) -> bytes:
        """按键目录帧 payload：[index][名长][名]，名长按 UTF-8 字节数。"""
        raw = name.encode()
        return build_frame(CmdPost.KeyDirectory, bytes([idx, len(raw)]) + raw)

    feed_table(hub, keyframe(0, "确认"))
    feed_table(hub, keyframe(1, "上"))
    col = hub.get_column("key")
    assert col == ["确认", "上"], "type=key 应直接返回名字列表"
    # 空缺用 None 占位：位置 i 就是 index
    saved_key = dict(hub.key_table)
    hub.key_table[3] = "下"
    col = hub.get_column("key")
    assert col == ["确认", "上", None, "下"], "缺项应为 None，位置仍是 index"
    hub.key_table.clear(); hub.key_table.update(saved_key)
    # cmd 表按 0x80 起算：位置 0 = 命令字 0x80，位置 1 = 0x81
    saved_cmd = dict(hub.cmd_table)
    hub.cmd_table.clear()
    hub.cmd_table[0x81] = CmdTableItem(tag=0, name="直行", fields=[])
    hub.cmd_table[0x83] = CmdTableItem(tag=0, name="左转", fields=[])
    ccol = hub.get_column("cmd")
    assert len(ccol) == 4 and ccol[0] is None and ccol[1].name == "直行" \
        and ccol[2] is None and ccol[3].name == "左转", "cmd 列表位置 i ↔ 命令字 0x80+i"
    hub.cmd_table.clear(); hub.cmd_table.update(saved_cmd)
    try:
        hub.get_column("nope")
        raise AssertionError("未知目录类型应抛 ValueError")
    except ValueError:
        pass

    # 9.1 按名字/index 寻址都能发；state=1 按下、0 抬起
    hub.abort_event.clear()
    hub.key_event("确认", 1)
    assert drain_tx(hub) == [build_frame(CmdGet.KeyEvent, bytes([0, 1]))]
    hub.key_event("1", 0)  # 数字字符串按 index 解释
    assert drain_tx(hub) == [build_frame(CmdGet.KeyEvent, bytes([1, 0]))]
    hub.key_event("上", 0)
    assert drain_tx(hub) == [build_frame(CmdGet.KeyEvent, bytes([1, 0]))]

    # 9.2 按键事件与 cmd 同等待遇：同命令字按 payload 分别登记，OK 只清对应那条
    inner = hub._pending_acks[CmdGet.KeyEvent]
    assert set(inner) == {bytes([0, 1]), bytes([1, 0])}, "不同按键 payload 各自登记"
    assert all(e.retries == CONFIG.max_retries for e in inner.values())
    ack_ok_before = hub.ack_ok
    feed_response(hub, CmdGet.KeyEvent, bytes([1, 0]))
    assert bytes([1, 0]) not in hub._pending_acks[CmdGet.KeyEvent], "OK 只清对应 payload 那条"
    assert bytes([0, 1]) in hub._pending_acks[CmdGet.KeyEvent], "同命令字其它按键帧不受影响"
    assert hub.ack_ok == ack_ok_before + 1

    with hub._ack_lock:
        hub._pending_acks[CmdGet.KeyEvent][bytes([0, 1])].deadline = time.monotonic() - 1
    hub._sweep_acks()
    assert drain_tx(hub) == [build_frame(CmdGet.KeyEvent, bytes([0, 1]))], "只重发超时的那一条"
    assert (
        hub._pending_acks[CmdGet.KeyEvent][bytes([0, 1])].retries == CONFIG.max_retries - 1
    )
    # 9.3 非法输入
    try:
        hub.key_event(9, 1)
        raise AssertionError("未登记的按键 index 应抛 KeyError")
    except KeyError:
        pass
    try:
        hub.key_event("确认", 2)
        raise AssertionError("state 非 0/1 应抛 ValueError")
    except ValueError:
        pass
    # 9.4 目录重拉时表一起清（按名寻址改为扫表，不再有会残留的反查表）
    hub.refresh_tables()
    assert not hub.key_table, "重拉按键目录应先清表"
    assert drain_tx(hub) == [request_tunable(), request_monitor(), request_command(), request_key_table()]
    print("[ok] 按键目录 type=key + 虚拟按键事件（0x05，回执与 cmd 同等）")

    print("\nall hub proxy tests passed")


if __name__ == "__main__":
    main()
