# -*- coding: utf-8 -*-
"""小技巧结构化直答层（攻略检索的「结构化优先」一级）。

定位
----
攻略检索的分层（本项目口径）：
    ① 归一化（core.entity_norm：舰名/BOSS 谐音错字 → 正规名）
    ② 结构化确定命中（**本模块**：小技巧键命中 → 原文直出）
    ③ 语义召回兜底（tools.knowledge 攻略QA库 RAG）
    ④ 攻略面板兜底
本模块只做 ② —— **有键就直答，没键就返回 None**（让给 ③）。
「无键跳过」和「有键命中」同等重要：知识没有分类轴的问题（机制长文、问答表、跨篇综合）
本来就不该硬套结构化，硬套只会掉分。

为什么需要它
------------
`知识库/攻略/tips.json` 是**结论式短条目**（平均 70 余字），每条自带
`category / title / tags`，天然可做确定性检索：命中即原文直出，**不调 LLM、不会编造、零延迟**。

匹配策略（宁可不命中，不硬套）
------------------------------
- 归一：`entity_norm.standardize` 让「阿斯莫德」对齐标签里的「阿丝莫德」；key 化时去空白与标点。
- **A 档（标题）**，任一命中即采用，分数高于 B 档：
    A1 key 完全相同
    A2 标题包含于输入（标题 ≥3 字，覆盖「甲弹克制关系是什么」这类后缀问法）
    A3 输入包含于标题（输入 ≥4 字，覆盖「温泉搓澡」「18-7」这类简短指代）
- **B 档（标签）**：仅认**≥4 字的具体标签**（如 巴巴托斯 / 多倍经验 / 钻石商店），
  2~3 字泛词（装备 / 天赋 / 循环 / 防守）一律忽略——它们是同义噪声，命中只会把答案锁窄。
- **并列**：同分条目 ≤3 条时一并给出（如「多倍经验」同时命中囤油与退役技巧）；
  超过 3 条视为歧义，**放弃直答**，回落语义召回。

数据只读，不写知识库（知识库由 开发者入口/build 维护）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TIPS_JSON = os.path.join(_PROJ_ROOT, '知识库', '攻略', 'tips.json')

_A2_MIN = 3          # 标题包含于输入：标题最短长度
_A3_MIN = 3          # 输入包含于标题：输入最短长度（纯 ASCII 时要求更长，见 _qualifies）
_TAG_MIN = 4         # 具体标签的最短长度
_MAX_ITEMS = 3       # 并列命中上限；超过视为歧义 → 放弃直答

_PUNCT_RE = re.compile(r'[\s，。！？、；：,.!?;:～·—_（）()\[\]【】《》“”‘’「」…/]+')
# 尾部疑问短语：剥掉后再试 A 档，覆盖「温泉搓澡怎么弄」这类「条目名 + 疑问尾」问法。
# 只收不会破坏主体词形的尾词（不含「怎么打/怎么配/怎么玩」——那会把主体一起吃掉）。
_TAIL_RE = re.compile(r'(是什么|有什么用|有什么|有哪些|怎么办|怎么弄|怎么用|怎么获得|怎么获取|'
                      r'如何获得|如何获取|选谁|选哪个|在哪儿?|多少|吗|呢)+$')
_cache: Dict[str, object] = {}

__all__ = ['lookup', 'render', 'handle', 'categories', 'all_tips']


def _mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _key(s) -> str:
    """归一化检索键：小写 + 去空白与标点（A 档/B 档统一口径）。"""
    return _PUNCT_RE.sub('', str(s or '').lower())


def _qualifies(k: str) -> bool:
    """键是否够「具体」到可用于匹配。

    中文 3 字（舰名如「女灶神」「夕茉莉」、关卡/系统名如「练级点」）已经足够具体；
    而纯 ASCII 的 3 字符（pvp / mvp / box / aoe）太泛，必须 ≥4 字才算。"""
    if len(k) >= _TAG_MIN:
        return True
    return len(k) >= _A3_MIN and not k.isascii()


def _cat_keys() -> set:
    """分类名不作为标签使用：如「遭遇战」既是分类又是 t096 的标签，
    放任它匹配会把「遭遇战怎么配队」错误直答成「遭遇战练习模式」。"""
    return {_key(c) for c in categories() if c}


def _load() -> List[dict]:
    """读 tips.json（mtime 感知缓存）。"""
    mt = _mtime(_TIPS_JSON)
    if _cache.get('mt') == mt and _cache.get('tips') is not None:
        return _cache['tips']  # type: ignore[return-value]
    tips: List[dict] = []
    try:
        with open(_TIPS_JSON, encoding='utf-8') as f:
            tips = (json.load(f) or {}).get('tips') or []
    except Exception:
        tips = []
    _cache['mt'] = mt
    _cache['tips'] = tips
    return tips


def all_tips() -> List[dict]:
    """全部小技巧条目（只读，mtime 感知缓存）。

    与 lookup() 的「键命中直答」不同，本入口供**按内容找条目**的场景使用
    （如对比/评估处理器要捞出提及某几个舰灵的小技巧原文）。返回的 dict 与
    tips.json 同构：{id, category, title, content, source, tags}；读盘失败返回 []。
    """
    return _load()


def categories() -> List[str]:
    """全部小技巧分类（按 tips.json 的 categories 顺序）。"""
    try:
        with open(_TIPS_JSON, encoding='utf-8') as f:
            cats = list((json.load(f) or {}).get('categories') or [])
    except Exception:
        cats = []
    if cats:
        return cats
    out: List[str] = []
    for t in _load():
        c = t.get('category')
        if c and c not in out:
            out.append(c)
    return out


def _normalize_query(query: str) -> str:
    """查询归一：实体标准化（舰名/BOSS 谐音错字 → 正规名），失败则原样。"""
    q = str(query or '')
    try:
        from core.entity_norm import standardize
        q = standardize(q)
    except Exception:
        pass
    return _key(q)


def _build_variants(query: str) -> List[str]:
    """查询键的所有试配形态。

    **原文键与归一化键都要试**：归一化会做实体展开（「大拉菲」→「拉菲(DD-459)」、
    「阿斯莫德」→「阿丝莫德」）——前者能对齐别名词条，后者反而会**偏离**知识库里的
    口语写法（tips 标题就是「保底打捞大拉菲」），两种都试才不会两头落空。
    另外再各加一份「剥掉尾部疑问词」的形态（「温泉搓澡怎么弄」→「温泉搓澡」）。
    """
    out: List[str] = []
    for v in (_normalize_query(query), _key(query)):
        if not v:
            continue
        for cand in (v, _TAIL_RE.sub('', v)):
            if cand and len(cand) >= 2 and cand not in out:
                out.append(cand)
    return out


def lookup(query: str) -> Optional[dict]:
    """结构化命中 → {'items': [...], 'match': 'title'|'tag'}；没键可命中返回 None。"""
    variants = _build_variants(query)
    if not variants:
        return None
    tips = _load()
    if not tips:
        return None

    # ---- A 档：标题 ----
    hits: List[tuple] = []          # (score, tip)
    for t in tips:
        kt = _key(t.get('title'))
        if not kt:
            continue
        best = 0
        for v in variants:
            if kt == v:
                best = max(best, 100)
            elif len(kt) >= _A2_MIN and kt in v:
                best = max(best, 50 + len(kt))
            elif _qualifies(v) and v in kt:
                best = max(best, 40 + len(v))
        if best:
            hits.append((best, t))
    if hits:
        top = max(s for s, _ in hits)
        items = [t for s, t in hits if s == top]
        if len(items) > _MAX_ITEMS:
            return None
        return {'items': items, 'match': 'title'}

    # ---- B 档：仅认具体标签（见 _qualifies；分类名不做标签）----
    _cats = _cat_keys()
    scored: List[tuple] = []
    for t in tips:
        sc = 0
        for tag in (t.get('tags') or []):
            kt = _key(tag)
            if kt in _cats or not _qualifies(kt):
                continue
            if any(kt in v for v in variants):
                sc += len(kt)
        if sc >= _A3_MIN:
            scored.append((sc, t))
    if not scored:
        return None
    top = max(s for s, _ in scored)
    items = [t for s, t in scored if s == top]
    if len(items) > _MAX_ITEMS:
        return None
    return {'items': items, 'match': 'tag'}


_ORD = '①②③④⑤'


def render(result: dict) -> str:
    """按项目固定结构渲染（书名号分类标题 + 编号条目 + 正文；原文直出，不改写）。"""
    items = result.get('items') or []
    if not items:
        return ''
    cat = items[0].get('category') or '小技巧'
    lines = ['《小技巧》%s' % cat]
    for i, t in enumerate(items):
        lines.append('%s %s' % (_ORD[i] if i < len(_ORD) else '%d.' % (i + 1), (t.get('title') or '').strip()))
        body = (t.get('content') or '').strip()
        if body:
            lines.append('　　%s' % body)
    return '\n'.join(lines)


def handle(query: str) -> Optional[str]:
    """便捷入口：命中则返回渲染后的原文，否则 None。"""
    try:
        r = lookup(query)
    except Exception:
        return None
    if not r:
        return None
    text = render(r)
    return text or None


if __name__ == '__main__':
    tests = [
        '甲弹克制关系', '甲弹克制关系是什么', '女灶神获取方式', '温泉搓澡', '温 泉 搓 澡 怎么弄',
        '18-7 练级要点', '13-5', '多倍经验', '钻石商店', '巴巴托斯应对', '阿斯莫德应对',
        '舰灵退役', '88自选', '舰灵羁绊加成',
        '遭遇战怎么配队', '争锋竞技场防守阵容推荐', '加算和乘算有什么区别', '去哪练级效率高',
        '长门天赋怎么点', '企业配什么装备', '胡德改怎么配装', 'pvp', '装备', '天赋',
    ]
    for q in tests:
        r = lookup(q)
        if r:
            print('HIT  %-24s [%s x%d] %s' % (q, r['match'], len(r['items']),
                                              ' / '.join(t['title'] for t in r['items'])))
        else:
            print('miss %-24s -> 回落语义召回' % q)
