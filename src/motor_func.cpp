#include "motor_func.h"

vex::motor motorA(vex::PORT11, vex::ratio6_1, true);
vex::motor motorB(vex::PORT12, vex::ratio6_1, true);
vex::motor motorC(vex::PORT13, vex::ratio6_1, true);
vex::motor motorD(vex::PORT14, vex::ratio6_1, false);
vex::motor motorE(vex::PORT15, vex::ratio6_1, false);
vex::motor motorF(vex::PORT16, vex::ratio6_1, false);

// pid常量
struct Motor_kpid
{
    float kp; // 比例系数
    float ki; // 积分系数
    float kd; // 微分系数

    float integral_limit; // 积分限幅
    float output_limit;   // 输出限幅
    float deadzone;       // 死区
};
// pid中间变量
struct Motor_pid_midvalue
{
    float p_term = 0;       // 比例项
    float integral = 0;     // 积分项
    float d_term = 0;       // 微分项
    float prev_error = 0;   // 上一次误差
    uint32_t prev_time = 0; // 上一次时间戳
    uint32_t gap_time = 0;  // 间隔时间
    float target = 0;       // 目标值
    float output = 0;       // 输出值
    float current = 0;      // 当前值
    float error = 0;        // 当前误差

    // 增量式
    float prev_output = 0;
    float prev_prev_error = 0;

    // 抗饱和式 抗饱和系数
    float windup_limit = 0.5f;

    float other_value = 0;
};

class MotorCtrl
{
private:
    Motor_kpid pid;              // pid常量
    vex::timer *TIMER = nullptr; // 时间定时器指针

public:
    Motor_pid_midvalue pid_midvalue;                              // pid中间变量
    float (*get_value)(vex::motor *);                             // 获取当前值函数指针
    float (*pid_output)(Motor_pid_midvalue, Motor_kpid);          // pid输出函数指针
    vex::directionType directiontype;                             // 电机方向
    vex::velocityUnits velocityunits;                             // 速度单位
    vex::motor *motors[4] = {nullptr, nullptr, nullptr, nullptr}; // 电机(组)指针

    MotorCtrl(Motor_kpid kpid, float (*get_value)(vex::motor *), float (*pid_output)(Motor_pid_midvalue, Motor_kpid),
              vex::motor *m1, vex::motor *m2 = nullptr, vex::motor *m3 = nullptr, vex::motor *m4 = nullptr, vex::timer *t = nullptr)
        : pid(kpid), TIMER(t)
    {
        motors[0] = m1;
        motors[1] = m2;
        motors[2] = m3;
        motors[3] = m4;
    }
    void refresh()
    { // 数据更新
        // 时间相关
        uint32_t now_time = get_time_ms(TIMER);
        pid_midvalue.gap_time = now_time - pid_midvalue.prev_time;
        pid_midvalue.prev_time = now_time;
        // 目标值与误差
        pid_midvalue.current = get_value(motors[0]);                         // 当前值
        pid_midvalue.error = pid_midvalue.target - pid_midvalue.current;     // 当前误差
        pid_midvalue.p_term = pid.kp * pid_midvalue.error;                   // 比例项 误差*比例
        pid_midvalue.integral += pid_midvalue.error * pid_midvalue.gap_time; // 积分项 误差*间隔时间
        // 积分限幅
        if (pid_midvalue.integral > pid.integral_limit)
            pid_midvalue.integral = pid.integral_limit;
        else if (pid_midvalue.integral < -pid.integral_limit)
            pid_midvalue.integral = -pid.integral_limit;
        // 微分项  误差变化率*微分系数/间隔时间;
        pid_midvalue.d_term = pid.kd * (pid_midvalue.error - pid_midvalue.prev_error) / pid_midvalue.gap_time;
        pid_midvalue.prev_error = pid_midvalue.error;        // 上一次误差
        pid_midvalue.output = pid_output(pid_midvalue, pid); // pid计算结果获取（通过函数指针进行函数调用）
        // 结果输出给电机
        motor_output(pid_midvalue.output);
    }
    void motor_output(float output)
    {
        for (int i = 0; i < 4 && motors[i] != nullptr; i++)
        {
            motors[i]->spin(directiontype, output, velocityunits);
        }
    }
    void stop(vex::brakeType braketype)
    {
        for (int i = 0; i < 4 && motors[i] != nullptr; i++)
        {
            motors[i]->stop(braketype);
        }
    }
};
float get_pid_outputA(Motor_pid_midvalue mid, Motor_kpid pid)
{
    return mid.output;
}
float motor_get_valueA(vex::motor *amotor)
{
    return amotor->position(vex::rotationUnits::rev);
}
struct Motor_kpid chassis_kpid1 = {0.8f, 0.05f, 0.2f, 300.0f, 100.0f, 3.0f};
MotorCtrl control_left(chassis_kpid1, motor_get_valueA, get_pid_outputA, &motorA, &motorB, &motorC);
MotorCtrl control_right(chassis_kpid1, motor_get_valueA, get_pid_outputA, &motorD, &motorE, &motorF);

