"""
统一传输桥：串口 / WiFi(UDP) / WS 三条链路并发探测，先完成握手者胜。

为什么合并（2026-11-07）：
    串口 / UDP / WS 三条链路里，真正与介质有关的只有
    “字节从哪进出设备”这一层；FrameParser 喂帧、Pong 时间戳、心跳 80ms、
    on_raw 原样广播、写锁全是共享的。选路本身不收发，只做并发探测 +
    先握手者胜 + 选中者失联回三探测。以前拆成“基类 + 继承 + 组合”，只是把
    同一套状态分散在三处；合并后分支集中在 _connect_* / _send_* / _lost_* 几个方法。

运行方式（没有 mode 开关）：
    每条链路各自探测，谁先完成 Ping/Pong 握手谁被选中（串口探测快，两端都在线
    时偏向串口，刻意接受）。掉线后先定向重试“上次的对端”（串口记下的口 /
    UDP 记下的地址），定向预算用尽再轮流探测。三条链路互斥：一条被选中时，
    另外两条释放句柄 / 端口，不占资源。每轮探测持有一个取消标志：重启只
    set 旧标志 + 立刻关闭旧轮句柄，监督者不 join，旧线程醒来只能关到自己那轮。

三条链路（MODE_SERIAL / MODE_UDP / MODE_WS 只是链路标识）：
    串口    V5 User 口。V5 两个 USB 口（下载口 / User 口）VID/PID 相同，操作系统
            无法区分；只有 User 口跑用户程序、收到 Ping 会回 Pong(A5 FF FF)。
            所以逐候选口发 Ping，谁回 Pong 谁就是 User 口。只碰带 VEX 标识的口
            （VID 0x2888 或描述符含 vex/user）——打开串口即独占，乱开会打断烧录。
    UDP     WiFi 下位机。bind 后靠上行帧发现候选 peer；只收到上行只证明单向可达，
            必须“我们发过 Ping 后收到 Pong”才锁定并允许下行（反向可能被 NAT/
            防火墙挡住）。失联只清 peer，socket 与 rx 线程保持，对外仍报 connected。
    WS      bridge 自己监听一个 TCP 端口（默认 8889），外部设备 / 调试进程主动连
            进来；连上后同样要回 Pong 才算握手成功。debug/mock_ws_source.py 就是
            借这条链路冒充真 V5，真设备也可以走它。监听不属于任何 mode：探测期
            常开，落败即释放端口。

对 Hub 的契约不变（10 个鸭子接口）：
    connect / disconnect / connected / send / start_heartbeat / stop_heartbeat /
    list_ports + on_frame / on_status / on_raw；另有两个可选钩子 on_locked
    （握手成功/选路成功，Hub 据此重拉目录）与 on_device_name（0xFE 登记名）。

单 client：任意时刻只有得胜链路的字节喂 Hub（未选中链路的 on_raw/on_frame
一律丢弃），Hub 的目录缓存 / 快照 / 待确认表仍是单份。多设备留待后续。
"""

import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection, serve

from .config import CONFIG
from .protocol import CmdPost, FrameParser, decode_discovery, ping

logger = logging.getLogger("vex.bridge")


@dataclass
class PortInfo:
    """一个串口设备的信息（pyserial 原始描述符的裁剪版，/api/ports 直接返回）。"""
    device: str                             # 设备路径：COM3 / udp://host:port / ws://host:port
    description: str                        # pyserial 描述或链路说明，可为空串
    vid: int | None                         # USB VID；UDP/WS 链路为 None
    pid: int | None                         # USB PID；UDP/WS 链路为 None
    serial_number: str                      # 串口序列号；UDP 为对端地址、WS 为对端 addr，可为空串
    location: str = ""
    '''USB 物理口路径（pyserial 的 location）；复认串口用，可能为空'''


class Status(Enum):
    """连接状态。值就是推给前端的字符串（前端按这些值决定指示灯颜色）。

    idle         未连接（含用户主动断开）
    connecting   正在探测/打开串口，或等 WS 设备接入
    connected    已连上（串口 rx 在跑 / UDP socket 在听 / WS 设备在线）
    disconnected 意外掉线（拔线/读异常/心跳超时）→ Hub 据此触发自动重连
    error        连不上（没装 pyserial / 没设备 / 认不出 V5 User 口）
    """
    idle = "idle"
    connecting = "connecting"
    connected = "connected"
    disconnected = "disconnected"
    error = "error"


import serial
import serial.tools.list_ports

# VEX Robotics 的 USB VID；描述符匹配不到时用它优先（真正判定仍靠 Ping 应答）
VEX_VID = 0x2888

DEFAULT_UDP_PORT = 8888
'''默认 UDP 监听端口，与 Hi3861 侧 SERVER_PORT 一致'''

DEFAULT_WS_PORT = 8889
'''默认 WS 监听端口：避开后端 / 前端通道的 8000 与 UDP 的 8888'''

# 链路标识（也是 _active 的取值），不再是可单独选择的 mode
MODE_SERIAL = "serial"
MODE_UDP = "udp"
MODE_WS = "ws"

# 探测 / 心跳 / 掉线 / UDP 收发的时间与上限都在 app/config.json，
# 代码里直接读 CONFIG.<段>.<字段>。


class _ProbeRound:
    """一轮探测的全部资源与取消标志。

    谁打开谁关闭：探测线程把句柄挂在本对象上，收尾统一调 _close_round(rnd)；
    监督者重启时也调同一个收尾（幂等）。旧线程晚退只能关到自己这轮的句柄，
    碰不到下一轮，所以监督者不需要 join。
    """
    __slots__ = (
        "cancel", "ser", "ser_stop", "ser_rx_thread", "sock", "udp_stop",
        "udp_thread", "ws_server", "ws_srv_thread", "ws_stop", "ws_conn",
    )

    def __init__(self) -> None:
        self.cancel = threading.Event()
        '''本轮取消标志：set 即这一轮作废'''
        self.ser: serial.Serial | None = None
        '''本轮打开并认领的串口'''
        self.ser_stop = threading.Event()
        '''串口收数线程停止标志'''
        self.ser_rx_thread: threading.Thread | None = None
        '''串口收数线程'''
        self.sock: socket.socket | None = None
        '''本轮 bind 的 UDP socket'''
        self.udp_stop = threading.Event()
        '''UDP 收数线程停止标志'''
        self.udp_thread: threading.Thread | None = None
        '''UDP 收数线程'''
        self.ws_server: Server | None = None
        '''本轮持有的 WS 监听'''
        self.ws_srv_thread: threading.Thread | None = None
        '''WS 监听循环线程'''
        self.ws_stop = threading.Event()
        '''WS 链路停止标志（关监听 + 关连接）'''
        self.ws_conn: ServerConnection | None = None
        '''本轮接入的 WS 连接'''


