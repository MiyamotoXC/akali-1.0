# -*- coding: utf-8 -*-
"""遭遇战 BOSS 攻略建议管线（A 攻略原文为主 + B 机制推导兜底）。

为什么需要它
------------
「遭遇战阿斯莫德配装配队有吗」这类问题一句话塞了 **玩法 + 实体 + 意图** 三个概念，
整句丢进向量检索会被语义稀释；实体名再有一字之差（阿斯莫德 / 阿丝莫德）就直接落空。
而「该 BOSS 属于哪种、配队怎么配」本质是 **结构化推理**，不是相似度问题。

管线（先归类 → 再定向取文 → 最后 LLM 综合）
-------------------------------------------
    ① 实体归一    core.entity_norm  → 正规 BOSS 名
    ② 结构化归类  拉古兹.json        → armor_type / ship_type / size / stats
    ③ 定向取文    专属篇 攻略_md/遭遇战_<boss>.md
                  + 7.2 遭遇战攻略.md      的 BOSS 表（甲种/吨位）
                  + 6.3 给萌新的PVE配队思路.md 的 BOSS 应对表
                  + 攻略/tips.json         该 BOSS 的「应对」条目与通用原则
    ④ LLM 综合    只依据 ①②③ 写配队/打法建议；
                  ③ 缺失时退化为按 ② 的机制推导，并明确标注「按机制推导、非攻略原文」

数据只读、不写知识库（严格分层：知识库由数据管理工具维护）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

# 允许直接以脚本运行自检（python core/boss_guide.py）：把 通用工具 加入 sys.path
if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import entity_norm

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_KB = os.path.join(_PROJ_ROOT, '知识库')
_BOSS_JSON = os.path.join(_KB, '底层数据', '源文件', '拉古兹.json')
_MD_DIR = os.path.join(_KB, '攻略_md')
_TIPS_JSON = os.path.join(_KB, '攻略', 'tips.json')

_EXCLUSIVE_MD = '7.2 遭遇战攻略.md'          # 含 BOSS 表：| BOSS名 | 甲种 | 吨位 |
_PVE_MD = '6.3 给萌新的PVE配队思路.md'        # 含 BOSS 应对表：| 阿丝莫德 | 毒液坑 |
# 通用原则条目（非某 BOSS 专属），用于 B 兜底时提供「不编造」的依据
_GENERAL_TAGS = ('按甲弹克制选船', '克制', '手操', '遭遇战')

_cache: Dict[str, object] = {}


def _mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------
# ① 实体归一：从输入里解析出正规 BOSS 名（命中多个时取先出现的）
# --------------------------------------------------------------------------
def boss_names() -> List[str]:
    return entity_norm.boss_names()


def resolve_boss(text: str) -> Optional[str]:
    """解析输入中的 BOSS 名，返回正规名；识别不到返回 None。"""
    if not text:
        return None
    try:
        n = entity_norm.build_matcher()
    except Exception:
        return None
    names = set(n.bosses)
    _, hits = n.scan(text)
    for _, canon in hits:
        if canon in names:
            return canon
    # 未产生改写（输入已是正规名）时，再做一次显式扫描
    for b in n.bosses:
        if b in text:
            return b
    return None


# --------------------------------------------------------------------------
# ② 结构化归类：拉古兹.json
# --------------------------------------------------------------------------
def boss_profile(name: str) -> Optional[dict]:
    """返回该 BOSS 的结构化档案（armor_type / ship_type / size / stats）。"""
    key = ('boss', _mtime(_BOSS_JSON))
    if _cache.get('boss_key') != key:
        data = {}
        try:
            with open(_BOSS_JSON, encoding='utf-8') as f:
                raw = json.load(f)
            for e in (raw.get('enemies') or []):
                if e.get('name'):
                    data[e['name']] = e
        except Exception:
            data = {}
        _cache['boss_key'] = key
        _cache['boss_data'] = data
    return (_cache.get('boss_data') or {}).get(name)


def classify_line(profile: dict) -> str:
    """拼一句人类可读的归类结论，如「中甲 · 航母 · 大船」。"""
    parts = [profile.get('armor_type'), profile.get('ship_type'), profile.get('size')]
    return ' · '.join([p for p in parts if p]) or '（档案字段缺失）'


# --------------------------------------------------------------------------
# ③ 定向取文：md 表格行 + 专属篇 + tips
# --------------------------------------------------------------------------
def _read_lines(rel_path: str) -> List[str]:
    p = os.path.join(_MD_DIR, rel_path)
    if not os.path.isfile(p):
        return []
    try:
        with open(p, encoding='utf-8') as f:
            return f.read().splitlines()
    except Exception:
        return []


def _table_rows_for(lines: List[str], name: str) -> List[str]:
    """从 markdown 表格里取「首列 == name」的行，并带上最近的表头 + 分隔行。"""
    if not lines or not name:
        return []
    out: List[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith('|'):
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if not cells or cells[0] != name:
            continue
        # 往回找表头（上一行是 |---| 分隔行的那一行）
        header = None
        if i >= 2 and lines[i - 1].strip().startswith('|'):
            prev = [c.strip() for c in lines[i - 1].strip().strip('|').split('|')]
            if prev and all(re.fullmatch(r':?-{2,}:?', c or '') for c in prev if c != ''):
                header = lines[i - 2].strip()
        if header and header not in out:
            out.append(header)
        if s not in out:
            out.append(s)
    return out


def _exclusive_boss_md(name: str) -> Tuple[str, str]:
    """专属篇 遭遇战_<boss>.md 全文（不存在返回空）。"""
    fn = '遭遇战_%s.md' % name
    p = os.path.join(_MD_DIR, fn)
    if not os.path.isfile(p):
        return '', ''
    try:
        with open(p, encoding='utf-8') as f:
            text = f.read().strip()
    except Exception:
        return '', ''
    return fn, text


def _tips_for(name: str) -> Tuple[List[str], List[str]]:
    """返回 (该 BOSS 的应对条目, 通用原则条目)。"""
    key = ('tips', _mtime(_TIPS_JSON))
    if _cache.get('tips_key') != key:
        tips = []
        try:
            with open(_TIPS_JSON, encoding='utf-8') as f:
                tips = (json.load(f) or {}).get('tips') or []
        except Exception:
            tips = []
        _cache['tips_key'] = key
        _cache['tips'] = tips
    boss_tips, general_tips = [], []
    for t in (_cache.get('tips') or []):
        title = str(t.get('title') or '')
        content = str(t.get('content') or '')
        tags = [str(x) for x in (t.get('tags') or [])]
        if (t.get('category') == '拉古兹BOSS' and (name in title or name in tags)):
            boss_tips.append('%s：%s' % (title, content))
            continue
        if any(g in title or g in tags for g in _GENERAL_TAGS):
            general_tips.append('%s：%s' % (title, content))
    return boss_tips[:3], general_tips[:4]


def collect_snippets(name: str) -> Dict[str, object]:
    """汇总该 BOSS 的全部定向取文结果。"""
    profile = boss_profile(name) or {}
    md_name, md_text = _exclusive_boss_md(name)
    rows_72 = _table_rows_for(_read_lines(_EXCLUSIVE_MD), name)
    rows_63 = _table_rows_for(_read_lines(_PVE_MD), name)
    boss_tips, general_tips = _tips_for(name)
    has_own_text = bool(md_text) or bool(rows_63) or bool(boss_tips)
    return {
        'name': name,
        'profile': profile,
        'classify': classify_line(profile) if profile else '',
        'exclusive_file': md_name,
        'exclusive_text': md_text,
        'rows_exclusive': rows_72,
        'rows_pve': rows_63,
        'boss_tips': boss_tips,
        'general_tips': general_tips,
        'has_own_text': has_own_text,
    }


# --------------------------------------------------------------------------
# ④ LLM 综合
# --------------------------------------------------------------------------
_SYS_A = (
    '你是拂晓（舰灵海战手游）的遭遇战攻略助手。只依据给定资料回答，严禁补充资料外内容。\n'
    '【可写什么】①BOSS 的甲种/舰种/吨位（取自档案与 BOSS 表）；②专属篇里的舰名、秒数与操作'
    '（必须原样摘抄）；③应对条目里的技能名与机制描述（必须原样摘抄）。\n'
    '【严禁-不得编造】资料中没有的一律不得出现，尤其：\n'
    '  · 不得新增任何舰名（舰名只能来自专属篇）；\n'
    '  · 不得新增或改写时间轴的秒数与操作顺序；\n'
    '  · 不得写资料未提及的弹种/武器选择（如「穿甲弹」「反潜」「雷击压制」）；\n'
    '  · 不得写资料未提及的走位与站位细节（如「绕圈」「侧对」「保持射程」「钻缝隙」）；\n'
    '  · 不得写资料未提及的机制名词。\n'
    '【禁止反问推销】不要出现任何反问或引导性提问，也不要承诺资料与本系统都不存在的功能。\n'
    '输出简洁分点：先一句给出归类结论（甲种 · 舰种 · 吨位），再分'
    '「配队（原文舰名）／时间轴节奏（原文秒数）／机制应对（原文）」三段；'
    '若某段资料缺失就省略该段，不要用推测补齐。不要标注来源篇目，也不要输出书名列表。'
)
_SYS_B = (
    '你是拂晓（舰灵海战手游）的遭遇战攻略助手。攻略库中暂无该 BOSS 的专属配队正文。\n'
    '【只做机制推导】仅依据给定的 BOSS 档案（甲种/舰种/吨位）与通用原则给出应对要点；'
    '开头必须明确说明「攻略库暂无该 BOSS 专属配队，以下按机制推导」。\n'
    '【严禁编造】不得给出任何具体舰名或时间轴（库中没有）；不得写档案与通用原则都推不出的'
    '弹种选择、走位细节或机制名词。\n'
    '【禁止反问推销】不要出现任何反问或引导性提问。\n'
    '输出简洁分点。'
)


def _render_context(s: dict) -> str:
    lines = ['【BOSS 档案】%s：%s' % (s['name'], s['classify'] or '（无）')]
    st = (s.get('profile') or {}).get('stats') or {}
    if st:
        lines.append('  面板：' + '、'.join('%s %s' % (k, v) for k, v in st.items()))
    if s.get('rows_exclusive'):
        lines.append('【遭遇战 BOSS 表】\n' + '\n'.join(s['rows_exclusive']))
    if s.get('rows_pve'):
        lines.append('【PVE 配队·BOSS 应对表】\n' + '\n'.join(s['rows_pve']))
    if s.get('boss_tips'):
        lines.append('【该 BOSS 应对条目】\n' + '\n'.join('· ' + t for t in s['boss_tips']))
    if s.get('exclusive_text'):
        lines.append('【专属篇《%s》原文】\n%s' % (s['exclusive_file'], s['exclusive_text']))
    if s.get('general_tips'):
        lines.append('【通用原则】\n' + '\n'.join('· ' + t for t in s['general_tips']))
    return '\n\n'.join(lines)


def build_advice(name: str, question: str = '', identity: str = '普通'):
    """生成建议。返回 (reply_text, raw_context)；LLM 失败时 reply 回落为资料原文。"""
    s = collect_snippets(name)
    ctx = _render_context(s)
    tone = {'萌新': '提问的是新手玩家：术语要解释，语气平和。',
            '老手': '提问的是老玩家：直接给结论与要点。'}.get(identity, '')
    sys_prompt = (_SYS_A if s['has_own_text'] else _SYS_B) + (('\n' + tone) if tone else '')
    user_prompt = '用户问题：%s\n\n资料：\n%s' % (question or ('遭遇战%s怎么打、怎么配队' % name), ctx)
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.3)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=45)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[{'role': 'system', 'content': sys_prompt},
                      {'role': 'user', 'content': user_prompt}],
            temperature=0.3,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考（整体传 extra_body）
            extra_body=cfg['generate_cfg']['extra_body'],
        )
        answer = (resp.choices[0].message.content or '').strip()
        if answer:
            return answer, ctx
    except Exception:
        pass
    # LLM 不可用 → 回落资料原文（原文直出，不编造）
    if s['has_own_text']:
        head = '攻略库中《%s》相关原文如下：' % (s['exclusive_file'] or s['name'])
        return head + '\n\n' + ctx, ctx
    return ('攻略库暂无《%s》的专属配队内容，仅给出该 BOSS 的档案归类：%s。'
            '可先按甲种克制与舰种威胁准备，或提供更多信息。' % (name, s['classify'] or '（档案缺失）')), ctx


if __name__ == '__main__':
    for q in ('遭遇战阿斯莫德配装配队有吗', '阿斯塔露怎么打', '嘉波配队推荐'):
        boss = resolve_boss(q)
        print('Q:', q, '| boss =', boss)
        if boss:
            s = collect_snippets(boss)
            print('  classify =', s['classify'], '| has_own_text =', s['has_own_text'])
            print('  专属篇 =', s['exclusive_file'] or '(无)',
                  '| rows72 =', len(s['rows_exclusive']), '| rows63 =', len(s['rows_pve']),
                  '| boss_tips =', len(s['boss_tips']), '| general =', len(s['general_tips']))
