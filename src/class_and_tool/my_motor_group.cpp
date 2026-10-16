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
    // 幂等：已经停着就什么都不做。摇杆回中时上层每拍都会调 stop()，
    // 不挡住的话等于每 10ms 对组内每台电机白下发一次 SDK stop()。
    if (spin_state == Stopped)
        return;
    spin_state = Stopped;
    target = 0.0f;
    integral = 0.0f;
    previous_error = 0.0f;
    target_sign = 0;
    pre_v = 0.0f;
    pre_a = 0.0f;
    transit_start_us = 0;
    stall_since_us = 0;

    // 停车即解除同步：下一轮起步从满力开始重新观察谁掉队，
    // 否则旧车况下压出来的系数会拖慢下一次起步。
    dyn_factor = 1.0f;
    achieve = 1.0f;
    achieve_valid = false;
    limited = false;
    for (uint8_t i = 0; i < motor_count; i++)
    {
        motors[i].volt_output = 0.0f;
        motors[i].motor->stop();
    }
}

// target=0 没有特权：和 ±1 走同一套状态机与 PID，由速度环自己把速度收敛到 0。
// 是否真正停车（硬刹 + 清积分）由外界 stop() 决定，本函数不自己判断。
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
    // 达标率低通系数：由时间常数换算，本组被调用的周期变了不用改参数
    const float alpha = 1.0f - expf(-dt_s * 1000000.0f / achieve_tau_us);

    last_time_us = now_us;

    // 直接读速度，不重复做滤波
    const float speed = static_cast<float>(motors[0].motor->velocity(vex::velocityUnits::pct));

    // ---------- 状态判定 1：目标 / 稳定（不依赖本拍输出，先判给 I 项和达标率用）----------
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

    // ---------- 达标率：本组此刻实际跟上了多少目标转速 ----------
    // 只在稳态跟踪(Track/Stable)且目标足够大时判定。起步窗内速度还没起来、换向瞬间、
    // 打滑/腾空（速度突变会落回 Transit）、以及低速目标，这些期间一律判不准 → 同步冻结。
    // Recover/Stall 也不在 {Track,Stable} 里 → 同步冻结：外力造成的慢不是"这轮天生弱"。
    // 注意：pct 是 float（由 rpm 换算），SDK 底层也已对编码器速度滤过一次，这里的低通
    // 只是让"慢性"的同步判定更稳，不是救量化噪声。
    achieve_valid = (spin_state == Track || spin_state == Stable) && fabs_target > achieve_min_target;
    if (achieve_valid)
    {
        // 比值夹到 [0,1]：被外力反拖时 speed 与 target 反号、比值为负 → 按完全跟不上(0)算；
        // 超速(>1)按满达标算——同步是降级器，只关心"谁跟不上"，超速不需要被奖励
        //（dyn_factor 上限本来就是 1，夹不夹结果一样；夹住只是避免"比满力还强"的怪值）。
        float ratio = fmaxf(0.0f, fminf(speed / target, 1.0f));
        achieve += (ratio - achieve) * alpha;
        achieve = fmaxf(0.0f, fminf(achieve, 1.0f));
    }
    else
        achieve += (1.0f - achieve) * alpha; // 判不准就漂回满力（同步侧会冻结系数，不吃这个值）

    // ---------- 连续摩擦补偿 ----------
    const float friction = dynamic_deadzone + (static_deadzone - dynamic_deadzone) * expf(-fabs_target / 3.0f);

    // ---------- PID ----------
    const float p_output = kp * error;
    const float d_output = kd * derivative;
    const float feedforward = kf * target;

    // ---------- I 项：Transit 不工作；Stable 只缩小新增积分，避免已有 I 输出跳变 ---------
    if (spin_state != Transit)
    {
        float delta = error * dt_s;
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

    // 顶格必须在夹紧**之前**取：夹紧后 output 恒在 [-1,1]，再判断就永远判不到（恒 false）。
    const bool output_saturated = output > 1.0f || output < -1.0f;
    if (output > 1)
        output = 1;
    else if (output < -1)
        output = -1;

    // ---------- 状态判定 2：非预期运动 / 堵转（要用到本拍输出，故排在 PID 之后）----------
    // 三个中间量：
    //   giving_max  归一输出已经顶到上限（我们要给的全给了）
    //   wheel_low   轮胎基本没在转
    //   pulled_away 车在本拍输出想推的方向的反方向上运动 —— 我在推它往这走，它偏往那走
    //   error_grow  车速正被拉离目标（误差在扩大）—— 用来把"外力"和"我们自己在收速"分开：
    //     主动减速（松杆缓冲、位置环收速）时速度是**趋向**目标的，error 与 now_a 同号；
    //     外力带偏时速度是**离开**目标的，两者反号。少了这一条，正常减速也会被判成 Recover，
    //     而一进 Recover 就解除斜率限制、同步又强制满力，反而抖一下、冲一下。
    const bool giving_max = fabsf(output) >= stall_output;
    const bool wheel_low = fabsf(speed) <= stall_speed;
    const bool pulled_away = (output > 0.0f && now_a < 0.0f) || (output < 0.0f && now_a > 0.0f);
    const bool error_grow = error * now_a < 0.0f;

    if (spin_state == Stall)
    {
        // 退出条件刻意不是进入条件的取反：
        //   轮速起来了 —— 外力没了或被我们克服了；
        //   操作手松杆（目标落进死区）—— 他要的是"停"，不是"顶着"，此时该按正常松杆快撤压。
        // 而"把杆收小但没收到底"（80→5）**不退出**：target 变小不代表负载消失，
        // 退出会把顶着的电压一把撤掉，车就被它顶着的东西弹回来。
        // 那种情况留在 Stall 里，靠松劲限速慢慢放，就是操作手要的"缓慢撤退"。
        if (!wheel_low || fabs_target == 0.0f)
        {
            stall_since_us = 0;
            transit_start_us = now_us;
            spin_state = Transit;
        }
    }
    else
    {
        // Stall 计时：必须持续够久。起步那几十毫秒 output 也是满的、轮速也是 0，只能靠时长滤掉。
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
            // Recover 退出：误差回到 I 工作区且输出不再顶格，走 Transit 重新观察。
            // 积分此刻已不可信（它是被外力拖出来的），但只冻结不清零 —— 清零会把车正在
            // 顶着的力矩一起抽掉，正是要防的"被顶飞"。
            if (fabs_error <= error_for_i && !giving_max)
            {
                transit_start_us = now_us;
                spin_state = Transit;
            }
        }
        else if (spin_state != Transit && pulled_away && error_grow &&
                 (fabsf(now_a) > recover_accel || (giving_max && fabsf(now_a) > speed_stable)))
        {
            // Recover 进入：多因素取或 —— 成因不止一种，没有单因素能断言。
            //   (a) 车速被猛地带偏（撞击 / 被抓 / 被推 / 突然打滑）
            //   (b) 已经给满是却在往后退（(a) 的兜底：渐进负载把 I 顶到饱和之后的版本）
            // 前置"不在 Transit"：Transit 里 error 大是操作手造成的，不是外力证据。
            // 前置 error_grow：主动收速时速度是趋向目标的，不是外力证据。
            spin_state = Recover;
        }
    }

    // 顶住：归一输出被裁剪（电压已到本组此刻的上限）或处于 Recover/Stall。**只进监控，同步不读它**。
    // （ki 有抗饱和，output 顶满基本是 kf 叠 kp 造成的。）
    // "稳态永远达不到"不靠它判：那种情况 output 没顶满、limited=false，
    // 靠 achieve < 1 → capability < 1 判，这才是达标率存在的意义。
    limited = output_saturated || (spin_state == Recover) || (spin_state == Stall);

    // ---------- 归一输出映射到各路电压 + 电压斜率限制 ----------
    // 斜率不对称：升压要慢（加压过快会打滑、拉大电流），撤压要快
    // （收油没有打滑风险，电机那端是被动负载，撤压慢等于迟收油、继续推着车走）。
    // Recover：已确认是非预期运动，两方向都不限，让 P、I 立刻给出所需补偿。
    // Stall：唯一例外。被顶住时"松劲"最危险——操作手拨杆会让 kf、kp 一起缩，
    //   而 I 在饱和方向积不进去，合成输出一塌车就被它顶着的东西弹回来。
    //   所以目标电压绝对值比当前小（= 在松劲）时按升压斜率慢慢松；
    //   加压和反向（操作手拨满想脱困）不受限，意图必须实现。
    // 各限幅都是正的绝对电压步长；不能乘带符号 output，否则反向命令的限幅方向会反转。
    const float dv_up = slew_boost * dt_s;
    const float dv_down = slew_release * dt_s;
    for (uint8_t i = 0; i < motor_count; i++)
    {
        const float base_voltage = friction + motors[i].volt_min;
        const float controllable_voltage = motors[i].volt_max - base_voltage;
        // volt_factor 是出厂标定的静态个体差异，dyn_factor 是 Chassis 每拍下发的在线同步，
        // 两者相乘生效：把本组此刻"允许跑到多快"压到全车的短板水平。
        // 乘积夹 1：标定约定 volt_factor ≤1、dyn_factor ≤1，理论上不会越界；夹住只是防标定填错
        //   时算出的电压超过 volt_max——那样实际只发到 volt_max，可 volt_output 记的是超限值，
        //   撤压斜率会从一个虚高的起点往下走（该快撤的时候慢半拍）。
        // 摩擦垫(base_voltage)不参与缩放，否则低速时压不住、同步失效。
        const float factor = fminf(motors[i].volt_factor * dyn_factor, 1.0f);
        float volt = target_sign * base_voltage +
                     output * controllable_voltage * factor;
        const float dv = volt - motors[i].volt_output;
        if (spin_state == Recover)
            ; // 不限
        else if (spin_state == Stall)
        {
            // 用电压绝对值比而不是数值大小比：反向加压时电压数值也在降，但那不是松劲
            const float limit = (fabsf(volt) < fabsf(motors[i].volt_output) ? slew_boost : slew_release) * dt_s;
            if (fabsf(dv) > limit)
                volt = motors[i].volt_output + (dv > 0.0f ? limit : -limit);
        }
        else if (dv > dv_up)
            volt = motors[i].volt_output + dv_up;
        else if (dv < -dv_down)
            volt = motors[i].volt_output - dv_down;
        motors[i].volt_output = volt;
        motors[i].motor->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }
    // 更新历史
    previous_error = error;
    pre_v = speed;
    pre_a = now_a;
}

