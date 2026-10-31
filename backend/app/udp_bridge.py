"""
UDP 桥：接 WiFi 下位机（Hi3861 等）的协议帧，单播 UDP 收发。

与 SerialBridge 的关系：只换"设备进出"这一层。父类的 FrameParser 喂帧、Pong 时间戳
截获、心跳线程与超时判定、on_raw 原样广播全部复用——这些都与传输介质无关。
MockV5Bridge 也是同样的套路（继承 + 覆盖设备层），改通道照这个范式走。

为什么下行也能走 UDP（不需要 TCP）：
    有副作用的帧（写参 / 订阅 / 动作命令）在协议层是「一帧在途」——发完必须等回执
    （Response 任意 status，或 Set_Tunable 成功时的 Tunable_Echo，二者先到先清），
    超时才重发，在途期间不发下一条。同一时刻只有一帧在途 ⇒ 既不会丢也不会乱序，
    所以协议里不需要 seq、也不需要连接状态。依据见 Hi3861 侧 my_proto.h 的
    「下行回执规则」一节。

对端发现（不硬编码 IP，换网络不用重新编译固件）：
    板子上电后向网关（PC 开热点时网关就是 PC）或广播地址发上行帧。本桥收到第一个
    上行包把它当作【候选】peer，只有收到"回应我们 Ping"的 Pong 才【锁定】
    （板子主动送上门的 Pong 不算，否则反向不通也会被误判成已连接）。
    为什么必须等 Pong：收到上行只证明单向可达（板子 → PC），PC → 板子方向可能因
    NAT / 防火墙 / 不对称路由而不通。Ping/Pong 握手是唯一能验证双向的手段，
    单靠"收到第一个包就连"会在反向不通时卡住并误报已连接。

掉线语义与串口不同（重要）：
    UDP 无连接，socket 与 rx 线程在"对端失联"时都是好的，心跳超时只意味着 peer 死了。
    所以 _lost() 只清 peer 回到发现态 + 重启心跳，不关 socket、不报 disconnected。
    父类那套 close / reopen 是串口语义，套过来会造出 WinError 10038 竞态，且
    Hub 的自动重连依赖 _want_connected（启动路径没置 True），端口会一直空着起不来。

单 client 约束（当前明确不做多设备）：
    只维护一个 peer，锁定后其他源地址一律丢弃并告警。要支持多设备得把 Hub 的目录
    缓存 / 快照 / 待确认表全部按 peer 索引，是散在 Hub 各处的重构，现阶段无需求。
"""

import logging
import socket
import threading
import time
from typing import Callable

from .dataclass import PortInfo
from .protocol import CmdPost, ping
from .serial_bridge import SerialBridge, Status

logger = logging.getLogger("vex.udp")

DEFAULT_UDP_PORT = 8888
'''默认监听端口，与 Hi3861 侧 SERVER_PORT 一致'''

UDP_RECV_MAX = 65535
'''单次 recvfrom 的上限：整帧最长 448B，一报可并多帧，这里留足余量即可'''

SO_RCVBUF_BYTES = 4 << 20
'''PC 侧接收缓冲 4MB：高包率下默认缓冲会被撑爆，丢包会被误判成网络问题'''


