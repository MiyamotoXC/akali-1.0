# -*- coding: utf-8 -*-
"""session tools."""
import copy
import json
import re
from qwen_agent.tools.base import BaseTool

from .common import (
    _PANEL_BUFF_FIELDS, _DIRECT_BUFF_FIELDS, _SIZE_BUFF_ALIASES,
    _SHIP_TYPE_BUFF_ALIASES, _BUFF_PAIR_RE, _normalize_buff_pair,
    _parse_buff_value, _extract_buff_duration, _parse_buff_str,
)

# ======================================================================
# 工具 7-9：会话级临时记忆（buff/面板/敌方）
# ======================================================================

class SetSessionBuff(BaseTool):
    """批量设置会话级临时 buff。传入完整 buff 字符串，支持多字段、百分比、负数。"""
    name = 'set_session_buff'
    description = '批量设置会话级临时 buff。传入完整 buff 字符串（如"全伤害+15%、暴击率+0.2"），支持顿号/逗号/空格分隔，数值支持百分比(20%)、小数(0.2)、负数(-15%)。字段名用简化名（不带"增加"）。无法识别的字段会提示重新填空。'
    parameters = {
        'type': 'object',
        'properties': {
            'buff_str': {
                'type': 'string',
                'description': 'buff 字符串，如「全伤害+15%、炮击伤害-20%、暴击率+0.2」。字段名用简化名（不带"增加"）。',
            },
        },
        'required': ['buff_str'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        session_overrides = self.cfg['session_overrides']
        buff_str = params['buff_str']
        if not buff_str.strip():
            return "buff_str 不能为空。"
        # 「持续N秒」：提取持续窗口（buff 仅在该时间窗口内生效，供总伤害时间轴模拟消费）
        clean_str, duration = _extract_buff_duration(buff_str)
        buff_str = clean_str.strip()
        if not buff_str:
            return "buff_str 不能为空（仅识别到「持续N秒」，请同时输入 buff 字段）。"
        successes, errors = _parse_buff_str(buff_str)
        # 只要存在无法识别的字段 → 不记忆任何 buff、清空已记 buff，重复上一步骤（用户重新填空）
        if errors:
            session_overrides.pop("buffs", None)
            session_overrides.pop("ship_type_damage_bonus", None)
            session_overrides.pop("ship_type_damage_reduction", None)
            return f"buff 字段无法识别: {', '.join(errors)}。已清空本轮 buff 记忆，请重新填空输入（字段名用简化名，如 全伤害、炮击伤害、暴击率，不带\"增加\"）。"
        # 带持续 → 存入 buff_windows（时间轴窗口，战斗开始即生效，持续 N 秒）
        if duration > 0:
            window_buffs = {}
            for storage, field, value in successes:
                if storage == 'buff':
                    window_buffs[field] = window_buffs.get(field, 0.0) + value
            if window_buffs:
                windows = session_overrides.setdefault("buff_windows", [])
                # 同字段覆盖式：新窗口覆盖旧窗口
                windows[:] = [w for w in windows if w.get('source') != 'user']
                windows.append({'source': 'user', 'duration': duration, 'buffs': window_buffs})
                # 正值不带 + 号、负值保留 - 号（避免 增加+-10% 奇怪格式）
                desc = '、'.join(f"{k}{v:g}" for k, v in window_buffs.items())
                return f"已记忆临时 buff 窗口（战斗开始起 {duration:g} 秒内生效）: {desc}。总伤害时间轴模拟将按窗口计算。"
            return "未解析到有效 buff 字段。"
        session_overrides.setdefault("buffs", {})
        session_overrides.setdefault("ship_type_damage_bonus", {})
        session_overrides.setdefault("ship_type_damage_reduction", {})
        msgs = []
        for storage, field, value in successes:
            if storage == 'buff':
                cur = session_overrides["buffs"].get(field, 0.0)
                session_overrides["buffs"][field] = cur + value
                msgs.append(f"{field}={session_overrides['buffs'][field]}")
            elif storage == 'ship_bonus':
                cur = session_overrides["ship_type_damage_bonus"].get(field, 0.0)
                session_overrides["ship_type_damage_bonus"][field] = cur + value
                msgs.append(f"对{field}伤害增加={value}")
            elif storage == 'ship_reduction':
                cur = session_overrides["ship_type_damage_reduction"].get(field, 0.0)
                session_overrides["ship_type_damage_reduction"][field] = cur + value
                msgs.append(f"对{field}伤害减免={value}")
        return f"已记忆临时 buff: {', '.join(msgs)}。"


class SetCalcContext(BaseTool):
    """设置本轮伤害计算的攻击方与敌方角色名。设置后，后续所有伤害计算工具会自动使用这两个角色，无需重复指定。直到用户说「结束本轮计算」或调用 clear_session_overrides。"""
    name = 'set_calc_context'
    description = '设置本轮伤害计算的攻击方、敌方角色名及航向/制空。设置后后续计算自动使用。'
    parameters = {
        'type': 'object',
        'properties': {
            'attacker_name': {'type': 'string', 'description': '攻击方角色名（支持昵称/简称/谐音）'},
            'enemy_name': {'type': 'string', 'description': '敌方角色名（可空，不填则输出原始伤害）'},
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势（默认同航）'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失（默认均势）'},
        },
        'required': ['attacker_name'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        session_overrides = self.cfg['session_overrides']
        app = self.cfg['app']

        # 验证攻击方角色是否存在
        attacker = app.get_ship_from_all(params['attacker_name'])
        if attacker is None:
            return f"未找到攻击方角色: {params['attacker_name']}。"
        # enemy_name 可空：不填则后续计算输出原始伤害
        enemy_name = params.get('enemy_name', '')
        if enemy_name:
            enemy_data = app.get_ship_from_all(enemy_name)
            if enemy_data is None:
                return f"未找到敌方角色: {enemy_name}。"

        # 写入 calc_context（含航向/制空，默认同航/均势）
        from .common import _normalize_course
        course = _normalize_course((params.get('course') or '同航').strip() or '同航')
        air = (params.get('air') or '均势').strip() or '均势'
        session_overrides['calc_context'] = {
            'attacker': params['attacker_name'],
            'enemy': enemy_name,
            'course': course,
            'air': air,
            'active': True,
        }
        enemy_desc = f" → 敌方 {enemy_name}" if enemy_name else "（无防守方，输出原始伤害）"
        return f"已设置本轮计算：攻击方 {params['attacker_name']}{enemy_desc}（航向 {course}，制空 {air}）。后续计算无需重复指定角色。如需修改请重新调用本工具；结束本轮请输入「结束本轮计算」。"

