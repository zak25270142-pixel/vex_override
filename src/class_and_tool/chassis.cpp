#include "chassis.h"
#include "timer.h"

Chassis::Chassis(MyMotorGroup &left_front,
                 MyMotorGroup &right_front,
                 MyMotorGroup &left_back,
                 MyMotorGroup &right_back,
                 vex::rotation &forward_tracking,
                 vex::rotation &left_tracking,
                 vex::inertial &inertial)
    : wheels{&left_front, &right_front, &left_back, &right_back},
      forward_tracking_sensor(forward_tracking),
      left_tracking_sensor(left_tracking),
      inertial_sensor(inertial)
{
}

void Chassis::init()
{
    // 初始化电机刹车模式为刹车
    for (int i = 0; i < 4; i++)
        wheels[i]->setStopping(vex::brakeType::brake);
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
    // 速度环不再用 is_moving 状态机，起步由电压斜率限制和连续摩擦补偿完成
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

    // 同步从"满力、无短板"起步：第一拍起才由实测达标率决定谁该被压。
    for (int i = 0; i < 4; i++)
    {
        wheels[i]->dyn_factor = 1.0f;
        wheels[i]->achieve = 1.0f;
        wheels[i]->achieve_valid = false;
        capability[i] = 1.0f;
        wheel_target[i] = 0.0f;
    }
    internal_force = 0.0f;
    fight_mode = false;
    fight_trans_scale = 1.0f;
    fight_time_us = 0;
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

    // 读取惯性传感器航向。增量只从 IMU 原始读数差分：上位机改 heading_offset
    // （或任何时刻写 heading）都不会让本轮增量失真，定位轮旋转补偿因此不受污染。
    float new_imu_heading = static_cast<float>(inertial_sensor.rotation(vex::rotationUnits::deg));
    // - gyro_bias * (now - heading_origin) / 1000000.0f;
    heading_change = new_imu_heading - imu_heading;
    imu_heading = new_imu_heading;
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

    // 定位轮里程回写用于下一轮；全局航向 = IMU 读数 + 软件偏置。
    forward_tracking_distance = new_forward_tracking_distance;
    side_tracking_distance = new_side_tracking_distance;
    heading = new_imu_heading + heading_offset;
    // 定位轮可以宣告到此结束

    // 里程计速度使用本轮位移除以实际更新时间（全向底盘前/侧向都要，判停与对抗判定都要用）。
    // 两次调用落在同一微秒时 update_gap 可能为 0，此时不能进行除法。
    if (update_gap > 0)
    {
        linear_speed = local_forward_change * 1000000.0f / update_gap;
        side_speed = local_side_change * 1000000.0f / update_gap;
    }
    // else 时保持不变(gap=0,无新值)

    // 内耗指标：轮速投影到运动学矩阵的左零空间 (1,1,-1,-1)/2 上的分量。
    // 四个轮只有三个自由度，刚体运动永远在这个方向上投影为 0，故非零即轮速互不一致。
    // 用的是上一拍速度环读到的转速(pct)，与轮目标同口径；差速底盘无冗余，没有这个量。
    internal_force = (wheels[0]->pre_v + wheels[1]->pre_v -
                      wheels[2]->pre_v - wheels[3]->pre_v) * 0.5f;

    // 计算当前坐标自 reset() 以来的local1下的位移
    float x_from_reset = x - x_start;
    float y_from_reset = y - y_start;
    distance_from_reset = x_from_reset * cos_i + y_from_reset * sin_i;
    side_distance_from_reset = -x_from_reset * sin_i + y_from_reset * cos_i;
    heading_from_reset = heading - heading_start;

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

void Chassis::output(float forward, float side, float turn)
{
    // 位置环 PID 与手柄映射都从这里喂入，本函数只做混控并写目标，闭环在 speed_tick() 里跑。
    //
    // X 布局全向轮：四个轮轴与车体成 45°，平移时四轮速度幅值相同、转向时左轮加右轮减。
    // 车体速度 (前 f, 右 s, 右转 t) 到四轮速度的线性映射（顺序 左前/右前/左后/右后）：
    //     w = ( f + s + t,  f - s - t,  f - s + t,  f + s - t )
    // 平移与旋转在混控前就是三个独立标量，线性叠加后依旧自洽——
    // 任何 (f,s,t) 组合生成的四个轮速都落在刚体运动的可行子空间里，指令层面不可能打架。
    // 受力不均时优先保姿态：平移按 fight_trans_scale 降幅，转向通道一点不降。
    // 平移掉 10cm 无所谓，姿态歪 10° 后面全崩——手柄推挤的手感就是"顶住了、慢慢推、车不歪"。
    const float fs = fight_mode ? fight_trans_scale : 1.0f;
    const float f = forward * fs;
    const float s = side * fs;
    const float t = turn * turn_ratio;

    float w[4] = {
        f + s + t, // 左前
        f - s - t, // 右前
        f - s + t, // 左后
        f + s - t, // 右后
    };

    // 等比缩回：按超出比例最大的那一轮把四轮同步缩回，保留指令向量的方向
    // （平移方向、以及转向与平移的比例）。各轮独立截断会破坏向量方向，是内耗来源之一。
    float scale = 1.0f;
    for (int i = 0; i < 4; i++)
        scale = fmaxf(scale, fabsf(w[i]) / max_wheel_speed);

    // 摩擦补偿由速度环内部电压域统一处理，这里不做任何死区映射，缩回后原样下发。
    for (int i = 0; i < 4; i++)
    {
        wheel_target[i] = w[i] / scale;
        wheels[i]->drive(wheel_target[i]);
    }
}

void Chassis::speed_tick()
{
    // 速度环唯一执行点：由短周期常驻任务调用。
    // 停车后各组 spin_state 为 Stopped，my_spin 立即返回；下次 output() 下目标时自动起步。
    for (int i = 0; i < 4; i++)
        wheels[i]->my_spin();
}

void Chassis::stop()
{
    // 指令必须一起清掉：同步吃的是 wheel_target，留着旧指令会让停着的车被判成"受力不均"。
    for (int i = 0; i < 4; i++)
        wheel_target[i] = 0.0f;
    // 各组 stop() 内部把 dyn_factor/achieve 复位为"满力"（静态差异由 volt_factor 标定承担，
    // 清掉在线学习到的那部分没损失），下次起步重新观察谁掉队。
    for (int i = 0; i < 4; i++)
        wheels[i]->stop();
}

// 同步：把"各轮实际达成情况"变成每轮一个新的 dyn_factor 下发下去。
//
// 为什么需要它：差速底盘某轮掉到 85% 时，胶轮的横向约束会把它硬拖着走，掉队量被地面吃掉，
// 姿态几乎不动；全向轮没有这个泄放通道，掉队的轮等于在那个角上持续施加一个制动力矩，
// 车就这样慢慢扭掉。所以差速底盘那份"机械耦合"在全向底盘上必须由控制器自己造出来。
//
// 为什么不用"谁慢就给谁加电压"：电压→转速→负载这条链路不可建模（对撞、配重、地面都在变），
// 预判不了。改成在线测"达成率"再同步降级——不预测、只同步：宁可一起慢，绝不各跑各的。
//
// 它是**慢性**回路：调整速度比速度环的 I 项还慢（dyn_*_per_s），
// 目的就是不抢在 PID 前面动作——同步只削天花板，不碰增益、不清积分、不改目标。
void Chassis::sync_tick()
{
    // 本拍时长取里程计那一拍的间隔（us）；为 0 表示还没跑过 update，本拍不动系数。
    const float dt_s = static_cast<float>(update_gap) / 1000000.0f;
    if (dt_s <= 0.0f)
        return;

    // ---- 1. 折算"满力达标能力" capability = achieve / dyn_factor ----
    // 关键一步：被我们压着的组折回 ≈1（它其实有能力），真跑不动的组才是短板。
    // 若直接用达标率取最小值，我们压出来的慢会被当成新短板，全车一起锁死在低速（闭锁）。
    float c_min = 1.0f;
    float c_max = 0.0f;
    bool any_valid = false;
    for (int i = 0; i < 4; i++)
    {
        MyMotorGroup &g = *wheels[i];
        if (!g.achieve_valid)
        {
            capability[i] = 1.0f; // 判不出达标率就不当短板（起步/换向/打滑/腾空都在这段里）
            continue;
        }
        capability[i] = fmaxf(cap_min, fminf(g.achieve / g.dyn_factor, 1.0f));
        c_min = fminf(c_min, capability[i]);
        c_max = fmaxf(c_max, capability[i]);
        any_valid = true;
    }

    // ---- 2. 受力不均判定：看各轮能力的离散度 / 内耗，不看"车动没动" ----
    // 整车顶死时四组都进 Recover，用不着专门判；两组卡死车还能动，判"不动"也说明不了问题。
    // 要防的是各轮受力不均 → 内耗 + 姿态歪，主要发生在手柄推挤期间。
    float cmd = 0.0f;
    for (int i = 0; i < 4; i++)
        cmd = fmaxf(cmd, fabsf(wheel_target[i]));
    const bool uneven = any_valid &&
                        ((c_max - c_min) > fight_disp_min ||
                         fabsf(internal_force) > fight_force_min);
    fight_time_us = uneven ? fight_time_us + update_gap : 0;
    fight_mode = fight_time_us >= fight_enter_us;
    // 平移通道降幅系数：受力越不均降得越多，下限 fight_trans_floor；转向通道不降（姿态优先）。
    fight_trans_scale = fight_mode ? fmaxf(fight_trans_floor, 1.0f - (c_max - c_min)) : 1.0f;

    // ---- 3. 每轮下发新系数 ----
    const float down_step = dyn_down_per_s * dt_s; // 本拍最多压低多少
    const float up_step = dyn_up_per_s * dt_s;     // 本拍最多回升多少
    for (int i = 0; i < 4; i++)
    {
        MyMotorGroup &g = *wheels[i];
        float want;
        if (fight_mode)
        {
            // 受力不均：改按指令向量的方向分配电压 → 合力方向不变，姿态不丢。
            // 原理：全向轮的力只沿轮轴方向，力向量取成 λ·J·v 一定落在 J 的像空间里（不打架），
            // 且 X 布局下 JᵀJ 是对角的，λ 按 |w_i| 取就能让合力方向对上指令方向。
            // 分支必须在 !achieve_valid 之前：否则处于 Transit/小目标的轮会被给到满力，
            // 其余轮按比例打折，正好破坏"守合力方向"这个唯一目的。
            want = fabsf(wheel_target[i]) / fmaxf(cmd, 1e-3f);
        }
        else if (!g.achieve_valid)
        {
            // 冻结：起步窗/换向/低速/打滑/腾空期间判不准，保持原值不动。
            // 不往 1 漂——打滑时电机转得飞快，"漂回满力"等于奖励了一个没抓地的轮。
            want = g.dyn_factor;
        }
        else
        {
            // 跟踪：能力强的让出（压到短板的水平），短板保持满力(1)。
            // 于是四轮达成率一致 → 实际运动是"指令方向的等比缩小版"，姿态不变形。
            want = c_min / capability[i];
        }
        want = fmaxf(dyn_floor, fminf(want, 1.0f));

        const float step = (want < g.dyn_factor) ? down_step : up_step;
        const float delta = fmaxf(-step, fminf(want - g.dyn_factor, step));
        g.dyn_factor += delta;
    }
}
