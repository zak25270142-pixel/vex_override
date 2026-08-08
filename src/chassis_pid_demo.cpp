#include "chassis_pid_demo.h"
#include "pid.h"
#include "timer.h"

static const uint8_t control_period = 10; // 单位 ms

// 以下参数属于具体自动动作，不属于底盘硬件本身，因此不放进 Chassis。
static const float distance_tolerance = 0.008f;     // m
static const float linear_speed_tolerance = 0.035f; // m/s
static const float heading_tolerance = 1.0f;        // deg
static const float angle_tolerance = 0.8f;          // deg
static const float angular_speed_tolerance = 5.0f;  // deg/s

void chassis_demo_init()
{
    chassis.init();
}

void chassis_demo_drive(float distance, float max_speed)
// distance 正数表示前进、负数表示后退；max_speed 是 PID 有效输出上限。
{
    if (fabsf(distance) <= distance_tolerance)
        return;

    /*
     * 直行使用两个并行控制器：
     * 1. 距离 PID 根据剩余距离产生共同的前进输出；
     * 2. 航向 PID 根据惯性传感器角度产生左右差速纠偏。
     *
     * PID 内部时间单位统一为 ms，因此 Ki 已按每毫秒积分设置，Kd 按每毫秒微分设置。
     */
    PositionPID distance_pid(130.0f, 0.035f, 18000.0f, max_speed);
    PositionPID heading_pid(1.10f, 0.00010f, 40.0f, 25.0f);
    StableJudge stable_judge(180);

    // Chassis::reset() 读取最新状态，并在底盘对象内部保存本次动作起点。
    chassis.reset();

    uint32_t now = get_time_ms(); // 当前系统时间戳，单位 ms
    float base_output = distance_pid.reset(distance, now);
    heading_pid.reset(0.0f, now);

    // 第一轮还没有产生航向误差，因此左右两侧使用相同的距离 PID 输出。
    chassis.output(base_output, base_output);
    Delay(control_period);

    while (true)
    {
        now = get_time_ms();

        /*
         * 本轮只读取一次全部底盘传感器。下面的距离、速度、航向、角速度
         * 都来自同一个 Chassis 快照，避免同一轮多次调用 VEX API 得到不同时间点的数据。
         */
        chassis.update();

        float distance_error = distance - chassis.distance_from_initial;

        // 目标是保持动作开始时的车头方向，因此目标相对航向始终为 0°。
        float heading_error = -chassis.heading_from_initial;

        // 两个 PID 使用同一个系统时间戳，保证本轮的时间基准一致。
        base_output = distance_pid.update(distance_error, now);
        float correction = heading_pid.update(heading_error, now);

        /*
         * 差速合成：共同部分控制前后移动，差值控制左右转向。
         * Chassis::output() 统一完成等比例限幅、左右死区补偿和电机组输出。
         */
        chassis.output(
            base_output + correction,
            base_output - correction);

        /*
         * 仅仅经过目标点不能算完成。距离、实际直线速度、航向误差和实际角速度
         * 必须同时足够小，并连续保持 180 ms，才认为小车已经稳定停在目标附近。
         */
        bool drive_ok =
            fabsf(distance_error) <= distance_tolerance &&
            fabsf(chassis.linear_speed) <= linear_speed_tolerance &&
            fabsf(heading_error) <= heading_tolerance &&
            fabsf(chassis.angular_speed) <= angular_speed_tolerance;

        if (stable_judge.update(drive_ok, now))
        {
            chassis.stop();
            return;
        }

        Delay(control_period);
    }
}

void chassis_demo_turn(float angle, float max_speed)
// angle 单位 deg，正数表示向右转，负数表示向左转。
{
    if (fabsf(angle) <= angle_tolerance)
        return;

    PositionPID turn_pid(0.75f, 0.00010f, 60.0f, max_speed);
    StableJudge stable_judge(180);

    chassis.reset();
    uint32_t now = get_time_ms(); // 当前系统时间戳，单位 ms

    /*
     * reset() 返回第一轮纯 P 输出。左右两侧使用大小相同、方向相反的命令，
     * 因而小车中心基本不平移，车体绕自身中心原地旋转。
     */
    float turn_output = turn_pid.reset(angle, now);
    chassis.output(turn_output, -turn_output);
    Delay(control_period);

    while (true)
    {
        now = get_time_ms();
        chassis.update();

        float angle_error = angle - chassis.heading_from_initial;

        turn_output = turn_pid.update(angle_error, now);
        chassis.output(turn_output, -turn_output);

        // 角度误差和真实角速度同时足够小并保持 180 ms，才算稳定转到目标。
        bool turn_ok =
            fabsf(angle_error) <= angle_tolerance &&
            fabsf(chassis.angular_speed) <= angular_speed_tolerance;

        if (stable_judge.update(turn_ok, now))
        {
            chassis.stop();
            return;
        }

        Delay(control_period);
    }
}
