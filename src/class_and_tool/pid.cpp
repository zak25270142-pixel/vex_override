#include "pid.h"
#include <cmath>

PositionPID::PositionPID(float p, float i, float d, float output_limit)
    : last_time_us(0), kp(p),
      ki(i),
      kd(d),
      integral(0.0f), previous_error(0.0f),
      max_output(output_limit) {}

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
    // uint32_t 时间戳相减得到两次计算之间经过的微秒数，再换算成本轮间隔秒数。
    uint32_t gap_time_us = current_time_us - last_time_us;
    float gap_time_s = static_cast<float>(gap_time_us) / 1000000.0f;

    // pre_error为0时pre_sign保持不变
    if (previous_error != 0.0f)
        pre_sign = previous_error > 0.0f ? 1 : -1;
    // 误差跨零：系统越过目标，清旧方向积分
    if ((error > 0.0f && pre_sign == -1) || (error < 0.0f && pre_sign == 1))
        integral = 0.0f;

    // 统一使用秒：I 项是“误差×秒”，D 项是“误差/秒”。
    float candidate_i = integral + error * gap_time_s;
    float p_add_d = kp * error + kd * (error - previous_error) / gap_time_s;
    // 用候选积分算一次未限幅输出，判断是否已达输出极限
    float candidate_output = p_add_d + ki * candidate_i;
    /*
     * 条件积分抗饱和：输出顶限且误差还朝限外推时，本轮不积
     */
    if (fabsf(candidate_output) <= max_output ||
        (candidate_output > max_output && error < 0.0f) ||
        (candidate_output < -max_output && error > 0.0f))
        integral = candidate_i;

    // 用最终被接受的积分重算输出，再限幅
    float output = p_add_d + ki * integral;
    if (output > max_output)
        output = max_output;
    else if (output < -max_output)
        output = -max_output;

    previous_error = error;
    last_time_us = current_time_us;

    return output;
}

StableJudge::StableJudge(uint16_t time)
    : stable_start_time(0), stable_time((uint32_t)time * 1000) {}

bool StableJudge::update(bool condition, uint32_t current_time_us)
{
    if (stable_start_time == 0)
    {
        if (condition)
            stable_start_time = current_time_us;
    }
    else
    {
        if (condition)
            return (current_time_us - stable_start_time >= stable_time);
        else
            stable_start_time = 0;
    }
    return false;
}
