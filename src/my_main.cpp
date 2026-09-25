#include "my_main.h"
#include "timer.h"
#include "LCD_menu.h"
#include "key_set.h"
#include "ai_vision.h"
#include "robot_and_control.h"
#include "communication.h"

vex::brain Brain;

CycleTimer main_timer(10); // 暂定10ms  生成主循环周期 类

// 通信拆成两个任务：
//  rx任务阻塞在/dev/serial1的read()上等USB来字节，收到命令立刻回（如目录请求）；
//  tx任务每10ms一拍，按订阅档位推送监控值。
// 两件事一个阻塞、一个周期，不能放同一个任务里——阻塞读会把10ms推送饿死。
// 注意：两个event的broadcast必须放在my_Init末尾，VEX线程调度器就绪后才生效，
// 放开头会静默失败（线程根本不起）。
static void comm_rx_task()
{
    comm.rx_task(); // 内部死循环，不会返回
}
static vex::event comm_rx_event(comm_rx_task);

static void comm_tx_task()
{
    CycleTimer comm_timer(10);
    while (true)
    {
        comm_timer.cycle();
        comm.tx_tick();
    }
}
static vex::event comm_tx_event(comm_tx_task);

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
    // 通信init最早做（只打开serial1，不依赖硬件）；
    // 但收/发任务的启动(broadcast)必须放到本函数末尾——VEX线程调度器要等
    // Brain/SDK初始化完成后才能响应broadcast，放在开头会静默失败（任务线程根本不起）。
    comm.init();

    robot_action.init();
    ai_vision_init();

    Brain.Screen.setPenWidth(1);
    key_init1();
    pre_menu_init();

    while (!robot_action.is_ready())
        Delay(20);
    while (!ai_vision_is_ready())
        Delay(20);

    robot_action.finish_init();

    ai_vision_event.broadcast();
    screen_refresh_event.broadcast();
    comm_rx_event.broadcast(); // 接收任务：调度器就绪后启动
    comm_tx_event.broadcast(); // 10ms推送任务
}

void my_while()
{ // main.cpp while 区
    main_timer.cycle();
    robot_action.refresh();
    // 通信收/发都在独立的comm_rx_task、comm_tx_task里，不能在主循环再调用
}
