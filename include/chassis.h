#ifndef CHASSIS_H_
#define CHASSIS_H_

#include "my_motor_group.h"

// ============================================================
// 底盘电机组 ChassisMotorGroup：MotorGroup 的派生层，叠一轮式专属状态机
// （本类与 Chassis 同声明于 chassis.h，但实现独立成 chassis_motor_group.cpp，
//   遵循"一个 .cpp 只服务一个类"。升降杆等其它机构不共用本类，它们直接用
//   MotorGroup 或另行派生。）
//
// 【本层做什么】按"误差是谁造成的"分类，决定 I 项是否工作、积分是否清零、压摆策略：
//     Stopped 停车（0V+刹车，环休眠）
//     Transit 起步/换向/目标大跳（操作手造成的：I 不工作，升压受限）
//     Track   正常跟踪（交给 I 吸收）
//     Stable  目标稳定（只缩小新增积分，防抖）
//     Recover 外力带偏、车还在动（两方向不限斜率，抢时间补力矩）
//     Stall   外力顶死、车已不动（顶住电压；松劲方向限速，松了会被弹回来）
//
// 【为什么单独一层】状态机是底盘独有的语义——升降杆没有"被外力带偏"这回事，
//   不该拿它污染基础层。
//
// 【派生方式：普通的公有继承 + 同名遮蔽，没有虚函数】
//   调用方（Chassis）持的是本具体类型而不是 MotorGroup&，所以 left_motors.set_target()
//   在编译期就绑到本类；基类那份不会被误调到，也不需要 vtable 与间接跳转。
//   代价：不能通过 MotorGroup* 调到本层语义——本工程已无此需求（联合体已拆掉）。
//   基类的 stop()/set_target() 被遮蔽但仍可用，遮蔽版开头显式调用它们；
//   基类提供 refresh()（采样+前置+算输出）与 apply_output()（映射下发），
//   本层在中间插状态机，并在其后接管积分策略（抗饱和、缩放）；
//   压摆窗口也在本层（apply_output 遮蔽版）：先按 slew_rise/fall 收口，再交给基类。
// ============================================================
class ChassisMotorGroup : public MotorGroup
{
public:
    enum SpinState : uint8_t
    {
        Stopped,
        Transit,
        Track,
        Stable,
        Recover,
        Stall
    };
    SpinState spin_state = Stopped;
private:
    // 计时累加器的"还没开始计"哨兵
    static constexpr uint32_t dur_unstart = 0xFFFFFFFFu;
    void set_spin_state(ChassisMotorGroup::SpinState s);

public:

    // ---- 目标跳变：纯命令事件，只看"这次指令比上次变了多少" ----
    // 放这一层而不是基类：它带阈值，是个判断。基类只算量、不下结论。
    float target_jump_abs = 12.0f; // 大跳阈值，pct

    // ---- 状态判据（沿用实车标定值）----
    float speed_stable = 50.0f;   // Transit→Track 加速度阈值，pct/s；兼 Recover/Stall 最小加速度门槛
    float err_in_dead = 2.0f;     // Track→Stable 误差死区，pct
    float err_out_dead = 3.0f;    // Stable→Track 误差死区，pct；另被 set_target() 起步唤醒复用为"小目标/0 直接 Track"
    float i_stable_k = 0.5f;      // Stable 下新增积分的缩放系数
    float error_for_i = 8.0f;     // 误差小于它（或加速度小于 speed_stable）→ I 开始工作；兼 Recover 退出条件
    float recover_accel = 200.0f; // 车速被外力带离目标时的加速度门槛，pct/s
    float stall_speed = 8.0f;     // 轮速低于它算"基本没转"，pct
    uint32_t stall_time = 100000; // 给满 + 轮速低 持续这么久进 Stall，us

    // ---- Transit 起步观察窗 ----
    uint32_t transit_freeze_us = 20000; // 冻结时长，避免起步误进 Track
    // 进了 Transit 之后"真正计入"的时长，us。**累加出来的**，不是"现在时刻 − 进入时刻"：
    // 状态机要的是"过了多久"这个时长，那就该由每拍的 dt 累加得到。一旦写成两个绝对
    // 时刻相减，就逼着"进入 Transit"那一刻（在 set_target 里、甚至来自上位机命令）
    // 去取一次时钟，凭空多出一个时间源。
    // 初值 dur_unstart = "还没开始计"：进场那一拍只把它置 0、不加 dt——
    // 因为命令往往落在两拍之间，那一整拍 dt 并不属于新状态。
    uint32_t transit_dur_us = dur_unstart;
    // 同上：给满 + 轮速低 已经持续了多久，us。条件不成立时退回 dur_unstart。
    uint32_t stall_dur_us = dur_unstart;

