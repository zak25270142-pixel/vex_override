# -*- coding: utf-8 -*-
"""
全向底盘速度环阶跃响应测试（闭环，走 0x85 设置底盘目标速度）。

底盘已改为 X 布局全向轮：0x85 的三参数是 前/侧/转 三通道（不再是左右轮），
chassis.output(forward, side, turn) 内部混控成四轮目标再交给各组速度环追踪。

与 motor_v_test.py（0x84 开环直加电压）互补：
  - 本脚本经速度环：三通道 → 混控 → 四轮 my_spin 追踪
  - 用于标定 kf/kp/ki/kd、核对 volt_min/volt_factor 在闭环下的表现

默认流程（单通道）：
  只给一个通道阶跃序列 0→30→60→0（pct），另两通道为 0，每段保持固定时间；
  全程记录 左前/右前 的 指令、转速、指令电压、实际电压 八项；
  原始时序写入 CSV，结束后打印上升时间/超调/稳态误差摘要。

记录交给后端：脚本只声明"记哪些项"，逐段发命令；每段是一段独立记录（start_log → 设目标 → 保持 → end_log），段末取波形/统计。

用法示例（先启动后端 uvicorn app.main:app --host 127.0.0.1 --port 8000，再运行本脚本）：
  python scripts/motor_speed_step.py --chan F
  python scripts/motor_speed_step.py --chan S --steps 0,20,40,60,0 --hold 2.0
  python scripts/motor_speed_step.py --chan all --csv pytest/speed_step.csv
  python scripts/motor_speed_step.py --base http://127.0.0.1:8000 --chan T
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.script_api import BackendLink  # noqa: E402

# 监控项中文名 → 逻辑键（与 LCD_menu.cpp monitor_menu_item 一致；全向底盘按轮组命名）
MONITOR_WANT = {
    "LF_tgt": "左前指令",
    "RF_tgt": "右前指令",
    "LF_pct": "左前转速",
    "RF_pct": "右前转速",
    "LF_Vcmd": "左前0指令电压",
    "RF_Vcmd": "右前0指令电压",
    "LF_Vact": "左前0实际电压",
    "RF_Vact": "右前0实际电压",
}

# 全程要记的监控键
SUB_KEYS = list(MONITOR_WANT.keys())

# 时序长表 CSV 列
TIMING_FIELDS = ["t_rel_s", "side", "step_target", "index", "name", "value"]


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
    # 上升时间：向非零目标时，首次进入目标±5%（至少 1pct 带宽）
    rise_t = None
    if abs(target) >= 1.0:
        band = max(abs(target) * 0.05, 1.0)
        lo, hi = target - band, target + band
        for r in rows:
            v = r.get(rpm_key)
            if v is not None and lo <= v <= hi:
                rise_t = r["t"]
                break
        overshoot = (peak - abs(target)) / abs(target) * 100.0 if abs(target) > 1e-6 else 0.0
        if overshoot < 0:
            overshoot = 0.0
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


def run_chan(
    bl: BackendLink,
    chan: str,
    steps: List[float],
    hold: float,
    all_rows: List[dict],
) -> List[Tuple[float, dict]]:
    """
    chan: 'F'（前向）| 'S'（侧向）| 'T'（转向）
    另两个通道始终 0；返回 [(target, metrics), ...]
    """
    assert chan in ("F", "S", "T")
    # 取左前轮作为观察对象：混控后四个轮目标幅值相同，看一个就够
    rpm_key = "LF_pct"
    tgt_key = "LF_tgt"
    rpm_name = MONITOR_WANT[rpm_key]
    tgt_name = MONITOR_WANT[tgt_key]
    summary: List[Tuple[float, dict]] = []

    # 起始停车，确保 is_busy=false 且速度环可接收 0x85
    bl.stop_move()
    bl.sleep(0.3)
    bl.spin_target(0.0, 0.0, 0.0)
    bl.sleep(0.4)

    for target in steps:
        if chan == "F":
            forward, side, turn = target, 0.0, 0.0
        elif chan == "S":
            forward, side, turn = 0.0, target, 0.0
        else:
            forward, side, turn = 0.0, 0.0, target

        print("  → %s 通道目标 %+.1f pct（另两通道 0），保持 %.2fs" % (chan, target, hold))
        bl.start_log()                     # 本段一段独立记录
        bl.spin_target(forward, side, turn)
        bl.sleep(hold)
        bl.end_log()

        seg = _capture(bl, chan, target, all_rows)  # 本段 8 项长表

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
            # 对照指令目标回读
            tgt_mean = st[tgt_name]["mean"]
            if tgt_mean is not None:
                print("     回读 %s 均值=%.2f（应接近目标）" % (tgt_key, tgt_mean))

    # 段末停车
    bl.spin_target(0.0, 0.0, 0.0)
    bl.sleep(0.3)
    bl.stop_move()
    return summary


def run_all(
    bl: BackendLink,
    steps: List[float],
    hold: float,
    all_rows: List[dict],
) -> List[Tuple[float, dict, dict]]:
    """前向阶跃，四轮同目标：看左前/右前对齐（差大说明 volt_factor 或机械阻力不一致）。"""
    summary = []
    bl.stop_move()
    bl.sleep(0.3)
    bl.spin_target(0.0, 0.0, 0.0)
    bl.sleep(0.4)

    for target in steps:
        print("  → 前向目标 %+.1f pct（四轮同幅值），保持 %.2fs" % (target, hold))
        bl.start_log()
        bl.spin_target(target, 0.0, 0.0)
        bl.sleep(hold)
        bl.end_log()

        seg = _capture(bl, "all", target, all_rows)

        ml = steady_metrics(_rpm_rows(seg, MONITOR_WANT["LF_pct"], "LF_pct"), "LF_pct", target)
        mr = steady_metrics(_rpm_rows(seg, MONITOR_WANT["RF_pct"], "RF_pct"), "RF_pct", target)
        summary.append((target, ml, mr))
        print(
            "     左前稳态=%.2f(err%+.2f)  右前稳态=%.2f(err%+.2f)  两轮差=%.2f"
            % (
                ml.get("ss_mean", float("nan")),
                ml.get("ss_err", float("nan")),
                mr.get("ss_mean", float("nan")),
                mr.get("ss_err", float("nan")),
                (ml.get("ss_mean", 0) - mr.get("ss_mean", 0)),
            )
        )

    bl.spin_target(0.0, 0.0, 0.0)
    bl.sleep(0.3)
    bl.stop_move()
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="速度环阶跃响应测试（0x85 闭环）")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址，默认本机 8000")
    ap.add_argument(
        "--chan",
        default="F",
        choices=["F", "S", "T", "all"],
        help="F/S/T=单通道阶跃、另两通道为0；all=前向阶跃（四轮同幅值，看轮间对齐）",
    )
    ap.add_argument(
        "--steps",
        default="0,30,60,0",
        help="阶跃目标序列，逗号分隔，单位 pct（可负）",
    )
    ap.add_argument("--hold", type=float, default=1.5, help="每段保持时间（秒）")
    ap.add_argument("--csv", default="pytest/motor_speed_step.csv", help="时序 CSV 路径，空字符串跳过")
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

        print("\n===== 速度环阶跃  chan=%s  steps=%s  hold=%.2fs ====="
              % (args.chan, steps, args.hold))

        if args.chan == "all":
            run_all(bl, steps, args.hold, all_rows)
        else:
            run_chan(bl, args.chan, steps, args.hold, all_rows)

        print("\n调参提示：")
        print("  · 稳态偏低且 Vcmd 未饱和 → 略增 kf 或 kp")
        print("  · 超调大/振荡 → 降 kp，kd 小量试")
        print("  · 静差长期不消 → 略增 ki（注意抗饱和已开启）")
        print("  · 启动迟缓 → 查 volt_min / static_deadzone（先用 motor_v_test 核对开环）")
        print("  · 四轮同目标稳态差大 → 回查 volt_factor / 机械阻力")
        print("  · 横移(S)比直行(F)明显差 → 滚子摩擦更大，四组静/动摩擦要按方向分别标定")
        print("  · 跑起来姿态歪 → 看监控的 内耗 与四组 达标率，同步没起作用就调 dyn_* 三件套")

        if args.csv and all_rows:
            bl.save_csv(args.csv, all_rows, TIMING_FIELDS)


if __name__ == "__main__":
    main()