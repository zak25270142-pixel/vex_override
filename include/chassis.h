#ifndef CHASSIS_H_
#define CHASSIS_H_

#include "my_main.h"

struct MyMotor
{
    vex::motor *motor; // 电机实例指针（vex::motor 不可拷贝，只能持指针）
    // 电压缩放因子：让组内各电机同指令下输出的轮速一致。
    // 各台斜率 k(pct/V) 有个体差异，且经齿轮传动的电机轮速 = 轴速/r，
    float volt_factor;
    float volt_max;           // 最大电压
    float volt_min;           // 最小电压（个体死区补偿，对齐各电机开始转动的起点）
    float volt_output = 0.0f; // 目标输出电压（活状态，速度环每拍更新）
    MyMotor(vex::motor *m, float factor, float vmax, float vmin)
        : motor(m), volt_factor(factor), volt_max(vmax), volt_min(vmin)
    {
    }
};

class MyMotorGroup
{
private:
public:
    enum SpinState : uint8_t
    {
        Stopped, // 停车：0V + 刹车，环休眠
        Transit, // 目标事件窗：起步 / 换向 / 目标大跳，I 不工作   特征:error大，但是我们自己刚改的主意
        Track,   // 正常跟踪：I 工作                               特征:error(较)小或加速度(较)小
        Stable,  // 目标稳定：I 大砍,尽量保持稳定                    特征:error小且加速度小
        Recover, // 非预期运动：车被外力带偏                         特征:error大且变大   （测试版，判据待实车磨）
        Stall    // 堵转：给满了车也不动                            特征:output 接近上限且轮速低且持续   （测试版，判据待实车磨）

        // ===== 三类处境，按"误差是谁造成的"分 =====
        //   Transit  —— 目标（操作手）造成的：重新建立输出，积分清零，升压受限
        //   Track    —— 没人在闹，或只是小扰动：交给 I 自己吸收
        //   Recover  —— 外力造成的，车还在动：两方向都不限斜率，抢时间补力矩
        //   Stall    —— 外力造成的，车已经不动：顶住电压，松劲方向限速（松了会被弹回来）

        // other -> Stopped  仅手动停车；0 pct 的 target 不算 Stopped，0 由速度环自己收敛
        // Stop -> Transit   启动
        // Other -> Transit  目标事件（见 drive()）：目标大跳且离实际速度远 / 换向
        // Transit -> Track  error小到一定程度 或加速度小到一定程度(加速度小了还没进入小error区间说明缺I不够，得加上I进行追踪)
        // Track -> Stable   error小且加速度小(说明稳定了,砍kI减小震荡)
        // Stable -> Track   error 超出死区
        // Track/Stable -> Recover  外力把车速拉向输出的反方向
        // Recover -> Transit  误差回到 I 工作区（积分已不可信，冻住重新观察）
        // Track/Recover -> Stall  给满 + 轮速低，并持续够久
        // Stall -> Transit  轮速起来了，或输出不再需要顶格
    };

    SpinState spin_state = Stopped;

    // 电机属性表：持调用方数组指针（不拷贝），容量上限 4

    MyMotor *motors;
    uint8_t motor_count;
    // 电机组始终以 motors[0]（与轮轴直连的那台）的速度作为本组反馈。

    // 摩擦补偿曲线两端：target=0 时用 static，target增大后用 dynamic。中间连续过渡
    float static_deadzone = 1.5f;  // 静止时补偿电压上限
    float dynamic_deadzone = 0.8f; // 运动时补偿电压下限
    float output_deadzone = 0.5f;  // 目标速度死区，单位 pct；绝对值小于它按下发0处理

    // ---- 目标事件（Transit）判据 ----
    // 目标变化量超过它才算"目标大跳"，单位 pct。
    float target_jump_abs = 12.0f;

    // ---- 稳定态判态参数 ----
    float speed_stable = 50.0f; // Transit->Track加速度阈值，单位 pct/s；同时作为 Recover/Stall 里的最小加速度门槛
    float err_in_dead = 2.0f;   // Track->Stable 误差死区
    float err_out_dead = 3.0f;  // Stable->Track  误差死区；另被 drive() 复用为"起步目标小于它则跳过 Transit 直接 Track"的阈值
    float i_stable_k = 0.5f;    // Stable模式下 I项系数为原先多少
    // Transit->Track 的判据之一,加速度小于speed_stable 或 误差小于error_for_i时i项就工作
    // 同时是 Recover 的退出条件
    float error_for_i = 8.0f;

