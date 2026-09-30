#include "robot_and_control.h"
#include "key_set.h"       // 手柄摇杆对象在本层注入，工具类不直接引用全局
#include "communication.h" // CMD_ITEM
#include <string.h>        // memcpy：从payload取float参数

// 本车硬件只在本文件声明，Chassis、RobotAction 等工具类不绑定端口。
static vex::motor left_chassis_1(vex::PORT3, vex::ratio6_1, false);   // 左前下,连60齿
static vex::motor left_chassis_2(vex::PORT4, vex::ratio6_1, false);   // 左后下,连60齿
static vex::motor left_chassis_3(vex::PORT1, vex::ratio6_1, true);    // 左前上,连48齿(需要更快转速/更大电压)
static vex::motor left_chassis_4(vex::PORT2, vex::ratio6_1, true);    // 左后上,连48齿(需要更快转速/更大电压)
static vex::motor right_chassis_1(vex::PORT10, vex::ratio6_1, true);  // 右前下,连60齿
static vex::motor right_chassis_2(vex::PORT11, vex::ratio6_1, false); // 右后下,连60齿
static vex::motor right_chassis_3(vex::PORT8, vex::ratio6_1, false);  // 右前上,连48齿(需要更快转速/更大电压)
static vex::motor right_chassis_4(vex::PORT9, vex::ratio6_1, true);   // 右后上,连48齿(需要更快转速/更大电压)

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
static vex::rotation forward_tracking_sensor(vex::PORT6, false); // 横竖还没测
static vex::rotation left_tracking_sensor(vex::PORT7, false);    // 横竖还没测

// 陀螺仪端口5
static vex::inertial inertial_sensor(vex::PORT5, vex::turnType::right);

Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 整机动作实例：main 与自动流程通过它发布任务并周期推进。
// 手柄摇杆在此绑定，手动控制由 RobotAction 经这两个指针操作。
RobotAction robot_action(chassis, &left_axis, &right_axis);

// ===== USB上位机动作命令（0x80~0x83）的薄封装 =====
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

// 字段类型与中文名表：上位机据此渲染下发框；顺序必须和handler里memcpy的顺序一致
static const VALUE_TYPE one_float_type[] = {type_float};
static const char *const turn_fields[] = {"角度(度)"};
static const char *const move_fields[] = {"距离(米)"};

static const VALUE_TYPE goto_types[] = {type_float, type_float, type_float};
static const char *const goto_fields[] = {"x前(米)", "y右(米)", "航向(度)"};

// 本车的动作命令总表：非static供外部链接，LCD_menu.cpp构造comm时整张传入，
// 与tunable/monitor两张MENU表的注入方式保持一致。
const CMD_ITEM robot_cmds[] = {
    {cmd_stop, 0x80, nullptr, nullptr, 0, "停止运动"},
    {cmd_turn, 0x81, one_float_type, turn_fields, 1, "原地转向"},
    {cmd_move, 0x82, one_float_type, move_fields, 1, "直行"},
    {cmd_goto, 0x83, goto_types, goto_fields, 3, "局部移动"},
};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
