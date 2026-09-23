# -*- coding: utf-8 -*-
"""
舰名规范化模块

提供两个层次的处理：
- canonicalize_name(name): 输出存储用的正规舰名
    1. 各类间隔号统一为半角中点 '·'
    2. 全角括号→半角、去括号备注（取第一个括号前部分，如 "丹阳(雪风·改)"→"丹阳"）
    3. 应用 ship_name_map.json 修正错别字
    4. 保留大小写（存储名保留原大小写，如 "Z1"）

- match_key(name): 输出合并用的匹配键
    在 canonicalize_name 基础上转小写、去所有间隔号/空格，用于三源数据合并去重。
"""
import json
import os
import re

_INTERVAL_CHARS = ['‧', '・', '·', '∙', '⋅']
_BRACKET_RE = re.compile(r'[\(（].*?[\)）]')

_DEFAULT_MAP_PATH = os.path.join(os.path.dirname(__file__), '..', 'config', 'ship_name_map.json')
_name_map_cache = None


def load_name_map(path: str = _DEFAULT_MAP_PATH) -> dict:
    """加载舰名错别字映射表（带缓存）"""
    global _name_map_cache
    if _name_map_cache is not None:
        return _name_map_cache
    if not os.path.exists(path):
        _name_map_cache = {}
        return _name_map_cache
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    _name_map_cache = {k: v for k, v in raw.items() if not k.startswith('_')}
    return _name_map_cache


# 昵称/简称表（与阿卡莉 agent 共用同一张：知识库/底层数据/源文件/ship_nicknames.json）
# build/src 向上三级 = 阿卡莉1.0 根目录
_NICKNAMES_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..',
    '知识库', '底层数据', '源文件', 'ship_nicknames.json'))
_nicknames_cache = None
_nicknames_mtime = None


def load_nicknames() -> dict:
    """加载昵称表（正规名 → 别名列表），mtime 感知缓存。"""
    global _nicknames_cache, _nicknames_mtime
    try:
        mtime = os.path.getmtime(_NICKNAMES_FILE)
    except OSError:
        return {}
    if _nicknames_cache is None or _nicknames_mtime != mtime:
        try:
            with open(_NICKNAMES_FILE, encoding='utf-8') as f:
                _nicknames_cache = json.load(f)
            _nicknames_mtime = mtime
        except (OSError, ValueError):
            _nicknames_cache = {}
            _nicknames_mtime = mtime
    return _nicknames_cache


def nickname_to_canonical(name: str) -> str:
    """别名 → 正规名（如 花生→华盛顿、太太→列克星敦、雪风改→丹阳）；未命中返回原输入。"""
    if not name:
        return name
    for canonical, aliases in (load_nicknames() or {}).items():
        if canonical == '_comment':
            continue
        if name in aliases:
            return canonical
    return name


def canonicalize_name(name: str) -> str:
    """正规化舰名：间隔号统一为 '·' + 去括号备注（保留舰船编号括号如 (DD-459)）+ 应用错别字映射。保留大小写。"""
    if not name:
        return ''
    # 1. 间隔号统一为 '·'
    for ch in _INTERVAL_CHARS:
        name = name.replace(ch, '·')
    # 2. 全角括号→半角、全角空格→半角
    name = name.replace('（', '(').replace('）', ')')
    name = name.replace('\u3000', ' ')
    # 3. 去括号备注，但保留舰船编号括号（如 拉菲(DD-459) / 列克星敦(CV-16)），
    #    否则 拉菲(DD-459) 与 拉菲(DD-724) 会被合并成同一角色
    if '(' in name:
        keep_ids = re.findall(r'\((?:DD|CV|SS|BB|CL|CA|DDG|CVL|BBV)-?\d+\)', name)
        # 去掉所有括号，再把编号括号加回来（保持原顺序）
        base = _BRACKET_RE.sub('', name).strip()
        if keep_ids:
            name = base + ''.join(keep_ids)
        else:
            name = base
    # 4. 应用错别字映射（在间隔号统一之后做，保证映射表用统一的 '·' 书写）
    name_map = load_name_map()
    name = name_map.get(name, name)
    # 5. 昵称/简称反向映射（与 agent NameMatcher 共用 ship_nicknames.json）：
    #    花生→华盛顿、太太→列克星敦、雪风改→丹阳、拉菲DD724→拉菲(DD-724) 等
    name = nickname_to_canonical(name)
    return name


def match_key(name: str) -> str:
    """合并用匹配键：canonicalize + 去间隔号/空格 + 转小写（保留舰船编号括号，避免 DD-459/DD-724 合并冲突）。"""
    if not name:
        return ''
    canon = canonicalize_name(name)
    canon = re.sub(r'[·‧.、，．\.]', '', canon)
    canon = re.sub(r'\s+', '', canon)
    return canon.lower()
