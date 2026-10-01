#include "robot_and_control.h"
#include "key_set.h"       // 手柄摇杆对象在本层注入，工具类不直接引用全局
#include "communication.h" // CMD_ITEM
#include <string.h>        // memcpy：从payload取float参数

// 本车硬件只在本文件声明，Chassis、RobotAction 等工具类不绑定端口。
static vex::motor left_chassis_1(vex::PORT3, vex::ratio6_1, false);   // 左前下,连60齿 (空转 重点标记)
static vex::motor left_chassis_2(vex::PORT4, vex::ratio6_1, true);    // 左后下,连60齿 (单电机10v 很慢)
static vex::motor left_chassis_3(vex::PORT1, vex::ratio6_1, true);    // 左前上,连48齿(需要更快转速/更大电压) (单电机10v,较快)
static vex::motor left_chassis_4(vex::PORT2, vex::ratio6_1, false);   // 左后上,连48齿(需要更快转速/更大电压) (单电机10v,快)
static vex::motor right_chassis_1(vex::PORT10, vex::ratio6_1, true);  // 右前下,连60齿(空转 重点标记)
static vex::motor right_chassis_2(vex::PORT11, vex::ratio6_1, false); // 右后下,连60齿(空转 重点标记)
static vex::motor right_chassis_3(vex::PORT8, vex::ratio6_1, false);  // 右前上,连48齿(需要更快转速/更大电压) (单电机10v,快)
static vex::motor right_chassis_4(vex::PORT9, vex::ratio6_1, true);   // 右后上,连48齿(需要更快转速/更大电压)(空转 重点标记)

// 参数顺序：直连轮轴的 motor1/2（60 齿）在前，经齿轮传动的 motor3/4（48 齿）在后。
// 实车接线未完成，当前按电机构造时 reversed 标志推测直连/减速归属，接线后必须核对。
// 后四个为速度环增益（秒量纲，误差单位 pct）：kf 阻力前馈(输出/pct)、kp(输出/pct)、
// ki(输出/(pct·秒))、kd(输出·秒/pct)。
// 当前为接通链路用的占位值，两组相同，必须在实车上重新标定后再用于比赛。
MyMotorGroup left_motors(
    left_chassis_1, left_chassis_2, left_chassis_3, left_chassis_4,
    0.008f, 0.02f, 0.5f, 0.0f);
MyMotorGroup right_motors(
    right_chassis_1, right_chassis_2, right_chassis_3, right_chassis_4,
    0.008f, 0.02f, 0.5f, 0.0f);

// 两个定位轮均已接入底盘里程计；reverse 标志若与实际滚动方向不符，试车时在此翻转。
static vex::rotation forward_tracking_sensor(vex::PORT6, true);         // 当前已经前进++
static vex::rotation left_tracking_sensor(vex::PORT7, true);            // 当前已经右转++
static vex::inertial inertial_sensor(vex::PORT5, vex::turnType::right); // 陀螺仪 当前已经右转++

Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 整机动作实例：main 与自动流程通过它发布任务并周期推进。
// 手柄摇杆在此绑定，手动控制由 RobotAction 经这两个指针操作。
RobotAction robot_action(chassis, &left_axis, &right_axis);

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
    // 直接改软件里程计全局位姿：x/y(米)、yaw(度)，并以其为新段起点。
    // 只改内存里的累计量(begin_segment 无传感器 I/O)，定位轮/IMU 硬件零位不动。
    if (robot_action.is_busy())
        return;
    float arg[3];
    memcpy(arg, p, sizeof(arg));
    chassis.x = arg[0];
    chassis.y = arg[1];
    chassis.heading = arg[2];
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
    {cmd_stop, 0x80, nullptr, nullptr, 0, "停止运动"},
    {cmd_turn, 0x81, cmd_f_type, (const char *[]){"角度(度)"}, 1, "原地转向"},
    {cmd_move, 0x82, cmd_f_type, (const char *[]){"距离(米)"}, 1, "直行"},
    {cmd_goto, 0x83, cmd_3f_type, (const char *[]){"x前(米)", "y右(米)", "航向(度)"}, 3, "局部移动"},
    {cmd_set_volt, 0x84, cmd_ui8_f_type, (const char *[]){"编号(0-3左1-4,4-7右1-4)", "电压(伏)"}, 2, "设置电机电压"},
    {cmd_set_spin, 0x85, cmd_2f_type, (const char *[]){"左目标速度", "右目标速度"}, 2, "设置底盘目标速度"},
    {cmd_reset_pos, 0x86, cmd_3f_type, (const char *[]){"坐标x", "坐标y", "航向yaw"}, 3, "重置底盘位置"},
};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
