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

class MotorGroup
{
public:
    // --- 持有电机 ---
    MyMotor *motors;
    uint8_t motor_count;

    // --- 目标 ---
    float target = 0.0f;
    int8_t target_sign = 0;
    float fabs_target = 0.0f;
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
    float base_volt = 0.0f; //  本拍摩擦垫底

    // 组内最大的个体死区补偿，V。构造时算一次（表是静态的，不必每拍遍历）。
    // 只给 headroom 用：实际垫底是"组级 base_volt + 逐台 volt_min"。
    float volt_min_max = 0.0f;

    // --- 速度环参数（V 量纲）---
    float kp; // V / pct
    float ki; // V / (pct·s)
    float kd; // V·s / pct

    // --- 本拍采样与 PID 前置（refresh 一次算齐，后面谁用谁读，不再现算）---
    float now_v = 0.0f;      // 本拍转速，pct，取 motors[0]
    float now_a = 0.0f;      // 本拍加速度，pct/s。用上一拍已提交的 pre_v 差分得到
    float error = 0.0f;      // 本拍误差 = target − now_v，pct
    float derivative = 0.0f; // 本拍误差变化率 = (error − previous_error) / dt，pct/s

    // 本拍可调电压的物理余量，V：12 − base_volt − max(volt_min_i)。
    // 实际下发 = 组级垫底 + 个体垫底 + 可调(output×factor)，能填到 12V 的是这几项之和，
    // 所以 output 最多只能到"余量"。它每拍随 base_volt（随 |target| 变）重算，是个
    // 活量而不是常量——所以要算一次存下来，给三个上层调用者共用：抗饱和判据（派生层）、
    // 顶格判据（派生层 Stall/Recover）、比例限幅分母（Chassis）。三处必须是同一个数，
    // 否则内层以为还有余量、外层已经在削。
    float headroom = 0.0f;
    float dt_s = 0.0f;
    uint32_t last_time_us = 0; // 上一次 refresh 的时刻，算 dt_s 用（状态机计时也复用它）

    // --- PID 状态 ---
    float integral = 0.0f;       // 误差累计，pct·s
    float previous_error = 0.0f; // 上一轮误差，pct
    float pre_v = 0.0f;          // 上一拍转速（refresh 提交）
    float pre_a = 0.0f;          // 上一拍加速度（refresh 提交），六态机判 Transit 用

    // ---------- 本拍可调电压（V）----------
    // 只有 calc_output() 写它（派生层 apply 前后都不碰），写的是"请求值"（前馈 + PID，已夹 ±12）。
    // 限幅不在这里落账：调用者把限幅系数作为参数交给 apply_output()。
    // 所以本字段只有一个含义、一个写者，读到它时不必猜"这是限幅前还是限幅后"。
    float output = 0.0f;

    // 电压斜率上限 本拍允许的电压变化速率，V/s。基类不解释这个数从哪来
    //   正无穷(INFINITY) = 不限速（当阶跃处理，立即到位）
    //   0               = 电压钉死（两方向都不许变，apply_output 直接返回）
    //   25/200          = 慢升、快撤（护齿箱、防打滑）
    float slew_boost = INFINITY;   // 幅值增大方向的上限，V/s
    float slew_release = INFINITY; // 幅值减小方向的上限，V/s

    MotorGroup(MyMotor *motor_list, uint8_t count,
               const Vec2f *kf_node, uint8_t kf_num,
               float p, float i, float d);

    // 外界唯一改目标的入口：只做存储（target / sign / fabs + 死区归零），不下结论。
    // "这次变化算不算大跳""要不要进起步窗"都是带阈值的判断，属于上层状态机——
    // 基类一旦替它算，就等于把某个机构的策略写进了公共层。
    void set_target(float t);
    // 摩擦补偿曲线
    float friction_volt() const; // dynamic + (static − dynamic) × exp(−|target|/3)

    // ---------- 每拍刷新：采样 + PID 前置（不算控制、不碰积分、不写 output）----------
    // 只在这一处读编码器 —— 状态判据与 PID 因此必然用同一个采样点，不会出现
    // "判据读一次编码器、控制律再读一次"的两个样本。
    // 顺手把控制律要用的一切前置算好存字段：error / derivative / base_volt /
    // feedforward / headroom。它们都只是 (target, now_v, 参数) 的函数，没有代价，
    // 也不该由使用方各算一遍——同一件事算两遍，迟早算出两个值。
    // 提交上一拍值（pre_v/pre_a）也在这里：交给控制步提交的话，Stopped 期间
    // 控制步不跑，pre_v 会冻结，重启首拍的加速度会被算成巨大的假值
    // （(0 − 50) / 0.01 = −5000 pct/s），正好撞上 Recover/Stall 的加速度判据。
    void refresh();

    // ---------- 基础计算：前馈 + P + I + D → output（V）----------
    // 只算，不判。前置量（error / derivative / feedforward）由本拍 refresh() 备好，
    // 所以必须先 refresh() 再调本函数，且每拍只调一次。
    //
    // 【故意的边界：不碰 integral】积分该不该累加、累加多少（抗饱和、i 缩放、
    // 误差带限积、增速上限）全是策略，由调用方决定——它是唯一知道"这段积分到底
    // 给了出去没有"的一层。基类只提供"用当前 integral 算出来是多少"。
    // 后果（写下来免得以后有人以为漏了）：单用 MotorGroup 不构成闭环，调用方
    // 必须自己写 integral += delta；派生层见 ChassisMotorGroup::calc_output()。
    void calc_output();

    // ---------- 执行：可调电压 → 各台电压并下发 ----------
    // volt_i = sign × (base_volt + volt_min_i) + (output / scale) × volt_factor_i
    // 垫底按目标方向（停止时 sign=0，电压归零）；可调部分才乘 factor 对齐出力。
    // scale = 调用者的比例限幅系数（1 = 不限幅，>1 = 按比例削），由跨组比较的一方算，
    // 不写回 output。末尾按 slew_boost / slew_release 限速（换向以 0 为界分段算），
    // 限速额度用本拍 refresh() 算出的 dt_s——所以三者（refresh→calc_output→apply_output）
    // 必须同拍、按序，本拍只跑一轮。
    void apply_output(float scale);

    // 开环直控
    void output_raw(const float *volt);

    // 全组 0V + 当前 stopping，清积分与输出。
    // 派生层若有状态机状态要一并清，定义同名函数遮蔽本函数并先调 MotorGroup::stop()。
    void stop();
    void setStopping(vex::brakeType brake);
};

#endif
