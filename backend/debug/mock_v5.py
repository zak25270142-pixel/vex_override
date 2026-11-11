"""
Mock V5：没有真车/真固件时，模拟一台讲本协议的 V5 主控（调试数据源）。

为什么在 backend 侧而不是小程序/浏览器里造帧：
  - 上位机（小程序/网页）连的是 WS，mock 顶替的是"串口后面的那台车"——
    WS 透传、status/alert 带外消息、订阅 diff、停车插队、重连全部走真实路径，
    客户端演示模式把这些全绕掉了，测了个寂寞
  - 一台 mock 同时服务小程序和网页两个上位机（各开各的 WS，订阅互不干扰）
  - 模拟逻辑与 protocol.py 的构建器同源，不复制帧格式知识

怎么用（真车时不会启动本进程）：
    .\\.venv\\Scripts\\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
    .\\.venv\\Scripts\\python -m debug.mock_ws_source
  后端固定三探测（串口 + UDP + WS），本进程连上默认 8889 的 WS 端口即被选中。
  连上后行为与真机一致：拉四张表（burst + OK 项数）、订阅推送 100Hz/80ms、
  调参回显、命令回执（原地转向永远 BUSY 拒绝，演示拒绝 toast）、80ms 心跳。

实现方式：本文件只提供 _MockV5Core（模拟主控内核 + 四张表），跑在调试进程
debug/mock_ws_source.py 里。帧仍是 A5|Cmd|Payload|XOR 二进制协议，走 WS 二进制
消息；后端的 on_raw 广播 + FrameParser 喂帧 + 心跳超时判定全部复用真链路代码。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable

from app.config import CONFIG
from app.protocol import (
    CMD_TAG_ARGC_SHIFT,
    CMD_TAG_RANGE,
    CmdGet,
    CmdPost,
    CmdSpec,
    MONITOR_TAG_FAST,
    MONITOR_TAG_GETTER,
    MONITOR_TAG_SUB,
    RESP_OK,
    RESP_OK_INFO,
    RESP_MODE_MASK,
    RespErrReject,
    ValueType,
    build_frame,
    decode_value,
    encode_value,
    value_size,
)

logger = logging.getLogger("vex.mock")


def build_response(echo_cmd: int, echo_payload: bytes = b"", status: int = RESP_OK, info: bytes = b"") -> bytes:
    """组一帧 Response（假固件/测试用；真机由固件 send_responses 直接拼帧）。

    帧 [L][原话cmd][status][附加段][原话payload]：0x00 无附加段；
    0x80（OK+info）/ 内定err类 = [len][data]（len=0 即不带）；串类 = 裸串，低 6 位就是串长。
    """
    if status == RESP_OK:
        body = bytes([echo_cmd & 0xFF, RESP_OK])
    elif status == RESP_OK_INFO or (status & RESP_MODE_MASK):
        body = bytes([echo_cmd & 0xFF, status & 0xFF, len(info)]) + info
    else:
        info = info[:0x3F]
        body = bytes([echo_cmd & 0xFF, (status & 0xC0) | len(info)]) + info
    body += echo_payload
    return build_frame(CmdPost.Response, bytes([len(body)]) + body)

# 监控项语义种类（tag & MONITOR_TAG_KIND_MASK，与 my_main.h 的 MENU_TAG_KIND_* 一致；
# protocol.py 未导出枚举，这里按位值用）
_KIND_POS_X = 1
_KIND_POS_Y = 2
_KIND_YAW = 3

_TAG_SUB = MONITOR_TAG_SUB
_TAG_FAST = MONITOR_TAG_FAST
_TAG_GETTER = MONITOR_TAG_GETTER
_VT = ValueType

# 拒绝/补发类 status 字节 = 类别位 | err号。注意 protocol.py 的 RespErrReject /
# RespErrRetry 成员值是 err 号而不是完整 status 字节——直接拿成员值当
# status 用会编出 0x03 这种"需补发+串模式"的错字节（本 mock 第一版就踩了）
_RESP_UNKNOWN_CMD = 0xC0 | int(RespErrReject.UnknownCmd)
_RESP_INDEX_RANGE = 0xC0 | int(RespErrReject.IndexRange)
_RESP_BUSY = 0xC0 | int(RespErrReject.Busy)

# 按键事件（下行，前端→主控）：[index][state]，无回执。protocol.py 只透传这条命令、
# 未列枚举，这里按原值处理
_CMD_KEY_EVENT = 0x05

# ---- 模拟的四张表（结构照实车：tag 位域/类型/单位都按协议给） ----
# 表项用 dict 而不是 dataclass：mock 只负责按协议造字节，不参与 Hub 的目录缓存

_MOCK_TUNABLES = [
    {"index": 0, "type": _VT.Float, "name": "速度环KP", "unit": "", "value": 1.2},
    {"index": 1, "type": _VT.Float, "name": "速度环KI", "unit": "", "value": 0.05},
    {"index": 2, "type": _VT.Float, "name": "速度环KD", "unit": "", "value": 0.01},
    {"index": 3, "type": _VT.Float, "name": "阻力前馈KF", "unit": "V", "value": 0.35},
    {"index": 4, "type": _VT.Float, "name": "最大电压", "unit": "V", "value": 12.0},
    {"index": 5, "type": _VT.UInt16, "name": "采样周期", "unit": "ms", "value": 10},
    {"index": 6, "type": _VT.Bool, "name": "调试开关", "unit": "", "value": 0},
    {"index": 7, "type": _VT.Color, "name": "主题色", "unit": "", "value": "#4fc3f7"},
]

_MOCK_MONITORS = [
    {"index": 0, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST | _KIND_POS_X, "name": "位姿X", "unit": "m"},
    {"index": 1, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST | _KIND_POS_Y, "name": "位姿Y", "unit": "m"},
    {"index": 2, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST | _KIND_YAW, "name": "航向角", "unit": "deg"},
    {"index": 3, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST, "name": "左0转速", "unit": "rpm"},
    {"index": 4, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST, "name": "左1转速", "unit": "rpm"},
    {"index": 5, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST, "name": "左2转速", "unit": "rpm"},
    {"index": 6, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST, "name": "右0转速", "unit": "rpm"},
    {"index": 7, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST, "name": "右1转速", "unit": "rpm"},
    {"index": 8, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST, "name": "右2转速", "unit": "rpm"},
    {"index": 9, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST | _TAG_GETTER, "name": "左0电压", "unit": "V"},
    {"index": 10, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST | _TAG_GETTER, "name": "右0电压", "unit": "V"},
    {"index": 11, "type": _VT.Float, "tag": _TAG_SUB | _TAG_FAST, "name": "电池电压", "unit": "V"},
    {"index": 12, "type": _VT.Float, "tag": _TAG_SUB, "name": "CPU占用", "unit": "%"},  # 低速（80ms 轮转）
    {"index": 13, "type": _VT.Int16, "tag": _TAG_SUB, "name": "错误码", "unit": ""},  # 低速
    # 新增：V5 主控电池电量（与真机 monitor_menu_item 同名同义；低速推送）
    {"index": 14, "type": _VT.Float, "tag": _TAG_SUB | _TAG_GETTER, "name": "电池电量", "unit": "%"},
]

_MOCK_COMMANDS = [
    {
        "index": 0x80,
        "tag": CMD_TAG_RANGE | (2 << CMD_TAG_ARGC_SHIFT) | int(CmdSpec.Normal),
        "name": "设置电机电压",
        "fields": [
            {"type": _VT.UInt8, "name": "编号", "min": 0, "max": 7},
            {"type": _VT.Float, "name": "电压", "min": -12.0, "max": 12.0},
        ],
    },
    {
        "index": 0x81,
        "tag": CMD_TAG_RANGE | (2 << CMD_TAG_ARGC_SHIFT) | int(CmdSpec.Normal),
        "name": "设置转速",
        "fields": [
            {"type": _VT.UInt8, "name": "编号", "min": 0, "max": 7},
            {"type": _VT.Float, "name": "转速", "min": -200.0, "max": 200.0},
        ],
    },
    {
        "index": 0x82,
        "tag": CMD_TAG_RANGE | (1 << CMD_TAG_ARGC_SHIFT) | int(CmdSpec.Normal),
        "name": "直行",
        "fields": [{"type": _VT.Float, "name": "距离", "min": -3.0, "max": 3.0}],
    },
    {
        # 永远 BUSY 拒绝：演示"命令被拒"toast 与 status 位域的人话转换
        "index": 0x83,
        "tag": CMD_TAG_RANGE | (1 << CMD_TAG_ARGC_SHIFT) | int(CmdSpec.Normal),
        "name": "原地转向",
        "fields": [{"type": _VT.Float, "name": "角度", "min": -180.0, "max": 180.0}],
    },
    {
        "index": 0x84,
        "tag": (0 << CMD_TAG_ARGC_SHIFT) | int(CmdSpec.Stop),
        "name": "停车",
        "fields": [],
    },
]

# 按键表：下标即 index（与固件 KEY_TABLE 同序），目录帧只带名字
_MOCK_KEYS = [
    {"index": 0, "name": "确认"},
    {"index": 1, "name": "上"},
    {"index": 2, "name": "下"},
    {"index": 3, "name": "返回"},
    {"index": 4, "name": "呼吸灯"},
    {"index": 5, "name": "风扇"},
    {"index": 6, "name": "指示灯"},
]


def _clone(list_: list[dict]) -> list[dict]:
    """浅拷贝表项（运行时可改 tag/value，不动模块级常量表）。"""
    out = []
    for it in list_:
        c = dict(it)
        if it.get("fields") is not None:
            c["fields"] = [dict(f) for f in it["fields"]]
        out.append(c)
    return out


class _MockV5Core:
    """模拟主控内核：四张表 + 10ms 拍 + 下行帧处理。

    与 firmware USB_Comm 的行为子集同构（逐条注释标了对应处），上行帧用
    protocol.py 的构建器组装。线程模型：receive() 在 Hub 的 tx/心跳线程里跑
    （入队/改表），tick() 在 mock 自己的 10ms 线程里跑（出帧/算值）——
    队列是 list，append/pop(0) 在 GIL 下原子，表的读写同理；演示够用，
    真机路径的并发纪律在 TransportBridge 侧，不归本类。
    """

    TICK_TABLE_BATCH = 8  # 固件 DIR_BATCH：每拍最多几项（一项一帧）
    MON_PACK_MAX = 16  # 固件 MON_PACK_MAX
    SLOW_PHASES = 8  # 固件 SLOW_PHASES
    LINK_PATROL_S = 0.3  # 固件 LINK_PATROL_TICKS=30 拍：300ms 无 Ping 停推
    ECHO_PER_TICK = 1
    RESP_PER_TICK = 2

    def __init__(self, emit: Callable[[bytes], None]) -> None:
        """
        emit: 上行帧出口（TransportBridge._mock_emit：on_raw 广播 + FrameParser 喂帧）
        """
        self._emit = emit
        self.tick_count = 0

        self.tun = _clone(_MOCK_TUNABLES)
        self.mon = _clone(_MOCK_MONITORS)
        self.cmd = _clone(_MOCK_COMMANDS)
        self.key = _clone(_MOCK_KEYS)

        # 四张表各一条目录倒计时（对应固件 dir_tun/dir_mon/dir_cmd/dir_key）：
        # q=待发项、pending=末批是否要回 OK 项数、cmd/echo/total=回执用
        self.tables: dict[str, dict] = {
            "tunable": {"q": [], "pending": False, "cmd": 0, "echo": b"", "total": 0},
            "monitor": {"q": [], "pending": False, "cmd": 0, "echo": b"", "total": 0},
            "cmd": {"q": [], "pending": False, "cmd": 0, "echo": b"", "total": 0},
            "key": {"q": [], "pending": False, "cmd": 0, "echo": b"", "total": 0},
        }

        # 待发帧队列（Pong 最优先，回显次之，Response 兜底）
        self.pong_q: list[bytes] = []
        self.echo_q: list[bytes] = []
        self.resp_q: list[bytes] = []

        # 链路巡视（本类自带的 Ping 监督；父类心跳线程的 Pong 超时是第二见证）
        self.last_ping_at = time.monotonic()
        self.link_ok = True

        # 模拟物理量
        self.t0 = time.monotonic()
        self.motor_v = [0.0] * 6  # 指令电压
        self.motor_rpm = [0.0] * 6  # 转速（一阶滞后跟随）
        self.pose_x = 0.0
        self.pose_y = 0.0

    # ---------- 下行帧 ----------

    def receive(self, frame: bytes) -> None:
        """收一帧下行帧（Hub/心跳线程经 TransportBridge 的 mock 分支转交）。

        每次 send 就是一整帧（上位机组帧后整帧下发），无需粘包状态机；
        但仍做帧头/XOR 校验，坏帧静默丢——与真实固件 rx 状态机一致。
        """
        if len(frame) < 3 or frame[0] != CONFIG.frame_head:
            return
        xor = 0
        for b in frame[1:-1]:
            xor ^= b
        if xor != frame[-1]:
            return
        self._dispatch(frame[1], frame[2:-1])

    def _dispatch(self, cmd: int, p: bytes) -> None:
        if cmd == CmdGet.Ping:
            self.last_ping_at = time.monotonic()
            self.link_ok = True
            self.pong_q.append(build_frame(CmdPost.Pong))
            return
        if cmd in (
            CmdGet.RequestTunable,
            CmdGet.RequestMonitor,
            CmdGet.RequestCommand,
            CmdGet.RequestKeyDir,
        ):
            self._on_table_request(cmd, p)
            return
        if cmd == CmdGet.Subscribe:
            self._on_subscribe(cmd, p)
            return
        if cmd == CmdGet.SetTunable:
            self._on_set_tunable(cmd, p)
            return
        if cmd == _CMD_KEY_EVENT:
            # 按键事件 [index][state]：离散输入，主控不回执（真机也不回）。
            # 不拦会把未登记命令字当 UnknownCmd 回出去，刷满前端日志
            return
        if CONFIG.cmd_action_min <= cmd <= CONFIG.cmd_action_max:
            self._on_action(cmd, p)
            return
        # 其余命令字：未注册
        self.resp_q.append(build_response(cmd, p, _RESP_UNKNOWN_CMD))

    def _on_table_request(self, cmd: int, p: bytes) -> None:
        """目录请求 [len 1B][index...]：len=0 全量；非0=只补发这些下标。

        与固件 req_list_ok/req_register 同规则：超 config.req_list_max 或下标越界
        → 整帧拒绝（回显原话 payload），主机端应改走全量重拉。
        """
        key = (
            "tunable"
            if cmd == CmdGet.RequestTunable
            else "monitor"
            if cmd == CmdGet.RequestMonitor
            else "key"
            if cmd == CmdGet.RequestKeyDir
            else "cmd"
        )
        items = {"tunable": self.tun, "monitor": self.mon, "cmd": self.cmd, "key": self.key}[key]
        if len(p) < 1:
            return
        n = p[0]
        if n == 0:
            queue = list(items)
            total = len(items)
        else:
            idxs = list(p[1 : 1 + n])
            if n > CONFIG.req_list_max or any(i >= len(items) for i in idxs):  # config.req_list_max
                self.resp_q.append(build_response(cmd, p, _RESP_INDEX_RANGE))
                return
            queue = [items[i] for i in idxs]
            total = n
        d = self.tables[key]
        d["q"] = queue
        d["pending"] = True
        d["cmd"] = cmd
        d["echo"] = p
        d["total"] = total
        # 收到即回：整表 0x80 + 表项数，局部 0x00；末批另回一帧无回显的传完通知
        if p[0] == 0:
            self.resp_q.append(build_response(cmd, p, RESP_OK_INFO, bytes([total & 0xFF])))
        else:
            self.resp_q.append(build_response(cmd, p, RESP_OK))
        if not queue:
            d["pending"] = False
            self.resp_q.append(build_response(cmd, b"", RESP_OK))

    def _on_subscribe(self, cmd: int, p: bytes) -> None:
        """订阅/退订：只合并上位机负责的 SUB/FAST 两位，保留本机 GETTER/KIND。

        与固件 handle_command 的合并逻辑一致；Subscribe 有回执（is_ack_tracked）。
        """
        if len(p) < 2:
            return
        idx = p[0]
        it = self.mon[idx] if idx < len(self.mon) else None
        if it is None:
            self.resp_q.append(build_response(cmd, p, _RESP_INDEX_RANGE))
            return
        it["tag"] = (it.get("tag", 0) & ~(_TAG_SUB | _TAG_FAST)) | (p[1] & (_TAG_SUB | _TAG_FAST))
        self.resp_q.append(build_response(cmd, p, RESP_OK))

    def _on_set_tunable(self, cmd: int, p: bytes) -> None:
        """调参写入 [index][value 8B 定长]：写成功回 Tunable_Echo（真实生效值）。

        下标超表拒绝（其余情况按表项类型解码）。
        """
        if len(p) < 9:
            return
        idx = p[0]
        it = self.tun[idx] if idx < len(self.tun) else None
        if it is None:
            self.resp_q.append(build_response(cmd, p, _RESP_INDEX_RANGE))
            return
        it["value"] = decode_value(p[1:9], 0, it["type"])
        self.echo_q.append(self._echo_frame(it))

    def _on_action(self, cmd: int, p: bytes) -> None:
        """动作命令：查表 → 应用 → 回 Response。原地转向永远 BUSY（演示拒绝）。"""
        it = next((c for c in self.cmd if c["index"] == cmd), None)
        if it is None:
            self.resp_q.append(build_response(cmd, p, _RESP_UNKNOWN_CMD))
            return
        self._apply_action(it, p)
        status = _RESP_BUSY if it["index"] == 0x83 else RESP_OK
        self.resp_q.append(build_response(cmd, p, status))

    def _apply_action(self, it: dict, p: bytes) -> None:
        """把命令写进模拟物理量（设置电机电压/直行/停车有可见效果，其余从简）。"""
        if it["index"] == 0x80 and len(p) >= 5:
            # [uint8 编号][float 电压]
            mid = p[0]
            v = decode_value(p[1:5], 0, _VT.Float)
            if mid < len(self.motor_v):
                self.motor_v[mid] = float(v)
        elif it["index"] == 0x82 and len(p) >= 4:
            # 直行 [float 距离]：直接挪一下模拟位姿
            self.pose_x += float(decode_value(p[0:4], 0, _VT.Float))
        elif it["index"] == 0x84:
            # 停车：全部电压归零
            self.motor_v = [0.0] * len(self.motor_v)

    # ---------- 上行帧构造 ----------

    def _table_frame(self, item: dict, cmd: int) -> bytes:
        """tunable/monitor 目录帧：[index][type][tag][名长][名][单位长][单位][值?]

        str/other 值宽 0，帧到单位段即止（2026-10-25 协议行为）。
        监控项表现值由 _sim_value 现算（目录帧带"当前值"，与固件 append_value 同）。
        """
        name_b = item["name"].encode("utf-8")
        unit_b = (item.get("unit") or "").encode("utf-8")
        payload = bytearray([item["index"], int(item["type"]), item.get("tag", 0), len(name_b)])
        payload += name_b
        payload.append(len(unit_b))
        payload += unit_b
        if value_size(item["type"]) > 0:
            v = item.get("value")
            if v is None:
                v = self._sim_value(item, time.monotonic())
            payload += encode_value(item["type"], v)
        return build_frame(cmd, bytes(payload))

    def _cmd_table_frame(self, item: dict) -> bytes:
        """命令目录帧：[内容长度2B][命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[bit7=1时:(min,max)×N]"""
        name_b = item["name"].encode("utf-8")
        content = bytearray([item["index"], item["tag"], len(name_b)])
        content += name_b
        content.append(len(item["fields"]))
        for f in item["fields"]:
            fb = f["name"].encode("utf-8")
            content += bytes([int(f["type"]), len(fb)])
            content += fb
        if item["tag"] & CMD_TAG_RANGE:
            for f in item["fields"]:
                if value_size(f["type"]) == 0:
                    continue
                content += encode_value(f["type"], f["min"])
                content += encode_value(f["type"], f["max"])
        return build_frame(
            CmdPost.CMDDirectory,
            len(content).to_bytes(2, "little") + bytes(content),
        )

    def _key_table_frame(self, item: dict) -> bytes:
        """按键目录帧：[index][名长][名]（无类型/单位/值，名长 1~16，UTF-8）"""
        name_b = item["name"].encode("utf-8")
        return build_frame(
            CmdPost.KeyDirectory, bytes([item["index"], len(name_b)]) + name_b
        )

    def _value_frame(self, pack: list[dict], t_ms: int) -> bytes:
        """监控值帧：[payloadlen][time 2B]([index][值])×N（项数/值宽都不单给）"""
        body = bytearray([t_ms & 0xFF, (t_ms >> 8) & 0xFF])
        for e in pack:
            body.append(e["index"])
            body += encode_value(e["type"], e["value"])
        return build_frame(CmdPost.MonitorValue, bytes([len(body)]) + bytes(body))

    def _echo_frame(self, item: dict) -> bytes:
        """调参回显：[type][index][value]（type 前缀自带宽度，解帧不查表）。

        值按真实宽度编（encode_value，不是 SetTunable 那种 8B 定长）——
        解帧方 expect = 2 + value_size(type)，编成 8B 会被当坏校验整帧丢弃。
        """
        return build_frame(
            CmdPost.TunableEcho,
            bytes([int(item["type"]), item["index"]]) + encode_value(item["type"], item["value"]),
        )

    # ---------- 10ms 拍 ----------

    def tick(self) -> None:
        """一拍：Pong → 回显 → 一个表的目录一批 → 监控值 → Response。

        发出顺序与固件 tx_tick 一致（Pong 最优先——看门狗不能等；同一拍只发
        一个表的一批，tunable 优先——目录不并行；Response 兜底防堵死值帧）。
        """
        now = time.monotonic()

        # 链路巡视：300ms 没见 Ping 就停推（Ping 恢复续传）
        if now - self.last_ping_at > self.LINK_PATROL_S:
            self.link_ok = False

        # ① Pong
        if self.pong_q:
            self._emit(self.pong_q.pop(0))

        # ② 调参回显
        for _ in range(self.ECHO_PER_TICK):
            if not self.echo_q:
                break
            self._emit(self.echo_q.pop(0))

        # ③ 目录 burst：一拍一张表的一批；末批当拍回 OK + 1B 表内总项数
        for key in ("tunable", "monitor", "cmd", "key"):
            d = self.tables[key]
            if not d["q"]:
                continue
            for _ in range(min(self.TICK_TABLE_BATCH, len(d["q"]))):
                item = d["q"].pop(0)
                if key == "cmd":
                    self._emit(self._cmd_table_frame(item))
                elif key == "key":
                    self._emit(self._key_table_frame(item))
                else:
                    post = CmdPost.TunableDirectory if key == "tunable" else CmdPost.MonitorDirectory
                    self._emit(self._table_frame(item, post))
            if not d["q"] and d["pending"]:
                d["pending"] = False
                # 传完通知：仅 OK、无回显（对该 cmd 全部生效），触发上位机完整性对照
                self.resp_q.append(build_response(d["cmd"], b"", RESP_OK))
            break  # 一拍一张表

        # ④ 监控值：FAST 每拍；SLOW 按 8 相轮转（每项 80ms）
        if self.link_ok:
            self._simulate(now)
            t_ms = int((now - self.t0) * 1000) & 0xFFFF
            pack = []
            for it in self.mon:
                tag = it.get("tag", 0)
                if not (tag & MONITOR_TAG_SUB):
                    continue
                if not (tag & MONITOR_TAG_FAST) and self.tick_count % self.SLOW_PHASES != it["index"] % self.SLOW_PHASES:
                    continue
                pack.append({"index": it["index"], "type": it["type"], "value": self._sim_value(it, now)})
            for i in range(0, len(pack), self.MON_PACK_MAX):
                self._emit(self._value_frame(pack[i : i + self.MON_PACK_MAX], t_ms))

        # ⑤ Response 队列
        for _ in range(self.RESP_PER_TICK):
            if not self.resp_q:
                break
            self._emit(self.resp_q.pop(0))

        self.tick_count += 1

    # ---------- 模拟物理量 ----------

    def _simulate(self, now: float) -> None:
        t = now - self.t0
        # 转速一阶滞后跟随指令电压（时间常数约 200ms）
        for i in range(len(self.motor_rpm)):
            target = self.motor_v[i] * 100.0
            self.motor_rpm[i] += (target - self.motor_rpm[i]) * 0.05
        self.pose_y += 0.0008 * math.sin(t / 2.3)

    def _sim_value(self, it: dict, now: float) -> float | int:
        t = now - self.t0
        name = it["name"]
        if name == "位姿X":
            return round(self.pose_x, 3)
        if name == "位姿Y":
            return round(self.pose_y, 3)
        if name == "航向角":
            return round(45 * math.sin(t / 5) * 10) / 10
        if name == "CPU占用":
            return round((35 + 20 * math.sin(t / 7)) * 10) / 10
        if name == "错误码":
            return int(t / 10) % 100
        if name == "电池电压":
            return round((7.6 + 0.2 * math.sin(t / 9)) * 100) / 100
        if name == "电池电量":
            # 缓慢起伏的剩余电量（74~82%），演示用
            return round((78 + 4 * math.sin(t / 25)) * 10) / 10
        if name in ("左0电压", "右0电压"):
            return round(self.motor_v[0 if name == "左0电压" else 3] * 100) / 100
        # 左/右 0~2 转速
        side, n = name[0], int(name[1])
        base = 0 if side == "左" else 3
        return round(self.motor_rpm[base + n] * 10) / 10


