#include "tool.h"
#include <cmath>

void list_copy(char *list1, const char *list2, uint16_t len)
{
    for (int i = 0; i < len; i++)
    {
        list1[i] = list2[i];
    }
}
void list_copy2(char *list1, const char *list2, uint16_t a, uint16_t b, uint16_t len)
{
    for (int i = 0; i < len; i++)
    {
        list1[i + a] = list2[i + b];
    }
}
void uint_to_str(uint32_t num, char *str)
{
    int8_t i = 0;
    if (num == 0)
    {
        i++;
    }
    else
    {
        for (uint32_t nnum = num; nnum > 0; i++)
        {
            nnum /= 10;
        }
    }
    str[i] = '\0';
    i--;
    for (; i >= 0; i--)
    {
        str[i] = num % 10 + '0';
        num /= 10;
    }
}
void int_to_str(int32_t num, char *str)
{
    if (num < 0)
    {
        str[0] = '-';
        uint_to_str(-num, str + 1);
    }
    else
    {
        uint_to_str(num, str);
    }
}
void value_to_str(void *ptr, char *str_buffer, uint8_t type)
{
    switch (type)
    {
    case type_uint8_t:
        uint_to_str(*(uint8_t *)ptr, str_buffer);
        break;
    case type_uint16_t:
        uint_to_str(*(uint16_t *)ptr, str_buffer);
        break;
    case type_uint32_t:
        uint_to_str(*(uint32_t *)ptr, str_buffer);
        break;
    case type_uint64_t:
        snprintf(str_buffer, 25, "%llu", *(uint64_t *)ptr);
        break;
    case type_int8_t:
        int_to_str(*(int8_t *)ptr, str_buffer);
        break;
    case type_int16_t:
        int_to_str(*(int16_t *)ptr, str_buffer);
        break;
    case type_int32_t:
        int_to_str(*(int32_t *)ptr, str_buffer);
        break;
    case type_int64_t:
        snprintf(str_buffer, 25, "%lld", *(int64_t *)ptr);
        break;
    case type_float:
        snprintf(str_buffer, 25, "%.4f", *(float *)ptr);
        break;
    case type_double:
        snprintf(str_buffer, 25, "%.8f", *(double *)ptr);
        break;
    default:
        list_copy(str_buffer, "error", 6);
        break;
    }
}
uint64_t ten_xx_num(int8_t num)
{
    if (num < 0)
    {
        num = -num;
    }
    uint64_t ans = 1;
    for (; num > 0; num--)
    {
        ans *= 10;
    }
    return ans;
}

// 压摆窗口：把 req 收到"以 prev 为锚、按速率张开"的区间里，返回收好之后的值。
// rise / fall 是"离 0 / 朝 0"的速率，不是"正 / 负"方向的速率——所以过零那一拍要把
// 这一拍的额度拆成两段：先用 fall 撤到 0，剩下的时间用 rise 往反向张开。
// 不换向时窗口对称，两条边各张 step；step 取哪条速率取决于这拍幅值在增大还是减小。
float slew_step(float req, float prev, float rise, float fall, float dt)
{
    if (req * prev < 0.0f) // 换向
    {
        const float t_zero = fabsf(prev) / fall; // 撤到 0 要花的时间
        if (t_zero >= dt)                        // 本拍撤不完，窗口缩成一个点
            return prev - copysignf(fall * dt, prev);
        return copysignf(fminf(fabsf(req), rise * (dt - t_zero)), req);
    }
    const float step = (fabsf(req) > fabsf(prev) ? rise : fall) * dt;
    return fminf(fmaxf(req, prev - step), prev + step);
}

// 升序 x 的分段线性插值：target 落在 [x0,xn] 内按距离加权，越界取端点 y。
// kf 前馈表与升降杆重力偏置表共用。表为 nullptr/空时返 0。
float linear_at(const Vec2f *node, uint8_t n, float target)
{
    if (node == nullptr || n == 0)
        return 0.0f;
    if (target <= node[0].x)
        return node[0].y;
    for (uint8_t i = 1; i < n; i++)
    {
        if (target <= node[i].x)
        {
            const float t = (target - node[i - 1].x) / (node[i].x - node[i - 1].x);
            return node[i - 1].y + (node[i].y - node[i - 1].y) * t;
        }
    }
    return node[n - 1].y;
}
