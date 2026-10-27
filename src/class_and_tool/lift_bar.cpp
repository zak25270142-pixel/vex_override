#include "robot_upper.h"
#include "timer.h" // get_time_us

// 满幅电压用 my_main.h 的 full_volt：VEX 电压模式可填值上限就是电池电压。
// MyMotor 的逐台 volt_max 已废弃，限幅由 SDK 负责，此处只用它做夹幅与
// "指令强度"归一化。

LiftMotorGroup::LiftMotorGroup(MyMotor *motor_list, uint8_t count)
    : motors(motor_list), motor_count(count)
{
}

void LiftMotorGroup::setStopping(vex::brakeType brake)
{
    for (uint8_t i = 0; i < motor_count; i++)
        motors[i].motor->setStopping(brake);
}

void LiftMotorGroup::stop_motors()
{
    for (uint8_t i = 0; i < motor_count; i++)
    {
        motors[i].volt_output = 0.0f;
        motors[i].motor->stop();
    }
}

void LiftMotorGroup::motor_output()
{
    for (uint8_t i = 0; i < motor_count; i++)
    {
        float volt = motors[i].volt_output;
        if (volt > full_volt)
            volt = full_volt;
        else if (volt < -full_volt)
            volt = -full_volt;
        motors[i].volt_output = volt;
        motors[i].motor->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }
}

void LiftMotorGroup::motor_output(float output, float deadzone)
{
    for (uint8_t i = 0; i < motor_count; i++)
    {
        const float base = deadzone + motors[i].volt_min;
        const float controllable = full_volt - base;
        float volt = output * controllable * motors[i].volt_factor;
        if (output > 0.0f)
            volt += base;
        else if (output < 0.0f)
            volt -= base;
        if (volt > full_volt)
            volt = full_volt;
        else if (volt < -full_volt)
            volt = -full_volt;
        motors[i].volt_output = volt;
        motors[i].motor->spin(vex::directionType::fwd, volt, vex::voltageUnits::volt);
    }
}

LiftBar::LiftBar(MyMotor *motor_list, uint8_t count, float p, float i)
    : motor_group(motor_list, count),
      pid(p, i, 0.0f, 1.0f) // 归一输出上限；速度环只用 PI（D 项在静摩擦大的机构上放大粘滑噪声）
{
}

void LiftBar::init()
{
    // brake 是全生命周期设定：本类从不设 coast（没有松力态），手动盘杆靠 brake 的
    // 转速阻尼，不需要放开。
    motor_group.setStopping(vex::brakeType::brake);
    state = Stopped;
    control_ptr = nullptr;
}

void LiftBar::refresh()
{
    // ---------- 数据更新：任何状态都读 ----------
    // 停机手动盘杆时监控要看得到位置；Stall 锁死时也要看卡在哪。
    MyMotor *motors = motor_group.motors;
    const uint8_t motor_count = motor_group.motor_count;
    float sum = 0.0f;
    float sum_left = 0.0f, sum_right = 0.0f;
    float v_sum = 0.0f;
    float i_sum = 0.0f;
    float i_left = 0.0f, i_right = 0.0f;
    float p_min = 0.0f, p_max = 0.0f;
    const uint8_t half = motor_count / 2; // 前半=左组，后半=右组（装配按此顺序接线）
    for (uint8_t i = 0; i < motor_count; i++)
    {
        // 原始 rev 读数：安装反向已由电机 reversed 标志在 VEXos 层面翻转
        // （输出方向和编码器读数一起翻），4 台同号才正常，见 robot_upper.h 决策 4
        const float p_i = static_cast<float>(motors[i].motor->position(vex::rotationUnits::rev));
        sum += p_i;
        if (i < half)
            sum_left += p_i;
        else
            sum_right += p_i;
        v_sum += static_cast<float>(motors[i].motor->velocity(vex::velocityUnits::pct));
        // 带符号电流：正=电机出力，负=被负载拖动（发电制动）。力矩 ∝ 电流，
        // 故左右电流差 = 左右力矩差 = 箱子承受的内力，与结构刚度无关。
        const float i_i = static_cast<float>(motors[i].motor->current(vex::currentUnits::amp));
        i_sum += fabsf(i_i);
        if (i < half)
            i_left += i_i;
        else
            i_right += i_i;
        if (i == 0)
        {
            p_min = p_i;
            p_max = p_i;
        }
        else
        {
            if (p_i < p_min)
                p_min = p_i;
            if (p_i > p_max)
                p_max = p_i;
        }
    }
    pos_prev = position;
    position = sum / motor_count * pos_factor; // 组级单位换算：rev → 杆端
    pos_left = sum_left / half * pos_factor;
    pos_right = sum_right / half * pos_factor;
    tilt = pos_left - pos_right;           // 半刚性结构：这是箱子受剪切力的代理量
    spread = (p_max - p_min) * pos_factor; // 组内啮合差，同口径
    speed = v_sum / motor_count;
    avg_current = i_sum / motor_count;
    cur_left = i_left / half;
    cur_right = i_right / half;
    cur_diff = cur_left - cur_right;

    // ---------- 控制：只跑挂载的指令 ----------
    if (control_ptr != nullptr)
        (this->*control_ptr)();
}

