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
//  tx任务每10ms一拍，按订阅档位推送监控值，与主循环共用10ms线程。
static vex::event comm_rx_event([]
                                { comm.rx_task(); }); // 内部死循环，不会返回

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

static void fast_thread()
{
    CycleTimer speed_timer(5);
    while (true)
    {
        speed_timer.cycle();
        chassis.speed_tick();
    }
}
static vex::event fast_thread_event(fast_thread);

// 心跳掉线 failsafe（作为回调注入 comm，检测与处置解耦）：
// 上位机断线时只在底盘空闲态（说明车在跑上位机直控0x84/0x85）替它收车；
// is_busy（自动动作/手柄手动）期间上位机本就没有控制权，一律不打断。
// 调用点是 comm.tx_tick()，与下面 my_while 同一主循环线程，无并发问题。
static void comm_link_lost_handler()
{
    if (!robot_action.is_busy())
        robot_action.stop_move();
}

void my_Init()
{ // main.cpp while 前
    robot_action.init();
    ai_vision_init();

    Brain.Screen.setPenWidth(1);
    key_init1();
    pre_menu_init();
    comm.init();
    comm.on_link_lost = comm_link_lost_handler; // 装配掉线收车回调（comm 本身不认机器人组件）

    while (!robot_action.is_ready())
        Delay(20);
    while (!ai_vision_is_ready())
        Delay(20);

    robot_action.finish_init();

    ai_vision_event.broadcast();
    screen_refresh_event.broadcast();
    comm_rx_event.broadcast();     // 接收任务：调度器就绪后启动
    fast_thread_event.broadcast(); // 5ms速度环任务

    // left_motors.motors[0]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // left_motors.motors[0]->stop(vex::brakeType::coast);
    // Delay(1000);
    // left_motors.motors[1]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // left_motors.motors[1]->stop(vex::brakeType::coast);
    // Delay(1000);
    // left_motors.motors[2]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // left_motors.motors[2]->stop(vex::brakeType::coast);
    // Delay(1000);
    // left_motors.motors[3]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // left_motors.motors[3]->stop(vex::brakeType::coast);
    // Delay(1000);
    // right_motors.motors[0]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // right_motors.motors[0]->stop(vex::brakeType::coast);
    // Delay(1000);
    // right_motors.motors[1]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // right_motors.motors[1]->stop(vex::brakeType::coast);
    // Delay(1000);
    // right_motors.motors[2]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // right_motors.motors[2]->stop(vex::brakeType::coast);
    // Delay(1000);
    // right_motors.motors[3]->spin(vex::directionType::fwd, 10, vex::voltageUnits::volt);
    // Delay(1000);
    // right_motors.motors[3]->stop(vex::brakeType::coast);
    // Delay(5000);
}

void my_while()
{ // main.cpp while 区 10ms标准任务频率区
    main_timer.cycle();
    robot_action.refresh();
    // 通信收/发都在独立的comm_rx_task、comm_tx_task里，不能在主循环再调用

    comm.tx_tick();
}
