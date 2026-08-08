#include "my_main.h"
#include "timer.h"
#include "LCD_menu.h"
#include "key_set.h"

vex::brain Brain;

vex::motor MotorA(vex::PORT1, vex::ratio6_1, true);

CycleTimer main_timer(10); // 暂定10ms  生成主循环周期 类

void my_Init()
{ // main.cpp while 前
    Brain.Screen.setPenWidth(1);
    key_init1();
    pre_menu_init();

    MotorA.setVelocity(10, vex::velocityUnits::rpm);
}

void my_while()
{                       // main.cpp while 区
    main_timer.cycle(); // 确立循环频率
    refresh_menu();

    if (screen_button_need_draw != Screen_Button::not_draw)
    {
        draw_button();
    }
}

void control_chasis()
{
}

void car_mode_init()
{
    left_axis.left_state = nullptr;
    left_axis.right_state = nullptr;
    left_axis.up_state = nullptr;
    left_axis.down_state = nullptr;

    right_axis.up_state = nullptr;
    right_axis.down_state = nullptr;
    right_axis.left_state = nullptr;
    right_axis.right_state = nullptr;
}

// 向量旋转角度
vect_f vect_swin_ang(vect_f vect, float ang)
{
    float cos_ang = cosf(ang);
    float sin_ang = sinf(ang);
    return {vect.x * cos_ang - vect.y * sin_ang, vect.x * sin_ang + vect.y * cos_ang};
}
// 向量旋转cos sin
vect_f vect_swin_cs(vect_f vect, float cos, float sin)
{
    return {vect.x * cos - vect.y * sin, vect.x * sin + vect.y * cos};
}
// 向量旋转 vect_f(cos,sin)
vect_f vect_swin_vect(vect_f vect, vect_f dir)
{
    return {vect.x * dir.x - vect.y * dir.y, vect.x * dir.y + vect.y * dir.x};
}
// 向量数乘
vect_f vect_scalar(vect_f vect, float num)
{
    return {vect.x * num, vect.y * num};
}
vect_f vect_add(vect_f vect_a, vect_f vect_b)
{
    return {vect_a.x + vect_b.x, vect_a.y + vect_b.y};
}
void car_mode()
{
    static vect_f car_mid = {200, 160}; // x坐标
    static float car_ang = 0;           // 与x轴夹角

    const vect_f vect_wd = {8, 5}; // wd方向的本地向量(or {half_len,half_wid})
    const float car_r = 3;         // 圆盖
    const float car_p = 6;         // 炮管长度，指示方向
    const int clear_r = 10;        // 清除半径
    const float to_w = 1 / 12.7;   // 映射角速度参数
    const float to_v = 1 / 127.0;  // 映射速度参数

    Brain.Screen.setPenColor(vex::color::black); // 清除
    Brain.Screen.setFillColor(vex::color::black);
    Brain.Screen.drawCircle(car_mid.x, car_mid.y, clear_r);
    static uint32_t last_time = get_time_ms();
    uint32_t now = get_time_ms();
    uint32_t time_gap = now - last_time;
    last_time = now;

    float car_w = 0; // 角速度
    car_w = right_axis.value[right_axis.value_p].value_x * to_w;
    car_ang += car_w * time_gap * 0.001; // ang=w*t

    vect_f v = {left_axis.value[left_axis.value_p].value_x * to_v, left_axis.value[left_axis.value_p].value_y * to_v};
    vect_f ang = {cosf(car_ang), sinf(car_ang)};
    car_mid = vect_add(vect_scalar(vect_swin_vect(v, ang), time_gap * 0.001), car_mid);

    if (car_mid.x < 10) // 死区
        car_mid.x = 10;
    if (car_mid.x > 470)
        car_mid.x = 470;
    if (car_mid.y < 90)
        car_mid.y = 90;
    if (car_mid.y > 265)
        car_mid.y = 265;

    vect_f new_wd = vect_swin_vect(vect_wd, ang); // 定点
    vect_f car_aw = {car_mid.x - new_wd.x, car_mid.y + new_wd.y};
    vect_f car_as = {car_mid.x - new_wd.x, car_mid.y - new_wd.y};
    vect_f car_sd = {car_mid.x + new_wd.x, car_mid.y - new_wd.y};
    vect_f car_wd = {car_mid.x + new_wd.x, car_mid.y + new_wd.y};
    vect_f p_end = vect_add(car_mid, vect_swin_vect({0, 6}, ang));

    // 设置车身颜色
    Brain.Screen.setPenColor(vex::color::white);
    Brain.Screen.setFillColor(vex::color::transparent);

    // 绘制矩形车身（连接四个顶点）
    Brain.Screen.drawLine(car_aw.x, car_aw.y, car_as.x, car_as.y);
    Brain.Screen.drawLine(car_as.x, car_as.y, car_sd.x, car_sd.y);
    Brain.Screen.drawLine(car_sd.x, car_sd.y, car_wd.x, car_wd.y);
    Brain.Screen.drawLine(car_wd.x, car_wd.y, car_aw.x, car_aw.y);

    // 绘制中心圆盖
    Brain.Screen.setFillColor(vex::color::blue);
    Brain.Screen.drawCircle(car_mid.x, car_mid.y, car_r);

    // 绘制方向指示炮管
    Brain.Screen.setPenColor(vex::color::red);
    Brain.Screen.setPenWidth(2);
    Brain.Screen.drawLine(car_mid.x, car_mid.y, p_end.x, p_end.y);
    Brain.Screen.setPenWidth(1);
}