    // ---- Transit 起步观察窗 ----
    uint32_t transit_freeze_us = 20000; // 冻结时长 us,避免起步误进 Track。
    uint32_t transit_start_us = 0;      // 进入 Transit 的时刻，单位 us
    uint32_t stall_since_us = 0;        // "给满 + 轮速低"开始成立的时刻；不成立时为 0

    // ---- 非预期运动（Recover）判据 --- 测试版不急于改 ----
    float recover_accel = 200.0f; // 车速被外力带离目标时的加速度门槛，单位 pct/s。
    float stall_output = 0.99f;   // 归一化输出达到它算"已经给到上限"
    float stall_speed = 8.0f;     // 轮速低于它算"轮胎基本没转"，单位 pct。
    uint32_t stall_time = 100000; // "给满 + 轮速低"同时成立进入 Stall的时间，单位 us。

    // ---- 电压斜率上限 ----
    float slew_boost = 25.0f;    // 升压斜率上限，单位 V/s
    float slew_release = 200.0f; // 撤压斜率上限，单位 V/s

    // ---- 目标速度 ----
    int8_t target_sign = 0; // 当前目标方向,0表示期望静止
    float target = 0.0f;    // 目标速度，单位 pct（±100）,外界禁止直接修改
    float fabs_target = 0.0f;

    // ---- 阻力前馈 kf 插值表 ----
    const Vec2f *kf_node;                // 节点数组首地址，调用方持有 x:|target|(pct，与目标同口径) y:前馈输出(归一)
    uint8_t kf_num;                      // 节点数
    float feedforward = 0.0f;            // 本拍前馈输出（= 目标方向×kf_at(|target|)），供监控读取，标定表时直接看它
    float kf_at(float abs_target) const; // 按 |target| 分段线性插值取稳态前馈输出

    // ---- 速度环参数 ----
    float kp;                    // 归一输出 / pct
    float ki;                    // 归一输出 / (pct·秒)
    float kd;                    // 秒 / pct
    float integral = 0.0f;       // integral 保存误差累计，单位 pct·秒
    float previous_error = 0.0f; // 上一轮误差
    float pre_v = 0.0f;          // 上一轮速度
    float pre_a = 0.0f;          // 上一轮加速度
    uint32_t last_time_us = 0;

    MyMotorGroup(MyMotor *motor_list, uint8_t count,
                 const Vec2f *kf_node, uint8_t kf_num,
                 float p, float ki, float kd);

    void setStopping(vex::brakeType brake); // 设置电机刹车类型

    // 在有定位轮后可舍弃
    //  void resetPosition();                   // 重置电机编码器位置
    //  double position();                      // 读取当前编码器位置，单位 rev (取motors[0]的值)

    // 速度环spin追踪pct,下发电压控制电机,周期调用
    void my_spin();

    // 给定目标速度（pct，±100）并确保速度环开始追踪。
    // 外界唯一修改target 入口 禁止直接赋值
    void drive(float target);

    // 停车后会自动 清积分
    void stop();
};

class Chassis
{
private:
    vex::rotation &forward_tracking_sensor;
    vex::rotation &left_tracking_sensor;
    vex::inertial &inertial_sensor;

    // 上一次 update() 的系统时间戳，单位 ms。
    uint32_t previous_time = 0;

    // // 陀螺残余零偏，单位 deg/s；finish_init 静止采样得到，update 每拍从航向中扣除。
    // float gyro_bias = 0.0f;
    // // 零偏补偿的计时起点（finish_init 时刻），单位 us，与传入 update 的 now 同时基。
    // uint32_t heading_origin = 0;

    // begin_segment() 时起始航向 正余弦
    float cos_i = 1.0f; // 起始航向的余弦
    float sin_i = 0.0f; // 起始航向的正弦

public:
    MyMotorGroup &left_motors;
    MyMotorGroup &right_motors;
    /* 以下调参区间 */

