#include "my_main.h"
#include "menu_func.h"
#include "key_set.h"
#include "LCD_menu.h"
#include "ai_vision.h"
#include "robot_and_control.h"

enum MENU_state : uint8_t
{
    pre = 0,
    main,
    ai_vision,
};

static MENU_state menu_state = pre;

// 四个轮组：LF=左前 RF=右前 LB=左后 RB=右后，与混控矩阵顺序一致。
const struct MENU_ITEM menu_item[] =
    {
        // ========== 速度环（四个轮组）==========
        // kf/kp/ki/kd 成员均与构造入参同量纲（秒量纲），不再有 1e6 内部换算
        {"LF_kf", type_float, &lf_motors.kf, "左前kf", ""},
        {"LF_kp", type_float, &lf_motors.kp, "左前kp", ""},
        {"LF_ki", type_float, &lf_motors.ki, "左前ki", ""},
        {"LF_kd", type_float, &lf_motors.kd, "左前kd", ""},
        {"RF_kf", type_float, &rf_motors.kf, "右前kf", ""},
        {"RF_kp", type_float, &rf_motors.kp, "右前kp", ""},
        {"RF_ki", type_float, &rf_motors.ki, "右前ki", ""},
        {"RF_kd", type_float, &rf_motors.kd, "右前kd", ""},
        {"LB_kf", type_float, &lb_motors.kf, "左后kf", ""},
        {"LB_kp", type_float, &lb_motors.kp, "左后kp", ""},
        {"LB_ki", type_float, &lb_motors.ki, "左后ki", ""},
        {"LB_kd", type_float, &lb_motors.kd, "左后kd", ""},
        {"RB_kf", type_float, &rb_motors.kf, "右后kf", ""},
        {"RB_kp", type_float, &rb_motors.kp, "右后kp", ""},
        {"RB_ki", type_float, &rb_motors.ki, "右后ki", ""},
        {"RB_kd", type_float, &rb_motors.kd, "右后kd", ""},

        // 摩擦补偿：全向轮横移时滚子摩擦明显大于直行，四组要按方向分别标定
        {"LF_st_dz", type_float, &lf_motors.static_deadzone, "左前静摩擦", "V"},
        {"LF_dy_dz", type_float, &lf_motors.dynamic_deadzone, "左前动摩擦", "V"},
        {"RF_st_dz", type_float, &rf_motors.static_deadzone, "右前静摩擦", "V"},
        {"RF_dy_dz", type_float, &rf_motors.dynamic_deadzone, "右前动摩擦", "V"},
        {"LB_st_dz", type_float, &lb_motors.static_deadzone, "左后静摩擦", "V"},
        {"LB_dy_dz", type_float, &lb_motors.dynamic_deadzone, "左后动摩擦", "V"},
        {"RB_st_dz", type_float, &rb_motors.static_deadzone, "右后静摩擦", "V"},
        {"RB_dy_dz", type_float, &rb_motors.dynamic_deadzone, "右后动摩擦", "V"},

        // ========== 电压映射（速度环归一输出 → 各台实际电压）==========
        // V = 目标方向·(摩擦+volt_min) + out·(volt_max-摩擦-volt_min)·volt_factor·dyn_factor
        // 摩擦垫只认目标方向，out 是双极性连续修正，过零不再翻转摩擦电压；
        // volt_factor 是出厂标定的个体差异，dyn_factor 是每拍下发的在线同步（两者都 ≤1）。
        {"LF_vmin0", type_float, &lf_motors.motors[0].volt_min, "左前0最小电压", "V"},
        {"LF_vmin1", type_float, &lf_motors.motors[1].volt_min, "左前1最小电压", "V"},
        {"RF_vmin0", type_float, &rf_motors.motors[0].volt_min, "右前0最小电压", "V"},
        {"RF_vmin1", type_float, &rf_motors.motors[1].volt_min, "右前1最小电压", "V"},
        {"LB_vmin0", type_float, &lb_motors.motors[0].volt_min, "左后0最小电压", "V"},
        {"LB_vmin1", type_float, &lb_motors.motors[1].volt_min, "左后1最小电压", "V"},
        {"RB_vmin0", type_float, &rb_motors.motors[0].volt_min, "右后0最小电压", "V"},
        {"RB_vmin1", type_float, &rb_motors.motors[1].volt_min, "右后1最小电压", "V"},
        {"LF_vmax0", type_float, &lf_motors.motors[0].volt_max, "左前0最大电压", "V"},
        {"LF_vmax1", type_float, &lf_motors.motors[1].volt_max, "左前1最大电压", "V"},
        {"RF_vmax0", type_float, &rf_motors.motors[0].volt_max, "右前0最大电压", "V"},
        {"RF_vmax1", type_float, &rf_motors.motors[1].volt_max, "右前1最大电压", "V"},
        {"LB_vmax0", type_float, &lb_motors.motors[0].volt_max, "左后0最大电压", "V"},
        {"LB_vmax1", type_float, &lb_motors.motors[1].volt_max, "左后1最大电压", "V"},
        {"RB_vmax0", type_float, &rb_motors.motors[0].volt_max, "右后0最大电压", "V"},
        {"RB_vmax1", type_float, &rb_motors.motors[1].volt_max, "右后1最大电压", "V"},
        {"LF_vf0", type_float, &lf_motors.motors[0].volt_factor, "左前0电压系数", ""},
        {"LF_vf1", type_float, &lf_motors.motors[1].volt_factor, "左前1电压系数", ""},
        {"RF_vf0", type_float, &rf_motors.motors[0].volt_factor, "右前0电压系数", ""},
        {"RF_vf1", type_float, &rf_motors.motors[1].volt_factor, "右前1电压系数", ""},
        {"LB_vf0", type_float, &lb_motors.motors[0].volt_factor, "左后0电压系数", ""},
        {"LB_vf1", type_float, &lb_motors.motors[1].volt_factor, "左后1电压系数", ""},
        {"RB_vf0", type_float, &rb_motors.motors[0].volt_factor, "右后0电压系数", ""},
        {"RB_vf1", type_float, &rb_motors.motors[1].volt_factor, "右后1电压系数", ""},

        // ========== 速度环判态（四个轮组）==========
        {"LF_odz", type_float, &lf_motors.output_deadzone, "左前速度死区", "pct"},
        {"RF_odz", type_float, &rf_motors.output_deadzone, "右前速度死区", "pct"},
        {"LB_odz", type_float, &lb_motors.output_deadzone, "左后速度死区", "pct"},
        {"RB_odz", type_float, &rb_motors.output_deadzone, "右后速度死区", "pct"},
        {"LF_slewB", type_float, &lf_motors.slew_boost, "左前提速斜率", "V/s"},
        {"RF_slewB", type_float, &rf_motors.slew_boost, "右前提速斜率", "V/s"},
        {"LB_slewB", type_float, &lb_motors.slew_boost, "左后提速斜率", "V/s"},
        {"RB_slewB", type_float, &rb_motors.slew_boost, "右后提速斜率", "V/s"},
        {"LF_errI", type_float, &lf_motors.error_for_i, "左前I误差阈值", "pct"},
        {"RF_errI", type_float, &rf_motors.error_for_i, "右前I误差阈值", "pct"},
        {"LB_errI", type_float, &lb_motors.error_for_i, "左后I误差阈值", "pct"},
        {"RB_errI", type_float, &rb_motors.error_for_i, "右后I误差阈值", "pct"},

        // ========== 同步：达标率判定（每组）==========
        // alpha 越大跟得越快但也越抖；min_target 以下的目标不参与判定（低速比值噪声大）
        {"LF_ach_t", type_float, &lf_motors.achieve_tau_us, "左前达标时间常数", "us"},
        {"RF_ach_t", type_float, &rf_motors.achieve_tau_us, "右前达标时间常数", "us"},
        {"LB_ach_t", type_float, &lb_motors.achieve_tau_us, "左后达标时间常数", "us"},
        {"RB_ach_t", type_float, &rb_motors.achieve_tau_us, "右后达标时间常数", "us"},
        {"LF_ach_t", type_float, &lf_motors.achieve_min_target, "左前达标门槛", "pct"},
        {"RF_ach_t", type_float, &rf_motors.achieve_min_target, "右前达标门槛", "pct"},
        {"LB_ach_t", type_float, &lb_motors.achieve_min_target, "左后达标门槛", "pct"},
        {"RB_ach_t", type_float, &rb_motors.achieve_min_target, "右后达标门槛", "pct"},

        // ========== 混控与同步（底盘级）==========
        {"mix_max", type_float, &chassis.max_wheel_speed, "单轮满速", "pct"},
        {"mix_turn", type_float, &chassis.turn_ratio, "转向折算", ""},
        // 同步是慢性回路：压低/回升都比 I 项慢，别抢在速度环前面
        {"dyn_floor", type_float, &chassis.dyn_floor, "同步系数下限", ""},
        {"dyn_down", type_float, &chassis.dyn_down_per_s, "同步压低速率", "/s"},
        {"dyn_up", type_float, &chassis.dyn_up_per_s, "同步回升速率", "/s"},
        {"cap_min", type_float, &chassis.cap_min, "达标能力下限", ""},

        // 受力不均（手柄推挤/对撞/地面差异）：看各轮能力离散度与内耗，不看"车动没动"
        {"fight_disp", type_float, &chassis.fight_disp_min, "受力不均离散阈值", ""},
        {"fight_frc", type_float, &chassis.fight_force_min, "受力不均内耗阈值", "pct"},
        {"fight_trs", type_float, &chassis.fight_trans_floor, "受力不均平移下限", ""},
        {"fight_t", type_uint32_t, &chassis.fight_enter_us, "受力不均确认时长", "us"},

        // 手柄松杆减速缓冲：太小会点头前倾，太大松杆后滑行太久
        {"man_decay", type_float, &robot_action.manual_decay_per_s, "松杆减速速率", "pct/s"},

        // // ========== 位置环 ==========
        // {"d_kp", type_float, &robot_action.distance_pid.kp, "距离kp", ""},
        // {"d_ki", type_float, &robot_action.distance_pid.ki, "距离ki", ""},
        // {"d_kd", type_float, &robot_action.distance_pid.kd, "距离kd", ""},
        // {"d_max", type_float, &robot_action.distance_pid.max_output, "距离环限速", "pct"},

        // {"h_kp", type_float, &robot_action.heading_pid.kp, "纠偏kp", ""},
        // {"h_ki", type_float, &robot_action.heading_pid.ki, "纠偏ki", ""},
        // {"h_kd", type_float, &robot_action.heading_pid.kd, "纠偏kd", ""},
        // {"h_max", type_float, &robot_action.heading_pid.max_output, "纠偏限速", "pct"},

        // {"t_kp", type_float, &robot_action.turn_pid.kp, "转向kp", ""},
        // {"t_ki", type_float, &robot_action.turn_pid.ki, "转向ki", ""},
        // {"t_kd", type_float, &robot_action.turn_pid.kd, "转向kd", ""},
        // {"t_max", type_float, &robot_action.turn_pid.max_output, "转向限速", "pct"},

        // // ========== 满速 / 容差 ==========
        // 单轮满速已移到上面"混控与同步"段（mix_max）
        // {"dist_tol", ...}

        // {"dist_tol", type_float, &robot_action.distance_tolerance, "距离容差", "m"},
        // {"v_tol", type_float, &robot_action.linear_speed_tolerance, "线速度容差", "m/s"},
        // {"head_tol", type_float, &robot_action.heading_tolerance, "直线航向容差", "deg"},
        // {"ang_tol", type_float, &robot_action.angle_tolerance, "转角容差", "deg"},
        // {"w_tol", type_float, &robot_action.angular_speed_tolerance, "角速度容差", "deg/s"},
        // {"man_dz", type_int32_t, &robot_action.manual_deadzone, "摇杆死区", ""},
};

