#include "chassis.h"
#include "timer.h" // get_time_us

// 本文件装两类实现：
//   ① ChassisMotorGroup——六态机速度环（底盘专属，只算可调电压）；
//   ② Chassis——里程计、位置环、两组之间的缩回与限幅。
// 两者同属"底盘"这一个概念，声明同在 chassis.h，实现也放一处。

ChassisMotorGroup::ChassisMotorGroup(MyMotor *motor_list, uint8_t count,
                                     const Vec2f *kf_node, uint8_t kf_num,
                                     float p, float i, float d)
    : MotorGroup(motor_list, count, kf_node, kf_num, p, i, d)
{
}

// 先调基类做存储与跳变判定，再按状态机决定起步 / 换向
void ChassisMotorGroup::set_target(float t)
{
    const int8_t old_sign = target_sign; // 换向判据要"上一次"的方向
    const float old_target = target;     // 大跳判据要"上一次"的指令
    MotorGroup::set_target(t);           // 基类只存储 + 死区归零，判断全在下面
    const uint32_t now_us = get_time_us();

    // 大跳只看命令自己变了多少，**不掺 pre_v**：阻塞或 I 还没追上时 pre_v 天然远离
    // 目标，掺进来判据就退化成"Δtarget > 阈值"，而它本来想表达的是"车已经在新目标
    // 附近就不算大跳"——那件事 Transit 自己会做（误差进了 error_for_i 一拍就回 Track）。
    // 掺了被控量，同一个拨杆在带载与空载时行为就不同。
    const bool jumped = fabsf(target - old_target) > target_jump_abs;

    if (spin_state == Stopped && target != 0.0f)
    {
        last_time_us = now_us;
        if (fabs_target < err_out_dead)
            spin_state = Track; // 目标很小，直接跟踪，不必走起步窗
        else
        {
            spin_state = Transit;
            transit_start_us = now_us;
        }
    }
    else if (jumped)
    {
        // 只进 Transit 观察（那里不积分），**不清积分**：
        // 积分是顶住负载的那一项，阻塞时被清掉等于当场松劲，而操作手在顶东西时
        // 恰恰最爱动杆。Transit 本身很便宜——误差进了 error_for_i 就一拍回 Track。
        spin_state = Transit;
        transit_start_us = now_us;
    }
    else if (target_sign != 0 && old_sign != 0 && target_sign != old_sign)
    {
        spin_state = Transit; // 换向
        transit_start_us = now_us;
    }
}

