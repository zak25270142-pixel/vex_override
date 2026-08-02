#include "key_set.h"
#include "timer.h"

vex::controller Remote_Controller = vex::controller(vex::primary); // 生成遥控 类

Remote_Control right_axis(&TimerA);
Remote_Control left_axis(&TimerA);

Control_key key_enter(&TimerA);
Control_key key_up(&TimerA);
Control_key key_down(&TimerA);
Control_key key_left(&TimerA);
Control_key key_right(&TimerA);
Control_key key_add(&TimerA);
Control_key key_reduce(&TimerA);
Control_key key_back(&TimerA);
Control_key key_shift(&TimerA);

Screen_Button button1(nullptr); // 暂不关联逻辑键，且不可用
Screen_Button button2(nullptr);
Screen_Button button3(nullptr);
Screen_Button button4(nullptr);
Screen_Button button5(nullptr);

Screen_Button *screen_buttons[] = {
    &button1, &button2, &button3, &button4, &button5};
const uint8_t SCREEN_BUTTON_COUNT = sizeof(screen_buttons) / sizeof(screen_buttons[0]);
Screen_Button *current_pressed_button = nullptr;
Screen_Button::Draw_state screen_button_need_draw = Screen_Button::not_draw;

void key_init1()
{
    // 屏幕按压回调
    Brain.Screen.pressed(screen_pressed_callback);
    Brain.Screen.released(screen_released_callback);
    // 遥控器手柄回调
    Remote_Controller.Axis1.changed([]()
                                    { right_axis.refresh(Remote_Controller.Axis1.value(), Remote_Controller.Axis2.value()); });
    Remote_Controller.Axis2.changed([]()
                                    { right_axis.refresh(Remote_Controller.Axis1.value(), Remote_Controller.Axis2.value()); });
    Remote_Controller.Axis3.changed([]()
                                    { left_axis.refresh(Remote_Controller.Axis4.value(), Remote_Controller.Axis3.value()); });
    Remote_Controller.Axis4.changed([]()
                                    { left_axis.refresh(Remote_Controller.Axis4.value(), Remote_Controller.Axis3.value()); });
    // 手柄方向绑定
    left_axis.left_state = &key_left;
    left_axis.right_state = &key_right;
    left_axis.up_state = &key_up;
    left_axis.down_state = &key_down;

    right_axis.up_state = &key_add;
    right_axis.down_state = &key_reduce;
    right_axis.left_state = &key_back;
    right_axis.right_state = &key_enter;

    // 遥控按键回调 - 按下
    Remote_Controller.ButtonA.pressed(PRESS(key_enter));
    Remote_Controller.ButtonB.pressed(PRESS(key_back));
    Remote_Controller.ButtonX.pressed(PRESS(key_shift));
    Remote_Controller.ButtonY.pressed(nullptr);
    Remote_Controller.ButtonUp.pressed(PRESS(key_up));
    Remote_Controller.ButtonDown.pressed(PRESS(key_down));
    Remote_Controller.ButtonLeft.pressed(PRESS(key_reduce));
    Remote_Controller.ButtonRight.pressed(PRESS(key_add));
    Remote_Controller.ButtonL1.pressed(nullptr);
    Remote_Controller.ButtonL2.pressed(nullptr);
    Remote_Controller.ButtonR1.pressed(nullptr);
    Remote_Controller.ButtonR2.pressed(nullptr);
    // 遥控按键回调 - 释放
    Remote_Controller.ButtonA.released(RELEASE(key_enter));
    Remote_Controller.ButtonB.released(RELEASE(key_back));
    Remote_Controller.ButtonX.released(RELEASE(key_shift));
    Remote_Controller.ButtonY.released(nullptr);
    Remote_Controller.ButtonUp.released(RELEASE(key_up));
    Remote_Controller.ButtonDown.released(RELEASE(key_down));
    Remote_Controller.ButtonLeft.released(RELEASE(key_reduce));
    Remote_Controller.ButtonRight.released(RELEASE(key_add));
    Remote_Controller.ButtonL1.released(nullptr);
    Remote_Controller.ButtonL2.released(nullptr);
    Remote_Controller.ButtonR1.released(nullptr);
    Remote_Controller.ButtonR2.released(nullptr);

    button1.reset_size(427, 5, 45, 40);
    button2.reset_size(427, 50, 45, 40);
    button3.reset_size(427, 95, 45, 40);
    button4.reset_size(427, 140, 45, 40);
    button5.reset_size(427, 185, 45, 40);

    key_enter.mode = Control_key::lpress;
    key_enter.first_press_gap = 65535;

    button5.reset_button_name("enter", 2, 16);
    button5.reset_button_color(vex::blue, vex::color(0x000077));
    button5.set_key(&key_enter);
    button5.can_use = true;
    button5.draw_button(Screen_Button::draw_released);
}
void pre_menu_key()
{
    key_up.mode = Control_key::disable;
    key_down.mode = Control_key::disable;
    key_shift.mode = Control_key::disable;
    key_enter.mode = Control_key::lpress;
    key_enter.first_press_gap = 65535;
    key_back.mode = Control_key::disable;
    key_add.mode = Control_key::disable;
    key_reduce.mode = Control_key::disable;
    key_left.mode = Control_key::disable;
    key_right.mode = Control_key::disable;

    button1.reset_size(20, 5, 400, 40);
    button1.reset_button_name("enter_menu", 14, 16);
    button1.reset_button_color(vex::color(0xff8800), vex::color(0x663300));
    button1.set_key(&key_enter);
    button1.can_use = true;
    button1.draw_button(Screen_Button::draw_released);

    button2.can_use = false;
    button3.can_use = false;
    button4.can_use = false;
    button5.can_use = false;
}

