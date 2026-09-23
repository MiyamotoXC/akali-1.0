# -*- coding: utf-8 -*-
"""Query 改写层（2026-09-18，参考《RAG 技术与应用》）。

把玩家口语/歧义/复合 query 改写成可被向量命中的「精确检索句」，是 RAG 检索质量
的上限关键点。本模块是项目唯一的 Query 改写实现，被两处复用：

  · rewrite_query_multi(query) —— 生产路径（knowledge._guide_rag_search 调用）：
    返回若干子检索句（对比拆 A/B、多意图分解、反问中性化），由检索层分别 embedding
    后按 chunk 取最高分融合（零额外延迟、零 LLM）。
  · rewrite_query(query, timeout) —— 评测/兜底路径（调试/eval_retrieval.py 的
    --rewrite 模式调用）：返回 {rewritten_query, query_type, confidence} 的字典，
    与原查询做双查询 RRF 融合；规则化实现（不调 LLM，确定性、可离线跑）。

设计约束（贴合本项目「规则优先、零额外延迟」哲学）：
  · 纯规则、不调 LLM（避免每次检索加一次 API 往返 + 依赖）；
  · 对比型拆成 A/B 两个聚焦子句（"北卡和胡德练哪个"分别召回两船培养片段）；
  · 多意图切分后做「实体回带」，解决"还有哪个对轻甲好"丢失前句实体的问题；
  · 模糊指代/上下文依赖型在**检索期无法解析**（缺前轮上下文），只打标 + 轻量中性化，
    真正解析交给 calc_flow 在生成期用对话历史完成（不在此硬解，避免误召回）。

意图标签：通用 / 对比型 / 反问型 / 多意图型 / 模糊指代型 / 上下文依赖型。
"""
import re

# ----------------------------------------------------------------------
# 意图标签
# ----------------------------------------------------------------------
RAG_INTENT_GENERAL = '通用'
RAG_INTENT_COMPARE = '对比型'
RAG_INTENT_RHETORICAL = '反问型'
RAG_INTENT_MULTI = '多意图型'
RAG_INTENT_FUZZY = '模糊指代型'
RAG_INTENT_CONTEXT = '上下文依赖型'

# 评测侧 query_type（eval_retrieval 的 dual-query 融合只认这四类）
_INTENT_TO_EVAL_TYPE = {
    RAG_INTENT_FUZZY: 'vague',
    RAG_INTENT_CONTEXT: 'context',
    RAG_INTENT_MULTI: 'multi',
    RAG_INTENT_RHETORICAL: 'rhetoric',
}

# ----------------------------------------------------------------------
# 规则常量
# ----------------------------------------------------------------------
# 对比型锚点：A 和 B + 这些词 → 判定为对比意图并做 A/B 分解
_COMPARE_KW = (r'区别|差异|哪个好|谁强|对比|比哪个|怎么选|选哪个|哪个值得|'
               r'练哪个|先练谁|更好|强在哪|弱在哪|哪个厉害|哪个强')
_COMPARE_RE = re.compile(
    r'(.+?)\s*(?:和|与|跟|vs|VS|／|/)\s*(.+?)\s*(?:' + _COMPARE_KW + r')')

# 多意图切分：句末标点 + 强连接词（"和"不切，防对比误拆）
_MULTI_SPLIT_RE = re.compile(
    r'[?？!！。；;\n]+|(还有|另外|以及|顺便|然后|再者|与此同时|、\s*(?:还有|另外|还是|怎么))')

