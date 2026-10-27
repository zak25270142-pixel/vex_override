#ifndef ROBOT_UPPER_H_
#define ROBOT_UPPER_H_

#include "my_motor_group.h" // MyMotor：上位机构复用逐台电机属性表（volt_factor/volt_min）
#include "pid.h"     // PositionPID（通用位置式 PID，此处作速度环用）

// 上位机构总头文件：intake / 升降四连杆（LiftBar，实现见 lift_bar.cpp）/ 夹爪。
// 三类都是"电机组 + 机构特性控制律"，与底盘 Chassis 平级，不属于 RobotAction 动作层。
// 当前只落地升降杆；intake/夹爪等机械确认后往本文件追加。

// ================= 升降杆电机组 LiftMotorGroup =================
// 升降杆专用组：只管逐台输出与整组停机，不含底盘 MotorGroup 的速度环
// 状态机 / kf 前馈。输出映射沿用底盘口径：volt_min + deadzone 沿指令方向
// 垫底，volt_factor 逐台对齐，满幅夹到电池电压（SDK 上限）。
class LiftMotorGroup
{
public:
    MyMotor *motors; // 电机属性表：持调用方数组指针（不拷贝），容量上限 4
    uint8_t motor_count;

    LiftMotorGroup(MyMotor *motor_list, uint8_t count);

    void setStopping(vex::brakeType brake); // 设置电机刹车类型

    // 全组 0V + 当前 stopping
    void stop_motors();

    // 逐台电压直通下发：各台 volt_output 已存指令，按满幅（12V）夹幅后 spin
    void motor_output();

    // 归一输出 → 逐台电压并下发：deadzone + volt_min 沿输出方向垫底，
    // factor 逐台对齐，满幅夹幅
    void motor_output(float output, float deadzone);
};

// ================= 升降杆 LiftBar =================
// 初版只搭开环链路：电压直控 + 纯速度环，共 3 条命令。位置环等实测（阻力曲线、
// 行程端点、摩擦量级）之后再按级联架构加——没有实测数据就整定位置环是瞎调。
//
// 0. 周期结构（与底盘同构：数据与控制分开）：
//    - refresh() 只更新数据，任何状态都跑（停机手动盘杆时监控要看得到位置）。
//    - 控制指令以成员函数指针挂载：set_volt → control_volt，set_speed → control_speed；
//      stop() 与卡死/倾斜故障把指针摘掉。为空时 refresh 只刷数据、不碰输出。
//    - 逐台电压下发统一走 motor_group.motor_output()，本类只算归一输出。
//
// 1. 两种工作态，命令即入口（stop 两用：停止 + 解锁 Stall）：
//    - Volt：逐台电压（V）直通下发，不垫死区、不经 volt_factor，只按满幅
//      夹幅。手动盘单台、扫压测启动电压与阻力、验转向靠它（垫底会污染测量）。
//    - Speed：纯速度环，目标 pct，反馈 = 4 台平均 velocity(pct)，PI 出归一电压。
//      电压模式下转速随负载波动（重力/摩擦），速度环把这层压住；I 项顶住重力。
//
// 2. 卡死判据只有一条（比位置环版简单得多，因为没有"到位保持"这个正常高电流态）：
//    命令明确要动 && 位置不动 && (输出顶格 || 电流超限) && 过起步宽容期 && 持续。
//    "位置不动"是唯一证据：杆速低时正常工作与堵死在转速维度上分不开。
//    触发 → 0V+brake，置 Stall 并摘掉控制指令；重新下发命令或 stop() 才会再出力。
//    brake 不是机械锁（短路制动，阻力矩∝转速、停转即无力），杆可能缓慢下滑，
//    见 lift_bar.cpp 卡死段注释。
//
// 3. 4 台按一个组控制，但机械是"半刚性"：左 2 台抬左侧、右 2 台抬右侧，
//    中间是箱子（平台）——左右可相对倾斜，不是 1 个 DOF。后果：
//    - 4 台平均值会掩盖倾斜（左 100 / 右 110 → 平均 105 看着到位了，实际
//      箱子斜着受剪切力）。所以位置反馈除平均值外，必须单独看左右与差值。
//    - 组内（同侧 2 台）齿轮串联 = 真刚性，其电流差才是纯电机个体差，
//      volt_factor 只在组内等流标定；组间不做等流（重心偏时反而制造倾斜）。
//    - 将来位置环不能只用平均反馈，要做同步控制：整体位置环 + 左右差补偿
//      （左 = PID(平均) − K·tilt，右 = PID(平均) + K·tilt），否则箱子长期受扭。
//    - 当前（初版）左右之间**没有任何机械同步件**（没有那根 80cm 轴），同步刚度
//      只剩箱子本身：任何左右出力差都直接变成箱子的剪切/扭转变形。而开环电压
//      与速度环都只看平均值、不管 tilt，所以 tilt 在初版里**不受控**——必须靠
//      tilt 超限停机兜底（见 tilt_limit），并尽快上同步控制。
//
// 4. 单位与方向：
//    - 位置单位初版 = 电机 rev（pos_factor=1.0 占位）；实测导程后改 mm/rev，
//      目标/限位同步换单位，控制律不动。
//    - 方向只靠电机 reversed 标志：VEXos 同时翻转输出方向与编码器读数
//      （jpearman 论坛确认），装车时哪台转向不对就改它的 bool。
//      4 台读数必须同号（监控表逐台位置直接验）。
//    - output 为归一指令 [-1,1]，到每台电压的映射见 LiftMotorGroup::motor_output()。
//
// 5. 摩擦补偿与前馈分工（两套机制，不合并成一张表）：
//    - 启动段（静摩擦/死区）：阈值现象，与速度无关。机制 = volt_min +
//      deadzone 沿指令方向垫底（motor_output() 里的 base），实测启动电压后标定。
//    - 速度段（动摩擦 + 负载）：连续量，由 kf 前馈表按目标速度给出。
//      升降杆必须带符号——上升要顶重力、下降重力帮忙，两侧不对称，
//      与底盘 kf_at(|target|) 的对称假设不同。表待扫压实测后再加，
//      当前先由速度环 I 项积累顶着（慢但不需要标定数据）。

