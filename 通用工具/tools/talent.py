# -*- coding: utf-8 -*-
"""天赋解析模块：把用户输入的天赋编码（如 1212）解析为该角色的天赋 buff。

天赋数据来源（数据管理工具 stage1 产物）：
- 知识库/raw_data.json 的每角色 talents 字段（四行二列：1A/1B ~ 4A/4B）
- 知识库/talent_map.json：天赋名 → {desc, v1, v2}（名称字典区）

编码规则：4 位数字，第 i 位表示第 i 行选择：1=A 选项、2=B 选项。
例如 1212 = 第1行A + 第2行B + 第3行A + 第4行B。

天赋效果 → 标准 buff 键（damage_formula 读带"增加"后缀的键）：
- 增伤向 → attacker.buffs（炮击伤害增加/雷击伤害增加/全伤害增加/大小型加成...）
- 减伤向 → enemy（全伤害减免/炮击伤害减免/大小型减免...）
"""
import json
import os
import re

from qwen_agent.tools.base import BaseTool

from .common import _ATK_PANEL_FIELDS, _DEF_PANEL_FIELDS, _ALL_PANEL_FIELDS

# 面板类字段集合（天赋影响面板时按原始面板计算，写入副本面板）
# 统一从 common 导入，确保与 calc_flow/session 等其他模块的面板字段定义一致
_PANEL_FIELDS = _ALL_PANEL_FIELDS
# 攻击/防御面板键集合（用于 raw data 面板字段识别）
_ATK_PANEL_KEYS = set(_ATK_PANEL_FIELDS)
_DEF_PANEL_KEYS = set(_DEF_PANEL_FIELDS)

# 知识库目录：talent.py 位于 通用工具/tools/，上溯三级到项目根（阿卡莉1.0/）
KB_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "知识库"))
TALENT_MAP_FILE = os.path.join(KB_DIR, "底层数据", "产物", "talent_map.json")
RAW_DATA_FILE = os.path.join(KB_DIR, "底层数据", "产物", "raw_data.json")
TALENT_PVP_FILE = os.path.join(KB_DIR, "底层数据", "产物", "talent_pvp.json")
TALENT_PVE_FILE = os.path.join(KB_DIR, "底层数据", "产物", "talent_pve.json")

_TALENT_MAP_CACHE = None
_TALENT_PVP_CACHE = None
_TALENT_PVE_CACHE = None


def format_talents_4x2(raw: dict) -> str:
    """把角色天赋渲染成「四行二列」版式（每行左 A 右 B，共 4 行）。

    数据契约：raw['talents'] 为 {行号: {A: {name, effect}, B: {name, effect}}}，
    行号键为 '1'~'4'。兼容旧格式（值为纯字符串天赋名）。
    处处复用同一段格式（查角色总览 / 查字段天赋 / 伤害计算天赋段），避免分叉。
    """
    talents = (raw or {}).get('talents') or {}
    if not talents:
        return "  天赋: 未配置"
    lines = []
    for row in ('1', '2', '3', '4'):
        rd = talents.get(row)
        if not rd:
            continue
        cells = []
        for opt in ('A', 'B'):
            od = rd.get(opt) or {}
            if isinstance(od, dict):
                tname = (od.get('name') or '').strip()
                effect = (od.get('effect') or '').strip()
            else:
                tname = (od or '').strip()
                effect = ''
            if tname and effect:
                cells.append("[%s%s] %s(%s)" % (row, opt, tname, effect))
            else:
                cells.append("[%s%s] %s" % (row, opt, tname) if tname else "-")
        lines.append("  第%s行: %s | %s" % (row, cells[0], cells[1]))
    return "\n".join(lines)


def load_talent_map() -> dict:
    """读取 知识库/talent_map.json（天赋名 → {desc, v1, v2}），带模块级缓存。"""
    global _TALENT_MAP_CACHE
    if _TALENT_MAP_CACHE is not None:
        return _TALENT_MAP_CACHE
    if not os.path.exists(TALENT_MAP_FILE):
        _TALENT_MAP_CACHE = {}
        return _TALENT_MAP_CACHE
    try:
        with open(TALENT_MAP_FILE, encoding="utf-8") as f:
            _TALENT_MAP_CACHE = json.load(f)
    except Exception:
        _TALENT_MAP_CACHE = {}
    return _TALENT_MAP_CACHE


