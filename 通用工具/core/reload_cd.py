# -*- coding: utf-8 -*-
"""装填对普攻 CD 影响的确定性公式（共享纯函数）。

公式（用户口径）：
- 普攻 CD 缩减% = 装填值 × 普攻基础 CD / 60，上限 25%
- 缩减后普攻 CD = 普攻基础 CD × (1 - 缩减% / 100)

该公式原只用于独立参考工具 tools/reload_cd.py（节奏参考）。现由伤害链路
统一调用，使装填计入实际伤害：
- tools/calc.py 总时间总伤时间轴：缩减后 CD 决定普攻触发频率 → 总伤上升；
- core/damage_formula.py 点燃频率：缩减后 CD 决定普攻点燃触发次数 → 点燃总伤上升。
（不改变「单次释放伤害」金额，只改变单位时间内的触发段数。）
"""

# 缩减上限（%）
RELOAD_CD_CAP = 25.0


def reload_cd_reduction(reload_value: float, base_cd: float):
    """返回 (缩减%, 缩减后CD秒)。base_cd<=0 时原样返回 (0, base_cd)。"""
    if base_cd <= 0:
        return 0.0, base_cd
    cap = min(reload_value * base_cd / 60.0, RELOAD_CD_CAP)
    return cap, base_cd * (1 - cap / 100.0)


def reload_reduced_cd(reload_value: float, base_cd: float) -> float:
    """直接返回缩减后的普攻 CD（秒）。"""
    return reload_cd_reduction(reload_value, base_cd)[1]