//差速小车：
// 左电机组：control_left
// 右电机组：control_right

const float wheel_diameter = 50.0f; // 轮直径
const float wheel_track = 100.0f; // 轮间距
const float chassis_width = 100.0f; // 车宽(轮到轮)





// float calculate(float target, float current, Motor_pid_midvalue &mid,
//                 Motor_kpid *params, uint32_t current_time)
// {
//     if (!params)
//         return 0.0f;

//     mid.target = target;
//     mid.current = current;
//     mid.error = target - current;

//     // 1. 死区处理
//     if (fabs(mid.error) < params->deadzone)
//     {
//         mid.output = 0;
//         return 0.0f;
//     }

//     // 2. 计算时间差
//     float dt = 0.0f;
//     if (mid.prev_time != 0)
//     {
//         dt = (current_time - mid.prev_time) / 1000.0f; // 毫秒转秒
//     }
//     if (dt <= 0)
//         dt = params->dt; // 使用默认采样时间

//     // 3. 比例项
//     float p_term = params->kp * mid.error;

//     // 4. 积分项
//     mid.integral += mid.error * dt;

//     // 积分限幅
//     if (params->integral_limit > 0)
//     {
//         if (mid.integral > params->integral_limit)
//             mid.integral = params->integral_limit;
//         if (mid.integral < -params->integral_limit)
//             mid.integral = -params->integral_limit;
//     }
//     float i_term = params->ki * mid.integral;

//     // 5. 微分项
//     float derivative = 0.0f;
//     if (mid.prev_time != 0)
//     {
//         derivative = (mid.error - mid.prev_error) / dt;
//     }
//     float d_term = params->kd * derivative;

//     // 6. 计算输出
//     mid.output = p_term + i_term + d_term;

//     // 7. 输出限幅
//     if (params->output_limit > 0)
//     {
//         if (mid.output > params->output_limit)
//             mid.output = params->output_limit;
//         if (mid.output < -params->output_limit)
//             mid.output = -params->output_limit;
//     }

//     // 8. 更新状态
//     mid.prev_error = mid.error;
//     mid.prev_time = current_time;

//     return mid.output;
// }

// // ==================== 5. 增量式PID算法 ====================
// float calculate2(float target, float current, Motor_pid_midvalue &mid,
//                  Motor_kpid *params, uint32_t current_time)
// {
//     if (!params)
//         return 0.0f;

//     mid.target = target;
//     mid.current = current;
//     mid.error = target - current;

//     // 死区处理
//     if (fabs(mid.error) < params->deadzone)
//     {
//         mid.output = 0;
//         return 0.0f;
//     }

//     // 计算时间差
//     float dt = 0.0f;
//     if (mid.prev_time != 0)
//     {
//         dt = (current_time - mid.prev_time) / 1000.0f;
//     }
//     if (dt <= 0)
//         dt = params->dt;

