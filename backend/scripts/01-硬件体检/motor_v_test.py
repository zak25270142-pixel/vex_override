#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单台电机伏安特性曲线与启动电压诊断（体检）。

【适用时机：电机尚未接入底盘传动链时】
逐台单独加电，同侧其余电机不参与传动——测到的是这台电机自己的伏安特性。
**装车啮连后不要用本脚本**：那时被测台要拖另两台（0V 短路形成发电制动），工作点
电流约为闭环的 3 倍，`I·R` 项不可忽略，测到的斜率不是纯 `k_e`，与闭环工况错位
（这正是 02 层改用等流搜索的原因）。所以本脚本的定位是装机前体检，不是标定。

走 FastAPI 后端：0x84 给单台电机直加电压（绕过速度环），监控表回读实际电压(V)、
转速(pct)、电流(A)。共同传动链摩擦在 V-v 直线的截距里，比斜率时自动抵消：

  up   细步进升压 0→1.6V（0.02V/档）：每台抽动 / 持续启动电压
  down 粗步进降压 5V→0（0.1V/档，已运转准稳态）：V=c+b·v 拟合，b=dV/dv
  high 2/4/6/8/10/12V 点检：核对实际电压与指令的比例（母线 / 端口健康）

诊断判据：
  抽动 / 持续启动电压明显偏高  → 该台阻力大（齿轮箱、轴承、装配）
  V-v 斜率比明显偏离 1         → 该台与同侧另外两台不同源
  Vact 明显跟不上 Vcmd         → 母线或端口供不上（电池、线、端口）

volt_min / volt_factor 的标定走 02-电机组固有属性/cal_motor_intrinsic.py（装机后、
悬空、等流流形拟合）。本脚本读数与它不同源：启动电压是裸机门槛、斜率比是以 0 号机
为锚的 V-v 斜率，都不要拿来填 MyMotor 字段。

启动电压接近或超过 1.6V 时用 --up-max 2.0 重跑，保证 up 段有持续运动点。

注意：脚本开头先发「停止运动」交出底盘——boot 默认手柄控车(is_busy)，
0x84 在 is_busy 期间被固件静默拒绝，不停车会全录到 0。

先启动后端：cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
再运行：python scripts/01-硬件体检/motor_v_test.py                  # 默认 6 台全测，约 6 分钟
       python scripts/01-硬件体检/motor_v_test.py --motors L0,L1,L2  # 只测一侧
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402
from motor_names import motor_names  # noqa: E402

HIGH_VS = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]


def motor_id(motor: str) -> int:
    # 固件 0x84 编号：0~2=左1~3，3~5=右1~3，越界忽略（robot_and_control.cpp）
    n = int(motor[1])
    return n if motor[0] == "L" else 3 + n


def main() -> None:
    ap = argparse.ArgumentParser(description="单台伏安特性与启动电压诊断：细扫起步 + 粗扫 V-v 斜率")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--motors", default="L0,L1,L2,R0,R1,R2")
    ap.add_argument("--step", type=float, default=0.02, help="升压扫描步长 V（细，扫起步门槛）")
    ap.add_argument("--up-max", type=float, default=1.6, help="升压扫描上限 V")
    ap.add_argument("--vmax", type=float, default=5.0, help="降压扫描起始电压 V（跑动 V-v 线）")
    ap.add_argument("--down-step", type=float, default=0.1, help="降压扫描步长 V（粗）")
    ap.add_argument("--hold", type=float, default=0.35)
    ap.add_argument("--settle", type=float, default=0.3,
                    help="换档后先等多久再开始记录 s（排掉起步/降档瞬态；"
                         "不加它窗内均值会被瞬态稀释，启动电压随 hold 漂移）")
    ap.add_argument("--tag", default="run1",
                    help="原始段目录后缀；落 backend/pytest/probe_data/<层>/motor_v_<tag>/")
    args = ap.parse_args()

    motors = [m.strip().upper() for m in args.motors.split(",") if m.strip()]
    up_vs = [round(i * args.step, 3) for i in range(int(args.up_max / args.step) + 1)]
    down_vs = [round(args.vmax - i * args.down_step, 3)
               for i in range(int(args.vmax / args.down_step) + 1)]
    rows = []

    with BackendLink(args.base) as bl:
        out_dir = bl.out_path("motor_v_%s" % args.tag)   # 每段一个原始 CSV
        os.makedirs(out_dir, exist_ok=True)
        bl.stop_move()   # 交出底盘：is_busy 期间 0x84 被固件静默拒绝（boot 默认手柄控车）
        bl.sleep(0.3)
        # 开环直设电压要求速度环空闲：环活着会每 10ms 覆盖 0x84，读到的是被扶住的假数据
        st_l, st_r = bl.latest("左环状态"), bl.latest("右环状态")
        if st_l != 0 or st_r != 0:
            print(f"[warn] 速度环状态非 Stopped（左={st_l:g} 右={st_r:g}），"
                  f"0x84 会被每拍覆盖，结论不可信")
        names = {m: motor_names(m) for m in motors if m[:1] in "LR" and m[1:].isdigit()}
        unknown = [m for m in motors if m not in names or names[m]["rpm"] not in bl.mon_dir]
        if unknown:
            sys.exit("监控表里没有这些电机: %s" % unknown)

        def scan(motor: str, phase: str, voltages: list) -> None:
            """逐档扫描：每档一段独立记录，end_log 后取本段统计（升压/降压/点检共用）。"""
            ids = names[motor]
            for v in voltages:
                bl.set_volt(motor_id(motor), v)
                bl.sleep(args.settle)   # 先放掉换档瞬态
                bl.start_log()          # 只把稳态段记进来
                bl.sleep(args.hold)
                bl.end_log()
                seg = os.path.join(out_dir, "%s_%s_%.2f" % (motor, phase, v))
                bl.print_log(seg + ".csv")
                bl.print_log(seg + ".png")
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

        bl.drive_volt(0.0)      # 起始全清零（0V=惰行，不刹车）
        bl.sleep(1.0)

        for motor in motors:
            bl.clear_subs()                                    # 记录只挂当前这台，日志不爆
            bl.sub(list(names[motor].values()))
            bl.sleep(0.2)

            print("\n===== %s 升压扫描（静止起步，细档找持续启动电压）=====" % motor)
            scan(motor, "up", up_vs)

            # 降到0歇一下，再给满建立连续运转，然后粗档降压扫 V-v 斜率
            bl.set_volt(motor_id(motor), 0.0)
            bl.sleep(0.8)
            bl.set_volt(motor_id(motor), args.vmax)
            bl.sleep(0.7)
            print("===== %s 降压扫描（运行中，V-v 斜率）=====" % motor)
            scan(motor, "down", down_vs)

            bl.set_volt(motor_id(motor), 0.0)
            bl.sleep(0.8)
            print("===== %s 高压点检（Vact 对 Vcmd）=====" % motor)
            scan(motor, "high", HIGH_VS)

            bl.set_volt(motor_id(motor), 0.0)
            bl.sleep(0.5)

    print("\n原始段目录：%s" % out_dir)
    print("（V-v 斜率与启动电压的解算交给日抛分析脚本读这些 CSV）")



if __name__ == "__main__":
    main()