def _load_raw_ship_talents(app, ship_name: str):
    """从 App 的 raw_storage 读取角色的 talents（{1:{A,B},...}）。未找到返回 None。"""
    matched_name, raw = app.get_raw_ship(ship_name)
    if raw is None:
        return None, "未找到角色: " + ship_name
    talents = raw.get("talents") or {}
    return talents, None


def parse_talent_code(code: str):
    """解析天赋编码（如 '1212'）→ [(行号, 选项)] 列表。
    行号 '1'~'4'，选项 'A'/'B'。非法编码返回 None。"""
    code = (code or '').strip()
    if len(code) != 4 or not code.isdigit():
        return None
    pairs = []
    for i, ch in enumerate(code):
        row = str(i + 1)
        opt = 'A' if ch == '1' else ('B' if ch == '2' else None)
        if opt is None:
            return None
        pairs.append((row, opt))
    return pairs


# 伤害类型 → 增伤 buff 键（「XX伤害提升」→ 对应伤害增加，用于合并提升扫描）
# 注意与面板字段区分：面板字段是「航空/炮击值」，伤害类是「航空伤害/炮击伤害」→ buff
_DMG_BUFF_MAP = {
    "炮击": "炮击伤害增加",
    "雷击": "雷击伤害增加",
    "航空": "航空伤害增加",
    "轰炸": "轰炸伤害增加",
    "鱼雷": "鱼雷伤害增加",
    "近战": "近战伤害增加",
    "高平炮": "高平炮伤害增加",
    "对空": "对空伤害增加",
    # 普攻专属伤害加成（仅对普攻类生效，引擎侧 is_normal 配合）
    "普通攻击": "普通攻击伤害加成",
    "普攻": "普通攻击伤害加成",
}
# 综合映射：desc 关键词 → 效果片段列表。
# 片段 = (类型, 键, v1/v2, 值形态)
#   类型: 'panel'（面板字段，percent 按原始面板乘算、fixed 直接加）
#         'buff'（攻击方 buffs，直接用数值）
#         'enemy'（敌方属性/减免，直接用数值）
#   值形态: 1 用 v1，2 用 v2
_TALENT_RULES = [
    # ---- 面板类 ----
    ("索敌及命中值提升", [('panel', '索敌', 1), ('panel', '命中', 2)]),
    ("对空值提升及对潜值少量提升", [('panel', '对空', 1)]),
    ("对空及索敌提升", [('panel', '对空', 1), ('panel', '索敌', 2)]),
    ("炮击值及命中值提升", [('panel', '炮击', 1), ('panel', '命中', 2)]),
    ("对空值提升及机动值提升", [('panel', '对空', 1), ('panel', '机动', 2)]),
    ("对潜值及索敌提升", [('panel', '索敌', 2)]),
    ("机动及索敌提升", [('panel', '机动', 1), ('panel', '索敌', 2)]),
    ("机动值提升", [('panel', '机动', 1), ('panel', '机动', 2)]),
    ("装甲值提升", [('panel', '装甲', 1), ('panel', '装甲', 2)]),
    ("命中值提升", [('panel', '命中', 1), ('panel', '命中', 2)]),
    ("血量提升", [('panel', '血量', 1), ('panel', '血量', 2)]),
    # 回避率 = 「被攻击方」属性（绝对百分比，从攻击方命中率中直接扣除），故归 enemy 通道。
    # 旧实现写作 ('panel','回避率',1)：面板按 percent 乘原始面板算，而底层 raw 面板
    # 根本没有「回避率」键 → 基数 0 → 增量恒为 0（天赋静默失效）。用户口径 2026-09-14 修正。
    ("回避率上升但耗油值上升", [('enemy', '回避率', 1)]),
    ("回避率上升但命中下降", [('enemy', '回避率', 1), ('panel', '命中', 2)]),
    ("回避率上升", [('enemy', '回避率', 1)]),
    ("移速上升但机动下降", [('panel', '机动', 2)]),
    ("被攻击伤害下降但机动下降", [('enemy', '全伤害减免', 1), ('panel', '机动', 2)]),
    ("装甲值提升 被轰炸攻击伤害下降", [('panel', '装甲', 1), ('enemy', '轰炸伤害减免', 2)]),
    ("被暴击率下降及装甲值提升", [('enemy', '防暴击率', 1), ('panel', '装甲', 2)]),
    ("血量及炮击伤害加成", [('panel', '血量', 1), ('buff', '炮击伤害增加', 2)]),
    ("航空伤害及装填提升", [('buff', '航空伤害增加', 1), ('panel', '装填', 2)]),
    ("鱼雷伤害加成", [('buff', '鱼雷伤害增加', 1), ('panel', '装填', 2)]),
    # 「XX伤害加成+N%」形态（表格「设定」列的另一种写法，与「XX伤害提升」同义）。
    # 2026-09-14 补：此前该形态只有 鱼雷伤害加成 一条 → 炮击/雷击/轰炸/航空 缺失，
    # 导致 樱花怒放(炮击)、彩虹计划(雷击)、夜间轰炸(轰炸) 三条天赋 desc 命中不了任何规则、
    # 天赋静默失效（如 独立 默认 PVP 1111 选 4A 夜间轰炸 → 轰炸伤害+20% 被丢掉）。
    ("炮击伤害加成", [('buff', '炮击伤害增加', 1)]),
    ("雷击伤害加成", [('buff', '雷击伤害增加', 1)]),
    ("航空伤害加成", [('buff', '航空伤害增加', 1)]),
    ("轰炸伤害加成", [('buff', '轰炸伤害增加', 1)]),
    # 「…及回避率+X%」形态（表格「设定」列的另一种写法，如 彩虹计划/夜间轰炸）。
    # 回避率按绝对百分比进 enemy 通道（被攻击方属性）。通用关键词放最后：
    # 更长的「回避率上升…」规则先命中并覆盖本区间，不会双算。
    ("回避率", [('enemy', '回避率', 1)]),
    ("被攻击伤害下降及索敌提升", [('enemy', '全伤害减免', 1), ('panel', '索敌', 2)]),
    ("回避率上升及夜战时被攻击伤害下降", [('enemy', '回避率', 1), ('enemy', '全伤害减免', 2)]),
    ("机动提升", [('panel', '机动', 1)]),
    ("命中值", [('panel', '命中', 2)]),
    ("索敌提升", [('panel', '索敌', 2)]),
    # ---- 增减伤 buff ----
    ("对小型舰伤害提升并且被小型舰攻击伤害小幅下降", [('buff', '小型攻击伤害加成', 1), ('enemy', '小型攻击伤害减免', 2)]),
    ("对中型舰伤害提升并且被中型舰攻击伤害小幅下降", [('buff', '中型攻击伤害加成', 1), ('enemy', '中型攻击伤害减免', 2)]),
    ("对大型舰伤害提升并且被大型舰攻击伤害小幅下降", [('buff', '大型攻击伤害加成', 1), ('enemy', '大型攻击伤害减免', 2)]),
    ("被小型舰攻击伤害下降并且对小型舰伤害提升", [('enemy', '小型攻击伤害减免', 1), ('buff', '小型攻击伤害加成', 2)]),
    ("被中型舰攻击伤害下降并且对中型舰伤害提升", [('enemy', '中型攻击伤害减免', 1), ('buff', '中型攻击伤害加成', 2)]),
    ("被大型舰攻击伤害下降并且对大型舰伤害提升", [('enemy', '大型攻击伤害减免', 1), ('buff', '大型攻击伤害加成', 2)]),
    ("被小型舰攻击伤害下降20%及中型舰攻击伤害下降10%", [('enemy', '小型攻击伤害减免', 1), ('enemy', '中型攻击伤害减免', 2)]),
    ("对小型舰及中型舰伤害提升", [('buff', '小型攻击伤害加成', 1), ('buff', '中型攻击伤害加成', 2)]),
    ("对小型舰伤害提升", [('buff', '小型攻击伤害加成', 1)]),
    ("对中型舰伤害提升", [('buff', '中型攻击伤害加成', 1)]),
    ("对大型舰伤害提升", [('buff', '大型攻击伤害加成', 1)]),
    ("被小型舰攻击伤害下降", [('enemy', '小型攻击伤害减免', 1)]),
    ("被中型舰攻击伤害下降", [('enemy', '中型攻击伤害减免', 1)]),
    ("被大型舰攻击伤害下降", [('enemy', '大型攻击伤害减免', 1)]),
    ("被雷击伤害及被鱼雷伤害下降", [('enemy', '雷击伤害减免', 1), ('enemy', '鱼雷伤害减免', 2)]),
    ("被炮击伤害下降", [('enemy', '炮击伤害减免', 2)]),
    ("被航空攻击伤害下降及对空值提升", [('enemy', '航空伤害减免', 1), ('panel', '对空', 2)]),
    ("被航空攻击伤害下降", [('enemy', '航空伤害减免', 1)]),
    ("被轰炸攻击伤害下降", [('enemy', '轰炸伤害减免', 1)]),
    ("被攻击伤害下降", [('enemy', '全伤害减免', 1)]),
    ("夜战时最终伤害提升及被攻击伤害下降", [('buff', '全伤害增加', 1), ('enemy', '全伤害减免', 2)]),
    ("夜战时伤害大幅提升但被攻击伤害提升", [('buff', '全伤害增加', 1), ('enemy', '全伤害减免', 2)]),
    ("炮击伤害大幅提升但被暴击率提升", [('buff', '炮击伤害增加', 1), ('enemy', '被暴击率', 2)]),
    ("雷击伤害大幅提升但炮击伤害下降", [('buff', '雷击伤害增加', 1), ('buff', '炮击伤害增加', 2)]),
    ("雷击伤害大幅提升但命中值下降", [('buff', '雷击伤害增加', 1), ('panel', '命中', 2)]),
    ("攻击伤害大幅提升但被攻击伤害提升", [('buff', '全伤害增加', 1), ('enemy', '全伤害减免', 2)]),
    ("炮击及雷击伤害提升", [('buff', '炮击伤害增加', 1), ('buff', '雷击伤害增加', 2)]),
    ("炮击伤害大幅提升", [('buff', '炮击伤害增加', 1)]),
    ("炮击伤害提升", [('buff', '炮击伤害增加', 1)]),
    ("雷击伤害大幅提升", [('buff', '雷击伤害增加', 1)]),
    ("雷击伤害提升", [('buff', '雷击伤害增加', 1)]),
    ("航空伤害提升", [('buff', '航空伤害增加', 1)]),
    ("轰炸伤害提升", [('buff', '轰炸伤害增加', 1)]),
    ("鱼雷伤害提升", [('buff', '鱼雷伤害增加', 1)]),
    ("对空伤害提升", [('buff', '对空伤害增加', 1)]),
    ("攻击伤害提升", [('buff', '全伤害增加', 1)]),
    # 普攻专属伤害加成：仅对普攻类生效（引擎侧 is_normal 配合）。
    # 与「炮击伤害加成」等形态同义，但走独立的 普通攻击伤害加成 键（技能/必杀/支援不享受）。
    ("普攻伤害加成", [('buff', '普通攻击伤害加成', 1)]),
    ("普攻伤害提升", [('buff', '普通攻击伤害加成', 1)]),
    ("普攻增伤", [('buff', '普通攻击伤害加成', 1)]),
    ("普通攻击伤害加成", [('buff', '普通攻击伤害加成', 1)]),
    ("普通攻击伤害提升", [('buff', '普通攻击伤害加成', 1)]),
    ("普通攻击增伤", [('buff', '普通攻击伤害加成', 1)]),
    ("暴击率及暴击伤害提升", [('buff', '暴击率增加', 1), ('buff', '暴击伤害增加', 2)]),
    ("暴击机率上升", [('buff', '暴击率增加', 1)]),
    ("命中率上升", [('buff', '命中率增加', 1)]),
    ("对航空母舰伤害提升", [('ship_bonus', '航母', 1)]),
    ("对驱逐舰伤害提升", [('ship_bonus', '驱逐', 1)]),
    ("对重巡伤害提升", [('ship_bonus', '重巡', 1)]),
    # ---- 技能 CD 缩减（百分比，应用时按四舍五入取整为实际 CD）----
    ("必杀技的CD缩短", [('cd', 'burst', 1)]),
    ("通常技的CD缩短", [('cd', 'skill', 1)]),
    ("必杀技CD减少", [('cd', 'burst', 2)]),
]


