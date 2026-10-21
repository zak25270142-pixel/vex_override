# -*- coding: utf-8 -*-
"""
cal_motor_intrinsic.py —— 电机组固有属性标定（volt_min / volt_factor），纯开环、车轮悬空。

【定位】
最底层标定：只测电机自身固有属性，目标是把组内三台的"出力"拉齐。同型号电机 k_t
相同，等流即等力矩分担。摩擦垫与前馈表不属于本脚本——那是整车属性，须在着地、
闭环下另标。

【原理】
组内三台被齿轮锁死在同一根轴上，转速强制同步，没法像全向底盘那样"同压比各轮转速"。
改用"直接找等流电压组合"：开环下可以给三台各设不同电压（闭环做不到，那里 V_i 被
output·controllable·f_i 统一决定），于是直接迭代——电流大的砍电压、小的加电压——
收敛时的那组电压就是物理上正确的分配，不经过 k_e / R 等任何中间量。

  Phase A  三台同压扫低压 —— 分侧找启动电压 V_start
    六台同时出力、各自克服自身静摩擦，起转电压即单台启动电压（不除 3）。
    左右是独立齿轮组，起转点分侧各判（同时扫没问题，判据不能混）；
    后续等流扫描的档位两侧共用，起点取较高的那侧。
    它作为后续等流扫描的最低基准。

  Phase B  多水平等流搜索 —— 每档按 1.5 倍等比抬高基准
    在每个基准水平上迭代调各台电压使三台电流一致（比值 < tol），记录收敛电压。
    迭代中始终把三台均值拉回 level，否则总力矩变了、转速会漂，等于追移动靶。

  Phase C  直线拟合 + 物理锚定
    V_i = a_i + b_i · level（level=三台均值电压）
    启动电压不是原始截距 a_i，而是流形在起步档的值 a_i + b_i·Vmin——
    Vmin 即 Phase A 先实测的**该侧**起步电压（"齿轮组稳定低速旋转的最低电压"），
    这正是要填的 volt_min；左右两侧各用各的 L0。
    （为什么不能直接填 a_i：每轮把三台均值拉回 level，拟合的均值线被钉成 y=x，
      mean(a_i)=0 是恒等式，三台截距和为零必有一负——参数化副产品，非物理量。）
    锚定到起步档：
      base_i     = a_i + b_i·L0（L0=Vmin） （o=0 时三台坐在起步等流点，流形精确复现）
      volt_min_i = base_i − (L0 − 0.5)      （均值恒 0.5；0.5=电机零负载自转的固有起步）
      b_i 斜率即 volt_factor（电压增益；mean(b)=1 恒等，最慢台自动得 1），按 max=1 归一
    拟合残差直接回答"是不是直线"：残差小 → 全速度段一套参数就够；
    残差大 → 非线性，volt_min 管低速、volt_factor 管高速，中间段留残差。

【与摩擦垫的关系：无关】
开环走 0x84 直设电机电压（固件 cmd_set_volt → motor->spin(volt)），完全绕过
base/friction/volt_min/volt_factor 映射——测量是纯物理量，摩擦垫当前值不进数据。
二者分工只在填值时体现：base = friction + volt_min，本脚本给 volt_min（均值 0.5），
摩擦垫由着地标定（②）补地面增量，垫子大了直接砍（在线调参，不用烧录）。

【前置条件】
  1. 车轮悬空（整车架起，轮子离地）——着地会把地面摩擦混进 base
  2. 电池尽量满电：volt_max 按 12V 算，掉压会让高档位实际电压达不到指令值
  3. 脚本开头发 stop_move 解除手柄占用（is_busy 期间 0x84 会被固件静默丢弃）
  4. 全程开环，不经过速度环，无需设 PID

【用法】
  先起后端（backend），然后：
    python scripts/02-电机组固有属性/cal_motor_intrinsic.py
    python scripts/02-电机组固有属性/cal_motor_intrinsic.py --low "0.3,0.5,0.7,0.9,1.1,1.4,1.8"
    python scripts/02-电机组固有属性/cal_motor_intrinsic.py --ratio 1.5 --max-levels 7 --v-limit 10
    python scripts/02-电机组固有属性/cal_motor_intrinsic.py --only L            # 只测左侧
    python scripts/02-电机组固有属性/cal_motor_intrinsic.py --start 0.9         # 跳过 Phase A，直接给启动电压

【输出】
  CSV 落 backend/pytest/probe_data/cal_motor_<tag>.csv（每轮迭代的各台指令/实际电压、
  电流、转速；未达标水平拟合时剔除并打印）
  控制台打印：各水平的等流电压与电流比、原始截距/斜率、锚定后的 base/volt_min/
  factor、残差（线性性），以及② 的摩擦垫基准（着地起转电压 − 0.5）

【注意】
  * 开环不经过 volt_factor，测到的是纯固有特性 → factor 建议是**绝对值**，
    直接覆盖 robot_and_control.cpp 的现值，不是叠加。
  * 反过来：factor 是否真把电流拉齐，开环验证不了（开环它不生效），必须烧录后
    跑闭环等流脚本复核。
  * MyMotor 字段顺序：{&motor, volt_factor, volt_max, volt_min} —— 第 2 字段是
    factor，第 4 字段是 volt_min。
  * volt_min 均值恒 0.5（0.5=电机零负载自转的固有起步，组内差异体现在截距/斜率里）；
    着地标完摩擦垫后低速等流由 friction 与 volt_min 共同决定，需闭环复核。
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402

SIDE_CN = {"L": "左", "R": "右"}
# 固件电机编号：0~2 = 左0~2，3~5 = 右0~2
ALL_IDS = list(range(6))


def mid(side: str, i: int) -> int:
    """侧+组内序号 → 固件电机编号"""
    return i if side == "L" else 3 + i


def tag_of(i: int) -> str:
    """固件电机编号 → 显示标签（L0 / R2）"""
    return ("L" if i < 3 else "R") + str(i % 3)


def mon(side: str, i: int, kind: str) -> str:
    """监控项中文名：kind ∈ {指令电压, 实际电压, 电流, 转速}"""
    return f"{SIDE_CN[side]}{i}{kind}"


WANT = ([mon(s, i, k) for s in "LR" for i in range(3)
         for k in ("指令电压", "实际电压", "电流", "转速")]
        + ["左环状态", "右环状态"])


@dataclass
class Sample:
    """一档（或一轮）稳态测量。各 list 按固件电机编号 0~5 索引。"""
    volt_cmd: float     # 本档基准电压 V（三台同压档）或本轮 level
    vcmd: list          # 六台指令电压 V
    vact: list          # 六台实际电压 V（SDK 回读）
    cur: list           # 六台电流 A
    rpm: list           # 六台转速 pct（组内耦合，同侧三台读数应一致）


def measure(bl: BackendLink, volt_cmd: float, settle: float, hold: float) -> Sample:
    """等稳态 → 记一段 → 取各台均值"""
    bl.sleep(settle)
    bl.start_log()
    bl.sleep(hold)
    bl.end_log()
    st = bl.stats(WANT)

    def m(name: str) -> float:
        d = st.get(name) or {}
        return float(d.get("mean") or 0.0)

    return Sample(
        volt_cmd=volt_cmd,
        vcmd=[m(mon(sd, i, "指令电压")) for sd in "LR" for i in range(3)],
        vact=[m(mon(sd, i, "实际电压")) for sd in "LR" for i in range(3)],
        cur=[m(mon(sd, i, "电流")) for sd in "LR" for i in range(3)],
        rpm=[m(mon(sd, i, "转速")) for sd in "LR" for i in range(3)],
    )


def apply_volts(bl: BackendLink, volts: dict) -> None:
    """下发电压：volts 里没给的电机一律 0V"""
    for i in ALL_IDS:
        bl.set_volt(i, float(volts.get(i, 0.0)))


def run_same_voltage(bl: BackendLink, drive_ids: list, volts: list,
                     settle: float, hold: float, i_max: float,
                     on_row: Callable[[Sample], None]) -> list:
    """Phase A：三台（两侧共六台）同压扫低压，找起转点"""
    out = []
    for v in volts:
        apply_volts(bl, {i: v for i in drive_ids})
        s = measure(bl, v, settle, hold)
        out.append(s)
        on_row(s)
        peak = max(abs(s.cur[i]) for i in drive_ids)
        if peak > i_max:
            print(f"  [保护] 电流 {peak:.2f}A 超过 {i_max:g}A，中止后续档位")
            break
        apply_volts(bl, {})
        bl.sleep(0.25)
    return out


def search_balance(bl: BackendLink, sides: list, level: float, alpha: float,
                   tol: float, max_rounds: int, settle: float, hold: float,
                   i_min_valid: float, v_cap: float,
                   on_row: Callable[[Sample, int], None]) -> tuple:
    """Phase B：在给定基准水平上迭代调各台电压，使组内三台电流一致。

    每轮：电流高于均值的砍电压、低于的加电压（步长 alpha），再把三台均值拉回 level
    以稳住转速。返回 (收敛电压 dict, 收敛时的 Sample, 达标与否)。
    """
    ids = [mid(s, i) for s in sides for i in range(3)]
    v = {i: level for i in ids}
    last = None
    ok = False
    for rnd in range(max_rounds):
        apply_volts(bl, v)
        s = measure(bl, level, settle, hold)
        last = s
        on_row(s, rnd)

        converged = True
        for s_side in sides:
            trio = [mid(s_side, i) for i in range(3)]
            cur = [s.cur[i] for i in trio]
            cmin, cmax = min(cur), max(cur)
            if cmin < i_min_valid:          # 电流太小，比值不可信
                converged = False
                continue
            if cmax / cmin >= tol:
                converged = False
        if converged:
            ok = True
            break

        for s_side in sides:
            trio = [mid(s_side, i) for i in range(3)]
            cmean = sum(s.cur[i] for i in trio) / 3.0
            for i in trio:
                v[i] -= alpha * (s.cur[i] - cmean)
            vm = sum(v[i] for i in trio) / 3.0
            for i in trio:
                v[i] = max(0.0, min(v_cap, v[i] + (level - vm)))

        apply_volts(bl, {})
        bl.sleep(0.2)
    apply_volts(bl, {})
    return v, last, ok


def fit_line(pts: list) -> tuple:
    """最小二乘拟合 y = a + b·x，返回 (a, b, 最大残差)"""
    n = len(pts)
    if n < 2:
        return 0.0, 0.0, 0.0
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return 0.0, 0.0, 0.0
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n
    resid = max(abs(y - (a + b * x)) for x, y in pts)
    return a, b, resid


def plot_balance(path: Path, fits: dict) -> None:
    """每侧一栏：三台的 (level, 等流电压) 散点 + 拟合直线——看截距与线性性"""
    from plot_util import subplots, save

    sides = list(fits.keys()) or ["L"]
    fig, axes = subplots(len(sides))
    for ax, sd in zip(axes[0], sides):
        for i, item in sorted(fits.get(sd, {}).items()):
            pts, a, b, resid = item
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ax.plot(xs, [p[1] for p in pts], "o", label=f"{tag_of(mid(sd, i))} 实测")
            x0, x1 = min(xs), max(xs)
            ax.plot([x0, x1], [a + b * x0, a + b * x1], "-", alpha=0.8,
                    label=f"{tag_of(mid(sd, i))} 拟合(截距{a:.2f} 残差{resid:.2f})")
        ax.set_title(f"{SIDE_CN[sd]}侧 等流电压 vs 基准水平")
        ax.set_xlabel("基准水平 level / V")
        ax.set_ylabel("等流时该台电压 / V")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7)
    save(fig, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="电机组固有属性标定（悬空，纯开环）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--low", default="0.3,0.5,0.7,0.9,1.1,1.4,1.8",
                    help="Phase A 三台同压细扫档（V），用于找启动电压")
    ap.add_argument("--start", type=float, default=0.0,
                    help="直接给定启动电压，跳过 Phase A")
    ap.add_argument("--ratio", type=float, default=1.5, help="等流基准的等比递增倍率")
    ap.add_argument("--max-levels", type=int, default=7, help="最多测几个基准水平")
    ap.add_argument("--v-limit", type=float, default=10.0, help="基准电压上限 V")
    ap.add_argument("--settle", type=float, default=0.8, help="每档/每轮起稳等待 s")
    ap.add_argument("--hold", type=float, default=1.5, help="稳态窗时长 s")
    ap.add_argument("--omega-thr", type=float, default=1.0, help="判定已起转的转速阈值 pct")
    ap.add_argument("--alpha", type=float, default=1.0, help="等流迭代步长 V/A")
    ap.add_argument("--tol", type=float, default=1.03, help="等流收敛判据 max/min")
    ap.add_argument("--max-rounds", type=int, default=8, help="每水平最多迭代轮数")
    ap.add_argument("--i-max", type=float, default=2.2, help="电流保护上限 A")
    ap.add_argument("--i-min-valid", type=float, default=0.05,
                    help="电流低于此值认为比值不可信 A")
    ap.add_argument("--only", default="LR", help="只测哪侧：L / R / LR")
    ap.add_argument("--tag", default="run1", help="输出文件名后缀")
    args = ap.parse_args()

    volts_low = [float(x) for x in args.low.split(",") if x.strip()]
    sides = [c for c in "LR" if c in args.only.upper()]
    volt_max = 12.0   # 与固件 volt_max 一致：等流搜索的单台电压上限（v_cap）

    rows = []

    def collect(s: Sample, phase: str, rnd: int = -1) -> None:
        row = {"phase": phase, "level": s.volt_cmd, "round": rnd}
        for i in ALL_IDS:
            t = tag_of(i)
            row[f"{t}_vcmd"] = s.vcmd[i]
            row[f"{t}_vact"] = s.vact[i]
            row[f"{t}_I"] = s.cur[i]
            row[f"{t}_rpm"] = s.rpm[i]
        rows.append(row)

    def line(s: Sample, drive_ids: list) -> str:
        parts = []
        for i in ALL_IDS:
            mark = "*" if i in drive_ids else " "
            parts.append("%s%s %4.2f/%4.2fV %5.2fA %6.1f"
                         % (mark, tag_of(i), s.vcmd[i], s.vact[i], s.cur[i], s.rpm[i]))
        return "  " + " | ".join(parts)

    with BackendLink(args.base) as bl:
        bl.stop_move()          # 交出底盘：is_busy 期间 0x84 会被固件丢弃
        bl.sleep(0.4)
        bl.clear_subs()
        bl.sub(WANT)
        bl.sleep(0.4)

        st_l, st_r = bl.latest("左环状态"), bl.latest("右环状态")
        if st_l != 0 or st_r != 0:
            print(f"[warn] 速度环状态非 Stopped（左={st_l:g} 右={st_r:g}），"
                  f"开环电压可能被每拍覆盖")
        print("[pre] 确认车轮悬空、电池满电\n")

        try:
            drive_all = [mid(s, i) for s in sides for i in range(3)]

            # ---------- Phase A：找启动电压（六台同压一起扫，起转点分侧各判）----------
            # 同时扫没问题（都是低压惰行），但左右是独立齿轮组、起转电压未必相同；
            # 共用一个 L0 会把另一侧的锚点带偏，所以分侧记录。
            v_start = {sd: args.start for sd in sides}
            if any(x <= 0 for x in v_start.values()):
                print("===== Phase A 三台同压扫低压（分侧找起转点）=====")
                a_samples = run_same_voltage(
                    bl, drive_all, volts_low, args.settle, args.hold, args.i_max,
                    lambda s: (collect(s, "A_start"), print(line(s, drive_all))))
                for sd in sides:
                    for s in a_samples:
                        if abs(s.rpm[mid(sd, 0)]) >= args.omega_thr:
                            v_start[sd] = s.volt_cmd
                            break
            miss = [SIDE_CN[sd] for sd in sides if v_start[sd] <= 0]
            if miss:
                print(f"[err] {'、'.join(miss)}侧没找到启动电压：抬高 --low 上限、"
                      f"降低 --omega-thr，或用 --start 直接给")
                return
            print("\n启动电压 V_start：" + "  ".join(
                f"{SIDE_CN[sd]}={v_start[sd]:.2f}V" for sd in sides) + "\n")

            # ---------- Phase B：多水平等流搜索 ----------
            levels, bal, converged = [], {}, {}
            L = max(v_start.values())   # 档位两侧共用，起点取较高的那侧，保证都起转
            while L <= args.v_limit + 1e-9 and len(levels) < args.max_levels:
                levels.append(L)
                L *= args.ratio

            for lv in levels:
                print(f"===== Phase B 等流搜索 level={lv:.2f}V =====")
                v_fin, s_fin, ok = search_balance(
                    bl, sides, lv, args.alpha, args.tol, args.max_rounds,
                    args.settle, args.hold, args.i_min_valid, volt_max,
                    lambda s, rnd: (collect(s, f"B_lv{lv:.2f}", rnd),
                                    print("  r%d %s" % (rnd, line(s, drive_all).strip()))))
                for sd in sides:
                    trio = [mid(sd, i) for i in range(3)]
                    cur = [s_fin.cur[i] for i in trio]
                    cmin = min(cur) if cur else 0.0
                    print("  %s侧 等流电压=[%s]V  电流=[%s]A  比值=%s  %s"
                          % (SIDE_CN[sd],
                             " ".join(f"{v_fin[i]:.2f}" for i in trio),
                             " ".join(f"{c:.2f}" for c in cur),
                             (f"{max(cur)/cmin:.3f}" if cmin > 0 else "n/a"),
                             "达标" if ok else "未收敛"))
                bal[lv] = v_fin
                converged[lv] = ok
                print()
            bl.stop_move()
        finally:
            apply_volts(bl, {})  # 异常退出也要把六台归零，别让轮子空转

    # ---------- Phase C：拟合 + 物理锚定 ----------
    print("\n================ 标定建议 ================")
    print("拟合式：V_i = a_i + b_i·level（level=三台均值电压）")
    print("启动电压 = a_i + b_i·Vmin（Vmin=Phase A 实测起步电压=齿轮组稳定低速旋转的最低电压），")
    print("不是原始截距 a_i：均值参数化下 mean(a)=0 是恒等式（每轮把三台均值拉回 level），")
    print("三台截距和为零必有一负，是参数化副产品；填锚定后的 volt_min。")
    print("锚定：L0=" + "  ".join(f"{SIDE_CN[sd]}={v_start[sd]:.2f}V" for sd in sides)
          + "；base_i = a_i + b_i·L0（o=0 时三台坐在起步等流点，流形精确复现）")
    print(f"      volt_min_i = base_i − (L0−0.5) → 均值恒 0.5（0.5=电机零负载自转固有起步）")
    bad = [lv for lv in bal if not converged.get(lv, False)]
    if bad:
        print(f"[fit] 剔除未达标水平：{', '.join(f'{lv:.2f}V' for lv in bad)}"
              f"（电流太小/未收敛，不在等流流形上）")
    all_fits = {}
    for sd in sides:
        cn = SIDE_CN[sd]
        print(f"\n{cn}侧：")
        resids = []
        all_fits[sd] = {}
        mins, gains = {}, {}
        for i in range(3):
            idx = mid(sd, i)
            pts = [(lv, v[idx]) for lv, v in bal.items() if converged.get(lv, False)]
            if len(pts) < 2:  # 全没达标才兜底用全部点，并明示
                pts = [(lv, v[idx]) for lv, v in bal.items()]
                if len(pts) < 2:
                    print(f"  {cn}{i}: 数据点不足，无法拟合")
                    continue
                print(f"  {cn}{i}: [warn] 无达标水平，拟合用了全部点")
            a, b, resid = fit_line(pts)
            resids.append(resid)
            base_i = a + b * v_start[sd]
            volt_min_i = base_i - (v_start[sd] - 0.5)
            mins[i] = volt_min_i
            # b_i 天生就是 volt_factor：电压增益，mean(b)=1 恒等，下面按 max=1 归一。
            # 不必除 (volt_max−base)：base 仅 1~2V，controllable≈10.7 三台几乎相同，
            # 该除法是 ±1.6% 量级的修正，还会把 Phase A 的 L0 噪声耦合进 factor。
            gains[i] = b if b > 0 else 0.0
            all_fits[sd][i] = (pts, a, b, resid)
            print(f"  {cn}{i}: 截距={a:+.3f}V  斜率={b:.4f}  base={base_i:.3f}V  "
                  f"volt_min={volt_min_i:+.3f}V  残差={resid:.3f}V")
        if not gains or max(gains.values()) <= 0:
            print("  factor：有效水平不足 2 组（降低 --v-limit 或增大 --max-levels）")
            continue
        if min(gains.values()) <= 0:
            print(f"  [warn] 有电机斜率 ≤0（{[i for i in gains if gains[i] <= 0]}），数据异常，"
                  f"该台 factor 会得 0，勿直接填")
        gmax = max(gains.values())
        print("  volt_min 建议（均值 0.5，填 MyMotor 第 4 字段）：" + "  ".join(
            f"{cn}{i}={mins[i]:.3f}V" for i in sorted(mins)))
        print("  volt_factor 建议（斜率归一到 max=1，恒 ≤1，填第 2 字段）："
              + "  ".join(f"{cn}{i}={gains[i] / gmax:.4f}" for i in sorted(gains)))
        if min(mins.values()) <= 0.1:
            print(f"  [warn] 有 volt_min ≤ 0.1V（最小 {min(mins.values()):.3f}V）：组内差异太大，"
                  f"均值 0.5 的记账兜不住，建议抬高 volt_min 均值重算")
        worst = max(resids) if resids else 0.0
        verdict = ("线性良好，全速度段一套参数即可" if worst < 0.15
                   else "残差偏大 → 非线性，低速靠 volt_min、高速靠 factor，中间段留残差")
        print(f"  线性性：最大残差 {worst:.3f}V —— {verdict}")
    print("\n② 着地摩擦垫基准：静垫 = 着地起转电压 − 0.5，动垫 = 着地滚停电压 − 0.5（左右各算）；")
    print("   base 总量 = 摩擦垫 + volt_min，垫子大了直接砍（在线调参，不用烧录）。")

    out = Path(bl.out_path(f"cal_motor_{args.tag}.csv"))
    cols = (["phase", "level", "round"]
            + [f"{tag_of(i)}_{k}" for i in ALL_IDS for k in ("vcmd", "vact", "I", "rpm")])
    bl.save_csv(str(out), rows, cols)  # 本地写文件，不依赖 HTTP 会话
    plot_balance(out.with_suffix(".png"), all_fits)
    print("\n填 src/robot_and_control.cpp 的 MyMotor 第 2（factor）、第 4（volt_min）字段后"
          "烧录；烧录后须跑闭环等流脚本复核——开环下 factor 不生效，本脚本验证不了。")


if __name__ == "__main__":
    main()
