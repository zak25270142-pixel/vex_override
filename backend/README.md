# VEX Python Backend

插在 Vue3 上位机与 V5 主控之间的 FastAPI 服务：

- **WebSocket `/ws`**：设备上行帧原样二进制透传给上位机（前端 FrameParser 零改动），上位机下行帧高优先级转发；停车帧走独立队列（不进 UDP 攒包窗口），同时清空脚本待发队列并置 abort
- **HTTP `/api/*`**：脚本友好接口（停车、拉目录、订阅、下发命令、记录日志）
- 三条链路（串口 / WiFi-UDP / WS）并发探测、先完成 Ping/Pong 握手者胜；pyserial 是硬依赖（模块顶层 `import serial`，没装则后端起不来）。协议与 `frontend/src/services/protocol.ts`、`include/communication.h` 一致

## 启动

```powershell
cd backend                                 # 在仓库根目录执行；若已在 backend 则跳过本行
py -3.13 -m venv .venv                      # 固定用 3.13 建（本机 py 默认是 3.14，别用 python）
.\.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
cd d:\work\vex\test1\backend
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> **`--host 0.0.0.0` 不能省**：`127.0.0.1` 只有跑后端的这台机器能连。手机小程序
> （`miniprogram/`）要连后端，手机与 PC 必须同网段（同连一个热点），且后端必须绑
> `0.0.0.0` 监听局域网。只跑浏览器上位机（同机）时绑哪个都行。

## 链路选路（固定三探测）

没有 mode 开关。后端启动即同时探测三条链路，谁先完成 Ping/Pong 握手就用谁：

- 串口（V5 User 口）：逐候选口发 Ping，回 Pong 的才是 User 口
- WiFi（UDP `8888`，可用 `V5_UDP_HOST` / `V5_UDP_PORT` 改）：靠上行帧发现对端
- WS（`V5_WS_HOST` / `V5_WS_PORT`，默认 `0.0.0.0:8889`）：bridge 自己监听，
  外部设备 / 调试进程主动接进来；不占用 8000（后端 HTTP + 前端 `/ws`）

选中者失联会自动回到三探测，其余链路可接管；三条链路互斥，未选中时会释放
串口句柄 / UDP 端口 / WS 端口。

下行发送：串口 / WS 直写、不攒包也不加发送间隔；UDP 由桥内独立 tx 线程攒包
（窗口 / 单包字节 / 回执帧上限见 `app/config.json`，整帧不拆分、装不下
顺延下一窗口），数据报之间至少间隔 `min_gap_ms`。细节见
`docs/代码补充注释/07-Backend-传输与回执.md`。

## 配置（app/config.json）

启动时读一次（`backend/app/config.py` 的 `CONFIG`），改配置只改 JSON、重启生效。
**这份配置是上位机对下位机的适配层**：下位机千奇百怪、各家自己扩展（帧长、发现帧
约定、命令段范围都可能不同），上位机源码恒定不碰，换固件就改这里对齐。
下表前半是「协议适配」，后半是「后端运行参数」。
JSON 没有注释语法，每个字段“为什么是这个数”的推理写在 `config.py` 的字段说明里；
标了固件常量名的字段，改之前先对齐手头这台固件的 `static constexpr`，
否则改错不报错、只表现为丢包、解不出帧或掉线误判。

| 字段 | 现值 | 用途 | 改错表现 | 取值依据（固件 / 实测） |
| --- | --- | --- | --- | --- |
| `frame_head` | 165 | 帧头字节 | 所有帧被丢弃、找不到帧边界 | 固件 `FRAME_HEAD` |
| `max_payload` | 445 | 单帧 payload 上限 | 调小→合法帧被截断；调大→攒包余量/MTU 失效 | 固件 `rx/tx` 静态数组按同值分配 |
| `discovery_magic` | HDU | 0xFE 发现帧魔数 | 设备登记全丢 | 必须正好 3 字节 ASCII |
| `discovery_name_max` | 16 | 发现帧名字长度上限 | 调小→合法登记帧被丢；调大→无收益 | 固件 `DISCOVERY_NAME_MAX` |
| `discovery_len_clear` | 255 | 清除登记哨兵 | 清除帧被当超长名丢弃 | 固件 `DISCOVERY_NAME_CLEAR` |
| `req_list_max` | 48 | 目录补发 index 列表上限 | 固件整帧拒绝 | V5 `REQ_BUF_CAP=48`（池只放下标，单段上限 48）；Hi3861 `REQ_LIST_MAX=16`，连它时改填 16 |
| `cmd_action_min` / `cmd_action_max` | 128 / 159 | 动作命令段 | 命令被误判、停车失效 | 固件 `CMD_BASE` |
| `fw_lag_stale_ms` | 300 | 监控项判旧阈值 | 新鲜度误判 | 固件 `LINK_PATROL_TICKS=30` |
| `fw_lag_extrapolate_cap_ms` | 60000 | lag 外推封顶 | 回绕后旧值被当新鲜 | 16 位时间戳 65.5s 回绕 |
| `max_retries` | 3 | 一帧等不到回执时最多重发几次 | 正常报错 | — |
| `table_resend_rounds` | 3 | 目录缺项“对照→补发”最多几轮 | 正常报错 | — |
| `max_groups` | 4 | 脚本建议曲线最多几组 | 脚本侧被拒 | 前端 `CHANNELS` 4 路，组数=通道数 |
| `max_items` | 4 | 每组最多几条建议曲线 | 脚本侧被拒 | 前端 4 通道 × 5 槽，每组留 1 槽空余 |
| `max_frames` | 5000 | 一次记录最多存多少拍（100Hz 下 50s） | 记录被截断 | 100Hz × 50s；宽表 50 列约 2MB |
| `same_tick_ms` | 2.0 | 相差多少毫秒内算同一拍 | 拍被拆散或错并 | 固件 10ms 一拍，同帧共一个时间戳 |
| `min_gap_ms` | 2 | UDP 数据报最小发送间隔（ms） | 丢包变多（不报错） | 实测 448B 单包 2/3/5ms 丢 1.25%/2.75%/1.50% |
| `batch_window_s` | 0.005 | 攒包窗口（秒） | 延迟 / 包数变化 | — |
| `batch_max_bytes` | 480 | 单数据报字节上限 | 超 MTU、丢包（不报错） | `max_payload + 3`（现值 445 → 整帧 448B）；满长帧一包只放一帧 |
| `batch_max_acks` | 6 | 单数据报内需回执帧上限 | 回执被丢、重发变多 | 固件 `RESP_QUEUE_DEPTH=8`，刻意留余量 |
| `buffer_bytes` | 4194304 | PC 侧接收缓冲（字节） | 丢包（不报错） | — |
| `max_packet_bytes` | 65535 | 单次 recvfrom 上限（字节） | 大包被截断 | UDP 单报文系统硬上限；整帧按 `max_payload` 计算 |
| `heartbeat_gap_s` | 0.08 | 心跳间隔（秒） | 掉线误判（不报错） | 固件 `LINK_PATROL_TICKS=30`，300ms 窗口内至少 3 个 Ping |
| `find_timeout_s` | 0.12 | 单个候选口 / WS 等 Pong 的时长（秒） | 探测变慢或误判 | 主控 10ms 一拍即回，留十倍余量 |
| `find_retry_gap_s` | 1.0 | 探测失败后的重试间隔（秒） | 重连变慢 | — |
| `poll_gap_s` | 0.05 | UDP / WS 探测线程轮询粒度（秒） | 握手变慢 | — |
| `port_try_gap_s` | 0.05 | 串口换下一个候选口前的间隔（秒） | 探测变慢 | — |
| `udp_peer_wait_s` | 0.2 | 向一个 peer 等 Pong 的窗口（秒） | 误判 | — |
| `peer_switch_timeout_s` | 3.0 | 候选 peer 多久没回 Pong 就换下一个 | 卡住或频繁切换 | — |
| `udp_broadcast_wait_s` | 1.2 | 等广播出现的时长（秒） | 发现变慢 | 板子无 peer 时 1Hz 广播 |
| `last_peer_retry_gap_s` | 0.25 | 定向重试间隔（秒） | 重连变慢 | 与总预算配套：250ms × 6 |
| `last_peer_retry_budget_s` | 1.5 | 定向重试总预算（秒） | 重连变慢 | 250ms × 6，用尽转轮流探测 |
| `pong_timeout_s` | 0.3 | 串口 / WS 判丢线的 Pong 超时（秒） | 掉线误判（不报错） | 固件 `LINK_PATROL_TICKS=30`，与心跳同一把 300ms 尺 |
| `udp_pong_timeout_s` | 2.0 | UDP 判丢线的 Pong 超时（秒） | 掉线误判（不报错） | Hi3861 实测 p99 14.6ms / max 80ms；STA 断线后 3s 重连 |

**改错不会报错、只会表现为丢包 / 掉线误判的字段**：`min_gap_ms`、
`batch_window_s`、`batch_max_bytes`、`batch_max_acks`、
`buffer_bytes`、`heartbeat_gap_s`、`pong_timeout_s`、
`udp_pong_timeout_s`。调这些值时先改一点、看日志里的丢包 / 掉线记录。

**留在代码里、不进 JSON 的两类**：

1. 协议语义契约：`CmdPost` / `CmdGet` 命令字、`MONITOR_TAG_*` / `CMD_TAG_*` /
   `RESP_*` 位域、`ValueType`、`BUILTIN_CMD_NAMES`。改这些不是换个数，是换一套解析
   逻辑——后端/前端的分支是硬编码的（`if cmd == CmdPost.MonitorValue`），放进 JSON
   只会造成“改个数字就能适配”的错觉。真要换协议就得三端一起改源码。
2. 下行优先级与部署参数：`PRI_STOP` / `PRI_UI` / `PRI_SCRIPT`（改错破坏“停车永远第一”）；
   `V5_PORT` / `V5_BAUD` / `V5_UDP_*` / `V5_WS_*` / `DEFAULT_UDP_PORT` /
   `DEFAULT_WS_PORT` / `VEX_VID`（已有环境变量覆盖机制，两套来源会打架）。

长度 / 容量 / 标识类的协议参数（`frame_head` / `max_payload` / `discovery_*` /
`req_list_max` / `cmd_action_*` / `fw_lag_*`）**不在**上面两类里：改数值即可适配、
解析逻辑不变，所以放 config.json。前端的帧长上限走设置页（本地持久化），
判据是“前端 ≥ 后端 `max_payload`”，不要求两边相等。

配置文件缺失、某段或某字段缺失、JSON 写坏时，启动直接抛原始异常（`FileNotFoundError` /
`KeyError` / `TypeError` / `JSONDecodeError`），不设默认值回退、不做范围检查。

## 演示模式（没有真车/真固件）

后端不内置假固件，它只是把 WS 端口开着等设备接入。没有真车时另起一个独立进程
冒充 V5：

```powershell
# 终端 1：后端
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 终端 2：假固件（可指定地址与运行时长，第二个参数 0=一直跑）
.\.venv\Scripts\python -m debug.mock_ws_source
# 或显式指定：... ws://127.0.0.1:8889 30
```

- 启动顺序无所谓：后端先起会显示「WS 监听 … 等待设备接入」；假设备先起会每秒重连
- 真车时不要启动 `debug/mock_ws_source.py`：它只是调试工具，不在后端运行路径上
- 帧仍是 `A5 | Cmd | Payload | XOR` 二进制协议，装进 WS 二进制消息（协议零改动）
- 假设备顶替真 V5：四张表（burst + OK 项数）、订阅后 100Hz/80ms 推送、调参回显、
  命令回执、80ms 心跳；「原地转向」永远返回 BUSY 拒绝，用来演示拒绝 toast；
  「设备备注」是 str 类型只读项（2026-10-25 协议行为）
- 断线语义：杀掉假设备进程 → 后端 `connected=false`、status `disconnected`，已收到的
  目录缓存保留；重新拉起后回到探测态并重新被选中、清缓存并重拉四张表（订阅意图同步补发）
- 自测（内核级，不起服务）：`.\.venv\Scripts\python -m debug.test_mock_v5`


自测（无需串口、不起服务；改了协议/hub 后跑一遍）：

```bash
cd d:\work\vex\test1\backend
.\.venv\Scripts\python -m debug.test_protocol
.\.venv\Scripts\python -m debug.test_hub_proxy
```

> **不用 pytest 框架**（直接 `python -m` 跑，原因有二）：venv 里没装；
> 且 `backend/pytest/` 是**标定测量数据与数据处理脚本**的家（`probe_data/` 按标定项目
> 分目录 + `_wave_capture.py` 等），从 `backend/` 运行任何 pytest 都会被这个同名目录
> 遮蔽成 import 本地数据目录而崩。哪天真要装 pytest，必须**从仓库根目录**跑
> （`python -m pytest backend/debug`），别在 backend/ 里跑；更别为此改名 pytest/。

> **Python 基线固定 3.13**：

接口文档：<http://127.0.0.1:8000/docs>

## 上位机切到后端模式

前端顶栏勾选「后端」（连接目标变为 `ws://127.0.0.1:8000/ws`）。
注意：后端会对 VEX 串口做探测 / 收数，后端跑着的时候浏览器不要再直连 Web Serial——同一句柄被两边抢会互相打断。

