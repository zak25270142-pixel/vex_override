#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐电机体检：每台单独加 3V / 0.5s，读转速 + 电流判断 堵转 / 空转 / 断线。

记录按段进行：每台一段独立曲线（start_log → 设值 → 保持 → end_log），段末取本段统计。
先启动后端：cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
再运行：python scripts/motor_check.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.script_api import BackendLink  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="逐电机体检（单台加电看转速/电流）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--volt", type=float, default=3.0, help="单台加电电压（V）")
    ap.add_argument("--hold", type=float, default=0.5, help="每台保持时间（秒）")
    ap.add_argument("--csv", default="pytest/motor_check.csv", help="结果 CSV，空字符串跳过")
    args = ap.parse_args()

    rpm_names = ["%s%d转速" % (s, n) for s in "左右" for n in range(3)]
    cur_names = ["%s%d电流" % (s, n) for s in "左右" for n in range(3)]
    pos_names = ["全局X", "全局Y", "航向"]

    # with 退出时 __exit__ 自动收车收尾（6电机0V / 刹车 / 停记录 / 退订 / 断连接）
    with BackendLink(args.base) as bl:
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

            st_r = bl.stats([rpm_names[mid]])[rpm_names[mid]]
            st_i = bl.stats([cur_names[mid]])[cur_names[mid]]
            pos = bl.stats(pos_names)

            bl.set_volt(mid, 0.0)
            bl.stop_move()
            bl.sleep(0.25)

            rpm, cur = st_r["mean"] or 0.0, st_i["mean"] or 0.0
            if abs(rpm) > 20:
                state = "FREE-SPIN?"
            elif abs(cur) < 0.05:
                state = "DISCONNECT?"
            else:
                state = "stall(ok)"
            rows.append({
                "motor": name,
                "vcmd": args.volt,
                "rpm": rpm,
                "abs_rpm": st_r["abs_mean"] or 0.0,
                "cur": cur,
                "x": pos["全局X"]["mean"] or 0.0,
                "y": pos["全局Y"]["mean"] or 0.0,
                "yaw": pos["航向"]["mean"] or 0.0,
                "samples": st_r["n"],
                "state": state,
            })
            print(
                "motor%d (%s): rpm=%6.1f I=%4.2fA  %-12s x=%+.3f y=%+.3f yaw=%+.1f  (n=%d)"
                % (mid, name, rpm, cur, state,
                   rows[-1]["x"], rows[-1]["y"], rows[-1]["yaw"], st_r["n"])
            )
        if args.csv and rows:
            bl.save_csv(args.csv, rows)

    bad = [r["motor"] for r in rows if r["state"] != "stall(ok)"]
    print("\n判定：空转=平均|转速|>20pct；断线=平均电流<0.05A；其余记 stall(ok)")
    print("异常电机：%s" % (bad if bad else "无"))


if __name__ == "__main__":
    main()