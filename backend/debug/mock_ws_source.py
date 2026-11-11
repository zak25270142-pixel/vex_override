"""WS 假固件数据源：独立进程，主动连后端的 WS 设备链路（默认 8889），冒充一台真 V5。

用法（先起后端，再起本进程；后端三探测会自动选走 WS）：
    .\\.venv\\Scripts\\python -m uvicorn app.main:app --port 8000
    .\\.venv\\Scripts\\python -m debug.mock_ws_source
    .\\.venv\\Scripts\\python -m debug.mock_ws_source ws://127.0.0.1:8889 30

帧就是后端与固件之间那套 A5|Cmd|Payload|XOR 二进制协议，装进 WS 二进制消息。
后端每次 send 只发一帧（Hub 的 tx 队列逐帧出队），所以这里把一条消息当一帧解；
若以后后端改成一消息多帧，这里要换成按帧头扫描的拆帧逻辑（见 udp_fake_device）。

设备侧逻辑全部复用 debug/mock_v5.py 的 _MockV5Core：四张目录、订阅回执、
100Hz 监控推送、Ping/Pong、停车回执——和真固件同一套应答。

断线行为：后端重启/断开 → 本进程自动每秒重连一次；被 Ctrl+C 或 run_s 到点才退出。
真车时不会启动本进程：它只是 debug/ 下的调试工具，不在后端运行路径上。
"""

import sys
import time

from websockets.sync.client import connect

from app.config import CONFIG
from app.protocol import build_frame
from debug.mock_v5 import _MockV5Core

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8889"
RUN_S = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0  # 0 = 一直跑

MIN_FRAME_LEN = 3
'''最短合法帧：帧头 + 命令字 + 校验（Ping）'''

TICK_S = 0.01
'''模拟固件 10ms 拍'''

RECV_TIMEOUT_S = 0.005
'''一次 recv 最多等多久，剩下的时间留给 tick'''

RECONNECT_S = 1.0
'''连不上后端时的重试间隔'''


def _decode(msg: bytes) -> tuple[int, bytes] | None:
    """一条 WS 消息 → (cmd, payload)；只认单帧，格式不对就丢。"""
    if len(msg) < MIN_FRAME_LEN or msg[0] != CONFIG.frame_head:
        return None
    return msg[1], msg[2:-1]


def serve(ws, deadline: float | None) -> None:
    """接上后一直答题：收一帧喂内核，内核出帧立刻发回。"""
    core = _MockV5Core(lambda frame: ws.send(frame))
    next_tick = time.monotonic()
    while deadline is None or time.monotonic() < deadline:
        try:
            msg = ws.recv(timeout=RECV_TIMEOUT_S)
        except TimeoutError:
            msg = None
        if msg is not None:
            frame = _decode(msg)
            if frame is not None:
                core.receive(build_frame(frame[0], frame[1]))
        now = time.monotonic()
        if now >= next_tick:
            core.tick()
            # 落后超过一拍就只补一拍，不追帧（真固件也只跑当前拍）
            next_tick = now + TICK_S


def main() -> None:
    deadline = None if RUN_S <= 0 else time.monotonic() + RUN_S
    while deadline is None or time.monotonic() < deadline:
        try:
            with connect(
                URL,
                proxy=None,
                compression=None,
                ping_interval=None,
                max_size=None,
                open_timeout=3,
            ) as ws:
                print(f"已接入后端 {URL}", flush=True)
                serve(ws, deadline)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"连接/收发失败: {e}，{RECONNECT_S}s 后重试", flush=True)
            if deadline is not None and time.monotonic() + RECONNECT_S >= deadline:
                break
            time.sleep(RECONNECT_S)
        else:
            print("连接已关闭，重试", flush=True)
            time.sleep(RECONNECT_S)


if __name__ == "__main__":
    main()
