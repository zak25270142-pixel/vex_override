#include "robot_and_control.h"
#include "key_set.h"       // 手柄摇杆对象在本层注入，工具类不直接引用全局
#include "communication.h" // CMD_ITEM

// PC 下发过位置环任务（turn/move/goto）即置位，不清零：掉线 failsafe 在 is_busy 时据此收车。
// 开环命令（set_volt/set_spin）不建任务、is_busy 为假，由基础规则收车，无需置标记。
bool pc_took_control = false;

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
    {10.0f, 0.0690f},
    {25.0f, 0.2024f},
    {50.0f, 0.4403f},
    {75.0f, 0.6863f},
    {100.0f, 0.9277f},
};
static const Vec2f right_kf_table[] = {
    {0.0f, 0.0f},
    {10.0f, 0.0594f},
    {25.0f, 0.1988f},
    {50.0f, 0.4338f},
    {75.0f, 0.6650f},
    {100.0f, 0.9094f},
};

// 逐台电机的个体属性：factor/max/min
MyMotor left_motor_list[3] = {
    {&left_chassis_1, 0.9330f, 12.0f, 0.597f},
    {&left_chassis_2, 1.0000f, 12.0f, 0.406f},
    {&left_chassis_3, 0.9240f, 12.0f, 0.491f},
};
MyMotor right_motor_list[3] = {
    {&right_chassis_1, 1.0000f, 12.0f, 0.542f},
    {&right_chassis_2, 0.9720f, 12.0f, 0.436f},
    {&right_chassis_3, 0.9950f, 12.0f, 0.516f},
};

MyMotorGroup left_motors(
    left_motor_list, 3,
    left_kf_table, static_cast<uint8_t>(sizeof(left_kf_table) / sizeof(left_kf_table[0])),
    0.0f, 0.0f, 0.0f);
MyMotorGroup right_motors(
    right_motor_list, 3,
    right_kf_table, static_cast<uint8_t>(sizeof(right_kf_table) / sizeof(right_kf_table[0])),
    0.0f, 0.0f, 0.0f);

// 定位轮端口与转向取
static vex::rotation forward_tracking_sensor(vex::PORT4, false);
static vex::rotation left_tracking_sensor(vex::PORT8, false);
// 陀螺仪；里程计按右转为正计算
static vex::inertial inertial_sensor(vex::PORT11, vex::turnType::right);

Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 升降杆电机：左 2 台抬左侧、右 2 台抬右侧，中间是箱子（半刚性，见 robot_upper.h
// 决策 3），按一个组控制。
// 绿盒 = 18:1 = 200 RPM（VEX 官方：红 36:1/100rpm、绿 18:1/200rpm、蓝 6:1/600rpm）。
// gearSetting 必须与实物盒型一致——position(rev) 由 ticks 按盒型换算（18:1 = 900
// ticks/rev），填错读数差一个比值；velocity(pct) 的满量程也随盒型变。pos_factor
// 只取决于外部传动（mm/输出轴 rev），换盒不用改它。
// reversed 全 false：约定"上升为正"，实测某台转向不对就改对应 bool——VEXos
// 会同时翻转它的输出方向和编码器读数，驱动和反馈一起翻，无需其他改动。
static vex::motor lift_motor_1(vex::PORT9, vex::ratio18_1, false);
static vex::motor lift_motor_2(vex::PORT10, vex::ratio18_1, false);
static vex::motor lift_motor_3(vex::PORT15, vex::ratio18_1, false);
static vex::motor lift_motor_4(vex::PORT16, vex::ratio18_1, false);

// 保持力矩前馈表（x=位置，y=悬停所需出力）：初版不做——实测确认"摩擦能否克服
// 重力"、以及各位置阻力差异后再决定要不要这张表，见 robot_upper.h 决策 1。

// 逐台属性：factor/vmax/vmin 待实测标定；读数→杆端单位的换算在
// LiftBar::pos_factor（组级），实测导程后改成 mm/rev
MyMotor lift_motor_list[4] = {
    {&lift_motor_1, 1.0f, 12.0f, 0.4f},
    {&lift_motor_2, 1.0f, 12.0f, 0.4f},
    {&lift_motor_3, 1.0f, 12.0f, 0.4f},
    {&lift_motor_4, 1.0f, 12.0f, 0.4f},
};

