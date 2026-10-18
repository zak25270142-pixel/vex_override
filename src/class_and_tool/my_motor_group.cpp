#include "chassis.h"

MyMotorGroup::MyMotorGroup(MyMotor *motor_list, uint8_t count,
                           const Vec2f *kf_node, uint8_t kf_num,
                           float p, float i, float d)
{
    this->motors = motor_list;
    this->kf_node = kf_node;
    this->kf_num = kf_num;
    kp = p;
    ki = i;
    kd = d;
    motor_count = count;
}

// 前馈插值：|target| 落在哪两节点之间就按距离加权，返回该处曲线上的稳态前馈输出（归一）。
float MyMotorGroup::kf_at(float abs_target) const
{
    if (abs_target <= kf_node[0].x)
        return kf_node[0].y;
    for (uint8_t i = 1; i < kf_num; i++)
    {
        if (abs_target <= kf_node[i].x)
        {
            const float t = (abs_target - kf_node[i - 1].x) / (kf_node[i].x - kf_node[i - 1].x);
            return kf_node[i - 1].y + (kf_node[i].y - kf_node[i - 1].y) * t;
        }
    }
    return kf_node[kf_num - 1].y;
}

void MyMotorGroup::setStopping(vex::brakeType brake)
{
    for (uint8_t i = 0; i < motor_count; i++)
        motors[i].motor->setStopping(brake);
}

// 在有定位轮后可舍弃
// void MyMotorGroup::resetPosition()
// {
//     for (uint8_t i = 0; i < motor_count; i++)
//         motors[i].motor->resetPosition();
// }

// double MyMotorGroup::position()
// {
//     return motors[0].motor->position(vex::rotationUnits::rev);
// }

void MyMotorGroup::stop()
{
    spin_state = Stopped;
    target = 0.0f;
    integral = 0.0f;
    previous_error = 0.0f;
    target_sign = 0;
    pre_v = 0.0f;
    pre_a = 0.0f;
    transit_start_us = 0;
    stall_since_us = 0;
    for (uint8_t i = 0; i < motor_count; i++)
    {
        motors[i].volt_output = 0.0f;
        motors[i].motor->stop();
    }
}

