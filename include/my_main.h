#ifndef __MY_MAIN_H__
#define __MY_MAIN_H__

#include "vex.h"
#include "stdint.h"

#define elif else if

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
