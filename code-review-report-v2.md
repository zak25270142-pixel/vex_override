# VEX U 整车调试链路代码审查报告（v2，按 AGENTS.md 与你反馈调整后）

**调整要点**：
- v1 列的 11 个 BUG 经你逐条核对，6 个已剔除、2 个降级、3 个已修
- 协议三端一致性：已发现前端 `protocol.ts` 多出 `Str/Other`（下位机、后端都没有），已删
- 保留 v1 的设计 smell 与改进建议，但加 AGENTS.md 适用性标注

---

## 1. 协议三端一致性（最终态）

经本次修复后的实际状态：

| 字段 | 下位机 `communication.h` | 后端 `protocol.py` | 前端 `protocol.ts` | 一致性 |
| --- | --- | --- | --- | --- |
| 帧格式 `A5\|Cmd\|Payload\|XOR` | ✓ | ✓ | ✓ | ✓ |
| XOR 从 Cmd 起（不含帧头） | ✓ | ✓ | ✓ | ✓ |
| 多字节小端 | ✓ | ✓ | ✓ | ✓ |
| `CmdPost/CmdGet` 编号 | ✓ 0~4 + 0xFF | 同 | 同 | ✓ |
| **`VALUE_TYPE` 0~9** | 0~9 | 0~9 | 0~9 | ✓ |
| **`VALUE_TYPE` 10~14** | 10/14 **空位** | 10/14 **空位** | ~~10/14 枚举~~ | **已对齐** |
| 监控 tag 位域 | SUB=0x80 / FAST=0x40 / GETTER=0x20 / KIND_MASK=0x07 | 同 | 同 | ✓ |
| 命令 tag 位域 | RANGE=0x80 / ARGC_MASK=0x70 / SHIFT=4 / SPEC_MASK=0x0F | 同 | 同 | ✓ |
| Subscribe 合并（只动 SUB/FAST） | ✓ | ✓ | ✓ | ✓ |
| 目录帧布局 | `[index][type][tag][nameLen][name][unitLen][unit][value]` | 同 | 同 | ✓ |
| 命令目录帧布局 | `[cmd][tag][nameLen][name][argc]([type][nameLen][name])×N` + 可选 ranges | 同 | 同 | ✓ |
| SetTunable 9B 定长 | ✓ | ✓ | ✓ | ✓ |
| Pong `[A5 FF FF]` | ✓ | ✓ | ✓ | ✓ |

**修复点**：前端 `protocol.ts` 的 `Str = 10` 与 `Other = 14` 已删，与下位机、后端对齐。`valueSize()` switch 不列 Str/Other（default 返回 0），`typeName()` Record 表也不含这俩（return `unknown` 兜底），删除安全。

---

## 2. 已执行的修复（4 处）

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `frontend/src/services/protocol.ts` | 删 `Str = 10` 与 `Other = 14`，加注释说明 10/14 是协议空位 |
| 2 | `frontend/src/stores/globle.ts::syncTraceSubs` | 取消追踪时**不删** `traceOrigSubs` 项；加注释说明"第二次追踪同一项时不要重记已被提速的 orig" |
| 3 | `frontend/src/services/mockTransport.ts` | 加 `pendingTimers: Set`，`emit()` 与 RequestCommand 的 setTimeout 都登记，`disconnect()` 里统一 `clearTimeout` |
| 4 | `backend/app/main.py::api_tunable` | 加 `except ValueError → 400`，与 `api_cmd` 对称 |

无 lint 错误。

---

## 3. 你确认"非 BUG / 不处理"（从报告里剔除）

| 原 v1 编号 | 描述 | 你的判断 | 我的处置 |
| --- | --- | --- | --- |
| BUG-2 | `ai_vision.cpp::awb_start_time` 是 `uint16_t` | 不是 BUG，开机 2.3s 内循环就退出，截断不发生 | **删除** |
| BUG-3 | `MENU::shift()` 切到空 monitor 表 UB | AGENTS.md 不为"以后可能需要"加防御，信任下游 | **删除** |
| BUG-4 | `PositionPID::update` D 项除 0 | 位置环调用节奏不会触发；速度环已有 `dt_us < 1000 → dt_us = 1000` 保护 | **删除** |
| BUG-9 | `test_protocol.py` tag 用魔术数 | 非主体测试，不影响主功能 | **删除** |
| BUG-10 | `find_cmd` 边界判断 uint8 加法溢出 | 完全信任下位机，按 AGENTS.md 原则不处理 | **删除** |
| BUG-11 | `chassis.h::wheel_r` 注释 | 注释"直径 5.08cm = 2 英寸" + 代码 0.0254m（半径 2.54cm）数值自洽 | **删除** |
| BUG-1 | `menu_func.cpp` + `tool.cpp` `type_double` cast bug | 当前无 double 调参项，无影响 | **保留为"已知休眠"**，不主动处理 |