def _match_rules(desc: str):
    """按 desc 关键词收集命中的全部规则片段（重叠关键词只取最长一条，避免双算）。
    例「炮击伤害提升20%及鱼雷伤害提升10%」同时命中 炮击伤害提升 与 鱼雷伤害提升；
    而「炮击伤害大幅提升」只命中长规则，不重复命中其子串「炮击伤害提升」。
    再经合并结构识别（「XX伤害及YY伤害/值提升」）与槽位分配 v1/v2。
    返回 [(typ, key, vi)]。"""
    hits = []  # [(start, end)] 已记录的命中区间（按关键词长度降序，长规则优先）
    frags = []  # [(typ, key, vi_hint, pos, self_conn)]
    for kw, f in sorted(_TALENT_RULES, key=lambda x: len(x[0]), reverse=True):
        if kw not in desc:
            continue
        start = desc.index(kw)
        end = start + len(kw)
        # 命中区间被更长的已记录规则覆盖 → 跳过（避免 大幅提升 与 提升 双算）
        if any(ms <= start and end <= me for ms, me in hits):
            continue
        hits.append((start, end))
        # 规则自身含分隔词（及/并且/但）→ 整段是独立效果单元，保留规则硬编码 v1/v2
        self_conn = any(c in kw for c in ('及', '并且', '但'))
        for fi, (typ, key, vi) in enumerate(f):
            pos = start + fi
            # 面板字段取其在 desc 中的真实位置（如「鱼雷伤害加成及装填+75」的装填在 及 后）
            if typ == 'panel':
                idx = desc.find(key)
                if idx != -1:
                    pos = idx
            frags.append((typ, key, vi, pos, self_conn))
    # 补充识别「XX伤害及YY伤害/值提升」合并结构（_TALENT_RULES 无法匹配的形态）
    merged = _scan_merged_boost(desc, hits)
    if merged:
        rngs = [r for _frags, r in merged]
        # 合并结构优先级高：移除落在合并区间内的规则片段（避免 航空伤害 被规则+合并双算）
        frags = [fr for fr in frags if not any(ms <= fr[3] < me for ms, me in rngs)]
        for mfrags, _r in merged:
            for typ, key, _vi, pos in mfrags:
                frags.append((typ, key, _vi, pos, False))
    # 按效果槽位分配 v1/v2 并去重
    return _assign_vi(frags, desc)


