#include "my_main.h"
#include "timer.h"
#include "LCD_menu.h"
#include "key_set.h"
#include "ai_vision.h"
#include "robot_and_control.h"
#include "communication.h"

vex::brain Brain;

CycleTimer main_timer(10); // 暂定10ms  生成主循环周期 类

// 通信拆成两个任务：rx 阻塞在 /dev/serial1 的 read() 上等字节；tx 每 10ms 一拍（与主循环同线程）。
static vex::event comm_rx_event([] { comm.rx_task(); }); // 内部死循环，不会返回

static void screen_refresh_task()
{
    CycleTimer screen_timer(20);
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

// 心跳掉线 failsafe（回调注入 comm，检测与处置解耦）：
// 开环/空闲态（!is_busy：跑电压、速度环直控）必须收车；PC 位置环任务在跑也收车；
// 手柄控车不打断。失联后控制权不在上位机，它下发/直控的任务一律收回。
static void comm_link_lost_handler()
{
    if (robot_action.is_manual()) // 手柄控车，不打断
        return;
    if (!robot_action.is_busy() || pc_took_control)
        robot_action.stop_move(100000); // 缓停收车，不急刹
}

void my_Init()
{ // main.cpp while 前
    robot_action.init();
    ai_vision_init();

    Brain.Screen.setPenWidth(1);
    key_init1();
    pre_menu_init();
    robot_init();
    comm.init();
    comm.on_link_lost = comm_link_lost_handler; // 装配掉线收车回调（comm 本身不认机器人组件）

    while (!robot_action.is_ready())
        Delay(20);
    while (!ai_vision_is_ready())
        Delay(20);

    robot_action.finish_init();

    ai_vision_event.broadcast();
    screen_refresh_event.broadcast();
    comm_rx_event.broadcast(); // 接收任务：调度器就绪后启动

    robot_action.manual();
    left_axis.state = Remote_Control::only_value;
    right_axis.state = Remote_Control::only_value;
}

void my_while()
{ // main.cpp while 区 10ms标准任务频率区
    main_timer.cycle();
    robot_action.refresh(); // 先更新状态
    // tx_tick 与主循环同线程同节拍（见文件头说明）；rx 在独立任务里阻塞等字节
    comm.tx_tick(); // 最后通信
}
