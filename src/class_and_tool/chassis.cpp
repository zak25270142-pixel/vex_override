#include "chassis.h"
#include "timer.h"

Chassis::Chassis(MyMotorGroup &left,
                 MyMotorGroup &right,
                 vex::rotation &forward_tracking,
                 vex::rotation &left_tracking,
                 vex::inertial &inertial)
    : forward_tracking_sensor(forward_tracking),
      left_tracking_sensor(left_tracking),
      inertial_sensor(inertial),
      left_motors(left),
      right_motors(right)
{
}

void Chassis::init()
{
    // 初始化电机刹车模式为刹车
    left_motors.setStopping(vex::brakeType::brake);
    right_motors.setStopping(vex::brakeType::brake);
    stop();                      // 停止电机运动
    inertial_sensor.calibrate(); // 惯性传感器校准
    // 电机编码器清零，在有定位轮后可舍弃
    // left_motors.resetPosition();
    // right_motors.resetPosition();
    forward_tracking_sensor.resetPosition(); // 初始化定位轮连的旋转传感器
    left_tracking_sensor.resetPosition();
}

// 是否校准完成(init的等待环)
bool Chassis::is_ready()
{
    return !inertial_sensor.isCalibrating();
}

// 初始化后半段(init的结尾)
void Chassis::finish_init()
{
    // // IMU 校准后仍有残余零偏（实测约0.014°/s），静止采样1s取平均，
    // // 之后每拍从航向和角速度里扣掉它，采样期间车体必须保持静止。
    // float bias_sum = 0.0f;
    // const int bias_samples = 100;
    // for (int i = 0; i < bias_samples; ++i)
    // {
    //     bias_sum += static_cast<float>(inertial_sensor.gyroRate(vex::axisType::zaxis, vex::velocityUnits::dps));
    //     vex::this_thread::sleep_for(10);
    // }
    // gyro_bias = bias_sum / bias_samples;
    // heading_origin = now;
    inertial_sensor.resetRotation();
    uint32_t now = get_time_us();
    previous_time = now;
    reset(now);
}

// 更新里程、速度和航向
void Chassis::update(uint32_t now)
{
    update_gap = now - previous_time;
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

    // 读取惯性传感器角速度，用于pid控制，扣除零偏使静止时读数在0附近
    angular_speed = static_cast<float>(inertial_sensor.gyroRate(vex::axisType::zaxis, vex::velocityUnits::dps));
    // - gyro_bias;

    // 读取惯性传感器航向，增量只从 IMU 原始读数差分
    float new_imu_heading = static_cast<float>(inertial_sensor.rotation(vex::rotationUnits::deg));
    // - gyro_bias * (now - heading_origin) / 1000000.0f;
    heading_change = new_imu_heading - imu_heading;
    imu_heading = new_imu_heading;
    // 转换为弧度
    float rotation_change = heading_change * deg_to_rad; // 单位 rad

    // 定位轮从此开始
    float tracking_wheel_c = 2.0f * math_pi * wheel_r; // 定位轮周长，单位 m。
    float new_forward_tracking_distance =              // 前向定位轮累计转数 * 定位轮周长 = 前向定位轮累计路程
        static_cast<float>(forward_tracking_sensor.position(vex::rotationUnits::rev)) * tracking_wheel_c;
    float new_side_tracking_distance = // 侧向定位轮累计转数 * 定位轮周长 = 侧向定位轮累计路程
        static_cast<float>(left_tracking_sensor.position(vex::rotationUnits::rev)) * tracking_wheel_c;

    // 定位轮不在车体旋转中心时，原地转向也会带动定位轮滚动，减去这部分旋转造成的假平移
    local_forward_change = new_forward_tracking_distance - forward_tracking_distance +
                           forward_tracking_offset * rotation_change;
    local_side_change = new_side_tracking_distance - side_tracking_distance -
                        side_tracking_offset * rotation_change;

    // 中点法：用本轮开始、结束角度的中值，把车体坐标的位移旋转到全局 x、y
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

    // 定位轮里程回写用于下一轮；全局航向 = IMU 读数 + 软件偏置。
    forward_tracking_distance = new_forward_tracking_distance;
    side_tracking_distance = new_side_tracking_distance;
    heading = new_imu_heading + heading_offset;
    // 定位轮可以宣告到此结束

    // 线速度 = 本轮前向位移 / 实际间隔；gap=0 时保持旧值
    if (update_gap > 0)
        linear_speed = local_forward_change * 1000000.0f / update_gap;
    // else 时保持不变(gap=0,无新值)
    // linear_speed 主要用于是否停车的判断

    // 当前坐标相对 reset() 起点，投影回起始车头方向
    float x_from_reset = x - x_start;
    float y_from_reset = y - y_start;
    distance_from_reset = x_from_reset * cos_i + y_from_reset * sin_i;
    side_distance_from_reset = -x_from_reset * sin_i + y_from_reset * cos_i;
    heading_from_reset = heading - heading_start;
}

// 开启新的一段动作：以当前状态为段起点快照，清零段内相对量与变化量缓存。
void Chassis::begin_segment()
{
    heading_start = heading;
    x_start = x;
    y_start = y;

    // 起始航向正余弦，本段不变，只在此处算一次
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

void Chassis::reset(uint32_t now)
{
    update(now);
    begin_segment();
}

void Chassis::output(float left_output, float right_output)
{
    // 速度环目标入口：任一指令超出本侧 max_speed 时，按超出比例最大的一侧把两侧同步缩回
    float scale = fmaxf(fmaxf(fabsf(left_output) / left_max_speed, fabsf(right_output) / right_max_speed), 1.0f);

    // 缩回后原样下发，摩擦补偿在速度环内部处理
    left_motors.drive(left_output / scale);
    right_motors.drive(right_output / scale);
}

void Chassis::speed_tick()
{
    // 速度环执行点：停车态各组内部直接返回
    left_motors.my_spin();
    right_motors.my_spin();
}

void Chassis::stop()
{
    left_motors.stop();
    right_motors.stop();
}