## 连不上 / 没数据时的排查

按「请求发出去了吗 → 字节回来了吗 → 帧凑出来了吗 → 目录来了吗」四段定位：

1. **发请求后一个字节都没回**（最常见）：① 后端模式下确认只有一方在占串口（浏览器不要同时直连）；② 主控里跑的程序必须是带二进制通信协议的版本——旧程序不认识 A5 帧，收到也不会回；③ 确认主控屏幕上用户程序正在运行（不是停在下载/选择界面）；④ 换一根支持数据的 USB 线、直连电脑 USB 口再试。
2. **选错口**：V5 会枚举出两个串口，且两个口 VID/PID 相同、系统区分不了。后端把带 VEX 标识的口（VID 0x2888 / 描述符含 vex·user）都当候选，逐个发 Ping、每口最多等 120ms（30ms 补发一次），只留回 Pong 的 User 口，其余口无应答即关闭。设了 `V5_PORT` 时跳过这层过滤、只探测指定的口——这时要自己确认填的是 User 口。前端直连模式要选「User 口 / User Port」。
3. **字节有回但帧全是坏的**：数据损坏/错位——检查是否串了其他设备的数据；或固件与前后端版本不匹配。
4. **链路通但对方不讲这个协议**（帧头后的命令字全不认识）：固件还是旧版，重新下载。
5. **以上都正常但表格不出来**：点「获取调参表 / 获取实参表」请求目录；个别量没数据，查该项订阅档位是否为「未订阅」。

