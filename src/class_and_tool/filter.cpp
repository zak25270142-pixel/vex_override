#include "filter.h"

// 初始化列表顺序须与头文件中的声明顺序一致（私有字段在前、Q/R 公共成员在后）。
KalmanFilter::KalmanFilter(float process_noise, float measure_noise)
    : x(0.0f),
      P(1.0f),
      K(0.0f),
      last_time_us(0),
      initialized(false),
      Q(process_noise),
      R(measure_noise)
{
}

void KalmanFilter::init(float process_noise, float measure_noise, float init_value,
                        uint32_t current_time_us)
{
    Q = process_noise;
    R = measure_noise;
    x = init_value;
    P = 1.0f; // 初始不确定度取较大，让首次测量权重更高
    K = 0.0f;
    last_time_us = current_time_us;
    initialized = true;
}

float KalmanFilter::update(float measurement, uint32_t current_time_us)
{
    // 未显式 init 时，第一次测量直接作为初值，不做融合，避免从默认 0 值缓慢爬回。
    if (!initialized)
    {
        x = measurement;
        P = R;
        K = 0.0f;
        last_time_us = current_time_us;
        initialized = true;
        return x;
    }

    // uint32_t 时间戳相减得到两次更新之间经过的微秒数（溢出自然回绕）。
    uint32_t gap_time_us = current_time_us - last_time_us;
    float dt_seconds = gap_time_us / 1000000.0f;

    // 预测：状态不变，不确定度按时间折算增加 Q。
    P = P + Q * dt_seconds;

    // 更新增益：P 越大越信任测量
    K = P / (P + R);

    // 状态更新
    x = x + K * (measurement - x);

    // 协方差更新
    P = (1.0f - K) * P;

    last_time_us = current_time_us;

    return x;
}
