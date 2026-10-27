#include "chassis.h"

// 本文件装两类实现：
//   ① ChassisMotorGroup——六态机速度环（底盘专属，只算可调电压）；
//   ② Chassis——里程计、位置环、两组之间的缩回与限幅。
// 两者同属"底盘"这一个概念，声明同在 chassis.h，实现也放一处。

ChassisMotorGroup::ChassisMotorGroup(MyMotor *motor_list, uint8_t count,
                                     const Vec2f *kf_node, uint8_t kf_num,
                                     float p, float i, float d)
    : MotorGroup(motor_list, count, kf_node, kf_num, p, i, d)
{
}

// 先调基类做存储与历史留痕（target_jump / target_sign_prev），再按状态机决定起步 / 换向
void ChassisMotorGroup::set_target(float t)
{
    MotorGroup::set_target(t); // 基类只存储 + 死区归零 + 留痕，判断全在下面

    // 大跳只看命令自己变了多少，**不掺 pre_v**：阻塞或 I 还没追上时 pre_v 天然远离
    // 目标，掺进来判据就退化成"Δtarget > 阈值"，而它本来想表达的是"车已经在新目标
    // 附近就不算大跳"——那件事 Transit 自己会做（误差进了 error_for_i 一拍就回 Track）。
    // 掺了被控量，同一个拨杆在带载与空载时行为就不同。
    const bool jumped = fabsf(target_jump) > target_jump_abs;

    if (spin_state == Stopped && target != 0.0f)
    {
        if (fabs_target < err_out_dead)
            spin_state = Track; // 目标很小，直接跟踪，不必走起步窗
        else
        {
            spin_state = Transit;
            transit_elapsed_us = elapsed_idle; // 未开始计；本拍不取钟也不计时长
        }
    }
    else if (jumped)
    {
        // 只进 Transit 观察（那里不积分），**不清积分**：
        // 积分是顶住负载的那一项，阻塞时被清掉等于当场松劲，而操作手在顶东西时
        // 恰恰最爱动杆。Transit 本身很便宜——误差进了 error_for_i 就一拍回 Track。
        spin_state = Transit;
        transit_elapsed_us = elapsed_idle;
    }
    else if (target_sign != 0 && target_sign_prev != 0 && target_sign != target_sign_prev)
    {
        spin_state = Transit; // 换向
        transit_elapsed_us = elapsed_idle;
    }
}

