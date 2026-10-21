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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402


def plot_share(path, results):
    """两栏：每档三台电流并排——factor 标对了就三根等高。"""
    from plot_util import subplots, save, legend

    targets = sorted(results.keys())
    fig, axes = subplots(2)
    for ax, sd in zip(axes[0], "LR"):
        cn = "左" if sd == "L" else "右"
        w = 0.8 / 3.0
        for k, tgt in enumerate(targets):
            for i, v in enumerate(results[tgt][sd][0]):   # (电流三台, 电压三台)
                ax.bar(k + i * w, v, width=w,
                       label="%s%d" % (cn, i) if k == 0 else None)
        ax.set_xticks([k + 0.4 for k in range(len(targets))],
                      ["%gpct" % t for t in targets])
        ax.set_ylabel("电流 / A")
        ax.set_title("%s侧 各台电流（等流则三根等高）" % cn)
        ax.grid(True, alpha=0.3, axis="y")
        legend(ax, fontsize=8)
    save(fig, path)


CUR = [f"{s}{i}电流" for s in "左右" for i in range(3)]
VACT = [f"{s}{i}实际电压" for s in "左右" for i in range(3)]
RPM = {"L": "左0转速", "R": "右0转速"}


def main() -> None:
    ap = argparse.ArgumentParser(description="闭环载荷分流标定 volt_factor（悬空）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--targets", default="15,30", help="目标速度档 pct，逗号分隔")
    ap.add_argument("--hold", type=float, default=4.0, help="每档稳态窗时长 s")
    ap.add_argument("--settle", type=float, default=1.0, help="每档起稳等待 s")
    ap.add_argument("--cur", default="", help="当前已烧的 factor，如 L0=1,L1=0.95；缺省全按 1")
    ap.add_argument("--png", default="motor_share.png",
                    help="结果图文件名，空字符串跳过；落 backend/pytest/probe_data/<层>/")
    args = ap.parse_args()

    targets = [float(t) for t in args.targets.split(",") if t.strip()]
    cur_f = {}
    for kv in args.cur.split(","):
        if kv.strip():
            k, v = kv.split("=")
            cur_f[k.strip().upper()] = float(v)

    want = CUR + VACT + [RPM["L"], RPM["R"], "左指令转速", "右指令转速"]
    with BackendLink(args.base) as bl:
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

    # ---------- 汇总：各档建议 factor（相对当前已烧值叠加）----------
    print("\n================ factor 建议 ================")
    print("口径：factor_i 新 = factor_i 旧 × I_锚/I_i（锚=0 号机）；两档差异大说明 k_e 主导，取中速档")
    for side, cn in (("L", "左"), ("R", "右")):
        print("\n%s侧（当前值：%s）：" % (cn, {f"{side}{i}": cur_f.get(f"{side}{i}", 1.0) for i in range(3)}))
        for tgt, row in results.items():
            ia, _ = row[side]
            if ia[0] <= 0.05:
                print("  %5gpct: 锚机电流 %.2fA 太小，跳过" % (tgt, ia[0]))
                continue
            sug = {i: cur_f.get(f"{side}{i}", 1.0) * ia[0] / ia[i] for i in range(3)}
            print("  %5gpct: factor = [%.4f, %.4f, %.4f]  （I=[%.2f %.2f %.2f]A）"
                  % (tgt, sug[0], sug[1], sug[2], ia[0], ia[1], ia[2]))
    print("\n填 src/robot_and_control.cpp 的 MyMotor 第 2 字段后烧录；烧录后重跑本脚本复核分流是否均匀。")

    if args.png and results:
        plot_share(bl.out_path(args.png), results)


if __name__ == "__main__":
    main()
