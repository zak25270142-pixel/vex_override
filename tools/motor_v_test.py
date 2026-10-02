# -*- coding: utf-8 -*-
"""
单电机电压特性实测脚本（电机无负载、未装轮胎时使用）。

协议与主控固件定版一致：
  帧 = A5 | Cmd | Payload | XOR（XOR 从 Cmd 起逐字节异或，小端）
通过 0x84 给单台电机直加电压（绕过速度环），订阅监控表回读：
  左/右0~3 实际电压(V)、转速(pct，红盒100rpm=100%)、电流(A)
每台电机做三组试验：
  up   升压扫描 0→1.2V 步长0.02V：找首次转动与持续启动电压
  down 降压扫描 1.2→0V：找运行中停转电压（迟滞）
  high 2/4/6/8/10/12V 点测：核对实际电压与指令电压的比例
"""

import sys
import time
import struct
import threading
import argparse
from collections import deque, OrderedDict

import serial
from serial.tools import list_ports

FRAME_HEAD = 0xA5
CMD_PING = 0xFF
CMD_PONG = 0xFF
CMD_REQ_MON_DIR = 0x01
CMD_POST_MON_DIR = 0x01
CMD_SUBSCRIBE = 0x02
CMD_MONITOR = 0x02

SUB_FAST = 0x80 | 0x40  # 订阅 + 高速档（10ms 每拍）

# 目录帧里各类型宽度
TYPE_SIZE = {0: 1, 1: 2, 2: 4, 3: 8, 4: 1, 5: 2, 6: 4, 7: 8,
             8: 4, 9: 8, 10: 0, 11: 1, 12: 1, 13: 3, 14: 0}


def frame(cmd, payload=b""):
    x = cmd
    for b in payload:
        x ^= b
    return bytes([FRAME_HEAD, cmd]) + payload + bytes([x])


class Link:
    def __init__(self, port_name):
        self.ser = serial.Serial(port_name, 115200, timeout=0.05)
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.values = {}          # 最近一次监控值 index -> (t, value)
        self.samples = deque()    # (t, index, value) 滚动样本，约保留1秒
        self.mon_dir = OrderedDict()  # index -> (name_str, type, tag)
        self.pong_count = 0
        self._stop = False
        self.rt = threading.Thread(target=self._reader, daemon=True)
        self.pt = threading.Thread(target=self._pinger, daemon=True)
        self.rt.start()
        self.pt.start()

    def send(self, data):
        with self.lock:
            self.ser.write(data)

    def set_volt(self, motor_id, volts):
        # 0x84 payload: [电机编号u8][电压f32小端]
        self.send(frame(0x84, bytes([motor_id]) + struct.pack("<f", volts)))

    def subscribe(self, index, tag=SUB_FAST):
        self.send(frame(CMD_SUBSCRIBE, bytes([index, tag])))

    def unsubscribe_all(self):
        # index=0xFF 对全部生效，tag=0 退订
        self.send(frame(CMD_SUBSCRIBE, bytes([0xFF, 0x00])))

    def _pinger(self):
        # 0.7s 一个 Ping，看门狗 2.56s，留足余量；掉线 failsafe 会刹车打断测试
        while not self._stop:
            try:
                self.send(frame(CMD_PING))
            except Exception:
                pass
            time.sleep(0.7)

    def _reader(self):
        while not self._stop:
            try:
                chunk = self.ser.read(256)
            except Exception:
                continue
            if not chunk:
                continue
            self.buf.extend(chunk)
            self._parse()

    def _parse(self):
        # 逐帧从缓冲里切，长度不够就等下一批字节
        while len(self.buf) >= 3:
            if self.buf[0] != FRAME_HEAD:
                del self.buf[0]
                continue
            cmd = self.buf[1]
            total = None
            if cmd == CMD_PONG:
                total = 3
            elif cmd == CMD_MONITOR:
                total = 3 + 5  # payload=[index][float32]
            elif cmd == CMD_POST_MON_DIR:
                total = self._walk_mon_dir(2)
            if total is None:
                # 不认识的上行帧无法定位长度，丢掉这个伪帧头继续找
                del self.buf[0]
                continue
            if len(self.buf) < total:
                break
            raw = self.buf[:total]
            xor = 0
            for b in raw[1:]:
                xor ^= b
            if xor == 0:
                self._dispatch(cmd, raw)
            del self.buf[:total]

    def _walk_mon_dir(self, start):
        # 返回整帧总长；数据不够返回 None
        p = start
        if len(self.buf) < p + 4:
            return None
        typ = self.buf[p + 1]
        nlen = self.buf[p + 3]
        p += 4 + nlen
        if len(self.buf) < p + 1:
            return None
        ulen = self.buf[p]
        p += 1 + ulen
        vlen = TYPE_SIZE.get(typ, 0)
        p += vlen
        return p + 1  # 末尾校验字节

    def _dispatch(self, cmd, raw):
        t = time.perf_counter()
        if cmd == CMD_PONG:
            self.pong_count += 1
        elif cmd == CMD_MONITOR:
            idx = raw[2]
            (val,) = struct.unpack_from("<f", raw, 3)
            self.values[idx] = (t, val)
            self.samples.append((t, idx, val))
            # 只保留1秒内样本，省内存
            cutoff = t - 1.0
            while self.samples and self.samples[0][0] < cutoff:
                self.samples.popleft()
        elif cmd == CMD_POST_MON_DIR:
            p = 2
            idx = raw[p]
            typ = raw[p + 1]
            tag = raw[p + 2]
            nlen = raw[p + 3]
            name = raw[p + 4:p + 4 + nlen].decode("utf-8", "ignore")
            p += 4 + nlen
            ulen = raw[p]
            self.mon_dir[idx] = (name, typ, tag)

    def fetch_monitor_dir(self, rounds=4, wait=0.6):
        # CDC 偶发丢帧会让目录缺项，连发几轮请求并合并，直到一轮下来没有新项
        self.mon_dir.clear()
        last_count = -1
        for _ in range(rounds):
            self.send(frame(CMD_REQ_MON_DIR))
            time.sleep(wait)
            if len(self.mon_dir) == last_count:
                break
            last_count = len(self.mon_dir)
        return dict(self.mon_dir)

    def window_stats(self, idx, t_start, t_end):
        # 取时间窗内某 index 的样本做统计
        vals = [v for (t, i, v) in self.samples if i == idx and t_start <= t <= t_end]
        if not vals:
            return None
        avg = sum(vals) / len(vals)
        return {
            "n": len(vals),
            "mean": avg,
            "abs_mean": sum(abs(v) for v in vals) / len(vals),
            "abs_max": max(abs(v) for v in vals),
        }

    def close(self):
        self._stop = True
        try:
            self.ser.close()
        except Exception:
            pass


