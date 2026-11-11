"""记录宽表导出冒烟（进程内，不连真车）：覆盖 test_hub_proxy 没碰到的 print_log 落盘。

  - csv / meta / png 五种组合各导一份，检查产物齐全、返回体与非法组合
  - 同拍合并：同一时刻的不同项必须落在同一行（与到达顺序无关）
  - 缺项的那拍留空单元格，计入 missing
  - max_frames（config.json）顶到后丢新帧，log_dropped 计数
运行: PYTHONPATH=. python debug/test_log_export.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from app.protocol import MenuItem, MonitorItem, MonitorSample
from app.hub import Hub
from app.config import CONFIG

MAX_LOG_FRAMES = CONFIG.max_frames  # 配置 JSON 化后常量搬走了
from app.protocol import ValueType


def mon_entry(index: int, name: str, unit: str = "m") -> MonitorItem:
    c = MonitorItem()
    c.item = MenuItem(type=ValueType.Float, tag=0, name=name, unit=unit,
                      value=MonitorSample(0.0, stamp_raw=None))
    return c


def main() -> None:
    hub = Hub(port="__none__")
    for i, nm in ((0, "左0转速"), (1, "左0电流"), (2, "右0转速")):
        hub.monitor_table[i] = mon_entry(i, nm, "pct" if "转速" in nm else "A")
    hub.log_reg.update({0, 1, 2})
    hub._stamp_ms = 1000
    hub.start_log()

    # 按项整段写（到达顺序"乱序"）：先写满 5 拍转速，再回头写电流的前 3 拍。
    # 行定位不能只跟上一行比，否则这里会开成 8 行、两项永不同行。
    for t in (0, 10, 20, 30, 40):
        hub._record(0, float(t), 3000 + t)
    for t in (0, 10, 20):
        hub._record(1, 0.5, 3000 + t)
    hub.end_log()

    df = hub.log_frame()
    assert list(df.columns) == ["左0转速", "左0电流"], df.columns
    assert len(df) == 5, f"应并成 5 行，实际 {len(df)}：\n{df}"
    assert df["左0转速"].tolist() == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert df["左0电流"].notna().sum() == 3, df["左0电流"].tolist()
    assert list(df.index) == [2000, 2010, 2020, 2030, 2040], list(df.index)
    print("[ok] 同拍合并与到达顺序无关（5 行 / 电流 3 个非空）")

    # calc 直接吃对齐好的行：交集 3 点
    r = hub.calc("左0转速 + 左0电流", agg="mean")
    assert r["n"] == 3, r
    assert abs(r["value"] - (0.5 + 10.5 + 20.5) / 3) < 1e-9, r   # 三拍：(0+.5)(10+.5)(20+.5)
    print("[ok] calc 逐点取交集（n=3）")

    tmp = Path(tempfile.mkdtemp(prefix="vexlog_"))
    try:
        combos = [
            ("csv",      dict(),                                ("csv",)),
            ("csv+meta", dict(meta=True),                       ("csv", "meta")),
            ("png",      dict(csv=False, plot=True),           ("png",)),
            ("png+csv",  dict(plot=True),                       ("png", "csv")),
            ("all",      dict(plot=True, meta=True),            ("png", "csv", "meta")),
        ]
        for tag, kw, expect in combos:
            res = hub.print_log(str(tmp / tag), **kw)
            assert set(res["files"]) == set(expect), (tag, res["files"])
            assert res["path"] == str(tmp / tag), res["path"]
            assert res["samples"] == 8, res
            for p in res["files"].values():
                assert Path(p).stat().st_size > 0, p
            if "csv" in expect:
                assert res["columns"] == ["左0转速", "左0电流"], res["columns"]
                assert res["missing"] == {"左0转速": 0, "左0电流": 2}, res["missing"]
            else:
                assert res["columns"] == [] and res["missing"] == {}, res
        print("[ok] 5 种导出组合（产物齐全 + 返回体）")

        head = (tmp / "csv.csv").read_text(encoding="utf-8-sig").splitlines()
        assert head[0].startswith("t_ms,左0转速,左0电流"), head[0]
        assert len(head) == 6, head           # 表头 + 5 行
        print("[ok] csv 宽表（缺项留空 + missing 计数）")

        meta = json.loads((tmp / "csv+meta.meta.json").read_text(encoding="utf-8"))
        assert meta["dropped"] == 0, meta
        assert [c["name"] for c in meta["columns"]] == ["左0转速", "左0电流"], meta["columns"]
        assert [c["unit"] for c in meta["columns"]] == ["pct", "A"], meta["columns"]
        assert {r["name"] for r in meta["registered"]} == {
            "左0转速", "左0电流", "右0转速"}, meta
        assert "samples" not in meta and "series" not in meta, meta
        print("[ok] 语义 json 头（单位/登记全集，不重复存样本）")

        assert (tmp / "png.png").stat().st_size > 1000
        print("[ok] png 出图（按单位分子图）")

        for kw in (dict(csv=False), dict(meta=True, csv=False, plot=True)):
            try:
                hub.print_log(str(tmp / "bad"), **kw)
                raise AssertionError("非法组合应报错")
            except ValueError:
                pass
        print("[ok] 非法组合（全 False / meta 脱离 csv）抛 ValueError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- 本段列集合在 start_log 定死：之后新登记的项不进本段 ---
    # 动机：中途加列要 np.concatenate 整块拷贝，而 _record 跑在 rx 线程上、
    # 100Hz 每帧只有 10ms。实测单次 grow 1.99~2.71 ms（5000 行），头几帧新 index
    # 集中到达时一帧能吃掉 13.5 ms——直接打穿串口接收。所以列只在 start_log 定。
    hub.monitor_table[7] = mon_entry(7, "晚登记")
    hub.log_reg.add(7)                      # 记录已 end_log，这里模拟"中途登记"
    hub.start_log()                          # 新的一段：列集合重算，含 7
    hub._record(7, 9.0, 5000)
    hub.log_reg.add(8)                       # 本段开始后才登记
    hub.monitor_table[8] = mon_entry(8, "更晚")
    hub._record(8, 1.0, 5010)                # 应被丢掉：本段没这一列
    hub.end_log()
    assert "晚登记" in hub.log_frame().columns, hub.log_frame().columns
    assert "更晚" not in hub.log_frame().columns, "本段开始后新登记的项不该进本段"
    print("[ok] 列集合在 start_log 定死（中途新登记不进本段）")

    # 空登记集合不能静默建出 0 列矩阵
    hub3 = Hub(port="__none__")
    try:
        hub3.start_log()
        raise AssertionError("没登记就 start_log 应报错")
    except ValueError as e:
        assert "还没登记" in str(e), e
    print("[ok] start_log 前没登记 → 400（不静默记空）")

    # 紧凑列宽：只登记高 index 项时列宽 = len(log_reg)，不按 index 直接寻址
    hub4 = Hub(port="__none__")
    hub4.monitor_table[200] = mon_entry(200, "高位项")
    hub4.log_reg.add(200)
    hub4._stamp_ms = 0
    hub4.start_log()
    assert hub4._log_mat.shape[1] == 1, hub4._log_mat.shape
    hub4._record(200, 3.0, 0)
    hub4.end_log()
    assert hub4.log_frame().columns.tolist() == ["高位项"]
    print("[ok] 列宽 = len(log_reg) 紧凑（登记 index=200 只占 1 列）")

    # 上限：MAX_LOG_FRAMES 满后丢新帧，保留开头那一段
    hub2 = Hub(port="__none__")
    hub2.monitor_table[0] = mon_entry(0, "x")
    hub2.log_reg.add(0)
    hub2._stamp_ms = 0
    hub2.start_log()
    for i in range(MAX_LOG_FRAMES + 20):
        hub2._record(0, float(i), i * 10)
    e = hub2.end_log()
    assert e["samples"] == MAX_LOG_FRAMES, e
    assert e["dropped"] == 20, e
    assert hub2.log_frame().index[-1] == (MAX_LOG_FRAMES - 1) * 10
    print(f"[ok] 帧上限 {MAX_LOG_FRAMES}（丢新不丢旧，dropped=20）")

    print("\nall log export tests passed")


if __name__ == "__main__":
    main()
