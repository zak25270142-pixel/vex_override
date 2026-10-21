# -*- coding: utf-8 -*-
"""
wave_plot.py —— 把 speed_loop_probe 的波形/汇总画成 PNG。

用法（先跑探针，再画图）：
    python scripts/tools/wave_plot.py open1                    # 按 tag 找 pytest/probe_data/open1_*
    python scripts/tools/wave_plot.py open1 -o my.png          # 指定输出路径

图面三栏：
    左：起步扫描时间轨迹（每档一条 左0转速 曲线，图例标电压）——看静摩擦释放/粘滑
    中：滚动降压时间轨迹（每档一条）——看维持/停转过程
    右：V-转速曲线（均值±std，起步 vs 降压，左/右各一条）——建 kf 表直接看这张

配对规则：波形文件按 seq 排序与汇总 CSV 行序一一对应（每个脉冲各写一个）。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无 GUI，必须用非交互后端
import matplotlib.pyplot as plt  # noqa: E402

# 监控项多是中文名，Windows 常见中文字体兜底，避免图例变成方块（与后端 hub._write_plot 一致）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

DATA = Path(__file__).resolve().parent.parent.parent / "pytest" / "probe_data"
RPM_L, RPM_R = "左0转速", "右0转速"
KEEP_TH = 3.0  # 探针的“维持”判定阈 (L+R)/2 > 3pct


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_wave(path: Path) -> dict[str, list[tuple[float, float]]]:
    """读长表波形 csv(t_rel_s,item,value) → {中文名: [(t, v), ...]}"""
    series: dict[str, list[tuple[float, float]]] = {}
    with path.open("r", encoding="utf-8-sig") as f:  # 后端 save_csv 带 BOM，用 -sig 读
        for row in csv.DictReader(f):
            t, v = _f(row.get("t_rel_s")), _f(row.get("value"))
            if t is None or v is None:
                continue
            series.setdefault(row["item"], []).append((t, v))
    return series


def trace_panel(ax, pairs, title):
    """pairs: [(电压float, wave series dict)]，画 左0转速 时间轨迹，颜色随电压深浅。"""
    if not pairs:
        ax.set_title(f"{title}（无数据）")
        return
    n = len(pairs)
    cmap = plt.cm.plasma
    for i, (v, series) in enumerate(pairs):
        pts = series.get(RPM_L) or []
        if not pts:
            continue
        ax.plot([p[0] for p in pts], [p[1] for p in pts],
                linewidth=1.2, color=cmap(0.15 + 0.8 * i / max(1, n - 1)),
                label=f"{v:g}V")
    ax.set_title(title)
    ax.set_xlabel("t / s（相对本档零点）")
    ax.set_ylabel("左0转速 / pct")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, ncol=2, title="指令电压", title_fontsize=8)


def curve_panel(ax, up_rows, down_rows):
    """V-转速曲线：均值±std，起步/降压 × 左/右 四条线 + 维持判定阈。"""
    styles = [("open_up", "o-", "起步"), ("open_down", "s--", "降压")]
    for phase, style, pname in styles:
        rows = up_rows if phase == "open_up" else down_rows
        for key, color, side in (("L_mean", "tab:blue", "左"), ("R_mean", "tab:orange", "右")):
            xs, ys, es = [], [], []
            for r in rows:
                x, y = _f(r.get("step")), _f(r.get(key))
                if x is None or y is None:
                    continue
                xs.append(x)
                ys.append(y)
                es.append(_f(r.get("L_std" if key == "L_mean" else "R_std"), 0.0) or 0.0)
            if xs:
                ax.errorbar(xs, ys, yerr=es, fmt=style, color=color, alpha=0.85,
                            elinewidth=0.8, capsize=2, markersize=4,
                            label=f"{pname}-{side}")
    ax.axhline(KEEP_TH, color="gray", linestyle=":", linewidth=1)
    ax.text(ax.get_xlim()[0] if ax.get_xlim()[0] else 0.2, KEEP_TH + 0.15,
            f"维持判定阈 {KEEP_TH:g}pct", fontsize=8, color="gray")
    ax.set_title("V-转速稳态曲线（建 kf 表看这张）")
    ax.set_xlabel("指令电压 / V")
    ax.set_ylabel("转速 / pct")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)


def main() -> None:
    ap = argparse.ArgumentParser(description="speed_loop_probe 波形/汇总 → PNG")
    ap.add_argument("tag", help="探针 --tag，如 open1")
    ap.add_argument("-o", "--out", default="", help="输出 png，默认 pytest/probe_data/<tag>_plot.png")
    args = ap.parse_args()

    # 波形按 tag 落在子目录（probe_data/05-速度环/<tag>/），下钻找
    sums = sorted(DATA.rglob(f"{args.tag}_summary.csv"))
    if not sums:
        sys.exit(f"找不到汇总文件: {DATA}/**/{args.tag}_summary.csv")
    sum_path = sums[0]
    with sum_path.open("r", encoding="utf-8-sig") as f:  # 后端 save_csv 带 BOM，用 -sig 读
        rows = list(csv.DictReader(f))

    waves = sorted(DATA.rglob(f"{args.tag}_*_wave.csv"))
    if len(waves) != len(rows):
        print(f"[warn] 波形 {len(waves)} 个与汇总 {len(rows)} 行数不一致，按最小值配对")

    pairs = []
    for row, wave in zip(rows, waves):
        v = _f(row.get("step"))
        if v is not None:
            pairs.append((row.get("phase", ""), v, load_wave(wave)))

    up = [(v, s) for ph, v, s in pairs if ph.startswith("open_up")]
    down = [(v, s) for ph, v, s in pairs if ph.startswith("open_down")]
    if not up and not down:
        sys.exit("汇总里没有 open_up/open_down 行（闭环相位的图暂不支持）")

    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(17, 4.8))
    trace_panel(ax1, up, f"起步扫描（{len(up)} 档，静止起步 0.5s/档）")
    trace_panel(ax2, down, f"滚动降压（{len(down)} 档，1.6V 起步连续降 0.3s/档）")
    curve_panel(ax3,
                [r for r in rows if (r.get("phase") or "").startswith("open_up")],
                [r for r in rows if (r.get("phase") or "").startswith("open_down")])
    fig.tight_layout()

    out = Path(args.out) if args.out else DATA / f"{args.tag}_plot.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"[out] 已写出 {out}")


if __name__ == "__main__":
    main()
