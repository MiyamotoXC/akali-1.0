# -*- coding: utf-8 -*-
"""意图注册表 + 「选择/比较/评估」语义词表（唯一来源）。用户口径 2026-09-17。

要解决的问题
------------
「对比 / 比较 / 值得 / 优先 / 推荐」这类字面量此前散落在 calc_flow.py 的**五处**正则里
（`_GUIDE_QA_RE` / `_RAG_Q_WORD` / `_SHIP_FILTER_EXCLUDE_RE` / scout·exp 反查闸 / `_calc_rq`），
彼此口径不一致；而「走哪条分支」实际由 `if` 的**行号**决定——新增一个语义要到处补词，
还要猜该插在第几行。典型翻车：「对比780和太太」没有被任何一个分支的**意图**接住，
反而因含两个舰名被末级 `_llm_analyze_intent` → `_resolve_role_from_msg` 的
「昵称子串取最长」捞成单个舰名（780→齐柏林伯爵），直接回了单舰面板。

本模块确立两条规则
------------------
1. **词表唯一来源**：凡「选择 / 比较 / 评估」语义，一律引用本模块的
   `COMPARE_WORDS` / `EVALUATE_WORDS` / `CHOICE_WORDS`（或预编译 `CHOICE_RE` /
   `CHOICE_RE_SRC`），不再在各处写裸字面量。
2. **意图是唯一裁决者**：LLM 分类成功以 `intent` 为准；失败/超时用 `classify_by_rules()`
   同口径兜底；两者都拿不到 → `None` → 交回 calc_flow 既有的正则阶梯（可用性兜底）。
   `INTENTS[...]['priority']` 只在**同一轮出现多个意图**时用于取舍，**不再与代码行号挂钩**。

铁律（不可违背）
----------------
本模块只做**分类**，不生成任何面向用户的文本。处理器回复必须原文直出（数据来自底层 raw /
知识库），无据内容一律明说「无据」，禁止编造。
"""
import re

__all__ = [
    'COMPARE_WORDS', 'EVALUATE_WORDS', 'CHOICE_WORDS', 'CHOICE_ALL',
    'COMPARE_RE_SRC', 'EVALUATE_RE_SRC', 'CHOICE_RE_SRC', 'CHOICE_RE',
    'INTENTS', 'default_priority', 'pick_intent', 'classify_by_rules',
]

# ======================================================================
# 一、语义词表（唯一来源）
# ======================================================================
# 比较：并列两个及以上实体，问彼此差异 / 高下（"对比A和B""A和B哪个好""A还是B"）
COMPARE_WORDS = (
    '对比', '相比', '比较', '区别', '差别', '差异', '还是',
    '哪个好', '哪个更好', '哪个强', '哪个厉害', '哪个值得',
    '谁更好', '谁更强', '谁值得', '选哪个', '选谁', '练哪个', '用哪个',
)
# 评估：问「值不值得 / 该先练谁 / 推荐哪个 / 优先级」（可只有单个实体）
EVALUATE_WORDS = (
    '值得', '值不值', '划算', '优先', '优先级', '先练', '先养',
    '优先练', '优先培养', '培养优先', '推荐', '推荐练',
    '练谁', '用谁', '该练', '该养', '排行', '排名', '榜',
    '评价', '评估', '点评', '怎么样', '咋样',
)
# 选择疑问标记（择一/单选措辞的补充）
CHOICE_WORDS = ('哪一个', '哪个', '选择', '选哪')

# 合并词表：凡「处在选择 / 比较 / 评估语境」的站点统一引用这一个
CHOICE_ALL = tuple(dict.fromkeys(COMPARE_WORDS + EVALUATE_WORDS + CHOICE_WORDS))


def _alt(words):
    """构造「长词优先」的正则片段（避免「哪个」先于「哪个好」命中）。"""
    return '|'.join(sorted(set(w for w in words if w), key=len, reverse=True))


COMPARE_RE_SRC = _alt(COMPARE_WORDS)
EVALUATE_RE_SRC = _alt(EVALUATE_WORDS)
CHOICE_RE_SRC = _alt(CHOICE_ALL)
CHOICE_RE = re.compile(CHOICE_RE_SRC)

