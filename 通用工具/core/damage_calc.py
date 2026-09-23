# -*- coding: utf-8 -*-
"""
简化版伤害计算器 - 供开发者端和用户端共用

与 damage_formula.py 的区别：
- 本模块是简化版，只计算原始伤害（攻击力 × 增伤 × 倍率 × 命中数）
- 不含航向/制空/敌方减免等复杂因素
- 用于技能数值评估和版本对比

个体计算方法委托给 formula_engine，编排方法保留。
"""
import os
import importlib.util
from typing import Dict, Any, List, Tuple

# 加载 formula_engine（原子计算工具集）
def _load_formula_engine():
    """加载 formula_engine 模块"""
    _HERE = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        'common_formula_engine',
        os.path.join(_HERE, 'formula_engine.py')
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

engine = _load_formula_engine()


class SimpleDamageCalculator:
    """简化版伤害计算器 - 用于开发者端

    与用户端 DamageCalculator 计算逻辑完全一致，唯一区别是没有防御方。
    防御衰减和减伤乘区设为默认值（1.0 和 1.0），其它乘区完全相同。
    """

    # 默认的敌方参数（没有防御方时使用）
    _DEFAULT_ENEMY = {
        '装甲类型': '轻甲',
        '幸运': 0,
        '机动': 0,
        '索敌': 0,
        'ship_type': '',
    }

    @staticmethod
    def calculate_attack_power(ship: Dict, dmg_type: str,
                                attrs: List[str] = None,
                                multiplier_expr: str = '') -> float:
        """计算攻击力（委托给 formula_engine）"""
        panel = ship.get('panel', {})
        buffs = ship.get('buffs', {})
        attack_expr = None
        if multiplier_expr:
            attack_expr, _ = engine.split_skill_expression(multiplier_expr)
        if not attack_expr and attrs:
            attack_expr = '+'.join(attrs)
        final_attack, _ = engine.compute_attack_power(
            panel, {}, buffs, dmg_type, attack_expr
        )
        return final_attack

    @staticmethod
    def calculate_defense_decay(attack: float, defense: float) -> float:
        """计算防御衰减（委托给 formula_engine）"""
        return engine.compute_defense_decay(attack, defense)

    @staticmethod
    def calculate_crit(ship: Dict, enemy: Dict) -> Dict[str, float]:
        """计算暴击率和暴击伤害（委托给 formula_engine）"""
        panel = ship.get('panel', {})
        buffs = ship.get('buffs', {})
        enemy_panel = enemy.get('panel', {})
        enemy_luck = float(enemy_panel.get('幸运', 0) or 0)
        enemy_armor_type = enemy_panel.get('装甲类型', '轻甲')
        crit_expect, crit_rate, crit_dmg = engine.compute_crit(
            panel, buffs, '通常', '', enemy_armor_type, enemy_luck
        )
        my_luck = float(panel.get('幸运', 0) or 0)
        if my_luck == 0 and enemy_luck == 0:
            base_crit = 0.0
        else:
            base_crit = (1 - (enemy_luck / (enemy_luck + my_luck / 3)) ** 0.8) * 0.8
        base_crit = min(base_crit, 0.70)
        return {
            'base_crit_rate': base_crit,
            'final_crit_rate': crit_rate,
            'final_crit_dmg': crit_dmg,
            'crit_expect': crit_expect,
        }

    @staticmethod
    def calculate_hit_rate(ship: Dict, enemy: Dict,
                           course: str = '同航', air_status: str = '均势') -> float:
        """计算命中率（委托给 formula_engine）"""
        panel = ship.get('panel', {})
        buffs = ship.get('buffs', {})
        enemy_panel = enemy.get('panel', {})
        enemy_evasion = float(enemy_panel.get('机动', 0) or 0)
        enemy_sou = float(enemy_panel.get('索敌', 0) or 0)
        # enemy 传 to_enemy_dict 口径时「回避率」在顶层；传裸 panel 时在同一 panel 内，两者都认
        enemy_evasion_rate = enemy.get('回避率', enemy_panel.get('回避率', 0))
        return engine.compute_hit_rate(panel, buffs, course, enemy_evasion, enemy_sou, air_status,
                                       enemy_evasion_rate)

    @staticmethod
    def calculate_raw_damage(attacker: Dict,
                              skill_segments: List[Dict],
                              skill_buffs: Dict[str, float] = None,
                              course: str = '同航',
                              air_status: str = '均势',
                              ammo_type: str = '通常',
                              enemy: Dict = None) -> Dict[str, Any]:
        """计算伤害（enemy=None 时为原始伤害口径）

        使用 engine.compute_single_type_factors 计算，确保与用户端逻辑完全相同。
        enemy=None：防御衰减/敌方减伤强制 1.0（无防守方口径），克制按默认轻甲。
        传入 enemy（to_enemy_dict 口径字典：装甲/装甲类型/对空/机动/索敌/幸运/减免键）：
        防御衰减/敌方减伤/克制修正按真实防守方生效，与用户端一致。
        """
        if skill_buffs is None:
            skill_buffs = {}

        attack_panel = attacker.get('panel', {})
        base_buffs = attacker.get('buffs', {})
        ship_type = attacker.get('ship_type', '战列')
        enemy_eff = enemy if enemy else SimpleDamageCalculator._DEFAULT_ENEMY

        # 合并基础 buff 和技能 buff
        merged_buffs: Dict[str, float] = {}
        for k, v in base_buffs.items():
            merged_buffs[k] = float(v or 0)
        for k, v in skill_buffs.items():
            merged_buffs[k] = merged_buffs.get(k, 0) + float(v)

        results = []
        total_normal = 0.0
        total_critical = 0.0
        total_expected = 0.0
        total_original = 0.0

        for seg_idx, seg in enumerate(skill_segments):
            dmg_type = seg['damage_type']
            weapon_type = seg['weapon_type']
            multiplier = seg['multiplier']
            hit_count = seg['hit_count']
            raw_text = seg.get('raw_text', '')

            # 弹种识别：段内原弹种不动；高平炮/三式弹/近战 需段内明确标注才算，
            # 航母炮击段 → 轻巡炮+通常弹（与用户端同源）
            weapon_type, seg_ammo = engine.normalize_gun_segment(
                ship_type, dmg_type, weapon_type, ammo_type)
            if not weapon_type:
                weapon_type = dmg_type

            # 提取 attack_expr（与用户端一致）
            attack_expr, percent_expr = engine.split_skill_expression(raw_text)
            if not percent_expr:
                percent_expr = str(multiplier)

            # 使用 engine 核心函数计算（与用户端完全一致）
            seg_result = engine.compute_single_type_factors(
                attack_panel, {}, merged_buffs,
                dmg_type, weapon_type, seg_ammo,
                enemy_eff,
                course, air_status,
                ship_type=ship_type,
                attack_expr=attack_expr,
                multiplier=multiplier,
                hit_count=hit_count,
                fallback_ammo=str(attacker.get('ammo_type', '') or ''),
                fallback_weapon=str(attacker.get('secondary_weapon', '') or ''),
            )

            # 无防守方口径：修正防御衰减和减伤为默认值
            if enemy is None:
                seg_result['defense_decay'] = 1.0
                seg_result['enemy_reduction'] = 1.0

            # 重新计算伤害值（使用修正后的乘区）
            final_attack = seg_result['final_attack']
            dmg_increase = seg_result['damage_increase']
            modifier = seg_result['modifier']
            cd = seg_result['cd']

            base_damage = final_attack * dmg_increase * multiplier
            normal = base_damage * seg_result['defense_decay'] \
                * seg_result['enemy_reduction'] * modifier
            critical = normal * (1 + seg_result['crit_dmg_mult'])
            # 整轮期望 = 单发实际 × 暴击期望 × 命中率 × hit（与 OutputHandler 口径一致）
            expected = normal * seg_result['crit_expect'] * seg_result['hit_rate'] * hit_count

            seg_result['base_damage'] = base_damage
            seg_result['normal_damage'] = normal * hit_count
            seg_result['critical_damage'] = critical * hit_count
            seg_result['expected_damage'] = expected
            seg_result['segment_index'] = seg_idx + 1

            results.append(seg_result)

            total_normal += normal * hit_count
            total_critical += critical * hit_count
            total_expected += expected
            total_original += base_damage * hit_count

        return {
            'segments': results,
            'total_original': total_original,
            'total_normal': total_normal,
            'total_critical': total_critical,
            'total_expected': total_expected,
            'total_raw': total_expected,
            'skill_buffs': skill_buffs,
            'base_buffs': base_buffs,
            'course': course,
            'air_status': air_status,
            'ammo_type': ammo_type,
            'enemy_used': enemy is not None,
        }
