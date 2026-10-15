# VEX U 整车调试链路代码审查报告

**审查范围**：
- 下位机 C++：`include/`、`src/`、`src/class_and_tool/`
- 后端：`backend/app/`、`backend/requirements.txt`
- 前端：`frontend/src/services/`、`frontend/src/stores/`
- 协议三端：`include/communication.h` ↔ `backend/app/protocol.py` ↔ `frontend/src/services/protocol.ts`

**审查结论一句话**：整体结构合理、边界清晰、自带注释密度高、可读性良好；但存在 **3 个休眠/潜在 bug**、**1 个真实性能 bug**、**2 个可能的逻辑漏洞**，以及 **若干重复实现、dead code、类型小问题**。

---

## 1. 协议三端一致性核查（按 AGENTS.md 第 4 条最高优先级）

| 协议字段 | 下位机 `communication.h` | 后端 `protocol.py` | 前端 `protocol.ts` | 一致性 |
| --- | --- | --- | --- | --- |
| 帧格式 `A5\|Cmd\|Payload\|XOR` | ✓ 写入注释 | ✓ 写入注释 | ✓ 写入注释 | ✓ |
| XOR 从 Cmd 起（不含帧头） | ✓ | ✓ | ✓ | ✓ |
| 多字节小端 | ✓ | ✓ | ✓ | ✓ |
| `CmdPost` 编号 0~4 + 0xFF | `Request_Tunable=0, Request_Monitor=1, Subscribe=2, Set_Tunable=3, Request_Command=4, Pong=0xFF` | 同 | 同 | ✓ |
| `CmdGet` 0~4 + 0xFF | `Post_Tunable=0, Post_Monitor=1, Monitor_Post=2, Tunable_Echo=3, Post_CMD=4, Ping=0xFF` | 同 | 同 | ✓ |
| `VALUE_TYPE` 0~9 | 0~9 一一对应 | 0~9 一一对应 | 0~9 一一对应 | ✓ |
| `VALUE_TYPE` 10~14 | `str=10, bool=11, on_off=12, color=13, other=14` | **缺 Str=10、Other=14** | 同 C++ | ⚠️ 缺口 |
| `CMD_TAG_RANGE/ARGC/SHIFT/SPEC` 位域 | `0x80, 0x70, 4, 0x0F` | 同 | 同 | ✓ |
| `MONITOR_TAG_SUB/FAST/GETTER/KIND` | `0x80, 0x40, 0x20, 0x07` | 同 | 同 | ✓ |
| Subscribe 合并（只动 SUB/FAST，保留 GETTER/KIND） | ✓ `merge_subscribe_tag` | ✓ | ✓ mockTransport 注释重申 | ✓ |
| 目录帧布局 `[index][type][tag][nameLen][name][unitLen][unit][value]` | `send_dir_batch` | `_dir_len` | `dirLen` | ✓ |
| 命令目录帧布局 | `send_cmd_dir_batch` | `_cmd_dir_len` | `cmdDirLen` | ✓ |
| SetTunable 定长 9B | ✓ | ✓ | ✓ | ✓ |
| Pong 帧 `[A5 FF FF]` | ✓ | ✓ | ✓ | ✓ |

### 不一致项（不致命，建议记录）
1. **Python `ValueType` 缺 Str=10 与 Other=14**：decode 时若遇到会按 `value_size=0` 忽略；当前没有这类目录项，问题休眠。
2. **Python `decode_value` 对 Color 的越界保护**：`if off+1 < len(data)` 单独写；前端 TS 用 `getUint8OrZero` 函数。语义一致但形式不同。
3. **TS `commandSend` 用 `buildFrame(item.index as CmdGet, payload)` 类型断言**：运行时 OK（0x80~0x9F 不与现有 CmdGet 数值冲突），但属绕过类型检查，未来若 CmdGet 扩到 0x80 段会冲突。

---

## 2. 🔴 必修 BUG（会造成静默错误、丢数据或崩溃）

### BUG-1 【下位机】`type_double` 双重 cast 错误（休眠，未来启用即触发）

**位置**：
- `src/class_and_tool/menu_func.cpp` — `tc_value_add` / `tc_value_reduce`
- `src/class_and_tool/menu_func.cpp` — `value_to_str`
- `src/class_and_tool/tool.cpp` — `value_to_str`

