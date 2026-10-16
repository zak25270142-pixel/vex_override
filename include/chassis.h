#ifndef CHASSIS_H_
#define CHASSIS_H_

#include "my_main.h"

// 电机组 = 一个轮：组内两台电机用齿轮串连传动、物理等速，速度环只取 motors[0] 的读数作反馈。
// 整车里程计不看电机编码器，由两个定位轮 + IMU 独立测量（见 Chassis::update），
// 所以驱动轮打滑不会污染定位——这一点是全向底盘最值钱的地方。

// 单台电机的个体属性：一台电机实例 + 逐台标定的电压参数 + 上一拍输出电压。
// 电机组按数组元素逐台读写这几个属性，外界（LCD调参/监控）也直接取其字段地址。
// 无默认初值：作为聚合体由调用方（robot_and_control.cpp）逐台显式填好（gnu++11 要求）。
struct MyMotor
{
    vex::motor *motor; // 电机实例指针（vex::motor 不可拷贝，只能持指针）
    // 电压缩放因子：静态标定值，一次性吃掉"各电机天生快慢不同"（组内两台的对齐也在这里）。
    // 各台斜率 k(pct/V) 有个体差异，且经齿轮传动的电机轮速 = 轴速/r，等效轮速斜率 = k/r。
    // **组间允许 >1**：把偏慢的轮抬起来本来就该由标定解决，硬压成 ≤1 会把组间差异
    // 全丢给在线同步去压，起步前几拍姿态就白偏了。约束只有一条——
    // volt_factor·dyn_factor 不能让最终电压超过 volt_max（满指令时撞顶只是损失一点极速）。
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
        Stopped, // 停车：0V + 刹车，环休眠
        Transit, // 目标事件窗：起步 / 换向 / 目标大跳，I 不工作   特征:error大，但是我们自己刚改的主意
        Track,   // 正常跟踪：I 工作                               特征:error(较)小或加速度(较)小
        Stable,  // 目标稳定：I 大砍,尽量保持稳定                    特征:error小且加速度小
        Recover, // 非预期运动：车被外力带偏                         特征:error大且车速被拉向输出的反方向
        Stall    // 堵转：给满了车也不动                            特征:output 接近上限且轮速低且持续

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
        // Recover -> Transit  误差回到 I 工作区（积分已不可信，冻结重新观察）
        // Track/Recover -> Stall  给满 + 轮速低，并持续够久
        // Stall -> Transit  轮速起来了，或操作手松杆

        // Transit 与 Stall 互斥：起步那几十毫秒 output 也满、轮速也 0，靠 stall_time 滤掉
        // Recover 不从 Transit 进：Transit 里 error 大是操作手造成的，不是外力证据
        // 全向底盘注意：Recover/Stall 不在 {Track,Stable} 里 → achieve_valid 自动为假 → 同步冻结，
        //   外力造成的慢不会被当成"这轮天生弱"学进去；同时 sync_tick 会给这两个态强制满力。
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
    float output_deadzone = 0.5f;  // 目标速度死区，单位 pct；绝对值小于它按下发0处理（收敛到0由速度环负责，停车由外界 stop()）

    // ---- 同步：volt_factor 标定的"在线动态版" ----
    // 静态 volt_factor（可 >1）吃掉"各电机天生快慢不同"；
    // dyn_factor 只补"此刻负载/地面/姿态造成的慢性差异"，由 Chassis 每拍下发。
    // ∈[dyn_floor, 1]，与本组静态 volt_factor 相乘后一起生效：只削峰、不超频。
    // 生效方式：可电压调节区间被整体压缩（volt = 摩擦垫 + 归一输出·可调区间·volt_factor·dyn_factor），
    // 压到低于维持当前目标所需时，速度环的归一输出会顶满，该轮实际转速就跟着降下来。
    // 它是**慢性量**，调整速度比 PID 的 I 项还慢（见 Chassis 的 dyn_*_per_s），
    // 目的就是不抢在速度环前面动作。
    float dyn_factor = 1.0f;