后端模式下连接状态与端口列表看 `GET /api/status`。链路活性按通道判定：后端每 80ms 发一次 Ping；串口 / WS 超过 0.3s 收不到 Pong 判掉线（固件 300ms 无 Ping 停推是同一事件的第二见证），Hub 随即每 5s 重试一轮并自动重连。UDP 的 Pong 超时放宽到 2.0s，且 UDP 本身不报 disconnected（只清 peer 回发现态），由选路仲裁层补报并回到三探测。

> 2026-10-23：后端 FrameParser 的 RxStats 诊断计数（bytes/frames/badChecksum/unknownCmd/overflow）已删除——
> 后端模式下它零读者（脚本没连上读 `/api/status` 就够，链路活性由 80ms Ping +
> 按链路 Pong 超时覆盖）。前端直连模式的
> 「通信诊断」弹窗在浏览器直连串口时看原始字节与事件日志；上述排查结论即该弹窗的知识沉淀。

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
v.print_log("pytest/run")        # path 是基名，默认出 run.csv
v.stop()
```

### 写新脚本：最短能到 15 行

便利设施：`with BackendLink() as bl`（退出自动收车收尾）、`bl.mon_dir`（后端拉取的实车
监控表 `中文名 → index`）。**后端不认识任何命名约定**，脚本只认实车目录里的**中文名**
或 **index**；`L2 → 左2转速` 这类别名属脚本自己的约定，在脚本侧维护
（`scripts/tools/motor_names.py` 是个可抄的样例）。分段记录惯例：每档一段
`start_log → 设值 → 保持 → end_log`，一段就是一条独立曲线，段末 `calc(..., agg=...)` 取整段标量。

```python
import sys; sys.path.insert(0, r"backend")
from scripts.tools.motor_names import motor_names     # 命名归脚本自己管
from app.script_api import BackendLink
with BackendLink() as bl:
    ids = motor_names("L0")        # {'vact':'左0实际电压','rpm':'左0转速','cur':'左0电流'}
    bl.sub(ids.values())           # 登记要记录的项
    rows = []
    for v in [0.3, 0.6, 0.9, 1.2]:
        bl.start_log()             # 每档一段独立曲线
        bl.set_volt(0, v)
        bl.sleep(0.4)
        bl.end_log()
        rows.append({"v": v, "rpm": bl.calc(ids["rpm"], agg="mean")["value"]})
    bl.save_csv("pytest/l0_start.csv", rows)
