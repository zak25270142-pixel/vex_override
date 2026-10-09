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
v.sub("x")                 # 登记要记录的项（顺带提升为高速推送）
v.sub("yaw", "push", fast=True)   # 只订阅看值的项用 push
v.start_log()              # 开始记录，以此刻为时间轴零点
v.cmd("直行", 0.1)         # 参数个数以实车命令目录 fields 为准，必须全给
v.sleep(1.0)               # 可被上位机停车打断
v.end_log()
v.print_log()              # JSON + CSV 落到 pytest/
v.stop()
```

### 写新脚本：最短能到 14 行

三件便利设施：`with BackendLink() as bl`（退出自动收车收尾）、`bl.motors`（电机三项中文名，
不用记命名约定）、`bl.sweep(...)`（"设值→等稳定→取后窗统计"的扫描原语）。

```python
import sys; sys.path.insert(0, r"backend")
from app.script_api import BackendLink

with BackendLink() as bl:
    ids = bl.motors["L0"]          # {'vact':'左0实际电压','rpm':'左0转速','cur':'左0电流'}
    bl.sub(ids.values())           # 登记要记录的项
    bl.start_log()
    rows = []
    for row in bl.sweep([0.3, 0.6, 0.9, 1.2], lambda v: bl.set_volt(0, v),
                        list(ids.values()), 0.4, last_frac=0.6):
        rows.append({"v": row["value"], "rpm": row["stats"][ids["rpm"]]["abs_mean"]})
    bl.save_csv("pytest/l0_start.csv", rows)
```

`sweep` 逐档给出 `{"value": 值, "t": 记录轴时刻, "stats": {中文名: {n,mean,abs_mean,...}}}`，
想中途停止直接 `break`（比如"一旦转起来就停"）。它就是生成器，一次遍历同时打印和攒 CSV。

### 记录会话：脚本只管发命令，记录由后端干

脚本决定"记哪些项、何时开始、存哪里"，中间只负责下达命令；取数据时按
**相对开始记录的秒** 划区间，不用对齐本地时钟（命令响应里直接带回打点 `t`）。

```python
v.sub(["左0转速", "左0实际电压"])
v.start_log()
for volt in (0.5, 1.0, 1.5):
    t = v.cmd("设置电机电压", 0, volt)["t"]       # 打点：这一刻在记录轴上
    v.sleep(0.4)
    st = v.stats(["左0转速"], since=t, last_frac=0.55)["items"][0]
    print(volt, st["n"], st["mean"], st["abs_max"])

v.stats(["左0转速"], tail=1.0, thresholds=[5.0])  # tail=最近1秒；thresholds 给占比
v.wave(["左0转速"], since=t)                       # 原始长表，自己画图/落盘用
v.print_log("pytest/run.csv")                      # 导出 JSON + CSV
```

- `stats` 字段：`n / mean / abs_mean / min / max / abs_max / std / rate_hz / ratio_ge`
  （`ratio_ge` 是 `|值| >= 阈值` 的样本占比；无样本时 `n=0`，其余为 `None`）
- 窗口优先级：`tail`（最近 N 秒）> `last_frac`（窗口后百分之几）> `since/until` > 全窗
- **记录缓冲上限 50 万条，超了丢新样**。长实验请在每个测点/每轮开头再调一次
  `start_log()`（会清缓冲并重置零点），别让一次记录跨几十万条样本。

### 现成脚本（`scripts/`）

均通过 `app.script_api.BackendLink` 走后端 HTTP（脚本构造即就绪；数据由后端记录，
脚本只发命令 + 按区间取统计），不再直连串口；先启动后端再运行：

| 脚本                    | 用途                                                           |
| ----------------------- | -------------------------------------------------------------- |
| `speed_loop_probe.py`   | 速度环标定：开环电压特性 + 闭环增益扫掠（带位移围栏/限速保护） |
| `motor_speed_step.py`   | 速度环阶跃响应（0x85 闭环）：上升时间/超调/稳态误差            |
| `motor_check.py`        | 逐电机体检：3V/0.5s 判堵转/空转/断线                           |
| `motor_v_test.py`       | 单电机开环电压特性：升压/降压扫描 + 高压点检                   |
| `motor_start_repeat.py` | 静止启动电压重复试验（随机化齿槽停位）                         |
| `neutral_curve.py`      | 60:48 齿轮组零扭矩电压曲线                                     |
| `wave_dump.py`          | 把 probe 波形 CSV 按时间轴对齐打印                             |
| `example_template.py`   | 最小模板：sub → start_log → cmd → end_log → print_log          |

## 订阅两种语义

| kind   | 当前值           | 历史记录 | 用途                                             |
| ------ | ---------------- | -------- | ------------------------------------------------ |
| `push` | ✅ 实时更新      | ❌       | **只留当前值不留历史**：看门狗读 x/y/yaw，砍带宽 |
| `log`  | ✅（自动提高速） | ✅       | start/end_log 出波形；取消登记不关推送           |
| `both` | ✅               | ✅       | 边看门狗边记录                                   |

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
- `cmd` 参数个数必须与命令目录完全一致，少参/多参都会 400（不再静默补 0）；目录里带取值范围的字段（tag bit7）还会校验上下限，越界同样 400
- 心跳与重连：后端约 1s 一发 Ping；下位机约 2.5s 收不到 Ping 会停推监控（link_ok）；后端 5s 收不到 Pong 判定串口掉线并自动重连（重连后自动重拉目录、补发订阅）
- 目录缓存：连上后自动拉三张表并按 index 缓存原始帧，多客户端重复请求不打车；怀疑缓存过期/半表时 `POST /api/dirs/refresh` 强制刷新（或脚本里 `v.refresh_dirs()`）
- 脚本退出：曾 `sub("push"/"both")` 的项应显式 `unsub`，或 finally 里 `v.clear_script_subs()` 一键清掉 script 侧订阅意图
- 不内置看门狗，按测试场景自行在脚本里超时 `v.stop()`
