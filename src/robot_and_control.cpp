#include "robot_and_control.h"
#include "key_set.h"       // 手柄摇杆对象在本层注入，工具类不直接引用全局
#include "communication.h" // CMD_ITEM
#include <string.h>        // memcpy：从payload取float参数

// 本车硬件只在本文件声明，Chassis、RobotAction 等工具类不绑定端口。
//
// X 布局全向轮底盘：四个轮组各带一个轮，每组两台电机齿轮串连（组内物理等速）。
// 组序与 Chassis 混控矩阵一一对应：0=左前 1=右前 2=左后 3=右后，错了整车方向就错。
//
// 端口是沿用差速底盘 8 台电机的分配重排而来（左前 6/7、左后 8/9、右前 17/20、右后 18/19），
// 上车必须按实际接线核对。
// 转向标志（第三个参数）全部置 false 是"未标定"状态：X 布局下每个轮的滚动方向
// 独立决定混控符号，必须上车用点动法逐个核对——给单轮正电压，看它把车推向哪，
// 不对就在这里翻 reverse。
static vex::motor lf_chassis_1(vex::PORT6, vex::ratio6_1, false);
static vex::motor lf_chassis_2(vex::PORT7, vex::ratio6_1, false);
static vex::motor lb_chassis_1(vex::PORT8, vex::ratio6_1, false);
static vex::motor lb_chassis_2(vex::PORT9, vex::ratio6_1, false);
static vex::motor rf_chassis_1(vex::PORT17, vex::ratio6_1, false);
static vex::motor rf_chassis_2(vex::PORT20, vex::ratio6_1, false);
static vex::motor rb_chassis_1(vex::PORT18, vex::ratio6_1, false);
static vex::motor rb_chassis_2(vex::PORT19, vex::ratio6_1, false);

// 后四个为速度环增益（秒量纲，误差单位 pct）：kf 阻力前馈(输出/pct)、kp(输出/pct)、
// ki(输出/(pct·秒))、kd(输出·秒/pct)。
// 当前为接通链路用的占位值，四组相同，必须在实车上重新标定后再用于比赛。

// 逐台电机的个体属性（构造种子，组内会拷走一份）：volt_factor 让同一指令下各轮轮速
// 一致，volt_min 对齐各轮开始转动的最低电压，volt_max 为该电机可达最高电压（V）。
// 原数值是旧车的实测结果，对本车无意义，已重置为默认（factor 全 1、max 12V、min 0.5V），
// 上车后必须逐台重新标定再用于比赛。组内速度环始终以 [0] 号电机（与轮轴直连）的速度为反馈。
// 注意：volt_factor 是"静态个体差异"，全向底盘另有一层 dyn_factor（在线同步）由 Chassis 每拍下发。
static const MyMotor lf_motor_list[2] = {
    {&lf_chassis_1, 1.0f, 12.0f, 0.5f, 0.0f},
    {&lf_chassis_2, 1.0f, 12.0f, 0.5f, 0.0f},
};
static const MyMotor rf_motor_list[2] = {
    {&rf_chassis_1, 1.0f, 12.0f, 0.5f, 0.0f},
    {&rf_chassis_2, 1.0f, 12.0f, 0.5f, 0.0f},
};
static const MyMotor lb_motor_list[2] = {
    {&lb_chassis_1, 1.0f, 12.0f, 0.5f, 0.0f},
    {&lb_chassis_2, 1.0f, 12.0f, 0.5f, 0.0f},
};
static const MyMotor rb_motor_list[2] = {
    {&rb_chassis_1, 1.0f, 12.0f, 0.5f, 0.0f},
    {&rb_chassis_2, 1.0f, 12.0f, 0.5f, 0.0f},
};