**症状**：所有 `case type_double` 分支写的是 `*(float *)item->data_ptr += ...` / `snprintf(..., "%.8f", *(float *)ptr)`——把 `double*` 当 `float*` 解/写。
**影响**：当前 `menu_item[]` / `monitor_menu_item[]` 没有任何 `type_double` 表项，问题"休眠"。一旦有 `type_double` 调参项（调参表加新字段），写入会**写 4 字节**（而不是 8 字节）覆盖相邻内存；LCD 显示会用 `%.8f` 显示 `*(float*)` 的前 4 字节——值会变。
**修复**：把所有 `*(float *)` 改为 `*(double *)`，`sizeof(float)` 改为 `sizeof(double)`。

### BUG-2 【下位机】`awb_start_time` 类型截断溢出

**位置**：`src/ai_vision.cpp:16`
```cpp
static uint16_t awb_start_time = 0;       // 是 uint16_t
...
awb_start_time = vex::timer::system();    // 被赋 uint32_t，隐式窄化
```
**症状**：`vex::timer::system()` 返回 uint32_t（毫秒）。开机 65.5s 后 `awb_start_time` 高位被截断；之后 `vex::timer::system() - awb_start_time` 计算差值时，按 uint16+uint32 提升——**会得到错误的"距启动时间"**。
**影响**：AWB 实际已完成但 `ai_vision_is_ready()` 长期返回 false（或反之），主循环 `while (!ai_vision_is_ready()) Delay(20)` 卡死。
**修复**：改为 `uint32_t awb_start_time = 0;`
**附带不一致**：头文件说"启动 2200ms 后视为就绪"，cpp 写 `> 2300`——至少统一两处。

### BUG-3 【下位机】`MENU::shift()` 切到空监控表 UB（除零 + nullptr deref）

**位置**：`src/class_and_tool/menu_func.cpp` `MENU::shift()`
```cpp
if (!monitor_len) ...; // 当前没有这个空检查
// up() 里：(now_choose + menu_len - 1) % menu_len
// refresh_highlight_line_left() 里：menu_item[now_choose] 解 nullptr
```
**症状**：若 `monitor_items=nullptr, monitor_len=0`（监控表未启用），第一次按 shift 切到监控菜单，`menu_item` 变 nullptr、`menu_len=0`，下次 `up()` `% 0` UB。
**影响**：罕见，但"切到空表 + 按键"会让循环内后续访问崩溃。
**修复**：在 `shift()` 入口 `if (monitor_len == 0) return;`；或外部调用前由 `LCD_menu` 守门。

### BUG-4 【下位机】`PositionPID::update` D 项除零保护缺失

**位置**：`include/pid.h`、`src/class_and_tool/pid.cpp` 的 `update`
```cpp
float gap_time_s = gap_time_us / 1000000.0f;
... kd * derivative ...   // derivative = (error - previous_error) / gap_time_s
```
**症状**：若两次 `update` 间隔为 0µs（1ms 内连调），`gap_time_s=0`，得 `Inf`/`NaN`，污染 I 项与输出。
**修复**：`update` 入口加 `if (gap_time_us == 0) return previous_output;` 类守卫；与 `my_motor_group.cpp:62-64` 的 `dt_us < 1000 → dt_us = 1000` 风格一致。

### BUG-5 【前端】`globle.ts` traceOrigSubs 二次追踪会导致订阅卡死高速

**位置**：`frontend/src/stores/globle.ts:303-325`
```ts
function syncTraceSubs(...) {
  for (const idx of after) { ... rememberTraceOrigin(idx); void sendSubscribe(idx, true, true); }
  for (const idx of before) {
    const orig = traceOrigSubs.get(idx);
    traceOrigSubs.delete(idx);                 // ← 取消追踪时直接删
    if (orig && !(orig.sub && orig.fast)) void sendSubscribe(idx, orig.sub, orig.fast);
  }
}
function rememberTraceOrigin(idx) {
  if (traceOrigSubs.has(idx)) return;          // 已删后会重新记
  ... else if (item) traceOrigSubs.set(idx, { sub: tagSubscribed(item.tag), fast: tagFast(item.tag) });
}
```
**症状**：
1. 用户把 idx=5 加到追踪 → 强制 FAST，记 orig={sub:true, fast:false}。
2. 移除 idx=5 → `syncTraceSubs` 里 `traceOrigSubs.delete(5)`。
3. 再次添加 idx=5 → `rememberTraceOrigin(5)`：`traceOrigSubs` 已删 → 用 `monitorMap[5].tag` 当前状态（即已被提速的 sub:true, fast:true）记为 orig={sub:true, fast:true}。
4. 第二次移除 → `if (orig && !(orig.sub && orig.fast))` —— 条件不满足，**不发退订**。

