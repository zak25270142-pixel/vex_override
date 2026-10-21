# -*- coding: utf-8 -*-
"""脚本画图共用件：中文字体兜底 + 落盘。各脚本自己决定画什么。

为什么要单独一个文件：字体/rcParams 那几行在每个画图的脚本里都要写一遍，
写错一处就是全图方块。落盘统一 dpi 与"写完关图"，避免脚本忘了 close 攒内存。

用法：
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from plot_util import subplots, save

    fig, axes = subplots(2)          # axes 恒为二维数组，取 axes[0][i]
    axes[0][0].plot(...)
    save(fig, out_png)
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无 GUI，必须用非交互后端
import matplotlib.pyplot as plt  # noqa: E402


def style() -> None:
    """中文字体兜底：图例/标题多为中文，缺字体就变方块。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def subplots(n: int, width: float = 5.8, height: float = 4.4):
    """开一张 n 栏的图，返回 (fig, axes)；axes[0][i] 取第 i 栏。"""
    style()
    return plt.subplots(1, n, figsize=(width * n, height), squeeze=False)


def legend(ax, **kw):
    """有图例才画——某栏没有数据时 ax.legend() 会刷 UserWarning。"""
    handles, _ = ax.get_legend_handles_labels()
    if handles:
        ax.legend(**kw)


def save(fig, path, dpi: int = 120) -> str:
    """落盘并关图；父目录自动创建。返回路径便于打印。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    print("[out] 图已写出 %s" % path)
    return str(path)
