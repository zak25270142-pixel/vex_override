#include "robot_action.h"
#include "Control_func.h" // 遥控输入源头文件；本文件只经构造函数注入的 int32_t* 使用其数据

RobotAction::RobotAction(Chassis &chassis_ref,
                         LiftBar &lift_ref,
                         int32_t *move,
                         int32_t *turn)
    : move_axis(move),
      turn_axis(turn),
      chassis(chassis_ref),
      lift(lift_ref),
      // 直线段同时使用距离 PID 推进、航向 PID 差速纠偏；转向段只使用 turn_pid。
      // ki、kd 按秒计（kp 无量纲）：(kp, ki, kd[, output_limit])
      distance_pid(130.0f, 35.0f, 18.0f),
      heading_pid(1.10f, 0.1f, 0.04f, 25.0f),
      turn_pid(0.75f, 0.1f, 0.06f),
      // 误差与实际速度连续满足条件 180 ms 后才结束当前步骤，避免刚经过目标就切换。
      stable_judge(180)
{
}

void RobotAction::init()
{
    chassis.init();
    lift.init();
}

bool RobotAction::is_ready()
{
    return chassis.is_ready();
}

void RobotAction::finish_init()
{
    chassis.finish_init();
}

// ---- 原地旋转 ----

void RobotAction::start_turn(uint32_t now)
{
    // 开启新段：本段转角从 0 起，首轮误差即目标本身
    chassis.begin_segment();
    float output = turn_pid.reset(turn_target, now);
    chassis.output(output, -output);

    // 首轮完成，接力到后续轮。
    chassis_task_ptr = &RobotAction::update_turn;
}

void RobotAction::update_turn(uint32_t now)
{
    // 目标角减去本段已经转过的角度，得到本轮剩余误差；左右轮反向输出。
    float error = turn_target - chassis.heading_from_reset;
    float output = turn_pid.update(error, now);
    chassis.output(output, -output);
    bool finish_angle = fabsf(error) <= angle_tolerance;
    bool finish_w = fabsf(chassis.angular_speed) <= angular_speed_tolerance;
    if (stable_judge.update(finish_angle && finish_w, now))
        action_finished();
}

// ---- 直线行驶 ----

void RobotAction::start_move(uint32_t now)
{
    // 开启新段：本段里程从 0 起，首轮误差即目标本身
    chassis.begin_segment();
    float output = distance_pid.reset(move_target, now);
    heading_pid.reset(0.0f, now);
    chassis.output(output, output);

    // 首轮完成，接力到后续轮。
    chassis_task_ptr = &RobotAction::update_move;
}

void RobotAction::update_move(uint32_t now)
{
    // 距离 PID 决定共同前进量，航向 PID 产生左右差值保持直线。
    float distance_error = move_target - chassis.distance_from_reset;
    float heading_error = -chassis.heading_from_reset;

    float forward = distance_pid.update(distance_error, now);
    float correction = heading_pid.update(heading_error, now);
    chassis.output(forward + correction, forward - correction);

    bool finished = stable_judge.update(
        fabsf(distance_error) <= distance_tolerance &&
            fabsf(chassis.linear_speed) <= linear_speed_tolerance &&
            fabsf(heading_error) <= heading_tolerance &&
            fabsf(chassis.angular_speed) <= angular_speed_tolerance,
        now);

    if (finished)
        action_finished();
}

// ---- 平滑 goto ----

void RobotAction::start_goto(uint32_t now)
{
    // 首轮误差即目标本身。
    float distance_error = sqrtf(goto_target.x * goto_target.x +
                                 goto_target.y * goto_target.y);

    // 开启新段：本段里程与转角均从 0 起
    chassis.begin_segment();

    // 本段航向为 0：车体坐标下的前后误差就是 x，瞄准角即 atan2(y, x)。
    float angle_error = remainderf(goto_target.heading, 360.0f);
    if (distance_error > distance_tolerance)
        angle_error = atan2f(goto_target.y, goto_target.x) * rad_to_deg;

    float forward = distance_pid.reset(goto_target.x, now);
    float turn = turn_pid.reset(angle_error, now);

    if (distance_error > distance_tolerance)
    {
        // 基础版不倒车：目标位于车头后方时首轮只转向。
        if (goto_target.x < 0.0f)
            forward = 0.0f;
        chassis.output(forward + turn, forward - turn);
    }
    else
        chassis.output(turn, -turn);

    // 首轮完成，接力到后续轮。
    chassis_task_ptr = &RobotAction::update_goto;
}