def probe_port():
    # 三个口逐个 Ping，谁回 Pong 谁就是 User 口
    ports = [p.device for p in list_ports.comports()]
    for name in ports:
        print("探测端口 %s ..." % name)
        try:
            ser = serial.Serial(name, 115200, timeout=0.1)
        except Exception as e:
            print("  打不开(%s)，跳过" % e)
            continue
        ser.write(frame(CMD_PING))
        time.sleep(0.4)
        ok = False
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < 0.5:
            buf.extend(ser.read(64))
            if b"\xa5\xff\xff" in buf:
                ok = True
                break
        ser.close()
        if ok:
            print("  -> User 口：%s" % name)
            return name
    return None


def find_motor_items(directory):
    # 按中文名找每台电机的 实际电压/转速/电流 三个监控项 index
    # 名称形如：左0实际电压、右3转速；转速表中有重复项，取第一个即可
    side_cn = {"L": "左", "R": "右"}
    out = {}
    for side in ("L", "R"):
        for n in range(4):
            want = {
                "vact": "%s%d实际电压" % (side_cn[side], n),
                "rpm": "%s%d转速" % (side_cn[side], n),
                "cur": "%s%d电流" % (side_cn[side], n),
            }
            got = {"vact": None, "rpm": None, "cur": None}
            for idx, (name, typ, tag) in directory.items():
                for key, w in want.items():
                    if got[key] is None and name == w:
                        got[key] = idx
            out["%s%d" % (side, n)] = got
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="auto")
    ap.add_argument("--motors", default="L0,L1,L2,L3,R0,R1,R2,R3")
    ap.add_argument("--step", type=float, default=0.02)
    ap.add_argument("--vmax", type=float, default=1.20)
    ap.add_argument("--hold", type=float, default=0.35)
    ap.add_argument("--csv", default="")
    args = ap.parse_args()

    port = probe_port() if args.port == "auto" else args.port
    if not port:
        print("没找到会回 Pong 的 V5 User 口：检查接线、VEXcode 里禁用 User Terminal、关掉占口的网页")
        sys.exit(1)

    link = Link(port)
    time.sleep(0.3)
    p0 = link.pong_count

    directory = link.fetch_monitor_dir()
    print("监控表共 %d 项" % len(directory))
    items = find_motor_items(directory)
    for k, v in items.items():
        print("  %s: %s" % (k, v))

    motors = args.motors.split(",")
    missing = [m for m in motors if None in items[m].values()]
    if missing:
        print("监控项缺失: %s" % missing)
        sys.exit(1)

    rows = []  # 原始结果行，最后写 CSV

    def sweep(motor, phase, voltages):
        ids = items[motor]
        for v in voltages:
            t0 = time.perf_counter()
            link.set_volt(motor_id, v)
            time.sleep(args.hold)
            t1 = time.perf_counter()
            # 只取后半窗（前半段是上一档的过渡）
            ws = t0 + args.hold * 0.45
            st_v = link.window_stats(ids["vact"], ws, t1)
            st_r = link.window_stats(ids["rpm"], ws, t1)
            st_i = link.window_stats(ids["cur"], ws, t1)
            row = {
                "motor": motor, "phase": phase, "vcmd": round(v, 4),
                "vact": None if st_v is None else st_v["mean"],
                "vact_abs": None if st_v is None else st_v["abs_mean"],
                "pct_abs": None if st_r is None else st_r["abs_mean"],
                "pct_max": None if st_r is None else st_r["abs_max"],
                "cur": None if st_i is None else st_i["mean"],
                "n": 0 if st_r is None else st_r["n"],
            }
            rows.append(row)
            print("[%s %-4s] Vcmd=%5.2f  Vact=%6.3f  |rpm|均=%6.2f 峰=%6.2f  I=%5.2f  样本%d"
                  % (motor, phase, v,
                     row["vact"] if row["vact"] is not None else float("nan"),
                     row["pct_abs"] or 0, row["pct_max"] or 0,
                     row["cur"] or 0, row["n"]))

    try:
        # 起始先把 8 台全部清零（0V=惰行，不刹车）
        for mid in range(8):
            link.set_volt(mid, 0.0)
        time.sleep(1.0)

        up_vs = [round(i * args.step, 3) for i in range(int(args.vmax / args.step) + 1)]
        down_vs = list(reversed(up_vs))
        high_vs = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]

        for motor in motors:
            side, n = motor[0], int(motor[1])
            motor_id = n if side == "L" else 4 + n
            ids = items[motor]

            # 只订当前电机的三项，避免高速项太多把串口占满
            link.unsubscribe_all()
            time.sleep(0.1)
            for key in ("vact", "rpm", "cur"):
                link.subscribe(ids[key])
            time.sleep(0.2)

            print("\n===== %s 升压扫描（静止起步）=====" % motor)
            sweep(motor, "up", up_vs)

            # 降到0歇一下，再给满扫描上限建立连续运转，然后降压扫停转点
            link.set_volt(motor_id, 0.0)
            time.sleep(0.8)
            link.set_volt(motor_id, args.vmax)
            time.sleep(0.7)
            print("===== %s 降压扫描（运行中停转点）=====" % motor)
            sweep(motor, "down", down_vs)

            link.set_volt(motor_id, 0.0)
            time.sleep(0.8)
            print("===== %s 高压点检（Vact 对 Vcmd）=====" % motor)
            sweep(motor, "high", high_vs)

            link.set_volt(motor_id, 0.0)
            time.sleep(0.5)
    finally:
        # 无论正常结束还是中途打断，务必撤掉所有电机电压并退订
        for mid in range(8):
            try:
                link.set_volt(mid, 0.0)
            except Exception:
                pass
        try:
            link.unsubscribe_all()
        except Exception:
            pass
        link.close()

    # ---------- 汇总结论 ----------
    print("\n================ 汇总 ================")
    print("判定口径：抽动=升压窗内转速峰值≥1pct；持续启动=窗内平均|转速|≥2pct；")
    print("停转点=降压过程中平均|转速|跌破2pct的那一档（取相邻两档中点）")
    summary = {}
    for motor in motors:
        up = [r for r in rows if r["motor"] == motor and r["phase"] == "up"]
        down = [r for r in rows if r["motor"] == motor and r["phase"] == "down"]
        twitch = next((r["vcmd"] for r in up if (r["pct_max"] or 0) >= 1.0), None)
        start = next((r["vcmd"] for r in up if (r["pct_abs"] or 0) >= 2.0), None)
        stop_v = None
        for i, r in enumerate(down):
            if (r["pct_abs"] or 0) < 2.0:
                # down 从高到低，第一次跌破；上一档还在转
                if i > 0:
                    stop_v = round((r["vcmd"] + down[i - 1]["vcmd"]) / 2, 3)
                else:
                    stop_v = r["vcmd"]
                break
        high = [r for r in rows if r["motor"] == motor and r["phase"] == "high"]
        summary[motor] = (twitch, start, stop_v, high)
        print("%s: 抽动≈%sV  持续启动≈%sV  运行停转≈%sV"
              % (motor,
                 twitch if twitch is not None else ">%.2f" % args.vmax,
                 start if start is not None else ">%.2f" % args.vmax,
                 stop_v if stop_v is not None else "<%.2f" % (0 if not down else down[-1]["vcmd"])))
        for r in high:
            print("    Vcmd=%5.1f -> Vact=%6.3fV  偏差%+5.3fV  |转速|=%6.1fpct  I=%.2fA"
                  % (r["vcmd"], r["vact"] or 0, (r["vact"] or 0) - r["vcmd"],
                     r["pct_abs"] or 0, r["cur"] or 0))

    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as f:
            f.write("motor,phase,vcmd,vact,vact_abs,pct_abs,pct_max,current,samples\n")
            for r in rows:
                f.write("%s,%s,%.3f,%s,%s,%s,%s,%s,%d\n" % (
                    r["motor"], r["phase"], r["vcmd"],
                    "" if r["vact"] is None else "%.4f" % r["vact"],
                    "" if r["vact_abs"] is None else "%.4f" % r["vact_abs"],
                    "" if r["pct_abs"] is None else "%.4f" % r["pct_abs"],
                    "" if r["pct_max"] is None else "%.4f" % r["pct_max"],
                    "" if r["cur"] is None else "%.4f" % r["cur"],
                    r["n"]))
        print("\n原始数据已写入 %s" % args.csv)


if __name__ == "__main__":
    main()
