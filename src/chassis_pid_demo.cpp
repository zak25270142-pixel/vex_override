#include "chassis_pid_demo.h"
#include "pid.h"

// 本车硬件只在 demo 中声明，Chassis 工具类不绑定端口。
static vex::motor left_chassis_1(vex::PORT6, vex::ratio6_1, false);
static vex::motor left_chassis_2(vex::PORT7, vex::ratio6_1, false);
static vex::motor left_chassis_3(vex::PORT8, vex::ratio6_1, true);
static vex::motor left_chassis_4(vex::PORT9, vex::ratio6_1, true);
static vex::motor right_chassis_1(vex::PORT17, vex::ratio6_1, true);
static vex::motor right_chassis_2(vex::PORT18, vex::ratio6_1, false);
static vex::motor right_chassis_3(vex::PORT19, vex::ratio6_1, false);
static vex::motor right_chassis_4(vex::PORT20, vex::ratio6_1, true);

// 参数顺序：直连轮轴的 motor1/2（60 齿）在前，经齿轮传动的 motor3/4（48 齿）在后。
// 实车接线未完成，当前按电机构造时 reversed 标志推测直连/减速归属，接线后必须核对。
static MyMotorGroup left_motors(
    left_chassis_1, left_chassis_2, left_chassis_3, left_chassis_4);
static MyMotorGroup right_motors(
    right_chassis_2, right_chassis_3, right_chassis_1, right_chassis_4);

// 两个定位轮均已接入底盘里程计；reverse 标志若与实际滚动方向不符，试车时在此翻转。
static vex::rotation forward_tracking_sensor(vex::PORT1, false);
static vex::rotation left_tracking_sensor(vex::PORT2, false);

static vex::inertial inertial_sensor(vex::PORT11, vex::turnType::right);

Chassis chassis(left_motors, right_motors,
                forward_tracking_sensor, left_tracking_sensor,
                inertial_sensor);

// 以下参数属于具体自动动作，不属于底盘硬件本身，因此不放进 Chassis。
static const float distance_tolerance = 0.008f;     // m
static const float linear_speed_tolerance = 0.035f; // m/s
static const float heading_tolerance = 1.0f;        // deg
static const float angle_tolerance = 0.8f;          // deg
static const float angular_speed_tolerance = 5.0f;  // deg/s

// 直线段同时使用距离 PID 推进、航向 PID 差速纠偏；转向段只使用 turn_pid。
// ki、kd 按秒计（kp 无量纲）：(kp, ki, kd[, output_limit])
static PositionPID distance_pid(130.0f, 35.0f, 18.0f);
static PositionPID heading_pid(1.10f, 0.1f, 0.04f, 25.0f);
static PositionPID turn_pid(0.75f, 0.1f, 0.06f);
// 误差与实际速度连续满足条件 180 ms 后才结束当前步骤，避免刚经过目标就切换。
static StableJudge stable_judge(180);

TASK_STATE state = task_finish;

// x、y 在发布时转换为距离和方向，避免在 10 ms 刷新中重复 sqrtf、atan2f。
struct GOTO_LOCAL_TARGET
{
    float distance;  // 起点到目标点的直线距离，单位 m。
    float direction; // 目标点相对起始车头的方向，单位 deg。
    float heading;   // 目标最终朝向；发布时相对起点，运行时按阶段转换。
};

static GOTO_LOCAL_TARGET goto_target;
// 0：朝向目标点；1：直线到目标点；2：纠正最终朝向。
static uint8_t goto_step = 0;

// slim_base 始终保存任务起点坐标中的固定目标位姿。
struct GOTO_SLIM_BASE_TARGET
{
    float x;
    float y;
    float heading;
};

static GOTO_SLIM_BASE_TARGET slim_base_target;
static bool use_slim_base = false;

// 根据已经确定的 goto_step 建立本段坐标原点、重置 PID 并输出第一轮结果。
// 无需转向或移动的空步骤会直接跳过，因此外层不需要额外的初始化状态。
static void start_goto_step(uint32_t now)
{
    if (goto_step == 0)
    {
        // 第一步原地转到目标点方向；左右轮输出相反，所以车体中心不前移。
        if (fabsf(goto_target.direction) > angle_tolerance)
        {
            chassis.reset();
            float output = turn_pid.reset(goto_target.direction, now);
            chassis.output(output, -output);
            return;
        }
        // 目标就在当前朝向上，直接进入直线段。
        goto_step = 1;
    }

    if (goto_step == 1)
    {
        // 第二步以当前车头为直线段基准；heading_pid 后续负责保持该方向。
        if (goto_target.distance > distance_tolerance)
        {
            chassis.reset();
            float output = distance_pid.reset(goto_target.distance, now);
            heading_pid.reset(0.0f, now);
            chassis.output(output, output);
            return;
        }
        // 目标点就在起点附近，不存在可靠的目标点方向，只处理最终朝向。
        goto_step = 2;
    }

    // 此前 heading 已转为绝对目标角；第三步开始时再换算成当前需要转过的角度。
    goto_target.heading -= chassis.heading;
    if (fabsf(goto_target.heading) <= angle_tolerance)
    {
        chassis.stop();
        state = task_finish;
        return;
    }

    chassis.reset();
    float output = turn_pid.reset(goto_target.heading, now);
    chassis.output(output, -output);
}

