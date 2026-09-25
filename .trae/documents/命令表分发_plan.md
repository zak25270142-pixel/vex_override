# 命令表分发系统 实施计划

## 已确认的决策

1. **删除 `need_exec` 字段**：动作/忙时等处理细节全部归各命令 handler 自己设计，comm 不解释。
2. **首批登记 4 个真实命令**（直接转发 robot_action）：
   - `0x80` 停止运动（无参）→ `stop_move()`
   - `0x81` 原地转向（float angle，deg）→ `turn()`
   - `0x82` 直行（float distance，m）→ `move()`
   - `0x83` 局部移动（float x / float y / float heading）→ `goto_local()`
3. **命令目录连接后自动拉取**（与调参表/实参表一致）。
4. **无 ACK**：下发即走，车的状态靠实参表监控量间接观察。

## Repository Research（调研结论）

### 固件（d:\work\vex\test1）

- 帧：`A5 | Cmd | Payload | XOR`，rx 状态机靠 `cmd_payload_len()` 定长收 payload；收齐校验后 `handle_command()` 同步执行。
- 内建命令字现状：Cmd_Get 0~3（Request_Tunable/Request_Monitor/Subscribe/Set_Tunable）+ Ping=0xFF；Cmd_Post 0~3 + Pong=0xFF。内建段 0x00~0x0F 有充足空位。
- 目录发送已有成熟范式：`dir_tun/dir_mon` 倒计时 + `send_dir_batch()` 每拍 8 项，tx_tick 里 `if/else if` 串行。
- rx 线程协作式模型：handler 只许快速赋值，不许 write/Delay。`robot_action.turn/move/goto_local/stop_move` 只做目标赋值与指针接力，满足该约束。
- `robot_action` 是 robot_and_control.cpp 的全局对象，handler 薄封装放该文件最自然，不新建文件。
- 命令字 `0x80-0x9F` 共 32 槽，与内建段、0xFF 均不冲突；可用 `cmd & 0x1F` 直接索引。

### 前端（d:\work\vex\SWJ\frontend）

- 布局：App.vue = AppHeader + main.workspace > ViewStage（ViewNav 顶部分页 + v-show 四视图）。改左右分栏只需在 workspace 内套一层 flex。
- 协议层 protocol.ts：FrameParser 对变长目录帧采用"收够前缀字节后算出总长"的模式；现有 `encodeValue8()` 可抽出按类型编码原语。
- store（globle.ts）：tunable/monitor 两张表 + fetch 动作 + 连接自动拉取，命令表照抄第三套。
- mockTransport.ts 已模拟目录/订阅/SetTunable，需补命令目录与命令帧 no-op。

## 协议定稿

### 命令字分配

- `Cmd_Get::Request_Command = 4`（内建段，无 payload）
- `Cmd_Post::Post_CMD_Directory = 4`
- 用户命令段 `0x80~0x9F`，32 条上限；帧里 index 字段直接用命令字本身。

### 命令目录帧（一条命令一帧）

```
[index 1B = 命令字] [name_len 1B] [命令名 UTF-8]
[input_num 1B] ( [type 1B] [fname_len 1B] [字段名 UTF-8] ) × input_num
```

- 无参命令 input_num=0（如停止）。
- 静态约束：命令名截断 24B、单字段名截断 10B、参数量上限 6。
- **MAX_PAYLOAD 由 128B 扩大到 255B**（多参数余量；len 字段为 uint8，255 是天然上限；
  整帧 258B < USB 单包 512B，不会被拆包）。固件与前端的 MAX_PAYLOAD 同步修改。
- payload 编码（下发帧）：各字段按 `value_size(type)` 真实宽度**紧凑小端**排列，
  不补零（与 SetTunable 的定长 8B 不同）；总长 = Σvalue_size，无需在表里冗余存储。

### CMD_ITEM（定稿，删 need_exec / payload_len，指针 const 化）

```cpp
typedef void (*CmdFunc)(const uint8_t *p); // rx线程上下文执行，禁阻塞/让出/write
struct CMD_ITEM
{
    CmdFunc func;                  // 处理函数，可在任意文件写好后登记
    uint8_t index;                 // 命令字，0x80~0x9F
    const VALUE_TYPE *data_type;   // 字段类型表
    const char *const *field_name; // 字段中文名表
    uint8_t input_num;             // 字段数
    const char *name;              // 命令中文名（目录/诊断用）
};
```

## Files and Changes

### 固件

- `include/communication.h`
  - Cmd_Post 加 `Post_CMD_Directory=4`；Cmd_Get 加 `Request_Command=4`。
  - 加 CmdFunc typedef、CMD_ITEM 结构（如上）。
  - 私有：`const CMD_ITEM* cmd_slots[32]`（类内零初始化）、`uint8_t cmd_count=0`、
    `uint8_t dir_cmd=0`、常量 `CMD_DIR_BATCH=4`、`CMD_SLOT_MAX=32`、
    `CMD_NAME_MAX=24`、`CMD_FIELD_NAME_MAX=10`、`CMD_INPUT_MAX=6`。
  - 私有方法：`const CMD_ITEM* find_cmd(uint8_t cmd) const`、
    `send_cmd_dir_batch(uint8_t batch)`、payload 总长累加辅助。
  - 公有：`void register_command(const CMD_ITEM *item)`（越界/重复 index 拒绝，重复直接覆盖）。
