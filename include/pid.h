#ifndef PID_H_
#define PID_H_

#include "vex.h"

/*
 * 通用位置式 PID 控制器。
 *
 * 这个类只负责数学计算，不直接读取传感器，也不直接控制电机。
 * 使用者在外部计算“目标值 - 当前值”，再把误差传给 update()。
 * 因此同一个类可以用于底盘距离、底盘转角、云台角度、升降位置和飞轮速度。
 *
 * 不需要为 P、PI、PD 和 PID 分别设计类：
 *
 *   PositionPID p_controller (1.0f);               // 只有 P
 *   PositionPID pi_controller(1.0f, 0.1f);         // P + I
 *   PositionPID pd_controller(1.0f, 0.0f, 0.2f);   // P + D
 *   PositionPID pid_controller(1.0f, 0.1f, 0.2f);  // P + I + D
 */
class PositionPID
{
private:
    // 三个控制系数在创建对象时确定。
    float kp;
    float ki;
    float kd;
    // integral 保存误差累计；本类所有时间量统一使用 ms。
    float integral;
    // previous_error 保存上一轮误差，用于计算 D 项和判断误差是否越过零点。
    float previous_error;

    // 上一轮 update() 使用的 VEX 系统毫秒时间戳。
    uint32_t last_time_ms;

public:
    // 输出限幅
    float max_output;

    // 构造函数：I、D 和输出上限均可省略。
    PositionPID(float p,
        float i = 0.0f,
        float d = 0.0f,
        float output_limit = 100.0f);

    /*
     * 开始一个新动作时调用 reset(error)。
     * reset() 同时也是这个动作的第一次 PID 计算：它会清除旧状态，
     * 保存第一份误差和时间，并返回只含 P 项的第一轮输出。
     * 因为第一轮没有上一份测量，所以不计算 I 和 D。
     */
    float reset(float error,
                uint32_t current_time_ms = vex::timer::system());

    /*
     * 根据本轮误差计算输出。
     *
     * error           = 目标值 - 当前值
     * current_time_ms = 当前系统毫秒时间戳；省略时自动读取 VEX 系统时间
     *
     * 内部时间单位全部为 ms：
     *   integral   += error * gap_time_ms
     *   derivative  = error_change / gap_time_ms
     *
     * 返回值范围由公开成员 max_output 决定。
     * reset() 与第一次 update() 之间必须经过至少 1 ms。
     */
    float update(float error,
                 uint32_t current_time_ms = vex::timer::system());
};

#endif
