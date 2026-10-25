#include "my_motor_group.h"

MotorGroup::MotorGroup(MyMotor *motor_list, uint8_t count,
                       const Vec2f *kf_node, uint8_t kf_num,
                       float p, float i, float d)
{
    this->motors = motor_list;
    this->kf_node = kf_node;
    this->kf_num = kf_num;
    motor_count = count;
    kp = p;
    ki = i;
    kd = d;

    // 组内最大的个体垫底电压：比例限幅的分母要用它（见 headroom）。
    for (uint8_t i = 0; i < motor_count; i++)
        if (motors[i].volt_min > volt_min_max)
            volt_min_max = motors[i].volt_min;
}

// 前馈插值：带符号 target 落在哪两节点之间就按距离加权。
// 表的 x 覆盖 -100..100（对称机构填成奇对称），因此上升与下降可以给出
float MotorGroup::kf_at(float target) const
{
    if (target <= kf_node[0].x)
        return kf_node[0].y;
    for (uint8_t i = 1; i < kf_num; i++)
    {
        if (target <= kf_node[i].x)
        {
            const float t = (target - kf_node[i - 1].x) / (kf_node[i].x - kf_node[i - 1].x);
            return kf_node[i - 1].y + (kf_node[i].y - kf_node[i - 1].y) * t;
        }
    }
    return kf_node[kf_num - 1].y;
}

void MotorGroup::setStopping(vex::brakeType brake)
{
    for (uint8_t i = 0; i < motor_count; i++)
        motors[i].motor->setStopping(brake);
}

// 静 → 动的指数过渡：|target| 越大越靠近 dynamic（只补动摩擦）
float MotorGroup::friction_volt() const
{
    return dynamic_deadzone + (static_deadzone - dynamic_deadzone) * expf(-fabs_target / 3.0f);
}

// 目标入口（基础版）：只做存储，不下判断。
// 死区归零留在这里，是为了让 target / fabs_target / target_sign 三者始终自洽——
// 它们都是 apply_output 直接用的量，若交给上层去改，必然出现"三个里有一个还是旧值"
// 的窗口，而那种窗口只在换向那一拍出现，测不出来。
void MotorGroup::set_target(float t)
{
    fabs_target = fabsf(t);
    if (fabs_target <= target_deadzone)
    {
        t = 0.0f;
        fabs_target = 0.0f;
    }
    target_sign = t > 0.0f ? 1 : (t < 0.0f ? -1 : 0);
    target = t;
}

// 基础计算：前馈 + P + I + D → output。**不碰 integral**——积分策略（抗饱和、
// i 缩放、误差带、增速上限）在调用方，因为只有它知道这段积分最后有没有给出去。
// 末尾夹 ±12 是最后一道物理上限；真正起作用的是调用方按 headroom 做的比例限幅。
void MotorGroup::calc_output()
{
    float out = feedforward + kp * error + ki * integral + kd * derivative;
    if (out > full_volt)
        out = full_volt;
    else if (out < -full_volt)
        out = -full_volt;
    output = out;
}

// 每拍刷新：采样 + PID 前置。写 now_v/now_a/dt_s/error/derivative/base_volt/
// feedforward/headroom，并把上一拍值提交到 pre_v/pre_a。
// 任何读数的地方只在这一个函数里——状态判定与控制律因此必然用同一个采样点，
// 不会出现"判据读一次编码器、控制律再读一次"的两个样本（这种错只看代码看不出来）。
void MotorGroup::refresh()
{
    const uint32_t now_us = get_time_us();
    uint32_t gap_us = now_us - last_time_us;
    if (gap_us < 1000) // set_target 与 refresh 同拍/亚毫秒重叠时防首帧除零
        gap_us = 1000;
    else if (gap_us > 20000) // 掉帧：别把一整个卡顿算成 dt，否则 P/D/积分全被放大
        gap_us = 20000;
    last_time_us = now_us; // 记时刻用真实读数，不用夹过的 gap（否则误差会累积）
    dt_s = static_cast<float>(gap_us) / 1000000.0f;

    now_v = static_cast<float>(motors[0].motor->velocity(vex::velocityUnits::pct));
    now_a = (now_v - pre_v) / dt_s; // 与上一拍之差；dt 已夹在 [1ms, 20ms]

    pre_v = now_v;
    pre_a = now_a;

    // ---------- PID 前置：本拍控制律要用的一切，一次算齐存字段 ----------
    // 这几行都只是 (target, now_v, 静态参数) 的函数。放在这里而不是控制律里，
    // 是因为状态机也要用 error / headroom：两边各算一遍，迟早出现两个样本、两个数。
    error = target - now_v;
    derivative = (error - previous_error) / dt_s;
    previous_error = error; // 谁读它算 D，谁就写它——都在本函数里，不跨函数

    base_volt = friction_volt() + base_bias; // 组级垫底，V
    feedforward = kf_at(target);             // V，已含符号（表覆盖负半轴）
    headroom = full_volt - base_volt - volt_min_max;
}

