#ifndef CHASSIS_H_
#define CHASSIS_H_

#include "my_main.h"

// 里程计读数只取motor1
// MotorGroup 里的电机都用齿轮串连传动，物理等速

// 单台电机的个体属性：一台电机实例 + 逐台标定的电压参数 + 上一拍输出电压。
// 电机组按数组元素逐台读写这几个属性，外界（LCD调参/监控）也直接取其字段地址。
// 无默认初值：作为聚合体由调用方（robot_and_control.cpp）逐台显式填好（gnu++11 要求）。
struct MyMotor
{
    vex::motor *motor; // 电机实例指针（vex::motor 不可拷贝，只能持指针）
    // 电压缩放因子：让组内各电机同指令下输出的轮速一致。
    // 各台斜率 k(pct/V) 有个体差异，且经齿轮传动的电机轮速 = 轴速/r，
    // 等效轮速斜率 = k/r（r=1直连 / 1.25齿轮）。只能削峰填谷——以全局最慢
    // 等效轮速为基准，factor = k_min_等效 / (k_i/r_i)，全部 ≤1，
    // 组内输出上限 1/max(factor) 才不损失极速。
    float volt_factor;
    float volt_max;    // 最大电压
    float volt_min;    // 最小电压（个体死区补偿，对齐各电机开始转动的起点）
    float volt_output; // 目标输出电压
};

class MyMotorGroup
{
private:
public:
    enum SpinState
    {
        Stopped, // 停车或目标死区：0V，环休眠
        Transit, // 目标事件窗：起步 / 换向 / target 大跳，I 不工作   特征:error大且加速度较大
        Track,   // 正常跟踪：I 工作                               特征:error(较)小或加速度(较)小
        Stable,  // 目标稳定：I 大砍,尽量保持稳定                    特征:error小且加速度小
        Recover  // 目标未大跳，但 error 被拉大：负载或跟不上，I 工作  特征:error大且加速度小->突然大->小(经常会推到100%电压)
        // other -> Stopped 仅手动停车 (0pct target不算Stopped)
        // Stop -> Transit  启动
        // Transit -> Track  error小到一定程度 或加速度小到一定程度(加速度小了还没进入小error区间说明缺I不够，得加上I进行追踪)
        // Track -> Stable  在不断追踪后error小且加速度小(说明稳定了,砍kI减小震荡)
        // Other -> Transit  targrt突变，需先等kf释放完
        // Track/Stable -> Recover  负载突变导致转速突变导致error(不符合预期地)突变,需要P和I回馈补偿
        // Track 与 Recover似乎有点模糊
        // 加速度不能丢，加速度是非常重要的判断依据
    };

    SpinState spin_state = Stopped;

    // 组内电机属性表：构造时从入参拷入，容量上限 4
    // public：监控 getter 直接取各台字段做 SDK 读数。实际台数见 motor_count。
    MyMotor motors[4];
    uint8_t motor_count;
    // 电机组始终以 motors[0]（与轮轴直连的那台）的速度作为本组反馈。

    // 摩擦补偿曲线两端：target=0 时用 static，target增大后用 dynamic。
    // 中间连续过渡（target 每增大 3pct，静态→动态多衰减约 63%），全程无跳变。
    float static_deadzone = 1.5f;  // 静止时补偿电压上限(整车下,单位V)
    float dynamic_deadzone = 0.8f; // 高速时补偿电压下限(整车下,单位V)
    float output_deadzone = 0.5f;  // 目标速度死区，单位 pct，目标绝对值小于它时按0处理

    // ---- target突变进入 Transit 状态的判态参数 ----
    // target 跳变判定：相对变化超过此比例算大幅，强制进入提速态若干拍。
    // 例如 0.5 表示 target 变化量超过 max(|target|, target_min_current) * 0.5 即触发。
    float target_jump_ratio = 0.5f;
    // 小 target 时基准被该值兜底，避免 1→2 这种绝对值小但比例大的误触发。
    float target_min_current = 5.0f;
    // ---- 稳定态判态参数 ----
    float speed_stable = 50.0f; // Transit->Track加速度阈值，单位 pct/s方
    float err_dead = 1.0f;      // Track->Stable 误差死区，进入该范围内后，I 大砍，减少震荡

    float i_stable_k = 0.5f; // Stable模式下 I项系数为原先多少

    // 速度稳定判定：速度变化率（pct/s）相对当前速度低于此比例，认为速度已稳定。
    // 例如 0.1 表示速度变化率 < max(|speed|, target_min_current) 的 10% 每秒。
    float speed_stable_ratio = 0.1f;