```

`calc(expr, agg)` 把一段聚合成一个标量，**agg 只支持 mean / sum**；要切子区间就
`print_log` 导 csv 后脚本侧 pandas 自己切。想提前停直接 `break`
（比如"一旦转起来就停"），已 end_log 的段不受影响。原始曲线按段 `print_log(基名)` 落盘，
默认出 `.csv`；`plot=True` 加出 `.png`，`meta=True` 加出语义 `.meta.json`。

> **分析的正路是 pandas 回读**：abs_mean、std、分位、布尔过滤、派生指标这类统计，
> 一律 `print_log` 落 csv 后脚本侧 `pd.read_csv` 自己算（`df["左0转速"].abs().mean()` 一行的事），
> 不要给后端加聚合功能——API 面是棘轮，加功能十行、删功能破裂契约，
> 每条"捷径"都会招募下一个补丁。后端刻意只保留 mean/sum + 一个表达式白名单。

### 记录会话：脚本只管发命令，记录由后端干

脚本决定"记哪些项"，每段测量自己收一段记录；段内时间戳是
**相对本段零点（start_log）的毫秒**，不用对齐本地时钟，也不用自己打点。

```python
v.sub(["左0转速", "左0实际电压"])
for volt in (0.5, 1.0, 1.5):
    v.start_log()                                    # 每档一段独立记录
    v.cmd("组开环电压", 0, volt, 0.0, 0.0)   # 组别 0=左组，后三个是该组逐台电压
    v.sleep(0.4)
    v.end_log()
    print(volt, v.calc("左0转速", agg="mean")["value"])   # 本段均值（mean/sum 二选一）