// 监控表要在运行时被通信模块改订阅档位（改tag字段），不能加const。
// tag 为位域：SUB/FAST/GETTER/KIND；x/y/yaw 用 KIND 供场地图识别。
static struct MENU_ITEM monitor_menu_item[] =
    {
        // 场地图语义：订阅+高速+KIND
        {"chassis_x", type_float, &chassis.x, "全局X", "m", monitor_tag_pos_x},
        {"chassis_y", type_float, &chassis.y, "全局Y", "m", monitor_tag_pos_y},
        {"chassis_heading", type_float, &chassis.heading, "航向", "deg", monitor_tag_yaw},

        // 本段相对量（手推/动作时看定位是否准）
        {"dist_seg", type_float, &chassis.distance_from_reset, "段内前向", "m", monitor_tag_fast},
        {"side_seg", type_float, &chassis.side_distance_from_reset, "段内侧向", "m", monitor_tag_fast},
        {"head_seg", type_float, &chassis.heading_from_reset, "段内转角", "deg", monitor_tag_fast},

        // 瞬时速度（判停、StableJudge 对照；全向底盘侧向也要看）
        {"v_lin", type_float, &chassis.linear_speed, "前向速度", "m/s", monitor_tag_fast},
        {"v_side", type_float, &chassis.side_speed, "侧向速度", "m/s", monitor_tag_fast},
        {"w_ang", type_float, &chassis.angular_speed, "角速度", "deg/s", monitor_tag_fast},

        // 同步与内耗：全向底盘的"健康指标"，调同步时盯这几个
        // 内耗 = 轮速在运动学冗余方向上的分量，非零即四轮互不一致（打架/打滑）
        {"internal", type_float, &chassis.internal_force, "内耗", "pct", monitor_tag_fast},
        {"fight", type_bool, &chassis.fight_mode, "受力不均", "", monitor_tag_fast},
        {"fight_tr", type_float, &chassis.fight_trans_scale, "受力不均平移系数", "", monitor_tag_fast},
        {"fight_t", type_uint32_t, &chassis.fight_time_us, "受力不均计时", "us", monitor_tag_fast},

        // 定位轮原始累计（第4步正负与尺度）
        {"trk_fwd", type_float, &chassis.forward_tracking_distance, "前向轮里程", "m", monitor_tag_slow},
        {"trk_side", type_float, &chassis.side_tracking_distance, "侧向轮里程", "m", monitor_tag_slow},

        // 混控结果：四轮目标速度（等比缩回后的下发值）。与实际转速对照即跟踪误差。
        {"LF_tgt", type_float, &chassis.wheel_target[0], "左前指令", "pct", monitor_tag_fast},
        {"RF_tgt", type_float, &chassis.wheel_target[1], "右前指令", "pct", monitor_tag_fast},
        {"LB_tgt", type_float, &chassis.wheel_target[2], "左后指令", "pct", monitor_tag_fast},
        {"RB_tgt", type_float, &chassis.wheel_target[3], "右后指令", "pct", monitor_tag_fast},

        // 四轮实际转速(慢1拍)：与上面四条指令一一对齐看
        {"LF_pct", type_float, &lf_motors.pre_v, "左前转速", "pct", monitor_tag_fast},
        {"RF_pct", type_float, &rf_motors.pre_v, "右前转速", "pct", monitor_tag_fast},
        {"LB_pct", type_float, &lb_motors.pre_v, "左后转速", "pct", monitor_tag_fast},
        {"RB_pct", type_float, &rb_motors.pre_v, "右后转速", "pct", monitor_tag_fast},

        // 同步三件套：达标率 / 动态系数 / 折算后的达标能力
        // 四组达标率一致 = 四轮跑在同一水平上 = 姿态不变形；达标能力最小的那个就是此刻的短板。
        {"LF_ach", type_float, &lf_motors.achieve, "左前达标率", "", monitor_tag_fast},
        {"RF_ach", type_float, &rf_motors.achieve, "右前达标率", "", monitor_tag_fast},
        {"LB_ach", type_float, &lb_motors.achieve, "左后达标率", "", monitor_tag_fast},
        {"RB_ach", type_float, &rb_motors.achieve, "右后达标率", "", monitor_tag_fast},
        {"LF_dyn", type_float, &lf_motors.dyn_factor, "左前动态系数", "", monitor_tag_fast},
        {"RF_dyn", type_float, &rf_motors.dyn_factor, "右前动态系数", "", monitor_tag_fast},
        {"LB_dyn", type_float, &lb_motors.dyn_factor, "左后动态系数", "", monitor_tag_fast},
        {"RB_dyn", type_float, &rb_motors.dyn_factor, "右后动态系数", "", monitor_tag_fast},
        {"LF_cap", type_float, &chassis.capability[0], "左前达标能力", "", monitor_tag_fast},
        {"RF_cap", type_float, &chassis.capability[1], "右前达标能力", "", monitor_tag_fast},
        {"LB_cap", type_float, &chassis.capability[2], "左后达标能力", "", monitor_tag_fast},
        {"RB_cap", type_float, &chassis.capability[3], "右后达标能力", "", monitor_tag_fast},
        {"LF_lim", type_bool, &lf_motors.limited, "左前顶住", "", monitor_tag_slow},
        {"RF_lim", type_bool, &rf_motors.limited, "右前顶住", "", monitor_tag_slow},
        {"LB_lim", type_bool, &lb_motors.limited, "左后顶住", "", monitor_tag_slow},
        {"RB_lim", type_bool, &rb_motors.limited, "右后顶住", "", monitor_tag_slow},

        // 指令电压：每组取 motors[0]（组内齿轮串连、同电压），有稳定地址，内存项即可
        {"LF_Vcmd", type_float, &lf_motors.motors[0].volt_output, "左前0指令电压", "V", monitor_tag_fast},
        {"RF_Vcmd", type_float, &rf_motors.motors[0].volt_output, "右前0指令电压", "V", monitor_tag_fast},
        {"LB_Vcmd", type_float, &lb_motors.motors[0].volt_output, "左后0指令电压", "V", monitor_tag_fast},
        {"RB_Vcmd", type_float, &rb_motors.motors[0].volt_output, "右后0指令电压", "V", monitor_tag_fast},
        {"LF1_Vcmd", type_float, &lf_motors.motors[1].volt_output, "左前1指令电压", "V", monitor_tag_slow},
        {"RF1_Vcmd", type_float, &rf_motors.motors[1].volt_output, "右前1指令电压", "V", monitor_tag_slow},
        {"LB1_Vcmd", type_float, &lb_motors.motors[1].volt_output, "左后1指令电压", "V", monitor_tag_slow},
        {"RB1_Vcmd", type_float, &rb_motors.motors[1].volt_output, "右后1指令电压", "V", monitor_tag_slow},

        // PID 内部量：调速度环时看积分有没有顶住、误差有没有收敛
        {"LF_error", type_float, &lf_motors.previous_error, "左前PID误差", "", monitor_tag_fast},
        {"RF_error", type_float, &rf_motors.previous_error, "右前PID误差", "", monitor_tag_fast},
        {"LB_error", type_float, &lb_motors.previous_error, "左后PID误差", "", monitor_tag_fast},
        {"RB_error", type_float, &rb_motors.previous_error, "右后PID误差", "", monitor_tag_fast},
        {"LF_integ", type_float, &lf_motors.integral, "左前PID积分", "", monitor_tag_slow},
        {"RF_integ", type_float, &rf_motors.integral, "右前PID积分", "", monitor_tag_slow},
        {"LB_integ", type_float, &lb_motors.integral, "左后PID积分", "", monitor_tag_slow},
        {"RB_integ", type_float, &rb_motors.integral, "右后PID积分", "", monitor_tag_slow},

        // 实际电压：SDK 回读，与指令电压 Vcmd 对照（差距大说明被限幅/堵转）
        {"LF_Vact", []
         { return static_cast<float>(lf_motors.motors[0].motor->voltage(vex::voltageUnits::volt)); }, "左前0实际电压", "V", monitor_tag_fast},
        {"RF_Vact", []
         { return static_cast<float>(rf_motors.motors[0].motor->voltage(vex::voltageUnits::volt)); }, "右前0实际电压", "V", monitor_tag_fast},
        {"LB_Vact", []
         { return static_cast<float>(lb_motors.motors[0].motor->voltage(vex::voltageUnits::volt)); }, "左后0实际电压", "V", monitor_tag_fast},
        {"RB_Vact", []
         { return static_cast<float>(rb_motors.motors[0].motor->voltage(vex::voltageUnits::volt)); }, "右后0实际电压", "V", monitor_tag_fast},
        {"LF1_Vact", []
         { return static_cast<float>(lf_motors.motors[1].motor->voltage(vex::voltageUnits::volt)); }, "左前1实际电压", "V", monitor_tag_slow},
        {"RF1_Vact", []
         { return static_cast<float>(rf_motors.motors[1].motor->voltage(vex::voltageUnits::volt)); }, "右前1实际电压", "V", monitor_tag_slow},
        {"LB1_Vact", []
         { return static_cast<float>(lb_motors.motors[1].motor->voltage(vex::voltageUnits::volt)); }, "左后1实际电压", "V", monitor_tag_slow},
        {"RB1_Vact", []
         { return static_cast<float>(rb_motors.motors[1].motor->voltage(vex::voltageUnits::volt)); }, "右后1实际电压", "V", monitor_tag_slow},

        // 电流：内耗最直接的表现——打架时同一指令下电流会明显偏高。每组取 motors[0]
        {"LF_I", []
         { return static_cast<float>(lf_motors.motors[0].motor->current(vex::currentUnits::amp)); }, "左前0电流", "A", monitor_tag_slow},
        {"RF_I", []
         { return static_cast<float>(rf_motors.motors[0].motor->current(vex::currentUnits::amp)); }, "右前0电流", "A", monitor_tag_slow},
        {"LB_I", []
         { return static_cast<float>(lb_motors.motors[0].motor->current(vex::currentUnits::amp)); }, "左后0电流", "A", monitor_tag_slow},
        {"RB_I", []
         { return static_cast<float>(rb_motors.motors[0].motor->current(vex::currentUnits::amp)); }, "右后0电流", "A", monitor_tag_slow},

        // 组内第二台的转速：与 motors[0] 对照，齿轮串连出问题（打齿/松动）能看出来
        {"LF1_pct", []
         { return static_cast<float>(lf_motors.motors[1].motor->velocity(vex::velocityUnits::pct)); }, "左前1转速", "pct", monitor_tag_slow},
        {"RF1_pct", []
         { return static_cast<float>(rf_motors.motors[1].motor->velocity(vex::velocityUnits::pct)); }, "右前1转速", "pct", monitor_tag_slow},
        {"LB1_pct", []
         { return static_cast<float>(lb_motors.motors[1].motor->velocity(vex::velocityUnits::pct)); }, "左后1转速", "pct", monitor_tag_slow},
        {"RB1_pct", []
         { return static_cast<float>(rb_motors.motors[1].motor->velocity(vex::velocityUnits::pct)); }, "右后1转速", "pct", monitor_tag_slow},

        // 手柄（手动/Arcade 对照）。now_x/now_y 是稳定地址，value环形槽会轮转不能直接取地址。
        {"axis_Lx", type_int32_t, &left_axis.now_x, "左杆X", nullptr, monitor_tag_slow},
        {"axis_Ly", type_int32_t, &left_axis.now_y, "左杆Y", nullptr, monitor_tag_slow},
        {"axis_Rx", type_int32_t, &right_axis.now_x, "右杆X", nullptr, monitor_tag_slow},
        {"axis_Ry", type_int32_t, &right_axis.now_y, "右杆Y", nullptr, monitor_tag_slow},
};

