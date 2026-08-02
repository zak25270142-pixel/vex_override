#ifndef __TOOL_H__
#define __TOOL_H__

#include "my_main.h"

void list_copy(char* list1, char* list2, uint16_t len);
void list_copy2(char* list1,const char* list2, uint16_t a, uint16_t b, uint16_t len);
uint8_t list_equal(char* list1, char* list2, uint16_t m, uint16_t n, uint16_t l);

void uint_to_str(uint32_t num, char* str);
void int_to_str(int32_t num, char* str);
uint16_t len_str(char* str);

uint64_t ten_xx_num(int8_t num);
void value_to_str(void *ptr, char *str_buffer, uint8_t type);

#endif