# 反问/选择 → 中性陈述 的替换表（轻量：保核心名词，补评价语义）
_RHETORICAL_MAP = [
    (r'行不行|行吗|能行吗|可以不|可行吗', ' 是否可行 '),
    (r'有没有必要|有必要吗', ' 必要性 '),
    (r'值得吗|值不值|划得来吗', ' 价值 性价比 '),
    (r'要不要|应不应该|该不该', ' 是否应该 '),
    (r'(必须|需要|该)\s*(.{0,6}?)吗', r' 是否应该 \2 '),
    (r'能不能|可以吗|能吗', ' 可行性 '),
    (r'是不是', ' 是否 '),
    (r'靠谱吗|坑吗|垃圾吗|废吗', ' 评价 强度 '),
]
_RHETORICAL_HIT = re.compile(
    r'行不行|行吗|能行吗|可以不|可行吗|有必要吗|值得吗|值不值|要不要|应不应该|'
    r'该不该|能不能|可以吗|是不是|靠谱吗|坑吗|垃圾吗|废吗|吗[？?]?$|呢[？?]?$')
_FUZZY_HIT = re.compile(r'它|这个|那个|它们|前者|后者|都\b|上面的|刚才|之前说的|前述')
_CONTEXT_HIT = re.compile(r'这游戏|那游戏|这手游|咱们|刚才|之前说的|上面提到的')

# 实体回带黑名单：这些通用玩法/疑问词不当作"实体"向后句回带（避免噪声）。
_RW_CARRY_BLACKLIST = {
    '强度', '定位', '培养', '特点', '获取', '方式', '推荐', '优先', '核心', '输出',
    '生存', '辅助', '装备', '技能', '阵容', '配队', '练级', '推图', '主线', '经验',
    '数值', '属性', '面板', '适用', '场景', '选择', '对比', '区别', '差异', '优劣',
    '值不值', '值得', '是否', '可行', '必要', '应该', '可以', '能够', '需要', '真的',
    '到底', '究竟', '这种', '那种', '之类', '所谓', '通过', '基于', '对于', '关于',
    '进行', '可能', '以及', '还是', '还有', '另外', '怎么', '怎样', '如何', '什么',
    '哪个', '哪些', '这个', '那个', '为什么', '为啥', '是不是', '有没有', '要不要',
}
_CJK_RUN_RE = re.compile(r'[一-鿿]{2,}')


def _rw_extract_entities(s: str):
    """从句子抽取可回带实体（>=2 字 CJK 片段且非通用黑名单词）。

    不依赖昵称表/术语表（避免循环导入），足以支撑多意图的实体回带。
    """
    out = []
    for t in _CJK_RUN_RE.findall(s or ''):
        if t not in _RW_CARRY_BLACKLIST and t not in out:
            out.append(t)
    return out


def _rw_is_elliptical(s: str) -> bool:
    """子句是否省略了实体（需从前句回带）：过短 / 含指代或裸疑问词。"""
    if len(s) <= 6:
        return True
    if re.search(r'哪个|这个|那个|它|它们|前者|后者|还是|咋|怎么选|怎么打|怎么配|'
                 r'怎么练|怎么用|好吗|好用吗', s):
        return True
    return False