v.print_log("pytest/run")                          # 导出 CSV（默认）
```

- `print_log(基名)`：path 不带后缀，产物按 `<基名>.csv` / `<基名>.meta.json` /
  `<基名>.png` 派生。三个开关 `csv`（默认 True）/ `meta` / `plot` 组合出 5 种：
  csv、csv+meta、png、png+csv、png+csv+meta；`meta` 是 csv 的语义快照（每列单位、
  登记全集、丢帧数、时间轴含义），只能跟 csv 一起。不传 path 落
  `pytest/probe_data/log_<时间戳>.csv`
- `calc(expr, agg)`：agg 只支持 `mean` / `sum`（必填，没有逐点序列选项——
  要原始点或子区间就 `print_log` 导 csv 回读，脚本侧 pandas 切）
- **abs_mean / std / 分位 / 布尔过滤 / 派生指标：脚本侧 pandas 算**，后端不加聚合。
  常用的一档已在 `scripts/tools/seg_stats.py` 里（`std` 用总体口径除 n，与旧后端口径一致）：

  ```python
  from seg_stats import seg_stats
  st = seg_stats(res["files"]["csv"], ["左0转速"], thresholds=[5.0])  # 从 print_log 返回体取
  st["左0转速"]["abs_mean"], st["左0转速"]["ratio_ge"]["5.0"]
  ```

  它读的是 csv **按 ±2ms 同拍对齐**后的值（近邻行合并取均值，无 10ms 分桶、也不取最后），而旧后端 stats 读的是原始样本列表——
  110Hz 等高推送率下同一桶可能并进 2 个样本，`n` 会比旧口径略小。判据用比值/均值不受影响，
  拿 `n` 当样本计数时要知道这个差。
- **记录缓冲上限 5000 帧（按拍计，100Hz 约 50s），超了丢新帧，`log_dropped` 计数**。段化写法天然不会累积；万一一次记录跨很多样本，
  中途再调一次 `start_log()`（会清缓冲并重置零点）即可。

### 一次只允许一个脚本写（2026-10-24 起）

后端有把「写权」，`BackendLink()` 构造时自动领取，**脚本侧不用写任何新代码**。
第二个 `BackendLink()` 会直接失败，**不会**跑去跟前一个抢串口：

```
RuntimeError: /api/script/claim -> 409: {"detail":"写权被另一个脚本占着。请先让它 release；或者在上位机按停车（会中止该脚本并释放写权）。"}
```

- **想接管正在跑的脚本**：先在上位机按停车（中止该脚本并释放写权），或让对方正常 release。
  没有抢占开关——要接管就先用已有的「强行中止」链路，比"硬抢"直白
- **写权归先占者**，直到它自己 release，或被停车中止时释放；**没有自动过期**
- **不用管收尾**：脚本正常退出或 Ctrl+C 走 `with` 的 `__exit__` 都会 release；
  就算崩了没 release，下一个脚本要么等它被停车中止，要么自己按一下停车
- 这套约束只覆盖**写路径**（`/api/sub`、`/api/cmd`、`/api/tunable`、`/api/log/start|end`、
  `/api/abort/clear`）；读路径（`status`/`column`/`value`/`print`/`calc`）随便看
- 前端完全走 WS，只用一个 `/api/connect`，所以这套互斥误伤不到上位机

为什么要这道闸：`abort` 闩锁是全局的且没有所有者，两个脚本同时在跑时，
晚来的那个 `connect()` 会顺手 `clear_abort`，把早来脚本刚吃到的停车解掉——
它以为自己在停车状态里待着，实际又开始发命令了。**先挡住第二个脚本，
比给 clear_abort 打补丁更靠得住。**

### 现成脚本（`scripts/`，按标定层级分目录）

均通过 `app.script_api.BackendLink` 走后端 HTTP（脚本构造即就绪；数据由后端记录，
脚本只发命令 + 按区间取标量），不再直连串口；先启动后端再运行。

层级即标定依赖顺序：**01~04 走开环（0x84 直设电压，绕过速度环）**，测纯物理量，
必须先经脑屏交出底盘（`is_busy` 期间 0x84 被静默丢弃）；**05 起进闭环（0x85）**。
01/02 车轮悬空，03~06 着地。

| 目录 | 脚本 | 用途 |
| --- | --- | --- |
| `01-硬件体检/` | `motor_check.py` | 逐电机体检：3V/0.5s 判堵转/空转/断线（悬空） |
| | `motor_v_test.py` | 单台伏安特性 + 启动电压诊断（**装机前**，未接入传动链） |
| | `motor_start_repeat.py` | 启动电压重复分布（随机化齿槽停位，装机前） |
| `02-电机组固有属性/` | `cal_motor_intrinsic.py` | volt_min/volt_factor 主标定（悬空·纯开环，等流搜索） |
| | `motor_share_test.py` | factor 闭环复核：同侧三台电流分流比（悬空·闭环） |
| | `neutral_curve.py` | 零扭矩电压曲线 → 每台 k_e·ω（悬空·开环，诊断向） |
| `03-摩擦垫/` | `car_vi_curve.py` | 静/动摩擦垫标定（着地·开环，等流爬坡） |
| `04-前馈表/` | `cal_kf_table.py` | kf 阻力前馈表：着地开环扫压反解 + 闭环纯前馈验证 |
| | `kf_open_sweep.py` | 老路：悬空组级开环扫描反解（`--recalc` 合并地面波形外推高速档） |
| `05-速度环/` | `speed_loop_probe.py` | 速度环诊断：开环起步/降压 + 闭环增益扫掠（带位移围栏/限速保护） |
| | `motor_speed_step.py` | 速度环阶跃响应（0x85 闭环）：上升时间/超调/稳态误差 |
| `06-几何标定/` | `cal_wheel_r.py` | 定位轮半径实测复核（着地，set_spin + 卷尺） |
| | `cal_rotation_center.py` | 旋转中心 offset 标定（着地·原地转） |
| `tools/` | `wave_dump.py` | probe 波形 CSV 按时间轴对齐打印 |
| | `wave_plot.py` | probe 波形/汇总画 PNG（仅开环相位） |
| | `plot_util.py` | 画图共用件：中文字体兜底 + `subplots/save/legend` |
| | `motor_names.py` | `L2→左2转速` 脚本侧别名约定（可抄的样例） |
| | `seg_stats.py` | **段统计共用件**：回读 `print_log` 落下的宽表 csv，返回 `{项名: {n, mean, abs_mean, abs_max, std, ratio_ge}}`（`ratio_ge` 需传 `thresholds`）。`/api/log/stats` 删除后这是取段统计的正路 |

各层的标定原理、判据与用法文档在 `scripts/docs/`（入口是 `00-标定总览.md`：
标定链顺序、悬空/着地与开环/闭环分界、每步验证手段）；主仓库 `docs/` 只放以下位机
固件为主的内容。

## 产物落盘与出图

- 所有 CSV/图统一落 `backend/pytest/probe_data/<层目录>/`（`BackendLink.out_path()` 分层，
  绝对路径原样放行）；`speed_loop_probe` 的波形再多一层 `<tag>/`
- 每个标定脚本跑完**自动出图**，与 CSV 同名同目录；`--png` 改文件名，传空字符串跳过
- 单段波形另可用 `print_log(基名, [项...], csv=, meta=, plot=)` 组合导出，
  可挑项、可多次导出（记录缓冲到下次 `start_log` 才清）
- 画图能力来自 `tools/plot_util.py`（中文字体兜底、统一 dpi、空图例不刷 warning）

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
v.read_item("x")            # 完整信息：index/name/value/unit/lag_ms
                             # 看门狗主判 lag_ms：None 或 >300ms 即视为旧值
```

