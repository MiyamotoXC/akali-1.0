# -*- coding: utf-8 -*-
"""
技能解析模块（qwen_agent / openai SDK 版）

仅负责"技能表"的 LLM 解析：把技能描述行解析成结构化 JSON。
面板与普攻由 file_extractor 代码确定，不走 LLM。

分块策略：>15 个角色分批，每块调 chat_json 拿 JSON，避免单次输出 token 超限。
"""
import json
import re

from .模型客户端 import chat_json
from .提示词 import build_skill_parse_prompt


def _normalize_for_match(name: str) -> str:
    """LLM 输出匹配用归一化：去间隔号/空格/括号、转小写。"""
    if not name:
        return ''
    name = re.sub(r'[·‧.、，．\.]', '', name)
    name = re.sub(r'[\(（].*?[\)）]', '', name)
    name = re.sub(r'\s+', '', name)
    return name.lower()


def _normalize_skill_field(field, with_cd: bool = True) -> dict:
    """补全技能字段结构：{cd, parts: [{hit, multiplier, damage_type}, ...]}；旗舰 with_cd=False 无 cd"""
    default = {"cd": 0.0, "parts": []} if with_cd else {"parts": []}
    if not isinstance(field, dict):
        return dict(default)
    out = dict(default)
    if with_cd:
        try:
            out['cd'] = float(field.get('cd', 0.0) or 0.0)
        except (ValueError, TypeError):
            out['cd'] = 0.0
    raw_parts = field.get('parts', [])
    if isinstance(raw_parts, list):
        for p in raw_parts:
            if not isinstance(p, dict):
                continue
            part = {"hit": 1, "multiplier": "", "damage_type": ""}
            try:
                part['hit'] = int(p.get('hit', 1) or 1)
            except (ValueError, TypeError):
                part['hit'] = 1
            part['multiplier'] = str(p.get('multiplier', '') or '')
            part['damage_type'] = str(p.get('damage_type', '') or '')
            # 透传 weapon_type（高平炮等，用于克制修正查表；空则忽略）
            wt = str(p.get('weapon_type', '') or '').strip()
            if wt:
                part['weapon_type'] = wt
            # 透传点燃配置（点燃段：damage_type=点燃 时带 ignite）
            ig = p.get('ignite')
            if isinstance(ig, dict):
                try:
                    part['ignite'] = {
                        'count': int(ig.get('count', 1) or 1),
                        'prob': float(ig.get('prob', 1.0) or 1.0),
                        'dps': float(ig.get('dps', 0) or 0),
                        'duration': float(ig.get('duration', 0) or 0),
                        'cd': float(ig.get('cd', 0) or 0),
                    }
                except (ValueError, TypeError):
                    part.pop('ignite', None)
            # 跳过空段（无倍率表达式的段不进入计算；点燃段无 multiplier 但带 ignite 需保留）
            if part['multiplier'] or part.get('ignite'):
                out['parts'].append(part)
    return out


def _normalize_skill_buffs(raw) -> list:
    """归一化 skill_buffs 字段：{trigger, target, duration, buffs} 校验。

    仅保留 trigger/target/duration/buffs 均合法的条目，数值转 float。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        trigger = str(item.get('trigger', '') or '').strip()
        target = str(item.get('target', '') or '').strip() or 'self'
        if trigger not in ('skill', 'burst', 'support', 'flagship'):
            continue
        if target not in ('self', 'enemy'):
            target = 'self'
        try:
            duration = float(item.get('duration', 0) or 0)
        except (ValueError, TypeError):
            continue
        if duration <= 0:
            continue
        buffs = item.get('buffs')
        if not isinstance(buffs, dict) or not buffs:
            continue
        nb = {}
        for k, v in buffs.items():
            try:
                nb[str(k)] = float(v)
            except (ValueError, TypeError):
                pass
        if nb:
            out.append({'trigger': trigger, 'target': target, 'duration': duration, 'buffs': nb})
    return out


def parse_skills_batch(skill_rows: list, user_instruction: str = "", chunk_size: int = 8):
    """分块调 LLM 解析技能表行数据。

    输入：skill_rows = [{Name, LeaderSkill, SupportSkill, BattleSkill1, BattleSkill2}, ...]
    输出：(list[(舰名, skill_dict)], error_str)
        - skill_dict 含 skill_attack/burst_attack/support_attack/flagship_attack/buffs/skill_buffs
        - error_str 为 None 表示全部成功；非 None 表示部分批次失败（仍返回已解析的）
    """
    if not skill_rows:
        return None, "技能表数据为空"

    sys_prompt = build_skill_parse_prompt(user_instruction)
    all_out = []
    errs = []

    total = len(skill_rows)
    for i in range(0, total, chunk_size):
        chunk = skill_rows[i:i + chunk_size]
        user_content = json.dumps(chunk, ensure_ascii=False, indent=2)
        try:
            result = chat_json(sys_prompt, user_content)
        except Exception as e:
            errs.append(f"第{i // chunk_size + 1}批({len(chunk)}个)LLM解析失败: {e}")
            continue

        # 兼容 {"characters": [...]} 和单角色 {...} 两种返回
        if isinstance(result, dict) and 'characters' in result:
            characters = result['characters']
        elif isinstance(result, list):
            characters = result
        elif isinstance(result, dict):
            characters = [result]
        else:
            errs.append(f"第{i // chunk_size + 1}批返回格式异常")
            continue

        # 按 Name 归一化索引 LLM 输出（容忍间隔号/大小写差异）
        llm_by_key = {}
        for char in characters:
            if not isinstance(char, dict):
                continue
            name = str(char.get('name', '') or '').strip()
            if name:
                llm_by_key[_normalize_for_match(name)] = char

        # 按输入行顺序匹配，使用输入行的 Name（正规名）作为返回 key
        for row in chunk:
            input_name = str(row.get('Name', '') or '').strip()
            if not input_name:
                continue
            char = llm_by_key.get(_normalize_for_match(input_name))
            if char is None:
                errs.append(f"LLM 未返回角色: {input_name}")
                continue
            skill_dict = {
                'skill_attack': _normalize_skill_field(char.get('skill_attack')),
                'burst_attack': _normalize_skill_field(char.get('burst_attack')),
                'support_attack': _normalize_skill_field(char.get('support_attack')),
                'flagship_attack': _normalize_skill_field(char.get('flagship_attack'), with_cd=False),
                'buffs': char.get('buffs', {}) if isinstance(char.get('buffs'), dict) else {},
                'skill_buffs': _normalize_skill_buffs(char.get('skill_buffs')),
            }
            all_out.append((input_name, skill_dict))

    if not all_out:
        return None, "所有批次解析失败: " + "; ".join(errs)
    if errs:
        return all_out, "部分批次失败（已解析 " + str(len(all_out)) + " 个）: " + "; ".join(errs)
    return all_out, None
