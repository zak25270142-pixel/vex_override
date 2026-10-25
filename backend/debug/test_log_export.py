"""记录宽表导出冒烟（进程内，不连真车）：覆盖 test_hub_proxy 没碰到的 print_log 落盘。

  - csv / json / png 三种后缀各导一份，检查行数、列名、空单元格计数
  - 同拍合并：同一时刻的不同项必须落在同一行（与到达顺序无关）
  - 缺项的那拍留空单元格，计入 missing
  - MAX_LOG_FRAMES 顶到后丢新帧，log_dropped 计数
运行: PYTHONPATH=. python debug/test_log_export.py
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.dataclass import DirEntry, MenuItem
from app.hub import MAX_LOG_FRAMES, Hub
from app.protocol import ValueType


def mon_entry(index: int, name: str, unit: str = "m") -> DirEntry:
    return DirEntry(
        MenuItem(index=index, type=ValueType.Float, tag=0, name=name, unit=unit, value=0.0), b""
    )


def main() -> None:
    hub = Hub(port="__none__")
    for i, nm in ((0, "左0转速"), (1, "左0电流"), (2, "右0转速")):
        hub.monitor_dir[i] = mon_entry(i, nm, "pct" if "转速" in nm else "A")
        hub._name_to_mon[nm] = i
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
    assert list(df.index) == [2000.0, 2010.0, 2020.0, 2030.0, 2040.0], list(df.index)
    print("[ok] 同拍合并与到达顺序无关（5 行 / 电流 3 个非空）")

    # calc 直接吃对齐好的行：交集 3 点
    r = hub.calc("左0转速 + 左0电流", agg="mean")
    assert r.n == 3, r
    assert abs(r.value - (0.5 + 10.5 + 20.5) / 3) < 1e-9, r   # 三拍：(0+.5)(10+.5)(20+.5)
    print("[ok] calc 逐点取交集（n=3）")

    tmp = Path(tempfile.mkdtemp(prefix="vexlog_"))
    try:
        res = hub.print_log(str(tmp / "a.csv"))
        assert res.samples == 8, res          # 5 + 3 个非空格
        assert res.columns == ["左0转速", "左0电流"], res.columns
        assert res.missing == {"左0转速": 0, "左0电流": 2}, res.missing
        head = (tmp / "a.csv").read_text(encoding="utf-8-sig").splitlines()
        assert head[0].startswith("t_ms,左0转速,左0电流"), head[0]
        assert len(head) == 6, head           # 表头 + 5 行
        print("[ok] csv 宽表（缺项留空 + missing 计数）")

        res = hub.print_log(str(tmp / "b.json"))
        assert res.samples == 8, res
        import json as _json
        meta = _json.loads((tmp / "b.json").read_text(encoding="utf-8"))
        assert meta["samples"] == 8 and len(meta["t_ms"]) == 5, meta["samples"]
        assert len(meta["series"]) == 2, meta["series"]
        print("[ok] json 共享时间轴 + 每列非空点")

        hub.print_log(str(tmp / "c.png"))
        assert (tmp / "c.png").stat().st_size > 1000
        print("[ok] png 出图（按单位分子图）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- 本段列集合在 start_log 定死：之后新登记的项不进本段 ---
    # 动机：中途加列要 np.concatenate 整块拷贝，而 _record 跑在 rx 线程上、
    # 100Hz 每帧只有 10ms。实测单次 grow 1.99~2.71 ms（5000 行），头几帧新 index
    # 集中到达时一帧能吃掉 13.5 ms——直接打穿串口接收。所以列只在 start_log 定。
    hub.monitor_dir[7] = mon_entry(7, "晚登记")
    hub._name_to_mon["晚登记"] = 7
    hub.log_reg.add(7)                      # 记录已 end_log，这里模拟"中途登记"
    hub.start_log()                          # 新的一段：列集合重算，含 7
    hub._record(7, 9.0, 5000)
    hub.log_reg.add(8)                       # 本段开始后才登记
    hub.monitor_dir[8] = mon_entry(8, "更晚")
    hub._name_to_mon["更晚"] = 8
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
    hub4.monitor_dir[200] = mon_entry(200, "高位项")
    hub4._name_to_mon["高位项"] = 200
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
    hub2.monitor_dir[0] = mon_entry(0, "x")
    hub2._name_to_mon["x"] = 0
    hub2.log_reg.add(0)
    hub2._stamp_ms = 0
    hub2.start_log()
    for i in range(MAX_LOG_FRAMES + 20):
        hub2._record(0, float(i), i * 10)
    e = hub2.end_log()
    assert e.samples == MAX_LOG_FRAMES, e
    assert e.dropped == 20, e
    assert hub2.log_frame().index[-1] == (MAX_LOG_FRAMES - 1) * 10
    print(f"[ok] 帧上限 {MAX_LOG_FRAMES}（丢新不丢旧，dropped=20）")

    print("\nall log export tests passed")


if __name__ == "__main__":
    main()