void LiftBar::control_volt()
{
    MyMotor *motors = motor_group.motors;
    const uint8_t motor_count = motor_group.motor_count;
    const uint32_t now_us = get_time_us();

    // 开环：指令即输出，无反馈。output 此处语义 = 归一指令强度
    // （各台 |volt_output|/full_volt 的平均），供卡死判据与监控用。
    float strength = 0.0f;
    for (uint8_t i = 0; i < motor_count; i++)
        strength += fabsf(motors[i].volt_output) / full_volt;
    output = strength / motor_count;
    if (output > 1.0f)
        output = 1.0f;

    if (stall_check(now_us, output >= stall_output))
        return;

    motor_group.motor_output();
}

void LiftBar::control_speed()
{
    const uint32_t now_us = get_time_us();
    if (last_time_us == 0)
        last_time_us = now_us; // 首拍只建基准
    uint32_t dt_us = now_us - last_time_us;
    last_time_us = now_us;
    if (dt_us < 1000) // 与 set_speed 同拍/亚毫秒重叠，防首帧除零
        dt_us = 1000;
    else if (dt_us > 20000)
        dt_us = 20000;
    // PID 按时间戳差算 dt，直接传 now 会因亚毫秒重叠算出接近 0 的 dt 炸 D 项。
    // 传合成时间戳，让 PID 看到的 dt 恒在 [1ms, 20ms]（写法同底盘速度环）。
    const uint32_t pid_now = pid_last_us + dt_us;

    const float err = speed_target - speed; // pct
    if (pid_need_reset)
    {
        pid_need_reset = false;
        output = pid.reset(err, pid_now); // 清积分 + 建基准；返回首拍 P 项输出
    }
    else
    {
        output = pid.update(err, pid_now);
    }
    pid_last_us = pid_now;

    if (output > 1.0f)
        output = 1.0f;
    else if (output < -1.0f)
        output = -1.0f;

    if (stall_check(now_us, fabsf(speed_target) >= stall_speed_min))
        return;

    // Speed 态映射同底盘口径：volt_min 沿输出方向垫底（死区补偿），volt_factor 逐台
    // 对齐出力，满幅夹到 full_volt。结构保护由 SDK 与上层负责。
    motor_group.motor_output(output, deadzone);
}

