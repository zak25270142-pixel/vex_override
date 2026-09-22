#ifndef __CONTROL_FUNC_H__
#define __CONTROL_FUNC_H__

#include "my_main.h"

class Control_key // 处理按键信号
{
private:
    uint32_t first_press_time; // 最初按下时刻
    uint32_t end_press_time;   // 最后按下时刻
    uint32_t last_press_time;  // 上次输出时刻
    uint32_t now_time;         // 当前时间(ms)
    bool is_pressed;           // 可生效的有效按下标记
    bool is_long_pressed;

public:
    enum KEY_MODE
    {
        disable,
        normal_press, // 下降沿触发，只触发单次
        spress,       // 支持长按连点
        lpress,       // 上升沿触发，支持短按长按
    };
    uint16_t press_gap;       // 长按间隔时间(ms)
    uint16_t first_press_gap; // 第一次长按判定阈值(ms)
    uint16_t flag_clear_gap;  // 按键信息长时间未处理清零阈值
    KEY_MODE mode;            // 按键模式
    int8_t state;             // 按键实际情况
    Control_key(uint16_t press_gap = 100, uint16_t first_press_gap = 500,
                uint16_t flag_clear_gap = 2000, KEY_MODE mode = disable);
    void refresh();
    bool read(bool get_long_press = false, bool only_read = false);
    void released(); // 允许多处按下，全部松开才为0
    void pressed();
};

// 生成有视觉属性的屏幕按键
class Screen_Button
{
private:
    struct BUTTON
    {
        vex::color bg_color;         // 背景颜色(background_color)
        vex::color bgp_color;        // 按下时背景颜色(background_pressed_color)
        struct position16t name_pos; // 文字位置(相对按键左上角位置)
        const char *name;            // 要显示的文本
        vex::color name_color;       // 字体颜色
        vex::fontType font;          // 文本字体
        uint32_t *PIC_p;             // 图像指针
        struct Size16t pic_size;     // 图像大小位置(相对按键左上角位置)

        BUTTON() : bg_color(vex::color(0xFFFFFF)), // #FFFFFF #777777
                   bgp_color(vex::color(0x777777)),
                   name_pos{0, 0},
                   name(nullptr),
                   name_color(vex::black),
                   font(vex::fontType::mono15),
                   PIC_p(nullptr),
                   pic_size{8, 8, 24, 24}
        {
        }
    };
    struct Size16t size;  // 按键定位和宽高
    struct BUTTON button; // 按键视觉属性
    Control_key *key_set; // 关联按键事件

public:
    enum Draw_state
    {
        not_draw = 0,
        draw_pressed,
        draw_released
    };
    bool can_use;
    Screen_Button(Control_key *key_set, bool can_use = false);
    void reset_size(int16_t x, int16_t y, int16_t width, int16_t height);
    void reset_button_color(vex::color bg_color, vex::color bgp_color);
    void reset_button_name(const char *name, int16_t x, int16_t y, vex::color name_color = vex::black, vex::fontType font = vex::fontType::mono15);
    void reset_button_pic(uint32_t *PIC_p, int16_t x, int16_t y, int16_t width, int16_t height);
    void draw_button(Draw_state state);  // 绘制按键状态
    void set_key(Control_key *key);      // 设置关联按键
    bool check_press(int x, int y);      // 检测按键区域是否按下
    void trigger_press(bool is_pressed); // 触发关联的逻辑按键
};

// 手柄摇杆类
class Remote_Control
{
private:
    struct t_v_list
    {
        uint32_t time;   // ms级时间
        int32_t value_x; // 按杆x轴值
        int32_t value_y; // 按杆y轴值
    };

public:
    enum STATE
    {
        up,
        down,
        left,
        right,
        middle,
        other,
        only_value
    };
    Control_key *up_state = nullptr;
    Control_key *down_state = nullptr;
    Control_key *left_state = nullptr;
    Control_key *right_state = nullptr;
    Remote_Control() {}
    t_v_list value[8] = {0};
    uint8_t value_p = 0;         // t_v_list[p]返回最新一次记录的摇杆x与y
    STATE state = middle;        // only_value时，不触发按键，仅存储value用于底盘控制
    void (*changed)() = nullptr; // 数据更新回调，refresh 写入新值后若非空则调用
    void refresh(int32_t current_value, int32_t current_value_y);
    void set_state();
};

#endif