def _scan_merged_boost(desc: str, hits):
    """识别「XX伤害及YY伤害/值提升」合并提升结构（_TALENT_RULES 未覆盖的形态）：
    - 及 前的伤害类型 → 增伤 buff（如「炮击伤害及航空伤害提升15%」→ 炮击伤害增加）
    - 及 后的伤害类型 → 增伤 buff（航空伤害增加）
    - 及 后的面板值字段 → 面板数值变化（如「炮击伤害及命中值提升15%」→ 命中 panel）
    - 及 后的其他（对陆地目标等无计算机制）→ 跳过
    已由 hits（_TALENT_RULES 命中区间）整体覆盖的跳过，避免重复。
    返回 [([(typ, key, vi_hint, pos)], (s, e))]，vi_hint=None 表示按槽位分配；
    pos 取 m.start()（及前）与 m.start(2)（及后，用于槽位判定）。"""
    out = []
    for m in re.finditer(r'(炮击|雷击|航空|轰炸|鱼雷|对空|普攻|普通攻击)伤害及([^，。、\s]+?)(伤害|值)?(提升|增加|加成)', desc):
        s, e = m.start(), m.end()
        if any(ms <= s and e <= me for ms, me in hits):
            continue
        left = m.group(1)   # 及前的伤害类型
        right = m.group(2)  # 及后的类型名
        kind = m.group(3)   # '伤害' 或 '值' 或 None
        frags = [('buff', _DMG_BUFF_MAP[left], None, s)]
        if kind == '伤害' and right in _DMG_BUFF_MAP:
            frags.append(('buff', _DMG_BUFF_MAP[right], None, m.start(2)))
        elif kind == '值' and right in _PANEL_FIELDS:
            frags.append(('panel', right, None, m.start(2)))
        out.append((frags, (s, e)))
    return out