# ======================================================================
# 二、意图注册表
# ======================================================================
# handler:  处理器函数名（由 calc_flow 在运行期绑定，避免循环 import）。
#           必须返回 dict（确定性回复）或 None（放弃接管，交回原正则阶梯）。
# slots:    本意图需要的槽位——处理器据此取实体/条件，评审新意图时一眼看清依赖。
# priority: 同轮多意图冲突时的取舍权重（大者优先）；**与代码行号无关**。
#           参考基线：明确工具流 > 计算 > 工具调用 > 数据查询 > 选择/评估 > 闲聊。
INTENTS = {
    'calc':     {'handler': 'handle_calc',           'slots': ('entities',),                 'priority': 90, 'rules': False},
    'scout':    {'handler': '_handle_scout',         'slots': ('numbers', 'entities'),       'priority': 85, 'rules': False},
    'exp':      {'handler': '_handle_exp_calc',      'slots': ('numbers',),                  'priority': 85, 'rules': False},
    'encounter': {'handler': None,                   'slots': ('entities',),                 'priority': 80, 'rules': False},
    'pvp':      {'handler': None,                    'slots': ('entities',),                 'priority': 80, 'rules': False},
    'compare':  {'handler': '_handle_ship_compare',  'slots': ('entities',),                 'priority': 70, 'rules': True},
    'evaluate': {'handler': '_handle_ship_compare',  'slots': ('entities',),                 'priority': 65, 'rules': True},
    'favor':    {'handler': None,                    'slots': ('numbers',),                  'priority': 60, 'rules': False},
    'equip':    {'handler': None,                    'slots': ('entities',),                 'priority': 55, 'rules': False},
    'query':    {'handler': '_handle_ship_filter',   'slots': ('entities', 'conditions'),    'priority': 50, 'rules': False},
    'formula':  {'handler': None,                    'slots': (),                            'priority': 45, 'rules': False},
    'guide':    {'handler': '_route_guide',          'slots': ('topic',),                    'priority': 40, 'rules': False},
    'theme_intro': {'handler': '_handle_theme_intro', 'slots': ('topic',),                   'priority': 35, 'rules': False},
    'capability': {'handler': None,                  'slots': (),                            'priority': 30, 'rules': False},
    'chat':     {'handler': None,                    'slots': (),                            'priority': 0,  'rules': False},
}


def default_priority(intent_name):
    """意图的默认取舍权重；未注册的意图返回 0。"""
    return (INTENTS.get(intent_name or '') or {}).get('priority', 0)


def pick_intent(names):
    """同轮出现多个意图时的裁决：取 priority 最大者（同分保持出现顺序）。

    用途：LLM 与规则兜底各判出一个意图、或一句话同时命中多类语义时，由优先级表决定，
    而不是由 `if` 的书写位置决定。返回 None 表示无候选。
    """
    best, best_p = None, -1
    for n in names or ():
        p = default_priority(n)
        if p > best_p:
            best, best_p = n, p
    return best


# ======================================================================
# 三、规则兜底分类（与 LLM 分类同口径）
# ======================================================================
# 仅用于「LLM 分类失败 / 超时」时的兜底；成功时一律以 LLM intent 为准。
# 刻意只覆盖**规则可判定**的意图，判不准一律 None（交回 calc_flow 原正则阶梯）——
# 这是可用性兜底，不是主力分类器，宁可少判也不误判。
#
# 明确的计算诉求（伤害/dps/期望/一击）不在此兜底：那些是 calc 的地盘，
# 交给 LLM 与下方 _calc_strong 判定，避免把「算A和B伤害」抢成对比。
_CALC_GUARD_RE = re.compile(r'伤害|dps|DPS|期望|一击|一发|总伤')
# 并列实体的连接/择一信号（compare 需要「两件东西」才成立）
_PAIR_RE = re.compile(r'和|与|、|,|，|\+|&|vs|VS|还是|哪个|哪一个|谁')


def classify_by_rules(msg):
    """按词表兜底判定意图；判不出返回 None（交回原正则阶梯）。

    - compare ：含比较/选择词 + 并列信号，且不是明确的计算诉求
    - evaluate：含评估词（值得/优先/推荐…），且不是明确的计算诉求
    其余一律 None。
    """
    s = (msg or '').strip()
    if not s or _CALC_GUARD_RE.search(s):
        return None
    if CHOICE_RE.search(s):
        if _PAIR_RE.search(s) and re.search(COMPARE_RE_SRC, s):
            return 'compare'
        if re.search(EVALUATE_RE_SRC, s):
            return 'evaluate'
    return None