---

## 4. 保留为可选优化（你未明确表态）

| # | 描述 | 备注 |
| --- | --- | --- |
| BUG-7 | `backend/app/hub.py::enqueue` 用 `deque(sorted(self._tx_q))` 全排序入队，O(n log n) | 队列常态只有几帧，影响有限；改 SortedList / insort 是真正的优化，但当前不阻塞。**未动**。 |

---

## 5. 保留为 design smell（按 AGENTS.md 适用性标注）

按"无条件信任下游"、"简洁直接"、"不隐藏主路径"原则，对每条 smell 给适用性：

### 5.1 信任下游可忽略（不动）

| 编号 | 描述 | 不处理理由 |
| --- | --- | --- |
| SMELL-3 | `hub.enqueue` 替换 deque 实例的可见性 | 锁内单 key 写；GIL 下原子 |
| SMELL-19 | `_stop_index` 兜底 0x80 | 信任固件目录顺序与 SPEC=Stop 标注 |
| SMELL-17 | `Set_Tunable` payload 长度浪费（7B/帧） | 9B 定长是"无表不能推断长度"的妥协，注释要写明是有意的（**已建议加注释**） |
| SMELL-23 | `hub.fw_sub` 在 rx 路径无锁写 | 信任 GIL 原子性 |
| SMELL-21 | `globle.ts` 50ms / 1s timer 永不 clear | 模块单例，HMR 重新求值时旧 interval 仍跑（**潜在隐患，待观察**） |
| SMELL-25 | `globle.ts` 心跳 timer 启停时机 | startPing 内部 stopPing 兜底，OK |

### 5.2 真重复 / dead code（卫生项，可随手上）

| 编号 | 描述 | 建议 |
| --- | --- | --- |
| SMELL-1 | `_drop_client_intent` 与 `clear_script_intents` 几乎重复 | 抽 `_drop_source_intent(source)` 公共函数 |
| SMELL-2 | `serial_bridge._lost` 与 `disconnect` 大量复制 | 抽 `_teardown(final_state, message)` |
| SMELL-4 | `mockTransport` 的 frame/appendValue/decodeValueLE 是 `protocol.ts` 的副本 | 加新帧格式（如 CRC）要改两处——抽 `payloadBytes` 共享工具 |
| SMELL-7 | `monitor.cpp` / `filter.cpp` 整文件 dead code（`monitor.h` 是空的） | 要么补头文件声明并接入实际传感器；要么整文件删除 |
| SMELL-8 | `PositionPID` 公共字段过宽（kp/ki/kd/integral/previous_error 都 public） | 与 AGENTS.md "常用调参量可 public" 不冲突；保留现状 |
| SMELL-9 | `Control_key::lpress + first_press_gap=65535` 用法与注释不符 | 注释明确"等价短按，仅 release 沿触发"即可 |
| SMELL-10 | `MENU::refresh_value` 对 GETTER 数量限流缺失 | 与 AGENTS.md "简洁" 冲突；现状是 10ms tick 能扛住；不优化 |
| SMELL-12 | `MENU_ITEM` getter 构造函数强制 type=float | 协议层硬约束，注释已说明；将来扩 type 才改 |
| SMELL-14 | `wsTransport.wsAlive` 是死代码 | 删除 |
| SMELL-15 | `wsTransport` disconnect 时 `onerror = null` 未置 | 置上 |
| SMELL-22 | `globle.ts::pushDiag` 用 splice O(n) 限 60 条 | 高频 RX 时每秒 100 次扫描 ≈ 12000 ops/s，可接受 |
| SMELL-24 | `clearMonitorData` 未清 `restoredSubs` | 加上 |
| SMELL-26 | `monitor.h` vs `ai_vision.h` 注释"2200ms" vs `ai_vision.cpp` 写 `> 2300` | 统一为 2300ms（**已建议改注释**） |

