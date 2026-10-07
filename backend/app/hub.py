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
    CmdSpec,
    build_frame,
    cmd_spec,
    command_send,
    request_command,
    request_monitor,
    request_tunable,
    set_tunable,
    subscribe,
    tag_fast,
    tag_subscribed,
)
from .serial_bridge import SerialBridge

logger = logging.getLogger("vex.hub")

# 命令优先级：数字越小越优先
PRI_STOP = 0
PRI_UI = 10
PRI_SCRIPT = 20

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

        # 最新监控值快照（/api/values 用）
        self.monitor_values: Dict[int, Any] = {}
        # 每项最近一次收到推送的时刻（看门狗判断数据是否新鲜，不存历史）
        self.monitor_time: Dict[int, float] = {}
        # 下位机侧推送订阅意图 index → (sub, fast)
        self.fw_sub: Dict[int, tuple] = {}
        # 后端日志登记项（脚本 sub(..., 'log'/'both') 登记）
        self.log_reg: Set[int] = set()

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

    def stop(self) -> None:
        self._running = False
        with self._tx_cv:
            self._tx_cv.notify_all()
        self.bridge.disconnect()
        if self._tx_thread:
            self._tx_thread.join(timeout=1.0)

    def reconnect(self) -> bool:
        self.bridge.disconnect()
        ok = self.bridge.connect()
        if ok:
            self.bridge.start_heartbeat(1.0)
            self.tunable_dir.clear()
            self.monitor_dir.clear()
            self.cmd_dir.clear()
            self._name_to_mon.clear()
            self._name_to_tun.clear()
            self._name_to_cmd.clear()
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

    def _on_serial_raw(self, chunk: bytes) -> None:
        # 上位机看到的串口和脚本看到的是同一股原始字节
        self._broadcast_bytes(chunk)

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
                # 目录帧的 tag 反映下位机当前订阅档位，先同步一份事实
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
                _pri, _seq, frame = self._tx_q.popleft()
            try:
                if self.bridge.connected:
                    self.bridge.send(frame)
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
                    self.from_ui(msg["bytes"])
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
                        self.from_ui(bytes(obj["payload"]))
        finally:
            with self._ws_lock:
                self._ws_clients.discard(ws)

    def from_ui(self, data: bytes) -> None:
        """上位机发来的原始帧：识别停车并升级，其余高优先级透传。"""
        # 帧: A5 | Cmd | ... 停车命令插队 + 中止脚本，保证上位机随时收车
        if len(data) >= 2 and data[0] == 0xA5 and data[1] == self._stop_index():
            self.emergency_stop()
            return
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
            self.fw_sub[idx] = (on, use_fast)
            self.enqueue(subscribe(idx, on, use_fast), PRI_SCRIPT)
            result["push"] = {"sub": on, "fast": use_fast}

        if kind in ("log", "both"):
            if on:
                self.log_reg.add(idx)
                # 日志要有数据可记：不是高速推送就自动提升（前提/提升二选一，统一提升）
                if kind == "log":
                    cur = self.fw_sub.get(idx, (False, False))
                    if not cur[1]:
                        self.fw_sub[idx] = (True, True)
                        self.enqueue(subscribe(idx, True, True), PRI_SCRIPT)
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
        if len(values) > len(types):
            raise ValueError(f"命令 {item['name']} 只要 {len(types)} 个参数，给了 {len(values)} 个")
        # 缺省参数补 0，脚本里少写一个也能跑（实际范围由目录 min/max 约束，脚本自负）
        values.extend([0] * (len(types) - len(values)))
        self.enqueue(command_send(item["index"], types, values), PRI_SCRIPT)
        return {
            "ok": True,
            "index": item["index"],
            "name": item["name"],
            "args": values[: len(types)],
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