典型看门狗（30ms 周期）：

```python
v.sub("x", "push", fast=True)
while ...:
    lag = v.read_item("x")["lag_ms"]
    if abs(v.read("x")) > 0.25 or lag is None or lag > 300:
        v.stop()
    time.sleep(0.03)
```

## 安全约定

- 停车按“脚本在不在跑”分流：脚本在跑时按任意停车按钮 → 杀脚本（置 abort、清脚本队列）+ **全部**停车命令；脚本没在跑 → 只发按钮对应的那一条，不连坐、不置 abort。脚本自己调 `v.stop()` 也只发指定的那一条（默认「停止底盘」）。`/api/stop` 是无条件全量的外部紧急入口。停车帧一律 `PRI_STOP` 插队
- `cmd` 参数个数必须与命令目录完全一致，少参/多参都会 400；目录里带取值范围的字段（tag bit7）还会校验上下限，越界同样 400
- 心跳与重连：后端 80ms 一发 Ping；下位机 300ms 收不到 Ping 会停推监控（link_ok，communication.h LINK_PATROL_TICKS=30）。串口 / WS 超过 0.3s 收不到 Pong 判掉线并自动重连（重连后自动重拉目录、补发订阅）；WiFi/UDP 的 Pong 超时放宽到 2.0s，且 UDP 本身只清 peer 回发现态、不报 disconnected，由选路仲裁层补报并回三探测。掉线后先按上次对端定向重试 250ms×6（预算 1.5s），用尽后转轮询（串口逐口 120ms、UDP 等广播 1.2s）；默认只开带 VEX 标识的口（VID 0x2888 / 描述符含 vex·user），非 VEX 设备的串口一律不抢占；设了 `V5_PORT` 时只开指定口、跳过该过滤
- 目录缓存：连上后自动拉四张表并按 index 缓存原始帧，多客户端重复请求不打车；怀疑缓存过期/半表时 `POST /api/dirs/refresh` 强制刷新（或脚本里 `v.refresh_dirs()`）
- 脚本退出：曾 `sub("push"/"both")` 的项应显式 `unsub`，或 finally 里 `v.clear_script_subs()` 一键清掉 script 侧订阅意图
- 不内置看门狗，按测试场景自行在脚本里超时 `v.stop()`