float MyMotorGroup::dyn_ceil() const
{
    float vf_max = motors[0].volt_factor;
    for (uint8_t i = 1; i < motor_count; i++)
        vf_max = fmaxf(vf_max, motors[i].volt_factor);
    return 1.0f / vf_max;
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

    // ---- 目标事件 ----
    // 大跳：目标动了，而且**新目标离实际速度还差得远**。
    //   只看 |Δtarget| 不够——target 100→5 而车实际只在 1 pct 时，新目标离速度只有 4 pct，
    //   既不该重置积分，撤压方向也不该被保护（撤压本来就允许快）。
    //   操作手 5pct→1pct→−1pct 的缓进缓退两条都不成立，不会被当成事件。
    // 换向：方向反了但幅度很小那也是操作手的明确意图，进 Transit 给斜率保护。
    // 注：不再为 Recover/Stall 开例外——这两个条件已经把"堵转中把杆收小"
    // （100→−5 而车在 1 pct）自动挡掉了，不需要额外的免疫规则。
    const int8_t new_target_sign = target > 0.0f ? 1 : (target < 0.0f ? -1 : 0);
    const bool target_jumped = fabsf(target - this->target) > target_jump_abs &&
                               fabsf(target - pre_v) > target_jump_abs;
    const bool reversed = new_target_sign != 0 && target_sign != 0 && new_target_sign != target_sign;

    if (spin_state == Stopped)
    {
        last_time_us = now_us;
        transit_start_us = now_us;
        spin_state = Transit;
    }
    else if (target_jumped || reversed)
    {
        // 只有"大跳"才清积分。换向但幅度很小（堵转中的缓进缓退）不清——
        // 那笔积分正是车此刻顶着负载的力矩，清掉车就塌下去了。
        if (target_jumped)
            integral = 0.0f;
        transit_start_us = now_us;
        spin_state = Transit;
    }

    // target 落进死区时不改 target_sign：摩擦补偿按上一次的方向继续给，
    // 免得杆在死区边缘抖动时反复翻极性。
    if (new_target_sign != 0)
        target_sign = new_target_sign;

    this->target = target;
}