// 六态机版速度环：算出本组请求的可调电压（output_raw），不执行、不限幅。
// 时序：MotorGroup::refresh（采样+前置+算输出）→ 状态判定① → 积分策略 → 状态判定② → 压摆。
// 基类的输出式子里不含任何判断，判断全在本层；积分策略排在输出之后，是因为它要判
// "吃掉这段积分输出会到哪"，而那要看本拍输出。
// 执行由本层 apply_output()（压摆）→ MotorGroup::apply_output()（映射下发）完成，
// 横向限幅由调用者（Chassis）负责。
void ChassisMotorGroup::refresh(uint32_t dt_us, float dt_s)
{
    // 停车 = 环整个不工作：不采样、不算、也不产生请求，一行都不走。
    // 残留（output_raw / integral / pre_v / pre_a …）由 stop() 一次清干净——
    // 在这里每帧擦掉是补丁，"根本不产生"才是正解。
    if (spin_state == Stopped)
        return;

    MotorGroup::refresh(dt_s);

    // 状态机计时：全是"过了多久"，用本拍 dt 累加，不碰时钟。
    // 进场那一拍不加 dt——命令往往落在两拍之间，那一整拍并不属于新状态。
    // 判"是不是进场拍"用的是 elapsed_idle（0xFFFFFFFF）这个哨兵，**靠显式赋值，不靠回绕**：
    // 0xFFFFFFFF + dt_us 得到的是 dt_us − 1，不是 0，只有当 dt_us == 1 时才凑巧归 0。
    if (spin_state == Transit)
    {
        if (transit_elapsed_us == elapsed_idle)
            transit_elapsed_us = 0; // 进场那一拍：只开表，不记时长
        else
            transit_elapsed_us += dt_us;
    }

    // error / derivative / base_volt / feedforward / headroom 都已在基类 refresh 里算好。
    const float fabs_error = fabsf(error);

    // ---------- 状态判定 1：目标 / 稳定（不依赖本拍输出，先判给 I 项用）----------
    if (spin_state == Transit && (fabs_error <= error_for_i ||
                                  (transit_elapsed_us >= transit_freeze_us &&
                                   fabsf(now_a) <= speed_stable && fabsf(pre_a) <= speed_stable)))
        spin_state = Track;
    else if (spin_state == Track && fabs_error <= err_in_dead)
        spin_state = Stable;
    else if (spin_state == Stable && fabs_error > err_out_dead)
        spin_state = Track;

    // ---------- 积分策略：抗饱和 + 缩放。基类不参与，全在这一层 ----------
    // 状态只决定两件事：本拍积不积（Transit 起步窗不积）、积多少（Stable 打对折防抖）。
    const bool allow_i = (spin_state != Transit);
    const float i_scale = (spin_state == Stable) ? i_stable_k : 1.0f;

    const float delta = error * dt_s * i_scale; // 本拍"想"积的量，pct·s
    // 判"吃掉这一段之后输出会到哪"，才停在天花板上而不是冲过头再回吐。
    // 输出对积分是线性的，所以只需在现值上补 I 那一段，不必把 P/D/前馈重算一遍。
    // 天花板必须是 headroom 而不是 12V：output_raw 之上还要叠组级垫底与个体死区，
    // 与比例限幅的分母是同一个数——两者不一致时，内层以为还有余量、外层已经在削。
    const float candidate = output_raw + ki * delta;
    // ki <= 0 时不积：此时积分不影响输出，攒下的只是"ki 一开就生效"的存量。
    if (allow_i && ki > 0.0f &&
        (candidate <= headroom || error <= 0.0f) &&
        (candidate >= -headroom || error >= 0.0f))
        integral += delta;
    // 本拍积的这段，下一拍的输出才吃到（与 D 用 previous_error 一样，都是"上一拍
    // 结算的历史"）。要当拍生效就得在这里再补一次输出，那等于把 P/D/夹幅再算一遍，
    // 而 10ms 的 I 项延迟对 ki 这个量级毫无意义。

    // ---------- 状态判定 2：非预期运动 / 堵转（要用本拍 output_raw，故排在最后）----------
    // "已给到上限"必须按 headroom 判，不能按 0.99×12V：抗饱和把积分卡在 headroom 上
    // （≈10.6V），12V 那个门槛在低速重载时永远够不着，Stall 就永远进不去。
    // 又一处"两个天花板必须同一个数"。
    const bool giving_max = fabsf(output_raw) >= headroom * 0.99f;                               // 输出是否已顶到上限
    const bool wheel_low = fabsf(now_v) <= stall_speed;                                          // 轮是否基本没在转
    const bool pulled_away = (target > 0.0f && now_a < 0.0f) || (target < 0.0f && now_a > 0.0f); // 目标与加速度反向

    if (spin_state == Stall)
    {
        // Stall 退出：轮速起来了，或操作手松杆（目标落进死区）；杆收小但没收到底不退出
        if (!wheel_low || fabs_target == 0.0f)
        {
            stall_elapsed_us = elapsed_idle;
            transit_elapsed_us = elapsed_idle;
            spin_state = Transit;
        }
    }
    else
    {
        // Stall 计时：给满 + 轮速低须持续够久（滤掉起步瞬间）；Transit 期间不计时
        if (giving_max && wheel_low && spin_state != Transit)
        {
            if (stall_elapsed_us == elapsed_idle)
                stall_elapsed_us = 0; // 条件刚成立那拍：只开表，不记时长
            else
                stall_elapsed_us += dt_us;
        }
        else
            stall_elapsed_us = elapsed_idle;

        if (stall_elapsed_us >= stall_time) // 已计入的时长够久；进场拍不计，故用 >=
            spin_state = Stall;
        else if (spin_state == Recover)
        {
            // Recover 退出：误差回到 I 工作区且输出不再顶格，走 Transit 重新观察（积分不清零）
            if (fabs_error <= error_for_i && !giving_max)
            {
                transit_elapsed_us = elapsed_idle;
                spin_state = Transit;
            }
        }
        else if (spin_state != Transit && pulled_away &&
                 (fabsf(now_a) > recover_accel || (giving_max && fabsf(now_a) > speed_stable)))
        {
            // Recover 进入：目标与加速度反向且幅度超门槛；前置不在 Transit
            // （那里的 error 是操作手造成的，不是外力）
            spin_state = Recover;
        }
    }

    // ---------- 压摆策略：把状态翻译成两个数值，写进基类 ----------
    slew_policy();

    // pre_v / pre_a 由 MotorGroup::refresh() 提交；本函数只读、不写历史值。
}

