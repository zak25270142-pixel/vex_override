# AI Vision 基础使用说明

当前视觉模块只做两件事：

1. 分别找出红、黄、蓝、灰四种颜色中面积最大的颜色框。
2. 从一帧画面中找出面积最大的 AprilTag。

没有目标时只把对应的 `visible` 设为 `false`。模块不判断 Pin、Cup、柱高和嵌套关系，也不控制底盘或机械结构。

## 程序如何运行

初始化时调用一次：

```cpp
ai_vision_init();
```

它会开启颜色检测和 AprilTag 检测，关闭 AI 模型分类，并执行一次自动白平衡。当前传感器接在主控智能端口 10。

主循环中周期调用：

```cpp
ai_vision_refresh();
```

当前工程约每 40 ms 拍摄一帧。一次拍摄同时更新颜色和 AprilTag，结果分别保存在 `ai_colors` 和 `ai_tag` 中。

## 读取颜色结果

```cpp
// 下标 0~3 分别代表红、黄、蓝、灰。
const uint8_t RED = 0;

if (ai_colors[RED].visible)
{
    // 画面中心约为 (160, 120)。
    int x = ai_colors[RED].center_x;
    int y = ai_colors[RED].center_y;

    // 检测框的像素宽高。
    int width = ai_colors[RED].width;
    int height = ai_colors[RED].height;
}
```

四种颜色拥有各自独立的结果，所以红色和蓝色可以在同一帧同时 `visible`。同一种颜色出现多个框时，只保留该颜色面积最大的一个。颜色框越大通常表示物体越近，但它不是实际距离。

颜色编号来自 [ai_vision.cpp](../src/ai_vision.cpp) 中的颜色签名。重新使用 AI Vision Utility 标定后，需要把新的 RGB、色调范围和饱和度范围填回这个文件。

## 读取 AprilTag 结果

```cpp
if (ai_tag.visible)
{
    int tag_id = ai_tag.id;
    int x = ai_tag.center_x;
    int y = ai_tag.center_y;
    int width = ai_tag.width;
    int height = ai_tag.height;
    float angle = ai_tag.angle_deg;
}
```

如果一帧里出现多个 AprilTag，程序只返回面积最大的一个。比赛用 Tag 实物尺寸相同时，它通常也是离相机最近的一个。

`center_x` 可以直接判断左右位置：

- 小于 160：目标在画面左侧。
- 接近 160：目标接近画面中央。
- 大于 160：目标在画面右侧。

`angle_deg` 是 Tag 在画面中的原始旋转角，没有滤波或角度换算。

## 当前限制

- 颜色和 Tag 结果都是当前帧原始值，可能抖动。
- 透明 Cup 没有稳定颜色，不能靠基础颜色框得到完整轮廓。
- 320×240 的广角画面不适合远距离识别很小的物体。
- 颜色识别会受到灯光、反光和背景颜色影响。

等实机确实需要某项功能时，再根据实际测试结果增加对应处理，不提前加入柱高、目标组合或底盘控制逻辑。

## 主控屏幕调试页

程序首页有两个入口：

- `enter_menu`：进入原来的参数菜单。
- `AI vision debug`：进入视觉调试页。

视觉页每 100 ms 刷新一次，显示以下信息：

- `PORT 10 CONNECTED/DISCONNECTED`：主控能否检测到 10 号口的 AI Vision。
- `OBJECTS`：最近一帧返回的颜色框与 AprilTag 总数。
- 颜色表：红、黄、蓝、灰各自显示 `YES/NONE`、中心和宽高，可以同时出现多个 `YES`。
- `APRILTAG FOUND/NONE`：是否检测到 Tag；找到时显示 ID、中心、宽高和原始旋转角。
- `BOX VIEW 1:2`：把 320×240 检测坐标缩小为 160×120 后画出的检测框，不是摄像头画面。

颜色框按对应颜色绘制，AprilTag 框使用白色。静态界面只在进入页面时绘制一次，周期刷新只覆盖数值行和右侧小视图，不会反复清除全屏。按页面右下角 `back` 返回首页。

连接状态和检测状态必须分开理解：`CONNECTED` 但显示 `NONE`，表示线路正常，只是当前画面没有符合参数的目标；`DISCONNECTED` 才表示主控没有检测到视觉仪。

## 文件职责

- `src/ai_vision.cpp`：读取传感器并生成颜色和 AprilTag 原始结果。
- `src/class_and_tool/ai_vision_menu_func.cpp`：`AI_VISION_MENU` 类的实现，只负责视觉调试页绘制。
- `src/LCD_menu.cpp`：保存菜单项目、页面状态，并把实际视觉数据传给视觉菜单对象。
- `include/menu_func.h`：包含通用 `MENU` 类，以及视觉菜单类和单行颜色数据接口。

当前页面使用 `MENU_state : uint8_t` 的 `pre`、`main`、`ai_vision` 表示首页、普通菜单和视觉菜单。状态只由 `LCD_menu.cpp` 切换，`AI_VISION_MENU::init()` 不读取也不修改页面状态。

`LCD_menu.cpp` 中的对象列表把名称、显示 RGB 与实际检测框逐项绑定。红、黄、蓝、灰和 AprilTag 使用同一个 `AI_VISION_MENU_OBJECT` 数组，文字行和缩略框由同一次对象遍历绘制。Tag 独有的 ID 和角度单独传入，不再重复声明一套坐标字段。

连接检查通过应用层回调传入。`menu_func.h` 不包含 `ai_vision.h`，视觉菜单类不认识 `AI_COLOR_RESULT`、`AI_TAG_RESULT` 或 `vex::aivision`。
