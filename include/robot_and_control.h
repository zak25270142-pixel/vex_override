#ifndef ROBOT_AND_CONTROL_H_
#define ROBOT_AND_CONTROL_H_

#include "robot_action.h"
#include "communication.h" // CMD_ITEM：对外暴露动作命令总表
#include "robot_upper.h"   // 上位机构声明集合头（LiftBar；Grab / Intake 待加）

// 本车硬件的组装入口：robot_and_control.cpp 用具体端口创建硬件与 Chassis，
// 再构造唯一的整机动作实例。流程代码（main、自动阶段）统一使用 robot_action。
extern RobotAction robot_action;
// 底盘实例：监控菜单要直接读它的全局坐标x/y与航向heading，故对外可见。
extern Chassis chassis;
extern ChassisMotorGroup left_motors;
extern ChassisMotorGroup right_motors;

// 逐台电机属性数组：组内持指针共用本数组；菜单/监控表直接绑本数组字段取址
// （数组基址是链接期常量，不依赖组构造时序，勿改回经 left_motors.motors[i] 绑址）
extern MyMotor left_motor_list[3];
extern MyMotor right_motor_list[3];

// 升降杆机构实例：与底盘 Chassis 平级。命令层直接下发给它，节拍由 RobotAction 推进；
// 监控菜单直接绑它的位置/状态/偏差，故对外可见
extern LiftBar lift_bar;

// 升降杆逐台电机属性数组：组内持指针共用本数组；菜单/监控表直接绑本数组字段取址
// （数组基址是链接期常量，不依赖组构造时序，同左右底盘数组）
extern MyMotor lift_motor_list[4];

// PC 下发过任务（底盘 turn/move/goto、升降杆 lift_to）的标记，不清零：
// 掉线 failsafe 据此把底盘与升降杆一起收掉，两个机构共用一个标记，不分开判。
// 开环命令（set_volt/set_spin/lift_volt）不建任务、不置标记，掉线不收。
extern bool pc_took_control;

// 本车动作命令总表（停止/转向/直行/局部移动）：构造USB_Comm时整张传入。
extern const CMD_ITEM robot_cmds[];
extern const uint8_t robot_cmd_count;

extern void robot_init();

#endif
