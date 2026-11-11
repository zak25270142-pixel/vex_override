# 架构拓扑整理

## 类

### class CycleTimer

锚定(较)准确的时间周期,在my_main调用,全局性

### class PositionPID

位置环pid类，用于chassis更好实现goto/move/turn

### class MENU

菜单类，但他只是一个普通的单页菜单，有待进化

### class AI_VISION_MENU

视觉仪菜单，平平无奇

### class KalmanFilter

滤波 暂时没用

### class Control_key

抽象按键类，后期肯定会与RobotAction对接，怎么写没想好

### class Screen_Button

屏幕按键实体类，与LCD屏和抽象按键类有关，在key_set配置

### class Remote_Control

手柄遥控类，除了绑定抽象按键类，其本身的摇杆值也是重要的控制判断依据

### class USB_Comm

通信类，负责和上位机通信，与其他模块低耦合

### class MotorGroup / ChassisMotorGroup

电机组类，控制的最底层，速度环的载体。基类 `MotorGroup` 只做采样/前馈 PID/逐台下发，
底盘专属的六态机、积分与压摆策略在派生类 `ChassisMotorGroup`——升降杆等机构直接用基类或另行派生。
组内台数由构造时的 `motor_count` 决定，增减不改代码

### class Chassis

底盘类，位置环的载体，控制的中间层，向上承接RobotAction，向下指挥左右两组 ChassisMotorGroup

### class RobotAction

运动功能集成类，封装各种上层运动

如 turn/move/goto/手动控制

如 各种调试入口

### AI_vision / AI_VISION_MENU

视觉菜单已封装成 `AI_VISION_MENU` 类（见上）；底层传感器取帧与识别结果仍由 `ai_vision.cpp` 的函数提供

## 上位机迁移

frontend 在node.js上运行/直接用npm run build 的构造产物使用

下位机需迁移类class USB_Comm,该类封装了与上位机匹配的通信协议。

然后就只需要提供Menu与命令表，上位机就能识别就能调参画图了。

上位机开箱即用，一个字也不用改。

具体怎么迁移可以把仓库丢给grok问他。

| `frontend/dist/` | 打包好的上位机，浏览器打开即可用 |
| 协议源码 | `include/communication.h`、`src/class_and_tool/communication.cpp` |
| 类型与表项定义 | `include/my_main.h` 里与菜单/通信相关的部分（`VALUE_TYPE`、`MENU_ITEM`、tag 常量等） |
| 本车示例 | 调参表 / 监控表 / `CMD_ITEM` 表怎么写，作参考，不必照搬业务 |

对dist的使用：

1. 解压 `dist`，进入该目录。
2. 起本地静态服务（不能双击 `index.html`）：

```bash
python -m http.server 5173
```

3. 用 **Chrome / Edge** 打开：`http://localhost:5173`
4. USB 接 V5 **User 口**，用户程序已在跑。
5. 关掉 VEXcode 等占串口的程序。
6. 点「选口」→ 选设备 → 等探测通过（靠 Ping/Pong 认 User 口）。

连上后，只要下位机移植了 `USB_Comm` 并挂了表，就会自动要目录、推监控、可调参。

---

下位机怎么接————核心：移植，不是重写

最小集合：

```text
communication.h
communication.cpp
```

以及 `my_main.h` 中与通信相关的定义（可单独抽成 `comm_types.h`）：

- `VALUE_TYPE` 枚举（编号必须与上位机 `protocol.ts` 一致）
- `MENU_TAG_*` 位域常量
- `MENU_ITEM` 结构体 + 两个构造函数（内存指针 / GETTER）
- `MenuFloatGetter` 类型

`USB_Comm` 依赖：

- `open` / `read` / `write`（`/dev/serial1`，V5 User 口原始 FIFO）
- 对方工程里提供的 `MENU_ITEM` 表、可选 `CMD_ITEM` 表

不依赖你的 `Chassis`、`RobotAction`、LCD。

工程里要做的三件事

**① 定义自己的三张表（内容随便，格式固定）**

```cpp
// 调参：const，data_ptr 指向可写变量
const MENU_ITEM my_tunable[] = {
    {"kp", type_float, &my_pid.kp, "比例增益", ""},
    // ...
};

// 监控：不能 const（订阅会改 tag）
MENU_ITEM my_monitor[] = {
    {"x", type_float, &pose.x, "X", "m", monitor_tag_pos_x},
    {"rpm", [] { return motor.velocity(pct); }, "转速", "pct", monitor_tag_fast},
    // ...
};

// 动作命令（可选，0x80~0x9F）
// 字段顺序固定为：{处理函数, 字段类型表, 字段名表, 命令中文名, tag, 取值范围}
//   tag 用 cmd_tag_make(参数个数, 特化类型) 拼；bit7（带取值范围）不用手填，
//   发目录时按 ranges 是否为空自动置位，表项不会两处不一致
//   ranges 为 nullptr 表示不带范围；非 nullptr 时按字段顺序紧凑排 {min1,max1,min2,max2,...}
static const VALUE_TYPE cmd_f_type[] = {type_float};
const CMD_ITEM my_cmds[] = {
    {cmd_stop, nullptr, nullptr, "停止", cmd_tag_make(0, CMD_SPEC_STOP), nullptr},
    {cmd_turn, cmd_f_type, (const char *[]){"角度(度)"}, "原地转向", cmd_tag_make(1, CMD_SPEC_NORMAL), nullptr},
    // ...
};
```

**② 构造并初始化 `USB_Comm`**

```cpp
USB_Comm comm(
    my_tunable,  (uint8_t)(sizeof(my_tunable)/sizeof(my_tunable[0])),
    my_monitor,  (uint8_t)(sizeof(my_monitor)/sizeof(my_monitor[0])),
    my_cmds,     (uint8_t)(sizeof(my_cmds)/sizeof(my_cmds[0]))  // 没有就传 nullptr, 0
);

// 初始化里：
comm.init();                    // open("/dev/serial1")
// 可选：comm.on_link_lost = 你的掉线收车函数;
```

**③ 两个调度点（和你仓库一致即可）**

```cpp
// 独立任务，内部死循环阻塞 read
vex::event rx_ev([]{ comm.rx_task(); });
rx_ev.broadcast();

// 主循环约 10ms 一次
void loop_10ms() {
    // ... 其它业务
    comm.tx_tick();   // 唯一写串口的地方：Pong / 回显 / 目录批 / 监控推送
}
```
