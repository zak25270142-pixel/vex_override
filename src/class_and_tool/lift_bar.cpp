#include "robot_upper.h" // LiftBar
#include "tool.h" // linear_at（重力前馈插值）

LiftBar::LiftBar(MyMotor *motor_list, uint8_t count,
                               const Vec2f *kf_node, uint8_t kf_num,
                               const Vec2f *bias_node, uint8_t bias_num,
                               float p, float i, float d,
                               float pos_p, float pos_i, float pos_d)
    : MotorGroup(motor_list, count, kf_node, kf_num, p, i, d),
      pos_pid(pos_p, pos_i, pos_d, max_speed),
      bias_node(bias_node), bias_num(bias_num)
{
}

// 重力偏置插值：x = 位置 raw，y = 该高度悬停所需补充电压 V（四连杆力矩臂随角度变，按高度插值）。
// 算法同 tool.cpp 的 linear_at（升序 x、越界取端点）。
float LiftBar::bias_at(float pos) const
{
    return linear_at(bias_node, bias_num, pos);
}

void LiftBar::init()
{
    setStopping(vex::brakeType::brake); // 全生命期设定；本类不设 coast
    MotorGroup::stop();
    state = Stoped;
    position = static_cast<float>(motors[0].motor->position(vex::rotationUnits::raw));
    pos_target = position;
    pos_need_reset = true;
}

void LiftBar::lift_tick(uint32_t dt_us, float dt_s)
{
    // 位置只在此处读一次；速度由基类 refresh 读。各读各的量，不重复取编码器。
    position = static_cast<float>(motors[0].motor->position(vex::rotationUnits::raw));

    // Stoped：只刷数据、不下发。开环写下的电压因此能一直保持；断电态也不必
    // 每拍重申 0V（0V 由 stop() 那一次表达）。同底盘 speed_tick 的 Stopped 早退。
    // 数据照刷：now_v 等派生量只在 refresh 里更新，不刷则开环扫压看不到转速。
    // 调基类那份只算不下发，积分与状态机都不动（基类 refresh 本就不碰积分）。
    if (state == Stoped)
    {
        MotorGroup::refresh(dt_s);
        return;
    }

    if (state == Stoping)
        run_stop(dt_s);
    else
        run_pos(dt_us, dt_s);
}

void LiftBar::lift_to(float target_raw)
{
    // 软行程限位：目标 clamp 进行程，防上位机发超程目标把机构顶死。
    // 这是预防性的目标限制，不是"顶住了再判断"的堵转判据。
    if (target_raw < lim_min)
        target_raw = lim_min;
    else if (target_raw > lim_max)
        target_raw = lim_max;

    // 唤醒（Stoped / Stoping）或目标大跳：清位置环积分重来；小改平滑追踪。
    if (state == Stoped || state == Stoping || fabsf(target_raw - pos_target) > jump_raw)
        pos_need_reset = true;

    pos_target = target_raw;
    state = Lift; // 具体 Lift / Hold 由下一拍按位置误差判
}

void LiftBar::hold()
{
    lift_to(position); // 保持不单独成命令，等价于"目标取当前位置"
}

// 遮蔽基类 stop()：起回落任务，不立即断电。
void LiftBar::stop()
{
    if (position <= lim_min + stop_eps) // 已在最低点就不必回落
    {
        power_off();
        return;
    }
    arrived = false; // 回落途中不在容差内，别让监控还停在“到位=1”
    state = Stoping;
}

void LiftBar::power_off()
{
    MotorGroup::stop(); // 0V + setStopping 的刹车模式；清积分、输出、各台电压
    state = Stoped;
    pos_target = position;
    arrived = false;
}

void LiftBar::run_pos(uint32_t dt_us, float dt_s)
{
    pos_error = pos_target - position;

    // 进保持按 tol_raw、出保持按 tol_out_raw（迟滞），避免在容差边界上反复切。
    if (fabsf(pos_error) <= tol_raw)
        state = Hold;
    else if (state == Hold && fabsf(pos_error) > tol_out_raw)
        state = Lift;
    arrived = (state == Hold);

    // 位置环用合成时间戳：dt 由 RobotAction 夹在 [1ms, 20ms]，这里不再夹第二次。
    const uint32_t pid_now = pid_last_us + dt_us;
    pos_pid.max_output = max_speed; // 位置环输出就是速度目标，跟着可达满速走
    float speed_req;
    if (pos_need_reset)
    {
        pos_need_reset = false;
        speed_req = pos_pid.reset(pos_error, pid_now);
    }
    else
        speed_req = pos_pid.update(pos_error, pid_now);
    pid_last_us = pid_now;

    spin_to(speed_req, dt_s);
}

void LiftBar::run_stop(float dt_s)
{
    if (position <= lim_min + stop_eps) // 到底：断电
    {
        power_off();
        return;
    }
    // 受控回落：目标为负速度；重力帮忙时速度环自己收住（error = 目标 − 实际）。
    spin_to(stop_speed, dt_s);
}

void LiftBar::spin_to(float speed_req, float dt_s)
{
    MotorGroup::set_target(speed_req);

    // 重力前馈按当前高度算，写进基类偏置参与垫底与 volt_floor 计算。
    // 必须在 refresh 之前写：base_volt / volt_floor 都在 refresh 里用它算。
    gravity_bias = bias_at(position);
    base_bias = gravity_bias;

    MotorGroup::refresh(dt_s);

    // 积分 + 抗饱和：基类 refresh 只算 output_raw、不碰 integral（同 ChassisMotorGroup，
    // 积分策略留在派生层）；投影电机电压顶到 ±full_volt 且误差还朝限外推时本拍不积。
    const float delta = error * dt_s;
    const float candidate = output_raw + ki * delta;
    const float proj = volt_floor + candidate; // 投影电机电压，带符号
    if ((proj <= full_volt || error <= 0.0f) && (proj >= -full_volt || error >= 0.0f))
    {
        integral += delta;
        output_raw = candidate;
    }

    // 重力前馈走 base_bias：基类已保证它不受 target_sign 影响，保持态（指令落进
    // 死区、sign 为 0）也照样顶着——这正是升降杆"不动 = 顶住重力"的语义。
    // 不下压摆：速度环 kp=0.05 V/pct、kf 表空，电压阶跃只有 1V 量级。
    MotorGroup::apply_output(output_raw);
}
