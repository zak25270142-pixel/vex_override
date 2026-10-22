# -*- coding: utf-8 -*-
"""
单侧/双侧速度环阶跃响应测试（闭环，走 0x85 设置底盘目标速度）。

与 motor_v_test.py（0x84 开环直加电压）互补：
  - 本脚本经速度环：chassis.output(left, right) → 5ms my_spin 追踪
  - 用于标定 kp/ki/kd、核对 volt_min/factor 在闭环下的表现
    （kf 前馈是手动填的插值表，不在此脚本扫，见 robot_and_control.cpp）

默认流程（单侧）：
  对侧目标=0，本侧按阶跃序列 0→30→60→0（pct），每段保持固定时间；
  全程记录 L_tgt/R_tgt、L0_rpm/R0_rpm、L0_Vcmd/R0_Vcmd、L0_Vact/R0_Vact 八项；
  原始时序写入 CSV，结束后打印上升时间/超调/稳态误差摘要。

记录交给后端：脚本只声明"记哪些项"，逐段发命令；每段是一段独立记录（start_log → 设目标 → 保持 → end_log），段末取波形/统计。

用法示例（先启动后端 uvicorn app.main:app --host 127.0.0.1 --port 8000，再运行本脚本）：
  python scripts/05-速度环/motor_speed_step.py --side L
  python scripts/05-速度环/motor_speed_step.py --side R --steps 0,20,40,60,0 --hold 2.0
  python scripts/05-速度环/motor_speed_step.py --side both --csv pytest/speed_step.csv
  python scripts/05-速度环/motor_speed_step.py --base http://127.0.0.1:8000 --side L
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from app.script_api import BackendLink  # noqa: E402


def plot_steps(path, all_rows, steps):
    """两栏（左/右）：每段的转速跟随曲线 + 该段目标水平线——看上升时间与超调。"""
    from plot_util import subplots, save, legend

    fig, axes = subplots(2)
    for ax, sd, cn in ((axes[0][0], "L", "左"), (axes[0][1], "R", "右")):
        name = MONITOR_WANT["L0_rpm" if sd == "L" else "R0_rpm"]
        for tgt in steps:
            pts = sorted((r["t_rel_s"], r["value"]) for r in all_rows
                         if r["name"] == name and r["step_target"] == tgt)
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], lw=1, label="目标 %gpct" % tgt)
                ax.axhline(tgt, ls=":", lw=0.8, color="gray")
        ax.set_xlabel("t / s（段内）")
        ax.set_ylabel("转速 / pct")
        ax.set_title("%s侧阶跃跟随（点线=各段目标）" % cn)
        ax.grid(True, alpha=0.3)
        legend(ax, fontsize=7)
    save(fig, path)

# 监控项中文名 → 逻辑键（与 LCD_menu.cpp monitor_menu_item 一致）
MONITOR_WANT = {
    "L_tgt": "左指令转速",
    "R_tgt": "右指令转速",
    "L0_rpm": "左0转速",
    "R0_rpm": "右0转速",
    "L0_Vcmd": "左0指令电压",
    "R0_Vcmd": "右0指令电压",
    "L0_Vact": "左0实际电压",
    "R0_Vact": "右0实际电压",
}

# 全程要记的监控键
SUB_KEYS = list(MONITOR_WANT.keys())

# 时序长表 CSV 列
TIMING_FIELDS = ["t_rel_s", "side", "step_target", "index", "name", "value"]

# 判定"进入带宽后停得住"的最短停留时间 s：
# 只看"首次进入"会被噪声穿越提前命中，报出一个漂亮但假的上升时间
SETTLE_HOLD_S = 0.15


def resolve_indices(mon_dir: Dict[str, int]) -> Dict[str, int]:
    """按中文名把监控目录映射到 index；缺项抛错。"""
    out: Dict[str, int] = {}
    missing = []
    for key, cn in MONITOR_WANT.items():
        if cn not in mon_dir:
            missing.append(cn)
        else:
            out[key] = mon_dir[cn]
    if missing:
        raise SystemExit("监控表缺少项: %s\n已有名称: %s" % (missing, sorted(mon_dir.keys())))
    return out


def steady_metrics(
    rows: List[dict],
    rpm_key: str,
    target: float,
    settle_frac: float = 0.35,
) -> dict:
    """
    用后 settle_frac 比例窗口做稳态统计；上升时间取首次进入 ±5% 带宽的时刻。
    target≈0 时不做超调/上升时间。
    """
    if not rows:
        return {"n": 0}
    n = len(rows)
    start = int(n * (1.0 - settle_frac))
    tail = [r[rpm_key] for r in rows[start:] if r.get(rpm_key) is not None]
    if not tail:
        return {"n": 0}
    mean = sum(tail) / len(tail)
    peak = max(abs(v) for v in (r[rpm_key] for r in rows if r.get(rpm_key) is not None))
    # 上升时间：进入目标±5%（至少 1pct 带宽）**且之后停得住**
    rise_t = None
    if abs(target) >= 1.0:
        band = max(abs(target) * 0.05, 1.0)
        lo, hi = target - band, target + band
        for i, r in enumerate(rows):
            v = r.get(rpm_key)
            if v is None or not (lo <= v <= hi):
                continue
            t_hold = r["t"] + SETTLE_HOLD_S
            if all(rr.get(rpm_key) is not None and lo <= rr.get(rpm_key) <= hi
                   for rr in rows[i:] if rr["t"] <= t_hold):
                rise_t = r["t"]
                break
        # 超调：负值 = 峰值没到目标（欠调）。不许归零——
        # 归零会把"根本没跟上"显示成"超调 0%"，是最典型的误导
        overshoot = (peak - abs(target)) / abs(target) * 100.0 if abs(target) > 1e-6 else 0.0
    else:
        overshoot = 0.0
    ss_err = mean - target
    return {
        "n": len(tail),
        "ss_mean": mean,
        "ss_err": ss_err,
        "peak_abs": peak,
        "overshoot_pct": overshoot,
        "rise_t": rise_t,
    }


def _rpm_rows(seg: dict, rpm_name: str, rpm_key: str) -> List[dict]:
    """从本段波形里抽转速那条，做成 steady_metrics 要的 [{t, rpm_key}] 序列。"""
    return [{"t": ts, rpm_key: val} for ts, val in seg.get(rpm_name, [])]


def _capture(bl: BackendLink, side: str, target: float, all_rows: List[dict]) -> dict:
    """取本段 8 项波形：追加到长表，并返回波形供算稳态/上升时间。

    wave() 的 t 是"相对本段开始记录的秒"；每段都是独立记录，所以直接可用。
    """
    seg = bl.wave(list(MONITOR_WANT.values()))
    for name, series in seg.items():
        for ts, val in series:
            all_rows.append({
                "t_rel_s": ts,
                "side": side,
                "step_target": target,
                "index": bl.mon_dir.get(name),
                "name": name,
                "value": val,
            })
    return seg


def run_side(
    bl: BackendLink,
    side: str,
    steps: List[float],
    hold: float,
    all_rows: List[dict],
) -> List[Tuple[float, dict]]:
    """
    side: 'L' | 'R'
    对侧始终 0；返回 [(target, metrics), ...]
    """
    assert side in ("L", "R")
    rpm_key = "L0_rpm" if side == "L" else "R0_rpm"
    tgt_key = "L_tgt" if side == "L" else "R_tgt"
    rpm_name = MONITOR_WANT[rpm_key]
    tgt_name = MONITOR_WANT[tgt_key]
    summary: List[Tuple[float, dict]] = []

    # 起始停车，确保 is_busy=false 且速度环可接收 0x85
    bl.stop_move()
    bl.sleep(0.3)
    bl.spin_target(0.0, 0.0)
    bl.sleep(0.4)

    for target in steps:
        if side == "L":
            left, right = target, 0.0
        else:
            left, right = 0.0, target

        print("  → %s 目标 %+.1f pct（对侧 0），保持 %.2fs" % (side, target, hold))
        bl.start_log()                     # 本段一段独立记录
        bl.spin_target(left, right)
        bl.sleep(hold)
        bl.end_log()

        seg = _capture(bl, side, target, all_rows)  # 本段 8 项长表

        rpm_rows = _rpm_rows(seg, rpm_name, rpm_key)
        m = steady_metrics(rpm_rows, rpm_key, target)
        summary.append((target, m))

        st = bl.stats([rpm_name, tgt_name])
        # 即时打印本段
        if st[rpm_name]["n"] == 0:
            print("     （本段无有效转速样本）")
        else:
            print(
                "     稳态转速=%.2f  误差=%+.2f  峰值=%.2f  超调=%.1f%%  上升≈%s"
                % (
                    m["ss_mean"],
                    m["ss_err"],
                    m["peak_abs"],
                    m["overshoot_pct"],
                    ("%.3fs" % m["rise_t"]) if m["rise_t"] is not None else "-",
                )
            )
            if m["overshoot_pct"] < -0.5:
                print("     [注意] 峰值未达目标（欠调 %.1f%%），不是超调" % m["overshoot_pct"])
            # 对照指令目标回读
            tgt_mean = st[tgt_name]["mean"]
            if tgt_mean is not None:
                print("     回读 %s 均值=%.2f（应接近目标）" % (tgt_key, tgt_mean))

    # 段末停车
    bl.spin_target(0.0, 0.0)
    bl.sleep(0.3)
    bl.stop_move()
    return summary


def run_both(
    bl: BackendLink,
    steps: List[float],
    hold: float,
    all_rows: List[dict],
) -> List[Tuple[float, dict, dict]]:
    """双侧同目标阶跃，用于看左右对齐。"""
    summary = []
    bl.stop_move()
    bl.sleep(0.3)
    bl.spin_target(0.0, 0.0)
    bl.sleep(0.4)

    for target in steps:
        print("  → 双侧目标 %+.1f pct，保持 %.2fs" % (target, hold))
        bl.start_log()
        bl.spin_target(target, target)
        bl.sleep(hold)
        bl.end_log()

        seg = _capture(bl, "both", target, all_rows)

        ml = steady_metrics(_rpm_rows(seg, MONITOR_WANT["L0_rpm"], "L0_rpm"), "L0_rpm", target)
        mr = steady_metrics(_rpm_rows(seg, MONITOR_WANT["R0_rpm"], "R0_rpm"), "R0_rpm", target)
        summary.append((target, ml, mr))
        print(
            "     L稳态=%.2f(err%+.2f)  R稳态=%.2f(err%+.2f)  左右差=%.2f"
            % (
                ml.get("ss_mean", float("nan")),
                ml.get("ss_err", float("nan")),
                mr.get("ss_mean", float("nan")),
                mr.get("ss_err", float("nan")),
                (ml.get("ss_mean", 0) - mr.get("ss_mean", 0)),
            )
        )

    bl.spin_target(0.0, 0.0)
    bl.sleep(0.3)
    bl.stop_move()
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="速度环阶跃响应测试（0x85 闭环）")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址，默认本机 8000")
    ap.add_argument(
        "--side",
        default="L",
        choices=["L", "R", "both"],
        help="L/R=单侧对侧为0；both=左右同目标",
    )
    ap.add_argument(
        "--steps",
        default="0,30,60,0",
        help="阶跃目标序列，逗号分隔，单位 pct（可负）",
    )
    ap.add_argument("--hold", type=float, default=1.5, help="每段保持时间（秒）")
    ap.add_argument("--csv", default="motor_speed_step.csv",
                    help="时序 CSV 文件名，空字符串跳过；落 backend/pytest/probe_data/<层>/")
    ap.add_argument("--png", default="motor_speed_step.png",
                    help="结果图文件名，空字符串跳过；落 backend/pytest/probe_data/<层>/")
    args = ap.parse_args()

    steps = [float(x) for x in args.steps.split(",") if x.strip() != ""]
    if not steps:
        print("阶跃序列为空")
        sys.exit(1)

    all_rows: List[dict] = []
    # with 退出时 __exit__ 自动收车收尾（目标归零 / 8电机0V / 刹车 / 停记录 / 退订 / 断连接）
    with BackendLink(args.base) as bl:
        print("监控表共 %d 项" % len(bl.mon_dir))
        indices = resolve_indices(bl.mon_dir)
        for k in SUB_KEYS:
            print("  %s → index %d (%s)" % (k, indices[k], MONITOR_WANT[k]))

        # 一次登记 8 项（推送 + 记录都由后端负责）
        bl.sub(list(MONITOR_WANT.values()))

        print("\n===== 速度环阶跃  side=%s  steps=%s  hold=%.2fs ====="
              % (args.side, steps, args.hold))

        if args.side == "both":
            run_both(bl, steps, args.hold, all_rows)
        else:
            run_side(bl, args.side, steps, args.hold, all_rows)

        print("\n调参提示：")
        print("  · 稳态偏低且 Vcmd 未饱和 → 略增 kf 或 kp")
        print("  · 超调大/振荡 → 降 kp，kd 小量试")
        print("  · 静差长期不消 → 略增 ki（注意抗饱和已开启）")
        print("  · 启动迟缓 → 查 volt_min / static_deadzone（先用 motor_v_test 核对开环）")
        print("  · 左右同目标稳态差大 → 回查 volt_factor / 机械阻力")

        if args.csv and all_rows:
            bl.save_csv(args.csv, all_rows, TIMING_FIELDS)
        if args.png and all_rows:
            plot_steps(bl.out_path(args.png), all_rows, steps)


if __name__ == "__main__":
    main()