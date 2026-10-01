# -*- coding: utf-8 -*-
"""tools shared."""
import copy
import json
import os
import re
from typing import Optional



# ======================================================================
# 常量与辅助
# ======================================================================

VALID_COURSES = {"同航", "反航", "T优势", "T劣势"}
VALID_AIRS = {"确保", "优势", "均势", "劣势", "丧失"}

# 航向别名归一化：前端按钮/默认值用简写 "T优"/"T劣"，常量表用标准名 "T优势"/"T劣势"
# 统一在此归一化，调用方无需重复处理
_COURSE_ALIAS = {"T优": "T优势", "T劣": "T劣势", "T劣势优势": "T优势"}


def _normalize_course(course: str) -> str:
    """航向简写归一化为常量表标准名（"T优"→"T优势"、"T劣"→"T劣势"）。"""
    return _COURSE_ALIAS.get(course, course)


# ======================================================================
# 用户输入数值统一钳制（用户口径 2026-09-17）
# ======================================================================
# 所有用户输入的数值上界一律 20000；面板类/好感/索敌等按调用方指定的下界（多为 0=不得为负）。
USER_NUM_MAX = 20000.0


def clamp_user_num(v, lo: float = 0.0, hi: float = USER_NUM_MAX):
    """用户输入数值统一钳制：默认下界 0（不得为负）、上界 20000。

    仅用于「用户可输入」的数值（面板赋值/增量、好感与索敌参数等）；内部常量与
    计算结果不经过本函数。无法解析为数字或为 NaN 时返回 None，由调用方决定跳过或报错。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:            # NaN
        return None
    if x < lo:
        x = lo
    if x > hi:
        x = hi
    return x

# 简化 buff 键 → 标准键（damage_formula 读带"增加"后缀的键）。
# 覆盖 session.py 的 _DIRECT_BUFF_FIELDS 全部简化名 + 面板类固定/百分比字段。
# 面板类字段映射（用户输入简写 → (固定值字段名, 百分比字段名)）
# 与 session.py / damage_formula.py 共用，统一面板 buff 键名来源
_PANEL_BUFF_FIELDS = {
    "炮击": ("炮击值固定", "炮击值百分比"),
    "雷击": ("雷击值固定", "雷击值百分比"),
    "航空": ("航空值固定", "航空值百分比"),
    "轰炸": ("轰炸值固定", "轰炸值百分比"),
    "鱼雷": ("鱼雷值固定", "鱼雷值百分比"),
    "装甲": ("装甲值固定", "装甲值百分比"),
    "对空": ("对空值固定", "对空值百分比"),
    "机动": ("机动值固定", "机动值百分比"),
    # 命中+20% 是命中值+20%（进 命中/(命中+1.3×机动) 比值分子），不是命中率直接 +0.2
    "命中": ("命中值固定", "命中值百分比"),
    "幸运": ("幸运值固定", "幸运值百分比"),
    "装填": ("装填值固定", "装填值百分比"),
    "血量": ("血量值", "血量值"),
}
# 所有面板 buff 键集合（从 _PANEL_BUFF_FIELDS 派生，无需手写）
_EXTRA_KEYS = {k for v in _PANEL_BUFF_FIELDS.values() for k in v}

# 敌方字典属性键别名：「X增加」写法统一折算为面板字段名 X。
# 回避率/防暴击率 是「被攻击方」属性（绝对百分比），天赋规则、防守方限时技能 buff、
# 用户手填三处写法不同（回避率 / 回避率增加），在写入 enemy 时统一成面板字段名，
# 供伤害公式读取（用户口径 2026-09-14）。
_ENEMY_ATTR_ALIAS = {
    "回避率增加": "回避率", "回避率": "回避率",
    "防暴击率增加": "防暴击率", "防暴击率": "防暴击率",
}


def _normalize_buff_key(key: str) -> str:
    """把会话临时 buff 的简化键映射为 damage_formula 标准键（带"增加"后缀）。
    已知简化键（无后缀）→ 补"增加"；减免/抵抗类已带语义的保持不变；其余原样。"""
    if not key:
        return key
    if key.endswith("增加") or key.endswith("减免") or key.endswith("抵抗"):
        return key
    # 大小型加成类（session 存 "小型攻击伤害加成" 已是标准键）
    if key.endswith("攻击伤害加成"):
        return key
    if key in _EXTRA_KEYS:
        return key + "增加"
    return key + "增加"


# ======================================================================
# buff 输入解析（用户端 SetSessionBuff 与开发者端额外 Buff 共用的唯一实现）
# ======================================================================

# 所有合法 buff 字段（实际字段名，不带"增加"后缀）
# 直接映射字段（用户输入名 = 实际字段名）
_DIRECT_BUFF_FIELDS = {
    # 攻击方增伤向
    "全伤害", "炮击伤害", "雷击伤害", "航空伤害",
    "轰炸伤害", "鱼雷伤害", "近战伤害", "高平炮伤害",
    "高爆弹伤害", "穿甲弹伤害", "通常弹伤害",
    "普通攻击伤害加成",
    "暴击率", "暴击伤害", "攻击力", "点燃抵抗",
    # 防守方减免向
    "全伤害减免", "炮击伤害减免", "雷击伤害减免", "航空伤害减免",
    "轰炸伤害减免", "鱼雷伤害减免", "近战伤害减免",
    "高爆弹伤害减免", "穿甲弹伤害减免", "通常弹伤害减免",
    "回避率", "防暴击率",
}

# 大小型别名（用户输入名 → 实际字段名）
_SIZE_BUFF_ALIASES = {
    "对小型伤害增加": "小型攻击伤害加成",
    "对中型伤害增加": "中型攻击伤害加成",
    "对大型伤害增加": "大型攻击伤害加成",
    "对小型伤害减免": "小型攻击伤害减免",
    "对中型伤害减免": "中型攻击伤害减免",
    "对大型伤害减免": "大型攻击伤害减免",
}

# 舰种专属增伤/减伤（用户输入名 → (舰种名, bonus/reduction)）
_SHIP_TYPE_BUFF_ALIASES = {
    # 增伤（攻击方，基于防守方舰种）
    "对驱逐伤害增加": ("驱逐", "bonus"),
    "对巡洋伤害增加": ("巡洋", "bonus"),
    "对战列伤害增加": ("战列", "bonus"),
    "对航母伤害增加": ("航母", "bonus"),
    "对轻母伤害增加": ("轻母", "bonus"),
    "对雷巡伤害增加": ("雷巡", "bonus"),
    "对战巡伤害增加": ("战巡", "bonus"),
    "对装母伤害增加": ("装母", "bonus"),
    "对航战伤害增加": ("航战", "bonus"),
    "对重炮伤害增加": ("重炮", "bonus"),
    "对补给伤害增加": ("补给", "bonus"),
    "对水母伤害增加": ("水母", "bonus"),
    # 减伤（防守方，基于攻击方舰种）
    "对驱逐伤害减免": ("驱逐", "reduction"),
    "对巡洋伤害减免": ("巡洋", "reduction"),
    "对战列伤害减免": ("战列", "reduction"),
    "对航母伤害减免": ("航母", "reduction"),
    "对轻母伤害减免": ("轻母", "reduction"),
    "对雷巡伤害减免": ("雷巡", "reduction"),
    "对战巡伤害减免": ("战巡", "reduction"),
    "对装母伤害减免": ("装母", "reduction"),
    "对航战伤害减免": ("航战", "reduction"),
    "对重炮伤害减免": ("重炮", "reduction"),
    "对补给伤害减免": ("补给", "reduction"),
    "对水母伤害减免": ("水母", "reduction"),
}

# 匹配 字段名 + 数值（支持 +15% / -20% / 0.2 / 50 / -0.1）
_BUFF_PAIR_RE = re.compile(r'([一-鿿A-Za-z]+)\s*([+\-]?[\d.]+%?)')

# 字段简称别名（用户口语输入 → 实际字段名）：「高平炮-50%」等按 高平炮伤害 解析
# 普攻专属：归一为引擎键 普通攻击伤害加成（仅对普攻类生效，公式侧 is_normal 配合），
# 与「炮击伤害」等增伤类相互独立（技能/必杀/支援不享受）。
_BUFF_NAME_ALIASES = {
    "高平炮": "高平炮伤害",
    "高爆弹": "高爆弹伤害",
    "穿甲弹": "穿甲弹伤害",
    "通常弹": "通常弹伤害",
    "普攻": "普通攻击伤害加成",
    "普攻伤害": "普通攻击伤害加成",
    "普攻增伤": "普通攻击伤害加成",
    "普攻伤害加成": "普通攻击伤害加成",
    "普攻伤害增加": "普通攻击伤害加成",
    "普通攻击伤害": "普通攻击伤害加成",
    "普通攻击增伤": "普通攻击伤害加成",
    "普通攻击伤害加成": "普通攻击伤害加成",
    "普通攻击伤害增加": "普通攻击伤害加成",
}


def _normalize_buff_pair(name_raw, val_str):
    """根据字段名和数值格式返回实际 buff 信息，无法识别返回 None。
    返回值为 (存储方式, 字段名) 元组：
    - ('buff', 实际字段名)：写入 buffs 字典
    - ('ship_bonus', 舰种名)：写入 ship_type_damage_bonus
    - ('ship_reduction', 舰种名)：写入 ship_type_damage_reduction
    面板类字段根据数值是否带 % 自动选择固定值或百分比字段。"""
    name = name_raw.strip()
    # 简称别名归一（高平炮 → 高平炮伤害），展示与存储一律用完整字段名
    name = _BUFF_NAME_ALIASES.get(name, name)
    # 直接映射（写入 buffs）
    if name in _DIRECT_BUFF_FIELDS:
        return ('buff', name)
    # 大小型别名（写入 buffs）
    if name in _SIZE_BUFF_ALIASES:
        return ('buff', _SIZE_BUFF_ALIASES[name])
    # 舰种专属增伤/减伤（写入 ship_type_damage_bonus / ship_type_damage_reduction）
    if name in _SHIP_TYPE_BUFF_ALIASES:
        ship_type, buff_type = _SHIP_TYPE_BUFF_ALIASES[name]
        if buff_type == 'bonus':
            return ('ship_bonus', ship_type)
        else:
            return ('ship_reduction', ship_type)
    # 面板类（写入 buffs，根据 % 选择字段）
    if name in _PANEL_BUFF_FIELDS:
        fixed_field, pct_field = _PANEL_BUFF_FIELDS[name]
        actual_field = pct_field if val_str.strip().replace('+', '').replace('-', '').endswith('%') else fixed_field
        return ('buff', actual_field)
    return None


def _parse_buff_value(s):
    s = str(s).strip().replace('+', '')
    if s.endswith('%'):
        return float(s[:-1]) / 100.0
    return float(s)


def _extract_buff_duration(buff_str):
    """从 buff 字符串提取「持续N秒/s」→ (清洗后的字符串, 持续秒数 float 或 0)。
    "全伤害+15% 持续30秒" → ("全伤害+15%", 30.0)。
    清洗掉持续段，避免其被 _BUFF_PAIR_RE 误识别为非法字段。"""
    m = re.search(r'持续\s*(\d+(?:\.\d+)?)\s*(?:秒|s|S)', buff_str)
    if not m:
        return buff_str, 0.0
    duration = float(m.group(1))
    clean = re.sub(r'持续\s*\d+(?:\.\d+)?\s*(?:秒|s|S)', '', buff_str)
    return clean, duration


def _parse_buff_str(buff_str):
    """解析 buff 字符串，返回 (成功列表[(存储方式, 字段名, 数值)], 错误列表[str])。
    支持顿号/逗号/空格分隔，数值支持百分比、小数、负数。
    存储方式：'buff'（写入 buffs）、'ship_bonus'（写入 ship_type_damage_bonus）、
    'ship_reduction'（写入 ship_type_damage_reduction）。
    注意：不含「持续N秒」解析——持续窗口由调用方先 _extract_buff_duration 提取。"""
    successes = []
    errors = []
    for m in _BUFF_PAIR_RE.finditer(buff_str):
        name_raw = m.group(1)
        val_str = m.group(2)
        result = _normalize_buff_pair(name_raw, val_str)
        if result is None:
            errors.append(f"{name_raw}{val_str}")
            continue
        try:
            value = _parse_buff_value(val_str)
        except ValueError:
            errors.append(f"{name_raw}{val_str}")
            continue
        storage, field = result
        successes.append((storage, field, value))
    return successes, errors

# 知识库目录：common.py 位于 通用工具/tools/，上溯三级到项目根（阿卡莉1.0/）
KB_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "知识库"))
# 用户自定义角色缓存（持久化：跨会话保存用户对指定角色名的基础面板赋值）
# 与 app.py 保持一致：项目根目录（common.py 位于 通用工具/tools/，上溯三级）
_PROJECT_ROOT = os.path.normpath(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
USER_SHIPS_FILE = os.path.join(_PROJECT_ROOT, "user_ships.json")
# 装备表：源文件统一放 知识库/底层数据/源文件/（导入表格也只从这里读）
EQUIPMENT_FILE = next((p for p in (os.path.join(KB_DIR, "装备.xlsx"),
                                   os.path.join(KB_DIR, "底层数据", "源文件", "装备.xlsx"))
                       if os.path.isfile(p)), os.path.join(KB_DIR, "装备.xlsx"))
GUIDE_DIR = os.path.join(KB_DIR, "攻略")
GUIDE_INDEX_FILE = os.path.join(GUIDE_DIR, "guide_index.json")
# 攻略知识库（精简版 JSON，攻略内容唯一来源）
GUIDE_KB_FILE = os.path.join(GUIDE_DIR, "拂晓攻略知识库_精简版.json")
# 完整 Agent 知识库（build_kb_incremental 产物）：chunks 全文检索（LLM 对话辅助 RAG）
AGENT_KB_FILE = os.path.join(KB_DIR, "llm", "拂晓知识库_agent.json")
# 游戏内活动日历：唯一数据源为表格 知识库/活动/活动日历.xlsx（实时读取、手动维护），
# **只放游戏内活动**；法定节假日属「真实日历」，走 holiday_cn（联网）+ 节假日.json，勿混入本表。
ACTIVITY_CALENDAR_XLSX_FILE = os.path.join(KB_DIR, "活动", "活动日历.xlsx")
# 表格里活动所在的工作表名（找不到则回退到首个工作表）
ACTIVITY_CALENDAR_SHEET = "活动日历"
# 真实日历（中国法定节假日）：与游戏内活动严格区分，仅用于
# ① 联网日历不可用时的离线兜底判定；② 按节假日名匹配阿卡莉口吻祝福语。
HOLIDAY_CALENDAR_FILE = os.path.join(KB_DIR, "活动", "节假日.json")

# 装备表属性组（0-based 列号）：名称, Init列, Final列（Final 为 None 表示单列）
EQUIP_ATTRS = [
    ("血量", 9, 10), ("炮擊", 11, 12), ("雷擊", 13, 14), ("航空", 15, 16),
    ("對空", 17, 18), ("對潛", 19, 20), ("裝甲", 21, 22), ("命中", 23, 24),
    ("機動", 25, 26), ("幸運", 27, 28), ("索敵", 29, 30), ("移動速度", 31, 32),
    ("裝填", 33, None), ("轟炸", 34, 35), ("魚雷", 36, 37),
]

# 装备数据库缓存（模块级，避免每次查询重读 xlsx）
_EQUIP_DB = None


def _load_equipment_db():
    """读取 知识库/装备.xlsx 的紫金彩装备表，返回 [{id, name, eq_type, nation, rarity, cls, attrs, extra}]。"""
    global _EQUIP_DB
    if _EQUIP_DB is not None:
        return _EQUIP_DB
    db = []
    if not os.path.exists(EQUIPMENT_FILE):
        return db
    import openpyxl
    wb = openpyxl.load_workbook(EQUIPMENT_FILE, data_only=True)
    ws = wb["装备"] if "装备" in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    for row in rows[2:]:  # 跳过两行表头
        if row[1] is None:
            continue
        attrs = []
        for name, i_init, i_final in EQUIP_ATTRS:
            init_v = row[i_init]
            final_v = row[i_final] if i_final is not None else None
            if init_v is not None or final_v is not None:
                attrs.append((name, init_v, final_v))
        extra = []
        if row[39] is not None:
            extra.append(("1.0", row[39]))
        if row[40] is not None:
            extra.append(("2.0", row[40]))
        db.append({
            "id": row[0],
            "name": str(row[1]).strip(),
            "eq_type": (row[4] or "").strip(),
            "sub_type": (row[5] or "").strip(),
            "nation": (row[6] or "").strip(),
            "rarity": (row[7] or "").strip(),
            "cls": (row[8] or "").strip(),
            "attrs": attrs,
            "extra": extra,
        })
    _EQUIP_DB = db
    return db


# 装备类型简繁兼容（装备表为繁体，用户/LLM 可能输入简体）
_S2T = str.maketrans({
    "战": "戰", "驱": "驅", "轻": "輕", "载": "載", "机": "機",
    "设": "設", "备": "備", "鱼": "魚", "龙": "龍", "舰": "艦",
    "导": "導", "弹": "彈", "击": "擊", "对": "對",
})


def _norm_type(s: str) -> str:
    """装备类型归一化：去空白 + 简体转繁体，用于与库中繁体值匹配。"""
    return s.replace(" ", "").replace("　", "").translate(_S2T)


def _format_equipment(eq: dict) -> str:
    """格式化单条装备为文本块。"""
    lines = [f"[{eq['id']}] {eq['name']}"]
    meta = []
    if eq["eq_type"]:
        meta.append(f"類型: {eq['eq_type']}")
    if eq["sub_type"] and eq["sub_type"] != "None":
        meta.append(f"副分類: {eq['sub_type']}")
    if eq["nation"]:
        meta.append(f"所屬: {eq['nation']}")
    if eq["rarity"]:
        meta.append(f"稀有: {eq['rarity']}")
    if eq["cls"]:
        meta.append(f"分類: {eq['cls']}")
    lines.append("  " + " | ".join(meta))
    for name, init_v, final_v in eq["attrs"]:
        if final_v is None:
            lines.append(f"  {name}: {init_v}")
        else:
            lines.append(f"  {name}: {init_v} → {final_v}")
    for tag, v in eq["extra"]:
        lines.append(f"  附加能力({tag}): {v}")
    return "\n".join(lines)


def _merge_panel(default: dict, override: dict) -> dict:
    """用 override 覆盖 default 的同名字段，保证键齐全"""
    merged = dict(default)
    for k, v in (override or {}).items():
        merged[k] = v
    return merged


def _validate_course_air(course: str, air: str) -> Optional[str]:
    """校验航向/制空，返回错误字符串或 None。
    内部先归一化航向简写（T优→T优势），用户点前端按钮"T优"不应被判无效。"""
    course = _normalize_course(course)
    if course not in VALID_COURSES:
        return f"无效航向: {course}，可选：{'/'.join(sorted(VALID_COURSES))}。"
    if air not in VALID_AIRS:
        return f"无效制空状态: {air}，可选：{'/'.join(sorted(VALID_AIRS))}。"
    return None


def _load_combatants(app, attacker_name: str, enemy_name: str, session_overrides: dict = None):
    """加载攻击方与敌方，应用会话级临时覆写（buff 叠加 / 面板赋值与增量 / 敌方覆盖）。
    深拷贝后再覆写，不污染原始存档数据。
    返回 (attacker, enemy, error_str)。enemy_name 为空时构造无削减虚拟敌人。"""
    session_overrides = session_overrides or {}
    attacker = app.get_ship_for_calc(attacker_name)
    if attacker is None:
        return None, None, f"未找到攻击方角色: {attacker_name}"
    # 攻击方以用户副本面板为基础（get_ship_for_calc 已应用副本；副本=底层默认）
    # 信息不全拦截：缺数据等同"没有该角色"，中止计算（口径与未找到一致）
    if not app.is_ship_complete(attacker_name):
        return None, None, f"未找到攻击方角色: {attacker_name}"
    if enemy_name:
        enemy_data = app.get_ship_for_calc(enemy_name)
        if enemy_data is None:
            return None, None, f"未找到敌方角色: {enemy_name}"
        # 信息不全拦截：防守方缺数据等同"没有该角色"，中止计算（口径与未找到一致）
        if not app.is_ship_complete(enemy_name):
            return None, None, f"未找到敌方角色: {enemy_name}"
        enemy = app._build_enemy_from_data(enemy_data)
    else:
        # 防守方未提供：构造无削减虚拟敌人，输出原始伤害（不受装甲/减伤影响）
        from core.combat_data import CombatData
        enemy = CombatData.create_empty_enemy()

    # 应用会话级临时覆写（不写库，仅影响本次计算）
    if session_overrides:
        attacker = copy.deepcopy(attacker)
        # buff 叠加到角色现有 buff（简化键 → 标准键，damage_formula 读带"增加"后缀的键）
        for k, v in session_overrides.get("buffs", {}).items():
            std_key = _normalize_buff_key(k)
            cur = attacker.buffs.get(std_key, 0.0)
            attacker.buffs[std_key] = cur + v
        # 舰种专属增伤叠加
        for ship_type, val in session_overrides.get("ship_type_damage_bonus", {}).items():
            cur = attacker.ship_type_damage_bonus.get(ship_type, 0.0)
            attacker.ship_type_damage_bonus[ship_type] = cur + val
        # 舰种专属减伤叠加
        for ship_type, val in session_overrides.get("ship_type_damage_reduction", {}).items():
            cur = attacker.ship_type_damage_reduction.get(ship_type, 0.0)
            attacker.ship_type_damage_reduction[ship_type] = cur + val
        # 技能 CD 缩减（天赋类，百分比 → 四舍五入取整为实际 CD）
        if session_overrides.get("cd"):
            from .talent import apply_cd_effects
            apply_cd_effects(attacker, session_overrides["cd"])
        # 面板字段赋值与增量（会话级输入"炮击2000"=赋值、"炮击+200"/"雷击-50"=增减益增量，
        # 叠加到副本面板；实际值为负时在下方计算层硬校验报错）
        for field, v in session_overrides.get("attack_panel", {}).items():
            if field in attacker.attack_panel:
                attacker.attack_panel[field] = attacker.attack_panel[field] + v
        for field, v in session_overrides.get("defense_panel", {}).items():
            if field in attacker.defense_panel:
                attacker.defense_panel[field] = attacker.defense_panel[field] + v
        # 面板字段直接赋值（"面板2000" = 覆盖，最后应用，优先级最高）
        for field, v in session_overrides.get("attack_panel_set", {}).items():
            attacker.attack_panel[field] = float(v)
        for field, v in session_overrides.get("defense_panel_set", {}).items():
            attacker.defense_panel[field] = float(v)
        # 敌方属性覆盖（防守方防御面板增量 → 合并进 enemy：对空/装甲/机动/血量/回避率/防暴击率）
        enemy_def_add = session_overrides.get("defense_panel", {}) or {}
        for field, v in enemy_def_add.items():
            if field in ("对空", "装甲", "机动", "血量", "回避率", "防暴击率"):
                enemy[field] = float(enemy.get(field, 0) or 0) + float(v)
        for field, v in session_overrides.get("enemy", {}).items():
            # 加法叠加到舰船自带减免（如 舰船自带20% + 用户输入16% = 36%）
            # 键归一：「回避率增加/防暴击率增加」→ 面板字段名（回避率/防暴击率），
            # 使伤害公式读 enemy['回避率'] 时口径一致（用户口径 2026-09-14）
            f = _ENEMY_ATTR_ALIAS.get(field, field)
            enemy[f] = float(enemy.get(f, 0) or 0) + float(v)
        # 天赋效果（按 side 分开，整体覆盖语义；见 tools/talent.py::SetSessionTalent）。
        # attacker 的面板增量只作用于攻击方自身（**不得**并入敌方属性——原实现把
        # 攻击方的防御类天赋经 defense_panel 串进 enemy，实测使俾斯麦装甲 704.64→771.784）；
        # defender 的减免/回避/防御面板增量才进敌方属性。
        _teff = session_overrides.get("talent_effects") or {}
        _atk_t = _teff.get("attacker") or {}
        for k, v in (_atk_t.get("buffs") or {}).items():
            std_key = _normalize_buff_key(k)
            attacker.buffs[std_key] = attacker.buffs.get(std_key, 0.0) + float(v)
        for k, v in (_atk_t.get("ship_bonus") or {}).items():
            cur = attacker.ship_type_damage_bonus.get(k, 0.0)
            attacker.ship_type_damage_bonus[k] = cur + float(v)
        if _atk_t.get("cd"):
            from .talent import apply_cd_effects
            apply_cd_effects(attacker, _atk_t["cd"])
        for field, v in (_atk_t.get("panel") or {}).items():
            if field in attacker.attack_panel:
                attacker.attack_panel[field] = attacker.attack_panel[field] + float(v)
            elif field in attacker.defense_panel:
                attacker.defense_panel[field] = attacker.defense_panel[field] + float(v)
        _def_t = _teff.get("defender") or {}
        for k, v in (_def_t.get("enemy") or {}).items():
            ek = _ENEMY_ATTR_ALIAS.get(k, k)
            enemy[ek] = float(enemy.get(ek, 0) or 0) + float(v)
        # 防守方天赋中的回避率/防暴击率也进敌方属性（降低被命中/被暴击）
        for k, v in (_def_t.get("buffs") or {}).items():
            if k in ("回避率增加", "防暴击率", "防暴击率增加"):
                ek = _ENEMY_ATTR_ALIAS.get(k, k)
                enemy[ek] = float(enemy.get(ek, 0) or 0) + float(v)
        for field, v in (_def_t.get("panel") or {}).items():
            if field in ("对空", "装甲", "机动", "血量", "回避率", "防暴击率"):
                ek = _ENEMY_ATTR_ALIAS.get(field, field)
                enemy[ek] = float(enemy.get(ek, 0) or 0) + float(v)

    # 面板值不能为负：赋值与实际计算时面板必须为正数（雷击-50 这类减益合法，但把面板减成负数即报错）
    neg_panel = [f"{k}={v:g}" for k, v in
                 list(attacker.attack_panel.items()) + list(attacker.defense_panel.items()) if v < 0]
    if neg_panel:
        return None, None, ("计算时面板值不能为负：" + "、".join(neg_panel)
                            + "。请减小面板减益（如 雷击-50 过量），或先用「面板N」赋值正数面板。")
    enemy_neg = [f"{k}={float(enemy.get(k, 0) or 0):g}" for k in ("对空", "装甲", "机动", "血量", "回避率", "防暴击率")
                 if float(enemy.get(k, 0) or 0) < 0]
    if enemy_neg:
        return None, None, ("防守方面板值不能为负：" + "、".join(enemy_neg)
                            + "。请减小防御面板减益。")

    return attacker, enemy, None


def _resolve_calc_context(params: dict, session_overrides: dict) -> tuple:
    """从 params 中读取攻防方角色名，若缺失则回退到 session_overrides.calc_context。
    返回 (attacker_name, enemy_name)，可能为空字符串。"""
    attacker_name = params.get('attacker_name', '') or ''
    enemy_name = params.get('enemy_name', '') or ''
    calc_ctx = session_overrides.get('calc_context', {})
    if not attacker_name and calc_ctx.get('attacker'):
        attacker_name = calc_ctx['attacker']
    if not enemy_name and calc_ctx.get('enemy'):
        enemy_name = calc_ctx['enemy']
    return attacker_name, enemy_name


def _resolve_course_air(params: dict, session_overrides: dict) -> tuple:
    """从 params 读取航向/制空，缺失则回退到 calc_context（默认 同航/均势）。
    返回 (course, air)；course 已归一化为常量表标准名（T优→T优势），保证查表加成正确。"""
    calc_ctx = session_overrides.get('calc_context', {}) or {}
    course = (params.get('course') or calc_ctx.get('course') or '同航').strip() or '同航'
    air = (params.get('air') or calc_ctx.get('air') or '均势').strip() or '均势'
    return _normalize_course(course), air

# 攻略索引缓存（开发者通过 FX数据管理工具/build_guide_index.py 更新索引文件）
_GUIDE_INDEX = None
# 攻略知识库缓存（拂晓攻略知识库_精简版.json）
_GUIDE_KB = None


def _load_guide_index():
    """读取 知识库/攻略/guide_index.json（结构化索引），带 mtime 感知只读缓存。
    索引由 FX数据管理工具 生成/更新；智能体纯只读，文件被外部更新后自动重读。"""
    global _GUIDE_INDEX, _GUIDE_INDEX_MTIME
    if not os.path.exists(GUIDE_INDEX_FILE):
        return None
    cur_mtime = os.path.getmtime(GUIDE_INDEX_FILE)
    if _GUIDE_INDEX is not None and _GUIDE_INDEX_MTIME == cur_mtime:
        return _GUIDE_INDEX
    try:
        with open(GUIDE_INDEX_FILE, encoding="utf-8") as f:
            _GUIDE_INDEX = json.load(f).get("guides", [])
        _GUIDE_INDEX_MTIME = cur_mtime
    except Exception:
        _GUIDE_INDEX = []
    return _GUIDE_INDEX


_GUIDE_INDEX_MTIME = None


def _load_guide_kb():
    """读取 知识库/拂晓攻略知识库_精简版.json（攻略内容唯一来源），带 mtime 感知只读缓存。
    知识库由 FX数据管理工具 生成/更新；智能体纯只读，文件被外部更新后自动重读。"""
    global _GUIDE_KB, _GUIDE_KB_MTIME
    if not os.path.exists(GUIDE_KB_FILE):
        return None
    cur_mtime = os.path.getmtime(GUIDE_KB_FILE)
    if _GUIDE_KB is not None and _GUIDE_KB_MTIME == cur_mtime:
        return _GUIDE_KB
    try:
        with open(GUIDE_KB_FILE, encoding="utf-8") as f:
            kb = json.load(f).get("knowledge_base", {})
        _GUIDE_KB = kb
        _GUIDE_KB_MTIME = cur_mtime
    except Exception:
        _GUIDE_KB = None
    return _GUIDE_KB


def _load_agent_kb():
    """读取 知识库/拂晓知识库_agent.json（build_kb_incremental 产物，LLM 对话辅助 RAG），
    带 mtime 感知只读缓存。返回 {metadata, chunks, reference_data, topic_routing, query_patterns} 或 None。"""
    global _AGENT_KB, _AGENT_KB_MTIME
    if not os.path.exists(AGENT_KB_FILE):
        return None
    cur_mtime = os.path.getmtime(AGENT_KB_FILE)
    if _AGENT_KB is not None and _AGENT_KB_MTIME == cur_mtime:
        return _AGENT_KB
    try:
        with open(AGENT_KB_FILE, encoding="utf-8") as f:
            _AGENT_KB = json.load(f)
        _AGENT_KB_MTIME = cur_mtime
    except Exception:
        _AGENT_KB = None
    return _AGENT_KB


_GUIDE_KB_MTIME = None
_AGENT_KB = None
_AGENT_KB_MTIME = None


# ==================== 用户自定义角色缓存（持久化基础面板） ====================
# 用户对指定角色名赋值基础面板（如 保存角色：我的船 炮击2000 命中300），
# 查询与计算时覆盖该角色的基础面板。文件 user_ships.json 存于项目根目录。

_ATK_PANEL_FIELDS = ('炮击', '雷击', '航空', '轰炸', '鱼雷', '索敌', '命中', '幸运', '装填')
_DEF_PANEL_FIELDS = ('装甲', '对空', '机动', '血量', '回避率', '防暴击率')
# 主属性（"面板N"直接赋值的范围）
_PANEL_MAIN_ATK = ('炮击', '雷击', '航空')
_PANEL_MAIN_DEF = ('装甲', '对空', '机动')
# 所有面板字段集合（用于 talent 匹配等场景，含回避率）
_ALL_PANEL_FIELDS = set(_ATK_PANEL_FIELDS) | set(_DEF_PANEL_FIELDS)


def _load_user_ships() -> dict:
    """读取用户自定义角色缓存 {角色名: {attack_panel, defense_panel}}，带 mtime 缓存。"""
    global _USER_SHIPS, _USER_SHIPS_MTIME
    if not os.path.exists(USER_SHIPS_FILE):
        return {}
    cur = os.path.getmtime(USER_SHIPS_FILE)
    if _USER_SHIPS is not None and _USER_SHIPS_MTIME == cur:
        return _USER_SHIPS
    try:
        with open(USER_SHIPS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _USER_SHIPS = data if isinstance(data, dict) else {}
    except Exception:
        _USER_SHIPS = {}
    _USER_SHIPS_MTIME = cur
    return _USER_SHIPS


def _save_user_ships(data: dict) -> bool:
    """整体写回用户自定义角色缓存。"""
    global _USER_SHIPS, _USER_SHIPS_MTIME
    try:
        with open(USER_SHIPS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _USER_SHIPS = data
        _USER_SHIPS_MTIME = os.path.getmtime(USER_SHIPS_FILE)
        return True
    except Exception:
        return False




_USER_SHIPS = None
_USER_SHIPS_MTIME = None


def _clean_guide_keyword(kw: str) -> str:
    """清洗用户口语关键词（去掉"攻略/文档/主题"等后缀词）。"""
    for w in ("攻略", "文档", "主题", "资料", "给萌新的", "的", "类型"):
        kw = kw.replace(w, "")
    return kw.strip()

# WMO 天气代码 → 中文描述（Open-Meteo 使用）
_WMO_CODES = {
    0: "晴",
    1: "大部分晴朗",
    2: "多云",
    3: "阴天",
    45: "雾",
    48: "冻雾",
    51: "小雪",
    53: "中雪",
    55: "大雪",
    56: "冻雨",
    57: "强冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "大阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


def _fetch_weather(lat: float, lon: float):
    """查询实时天气（Open-Meteo，免 key；失败时回退 wttr.in）。
    返回 dict 或 None。"""
    import urllib.request
    # 主源：Open-Meteo（用 http 规避国内网络对 https 的干扰）
    url = (f"http://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           f"&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
           f"&timezone=auto&language=zh")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        cur = data.get("current", {})
        code = int(cur.get("weather_code", 0))
        return {
            "temp_c": cur.get("temperature_2m"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "desc": _WMO_CODES.get(code, f"天气代码{code}"),
            "source": "open-meteo",
        }
    except Exception:
        pass
    # 备选源：wttr.in
    try:
        wurl = f"https://wttr.in/{lat},{lon}?format=j1"
        req = urllib.request.Request(wurl, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        cc = (data.get("current_condition") or [{}])[0]
        desc = ""
        wd = cc.get("weatherDesc") or [{}]
        if wd:
            desc = (wd[0].get("value") or "").strip()
        return {
            "temp_c": cc.get("temp_C"),
            "humidity": cc.get("humidity"),
            "wind_kmh": cc.get("windspeedKmph"),
            "desc": desc or "未知",
            "source": "wttr.in",
        }
    except Exception:
        return None



def _normalize_ship_input(name, app=None):
    """用户输入角色名 → 正规名（底层统一识别）。
    匹配顺序：①昵称/别名硬映射（name_service，昵称/谐音/错别字命中立即返回，最高优先级）
    → ②app.get_raw_ship（精确→X改→NameMatcher 模糊）。
    不堆口语尾词正则——"口罩拉菲呢/是"等由末级 LLM 意图分析兜底。
    识别不到返回原串（如自定义角色名/非角色词）。"""
    if not name:
        return name or ''
    n = str(name).strip()
    if not n:
        return n
    # ① 昵称/别名硬映射（不依赖 app，任何入口都能识别）
    from core.name_service import hard_lookup, nick_variant
    hit = hard_lookup(n)
    if hit is None and nick_variant(n) != n:
        hit = hard_lookup(nick_variant(n))
    if hit:
        return hit
    # ② 常规匹配链（含改造名映射/模糊）
    if app is None:
        return n
    matched, _ = app.get_raw_ship(n)
    return matched if matched else n
