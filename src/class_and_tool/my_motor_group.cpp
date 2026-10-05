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
    target = 0.0f;
    for (uint8_t i = 0; i < 4; i++)
        motors[i]->stop();
}

// 速度环每短周期(5ms)调用一次，由常驻线程循环；追踪 target（pct）并用电压驱动电机。
// 核心设计：提速态不积 I、不限电压；稳定态才积 I 并限制电压抖动。
// 目标为0时输出0V不刹车，制动由外界 stop() 决定。
void MyMotorGroup::my_spin()
{
    if (is_stoped)
        return;
    uint32_t now_us = get_time_us();
    uint32_t dt_us = now_us - last_time_us;
    last_time_us = now_us;

    // 直接读 SDK 速度，不做滤波（kd≈0 且增益小，滤波无意义；量化根因在摩擦标定）。
    const float speed = static_cast<float>(motors[0]->velocity(vex::velocityUnits::pct));

    // 目标速度接近0：输出0V惰行。
    if (fabsf(target) <= output_deadzone)
    {
        previous_error = -speed;
        for (uint8_t i = 0; i < 4; i++)
        {
            volt_output[i] = 0.0f;
            prev_volt[i] = 0.0f;
            motors[i]->spin(vex::directionType::fwd, 0.0f, vex::voltageUnits::volt);
        }
        pre_v = speed;
        pre_a = (speed - pre_v) / dt_us * 1000000.0f;
        return;
    }

    // ---------- 提速态 / 稳定态 判态 ----------
    const float error = target - speed;
    const float now_a = (speed - pre_v) / dt_us * 1000000.0f;
    // 稳定条件：误差在 I 工作范围内 + 速度变化率低于阈值（pct/s，按 dt_us 归一化）。
    const float speed_scale = fmaxf(fabsf(speed), target_min_current);
    if (fabsf(error) <= error_for_i &&
        fabsf(now_a) <= speed_scale * speed_stable_ratio &&
        fabsf(pre_a) <= speed_scale * speed_stable_ratio)
        is_boost = false;

    // ---------- 连续摩擦补偿 ----------
    const float friction = dynamic_deadzone + (static_deadzone - dynamic_deadzone) * expf(-fabsf(target) / 3.0f);

    // ---------- PID ----------
    const float derivative = (error - previous_error) / static_cast<float>(dt_us);
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    const float feedforward = kf * target;

    // ------- 组内归一输出上限 ----------
    float max_volt_factor = volt_factor[0];
    for (uint8_t i = 1; i < 4; i++)
        if (volt_factor[i] > max_volt_factor)
            max_volt_factor = volt_factor[i];
    const float output_limit = 1.0f / max_volt_factor;

    // ---------- I 项：只在稳定态累积，且限步长 ----------
    if (!is_boost && fabsf(ki) > 1e-9f)
    {
        // I 项贡献的最大变化速率 → 每拍积分增量上限
        const float max_delta = (i_slew_max / ki) * static_cast<float>(dt_us);
        float delta = error * static_cast<float>(dt_us);
        if (delta > max_delta)
            delta = max_delta;
        else if (delta < -max_delta)
            delta = -max_delta;
        integral += delta;
    }

    float output = feedforward + p_output + ki * integral + d_output;
    if (output > output_limit)
        output = output_limit;
    else if (output < -output_limit)
        output = -output_limit;

    // ---------- 归一输出映射到四路电压 + 稳定态抖动限制 ----------
    target_sign = target > 0.0f ? 1 : -1;
    for (uint8_t i = 0; i < 4; i++)
    {
        const float base_voltage = friction + volt_min[i];
        const float controllable_voltage = volt_max[i] - base_voltage;
        float volt = target_sign * base_voltage +
                     output * controllable_voltage * volt_factor[i];
        if (volt > volt_max[i])
            volt = volt_max[i];
        else if (volt < -volt_max[i])
            volt = -volt_max[i];

        // 稳定态：限制电压抖动，纯粹消抖；提速态不限，让电压自由响应 target 突变。
        if (!is_boost)
        {
            const float max_dv = volt_jitter_max * static_cast<float>(dt_us) / 1000000.0f;
            const float dv = volt - prev_volt[i];
            if (dv > max_dv)
                volt = prev_volt[i] + max_dv;
            else if (dv < -max_dv)
                volt = prev_volt[i] - max_dv;
        }

        prev_volt[i] = volt;
        volt_output[i] = volt;
        motors[i]->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }

    // 更新历史
    previous_error = error;
    pre_v = speed;
    pre_a = now_a;
}

void MyMotorGroup::my_respin()
{
    is_stoped = false;
    integral = 0.0f;
    previous_error = 0.0f;
    target_sign = 0;
    for (uint8_t i = 0; i < 4; i++)
    {
        prev_volt[i] = 0.0f;
        volt_output[i] = 0.0f;
    }
    pre_v = 0.0f;
    pre_a = 0.0f;
    last_time_us = get_time_us();
}

void MyMotorGroup::drive(float target)
{
    // 停车后首次给目标：先清积分和运动状态再起步，避免上次残留积分造成冲击；
    // 运行中再次调用只改目标，不清任何环内状态。
    if (is_stoped)
        my_respin();

    // target 相对跳变：进入提速态的唯一入口。
    // 变化量超过 max(|new_target|, 3) 的一定比例即触发。
    const float target_scale = fmaxf(fabsf(target), target_min_current);
    if (fabsf(target - this->target) > target_scale * target_jump_ratio)
        is_boost = true;

    this->target = target;
}
