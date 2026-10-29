r"""
FastAPI 入口：
  - WebSocket /ws      与 Vue3 上位机通信（串口字节二进制透传 + 状态 JSON）
  - HTTP   /api/*      脚本友好接口（停车/目录/订阅/命令/日志）

两条通道的分工要分清：
  · 下位机的原始字节只走 WS 二进制帧，前端 FrameParser 自己拆
    （因此前端代码跟"直连串口"时代完全一致，零改动）
  · Backend 自己的状态（连接/掉线）走 WS 的 JSON 文本帧，op="status"
  · /api/* 是 HTTP，只给 Python 脚本用，前端不调

启动（先建虚拟环境并用它跑，见 README）：
  cd backend
  .\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
串口可用环境变量指定：set V5_PORT=COM5（不设则自动 Ping 探测）
"""

import logging 
import os
from contextlib import asynccontextmanager # 异步上下文管理器（自动收车收尾）
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware # CORS 中间件（允许跨域）
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field 

from .dataclass import (
    CalcResult,
    ChartSuggestResult,
    ConnectResult,
    LeaseResult,
    OkResult,
    ValueSnapshot,
)
from .hub import SCRIPT_LEASE_TTL_S, Hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("vex.main")

# 环境变量都只在启动时读一次，运行中改无效
V5_PORT = os.environ.get("V5_PORT")  # 例如 COM3 / /dev/ttyACM0；不设则 Ping 探测
V5_BAUD = int(os.environ.get("V5_BAUD", "115200"))  # CDC 虚拟口，波特率仅占位（USB 全速跑）
AUTO_CONNECT = os.environ.get("V5_AUTO_CONNECT", "1") not in ("0", "false", "False")
# 传输通道：serial=USB 直连（默认），udp=WiFi 下位机（Hi3861 等，对端自动发现）。
# 两者互斥——现阶段只连一个 client，不支持多设备并存。
V5_TRANSPORT = os.environ.get("V5_TRANSPORT", "serial").lower()  # serial | udp
UDP_BIND_HOST = os.environ.get("V5_UDP_HOST", "0.0.0.0")
UDP_BIND_PORT = int(os.environ.get("V5_UDP_PORT", "8888"))
# 演示模式：没有真车/真固件时，用 mock_v5.MockV5Bridge 顶替串口后面的那台 V5。
# 小程序/网页连 WS 的行为与真机完全一致（目录 burst、100Hz 推送、回执、心跳），
# 区别只在数据是本地模拟的
V5_MOCK = os.environ.get("V5_MOCK", "0") in ("1", "true", "True")

# hub 是模块级单例：整个进程只有一个，所有路由共享同一份目录缓存和下行队列。
# 这也是"串口独占"的落点——别在别处再 new 一个 Hub。

_bridge_factory = None
if V5_MOCK:
    from .mock_v5 import MockV5Bridge

    _bridge_factory = MockV5Bridge
elif V5_TRANSPORT == "udp":
    from .udp_bridge import UdpBridge

    def _udp_factory(**kw):
        """Hub 统一传 port/baud，UDP 用不上，剥掉后按监听地址构造。"""
        kw.pop("port", None)
        kw.pop("baud", None)
        return UdpBridge(bind_host=UDP_BIND_HOST, bind_port=UDP_BIND_PORT, **kw)

    _bridge_factory = _udp_factory

