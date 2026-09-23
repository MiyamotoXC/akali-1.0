# -*- coding: utf-8 -*-
"""舰名昵称/简称归一化（数据管理工具侧，与阿卡莉 agent 共用同一张表）。

表：知识库/底层数据/ship_nicknames.json（正规名 → 别名列表），
agent 侧 src/name_matcher.py 读同一份，此处不重复维护映射，只做读取与反向查询。

normalize_ship_name(name, raw_storage)：
    任意输入（正规名 / ·改 / 别名 / 表名'原名/昵称'）→ raw 正规舰名键。
    命中顺序：原名直查 → 去「·改」→ 去括号编号 → 昵称表反向（别名→正规名→raw）→ manual 兜底。
"""
import json
import os
import re

NICKNAMES_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '知识库', '底层数据', 'ship_nicknames.json'))

_cache = None
_cache_mtime = None


def _load_nicknames() -> dict:
    """带 mtime 感知的缓存读取（与 agent 侧 name_service 一致，避免重复读盘）。"""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(NICKNAMES_FILE)
    except OSError:
        return {}
    if _cache is None or _cache_mtime != mtime:
        try:
            with open(NICKNAMES_FILE, encoding='utf-8') as f:
                _cache = json.load(f)
            _cache_mtime = mtime
        except (OSError, ValueError):
            _cache = {}
            _cache_mtime = mtime
    return _cache


def _strip_bracket(name: str) -> str:
    return re.sub(r'[（(].*$', '', name or '').strip()


def normalize_ship_name(name, raw_storage: dict, manual: dict = None) -> str:
    """归一化到 raw 正规舰名键；找不到返回 ''。manual 为调用方旧逻辑兜底字典。"""
    name = (name or '').strip()
    if not name:
        return ''
    main = name.split('/')[0].strip()  # 兼容 '原名/昵称' 表名格式
    candidates = []
    for cand in (main, name):
        for c in (cand, cand.replace('·改', ''), _strip_bracket(cand)):
            if c and c in raw_storage and c not in candidates:
                candidates.append(c)
    if candidates:
        return candidates[0]
    # 昵称表反向映射：别名 → 正规名 → raw（正规名再兼容 去·改/去括号）
    for canonical, aliases in (_load_nicknames() or {}).items():
        if canonical == '_comment':
            continue
        if main in aliases or name in aliases:
            for c in (canonical, canonical.replace('·改', ''), _strip_bracket(canonical)):
                if c and c in raw_storage:
                    return c
    # manual 兜底（保留各调用方旧的特殊映射）
    if manual and main in manual and manual[main] in raw_storage:
        return manual[main]
    return ''
