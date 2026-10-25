# VEX Python Backend

插在 Vue3 上位机与 V5 主控之间的 FastAPI 服务：

- **WebSocket `/ws`**：串口上行帧原样二进制透传给上位机（前端 FrameParser 零改动），上位机下行帧高优先级转发；停车帧自动插队并中止脚本
- **HTTP `/api/*`**：脚本友好接口（停车、拉目录、订阅、下发命令、记录日志）
- 串口独占（pyserial），协议与 `frontend/src/services/protocol.ts`、`include/communication.h` 一致

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

## 演示模式（没有真车/真固件）

`V5_MOCK=1` 起服务，后端用 `app/mock_v5.py` 的 MockV5Bridge 顶替串口后面的那台 V5：

```powershell
$env:V5_MOCK=1
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 小程序/网页连 WS 的行为与真机**完全一致**：拉三张表（burst + OK 项数）、订阅后
  100Hz/80ms 推送、调参回显、命令回执、80ms 心跳、status/alert 带外消息
- 模拟主控内置 14 个监控项 / 9 个调参项 / 5 条命令（含停车）；「原地转向」永远返回
  BUSY 拒绝，用来演示拒绝 toast；「设备备注」是 str 类型只读项（2026-10-25 协议行为）
- 实现方式：Hub 构造时注入 bridge 工厂（`bridge_factory`），只换"字节怎么进出设备"，
  WS 透传/订阅 diff/停车插队/重连全部走真实路径——演示模式测的是真链路，不是 mock 本身
- 自测（不起服务）：`.\.venv\Scripts\python -m debug.test_mock_v5`

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
注意：同一时刻只有一方能占串口——后端跑着的时候浏览器不要再直连 Web Serial。

## 连不上 / 没数据时的排查

按「请求发出去了吗 → 字节回来了吗 → 帧凑出来了吗 → 目录来了吗」四段定位：

1. **发请求后一个字节都没回**（最常见）：① 后端模式下确认只有一方在占串口（浏览器不要同时直连）；② 主控里跑的程序必须是带二进制通信协议的版本——旧程序不认识 A5 帧，收到也不会回；③ 确认主控屏幕上用户程序正在运行（不是停在下载/选择界面）；④ 换一根支持数据的 USB 线、直连电脑 USB 口再试。
2. **选错口**：V5 会枚举出两个串口。后端只认带 VEX 标识的口（VID 0x2888 / 描述符含 vex·user），下载口不会误占；前端直连模式要选「User 口 / User Port」。
3. **字节有回但帧全是坏的**：数据损坏/错位——检查是否串了其他设备的数据；或固件与前后端版本不匹配。
4. **链路通但对方不讲这个协议**（帧头后的命令字全不认识）：固件还是旧版，重新下载。
5. **以上都正常但表格不出来**：点「获取调参表 / 获取实参表」请求目录；个别量没数据，查该项订阅档位是否为「未订阅」。

后端模式下连接状态与端口列表看 `GET /api/status`；链路活性由 300ms 心跳判定（固件 300ms 无 Ping 停推 / 后端 300ms 无 Pong 判掉线，掉线自动重连）。

> 2026-10-23：后端 FrameParser 的 RxStats 诊断计数（bytes/frames/badChecksum/unknownCmd/overflow）已删除——
> 后端模式下它零读者（脚本没连上读 `/api/status` 就够，链路活性由 300ms 心跳覆盖）。前端直连模式的
> 「通信诊断」弹窗保留作装饰（仅浏览器直连串口时经过它）。上述排查结论即该弹窗的知识沉淀。

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
v.print_log("pytest/run.json")   # 后缀决定格式：json/csv/png/jpg（无后缀默认 json）
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

`calc(expr, agg)` 把一段聚合成一个标量，**agg 只支持 mean / sum**；想避开开头的阶跃过渡，
加 `window=[t0_ms, t1_ms]`（t 轴零点就是 start_log 那一刻）。想提前停直接 `break`
（比如"一旦转起来就停"），已 end_log 的段不受影响。原始曲线按段 `print_log(路径)` 落盘，
格式看后缀：`.json` / `.csv` / `.png` / `.jpg`（无后缀默认 json）。

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

v.calc("左0转速", agg="mean", window=[100, 400])  # 子区间聚合（window 单位 ms，相对本段零点）
v.print_log("pytest/run.csv")                      # 导出 CSV（后缀决定格式）
```

