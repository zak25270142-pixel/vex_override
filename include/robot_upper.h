#ifndef ROBOT_UPPER_H_
#define ROBOT_UPPER_H_

#include "my_motor_group.h" // MotorGroup：速度环 + 摩擦垫底 + kf 前馈 + 开环 set_volt
#include "pid.h"            // PositionPID（位置环，外环）

// 上位机构声明集合头（对标 chassis.h 放 ChassisMotorGroup）：一个机构一个类、一个 .cpp
// （lift_bar.cpp / grab.cpp / intake.cpp 各自实现），声明都聚合在这里。
// 实例由装配层 robot_and_control.cpp 构造，extern 声明在 robot_and_control.h。
// 机构之间不互相调用——跨机构配合（抬杆到位 + 走到位 → 放夹爪）由任务流编排。

// ================= 升降杆 LiftBar : MotorGroup =================
// 四连杆机构整体（4 台 36:1 刚性一组，位置只取 motors[0] 代表）。
// 继承 MotorGroup 复用速度环、摩擦垫底、kf 前馈、抗饱和、开环 set_volt；
// 本层只加机构专属：位置环（外环）、四态机、重力前馈、行程软限位。
// 派生方式同 ChassisMotorGroup：公有继承 + 同名遮蔽，无虚函数。
// 位置单位一律 raw：ratio36_1 下 1 rev = 1800 raw。
class LiftBar : public MotorGroup
{
public:
    enum State : uint8_t
    {
        Stoped = 0,  // 已断电：无任务、本拍不下发（开环电压只在此态能保持）
        Lift = 1,    // 位置环追踪目标中（|误差| > tol_raw）
        Hold = 2,    // 已在容差内，持续供电保持；掉出 tol_out_raw 回 Lift
        Stoping = 3  // stop() 后受控回落中，到底才断电
    };
    State state = Stoped;

    // ---- 位置环（外环）：误差 raw，输出 = 目标速度 pct，喂给继承来的速度环 ----
    PositionPID pos_pid; // kp/ki/kd 公有，调参表直接绑址

    // ---- 位置：raw ----
    float position = 0.0f;   // 本拍位置（motors[0] 代表）
    float pos_target = 0.0f; // 位置目标，已 clamp 进行程
    float pos_error = 0.0f;  // 本拍位置误差 = pos_target − position

    // ---- 重力前馈表：x = 位置 raw（升序），y = 该高度悬停所需补充电压 V ----
    // 每拍写入基类的 base_bias；速度环 I 项只补没抵消干净的残余。开环测、闭环用。
    const Vec2f *bias_node;
    uint8_t bias_num;
    float bias_at(float pos) const;
    float gravity_bias = 0.0f; // 本拍前馈输出，V（监控用）

    /* 以下调参区间 */
    float tol_raw = 10.0f;      // 到位容差，raw（±3cm 实测换算后填）
    float tol_out_raw = 13.0f;  // 迟滞：误差超它才退出 Hold，raw
    float jump_raw = 100.0f;    // 目标跳变超它则 reset 位置环积分，raw
    float lim_min = 0.0f;       // 行程下端，raw（软限位，实测填）
    float lim_max = 5400.0f;    // 行程上端，raw（3 rev 占位，实测填）
    float stop_speed = -20.0f; // Stoping 回落目标速度，pct（负 = 下降）
    float stop_eps = 2.0f;     // 到底判据：position ≤ lim_min + stop_eps，raw
    /* 以上调参区间 */

    bool arrived = false; // 本拍是否在容差内（观测）

    LiftBar(MyMotor *motor_list, uint8_t count,
            const Vec2f *kf_node, uint8_t kf_num,
            const Vec2f *bias_node, uint8_t bias_num,
            float p, float i, float d,
            float pos_p, float pos_i, float pos_d);

    void init(); // 刹车模式 + 断电态

    // 每拍推进：读位置 → 状态机 → 位置环 → 速度环（继承）→ 下发。
    void lift_tick(uint32_t dt_us, float dt_s);

    // 位置命令：抬到 target_raw 并持续保持，任务不结束；可重复下发改目标。
    void lift_to(float target_raw);

    // 遮蔽基类 stop()：起回落任务，不立即断电——重力大摩擦小，直接 0V 就是自由落体。
    // 真断电在到底那一步走 MotorGroup::stop()。
    void stop();

    void hold(); // 保持不抽离为命令，等价于 lift_to(当前位置)

private:
    bool pos_need_reset = false; // 下拍位置环 reset（唤醒 / 目标大跳时置位）
    uint32_t pid_last_us = 0;    // 位置环合成时间戳基准（dt 累加，不取钟）

    void spin_to(float speed_req, float dt_s); // 设速度目标 → 速度环推进 → 下发
    void run_pos(uint32_t dt_us, float dt_s);  // Lift / Hold
    void run_stop(float dt_s);                 // Stoping：回落 + 到底判据
    void power_off();                          // 到底：MotorGroup::stop() + 置 Stoped
};

// ================= 夹爪 / 吸球（待加） =================
// 同规格：class Grab : public MotorGroup / class Intake : public MotorGroup，实现放各自的 .cpp。

#endif
