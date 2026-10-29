"""
Mock V5：没有真车/真固件时，在后端里模拟一台讲本协议的 V5 主控。

为什么在 backend 侧而不是小程序/浏览器里造帧：
  - 上位机（小程序/网页）连的是 WS，mock 顶替的是"串口后面的那台车"——
    WS 透传、status/alert 带外消息、订阅 diff、停车插队、重连全部走真实路径，
    客户端演示模式把这些全绕掉了，测了个寂寞
  - 一台 mock 同时服务小程序和网页两个上位机（各开各的 WS，订阅互不干扰）
  - 模拟逻辑与 protocol.py 的构建器同源，不复制帧格式知识

怎么用：
    set V5_MOCK=1
    .\\.venv\\Scripts\\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  或 PowerShell：  $env:V5_MOCK=1; ...uvicorn...
  连上后行为与真机一致：拉三张表（burst + OK 项数）、订阅推送 100Hz/80ms、
  调参回显、命令回执（原地转向永远 BUSY 拒绝，演示拒绝 toast）、80ms 心跳。

实现方式：MockV5Bridge 继承 SerialBridge，只换"字节怎么进出设备"这一层
（connect/send/disconnect/connected/list_ports），收数侧的 on_raw 广播 +
FrameParser 喂帧 + 心跳超时判定全部复用父类——父类的心跳线程 self.send(ping())
打进来的 Ping 由本类的 send 转交模拟主控，Pong 原路回去，_last_pong 自然更新，
即"mock 也会掉线"这条真机行为不用特殊代码就成立了（停发 Ping 300ms 即停推）。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from .dataclass import PortInfo
from .protocol import (
    CMD_ACTION_MAX,
    CMD_ACTION_MIN,
    CMD_TAG_ARGC_SHIFT,
    CMD_TAG_RANGE,
    CmdGet,
    CmdPost,
    CmdSpec,
    FRAME_HEAD,
    MONITOR_TAG_FAST,
    MONITOR_TAG_GETTER,
    MONITOR_TAG_SUB,
    RESP_OK,
    RespErrReject,
    RespErrRetry,
    ValueType,
    build_frame,
    build_response,
    decode_value,
    encode_value,
    tag_fast,
    tag_subscribed,
    value_size,
)
from .serial_bridge import VEX_VID, SerialBridge, Status

logger = logging.getLogger("vex.mock")

# 监控项语义种类（tag & MONITOR_TAG_KIND_MASK，与 my_main.h 的 MENU_TAG_KIND_* 一致；
# protocol.py 未导出枚举，这里按位值用）
_KIND_POS_X = 1
_KIND_POS_Y = 2
_KIND_YAW = 3

_TAG_SUB = MONITOR_TAG_SUB
_TAG_FAST = MONITOR_TAG_FAST
_TAG_GETTER = MONITOR_TAG_GETTER
_VT = ValueType
# str 类型编号 10（与 my_main.h VALUE_TYPE 一致）。protocol.py 的 ValueType
# 枚举没列 Str/Other 成员（TS 端有），但 value_size 对未列类型默认返回 0，
# 行为等价；这里用原始值，不为此改协议文件
_VT_STR = 10

# 拒绝/补发类 status 字节 = 类别位 | err号。注意 protocol.py 的 RespErrReject /
# RespErrRetry 成员值是 err 号（0~3）而不是完整 status 字节——直接拿成员值当
# status 用会编出 0x03 这种"需补发+串模式"的错字节（本 mock 第一版就踩了）
_RESP_UNKNOWN_CMD = 0x40 | int(RespErrRetry.UnknownCmd)
_RESP_INDEX_RANGE = 0xC0 | int(RespErrReject.IndexRange)
_RESP_TYPE_UNWRITABLE = 0xC0 | int(RespErrReject.TypeUnwritable)
_RESP_BUSY = 0xC0 | int(RespErrReject.Busy)

# ---- 模拟的三张表（结构照实车：tag 位域/类型/单位都按协议给） ----
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
    # str 类型：值宽 0，目录帧到单位段即止；不可订阅/写入，UI 登记为只读
    {"index": 8, "type": _VT_STR, "name": "设备备注", "unit": "", "value": ""},
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
    """模拟主控内核：三张表 + 10ms 拍 + 下行帧处理。

    与 firmware USB_Comm 的行为子集同构（逐条注释标了对应处），上行帧用
    protocol.py 的构建器组装。线程模型：receive() 在 Hub 的 tx/心跳线程里跑
    （入队/改表），tick() 在 mock 自己的 10ms 线程里跑（出帧/算值）——
    队列是 list，append/pop(0) 在 GIL 下原子，表的读写同理；演示够用，
    真机路径的并发纪律在 SerialBridge 侧，不归本类。
    """

    TICK_DIR_BATCH = 8  # 固件 DIR_BATCH：每拍最多几项（一项一帧）
    MON_PACK_MAX = 16  # 固件 MON_PACK_MAX
    SLOW_PHASES = 8  # 固件 SLOW_PHASES
    LINK_PATROL_S = 0.3  # 固件 LINK_PATROL_TICKS=30 拍：300ms 无 Ping 停推
    ECHO_PER_TICK = 1
    RESP_PER_TICK = 2

    def __init__(self, emit: Callable[[bytes], None]) -> None:
        """
        emit: 上行帧出口（MockV5Bridge._emit：on_raw 广播 + FrameParser 喂帧）
        """
        self._emit = emit
        self.tick_count = 0

        self.tun = _clone(_MOCK_TUNABLES)
        self.mon = _clone(_MOCK_MONITORS)
        self.cmd = _clone(_MOCK_COMMANDS)

        # 三张表各一条目录倒计时（对应固件 dir_tun/dir_mon/dir_cmd）：
        # q=待发项、pending=末批是否要回 OK 项数、cmd/echo/total=回执用
        self.dir: dict[str, dict] = {
            "tunable": {"q": [], "pending": False, "cmd": 0, "echo": b"", "total": 0},
            "monitor": {"q": [], "pending": False, "cmd": 0, "echo": b"", "total": 0},
            "cmd": {"q": [], "pending": False, "cmd": 0, "echo": b"", "total": 0},
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
        """收一帧下行帧（Hub/心跳线程经 MockV5Bridge.send 转交）。

        每次 send 就是一整帧（上位机组帧后整帧下发），无需粘包状态机；
        但仍做帧头/XOR 校验，坏帧静默丢——与真实固件 rx 状态机一致。
        """
        if len(frame) < 3 or frame[0] != FRAME_HEAD:
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
        if cmd in (CmdGet.RequestTunable, CmdGet.RequestMonitor, CmdGet.RequestCommand):
            self._on_dir_request(cmd, p)
            return
        if cmd == CmdGet.Subscribe:
            self._on_subscribe(cmd, p)
            return
        if cmd == CmdGet.SetTunable:
            self._on_set_tunable(cmd, p)
            return
        if CMD_ACTION_MIN <= cmd <= CMD_ACTION_MAX:
            self._on_action(cmd, p)
            return
        # 其余命令字：未注册
        self.resp_q.append(build_response(cmd, p, _RESP_UNKNOWN_CMD))

    def _on_dir_request(self, cmd: int, p: bytes) -> None:
        """目录请求 [len 1B][index...]：len=0 全量；非0=只补发这些下标。

        与固件 req_list_ok/req_register 同规则：超 REQ_LIST_MAX 或下标越界
        → 整帧拒绝（回显原话 payload），主机端应改走全量重拉。
        """
        key = (
            "tunable"
            if cmd == CmdGet.RequestTunable
            else "monitor"
            if cmd == CmdGet.RequestMonitor
            else "cmd"
        )
        items = {"tunable": self.tun, "monitor": self.mon, "cmd": self.cmd}[key]
        if len(p) < 1:
            return
        n = p[0]
        if n == 0:
            queue = list(items)
            total = len(items)
        else:
            idxs = list(p[1 : 1 + n])
            if n > 16 or any(i >= len(items) for i in idxs):  # REQ_LIST_MAX
                self.resp_q.append(build_response(cmd, p, _RESP_INDEX_RANGE))
                return
            queue = [items[i] for i in idxs]
            total = n
        d = self.dir[key]
        d["q"] = queue
        d["pending"] = True
        d["cmd"] = cmd
        d["echo"] = p
        d["total"] = total

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
        """调参写入 [index][value 8B 定长]：写成功回 TunableEcho（真实生效值）。

        str/other 值宽 0 不可写（固件同款拒绝）；下标超表同样拒绝。
        """
        if len(p) < 9:
            return
        idx = p[0]
        it = self.tun[idx] if idx < len(self.tun) else None
        if it is None:
            self.resp_q.append(build_response(cmd, p, _RESP_INDEX_RANGE))
            return
        if value_size(it["type"]) == 0:
            self.resp_q.append(build_response(cmd, p, _RESP_TYPE_UNWRITABLE))
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

    def _dir_frame(self, item: dict, cmd: int) -> bytes:
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

    def _cmd_dir_frame(self, item: dict) -> bytes:
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
        for key in ("tunable", "monitor", "cmd"):
            d = self.dir[key]
            if not d["q"]:
                continue
            for _ in range(min(self.TICK_DIR_BATCH, len(d["q"]))):
                item = d["q"].pop(0)
                if key == "cmd":
                    self._emit(self._cmd_dir_frame(item))
                else:
                    post = CmdPost.TunableDirectory if key == "tunable" else CmdPost.MonitorDirectory
                    self._emit(self._dir_frame(item, post))
            if not d["q"] and d["pending"]:
                d["pending"] = False
                # OK 尾字节 = 表内总项数（长度隐式，帧长剩余；主机拿它对照补发）
                self.resp_q.append(build_response(d["cmd"], d["echo"], RESP_OK, bytes([d["total"] & 0xFF])))
            break  # 一拍一张表

        # ④ 监控值：FAST 每拍；SLOW 按 8 相轮转（每项 80ms）
        if self.link_ok:
            self._simulate(now)
            t_ms = int((now - self.t0) * 1000) & 0xFFFF
            pack = []
            for it in self.mon:
                tag = it.get("tag", 0)
                if not tag_subscribed(tag):
                    continue
                if not tag_fast(tag) and self.tick_count % self.SLOW_PHASES != it["index"] % self.SLOW_PHASES:
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


class MockV5Bridge(SerialBridge):
    """把 SerialBridge 的"串口"换成模拟主控的桥。

    只覆盖设备进出这一层；父类的 on_raw 广播、FrameParser 喂帧、Pong 时间戳
    截获、心跳线程与 PONG_TIMEOUT 判定全部复用——父类心跳 self.send(ping())
    打进来的 Ping 由本类 send 转交模拟主控，Pong 原路回去，_last_pong 自然
    更新，"停 Ping 300ms 即停推"的真机行为不用特殊代码就成立。
    """

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        on_frame: Callable[[int, bytes], None] | None = None,
        on_status: Callable[[Status, str], None] | None = None,
        on_raw: Callable[[bytes], None] | None = None,
    ) -> None:
        """签名与 SerialBridge 一致（Hub 用同一工厂创建）；port/baud 忽略。"""
        super().__init__(port=port, baud=baud, on_frame=on_frame, on_status=on_status, on_raw=on_raw)
        self._core: _MockV5Core | None = None
        self._tick_thread: threading.Thread | None = None

    # ---------- 连接 ----------

    def connect(self, timeout: float = 0.12) -> bool:
        """演示模式无需探测：立即"连上"，起 10ms 拍线程。"""
        if self.connected:
            return True
        self._connected = True
        self._stop.clear()
        self._core = _MockV5Core(self._emit)
        self._tick_thread = threading.Thread(target=self._tick_loop, name="vex-mock-tick", daemon=True)
        self._tick_thread.start()
        self.on_status(Status.connected, "演示模式：模拟 V5 已连接（V5_MOCK=1）")
        logger.info("MockV5Bridge 已连接（V5_MOCK=1，无真车）")
        return True

    def disconnect(self) -> None:
        """停心跳 → 置停止 → 收拍线程 → 报 idle。"""
        self.stop_heartbeat()
        self._stop.set()
        self._connected = False
        t = self._tick_thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=1.0)
        self._tick_thread = None
        self._core = None
        self.on_status(Status.idle, "演示模式已断开")

    @property
    def connected(self) -> bool:
        """mock 的"在线"就是标志位（没有真端口可查）。"""
        return self._connected

    # ---------- 收发 ----------

    def send(self, data: bytes) -> None:
        """写一帧：转交模拟主控（父类的写串口在这里换成喂 mock）。"""
        if not self._connected or self._core is None:
            raise RuntimeError("演示模式未连接")
        self._core.receive(bytes(data))

    def _emit(self, frame: bytes) -> None:
        """模拟主控发出的一帧：走与父类 _rx_loop 完全相同的两个消费者。"""
        self.on_raw(frame)
        self._parser.feed(frame, self._on_frame)

    def _tick_loop(self) -> None:
        """10ms 拍线程（可中断 sleep：disconnect 不用等满一拍）。"""
        while not self._stop.is_set():
            try:
                if self._core is not None:
                    self._core.tick()
            except Exception as e:  # pragma: no cover - 演示源不该崩，崩了也别带走线程
                logger.exception("mock tick 异常: %s", e)
            self._stop.wait(0.01)

    # ---------- 端口枚举 ----------

    @staticmethod
    def list_ports() -> list[PortInfo]:
        """演示模式报一个假端口（/api/ports 与 /api/status 的 ports 字段）。"""
        return [
            PortInfo(
                device="MOCK-V5",
                description="Mock V5 (演示数据源, V5_MOCK=1)",
                vid=VEX_VID,
                pid=0x0000,
                serial_number="MOCK",
            )
        ]
