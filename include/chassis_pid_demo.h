#ifndef CHASSIS_PID_DEMO_H_
#define CHASSIS_PID_DEMO_H_

#include "my_main.h"

// 1. 设置刹车模式并校准 17 号端口的惯性传感器。
void chassis_demo_init();

// 2. 距离 PID 直行；distance_m 正数前进、负数后退。
void chassis_demo_drive(float distance_m, float max_speed_percent = 80.0f);

// 3. 角度 PID 原地转向；angle_deg 正数右转、负数左转。
void chassis_demo_turn(float angle_deg, float max_speed_percent = 70.0f);

#endif
