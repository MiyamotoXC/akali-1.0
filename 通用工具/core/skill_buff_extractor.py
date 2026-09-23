# -*- coding: utf-8 -*-
"""
技能持续 buff 提取器（确定性规则引擎，不依赖 LLM）

从 raw_data.json 的技能原文（skills 字段）中识别"【X±N%，持续M秒】"类描述，
输出 CombatData.skill_buffs 结构化列表：
    [{"trigger": "skill|burst|support", "target": "self|enemy", "duration": 秒, "buffs": {标准键名: 数值}}, ...]

与 LLM 解析路径（prompts.py/parser.py）互为补充：
- 本模块：确定性、可离线运行、可单测，作为数据再生的主路径
- LLM 路径：已同步增加 skill_buffs 输出规则，供后续全量重解析使用

规则约定（与 prompts.py 的 LLM 规则保持一致）：
- 提取来源：SupportSkill / BattleSkill1 / BattleSkill2
- 不提取 LeaderSkill（旗舰技为全舰队共享，不写入单角色）
- 只提取带"持续N秒"的 buff；无"持续"的 buff 保持常驻（不动）
- 回复/护盾/免疫类（血量恢复、护盾、伤害吸收等）跳过
"""
import os
import re
from typing import Dict, List, Optional, Tuple

# 技能字段 → trigger
TRIGGER_MAP = {
    "SupportSkill": "support",
    "BattleSkill1": "skill",
    "BattleSkill2": "burst",
}

# 跳过类关键词（回复/护盾/免疫/召唤/点燃等不建模为属性 buff；
# 以及移动速度/对潜等非战斗建模属性、晶币/燃料/经验等经济属性）
_SKIP_KEYWORDS = [
    "血量每秒逐渐恢复", "每秒恢复", "回复", "恢复", "治疗", "治愈", "吸血",
    "护盾", "屏障", "伤害吸收", "免疫", "无敌", "召唤", "复活", "重生",
    "点燃", "燃烧", "冰冻", "眩晕", "嘲讽", "加速",
    # 航速已升级为角色底层面板通用字段（不再跳过）；移动速度/移速仍是非战斗建模属性
    "移动速度", "移速", "对潜",
    "经验", "晶币", "燃料",
]

# 属性百分比 buff：值(带%) → 标准键
_ATTR_PCT = {
    "炮击": "炮击值百分比增加", "雷击": "雷击值百分比增加", "航空": "航空值百分比增加",
    "轰炸": "轰炸值百分比增加", "鱼雷": "鱼雷值百分比增加", "装甲": "装甲值百分比增加",
    "对空": "对空值百分比增加", "机动": "机动值百分比增加", "命中": "命中值百分比增加",
    "幸运": "幸运值百分比增加", "装填": "装填值百分比增加", "索敌": "索敌值百分比增加",
}
# 属性固定值 buff：值(无%) → 标准键
# 航速为角色底层面板通用字段（节，不带 %），与幸运同类按固定值建模
_ATTR_FIXED = {
    "炮击": "炮击值固定增加", "雷击": "雷击值固定增加", "航空": "航空值固定增加",
    "轰炸": "轰炸值固定增加", "鱼雷": "鱼雷值固定增加", "装甲": "装甲值固定增加",
    "对空": "对空值固定增加", "机动": "机动值固定增加", "命中": "命中值固定增加",
    "幸运": "幸运值固定增加", "装填": "装填值固定增加", "索敌": "索敌值固定增加",
    "航速": "航速值固定增加",
}

# 特殊百分比 buff —— 具体关键词在前、通用关键词在后（顺序首匹配），
# 否则"暴击伤害加成"会被通用的"伤害加成"截胡误映射为全伤害增加
_SPECIAL_PCT = [
    ("攻击伤害减免", "全伤害减免"), ("攻击伤害加成", "全伤害增加"),
    ("全伤害减免", "全伤害减免"), ("全伤害增加", "全伤害增加"),
    ("暴击伤害", "暴击伤害增加"), ("爆伤", "暴击伤害增加"),
    ("伤害减免", "全伤害减免"), ("伤害加成", "全伤害增加"), ("减伤", "全伤害减免"),
    ("命中率", "命中率增加"), ("回避率", "回避率增加"), ("闪避", "回避率增加"),
    ("防暴击率", "防暴击率增加"), ("暴击率", "暴击率增加"),
    ("攻击力", "攻击力增加"),
]

# 伤害类型加成/减免
_DMG_INC = {
    "炮击": "炮击伤害增加", "雷击": "雷击伤害增加", "航空": "航空伤害增加",
    "轰炸": "轰炸伤害增加", "鱼雷": "鱼雷伤害增加", "近战": "近战伤害增加",
    "对空": "对空伤害增加", "高平炮": "高平炮伤害增加",
}
_DMG_RED = {
    "炮击": "炮击伤害减免", "雷击": "雷击伤害减免", "航空": "航空伤害减免",
    "轰炸": "轰炸伤害减免", "鱼雷": "鱼雷伤害减免", "近战": "近战伤害减免",
    "对空": "对空伤害减免", "高平炮": "高平炮伤害减免",
}
_SIZE_INC = {"小型": "小型攻击伤害加成", "中型": "中型攻击伤害加成", "大型": "大型攻击伤害加成"}
_SIZE_RED = {"小型": "小型攻击伤害减免", "中型": "中型攻击伤害减免", "大型": "大型攻击伤害减免"}

# 舰种专属增伤（对敌方某舰种的伤害加成，轻巡/重巡归入巡洋）
_TARGET_TYPE_KEYS = {
    "驱逐": "对驱逐伤害增加", "巡洋": "对巡洋伤害增加",
    "轻巡": "对巡洋伤害增加", "重巡": "对巡洋伤害增加",
    "雷巡": "对雷巡伤害增加", "战列": "对战列伤害增加", "战巡": "对战巡伤害增加",
    "航母": "对航母伤害增加", "轻母": "对轻母伤害增加", "装母": "对装母伤害增加",
    "航战": "对航战伤害增加", "重炮": "对重炮伤害增加",
    "陆基": "对陆基伤害增加",
}
_TGT_TYPE_ALT = "|".join(_TARGET_TYPE_KEYS)

# 目标类增伤的"尾部形态"：_BUFF_RE 要求 % 后紧跟 】/持续N秒，
# 而这些描述在 % 后还有文字（如「50%额外伤害」），属于正则盲区：
#   「对驱逐、轻巡、补给造成100%额外伤害」「对重巡目标增加50%额外伤害」
#   「对中小型目标增加25%额外伤害」「对大型目标增加30%额外伤害」「对航母造成：25%额外伤害」
#   「对陆基目标额外增加50%伤害」「对陆基目标伤害翻倍」
_TGT_TAIL_RE = re.compile(
    r'【[^】]*?对(?:敌方)?((?:' + _TGT_TYPE_ALT + r')(?:、(?:敌方)?(?:' + _TGT_TYPE_ALT + r'))*)'
    r'(?:舰灵|级|舰|目标)?[^】]*?(\d+(?:\.\d+)?)\s*%\s*(?:的)?(?:额外)?伤害[^】]{0,6}】')
# 体型组必须整词捕获（含"型"）：写成 (中小型|中大型|[小中大])型 会因交替命中
# "中小型"后还要求一个"型"而必然回溯失败，导致复合体型增伤整体漏提取
_SIZE_TAIL_RE = re.compile(
    r'【[^】]*?对(?:敌方)?(中小型|中大型|[小中大]型)(?:舰灵|级|舰|目标)?[^】]*?'
    r'(\d+(?:\.\d+)?)\s*%\s*(?:的)?(?:额外)?伤害[^】]{0,6}】')
# 「对X目标伤害翻倍」形态：无百分比数字，按增伤+100% 口径（引擎侧加法叠加）
_TGT_DOUBLE_RE = re.compile(
    r'【[^】]*?对(?:敌方)?((?:' + _TGT_TYPE_ALT + r')(?:、(?:敌方)?(?:' + _TGT_TYPE_ALT + r'))*)'
    r'(?:舰灵|级|舰|目标)?[^】]*?伤害翻倍[^】]{0,6}】')

# 敌方目标提示词（紧邻【 之前的上下文中出现则 target=enemy）
# "空中单位"：指敌方空中单位（buff 目标为空中单位时均为对其 debuff，
# 如"范围内所有空中单位【攻击力-20%】"），即使无"敌对/敌方"前缀也判 enemy
_ENEMY_HINTS = ("敌方", "敌人", "敌对", "空中单位", "目标", "被命中", "受到攻击", "被击中的")

# 范围过滤类型（scope 列表元素的 kind）
SCOPE_KIND_NATION = "nationality"      # 按所属（阵营/国籍）
SCOPE_KIND_SHIP_TYPE = "ship_type"     # 按舰种（航母/巡洋/战列…）
SCOPE_KIND_NAME = "name"               # 按指定角色名（如 瑞鹤）
SCOPE_KIND_SHIP_CLASS = "ship_class"   # 按舰级（如 翔鹤级）
SCOPE_KIND_SPEED = "speed"             # 按航速门槛（如 航速≤25）
SCOPE_KIND_LUCK = "luck"               # 按幸运门槛（如 幸运≥30）
SCOPE_KIND_POSITION = "position"       # 按站位（出战位=前排）
SCOPE_KIND_SPECIAL = "special_type"    # 按子类别（如 特型驱逐）

# 触发条件类型（condition 字段的 kind）
COND_COURSE = "course"    # 航向：T优/T劣势/同航/反航
COND_AIR = "air"          # 制空：确保/优势/均势/劣势/丧失
COND_HP = "hp"            # 血线：血量高于/低于 X%
COND_PERIOD = "period"    # 时段：夜战/白天
COND_ROLE = "role"        # 站位：作为支援舰/旗舰
COND_KILL = "kill"        # 击沉目标
COND_TIME = "time"        # 开场 N 秒内
# 旗舰技专属（队伍构成类条件）
COND_EXISTS = "exists"        # 队伍中存在 N 名满足 of 的舰灵
COND_TEAM_COUNT = "team_count"  # 队伍人数条件
COND_FORMATION = "formation"  # 阵型（复纵阵/轮型阵…）
COND_MODE = "mode"            # 模式：pvp（演习/竞技）

