#ifndef PID_H_
#define PID_H_

#include "vex.h"

// 通用位置式 PID 控制器。
// 契约：先 reset() 再 update()（含中途改目标）；不可连续调用，每次至少间隔 1ms（调用方负责）。
// 参数量纲与抗饱和/跨零清积分语义见 docs/代码补充注释/03。
class PositionPID
{
private:
    // 上一轮 update() 使用的 VEX 系统微秒时间戳。
    uint32_t last_time_us;

    int8_t pre_sign = 0;

public:
    float kp; // 单位：输出 / 误差
    float ki; // 单位：输出 / (误差·秒)
    float kd; // 单位：输出·秒 / 误差

    float integral;//误差累计，消除稳态误差
    float previous_error;
    float max_output;

    // 构造函数：I、D 和输出上限均可省略。
    // p: kp (输出/误差)；i: ki (输出/(误差·秒))；d: kd (输出·秒/误差)
    PositionPID(float p,
                float i = 0.0f,
                float d = 0.0f,
                float output_limit = 100.0f);

    // 开始设置一个目标并返回第一轮输出。
    float reset(float error, uint32_t current_time_us = vex::timer::systemHighResolution());

    // 根据本轮误差计算输出（时间戳 us，内部换算秒）；返回值由 max_output 限幅。
    float update(float error, uint32_t current_time_us = vex::timer::systemHighResolution());
};

// 判断一个条件是否连续保持了指定时间。
// 无 reset：stable_start_time 跨动作保留
class StableJudge
{
private:
    // 记录条件开始持续满足的 us 时间戳，0 表示当前未在计时。
    uint32_t stable_start_time;

public:
    uint32_t stable_time; // 单位 us

    // 参数单位 ms，内部换算为 us（与 PositionPID 构造换算同一风格）。
    StableJudge(uint16_t time = 180);

    bool update(bool condition, uint32_t current_time_us = vex::timer::systemHighResolution());
};

#endif
