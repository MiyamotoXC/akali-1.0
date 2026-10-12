# -*- coding: utf-8 -*-
"""
通用技能解析器 - 从技能描述文本中提取伤害公式段

与用户端 calc_flow._infer_damage_type 规则保持一致。
"""
import re
from typing import Dict, Any, List, Optional

# 属性名集合（与用户端 damage_formula._ATTR_NAMES 一致）
_ATTR_NAMES = ("炮击", "雷击", "航空", "轰炸", "鱼雷", "索敌", "命中", "幸运", "装填", "对空", "装甲", "机动")


def _infer_damage_type(attr: str) -> str:
    """根据属性名推断伤害类型（与用户端 calc_flow._infer_damage_type 规则一致）"""
    a = attr or ''
    # 碰撞/冲撞伤害等同近战伤害（用户口径 2026-10-06）：无论前置缩放属性是炮击/机动/装甲，
    # 伤害类型一律归为近战，使其正确套用近战类增益与减免
    if '冲撞' in a or '碰撞' in a:
        return '近战'
    if '轰炸' in a and '航空' in a:
        return '轰炸机'
    if '鱼雷' in a and '航空' in a:
        return '鱼雷'
    if '轰炸' in a:
        return '轰炸机'
    if '鱼雷' in a:
        return '鱼雷'
    if '炮击' in a:
        return '炮击'
    if '雷击' in a:
        return '雷击'
    if '航空' in a:
        return '航空'
    if '近战' in a:
        return '近战'
    if '装甲' in a:
        return '近战'
    if '对空' in a:
        # 纯对空伤害段保持 对空 类型（存储口径一致），高平炮弹种由计算侧按 炮击+对空 表达式识别
        return '对空'
    return '炮击'


# 描述明确写出的弹种关键词 → 弹种（写了才算，按顺序匹配，高平两用炮先于高平炮）
WRITTEN_WEAPON_KEYWORDS = [
    ('高平两用炮', '高平炮'),
    ('高平炮', '高平炮'),
    ('三式弹', '三式弹'),
    ('近战', '近战'),
]


def detect_written_weapon(text: str) -> str:
    """从段上下文文字识别明确写出的弹种；未写出返回空串"""
    for kw, wt in WRITTEN_WEAPON_KEYWORDS:
        if kw in (text or ''):
            return wt
    return ''


class SkillParser:
    """通用技能解析器，从技能描述中提取伤害公式段。"""

    @staticmethod
    def parse(skill_desc: str) -> List[Dict[str, Any]]:
        """
        解析技能描述，返回技能段列表。

        策略：按【】分拆伤害公式，从括号内容提取倍率+属性，
        从括号前的"段特定上下文"提取命中数（仅当前段的前文，跨越段不干扰）。
        """
        segments = []
        desc = skill_desc.strip()

        bracket_pattern = re.compile(r'[【\[]([^】\]]+)[】\]]')
        matches = list(bracket_pattern.finditer(desc))

        if not matches:
            for part in re.split(r'[\n;；、]', desc):
                part = part.strip()
                if not part:
                    continue
                seg = SkillParser._parse_single(part, part)
                if seg:
                    segments.append(seg)
            return segments

        for idx, match in enumerate(matches):
            bracket_content = match.group(1).strip()
            # 段特定上下文：上一个括号结束到当前括号开始之间的文本
            prev_end = matches[idx - 1].end() if idx > 0 else 0
            context = desc[prev_end:match.start()]
            full_text = context + ' ' + bracket_content

            seg = SkillParser._parse_single(full_text, bracket_content)
            if seg:
                segments.append(seg)

        return segments

    @staticmethod
    def _parse_single(full_text: str, core_text: str) -> Optional[Dict[str, Any]]:
        """解析单段伤害公式。"""
        multiplier_info = SkillParser._extract_multiplier(core_text)
        if not multiplier_info:
            return None

        attrs = multiplier_info['attributes']
        dmg_type = _infer_damage_type(''.join(attrs))
        hit_count = SkillParser._extract_hit_count(full_text)
        weapon_type = SkillParser._extract_weapon_type(core_text, dmg_type)
        if weapon_type in ('', dmg_type):
            # 括号内无具体弹种时，看段上下文是否明确写出（高平两用炮/三式弹/近战，写了才算）
            written = detect_written_weapon(full_text)
            if written:
                weapon_type = written

        # 使用 multiplier_info 中的 expression 作为 raw_text（倍率表达式）
        raw_text = multiplier_info.get('expression', core_text)

        return {
            'damage_type': dmg_type,
            'weapon_type': weapon_type,
            'multiplier': multiplier_info['multiplier'],
            'attributes': attrs,
            'hit_count': hit_count,
            'raw_text': raw_text,
        }

    @staticmethod
    def _extract_multiplier(text: str) -> Optional[Dict[str, Any]]:
        """提取倍率表达式。"""
        patterns = [
            (r'最多\s*(\d+(?:\.\d+)?)%?\s*\*\s*[（(]([^)）]+)[)）]', True),
            (r'(\d+(?:\.\d+)?)%?\s*\*\s*[（(]([^)）]+)[)）]', True),
            (r'最多\s*(\d+(?:\.\d+)?)%?\s*\*\s*(\S+)', False),
            (r'(\d+(?:\.\d+)?)%?\s*\*\s*(\S+)', False),
        ]

        for pattern, has_paren in patterns:
            match = re.search(pattern, text)
            if match:
                multiplier_str = match.group(1)
                attrs_str = match.group(2)

                multiplier = float(multiplier_str)
                if multiplier > 100:
                    multiplier = multiplier / 100.0

                # 构造原始表达式（保留 % 号和括号）
                if has_paren:
                    raw_expr = f"{multiplier_str}%*({attrs_str})"
                    attrs = [a.strip() for a in re.split(r'[+＋]', attrs_str)]
                else:
                    raw_expr = f"{multiplier_str}%*{attrs_str}"
                    raw = attrs_str.strip()
                    for suffix in ['的炮击伤害', '的雷击伤害', '的航空伤害', '的轰炸伤害',
                                   '的鱼雷伤害', '的对空伤害', '的近战伤害', '伤害']:
                        idx = raw.find(suffix)
                        if idx > 0:
                            raw = raw[:idx]
                            break
                    attrs = [raw]

                return {
                    'expression': raw_expr,
                    'multiplier': multiplier,
                    'attributes': attrs,
                }

        return None

    @staticmethod
    def _extract_hit_count(text: str) -> int:
        """提取命中次数。支持 x5 / X5 / ×5 / 5轮 / 5段 / 5发 / 5架 / N hit。"""
        match = re.search(r'[xX×]\s*(\d+)', text)
        if match:
            return int(match.group(1))

        match = re.search(r'(\d+)\s*hit', text)
        if match:
            return int(match.group(1))

        matches = list(re.finditer(r'(\d+)\s*[轮段发架]', text))
        if matches:
            return int(matches[-1].group(1))

        return 1

    @staticmethod
    def _extract_weapon_type(text: str, dmg_type: str) -> str:
        """提取武器类型。"""
        if dmg_type == '高平炮':
            return '高平炮'
        if '轰炸机' in text or dmg_type == '轰炸机':
            return '轰炸机'
        if '鱼雷机' in text or dmg_type == '鱼雷':
            return '雷击'
        if '战列炮' in text:
            return '战列炮'
        if '轻巡炮' in text:
            return '轻巡炮'
        return dmg_type