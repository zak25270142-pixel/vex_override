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

// 速度环每短周期(5ms)调用一次，由常驻线程循环；追踪 target（pct）并用电压驱动电机。
// 目标为0时输出0V不刹车，制动由外界 stop() 决定。
void MyMotorGroup::my_spin()
{
    if (is_stoped)
        return;
    uint32_t now_us = get_time_us();
    uint32_t dt_us = now_us - last_time_us;
    last_time_us = now_us;

    // SDK 速度约 10ms 更新一档，低速量化台阶约 1pct，先对 pct 反馈做一阶低通再喂 PID，
    // 避免量化噪声在比例项上变成电压抖动（首拍直接用原读数填充，不从 0 拉一条假曲线）。
    const float raw_speed = static_cast<float>(motors[0]->velocity(vex::velocityUnits::pct));
    if (speed_filter_inited)
        filtered_speed += speed_filter_alpha * (raw_speed - filtered_speed);
    else
    {
        filtered_speed = raw_speed;
        speed_filter_inited = true;
    }

    // 目标速度接近0：输出0V惰行，专供目标连续过零的场景。
    // 注意整车“停车”统一走 stop()（brake 制动，再次 drive 时 my_respin 清积分起步），
    // 当前接线链路下停车即 is_stoped=true，正常不会停在本分支。
    if (fabsf(target) <= output_deadzone)
    {
        // 只断电不改变任何环内状态，previous_error 按零目标对齐，重新给目标时无冲击。
        previous_error = -filtered_speed;
        for (uint8_t i = 0; i < 4; i++)
        {
            volt_output[i] = 0.0f;
            prev_volt[i] = 0.0f;
            motors[i]->spin(vex::directionType::fwd, 0.0f, vex::voltageUnits::volt);
        }
        return;
    }

    // 两个量各司其职：rpm 是编码器原生物理量，只用于积分冻结判定；
    // pct 与目标同口径，只用于 PID 误差。
    const float speed = filtered_speed;
    const float speed_rpm = static_cast<float>(velocity());
    const float abs_speed = fabsf(speed_rpm);

    // 目标换向视同重新起步：清积分、清电压历史，避免旧方向积分带着车冲过零点。
    const int8_t new_target_sign = target > 0.0f ? 1 : -1;
    if (target_sign != 0 && new_target_sign != target_sign)
    {
        integral = 0.0f;
        for (uint8_t i = 0; i < 4; i++)
            prev_volt[i] = 0.0f;
    }
    target_sign = new_target_sign;

    // ---------- 连续摩擦补偿：随目标速度从 static 连续过渡到 dynamic ----------
    // 不再用 is_moving 状态机切换：target 小→补偿接近 static（起步需要高电压），
    // target 大→补偿接近 dynamic（高速只需要克服动摩擦），全程无电压跳变。
    // 原来 1.0↔2.5V 的跳变恰好落在位置环末端爬行区（2~5pct），是粘滑振荡主因。
    const float friction = dynamic_deadzone +
                           (static_deadzone - dynamic_deadzone) * expf(-fabsf(target) / 3.0f);

    const float error = target - speed;
    const float derivative = (error - previous_error) / static_cast<float>(dt_us);
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    const float feedforward = kf * target;

    // 组内归一输出上限：电压映射为 摩擦垫 + output·可控电压·volt_factor，
    // 不超过 volt_max 要求 |output| ≤ 1/volt_factor；volt_factor 最大的电机最先到顶，
    // 由它决定整组上限（各台 factor 均为 1 时上限就是 1）。
    float max_volt_factor = volt_factor[0];
    for (uint8_t i = 1; i < 4; i++)
        if (volt_factor[i] > max_volt_factor)
            max_volt_factor = volt_factor[i];
    const float output_limit = 1.0f / max_volt_factor;

    // ---------- 条件积分抗饱和 ----------
    // 先把本轮误差计入候选积分并算出候选输出，饱和判据直接用上面的真实电压饱和点，
    // 与最终输出限幅完全一致，不会出现“电压已被削、积分还在堆”的脱节。
    // 已饱和但误差方向能帮助退出饱和时，仍允许积分（与位置环 PositionPID 同一套规则）。
    // 转速极低（|rpm| < 3，即原 stopped_confirm_speed）时冻结：
    // 起步/换向瞬间车还没动，误差恒为满值，积分若累积会在车动起来的瞬间一次性释放。
    if (abs_speed >= 3.0f)
    {
        const float candidate_integral = integral + error * static_cast<float>(dt_us);
        const float candidate_output = feedforward + p_output + ki * candidate_integral + d_output;
        if (fabsf(candidate_output) <= output_limit ||
            (candidate_output > output_limit && error < 0.0f) ||
            (candidate_output < -output_limit && error > 0.0f))
        {
            integral = candidate_integral;
        }
        // 积分绝对上限：积分单项不超过归一输出上限，轮子悬空/堵转时积分不会无限堆。
        if (fabsf(ki) > 1e-9f)
        {
            const float max_integral = output_limit / ki;
            if (integral > max_integral)
                integral = max_integral;
            else if (integral < -max_integral)
                integral = -max_integral;
        }
    }

    float output = feedforward + p_output + ki * integral + d_output;
    if (output > output_limit)
        output = output_limit;
    else if (output < -output_limit)
        output = -output_limit;

    // ---------- 归一输出映射到四路电压：连续摩擦垫 + 双极性修正 + 电压斜率限制 ----------
    // 摩擦垫只认目标方向，追踪中误差过零、output 变号都不翻转它：
    // 旧映射在 output 过零时电压直接从 +(摩擦+volt_min) 跳到 -(摩擦+volt_min)，
    // 低速稳态 output 就在 0 附近，于是控制器在正负摩擦电压间来回反打（粘滑根因）。
    // output 现在是叠加在摩擦垫上的双极性连续量：变负时先减小正向电压、再降到 0V、
    // 之后才进入反向制动，摩擦电压附近的精细调节因此连续可调。
    // 电压斜率限制：起步/换向时电压不能阶跃，只能以 slew_rate 爬升，
    // 车被“推起来”而不是“被踢出去”。
    const float max_dv = slew_rate * static_cast<float>(dt_us) / 1000000.0f;
    for (uint8_t i = 0; i < 4; i++)
    {
        const float base_voltage = friction + volt_min[i];
        const float controllable_voltage = volt_max[i] - base_voltage;
        float volt = target_sign * base_voltage +
                     output * controllable_voltage * volt_factor[i];
        // factor 被误调到 >1 等异常情况下保证不超硬件电压上限。
        if (volt > volt_max[i])
            volt = volt_max[i];
        else if (volt < -volt_max[i])
            volt = -volt_max[i];

        // 斜率限制：本拍电压变化不超过 slew_rate × dt
        if (volt > prev_volt[i] + max_dv)
            volt = prev_volt[i] + max_dv;
        else if (volt < prev_volt[i] - max_dv)
            volt = prev_volt[i] - max_dv;
        prev_volt[i] = volt;

        volt_output[i] = volt;
        motors[i]->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }

    previous_error = error;
}
void MyMotorGroup::my_respin()
{
    is_stoped = false;
    integral = 0.0f;
    previous_error = 0.0f;
    target_sign = 0;
    for (uint8_t i = 0; i < 4; i++)
        prev_volt[i] = 0.0f;
    speed_filter_inited = false;
    last_time_us = get_time_us();
}

void MyMotorGroup::drive(float target)
{
    // 停车后首次给目标：先清积分和运动状态再起步，避免上次残留积分造成冲击；
    // 运行中再次调用只改目标，不清任何环内状态。
    if (is_stoped)
        my_respin();
    this->target = target;
}
