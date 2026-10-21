# -*- coding: utf-8 -*-
"""
car_vi_curve.py —— ② 静/动摩擦垫标定（着地·开环，按 ① 系数等流爬坡）

【与旧版（恒压爬坡）的区别】
旧版六台恒压爬坡找启动/停转电压——测的不是固件的工作点：固件起步时六台各在
自己 base_i 上、电流近似相等（① 的锚定构造），恒压是"强台多扛、弱台少扛"的
非等流分配（数学上只差 R 分布的二阶小量，但口径不对）。本版按 ①
（cal_motor_intrinsic.py）标定的 volt_min/volt_factor 手工核算每台电压，
沿等流流形爬坡（friction=0，只带 ① 的两个量）：

    V_i = sign(o)·volt_min_i + o·(volt_max - volt_min_i)·factor_i  （o≈固件 output，o<0 反向）

o 爬到 per-side 持续起步 → 该侧三台 vact 的均值即固件起步所需 mean base：
    静垫 = mean(vact_break_side) - 0.5
降压到整车滚停同法：
    动垫 = mean(vact_stop_car) - 0.5
（减 0.5 = ① 的 volt_min 均值记账口径；摩擦垫只补 volt_min 之外的地面增量。）

【为什么等流爬坡】
- 测的就是固件实际工作点，垫子公式直给，不需要恒压法的口径换算；
- 爬坡时各台电流应大致相等——顺带就是 ① 的负载下复核（电流明显不齐=① 有问题，
  脚本每试打印各台电流比）。

【判据（关键）】
持续起步/滚停 = 窗内 mean(该侧 0 号机转速) ≥ move_pct 且 ≥1pct 样本占比 ≥ ratio。
**不要"抽动"**（齿隙弹跳）：既往地面实测抽动 1.2V vs 持续 1.6V，差 0.4V，
按抽动填会得出过低的垫子。重复 --trials 次（试间短冲+惰行随机化停位——齿槽
位置让单次读数差 2~3 个量化档），取中位。

【前置】
  1. 着地（垫子补的就是地面阻力分摊），标定方向留空 ~0.6m（正向=车前、反向=车后），
     USB 松量固定，人手旁
  2. ① 的 volt_min/factor 已知（--vmin/--factor 传入；未传默认全 0.5/全 1.0，
     此时 map 退化成近似恒压，仅作兜底）
  3. 脚本开头 stop_move 交车（之后手柄禁用，碰不到速度环）+ 校验环状态=Stopped
  4. 电池尽量满电（volt_max 按 12V 算；每档记录 vact，掉压可见）

【输出】
  CSV 落 backend/pytest/probe_data/cal_pads_<tag>.csv（每试每档的 o/转速/六台
  vact/六台电流）。控制台给在线调参项名与建议值（摩擦垫是在线调参项，不用烧录）：
    左/右静摩擦 = 各侧 mean(vact_break) - 0.5
    左/右动摩擦 = 整车 mean(vact_stop) - 0.5（左右同值，见下）
  （反向标定时 vact 为负，脚本取幅值再减 0.5，正反两次结果可直接相减比较。）
  填后验收：闭环纯前馈（kp=ki=kd=0）打 1/2/3pct 蠕行——恰好=对；蹿出去=垫子
  大砍；不动=垫子小加。垫子在线可调，1~2 轮收敛。

【反向标定与正反对照】
--o-to 传负即标定反向（例：--o-from 0 --o-to -0.30），爬坡沿 sign(o)·vmin 流形走，
与固件 target_sign·base 对齐；短冲/升压/降压方向一并翻转。
判读正负差（正垫 − 反垫）：
  - 掉头 180° 后差值反号 → 场地坡度/纹理所致，不是车，别改下位机
  - 掉头后仍同号且 >0.3V → 车体固有不对称（脚轮/传动），才考虑加负向垫
  - <0.2V → 噪声档（地面不匀/齿槽/电量），维持对称模型

【为什么动垫不分局】
刚性车体上两侧轮子一起转：一侧电压不够时，另一侧仍拖着整车滚，该侧转速被
"带起来"，per-side 滚停在物理上分不开（单侧降档又会被另一侧 0V 短路阻尼
污染）。动垫取整车滚停、左右同值；静垫 per-side（起转前车体不动，两侧独立）。

【旧版 --phase factor 已删除】
单台 R/K 分解法（开着车测各台 volt_factor）已被 cal_motor_intrinsic.py 的
等流搜索法取代，勿再用。
"""

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from app.script_api import BackendLink  # noqa: E402