class TransportBridge:
    """线程安全的设备层：一个实例同时装下串口 / UDP / WS 三条链路。

    三个回调让本类与上层解耦（Hub 在构造时注入）：
        on_raw(chunk)          每收到一批原始字节就调（只转发选中链路），
                               Hub 原样广播给 WebSocket
        on_frame(cmd, payload) FrameParser 解出一完整帧就调（只转发选中链路）
        on_status(state,msg)   连接状态变化（state 是 Status 枚举）

    状态分块：串口 / UDP / WS 各一组，另有仲裁字段；心跳只有一条线程，
    但按“本次心跳属于哪条链路”（_hb_link）取对应的 Pong 时间戳与超时值。
    """

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        on_frame: Callable[[int, bytes], None] | None = None,
        on_status: Callable[[Status, str], None] | None = None,
        on_raw: Callable[[bytes], None] | None = None,
        *,
        bind_host: str = "0.0.0.0",
        bind_port: int = DEFAULT_UDP_PORT,
        ws_host: str = "0.0.0.0",
        ws_port: int = DEFAULT_WS_PORT,
    ) -> None:
        """建桥（不立即打开设备，connect() 才探测/ bind）。

        port      : 指定串口名（如 "COM5"）；None=串口 connect 时自动逐口 Ping 探测
        baud      : 波特率，CDC 虚拟口仅占位（USB 全速跑，实际不影响）
        on_frame  : 完整帧回调 (cmd, payload)
        on_status : 连接状态变化回调，收到 Status 枚举 + 人读说明
        on_raw    : 原始字节回调，用来原样广播给 WS 上位机
        bind_host / bind_port : UDP 监听地址
        ws_host / ws_port     : WS 监听地址
        """
        self.port_name = port
        '''指定串口名；None=自动探测'''
        self.baud = baud
        '''波特率（CDC 虚拟口仅占位）'''
        self.bind_host = bind_host
        '''UDP 监听地址'''
        self.bind_port = bind_port
        '''UDP 监听端口'''
        self.ws_host = ws_host
        '''WS 监听地址'''
        self.ws_port = ws_port
        '''WS 监听端口'''

        # 不给回调就塞空 lambda，省得调用处到处判 None
        self.on_frame = on_frame or (lambda cmd, payload: None)
        '''完整帧回调 (cmd, payload)：FrameParser 解出的一帧，语义归 Hub'''
        self.on_status = on_status or (lambda s, m: None)
        '''状态回调，连接状态变化'''
        self.on_raw = on_raw or (lambda data: None)
        '''原始字节回调，Hub 用它原样广播给 WebSocket'''

        self.on_locked: Callable[[], None] | None = None
        '''握手成功 / 选路成功后的回调（Hub 注入，重拉目录+补订阅）'''
        self.on_device_name: Callable[[str | None], None] | None = None
        '''当前 peer 设备名变化回调（Hub 注入）；None=未登记/已清除'''

        # 写锁：串行化写出，兼管 UDP 数据报的最小发送间隔
        self._tx_lock = threading.Lock()
        '''写锁：串行化写出；兼管 UDP 数据报最小间隔'''
        self._last_write_at = 0.0
        '''最近一次真正写出的 time.monotonic() 秒；0.0=还没写过（首帧不等待）'''

        # ---------- UDP 下行攒包 ----------
        # 帧进缓冲 → 窗口到期（或触上限）→ 打包 → 写链路。
        self._tx_cv = threading.Condition()
        '''攒包缓冲的锁，兼作攒包线程的唤醒条件'''
        self._tx_buf: list[tuple[bytes, bool]] = []
        '''待并包帧 [(帧, 是否需回执), ...]，按到达顺序'''
        self._tx_urgent: list[bytes] = []
        '''停车帧队列，不参与窗口'''
        self._tx_deadline = 0.0
        '''当前窗口的到期时刻（monotonic 秒）；0.0=没开窗或该立刻发'''
        self._tx_stop = threading.Event()
        '''攒包线程停止标志'''
        self._tx_thread: threading.Thread | None = None
        '''攒包线程'''

        # 每条链路一份解析器。探测期三条链路可能同时在跑，共用一份会把
        # 两路的半帧字节拼在一起，帧边界直接坏掉。
        self._ser_parser = FrameParser()
        self._udp_parser = FrameParser()

        # ---------- 串口链路 ----------
        self._ser: serial.Serial | None = None
        '''当前选中并已认领的串口；探测期不开'''

        # ---------- UDP 链路 ----------
        self._sock: socket.socket | None = None
        '''当前这一轮 bind 的 UDP socket；探测期就要能发 Ping'''
        self._peer: tuple[str, int] | None = None
        '''当前对端（候选或已锁定）的 (ip, port)；None=还没见过任何上行包'''
        self._peer_locked = False
        '''是否已收到该 peer 的 Pong 并完成锁定'''
        self._peer_since = 0.0
        '''当前候选 peer 的起始时刻，用于候选超时换人'''
        self._ping_sent = False
        '''是否已向当前 peer 发出过 Ping。只有“我们发了 Ping 才收到的 Pong”能证明
           双向可达——板子主动送上门的 Pong 不算'''
        self._strangers: set[tuple[str, int]] = set()
        '''已告警过的陌生源地址，避免每包都打日志'''
        self._device_names: dict[tuple[str, int], str] = {}
        '''源地址 → 设备名（0xFE 登记帧带来；按地址保存，不参与锁定判定）'''

        # ---------- WS 链路 ----------
        self._ws_parser = FrameParser()
        '''WS 链路独立解析器'''
        self._ws: ServerConnection | None = None
        '''已接入的 WS 设备连接；None=没设备'''
        self._ws_server: Server | None = None
        '''当前这一轮的 WS 监听器；落败 / 断开时由本轮收尾关闭'''
        self._ws_locked = False
        '''当前 WS 连接是否已回 Pong 完成握手'''

        # ---------- 选路仲裁 ----------
        self._active: str | None = None
        '''当前选中的链路（serial / udp / ws）；None=还在探测 / 已失联等待重新选路'''
        self._auto_stop = threading.Event()
        '''选路彻底停止（Hub.stop 或用户主动断开）；与各链路的停止标志分开'''
        self._claim_lock = threading.Lock()
        '''保护 _active 的一次性抢占与 _round 切换'''
        self._round: _ProbeRound | None = None
        '''当前探测轮（含取消标志与本轮句柄）；None=没有在探测'''
        self._last_serial: PortInfo | None = None
        '''上次成功握手的串口身份（定向重试用，只在内存）'''
        self._last_peer: tuple[str, int] | None = None
        '''上次成功握手的 UDP 对端地址（定向重试用，只在内存）'''

        # ---------- 心跳（所有链路共用一条线程） ----------
        self._hb_thread: threading.Thread | None = None
        '''心跳线程；单例：重连叠调 start_heartbeat 时先停旧线程，避免多路 Ping 打架'''
        self._hb_stop = threading.Event()
        '''心跳线程停止标志'''
        self._hb_link: str | None = None
        '''当前心跳线程归属的链路；send/lost 据此走对应分支'''
        self._hb_interval = CONFIG.heartbeat_gap_s
        '''记下的心跳间隔（串口赢得选路时用）'''
        self._last_pong: dict[str, float] = {
            MODE_SERIAL: 0.0, MODE_UDP: 0.0, MODE_WS: 0.0,
        }
        '''各链路最近一次 Pong 的 monotonic 时刻；心跳按 _hb_link 取，避免三条链路
           在探测期短暂共存时互相顶掉时间戳'''

    # ---------- 对外状态 ----------

    @property
    def connected(self) -> bool:
        """当前是否真正连着（看选中链路；探测中为 False）。"""
        if self._active == MODE_SERIAL:
            return self._serial_ready()
        if self._active == MODE_UDP:
            return self._udp_ready()
        if self._active == MODE_WS:
            return self._ws_ready()
        return False

    def _serial_ready(self) -> bool:
        # 只判标志位会在拔线后仍返回 True，所以必须看端口是否真开着
        return self._ser is not None and self._ser.is_open

    def _udp_ready(self) -> bool:
        # UDP 无连接：bind 成功（socket 在）就算通道就绪，不等 peer 锁定
        return self._sock is not None

    def _ws_ready(self) -> bool:
        # 有设备接进来才算连上；监听在不在由 _ws_server 另外表示
        return self._ws is not None

    @property
    def peer(self) -> tuple[str, int] | None:
        """当前对端；None=尚未发现。诊断显示用。"""
        return self._peer

    @property
    def device_name(self) -> str | None:
        """当前 peer 的设备登记名；没发登记帧时为 None。"""
        if self._peer is None:
            return None
        return self._device_names.get(self._peer)

    @property
    def transport(self) -> str | None:
        """当前链路，供 /api/status 诊断：serial / udp / ws / probing / idle。"""
        if self._active is not None:
            return self._active
        return "idle" if self._auto_stop.is_set() else "probing"

    # ---------- 端口枚举 ----------

    @staticmethod
    def _scan_serial_ports() -> list[PortInfo]:
        """枚举本机串口设备（pyserial 描述符裁剪成 PortInfo）。"""
        out = []
        for p in serial.tools.list_ports.comports():
            out.append(
                PortInfo(
                    device=p.device,
                    description=p.description or "",
                    vid=p.vid,
                    pid=p.pid,
                    serial_number=p.serial_number or "",
                    location=p.location or "",
                )
            )
        return out

    def list_ports(self) -> list[PortInfo]:
        """给 /api/ports 的设备描述：串口候选 + UDP 监听项 + WS 监听项。"""
        out = self._scan_serial_ports()
        out.append(self._udp_port_info())
        out.append(self._ws_port_info())
        return out

    def _udp_port_info(self) -> PortInfo:
        """UDP 通道没有串口可枚举，返回一个描述项供 /api/ports 显示。"""
        return PortInfo(
            device=f"udp://{self.bind_host}:{self.bind_port}",
            description="UDP 监听（WiFi 下位机，对端自动发现）",
            vid=None,
            pid=None,
            serial_number=self._peer[0] if self._peer else "",
        )

    def _ws_port_info(self) -> PortInfo:
        """WS 通道同样没有串口可枚举，返回监听地址供 /api/ports 显示。"""
        addr = self._ws.remote_address if self._ws is not None else None
        return PortInfo(
            device=f"ws://{self.ws_host}:{self.ws_port}",
            description="WS 监听（外部设备 / debug/mock_ws_source.py 主动接入）",
            vid=None,
            pid=None,
            serial_number=f"{addr[0]}:{addr[1]}" if addr else "",
        )

    def _serial_candidates(self) -> list[PortInfo]:
        """待探测的串口候选：只含 VEX 标识的口（VID / 描述符认得出）。

        指定了 self.port_name 时只返回它（跳过自动探测）；没有 VEX 口时返回空——
        非 VEX 串口一律不打开（打开即独占）。
        """
        if self.port_name:
            for p in self._scan_serial_ports():
                if p.device == self.port_name:
                    return [p]
            return [PortInfo(self.port_name, "", None, None, "")]
        out = []
        for p in self._scan_serial_ports():
            desc = (p.description or "").lower()
            if p.vid == VEX_VID or "vex" in desc or "user" in desc:
                out.append(p)
        return out

    @staticmethod
    def _match_serial(info: PortInfo, ports: list[PortInfo]) -> PortInfo | None:
        """按上次身份在当前候选里复认：序列号 > 物理口 > 设备名。"""
        if info.serial_number:
            for p in ports:
                if p.serial_number and p.serial_number == info.serial_number:
                    return p
        if info.location:
            for p in ports:
                if p.location == info.location:
                    return p
        for p in ports:
            if p.device == info.device:
                return p
        return None

    # ---------- 连接 ----------

    def connect(self) -> bool:
        """建链：幂等地拉起一轮探测（返回 True 只代表探测已在跑）。"""
        if not self._auto_stop.is_set() and self._active is not None:
            return True
        self._auto_stop.clear()
        self._start_tx_thread()
        self._start_probe()
        return True

    def disconnect(self) -> None:
        """主动断开：停心跳 → 取消本轮探测并关闭本轮句柄 → 停出队线程 → 报 idle。"""
        self._auto_stop.set()
        with self._claim_lock:
            self._active = None
            rnd = self._round
            self._round = None
        self.stop_heartbeat()
        self._stop_tx_thread()
        self._close_round(rnd)
        self.on_status(Status.idle, "已断开")

    # ---------- 串口链路 ----------

    @staticmethod
    def _open_serial(dev: str, baud: int) -> "serial.Serial | None":
        """打开一个候选口；打不开（被占用 / 已拔掉）返回 None。"""
        try:
            return serial.Serial(
                port=dev,
                baudrate=baud,
                timeout=0.05,  # 读超时短：收数线程靠 in_waiting 轮询，不需要长阻塞
                write_timeout=0.5,
            )
        except Exception as e:
            logger.warning("打开 %s 失败: %s", dev, e)
            return None

    # 签名注解必须加引号：函数签名在 def 执行时就求值，pyserial 未装时 serial 为 None，
    # 不加引号会 AttributeError: 'NoneType' object has no attribute 'Serial'。
    def _probe(
        self, ser: "serial.Serial", cancel: threading.Event, timeout: float | None = None
    ) -> bool:
        """发 Ping，在 timeout 内找到完整 Pong(A5 FF FF) 才算这个口对。

        每 30ms 补发一次 Ping，防止首帧丢在 USB 枚举窗口里；每次补发前看本轮取消标志。
        """
        if timeout is None:
            timeout = CONFIG.find_timeout_s
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
            if cancel.is_set() or self._auto_stop.is_set():
                return False
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
                        if acc[i] == CONFIG.frame_head and acc[i + 1] == 0xFF and acc[i + 2] == 0xFF:
                            return True
            except Exception:
                return False
            time.sleep(0.01)
            now = time.monotonic()
            if now >= next_ping:
                try:
                    ser.write(frame)
                except Exception:
                    pass
                next_ping = now + 0.03
        return False

    def _lost_serial(self, message: str) -> None:
        """意外掉线（拔线/读异常/心跳超时）：关口并发 disconnected，由 Hub 决定重连。"""
        ser = self._ser
        if ser is None:
            return  # 已经断过了，不重复上报
        if self._hb_link == MODE_SERIAL:
            self.stop_heartbeat()
        rnd = self._round
        if rnd is not None and rnd.ser is ser:
            rnd.ser_stop.set()
            rnd.ser = None
        self._ser = None
        self._last_pong[MODE_SERIAL] = 0.0
        try:
            ser.close()
        except Exception:
            pass
        self._status(MODE_SERIAL, Status.disconnected, message)

    def _send_serial(self, data: bytes) -> bool:
        """写一帧到串口（调用方持 _tx_lock）；未连接抛 RuntimeError。返回是否真的写出。"""
        if not self._serial_ready() or self._ser is None:
            raise RuntimeError("串口未连接")
        self._ser.write(data)
        return True

    def _serial_rx_loop(self, rnd: _ProbeRound) -> None:
        """串口收数线程：轮询 in_waiting，有字节就一次读走并交给共享入口 _ingest。"""
        ser = rnd.ser
        while not rnd.ser_stop.is_set():
            try:
                n = ser.in_waiting
                if n:
                    # 一次把已到的字节全读走：目录 burst 约 4KB，小缓冲会丢帧
                    chunk = ser.read(n)
                    if chunk:
                        self._ingest(MODE_SERIAL, chunk, rnd=rnd)
                else:
                    # 没数据时短睡：2ms ≈ 500Hz 轮询，够快又不烧 CPU
                    time.sleep(0.002)
            except Exception as e:
                if not rnd.ser_stop.is_set():
                    logger.error("串口读异常: %s", e)
                    self._lost_serial(f"串口读取异常: {e}")
                break


    # ---------- UDP 链路 ----------

    def _connect_udp(self, rnd: _ProbeRound) -> bool:
        """建 socket 并起收数线程。UDP 无握手，bind 成功即返回 True。"""
        if rnd.sock is not None:
            return True

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Windows：对端端口不可达时 ICMP 会让下次 recvfrom 抛 WSAECONNRESET(10054)。
            # 板子重启/断电期间我们仍在发 Ping，极易触发——UDP 本就该容忍对端暂时消失。
            if hasattr(socket, "SIO_UDP_CONNRESET"):
                try:
                    sock.ioctl(socket.SIO_UDP_CONNRESET, False)
                except OSError:
                    pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, CONFIG.buffer_bytes)
            except OSError:
                pass  # 有的平台不接受这么大，用默认值也能跑
            # 读超时：让 recvfrom 周期返回，rx 线程才能及时响应停止标志
            sock.settimeout(0.5)
            sock.bind((self.bind_host, self.bind_port))
        except OSError as e:
            self._status(MODE_UDP, Status.error, f"UDP 端口 {self.bind_port} 绑定失败: {e}")
            return False

        rnd.sock = sock
        self._sock = sock
        self._reset_peer()
        rnd.udp_stop.clear()
        rnd.udp_thread = threading.Thread(
            target=self._udp_rx_loop, args=(rnd,), name="vex-udp-rx", daemon=True
        )
        rnd.udp_thread.start()
        self._status(
            MODE_UDP,
            Status.connected,
            f"UDP 监听 {self.bind_host}:{self.bind_port}，等待下位机上行帧…",
        )
        logger.info("UDP 监听 %s:%d", self.bind_host, self.bind_port)
        return True

    def _lost_udp(self, message: str) -> None:
        """失联：只清 peer 回到发现态，由仲裁层换人；不在这里起心跳。"""
        logger.warning("UDP 链路失联，回到发现态：%s", message)
        self._reset_peer()
        if self._active == MODE_UDP:
            self._on_active_lost(already_reported=False)

    def _reset_peer(self) -> None:
        """清空对端状态，回到“等第一个上行包”。设备名缓存不清（按地址保留）。"""
        self._peer = None
        self._peer_locked = False
        self._peer_since = 0.0
        self._ping_sent = False
        self._last_pong[MODE_UDP] = 0.0

    def _send_udp(self, data: bytes) -> bool:
        """发一帧给当前 peer（候选或已锁定）；没有 peer 时静默返回 False。
        调用方持 _tx_lock。"""
        peer = self._peer
        sock = self._sock
        if peer is None or sock is None:
            logger.debug("UDP 未发现对端，丢弃 %d 字节", len(data))
            return False
        try:
            sock.sendto(data, peer)
        except OSError as e:
            logger.warning("UDP 发送失败: %s", e)
            return False
        # 只有“我们主动发出的 Ping”才能当作双向可达的凭据；板子自己送上门的
        # Pong 不算（反向可能根本不通）。心跳每拍 send(ping())，按内容识别。
        if data == ping():
            self._ping_sent = True
        return True

    def _udp_rx_loop(self, rnd: _ProbeRound) -> None:
        """收数线程：recvfrom 循环，按 peer 过滤后交给共享入口 _ingest。

        过滤规则（单 client）：
            未锁定 → 第一个源地址成为候选；候选超时未回 Pong 就换下一个源地址
            已锁定 → 只认该 peer，其他源地址丢弃并告警一次
        """
        sock = rnd.sock
        while not rnd.udp_stop.is_set():
            try:
                data, addr = sock.recvfrom(CONFIG.max_packet_bytes)
            except socket.timeout:
                continue  # 让停止标志有机会生效
            except OSError as e:
                if rnd.udp_stop.is_set():
                    break
                # Windows 上 sendto 到已消失的对端会收到 WSAECONNRESET(10054)，
                # 那只是“上次发送的目标不可达”的迟到通知，socket 本身没坏。
                # UDP 无连接，不能因为它拆掉整条链路；由心跳超时去判失联并软重置。
                logger.debug("UDP recv 异常（忽略继续收包）: %s", e)
                time.sleep(0.05)
                continue

            if not data:
                continue
            if not self._accept(addr):
                continue
            self._ingest(MODE_UDP, data, addr, rnd=rnd)


    def _accept(self, addr: tuple[str, int]) -> bool:
        """这个源地址的包要不要处理；顺带推进候选/锁定的选取。"""
        if self._peer is None:
            self._peer = addr
            self._peer_since = time.monotonic()
            logger.info("候选对端 %s:%d（等 Pong 确认双向可达）", addr[0], addr[1])
            return True

        if self._peer_locked:
            if addr == self._peer:
                return True
            if addr not in self._strangers:
                self._strangers.add(addr)
                logger.warning(
                    "忽略非锁定对端 %s:%d（已锁定 %s:%d，单 client 只认一个）",
                    addr[0], addr[1], self._peer[0], self._peer[1],
                )
            return False

        # 候选态：超时没等来 Pong 就换人，免得卡在一个不通的设备上
        if (
            time.monotonic() - self._peer_since > CONFIG.peer_switch_timeout_s
            and addr != self._peer
        ):
            logger.warning(
                "候选 %s:%d 超时未回 Pong，改用 %s:%d",
                self._peer[0], self._peer[1], addr[0], addr[1],
            )
            self._peer = addr
            self._peer_since = time.monotonic()
            self._last_pong[MODE_UDP] = 0.0
            self._ping_sent = False
        return addr == self._peer

    def _on_discovery(self, payload: bytes, addr: tuple[str, int] | None) -> None:
        """0xFE 发现/登记帧：只更新“帧来源地址 → 设备名”，不参与候选/锁定判定。

        addr 由 _ingest 从 recvfrom 一路带下来，多地址并发时不会串名；
        名长/魔数不符由 decode_discovery 返回 None，这里告警后丢弃，
        绝不抛异常打断收包线程。打印只在内容变化时发生：地址首次出现、
        设备名变了、同名设备换了地址；同一台设备重复广播完全静默。
        """
        parsed = decode_discovery(payload)
        if parsed is None:
            logger.warning("丢弃非法发现帧 payload=%r", payload[:24])
            return

        clear, name = parsed
        if addr is None:
            addr = self._peer
        if addr is None:
            return

        if clear:
            if self._device_names.pop(addr, None) is not None:
                logger.info("设备登记已清除 %s:%d", addr[0], addr[1])
                self._notify_device_name(None)
            return

        known = addr in self._device_names
        old = self._device_names.get(addr)
        if known and (name is None or name == old):
            return

        # 同名已在别的地址登记过 = 同一台设备换了地址
        moved = None
        if name is not None and not known:
            for a, n in self._device_names.items():
                if n == name:
                    moved = a
                    break

        self._device_names[addr] = name
        if moved is not None:
            logger.info("设备换地址 %s:%d → %s:%d（%s）",
                        moved[0], moved[1], addr[0], addr[1], name)
        elif known:
            logger.info("设备改名 %s:%d %s → %s", addr[0], addr[1], old or "(未登记)", name)
        else:
            logger.info("发现设备 %s:%d 名称=%s", addr[0], addr[1], name or "(未登记)")
        if name is not None:
            self._notify_device_name(name)

    def _notify_device_name(self, name: str | None) -> None:
        """把设备名变化交给 Hub；回调异常不能带崩收包线程。"""
        if self.on_device_name is None:
            return
        try:
            self.on_device_name(name)
        except Exception:
            logger.exception("on_device_name 回调异常")

    # ---------- WS 链路 ----------
    #
    # 监听由 bridge 自己持有：serve_forever 跑在独立线程，探测期常开；别的链路
    # 一旦被选中，本轮收尾关监听释放端口，回探测时再 bind 回来。WS 是设备主动
    # 连进来，没法“定向”，只能等。

    def _connect_ws(self, rnd: _ProbeRound) -> bool:
        """bind WS 端口并起监听线程；返回 True 只代表监听已起。"""
        if rnd.ws_server is not None:
            return True
        try:
            server = serve(
                lambda conn: self._ws_handler(conn, rnd),
                self.ws_host,
                self.ws_port,
                compression=None,
                ping_interval=None,
                close_timeout=0.5,
            )
        except OSError as e:
            self._status(MODE_WS, Status.error, f"WS 端口 {self.ws_port} 监听失败: {e}")
            return False

        rnd.ws_server = server
        self._ws_server = server
        rnd.ws_stop.clear()
        self._ws_locked = False
        rnd.ws_srv_thread = threading.Thread(
            target=server.serve_forever, name="vex-ws-serve", daemon=True
        )
        rnd.ws_srv_thread.start()
        self._status(
            MODE_WS,
            Status.connecting,
            f"WS 监听 {self.ws_host}:{self.ws_port}，等待设备接入…",
        )
        logger.info("WS 监听 %s:%d", self.ws_host, self.ws_port)
        return True

    def _ws_handler(self, conn: ServerConnection, rnd: _ProbeRound) -> None:
        """一个设备连接一个线程：拒第二台 → 起探测 Ping 等 Pong → 持续收消息。"""
        with self._claim_lock:
            busy = (
                rnd.cancel.is_set()
                or self._auto_stop.is_set()
                or self._round is not rnd
                or self._active is not None
                or self._ws is not None
            )
            if not busy:
                rnd.ws_conn = conn
                self._ws = conn
                self._ws_locked = False
        if busy:
            try:
                conn.close(code=1008, reason="已有设备接入")
            except Exception:
                pass
            return

        self._ws_parser = FrameParser()
        self._last_pong[MODE_WS] = 0.0
        addr = conn.remote_address
        label = f"{addr[0]}:{addr[1]}" if addr else "未知地址"
        self._status(MODE_WS, Status.connecting, f"WS 设备已接入 {label}，等待 Ping/Pong 握手")

        # 探测期由本线程发 Ping（不另起心跳线程）；握手成功后由 _claim 起正式心跳。
        deadline = time.monotonic() + CONFIG.peer_switch_timeout_s
        try:
            while not rnd.ws_stop.is_set() and self._ws is conn:
                if not self._ws_locked:
                    if time.monotonic() > deadline:
                        logger.warning("WS 设备 %s 握手超时", label)
                        break
                    try:
                        self._send_link(MODE_WS, ping())
                    except Exception as e:
                        logger.warning("WS 探测 Ping 发送失败: %s", e)
                        break
                try:
                    msg = conn.recv(timeout=CONFIG.poll_gap_s)
                except TimeoutError:
                    continue
                if isinstance(msg, str):
                    continue  # 设备只说二进制协议；文本帧丢弃
                if msg:
                    self._ingest(MODE_WS, msg, rnd=rnd)
        except ConnectionClosed:
            pass
        except Exception as e:
            logger.warning("WS 收数异常: %s", e)
        finally:
            if self._ws is conn:
                self._lost_ws("WS 设备断开")
            try:
                conn.close()
            except Exception:
                pass

    def _send_ws(self, data: bytes) -> bool:
        """写一帧给已接入的设备（调用方持 _tx_lock）。返回是否真的写出。"""
        ws = self._ws
        if ws is None:
            raise RuntimeError("WS 设备未接入")
        ws.send(bytes(data))
        return True

    def _lost_ws(self, message: str) -> None:
        """设备断开/心跳超时：停心跳、关连接、报 disconnected；监听留着等下一条。"""
        conn = self._ws
        if conn is None:
            return
        if self._hb_link == MODE_WS:
            self.stop_heartbeat()
        rnd = self._round
        if rnd is not None and rnd.ws_conn is conn:
            rnd.ws_conn = None
        self._ws = None
        self._ws_locked = False
        self._last_pong[MODE_WS] = 0.0
        try:
            conn.close()
        except Exception:
            pass
        self._status(MODE_WS, Status.disconnected, message)
        if self._active == MODE_WS:
            self._on_active_lost(already_reported=True)


    # ---------- 收发共享入口 ----------

    def _ingest(
        self,
        link: str,
        chunk: bytes,
        addr: tuple[str, int] | None = None,
        rnd: _ProbeRound | None = None,
    ) -> None:
        """一批字节：原样广播（只转发选中链路）+ 喂该链路自己的解析器。

        addr 是 UDP 的来源地址，随帧一起交给 _on_discovery 登记设备名；
        串口 / WS 无来源地址，传 None。rnd 是这条收数线程所属的探测轮，
        握手认领时用它校验本轮是否已作废。
        """
        if self._active == link:
            self.on_raw(chunk)
        parser = self._parser_of(link)
        parser.feed(chunk, lambda cmd, payload: self._frame(link, cmd, payload, addr, rnd))

    def _parser_of(self, link: str) -> FrameParser:
        """取链路对应的解析器（每条链路独立，避免探测期多路字节串帧）。"""
        if link == MODE_SERIAL:
            return self._ser_parser
        if link == MODE_UDP:
            return self._udp_parser
        return self._ws_parser

    def _frame(
        self,
        link: str,
        cmd: int,
        payload: bytes,
        addr: tuple[str, int] | None = None,
        rnd: _ProbeRound | None = None,
    ) -> None:
        """某链路解出一完整帧：先处理 UDP 发现、Pong 握手，再转发给 Hub。

        addr 是 UDP 帧的来源地址（串口 / WS 为 None），0xFE 按它登记设备名。
        上层回调抛异常只记日志、不上抛——否则会把收数线程带崩，整条链路静默死亡。
        """
        if link == MODE_UDP:
            if cmd == CmdPost.Discovery:
                self._on_discovery(payload, addr)
                return
            peer = self._peer
            if (
                cmd == CmdPost.Pong
                and not self._peer_locked
                and peer is not None
                and self._ping_sent
            ):
                self._peer_locked = True
                logger.info("对端已确认 %s:%d（Ping/Pong 握手通过）", peer[0], peer[1])
                self._status(
                    MODE_UDP, Status.connected, f"已连接 WiFi 下位机 {peer[0]}:{peer[1]}"
                )
                # 锁定=下行首次真正可用：此刻拉目录才送得到板子
                self._on_locked(MODE_UDP, rnd)

        if link == MODE_WS and cmd == CmdPost.Pong and not self._ws_locked:
            self._ws_locked = True
            self._on_locked(MODE_WS, rnd)

        # Pong 在这里被“截获”更新时间戳，同时也继续往下传给上层
        if cmd == CmdPost.Pong:
            self._last_pong[link] = time.monotonic()

        # 未选中链路只用于探测，帧一律不喂 Hub（单 client，状态是单份）
        if self._active != link:
            return
        try:
            self.on_frame(cmd, payload)
        except Exception as e:
            logger.exception("on_frame 异常: %s", e)

    def send(self, data: bytes, need_ack: bool = False, urgent: bool = False) -> None:
        """写一帧到当前链路：UDP 进攒包窗口，串口 / WS 直接连续写。

        need_ack: 这一帧是否会产生固件 Response（计入单数据报的回执帧上限）
        urgent  : 停车帧，跳过攒包窗口单独成包
        """
        link = self._active
        if link is None:
            logger.debug("未选中链路，丢弃 %d 字节", len(data))
            return
        if link != MODE_UDP:
            self._send_link(link, data)
            return
        with self._tx_cv:
            if urgent:
                self._tx_urgent.append(data)
            else:
                if not self._tx_buf:
                    self._tx_deadline = time.monotonic() + CONFIG.batch_window_s
                self._tx_buf.append((data, need_ack))
                if self._tx_full():
                    self._tx_deadline = 0.0  # 已达上限，立即发
            self._tx_cv.notify()

    def _start_tx_thread(self) -> None:
        """起 UDP 攒包线程（已在跑就不重开）。"""
        t = self._tx_thread
        if t is not None and t.is_alive():
            return
        self._tx_stop.clear()
        self._tx_thread = threading.Thread(
            target=self._tx_loop, name="vex-bridge-tx", daemon=True
        )
        self._tx_thread.start()

    def _stop_tx_thread(self) -> None:
        """停攒包线程，并清空未发出的帧。"""
        self._tx_stop.set()
        with self._tx_cv:
            self._clear_tx_buf()
            self._tx_cv.notify()
        t = self._tx_thread
        self._tx_thread = None
        if t is not None and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=1.0)

    def _clear_tx_buf(self) -> None:
        """清空攒包缓冲与窗口计时（调用方持 _tx_cv）。"""
        self._tx_buf.clear()
        self._tx_urgent.clear()
        self._tx_deadline = 0.0

    def _tx_full(self) -> bool:
        """缓冲里的帧是否已触到单数据报上限（调用方持 _tx_cv）。"""
        total = sum(len(frame) for frame, _ in self._tx_buf)
        acks = sum(1 for _, need_ack in self._tx_buf if need_ack)
        return total >= CONFIG.batch_max_bytes or acks >= CONFIG.batch_max_acks

    def _take_packet(self) -> bytes:
        """从缓冲按到达顺序取一个数据报（调用方持 _tx_cv）。

        受字节数与需回执帧数两条上限；放不下的整帧顺延到下一窗口。
        """
        n = 0
        total = 0
        acks = 0
        for frame, need_ack in self._tx_buf:
            if n and (
                total + len(frame) > CONFIG.batch_max_bytes
                or acks + int(need_ack) > CONFIG.batch_max_acks
            ):
                break
            total += len(frame)
            acks += int(need_ack)
            n += 1
        packet = b"".join(frame for frame, _ in self._tx_buf[:n])
        del self._tx_buf[:n]
        return packet

    def _tx_loop(self) -> None:
        """UDP 下行写线程：帧进缓冲 → 窗口到期（或触上限）→ 打包 → 写链路。

        停车帧不参与窗口，单独成包。窗口按 2ms 分片计时。
        """
        while not self._tx_stop.is_set():
            packet = None
            wait_s = 0.0
            with self._tx_cv:
                # 空缓冲：等新帧（50ms 兜底）
                while (
                    not self._tx_urgent
                    and not self._tx_buf
                    and not self._tx_stop.is_set()
                ):
                    self._tx_cv.wait(timeout=0.05)
                if self._tx_stop.is_set():
                    break
                if self._tx_urgent:
                    packet = self._tx_urgent.pop(0)
                elif self._tx_buf:
                    wait_s = self._tx_deadline - time.monotonic()
                    if wait_s <= 0:
                        packet = self._take_packet()
                        wait_s = 0.0
                if packet is not None:
                    # 有剩帧就开下一窗口
                    self._tx_deadline = (
                        time.monotonic() + CONFIG.batch_window_s if self._tx_buf else 0.0
                    )
            if wait_s > 0:
                time.sleep(min(wait_s, 0.002))
                continue
            if packet is None:
                continue
            if self._active != MODE_UDP:
                continue  # 只写当前选中的 UDP 链路
            try:
                self._send_link(MODE_UDP, packet)
            except Exception as e:
                logger.error("UDP 写失败: %s", e)

    # ---------- 状态过滤（只有选中链路才向前端报） ----------

    def _status(self, link: str, state: Status, message: str) -> None:
        """链路状态的唯一出口：只转发当前选中链路的状态，未选中链路只进日志。

        串口意外掉线还要顺手触发重连流程（Hub 已收到 disconnected）。
        """
        if self._active != link:
            logger.debug("[%s] %s: %s（未选中，不转发）", link, state.value, message)
            return
        self.on_status(state, message)
        if link == MODE_SERIAL and state is Status.disconnected:
            self._on_active_lost(already_reported=True)

    # ---------- 选路仲裁 ----------

    def _on_locked(self, link: str, rnd: _ProbeRound | None = None) -> None:
        """某链路完成 Ping/Pong 握手（在收数线程里执行，别干重活）。"""
        if self._claim(link, rnd):
            self._fire_link_up()

    def _claim(
        self,
        link: str,
        rnd: _ProbeRound | None,
        *,
        ser: "serial.Serial | None" = None,
        ser_info: PortInfo | None = None,
    ) -> bool:
        """一次性抢占：校验本轮未作废，先到者胜。返回 True 表示本条链路被选中。"""
        with self._claim_lock:
            if (
                rnd is None
                or rnd.cancel.is_set()
                or self._auto_stop.is_set()
                or self._active is not None
                or self._round is not rnd
            ):
                return False
            if ser is not None:
                rnd.ser = ser
                self._ser = ser
            self._active = link
            if link == MODE_UDP:
                self._last_peer = self._peer
            elif link == MODE_SERIAL:
                self._last_serial = ser_info

        # 换链路：发送节拍、攒包缓冲与 Pong 时间戳都从零开始
        with self._tx_cv:
            self._clear_tx_buf()
        with self._tx_lock:
            self._last_write_at = 0.0
        self._last_pong[link] = 0.0

        label = self._describe(link)
        logger.info("[选路] 完成: %s", label)
        # 所有链路都只在认领成功后起心跳；探测期的 Ping 由探测轮自己发。
        self._start_heartbeat(link, self._hb_interval)
        self.on_status(Status.connected, f"已连接 {label}（自动选路）")
        return True

    def _fire_link_up(self) -> None:
        """通知 Hub「链路确立」→ _resync_link() 清缓存、重拉四张表、补订阅。"""
        cb = self.on_locked
        if cb is None:
            return
        try:
            cb()
        except Exception:
            logger.exception("链路确立回调异常")

    def _describe(self, link: str) -> str:
        if link == MODE_UDP:
            peer = self._peer
            return f"WiFi 下位机 {peer[0]}:{peer[1]}" if peer else "WiFi 下位机"
        if link == MODE_WS:
            addr = self._ws.remote_address if self._ws is not None else None
            return f"WS 设备 {addr[0]}:{addr[1]}" if addr else "WS 设备"
        dev = self._ser.port if self._ser is not None else (self.port_name or "自动探测")
        return f"VEX 串口 {dev}"

    def _alive(self, rnd: _ProbeRound) -> bool:
        """本轮探测是否还该继续：未停止、本轮未作废、还没选出赢家。"""
        return (
            not self._auto_stop.is_set()
            and not rnd.cancel.is_set()
            and self._round is rnd
            and self._active is None
        )

    def _start_probe(self) -> None:
        """拉起新一轮三探测。选中期间 / 已有一轮在跑则什么都不做。"""
        with self._claim_lock:
            if self._auto_stop.is_set() or self._active is not None:
                return
            old = self._round
            if old is not None and not old.cancel.is_set():
                return
            rnd = _ProbeRound()
            self._round = rnd
            threads = [
                threading.Thread(
                    target=self._probe_serial, args=(rnd,),
                    name="vex-auto-serial", daemon=True,
                ),
                threading.Thread(
                    target=self._probe_udp, args=(rnd,),
                    name="vex-auto-udp", daemon=True,
                ),
                threading.Thread(
                    target=self._probe_ws, args=(rnd,),
                    name="vex-auto-ws", daemon=True,
                ),
            ]
            for t in threads:
                t.start()
        logger.info(
            "[选路] 开始新一轮三探测（串口 + UDP %s:%d + WS %s:%d）",
            self.bind_host, self.bind_port, self.ws_host, self.ws_port,
        )

    def _probe_serial(self, rnd: _ProbeRound) -> None:
        """串口探测：先定向试上次的口，预算用尽再按候选轮转（每次只开一个）。"""
        start_dev: str | None = None
        last = self._last_serial
        if last is not None:
            port = self._match_serial(last, self._serial_candidates())
            if port is not None:
                start_dev = port.device
                deadline = time.monotonic() + CONFIG.last_peer_retry_budget_s
                next_at = time.monotonic()
                while self._alive(rnd) and time.monotonic() < deadline:
                    # 每次尝试占 120ms 窗口，剩下的时间补到 250ms 一拍
                    next_at += CONFIG.last_peer_retry_gap_s
                    if self._try_serial(rnd, port):
                        return
                    delay = next_at - time.monotonic()
                    if delay > 0 and rnd.cancel.wait(delay):
                        break
        self._poll_serial(rnd, start_dev)
        # 自己赢了就不能再关句柄（此刻 self._ser 就是本轮的串口）
        if self._active != MODE_SERIAL:
            self._close_serial(rnd)

    def _poll_serial(self, rnd: _ProbeRound, skip_dev: str | None) -> None:
        """轮流探测候选口：每轮只打开一个，窗口 120ms。"""
        idx = 0
        first = True
        while self._alive(rnd):
            cands = self._serial_candidates()
            if not cands:
                self._status(MODE_SERIAL, Status.error, "未发现 VEX 串口设备（VID 0x2888）")
                if rnd.cancel.wait(CONFIG.find_retry_gap_s):
                    return
                continue
            if first and skip_dev is not None:
                for i, p in enumerate(cands):
                    if p.device == skip_dev:
                        idx = i + 1
                        break
            first = False
            port = cands[idx % len(cands)]
            idx += 1
            if self._try_serial(rnd, port):
                return
            if rnd.cancel.wait(CONFIG.port_try_gap_s):
                return

    def _try_serial(self, rnd: _ProbeRound, port: PortInfo) -> bool:
        """打开一个候选口并探 120ms；成功就认领。返回 True=本轮该结束。"""
        if not self._alive(rnd):
            return True
        self._status(MODE_SERIAL, Status.connecting, f"探测 {port.device}…")
        ser = self._open_serial(port.device, self.baud)
        if ser is None:
            return False
        if not self._probe(ser, rnd.cancel, CONFIG.find_timeout_s):
            try:
                ser.close()
            except Exception:
                pass
            return False
        if not self._claim(MODE_SERIAL, rnd, ser=ser, ser_info=port):
            try:
                ser.close()
            except Exception:
                pass
            return True  # 别人先赢 / 本轮作废：结束探测
        rnd.ser_stop.clear()
        rnd.ser_rx_thread = threading.Thread(
            target=self._serial_rx_loop, args=(rnd,), name="vex-serial-rx", daemon=True
        )
        rnd.ser_rx_thread.start()
        return True

    def _probe_udp(self, rnd: _ProbeRound) -> None:
        """UDP 探测：bind 后先定向试上次的对端，预算用尽再等广播轮流试。"""
        while self._alive(rnd):
            try:
                if self._connect_udp(rnd):
                    break
            except Exception as e:
                logger.warning("[UDP] bind 异常: %s", e)
            if rnd.cancel.wait(CONFIG.find_retry_gap_s):
                break
        if not self._alive(rnd):
            if self._active != MODE_UDP:
                self._close_udp(rnd)
            return

        last = self._last_peer
        if last is not None:
            deadline = time.monotonic() + CONFIG.last_peer_retry_budget_s
            next_at = time.monotonic()
            while self._alive(rnd) and time.monotonic() < deadline:
                # 每次等 200ms Pong，剩下的时间补到 250ms 一拍
                next_at += CONFIG.last_peer_retry_gap_s
                if self._udp_ping_wait(rnd, last, CONFIG.udp_peer_wait_s):
                    return
                delay = next_at - time.monotonic()
                if delay > 0 and rnd.cancel.wait(delay):
                    break
            if self._active == MODE_UDP:
                return
            self._reset_peer()  # 定向失败：清记忆地址，进入轮询

        while self._alive(rnd):
            addr = self._wait_uplink(rnd, CONFIG.udp_broadcast_wait_s)
            if addr is None:
                continue
            if self._udp_ping_wait(rnd, addr, CONFIG.udp_peer_wait_s):
                return
            self._reset_peer()
        if self._active != MODE_UDP:
            self._close_udp(rnd)

    def _udp_ping_wait(self, rnd: _ProbeRound, addr: tuple[str, int], window: float) -> bool:
        """向 addr 发一次探测 Ping，等它回 Pong 完成认领。"""
        if not self._alive(rnd):
            return False
        self._peer = addr
        self._peer_since = time.monotonic()
        self._peer_locked = False
        self._ping_sent = False
        self._last_pong[MODE_UDP] = 0.0
        try:
            self._send_link(MODE_UDP, ping())
        except Exception as e:
            logger.warning("[UDP] 探测 Ping 失败: %s", e)
            return False
        deadline = time.monotonic() + window
        while self._alive(rnd) and time.monotonic() < deadline:
            if self._active == MODE_UDP:
                return True
            rnd.udp_stop.wait(CONFIG.poll_gap_s)
        return self._active == MODE_UDP

    def _wait_uplink(self, rnd: _ProbeRound, timeout: float) -> tuple[str, int] | None:
        """等任意源地址的上行包出现（广播发现 / 别台车主动发）；超时返回 None。"""
        deadline = time.monotonic() + timeout
        while self._alive(rnd) and time.monotonic() < deadline:
            peer = self._peer
            if peer is not None:
                return peer
            rnd.udp_stop.wait(CONFIG.poll_gap_s)
        return None

    def _probe_ws(self, rnd: _ProbeRound) -> None:
        """WS 探测：bind 后等设备自己连进来；Pong 握手由 _ws_handler → _on_locked 完成。"""
        while self._alive(rnd):
            try:
                if self._connect_ws(rnd):
                    break
            except Exception as e:
                logger.warning("[WS] 监听异常: %s", e)
            if rnd.cancel.wait(CONFIG.find_retry_gap_s):
                self._close_ws(rnd)
                return
        if not self._alive(rnd):
            self._close_ws(rnd)
            return
        while self._alive(rnd):
            rnd.ws_stop.wait(CONFIG.poll_gap_s)
        # 自己赢了就不能再关句柄（此刻 self._ws_server 就是本轮的监听）
        if self._active != MODE_WS:
            self._close_ws(rnd)

    def _on_active_lost(self, already_reported: bool) -> None:
        """选中者失联：清空 _active，另起线程取消上一轮并重新探测。"""
        with self._claim_lock:
            if self._active is None:
                return  # 已被别的路径处理（串口 rx/心跳/超时会重复调 _lost）
            lost = self._active
            self._active = None

        logger.warning("[选路] %s 链路失联，回到重连流程", lost)
        with self._tx_cv:
            self._clear_tx_buf()
        if not already_reported:
            self.on_status(Status.disconnected, f"{lost} 链路失联，正在重连…")
        threading.Thread(
            target=self._restart_probe, args=(lost,),
            name="vex-auto-restart", daemon=True,
        ).start()

    def _restart_probe(self, lost: str) -> None:
        """取消上一轮 → 立刻关闭本轮句柄 → 开下一轮。监督者不 join 任何探测线程。"""
        with self._claim_lock:
            old = self._round
            self._round = None
        try:
            self._close_round(old)
        except Exception:
            logger.exception("[选路] 关闭上一轮句柄异常（%s 失联善后）", lost)
        self._start_probe()

    def _close_round(self, rnd: "_ProbeRound | None") -> None:
        """取消本轮并关闭它打开的全部句柄（监督者 / 用户断开用）。"""
        if rnd is None:
            return
        rnd.cancel.set()
        self._close_serial(rnd)
        self._close_udp(rnd)
        self._close_ws(rnd)

    def _close_serial(self, rnd: _ProbeRound) -> None:
        """只关本轮串口（幂等）。探测线程落败时调它，别碰同轮其它链路的资源。"""
        rnd.ser_stop.set()
        ser = rnd.ser
        if ser is not None:
            rnd.ser = None
            if self._ser is ser:
                self._ser = None
            try:
                ser.close()
            except Exception:
                pass

    def _close_udp(self, rnd: _ProbeRound) -> None:
        """只关本轮 socket（幂等）；共享属性先比对身份再清。"""
        rnd.udp_stop.set()
        sock = rnd.sock
        if sock is not None:
            rnd.sock = None
            if self._sock is sock:
                self._sock = None
                self._reset_peer()
            try:
                sock.close()
            except OSError:
                pass

    def _close_ws(self, rnd: _ProbeRound) -> None:
        """只关本轮 WS 连接与监听（幂等）；共享属性先比对身份再清。"""
        rnd.ws_stop.set()
        conn = rnd.ws_conn
        if conn is not None:
            rnd.ws_conn = None
            if self._ws is conn:
                self._ws = None
                self._ws_locked = False
                self._last_pong[MODE_WS] = 0.0
            try:
                conn.close()
            except Exception:
                pass
        server = rnd.ws_server
        if server is not None:
            rnd.ws_server = None
            if self._ws_server is server:
                self._ws_server = None
            try:
                server.shutdown()
            except Exception:
                logger.exception("WS 监听停止异常")

    # ---------- 心跳 ----------

    def start_heartbeat(self, interval: float | None = None) -> threading.Thread | None:
        """单例心跳：只在已选中链路时启动，重复调用先停旧线程再启。

        探测 / 定向期的 Ping 由探测轮自己发，不经过这里；掉线路径也不会调用这里。
        """
        if interval is None:
            interval = CONFIG.heartbeat_gap_s
        self._hb_interval = interval
        if self._active is None:
            return None
        return self._start_heartbeat(self._active, interval)

    def stop_heartbeat(self) -> None:
        """停心跳线程并等它退出；从心跳线程自己调 _lost 进来时不 join（等自己无意义）。"""
        self._hb_stop.set()
        t = self._hb_thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=1.5)
        self._hb_thread = None
        self._hb_link = None

    def _start_heartbeat(self, link: str, interval: float) -> threading.Thread:
        """为指定链路起心跳；该链路不可用时循环会立即退出。"""
        self.stop_heartbeat()
        self._hb_link = link
        self._hb_stop.clear()

        def _loop() -> None:
            timeout = (
                CONFIG.udp_pong_timeout_s
                if link == MODE_UDP
                else CONFIG.pong_timeout_s
            )
            started = time.monotonic()
            while not self._hb_stop.is_set() and self._link_ready(link):
                try:
                    self._send_link(link, ping())
                except Exception as e:
                    logger.warning("心跳发送失败: %s", e)
                    self._lost(link, f"心跳发送失败: {e}")
                    break
                # 可中断 sleep：stop_heartbeat 不用等满一个周期才返回。
                # Event.wait 返回 True = 被 set 唤醒（该停了），False = 正常超时（继续下一拍）
                if self._hb_stop.wait(interval):
                    break
                last = self._last_pong[link]
                # 认领后 _last_pong 被清零：第一拍以心跳起点计时，避免“还没收到任何
                # Pong 就永远不超时”的漏判。
                ref = last if last > 0 else started
                if time.monotonic() - ref > timeout:
                    logger.warning("超过 %.1fs 未收到 Pong，判定下位机掉线", timeout)
                    self._lost(link, "心跳超时，下位机无响应")
                    break

        t = threading.Thread(target=_loop, name=f"vex-heartbeat-{link}", daemon=True)
        self._hb_thread = t
        t.start()
        return t

    def _link_ready(self, link: str) -> bool:
        """该链路是否仍是当前选中链路且句柄可用（心跳继续条件）。"""
        if self._active != link:
            return False
        if link == MODE_SERIAL:
            return self._serial_ready()
        if link == MODE_UDP:
            return self._udp_ready()
        return self._ws_ready()

    def _send_link(self, link: str, data: bytes) -> None:
        """所有出站载荷的唯一写出点：持写锁，UDP 先等满最小发送间隔（CONFIG.min_gap_ms，ms）。

        串口 / WS 直接连续写。没有被真正写出的载荷不推进 _last_write_at。
        """
        with self._tx_lock:
            if link == MODE_UDP:
                wait = CONFIG.min_gap_ms / 1000.0 - (time.monotonic() - self._last_write_at)
                if wait > 0:
                    time.sleep(wait)
            if link == MODE_SERIAL:
                wrote = self._send_serial(data)
            elif link == MODE_UDP:
                wrote = self._send_udp(data)
            else:
                wrote = self._send_ws(data)
            if wrote:
                self._last_write_at = time.monotonic()

    def _lost(self, link: str, message: str) -> None:
        """心跳发送失败/超时的统一入口，按链路分派到各自的失联语义。"""
        if link == MODE_SERIAL:
            self._lost_serial(message)
        elif link == MODE_UDP:
            self._lost_udp(message)
        else:
            self._lost_ws(message)
