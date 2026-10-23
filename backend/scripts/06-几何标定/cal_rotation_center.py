#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cal_rotation_center.py —— 旋转中心（定位轮 offset）实车标定

为什么不用几何中心
------------------
chassis.h 里两个 offset 的语义是"定位轮到车体**实际旋转中心**的有符号距离"，
不是到几何中心的距离。车因为重心偏、两侧抓地力不同，实际绕哪转可以和几何中心差不少。
所以不去算几何中心，而是让它原地转一圈，从定位轮读数里反解——绕哪转量出来就是哪。

原理
----
原地纯转（中心不动）时，定位轮读数里那部分"假平移"完全来自轮子偏离旋转中心：
    前向轮读数增量 Δf = -offset_f · Δθ
    侧向轮读数增量 Δs = +offset_s · Δθ
Δθ 为航向增量（rad，右转为正）。于是：
    offset_f = -Δf / Δθ      前向轮的横向偏移（右为正）
    offset_s = +Δs / Δθ      侧向轮的纵向偏移（前为正）
与 chassis.h 的 forward_tracking_offset / side_tracking_offset 同义同号。

做法
----
起转 → 等进入稳态 → 记起点(航向/前向轮里程/侧向轮里程) → 转到目标角度 → 记终点。
只在稳态段取首尾两个点，起步与收尾的窜动不计入。
正反各转一次互相印证：两次结果应一致，差得多说明打滑不对称，降速或换地面重测。

取值用的是定位轮的**累计原始读数**（"前向轮里程"/"侧向轮里程"），取首尾差，
与推送档位、采样率都无关。

安全
----
原地转，用全局X/Y 做 0.5m 位移围栏；sleep() 可被上位机停车打断。
默认只转一圈（360°）——转之前确认 USB 线留足松量，别把线绞上。

用法（先起后端：cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000）
    python scripts/06-几何标定/cal_rotation_center.py                 # 默认 V=25，正反各 1 圈
    python scripts/06-几何标定/cal_rotation_center.py -v 35 -t 720    # 快一点、转两圈
    python scripts/06-几何标定/cal_rotation_center.py --dir cw        # 只转一个方向

