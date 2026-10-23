"""段统计：读回 print_log 落下的宽表 csv，返回与旧 bl.stats 同构的字典。

2026-10-23 后端 /api/log/stats 删除后的脚本侧正路：原始段本来就已经落盘
（结论的底稿），统计在脚本侧算——后端不再猜语义，API 面不再长钩子。

宽表格式（_write_csv 产）：行首 t_ms，一列一项（中文名），10ms 分桶对齐、
桶内取最后、缺样本留空单元格（pandas 读成 NaN，dropna 即丢掉包点）。

字段与旧后端实现的语义对齐：
  n         非空样本数
  mean      算术均值
  abs_mean  |值| 的均值——看抖动/误差幅度（来回震荡时 mean 正负抵消会骗人）
  abs_max   |值| 的最大值——最坏情况
  std       总体标准差（除 n，不是 n-1）——与旧后端口径一致
  ratio_ge  {str(th): |值|>=th 的样本占比}——"超差时间占比"类判据

旧后端的 min/max/rate_hz 没有脚本消费，故意不在这里实现——
需要的时候在这一个文件里加，不要散落到九个脚本里。
"""
import pandas as pd


def seg_stats(csv_path, names=None, thresholds=()):
    """读一段的宽表 csv，返回 {项名: 统计量dict}。

    names：要统计的列名（中文名）；None = 除 t_ms 外全部列。
    thresholds：ratio_ge 的阈值列表，语义 |值|>=th 的样本占比。
    """
    df = pd.read_csv(csv_path)
    cols = [c for c in df.columns if c != "t_ms"] if names is None else list(names)
    out = {}
    for c in cols:
        if c not in df.columns:
            raise KeyError("csv %s 里没有列 %r（现有列：%s）"
                           % (csv_path, c, list(df.columns)))
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        n = int(len(s))
        if n == 0:
            out[c] = {"n": 0, "mean": None, "abs_mean": None, "abs_max": None,
                      "std": None, "ratio_ge": {}}
            continue
        a = s.abs()
        out[c] = {
            "n": n,
            "mean": float(s.mean()),
            "abs_mean": float(a.mean()),
            "abs_max": float(a.max()),
            "std": float(s.std(ddof=0)),
            "ratio_ge": {str(th): float((a >= th).mean()) for th in thresholds},
        }
    return out
