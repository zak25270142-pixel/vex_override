#ifndef __MY_MAIN_H__
#define __MY_MAIN_H__

#include "vex.h"
#include "stdint.h"

#define elif else if

// 几何常量与角度换算
constexpr float math_pi = 3.1415927f;
constexpr float deg_to_rad = math_pi / 180.0f;
constexpr float rad_to_deg = 180.0f / math_pi;

struct position16t
{
    int16_t x;
    int16_t y;
};
struct vect_f
{
    float x;
    float y;
};
struct Size16t
{
    position16t start_p; // 标记顶点
    int16_t width;       // 标记宽
    int16_t height;      // 高
};
typedef enum
{
    type_uint8_t = 0,
    type_uint16_t,
    type_uint32_t,
    type_uint64_t,
    type_int8_t,
    type_int16_t,
    type_int32_t,
    type_int64_t,
    type_float,
    type_double = 9,
    type_str,
    type_bool,
    type_on_off,
    type_color,
    type_other,
} VALUE_TYPE;

// 监控项标签：同时承担两个职责——
//   1. 连接后的默认订阅档位（上位机请求监控目录后按此推送，也可再用命令改档）
//   2. 语义标记：x/y/yaw供上位机场地图页自动识别，不用按名字猜
// 档位规则：0不订阅；1低速(8相轮转约80ms)；>=2高速(每10ms)，
// 所以语义量x/y/yaw天然按高速推送。
enum MONITOR_TAG : uint8_t
{
    monitor_tag_none = 0,
    monitor_tag_slow = 1,
    monitor_tag_fast = 2,
    monitor_tag_pos_x = 3,
    monitor_tag_pos_y = 4,
    monitor_tag_yaw = 5,
};

// 菜单与通信共用的条目描述：名字、数据类型、数据指针、中文名、单位、监控标签
struct MENU_ITEM
{
    const char *item_name;
    VALUE_TYPE data_type;
    void *data_ptr;
    const char *Chinese_name;
    const char *unit;
    MONITOR_TAG tag; // 仅监控表使用；调参表默认none即可
    MENU_ITEM(const char *name, VALUE_TYPE type, void *ptr, const char *c,
              const char *u = nullptr, MONITOR_TAG t = monitor_tag_none)
        : item_name(name), data_type(type), data_ptr(ptr), Chinese_name(c), unit(u), tag(t) {}
};

// 所有周期任务共用的生命周期；每个模块分别保存自己的状态变量。
typedef enum
{
    task_start,
    task_run,
    task_finish,
} TASK_STATE;

void my_Init();
void my_while();

extern vex::brain Brain;

// menu_func.cpp
void reset_origin(int16_t x = 0, int16_t y = 0, uint16_t width = 480, uint16_t height = 272);

// timer.cpp
uint32_t get_time_ms();

#endif
