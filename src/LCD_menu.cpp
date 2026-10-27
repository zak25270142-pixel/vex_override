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

const struct MENU_ITEM menu_item[] =
    {
        // ========== 速度环（左右）==========
        {"L_kp", type_float, &left_motors.kp, "左kp", ""},
        {"L_ki", type_float, &left_motors.ki, "左ki", ""},
        {"L_kd", type_float, &left_motors.kd, "左kd", ""},
        {"R_kp", type_float, &right_motors.kp, "右kp", ""},
        {"R_ki", type_float, &right_motors.ki, "右ki", ""},
        {"R_kd", type_float, &right_motors.kd, "右kd", ""},

        {"L_st_dz", type_float, &left_motors.static_deadzone, "左静摩擦", "V"},
        {"L_dy_dz", type_float, &left_motors.dynamic_deadzone, "左动摩擦", "V"},
        {"R_st_dz", type_float, &right_motors.static_deadzone, "右静摩擦", "V"},
        {"R_dy_dz", type_float, &right_motors.dynamic_deadzone, "右动摩擦", "V"},

        // ========== 电压映射（速度环归一输出 → 三路实际电压）==========
        {"L_vmin0", type_float, &left_motor_list[0].volt_min, "左0最小电压", "V"},
        {"L_vmin1", type_float, &left_motor_list[1].volt_min, "左1最小电压", "V"},
        {"L_vmin2", type_float, &left_motor_list[2].volt_min, "左2最小电压", "V"},
        {"R_vmin0", type_float, &right_motor_list[0].volt_min, "右0最小电压", "V"},
        {"R_vmin1", type_float, &right_motor_list[1].volt_min, "右1最小电压", "V"},
        {"R_vmin2", type_float, &right_motor_list[2].volt_min, "右2最小电压", "V"},
        {"L_vf0", type_float, &left_motor_list[0].volt_factor, "左0电压系数", ""},
        {"L_vf1", type_float, &left_motor_list[1].volt_factor, "左1电压系数", ""},
        {"L_vf2", type_float, &left_motor_list[2].volt_factor, "左2电压系数", ""},
        {"R_vf0", type_float, &right_motor_list[0].volt_factor, "右0电压系数", ""},
        {"R_vf1", type_float, &right_motor_list[1].volt_factor, "右1电压系数", ""},
        {"R_vf2", type_float, &right_motor_list[2].volt_factor, "右2电压系数", ""},
        {"L_odz", type_float, &left_motors.target_deadzone, "左速度死区", "pct"},
        {"R_odz", type_float, &right_motors.target_deadzone, "右速度死区", "pct"},
        // 绑的是标定值 slow/fast（slew_rise/slew_fall 是每拍被状态覆盖的生效值，
        // 绑那里等于调了立刻被盖掉）。常规态下 slow=升压上限、fast=撤压上限。
        {"L_slewB", type_float, &left_motors.slew_slow, "左升压斜率", "V/s"},
        {"R_slewB", type_float, &right_motors.slew_slow, "右升压斜率", "V/s"},
        {"L_slewR", type_float, &left_motors.slew_fast, "左撤压斜率", "V/s"},
        {"R_slewR", type_float, &right_motors.slew_fast, "右撤压斜率", "V/s"},
        {"L_jump", type_float, &left_motors.target_jump_abs, "左大跳阈值", "pct"},
        {"R_jump", type_float, &right_motors.target_jump_abs, "右大跳阈值", "pct"},
        {"L_errI", type_float, &left_motors.error_for_i, "左I误差阈值", "pct"},
        {"R_errI", type_float, &right_motors.error_for_i, "右I误差阈值", "pct"},
        {"L_recA", type_float, &left_motors.recover_accel, "左反常加速度", "pct/s"},
        {"R_recA", type_float, &right_motors.recover_accel, "右反常加速度", "pct/s"},

        // ========== 升降杆（lift_bar，初版开环：电压直控 + 速度环）==========
        {"lift_kp", type_float, &lift_bar.pid.kp, "升速kp", ""},
        {"lift_ki", type_float, &lift_bar.pid.ki, "升速ki", ""},
        {"lift_dz", type_float, &lift_bar.deadzone, "升死区补偿", "V"},
        {"lift_speps", type_float, &lift_bar.stall_pos_eps, "升不动判据", "rev"},
        {"lift_stme", type_uint32_t, &lift_bar.stall_time, "升卡死时长", "us"},
        {"lift_grace", type_uint32_t, &lift_bar.stall_grace, "升起步宽容期", "us"},
        {"lift_sout", type_float, &lift_bar.stall_output, "升顶格判据", ""},
        {"lift_scur", type_float, &lift_bar.stall_current, "升电流判据", "A"},
        {"lift_sspd", type_float, &lift_bar.stall_speed_min, "升要动判据", "pct"},
        {"lift_pf", type_float, &lift_bar.pos_factor, "升杆端系数", "mm/rev"},
        {"lift_tlim", type_float, &lift_bar.tilt_limit, "升倾斜上限", "rev"},
        {"lift_lim0", type_float, &lift_bar.limit_min, "升行程下端", "rev"},
        {"lift_lim1", type_float, &lift_bar.limit_max, "升行程上端", "rev"},

        // 升降杆逐台属性：vf=出力均摊标定，vmax=结构限幅（别搞坏的安全闸），vmin=死区
        {"lift_vf0", type_float, &lift_motor_list[0].volt_factor, "升0电压系数", ""},
        {"lift_vf1", type_float, &lift_motor_list[1].volt_factor, "升1电压系数", ""},
        {"lift_vf2", type_float, &lift_motor_list[2].volt_factor, "升2电压系数", ""},
        {"lift_vf3", type_float, &lift_motor_list[3].volt_factor, "升3电压系数", ""},
        {"lift_vmin0", type_float, &lift_motor_list[0].volt_min, "升0最小电压", "V"},
        {"lift_vmin1", type_float, &lift_motor_list[1].volt_min, "升1最小电压", "V"},
        {"lift_vmin2", type_float, &lift_motor_list[2].volt_min, "升2最小电压", "V"},
        {"lift_vmin3", type_float, &lift_motor_list[3].volt_min, "升3最小电压", "V"},

        // 手柄松杆减速缓冲
        {"man_decay", type_uint32_t, &robot_action.manual_stop_delay, "松杆停车延迟", "us"},
        // 45° 倾角时的转弯半径(m)：方向定半径，与杆量无关
        {"turn_r45", type_float, &robot_action.turn_radius_45, "45度转弯半径", "m"},

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
        // {"L_max", type_float, &left_motors.max_speed, "左满速", "pct"},
        // {"R_max", type_float, &right_motors.max_speed, "右满速", "pct"},

        // {"dist_tol", type_float, &robot_action.distance_tolerance, "距离容差", "m"},
        // {"v_tol", type_float, &robot_action.linear_speed_tolerance, "线速度容差", "m/s"},
        // {"head_tol", type_float, &robot_action.heading_tolerance, "直线航向容差", "deg"},
        // {"ang_tol", type_float, &robot_action.angle_tolerance, "转角容差", "deg"},
        // {"w_tol", type_float, &robot_action.angular_speed_tolerance, "角速度容差", "deg/s"},
        // {"man_dz", type_int32_t, &robot_action.manual_deadzone, "摇杆死区", ""},
};

