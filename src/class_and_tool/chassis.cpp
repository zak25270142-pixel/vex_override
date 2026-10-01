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
    left_motors.is_moving = false;
    right_motors.is_moving = false;
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

    // 读取惯性传感器航向，并按距起点的时间扣除零偏累计量（静止时修正航向保持0）
    float new_heading = static_cast<float>(inertial_sensor.rotation(vex::rotationUnits::deg));
    // - gyro_bias * (now - heading_origin) / 1000000.0f;
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
    // 两次调用落在同一微秒时 update_gap 可能为 0，此时不能进行除法。
    if (update_gap > 0)
        linear_speed = local_forward_change * 1000000.0f / update_gap;
    // else 时保持不变(gap=0,无新值)
    // linear_speed 主要用于是否停车的判断

    // 计算当前坐标自 reset() 以来的local1下的位移
    float x_from_reset = x - x_start;
    float y_from_reset = y - y_start;
    distance_from_reset = x_from_reset * cos_i + y_from_reset * sin_i;
    side_distance_from_reset = -x_from_reset * sin_i + y_from_reset * cos_i;
    heading_from_reset = heading - heading_start;

    // left_speed = static_cast<float>(left_motors.velocity());
    // right_speed = static_cast<float>(right_motors.velocity());
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
    // 位置环 PID 与手柄映射都从这里喂入，
    // 真正的闭环计算在 speed_tick() 常驻任务里执行，本函数只写目标。
    // 左右配重不同则可达满速不同：任一指令超出本侧上限时，按超出比例最大的一侧
    // 把两侧同步缩回，保留差速关系（直行时整车被慢侧拖住，但方向不偏）。
    float scale = fmaxf(fmaxf(fabsf(left_output) / left_max_speed, fabsf(right_output) / right_max_speed), 1.0f);

    // 摩擦补偿由速度环内部电压域统一处理，这里不做任何死区映射，缩回后原样下发。
    left_motors.drive(left_output / scale);
    right_motors.drive(right_output / scale);
}

void Chassis::speed_tick()
{
    // 速度环唯一执行点：由 5ms 常驻任务短周期调用。
    // 停车后 is_stoped 为真，my_spin 立即返回；下次 output() 下目标时 drive 自动清积分起步。
    left_motors.my_spin();
    right_motors.my_spin();
}

void Chassis::stop()
{
    left_motors.stop();
    right_motors.stop();
}