hub = Hub(port=V5_PORT, baud=V5_BAUD, bridge_factory=_bridge_factory)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期钩子：启动时注入事件循环并拉起 Hub，关闭时 hub.stop() 收尾。

    app: FastAPI 实例（框架注入，本函数不用它）
    进入时 set_loop（须在 start 前，否则首批串口事件推不出）+ hub.start；退出时 hub.stop。
    """
    # 把主事件循环注入 Hub：串口线程要用 run_coroutine_threadsafe 把 WS 发送
    # 投回这个循环里执行（线程里不能 await）。必须在 start() 之前注入，
    # 否则首批串口事件推不出去。
    hub.set_loop(asyncio_get_loop())
    logger.info(
        "启动 Hub, transport=%s port=%s auto=%s mock=%s",
        "udp@%s:%d" % (UDP_BIND_HOST, UDP_BIND_PORT) if V5_TRANSPORT == "udp" else "serial",
        V5_PORT or "自动探测",
        AUTO_CONNECT,
        V5_MOCK,
    )
    hub.start(auto_connect=AUTO_CONNECT)
    try:
        yield
    finally:
        hub.stop()


def asyncio_get_loop():
    """取当前正在运行的事件循环（只能在 async 上下文里调，所以放在 lifespan 内）。"""
    import asyncio

    return asyncio.get_running_loop()


app = FastAPI(title="VEX Backend", version="0.2.0", lifespan=lifespan)
# CORS 全开：前端可能跑在 vite dev server（不同端口）上，
# 本项目只监听 127.0.0.1 且是本机调试工具，不做更细的限制
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 脚本写路径互斥 ----------
# 一次只允许一个脚本写。受保护的是"会改变车的行为或改写权本身"的接口；
# 读取类（status/column/value/print/calc）不拦——看一眼数据不会打架。
# 前端完全走 WS（只用一个 /api/connect），所以这套不会误伤上位机：
# 确认这一点的成本是一次全仓 grep，改 WS 路线时记得重做。
_GUARDED_WRITE_PATHS = frozenset({
    "/api/sub",                # 改订阅意图 + 登记记录
    "/api/sub/clear_script",
    "/api/cmd",                # 下发动作命令
    "/api/tunable",            # 写调参项
    "/api/log/start",          # 开/收段（缓冲属于持有者）
    "/api/log/end",
    "/api/abort/clear",        # ← 原来的洞：谁都能解别人的停车
})


@app.middleware("http")
async def script_exclusive(request: Request, call_next):
    """写路径准入：X-Script-Token 必须是当前写权持有者，否则 409。

    为什么用中间件而不是逐个路由加依赖：受保护的面要能一眼看全（就是上面那个集合），
    而且不用给七个路由签名都塞一个参数。
    """
    if request.url.path in _GUARDED_WRITE_PATHS:
        try:
            hub.require_script(request.headers.get("x-script-token"))
        except PermissionError as e:
            logger.warning("写路径被拒 %s: %s", request.url.path, e)
            return JSONResponse(status_code=409, content={"detail": str(e)})
    return await call_next(request)


# ---------- WebSocket（Vue3 上位机）----------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WS 端点：把连接整体托管给 hub.attach_ui（收字节透传/发状态 JSON 都在里面）。

    ws: 已建立的 WebSocket 连接
    一个连接 = 一个 WS 客户端，Hub 内部用 id(ws) 区分订阅来源，
    所以开多个页面时各自的订阅不会互相覆盖。本函数在连接关闭前一直不返回。
    """
    await hub.attach_ui(ws)


# ---------- HTTP 脚本 API ----------


class SubBody(BaseModel):
    """/api/sub 请求体：订阅/退订一个监控项（push 档位 / log 登记可叠加）。"""
    # index_or_name 用 Any：HTTP 传进来的可能是 5 / "5" / "左0转速" 三种形态，
    # 具体分发在 hub.resolve_monitor 里用 isinstance 做
    index_or_name: Any
    # 字段名兼容：对外文档写 type，但 Python 里 type 是内置名，故别名到 kind
    kind: str = Field(default="both", alias="type")  # push | log | both
    on: bool = True            # False = 退订 / 取消登记
    fast: bool | None = None   # None 时跟随 on（订阅即高速，退订即关）

    model_config = {"populate_by_name": True}  # 允许请求体里写 kind 或 type 都行


class ChartSuggestBody(BaseModel):
    """/api/chart/suggest 请求体：脚本推荐的一套曲线配置（最多 4 组、每组最多 4 项）。

    groups[i] = 第 i 个通道的追踪项（中文名或 index 均可）；空组表示该通道清空。
    前端整份覆盖应用：给了几组就改几个通道，其余通道一并清空。
    """
    groups: list[list[Any]]
    note: str = ""    # 一句话说明（"这档建议看电流"之类），显示在建议条上
    source: str = ""  # 来源标识（BackendLink 自动填自己的脚本 token）


class CmdBody(BaseModel):
    """/api/cmd 请求体：下发一条动作命令（按命令目录的字段顺序给参数）。"""
    index_or_name: Any
    # 参数按命令目录里的顺序给，个数必须完全一致（Hub.cmd 会严格校验）
    args: list[Any] = Field(default_factory=list)  # 默认空，用于无参命令（如停车）


class TunableBody(BaseModel):
    """/api/tunable 请求体：写一个调参项的值。"""
    index_or_name: Any
    value: Any


class ScriptClaimBody(BaseModel):
    """/api/script/claim 与 /api/script/release 请求体：认领/归还脚本写权。"""
    token: str            # 脚本进程唯一标识（BackendLink 自动生成 uuid）
    steal: bool = False   # 别人占着时是否抢占（release 不看这个字段）
    ttl_s: float = Field(default=SCRIPT_LEASE_TTL_S, gt=0)  # 租约时长，release 不看


class PrintLogBody(BaseModel):
    """/api/log/print 请求体：导出路径 + 要导出的项。

    path：None=默认落后端 pytest/probe_data/log_<时间戳>.json；后缀决定格式
          （json / csv / png / jpg / pdf / svg）。
    names：只导出这几项（中文名或 index，可混）；None=全部已登记项。
          记录缓冲 end_log 后仍在（下次 start_log 才清），可换组合反复导出。
    """
    path: str | None = None
    names: list[Any] | None = None


