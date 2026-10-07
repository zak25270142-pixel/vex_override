"""
脚本友好封装（HTTP 模式）：测试脚本只跟本类打交道，不碰帧和串口。

典型用法：
    from app.script_api import VexScript

    v = VexScript()
    v.wait_connected()
    v.wait_dirs()
    v.sub("x", "log")          # 后端登记记录，自动提高速
    v.sub("yaw", "push", fast=True)
    v.start_log()
    for i in range(50):
        v.cmd("直行", 0.1)
        v.sleep(0.1)           # 可被上位机停车打断
    v.end_log()
    v.print_log()              # JSON + CSV 落到 pytest/
    v.stop()
"""

from __future__ import annotations

import json
import time
from typing import Any, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


class VexScript:
    def __init__(self, base: str = "http://127.0.0.1:8000", timeout: float = 5.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ---------- 内部 ----------

    def _get(self, path: str, **params):
        if requests is None:
            raise RuntimeError("请先 pip install requests")
        r = requests.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, body: Optional[dict] = None):
        if requests is None:
            raise RuntimeError("请先 pip install requests")
        r = requests.post(f"{self.base}{path}", json=body or {}, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text}")
        return r.json()

    # ---------- 连接 / 目录 ----------

    def status(self) -> dict:
        return self._get("/api/status")

    def wait_connected(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.status().get("connected"):
                    return True
            except Exception:
                pass  # 后端还没起来时连着试
            time.sleep(0.3)
        return False

    def wait_dirs(self, timeout: float = 5.0) -> bool:
        """等到监控表或命令表到位。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.status()
            if s.get("monitor_count", 0) > 0 or s.get("cmd_count", 0) > 0:
                return True
            time.sleep(0.2)
        return False

    def get_column(self, type: str = "monitor") -> List[dict]:
        """拉目录：type = monitor(实参) | tunable(调参) | cmd(命令)，JSON。"""
        return self._get("/api/column", type=type)

    def values(self) -> List[dict]:
        return self._get("/api/values")

    def read(self, *index_or_name):
        """
        读监控项当前值（看门狗用，index int 或中文名 str 均可）：
          v.read("x")                → 直接返回数值，如 0.123
          v.read(5)                  → 也可传 index
          v.read("x", "y", "yaw")   → {"x":.., "y":.., "yaw":..}（一次请求取多项）
        前提：该项已 sub(..., "push"/"log"/"both")，值才会持续更新。
        要看数据新鲜度用 read_item()（返回里有 age_s）。
        """
        if not index_or_name:
            raise ValueError("read 至少给一个 index 或名字")
        # requests 对 list 值自动展开成重复 query 键：index_or_name=x&index_or_name=y
        items = self._get("/api/value", index_or_name=list(index_or_name))
        if len(items) == 1:
            return items[0]["value"]
        return {it["name"]: it["value"] for it in items}

    def read_item(self, index_or_name) -> dict:
        """读单项完整信息：{index,name,value,unit,age_s}，age_s 为距上次推送秒数。"""
        return self._get("/api/value", index_or_name=index_or_name)[0]

    # ---------- 停车 / 中止 ----------

    def stop(self) -> dict:
        """紧急停车：最高优先级，清脚本队列并置 abort（也会打断其他在跑的脚本）。"""
        return self._post("/api/stop")

    def clear_abort(self) -> dict:
        """停车后继续跑新脚本前先清 abort。"""
        return self._post("/api/abort/clear")

    # ---------- 订阅 ----------

    def sub(
        self,
        index_or_name,
        type: str = "both",
        *,
        fast: Optional[bool] = None,
    ) -> dict:
        """
        订阅/登记，index_or_name 可传监控 index(int) 或目录中文名(str)：
          type="push" 只改下位机数据推送档位（砍带宽），fast=True 高速 / False 低速
          type="log"  只在后端登记记录，登记时自动把该项提升为高速推送
          type="both" 推送 + 记录都要
        """
        return self._post(
            "/api/sub",
            {"index_or_name": index_or_name, "type": type, "on": True, "fast": fast},
        )

    def unsub(self, index_or_name, type: str = "both") -> dict:
        """退订/取消登记（取消 log 登记不会顺带关下位机推送，以免影响上位机）。"""
        return self._post(
            "/api/sub",
            {"index_or_name": index_or_name, "type": type, "on": False},
        )

    # ---------- 命令 ----------

    def cmd(self, index_or_name, *args) -> dict:
        """
        下发动作命令：index_or_name 为命令字(int)或命令目录中文名，
        参数按目录字段顺序依次给（缺省补 0）。abort 状态下调用直接抛错。
        """
        return self._post(
            "/api/cmd",
            {"index_or_name": index_or_name, "args": list(args)},
        )

    def set_tunable(self, index_or_name, value) -> dict:
        """写调参项（下位机回显后上位机表格自会更新）。"""
        return self._post(
            "/api/tunable",
            {"index_or_name": index_or_name, "value": value},
        )

    # ---------- 日志 ----------

    def start_log(self) -> dict:
        return self._post("/api/log/start")

    def end_log(self) -> dict:
        return self._post("/api/log/end")

    def print_log(self, path: Optional[str] = None) -> dict:
        """结束后导出：JSON 全量记录 + CSV 长表写到后端 pytest/ 目录，并在本机打印摘要 JSON。"""
        info = self._post("/api/log/print", {"path": path})
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return info

    # ---------- 时序 ----------

    def sleep(self, seconds: float) -> None:
        """可被停车打断的等待：abort 置位后最多 0.05s 内抛 RuntimeError。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.status().get("abort"):
                raise RuntimeError("已紧急停车 / 脚本被中止")
            time.sleep(min(0.05, end - time.monotonic()))
