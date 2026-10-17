#include "robot_and_control.h"
#include "key_set.h"       // 手柄摇杆对象在本层注入，工具类不直接引用全局
#include "communication.h" // CMD_ITEM
#include <string.h>        // memcpy：从payload取float参数

// 本车硬件只在本文件声明，Chassis、RobotAction 等工具类不绑定端口。
// 端口与转向取自旧车工程（TEXT）：同侧四台电机在车架上对置安装，故转向标志两两相反。
// 各台电机的齿轮/轮轴安装位置旧工程无记录，上车接线后必须核对。
static vex::motor left_chassis_1(vex::PORT6, vex::ratio6_1, true);
static vex::motor left_chassis_2(vex::PORT7, vex::ratio6_1, true);
static vex::motor left_chassis_3(vex::PORT8, vex::ratio6_1, false);
static vex::motor left_chassis_4(vex::PORT9, vex::ratio6_1, false);
static vex::motor right_chassis_1(vex::PORT17, vex::ratio6_1, false);
static vex::motor right_chassis_2(vex::PORT20, vex::ratio6_1, false);
static vex::motor right_chassis_3(vex::PORT18, vex::ratio6_1, true);
static vex::motor right_chassis_4(vex::PORT19, vex::ratio6_1, true);

// 后三个为速度环增益（秒量纲，误差单位 pct）：kp(输出/pct)、ki(输出/(pct·秒))、
// kd(输出·秒/pct)；前馈 kf 是插值表，见下方 kf_table 定义，不走标量入参。

// kf 阻力前馈插值表（左右各一份）：node 为 |target| 节点(pct，升序)，k 为各节点斜率
// (归一输出/pct)。当前是接通链路用的占位值——6 个节点全填 0.01，等价于改造前的常数 kf。
// 实车标定（手动填表在此处完成，改完重新烧录；调参目录与上位机脚本都不含 kf，它不是在
// 线调参拧出来的量）：开环逐档扫电压得 V(v)，反解该转速下稳态所需归一输出
// out(v)=(V(v)-sign·(friction(v)+volt_min))/((volt_max-friction(v)-volt_min)·volt_factor)
// （volt 参数取 motors[0]，与速度反馈同台），再取 k[i]=out(v_i)/v_i。
// 口径：v 是电机 SDK pct 转速，node 是相对本侧 max_speed 归一后的目标速度
// （target = v/max_speed×100，output() 已把外部指令缩到这个口径），换算别搞混。
// out(v) 被 1.0 顶住（阻力小、前馈先饱和）时该式自动给出 ff 平台，无需特殊节点；
// 曲线只要求随 |target| 单调不减。用监控项"左前馈/右前馈"逐档核对。详见 chassis.h 的 KfTable。
static const KfTable left_kf_table = {
    {0.0f, 10.0f, 25.0f, 50.0f, 75.0f, 100.0f}, // node：低速段密（摩擦过渡在这段）
    {0.01f, 0.01f, 0.01f, 0.01f, 0.01f, 0.01f}, // k：占位，实车逐节点标定
    6,
};
static const KfTable right_kf_table = {
    {0.0f, 10.0f, 25.0f, 50.0f, 75.0f, 100.0f},
    {0.01f, 0.01f, 0.01f, 0.01f, 0.01f, 0.01f},
    6,
};

// 逐台电机的个体属性（构造种子，组内会拷走一份）：volt_factor 让同一指令下各轮轮速
// 一致，volt_min 对齐各轮开始转动的最低电压，volt_max 为该电机可达最高电压（V）。
// 原数值是旧车的实测结果，对本车无意义，已重置为默认（factor 全 1、max 12V、min 0.5V），
// 上车后必须逐台重新标定再用于比赛。组内速度环始终以 [0] 号电机（与轮轴直连）的速度为反馈。
static const MyMotor left_motor_list[4] = {
    {&left_chassis_1, 1.0f, 12.0f, 0.5f, 0.0f},
    {&left_chassis_2, 1.0f, 12.0f, 0.5f, 0.0f},
    {&left_chassis_3, 1.0f, 12.0f, 0.5f, 0.0f},
    {&left_chassis_4, 1.0f, 12.0f, 0.5f, 0.0f},
};
static const MyMotor right_motor_list[4] = {
    {&right_chassis_1, 1.0f, 12.0f, 0.5f, 0.0f},
    {&right_chassis_2, 1.0f, 12.0f, 0.5f, 0.0f},
    {&right_chassis_3, 1.0f, 12.0f, 0.5f, 0.0f},
    {&right_chassis_4, 1.0f, 12.0f, 0.5f, 0.0f},
};

