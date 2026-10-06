#include "robot_action.h"
#include "Control_func.h" // 仅本文件需要访问 Remote_Control 成员，头文件不暴露此依赖

RobotAction::RobotAction(Chassis &chassis_ref,
                         int32_t *move,
                         int32_t *turn)
    : move_axis(move),
      turn_axis(turn),
      chassis(chassis_ref),
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
    // refresh 已 update，此处仅开启新段，本段转角从 0 起，首轮误差即目标本身。
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
    // refresh 已 update，此处仅开启新段，本段里程从 0 起，首轮误差即目标本身。
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

    // refresh 已 update，此处仅开启新段，本段里程与转角均从 0 起。
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
    // 动作层只消费 Chassis 已经换算好的本段起点坐标，不自行积分测量量。
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

float RobotAction::square_map_axis(int v)
{
    // 保留符号的平方：把摇杆小值压低、大值保留，低速更可控。
    // 输入 -127~127，输出 -100~100。
    // 输入平方小于 manual_deadzone 时输出 0；
    // 否则把 [deadzone^2, 127^2] 线性映射到 [0, 100]，再加符号。
    // 平方后的有效范围即 [-(127*127)+deadzone, 127*127-deadzone]。
    int sign = v < 0 ? -1 : 1;
    int v2 = v * v;
    if (v2 < manual_deadzone)
        return 0.0f;
    return (float)sign * (v2 - manual_deadzone) / (16129 - manual_deadzone) * 100;
}

void RobotAction::update_manual(uint32_t now)
{
    // square_map_axis 已把单轴做成低速更细（死区 + 平方映射），输出约 ±100
    float forward = square_map_axis(*move_axis);
    float turn = square_map_axis(*turn_axis);

    // 两杆都回中 → 停车（清积分由 chassis.stop → MyMotorGroup::stop 完成）
    if (forward == 0.0f && turn == 0.0f)
    {
        chassis.stop();
        return;
    }

    // 摇杆合成半径 r ∈ [0, 1]（用原始轴值算，避免 square_map 二次非线性）
    float r = sqrtf(static_cast<float>((*move_axis) * (*move_axis) + (*turn_axis) * (*turn_axis))) / 127.0f;

    float turn_scaled = turn * (1.0f - turn_atten * r);

    float left_cmd = forward + turn_scaled;
    float right_cmd = forward - turn_scaled;

    float max_abs = fmaxf(fabsf(left_cmd), fabsf(right_cmd));
    if (max_abs > 1e-3f)
    {
        float scale = r * 100 / max_abs;
        left_cmd *= scale;
        right_cmd *= scale;
    }
    chassis.output(left_cmd, right_cmd);
}

void RobotAction::refresh()
{
    // 周期统一时间戳（us）
    uint32_t now = get_time_us();

    // 每轮首先更新里程、速度和航向。
    chassis.update(now);

    // 指针为空即空闲。
    if (chassis_task_ptr != nullptr)
        (this->*chassis_task_ptr)(now);
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

void RobotAction::action_finished()
{
    // 自然结束统一收尾：停车、清指针，最后通知回调（此时本动作已收尾，回调可直接发起下一个）。
    chassis.stop();
    chassis_task_ptr = nullptr;
    if (move_end_callback != nullptr)
        move_end_callback();
}

void RobotAction::stop_move()
{
    // 外部主动打断：只停车、清指针，不触发回调。
    chassis.stop();
    chassis_task_ptr = nullptr;
}

void RobotAction::manual()
{
    // 建议外部手动设置only_value状态,避免误触
    chassis_task_ptr = &RobotAction::update_manual;
}
