# -*- coding: utf-8 -*-
"""
伤害计算模块 - 支持动态数量普攻，含完整点燃功能

本模块是 formula_engine 的封装层，保持向后兼容。
所有原子计算函数在 formula_engine.py 中。
"""
import copy
import math
import re
from typing import Dict, Any, Optional, Tuple, List

from core.constants import (
    DAMAGE_MODIFIER,
    PENETRATION_PROB,
    IGNITE_PROB,
    COURSE_DAMAGE_MOD,
    COURSE_HIT_MOD,
    AIR_SUPERIORITY_MOD,
    AIR_HIT_MOD,
    ATTACK_TO_DEFENSE,
)
from core.combat_data import CombatData
from core import formula_engine as engine


def _modifier_show_line(modifier_key: str, ammo_type: str, armor: str, modifier: float) -> str:
    """克制修正展示行。

    特殊武器（高平炮/三式弹/近战/雷击/鱼雷机/轰炸机等）克制表键为 武器||装甲，
    按武器直接映射、与弹种无关 —— 展示时省略弹种位，避免出现
    「(高平炮, 通常, 重甲) 查表」这类误导性写法（用户约定，勿回退）。
    判定依据 = 实际查表键：`(武器, 弹种, 甲)` 不在表、而 `(武器, , 甲)` 在表 → 直映射。
    普通炮类（弹种细分）保持 `(战列炮, 高爆, 重甲)` 展示。
    """
    ammo = ammo_type or ''
    direct = (modifier_key, '', armor) in DAMAGE_MODIFIER and \
        (not ammo or (modifier_key, ammo, armor) not in DAMAGE_MODIFIER)
    if direct:
        return (f'克制修正 = ({modifier_key}, {armor}) 查表'
                f'（{modifier_key} 弹种无关、按武器直映射） = {modifier:.4f}')
    return f'克制修正 = ({modifier_key}, {ammo or "无弹种"}, {armor}) 查表 = {modifier:.4f}'