    // I 项工作范围（误差阈值）：|error| 落在它以内算小误差——I 才开始工作、Transit 才可进 Track。单位 pct。
    // 固定值即可：I 的波动幅度天生与 error 同量纲、与 target 无关，稳定态抖动靠调小 ki 抑制。
    float error_for_i = 2.5f;

    // I 项贡献的步长限幅
    float i_slew = 5.0f; // 5.0代表每次 ki* error 时 error只计算 不超过 5.0的部分

    // 提速态电压斜率上限：比稳定态宽，让电压快速跟上目标跳变，
    float slew_boost = 25.0f; // V/s

    int8_t target_sign = 0; // 上一轮目标方向，用于检测换向与双极性映射；0 表示起步前未知
    float target = 0.0f;    // 目标速度，单位 pct（±100）,外界禁止直接修改
    float fabs_target = 0.0f;

    float kf;                    // 阻力前馈，单位：归一输出 / pct
    float kp;                    // 比例项，单位：归一输出 / pct
    float ki;                    // 积分项，单位：归一输出 / (pct·秒)，与构造入参一致
    float kd;                    // 微分项，单位：归一输出·秒 / pct，与构造入参一致
    float integral = 0.0f;       // integral 保存误差累计，单位 pct·秒（本类内部时间统一为秒）。
    float previous_error = 0.0f; // previous_error 保存上一轮误差，用于计算 D 项。
    float pre_v = 0.0f;          // 上一次速度
    float pre_a = 0.0f;          // 上一次的加速度

    uint32_t last_time_us = 0; // 上一轮 update() 使用的 VEX 系统微秒时间戳。
    // ---- Transit 起步观察窗 ----
    // 新进入 Transit 后先冻结这么久（窗内只采样加速度），避免起步第一拍速度仍为 0 时直接误进 Track。
    uint32_t transit_freeze_us = 20000; // 冻结时长，单位 us（约合 10ms 周期的两拍）
    uint32_t transit_start_us = 0;      // 进入 Transit 的时刻，单位 us

    // motor_list/count 为本组电机表（构造时拷入，上限 4）；后四个为速度环增益。
    MyMotorGroup(const MyMotor *motor_list, uint8_t count,
                 float f, float p, float ki, float kd);

    void setStopping(vex::brakeType brake); // 设置电机刹车类型

    // 在有定位轮后可舍弃
    //  void resetPosition();                   // 重置电机编码器位置
    //  double position();                      // 读取当前编码器位置，单位 rev (取motors[0]的值)

    double velocity(); // 读取当前转速，单位 rpm (取motors[0]的值)

    // 速度环全程使用 pct（±100，100 即满速）：追踪 target（pct），直接用电压控制电机，外界无需做摩擦补偿。
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
    // 硬件由具体车辆创建，Chassis 只保存实际使用的电机组和惯性传感器。
    MyMotorGroup &left_motors;
    MyMotorGroup &right_motors;
    vex::rotation &forward_tracking_sensor;
    vex::rotation &left_tracking_sensor;
    vex::inertial &inertial_sensor;

    // 上一次 update() 的系统时间戳，单位 ms。
    uint32_t previous_time = 0;

    // // 陀螺残余零偏，单位 deg/s；finish_init 静止采样得到，update 每拍从航向中扣除。
    // float gyro_bias = 0.0f;
    // // 零偏补偿的计时起点（finish_init 时刻），单位 us，与传入 update 的 now 同时基。
    // uint32_t heading_origin = 0;

    // reset() 时起始航向 heading_start 的正余弦，update() 用它把全局坐标投影回起点方向
    float cos_i = 1.0f; // 起始航向的余弦，用于计算从起点开始的位移
    float sin_i = 0.0f; // 起始航向的正弦，用于计算从起点开始的位移

public:
    /* 以下调参区间 */

    // 底盘物理参数。轮距预留。
    float wheel_r = 0.0254f;      // 定位轮半径，单位 m（旧车定位轮直径 5.08cm，即 2 英寸）。
    float track_width = 0.28694f; // 左右驱动轮接地点之间的距离，单位 m，实测。
    // 速比现在恰好为1，故没写，后续若不为一需新增。

    // 定位轮到车体旋转中心的有符号距离，单位 m，实测。
    // 前向轮读数只受其右向偏移影响：偏右为正（其偏前 74.315mm 不影响前向读数，无需参数）。
    float forward_tracking_offset = 0.012434f; // 前向定位轮偏右 12.434mm。
    // 侧向轮读数只受其前向偏移影响：偏前为正、偏后为负（其左右居中，无侧向偏移）。
    float side_tracking_offset = -0.021262f; // 侧向定位轮偏后 21.262mm。

