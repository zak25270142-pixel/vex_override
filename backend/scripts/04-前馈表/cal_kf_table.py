# -*- coding: utf-8 -*-
"""
cal_kf_table.py —— kf 阻力前馈表标定（着地·开环扫压 + 反解；验证模式为闭环且 kp=ki=kd=0）

【定位】
标定左右各自的 kf 前馈表，使得**纯前馈（kp=ki=kd=0）下稳态速度恰好等于 target**。
kf 补偿的是整车阻力（地面摩擦、轮径、载荷），是整车属性，所以必须着地标——
悬空没有阻力，标出来是空的。

【依赖：必须先做完的】
  1. volt_min / volt_factor（悬空·开环，见 cal_motor_intrinsic.py）
  2. 静/动摩擦垫（着地·开环）
反解公式要用这三个量，它们不准，kf 一定歪。

【原理：开环直测 + 反解，不迭代】
开环可以给任意电压、直接读稳态速度，一步得到"速度→所需电压"曲线 V(v)。
稳态时闭环满足：

    V = base + ff · controllable · volt_factor
    base        = friction(v) + volt_min
    controllable = volt_max − base

所以直接反解出该节点的前馈输出：

    y_j = (|V(v_j)| − base(v_j)) / (controllable(v_j) · volt_factor)

（反向扫压时 V 为负，脚本取幅值：表只存 |target| 对应的正值。）
不经过任何迭代，也不需要在线改表（kf 不是调参项，改一次要烧录）。
参数一律取 motors[0]（与速度反馈同台）。

【摩擦垫随速度衰减】
    friction(v) = dynamic_deadzone + (static_deadzone − dynamic_deadzone)·exp(−v/3)
过渡尺度 3pct 是固件硬编码，反解时必须按每个节点各自的 v 算，不能用同一个值。

【验证模式：必须清零 kp/ki/kd】
kp/ki/kd 是干扰项——不清零的话 output 里混着 P、I、D，你看到的速度就不是前馈
一个人的功劳，标定出来的表会被 PID 的补偿量带偏。验证模式会：
把 L_kp/L_ki/L_kd/R_kp/R_ki/R_kd 全部写 0 → 读回确认 → 测完恢复原值。

【前置条件】
  1. 着地，且**有足够长的直线场地**——每档都要跑到稳态，车会持续前进
  2. 电池满电（volt_max 按 12V 算，掉压会让高档位达不到指令电压）
  3. 脚本开头发 stop_move 解除手柄占用

【用法】
  先起后端，然后：
    python scripts/04-前馈表/cal_kf_table.py                          # 扫压 + 反解，出建议表
    python scripts/04-前馈表/cal_kf_table.py --volts "3,4,5,6,7,8,9,10,11,12"
    python scripts/04-前馈表/cal_kf_table.py --nodes "10,25,50,75"     # 自定义节点 pct
  烧录建议表后复查：
    python scripts/04-前馈表/cal_kf_table.py --verify                  # 闭环，kp=ki=kd=0
  反向（车后退）——档位仍写正值、方向交给 --dir，正反各跑一次别混在一档列表里：
    python scripts/04-前馈表/cal_kf_table.py --dir neg --tag neg
    python scripts/04-前馈表/cal_kf_table.py --verify --dir neg --nodes "10,25,50,75,100" --tag negv

【输出】
  CSV  probe_data/cal_kf_<tag>.csv（各档指令/实际电压、电流、稳态转速）
  PNG  probe_data/cal_kf_<tag>.png（V-转速曲线 / 反解出的 kf 表曲线 / 验证偏差）
  控制台：每个节点建议的 y 值，直接填 robot_and_control.cpp 的表

【注意】
  * small_car 的 kf 表口径：y 是**前馈输出值**，ff = target_sign · kf_at(|target|)，
    **不要再乘 target**（全向分支存的是斜率，那边才乘 target，别混）。
  * 最高节点若超出实测速度范围会标记"需外推"，实测跑不到的速度不要硬填。

【反向标定与正反对照】
固件正反向共用一张表（ff = target_sign·kf_at(|target|)），所以反向只作对照、不改表结构。
把正反两次同一节点 x 的 y 相减得 Δy：
  - Δy 各节点差不多 → 恒定平移（坡度/固定偏置），闭环 I 项会完整吸收，不动
  - Δy 随 |target| 单调变大 → 形状差（随速度的阻力不对称），才考虑加第二张表
  - 想分离"场地坡度 vs 车体固有"：掉头 180° 重跑，差值反号=坡度，同号=车
"""

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402

