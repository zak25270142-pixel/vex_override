# -*- coding: utf-8 -*-
"""
cal_motor_intrinsic.py —— 电机组固有属性采集（volt_min / volt_factor 的原始数据来源），
纯开环、车轮悬空。

【定位】
只负责采集：把每个稳态工作点的原始波形整段录下来落盘。不拟合、不给建议值——
volt_min / volt_factor 的解算交给日抛分析脚本（读本脚本落的 CSV）。
测的是电机自身固有属性，目标是把组内三台的"出力"拉齐。同型号电机 k_t
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

  解算（不在本脚本）：V_i = a_i + b_i·level 拟合 + 起步档锚定出 volt_min / volt_factor，
  由日抛分析脚本读本脚本落的原始段来做。

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
  每段（每档 / 每轮迭代）一个原始 CSV，落
  backend/pytest/probe_data/02-电机组固有属性/cal_motor_<tag>/：
      A_<电压>V.csv            Phase A 同压档
      B_lv<水平>_r<轮次>.csv   Phase B 等流迭代轮
  全部走 print_log：六台指令电压 / 实际电压 / 电流 / 转速 + 左右环状态，原样导出不聚合。
  后端缓冲在下一段 start_log 时清空，所以每段结束立刻导出。
  控制台只打印进度（各轮电压 / 电流 / 转速）与收尾的各水平等流电压，不做解算。

【注意】
  * 开环不经过 volt_factor，测到的是纯固有特性。
  * factor 是否真把电流拉齐，开环验证不了（开环它不生效），必须烧录后跑闭环等流脚本复核。
  * MyMotor 字段顺序：{&motor, volt_factor, volt_max, volt_min} —— 第 2 字段是
    factor，第 4 字段是 volt_min（解算脚本填这两个）。
"""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))  # 脚本共用工具目录
from app.script_api import BackendLink  # noqa: E402
from seg_stats import seg_stats  # noqa: E402

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

# 出图分组：左右各一张（左右混画分不清是谁在摆）。
# 每张内部由后端按单位再拆成上下子图（V / A / pct 各自独立 y 轴）。
KINDS = ("指令电压", "实际电压", "电流", "转速")
PNG_GROUPS = [
    ("左", [mon("L", i, k) for i in range(3) for k in KINDS]),
    ("右", [mon("R", i, k) for i in range(3) for k in KINDS]),
]


@dataclass
class Sample:
    """一档（或一轮）稳态测量。各 list 按固件电机编号 0~5 索引。"""
    volt_cmd: float     # 本档基准电压 V（三台同压档）或本轮 level
    vcmd: list          # 六台指令电压 V
    vact: list          # 六台实际电压 V（SDK 回读）
    cur: list           # 六台电流 A
    rpm: list           # 六台转速 pct（组内耦合，同侧三台读数应一致）


# 稳态转速上叠着"轮子每转一次"的周期起伏，实测 周期(s) × 转速(pct) ≈ 10.1
# （7.5~60pct 各档实测，误差 ±2%）。所以记录窗必须按转速给：10pct 档一个周期就 1s，
# 写死 1.5s 只够一个半周期，均值会被摆动带着跑——这正是低速档等流判据达不了标的原因。
RIPPLE_CYCLE_PCT_S = 10.1
MIN_CYCLES = 3.0      # 一段至少覆盖几个起伏周期
HOLD_CAP_S = 10.0     # 上限，免得近停转档算出几十秒
RPM_REF = mon("L", 0, "转速")   # 估窗长用的转速读数


def measure(bl: BackendLink, volt_cmd: float, settle: float, hold: float,
            raw_path: str = "") -> Sample:
    """等稳态 → 按转速定窗长 → 记一段 → 取各台均值；raw_path 给原始段命名
    （不给就按电压自动命名），段统计从该 csv 回读。

    后端缓冲在下一段 start_log 时清空，原始段必须在本段内立刻导出，否则被覆盖。
    """
    bl.sleep(settle)

    v = abs(bl.latest(RPM_REF) or 0.0)
    if v >= 1.0:
        need = MIN_CYCLES * RIPPLE_CYCLE_PCT_S / v
        if need > hold:
            hold = min(need, HOLD_CAP_S)
            print("  [窗长] %.1fpct → 周期%.2fs，窗放 %.1fs（%.1f 个周期）"
                  % (v, RIPPLE_CYCLE_PCT_S / v, hold, hold * v / RIPPLE_CYCLE_PCT_S))

    bl.start_log()
    bl.sleep(hold)
    bl.end_log()
    # 原始段一律落盘：它既是结论的底稿，也是段统计的数据源（stats 端点已删，
    # 统计在脚本侧从 csv 算）。raw_path 给了就用它，没给就按电压自动命名。
    csv_path = raw_path or bl.out_path("cal_motor_v%g.csv" % volt_cmd)
    bl.print_log(csv_path)                                     # 全量原始点 CSV
    if raw_path:
        base = raw_path[:-4]
        for tag, names in PNG_GROUPS:
            bl.v.print_log("%s_%s.png" % (base, tag), names)       # 分组出图
    st = seg_stats(csv_path, WANT)

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
                     settle: float, hold: float, i_max: float, raw_dir: str,
                     on_row: Callable[[Sample], None]) -> list:
    """Phase A：三台（两侧共六台）同压扫低压，找起转点"""
    out = []
    for v in volts:
        apply_volts(bl, {i: v for i in drive_ids})
        s = measure(bl, v, settle, hold, os.path.join(raw_dir, "A_%.2fV.csv" % v))
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
                   i_min_valid: float, v_cap: float, raw_dir: str,
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
        s = measure(bl, level, settle, hold,
                    os.path.join(raw_dir, "B_lv%.2f_r%d.csv" % (level, rnd)))
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

    def line(s: Sample, drive_ids: list) -> str:
        parts = []
        for i in ALL_IDS:
            mark = "*" if i in drive_ids else " "
            parts.append("%s%s %4.2f/%4.2fV %5.2fA %6.1f"
                         % (mark, tag_of(i), s.vcmd[i], s.vact[i], s.cur[i], s.rpm[i]))
        return "  " + " | ".join(parts)

    with BackendLink(args.base) as bl:
        out_dir = bl.out_path("cal_motor_%s" % args.tag)   # 每段一个原始 CSV 的目录
        os.makedirs(out_dir, exist_ok=True)
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
                    bl, drive_all, volts_low, args.settle, args.hold, args.i_max, out_dir,
                    lambda s: print(line(s, drive_all)))
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
                    args.settle, args.hold, args.i_min_valid, volt_max, out_dir,
                    lambda s, rnd: print("  r%d %s" % (rnd, line(s, drive_all).strip())))
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

    # ---------- 收尾：报告原始段位置与各水平等流电压 ----------
    print("\n================ 采集完成 ================")
    print("原始段目录：%s" % out_dir)
    print("启动电压 V_start：" + "  ".join(
        f"{SIDE_CN[sd]}={v_start[sd]:.2f}V" for sd in sides))
    print("各水平等流收敛电压（V）：")
    for lv in levels:
        v = bal.get(lv)
        if not v:
            continue
        seg = "  ".join(
            "%s侧[%s]" % (SIDE_CN[sd], " ".join("%.2f" % v[mid(sd, i)] for i in range(3)))
            for sd in sides)
        print("  lv %5.2f  %s  %s" % (lv, seg,
                                      "达标" if converged.get(lv) else "未收敛"))
    print("（volt_min / volt_factor 的解算交给日抛分析脚本读这些 CSV）")


if __name__ == "__main__":
    main()
