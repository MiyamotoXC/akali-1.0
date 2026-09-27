# -*- coding: utf-8 -*-
"""
伤害计算公式引擎 - 原子化计算工具集合

本模块包含所有伤害计算的基础公式，每个函数独立可调用：
- get_ship_size: 舰种大小映射
- compute_attack_power: 最终攻击力计算
- compute_defense_decay: 防御衰减
- compute_crit: 暴击计算
- compute_hit_rate: 命中率
- compute_damage_increase: 伤害增益
- compute_damage_reduction: 伤害减免
- compute_modifier: 克制修正
- compute_ignite_prob: 点燃概率
- evaluate_formula: 公式求值
- parse_multiplier: 倍率解析
"""
import os
import math
import re
import importlib.util
from typing import Dict, Any, Optional, Tuple

# 加载 constants 模块
def _load_constants():
    """加载常量模块"""
    _HERE = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        'common_constants',
        os.path.join(_HERE, 'constants.py')
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_constants = _load_constants()
DAMAGE_MODIFIER = _constants.DAMAGE_MODIFIER
PENETRATION_PROB = _constants.PENETRATION_PROB
IGNITE_PROB = _constants.IGNITE_PROB
COURSE_DAMAGE_MOD = _constants.COURSE_DAMAGE_MOD
COURSE_HIT_MOD = _constants.COURSE_HIT_MOD
AIR_SUPERIORITY_MOD = _constants.AIR_SUPERIORITY_MOD
AIR_HIT_MOD = _constants.AIR_HIT_MOD
ATTACK_TO_DEFENSE = _constants.ATTACK_TO_DEFENSE

# 攻击属性名
_ATTR_NAMES = ("炮击", "雷击", "航空", "轰炸", "鱼雷", "索敌", "命中", "幸运", "装填", "对空", "装甲", "机动")

# 属性后缀清理表：去掉技能描述中的冗余后缀
_ATTR_SUFFIXES = [
    '的炮击伤害', '的雷击伤害', '的航空伤害', '的轰炸伤害',
    '的鱼雷伤害', '的对空伤害', '的近战伤害', '的伤害',
    '的炮击', '的雷击', '的航空', '的轰炸', '的鱼雷',
    '的对空', '的近战', '的攻击伤害', '的攻击',
]

# 混合面板伤害类型 → 属性合计公式
_DAMAGE_TYPE_EXPR = {
    "高平炮": "炮击+对空",
    "三式弹": "炮击+对空",
    "近战": "炮击+装甲",
}

# 舰种大小映射（唯一真相源：筛选「小/中/大船」分类与伤害计算的尺寸减免键都走这里）
_SHIP_SIZE_MAP = {
    '驱逐': '小型', '补给': '小型', '水母': '小型', '潜艇': '小型',
    '巡洋': '中型', '轻巡': '中型', '重巡': '中型', '雷巡': '中型', '轻母': '中型',
    '战列': '大型', '战巡': '大型', '重炮': '大型', '航母': '大型', '装母': '大型', '航战': '大型',
}


def get_ship_size(ship_type: str) -> str:
    """根据舰种返回大小型标识"""
    return _SHIP_SIZE_MAP.get(ship_type, '大型')


def _clean_attr_name(attr: str) -> str:
    """清理属性名，去掉后缀如 '的炮击伤害' -> '炮击'"""
    s = attr.strip()
    for suffix in _ATTR_SUFFIXES:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    return s.strip()


def _apply_attr_buffs(base: float, buffs: Dict, attr_name: str, extra_percent: float = 0.0) -> float:
    """对单属性基础值应用 固定值/百分比 buff（与 _get_effective_attr 的 buff 公式一致）。
    attr_name 以 '值' 结尾时先剥掉，保证 buff 键为 '{属性}值固定增加' 格式。
    extra_percent：攻击力% 等全局加成，与属性% **同乘区加算**（用户口径 2026-09-05：
    攻击力+10% 与 航空+10% → 航空段 ×1.2，而非连乘 ×1.21）。"""
    if attr_name.endswith('值'):
        attr_name = attr_name[:-1]
    fixed_buff = float(buffs.get(f"{attr_name}值固定增加", 0) or 0)
    percent_buff = float(buffs.get(f"{attr_name}值百分比增加", 0) or 0)
    return (base + fixed_buff) * (1 + percent_buff + extra_percent)


