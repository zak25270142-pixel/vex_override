# VEX Python Backend

插在 Vue3 上位机与 V5 主控之间的 FastAPI 服务：

- **WebSocket `/ws`**：串口上行帧原样二进制透传给上位机（前端 FrameParser 零改动），上位机下行帧高优先级转发；停车帧自动插队并中止脚本
- **HTTP `/api/*`**：脚本友好接口（停车、拉目录、订阅、下发命令、记录日志）
- 串口独占（pyserial），协议与 `frontend/src/services/protocol.ts`、`include/communication.h` 一致

## 启动

```powershell
cd backend
pip install -r requirements.txt
# 可选：指定串口，不设则自动 Ping 探测 User 口
$env:V5_PORT = "COM5"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

接口文档：<http://127.0.0.1:8000/docs>

## 上位机切到后端模式

前端顶栏勾选「后端」（连接目标变为 `ws://127.0.0.1:8000/ws`）。
注意：同一时刻只有一方能占串口——后端跑着的时候浏览器不要再直连 Web Serial。

## 脚本

```python
from app.script_api import VexScript

v = VexScript()
v.wait_connected(); v.wait_dirs()
v.sub("x", "log")          # 后端登记记录，自动提高速档
v.sub("yaw", "push", fast=True)
v.start_log()
v.cmd("直行", 0.1)
v.sleep(1.0)               # 可被上位机停车打断
v.end_log()
v.print_log()              # JSON + CSV 落到 pytest/
v.stop()
```

## 订阅两种语义

| kind | 当前值 | 历史记录 | 用途 |
|------|--------|---------|------|
| `push` | ✅ 实时更新 | ❌ | **只留当前值不留历史**：看门狗读 x/y/yaw，砍带宽 |
| `log`  | ✅（自动提高速） | ✅ | start/end_log 出波形；取消登记不关推送 |
| `both` | ✅ | ✅ | 边看门狗边记录 |

## 读当前值（看门狗）

```python
v.read("x")                  # 直接拿数值，index 或中文名均可：v.read(5)
v.read("x", "y", "yaw")     # 多项一次请求 → {"x":.., "y":.., "yaw":..}
v.read_item("x")            # 完整信息：value/unit/age_s（age_s 判断数据是否断流）
```

典型看门狗（30ms 周期）：

```python
v.sub("x", "push", fast=True)
while ...:
    if abs(v.read("x")) > 0.25 or v.read_item("x")["age_s"] > 0.2:
        v.stop()
    time.sleep(0.03)
```

## 安全约定

- 停车（上位机停车按钮或 `/api/stop`）优先级最高：立即发停车帧、清空脚本待发队列、置 abort，脚本的 `cmd()`/`sleep()` 随即抛错中止
- 不内置看门狗，按测试场景自行在脚本里超时 `v.stop()`
