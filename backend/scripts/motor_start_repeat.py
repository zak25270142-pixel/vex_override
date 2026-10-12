# -*- coding: utf-8 -*-
"""
单电机"静止启动电压"重复试验。

单次爬坡扫出来的启动电压受齿槽位置影响很大（转子恰好停在不同位置，
需要的启动电压能差两三个量化档），所以每台电机重复多次：
  每次先短冲再惰行，让转子随机停在某个齿槽位置，
  然后从 0.2V 起 0.02V 一档爬坡，每档保持 0.4s；
  用"后半窗里大部分样本都在转"判定持续启动（瞬时冲一下不算），
  一旦持续转动立刻收电压、随机化停位，开始下一次。
最后给出每台电机启动电压的最小值/中位值/最大值分布。

记录按段进行：每档一段独立曲线（start_log → 设值 → 保持 → end_log），
段结束后立即取本段统计做判定；原始数据需要时按段 print_log 存盘。

运行前先启动后端：
    cd backend
    uvicorn app.main:app --host 127.0.0.1 --port 8000
然后：
    python scripts/motor_start_repeat.py
"""

import sys
import argparse
# 不引 statistics：沙箱环境可能拦截其 pyc 写入，中位数自己算即可
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # backend 根，供 import app
from app.script_api import BackendLink  # noqa: E402
from motor_names import motor_names  # noqa: E402


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def sustained(stats):
    # 持续转动判据：窗内平均|转速|>=1.5pct，且至少70%的样本转速不低于1pct
    # 只靠均值会把"冲一格就卡死"的瞬时抽动误判成启动
    if stats is None or stats["n"] < 8:
        return False
    ratio_ge_1 = (stats.get("ratio_ge") or {}).get("1.0", 0.0) or 0.0
    return (stats["abs_mean"] or 0.0) >= 1.5 and ratio_ge_1 >= 0.7


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--motors", default="L0,L1,L2,L3,R0,R1,R2,R3")
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--step", type=float, default=0.02)
    ap.add_argument("--vstart", type=float, default=0.20)
    ap.add_argument("--vmax", type=float, default=1.20)
    ap.add_argument("--hold", type=float, default=0.40)
    ap.add_argument("--csv", default="pytest/motor_start_repeat.csv",
                    help="每次试验一行，空字符串跳过")
    ap.add_argument("--log", default="", help="非空则每段原始记录导出到该目录（每段一对 json/csv）")
    args = ap.parse_args()

    motors = args.motors.split(",")
    results = {m: [] for m in motors}   # 每次试验的持续启动电压（未启动记 None）
    twitches = {m: [] for m in motors}  # 每次试验第一次明显抽动的电压
    rows = []  # 每次试验一行，最后写 CSV

    # with 退出时 __exit__ 自动收车收尾（8电机0V / 刹车 / 停记录 / 退订 / 断连接）
    with BackendLink(args.base) as bl:
        print("监控表共 %d 项" % len(bl.mon_dir))
        alias = {m: motor_names(m) for m in motors if m[:1] in "LR" and m[1:].isdigit()}
        missing = ["%s:%s" % (m, k) for m in motors for k in ("rpm", "vact", "cur")
                   if m not in alias or alias[m][k] not in bl.mon_dir]
        if missing:
            print("监控项映射不完整：%s" % missing)
            sys.exit(1)

        bl.drive_volt(0.0)  # 8 台全部 0V 起步
        bl.sleep(1.0)

        voltages = [round(args.vstart + i * args.step, 3)
                    for i in range(int((args.vmax - args.vstart) / args.step) + 1)]

        for motor in motors:
            side, n = motor[0], int(motor[1])
            motor_id = n if side == "L" else 4 + n
            names = alias[motor]   # {'vact'/'rpm'/'cur': 中文名}（脚本侧 motor_names 维护）

            bl.clear_subs()
            bl.sub(list(names.values()))
            bl.sleep(0.2)

            print("\n===== %s 重复启动试验（%d 次）=====" % (motor, args.trials))
            for trial in range(1, args.trials + 1):
                # 随机化静止位置：短冲一下再惰行，停在哪个齿槽全凭运气
                bl.set_volt(motor_id, 0.9)
                bl.sleep(0.12)
                bl.set_volt(motor_id, 0.0)
                bl.sleep(0.9)

                start_v = None
                twitch_v = None
                vact_at_start = None
                # 逐档爬坡：每档一段独立记录，end_log 后取本段统计做判定；
                # sustained 命中即提前 break
                for v in voltages:
                    bl.start_log()             # 每档一段：清上一段缓冲、零点归零
                    bl.set_volt(motor_id, v)
                    bl.sleep(args.hold)
                    bl.end_log()
                    if args.log:
                        bl.print_log("%s/%s_t%d_v%.2f" % (args.log, motor, trial, v))
                    st_r = bl.stats([names["rpm"]], thresholds=[1.0, 3.0])[names["rpm"]]
                    st_v = bl.stats([names["vact"]])[names["vact"]]
                    # 抽动：本段峰值过3pct（越过齿槽动了一下但没转起来）
                    if twitch_v is None and (st_r["abs_max"] or 0.0) >= 3.0:
                        twitch_v = v
                    if sustained(st_r):
                        start_v = v
                        vact_at_start = st_v["abs_mean"]
                        break

                results[motor].append(start_v)
                twitches[motor].append(twitch_v)
                rows.append({
                    "motor": motor,
                    "trial": trial,
                    "start_v": start_v,
                    "twitch_v": twitch_v,
                    "vact_at_start": vact_at_start,
                    "no_start": start_v is None,
                })
                print("  第%d次: %s  抽动=%sV  启动时Vact=%s"
                      % (trial,
                         ("持续启动 %.2fV" % start_v) if start_v is not None else "到%.2fV仍未持续" % args.vmax,
                         ("%.2f" % twitch_v) if twitch_v is not None else "无",
                         ("%.3f" % vact_at_start) if vact_at_start is not None else "-"))

                bl.set_volt(motor_id, 0.0)  # 本轮收电压
                bl.sleep(0.4)

        if args.csv and rows:
            bl.save_csv(args.csv, rows)

    print("\n================ 启动电压分布（指令电压 V）================")
    print("电机  最小值  中位值  最大值  未启动次数  各次抽动电压")
    for motor in motors:
        starts = [v for v in results[motor] if v is not None]
        none_n = results[motor].count(None)
        if starts:
            print("%s   %5.2f   %5.2f   %5.2f   %d          %s"
                  % (motor, min(starts), median(starts), max(starts), none_n,
                     ["%.2f" % v if v is not None else ">" for v in twitches[motor]]))
        else:
            print("%s   全程未持续启动（>%.2fV），抽动: %s"
                  % (motor, args.vmax,
                     ["%.2f" % v if v is not None else ">" for v in twitches[motor]]))


if __name__ == "__main__":
    main()