// 每轮重新瞄准固定目标点，距离 PID 与方向 PID 同时输出，因此能够边走边转。
static void refresh_goto_local_slim_base(uint32_t now)
{
    bool starting = state == task_start;

    if (starting)
    {
        chassis.reset();
        state = task_run;
    }

    // demo 只消费 Chassis 已经换算好的任务起点坐标，不自行积分测量量。
    float x_error = slim_base_target.x - chassis.distance_from_reset;
    float y_error = slim_base_target.y - chassis.side_distance_from_reset;
    float distance_error = sqrtf(x_error * x_error + y_error * y_error);
    float heading_error = remainderf(slim_base_target.heading - chassis.heading_from_reset, 360.0f);

    // 将目标向量旋转到当前车体坐标；距离 PID 使用有方向的前后误差。
    float heading_rad = chassis.heading_from_reset * deg_to_rad;
    float forward_error =
        x_error * cosf(heading_rad) + y_error * sinf(heading_rad);
    float side_error =
        -x_error * sinf(heading_rad) + y_error * cosf(heading_rad);

    // 未到目标点时追踪目标点方向；进入距离容差后改为纠正最终朝向。
    float angle_error = heading_error;
    if (distance_error > distance_tolerance)
        angle_error = atan2f(side_error, forward_error) * rad_to_deg;

    float forward;
    float turn;
    if (starting)
    {
        forward = distance_pid.reset(forward_error, now);
        turn = turn_pid.reset(angle_error, now);
    }
    else
    {
        forward = distance_pid.update(forward_error, now);
        turn = turn_pid.update(angle_error, now);
    }

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
    {
        chassis.stop();
        state = task_finish;
    }
}

void refresh()
{
    // 每轮首先更新里程、速度和航向；步骤初始化时 reset() 会再读取并建立本段原点。
    chassis.update();

    if (state == task_finish)
        return;

    uint32_t now = get_time_ms();

    if (use_slim_base)
    {
        refresh_goto_local_slim_base(now);
        return;
    }

    if (state == task_start)
    {
        // 发布接口给的是相对朝向；加上任务起点航向后得到固定的绝对目标朝向。
        goto_target.heading += chassis.heading;
        state = task_run;

        // 距离过小时跳过“朝向目标点”和“直行”，避免使用无意义的 atan2 方向。
        if (goto_target.distance <= distance_tolerance)
            goto_step = 2;
        else
            goto_step = 0;

        start_goto_step(now);
        return;
    }

    bool finished;

    if (goto_step == 0)
    {
        // 原地转向：目标方向减去本段已经转过的角度，得到本轮剩余误差。
        float error = goto_target.direction - chassis.heading_from_reset;
        float output = turn_pid.update(error, now);
        chassis.output(output, -output);

        finished = stable_judge.update(
            fabsf(error) <= angle_tolerance &&
                fabsf(chassis.angular_speed) <= angular_speed_tolerance,
            now);
    }
    else if (goto_step == 1)
    {
        // 距离 PID 决定共同前进量，航向 PID 产生左右差值以保持直线。
        float distance_error =
            goto_target.distance - chassis.distance_from_reset;
        float heading_error = -chassis.heading_from_reset;
        float output = distance_pid.update(distance_error, now);
        float correction = heading_pid.update(heading_error, now);
        chassis.output(output + correction, output - correction);

        finished = stable_judge.update(
            fabsf(distance_error) <= distance_tolerance &&
                fabsf(chassis.linear_speed) <= linear_speed_tolerance &&
                fabsf(heading_error) <= heading_tolerance &&
                fabsf(chassis.angular_speed) <= angular_speed_tolerance,
            now);
    }
    else
    {
        // 到达目标点后原地转到调用者要求的最终朝向。
        float error = goto_target.heading - chassis.heading_from_reset;
        float output = turn_pid.update(error, now);
        chassis.output(output, -output);

        finished = stable_judge.update(
            fabsf(error) <= angle_tolerance &&
                fabsf(chassis.angular_speed) <= angular_speed_tolerance,
            now);
    }

    if (finished)
    {
        // 先停止上一段，再确定下一步并初始化；最后一步完成后保持 finish。
        chassis.stop();
        goto_step++;
        if (goto_step >= 3)
            state = task_finish;
        else
            start_goto_step(now);
    }
}

void goto_local(float x, float y, float heading,
                float max_speed)
{
    // 这里只整理任务输入；电机输出由下一轮 refresh() 开始。
    goto_target.distance = sqrtf(x * x + y * y);
    goto_target.heading = heading;

    // 接近原点时方向没有物理意义，不计算 atan2f(0, 0)。
    if (goto_target.distance > distance_tolerance)
        goto_target.direction = atan2f(y, x) * rad_to_deg;

    // 同一上限同时约束直线和转向 PID，具体左右轮限幅由 Chassis::output() 完成。
    distance_pid.max_output = max_speed;
    turn_pid.max_output = max_speed;
    use_slim_base = false;
    state = task_start;
}

void goto_local_slim_base(float x, float y, float heading,
                          float max_speed)
{
    slim_base_target = {x, y, heading};
    distance_pid.max_output = max_speed;
    turn_pid.max_output = max_speed;
    use_slim_base = true;
    state = task_start;
}

void stop()
{
    // main 处理暂停或超时时调用；finish 会阻止 refresh 继续产生电机输出。
    chassis.stop();
    state = task_finish;
}