**结果**：用户从未主动定到高速，但该量永久卡在高速推送。
**修复**：orig 表永久保留（不 delete），或独立维护"用户原始订阅"快照。

### BUG-6 【前端】`mockTransport` setTimeout 未在 disconnect 清理

**位置**：`frontend/src/services/mockTransport.ts:262-264, 270-273`
```ts
mockCommands.forEach((c, i) => { setTimeout(() => this.cb.onData(cmdDirectoryFrame(c)), i * 10); })
...
private emit(f: Uint8Array): void { setTimeout(() => this.cb.onData(f), 0); }
```
**症状**：`disconnect()` 只调 `stopTimers()`，不清这些 setTimeout。如果用户点命令目录请求后立即切到真串口或演示 off，残留 setTimeout 仍会调 `cb.onData`，把演示帧喂给刚连上的真实 store 的 FrameParser——`monitorMap/parser.monitorType` 被污染。
**修复**：把 setTimeout 的 handle 收集到数组，`disconnect()` 里 `for (const id of this.pendingTimers) clearTimeout(id);`。

### BUG-7 【后端】`_tx_q` 入队全排序 O(n log n)，注释自承"知而不改"

**位置**：`backend/app/hub.py:752`
```python
self._tx_q = deque(sorted(self._tx_q, key=lambda x: (x[0], x[1])))
```
**症状**：每帧入队都完整排序；高频 `set_spin` / `set_volt` × 8 路电机时，O(n log n) 会被放大。`set_tunable_value`、连续 Subscribe 也都进同一条队列。
**修复**：改用 `sortedcontainers.SortedList` 或 `bisect.insort` + 普通 `list.popleft`，O(log n) 入队 / O(1) 出队。

### BUG-8 【后端】`api_tunable` 漏 `ValueError → 400` 处理

**位置**：`backend/app/main.py:242-254`
```python
try:
    return hub.set_tunable_value(...)
except KeyError as e: raise HTTPException(404, ...)
except RuntimeError as e: raise HTTPException(409, ...)
# 缺 ValueError → 400
```
**影响**：未来协议层若给 `set_tunable_value` 加范围校验（与 `cmd` 一致），会冒 500 而非 400，不符合本项目约定（`api_cmd` 是有的）。
**修复**：补 `except ValueError as e: raise HTTPException(400, str(e))`。

### BUG-9 【后端】`test_protocol.py` tag 用魔术数，未来协议变更会假阳性绿

**位置**：`backend/app/test_protocol.py` 中相关测试，`tag = 0x80 | (1<<4)` 直接拼位。
**修复**：用 `cmd_tag_make(1, CmdSpec.Normal) | CMD_TAG_RANGE` 构造，意图自描述。

### BUG-10 【下位机】`find_cmd` 边界判断在 cmd_count > 0x80 时溢出（理论）

**位置**：`src/class_and_tool/communication.cpp:38`
```cpp
if (cmd < CMD_BASE || cmd >= CMD_BASE + cmd_count)
```
**症状**：`CMD_BASE=0x80` 是 uint8_t，加 `cmd_count=128` 会回绕到 0。当前 cmd_count 上限 32（CMD_SLOT_MAX），实际不发生；但"信任下游"策略下应显式 clamp。
**修复**：用 uint16_t 比较：`if ((uint16_t)cmd < CMD_BASE || (uint16_t)cmd >= (uint16_t)CMD_BASE + cmd_count)`。

### BUG-11 【下位机】`chassis.h` 注释/数值不自洽

**位置**：`include/chassis.h:150`
```cpp
float wheel_r = 0.0254f;      // 定位轮半径，单位 m（旧车定位轮直径 5.08cm，即 2 英寸）。
```
**症状**：直径 5.08cm = 2 英寸，半径应为 2.54cm = 0.0254m —— **数值对的**（0.0254m = 1 英寸半径），但注释把"直径 5.08cm = 2 英寸"与"半径"混在一句话里，且若确实 2 英寸直径轮，半径应是 0.0254m，与"5.08cm"自洽（圆周长公式 `2π * 半径 = π * 直径`）。
**修复**：注释改为"定位轮半径，单位 m（旧车定位轮直径 5.08cm = 2 英寸，半径 2.54cm）"。