// 六态机版速度环：算本组可调电压（写入 output），不执行、不限幅。
// 时序：refresh（采样 + PID 前置）→ 状态判定① → 基础计算 → 积分策略 → 状态判定② → 压摆。
// 五段不是"随手拆细"，各自都有理由：
//   · 判定①只需要误差/加速度，而它的结果（积不积、积多少）是积分策略的输入；
//   · 基础计算用"当前 integral"算出本拍输出，本身不含任何判断；
//   · 积分策略要判"吃掉这段积分后输出会到哪"，只能排在有了输出之后；
//   · 判定②（Stall/Recover）要用本拍输出判"是否顶格"，所以必须在最后。
// 基类只提供 refresh / calc_output / apply_output 三段，判断全在本层。
// 执行由 MotorGroup::apply_output() 负责，横向限幅由调用者（Chassis）负责。
void ChassisMotorGroup::calc_output()
{
    // 采样 + 前置。停车也要跑：监控要看真实转速，且 pre_v 一旦冻结，
    // 重新给目标那拍的加速度会是个巨大的假值（正好撞上 Recover/Stall 的加速度判据）。
    MotorGroup::refresh();

    if (spin_state == Stopped)
        return; // 环不工作：连 P 项都不算，output 保持 stop() 留下的 0

    // 时间戳直接用 refresh 采样的那一刻（last_time_us）：状态机计时与转速采样对齐，
    // 也省一次系统调用。error / derivative / base_volt / feedforward / headroom
    // 都已在 refresh 里算好，这里直接读。
    const uint32_t now_us = last_time_us;
    const float fabs_error = fabsf(error);

    // ---------- 状态判定 1：目标 / 稳定（不依赖本拍输出，先判给 I 项用）----------
    if (spin_state == Transit && (fabs_error <= error_for_i ||
                                  (now_us - transit_start_us >= transit_freeze_us &&
                                   fabsf(now_a) <= speed_stable && fabsf(pre_a) <= speed_stable)))
        spin_state = Track;
    else if (spin_state == Track && fabs_error <= err_in_dead)
        spin_state = Stable;
    else if (spin_state == Stable && fabs_error > err_out_dead)
        spin_state = Track;

    // ---------- 基础计算：前馈 + P + I + D（用当前 integral，不判不改）----------
    MotorGroup::calc_output();

    // ---------- 积分策略：抗饱和 + 缩放。基类不参与，全在这一层 ----------
    // 状态只决定两件事：本拍积不积（Transit 起步窗不积）、积多少（Stable 打对折防抖）。
    const bool allow_i = (spin_state != Transit);
    const float i_scale = (spin_state == Stable) ? i_stable_k : 1.0f;

    const float delta = error * dt_s * i_scale; // 本拍"想"积的量，pct·s
    // 判"吃掉这一段之后输出会到哪"，才停在天花板上而不是冲过头再回吐。
    // 输出对积分是线性的，所以只需在现值上补 I 那一段，不必把 P/D/前馈重算一遍。
    // 天花板必须是 headroom 而不是 12V：output 之上还要叠组级垫底与个体死区，
    // 与比例限幅的分母是同一个数——两者不一致时，内层以为还有余量、外层已经在削。
    const float candidate = output + ki * delta;
    // ki <= 0 时不积：此时积分不影响输出，攒下的只是"ki 一开就生效"的存量。
    if (allow_i && ki > 0.0f &&
        (candidate <= headroom || error <= 0.0f) &&
        (candidate >= -headroom || error >= 0.0f))
        integral += delta;
    // 本拍积的这段，下一拍的输出才吃到（与 D 用 previous_error 一样，都是"上一拍
    // 结算的历史"）。要当拍生效就得在这里再补一次输出，那等于把 P/D/夹幅再算一遍，
    // 而 10ms 的 I 项延迟对 ki 这个量级毫无意义。

    // ---------- 状态判定 2：非预期运动 / 堵转（要用本拍输出，故排在 PID 之后）----------
    // "已给到上限"必须按 headroom 判，不能按 0.99×12V：抗饱和把积分卡在 headroom 上
    // （≈10.6V），12V 那个门槛在低速重载时永远够不着，Stall 就永远进不去。
    // 又一处"两个天花板必须同一个数"。
    const bool giving_max = fabsf(output) >= headroom * 0.99f;                                   // 输出是否已顶到上限
    const bool wheel_low = fabsf(now_v) <= stall_speed;                                          // 轮是否基本没在转
    const bool pulled_away = (target > 0.0f && now_a < 0.0f) || (target < 0.0f && now_a > 0.0f); // 目标与加速度反向

    if (spin_state == Stall)
    {
        // Stall 退出：轮速起来了，或操作手松杆（目标落进死区）；杆收小但没收到底不退出
        if (!wheel_low || fabs_target == 0.0f)
        {
            stall_since_us = 0;
            transit_start_us = now_us;
            spin_state = Transit;
        }
    }
    else
    {
        // Stall 计时：给满 + 轮速低须持续够久（滤掉起步瞬间）；Transit 期间不计时
        if (giving_max && wheel_low && spin_state != Transit)
        {
            if (stall_since_us == 0)
                stall_since_us = now_us;
        }
        else
            stall_since_us = 0;

        if (stall_since_us != 0 && now_us - stall_since_us >= stall_time)
            spin_state = Stall;
        else if (spin_state == Recover)
        {
            // Recover 退出：误差回到 I 工作区且输出不再顶格，走 Transit 重新观察（积分不清零）
            if (fabs_error <= error_for_i && !giving_max)
            {
                transit_start_us = now_us;
                spin_state = Transit;
            }
        }
        else if (spin_state != Transit && pulled_away &&
                 (fabsf(now_a) > recover_accel || (giving_max && fabsf(now_a) > speed_stable)))
        {
            // Recover 进入：目标与加速度反向且幅度超门槛；前置不在 Transit
            // （那里的 error 是操作手造成的，不是外力）
            spin_state = Recover;
        }
    }

    // ---------- 压摆策略：把状态翻译成两个数值，写进基类 ----------
    slew_policy();

    // pre_v / pre_a 的提交在 refresh() 里，本函数不再写历史值。
}

