#ifndef ROBOT_AND_CONTROL_H_
#define ROBOT_AND_CONTROL_H_

#include "robot_action.h"

// 本车硬件的组装入口：robot_and_control.cpp 用具体端口创建硬件与 Chassis，
// 再构造唯一的整机动作实例。流程代码（main、自动阶段）统一使用 robot_action。
extern RobotAction robot_action;
// 底盘实例：监控菜单要直接读它的全局坐标x/y与航向heading，故对外可见。
extern Chassis chassis;

#endif