def evaluate_formula(expr: str, attack_panel: Dict, defense_panel: Dict, buffs: Dict,
                     extra_percent: float = 0.0) -> float:
    """公式求值（支持属性名+值的映射）。extra_percent：与各属性%同乘区加算的攻击力%"""
    expr = expr.replace('（', '(').replace('）', ')')

    # 清理属性名：将 "炮击的炮击伤害" -> "炮击" 等
    for attr in _ATTR_NAMES:
        for suffix in _ATTR_SUFFIXES:
            old = attr + suffix
            expr = expr.replace(old, attr)

    namespace = {}
    # 属性值应用攻方 buff（固定值/百分比），与无公式路径 _get_effective_attr 同语义，
    # 保证带攻击力公式的技能（如 780%*炮击）同样吃到「炮击值百分比增加」等面板 buff
    for k, v in attack_panel.items():
        namespace[k] = _apply_attr_buffs(float(v or 0), buffs, k, extra_percent)
    for k, v in defense_panel.items():
        namespace[k] = _apply_attr_buffs(float(v or 0), buffs, k, extra_percent)
    # 属性别名补全
    for src in list(namespace.keys()):
        if not isinstance(src, str):
            continue
        if src.endswith('值'):
            alias = src[:-1]
        else:
            alias = src + '值'
        if alias and alias not in namespace:
            namespace[alias] = namespace[src]
    namespace["math"] = math
    try:
        result = eval(expr, {"__builtins__": None}, namespace)
        return float(result)
    except NameError as e:
        missing = str(e).split("'")[1] if "'" in str(e) else "未知变量"
        raise ValueError(f"公式中引用了未定义的变量: {missing}")


def parse_multiplier(multiplier: Any, attack_panel: Dict, defense_panel: Dict) -> float:
    """倍率解析（支持公式表达式）"""
    if isinstance(multiplier, (int, float)):
        return float(multiplier)
    if isinstance(multiplier, str):
        expr = multiplier.replace('（', '(').replace('）', ')')
        expr = re.sub(r'(\d+\.?\d*)%', r'(\1/100)', expr)
        expr = re.sub(r'(\d)\(', r'\1*(', expr)
        expr = re.sub(r'\)\(', r')*(', expr)
        try:
            return float(expr)
        except ValueError:
            try:
                return evaluate_formula(expr, attack_panel, defense_panel, {})
            except Exception:
                return 0.0
    return 0.0


def split_skill_expression(expr: str) -> Tuple[str, str]:
    """拆分技能倍率表达式为 (攻击力公式, 倍率字符串)。

    与 damage_formula.DamageCalculator.split_skill_expression 保持一致。
    """
    expr = expr.strip()
    match = re.search(r'(\d+\.?\d*)%', expr)
    if match:
        percent_str = match.group(0)
        attack_part = expr.replace(percent_str, '').strip()
        attack_part = re.sub(r'[×x*]+', ' ', attack_part).strip()
        if attack_part.startswith('(') and attack_part.endswith(')'):
            attack_part = attack_part[1:-1].strip()
        return attack_part, percent_str
    else:
        has_attr = any(a in expr for a in _ATTR_NAMES)
        if has_attr:
            return expr, ""
        else:
            return "", expr


