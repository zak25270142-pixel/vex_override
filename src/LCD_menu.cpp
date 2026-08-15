#include "my_main.h"
#include "menu_func.h"
#include "key_set.h"
#include "LCD_menu.h"
#include "ai_vision.h"

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
        {"Lucky_num", type_uint32_t, &Lucky_num, "ms"},
        {"Lucky_num2", type_uint32_t, &Lucky_num2},
        {"LED PC13", type_bool, &LED_PC13_state},
        {"LED5 PWM", type_uint16_t, &LED5_pwm_num},
        {"ANGLE(float)", type_float, &ANGLE},
        {"this is a so long num", type_uint64_t, &so_long_num},
        {"this is an int8_t num", type_int8_t, &int8tnum},
        {"this is an int16_t num", type_int16_t, &int16tnum},
        {"this is an int32_t num", type_int32_t, &int32tnum},
        {"this is an int64_t num", type_int64_t, &int64tnum},
        {"small num", type_uint8_t, &small_num},
};

static const struct MENU_ITEM monitor_menu_item[] =
    {
        {"axis_left_x", type_int32_t, &left_axis.value[left_axis.value_p].value_x},
        {"Lucky_num2", type_uint32_t, &Lucky_num2},
        {"LED PC13", type_bool, &LED_PC13_state},
        {"LED5 PWM", type_uint16_t, &LED5_pwm_num},
        {"ANGLE(float)", type_float, &ANGLE},
        {"this is a so long num", type_uint64_t, &so_long_num},
        {"this is an int8_t num", type_int8_t, &int8tnum},
        {"this is an int16_t num", type_int16_t, &int16tnum},
        {"this is an int32_t num", type_int32_t, &int32tnum},
        {"this is an int64_t num", type_int64_t, &int64tnum},
        {"small num", type_uint8_t, &small_num},
};

MENU menu(menu_item,
          sizeof(menu_item) / sizeof(menu_item[0]),
          menu_key_reset,
          menu_tc_key_reset,
          monitor_menu_item,
          sizeof(monitor_menu_item) / sizeof(monitor_menu_item[0]));

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