//     // 增量式PID公式：Δu = kp*(e(k)-e(k-1)) + ki*e(k) + kd*(e(k)-2e(k-1)+e(k-2))
//     float delta_p = params->kp * (mid.error - mid.prev_error);
//     float delta_i = params->ki * mid.error;
//     float delta_d = params->kd * (mid.error - 2 * mid.prev_error + prev_prev_error);

//     float delta_output = delta_p + delta_i + delta_d;
//     mid.output = prev_output + delta_output;

//     // 输出限幅
//     if (params->output_limit > 0)
//     {
//         if (mid.output > params->output_limit)
//             mid.output = params->output_limit;
//         if (mid.output < -params->output_limit)
//             mid.output = -params->output_limit;
//     }

//     // 更新状态
//     prev_prev_error = mid.prev_error;
//     mid.prev_error = mid.error;
//     mid.prev_time = current_time;
//     prev_output = mid.output;

//     return mid.output;
// }

// float calculate(float target, float current, Motor_pid_midvalue &mid,
//                 Motor_kpid *params, uint32_t current_time)
// {
//     if (!params)
//         return 0.0f;

//     mid.target = target;
//     mid.current = current;
//     mid.error = target - current;

//     // 死区处理
//     if (fabs(mid.error) < params->deadzone)
//     {
//         mid.output = 0;
//         return 0.0f;
//     }

//     // 计算时间差
//     float dt = 0.0f;
//     if (mid.prev_time != 0)
//     {
//         dt = (current_time - mid.prev_time) / 1000.0f;
//     }
//     if (dt <= 0)
//         dt = params->dt;

//     // 比例项
//     float p_term = params->kp * mid.error;

//     // 积分项（带抗饱和）
//     float error_for_integral = mid.error;

//     // 如果输出已经饱和，减小积分积累
//     if (params->output_limit > 0 && fabs(mid.prev_error) > 0)
//     {
//         float saturation = fabs(mid.prev_error) / params->output_limit;
//         if (saturation > 1.0f)
//         {
//             error_for_integral *= (1.0f - windup_limit * (saturation - 1.0f));
//         }
//     }

//     mid.integral += error_for_integral * dt;

//     // 积分限幅
//     if (params->integral_limit > 0)
//     {
//         if (mid.integral > params->integral_limit)
//             mid.integral = params->integral_limit;
//         if (mid.integral < -params->integral_limit)
//             mid.integral = -params->integral_limit;
//     }
//     float i_term = params->ki * mid.integral;

//     // 微分项
//     float derivative = 0.0f;
//     if (mid.prev_time != 0)
//     {
//         derivative = (mid.error - mid.prev_error) / dt;
//     }
//     float d_term = params->kd * derivative;

//     // 计算输出
//     mid.output = p_term + i_term + d_term;

//     // 输出限幅
//     if (params->output_limit > 0)
//     {
//         if (mid.output > params->output_limit)
//             mid.output = params->output_limit;
//         if (mid.output < -params->output_limit)
//             mid.output = -params->output_limit;
//     }

//     // 更新状态
//     mid.prev_error = mid.error;
//     mid.prev_time = current_time;

//     return mid.output;
// }
// motor( int32_t index, gearSetting gears );/ motor( int32_t index, gearSetting gears, bool reverse );

// 翻译/作用：构造函数（带齿轮组设置）。VEX电机有不同的齿轮盒（如红齿轮36:1，绿齿轮18:1，蓝齿轮6:1）。这个参数告诉库你用的是哪一种，这会影响spinFor、position()等函数中关于“转数”的计算精度。
// 如何使用：vex::motor liftMotor(3, vex::gearSetting::ratio18_1, false);创建一个用于升降的电机，在端口3，使用绿齿轮（18:1），不反转。

// void setReversed( bool value );

// 翻译/作用：在电机创建后，动态设置或改变其转向。true为反转，false为正转。
// 如何使用：在自动程序阶段，你可能需要一个转向；在手动控制阶段，想反过来。就可以用 myMotor.setReversed(true);来切换。

