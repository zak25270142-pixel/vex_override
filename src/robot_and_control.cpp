#include "robot_and_control.h"
#include "key_set.h"       // 手柄摇杆对象在本层注入，工具类不直接引用全局
#include "communication.h" // CMD_ITEM

// PC 下发过任务（底盘 turn/move/goto、升降杆 lift_to）即置位，不清零：
// 掉线 failsafe 据此把两者一起收掉。开环命令不建任务、不置标记（见头文件）。
bool pc_took_control = false;

// 新车（feature/small_car）端口表：左右各 3 台
static vex::motor left_chassis_1(vex::PORT1, vex::ratio6_1, true);
static vex::motor left_chassis_2(vex::PORT2, vex::ratio6_1, false);
static vex::motor left_chassis_3(vex::PORT3, vex::ratio6_1, true);
static vex::motor right_chassis_1(vex::PORT5, vex::ratio6_1, false);
static vex::motor right_chassis_2(vex::PORT6, vex::ratio6_1, true);
static vex::motor right_chassis_3(vex::PORT7, vex::ratio6_1, false);

static const Vec2f left_kf_table[] = {
    {-100.0f, -11.1324f},
    {-75.0f, -8.2356f},
    {-50.0f, -5.2836f},
    {-25.0f, -2.4288f},
    {-10.0f, -0.8280f},
    {0.0f, 0.0f},
    {10.0f, 0.8280f},
    {25.0f, 2.4288f},
    {50.0f, 5.2836f},
    {75.0f, 8.2356f},
    {100.0f, 11.1324f},
};
static const Vec2f right_kf_table[] = {
    {-100.0f, -10.9128f},
    {-75.0f, -7.9800f},
    {-50.0f, -5.2056f},
    {-25.0f, -2.3856f},
    {-10.0f, -0.7128f},
    {0.0f, 0.0f},
    {10.0f, 0.7128f},
    {25.0f, 2.3856f},
    {50.0f, 5.2056f},
    {75.0f, 7.9800f},
    {100.0f, 10.9128f},
};
// 逐台电机的个体属性：factor/max/min
MyMotor left_motor_list[3] = {
    {&left_chassis_1, 0.9330f, 0.597f},
    {&left_chassis_2, 1.0000f, 0.406f},
    {&left_chassis_3, 0.9240f, 0.491f},
};
MyMotor right_motor_list[3] = {
    {&right_chassis_1, 1.0000f, 0.542f},
    {&right_chassis_2, 0.9720f, 0.436f},
    {&right_chassis_3, 0.9950f, 0.516f},
};

ChassisMotorGroup left_motors(
    left_motor_list, 3,
    left_kf_table, static_cast<uint8_t>(sizeof(left_kf_table) / sizeof(left_kf_table[0])),
    0.0f, 0.0f, 0.0f);
ChassisMotorGroup right_motors(
    right_motor_list, 3,
    right_kf_table, static_cast<uint8_t>(sizeof(right_kf_table) / sizeof(right_kf_table[0])),
    0.0f, 0.0f, 0.0f);

// 定位轮端口与转向取
static vex::rotation forward_tracking_sensor(vex::PORT4, false);
static vex::rotation left_tracking_sensor(vex::PORT8, false);
// 陀螺仪；里程计按右转为正计算
static vex::inertial inertial_sensor(vex::PORT11, vex::turnType::right);

// 左右两组机械上非耦合（靠地面/车体协调）。两侧的横向协调（指令缩回、
// 电压限幅）在 Chassis 内部做——它是唯一同时看得到两侧的地方。
Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 红电机 36:1（100rpm）：编码器 1800 raw/rev，位置单位一律 raw
static vex::motor lift_motor_1(vex::PORT17, vex::ratio36_1, true);
static vex::motor lift_motor_2(vex::PORT18, vex::ratio36_1, false);
static vex::motor lift_motor_3(vex::PORT19, vex::ratio36_1, false);
static vex::motor lift_motor_4(vex::PORT20, vex::ratio36_1, true);

// 前馈表先不配（kf_at / bias_at 支持"没表 = 恒 0"）。实测后按下面口径补两张表：
//   速度前馈：x = 带符号目标速度 pct，y = 稳态前馈 V（表要覆盖负半轴）
//   重力前馈：x = 位置 raw，y = 该高度悬停所需补充电压 V（四连杆力矩臂随角度变，按高度插值）

// 逐台属性：factor/volt_min 待扫压标定（cmd 开环电压就是用来测这两个的）
MyMotor lift_motor_list[4] = {
    {&lift_motor_1, 1.0f, 0.5f},
    {&lift_motor_2, 1.0f, 0.5f},
    {&lift_motor_3, 1.0f, 0.5f},
    {&lift_motor_4, 1.0f, 0.5f},
};

