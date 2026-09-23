# -*- coding: utf-8 -*-
"""用户输入实体标准化（检索前预处理）：舰名 / BOSS 名的 昵称·谐音·错字 → 正规名。

问题背景
--------
「遭遇战阿斯莫德配装配队有吗」检索落空，根因之一是**实体名与知识库不一致**：
知识库该 BOSS 记为「阿丝莫德」（拉古兹.json），用户打成「阿斯莫德」（丝/斯）。
实体名一错，向量检索与关键词检索都对齐不上，后面再好的排序也救不回来。

本模块在检索前把输入里的实体原地改写为正规名，使 query 与知识库用词对齐。
（查询期标准化 → **无需重建向量库、无需重新嵌入**。）

安全策略（沿用项目口径：宁可解析不出，不硬凑）
--------------------------------------------
1. 硬别名表（ship_nicknames.json + boss_nicknames.json）→ 无条件替换为正规名；
2. 已是正规名 → 原样保留（不产生改写）；
3. 拼音【精确等值】（同音字，如 丝/斯 均为 sī）：
     · BOSS：允许（仅 11 个罕见名，碰撞风险极低）；
     · 舰名：仅当拼音长度 ≥ _SHIP_PINYIN_MIN（长名罕用；短名如「大凤」与常见词
       「大风」同音，盲目纠正会误改正常语句）；
4. 拼音【编辑距离 ≤ 阈值】（近音错字，如 亚/阿）：
     · 仅 BOSS；舰名不做（同上的误改风险）。

环境开关
--------
    AKALI_ENTITY_FUZZY_SHIP=0   关闭舰名的同音纠正（只保留硬别名）

用法
----
    from core.entity_norm import standardize
    standardize('遭遇战阿斯莫德配装配队有吗')   # → '遭遇战阿丝莫德配装配队有吗'
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Optional, Tuple

# 允许直接以脚本运行自检（python core/entity_norm.py）：把 通用工具 加入 sys.path
if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.name_service import _levenshtein, _normalize, _pinyin_key, load_nicknames

__all__ = ["standardize", "canonical_terms", "scan", "EntityNormalizer",
           "build_matcher", "boss_names"]

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC_DIR = os.path.join(_PROJ_ROOT, '知识库', '底层数据', '源文件')
_DATA_JSON = os.path.join(_PROJ_ROOT, '知识库', '底层数据', '产物', 'data.json')
_BOSS_JSON = os.path.join(_SRC_DIR, '拉古兹.json')
_BOSS_NICK_JSON = os.path.join(_SRC_DIR, 'boss_nicknames.json')
_SHIP_NICK_JSON = os.path.join(_SRC_DIR, 'ship_nicknames.json')

# 舰名同音纠正的最小拼音长度（低于此不纠正，避开常见词误改）
_SHIP_PINYIN_MIN = 7
# 单字不参与（项目既有口径：单字昵称/单字名在长句里极易误命中）
_MIN_LEN = 2

_cache_key = None
_cache_norm = None


def _mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _flag(name: str, default: bool = True) -> bool:
    v = (os.environ.get(name) or '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'on', 'yes')


def _load_ship_names() -> List[str]:
    """正规舰名 = 底层 data.json 的键（查询/计算同源）。"""
    try:
        with open(_DATA_JSON, encoding='utf-8') as f:
            raw = json.load(f)
        return [k for k in raw.keys() if k and not str(k).startswith('_')]
    except Exception:
        return []


def _load_boss_names() -> List[str]:
    """正规 BOSS 名 = 拉古兹.json 的 enemies[].name。"""
    try:
        with open(_BOSS_JSON, encoding='utf-8') as f:
            raw = json.load(f)
        return [e.get('name') for e in (raw.get('enemies') or []) if e.get('name')]
    except Exception:
        return []


def _load_alias_map(path: str) -> dict:
    """读 {正规名: [别名,…]} 形态的映射文件（过滤 _ 开头元键）。"""
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        return {k: list(v or []) for k, v in raw.items() if not str(k).startswith('_')}
    except Exception:
        return {}


class EntityNormalizer:
    """舰名 + BOSS 名 的实体标准化器（最长优先 + 从左到右扫描）。"""

    def __init__(self, ships: List[str], bosses: List[str],
                 ship_alias: dict, boss_alias: dict, fuzzy_ship: bool = True):
        self.ships = list(dict.fromkeys(ships))
        self.bosses = list(dict.fromkeys(bosses))
        self.ship_set = set(self.ships)
        self.boss_set = set(self.bosses)
        self.fuzzy_ship = fuzzy_ship

        # 硬别名：归一化别名 → 正规名（先塞舰名，再塞 BOSS；setdefault 保舰名优先）
        self.alias: dict = {}
        for a, std in ship_alias.items():
            na = _normalize(a)
            if na:
                self.alias.setdefault(na, std)
        for a, std in boss_alias.items():
            na = _normalize(a)
            if na:
                self.alias.setdefault(na, std)

        # 拼音精确索引：拼音键 → 正规名（collision 时保留更短的本名）
        self.pin: dict = {}
        for n in self.ships + self.bosses:
            k = _pinyin_key(_normalize(n))
            if not k:
                continue
            old = self.pin.get(k)
            if old is None or len(n) < len(old):
                self.pin[k] = n

        # BOSS 拼音预计算（编辑距离通道用，避免每次窗口重算）
        self.boss_pin = [(b, _pinyin_key(_normalize(b))) for b in self.bosses]
        self.boss_pin = [(b, p) for b, p in self.boss_pin if p]

        self.maxlen = max([len(n) for n in self.ships + self.bosses] + [_MIN_LEN])

    def _resolve(self, w: str) -> Optional[str]:
        """窗口 w 能解析出的正规名；解析不出返回 None。"""
        if not w or len(w) < _MIN_LEN:
            return None
        # 0) 已是正规名 → 原样（仍返回，用于"消费整个窗口"，避免内部再被短窗切开）
        if w in self.ship_set or w in self.boss_set:
            return w
        nw = _normalize(w)
        if not nw:
            return None
        # 1) 硬别名
        if nw in self.alias:
            return self.alias[nw]
        pk = _pinyin_key(nw)
        if not pk:
            return None
        # 2) 拼音精确等值（同音字）
        hit = self.pin.get(pk)
        if hit:
            if hit in self.boss_set:
                return hit
            if hit in self.ship_set and self.fuzzy_ship and len(pk) >= _SHIP_PINYIN_MIN:
                return hit
        # 3) 拼音编辑距离（仅 BOSS）。两道硬约束防误配：
        #    ① 长度必须相等 —— 否则「夕茉莉大」会因"删 2 字只花 2 代价"被吞成「夕茉莉」；
        #    ② 窗口须 ≥3 字 —— 2 字窗口拼音仅 5~6 长，编辑距离 1 噪声过大
        #       （实测「光荣与梦想」的「与梦」yumeng 会误配 雅蒙 yameng）。
        if len(nw) < 3:
            return None
        best, best_d = None, None
        for b, pn in self.boss_pin:
            if len(b) != len(nw):
                continue
            d = _levenshtein(pk, pn)
            tp = 1 if max(len(pk), len(pn)) <= 6 else 2
            if d <= tp and (best_d is None or d < best_d
                            or (d == best_d and best is not None and len(b) < len(best))):
                best, best_d = b, d
        return best

    def scan(self, text: str) -> Tuple[str, List[Tuple[str, str]]]:
        """最长优先 + 从左到右扫描，把命中的实体原地替换为正规名。
        返回 (新文本, [(原写法, 正规名), …])。"""
        if not text:
            return text, []
        out: List[str] = []
        hits: List[Tuple[str, str]] = []
        i, n = 0, len(text)
        while i < n:
            hit = None
            for L in range(min(self.maxlen, n - i), _MIN_LEN - 1, -1):
                w = text[i:i + L]
                r = self._resolve(w)
                if r:
                    hit = (w, r, L)
                    break
            if hit:
                w, r, L = hit
                out.append(r)
                if r != w:
                    hits.append((w, r))
                i += L
            else:
                out.append(text[i])
                i += 1
        return ''.join(out), hits


def _build() -> EntityNormalizer:
    """构建标准化器（mtime 感知缓存：数据文件更新后自动重建）。"""
    global _cache_key, _cache_norm
    key = (_mtime(_DATA_JSON), _mtime(_BOSS_JSON), _mtime(_BOSS_NICK_JSON), _mtime(_SHIP_NICK_JSON))
    if _cache_norm is not None and _cache_key == key:
        return _cache_norm
    ship_alias = {a: std for std, al in load_nicknames().items() for a in (al or []) if str(a).strip()}
    boss_alias = {a: std for std, al in _load_alias_map(_BOSS_NICK_JSON).items()
                  for a in (al or []) if str(a).strip()}
    norm = EntityNormalizer(_load_ship_names(), _load_boss_names(),
                            ship_alias, boss_alias,
                            fuzzy_ship=_flag('AKALI_ENTITY_FUZZY_SHIP', True))
    _cache_key, _cache_norm = key, norm
    return norm


def scan(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """返回 (标准化文本, 命中明细)。异常时原样返回、不抛出。"""
    if not text:
        return text, []
    try:
        return _build().scan(text)
    except Exception:
        return text, []


def standardize(text: str) -> str:
    """把输入里的 舰名/BOSS 名 昵称·谐音·错字 替换为正规名；无命中则原样返回。"""
    return scan(text)[0]


def canonical_terms(text: str) -> List[str]:
    """输入中识别到的正规实体名（去重保序；供检索增强追加）。"""
    try:
        return list(dict.fromkeys(r for _, r in scan(text)[1]))
    except Exception:
        return []


# ---------------- 供其它模块使用的公开入口（避免直接依赖下划线私有名） ----------------
def build_matcher() -> EntityNormalizer:
    """取（带缓存的）标准化器实例。"""
    return _build()


def boss_names() -> List[str]:
    """正规 BOSS 名列表（来自 拉古兹.json 的 enemies[].name）。"""
    try:
        return _build().bosses
    except Exception:
        return []


if __name__ == '__main__':
    for t in ('遭遇战阿斯莫德配装配队有吗', '阿斯塔露怎么打', '加波配队', '艾塞克斯和埃塞克斯',
              '今天风很大，大风天气', '列拉金有毒液坑吗'):
        print(t, '->', standardize(t))
