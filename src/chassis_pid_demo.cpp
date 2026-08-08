#include "chassis_pid_demo.h"
#include "motor_func.h"
#include "pid.h"
#include "timer.h"
/*
 * 惯性传感器预留在 17 号智能端口。
 * vex::turnType::right 表示从上方看，向右/顺时针旋转时角度增加。
 */
static vex::inertial Inertial(vex::PORT17, vex::turnType::right);

static const float wheel_r = 0.041275f; // 轮半径 41.275 mm
// static const float car_width = 0.3000f; // 轮距暂时预留给以后扩展纯编码器转向或里程计
static const float motor_revs_per_wheel_rev = 1.5000f; // 外部传动比 1.5:1 ，车轮转 1 圈需要电机转 1.5 圈

static const float math_pi = 3.14159f;
static const uint8_t control_period = 10;

static float distance_deadzone = 0.008f; // 误差死区，用于判断是否稳定到位。

void chassis_demo_init()
{
    // 设置以后电机的默认停车模式为“刹车”。
    // coast 会自由滑行；hold 会持续用力保持位置；底盘定点动作通常使用 brake，
    left_motors.setStopping(vex::brakeType::brake);
    right_motors.setStopping(vex::brakeType::brake);
    // stop(brake) 立即停止电机。初始化先停车，避免校准 IMU 时车身仍在运动。
    left_motors.stop(vex::brakeType::brake);
    right_motors.stop(vex::brakeType::brake);
    Inertial.calibrate();            // 栯动陀螺仪零偏校准。
    while (Inertial.isCalibrating()) // 等待校准结束
    {
        Delay(20);
    }
    Inertial.resetRotation(); // 重置陀螺仪角度为 0。
}

void chassis_demo_drive(float distance, float max_speed)
// 直行：distance 正数前进、负数后退；max_speed 是最大电机速度百分比。
{
    // 指令本身已经处于允许误差内时，不需要启动电机。
    if (fabsf(distance) <= distance_deadzone)
        return;

    // PID 内部时间单位为 ms。距离 PID 负责前进，航向 PID 负责修正左右偏转。Ki 已由“每秒”参数除以 1000，Kd 已乘以 1000。
    PositionPID distance_pid(130.0f, 0.035f, 18000.0f, max_speed);
    PositionPID heading_pid(1.10f, 0.00010f, 40.0f, 25.0f);

    // 保存动作起点。
    float start_left_rev = left_motors.position(vex::rotationUnits::rev);
    float start_right_rev = right_motors.position(vex::rotationUnits::rev);
    // 记录动作起点的航向角度。 rotation返回可连续累计的旋转角，例如 450°。
    float start_heading_deg = Inertial.rotation(vex::rotationUnits::deg);

    uint32_t now_ms = get_time_ms();
    float base_output = distance_pid.reset(distance, now_ms);
    heading_pid.reset(0.0f, now_ms);

    // 第一轮左右两侧使用相同的前进输出。
    left_motors.spin(vex::directionType::fwd, base_output, vex::velocityUnits::pct);
    right_motors.spin(vex::directionType::fwd, base_output, vex::velocityUnits::pct);
    Delay(control_period);

    // 首次连续满足“位置误差小且实际速度低”时的系统时间戳；条件失效时清零。
    uint32_t settled_start_time_ms = 0;
    while (true)
    {
        now_ms = get_time_ms();
        /*
         * 读取左右电机组的位置，并把电机转数换算成车轮实际路程：
         *   车轮转数 = 电机转数 / 外部传动比
         *   路程 = 车轮转数 * 2*pi*轮半径
         *
         * motor_group.position() 返回组内第一台电机的编码器位置。
         * 同侧三台电机由齿轮强制同步，因此第一台电机可以代表这一整侧的转数；
         * motor_group.spin() 则会把相同输出同时发送给这一侧的三台电机。
         */
        float left_motor_rev = left_motors.position(vex::rotationUnits::rev) - start_left_rev;
        float right_motor_rev = right_motors.position(vex::rotationUnits::rev) - start_right_rev;
        float left_m = left_motor_rev / motor_revs_per_wheel_rev *
                       2.0f * math_pi * wheel_r;
        float right_m = right_motor_rev / motor_revs_per_wheel_rev *
                        2.0f * math_pi * wheel_r;
        float current_distance_m = (left_m + right_m) * 0.5f;
        float distance_error = distance - current_distance_m;

        // 航向目标是相对起点保持 0°；右偏为正时，误差取负使小车向左修正。
        float current_heading_deg = Inertial.rotation(vex::rotationUnits::deg) -
                                    start_heading_deg;
        float heading_error = -current_heading_deg;

        // 两个 PID 使用同一个 now_ms，保证这一轮的时间基准一致。
        base_output = distance_pid.update(distance_error, now_ms);
        float correction = heading_pid.update(heading_error, now_ms);

        // 差速合成：平均输出负责前进，左右输出差负责纠正航向。
        float left_output = base_output + correction;
        float right_output = base_output - correction;

        // PID 输出与纠偏相加后可能超过百分比范围，因此对最终电机命令限幅。
        left_output = fmaxf(-100.0f, fminf(100.0f, left_output));
        right_output = fmaxf(-100.0f, fminf(100.0f, right_output));

        left_motors.spin(vex::directionType::fwd, left_output, vex::velocityUnits::pct);
        right_motors.spin(vex::directionType::fwd, right_output, vex::velocityUnits::pct);

        float average_motor_rpm =
            (left_motors.velocity(vex::velocityUnits::rpm) +
             right_motors.velocity(vex::velocityUnits::rpm)) *
            0.5f;
        float chassis_speed_mps = average_motor_rpm / motor_revs_per_wheel_rev *
                                  2.0f * math_pi * wheel_r / 60.0f;

        // 必须在目标附近保持低速 180 ms，避免高速经过目标点时被误判为到位。
        bool drive_ok = distance_error <= distance_deadzone && distance_error >= -distance_deadzone &&
                        chassis_speed_mps <= 0.035f && chassis_speed_mps >= -0.035f;
        if (settled_start_time_ms == 0)
        {
            if (drive_ok)
                settled_start_time_ms = now_ms;
        }
        else
        {
            if (drive_ok)
            {
                if (now_ms - settled_start_time_ms >= 180)
                {
                    left_motors.stop(vex::brakeType::brake);
                    right_motors.stop(vex::brakeType::brake);
                    return;
                }
            }
            else
                settled_start_time_ms = 0;
        }
        Delay(control_period);
    }
}