MENU menu(menu_item,
          sizeof(menu_item) / sizeof(menu_item[0]),
          menu_key_reset,
          menu_tc_key_reset,
          monitor_menu_item,
          sizeof(monitor_menu_item) / sizeof(monitor_menu_item[0]));

// USB上位机通信，三张表（调参/监控/动作命令）都在构造时整张传入。
// robot_cmds是另一编译单元里的纯const字面量表（常量初始化先于动态初始化完成），
// 此处引用不存在全局对象构造顺序问题。
USB_Comm comm(menu_item,
              sizeof(menu_item) / sizeof(menu_item[0]),
              monitor_menu_item,
              sizeof(monitor_menu_item) / sizeof(monitor_menu_item[0]),
              robot_cmds,
              robot_cmd_count);

// 颜色名称、显示颜色和检测结果在应用层逐项对应，菜单类不猜测颜色含义。
static const AI_VISION_MENU_OBJECT ai_vision_objects[] = {
    {"RED", 0xFF0000, &ai_colors[0].visible, &ai_colors[0].center_x,
     &ai_colors[0].center_y, &ai_colors[0].width, &ai_colors[0].height},
    {"YELLOW", 0xFFFF00, &ai_colors[1].visible, &ai_colors[1].center_x,
     &ai_colors[1].center_y, &ai_colors[1].width, &ai_colors[1].height},
    {"BLUE", 0x0000FF, &ai_colors[2].visible, &ai_colors[2].center_x,
     &ai_colors[2].center_y, &ai_colors[2].width, &ai_colors[2].height},
    {"GRAY", 0xAAAAAA, &ai_colors[3].visible, &ai_colors[3].center_x,
     &ai_colors[3].center_y, &ai_colors[3].width, &ai_colors[3].height},
    {"TAG", 0xFFFFFF, &ai_tag.visible, &ai_tag.center_x,
     &ai_tag.center_y, &ai_tag.width, &ai_tag.height},
};