- `src/class_and_tool/communication.cpp`
  - `cmd_payload_len()`：内建 switch 之外，find_cmd 命中则返回 Σvalue_size，否则 0xFF。
  - `handle_command()`：加 Request_Command（置 dir_cmd 倒计时）；
    switch 之后 find_cmd 命中则 `item->func(rx_payload)`。
  - `send_cmd_dir_batch()`：扫 cmd_slots 非空项，按注册顺序每拍 4 条组目录帧。
  - `tx_tick()`：目录优先级 dir_tun > dir_mon > dir_cmd（末尾 else-if 链）。
- `src/robot_and_control.cpp`：4 个 static handler + static const CMD_ITEM 表 +
  `void register_robot_commands()`（逐条 comm.register_command）。
  - turn/move/goto handler 内加 `if (robot_action.is_busy()) return;` 忙时忽略；
    stop 无守卫，随时可停。
- `include/robot_and_control.h`：声明 `register_robot_commands()`。
- `src/my_main.cpp`：`comm.init()` 后调 `register_robot_commands()`（broadcast 之前）。

### 前端

- `src/services/protocol.ts`
  - 枚举加 RequestCommand=4 / Post_CMD_Directory(命名 CmdPost.CMDDirectory)=4。
  - 类型 `CmdField { type: ValueType; name: string }`、
    `CmdDirItem { index; name; fields: CmdField[] }`；RxEvent 加 `cmdDirectory`。
  - 从 encodeValue8 抽出 `encodeValue(type, value): Uint8Array`（真实宽度），
    encodeValue8 复用它并补零 8B。
  - `requestCommand()`、`commandSend(index, fields, values)`（紧凑编码）。
  - FrameParser：WaitCmd 白名单加；新增渐进走查函数
    （前缀不全返回 -1，name→input_num→逐字段走完得总长）；dispatch 拼 CmdDirItem。
- `src/stores/globle.ts`
  - `commandMap`、`fetchCommands()`（connected 时自动调）、
    handleEvent 收 cmdDirectory、`sendCommand(index, values)`（走 sendFrame 记日志）。
- `src/services/mockTransport.ts`
  - 内置 3 条 mock 命令（停止无参/转向 float/goto 三 float）的目录帧；
    Request_Command 回目录；收到 0x80~0x9F no-op（不报错）。
- `src/App.vue`：workspace 内改为左右分栏 flex 容器，左 flex 7 包 ViewStage，
  右 flex 3（min-width 260px）挂新面板。
- 新增 `src/components/command/CommandPanel.vue`
  - 命令下拉（index + 名称）→ 动态字段表单（字段名+类型小标签；
    数值 input step=any、bool/on_off 复选、color 取色器）→「下发」按钮。
  - 未连接/空表/数值非法时按钮禁用；下发后保留表单，状态栏与诊断日志可见。
  - 无二次确认（need_exec 已删）。

## Implementation Steps（依赖顺序）

1. 固件：枚举 + CMD_ITEM + 注册表/查找 + cmd_payload_len/handle 分发。
2. 固件：命令目录组帧 + dir_cmd 倒计时接入 tx_tick。
3. 固件：robot_and_control.cpp 四命令 + 注册，my_main 挂载；clang 语法检查。
4. 前端：protocol.ts 枚举/类型/编码/parser/组帧函数。
5. 前端：store commandMap/fetch/send + 连接自动拉取。
6. 前端：mockTransport 命令目录与 no-op。
7. 前端：App.vue 分栏 + CommandPanel.vue。
8. 验证：npm run build；浏览器演示模式联调面板与下发。

## Validation

- 固件 clang `-fsyntax-only -Wall -Wextra` 编译 communication.cpp / robot_and_control.cpp / my_main.cpp，EXIT=0。
- 前端 `npm run build`（vue-tsc）通过，仅既有 chunk 体积警告。
- 浏览器演示模式：右侧列出 mock 命令；选 goto 填三参下发，诊断日志出现对应 TX 帧（13B：A5 83 + 12B payload + XOR）；左侧四视图切换、调参表等既有功能不受影响；30% 栏宽下表单可用。
- 提醒用户真机 Download 后联调 0x80~0x83。

## Risks

- **parser 渐进走查写错会误判帧长**：走查函数严格"字节不够→待定"，对照固件组帧偏移单测性走读；演示模式实测三帧。
- **rx 线程调 robot_action**：协作式线程 + handler 纯赋值，沿用既有结论；handler 内禁止 Delay/write，忙时守卫只做一次 is_busy 读。
- **128B 越界**：名称截断与参数上限在组帧处强制执行，不靠命令作者自觉。
- **重复/越界注册**：register 内做 index 段校验，重复 index 直接覆盖槽位并保持 cmd_count 不重复累加（按槽位非空计）。
- **mock 与真机格式漂移**：两边目录帧字段顺序严格按本协议同节实现。
