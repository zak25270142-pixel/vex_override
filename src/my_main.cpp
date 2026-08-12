#include "my_main.h"
#include "timer.h"
#include "LCD_menu.h"
#include "key_set.h"
#include "ai_vision.h"

vex::brain Brain;

vex::motor MotorA(vex::PORT1, vex::ratio6_1, true);

CycleTimer main_timer(10); // 暂定10ms  生成主循环周期 类

void my_Init()
{ // main.cpp while 前
    Brain.Screen.setPenWidth(1);
    key_init1();
    pre_menu_init();

    ai_vision_init();

    MotorA.setVelocity(10, vex::velocityUnits::rpm);
}

void my_while()
{                       // main.cpp while 区
    main_timer.cycle(); // 确立循环频率
    refresh_menu();

    // 主循环为 10 ms，每 4 轮读取一次视觉，即约 40 ms 一帧。
    static uint8_t cycle_num = 0;
    cycle_num++;
    if (cycle_num % 4 == 1)
    {
        ai_vision_refresh();
    }

    if (screen_button_need_draw != Screen_Button::not_draw)
    {
        draw_button();
    }
}
