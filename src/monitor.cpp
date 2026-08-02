#include "monitor.h"

vex::inertial imu(vex::PORT1); // 惯性

vex::rotation rotationA(vex::PORT2, false); // 角度

vex::optical optical(vex::PORT3, false); // 光学

vex::distance distance(vex::PORT4);//距离

//vex::electromagnet electromagnet(vex::PORT5);//电磁铁(非传感器)

//vex::gps gps(vex::PORT6,0,vex::turnType::right);//gps(未知是否使用)

//vex::controller controller;//遥控器，已有，见key_set(非传感器)

//vex::brain brain;//主控，已有，见my_main(非传感器)

//vex::competition competition;//比赛控制框架，暂时无用(非传感器)

//vex::triport triport(vex::PORT7);//三线端口，未知用法(非传感器)

//vex::timer timer;//计时器，已有，见timer 待扩展(非传感器)

double imu_angle;
double imu_roll;
double imu_pitch;
double imu_yaw;
double imu_rotation;
vex::inertial::attitude imu_att;
struct vect3D_db imu_a;
struct vect3D_db imu_g;

double rotation_angle;
double rotation_position;
double rotation_velocity;

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
    imu.calibrate();     // 校准
    imu.resetHeading();  // 朝向设置为0
    imu.resetRotation(); // 累积旋转角设置为0
    imu.changed(nullptr);
    imu.collision(nullptr);

    rotationA.setReversed(true);
    rotationA.changed(nullptr);

    optical.objectDetected(nullptr);
    optical.objectLost(nullptr);
    optical.gestureUp(nullptr);
    //...

    distance.changed(nullptr);

}

bool check_monitor_init()
{
    return imu.isCalibrating();
}

void monitor_reset()
{
    imu.setHeading(0, vex::rotationUnits::deg);
    imu.setRotation(0, vex::rotationUnits::deg);

    rotationA.resetPosition();
    rotationA.setPosition(0, vex::rotationUnits::deg);

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
    imu_angle = imu.angle();
    imu_roll = imu.roll();
    imu_pitch = imu.pitch();
    imu_yaw = imu.yaw();
    imu_rotation = imu.rotation();
    imu.orientation(imu_att);
    imu_a.x = imu.acceleration(vex::axisType::xaxis);
    imu_a.y = imu.acceleration(vex::axisType::yaxis);
    imu_a.z = imu.acceleration(vex::axisType::zaxis);
    imu_g.x = imu.gyroRate(vex::axisType::xaxis, vex::velocityUnits::dps);
    imu_g.y = imu.gyroRate(vex::axisType::yaxis, vex::velocityUnits::dps);
    imu_g.z = imu.gyroRate(vex::axisType::zaxis, vex::velocityUnits::dps);

    rotation_angle = rotationA.angle();
    rotation_position = rotationA.position(vex::rotationUnits::deg);
    rotation_velocity = rotationA.velocity(vex::velocityUnits::dps);

    optical_hue = optical.hue();
    optical_brightness = optical.brightness();
    optical_color = optical.color();
    optical_rgbc = optical.getRgb(true);

    distance_ObjectDistance=distance.objectDistance(vex::distanceUnits::mm);
    distance_ObjectSize=distance.objectSize();
    distance_ObjectRawSize=distance.objectRawSize();
    distance_ObjectVelocity=distance.objectVelocity();
    distance_isObjectDetected=distance.isObjectDetected();
}
