#ifndef FILTER_H_
#define FILTER_H_

#include "vex.h"

// 一维卡尔曼滤波器（自 summer070214/VEX_chassis_drive 移植，暂未接入任何传感器）。
// 假设状态近似不变（无运动模型），仅用过程噪声 Q 与测量噪声 R 做平滑，
// 迭代收敛到稳态后等价于一阶低通：Q 相对 R 越大，越信任新测量（滞后小、噪声大）。
// 典型用途：编码器差分速度、电机 velocity() 读数等单通道测量的低通滤波。
//
// 参数量纲：
//   Q : 状态方差 / 秒（过程噪声，状态变化越慢取越小）
//   R : 状态方差（测量噪声，依传感器精度；必须大于 0）
// update 由外部传入时间戳，过程噪声按真实间隔折算，改变调用周期无需重调 Q；
// 但仍应在周期固定的任务中调用（与 PositionPID 相同要求）。
class KalmanFilter
{
private:
    float x; // 状态估计值
    float P; // 估计误差协方差
    float K; // 卡尔曼增益

    // 上一轮 update()/init() 的 VEX 系统微秒时间戳。
    uint32_t last_time_us;

    bool initialized;

public:
    // 过程噪声与测量噪声运行期可直接改（Q 修改下一拍自然生效），
    // 方便挂进调参表现场标定；要求 Q >= 0、R > 0。
    float Q;
    float R;

    // 只设定 Q/R，不设定初值；首次 update 自动用第一次测量完成初始化。
    KalmanFilter(float process_noise = 0.1f,
                 float measure_noise = 0.1f);

    // 显式给定先验初值并立即进入已初始化状态（首拍起即按此先验与测量融合）。
    void init(float process_noise, float measure_noise, float init_value,
              uint32_t current_time_us = vex::timer::systemHighResolution());

    // 用本次测量更新估计，返回滤波后的状态值。
    // 内部按两次调用的微秒间隔把 Q 折算成本拍噪声增量。
    float update(float measurement,
                 uint32_t current_time_us = vex::timer::systemHighResolution());

    // 当前估计值（不触发更新）
    float value() const { return x; }
};

#endif
