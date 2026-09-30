#include "pid.h"
#include <cmath>

PositionPID::PositionPID(float p, float i, float d, float output_limit)
    : kp(p),
      ki(i / 1000000.0f),
      kd(d * 1000000.0f),
      integral(0.0f), previous_error(0.0f),
      last_time_us(0), max_output(output_limit) {}

float PositionPID::reset(float error, uint32_t current_time_us)
{
    // 清除积分项
    integral = 0.0f;

    previous_error = error;
    pre_sign = 0; // pre_sign初始未知，不参与积分清空
    last_time_us = current_time_us;

    // 第一轮没有历史数据，只计算并限幅 P 项。
    float output = kp * error;
    if (output > max_output)
        output = max_output;
    else if (output < -max_output)
        output = -max_output;
    return output;
}

float PositionPID::update(float error, uint32_t current_time_us)
{
    // uint32_t 时间戳相减得到两次计算之间经过的微秒数。
    uint32_t gap_time_us = current_time_us - last_time_us;

    // pre_error为0时pre_sign保持不变，引入该项主要解出err正好是0时的后续处理
    if (previous_error != 0.0f)
        pre_sign = previous_error > 0.0f ? 1 : -1;
    // 误差从正侧（>0）跨到负侧（<0），或反过来，说明系统越过了目标值。
    // 清除旧方向积分，减少越过目标后的持续推力。
    if ((error > 0.0f && pre_sign == -1) || (error < 0.0f && pre_sign == 1))
        integral = 0.0f;

    // 统一使用 us：I 项是“误差×us”，D 项是“误差/us”。
    float candidate_i = integral + error * gap_time_us;
    float p_add_d = kp * error + kd * (error - previous_error) / gap_time_us;
    // 用候选积分计算一次未经限幅的输出，判断电机是否已经达到输出极限。
    float candidate_output = p_add_d + ki * candidate_i;
    /*
     * 条件积分抗饱和：
     * 1. 输出没有超过上限时，正常接受本轮积分。
     * 2. 输出超过正上限，但误差为负时，积分会帮助输出离开正上限，允许积分。
     * 3. 输出超过负上限，但误差为正时，积分会帮助输出离开负上限，允许积分。
     * 如果输出已经达到极限，并且误差还想让输出继续朝极限外增大，就暂停积分。
     */
    if (fabsf(candidate_output) <= max_output ||
        (candidate_output > max_output && error < 0.0f) ||
        (candidate_output < -max_output && error > 0.0f))
        integral = candidate_i;

    // 使用最终被接受的积分重新计算本轮位置式 PID 输出。
    float output = p_add_d + ki * integral;

    // 输出限幅
    if (output > max_output)
        output = max_output;
    else if (output < -max_output)
        output = -max_output;

    previous_error = error;
    last_time_us = current_time_us;

    return output;
}

StableJudge::StableJudge(uint16_t time)
    : stable_start_time(0), stable_time(time) {}

bool StableJudge::update(bool condition, uint32_t current_time)
{
    if (stable_start_time == 0)
    {
        if (condition)
            stable_start_time = current_time;
    }
    else
    {
        if (condition)
            return (current_time - stable_start_time >= stable_time);
        else
            stable_start_time = 0;
    }
    return false;
}