void menu_key_reset()
{
    button1.reset_size(427, 5, 45, 40);
    button2.reset_size(427, 50, 45, 40);
    button3.reset_size(427, 95, 45, 40);
    button4.reset_size(427, 140, 45, 40);
    button5.reset_size(427, 185, 45, 40);

    key_up.mode = Control_key::spress;
    key_down.mode = Control_key::spress;
    key_shift.mode = Control_key::lpress;
    key_shift.first_press_gap = 65535; // 等于短按但是下降沿触发
    key_enter.mode = Control_key::lpress;
    key_enter.first_press_gap = 65535;
    key_back.mode = Control_key::lpress;
    key_back.first_press_gap = 65535;
    key_add.mode = Control_key::disable;
    key_reduce.mode = Control_key::disable;
    key_left.mode = Control_key::disable;
    key_right.mode = Control_key::disable;

    button1.reset_button_name("up", 14, 16);
    button1.reset_button_color(vex::red, vex::color(0x660000));
    button1.set_key(&key_up);
    button1.can_use = true;
    button1.draw_button(Screen_Button::draw_released);

    button2.reset_button_name("down", 6, 16);
    button2.reset_button_color(vex::green, vex::color(0x005500));
    button2.set_key(&key_down);
    button2.can_use = true;
    button2.draw_button(Screen_Button::draw_released);

    button3.reset_button_name("shift", 2, 16);
    button3.reset_button_color(vex::purple, vex::color(0x6d136d));
    button3.set_key(&key_shift);
    button3.can_use = true;
    button3.draw_button(Screen_Button::draw_released);

    button4.reset_button_name("enter", 2, 16);
    button4.reset_button_color(vex::blue, vex::color(0x000077));
    button4.set_key(&key_enter);
    button4.can_use = true;
    button4.draw_button(Screen_Button::draw_released);

    button5.reset_button_name("back", 6, 16);
    button5.reset_button_color(vex::yellow, vex::color(0x82620e));
    button5.set_key(&key_back);
    button5.can_use = true;
    button5.draw_button(Screen_Button::draw_released);
}

void menu_tc_key_reset()
{
    button1.reset_size(427, 5, 45, 40);
    button2.reset_size(427, 50, 45, 40);
    button3.reset_size(427, 95, 45, 40);
    button4.reset_size(427, 140, 45, 40);
    button5.reset_size(427, 185, 45, 40);

    key_up.mode = Control_key::spress;
    key_down.mode = Control_key::spress;
    key_add.mode = Control_key::spress;
    key_reduce.mode = Control_key::spress;
    key_back.mode = Control_key::lpress;
    key_back.first_press_gap = 65535;
    key_shift.mode = Control_key::disable;
    key_enter.mode = Control_key::disable;
    key_left.mode = Control_key::disable;
    key_right.mode = Control_key::disable;

    button1.reset_button_name("e+", 14, 16);
    button1.reset_button_color(vex::red, vex::color(0x660000));
    button1.set_key(&key_up);
    button1.can_use = true;
    button1.draw_button(Screen_Button::draw_released);

    button2.reset_button_name("e-", 14, 16);
    button2.reset_button_color(vex::green, vex::color(0x005500));
    button2.set_key(&key_down);
    button2.can_use = true;
    button2.draw_button(Screen_Button::draw_released);

    button3.reset_button_name("v+", 14, 16);
    button3.reset_button_color(vex::color(0x00FFFF), vex::color(0x137272));
    button3.set_key(&key_add);
    button3.can_use = true;
    button3.draw_button(Screen_Button::draw_released);

    button4.reset_button_name("v-", 14, 16);
    button4.reset_button_color(vex::white, vex::color(0x666666));
    button4.set_key(&key_reduce);
    button4.can_use = true;
    button4.draw_button(Screen_Button::draw_released);

    button5.reset_button_name("back", 6, 16);
    button5.reset_button_color(vex::yellow, vex::color(0x82620e));
    button5.set_key(&key_back);
    button5.can_use = true;
    button5.draw_button(Screen_Button::draw_released);
}

// 屏幕按下回调
void screen_pressed_callback()
{
    // 检查所有屏幕按钮
    int x = Brain.Screen.xPosition();
    int y = Brain.Screen.yPosition();
    for (int i = 0; i < SCREEN_BUTTON_COUNT; i++)
    {
        if (screen_buttons[i]->check_press(x, y))
        {
            current_pressed_button = screen_buttons[i];
            screen_buttons[i]->trigger_press(true);
            break;
        }
    }
    screen_button_need_draw = Screen_Button::draw_pressed;
}
// 屏幕释放回调
void screen_released_callback()
{
    if (current_pressed_button != nullptr)
    {
        current_pressed_button->trigger_press(false);
        current_pressed_button = nullptr;
    }
    screen_button_need_draw = Screen_Button::draw_released;
}

// 在主循环使用
void draw_button()
{
    static Screen_Button *last_draw_pressed_button = nullptr;

    if (screen_button_need_draw)
    {
        if (last_draw_pressed_button != nullptr)
        {
            last_draw_pressed_button->draw_button(screen_button_need_draw);
            last_draw_pressed_button = nullptr;
        }

        if (current_pressed_button != nullptr)
        {
            current_pressed_button->draw_button(screen_button_need_draw);
            last_draw_pressed_button = current_pressed_button; // 记录已绘制按下的按键
        }
    }
    screen_button_need_draw = Screen_Button::not_draw;
}
