#include "robot_action.h"
#include "Control_func.h" // 仅本文件需要访问 Remote_Control 成员，头文件不暴露此依赖

RobotAction::RobotAction(Chassis &chassis_ref,
                         int32_t *move,
                         int32_t *side,
                         int32_t *turn)
    : move_axis(move),
      side_axis(side),
      turn_axis(turn),
      chassis(chassis_ref),
      // 三通道各自一个 PID，输出分别进混控的 forward/side/turn。
      // ki、kd 按秒计（kp 无量纲）：(kp, ki, kd[, output_limit])
      distance_pid(130.0f, 35.0f, 18.0f),
      side_pid(130.0f, 35.0f, 18.0f),
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
    chassis.output(0.0f, 0.0f, output);

    // 首轮完成，接力到后续轮。
    chassis_task_ptr = &RobotAction::update_turn;
}

void RobotAction::update_turn(uint32_t now)
{
    // 目标角减去本段已经转过的角度，得到本轮剩余误差；只走转向通道。
    float error = turn_target - chassis.heading_from_reset;
    float output = turn_pid.update(error, now);
    chassis.output(0.0f, 0.0f, output);
    bool finish_angle = fabsf(error) <= angle_tolerance;
    bool finish_w = fabsf(chassis.angular_speed) <= angular_speed_tolerance;
    if (stable_judge.update(finish_angle && finish_w, now))
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

    // 全向底盘不需要"先转向对准目标点"：平移直接按误差分解到前/侧两个通道，
    // 转向通道从头到尾只跟踪最终朝向。三通道并行，比差速版少一个动作阶段。
    float forward = distance_pid.reset(goto_target.x, now);
    float side = side_pid.reset(goto_target.y, now);
    float turn = turn_pid.reset(remainderf(goto_target.heading, 360.0f), now);

    if (distance_error > distance_tolerance)
        chassis.output(forward, side, turn);
    else
        chassis.output(0.0f, 0.0f, turn);

    // 首轮完成，接力到后续轮。
    chassis_task_ptr = &RobotAction::update_goto;
}

