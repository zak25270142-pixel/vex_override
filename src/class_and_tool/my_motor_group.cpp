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

// 目标入口（基础版）：只做存储与历史留痕，不下判断。
// 死区归零留在这里，是为了让 target / fabs_target / target_sign / target_jump 始终自洽——
// 它们都是上游直接用的量，若交给上层去改，必然出现"几个里有一个还是旧值"的窗口，
void MotorGroup::set_target(float t)
{
    fabs_target = fabsf(t);
    if (fabs_target <= target_deadzone)
    {
        t = 0.0f;
        fabs_target = 0.0f;
    }
    target_sign_prev = target_sign;
    target_sign = t > 0.0f ? 1 : (t < 0.0f ? -1 : 0);
    target_jump = t - target;
    target = t;
}

// 每拍刷新：采样 + PID 前置 + 算 output_raw。写 now_v/now_a/error/derivative/
// base_volt/feedforward/headroom/output_raw，并把本拍采样提交为 pre_v/pre_a（"上一拍"）。
// 任何读数的地方只在这一个函数里——状态判定与控制律因此必然用同一个采样点，
// 不会出现"判据读一次编码器、控制律再读一次"的两个样本（这种错只看代码看不出来）。
// dt_s 由调用者给：全机只取一次时钟、只夹取一次，本函数不再自己算。
void MotorGroup::refresh(float dt_s)
{
    this->dt_s = dt_s;

    now_v = static_cast<float>(motors[0].motor->velocity(vex::velocityUnits::pct));
    now_a = (now_v - pre_v) / dt_s;

    pre_v = now_v;
    pre_a = now_a;

    // PID 前置：本拍控制律要用的一切，一次算齐存字段
    error = target - now_v;
    derivative = (error - previous_error) / dt_s;
    previous_error = error; // 谁读它算 D，谁就写它——都在本函数里，不跨函数

    // 组级垫底：摩擦（静→动指数过渡，|target| 越大越靠近 dynamic）+ 上层静态偏置
    base_volt = dynamic_deadzone + (static_deadzone - dynamic_deadzone) * expf(-fabs_target / 3.0f) + base_bias;
    feedforward = kf_at(target); // V，已含符号（表覆盖负半轴）
    headroom = full_volt - base_volt - volt_min_max;

    // 原始输出值
    output_raw = feedforward + kp * error + ki * integral + kd * derivative;
}

// 执行：一份算好的可调电压 → 各台电压并下发。
void MotorGroup::apply_output(float volt_adjust)
{
    output_applied = volt_adjust;

    // 垫底不进可调段：它是摩擦补偿量（≈1~1.5V，正落在破静摩擦门槛附近），不是驱动力，
    // 翻符号只是换工作点。换向那拍它让幅值变小，属于"撤压"，而撤压本来就允许快。
    const float pad = static_cast<float>(target_sign) * base_volt;
    for (uint8_t i = 0; i < motor_count; i++)
    {
        const float volt = pad + static_cast<float>(target_sign) * motors[i].volt_min + volt_adjust * motors[i].volt_factor;
        motors[i].volt_output = volt;
        motors[i].motor->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }
}

// 不算摩擦垫电机基本属性的纯电压设置
void MotorGroup::set_volt(const float *volt)
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
    target_sign_prev = 0;
    fabs_target = 0.0f;
    target_jump = 0.0f;
    integral = 0.0f;
    output_raw = 0.0f;
    output_applied = 0.0f; // 压摆锚点也归零：下次起步从 0 开始爬
    previous_error = 0.0f;
    pre_v = 0.0f;
    pre_a = 0.0f;
    for (uint8_t i = 0; i < motor_count; i++)
    {
        motors[i].volt_output = 0.0f;
        motors[i].motor->stop();
    }
}