---

## 3. 🟡 设计 smell（影响可维护性，目前不致崩）

### SMELL-1 【后端】`_drop_client_intent` 与 `clear_script_intents` 几乎重复

**位置**：`backend/app/hub.py:820-853`

除 `source` key 不同外完全一致。抽 `def _drop_source_intent(self, source: str) -> None`。

### SMELL-2 【后端】`serial_bridge._lost` 与 `disconnect` 大量复制

**位置**：`backend/app/serial_bridge.py:297-334`

都是"停心跳 → _stop.set → _connected=False → 收 rx → 关 ser → 上报 on_status"。抽公共 `_teardown(final_state: Status, message: str)`。

### SMELL-3 【后端】`hub.enqueue` 期间**替换 deque 实例**（隐式可见性问题）

```python
self._tx_q = deque(sorted(self._tx_q, key=lambda x: (x[0], x[1])))
```
- `_tx_loop` 持锁外 `popleft()`，因为 enqueue 替换了 `self._tx_q`，理论 OK 但隐式。注释明确警告"不要缓存 self._tx_q 引用"。
- 与 SMELL-1/BUG-7 一起改：换 SortedList / insort + 普通 list，无此问题。

### SMELL-4 【前端】`mockTransport` 是 `protocol.ts` 的近副本

- 自己的 `frame(cmd, payload)` / `appendValue` / `decodeValueLE` —— 一处改帧格式（如加 CRC）要改两处。
- 修复方案：让 `protocol.ts::buildFrame` 接受 `(cmd: number)` 重载，演示侧用其；或抽取 `payloadBytes` 共享工具。

### SMELL-5 【前端】`persist.ts` 无 `beforeunload`/`pagehide` 同步 flush

`flush()` 走 300ms debounce，按 F5 / 关闭标签时 300ms 内改动丢。
**修复**：`window.addEventListener('pagehide', () => flush())`。

### SMELL-6 【前端】`mockTransport` Subscribe/SetTunable 解析无长度校验

`data[2]! data[3]!` 直接非空断言；`data.subarray(3, 11)` 假定 11 字节。异常帧会让 `setTimeout(...)` 路径抛 TypeError，污染 emit 流程。

### SMELL-7 【下位机】`monitor.cpp` 与 `filter.cpp` 整文件 dead code

- `monitor.h` **是空的**（仅 `#include "my_main.h"`），但 `monitor.cpp` 定义了 `monitor_init/_reset/_refresh` 与全局 `optical/distance`——没有任何头文件声明，等于死代码。
- `filter.cpp` 的 KalmanFilter 也未被任何地方使用（`my_motor_group.cpp / robot_action.cpp / chassis.cpp` 都无引用）。
- 修复：要么补头文件声明并接入实际传感器；要么整文件删除。

### SMELL-8 【下位机】`PositionPID` 公共字段过宽

`kp/ki/kd/integral/previous_error` 都 public，外部调参菜单直接 `&robot_action.distance_pid.kp` —— 与"封装 PID"意图不符。
AGENTS.md 第 2 节说"字段按实际用途设计，常用状态、调参量、监控量可以 public"，**只调参的留 public**：`kp/ki/kd/max_output` 留，其他改 private + getter。

### SMELL-9 【下位机】`Control_key::lpress + first_press_gap=65535` 用法与注释不符

- 注释："lpress 长按模式支持短按/长按"
- 实际用法 `key_set.cpp`：`first_press_gap = 65535` 在 release 时才触发，行为等价"短按但 release-沿触发"。
- 修复：要么补 `ENABLE_RELEASE_EDGE` 模式；要么注释明确"等价短按，仅 release 沿触发"。

### SMELL-10 【下位机】`MENU` 状态切换时序对转速测量场景不友好

`MENU::refresh_value()` 对 GETTER 项会执行 `motor->voltage()/current()/velocity()` —— VEX SDK 调用是**阻塞串口查询**，每个约 1~50ms。当前 `monitor_menu_item` 有 16 个 Vact/I/pct GETTER，每拍全订阅时 10ms tick 可被拖到几十毫秒。
**修复**：把全部 GETTER 默认置 `monitor_tag_slow`（80ms 轮转），曲线追踪时再按需提速。

### SMELL-11 【下位机】`Control_key::can_use` 多方修改

