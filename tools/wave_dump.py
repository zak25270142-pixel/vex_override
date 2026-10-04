# 把 probe 波形按时间轴对齐打印（每50ms一行），看蹿动/振荡时序
import csv, sys, glob

path = sys.argv[1] if len(sys.argv) > 1 else \
    sorted(glob.glob(r"d:\work\vex\test1\tools\probe_data\*_wave.csv"))[-1]
data = {}
with open(path, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        data.setdefault(row["item"], []).append((float(row["t_rel_s"]), float(row["value"])))

base = min(v[0][0] for v in data.values() if v)
items = ["左指令转速", "左0转速", "右0转速", "左0指令电压", "右0指令电压", "左0实际电压", "全局X"]
series = {k: sorted(data.get(k, [])) for k in items}

def near(ser, t):
    best = None
    for tt, v in ser:
        if tt <= t + 0.002:
            best = v
        else:
            break
    return best

t0 = min((s[0][0] for s in series.values() if s), default=0)
tN = max((s[-1][0] for s in series.values() if s), default=0)
print("  t(s)  tgt    Lrpm   Rrpm   LVcmd  RVcmd  LVact   X(m)")
t = t0
while t <= tN + 1e-6:
    v = [near(series[k], t) for k in items]
    f = lambda x, w, d: ("%*.*f" % (w, d, x)) if x is not None else " " * (w + d + 1)
    print("%5.2f  %4s %6s %6s %6s %6s %6s %7s" % (
        t - base, f(v[0], 4, 0), f(v[1], 6, 1), f(v[2], 6, 1),
        f(v[3], 6, 2), f(v[4], 6, 2), f(v[5], 6, 2), f(v[6], 7, 3)))
    t += 0.05