SIDE_CN = {"L": "左", "R": "右"}
ALL_IDS = list(range(6))
# 每组速度环 PID 六个调参项：验证模式清零。
# 注意必须是目录里的中文名——目录帧发的是 Chinese_name（communication.cpp），
# 固件内部的英文 item_name（L_kp 等）在 tun_dir 里查不到，会静默取默认值。
PID_ITEMS = ("左kp", "左ki", "左kd", "右kp", "右ki", "右kd")


def mid(side: str, i: int) -> int:
    return i if side == "L" else 3 + i


def tag_of(i: int) -> str:
    return ("L" if i < 3 else "R") + str(i % 3)


def mon(side: str, i: int, kind: str) -> str:
    return f"{SIDE_CN[side]}{i}{kind}"


WANT = ([mon(s, i, k) for s in "LR" for i in range(3)
         for k in ("指令电压", "实际电压", "电流", "转速")]
        + ["左环状态", "右环状态", "左指令转速", "右指令转速", "左前馈", "右前馈"])


@dataclass
class Sample:
    volt_cmd: float
    vcmd: list
    vact: list
    cur: list
    rpm: list
    tgt: list      # 左右指令转速
    ff: list       # 左右前馈
    rpm_std: list = None   # 六台转速的整段标准差：看"是否真稳"，光看均值看不出来
    rpm_n: list = None     # 六台转速的样本数：看有没有丢包（n 太少说明这段没记到）
    cur_amax: list = None  # 六台电流的 |峰值|：电流保护要看真峰值，不能看窗口均值的最大值


def nz(x, default=float("nan")):
    """None → nan 用于打印。宁可显示 nan 也不要拿 0.0 冒充实测值。"""
    return x if x is not None else default


def measure(bl: BackendLink, volt_cmd: float, settle: float, hold: float) -> Sample:
    bl.sleep(settle)
    bl.start_log()
    bl.sleep(hold)
    bl.end_log()
    st = bl.stats(WANT)

    def m(name: str):
        """整段均值。该项没数据/没样本时返回 None——绝不拿 0.0 冒充实测值。"""
        d = st.get(name) or {}
        v = d.get("mean")
        return None if v is None else float(v)

    def sd(name: str):
        d = st.get(name) or {}
        v = d.get("std")
        return None if v is None else float(v)

    def amax(name: str):
        d = st.get(name) or {}
        v = d.get("abs_max")
        return None if v is None else float(v)

    def cnt(name: str) -> int:
        d = st.get(name) or {}
        return int(d.get("n") or 0)

    return Sample(
        volt_cmd=volt_cmd,
        vcmd=[m(mon(sd, i, "指令电压")) for sd in "LR" for i in range(3)],
        vact=[m(mon(sd, i, "实际电压")) for sd in "LR" for i in range(3)],
        cur=[m(mon(sd, i, "电流")) for sd in "LR" for i in range(3)],
        rpm=[m(mon(sd, i, "转速")) for sd in "LR" for i in range(3)],
        tgt=[m("左指令转速"), m("右指令转速")],
        ff=[m("左前馈"), m("右前馈")],
        rpm_std=[sd(mon(sd, i, "转速")) for sd in "LR" for i in range(3)],
        rpm_n=[cnt(mon(sd, i, "转速")) for sd in "LR" for i in range(3)],
        cur_amax=[amax(mon(sd, i, "电流")) for sd in "LR" for i in range(3)],
    )


def tun(bl: BackendLink, name: str, default: float = 0.0) -> float:
    """读当前调参值（目录缓存，不下位机）"""
    item = bl.tun_dir.get(name)
    if item and item[1] is not None:
        try:
            return float(item[1])
        except (TypeError, ValueError):
            pass
    return default


def friction_at(v: float, st_dz: float, dy_dz: float) -> float:
    """摩擦垫随目标速度衰减（与 my_motor_group.cpp 一致，过渡尺度 3pct 硬编码）"""
    return dy_dz + (st_dz - dy_dz) * math.exp(-abs(v) / 3.0)