@app.get("/api/status")
def api_status():
    """后端全貌快照：连接状态、三张表项数、记录状态、端口列表（调试连不上的第一站）。"""
    return hub.snapshot()


@app.post("/api/connect")
def api_connect():
    """前端连接/重连按钮入口：标记想连并异步触发探测，立即返回当前快照。

    返回: ConnectResult(ok, snapshot)
    探测本身有阻塞（逐口开/关，每口 ≤120ms），不等结果——进度靠 WS 状态消息感知；置意愿后掉线会自动重连。
    """
    # 前端连接/重连按钮入口：标记想连并立即尝试。
    # 串口探测本身有阻塞，不等它出结果，交给重连线程反复试；
    # 前端看 WS 状态消息（connecting/connected/disconnected）感知进度。
    # 注意这是"意愿开关"：置 _want_connected=True 后，之后每次掉线都会自动重连。
    hub.ensure_connected()
    return ConnectResult(ok=True, snapshot=hub.snapshot())


@app.post("/api/disconnect")
def api_disconnect():
    """用户主动断开：先关自动重连意愿，再关串口（顺序反了会被立刻重连回去）。"""
    # 先关意愿再断：顺序反了的话，断线回调看到 _want_connected 还是 True，
    # 会立刻触发自动重连，等于断开失败
    hub.set_want_connected(False)
    hub.bridge.disconnect()
    return OkResult()


@app.post("/api/dirs/refresh")
def api_dirs_refresh():
    """清空目录缓存并重新向 V5 拉三张表（表结构变更或怀疑缓存过期时用）。"""
    return hub.refresh_directories()


@app.post("/api/sub/clear_script")
def api_clear_script_subs():
    """脚本结束时调用：去掉 script 侧订阅意图，不影响各 UI 客户端。"""
    hub.clear_script_intents()
    return OkResult()


@app.get("/api/ports")
def api_ports():
    """枚举本机串口设备列表（PortInfo：device/description/vid/pid/serial_number）。"""
    return hub.bridge.list_ports()


@app.get("/api/column")
def api_column(type: str = "monitor"):
    """拉一张目录整表。

    type: monitor(实参表) | tunable(调参表) | cmd(命令表)，默认 monitor
    返回: 该项列表；type 不认识 → 400
    """
    try:
        return hub.get_column(type)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/sub")
def api_sub(body: SubBody):
    """订阅/退订一个监控项。

    body: SubBody（index_or_name + kind=push/log/both + on + fast）
    返回: SubResult；index_or_name 不存在 → 404
    """
    try:
        return hub.sub(body.index_or_name, body.kind, on=body.on, fast=body.fast)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/chart/suggest")
def api_chart_suggest(body: ChartSuggestBody) -> ChartSuggestResult:
    """脚本推荐一套曲线配置：广播给所有上位机，桌面端左下角弹条（5s 内可应用/忽略）。

    body: ChartSuggestBody（groups + note + source）
    返回: ChartSuggestResult（回显解析后的 index，便于脚本自检有没有订错槽）
    越界 → 400；名字不在监控目录 → 404

    不纳入脚本写权互斥：它不改车的行为，只是给操作者递个建议；纯分析脚本（不 claim
    写权）也该能推。防骚扰靠 source 显示在建议条上，能溯源是谁弹的。
    """
    try:
        return hub.suggest_chart(body.groups, note=body.note, source=body.source)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/stop")
def api_stop():
    """紧急停车：最高优先级发停车帧 + 清脚本队列 + 置 abort（立即返回，不等串口）。"""
    # 停车不等串口：先入队（PRI_STOP），串口一接上立刻发出去。
    # 即便此刻断线，停车帧也会反复重试直到发出——不会静默丢失。
    return hub.emergency_stop()


@app.post("/api/abort/clear")
def api_abort_clear():
    """清掉中止旗标：停车后要再跑脚本前必须调，否则 cmd() 直接报 409。"""
    hub.clear_abort()
    return OkResult()


@app.post("/api/script/claim")
def api_script_claim(body: ScriptClaimBody):
    """领脚本写权：一次只有一个脚本握得住，第二个在 connect 阶段就被挡回去。

    abort 闩锁是全局的、没有所有者，两个脚本同时在跑时晚来的那个会把早来脚本
    刚吃到的停车解掉（后者以为自己被停了，实际又开始发命令）。所以与其修 clear_abort，
    不如先把第二个脚本挡在门外——这里就是那道门。

    租约到期自动释放（SCRIPT_LEASE_TTL_S 秒没动静），所以脚本崩了别人也能接管。
    返回: LeaseResult；被别人占着 → 409（held_age_s 是对方已持有的秒数）
    """
    r = hub.claim_script(body.token, body.steal, body.ttl_s)
    if not r.ok:
        raise HTTPException(
            409, f"已有脚本持有写权（{r.held_age_s:.0f}s 前领取，token={body.token} 不是它）。"
                 f"确认对方已退出后重跑，或 steal=true 抢占（抢占会给对方置 abort）")
    return r


