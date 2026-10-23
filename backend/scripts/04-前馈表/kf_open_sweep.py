# -*- coding: utf-8 -*-
"""
kf_open_sweep.py —— 悬空状态下的组级开环扫描，反解速度环 kf 前馈表。

前提：四轮悬空（无位移风险）、电池满电。整车 6 电机同电压（drive_volt，左右同压），
每档先 settle 再取稳态窗记录：L0/R0 转速（与速度环 target/feedback 同口径 pct）、
两侧实际电压（反解用实际值，电池掉压自己现形）、6 台电流（看齿轮啮合内耗）。

反解原理：开环电压 V_ss 能守住稳态转速 v；闭环里归一输出 o 加到电机上的电压是
    volt = base + o * (volt_max - base)
    base  = 动摩擦 + (静摩擦 - 动摩擦) * exp(-|target|/3) + volt_min
令 volt = V_ss 解出 o，即 kf 表节点 y：
    y(v) = (V_ss(v) - base(v)) / (volt_max - base(v))
base/volt_max/volt_min 全部读取实车当前调参值（左右两侧分开建表，对应固件
left_kf_table / right_kf_table）。x 节点与 robot_and_control.cpp 现有表一致。

产出：终端打印原始扫描表 + 左右两张节点表 + 可直接粘贴的 Vec2f 数组；
      原始数据落 pytest/probe_data/kf_open_<tag>.csv。

用法（先起后端；车悬空、四轮离地、轮子周围无异物）：
    python scripts/04-前馈表/kf_open_sweep.py                       # 默认电压档
    python scripts/04-前馈表/kf_open_sweep.py --volts 0,2,4,8,12 --settle 1.5 --hold 1.0
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402

RPM = {"L": "左0转速", "R": "右0转速"}
VACT = {"L": "左0实际电压", "R": "右0实际电压"}
CUR = [f"{s}{i}电流" for s in "左右" for i in range(3)]


def tval(bl: BackendLink, name: str, default: float) -> float:
    """读实车当前调参值，缺项回退默认（默认值只是兜底，以实车为准）。"""
    entry = bl.tun_dir.get(name)
    if entry and entry[1] is not None:
        try:
            return float(entry[1])
        except (TypeError, ValueError):
            pass
    return default


def main() -> None:
    ap = argparse.ArgumentParser(description="悬空组级开环扫描，反解 kf 前馈表")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--volts", default="0,1.0,1.6,2,3,4,5,6,8,10,12",
                    help="逗号分隔的指令电压档（V）")
    ap.add_argument("--settle", type=float, default=1.5, help="每档加压后稳定等待 s")
    ap.add_argument("--hold", type=float, default=1.0, help="稳态窗记录时长 s")
    ap.add_argument("--tag", default=time.strftime("%Y%m%d_%H%M"), help="数据文件标记")
    args = ap.parse_args()

    volts = [float(v) for v in args.volts.split(",") if v.strip()]
    want = list(RPM.values()) + list(VACT.values()) + CUR
    rows: list[dict] = []

    with BackendLink(args.base) as bl:
        bl.stop_move()   # 交出底盘：is_busy 期间 0x84 被固件静默拒绝（boot 默认手柄控车）
        bl.sleep(0.3)
        # 开环直设电压要求速度环空闲：环活着会每 10ms 覆盖 0x84，读到被扶住的假数据
        st_l, st_r = bl.latest("左环状态"), bl.latest("右环状态")
        if st_l != 0 or st_r != 0:
            print(f"[warn] 速度环状态非 Stopped（左={st_l:g} 右={st_r:g}），"
                  f"0x84 会被每拍覆盖，结论不可信")
        missing = [n for n in want if n not in bl.mon_dir]
        if missing:
            sys.exit("监控表缺少: %s" % missing)

        # 采集时的工况参数（分析脚本要用；默认值仅兜底，以实车为准）
        pads = {s: (tval(bl, f"{'左' if s == 'L' else '右'}静摩擦", 1.5),
                    tval(bl, f"{'左' if s == 'L' else '右'}动摩擦", 0.8)) for s in "LR"}
        vmin = {s: tval(bl, f"{'左' if s == 'L' else '右'}0最小电压", 0.5) for s in "LR"}
        vmax = {s: tval(bl, f"{'左' if s == 'L' else '右'}0最大电压", 12.0) for s in "LR"}

        out_dir = bl.out_path("kf_open_%s" % args.tag)   # 每档一段原始 CSV
        os.makedirs(out_dir, exist_ok=True)
        bl.clear_subs()
        bl.sub(want)
        bl.sleep(0.3)
        print("调参基准：左 静/动摩擦 %.2f/%.2fV  右 %.2f/%.2fV  volt_min %.2fV  volt_max %.2fV"
              % (pads["L"][0], pads["L"][1], pads["R"][0], pads["R"][1], vmin["L"], vmax["L"]))

        print("\n%-6s %8s %8s %8s %8s %8s %8s" % ("Vcmd", "L_rpm", "R_rpm", "L_Vact", "R_Vact", "I_L均", "I_R均"))
        for v in volts:
            bl.drive_volt(v)
            bl.sleep(args.settle)
            bl.start_log()
            bl.sleep(args.hold)
            bl.end_log()
            seg = os.path.join(out_dir, "v%.2f" % v)
            bl.print_log(seg + ".csv")
            bl.print_log(seg + ".png")
            st = bl.stats(want)

            def m(name):
                d = st.get(name) or {}
                return d.get("mean")

            row = {"vcmd": v,
                   "L_rpm": m(RPM["L"]), "R_rpm": m(RPM["R"]),
                   "L_vact": m(VACT["L"]), "R_vact": m(VACT["R"]),
                   "I_L_max": max((st.get(n) or {}).get("mean") or 0.0 for n in CUR[:3]),
                   "I_R_max": max((st.get(n) or {}).get("mean") or 0.0 for n in CUR[3:]),
                   }
            rows.append(row)
            print("%-6.1f %8.2f %8.2f %8.3f %8.3f %8.2f %8.2f"
                  % (v, row["L_rpm"] or 0, row["R_rpm"] or 0,
                     row["L_vact"] or 0, row["R_vact"] or 0, row["I_L_max"], row["I_R_max"]))

        bl.drive_volt(0.0)
        bl.sleep(0.3)

        # 12V 档的电池/母线健康与啮合内耗提示
        top = rows[-1] if rows else None
        if top and top["vcmd"] >= 10 and top["L_vact"] is not None:
            dev = top["vcmd"] - top["L_vact"]
            if dev > 1.0:
                print("\n[warn] 12V 档实际电压只有 %.2fV（掉压 %.2fV）：电池/母线供不上，"
                      "kf 表按实际电压反解仍然成立，但高速档可能够不到 100pct" % (top["L_vact"], dev))
            else:
                print("\n[ok] 12V 档实际电压 %.2fV，母线健康（上次 5.5V 塌陷未复现）" % top["L_vact"])
        if top:
            for side, key in (("左", "I_L_max"), ("右", "I_R_max")):
                if top[key] > 2.0:
                    print("[warn] %s侧 12V 档最大均流 %.2fA：啮合内耗偏大（factor 未标定，正常，"
                          "待闭环后细修）" % (side, top[key]))

    print("\n原始段目录：%s" % out_dir)
    print("（kf 表反解交给日抛分析脚本读这些 CSV）")


if __name__ == "__main__":
    main()
