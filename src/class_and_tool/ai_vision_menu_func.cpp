#include "menu_func.h"

// 布局属于视觉菜单自身，外部只提供数据，不需要知道绘制坐标。
static const Size16t CONTENT_SIZE = {{3, 5}, 420, 216};
static const Size16t BOX_VIEW_SIZE = {{255, 55}, 160, 120};
static const uint16_t REFRESH_MS = 100;

AI_VISION_MENU::AI_VISION_MENU(bool (*is_connected)(),
                               const AI_VISION_MENU_OBJECT *objects,
                               uint8_t object_num,
                               uint8_t tag_index,
                               const int32_t *tag_id,
                               const float *tag_angle_deg,
                               const int32_t &object_count,
                               void (*key_reset)(),
                               vex::color table_color,
                               vex::color bg_color)
    : is_connected(is_connected),
      objects(objects),
      object_num(object_num),
      tag_index(tag_index),
      tag_id(tag_id),
      tag_angle_deg(tag_angle_deg),
      object_count(object_count),
      key_reset(key_reset),
      table_color(table_color),
      bg_color(bg_color)
{
}

// 把 320×240 检测坐标缩小一半；这里只画检测框，不包含相机图像。
void AI_VISION_MENU::draw_box_view()
{
    Brain.Screen.setPenColor(vex::color(0x666666));
    Brain.Screen.drawRectangle(BOX_VIEW_SIZE.start_p.x, BOX_VIEW_SIZE.start_p.y,
                               BOX_VIEW_SIZE.width, BOX_VIEW_SIZE.height,
                               vex::color(0x111111));
    Brain.Screen.drawLine(BOX_VIEW_SIZE.start_p.x + BOX_VIEW_SIZE.width / 2,
                          BOX_VIEW_SIZE.start_p.y,
                          BOX_VIEW_SIZE.start_p.x + BOX_VIEW_SIZE.width / 2,
                          BOX_VIEW_SIZE.start_p.y + BOX_VIEW_SIZE.height);
    Brain.Screen.drawLine(BOX_VIEW_SIZE.start_p.x,
                          BOX_VIEW_SIZE.start_p.y + BOX_VIEW_SIZE.height / 2,
                          BOX_VIEW_SIZE.start_p.x + BOX_VIEW_SIZE.width,
                          BOX_VIEW_SIZE.start_p.y + BOX_VIEW_SIZE.height / 2);

}

// 同一个对象的文字和检测框在这里一起绘制，避免两套显示数据不同步。
void AI_VISION_MENU::draw_object(uint8_t index)
{
    const AI_VISION_MENU_OBJECT &object = objects[index];
    bool visible = *object.visible;

    Brain.Screen.setFillColor(bg_color);
    Brain.Screen.setPenColor(visible
                                 ? vex::color(object.pen_rgb)
                                 : vex::color(0x777777));
    if (visible)
        Brain.Screen.printAt(10, 78 + index * 20, true,
                             "%-6s YES  %3d %3d %3d %3d",
                             object.name, *object.center_x, *object.center_y,
                             *object.width, *object.height);
    else
        Brain.Screen.printAt(10, 78 + index * 20, true,
                             "%-6s NONE                   ", object.name);

    if (!visible)
        return;

    // 绘制时再根据 RGB 构造颜色，避免跨文件全局初始化顺序导致黑色。
    Brain.Screen.setPenColor(vex::color(object.pen_rgb));
    Brain.Screen.setFillColor(vex::transparent);
    Brain.Screen.drawRectangle(BOX_VIEW_SIZE.start_p.x + *object.center_x / 2 - *object.width / 4,
                               BOX_VIEW_SIZE.start_p.y + *object.center_y / 2 - *object.height / 4,
                               *object.width / 2, *object.height / 2);
}

void AI_VISION_MENU::init()
{
    reset_origin();
    Brain.Screen.drawRectangle(0, 0, 480, 272, vex::black);
    if (key_reset != nullptr)
        key_reset();
    last_draw_time = get_time_ms() - REFRESH_MS;

    Brain.Screen.setPenColor(table_color);
    Brain.Screen.drawRectangle(CONTENT_SIZE.start_p.x, CONTENT_SIZE.start_p.y,
                               CONTENT_SIZE.width, CONTENT_SIZE.height, bg_color);

    // 静态文字只在进入页面时绘制一次。
    Brain.Screen.setFont(vex::fontType::mono15);
    Brain.Screen.setPenColor(vex::white);
    Brain.Screen.printAt(10, 18, false, "AI VISION DEBUG");
    Brain.Screen.printAt(10, 58, false, "OBJECT STATE   X   Y   W   H");
    Brain.Screen.printAt(275, 195, false, "BOX VIEW 1:2");
    refresh();
}

void AI_VISION_MENU::refresh()
{
    uint32_t now = get_time_ms();
    if (now - last_draw_time < REFRESH_MS)
        return;
    last_draw_time = now;

    reset_origin();
    Brain.Screen.setFont(vex::fontType::mono15);
    Brain.Screen.setFillColor(bg_color);
    bool connected = is_connected();
    Brain.Screen.setPenColor(connected ? vex::green : vex::red);
    Brain.Screen.printAt(10, 38, true, "PORT 10:%-12s",
                         connected ? "CONNECTED" : "DISCONNECTED");

    Brain.Screen.setPenColor(vex::white);
    Brain.Screen.printAt(200, 38, true, "OBJ:%-3ld", object_count);
    Brain.Screen.printAt(335, 38, true, "AI:OFF");

    // 先清除右侧小视图，再逐项同时更新文字与框。
    draw_box_view();
    for (uint8_t i = 0; i < object_num; i++)
        draw_object(i);

    const AI_VISION_MENU_OBJECT &tag = objects[tag_index];
    Brain.Screen.setPenColor(vex::white);
    if (*tag.visible)
    {
        Brain.Screen.printAt(10, 193, true, "TAG ID:%-3ld ANG:%6.1f        ",
                             *tag_id, *tag_angle_deg);
    }
    else
    {
        Brain.Screen.printAt(10, 193, true, "                              ");
    }

}
