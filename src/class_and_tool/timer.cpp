#include "timer.h"

uint32_t get_time_ms();

void Timer_init()
{
}

void Delay(double time)
{ // 延时，单位ms
    vex::wait(time, vex::msec);
}

void wait_to(uint32_t time)
{ // 延时到整点time ms

    double need_wait = time - get_time_ms();
    if (need_wait <= 0)
        return;
    else
        vex::wait(need_wait, vex::msec);
}

uint32_t get_time_ms()
{
    return vex::timer::system();
}

// CycleTimer类
CycleTimer::CycleTimer(uint8_t time_ms) : period(time_ms) {}

void CycleTimer::cycle()
{ // 周期循环 规范每周期时间
    if (need_time > vex::timer::system())
        wait_to(need_time);
    if (Monitor)
    {
        last_start = next_start;
        next_start = vex::timer::systemHighResolution();
    }
    need_time = get_time_ms() + period;
}

uint64_t CycleTimer::get_real_period() { return (next_start - last_start); }

void CycleTimer::cycletimer_reset(uint8_t t_period)
{
    period = t_period;
    need_time = 0;
}

void CycleTimer::monitor(bool enable)
{
    Monitor = enable;
    if (enable)
    {
        last_start = vex::timer::systemHighResolution();
        next_start = last_start;
    }
    else
    {
        last_start = 0;
        next_start = 0;
    }
}
