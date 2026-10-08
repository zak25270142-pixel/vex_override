"""
FastAPI 入口：
  - WebSocket /ws      与 Vue3 上位机通信（串口字节二进制透传 + 状态 JSON）
  - HTTP   /api/*      脚本友好接口（停车/目录/订阅/命令/日志）

启动：
  cd backend
  uvicorn app.main:app --host 127.0.0.1 --port 8000
串口可用环境变量指定：set V5_PORT=COM5（不设则自动 Ping 探测）
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .hub import Hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("vex.main")

V5_PORT = os.environ.get("V5_PORT")  # 例如 COM3 / /dev/ttyACM0
V5_BAUD = int(os.environ.get("V5_BAUD", "115200"))  # CDC 虚拟口，波特率仅占位
AUTO_CONNECT = os.environ.get("V5_AUTO_CONNECT", "1") not in ("0", "false", "False")

hub = Hub(port=V5_PORT, baud=V5_BAUD)


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.set_loop(asyncio_get_loop())
    logger.info("启动 Hub, port=%s baud=%s auto=%s", V5_PORT or "自动探测", V5_BAUD, AUTO_CONNECT)
    hub.start(auto_connect=AUTO_CONNECT)
    try:
        yield
    finally:
        hub.stop()


def asyncio_get_loop():
    import asyncio

    return asyncio.get_running_loop()


app = FastAPI(title="VEX Backend", version="0.2.0", lifespan=lifespan)
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
    await hub.attach_ui(ws)


# ---------- HTTP 脚本 API ----------


class SubBody(BaseModel):
    index_or_name: Any
    kind: str = Field(default="both", alias="type")  # push | log | both（兼容 type 字段名）
    on: bool = True
    fast: Optional[bool] = None

    model_config = {"populate_by_name": True}


class CmdBody(BaseModel):
    index_or_name: Any
    args: List[Any] = Field(default_factory=list)


class TunableBody(BaseModel):
    index_or_name: Any
    value: Any


class PrintLogBody(BaseModel):
    path: Optional[str] = None


@app.get("/api/status")
def api_status():
    return hub.snapshot()


@app.post("/api/connect")
def api_connect():
    # 前端连接/重连按钮入口：标记想连并立即尝试。
    # 串口探测本身可能阻塞数秒，不等它出结果，交给重连线程反复试；
    # 前端看 WS 状态消息（connecting/reconnecting/connected）感知进度。
    hub.ensure_connected()
    return {"ok": True, "snapshot": hub.snapshot()}


@app.post("/api/disconnect")
def api_disconnect():
    hub._want_connected = False
    hub.bridge.disconnect()
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
    # 停车不等串口：先入队，串口一接上立刻发出去
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
        raise HTTPException(409, str(e))  # abort 中
    except ValueError as e:
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


@app.get("/api/value")
def api_value(index_or_name: List[Any] = Query(default=[])):
    """读当前值（看门狗用）：?index_or_name=x 单项；多个就重复传参一次取多项。
    每项返回 index/name/value/unit/age_s（age_s=距上次推送秒数）。"""
    if not index_or_name:
        raise HTTPException(400, "用法：/api/value?index_or_name=x&index_or_name=y")
    try:
        return hub.read_items(index_or_name)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/values")
def api_values():
    """当前监控值快照（未订阅的项停推后保持最后收到的值）。"""
    out = []
    for idx, val in hub.monitor_values.items():
        item = hub.monitor_dir.get(idx, {})
        out.append(
            {
                "index": idx,
                "name": item.get("name", str(idx)),
                "value": val,
                "unit": item.get("unit", ""),
            }
        )
    return out
