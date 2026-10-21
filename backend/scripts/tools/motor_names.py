# -*- coding: utf-8 -*-
"""电机监控项中文名约定（脚本侧，不属于后端）。

后端只认实车目录里的**中文名**或 **index**，不认识任何"英文名/缩写"——
下位机也从不发送英文名，只有中文显示名。所以 L/R、vact/rpm/cur 这套缩写
纯属本套测试脚本自己约定的别名，就放在脚本这边维护，别塞进后端。

下位机改了监控项命名时，只改这一个文件即可：

    motor_names("L2") -> {"vact": "左2实际电压", "rpm": "左2转速", "cur": "左2电流"}

用法（用 bl.mon_dir 校验一遍，缺项早点报错，别静默跑空）：
    ids = motor_names("L2")
    missing = [k for k, n in ids.items() if n not in bl.mon_dir]
    if missing:
        raise RuntimeError(f"监控表里没有 L2 的 {missing}")
"""

# L/R = 左/右，0~3 = 该侧第几个电机
_SIDE_CN = {"L": "左", "R": "右"}
# 缩写 → 中文后缀（按键名与固件监控表保持一致）
_SUFFIX_CN = {"vact": "实际电压", "rpm": "转速", "cur": "电流"}


def motor_names(motor: str) -> dict[str, str]:
    """'L2' → {'vact': '左2实际电压', 'rpm': '左2转速', 'cur': '左2电流'}。"""
    cn = _SIDE_CN[motor[0].upper()]
    n = int(motor[1])
    return {k: f"{cn}{n}{sfx}" for k, sfx in _SUFFIX_CN.items()}
