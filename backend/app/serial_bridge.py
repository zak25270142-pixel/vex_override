"""
串口桥：独占 V5 User 口，读写二进制协议帧。
识别口的办法与前端 webSerialTransport 的 userPortProbe 一致：
    下载口/User 口同 VID/PID，只有 User 口跑用户程序会回 Pong(A5 FF FF)，逐口 Ping 探测。

为什么必须探测：
    V5 有两个 USB 口（下载口 / User 口），描述符里的 VID/PID 是一样的，
    操作系统层面无法区分。唯一区别是 User 口上跑着用户程序，收到 Ping 会回 Pong。
    所以只能逐个口发 Ping，谁回 A5 FF FF 谁就是 User 口。

本文件只管"字节怎么进出串口"，不关心帧的业务含义——解析交给 protocol.FrameParser，
调度交给 hub.Hub。写串口的入口虽在这里，但实际由 Hub 的优先级队列串行调用。
"""

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum

from .protocol import FRAME_HEAD, EventKind, FrameParser, ping

logger = logging.getLogger("vex.serial")


class Status(Enum):
    """串口连接状态。值就是推给前端的字符串（前端按这些值决定指示灯颜色）。

    idle         未连接（含用户主动断开）
    connecting   正在探测/打开串口
    connected    已连上且 rx 线程在跑
    disconnected 意外掉线（拔线/读异常/心跳超时）→ Hub 据此触发自动重连
    error        连不上（没装 pyserial / 没设备 / 认不出 V5 User 口）
    """
    idle = "idle"
    connecting = "connecting"
    connected = "connected"
    disconnected = "disconnected"
    error = "error"

# pyserial 是可选依赖：没装时本模块仍能导入（list_ports 返回空、connect 报错），
# 这样后端在没有串口的开发机上也能启动并暴露 HTTP 接口供前端联调。
try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover
    serial = None  # type: ignore

# VEX Robotics 的 USB VID；描述符匹配不到时用它优先（真正判定仍靠 Ping 应答）
VEX_VID = 0x2888


