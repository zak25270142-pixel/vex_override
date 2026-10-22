#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cal_wheel_r.py —— 定位轮半径实测复核

定位轮半径 wheel_r 只影响"编码器转数 → 米"的换算，是里程计唯一的长度基准。
出厂标称直径有公差，装车后直接量实际走多远最可靠。

做法
----
用 set_spin 给左右同一个目标速度走一段（开环目标，不经位置环），
记下里程计自己报的位移，你再用卷尺量出实际位移：
    读数 ∝ 当前用的 wheel_r
    wheel_r_true = wheel_r_used × 实际位移 / 里程计读数
量的是**起点到终点的直线距离**（与里程计 hypot(Δx,Δy) 同一口径）——
车走弯了也量直线，不要量走过的弧长。

为什么不用 move() 走：位置环增益还没标，走多远、会不会震荡都不确定；
set_spin 是最朴素的双侧同速，够用。

安全
----
两侧同速直行，带 1.5m 位移围栏；sleep() 可被上位机停车打断。
走之前确认前面有空地，USB 线留足松量。

用法（先起后端：cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000）
    python scripts/06-几何标定/cal_wheel_r.py                    # 默认 15pct 走 2s，走完问你卷尺值
    python scripts/06-几何标定/cal_wheel_r.py -s 20 -t 3 --used 0.034925
    python scripts/06-几何标定/cal_wheel_r.py --actual 1.023     # 不带交互，直接给实测值
"""

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.script_api import BackendLink  # noqa: E402

POS_NAMES = ["全局X", "全局Y"]
NAME_YAW = "航向"
SPEED_NAMES = ["左0转速", "右0转速"]
FENCE_M = 1.5       # 位移围栏
SETTLE_S = 0.6


def main():
    ap = argparse.ArgumentParser(description="定位轮半径 wheel_r 实测复核")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("-s", "--speed", type=float, default=15.0,
                    help="双侧目标速度 pct（默认 15；慢一点好量）")
    ap.add_argument("-t", "--time", type=float, default=2.0, help="走多久 s")
    ap.add_argument("--used", type=float, default=0.034925,
                    help="当前 chassis.h 里的 wheel_r，单位 m（默认 0.034925 = 2.75 英寸直径的半径）")
    ap.add_argument("--actual", type=float, default=None,
                    help="卷尺量到的实际位移 m；不给则走完交互式询问")
    args = ap.parse_args()

    if args.speed <= 0 or args.speed > 100:
        raise ValueError("-s 必须在 0~100 pct")
    if args.time <= 0:
        raise ValueError("-t 必须 > 0")

    want = POS_NAMES + [NAME_YAW] + SPEED_NAMES
    with BackendLink(args.base) as bl:
        missing = [n for n in want if n not in bl.mon_dir]
        if missing:
            sys.exit("监控表缺项: %s\n已有: %s" % (missing, sorted(bl.mon_dir)))

        bl.clear_subs()
        bl.sub(want)
        bl.sleep(0.3)

        print("\n提示：命令要在底盘空闲时才生效——脚本开头会发一次「停止运动」交出底盘；")
        print("      若脑屏 button3 还是 remote:on（手柄占着），命令会被拒。")
        print("\n在起点做个记号，走完在终点做个记号，量两记号之间的**直线距离**。")

        bl.stop_move()
        bl.sleep(SETTLE_S)

        x0, y0 = bl.latest(POS_NAMES[0]), bl.latest(POS_NAMES[1])
        yaw0 = bl.latest(NAME_YAW)

        print("\n  起点  X=%+.3f Y=%+.3f  yaw=%+.1f°" % (x0, y0, yaw0))
        print("  走 %.0f pct × %.1f s …" % (args.speed, args.time))
        bl.spin_target(args.speed, args.speed)

        t_end = time.perf_counter() + args.time
        while time.perf_counter() < t_end:
            bl.check_abort()
            dx = bl.latest(POS_NAMES[0]) - x0
            dy = bl.latest(POS_NAMES[1]) - y0
            if math.hypot(dx, dy) > FENCE_M:
                print("  [warn] 位移触到 %.1f m 围栏，提前停" % FENCE_M)
                break
            bl.sleep(0.05)

        bl.spin_target(0.0, 0.0)
        bl.sleep(0.3)
        bl.stop_move()
        bl.sleep(SETTLE_S)

        x1, y1 = bl.latest(POS_NAMES[0]), bl.latest(POS_NAMES[1])
        yaw1 = bl.latest(NAME_YAW)
        reported = math.hypot(x1 - x0, y1 - y0)
        print("  终点  X=%+.3f Y=%+.3f  yaw=%+.1f°" % (x1, y1, yaw1))
        print("\n  里程计报的位移：%.4f m（航向漂移 %+.1f°）" % (reported, yaw1 - yaw0))

        if reported < 0.05:
            print("\n  位移太小，没法定标：加大 -s 或 -t，或先确认车真的在走")
            return

        actual = args.actual
        if actual is None:
            try:
                actual = float(input("\n  卷尺量到的实际直线距离 (m) > ").strip())
            except (EOFError, ValueError):
                print("  没拿到实测值，退出")
                return
        if actual <= 0:
            raise ValueError("实测距离必须 > 0")

        new_r = args.used * actual / reported
        print("\n===== 结果 =====")
        print("  当前 wheel_r      %.5f m" % args.used)
        print("  里程计读数        %.4f m" % reported)
        print("  实际位移          %.4f m" % actual)
        print("  比例 实际/读数    %.4f" % (actual / reported))
        print("\n  填进 include/chassis.h：")
        print("    float wheel_r = %.5ff;  // 定位轮半径，单位 m" % new_r)
        if abs(actual / reported - 1.0) > 0.05:
            print("\n  [warn] 偏差超过 5%%：先确认卷尺量的是直线距离、轮子没打滑，")
            print("         再怀疑定位轮直径标称值或轮子在地面有滑移。")

        # 必须落盘：单次测量 + 全程只取首尾两个快照，
        # 不落的话打滑/偏航/地面不平全折进一个比例系数，事后无从复核
        bl.save_csv("cal_wheel_r.csv", [{
            "used_r": args.used, "reported_m": round(reported, 4),
            "actual_m": actual, "ratio": round(actual / reported, 4),
            "new_r": round(new_r, 5),
            "x0": round(x0, 4), "y0": round(y0, 4), "yaw0": round(yaw0, 2),
            "x1": round(x1, 4), "y1": round(y1, 4), "yaw1": round(yaw1, 2),
            "yaw_drift_deg": round(yaw1 - yaw0, 2),
            "speed_pct": args.speed, "time_s": args.time,
        }], ["used_r", "reported_m", "actual_m", "ratio", "new_r",
             "x0", "y0", "yaw0", "x1", "y1", "yaw1", "yaw_drift_deg",
             "speed_pct", "time_s"])
        print("\n[out] 落盘 cal_wheel_r.csv（首尾位姿 + 比例，供事后复核）")


if __name__ == "__main__":
    main()