// 监控表要在运行时被通信模块改订阅档位（改tag字段），不能加const。
static struct MENU_ITEM monitor_menu_item[] =
    {
        // 场地图语义：订阅+高速+KIND
        {"chassis_x", type_float, &chassis.x, "全局X", "m", monitor_tag_pos_x},
        {"chassis_y", type_float, &chassis.y, "全局Y", "m", monitor_tag_pos_y},
        {"chassis_heading", type_float, &chassis.heading, "航向", "deg", monitor_tag_yaw},

        // 本段相对量
        {"dist_seg", type_float, &chassis.distance_from_reset, "段内前向", "m", monitor_tag_fast},
        {"side_seg", type_float, &chassis.side_distance_from_reset, "段内侧向", "m", monitor_tag_fast},
        {"head_seg", type_float, &chassis.heading_from_reset, "段内转角", "deg", monitor_tag_fast},

        // 瞬时速度
        {"v_lin", type_float, &chassis.linear_speed, "线速度", "m/s", monitor_tag_fast},
        {"w_ang", type_float, &chassis.angular_speed, "角速度", "deg/s", monitor_tag_fast},

        // 定位轮原始累计
        {"trk_fwd", type_float, &chassis.forward_tracking_distance, "前向轮里程", "m", monitor_tag_slow},
        {"trk_side", type_float, &chassis.side_tracking_distance, "侧向轮里程", "m", monitor_tag_slow},

        // 指令电压
        {"L0_Vcmd", type_float, &left_motor_list[0].volt_output, "左0指令电压", "V", monitor_tag_fast},
        {"R0_Vcmd", type_float, &right_motor_list[0].volt_output, "右0指令电压", "V", monitor_tag_fast},
        {"L1_Vcmd", type_float, &left_motor_list[1].volt_output, "左1指令电压", "V", monitor_tag_slow},
        {"R1_Vcmd", type_float, &right_motor_list[1].volt_output, "右1指令电压", "V", monitor_tag_slow},
        {"L2_Vcmd", type_float, &left_motor_list[2].volt_output, "左2指令电压", "V", monitor_tag_slow},
        {"R2_Vcmd", type_float, &right_motor_list[2].volt_output, "右2指令电压", "V", monitor_tag_slow},

        // 指令转速（速度环目标）
        {"L_tgt", type_float, &left_motors.target, "左指令转速", "pct", monitor_tag_fast},
        {"R_tgt", type_float, &right_motors.target, "右指令转速", "pct", monitor_tag_fast},
        {"L_integral", type_float, &left_motors.integral, "左PID积分", "", monitor_tag_fast},
        {"R_integral", type_float, &right_motors.integral, "右PID积分", "", monitor_tag_fast},
        // 本拍前馈输出
        {"L_ff", type_float, &left_motors.feedforward, "左前馈", "", monitor_tag_fast},
        {"R_ff", type_float, &right_motors.feedforward, "右前馈", "", monitor_tag_fast},
        {"L_error", type_float, &left_motors.error, "左PID误差", "", monitor_tag_fast},
        {"R_error", type_float, &right_motors.error, "右PID误差", "", monitor_tag_fast},
        // 可调电压还能给多少（12 − 垫底 − 个体死区）。比例限幅的分母、抗饱和的天花板
        // 都是它；volt_scale > 1 时看这里就知道是被谁顶住的。
        {"L_head", type_float, &left_motors.headroom, "左电压余量", "V", monitor_tag_slow},
        {"R_head", type_float, &right_motors.headroom, "右电压余量", "V", monitor_tag_slow},
        // 速度环状态机：0=Stopped 1=Transit 2=Track 3=Stable 4=Recover 5=Stall
        {"L_state", type_uint8_t, &left_motors.spin_state, "左环状态", "", monitor_tag_fast},
        {"R_state", type_uint8_t, &right_motors.spin_state, "右环状态", "", monitor_tag_fast},

        // 转速/加速度（speed 环内部量，慢1拍）
        {"L_pct", type_float, &left_motors.pre_v, "左转速(慢1拍)", "pct", monitor_tag_fast},
        {"R_pct", type_float, &right_motors.pre_v, "右转速(慢1拍)", "pct", monitor_tag_fast},
        {"L_a", type_float, &left_motors.pre_a, "左加速度(慢1拍)", "pct", monitor_tag_fast},
        {"R_a", type_float, &right_motors.pre_a, "右加速度(慢1拍)", "pct", monitor_tag_fast},

        // 实际电压：SDK 回读
        {"L0_Vact", []
         { return static_cast<float>(left_motors.motors[0].motor->voltage(vex::voltageUnits::volt)); }, "左0实际电压", "V", monitor_tag_fast},
        {"R0_Vact", []
         { return static_cast<float>(right_motors.motors[0].motor->voltage(vex::voltageUnits::volt)); }, "右0实际电压", "V", monitor_tag_fast},
        {"L1_Vact", []
         { return static_cast<float>(left_motors.motors[1].motor->voltage(vex::voltageUnits::volt)); }, "左1实际电压", "V", monitor_tag_slow},
        {"R1_Vact", []
         { return static_cast<float>(right_motors.motors[1].motor->voltage(vex::voltageUnits::volt)); }, "右1实际电压", "V", monitor_tag_slow},
        {"L2_Vact", []
         { return static_cast<float>(left_motors.motors[2].motor->voltage(vex::voltageUnits::volt)); }, "左2实际电压", "V", monitor_tag_slow},
        {"R2_Vact", []
         { return static_cast<float>(right_motors.motors[2].motor->voltage(vex::voltageUnits::volt)); }, "右2实际电压", "V", monitor_tag_slow},

        {"L0_I", []
         { return static_cast<float>(left_motors.motors[0].motor->current(vex::currentUnits::amp)); }, "左0电流", "A", monitor_tag_slow},
        {"L0_pct", []
         { return static_cast<float>(left_motors.motors[0].motor->velocity(vex::velocityUnits::pct)); }, "左0转速", "pct", monitor_tag_slow},
        {"L1_I", []
         { return static_cast<float>(left_motors.motors[1].motor->current(vex::currentUnits::amp)); }, "左1电流", "A", monitor_tag_slow},
        {"L1_pct", []
         { return static_cast<float>(left_motors.motors[1].motor->velocity(vex::velocityUnits::pct)); }, "左1转速", "pct", monitor_tag_slow},
        {"L2_I", []
         { return static_cast<float>(left_motors.motors[2].motor->current(vex::currentUnits::amp)); }, "左2电流", "A", monitor_tag_slow},
        {"L2_pct", []
         { return static_cast<float>(left_motors.motors[2].motor->velocity(vex::velocityUnits::pct)); }, "左2转速", "pct", monitor_tag_slow},

        {"R0_I", []
         { return static_cast<float>(right_motors.motors[0].motor->current(vex::currentUnits::amp)); }, "右0电流", "A", monitor_tag_slow},
        {"R0_pct", []
         { return static_cast<float>(right_motors.motors[0].motor->velocity(vex::velocityUnits::pct)); }, "右0转速", "pct", monitor_tag_slow},
        {"R1_I", []
         { return static_cast<float>(right_motors.motors[1].motor->current(vex::currentUnits::amp)); }, "右1电流", "A", monitor_tag_slow},
        {"R1_pct", []
         { return static_cast<float>(right_motors.motors[1].motor->velocity(vex::velocityUnits::pct)); }, "右1转速", "pct", monitor_tag_slow},
        {"R2_I", []
         { return static_cast<float>(right_motors.motors[2].motor->current(vex::currentUnits::amp)); }, "右2电流", "A", monitor_tag_slow},
        {"R2_pct", []
         { return static_cast<float>(right_motors.motors[2].motor->velocity(vex::velocityUnits::pct)); }, "右2转速", "pct", monitor_tag_slow},

        // 升降杆（lift_bar）：state 0=Stopped 1=Volt 2=Speed 3=Stall(锁死)
        {"lift_state", type_uint8_t, &lift_bar.state, "升状态", "", monitor_tag_fast},
        {"lift_tspd", type_float, &lift_bar.speed_target, "升目标速度", "pct", monitor_tag_fast},
        {"lift_spd", type_float, &lift_bar.speed, "升速度", "pct", monitor_tag_fast},
        {"lift_pos", type_float, &lift_bar.position, "升位置", "rev", monitor_tag_fast},
        {"lift_out", type_float, &lift_bar.output, "升输出", "", monitor_tag_fast},
        {"lift_I", type_float, &lift_bar.avg_current, "升平均电流", "A", monitor_tag_fast},
        {"lift_IL", type_float, &lift_bar.cur_left, "升左电流", "A", monitor_tag_fast},
        {"lift_IR", type_float, &lift_bar.cur_right, "升右电流", "A", monitor_tag_fast},
        {"lift_Idf", type_float, &lift_bar.cur_diff, "升左右电流差", "A", monitor_tag_fast},
        {"lift_L", type_float, &lift_bar.pos_left, "升左位置", "rev", monitor_tag_fast},
        {"lift_R", type_float, &lift_bar.pos_right, "升右位置", "rev", monitor_tag_fast},
        {"lift_tilt", type_float, &lift_bar.tilt, "升左右倾斜", "rev", monitor_tag_fast},
        {"lift_sprd", type_float, &lift_bar.spread, "升组内偏差", "rev", monitor_tag_fast},
        {"lift_jam", type_float, &lift_bar.jam_pos, "升卡死位置", "rev", monitor_tag_slow},

        // 逐台：验转向同号、验齿轮都啮上（某台空转时它这几项会明显离群）
        {"lift_m0_pos", []
         { return static_cast<float>(lift_bar.motor_group.motors[0].motor->position(vex::rotationUnits::rev)); }, "升0位置", "rev", monitor_tag_slow},
        {"lift_m1_pos", []
         { return static_cast<float>(lift_bar.motor_group.motors[1].motor->position(vex::rotationUnits::rev)); }, "升1位置", "rev", monitor_tag_slow},
        {"lift_m2_pos", []
         { return static_cast<float>(lift_bar.motor_group.motors[2].motor->position(vex::rotationUnits::rev)); }, "升2位置", "rev", monitor_tag_slow},
        {"lift_m3_pos", []
         { return static_cast<float>(lift_bar.motor_group.motors[3].motor->position(vex::rotationUnits::rev)); }, "升3位置", "rev", monitor_tag_slow},
        {"lift_m0_I", []
         { return static_cast<float>(lift_bar.motor_group.motors[0].motor->current(vex::currentUnits::amp)); }, "升0电流", "A", monitor_tag_slow},
        {"lift_m1_I", []
         { return static_cast<float>(lift_bar.motor_group.motors[1].motor->current(vex::currentUnits::amp)); }, "升1电流", "A", monitor_tag_slow},
        {"lift_m2_I", []
         { return static_cast<float>(lift_bar.motor_group.motors[2].motor->current(vex::currentUnits::amp)); }, "升2电流", "A", monitor_tag_slow},
        {"lift_m3_I", []
         { return static_cast<float>(lift_bar.motor_group.motors[3].motor->current(vex::currentUnits::amp)); }, "升3电流", "A", monitor_tag_slow},
        {"lift_m0_Vcmd", type_float, &lift_motor_list[0].volt_output, "升0指令电压", "V", monitor_tag_slow},
        {"lift_m1_Vcmd", type_float, &lift_motor_list[1].volt_output, "升1指令电压", "V", monitor_tag_slow},
        {"lift_m2_Vcmd", type_float, &lift_motor_list[2].volt_output, "升2指令电压", "V", monitor_tag_slow},
        {"lift_m3_Vcmd", type_float, &lift_motor_list[3].volt_output, "升3指令电压", "V", monitor_tag_slow},

        // 实际电压：SDK 回读
        {"lift_m0_Vact", []
         { return static_cast<float>(lift_bar.motor_group.motors[0].motor->voltage(vex::voltageUnits::volt)); }, "升0实际电压", "V", monitor_tag_slow},
        {"lift_m1_Vact", []
         { return static_cast<float>(lift_bar.motor_group.motors[1].motor->voltage(vex::voltageUnits::volt)); }, "升1实际电压", "V", monitor_tag_slow},
        {"lift_m2_Vact", []
         { return static_cast<float>(lift_bar.motor_group.motors[2].motor->voltage(vex::voltageUnits::volt)); }, "升2实际电压", "V", monitor_tag_slow},
        {"lift_m3_Vact", []
         { return static_cast<float>(lift_bar.motor_group.motors[3].motor->voltage(vex::voltageUnits::volt)); }, "升3实际电压", "V", monitor_tag_slow},

        // 手柄摇杆原始值
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

// USB上位机通信：调参/监控/动作命令三张表构造时整张传入
USB_Comm comm(menu_item,
              sizeof(menu_item) / sizeof(menu_item[0]),
              monitor_menu_item,
              sizeof(monitor_menu_item) / sizeof(monitor_menu_item[0]),
              robot_cmds,
              robot_cmd_count);

// 颜色名称、显示颜色与检测结果逐项对应
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

static bool remote_drive = true;
// button3 标签与颜色同步到 joystick_drive_on：开=遥控器控车(绿)，关=摇杆控菜单并交出底盘(灰)
static void refresh_remote_button()
{
    if (remote_drive)
    {
        button3.reset_button_name("remote:on", 14, 16);
        button3.reset_button_color(vex::color(0x00cc44), vex::color(0x005511));
    }
    else
    {
        button3.reset_button_name("remote:off", 14, 16);
        button3.reset_button_color(vex::color(0x777777), vex::color(0x222222));
    }
    button3.draw_button(Screen_Button::draw_released);
}

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
        else if (key_remote.read())
        {
            // 遥控器控车开关：开=摇杆只当数据源开车；关=摇杆操作菜单并交出底盘（互锁见 docs/代码补充注释/05）
            remote_drive = !remote_drive;
            if (remote_drive)
            {
                left_axis.state = Remote_Control::only_value;
                right_axis.state = Remote_Control::only_value;
                robot_action.manual();
            }
            else
            {
                left_axis.state = Remote_Control::middle;
                right_axis.state = Remote_Control::middle;
                robot_action.stop_move(); // 解除手柄任务指针交出底盘
            }
            refresh_remote_button();
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

        // 上位机改过调参的脏标志：自取自清，调参界面补刷一帧
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
    refresh_remote_button(); // 进/回待机屏时把标签同步到当前开关状态
    menu_state = pre;
}
