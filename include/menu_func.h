#ifndef __MENU_FUNC_H__
#define __MENU_FUNC_H__

#include "my_main.h"

class MENU
{
private:
    struct Size16t size;
    uint8_t y_gap;
    uint8_t blank_width;
    uint8_t line_num;
    int8_t exn = 0;
    const struct MENU_ITEM *menu_item;
    uint8_t menu_len;
    const struct MENU_ITEM *menu_buffer;
    uint8_t menu_len_buffer;

    void print_value(int32_t y, void *ptr, VALUE_TYPE type, const char *unit, bool bOpaque = false);
    void tc_value_add();
    void tc_value_reduce();
    void refresh_highlight_line_left();
    void refresh_highlight_line_right();
    void format_exn();
    void refresh_exn();

public:
    vex::color name_color = vex::color(0xFFFFFF);
    vex::color table_color = vex::color(114514);
    vex::color bg_color = vex::color(1909810);
    vex::color highlightbg_color = vex::color(0xFFFFFF);
    vex::color highlightnm_color = vex::color(0x000000);
    uint8_t highlight_line = 3;
    uint8_t x_gap = 180;
    uint8_t now_choose = 0;
    vex::fontType font = vex::fontType::mono20;
    bool inner_menu = false;
    bool is_monitor_menu = false;

    void (*normal_key_reset)(); // 重置为普通模式按键配置
    void (*tc_key_reset)();     // 重置为调参模式按键配置

    MENU(const struct MENU_ITEM *items,
         uint8_t length,
         void (*normal_reset)(),
         void (*tuning_reset)(),
         const struct MENU_ITEM *monitor_items = nullptr,
         uint8_t monitor_len = 0,
         int16_t x = 3, int16_t y = 5,
         int16_t width = 420, int16_t height = 216,
         int8_t blank = 1, uint8_t y_gap = 19);

    void refresh();
    void refresh_value();
    void up();
    void down();
    void add();
    void reduce();
    void shift();
    void enter();
    void back();
};

// 一个视觉菜单对象同时包含文字样式和检测框数据，两者由同一次循环绘制。
struct AI_VISION_MENU_OBJECT
{
    const char *name;
    uint32_t pen_rgb;
    const bool *visible;
    const int16_t *center_x;
    const int16_t *center_y;
    const int16_t *width;
    const int16_t *height;
};

class AI_VISION_MENU
{
private:
    bool (*is_connected)();
    const AI_VISION_MENU_OBJECT *objects;
    uint8_t object_num;
    uint8_t tag_index;
    const int32_t *tag_id;
    const float *tag_angle_deg;
    const int32_t &object_count;
    void (*key_reset)();
    vex::color table_color;
    vex::color bg_color;
    uint32_t last_draw_time = 0;

    void draw_box_view();
    void draw_object(uint8_t index);

public:
    // 检测数据由外部传入；类只负责显示，不读取或修改页面状态。
    AI_VISION_MENU(bool (*is_connected)(),
                   const AI_VISION_MENU_OBJECT *objects,
                   uint8_t object_num,
                   uint8_t tag_index,
                   const int32_t *tag_id,
                   const float *tag_angle_deg,
                   const int32_t &object_count,
                   void (*key_reset)(),
                   vex::color table_color,
                   vex::color bg_color);

    void init();
    void refresh();
};

#endif
