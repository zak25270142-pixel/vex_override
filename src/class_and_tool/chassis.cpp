#include "chassis.h"
#include "timer.h"

Chassis::Chassis(MyMotorGroup &left,
                 MyMotorGroup &right,
                 vex::rotation &forward_tracking,
                 vex::rotation &left_tracking,
                 vex::inertial &inertial)
    : left_motors(left),
      right_motors(right),
      forward_tracking_sensor(forward_tracking),
      left_tracking_sensor(left_tracking),
      inertial_sensor(inertial)
{
}

void Chassis::init()
{
    // 初始化电机刹车模式为刹车
    left_motors.setStopping(vex::brakeType::brake);
    right_motors.setStopping(vex::brakeType::brake);
    // 停止电机运动
    stop();
    // 惯性传感器校准期间车体必须保持静止，否则零偏会不准确。
    inertial_sensor.calibrate();

    // 电机编码器清零，在有定位轮后可舍弃
    // left_motors.resetPosition();
    // right_motors.resetPosition();

    // 初始化定位轮连的旋转传感器
    forward_tracking_sensor.resetPosition();
    left_tracking_sensor.resetPosition();
    // 是否运动标识位
    left_is_moving = false;
    right_is_moving = false;
}

// 是否校准完成(init的等待环)
bool Chassis::is_ready()
{
    return !inertial_sensor.isCalibrating();
}

// 初始化后半段(init的结尾)
void Chassis::finish_init()
{
    inertial_sensor.resetRotation();
    uint32_t now = get_time_ms();
    previous_time = now;
    reset(now);
}

// 更新里程、速度和航向
void Chassis::update(uint32_t now)
{
    update_gap = static_cast<uint16_t>(now - previous_time);
    previous_time = now;

    // 在有定位轮后可舍弃 编码器里程相关计算
    // // 电机内置编码器测得的是电机轴转数。经过外部齿轮组后：
    // //  车轮转数 = 电机转数 / 电机每车轮一圈所需转数,当前值为1故后续式子中没写(motor_wheel_ratio)
    // //  行驶距离 = 车轮转数 * 2*pi*车轮半径
    // float left_wheel_turns = static_cast<float>(left_motors.position());
    // float right_wheel_turns = static_cast<float>(right_motors.position());
    // float wheel_c = 2.0f * math_pi * wheel_r; // 车轮周长，单位 m。

    // // float new_left_distance = left_motor_turns / motor_wheel_ratio * wheel_c;
    // // float new_right_distance = right_motor_turns / motor_wheel_ratio * wheel_c;

    // float new_left_distance = left_wheel_turns * wheel_c;
    // float new_right_distance = right_wheel_turns * wheel_c;

    // left_distance_change = new_left_distance - left_distance;
    // right_distance_change = new_right_distance - right_distance;

    // 读取惯性传感器角速度，用于pid控制
    angular_speed = static_cast<float>(inertial_sensor.gyroRate(vex::axisType::zaxis, vex::velocityUnits::dps));

    // 读取惯性传感器航向
    float new_heading = static_cast<float>(inertial_sensor.rotation(vex::rotationUnits::deg));
    heading_change = new_heading - heading;
    // 转换为弧度
    float rotation_change = heading_change * deg_to_rad; // 单位 rad

    // 定位轮从此开始
    // 里程计传感器输入接口：当前为定位轮
    // 直接读取两个定位轮的累计转数，乘定位轮周长换算为累计路程。
    // 读数正负取决于传感器实际安装方向；若试车发现前进/右移读数为负，
    float tracking_wheel_c = 2.0f * math_pi * wheel_r; // 定位轮周长，单位 m。
    float new_forward_tracking_distance =              // 前向定位轮累计转数 * 定位轮周长 = 前向定位轮累计路程
        static_cast<float>(forward_tracking_sensor.position(vex::rotationUnits::rev)) * tracking_wheel_c;
    float new_side_tracking_distance = // 侧向定位轮累计转数 * 定位轮周长 = 侧向定位轮累计路程
        static_cast<float>(left_tracking_sensor.position(vex::rotationUnits::rev)) * tracking_wheel_c;

    // 定位轮不在车体旋转中心时，原地转向也会带动定位轮滚动。
    // 根据定位轮到中心的有符号偏移量，需要减去这部分由旋转造成的假平移。
    // 刚体上点(前向 a, 右向 b)在右转 Δθ 时：前向附加速度为 -b·ω、侧向附加速度为 +a·ω，
    // 故前向读数加回 b·Δθ（forward_tracking_offset 即 b），
    // 侧向读数减去 a·Δθ（side_tracking_offset 即 a，偏后为负值）。
    // 最终得到的本轮前向、侧向位移(local)为：
    // 本轮前向位移 = 前向定位轮累计路程 - 前向定位轮初始路程 + 前向定位轮到中心的有符号偏移量 * 旋转角度
    // 本轮侧向位移 = 侧向定位轮累计路程 - 侧向定位轮初始路程 - 侧向定位轮到中心的有符号偏移量 * 旋转角度
    local_forward_change = new_forward_tracking_distance - forward_tracking_distance +
                           forward_tracking_offset * rotation_change;
    local_side_change = new_side_tracking_distance - side_tracking_distance -
                        side_tracking_offset * rotation_change;

    // 本轮行驶过程中车头也可能在转动，所以用本轮开始、结束角度的中间值，即一次中点法;
    // 圆弧逼近法在转角大时更准确，但计算复杂度有所提高。
    // 把车体坐标中的“向前、向右”位移旋转到全局 x、y 坐标。
    float middle_heading = (heading + heading_change * 0.5f) * deg_to_rad; // 单位 rad
    float cos_h = cosf(middle_heading);
    float sin_h = sinf(middle_heading);
    x_change = local_forward_change * cos_h - local_side_change * sin_h;
    y_change = local_forward_change * sin_h + local_side_change * cos_h;
    x += x_change;
    y += y_change;

    // 编码器里程回写已废弃（恢复点：打滑检测）：
    // left_distance = new_left_distance;
    // right_distance = new_right_distance;

    // 定位轮里程与惯性传感器航向回写，用于下一轮
    forward_tracking_distance = new_forward_tracking_distance;
    side_tracking_distance = new_side_tracking_distance;
    heading = new_heading;
    // 定位轮可以宣告到此结束

    // 里程计速度使用本轮前向位移除以实际更新时间。
    // 两次调用落在同一毫秒时 update_gap 可能为 0，此时不能进行除法。
    if (update_gap > 0)
        linear_speed = local_forward_change * 1000.0f / update_gap;
    // else 时保持不变(gap=0,无新值)
    // linear_speed 主要用于是否停车的判断

    // 计算当前坐标自 reset() 以来的local1下的位移
    float x_from_reset = x - x_start;
    float y_from_reset = y - y_start;
    distance_from_reset = x_from_reset * cos_i + y_from_reset * sin_i;
    side_distance_from_reset = -x_from_reset * sin_i + y_from_reset * cos_i;
    heading_from_reset = heading - heading_start;

    // 记录当前速度，用于判断电机是否处于转动状态
    left_speed = static_cast<float>(left_motors.velocity());
    right_speed = static_cast<float>(right_motors.velocity());
    float left_absolute_speed = fabsf(left_speed);
    float right_absolute_speed = fabsf(right_speed);

    // 通过电机编码器speed判断采用的阻力补偿
    // 静止状态必须达到较高阈值才确认启动；运动状态必须降到较低阈值才确认停止。避免编码器波动导致两种补偿反复切换。
    if (left_is_moving)
    {
        if (left_absolute_speed <= stopped_confirm_speed)
            left_is_moving = false;
    }
    else if (left_absolute_speed >= moving_confirm_speed)
        left_is_moving = true;
    if (right_is_moving)
    {
        if (right_absolute_speed <= stopped_confirm_speed)
            right_is_moving = false;
    }
    else if (right_absolute_speed >= moving_confirm_speed)
        right_is_moving = true;
}

