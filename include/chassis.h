#ifndef CHASSIS_H_
#define CHASSIS_H_

#include "my_main.h"

class Chassis
{
private:
    // 硬件由具体车辆创建，Chassis 只保存实际使用的电机组和惯性传感器。
    vex::motor_group &left_motors;
    vex::motor_group &right_motors;
    vex::rotation &forward_tracking_sensor;
    vex::rotation &left_tracking_sensor;
    vex::inertial &inertial_sensor;

    // 是否运动
    bool left_is_moving = false;
    bool right_is_moving = false;
    // 上一次 update() 的系统时间戳，单位 ms。
    uint32_t previous_time = 0;

public:
    // 底盘物理参数。轮距预留。
    float wheel_r = 0.041275f;           // 驱动车轮半径，单位 m。
    float track_width = 0.3000f;         // 左右驱动轮接地点之间的距离，单位 m。
    float motor_wheel_ratio = 1.5000f;   // 电机转数 / 车轮转数，用于把电机编码器换算为车轮路程。

    // 定位轮到车体旋转中心的有符号距离，单位 m。
    float forward_tracking_offset = 0.0f; // 前向定位轮相对旋转中心的右向偏移，单位 m。
    float side_tracking_offset = 0.0f;    // 侧向定位轮相对旋转中心的前向偏移，单位 m。

    // 克服摩擦所需的最小输出百分比(死区)。
    float left_static_deadzone = 10.0f;
    float left_dynamic_deadzone = 5.0f;
    float right_static_deadzone = 10.0f;
    float right_dynamic_deadzone = 5.0f;
    float output_deadzone = 0.1f; // PID 有效输出绝对值不超过该值时视为 0，单位百分比。

    // 编码器运动状态确认阈值，单位 rpm。
    float moving_confirm_speed = 0.15f;
    float stopped_confirm_speed = 0.03f;

    // reset() 保存的本次动作起点，只在调用 reset() 时改变。
    float left_distance_start = 0.0f; // 累积路程
    float right_distance_start = 0.0f;
    float heading_start = 0.0f; // 累计航向角，单位 deg。
    float x_start = 0.0f;
    float y_start = 0.0f;

    // 最近一次 update() 保存的当前状态，不在同一周期内重复读取硬件。
    float left_distance = 0.0f; // 累积路程，单位 m。
    float right_distance = 0.0f;
    float left_speed = 0.0f; // 电机组编码器测得的转速，单位 rpm。
    float right_speed = 0.0f;
    float linear_speed = 0.0f;  // 车体中心沿自身前方的线速度，单位 m/s。
    float heading = 0.0f;       // IMU 测得的累计航向角，向右转为正，单位 deg。
    float angular_speed = 0.0f; // IMU 测得的车体转动角速度，单位 deg/s。

    // 定位轮累计滚动路程，单位 m。
    float forward_tracking_distance = 0.0f; // 前向定位轮的累计滚动路程，单位 m。
    float side_tracking_distance = 0.0f;    // 侧向定位轮的累计滚动路程，单位 m。

    // 二维里程计当前坐标。
    // 初始化时车头方向为 x 正方向，车体右侧为 y 正方向；向右转角度为正。
    float x = 0.0f; // 小车当前在全局坐标系中的 x 坐标，单位 m。
    float y = 0.0f; // 小车当前在全局坐标系中的 y 坐标，单位 m。

    // 3. gap：update() 根据 start、now 或上一轮 now 计算出的可选差值。

    // 3.1 上一次 update() 到本次 update() 之间的变化量。
    uint16_t update_gap = 0;              // 相邻两次 update() 的时间间隔，单位 ms。
    float left_distance_change = 0.0f;    // 左侧驱动轮在本轮 update() 中的路程变化，单位 m。
    float right_distance_change = 0.0f;   // 右侧驱动轮在本轮 update() 中的路程变化，单位 m。
    float local_forward_change = 0.0f;    // 本轮车体中心沿自身前方的位移，单位 m。
    float local_side_change = 0.0f;       // 本轮车体中心沿自身右方的位移，单位 m。
    float heading_change = 0.0f;          // 本轮 update() 中的航向角变化，单位 deg。
    float x_change = 0.0f;                // 本轮在全局坐标系中的 x 位移，单位 m。
    float y_change = 0.0f;                // 本轮在全局坐标系中的 y 位移，单位 m。

    // 3.2 最近一次 reset() 保存的 start 到本次 update() 的变化量。
    float distance_from_initial = 0.0f; // 当前坐标沿起始车头方向相对起点的有符号距离，单位 m。
    float heading_from_initial = 0.0f;  // 当前航向相对起始航向的角度变化，向右为正，单位 deg。

    Chassis(vex::motor_group &left,
            vex::motor_group &right,
            vex::rotation &forward_tracking,
            vex::rotation &left_tracking,
            vex::inertial &inertial);

    // 设置停车方式并启动惯性传感器校准，不在这里等待。
    void init();

    bool is_ready();

    // 所有传感器就绪后建立底盘里程计原点。
    void finish_init();

    // 更新传感器读数，并更新运动状态。
    void update();

    // 读取一次最新状态，并把它保存为后续动作计算使用的初始值。
    void reset();

    // 使用最近一次 update() 保存的运动状态处理死区并输出给电机。
    void output(float left_output, float right_output);

    // 按 init() 设置的停车方式停止左右电机组。
    void stop();
};

#endif