// 速度环 kp/ki/kd 是 V 量纲（V/pct、V/(pct·s)、V·s/pct），初值待整定；
// 位置环 kp/ki/kd 是 raw → pct，同样待整定。均可由调参表/上位机改。
LiftBar lift_bar(lift_motor_list, 4,
                           nullptr, 0, // 速度前馈表（不配 = 恒 0）
                           nullptr, 0, // 重力前馈表（不配 = 恒 0）
                           0.05f, 0.02f, 0.0f,  // 速度环：V 量纲
                           0.02f, 0.0f, 0.0f);  // 位置环：误差 raw → 输出 pct

// 整机动作实例：底盘与升降杆均构造时注入；主循环与命令表只认 robot_action
RobotAction robot_action(chassis, lift_bar, &left_axis.now_y, &left_axis.now_x);

void robot_init()
{
    left_motors.static_deadzone = 1.01f;
    left_motors.dynamic_deadzone = 0.8f;
    right_motors.static_deadzone = 1.04f;
    right_motors.dynamic_deadzone = 0.8f;
    // 升降杆摩擦垫底：重力大、摩擦小，启动电压待扫压实测（cmd 升降杆电压测出来再填）
    lift_bar.static_deadzone = 0.8f;
    lift_bar.dynamic_deadzone = 0.5f;
    // lift_bar.init() 不在这：随 RobotAction::init() 走动作层生命周期
}
// ---- 上位机命令 ----
// handler 返回值：CMD_RET_OK=已处理 / CMD_RET_BUSY=忙时拒绝 / CMD_RET_REJECT=参数越界。
// 非 OK 由 handle_command 回对应的 Response 拒绝帧——拒绝不再静默。

