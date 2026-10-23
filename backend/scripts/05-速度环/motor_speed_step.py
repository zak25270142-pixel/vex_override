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
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))  # 脚本共用工具目录

from app.script_api import BackendLink  # noqa: E402
from seg_stats import seg_stats  # noqa: E402

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


def capture(bl: BackendLink, out_dir: str, tag: str) -> str:
    """把本段（start_log → end_log）的原始样本整段落盘，返回段路径（不含后缀）。

    后端缓冲在下一段 start_log 时清空，原始段必须在本段内立刻导出。
    段统计（stats 端点已删）从返回路径 + ".csv" 回读。
    """
    seg = os.path.join(out_dir, tag)
    bl.print_log(seg + ".csv")
    bl.print_log(seg + ".png")
    return seg


def run_side(
    bl: BackendLink,
    side: str,
    steps: List[float],
    hold: float,
    out_dir: str,
) -> None:
    """side: 'L' | 'R'，对侧始终 0；每段一个原始 CSV。"""
    assert side in ("L", "R")
    rpm_key = "L0_rpm" if side == "L" else "R0_rpm"
    tgt_key = "L_tgt" if side == "L" else "R_tgt"
    rpm_name = MONITOR_WANT[rpm_key]
    tgt_name = MONITOR_WANT[tgt_key]

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

        seg = capture(bl, out_dir, "%s_t%+g" % (side, target))

        st = seg_stats(seg + ".csv", [rpm_name, tgt_name])
        if st[rpm_name]["n"] == 0:
            print("     （本段无有效转速样本）")
        else:
            print("     转速均值=%.2f  |转速|峰=%.2f  回读 %s=%.2f"
                  % (st[rpm_name]["mean"] or 0.0,
                     st[rpm_name]["abs_max"] or 0.0,
                     tgt_key, st[tgt_name]["mean"] or 0.0))

    # 段末停车
    bl.spin_target(0.0, 0.0)
    bl.sleep(0.3)
    bl.stop_move()


def run_both(
    bl: BackendLink,
    steps: List[float],
    hold: float,
    out_dir: str,
) -> None:
    """双侧同目标阶跃，用于看左右对齐；每段一个原始 CSV。"""
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

        seg = capture(bl, out_dir, "both_t%+g" % target)
        st = seg_stats(seg + ".csv", [MONITOR_WANT["L0_rpm"], MONITOR_WANT["R0_rpm"]])
        print("     L转速均值=%.2f  R转速均值=%.2f"
              % (st[MONITOR_WANT["L0_rpm"]]["mean"] or 0.0,
                 st[MONITOR_WANT["R0_rpm"]]["mean"] or 0.0))

    bl.spin_target(0.0, 0.0)
    bl.sleep(0.3)
    bl.stop_move()


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
    ap.add_argument("--tag", default="run1",
                    help="原始段目录后缀；落 backend/pytest/probe_data/<层>/motor_speed_step_<tag>/")
    args = ap.parse_args()

    steps = [float(x) for x in args.steps.split(",") if x.strip() != ""]
    if not steps:
        print("阶跃序列为空")
        sys.exit(1)

    # with 退出时 __exit__ 自动收车收尾（刹车 / 停记录 / 退订 / 断连接）
    with BackendLink(args.base) as bl:
        out_dir = bl.out_path("motor_speed_step_%s" % args.tag)   # 每段一个原始 CSV
        os.makedirs(out_dir, exist_ok=True)
        print("监控表共 %d 项" % len(bl.mon_dir))
        indices = resolve_indices(bl.mon_dir)
        for k in SUB_KEYS:
            print("  %s → index %d (%s)" % (k, indices[k], MONITOR_WANT[k]))

        # 一次登记 8 项（推送 + 记录都由后端负责）
        bl.sub(list(MONITOR_WANT.values()))

        print("\n===== 速度环阶跃  side=%s  steps=%s  hold=%.2fs ====="
              % (args.side, steps, args.hold))

        if args.side == "both":
            run_both(bl, steps, args.hold, out_dir)
        else:
            run_side(bl, args.side, steps, args.hold, out_dir)

    print("\n原始段目录：%s" % out_dir)
    print("（上升时间 / 超调 / 稳态误差交给日抛分析脚本读这些 CSV）")


if __name__ == "__main__":
    main()