- `print_log(路径)` 按后缀选格式：`.json`（无后缀默认）/ `.csv` / `.png`|`.jpg`（按项作图）；
  路径带子目录会自动创建；不传则落后端 `pytest/probe_data/log_<时间戳>.json`
- `calc(expr, agg, window)`：agg 只支持 `mean` / `sum`（必填，没有逐点序列选项——
  要原始点就 `print_log` 导 csv/json 回读）；window 相对本段零点切子区间
- **abs_mean / std / 分位 / 布尔过滤 / 派生指标：脚本侧 pandas 算**，后端不加聚合。
  常用的一档已在 `scripts/tools/seg_stats.py` 里（`std` 用总体口径除 n，与旧后端口径一致）：

  ```python
  from seg_stats import seg_stats
  st = seg_stats(seg_csv, ["左0转速"], thresholds=[5.0])   # seg_csv 是本段 print_log 落的
  st["左0转速"]["abs_mean"], st["左0转速"]["ratio_ge"]["5.0"]
  ```

  它读的是 csv **10ms 分桶后**的值（桶内取最后），而旧后端 stats 读的是原始样本列表——
  110Hz 等高推送率下同一桶可能并进 2 个样本，`n` 会比旧口径略小。判据用比值/均值不受影响，
  拿 `n` 当样本计数时要知道这个差。
- **记录缓冲上限 50 万条，超了丢新样**。段化写法天然不会累积；万一一次记录跨很多样本，
  中途再调一次 `start_log()`（会清缓冲并重置零点）即可。

### 一次只允许一个脚本写（2026-10-24 起）

后端有把「写权租约」，`BackendLink()` 构造时自动领取，**脚本侧不用写任何新代码**。
第二个 `BackendLink()` 会直接失败，**不会**跑去跟前一个抢串口：

```
RuntimeError: /api/script/claim -> 409: {"detail":"已有脚本持有写权（12s 前领取，..."}
```

- **想接管正在跑的脚本**：`BackendLink(steal=True)`。被踢的那个会被置 abort，
  在它下一次 `cmd()` / `sleep()` 处抛错停下，不会继续往串口塞命令
- **不用管收尾**：脚本正常退出或 Ctrl+C 走 `with` 的 `__exit__` 都会 release；
  就算崩了，租约 60s 没动静自动过期，下一个脚本能直接接管
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
- 单段波形另可用 `print_log(path, [项...])` 导出：`.json/.csv/.png/.jpg/.pdf/.svg`，
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
- 心跳与重连：后端 80ms 一发 Ping；下位机 300ms 收不到 Ping 会停推监控（link_ok，communication.h LINK_PATROL_TICKS=30）；后端 300ms 收不到 Pong 判定串口掉线并自动重连（重连后自动重拉目录、补发订阅）。重连每 5s 一轮，只开带 VEX 标识的口（VID 0x2888 / 描述符含 vex·user），非 VEX 设备的串口一律不抢占；每个候选口最多等 120ms Pong，认不出就关掉换下一个
- 目录缓存：连上后自动拉三张表并按 index 缓存原始帧，多客户端重复请求不打车；怀疑缓存过期/半表时 `POST /api/dirs/refresh` 强制刷新（或脚本里 `v.refresh_dirs()`）
- 脚本退出：曾 `sub("push"/"both")` 的项应显式 `unsub`，或 finally 里 `v.clear_script_subs()` 一键清掉 script 侧订阅意图
- 不内置看门狗，按测试场景自行在脚本里超时 `v.stop()`