@app.post("/api/script/release")
def api_script_release(body: ScriptClaimBody):
    """还写权（脚本收尾时调）。不是自己的就静默无事——这里不需要用报错提醒谁。"""
    hub.release_script(body.token)
    return OkResult()


@app.post("/api/cmd")
def api_cmd(body: CmdBody):
    """下发动作命令（脚本主力写接口）。

    body: CmdBody（index_or_name + args，参数个数须与命令目录一致）
    返回: CmdResult；名字不存在 → 404；abort 中 → 409；参数个数/范围不符 → 400
    """
    try:
        return hub.cmd(body.index_or_name, *body.args)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        # 409 = abort 中（上位机按了停车），脚本要先 clear_abort 才能继续
        raise HTTPException(409, str(e))
    except ValueError as e:
        # 400 = 参数个数不符 或 越界。照着报错改，别绕过校验
        raise HTTPException(400, str(e))


@app.post("/api/tunable")
def api_tunable(body: TunableBody):
    """写一个调参项的值。

    body: TunableBody（index_or_name + value）
    返回: SetTunableResult；名字不存在 → 404；abort 中 → 409
    """
    try:
        return hub.set_tunable_value(body.index_or_name, body.value)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/log/start")
def api_log_start():
    """开始一段记录：清上一段缓冲并以此刻为本段零点，返回已登记项列表。"""
    return hub.start_log()


@app.post("/api/log/end")
def api_log_end():
    """结束记录：停止收样本并返回本段样本总数与丢弃数。"""
    return hub.end_log()


@app.post("/api/log/print")
def api_log_print(body: PrintLogBody = PrintLogBody()):
    """把本段记录导出到后端机器（格式由 path 后缀决定：json/csv/png/jpg/pdf/svg）。

    body: PrintLogBody（path + names；names=None 导出全部已登记项）
    返回: PrintLogResult(ok, path, format, samples, dropped, columns, missing)
    """
    return hub.print_log(body.path, body.names)


class CalcBody(BaseModel):
    """/api/log/calc 请求体：对已记录的项做表达式计算。

    expr 只含监控项名 + 算术运算符；agg 必填且只支持 mean/sum。
    复杂分析（滤波/分位/布尔过滤/派生指标）请 print_log 导 csv 后 pandas 回读。
    """
    expr: str
    agg: str = ""
    window: list[float] | None = None


@app.post("/api/log/calc")
def api_log_calc(body: CalcBody) -> CalcResult:
    """对本段记录做表达式计算并聚合成标量，如 "左0电流 - 左1电流" + agg="mean"。

    agg 只支持 mean/sum；要逐点序列或更复杂的统计，用 /api/log/print 导 csv 回读。
    """
    try:
        return hub.calc(body.expr, body.agg, body.window)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/value")
def api_value(
    index_or_name: list[Any] = Query(default=[]),
    missing_ok: bool = False,
):
    """读当前值（看门狗用）：?index_or_name=x 单项；多个就重复传参一次取多项。
    每项返回 index/name/value/unit/age_s/lag_ms。看门狗主判 lag_ms（落后最新
    监控帧 >300ms 或 None 即旧，对传输延迟免疫）；age_s 是到达新近度，仅诊断。
    missing_ok=true 时跳过还没有数据的项，不整批 404（批量轮询用）。"""
    if not index_or_name:
        raise HTTPException(400, "用法：/api/value?index_or_name=x&index_or_name=y")
    try:
        return hub.read_items(index_or_name, missing_ok=missing_ok)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/values")
def api_values():
    """当前监控值快照（未订阅的项停推后保持最后收到的值）。

    ⚠️ 这里给的是"缓存里的最新值"，不保证新鲜——已经停推的项会一直留着旧值。
    要判新鲜度请用 /api/value，它带 lag_ms（主判据）/age_s（诊断）。
    """
    out = []
    # list() 快照：本路由在线程池里跑，rx 线程 100Hz 在往 monitor_latest 里写（R1）
    for idx, s in list(hub.monitor_latest.items()):
        entry = hub.monitor_dir.get(idx)
        out.append(
            ValueSnapshot(
                index=idx,
                name=entry.item.name if entry else str(idx),
                value=s.value,
                unit=entry.item.unit if entry else "",
            )
        )
    return out
