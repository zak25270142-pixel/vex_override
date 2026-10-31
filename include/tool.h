#ifndef __TOOL_H__
#define __TOOL_H__

#include "my_main.h"

void list_copy(char *list1, char *list2, uint16_t len);
void list_copy2(char *list1, const char *list2, uint16_t a, uint16_t b, uint16_t len);

void uint_to_str(uint32_t num, char *str);
void int_to_str(int32_t num, char *str);

uint64_t ten_xx_num(int8_t num);
void value_to_str(void *ptr, char *str_buffer, uint8_t type);

// 非对称压摆（速率限制器）：以 prev 为锚，按 rise/fall 速率把 req 收进本拍可达区间。
// rise/fall 是"离 0 / 朝 0"速率；过零那一拍先 fall 撤到 0，剩的时间再 rise 反向张开。
// 纯函数、无状态，任何需要按时间速率爬升的限幅都能复用（当前底盘状态机在用）。
float slew_step(float req, float prev, float rise, float fall, float dt);

// 升序 x 的分段线性插值：target 落在 [x0,xn] 内按距离加权，越界取端点 y。
// kf 前馈表与升降杆重力偏置表共用；表为 nullptr/空时返 0（调用方保证不传非法表）。
float linear_at(const Vec2f *node, uint8_t n, float target);

#endif