# 特型驱逐成员（权威来源：知识库/攻略_md/7.1 争锋竞技场简易百科
# 「特型驱逐包括：晓响雷电、四雪，改造后也算特驱」；信赖=响·改 一并计入）。
# 特型驱逐不是 panel 的舰种字段（舰种只有"驱逐"），故此处显式枚举。
_SPECIAL_TYPE_MEMBERS = {
    "特型驱逐": frozenset({"晓", "响", "雷", "电", "吹雪", "白雪", "初雪", "深雪", "信赖"}),
}

# 比较符归一（全角/半角统一）
_OP_MAP = {"≥": ">=", "≤": "<=", "＞": ">", "＜": "<", "=": "=",
           ">=": ">=", "<=": "<=", ">": ">", "<": "<"}

# 技能原文用字与 panel「所属」字段的异体字对照（全量扫描确认：仅「凤棲/凤栖」一对）。
# 不归一会导致「所属队伍中的凤棲舰灵」解析不出国籍过滤，光环被放大成全队生效。
_NATION_TEXT_ALIASES = {"凤棲": "凤栖"}

# 舰级连接词（「且埃塞克斯级」「中的翔鹤级」需剥离到最后一个连接词之后）
_CLASS_JOIN_WORDS = ("的", "且", "与", "及", "中", "至", "或")

# 默认 raw_data 路径（相对本文件：知识库/底层数据/产物/raw_data.json）
_DEFAULT_RAW_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "知识库", "底层数据", "产物", "raw_data.json"))
# 原型舰历史表（知识库/底层数据/源文件/原型舰历史.json），含每船权威舰级字段
_HISTORY_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "知识库", "底层数据", "源文件", "原型舰历史.json"))

_REF = None  # 缓存：{names:set, nations:set, ship_types:set, ship_class_by_name:dict}


def _norm_class(val: str) -> Optional[str]:
    """历史表舰级值归一：取到第一个"级/型"为止（"翔鹤级航空母舰"→"翔鹤级"，
    "1934A型驱逐舰"→"1934A型"）。无级/型字样返回 None。"""
    if not val:
        return None
    m = re.search(r"^(.*?[级型])", val)
    return m.group(1) if m else None


def load_reference(raw_path: str = None) -> dict:
    """从 raw_data.json 构建范围识别参考集：所有船名 / 所属(国籍) / 舰种；
    并从 原型舰历史.json 构建 船名→舰级（归一为"X级/型"）映射。"""
    global _REF
    path = raw_path or _DEFAULT_RAW_PATH
    names, nations, ship_types = set(), set(), set()
    try:
        with open(path, encoding="utf-8") as f:
            raw = __import__("json").load(f)
        for rec in raw.values():
            panel = rec.get("panel", {})
            if isinstance(panel, dict):
                if panel.get("舰名"):
                    names.add(str(panel["舰名"]))
                if panel.get("所属"):
                    nations.add(str(panel["所属"]))
                if panel.get("舰种"):
                    ship_types.add(str(panel["舰种"]))
    except (OSError, ValueError):
        pass
    # 船名 → 舰级（权威来源：原型舰历史.json 的 ship_class 字段）
    ship_class_by_name = {}
    try:
        with open(_HISTORY_PATH, encoding="utf-8") as f:
            hist = __import__("json").load(f)
        for rec in hist.get("data", []):
            nm = rec.get("ship_name")
            cls = _norm_class(rec.get("ship_class") or "")
            if nm and cls:
                ship_class_by_name[str(nm)] = cls
    except (OSError, ValueError):
        pass
    _REF = {"names": names, "nations": nations, "ship_types": ship_types,
            "ship_class_by_name": ship_class_by_name}
    return _REF


def _get_ref(ref=None):
    if ref is not None:
        return ref
    if _REF is None:
        load_reference()
    return _REF or {"names": set(), "nations": set(), "ship_types": set()}


def _normalize_desc(desc: str) -> str:
    """清洗 buff 描述：去空白、去『额外』『再次』『大幅』等修饰词。"""
    s = re.sub(r"\s+", "", desc)
    s = s.replace("额外", "").replace("再次", "").replace("大幅", "").replace("小幅", "")
    s = s.replace("加成", "").replace("提升", "").replace("提高", "").replace("增强", "")
    s = s.replace("获得", "").replace("增加", "").replace("上升", "").replace("上升", "")
    # 去重复的"值"字（装甲值值+30% 之类异常）
    s = re.sub(r"值值", "值", s)
    return s


def _map_buff(desc: str, is_percent: bool) -> Optional[str]:
    """把 buff 描述映射到标准键名。映射失败返回 None。

    desc 示例：装甲值+30%（percent）、机动+225（fixed）、命中率+20%、雷击伤害减免+15%
    """
    d = _normalize_desc(desc)

    # 1) 伤害类型加成/减免（优先于属性，如"航空伤害加成"→航空伤害增加）
    # 触发词判定前先剥离『额外』（如「航空伤害额外+15%」「自身航空伤害额外+5%」），
    # 否则该类描述因无 加成/伤害增加 等触发词而被静默丢弃
    trig = desc.replace("额外", "")
    for attr, key in _DMG_INC.items():
        if f"{attr}伤害" in d and (
            "加成" in trig or "伤害增加" in trig or "伤害提高" in trig or "伤害提升" in trig
            # 以"<attr>伤害"结尾的裸形式（如「鱼雷机的鱼雷伤害+35%」）也算增伤触发
            or trig.endswith(f"{attr}伤害")
        ):
            return key
    for attr, key in _DMG_RED.items():
        if f"{attr}伤害" in d and "减免" in desc:
            return key

    # 2) 大小型攻击伤害加成/减免（优先于通用伤害 buff，避免子串误匹配）
    for size, key in _SIZE_INC.items():
        if size in d and ("伤害加成" in desc or "伤害增加" in desc):
            return key
    for size, key in _SIZE_RED.items():
        if size in d and "伤害减免" in desc:
            return key

    # 2.5) 目标类别增伤/减伤：对(敌方)X舰种/体型 伤害加成/提升/增加
    # （如「对敌方驱逐伤害加成+30%」「对小型舰伤害提升+25%」）
    s_t = desc.replace(" ", "").replace("额外", "")
    if "伤害减免" not in s_t:
        m_t = re.search(rf'对(?:敌方)?({_TGT_TYPE_ALT})(?:舰灵|级|舰|目标)?(?:的)?伤害', s_t)
        if m_t:
            return _TARGET_TYPE_KEYS[m_t.group(1)]
        m_s = re.search(r'对(?:敌方)?(小型|中型|大型)(?:舰灵|级|舰|目标)?(?:的)?伤害', s_t)
        if m_s:
            return _SIZE_INC[m_s.group(1)]
    else:
        m_t = re.search(rf'对(?:敌方)?({_TGT_TYPE_ALT})(?:舰灵|级|舰|目标)?(?:的)?伤害', s_t)
        if m_t:
            return f"对{_TARGET_TYPE_KEYS[m_t.group(1)][1:-2]}减免"
        m_s = re.search(r'对(?:敌方)?(小型|中型|大型)(?:舰灵|级|舰|目标)?(?:的)?伤害', s_t)
        if m_s:
            return _SIZE_RED[m_s.group(1)]

    # 3) 特殊百分比 buff（按最长关键词优先）
    # 使用原始 desc 匹配：normalize 移除了 "加成/增加/减免" 等词，
    # 导致 _SPECIAL_PCT 中以这些词结尾的关键词无法匹配
    if is_percent:
        for kw, key in _SPECIAL_PCT:
            if kw in desc:
                return key

    # 4) 属性 buff：百分比 / 固定值
    if is_percent:
        for attr, key in _ATTR_PCT.items():
            if f"{attr}值" in d or (attr in d and "伤害" not in d and "减免" not in d):
                return key
    else:
        for attr, key in _ATTR_FIXED.items():
            if f"{attr}值" in d or attr in d:
                return key

    return None


def _scope_clause(text: str, bracket_start: int) -> str:
    """取紧邻【之前的"范围描述从句"。

    左边界取最近的 】/■/①②…⑨（bullet），再取该段最后一行非空文本，
    兼容"①所属舰队中的翔鹤级舰灵\\n【航空+30%】"这类跨换行的范围描述。
    """
    left = 0
    for m in ("】", "■"):
        p = text.rfind(m, 0, bracket_start)
        if p >= 0 and p + 1 > left:
            left = p + 1
    for b in "①②③④⑤⑥⑦⑧⑨":
        p = text.rfind(b, 0, bracket_start)
        if p >= 0 and p + 1 > left:
            left = p + 1
    chunk = text[left:bracket_start]
    # 兼容两种换行形态：真实换行 与 字面 "\n"（反斜杠+n 两字符）——统一转真实换行后切分，
    # 否则「若成功击沉目标，出战位的舰灵\n【装填+30%】」会把条件与范围混成一段，
    # 导致「目标」命中敌方提示、把 team 误判成 enemy。
    chunk = chunk.replace("\\n", "\n")
    segs = [s for s in chunk.split("\n") if s.strip()]
    clause = segs[-1] if segs else ""
    clause = re.sub(r"[■\s①②③④⑤⑥⑦⑧⑨]+", "", clause)
    return clause


