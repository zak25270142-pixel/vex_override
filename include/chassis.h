#ifndef CHASSIS_H_
#define CHASSIS_H_

#include "my_main.h"

// motor1/2 与轮轴同轴直连（60 齿），motor3/4 经齿轮传动（48 齿），
// 里程计读数只取直连轮轴、无啮合背隙的 motor1；后期改用定位轮后该读数可废弃。
class MyMotorGroup
{
private:
    // 绑定构造传入的四台电机
    vex::motor *motors[4];
    bool is_stoped = true;

public:
    // 电压缩放因子
    float volt_factor[4] = {1.0f, 1.0f, 1.2f, 1.2f};

    // 判断“要求电压”和“实际电压”是否明显不一致的容差
    float voltage_tolerance = 0.2f;

    // 关于速度环与电压控制，将在本类完成

    //-----pid速度环------------------------------------
    // 速度环全程使用 pct（±100，100 即满速）：目标来自外部指令，反馈直接读电机
    // 电压限幅与映射
    float volt_max[4] = {12.0f, 12.0f, 12.0f, 12.0f}; // 最大电压(实测，空载时)
    float volt_min[4] = {1.0f, 1.0f, 1.0f, 1.0f};     // 最小电压(实测，空载时)
    float volt_output[4] = {0.0f, 0.0f, 0.0f, 0.0f};  // 上一轮的输出电压(单位V)，和实际比较如果没达到可能就是被限了，会影响限幅
    float static_deadzone = 2.0f;                     // 克服静摩擦所需电压(整车下,单位V)
    float dynamic_deadzone = 1.0f;                    // 克服动摩擦所需电压(整车下,单位V)
    float output_deadzone = 0.1f;                     // 目标速度死区，单位 pct，目标绝对值小于它时按0处理

    // 编码器运动状态确认阈值，直接对原生 rpm 读数判定：
    bool is_moving = false;
    float moving_confirm_speed = 0.15f;  // 静止到运动以转速大于该值确认为运动状态，单位 rpm。
    float stopped_confirm_speed = 0.03f; // 运动到静止以转速小于该值确认为静止状态，单位 rpm。

    float target = 0.0f; // 目标速度，单位 pct（±100）

    float kf;                    // 阻力前馈，单位：归一输出 / pct
    float kp;                    // 比例项，单位：归一输出 / pct
    float ki;                    // 积分项，内部单位：归一输出 / (pct·us)
    float kd;                    // 微分项，内部单位：归一输出·us / pct
    float integral = 0.0f;       // integral 保存误差累计；本类所有时间量统一使用 us。
    float previous_error = 0.0f; // previous_error 保存上一轮误差，用于计算 D 项和判断误差是否越过零点。
    uint32_t last_time_us = 0;   // 上一轮 update() 使用的 VEX 系统微秒时间戳。
    // 其他参数待补充
    //--------------------------------------------------------

    MyMotorGroup(vex::motor &m1, vex::motor &m2,
                 vex::motor &m3, vex::motor &m4,
                 float f, float p, float ki, float kd);

    void setStopping(vex::brakeType brake); // 设置电机刹车类型

    // 在有定位轮后可舍弃
    //  void resetPosition();                   // 重置电机编码器位置
    //  double position();                      // 读取当前编码器位置，单位 rev (取motors[0]的值)

    double velocity(); // 读取当前转速，单位 rpm (取motors[0]的值)

    // 速度环本体：追踪 target（pct），直接用电压控制电机，外界无需做摩擦补偿。
    void my_spin();

    // 给定目标速度（pct，±100）并确保速度环开始追踪。
    // 停车后首次调用会自动 my_respin() 清积分起步；持续运行期间只更新目标。
    void drive(float target);

    void stop();

    void my_respin();
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

    // reset() 时起始航向 heading_start 的正余弦，update() 用它把全局坐标投影回起点方向
    float cos_i = 1.0f; // 起始航向的余弦，用于计算从起点开始的位移
    float sin_i = 0.0f; // 起始航向的正弦，用于计算从起点开始的位移

public:
    /* 以下调参区间 */

    // 底盘物理参数。轮距预留。
    float wheel_r = 0.041275f;    // 轮半径，单位 m（驱动轮/定位轮等径，实测）。
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
    float linear_speed = 0.0f;  // 车体中心沿自身前方的线速度，单位 m/s。
    float heading = 0.0f;       // IMU 测得的累计航向角，向右转为正，单位 deg。
    float angular_speed = 0.0f; // IMU 测得的车体转动角速度，单位 deg/s。

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
    float x_change = 0.0f;             // 本轮在全局坐标系中的 x 位移，单位 m。
    float y_change = 0.0f;             // 本轮在全局坐标系中的 y 位移，单位 m。

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
