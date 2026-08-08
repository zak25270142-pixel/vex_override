#ifndef CHASSIS_PID_DEMO_H_
#define CHASSIS_PID_DEMO_H_

#include "chassis.h"

// 1. 设置刹车模式并校准 17 号端口的惯性传感器。
void chassis_demo_init();

// 2. 距离 PID 直行；distance 单位 m，正数前进、负数后退；max_speed 是有效输出上限百分比。
void chassis_demo_drive(float distance, float max_speed = 80.0f);

// 3. 角度 PID 原地转向；angle 单位 deg，正数右转、负数左转；max_speed 是有效输出上限百分比。
void chassis_demo_turn(float angle, float max_speed = 70.0f);

#endif
