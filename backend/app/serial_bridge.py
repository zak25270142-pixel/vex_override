"""
串口桥：独占 V5 User 口，读写二进制协议帧。
识别口的办法与前端 webSerialTransport 的 userPortProbe 一致：
    下载口/User 口同 VID/PID，只有 User 口跑用户程序会回 Pong(A5 FF FF)，逐口 Ping 探测。

为什么必须探测：
    V5 有两个 USB 口（下载口 / User 口），描述符里的 VID/PID 是一样的，
    操作系统层面无法区分。唯一区别是 User 口上跑着用户程序，收到 Ping 会回 Pong。
    所以只能逐个口发 Ping，谁回 A5 FF FF 谁就是 User 口。

谁有资格被 Ping（不抢占别人的口）：
    只有带 VEX 标识的口（VID 0x2888，或描述符含 vex/user）才会被打开——
    打开串口即独占，乱开别的设备的口会打断烧录和其他进程。没识别出 VEX 口时
    直接收工，不穷举本机全部串口。每个候选口最多等 120ms（主控 10ms 一拍即回
    Pong），认不出就关掉换下一个；Hub 的重连循环每 5s 才整体试一轮。

本文件只管"字节怎么进出串口"，不关心帧的业务含义——解析交给 protocol.FrameParser，
调度交给 hub.Hub。写串口的入口虽在这里，但实际由 Hub 的优先级队列串行调用。
"""

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum

from .dataclass import PortInfo
from .protocol import FRAME_HEAD, CmdPost, FrameParser, ping

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

# 主控 10ms 一拍即回，120ms 留有十倍余量；窗口内每 30ms 补发一次防首帧丢在枚举窗口
PROBE_TIMEOUT_S = 0.12
'''单个候选口等 Pong 的时长'''


