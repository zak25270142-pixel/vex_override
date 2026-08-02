#include "my_main.h"
#include "menu_func.h"
#include "key_set.h"
#include "LCD_menu.h"

bool in_pre_menu = true;

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
        {"imu_pitch", type_double, &imu_att.pitch, "ms"},
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

void refresh_menu()
{
    if (in_pre_menu)
    {
        if (key_enter.read())
        {
            in_pre_menu = false;
            menu_key_reset();
            menu.refresh();
        }
    }
    else
    {
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
            else // 进入前菜单
                pre_menu_init();
        }
        else if (key_shift.read())
            menu.shift();
        else if (key_add.read())
            menu.add();
        else if (key_reduce.read())
            menu.reduce();
        else if (menu.is_monitor_menu)
        { // 每次都刷新
            menu.refresh_value();
        }
    }
};

void pre_menu_init()
{
    Brain.Screen.drawRectangle(0, 0, 480, 272, vex::black);
    pre_menu_key();
    in_pre_menu = true;
}