def compute_attack_power(
    attack_panel: Dict,
    defense_panel: Dict,
    buffs: Dict,
    damage_type: str,
    attack_expr: Optional[str] = None,
    weapon_type: Optional[str] = None,
) -> Tuple[float, float]:
    """计算最终攻击力

    返回 (最终攻击力, 基础攻击力)
    """
    # 普攻未显式传 attack_expr 时，对混合伤害类型展开多属性
    if not attack_expr:
        key = weapon_type or damage_type
        if key in _DAMAGE_TYPE_EXPR:
            attack_expr = _DAMAGE_TYPE_EXPR[key]

    # 攻击力% 与对应属性% **同乘区加算**（用户口径 2026-09-05）：
    # 航空+鱼雷 混合段，攻击力+10% + 航空+10% → 航空×1.2 + 鱼雷×1.1
    atk_increase = float(buffs.get("攻击力增加", 0) or 0)

    if attack_expr:
        base_attack = evaluate_formula(attack_expr, attack_panel, defense_panel, buffs,
                                       extra_percent=atk_increase)
    else:
        if damage_type == "轰炸机":
            base_attack = _get_effective_attr(attack_panel, defense_panel, buffs, "航空", atk_increase) + \
                         _get_effective_attr(attack_panel, defense_panel, buffs, "轰炸", atk_increase)
        elif damage_type == "鱼雷":
            base_attack = _get_effective_attr(attack_panel, defense_panel, buffs, "航空", atk_increase) + \
                         _get_effective_attr(attack_panel, defense_panel, buffs, "鱼雷", atk_increase)
        else:
            base_attack = _get_effective_attr(attack_panel, defense_panel, buffs, damage_type, atk_increase)

    # 攻击力% 已并入各属性乘区（加算），不再独立连乘（用户口径 2026-09-05）
    final_attack = base_attack
    return final_attack, base_attack


def _get_effective_attr(attack_panel: Dict, defense_panel: Dict, buffs: Dict, attr_name: str,
                        extra_percent: float = 0.0) -> float:
    """获取有效属性值（面板+buff）。extra_percent：攻击力% 与属性% 同乘区加算。"""
    base = float(attack_panel.get(attr_name, 0) or 0) if attr_name in attack_panel else \
           float(defense_panel.get(attr_name, 0) or 0) if attr_name in defense_panel else 0.0
    fixed_buff = float(buffs.get(f"{attr_name}值固定增加", 0) or 0)
    percent_buff = float(buffs.get(f"{attr_name}值百分比增加", 0) or 0)
    return (base + fixed_buff) * (1 + percent_buff + extra_percent)


def compute_defense_decay(final_attack: float, enemy_def: float) -> float:
    """防御衰减计算"""
    if final_attack + 1.3 * enemy_def == 0:
        return 1.0
    return final_attack / (final_attack + 1.3 * enemy_def)


def compute_crit(
    attack_panel: Dict,
    buffs: Dict,
    ammo_type: str,
    weapon_type: str,
    enemy_armor_type: str,
    enemy_luck: float,
    force_crit: bool = False,
) -> Tuple[float, float, float]:
    """暴击计算

    返回 (暴击期望, 最终暴击率, 暴击伤害倍率)。
    force_crit：技能描述含「必定暴击」时整个技能出手按 100% 暴击率计算
    （覆盖基础暴击/暴击率增减益/敌方防暴击）。
    """
    my_luck = float(attack_panel.get("幸运", 0) or 0)
    enemy_luck = float(enemy_luck or 0)

    if my_luck == 0 and enemy_luck == 0:
        base_crit = 0.0
    else:
        base_crit = (1 - (enemy_luck / (enemy_luck + my_luck / 3)) ** 0.8) * 0.8
    base_crit = min(base_crit, 0.70)

    enemy_anti_crit = float(buffs.get("被暴击率", 0) or 0)
    crit_add = float(buffs.get("暴击率增加", 0) or 0)
    if force_crit:
        final_crit_rate = 1.0
    else:
        final_crit_rate = max(0.0, min(1.00, base_crit + crit_add - enemy_anti_crit))

    base_crit_dmg = 0.40
    crit_dmg_add = float(buffs.get("暴击伤害增加", 0) or 0)
    final_crit_dmg = base_crit_dmg + crit_dmg_add

    if ammo_type == "穿甲":
        p_no, p_pen, p_over = PENETRATION_PROB.get((weapon_type, enemy_armor_type), (0, 0, 1))
        crit_expect = p_no * 1.0 + p_pen * (1 + final_crit_dmg) + p_over * (1 + final_crit_rate * final_crit_dmg)
    else:
        crit_expect = 1 + final_crit_rate * final_crit_dmg

    return crit_expect, final_crit_rate, final_crit_dmg