def _connector_positions(desc: str):
    """返回效果分隔词（并且/及/但）在 desc 中的位置（升序）。"""
    pos = []
    for c in ('并且', '及', '但'):
        i = desc.find(c)
        while i != -1:
            pos.append(i)
            i = desc.find(c, i + 1)
    return sorted(pos)


def _assign_vi(frags, desc):
    """按效果槽位分配 v1/v2 并去重，返回 [(typ, key, vi)]：
    - 无分隔词 → 单一效果，保留规则硬编码 vi（如 机动值提升 = v1百分比 + v2固定值）
    - 规则自身含分隔词 → 独立效果单元，保留硬编码 vi（如 索敌及命中值提升）
    - 有分隔词 → 分隔词之前的片段为第一效果(v1)、之后为第二效果(v2)；
      同一槽位内同键去重（「命中值提升」规则在槽2会生成多条 → 只留一条）
    """
    slots = _connector_positions(desc)
    result = []  # [(typ, key, vi, slot)]
    for typ, key, vi, pos, self_conn in frags:
        if self_conn or not slots:
            result.append((typ, key, vi, None))
            continue
        before = sum(1 for cpos in slots if cpos < pos)
        slot = 0 if before % 2 == 0 else 1
        result.append((typ, key, 1 if slot == 0 else 2, slot))
    # 槽位分配的同键去重：同一槽位内 (typ, key) 只保留一条；硬编码片段保留全部
    dedup = []
    seen = set()
    for typ, key, vi, slot in result:
        if slot is None:
            dedup.append((typ, key, vi))
            continue
        sig = (typ, key, slot)
        if sig in seen:
            continue
        seen.add(sig)
        dedup.append((typ, key, vi))
    return dedup