def _rw_neutralize(p: str) -> str:
    """反问/选择句 → 中性陈述检索句（保核心名词，补评价语义）。

    先跑替换表（含"...吗"类规则），再剔除句尾疑问/语气字符，避免先把 吗
    删掉导致"必须…吗→是否应该"这类规则失效。
    """
    s = (p or '').strip()
    for pat, rep in _RHETORICAL_MAP:
        s = re.sub(pat, rep, s)
    s = re.sub(r'[?？!！。；;吗呢嘛呀]+$', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _rw_expand_clause(p: str):
    """单句 → 1~2 条精确检索子句（对比型拆 A/B 两个聚焦句，否则反问中性化）。"""
    p = (p or '').strip()
    if not p:
        return []
    m = _COMPARE_RE.search(p)
    if m:
        a = _rw_neutralize(m.group(1))
        b = _rw_neutralize(m.group(2))
        return [f"{a} 强度 定位 培养 特点", f"{b} 强度 定位 培养 特点"]
    return [_rw_neutralize(p)]


def _rw_split_multi(q: str):
    """多意图切分（句末标点 / 强连接词），清理连接词前缀，返回子句列表。"""
    raw = _MULTI_SPLIT_RE.split(q)
    out = []
    for p in raw:
        if not p:
            continue
        p = re.sub(r'^(还有|另外|以及|顺便|然后|再者|与此同时|还是|怎么)\s*', '', p.strip())
        if p.strip():
            out.append(p.strip())
    return out


def classify_intent(q: str) -> str:
    """规则意图识别（诊断标签，不影响改写主流程）。

    优先级：对比 > 上下文依赖 > 模糊指代 > 反问 > 通用。
    代词/上下文标签优先于反问——"这个技能强吗"更该被标注为模糊指代，
    便于后续在生成期用对话历史解析（反问中性化与标签无关，照常执行）。
    """
    if _COMPARE_RE.search(q):
        return RAG_INTENT_COMPARE
    if _CONTEXT_HIT.search(q):
        return RAG_INTENT_CONTEXT
    if _FUZZY_HIT.search(q):
        return RAG_INTENT_FUZZY
    if _RHETORICAL_HIT.search(q):
        return RAG_INTENT_RHETORICAL
    return RAG_INTENT_GENERAL


def rewrite_query_multi(query: str):
    """生产路径主入口：返回 (子检索句列表, 意图标签)。

    - 多意图 → 切分 + 实体回带；
    - 对比   → 拆 A/B 两个聚焦子句；
    - 反问   → 中性化；
    - 模糊指代/上下文依赖 → 仅打标，原样回退（检索期无法解析，交生成期）。
    单意图非对比 → 返回 1 条中性化子句。
    """
    q = (query or '').strip()
    if not q:
        return [''], RAG_INTENT_GENERAL
    parts = _rw_split_multi(q)
    if len(parts) <= 1:
        intent = classify_intent(q)
        return _rw_expand_clause(q) or [q], intent
    # 多意图：逐句展开（对比句内部再拆），再做实体回带
    flat = []
    for p in parts:
        flat.extend(_rw_expand_clause(p))
    if not flat:
        return [q], RAG_INTENT_MULTI
    out, carry = [], []
    for i, s in enumerate(flat):
        if i > 0 and _rw_is_elliptical(s) and carry:
            s = (s + ' ' + ' '.join(carry)).strip()
        out.append(s)
        for e in _rw_extract_entities(s):
            if e not in carry:
                carry.append(e)
    return out, RAG_INTENT_MULTI


def rewrite_query(query: str, timeout=8.0):
    """评测/兜底路径：返回 {rewritten_query, query_type, confidence} 字典。

    规则化（不调 LLM，确定性、可离线）。与原查询做双查询 RRF 融合时使用：
    置信度 ≥ 0.6 且 query_type ∈ {context, vague, multi, rhetoric} 才启用融合。
    模糊指代/上下文依赖型无法在检索期解析 antecedent，置信度仅 0.6（交给生成期用
    对话历史解析），反问/多意图型解析程度更高（0.7~0.8）。
    """
    q = (query or '').strip()
    if not q:
        return {'rewritten_query': q, 'query_type': 'plain', 'confidence': 0.0}
    intent = classify_intent(q)
    if intent == RAG_INTENT_GENERAL:
        return {'rewritten_query': q, 'query_type': 'plain', 'confidence': 0.0}
    subs, _ = rewrite_query_multi(q)
    rewritten = subs[0] if subs else q
    conf = {
        RAG_INTENT_RHETORICAL: 0.8,
        RAG_INTENT_COMPARE: 0.8,
        RAG_INTENT_MULTI: 0.7,
        RAG_INTENT_FUZZY: 0.6,
        RAG_INTENT_CONTEXT: 0.6,
    }.get(intent, 0.0)
    return {
        'rewritten_query': rewritten,
        'query_type': _INTENT_TO_EVAL_TYPE.get(intent, 'plain'),
        'confidence': conf,
    }
