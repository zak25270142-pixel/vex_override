# -*- coding: utf-8 -*-
"""
单侧/双侧速度环阶跃响应测试（闭环，走 0x85 设置底盘目标速度）。

与 motor_v_test.py（0x84 开环直加电压）互补：
  - 本脚本经速度环：chassis.output(left, right) → 5ms my_spin 追踪
  - 用于标定 kf/kp/ki/kd、核对 volt_min/factor 在闭环下的表现

默认流程（单侧）：
  对侧目标=0，本侧按阶跃序列 0→30→60→0（pct），每段保持固定时间；
  全程高速订阅 L_tgt/R_tgt、L0_rpm/R0_rpm、L0_Vcmd/R0_Vcmd、L0_Vact/R0_Vact；
  原始时序写入 CSV，结束后打印上升时间/超调/稳态误差摘要。

用法示例：
  python motor_speed_step.py --side L
  python motor_speed_step.py --side R --steps 0,20,40,60,0 --hold 2.0
  python motor_speed_step.py --side both --csv speed_step.csv
  python motor_speed_step.py --port COM5 --side L   # Windows 指定口
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from typing import Dict, List, Optional, Tuple

# 复用同目录开环脚本的连接与目录解析
from motor_v_test import (
    FRAME_HEAD,
    Link,
    find_motor_items,
    frame,
    probe_port,
)

# ---- 动作命令字（与 robot_and_control.cpp robot_cmds 一致）----
CMD_STOP = 0x80          # 停止运动（无 payload）
CMD_SET_SPIN = 0x85      # 设置底盘目标速度：[left f32][right f32] pct


def cmd_stop() -> bytes:
    return frame(CMD_STOP)


def cmd_set_spin(left_pct: float, right_pct: float) -> bytes:
    return frame(CMD_SET_SPIN, struct.pack("<ff", left_pct, right_pct))


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

# 全程要订的监控键（高速）
SUB_KEYS = list(MONITOR_WANT.keys())


def resolve_indices(directory: dict) -> Dict[str, int]:
    """按中文名把监控目录映射到 index；缺项抛错。"""
    name_to_idx: Dict[str, int] = {}
    for idx, (name, _typ, _tag) in directory.items():
        if name not in name_to_idx:
            name_to_idx[name] = idx
    out: Dict[str, int] = {}
    missing = []
    for key, cn in MONITOR_WANT.items():
        if cn not in name_to_idx:
            missing.append(cn)
        else:
            out[key] = name_to_idx[cn]
    if missing:
        raise SystemExit("监控表缺少项: %s\n已有名称: %s" % (missing, sorted(name_to_idx.keys())))
    return out


def subscribe_keys(link: Link, indices: Dict[str, int], keys: List[str]) -> None:
    link.unsubscribe_all()
    time.sleep(0.08)
    for k in keys:
        link.subscribe(indices[k])
    time.sleep(0.15)


def latest(link: Link, idx: int) -> Optional[float]:
    item = link.values.get(idx)
    return None if item is None else item[1]


def record_window(
    link: Link,
    indices: Dict[str, int],
    duration: float,
    sample_hz: float = 100.0,
) -> List[dict]:
    """
    在 duration 秒内按 sample_hz 取快照。
    监控推送约 100Hz，本地按 10ms 采样即可与主控对齐。
    """
    period = 1.0 / sample_hz
    t0 = time.perf_counter()
    rows: List[dict] = []
    next_t = t0
    while True:
        now = time.perf_counter()
        if now - t0 >= duration:
            break
        if now < next_t:
            time.sleep(min(0.002, next_t - now))
            continue
        next_t += period
        row = {"t": now - t0}
        for k, idx in indices.items():
            row[k] = latest(link, idx)
        rows.append(row)
    return rows


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


def run_side(
    link: Link,
    indices: Dict[str, int],
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
    summary: List[Tuple[float, dict]] = []

    # 起始停车，确保 is_busy=false 且速度环可接收 0x85
    link.send(cmd_stop())
    time.sleep(0.3)
    link.send(cmd_set_spin(0.0, 0.0))
    time.sleep(0.4)

    for target in steps:
        if side == "L":
            left, right = target, 0.0
        else:
            left, right = 0.0, target

        print("  → %s 目标 %+.1f pct（对侧 0），保持 %.2fs" % (side, target, hold))
        link.send(cmd_set_spin(left, right))
        rows = record_window(link, indices, hold)
        for r in rows:
            r["side"] = side
            r["step_target"] = target
            all_rows.append(r)

        m = steady_metrics(rows, rpm_key, target)
        summary.append((target, m))
        # 即时打印本段
        if m.get("n", 0) == 0:
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
            tgt_vals = [r[tgt_key] for r in rows if r.get(tgt_key) is not None]
            if tgt_vals:
                print("     回读 %s 均值=%.2f（应接近目标）" % (tgt_key, sum(tgt_vals) / len(tgt_vals)))

    # 段末停车
    link.send(cmd_set_spin(0.0, 0.0))
    time.sleep(0.3)
    link.send(cmd_stop())
    return summary


def run_both(
    link: Link,
    indices: Dict[str, int],
    steps: List[float],
    hold: float,
    all_rows: List[dict],
) -> List[Tuple[float, dict, dict]]:
    """双侧同目标阶跃，用于看左右对齐。"""
    summary = []
    link.send(cmd_stop())
    time.sleep(0.3)
    link.send(cmd_set_spin(0.0, 0.0))
    time.sleep(0.4)

    for target in steps:
        print("  → 双侧目标 %+.1f pct，保持 %.2fs" % (target, hold))
        link.send(cmd_set_spin(target, target))
        rows = record_window(link, indices, hold)
        for r in rows:
            r["side"] = "both"
            r["step_target"] = target
            all_rows.append(r)
        ml = steady_metrics(rows, "L0_rpm", target)
        mr = steady_metrics(rows, "R0_rpm", target)
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

    link.send(cmd_set_spin(0.0, 0.0))
    time.sleep(0.3)
    link.send(cmd_stop())
    return summary


def write_csv(path: str, rows: List[dict], keys: List[str]) -> None:
    cols = ["t", "side", "step_target"] + keys
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            cells = []
            for c in cols:
                v = r.get(c)
                if v is None:
                    cells.append("")
                elif isinstance(v, float):
                    cells.append("%.6f" % v)
                else:
                    cells.append(str(v))
            f.write(",".join(cells) + "\n")
    print("\n时序已写入 %s（%d 行）" % (path, len(rows)))


def main() -> None:
    ap = argparse.ArgumentParser(description="速度环阶跃响应测试（0x85 闭环）")
    ap.add_argument("--port", default="auto", help="串口名，默认 auto 探测 User 口")
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
    ap.add_argument("--csv", default="motor_speed_step.csv", help="时序 CSV 路径，空字符串跳过")
    args = ap.parse_args()

    steps = [float(x) for x in args.steps.split(",") if x.strip() != ""]
    if not steps:
        print("阶跃序列为空")
        sys.exit(1)

    port = probe_port() if args.port == "auto" else args.port
    if not port:
        print("没找到会回 Pong 的 V5 User 口")
        sys.exit(1)

    link = Link(port)
    time.sleep(0.3)

    try:
        directory = link.fetch_monitor_dir()
        print("监控表共 %d 项" % len(directory))
        indices = resolve_indices(directory)
        for k in SUB_KEYS:
            print("  %s → index %d (%s)" % (k, indices[k], MONITOR_WANT[k]))

        subscribe_keys(link, indices, SUB_KEYS)

        # 确认链路与看门狗
        p0 = link.pong_count
        time.sleep(0.9)
        if link.pong_count <= p0:
            print("警告：未见 Pong，监控可能被看门狗停推；继续尝试…")

        all_rows: List[dict] = []
        print("\n===== 速度环阶跃  side=%s  steps=%s  hold=%.2fs ====="
              % (args.side, steps, args.hold))

        if args.side == "both":
            run_both(link, indices, steps, args.hold, all_rows)
        else:
            run_side(link, indices, args.side, steps, args.hold, all_rows)

        if args.csv:
            write_csv(args.csv, all_rows, SUB_KEYS)

        print("\n调参提示：")
        print("  · 稳态偏低且 Vcmd 未饱和 → 略增 kf 或 kp")
        print("  · 超调大/振荡 → 降 kp，kd 小量试")
        print("  · 静差长期不消 → 略增 ki（注意抗饱和已开启）")
        print("  · 启动迟缓 → 查 volt_min / static_deadzone（先用 motor_v_test 核对开环）")
        print("  · 左右同目标稳态差大 → 回查 volt_factor / 机械阻力")
    finally:
        try:
            link.send(cmd_set_spin(0.0, 0.0))
            time.sleep(0.15)
            link.send(cmd_stop())
            link.unsubscribe_all()
        except Exception:
            pass
        link.close()


if __name__ == "__main__":
    main()
