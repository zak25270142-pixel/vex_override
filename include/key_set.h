#ifndef __KEY_SET_H__
#define __KEY_SET_H__

#include "my_main.h"
#include "Control_func.h"

#define PRESS(obj) []() { (obj).pressed(); }
#define RELEASE(obj) []() { (obj).released(); }

// 控制器对象
extern vex::controller Remote_Controller;

// 遥控轴处理对象
extern Remote_Control right_axis;
extern Remote_Control left_axis;

// 逻辑按键对象
extern Control_key key_enter;
extern Control_key key_up;
extern Control_key key_down;
extern Control_key key_left;
extern Control_key key_right;
extern Control_key key_add;
extern Control_key key_reduce;
extern Control_key key_back;
extern Control_key key_shift;

// 屏幕按钮对象
extern Screen_Button button1;
extern Screen_Button button2;
extern Screen_Button button3;
extern Screen_Button button4;
extern Screen_Button button5;

// 屏幕按钮数组
extern Screen_Button *screen_buttons[];
extern const uint8_t SCREEN_BUTTON_COUNT;
extern Screen_Button *current_pressed_button;
extern Screen_Button::Draw_state screen_button_need_draw;

void key_init1();
void screen_pressed_callback();
void screen_released_callback();
void draw_button();

void menu_key_reset();
void menu_tc_key_reset();
void pre_menu_key();
void ai_vision_menu_key();


#endif
