#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 逐电机体检：每台单独加 3V/0.5s，读转速+电流判断 堵转/空转/断线。用完即删。
import sys, time, struct
sys.path.insert(0, r'd:\work\vex\test1\tools')
from speed_loop_probe import Link, CMD_STOP

link = Link('COM8')
link.fetch_dirs()
link.unsubscribe_all(); time.sleep(0.2)
rpm_names = ['左0转速','左1转速','左2转速','左3转速','右0转速','右1转速','右2转速','右3转速']
cur_names = ['左0电流','左1电流','左2电流','左3电流','右0电流','右1电流','右2电流','右3电流']
link.subscribe(rpm_names + cur_names + ['全局X','全局Y','航向'], fast=True)
time.sleep(0.4)

for mid in range(8):
    link.send(CMD_STOP); time.sleep(0.15)
    link.send(0x84, bytes([mid]) + struct.pack('<f', 3.0))
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.5:
        time.sleep(0.05)
    rpm = link.latest(rpm_names[mid])
    cur = link.latest(cur_names[mid])
    x = link.latest('全局X'); y = link.latest('全局Y'); yaw = link.latest('航向')
    link.send(0x84, bytes([mid]) + struct.pack('<f', 0.0))
    link.send(CMD_STOP); time.sleep(0.25)
    side = 'L' if mid < 4 else 'R'
    if abs(rpm) > 20:
        state = 'FREE-SPIN?'
    elif abs(cur) < 0.05:
        state = 'DISCONNECT?'
    else:
        state = 'stall(ok)'
    print('motor%d (%s%d): rpm=%6.1f I=%4.2fA  %s  x=%+.3f y=%+.3f yaw=%+.1f'
          % (mid, side, mid % 4, rpm, cur, state, x, y, yaw))
link.unsubscribe_all(); link.close()
