#ifndef PID_H_
#define PID_H_

#include "vex.h"

// 通用位置式 PID 控制器。
// 需保证先reset()，再update()。包括中途改变目标也需reset()
// 需保证每次update()至少间隔1ms(最好5ms+)，不能连续调用。本身也没有连续调用的必要
//
// 参数量纲（外部传入）：
//   kp : 输出 / 误差
//   ki : 输出 / (误差·秒)
//   kd : 输出·秒 / 误差
// 内部时间单位为 ms，构造时已将 ki、kd 换算到 ms 量纲，调用无需关心。
class PositionPID
{
private:
    // 三个控制系数在创建对象时确定；内部 ki、kd 已换算为 ms 量纲。
    float kp;
    float ki; // 内部单位：输出 / (误差·ms)
    float kd; // 内部单位：输出·ms / 误差
    // integral 保存误差累计；本类所有时间量统一使用 ms。
    float integral;
    // previous_error 保存上一轮误差，用于计算 D 项和判断误差是否越过零点。
    float previous_error;

    // 上一轮 update() 使用的 VEX 系统毫秒时间戳。
    uint32_t last_time_ms;

    int8_t pre_sign = 0;

public:
    // 输出限幅
    float max_output;

    // 构造函数：I、D 和输出上限均可省略。
    // p: kp (输出/误差)；i: ki (输出/(误差·秒))；d: kd (输出·秒/误差)
    PositionPID(float p,
                float i = 0.0f,
                float d = 0.0f,
                float output_limit = 100.0f);

    // 开始设置一个目标并返回第一轮输出。
    float reset(float error, uint32_t current_time_ms = vex::timer::system());

    // 根据本轮误差计算输出。
    // 内部时间单位为 ms：i += error * gap_time_ms；d = error_change / gap_time_ms
    // 返回值范围由公开成员 max_output 决定。
    float update(float error, uint32_t current_time_ms = vex::timer::system());
};

// 判断一个条件是否连续保持了指定时间。
// 没有reset清除旧状态的必要(也就是复位为false)
// 如果第一次update的值就是true，那么就说明系统一直是稳定的，没必要再等待stable_time。
class StableJudge
{
private:
    uint32_t stable_start_time;

public:
    uint16_t stable_time; // 单位 ms

    StableJudge(uint16_t time = 180);

    bool update(bool condition, uint32_t current_time = vex::timer::system());
};

#endif
