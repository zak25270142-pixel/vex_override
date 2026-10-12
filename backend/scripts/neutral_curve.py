# -*- coding: utf-8 -*-
"""
接入 60:48 齿轮组后的零扭矩电压曲线测试。

通过 FastAPI 后端（HTTP）中转，不再直连串口：
    0x84 -> 单电机直接给电压
    监控目录 -> 读取实际电压 / 转速 / 电流

记录交给后端：脚本只声明"记哪些项"，逐档发命令；每次测量（baseline / 被测）都是一段独立记录
（start_log → 设各电机电压 → 保持 hold → end_log），段末取本段整段统计。

运行前先启动后端：
    cd backend
    uvicorn app.main:app --host 127.0.0.1 --port 8000

实验模型（以左侧 L2 为例）：
    L0/L1 : 60T，固定同一个电压 D，负责把整组带到一个稳定轮速
    L2    : 48T，被测，电压从 0V -> 12V 扫描
    L3    : 48T，0V（惰行）

每个 D 对应一个自然稳定的轮端速度 W0：
    先 L2=0V，记录 baseline W0
    再保持 L0/L1=D，扫描 L2 电压

    delta_w = W(test_voltage) - W0

    delta_w < 0 : L2 对系统形成额外拖曳
    delta_w ≈ 0 : L2 约处于零电磁扭矩边界
    delta_w > 0 : L2 开始产生正向驱动力

因此得到：
    wheel_speed -> V_neutral_actual

注意：
    1. 0V 是现有固件中的“惰行”语义（你现有单电机测试已经这样使用）。
    2. 不使用 current() 的正负号判断发电，因为 current() 是幅值。
    3. 零扭矩点使用轮速相对 baseline 的过零点判断。
    4. 每个测试电压前都重新测一次 L2=0V baseline，抵消电池电压、温度等慢漂移。
    5. 默认记录左右两个 60T 电机的转速，并用两者平均值作为轮速点；原始值仍全部写入 CSV。

示例（先启动后端，再运行本脚本）：
    python scripts/neutral_curve.py --motors L2
    python scripts/neutral_curve.py --motors L2,L3,R2,R3

只想测较低范围：
    python scripts/neutral_curve.py --motors L2 --driver-volts 3,4,5,6,7,8

减小/增加扫描步长：
    python scripts/neutral_curve.py --coarse-step 0.25 --fine-step 0.05
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # backend 根，供 import app
from app.script_api import BackendLink  # noqa: E402
from motor_names import motor_names  # noqa: E402


# ------------------------------------------------------------
# 基本工具
# ------------------------------------------------------------


def motor_id(motor_name: str) -> int:
    side = motor_name[0].upper()
    n = int(motor_name[1])
    if side == "L":
        return n
    if side == "R":
        return 4 + n
    raise ValueError(f"非法电机名称：{motor_name}")


def side_id_name(mid: int) -> str:
    if 0 <= mid < 4:
        return f"L{mid}"
    if 4 <= mid < 8:
        return f"R{mid - 4}"
    raise ValueError(f"非法电机编号：{mid}")


def group_for_test(test_name: str) -> dict:
    """
    返回同侧的：
      driver0 / driver1 : 60T 直连电机
      test               : 48T 被测电机
      idle                : 另一台 48T，保持 0V 惰行
    """
    side = test_name[0].upper()
    n = int(test_name[1])
    if n not in (2, 3):
        raise ValueError("被测电机必须是 2 或 3，例如 L2/L3/R2/R3")

    base = 0 if side == "L" else 4
    return {
        "driver0": base + 0,
        "driver1": base + 1,
        "test": base + n,
        "idle": base + (3 if n == 2 else 2),
    }


def set_voltage(link: BackendLink, mid: int, volts: float) -> None:
    """调用现有 0x84 给单台电机直接加电压（钳在 ±12V）。"""
    link.set_volt(mid, max(-12.0, min(12.0, float(volts))))


def all_zero(link: BackendLink) -> None:
    for mid in range(8):
        try:
            set_voltage(link, mid, 0.0)
        except Exception:
            pass


def avg_or_none(*values: Optional[float]) -> Optional[float]:
    valid = [v for v in values if v is not None and math.isfinite(v)]
    if not valid:
        return None
    return sum(valid) / len(valid)


def build_ids(link: BackendLink, group: dict) -> dict:
    """建立本测试用的 9 项监控中文名（脚本侧 motor_names 约定的别名）。
    key 形如 driver0_rpm / test_vact / driver0_cur。"""
    out = {}
    for role in ("driver0", "driver1", "test"):
        name = side_id_name(group[role])
        cn = motor_names(name)
        for key in ("rpm", "vact", "cur"):
            if cn[key] not in link.mon_dir:
                raise RuntimeError(f"监控项缺失：{name} / {key}")
            out[f"{role}_{key}"] = cn[key]
    return out


def subscribe_for_group(link: BackendLink, ids: dict) -> None:
    """只订阅本次实验需要的 3 台电机 × 3 项监控。"""
    link.clear_subs()
    link.sub(list(ids.values()))
    link.sleep(0.2)


# ------------------------------------------------------------
# 采样
# ------------------------------------------------------------


def measure_state(
    link: BackendLink,
    group: dict,
    ids: dict,
    driver_voltage: float,
    test_voltage: float,
    hold: float,
) -> dict:
    """设置一个状态、保持 hold 秒后收段，取本段整段统计。

    一段就是一次独立记录：start_log → 设 4 台电机电压 → 保持 → end_log。
    rpm 用 |rpm| 的 mean；实际电压/电流用 mean。
    """
    link.start_log()
    set_voltage(link, group["driver0"], driver_voltage)
    set_voltage(link, group["driver1"], driver_voltage)
    set_voltage(link, group["idle"], 0.0)
    set_voltage(link, group["test"], test_voltage)
    link.sleep(hold)
    link.end_log()

    st = link.stats(list(ids.values()))

    def rpm_abs(key):
        return st[ids[key]]["abs_mean"]

    def mean(key):
        return st[ids[key]]["mean"]

    driver0_rpm = rpm_abs("driver0_rpm")
    driver1_rpm = rpm_abs("driver1_rpm")
    test_rpm = rpm_abs("test_rpm")

    wheel_rpm = avg_or_none(driver0_rpm, driver1_rpm)

    d0_i = mean("driver0_cur")
    d1_i = mean("driver1_cur")
    driver_current = None
    if d0_i is not None or d1_i is not None:
        driver_current = (d0_i or 0.0) + (d1_i or 0.0)

    return {
        "wheel_rpm": wheel_rpm,
        "driver0_rpm": driver0_rpm,
        "driver1_rpm": driver1_rpm,
        "test_rpm": test_rpm,
        "driver0_vact": mean("driver0_vact"),
        "driver1_vact": mean("driver1_vact"),
        "test_vact": mean("test_vact"),
        "driver_current": driver_current,
        "test_current": mean("test_cur"),
    }


def paired_point(
    link: BackendLink,
    group: dict,
    ids: dict,
    driver_voltage: float,
    test_voltage: float,
    args,
) -> tuple[dict, dict, dict]:
    """
    一次完整测试点：
      A. 被测电机 0V -> baseline
      B. 被测电机 test_voltage -> test

    每个 test_voltage 都重新测一次 baseline，减小电池和温度慢漂移影响。
    A、B 各是一段独立记录（收发段在 measure_state 里）。
    """
    baseline = measure_state(
        link,
        group,
        ids,
        driver_voltage,
        0.0,
        args.hold,
    )

    result = measure_state(
        link,
        group,
        ids,
        driver_voltage,
        test_voltage,
        args.hold,
    )

    b = baseline["wheel_rpm"]
    r = result["wheel_rpm"]
    delta_rpm = None if b is None or r is None else r - b

    result["baseline_wheel_rpm"] = b
    result["delta_rpm"] = delta_rpm

    bc = baseline["driver_current"]
    rc = result["driver_current"]
    result["driver_delta_current"] = None if bc is None or rc is None else rc - bc

    return baseline, result, {
        "baseline": baseline,
        "result": result,
        "delta_rpm": delta_rpm,
    }


# ------------------------------------------------------------
# 过零检测
# ------------------------------------------------------------


def interpolate_zero(prev_row: dict, row: dict) -> Optional[float]:
    """对 delta_rpm=0 做线性插值，优先使用被测电机实际电压。"""
    x0 = prev_row.get("test_vact")
    x1 = row.get("test_vact")
    y0 = prev_row.get("delta_rpm")
    y1 = row.get("delta_rpm")

    if None in (x0, x1, y0, y1):
        return None
    if abs(y1 - y0) < 1e-12:
        return (x0 + x1) * 0.5

    ratio = -y0 / (y1 - y0)
    return x0 + ratio * (x1 - x0)


def find_neutral(rows: list[dict], deadband: float) -> Optional[float]:
    """
    找首次从明显拖曳(delta < -deadband)跨到明显正驱(delta > +deadband)
    的过零点；如果某个点已经落进死区，则直接使用其实际电压。
    """
    prev = None

    for row in rows:
        delta = row.get("delta_rpm")
        if delta is None:
            continue

        vact = row.get("test_vact")
        if vact is None:
            continue

        # 0V 是 baseline，本身不是 neutral crossing；跳过
        if row.get("test_voltage_cmd", 0.0) <= 0.0:
            prev = row
            continue

        if abs(delta) <= deadband:
            return vact

        if prev is not None:
            pdelta = prev.get("delta_rpm")
            if pdelta is not None:
                if pdelta < -deadband and delta > deadband:
                    return interpolate_zero(prev, row)
                # 如果前一点已经很接近 0，本点往上越过，也允许插值
                if pdelta < 0.0 <= delta:
                    return interpolate_zero(prev, row)

        prev = row

    return None


# ------------------------------------------------------------
# 扫描
# ------------------------------------------------------------


def make_voltage_list(vmax: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("电压步长必须 > 0")
    n = int(math.floor(vmax / step + 1e-9))
    values = [round(i * step, 5) for i in range(n + 1)]
    if not values or values[-1] < vmax - 1e-9:
        values.append(round(vmax, 5))
    return values


def fine_range(lo: float, hi: float, step: float) -> list[float]:
    lo = max(0.0, lo)
    hi = max(lo, hi)
    return make_voltage_list(hi, step)[
        : int(math.floor(lo / step + 1e-9)) + 1
    ] + []


def unique_sorted(values):
    return sorted(set(round(v, 5) for v in values))


def coarse_bracket(rows: list[dict], deadband: float):
    """找到第一个可能的过零电压区间。"""
    prev = None
    for row in rows:
        if row.get("test_voltage_cmd", 0.0) <= 0.0:
            continue
        d = row.get("delta_rpm")
        if d is None:
            continue

        if abs(d) <= deadband:
            v = row["test_voltage_cmd"]
            return max(0.0, v - 1.0), min(12.0, v + 1.0)

        if prev is not None:
            pd = prev.get("delta_rpm")
            if pd is not None and pd < 0.0 <= d:
                return prev["test_voltage_cmd"], row["test_voltage_cmd"]

        prev = row
    return None


def scan_driver_voltage(
    link: BackendLink,
    group: dict,
    ids: dict,
    driver_voltage: float,
    args,
    rows: list[dict],
) -> Optional[dict]:
    """
    对一个固定的 0/1 驱动电压 D：
      粗扫 test 0~12V
      如找到 crossing，再细扫 crossing 附近
      输出这个速度点的 Vneutral
    """
    print(f"\n--- 驱动电压 D={driver_voltage:.2f} V ---")

    # 粗扫
    coarse = []
    for test_cmd in make_voltage_list(args.test_vmax, args.coarse_step):
        baseline, result, extra = paired_point(
            link, group, ids,
            driver_voltage, test_cmd, args,
        )

        row = {
            "motor": args._current_motor,
            "driver_voltage_cmd": driver_voltage,
            "stage": "coarse",
            "test_voltage_cmd": test_cmd,
            "baseline_wheel_rpm": baseline["wheel_rpm"],
            "wheel_rpm": result["wheel_rpm"],
            "delta_rpm": result["delta_rpm"],
            "driver0_rpm": result["driver0_rpm"],
            "driver1_rpm": result["driver1_rpm"],
            "test_rpm": result["test_rpm"],
            "driver0_vact": result["driver0_vact"],
            "driver1_vact": result["driver1_vact"],
            "test_vact": result["test_vact"],
            "driver_current": result["driver_current"],
            "driver_delta_current": result["driver_delta_current"],
            "test_current": result["test_current"],
        }
        coarse.append(row)
        rows.append(row)

        print(
            "  [粗] Tcmd=%5.2f V | Tact=%6s V | "
            "Wbase=%7s | W=%7s | Δ=%+7s | "
            "I驱=%6s | I测=%6s"
            % (
                test_cmd,
                "-" if row["test_vact"] is None else f"{row['test_vact']:.3f}",
                "-" if row["baseline_wheel_rpm"] is None else f"{row['baseline_wheel_rpm']:.2f}",
                "-" if row["wheel_rpm"] is None else f"{row['wheel_rpm']:.2f}",
                "-" if row["delta_rpm"] is None else f"{row['delta_rpm']:+.2f}",
                "-" if row["driver_current"] is None else f"{row['driver_current']:.3f}",
                "-" if row["test_current"] is None else f"{row['test_current']:.3f}",
            )
        )

        # 粗扫一旦已经明显跨过 0，可以提前结束；避免高压测试无必要继续。
        if len(coarse) >= 2:
            d0 = coarse[-2]["delta_rpm"]
            d1 = coarse[-1]["delta_rpm"]
            if d0 is not None and d1 is not None:
                if d0 < 0.0 <= d1:
                    break

    neutral_coarse = find_neutral(coarse, args.rpm_deadband)
    neutral_final = neutral_coarse

    # 细扫：围绕第一个 crossing 区间重新测更密的数据。
    bracket = coarse_bracket(coarse, args.rpm_deadband)
    if bracket is not None:
        lo, hi = bracket
        pad = args.coarse_step
        lo = max(0.0, lo - pad)
        hi = min(args.test_vmax, hi + pad)

        print(f"  粗扫找到区域 {lo:.2f}~{hi:.2f} V，进入细扫")

        fine_vs = []
        n = int(math.floor((hi - lo) / args.fine_step + 1e-9))
        for i in range(n + 1):
            fine_vs.append(round(lo + i * args.fine_step, 5))
        if not fine_vs or fine_vs[-1] < hi - 1e-9:
            fine_vs.append(round(hi, 5))
        fine_vs = unique_sorted(fine_vs)

        fine = []
        for test_cmd in fine_vs:
            baseline, result, extra = paired_point(
                link, group, ids,
                driver_voltage, test_cmd, args,
            )

            row = {
                "motor": args._current_motor,
                "driver_voltage_cmd": driver_voltage,
                "stage": "fine",
                "test_voltage_cmd": test_cmd,
                "baseline_wheel_rpm": baseline["wheel_rpm"],
                "wheel_rpm": result["wheel_rpm"],
                "delta_rpm": result["delta_rpm"],
                "driver0_rpm": result["driver0_rpm"],
                "driver1_rpm": result["driver1_rpm"],
                "test_rpm": result["test_rpm"],
                "driver0_vact": result["driver0_vact"],
                "driver1_vact": result["driver1_vact"],
                "test_vact": result["test_vact"],
                "driver_current": result["driver_current"],
                "driver_delta_current": result["driver_delta_current"],
                "test_current": result["test_current"],
            }
            fine.append(row)
            rows.append(row)

            print(
                "  [细] Tcmd=%5.2f V | Tact=%6s V | "
                "W=%7s | Δ=%+7s | I驱=%6s | I测=%6s"
                % (
                    test_cmd,
                    "-" if row["test_vact"] is None else f"{row['test_vact']:.3f}",
                    "-" if row["wheel_rpm"] is None else f"{row['wheel_rpm']:.2f}",
                    "-" if row["delta_rpm"] is None else f"{row['delta_rpm']:+.2f}",
                    "-" if row["driver_current"] is None else f"{row['driver_current']:.3f}",
                    "-" if row["test_current"] is None else f"{row['test_current']:.3f}",
                )
            )

        if fine:
            neutral_final = find_neutral(fine, args.rpm_deadband)

    # 基准速度用最后一次/最低测试电压的 paired baseline；这里重新取一次干净值
    baseline = measure_state(
        link,
        group,
        ids,
        driver_voltage,
        0.0,
        args.hold,
    )

    print(
        "  => wheel=%.2f pct, Vneutral=%s"
        % (
            baseline["wheel_rpm"] if baseline["wheel_rpm"] is not None else float("nan"),
            "%.3f V" % neutral_final if neutral_final is not None else "未找到",
        )
    )

    return {
        "motor": args._current_motor,
        "driver_voltage_cmd": driver_voltage,
        "wheel_rpm": baseline["wheel_rpm"],
        "neutral_voltage": neutral_final,
    }


# ------------------------------------------------------------
# CSV
# ------------------------------------------------------------

RAW_FIELDS = [
    "motor",
    "driver_voltage_cmd",
    "stage",
    "test_voltage_cmd",
    "test_vact",
    "baseline_wheel_rpm",
    "wheel_rpm",
    "delta_rpm",
    "driver0_rpm",
    "driver1_rpm",
    "test_rpm",
    "driver0_vact",
    "driver1_vact",
    "driver_current",
    "driver_delta_current",
    "test_current",
]

SUMMARY_FIELDS = [
    "motor",
    "driver_voltage_cmd",
    "wheel_rpm",
    "neutral_voltage",
]


# ------------------------------------------------------------
# 主程序
# ------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="VEX 60:48 齿轮组零扭矩电压曲线测试（复用现有 0x84 协议）"
    )
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址，默认本机 8000")
    ap.add_argument(
        "--motors",
        default="L2,L3,R2,R3",
        help="要测试的 48T 电机，逗号分隔，例如 L2,L3",
    )
    ap.add_argument(
        "--driver-volts",
        default="3,4,5,6,7,8,9,10,11,12",
        help="0/1 两台 60T 电机的固定驱动电压点",
    )
    ap.add_argument("--test-vmax", type=float, default=12.0)
    ap.add_argument("--coarse-step", type=float, default=0.25)
    ap.add_argument("--fine-step", type=float, default=0.05)
    ap.add_argument("--hold", type=float, default=0.40)
    ap.add_argument(
        "--rpm-deadband",
        type=float,
        default=1.5,
        help="delta_rpm 在此范围内认为已经接近零扭矩",
    )
    ap.add_argument("--cooldown", type=float, default=0.5)
    ap.add_argument("--csv", default="pytest/neutral_curve_raw.csv", help="原始数据 CSV，空字符串跳过")
    ap.add_argument("--summary", default="pytest/neutral_curve_summary.csv", help="曲线数据 CSV，空字符串跳过")
    args = ap.parse_args()

    if args.hold <= 0:
        raise ValueError("--hold 必须 > 0")
    if args.test_vmax <= 0 or args.test_vmax > 12:
        raise ValueError("--test-vmax 必须在 0~12V")
    if args.coarse_step <= 0 or args.fine_step <= 0:
        raise ValueError("扫描步长必须 > 0")

    motors = [x.strip().upper() for x in args.motors.split(",") if x.strip()]
    driver_voltages = [
        float(x.strip())
        for x in args.driver_volts.split(",")
        if x.strip()
    ]

    for name in motors:
        if len(name) != 2 or name[0] not in ("L", "R") or name[1] not in ("2", "3"):
            raise ValueError(f"非法被测电机 {name}，只能是 L2/L3/R2/R3")

    for v in driver_voltages:
        if not (0.0 < v <= 12.0):
            raise ValueError("--driver-volts 中的值必须在 0~12V")

    raw_rows = []
    summary_rows = []

    # with 退出时 __exit__ 自动收车收尾（目标归零 / 8电机0V / 刹车 / 停记录 / 退订 / 断连接）
    with BackendLink(args.base) as link:
        directory = link.mon_dir
        try:
            print("监控表共 %d 项" % len(directory))

            # 先检查所有将用到的监控项
            required_names = set()
            for test_name in motors:
                group = group_for_test(test_name)
                for role in ("driver0", "driver1", "test"):
                    required_names.add(side_id_name(group[role]))

            missing = []
            for name in sorted(required_names):
                cn = motor_names(name)
                for key in ("rpm", "vact", "cur"):
                    if cn[key] not in directory:
                        missing.append(f"{name}:{key}")

            if missing:
                print("以下监控项缺失：")
                for x in missing:
                    print("  ", x)
                sys.exit(1)

            all_zero(link)
            link.sleep(1.0)

            for test_name in motors:
                args._current_motor = test_name
                group = group_for_test(test_name)
                ids = build_ids(link, group)
                subscribe_for_group(link, ids)

                print("\n================================================")
                print(f"开始测试 {test_name}")
                print(
                    "驱动电机：%s、%s（60T） | 被测：%s（48T） | 闲置：%s（48T）"
                    % (
                        side_id_name(group["driver0"]),
                        side_id_name(group["driver1"]),
                        side_id_name(group["test"]),
                        side_id_name(group["idle"]),
                    )
                )
                print("================================================")

                for dv in driver_voltages:
                    summary = scan_driver_voltage(
                        link,
                        group,
                        ids,
                        dv,
                        args,
                        raw_rows,
                    )
                    summary_rows.append(summary)

                    # 每个速度点结束后先全部 0V，再冷却一小段时间。
                    all_zero(link)
                    link.sleep(args.cooldown)

            print("\n================ 最终汇总 ================")
            print("motor  wheel_pct    Vneutral")
            for row in summary_rows:
                print(
                    "%4s   %8s     %s"
                    % (
                        row["motor"],
                        "-" if row["wheel_rpm"] is None else f"{row['wheel_rpm']:.2f}",
                        "-" if row["neutral_voltage"] is None else f"{row['neutral_voltage']:.3f} V",
                    )
                )

            print("\n原始数据：", args.csv)
            print("曲线数据：", args.summary)

        except KeyboardInterrupt:
            print("\n用户中断，正在把全部电机收回 0V...")

        all_zero(link)
        if args.csv and raw_rows:
            link.save_csv(args.csv, raw_rows, RAW_FIELDS)
        if args.summary and summary_rows:
            link.save_csv(args.summary, summary_rows, SUMMARY_FIELDS)


if __name__ == "__main__":
    main()