def talent_to_effects(talent_map: dict, names: list, raw_panel: dict = None):
    """把选中的天赋名列表转为效果：
    - panel: {面板字段: 增量}（按原始面板 percent 乘算 / fixed 直接加）
    - buffs: {标准增伤键: 数值}
    - enemy: {敌方属性/减免键: 数值}
    - ship_bonus: {舰种: 数值}
    - cd: {'skill': pct, 'burst': pct} 技能 CD 缩减百分比（应用时四舍五入取整）
    raw_panel: raw_data.json 的原始面板 dict（'面板' 下），percent 类按它算。
    """
    result = {"panel": {}, "buffs": {}, "enemy": {}, "ship_bonus": {}, "cd": {}}
    raw_panel = raw_panel or {}
    for name in names:
        info = talent_map.get(name)
        if not info:
            continue
        desc = info.get("desc", "") or ""
        v1 = info.get("v1")
        v2 = info.get("v2")
        for tag, val in (('1', v1), ('2', v2)):
            try:
                if val in (None, ''):
                    locals()['v' + tag] = None
                else:
                    locals()['v' + tag] = float(val)
            except (ValueError, TypeError):
                locals()['v' + tag] = None
        frags = _match_rules(desc)
        if not frags:
            # 无规则命中：尝试按面板字段名兜底（如"机动提升"→机动），同样走槽位分配
            for f in _PANEL_FIELDS:
                if f in desc and ('提升' in desc or '增加' in desc or '下降' in desc):
                    frags.append(('panel', f, 1, desc.index(f), False))
                    break
            frags = _assign_vi(frags, desc) if frags else []
        for typ, key, vi in frags:
            val = v1 if vi == 1 else v2
            if val is None:
                continue
            if typ == 'panel':
                # percent：数值 < 1 视为百分比（按原始面板乘算）；>= 1 视为固定值
                if abs(val) < 1:
                    base = raw_panel.get(key, 0) or 0
                    result["panel"][key] = result["panel"].get(key, 0.0) + base * val
                else:
                    result["panel"][key] = result["panel"].get(key, 0.0) + val
            elif typ == 'buff':
                result["buffs"][key] = result["buffs"].get(key, 0.0) + val
            elif typ == 'enemy':
                result["enemy"][key] = result["enemy"].get(key, 0.0) + val
            elif typ == 'ship_bonus':
                result["ship_bonus"][key] = result["ship_bonus"].get(key, 0.0) + val
            elif typ == 'cd':
                # 同类 CD 缩减跨天赋叠加（多个必杀 CD 缩减天赋取累计百分比）
                result["cd"][key] = result["cd"].get(key, 0.0) + val
    return result