// 状态 → 本拍生效的两个速率。基类只认数，不认枚举；这里就是策略的唯一出处。
// 每个状态都必须显式覆盖，别指望"沿用上一次"——状态是在本函数前段判出来的，
// 漏写一个分支就等于让上一种策略漏到这一种里。
// Stopped 不在表里：那时 refresh 提前返回，走不到这里；apply_output 也不会被调。
void ChassisMotorGroup::slew_policy()
{
    switch (spin_state)
    {
    case Recover: // 外力带偏、车还在动：两方向都放开，抢时间补力矩
        slew_rise = INFINITY;
        slew_fall = INFINITY;
        break;

    case Stall: // 顶死：顶住要快（别被推回去），松劲要慢（松了会被弹回来）
        slew_rise = slew_fast;
        slew_fall = slew_slow;
        break;

    default: // Transit / Track / Stable：升压慢（护齿箱、防打滑），撤压快
        slew_rise = slew_slow;
        slew_fall = slew_fast;
        break;
    }
}

void ChassisMotorGroup::stop()
{
    MotorGroup::stop(); // 清积分、输出、各台电压
    spin_state = Stopped;
    transit_elapsed_us = elapsed_idle;
    stall_elapsed_us = elapsed_idle;
}

// 压摆窗口：把 req 收到"以 prev 为锚、按速率张开"的区间里，返回收好之后的值。
// rise / fall 是"离 0 / 朝 0"的速率，不是"正 / 负"方向的速率——所以过零那一拍要把
// 这一拍的额度拆成两段：先用 fall 撤到 0，剩下的时间用 rise 往反向张开。
// 不换向时窗口对称，两条边各张 step；step 取哪条速率取决于这拍幅值在增大还是减小。
static float slew_step(float req, float prev, float rise, float fall, float dt)
{
    if (req * prev < 0.0f) // 换向
    {
        const float t_zero = fabsf(prev) / fall; // 撤到 0 要花的时间
        if (t_zero >= dt)                        // 本拍撤不完，窗口缩成一个点
            return prev - copysignf(fall * dt, prev);
        return copysignf(fminf(fabsf(req), rise * (dt - t_zero)), req);
    }
    const float step = (fabsf(req) > fabsf(prev) ? rise : fall) * dt;
    return fminf(fmaxf(req, prev - step), prev + step);
}

// 执行：request（已按跨侧比例限缩）→ 收进本拍压摆窗口 → 基类映射下发。
// 限速对象是**可调电压**而不是每台电压：只要这一段被限住，factor 的比例在爬升全程不变。
// 锚点是 output_applied（上一次真正下发的可调电压），由基类在映射时写入。
void ChassisMotorGroup::apply_output(float request)
{
    MotorGroup::apply_output(slew_step(request, output_applied, slew_rise, slew_fall, dt_s));
}

// ==================== 以下为 Chassis（里程计 + 横向协调）====================

Chassis::Chassis(ChassisMotorGroup &left, ChassisMotorGroup &right,
                 vex::rotation &forward_tracking,
                 vex::rotation &left_tracking,
                 vex::inertial &inertial)
    : forward_tracking_sensor(forward_tracking),
      left_tracking_sensor(left_tracking),
      inertial_sensor(inertial),
      left_motors(left),
      right_motors(right)
{
}

void Chassis::init()
{
    // 初始化电机刹车模式为刹车（左右各设一次，brake 是 SDK 侧状态不是本工程的）
    left_motors.setStopping(vex::brakeType::brake);
    right_motors.setStopping(vex::brakeType::brake);
    stop();                      // 停止电机运动
    inertial_sensor.calibrate(); // 惯性传感器校准
    // 电机编码器清零，在有定位轮后可舍弃
    // left_motors.resetPosition();
    // right_motors.resetPosition();
    forward_tracking_sensor.resetPosition(); // 初始化定位轮连的旋转传感器
    left_tracking_sensor.resetPosition();
}

// 是否校准完成(init的等待环)
bool Chassis::is_ready()
{
    return !inertial_sensor.isCalibrating();
}

// 初始化后半段(init的结尾)
void Chassis::finish_init()
{
    // // IMU 校准后仍有残余零偏（实测约0.014°/s），静止采样1s取平均，
    // // 之后每拍从航向和角速度里扣掉它，采样期间车体必须保持静止。
    // float bias_sum = 0.0f;
    // const int bias_samples = 100;
    // for (int i = 0; i < bias_samples; ++i)
    // {
    //     bias_sum += static_cast<float>(inertial_sensor.gyroRate(vex::axisType::zaxis, vex::velocityUnits::dps));
    //     vex::this_thread::sleep_for(10);
    // }
    // gyro_bias = bias_sum / bias_samples;
    // heading_origin = now;
    inertial_sensor.resetRotation();
    reset(0); // 初始化没有"上一拍"，dt 给 0：update_gap=0 → linear_speed 保持旧值
}

