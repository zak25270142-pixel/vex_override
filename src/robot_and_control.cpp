#include "robot_and_control.h"
#include "key_set.h"       // 手柄摇杆对象在本层注入，工具类不直接引用全局
#include "communication.h" // CMD_ITEM

// 新车（feature/small_car）端口表：左右各 3 台
static vex::motor left_chassis_1(vex::PORT1, vex::ratio6_1, true);
static vex::motor left_chassis_2(vex::PORT2, vex::ratio6_1, false);
static vex::motor left_chassis_3(vex::PORT3, vex::ratio6_1, true);
static vex::motor right_chassis_1(vex::PORT5, vex::ratio6_1, false);
static vex::motor right_chassis_2(vex::PORT6, vex::ratio6_1, true);
static vex::motor right_chassis_3(vex::PORT7, vex::ratio6_1, false);

// kf 阻力前馈插值表
static const Vec2f left_kf_table[] = {
    {0.0f, 0.0f},
    {10.0f, 0.1f},
    {25.0f, 0.25f},
    {50.0f, 0.5f},
    {75.0f, 0.75f},
    {100.0f, 1.0f},
};
static const Vec2f right_kf_table[] = {
    {0.0f, 0.0f},
    {10.0f, 0.1f},
    {25.0f, 0.25f},
    {50.0f, 0.5f},
    {75.0f, 0.75f},
    {100.0f, 1.0f},
};

// 逐台电机的个体属性：factor/max/min
MyMotor left_motor_list[3] = {
    {&left_chassis_1, 1.0f, 12.0f, 0.5f},
    {&left_chassis_2, 1.0f, 12.0f, 0.5f},
    {&left_chassis_3, 1.0f, 12.0f, 0.5f},
};
MyMotor right_motor_list[3] = {
    {&right_chassis_1, 1.0f, 12.0f, 0.5f},
    {&right_chassis_2, 1.0f, 12.0f, 0.5f},
    {&right_chassis_3, 1.0f, 12.0f, 0.5f},
};

MyMotorGroup left_motors(
    left_motor_list, 3,
    left_kf_table, static_cast<uint8_t>(sizeof(left_kf_table) / sizeof(left_kf_table[0])),
    0.006f, 0.08f, 0.0f);
MyMotorGroup right_motors(
    right_motor_list, 3,
    right_kf_table, static_cast<uint8_t>(sizeof(right_kf_table) / sizeof(right_kf_table[0])),
    0.006f, 0.08f, 0.0f);

// 定位轮端口与转向取
static vex::rotation forward_tracking_sensor(vex::PORT4, false);
static vex::rotation left_tracking_sensor(vex::PORT8, false);
// 陀螺仪；里程计按右转为正计算
static vex::inertial inertial_sensor(vex::PORT11, vex::turnType::right);

Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 整机动作实例
RobotAction robot_action(chassis, &left_axis.now_y, &left_axis.now_x);

// ---- 上位机命令 ----

static void cmd_stop(const uint8_t *p)
{
    (void)p;                       // 无参
    robot_action.stop_move(50000); // 缓停：0V 减速→近零/到时硬刹
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

static void cmd_set_volt(const uint8_t *p)
{
    // 给单台电机直加电压，绕过速度环（编号 0~2=左1~3，3~5=右1~3，越界忽略）
    // 直设同步 motors[].volt_output
    if (robot_action.is_busy())
        return;
    uint8_t id;
    float volts;
    memcpy(&id, p, sizeof(id));
    memcpy(&volts, p + sizeof(id), sizeof(volts));
    if (id < 3)
    {
        left_motors.motors[id].volt_output = volts;
        left_motors.motors[id].motor->spin(vex::directionType::fwd, volts, vex::voltageUnits::volt);
    }
    else if (id < 6)
    {
        right_motors.motors[id - 3].volt_output = volts;
        right_motors.motors[id - 3].motor->spin(vex::directionType::fwd, volts, vex::voltageUnits::volt);
    }
}

static void cmd_set_spin(const uint8_t *p)
{
    // 直接给左右轮目标速度(pct，±100)，只写目标，速度环负责追踪并保持。
    if (robot_action.is_busy())
        return;
    float arg[2];
    memcpy(arg, p, sizeof(arg));
    chassis.output(arg[0], arg[1]);
}

static void cmd_reset_pos(const uint8_t *p)
{
    // 改软件里程计全局位姿：x/y(米)、全局 yaw(度)，并以其为新段起点
    if (robot_action.is_busy())
        return;
    float arg[3];
    memcpy(arg, p, sizeof(arg));
    chassis.x = arg[0];
    chassis.y = arg[1];
    chassis.heading_offset = arg[2] - chassis.imu_heading;
    chassis.heading = arg[2]; // 同步供 begin_segment() 快照
    chassis.begin_segment();
}

// 字段类型表：顺序必须与 handler 里 memcpy 的顺序一致
static const VALUE_TYPE cmd_f_type[] = {type_float};
static const VALUE_TYPE cmd_2f_type[] = {type_float, type_float};
static const VALUE_TYPE cmd_3f_type[] = {type_float, type_float, type_float};
static const VALUE_TYPE cmd_ui8_f_type[] = {type_uint8_t, type_float};

static const struct __attribute__((packed))
{
    uint8_t id_min, id_max;
    float v_min, v_max;
} range_set_volt = {0, 5, 0.0f, 12.0f};

// 本车动作命令总表，非 static 供外部链接
const CMD_ITEM robot_cmds[] = {
    {cmd_stop, nullptr, nullptr, "停止运动", cmd_tag_make(0, CMD_SPEC_STOP), nullptr},
    {cmd_turn, cmd_f_type, (const char *[]){"角度(度)"}, "原地转向", cmd_tag_make(1, CMD_SPEC_NORMAL), nullptr},
    {cmd_move, cmd_f_type, (const char *[]){"距离(米)"}, "直行", cmd_tag_make(1, CMD_SPEC_NORMAL), (const void *)&(const float[2]){-5.0f, 5.0f}},
    {cmd_goto, cmd_3f_type, (const char *[]){"x前(米)", "y右(米)", "航向(度)"}, "局部移动", cmd_tag_make(3, CMD_SPEC_NORMAL), (const void *)&(const float[6]){-3.6f, 3.6f, -3.6f, 3.6f, -360.0f, 360.0f}},
    {cmd_set_volt, cmd_ui8_f_type, (const char *[]){"编号(0-2左1-3,3-5右1-3)", "电压(伏)"}, "设置电机电压", cmd_tag_make(2, CMD_SPEC_NORMAL), &range_set_volt},
    {cmd_set_spin, cmd_2f_type, (const char *[]){"左目标速度", "右目标速度"}, "设置底盘目标速度", cmd_tag_make(2, CMD_SPEC_NORMAL), (const void *)&(const float[4]){-100.0f, 100.0f, -100.0f, 100.0f}},
    {cmd_reset_pos, cmd_3f_type, (const char *[]){"坐标x", "坐标y", "航向yaw"}, "重置底盘位置", cmd_tag_make(3, CMD_SPEC_NORMAL), nullptr},
};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
