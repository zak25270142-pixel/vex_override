#ifndef CHASSIS_PID_DEMO_H_
#define CHASSIS_PID_DEMO_H_

#include "chassis.h"

// 本 demo 对应的实际底盘实例；具体硬件端口只在 .cpp 中声明。
extern Chassis chassis;

// 主循环每 10 ms 调用一次；函数不等待，每次只推进一轮当前动作。
void chassis_demo_refresh();

// main 只读取该状态来协调后续任务和处理超时；finish 会保持到新任务发布。
extern TASK_STATE chassis_demo_state;

// 发布局部位姿任务，不在调用处直接运行。
// 坐标以任务起点为原点：x 向前、y 向右，heading 单位 deg、向右为正。
void chassis_demo_goto_local(float x, float y, float heading,
                             float max_speed = 80.0f);

// 主循环暂停动作时停止底盘输出。
void chassis_demo_stop();

#endif
