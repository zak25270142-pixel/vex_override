#include "my_main.h"
#include "timer.h"
#include "LCD_menu.h"
#include "key_set.h"

vex::brain Brain;

vex::motor MotorA(vex::PORT1, vex::ratio6_1, true);

CycleTimer main_timer(10); // 暂定10ms  生成主循环周期 类

void my_Init()
{ // main.cpp while 前
    TimerA.reset();
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

    static uint32_t last_time = TimerA.time();
    uint32_t now = TimerA.time();
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

// VEX AI Vision Sensor (aivision 类) 详细学习指南，专为 VEXU 零基础队员准备。我会从最基础的概念开始，逐步解释头文件中的 API、如何使用、视觉基础知识，以及如何用它控制机器人移动。
// 1. 视觉基础知识（先理解这些再看代码）
// 相机如何“看”世界：

// 相机捕获图像（通常 320x240 像素分辨率）。
// 颜色检测（Color Detection）：把图像中特定颜色的“blob”（连续色块）找出来，计算它的位置、大小、中心点等。类似“找红球”。
// 颜色码（Color Code）：多个颜色按特定顺序排列的图案，用于识别更复杂的对象（如条纹标记）。
// AprilTag：一种特殊的黑白方块二维码（像 QR 码但更适合机器人），能精确给出位置、旋转角度和 ID。非常适合定位和导航。
// AI 模型检测（Model/AI Object）：用预训练的 AI 模型识别特定物体（如红球、绿环等），返回置信度（score）。这是“AI Vision”名字的来源。

// 白平衡 (White Balance / AWB)：

// 不同灯光下，同一个物体颜色看起来会变（比如白灯 vs 黄灯）。
// Auto White Balance 会自动校正，让白色真正看起来是白色。传感器上电或调用 startAwb() 时会运行（需要几秒）。如果颜色看起来“偏色”，就重新跑 AWB。

// 其他重要概念：

// Snapshot：每次调用 takeSnapshot() 就是“拍一张当前看到的照片”并处理它。必须在循环中反复调用，才能实时跟踪。
// 坐标系：图像左上角 (0,0)，右下角约 (320,240)。中心约 (160,120)。
// largestObject：传感器按大小排序，最大的排第一个。
// 距离估算：物体像素宽度/高度越大，通常离相机越近（可以用这个粗略判断距离）。

// 学习建议：

// 先用 VEXcode（Blocks 或 Python）玩几天，配置颜色、看实时画面，非常直观。
// 看 VEX 官方视频系列（PD+）：Introducing the AI Vision Sensor、Detecting a Color、AprilTags 等。
// 配置工具：用 VEXcode 的 AI Vision Utility（连接传感器到电脑）来训练颜色签名（Color Signature）。
// 然后转到 C++（你的头文件）。

// 2. 头文件核心 API 解释（vex::aivision）
// C++aivision ai(PORT1);                    // 基本构造
// // 或带描述：aivision ai(PORT1, color1, color2, code1);
// 主要功能方法：

// takeSnapshot(...)：最重要的函数。拍快照并过滤对象。返回找到的对象数量。
// ai.takeSnapshot( colordesc ) 或 codedesc：找特定颜色/码。
// ai.takeSnapshot( aivision::ALL_COLORS )：找所有颜色。
// ai.takeSnapshot( aivision::ALL_TAGS )：找所有 AprilTag。
// ai.takeSnapshot( aivision::ALL_AIOBJS )：找 AI 模型物体。
// 可选 count 参数：最多返回多少个对象（默认 8，最多 24）。

// 启用不同模式（默认颜色开启，其他关闭）：
// ai.colorDetection(true, merge=false); — 颜色检测（merge 可合并相邻颜色块）。
// ai.tagDetection(true); — AprilTag。
// ai.modelDetection(true); — AI 模型。

// startAwb(); — 启动自动白平衡（等 2 秒）。
// reset(); — 恢复默认设置。
// set(const colordesc &desc); / set(const codedesc &desc); — 动态修改描述。

// object 类关键属性（ai.largestObject 或 ai.objects[i]）：

// exists：是否真的检测到。
// centerX, centerY：中心像素坐标（最常用）。
// originX, originY：左上角。
// width, height：大小（像素）。
// area：面积（width*height）。
// angle：旋转角度（Color Code 或 Tag 有用）。
// id：Tag 或 AI 物体的 ID。
// score：AI 检测的置信度 (0-100)。
// color：颜色对象用。
// tag：AprilTag 的四个角坐标。

// colordesc（颜色描述）：
// C++aivision::colordesc MYRED(1, 红值, 绿值, 蓝值, hue范围, sat范围);
// // 例如: aivision::colordesc RED(1, 222, 79, 85, 10.0, 0.2);
// 这些值最好通过 VEXcode Utility 配置后复制出来（RGB + 范围）。
// codedesc：由多个 colordesc 组成的多色码。
// 3. 基本使用示例（C++）
// C++#include "vex.h"
// using namespace vex;

// aivision::colordesc RED(1, 200, 50, 50, 15, 0.3);  // 自己调或从Utility复制

// aivision ai(PORT1, RED);   // 构造时传入描述

// int main() {
//     ai.colorDetection(true);
//     // ai.tagDetection(true);  // 需要时开启

//     while(true) {
//         ai.takeSnapshot(aivision::ALL_COLORS);  // 或具体 RED

//         if(ai.largestObject.exists) {
//             // 打印信息
//             Brain.Screen.printAt(10, 20, "CenterX: %d", ai.largestObject.centerX);

//             // 示例：转向让物体居中
//             int error = ai.largestObject.centerX - 160;  // 偏差
//             if(error > 20) {
//                 // 右转
//             } else if(error < -20) {
//                 // 左转
//             } else {
//                 // 直行
//             }
//         } else {
//             // 没看到 -> 搜索（转圈）
//         }

//         wait(20, msec);  // 不要太快，传感器处理需要时间
//     }
// }
// 4. 用视觉控制机器人移动（Visual Servoing / 视觉伺服）
// 核心思路：把视觉误差转成电机控制（PID 最好）。
// 常见任务：

// 对准物体（Alignment）：用 centerX - 160 作为转向误差。
// 接近物体（Approach）：用 width 或 centerY 判断距离，往前开直到足够大。
// 跟随（Follow）：结合上面，保持在视野中央 + 一定距离。
// AprilTag 导航：用 ID 区分不同位置，用 angle 调整朝向，用大小估距离。

// 简单比例控制示例（Proportional）：
// C++double Kp_turn = 0.5;   // 调参
// double Kp_drive = 0.3;

// int error_x = ai.largestObject.centerX - 160;
// drivetrain.turn( error_x * Kp_turn );  // 简单版，实际用 velocity

// // 距离控制
// if(ai.largestObject.width < 100) {
//     drivetrain.drive(forward);
// }
// 进阶：

// 用 PID 控制转向和前进（减小震荡）。
// 多传感器融合（惯性 + 视觉）。
// 滤波：连续几帧都看到才相信（避免噪声）。
// 丢失目标处理：搜索模式（左右转找）。

// 5. 学习路线 & 实践建议（VEXU 适用）

// Week 1：连接传感器，用 VEXcode Utility 配置 3-5 个颜色。跑官方示例，看实时画面。
// Week 2：写简单对准 + 接近程序（红环/球）。
// Week 3：AprilTag 测试（放几个不同 ID 的 Tag 做路标）。
// Week 4：AI 模型 + 颜色结合，写完整自主程序。
// 调试技巧：
// 灯光一致（比赛场灯）。
// 经常跑 AWB。
// 传感器安装位置高一点、角度向下，避免震动。
// 用 Brain.Screen 实时画物体框调试。

// 帧率：颜色 ~30fps，AI 模式可能慢一点。

// 资源：

// VEX KB 和 API 文档（搜索 AI Vision）。
// VEX Forum（很多 C++ 示例）。
// 官方视频系列。
// 对于 VEXU，颜色 + AprilTag 通常足够可靠，AI 模型适合特定游戏元素。

// 常见坑：

// 没反复 takeSnapshot() → 数据是旧的。
// 颜色在不同光照下失效 → 调范围或 AWB。
// 视野被挡 / 震动大 → 优化安装。
// 性能：不要在循环里做太多复杂计算。

// 从简单颜色对准开始练手，慢慢加复杂逻辑。你会很快上手！有具体代码问题或想实现某个功能（比如“跟着红球”），直接贴代码我帮你调试。加油，VEXU 视觉会让你们机器人强很多！ 🚀
// 需要我给你一个完整的可运行示例工程结构吗？