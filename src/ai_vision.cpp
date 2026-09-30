#include "ai_vision.h"

// 颜色参数来自 AI Vision Utility；以后重新标定时替换对应 RGB 和范围。
static vex::aivision::colordesc AI_RED(1, 225, 24, 35, 20.0f, 0.30f);
static vex::aivision::colordesc AI_YELLOW(2, 224, 190, 34, 20.0f, 0.30f);
static vex::aivision::colordesc AI_BLUE(3, 35, 55, 205, 20.0f, 0.30f);
static vex::aivision::colordesc AI_GRAY(4, 92, 94, 96, 12.0f, 0.16f);

vex::aivision ai_sensor(vex::PORT12,
                        AI_RED, AI_YELLOW, AI_BLUE, AI_GRAY,
                        vex::aivision::ALL_TAGS);

AI_COLOR_RESULT ai_colors[AI_COLOR_COUNT] = {};
AI_TAG_RESULT ai_tag = {};
int32_t ai_object_count = 0;
static uint16_t awb_start_time = 0;

void ai_vision_init()
{
    // 当前只用颜色框和 AprilTag，不加载 AI 分类模型。
    ai_sensor.colorDetection(true, false);
    ai_sensor.tagDetection(true);
    ai_sensor.modelDetection(false);

    // 自动白平衡只在开机时执行，避免比赛中颜色参数不断变化。
    ai_sensor.startAwb();
    awb_start_time = vex::timer::system();
}

bool ai_vision_is_ready()
{
    return vex::timer::system() - awb_start_time > 2300;
}

void ai_vision_refresh()
{
    for (uint8_t i = 0; i < AI_COLOR_COUNT; i++)
        ai_colors[i].visible = false;
    ai_tag.visible = false;

    // 一帧同时读取颜色和 Tag，避免对传感器重复拍摄。
    int32_t count = ai_sensor.takeSnapshot(vex::aivision::ALL_OBJECTS,
                                           AI_MAX_OBJECTS);
    if (count < 0)
        count = 0;
    if (count > AI_MAX_OBJECTS)
        count = AI_MAX_OBJECTS;
    ai_object_count = count;

    int32_t largest_color_area[AI_COLOR_COUNT] = {};
    int32_t largest_tag_area = 0;

    for (int32_t i = 0; i < count; i++)
    {
        const vex::aivision::object &object = ai_sensor.objects[i];

        if (object.type == vex::aivision::objectType::colorObject &&
            object.id >= 1 && object.id <= AI_COLOR_COUNT)
        {
            uint8_t color_index = object.id - 1;
            if (object.area > largest_color_area[color_index])
            {
                largest_color_area[color_index] = object.area;
                ai_colors[color_index].visible = true;
                ai_colors[color_index].center_x = object.centerX;
                ai_colors[color_index].center_y = object.centerY;
                ai_colors[color_index].width = object.width;
                ai_colors[color_index].height = object.height;
            }
        }

        if (object.type == vex::aivision::objectType::tagObject &&
            object.area > largest_tag_area)
        {
            largest_tag_area = object.area;
            ai_tag.visible = true;
            ai_tag.id = object.id;
            ai_tag.center_x = object.centerX;
            ai_tag.center_y = object.centerY;
            ai_tag.width = object.width;
            ai_tag.height = object.height;
            ai_tag.angle_deg = object.angle;
        }
    }
}