    // 底盘物理参数
    float wheel_r = 0.034925f;    // 定位轮半径，单位 m（新车定位轮直径 2.75 英寸）。
    float track_width = 0.29223f; // 左右驱动轮接地点之间的距离，单位 m。

    // 定位轮到车体旋转中心的有符号距离，单位 m。
    float forward_tracking_offset = 0.01023f; // 前向轮的横向偏移，向右为正
    float side_tracking_offset = -0.0889f;  // 侧向轮的纵向偏移，向前为正

    // 左右两侧各自的可达满速，pct
    float left_max_speed = 100.0f;
    float right_max_speed = 100.0f;

    /* 以上调参区间 */

    // begin_segment() 保存的本次动作起点，只在调用 reset() 时改变。
    // 编码器里程已废弃（恢复点：打滑检测），左右驱动轮路程起点一并注释：
    // float left_distance_start = 0.0f; // 累积路程
    // float right_distance_start = 0.0f;
    float heading_start = 0.0f; // 累计航向角，单位 deg。
    float x_start = 0.0f;       // 初始的全局坐标
    float y_start = 0.0f;

    // 最近一次 update() 保存的当前状态，不在同一周期内重复读取硬件。
    // 编码器里程已废弃（恢复点：打滑检测）：
    // float left_distance = 0.0f; // 累积路程，单位 m。
    // float right_distance = 0.0f;

    // 陀螺仪相关
    float heading = 0.0f;        // 全局航向角，向右转为正，deg。
    float heading_offset = 0.0f; // 全局航向相对 IMU 零点的软件偏置
    float imu_heading = 0.0f;    // update() 维护的 IMU 原始累计航向
    float angular_speed = 0.0f;  // IMU 测得的车体转动角速度，单位 deg/s。

    // 定位轮相关
    float forward_tracking_distance = 0.0f; // 前向定位轮的累计滚动距离，单位 m，有符号。
    float side_tracking_distance = 0.0f;    // 侧向定位轮的累计滚动距离，单位 m，有符号。
    float linear_speed = 0.0f;              // 车体中心沿自身前方的线速度，单位 m/s。

    // 二维里程计当前坐标。
    float x = 0.0f; // 全局坐标系中的 x 坐标，单位 m。
    float y = 0.0f; // 全局坐标系中的 y 坐标

    // 上一次 update() 到本次 update() 之间的变化量。
    uint32_t update_gap = 0;           // 相邻两次 update() 的时间间隔，单位 us。
    float local_forward_change = 0.0f; // 本轮车体中心沿自身前方的位移，单位 m。
    float local_side_change = 0.0f;    // 本轮车体中心沿自身右方的位移，单位 m。
    float heading_change = 0.0f;       // 本轮 update() 中的航向角变化，单位 deg。
    float x_change = 0.0f;             // 本轮在全局坐标系中的 x 位移。
    float y_change = 0.0f;             // 本轮在全局坐标系中的 y 位移。

    // 编码器里程已废弃（恢复点：打滑检测）。
    // float left_distance_change = 0.0f;
    // float right_distance_change = 0.0f;

    // 最近一次 reset() 保存的 start 到本次 update() 的变化量。
    float distance_from_reset = 0.0f;      // 当前坐标沿起始车头方向相对起点的有符号距离，单位 m。
    float side_distance_from_reset = 0.0f; // 当前坐标沿起始车体右方相对起点的有符号距离，单位 m。
    float heading_from_reset = 0.0f;       // 当前航向相对起始航向的角度变化，向右为正，单位 deg。

    Chassis(MyMotorGroup &left,
            MyMotorGroup &right,
            vex::rotation &forward_tracking,
            vex::rotation &left_tracking,
            vex::inertial &inertial);

    void init();
    bool is_ready();           // init是否完成
    void finish_init();        // init完成后的收尾
    void update(uint32_t now); // 更新传感器读数与运动状态。
    void begin_segment();      // 以当前状态为段起点快照，前置条件：本周期已调用过 update()
    void reset(uint32_t now);  // 没update场景的开启新任务段的组合调用
    // 速度环唯一目标入口，允许输入超过 100 的叠加值，内部按比例缩回
    void output(float left_output, float right_output);
    void speed_tick(); // 速度环每轮推进
    void stop();
};

#endif
