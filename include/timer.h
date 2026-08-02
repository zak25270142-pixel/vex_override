#ifndef __TIMER_H__
#define __TIMER_H__

#include "vex.h"

class CycleTimer
{
private:
    vex::timer *TIMER;
    uint32_t need_time = 0;  // 下一周期理论开启时刻(ms)
    uint8_t period = 0;      // 周期(ms)
    uint64_t last_start = 0; // 上一周期开启的准确刻度(us)
    uint64_t next_start = 0; // 这一周期开启的准确刻度(us)
    bool Monitor = false;

public:
    CycleTimer(uint8_t time_ms, vex::timer *Timer = nullptr);
    void cycle(); // 规范循环时间
    uint64_t get_real_period();
    void monitor(bool enable);
    void cycletimer_reset(uint8_t t_period, vex::timer *Timer = nullptr);
};

void Delay(double time);
void Timer_init();
void wait_to(uint32_t time, vex::timer *tmr = nullptr);

#endif
