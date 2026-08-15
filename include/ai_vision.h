#ifndef AI_VISION_H_
#define AI_VISION_H_

#include "vex.h"
#include <stdint.h>

// AI Vision 画面固定为 320×240；中心坐标约为 (160, 120)。
const uint8_t AI_MAX_OBJECTS = 24;
const uint8_t AI_COLOR_COUNT = 4;

// 一种颜色在本帧中面积最大的检测框。
struct AI_COLOR_RESULT
{
    bool visible;     // true：本帧至少检测到一个颜色框。
    int16_t center_x; // 框中心横坐标，单位：像素，向右增大。
    int16_t center_y; // 框中心纵坐标，单位：像素，向下增大。
    int16_t width;    // 框宽度，单位：像素。
    int16_t height;   // 框高度，单位：像素。
};

// 本帧面积最大的 AprilTag；相同尺寸的 Tag 通常是越近面积越大。
struct AI_TAG_RESULT
{
    bool visible;     // true：本帧至少检测到一个 AprilTag。
    int32_t id;       // AprilTag 编号；visible 为 true 时有效。
    int16_t center_x; // Tag 中心横坐标，单位：像素，向右增大。
    int16_t center_y; // Tag 中心纵坐标，单位：像素，向下增大。
    int16_t width;    // Tag 框宽度，单位：像素。
    int16_t height;   // Tag 框高度，单位：像素。
    float angle_deg;  // Tag 在画面中的旋转角，单位：度。
};

extern vex::aivision ai_sensor;
// 下标 0~3 分别保存红、黄、蓝、灰；不同颜色可以在同一帧同时 visible。
extern AI_COLOR_RESULT ai_colors[AI_COLOR_COUNT];
extern AI_TAG_RESULT ai_tag;
extern int32_t ai_object_count; // 本帧颜色框和 AprilTag 的总数量，仅供调试页显示。

// 开机调用一次：开启颜色、AprilTag，并执行一次自动白平衡。
void ai_vision_init();

// AI Vision 没有白平衡完成标志，启动 2200 ms 后视为就绪。
bool ai_vision_is_ready();

// 周期调用：拍摄一帧，同时更新 ai_colors 和 ai_tag。
void ai_vision_refresh();

#endif