    // 达标率：稳态跟踪下 实际转速/目标转速 的低通值，∈[0,1]。1=跟得上，<1=掉队或被压。
    // 注：pct 是 float（由 rpm 换算，会出现 1.01 这种），且 SDK 底层已对编码器速度滤过一次，
    // 这里再滤一次只是让同步的慢性判定更稳，不是救量化噪声。
    // 起步(Transit)、换向、目标过小这些判不准的期间本值无效，同步**冻结**（不跟也不压），
    // 打滑/腾空也落在这段里——不引入错误方向的学习，少量误差交给位置环和操作手。
    // Recover/Stall 同样落在冻结段里：那是外力造成的慢，不是"这轮天生弱"，
    // 学进去只会让全车被一个临时被顶住的轮拖慢。
    float achieve = 1.0f;
    bool achieve_valid = false; // 本拍达标率是否有效（稳态跟踪 且 目标足够大）
    bool limited = false;       // 本拍是否顶住：归一输出被顶满 或 处于 Recover/Stall。仅进监控，同步不读它
    float achieve_tau_us = 30000.0f; // 达标率低通时间常数(us)：约 30ms 跟上，调用周期变了不用改
    float achieve_min_target = 8.0f; // 目标速度低于此值(pct)不参与达标判定

    // ---- 目标事件（Transit）判据 ----
    // 目标变化量超过它才算"目标大跳"，单位 pct。因为是在相邻两拍上比的，
    // 它实际是个**速率**门槛：10ms 周期下 12 pct/拍 ≈ 操作手在 83ms 内推完整个行程。
    // 慢慢拨杆（每拍变化小于它）不会进 Transit——那本就不需要重新建立输出。
    float target_jump_abs = 12.0f;
    // ---- 稳定态判态参数 ----
    float speed_stable = 50.0f; // Transit->Track加速度阈值，单位 pct/s；同时作为 Recover/Stall 里的最小加速度门槛
    float err_in_dead = 2.0f;   // Track->Stable 误差死区，进入该范围内后，I 大砍，减少震荡
    float err_out_dead = 3.0f;  // Stable->Track  误差死区，退出该范围内后，I 大砍，减少震荡

    float i_stable_k = 0.5f; // Stable模式下 I项系数为原先多少

    // I 项工作范围（误差阈值）：|error| 落在它以内算小误差——I 才开始工作、Transit 才可进 Track。单位 pct。
    // 固定值即可：I 的波动幅度天生与 error 同量纲、与 target 无关，稳定态抖动靠调小 ki 抑制。
    // 同时是 Recover 的退出条件：误差被拉出这个范围才谈得上"非预期"。
    float error_for_i = 8.0f;

    // ---- 非预期运动（Recover）判据 ----
    // 车速被外力带离目标时的加速度门槛，单位 pct/s。
    // 物理参照：传动系统自身在反向满电压下能造成的最大减速度，超过它就不可能是我们做的。
    // 注意 kf 标定到 target=100 时 ff 正好等于 1.0，满杆下没有余量可给、Recover 收益为 0，
    // 所以这个门槛不需要很灵敏。实车标定。
    float recover_accel = 200.0f;
    // 归一化输出达到它算"已经给到上限"，用于 Recover 的兜底项和 Stall 的合取项。
    float stall_output = 0.99f;
    // 轮速低于它算"轮胎基本没转"，单位 pct。
    float stall_speed = 8.0f;
    // "给满 + 轮速低"要同时成立这么久才认 Stall，单位 us。
    // 它必须大于整车满载起步到 stall_speed 的时间：起步那段 output 本来就是满的、轮速本来就是 0，
    // 没有这个延时的话每次起步都会误判成堵转。实车标定。
    uint32_t stall_time = 300000;

