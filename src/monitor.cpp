#include "monitor.h"

// vex::inertial imu(vex::PORT1); // 惯性，已有见chassis

// vex::rotation rotationA(vex::PORT2, false); // 角度，已有见chassis

vex::optical optical(vex::PORT13, false); // 光学

vex::distance distance(vex::PORT14); // 距离

// vex::electromagnet electromagnet(vex::PORT5);//电磁铁(非传感器)

// vex::gps gps(vex::PORT6,0,vex::turnType::right);//gps(未知是否使用)

// vex::controller controller;//遥控器，已有，见key_set(非传感器)

// vex::brain brain;//主控，已有，见my_main(非传感器)

// vex::competition competition;//比赛控制框架，暂时无用(非传感器)

// vex::triport triport(vex::PORT7);//三线端口，未知用法(非传感器)

// vex::timer timer;//计时器，已有，见timer 待扩展(非传感器)

double optical_hue;
double optical_brightness;
vex::color optical_color;
vex::optical::gesture optical_gesture;
vex::optical::rgbc optical_rgbc;
double optical_integrationTime;

double distance_ObjectDistance;
vex::sizeType distance_ObjectSize;
int32_t distance_ObjectRawSize;
double distance_ObjectVelocity;
bool distance_isObjectDetected;

void monitor_init()
{
    optical.objectDetected(nullptr);
    optical.objectLost(nullptr);
    optical.gestureUp(nullptr);
    //...

    distance.changed(nullptr);
}

void monitor_reset()
{
    optical.setLight(vex::ledState::on);
    optical.setLightPower(100, vex::percentUnits::pct);
    optical.gestureDisable();
    optical.gestureEnable();
    optical.integrationTime(2);
    optical_integrationTime = optical.integrationTime();

    distance.changed(nullptr);
}

void monitor_refresh()
{
    optical_hue = optical.hue();
    optical_brightness = optical.brightness();
    optical_color = optical.color();
    optical_rgbc = optical.getRgb(true);

    distance_ObjectDistance = distance.objectDistance(vex::distanceUnits::mm);
    distance_ObjectSize = distance.objectSize();
    distance_ObjectRawSize = distance.objectRawSize();
    distance_ObjectVelocity = distance.objectVelocity();
    distance_isObjectDetected = distance.isObjectDetected();
}