def compute_hit_rate(
    attack_panel: Dict,
    buffs: Dict,
    course: str,
    enemy_evasion: float,
    enemy_sou: float,
    air_status: str,
    enemy_evasion_rate: float = 0.0,
) -> float:
    """命中率计算。

    - enemy_evasion：敌方「机动」，作为基础命中率的分母（命中/(命中+机动)）。
    - enemy_evasion_rate：敌方「回避率」（绝对百分比，0.1 = 10%），从最终命中率中直接扣除。
      回避率是**被攻击方**的属性，来源 = 防守方天赋 / 防守方限时技能 buff / 用户手填
      （用户口径 2026-09-14）。旧实现误从 `buffs['回避率增加']`（攻击方自身的 buff 字典）读取，
      造成两个错误：① 攻击方自己的回避天赋会反噬自己的命中率；② 防守方的回避率（天赋、
      技能、手填）全部无效——敌方 enemy 字典里的「回避率」从未被传入公式。
    """
    base_hit = float(attack_panel.get("命中", 0) or 0)
    fixed_buff = float(buffs.get("命中值固定增加", 0) or 0)
    percent_buff = float(buffs.get("命中值百分比增加", 0) or 0)
    course_hit_mod = COURSE_HIT_MOD.get(course, 0.0)
    air_hit = AIR_HIT_MOD.get(air_status, 0.0)

    base_effective = base_hit + fixed_buff
    total_percent = percent_buff + course_hit_mod + air_hit
    final_hit = base_effective * (1 + total_percent)

    enemy_evasion = float(enemy_evasion or 0)
    if final_hit == 0 and enemy_evasion == 0:
        base_hit_rate = 1.0
    else:
        base_hit_rate = final_hit / (final_hit + enemy_evasion)

    my_sou = float(attack_panel.get("索敌", 0) or 0)
    if course in ["同航", "T优势"]:
        recon_success = True
    elif course in ["反航", "T劣势"]:
        recon_success = False
    else:
        recon_success = my_sou >= float(enemy_sou or 0)
    recon_hit = 0.10 if recon_success else 0.0

    hit_rate_add = float(buffs.get("命中率增加", 0) or 0)
    enemy_evasion_add = float(enemy_evasion_rate or 0)

    final_hit_rate = base_hit_rate + hit_rate_add - enemy_evasion_add + recon_hit
    return max(0.05, min(0.95, final_hit_rate))