    // ---- 压摆标定值：当前策略只用"慢/快"两档，不是"升/降"两个方向 ----
    // 常规运行升压慢（护齿箱、防打滑）、撤压快（别拖着不松劲）；Stall 反过来用。
    // 所以按快/慢命名，哪一方向用哪个由 set_spin_state() 按状态决定。
    // 调参表绑这两个（不是 slew_rise / slew_fall——那两个是翻转时被状态写好的生效值）。
    // 两档只是当前状态机的需要，不是结构限制：将来要按状态细分，加字段 + 改 set_spin_state()
    // 即可，apply_output 收的仍是"一份速率"，不用动。
    float slew_slow = 25.0f;  // 慢斜率，V/s
    float slew_fast = 200.0f; // 快斜率，V/s

    // 本拍生效值，由 set_spin_state() 在状态翻转时一并写好（不是每帧重算）。
    // 初值取"不限速"：没经历过任何翻转就不该凭空限速，与基类"不认识压摆"的口径一致。
    float slew_rise = INFINITY; // 幅值增大方向，V/s
    float slew_fall = INFINITY; // 幅值减小方向，V/s

    // 积分增益：仅 Stable 打折（i_stable_k）防抖，其余态满增益（1.0f）。
    // 同 slew，由 set_spin_state() 在状态翻转时一并写好，不是每帧重算。
    float i_scale = 1.0f;

    ChassisMotorGroup(MyMotor *motor_list, uint8_t count,
                      const Vec2f *kf_node, uint8_t kf_num,
                      float p, float i, float d);

    // 目标入口兼驾驶会话入口：Stopped 收到任何目标（含 0）都唤醒起步
    // （小目标直接 Track 钉住，大目标 Transit），运行中判换向 / 大跳是否进 Transit。
    // 底盘同启同停由 Chassis::output() 两侧同调保证。
    void set_target(float t);

    // 六态机版速度环：基类 refresh（采样+前置+算 output_raw）→ 状态机①
    //   → 积分策略 → 状态机② → 压摆。
    // 只算 output_raw，不执行、不限幅。Stopped 整个不进来（一行都不走），
    // 残留由 stop() 一次清干净。
    // dt_us / dt_s 是同一个量的两种单位，由调用者一次算好；本层不取钟。
    // 状态机计时只用"累加"，所以既不需要绝对时刻，也就不需要时钟源。
    void refresh(uint32_t dt_us, float dt_s);

    // 执行：把 request 收进压摆窗口（锚点 = output_applied）后交给基类映射下发。
    // 压摆是策略——"该多快"按状态判——所以留在本层；基类只认算好的可调电压。
    void apply_output(float request);

    // 停机：清状态机状态，再调基类清积分与输出
    void stop();
};

// ============================================================
// 底盘：里程计（双定位轮 + IMU）+ 位置环 + 左右两组速度环
//
// 【职责】感知（定位轮/IMU）与位姿推算；把位置环算出的左右速度交给左右组；
//   并且——因为只有它同时看得到左右两侧——负责两组之间的横向协调：
//     ① 速度指令缩回（output()）：任一指令超本侧可达满速，则两侧同比例缩回；
//     ② 可调电压比例限幅（speed_tick()）：垫底 + 可调电压不许超电池电压，
//        超了同比例缩，保两侧出力比例（单边削平会跑偏）。
//   这两件事合起来只有十几行，不值得单开一个"联合体"类。
//
// 【不做】不自己算电机电压：速度环算法在 MotorGroup（或其派生 ChassisMotorGroup），
//   底盘只负责"回读 → 横向限幅 → 下发"。
//
// 【可达满速】在 MotorGroup::max_speed（pct），左右各一份。
// ============================================================
class Chassis
{
private:
    vex::rotation &forward_tracking_sensor;
    vex::rotation &left_tracking_sensor;
    vex::inertial &inertial_sensor;

    // begin_segment() 时起始航向正余弦
    float cos_i = 1.0f; // 起始航向的余弦
    float sin_i = 0.0f; // 起始航向的正弦

public:
    // 左右两组（机械上非耦合，靠地面/车体协调）。持引用而非指针：
    // 两组都是整机装配期创建的静态实例，生命周期长于 Chassis，不存在空指针场景。
    // 具体类型而非 MotorGroup&：速度环调用要走六态机版的遮蔽函数，不需要虚函数。
    ChassisMotorGroup &left_motors;
    ChassisMotorGroup &right_motors;

