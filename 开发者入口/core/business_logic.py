# -*- coding: utf-8 -*-
"""
开发者端业务逻辑层

本模块包含开发者端特有的业务逻辑：
1. 伤害对比工具 (DamageComparator)
2. 配置文件管理 (ConfigManager)
3. 输出格式化 (OutputFormatter)
4. 从通用工具聚合的统一入口（SimpleDamageCalculator、SkillParser 等）

所有计算逻辑在通用工具 core/formula_engine.py 和 core/damage_calc.py 中。
"""
import json
import os
import importlib.util
from typing import Dict, Any

# 通过 importlib 加载通用工具中的公共模块
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_COMMON_TOOLS_DIR = os.path.join(_PROJECT_ROOT, "通用工具", "core")

def _load_common_module(filename, module_name):
    """加载通用工具中的模块"""
    filepath = os.path.join(_COMMON_TOOLS_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# 加载通用工具中的计算模块
_common_calc = _load_common_module('damage_calc.py', 'common_damage_calc')
SimpleDamageCalculator = _common_calc.SimpleDamageCalculator

# 加载通用工具中的 skill_parser
_common_parser = _load_common_module('skill_parser.py', 'common_skill_parser')
SkillParser = _common_parser.SkillParser
detect_written_weapon = _common_parser.detect_written_weapon

# 加载通用工具中的 skill_buff_extractor
_common_buff = _load_common_module('skill_buff_extractor.py', 'common_skill_buff_extractor')
extract_skill_buffs = _common_buff.extract_skill_buffs
extract_flagship_buffs = _common_buff.extract_flagship_buffs
extract_duration_contrib = _common_buff.extract_duration_contrib
_map_buff = _common_buff._map_buff
_BUFF_RE = _common_buff._BUFF_RE
_DMG_INC = _common_buff._DMG_INC

# 配置文件路径（与底层数据 json 同目录：知识库/底层数据/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_FILE = os.path.join(_PROJECT_ROOT, "知识库", "底层数据", "产物", "custom_ship_config.json")


# ==================== 对比工具 ====================

class DamageComparator:
    """伤害变化对比器"""

    @staticmethod
    def compare(base_result: Dict, new_result: Dict) -> Dict[str, Any]:
        """对比两次完整计算结果，返回变化百分比"""
        comparison = {
            'total_expected': DamageComparator._compare_value(
                base_result['total_expected'], new_result['total_expected']
            ),
            'total_normal': DamageComparator._compare_value(
                base_result['total_normal'], new_result['total_normal']
            ),
            'total_critical': DamageComparator._compare_value(
                base_result['total_critical'], new_result['total_critical']
            ),
            'segments': [],
        }

        for i, (base_seg, new_seg) in enumerate(zip(
            base_result.get('segments', []),
            new_result.get('segments', [])
        )):
            comparison['segments'].append({
                'index': i + 1,
                'normal': DamageComparator._compare_value(
                    base_seg.get('normal_damage', 0),
                    new_seg.get('normal_damage', 0)
                ),
                'critical': DamageComparator._compare_value(
                    base_seg.get('critical_damage', 0),
                    new_seg.get('critical_damage', 0)
                ),
                'expected': DamageComparator._compare_value(
                    base_seg.get('expected_damage', 0),
                    new_seg.get('expected_damage', 0)
                ),
                'crit_rate': DamageComparator._compare_value(
                    base_seg.get('final_crit_rate', 0),
                    new_seg.get('final_crit_rate', 0)
                ),
                'crit_dmg': DamageComparator._compare_value(
                    base_seg.get('final_crit_dmg', 0),
                    new_seg.get('final_crit_dmg', 0)
                ),
            })

        return comparison

    @staticmethod
    def compare_raw(base_result: Dict, new_result: Dict) -> Dict[str, Any]:
        """对比两次伤害计算结果（以原始伤害为基准）"""
        comparison = {
            'total_raw': DamageComparator._compare_value(
                base_result.get('total_original', 0),
                new_result.get('total_original', 0)
            ),
            'segments': [],
        }

        for i, (base_seg, new_seg) in enumerate(zip(
            base_result.get('segments', []),
            new_result.get('segments', [])
        )):
            comparison['segments'].append({
                'index': i + 1,
                'raw': DamageComparator._compare_value(
                    base_seg.get('base_damage', 0),
                    new_seg.get('base_damage', 0)
                ),
            })

        return comparison

    @staticmethod
    def _compare_value(base: float, new: float) -> Dict[str, Any]:
        """计算单个值的变化"""
        change = new - base
        change_pct = (change / base * 100) if base != 0 else 0.0

        return {
            'base': base,
            'new': new,
            'change': change,
            'change_pct': change_pct,
            'is_increase': change > 0,
            'is_decrease': change < 0,
        }


# ==================== 配置管理 ====================

class ConfigManager:
    """配置文件管理器"""

    @staticmethod
    def load_config() -> Dict:
        """加载配置，旧配置自动补齐新增字段"""
        defaults = ConfigManager._create_default_config()
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # 补齐顶层新增字段
            for key in ('attacker_mode', 'attacker_ship_name', 'defender_mode', 'defender_ship_name',
                        'enemy_mode', 'enemy_ship_name', 'calc_time'):
                if key not in config:
                    config[key] = defaults[key]
            # 补齐 ship/enemy 的新增字段
            for section in ('custom_ship', 'enemy'):
                if section in config:
                    for key in ('nation', 'ammo_type'):
                        if key not in config[section]:
                            config[section][key] = defaults[section][key]
            # 补齐自定义靶子（enemy）字段
            if 'enemy' in config:
                if 'armor_type' not in config['enemy']:
                    config['enemy']['armor_type'] = '轻甲'
                config['enemy'].setdefault('buffs', {})
            return config
        return defaults

    @staticmethod
    def save_config(config: Dict):
        """保存配置"""
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _create_default_config() -> Dict:
        """创建默认配置"""
        return {
            'attacker_mode': 'ship',
            'attacker_ship_name': '',
            'defender_mode': 'custom',
            'defender_ship_name': '',
            'enemy_mode': 'none',
            'enemy_ship_name': '',
            'calc_time': 0,
            'custom_ship': {
                'name': '自定义航母',
                'ship_type': '航母',
                'nation': '',
                'ammo_type': '',
                'panel': {
                    '血量': 40000,
                    '炮击': 1000,
                    '雷击': 800,
                    '航空': 1500,
                    '对空': 400,
                    '装甲': 300,
                    '命中': 250,
                    '机动': 150,
                    '索敌': 150,
                    '幸运': 35,
                },
                'buffs': {
                    '全伤害增加': 0.2,
                },
            },
            'enemy': {
                'name': '标准战列',
                'ship_type': '战列',
                'nation': '',
                'ammo_type': '',
                'panel': {
                    '血量': 50000,
                    '炮击': 1200,
                    '雷击': 0,
                    '航空': 0,
                    '对空': 300,
                    '装甲': 500,
                    '命中': 200,
                    '机动': 100,
                    '索敌': 100,
                    '幸运': 25,
                },
                'buffs': {
                    '全伤害减免': 0.2,
                },
            },
            'skill_versions': {
                '通常技': {
                    'name': '通常技',
                    'description': '270%炮击伤害x1',
                },
                '必杀技': {
                    'name': '必杀技',
                    'description': '1350%炮击伤害x1、480%雷击伤害x2',
                },
            },
            'settings': {
                'course': '同航',
                'air_status': '均势',
            },
            'history': [],
        }


# ==================== 输出格式化 ====================

class OutputFormatter:
    """输出格式化工具"""

    @staticmethod
    def format_damage_result(result: Dict, title: str = "伤害计算结果",
                              mode: str = 'full') -> str:
        """格式化伤害计算结果

        Args:
            result: 计算结果
            title: 标题
            mode: 'full' (完整) 或 'raw' (原始伤害)
        """
        lines = [
            f"\n{'='*60}",
            f"  {title}",
            f"{'='*60}",
        ]

        for seg in result['segments']:
            lines.append(f"\n  --- 第 {seg['segment_index']} 段: {seg['damage_type']}伤害 ---")
            lines.append(f"  技能描述: {seg['raw_text'][:50]}...")
            lines.append(f"  倍率: {seg['multiplier']:.4f} x 命中: {seg['hit_count']}")
            lines.append(f"  属性: {' + '.join(seg['attributes'])}")
            lines.append(f"  最终攻击力: {seg['final_attack']:.2f}")
            lines.append(f"  增伤系数: {seg['dmg_increase']:.4f}")

            if mode == 'raw':
                lines.append(f"  原始伤害: {seg['raw_damage']:.2f}")
            else:
                lines.append(f"  防御衰减: {seg.get('defense_decay', 0):.4f}")
                lines.append(f"  敌方减伤: {seg.get('enemy_reduction', 0):.4f}")
                lines.append(f"  基础暴击率: {seg.get('base_crit_rate', 0):.4f}")
                lines.append(f"  最终暴击率: {seg.get('final_crit_rate', 0):.4f}")
                lines.append(f"  暴击伤害倍率: {seg.get('final_crit_dmg', 0):.4f}")
                lines.append(f"  命中率: {seg.get('hit_rate', 0):.4f}")
                lines.append(f"  正常伤害: {seg.get('normal_damage', 0):.2f}")
                lines.append(f"  暴击伤害: {seg.get('critical_damage', 0):.2f}")
                lines.append(f"  期望伤害: {seg.get('expected_damage', 0):.2f}")

        lines.append(f"\n  --- 总计 ---")
        if mode == 'raw':
            lines.append(f"  总原始伤害: {result.get('total_raw', 0):.2f}")
        else:
            lines.append(f"  总正常伤害: {result.get('total_normal', 0):.2f}")
            lines.append(f"  总暴击伤害: {result.get('total_critical', 0):.2f}")
            lines.append(f"  总期望伤害: {result.get('total_expected', 0):.2f}")
        lines.append(f"{'='*60}")

        return '\n'.join(lines)

    @staticmethod
    def format_comparison(comparison: Dict, base_version: str,
                          new_version: str, mode: str = 'full') -> str:
        """格式化对比结果"""
        lines = [
            f"\n{'='*60}",
            f"  伤害变化对比",
            f"  基准版本: {base_version} -> 新版本: {new_version}",
            f"{'='*60}",
        ]

        if mode == 'raw':
            lines.append(f"\n  总原始伤害变化:")
            lines.append(f"    基准: {comparison['total_raw']['base']:.2f}")
            lines.append(f"    新版: {comparison['total_raw']['new']:.2f}")
            lines.append(f"    变化: {comparison['total_raw']['change']:.2f} ({comparison['total_raw']['change_pct']:+.2f}%)")

            for seg in comparison['segments']:
                symbol = '+' if seg['raw']['is_increase'] else '-' if seg['raw']['is_decrease'] else ' '
                lines.append(f"\n  --- 第 {seg['index']} 段 ---")
                lines.append(f"  原始伤害: {symbol} {seg['raw']['change_pct']:+.2f}%")
        else:
            lines.append(f"\n  总期望伤害变化:")
            lines.append(f"    基准: {comparison['total_expected']['base']:.2f}")
            lines.append(f"    新版: {comparison['total_expected']['new']:.2f}")
            lines.append(f"    变化: {comparison['total_expected']['change']:.2f} ({comparison['total_expected']['change_pct']:+.2f}%)")

            for seg in comparison['segments']:
                symbol = '+' if seg['expected']['is_increase'] else '-' if seg['expected']['is_decrease'] else ' '
                lines.append(f"\n  --- 第 {seg['index']} 段 ---")
                lines.append(f"  期望伤害: {symbol} {seg['expected']['change_pct']:+.2f}%")
                lines.append(f"  暴击率: {symbol} {seg['crit_rate']['change_pct']:+.2f}%")
                lines.append(f"  暴击伤害: {symbol} {seg['crit_dmg']['change_pct']:+.2f}%")

        lines.append(f"\n{'='*60}")
        return '\n'.join(lines)