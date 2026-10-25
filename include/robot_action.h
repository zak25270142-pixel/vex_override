#ifndef ROBOT_ACTION_H_
#define ROBOT_ACTION_H_

#include "chassis.h"
#include "pid.h"
#include "robot_upper.h" // LiftBar：上位机构（与底盘同级，构造时注入）

class Remote_Control; // 前置声明：手动控制只借用摇杆指针，不依赖 Control_func.h

// 整机动作层：执行单个动作（转向、直行、去某点），不负责任务编排与中断。
class RobotAction
{
private:
    // goto_local 的目标：相对发布时刻车体位姿的局部位姿，运行过程中不被改写。
    struct GOTO_LOCAL_TARGET
    {
        float x;       // 向前，单位 m
        float y;       // 向右，单位 m
        float heading; // 最终朝向，向右为正，单位 deg
    };

    // 本轮要到达的目标（均相对 begin_segment 开启的新段）。
    float turn_target = 0.0f; // deg
    float move_target = 0.0f; // m
    GOTO_LOCAL_TARGET goto_target = {};

    // 任务接力指针：nullptr 为空闲；start_xxx 首轮结束接力到对应 update_xxx。
    void (RobotAction::*chassis_task_ptr)(uint32_t now) = nullptr;

    // start_xxx：发起后第一轮，建段+PID reset+首轮输出；update_xxx：后续轮推进与完成判定。
    void start_turn(uint32_t now);
    void start_move(uint32_t now);
    void start_goto(uint32_t now);
    void update_turn(uint32_t now);
    void update_move(uint32_t now);
    void update_goto(uint32_t now);

    void update_manual(uint32_t now); // 手动控制每轮执行

    // 缓停任务
    void stop_tick(uint32_t now);

    // 摇杆数值指针，构造时注入
    int32_t *move_axis;
    int32_t *turn_axis;

    uint32_t stop_start_us = 0; // 本次停止起点，0 表示未计时/未开始
    uint32_t stop_time_us = 0;  // 本次停止时限（us）

    // 动作自然结束的统一收尾：停车、清指针、触发 move_end_callback。
    void action_finished();

public:
    // 底盘实例由 .cpp 用本车硬件构造后传入。
    Chassis &chassis;

    // 升降杆实例由 .cpp 用本车硬件构造后传入（与底盘平级的上位机构）。
    // 控制核心在 LiftBar 自身（升降杆电机组：电压直控 / 速度环），
    // 本层只负责驱动节拍与命令入口。
    LiftBar &lift;

    // 直线段同时使用距离 PID 推进、航向 PID 差速纠偏；转向段只使用 turn_pid。
    // distance：直行/goto 的纵向推进；heading：直行段差速纠偏；turn：原地/goto 转向。
    PositionPID distance_pid;
    PositionPID heading_pid;
    PositionPID turn_pid;
    // 误差与实际速度连续满足条件 180 ms 后才结束当前步骤，避免刚经过目标就切换。
    StableJudge stable_judge;

    // ---- 动作判定容差 ----
    float distance_tolerance = 0.05f;     // m
    float linear_speed_tolerance = 0.08f; // m/s
    float heading_tolerance = 1.0f;       // deg，直线段航向保持
    float angle_tolerance = 0.8f;         // deg，原地旋转/goto 最终朝向
    float angular_speed_tolerance = 5.0f; // deg/s

    // 底盘与手柄摇杆数值均在构造时注入：底盘来自本车硬件，摇杆来自装配层。
    RobotAction(Chassis &chassis, LiftBar &lift, int32_t *move, int32_t *turn);

    // ---- 整机生命周期：main 在主循环前调用 ----
    void init();
    bool is_ready();
    void finish_init();

    // 主循环每周期调用：更新各底盘参数并推进一轮当前动作，不等待。
    void refresh();

    // 是否有动作正在执行（接力指针非空）。
    bool is_busy() const { return chassis_task_ptr != nullptr; }

    // 当前是否手动档（接力指针绑在 update_manual 上）；is_busy 分不清手动与 PC 动作。
    bool is_manual() const { return chassis_task_ptr == &RobotAction::update_manual; }

    // 任务结束回调：完成或发起时发现目标已在容差内时调用；stop_move() 打断不触发。
    void (*move_end_callback)() = nullptr;

    // 停止通道：time=0 立即硬刹（急停）；time>0 缓停
    // 0V 交给速度环 PID 减速
    // 不触发 move_end_callback 缓停期间 is_busy() 为真
    void stop_move(uint32_t time = 0);

    // ---- 手动控制----
    float turn_radius_45 = 1.2f; // 45° 倾角时的转弯半径(m)
    int manual_deadzone = 16;    // 摇杆死区，平方域判据

    // 松杆到硬刹之间的延时，单位 us（当前 50ms）。
    uint32_t manual_stop_delay = 50000;

    // ---- 停止通道参数 ----
    float stop_speed_th = 15.0f; // 硬刹轮速门槛，单位 pct：两组都低于它才刹（近零才刹，不点头）

    // 进入手动模式，退出用 stop_move()
    void manual();

    // ---- 可直接调用的动作 ----

    // 原地旋转，单位 deg、正值向右；已在容差内按完成处理。
    void turn(float angle);

    // 直行，单位 m，负值倒车；已在容差内按完成处理。
    void move(float distance);

    // 平滑移动到局部位姿：x 向前、y 向右，heading 单位 deg、向右为正；
    // 每轮重新瞄准目标点，距离 PID 与转向 PID 同时输出；点与最终朝向均已达标按完成处理。
    void goto_local(float x, float y, float heading);

    // ---- 上位机构：升降杆（薄转发，状态机与控制核心在 LiftBar）----
    // 电压直控：逐台电压（V），顺序同 lift_motor_list。Stall 锁死中忽略。
    void lift_volt(float v0, float v1, float v2, float v3);

    // 速度环目标：pct（上升为正）。Stall 锁死中忽略。
    void lift_speed(float pct);

    // 停止：0V+brake，兼作 Stall 解锁。由命令层 cmd_stop 调用（停止一切）；
    // 故意不进 stop_move()——那个被掉线 failsafe 复用，见 robot_and_control.cpp。
    void lift_stop();
};

#endif
