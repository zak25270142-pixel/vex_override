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
    spin_state = Stopped;
    target = 0.0f;
    integral = 0.0f;
    previous_error = 0.0f;
    target_sign = 0;
    pre_v = 0.0f;
    pre_a = 0.0f;
    for (uint8_t i = 0; i < 4; i++)
    {
        volt_output[i] = 0.0f;
        motors[i]->stop();
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
    float dt_s = static_cast<float>(dt_us) / 1000000.0f;

    last_time_us = now_us;

    // 直接读 SDK 速度，不做滤波
    const float speed = static_cast<float>(motors[0]->velocity(vex::velocityUnits::pct));

    // 目标速为0：输出0V惰行。(在drive中归一)
    if (fabs_target == 0.0f)
    {
        previous_error = -speed;
        for (uint8_t i = 0; i < 4; i++)
        {
            volt_output[i] = 0.0f;
            motors[i]->spin(vex::directionType::fwd, 0.0f, vex::voltageUnits::volt);
        }
        pre_a = (speed - pre_v) / dt_s;
        pre_v = speed;
        return;
    }

    // ---------- 提速态 / 稳定态 判态 ----------
    const float error = target - speed;
    const float now_a = (speed - pre_v) / dt_s;

    // 误差小时需稳定态微操 加速度小时也放开i积分
    if (fabsf(error) <= fmaxf(error_for_i_min, error_for_i_ratio * fabs_target) ||
        (fabsf(now_a) <= speed_stable && fabsf(pre_a) <= speed_stable))
        spin_state = Track;

    // ---------- 连续摩擦补偿 ----------
    const float friction = dynamic_deadzone + (static_deadzone - dynamic_deadzone) * expf(-fabs_target / 3.0f);

    // ---------- PID ----------
    const float derivative = (error - previous_error) / static_cast<float>(dt_us);
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    const float feedforward = kf * target;

    // ---------- I 项：只在稳定态累积，限步长 + 方向性抗饱和 ---------
    if (spin_state == Track)
    {
        float delta;
        // 仅在同向增加积分（加速充能）时限制步长；反向退积分（泄能）时放开限制
        if (integral >= 0.0f && error > i_slew)
            delta = i_slew * dt_us;
        else if (integral <= 0.0f && error < -i_slew)
            delta = -i_slew * dt_us;
        else
            delta = error * static_cast<float>(dt_us);

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

    // ---------- 归一输出映射到四路电压 + 分级电压斜率限制 ----------
    // 提速态用 slew_boost（宽松但仍有上限，防踢车），稳定态用 volt_jitter_max（紧限抖）。
    const float max_dv = slew_boost * output * dt_s;
    for (uint8_t i = 0; i < 4; i++)
    {
        const float base_voltage = friction + volt_min[i];
        const float controllable_voltage = volt_max[i] - base_voltage;
        float volt = target_sign * base_voltage + output * controllable_voltage * volt_factor[i];
        const float dv = volt - volt_output[i];
        if (dv > max_dv)
            volt = volt_output[i] + max_dv;
        else if (dv < -max_dv)
            volt = volt_output[i] - max_dv;
        volt_output[i] = volt;
        motors[i]->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
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
    // 运行中再次调用只改目标，不清任何环内状态。
    if (spin_state == Stopped)
    {
        last_time_us = get_time_us();
        spin_state = Transit;
    }
    // 清积分、清电压历史、进提速态，避免旧方向积分带着车冲过零点。
    // 必要性存疑，与下面那个的职责划分也不太清晰
    const int8_t new_target_sign = target > 0.0f ? 1 : (target < 0.0f ? -1 : 0);
    if (new_target_sign != 0)
    {
        if (target_sign != 0 && new_target_sign != target_sign)
        {
            integral = 0.0f;
            spin_state = Transit;
        }
        target_sign = new_target_sign;
    }
    // else target_sign保留符号

    // target 相对跳变：进入提速态的唯一入口。
    // 变化量超过 max(|new_target|, 3) 的一定比例即触发。
    // 在速度环与手柄操控中，target的突变幅度又是多大呢?
    const float target_scale = fmaxf(fabs_target, target_min_current);
    if (fabsf(target - this->target) > target_scale * target_jump_ratio)
        spin_state = Transit;

    this->target = target;
}