void RobotAction::update_goto(uint32_t now)
{
    // 每轮重新瞄准固定目标点，距离 PID 与转向 PID 同时输出，边走边转。
    float x_error = goto_target.x - chassis.distance_from_reset;
    float y_error = goto_target.y - chassis.side_distance_from_reset;
    float distance_error = sqrtf(x_error * x_error + y_error * y_error);
    float heading_error = remainderf(goto_target.heading - chassis.heading_from_reset, 360.0f);

    // 将目标向量旋转到当前车体坐标；距离 PID 使用有方向的前后误差。
    float heading_rad = chassis.heading_from_reset * deg_to_rad;
    float cos_heading = cosf(heading_rad);
    float sin_heading = sinf(heading_rad);
    float forward_error = x_error * cos_heading + y_error * sin_heading;
    float side_error = -x_error * sin_heading + y_error * cos_heading;

    // 未到目标点时追踪目标点方向；进入距离容差后改为纠正最终朝向。
    float angle_error = heading_error;
    if (distance_error > distance_tolerance)
        angle_error = atan2f(side_error, forward_error) * rad_to_deg;

    float forward = distance_pid.update(forward_error, now);
    float turn = turn_pid.update(angle_error, now);

    if (distance_error > distance_tolerance)
    {
        // 基础版不倒车：目标位于当前车头后方时，只保留转向输出。
        if (forward_error < 0.0f)
            forward = 0.0f;
        chassis.output(forward + turn, forward - turn);
    }
    else
        chassis.output(turn, -turn);

    bool finished = stable_judge.update(
        distance_error <= distance_tolerance &&
            fabsf(chassis.linear_speed) <= linear_speed_tolerance &&
            fabsf(heading_error) <= angle_tolerance &&
            fabsf(chassis.angular_speed) <= angular_speed_tolerance,
        now);

    if (finished)
        action_finished();
}

// ---- 手动控制（驾驶员开环）----
void RobotAction::update_manual(uint32_t now)
{
    // 摇杆值开头一次读入，避免混用两拍的值
    int32_t mv = *move_axis;
    int32_t tn = *turn_axis;
    int32_t mv2tn2 = mv * mv + tn * tn;

    if (mv2tn2 > manual_deadzone)
    {
        stop_start_us = 0; // 推杆即中止停止通道，下次松杆重新计时

        // 方向→半径（k=轮距/(2C)）：半径只随方向，与杆量无关
        float k = chassis.track_width / (2.0f * turn_radius_45);
        float left_cmd = mv + k * tn;
        float right_cmd = mv - k * tn;

        // 杆量→外侧轮速度：杆量² 从死区到 127² 线性映射（小值压低，低速更可控）
        float speed = (mv2tn2 - manual_deadzone) / (127.0f * 127.0f - manual_deadzone) * 100.0f;
        float scale = speed / fmaxf(fabsf(left_cmd), fabsf(right_cmd));
        chassis.output(left_cmd * scale, right_cmd * scale);
        return;
    }
    // 两侧都停了就直接待命，不必再走停止通道
    if (chassis.left_motors.is_stopped() && chassis.right_motors.is_stopped())
        return;
    // 松杆：走停止通道（0V 减速→转速低于阈值或到 manual_stop_delay 硬刹），刹完留在 manual 待命
    if (stop_start_us == 0)
        stop_time_us = manual_stop_delay; // 发起：只设时限，起点由 stop_tick 首拍记
    stop_tick(now);
}

