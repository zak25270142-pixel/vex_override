#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐电机体检：每台单独加 3V / 0.5s，读转速 + 电流判断 堵转 / 空转 / 断线。

记录按段进行：每台一段独立曲线（start_log → 设值 → 保持 → end_log），段末取本段统计。
先启动后端：cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
再运行：python scripts/01-硬件体检/motor_check.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402


def plot_check(path, rows):
    """两栏：各台平均转速 / 平均电流，异常台标红——一眼看出哪台空转或断线。"""
    from plot_util import subplots, save

    fig, axes = subplots(2)
    names = [r["motor"] for r in rows]
    colors = ["tab:red" if r["state"] != "stall(ok)" else "tab:blue" for r in rows]

    ax = axes[0][0]
    ax.bar(names, [r["rpm"] for r in rows], color=colors)
    ax.axhline(20, color="gray", ls=":", lw=1)
    ax.text(0, 21, "空转判据 20pct", fontsize=8, color="gray")
    ax.set_title("平均转速（|均值|>20pct 记 FREE-SPIN?）")
    ax.set_ylabel("rpm / pct")
    ax.grid(True, alpha=0.3)

    ax = axes[0][1]
    ax.bar(names, [r["cur"] for r in rows], color=colors)
    ax.axhline(0.05, color="gray", ls=":", lw=1)
    ax.text(0, 0.06, "断线判据 0.05A", fontsize=8, color="gray")
    ax.set_title("平均电流（<0.05A 记 DISCONNECT?）")
    ax.set_ylabel("I / A")
    ax.grid(True, alpha=0.3)
    save(fig, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="逐电机体检（单台加电看转速/电流）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--volt", type=float, default=3.0, help="单台加电电压（V）")
    ap.add_argument("--hold", type=float, default=0.5, help="每台保持时间（秒）")
    ap.add_argument("--csv", default="motor_check.csv",
                    help="结果 CSV 文件名，空字符串跳过；落 backend/pytest/probe_data/<层>/")
    ap.add_argument("--png", default="motor_check.png",
                    help="结果图文件名，空字符串跳过；落 backend/pytest/probe_data/<层>/")
    args = ap.parse_args()

    rpm_names = ["%s%d转速" % (s, n) for s in "左右" for n in range(3)]
    cur_names = ["%s%d电流" % (s, n) for s in "左右" for n in range(3)]
    pos_names = ["全局X", "全局Y", "航向"]

    # with 退出时 __exit__ 自动收车收尾（6电机0V / 刹车 / 停记录 / 退订 / 断连接）
    with BackendLink(args.base) as bl:
        # 开环直设电压要求速度环空闲：环活着会每 10ms 覆盖 0x84，读到的是被扶住的假数据
        st_l, st_r = bl.latest("左环状态"), bl.latest("右环状态")
        if st_l != 0 or st_r != 0:
            print(f"[warn] 速度环状态非 Stopped（左={st_l:g} 右={st_r:g}），"
                  f"0x84 会被每拍覆盖，结论不可信")
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
        if args.csv and rows:
            bl.save_csv(args.csv, rows)
        if args.png and rows:
            plot_check(bl.out_path(args.png), rows)

    bad = [r["motor"] for r in rows if r["state"] != "stall(ok)"]
    print("\n判定口径（都用幅值均值，不用有符号均值——抖动会正负抵消成 0）：")
    print("  无数据=样本<5；空转=平均|转速|>20pct；断线=平均|电流|<0.05A；其余记 stall(ok)")
    print("异常电机：%s" % (bad if bad else "无"))


if __name__ == "__main__":
    main()