// 速度环初值待整定（调参表/上位机可改）；初版只有电压直控 + 速度环，
// 位置环等阻力/行程实测后再按级联架构加
LiftBar lift_bar(lift_motor_list, 4, 0.05f, 0.02f);

// 整机动作实例：底盘与升降杆均构造时注入；主循环与命令表只认 robot_action
RobotAction robot_action(chassis, lift_bar, &left_axis.now_y, &left_axis.now_x);

void robot_init()
{
    left_motors.static_deadzone = 1.01f;
    left_motors.dynamic_deadzone = 0.8f;
    right_motors.static_deadzone = 1.04f;
    right_motors.dynamic_deadzone = 0.8f;
    // lift.init() 不在这：随 RobotAction::init() 走动作层生命周期
}
// ---- 上位机命令 ----
// handler 返回值：CMD_RET_OK=已处理 / CMD_RET_BUSY=忙时拒绝 / CMD_RET_REJECT=参数越界。
// 非 OK 由 handle_command 回对应的 Response 拒绝帧——拒绝不再静默。

static uint8_t cmd_stop(const uint8_t *p)
{
    (void)p;                       // 无参：上位机停止 = 停止一切
    robot_action.stop_move(50000); // 底盘缓停：0V 减速→近零/到时硬刹
    // 升降杆一并停（含 Stall 解锁）。刻意不走 stop_move()——那个被掉线
    // failsafe 复用（my_main.cpp comm_link_lost_handler），而升降杆的失效
    // 模式是自行保持 + Stall 兜底，掉链时不接管（见 robot_upper.h 决策 6）。
    robot_action.lift_stop();
    return CMD_RET_OK;
}

static uint8_t cmd_turn(const uint8_t *p)
{
    pc_took_control = true; // PC 位置环任务：掉线 failsafe 在 is_busy 时据此收车
    if (robot_action.is_busy())
        return CMD_RET_BUSY;
    float angle;
    memcpy(&angle, p, sizeof(angle));
    robot_action.turn(angle);
    return CMD_RET_OK;
}

static uint8_t cmd_move(const uint8_t *p)
{
    pc_took_control = true; // PC 位置环任务：掉线 failsafe 在 is_busy 时据此收车
    if (robot_action.is_busy())
        return CMD_RET_BUSY;
    float distance;
    memcpy(&distance, p, sizeof(distance));
    robot_action.move(distance);
    return CMD_RET_OK;
}

static uint8_t cmd_goto(const uint8_t *p)
{
    pc_took_control = true; // PC 位置环任务：掉线 failsafe 在 is_busy 时据此收车
    if (robot_action.is_busy())
        return CMD_RET_BUSY;
    float arg[3]; // x前(米)、y右(米)、heading最终朝向(度)，紧凑小端排列
    memcpy(arg, p, sizeof(arg));
    robot_action.goto_local(arg[0], arg[1], arg[2]);
    return CMD_RET_OK;
}

static uint8_t cmd_set_volt(const uint8_t *p)
{
    // 给单台电机直加电压，绕过速度环（编号 0~2=左1~3，3~5=右1~3）
    // 直设同步 motors[].volt_output
    if (robot_action.is_busy())
        return CMD_RET_BUSY;
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
    else
        return CMD_RET_REJECT; // 编号越界：原来静默忽略，现在诚实回拒绝
    return CMD_RET_OK;
}

static uint8_t cmd_set_spin(const uint8_t *p)
{
    // 直接给左右轮目标速度(pct，±100)，只写目标，速度环负责追踪并保持。
    if (robot_action.is_busy())
        return CMD_RET_BUSY;
    float arg[2];
    memcpy(arg, p, sizeof(arg));
    chassis.output(arg[0], arg[1]);
    return CMD_RET_OK;
}

static uint8_t cmd_reset_pos(const uint8_t *p)
{
    float arg[3];
    memcpy(arg, p, sizeof(arg));
    chassis.x = arg[0];
    chassis.y = arg[1];
    chassis.heading_offset = arg[2] - chassis.imu_heading;
    chassis.heading = arg[2]; // 同步供 begin_segment() 快照
    chassis.begin_segment();
    return CMD_RET_OK;
}