class DamageCalculator:
    DEBUG = False
    DEBUG_FILE = "debug.txt"

    @staticmethod
    def _debug_log(msg: str):
        if DamageCalculator.DEBUG:
            with open(DamageCalculator.DEBUG_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

    # ==================== 辅助方法 ====================

    @staticmethod
    def get_ship_size(ship_type: str) -> str:
        """根据舰种返回大小型标识：'小型'、'中型' 或 '大型'"""
        return engine.get_ship_size(ship_type)

    @staticmethod
    def get_effective_attr(data: CombatData, attr_name: str) -> float:
        return engine._get_effective_attr(
            data.attack_panel, data.defense_panel, data.buffs, attr_name
        )

    @staticmethod
    def evaluate_formula(expr: str, data: CombatData) -> float:
        try:
            return engine.evaluate_formula(
                expr, data.attack_panel, data.defense_panel, data.buffs
            )
        except ValueError as e:
            DamageCalculator._debug_log(f"公式解析错误: {expr}, 错误: {e}")
            raise

    @staticmethod
    def parse_multiplier(multiplier: Any, data: CombatData) -> float:
        if isinstance(multiplier, (int, float)):
            return float(multiplier)
        if isinstance(multiplier, str):
            expr = multiplier.replace('（', '(').replace('）', ')')
            expr = re.sub(r'(\d+\.?\d*)%', r'(\1/100)', expr)
            expr = re.sub(r'(\d)\(', r'\1*(', expr)
            expr = re.sub(r'\)\(', r')*(', expr)
            DamageCalculator._debug_log(f"倍率表达式: '{multiplier}' -> '{expr}'")
            try:
                return float(expr)
            except ValueError:
                try:
                    return engine.evaluate_formula(
                        expr, data.attack_panel, data.defense_panel, data.buffs
                    )
                except Exception as e:
                    DamageCalculator._debug_log(f"倍率解析错误: {expr} -> {e}")
                    return 0.0
        return 0.0

    @staticmethod
    def split_skill_expression(expr: str) -> Tuple[str, str]:
        """拆分技能倍率表达式为 (攻击力公式, 倍率字符串)。"""
        expr = expr.strip()
        match = re.search(r'(\d+\.?\d*)%', expr)
        if match:
            percent_str = match.group(0)
            attack_part = expr.replace(percent_str, '').strip()
            attack_part = re.sub(r'[×x*]+', ' ', attack_part).strip()
            if attack_part.startswith('(') and attack_part.endswith(')'):
                attack_part = attack_part[1:-1].strip()
            DamageCalculator._debug_log(f"拆分技能表达式: 攻击力='{attack_part}', 倍率='{percent_str}'")
            return attack_part, percent_str
        else:
            has_attr = any(a in expr for a in engine._ATTR_NAMES)
            if has_attr:
                DamageCalculator._debug_log(f"拆分技能表达式: 无%但含属性，作为攻击公式: '{expr}'")
                return expr, ""
            else:
                DamageCalculator._debug_log(f"拆分技能表达式: 无%无属性，作为倍率: '{expr}'")
                return "", expr

    # ==================== 核心因子计算 ====================

    _ATTR_NAMES = engine._ATTR_NAMES
    _DAMAGE_TYPE_EXPR = engine._DAMAGE_TYPE_EXPR

    @staticmethod
    def _get_panel_val(data: CombatData, attr: str) -> float:
        """获取属性的面板值（先取 attack_panel，再取 defense_panel）。"""
        if attr in data.attack_panel:
            return float(data.attack_panel.get(attr, 0) or 0)
        if attr in data.defense_panel:
            return float(data.defense_panel.get(attr, 0) or 0)
        return 0.0

    @staticmethod
    def format_attack_base(data: CombatData, attack_expr: Optional[str], damage_type: str,
                           weapon_type: Optional[str] = None) -> Tuple[str, float]:
        """最终攻击力拆解展示"""
        if not attack_expr:
            key = weapon_type or damage_type
            if key in DamageCalculator._DAMAGE_TYPE_EXPR:
                attack_expr = DamageCalculator._DAMAGE_TYPE_EXPR[key]
        if attack_expr:
            attrs = [a for a in DamageCalculator._ATTR_NAMES if a in attack_expr]
            if not attrs:
                attrs = [damage_type] if damage_type in data.attack_panel or damage_type in data.defense_panel else []
        elif damage_type == "轰炸机":
            attrs = ["航空", "轰炸"]
        elif damage_type == "鱼雷":
            attrs = ["航空", "鱼雷"]
        else:
            attrs = [damage_type] if damage_type in data.attack_panel or damage_type in data.defense_panel else []
        terms = []
        total = 0.0
        atk = float(data.buffs.get("攻击力增加", 0) or 0)
        for a in attrs:
            p = DamageCalculator._get_panel_val(data, a)
            p += float(data.buffs.get(f"{a}值固定增加", 0) or 0)
            # 攻击力% 与属性% 同乘区加算（用户口径 2026-09-05），不再独立连乘
            pc = float(data.buffs.get(f"{a}值百分比增加", 0) or 0) + atk
            total += p * (1 + pc)
            bond_label = "（含羁绊）" if a in ("炮击", "雷击", "航空") else ""
            terms.append(f"{a}{bond_label}{p:.2f}×(1+{pc * 100:g}%)" if pc else f"{a}{bond_label}{p:.2f}")
        base_desc = " + ".join(terms) if terms else f"{total:.2f}"
        return f"({base_desc})", total

    @staticmethod
    def compute_final_attack_power(data: CombatData, damage_type: str, attack_expr: Optional[str] = None,
                                    weapon_type: Optional[str] = None) -> Tuple[float, float]:
        final_attack, base_attack = engine.compute_attack_power(
            data.attack_panel, data.defense_panel, data.buffs,
            damage_type, attack_expr, weapon_type
        )
        DamageCalculator._debug_log(f"最终攻击力: {base_attack:.2f} × (1 + {float(data.buffs.get('攻击力增加', 0) or 0):.2f}) = {final_attack:.2f}")
        return final_attack, base_attack

    @staticmethod
    def compute_defense_decay(final_attack: float, enemy_def: float) -> float:
        return engine.compute_defense_decay(final_attack, enemy_def)

    @staticmethod
    def compute_crit_expect(data: CombatData, ammo_type: str, weapon_type: str, enemy_armor_type: str,
                            enemy_luck: float) -> Tuple[float, float, float]:
        crit_expect, final_crit_rate, final_crit_dmg = engine.compute_crit(
            data.attack_panel, data.buffs, ammo_type, weapon_type, enemy_armor_type, enemy_luck
        )
        DamageCalculator._debug_log(
            f"暴击率: {final_crit_rate:.4f}, 暴击伤害倍率: {final_crit_dmg:.4f}, 暴击期望: {crit_expect:.4f}")
        return crit_expect, final_crit_rate, final_crit_dmg

    @staticmethod
    def compute_hit_rate_with_air(data: CombatData, course: str, enemy_evasion: float, enemy_sou: float,
                                  air_status: str, enemy_evasion_rate: float = 0.0) -> float:
        return engine.compute_hit_rate(
            data.attack_panel, data.buffs, course, enemy_evasion, enemy_sou, air_status,
            enemy_evasion_rate
        )

    @staticmethod
    def compute_damage_increase(data: CombatData, damage_type: str, course: str, air_status: str, enemy: Dict = None, ammo_type: str = "") -> float:
        enemy_ship_type = enemy.get("ship_type", "") if enemy else ""
        return engine.compute_damage_increase(
            data.buffs, damage_type, course, air_status,
            data.ship_type, enemy_ship_type=enemy_ship_type,
            ship_type_bonus=getattr(data, 'ship_type_damage_bonus', None),
            ammo_type=ammo_type,
        )

    @staticmethod
    def _map_reduction_damage_type(damage_type: str) -> str:
        return engine._map_reduction_damage_type(damage_type)

    @staticmethod
    def compute_damage_reduction(damage_type: str, enemy: Dict, data: CombatData = None, ammo_type: str = "") -> float:
        ship_type = data.ship_type if data else ""
        ship_type_reduction = getattr(data, 'ship_type_damage_reduction', None) if data else None
        return engine.compute_damage_reduction(
            damage_type, enemy, ship_type=ship_type, ship_type_reduction=ship_type_reduction,
            ammo_type=ammo_type,
        )

    @staticmethod
    def compute_damage_reduction_detail(damage_type: str, enemy: Dict, data: CombatData = None, ammo_type: str = "") -> Dict:
        """返回减伤各组成部分的详细信息"""
        full = float(enemy.get("全伤害减免", 0) or 0)
        buff_damage_type = engine._map_reduction_damage_type(damage_type)
        base_type_red = float(enemy.get(f"{buff_damage_type}伤害减免", 0) or 0)
        size_reduction = 0.0
        ship_size = ""
        if data:
            ship_size = engine.get_ship_size(data.ship_type)
            size_reduction_key = f"{ship_size}攻击伤害减免"
            size_reduction = float(enemy.get(size_reduction_key, 0) or 0)
        ship_type_reduction = 0.0
        if data and hasattr(data, 'ship_type_damage_reduction'):
            attacker_ship_type = data.ship_type
            if attacker_ship_type and attacker_ship_type in data.ship_type_damage_reduction:
                ship_type_reduction = float(data.ship_type_damage_reduction.get(attacker_ship_type, 0) or 0)
        air_reduction = 0.0
        if damage_type in ["轰炸机", "鱼雷"]:
            air_reduction = float(enemy.get("航空伤害减免", 0) or 0)
        ammo_reduction = float(enemy.get(f"{ammo_type}弹伤害减免", 0) or 0) if ammo_type else 0.0
        return {
            "full": full,
            "base_type": base_type_red,
            "size": size_reduction,
            "size_label": f"{ship_size}攻击伤害减免" if ship_size else "",
            "ship_type": ship_type_reduction,
            "air": air_reduction,
            "ammo": ammo_reduction,
            "ammo_label": f"{ammo_type}弹伤害减免" if ammo_type else "",
            "type_label": f"{buff_damage_type}伤害减免",
        }

    @staticmethod
    def _secondary_gun(data: CombatData) -> Tuple[str, str]:
        """角色副武器段 (weapon_type, ammo_type)；无副武器段返回 ('','')。
        用于技能描述标记「副炮」时的武器/弹种解析（如新泽西副炮=驱逐炮/高爆）。"""
        for a in (getattr(data, 'normal_attacks', None) or []):
            if (a.get('slot') or '') == '副武器':
                return (a.get('weapon_type') or '', a.get('ammo_type') or '')
        return '', ''

    @staticmethod
    def compute_modifier(weapon_type: str, ammo_type: str, enemy_armor_type: str,
                         ship_type: str = '', fallback_ammo: str = '',
                         damage_type: str = '') -> float:
        return engine.compute_modifier(weapon_type, ammo_type, enemy_armor_type,
                                       ship_type=ship_type, fallback_ammo=fallback_ammo,
                                       damage_type=damage_type)

    # ==================== 单类型因子 ====================

    @staticmethod
    def _compute_single_type_factors(
            data: CombatData,
            attack_dict: Dict,
            damage_type: str,
            weapon_type: str,
            ammo_type: str,
            enemy: Dict,
            course: str,
            air_status: str,
            attack_expr: Optional[str] = None,
            crit_expect: Optional[float] = None,
            crit_rate: Optional[float] = None,
            crit_dmg_mult: Optional[float] = None,
            hit_rate: Optional[float] = None,
            force_crit: bool = False,
    ) -> Dict:
        DamageCalculator._debug_log(f"\n--- 开始计算 {damage_type} 类型伤害 ---")
        enemy_armor_type = enemy.get("装甲类型", "轻甲")
        enemy_def = float(enemy.get(ATTACK_TO_DEFENSE.get(damage_type, "装甲"), 0) or 0)
        DamageCalculator._debug_log(f"敌方防御属性: {ATTACK_TO_DEFENSE.get(damage_type, '装甲')} = {enemy_def:.2f}")

        # 段武器标记「副炮」→ 用角色副武器段武器/弹种（用户口径 2026-09-05：
        # 技能写「副炮射击」应读副炮类型如驱逐炮，而非兜底舰种主炮）
        if (weapon_type or '').strip() == '副炮' or (damage_type or '').strip() == '副炮':
            _sw, _sa = DamageCalculator._secondary_gun(data)
            if _sw:
                weapon_type = _sw
                if _sa:
                    ammo_type = _sa

        # 段武器/弹种解析前移：使后续增伤（弹种增伤）、穿甲判定、点燃等均以解析后的弹种为准
        _mw, _ma = engine.resolve_attack_weapon(
            weapon_type, damage_type, getattr(data, 'ship_type', '') or '',
            ammo_type, getattr(data, 'ammo_type', '') or '')

        final_attack, base_attack = engine.compute_attack_power(
            data.attack_panel, data.defense_panel, data.buffs,
            damage_type, attack_expr, weapon_type
        )
        defense_decay = engine.compute_defense_decay(final_attack, enemy_def)

        if crit_expect is None:
            crit_expect, crit_rate, crit_dmg_mult = engine.compute_crit(
                data.attack_panel, data.buffs, ammo_type, weapon_type, enemy_armor_type, enemy.get("幸运", 0),
                force_crit=bool(attack_dict.get("always_crit"))
            )
        if hit_rate is None:
            hit_rate = engine.compute_hit_rate(
                data.attack_panel, data.buffs, course, enemy.get("机动", 0), enemy.get("索敌", 0), air_status,
                enemy.get("回避率", 0)
            )

        dmg_increase = engine.compute_damage_increase(
            data.buffs, damage_type, course, air_status, data.ship_type,
            enemy_ship_type=enemy.get("ship_type", ""),
            ship_type_bonus=getattr(data, 'ship_type_damage_bonus', None),
            ammo_type=_ma,
        )
        enemy_reduction = engine.compute_damage_reduction(
            damage_type, enemy, ship_type=data.ship_type,
            ship_type_reduction=getattr(data, 'ship_type_damage_reduction', None),
            ammo_type=_ma,
        )
        # 克制查表键：段内武器/弹种未写清 → 角色底层兜底（舰种→主炮，面板「弹种」列→弹药）
        modifier = DamageCalculator.compute_modifier(
            weapon_type, ammo_type, enemy_armor_type,
            ship_type=getattr(data, 'ship_type', '') or '',
            fallback_ammo=getattr(data, 'ammo_type', '') or '',
            damage_type=damage_type,
        )
        multiplier = DamageCalculator.parse_multiplier(attack_dict.get("multiplier", 1.0), data)
        hit_count = attack_dict.get("hit", 1)

        base_damage = final_attack * dmg_increase * multiplier
        DamageCalculator._debug_log(f"倍率: {multiplier:.4f}, hit数: {hit_count}")

        normal = base_damage * defense_decay * enemy_reduction * modifier
        critical = normal * (1 + crit_dmg_mult)

        total_original = base_damage * hit_count
        total_normal = normal * hit_count
        total_critical = critical * hit_count
        # 整轮期望 = 整轮实际 × 暴击期望 × 命中率（与 OutputHandler 口径一致；
        # 勿再乘 hit_count 二次——旧代码 expected 内乘一次、total 又乘一次）
        total_expected = total_normal * crit_expect * hit_rate

        DamageCalculator._debug_log(
            f"结果: 未削减={total_original:.2f}, 正常={total_normal:.2f}, 暴击={total_critical:.2f}, 期望={total_expected:.2f}")

        # 详细计算过程
        proc = []
        base_desc, shown_final = DamageCalculator.format_attack_base(
            data, attack_expr, damage_type, weapon_type=weapon_type
        )
        proc.append(f"最终攻击力 = {base_desc} = {shown_final:.2f}")
        proc.append(
            f"防御衰减 = 最终攻击力 {final_attack:.2f} / (最终攻击力 {final_attack:.2f} + 1.3×敌方防御 {enemy_def:.2f}) = {defense_decay:.4f}")

        # 增伤展示：与 engine.compute_damage_increase 的取键逻辑严格一致
        enemy_etype = enemy.get("ship_type", "") if enemy else ""
        size_buff, size_buff_key = 0.0, ""
        tgt_bonus = 0.0
        if enemy_etype:
            size_buff_key = f"{engine.get_ship_size(enemy_etype)}攻击伤害加成"
            size_buff = float(data.buffs.get(size_buff_key, 0) or 0)
            tgt_bonus = float(data.buffs.get(f"对{enemy_etype}伤害增加", 0) or 0)
        full_dmg = float(data.buffs.get("全伤害增加", 0) or 0)
        if damage_type in ["炮击", "雷击"]:
            course_part = COURSE_DAMAGE_MOD.get(course, 0.0)
            air_part = 0.0
        elif damage_type in ["航空", "轰炸机", "鱼雷"]:
            course_part = 0.0
            air_part = AIR_SUPERIORITY_MOD.get(air_status, 0.0)
        else:
            course_part = 0.0
            air_part = 0.0
        # 弹种增伤（按解析后弹种生效，从 dmg_increase 中拆出单独展示）
        ammo_inc = 0.0
        if _ma in ("高爆", "穿甲", "通常"):
            ammo_inc = float(data.buffs.get(f"{_ma}弹伤害增加", 0) or 0)
        type_inc = dmg_increase - (1.0 + full_dmg + size_buff + tgt_bonus + course_part + air_part + ammo_inc)
        proc.append(
            f"增伤系数 = 1 + 全伤害增加 {full_dmg:.4f} + 大小型{size_buff_key}(天赋) {size_buff:.4f} + 对舰种增伤 {tgt_bonus:.4f} + 类型增伤 {type_inc:.4f} + 弹种增伤 {ammo_inc:.4f} + 航向 {course_part:.4f} + 制空 {air_part:.4f} = {dmg_increase:.4f}")

        red_detail = DamageCalculator.compute_damage_reduction_detail(damage_type, enemy, data, ammo_type=_ma)
        parts_red = []
        if red_detail["full"]:
            parts_red.append(f"全伤害减免 {red_detail['full']:.4f}")
        if red_detail["size"]:
            parts_red.append(f"{red_detail['size_label']}(天赋) {red_detail['size']:.4f}")
        if red_detail["base_type"]:
            parts_red.append(f"{red_detail['type_label']} {red_detail['base_type']:.4f}")
        if red_detail["ship_type"]:
            parts_red.append(f"舰种专属减伤 {red_detail['ship_type']:.4f}")
        if red_detail["air"]:
            parts_red.append(f"航空伤害减免 {red_detail['air']:.4f}")
        if red_detail["ammo"]:
            parts_red.append(f"{red_detail['ammo_label']} {red_detail['ammo']:.4f}")
        proc.append(
            f"敌方减伤 = 1 - ({' + '.join(parts_red)}) = {enemy_reduction:.4f}")
        proc.append(_modifier_show_line(_mw, _ma, enemy_armor_type, modifier))
        proc.append(f"基础伤害 = 最终攻击力 {final_attack:.2f} × 增伤系数 {dmg_increase:.4f} × 倍率 {multiplier:.4f} = {base_damage:.2f}")
        proc.append(f"未削减伤害 = 基础伤害 {base_damage:.2f} × 命中段数 {hit_count} = {total_original:.2f}")
        proc.append(
            f"实际伤害 = 未削减伤害 {total_original:.2f} × 防御衰减 {defense_decay:.4f} × 敌方减伤 {enemy_reduction:.4f} × 克制修正 {modifier:.4f} = {total_normal:.2f}")
        if (ammo_type or _ma) == "穿甲":
            proc.append(
                f"击穿最高暴击伤害（穿甲·击穿情形）= 实际伤害 {total_normal:.2f} × (1 + 暴击伤害倍率 {crit_dmg_mult:.4f}) = {total_critical:.2f}")
        else:
            proc.append(
                f"最高暴击伤害 = 实际伤害 {total_normal:.2f} × (1 + 暴击伤害倍率 {crit_dmg_mult:.4f}) = {total_critical:.2f}")

        my_luck = float(data.attack_panel.get("幸运", 0) or 0)
        enemy_luck_val = float(enemy.get("幸运", 0) or 0)
        if my_luck == 0 and enemy_luck_val == 0:
            base_crit_calc = 0.0
        else:
            base_crit_calc = (1 - (enemy_luck_val / (enemy_luck_val + my_luck / 3)) ** 0.8) * 0.8
        base_crit_calc = min(base_crit_calc, 0.70)
        enemy_anti_crit = float(data.buffs.get("被暴击率", 0) or 0)
        crit_add = float(data.buffs.get("暴击率增加", 0) or 0)
        crit_dmg_add = float(data.buffs.get("暴击伤害增加", 0) or 0)
        if force_crit or attack_dict.get("always_crit"):
            # 技能描述「必定暴击」：不展示幸运/暴击率公式，直接 100%（用户口径 2026-09-05）
            proc.append('暴击率 = 100%（技能必定暴击）')
        else:
            proc.append(
                f"基础暴击率 = (1 - (敌方幸运 {enemy_luck_val:.2f} / (敌方幸运 {enemy_luck_val:.2f} + 我方幸运 {my_luck:.2f}/3))^0.8) × 0.8 = {base_crit_calc:.4f}（封顶70%）")
            proc.append(
                f"最终暴击率 = 基础暴击率 {base_crit_calc:.4f} + 暴击率增加buff {crit_add:.4f} - 敌方防暴击率 {enemy_anti_crit:.4f} = {crit_rate:.4f}（封顶100%）")
        # 穿甲弹：以「穿甲判定 + 暴击期望（穿甲）」替代通常的暴击伤害倍率输出
        if (ammo_type or _ma) == "穿甲":
            _p_no, _p_pen, _p_over = engine.PENETRATION_PROB.get((weapon_type, enemy_armor_type), (0, 0, 1))
            _coef_no = 1.0
            _coef_pen = 1 + crit_dmg_mult
            _coef_over = 1 + crit_rate * crit_dmg_mult
            _pw = weapon_type or _mw
            proc.append(
                f"穿甲判定（查穿甲系数表：{_pw} 对 {enemy_armor_type}）未击穿（跳弹）{_p_no * 100:.0f}% ／ 击穿 {_p_pen * 100:.0f}% ／ 过穿 {_p_over * 100:.0f}%")
            proc.append(f"未击穿（跳弹）→ 不暴击，伤害系数 = {_coef_no:.4f}")
            proc.append(f"击穿 → 必定暴击，伤害系数 = 1 + 暴击伤害倍率 {crit_dmg_mult:.4f} = {_coef_pen:.4f}")
            proc.append(
                f"过穿 → 正常暴击判定，伤害系数 = 1 + 暴击率 {crit_rate:.4f} × 暴击伤害倍率 {crit_dmg_mult:.4f} = {_coef_over:.4f}")
            proc.append(
                f"暴击期望（穿甲） = 未击穿 {_p_no:.4f} × {_coef_no:.4f} + 击穿 {_p_pen:.4f} × {_coef_pen:.4f} + 过穿 {_p_over:.4f} × {_coef_over:.4f} = {crit_expect:.4f}")
        else:
            proc.append(
                f"最终暴击伤害倍率 = 基础 0.4000 + 暴击伤害增加buff {crit_dmg_add:.4f} = {crit_dmg_mult:.4f}")

        base_hit = float(data.attack_panel.get("命中", 0) or 0)
        hit_fixed = float(data.buffs.get("命中值固定增加", 0) or 0)
        hit_percent = float(data.buffs.get("命中值百分比增加", 0) or 0)
        course_hit_mod = COURSE_HIT_MOD.get(course, 0.0)
        air_hit_mod = AIR_HIT_MOD.get(air_status, 0.0)
        base_effective = base_hit + hit_fixed
        total_hit_percent = hit_percent + course_hit_mod + air_hit_mod
        final_hit = base_effective * (1 + total_hit_percent)
        enemy_evasion = float(enemy.get("机动", 0) or 0)
        if final_hit == 0 and enemy_evasion == 0:
            base_hit_rate = 1.0
        else:
            base_hit_rate = final_hit / (final_hit + enemy_evasion)
        my_sou = float(data.attack_panel.get("索敌", 0) or 0)
        enemy_sou_val = float(enemy.get("索敌", 0) or 0)
        if course in ["同航", "T优势"]:
            recon_success = True
        elif course in ["反航", "T劣势"]:
            recon_success = False
        else:
            recon_success = my_sou >= enemy_sou_val
        recon_hit = 0.10 if recon_success else 0.0
        hit_rate_add = float(data.buffs.get("命中率增加", 0) or 0)
        # 敌方回避率取 enemy 字典（防守方天赋/限时技能/手填），不再误读攻击方自身 buffs
        enemy_evasion_add = float(enemy.get("回避率", 0) or 0)
        proc.append(
            f"最终命中值 = (命中 {base_hit:.2f} + 命中值固定增加 {hit_fixed:.2f}) × (1 + 命中值百分比增加 {hit_percent:.4f} + 航向修正 {course_hit_mod:.4f} + 制空修正 {air_hit_mod:.4f}) = {final_hit:.2f}")
        proc.append(
            f"基础命中率 = 最终命中值 {final_hit:.2f} / (最终命中值 {final_hit:.2f} + 敌方机动 {enemy_evasion:.2f}) = {base_hit_rate:.4f}")
        proc.append(
            f"最终命中率 = 基础命中率 {base_hit_rate:.4f} + 命中率增加 {hit_rate_add:.4f} - 敌方回避率 {enemy_evasion_add:.4f} + 索敌修正 {recon_hit:.4f} = {hit_rate:.4f}")
        proc.append(
            f"期望伤害 = 实际伤害 {total_normal:.2f} × 暴击期望 {crit_expect:.4f} × 命中率 {hit_rate:.4f} = {total_expected:.2f}")

        ignite_prob = 0.0
        ignite_dps = 0.0
        if _ma == "高爆":
            # 点燃表键为 武器|甲：段内武器未写清时已按角色底层兜底（_mw）
            ignite_prob = engine.compute_ignite_prob(_mw, enemy_armor_type)
            if ignite_prob > 0:
                ignite_dps = 0.10 * engine._get_effective_attr(
                    data.attack_panel, data.defense_panel, data.buffs, "炮击",
                    float(data.buffs.get("攻击力增加", 0) or 0)
                )

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
            "cd": attack_dict.get("cd", 0.0),
            "ignite_prob": ignite_prob,
            "ignite_dps": ignite_dps,
            "ignite_duration": 10.0,
            "proc": proc,
        }

    # ==================== 点燃相关 ====================

    @staticmethod
    def get_ignite_prob(weapon_type: str, enemy_armor_type: str, ammo_type: str = "高爆") -> float:
        """获取高爆弹的点燃概率"""
        return engine.compute_ignite_prob(weapon_type, enemy_armor_type, ammo_type)

    @staticmethod
    def calculate_burn(data: CombatData, enemy: Dict, burn_time: float, burn_skills: List[Dict], course: str,
                       air: str) -> Dict:
        """计算单次点燃总伤害（含普攻和技能点燃）"""
        ignite_resist = float(enemy.get("点燃抵抗", 0) or 0)
        normal_factors = DamageCalculator.calculate_normal_all(data, enemy, course, air)
        total_raw_ignite = 0.0
        total_actual_ignite = 0.0
        details = []

        for factors in normal_factors:
            cd = factors.get('cd', 0)
            if cd <= 0:
                continue
            if burn_time < 2:
                trigger_count = 0
            else:
                trigger_count = int((burn_time - 2) // cd) + 1
            if trigger_count <= 0:
                continue
            ignite_prob = factors.get('ignite_prob', 0)
            ignite_dps = factors.get('ignite_dps', 0)
            ignite_duration = factors.get('ignite_duration', 10)
            hit_count = factors.get('hit_count', 1)
            hit_rate = factors.get('hit_rate', 1.0)
            if ignite_prob <= 0 or ignite_dps <= 0:
                continue
            prob = max(0.0, ignite_prob - ignite_resist)
            if prob <= 0:
                continue
            total_raw = 0.0
            total_actual = 0.0
            for i in range(trigger_count):
                trigger_time = 2 + i * cd
                actual_duration = max(0.0, min(ignite_duration, burn_time - trigger_time))
                if actual_duration <= 0:
                    continue
                raw_per_trigger = prob * ignite_dps * actual_duration * hit_count
                actual_per_trigger = raw_per_trigger * hit_rate
                total_raw += raw_per_trigger
                total_actual += actual_per_trigger
            if total_raw > 0:
                total_raw_ignite += total_raw
                total_actual_ignite += total_actual
                details.append({
                    "source": f"普攻 {factors.get('index', '?')}",
                    "触发次数": trigger_count,
                    "平均单次原始伤害": total_raw / trigger_count,
                    "平均单次实际伤害": total_actual / trigger_count,
                    "原始总伤害": total_raw,
                    "实际总伤害": total_actual,
                    "命中率": hit_rate,
                })

        for skill_cfg in burn_skills:
            count_per_cast = skill_cfg['count']
            prob = max(0.0, skill_cfg['prob'] - ignite_resist)
            dps = skill_cfg['damage']
            duration = skill_cfg['duration']
            cd = skill_cfg.get('cd', 0)
            if cd <= 0:
                continue
            skill_hit_rate = engine.compute_hit_rate(
                data.attack_panel, data.buffs, course, enemy.get("机动", 0), enemy.get("索敌", 0), air,
                enemy.get("回避率", 0)
            )
            if burn_time < cd:
                trigger_count = 0
            else:
                trigger_count = int(burn_time // cd)
            if trigger_count <= 0:
                continue
            total_raw = 0.0
            total_actual = 0.0
            for i in range(trigger_count):
                trigger_time = (i + 1) * cd
                actual_duration = max(0.0, min(duration, burn_time - trigger_time))
                if actual_duration <= 0:
                    continue
                raw_per_cast = count_per_cast * prob * dps * actual_duration
                actual_per_cast = raw_per_cast * skill_hit_rate
                total_raw += raw_per_cast
                total_actual += actual_per_cast
            if total_raw > 0:
                total_raw_ignite += total_raw
                total_actual_ignite += total_actual
                details.append({
                    "source": f"技能点燃 (CD={cd}s)",
                    "触发次数": trigger_count,
                    "平均单次原始伤害": total_raw / trigger_count,
                    "平均单次实际伤害": total_actual / trigger_count,
                    "原始总伤害": total_raw,
                    "实际总伤害": total_actual,
                    "命中率": skill_hit_rate,
                })

        return {
            "total_raw_ignite": total_raw_ignite,
            "total_actual_ignite": total_actual_ignite,
            "details": details,
            "burn_time": burn_time,
        }

    # ==================== 普攻（全部） ====================

    @staticmethod
    def calculate_normal_all(data: CombatData, enemy: Dict, course: str = "同航", air_status: str = "均势") -> List[Dict]:
        results = []
        for idx, attack_dict in enumerate(data.normal_attacks):
            if attack_dict.get("hit", 0) == 0 or attack_dict.get("multiplier", 0) == 0:
                continue
            weapon_type = attack_dict.get("weapon_type") or data.weapon_type or "未知"
            ammo_type = attack_dict.get("ammo_type") or data.ammo_type or ""
            dmg_map = {
                "驱逐炮": "炮击", "轻巡炮": "炮击", "重巡炮": "炮击", "战列炮": "炮击",
                "雷击": "雷击", "鱼雷": "鱼雷", "轰炸机": "轰炸机",
                "高平炮": "炮击", "三式弹": "炮击", "近战": "近战"
            }
            damage_type = attack_dict.get("preset_damage_type") or dmg_map.get(weapon_type, "炮击")
            factors = DamageCalculator._compute_single_type_factors(
                data, attack_dict, damage_type, weapon_type, ammo_type,
                enemy, course, air_status
            )
            factors["index"] = idx + 1
            results.append(factors)
        return results

    # ==================== 技能计算 ====================

    @staticmethod
    def _air_normalized_damage_type(part: Dict) -> str:
        """段伤害类型归一：入库解析时部分航空段被误存为"航空"（如 384%*（轰炸+航空）），
        直查克制表无 航空 键恒 1.0。按倍率表达式引用的属性升级为轰炸机/鱼雷，
        增伤/减伤/克制口径与引擎对 轰炸机/鱼雷 的既有处理一致。"""
        dt = part.get("damage_type") or "炮击"
        if dt != "航空":
            return dt
        expr = (part.get("multiplier") or "") + "".join(part.get("attributes") or [])
        if "轰炸" in expr:
            return "轰炸机"
        if "鱼雷" in expr:
            return "鱼雷"
        return dt

    @staticmethod
    def _compute_skill_factors(data: CombatData, attack_dict: Dict, enemy: Dict, course: str, air_status: str,
                               trigger: str = '', include_skill_buffs: bool = False) -> Optional[Dict]:
        parts = attack_dict.get("parts", [])
        if not parts:
            return None
        cd = attack_dict.get("cd", 0.0)
        DamageCalculator._debug_log(f"\n--- 开始计算技能（{len(parts)}段）---")

        # 技能自带 buff 计入（用户口径 2026-09-05：单次伤害必须计入技能自带的 self/team buff）。
        # deepcopy 防污染原对象；target=enemy 的减益不在此处（由防守方口径处理）。
        # 时间轴路径不开启（include_skill_buffs=False）——持续窗口由 skill_buffs windows 机制负责，避免双计。
        _merged_skill_buff_desc = ''
        if include_skill_buffs and trigger:
            data = copy.deepcopy(data)
            merged = {}
            for w in (data.skill_buffs or []):
                if (w.get('trigger') or '') == trigger and (w.get('target') or 'self') in ('self', 'team'):
                    for k, v in (w.get('buffs') or {}).items():
                        try:
                            fv = float(v)
                        except (TypeError, ValueError):
                            continue
                        data.buffs[k] = float(data.buffs.get(k, 0) or 0) + fv
                        merged[k] = merged.get(k, 0) + fv
            if merged:
                _merged_skill_buff_desc = '、'.join(f'{k}{v:g}' for k, v in merged.items())

        first_dmg_type = DamageCalculator._air_normalized_damage_type(parts[0])
        enemy_armor_type = enemy.get("装甲类型", "轻甲")
        # 技能描述含「必定暴击」→ 该技能整次出手暴击率按 100% 计算（数据层
        # 构建流程 stage 3 回填 always_crit 键，如 夕立/新泽西/日向·改）
        crit_expect, crit_rate, crit_dmg_mult = engine.compute_crit(
            data.attack_panel, data.buffs, data.ammo_type, first_dmg_type, enemy_armor_type, enemy.get("幸运", 0),
            force_crit=bool(attack_dict.get("always_crit"))
        )
        hit_rate = engine.compute_hit_rate(
            data.attack_panel, data.buffs, course, enemy.get("机动", 0), enemy.get("索敌", 0), air_status,
            enemy.get("回避率", 0)
        )
        DamageCalculator._debug_log(f"技能共用暴击率: {crit_rate:.4f}, 命中率: {hit_rate:.4f}")

        part_factors = []
        for p in parts:
            dmg_type = DamageCalculator._air_normalized_damage_type(p)
            # 弹种识别：段内原弹种不动；高平炮/三式弹/近战 需描述明确写出才算，
            # 航母炮击段 → 轻巡炮+通常弹（见 formula_engine.normalize_gun_segment）
            weapon_type, ammo_type = engine.normalize_gun_segment(
                getattr(data, 'ship_type', ''), dmg_type,
                (p.get("weapon_type") or "").strip(), data.ammo_type)
            if not weapon_type:
                weapon_type = dmg_type
            attack_expr, percent_expr = DamageCalculator.split_skill_expression(p.get("multiplier", ""))
            if not percent_expr:
                percent_expr = "1.0"
            temp_attack = {"hit": p.get("hit", 1), "multiplier": percent_expr, "cd": cd}
            factors = DamageCalculator._compute_single_type_factors(
                data, temp_attack, dmg_type, weapon_type, ammo_type,
                enemy, course, air_status,
                attack_expr=attack_expr,
                crit_expect=crit_expect,
                crit_rate=crit_rate,
                crit_dmg_mult=crit_dmg_mult,
                hit_rate=hit_rate,
                force_crit=bool(attack_dict.get("always_crit"))
            )
            part_factors.append(factors)
        if _merged_skill_buff_desc:
            # 技能自带 buff 已并入本次计算（deepcopy 副本上生效，不污染原对象）
            for f in part_factors:
                f.setdefault('proc', []).insert(
                    0, f'技能自带 buff 已计入（{trigger}/self|team）：{_merged_skill_buff_desc}')

        if len(part_factors) == 1:
            return part_factors[0]
        return {
            "type": "混合技能",
            "parts": part_factors,
            "crit_rate": crit_rate,
            "hit_rate": hit_rate,
            "crit_dmg_mult": crit_dmg_mult,
        }

    @staticmethod
    def calculate_skill(data: CombatData, enemy: Dict, course: str = "同航", air_status: str = "均势",
                        include_skill_buffs: bool = False) -> Optional[Dict]:
        attack_dict = data.skill_attack
        if not attack_dict.get("parts"):
            return None
        return DamageCalculator._compute_skill_factors(data, attack_dict, enemy, course, air_status,
                                                       trigger='skill', include_skill_buffs=include_skill_buffs)

    @staticmethod
    def calculate_burst(data: CombatData, enemy: Dict, course: str = "同航", air_status: str = "均势",
                        include_skill_buffs: bool = False) -> Optional[Dict]:
        attack_dict = data.burst_attack
        if not attack_dict.get("parts"):
            return None
        return DamageCalculator._compute_skill_factors(data, attack_dict, enemy, course, air_status,
                                                       trigger='burst', include_skill_buffs=include_skill_buffs)

    @staticmethod
    def calculate_support(data: CombatData, enemy: Dict, course: str = "同航", air_status: str = "均势",
                          include_skill_buffs: bool = False) -> Optional[Dict]:
        """计算支援技单次释放伤害"""
        attack_dict = data.support_attack
        if not attack_dict.get("parts"):
            return None
        return DamageCalculator._compute_skill_factors(data, attack_dict, enemy, course, air_status)

    # ==================== 统一调度 ====================

    @staticmethod
    def compute_all_attacks(data: CombatData, enemy: Dict, course: str, air_status: str, choice: int,
                            include_skill_buffs: bool = False) -> Dict:
        results = {}
        try:
            if choice == 1:
                res = DamageCalculator.calculate_normal_all(data, enemy, course, air_status)
                if res:
                    results["普攻"] = res
            elif choice == 2:
                res = DamageCalculator.calculate_skill(data, enemy, course, air_status,
                                                       include_skill_buffs=include_skill_buffs)
                if res:
                    results["通常技"] = res
            elif choice == 3:
                res = DamageCalculator.calculate_burst(data, enemy, course, air_status,
                                                       include_skill_buffs=include_skill_buffs)
                if res:
                    results["必杀技"] = res
            elif choice == 4:
                res = DamageCalculator.calculate_support(data, enemy, course, air_status,
                                                         include_skill_buffs=include_skill_buffs)
                if res:
                    results["支援技"] = res
        except ValueError as e:
            results["error"] = str(e)
        return results