"""
脚本友好封装（HTTP 模式）：测试脚本只跟本类打交道，不碰帧和串口。

核心约定——脚本决定"记什么、何时开始、存哪里"，记录本身由后端干：
    sub() 登记要记的项 → start_log() → 这期间脚本只管发命令 → end_log() → print_log()

典型用法：
    from app.script_api import VexScript

    v = VexScript()
    v.wait_connected()
    v.sub("x")                 # 登记记录（顺带把该项提升为高速推送）
    v.start_log()
    for i in range(50):
        v.cmd("直行", 0.1)
        v.sleep(0.1)           # 可被上位机停车打断
    v.end_log()
    v.print_log()              # JSON + CSV 落到 pytest/
    v.stop()

按区间取统计/波形（时间一律是"相对开始记录的秒"，不用对齐本地时钟）：
    v.stats(["x"], tail=1.0)               # 后端算好 n/mean/abs_mean/... 直接给
    v.wave(["x"], since=0.5, until=1.5)    # 原始长表，自己画图用
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


def _as_list(v) -> list:
    """None → []（requests 遇到空列表就不带这个 query 键了），单值 → [值]。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


# 监控表里电机相关项的命名约定（来自 LCD_menu.cpp）：L2 → 左2实际电压 / 左2转速 / 左2电流
_MOTOR_CN = {"L": "左", "R": "右"}
_MOTOR_SUFFIX = {"vact": "实际电压", "rpm": "转速", "cur": "电流"}


def motor_names(motor: str) -> dict[str, str]:
    """'L2' → {'vact': '左2实际电压', 'rpm': '左2转速', 'cur': '左2电流'}。"""
    cn = _MOTOR_CN[motor[0].upper()]
    n = int(motor[1])
    return {k: f"{cn}{n}{sfx}" for k, sfx in _MOTOR_SUFFIX.items()}