def apply_cd_effects(cd_obj, cd_eff: dict) -> dict:
    """把天赋 CD 缩减百分比应用到 CombatData 实际 CD（四舍五入取整）。
    cd_eff: {'skill': pct, 'burst': pct}（talent_to_effects 产出）。
    直接修改传入对象的 skill_attack['cd'] / burst_attack['cd']，返回缩减明细
    {'skill': (旧, 新), 'burst': (旧, 新)}（仅列出发生变化的项）。"""
    changed = {}
    for key, attr in (('skill', 'skill_attack'), ('burst', 'burst_attack')):
        pct = float((cd_eff or {}).get(key, 0) or 0)
        if pct <= 0:
            continue
        atk = getattr(cd_obj, attr, None)
        if not atk:
            continue
        base = float(atk.get('cd', 0) or 0)
        if base <= 0:
            continue
        # 四舍五入取整为实际 CD（缩后值必为正，int(x+0.5) 即四舍五入）
        new_cd = float(int(base * (1 - pct) + 0.5))
        if new_cd != base:
            atk['cd'] = new_cd
            changed[key] = (base, new_cd)
    return changed


def resolve_talents(app, ship_name: str, code: str) -> dict:
    """解析角色天赋编码 → 标准效果（面板/buffs/enemy/ship_bonus）+ 天赋名列表。
    返回 {'ok': bool, 'msg': str, 'panel': {...}, 'buffs': {...}, 'enemy': {...},
           'ship_bonus': {...}, 'names': [...], 'code': 编码}"""
    talents, err = _load_raw_ship_talents(app, ship_name)
    if err:
        return {"ok": False, "msg": err}
    if not talents:
        return {"ok": False, "msg": "角色 %s 没有天赋数据（知识库天赋表未收录）" % ship_name}
    pairs = parse_talent_code(code)
    if pairs is None:
        return {"ok": False, "msg": "天赋编码格式错误: %s，应为 4 位数字（如 1212，每位 1=A 或 2=B）" % code}
    names = []
    for row, opt in pairs:
        row_data = talents.get(row)
        if not row_data:
            return {"ok": False, "msg": "角色 %s 缺少第 %s 行天赋" % (ship_name, row)}
        opt_data = row_data.get(opt)
        if not opt_data:
            return {"ok": False, "msg": "角色 %s 第 %s 行无 %s 选项" % (ship_name, row, opt)}
        # 兼容两种结构：新格式 {name, effect} / 旧格式纯字符串
        name = opt_data.get('name') if isinstance(opt_data, dict) else opt_data
        name = (name or '').strip()
        if not name:
            return {"ok": False, "msg": "角色 %s 第 %s 行无 %s 选项" % (ship_name, row, opt)}
        names.append(name)
    tmap = load_talent_map()
    # 原始面板（天赋面板效果按原始面板计算）
    raw_panel = {}
    matched_name, raw = app.get_raw_ship(ship_name)
    if raw is not None:
        raw_panel = raw.get('panel', {}).get('面板', {}) or {}
    eff = talent_to_effects(tmap, names, raw_panel)
    return {"ok": True, "msg": "OK", "panel": eff["panel"], "buffs": eff["buffs"],
            "enemy": eff["enemy"], "ship_bonus": eff["ship_bonus"], "cd": eff["cd"],
            "names": names, "code": code}


def load_talent_pvp() -> dict:
    """读取 知识库/talent_pvp.json（角色名 → 推荐编码），带模块级缓存。"""
    global _TALENT_PVP_CACHE
    if _TALENT_PVP_CACHE is not None:
        return _TALENT_PVP_CACHE
    pvp_file = os.path.join(KB_DIR, "底层数据", "产物", "talent_pvp.json")
    if not os.path.exists(pvp_file):
        _TALENT_PVP_CACHE = {}
        return _TALENT_PVP_CACHE
    try:
        with open(pvp_file, encoding="utf-8") as f:
            _TALENT_PVP_CACHE = json.load(f)
    except Exception:
        _TALENT_PVP_CACHE = {}
    return _TALENT_PVP_CACHE


def load_talent_pve() -> dict:
    """读取 知识库/talent_pve.json（角色名 → 推荐编码），带模块级缓存。

    该表通常缺失（PvE 点法暂与 PVP 一致），缺失时返回空 dict，
    由调用方（天赋问法处理器）回退到 PVP 编码并明确标注，不冒充 PvE 专属。"""
    global _TALENT_PVE_CACHE
    if _TALENT_PVE_CACHE is not None:
        return _TALENT_PVE_CACHE
    if not os.path.exists(TALENT_PVE_FILE):
        _TALENT_PVE_CACHE = {}
        return _TALENT_PVE_CACHE
    try:
        with open(TALENT_PVE_FILE, encoding="utf-8") as f:
            _TALENT_PVE_CACHE = json.load(f)
    except Exception:
        _TALENT_PVE_CACHE = {}
    return _TALENT_PVE_CACHE