- `Screen_Button::check_press` 内改 can_use；
- `key_set.cpp` 中 `pre_menu_key` / `menu_key_reset` 等直接赋值。
- 改 readonly + setter 更稳。

### SMELL-12 【下位机】`MENU_ITEM` getter 构造函数不对称的代价

```cpp
MENU_ITEM(const char *name, MenuFloatGetter get, ...)   // 强制 type=float
```
注释："当前协议 GETTER 项只支持 float"。当前确实只有 float。
**修复**：协议层显式封死（protocol.md 注明"GETTER 项固定 type_float"），将来扩 type 才改这里。

### SMELL-13 【前端】`serialClient` 重试路径在两处都调 schedule

`cb.onStatus('error', ...)` 末尾会 schedule；`attempt()` 的 finally 也 schedule。两边都 `clearRetry()` 再 `schedule()`，实际上不会双发，但代码读起来费解。
**修复**：抽 `schedule(reason: string)`，传 reason 决定 RETRY_MS vs RETRY_BUSY_MS，调用点只剩 2 处。

### SMELL-14 【前端】`wsTransport.ts:wsAlive` 是死代码

只在 disconnect 中被赋值，无任何读点。
**修复**：删除。

### SMELL-15 【前端】`wsTransport.ts` 的 WS error 回调未置 null

`disconnect()` 只 `onclose = null`，未置 `onerror`，可能上报无意义的 error。

### SMELL-16 【前端】`serialClient` `autoConnect` 在 supported===false 时仍跑 `isPortReady`

`init()` 已判过 supported；autoConnect 里的检查冗余。

### SMELL-17 【下位机】`Set_Tunable` payload 长度与"按类型读"不一致

- 协议 `cmd_payload_len` 对 `Set_Tunable` 返回 9B 定长。
- `write_value` 按 `item.data_type` 真实宽度只取前几个字节，高位补零被自然忽略。
- 功能正确，但**浪费 7B 带宽**（对 BOOL/INT8 类型）。
- 修复：保留定长是"无表不能推断长度"的妥协，注释要写清楚这是有意的。

### SMELL-18 【后端】`api_disconnect` 的顺序防御

`bridge.disconnect()` 可能与 `_on_serial_status` 跨线程，存在"set_want=False 还未生效 _lost 已上报 disconnected"的极小窗口。当前用注释说明。

### SMELL-19 【后端】`_stop_index` 兜底 0x80 在目录未到时

`if cmd == self._stop_index()`（0x80）—— 目录未到时所有 cmd==0x80 都被当停车。巧合概率低，且路径 5 仍会透传，OK。但属于"信任固件目录"的隐式契约。

### SMELL-20 【下位机】`Control_func::Screen_Button::last_draw_pressed_button` 是 static

可能被多实例共享污染；当前全局单实例，OK。

### SMELL-21 【前端】`globle.ts` 50ms / 1s timer 永不 clear

模块级单例 OK；但 HMR 重求值时旧 interval 仍跑，造成双发。
**修复**：interval 持有引用，disconnected 时 `clearInterval` 暂停（不要清，否则连接上不重启）。

### SMELL-22 【前端】`globle.ts::pushDiag` 用 splice O(n) 限 60 条

高频 RX 时每秒 100 次 120 长度扫描 ≈ 12000 ops/s，可接受但不优雅。
**修复**：分两个 buffer：RX 单独一个 60 长 vec，非 RX 单独 60 长 vec。

### SMELL-23 【后端】`hub.fw_sub` 在 rx 路径无锁写

```python
self.fw_sub[idx] = (tag_subscribed(item.tag), tag_fast(item.tag))  # rx 线程
```
- GIL 下单 key 写原子；但与 HTTP 线程同时 `dict.items()` 遍历时不安全。
- 当前没有这种调用，但属隐式契约。

### SMELL-24 【前端】`globle.ts:clearMonitorData` 未清 `restoredSubs`

仅在 `onStatus('connected')` 清；用户连接中手动 `fetchMonitor()`，旧的 `restoredSubs` 仍挡住"按意向补订"。

### SMELL-25 【前端】`globle.ts` 心跳清理时机

`onStatus('disconnected', ...)` 没看到有 stopPing——重连后 `onStatus('connected', ...)` 启动 PingTimer：但若原 timer 还在跑会叠加？startPing 内部 `stopPing()` 再创建，OK。但模块级初始化的写法不直观。