class UdpBridge(SerialBridge):
    """UDP 版的设备层：bind 一个 socket 收帧，发帧打向已锁定的 peer。

    线程模型与串口桥一致：rx 线程收数并喂解析器；写由调用方（Hub tx 线程 / 心跳线程）
    经 send() 串行化，靠父类 _tx_lock 保证不撕帧。
    """

    # WiFi 下 Pong 超时要比 USB 宽得多：Hi3861 UDP 实测 p99 抖动 14.6ms、max 80ms，
    # 且掉线重连是秒级（STA 断线后 sleep 3s 重连）。沿用串口的 300ms 会频繁误判。
    PONG_TIMEOUT_S = 2.0

    PEER_CANDIDATE_TIMEOUT_S = 3.0
    '''候选 peer 多久没回 Pong 就换下一个源地址，避免卡在一个不回 Pong 的设备上'''

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        on_frame=None,
        on_status=None,
        on_raw=None,
        bind_host: str = "0.0.0.0",
        bind_port: int = DEFAULT_UDP_PORT,
    ) -> None:
        """建 UDP 桥（不立即 bind，connect() 才建 socket）。

        port/baud    : 仅为与 SerialBridge 同签名（Hub 的工厂统一传这两个），UDP 下无意义
        bind_host    : 监听地址，默认全网卡
        bind_port    : 监听端口，默认 8888
        """
        super().__init__(
            port=port,
            baud=baud,
            on_frame=on_frame,
            on_status=on_status,
            on_raw=on_raw,
        )
        self.bind_host = bind_host
        '''监听地址'''

        self.bind_port = bind_port
        '''监听端口'''

        self._sock: socket.socket | None = None
        '''UDP socket；connect() 里创建，disconnect 里关闭'''

        self._peer: tuple[str, int] | None = None
        '''当前对端（候选或已锁定）的 (ip, port)；None=还没见过任何上行包'''

        self._peer_locked = False
        '''是否已收到该 peer 的 Pong 并完成锁定'''

        self._peer_since = 0.0
        '''当前候选 peer 的起始时刻，用于候选超时换人'''

        self._ping_sent = False
        '''是否已向当前 peer 发出过 Ping。只有"我们发了 Ping 才收到的 Pong"能证明
           双向可达——板子主动送上门的 Pong 不算，否则单向不通也会被误判成已连接'''

        self._strangers: set[tuple[str, int]] = set()
        '''已告警过的陌生源地址，避免每包都打日志'''

        self.on_locked: Callable[[], None] | None = None
        '''Ping/Pong 握手【锁定成功】后的回调（Hub 注入，重拉目录+补订阅）。

        为什么必须有：Hub 只在启动/重连时拉目录，而 UDP 锁定前 send() 静默丢弃，
        启动时那次请求根本送不到板子。锁定（含失联后板子回来重新握手）才是
        下行真正可用的时刻，目录请求必须等这一刻再发——用户口径：
        "目录肯定是 ping/pong 确认后再由 backend 发送"。'''

    # ---------- 状态 ----------

    @property
    def connected(self) -> bool:
        """socket 已 bind 且 rx 线程在跑。

        注意这里【不要求】peer 已锁定——UDP 无连接，bind 成功就算通道就绪；
        peer 为空时 send() 静默丢弃（此时心跳的 Ping 自然发不出去）。
        已知边界：候选态若板子只发数据、既不回 Ping 也不主动发 Pong，
        _last_pong 恒 0，PONG 超时判定不会触发，可能停在候选态（待办）。
        """
        return self._connected and self._sock is not None

    @property
    def peer(self) -> tuple[str, int] | None:
        """当前对端；None=尚未发现。诊断显示用。"""
        return self._peer

    @property
    def peer_locked(self) -> bool:
        """对端是否已通过 Ping/Pong 握手确认（双向可达）。"""
        return self._peer_locked

    def list_ports(self) -> list[PortInfo]:
        """UDP 通道没有串口可枚举，返回一个描述项供 /api/ports 显示。"""
        return [
            PortInfo(
                device=f"udp://{self.bind_host}:{self.bind_port}",
                description="UDP 监听（WiFi 下位机，对端自动发现）",
                vid=None,
                pid=None,
                serial_number=self._peer[0] if self._peer else "",
            )
        ]

    # ---------- 连接 ----------

    def connect(self, timeout: float = 0.0) -> bool:
        """建 socket 并起收数线程。UDP 无握手，bind 成功即返回 True。

        timeout: 保留参数（与串口桥同签名），UDP 下不使用
        返回: True=通道就绪（是否真连上下位机要看 peer 是否锁定）
        """
        if self.connected:
            return True

        # 兜底：若真有遗留 socket（异常路径），先关，保证这次 bind 不撞旧句柄
        if self._sock is not None:
            self._close_socket()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Windows：对端端口不可达时 ICMP 会让下次 recvfrom 抛 WSAECONNRESET(10054)。
            # 板子重启/断电期间我们仍在发 Ping，极易触发——UDP 本就该容忍对端暂时消失，
            # 关掉这个通知；非 Windows 无此常量，hasattr 保护。
            if hasattr(socket, "SIO_UDP_CONNRESET"):
                try:
                    sock.ioctl(socket.SIO_UDP_CONNRESET, False)
                except OSError:
                    pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SO_RCVBUF_BYTES)
            except OSError:
                pass  # 有的平台不接受这么大，用默认值也能跑
            # 读超时：让 recvfrom 周期返回，rx 线程才能及时响应 _stop
            sock.settimeout(0.5)
            sock.bind((self.bind_host, self.bind_port))
        except OSError as e:
            self.on_status(Status.error, f"UDP 端口 {self.bind_port} 绑定失败: {e}")
            return False

        self._sock = sock
        self._connected = True
        self._stop.clear()
        self._reset_peer()
        self._rx_thread = threading.Thread(target=self._rx_loop, name="vex-udp-rx", daemon=True)
        self._rx_thread.start()
        self.on_status(
            Status.connected,
            f"UDP 监听 {self.bind_host}:{self.bind_port}，等待下位机上行帧…",
        )
        logger.info("UDP 监听 %s:%d", self.bind_host, self.bind_port)
        return True

    def disconnect(self) -> None:
        """主动断开：停心跳 → 置停止标志 → 收 rx 线程 → 关 socket。"""
        self.stop_heartbeat()
        self._stop.set()
        self._connected = False
        self._reset_peer()
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=1.0)
        self._rx_thread = None
        self._close_socket()
        self.on_status(Status.idle, "UDP 已断开")

    def _lost(self, message: str) -> None:
        """心跳超时：只清 peer 回到发现态，socket / rx 线程保持不动。

        不调父类 _lost()：那套 close / reopen 是串口语义，套到 UDP 上会造出
        WinError 10038 竞态，且 Hub 的自动重连依赖启动路径没置 True 的
        _want_connected，端口会一直空着。重启心跳是为了板子回来时有 Ping 可回。
        """
        logger.warning("UDP 链路失联，回到发现态：%s", message)
        self._reset_peer()
        self.start_heartbeat(0.08)
        self.on_status(
            Status.connected,
            f"UDP 监听 {self.bind_host}:{self.bind_port}，等待下位机上行帧…",
        )

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _reset_peer(self) -> None:
        """清空对端状态，回到"等第一个上行包"。"""
        self._peer = None
        self._peer_locked = False
        self._peer_since = 0.0
        self._ping_sent = False
        self._last_pong = 0.0

    # ---------- 收发 ----------

    def send(self, data: bytes) -> None:
        """发一帧给当前 peer（候选或已锁定）。

        没有 peer 时【静默返回】而不是抛错：发现阶段还没见过上行包是正常状态，
        此时心跳的 Ping 发不出去属预期，让 Hub 的 tx 线程报错反而会污染诊断。
        """
        peer = self._peer
        sock = self._sock
        if peer is None or sock is None or not self._connected:
            logger.debug("UDP 未发现对端，丢弃 %d 字节", len(data))
            return
        with self._tx_lock:
            try:
                sock.sendto(data, peer)
            except OSError as e:
                logger.warning("UDP 发送失败: %s", e)
                return
        # 只有"我们主动发出的 Ping"才能当作双向可达的凭据；板子自己送上门的
        # Pong 不算（反向可能根本不通）。父类心跳每拍 send(ping())，按内容识别。
        if data == ping():
            self._ping_sent = True

    def _rx_loop(self) -> None:
        """收数线程：recvfrom 循环，按 peer 过滤后广播原样字节 + 喂 FrameParser。

        过滤规则（单 client）：
            未锁定 → 第一个源地址成为候选；候选超时未回 Pong 就换下一个源地址
            已锁定 → 只认该 peer，其他源地址丢弃并告警一次
        """
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(UDP_RECV_MAX)
            except socket.timeout:
                continue  # 让 _stop 有机会生效
            except OSError as e:
                if self._stop.is_set():
                    break
                # Windows 上 sendto 到已消失的对端会收到 WSAECONNRESET(10054)，
                # 那只是"上次发送的目标不可达"的迟到通知，socket 本身没坏。
                # UDP 无连接，不能因为它拆掉整条链路；由心跳超时去判失联并软重置。
                # 真错误也先继续试，短暂 sleep 防止异常时 CPU 空转。
                logger.debug("UDP recv 异常（忽略继续收包）: %s", e)
                time.sleep(0.05)
                continue

            if not data:
                continue

            if not self._accept(addr):
                continue

            # 与串口桥同一顺序：先原样喂 WS 上位机（前端自己拆帧），再喂 Python 解析器
            self.on_raw(data)
            self._parser.feed(data, self._on_frame)

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
            time.monotonic() - self._peer_since > self.PEER_CANDIDATE_TIMEOUT_S
            and addr != self._peer
        ):
            logger.warning(
                "候选 %s:%d 超时未回 Pong，改用 %s:%d",
                self._peer[0], self._peer[1], addr[0], addr[1],
            )
            self._peer = addr
            self._peer_since = time.monotonic()
            self._last_pong = 0.0
            self._ping_sent = False
        return addr == self._peer

    def _on_frame(self, cmd: int, payload: bytes) -> None:
        """解析器出一帧：Pong 兼作"锁定"凭据，再交给父类做时间戳截获与转发。"""
        if (
            cmd == CmdPost.Pong
            and not self._peer_locked
            and self._peer is not None
            and self._ping_sent
        ):
            self._peer_locked = True
            logger.info("对端已确认 %s:%d（Ping/Pong 握手通过）", self._peer[0], self._peer[1])
            self.on_status(
                Status.connected,
                f"已连接 WiFi 下位机 {self._peer[0]}:{self._peer[1]}",
            )
            # 锁定=下行首次真正可用：此刻拉目录才送得到板子（失败静默丢弃没有意义）
            if self.on_locked is not None:
                self.on_locked()
        super()._on_frame(cmd, payload)
