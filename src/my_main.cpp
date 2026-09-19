#include "my_main.h"
#include "timer.h"
#include "LCD_menu.h"
#include "key_set.h"
#include "ai_vision.h"
#include "chassis_pid_demo.h"

vex::brain Brain;

CycleTimer main_timer(10); // 暂定10ms  生成主循环周期 类

static void screen_refresh_task()
{
    CycleTimer screen_timer(10);
    while (true)
    {
        screen_timer.cycle();
        refresh_menu();
        if (screen_button_need_draw != Screen_Button::not_draw)
            draw_button();
    }
}
static vex::event screen_refresh_event(screen_refresh_task);

static void ai_vision_task()
{
    CycleTimer vision_timer(40);
    while (true)
    {
        vision_timer.cycle();
        ai_vision_refresh();
    }
}
static vex::event ai_vision_event(ai_vision_task);

void my_Init()
{ // main.cpp while 前
    chassis.init();
    ai_vision_init();

    Brain.Screen.setPenWidth(1);
    key_init1();
    pre_menu_init();

    while (!chassis.is_ready())
        Delay(20);
    while (!ai_vision_is_ready())
        Delay(20);

    chassis.finish_init();

    ai_vision_event.broadcast();
    screen_refresh_event.broadcast();
}

void my_while()
{ // main.cpp while 区
    main_timer.cycle();
    refresh();
}
