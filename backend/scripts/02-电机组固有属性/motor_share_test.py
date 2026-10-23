# -*- coding: utf-8 -*-
"""
motor_share_test.py —— 闭环载荷分流标定 volt_factor（车轮悬空，3 台同速度环里跑）。

1:1 啮连的 3 台电机被同一根轮轴锁死：单台伏安法会被端口电流限制(~2.5A)和
另外两台短路阻尼机的发电制动糊掉（实测窗口只剩 2~6V）。改用闭环分流——
速度环把同侧 3 台摁在同一转速，各台电流分流直接暴露 factor 失配
（同型号电机 k_t 相同，等流=等力矩分担）：
    factor_i 新 = factor_i 旧 × I_锚 / I_i     锚=该侧 0 号机（固件速度反馈源）

若两个目标档算出的 factor 不一致，说明失配以反电动势(k_e)为主、单 factor
是折中——取中速档的值，误差由闭环 I 项兜。

用法（先起后端；车轮悬空）：
    python scripts/02-电机组固有属性/motor_share_test.py                  # 默认 15/30pct 各 4s
    python scripts/02-电机组固有属性/motor_share_test.py --targets 12,25 --hold 5
    python scripts/02-电机组固有属性/motor_share_test.py --cur "L0=1,L1=0.95"   # 已烧过 factor 时在此基础上叠加
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.script_api import BackendLink  # noqa: E402

CUR = [f"{s}{i}电流" for s in "左右" for i in range(3)]
VACT = [f"{s}{i}实际电压" for s in "左右" for i in range(3)]
RPM = {"L": "左0转速", "R": "右0转速"}

# 出图分组：左右各一张。每张内部由后端按单位再拆子图（V / A / pct 各自 y 轴）
PNG_GROUPS = [
    ("左", [f"左{i}实际电压" for i in range(3)] + [f"左{i}电流" for i in range(3)]
     + [RPM["L"], "左指令转速"]),
    ("右", [f"右{i}实际电压" for i in range(3)] + [f"右{i}电流" for i in range(3)]
     + [RPM["R"], "右指令转速"]),
]


def main() -> None:
    ap = argparse.ArgumentParser(description="闭环载荷分流标定 volt_factor（悬空）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--targets", default="15,30", help="目标速度档 pct，逗号分隔")
    ap.add_argument("--hold", type=float, default=4.0, help="每档稳态窗时长 s")
    ap.add_argument("--settle", type=float, default=1.0, help="每档起稳等待 s")
    ap.add_argument("--tag", default="run1",
                    help="原始段目录后缀；落 backend/pytest/probe_data/<层>/motor_share_<tag>/")
    args = ap.parse_args()

    targets = [float(t) for t in args.targets.split(",") if t.strip()]

    want = CUR + VACT + [RPM["L"], RPM["R"], "左指令转速", "右指令转速"]
    with BackendLink(args.base) as bl:
        out_dir = bl.out_path("motor_share_%s" % args.tag)   # 每档一段原始 CSV
        os.makedirs(out_dir, exist_ok=True)
        bl.stop_move()   # 交出底盘：is_busy 期间 0x85 被固件拒绝（boot 默认手柄控车）
        bl.sleep(0.3)
        bl.clear_subs()
        bl.sub(want)
        bl.sleep(0.3)

        results = {}
        for tgt in targets:
            print(f"\n===== 目标 {tgt:g}pct（双侧同目标，稳态窗 {args.hold:g}s）=====")
            bl.spin_target(tgt, tgt)
            bl.sleep(args.settle)
            bl.start_log()
            bl.sleep(args.hold)
            bl.end_log()
            seg = os.path.join(out_dir, "t%g" % tgt)
            bl.print_log(seg + ".csv")
            for tag, names in PNG_GROUPS:
                bl.v.print_log("%s_%s.png" % (seg, tag), names)
            bl.spin_target(0.0, 0.0)
            bl.sleep(0.3)
            st = bl.stats(want)

            def m(name):
                d = st.get(name) or {}
                return d.get("mean") or 0.0

            row = {}
            for side in "LR":
                ia = [m(f"{'左' if side == 'L' else '右'}{i}电流") for i in range(3)]
                va = [m(f"{'左' if side == 'L' else '右'}{i}实际电压") for i in range(3)]
                row[side] = (ia, va)
                print("  %s侧 转速 %6.2f  I=[%5.2f %5.2f %5.2f]A  分流比=[1:%.3f:%.3f]  Vact均=%.2fV"
                      % ("左" if side == "L" else "右", m(RPM[side]),
                         ia[0], ia[1], ia[2], ia[1] / ia[0] if ia[0] else 0,
                         ia[2] / ia[0] if ia[0] else 0, sum(va) / 3))
            results[tgt] = row

        bl.stop_move()
        bl.sleep(0.2)

    print("\n原始段目录：%s" % out_dir)
    print("（factor 的解算交给日抛分析脚本读这些 CSV）")


if __name__ == "__main__":
    main()
