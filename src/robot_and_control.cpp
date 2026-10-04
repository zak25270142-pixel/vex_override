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

// 后四个为速度环增益（秒量纲，误差单位 pct）：kf 阻力前馈(输出/pct)、kp(输出/pct)、
// ki(输出/(pct·秒))、kd(输出·秒/pct)。
// 当前为接通链路用的占位值，两组相同，必须在实车上重新标定后再用于比赛。

// volt_factor / volt_min 是逐台电机的空载标定值：前者让同一指令下各轮轮速一致，
// 后者对齐各轮开始转动的最低电压。原数值是旧车的实测结果，对本车无意义，已重置为
// 默认（factor 全 1、min 全 0.5V），上车后必须逐台重新标定再用于比赛。
static const float left_volt_factor[4] = {1.0f, 1.0f, 1.0f, 1.0f};
static const float right_volt_factor[4] = {1.0f, 1.0f, 1.0f, 1.0f};
static const float left_volt_min[4] = {0.5f, 0.5f, 0.5f, 0.5f};
static const float right_volt_min[4] = {0.5f, 0.5f, 0.5f, 0.5f};
MyMotorGroup left_motors(
    left_chassis_1, left_chassis_2, left_chassis_3, left_chassis_4,
    0.008f, 0.02f, 0.5f, 0.0f, left_volt_factor, left_volt_min);
MyMotorGroup right_motors(
    right_chassis_1, right_chassis_2, right_chassis_3, right_chassis_4,
    0.008f, 0.02f, 0.5f, 0.0f, right_volt_factor, right_volt_min);

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
RobotAction robot_action(chassis, &left_axis.now_x, &left_axis.now_y);

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
        left_motors.motors[id]->spin(vex::directionType::fwd, volts, vex::voltageUnits::volt);
    else if (id < 8)
        right_motors.motors[id - 4]->spin(vex::directionType::fwd, volts, vex::voltageUnits::volt);
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

// 本车的动作命令总表：非static供外部链接，LCD_menu.cpp构造comm时整张传入，
// 与tunable/monitor两张MENU表的注入方式保持一致。
const CMD_ITEM robot_cmds[] = {
    {cmd_stop, nullptr, nullptr, 0, "停止运动"},
    {cmd_turn, cmd_f_type, (const char *[]){"角度(度)"}, 1, "原地转向"},
    {cmd_move, cmd_f_type, (const char *[]){"距离(米)"}, 1, "直行"},
    {cmd_goto, cmd_3f_type, (const char *[]){"x前(米)", "y右(米)", "航向(度)"}, 3, "局部移动"},
    {cmd_set_volt, cmd_ui8_f_type, (const char *[]){"编号(0-3左1-4,4-7右1-4)", "电压(伏)"}, 2, "设置电机电压"},
    {cmd_set_spin, cmd_2f_type, (const char *[]){"左目标速度", "右目标速度"}, 2, "设置底盘目标速度"},
    {cmd_reset_pos, cmd_3f_type, (const char *[]){"坐标x", "坐标y", "航向yaw"}, 3, "重置底盘位置"},
};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