// 执行：可调电压 → 各台电压并下发（末尾按 slew_boost / slew_release 限速）
void MotorGroup::apply_output(float scale)
{
    // 两方向斜率都 ≤0 = 电压钉死：各台保持上一次指令，本拍什么都不写。
    // 提前返回顺带避开 0 当除数——下面换向分支要算 |volt_output| / rate_fall。
    if (slew_boost <= 0.0f && slew_release <= 0.0f)
        return;

    // 换向以 0 为界分段限速、额度按时间分，思路见 docs/代码补充注释/01
    const float rate_rise = slew_boost;   // 幅值增大方向的速率，V/s
    const float rate_fall = slew_release; // 幅值减小方向的速率，V/s

    // 垫底按目标方向：停止时 target_sign=0，电压归零（不会残存垫底电压）
    const float pad = static_cast<float>(target_sign) * base_volt;
    const float o = output / scale; // 限幅后的可调电压；scale=1 时就是 output

    for (uint8_t i = 0; i < motor_count; i++)
    {
        float volt = pad + static_cast<float>(target_sign) * motors[i].volt_min +
                     o * motors[i].volt_factor;
        const float volt_output = motors[i].volt_output;
        if (volt * volt_output < 0.0f) // 过零换向：撤到 0 之前升压速率不生效
        {
            const float t_fall = fabsf(volt_output) / rate_fall; // 撤到 0 要花的时间，s
            if (t_fall >= dt_s)
                volt = volt_output - (volt_output > 0.0f ? rate_fall : -rate_fall) * dt_s;
            else
            {
                const float rise = rate_rise * (dt_s - t_fall); // 剩下的时间够升多少，V
                volt = volt > 0.0f ? fminf(volt, rise) : fmaxf(volt, -rise);
            }
        }
        else
        {
            const float limit = (fabsf(volt) > fabsf(volt_output) ? rate_rise : rate_fall) * dt_s;
            const float dv = volt - volt_output;
            if (fabsf(dv) > limit)
                volt = volt_output + (dv > 0.0f ? limit : -limit);
        }
        motors[i].volt_output = volt;
        motors[i].motor->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }
}

void MotorGroup::output_raw(const float *volt)
{
    for (uint8_t i = 0; i < motor_count; i++)
    {
        motors[i].volt_output = volt[i];
        motors[i].motor->spin(vex::directionType::fwd, volt[i], vex::voltageUnits::volt);
    }
}

void MotorGroup::stop()
{
    target = 0.0f;
    target_sign = 0;
    fabs_target = 0.0f;
    integral = 0.0f;
    output = 0.0f;
    // previous_error / derivative 不清：它们是"上一拍实测的历史"，属于 refresh。
    // 在这里清零等于伪造一次巨大的误差跳变——下一拍 derivative = (error − 0)/dt
    // 会顶到几千 pct/s，D 项（还没启用）一旦打开就是一脚踢。
    // pre_v / pre_a 不清：它们是"上一拍实测"，属于 refresh 的历史记录。
    // 在这里清零等于伪造一次减速——下一拍 now_a = (now_v − 0)/dt 会顶到上千 pct/s，
    // 正好撞进六态机的 Recover/Stall 加速度判据。
    for (uint8_t i = 0; i < motor_count; i++)
    {
        motors[i].volt_output = 0.0f;
        motors[i].motor->stop();
    }
}
