"""
状态中枢：
  - 串口上行原始字节 → 原样广播给 WebSocket 上位机（前端 FrameParser 零改动）
  - 同一份字节再喂 Python FrameParser → 目录缓存 / 监控快照 / 日志（供脚本 API）
  - 上位机与脚本的下行帧统一走优先级队列：STOP(0) < 上位机(10) < 脚本(20)
  - 停车帧会清空脚本队列并置 abort，正在跑的脚本在 cmd()/sleep() 处立即中断
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from fastapi import WebSocket

from .protocol import (
    CmdGet,
    CmdPost,
    CmdSpec,
    build_frame,
    cmd_spec,
    command_send,
    ping,
    request_command,
    request_monitor,
    request_tunable,
    scan_dir_frames,
    set_tunable,
    subscribe,
    tag_fast,
    tag_subscribed,
)
from .serial_bridge import SerialBridge

logger = logging.getLogger("vex.hub")

# 命令优先级：数字越小越优先
PRI_STOP = 0 # 停车帧优先级
PRI_UI = 10 # 上位机优先级
PRI_SCRIPT = 20 # 脚本优先级

# 日志缓冲上限：高速订阅 100Hz×多项时防止脚本忘 end_log 把内存吃光，超出后丢新样本
MAX_LOG_SAMPLES = 500_000

# 默认 pytest 输出目录（相对 backend/）
DEFAULT_PYTEST_DIR = Path(__file__).resolve().parent.parent / "pytest"


class Hub:
    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = 115200,
        pytest_dir: Optional[Path] = None,
    ) -> None:
        self.bridge = SerialBridge(
            port=port,
            baud=baud,
            on_event=self._on_serial_event,
            on_status=self._on_serial_status,
            on_raw=self._on_serial_raw,
        )
        self.pytest_dir = Path(pytest_dir) if pytest_dir else DEFAULT_PYTEST_DIR
        self.pytest_dir.mkdir(parents=True, exist_ok=True)

        # 三张目录缓存 index → item dict
        self.tunable_dir: Dict[int, dict] = {}
        self.monitor_dir: Dict[int, dict] = {}
        self.cmd_dir: Dict[int, dict] = {}
        # name → index 反查（脚本用中文名寻址）
        self._name_to_mon: Dict[str, int] = {}
        self._name_to_tun: Dict[str, int] = {}
        self._name_to_cmd: Dict[str, int] = {}

        # 目录原始帧缓存：cmd → 完整帧列表（字节级一致，前端 FrameParser 零改动）
        self._dir_cache: Dict[int, List[bytes]] = {}
        # 粘包尾部：目录 burst 可能跨 chunk，保留未完整帧尾部
        self._dir_buf = bytearray()

        # 最新监控值快照（/api/values 用）
        self.monitor_values: Dict[int, Any] = {}
        # 每项最近一次收到推送的时刻（看门狗判断数据是否新鲜，不存历史）
        self.monitor_time: Dict[int, float] = {}
        # 下位机侧推送订阅意图 index → (sub, fast)
        self.fw_sub: Dict[int, tuple] = {}
        # 后端日志登记项（脚本 sub(..., 'log'/'both') 登记）
        self.log_reg: Set[int] = set()

        # 订阅聚合：index → {source: (sub, fast)}，source = ui:conn_id 或 script
        self._sub_intent: Dict[int, Dict[str, tuple]] = {}
        self._sub_lock = threading.Lock()

        # 日志记录
        self.logging = False
        self.log_buffer: List[dict] = []
        self.log_dropped = 0
        self._log_t0 = 0.0

        # WebSocket 上位机
        self._ws_clients: Set[WebSocket] = set()
        self._ws_lock = threading.Lock()

        # 下行帧优先级队列 (priority, seq, frame)
        self._tx_q: Deque[Tuple[int, int, bytes]] = deque()
        self._tx_seq = 0
        self._tx_cv = threading.Condition()
        self._tx_thread: Optional[threading.Thread] = None
        self._running = False

        # 紧急停车 / 脚本中止
        self.abort_event = threading.Event()
        self._stop_cmd_index: Optional[int] = None  # 从命令目录 tag spec=Stop 识别

        # 自动重连（Backend ↔ 下位机）
        self._want_connected = False
        self._reconnect_thread: Optional[threading.Thread] = None

        self.status = "idle"
        self.status_msg = "未启动"
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ---------- 生命周期 ----------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """FastAPI lifespan 里注入主事件循环，供串口线程跨线程广播。"""
        self._loop = loop

    def start(self, auto_connect: bool = True) -> None:
        self._running = True
        self._tx_thread = threading.Thread(target=self._tx_loop, name="vex-tx", daemon=True)
        self._tx_thread.start()
        if auto_connect:
            threading.Thread(target=self._auto_connect, name="vex-connect", daemon=True).start()

    def _auto_connect(self) -> None:
        # 串口探测可能阻塞数秒，放线程里做，不卡 FastAPI startup
        if self.bridge.connect():
            self.bridge.start_heartbeat(1.0)
            # 脚本没连上位机时也要有目录：连上自动拉三张表
            self.enqueue(request_tunable(), PRI_UI)
            self.enqueue(request_monitor(), PRI_UI)
            self.enqueue(request_command(), PRI_UI)

    def ensure_connected(self) -> None:
        """保证 Backend ↔ 下位机有连接意愿，断线由重连线程兜底。"""
        self._want_connected = True
        if self.bridge.connected:
            return
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, name="vex-reconnect", daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        # 断线后每 2s 重试，直到连上或用户不想连了
        while self._want_connected and not self.bridge.connected:
            if self.bridge.connect():
                self.bridge.start_heartbeat(1.0)
                # 重连后目录缓存可能已过期，重新拉三张表
                self._dir_cache.clear()
                self.enqueue(request_tunable(), PRI_UI)
                self.enqueue(request_monitor(), PRI_UI)
                self.enqueue(request_command(), PRI_UI)
                # 把聚合后的订阅意图补发给下位机（重连不丢订阅）
                self._restore_subscriptions()
                return
            time.sleep(2.0)

    def _restore_subscriptions(self) -> None:
        with self._sub_lock:
            for idx, intent in self._sub_intent.items():
                if not intent:
                    continue
                sub, fast = self._aggregate(intent)
                self.fw_sub[idx] = (sub, fast)
                if sub:
                    self.enqueue(subscribe(idx, True, fast), PRI_UI)

    @staticmethod
    def _aggregate(intent: Dict[str, tuple]) -> tuple:
        """订阅取并集，fast 取已订阅者中最高档（True > False）。"""
        any_sub = any(v[0] for v in intent.values())
        any_fast = any(v[1] for v in intent.values() if v[0])
        return (any_sub, any_fast if any_sub else False)

    def stop(self) -> None:
        self._running = False
        self._want_connected = False
        with self._tx_cv:
            self._tx_cv.notify_all()
        self.bridge.disconnect()
        if self._tx_thread:
            self._tx_thread.join(timeout=1.0)

    def reconnect(self) -> bool:
        self.bridge.disconnect()
        self._dir_buf.clear()
        ok = self.bridge.connect()
        if ok:
            self.bridge.start_heartbeat(1.0)
            self.tunable_dir.clear()
            self.monitor_dir.clear()
            self.cmd_dir.clear()
            self._name_to_mon.clear()
            self._name_to_tun.clear()
            self._name_to_cmd.clear()
            self._dir_cache.clear()
            self.enqueue(request_tunable(), PRI_UI)
            self.enqueue(request_monitor(), PRI_UI)
            self.enqueue(request_command(), PRI_UI)
        return ok

    # ---------- 串口回调 ----------

    def _on_serial_status(self, state: str, message: str) -> None:
        self.status = state
        self.status_msg = message
        logger.info("串口状态 %s: %s", state, message)
        self._broadcast_json({"op": "status", "state": state, "message": message})
        # 掉线了但用户还想连着 → 自动重连
        if state == "disconnected" and self._want_connected and self._running:
            self.ensure_connected()

    def _on_serial_raw(self, chunk: bytes) -> None:
        # 上位机看到的串口和脚本看到的是同一股原始字节
        self._broadcast_bytes(chunk)
        # 从字节流里挑目录帧缓存，供目录代理回放；未收完的半帧尾巴留到下一批
        self._dir_buf.extend(chunk)
        frames, consumed = scan_dir_frames(bytes(self._dir_buf))
        for cmd, frame in frames:
            self._dir_cache.setdefault(cmd, []).append(frame)
        del self._dir_buf[:consumed]
        # 异常兜底：缓冲涨破两倍 burst 说明解析对不上，截掉旧数据防内存膨胀
        if len(self._dir_buf) > 8192:
            del self._dir_buf[:-4096]

    def _on_serial_event(self, event: dict) -> None:
        kind = event.get("kind")

        if kind == "directory":
            table = event["table"]
            item = event["item"]
            idx = item["index"]
            if table == "monitor":
                self.monitor_dir[idx] = item
                self._name_to_mon[item["name"]] = idx
                self.monitor_values[idx] = item.get("value")
                # 目录帧的 tag 反映下位机当前订阅档位；但已有聚合意图的项以意图为准，
                # 否则重连补订前的目录应答会把已生效的订阅状态覆盖掉
                if idx not in self._sub_intent:
                    self.fw_sub[idx] = (tag_subscribed(item["tag"]), tag_fast(item["tag"]))
            elif table == "tunable":
                self.tunable_dir[idx] = item
                self._name_to_tun[item["name"]] = idx

        elif kind == "cmdDirectory":
            item = event["item"]
            idx = item["index"]
            self.cmd_dir[idx] = item
            self._name_to_cmd[item["name"]] = idx
            if cmd_spec(item["tag"]) == CmdSpec.Stop:
                self._stop_cmd_index = idx

        elif kind == "value":
            idx = event["index"]
            val = event["value"]
            self.monitor_values[idx] = val
            self.monitor_time[idx] = time.monotonic()
            if self.logging and idx in self.log_reg:
                if len(self.log_buffer) >= MAX_LOG_SAMPLES:
                    self.log_dropped += 1
                else:
                    name = self.monitor_dir.get(idx, {}).get("name", str(idx))
                    self.log_buffer.append(
                        {
                            "t": round((time.monotonic() - self._log_t0) * 1000, 2),
                            "index": idx,
                            "name": name,
                            "value": val,
                        }
                    )

        elif kind == "echo":
            idx = event["index"]
            if idx in self.tunable_dir:
                self.tunable_dir[idx]["value"] = event["value"]

    # ---------- 下行优先级队列 ----------

    def enqueue(self, frame: bytes, priority: int = PRI_UI) -> None:
        with self._tx_cv:
            self._tx_seq += 1
            self._tx_q.append((priority, self._tx_seq, frame))
            # 插入后按 (优先级, 序号) 归位：STOP 永远排最前，同级保序
            self._tx_q = deque(sorted(self._tx_q, key=lambda x: (x[0], x[1])))
            self._tx_cv.notify()

    def _tx_loop(self) -> None:
        while self._running:
            with self._tx_cv:
                while self._running and not self._tx_q:
                    self._tx_cv.wait(timeout=0.1)
                if not self._running:
                    break
                if not self._tx_q:
                    continue
                pri, _seq, frame = self._tx_q.popleft()
            try:
                if self.bridge.connected:
                    self.bridge.send(frame)
                elif pri == PRI_STOP:
                    # 停车帧在断线时不能丢：塞回队首，等重连后立刻补发
                    with self._tx_cv:
                        self._tx_q.appendleft((pri, _seq, frame))
                    time.sleep(0.5)
            except Exception as e:
                logger.error("写串口失败: %s", e)

    # ---------- WebSocket ----------

    async def attach_ui(self, ws: WebSocket) -> None:
        await ws.accept()
        with self._ws_lock:
            self._ws_clients.add(ws)
        # 只推连接状态；目录由前端连上后自己发请求（与直连串口行为一致）
        await ws.send_json(
            {"op": "status", "state": self.status, "message": self.status_msg}
        )
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"] is not None:
                    self.from_ui(msg["bytes"], ws)
                elif "text" in msg and msg["text"] is not None:
                    try:
                        obj = json.loads(msg["text"])
                    except Exception as e:
                        logger.warning("UI JSON 解析失败: %s", e)
                        continue
                    op = obj.get("op")
                    if op == "stop":
                        self.emergency_stop()
                    elif op == "raw" and "payload" in obj:
                        self.from_ui(bytes(obj["payload"]), ws)
        finally:
            with self._ws_lock:
                self._ws_clients.discard(ws)
            # 该客户端断开后清掉它的订阅意图
            self._drop_client_intent(id(ws))

    def _drop_client_intent(self, conn_id: int) -> None:
        source = f"ui:{conn_id}"
        with self._sub_lock:
            changed = []
            for idx, intent in list(self._sub_intent.items()):
                if source in intent:
                    del intent[source]
                    if not intent:
                        del self._sub_intent[idx]
                    changed.append(idx)
            if changed:
                self._recompute_and_emit(set(changed))

    def _recompute_and_emit(self, indices: Set[int]) -> None:
        for idx in indices:
            intent = self._sub_intent.get(idx, {})
            new_sub, new_fast = self._aggregate(intent) if intent else (False, False)
            old = self.fw_sub.get(idx, (False, False))
            if (new_sub, new_fast) != old:
                self.fw_sub[idx] = (new_sub, new_fast)
                if new_sub:
                    self.enqueue(subscribe(idx, True, new_fast), PRI_UI)
                else:
                    self.enqueue(subscribe(idx, False, False), PRI_UI)

    def from_ui(self, data: bytes, ws: WebSocket) -> None:
        """上位机发来的原始帧：
        - Ping 直接回 Pong，不转发下位机
        - 目录请求用缓存回放，不重复拉下位机
        - Subscribe 做聚合，diff 后才发
        - 停车命令最高优先级
        - 其余透传
        """
        if len(data) < 2 or data[0] != 0xA5:
            return
        cmd = data[1]

        # 1) Ping → 直接回 Pong，记录心跳
        if cmd == CmdGet.Ping:
            try:
                loop = self._loop
                if loop:
                    asyncio.run_coroutine_threadsafe(ws.send_bytes(ping()), loop)
            except Exception as e:
                logger.debug("Ping 回复失败: %s", e)
            return

        # 2) 目录请求 → 缓存回放
        dir_map = {
            CmdGet.RequestTunable: (CmdPost.TunableDirectory, request_tunable),
            CmdGet.RequestMonitor: (CmdPost.MonitorDirectory, request_monitor),
            CmdGet.RequestCommand: (CmdPost.CMDDirectory, request_command),
        }
        if cmd in dir_map:
            post_cmd, request_fn = dir_map[cmd]
            frames = self._dir_cache.get(post_cmd, [])
            if frames:
                try:
                    loop = self._loop
                    if loop:
                        for frame in frames:
                            asyncio.run_coroutine_threadsafe(ws.send_bytes(frame), loop)
                except Exception as e:
                    logger.debug("目录回放失败: %s", e)
            else:
                # 缓存空了：补一次拉取，下位机应答 burst 会经广播送达该客户端
                self.enqueue(request_fn(), PRI_UI)
            return

        # 3) Subscribe → 聚合处理（防多页面互相退订）
        if cmd == CmdGet.Subscribe and len(data) >= 5:
            idx = data[2]
            tag = data[3]
            sub = tag_subscribed(tag)
            fast = tag_fast(tag)
            source = f"ui:{id(ws)}"
            with self._sub_lock:
                intent = self._sub_intent.setdefault(idx, {})
                intent[source] = (sub, fast)
                self._recompute_and_emit({idx})
            return

        # 4) 停车命令：插队 + 中止脚本
        if cmd == self._stop_index():
            self.emergency_stop()
            return

        # 5) 其余透传
        self.enqueue(data, PRI_UI)

    def _stop_index(self) -> int:
        # 目录未到时的兜底：本车停车固定是命令表第一项 0x80
        return self._stop_cmd_index if self._stop_cmd_index is not None else 0x80

    def _broadcast_json(self, obj: dict) -> None:
        loop = self._loop
        if loop is None:
            return
        with self._ws_lock:
            clients = list(self._ws_clients)
        if not clients:
            return
        text = json.dumps(obj, ensure_ascii=False, default=str)

        async def _send_all() -> None:
            dead = []
            for ws in clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            if dead:
                with self._ws_lock:
                    for w in dead:
                        self._ws_clients.discard(w)

        try:
            asyncio.run_coroutine_threadsafe(_send_all(), loop)
        except Exception as e:
            logger.debug("JSON 广播失败: %s", e)

    def _broadcast_bytes(self, data: bytes) -> None:
        loop = self._loop
        if loop is None:
            return
        with self._ws_lock:
            clients = list(self._ws_clients)
        if not clients:
            return

        async def _send_all() -> None:
            dead = []
            for ws in clients:
                try:
                    await ws.send_bytes(data)
                except Exception:
                    dead.append(ws)
            if dead:
                with self._ws_lock:
                    for w in dead:
                        self._ws_clients.discard(w)

        try:
            asyncio.run_coroutine_threadsafe(_send_all(), loop)
        except Exception as e:
            logger.debug("字节广播失败: %s", e)

    # ---------- 脚本 API ----------

    def resolve_monitor(self, index_or_name) -> int:
        if isinstance(index_or_name, int):
            return index_or_name
        name = str(index_or_name)
        # HTTP 传过来的 index 是字符串数字（如 "5"），先按数字解释
        if name.isdigit() and int(name) in self.monitor_dir:
            return int(name)
        if name in self._name_to_mon:
            return self._name_to_mon[name]
        for n, i in self._name_to_mon.items():  # 容错：忽略大小写
            if n.lower() == name.lower():
                return i
        raise KeyError(f"监控项不存在: {index_or_name}")

    def resolve_tunable(self, index_or_name) -> int:
        if isinstance(index_or_name, int):
            return index_or_name
        name = str(index_or_name)
        if name in self._name_to_tun:
            return self._name_to_tun[name]
        for n, i in self._name_to_tun.items():
            if n.lower() == name.lower():
                return i
        raise KeyError(f"调参项不存在: {index_or_name}")

    def resolve_cmd(self, index_or_name) -> dict:
        if isinstance(index_or_name, int):
            if index_or_name not in self.cmd_dir:
                raise KeyError(f"命令字不存在: 0x{index_or_name:02X}")
            return self.cmd_dir[index_or_name]
        name = str(index_or_name)
        if name in self._name_to_cmd:
            return self.cmd_dir[self._name_to_cmd[name]]
        for n, i in self._name_to_cmd.items():
            if n.lower() == name.lower():
                return self.cmd_dir[i]
        raise KeyError(f"命令不存在: {index_or_name}")

    def get_column(self, type_: str) -> List[dict]:
        t = type_.lower()
        if t in ("monitor", "mon"):
            return list(self.monitor_dir.values())
        if t in ("tunable", "tun"):
            return list(self.tunable_dir.values())
        if t in ("cmd", "command", "commands"):
            return list(self.cmd_dir.values())
        raise ValueError("type 必须是 monitor | tunable | cmd")

    def read_item(self, index_or_name) -> dict:
        """单项当前值（看门狗用）：index(int) 或目录中文名均可。
        前提是该项正在推送（push/log/both 任一），否则没有实时数据。
        age_s 为距上次收到值的秒数，用来判断推送是否已断。"""
        idx = self.resolve_monitor(index_or_name)
        if idx not in self.monitor_values:
            raise KeyError(f"监控项尚无数据（先 push 订阅再 read）: {index_or_name}")
        item = self.monitor_dir.get(idx, {})
        return {
            "index": idx,
            "name": item.get("name", str(idx)),
            "value": self.monitor_values[idx],
            "unit": item.get("unit", ""),
            "age_s": round(time.monotonic() - self.monitor_time.get(idx, 0.0), 3),
        }

    def read_items(self, keys: list) -> List[dict]:
        """多项当前值，一次返回（看门狗一轮要看 x/y/yaw，避免连发三个请求）。"""
        return [self.read_item(k) for k in keys]

    def read(self, *index_or_name):
        """最便捷读法：单项直接拿数值（hub.read('x')）；
        多项一次返回 {名字: 值}（hub.read('x','y','yaw')）。"""
        if len(index_or_name) == 1:
            return self.read_item(index_or_name[0])["value"]
        return {it["name"]: it["value"] for it in self.read_items(list(index_or_name))}

    def sub(
        self,
        index_or_name,
        kind: str = "both",
        *,
        on: bool = True,
        fast: Optional[bool] = None,
    ) -> dict:
        """
        两种订阅可叠加：
          push - 只改下位机推送档位（Subscribe 帧），用于砍带宽，fast 指定高/低速
          log  - 只在后端登记/取消记录；登记时若该项不是高速推送则自动提升成高速，
                 取消登记不动下位机档位（避免把上位机正在看的推送关掉）
          both - 两者都做
        """
        idx = self.resolve_monitor(index_or_name)
        kind = kind.lower()
        result = {
            "index": idx,
            "name": self.monitor_dir.get(idx, {}).get("name"),
            "push": None,
            "log": None,
        }

        if kind in ("push", "both"):
            use_fast = bool(fast) if fast is not None else on
            with self._sub_lock:
                intent = self._sub_intent.setdefault(idx, {})
                intent["script"] = (on, use_fast)
                self._recompute_and_emit({idx})
            result["push"] = {"sub": on, "fast": use_fast}

        if kind in ("log", "both"):
            if on:
                self.log_reg.add(idx)
                # 日志要有数据可记：不是高速推送就自动提升（前提/提升二选一，统一提升）
                if kind == "log":
                    cur = self.fw_sub.get(idx, (False, False))
                    if not cur[1]:
                        with self._sub_lock:
                            intent = self._sub_intent.setdefault(idx, {})
                            intent["script"] = (True, True)
                            self._recompute_and_emit({idx})
                        result["push"] = {"sub": True, "fast": True}
            else:
                self.log_reg.discard(idx)
            result["log"] = on

        return result

    def emergency_stop(self) -> dict:
        """最高优先级停车：发停车帧、清脚本队列、置 abort 打断脚本。"""
        self.abort_event.set()
        with self._tx_cv:
            # 只留比脚本优先级高的帧（上位机的请求/订阅不丢），脚本待发帧全清
            self._tx_q = deque(x for x in self._tx_q if x[0] < PRI_SCRIPT)
            self._tx_cv.notify()

        idx = self._stop_index()
        self.enqueue(build_frame(idx, b""), PRI_STOP)
        return {"ok": True, "cmd_index": idx}

    def clear_abort(self) -> None:
        self.abort_event.clear()

    def check_abort(self) -> None:
        if self.abort_event.is_set():
            raise RuntimeError("已紧急停车 / 脚本被中止（调 clear_abort 后才能继续）")

    def cmd(self, index_or_name, *args) -> dict:
        self.check_abort()
        item = self.resolve_cmd(index_or_name)
        fields = item.get("fields") or []
        types = [f["type"] for f in fields]
        values = list(args)
        # 参数个数必须和目录完全一致：少给静默补 0 可能触发未预期动作，多给更是写错
        if len(values) != len(types):
            raise ValueError(
                f"命令 {item['name']} 需要 {len(types)} 个参数"
                f"（{[f['name'] for f in fields]}），实际给了 {len(values)} 个"
            )
        self.enqueue(command_send(item["index"], types, values), PRI_SCRIPT)
        return {
            "ok": True,
            "index": item["index"],
            "name": item["name"],
            "args": values,
        }

    def set_tunable_value(self, index_or_name, value) -> dict:
        self.check_abort()
        idx = self.resolve_tunable(index_or_name)
        item = self.tunable_dir[idx]
        self.enqueue(set_tunable(idx, item["type"], value), PRI_SCRIPT)
        return {"ok": True, "index": idx, "value": value}

    # ---------- 日志 ----------

    def start_log(self) -> dict:
        self.log_buffer.clear()
        self.log_dropped = 0
        self._log_t0 = time.monotonic()
        self.logging = True
        return {"ok": True, "registered": sorted(self.log_reg)}

    def end_log(self) -> dict:
        self.logging = False
        return {"ok": True, "samples": len(self.log_buffer), "dropped": self.log_dropped}

    def print_log(self, path: Optional[str] = None) -> dict:
        """写 JSON 全量记录 + CSV 长表到 pytest/，返回文件信息。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if path:
            base = Path(path)
            json_path = base if base.suffix else base.with_name(base.name + ".json")
            csv_path = json_path.with_suffix(".csv")
        else:
            json_path = self.pytest_dir / f"log_{ts}.json"
            csv_path = self.pytest_dir / f"log_{ts}.csv"
        json_path.parent.mkdir(parents=True, exist_ok=True)

        meta = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "samples": len(self.log_buffer),
            "dropped": self.log_dropped,
            "registered": [
                {"index": i, "name": self.monitor_dir.get(i, {}).get("name", str(i))}
                for i in sorted(self.log_reg)
            ],
            "data": self.log_buffer,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, default=str)

        # 长表：每条推送一行，Excel/pandas 直接可读
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write("t_ms,index,name,value\n")
            for row in self.log_buffer:
                f.write(f"{row['t']},{row['index']},{row['name']},{row['value']}\n")

        return {
            "ok": True,
            "json": str(json_path),
            "csv": str(csv_path),
            "samples": len(self.log_buffer),
            "dropped": self.log_dropped,
        }

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "message": self.status_msg,
            "connected": self.bridge.connected,
            "monitor_count": len(self.monitor_dir),
            "tunable_count": len(self.tunable_dir),
            "cmd_count": len(self.cmd_dir),
            "log_reg": sorted(self.log_reg),
            "logging": self.logging,
            "log_samples": len(self.log_buffer),
            "abort": self.abort_event.is_set(),
            "stop_cmd": self._stop_cmd_index,
            "ports": SerialBridge.list_ports(),
        }
