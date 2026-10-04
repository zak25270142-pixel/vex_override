# -*- coding: utf-8 -*-
"""
接入 60:48 齿轮组后的零扭矩电压曲线测试。

不需要修改 V5 固件，直接复用现有 motor_v_test.py：
    0x84 -> 单电机直接给电压
    监控目录 -> 读取实际电压 / 转速 / 电流

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

示例：
    python measure_neutral_curve.py --motors L2
    python measure_neutral_curve.py --motors L2,L3,R2,R3

只想测较低范围：
    python measure_neutral_curve.py --motors L2 --driver-volts 3,4,5,6,7,8

减小/增加扫描步长：
    python measure_neutral_curve.py --coarse-step 0.25 --fine-step 0.05
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from typing import Optional

from motor_v_test import probe_port, Link, find_motor_items


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


def set_voltage(link: Link, mid: int, volts: float) -> None:
    """调用现有 0x84。"""
    v = max(-12.0, min(12.0, float(volts)))
    link.set_volt(mid, v)


def all_zero(link: Link) -> None:
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


def build_ids(items: dict, group: dict) -> dict:
    """根据现有 find_motor_items() 的 L0~R3 名字建立本测试用监控 index。"""
    out = {}
    for role in ("driver0", "driver1", "test"):
        name = side_id_name(group[role])
        entry = items[name]
        for key in ("rpm", "vact", "cur"):
            if entry[key] is None:
                raise RuntimeError(f"监控项缺失：{name} / {key}")
            out[f"{role}_{key}"] = entry[key]
    return out


def subscribe_for_group(link: Link, ids: dict) -> None:
    """只订阅本次实验需要的 3 台电机 × 3 项监控。"""
    link.unsubscribe_all()
    time.sleep(0.12)

    sent = set()
    for key, idx in ids.items():
        if idx not in sent:
            link.subscribe(idx)
            sent.add(idx)
    time.sleep(0.25)


# ------------------------------------------------------------
# 采样
# ------------------------------------------------------------


def get_stats(link: Link, idx: int, t0: float, t1: float):
    return link.window_stats(idx, t0, t1)


def stable_sample(
    link: Link,
    ids: dict,
    hold: float,
    sample_ratio: float,
) -> dict:
    """
    当前电压状态保持 hold 秒，后半段做统计。

    rpm 使用 |rpm| 的 mean；实际电压/current 使用 mean。
    """
    t0 = time.perf_counter()
    time.sleep(hold)
    t1 = time.perf_counter()

    ws = t0 + hold * sample_ratio

    d0_r = get_stats(link, ids["driver0_rpm"], ws, t1)
    d1_r = get_stats(link, ids["driver1_rpm"], ws, t1)
    t_r = get_stats(link, ids["test_rpm"], ws, t1)

    d0_v = get_stats(link, ids["driver0_vact"], ws, t1)
    d1_v = get_stats(link, ids["driver1_vact"], ws, t1)
    t_v = get_stats(link, ids["test_vact"], ws, t1)

    d0_i = get_stats(link, ids["driver0_cur"], ws, t1)
    d1_i = get_stats(link, ids["driver1_cur"], ws, t1)
    t_i = get_stats(link, ids["test_cur"], ws, t1)

    def rpm_abs(st):
        return None if st is None else st["abs_mean"]

    def mean(st):
        return None if st is None else st["mean"]

    driver0_rpm = rpm_abs(d0_r)
    driver1_rpm = rpm_abs(d1_r)
    test_rpm = rpm_abs(t_r)

    wheel_rpm = avg_or_none(driver0_rpm, driver1_rpm)

    driver_current = None
    if d0_i is not None or d1_i is not None:
        driver_current = (d0_i["mean"] if d0_i else 0.0) + (d1_i["mean"] if d1_i else 0.0)

    return {
        "wheel_rpm": wheel_rpm,
        "driver0_rpm": driver0_rpm,
        "driver1_rpm": driver1_rpm,
        "test_rpm": test_rpm,
        "driver0_vact": mean(d0_v),
        "driver1_vact": mean(d1_v),
        "test_vact": mean(t_v),
        "driver_current": driver_current,
        "test_current": mean(t_i),
    }


def measure_state(
    link: Link,
    group: dict,
    ids: dict,
    driver_voltage: float,
    test_voltage: float,
    hold: float,
    sample_ratio: float,
) -> dict:
    """设置一个状态后等待稳定并采样。"""
    set_voltage(link, group["driver0"], driver_voltage)
    set_voltage(link, group["driver1"], driver_voltage)
    set_voltage(link, group["test"], test_voltage)
    set_voltage(link, group["idle"], 0.0)

    return stable_sample(link, ids, hold, sample_ratio)


def paired_point(
    link: Link,
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

    每个 test_voltage 都重新测 baseline，减小电池和温度慢漂移影响。
    """
    baseline = measure_state(
        link,
        group,
        ids,
        driver_voltage,
        0.0,
        args.hold,
        args.sample_ratio,
    )

    result = measure_state(
        link,
        group,
        ids,
        driver_voltage,
        test_voltage,
        args.hold,
        args.sample_ratio,
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
    link: Link,
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
        args.sample_ratio,
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


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_summary(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ------------------------------------------------------------
# 主程序
# ------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="VEX 60:48 齿轮组零扭矩电压曲线测试（复用现有 0x84 协议）"
    )
    ap.add_argument("--port", default="auto")
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
        "--sample-ratio",
        type=float,
        default=0.50,
        help="每档只统计后百分之多少，例如 0.5=后50%%",
    )
    ap.add_argument(
        "--rpm-deadband",
        type=float,
        default=1.5,
        help="delta_rpm 在此范围内认为已经接近零扭矩",
    )
    ap.add_argument("--cooldown", type=float, default=0.5)
    ap.add_argument("--csv", default="neutral_curve_raw.csv")
    ap.add_argument("--summary", default="neutral_curve_summary.csv")
    args = ap.parse_args()

    if not (0.0 < args.sample_ratio < 1.0):
        raise ValueError("--sample-ratio 必须在 0~1 之间")
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

    port = probe_port() if args.port == "auto" else args.port
    if not port:
        print("没有找到会回 Pong 的 V5 User 口")
        sys.exit(1)

    print("使用端口：", port)
    link = Link(port)
    raw_rows = []
    summary_rows = []

    try:
        time.sleep(0.3)

        directory = link.fetch_monitor_dir()
        print("监控表共 %d 项" % len(directory))

        items = find_motor_items(directory)

        # 先检查所有将用到的监控项
        required_names = set()
        for test_name in motors:
            group = group_for_test(test_name)
            for role in ("driver0", "driver1", "test"):
                required_names.add(side_id_name(group[role]))

        missing = []
        for name in sorted(required_names):
            for key in ("rpm", "vact", "cur"):
                if items[name].get(key) is None:
                    missing.append(f"{name}:{key}")

        if missing:
            print("以下监控项缺失：")
            for x in missing:
                print("  ", x)
            sys.exit(1)

        all_zero(link)
        time.sleep(1.0)

        for test_name in motors:
            args._current_motor = test_name
            group = group_for_test(test_name)
            ids = build_ids(items, group)
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
                time.sleep(args.cooldown)

            # 一台测完退订，下一台再订阅。
            link.unsubscribe_all()
            time.sleep(0.12)

        # 保存结果
        write_csv(args.csv, raw_rows)
        write_summary(args.summary, summary_rows)

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
    finally:
        all_zero(link)
        try:
            link.unsubscribe_all()
        except Exception:
            pass
        link.close()


if __name__ == "__main__":
    main()
