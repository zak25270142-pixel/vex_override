#include "Control_func.h"

// 构造函数
Screen_Button::Screen_Button(Control_key *set, bool can_use) : key_set(set), can_use(can_use)
{
    size = {{0, 0}, 40, 40};
}
void Screen_Button::set_key(Control_key *key)
{
    key_set = key;
}

void Screen_Button::trigger_press(bool is_pressed)
{
    if (key_set != nullptr)
    {
        if (is_pressed)
        {
            key_set->pressed();
        }
        else
        {
            key_set->released();
        }
    }
}

// 判断按键此刻是否被按下
bool Screen_Button::check_press(int x, int y)
{
    if (key_set == nullptr || key_set->mode == Control_key::disable)
    {
        can_use = false; // 可选：这里也可以强制同步 can_use 状态
        return false;
    }
    if (can_use && Brain.Screen.pressing())
    {
        if (x >= size.start_p.x && x <= size.start_p.x + size.width &&
            y >= size.start_p.y && y <= size.start_p.y + size.height)
        {
            return true;
        }
    }
    return false;
}
// 重置按钮大小和位置
void Screen_Button::reset_size(int16_t x, int16_t y, int16_t width, int16_t height)
{
    Brain.Screen.setPenColor(vex::color::black);
    Brain.Screen.drawRectangle(size.start_p.x, size.start_p.y, size.width, size.height, vex::color::black);
    Brain.Screen.setPenColor(vex::color::white);
    size.start_p.x = x;
    size.start_p.y = y;
    size.width = width;
    size.height = height;
}
// 重置按钮颜色
void Screen_Button::reset_button_color(vex::color bg_color, vex::color bgp_color)
{
    button.bg_color = bg_color;
    button.bgp_color = bgp_color;
}
// 重置按钮文字
void Screen_Button::reset_button_name(const char *name, int16_t x, int16_t y, vex::color name_color, vex::fontType font)
{
    button.name = name;
    button.font = font;
    button.name_pos.x = x;
    button.name_pos.y = y;
}
// 重置按钮图片
void Screen_Button::reset_button_pic(uint32_t *PIC_p, int16_t x, int16_t y, int16_t width, int16_t height)
{
    button.PIC_p = PIC_p;
    button.pic_size.start_p.x = x;
    button.pic_size.start_p.y = y;
    button.pic_size.width = width;
    button.pic_size.height = height;
}
// 绘制按钮
void Screen_Button::draw_button(Draw_state need_draw)
{
    if (can_use && need_draw)
    {
        reset_origin(size.start_p.x, size.start_p.y, size.width, size.height);
        if (need_draw == draw_pressed)
            Brain.Screen.drawRectangle(-1, -1, size.width + 2, size.height + 2, button.bgp_color);
        else if (need_draw == draw_released)
            Brain.Screen.drawRectangle(-1, -1, size.width + 2, size.height + 2, button.bg_color);
        if (button.name != nullptr)
        {
            Brain.Screen.setPenColor(button.name_color);
            Brain.Screen.setFont(button.font);
            Brain.Screen.printAt(button.name_pos.x, button.name_pos.y, false, button.name);
        }
        if (button.PIC_p != nullptr)
            Brain.Screen.drawImageFromBuffer(button.PIC_p, button.pic_size.start_p.x, button.pic_size.start_p.y,
                                             button.pic_size.width, button.pic_size.height);
        reset_origin();
    }
}

Control_key::Control_key(vex::timer *t, uint16_t press_gap, uint16_t first_press_gap,
                         uint16_t flag_clear_gap, KEY_MODE mode)
    : TIMER(t), press_gap(press_gap), first_press_gap(first_press_gap),
      flag_clear_gap(flag_clear_gap), mode(mode)
{
    first_press_time = 0;
    end_press_time = 0;
    last_press_time = 0;
    now_time = 0;
    is_pressed = false;
    is_long_pressed = false;
    state = 0;
}
void Control_key::refresh()
{
    if (TIMER)
        now_time = vex::timer::system();
    else
        now_time = TIMER->time();
    if (mode == disable)
        return;
    if (state)
    { // 如果按下
        if (first_press_time == 0)
        { // 上次状态为未按下(下降沿)
            first_press_time = now_time;
            if (mode == normal_press || mode == spress)
            {
                is_pressed = true;
                last_press_time = first_press_time;
            }
        }
        else
        {
            if (mode == spress)
            {
                if (now_time >= first_press_gap + first_press_time &&
                    now_time >= press_gap + last_press_time)
                {
                    last_press_time = now_time;
                    is_pressed = true;
                }
            }
        }
    }
    else
    { // 如果未按下
        if (first_press_time)
        { // 上次状态为已按下(上升沿)
            if (mode == lpress)
            {
                if (now_time >= first_press_gap + first_press_time)
                    is_long_pressed = true;
                else
                    is_pressed = true;
            }
            end_press_time = now_time;
            first_press_time = 0;
        }
        else if (now_time >= end_press_time + flag_clear_gap)
        { // 按键读取超时清除
            is_long_pressed = false;
            is_pressed = false;
        }
    }
}
// 读取按键状态
bool Control_key::read(bool get_long_press, bool only_read)
{
    refresh();
    if (mode == disable)
        return false;
    if (only_read)
    {
        return get_long_press ? is_long_pressed : is_pressed;
    }
    else
    {
        bool ans = get_long_press ? is_long_pressed : is_pressed;
        is_long_pressed = false;
        is_pressed = false;
        return ans;
    }
}
void Control_key::pressed()
{
    state++;
    refresh();
}
void Control_key::released()
{
    state--;
    if (state < 0)
    {
        state = 0;
    }
    refresh();
}

void Remote_Control::set_state()
{
    int32_t x = value[value_p].value_x;
    int32_t y = value[value_p].value_y;
    STATE last_state = state;
    if (x * x + y * y <= 10)
        state = middle;
    else if (x >= 110)
        state = right;
    else if (x <= -110)
        state = left;
    else if (y >= 110)
        state = up;
    else if (y <= -110)
        state = down;
    else
        state = other;
    if (last_state != state)
    {
        switch (last_state)
        {
        case up:
            if (up_state != nullptr)
                up_state->released();
            break;
        case down:
            if (down_state != nullptr)
                down_state->released();
            break;
        case left:
            if (left_state != nullptr)
                left_state->released();
            break;
        case right:
            if (right_state != nullptr)
                right_state->released();
            break;
        default:
            break;
        }
        switch (state)
        {
        case up:
            if (up_state != nullptr)
                up_state->pressed();
            break;
        case down:
            if (down_state != nullptr)
                down_state->pressed();
            break;
        case left:
            if (left_state != nullptr)
                left_state->pressed();
            break;
        case right:
            if (right_state != nullptr)
                right_state->pressed();
            break;
        default:
            break;
        }
    }
}

void Remote_Control::refresh(int32_t current_value_x, int32_t current_value_y)
{
    uint32_t now;
    if (TIMER)
        now = vex::timer::system();
    else
        now = TIMER->time();
    if (now - 2 >= value[value_p].time)
    {
        value_p = (value_p + 1) % 8;
        value[value_p].time = now;
        value[value_p].value_x = current_value_x;
        value[value_p].value_y = current_value_y;
        changed = true;
        set_state();
    }
}