def interp_volt_for_speed(pts: list, v_target: float) -> float:
    """由 (电压, 稳态转速) 数据反插值：达到 v_target 需要的电压。

    pts 按转速升序；超出范围返回 None（不外推，让调用方标记）。
    """
    if len(pts) < 2:
        return None
    pts = sorted(pts, key=lambda p: p[1])
    if v_target < pts[0][1] or v_target > pts[-1][1]:
        return None
    for (v0, s0), (v1, s1) in zip(pts, pts[1:]):
        if s0 <= v_target <= s1:
            if abs(s1 - s0) < 1e-9:
                return (v0 + v1) / 2.0
            return v0 + (v1 - v0) * (v_target - s0) / (s1 - s0)
    return None


def plot_result(path: Path, curve: dict, table: dict, verify: list) -> None:
    """三栏汇总图：V-转速曲线 / 反解出的 kf 表 / 验证偏差"""
    from plot_util import subplots, save

    n = 3 if verify else 2
    fig, axes = subplots(n, width=5.6)
    ax = axes[0][0]
    for sd, color in (("L", "tab:blue"), ("R", "tab:orange")):
        pts = curve.get(sd) or []
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "o-", color=color,
                    label=f"{SIDE_CN[sd]}侧")
    ax.set_title("开环稳态：指令电压 → 转速")
    ax.set_xlabel("指令电压 / V")
    ax.set_ylabel("稳态转速 / pct")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    ax = axes[0][1]
    for sd, color in (("L", "tab:blue"), ("R", "tab:orange")):
        pts = table.get(sd) or []
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "s-", color=color,
                    label=f"{SIDE_CN[sd]}侧")
    ax.set_title("反解出的 kf 表（y=前馈输出值）")
    ax.set_xlabel("|target| / pct")
    ax.set_ylabel("前馈输出（归一）")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    if verify:
        ax = axes[0][2]
        xs = [r[1] for r in verify]
        for sd, color, key in (("L", "tab:blue", 2), ("R", "tab:orange", 3)):
            ax.plot(xs, [r[key] for r in verify], "o", color=color,
                    label=f"{SIDE_CN[sd]}侧实测")
        lim = max(xs + [1.0])
        ax.plot([0, lim], [0, lim], "k--", linewidth=1, label="理想(实测=目标)")
        ax.set_title("验证：纯前馈稳态转速 vs 目标")
        ax.set_xlabel("目标 / pct")
        ax.set_ylabel("实测稳态转速 / pct")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)

    save(fig, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="kf 前馈表标定（着地）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--volts", default="3,4,5,6,7,8,9,10,11,12",
                    help="开环扫压档位（V）")
    ap.add_argument("--nodes", default="0,10,25,50,75,100",
                    help="kf 表节点（|target| pct）")
    ap.add_argument("--settle", type=float, default=1.5, help="每档起稳等待 s")
    ap.add_argument("--hold", type=float, default=2.0, help="稳态窗时长 s")
    ap.add_argument("--i-max", type=float, default=2.5, help="电流保护上限 A")
    ap.add_argument("--verify", action="store_true",
                    help="闭环验证模式（强制 kp=ki=kd=0，需先烧录建议表）")
    ap.add_argument("--dir", default="pos",
                    help="标定方向：pos 正向 / neg 反向。neg 时扫压档取负（车后退）、"
                         "验证目标取负；表节点 x 恒为 |target|，不受影响")
    ap.add_argument("--only", default="LR", help="只测哪侧：L / R / LR")
    ap.add_argument("--tag", default="run1", help="输出文件名后缀")
    args = ap.parse_args()

    neg = args.dir.lower().startswith("n")
    # 档位写正值、方向交给 --dir：避免 "--volts -3,..." 被 argparse 当成选项
    volts = [(-abs(float(x)) if neg else abs(float(x)))
             for x in args.volts.split(",") if x.strip()]
    nodes = [float(x) for x in args.nodes.split(",") if x.strip()]
    if args.verify and neg:
        nodes = [-abs(x) for x in nodes]
    sides = [c for c in "LR" if c in args.only.upper()]

    rows = []
    curve = {s: [] for s in sides}
    table = {s: [] for s in sides}
    verify_rows = []

    with BackendLink(args.base) as bl:
        bl.stop_move()          # 着地测试前先收车并解除手柄占用
        bl.sleep(0.4)
        bl.clear_subs()
        bl.sub(WANT)
        bl.sleep(0.4)

        pts = nodes if args.verify else volts
        print("方向：%s（车%s）%s：%s"
              % ("反向" if neg else "正向", "后退" if neg else "前进",
                 "目标" if args.verify else "档位",
                 " ".join("%.0f" % p for p in pts)))
        st_l, st_r = bl.latest("左环状态"), bl.latest("右环状态")
        if st_l != 0 or st_r != 0:
            print(f"[warn] 速度环状态非 Stopped（左={st_l:g} 右={st_r:g}）")

        def collect(s: Sample, phase: str) -> None:
            row = {"phase": phase, "volt_cmd": s.volt_cmd}
            for i in ALL_IDS:
                t = tag_of(i)
                row[f"{t}_vcmd"] = s.vcmd[i]
                row[f"{t}_vact"] = s.vact[i]
                row[f"{t}_I"] = s.cur[i]
                row[f"{t}_rpm"] = s.rpm[i]
                # 均值之外同时落 std/n：光看均值分不出"稳"和"均值为 0"
                row[f"{t}_rpm_std"] = s.rpm_std[i]
                row[f"{t}_rpm_n"] = s.rpm_n[i]
                row[f"{t}_I_peak"] = s.cur_amax[i]
            rows.append(row)

        if args.verify:
            # ---------- 闭环验证：强制清零 kp/ki/kd ----------
            saved = {}
            for name in PID_ITEMS:
                if name in bl.tun_dir:
                    saved[name] = bl.tun_dir[name][1]
                    bl.set_tunable(name, 0.0)
            bl.fetch_dirs()
            got = {n: tun(bl, n, -1.0) for n in PID_ITEMS}
            bad = [n for n, v in got.items() if abs(v) > 1e-9]
            print(f"[pid] 已写 0 并读回确认：{got}")
            if bad:
                print(f"[err] 以下项没能清零，验证结果不可信：{bad}")
            print("[pre] 着地、场地足够长，开始纯前馈验证\n")
            try:
                for v in nodes:
                    bl.spin_target(v, v)
                    s = measure(bl, v, args.settle, args.hold)
                    collect(s, "verify")
                    idx = {"L": 0, "R": 1}
                    rl, rr = s.rpm[mid("L", 0)], s.rpm[mid("R", 0)]
                    print("  目标 %5.1fpct → 左 %6.2f  右 %6.2f pct   ff=[%.4f %.4f]  "
                          "抖动[%.2f %.2f]  样本[%d %d]"
                          % (v, nz(rl), nz(rr), nz(s.ff[0]), nz(s.ff[1]),
                             nz(s.rpm_std[mid("L", 0)]), nz(s.rpm_std[mid("R", 0)]),
                             s.rpm_n[mid("L", 0)], s.rpm_n[mid("R", 0)]))
                    if rl is None or rr is None:
                        print("    [warn] 本档转速无数据（没登记/丢包），不进验证散点")
                    else:
                        verify_rows.append((v, v, rl, rr))
                    bl.spin_target(0.0, 0.0)
                    bl.sleep(0.4)
                bl.stop_move()
            finally:
                for name, val in saved.items():
                    try:
                        bl.set_tunable(name, val)
                    except Exception:
                        pass
                print(f"[pid] 已恢复原值：{saved}")
        else:
            # ---------- 开环扫压 ----------
            print("[pre] 着地、场地足够长（每档要跑到稳态，车持续%s）\n"
                  % ("后退" if neg else "前进"))
            for v in volts:
                for i in ALL_IDS:
                    bl.set_volt(i, v)
                s = measure(bl, v, args.settle, args.hold)
                collect(s, "sweep")
                print("  V=%5.2f  左转速 %6.2f±%.2f  右转速 %6.2f±%.2f pct   "
                      "Vact=[%.2f %.2f]  I=[%.2f %.2f]A  样本[%d %d]"
                      % (v, nz(s.rpm[mid("L", 0)]), nz(s.rpm_std[mid("L", 0)]),
                         nz(s.rpm[mid("R", 0)]), nz(s.rpm_std[mid("R", 0)]),
                         nz(s.vact[mid("L", 0)]), nz(s.vact[mid("R", 0)]),
                         nz(s.cur[mid("L", 0)]), nz(s.cur[mid("R", 0)]),
                         s.rpm_n[mid("L", 0)], s.rpm_n[mid("R", 0)]))
                for sd in sides:
                    r0 = s.rpm[mid(sd, 0)]
                    if r0 is None or s.rpm_n[mid(sd, 0)] == 0:
                        print(f"  [warn] {SIDE_CN[sd]}0转速无数据，{v}V 档不进曲线")
                        continue
                    curve[sd].append((v, abs(r0)))
                # 电流保护看真峰值（abs_max），不是"窗口均值的最大值"——后者会漏掉尖峰
                peaks = [s.cur_amax[i] for i in ALL_IDS if s.cur_amax[i] is not None]
                peak = max(peaks) if peaks else 0.0
                if peak > args.i_max:
                    print(f"  [保护] 电流峰值 {peak:.2f}A 超过 {args.i_max:g}A，中止后续档位")
                    break
                for i in ALL_IDS:
                    bl.set_volt(i, 0.0)
                bl.sleep(0.3)
            bl.stop_move()

        # ---------- 反解 kf 表 ----------
        if not args.verify:
            print("\n================ kf 表建议值 ================")
            print("口径：y = 前馈输出值（ff = target_sign·kf_at(|target|)，不乘 target）")
            print("参数取 motors[0]，与速度反馈同台\n")
            for sd in sides:
                cn = SIDE_CN[sd]
                cn = SIDE_CN[sd]
                st_dz = tun(bl, f"{cn}静摩擦")
                dy_dz = tun(bl, f"{cn}动摩擦")
                vmin = tun(bl, f"{cn}0最小电压")
                vf = tun(bl, f"{cn}0电压系数", 1.0)
                vmax = tun(bl, f"{cn}0最大电压", 12.0)
                print(f"{cn}侧  静摩擦={st_dz:g}V 动摩擦={dy_dz:g}V "
                      f"volt_min={vmin:g}V volt_factor={vf:g} volt_max={vmax:g}V")
                if vf <= 0:
                    print("  volt_factor 读到 0，反解无效（先标 volt_factor）")
                    continue
                for nd in nodes:
                    if nd <= 0:
                        # 曲线过原点：低速段的固定偏置归摩擦垫，前馈表不造截距
                        table[sd].append((0.0, 0.0))
                        print("  x=   0.0  →  y=0.0000（过原点，截距归摩擦垫）")
                        continue
                    v_need = interp_volt_for_speed(curve[sd], nd)
                    if v_need is None:
                        print(f"  x={nd:6.1f}  超出实测转速范围，需外推（实测最高 "
                              f"{max(p[1] for p in curve[sd]):.1f}pct），暂不填")
                        continue
                    fr = friction_at(nd, st_dz, dy_dz)
                    base = fr + vmin
                    controllable = vmax - base
                    if controllable <= 0:
                        print(f"  x={nd:6.1f}  可控区间 ≤0（base={base:.2f}V 顶到 volt_max）")
                        continue
                    # 反向扫压（--volts 传负档）时 v_need 为负，取幅值：
                    # 表只存 |target| 对应的正值，正反两次的 y 才能逐节点相减比较
                    y = (abs(v_need) - base) / (controllable * vf)
                    table[sd].append((nd, y))
                    print(f"  x={nd:6.1f}  V(v)={v_need:5.2f}V  friction={fr:4.2f}V "
                          f"base={base:5.2f}V  →  y={y:.4f}")

        # ---------- 输出 ----------
        out_csv = Path(bl.out_path(f"cal_kf_{args.tag}.csv"))
        cols = (["phase", "volt_cmd"]
                + [f"{tag_of(i)}_{k}" for i in ALL_IDS
                   for k in ("vcmd", "vact", "I", "rpm", "rpm_std", "rpm_n", "I_peak")])
        bl.save_csv(str(out_csv), rows, cols)
        out_png = out_csv.with_suffix(".png")
        plot_result(out_png, curve, table, verify_rows)
        print(f"\n[out] 数据 {out_csv}")
        if not args.verify:
            print("把 y 值填进 src/robot_and_control.cpp 的 left_kf_table / right_kf_table "
                  "后烧录，再跑 --verify 复查纯前馈是否恰好达标。")


if __name__ == "__main__":
    main()
