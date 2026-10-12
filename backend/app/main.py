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

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware # CORS 中间件（允许跨域）
from pydantic import BaseModel, Field 

from .hub import Hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("vex.main")

# 三个环境变量都只在启动时读一次，运行中改无效
V5_PORT = os.environ.get("V5_PORT")  # 例如 COM3 / /dev/ttyACM0；不设则 Ping 探测
V5_BAUD = int(os.environ.get("V5_BAUD", "115200"))  # CDC 虚拟口，波特率仅占位（USB 全速跑）
AUTO_CONNECT = os.environ.get("V5_AUTO_CONNECT", "1") not in ("0", "false", "False")

# hub 是模块级单例：整个进程只有一个，所有路由共享同一份目录缓存和下行队列。
# 这也是"串口独占"的落点——别在别处再 new 一个 Hub。

hub = Hub(port=V5_PORT, baud=V5_BAUD)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 把主事件循环注入 Hub：串口线程要用 run_coroutine_threadsafe 把 WS 发送
    # 投回这个循环里执行（线程里不能 await）。必须在 start() 之前注入，
    # 否则首批串口事件推不出去。
    hub.set_loop(asyncio_get_loop())
    logger.info("启动 Hub, port=%s baud=%s auto=%s", V5_PORT or "自动探测", V5_BAUD, AUTO_CONNECT)
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


# ---------- WebSocket（Vue3 上位机）----------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # 一个连接 = 一个 WS 客户端，Hub 内部用 id(ws) 区分订阅来源，
    # 所以开多个页面时各自的订阅不会互相覆盖
    await hub.attach_ui(ws)


# ---------- HTTP 脚本 API ----------


class SubBody(BaseModel):
    # index_or_name 用 Any：HTTP 传进来的可能是 5 / "5" / "左0转速" 三种形态，
    # 具体分发在 hub.resolve_monitor 里用 isinstance 做
    index_or_name: Any
    # 字段名兼容：对外文档写 type，但 Python 里 type 是内置名，故别名到 kind
    kind: str = Field(default="both", alias="type")  # push | log | both
    on: bool = True            # False = 退订 / 取消登记
    fast: bool | None = None   # None 时跟随 on（订阅即高速，退订即关）

    model_config = {"populate_by_name": True}  # 允许请求体里写 kind 或 type 都行


class CmdBody(BaseModel):
    index_or_name: Any
    # 参数按命令目录里的顺序给，个数必须完全一致（Hub.cmd 会严格校验）
    args: list[Any] = Field(default_factory=list)  # 默认空，用于无参命令（如停车）


class TunableBody(BaseModel):
    index_or_name: Any
    value: Any


class PrintLogBody(BaseModel):
    path: str | None = None


@app.get("/api/status")
def api_status():
    return hub.snapshot()


@app.post("/api/connect")
def api_connect():
    # 前端连接/重连按钮入口：标记想连并立即尝试。
    # 串口探测本身可能阻塞数秒，不等它出结果，交给重连线程反复试；
    # 前端看 WS 状态消息（connecting/connected/disconnected）感知进度。
    # 注意这是"意愿开关"：置 _want_connected=True 后，之后每次掉线都会自动重连。
    hub.ensure_connected()
    return {"ok": True, "snapshot": hub.snapshot()}


@app.post("/api/disconnect")
def api_disconnect():
    # 先关意愿再断：顺序反了的话，断线回调看到 _want_connected 还是 True，
    # 会立刻触发自动重连，等于断开失败
    hub.set_want_connected(False)
    hub.bridge.disconnect()
    return {"ok": True}


@app.post("/api/dirs/refresh")
def api_dirs_refresh():
    """清空目录缓存并重新向 V5 拉三张表（表结构变更或怀疑缓存过期时用）。"""
    return hub.refresh_directories()


@app.post("/api/sub/clear_script")
def api_clear_script_subs():
    """脚本结束时调用：去掉 script 侧订阅意图，不影响各 UI 客户端。"""
    hub.clear_script_intents()
    return {"ok": True}


@app.get("/api/ports")
def api_ports():
    return hub.bridge.list_ports()


@app.get("/api/column")
def api_column(type: str = "monitor"):
    try:
        return hub.get_column(type)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/sub")
def api_sub(body: SubBody):
    try:
        return hub.sub(body.index_or_name, body.kind, on=body.on, fast=body.fast)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/stop")
def api_stop():
    # 停车不等串口：先入队（PRI_STOP），串口一接上立刻发出去。
    # 即便此刻断线，停车帧也会反复重试直到发出——不会静默丢失。
    return hub.emergency_stop()


@app.post("/api/abort/clear")
def api_abort_clear():
    hub.clear_abort()
    return {"ok": True}


@app.post("/api/cmd")
def api_cmd(body: CmdBody):
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
    try:
        return hub.set_tunable_value(body.index_or_name, body.value)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/log/start")
def api_log_start():
    return hub.start_log()


@app.post("/api/log/end")
def api_log_end():
    return hub.end_log()


@app.post("/api/log/print")
def api_log_print(body: PrintLogBody = PrintLogBody()):
    return hub.print_log(body.path)


@app.get("/api/log/stats")
def api_log_stats(
    index_or_name: list[Any] = Query(default=[]),
    thresholds: list[float] = Query(default=[]),
):
    """整段统计，不传 index_or_name 就统计全部已记录的项。

    统计的是"本段（start_log → end_log）的全部样本"，后端不切窗口；
    要只看某一段子区间，脚本自己从 /api/log/window 拿原始点筛。

    thresholds 里每个值额外给出 ratio_ge（|值| >= 该阈值的样本占比）。
    """
    try:
        return hub.log_stats(index_or_name or None, thresholds)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/log/window")
def api_log_window(
    index_or_name: list[Any] = Query(default=[]),
):
    """整段原始样本长表（t_ms/index/name/value），供脚本自己画图、落盘或自行统计。"""
    try:
        return hub.log_rows(index_or_name or None)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/value")
def api_value(
    index_or_name: list[Any] = Query(default=[]),
    missing_ok: bool = False,
):
    """读当前值（看门狗用）：?index_or_name=x 单项；多个就重复传参一次取多项。
    每项返回 index/name/value/unit/age_s（age_s=距上次推送秒数）。
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
    要判新鲜度请用 /api/value，它带 age_s。
    """
    out = []
    for idx, s in hub.monitor_latest.items():
        item = hub.monitor_dir.get(idx)
        out.append(
            {
                "index": idx,
                "name": item.name if item else str(idx),
                "value": s.value,
                "unit": item.unit if item else "",
            }
        )
    return out