// 状态 → 斜率数值。基类只认数，不认枚举；这里就是策略的唯一出处。
// 每个状态都必须显式覆盖，别指望"沿用上一次"——状态是在本函数前段判出来的，
// 漏写一个分支就等于让上一种策略漏到这一种里。
void ChassisMotorGroup::slew_policy()
{
    switch (spin_state)
    {
    case Stopped: // 电压钉死：stop() 已把各台清零并刹车，之后不再重写电压
        slew_boost = 0.0f;
        slew_release = 0.0f;
        break;

    case Recover: // 外力带偏、车还在动：两方向都放开，抢时间补力矩
        slew_boost = INFINITY;
        slew_release = INFINITY;
        break;

    case Stall: // 顶死：顶住要快（别被推回去），松劲要慢（松了会被弹回来）
        slew_boost = slew_fast;
        slew_release = slew_slow;
        break;

    default: // Transit / Track / Stable：升压慢（护齿箱、防打滑），撤压快
        slew_boost = slew_slow;
        slew_release = slew_fast;
        break;
    }
}

void ChassisMotorGroup::stop()
{
    MotorGroup::stop(); // 清积分、输出、各台电压
    spin_state = Stopped;
    transit_start_us = 0;
    stall_since_us = 0;
    // Stopped 态 calc_output 会提前返回，走不到里面的 slew_policy()，
    // 必须在这里补写一次——否则会留着上一个状态的斜率（比如 Recover 的无穷）。
    slew_policy();
}

// ==================== 以下为 Chassis（里程计 + 横向协调）====================

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
    // 初始化电机刹车模式为刹车（左右各设一次，brake 是 SDK 侧状态不是本工程的）
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
    // 速度环目标入口。缩回系数取"超出比例最大"的那一侧，两侧同比例缩小——
    // 保比例而非单边削平，削平会改变左右出力比，车直接跑偏。
    // 之所以在底盘而不是组内：单组看不到另一侧的需求量。
    float s = 1.0f;
    if (left_motors.max_speed > 0.0f)
        s = fmaxf(s, fabsf(left_output) / left_motors.max_speed);
    if (right_motors.max_speed > 0.0f)
        s = fmaxf(s, fabsf(right_output) / right_motors.max_speed);
    spd_scale = s;

    left_motors.set_target(left_output / s);
    right_motors.set_target(right_output / s);
}

void Chassis::speed_tick()
{
    // 本拍这个组要不要跑速度环？只看状态机：Stopped = 环不工作。
    // 环不工作时这里什么都不做——**不是"每帧补一个 0V"**。0V 也是一条指令，
    // 会把开环直写的电压覆盖掉；而"停住"的语义已经由 stop() 里那次 motor->stop()
    // （按 setStopping 的刹车）表达过了，不需要每秒重复声明 100 次。
    // 这样开环电压天然不会被覆盖，也就不需要任何"开环标志"。

    // ① 算本拍可调电压。两组都调：停车组在里面自己停在采样之后就返回了，
    //    所以监控照样能看到真实转速，而 output 保持 0。
    left_motors.calc_output();
    right_motors.calc_output();

    // ② 比例限幅只统计在跑的组。停车组的 output 是残值，若让它参与，
    //    会凭一个早已作废的数去削另一侧——拿冻结值当真值用。
    const bool run_l = !left_motors.is_stopped();
    const bool run_r = !right_motors.is_stopped();
    const float head_l = left_motors.headroom;
    const float head_r = right_motors.headroom;
    float s = 1.0f;
    if (run_l && head_l > 0.0f)
        s = fmaxf(s, fabsf(left_motors.output) / head_l);
    if (run_r && head_r > 0.0f)
        s = fmaxf(s, fabsf(right_motors.output) / head_r);
    volt_scale = s;

    // ③ 下发。限幅系数当参数交给组，不回写它们的 output——output 始终是
    //    calc_output 算出的请求值，谁也没改过它。停车组不碰（可能是开环直写的）。
    if (run_l)
        left_motors.apply_output(s);
    if (run_r)
        right_motors.apply_output(s);
}

void Chassis::stop()
{
    left_motors.stop();
    right_motors.stop();
}