    /* 以下调参区间 */

    // 底盘物理参数
    float wheel_r = 0.034925f;    // 定位轮半径，单位 m（新车定位轮直径 2.75 英寸）
    float track_width = 0.29223f; // 左右驱动轮接地点之间的距离，单位 m

    // 定位轮到车体旋转中心的有符号距离，单位 m
    float forward_tracking_offset = 0.01023f; // 前向轮的横向偏移，向右为正
    float side_tracking_offset = -0.0889f;    // 侧向轮的纵向偏移，向前为正

    /* 以上调参区间 */

    // 本拍两侧指令缩回系数：1.0 = 未缩回；>1 说明指令超了本侧可达满速。
    // 只作观测——频繁 >1 说明位置环给的速度长期超出机械能力。
    float spd_scale = 1.0f;

    // 本拍两侧可调电压限幅系数：1.0 = 未限幅；>1 说明有一侧垫底+可调要超 12V。
    // 与 spd_scale 是两件事，分开记才看得出是哪一层在削。
    float volt_scale = 1.0f;

    // begin_segment() 保存的本次动作起点，只在调用 reset() 时改变
    float heading_start = 0.0f; // 累计航向角，单位 deg
    float x_start = 0.0f;       // 初始的全局坐标
    float y_start = 0.0f;

    // 陀螺仪相关
    float heading = 0.0f;        // 全局航向角，向右转为正，deg
    float heading_offset = 0.0f; // 全局航向相对 IMU 零点的软件偏置
    float imu_heading = 0.0f;    // update() 维护的 IMU 原始累计航向
    float angular_speed = 0.0f;  // IMU 测得的车体转动角速度，单位 deg/s

    // 定位轮相关
    float forward_tracking_distance = 0.0f; // 前向定位轮的累计滚动距离，单位 m，有符号
    float side_tracking_distance = 0.0f;    // 侧向定位轮的累计滚动距离，单位 m，有符号
    float linear_speed = 0.0f;              // 车体中心沿自身前方的线速度，单位 m/s

    // 二维里程计当前坐标
    float x = 0.0f; // 全局坐标系中的 x 坐标，单位 m
    float y = 0.0f; // 全局坐标系中的 y 坐标

    // 上一次 update() 到本次 update() 之间的变化量
    uint32_t update_gap = 0;           // 相邻两次 update() 的时间间隔，单位 us
    float local_forward_change = 0.0f; // 本轮车体中心沿自身前方的位移，单位 m
    float local_side_change = 0.0f;    // 本轮车体中心沿自身右方的位移，单位 m
    float heading_change = 0.0f;       // 本轮 update() 中的航向角变化，单位 deg
    float x_change = 0.0f;             // 本轮在全局坐标系中的 x 位移
    float y_change = 0.0f;             // 本轮在全局坐标系中的 y 位移

    // 最近一次 reset() 保存的 start 到本次 update() 的变化量
    float distance_from_reset = 0.0f;      // 当前坐标沿起始车头方向相对起点的有符号距离，单位 m
    float side_distance_from_reset = 0.0f; // 当前坐标沿起始车体右方相对起点的有符号距离，单位 m
    float heading_from_reset = 0.0f;       // 当前航向相对起始航向的角度变化，向右为正，单位 deg

    Chassis(ChassisMotorGroup &left, ChassisMotorGroup &right,
            vex::rotation &forward_tracking,
            vex::rotation &left_tracking,
            vex::inertial &inertial);

    void init();
    bool is_ready();              // init是否完成
    void finish_init();           // init完成后的收尾
    void update(uint32_t dt_us);  // 更新传感器读数与运动状态；dt_us 由调用者（RobotAction）给
    void begin_segment();         // 以当前状态为段起点快照，前置条件：本周期已调用过 update()
    void reset(uint32_t dt_us);   // 没update场景的开启新任务段的组合调用
    // 速度环唯一目标入口，允许输入超过 100 的叠加值，按两侧最大值同比例缩回。
    // 兼驾驶会话入口：Stopped 组收到目标（含 0）即唤醒钉住；停车/放手走 stop()（brake + 休眠）。
    void output(float left_output, float right_output);
    // 速度环每轮推进：算电压 → 横向限幅 → 下发。两组同启同停，停着整拍直接返回。
    // 时间参数只是转交给两组（状态机计时与 PID/压摆都只要步长），本层自己不用。
    void speed_tick(uint32_t dt_us, float dt_s);
    void stop();
};

#endif
