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
import csv
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402

# kf 表 x 节点（与 robot_and_control.cpp 现有表一致；改节点先改这里）
NODES = [0.0, 10.0, 25.0, 50.0, 75.0, 100.0]

RPM = {"L": "左0转速", "R": "右0转速"}
VACT = {"L": "左0实际电压", "R": "右0实际电压"}
CUR = [f"{s}{i}电流" for s in "左右" for i in range(3)]


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def tval(bl: BackendLink, name: str, default: float) -> float:
    """读实车当前调参值，缺项回退默认（默认值只是兜底，以实车为准）。"""
    entry = bl.tun_dir.get(name)
    if entry and entry[1] is not None:
        try:
            return float(entry[1])
        except (TypeError, ValueError):
            pass
    return default


def interp_v(pts: list[tuple[float, float]], x: float):
    """pts=[(转速pct, 实际电压V)] 按转速插值出 x 处的 V_ss；越界钳端并返回标记。"""
    if not pts:
        return None, "无数据"
    if x <= pts[0][0]:
        return pts[0][1], "低于最低实测点，钳端"
    if x >= pts[-1][0]:
        return pts[-1][1], "高于最高实测点，按平台"
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0, ""
            t = (x - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t, ""
    return pts[-1][1], ""


# ------------------------------------------------------------
# 反解模式（--recalc）：不扫车，合并悬空 CSV 与地面探针波形重建 kf 表
# ------------------------------------------------------------
RPM_VACT = {"L": ("左0转速", "左0实际电压"), "R": ("右0转速", "右0实际电压")}


def wave_steady(path: Path):
    """读探针波形长表 → {side: (脉冲内末30%稳态转速均值, 同窗实际电压均值)}；运动太小的档丢弃。

    注意：起步档波形含脉冲后的刹车/观察段（halt 在 end_log 前，vact 会塌到 0），
    稳态窗必须按 vact 高电平段截取脉冲区间，不能直接取整段末尾。
    """
    series: dict[str, list[tuple[float, float]]] = {}
    with path.open("r", encoding="utf-8-sig") as f:  # 后端 save_csv 带 BOM
        for row in csv.DictReader(f):
            t, v = _f(row.get("t_rel_s")), _f(row.get("value"))
            if t is None or v is None:
                continue
            series.setdefault(row["item"], []).append((t, v))
    out = {}
    for side, (rn, vn) in RPM_VACT.items():
        rpm, vact = series.get(rn) or [], series.get(vn) or []
        if len(rpm) < 10 or not vact:
            continue
        vmax = max(v for _, v in vact)
        if vmax < 0.3:
            continue  # 0V 档
        # 脉冲段终点 = vact ≥ 0.5*max 的最后时刻（刹车后电压塌 0，降压档则整段都是脉冲）
        t_end = max(t for t, v in vact if v >= 0.5 * vmax)
        t0 = 0.7 * t_end
        win = [v for t, v in rpm if t0 <= t <= t_end]
        vwin = [v for t, v in vact if t0 <= t <= t_end]
        if len(win) < 5 or not vwin:
            continue
        r = sum(win) / len(win)
        v = sum(vwin) / len(vwin)
        if r >= 3.0:  # 只取持续运动档（起步瞬抖的档进不了表）
            out[side] = (r, v)
    return out


def run_recalc(args) -> None:
    data_dir = Path(__file__).resolve().parent.parent.parent / "pytest" / "probe_data"
    # 扫车模式的 CSV 落在 04-前馈表/ 子目录，recalc 的默认输入跟着走
    air_default = data_dir / "04-前馈表" / "kf_open_open2.csv"
    st, dy, vmin, vmax = args.static, args.dynamic, args.vmin, args.vmax

    def base_of(x: float) -> float:
        return dy + (st - dy) * math.exp(-abs(x) / 3.0) + vmin

    # 悬空实测点（kf_open_sweep 扫车模式的 CSV）
    air_csv = Path(args.air_csv) if args.air_csv else air_default
    air = {"L": [], "R": []}
    with air_csv.open("r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            for side, rk, vk in (("L", "L_rpm", "L_vact"), ("R", "R_rpm", "R_vact")):
                rpm, v = _f(r.get(rk)), _f(r.get(vk))
                if rpm is not None and v is not None and rpm >= 1.0:
                    air[side].append((rpm, v))
    for s in "LR":
        air[s].sort()

    # 地面实测点（探针波形目录，末30%稳态窗）
    ground = {"L": [], "R": []}
    if args.ground:
        for p in sorted(data_dir.rglob(f"{args.ground}_*_wave.csv")):
            for side, (r, v) in wave_steady(p).items():
                ground[side].append((r, v))
        for s in "LR":
            ground[s].sort()

    print("反解基准：静/动摩擦 %.2f/%.2fV  volt_min %.2fV  volt_max %.2fV" % (st, dy, vmin, vmax))
    tables = {}
    for side in "LR":
        print("\n===== %s侧 =====" % ("左" if side == "L" else "右"))
        print("悬空点: %s" % ("  ".join("%.1f→%.2fV" % p for p in air[side]) or "无"))
        print("地面点: %s" % ("  ".join("%.1f→%.2fV" % p for p in ground[side]) or "无"))
        # 重叠区地面-悬空电压差（电缆拖动+滚动阻力的等效增量）
        deltas = []
        for r_g, v_g in ground[side]:
            v_air, note = interp_v(air[side], r_g)
            if v_air is not None and not note and air[side] and r_g <= air[side][-1][0]:
                deltas.append(v_g - v_air)
        dv = sorted(deltas)[len(deltas) // 2] if deltas else None
        if deltas:
            print("重叠区 ΔV(地面-悬空): 中位 %.3fV  范围 [%.3f, %.3f]  n=%d"
                  % (dv, min(deltas), max(deltas), len(deltas)))
        else:
            print("[warn] 无重叠区，估不出 ΔV；高于地面覆盖的节点退回悬空值")

        def vss(x):
            """节点 x 处的稳态电压：地面覆盖内用地实实测，高出部分用悬空+ΔV 外推。"""
            v_g, note_g = interp_v(ground[side], x) if ground[side] else (None, "")
            v_air, _ = interp_v(air[side], x) if air[side] else (None, "")
            if v_g is not None and not note_g:
                return v_g, "地面实测"
            if v_air is not None:
                src = "悬空+ΔV" if dv is not None else "悬空(无ΔV)"
                return v_air + (dv or 0.0), src + ((" " + note_g) if note_g else "")
            if v_g is not None:
                return v_g, "地面" + note_g
            return None, "无数据"

        nodes, prev_y = [], 0.0
        for x in NODES:
            if x == 0.0:
                nodes.append((x, 0.0, ""))
                continue
            v, src = vss(x)
            if v is None:
                nodes.append((x, prev_y, "无数据，沿用上一节点"))
                continue
            b = base_of(x)
            y = min(1.0, max(0.0, (v - b) / (vmax - b)))
            if y < prev_y - 1e-6:
                src += " 低于上一节点(检查数据)"
            nodes.append((x, y, src))
            prev_y = y
        tables[side] = nodes
        print("  %6s %9s %8s %8s  %s" % ("x(pct)", "V_ss(V)", "base(V)", "y", "来源"))
        for x, y, src in nodes:
            v, _ = vss(x)
            print("  %6.1f %9s %8.3f %8.4f  %s"
                  % (x, "%.3f" % v if v is not None else "-", base_of(x), y, src))

    print("\n===== 粘贴到 src/robot_and_control.cpp =====")
    for side, var in (("L", "left_kf_table"), ("R", "right_kf_table")):
        print("static const Vec2f %s[] = {" % var)
        for x, y, _ in tables[side]:
            print("    {%.1ff, %.4ff}," % (x, y))
        print("};")
    gmax = ground["L"][-1][0] if ground["L"] else 0
    print("\n口径：地面覆盖内（约%.0fpct 以下）用实测——低速段就是抖动区，最准；" % gmax)
    print("高于覆盖用悬空+ΔV 外推。烧录后闭环稳态看「左前馈/右前馈」对比 Vcmd 复核。")


def plot_result(path, rows, tables):
    """两栏：悬空 V-转速曲线 / 反解出的 kf 表节点。"""
    from plot_util import subplots, save

    fig, axes = subplots(2)
    ax = axes[0][0]
    ax.plot([r["vcmd"] for r in rows], [r["L_rpm"] or 0 for r in rows], "o-", ms=3, label="左")
    ax.plot([r["vcmd"] for r in rows], [r["R_rpm"] or 0 for r in rows], "s-", ms=3, label="右")
    ax.set_xlabel("指令电压 / V")
    ax.set_ylabel("稳态转速 / pct")
    ax.set_title("悬空稳态：指令电压 → 转速")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    ax = axes[0][1]
    for sd, color, cn in (("L", "tab:blue", "左"), ("R", "tab:orange", "右")):
        pts = tables.get(sd) or []
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "s-", ms=3, color=color, label=cn)
    ax.set_xlabel("|target| / pct")
    ax.set_ylabel("前馈输出（归一）")
    ax.set_title("反解出的 kf 表")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    save(fig, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="悬空组级开环扫描，反解 kf 前馈表")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--volts", default="0,1.0,1.6,2,3,4,5,6,8,10,12",
                    help="逗号分隔的指令电压档（V）")
    ap.add_argument("--settle", type=float, default=1.5, help="每档加压后稳定等待 s")
    ap.add_argument("--hold", type=float, default=1.0, help="稳态窗记录时长 s")
    ap.add_argument("--tag", default=time.strftime("%Y%m%d_%H%M"), help="数据文件标记")
    ap.add_argument("--recalc", action="store_true", help="不扫车：用已有数据（悬空CSV+地面波形）重建 kf 表")
    ap.add_argument("--ground", default="", help="地面开环的探针 tag，如 ground2")
    ap.add_argument("--air-csv", default="",
                    help="悬空开环 CSV；缺省用 probe_data/04-前馈表/kf_open_open2.csv"
                         "（历史文件，用前先确认对应当前车/固件）")
    ap.add_argument("--static", type=float, default=1.5, help="反解用静摩擦垫 V（当前实车值）")
    ap.add_argument("--dynamic", type=float, default=0.8, help="反解用动摩擦垫 V（当前实车值）")
    ap.add_argument("--vmin", type=float, default=0.5, help="反解用 volt_min V")
    ap.add_argument("--vmax", type=float, default=12.0, help="反解用 volt_max V")
    ap.add_argument("--png", default=None,
                    help="结果图（默认 kf_open_<tag>.png，落 probe_data/<层>/）；空字符串跳过")
    args = ap.parse_args()

    if args.recalc:
        run_recalc(args)
        return

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

        # 反解用的实车当前调参值（左右分开；默认值仅兜底，与 --static/--dynamic 取同一组）
        pads = {s: (tval(bl, f"{'左' if s == 'L' else '右'}静摩擦", 1.5),
                    tval(bl, f"{'左' if s == 'L' else '右'}动摩擦", 0.8)) for s in "LR"}
        vmin = {s: tval(bl, f"{'左' if s == 'L' else '右'}0最小电压", 0.5) for s in "LR"}
        vmax = {s: tval(bl, f"{'左' if s == 'L' else '右'}0最大电压", 12.0) for s in "LR"}

        def base_v(side: str, x: float) -> float:
            st, dy = pads[side]
            return dy + (st - dy) * math.exp(-abs(x) / 3.0) + vmin[side]

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

        # ---------- 反解 kf 表 ----------
        print("\n================ kf 表反解 ================")
        tables = {}
        for side, rpm_key, vact_key in (("L", "L_rpm", "L_vact"), ("R", "R_rpm", "R_vact")):
            pts = sorted((r[rpm_key], r[vact_key]) for r in rows
                         if r[rpm_key] is not None and r[vact_key] is not None and r[rpm_key] >= 1.0)
            print("\n%s侧实测点（转速pct → V_ss）：%s"
                  % ("左" if side == "L" else "右",
                     "  ".join("%.1f→%.2fV" % p for p in pts) if pts else "无（没转起来）"))
            nodes = []
            prev_y = 0.0
            for x in NODES:
                if x == 0.0:
                    nodes.append((x, 0.0, ""))
                    continue
                v_ss, note = interp_v(pts, x)
                if v_ss is None:
                    nodes.append((x, prev_y, "无实测点，沿用上一节点"))
                    prev_y = nodes[-1][1]
                    continue
                b = base_v(side, x)
                y = (v_ss - b) / (vmax[side] - b)
                y = min(1.0, max(0.0, y))
                if y < prev_y - 1e-6:
                    note = (note + " " if note else "") + "低于上一节点（应单调不减，请检查数据）"
                nodes.append((x, y, note))
                prev_y = y
            tables[side] = nodes
            print("  %6s %10s %8s %8s  %s" % ("x(pct)", "V_ss(V)", "base(V)", "y", "备注"))
            for x, y, note in nodes:
                b = base_v(side, x)
                v_ss, _ = interp_v(pts, x)
                print("  %6.1f %10s %8.3f %8.4f  %s"
                      % (x, "%.3f" % v_ss if v_ss is not None else "-", b, y, note))

        print("\n===== 粘贴到 src/robot_and_control.cpp（左/右两张表）=====")
        for side, var in (("L", "left_kf_table"), ("R", "right_kf_table")):
            print("static const Vec2f %s[] = {" % var)
            for x, y, _ in tables[side]:
                print("    {%.1ff, %.4ff}," % (x, y))
            print("};")
        print("\n烧录后用监控项「左前馈/右前馈」核对：闭环稳态下实际 Vcmd ≈ 前馈换算电压 + 摩擦 base，")
        print("哪档偏低就把对应节点 y 上调；factor 标定值变化后也按此法复核。")

        out = Path(bl.out_path(f"kf_open_{args.tag}.csv"))
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["vcmd", "L_rpm", "R_rpm", "L_vact", "R_vact", "I_L_max", "I_R_max"])
            w.writeheader()
            w.writerows(rows)
        print("[out] 原始数据: %s" % out)

        # 节点表也要落盘：note 里记着哪些点是"沿用上一节点/钳到端点/非单调"，
        # 只打印到控制台的话，事后拿到的表看不出哪些 y 是造的、哪些是实测的
        node_rows = [{"side": sd, "x": x, "y": round(y, 6), "note": note}
                     for sd in tables for x, y, note in tables[sd]]
        if node_rows:
            bl.save_csv(f"kf_open_{args.tag}_nodes.csv", node_rows,
                        ["side", "x", "y", "note"])
            print("[out] 节点表(带来源备注): kf_open_%s_nodes.csv" % args.tag)

        png = "" if args.png == "" else (args.png or f"kf_open_{args.tag}.png")
        if png:
            plot_result(bl.out_path(png), rows, tables)


if __name__ == "__main__":
    main()
