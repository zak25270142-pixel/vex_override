#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐电机体检：每台单独加 3V / 0.5s，读转速 + 电流判断 堵转 / 空转 / 断线。

记录按段进行：每台一段独立曲线（start_log → 设值 → 保持 → end_log），段末取本段统计。
先启动后端：cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
再运行：python scripts/01-硬件体检/motor_check.py
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))  # 脚本共用工具目录
from app.script_api import BackendLink  # noqa: E402
from seg_stats import seg_stats  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="逐电机体检（单台加电看转速/电流）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--volt", type=float, default=3.0, help="单台加电电压（V）")
    ap.add_argument("--hold", type=float, default=0.5, help="每台保持时间（秒）")
    ap.add_argument("--tag", default="run1",
                    help="原始段目录后缀；落 backend/pytest/probe_data/<层>/motor_check_<tag>/")
    args = ap.parse_args()

    rpm_names = ["%s%d转速" % (s, n) for s in "左右" for n in range(3)]
    cur_names = ["%s%d电流" % (s, n) for s in "左右" for n in range(3)]
    pos_names = ["全局X", "全局Y", "航向"]

    # with 退出时 __exit__ 自动收车收尾（刹车 / 停记录 / 退订 / 断连接）
    with BackendLink(args.base) as bl:
        out_dir = bl.out_path("motor_check_%s" % args.tag)   # 每台一段原始 CSV
        os.makedirs(out_dir, exist_ok=True)
        # 0x84 组开环电压会自己把速度环踹开并保持（open_loop 态），不需要先停车。
        # 这里只是提醒"上一次没收车"，不影响本次结论。
        st_l, st_r = bl.latest("左环状态"), bl.latest("右环状态")
        if st_l != 0 or st_r != 0:
            print(f"[note] 速度环状态非 Stopped（左={st_l:g} 右={st_r:g}），"
                  f"说明上一次没收车；本脚本会自行接管，不影响结论")
        bl.sub(rpm_names + cur_names + pos_names)  # 登记要记录的项
        rows = []
        for mid in range(6):
            name = "%s%d" % ("L" if mid < 3 else "R", mid % 3)
            bl.stop_move()
            bl.sleep(0.15)
            bl.start_log()  # 每台一段独立曲线
            bl.set_volt(mid, args.volt)
            bl.sleep(args.hold)
            bl.end_log()
            seg = os.path.join(out_dir, name)
            bl.print_log(seg + ".csv")
            bl.print_log(seg + ".png")

            st = seg_stats(seg + ".csv",
                           [rpm_names[mid], cur_names[mid], *pos_names])
            st_r = st[rpm_names[mid]]
            st_i = st[cur_names[mid]]
            pos = st

            bl.set_volt(mid, 0.0)
            bl.stop_move()
            bl.sleep(0.25)

            n_rpm = int(st_r.get("n") or 0)
            rpm = st_r["mean"] or 0.0            # 有符号均值（备查：净转方向）
            rpm_abs = st_r["abs_mean"] or 0.0    # 幅值均值：来回抖动时它为大、有符号均值抵消成 0
            cur = st_i["mean"] or 0.0
            cur_abs = st_i["abs_mean"] or 0.0
            # 先卡样本数：断流时 mean 会被拉成 0，直接按堵转判就是漏判
            if n_rpm < 5:
                state = "NO-DATA?"
            elif rpm_abs > 20:
                state = "FREE-SPIN?"
            elif cur_abs < 0.05:
                state = "DISCONNECT?"
            else:
                state = "stall(ok)"
            rows.append({
                "motor": name,
                "vcmd": args.volt,
                "rpm": rpm,
                "abs_rpm": rpm_abs,
                "cur": cur,
                "cur_abs": cur_abs,
                "x": pos["全局X"]["mean"] or 0.0,
                "y": pos["全局Y"]["mean"] or 0.0,
                "yaw": pos["航向"]["mean"] or 0.0,
                "samples": n_rpm,
                "state": state,
            })
            print(
                "motor%d (%s): |rpm|=%6.1f(rpm=%+6.1f) |I|=%4.2fA  %-12s "
                "x=%+.3f y=%+.3f yaw=%+.1f  (n=%d)"
                % (mid, name, rpm_abs, rpm, cur_abs, state,
                   rows[-1]["x"], rows[-1]["y"], rows[-1]["yaw"], n_rpm)
            )
    print("\n原始段目录：%s" % out_dir)
    print("判定口径（都用幅值均值，不用有符号均值——抖动会正负抵消成 0）：")
    print("  无数据=样本<5；空转=平均|转速|>20pct；断线=平均|电流|<0.05A；其余记 stall(ok)")
    print("异常电机：%s" % (bad if bad else "无"))


if __name__ == "__main__":
    main()