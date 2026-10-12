#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
speed_loop_probe.py —— 速度环低速粘滑问题的实车诊断脚本（后端模式）

目的（用数据回答一个争论）：
    低速来回抖，到底是 PID 参数不合适，还是旧电压映射(V=sign(out)*(摩擦+...))的结构锅？
脚本做两组短脉冲实验（全程只直线前进，勤停车，给有线连接留足安全余量）：
    1) open   开环电压特性：整车下"多低电压能起步 / 滚动中多低电压会停"，
             得到 维持小速度到底需要几伏 —— 直接对照闭环最低正电压(摩擦+volt_min≈1.5V)。
    2) closed 闭环小目标 + 不同增益组：记录指令电压 Vcmd 是否在正负之间反打、
             转速抖动幅度。若任何增益组都存在 Vcmd 正负反打 → 结构问题；
             若增益换了就稳 → 参数问题。

通信方式（与 tools 版最大的区别）：
    不再自己开串口，全部走后端 HTTP（backend/app/script_api.py 的 BackendLink）。
    动作/调参转发给后端；监控数据由**后端**在记录期间采样并带时间戳，
    脚本只负责"声明记哪些项 + 何时开始"，再用波/统计接口按区间取值——
    时间轴就是"相对 start_log() 的秒数"，命令响应里直接带回打点 t，不用对齐本地时钟（现在每个脉冲/档位各自成段，段内时间轴自成一条曲线）。

安全设计（重要）：
    - 只发直线等速目标，不转向；单次动作 ≤1.0s，每次动作后滑行/刹车 0.8s 以上
    - 用定位轮里程(全局X)和航向做硬围栏：单次位移 >0.35m 或航向变化 >15° 立即收车退出
    - 围栏不单开线程了，改由脉冲循环按 30ms 周期主动 check()（配合上位机看得到车）
    - 每组增益之间暂停，等你把车摆回起点再继续（--no-prompt 可跳过）
    - 任何异常/Ctrl+C：速度环目标归零 → 8电机0V滑行 → 0x80刹车 → 退订
    - 上位机停车按钮会置后端 abort，循环里 check 到立刻抛错中止

用法：
    先起后端：uvicorn app.main:app --host 127.0.0.1 --port 8000
    然后：python scripts/speed_loop_probe.py               # open+closed 全跑
          python scripts/speed_loop_probe.py --phase open  # 只跑开环
          python scripts/speed_loop_probe.py --tag old_mapping   # 给数据文件打标记(如固件版本)
