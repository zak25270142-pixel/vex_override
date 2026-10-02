#include "chassis.h"

MyMotorGroup::MyMotorGroup(vex::motor &m1, vex::motor &m2,
                           vex::motor &m3, vex::motor &m4,
                           float f, float p, float i, float d,
                           const float vf[4] = (float[]){1.0f, 1.0f, 1.0f, 1.0f},
                           const float vmin[4] = (float[]){0.5f, 0.5f, 0.5f, 0.5f})
{
    // 传入增益按秒量纲标定（误差单位 pct），内部统一换算到 us 时基。
    kf = f;
    kp = p;
    ki = i / 1000000.0f;
    kd = d * 1000000.0f;
    motors[0] = &m1;
    motors[1] = &m2;
    motors[2] = &m3;
    motors[3] = &m4;
    for (uint8_t j = 0; j < 4; j++)
    {
        volt_factor[j] = vf[j];
        volt_min[j] = vmin[j];
    }
}

void MyMotorGroup::setStopping(vex::brakeType brake)
{
    for (uint8_t i = 0; i < 4; i++)
        motors[i]->setStopping(brake);
}

// 在有定位轮后可舍弃
// void MyMotorGroup::resetPosition()
// {
//     for (uint8_t i = 0; i < 4; i++)
//         motors[i]->resetPosition();
// }

// double MyMotorGroup::position()
// {
//     return motors[0]->position(vex::rotationUnits::rev);
// }

double MyMotorGroup::velocity()
{
    return motors[0]->velocity(vex::velocityUnits::rpm);
}

void MyMotorGroup::stop()
{
    is_stoped = true;
    for (uint8_t i = 0; i < 4; i++)
        motors[i]->stop();
}

// 速度环每短周期(3-5ms)调用一次，由常驻线程循环；追踪 target（pct）并用电压驱动电机。
// 目标为0时输出0V不刹车，制动由外界 stop() 决定。
// dt不可能除0，绝对不可能
void MyMotorGroup::my_spin()
{
    if (is_stoped)
        return;
    uint32_t now_us = get_time_us();
    uint32_t dt_us = now_us - last_time_us;
    last_time_us = now_us;

    // 目标速度接近0：输出0V惰行且保留积分，专供目标连续过零的场景。
    // 注意整车“停车”统一走 stop()（brake 制动，再次 drive 时 my_respin 清积分），
    // 当前接线链路下停车即 is_stoped=true，正常不会停在本分支。
    if (fabsf(target) <= output_deadzone)
    {
        target = 0.0f;
        previous_error = -static_cast<float>(motors[0]->velocity(vex::velocityUnits::pct));
        for (uint8_t i = 0; i < 4; i++)
        {
            volt_output[i] = 0.0f;
            motors[i]->spin(vex::directionType::fwd, 0.0f, vex::voltageUnits::volt);
        }
        return;
    }

    // 两个量各司其职：rpm 是编码器原生物理量，只用于运动状态迟滞判定（阈值按 rpm 标定）；
    // pct 与目标同口径，只用于 PID 误差。
    const float speed_rpm = static_cast<float>(velocity());
    const float speed = static_cast<float>(motors[0]->velocity(vex::velocityUnits::pct));
    const float abs_speed = fabsf(speed_rpm);

    // 运动状态确认
    if (is_moving)
    {
        if (abs_speed <= stopped_confirm_speed)
            is_moving = false;
    }
    else if (abs_speed >= moving_confirm_speed)
        is_moving = true;

    const float friction = is_moving ? dynamic_deadzone : static_deadzone;

    const float error = target - speed;
    const float derivative = (error - previous_error) / static_cast<float>(dt_us);
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    const float feedforward = kf * target;

    // 组内归一输出上限：电压映射为 基础电压 + |output|·可控电压·volt_factor，
    // 不超过 volt_max 要求 |output| ≤ 1/volt_factor；volt_factor 最大的电机最先到顶，
    // 由它决定整组上限（当前为 1/1.2≈0.83，不是 1）。
    float max_volt_factor = volt_factor[0];
    for (uint8_t i = 1; i < 4; i++)
        if (volt_factor[i] > max_volt_factor)
            max_volt_factor = volt_factor[i];
    const float output_limit = 1.0f / max_volt_factor;

    // ---------- 条件积分抗饱和 ----------
    // 先把本轮误差计入候选积分并算出候选输出，饱和判据直接用上面的真实电压饱和点，
    // 与最终输出限幅完全一致，不会出现“电压已被削、积分还在堆”的脱节。
    // 已饱和但误差方向能帮助退出饱和时，仍允许积分（与位置环 PositionPID 同一套规则）。
    const float candidate_integral = integral + error * static_cast<float>(dt_us);
    const float candidate_output = feedforward + p_output + ki * candidate_integral + d_output;
    if (fabsf(candidate_output) <= output_limit ||
        (candidate_output > output_limit && error < 0.0f) ||
        (candidate_output < -output_limit && error > 0.0f))
    {
        integral = candidate_integral;
    }
    // 如需更积极的积分限幅，可在这里加绝对值上限（保证 i 项不超过输出上限），例如：
    // const float max_integral = output_limit / (ki + 1e-6f);
    // integral = fmaxf(-max_integral, fminf(max_integral, integral));

    float output = feedforward + p_output + ki * integral + d_output;
    if (output > output_limit)
        output = output_limit;
    else if (output < -output_limit)
        output = -output_limit;

    // ---------- 归一输出映射到四路电压 ----------
    // output 已限在组内上限内，代入映射后每一路都不会超过各自 volt_max，
    // 无需再做事后等比缩回；volt_output 保留供监控比对。
    const float abs_output = fabsf(output);
    const float sign = output >= 0.0f ? 1.0f : -1.0f;
    for (uint8_t i = 0; i < 4; i++)
    {
        const float controllable_voltage = volt_max[i] - friction - volt_min[i];
        volt_output[i] = sign * (friction + volt_min[i] +
                                 abs_output * controllable_voltage * volt_factor[i]);
        motors[i]->spin(vex::directionType::fwd, volt_output[i], vex::voltageUnits::volt);
    }

    previous_error = error;
}
void MyMotorGroup::my_respin()
{
    is_stoped = false;
    integral = 0.0f;
    previous_error = 0.0f;
    is_moving = false;
    last_time_us = get_time_us();
    // 其他量的重置
}

void MyMotorGroup::drive(float target)
{
    // 停车后首次给目标：先清积分和运动状态再起步，避免上次残留积分造成冲击；
    // 运行中再次调用只改目标，不清任何环内状态。
    if (is_stoped)
        my_respin();
    this->target = target;
}