// 开启新的一段动作：以当前状态为段起点快照，清零段内相对量与变化量缓存。
// 前置条件：本周期已调用过 update()，快照数据才是新鲜的；周期内请勿再调 update。
void Chassis::begin_segment()
{
    heading_start = heading;
    x_start = x;
    y_start = y;

    // 起始航向在本段动作内不变，正余弦只在此处计算一次。
    float initial_direction = heading_start * deg_to_rad; // 单位 rad
    cos_i = cosf(initial_direction);
    sin_i = sinf(initial_direction);

    distance_from_reset = 0.0f;
    side_distance_from_reset = 0.0f;
    heading_from_reset = 0.0f;

    update_gap = 0;
    local_forward_change = 0.0f;
    local_side_change = 0.0f;
    heading_change = 0.0f;
    x_change = 0.0f;
    y_change = 0.0f;

    // // 编码器里程已废弃
    // left_distance_change = 0.0f;
    // right_distance_change = 0.0f;
}

// 周期外独立使用的组合入口（如 finish_init）：自己先读一次最新状态再开新段。
// 周期内（refresh 已 update）不要用本函数，避免同周期二次 update；改用 begin_segment()。
void Chassis::reset(uint32_t now)
{
    update(now);
    begin_segment();
}

void Chassis::output(float left_output, float right_output)
{
    // 等比例限幅保留差速关系。例如 200、100 会缩小为 100、50。
    float largest_output = fmaxf(fabsf(left_output), fabsf(right_output));
    if (largest_output > 100.0f)
    {
        float scale = 100.0f / largest_output;
        left_output *= scale;
        right_output *= scale;
    }

    float left_deadzone = left_is_moving ? left_dynamic_deadzone : left_static_deadzone;
    float right_deadzone = right_is_moving ? right_dynamic_deadzone : right_static_deadzone;

    // 绝对值不超过 output_deadzone 的微小 PID 输出直接归零，避免被摩擦补偿突然放大。
    // 超出零输出区后，再将有效输出从 deadzone～100 线性映射到电机命令。
    float left_extra_output = left_output * (100.0f - left_deadzone) / 100.0f;
    if (left_output > output_deadzone)
        left_output = left_deadzone + left_extra_output;
    else if (left_output < -output_deadzone)
        left_output = -left_deadzone + left_extra_output;
    else
        left_output = 0.0f;

    float right_extra_output = right_output * (100.0f - right_deadzone) / 100.0f;
    if (right_output > output_deadzone)
        right_output = right_deadzone + right_extra_output;
    else if (right_output < -output_deadzone)
        right_output = -right_deadzone + right_extra_output;
    else
        right_output = 0.0f;

    // 用电机组输出给电机
    left_motors.spin(vex::directionType::fwd, left_output, vex::velocityUnits::pct);
    right_motors.spin(vex::directionType::fwd, right_output, vex::velocityUnits::pct);
}

void Chassis::stop()
{
    left_motors.stop();
    right_motors.stop();
}