class SerialBridge:
    """线程安全的串口读写；读走独立线程 + FrameParser，写由 Hub 队列统一调度。

    三个回调让本类与上层解耦（Hub 在构造时注入）：
        on_raw(chunk)          每收到一批原始字节就调，Hub 原样广播给 WebSocket
        on_frame(cmd, payload) FrameParser 解出一完整帧就调，Hub 据此更新目录/快照/日志
        on_status(state,msg)   连接状态变化，Hub 广播给前端并触发自动重连（state 是 Status 枚举）
    """

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        on_frame: Callable[[int, bytes], None] | None = None,
        on_status: Callable[[Status, str], None] | None = None,
        on_raw: Callable[[bytes], None] | None = None,
    ) -> None:
        """建串口桥（不立即开串口，connect() 才探测打开）。

        port     : 指定串口名（如 "COM5"）；None=connect 时自动逐口 Ping 探测
        baud     : 波特率，CDC 虚拟口仅占位（USB 全速跑，实际不影响）
        on_frame : 完整帧回调（Hub._on_frame），参数 (cmd, payload)
        on_status: 连接状态变化回调（Hub._on_serial_status），收到 Status 枚举 + 人读说明
        on_raw   : 原始字节回调（Hub._on_serial_raw），用来原样广播给 WS 上位机
        """
        self.port_name = port
        '''指定串口名；None=自动探测'''
        self.baud = baud
        '''波特率（CDC 虚拟口仅占位）'''
        
        # 不给回调就塞空 lambda，省得调用处到处判 None
        self.on_frame = on_frame or (lambda cmd, payload: None)
        '''完整帧回调 (cmd, payload)：FrameParser 解出的一帧，语义归 Hub'''

        self.on_status = on_status or (lambda s, m: None)
        '''状态回调，连接状态变化'''

        self.on_raw = on_raw or (lambda data: None)
        '''原始数据接收回调，每收到一批原始字节就调
           Hub 用它把串口字节原样广播给 WebSocket 上位机'''

        self._ser: serial.Serial | None = None
        '''串口对象'''

        self._parser = FrameParser()
        '''帧解析器：喂收上来的字节，吐 (cmd, payload)，业务语义归 Hub'''

        # 写锁：Hub 的 tx 线程和内置心跳线程都会调 send，且 send 是公共方法，
        # 锁负责把多次 write 串行化，交错会撕帧。
        self._tx_lock = threading.Lock()
        '''写锁：串行化 send()，防多线程写交错撕帧'''
        
        self._rx_thread: threading.Thread | None = None
        '''收数线程（connect 成功时启动，disconnect/_lost 时收尾）'''

        self._stop = threading.Event()  
        '''全局停止标志，置位后 rx 循环退出'''

        self._connected = False
        '''连接状态标志位'''
        
        self._last_pong = 0.0  
        '''上次收到 Pong 的 monotonic 时刻，心跳超时判定用'''

        self._hb_thread: threading.Thread | None = None
        '''心跳线程；单例：重连叠调 start_heartbeat 时先停旧线程，避免多路 Ping 打架'''

        self._hb_stop = threading.Event()  
        '''心跳线程停止标志'''

    @property
    def connected(self) -> bool:
        """当前是否真正连着（三重判断，供 Hub/路由判断能否发帧）。"""
        # 三重判断：标志位 + 对象存在 + 端口真开着。
        # 只判 _connected 会在拔线后仍返回 True（标志还没被 _lost 清掉）。
        return self._connected and self._ser is not None and self._ser.is_open

    # ---------- 端口枚举 ----------

    @staticmethod
    def list_ports() -> list[PortInfo]:
        """枚举本机串口设备（pyserial 描述符裁剪成 PortInfo）。

        返回: PortInfo 列表；没装 pyserial 时返回空列表（不抛错，便于无串口开发机启动）
        """
        if serial is None:
            return []
        out = []
        for p in serial.tools.list_ports.comports():
            out.append(
                PortInfo(
                    device=p.device,
                    description=p.description or "",
                    vid=p.vid,
                    pid=p.pid,
                    serial_number=p.serial_number or "",
                )
            )
        return out

    def find_candidates(self) -> list[str]:
        """给出待探测的串口名列表：只含 VEX 标识的口（VID / 描述符认得出）。

        返回: 设备名字符串列表；指定了 self.port_name 就只返回它（跳过自动探测）。
        没有 VEX 口时返回空列表——非 VEX 设备的串口一律不打开（打开即独占，
        会打断烧录和其他进程），这就是"没识别到就不抢占"。
        """
        if self.port_name:
            return [self.port_name]
        out = []
        for p in self.list_ports():
            desc = (p.description or "").lower()
            if p.vid == VEX_VID or "vex" in desc or "user" in desc:
                out.append(p.device)
        return out

    # ---------- 连接 / 探测 ----------

    def connect(self, timeout: float = PROBE_TIMEOUT_S) -> bool:
        """打开并认领 V5 User 口：逐候选口发 Ping，认得出才建收数线程。

        timeout: 每个候选口的探测时长（秒），默认 120ms；主控一拍（10ms）就回 Pong，
                 等太久只是白占着口
        返回: True=已连上（收数线程已在跑）；False=没认出来（已上报 error 状态）
        副作用: 认领成功后启动 _rx_thread、清停止标志、上报 connected 状态
        """
        if serial is None:
            self.on_status(Status.error, "未安装 pyserial，请 pip install pyserial")
            return False
        if self.connected:
            return True

        candidates = self.find_candidates()
        if not candidates:
            self.on_status(Status.error, "未发现 VEX 串口设备（VID 0x2888）")
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

        self.on_status(
            Status.error,
            f"未能识别 V5 User 口（仅试了 VEX 标识的 {len(candidates)} 个口）: {last_err}",
        )
        return False

    # 签名注解必须加引号：函数签名（参数/返回）的注解在 def 执行时就求值，
    # 而函数体内的注解不求值。pyserial 未装时 serial 为 None，
    # 不加引号这里会 AttributeError: 'NoneType' object has no attribute 'Serial'。
    def _probe(self, ser: "serial.Serial", timeout: float = PROBE_TIMEOUT_S) -> bool:
        """发 Ping，在 timeout 内找到完整 Pong(A5 FF FF) 才算这个口对。

        ser    : 已打开、待验证的串口对象
        timeout: 等 Pong 的时长（秒），默认 120ms；每 30ms 补发一次 Ping 防首帧丢在枚举窗口
        返回: True=认领成功（这是 V5 User 口）；False=该口无应答
        """
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
        next_ping = time.monotonic() + 0.03
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
            # 每30ms补发一次Ping，防止首帧丢在USB枚举窗口里
            # （刚open时设备可能还没准备好接收，第一帧会石沉大海）
            now = time.monotonic()
            if now >= next_ping:
                try:
                    ser.write(frame)
                except Exception:
                    pass
                next_ping = now + 0.03
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
        """写一帧到串口（加写锁串行化）。

        data: 完整帧字节（含帧头与校验，由 protocol.build_frame 组好）
        抛错: 未连接时 RuntimeError("串口未连接")，由 Hub 的 tx 线程捕获处理
        """
        if not self.connected or not self._ser:
            raise RuntimeError("串口未连接")
        with self._tx_lock:
            self._ser.write(data)

    def _rx_loop(self) -> None:
        """收数线程主循环：轮询 in_waiting，有字节就一次读走并分给两个消费者。

        每批字节先 on_raw 原样广播给 WS 上位机（前端自己拆帧），
        再喂 Python FrameParser（供脚本侧目录/快照/日志）。读异常即判定掉线 _lost。
        """
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
                        self._parser.feed(chunk, self._on_frame)
                else:
                    # 没数据时短睡：2ms ≈ 500Hz 轮询，够快又不烧 CPU
                    time.sleep(0.002)
            except Exception as e:
                if not self._stop.is_set():
                    logger.error("串口读异常: %s", e)
                    self._lost(f"串口读取异常: {e}")
                break

    def _on_frame(self, cmd: int, payload: bytes) -> None:
        """FrameParser 解出一完整帧就调：截获 Pong 更新时间戳，再转给上层。

        cmd/payload: 帧的命令字与 payload（不含帧头/命令字/校验）
        上层回调抛异常只记日志、不上抛——否则会把 rx 线程带崩，整条链路静默死亡。
        """
        # Pong 在这里被"截获"更新时间戳，同时也继续往下传给上层
        if cmd == CmdPost.Pong:
            self._last_pong = time.monotonic()
        try:
            self.on_frame(cmd, payload)
        except Exception as e:
            # 上层回调抛异常不能把 rx 线程带崩，否则整个链路静默死亡
            logger.exception("on_frame 异常: %s", e)

    # ---------- 心跳 ----------

    # 心跳 80ms 一发，超过 300ms 没收到 Pong 判定下位机程序已死（USB 还连着但链路没意义）。
    # 300ms = 固件断链巡视周期（communication.h LINK_PATROL_TICKS=30，300ms 无 Ping 停推），
    # 与 FW_LAG_STALE_MS 同尺：链路冻结时 lag 前沿也冻、但 read 侧按 PC 时钟外推照常报旧，
    # 本判定是同一事件的第二见证（掉线事件本身）。
    # 容忍约 3 个 Pong 丢失；USB CDC 往返亚毫秒级，rx 线程无同步阻塞
    # （WS 广播是 run_coroutine_threadsafe 异步投递，网页慢拖不到这里），
    # 300ms 仍有约 300 倍余量；误判代价也只是走一遍重连，可恢复。
    PONG_TIMEOUT_S = 0.3

    def stop_heartbeat(self) -> None:
        """停心跳线程并等它退出；从心跳线程自己调 _lost 进来时不 join（等自己无意义）。"""
        self._hb_stop.set()
        t = self._hb_thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=1.5)
        self._hb_thread = None

    def start_heartbeat(self, interval: float = 0.08) -> threading.Thread:
        """单例心跳：重复调用先停旧线程再启，避免重连后叠出多个 Ping 源。

        interval: 发 Ping 的间隔（秒），默认 80ms（主控 300ms 收不到即断链收车）
        返回: 刚启动的心跳线程
        线程内每拍发一次 Ping，并检查 last_pong——超过 PONG_TIMEOUT_S 未回则判定掉线。
        """
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
                    logger.warning("超过 %.1fs 未收到 Pong，判定下位机掉线", self.PONG_TIMEOUT_S)
                    self._lost("心跳超时，下位机无响应")
                    break

        t = threading.Thread(target=_loop, name="vex-heartbeat", daemon=True)
        self._hb_thread = t
        t.start()
        return t