bool LiftBar::stall_check(uint32_t now_us, bool cmd_wants_move)
{
    // ① "命令明确要动"：Volt 态看指令是否给满（给小电压不动是静摩擦，正常）；
    //    Speed 态看目标速度（目标 0 时速度环顶着负载不动是正常工作，不是堵转）。
    // ② "位置不动"：杆速低时转速维度分不开正常工作与堵死，位置增量是唯一证据。
    // ③ "在顶"：输出顶格或平均电流超限（电流比 40ms 时限快得多，几 ms 就上来）。
    // Volt 态 output 就是归一指令强度（见 control_volt），直接拿来判"给满了没"。
    const bool pos_still = fabsf(position - pos_prev) < stall_pos_eps;
    const bool pushing = fabsf(output) >= stall_output || avg_current >= stall_current;
    const bool past_grace = now_us - run_since_us >= stall_grace;

    if (past_grace && cmd_wants_move && pos_still && pushing)
    {
        if (stall_since_us == 0)
            stall_since_us = now_us;
        else if (now_us - stall_since_us >= stall_time)
        {
            stall_stop();
            return true;
        }
    }
    else
        stall_since_us = 0;

    // ---------- 倾斜超限：半刚性结构的机械保护 ----------
    // 左右之间没有机械同步件，所有左右出力差都变成箱子的剪切/扭转变形。
    // 开环与速度环都只盯平均值、不管 tilt，所以这里是初版唯一能拦住"箱子被
    // 扭坏"的闸。处置与卡死相同：0V+brake + 置 Stall 并摘掉控制指令。
    // 注意：tilt 是相对上电零点的差值，含左右装配零点偏差；阈值要留这部分余量，
    // 做回零（两端挡点）后才能按真实倾斜量收紧。
    if (fabsf(tilt) >= tilt_limit)
    {
        stall_stop();
        return true;
    }
    return false;
}

void LiftBar::stall_stop()
{
    // 卡死：立即 0V+brake + 上报 + 摘掉控制指令。
    // brake 的原理是短路制动（H 桥短接电机端子，转动时反电动势在短路
    // 回路产生电流，电流转矩阻碍转动）——阻力矩与转速成正比，停转即
    // 无力，不是机械锁。VEX Support 官方口径："brake 与编码器无关，被
    // 外力推过之后会停在新位置"；vexide 文档："大力作用下仍能被转动"。
    // 所以 Stall 后重力负载会拖着杆缓慢下滑，不会保持在卡死点。
    // 设计取舍：此刻继续施加力矩就是在拉坏机构，唯一正确的动作是撤掉
    // 主动驱动，让 brake 尽量减慢下沉、上报卡死位置，由挡点/地面接住。
    // 真正"钉住"只有机械自锁（实物待确认）。
    // 另注意：被外力推着转时 brake 把动能耗在电机内阻和 H 桥上
    // （regenerative braking），长时间缓慢下滑有热积累，量级远小于
    // hold 模式 PID 满力顶（VEX 论坛：brake 极少过热，hold 常过热）。
    state = Stall;
    jam_pos = position;
    stall_since_us = 0;
    output = 0.0f;
    control_ptr = nullptr;
    motor_group.stop_motors(); // stopping 恒为 brake（init 设过，本类不设 coast）
}

void LiftBar::set_volt(float v0, float v1, float v2, float v3)
{
    // 电压直给：指令存在各台 volt_output 里，写完立刻下发（同底盘 set_volt_raw）
    motor_group.motors[0].volt_output = v0;
    motor_group.motors[1].volt_output = v1;
    motor_group.motors[2].volt_output = v2;
    motor_group.motors[3].volt_output = v3;
    motor_group.motor_output();
    state = Volt;
    control_ptr = &LiftBar::control_volt;
    stall_since_us = 0;
    run_since_us = get_time_us();
}

void LiftBar::set_speed(float pct)
{
    speed_target = pct;
    state = Speed;
    control_ptr = &LiftBar::control_speed;
    pid_need_reset = true;
    stall_since_us = 0;
    run_since_us = get_time_us();
}

void LiftBar::stop()
{
    state = Stopped;
    control_ptr = nullptr;
    stall_since_us = 0;
    output = 0.0f;
    speed_target = 0.0f;
    motor_group.stop_motors(); // 各台 volt_output 一并清 0，stopping 恒为 brake
}