### SMELL-26 【下位机】注释与代码行为的小不一致

- `monitor.h` 说"启动 2200ms" → cpp 用 2300ms。
- `my_motor_group.cpp::transit_freeze_us` 单位 us，注释说"约合 10ms 周期的两拍"（20000us = 20ms = 两拍），OK。
- `Control_func.h::first_press_gap = 65535` 用 lpress——见 SMELL-9。
- `Main` 注释"通信收发都在独立的... 不能在主循环再调用"——main 也没在主循环调 rx_task，OK。

---

## 4. 🟢 改进建议（非 bug，按 AGENTS.md 风格决定取舍）

### IMP-1 把"取一位浮点的 GETTER"扩成"取任意类型"时同步更新 protocol

当前 `MENU_ITEM::data_type = type_float` 是协议硬约束。扩 type 时要同步更新：
- `append_value` 的 GETTER 分支
- TS `FrameParser.decodeValue`
- Python `decode_value`
- 注释四处都要更新。

### IMP-2 监控表"KIND 位"目前只用 PosX/PosY/Yaw 三种

`MonitorKind` 枚举只用到 0~3。预留 4~7 给将来场地图扩展（比如指定底盘坐标系等）。当前实现已支持语义占位，无需改。

### IMP-3 `position_pid` 单位统一到秒，与 `my_motor_group` 同步

`my_motor_group.cpp` 注释"积分单位 pct·秒"，`pid.h` 也"秒量纲"——一致。但 `previous_error` 单位是 `pct`，与 I 项单位 `pct·秒` 混用，建议加到字段注释里。

### IMP-4 全局 README / 协议文档化

- `backend/README.md` 未提 `V5_PORT` / `V5_BAUD` / `V5_AUTO_CONNECT` 环境变量。
- HTTP 错误码（404/409/400）含义没解释。
- `StatusMessage.op="status"` JSON 字段契约缺失。
- `sub type="both"/"push"/"log"` 三种语义的表格已有，但需在 README 标"接口边界"段。

### IMP-5 `cmd` handler 输入校验

- `cmd_turn / cmd_move / cmd_goto / cmd_set_volt / cmd_set_spin / cmd_reset_pos` 全部 memcpy 后直接用——没按 `ranges` 做 clamp。
- AGENTS.md 第 2 节"无条件信任下游使用者"，故 OK，但要写一个 `clamp(values, ranges)` helper 收口，将来加范围只需要在表里加不写 handler。

### IMP-6 `tag` 位域"默认 tag"组合的魔法

`monitor_tag_*` 命名一致但语义需要看注释。可考虑用 enum class：
```cpp
enum class MonitorPreset { None, Slow, Fast, PosX, PosY, Yaw };
```
但会与现有"raw uint8_t"接口不兼容，破坏三端同步。当前风格下"魔法组合 + 注释常量"是合理的。

### IMP-7 测试覆盖盲区（`backend/test_protocol.py`）

- 缺：`command_send` / `set_tunable` 的组帧测试。
- 缺：各种 ValueType 的编码边界（Float/Double/Int8/UInt8/UInt16/UInt32 的负数/超界/对齐）。
- 缺：`scan_dir_frames` 的解析测试。
- 缺：MAX_PAYLOAD 越界拒绝测试。
- 缺：并发测试（`_aggregate` 并发、`enqueue` 排序稳定性）。
- 缺：`test_hub_proxy.py` 是占位（`pytest/` 目录）？建议补一个最小 hub 集成测试。

### IMP-8 模拟数据假数据合理化

`mockTransport.ts:42` `幸运数2 = 1919810`、`114514` 这类固定值对调试逻辑没问题；但 10ms 心跳 / 80ms 慢轮转的时序会因一个"读 → 设 → 读"的循环累积误差；脚本/单元测试应避免依赖这种 timing-sensitive mock 输出。

### IMP-9 `hub._tx_q` 用 `bisect.insort` 实现的有序列表替换 deque

```python
import bisect
insort(self._tx_list, (priority, seq, frame))  # O(log n)
...
pri, seq, frame = self._tx_list.pop(0)  # O(n)，但 FIFO 同级不必保序
```
或：
```python
import sortedcontainers
self._tx_sl = SortedList(key=lambda x: (x[0], x[1]))
```
需要新依赖（`requirements.txt` 加 `sortedcontainers`）。

