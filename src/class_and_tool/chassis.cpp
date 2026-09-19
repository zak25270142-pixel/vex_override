#include "chassis.h"
#include "timer.h"

static const float math_pi = 3.14159f;

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
    left_motors.setStopping(vex::brakeType::brake);
    right_motors.setStopping(vex::brakeType::brake);
    stop();
    // 惯性传感器校准期间车体必须保持静止，否则零偏会不准确。
    inertial_sensor.calibrate();

    // 电机编码器不需要校准，可以在等待其他传感器时先清零。
    left_motors.resetPosition();
    right_motors.resetPosition();
    forward_tracking_sensor.resetPosition();
    left_tracking_sensor.resetPosition();
    left_is_moving = false;
    right_is_moving = false;
}

bool Chassis::is_ready()
{
    return !inertial_sensor.isCalibrating();
}

void Chassis::finish_init()
{
    inertial_sensor.resetRotation();
    previous_time = get_time_ms();
    reset();
}

void Chassis::update()
{
    uint32_t current_time = get_time_ms();
    update_gap = static_cast<uint16_t>(current_time - previous_time);
    previous_time = current_time;

    // 电机内置编码器测得的是电机轴转数。经过外部齿轮组后：
    //  车轮转数 = 电机转数 / 电机每车轮一圈所需转数,当前值为1故后续式子中没写(motor_wheel_ratio)
    //  行驶距离 = 车轮转数 * 2*pi*车轮半径
    float left_wheel_turns = static_cast<float>(left_motors.position());
    float right_wheel_turns = static_cast<float>(right_motors.position());
    float wheel_c = 2.0f * math_pi * wheel_r; // 车轮周长，单位 m。

    // float new_left_distance = left_motor_turns / motor_wheel_ratio * wheel_c;
    // float new_right_distance = right_motor_turns / motor_wheel_ratio * wheel_c;

    float new_left_distance = left_wheel_turns * wheel_c;
    float new_right_distance = right_wheel_turns * wheel_c;

    left_distance_change = new_left_distance - left_distance;
    right_distance_change = new_right_distance - right_distance;

    // API 返回 double；读取后转换为 float。
    left_speed = static_cast<float>(left_motors.velocity());
    right_speed = static_cast<float>(right_motors.velocity());

    float new_heading = static_cast<float>(
        inertial_sensor.rotation(vex::rotationUnits::deg));
    angular_speed = static_cast<float>(
        inertial_sensor.gyroRate(
            vex::axisType::zaxis,
            vex::velocityUnits::dps));

    heading_change = new_heading - heading;
    float rotation_change = heading_change * math_pi / 180.0f; // 单位 rad

    // 里程计传感器输入接口：
    // 当前没有独立定位轮，所以用左右驱动轮平均距离临时代替前向定位轮，
    // 并令侧向定位轮读数为 0。以后安装定位轮，只替换下面两个新读数的来源；
    // 后面的旋转补偿、坐标变换和累计位置不需要修改。
    float new_forward_tracking_distance =
        (new_left_distance + new_right_distance) * 0.5f;
    float new_side_tracking_distance = 0.0f;

    float raw_forward_change =
        new_forward_tracking_distance - forward_tracking_distance;
    float raw_side_change =
        new_side_tracking_distance - side_tracking_distance;

    // 定位轮不在车体旋转中心时，原地转向也会带动定位轮滚动。
    // 根据定位轮到中心的有符号偏移量，先减去这部分由旋转造成的假平移。
    // 当前两个偏移量都是 0，因此暂时不会改变驱动轮估算结果。
    local_forward_change =
        raw_forward_change + forward_tracking_offset * rotation_change;
    local_side_change =
        raw_side_change - side_tracking_offset * rotation_change;

    // 本轮行驶过程中车头也可能在转动，所以用本轮开始、结束角度的中间值，
    // 把车体坐标中的“向前、向右”位移旋转到全局 x、y 坐标。
    float middle_heading =
        (heading + heading_change * 0.5f) * math_pi / 180.0f; // 单位 rad
    x_change =
        local_forward_change * cosf(middle_heading) -
        local_side_change * sinf(middle_heading);
    y_change =
        local_forward_change * sinf(middle_heading) +
        local_side_change * cosf(middle_heading);

    x += x_change;
    y += y_change;

    left_distance = new_left_distance;
    right_distance = new_right_distance;
    forward_tracking_distance = new_forward_tracking_distance;
    side_tracking_distance = new_side_tracking_distance;
    heading = new_heading;

    // 里程计速度使用本轮前向位移除以实际更新时间。
    // 两次调用落在同一毫秒时 update_gap 可能为 0，此时不能进行除法。
    if (update_gap > 0)
        linear_speed =
            local_forward_change * 1000.0f / update_gap;
    else
        linear_speed = 0.0f;

    // 当前坐标相对 reset() 保存起点，在起点朝向上的有符号投影。
    // 前进为正、后退为负，比反复累加每轮小量更适合距离 PID。
    float x_from_initial = x - x_start;
    float y_from_initial = y - y_start;
    float initial_direction = heading_start * math_pi / 180.0f; // 单位 rad
    distance_from_initial =
        x_from_initial * cosf(initial_direction) +
        y_from_initial * sinf(initial_direction);
    side_distance_from_initial =
        -x_from_initial * sinf(initial_direction) +
        y_from_initial * cosf(initial_direction);
    heading_from_initial = heading - heading_start;

    float left_absolute_speed = fabsf(left_speed);
    float right_absolute_speed = fabsf(right_speed);

    // 两个阈值形成迟滞：
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

void Chassis::reset()
{
    // reset() 自己先读取一次最新状态，调用者不需要在它前面额外 update()。
    // x、y 是全局连续里程计，不能在每次动作开始时清零。
    update();

    left_distance_start = left_distance;
    right_distance_start = right_distance;
    heading_start = heading;
    x_start = x;
    y_start = y;

    distance_from_initial = 0.0f;
    side_distance_from_initial = 0.0f;
    heading_from_initial = 0.0f;

    update_gap = 0;
    left_distance_change = 0.0f;
    right_distance_change = 0.0f;
    local_forward_change = 0.0f;
    local_side_change = 0.0f;
    heading_change = 0.0f;
    x_change = 0.0f;
    y_change = 0.0f;
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

    float left_deadzone = left_is_moving
                              ? left_dynamic_deadzone
                              : left_static_deadzone;
    float right_deadzone = right_is_moving
                               ? right_dynamic_deadzone
                               : right_static_deadzone;

    // 绝对值不超过 output_deadzone 的微小 PID 输出直接归零，避免被摩擦补偿突然放大。
    // 超出零输出区后，再将有效输出从 deadzone～100 线性映射到电机命令。
    if (left_output > output_deadzone)
        left_output = left_deadzone +
                      left_output * (100.0f - left_deadzone) / 100.0f;
    else if (left_output < -output_deadzone)
        left_output = -left_deadzone +
                      left_output * (100.0f - left_deadzone) / 100.0f;
    else
        left_output = 0.0f;

    if (right_output > output_deadzone)
        right_output = right_deadzone +
                       right_output * (100.0f - right_deadzone) / 100.0f;
    else if (right_output < -output_deadzone)
        right_output = -right_deadzone +
                       right_output * (100.0f - right_deadzone) / 100.0f;
    else
        right_output = 0.0f;

    left_motors.spin(
        vex::directionType::fwd, left_output, vex::velocityUnits::pct);
    right_motors.spin(
        vex::directionType::fwd, right_output, vex::velocityUnits::pct);
}

void Chassis::stop()
{
    left_motors.stop();
    right_motors.stop();
}