    // 电压斜率上限，两个方向不对称：
    //   升压侧：加压过快会让起步打滑、电流拉爆，必须慢。
    float slew_boost = 25.0f; // 升压斜率上限，单位 V/s
    //   撤压侧：收油/制动没有打滑风险，电机那端本来就是被动负载，
    //   撤压慢等于迟收油、继续推着车走。初值按"松杆后 manual_stop_delay 内能从
    //   满压收到 0"取：12V / 0.05s = 240 V/s。改 manual_stop_delay 时一并复核。
    float slew_release = 240.0f; // 撤压斜率上限，单位 V/s

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
    uint32_t stall_since_us = 0;        // "给满 + 轮速低"开始成立的时刻；不成立时为 0

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
    // 四个轮组：0=左前 1=右前 2=左后 3=右后。每组两台电机齿轮串连，组内物理等速。
    // 用指针数组：MyMotorGroup 不可默认构造，引用数组没法在初始化列表里逐个绑定。
    MyMotorGroup *wheels[4];
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

    // 底盘物理参数
    float wheel_r = 0.0254f; // 定位轮半径，单位 m（旧车定位轮直径 5.08cm，即 2 英寸）。
    // 速比现在恰好为1，故没写，后续若不为一需新增。

    // X 布局全向轮混控参数：
    // 单轮可达满速上限，单位 pct；混控结果超出时按它整体等比缩回（保留指令向量方向）。
    float max_wheel_speed = 100.0f;
    // 转向通道折算系数：把"转向通道 pct"折成轮速 pct，等效力臂 (半轴距+半轮距)/轮半径 已折算在内。
    // 默认 1.0 = 与平移通道同权；上车按"同样数值下转向与平移的实际效果"实测标定。
    float turn_ratio = 1.0f;

    // ---- 同步（在线降级）调参：全部按"每秒"给，与调用周期无关 ----
    // dyn_floor 兜两层：
    //   ① 保证 capability = achieve/dyn 的折算假设（转速 ∝ dyn）还成立——dyn 太低时可控电压段
    //      被压到与摩擦垫同量级，转速不再随它线性下降，折算失真 → 闭锁从后门回来；
    //   ② 限制"为一个短板轮把全车拖到多慢"，并给转向通道留纠偏余量——同步只削天花板，
    //      压到地板后"把这一侧提起来"这条路就没了，偏航修正只剩"把另一侧压慢"。
    float dyn_floor = 0.40f;
    // 压低/回升都是慢性的：应比 I 项积分还慢，别抢在速度环前面动作。
    // 1.0→0.4 约 0.5s，0.4→1.0 约 3s（恢复更慢，留观察窗，避免在门槛上抖）。
    float dyn_down_per_s = 1.2f;
    float dyn_up_per_s = 0.2f;
    float cap_min = 0.10f; // 达标能力下限：达标率趋零时防止算出的系数炸掉（纯数值兜底）

    // ---- 受力不均（手柄推挤/对撞/地面差异）----
    // 判据不看"整车动没动"：整车顶死时四组都进 Recover，用不着专门判；
    // 两组卡死时车还能动，判"不动"也说明不了问题。真正要防的是各轮受力不均 → 内耗 + 姿态歪，
    // 主要发生在手柄操控期间（自动程序几乎不会堵）。
    float fight_disp_min = 0.15f;   // 各轮 capability 的 (max−min) 超阈值即认为受力不均
    float fight_force_min = 8.0f;   // 或内耗 |internal_force|(pct) 超阈值：四轮已经在不一致地跑
    float fight_trans_floor = 0.5f; // 受力不均时平移通道最多降到原来的多少（转向通道不降，姿态优先）
    uint32_t fight_enter_us = 100000; // 连续判定成立多久才进（us），退出是立即的

    // 定位轮到车体旋转中心的有符号距离，单位 m，实测。
    // 前向轮读数只受其右向偏移影响：偏右为正（其偏前 74.315mm 不影响前向读数，无需参数）。
    float forward_tracking_offset = 0.012434f; // 前向定位轮偏右 12.434mm。
    // 侧向轮读数只受其前向偏移影响：偏前为正、偏后为负（其左右居中，无侧向偏移）。
    float side_tracking_offset = -0.021262f; // 侧向定位轮偏后 21.262mm。