def resolve_default_talent(app, ship_name: str, mode: str = 'pvp') -> dict:
    """解析角色默认天赋（普通计算=pvp 表 / 遭遇战=pve 表，暂无 pve 表时回退 pvp）。
    返回 resolve_talents 同结构；无推荐编码或角色无天赋时返回 ok=False。"""
    pvp = load_talent_pvp()
    code = pvp.get(ship_name)
    if not code:
        # 尝试别名匹配
        matched_name, _ = app.get_raw_ship(ship_name)
        if matched_name and matched_name != ship_name:
            code = pvp.get(matched_name)
    if not code:
        return {"ok": False, "msg": "角色 %s 在推荐天赋表中无记录，请手动输入天赋编码（如 1212）" % ship_name}
    return resolve_talents(app, ship_name, code)


class SetSessionTalent(BaseTool):
    """设置会话级天赋（四行二列编码，如 1212 = 第1行A/第2行B/第3行A/第4行B）。
    解析角色天赋为 buff/面板效果叠加到会话临时记忆，计算时自动生效。
    side=attacker 时增伤生效；side=defender 时减免生效。
    talent_code 缺省（空/不传）时使用 PVP 推荐表默认编码。"""
    name = 'set_session_talent'
    description = '设置会话级天赋选择。参数：ship_name（角色名）、side（attacker=攻击方增伤生效 / defender=防守方减免生效）、talent_code（可选，4 位数字编码每位 1=A 或 2=B，如 1212；缺省用 PVP 推荐表默认）。解析天赋表选中内容转为 buff 与面板效果，计算时自动叠加。'
    parameters = {
        'type': 'object',
        'properties': {
            'ship_name': {'type': 'string', 'description': '角色名（攻击方或防守方）'},
            'side': {'type': 'string', 'description': 'attacker（攻击方，增伤生效）或 defender（防守方，减免生效），默认 attacker'},
            'talent_code': {'type': 'string', 'description': '天赋编码（可选），4 位数字每位 1=A 或 2=B，如 1212；缺省用 PVP 推荐表默认编码'},
        },
        'required': ['ship_name'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        session_overrides = self.cfg.get('session_overrides', {})
        ship_name = params['ship_name']
        code = (params.get('talent_code') or '').strip()
        side = (params.get('side') or 'attacker').strip().lower()
        if side not in ('attacker', 'defender'):
            return "side 必须是 attacker 或 defender。"
        if code:
            r = resolve_talents(app, ship_name, code)
        else:
            r = resolve_default_talent(app, ship_name)
        if not r['ok']:
            return r['msg']
        # 天赋效果：按 side「整体覆盖」写入 session（用户口径 2026-09-14 修复）。
        # 原实现把增量直接 += 到 attack_panel / defense_panel / buffs / cd / enemy，
        # 从不撤销上一次天赋 —— 同一编码调两次会翻倍，换编码后旧天赋增量永远残留
        # （实测 Lv45 场景 命中 685.44 → 616.90 → 548.35 → 445.54，越改越偏）。
        # 现在只保留「最近一次」的天赋效果，由 _load_combatants 按 side 分别叠加：
        # attacker 的只作用于攻击方自身；defender 的进敌方属性；用户手工 buff/面板增量
        # 仍走 attack_panel/defense_panel/buffs 等独立通道，两者互不污染。
        session_overrides.setdefault('talent_effects', {})[side] = {
            'code': r.get('code', code),
            'names': list(r.get('names') or []),
            'panel': dict(r.get('panel') or {}),
            'buffs': dict(r.get('buffs') or {}),
            'enemy': dict(r.get('enemy') or {}),
            'ship_bonus': dict(r.get('ship_bonus') or {}),
            'cd': dict(r.get('cd') or {}),
        }
        parts = []
        for n in r['names']:
            info = load_talent_map().get(n, {})
            desc = info.get('desc', '')
            parts.append(n + '(' + desc + ')' if desc else n)
        panel_desc = ('；面板: ' + '、'.join('%s+%.1f' % (k, v) for k, v in r['panel'].items())) if r['panel'] else ''
        return "已记忆天赋 %s（%s）：%s → %s。效果：%s%s。计算时自动生效。" % (
            ship_name, side, r.get('code', code), '、'.join(r['names']), '；'.join(parts), panel_desc)