static uint8_t cmd_stop(const uint8_t *p)
{
    (void)p;                       // 无参：只停底盘
    robot_action.stop_move(50000); // 底盘缓停：0V 减速 → 近零 / 到时硬刹
    // 掉线 failsafe 复用的也是 stop_move()（my_main.cpp comm_link_lost_handler）。
    // 升降杆的停止是另一条命令：它掉链时的失效模式是就地保持（没 stop 就不该断
    // 电压），不跟着底盘收。想停一切就连发两条。
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

// 开环直控的组表：下标即命令里的"组别"。**只放底盘左右两组**。
// 升降杆不进这张表，也不共用命令字——它有自己的"升降杆电压"命令（见 robot_cmds；
// 四连杆的电压是有结构含义的（左右半刚性、要防剪切），跟底盘"左右对称跑"不是一回事，
// 混进同一个组别列表只会让上位机那边多一层"这个组是哪种机构"的判断。
// 所以这里的组别范围 0~1 是终态，不是占位。
static MotorGroup *const raw_groups[] = {&left_motors, &right_motors};
static const uint8_t raw_group_count = sizeof(raw_groups) / sizeof(raw_groups[0]);

static uint8_t cmd_group_volt(const uint8_t *p)
{
    // 按组开环给电压：绕过速度环，逐台直通。用于扫压测启动电压/阻力/转向。
    // 本组停着时，写下的电压会一直保持：速度环在 Stopped 下每拍什么都不下发，
    // 不会拿 0V 覆盖。直到收到"给目标"或"停止"，本组才重新归速度环管。
    if (robot_action.is_busy())
        return CMD_RET_BUSY;
    uint8_t group;
    float volt[4]; // 固定 4 槽，够任何组用；该组台数不足时多传的忽略
    memcpy(&group, p, sizeof(group));
    memcpy(volt, p + sizeof(group), sizeof(volt));
    if (group >= raw_group_count)
        return CMD_RET_REJECT; // 组别越界

    // 前提是本组的速度环不在工作。任务型的冲突（位置环、缓停、手柄）已经被上面的
    // is_busy() 挡掉了；剩下唯一能进来的矛盾是"上位机先下过目标速度、再下开环电压"——
    // 两条命令都是上位机自己发的，谁后发谁生效、被速度环覆盖也由上位机担责，
    // 固件不替它决定该停哪一个（这里越权去 stop() 反而会静默清掉它的速度目标）。
    raw_groups[group]->set_volt(volt);
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
// 闭环命令直接给电机组；开环电压走"最后门"也直接打电机组（后门，不进状态机）。
// 开环电压优先级最低：只有在 Stoped（已回落到底、断电）下写才留得住，其余态
// 下一拍就被位置环 + 速度环覆盖。谁后发谁生效，操作者担责，固件不替他决定。
static uint8_t cmd_lift_to(const uint8_t *p)
{
    float raw; // 目标位置（编码器 raw；ratio36_1 下 1 rev = 1800 raw）
    memcpy(&raw, p, sizeof(raw));
    pc_took_control = true;   // 与底盘共用：掉线时两者一起收
    lift_bar.lift_to(raw);    // 单机构命令直接给电机组，不经 RobotAction 转发
    return CMD_RET_OK;
}

// 开环直控走"最后门"：直接调电机组的 set_volt()——它本就是绕过
// 一切控制的直接执行（同底盘 cmd_group_volt 直接打 MotorGroup::set_volt）。
// 只有在 Stoped 下写才留得住（Stoped 态 lift_tick 不下发），其余态下一拍被闭环覆盖。
static uint8_t cmd_lift_volt(const uint8_t *p)
{
    float volt[4]; // 逐台电压（V），直通下发；顺序同 lift_motor_list（升0~3）
    memcpy(volt, p, sizeof(volt));
    lift_bar.set_volt(volt);
    return CMD_RET_OK;
}

// 上位机构停止：升降杆受控回落，到底才断电。与底盘停止各走各的命令，
// 停车条上并排两个按钮，想停一切就连发两条。
static uint8_t cmd_stop_upper(const uint8_t *p)
{
    (void)p;          // 无参
    lift_bar.stop();  // 受控回落，到底才断电；pc_took_control 是历史标记，不清零
    return CMD_RET_OK;
}

// 字段类型表：顺序必须与 handler 里 memcpy 的顺序一致
static const VALUE_TYPE cmd_f_type[] = {type_float};
static const VALUE_TYPE cmd_2f_type[] = {type_float, type_float};
static const VALUE_TYPE cmd_3f_type[] = {type_float, type_float, type_float};
static const VALUE_TYPE cmd_4f_type[] = {type_float, type_float, type_float, type_float};
static const VALUE_TYPE cmd_ui8_4f_type[] = {type_uint8_t, type_float, type_float, type_float, type_float};

// 组别 + 4 个电压槽。范围按字段顺序紧凑排（uint8 两字节 + 4 组 float 的 min/max），
// packed 不能省：不压紧时 uint8 之后会被补到 4 字节对齐，三端字节流就对不上。
static const struct __attribute__((packed))
{
    uint8_t group_min, group_max;
    // 必须 min/max 交错：协议约定的字节流是 {min1,max1,min2,max2,...}，
    // 写成两组数组会变成 {min1..min4,max1..max4}，上位机读到的每个范围都是(min,min)。
    float v0_min, v0_max, v1_min, v1_max, v2_min, v2_max, v3_min, v3_max;
} range_group_volt = {0, 1, -12.0f, 12.0f, -12.0f, 12.0f, -12.0f, 12.0f, -12.0f, 12.0f};

// 上面那个结构体是三端共享的字节流，长度算错一格整个目录帧就歪了
static_assert(sizeof(range_group_volt) == 2 + 4 * 2 * sizeof(float), "范围字节流长度不对");

// 本车动作命令总表，非 static 供外部链接
const CMD_ITEM robot_cmds[] = {
    {cmd_stop, nullptr, nullptr, "停止底盘", cmd_tag_make(0, CMD_SPEC_STOP), nullptr},
    {cmd_turn, cmd_f_type, (const char *[]){"角度(度)"}, "原地转向", cmd_tag_make(1, CMD_SPEC_NORMAL), nullptr},
    {cmd_move, cmd_f_type, (const char *[]){"距离(米)"}, "直行", cmd_tag_make(1, CMD_SPEC_NORMAL), (const void *)&(const float[2]){-5.0f, 5.0f}},
    {cmd_goto, cmd_3f_type, (const char *[]){"x前(米)", "y右(米)", "航向(度)"}, "局部移动", cmd_tag_make(3, CMD_SPEC_NORMAL), (const void *)&(const float[6]){-3.6f, 3.6f, -3.6f, 3.6f, -360.0f, 360.0f}},
    {cmd_group_volt, cmd_ui8_4f_type, (const char *[]){"组别(0左1右)", "0号电压", "1号电压", "2号电压", "3号电压"}, "组开环电压", cmd_tag_make(5, CMD_SPEC_NORMAL), &range_group_volt},
    {cmd_set_spin, cmd_2f_type, (const char *[]){"左目标速度", "右目标速度"}, "设置底盘目标速度", cmd_tag_make(2, CMD_SPEC_NORMAL), (const void *)&(const float[4]){-100.0f, 100.0f, -100.0f, 100.0f}},
    {cmd_reset_pos, cmd_3f_type, (const char *[]){"坐标x", "坐标y", "航向yaw"}, "重置底盘位置", cmd_tag_make(3, CMD_SPEC_NORMAL), nullptr},

    {cmd_lift_to, cmd_f_type, (const char *[]){"目标位置(raw)"}, "升降杆位置", cmd_tag_make(1, CMD_SPEC_NORMAL), (const void *)&(const float[2]){0.0f, 5400.0f}},
    {cmd_lift_volt, cmd_4f_type, (const char *[]){"升0电压", "升1电压", "升2电压", "升3电压"}, "升降杆电压", cmd_tag_make(4, CMD_SPEC_NORMAL), (const void *)&(const float[8]){-12.0f, 12.0f, -12.0f, 12.0f, -12.0f, 12.0f, -12.0f, 12.0f}},
    {cmd_stop_upper, nullptr, nullptr, "停止升降杆", cmd_tag_make(0, CMD_SPEC_STOP), nullptr},

};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
