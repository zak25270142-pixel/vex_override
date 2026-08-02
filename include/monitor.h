#include "my_main.h"

extern vex::inertial imu;

extern double imu_angle;
extern double imu_roll;
extern double imu_pitch;
extern double imu_yaw;
extern double imu_rotation;
extern vex::inertial::attitude imu_att;

struct vect3D_db
{
    double x;
    double y;
    double z;
};

extern struct vect3D_db imu_a;
extern struct vect3D_db imu_g;