MyMotorGroup left_motors(
    left_motor_list, 4,
    left_kf_table, 0.006f, 0.08f, 0.0f);
MyMotorGroup right_motors(
    right_motor_list, 4,
    right_kf_table, 0.006f, 0.08f, 0.0f);

// 定位轮端口与转向取自旧车工程：PORT1 为前向轮（旧工程 RotationY），
// PORT2 为侧向轮（旧工程 RotationX），均反转。首次试车需核对滚动方向：
// 前进时前向轮读数应增大、右转时侧向轮读数应增大，不符在此翻转 reverse。
static vex::rotation forward_tracking_sensor(vex::PORT1, true);
static vex::rotation left_tracking_sensor(vex::PORT2, true);
// 陀螺仪端口取自旧车工程；底盘里程计按“右转为正”计算，试车核对，不符则改 turnType。
static vex::inertial inertial_sensor(vex::PORT11, vex::turnType::right);

Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 整机动作实例：main 与自动流程通过它发布任务并周期推进。
RobotAction robot_action(chassis, &left_axis.now_y, &left_axis.now_x);

// ===== USB上位机动作命令（0x80~0x86）的薄封装 =====
// 这些函数在通信rx线程里被直接调用：只做一次memcpy取参+发起动作（动作本体
// 只是置目标/接力指针），不阻塞。运动类命令在已有动作未结束时直接忽略，
// 避免新目标覆盖正在执行的动作；停止不设守卫，任何时候都能停。

static void cmd_stop(const uint8_t *p)
{
    (void)p; // 无参
    robot_action.stop_move();
}

static void cmd_turn(const uint8_t *p)
{
    if (robot_action.is_busy())
        return;
    float angle;
    memcpy(&angle, p, sizeof(angle));
    robot_action.turn(angle);
}

static void cmd_move(const uint8_t *p)
{
    if (robot_action.is_busy())
        return;
    float distance;
    memcpy(&distance, p, sizeof(distance));
    robot_action.move(distance);
}

static void cmd_goto(const uint8_t *p)
{
    if (robot_action.is_busy())
        return;
    float arg[3]; // x前(米)、y右(米)、heading最终朝向(度)，紧凑小端排列
    memcpy(arg, p, sizeof(arg));
    robot_action.goto_local(arg[0], arg[1], arg[2]);
}

// ===== 调试/遥控命令（0x84~0x86）：不经动作层，直接操控底盘 =====
// 与运动类命令同样只用 is_busy() 拦截，不主动停车：调用前提是底盘无任务、已处于停车状态。
// 其中 set_spin 是给上位机/AI 模拟手柄连续下达速度环指令用的，可高频反复下发。

static void cmd_set_volt(const uint8_t *p)
{
    // 给单台电机直加电压，绕过速度环（编号 0~3=左1~4，4~7=右1~4，越界忽略）。
    // 停车态下速度环挂起(is_stoped)，spin 指令会保持；逐台连续下发 8 次即可同时试 8 台。
    if (robot_action.is_busy())
        return;
    uint8_t id;
    float volts;
    memcpy(&id, p, sizeof(id));
    memcpy(&volts, p + sizeof(id), sizeof(volts));
    if (id < 4)
        left_motors.motors[id].motor->spin(vex::directionType::fwd, volts, vex::voltageUnits::volt);
    else if (id < 8)
        right_motors.motors[id - 4].motor->spin(vex::directionType::fwd, volts, vex::voltageUnits::volt);
}