// 速度环默认增益（连续摩擦补偿+斜率限制架构下标定）。
// 注意构造签名是 (motor_list, count, kf, kp, ki, kd)——前两个增益是 kf 在前、kp 在后，别写反：
// kf=0.006 前馈按开环 V(tgt)≈1.15+0.065·pct 反推斜率（100pct→7.65V→归一输出≈0.57）；
//         写反成 0.01 的话，100pct 时前馈单独就把归一输出顶满 12V，闭环没有向下调节余量。
// kp=0.01 使 1pct 误差产生约 0.1V 电压扰动（写反成 0.006 则修正力少 40%）。
// ki=0.08/s 是实测安全上限（0.2 在 tgt10 已积分超调截停）；
// kd=0 不用（SDK 底层已对编码器速度滤过一次，微分只放大噪声）。
// 注：全向轮横移时滚子摩擦明显大于直行，四组的 static/dynamic_deadzone 要按方向重新标定。
MyMotorGroup lf_motors(lf_motor_list, 2, 0.006f, 0.01f, 0.08f, 0.0f);
MyMotorGroup rf_motors(rf_motor_list, 2, 0.006f, 0.01f, 0.08f, 0.0f);
MyMotorGroup lb_motors(lb_motor_list, 2, 0.006f, 0.01f, 0.08f, 0.0f);
MyMotorGroup rb_motors(rb_motor_list, 2, 0.006f, 0.01f, 0.08f, 0.0f);

// 定位轮端口与转向取自旧车工程：PORT1 为前向轮（旧工程 RotationY），
// PORT2 为侧向轮（旧工程 RotationX），均反转。首次试车需核对滚动方向：
// 前进时前向轮读数应增大、右转时侧向轮读数应增大，不符在此翻转 reverse。
static vex::rotation forward_tracking_sensor(vex::PORT1, true);
static vex::rotation left_tracking_sensor(vex::PORT2, true);
// 陀螺仪端口取自旧车工程；底盘里程计按“右转为正”计算，试车核对，不符则改 turnType。
static vex::inertial inertial_sensor(vex::PORT11, vex::turnType::right);

// 轮组传入顺序 = 混控矩阵顺序：左前、右前、左后、右后。
Chassis chassis(lf_motors, rf_motors, lb_motors, rb_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 整机动作实例：main 与自动流程通过它发布任务并周期推进。
// 摇杆三通道：左杆 Y=前后、左杆 X=左右平移、右杆 X=转向。
RobotAction robot_action(chassis, &left_axis.now_y, &left_axis.now_x, &right_axis.now_x);

// ===== USB上位机动作命令（0x80~0x86）的薄封装 =====
// 这些函数在通信rx线程里被直接调用：只做一次memcpy取参+发起动作（动作本体
// 只是置目标/接力指针），不阻塞。运动类命令在已有动作未结束时直接忽略，
// 避免新目标覆盖正在执行的动作；停止不设守卫，任何时候都能停。

// 命令里按"轮组编号"索引电机用的表：0=左前 1=右前 2=左后 3=右后，与混控矩阵顺序一致。
static MyMotorGroup *const wheel_groups[4] = {&lf_motors, &rf_motors, &lb_motors, &rb_motors};

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
    // 给单台电机直加电压，绕过速度环（编号 0~1=左前，2~3=右前，4~5=左后，6~7=右后，越界忽略）。
    // 停车态下速度环挂起(Stopped)，spin 指令会保持；逐台连续下发 8 次即可同时试 8 台。
    // 这也是核对每个轮转向标志的手段：给正电压，看它把车推向哪个方向。
    if (robot_action.is_busy())
        return;
    uint8_t id;
    float volts;
    memcpy(&id, p, sizeof(id));
    memcpy(&volts, p + sizeof(id), sizeof(volts));
    if (id < 8)
        wheel_groups[id >> 1]->motors[id & 1].motor->spin(vex::directionType::fwd, volts, vex::voltageUnits::volt);
}