// 更新里程、速度和航向
void Chassis::update(uint32_t dt_us)
{
    update_gap = dt_us; // 本拍间隔由调用者给（全机一拍一戳），本层不再记时间戳

    // 在有定位轮后可舍弃 编码器里程相关计算
    // // 电机内置编码器测得的是电机轴转数。经过外部齿轮组后：
    // //  车轮转数 = 电机转数 / 电机每车轮一圈所需转数,当前值为1故后续式子中没写(motor_wheel_ratio)
    // //  行驶距离 = 车轮转数 * 2*pi*车轮半径
    // float left_wheel_turns = static_cast<float>(left_motors.position());
    // float right_wheel_turns = static_cast<float>(right_motors.position());
    // float wheel_c = 2.0f * math_pi * wheel_r; // 车轮周长，单位 m。

    // // float new_left_distance = left_motor_turns / motor_wheel_ratio * wheel_c;
    // // float new_right_distance = right_motor_turns / motor_wheel_ratio * wheel_c;

    // float new_left_distance = left_wheel_turns * wheel_c;
    // float new_right_distance = right_wheel_turns * wheel_c;

    // left_distance_change = new_left_distance - left_distance;
    // right_distance_change = new_right_distance - right_distance;

    // 读取惯性传感器角速度，用于pid控制，扣除零偏使静止时读数在0附近
    angular_speed = static_cast<float>(inertial_sensor.gyroRate(vex::axisType::zaxis, vex::velocityUnits::dps));
    // - gyro_bias;

    // 读取惯性传感器航向，增量只从 IMU 原始读数差分
    float new_imu_heading = static_cast<float>(inertial_sensor.rotation(vex::rotationUnits::deg));
    // - gyro_bias * (now - heading_origin) / 1000000.0f;
    heading_change = new_imu_heading - imu_heading;
    imu_heading = new_imu_heading;
    // 转换为弧度
    float rotation_change = heading_change * deg_to_rad; // 单位 rad

    // 定位轮从此开始
    float tracking_wheel_c = 2.0f * math_pi * wheel_r; // 定位轮周长，单位 m。
    float new_forward_tracking_distance =              // 前向定位轮累计转数 * 定位轮周长 = 前向定位轮累计路程
        static_cast<float>(forward_tracking_sensor.position(vex::rotationUnits::rev)) * tracking_wheel_c;
    float new_side_tracking_distance = // 侧向定位轮累计转数 * 定位轮周长 = 侧向定位轮累计路程
        static_cast<float>(left_tracking_sensor.position(vex::rotationUnits::rev)) * tracking_wheel_c;

    // 定位轮不在车体旋转中心时，原地转向也会带动定位轮滚动，减去这部分旋转造成的假平移
    local_forward_change = new_forward_tracking_distance - forward_tracking_distance +
                           forward_tracking_offset * rotation_change;
    local_side_change = new_side_tracking_distance - side_tracking_distance -
                        side_tracking_offset * rotation_change;

    // 中点法：用本轮开始、结束角度的中值，把车体坐标的位移旋转到全局 x、y
    float middle_heading = (heading + heading_change * 0.5f) * deg_to_rad; // 单位 rad
    float cos_h = cosf(middle_heading);
    float sin_h = sinf(middle_heading);
    x_change = local_forward_change * cos_h - local_side_change * sin_h;
    y_change = local_forward_change * sin_h + local_side_change * cos_h;
    x += x_change;
    y += y_change;

    // 编码器里程回写已废弃（恢复点：打滑检测）：
    // left_distance = new_left_distance;
    // right_distance = new_right_distance;

    // 定位轮里程回写用于下一轮；全局航向 = IMU 读数 + 软件偏置。
    forward_tracking_distance = new_forward_tracking_distance;
    side_tracking_distance = new_side_tracking_distance;
    heading = new_imu_heading + heading_offset;
    // 定位轮可以宣告到此结束

    // 线速度 = 本轮前向位移 / 实际间隔；gap=0 时保持旧值
    if (update_gap > 0)
        linear_speed = local_forward_change * 1000000.0f / update_gap;
    // else 时保持不变(gap=0,无新值)
    // linear_speed 主要用于是否停车的判断

    // 当前坐标相对 reset() 起点，投影回起始车头方向
    float x_from_reset = x - x_start;
    float y_from_reset = y - y_start;
    distance_from_reset = x_from_reset * cos_i + y_from_reset * sin_i;
    side_distance_from_reset = -x_from_reset * sin_i + y_from_reset * cos_i;
    heading_from_reset = heading - heading_start;
}