def compute_damage_increase(
    buffs: Dict,
    damage_type: str,
    course: str,
    air_status: str,
    ship_type: str,
    enemy_ship_type: str = "",
    ship_type_bonus: Dict = None,
    ammo_type: str = "",
    is_normal: bool = False,
) -> float:
    """伤害增益计算"""
    buff_damage_type = damage_type
    if damage_type in ["高平炮", "三式弹"]:
        buff_damage_type = "炮击"

    base_increase = 1.0 + float(buffs.get("全伤害增加", 0) or 0)

    # 大小型攻击伤害加成：语义为“对X型舰（敌）伤害提升”，按防守方体型选键，
    # 与减伤侧“被X型舰攻击伤害减免”按攻方体型选键互为镜像；
    # 敌方未指定（虚拟敌人=原始伤害口径）时该类依赖目标的加成不参与
    if enemy_ship_type:
        enemy_size = get_ship_size(enemy_ship_type)
        size_buff_key = f"{enemy_size}攻击伤害加成"
        base_increase += float(buffs.get(size_buff_key, 0) or 0)
        # 舰种专属增伤：buffs 规范键（数据管线下发/旗舰光环下发）
        base_increase += float(buffs.get(f"对{enemy_ship_type}伤害增加", 0) or 0)
        # 舰种专属增伤：会话手输 dict（session_overrides.ship_type_damage_bonus）
        if ship_type_bonus and enemy_ship_type in ship_type_bonus:
            base_increase += float(ship_type_bonus.get(enemy_ship_type, 0) or 0)

    # 类型增伤
    if damage_type == "轰炸机":
        type_increase = float(buffs.get("航空伤害增加", 0) or 0) + float(buffs.get("轰炸伤害增加", 0) or 0)
    elif damage_type == "鱼雷":
        type_increase = float(buffs.get("航空伤害增加", 0) or 0) + float(buffs.get("鱼雷伤害增加", 0) or 0)
    elif damage_type == "高平炮":
        type_increase = float(buffs.get("炮击伤害增加", 0) or 0) + float(buffs.get("高平炮伤害增加", 0) or 0)
    else:
        type_increase = float(buffs.get(f"{buff_damage_type}伤害增加", 0) or 0)
    base_increase += type_increase

    # 普攻专属伤害加成：仅对普攻类生效（is_normal），技能/必杀/支援不享受
    if is_normal:
        base_increase += float(buffs.get("普通攻击伤害加成", 0) or 0)
        base_increase += float(buffs.get("普通攻击伤害增加", 0) or 0)

    # 弹种增减伤：按攻击方弹种生效（高爆弹/穿甲弹/通常弹）
    if ammo_type in ("高爆", "穿甲", "通常"):
        base_increase += float(buffs.get(f"{ammo_type}弹伤害增加", 0) or 0)

    # 航向/制空修正
    external = 0.0
    if damage_type in ["炮击", "雷击"]:
        external += COURSE_DAMAGE_MOD.get(course, 0.0)
    elif damage_type in ["航空", "轰炸机", "鱼雷"]:
        external += AIR_SUPERIORITY_MOD.get(air_status, 0.0)

    return base_increase + external


def compute_damage_reduction(
    damage_type: str,
    enemy: Dict,
    ship_type: str = "",
    ship_type_reduction: Dict = None,
    ammo_type: str = "",
) -> float:
    """伤害减免计算（返回剩余伤害比例）"""
    full = float(enemy.get("全伤害减免", 0) or 0)

    buff_damage_type = _map_reduction_damage_type(damage_type)
    type_red = float(enemy.get(f"{buff_damage_type}伤害减免", 0) or 0)

    # 大小型攻击伤害减免
    if ship_type:
        ship_size = get_ship_size(ship_type)
        size_reduction_key = f"{ship_size}攻击伤害减免"
        type_red += float(enemy.get(size_reduction_key, 0) or 0)

    # 舰种专属减伤
    if ship_type_reduction and ship_type and ship_type in ship_type_reduction:
        type_red += float(ship_type_reduction.get(ship_type, 0) or 0)

    # 轰炸机/鱼雷额外叠加航空伤害减免
    if damage_type in ["轰炸机", "鱼雷"]:
        type_red += float(enemy.get("航空伤害减免", 0) or 0)

    # 弹种专属减伤（按攻击方弹种）
    if ammo_type:
        type_red += float(enemy.get(f"{ammo_type}弹伤害减免", 0) or 0)

    total = min(1.0, full + type_red)
    return 1.0 - total


def _map_reduction_damage_type(damage_type: str) -> str:
    """把攻击伤害类型映射为敌方减免字段名"""
    if damage_type == "轰炸机":
        return "轰炸"
    if damage_type in ["高平炮", "三式弹"]:
        return "炮击"
    return damage_type


# 技能/攻击段未写明武器或弹种时，回落角色底层（用户约定 2026-09-05）：
# 「技能没写清的用角色底层的弹种类型，比如胡德是穿甲，那克制就是穿甲弹的，
#   所有计算都是这个逻辑」——克制与点燃查表统一走角色底层兜底。
_VALID_WEAPONS = ('驱逐炮', '轻巡炮', '重巡炮', '战列炮', '高平炮', '三式弹',
                  '近战', '雷击', '鱼雷', '鱼雷机', '轰炸机')
