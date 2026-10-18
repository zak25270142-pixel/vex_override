"""
脚本友好封装（HTTP 模式）：测试脚本只跟本类打交道，不碰帧和串口。

本文件有两个类，别选错：
    VexScript    薄的 HTTP 封装，一个方法对应一个 /api/* 接口。
                 需要精确控制、或要调本文件没包出来的接口时用。
    BackendLink  标定脚本的主力入口（推荐）。构造即就绪，
                 带目录名反查、看门狗、with 自动收车。分段记录惯例：每段 start_log → 测试 → end_log 一条独立曲线。
                 内部持有一个 VexScript（self.v）。

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
    v.print_log("pytest/run.json")   # 后缀决定格式：json / csv / png / jpg（无后缀默认 json）
    v.stop()

取整段统计/波形（一段 = 一次 start_log → end_log，时间是"相对本段零点的秒"）：
    v.stats(["x"])                      # 后端算好 n/mean/abs_mean/... 直接给（整段）
    v.wave(["x"])                       # 整段原始长表，自己画图或自行筛区间
"""

import json
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


def _as_list(v) -> list:
    """None → []（requests 遇到空列表就不带这个 query 键了），单值 → [值]。

    空列表要显式转成 None 的原因：requests 对 [] 的处理是不带这个参数，
    后端就收不到 index_or_name，语义变成"查全部"而不是"什么都不查"——
    这两者在 stats() 里差别很大。
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


class VexScript:
    def __init__(self, base: str = "http://127.0.0.1:8000", timeout: float = 5.0) -> None:
        """base 指向 Backend（默认本机 8000）；脚本和 Backend 通常同机，延迟 <1ms。

        requests 是可选依赖：没装也能 import 本模块，只是所有调用会抛错。
        这样 IDE 补全和静态检查不会因为缺个库就全线飘红。
        """
        self.base = base.rstrip("/")
        self.timeout = timeout
        # 复用连接：100Hz 轮询要是每拍新建 TCP，端口很快会被 TIME_WAIT 吃光
        self._sess = requests.Session() if requests is not None else None

    # ---------- 内部 ----------

    def _get(self, path: str, **params):
        """发 GET 取 JSON；HTTP >=400 抛 RuntimeError（带路径与响应体，便于定位）。"""
        if self._sess is None:
            raise RuntimeError("请先 pip install requests")
        r = self._sess.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, body: dict | None = None):
        """发 POST（body 转 JSON）取响应 JSON；HTTP >=400 抛 RuntimeError。"""
        if self._sess is None:
            raise RuntimeError("请先 pip install requests")
        r = self._sess.post(f"{self.base}{path}", json=body or {}, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text}")
        return r.json()

    # ---------- 连接 / 目录 ----------

    def status(self) -> dict:
        """后端状态快照（连接/表项数/记录状态等），与 /api/status 同结构。"""
        return self._get("/api/status")

    def wait_connected(self, timeout: float = 10.0) -> bool:
        """轮询等后端连上下位机。

        给 10s 是因为串口探测要逐口 Ping，冷启动可能要好几秒。
        返回 False 不等于失败——只是这段时间内没连上，可以自己再等等。
        """
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
        """全部监控项的当前值快照列表（结构同 ValueSnapshot；不含新鲜度）。"""
        return self._get("/api/values")

    def read(self, *index_or_name):
        """
        读监控项当前值（看门狗用，index int 或中文名 str 均可）：
          v.read("x")                → 直接返回数值，如 0.123
          v.read(5)                  → 也可传 index
          v.read("x", "y", "yaw")   → {"x":.., "y":.., "yaw":..}（一次请求取多项）
        前提：该项已 sub(..., "push"/"log"/"both")，值才会持续更新。
        ⚠️ 读到的是后端缓存的最新值，推送断了以后会一直返回旧值。
        做看门狗时务必用 read_item() 看 age_s，否则"值正常"可能是假象。
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
        """紧急停车：最高优先级，清脚本队列并置 abort（也会打断其他在跑的脚本）。

        与 BackendLink.stop_move() 的区别：
            stop()       上位机语义，置 abort，中断所有脚本
            stop_move()  只发 0x80 停车帧，不置 abort（脚本自己还要继续跑时用）
        """
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

        ⚠️ 注意 fast 默认是 None（跟随 on），而 BackendLink.sub 默认 type="both"。
        在 hub 里只有 type=="log" 才做"非高速自动提升"，所以走 both 时
        日志采样率会跟着下位机原有档位（低速项约 80ms）。
        要 100Hz 数据请显式 sub(..., fast=True)。
        """
        return self._post(
            "/api/sub",
            {"index_or_name": index_or_name, "type": type, "on": True, "fast": fast},
        )

    def unsub(self, index_or_name, type: str = "both") -> dict:
        """退订/取消登记（取消 log 登记不会顺带关下位机推送，以免影响上位机）。

        为什么"取消 log 不动推送"：脚本跑完不该把上位机正在看的曲线也关掉。
        这也是订阅按来源（script / ui:xxx）分开记的价值。

        脚本退出前若曾 sub("push"/"both") 应显式 unsub，否则后端仍保留 script 订阅意图；
        嫌麻烦可在 finally 里调 clear_script_subs() 一键清掉。
        """
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
        """结束后导出记录并在本机打印摘要 JSON。格式由 path 后缀决定：
        .json（无后缀默认）/ .csv / .png|.jpg（作图）；
        path 里带子目录会自动创建；不传则默认落后端 pytest/log_<时间戳>.json。"""
        info = self._post("/api/log/print", {"path": path})
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return info

    def stats(self, names=None, thresholds=()) -> dict:
        """整段统计（后端算好，脚本不用自己攒样本）：names 不给就统计全部已记录的项。
        thresholds 里每个阈值 th 额外给出 ratio_ge[th]=|值|>=th 的样本占比。
        只看子区间的话，用 wave() 拿原始点自己筛。"""
        return self._get(
            "/api/log/stats",
            index_or_name=_as_list(names),
            thresholds=list(thresholds),
        )

    def wave(self, names=None) -> dict:
        """整段原始样本长表：{"rows":[{"t","index","name","value"}]}。"""
        return self._get(
            "/api/log/window",
            index_or_name=_as_list(names),
        )

    # ---------- 时序 ----------

    def sleep(self, seconds: float) -> None:
        """可被停车打断的等待：abort 置位后最多 0.05s 内抛 RuntimeError。

        不要用 time.sleep 替代它——停车时脚本会继续往下跑，很危险。
        """
        # 每拍都查一次 status：一次 HTTP 往返 <1ms（同机），50ms 一拍开销可忽略。
        # 用 VexScript.sleep 比 BackendLink.sleep 反应更快，但 HTTP 请求更频繁。
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.status().get("abort"):
                raise RuntimeError("已紧急停车 / 脚本被中止")
            time.sleep(min(0.05, end - time.monotonic()))

    def close(self) -> None:
        """关闭复用的 HTTP 会话（脚本结束时调，释放连接）。"""
        if self._sess is not None:
            self._sess.close()


# 监控 tag 的 fast 位（bit6 = 0x40）：目录里可能有同名项（比如低速/高速两个版本），
# fetch_dirs 用它挑高速那个槽，避免订到 80ms 才更新一次的陈旧值。
_TAG_FAST = 0x40


class BackendLink:
    """测试脚本的通信底座：脚本只发命令，数据记录/统计交给后端。

    构造即就绪（自动等连上、等目录、清 abort、拉目录），不用再写一遍前置检查：
        bl = BackendLink()

    惯用写法（脚本决定记什么；每段测量自己收一段，段起点就是本段零点）：
        bl = BackendLink()
        bl.sub(["左0转速", "左0实际电压"])       # 登记要记录的项
        for v in voltages:
            bl.start_log()                       # 每档一段独立记录
            bl.set_volt(0, v)
            bl.sleep(0.4)
            bl.end_log()
            st = bl.stats(["左0转速"])["左0转速"]              # 本段整段统计
            pts = bl.wave(["左0转速"])["左0转速"]              # 本段原始点（要筛子区间就自己筛）
        bl.print_log("pytest/motor_v.csv")       # 后缀决定格式：csv/json/png/jpg
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
        self._mon_fast: dict[str, bool] = {}
        # 看门狗用的本地缓存：一轮里问 x/y/yaw 三项，只发一次 HTTP 请求
        self._watch: set[int] = set()          # 最近问过值的项（批量取回的名单）
        self._vals: dict[int, dict] = {}       # index -> {index,name,value,unit,age_s}
        self._vals_at = 0.0                    # 上次批量刷新的时刻
        self._ttl = 0.02                       # 当前值缓存 20ms：一轮问多项只发一次请求
        # abort 只 100ms 查一次：停车是罕见事件，但 sleep(0.05) 一拍就要判一次，
        # 每拍都发 HTTP 太浪费，所以缓存起来按 TTL 刷新
        self._abort_val = False
        self._abort_at = 0.0
        self._abort_ttl = 0.1
        if connect:
            self.connect()

    def __enter__(self) -> "BackendLink":
        """用 with 写法就不用每个脚本再抄一遍 try/finally 收车：
            with BackendLink() as bl:
                bl.sub(...); bl.start_log(); ...
        退出时 __exit__ 自动收车收尾（目标归零/8电机0V/刹车/停记录/退订/断连接）。"""
        return self

    def __exit__(self, *exc_info) -> None:
        """with 退出时调 close_safely()（自动收车收尾），异常也照收。"""
        self.close_safely()

    # ---------- 连接 / 目录 ----------

    def connect(self, wait_connected: float = 10.0, wait_dirs: float = 5.0) -> None:
        """等后端连上下位机 + 目录到位 + 清 abort，一步到位。

        清 abort 放在这里：上一次脚本可能是在停车状态下异常退出的，
        不清的话新脚本第一次 cmd() 就抛错。
        """
        if not self.v.wait_connected(wait_connected):
            raise RuntimeError(f"后端没连上下位机：{self.v.status()}")
        if not self.v.wait_dirs(wait_dirs):
            print("[warn] 目录还没到位，fetch_dirs 可能拿不全项")
        self.v.clear_abort()
        self.fetch_dirs()

    def fetch_dirs(self) -> None:
        """拉三张目录建名字反查表；同名条目保留 fast 槽，避免订到低速陈旧值。

        目录走的是后端缓存（不是每次都打扰下位机），所以这个函数很便宜，
        脚本里想刷新就调，不用担心开销。
        """
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
        # 电机别名（L2 → 左2转速 之类）不在这里建：后端不认识任何命名约定，
        # 只认实车目录里的中文名或 index。需要别名的脚本在脚本侧自己维护。
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
        """名字或 index → index；名字不在目录里返回 None。"""
        return name_or_idx if isinstance(name_or_idx, int) else self.mon_dir.get(name_or_idx)

    # ---------- 订阅 / 记录登记 ----------

    def sub(self, names, type: str = "both", fast: bool | None = None) -> None:
        """登记要订阅/记录的项（中文名或 index 均可，一律按 index 下发，重名不会有歧义）。

        按 index 下发是关键：目录里可能有重名项，用名字下发会订错槽位。

        type="push" 只改下位机推送档位（砍带宽）；"log" 只在后端登记记录；"both" 两者都做。
        """
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
        """结束本段记录（停止收集样本），返回本段样本数与丢弃数。"""
        return self.v.end_log()

    def print_log(self, path: str | None = None) -> dict:
        """导出记录到后端机器（脚本和后端通常同机），并打印摘要。

        格式由 path 后缀决定：.json（无后缀默认）/ .csv / .png|.jpg（作图）。
        path 带子目录会自动创建；不传则默认 pytest/log_<时间戳>.json。
        """
        return self.v.print_log(path)

    def stats(self, names=None, thresholds=()) -> dict:
        """整段统计，返回 {传入的名字或 index: {n, mean, abs_mean, min, max, abs_max, std, ratio_ge}}。
        thresholds 见 VexScript.stats。"""
        items = self.v.stats(names, thresholds)["items"]
        if names is None:
            return {it["name"]: it for it in items}
        return dict(zip(_as_list(names), items))

    def wave(self, names=None) -> dict:
        """整段原始波形：{名字: [(相对本段零点的秒, 值), ...]}。
        请求了却没数据的项也会给空列表，省得脚本到处判 KeyError。

        与 stats() 的分工：stats 给整段统计量；wave 给原始点（画图、或自己筛子区间）。
        """
        out: dict[str, list] = {n: [] for n in _as_list(names) if isinstance(n, str)}
        for r in self.v.wave(names)["rows"]:
            out.setdefault(r["name"], []).append((r["t"] / 1000.0, r["value"]))
        return out

    def save_csv(self, path: str, rows: list[dict], columns: list[str] | None = None,
                 quiet: bool = False) -> str:
        """把 rows（dict 列表）写 CSV；不给 columns 就按第一行的键序，utf-8-sig 便于 Excel。

        utf-8-sig 带 BOM：不加的话 Excel 打开中文列会乱码。
        浮点数固定 6 位小数，避免科学计数法把小值搞得看不清。
        """
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

    # ---------- 读当前值（看门狗/围栏用，脚本自己判安全） ----------

    def latest(self, name_or_idx, default: float = 0.0) -> float:
        """取某项当前值（看门狗/围栏用）。短 TTL 批量：一轮问多项只发一次请求。

        ⚠️ default 默认是 0.0 —— 取不到值时返回 0 而不是抛错。
        做安全围栏时要小心：传感器断线时 latest 会返回 0，
        看起来像"速度为零、很安全"，实际是"没数据"。
        真要判安全，用 read() 拿完整信息看 age_s。
        """
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
            # missing_ok=True：还没推送过的项跳过，不因为其中一项没数据就整批 404
            for it in self.v.read_many(idxs, missing_ok=True):
                self._vals[it["index"]] = it
        except Exception:
            # 后端暂时没连上：保留上次缓存继续用，下一拍再试。
            # 不抛错是为了让看门狗在网络抖动时不要直接把脚本中断掉
            pass

    # ---------- 写 ----------

    def cmd(self, name, *args) -> dict:
        """按命令名或命令字下发动作命令，args 个数须与命令目录一致（后端严格校验）。"""
        return self.v.cmd(name, *args)

    def stop_move(self) -> dict:
        """0x80 停止运动：只发停车帧，不置 abort（不同于上位机停车按钮）。

        用在"脚本自己要收车但还想继续跑后续步骤"的场景，
        比如扫参每一档之间停一下。
        """
        return self.cmd("停止运动")

    def set_volt(self, motor_id: int, volts: float) -> dict:
        """设单个电机电压：motor_id 电机编号(0~7)，volts 目标电压(单位 V)。"""
        return self.cmd("设置电机电压", int(motor_id), float(volts))

    def drive_volt(self, volts: float) -> None:
        """6 个电机全给同一个电压（开环测试用，绕开速度环直接看电机响应）。

        标速度环前通常先跑这个，摸清"电压→转速"的静态映射和死区。
        """
        for mid in range(6):
            self.set_volt(mid, volts)

    def spin_target(self, left: float, right: float) -> dict:
        """下发"设置底盘目标速度"：left/right 为左、右两侧的目标速度（走速度环）。"""
        return self.cmd("设置底盘目标速度", float(left), float(right))

    def set_tunable(self, name: str, value: float) -> dict:
        """写调参项（转发 VexScript.set_tunable）；下位机回显后上位机表格自会更新。"""
        return self.v.set_tunable(name, value)

    # ---------- 时序 / 中止 ----------

    def sleep(self, seconds: float) -> None:
        """可被上位机停车打断的等待。

        不要用 time.sleep 替代——停车时脚本会继续往下跑。
        abort 状态按 _abort_ttl(100ms) 缓存查询，比 VexScript.sleep 省 HTTP，
        代价是最坏 100ms 才响应停车（0.1s 粒度与原实现一致）。
        """
        end = time.monotonic() + seconds
        while True:
            self.check_abort()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))

    def check_abort(self) -> None:
        """查后端 abort（上位机停车置位）；约 0.1s 查一次，与原实现同节奏。

        缓存查询期间如果 HTTP 失败（后端重启），保留上一次的值继续判——
        宁可多跑 100ms 也不要因为一次网络抖动就把正常脚本中断掉。
        """
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
        """收车收尾：目标归零 → 6 电机 0V → 0x80 刹车 → 结束记录 → 退订 → 关连接。

        顺序有讲究：
          1. 先把目标转速归零（让速度环平滑收，不要急刹）
          2. 再直接把 6 个电机电压写 0（绕开速度环，确保真的不动）
          3. 最后发 0x80 刹车（动作层状态机归位）
        每步单独 try：上位机刚按了停车（后端 abort）时也保证把连接和订阅清干净。
        """
        try:
            self.spin_target(0.0, 0.0)
        except Exception:
            pass
        for mid in range(6):
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
        """关闭底层 HTTP 会话；只断连接、不做收车（收车请用 close_safely）。"""
        self.v.close()
