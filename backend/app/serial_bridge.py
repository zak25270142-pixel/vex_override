"""
串口桥：独占 V5 User 口，读写二进制协议帧。
识别口的办法与前端 webSerialTransport 的 userPortProbe 一致：
    下载口/User 口同 VID/PID，只有 User 口跑用户程序会回 Pong(A5 FF FF)，逐口 Ping 探测。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

from .protocol import FRAME_HEAD, FrameParser, ping

logger = logging.getLogger("vex.serial")

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover
    serial = None  # type: ignore

# VEX Robotics 的 USB VID；描述符匹配不到时用它优先（真正判定仍靠 Ping 应答）
VEX_VID = 0x2888


class SerialBridge:
    """线程安全的串口读写；读走独立线程 + FrameParser，写由 Hub 队列统一调度。"""

    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = 115200,
        on_event: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[str, str], None]] = None,
        on_raw: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.port_name = port
        self.baud = baud
        self.on_event = on_event or (lambda e: None)
        self.on_status = on_status or (lambda s, m: None)
        # 原始 chunk 回调：Hub 用它把串口字节原样广播给 WebSocket 上位机（不拆帧）
        self.on_raw = on_raw or (lambda data: None)

        self._ser: Optional["serial.Serial"] = None
        self._parser = FrameParser()
        self._tx_lock = threading.Lock()
        self._rx_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False
        self._last_pong = 0.0

    @property
    def connected(self) -> bool:
        return self._connected and self._ser is not None and self._ser.is_open

    @property
    def parser(self) -> FrameParser:
        return self._parser

    @property
    def last_pong(self) -> float:
        return self._last_pong

    # ---------- 端口枚举 ----------

    @staticmethod
    def list_ports() -> List[dict]:
        if serial is None:
            return []
        out = []
        for p in serial.tools.list_ports.comports():
            out.append(
                {
                    "device": p.device,
                    "description": p.description or "",
                    "vid": p.vid,
                    "pid": p.pid,
                    "serial_number": p.serial_number or "",
                }
            )
        return out

    def find_candidates(self) -> List[str]:
        """优先返回可能是 V5 的口；若环境变量指定了口则只试它。"""
        if self.port_name:
            return [self.port_name]
        preferred = []
        others = []
        for p in self.list_ports():
            dev = p["device"]
            desc = (p["description"] or "").lower()
            if p["vid"] == VEX_VID or "vex" in desc or "user" in desc:
                preferred.append(dev)
            else:
                others.append(dev)
        return preferred + others

    # ---------- 连接 / 探测 ----------

    def connect(self, timeout: float = 2.0) -> bool:
        if serial is None:
            self.on_status("error", "未安装 pyserial，请 pip install pyserial")
            return False
        if self.connected:
            return True

        candidates = self.find_candidates()
        if not candidates:
            self.on_status("error", "未发现串口设备")
            return False

        self.on_status("connecting", f"探测 {len(candidates)} 个候选口…")
        last_err = ""
        for dev in candidates:
            try:
                ser = serial.Serial(
                    port=dev,
                    baudrate=self.baud,
                    timeout=0.05,
                    write_timeout=0.5,
                )
            except Exception as e:
                last_err = str(e)
                logger.warning("打开 %s 失败: %s", dev, e)
                continue

            if self._probe(ser, timeout=0.3):
                self._ser = ser
                self._connected = True
                self._stop.clear()
                self._rx_thread = threading.Thread(
                    target=self._rx_loop, name="vex-serial-rx", daemon=True
                )
                self._rx_thread.start()
                self.on_status("connected", f"已连接 {dev}")
                logger.info("串口已连接: %s", dev)
                return True

            try:
                ser.close()
            except Exception:
                pass
            last_err = f"{dev} 无 Pong 应答"

        self.on_status("error", f"未能识别 V5 User 口: {last_err}")
        return False

    def _probe(self, ser: "serial.Serial", timeout: float = 0.3) -> bool:
        """发 Ping，在 timeout 内找到完整 Pong(A5 FF FF) 才算这个口对。"""
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        frame = ping()
        deadline = time.monotonic() + timeout
        acc = bytearray()
        try:
            ser.write(frame)
        except Exception:
            return False
        while time.monotonic() < deadline:
            try:
                n = ser.in_waiting
                if n:
                    chunk = ser.read(n)
                    acc.extend(chunk)
                    # 目录 burst 也不过 4KB，探测阶段只找 Pong，留足余量即可
                    if len(acc) > 4096:
                        del acc[:-4096]
                    for i in range(len(acc) - 2):
                        if acc[i] == FRAME_HEAD and acc[i + 1] == 0xFF and acc[i + 2] == 0xFF:
                            return True
            except Exception:
                return False
            time.sleep(0.01)
            # 后半程补发一次 Ping，防止首帧丢在 USB 枚举窗口里
            if time.monotonic() > deadline - timeout / 2:
                try:
                    ser.write(frame)
                except Exception:
                    pass
        return False

    def disconnect(self) -> None:
        self._stop.set()
        self._connected = False
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=1.0)
        self._rx_thread = None
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self.on_status("idle", "串口已断开")

    # ---------- 收发 ----------

    def send(self, data: bytes) -> None:
        if not self.connected or not self._ser:
            raise RuntimeError("串口未连接")
        with self._tx_lock:
            self._ser.write(data)

    def _rx_loop(self) -> None:
        assert self._ser is not None
        while not self._stop.is_set():
            try:
                n = self._ser.in_waiting
                if n:
                    # 一次把已到的字节全读走：目录 burst 约 4KB，小缓冲会丢帧
                    chunk = self._ser.read(n)
                    if chunk:
                        # 先原样喂给 WS 上位机（半帧/粘包由前端 FrameParser 自己拼）
                        self.on_raw(chunk)
                        self._parser.feed(chunk, self._on_parsed)
                else:
                    time.sleep(0.002)
            except Exception as e:
                if not self._stop.is_set():
                    logger.error("串口读异常: %s", e)
                    self._connected = False
                    self.on_status("error", f"串口读取异常: {e}")
                break

    def _on_parsed(self, event: dict) -> None:
        if event.get("kind") == "pong":
            self._last_pong = time.monotonic()
        try:
            self.on_event(event)
        except Exception as e:
            logger.exception("on_event 异常: %s", e)

    # ---------- 心跳 ----------

    def start_heartbeat(self, interval: float = 1.0) -> threading.Thread:
        def _loop() -> None:
            while not self._stop.is_set() and self.connected:
                try:
                    self.send(ping())
                except Exception as e:
                    logger.warning("心跳发送失败: %s", e)
                time.sleep(interval)

        t = threading.Thread(target=_loop, name="vex-heartbeat", daemon=True)
        t.start()
        return t