void MyMotorGroup::my_spin()
{
    if (spin_state == Stopped)
        return;
    uint32_t now_us = get_time_us();
    uint32_t dt_us = now_us - last_time_us;
    if (dt_us < 1000) // drive 与 my_spin 同拍/亚毫秒重叠时防首帧除零
        dt_us = 1000;
    else if (dt_us > 20000)
        dt_us = 20000;
    const float dt_s = static_cast<float>(dt_us) / 1000000.0f;

    last_time_us = now_us;

    // 直接读 motors[0] 速度，不做滤波
    const float speed = static_cast<float>(motors[0].motor->velocity(vex::velocityUnits::pct));

    // ---------- 状态判定 1：目标 / 稳定（不依赖本拍输出，先判给 I 项用）----------
    const float error = target - speed;
    const float fabs_error = fabsf(error);
    const float derivative = (error - previous_error) / dt_s;
    const float now_a = (speed - pre_v) / dt_s;

    if (spin_state == Transit && (fabs_error <= error_for_i || (now_us - transit_start_us >= transit_freeze_us && fabsf(now_a) <= speed_stable && fabsf(pre_a) <= speed_stable)))
        spin_state = Track;
    else if (spin_state == Track && fabs_error <= err_in_dead)
        spin_state = Stable;
    else if (spin_state == Stable && fabs_error > err_out_dead)
        spin_state = Track;

    const float friction = dynamic_deadzone + (static_deadzone - dynamic_deadzone) * expf(-fabs_target / 3.0f);
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    feedforward = target_sign * kf_at(fabs_target);

    // ---------- I 项：Transit 不工作；Stable 只缩小新增积分，避免已有 I 输出跳变 ---------
    if (spin_state != Transit)
    {
        float delta = error * dt_s;
        if (spin_state == Stable)
            delta *= i_stable_k;
        // 方向性抗饱和：候选输出在误差想推的方向被归一硬限顶住，本拍就不积；
        const float candidate_output = feedforward + p_output + ki * (integral + delta) + d_output;
        if ((candidate_output <= 1.0f || error <= 0.0f) && (candidate_output >= -1.0f || error >= 0.0f))
            integral += delta;
    }
    float output = feedforward + p_output + ki * integral + d_output;

    if (output > 1)
        output = 1;
    else if (output < -1)
        output = -1;

    // ---------- 状态判定 2：非预期运动 / 堵转（要用到本拍输出，故排在 PID 之后）----------
    const bool giving_max = fabsf(output) >= stall_output;                                       // 归一输出是否已经顶到上限
    const bool wheel_low = fabsf(speed) <= stall_speed;                                          // 轮胎是否基本没在转
    const bool pulled_away = (target > 0.0f && now_a < 0.0f) || (target < 0.0f && now_a > 0.0f); // 目标是否和加速度反向

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
        // Stall 计时：给满+轮速低须持续够久（滤掉起步瞬间）；Transit 期间不计时
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
            // Recover 进入：目标与加速度反向且幅度超门槛；前置不在 Transit（那里的 error 是操作手造成的）
            spin_state = Recover;
        }
    }

    // ---------- 归一输出映射到各路电压 + 电压斜率限制 ----------
    // 换向以 0 为界分段限速、额度按时间分，思路见 docs/代码补充注释/01
    const bool slow_on_rise = spin_state != Stall;                    // 常态加压慢、撤压快；Stall 反过来：松劲慢、顶住快
    const float rate_fall = slow_on_rise ? slew_release : slew_boost; // 幅值减小方向的速率，单位 V/s
    const float rate_rise = slow_on_rise ? slew_boost : slew_release; // 幅值增大方向的速率，单位 V/s
    for (uint8_t i = 0; i < motor_count; i++)
    {
        const float base_voltage = friction + motors[i].volt_min;                                        // 基础电压
        const float controllable_voltage = motors[i].volt_max - base_voltage;                            // 可调电压
        float volt = target_sign * base_voltage + output * controllable_voltage * motors[i].volt_factor; // 目标电压
        const float volt_output = motors[i].volt_output;
        if (spin_state != Recover) // Recover 两方向都不限斜率，抢时间补力矩
        {
            if (volt * volt_output < 0.0f) // 过零换向：撤到 0 之前升压速率不生效
            {
                const float t_fall = fabsf(volt_output) / rate_fall; // 撤到 0 要花的时间，单位 s
                if (t_fall >= dt_s)
                    volt = volt_output - (volt_output > 0.0f ? rate_fall : -rate_fall) * dt_s; // 本拍撤不完
                else
                {
                    const float rise = rate_rise * (dt_s - t_fall); // 剩下的时间够升多少，单位 V
                    volt = volt > 0.0f ? fminf(volt, rise) : fmaxf(volt, -rise);
                }
            }
            else
            {
                const float limit = (fabsf(volt) > fabsf(volt_output) ? rate_rise : rate_fall) * dt_s;
                const float dv = volt - volt_output; // 目标电压变化
                if (fabsf(dv) > limit)
                    volt = volt_output + (dv > 0.0f ? limit : -limit);
            }
        }
        motors[i].volt_output = volt;
        motors[i].motor->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }
    // 更新历史
    previous_error = error;
    pre_v = speed;
    pre_a = now_a;
}

void MyMotorGroup::drive(float target)
{
    fabs_target = fabsf(target);
    if (fabs_target <= output_deadzone)
    {
        target = 0.0f;
        fabs_target = 0.0f;
    }
    const uint32_t now_us = get_time_us();
    const int8_t new_target_sign = target > 0.0f ? 1 : (target < 0.0f ? -1 : 0);
    if (spin_state == Stopped && target != 0.0f)
    {
        last_time_us = now_us;
        if (fabs_target < err_out_dead)
            spin_state = Track;
        else
        {
            spin_state = Transit;
            transit_start_us = now_us;
        }
    }
    else if (fabsf(target - this->target) > target_jump_abs && fabsf(target - pre_v) > target_jump_abs)
    { // 目标跳动：积分清零，走 Transit 重新观察。
        integral = 0.0f;
        spin_state = Transit;
        transit_start_us = now_us;
    }
    else if (new_target_sign != 0 && target_sign != 0 && new_target_sign != target_sign)
    {
        spin_state = Transit;
        transit_start_us = now_us;
    }
    target_sign = new_target_sign;
    this->target = target;
}
