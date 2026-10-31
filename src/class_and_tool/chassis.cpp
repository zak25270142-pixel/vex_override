#include "chassis.h"

Chassis::Chassis(ChassisMotorGroup &left, ChassisMotorGroup &right,
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
    stop();
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
    reset(0); // 初始化没有"上一拍"，dt 给 0：update_gap=0 → linear_speed 保持旧值
}

// 更新里程、速度和航向
void Chassis::update(uint32_t dt_us)
{
    update_gap = dt_us;

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

void Chassis::reset(uint32_t dt_us)
{
    update(dt_us);
    begin_segment();
}

void Chassis::output(float left_output, float right_output)
{
    spd_scale = 1.0f;
    spd_scale = fmaxf(spd_scale, fabsf(left_output) / left_motors.max_speed);
    spd_scale = fmaxf(spd_scale, fabsf(right_output) / right_motors.max_speed);
    left_motors.set_target(left_output / spd_scale);
    right_motors.set_target(right_output / spd_scale);
}

void Chassis::speed_tick(uint32_t dt_us, float dt_s)
{
    // 底盘同启同停：唤醒在 output()（两侧一起醒，含 0 目标侧）、休眠在 stop()（两侧一起睡），
    // 两组 is_stopped 恒等，查一侧即可。睡着时整拍不下发——0V 也是一条指令，
    // 会覆盖开环直写（扫压）与 stop() 留下的刹车；"停"的语义由 stop() 那一次
    // motor->stop()（按 setStopping 的刹车）表达，不需要每秒重复声明 100 次。
    // 睡着时也刷一次数据：now_v 等派生量只在 refresh 里更新，不刷则开环扫压看不到转速。
    // 调基类那份——派生类的 refresh 带状态机与积分，停着跑会偷偷累积误差积分。
    if (left_motors.spin_state == ChassisMotorGroup::SpinState::Stopped)
    {
        left_motors.MotorGroup::refresh(dt_s);
        right_motors.MotorGroup::refresh(dt_s);
        return;
    }

    // ① 算本拍请求的可调电压。两组都收到同一个 dt：左右就不会各自夹出一个来。
    left_motors.refresh(dt_us, dt_s);
    right_motors.refresh(dt_us, dt_s);

    // ② 比例限幅。
    //    【未决】分母用的是"请求值"，而请求可以远超余量：kp 标起来之后 P 项能到
    //    几十伏（error 大 × kp，I 反倒被抗饱和卡在 ±full_volt 的投影上）。超过余量的那部分
    //    物理上做不到，但两侧的比例仍是控制器的意图，所以这里按意图等比缩、不夹请求
    //    （往 output_raw 上夹 ±12 会让比例先失真）。待实车确认：若出现"单侧顶住 →
    //    整车被拽偏"，说明虚构的 P 不该有话语权，那时再加夹幅。见 memory 2026-10-25。
    // 缩放分母 full_volt ± volt_floor 恒为正：volt_floor 量级（偏置 + 垫底 + 最小电压）
    // 远小于 12；真要 ≥12 等价于满电压车也动不了，正常调参到不了，无需除零守卫。
    volt_scale = 1.0f;
    // 缩放只缩可调段 output_raw，承诺地板 volt_floor 不缩放；
    // 按"投影电机电压 volt_floor + output_raw 落回 ±full_volt"反推每侧所需缩放。
    {
        const float a = left_motors.output_raw;
        const float k = a > 0.0f ? a / (full_volt - left_motors.volt_floor)
                                 : (a < 0.0f ? (-a) / (full_volt + left_motors.volt_floor) : 0.0f);
        volt_scale = fmaxf(volt_scale, k);
    }
    {
        const float a = right_motors.output_raw;
        const float k = a > 0.0f ? a / (full_volt - right_motors.volt_floor)
                                 : (a < 0.0f ? (-a) / (full_volt + right_motors.volt_floor) : 0.0f);
        volt_scale = fmaxf(volt_scale, k);
    }

    // ③ 下发：把限幅后的值直接当参数传下去。不回写 output_raw——它永远是
    //    "请求值"，所以监控里看到的是要多少，不是给了多少。
    left_motors.apply_output(left_motors.output_raw / volt_scale);
    right_motors.apply_output(right_motors.output_raw / volt_scale);
}

void Chassis::stop()
{
    left_motors.stop();
    right_motors.stop();
}