数据写到 backend/pytest/probe_data/ 下：每试次的指标汇总 csv + 完整波形 csv。
"""

import argparse
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.script_api import BackendLink  # noqa: E402

# 关心的监控项中文名（来自 LCD_menu.cpp 注册表）
MON_OPEN = ["左0转速", "右0转速", "左0实际电压", "右0实际电压", "全局X", "全局Y", "航向"]
MON_CLOSED = ["左指令转速", "右指令转速", "左0转速", "右0转速",
              "左0指令电压", "右0指令电压", "左0实际电压", "全局X", "全局Y", "航向"]
NAME_X, NAME_Y, NAME_YAW = "全局X", "全局Y", "航向"

# 调参项中文名（调参目录下发的是 Chinese_name，不是代码里的 item_name）
GAIN_KEYS = ["左kf 输出/pct", "左kp 输出/pct", "左ki内部÷1e6", "左kd内部×1e6",
             "右kf 输出/pct", "右kp 输出/pct", "右ki内部÷1e6", "右kd内部×1e6"]
GAIN_NAME = {"L": {"kf": "左kf 输出/pct", "kp": "左kp 输出/pct",
                   "ki": "左ki内部÷1e6", "kd": "左kd内部×1e6"},
             "R": {"kf": "右kf 输出/pct", "kp": "右kp 输出/pct",
                   "ki": "右ki内部÷1e6", "kd": "右kd内部×1e6"}}

# 闭环增益组：(组名, kf, kp, ki每秒, kd每秒, 摩擦垫设置)
# 摩擦垫设置=None 用当前调参值；否则为 (静摩擦V, 动摩擦V, volt_min V×8台)
# 注意 ki/kd 调参表里存的是内部值 = 每秒值×1e-6，脚本下发时自行换算。
GAIN_GROUPS = [
    # --- 前馈扫掠（ki=0，看纯前馈残差分布）---
    ("ff05_hi",  0.005, 0.0,   0.0, 0.0, (2.0, 1.0, 0.5)),
    ("ff06_hi",  0.006, 0.0,   0.0, 0.0, (2.0, 1.0, 0.5)),
    ("ff07_hi",  0.007, 0.0,   0.0, 0.0, (2.0, 1.0, 0.5)),
    ("ff05_lo",  0.005, 0.0,   0.0, 0.0, (1.3, 0.6, 0.5)),
    ("ff06_lo",  0.006, 0.0,   0.0, 0.0, (1.3, 0.6, 0.5)),
    ("ff07_lo",  0.007, 0.0,   0.0, 0.0, (1.3, 0.6, 0.5)),
    # --- 积分扫掠（kf=0.006 主候选，ki 从 0.1 到 0.4）---
    ("pi01_hi",  0.006, 0.01,  0.1, 0.0, (2.0, 1.0, 0.5)),
    ("pi02_hi",  0.006, 0.01,  0.2, 0.0, (2.0, 1.0, 0.5)),
    ("pi04_hi",  0.006, 0.01,  0.4, 0.0, (2.0, 1.0, 0.5)),
    ("pi01_lo",  0.006, 0.01,  0.1, 0.0, (1.3, 0.6, 0.5)),
    ("pi02_lo",  0.006, 0.01,  0.2, 0.0, (1.3, 0.6, 0.5)),
    ("pi04_lo",  0.006, 0.01,  0.4, 0.0, (1.3, 0.6, 0.5)),
    # --- kp 扫掠（确认比例项必要性）---
    ("kp005",    0.006, 0.005, 0.2, 0.0, (1.3, 0.6, 0.5)),
    ("kp010",    0.006, 0.01,  0.2, 0.0, (1.3, 0.6, 0.5)),
    ("kp020",    0.006, 0.02,  0.2, 0.0, (1.3, 0.6, 0.5)),
    # --- 基准对照（原固件默认值）---
    ("baseline", 0.008, 0.01,  0.3, 0.0, None),
]
PAD_NAMES = ["左静摩擦", "左动摩擦", "右静摩擦", "右动摩擦"] + \
            [f"左{i}最小电压" for i in range(4)] + [f"右{i}最小电压" for i in range(4)]
SPEED_TARGETS = [3.0, 5.0, 10.0, 20.0]   # pct；3/5pct 是低速重点，10/20pct 验证高速不欠速

# 指标汇总 CSV 的列名（sum_rows 每行按顺序落位，短行后面留空）
SUM_COLS = ["phase", "step", "L_mean", "R_mean", "L_std", "R_std",
            "note1", "note2", "note3", "note4"]

# 开环电压扫描（V）：8电机同电压（整车直线，左右抵消转向力矩）
VOLT_UP = [round(0.3 + 0.1 * i, 2) for i in range(14)]    # 0.3~1.6，每档重新起步
VOLT_DOWN = [round(1.6 - 0.1 * i, 2) for i in range(15)]  # 1.6~0.2，滚动中连续扫

# ===== 安全限位（新版核心：累积位移归零，不靠单脉冲赌运气）=====
FENCE_X_M = 1.0       # 相对会话原点的纵向围栏：越过立即收车中止（用户允许1.5m，留余量取1.0）
FENCE_Y_M = 0.30      # 横向围栏（走偏保护）
FENCE_YAW_DEG = 30.0  # 航向围栏（防止扫成转圈绕线）
PULSE_VOLT_S = 0.5    # 开环单档脉冲时长
PULSE_SPIN_S = 0.8    # 闭环单目标脉冲时长
RETURN_SPEED = 6.0    # 倒车复位速度 pct（慢，留制动余量）
RETURN_TOL_M = 0.03   # 回到原点 3cm 内视为复位
RETURN_EARLY_M = 0.06 # 距原点 6cm 就断电滑行，绝不带着速度过零反向窜
HOME_GUARD_S = 0.35   # 脉冲后静止滑行观察时间
STOP_SETTLE_S = 1.5   # 倒车前先刹停并等待真正零速的最长时间
WD_PERIOD_S = 0.03    # 围栏主动 check 的周期
PULSE_BUDGET_M = 0.25 # 单个测试脉冲自身的位移预算（不含倒车复位）
OVERSPEED_HOLD_S = 0.12  # 超速持续多久才判定蹿冲（滤掉单样本毛刺）
# ---- 原地旋转模式专用（高速段标定；防车飘、防线缆缠绕）----
ROTATE_DRIFT_M = 0.15      # 单脉冲旋转中心 2D 飘移上限：超过即收车（车飘了）
ROTATE_YAW_BUDGET = 270.0  # 单脉冲航向累计上限（度）：最多转 3/4 圈，防止缠线
ROTATE_SETTLE_S = 0.6      # 旋转脉冲间刹停等零速时长（比直线长，角动量更大）


def stats(vals):
    if not vals:
        return 0.0, 0.0, 0.0, 0.0
    n = len(vals)
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n
    return mean, min(vals), max(vals), var ** 0.5


class Watchdog:
    """位置/超速保护：不再自开线程，改由主流程在脉冲循环里按 WD_PERIOD_S 主动 check()。

    多层保险之一；另有：上位机停车置 abort（check 到立即中止）、
    主控心跳看门狗（脚本死掉 2.56s 后固件 on_link_lost 自己收车）。
    """

    def __init__(self, link):
        self.L = link
        self.armed = False
        self.tripped = None  # 触发原因字符串，None=正常
        self.ox = self.oy = self.oyaw = 0.0
        self.pb = None       # 单脉冲位移预算（m），None=不启用
        self.px = self.py = 0.0     # 脉冲起点 X/Y
        self.vlim = None     # 转速上限(pct)，任一侧持续超速即收车
        # 旋转脉冲专用：2D 飘移上限(m)、脉冲航向预算(度)及连续航向累加
        self.drift_m = None
        self.yaw_budget = None
        self._pyaw0 = 0.0
        self._yaw_cont = 0.0
        self._yaw_prev = 0.0
        self.yaw_fence_on = True  # 旋转会话中车就是要转，关闭会话级航向围栏
        self._v_since = None

    def arm(self, rotate=False):
        """以当前车位为会话原点布防（车被人工摆回起点后可重新 arm）"""
        self.ox = self.L.latest(NAME_X)
        self.oy = self.L.latest(NAME_Y)
        self.oyaw = self.L.latest(NAME_YAW)
        self.yaw_fence_on = not rotate
        self.tripped = None
        self.armed = True
        mode = "原地旋转（关航向围栏，脉冲级飘移/转角保护）" if rotate else "直线"
        print(f"[wd] 已布防[{mode}] 原点 x={self.ox:+.3f} y={self.oy:+.3f} yaw={self.oyaw:+.1f}")

    def begin_pulse(self, vlim, budget=PULSE_BUDGET_M,
                    drift_m=None, yaw_budget_deg=None):
        """脉冲开始：记录起点并启用超速/位移双保护。
        旋转脉冲传 drift_m + yaw_budget_deg，并把 budget 传 None 关闭纵向预算。"""
        self.px = self.L.latest(NAME_X)
        self.py = self.L.latest(NAME_Y)
        self.pb = budget
        self.vlim = vlim
        self.drift_m = drift_m
        self.yaw_budget = yaw_budget_deg
        y0 = self.L.latest(NAME_YAW)
        self._pyaw0 = self._yaw_cont = self._yaw_prev = y0
        self._v_since = None

    def end_pulse(self):
        self.pb = None
        self.vlim = None
        self.drift_m = None
        self.yaw_budget = None
        self._v_since = None

    def _emergency(self, reason):
        # 不依赖主流程，直接三重收车
        try:
            self.L.spin_target(0.0, 0.0)
            self.L.drive_volt(0.0)
            self.L.stop_move()
        except Exception:
            pass
        self.tripped = reason
        self.armed = False
        print(f"\n[wd] 围栏触发收车：{reason}")

    def _fence_round(self):
        """走一遍围栏判定（原来是独立线程里每秒几十拍，现在由 check() 每拍调一次）。"""
        xn = self.L.latest(NAME_X)
        yn = self.L.latest(NAME_Y)
        yawn = self.L.latest(NAME_YAW)
        dx = xn - self.ox
        dy = yn - self.oy
        if self.drift_m is not None:
            # 旋转脉冲：只信脉冲级保护（飘移+转角+超速），会话/纵向围栏不适用
            drift = math.hypot(xn - self.px, yn - self.py)
            # 航向连续化：每拍增量折到 ±180°，防 IMU 任何跳变
            d = (yawn - self._yaw_prev + 180.0) % 360.0 - 180.0
            self._yaw_prev = yawn
            self._yaw_cont += d
            turned = abs(self._yaw_cont - self._pyaw0)
            if drift > self.drift_m:
                self._emergency(f"旋转中心飘移 {drift:.3f}m 超过 {self.drift_m}m（车飘了）")
            elif turned > self.yaw_budget:
                self._emergency(f"单脉冲转角 {turned:.0f}° 超过 {self.yaw_budget:.0f}°（防缠线）")
        else:
            # 直线脉冲：会话级 x/y/yaw 围栏 + 单脉冲纵向预算
            dyaw = yawn - self.oyaw
            if abs(dx) > FENCE_X_M:
                self._emergency(f"纵向位移 {dx:+.3f}m 超过 ±{FENCE_X_M}m")
            elif abs(dy) > FENCE_Y_M:
                self._emergency(f"横向偏移 {dy:+.3f}m 超过 ±{FENCE_Y_M}m")
            elif self.yaw_fence_on and abs(dyaw) > FENCE_YAW_DEG:
                self._emergency(f"航向变化 {dyaw:+.1f}° 超过 ±{FENCE_YAW_DEG}°")
            elif self.pb is not None and abs(xn - self.px) > self.pb:
                self._emergency(f"单脉冲位移 {xn - self.px:+.3f}m 超过预算 {self.pb}m")
        if self.vlim is not None:
            spd = max(abs(self.L.latest("左0转速")),
                      abs(self.L.latest("右0转速")))
            now = time.perf_counter()
            if spd > self.vlim:
                if self._v_since is None:
                    self._v_since = now
                elif now - self._v_since > OVERSPEED_HOLD_S:
                    self._emergency(f"转速 {spd:.0f}pct 持续超过上限 {self.vlim:.0f}")
            else:
                self._v_since = None

    def check(self):
        """主流程按 WD_PERIOD_S 周期调用：先看后端 abort，再走围栏判定。"""
        self.L.check_abort()  # 上位机停车 → 立即抛错中止脉冲
        if self.armed and self.tripped is None:
            try:
                self._fence_round()
            except Exception:
                pass  # 数据暂缺时跳过这一拍，别影响正常流程
        if self.tripped:
            raise RuntimeError("watchdog: " + self.tripped)

    def close(self):
        pass


class Probe:
    def __init__(self, link, outdir, tag, wd):
        self.L = link
        self.wd = wd
        self.outdir = outdir
        self.tag = tag
        self.sum_rows = []
        self._wave_seq = 0
        self.abort = False
        self.cur_settings = None  # 当前实验组参数，回程临时改纯前馈后据此恢复

    def _sleep(self, dur):
        """分片睡眠：围栏/停车触发的瞬间立即中止当前脉冲"""
        t_end = time.perf_counter() + dur
        while time.perf_counter() < t_end:
            self.wd.check()
            time.sleep(WD_PERIOD_S)

    def halt(self):
        """静止：先0目标惰行，再stop挂起速度环，再8电机0V（开环压测定心用）"""
        self.L.spin_target(0.0, 0.0)
        time.sleep(0.05)
        self.L.stop_move()
        time.sleep(0.05)
        self.L.drive_volt(0.0)

    def _wait_stop(self, timeout=STOP_SETTLE_S):
        """刹停后等两侧转速真正归零（连续0.2s <2pct），防止带速反向起步蹿车。"""
        t_end = time.perf_counter() + timeout
        quiet = 0.0
        while time.perf_counter() < t_end:
            self.wd.check()
            spd = max(abs(self.L.latest("左0转速")), abs(self.L.latest("右0转速")))
            quiet = quiet + WD_PERIOD_S if spd < 2.0 else 0.0
            if quiet >= 0.2:
                return True
            time.sleep(WD_PERIOD_S)
        return False

    def return_home(self):
        """安全倒车复位：先彻底刹停等零速 → 临时切纯前馈(消除PI粘滑极限环)慢倒
        → 距原点6cm提前断电 → 刹停 → 恢复当前实验组增益。"""
        self.wd.end_pulse()  # 复位是受控动作，关掉脉冲预算，只保留会话围栏+超速保护
        self.L.stop_move()   # 电机制动（不是0V惰行），尽快消掉前进余速
        self._wait_stop()
        self.apply_settings(0.008, 0.0, 0.0, 0.0, None, remember=False)  # 回程用纯前馈
        self.wd.begin_pulse(vlim=20.0, budget=None)
        try:
            dx = self.L.latest(NAME_X) - self.wd.ox
            if abs(dx) > RETURN_TOL_M:
                target = -RETURN_SPEED if dx > 0 else RETURN_SPEED
                self.L.spin_target(target, target)
                t_end = time.perf_counter() + 4.0
                while time.perf_counter() < t_end:
                    self.wd.check()
                    if abs(self.L.latest(NAME_X) - self.wd.ox) <= RETURN_EARLY_M:
                        break
                    time.sleep(WD_PERIOD_S)
                self.L.spin_target(0.0, 0.0)  # 提前断电滑行剩余 3~6cm
                self._sleep(0.4)
        finally:
            self.wd.end_pulse()
            if self.cur_settings is not None:
                self.apply_settings(*self.cur_settings)  # 恢复实验组增益
        self.L.stop_move()  # 滑行末段制动，避免越过原点
        self._wait_stop(0.8)
        self.L.drive_volt(0.0)
        dx = self.L.latest(NAME_X) - self.wd.ox
        print(f"    [复位] 残差 {dx:+.3f}m")
        if abs(dx) > 0.10:
            raise RuntimeError(f"复位失败，残差 {dx:+.3f}m，人工检查")

    def save_wave(self, prefix, wave):
        """把一段波形写成"长表"CSV（t_rel_s/item/value）。wave 形如 {名字: [(秒, 值), ...]}。"""
        names = [n for n in wave if wave[n]]
        if not names:
            return None
        self._wave_seq += 1
        t_base = min(tt[0] for n in names for tt in wave[n])
        rows = [{"t_rel_s": f"{t - t_base:.4f}", "item": n, "value": f"{v:.4f}"}
                for n in names for t, v in wave[n]]
        path = os.path.join(self.outdir, f"{self.tag}_{prefix}_{self._wave_seq:02d}_wave.csv")
        return self.L.save_csv(path, rows, ["t_rel_s", "item", "value"], quiet=True)

    # ---------------- 实验1：开环电压特性（整车8电机同压，每脉冲后倒车回原点） ----------------
    def phase_open(self):
        L = self.L
        L.clear_subs()
        L.sub(MON_OPEN)  # 登记要记录的项（顺带高速推送）
        time.sleep(0.3)
        self.halt()
        self.wd.arm()

        print(f"\n===== 开环起步扫描：{PULSE_VOLT_S}s/档，动了就倒回原点 =====")
        for v in VOLT_UP:
            self.wd.check()
            x_pre = L.latest(NAME_X)
            L.start_log()      # 每档一段独立记录：缓冲不跨档累积，时间轴也从这里算起
            self.wd.begin_pulse(vlim=80.0)  # 开环只防瞬间大蹿（松，允许静摩擦释放毛刺）
            try:
                L.drive_volt(v)
                self._sleep(PULSE_VOLT_S)
                self.halt()
                self._sleep(HOME_GUARD_S)
            except RuntimeError:
                L.end_log()    # 先冻结本段，再取波形
                self.save_wave("open_up_abort", L.wave(MON_OPEN))
                self.wd.end_pulse()
                raise
            L.end_log()
            self.wd.end_pulse()
            w = L.wave(MON_OPEN)
            self.save_wave("open_up", w)
            lv = [x for _, x in w["左0转速"]][3:]
            rv = [x for _, x in w["右0转速"]][3:]
            lm, _, _, ls = stats(lv)
            rm, _, _, rs = stats(rv)
            dx = L.latest(NAME_X) - x_pre
            moved = "起步" if (lm + rm) / 2 > 3.0 else "不动"
            self.sum_rows.append(["open_up", v, f"{lm:.2f}", f"{rm:.2f}",
                                  f"{ls:.2f}", f"{rs:.2f}", moved, f"dx={dx:+.3f}"])
            print(f"  {v:4.1f}V  L {lm:6.2f}±{ls:4.2f}  R {rm:6.2f}±{rs:4.2f}  {moved}  Δx {dx:+.3f}")
            if abs(dx) > 0.015:  # 真的挪动了才倒回，省时间也减少无谓动作
                self.return_home()
            self._sleep(0.25)

        print("\n===== 开环滚动降压：1.6V起步后连续降档，整体移出≤0.2m 后倒回 =====")
        self.wd.begin_pulse(vlim=80.0, budget=0.20)
        try:
            L.drive_volt(1.6)
            self._sleep(0.7)
            x_scan0 = L.latest(NAME_X)
            for v in VOLT_DOWN:
                self.wd.check()
                L.start_log()      # 每档一段独立记录，波形按档切分
                L.drive_volt(v)
                self._sleep(0.3)
                L.end_log()
                w = L.wave(MON_OPEN)
                self.save_wave("open_down", w)
                lv = [x for _, x in w["左0转速"]][2:]
                rv = [x for _, x in w["右0转速"]][2:]
                lm, lmin, _, ls = stats(lv)
                rm, rmin, _, rs = stats(rv)
                state = "维持" if (lm + rm) / 2 > 3.0 else "快停"
                self.sum_rows.append(["open_down", v, f"{lm:.2f}", f"{rm:.2f}",
                                      f"{ls:.2f}", f"{rs:.2f}", state])
                print(f"  {v:4.1f}V  L {lm:6.2f}±{ls:4.2f}(min {lmin:5.1f})  "
                      f"R {rm:6.2f}±{rs:4.2f}  {state}")
                if abs(L.latest(NAME_X) - x_scan0) > 0.20:
                    print("    [分段] 本段已移出0.2m，先倒回再继续降档")
                    break
        finally:
            self.wd.end_pulse()
        self.halt()
        self._sleep(HOME_GUARD_S)
        if abs(L.latest(NAME_X) - self.wd.ox) > RETURN_TOL_M:
            self.return_home()

    # ---------------- 实验2：闭环增益扫掠（每脉冲后倒车回原点） ----------------
    def apply_settings(self, kf, kp, ki_s, kd_s, pads, remember=True):
        val = {"kf": kf, "kp": kp, "ki": ki_s * 1e-6, "kd": kd_s * 1e6}
        for side in ("L", "R"):
            for k in ("kf", "kp", "ki", "kd"):
                self.L.set_tunable(GAIN_NAME[side][k], val[k])
        if pads is not None:
            st, dy, vmin = pads
            for nm, v in [("左静摩擦", st), ("右静摩擦", st),
                          ("左动摩擦", dy), ("右动摩擦", dy)]:
                self.L.set_tunable(nm, v)
            if vmin is not None:
                for i in range(4):
                    self.L.set_tunable(f"左{i}最小电压", vmin)
                    self.L.set_tunable(f"右{i}最小电压", vmin)
        if remember:
            self.cur_settings = (kf, kp, ki_s, kd_s, pads)

    def phase_closed(self, no_prompt=False, group_filter=None, rotate=False):
        L = self.L
        L.clear_subs()
        L.sub(MON_CLOSED)  # 登记要记录的项
        time.sleep(0.3)
        self.halt()
        groups = [g for g in GAIN_GROUPS if group_filter is None or g[0] in group_filter]
        for gname, kf, kp, ki, kd, pads in groups:
            self.wd.check()
            self.apply_settings(kf, kp, ki, kd, pads)
            if not no_prompt:
                print(f"\n>>> 下一组: {gname}  kf={kf} kp={kp} ki={ki} kd={kd} pads={pads}")
                print("    车应已在原点附近；回车开始（q 结束闭环实验）")
                ans = input("    > ").strip().lower()
                if ans == "q":
                    break
                self.wd.arm(rotate=rotate)
            else:
                mode_txt = "  [原地旋转：正反转交替/脉冲≤270°/飘移0.15m收车]" if rotate else ""
                print(f"\n===== 增益组 {gname}  kf={kf} kp={kp} ki={ki} pads={pads}{mode_txt} =====")
                self.wd.arm(rotate=rotate)
                time.sleep(0.3)
            for pulse_i, tgt in enumerate(SPEED_TARGETS):
                self.wd.check()
                L.start_log()   # 每个脉冲一段独立记录：缓冲不累积，波形天然按脉冲切分
                if rotate:
                    # 原地旋转：正反转交替让线缆来回解扭；脉冲级双保护（飘移+转角）
                    self.wd.begin_pulse(vlim=tgt * 1.8 + 6.0, budget=None,
                                        drift_m=ROTATE_DRIFT_M,
                                        yaw_budget_deg=ROTATE_YAW_BUDGET)
                    s = 1.0 if pulse_i % 2 == 0 else -1.0
                    ltgt, rtgt = s * tgt, -s * tgt
                else:
                    # 超速：目标1.8倍+6pct持续120ms即收车；预算按实测物理速度约2.6cm/s/pct给1.4倍余量
                    budget = min(0.65, 0.12 + tgt * 0.025 * 1.4)
                    self.wd.begin_pulse(vlim=tgt * 1.8 + 6.0, budget=budget)
                    ltgt, rtgt = tgt, tgt
                try:
                    L.spin_target(ltgt, rtgt)
                    self._sleep(PULSE_SPIN_S)
                    L.spin_target(0.0, 0.0)
                    self._sleep(ROTATE_SETTLE_S if rotate else HOME_GUARD_S)
                except RuntimeError:
                    reason = self.wd.tripped or ""
                    L.end_log()    # 先冻结本段，再取波形
                    w = L.wave(MON_CLOSED)
                    self.save_wave(f"closed_{gname}_t{tgt:g}_ABORT", w)
                    self.wd.end_pulse()
                    lv2 = [abs(v) for tt, v in w["左0转速"] if tt >= 0.2]
                    rv2 = [abs(v) for tt, v in w["右0转速"] if tt >= 0.2]
                    lm2, _, _, ls2 = stats(lv2)
                    rm2, _, _, rs2 = stats(rv2)
                    self.sum_rows.append([f"closed_{gname}", f"tgt={tgt:g}",
                                          f"{lm2:.2f}", f"{rm2:.2f}", f"{ls2:.2f}",
                                          f"{rs2:.2f}", f"ABORT:{reason[:14]}"])
                    print(f"  tgt {tgt:3.0f}  [截停] {reason}")
                    if rotate or "单脉冲" not in reason:
                        # 旋转截停=车飘/缠线风险，必须人工看线；直线蹿冲/越围栏同理硬中止
                        raise
                    # 仅直线脉冲位移预算超限：重新挂围栏，倒回原点，继续下一个目标
                    self.wd.tripped = None
                    self.wd.armed = True
                    self.return_home()
                    self._sleep(0.2)
                    continue
                self.wd.end_pulse()
                L.end_log()
                w = L.wave(MON_CLOSED)
                self.save_wave(f"closed_{gname}_t{tgt:g}", w)

                # 指标只取后 50% 稳态窗；旋转时右轮为反转，取绝对值与目标比
                # 时间轴从 start_log 起算，所以稳态窗起点就是 PULSE_SPIN_S 的一半
                tw = PULSE_SPIN_S * 0.5
                win_len = PULSE_SPIN_S * 0.5
                lv = [v for t, v in w["左0转速"] if t >= tw]
                rv = [(abs(v) if rotate else v) for t, v in w["右0转速"] if t >= tw]
                vcmd = [v for t, v in w["左0指令电压"] if t >= tw]
                lm, _, _, lstd = stats(lv)
                rm, _, _, rstd = stats(rv)
                vm, vmin, vmax, _ = stats(vcmd)
                neg = sum(1 for v in vcmd if v < -0.1) / max(1, len(vcmd))
                flips = sum(1 for a, b in zip(vcmd, vcmd[1:])
                            if (a > 0.1 and b < -0.1) or (a < -0.1 and b > 0.1)) / win_len
                err = ((tgt - lm) + (tgt - rm)) / 2
                self.sum_rows.append([f"closed_{gname}", f"tgt={tgt:g}",
                                      f"{lm:.2f}", f"{rm:.2f}", f"{lstd:.2f}",
                                      f"{rstd:.2f}", f"err={err:+.2f}",
                                      f"Vcmd {vm:+.2f}[{vmin:+.2f},{vmax:+.2f}]",
                                      f"neg={neg:.2f}", f"flips/s={flips:.1f}"])
                print(f"  tgt {tgt:4.0f}  L {lm:6.2f}±{lstd:4.2f}  R {rm:6.2f}±{rstd:4.2f}"
                      f"  err {err:+5.2f} | Vcmd {vm:+5.2f}[{vmin:+4.1f},{vmax:+4.1f}]"
                      f"  负压 {neg:.2f}  反打 {flips:.1f}/s")
                if rotate:
                    # 车不位移：刹停等零速即可，脉冲间额外停顿便于人工观察线缆
                    self.halt()
                    self._sleep(0.4)
                else:
                    self.return_home()
                    self._sleep(0.2)
            self.halt()

    def write_summary(self):
        path = os.path.join(self.outdir, f"{self.tag}_summary.csv")
        rows = [dict(zip(SUM_COLS, r)) for r in self.sum_rows]  # 每行按列名落位，短行后面留空
        self.L.save_csv(path, rows, SUM_COLS)
        print(f"[out] 波形文件: {self.outdir}\\*_wave.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("--phase", choices=["open", "closed", "all"], default="all")
    ap.add_argument("--tag", default=time.strftime("%Y%m%d_%H%M"))
    ap.add_argument("--no-prompt", action="store_true", help="组间不暂停（默认暂停以便把车摆回去）")
    ap.add_argument("--groups", default="all", help="闭环只跑指定组，逗号分隔，如 baseline,pure_ff")
    ap.add_argument("--recover", action="store_true",
                    help="回收模式：以≤6pct慢速倒车把车倒回 x≈0，不做任何实验")
    ap.add_argument("--rotate", action="store_true",
                    help="原地旋转模式：左正反右，车不位移，专用于高速段测试(防拉线)")
    args = ap.parse_args()

    outdir = str(Path(__file__).resolve().parent.parent / "pytest" / "probe_data")
    os.makedirs(outdir, exist_ok=True)

    link = BackendLink(args.base)  # 构造即就绪：等连上/等目录/清 abort/拉目录

    wd = Watchdog(link)
    saved_gains = {}
    probe = None
    try:
        link.clear_subs()
        time.sleep(0.2)

        if args.recover:
            # 围栏以世界坐标原点0为中心；车在 +0.3m 处起步、向 0 倒，不触发围栏
            link.sub(MON_CLOSED)
            time.sleep(0.3)
            link.spin_target(0.0, 0.0)
            time.sleep(0.1)
            link.stop_move()
            time.sleep(0.1)
            wd.ox, wd.oy, wd.oyaw = 0.0, link.latest(NAME_Y), link.latest(NAME_YAW)
            wd.tripped, wd.armed = None, True
            wd.begin_pulse(vlim=15.0, budget=None)
            print(f"[recover] 从 x={link.latest(NAME_X):+.3f} 慢速倒回 x≈0 ...")
            link.spin_target(-6.0, -6.0)
            t_end = time.perf_counter() + 8.0
            while time.perf_counter() < t_end:
                wd.check()
                if abs(link.latest(NAME_X)) < RETURN_TOL_M:
                    break
                time.sleep(WD_PERIOD_S)
            link.spin_target(0.0, 0.0)
            time.sleep(0.3)
            link.stop_move()
            time.sleep(0.1)
            link.drive_volt(0.0)
            print(f"[recover] 完成，当前 x={link.latest(NAME_X):+.3f}m")
            return

        missing = [k for k in GAIN_KEYS + PAD_NAMES if k not in link.tun_dir]
        if missing:
            print(f"[warn] 调参表缺少: {missing}（lowpad 组或闭环扫掠可能受限）")
        link.clear_subs()
        time.sleep(0.2)

        probe = Probe(link, outdir, args.tag, wd)
        if args.phase in ("closed", "all"):
            for k in GAIN_KEYS + PAD_NAMES:
                if k in link.tun_dir:
                    saved_gains[k] = link.tun_dir[k][1]

        print("\n安全机制：每个测试脉冲后【先刹停等零速再6pct慢倒】回原点；")
        print("         相对起点 ±1.0m/±0.30m/±30° 硬围栏；单脉冲0.25m预算+超速保护；")
        print("         围栏在脉冲循环里按 30ms 主动 check（不再单开看门狗线程）；")
        print("         上位机停车会置 abort 立即中止；脚本死掉则主控心跳看门狗 2.56s 内自行收车。")
        if not args.no_prompt:
            print("请确认：车放平地、USB 线在车身上留足 0.4m 松量并固定、手在 Ctrl+C 旁。")
            input("准备好后回车开始 > ")

        if args.phase in ("open", "all"):
            probe.phase_open()
        if args.phase in ("closed", "all") and not (probe and probe.abort):
            gf = None if args.groups == "all" else set(args.groups.split(","))
            probe.phase_closed(args.no_prompt, gf, rotate=args.rotate)
        probe.write_summary()

        # 还原调参（闭环实验改过 kf/kp/ki/kd）
        if saved_gains:
            for k, v in saved_gains.items():
                if v is not None:
                    link.set_tunable(k, v)
            print("[restore] 增益已还原为实验前数值（重启主控也会恢复）")
    except KeyboardInterrupt:
        print("\n[中断] 用户中止")
    except Exception as e:
        print(f"\n[异常] {e}")
    finally:
        wd.armed = False  # 先撤防，收尾动作不再被围栏打断
        link.close_safely()  # 目标归零 → 8电机0V → 刹车 → 停记录 → 退订 → 关连接
        wd.close()
        print("[safe] 已收车（目标归零/8电机0V/刹车/退订）")


if __name__ == "__main__":
    main()