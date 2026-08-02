#include "menu_func.h"
#include "tool.h"
MENU::MENU(const struct MENU_ITEM *items,
           uint8_t length,
           void (*normal_reset)(),
           void (*tuning_reset)(),
           const struct MENU_ITEM *monitor_items,
           uint8_t monitor_len,
           int16_t x, int16_t y,
           int16_t width, int16_t height,
           int8_t blank, uint8_t y_gap)
    : size({{x, y}, width, height}),
      y_gap(y_gap),
      blank_width(blank),
      menu_item(items),
      menu_len(length),
      menu_buffer(monitor_items),
      menu_len_buffer(monitor_len),
      normal_key_reset(normal_reset),
      tc_key_reset(tuning_reset)
{
    line_num = (size.height - 1 - 2 * blank_width) / y_gap + 1;
}
// 私有方法实现

// 打印数值+单位或个性化打印
void MENU::print_value(int32_t y, void *ptr, VALUE_TYPE type, const char *unit, bool bOpaque)
{
    char str_buffer[28] = {32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,32,'\0'};
    switch (type)
    {
    case type_str:
        Brain.Screen.printAt(0, y, bOpaque, (const char *)ptr);
        break;
    case type_color:
        Brain.Screen.setPenColor(*(int *)ptr);
        Brain.Screen.drawRectangle(4, y + 1, y_gap, y_gap - 2, *(int *)ptr);
        Brain.Screen.setPenColor(name_color);
        break;
    case type_on_off:
        if (*(bool *)ptr)
            Brain.Screen.printAt(0, y, bOpaque, "ON ");
        else
            Brain.Screen.printAt(0, y, bOpaque, "OFF");
        break;
    case type_bool:
        if (*(bool *)ptr)
            Brain.Screen.printAt(0, y, bOpaque, "true ");
        else
            Brain.Screen.printAt(0, y, bOpaque, "false");
        break;
    case type_other:
        Brain.Screen.printAt(0, y, bOpaque, "error");
        break;
    default:
        value_to_str(ptr, str_buffer, type);
        uint8_t len=strlen(str_buffer);
        if (unit != nullptr)
        {
            uint8_t ulen=strlen(unit);
            list_copy2(str_buffer, unit, len, 0,ulen);
            len+=ulen;
        }
        str_buffer[27]='\0';
        Brain.Screen.printAt(0, y, bOpaque, (const char *)str_buffer);
        break;
    }
}