// void setStopping( brakeType mode );

// 翻译/作用：设置电机的默认停止模式。有三种：brake（刹车，快速停住）、coast（滑行，自然停下）、hold（锁死，用力停在当前位置）。
// 如何使用：对于升降臂的电机，一定要设为 hold，这样停在半空不会掉下来。对于驱动轮，通常用 brake来快速制动。myMotor.setStopping(vex::brakeType::hold);

// void resetPosition( void );/ void setPosition( double value, rotationUnits units );

// 翻译/作用：
// resetPosition：将电机内置编码器的计数值归零。常用于在程序开始时定义“零点位置”。
// setPosition：将电机编码器的值设为一个指定的数，而不是零。
// 如何使用：自动程序开始，升降臂收回到最低点，此时调用 liftMotor.resetPosition();，之后所有 liftMotor.position(vex::rotationUnits::deg)读取的值都相对于这个零点。

// void setTimeout( int32_t time, timeUnits units );

// 翻译/作用：为电机的spinTo/spinFor等定位运动设置一个超时时间。如果电机在指定时间内没有到达目标位置（可能被卡住），就会停止并退出，防止程序死等。
// 如何使用：myMotor.setTimeout(3000, vex::timeUnits::msec);设置超时为3秒。

// 第二部分：让电机动起来（动作控制）
// 这是最核心的部分，所有让电机旋转的函数。

// void spin( directionType dir );

// 翻译/作用：让电机以一个方向开始旋转。旋转的速度由之前setVelocity设置的默认速度决定，或者如果没有设置，则是一个默认速度。
// 如何使用：myMotor.spin(vex::forward);让电机正转。

// void spin( directionType dir, double velocity, velocityUnits units );

// 翻译/作用：让电机以指定的方向和速度立即旋转。这是最常用的手动控制函数。
// 如何使用：driveMotor.spin(vex::reverse, 80, vex::percentUnits::pct);让驱动电机以80%功率反转。

// void stop( void );/ void stop( brakeType mode );

// 翻译/作用：停止电机。无参数版本使用默认停止模式（由setStopping设置）。带参数版本使用指定的模式立即停止。
// 如何使用：myMotor.stop();或 myMotor.stop(vex::brakeType::brake);急刹车。

// 第三部分：读取电机状态（传感器反馈）
// 这些函数让你读取电机的各种实时数据，对于调试和高级控制策略非常重要。

// double position( rotationUnits units );

// 翻译/作用：读取电机编码器的当前位置值（角度）。
// 如何使用：double currentAngle = armMotor.position(vex::rotationUnits::deg);获取机械臂当前角度。

// double velocity( velocityUnits units );
// 翻译/作用：读取电机的当前实时转速。

// 如何使用：double currentSpeed = myMotor.velocity(vex::percentUnits::pct);获取当前实际功率百分比。

// double current( currentUnits units );/ double torque( torqueUnits units );

// 翻译/作用：
// current：读取电机的电流。电流大小直接反映电机的“吃力”程度。
// torque：读取电机的扭矩（扭力）。
// 如何使用：实现“堵转检测”。如果 myMotor.current(vex::currentUnits::amp) > 1.5持续一段时间，说明电机可能被卡住，需要停止。

// double temperature( percentUnits units );

// 翻译/作用：读取电机的温度（百分比形式）。温度过高电机会被强制降功率保护。
// 如何使用：在长时间高强度运行的程序中监控温度，如果 myMotor.temperature() > 80，可以降低功率或报警。

// 第四部分：其他辅助功能

// bool installed();

// 翻译/作用：检查这个端口是否真的连接了一个电机。用于设备检测和调试。
// 如何使用：程序初始化时，if (!myMotor.installed()) { Brain.Screen.print(“电机未连接！”); }

// gearSetting getMotorCartridge();

// 翻译/作用：获取这个电机对象的齿轮盒设置（是红齿轮、绿齿轮还是蓝齿轮）。