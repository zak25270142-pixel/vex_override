"""
记录会话自测（不连真车）：
  - 只记 sub(...,'log') 登记过的项；记录期间动态登记/退订即时生效
  - log_mark / log_stats（含 ratio_ge、last_frac、空窗口）/ log_rows 的区间切分
  - print_log 落盘：JSON 全量 + CSV 长表
  - cmd 取值范围校验 + 命令响应带回记录时间轴上的 t
运行：python -m app.test_log
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .hub import MAX_LOG_SAMPLES, Hub


def add_monitor(hub: Hub, idx: int, name: str, unit: str = "") -> None:
    hub.monitor_dir[idx] = {
        "index": idx, "name": name, "type": 8, "tag": 0, "unit": unit, "value": 0.0,
    }
    hub._name_to_mon[name] = idx


def feed(hub: Hub, idx: int, value: float) -> None:
    """直接投递一条监控值事件——SerialBridge 解析出来的就是这个形状。"""
    hub._on_serial_event({"kind": "value", "index": idx, "value": value})


def drain_tx(hub: Hub) -> list:
    out = []
    while hub._tx_q:
        out.append(hub._tx_q.popleft()[2])
    return out


def test_endpoints() -> None:
    """HTTP 端点层：走真实的 FastAPI 路由，验证参数解析与响应结构。"""
    import os

    os.environ.setdefault("V5_AUTO_CONNECT", "0")  # 别去连真串口
    from fastapi.testclient import TestClient

    from . import main as app_main

    with TestClient(app_main.app) as client:
        hub = app_main.hub
        add_monitor(hub, 3, "x", "m")
        assert client.post("/api/sub", json={"index_or_name": "x", "type": "log"}).json()["log"] is True

        assert client.post("/api/log/start").json()["ok"]
        for v in (0.5, 1.5, 2.5):
            feed(hub, 3, v)
            time.sleep(0.02)
        assert client.post("/api/log/mark").json()["t"] > 0

        it = client.get("/api/log/stats", params={"index_or_name": "x", "tail": 10}).json()["items"][0]
        assert it["name"] == "x" and it["n"] == 3, it

        st = client.get(
            "/api/log/stats",
            params=[("index_or_name", "x"), ("thresholds", 1.0), ("thresholds", 2.0)],
        ).json()["items"][0]
        assert abs(st["ratio_ge"]["1.0"] - 2 / 3) < 1e-9, st["ratio_ge"]

        rows = client.get("/api/log/window", params={"index_or_name": "x"}).json()["rows"]
        assert [r["value"] for r in rows] == [0.5, 1.5, 2.5], rows

        # 不传名字 → 统计全部已记录的项
        assert [i["name"] for i in client.get("/api/log/stats", params={"tail": 10}).json()["items"]] == ["x"]

        got = client.get("/api/value", params={"index_or_name": "x", "missing_ok": "true"}).json()
        assert got[0]["index"] == 3 and got[0]["value"] == 2.5, got

        assert client.post("/api/log/end").json()["samples"] == 3
        assert client.get("/api/status").json()["logging"] is False
    print("[ok] HTTP 端点 start/mark/stats/window/end/value")


def main() -> None:
    hub = Hub(port="__none__")
    add_monitor(hub, 0, "x", "m")
    add_monitor(hub, 1, "yaw", "deg")

    # --- 1. 只记登记过的项 ---
    hub.sub(0, "log")
    hub.start_log()
    for v in (1.0, 2.0, 3.0):
        feed(hub, 0, v)
        feed(hub, 1, 99.0)  # 没登记，不该被记
        time.sleep(0.02)    # 拉开时间轴，后面按区间切分才可预期
    assert hub.log_samples() == 3, hub.log_samples()
    assert sorted(hub.log_dir) == [0], hub.log_dir
    print("[ok] 只记登记项")

    # --- 2. 记录期间动态登记/退订即时生效 ---
    hub.sub(1, "log")
    feed(hub, 1, 5.0)
    assert hub.log_samples() == 4
    hub.sub(1, "log", on=False)
    feed(hub, 1, 6.0)
    assert hub.log_samples() == 4, "退订后不该再记"
    print("[ok] 动态登记/退订")

    # --- 3. mark + 区间统计 ---
    assert hub.log_mark()["t"] > 0.0
    st = hub.log_stats([0], tail=100.0)["items"][0]
    assert st["n"] == 3 and st["mean"] == 2.0 and st["min"] == 1.0 and st["max"] == 3.0
    assert abs(st["abs_mean"] - 2.0) < 1e-9 and st["abs_max"] == 3.0
    assert st["rate_hz"] is not None and st["std"] is not None
    print("[ok] log_stats 基本字段")

    st2 = hub.log_stats([0], thresholds=[1.5, 3.0])["items"][0]
    assert abs(st2["ratio_ge"]["1.5"] - 2 / 3) < 1e-9, st2["ratio_ge"]
    assert abs(st2["ratio_ge"]["3.0"] - 1 / 3) < 1e-9, st2["ratio_ge"]
    print("[ok] ratio_ge 阈值占比")

    st3 = hub.log_stats([0], since=9999.0)["items"][0]
    assert st3["n"] == 0 and st3["mean"] is None and st3["ratio_ge"] == {}
    print("[ok] 空窗口返回 n=0")

    # 区间切分（样本在 t≈0/20/40ms，取中段边界）：闭区间，边界样本都算在内
    assert hub.log_stats([0], since=0.010)["items"][0]["n"] == 2   # t>=10ms 的两条
    assert hub.log_stats([0], until=0.030)["items"][0]["n"] == 2   # t<=30ms 的两条
    assert hub.log_stats([0], since=0.010, until=0.030)["items"][0]["n"] == 1
    print("[ok] since/until 闭区间切分")

    # 优先级：tail 覆盖 since；last_frac=1 等价整个窗口
    assert hub.log_stats([0], since=9999.0, tail=100.0)["items"][0]["n"] == 3
    assert hub.log_stats([0], since=0.0, last_frac=1.0)["items"][0]["n"] == 3
    print("[ok] 窗口优先级")

    # --- 4. log_rows 长表 ---
    rows = hub.log_rows([0])["rows"]
    assert [r["value"] for r in rows] == [1.0, 2.0, 3.0], rows
    assert all(r["name"] == "x" and r["index"] == 0 for r in rows)
    assert rows == sorted(rows, key=lambda r: r["t"])
    print("[ok] log_rows 长表")

    # --- 5. print_log 落盘 ---
    with tempfile.TemporaryDirectory() as td:
        info = hub.print_log(str(Path(td) / "rec.csv"))
        csv_text = Path(info["csv"]).read_text(encoding="utf-8-sig")
        assert csv_text.startswith("t_ms,index,name,value\n"), csv_text[:60]
        assert len(csv_text.strip().splitlines()) == 1 + 4, csv_text
        assert Path(info["json"]).exists()
        assert info["samples"] == 4
    assert hub.end_log()["samples"] == 4
    print("[ok] print_log 落盘")

    # --- 5.1 停止记录后时间轴冻结在结束时刻（事后 tail 查询才不会落空） ---
    time.sleep(0.2)
    assert hub._log_now_ms() == hub._log_t_end, "停止记录后应按结束时刻冻结"
    assert hub.log_stats([0], tail=100.0)["items"][0]["n"] == 3
    print("[ok] 停止记录后时间轴冻结")

    # --- 6. cmd 取值范围校验 ---
    hub.cmd_dir[0x84] = {
        "index": 0x84, "tag": 0, "name": "设置电机电压",
        "fields": [
            {"type": 11, "name": "编号", "min": 0.0, "max": 7.0},
            {"type": 8, "name": "电压", "min": -12.0, "max": 12.0},
        ],
    }
    hub._name_to_cmd["设置电机电压"] = 0x84
    drain_tx(hub)
    r = hub.cmd("设置电机电压", 3, 3.0)
    assert r["ok"] and r["t"] is None, "没在记录时 t 应为 None"
    for bad in ((9, 3.0), (3, 20.0)):
        try:
            hub.cmd("设置电机电压", *bad)
            raise AssertionError(f"越界应报错: {bad}")
        except ValueError as e:
            assert "超出范围" in str(e), e
    try:
        hub.cmd("设置电机电压", 3, "abc")
        raise AssertionError("非数值应报错")
    except ValueError:
        pass
    print("[ok] cmd 取值范围校验")

    # --- 7. 记录中命令响应带 t；snapshot 暴露记录状态 ---
    hub.start_log()
    t = hub.cmd("设置电机电压", 0, 1.0)["t"]
    assert isinstance(t, float) and t >= 0.0, t
    snap = hub.snapshot()
    assert snap["logging"] is True and snap["log_samples"] == 0 and snap["log_dropped"] == 0
    hub.end_log()
    print("[ok] 命令带 t + snapshot")

    # --- 8. missing_ok 批量读 ---
    hub.monitor_values[0] = 1.5
    hub.monitor_time[0] = 0.0
    hub.monitor_values.pop(1, None)
    got = hub.read_items([0, 1], missing_ok=True)
    assert [it["index"] for it in got] == [0], got
    try:
        hub.read_items([0, 1])
        raise AssertionError("严格模式缺数据应抛 KeyError")
    except KeyError:
        pass
    print("[ok] missing_ok 批量读")

    assert MAX_LOG_SAMPLES > 0
    test_endpoints()
    print("\nall log session tests passed")


if __name__ == "__main__":
    main()