void RobotAction::update_goto(uint32_t now)
{
    // 每轮把当前车体坐标下的目标误差直接喂给三个通道：前后、左右、转向。
    // 动作层只消费 Chassis 已经换算好的本段起点坐标，不自行积分测量量。
    float x_error = goto_target.x - chassis.distance_from_reset;
    float y_error = goto_target.y - chassis.side_distance_from_reset;
    float distance_error = sqrtf(x_error * x_error + y_error * y_error);
    float heading_error = remainderf(goto_target.heading - chassis.heading_from_reset, 360.0f);

    // 目标点是在发布时刻的车体系里记的，车头转了多少，误差向量就跟着转多少。
    float heading_rad = chassis.heading_from_reset * deg_to_rad;
    float cos_heading = cosf(heading_rad);
    float sin_heading = sinf(heading_rad);
    float forward_error = x_error * cos_heading + y_error * sin_heading;
    float side_error = -x_error * sin_heading + y_error * cos_heading;

    float forward = distance_pid.update(forward_error, now);
    float side = side_pid.update(side_error, now);
    float turn = turn_pid.update(heading_error, now);

    if (distance_error > distance_tolerance)
        chassis.output(forward, side, turn);
    else
        chassis.output(0.0f, 0.0f, turn);

    bool finished = stable_judge.update(
        distance_error <= distance_tolerance &&
            fabsf(chassis.linear_speed) <= linear_speed_tolerance &&
            fabsf(chassis.side_speed) <= linear_speed_tolerance &&
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
    // 输入平方小于 manual_deadzone（单位 pct²，16 即 |v|<4）时输出 0；
    // 否则把 [manual_deadzone, 127²] 线性映射到 [0, 100]，再加符号。
    int sign = v < 0 ? -1 : 1;
    int v2 = v * v;
    if (v2 < manual_deadzone)
        return 0.0f;
    return (float)sign * (v2 - manual_deadzone) / (16129 - manual_deadzone) * 100;
}

/** 把 v 朝 0 收 step（绝对值减法，保号），跨过 0 就直接归零 */
static float approach_zero(float v, float step)
{
    if (v > step)
        return v - step;
    if (v < -step)
        return v + step;
    return 0.0f;
}

void RobotAction::update_manual(uint32_t now)
{
    // square_map_axis 已把单轴做成低速更细（死区 + 平方映射），输出约 ±100
    float forward = square_map_axis(*move_axis); // 左杆 Y：前后
    float side = square_map_axis(*side_axis);    // 左杆 X：左右平移
    float turn = square_map_axis(*turn_axis);    // 右杆 X：转向

    if (forward != 0.0f || side != 0.0f || turn != 0.0f)
    {
        // 有输入：正常输出，并记住本拍的平移指令——松杆后要按它做减速缓冲
        manual_forward = forward;
        manual_side = side;
        manual_prev_us = now;
        chassis.output(forward, side, turn_hold(turn, now));
        return;
    }

    // ---- 松杆减速缓冲 ----
    // 这里不能直接 stop()：那是 brake 硬刹，惯性会把整车压得前倾（old_car 手柄测试实测）。
    // 位置环没这问题，因为它本来就按误差渐变减速。所以手动也要先把平移指令按固定斜率收到 0，
    // 让减速过程被斜率限住；收完速度已经很低，再 stop() 就不会点头了。
    // 缓冲期间转向通道继续由航向保持接管，减速过程中姿态仍然不歪。
    float dt_s = static_cast<float>(now - manual_prev_us) / 1000000.0f;
    manual_prev_us = now;
    if (dt_s <= 0.0f)
        dt_s = 0.01f; // 首个松杆拍（或时间戳未初始化）按一拍算，避免除出 0 步长

    const float step = manual_decay_per_s * dt_s;
    manual_forward = approach_zero(manual_forward, step);
    manual_side = approach_zero(manual_side, step);
    if (manual_forward == 0.0f && manual_side == 0.0f)
    {
        chassis.stop(); // 指令已收到 0，实际速度也跟下来了，此时刹车不会点头
        clear_manual_state();
        return;
    }
    chassis.output(manual_forward, manual_side, turn_hold(0.0f, now));
}

float RobotAction::turn_hold(float turn, uint32_t now)
{
    // 航向保持：转向杆回中时锁住该瞬间的航向，用航向环持续补 ω。
    // 全向轮没有胶轮的被动横摆阻尼，松手就会慢慢歪；这条闭环是手动时姿态的唯一依靠。
    // 转向杆一动就立刻松锁，把转向权还给驾驶员（不再纠偏，否则会和摇杆打架）。
    if (turn != 0.0f)
    {
        hold_active = false;
        return turn;
    }
    if (!hold_active)
    {
        hold_active = true;
        hold_heading = chassis.heading;
        // 锁的这一拍只能要 reset() 的首轮输出，不能再跟一次 update()：
        // 两者用同一个时间戳 → gap_time_s = 0 → D 项 (0−0)/0 = NaN，
        // NaN 顺着转向通道进混控（f±s±t 四项都含 t）会把四个轮目标全带成 NaN。
        // 锁定瞬间误差恒为 0（目标就是此刻航向），reset 返回的 P 项也是 0，语义一致。
        return heading_pid.reset(0.0f, now);
    }
    return heading_pid.update(remainderf(hold_heading - chassis.heading, 360.0f), now);
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
    chassis.speed_tick(); // 内部先同步（读上拍达标情况、算各组 dyn_factor），再跑四组速度环
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
    // 全向底盘下 move 就是 goto 的一个特例：只给前向目标、侧向 0、朝向不变，直接转调。
    // 转调的好处是侧向漂移也有人管——单独实现时 side 通道恒 0，横漂 5cm 也算走完了。
    goto_local(distance, 0.0f, 0.0f);
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
    clear_manual_state();
    chassis_task_ptr = nullptr;
    if (move_end_callback != nullptr)
        move_end_callback();
}

void RobotAction::stop_move()
{
    // 外部主动打断：只停车、清指针，不触发回调。
    chassis.stop();
    clear_manual_state();
    chassis_task_ptr = nullptr;
}

void RobotAction::clear_manual_state()
{
    manual_forward = 0.0f;
    manual_side = 0.0f;
    hold_active = false;
}

void RobotAction::manual()
{
    // 建议外部手动设置only_value状态,避免误触
    clear_manual_state();
    // 缓冲步长要有真实起点：留 0 的话首个松杆拍 dt_s 会走兜底值 0.01，一步收得偏大。
    manual_prev_us = get_time_us();
    chassis_task_ptr = &RobotAction::update_manual;
}