def _parse_filters(clause: str, ref: dict) -> list:
    """从范围从句解析 scope 过滤列表（可叠加，如「洛蒙瑞亚的驱逐舰灵」→国籍+舰种）。"""
    filters = []
    if not clause:
        return filters
    for src, dst in _NATION_TEXT_ALIASES.items():
        clause = clause.replace(src, dst)
    # 指定角色名（避免把"翔鹤级"误判为名为"翔鹤"）
    # 注意：ref 的 nations/ship_types/names 都是 set，直接遍历顺序跨进程随机
    # （Python 字符串哈希随机化）→ 产物 scope 顺序不稳定，构建侧幂等校验会一直报差异。
    # 故此处一律排序遍历；names 取最长命中（多处同句时取最具体的舰名，取代原来的随机首个）。
    for name in sorted(ref.get("names", ()), key=len, reverse=True):
        if name and name in clause and (name + "级") not in clause:
            filters.append({"kind": SCOPE_KIND_NAME, "value": name})
            break
    # 舰级（X级）——正则可能吞"所属舰队中(的)"等连接词（→"队中伊势级"），也可能截断长级名
    # （"沙恩霍斯特级"取后4字→"恩霍斯特级"），故取 {1,6} 并收集全部匹配后清理连接词
    vals = []
    for m in re.finditer(r"([一-鿿]{1,6})级", clause):
        val = m.group(1)
        changed = True
        while changed:   # "中的翔鹤"→翔鹤、"队中伊势"→伊势、"中黑暗界"→黑暗界
            changed = False
            for sep in ("的", "中"):
                if sep in val:
                    val = val.rsplit(sep, 1)[-1]
                    changed = True
        if val and val + "级" not in vals:
            vals.append(val + "级")
    for v in vals:
        filters.append({"kind": SCOPE_KIND_SHIP_CLASS, "value": v})
    # 国籍（所属）——并列（顿号/、）时收集全部，同 kind 多值按"任一匹配"理解
    for n in sorted(ref.get("nations", ())):
        if n and n in clause:
            filters.append({"kind": SCOPE_KIND_NATION, "value": n})
    # 舰种——同上，如「出战位中的轻巡、重巡舰灵」收集 轻巡+重巡 两个过滤项
    for st in sorted(ref.get("ship_types", ())):
        if st and st in clause:
            filters.append({"kind": SCOPE_KIND_SHIP_TYPE, "value": st})
    return filters


# 改造船 → 改造前基座映射（权威源，唯一一份；main._fill_kai_talents 从此导入）。
# 基座为游戏内角色名：当基名存在同名/同名括号多角色且舰级不同时，必须靠此映射消歧，
# 否则改船会错绑别船的舰级。游戏事实（用户确认）：列克星敦·改 是 CV-2（bare 列克星敦，
# 列克星敦级）的改造，与 CV-16 蓝太太（埃塞克斯级）无关——早期数据曾把两者弄混。
KAI_BASE = {
    "拉菲·改": "拉菲(DD-724)",
    "列克星敦·改": "列克星敦",
}


def match_ship_class(name: str, class_val: str) -> bool:
    """舰级匹配（权威来源：原型舰历史.json 的 ship_class 字段）：
    船的舰级（归一为"X级/型"）与 buff 舰级相同即命中——本级主名与姊妹船都由真实
    舰级数据覆盖（瑞鹤→翔鹤级、恐怖→黑暗界级、黑暗界自身→黑暗界级）。
    改造前后舰级不变：改船按基座舰级匹配——先查 KAI_BASE 显式映射，再剥"·改"取基名；
    基座名历史表缺失且含括号时回退裸名（列克星敦(CV-16)→列克星敦）。
    历史表缺失该船时退回名称包含（级名去'级'后包含于船名）。"""
    if not name or not class_val:
        return False
    by_name = _get_ref().get("ship_class_by_name") or {}
    ship_cls = by_name.get(name)
    if ship_cls is None and name.endswith("·改"):
        base = KAI_BASE.get(name) or name[:-2]
        ship_cls = by_name.get(base)
        if ship_cls is None and "(" in base:
            ship_cls = by_name.get(base.split("(")[0])
    want = _norm_class(class_val)
    if ship_cls and want:
        return ship_cls == want
    stem = class_val[:-1] if class_val.endswith("级") else class_val
    return bool(stem) and stem in name


def _infer_scope(text: str, bracket_start: int, ref: dict, desc_part: str = "") -> tuple:
    """推断 buff 目标维度与范围过滤。

    返回 (target, scope, clause)：clause 为空表示本括号未带范围从句，
    调用方在"连续括号共享主语"场景下需继承前一括号的结果。
    - target ∈ {self, team, enemy, air}
        self  : 仅释放者自身
        team  : 我方（全队或按 scope 过滤的子集）
        enemy : 敌方减益
        air   : 仅敌方空中单位
    - scope : list[{"kind","value"}]，仅 team 时有效；空列表=全队。
    """
    clause = _scope_clause(text, bracket_start)
    # 空中单位专属
    if "空中单位" in clause:
        return ("air", [], clause)
    # 敌方
    if any(h in clause for h in _ENEMY_HINTS):
        return ("enemy", [], clause)
    # 自身（显式 自身/自己，且非「中的X舰灵」复合范围）
    if ("自身" in clause or "自己" in clause) and not re.search(r"中的?[^舰]*舰灵", clause):
        return ("self", [], clause)
    # 含「全队/全体/我方全体/所属舰队/出战位/友军/我军/舰队中/舰灵/成员」→ 我方（team）
    if any(k in clause or k in desc_part for k in
           ("全队", "全体", "我方全体", "所属舰队", "所属队伍", "出战位",
            "友军", "我军", "舰队中", "舰灵", "成员")):
        return ("team", _parse_filters(clause, ref), clause)
    # 默认：自身（释放者），保底
    return ("self", [], clause)


# 航向条件：长词优先（T劣势 先于 T劣，避免部分匹配）
_COURSE_COND = [
    ("T优势", "T优"), ("T劣势", "T劣势"), ("T劣", "T劣势"),
    ("同航", "同航"), ("反航", "反航"), ("T优", "T优"),
]
# 制空条件：匹配「制空状态为X时 / 制空X时」（X 保留完整描述：均势或以上/劣势或丧失/均势、优势、确保）
_AIR_COND_RE = re.compile(r"制空(?:状态)?(?:为|是)?([^时。；;\n]*?)时")
# 血线条件：血量（低于/少于/高于/大于/不低于）X%
_HP_COND_RE = re.compile(r"血量(?:低于|少于|高于|大于|不低于)(\d+(?:\.\d+)?)\s*%")
# 开场时间：开场 N 秒内
_TIME_COND_RE = re.compile(r"开场\s*(\d+)\s*秒内")


def _infer_condition(text: str, bracket_start: int) -> Optional[dict]:
    """从 buff 的【】内外文本推断触发条件，返回 condition dict 或 None。

    条件可能出现在两处：
    - 【】内部：如「【T劣势时，命中+30%，持续15秒】」（条件在 buff 描述内）
    - 【】外部紧邻从句：如「若自身血量低于50%\n【炮击伤害加成+15%，持续8秒】」
      条件也可能跨段：如「■夜战时，使用探照灯照射目标\n■出战位的舰灵\n【命中率+30%】」
      （条件在上上段）——因此上下文取【之前的最后两段，而非仅紧邻一段。

    输出 condition（kind 见 COND_* 常量）：
    - {"kind":"course","value":"T劣势"}           航向
    - {"kind":"air","value":"劣势或丧失"}          制空（保留原文片段）
    - {"kind":"hp","op":">"|"<","value":0.5}       血线（高于=1.0 即全血线以下）
    - {"kind":"period","value":"night"|"day"}      时段
    - {"kind":"role","value":"support"|"flagship"} 站位
    - {"kind":"kill"}                              击沉目标
    - {"kind":"time","op":"<=","value":10}         开场 N 秒内
    无法识别返回 None（调用方省略该字段）。
    """
    # 条件上下文：上一个】之后（buff 边界）到【之前，按■/换行分段取最后两段。
    # 取上一个】而非最近■：条件可跨■段（「■夜战时…\n■出战位的舰灵\n【命中率+30%】」），
    # 且不会混入其他 buff 的【】内容（上一个】已截断）。
    left = 0
    p = text.rfind("】", 0, bracket_start)
    if p >= 0:
        left = p + 1
    chunk = text[left:bracket_start].replace("\\n", "\n")
    segs = [s.strip() for s in re.split(r"[\n■]", chunk) if s.strip()]
    clause = "\n".join(segs[-2:])
    # 【】内部：取该 buff 描述文本（【 到结束）
    end = text.find("】", bracket_start)
    inner = text[bracket_start + 1:end] if end > bracket_start else ""
    ctx = (clause or "") + "\n" + (inner or "")

    # 1) 航向（T优/T劣势/同航/反航）
    for kw, val in _COURSE_COND:
        if kw in ctx:
            return {"kind": COND_COURSE, "value": val}
    # 2) 制空：先试「制空…时」完整片段，兜底裸「制空」
    m = _AIR_COND_RE.search(ctx)
    if m:
        frag = (m.group(1) or "").strip("、，, ")
        frag = frag.replace("制空", "").strip("、，, ")  # 「劣势或制空丧失」→「劣势或丧失」
        if frag:
            return {"kind": COND_AIR, "value": frag}
    if "制空" in ctx:
        return {"kind": COND_AIR, "value": "丧失"}
    # 3) 血线（高于/低于 X%）
    m = _HP_COND_RE.search(ctx)
    if m:
        try:
            pct = float(m.group(1)) / 100.0
        except (ValueError, TypeError):
            pct = 0.5
        op = ">" if ("高于" in ctx or "大于" in ctx or "不低于" in ctx) else "<"
        return {"kind": COND_HP, "op": op, "value": pct}
    # 4) 时段（夜战/黑夜/夜间 → night；白天/昼战 → day）
    if any(k in ctx for k in ("夜战", "黑夜", "夜间")):
        return {"kind": COND_PERIOD, "value": "night"}
    if any(k in ctx for k in ("白天", "昼战")):
        return {"kind": COND_PERIOD, "value": "day"}
    # 5) 站位（作为支援舰/作为旗舰）
    if "作为支援舰" in ctx or "作为支援" in ctx:
        return {"kind": COND_ROLE, "value": "support"}
    if "作为旗舰" in ctx or "作为队长" in ctx:
        return {"kind": COND_ROLE, "value": "flagship"}
    # 6) 击沉目标
    if "击沉" in ctx and ("目标" in ctx or "后" in ctx):
        return {"kind": COND_KILL}
    # 7) 开场 N 秒内
    m = _TIME_COND_RE.search(ctx)
    if m:
        try:
            return {"kind": COND_TIME, "op": "<=", "value": int(m.group(1))}
        except (ValueError, TypeError):
            pass
    return None