_VALID_AMMO = ('通常', '穿甲', '高爆')
# 舰种 → 主炮武器（段内未写明武器时的角色底层口径；航母系炮击段按既有约定取轻巡口径）
_SHIP_MAIN_GUN = {
    '战列': '战列炮', '战巡': '战列炮', '重炮': '战列炮', '航战': '战列炮',
    '重巡': '重巡炮',
    '轻巡': '轻巡炮', '雷巡': '轻巡炮',
    '驱逐': '驱逐炮', '水母': '驱逐炮', '维修': '驱逐炮',
    '航母': '轻巡炮', '轻母': '轻巡炮', '装母': '轻巡炮',
    '潜艇': '鱼雷',
}


def resolve_attack_weapon(weapon_type: str, damage_type: str, ship_type: str,
                          ammo_type: str = '', fallback_ammo: str = '',
                          fallback_weapon: str = ''):
    """解析实际查表用的 (weapon, ammo)：段内未写清 → 回落角色底层。

    weapon：段内标记「副炮」→ 取角色副武器段武器类型（fallback_weapon，如新泽西副炮=驱逐炮，
            用户口径 2026-09-05）；
            段内武器有效（属克制表武器集合）→ 沿用；
            伤害类型为 轰炸机/鱼雷 → 直接取伤害类型（航母系无炮弹种）；
            否则按角色舰种推主炮（战列→战列炮、驱逐→驱逐炮、重巡→重巡炮…）。
    ammo：段内弹种为空或非标准值（如脏值 '轻巡通常'）→ 取角色底层弹种（面板「弹种」列）。
    """
    w = (weapon_type or '').strip()
    if w == '副炮':
        # 技能描述写「副炮」→ 角色副武器段武器类型；无副武器段回落舰种主炮
        fw = (fallback_weapon or '').strip()
        w = fw if fw in _VALID_WEAPONS else _SHIP_MAIN_GUN.get((ship_type or '').strip(), '') or w
    elif w not in _VALID_WEAPONS:
        dt = (damage_type or '').strip()
        if dt in ('轰炸机', '鱼雷'):
            w = dt
        else:
            w = _SHIP_MAIN_GUN.get((ship_type or '').strip(), '') or w
    a = (ammo_type or '').strip()
    if a not in _VALID_AMMO:
        fa = (fallback_ammo or '').strip()
        a = fa if fa in _VALID_AMMO else ''
    return w, a


def compute_modifier(weapon_type: str, ammo_type: str, enemy_armor_type: str,
                     ship_type: str = '', fallback_ammo: str = '',
                     damage_type: str = '') -> float:
    """克制修正计算。段内武器/弹种未写清时按角色底层兜底（见 resolve_attack_weapon）。"""
    w, a = resolve_attack_weapon(weapon_type, damage_type, ship_type,
                                 ammo_type, fallback_ammo)
    key = (w, a, enemy_armor_type)
    if key in DAMAGE_MODIFIER:
        return DAMAGE_MODIFIER[key]
    return DAMAGE_MODIFIER.get((w, "", enemy_armor_type), 1.0)


def normalize_gun_segment(ship_type: str, damage_type: str, weapon_type: str,
                          ammo_type: str) -> Tuple[str, str]:
    """炮击类段的弹种识别（段内原弹种不动）。

    返回 (weapon_type, ammo_type)：
    - 高平炮/三式弹/近战 必须段内明确标注才算（damage_type 标注，或 weapon_type 显式存储），
      单纯的 炮击+对空 等混合表达式不算特殊弹种，类型跟随段内 damage_type
    - 航母炮击段 → 轻巡炮+通常弹（舰载副炮口径，来自底层 轻巡通常 弹种标签）
    """
    wt = (weapon_type or "").strip()
    if damage_type in ('高平炮', '三式弹', '近战'):
        return damage_type, ammo_type
    if damage_type == '炮击' and wt in ('', '炮击') and ship_type == '航母':
        return '轻巡炮', '通常'
    return wt, ammo_type


def compute_ignite_prob(weapon_type: str, enemy_armor_type: str, ammo_type: str = "高爆") -> float:
    """点燃概率计算"""
    if ammo_type != "高爆":
        return 0.0
    key = (weapon_type, enemy_armor_type)
    return IGNITE_PROB.get(key, 0.0)


