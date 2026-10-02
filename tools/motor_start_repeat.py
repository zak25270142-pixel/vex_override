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
"""

import sys
import time
import argparse
# 不引 statistics：沙箱环境可能拦截其 pyc 写入，中位数自己算即可

sys.path.insert(0, sys.path[0])
from motor_v_test import probe_port, Link, find_motor_items  # 复用连接与目录解析


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def sustained(stats):
    # 持续转动判据：窗内平均|转速|>=1.5pct，且至少70%的样本转速不低于1pct
    # 只靠均值会把"冲一格就卡死"的瞬时抽动误判成启动
    if stats is None or stats["n"] < 8:
        return False
    return stats["abs_mean"] >= 1.5 and stats["ratio_ge_1"] >= 0.7


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="auto")
    ap.add_argument("--motors", default="L0,L1,L2,L3,R0,R1,R2,R3")
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--step", type=float, default=0.02)
    ap.add_argument("--vstart", type=float, default=0.20)
    ap.add_argument("--vmax", type=float, default=1.20)
    ap.add_argument("--hold", type=float, default=0.40)
    args = ap.parse_args()

    port = probe_port() if args.port == "auto" else args.port
    if not port:
        print("没找到会回 Pong 的 V5 User 口")
        sys.exit(1)

    link = Link(port)
    time.sleep(0.3)
    directory = link.fetch_monitor_dir()
    print("监控表共 %d 项" % len(directory))
    items = find_motor_items(directory)
    motors = args.motors.split(",")
    if any(None in items[m].values() for m in motors):
        print("监控项映射不完整：%s" % items)
        sys.exit(1)

    results = {m: [] for m in motors}   # 每次试验的持续启动电压（未启动记 None）
    twitches = {m: [] for m in motors}  # 每次试验第一次明显抽动的电压

    try:
        for mid in range(8):
            link.set_volt(mid, 0.0)
        time.sleep(1.0)

        voltages = [round(args.vstart + i * args.step, 3)
                    for i in range(int((args.vmax - args.vstart) / args.step) + 1)]

        for motor in motors:
            side, n = motor[0], int(motor[1])
            motor_id = n if side == "L" else 4 + n
            ids = items[motor]

            link.unsubscribe_all()
            time.sleep(0.1)
            for key in ("vact", "rpm", "cur"):
                link.subscribe(ids[key])
            time.sleep(0.2)

            print("\n===== %s 重复启动试验（%d 次）=====" % (motor, args.trials))
            for trial in range(1, args.trials + 1):
                # 随机化静止位置：短冲一下再惰行，停在哪个齿槽全凭运气
                link.set_volt(motor_id, 0.9)
                time.sleep(0.12)
                link.set_volt(motor_id, 0.0)
                time.sleep(0.9)

                start_v = None
                twitch_v = None
                vact_at_start = None
                for v in voltages:
                    t0 = time.perf_counter()
                    link.set_volt(motor_id, v)
                    time.sleep(args.hold)
                    t1 = time.perf_counter()
                    ws = t0 + args.hold * 0.30  # 只看后70%窗口
                    st_r = link.window_stats(ids["rpm"], ws, t1)
                    st_v = link.window_stats(ids["vact"], ws, t1)
                    # 抽动：窗内峰值过3pct（越过齿槽动了一下但没转起来）
                    if twitch_v is None and st_r is not None and st_r["abs_max"] >= 3.0:
                        twitch_v = v
                    if st_r is not None:
                        moving = sum(1 for x in _samples(link, ids["rpm"], ws, t1) if abs(x) >= 1.0)
                        st_r["ratio_ge_1"] = moving / st_r["n"]
                    if sustained(st_r):
                        start_v = v
                        vact_at_start = None if st_v is None else st_v["abs_mean"]
                        break

                results[motor].append(start_v)
                twitches[motor].append(twitch_v)
                print("  第%d次: %s  抽动=%sV  启动时Vact=%s"
                      % (trial,
                         ("持续启动 %.2fV" % start_v) if start_v is not None else "到%.2fV仍未持续" % args.vmax,
                         ("%.2f" % twitch_v) if twitch_v is not None else "无",
                         ("%.3f" % vact_at_start) if vact_at_start is not None else "-"))

                link.set_volt(motor_id, 0.0)
                time.sleep(0.4)
    finally:
        for mid in range(8):
            try:
                link.set_volt(mid, 0.0)
            except Exception:
                pass
        try:
            link.unsubscribe_all()
        except Exception:
            pass
        link.close()

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


def _samples(link, idx, t0, t1):
    return [v for (t, i, v) in link.samples if i == idx and t0 <= t <= t1]


if __name__ == "__main__":
    main()