class VexScript:
    def __init__(self, base: str = "http://127.0.0.1:8000", timeout: float = 5.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        # 复用连接：100Hz 轮询要是每拍新建 TCP，端口很快会被 TIME_WAIT 吃光
        self._sess = requests.Session() if requests is not None else None

    # ---------- 内部 ----------

    def _get(self, path: str, **params):
        if self._sess is None:
            raise RuntimeError("请先 pip install requests")
        r = self._sess.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, body: dict | None = None):
        if self._sess is None:
            raise RuntimeError("请先 pip install requests")
        r = self._sess.post(f"{self.base}{path}", json=body or {}, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text}")
        return r.json()

    # ---------- 连接 / 目录 ----------

    def status(self) -> dict:
        return self._get("/api/status")

    def wait_connected(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.status().get("connected"):
                    return True
            except Exception:
                pass  # 后端还没起来时连着试
            time.sleep(0.3)
        return False

    def wait_dirs(self, timeout: float = 5.0) -> bool:
        """等到监控表或命令表到位。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.status()
            if s.get("monitor_count", 0) > 0 or s.get("cmd_count", 0) > 0:
                return True
            time.sleep(0.2)
        return False

    def get_column(self, type: str = "monitor") -> list[dict]:
        """拉目录：type = monitor(实参) | tunable(调参) | cmd(命令)，JSON。"""
        return self._get("/api/column", type=type)

    def values(self) -> list[dict]:
        return self._get("/api/values")

    def read(self, *index_or_name):
        """
        读监控项当前值（看门狗用，index int 或中文名 str 均可）：
          v.read("x")                → 直接返回数值，如 0.123
          v.read(5)                  → 也可传 index
          v.read("x", "y", "yaw")   → {"x":.., "y":.., "yaw":..}（一次请求取多项）
        前提：该项已 sub(..., "push"/"log"/"both")，值才会持续更新。
        要看数据新鲜度用 read_item()（返回里有 age_s）。
        """
        if not index_or_name:
            raise ValueError("read 至少给一个 index 或名字")
        # requests 对 list 值自动展开成重复 query 键：index_or_name=x&index_or_name=y
        items = self._get("/api/value", index_or_name=list(index_or_name))
        if len(items) == 1:
            return items[0]["value"]
        return {it["name"]: it["value"] for it in items}

    def read_item(self, index_or_name) -> dict:
        """读单项完整信息：{index,name,value,unit,age_s}，age_s 为距上次推送秒数。"""
        return self._get("/api/value", index_or_name=index_or_name)[0]

    def read_many(self, keys: list[Any], missing_ok: bool = False) -> list[dict]:
        """一次请求读多项（key 可为 index 或中文名），返回与 read_item 同结构的信息列表。
        比 read() 多带回 index，方便按 index 建索引；missing_ok=True 时跳过还没数据的项，
        不会因为其中一项没推送就整请求 404。"""
        return self._get("/api/value", index_or_name=list(keys), missing_ok=missing_ok)

    # ---------- 停车 / 中止 ----------

    def stop(self) -> dict:
        """紧急停车：最高优先级，清脚本队列并置 abort（也会打断其他在跑的脚本）。"""
        return self._post("/api/stop")

    def clear_abort(self) -> dict:
        """停车后继续跑新脚本前先清 abort。"""
        return self._post("/api/abort/clear")

    # ---------- 订阅 ----------

    def sub(
        self,
        index_or_name,
        type: str = "both",
        *,
        fast: bool | None = None,
    ) -> dict:
        """
        订阅/登记，index_or_name 可传监控 index(int) 或目录中文名(str)：
          type="push" 只改下位机数据推送档位（砍带宽），fast=True 高速 / False 低速
          type="log"  只在后端登记记录，登记时自动把该项提升为高速推送
          type="both" 推送 + 记录都要
        """
        return self._post(
            "/api/sub",
            {"index_or_name": index_or_name, "type": type, "on": True, "fast": fast},
        )

    def unsub(self, index_or_name, type: str = "both") -> dict:
        """退订/取消登记（取消 log 登记不会顺带关下位机推送，以免影响上位机）。
        脚本退出前若曾 sub("push"/"both") 应显式 unsub，否则后端仍保留 script 订阅意图；
        嫌麻烦可在 finally 里调 clear_script_subs() 一键清掉。"""
        return self._post(
            "/api/sub",
            {"index_or_name": index_or_name, "type": type, "on": False},
        )

    def refresh_dirs(self) -> dict:
        """清空后端目录缓存并重新向车拉三张表（怀疑缓存过期/半表时用）。"""
        return self._post("/api/dirs/refresh")

    def clear_script_subs(self) -> dict:
        """脚本进程结束前调用：清掉 script 侧全部订阅意图，不影响各 UI 客户端。"""
        return self._post("/api/sub/clear_script")

    # ---------- 命令 ----------

    def cmd(self, index_or_name, *args) -> dict:
        """
        下发动作命令：index_or_name 为命令字(int)或命令目录中文名，
        参数按目录字段顺序依次给，个数必须与目录完全一致（后端严格校验，少参/多参都报错）。
        abort 状态下调用直接抛错；停车后需先 clear_abort() 再跑。
        """
        return self._post(
            "/api/cmd",
            {"index_or_name": index_or_name, "args": list(args)},
        )

    def set_tunable(self, index_or_name, value) -> dict:
        """写调参项（下位机回显后上位机表格自会更新）。"""
        return self._post(
            "/api/tunable",
            {"index_or_name": index_or_name, "value": value},
        )

    # ---------- 日志 ----------

    def start_log(self) -> dict:
        return self._post("/api/log/start")

    def end_log(self) -> dict:
        return self._post("/api/log/end")

    def print_log(self, path: str | None = None) -> dict:
        """结束后导出：JSON 全量记录 + CSV 长表写到后端 pytest/ 目录，并在本机打印摘要 JSON。"""
        info = self._post("/api/log/print", {"path": path})
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return info

    def mark(self) -> dict:
        """在记录时间轴上打点：{"t": 自开始记录起的秒数, "samples": 已记样本数}。"""
        return self._post("/api/log/mark")

    def stats(
        self,
        names=None,
        *,
        since: float | None = None,
        until: float | None = None,
        tail: float | None = None,
        last_frac: float | None = None,
        thresholds=(),
    ) -> dict:
        """区间统计（后端算好，脚本不用自己攒样本）：names 不给就统计全部已记录的项。
        时间单位是"相对开始记录的秒"；tail=最近 N 秒；last_frac=取窗口的后百分之几；
        thresholds 里每个阈值 th 额外给出 ratio_ge[th]=|值|>=th 的样本占比。"""
        return self._get(
            "/api/log/stats",
            index_or_name=_as_list(names),
            since=since,
            until=until,
            tail=tail,
            last_frac=last_frac,
            thresholds=list(thresholds),
        )

    def wave(
        self,
        names=None,
        *,
        since: float | None = None,
        until: float | None = None,
        tail: float | None = None,
        last_frac: float | None = None,
    ) -> dict:
        """区间内的原始样本长表：{"t_from","t_to","rows":[{"t","index","name","value"}]}。"""
        return self._get(
            "/api/log/window",
            index_or_name=_as_list(names),
            since=since,
            until=until,
            tail=tail,
            last_frac=last_frac,
        )

    # ---------- 时序 ----------

    def sleep(self, seconds: float) -> None:
        """可被停车打断的等待：abort 置位后最多 0.05s 内抛 RuntimeError。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.status().get("abort"):
                raise RuntimeError("已紧急停车 / 脚本被中止")
            time.sleep(min(0.05, end - time.monotonic()))

    def close(self) -> None:
        if self._sess is not None:
            self._sess.close()


# 监控 tag 的 fast 位（bit6）：重名时用它挑高速槽
_TAG_FAST = 0x40


class BackendLink:
    """测试脚本的通信底座：脚本只发命令，数据记录/统计交给后端。

    构造即就绪（自动等连上、等目录、清 abort、拉目录），不用再写一遍前置检查：
        bl = BackendLink()

    惯用写法（脚本决定记什么、何时开始、存哪里；记录期间只管发命令）：
        bl = BackendLink()
        bl.sub(["左0转速", "左0实际电压"])       # 登记要记录的项
        bl.start_log()
        for v in voltages:
            t = bl.set_volt(0, v)["t"]           # 命令响应带回记录时间轴上的时刻
            bl.sleep(0.4)
            st = bl.stats(["左0转速"], since=t, last_frac=0.55)["左0转速"]
        bl.end_log()
        bl.print_log("pytest/motor_v.csv")       # JSON + CSV 落盘
        bl.close()

    看门狗/围栏仍由脚本自己维护：latest() 现查现取（短 TTL 批量，一轮问多项只发一次请求）。
    """

    def __init__(
        self,
        base: str = "http://127.0.0.1:8000",
        connect: bool = True,
        timeout: float = 5.0,
    ) -> None:
        self.v = VexScript(base, timeout=timeout)
        self.mon_dir: dict[str, int] = {}      # 中文名 -> index（重名保留 fast 槽）
        self.tun_dir: dict[str, tuple] = {}    # 中文名 -> (index, 当前值)
        self.motors: dict[str, dict] = {}      # 'L2' -> {'vact'/'rpm'/'cur': 中文名}，fetch_dirs 填
        self._mon_fast: dict[str, bool] = {}
        self._watch: set[int] = set()          # 最近问过值的项，一次请求批量取回
        self._vals: dict[int, dict] = {}
        self._vals_at = 0.0
        self._ttl = 0.02                       # 当前值缓存 20ms：一轮问多项只发一次请求
        self._abort_val = False
        self._abort_at = 0.0
        self._abort_ttl = 0.1                  # abort 状态 100ms 查一次（与原实现同节奏）
        if connect:
            self.connect()

    def __enter__(self) -> BackendLink:
        """用 with 写法就不用每个脚本再抄一遍 try/finally 收车：
            with BackendLink() as bl:
                bl.sub(...); bl.start_log(); ...
        退出时 __exit__ 自动收车收尾（目标归零/8电机0V/刹车/停记录/退订/断连接）。"""
        return self

    def __exit__(self, *exc_info) -> None:
        self.close_safely()

    # ---------- 连接 / 目录 ----------

    def connect(self, wait_connected: float = 10.0, wait_dirs: float = 5.0) -> None:
        """等后端连上下位机 + 目录到位 + 清 abort，一步到位。"""
        if not self.v.wait_connected(wait_connected):
            raise RuntimeError(f"后端没连上下位机：{self.v.status()}")
        if not self.v.wait_dirs(wait_dirs):
            print("[warn] 目录还没到位，fetch_dirs 可能拿不全项")
        self.v.clear_abort()
        self.fetch_dirs()

    def fetch_dirs(self) -> None:
        """目录走后端缓存；同名条目保留 fast 槽，避免订到低速陈旧值。"""
        self.mon_dir.clear()
        self._mon_fast.clear()
        for it in self.v.get_column("monitor"):
            name = it["name"]
            fast = bool(it["tag"] & _TAG_FAST)
            if name not in self.mon_dir or (fast and not self._mon_fast.get(name)):
                self.mon_dir[name] = it["index"]
                self._mon_fast[name] = fast
        self.tun_dir = {
            it["name"]: (it["index"], it.get("value"))
            for it in self.v.get_column("tunable")
        }
        # 电机三项的中文名顺手建好：脚本直接写 bl.motors["L2"]["rpm"]，不用记命名约定
        self.motors = {m: motor_names(m) for m in (f"{s}{i}" for s in "LR" for i in range(4))}
        print(f"[dir] 监控项 {len(self.mon_dir)}，调参项 {len(self.tun_dir)}")

    def resolve(self, names) -> list[int]:
        """中文名或 index 统一解析成 index；名字不认识就跳过并提醒。"""
        out = []
        for n in _as_list(names):
            idx = n if isinstance(n, int) else self.mon_dir.get(n)
            if idx is None:
                print(f"[warn] 监控项不存在: {n}")
                continue
            out.append(idx)
        return out

    def _idx(self, name_or_idx) -> int | None:
        return name_or_idx if isinstance(name_or_idx, int) else self.mon_dir.get(name_or_idx)

    # ---------- 订阅 / 记录登记 ----------

    def sub(self, names, type: str = "both", fast: bool | None = None) -> None:
        """登记要订阅/记录的项（中文名或 index 均可，一律按 index 下发，重名不会有歧义）。
        type="push" 只改下位机推送档位（砍带宽）；"log" 只在后端登记记录；"both" 两者都做。"""
        for idx in self.resolve(names):
            self.v.sub(idx, type, fast=fast)
            self._watch.add(idx)

    def unsub(self, names, type: str = "both") -> None:
        """退订/取消登记。取消 log 登记不会顺带关掉下位机推送，不影响上位机。"""
        for idx in self.resolve(names):
            self.v.unsub(idx, type)

    def clear_subs(self) -> None:
        """清掉 script 侧全部订阅意图，不动上位机正在看的那些项。"""
        self.v.clear_script_subs()
        self._watch.clear()
        self._vals.clear()

    # ---------- 记录（脚本决定记什么/何时开始/存哪里，记录本身由后端干） ----------

    def start_log(self) -> dict:
        """开始记录：以此刻为时间轴零点，记哪些项由 sub(..., 'log') 先登记。"""
        return self.v.start_log()

    def end_log(self) -> dict:
        return self.v.end_log()

    def print_log(self, path: str | None = None) -> dict:
        """导出 JSON + CSV（默认落后端 pytest/ 目录），并打印摘要。"""
        return self.v.print_log(path)

    def mark(self) -> float:
        """在记录时间轴上打点，返回自开始记录起的秒数。"""
        return self.v.mark()["t"]

    def stats(self, names=None, **win) -> dict:
        """区间统计，返回 {传入的名字或 index: {n, mean, abs_mean, min, max, abs_max, std, ratio_ge}}。
        窗口参数见 VexScript.stats（since/until/tail/last_frac/thresholds）。"""
        items = self.v.stats(names, **win)["items"]
        if names is None:
            return {it["name"]: it for it in items}
        return dict(zip(_as_list(names), items))

    def wave(self, names=None, **win) -> dict:
        """区间原始波形：{名字: [(相对开始记录的秒, 值), ...]}。
        请求了却没数据的项也会给空列表，省得脚本到处判 KeyError。"""
        out: dict[str, list] = {n: [] for n in _as_list(names) if isinstance(n, str)}
        for r in self.v.wave(names, **win)["rows"]:
            out.setdefault(r["name"], []).append((r["t"] / 1000.0, r["value"]))
        return out

    def save_csv(self, path: str, rows: list[dict], columns: list[str] | None = None,
                 quiet: bool = False) -> str:
        """把 rows（dict 列表）写 CSV；不给 columns 就按第一行的键序，utf-8-sig 便于 Excel。"""
        keys = list(columns or (rows[0].keys() if rows else []))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(",".join(keys) + "\n")
            for r in rows:
                cells = []
                for k in keys:
                    v = r.get(k)
                    cells.append("" if v is None else (f"{v:.6f}" if isinstance(v, float) else str(v)))
                f.write(",".join(cells) + "\n")
        if not quiet:
            print(f"[out] 已写入 {path}（{len(rows)} 行）")
        return path

    def sweep(self, values, setter, names, hold, last_frac=None, thresholds=()):
        """扫描原语：逐个值调 setter(v) → 等 hold 秒 → 取这一档的区间统计，逐档 yield
        {"value": v, "t": 记录轴时刻, "stats": {中文名: {n, mean, abs_mean, ...}}}。

        setter 直接写 bl.set_volt / bl.spin_target 之类（命令响应里的 t 会自动拿来打点）。
        于是"逐档扫描 + 取后窗统计"只剩一个 for：
            for row in bl.sweep(volts, lambda v: bl.set_volt(0, v), [rpm], 0.4, last_frac=0.7):
                print(row["value"], row["stats"][rpm]["abs_mean"])
        """
        for v in values:
            r = setter(v)
            t = r.get("t") if isinstance(r, dict) else r
            self.sleep(hold)
            yield {
                "value": v,
                "t": t,
                "stats": self.stats(names, since=t, last_frac=last_frac, thresholds=thresholds),
            }

    # ---------- 读当前值（看门狗/围栏用，脚本自己判安全） ----------

    def latest(self, name_or_idx, default: float = 0.0) -> float:
        """取某项当前值。短 TTL 批量：一轮问多项只发一次请求。"""
        idx = self._idx(name_or_idx)
        if idx is None:
            return default
        self._watch.add(idx)
        if idx not in self._vals or time.monotonic() - self._vals_at >= self._ttl:
            self._refresh_vals()
        it = self._vals.get(idx)
        return default if it is None else it["value"]

    def read(self, *names):
        """一次读多项当前值：读单项直接返回数值，多项返回 {名字: 数值}。"""
        return self.v.read(*names)

    def _refresh_vals(self) -> None:
        """一次请求把最近问过的项全取回（后端对还没数据的项跳过，不会整批 404）。"""
        self._vals_at = time.monotonic()
        idxs = sorted(self._watch)
        if not idxs:
            return
        try:
            for it in self.v.read_many(idxs, missing_ok=True):
                self._vals[it["index"]] = it
        except Exception:
            pass  # 后端暂时没连上/没数据：保留上次缓存，下一拍再试

    # ---------- 写 ----------

    def cmd(self, name, *args) -> dict:
        return self.v.cmd(name, *args)

    def stop_move(self) -> dict:
        """0x80 停止运动：只发停车帧，不置 abort（不同于上位机停车按钮）。"""
        return self.cmd("停止运动")

    def set_volt(self, motor_id: int, volts: float) -> dict:
        return self.cmd("设置电机电压", int(motor_id), float(volts))

    def drive_volt(self, volts: float) -> None:
        for mid in range(8):
            self.set_volt(mid, volts)

    def spin_target(self, left: float, right: float) -> dict:
        return self.cmd("设置底盘目标速度", float(left), float(right))

    def set_tunable(self, name: str, value: float) -> dict:
        return self.v.set_tunable(name, value)

    # ---------- 时序 / 中止 ----------

    def sleep(self, seconds: float) -> None:
        """可被上位机停车打断的等待。"""
        end = time.monotonic() + seconds
        while True:
            self.check_abort()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))

    def check_abort(self) -> None:
        """查后端 abort（上位机停车置位）；约 0.1s 查一次，与原实现同节奏。"""
        now = time.monotonic()
        if now - self._abort_at >= self._abort_ttl:
            self._abort_at = now
            try:
                self._abort_val = bool(self.v.status().get("abort"))
            except Exception:
                pass
        if self._abort_val:
            raise RuntimeError("已紧急停车 / 脚本被中止（后端 abort 置位）")

    def close_safely(self) -> None:
        """收车收尾：目标归零 → 8 电机 0V → 0x80 刹车 → 结束记录 → 退订 → 关连接。
        每步单独 try：上位机刚按了停车（后端 abort）时也保证把连接和订阅清干净。"""
        try:
            self.spin_target(0.0, 0.0)
        except Exception:
            pass
        for mid in range(8):
            try:
                self.set_volt(mid, 0.0)
            except Exception:
                pass
        try:
            self.stop_move()
        except Exception:
            pass
        try:
            self.end_log()
        except Exception:
            pass
        try:
            self.clear_subs()
        except Exception:
            pass
        self.close()

    def close(self) -> None:
        self.v.close()
