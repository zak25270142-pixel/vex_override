#ifndef ROBOT_AND_CONTROL_H_
#define ROBOT_AND_CONTROL_H_

#include "robot_action.h"
#include "communication.h" // CMD_ITEM：对外暴露动作命令总表

// 本车硬件的组装入口：robot_and_control.cpp 用具体端口创建硬件与 Chassis，
// 再构造唯一的整机动作实例。流程代码（main、自动阶段）统一使用 robot_action。
extern RobotAction robot_action;
// 底盘实例：监控菜单要直接读它的全局坐标x/y与航向heading，故对外可见。
extern Chassis chassis;
extern MyMotorGroup left_motors;
extern MyMotorGroup right_motors;

// 本车动作命令总表（停止/转向/直行/局部移动）：构造USB_Comm时整张传入。
extern const CMD_ITEM robot_cmds[];
extern const uint8_t robot_cmd_count;

#endif
