#include "timer.h"

vex::timer TimerA;
uint32_t get_time_ms(vex::timer *tmr = nullptr);

void Timer_init()
{
}

void Timer_event()
{
    TimerA.event(Timer_init, 114); // 未知用法，114ms后运行Timer_init()
}

void Delay(double time)
{ // 延时，单位ms
    vex::wait(time, vex::msec);
}

void wait_to(uint32_t time, vex::timer *tmr)
{ // 延时到整点time ms

    double need_wait = time - get_time_ms(tmr);
    if (need_wait <= 0)
        return;
    else
        vex::wait(need_wait, vex::msec);
}

uint32_t get_time_ms(vex::timer *tmr)
{
    if (tmr)
        return tmr->system();
    return vex::timer::system();
}

// CycleTimer类
CycleTimer::CycleTimer(uint8_t time_ms, vex::timer *Timer) : TIMER(Timer), period(time_ms) {}

void CycleTimer::cycle()
{ // 周期循环 规范每周期时间
    if (need_time > TIMER->time())
        wait_to(need_time, TIMER);
    if (Monitor)
    {
        last_start = next_start;
        next_start = TIMER->systemHighResolution();
    }
    need_time = get_time_ms(TIMER) + period;
}

uint64_t CycleTimer::get_real_period() { return (next_start - last_start); }

void CycleTimer::cycletimer_reset(uint8_t t_period, vex::timer *Timer)
{
    TIMER = Timer;
    period = t_period;
    need_time = 0;
}

void CycleTimer::monitor(bool enable)
{
    Monitor = enable;
    if (enable && TIMER)
    {
        last_start = TIMER->systemHighResolution();
        next_start = last_start;
    }
    else
    {
        last_start = 0;
        next_start = 0;
    }
}