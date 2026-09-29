#include "chassis.h"

MyMotorGroup::MyMotorGroup(vex::motor &m1, vex::motor &m2,
                           vex::motor &m3, vex::motor &m4,
                           float f, float p, float i, float d)
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
    uint32_t current_time_us = get_time_us();
    uint32_t dt_us = current_time_us - last_time_us;
    last_time_us = current_time_us;

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
    const float feedforward = kf * target;
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    const float i_output = ki * integral;

    // ---------- 条件积分（anti-windup）----------
    // 先算"如果不加本次误差积分"的输出，判断当前是否已饱和
    const float output_before_clip = feedforward + p_output + i_output + d_output;
    const float clipped_preview = (output_before_clip > 1.0f) ? 1.0f : (output_before_clip < -1.0f) ? -1.0f
                                                                                                    : output_before_clip;
    const bool is_saturated = (fabsf(output_before_clip) >= 1.0f);

    // 积分条件：未饱和 或 误差与输出反向（积分已在减小，无需抑制）
    // 误差变号时积分自然衰减，不干预
    if (!is_saturated || (error * clipped_preview < 0.0f))
    {
        integral += error * static_cast<float>(dt_us);
    }

    // 如需更积极的积分限幅，可在这里加绝对值上限，例如：
    // const float max_integral = 1.0f / (ki + 1e-6f); // 保证i_output不超过1.0
    // integral = fmaxf(-max_integral, fminf(max_integral, integral));

    const float final_i_output = ki * integral;
    float output = feedforward + p_output + final_i_output + d_output;
    if (output > 1.0f)
        output = 1.0f;
    else if (output < -1.0f)
        output = -1.0f;

    const float abs_output = fabsf(output);
    const float sign = output > 0.0f ? 1.0f : -1.0f;

    // ---------- 堵转保护：任一路电流超限则整体降输出 ----------
    // motors[i]->current(vex::currentUnits::amp) 获取实时电流
    float current_max = 0.0f;
    for (uint8_t i = 0; i < 4; i++)
    {
        const float c = motors[i]->current(vex::currentUnits::amp);
        if (c > current_max)
            current_max = c;
    }
    const float current_limit = 2.5f; // 堵流阈值，单位A，需实测标定
    float current_scale = 1.0f;
    if (current_max > current_limit)
    {
        // 线性衰减，越超限压得越狠
        current_scale = fmaxf(0.3f, current_limit / current_max);
    }

    float largest_output_ratio = 0.0f;
    for (uint8_t i = 0; i < 4; i++)
    {
        // PID真正可以调节的电压范围
        const float controllable_voltage = volt_max[i] - friction - volt_min[i];
        volt_output[i] = sign * (friction + volt_min[i] +
                                 abs_output * controllable_voltage * volt_factor[i]);
        // 应用堵转缩放
        volt_output[i] *= current_scale;

        const float output_ratio = fabsf(volt_output[i]) / volt_max[i];
        if (output_ratio > largest_output_ratio)
            largest_output_ratio = output_ratio;
    }
    // 超过任意电机上限后，四路整体等比例限幅
    if (largest_output_ratio > 1.0f)
    {
        const float scale = 1.0f / largest_output_ratio;
        for (uint8_t i = 0; i < 4; i++)
            volt_output[i] *= scale;
    }
    for (uint8_t i = 0; i < 4; i++)
        motors[i]->spin(vex::directionType::fwd, volt_output[i], vex::voltageUnits::volt);

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