class LiftBar
{
private:
    // 当前挂载的控制指令：为空 = 无指令，refresh 只刷数据、不碰输出
    void (LiftBar::*control_ptr)() = nullptr;

    uint32_t last_time_us = 0;   // 上拍 control_speed 时刻
    uint32_t pid_last_us = 0;    // 传给 PID 的合成时间戳基准
    uint32_t run_since_us = 0;   // 本次 set_volt/set_speed 时刻（宽容期起算点）
    uint32_t stall_since_us = 0; // 卡死判据开始成立的时刻；不成立为 0
    bool pid_need_reset = false; // 进 Speed 后首拍 reset PID（清积分）
    float pos_prev = 0.0f;       // 上拍位置（相邻拍位移判据）

    void control_volt();  // Volt 态：算指令强度 + 卡死/倾斜判据后下发
    void control_speed(); // Speed 态控制律
    // 卡死/倾斜判据：触发则停机锁死并摘掉控制指令，返回 true
    bool stall_check(uint32_t now_us, bool cmd_wants_move);
    void stall_stop(); // 0V+brake + 置 Stall

public:
    enum State : uint8_t
    {
        Stopped = 0, // 无控制指令：0V + brake
        Volt = 1,    // 电压直控（开环）
        Speed = 2,   // 速度环（PI → 电压）
        Stall = 3    // 卡死/倾斜超限：已 0V+brake 并摘掉控制指令
    };

    State state = Stopped;

    // ---- 升降电机组：逐台输出与整组停机走组内，数据刷新在 refresh() ----
    LiftMotorGroup motor_group;

    // ---- 速度环：误差单位 pct，输出归一，max_output=1.0 ----
    PositionPID pid; // kp/ki 公有，调参表直接绑址

    /* 以下调参区间 */
    float pos_factor = 1.0f;       // 编码器 rev→杆端单位的组级系数（实测导程后改 mm/rev）
    float deadzone = 0.5f;         // 死区补偿电压 V：小输出时沿输出方向垫底
    float stall_pos_eps = 0.001f;  // 相邻两拍位移小于它算"没动"（rev）
    uint32_t stall_time = 40000;   // 判据持续这么久才判卡死（us）
    uint32_t stall_grace = 300000; // 起步宽容期：重载杆起转要时间，期内不判（us）
    float stall_output = 0.95f;    // 归一输出/电压指令达到它算"已经给满"
    float stall_current = 1.5f;    // 4 台平均电流达到它算"在顶"（A）
    float stall_speed_min = 3.0f;  // Speed 态：|目标速度| 小于它不算"命令要动"（pct）
    float tilt_limit = 0.10f;      // 左右位置差超过它判机械过载，立即停机（rev，实测后调）
    /* 以上调参区间 */

    // ---- 活状态（监控表直接绑址）----
    float speed_target = 0.0f; // Speed 态目标（pct，上升为正）
    float speed = 0.0f;        // 4 台平均转速（pct）
    float position = 0.0f;     // 杆端位置 = 4 台平均（rev）
    float pos_left = 0.0f;     // 左组平均位置（rev）：motor_list 前半（0,1）
    float pos_right = 0.0f;    // 右组平均位置（rev）：motor_list 后半（2,3）
    float tilt = 0.0f;         // pos_left − pos_right = 箱子倾斜量（rev）
    float spread = 0.0f;       // 4 台位置两两最大偏差（rev）：组内啮合差，观测用
    float output = 0.0f;       // 本拍归一输出（已夹 ±1）
    float avg_current = 0.0f;  // 4 台平均电流绝对值（A）
    float cur_left = 0.0f;     // 左组平均电流（A，带符号：正=出力，负=被拖动发电）
    float cur_right = 0.0f;    // 右组平均电流（A，带符号）
    float cur_diff = 0.0f;     // cur_left − cur_right：左右力矩差 ∝ 箱子内力（A）
    float jam_pos = 0.0f;      // 卡死发生时的位置（rev）
    float limit_min = 0.0f;    // 行程下端（人工记录/上位机写入，v1 不参与控制）
    float limit_max = 0.0f;    // 行程上端

    LiftBar(MyMotor *motor_list, uint8_t count, float p, float i);

    void init(); // 刹车模式 + 停机态（随 RobotAction::init() 调用）

    // 周期调用（与底盘同拍 10ms）：先更新位置/转速/电流读数（任何状态都跑），
    // 再按挂载的控制指令跑控制；无指令时只刷数据。
    void refresh();

    // 电压直控：逐台电压（V）直通下发——不加死区垫底、不经 volt_factor，
    // 直控的目的就是测出真实启动电压与阻力，任何垫底都会污染测量。
    // 指令直接写各台 volt_output 再下发（同底盘 MotorGroup::set_volt_raw），只按满幅夹幅。
    void set_volt(float v0, float v1, float v2, float v3);

    // 速度环目标：pct。挂载 control_speed。
    void set_speed(float pct);

    // 停止：0V + brake，摘掉控制指令。
    void stop();
};

#endif
