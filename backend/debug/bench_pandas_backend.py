"""临时基准：宽表化之后，存储与导出这两件事用不用 pandas 差多少。

场景统一按"50 项 × 5000 帧 = 250k 样本"（100Hz 下 50 秒）：
  A 当前形态  {idx: [(t_ms, value), ...]}      list of tuple
  B 宽表形态  numpy float64 (5000, 50) + 一个 t 轴 (5000,)
对比：常驻内存、以及"分桶对齐导出"这一步的耗时。
"""
import time
import tracemalloc

N_ITEM = 50
N_FRAME = 5000
N = N_ITEM * N_FRAME

# ---------------------------------------------------------------- A 当前形态
tracemalloc.start()
base = tracemalloc.get_traced_memory()[0]
buckets = {i: [(f * 10, f * 0.01 + i) for f in range(N_FRAME)] for i in range(N_ITEM)}
cur, _a_peak = tracemalloc.get_traced_memory()
a_mem = cur - base
tracemalloc.stop()

# ---------------------------------------------------------------- B 宽表形态
import numpy as np  # noqa: E402

tracemalloc.start()
base2 = tracemalloc.get_traced_memory()[0]
t_axis = np.arange(N_FRAME, dtype=np.float64) * 10.0
mat = np.empty((N_FRAME, N_ITEM), dtype=np.float64)
for i in range(N_ITEM):
    mat[:, i] = np.arange(N_FRAME, dtype=np.float64) * 0.01 + i
cur2, _ = tracemalloc.get_traced_memory()
b_mem_py = cur2 - base2
tracemalloc.stop()
b_mem = t_axis.nbytes + mat.nbytes + b_mem_py

print("=== 常驻内存（50 项 × 5000 帧） ===")
print("A 分桶 list-of-tuple : %8.2f MB   (%.1f B/样本)" % (a_mem / 1e6, a_mem / N))
print("B numpy 宽表 float64 : %8.2f MB   (%.1f B/样本)" % (b_mem / 1e6, b_mem / N))
print("   其中裸数组 %.2f MB，Python 侧 %.4f MB" % ((t_axis.nbytes + mat.nbytes) / 1e6, b_mem_py / 1e6))
print("   倍数：A 是 B 的 %.1f 倍" % (a_mem / b_mem))

# ---------------------------------------------- 导出步骤：分桶对齐 + 缺值统计
LOG_BUCKET_MS = 10
names = ["项%d" % i for i in range(N_ITEM)]


def bucket_python():
    """当前 _write_csv 的核心：遍历长表分桶，桶内取最后，统计空单元格。"""
    rows = []
    for i in range(N_ITEM):
        nm = names[i]
        for t, v in buckets[i]:
            rows.append((t, nm, v))
    rows.sort(key=lambda r: r[0])
    slots: dict[int, dict] = {}
    for t, nm, v in rows:
        b = int(t // LOG_BUCKET_MS)
        slots.setdefault(b, {})[nm] = v
    missing = {c: 0 for c in names}
    for b in sorted(slots):
        slot = slots[b]
        for c in names:
            if c not in slot:
                missing[c] += 1
    return missing


def bucket_pandas():
    """宽表 + pandas：每行已是同拍，缺值由 DataFrame 自带 NaN 表达。"""
    import pandas as pd  # noqa: E402
    df = pd.DataFrame(mat, columns=names, index=t_axis)
    df[~np.isfinite(df)] = np.nan
    return df.isna().sum().to_dict()


def from_buckets_pandas():
    """保留当前分桶存储，只在导出时用 pandas 对齐（不动写入路径）。"""
    import pandas as pd  # noqa: E402
    series = {}
    for i in range(N_ITEM):
        pts = buckets[i]
        ts = np.fromiter((p[0] for p in pts), dtype=np.int64, count=len(pts))
        vs = np.fromiter((p[1] for p in pts), dtype=np.float64, count=len(pts))
        s = pd.Series(vs, index=ts // LOG_BUCKET_MS)
        series[names[i]] = s.groupby(level=0).last()
    df = pd.concat(series, axis=1)
    return df.isna().sum().to_dict()


for fn in (bucket_python, bucket_pandas, from_buckets_pandas):
    fn()  # 预热（from_buckets 首次含 pandas import）
    t0 = time.perf_counter()
    r = fn()
    dt = (time.perf_counter() - t0) * 1000
    print("%-20s %8.1f ms   missing 列数=%d" % (fn.__name__, dt, len(r)))