// ---- 上位机命令：升降杆 ----
// 入口统一经 robot_action（动作层），与底盘命令同构。
// 初版只三种：电压直控 / 速度环 / 停止（停止兼作 Stall 解锁）。
static uint8_t cmd_lift_volt(const uint8_t *p)
{
    float volt[4]; // 逐台电压（V），直通下发；顺序同 lift_motor_list（升0~3）
    memcpy(volt, p, sizeof(volt));
    robot_action.lift_volt(volt[0], volt[1], volt[2], volt[3]);
    return CMD_RET_OK;
}

static uint8_t cmd_lift_speed(const uint8_t *p)
{
    float pct; // 目标转速（pct，上升为正）
    memcpy(&pct, p, sizeof(pct));
    robot_action.lift_speed(pct);
    return CMD_RET_OK;
}

// 字段类型表：顺序必须与 handler 里 memcpy 的顺序一致
static const VALUE_TYPE cmd_f_type[] = {type_float};
static const VALUE_TYPE cmd_2f_type[] = {type_float, type_float};
static const VALUE_TYPE cmd_3f_type[] = {type_float, type_float, type_float};
static const VALUE_TYPE cmd_4f_type[] = {type_float, type_float, type_float, type_float};
static const VALUE_TYPE cmd_ui8_f_type[] = {type_uint8_t, type_float};

static const struct __attribute__((packed))
{
    uint8_t id_min, id_max;
    float v_min, v_max;
} range_set_volt = {0, 5, -12.0f, 12.0f};

// 本车动作命令总表，非 static 供外部链接
const CMD_ITEM robot_cmds[] = {
    {cmd_stop, nullptr, nullptr, "停止运动", cmd_tag_make(0, CMD_SPEC_STOP), nullptr},
    {cmd_turn, cmd_f_type, (const char *[]){"角度(度)"}, "原地转向", cmd_tag_make(1, CMD_SPEC_NORMAL), nullptr},
    {cmd_move, cmd_f_type, (const char *[]){"距离(米)"}, "直行", cmd_tag_make(1, CMD_SPEC_NORMAL), (const void *)&(const float[2]){-5.0f, 5.0f}},
    {cmd_goto, cmd_3f_type, (const char *[]){"x前(米)", "y右(米)", "航向(度)"}, "局部移动", cmd_tag_make(3, CMD_SPEC_NORMAL), (const void *)&(const float[6]){-3.6f, 3.6f, -3.6f, 3.6f, -360.0f, 360.0f}},
    {cmd_set_volt, cmd_ui8_f_type, (const char *[]){"编号(0-2左1-3,3-5右1-3)", "电压(伏)"}, "设置电机电压", cmd_tag_make(2, CMD_SPEC_NORMAL), &range_set_volt},
    {cmd_set_spin, cmd_2f_type, (const char *[]){"左目标速度", "右目标速度"}, "设置底盘目标速度", cmd_tag_make(2, CMD_SPEC_NORMAL), (const void *)&(const float[4]){-100.0f, 100.0f, -100.0f, 100.0f}},
    {cmd_reset_pos, cmd_3f_type, (const char *[]){"坐标x", "坐标y", "航向yaw"}, "重置底盘位置", cmd_tag_make(3, CMD_SPEC_NORMAL), nullptr},

    // 升降杆：初版开环两态。停止并入了 cmd_stop（上位机停止 = 停止一切，前端只有一个停止按钮）；
    // 行程端点未实测，limit_min/limit_max 只记录不参与控制
    {cmd_lift_volt, cmd_4f_type, (const char *[]){"升0电压", "升1电压", "升2电压", "升3电压"}, "升降杆电压", cmd_tag_make(4, CMD_SPEC_NORMAL), (const void *)&(const float[8]){-12.0f, 12.0f, -12.0f, 12.0f, -12.0f, 12.0f, -12.0f, 12.0f}},
    {cmd_lift_speed, cmd_f_type, (const char *[]){"目标速度"}, "升降杆速度", cmd_tag_make(1, CMD_SPEC_NORMAL), (const void *)&(const float[2]){-100.0f, 100.0f}},
};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