出结果后把打印的两行填进 include/chassis.h，重新烧录，
再跑一次看「每圈假平移」是否收敛到 0（那两个数就是还没消掉的残差）。
"""

import argparse
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.script_api import BackendLink  # noqa: E402

NAME_YAW = "航向"
NAME_FWD = "前向轮里程"
NAME_SIDE = "侧向轮里程"
NAME_LRPM = "左0转速"
NAME_RRPM = "右0转速"
NAME_X = "全局X"
NAME_Y = "全局Y"
WANT = [NAME_YAW, NAME_FWD, NAME_SIDE, NAME_LRPM, NAME_RRPM, NAME_X, NAME_Y]

FENCE_M = 0.5       # 原地转的位移围栏：车不该走远
SETTLE_S = 0.8      # 起转后等多久算进入稳态
TIMEOUT_S = 25.0    # 等转到目标角度的上限
POLL_S = 0.05       # 等转时的轮询周期

def snapshot(bl):
    """一次批量取回，几个量来自同一拍（BackendLink 内部 20ms TTL 缓存）。"""
    return {
        "yaw": bl.latest(NAME_YAW),
        "fwd": bl.latest(NAME_FWD),
        "side": bl.latest(NAME_SIDE),
        "x": bl.latest(NAME_X),
        "y": bl.latest(NAME_Y),
    }


def run_one(bl, v, target_deg, label, out_dir):
    """原地转一圈，整段原始波形落盘。返回 (Δθ度, Δf米, Δs米)。"""
    print("  [%s] 起转 左%+.0f / 右%+.0f pct，目标 %.0f° …" % (label, v, -v, target_deg))
    bl.stop_move()
    bl.sleep(0.4)
    bl.start_log()
    bl.spin_target(v, -v)
    bl.sleep(SETTLE_S)

    a = snapshot(bl)
    last = a
    t_end = time.perf_counter() + TIMEOUT_S
    while time.perf_counter() < t_end:
        bl.check_abort()
        last = snapshot(bl)
        if abs(last["yaw"] - a["yaw"]) >= abs(target_deg):
            break
        if math.hypot(last["x"] - a["x"], last["y"] - a["y"]) > FENCE_M:
            raise RuntimeError("转圈时位移超过 %.2f m，检查打滑/地面/线是否绊住" % FENCE_M)
        bl.sleep(POLL_S)
    b = last

    # 收车后再看终点：上面 b 已是稳态末点，这里只负责把车停下来
    bl.spin_target(0.0, 0.0)
    bl.sleep(0.3)
    bl.end_log()
    seg = os.path.join(out_dir, label)
    bl.print_log(seg + ".csv")
    bl.print_log(seg + ".png")
    bl.stop_move()

    d_yaw = b["yaw"] - a["yaw"]
    d_fwd = b["fwd"] - a["fwd"]
    d_side = b["side"] - a["side"]
    if abs(d_yaw) < 1e-6:
        raise RuntimeError("航向没有变化：检查电机转向/接线，或抬高 -v 转速")

    bl.sleep(0.6)  # 等真正停稳，避免下一方向带着余速起步

    return d_yaw, d_fwd, d_side


def main():
    ap = argparse.ArgumentParser(description="旋转中心（定位轮 offset）实车标定")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("-v", "--volt-pct", type=float, default=25.0,
                    help="原地转的目标转速 pct（默认 25；抖就降，慢就升）")
    ap.add_argument("-t", "--target-deg", type=float, default=360.0,
                    help="每个方向转多少度（默认 360；转得越多 Δθ 的相对误差越小）")
    ap.add_argument("--dir", choices=["both", "cw", "ccw"], default="both",
                    help="cw=只右转，ccw=只左转，both=两向都转取平均（默认）")
    ap.add_argument("--tag", default="run1",
                    help="原始段目录后缀；落 backend/pytest/probe_data/<层>/cal_rotation_center_<tag>/")
    args = ap.parse_args()

    if args.volt_pct <= 0 or args.volt_pct > 100:
        raise ValueError("-v 必须在 0~100 pct")
    if abs(args.target_deg) < 30:
        raise ValueError("-t 至少 30°，太小 Δθ 的相对误差没意义")

    with BackendLink(args.base) as bl:
        out_dir = bl.out_path("cal_rotation_center_%s" % args.tag)   # 每向一段原始 CSV
        os.makedirs(out_dir, exist_ok=True)
        missing = [n for n in WANT if n not in bl.mon_dir]
        if missing:
            sys.exit("监控表缺项: %s\n已有: %s" % (missing, sorted(bl.mon_dir)))

        print("监控表共 %d 项；标定量：%s / %s / %s" % (len(bl.mon_dir), NAME_YAW, NAME_FWD, NAME_SIDE))
        bl.clear_subs()
        bl.sub(WANT)
        bl.sleep(0.3)

        print("\n安全：原地转，位移围栏 %.2f m；转前确认 USB 线留足松量。" % FENCE_M)
        print("      若命令无反应，先在脑屏按 button3 交出底盘（manual 期间命令会被拒）。")

        runs = []
        print("\n===== 开始标定 =====")
        if args.dir in ("both", "cw"):
            runs.append(("右转",) + run_one(bl, args.volt_pct, args.target_deg, "右转", out_dir))
        if args.dir in ("both", "ccw"):
            runs.append(("左转",) + run_one(bl, -args.volt_pct, args.target_deg, "左转", out_dir))

    print("\n原始段目录：%s" % out_dir)
    print("（offset 解算 offset_f=-Δf/Δθ、offset_s=+Δs/Δθ 交给日抛分析脚本）")


if __name__ == "__main__":
    main()
