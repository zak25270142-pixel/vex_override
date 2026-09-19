#include "chassis.h"

MyMotorGroup::MyMotorGroup(vex::motor &m1, vex::motor &m2,
                           vex::motor &m3, vex::motor &m4)
{
    motors[0] = &m1;
    motors[1] = &m2;
    motors[2] = &m3;
    motors[3] = &m4;
}

void MyMotorGroup::setStopping(vex::brakeType brake)
{
    for (uint8_t i = 0; i < 4; i++)
        motors[i]->setStopping(brake);
}

// 在有定位轮后可舍弃
// void MyMotorGroup::resetPosition()
// {
//     for (uint8_t i = 0; i < 4; i++)
//         motors[i]->resetPosition();
// }

// double MyMotorGroup::position()
// {
//     return motors[0]->position(vex::rotationUnits::rev);
// }

double MyMotorGroup::velocity()
{
    return motors[0]->velocity(vex::velocityUnits::rpm);
}

void MyMotorGroup::spin(vex::directionType dir, double output,
                        vex::velocityUnits units)
{
    // 已假定 -1<=output<=1;max(speed_scale[i])=1
    for (uint8_t i = 0; i < 4; i++)
        motors[i]->spin(dir, output * speed_scale[i], units);
}

void MyMotorGroup::stop()
{
    for (uint8_t i = 0; i < 4; i++)
        motors[i]->stop();
}