### IMP-10 `chassis.begin_segment` 把 `cos_i/sin_i` 预计算一次是合理设计

但 `chassis.update` 里 `cosf(middle_heading)`/`sinf(middle_heading)` 每拍重算，是必要的（角度会变）。注释明确"用本轮开始、结束角度的中间值"，数学上 OK。

### IMP-11 主循环 10ms 周期对齐在 `my_while` / `screen_refresh_event` / `comm.tx_tick` 三处独立维护

`CycleTimer main_timer(10)` 在 `my_while`、屏幕 `screen_timer(10)`、AI vision 用 40ms 周期——三个独立定时器。V5 调度器独立任务，未必有严格保证；需确认每条任务是否能抢到 10ms 内完成。

### IMP-12 `screen_refresh_event` 与 `comm.tx_tick` 共用 10ms 线程？

注释"tx_tick 由 my_while 调用"——意味着通信 tx 跑在 `my_while` 的 10ms 周期里；屏幕刷新是独立线程；AI vision 是独立线程（40ms）。这就**没有热点冲突**——OK。

### IMP-13 把 protocol.md 单独抽出来

三端 README 都引同一份协议真源。当前写在每个文件顶部，OK；但可以考虑在仓库根加一个 `PROTOCOL.md`，三处用注释"// 同步自 PROTOCOL.md"指过去。

---

## 5. 模块评分（按"主线简洁易读"为标准）

| 模块 | 评分 | 评语 |
| --- | --- | --- |
| `include/communication.h` | ⭐⭐⭐⭐⭐ | 注释密度、契约、执行模型都写透 |
| `src/class_and_tool/communication.cpp` | ⭐⭐⭐⭐⭐ | 同上 |
| `backend/app/protocol.py` | ⭐⭐⭐⭐ | 干净，enum/dataclass 结构清晰；唯缺 Str/Other |
| `frontend/src/services/protocol.ts` | ⭐⭐⭐⭐ | 一致，注释到位；commandSend 类型断言与 mockTransport 副本是扣分项 |
| `src/robot_and_control.cpp` | ⭐⭐⭐⭐ | 动作命令表清晰；但 cmd_set_volt/set_spin/set_pos handler 无任何校验，与 AGENTS.md"信任下游"取向一致 |
| `src/class_and_tool/chassis.cpp` | ⭐⭐⭐⭐⭐ | 里程计更新算法注释+代码匹配 |
| `src/class_and_tool/my_motor_group.cpp` | ⭐⭐⭐ | 状态机多、入口多，速度环核心算法逻辑正确；但 if/else if 链密集，新人需多读才能懂判据顺序；Type_double castbug 休眠 |
| `backend/app/hub.py` | ⭐⭐⭐⭐ | 数据结构清晰；但 _drop_client/clear_script 重复、enqueue 全排序 O(n log n) |
| `backend/app/serial_bridge.py` | ⭐⭐⭐⭐ | 探测、心跳、超时三件套齐；disconnect/_lost 重复；ping timeout 比下位机长 1 倍（合理） |
| `frontend/src/services/serialClient.ts` | ⭐⭐⭐⭐ | 状态机完备；重试双发路径费解 |
| `frontend/src/services/webSerialTransport.ts` | ⭐⭐⭐⭐⭐ | 浏览器兼容处理细致 |
| `frontend/src/services/wsTransport.ts` | ⭐⭐⭐ | 多数干净；wsAlive 死代码 |
| `frontend/src/services/mockTransport.ts` | ⭐⭐ | 与 protocol 大量重复；setTimeout 未清理；无长度校验 |
| `frontend/src/services/persist.ts` | ⭐⭐⭐ | 字段级 validator 链设计好；缺 beforeunload flush |
| `frontend/src/stores/globle.ts` | ⭐⭐⭐ | 单文件 968 行不优雅；traceOrigSubs bug 是真问题；50ms 同步层合理 |
| `src/monitor.cpp`、`src/class_and_tool/filter.cpp` | ⭐ | 整文件 dead code |
| `src/class_and_tool/pid.cpp` | ⭐⭐⭐⭐ | 秒量纲 + 抗积分饱和标准实现；缺除零保护 |
| `src/class_and_tool/menu_func.cpp` | ⭐⭐ | Type_double castbug；shift 空表 UB |
| `src/ai_vision.cpp` | ⭐⭐⭐ | 干净，但 awb_start_time 类型截断 |

---

## 6. 待修清单（按优先级）

