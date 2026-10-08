#!/usr/bin/env python3
"""
测试脚本模板：sub → start_log → 循环 cmd → end_log → print_log → stop
运行前先启动后端：
    cd backend
    uvicorn app.main:app --host 127.0.0.1 --port 8000
然后：
    python scripts/example_template.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.script_api import VexScript  # noqa: E402


def main() -> None:
    v = VexScript()  # 默认连 http://127.0.0.1:8000

    if not v.wait_connected(15):
        print("后端未连上 V5：", v.status())
        sys.exit(1)
    v.wait_dirs(8)
    v.clear_abort()

    # 先看目录里都有什么，名称以实车 JSON 为准
    print("监控项:", [(x["index"], x["name"]) for x in v.get_column("monitor")])
    print("命令:", [(hex(x["index"]), x["name"]) for x in v.get_column("cmd")])

    # 订阅：
    #   push = 只要下位机推当前值（不记历史），看门狗读 x/y/yaw 用这个
    #   log  = 后端登记记录并自动提高速；both = 推送+记录
    v.sub("x", "push", fast=True)
    v.sub("y", "push", fast=True)
    v.sub("yaw", "push", fast=True)
    for name in ("左轮转速", "右轮转速"):  # 名称以实车 get_column("monitor") 为准
        try:
            v.sub(name, "log")
        except Exception as e:
            print("skip", name, e)

    # 示范命令：参数个数按目录 fields 填（后端严格校验，少参/多参都报错）
    cmds = v.get_column("cmd")
    demo = next((c for c in cmds if c.get("name") == "直行"), None)
    n_args = len(demo.get("fields") or []) if demo else 0

    v.start_log()
    try:
        for _ in range(5):
            if demo is not None:
                # 0.1 仅作模板占位，实车请改成合理值
                v.cmd("直行", *([0.1] * n_args))
            # 看门狗：脉冲期间反复读当前值，位移/数据超时立即收车
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.0:
                try:
                    pos = v.read("x", "y", "yaw")  # 一次请求取多项 → {"x":..,"y":..,"yaw":..}
                    drift = (pos["x"] ** 2 + pos["y"] ** 2) ** 0.5
                    if drift > 0.25 or v.read_item("x")["age_s"] > 0.2:
                        raise RuntimeError(f"看门狗触发：位移 {drift:.3f}m 或数据断流")
                except KeyError:
                    pass  # 目录里没有 x/y/yaw 时跳过看门狗
                time.sleep(0.03)  # 检查周期，v.sleep 没必要：这里本来就要持续轮询
    except RuntimeError as e:
        print("脚本被中止:", e)
        v.stop()
    finally:
        v.end_log()
        v.print_log()  # JSON + CSV 写到 backend/pytest/


if __name__ == "__main__":
    main()