void chassis_demo_turn(float angle_deg, float max_speed)
{
    const float angle_tolerance_deg = 0.8f;
    if (fabsf(angle_deg) <= angle_tolerance_deg)
    {
        left_motors.stop(vex::brakeType::brake);
        right_motors.stop(vex::brakeType::brake);
        return;
    }

    // 正角度表示右转，负角度表示左转。
    PositionPID turn_pid(0.75f, 0.00010f, 60.0f, max_speed);

    float start_heading_deg = Inertial.rotation(vex::rotationUnits::deg);
    uint32_t now_time = get_time_ms();
    uint32_t settled_start_time_ms = 0; // 记录稳定开始时间，用于判断是否稳定到位。

    // reset() 返回第一轮纯 P 输出；左右两侧反向旋转，使车体绕中心自转。
    float turn_output = turn_pid.reset(angle_deg, now_time);
    left_motors.spin(vex::directionType::fwd, turn_output, vex::velocityUnits::pct);
    right_motors.spin(vex::directionType::fwd, -turn_output, vex::velocityUnits::pct);
    Delay(control_period);

    while (true)
    {
        now_time = get_time_ms();

        // rotation() 给出累计角度；减去起点角度后得到本次动作已经转过的角度。
        float current_angle_deg = Inertial.rotation(vex::rotationUnits::deg) -
                                  start_heading_deg;
        float angle_error = angle_deg - current_angle_deg;

        turn_output = turn_pid.update(angle_error, now_time);

        left_motors.spin(vex::directionType::fwd, turn_output, vex::velocityUnits::pct);
        right_motors.spin(vex::directionType::fwd, -turn_output, vex::velocityUnits::pct);

        // 陀螺仪直接测量车体绕竖直轴的实际角速度，单位 deg/s。
        float angular_speed_dps =
            Inertial.gyroRate(vex::axisType::zaxis, vex::velocityUnits::dps);

        // 角度误差和实际角速度同时很小，并持续 180 ms，才算稳定到位。
        bool angle_ok = fabsf(angle_error) <= angle_tolerance_deg;
        bool angular_speed_ok = fabsf(angular_speed_dps) <= 5.0f;
        if (angle_ok && angular_speed_ok)
        {
            if (settled_start_time_ms == 0)
                settled_start_time_ms = now_time;
        }
        else
        {
            settled_start_time_ms = 0;
        }

        if (settled_start_time_ms != 0 && now_time - settled_start_time_ms >= 180)
        {
            left_motors.stop(vex::brakeType::brake);
            right_motors.stop(vex::brakeType::brake);
            return;
        }

        Delay(control_period);
    }
}