### P0（必修，会引发静默错误）
1. **BUG-1** `menu_func.cpp` + `tool.cpp` 的 `type_double` cast bug
2. **BUG-2** `ai_vision.cpp::awb_start_time` 改 `uint32_t`
3. **BUG-5** `globle.ts::syncTraceSubs` 取消追踪时不要 delete orig；或独立维护"用户原始订阅"
4. **BUG-6** `mockTransport` `setTimeout` 加 cleanup
5. **BUG-8** `api_tunable` 加 `ValueError → 400`

### P1（强烈建议，下赛季前修）
6. **BUG-3** `MENU::shift()` 空 monitor 表保护
7. **BUG-4** `PositionPID::update` D 项除零保护
8. **BUG-7** `hub.enqueue` 用 `SortedList`/`bisect.insort` 换全排序
9. **BUG-9** `test_protocol.py` tag 改用 `cmd_tag_make | CMD_TAG_RANGE`
10. **BUG-10** `find_cmd` 用 uint16 比较代替 uint8 加法
11. **BUG-11** `chassis.h::wheel_r` 注释自洽
12. **SMELL-7** `monitor.cpp` / `filter.cpp` 要么暴露头文件，要么删除
13. **SMELL-4** `mockTransport` 的 frame/appendValue/decodeValueLE 改成复用 `protocol.ts`
14. **SMELL-5** `persist.ts` 加 `pagehide` 同步 flush
15. **SMELL-6** `mockTransport` Subscribe/SetTunable 加长度校验

### P2（卫生改进，可随手上）
16. **SMELL-1** 抽 `_drop_source_intent`
17. **SMELL-2** 抽 `serial_bridge._teardown`
18. **SMELL-8** `PositionPID` 的 `kp/ki/kd/integral/previous_error` 改 private + getter
19. **SMELL-9** `Control_key` 的 `lpress+65535` 用法与注释对齐（要么加 `ENABLE_RELEASE_EDGE` 模式，要么改注释）
20. **SMELL-10** 监控表 GETTER 默认置 `monitor_tag_slow`
21. **SMELL-14** `wsTransport.wsAlive` 删
22. **SMELL-15** `wsTransport` disconnect 时 `onerror = null`
23. **SMELL-22** `globle.ts::pushDiag` RX/non-RX 分两个 buffer
24. **SMELL-25** `globe.ts` 心跳 timer 在 disconnect/connected 转换时机理顺
25. **SMELL-21** 50ms/1s timer 在 disconnected 时 `clearInterval`，connected 时重启
26. **SMELL-24** `clearMonitorData` 清 `restoredSubs`
27. **P5-Python** `protocol.py::ValueType` 加 `Str=10, Other=14` 显式占位（注释"暂不编码"也行）
28. **Protocol-TS** `commandSend` 不再用 `as CmdGet`，加 `buildFrame(cmd: number, payload: number[])` 重载

### P3（文档）
29. `PROTOCOL.md` 抽出三端同步真源
30. `backend/README.md` 补 `V5_PORT/BAUD/AUTO_CONNECT` 环境变量说明、HTTP 错误码表、StatusMessage JSON 契约

---

## 7. 总结

**整体评价**：项目结构清晰、注释密度高、自带模块边界纪律。三端协议一致性基本达标，少数缺口已记录。

**关键风险**集中在三处：
1. 下位机 `type_double` cast bug 与 `awb_start_time` 类型截断——休眠/低概率触发；
2. 前端 `traceOrigSubs` 二次追踪与 `mockTransport` setTimeout 清理——上线会偶发；
3. 后端 `_tx_q` 全排序——高频下发时性能隐患。

**值得欣慰**：
- 协议三端**完全一致**是合规的；
- `merge_subscribe_tag` "只动 SUB/FAST"约束在三端都正确实现；
- 帧头/XOR/小端约定无歧义；
- 下位机 RX 状态机、TX 节拍、心跳看门狗、Set_Tunable 回显真实值的设计严谨；
- 后端优先队列 STOP < UI < SCRIPT、订阅按 source 分开记、目录缓存回放的设计精致；
- 前端 serialClient HMR 单例、bailAfter 兜底、setSignals DTR/RTS 都体现了工程师对浏览器/USB 边界的细致理解。

建议按 P0 → P1 → P2 → P3 顺序逐步修；P0 五项都是低风险高收益改动，可在一次提交里一起处理。
