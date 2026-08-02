#ifndef __MENU_FUNC_H__
#define __MENU_FUNC_H__

#include "my_main.h"

struct VALUE_HISTORY
{                           // 存储的历史记录
    uint16_t history_depth; // 历史数据存储深度
    void *history_buffer;   // 历史数据缓冲区指针
    uint16_t write_index;   // 循环缓冲区的写入位置
};

struct SHOW_VALUE_HISTORY
{ // 绘图(历史记录)参数
    struct VALUE_HISTORY value_history;
    uint8_t x_gap; //
};

struct MENU_ITEM
{
    const char *item_name;
    VALUE_TYPE data_type;
    void *data_ptr;
    const char *unit;
    struct SHOW_VALUE_HISTORY *history_p;
    MENU_ITEM(const char *name, VALUE_TYPE type, void *ptr, const char *u = nullptr, SHOW_VALUE_HISTORY *hist = nullptr)
        : item_name(name), data_type(type), data_ptr(ptr), unit(u), history_p(hist) {}
};

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

#endif