    /* 以上调参区间 */

    // 混控结果：四轮目标速度(pct)，等比缩回后的最终下发值。同步算法与监控都读它。
    // stop() 必须把它清零——否则停车后同步还拿着旧指令当"在使劲"，会误判受力不均。
    float wheel_target[4] = {0.0f, 0.0f, 0.0f, 0.0f};
    
    // 每组"满力时的达标能力" = achieve / dyn_factor，∈(0,1]。1=满力能跟上，<1=这块是短板。
    // 用它而不是直接用达标率，是为了把"我们压过它"折回满力，否则同步会把自己压出来的
    // 慢当成短板，全车一起锁死在低速（闭锁）。
    float capability[4] = {1.0f, 1.0f, 1.0f, 1.0f};

    // 内耗指标，单位 pct：轮速在运动学矩阵左零空间方向上的分量 =(左前+右前−左后−右后)/2。
    // 4 轮只有 3 个自由度，多出来的那一个约束就是它：任何刚体运动都投影不到这个方向上，
    // 所以非零即代表轮速互不一致——打架或打滑的直接读数，差速底盘没有这个量（无冗余）。
    float internal_force = 0.0f;

    // 受力不均标志：true 时平移通道降幅、转向通道不降（姿态优先），同步改按指令向量守合力方向。
    bool fight_mode = false;
    // 平移通道本拍的降幅系数 ∈[fight_trans_floor, 1]，由 sync_tick 按能力离散度算出。
    float fight_trans_scale = 1.0f;

    uint32_t fight_time_us = 0; // 连续判定"受力不均"已持续多久(us)

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
    float linear_speed = 0.0f;   // 车体中心沿自身前方的线速度，单位 m/s。
    float side_speed = 0.0f;     // 车体中心沿自身右方的线速度，单位 m/s（侧向定位轮增量/间隔）。
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

    // 四个轮组按 左前、右前、左后、右后 的顺序传入，顺序错了整车运动方向就错。
    Chassis(MyMotorGroup &left_front,
            MyMotorGroup &right_front,
            MyMotorGroup &left_back,
            MyMotorGroup &right_back,
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

    // 混控唯一目标入口：车体坐标系三通道，单位 pct（转向向右为正）。
    //   forward 车头方向、side 车体右方、turn 绕车体中心右转。
    // 内部一次性线性映射到四轮：四轮目标速度永远由同一个向量生成，也就永远落在
    // 刚体运动的可行子空间里——这是"指令层面不会打架"的根源。
    // 超出单轮上限时整体等比缩回（保留平移方向与转向比例），绝不各轮独立截断：
    // 独立截断会破坏向量方向，正是全向轮内耗的来源之一。
    // 本函数只写目标，闭环由 speed_tick() 推进。
    void output(float forward, float side, float turn);

    // 速度环每轮推进：由独立常驻任务短周期调用。停车状态下各组内部直接返回。
    void speed_tick();

    // 同步节拍：每个 refresh 周期在 speed_tick() **之前**调用一次（先决策后执行）。
    // 读四组的达标情况 → 折算满力达标能力 → 重算各组的 dyn_factor 并下发，本拍 speed_tick 就用新值。
    // （放在之前/之后都是 1 拍延迟，等价；放前面读着顺，且不会用到"旧 target 下"的达标率。）
    // 全向轮没有胶轮的横向约束去"平均"掉队量，这份耦合只能在控制器里人为造出来：
    // 谁跑得快就让谁，谁真跑不动就全车一起按它的水平慢下来，姿态因此不变形。
    void sync_tick();

    // 按 init() 设置的停车方式停止四个轮组（连带把同步状态复位为"满力"）。
    void stop();
};

#endif