MOTORS = ["L0", "L1", "L2", "R0", "R1", "R2"]
MID = {m: i for i, m in enumerate(MOTORS)}  # 固件 0x84：0~2=左、3~5=右
SIDE_MOTORS = {"L": ["L0", "L1", "L2"], "R": ["R0", "R1", "R2"]}
SIDE_CN = {"L": "左", "R": "右"}
I_NAME = {m: "%s%s电流" % ("左" if m[0] == "L" else "右", m[1]) for m in MOTORS}
V_NAME = {m: "%s%s实际电压" % ("左" if m[0] == "L" else "右", m[1]) for m in MOTORS}
W0 = {"L": "左0转速", "R": "右0转速"}
STATE = {"L": "左环状态", "R": "右环状态"}
X_NAME = "全局X"
WANT = ([I_NAME[m] for m in MOTORS] + [V_NAME[m] for m in MOTORS]
        + [W0["L"], W0["R"], STATE["L"], STATE["R"], X_NAME])
CSV_COLS = (["trial", "phase", "o", "L_rpm", "R_rpm"]
            + [V_NAME[m] for m in MOTORS] + [I_NAME[m] for m in MOTORS])


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return float("nan")
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def parse_kv(text: str, what: str) -> dict:
    out = {}
    for kv in text.split(","):
        if kv.strip():
            k, v = kv.split("=")
            k = k.strip().upper()
            if k not in MOTORS:
                sys.exit("--%s 里的电机名 %r 不认识（应为 L0..R2）" % (what, k))
            out[k] = float(v)
    return out


def motor_volts(vmin: dict, factor: dict, vmax: float, o: float) -> dict:
    """等流 map（friction=0）：V_i = sign(o)·vmin_i + o·(vmax - vmin_i)·factor_i。o<0 反向。

    sign 项对应固件的 target_sign·base，不能省：省了负向流形会整体平移 2·vmin（≈1V）。
    """
    s = 1.0 if o >= 0.0 else -1.0
    return {m: s * vmin[m] + o * (vmax - vmin[m]) * factor[m] for m in MOTORS}


def apply_map(bl, volts: dict) -> None:
    # 钳 ±12：负压合法（=反转），与命令目录范围一致
    for m, v in volts.items():
        bl.set_volt(MID[m], max(-12.0, min(12.0, v)))


def zero_all(bl) -> None:
    apply_map(bl, {m: 0.0 for m in MOTORS})


def sample(bl, hold: float, items) -> dict:
    """hold 秒内 ~40ms 轮询采样监控缓存，返回 {item: [values]}。"""
    data = {k: [] for k in items}
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < hold:
        for k in items:
            v = bl.latest(k)
            if v is not None:
                data[k].append(v)
        time.sleep(0.04)
    return data


def sustained(rpms, move_pct: float, ratio: float):
    """持续转动判据：窗内均值达标 且 ≥1pct 样本占比达标。挡掉齿隙抽动。"""
    if len(rpms) < 5:
        return False, 0.0
    m = sum(abs(x) for x in rpms) / len(rpms)
    r = sum(1 for x in rpms if abs(x) >= 1.0) / len(rpms)
    return (m >= move_pct and r >= ratio), m