void RobotAction::refresh()
{
    // 周期统一时间戳（us）
    uint32_t now = get_time_us();
    // 先更新里程、速度和航向，再推进当前任务，最后跑速度环
    chassis.update(now);
    // 指针为空即空闲。
    if (chassis_task_ptr != nullptr)
        (this->*chassis_task_ptr)(now);
    chassis.speed_tick(); // 任务计算完下达完后，更新速度环
    lift.refresh();       // 升降杆（与底盘同拍；数据恒刷，控制指令按挂载情况运行）
}

void RobotAction::turn(float angle)
{
    // 已在容差内：不起步，按“瞬间完成”走正常收尾（触发回调）。
    if (fabsf(angle) <= angle_tolerance)
    {
        action_finished();
        return;
    }

    turn_target = angle;
    chassis_task_ptr = &RobotAction::start_turn;
}

void RobotAction::move(float distance)
{
    // 已在容差内：不起步，按“瞬间完成”走正常收尾（触发回调）。
    if (fabsf(distance) <= distance_tolerance)
    {
        action_finished();
        return;
    }

    move_target = distance;
    chassis_task_ptr = &RobotAction::start_move;
}

void RobotAction::goto_local(float x, float y, float heading)
{
    // 点与最终朝向均已达标：不起步，按“瞬间完成”走正常收尾（触发回调）。
    float distance_error = sqrtf(x * x + y * y);
    if (distance_error <= distance_tolerance &&
        fabsf(remainderf(heading, 360.0f)) <= angle_tolerance)
    {
        action_finished();
        return;
    }

    // 目标以发布时刻位姿为起点保存，运行中不再改写，随时可查原始目标。
    goto_target = {x, y, heading};
    chassis_task_ptr = &RobotAction::start_goto;
}

// ---- 停止通道（manual 松杆内联调用；外部缓停把任务指针直接绑到本函数）----
void RobotAction::stop_tick(uint32_t now)
{
    if (stop_start_us == 0) // 以stop_start_us = 0 作为没开始停车的标志
    {
        if (now == 0)
            now++;
        stop_start_us = now;
    }
    // 双判据先到先刹：两组实际转速都低于阈值（近零才刹，不点头），或时限到（兜底）
    if ((fabsf(chassis.left_motors.pre_v) <= stop_speed_th &&
         fabsf(chassis.right_motors.pre_v) <= stop_speed_th) ||
        now - stop_start_us >= stop_time_us)
    {
        chassis.stop();
        stop_start_us = 0;
        if (chassis_task_ptr == &RobotAction::stop_tick)
            chassis_task_ptr = nullptr;
    }
    chassis.output(0, 0); // 0V：target=0 时前馈与摩擦垫都归零，P 项即主动制动力；刹完那拍是 no-op（drive 0pct 守卫）
}

void RobotAction::action_finished()
{
    // 自然结束统一收尾：急停放倒（完成判据已要求近零速），清指针，最后通知回调
    stop_move(0);
    if (move_end_callback != nullptr)
        move_end_callback();
}

void RobotAction::stop_move(uint32_t time)
{
    if (time == 0)
    {
        // 急停：立即硬刹，不等减速
        chassis.stop();
        chassis_task_ptr = nullptr;
        stop_start_us = 0;
        return;
    }
    if (chassis_task_ptr == &RobotAction::stop_tick)
        return; // 已在缓停，不重置计时
    stop_time_us = time;
    stop_start_us = 0;
    chassis_task_ptr = &RobotAction::stop_tick; // 直接绑本函数，无需 wrapper
}

void RobotAction::manual()
{
    chassis_task_ptr = &RobotAction::update_manual;
    stop_start_us = 0;
}

// ---- 上位机构：升降杆（薄转发，判据与状态机在 LiftBar）----
void RobotAction::lift_volt(float v0, float v1, float v2, float v3)
{
    lift.set_volt(v0, v1, v2, v3);
}

void RobotAction::lift_speed(float pct)
{
    lift.set_speed(pct);
}

void RobotAction::lift_stop()
{
    lift.stop();
}
