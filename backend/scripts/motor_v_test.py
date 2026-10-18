# -*- coding: utf-8 -*-
"""
单电机电压特性实测脚本（电机无负载、未装轮胎时使用）。

走 FastAPI 后端：0x84 给单台电机直加电压（绕过速度环），监控表回读
实际电压(V)、转速(pct，红盒100rpm=100%)、电流(A)。每台电机做三组：
  up   升压扫描 0→1.2V 步长0.02V：找首次转动与持续启动电压
  down 降压扫描 1.2→0V：找运行中停转电压（迟滞）
  high 2/4/6/8/10/12V 点检：核对实际电压与指令电压的比例

记录按段进行：每档一段独立曲线（start_log → 设值 → 保持 → end_log），段末取本段统计。

先启动后端：cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
再运行：python scripts/motor_v_test.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.script_api import BackendLink  # noqa: E402
from motor_names import motor_names  # noqa: E402

HIGH_VS = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]


def motor_id(motor: str) -> int:
    return int(motor[1]) if motor[0] == "L" else 4 + int(motor[1])


def main() -> None:
    ap = argparse.ArgumentParser(description="单电机开环电压特性（升/降压扫描 + 高压点检）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--motors", default="L0,L1,L2,R0,R1,R2")
    ap.add_argument("--step", type=float, default=0.02)
    ap.add_argument("--vmax", type=float, default=1.20)
    ap.add_argument("--hold", type=float, default=0.35)
    ap.add_argument("--csv", default="pytest/motor_v_result.csv", help="结果 CSV，空字符串跳过")
    ap.add_argument("--log", default="", help="非空则每段原始记录导出到该目录（每段一对 json/csv）")
    args = ap.parse_args()

    motors = [m.strip().upper() for m in args.motors.split(",") if m.strip()]
    up_vs = [round(i * args.step, 3) for i in range(int(args.vmax / args.step) + 1)]
    rows = []

    with BackendLink(args.base) as bl:
        names = {m: motor_names(m) for m in motors if m[:1] in "LR" and m[1:].isdigit()}
        unknown = [m for m in motors if m not in names or names[m]["rpm"] not in bl.mon_dir]
        if unknown:
            sys.exit("监控表里没有这些电机: %s" % unknown)

        def scan(motor: str, phase: str, voltages: list) -> None:
            """逐档扫描：每档一段独立记录，end_log 后取本段统计（升压/降压/点检共用）。"""
            ids = names[motor]
            for v in voltages:
                bl.start_log()
                bl.set_volt(motor_id(motor), v)
                bl.sleep(args.hold)
                bl.end_log()
                if args.log:
                    bl.print_log("%s/%s_%s_%.2f" % (args.log, motor, phase, v))
                st = bl.stats(list(ids.values()))
                v_act, rpm, cur = (st[ids[k]] for k in ("vact", "rpm", "cur"))
                rows.append({
                    "motor": motor, "phase": phase, "vcmd": round(v, 4),
                    "vact": v_act["mean"], "vact_abs": v_act["abs_mean"],
                    "pct_abs": rpm["abs_mean"], "pct_max": rpm["abs_max"],
                    "cur": cur["mean"], "n": rpm["n"],
                })
                r = rows[-1]
                print("[%s %-4s] Vcmd=%5.2f  Vact=%6.3f  |rpm|均=%6.2f 峰=%6.2f  I=%5.2f  样本%d"
                      % (motor, phase, r["vcmd"],
                         r["vact"] if r["vact"] is not None else float("nan"),
                         r["pct_abs"] or 0, r["pct_max"] or 0, r["cur"] or 0, r["n"]))

        bl.drive_volt(0.0)      # 起始 8 台全清零（0V=惰行，不刹车）
        bl.sleep(1.0)

        for motor in motors:
            bl.clear_subs()                                    # 记录只挂当前这台，日志不爆
            bl.sub(list(names[motor].values()))
            bl.sleep(0.2)

            print("\n===== %s 升压扫描（静止起步）=====" % motor)
            scan(motor, "up", up_vs)

            # 降到0歇一下，再给满扫描上限建立连续运转，然后降压扫停转点
            bl.set_volt(motor_id(motor), 0.0)
            bl.sleep(0.8)
            bl.set_volt(motor_id(motor), args.vmax)
            bl.sleep(0.7)
            print("===== %s 降压扫描（运行中停转点）=====" % motor)
            scan(motor, "down", list(reversed(up_vs)))

            bl.set_volt(motor_id(motor), 0.0)
            bl.sleep(0.8)
            print("===== %s 高压点检（Vact 对 Vcmd）=====" % motor)
            scan(motor, "high", HIGH_VS)

            bl.set_volt(motor_id(motor), 0.0)
            bl.sleep(0.5)

        if args.csv and rows:
            bl.save_csv(args.csv, rows)

    # ---------- 汇总结论 ----------
    print("\n================ 汇总 ================")
    print("判定口径：抽动=升压窗内转速峰值≥1pct；持续启动=窗内平均|转速|≥2pct；")
    print("停转点=降压过程中平均|转速|跌破2pct的那一档（取相邻两档中点）")
    for motor in motors:
        up = [r for r in rows if r["motor"] == motor and r["phase"] == "up"]
        down = [r for r in rows if r["motor"] == motor and r["phase"] == "down"]
        twitch = next((r["vcmd"] for r in up if (r["pct_max"] or 0) >= 1.0), None)
        start = next((r["vcmd"] for r in up if (r["pct_abs"] or 0) >= 2.0), None)
        stop_v = None
        for i, r in enumerate(down):
            if (r["pct_abs"] or 0) < 2.0:
                # down 从高到低，第一次跌破；上一档还在转
                stop_v = round((r["vcmd"] + down[i - 1]["vcmd"]) / 2, 3) if i > 0 else r["vcmd"]
                break
        print("%s: 抽动≈%sV  持续启动≈%sV  运行停转≈%sV"
              % (motor,
                 twitch if twitch is not None else ">%.2f" % args.vmax,
                 start if start is not None else ">%.2f" % args.vmax,
                 stop_v if stop_v is not None else "<%.2f" % (0 if not down else down[-1]["vcmd"])))
        for r in (r for r in rows if r["motor"] == motor and r["phase"] == "high"):
            print("    Vcmd=%5.1f -> Vact=%6.3fV  偏差%+5.3fV  |转速|=%6.1fpct  I=%.2fA"
                  % (r["vcmd"], r["vact"] or 0, (r["vact"] or 0) - r["vcmd"],
                     r["pct_abs"] or 0, r["cur"] or 0))


if __name__ == "__main__":
    main()