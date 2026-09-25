#include "robot_and_control.h"
#include "key_set.h" // 手柄摇杆对象在本层注入，工具类不直接引用全局

// 本车硬件只在本文件声明，Chassis、RobotAction 等工具类不绑定端口。
static vex::motor left_chassis_1(vex::PORT6, vex::ratio6_1, false);
static vex::motor left_chassis_2(vex::PORT7, vex::ratio6_1, false);
static vex::motor left_chassis_3(vex::PORT8, vex::ratio6_1, true);
static vex::motor left_chassis_4(vex::PORT9, vex::ratio6_1, true);
static vex::motor right_chassis_1(vex::PORT17, vex::ratio6_1, true);
static vex::motor right_chassis_2(vex::PORT18, vex::ratio6_1, false);
static vex::motor right_chassis_3(vex::PORT19, vex::ratio6_1, false);
static vex::motor right_chassis_4(vex::PORT20, vex::ratio6_1, true);

// 参数顺序：直连轮轴的 motor1/2（60 齿）在前，经齿轮传动的 motor3/4（48 齿）在后。
// 实车接线未完成，当前按电机构造时 reversed 标志推测直连/减速归属，接线后必须核对。
static MyMotorGroup left_motors(
    left_chassis_1, left_chassis_2, left_chassis_3, left_chassis_4);
static MyMotorGroup right_motors(
    right_chassis_2, right_chassis_3, right_chassis_1, right_chassis_4);

// 两个定位轮均已接入底盘里程计；reverse 标志若与实际滚动方向不符，试车时在此翻转。
static vex::rotation forward_tracking_sensor(vex::PORT1, false);
static vex::rotation left_tracking_sensor(vex::PORT2, false);

static vex::inertial inertial_sensor(vex::PORT11, vex::turnType::right);

Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 整机动作实例：main 与自动流程通过它发布任务并周期推进。
// 手柄摇杆在此绑定，手动控制由 RobotAction 经这两个指针操作。
RobotAction robot_action(chassis, &left_axis, &right_axis);