# 匹配：【描述+数值%（可带）[，持续N秒]】
_BUFF_RE = re.compile(
    r"【([^】]*?[^0-9+\-])([+-]?\d+(?:\.\d+)?)\s*(%?)\s*(?:[，,、]\s*(?:持续|效果持续)\s*(\d+(?:\.\d+)?)\s*秒)?】"
)


# 属性值类 buff 键后缀（复合拆分只认这类键，避免「雷击、炮击伤害+20%」被误拆）
_ATTR_KEY_SUFFIX = ("值百分比增加", "值固定增加")


def _map_buff_keys(desc: str, is_percent: bool) -> List[str]:
    """buff 描述 → 标准键列表。

    支持复合属性并列：「对空、装甲值+300%」拆为 对空/装甲值 两条属性 buff。
    仅当整句映射为属性值键、且按分隔符拆出的每个片段也都映射为属性值键时才拆分；
    其余形态（含伤害类混合描述）维持整句 _map_buff 的原结果，避免误映射。
    """
    key = _map_buff(desc, is_percent)
    if key is None:
        return []
    parts = [p.strip() for p in re.split(r"[、，,]", desc) if p.strip()]
    if len(parts) > 1 and key.endswith(_ATTR_KEY_SUFFIX):
        keys = []
        for p in parts:
            pk = _map_buff(p, is_percent)
            if pk is None or not pk.endswith(_ATTR_KEY_SUFFIX):
                return [key]  # 有片段映射不出属性键 → 维持整句结果
            if pk not in keys:
                keys.append(pk)
        return keys or [key]
    return [key]


def extract_skill_buffs(skills: Dict) -> List[dict]:
    """从单个角色的 skills 字典（raw_data.json 结构）提取 skill_buffs 列表。"""
    if not isinstance(skills, dict):
        return []
    out: List[dict] = []
    for field, trigger in TRIGGER_MAP.items():
        text = skills.get(field)
        if not text or not isinstance(text, str):
            continue
        # 连续括号共享主语继承：如「出战位的航母、轻母、装母\n【命中+45%，持续10秒】\n【装甲值+45%…】」，
        # 第二个括号取不到从句，需沿用前一括号的 target/scope/condition。
        # 同块判定：两个【】之间无 ■/①-⑨ 分隔符。
        prev_end = None
        prev_target = None
        prev_scope = None
        prev_cond = None
        for m in _BUFF_RE.finditer(text):
            desc_part, num_str, pct, dur_str = m.group(1), m.group(2), m.group(3), m.group(4)
            same_block = (
                prev_end is not None
                and not any(ch in text[prev_end:m.start()] for ch in ("■",) + tuple("①②③④⑤⑥⑦⑧⑨"))
            )
            # 只有带"持续N秒"的才进入 skill_buffs
            if not dur_str:
                continue
            if any(kw in desc_part for kw in _SKIP_KEYWORDS):
                continue
            try:
                value = float(num_str) / 100.0 if pct else float(num_str)
            except (ValueError, TypeError):
                continue
            if value == 0:
                continue
            key = _map_buff(desc_part, bool(pct))
            if key is None:
                continue
            keys = _map_buff_keys(desc_part, bool(pct)) or [key]
            target, scope, clause = _infer_scope(text, m.start(), _get_ref(), desc_part)
            cond = _infer_condition(text, m.start())
            # 从句为空且与前一 buff 同块 → 继承主语（目标/范围/条件）
            if not clause and same_block and prev_target is not None:
                target, scope, cond = prev_target, prev_scope, prev_cond
            item = {
                "trigger": trigger,
                "target": target,
                "duration": float(dur_str),
                "buffs": {k: value for k in keys},
            }
            # 范围过滤（仅 team 时有意义；self/enemy/air 为空列表）
            if scope:
                item["scope"] = scope
            # 条件识别（T劣/制空/被命中/血线/时段/站位等，无则省略）
            if cond:
                item["condition"] = cond
            # 记录本括号结果供后续同块继承
            prev_end, prev_target, prev_scope, prev_cond = m.end(), target, scope, cond
            # 同一技能内同键合并（如"命中率额外+5%"多段、复合属性多键）
            merged = None
            for it in out:
                if it["trigger"] == trigger and it["target"] == target \
                        and it.get("condition") == item.get("condition") \
                        and it["duration"] == item["duration"] \
                        and any(k in it["buffs"] for k in keys):
                    merged = it
                    break
            if merged is not None:
                # 累加后保留最多两位小数，避免浮点残留
                for k in keys:
                    merged["buffs"][k] = round(merged["buffs"].get(k, 0.0) + value, 2)
            else:
                out.append(item)
    return out


# ======================================================================
# 旗舰技（LeaderSkill）细分识别：范围 / 条件 / 计数缩放
# ======================================================================
# 旗舰技原文形态（■分块、条件与范围常跨块/跨行），示例：
#   翔鹤   ■作为旗舰时/所属舰队中的八咫舰灵【航空伤害加成+30%】
#          ■翔鹤级航母【航空伤害加成额外+15%】
#   丹阳   ■存在幸运≥30的舰灵时，所属舰队舰灵【命中率额外+5%】
#   雪风   ■所属舰队中，至少存在3名幸运≥20的舰灵时，自身【对空伤害加成+67%】
#   伊势   ■作为旗舰时/所属舰队中航速≤25的舰灵【对空+32%】
#   提尔比茨 ■作为旗舰/舰队人数=6时，所属舰队的舰灵【攻击伤害减免+15%】
#   埃塞克斯 ■舰队中航母≥3时，敌方单位【攻击伤害加成-20%】
#           且每多存在1名埃塞克斯级舰灵时，敌方单位额外【炮击伤害加成-4%】(最高12%)
#   纳尔逊 ■使用复纵阵时【炮击伤害加成+5%】

# 分段判定：含以下关键词的从句片段视为"条件片段"，其余视为"范围/目标片段"
_COND_SEG_HINTS = ("存在", "至少", "多于", "人数", "血量", "阵", "昼战", "夜战",
                   "演习", "竞技")
# 条件片段补充判定：数字后紧跟"时"（如「尤奈特舰灵≥3时」「舰队人数=6时」），
# 百分比形态（「自身血量≥30%时」）由"血量"提示词覆盖，此处排除带 % 的情况
_COUNT_THEN_SHI_RE = re.compile(r"\d\s*时")

# 属性门槛（幸运 / 航速）——既可能是条件（存在幸运≥30）也可能是范围（航速≤25的舰灵）
_ATTR_THRESHOLD_RE = re.compile(r"(幸运|航速)\s*([<>≥≤=]=?)\s*(\d+(?:\.\d+)?)")
# 「存在 N 名」「至少存在 N 名」
_EXIST_N_RE = re.compile(r"存在\s*(?:至?少)?\s*(\d+)\s*名")
# 「多于 N 名」→ 换算为 ≥ N+1
_MORE_THAN_RE = re.compile(r"多于\s*(\d+)\s*名")
# 类别计数门槛：「尤奈特舰灵≥3时」「轻巡≥3时」「重巡≥3时」
_CATEGORY_COUNT_RE = re.compile(r"^\s*(.{1,12}?)\s*(≥|≤|>=|<=|=|>|<)\s*(\d+)\s*时?\s*$")
# 队伍人数：「人数为4-5时」/「舰队人数=6时」/「人数≤3时」
_TEAM_RANGE_RE = re.compile(r"人数(?:为)?\s*(\d+)\s*[-~－—]\s*(\d+)")
_TEAM_CMP_RE = re.compile(r"人数\s*([=≥≤><]=?)\s*(\d+)")
# 血量（旗舰技形态额外支持"超过"）
_LEADER_HP_RE = re.compile(
    r"血量\s*(低于|少于|高于|大于|不低于|超过|≥|≤|>=|<=|=)\s*(\d+(?:\.\d+)?)\s*%")
# 阵型
_FORMATION_RE = re.compile(r"(复纵阵|轮型阵|轮形阵|单纵阵|梯形阵|复横阵|单横阵|梯形|单纵|复纵|轮型)")
# 模式（PVP）
_PVP_RE = re.compile(r"演习|竞技")
# 缩放标记：「每多1名X」「每多存在1名X」
_SCALE_RE = re.compile(r"每多(?:存在)?\s*\d*\s*名")
# 缩放封顶：「(最高18%)」写在【】之后
_SCALE_CAP_RE = re.compile(r"[（(]\s*(?:最高|最多|上限)\s*(\d+(?:\.\d+)?)\s*%\s*[）)]")


def _norm_op(op: str) -> str:
    """比较符归一（全角→半角）。"""
    return _OP_MAP.get((op or "").strip(), (op or "").strip() or ">=")


def _parse_threshold_filters(seg: str, ref: dict) -> list:
    """从片段解析属性门槛过滤（幸运/航速），可多个（跨 kind 取交集）。"""
    out = []
    for m in _ATTR_THRESHOLD_RE.finditer(seg or ""):
        attr, op, val = m.group(1), m.group(2), m.group(3)
        try:
            num = float(val)
        except (ValueError, TypeError):
            continue
        out.append({"kind": SCOPE_KIND_LUCK if attr == "幸运" else SCOPE_KIND_SPEED,
                    "op": _norm_op(op), "value": num})
    return out


def _is_cond_segment(seg: str) -> bool:
    """判断从句片段是否为条件片段。"""
    if not seg:
        return False
    if any(h in seg for h in _COND_SEG_HINTS):
        return True
    if "%" in seg:
        return False
    return bool(_COUNT_THEN_SHI_RE.search(seg))


