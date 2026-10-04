# -*- coding: utf-8 -*-
"""舰名昵称/名字映射统一服务（唯一数据源 + 唯一加载/匹配入口）。

数据：知识库/底层数据/ship_nicknames.json
  schema: { 正规名: [别名, ...], _comment: "..." }

本模块只依赖标准库，无任何业务模块依赖，可被任意模块安全导入。

功能：
- load_nicknames(): 带 mtime 缓存的完整加载（过滤 _ 开头元键）
- nick_variant(s): 昵称变体键（去空白 + 小写，保留括号编号，避免归一化去括号冲突）
- build_hard_map(): 硬映射 {别名(含变体): 正规名}，优先级最高
- hard_lookup(name): 查硬映射（含变体），未命中返回 None
- nicknames_of(std): 查询某正规名对应的别名列表
- NameMatcher: 分层匹配器（精确/归一化/别名/子串/编辑距离）
"""
import json
import os
import re
from typing import Optional, List

# 昵称数据路径（基于项目根目录）
_NICKNAMES_REL = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', '知识库', '底层数据', '源文件', 'ship_nicknames.json'))

DEFAULT_NICKNAMES_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', '..', '知识库', '底层数据', '源文件', 'ship_nicknames.json'))

_nick_cache = None
_nick_mtime = 0.0
_hard_cache = None
_hard_mtime = 0.0


def _file_mtime() -> float:
    try:
        return os.path.getmtime(DEFAULT_NICKNAMES_PATH)
    except OSError:
        return 0.0


def load_nicknames() -> dict:
    """加载昵称映射 {正规名: [别名, ...]}（mtime 感知缓存，过滤 _ 开头元键）。"""
    global _nick_cache, _nick_mtime
    m = _file_mtime()
    if _nick_cache is not None and m == _nick_mtime:
        return _nick_cache
    data = {}
    if m:
        try:
            with open(DEFAULT_NICKNAMES_PATH, encoding='utf-8') as f:
                raw = json.load(f)
            data = {k: list(v or []) for k, v in raw.items() if not str(k).startswith('_')}
        except Exception:
            data = {}
    _nick_cache, _nick_mtime = data, m
    return data


def nick_variant(s: str) -> str:
    """昵称变体键：仅去空白 + 小写（保留括号，避免 (DD-459)/(DD-724) 归一化冲突）。"""
    if not s:
        return ''
    return re.sub(r'\s+', '', s).lower()


def build_hard_map() -> dict:
    """硬映射表：{别名(含变体): 正规名}。mtime 感知缓存。"""
    global _hard_cache, _hard_mtime
    m = _file_mtime()
    if _hard_cache is not None and m == _hard_mtime:
        return _hard_cache
    hard = {}
    if m:
        for std, aliases in load_nicknames().items():
            for a in aliases or []:
                key = str(a).strip()
                if not key:
                    continue
                hard[key] = std
                v = nick_variant(key)
                if v and v != key:
                    hard[v] = std
    _hard_cache, _hard_mtime = hard, m
    return hard


def hard_lookup(name: str):
    """查硬映射（含变体展开），命中返回正规名，未命中返回 None。"""
    if not name:
        return None
    n = str(name).strip()
    hard = build_hard_map()
    if n in hard:
        return hard[n]
    v = nick_variant(n)
    if v and v in hard:
        return hard[v]
    return None


def nicknames_of(std: str) -> list:
    """查询正规名对应的别名列表（无则空列表）。"""
    if not std:
        return []
    return list(load_nicknames().get(str(std).strip(), []) or [])


def _normalize(name: str) -> str:
    """归一化舰名：全角→半角、去间隔号/空格/括号、转小写"""
    if not name:
        return ''
    name = _to_halfwidth(name)
    name = re.sub(r'[·‧.、，．\.]', '', name)
    name = re.sub(r'[\(（].*?[\)）]', '', name)
    name = re.sub(r'\s+', '', name)
    return name.lower()


# 全角 ASCII 区（！～）与全角空格 → 半角
_FW_TABLE = {0xFF01 + i: 0x21 + i for i in range(0x5E)}
_FW_TABLE[0x3000] = 0x20