// 调参倍率++
void MENU::tc_value_add()
{
    const struct MENU_ITEM *item = &menu_item[now_choose];
    switch (item->data_type)
    {
    case type_int8_t:
        *(int8_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_int16_t:
        *(int16_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_int32_t:
        *(int32_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_int64_t:
        *(int64_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_uint8_t:
        *(uint8_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_uint16_t:
        *(uint16_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_uint32_t:
        *(uint32_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_uint64_t:
        *(uint64_t *)item->data_ptr += ten_xx_num(exn);
        break;
    case type_float:
        *(float *)item->data_ptr += (exn >= 0) ? (float)ten_xx_num(exn) : 1.0 / ten_xx_num(-exn);
        break;
    case type_double:
        *(float *)item->data_ptr += (exn >= 0) ? (float)ten_xx_num(exn) : 1.0 / ten_xx_num(-exn);
        break;
    case type_on_off:
    case type_bool:
        *(bool *)item->data_ptr = !*(bool *)item->data_ptr;
        break;
    default:
        break;
    }
}

// 调参倍率--
void MENU::tc_value_reduce()
{
    const struct MENU_ITEM *item = &menu_item[now_choose];
    switch (item->data_type)
    {
    case type_int8_t:
        *(int8_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_int16_t:
        *(int16_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_int32_t:
        *(int32_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_int64_t:
        *(int64_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_uint8_t:
        *(uint8_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_uint16_t:
        *(uint16_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_uint32_t:
        *(uint32_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_uint64_t:
        *(uint64_t *)item->data_ptr -= ten_xx_num(exn);
        break;
    case type_float:
        *(float *)item->data_ptr -= (exn >= 0) ? (float)ten_xx_num(exn) : 1.0 / ten_xx_num(-exn);
        break;
    case type_double:
        *(float *)item->data_ptr -= (exn >= 0) ? (float)ten_xx_num(exn) : 1.0 / ten_xx_num(-exn);
        break;
    case type_on_off:
    case type_bool:
        *(bool *)item->data_ptr = !*(bool *)item->data_ptr;
        break;
    default:
        break;
    }
}

// 规范倍率exn
void MENU::format_exn()
{
    switch (menu_item[now_choose].data_type)
    {
    case type_uint8_t:
    case type_int8_t:
        exn += 3;
        exn %= 3;
        break;
    case type_uint16_t:
    case type_int16_t:
        exn += 5;
        exn %= 5;
        break;
    case type_int32_t:
    case type_uint32_t:
        exn += 10;
        exn %= 10;
        break;
    case type_int64_t:
    case type_uint64_t:
        exn += 20;
        exn %= 20;
        break;
    case type_float:
    case type_double:
        exn %= 20;
        break;
    case type_on_off:
    case type_bool:
    default:
        return;
    }
}

// 刷新左侧高亮条
void MENU::refresh_highlight_line_left()
{
    reset_origin(size.start_p.x + 1 + blank_width, size.start_p.y + 3 + blank_width + (highlight_line - 1) * y_gap,
                 x_gap - blank_width - 2, y_gap);
    Brain.Screen.drawRectangle(-1, -1, x_gap - blank_width, y_gap + 2, highlightbg_color);
    Brain.Screen.setFont(font);
    Brain.Screen.setPenColor(highlightnm_color);
    Brain.Screen.printAt(0, y_gap - 3, false, menu_item[now_choose].item_name);
    reset_origin();
}

// 刷新右侧高亮条
void MENU::refresh_highlight_line_right()
{
    reset_origin(size.start_p.x + x_gap + 1 + blank_width, size.start_p.y + 3 + blank_width + (highlight_line - 1) * y_gap,
                 size.width - blank_width - 3 - x_gap, y_gap);
    Brain.Screen.drawRectangle(-1, -1, size.width - blank_width + 1, y_gap + 2, highlightbg_color);
    Brain.Screen.setFont(font);
    Brain.Screen.setPenColor(highlightnm_color);
    print_value(y_gap - 3, menu_item[now_choose].data_ptr, menu_item[now_choose].data_type, menu_item[now_choose].unit);
    reset_origin();
}

// 刷新倍率exn
void MENU::refresh_exn()
{
    format_exn();
    reset_origin(size.start_p.x + x_gap + blank_width, size.start_p.y + 3 + blank_width + highlight_line * y_gap, 60, y_gap);
    Brain.Screen.setPenColor(highlightnm_color);
    Brain.Screen.drawRectangle(0, 0, 60, y_gap, highlightbg_color);
    Brain.Screen.setFont(font);
    Brain.Screen.printAt(0, y_gap - 3, false, "x10^%d", exn);
    reset_origin();
}

// PUBLIC

// 菜单刷新
void MENU::refresh()
{
    reset_origin(size.start_p.x, size.start_p.y, size.width, size.height);
    Brain.Screen.setPenColor(table_color);
    Brain.Screen.drawRectangle(0, 0, size.width, size.height, bg_color);
    Brain.Screen.drawLine(x_gap, 0, x_gap, 272);
    reset_origin(size.start_p.x + 1 + blank_width, size.start_p.y + 1 + blank_width, x_gap - blank_width - 3, size.height - 1 - blank_width);
    Brain.Screen.setPenColor(name_color);
    Brain.Screen.setFont(font);
    for (int8_t i = 1; i <= line_num && i <= menu_len; i++)
    {
        uint8_t index = (now_choose + i + menu_len - highlight_line) % menu_len;
        if (i == highlight_line)
            continue;
        Brain.Screen.printAt(0, i * y_gap, false, menu_item[index].item_name);
    }
    refresh_value();
    refresh_highlight_line_left();
}

// 值刷新
void MENU::refresh_value()
{
    reset_origin(size.start_p.x + x_gap + 1 + blank_width, size.start_p.y + 1 + blank_width,
                 size.width - blank_width - 3 - x_gap, size.height - 1 - blank_width);
    Brain.Screen.setPenColor(name_color);
    Brain.Screen.setFont(font);
    Brain.Screen.setFillColor(bg_color);
    for (int8_t i = 1; i <= line_num && i <= menu_len; i++)
    {
        uint8_t index = (now_choose + i + menu_len - highlight_line) % menu_len;
        print_value(i * y_gap, menu_item[index].data_ptr, menu_item[index].data_type, menu_item[index].unit, true);
    }
    reset_origin();
}

void MENU::up()
{
    if (!inner_menu)
    {
        now_choose = (now_choose + menu_len - 1) % menu_len;
        refresh();
        refresh_highlight_line_left();
    }
    else if (inner_menu && !is_monitor_menu)
    {
        exn++;
        refresh_exn();
    }
}

void MENU::down()
{
    if (!inner_menu)
    {
        now_choose++;
        now_choose %= menu_len;
        refresh();
        refresh_highlight_line_left();
    }
    else if (inner_menu && !is_monitor_menu)
    {
        exn--;
        refresh_exn();
    }
}

void MENU::add()
{
    tc_value_add();
    refresh_highlight_line_right();
}

void MENU::reduce()
{
    tc_value_reduce();
    refresh_highlight_line_right();
}

void MENU::shift()
{
    is_monitor_menu = !is_monitor_menu;
    const struct MENU_ITEM *buffer = menu_item;
    uint8_t buffer_len = menu_len;
    menu_item = menu_buffer;
    menu_len = menu_len_buffer;
    menu_buffer = buffer;
    menu_len_buffer = buffer_len;
    now_choose = 0;
    refresh();
}

void MENU::enter()
{
    inner_menu = true;
    if (is_monitor_menu)
    {
        // 监听菜单相关处理
    }
    else
    {
        if (tc_key_reset != nullptr)
            tc_key_reset();
        exn = 0;
        refresh_exn();
        refresh_highlight_line_right();
    }
}

void MENU::back()
{
    if (inner_menu)
    {
        if (normal_key_reset != nullptr)
            normal_key_reset();
        refresh();
        inner_menu = false;
    }
}

void reset_origin(int16_t x, int16_t y, uint16_t width, uint16_t height)
{
    Brain.Screen.setOrigin(x, y);
    Brain.Screen.setClipRegion(0, 0, width, height);
}