static void cmd_set_spin(const uint8_t *p)
{
    // 模拟手柄：直接给左右轮目标速度(pct，±100)，只写目标，5ms 常驻速度环负责追踪并保持，
    // 可像摇杆一样连续高频下发；有动作在跑时拒绝，避免与位置环输出互相覆盖。
    if (robot_action.is_busy())
        return;
    float arg[2];
    memcpy(arg, p, sizeof(arg));
    chassis.output(arg[0], arg[1]);
}

static void cmd_reset_pos(const uint8_t *p)
{
    // 直接改软件里程计全局位姿：x/y(米)、全局 yaw(度)，并以其为新段起点。
    // heading_offset 负责把 IMU 原始读数映射到全局航向，不碰 IMU 硬件零位。
    // 车必须处于停车空闲态（is_busy 拦截），否则里程计在运动中突变会失配。
    if (robot_action.is_busy())
        return;
    float arg[3];
    memcpy(arg, p, sizeof(arg));
    chassis.x = arg[0];
    chassis.y = arg[1];
    chassis.heading_offset = arg[2] - chassis.imu_heading;
    chassis.heading = arg[2]; // 同步供 begin_segment() 快照，下一拍 update() 写同一值也无害
    chassis.begin_segment();
}

// 字段类型与中文名表：上位机据此渲染下发框；顺序必须和handler里memcpy的顺序一致
static const VALUE_TYPE cmd_f_type[] = {type_float};
static const VALUE_TYPE cmd_2f_type[] = {type_float, type_float};
static const VALUE_TYPE cmd_3f_type[] = {type_float, type_float, type_float};
static const VALUE_TYPE cmd_ui8_f_type[] = {type_uint8_t, type_float};

static const struct __attribute__((packed))
{
    uint8_t id_min, id_max;
    float v_min, v_max;
} range_set_volt = {0, 7, 0.0f, 12.0f};

// 本车的动作命令总表：非static供外部链接，LCD_menu.cpp构造comm时整张传入，
// 与tunable/monitor两张MENU表的注入方式保持一致。
// tag末位由cmd_tag_make拼出：bit6~4参数个数（上限7）；停车命令特化位=1。
// 带取值范围的命令：ranges填非空（每字段一对{min,max}，顺序同字段表）即可，
// 目录发送时自动按它置位tag bit7并追加min/max，tag里不用手填范围位。
const CMD_ITEM robot_cmds[] = {
    {cmd_stop, nullptr, nullptr, "停止运动", cmd_tag_make(0, CMD_SPEC_STOP), nullptr},
    {cmd_turn, cmd_f_type, (const char *[]){"角度(度)"}, "原地转向", cmd_tag_make(1, CMD_SPEC_NORMAL), nullptr},
    {cmd_move, cmd_f_type, (const char *[]){"距离(米)"}, "直行", cmd_tag_make(1, CMD_SPEC_NORMAL), (const void *)&(const float[2]){-5.0f, 5.0f}},
    {cmd_goto, cmd_3f_type, (const char *[]){"x前(米)", "y右(米)", "航向(度)"}, "局部移动", cmd_tag_make(3, CMD_SPEC_NORMAL), (const void *)&(const float[6]){-3.6f, 3.6f, -3.6f, 3.6f, -360.0f, 360.0f}},
    {cmd_set_volt, cmd_ui8_f_type, (const char *[]){"编号(0-3左1-4,4-7右1-4)", "电压(伏)"}, "设置电机电压", cmd_tag_make(2, CMD_SPEC_NORMAL), &range_set_volt},
    {cmd_set_spin, cmd_2f_type, (const char *[]){"左目标速度", "右目标速度"}, "设置底盘目标速度", cmd_tag_make(2, CMD_SPEC_NORMAL), (const void *)&(const float[4]){-100.0f, 100.0f, -100.0f, 100.0f}},
    {cmd_reset_pos, cmd_3f_type, (const char *[]){"坐标x", "坐标y", "航向yaw"}, "重置底盘位置", cmd_tag_make(3, CMD_SPEC_NORMAL), nullptr},
};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