def _parse_cond_segment(seg: str, ref: dict) -> Optional[dict]:
    """把条件片段解析为 condition dict，无法识别返回 None。"""
    s = (seg or "").strip()
    if not s:
        return None
    # 1) 队伍人数
    m = _TEAM_RANGE_RE.search(s)
    if m:
        try:
            return {"kind": COND_TEAM_COUNT, "op": "between",
                    "min": int(m.group(1)), "max": int(m.group(2))}
        except (ValueError, TypeError):
            pass
    m = _TEAM_CMP_RE.search(s)
    if m:
        try:
            return {"kind": COND_TEAM_COUNT, "op": _norm_op(m.group(1)),
                    "value": int(m.group(2))}
        except (ValueError, TypeError):
            pass
    # 2) 血量
    m = _LEADER_HP_RE.search(s)
    if m:
        op_raw = m.group(1)
        op = ">" if op_raw in ("高于", "大于", "不低于", "超过", "≥", ">=") else "<"
        try:
            return {"kind": COND_HP, "op": op, "value": float(m.group(2)) / 100.0}
        except (ValueError, TypeError):
            pass
    # 3) 阵型
    m = _FORMATION_RE.search(s)
    if m and ("阵" in s or "使用" in s):
        return {"kind": COND_FORMATION, "value": m.group(1)}
    # 4) 模式（演习/竞技 = PVP）
    if _PVP_RE.search(s):
        return {"kind": COND_MODE, "value": "pvp"}
    # 4.5) 时段（夜战/昼战）——须先于类别计数判定，否则「夜战时」落进数字门槛分支
    if any(k in s for k in ("夜战", "黑夜", "夜间")):
        return {"kind": COND_PERIOD, "value": "night"}
    if any(k in s for k in ("昼战", "白天", "白昼")):
        return {"kind": COND_PERIOD, "value": "day"}
    # 5) 存在 N 名满足门槛的舰灵（幸运/航速）
    if "存在" in s or "至少" in s or "多于" in s:
        ths = _parse_threshold_filters(s, ref)
        if ths:
            count = 1
            m = _EXIST_N_RE.search(s)
            if m:
                try:
                    count = int(m.group(1))
                except (ValueError, TypeError):
                    count = 1
            m = _MORE_THAN_RE.search(s)
            if m:   # 「多于3名」→ ≥4
                try:
                    count = int(m.group(1)) + 1
                except (ValueError, TypeError):
                    pass
            return {"kind": COND_EXISTS, "count": count, "of": ths}
    # 6) 类别计数门槛：「尤奈特舰灵≥3时」「轻巡≥3时」
    m = _CATEGORY_COUNT_RE.match(s)
    if m and _COUNT_THEN_SHI_RE.search(s):
        subject, op, n = m.group(1).strip(), m.group(2), m.group(3)
        try:
            count = int(n)
        except (ValueError, TypeError):
            return None
        if "人数" in subject:
            return {"kind": COND_TEAM_COUNT, "op": _norm_op(op), "value": count}
        filters = _parse_category_filters(subject, ref)
        if filters:
            return {"kind": COND_EXISTS, "count": count, "of": filters}
    # 7) 裸类别计数（无"时"，如「航母≥3」）
    m = _CATEGORY_COUNT_RE.match(s)
    if m:
        subject, n = m.group(1).strip(), m.group(3)
        try:
            count = int(n)
        except (ValueError, TypeError):
            return None
        filters = _parse_category_filters(subject, ref)
        if filters:
            return {"kind": COND_EXISTS, "count": count, "of": filters}
    return None


def _clean_class_name(val: str) -> str:
    """剥离舰级名前的连接词：「且埃塞克斯」→「埃塞克斯」、「队中伊势」→「伊势」。"""
    changed = True
    while changed:
        changed = False
        for sep in _CLASS_JOIN_WORDS:
            if sep in val:
                val = val.rsplit(sep, 1)[-1]
                changed = True
    return val


def _parse_category_filters(subject: str, ref: dict) -> list:
    """从类别主语（如「尤奈特舰灵」「轻巡」「埃塞克斯级」「奥鲁加驱逐」）解析过滤列表。"""
    subject = (subject or "").strip()
    if not subject:
        return []
    for src, dst in _NATION_TEXT_ALIASES.items():
        subject = subject.replace(src, dst)
    # 门槛词残留保护：不含幸运/航速门槛时再走通用过滤
    filters = _parse_threshold_filters(subject, ref)
    cleaned = _ATTR_THRESHOLD_RE.sub("", subject)
    # 舰级（X级）
    for m in re.finditer(r"([一-鿿]{1,8})级", cleaned):
        val = _clean_class_name(m.group(1))
        if val:
            filters.append({"kind": SCOPE_KIND_SHIP_CLASS, "value": val + "级"})
    # 国籍 / 舰种（沿用 _parse_filters 的并列收集口径；排序遍历保证顺序确定）
    for n in sorted(ref.get("nations", ())):
        if n and n in cleaned:
            filters.append({"kind": SCOPE_KIND_NATION, "value": n})
    for st in sorted(ref.get("ship_types", ())):
        if st and st in cleaned:
            filters.append({"kind": SCOPE_KIND_SHIP_TYPE, "value": st})
    for sp in sorted(_SPECIAL_TYPE_MEMBERS):
        if sp in cleaned:
            filters.append({"kind": SCOPE_KIND_SPECIAL, "value": sp})
    return filters


