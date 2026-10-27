#ifndef MY_MOTOR_GROUP_H_
#define MY_MOTOR_GROUP_H_

#include "my_main.h"

struct MyMotor
{
    vex::motor *motor;        // 电机实例指针（vex::motor 不可拷贝，只能持指针）
    float volt_factor;        // 出力对齐系数：让组内各电机同指令下输出的转速一致。
    float volt_min;           // 个体死区补偿电压，V。对齐各台开始转动的起点
    float volt_output = 0.0f; // 实际下发的电压，V。活状态，由下发方法写入，监控表直接绑它
    MyMotor(vex::motor *m, float factor, float vmin)
        : motor(m), volt_factor(factor), volt_min(vmin)
    {
    }
};

// 一个组 = 一组机械刚性的电机（同转速），所以反馈只取一台代表、指令整组同发。
// 不刚性的机构要拆成多个组 + 上层同步，别在组内平均。边界与理由见
// docs/代码补充注释/01-MyMotorGroup-速度环与状态机.md 第 9 节。
class MotorGroup
{
private:
    // 组内最大的个体死区补偿，V。
    float volt_min_max = 0.0f;

public:
    // --- 持有电机 ---
    MyMotor *motors;
    uint8_t motor_count;

    // --- 目标 ---
    float target = 0.0f;
    int8_t target_sign = 0;
    int8_t target_sign_prev = 0; // 上一次的符号。只是历史量，换向与否由上层判
    float fabs_target = 0.0f;
    // 本次目标减上次目标，pct，带符号。老值在这里存好，上层不必自己留旧值再相减；
    // 但它是量不是判据——大不大跳、算不算换向，全在上层比较。
    float target_jump = 0.0f;
    float target_deadzone = 0.5f; // 目标死区，pct。|target| 小于它按 0 处理

    // 可达满速，pct。
    float max_speed = 100.0f;

    // --- 摩擦补偿 ---
    // 中间按 exp(-|target|/3) 连续过渡
    float static_deadzone = 1.5f;  // 静止时补偿电压上限，V
    float dynamic_deadzone = 0.8f; // 运动时补偿电压下限，V

    // --- 阻力补偿与前馈 ---
    const Vec2f *kf_node; // x = 带符号升序目标速度，y = 稳态前馈（V，带符号）
    uint8_t kf_num;
    float feedforward = 0.0f;        // 本拍前馈输出，V。监控/标定表时直接看它
    float kf_at(float target) const; // 按带符号 target 分段线性插值

    float base_bias = 0.0f; // 上层注入的组级静态偏置V，意思是需要的额外的电压补偿。
    float base_volt = 0.0f; // 本拍摩擦垫底 + base_bias，V
    float headroom = 0.0f;  // 本拍可调电压的物理余量

    // --- 速度环参数（V 量纲）---
    float kp; // V / pct
    float ki; // V / (pct·s)
    float kd; // V·s / pct

    // --- 本拍采样与 PID 前置（refresh 一次算齐，后面谁用谁读，不再现算）---
    float now_v = 0.0f;      // 本拍转速，pct，取代表台 motors[0]（前提见类注释）
    float now_a = 0.0f;      // 本拍加速度，pct/s。用上一拍已提交的 pre_v 差分得到
    float error = 0.0f;      // 本拍误差 = target − now_v，pct
    float derivative = 0.0f; // 本拍误差变化率 = (error − previous_error) / dt，pct/s

    float dt_s = 0.0f; // 本拍步长，由 refresh 传入（本类不取钟、不夹取）

    // --- PID 状态 ---
    float integral = 0.0f;       // 误差累计，pct·s
    float previous_error = 0.0f; // 上一轮误差，pct
    float pre_v = 0.0f;          // 上一拍转速（refresh 提交）
    float pre_a = 0.0f;          // 上一拍加速度（refresh 提交），六态机判 Transit 用

    // ---------- 本拍请求的可调电压（V，未限幅）----------
    float output_raw = 0.0f;     // 本拍请求的可调电压，V（未限幅）
    float output_applied = 0.0f; // 最近一次真正下发的可调电压，V。派生层拿它当压摆锚点

    MotorGroup(MyMotor *motor_list, uint8_t count,
               const Vec2f *kf_node, uint8_t kf_num,
               float p, float i, float d);

    // 外界唯一改目标的入口
    void set_target(float t);

    // ---------- 每拍刷新：采样 + PID 前置 + 算 output_raw（不碰 integral）----------
    // 只在这一处读编码器 —— 状态判据与 PID 因此必然用同一个采样点，不会出现
    // "判据读一次编码器、控制律再读一次"的两个样本。
    // 顺手把控制律要用的一切前置算好存字段：error / derivative / base_volt /
    // feedforward / headroom。它们都只是 (target, now_v, 参数) 的函数，没有代价，
    // 也不该由使用方各算一遍——同一件事算两遍，迟早算出两个值。
    // dt_s 由调用者给（全机一个时间戳、一次夹取），本类既不取钟也不夹取。
    // pre_v / pre_a 是"上一拍采样"的历史，跟着采样一起提交；停车不进来（派生层早退），
    // 那两份残值由 stop() 清 0。
    void refresh(float dt_s);

    // ---------- 执行：一份算好的可调电压 → 各台电压并下发 ----------
    // 只做映射 volt_i = sign × (base_volt + volt_min_i) + 可调 × volt_factor_i，不做限速。
    // 压摆是策略（"该多快"要按状态判），归派生层；本层只认"给我多少就给多少"。
    void apply_output(float volt_adjust);

    // 开环直控：直接写各台电压，不垫底、不乘 factor（标定专用）。
    // 不走 apply_output，所以压摆锚点 output_applied 不更新——从开环切回闭环前先 stop()。
    void set_volt(const float *volt);

    // 全组 0V + 当前 stopping，清积分与输出。
    // 派生层若有状态机状态要一并清，定义同名函数遮蔽本函数并先调 MotorGroup::stop()。
    void stop();
    void setStopping(vex::brakeType brake);
};

#endif