class SerialBridge:
    """线程安全的串口读写；读走独立线程 + FrameParser，写由 Hub 队列统一调度。

    三个回调让本类与上层解耦（Hub 在构造时注入）：
        on_raw(chunk)      每收到一批原始字节就调，Hub 原样广播给 WebSocket
        on_event(event)    FrameParser 解出一个事件就调，Hub 据此更新目录/快照/日志
        on_status(state,msg) 连接状态变化，Hub 广播给前端并触发自动重连（state 是 Status 枚举）
    """

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        on_event: Callable[[dict], None] | None = None,
        on_status: Callable[[Status, str], None] | None = None,
        on_raw: Callable[[bytes], None] | None = None,
    ) -> None:
        self.port_name = port
        self.baud = baud
        
        # 不给回调就塞空 lambda，省得调用处到处判 None
        self.on_event = on_event or (lambda e: None)
        '''事件回调，解析后的事件'''

        self.on_status = on_status or (lambda s, m: None)
        '''状态回调，连接状态变化'''

        self.on_raw = on_raw or (lambda data: None)
        '''原始数据接收回调，每收到一批原始字节就调
           Hub 用它把串口字节原样广播给 WebSocket 上位机'''

        self._ser: serial.Serial | None = None
        '''串口对象'''

        self._parser = FrameParser()
        # 写锁：虽然目前只有 Hub 的 tx 线程会调 send，但 send 是公共方法，
        # 留锁防将来有人从别的线程直接写，两次 write 交错会撕帧。
        self._tx_lock = threading.Lock()
        self._rx_thread: threading.Thread | None = None

        self._stop = threading.Event()  
        '''全局停止标志，置位后 rx 循环退出'''

        self._connected = False
        '''连接状态标志位'''
        
        self._last_pong = 0.0  
        '''上次收到 Pong 的 monotonic 时刻，心跳超时判定用'''
        '''心跳单例：重连叠调 start_heartbeat 时先停旧线程，避免多路 Ping 打架'''

        self._hb_thread: threading.Thread | None = None
        '''心跳线程'''

        self._hb_stop = threading.Event()  
        '''心跳线程停止标志'''

    @property
    def connected(self) -> bool:
        # 三重判断：标志位 + 对象存在 + 端口真开着。
        # 只判 _connected 会在拔线后仍返回 True（标志还没被 _lost 清掉）。
        return self._connected and self._ser is not None and self._ser.is_open

    @property
    def parser(self) -> FrameParser:
        return self._parser

    @property
    def last_pong(self) -> float:
        return self._last_pong

    # ---------- 端口枚举 ----------

    @staticmethod
    def list_ports() -> list[dict]:
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

    def find_candidates(self) -> list[str]:
        """寻找候选串口
        优先返回可能是 V5 的口"""
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
            self.on_status(Status.error, "未安装 pyserial，请 pip install pyserial")
            return False
        if self.connected:
            return True

        candidates = self.find_candidates()
        if not candidates:
            self.on_status(Status.error, "未发现串口设备")
            return False

        self.on_status(Status.connecting, f"探测 {len(candidates)} 个候选口…")
        last_err = ""
        for dev in candidates:
            try:
                ser = serial.Serial(
                    port=dev,
                    baudrate=self.baud,
                    timeout=0.05,  # 读超时短：rx 线程靠 in_waiting 轮询，不需要长阻塞
                    write_timeout=0.5,
                )
            except Exception as e:
                # 打不开（被别的进程占、权限不足）就换下一个候选
                last_err = str(e)
                logger.warning("打开 %s 失败: %s", dev, e)
                continue

            if self._probe(ser, timeout):
                self._ser = ser
                self._connected = True
                self._stop.clear()
                self._rx_thread = threading.Thread(
                    target=self._rx_loop, name="vex-serial-rx", daemon=True
                )
                self._rx_thread.start()
                self.on_status(Status.connected, f"已连接 {dev}")
                logger.info("串口已连接: %s", dev)
                return True

            # 探测失败：关掉这个口再试下一个（不关会一直占着句柄）
            try:
                ser.close()
            except Exception:
                pass
            last_err = f"{dev} 无 Pong 应答"

        self.on_status(Status.error, f"未能识别 V5 User 口: {last_err}")
        return False

    # 签名注解必须加引号：函数签名（参数/返回）的注解在 def 执行时就求值，
    # 而函数体内的注解不求值。pyserial 未装时 serial 为 None，
    # 不加引号这里会 AttributeError: 'NoneType' object has no attribute 'Serial'。
    def _probe(self, ser: "serial.Serial", timeout: float = 0.3) -> bool:
        """发 Ping，在 timeout 内找到完整 Pong(A5 FF FF) 才算这个口对。"""
        try:
            ser.reset_input_buffer()  # 清掉打开端口时可能残留的旧字节
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
                    # 线性扫描找 A5 FF FF。只在探测期用，正常收数靠 FrameParser 状态机
                    for i in range(len(acc) - 2):
                        if acc[i] == FRAME_HEAD and acc[i + 1] == 0xFF and acc[i + 2] == 0xFF:
                            return True
            except Exception:
                return False
            time.sleep(0.01)
            # 后半程补发一次 Ping，防止首帧丢在 USB 枚举窗口里
            # （刚 open 时设备可能还没准备好接收，第一帧会石沉大海）
            if time.monotonic() > deadline - timeout / 2:
                try:
                    ser.write(frame)
                except Exception:
                    pass
        return False

    def disconnect(self) -> None:
        """主动断开：停心跳 → 置停止标志 → 收 rx 线程 → 关口。"""
        self.stop_heartbeat()
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
        self.on_status(Status.idle, "串口已断开")

    def _lost(self, message: str) -> None:
        """掉线善后（拔线/读异常/心跳超时）：关口并发 disconnected，由 Hub 决定重连。

        与 disconnect 的区别：这里是"意外掉线"，会上报 disconnected 状态，
        Hub 收到后若 _want_connected 为真会自动重连；disconnect 是用户主动断，报 idle。
        """
        if not self._connected and self._ser is None:
            return  # 已经断过了，不重复上报（_lost 会被 rx/心跳发送/心跳超时三处调用）
        self.stop_heartbeat()
        self._stop.set()
        self._connected = False
        # 从 rx 线程自己调进来时不 join 自己——线程 join 自己会永远等下去（经典死锁）
        if self._rx_thread and self._rx_thread.is_alive() and threading.current_thread() is not self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        self._rx_thread = None
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self.on_status(Status.disconnected, message)

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
                        # 再喂 Python 解析器——同一股字节服务两个消费者，
                        # 前端因此无需改动拆分逻辑。
                        self.on_raw(chunk)
                        self._parser.feed(chunk, self._on_parsed)
                else:
                    # 没数据时短睡：2ms ≈ 500Hz 轮询，够快又不烧 CPU
                    time.sleep(0.002)
            except Exception as e:
                if not self._stop.is_set():
                    logger.error("串口读异常: %s", e)
                    self._lost(f"串口读取异常: {e}")
                break

    def _on_parsed(self, event: dict) -> None:
        # Pong 在这里被"截获"更新时间戳，同时也继续往下传给 on_event
        if event.get("kind") is EventKind.pong:
            self._last_pong = time.monotonic()
        try:
            self.on_event(event)
        except Exception as e:
            # 上层回调抛异常不能把 rx 线程带崩，否则整个链路静默死亡
            logger.exception("on_event 异常: %s", e)

    # ---------- 心跳 ----------

    # 心跳 1s 一发，超过 5s 没收到 Pong 判定下位机程序已死（USB 还连着但链路没意义）
    PONG_TIMEOUT_S = 5.0

    def stop_heartbeat(self) -> None:
        """停心跳线程并等它退出；从心跳线程自己调 _lost 进来时不 join（等自己无意义）。"""
        self._hb_stop.set()
        t = self._hb_thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=1.5)
        self._hb_thread = None

    def start_heartbeat(self, interval: float = 1.0) -> threading.Thread:
        """单例心跳：重复调用先停旧线程再启，避免重连后叠出多个 Ping 源。"""
        self.stop_heartbeat()
        self._hb_stop.clear()

        def _loop() -> None:
            while not self._hb_stop.is_set() and not self._stop.is_set() and self.connected:
                try:
                    self.send(ping())
                except Exception as e:
                    logger.warning("心跳发送失败: %s", e)
                    self._lost(f"心跳发送失败: {e}")
                    break
                # 可中断 sleep：stop_heartbeat 不用等满一个周期才返回。
                # Event.wait 返回 True = 被 set 唤醒（该停了），False = 正常超时（继续下一拍）
                if self._hb_stop.wait(interval):
                    break
                last = self._last_pong
                if last > 0 and time.monotonic() - last > self.PONG_TIMEOUT_S:
                    logger.warning("超过 %.0fs 未收到 Pong，判定下位机掉线", self.PONG_TIMEOUT_S)
                    self._lost("心跳超时，下位机无响应")
                    break

        t = threading.Thread(target=_loop, name="vex-heartbeat", daemon=True)
        self._hb_thread = t
        t.start()
        return t
