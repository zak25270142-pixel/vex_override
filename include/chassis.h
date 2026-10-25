#ifndef CHASSIS_H_
#define CHASSIS_H_

#include "my_motor_group.h" // MotorGroup / MyMotor

// ============================================================
// 底盘电机组 ChassisMotorGroup：MotorGroup 的派生层，叠一轮式专属状态机
// （本类与 Chassis 同属"底盘"，故与它同文件；实现见 chassis.cpp。升降杆等
//   其它机构不共用本类，它们直接用 MotorGroup 或另行派生。）
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
//   refresh()/calc_output() 是基类提供的前后半段（采样+前置 / 基础计算），
//   本层在中间插状态机，并在其后接管积分策略（抗饱和、缩放）。
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

    // ---- 目标跳变：纯命令事件，只看"这次指令比上次变了多少" ----
    // 放这一层而不是基类：它带阈值，是个判断。基类只算量、不下结论。
    float target_jump_abs = 12.0f; // 大跳阈值，pct

    // ---- 状态判据（沿用实车标定值）----
    float speed_stable = 50.0f;   // Transit→Track 加速度阈值，pct/s；兼 Recover/Stall 最小加速度门槛
    float err_in_dead = 2.0f;     // Track→Stable 误差死区，pct
    float err_out_dead = 3.0f;    // Stable→Track 误差死区，pct；另被 set_target 复用为"小目标跳过 Transit"
    float i_stable_k = 0.5f;      // Stable 下新增积分的缩放系数
    float error_for_i = 8.0f;     // 误差小于它（或加速度小于 speed_stable）→ I 开始工作；兼 Recover 退出条件
    float recover_accel = 200.0f; // 车速被外力带离目标时的加速度门槛，pct/s
    float stall_speed = 8.0f;     // 轮速低于它算"基本没转"，pct
    uint32_t stall_time = 100000; // 给满 + 轮速低 持续这么久进 Stall，us

    // ---- Transit 起步观察窗 ----
    uint32_t transit_freeze_us = 20000; // 冻结时长，避免起步误进 Track
    uint32_t transit_start_us = 0;      // 进入 Transit 的时刻
    uint32_t stall_since_us = 0;        // "给满+轮速低"开始成立的时刻；不成立为 0

    // ---- 压摆标定值：只有"慢/快"两个，不是"升/降"两个 ----
    // 常规运行升压慢（护齿箱、防打滑）、撤压快（别拖着不松劲）；Stall 反过来用。
    // 所以按快/慢命名，哪一方向用哪个由 slew_policy() 按状态决定。
    // 调参表绑这两个（不是基类的 slew_boost / slew_release——那两个是每拍被覆盖的生效值）。
    float slew_slow = 25.0f;  // 慢斜率，V/s
    float slew_fast = 200.0f; // 快斜率，V/s

    ChassisMotorGroup(MyMotor *motor_list, uint8_t count,
                      const Vec2f *kf_node, uint8_t kf_num,
                      float p, float i, float d);

    // 存储目标后，按状态机决定起步 / 换向 / 大跳是否进 Transit
    void set_target(float t);

    // 六态机版速度环：refresh（采样+前置）→ 状态机① → 基础计算 → 积分策略
    //   → 状态机② → 压摆。
    // 只算本组可调电压，不执行、不限幅。停车态也采样（监控要看真转速），但不控。
    void calc_output();

    // 停机：清状态机状态，再调基类清积分与输出
    void stop();

    // 停车态查询：底盘的"停"由状态机定义（Stopped 态环休眠）。
    // 只在派生层有这个概念，基类不再声明同名的假实现。
    bool is_stopped() const { return spin_state == Stopped; }

    // 按当前状态把压摆策略写进基类的 slew_boost / slew_release（每拍一次）：
    //   Stopped 0/0（电压钉死）、Recover 无穷/无穷（不限速）、Stall 快/慢（顶住快松劲慢）、
    //   其余 慢/快（常规）。数值即策略，基类不认识枚举。
    void slew_policy();
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

    // 上一次 update() 的系统时间戳，单位 ms。
    uint32_t previous_time = 0;

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
    bool is_ready();           // init是否完成
    void finish_init();        // init完成后的收尾
    void update(uint32_t now); // 更新传感器读数与运动状态
    void begin_segment();      // 以当前状态为段起点快照，前置条件：本周期已调用过 update()
    void reset(uint32_t now);  // 没update场景的开启新任务段的组合调用
    // 速度环唯一目标入口，允许输入超过 100 的叠加值，按两侧最大值同比例缩回
    void output(float left_output, float right_output);
    void speed_tick(); // 速度环每轮推进：算电压 → 横向限幅 → 下发
    void stop();
};

#endif
