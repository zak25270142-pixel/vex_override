"""
后端启动配置：启动时从 app/config.json 读一次，改配置只改 JSON、重启生效。

两类字段：
  - 协议适配（frame_head / max_payload / discovery_* / req_list_max /
    cmd_action_* / fw_lag_*）：下位机千奇百怪、各家自己扩展，上位机源码恒定不碰，
    靠这里对齐手头这台固件的帧头、帧长、发现帧约定等。固件改了就改这里。
  - 后端运行参数（重发 / 攒包 / 探测 / 心跳 / 日志）：改错只影响快慢或丢包。

JSON 没有注释语法，每个字段"为什么是这个数"写在下面的字段说明里；README 的
配置表再列一遍。字段名与键一一对应，不做别名；代码里只写 CONFIG.<字段>。
"""

import json
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")


@dataclass
class Config:
    """app/config.json 的完整结构；键与字段一一对应，不分组也串不了。"""

    # ---- 协议适配：换固件改这里，上位机源码不用动 ----
    frame_head: int
    """帧头字节（0xA5=165），与固件的帧头常量一致。"""
    max_payload: int
    """单帧 payload 上限；固件 rx/tx 静态数组按同值分配，改大要连攒包余量、MTU 一起重算。"""
    discovery_magic: str
    """0xFE 发现帧的 3 字节 ASCII 魔数；FrameParser 逐字节核对，长度必须正好 3。"""
    discovery_name_max: int
    """发现帧 len 的合法名字长度上限，超过判非法帧丢弃。"""
    discovery_len_clear: int
    """发现帧 len 的清除登记哨兵（255），取该值时不带名字字节。"""
    req_list_max: int
    """目录补发请求一次最多带几个 index；与固件 REQ_BUF_CAP 同值（池只放下标，48 个正好装满），超了整帧拒绝。"""
    cmd_action_min: int
    cmd_action_max: int
    """动作命令段 [min,max]（0x80~0x9F），也是 cmd 目录表下标换算基准。"""
    fw_lag_stale_ms: int
    """监控项落后最新帧超过多少 ms 判旧，绑固件掉线巡视周期。"""
    fw_lag_extrapolate_cap_ms: int
    """lag 外推封顶（ms），防时间戳回绕把旧值伪装成新鲜。"""

    # ---- 回执 ----
    max_retries: int
    """一条下行帧等不到回执时最多重发几次；用完放弃并记日志（防版本不匹配无限重发）。"""
    table_resend_rounds: int
    """目录"对照→补发"外循环最多几轮。每个补发帧自带 max_retries 次重发，
    但外循环每轮发的都是新帧、预算永远烧不完——不单独数轮次会滚雪球。"""

    # ---- 曲线建议 ----
    max_groups: int
    """最多几组建议曲线。上限是脚本侧的业务约定，不是前端容量（前端 4 通道 ×
    5 槽）：组数=通道数，每组留 1 槽空余，好让操作者自己再补一条对比曲线。"""
    max_items: int
    """每组最多几条建议曲线，与 max_groups 同一口径。"""

    # ---- 日志 ----
    max_frames: int
    """最多存多少拍（宽表的行）。脚本忘了 end_log 会一直攒到内存吃满，超出后
    "丢新不丢旧"：保留已记录的时间窗完整，事后按窗口统计仍有意义。按拍而不是
    按样本条数：条数会随登记项数漂移（记 20 项能撑 250s、记 50 项只有 100s），
    语义说不清；帧数才是"最长记多久"。100Hz 下 5000 帧 = 50 秒。内存不再是
    约束：宽表一格 8 B，50 列 × 5000 帧 = 2 MB，给到 3 万帧也才 12 MB。"""
    same_tick_ms: float
    """同拍合并容差（ms）。下位机一帧带一个时间戳 + N 个监控项，同帧内时刻完全
    相同；同一拍若被拆成多帧发，帧间时刻差也远小于 2ms——超过 2ms 就算下一拍。
    不插值、不最近邻：这是"同拍精确配对"，与 calc 过去的口径一致。"""

    # ---- UDP 发送 ----
    min_gap_ms: float
    """两个数据报之间的最小发送间隔（ms）。只约束 UDP 数据报；串口 / WS 是字节流，
    不等待。实测 448B 单包在 2/3/5ms 间隔下丢 1.25% / 2.75% / 1.50%，所以下限取
    2ms。改小表现为丢包变多，不是报错。"""
    batch_window_s: float
    """攒包窗口（秒）：窗口内到齐的帧合成一个数据报，到期即发。改小更实时、包数
    更多；改大更省包、延迟更高。"""
    batch_max_bytes: int
    """单数据报字节上限。只装"整帧"，下一帧装不下就顺延到下一窗口，不拆分、不
    截断。整帧上限 = config.max_payload + 3（现值 445 → 448B），两帧就 896B，超过
    本值，所以满长帧一包只能放一帧；只有短帧（订阅、Ping、小 payload 命令）才能
    合包。这是刻意的：拆帧会让固件的解帧假设失效。改大可能超过 MTU 或触发丢包。"""
    batch_max_acks: int
    """单数据报内"会产生 Response 的帧"上限（条）。固件 RESP_QUEUE_DEPTH=8，队列
    满时丢最新 ACK，所以这里刻意小于 8，给同一窗口里其它回执（SetTunable echo、
    目录 OK）留位置。改大表现为回执被丢、超时重发变多。"""

    # ---- UDP 接收 ----
    buffer_bytes: int
    """PC 侧接收缓冲（字节）。高包率下默认缓冲会被撑爆，丢包会被误判成网络问题。"""
    max_packet_bytes: int
    """单次 recvfrom 的上限（字节）。整帧最长 448B，一报可并多帧，这里留足余量
    即可；65535 是 UDP 单报文的系统硬上限。"""

    # ---- 探测与心跳 ----
    heartbeat_gap_s: float
    """心跳间隔（秒）。80ms 一发 Ping，保证固件 300ms 巡视窗口
    （LINK_PATROL_TICKS=30）内至少 3 个 Ping；改大直接改变掉线语义，改小只多占带宽。"""
    find_timeout_s: float
    """单个候选口 / WS 连接等 Pong 的时长（秒）。主控 10ms 一拍即回，120ms 留有
    十倍余量；窗口内还会每 30ms 补发一次，防首帧丢在枚举窗口。"""
    find_retry_gap_s: float
    """探测失败（没插 VEX / UDP 或 WS 端口被占）后的重试间隔（秒）。"""
    poll_gap_s: float
    """UDP / WS 探测线程轮询握手事件的粒度（秒）。"""
    port_try_gap_s: float
    """串口轮询换下一个候选口前的间隔（秒），给取消标志一个落脚处。"""
    udp_peer_wait_s: float
    """向一个 peer 发探测 Ping 后等待 Pong 的时长（秒）。"""
    peer_switch_timeout_s: float
    """候选 peer 多久没回 Pong 就换下一个源地址，避免卡在一个不回 Pong 的设备上。"""
    udp_broadcast_wait_s: float
    """等广播出现的时长（秒）：板子无 peer 时 1Hz 广播，至少能收到一帧。"""
    last_peer_retry_gap_s: float
    """定向重试间隔（秒）：一次没应答后隔这么久再试上次的对端。"""
    last_peer_retry_budget_s: float
    """定向重试总预算（秒）：250ms × 6，用尽转入轮流探测。"""

    # ---- 掉线判定 ----
    pong_timeout_s: float
    """串口 / WS 的 Pong 超时（秒）：80ms 心跳，容忍约 3 个 Pong 丢失。300ms =
    固件断链巡视周期（communication.h LINK_PATROL_TICKS=30），同一事件的第二
    见证。改小会误判掉线。"""
    udp_pong_timeout_s: float
    """WiFi 下 Pong 超时（秒）要宽得多：Hi3861 UDP 实测 p99 抖动 14.6ms、max 80ms，
    且掉线重连是秒级（STA 断线后 sleep 3s 重连），沿用串口的 300ms 会频繁误判。"""


# 不设默认值、不做范围检查：文件缺失 / 缺字段 / JSON 写坏都直接抛原始异常，错了当场炸。
CONFIG = Config(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
