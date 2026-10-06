#ifndef ROBOT_ACTION_H_
#define ROBOT_ACTION_H_

#include "chassis.h"
#include "pid.h"

class Remote_Control; // 前置声明：手动控制只借用摇杆指针，不依赖 Control_func.h

// 整机动作层：只负责执行单个动作（转向、直行、去某点），不负责任务编排与中断。
// 顺序执行、并行、条件等待、超时强制中断等由未来的任务管理类负责：
//   给 move_end_callback 赋回调 → 发起动作 → 每 10 ms 调 refresh() 推进 → 动作自然结束时回调通知发起下一个；
//   需要打断时直接 stop_move()（不触发回调，打断方自行安排后续）。
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

    // 任务执行接力指针：nullptr 为空闲。发起动作时绑定 start_xxx；
    // start_xxx 首轮结束自动接力到对应 update_xxx；、
    // 自然结束经 action_finished，外部打断经 stop_move，前者触发回调。
    // 指针即状态，refresh 直接调用、无需 switch。
    void (RobotAction::*chassis_task_ptr)(uint32_t now) = nullptr;

    // 每个动作分两段，由 chassis_task_ptr 接力调用：
    // start_xxx：发起后的第一轮，设定本段起点、PID reset、输出第一轮结果，
    //            收尾把 chassis_task_ptr 接力到对应 update_xxx。
    // update_xxx：后续每轮，PID update 推进并做稳定完成判定，完成时经 action_finished 收尾。
    void start_turn(uint32_t now);
    void start_move(uint32_t now);
    void start_goto(uint32_t now);
    void update_turn(uint32_t now);
    void update_move(uint32_t now);
    void update_goto(uint32_t now);

    void update_manual(uint32_t now); // 手动控制每轮执行

    // 手动控制引用的手柄摇杆数值，由装配层（robot_and_control.cpp）在构造时注入。
    int32_t *move_axis;
    int32_t *turn_axis;

    // 动作自然结束的统一收尾（含发起时已在容差内、一步未动的瞬间完成）：
    // 停车、清指针、触发 move_end_callback。
    void action_finished();

    // 摇杆值平方映射：保留符号的平方，把 [-127, 127] 映射到 [-100, 100]。
    // 输入绝对值小于 manual_deadzone 时输出 0；
    // 否则把 [deadzone^2, 127^2] 线性映射到 [0, 100]，再加符号。
    float square_map_axis(int v);

public:
    // 底盘实例由 .cpp 用本车硬件构造后传入；里程与速度数据可供 monitor 等直接读取。
    Chassis &chassis;

    // 直线段同时使用距离 PID 推进、航向 PID 差速纠偏；转向段只使用 turn_pid。
    // distance：直行/goto 的纵向推进；heading：直行段差速纠偏；turn：原地/goto 转向。
    PositionPID distance_pid;
    PositionPID heading_pid;
    PositionPID turn_pid;
    // 误差与实际速度连续满足条件 180 ms 后才结束当前步骤，避免刚经过目标就切换。
    StableJudge stable_judge;

    // ---- 动作判定容差，可在发起动作前按任务调整 ----
    float distance_tolerance = 0.008f;     // m
    float linear_speed_tolerance = 0.035f; // m/s
    float heading_tolerance = 1.0f;        // deg，直线段航向保持
    float angle_tolerance = 0.8f;          // deg，原地旋转/goto 最终朝向
    float angular_speed_tolerance = 5.0f;  // deg/s

    // 底盘与手柄摇杆数值均在构造时注入：底盘来自本车硬件，摇杆来自装配层。
    RobotAction(Chassis &chassis, int32_t *move, int32_t *turn);

    // ---- 整机生命周期：main 在主循环前调用 ----
    void init();
    bool is_ready();
    void finish_init();

    // 主循环每 10 ms 调用一次；不等待，每次只更新各底盘参数并推进一轮当前动作。
    void refresh();

    // 是否有动作正在执行（接力指针非空）。只读查询口，状态唯一载体仍是指针，不另设标志。
    bool is_busy() const { return chassis_task_ptr != nullptr; }

    // 任务结束回调：
    // 完成或发起时发现目标已在容差内时调用
    // 外部 stop_move() 打断不触发
    void (*move_end_callback)() = nullptr;

    // 外部主动打断：立即停车并回到空闲，不触发 move_end_callback
    void stop_move();

    // ---- 手动控制----
    float turn_atten = 0.5f;  // 转向衰减系数，0=无衰减，1=全衰减
    int manual_deadzone = 16; // 手动摇杆输入死区

    // 进入手动模式：把注入的摇杆设为 only_value（不触发方向键），
    // 接力指针指向 update_manual，refresh() 每轮自动执行手动控制。
    // is_busy() 此后为真，调用方可据此挡住自动动作。
    // 退出用 stop_move()。
    // 当前实现为 Arcade（单左手摇杆：value_y 前后、value_x 转向）。
    // 迁移到 Tank：改读 manual_right_axis 的 value_y，与左手摇杆的 value_y
    // 分别作为左右轮速度，去掉 turn 项，直接 chassis.output(left, right)。
    void manual();

    // ---- 可直接调用的动作 ----

    // 原地旋转，单位 deg、正值向右。
    // 已在容差内时不起步，按完成处理。限速直接改 turn_pid.max_output。
    void turn(float angle);

    // 直行，单位 m，负值倒车。
    // 已在容差内时不起步，按完成处理。限速直接改 distance_pid.max_output。
    void move(float distance);

    // 平滑移动到局部位姿：x 向前、y 向右，heading 单位 deg、向右为正。
    // 每轮重新瞄准目标点，距离 PID 与转向 PID 同时输出，能够边走边转；
    // 点与最终朝向均已达标时不起步，按完成处理。
    // 限速直接改 distance_pid/turn_pid 的 max_output。
    void goto_local(float x, float y, float heading);
};

#endif
