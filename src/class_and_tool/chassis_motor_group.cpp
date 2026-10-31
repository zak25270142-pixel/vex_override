#include "chassis.h"
#include "tool.h"

ChassisMotorGroup::ChassisMotorGroup(
    MyMotor *motor_list, uint8_t count, const Vec2f *kf_node,
    uint8_t kf_num, float p, float i, float d)
    : MotorGroup(motor_list, count, kf_node, kf_num, p, i, d)
{
}

// 先调基类做存储与历史留痕（target_jump / target_sign_prev），再按状态机决定起步 / 换向
void ChassisMotorGroup::set_target(float t)
{
    MotorGroup::set_target(t);
    if (spin_state == Stopped)
        set_spin_state(fabs_target < err_out_dead ? Track : Transit);
    else if (fabsf(target_jump) > target_jump_abs && fabsf(target - pre_v) > target_jump_abs)
    {
        integral = 0.0f;// 大跳时清零积分
        set_spin_state(Transit);
    }
    else if (target_sign != 0 && target_sign_prev != 0 && target_sign != target_sign_prev)
        set_spin_state(Transit); // 换向不清积分
}

void ChassisMotorGroup::refresh(uint32_t dt_us, float dt_s)
{
    if (spin_state == Stopped)
        return;
    MotorGroup::refresh(dt_s);
    if (spin_state == Transit)
    {
        if (transit_dur_us == dur_unstart)
            transit_dur_us = 0;
        else
            transit_dur_us += dt_us;
    }
    const float fabs_error = fabsf(error);
    if (spin_state == Transit && (fabs_error <= error_for_i ||
        (transit_dur_us >= transit_freeze_us && fabsf(now_a) <= speed_stable && fabsf(pre_a) <= speed_stable)))
        set_spin_state(Track);
    else if (spin_state == Track && fabs_error <= err_in_dead)
        set_spin_state(Stable);
    else if (spin_state == Stable && fabs_error > err_out_dead)
        set_spin_state(Track);
    // ---------- 积分策略：抗饱和 + 缩放。基类不参与，全在这一层 ----------
    const float delta = error * dt_s * i_scale;

    const float candidate = output_raw + ki * delta;
    const float volt_want = volt_floor + candidate; // 无限幅时想要的电压
    if (spin_state != Transit && (volt_want <= full_volt || error <= 0.0f) && (volt_want >= -full_volt || error >= 0.0f))
    {
        integral += delta;
        output_raw = candidate;
    }

    // ---------- 状态判定 2：非预期运动 / 堵转（要用本拍 output_raw，故排在最后）----------
    // "已给到上限"按实际电机电压（volt_floor + output_raw）判，不能拿 0.99×12V 当裸
    // output_raw 门槛：抗饱和把积分卡在投影 ±full_volt，output_raw 本身到不了 12，
    // 低速重载时永远够不着，Stall 就永远进不去。
    // 又一处"两个天花板必须同一个数"。
    const bool giving_max = fabsf(volt_floor + output_raw) >= full_volt * 0.99f;                  // 输出是否已顶到上限（投影电机电压）
    const bool wheel_low = fabsf(now_v) <= stall_speed;                                          // 轮是否基本没在转
    const bool pulled_away = (target > 0.0f && now_a < 0.0f) || (target < 0.0f && now_a > 0.0f); // 目标与加速度反向

    if (spin_state == Stall)
    {
        // Stall 退出：轮速起来了，或操作手松杆（目标落进死区）；杆收小但没收到底不退出
        if (!wheel_low || fabs_target == 0.0f)
            set_spin_state(Transit);
    }
    else
    {
        // Stall 计时：给满 + 轮速低须持续够久（滤掉起步瞬间）；Transit 期间不计时
        if (giving_max && wheel_low && spin_state != Transit)
        {
            if (stall_dur_us == dur_unstart)
                stall_dur_us = 0; // 条件刚成立那拍：只开表，不记时长
            else
                stall_dur_us += dt_us;
        }
        else
            stall_dur_us = dur_unstart;
        if (stall_dur_us >= stall_time) // 已计入的时长够久；进场拍不计，故用 >=
            set_spin_state(Stall);
        else if (spin_state == Recover)
        {
            // Recover 退出：误差回到 I 工作区且输出不再顶格，走 Transit 重新观察（积分不清零）
            if (fabs_error <= error_for_i && !giving_max)
                set_spin_state(Transit);
        }
        else if (spin_state != Transit && pulled_away &&
                 (fabsf(now_a) > recover_accel || (giving_max && fabsf(now_a) > speed_stable)))
            // Recover 进入：目标与加速度反向且幅度超门槛；前置不在 Transit
            // （那里的 error 是操作手造成的，不是外力）
            set_spin_state(Recover);
    }
}

void ChassisMotorGroup::set_spin_state(SpinState s)
{
    spin_state = s;
    transit_dur_us = dur_unstart;
    i_scale = 1.0f;
    switch (s)
    {
    case Stopped:
        stall_dur_us = dur_unstart;
        break;
    case Stall:
        stall_dur_us = dur_unstart;
        slew_rise = slew_fast;
        slew_fall = slew_slow;
        break;
    case Recover:
        slew_rise = INFINITY;
        slew_fall = INFINITY;
        break;
    case Stable:
        slew_rise = slew_slow;
        slew_fall = slew_fast;
        i_scale = i_stable_k;
        break;
    default:
        slew_rise = slew_slow;
        slew_fall = slew_fast;
        break;
    }
}

void ChassisMotorGroup::stop()
{
    MotorGroup::stop(); // 清积分、输出、各台电压
    set_spin_state(Stopped);
}

void ChassisMotorGroup::apply_output(float request)
{
    MotorGroup::apply_output(slew_step(request, output_applied, slew_rise, slew_fall, dt_s));
}
