#include "chassis.h"

MyMotorGroup::MyMotorGroup(const MyMotor *motor_list, uint8_t count,
                           float f, float p, float i, float d)
{
    kf = f;
    kp = p;
    ki = i;
    kd = d;
    motor_count = count;
    for (uint8_t j = 0; j < count; j++)
        motors[j] = motor_list[j];
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

double MyMotorGroup::velocity()
{
    return motors[0].motor->velocity(vex::velocityUnits::rpm);
}

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
    for (uint8_t i = 0; i < motor_count; i++)
    {
        motors[i].volt_output = 0.0f;
        motors[i].motor->stop();
    }
}

// 目标为0时输出0V不刹车，制动由外界 stop() 决定。
void MyMotorGroup::my_spin()
{
    if (spin_state == Stopped)
        return;
    uint32_t now_us = get_time_us();
    uint32_t dt_us = now_us - last_time_us;
    if (dt_us < 1000) // drive 与my_spin 撞后 dt容易很小，给个1ms最小值
        dt_us = 1000;
    else if (dt_us > 20000)
        dt_us = 20000;
    const float dt_s = static_cast<float>(dt_us) / 1000000.0f;

    last_time_us = now_us;

    // 直接读 SDK 速度，不做滤波
    const float speed = static_cast<float>(motors[0].motor->velocity(vex::velocityUnits::pct));

    // 目标速为0：输出0V惰行。(在drive中归一)
    if (fabs_target == 0.0f)
    {
        previous_error = -speed;
        for (uint8_t i = 0; i < motor_count; i++)
        {
            motors[i].volt_output = 0.0f;
            motors[i].motor->spin(vex::directionType::fwd, 0.0f, vex::voltageUnits::volt);
        }
        pre_a = (speed - pre_v) / dt_s;
        pre_v = speed;
        return;
    }

    // ---------- 状态判定 ----------
    const float error = target - speed;
    const float now_a = (speed - pre_v) / dt_s;
    const bool error_small = fabsf(error) <= error_for_i;
    const bool acceleration_small = fabsf(now_a) <= speed_stable && fabsf(pre_a) <= speed_stable;
    const float error_change = error - previous_error;
    // target 未大跳而误差仍向自身方向增大，才认为是负载变化或实际跟不上。
    const bool need_recover = fabsf(error) > error_for_i && error * error_change > 0.0f;

    if (spin_state == Transit)
    {
        // 冻结窗内只采样，避免刚起步速度未变化、加速度为 0 而提前进入 Track。
        // 若确实推不动，冻结窗结束后的低加速度会放开 I，避免一直卡在 Transit。
        if (error_small || (now_us - transit_start_us >= transit_freeze_us && acceleration_small))
            spin_state = Track;
    }
    else if ((spin_state == Track || spin_state == Stable) && need_recover)
    {
        spin_state = Recover;
    }
    else if (spin_state == Track && fabsf(error) <= err_dead && acceleration_small)
    {
        spin_state = Stable;
    }
    else if (spin_state == Stable && (fabsf(error) > err_dead || !acceleration_small))
    {
        spin_state = Track;
    }
    else if (spin_state == Recover && !need_recover)
    {
        spin_state = (fabsf(error) <= err_dead && acceleration_small) ? Stable : Track;
    }

    // ---------- 连续摩擦补偿 ----------
    const float friction = dynamic_deadzone + (static_deadzone - dynamic_deadzone) * expf(-fabs_target / 3.0f);

    // ---------- PID ----------
    const float derivative = (error - previous_error) / dt_s;
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    const float feedforward = kf * target;

    // ---------- I 项：Transit 不工作；Stable 只缩小新增积分，避免已有 I 输出跳变 ---------
    if (spin_state != Transit)
    {
        float delta;
        // 仅在同向增加积分（加速充能）时限制步长；反向退积分（泄能）时放开限制
        if (integral >= 0.0f && error > i_slew)
            delta = i_slew * dt_s;
        else if (integral <= 0.0f && error < -i_slew)
            delta = -i_slew * dt_s;
        else
            delta = error * dt_s;

        if (spin_state == Stable)
            delta *= i_stable_k;

        // 方向性抗饱和：候选输出在误差想推的方向被归一硬限顶住，本拍就不积；
        const float candidate_output = feedforward + p_output + ki * (integral + delta) + d_output;
        if ((candidate_output <= 1.0f || error <= 0.0f) && (candidate_output >= -1.0f || error >= 0.0f))
        {
            integral += delta;
        }
    }

    float output = feedforward + p_output + ki * integral + d_output;
    if (output > 1)
        output = 1;
    else if (output < -1)
        output = -1;

    // ---------- 归一输出映射到各路电压 + 电压斜率限制 ----------
    // Recover 已确认是负载突变，不再限电压斜率，让 P、I 立刻给出所需补偿。
    // max_dv 是正的绝对电压步长；不能乘带符号 output，否则反向命令的限幅方向会反转。
    const float max_dv = slew_boost * dt_s;
    for (uint8_t i = 0; i < motor_count; i++)
    {
        const float base_voltage = friction + motors[i].volt_min;
        const float controllable_voltage = motors[i].volt_max - base_voltage;
        float volt = target_sign * base_voltage + output * controllable_voltage * motors[i].volt_factor;
        const float dv = volt - motors[i].volt_output;
        if (spin_state != Recover)
        {
            if (dv > max_dv)
                volt = motors[i].volt_output + max_dv;
            else if (dv < -max_dv)
                volt = motors[i].volt_output - max_dv;
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
    // 进入 Transit 的三种情形都刷新冻结窗起点，故这里统一取一次时间。
    const uint32_t now_us = get_time_us();
    // 运行中再次调用只改目标，不清任何环内状态。
    if (spin_state == Stopped)
    {
        last_time_us = now_us;
        transit_start_us = now_us;
        spin_state = Transit;
    }
    // 清积分、进提速态，避免旧方向积分带着车冲过零点。
    const int8_t new_target_sign = target > 0.0f ? 1 : (target < 0.0f ? -1 : 0);
    if (new_target_sign != 0)
    {
        if (target_sign != 0 && new_target_sign != target_sign)
        {
            integral = 0.0f;
            transit_start_us = now_us;
            spin_state = Transit;
        }
        target_sign = new_target_sign;
    }
    // else target_sign保留符号

    // target 相对跳变：进入 Transit。
    const float target_scale = fmaxf(fabs_target, target_min_current);
    if (fabsf(target - this->target) > target_scale * target_jump_ratio)
    {
        transit_start_us = now_us;
        spin_state = Transit;
    }

    this->target = target;
}
