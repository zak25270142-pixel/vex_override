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
        // kp/kf 与构造入参同量纲；ki 成员=秒量纲/1e6；kd 成员=秒量纲*1e6
        {"L_kf", type_float, &left_motors.kf, "左kf 输出/pct", ""},
        {"L_kp", type_float, &left_motors.kp, "左kp 输出/pct", ""},
        {"L_ki", type_float, &left_motors.ki, "左ki内部÷1e6", ""},
        {"L_kd", type_float, &left_motors.kd, "左kd内部×1e6", ""},
        {"R_kf", type_float, &right_motors.kf, "右kf 输出/pct", ""},
        {"R_kp", type_float, &right_motors.kp, "右kp 输出/pct", ""},
        {"R_ki", type_float, &right_motors.ki, "右ki内部÷1e6", ""},
        {"R_kd", type_float, &right_motors.kd, "右kd内部×1e6", ""},

        {"L_st_dz", type_float, &left_motors.static_deadzone, "左静摩擦", "V"},
        {"L_dy_dz", type_float, &left_motors.dynamic_deadzone, "左动摩擦", "V"},
        {"R_st_dz", type_float, &right_motors.static_deadzone, "右静摩擦", "V"},
        {"R_dy_dz", type_float, &right_motors.dynamic_deadzone, "右动摩擦", "V"},

        // ========== 电压映射（速度环归一输出 → 四路实际电压）==========
        // V = 摩擦 + volt_min + |out|·(volt_max-摩擦-volt_min)·volt_factor
        {"L_vmin0", type_float, &left_motors.volt_min[0], "左0最小电压", "V"},
        {"L_vmin1", type_float, &left_motors.volt_min[1], "左1最小电压", "V"},
        {"L_vmin2", type_float, &left_motors.volt_min[2], "左2最小电压", "V"},
        {"L_vmin3", type_float, &left_motors.volt_min[3], "左3最小电压", "V"},
        {"R_vmin0", type_float, &right_motors.volt_min[0], "右0最小电压", "V"},
        {"R_vmin1", type_float, &right_motors.volt_min[1], "右1最小电压", "V"},
        {"R_vmin2", type_float, &right_motors.volt_min[2], "右2最小电压", "V"},
        {"R_vmin3", type_float, &right_motors.volt_min[3], "右3最小电压", "V"},
        {"L_vmax0", type_float, &left_motors.volt_max[0], "左0最大电压", "V"},
        {"L_vmax1", type_float, &left_motors.volt_max[1], "左1最大电压", "V"},
        {"L_vmax2", type_float, &left_motors.volt_max[2], "左2最大电压", "V"},
        {"L_vmax3", type_float, &left_motors.volt_max[3], "左3最大电压", "V"},
        {"R_vmax0", type_float, &right_motors.volt_max[0], "右0最大电压", "V"},
        {"R_vmax1", type_float, &right_motors.volt_max[1], "右1最大电压", "V"},
        {"R_vmax2", type_float, &right_motors.volt_max[2], "右2最大电压", "V"},
        {"R_vmax3", type_float, &right_motors.volt_max[3], "右3最大电压", "V"},
        {"L_vf0", type_float, &left_motors.volt_factor[0], "左0电压系数", ""},
        {"L_vf1", type_float, &left_motors.volt_factor[1], "左1电压系数", ""},
        {"L_vf2", type_float, &left_motors.volt_factor[2], "左2电压系数", ""},
        {"L_vf3", type_float, &left_motors.volt_factor[3], "左3电压系数", ""},
        {"R_vf0", type_float, &right_motors.volt_factor[0], "右0电压系数", ""},
        {"R_vf1", type_float, &right_motors.volt_factor[1], "右1电压系数", ""},
        {"R_vf2", type_float, &right_motors.volt_factor[2], "右2电压系数", ""},
        {"R_vf3", type_float, &right_motors.volt_factor[3], "右3电压系数", ""},
        {"L_odz", type_float, &left_motors.output_deadzone, "左速度死区", "pct"},
        {"R_odz", type_float, &right_motors.output_deadzone, "右速度死区", "pct"},

        // ========== 位置环 ==========
        {"d_kp", type_float, &robot_action.distance_pid.kp, "距离kp 输出/误差", ""},
        {"d_ki", type_float, &robot_action.distance_pid.ki, "距离ki内部÷1e6", ""},
        {"d_kd", type_float, &robot_action.distance_pid.kd, "距离kd内部×1e6", ""},
        {"d_max", type_float, &robot_action.distance_pid.max_output, "距离环限速", "pct"},

        {"h_kp", type_float, &robot_action.heading_pid.kp, "纠偏kp 输出/误差", ""},
        {"h_ki", type_float, &robot_action.heading_pid.ki, "纠偏ki内部÷1e6", ""},
        {"h_kd", type_float, &robot_action.heading_pid.kd, "纠偏kd内部×1e6", ""},
        {"h_max", type_float, &robot_action.heading_pid.max_output, "纠偏限速", "pct"},

        {"t_kp", type_float, &robot_action.turn_pid.kp, "转向kp 输出/误差", ""},
        {"t_ki", type_float, &robot_action.turn_pid.ki, "转向ki内部÷1e6", ""},
        {"t_kd", type_float, &robot_action.turn_pid.kd, "转向kd内部×1e6", ""},
        {"t_max", type_float, &robot_action.turn_pid.max_output, "转向限速", "pct"},

        // ========== 几何 / 满速 / 容差 ==========
        {"wheel_r", type_float, &chassis.wheel_r, "轮半径", "m"},
        {"track_w", type_float, &chassis.track_width, "轮距", "m"},
        {"fwd_off", type_float, &chassis.forward_tracking_offset, "前向轮偏右", "m"},
        {"side_off", type_float, &chassis.side_tracking_offset, "侧向轮偏后", "m"},
        {"L_max", type_float, &chassis.left_max_speed, "左满速", "pct"},
        {"R_max", type_float, &chassis.right_max_speed, "右满速", "pct"},

        {"dist_tol", type_float, &robot_action.distance_tolerance, "距离容差", "m"},
        {"v_tol", type_float, &robot_action.linear_speed_tolerance, "线速度容差", "m/s"},
        {"head_tol", type_float, &robot_action.heading_tolerance, "直线航向容差", "deg"},
        {"ang_tol", type_float, &robot_action.angle_tolerance, "转角容差", "deg"},
        {"w_tol", type_float, &robot_action.angular_speed_tolerance, "角速度容差", "deg/s"},
        {"man_dz", type_int32_t, &robot_action.manual_deadzone, "摇杆死区", ""},
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

        // 瞬时速度（判停、StableJudge 对照）
        {"v_lin", type_float, &chassis.linear_speed, "线速度", "m/s", monitor_tag_fast},
        {"w_ang", type_float, &chassis.angular_speed, "角速度", "deg/s", monitor_tag_fast},

        // 定位轮原始累计（第4步正负与尺度）
        {"trk_fwd", type_float, &chassis.forward_tracking_distance, "前向轮里程", "m", monitor_tag_slow},
        {"trk_side", type_float, &chassis.side_tracking_distance, "侧向轮里程", "m", monitor_tag_slow},

        // 指令电压：有稳定地址，内存项即可（无需 getter）
        {"L0_Vcmd", type_float, &left_motors.volt_output[0], "左0指令电压", "V", monitor_tag_fast},
        {"R0_Vcmd", type_float, &right_motors.volt_output[0], "右0指令电压", "V", monitor_tag_fast},
        {"L1_Vcmd", type_float, &left_motors.volt_output[1], "左1指令电压", "V", monitor_tag_slow},
        {"R1_Vcmd", type_float, &right_motors.volt_output[1], "右1指令电压", "V", monitor_tag_slow},
        {"L2_Vcmd", type_float, &left_motors.volt_output[2], "左2指令电压", "V", monitor_tag_slow},
        {"R2_Vcmd", type_float, &right_motors.volt_output[2], "右2指令电压", "V", monitor_tag_slow},
        {"L3_Vcmd", type_float, &left_motors.volt_output[3], "左3指令电压", "V", monitor_tag_slow},
        {"R3_Vcmd", type_float, &right_motors.volt_output[3], "右3指令电压", "V", monitor_tag_slow},

        // 指令转速：速度环目标，内存项直接读（与 L0/R0_rpm 对照即跟踪误差）
        {"L_tgt", type_float, &left_motors.target, "左指令转速", "pct", monitor_tag_fast},
        {"R_tgt", type_float, &right_motors.target, "右指令转速", "pct", monitor_tag_fast},

        // 转速/电流/编码器：无捕获 lambda 直接转 MenuFloatGetter，构造自动置 GETTER；
        // SDK 读数是 double，cast 成 float 匹配签名。加/删一项只动这一行
        // 转速只取 motors[0]（与轮轴直连，经齿轮传动的读数不代表轮速），pct 与 target 同口径
        {"L0_rpm", []
         { return static_cast<float>(left_motors.motors[0]->velocity(vex::velocityUnits::pct)); }, "左0转速", "pct", monitor_tag_fast},
        {"R0_rpm", []
         { return static_cast<float>(right_motors.motors[0]->velocity(vex::velocityUnits::pct)); }, "右0转速", "pct", monitor_tag_fast},

        // 实际电压：SDK 回读，与指令电压 Vcmd 对照（差距大说明被限幅/堵转）
        {"L0_Vact", []
         { return static_cast<float>(left_motors.motors[0]->voltage(vex::voltageUnits::volt)); }, "左0实际电压", "V", monitor_tag_fast},
        {"R0_Vact", []
         { return static_cast<float>(right_motors.motors[0]->voltage(vex::voltageUnits::volt)); }, "右0实际电压", "V", monitor_tag_fast},
        {"L1_Vact", []
         { return static_cast<float>(left_motors.motors[1]->voltage(vex::voltageUnits::volt)); }, "左1实际电压", "V", monitor_tag_slow},
        {"R1_Vact", []
         { return static_cast<float>(right_motors.motors[1]->voltage(vex::voltageUnits::volt)); }, "右1实际电压", "V", monitor_tag_slow},
        {"L2_Vact", []
         { return static_cast<float>(left_motors.motors[2]->voltage(vex::voltageUnits::volt)); }, "左2实际电压", "V", monitor_tag_slow},
        {"R2_Vact", []
         { return static_cast<float>(right_motors.motors[2]->voltage(vex::voltageUnits::volt)); }, "右2实际电压", "V", monitor_tag_slow},
        {"L3_Vact", []
         { return static_cast<float>(left_motors.motors[3]->voltage(vex::voltageUnits::volt)); }, "左3实际电压", "V", monitor_tag_slow},
        {"R3_Vact", []
         { return static_cast<float>(right_motors.motors[3]->voltage(vex::voltageUnits::volt)); }, "右3实际电压", "V", monitor_tag_slow},

        {"L0_I", []
         { return static_cast<float>(left_motors.motors[0]->current(vex::currentUnits::amp)); }, "左0电流", "A", monitor_tag_slow},
        {"L0_pos", []
         { return static_cast<float>(left_motors.motors[0]->position(vex::rotationUnits::rev)); }, "左0编码器", "rev", monitor_tag_slow},
        {"L1_I", []
         { return static_cast<float>(left_motors.motors[1]->current(vex::currentUnits::amp)); }, "左1电流", "A", monitor_tag_slow},
        {"L1_pos", []
         { return static_cast<float>(left_motors.motors[1]->position(vex::rotationUnits::rev)); }, "左1编码器", "rev", monitor_tag_slow},
        {"L2_I", []
         { return static_cast<float>(left_motors.motors[2]->current(vex::currentUnits::amp)); }, "左2电流", "A", monitor_tag_slow},
        {"L2_pos", []
         { return static_cast<float>(left_motors.motors[2]->position(vex::rotationUnits::rev)); }, "左2编码器", "rev", monitor_tag_slow},
        {"L3_I", []
         { return static_cast<float>(left_motors.motors[3]->current(vex::currentUnits::amp)); }, "左3电流", "A", monitor_tag_slow},
        {"L3_pos", []
         { return static_cast<float>(left_motors.motors[3]->position(vex::rotationUnits::rev)); }, "左3编码器", "rev", monitor_tag_slow},

        {"R0_I", []
         { return static_cast<float>(right_motors.motors[0]->current(vex::currentUnits::amp)); }, "右0电流", "A", monitor_tag_slow},
        {"R0_pos", []
         { return static_cast<float>(right_motors.motors[0]->position(vex::rotationUnits::rev)); }, "右0编码器", "rev", monitor_tag_slow},
        {"R1_I", []
         { return static_cast<float>(right_motors.motors[1]->current(vex::currentUnits::amp)); }, "右1电流", "A", monitor_tag_slow},
        {"R1_pos", []
         { return static_cast<float>(right_motors.motors[1]->position(vex::rotationUnits::rev)); }, "右1编码器", "rev", monitor_tag_slow},
        {"R2_I", []
         { return static_cast<float>(right_motors.motors[2]->current(vex::currentUnits::amp)); }, "右2电流", "A", monitor_tag_slow},
        {"R2_pos", []
         { return static_cast<float>(right_motors.motors[2]->position(vex::rotationUnits::rev)); }, "右2编码器", "rev", monitor_tag_slow},
        {"R3_I", []
         { return static_cast<float>(right_motors.motors[3]->current(vex::currentUnits::amp)); }, "右3电流", "A", monitor_tag_slow},
        {"R3_pos", []
         { return static_cast<float>(right_motors.motors[3]->position(vex::rotationUnits::rev)); }, "右3编码器", "rev", monitor_tag_slow},

        // 手柄（手动/Arcade 对照）
        {"axis_Lx", type_int32_t, &left_axis.value[left_axis.value_p].value_x, "左杆X", nullptr, monitor_tag_slow},
        {"axis_Ly", type_int32_t, &left_axis.value[left_axis.value_p].value_y, "左杆Y", nullptr, monitor_tag_slow},
        {"axis_Rx", type_int32_t, &right_axis.value[right_axis.value_p].value_x, "右杆X", nullptr, monitor_tag_slow},
        {"axis_Ry", type_int32_t, &right_axis.value[right_axis.value_p].value_y, "右杆Y", nullptr, monitor_tag_slow},
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
