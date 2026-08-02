#include "tool.h"

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
uint8_t list_equal(char *list1, char *list2, uint16_t m, uint16_t n, uint16_t l)
{ // list1 开始位置 list2 开始位置 判断的长度
    for (uint16_t i = 0; i < l; i++)
    {
        if (list1[m + i] != list2[n + i])
            return 0;
    }
    return 1;
}
uint16_t len_str(char *str)
{
    uint16_t i;
    for (i = 0; str[i] != '\0'; i++)
        ;
    return i;
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
        snprintf(str_buffer, 25, "%.8f", *(float *)ptr);
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