# -*- coding: utf-8 -*-
"""
常量定义模块 - 静态数值常量（克制修正/概率表/航向/制空修正等）

数据统一存放于 `知识库/底层数据/mappings.json`（一个 JSON 文件的多个类别），
本模块启动时加载并还原 tuple 键（mtime 感知缓存，改 JSON 无需重启）。
类别：
- damage_modifier   甲弹克制修正表 (炮种|弹种|装甲) → 系数
- penetration_prob  穿甲概率表   (舰种|装甲) → (未击穿, 击穿, 过穿)
- ignite_prob       点燃概率表   (舰种|装甲) → 概率
- course_damage_mod 航向增伤    航向 → 系数
- course_hit_mod    航向命中修正 航向 → 系数
- air_superiority_mod 制空增伤   制空 → 系数
- air_hit_mod       制空命中修正 制空 → 系数
- attack_to_defense 攻击类型 → 敌方防御属性
"""
import json
import os

# constants.py 位于 通用工具/core/，上溯三级到项目根（阿卡莉1.0/）
_KB_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "知识库"))
_MAPPINGS_FILE = os.path.join(_KB_DIR, "底层数据", "源文件", "mappings.json")

_cache = None
_mtime = None

_TUPLE_CATEGORIES = {
    "damage_modifier", "penetration_prob", "ignite_prob",
}


def _load_mappings() -> dict:
    """加载 mappings.json（mtime 感知缓存），tuple 键按 '|' 还原。"""
    global _cache, _mtime
    try:
        cur = os.path.getmtime(_MAPPINGS_FILE)
    except OSError:
        return {}
    if _cache is not None and _mtime == cur:
        return _cache
    try:
        with open(_MAPPINGS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        _cache, _mtime = {}, cur
        return _cache
    out = {}
    for cat, d in raw.items():
        if cat in _TUPLE_CATEGORIES and isinstance(d, dict):
            out[cat] = {tuple(k.split("|")): v for k, v in d.items()}
        else:
            out[cat] = d
    _cache, _mtime = out, cur
    return out


_M = _load_mappings()

DAMAGE_MODIFIER = _M.get("damage_modifier", {})
PENETRATION_PROB = _M.get("penetration_prob", {})
IGNITE_PROB = _M.get("ignite_prob", {})
COURSE_DAMAGE_MOD = _M.get("course_damage_mod", {})
COURSE_HIT_MOD = _M.get("course_hit_mod", {})
AIR_SUPERIORITY_MOD = _M.get("air_superiority_mod", {})
AIR_HIT_MOD = _M.get("air_hit_mod", {})
ATTACK_TO_DEFENSE = _M.get("attack_to_defense", {})

# 阵型 → 生效 buff（标准键，计算时叠加到角色；空/机动=无修正）。
# 单一来源：用户端对话路由（calc_flow）与开发者端伤害计算器共用此映射。
FORMATION_BUFFS = {
    '单纵': {'炮击伤害增加': 0.12, '雷击伤害增加': 0.12},
    '复纵': {'机动值百分比增加': 0.25},
    '轮型': {'航空伤害增加': 0.15, '对空伤害增加': 0.15, '炮击伤害增加': -0.05, '雷击伤害增加': -0.05},
    '梯型': {'暴击率增加': 0.15, '暴击伤害增加': 0.05, '被暴击率增加': 0.05},
}