static const uint8_t AI_VISION_TAG_INDEX = 4;

static bool ai_vision_connected()
{
    return ai_sensor.installed();
}

static AI_VISION_MENU ai_vision_menu(
    ai_vision_connected,
    ai_vision_objects,
    sizeof(ai_vision_objects) / sizeof(ai_vision_objects[0]),
    AI_VISION_TAG_INDEX,
    &ai_tag.id,
    &ai_tag.angle_deg,
    ai_object_count,
    ai_vision_menu_key,
    menu.table_color,
    menu.bg_color);

void refresh_menu()
{
    switch (menu_state)
    {
    case pre:
        if (key_enter.read())
        {
            menu_state = main;
            menu_key_reset();
            menu.refresh();
        }
        else if (key_shift.read())
        {
            menu_state = ai_vision;
            ai_vision_menu.init();
        }
        break;

    case main:
        if (key_up.read())
            menu.up();
        else if (key_down.read())
            menu.down();
        else if (key_enter.read())
            menu.enter();
        else if (key_back.read())
        {
            if (menu.inner_menu)
                menu.back();
            else
                pre_menu_init();
        }
        else if (key_shift.read())
            menu.shift();
        else if (key_add.read())
            menu.add();
        else if (key_reduce.read())
            menu.reduce();
        else if (menu.is_monitor_menu)
            menu.refresh_value();

        // 上位机改过调参的脏标志：菜单轮询自取自清并刷一帧；
        // 监控界面本来每轮都在刷，只在调参界面需要补刷。没有菜单时标志挂着无害。
        if (comm.tunable_dirty)
        {
            comm.tunable_dirty = false;
            if (!menu.is_monitor_menu)
                menu.refresh_value();
        }
        break;

    case ai_vision:
        if (key_back.read())
            pre_menu_init();
        else
            ai_vision_menu.refresh();
        break;
    }
}

void pre_menu_init()
{
    reset_origin();
    Brain.Screen.drawRectangle(0, 0, 480, 272, vex::black);
    pre_menu_key();
    menu_state = pre;
}