def compute_single_type_factors(
    attack_panel: Dict,
    defense_panel: Dict,
    attack_buffs: Dict,
    damage_type: str,
    weapon_type: str,
    ammo_type: str,
    enemy: Dict,
    course: str,
    air_status: str,
    ship_type: str = "",
    attack_expr: Optional[str] = None,
    multiplier: float = 1.0,
    hit_count: int = 1,
    cd: float = 0.0,
    ship_type_bonus: Dict = None,
    ship_type_reduction: Dict = None,
    fallback_ammo: str = "",
    fallback_weapon: str = "",
    is_normal: bool = False,
) -> Dict[str, Any]:
    """单类型伤害因子计算（核心函数）

    返回完整的伤害因子字典
    """
    enemy_armor_type = enemy.get("装甲类型", "轻甲")
    enemy_def = float(enemy.get(ATTACK_TO_DEFENSE.get(damage_type, "装甲"), 0) or 0)

    # 最终攻击力
    final_attack, base_attack = compute_attack_power(
        attack_panel, defense_panel, attack_buffs, damage_type, attack_expr, weapon_type
    )

    # 防御衰减
    defense_decay = compute_defense_decay(final_attack, enemy_def)

    # 暴击
    crit_expect, crit_rate, crit_dmg_mult = compute_crit(
        attack_panel, attack_buffs, ammo_type, weapon_type, enemy_armor_type, enemy.get("幸运", 0)
    )

    # 命中率（敌方回避率来自 enemy['回避率']：防守方天赋/技能/手填）
    hit_rate = compute_hit_rate(
        attack_panel, attack_buffs, course, enemy.get("机动", 0), enemy.get("索敌", 0), air_status,
        enemy.get("回避率", 0)
    )

    # 增伤
    dmg_increase = compute_damage_increase(
        attack_buffs, damage_type, course, air_status, ship_type,
        enemy_ship_type=enemy.get("ship_type", ""),
        ship_type_bonus=ship_type_bonus, is_normal=is_normal
    )

    # 减伤
    enemy_reduction = compute_damage_reduction(
        damage_type, enemy, ship_type=ship_type, ship_type_reduction=ship_type_reduction
    )

    # 克制/点燃查表键：武器与弹种未写清时按角色底层兜底（舰种→主炮，面板弹种列→弹药）
    _w_key, _a_key = resolve_attack_weapon(weapon_type, damage_type, ship_type,
                                           ammo_type, fallback_ammo, fallback_weapon)
    modifier = compute_modifier(_w_key, _a_key, enemy_armor_type)

    # 点燃
    ignite_prob = 0.0
    ignite_dps = 0.0
    if _a_key == "高爆":
        ignite_prob = compute_ignite_prob(_w_key, enemy_armor_type)
        if ignite_prob > 0:
            ignite_dps = 0.10 * _get_effective_attr(attack_panel, defense_panel, attack_buffs, "炮击",
                                                    float(attack_buffs.get("攻击力增加", 0) or 0))

    # 基础伤害
    base_damage = final_attack * dmg_increase * multiplier
    normal = base_damage * defense_decay * enemy_reduction * modifier
    critical = normal * (1 + crit_dmg_mult)
    expected = normal * crit_expect * hit_count

    return {
        "type": "单类型",
        "damage_type": damage_type,
        "weapon_type": weapon_type,
        "ammo_type": ammo_type,
        "base_attack": base_attack,
        "final_attack": final_attack,
        "defense_decay": defense_decay,
        "crit_expect": crit_expect,
        "crit_rate": crit_rate,
        "crit_dmg_mult": crit_dmg_mult,
        "hit_rate": hit_rate,
        "damage_increase": dmg_increase,
        "enemy_reduction": enemy_reduction,
        "modifier": modifier,
        "multiplier": multiplier,
        "hit_count": hit_count,
        "base_damage": base_damage,
        "normal_damage": normal * hit_count,
        "critical_damage": critical * hit_count,
        "expected_damage": expected,
        "ignite_prob": ignite_prob,
        "ignite_dps": ignite_dps,
        "ignite_duration": 10.0,
        "cd": cd,
    }