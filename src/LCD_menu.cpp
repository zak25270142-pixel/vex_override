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

uint32_t Lucky_num = 114514;
uint32_t Lucky_num2 = 1919810;
bool A_bool = false;
float ANGLE = 114.514;
uint64_t so_long_num = 1145141919810;
uint8_t LED_PC13_state = 0;
uint16_t LED5_pwm_num = 990;
uint8_t small_num = 114;
int8_t int8tnum = -114;
int16_t int16tnum = -1145;
int32_t int32tnum = -1145141909;
int64_t int64tnum = -1145141909810114514;

const struct MENU_ITEM menu_item[] =
    {
        // ========== 速度环（左右）==========
        {"L_kf", type_float, &left_motors.kf, "左速度前馈kf", ""},
        {"L_kp", type_float, &left_motors.kp, "左速度kp", ""},
        {"L_ki", type_float, &left_motors.ki, "左速度ki(内部)", ""},
        {"L_kd", type_float, &left_motors.kd, "左速度kd(内部)", ""},
        {"R_kf", type_float, &right_motors.kf, "右速度前馈kf", ""},
        {"R_kp", type_float, &right_motors.kp, "右速度kp", ""},
        {"R_ki", type_float, &right_motors.ki, "右速度ki(内部)", ""},
        {"R_kd", type_float, &right_motors.kd, "右速度kd(内部)", ""},

        {"L_st_dz", type_float, &left_motors.static_deadzone, "左静摩擦", "V"},
        {"L_dy_dz", type_float, &left_motors.dynamic_deadzone, "左动摩擦", "V"},
        {"R_st_dz", type_float, &right_motors.static_deadzone, "右静摩擦", "V"},
        {"R_dy_dz", type_float, &right_motors.dynamic_deadzone, "右动摩擦", "V"},

        // ========== 位置环 ==========
        {"d_kp", type_float, &robot_action.distance_pid.kp, "距离环kp", ""},
        {"d_ki", type_float, &robot_action.distance_pid.ki, "距离环ki(内部)", ""},
        {"d_kd", type_float, &robot_action.distance_pid.kd, "距离环kd(内部)", ""},
        {"d_max", type_float, &robot_action.distance_pid.max_output, "距离环限速", "pct"},

        {"h_kp", type_float, &robot_action.heading_pid.kp, "纠偏kp", ""},
        {"h_ki", type_float, &robot_action.heading_pid.ki, "纠偏ki(内部)", ""},
        {"h_kd", type_float, &robot_action.heading_pid.kd, "纠偏kd(内部)", ""},
        {"h_max", type_float, &robot_action.heading_pid.max_output, "纠偏限速", "pct"},

        {"t_kp", type_float, &robot_action.turn_pid.kp, "转向kp", ""},
        {"t_ki", type_float, &robot_action.turn_pid.ki, "转向ki(内部)", ""},
        {"t_kd", type_float, &robot_action.turn_pid.kd, "转向kd(内部)", ""},
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
// tag决定上位机连接后的默认推送档位；x/y/yaw还兼作场地图的语义标记。
static struct MENU_ITEM monitor_menu_item[] =
    {
        // 场地图语义：高速推送（tag 3/4/5）
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