def _parse_leader_clause(clause: str, ref: dict) -> dict:
    """解析旗舰技从句 → {"target","scope","condition","exclude_self","scaling","noncombat"}。

    clause 为若干原文片段以「，」拼接的结果（保留片段边界，便于按片段判定
    条件 vs 范围）。判定顺序：
    - 条件片段（存在/人数/血量/阵型/演习竞技/「X≥N时」）→ condition
    - 其余片段 → 目标（敌方/自身/全队）与范围过滤（国籍/舰种/舰级/航速/幸运/站位/子类别）
    - 「每多1名X」片段 → scaling 标记（计数缩放，逐名递增）
    """
    out = {"target": "team", "scope": [], "condition": None,
           "exclude_self": False, "scaling": None}
    c = re.sub(r"[■\s①②\u2003-\u206f]+", "", clause or "")
    # 统一 bullet 符号
    for b in "②③④⑤⑥⑦⑧⑨":
        c = c.replace(b, "")
    if not c:
        return out
    # 非战斗技能（港区生产类，如「于港区中使用/晶币产量」）→ 整条不产出光环
    if "非战斗技能" in c or "港区中" in c:
        out["noncombat"] = True
        return out

    segs = [s.strip() for s in c.split("，") if s.strip()]
    scale_of: list = []
    for seg in segs:
        # 缩放标记：「每多1名X」「每多存在1名X」——其类别只描述"计谁的数"，
        # 不构成接收者范围（如「每多1名奥鲁加驱逐时，自身」接收者是自身）
        is_scale_seg = bool(_SCALE_RE.search(seg))
        if is_scale_seg:
            out["scaling"] = {"of": [], "base": 0}
            scale_of = _parse_category_filters(_SCALE_RE.sub("", seg), ref)
        # 除自身外 / 自身以外 → 目标仍为全队，仅排除旗舰自身
        if "除自身外" in seg or "自身以外" in seg:
            out["exclude_self"] = True
        if _is_cond_segment(seg):
            cond = _parse_cond_segment(seg, ref)
            if cond is not None:
                out["condition"] = cond
                continue
        # 目标判定
        if any(k in seg for k in ("敌方", "敌对", "敌人")):
            out["target"] = "enemy"
        elif seg in ("自身", "自己") or (
                seg.endswith("自身") and "以外" not in seg and "除" not in seg):
            out["target"] = "self"
        if is_scale_seg:
            continue
        # 范围过滤（目标类片段）
        out["scope"].extend(_parse_threshold_filters(seg, ref))
        out["scope"].extend(_parse_category_filters(seg, ref))
        if "出战位" in seg:
            out["scope"].append({"kind": SCOPE_KIND_POSITION, "value": "front"})
    if out["scaling"] is not None:
        out["scaling"]["of"] = scale_of
    # 范围过滤去重（同 kind+value 只保留一条；同 kind 多值按"任一匹配"）
    # 去重后再按 (kind, value, op) 排序 —— scope 只是过滤条件、与顺序无关，
    # 固定顺序让产物（data.json / raw_data.json）跨进程逐字节稳定（构建幂等的前提）。
    seen = set()
    uniq = []
    for f in out["scope"]:
        k = (f.get("kind"), str(f.get("value")), f.get("op"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    uniq.sort(key=lambda f: (str(f.get("kind")), str(f.get("value")), str(f.get("op"))))
    out["scope"] = uniq
    return out


# 航速 buff 专属形态：「【航速+5节】」「【航速-3节】」——尾部单位"节"使通用 _BUFF_RE
# （要求 % 后紧跟 】或 持续N秒）匹配不到，故单列一条；非旗舰技中暂无此形态。
_SPEED_BUFF_RE = re.compile(r"【\s*航速\s*([+-]?\d+(?:\.\d+)?)\s*(?:节)?\s*】")


def _leader_buffs_in_line(line: str) -> list:
    """从单行文本提取【…】buff 列表 → [{"key","value","cap","self_hint"}]。

    - 跳过「最多/最高/上限」封顶条目（如【航空伤害加成最多+50%】是缩放上限而非 buff），
      但把其数值回传为 cap，供缩放条目封顶使用。
    - "cap" 取【】后紧跟的「(最高N%)」形态；"self_hint" 标记【】内含「自身」（目标=旗舰自身）。
    """
    out = []
    for m in _SPEED_BUFF_RE.finditer(line):
        try:
            value = float(m.group(1))
        except (ValueError, TypeError):
            continue
        if value:
            out.append({"key": "航速值固定增加", "value": value, "cap": None,
                        "self_hint": False})
    for m in _BUFF_RE.finditer(line):
        desc_part, num_str, pct = m.group(1), m.group(2), m.group(3)
        is_cap = any(kw in desc_part for kw in ("最多", "最高", "上限"))
        if not is_cap and any(kw in desc_part for kw in _SKIP_KEYWORDS):
            continue
        try:
            value = float(num_str) / 100.0 if pct else float(num_str)
        except (ValueError, TypeError):
            continue
        if value == 0:
            continue
        key = _map_buff(desc_part, bool(pct))
        if key is None:
            continue
        if is_cap:
            out.append({"key": key, "value": 0.0, "cap": abs(value), "is_cap": True,
                        "self_hint": False})
            continue
        tail = line[m.end():m.end() + 14]
        cap = None
        mc = _SCALE_CAP_RE.search(tail)
        if mc:
            try:
                cap = float(mc.group(1)) / 100.0
            except (ValueError, TypeError):
                cap = None
        out.append({"key": key, "value": value, "cap": cap,
                    "self_hint": "自身" in desc_part})
    return out


def extract_flagship_auras(skills: Dict) -> List[dict]:
    """从 LeaderSkill 提取结构化旗舰光环列表（细分范围 + 条件 + 计数缩放）。

    返回 [{"target","scope","condition"|None,"buffs"} | {"target","scope","scaling","buffs"}]

    - target: self（仅旗舰自身）/ team（我方，按 scope 过滤）/ enemy（敌方减益）
    - scope  : 接收者过滤列表，元素见 SCOPE_KIND_*；空列表=全队
    - condition: 触发条件 dict，见 COND_*；None=无条件（整场生效）
    - scaling: {"of":[过滤],"base":N,"step":单步增量,"cap":封顶幅度(正)}
               「每多存在1名X，额外【机动-6%】(最高18%)」→ 超出 base 的每名按 step 叠加、
               总幅度不超过 cap。base 取前一光环的同类 exists 计数（过滤集相同），否则 0。

    解析口径：逐行扫描，累积从句行（carry），遇到含【】的行即按当前 carry 解析一次
    目标/范围/条件；同一行内多个【】共享同一从句（如【回避率+18%】【机动+90】）；
    下一行若仍带【】且 carry 为空，则继承上一光环的目标/范围/条件。
    """
    if not isinstance(skills, dict):
        return []
    text = skills.get("LeaderSkill")
    if not text or not isinstance(text, str) or not text.strip():
        return []
    ref = _get_ref()
    # 兼容真实换行与字面 "\n"
    lines = [re.sub(r"[■①-⑨]+", "", ln).strip()
             for ln in text.replace("\\n", "\n").split("\n")]
    out: List[dict] = []
    carry: List[str] = []
    prev = None          # 上一个光环条目（供同块继承 / 缩放 base 推导）
    prev_exists = None   # 上一个 exists 条件（供缩放 base 推导）

    def _flush(line: str):
        nonlocal carry, prev, prev_exists
        buffs = _leader_buffs_in_line(line)
        if not buffs:
            carry = []
            return
        # 封顶条目（【航空伤害加成最多+50%】）不单独成条，只给前一光环/当前行补 cap
        cap = None
        self_hint = False
        real = []
        for b in buffs:
            if b.get("is_cap"):
                if cap is None:
                    cap = b.get("cap")
                continue
            real.append(b)
            if b.get("cap") is not None and cap is None:
                cap = b.get("cap")
            if b.get("self_hint"):
                self_hint = True
        if not real:
            # 全是封顶条目 → 回写给上一个光环（同一缩放块的 cap 常另起一行）
            if prev is not None and prev.get("scaling") is not None and cap is not None:
                prev["scaling"]["cap"] = abs(cap)
            carry = []
            return
        buffs = real
        clause = "，".join(s for s in carry if s)
        parsed = _parse_leader_clause(clause, ref) if clause else None
        if parsed is None and prev is not None:
            # 无从句（连续【】）→ 继承上一光环的目标/范围/条件
            parsed = {"target": prev["target"], "scope": list(prev.get("scope") or []),
                      "condition": prev.get("condition"),
                      "exclude_self": prev.get("exclude_self", False),
                      "scaling": prev.get("scaling")}
        if parsed is None:
            parsed = {"target": "team", "scope": [], "condition": None,
                      "exclude_self": False, "scaling": None}
        if parsed.get("noncombat"):
            carry = []
            return
        if self_hint:
            parsed["target"] = "self"
        agg: Dict[str, float] = {}
        for b in buffs:
            agg[b["key"]] = round(agg.get(b["key"], 0.0) + b["value"], 2)
        item = {"target": parsed["target"]}
        if parsed["scope"]:
            item["scope"] = parsed["scope"]
        if parsed.get("exclude_self"):
            item["exclude_self"] = True
        if parsed.get("scaling") is not None:
            sc = dict(parsed["scaling"])
            sc["step"] = next(iter(agg.values()), 0.0)
            if cap is not None:
                sc["cap"] = abs(cap)
            # base：前一光环为同类 exists 且过滤集相同时沿用其计数，否则 0
            if prev_exists and prev_exists.get("of") == sc.get("of"):
                sc["base"] = int(prev_exists.get("count", 0) or 0)
            item["scaling"] = sc
        elif parsed.get("condition") is not None:
            item["condition"] = parsed["condition"]
        item["buffs"] = agg
        out.append(item)
        prev = item
        cond = parsed.get("condition")
        if isinstance(cond, dict) and cond.get("kind") == COND_EXISTS:
            prev_exists = cond
        elif not parsed.get("scaling"):
            prev_exists = None
        carry = []

    for ln in lines:
        if not ln:
            continue
        if "【" in ln:
            _flush(ln)
        else:
            carry.append(ln)
    return out


def extract_flagship_buffs(skills: Dict) -> Dict[str, float]:
    """（兼容接口）把旗舰技光环压平成 {标准键名: 数值}。

    仅供展示/旧逻辑使用：会丢掉范围与条件，**计算请以 flagship_buffs
    （extract_flagship_auras 的结构化列表）为准**。
    """
    flat: Dict[str, float] = {}
    for item in extract_flagship_auras(skills):
        for k, v in (item.get("buffs") or {}).items():
            flat[k] = round(flat.get(k, 0.0) + float(v), 2)
    return flat


def _target_bonus_tail_items(text: str, period_only: bool = False):
    """扫描文本中尾部带残留文字的目标类增伤（_BUFF_RE 匹配不到的盲区形态）。

    并列多舰种（如「对驱逐、轻巡、补给造成100%额外伤害」）时每个舰种取全值——
    引擎按防守方体型/舰种选键，单次命中只会读到其中一个键，不会叠加。
    联合体型（中小型/中大型）同理拆到各体型键。
    period_only=True 时仅保留带时段条件（夜战/昼战）的匹配项。
    """
    def _period_hit(pos: int) -> bool:
        cond = _infer_condition(text, pos)
        return isinstance(cond, dict) and cond.get("kind") == COND_PERIOD

    try:
        for m in _TGT_TAIL_RE.finditer(text):
            if period_only and not _period_hit(m.start()):
                continue
            val = round(float(m.group(2)) / 100.0, 2)
            for t in [x.strip().replace("敌方", "") for x in m.group(1).split("、")]:
                key = _TARGET_TYPE_KEYS.get(t)
                if key:
                    yield key, val
        for m in _SIZE_TAIL_RE.finditer(text):
            if period_only and not _period_hit(m.start()):
                continue
            val = round(float(m.group(2)) / 100.0, 2)
            size = m.group(1)
            if size == "中小型":
                sizes = ("小型", "中型")
            elif size == "中大型":
                sizes = ("中型", "大型")
            else:
                sizes = (size,)
            for s_ in sizes:
                yield _SIZE_INC[s_], val
        # 「对X目标伤害翻倍」形态：按增伤 +100% 计（引擎侧加法叠加，等效翻倍）
        for m in _TGT_DOUBLE_RE.finditer(text):
            if period_only and not _period_hit(m.start()):
                continue
            for t in [x.strip().replace("敌方", "") for x in m.group(1).split("、")]:
                key = _TARGET_TYPE_KEYS.get(t)
                if key:
                    yield key, 1.0
    except (ValueError, TypeError):
        return


# ==================== 条件增伤静态提取（2026-09-04 定稿口径） ====================
# 用户定稿：技能描述「XX条件下造成/增加 X% 额外伤害」类增伤，条件不建模，
# 全部直接默认算入对应增伤区（该技能出手期间生效的 static_buffs）。
# 既有通道分工（防双计）：
#   对+舰种/体型目标词形态 → _TGT_TAIL_RE/_SIZE_TAIL_RE（dev 对比 sbuffs /
#   夜战时段 period 通道）负责，本提取器跳过；
#   拉古兹 → raguz_bonus 通道，跳过；
#   夜战/昼战 + 对目标词 → period 通道，跳过；时段 + 无目标词（如绫波
#   「夜战时该技能增加50%额外伤害」period 通道拿不到）→ 本提取器收。
# 无目标词/该技能/自身条件（燃烧/减速/血线等）→ 全伤害增加；
# 明确舰灵名目标（青叶「若被命中目标是昆西」）→ 对{名}伤害增加（语义存档，
# 引擎动态键按敌方舰种取值，舰灵名键暂不生效，待引擎扩展）。
_COND_EXTRA_BLOCK_RE = re.compile(
    r'[^】%\n*]{0,16}?'
    r'(?:额外增加|增加|造成)\s*[：:]?\s*'
    r'(?P<val>\d+(?:\.\d+)?)\s*%\s*(?:的)?(?:额外)?伤害')
# tail 通道（_TGT_TAIL_RE/_SIZE_TAIL_RE）管辖判定：块内含 对+舰种/体型词 即跳过
# （用词表交替整体判定，避免分组捕获非贪婪截断漏判，如「对重巡目标…」截成「重」）
_TGT_WORD_HINT_RE = re.compile(
    r'对(?:敌方)?((?:' + _TGT_TYPE_ALT + r'|中小型|中大型|[小中大]型)(?:舰灵|级|舰|目标)?)')
# 块前紧邻文字里的舰灵名目标主语（「若被命中目标是昆西 【造成：50%额外伤害】」）
_NAMED_TARGET_RE = re.compile(r'目标[是为]([\u4e00-\u9fa5]{2,6})\s*【?')


def extract_static_buffs_from_text(text: str) -> Dict[str, float]:
    """提取「XX条件下造成/增加 X% 额外伤害」型条件增伤 → {标准增伤键: 小数}。

    条件不建模，数值默认算入对应增伤区（调用方在该技能出手期间临时叠加）。
    键归一：无目标/该技能/自身条件 → 全伤害增加；舰灵名目标 → 对{名}伤害增加；
    对舰种/体型词形态不在本提取器（既有 tail 通道负责，防双计）。
    分块：【】块与 ■ 裸块都扫（罗德尼/爱宕等「■如对方处于XX状态，造成：X%额外伤害」
    不带【】，只扫【】会漏）。
    """
    out: Dict[str, float] = {}
    if not isinstance(text, str) or not text:
        return out
    blocks = []  # (块文本, 块在原文起始位置)
    cursor = 0
    for m in re.finditer(r'【([^】]*)】|([^【】]+)', text):
        if m.group(1) is not None:
            blocks.append((m.group(1), m.start()))
            cursor = m.end()
        else:
            for piece in m.group(2).split('■'):
                pos = text.find(piece, cursor)
                if pos < 0:
                    continue
                cursor = pos + len(piece)
                blocks.append((piece, pos))
    for block, bpos in blocks:
        if '拉古兹' in block or _TGT_WORD_HINT_RE.search(block):
            continue
        sm = _COND_EXTRA_BLOCK_RE.search(block)
        if not sm or '*' in sm.group(0):
            continue
        named = _NAMED_TARGET_RE.search(text[max(0, bpos - 24):bpos])
        if named:
            key = f'对{named.group(1)}伤害增加'
        else:
            key = '全伤害增加'
        val = round(float(sm.group('val')) / 100.0, 2)
        if val:
            out[key] = round(out.get(key, 0.0) + val, 4)
    return out


def extract_period_buffs_from_text(text: str) -> Dict[str, float]:
    """提取「夜战/昼战」时段条件且无持续时间的 buff。

    语义（与"场合"条件区分）：时段不是单次出手——带「持续N秒」走窗口管线；
    无时间则在整个时段内一直生效，返回 {标准buff键: 数值} 供调用方并入基础 buffs。
    """
    out: Dict[str, float] = {}
    if not isinstance(text, str) or not text:
        return out
    for m in _BUFF_RE.finditer(text):
        desc_part, num_str, pct, dur_str = m.group(1), m.group(2), m.group(3), m.group(4)
        if dur_str:
            continue  # 带持续时间 → 窗口管线，不在此列
        cond = _infer_condition(text, m.start())
        if not (isinstance(cond, dict) and cond.get("kind") == COND_PERIOD):
            continue
        try:
            value = float(num_str) / 100.0 if pct else float(num_str)
        except (TypeError, ValueError):
            continue
        if value == 0:
            continue
        key = _map_buff(desc_part, bool(pct))
        if key:
            out[key] = out.get(key, 0.0) + value
    # 尾部形态盲区（% 后有文字），如【夜战时，对驱逐造成：额外30%伤害】
    for key, val in _target_bonus_tail_items(text, period_only=True):
        out[key] = round(out.get(key, 0.0) + val, 4)
    return out


# 拉古兹阵营增伤的两种原文形态：
#   「对邪祟之物（拉古兹）造成50%额外伤害」/「对拉古兹单位造成20%额外伤害」→ 造成X%额外伤害
#   「此伤害对拉古兹单位造成伤害提高60%」→ 伤害提高X%
_RAGUZ_TAIL_RE = re.compile(r'拉古兹[^】]*?造成\s*(\d+(?:\.\d+)?)\s*%\s*(?:的)?(?:额外)?伤害')
_RAGUZ_INC_RE = re.compile(r'拉古兹[^】]*?伤害(?:提高|提升|增加)\s*(\d+(?:\.\d+)?)\s*%')


def extract_raguz_bonus(skills: Dict) -> float:
    """从技能描述提取拉古兹阵营增伤 → raguz_bonus（小数，如 0.6 表示 60%）。

    遭遇战 boss 默认属于拉古兹阵营，该增伤由模拟器叠加到「全伤害增加」。
    raguz_bonus 为角色级单一数值；同一角色多个技能各带拉古兹增伤时互斥生效
    （不同技能释放才触发），取最大值为保守口径。
    """
    if not isinstance(skills, dict):
        return 0.0
    best = 0.0
    for field in TRIGGER_MAP:
        text = skills.get(field)
        if not text or not isinstance(text, str) or "拉古兹" not in text:
            continue
        for m in _RAGUZ_TAIL_RE.finditer(text):
            best = max(best, float(m.group(1)) / 100.0)
        for m in _RAGUZ_INC_RE.finditer(text):
            best = max(best, float(m.group(1)) / 100.0)
    return round(best, 2)


# ==================== CD 效果提取（重置 / 固定减秒） ====================

# 目标技能名 → CombatData 槽位；
# "炮击/雷击技能"是舰种语境下对通常技的称呼（八咫驱逐/轻巡/雷巡的通常技为炮击/雷击类），归入 skill
_CD_TARGET_MAP = {
    "通常技": "skill", "必杀技": "burst",
    "炮击、雷击技能": "skill", "炮击技能": "skill", "雷击技能": "skill",
}
# 条件触发关键词（暴击/几率/命中后/若目标是X）：确定性时间轴不模拟随机触发，整条跳过
_CD_CONDITIONAL_KEYWORDS = ("暴击", "几率", "机率", "命中后", "若目标")
# 冷却重置：「重置自身通常技的冷却时间」「重置通常技的冷却」
_CD_RESET_RE = re.compile(r'重置(?:自身)?(通常技|该技能)(?:的)?冷却时间?')
# 固定减秒：「必杀技的技能冷却时间减少2秒」「炮击、雷击技能冷却时间减少2秒」
# 目标与"冷却时间减少"之间允许 的/技能/，等连接文字（不跨【】与换行）
_CD_REDUCE_RE = re.compile(
    r'(通常技|必杀技|炮击、雷击技能|炮击技能|雷击技能)[^【】\n]*?冷却时间减少(\d+)秒')


def _cd_effect_clause(text: str, match_start: int, match_end: int = 0) -> str:
    """取 CD 效果匹配所在的范围从句。

    - 匹配在【】内：取紧邻【之前的从句（复用 _scope_clause）
    - 匹配为裸语句（如「①立即重置自身通常技的冷却时间」）：
      "自身"可能在匹配词内部（重置自身通常技），故取整行（前后都取）
    """
    bracket = text.rfind("【", 0, match_start)
    if bracket != -1 and text.find("】", bracket) >= match_start:
        return _scope_clause(text, bracket)
    line_start = max(text.rfind("\n", 0, match_start), text.rfind("■", 0, match_start),
                     text.rfind("：", 0, match_start))
    after = text.find("\n", match_end or match_start)
    if after == -1:
        after = len(text)
    return re.sub(r"[■\s①②③④⑤⑥⑦⑧⑨]", "", text[line_start + 1:after])


def _cd_effect_scope(text: str, match_start: int, match_end: int = 0) -> dict:
    """推断 CD 效果的作用范围（目标者）。

    返回 {'target': 'self'} 或
         {'target': 'team', 'scope': [...], 'exclude_self': bool,
          'flagship_only': bool, 'flagship_name': str|None}
    """
    clause = _cd_effect_clause(text, match_start, match_end)
    if "自身" in clause and "舰灵" not in clause:
        return {"target": "self"}
    filters = []
    for f in _parse_filters(clause, _get_ref()):
        # 单字名（如"雷"）多为舰种词（雷巡）的子串误命中 → 丢弃
        if f["kind"] == "name" and len(f["value"]) < 2:
            continue
        # 舰级误抓连接字（"队的独立级"）→ 截到最后一个"的"之后
        if f["kind"] == "ship_class" and "的" in f["value"]:
            f = dict(f, value=f["value"].split("的")[-1])
        filters.append(f)
    return {
        "target": "team",
        "scope": filters,
        "exclude_self": "自身以外" in clause,
        # 「旗舰舰灵」「旗舰为X舰灵」→ 仅旗舰
        "flagship_only": "旗舰" in clause,
        # 「旗舰为列加杜舰灵」→ 限定旗舰名（无则 None）
        "flagship_name": _extract_flagship_name(clause),
    }


def _extract_flagship_name(clause: str) -> Optional[str]:
    """从「旗舰为X舰灵」形态提取限定旗舰名，无则 None。
    X 为阵营名（国籍，如「旗舰为列加杜舰灵」）时不作为旗舰名——阵营限定已由
    scope 的 nationality 过滤表达（配合 flagship_only 即"旗舰且该国籍"），
    写成 flagship_name 会要求旗舰的名字叫阵营名，永远匹配不上成死条件。"""
    m = re.search(r"旗舰为(.+?)舰灵", clause)
    if m:
        val = m.group(1).strip()
        if val and val in _get_ref().get("nations", ()):
            return None
        return val
    return None


def extract_cd_effects(skills: Dict) -> Dict[str, List[dict]]:
    """从技能描述提取无条件的 CD 效果（重置/固定减秒）→ CombatData.cd_effects。

    结构：{slot: [效果]}，slot 为释放源技能（skill/burst/support），
    效果 = {"action": "reset"|"reduce", "target": "skill"|"burst",
            "scope": {...}, 可选 "seconds": N}
    条件触发（暴击/几率/命中后/若目标是X）不模拟 → 跳过；
    目标为支援技的效果不存在，不产出。
    """
    if not isinstance(skills, dict):
        return {}
    out: Dict[str, List[dict]] = {}
    for field, trigger in TRIGGER_MAP.items():
        text = skills.get(field)
        if not text or not isinstance(text, str):
            continue
        effects: List[dict] = []

        def _build(action, target, scope, seconds=None):
            eff = {"action": action, "target": target, "scope": scope}
            if seconds is not None:
                eff["seconds"] = seconds
            effects.append(eff)

        # 重置类
        for m in _CD_RESET_RE.finditer(text):
            # 条件上下文：所在【】内部 + 范围从句
            bracket = text.rfind("【", 0, m.start())
            ctx = ""
            if bracket != -1 and text.find("】", bracket) >= m.start():
                ctx += text[bracket:m.start()]
            ctx += _cd_effect_clause(text, m.start(), m.end())
            if any(kw in ctx for kw in _CD_CONDITIONAL_KEYWORDS):
                continue
            tgt_name = m.group(1)
            if tgt_name == "该技能":
                target = trigger   # 重置自身当前技能（无条件实例不存在，防御性处理）
            else:
                target = _CD_TARGET_MAP.get(tgt_name, "skill")
            _build("reset", target, _cd_effect_scope(text, m.start(), m.end()))

        # 固定减秒类
        for m in _CD_REDUCE_RE.finditer(text):
            bracket = text.rfind("【", 0, m.start())
            ctx = ""
            if bracket != -1 and text.find("】", bracket) >= m.start():
                ctx += text[bracket:m.start()]
            ctx += _cd_effect_clause(text, m.start(), m.end())
            if any(kw in ctx for kw in _CD_CONDITIONAL_KEYWORDS):
                continue
            target = _CD_TARGET_MAP.get(m.group(1))
            if target is None:
                continue
            _build("reduce", target, _cd_effect_scope(text, m.start(), m.end()), float(m.group(2)))

        if effects:
            out[trigger] = effects
    return out


# 技能循环 CD 权威正则（单一实现）：main.py 重建管线 / patch.py skill_cd 子命令 /
# 开发者端 app.py 对比页重建解析共用本函数，禁止再复制正则。
# 优先「冷却时间X秒」，其次「每X秒+动词」（动词前允许 ≤6字填充，如"依次发动/在目标位置释放"）、
# 「每X秒内最多发动1次」（排除"此效果"限频）；锚定动词避免把
# 「每10秒，全伤害增加15%」这类技能内 buff 触发句误当 CD
LOOP_CD_RE = re.compile(
    r'冷却时间\s*(\d+(?:\.\d+)?)\s*秒'
    r'|每\s*(\d+(?:\.\d+)?)\s*秒[^\n，,。；;]{0,6}?(?:发动|进入|施放|释放|使用|发射|进行)'
    r'|(?<!此效果)每\s*(\d+(?:\.\d+)?)\s*秒内最多(?:发动|触发|释放|使用|施放|进行)\s*\d*\s*次'
)


def extract_loop_cd(text: str) -> Optional[float]:
    """提取技能循环 CD（秒）：「冷却时间X秒」/「每X秒+动词」/「每X秒内最多N次」。

    用于 LLM 未给出 CD（cd=0）而描述含循环发动的技能；无命中返回 None。"""
    if not isinstance(text, str) or not text:
        return None
    m = LOOP_CD_RE.search(text)
    if not m:
        return None
    return float(m.group(1) or m.group(2) or m.group(3))


def extract_duration_contrib(skills: Dict) -> Dict[str, float]:
    """返回该角色所有持续 buff 对标准键的贡献总和 {键名: 总和}。

    只统计 target=self 且为正向的 buff（这些才可能被 LLM 写进角色自身常驻 buffs），
    敌方减益（target=enemy）不参与自身常驻扣除。
    """
    contrib: Dict[str, float] = {}
    for item in extract_skill_buffs(skills):
        if item["target"] != "self":
            continue
        for k, v in item["buffs"].items():
            if v > 0:
                # 累加后保留最多两位小数，避免浮点残留
                contrib[k] = round(contrib.get(k, 0.0) + v, 2)
    return contrib

# ======================================================================
# 技能文本 buff 提取（自由文本口径）：己方 buffs 与敌方减益分桶。
# 单一来源：开发者端对比链路（data_api._resolve_skill）与后续增量解析
# 共用此实现；「技能没写清的用角色底层」等口径见 resolve_attack_weapon
# （formula_engine）。2026-09-05 新增，勿在调用方另建平行正则。
# ======================================================================

_ENEMY_TARGET_MARK = '被命中的敌方单位'
# 阵营限定角色属性 buff（■/①尤奈特角色炮击+20%，不在【】内 → _BUFF_RE 盲区）。
# 注意：`(?:角色)?` 整词可选——写作 `角色?` 会要求必须有「角」字，漏掉「对空+30%」等
_TEAM_ATTR_BUFF_RE = re.compile(
    r'(尤奈特|八咫|洛蒙瑞亚|奥鲁加|列加杜|纳谢尔|凤栖|沃尔克)?\s*(?:角色)?'
    r'(炮击|雷击|航空|轰炸|鱼雷|装甲|对空|机动|命中|幸运|装填)值?\s*'
    r'([+＋]\d+(?:\.\d+)?)\s*(%?)')
# 敌方减益键名归一（「被命中的敌方单位」块内；抗性/抵抗 为同义变体）
_ENEMY_DEBUFF_KEY = {'燃烧抵抗': '点燃抵抗', '点燃抵抗': '点燃抵抗',
                     '燃烧抗性': '点燃抵抗', '点燃抗性': '点燃抵抗'}


def _is_action_line(stripped: str) -> bool:
    """行动行判定：■/①②③/• 等序号符开头（用于重置敌方减益区）。"""
    return bool(re.match(r'^[■①②③④⑤⑥⑦⑧•·]', stripped or ''))


def extract_own_buffs_from_text(text: str) -> Dict[str, float]:
    """从技能文本提取**己方** buff（属性增益、伤害增益、条件增伤等）。

    - 敌方减益剥离：「被命中的敌方单位」之后的【】块是敌方 debuff，不并入己方；
      由 extract_enemy_debuffs_from_text 单独提取。
    - 阵营限定角色属性 buff（■/①队伍里尤奈特角色炮击+20%，对空+30%）：
      非【】文本由 _TEAM_ATTR_BUFF_RE 扫描（剔除【】块防与 _BUFF_RE 双计）。
    """
    buffs = {}
    enemy_mode = False
    own_chunks = []
    for ln in (text or '').splitlines():
        stripped = ln.strip()
        if _is_action_line(stripped) and _ENEMY_TARGET_MARK not in ln:
            enemy_mode = False          # 新的行动行 → 退出敌方减益区
        if _ENEMY_TARGET_MARK in ln:
            enemy_mode = True
            continue
        if not enemy_mode:
            for m in _TEAM_ATTR_BUFF_RE.finditer(re.sub(r'【[^】]*】', '，', ln)):
                _scope, attr, num, pct = m.groups()
                try:
                    v = float(num) / 100.0 if pct else float(num)
                except (TypeError, ValueError):
                    continue
                if v:
                    key = f'{attr}值百分比增加' if pct else f'{attr}值固定增加'
                    buffs[key] = buffs.get(key, 0) + v
            own_chunks.append(ln)

    own_text = '\n'.join(own_chunks)
    for m in _BUFF_RE.finditer(own_text):
        desc_part, num_str, pct, _dur = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            value = float(num_str) / 100.0 if pct else float(num_str)
        except (TypeError, ValueError):
            continue
        if value == 0:
            continue
        for key in _map_buff_keys(desc_part, bool(pct)):
            buffs[key] = buffs.get(key, 0) + value

    for key, val in _target_bonus_tail_items(own_text):
        buffs[key] = round(buffs.get(key, 0) + val, 4)
    for key, val in extract_static_buffs_from_text(own_text).items():
        buffs[key] = round(buffs.get(key, 0) + val, 4)
    return buffs


def extract_enemy_debuffs_from_text(text: str) -> Dict[str, float]:
    """提取「被命中的敌方单位」标记后的敌方减益【】块 → {标准键: 值}。

    键映射：燃烧抵抗/抗性、点燃抵抗/抗性 → 点燃抵抗；其余块（装甲值-15% 等）
    走 _map_buff 标准映射（装甲值百分比增加 等防守方口径键）。敌方减益由对比
    链路作用于防守方（如 装甲×(1+值)），不并入己方 buffs。"""
    debuffs = {}
    enemy_mode = False
    for ln in (text or '').splitlines():
        stripped = ln.strip()
        if _is_action_line(stripped) and _ENEMY_TARGET_MARK not in ln:
            enemy_mode = False
            continue
        if _ENEMY_TARGET_MARK in ln:
            enemy_mode = True
            continue
        if not enemy_mode:
            continue
        for m in _BUFF_RE.finditer(ln):
            desc_part, num_str, pct, _dur = m.group(1), m.group(2), m.group(3), m.group(4)
            try:
                value = float(num_str) / 100.0 if pct else float(num_str)
            except (TypeError, ValueError):
                continue
            d = desc_part.strip()
            std = _ENEMY_DEBUFF_KEY.get(d) or _map_buff(d, bool(pct))
            if std and value:
                debuffs[std] = debuffs.get(std, 0) + value
    return debuffs


_RAMP_RE = re.compile(
    r'([\u4e00-\u9fa5]+值?)?每(\d+(?:\.\d+)?)秒[+＋](\d+(?:\.\d+)?)%，最多[+＋](\d+(?:\.\d+)?)%'
)
_RAMP_ATTR_MAP = {
    **{f'{attr}值': f'{attr}值百分比增加' for attr in
       ('炮击', '雷击', '航空', '轰炸', '鱼雷', '装甲', '对空', '机动', '命中', '幸运', '装填')},
    **{attr: f'{attr}值百分比增加' for attr in
       ('炮击', '雷击', '航空', '轰炸', '鱼雷', '装甲', '对空', '机动', '命中', '幸运', '装填')},
}


def extract_ramp_buffs(skills: dict) -> list:
    """提取“每 X 秒增加 Y%，最多增加 Z%”的递增属性光环。"""
    if not isinstance(skills, dict):
        return []
    merged = {}
    for field in ('SupportSkill', 'BattleSkill1', 'BattleSkill2', 'LeaderSkill'):
        text = skills.get(field)
        if not isinstance(text, str) or not text:
            continue
        for match in _RAMP_RE.finditer(text):
            attr, interval, step, cap = match.groups()
            key = _RAMP_ATTR_MAP.get(attr or '')
            if not key:
                continue
            item = {
                'attr': key,
                'step': float(step) / 100.0,
                'interval': float(interval),
                'cap': float(cap) / 100.0,
                'night_disabled': '夜战' in text,
            }
            merge_key = (item['attr'], item['interval'])
            previous = merged.get(merge_key)
            if previous:
                previous['step'] = max(previous['step'], item['step'])
                previous['cap'] = max(previous['cap'], item['cap'])
            else:
                merged[merge_key] = item
    return list(merged.values())