// 开启新的一段动作：以当前状态为段起点快照，清零段内相对量与变化量缓存。
void Chassis::begin_segment()
{
    heading_start = heading;
    x_start = x;
    y_start = y;

    // 起始航向正余弦，本段不变，只在此处算一次
    float initial_direction = heading_start * deg_to_rad; // 单位 rad
    cos_i = cosf(initial_direction);
    sin_i = sinf(initial_direction);

    distance_from_reset = 0.0f;
    side_distance_from_reset = 0.0f;
    heading_from_reset = 0.0f;

    update_gap = 0;
    local_forward_change = 0.0f;
    local_side_change = 0.0f;
    heading_change = 0.0f;
    x_change = 0.0f;
    y_change = 0.0f;

    // // 编码器里程已废弃
    // left_distance_change = 0.0f;
    // right_distance_change = 0.0f;
}

void Chassis::reset(uint32_t dt_us)
{
    update(dt_us);
    begin_segment();
}

void Chassis::output(float left_output, float right_output)
{
    // 速度环目标入口。缩回系数取"超出比例最大"的那一侧，两侧同比例缩小——
    // 保比例而非单边削平，削平会改变左右出力比，车直接跑偏。
    // 之所以在底盘而不是组内：单组看不到另一侧的需求量。
    float s = 1.0f;
    if (left_motors.max_speed > 0.0f)
        s = fmaxf(s, fabsf(left_output) / left_motors.max_speed);
    if (right_motors.max_speed > 0.0f)
        s = fmaxf(s, fabsf(right_output) / right_motors.max_speed);
    spd_scale = s;

    left_motors.set_target(left_output / s);
    right_motors.set_target(right_output / s);
}

void Chassis::speed_tick(uint32_t dt_us, float dt_s)
{
    // 本拍这个组要不要跑速度环？只看状态机：Stopped = 环不工作。
    // 环不工作时这里什么都不做——**不是"每帧补一个 0V"**。0V 也是一条指令，
    // 会把开环直写的电压覆盖掉；而"停住"的语义已经由 stop() 里那次 motor->stop()
    // （按 setStopping 的刹车）表达过了，不需要每秒重复声明 100 次。
    // 这样开环电压天然不会被覆盖，也就不需要任何"开环标志"。

    // ① 算本拍请求的可调电压。两组都收到同一个 dt：左右就不会各自夹出一个来。
    left_motors.refresh(dt_us, dt_s);
    right_motors.refresh(dt_us, dt_s);

    // ② 比例限幅只统计在跑的组。停车组的 output_raw 已被置 0，本来也不会削到
    //    另一侧；但下发必须 gate 住——停车组的电压归开环直写或 stop() 的刹车。
    //
    //    【未决】分母用的是"请求值"，而请求可以远超余量：kp 标起来之后 P 项能到
    //    几十伏（error 大 × kp，I 反倒被抗饱和卡在 headroom 上）。超过余量的那部分
    //    物理上做不到，但两侧的比例仍是控制器的意图，所以这里按意图等比缩、不夹请求
    //    （往 output_raw 上夹 ±12 会让比例先失真）。待实车确认：若出现"单侧顶住 →
    //    整车被拽偏"，说明虚构的 P 不该有话语权，那时再加夹幅。见 memory 2026-10-25。
    const bool run_l = !left_motors.is_stopped();
    const bool run_r = !right_motors.is_stopped();
    const float head_l = left_motors.headroom;
    const float head_r = right_motors.headroom;
    float s = 1.0f;
    if (run_l && head_l > 0.0f)
        s = fmaxf(s, fabsf(left_motors.output_raw) / head_l);
    if (run_r && head_r > 0.0f)
        s = fmaxf(s, fabsf(right_motors.output_raw) / head_r);
    volt_scale = s;

    // ③ 下发：把限幅后的值直接当参数传下去。不回写 output_raw——它永远是
    //    "请求值"，所以监控里看到的是要多少，不是给了多少。
    //    停车组不碰（可能是开环直写的）。
    if (run_l)
        left_motors.apply_output(left_motors.output_raw / s);
    if (run_r)
        right_motors.apply_output(right_motors.output_raw / s);
}

void Chassis::stop()
{
    left_motors.stop();
    right_motors.stop();
}