    // 左右两侧各自的可达满速，单位 pct，取值 (0,100]，随整车配重分布不同而不同，实测标定。
    // output() 等比限幅以此为基准；位置环 max_output 不应超过两侧较小值。
    float left_max_speed = 100.0f;
    float right_max_speed = 100.0f;

    /* 以上调参区间 */

    // reset() 保存的本次动作起点，只在调用 reset() 时改变。
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
    float left_speed = 0.0f; // 电机组编码器测得的转速，单位 rpm。
    float right_speed = 0.0f;
    float linear_speed = 0.0f;   // 车体中心沿自身前方的线速度，单位 m/s。
    float heading = 0.0f;        // 全局航向角 = IMU 读数 + heading_offset，向右转为正，单位 deg。
    float heading_offset = 0.0f; // 全局航向相对 IMU 零点的软件偏置，单位 deg；上位机 0x86 改航向即改它。
    float imu_heading = 0.0f;    // update() 维护的 IMU 原始累计航向，单位 deg；里程计增量只从它差分。
    float angular_speed = 0.0f;  // IMU 测得的车体转动角速度，单位 deg/s。

    // 传感器角位置乘以周长得到的一维有符号坐标 沿轮的测量方向，前进为正、后退为负 算出的定位轮滚动距离
    float forward_tracking_distance = 0.0f; // 前向定位轮的累计滚动距离，单位 m，有符号。
    float side_tracking_distance = 0.0f;    // 侧向定位轮的累计滚动距离，单位 m，有符号。

    // 二维里程计当前坐标。
    // 初始化时车头方向为 x 正方向，车体右侧为 y 正方向；向右转角度为正。
    float x = 0.0f; // 小车当前在全局坐标系中的 x 坐标，单位 m。
    float y = 0.0f; // 小车当前在全局坐标系中的 y 坐标，单位 m。

    // 3. gap：update() 根据 start、now 或上一轮 now 计算出的可选差值。

    // 3.1 上一次 update() 到本次 update() 之间的变化量。
    uint32_t update_gap = 0;           // 相邻两次 update() 的时间间隔，单位 us。
    float local_forward_change = 0.0f; // 本轮车体中心沿自身前方的位移，单位 m。
    float local_side_change = 0.0f;    // 本轮车体中心沿自身右方的位移，单位 m。
    float heading_change = 0.0f;       // 本轮 update() 中的航向角变化，单位 deg。
    float x_change = 0.0f;             // 本轮在全局坐标系中的 x 位移。
    float y_change = 0.0f;             // 本轮在全局坐标系中的 y 位移。

    // 编码器里程已废弃（恢复点：打滑检测）。
    // float left_distance_change = 0.0f;
    // float right_distance_change = 0.0f;

    // 3.2 最近一次 reset() 保存的 start 到本次 update() 的变化量。
    float distance_from_reset = 0.0f;      // 当前坐标沿起始车头方向相对起点的有符号距离，单位 m。
    float side_distance_from_reset = 0.0f; // 当前坐标沿起始车体右方相对起点的有符号距离，单位 m。
    float heading_from_reset = 0.0f;       // 当前航向相对起始航向的角度变化，向右为正，单位 deg。

    Chassis(MyMotorGroup &left,
            MyMotorGroup &right,
            vex::rotation &forward_tracking,
            vex::rotation &left_tracking,
            vex::inertial &inertial);

    // 设置停车方式并启动惯性传感器校准，不在这里等待。
    void init();

    // 是否校准完成(init的等待环)
    bool is_ready();

    // 所有传感器就绪后设定底盘里程计起点(init的结尾)
    void finish_init();

    // 更新传感器读数，并更新运动状态。
    // now 由调用方传入（与本周期 PID 共用同一时间戳），内部不再自行取时间。
    void update(uint32_t now);

    // 开启新的一段动作：以当前状态为段起点快照（x_start/y_start/heading_start 等），
    // 并清零段内相对量（*_from_reset）与变化量缓存（*_change）。
    // 前置条件：本周期已调用过 update()，快照数据才是新鲜的。
    void begin_segment();

    // 周期外独立使用的组合入口：update(now) + begin_segment()。
    // 仅供没有周期 update 的场景（如 finish_init）使用，周期内禁止调用（会二次 update）。
    void reset(uint32_t now);

    // 速度环唯一目标入口：位置环 PID 与手柄映射都走这里，入参是左右轮目标速度（pct，±100）。
    // 允许输入超过 100 的叠加值，内部按比例缩回；本函数只写目标，闭环由 speed_tick() 常驻任务执行。
    void output(float left_output, float right_output);

    // 速度环每轮推进：由独立常驻任务短周期调用。停车状态下各组内部直接返回。
    void speed_tick();

    // 按 init() 设置的停车方式停止左右电机组。
    void stop();
};

#endif