def _to_halfwidth(s: str) -> str:
    """全角字母/数字/符号/空格 → 半角（如 Ｚ１６→Z16）"""
    return s.translate(_FW_TABLE)


def _levenshtein(a: str, b: str) -> int:
    """编辑距离（DP 实现）"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1)))
        prev = cur
    return prev[-1]


class NameMatcher:
    """舰名分层匹配器

    匹配优先级（从高到低）：
    1. 精确匹配：query == ship_name
    2. 归一化匹配：全角→半角、去间隔号/空格/括号 + 转小写后等值
    3. 别名表匹配：query 命中 ship_nicknames.json 的某个别名
    4. 子串匹配：query 是某个 ship_name 的子串（唯一命中时直接返回）
    5. 编辑距离匹配：Levenshtein **字符**通道（短名 <=1，长名 <=2），取最小者

    口径变更（2026-10-04）：**移除全部「拼音」通道（谐音）** —— 同音/近音匹配只看
    音不看字形，会把「想喝」(xianghe) 误纠成「翔鹤」(xianghe) 这类日常词。
    现只保留字形层面的别名、子串、字符编辑距离（**错字**）匹配。
    """

    def __init__(self, all_names: List[str], nicknames_path: str = None):
        self.all_names = list(all_names)
        self.nicknames = self._load_nicknames(nicknames_path)
        # 预计算归一化索引 {normalized_name: ship_name}
        # 碰撞策略：键相同（如 拉菲 与 拉菲(DD-724) 归一化同键）时保留更短的本名
        self._norm_index = {}
        for n in self.all_names:
            nk = _normalize(n)
            if not nk:
                continue
            old = self._norm_index.get(nk)
            if old is None or len(n) < len(old):
                self._norm_index[nk] = n
        # 反向别名索引 {normalized_alias: 正规名}
        self._alias_index = {}
        for std, aliases in self.nicknames.items():
            for a in aliases:
                self._alias_index.setdefault(_normalize(a), std)

    def _load_nicknames(self, path: str) -> dict:
        if not path or not os.path.exists(path):
            return load_nicknames()
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith('_')}

    def match_alias_exact(self, query: str) -> Optional[str]:
        """只走 精确/归一化/昵称表 三层匹配（不做子串与编辑距离模糊）。"""
        if not query:
            return None
        q = query.strip()
        if q in self.all_names:
            return q
        nq = _normalize(q)
        if not nq:
            return None
        if nq in self._norm_index:
            return self._norm_index[nq]
        if nq in self._alias_index:
            std = self._alias_index[nq]
            if std in self.all_names:
                return std
            nstd = _normalize(std)
            if nstd in self._norm_index:
                return self._norm_index[nstd]
        return None

    def match(self, query: str) -> Optional[str]:
        """匹配舰名，返回命中的正规舰名或 None"""
        if not query:
            return None
        q = query.strip()
        # 1. 精确匹配
        if q in self.all_names:
            return q
        nq = _normalize(q)
        if not nq:
            return None
        # 2. 归一化精确匹配
        if nq in self._norm_index:
            return self._norm_index[nq]
        # 3. 别名表匹配
        if nq in self._alias_index:
            std = self._alias_index[nq]
            if std in self.all_names:
                return std
            nstd = _normalize(std)
            if nstd in self._norm_index:
                return self._norm_index[nstd]
        # 4. 子串匹配（唯一命中时返回）
        candidates = [n for n in self.all_names if nq in _normalize(n)]
        if len(candidates) == 1:
            return candidates[0]
        # 5. 编辑距离匹配：仅 **字符** 通道（错字纠正），不做拼音（谐音）。
        #    全部拼音通道已移除：见类文档（同音/近音会把「想喝」误纠「翔鹤」等）。
        best, best_d = None, None
        for n in self.all_names:
            nn = _normalize(n)
            if not nn:
                continue
            d = _levenshtein(nq, nn)
            threshold = 1 if max(len(nq), len(nn)) <= 4 else 2
            if d <= threshold and (best_d is None or d < best_d
                                   or (d == best_d and len(n) < len(best))):
                best, best_d = n, d
        return best
