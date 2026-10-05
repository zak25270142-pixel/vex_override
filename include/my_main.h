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

// 监控项 tag：单字节位域（复用原 8bit，不再用 0/1/2/3/4/5 枚举占满语义）
//   bit7 (0x80) MENU_TAG_SUB     是否订阅：1=参与推送，0=不推送（退订）
//   bit6 (0x40) MENU_TAG_FAST    速率：1=高速(每10ms)，0=低速(8相轮转约80ms)
//   bit5 (0x20) MENU_TAG_GETTER  取数方式：1=data_ptr 为 float(*)() 函数指针；
//                                 0=data_ptr 为普通变量地址（直接解引用）
//   bit4~3      预留，写 0；订阅合并时下位机保留，上位机不要当速率位用
//   bit2~0      语义种类 MENU_TAG_KIND_*：场地图等特殊量
//                 0 none，1 pos_x，2 pos_y，3 yaw，4~7 预留
static constexpr uint8_t MENU_TAG_SUB = 0x80u;
static constexpr uint8_t MENU_TAG_FAST = 0x40u;
static constexpr uint8_t MENU_TAG_GETTER = 0x20u;
static constexpr uint8_t MENU_TAG_KIND_MASK = 0x07u;
static constexpr uint8_t MENU_TAG_KIND_NONE = 0u;
static constexpr uint8_t MENU_TAG_KIND_POS_X = 1u;
static constexpr uint8_t MENU_TAG_KIND_POS_Y = 2u;
static constexpr uint8_t MENU_TAG_KIND_YAW = 3u;

// 表项常用默认 tag（已含 SUB；getter 构造函数会再或上 MENU_TAG_GETTER）
static constexpr uint8_t monitor_tag_none = 0;                                       // 不订阅
static constexpr uint8_t monitor_tag_slow = MENU_TAG_SUB;                            // 订阅 + 低速 + 内存指针
static constexpr uint8_t monitor_tag_fast = (uint8_t)(MENU_TAG_SUB | MENU_TAG_FAST); // 订阅 + 高速 + 内存指针
static constexpr uint8_t monitor_tag_pos_x = (uint8_t)(MENU_TAG_SUB | MENU_TAG_FAST | MENU_TAG_KIND_POS_X);
static constexpr uint8_t monitor_tag_pos_y = (uint8_t)(MENU_TAG_SUB | MENU_TAG_FAST | MENU_TAG_KIND_POS_Y);
static constexpr uint8_t monitor_tag_yaw = (uint8_t)(MENU_TAG_SUB | MENU_TAG_FAST | MENU_TAG_KIND_YAW);

// 无参读函数：返回 float；监控项的 data_type 必须为 type_float 才能用此构造函数。
// （当前协议 GETTER 项固定发 4B float；若未来需其他类型，再按 VALUE_TYPE 追加同风格 typedef。）
typedef float (*MenuFloatGetter)();

// 菜单与通信共用的条目描述：名字、数据类型、数据指针、中文名、单位、监控标签
// data_ptr 双重含义（由 tag 的 MENU_TAG_GETTER 区分，不另增指针成员）：
//   - 未置 GETTER：指向可读写变量（调参/普通监控）
//   - 已置 GETTER：值为 MenuFloatGetter，只读；禁止 write/旋钮改值
struct MENU_ITEM
{
    const char *item_name;
    VALUE_TYPE data_type;
    void *data_ptr;
    const char *Chinese_name;
    const char *unit;
    uint8_t tag; // 监控表用位域；调参表保持 0 即可
    // 内存项（可读写）：ptr 为变量地址
    MENU_ITEM(const char *name, VALUE_TYPE type, void *ptr, const char *c,
              const char *u = nullptr, uint8_t t = monitor_tag_none)
        : item_name(name), data_type(type), data_ptr(ptr), Chinese_name(c), unit(u), tag(t)
    {
    }
    // getter 只读项：固定 type_float；get 无参，返回 float；tag 自动加上 MENU_TAG_GETTER
    // （构造函数不接收 VALUE_TYPE，因当前协议 GETTER 项只支持 float，避免不对称）
    MENU_ITEM(const char *name, MenuFloatGetter get, const char *c,
              const char *u, uint8_t t)
        : item_name(name), data_type(type_float), data_ptr(reinterpret_cast<void *>(get)), Chinese_name(c), unit(u), tag(static_cast<uint8_t>(t | MENU_TAG_GETTER))
    {
    }
    bool is_getter() const { return (tag & MENU_TAG_GETTER) != 0; }
    bool is_subscribed() const { return (tag & MENU_TAG_SUB) != 0; }
    bool is_fast() const { return (tag & MENU_TAG_FAST) != 0; }
    uint8_t kind() const { return static_cast<uint8_t>(tag & MENU_TAG_KIND_MASK); }
};

void my_Init();
void my_while();

extern vex::brain Brain;

// menu_func.cpp
void reset_origin(int16_t x = 0, int16_t y = 0, uint16_t width = 480, uint16_t height = 272);

// timer.cpp
uint32_t get_time_ms();
uint32_t get_time_us();

#endif