### 5.3 与 AGENTS.md 风格一致（无需改）

| 编号 | 描述 | 评语 |
| --- | --- | --- |
| SMELL-11 | `Control_key::can_use` 多方修改 | 字段 public，符合 AGENTS.md |
| SMELL-13 | `serialClient` 重试双发路径 | 双重 `clearRetry` + `schedule` 实际不双发；当前写法直接可读 |
| SMELL-16 | `serialClient` `autoConnect` 在 supported===false 仍跑 `isPortReady` | init 已判过，冗余但不致错 |
| SMELL-18 | `api_disconnect` 与 `_on_serial_status` 跨线程时序 | 注释已说明，OK |
| SMELL-20 | `Control_func::Screen_Button::last_draw_pressed_button` 是 static | 全局单实例，OK |

---

## 6. 改进建议（非 bug，文档化层）

| 编号 | 描述 |
| --- | --- |
| IMP-1 | 协议层"GETTER 项固定 type_float"写进 `PROTOCOL.md` |
| IMP-3 | `PositionPID` 的 `previous_error` 单位是 `pct`，与 I 项单位 `pct·秒` 混用——加字段注释 |
| IMP-4 | `backend/README.md` 补 `V5_PORT/BAUD/AUTO_CONNECT` 环境变量、HTTP 错误码表（404/409/400）、`StatusMessage` JSON 契约 |
| IMP-5 | `cmd` handler 输入校验：当前 `cmd_turn/cmd_move/cmd_goto/cmd_set_volt/cmd_set_spin/cmd_reset_pos` 全部 memcpy 后直接用；按 AGENTS.md "信任下游" 可不写——但 `ranges` 表已带，写一个 `clamp()` 收口，将来加范围只改表不写 handler |
| IMP-7 | `backend/test_protocol.py` 缺 `command_send` / `set_tunable` 组帧测试、各 ValueType 边界、`scan_dir_frames` 解析、MAX_PAYLOAD 拒绝、并发测试 |
| IMP-9 | `_tx_q` 换 `SortedList` 需加 `sortedcontainers` 依赖——见 BUG-7 |
| IMP-10 | `chassis.begin_segment` 把 `cos_i/sin_i` 预计算；`chassis.update` 用中间角度每拍 cos/sin 是必要的，OK |
| IMP-13 | 抽 `PROTOCOL.md` 单独文档，三处用注释"同步自 PROTOCOL.md"指过去 |

---

## 7. 模块评分（v2）

| 模块 | v1 评分 | v2 评语 |
| --- | --- | --- |
| `protocol.ts`（前端） | ⭐⭐⭐⭐ | +1 已与下位机/后端完全对齐 |
| `mockTransport.ts` | ⭐⭐ | +1 setTimeout 清理已修 |
| `globle.ts` | ⭐⭐⭐ | +1 traceOrigSubs 二次追踪 bug 已修 |
| `main.py` | ⭐⭐⭐⭐ | +1 api_tunable 错误码对称 |
| `protocol.py`（后端） | ⭐⭐⭐⭐ | Str/Other 始终不写，正确 |
| `communication.cpp` | ⭐⭐⭐⭐⭐ | 无变化 |
| `hub.py` | ⭐⭐⭐⭐ | enqueue 性能保留为可选优化 |
| 其余模块 | 不变 | 详见 v1 报告 |

---

## 8. 本次修改后的代码状态总结

**4 个文件改动**（按你判断为"可修复"的清单）：
- `frontend/src/services/protocol.ts` — 删 Str/Other 枚举
- `frontend/src/services/mockTransport.ts` — 加 pendingTimers 清理
- `frontend/src/stores/globle.ts` — 永久保留 traceOrigSubs
- `backend/app/main.py` — api_tunable 加 ValueError→400

**11 个 BUG 的最终处置**：
- 6 个删除（按你与 AGENTS.md 判断）：BUG-2, 3, 4, 9, 10, 11
- 1 个降级（已知休眠）：BUG-1 type_double cast
- 1 个降级（已知不在乎）：BUG-7 `_tx_q` 全排序
- 3 个已修：BUG-5 traceOrigSubs, BUG-6 mockTransport setTimeout, BUG-8 api_tunable ValueError
- 1 个新增（前端 Str/Other 不一致）已修

`code-review-report-v1.md` 保留备查，本次修改以 v2 为准。