static void cmd_set_spin(const uint8_t *p)
{
    // 模拟手柄：直接给底盘三通道目标速度(pct，±100)，只写目标，速度环负责追踪并保持，
    // 可像摇杆一样连续高频下发；有动作在跑时拒绝，避免与位置环输出互相覆盖。
    if (robot_action.is_busy())
        return;
    float arg[3];
    memcpy(arg, p, sizeof(arg));
    chassis.output(arg[0], arg[1], arg[2]);
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
static const VALUE_TYPE cmd_3f_type[] = {type_float, type_float, type_float};
static const VALUE_TYPE cmd_ui8_f_type[] = {type_uint8_t, type_float};

// 本车的动作命令总表：非static供外部链接，LCD_menu.cpp构造comm时整张传入，
// 与tunable/monitor两张MENU表的注入方式保持一致。
// tag末位由cmd_tag_make拼出：bit6~4参数个数（上限7）；停车命令特化位=1。
// 带取值范围的命令：ranges填非空（每字段一对{min,max}，顺序同字段表）即可，
// 目录发送时自动按它置位tag bit7并追加min/max，tag里不用手填范围位。
//
// 关键：min/max 的类型与宽度必须和它那个字段的 VALUE_TYPE 一致（uint8_t 字段的范围
// 就得是 uint8_t，不能塞 float）。字段同型时直接给同型数组；字段异型（编号 uint8_t +
// 电压 float）要用 packed 结构体——协议里这段是按 value_size 逐字节紧排的，不加 packed
// 编译器会在 uint8_t 与 float 之间插 2B 对齐空洞，字节流就整个错位了。
static const float range_move[] = {-5.0f, 5.0f};                            // 直行：距离
static const float range_goto[] = {-3.6f, 3.6f, -3.6f, 3.6f, -360.0f, 360.0f}; // 局部移动：x、y、航向
static const float range_spin[] = {-100.0f, 100.0f, -100.0f, 100.0f, -100.0f, 100.0f}; // 前、侧、转
struct __attribute__((packed)) RANGE_VOLT
{
    uint8_t id_min, id_max;   // 电机编号，uint8_t
    float volt_min, volt_max; // 电压，float
};
static const RANGE_VOLT range_volt = {0, 7, 0.0f, 12.0f}; // 设置电机电压：编号、电压
const CMD_ITEM robot_cmds[] = {
    {cmd_stop, nullptr, nullptr, "停止运动", cmd_tag_make(0, CMD_SPEC_STOP), nullptr},
    {cmd_turn, cmd_f_type, (const char *[]){"角度(度)"}, "原地转向", cmd_tag_make(1, CMD_SPEC_NORMAL), nullptr},
    {cmd_move, cmd_f_type, (const char *[]){"距离(米)"}, "直行", cmd_tag_make(1, CMD_SPEC_NORMAL), (const void *)range_move},
    {cmd_goto, cmd_3f_type, (const char *[]){"x前(米)", "y右(米)", "航向(度)"}, "局部移动", cmd_tag_make(3, CMD_SPEC_NORMAL), (const void *)range_goto},
    {cmd_set_volt, cmd_ui8_f_type, (const char *[]){"编号(0-1左前,2-3右前,4-5左后,6-7右后)", "电压(伏)"}, "设置电机电压", cmd_tag_make(2, CMD_SPEC_NORMAL), (const void *)&range_volt},
    {cmd_set_spin, cmd_3f_type, (const char *[]){"前向目标速度", "侧向目标速度", "转向目标速度"}, "设置底盘目标速度", cmd_tag_make(3, CMD_SPEC_NORMAL), (const void *)range_spin},
    {cmd_reset_pos, cmd_3f_type, (const char *[]){"坐标x", "坐标y", "航向yaw"}, "重置底盘位置", cmd_tag_make(3, CMD_SPEC_NORMAL), nullptr},
};
const uint8_t robot_cmd_count = sizeof(robot_cmds) / sizeof(robot_cmds[0]);