def return_to(bl, x_ref: float, tol=0.02, timeout=12.0) -> None:
    """试间复位：速度环低速闭环倒回 x_ref（远段定速、近段降速），停稳交回底盘。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        bl.check_abort()
        dx = bl.latest(X_NAME) - x_ref
        if abs(dx) <= tol:
            break
        speed = max(3.0, min(10.0, abs(dx) * 40.0))
        tgt = -speed if dx > 0 else speed
        bl.spin_target(tgt, tgt)
        time.sleep(0.15)
    bl.spin_target(0.0, 0.0)
    time.sleep(0.3)
    bl.stop_move()  # 交回，准备下一段开环
    time.sleep(0.2)


def plot_pads(path, rows, move_pct, results):
    """两栏：升压段 per-side 起步、降压段整车滚停，竖线标判据命中的那一档。"""
    from plot_util import subplots, save, legend

    def med(xs):
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0

    start_o = {s: med([r["static"][s]["o"] for r in results if s in r["static"]])
               for s in "LR"}
    stop_o = med([r["dynamic"]["o"] for r in results if r["dynamic"]]) if any(
        r["dynamic"] for r in results) else None

    fig, axes = subplots(2)
    for ax, ph, title in ((axes[0][0], "up", "升压：per-side 持续起步"),
                          (axes[0][1], "down", "降压：整车滚停")):
        rs = [r for r in rows if r["phase"] == ph]
        if rs:
            ax.plot([r["o"] for r in rs], [r["L_rpm"] for r in rs], "o-", ms=3, label="左0转速")
            ax.plot([r["o"] for r in rs], [r["R_rpm"] for r in rs], "s-", ms=3, label="右0转速")
        ax.axhline(move_pct, color="gray", ls=":", lw=1)
        ax.set_xlabel("输出档 o")
        ax.set_ylabel("转速 / pct")
        ax.set_title(title + ("（虚线=持续转动判据 %.1fpct）" % move_pct))
        ax.grid(True, alpha=0.3)
        legend(ax, fontsize=8)
    for s, cn in (("L", "左"), ("R", "右")):
        if start_o[s] is not None:
            axes[0][0].axvline(start_o[s], color="tab:red", ls="--", lw=1)
            axes[0][0].text(start_o[s], move_pct, " %s侧起步" % cn, fontsize=8,
                            color="tab:red")
    if stop_o is not None:
        axes[0][1].axvline(stop_o, color="tab:green", ls="--", lw=1)
        axes[0][1].text(stop_o, move_pct, " 整车滚停", fontsize=8, color="tab:green")
    save(fig, path)


def run_trial(bl, args, vmin, factor, idx, rows) -> dict:
    """一轮：随机化停位 → 升压找 per-side 持续起步 → 降压找整车滚停 → 复位。"""
    print("\n===== 第 %d/%d 试 =====" % (idx + 1, args.trials))

    # 1) 随机化停位：同向短冲后惰行停下（齿槽位置随机化，单次读数才可重复比较）
    #    短冲须与本轮标定同向：反向时齿隙预紧在另一侧，电机轴先空转过齿隙才带动轮子，
    #    而反馈取的就是电机轴速度，判据会在车体还没动时命中 → 静垫偏低
    sgn = args.sgn  # 本轮标定方向：+1 正向 / -1 反向
    apply_map(bl, motor_volts(vmin, factor, args.vmax, 0.15 * sgn))
    time.sleep(0.4)
    zero_all(bl)
    time.sleep(0.6)
    x_ref = args.x_ref if args.x_ref is not None else bl.latest(X_NAME)
    args.x_ref = x_ref

    # 2) 升压：per-side 首次"持续起步"档即该侧静摩擦点（起转前车体不动，两侧独立）
    rec = {}
    o = args.o_from
    while (args.o_to - o) * sgn >= -1e-12 and len(rec) < 2:  # 未越过 o_to（sgn 决定爬坡方向）
        bl.check_abort()
        apply_map(bl, motor_volts(vmin, factor, args.vmax, o))
        data = sample(bl, args.hold, WANT)
        x = mean(data[X_NAME])
        if abs(x - x_ref) > args.fence_m:
            zero_all(bl)
            raise RuntimeError("位移越界 %+.3fm（围栏 %.2fm），已收车中止" % (x - x_ref, args.fence_m))
        row = {"trial": idx, "phase": "up", "o": round(o, 4),
               "L_rpm": round(mean(data[W0["L"]]), 3), "R_rpm": round(mean(data[W0["R"]]), 3)}
        for m in MOTORS:
            row[V_NAME[m]] = round(mean(data[V_NAME[m]]), 4)
            row[I_NAME[m]] = round(mean(data[I_NAME[m]]), 4)
        rows.append(row)
        for side in "LR":
            if side in rec:
                continue
            ok, _ = sustained(data[W0[side]], args.move_pct, args.ratio)
            if ok:
                rec[side] = {"o": o,
                             "vact": mean([mean(data[V_NAME[m]]) for m in SIDE_MOTORS[side]]),
                             "cur": {m: mean(data[I_NAME[m]]) for m in MOTORS}}
                print("  %s侧持续起步：o=%.4f  三台vact均值=%.3fV  (L0转速均值%.1fpct)"
                      % (SIDE_CN[side], o, rec[side]["vact"], mean(data[W0[side]])))
        o += args.o_step * sgn
    zero_all(bl)
    time.sleep(0.4)

    if len(rec) == 2 and abs(rec["L"]["o"] - rec["R"]["o"]) > 2 * args.o_step + 1e-9:
        print("  [warn] 两侧起步档相差 >2 档：后起转侧的读数已被先转侧推动的车体污染"
              "（相当于从滚动态起转），该侧静垫可能偏低")

    for side in "LR":
        if side not in rec:
            print("  [warn] %s侧到 o=%.3f 都没持续起步（--o-to 幅度调大重测）" % (SIDE_CN[side], args.o_to))

    # 3) 降压：整车滚停点（刚性车体两侧分不开，取整车；左右同值）
    stop = None
    if rec:
        # 降压起点：离 0 最远那档再往外让 over（sgn 决定"外"是哪边）
        o = (max if sgn > 0 else min)(r["o"] for r in rec.values()) + args.over * sgn
        while (o - args.o_from) * sgn >= -1e-12:
            bl.check_abort()
            apply_map(bl, motor_volts(vmin, factor, args.vmax, o))
            data = sample(bl, args.hold, WANT)
            x = mean(data[X_NAME])
            if abs(x - x_ref) > args.fence_m:
                zero_all(bl)
                raise RuntimeError("位移越界 %+.3fm（围栏 %.2fm），已收车中止" % (x - x_ref, args.fence_m))
            row = {"trial": idx, "phase": "down", "o": round(o, 4),
                   "L_rpm": round(mean(data[W0["L"]]), 3), "R_rpm": round(mean(data[W0["R"]]), 3)}
            for m in MOTORS:
                row[V_NAME[m]] = round(mean(data[V_NAME[m]]), 4)
                row[I_NAME[m]] = round(mean(data[I_NAME[m]]), 4)
            rows.append(row)
            ok, _ = sustained(data[W0["L"]] + data[W0["R"]], args.move_pct, args.ratio)
            if ok:
                stop = {"o": o, "vact": mean([mean(data[V_NAME[m]]) for m in MOTORS]),
                        "cur": {m: mean(data[I_NAME[m]]) for m in MOTORS}}
            else:
                break  # 整车撑不住了，再低也没意义
            o -= args.o_step * sgn
        zero_all(bl)
        time.sleep(0.4)
        if stop is None:
            print("  [warn] 降压段整车从没能持续转动（--over 调大或 --o-from 调低重测）")
        else:
            print("  整车滚停：o=%.4f  六台vact均值=%.3fV" % (stop["o"], stop["vact"]))
    else:
        print("  [skip] 两侧都没起转，跳过降压段")

    # 4) 复位到本轮起点（速度环闭环，停稳交回）
    return_to(bl, x_ref)
    return {"static": rec, "dynamic": stop}


def main() -> None:
    ap = argparse.ArgumentParser(description="② 静/动摩擦垫标定：着地·等流 map 爬坡")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("--tag", default=time.strftime("%Y%m%d_%H%M"), help="数据文件标记")
    ap.add_argument("--vmin", default="", help="① 的各台 volt_min，如 L0=0.51,L1=0.48（缺省全 0.5）")
    ap.add_argument("--factor", default="", help="① 的各台 volt_factor，如 L0=1,L1=0.93（缺省全 1.0）")
    ap.add_argument("--vm0", type=float, default=0.5,
                    help="volt_min 均值记账口径 V（① 恒为 0.5；垫子=实测-该值）")
    ap.add_argument("--vmax", type=float, default=12.0, help="map 的 volt_max V（与固件一致）")
    ap.add_argument("--o-from", type=float, default=0.0, help="输出档起始 o（爬坡起点，通常 0）")
    ap.add_argument("--o-to", type=float, default=0.30,
                    help="输出档终点 o（mean 电压≈sign(o)·vmin+o·(12-vmin)·f）；"
                         "传负=标定反向，o_to<o_from 即反向爬坡")
    ap.add_argument("--o-step", type=float, default=0.004, help="输出档步长 o（≈0.045V/档）")
    ap.add_argument("--over", type=float, default=0.02, help="降压段起点高出静摩擦点多少 o")
    ap.add_argument("--hold", type=float, default=0.7, help="每档保持/采样时长 s")
    ap.add_argument("--move-pct", type=float, default=2.0, help="持续转动判据的转速阈值 pct")
    ap.add_argument("--ratio", type=float, default=0.7, help="持续转动判据的 ≥1pct 样本占比")
    ap.add_argument("--trials", type=int, default=3, help="重复轮数（试间随机化停位，取中位）")
    ap.add_argument("--fence-m", type=float, default=0.6, help="相对本轮起点的位移围栏 m")
    ap.add_argument("--csv", default="", help="结果 CSV（默认 probe_data/<层>/cal_pads_<tag>.csv）")
    ap.add_argument("--png", default=None,
                    help="结果图（默认 cal_pads_<tag>.png，落 probe_data/<层>/）；空字符串跳过")
    args = ap.parse_args()

    vmin = {m: 0.5 for m in MOTORS}
    vmin.update(parse_kv(args.vmin, "vmin"))
    factor = {m: 1.0 for m in MOTORS}
    factor.update(parse_kv(args.factor, "factor"))
    args.x_ref = None  # 第 1 试的起点作为会话基准，之后每试复位到它
    args.sgn = 1.0 if args.o_to >= args.o_from else -1.0  # 爬坡方向：o_to 比 o_from 更负即反向
    if not args.vmin and not args.factor:
        print("[warn] 未传 ① 的 volt_min/factor，map 退化为近似恒压——请传入 cal_motor_intrinsic 的产出")

    rows = []
    results = []
    with BackendLink(args.base) as bl:
        missing = [n for n in WANT if n not in bl.mon_dir]
        if missing:
            sys.exit("监控表缺项: %s" % missing)
        bl.clear_subs()
        bl.sub(WANT)
        time.sleep(0.3)
        bl.stop_move()  # 交出底盘（is_busy 期间 0x84 被静默拒绝）
        time.sleep(0.3)
        for side in "LR":
            st = bl.latest(STATE[side])
            if st not in (0, None):
                sys.exit("%s环状态=%s（应为 0=Stopped）：速度环活着会覆盖开环电压，已退出"
                         % (SIDE_CN[side], st))

        print("map：V_i = sign(o)·vmin_i + o·(12-vmin_i)·factor_i（friction=0）")
        print("  vmin  ：" + "  ".join("%s=%.3f" % (m, vmin[m]) for m in MOTORS))
        print("  factor：" + "  ".join("%s=%.4f" % (m, factor[m]) for m in MOTORS))
        print("方向：%s（o: %.3f → %.3f，步长 %.4f）"
              % ("正向·车前进" if args.sgn > 0 else "反向·车后退",
                 args.o_from, args.o_to, args.o_step))
        print("前提：着地、%s留空 %.1fm、USB 松量固定、人手旁；环状态 Stopped、手柄已禁用"
              % ("车前方" if args.sgn > 0 else "车后方", args.fence_m))
        try:
            for i in range(args.trials):
                results.append(run_trial(bl, args, vmin, factor, i, rows))
        finally:
            zero_all(bl)
            time.sleep(0.2)
            bl.spin_target(0.0, 0.0)
            bl.stop_move()

    # ---------- 汇总：各试中位 → 垫子建议 ----------
    print("\n================ 摩擦垫建议（在线调参项，不用烧录）================")
    out = {}
    for side in "LR":
        # 反向时 vact 为负，统一取幅值再减记账口径
        vals = [abs(r["static"][side]["vact"]) - args.vm0
                for r in results if side in r["static"]]
        if vals:
            out["%s_st" % side] = median(vals)
            print("  %s静摩擦 = %.3f V   （各试 %s，减 vm0=%.2f）"
                  % (SIDE_CN[side], out["%s_st" % side],
                     " ".join("%.3f" % v for v in vals), args.vm0))
        else:
            print("  %s静摩擦 = 无有效数据" % SIDE_CN[side])
    dvals = [abs(r["dynamic"]["vact"]) - args.vm0 for r in results if r["dynamic"]]
    if dvals:
        out["dy"] = median(dvals)
        print("  动摩擦 = %.3f V（左右同值；各试 %s，减 vm0=%.2f）"
              % (out["dy"], " ".join("%.3f" % v for v in dvals), args.vm0))
    else:
        print("  动摩擦 = 无有效数据")

    # ① 的负载下复核：起步档各台电流应大致相等（同侧三台比）
    print("\n① 复核（起步档各台电流比，同侧内应≈1；明显不齐=① 的 factor/volt_min 可疑）：")
    for r in results:
        for side in "LR":
            if side not in r["static"]:
                continue
            c = r["static"][side]["cur"]
            a = c[side + "0"]
            if abs(a) > 1e-6:  # 反向时电流为负
                print("  %s侧: I1/I0=%.2f  I2/I0=%.2f  (I0=%.2fA)"
                      % (SIDE_CN[side], c[side + "1"] / a, c[side + "2"] / a, a))

    print("\n验收：填垫后闭环纯前馈（kp=ki=kd=0）打 1/2/3pct 蠕行——")
    print("  恰好跟住=对；蹿出去=垫子大砍；不动=垫子小加（在线调 1~2 轮收敛）。")

    csv_path = bl.out_path(args.csv or "cal_pads_%s.csv" % args.tag)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:  # BOM：Excel 中文友好
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("\nCSV：%s（%d 行）" % (csv_path, len(rows)))

    png = "" if args.png == "" else (args.png or "cal_pads_%s.png" % args.tag)
    if png and rows:
        plot_pads(bl.out_path(png), rows, args.move_pct, results)


if __name__ == "__main__":
    main()
