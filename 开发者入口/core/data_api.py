# -*- coding: utf-8 -*-
"""
开发者入口 · 纯函数 API 层

业务逻辑，不包含 UI / CLI，返回结构化数据，供 Streamlit app.py 调用。
与 build/角色数据构建.py 有一批同名同职责函数（CLI 走 角色数据构建.py、GUI 走本模块）——改规则时两边都要改。
"""
import json
import hashlib
import os
import re
import sys
import importlib.util

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.join(_SCRIPT_DIR, '..', 'build')
PROJECT_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_COMMON_TOOLS_DIR = os.path.join(PROJECT_ROOT, '通用工具', 'core')

sys.path.insert(0, BASE_DIR)

# 通过 importlib 加载通用工具中的公共模块，避免与本项目 core 包冲突
def _load_common_module(filename, module_name):
    """加载通用工具中的模块"""
    filepath = os.path.join(_COMMON_TOOLS_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# 加载通用工具中的 skill_buff_extractor
_common_buff_module = _load_common_module('skill_buff_extractor.py', 'common_skill_buff_extractor')
extract_skill_buffs = _common_buff_module.extract_skill_buffs
extract_flagship_buffs = _common_buff_module.extract_flagship_buffs
extract_flagship_auras = _common_buff_module.extract_flagship_auras
_BUFF_RE = _common_buff_module._BUFF_RE
_map_buff = _common_buff_module._map_buff
_map_buff_keys = _common_buff_module._map_buff_keys
_target_bonus_tail_items = _common_buff_module._target_bonus_tail_items
extract_period_buffs_from_text = _common_buff_module.extract_period_buffs_from_text
extract_static_buffs_from_text = _common_buff_module.extract_static_buffs_from_text
extract_loop_cd = _common_buff_module.extract_loop_cd
_DMG_INC = _common_buff_module._DMG_INC

# 加载通用工具中的 skill_parser
_common_parser_module = _load_common_module('skill_parser.py', 'common_skill_parser')
SkillParser = _common_parser_module.SkillParser

# 加载通用工具 tools/common 的用户端 buff 输入解析规则（与用户端 SetSessionBuff 唯一同源实现）
_common_tools_common = _load_common_module(
    os.path.join('..', 'tools', 'common.py'), 'common_tools_common')
_parse_buff_str = _common_tools_common._parse_buff_str

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, '.env'), override=False)

from src.战斗数据 import CombatData
from src.战斗数据 import DataStore
from src.文件提取 import (
    extract_panel_data,
    extract_normal_attacks,
    extract_combo_normal,
    extract_skill_rows,
    extract_talents,
)
from src.名称归一化 import canonicalize_name, match_key
from src.原始数据存档 import RawDataStore
from src.技能解析 import parse_skills_batch
from 规则补全 import _extract_ignite, SKILL_MAP as _SKILL_MAP

sys.path.insert(0, _SCRIPT_DIR)
from core import business_logic
from core.business_logic import detect_written_weapon


def _resolve_path(p):
    """相对路径 → 基于脚本目录解析为绝对路径。"""
    if not p:
        return p
    p = str(p).strip()
    if os.path.isabs(p) or (len(p) > 1 and p[1] == ':'):
        return p
    return os.path.normpath(os.path.join(BASE_DIR, p))


DEFAULT_KB_DIR = os.path.join(PROJECT_ROOT, '知识库')

# 面板字段 → CombatData 面板字段映射
PANEL_TO_ATTACK = {
    '炮击': '炮击', '雷击': '雷击', '航空': '航空',
    '索敌': '索敌', '命中': '命中', '幸运': '幸运',
}
PANEL_TO_DEFENSE = {
    '血量': '血量', '对空': '对空', '装甲': '装甲', '机动': '机动',
}

# 装甲类型别名 → 标准名
ARMOR_TYPE_MAP = {
    'L': '轻甲', 'N': '中甲', 'M': '中甲', 'H': '重甲',
    '轻': '轻甲', '中': '中甲', '重': '重甲',
    '轻甲': '轻甲', '中甲': '中甲', '重甲': '重甲',
}


# ======================================================================
# 存储初始化
# ======================================================================

def get_raw_store():
    """获取底层数据存储。"""
    raw_path = _resolve_path(os.environ.get('RAW_DATA_OUTPUT_PATH'))
    if not raw_path:
        raise RuntimeError('未配置 RAW_DATA_OUTPUT_PATH，请在 .env 文件中设置')
    return RawDataStore(raw_path)


def get_data_store():
    """获取计算表存储。"""
    output_path = _resolve_path(os.environ.get('DATA_OUTPUT_PATH'))
    if not output_path:
        raise RuntimeError('未配置 DATA_OUTPUT_PATH，请在 .env 文件中设置')
    return DataStore(output_path)


def load_stores():
    """一次性加载两个存储。"""
    return get_raw_store(), get_data_store()


# ======================================================================
# 数据更新后的三方同步（raw_data.json -> data.json -> custom_ship_config.json）
# ======================================================================

def _compute_panel_hash(panel_info: dict) -> str:
    """计算面板信息指纹（MD5），用于检测面板变化触发 data.json 同步。"""
    relevant_fields = ('舰种', '所属', '稀有', '装甲类型', '弹种', '装填')
    parts = []
    for field in relevant_fields:
        parts.append(f'{field}={panel_info.get(field, "")}')
    sub = panel_info.get('面板') or {}
    for stat in sorted(sub.keys()):
        parts.append(f'{stat}={sub.get(stat)}')
    raw = '|'.join(parts)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def _sync_combat_panel(obj: CombatData, panel_info: dict):
    """将原始面板信息同步到 CombatData（元数据 + 面板数值 + 指纹）。"""
    obj.ship_type = str(panel_info.get('舰种', '') or '')
    obj.nation = str(panel_info.get('所属', '') or '')
    obj.rarity = str(panel_info.get('稀有', '') or '')
    armor_raw = str(panel_info.get('装甲类型', '') or '')
    obj.armor_type = ARMOR_TYPE_MAP.get(armor_raw, armor_raw)
    obj.ammo_type = str(panel_info.get('弹种', '') or '')

    zhuangtian = panel_info.get('装填', 0)
    try:
        obj.attack_panel['装填'] = float(zhuangtian or 0)
    except (ValueError, TypeError):
        pass

    panel = panel_info.get('面板') or {}
    for src_field, dst_field in PANEL_TO_ATTACK.items():
        if src_field in panel:
            try:
                obj.attack_panel[dst_field] = float(panel[src_field] or 0)
            except (ValueError, TypeError):
                pass
    for src_field, dst_field in PANEL_TO_DEFENSE.items():
        if src_field in panel:
            try:
                obj.defense_panel[dst_field] = float(panel[src_field] or 0)
            except (ValueError, TypeError):
                pass

    obj.panel_hash = _compute_panel_hash(panel_info)


def _sync_data_panels(raw_store: RawDataStore) -> int:
    """表格合并后将最新面板批量刷新进 data.json（仅改面板，不经 LLM 重解析）。

    返回刷新的角色数。
    """
    output_path = _resolve_path(os.environ.get('DATA_OUTPUT_PATH'))
    if not output_path:
        raise RuntimeError('未配置 DATA_OUTPUT_PATH，请在 .env 文件中设置')
    data_store = DataStore(output_path)
    synced = 0
    for name, raw in raw_store.storage.items():
        obj = data_store.storage.get(name)
        if obj is None:
            continue
        panel_info = raw.get('panel') or {}
        if getattr(obj, 'panel_hash', '') != _compute_panel_hash(panel_info):
            _sync_combat_panel(obj, panel_info)
            synced += 1
    if synced:
        data_store.save()
    return synced


def build_ship_skill_versions(raw_entry: dict) -> dict:
    """从原始角色记录构建技能版本缓存（伤害计算器与配置同步共用）。"""
    skills_raw = (raw_entry or {}).get('skills') or {}
    versions = {}
    type_map = [
        ('BattleSkill1', '通常技'), ('BattleSkill2', '必杀技'),
        ('LeaderSkill', '旗舰技'), ('SupportSkill', '支援技'),
    ]
    for raw_key, label in type_map:
        text = skills_raw.get(raw_key, '')
        if not text:
            continue
        segments = SkillParser.parse(text)
        # 纯 buff 技能（无伤害段，如空想必杀）同样保留版本：对比页需能选中、
        # 修改其 buff 并在时间轴观察释放节奏（segments=[] 按纯 buff 技能处理）
        descs = []
        for seg in segments:
            attrs = '+'.join(seg.get('attributes', []))
            descs.append(f"{seg['multiplier']*100:.0f}%*({attrs})x{seg.get('hit_count', 1)} {seg.get('damage_type', '')}")
        versions[label] = {
            'name': label,
            'description': '、'.join(descs),
            'raw_text': text,
            'segments': segments,
            'skill_buffs': _extract_skill_buffs_from_text(text),
        }
    return versions


def sync_custom_config(raw_store: RawDataStore, data_store: DataStore = None) -> bool:
    """数据更新后同步 custom_ship_config.json。

    - 攻击方为已有角色：按当前 raw 数据重建技能缓存，舰名失效时清空引用
    - 敌方为已有角色：校验存在于计算表，失效时重置为不选敌方
    返回是否有变更。
    """
    config = business_logic.ConfigManager.load_config()
    before = json.dumps(config, ensure_ascii=False, sort_keys=True)

    # 攻击方：已有角色引用与技能缓存同步
    if config.get('attacker_mode') == 'ship':
        name = str(config.get('attacker_ship_name') or '')
        raw_obj = raw_store.get(name) if name else None
        if raw_obj is None:
            config['attacker_ship_name'] = ''
            config['skill_source'] = ''
        else:
            config['skill_versions'] = build_ship_skill_versions(raw_obj)
            config['skill_source'] = f'ship:{name}'

    # 敌方：已有角色引用有效性校验
    if config.get('enemy_mode') == 'ship' and data_store is not None:
        ename = str(config.get('enemy_ship_name') or '')
        if not ename or not data_store.has(ename):
            config['enemy_mode'] = 'none'
            config['enemy_ship_name'] = ''

    after = json.dumps(config, ensure_ascii=False, sort_keys=True)
    if after != before:
        business_logic.ConfigManager.save_config(config)
        return True
    return False


# ======================================================================
# 查询模块
# ======================================================================


def _sug_norm(s: str) -> str:
    """联想匹配用轻归一：去间隔点/空格并转小写（不做别名展开，保留原名/别名原文）。"""
    return re.sub(r'[·‧.、，．\s]', '', str(s or '')).lower()


def resolve_ship_name(raw_store, query: str):
    """输入 → 正规名。find_by_match_key 内部已做昵称/别名归一（同 ship_nicknames.json）；
    未命中返回 None。"""
    q = (query or '').strip()
    if not q:
        return None
    return raw_store.find_by_match_key(q)


def suggest_ships(raw_store, query: str, limit: int = 15) -> list:
    """角色输入联想：输入 拉菲/北卡/翔鹤/太太 等 → 完整角色候选（下拉提示用）。

    匹配优先级（rank）：精确（完整名或别名）→ 名前缀 → 名包含 → 别名前缀。
    别名只做『前缀』匹配、不做子串包含，避免「巨拉菲/口罩拉菲」把无关船
    （北卡罗来纳/前卫）带进『拉菲』的结果；别名命中 label 括注来源。
    昵称表与查询归一共用 ship_nicknames.json（name_normalizer.load_nicknames）。"""
    q = (query or '').strip()
    if not q:
        return []
    nq = _sug_norm(q)
    if not nq:
        return []
    canons = sorted(raw_store.storage.keys())
    nick = {}
    try:
        from src.名称归一化 import load_nicknames
        nick = load_nicknames() or {}
    except Exception:
        pass
    order = {c: i for i, c in enumerate(canons)}
    # 候选条目：(canon, display, norm, is_alias)
    items = [(c, c, _sug_norm(c), False) for c in canons]
    for c, aliases in nick.items():
        for a in aliases or []:
            if isinstance(a, str) and a.strip():
                items.append((c, a.strip(), _sug_norm(a), True))

    picked = {}   # canon -> (rank, order, label)
    def add(canon, rank, alias_disp=None):
        if canon in picked or canon not in raw_store.storage:
            return
        picked[canon] = (rank, order.get(canon, 9999),
                         canon if not alias_disp else f'{canon}（{alias_disp}）')

    # 0) 精确（完整名或别名）
    for c, disp, nt, is_alias in items:
        if nt == nq:
            add(c, 0, disp if is_alias else None)
    # 1) 名前缀  2) 名包含（非前缀）
    for c in canons:
        nt = _sug_norm(c)
        if nt == nq:
            continue
        if nt.startswith(nq):
            add(c, 1)
    for c in canons:
        nt = _sug_norm(c)
        if nq in nt and not nt.startswith(nq):
            add(c, 2)
    # 3) 别名前缀（括注来源；已由精确/名命中收录的船跳过）
    for c, disp, nt, is_alias in items:
        if is_alias and nt.startswith(nq) and nt != nq:
            add(c, 3, disp)
    ordered = sorted(picked.items(), key=lambda kv: (kv[1][0], kv[1][1]))
    return [{'name': canon, 'label': meta[2], 'rank': meta[0]}
            for canon, meta in ordered[:limit]]


def list_all_ships(raw_store: RawDataStore, data_store: DataStore) -> list:
    """列出全部角色及其计算表状态与国籍（所属）。"""
    names = sorted(raw_store.storage.keys())
    result = []
    for n in names:
        raw = raw_store.get(n) or {}
        result.append({
            'name': n,
            'has_data': data_store.has(n),
            'nation': (raw.get('panel', {}) or {}).get('所属', '') or '未知阵营',
        })
    return result


def query_ship(raw_store: RawDataStore, data_store: DataStore, query: str) -> dict:
    """查询单个角色的完整信息（支持昵称/别名，如 北卡→北卡罗来纳）。"""
    canon = resolve_ship_name(raw_store, query)
    if not canon:
        return {'error': f'未找到角色: {query}'}

    raw = raw_store.get(canon)
    panel = raw.get('panel', {})
    pobj = panel.get('面板', {})
    na = raw.get('normal_attacks', [])
    sk = raw.get('skills', {})

    data_summary = {}
    buffs_summary = {}
    if data_store.has(canon):
        obj = data_store.get(canon)
        for label, attr in [('通常技', 'skill_attack'), ('必杀技', 'burst_attack'),
                            ('支援技', 'support_attack'), ('旗舰', 'flagship_attack')]:
            skill = getattr(obj, attr, {})
            parts = skill.get('parts', [])
            if parts:
                data_summary[label] = {
                    'cd': skill.get('cd', 0),
                    'segments': [{'hit': p.get('hit', 1), 'damage_type': p.get('damage_type', '')} for p in parts],
                }
        # buff 汇总：常驻 buffs 只列非零项，另含技能持续 buff / 旗舰光环 / 额外伤害
        # 旗舰光环为结构化列表（含 scope/condition/scaling），展示时展平成一行一条
        fb = obj.flagship_buffs or []
        if isinstance(fb, dict):
            fb = [{'target': 'team', 'buffs': fb}] if fb else []
        buffs_summary = {
            'buffs': {k: v for k, v in (obj.buffs or {}).items() if v},
            'skill_buffs': obj.skill_buffs or [],
            'flagship_buffs': fb,
            'damage_extra': obj.damage_extra or {},
        }

    return {
        'name': canon,
        'panel': {
            '舰种': panel.get('舰种', ''),
            '所属': panel.get('所属', ''),
            '稀有': panel.get('稀有', ''),
            '装甲类型': panel.get('装甲类型', ''),
            '弹种': panel.get('弹种', ''),
            '面板': pobj,
        },
        'normal_attacks': na,
        'skills': {
            'LeaderSkill': sk.get('LeaderSkill', ''),
            'SupportSkill': sk.get('SupportSkill', ''),
            'BattleSkill1': sk.get('BattleSkill1', ''),
            'BattleSkill2': sk.get('BattleSkill2', ''),
        },
        'talents': raw.get('talents'),
        'has_data': data_store.has(canon),
        'data_summary': data_summary,
        'buffs_summary': buffs_summary,
    }


# ======================================================================
# 更新模块
# ======================================================================

SKILL_FIELDS = ('BattleSkill1', 'BattleSkill2', 'LeaderSkill', 'SupportSkill')


def update_skill(raw_store: RawDataStore, data_store: DataStore, ship_name: str, field: str, new_text: str) -> dict:
    """更新技能描述，保存后自动调用 LLM + 代码规则解析并更新计算表。"""
    canon = raw_store.find_by_match_key(ship_name)
    if not canon:
        return {'error': f'未找到角色: {ship_name}'}

    if field not in SKILL_FIELDS:
        return {'error': f'无效字段: {field}，有效字段: {SKILL_FIELDS}'}

    raw = raw_store.get(canon)
    sk = raw.setdefault('skills', {})
    sk[field] = new_text
    raw_store.save()

    # 完整解析流程：LLM 解析 + 代码规则解析
    parse_err = None
    try:
        _full_reparse_one(raw, data_store, canon)
    except Exception as e:
        parse_err = str(e)

    result = {'success': True, 'name': canon}
    if parse_err:
        result['warning'] = f'技能文本已保存，但解析失败: {parse_err}'
    else:
        result['config_synced'] = sync_custom_config(raw_store, data_store)
    return result


def _full_reparse_one(raw: dict, data_store: DataStore, canon: str):
    """完整重解析单个角色：LLM + 代码规则（点燃 / skill_buffs / 旗舰光环 / 持续扣除）。"""
    skills = raw.get('skills', {})
    panel = raw['panel']
    normal_attacks = raw.get('normal_attacks', [])

    # Step 1: LLM 解析 → 基础攻击结构
    skill_dict = _parse_skill_one(skills, canon)
    if skill_dict is None:
        skill_dict = {}

    # Step 2: 代码规则解析 → 点燃（写入对应攻击的 parts）
    _apply_ignite(skills, skill_dict)

    # Step 3: 构建 CombatData
    obj = _build_combatdata(canon, panel, normal_attacks, skill_dict, skills)

    # Step 4: 代码规则解析 → skill_buffs / flagship_buffs / 持续扣除
    _apply_code_buffs(skills, obj)

    # Step 5: 存储技能文本指纹（用于增量解析检测变化）
    obj.skill_text_hash = _compute_skills_hash(skills)

    data_store.upsert(canon, obj)
    data_store.save()


def _apply_ignite(skills: dict, skill_dict: dict):
    """从技能描述提取点燃，追加到对应攻击的 parts 末尾。"""
    for raw_key, data_key, _ in _SKILL_MAP:
        txt = skills.get(raw_key, '')
        ig = _extract_ignite(txt)
        if not ig:
            continue
        skill = skill_dict.setdefault(data_key, {'cd': 0.0, 'parts': []})
        parts = skill.setdefault('parts', [])
        # 避免重复添加
        if any(isinstance(p, dict) and p.get('damage_type') == '点燃' for p in parts):
            continue
        parts.append({'hit': 1, 'multiplier': '', 'damage_type': '点燃', 'ignite': ig})


def _apply_code_buffs(skills: dict, obj: CombatData):
    """代码规则解析 → skill_buffs / flagship_buffs + 常驻 buffs 白名单重置。

    常驻 buffs 只允许引擎默认值（全伤害增加/全伤害减免 0.2）：
    - 旗舰技光环 → flagship_buffs（代码提取，非空覆盖）
    - 带/不带持续的技能 buff（自身/敌方/出战位/舰队）→ skill_buffs（代码提取，非空覆盖）
    LLM 解析出的 buffs 键一律不进常驻（历史上反复出现持续 buff、旗舰光环、
    幻觉值误归常驻导致重复计算，如光辉的幸运-30% 同时进了 skill_buffs 与常驻）。"""
    # 提取 skill_buffs（确定性规则，覆盖 LLM 输出）
    sb = extract_skill_buffs(skills)
    if sb:
        obj.skill_buffs = sb

    # 提取旗舰光环（结构化：细分范围/条件/计数缩放）
    fa = extract_flagship_auras(skills)
    if fa:
        obj.flagship_buffs = fa

    # 常驻 buffs 白名单重置（放在最后，直接覆盖 LLM 解析写入的 obj.buffs）
    obj.buffs = {"全伤害增加": 0.2, "全伤害减免": 0.2}


def update_panel_field(raw_store: RawDataStore, data_store: DataStore, ship_name: str, field: str, value) -> dict:
    """更新面板基础字段（舰名/舰种/所属等），并同步计算表与自定义配置。"""
    canon = raw_store.find_by_match_key(ship_name)
    if not canon:
        return {'error': f'未找到角色: {ship_name}'}

    old_canon = canon
    raw = raw_store.get(canon)
    panel = raw.setdefault('panel', {})

    if field == '装填':
        value = int(float(value))
    elif field in ('血量', '炮击', '雷击', '航空', '对空', '装甲', '命中', '机动', '索敌', '幸运'):
        value = float(value)

    if field == '舰名' and str(value) != canon:
        if raw_store.get(str(value)):
            return {'error': f'{value} 已存在，无法重命名'}
        raw_store.storage[str(value)] = raw_store.storage.pop(canon)
        canon = str(value)

    panel[field] = value
    raw_store.save()

    # 同步一：舰名重命名时修正 custom_ship_config.json 中的旧引用
    if field == '舰名' and canon != old_canon:
        cfg = business_logic.ConfigManager.load_config()
        refs_changed = False
        for key in ('attacker_ship_name', 'enemy_ship_name'):
            if cfg.get(key) == old_canon:
                cfg[key] = canon
                refs_changed = True
        if cfg.get('skill_source') == f'ship:{old_canon}':
            cfg['skill_source'] = f'ship:{canon}'
            refs_changed = True
        if refs_changed:
            business_logic.ConfigManager.save_config(cfg)

    # 同步二：面板元数据变化时刷新计算表（data.json）对应角色
    if data_store is not None:
        dname = data_store.find_by_match_key(old_canon)
        if dname:
            obj = data_store.storage[dname]
            if field == '舰名':
                if dname != canon:
                    data_store.storage.pop(dname)
                    data_store.upsert(canon, obj)
                obj.name = canon
            else:
                _sync_combat_panel(obj, panel)
            data_store.save()

    config_synced = sync_custom_config(raw_store, data_store)
    return {'success': True, 'name': canon, 'config_synced': config_synced}


def update_panel_stat(raw_store: RawDataStore, data_store: DataStore, ship_name: str, stat_name: str, value: float) -> dict:
    """更新面板属性值，并同步计算表与自定义配置。"""
    canon = raw_store.find_by_match_key(ship_name)
    if not canon:
        return {'error': f'未找到角色: {ship_name}'}

    raw = raw_store.get(canon)
    panel = raw.setdefault('panel', {})
    sub = panel.setdefault('面板', {})
    sub[stat_name] = float(value)
    raw_store.save()

    # 同步：面板数值变化时刷新计算表（data.json）对应角色
    if data_store is not None:
        dname = data_store.find_by_match_key(canon)
        if dname:
            _sync_combat_panel(data_store.storage[dname], panel)
            data_store.save()

    config_synced = sync_custom_config(raw_store, data_store)
    return {'success': True, 'name': canon, 'config_synced': config_synced}


def update_talent(raw_store: RawDataStore, ship_name: str, row: str, col: str, talent_data: dict) -> dict:
    """更新单个天赋格。talent_data: {'name': str, 'effect': str} 或 None（清除）。"""
    canon = raw_store.find_by_match_key(ship_name)
    if not canon:
        return {'error': f'未找到角色: {ship_name}'}

    raw = raw_store.get(canon)
    talents = raw.get('talents')
    if not isinstance(talents, dict):
        talents = {'1': {}, '2': {}, '3': {}, '4': {}}

    if talent_data is None:
        talents.setdefault(row, {}).pop(col, None)
    else:
        talents.setdefault(row, {})[col] = talent_data

    raw['talents'] = talents
    raw_store.save()
    return {'success': True, 'name': canon}


def reparse_ship(raw_store: RawDataStore, data_store: DataStore, ship_name: str) -> dict:
    """强制重新解析指定角色：LLM + 代码规则完整流程。"""
    canon = raw_store.find_by_match_key(ship_name)
    if not canon:
        return {'error': f'未找到角色: {ship_name}'}

    raw = raw_store.get(canon)
    _full_reparse_one(raw, data_store, canon)
    config_synced = sync_custom_config(raw_store, data_store)
    return {'success': True, 'name': canon, 'config_synced': config_synced}


def create_ship(raw_store: RawDataStore, data_store: DataStore, ship_name: str) -> dict:
    """新建角色：在 raw_data.json 创建空记录，并自动初始化计算表。"""
    from src.名称归一化 import canonicalize_name
    canon = canonicalize_name(ship_name) or ship_name.strip()
    if not canon:
        return {'error': '角色名不能为空'}

    if raw_store.has(canon):
        return {'error': f'角色已存在: {canon}'}

    # 创建空的原始数据结构
    raw_entry = {
        'panel': {
            '舰名': canon,
            '舰种': '',
            '所属': '',
            '稀有': '',
            '装甲类型': '',
            '弹种': '',
            '装填': 0,
            '面板': {
                '血量': 0, '炮击': 0, '雷击': 0, '航空': 0,
                '对空': 0, '装甲': 0, '命中': 0, '机动': 0,
                '索敌': 0, '幸运': 0,
            },
        },
        'normal_attacks': [],
        'skills': {
            'Name': canon,
            'BattleSkill1': '',
            'BattleSkill2': '',
            'LeaderSkill': '',
            'SupportSkill': '',
        },
    }

    raw_store.upsert(canon, raw_entry)
    raw_store.save()

    # 同时创建空的计算表
    obj = CombatData()
    obj.name = canon
    obj.skill_text_hash = _compute_skills_hash(raw_entry['skills'])
    data_store.upsert(canon, obj)
    data_store.save()

    config_synced = sync_custom_config(raw_store, data_store)
    return {'success': True, 'name': canon, 'config_synced': config_synced}


# ======================================================================
# 导入与 LLM 解析模块
# ======================================================================

def _classify_source_file(path: str):
    """按文件名关键字归类知识库 xlsx 源：panel/normal/skill/combo/talent；不识别返回 None。
    - panel 面板表（舰灵数值）、normal 普攻表（普攻倍率，旧表）、skill 技能表（CharSkill）、
    - combo 数值自动化表（综合表：普攻列表 + 天赋 + …）、talent 旧天赋表（兼容）。"""
    fn = os.path.basename(path)
    lower = fn.lower()
    if fn.startswith('~$') or not lower.endswith('.xlsx') or '.bak' in lower:
        return None
    if '舰灵数值' in fn:
        return 'panel'
    if '普攻倍率' in fn:
        return 'normal'
    if 'charskill' in lower or '技能' in fn:
        return 'skill'
    if '数值自动化' in fn:
        return 'combo'
    if 'pvp' in lower or '天赋表' in fn:
        return 'talent'
    return None


# 源类型 → 中文名（供界面展示）
_SOURCE_LABELS = {'panel': '面板表', 'normal': '普攻倍率表', 'skill': '技能表',
                  'combo': '数值自动化表（综合表）', 'talent': '天赋表'}

# 界面三槽：技能表 / 面板表 / 数值自动化表（综合表，普攻+天赋归入）
_SLOT_LABELS = {'skill': '技能表', 'panel': '面板表', 'combo': '数值自动化表（综合表）'}


def _slot_of_kind(kind: str) -> str:
    """源类型 → 界面槽位。普攻/天赋随数值自动化表（综合表）槽。"""
    if kind in ('panel', 'skill'):
        return kind
    return 'combo'


def list_source_files(kb_dir: str = None) -> list:
    """列出知识库中可识别的表格源（供三表导入选择）。
    只扫描 底层数据/源文件/ —— 导入表格只能从源文件中读取。"""
    kb_dir = kb_dir or _resolve_path(os.environ.get('KNOWLEDGE_BASE_DIR')) or DEFAULT_KB_DIR
    files = []
    src_dir = os.path.join(kb_dir, '底层数据', '源文件')
    if not os.path.isdir(src_dir):
        return files
    for fn in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, fn)
        kind = _classify_source_file(path)
        if kind:
            files.append({'path': path, 'name': fn, 'kind': kind,
                          'slot': _slot_of_kind(kind),
                          'label': f'{_SOURCE_LABELS[kind]}｜{fn}'})
    return files


def list_source_slots(kb_dir: str = None) -> list:
    """三表（槽位）状态：技能表 / 面板表 / 数值自动化表（综合表）。
    每槽返回 {slot, label, files:[...], has_source}，供界面勾选展示。"""
    files = list_source_files(kb_dir)
    slots = []
    for slot, label in _SLOT_LABELS.items():
        sf = [f for f in files if f['slot'] == slot]
        slots.append({'slot': slot, 'label': label,
                      'files': sf, 'has_source': bool(sf)})
    return slots


def scan_knowledge_base(kb_dir: str):
    """扫描知识库源文件，返回 {kind: 首个匹配文件路径}（panel/normal/skill/combo/talent）。"""
    found = {}
    if not os.path.isdir(kb_dir):
        return found
    scan_dirs = [kb_dir, os.path.join(kb_dir, '底层数据', '源文件')]
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            kind = _classify_source_file(os.path.join(d, fn))
            if kind and kind not in found:
                found[kind] = os.path.join(d, fn)
    return found


def merge_tables(kb_dir: str = None, only_files: list = None, slots: list = None,
                 confirm: set = None, skip_prune: bool = False) -> dict:
    """合并表格 → raw_data.json。
    slots（三表：panel/skill/combo）或 only_files 非空时为定向导入：只识别更新这些源，
    其余源保持原值；后续增量 LLM / 普攻轻同步自动感知差异。
    combo（数值自动化表·综合表）同时贡献 普攻列表 + 天赋。
    confirm：{source:canon} 集合——有差异且未被确认的角色保持原值（先对照后写入流程）；
    skip_prune：确认流程不做面板全集清理（删除角色请走手动更新页，避免误删）。"""
    kb_dir = kb_dir or _resolve_path(os.environ.get('KNOWLEDGE_BASE_DIR')) or DEFAULT_KB_DIR
    raw_path = _resolve_path(os.environ.get('RAW_DATA_OUTPUT_PATH'))
    if not raw_path:
        return {'error': '未配置 RAW_DATA_OUTPUT_PATH'}

    result = _extract_and_normalize(kb_dir, selected_paths=only_files, selected_slots=slots)
    if result[0] is None:
        return {'error': '未找到可识别的表格文件'}

    panel_by_key, normal_by_key, skill_by_key, talent_by_key, talent_map = result
    store = RawDataStore(raw_path)
    skipped = {'panel': 0, 'normal': 0, 'skill': 0, 'talent': 0}
    if confirm is not None:
        panel_by_key, skipped['panel'] = _filter_unconfirmed(store, panel_by_key, 'panel', confirm)
        normal_by_key, skipped['normal'] = _filter_unconfirmed(store, normal_by_key, 'normal', confirm)
        skill_by_key, skipped['skill'] = _filter_unconfirmed(store, skill_by_key, 'skill', confirm)
        talent_by_key, skipped['talent'] = _filter_unconfirmed(store, talent_by_key, 'talent', confirm)
    updated, removed = _merge_to_raw(store, panel_by_key, normal_by_key, skill_by_key,
                                     talent_by_key, skip_prune=skip_prune)
    store.save()

    if talent_map:
        talent_map_path = os.path.join(os.path.dirname(raw_path), 'talent_map.json')
        try:
            with open(talent_map_path, 'w', encoding='utf-8') as f:
                json.dump(talent_map, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    _fill_kai_talents(store)
    _generate_talent_pvp(raw_path, store.storage)

    # 面板同步：合并后的最新面板直接刷新进 data.json（技能解析由增量 LLM 步骤负责）
    panel_synced = _sync_data_panels(store)
    config_synced = sync_custom_config(store)
    out = {'updated': updated, 'removed': removed, 'total': len(store.storage),
           'panel_synced': panel_synced, 'config_synced': config_synced}
    if confirm is not None:
        out['skipped'] = skipped
    return out


def incremental_llm(force: bool = False, only_names=None, user_instruction: str = '') -> dict:
    """增量解析（LLM + 代码规则）→ 更新 data.json。只解析差异化部分。"""
    raw_path = _resolve_path(os.environ.get('RAW_DATA_OUTPUT_PATH'))
    output_path = _resolve_path(os.environ.get('DATA_OUTPUT_PATH'))
    if not output_path:
        return {'error': '未配置 DATA_OUTPUT_PATH'}
    if not raw_path or not os.path.exists(raw_path):
        return {'error': 'raw_data.json 不存在，请先导入表格'}

    raw_store = RawDataStore(raw_path)
    data_store = DataStore(output_path)

    raw_keys = {match_key(n) for n in raw_store.storage}
    removed = data_store.prune(raw_keys)

    # 配置同步：清理失效的角色引用，重建技能缓存
    config_synced = sync_custom_config(raw_store, data_store)

    if only_names:
        pending = []
        normal_only = []
        not_found = []
        for uname in only_names:
            canon = raw_store.find_by_match_key(uname)
            if canon:
                pending.append(canon)
            else:
                not_found.append(uname)
    elif force:
        pending = list(raw_store.storage.keys())
        normal_only = []
    else:
        # 差异化检测：缺角色 / 技能空 / 技能文本变化 → LLM 完整重解析；
        # 仅普攻数据变化 → 跳过 LLM，轻量同步进计算表
        pending = []
        normal_only = []
        for n in raw_store.storage:
            raw = raw_store.storage[n]
            new_hash = _compute_skills_hash(raw.get('skills', {}))
            # 情况1：角色不存在于 data.json
            if not data_store.has(n):
                pending.append(n)
                continue
            existing = data_store.get(n)
            old_hash = existing.skill_text_hash or ''
            # 情况2：角色存在但技能为空 —— 仅当文本指纹变化才重试；
            # hash 一致仍为空说明已按当前文本解析过（无攻击技能 / 技能表无文本），
            # 再解析结果不变，跳过避免每次导入重复 LLM
            if _skills_empty(existing):
                if old_hash != new_hash:
                    pending.append(n)
                continue
            # 情况3：技能描述文本有变化（指纹对比）
            if old_hash != new_hash:
                pending.append(n)
                continue
            # 情况4：普攻数据有变化（倍率/cd/伤害类型/炮弹数）
            if _normal_signature(raw.get('normal_attacks')) != _normal_signature(getattr(existing, 'normal_attacks', None)):
                normal_only.append(n)

    # 仅普攻变化：直接把新普攻与面板刷进 data.json（不做 LLM 重解析、不动 buff 推导结果）
    for name in list(normal_only):
        obj = data_store.storage.get(name)
        raw = raw_store.storage.get(name)
        if obj is None or raw is None:
            normal_only.remove(name)
            continue
        _sync_combat_panel(obj, raw.get('panel') or {})
        obj.normal_attacks = raw.get('normal_attacks', [])
    if normal_only:
        data_store.save()

    if not pending:
        if normal_only:
            return {'status': '仅普攻同步', 'removed': removed, 'normal_synced': len(normal_only),
                    'parsed': 0, 'pending': 0, 'config_synced': config_synced}
        return {'status': '无需解析', 'removed': removed, 'normal_synced': 0,
                'config_synced': config_synced}

    # 差异化增量：只对 pending 列表做 LLM + 代码规则完整解析
    for name in pending:
        raw = raw_store.storage[name]
        _full_reparse_one(raw, data_store, name)

    return {
        'parsed': len(pending),
        'pending': len(pending),
        'removed': removed,
        'error_msg': None,
        'config_synced': config_synced,
        'normal_synced': len(normal_only),
    }


def import_full(kb_dir: str = None, force: bool = False, only_files: list = None,
                slots: list = None, confirm: set = None) -> dict:
    """完整导入：合并表格 + 差异化增量更新（一条链自动完成）。
    slots：三表选择（panel/skill/combo）；only_files 兼容旧调用（按文件定向）。
    两者都为空 = 全量导入全部表。
    confirm：先对照后写入流程传入已确认的 {source:canon} 集合；有差异未确认的角色保持原值，
    且确认流程不做 prune（删除角色请走手动更新页）。"""
    merge_result = merge_tables(kb_dir, only_files=only_files, slots=slots,
                                confirm=confirm, skip_prune=confirm is not None)
    if 'error' in merge_result:
        return merge_result
    llm_result = incremental_llm(force=force)
    return {
        'merge': merge_result,
        'llm': llm_result,
    }


# ======================================================================
# 伤害计算器 API
# ======================================================================

# 技能文本 buff 提取（己方 buffs / 敌方减益）——单一来源在
# 通用工具/core/skill_buff_extractor.py（extract_own_buffs_from_text /
# extract_enemy_debuffs_from_text），此处仅转发，勿再建平行正则。
def _extract_skill_buffs_from_text(text: str) -> dict:
    return _common_buff_module.extract_own_buffs_from_text(text)


def _extract_enemy_debuffs_from_text(text: str) -> dict:
    return _common_buff_module.extract_enemy_debuffs_from_text(text)


def _resolve_skill(skill_version: dict) -> tuple:
    """解析技能段和技能内伤害增益 buff。

    返回 (segments, skill_buffs)。
    """
    raw_text = skill_version.get('raw_text', '')
    desc = skill_version.get('description', '')

    segments = None
    for text in [raw_text, desc]:
        if text:
            segments = business_logic.SkillParser.parse(text)
            if segments:
                break

    if not segments:
        segments = skill_version.get('segments') or []

    # 技能描述写「副炮」（副炮射击/副炮连续射击等）→ 段 weapon_type 标记「副炮」，
    # 计算层 resolve_attack_weapon 按角色副武器段解析（如新泽西副炮=驱逐炮）；
    # 段内已明确具体武器（驱逐炮/高平炮/三式弹…）不覆盖（用户口径 2026-09-05）
    if segments and re.search(r'副炮', raw_text or desc or ''):
        _valid_w = ('驱逐炮', '轻巡炮', '重巡炮', '战列炮', '高平炮', '三式弹',
                    '近战', '雷击', '鱼雷', '鱼雷机', '轰炸机')
        for s in segments:
            wt = (s.get('weapon_type') or '').strip()
            if wt != '副炮' and wt not in _valid_w:
                s['weapon_type'] = '副炮'

    # 调用用户端工具提取技能内伤害增益 buff
    source_text = raw_text or desc or ''
    skill_buffs = _extract_skill_buffs_from_text(source_text)

    return segments, skill_buffs


def calc_damage(config: dict, version_id: str = '通常技',
                course: str = '同航', air_status: str = '均势',
                ammo_type: str = '通常') -> dict:
    """计算指定技能的原始伤害（开发者专用）。"""
    ship = config['custom_ship']

    if version_id not in config['skill_versions']:
        return {'error': f'技能 {version_id} 不存在'}

    skill_version = config['skill_versions'][version_id]
    segments, skill_buffs = _resolve_skill(skill_version)
    if not segments:
        return {'error': '未能解析出有效技能段，请检查技能描述格式'}

    calculator = business_logic.SimpleDamageCalculator()
    result = calculator.calculate_raw_damage(
        ship, segments, skill_buffs,
        course=course, air_status=air_status, ammo_type=ammo_type
    )
    return {
        'title': f"原始伤害 - {ship['name']}（{skill_version['name']}）",
        'result': result,
    }


def compare_damage(config: dict, v1_id: str = None, v2_id: str = None,
                   custom_a_text: str = None, custom_b_text: str = None,
                   course: str = '同航', air_status: str = '均势',
                   ammo_type: str = '通常') -> dict:
    """对比两个技能的原始伤害（开发者专用）。
    
    支持两种模式：
    - 已有技能：通过 v1_id/v2_id 从配置中读取
    - 自定义输入：通过 custom_a_text/custom_b_text 直接传入技能描述文本
    """
    calculator = business_logic.SimpleDamageCalculator()
    ship = config['custom_ship']

    # 解析技能A
    if custom_a_text:
        s1 = business_logic.SkillParser.parse(custom_a_text)
        sb1 = _extract_skill_buffs_from_text(custom_a_text)
        name1 = '自定义技能A'
    elif v1_id and v1_id in config['skill_versions']:
        v1 = config['skill_versions'][v1_id]
        s1, sb1 = _resolve_skill(v1)
        name1 = v1['name']
    else:
        return {'error': '技能A未配置'}

    # 解析技能B
    if custom_b_text:
        s2 = business_logic.SkillParser.parse(custom_b_text)
        sb2 = _extract_skill_buffs_from_text(custom_b_text)
        name2 = '自定义技能B'
    elif v2_id and v2_id in config['skill_versions']:
        v2 = config['skill_versions'][v2_id]
        s2, sb2 = _resolve_skill(v2)
        name2 = v2['name']
    else:
        return {'error': '技能B未配置'}

    if not s1 or not s2:
        return {'error': '未能解析出有效技能段，请检查技能描述格式'}

    result1 = calculator.calculate_raw_damage(
        ship, s1, sb1, course=course, air_status=air_status, ammo_type=ammo_type
    )
    result2 = calculator.calculate_raw_damage(
        ship, s2, sb2, course=course, air_status=air_status, ammo_type=ammo_type
    )

    comparison = business_logic.DamageComparator.compare_raw(result1, result2)

    return {
        'base_result': result1,
        'new_result': result2,
        'comparison': comparison,
        'base_name': name1,
        'new_name': name2,
    }


# ======================================================================
# 内部辅助函数
# ======================================================================

def _enrich_weapon_types(skill_dict: dict, raw_skills: dict):
    """按技能原文为炮击段补充明确写出的弹种（写了才算）。

    在原文中定位段的倍率表达式，取该【】块前文上下文识别
    高平两用炮/高平炮/三式弹/近战；已有 weapon_type 的段不动。
    """
    if not skill_dict or not raw_skills:
        return
    text_by_key = {data_key: raw_skills.get(raw_key, '')
                   for raw_key, data_key, _ in _SKILL_MAP}
    for data_key, text in text_by_key.items():
        atk = skill_dict.get(data_key)
        if not atk:
            continue
        for p in atk.get('parts', []) or []:
            if not isinstance(p, dict) or p.get('weapon_type'):
                continue
            if p.get('damage_type') != '炮击':
                continue
            mult = str(p.get('multiplier', '') or '')
            if not mult:
                continue
            idx = text.find(mult)
            if idx < 0:
                # 兜底：按倍率数字前缀定位（如 '540%*'）
                idx = text.find(mult.split('（', 1)[0].split('(', 1)[0])
            if idx < 0:
                continue
            # 上下文边界与解析器一致：上一个【】块结束（或 ■ 小节开始）到本倍率之间，
            # 不按换行切（攻击句常跨行，如 造成：\n【最多540%*…】）
            seg_start = max(text.rfind('】', 0, idx), text.rfind('■', 0, idx))
            context = text[seg_start + 1: idx]
            written = detect_written_weapon(context)
            if written:
                p['weapon_type'] = written


def _build_combatdata(name: str, panel_info: dict, normal_attacks: list, skill_dict: dict,
                      raw_skills: dict = None) -> CombatData:
    """构建 CombatData 对象。"""
    obj = CombatData()
    obj.name = name
    obj.ship_type = str(panel_info.get('舰种', '') or '')
    obj.nation = str(panel_info.get('所属', '') or '')
    obj.rarity = str(panel_info.get('稀有', '') or '')
    armor_raw = str(panel_info.get('装甲类型', '') or '')
    obj.armor_type = ARMOR_TYPE_MAP.get(armor_raw, armor_raw)
    obj.ammo_type = str(panel_info.get('弹种', '') or '')

    zhuangtian = panel_info.get('装填', 0)
    try:
        obj.attack_panel['装填'] = float(zhuangtian or 0)
    except (ValueError, TypeError):
        pass

    panel = panel_info.get('面板', {})
    for src_field, dst_field in PANEL_TO_ATTACK.items():
        if src_field in panel:
            try:
                obj.attack_panel[dst_field] = float(panel[src_field] or 0)
            except (ValueError, TypeError):
                pass
    for src_field, dst_field in PANEL_TO_DEFENSE.items():
        if src_field in panel:
            try:
                obj.defense_panel[dst_field] = float(panel[src_field] or 0)
            except (ValueError, TypeError):
                pass

    obj.panel_hash = _compute_panel_hash(panel_info)

    if normal_attacks:
        obj.normal_attacks = normal_attacks

    if skill_dict:
        # 按技能原文为炮击段补充明确写出的弹种（高平两用炮/三式弹/近战，写了才算）
        _enrich_weapon_types(skill_dict, raw_skills)
        obj.skill_attack = skill_dict.get('skill_attack', obj.skill_attack)
        obj.burst_attack = skill_dict.get('burst_attack', obj.burst_attack)
        obj.support_attack = skill_dict.get('support_attack', obj.support_attack)
        obj.flagship_attack = skill_dict.get('flagship_attack', obj.flagship_attack)
        llm_buffs = skill_dict.get('buffs', {})
        for k, v in llm_buffs.items():
            if k in obj.buffs:
                try:
                    obj.buffs[k] = float(v)
                except (ValueError, TypeError):
                    pass
        sb = skill_dict.get('skill_buffs', [])
        if isinstance(sb, list):
            obj.skill_buffs = sb
        ramp = _extract_ramp_buffs(raw_skills)
        if ramp:
            obj.ramp_buffs = ramp
            for rb in ramp:
                key = rb.get('attr', '')
                if key in obj.buffs:
                    obj.buffs[key] = 0.0

    return obj


def _parse_skill_one(skills: dict, name: str) -> dict | None:
    """解析单个角色的技能。"""
    try:
        parsed, _ = parse_skills_batch([skills])
        if parsed and len(parsed) > 0:
            return parsed[0][1]
        return None
    except Exception:
        return None


def _skills_empty(obj) -> bool:
    """判断角色技能是否全空。"""
    for attr in ('skill_attack', 'burst_attack', 'support_attack', 'flagship_attack'):
        skill = getattr(obj, attr, {})
        if skill.get('parts'):
            return False
    return True


def parse_buff_input(text: str):
    """解析额外输入：JSON 或自然语言条目（字段映射与用户端 SetSessionBuff 唯一同源）。

    - buff 条目：「暴击伤害+100%」「炮击+155」「全伤害+10%」「全伤害减免+5%」，
      数值可带 %（百分比 buff，值/100）或不带（固定值）；字段名用用户端简化名
      （不带"增加"），支持 大小型/舰种专属/面板类 字段，与用户端规则完全一致；
      旧写法/描述式写法（「全伤害加成10%」「攻击伤害减免-5%」等）按开发者端宽容
      口径兼容（走 _map_buff，与技能提取管线同规则）；
    - 面板赋值条目（含 = / ＝）：「炮击=800」「幸运=50」，作为绝对值覆盖面板，
      对比时两侧同时生效（受控变量，开发者端专用语法）；
    - 时间类输入（「持续N秒」）不适用于单次伤害对比，出现即报错。
    返回 (buffs_dict, panel_dict, err)；err 非空时两个 dict 为 None。"""
    text = (text or '').strip()
    if not text:
        return {}, {}, ''
    # 单次伤害不涉及时间：检测到「持续N秒」直接拒绝
    if re.search(r'持续\s*\d', text):
        return None, None, '单次伤害对比不支持「持续N秒」，请去掉时间'
    # JSON 优先（原格式）
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                out[str(k)] = float(v)
            return out, {}, ''
    except (ValueError, TypeError):
        pass
    out = {}
    panel = {}
    buff_lines = []
    # 面板赋值合法字段（攻方 ∪ 防守方面板全集，2026-09-05：所有面板均支持 xx=数值 写法，
    # 但不允许负数——面板赋值是绝对覆盖，负值无意义）
    _PANEL_ASSIGN_FIELDS = (
        '血量', '炮击', '雷击', '航空', '轰炸', '鱼雷',
        '装甲', '对空', '机动', '命中', '索敌', '幸运', '装填',
        '回避率', '防暴击率',
    )
    for seg in re.split(r'[\n，,、；;]+', text):
        seg = seg.strip().strip('【】[]() 　')
        if not seg:
            continue
        # 面板赋值（键=值）：绝对覆盖，字段须在面板全集内、数值不可为负
        if '=' in seg or '＝' in seg:
            parts = re.split(r'[=＝]', seg, maxsplit=1)
            k = parts[0].strip().rstrip('%％')
            v = parts[-1].strip().rstrip('%％')
            if not k:
                return None, None, f'面板赋值缺少属性名: {seg}'
            if k not in _PANEL_ASSIGN_FIELDS:
                return None, None, (f'面板赋值字段无法识别: {k} '
                                    f'（支持: {"、".join(_PANEL_ASSIGN_FIELDS)}；'
                                    f'「{k}」增减益请用「{k}+数值」写法）: {seg}')
            try:
                val = float(v)
            except ValueError:
                return None, None, f'面板赋值数值无效: {seg}'
            if val < 0:
                return None, None, f'面板赋值不能为负数: {seg}'
            panel[k] = val
            continue
        buff_lines.append(seg)
    if buff_lines:
        errors_all = []
        for seg in buff_lines:
            # 第一层：用户端严格规则（简化名，与 SetSessionBuff 完全一致）
            successes, errors = _parse_buff_str(seg)
            if not errors:
                for storage, field, value in successes:
                    if storage == 'buff':
                        out[field] = out.get(field, 0.0) + value
                    elif storage == 'ship_bonus':
                        # 舰种专属增伤 → 对X伤害增加（引擎从 buffs 读取该键，数值口径一致）
                        key = f'对{field}伤害增加'
                        out[key] = out.get(key, 0.0) + value
                    # ship_reduction（对X伤害减免）为防守方语义，攻击侧对比不消费，仅校验格式
                continue
            # 第二层：宽容回退（开发者端为主）——旧写法/描述式写法走 _map_buff
            #（与技能提取管线同口径，兼容「全伤害加成10%」「攻击伤害减免-5%」
            # 「全伤害增加10%」「航空伤害额外+15%」等），键为引擎标准键
            m = re.match(r'^(.+?)\s*([+-]?\s*\d+(?:\.\d+)?)\s*(%|％)?$', seg)
            if not m:
                errors_all.append(seg)
                continue
            desc = m.group(1).strip()
            val = float(m.group(2))
            pct = bool(m.group(3))
            # 同义触发词归一（仅开发者端回退层）：提升/提高→增加、减少→减免
            desc = (desc.replace('伤害提升', '伤害增加')
                        .replace('伤害提高', '伤害增加')
                        .replace('伤害减少', '伤害减免'))
            key = _map_buff(desc, pct)
            if not key:
                errors_all.append(seg)
                continue
            out[key] = out.get(key, 0.0) + (round(val / 100.0, 6) if pct else val)
        # 两层都无法识别 → 整体报错（与用户端一致，全部不记忆）
        if errors_all:
            return None, None, f'buff 字段无法识别: {", ".join(errors_all)}'
    return out, panel, ''


def _compute_skills_hash(skills: dict) -> str:
    """计算技能描述文本的指纹（MD5），用于增量解析检测变化。"""
    if not skills or not isinstance(skills, dict):
        return ''
    # 只包含4个技能字段，排除 Name 等非技能描述字段
    relevant_fields = ('BattleSkill1', 'BattleSkill2', 'LeaderSkill', 'SupportSkill')
    parts = []
    for field in relevant_fields:
        text = skills.get(field, '')
        if text is None:
            text = ''
        # 换行形态归一（真实换行→字面 \n）：历史数据的指纹按字面 \n 计算，
        # 换行形态修正（字面转真实换行）不应触发增量 LLM 重解析
        text = text.replace('\n', '\\n')
        parts.append(f'{field}={text}')
    raw = '|'.join(parts)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def _canonical_numbers(x):
    """递归规范化数字与键序，用于结构对比（100 与 100.0 视为相同）。"""
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return round(float(x), 6)
    if isinstance(x, dict):
        return {str(k): _canonical_numbers(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_canonical_numbers(v) for v in x]
    return x


def _normal_signature(normal_list) -> str:
    """普攻数据指纹（归一化 JSON），用于检测普攻表变化触发轻量同步。"""
    try:
        norm = _canonical_numbers(normal_list or [])
        return json.dumps(norm, ensure_ascii=False, sort_keys=True)
    except Exception:
        return ''


_RAMP_RE = re.compile(r'([\u4e00-\u9fa5]+值?)?每(\d+(?:\.\d+)?)秒[+＋](\d+(?:\.\d+)?)%，最多[+＋](\d+(?:\.\d+)?)%')
_RAMP_ATTR_MAP = {
    '炮击值': '炮击值百分比增加', '雷击值': '雷击值百分比增加', '航空值': '航空值百分比增加',
    '轰炸值': '轰炸值百分比增加', '鱼雷值': '鱼雷值百分比增加', '装甲值': '装甲值百分比增加',
    '对空值': '对空值百分比增加', '机动值': '机动值百分比增加', '命中值': '命中值百分比增加',
    '幸运值': '幸运值百分比增加', '装填值': '装填值百分比增加',
    '炮击': '炮击值百分比增加', '雷击': '雷击值百分比增加', '航空': '航空值百分比增加',
    '轰炸': '轰炸值百分比增加', '鱼雷': '鱼雷值百分比增加', '装甲': '装甲值百分比增加',
    '对空': '对空值百分比增加', '机动': '机动值百分比增加', '命中': '命中值百分比增加',
    '幸运': '幸运值百分比增加', '装填': '装填值百分比增加',
}


def _extract_ramp_buffs(skills: dict) -> list:
    """从技能描述提取递增光环。"""
    if not isinstance(skills, dict):
        return []
    out = []
    for field in ('SupportSkill', 'BattleSkill1', 'BattleSkill2', 'LeaderSkill'):
        text = skills.get(field)
        if not text or not isinstance(text, str):
            continue
        for m in _RAMP_RE.finditer(text):
            attr_word, step_s, pct, cap_s = m.group(1), m.group(2), m.group(3), m.group(4)
            key = _RAMP_ATTR_MAP.get(attr_word or '') if attr_word else None
            if key is None:
                continue
            out.append({
                'attr': key,
                'step': float(pct) / 100.0,
                'interval': float(step_s),
                'cap': float(cap_s) / 100.0,
                'night_disabled': '夜战' in text,
            })
    seen = {}
    for rb in out:
        k = (rb['attr'], rb['interval'])
        if k in seen:
            seen[k]['step'] = max(seen[k]['step'], rb['step'])
            seen[k]['cap'] = max(seen[k]['cap'], rb['cap'])
        else:
            seen[k] = rb
    return list(seen.values())


def _extract_and_normalize(kb_dir: str, selected_paths=None, selected_slots=None):
    """扫描知识库，按三槽提取数据并舰名归一化。

    槽位语义（界面三表，与 _SLOT_LABELS 一致）：
      panel  面板表        → 面板（舰灵数值.xlsx）
      skill  技能表        → 技能描述（CharSkillInfoLv10.xlsx）
      combo  数值自动化表（综合表）→ 普攻列表（extract_combo_normal）+ 天赋（extract_talents）

    来源解析优先级：selected_slots > selected_paths > 全量扫描（kind 字典）。
    未选中的槽对应数据返回空 dict（_merge_to_raw 对空源沿用库内旧值，保持原值）。
    全部不识别时返回 (None, None, None, None, None)。
    """
    # 解析本次参与的文件路径 → kind 集合
    if selected_slots:
        slots = set(selected_slots)
        paths = [f['path'] for f in list_source_files(kb_dir) if f['slot'] in slots]
    elif selected_paths:
        paths = list(selected_paths)
    else:
        paths = list(scan_knowledge_base(kb_dir).values())

    panel_file = skill_file = None
    normal_files, talent_files = [], []   # combo 文件同时进两线
    for p in paths or []:
        kind = _classify_source_file(p)
        if kind == 'panel':
            panel_file = p
        elif kind == 'skill':
            skill_file = p
        elif kind in ('normal', 'combo'):
            normal_files.append(p)
            if kind == 'combo':
                talent_files.append(p)
        elif kind == 'talent':
            talent_files.append(p)
    if not any([panel_file, skill_file, normal_files, talent_files]):
        return None, None, None, None, None

    panel_data = {}
    if panel_file:
        panel_data = extract_panel_data(panel_file)
    normal_data = {}
    for p in normal_files:
        try:
            if _classify_source_file(p) == 'combo':
                normal_data.update(extract_combo_normal(p))
            else:
                normal_data.update(extract_normal_attacks(p))
        except Exception:
            continue
    skill_rows = []
    if skill_file:
        skill_rows = extract_skill_rows(skill_file)
    talent_data = {}
    talent_map = {}
    for p in talent_files:
        try:
            td, tm = extract_talents(p)
            talent_data.update(td or {})
            talent_map.update(tm or {})
        except Exception:
            continue

    panel_by_key = {}
    for k, v in panel_data.items():
        cname = canonicalize_name(k)
        if cname:
            panel_by_key[match_key(cname)] = (cname, v)
    normal_by_key = {}
    for k, v in normal_data.items():
        cname = canonicalize_name(k)
        if cname:
            normal_by_key[match_key(cname)] = (cname, v)
    skill_by_key = {}
    for row in skill_rows:
        cname = canonicalize_name(row.get('Name', ''))
        if not cname:
            continue
        row_copy = dict(row)
        row_copy['Name'] = cname
        skill_by_key[match_key(cname)] = (cname, row_copy)
    talent_by_key = {}
    for k, v in talent_data.items():
        cname = canonicalize_name(k)
        if cname:
            talent_by_key[match_key(cname)] = (cname, v)
    return panel_by_key, normal_by_key, skill_by_key, talent_by_key, talent_map


def _clean_skill_value(v):
    """清理技能字段值。"""
    if v is None:
        return None
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == '' or s == '???' or s == '???':
        return None
    return v


def _merge_to_raw(store, panel_by_key, normal_by_key, skill_by_key, talent_by_key=None,
                  skip_prune: bool = False, skill_overrides=None):
    """以面板表为基准合并四源数据。
    全量导入时各源键应为面板表子集，行为与旧逻辑一致；
    定向导入（无面板源）时遍历四源并集：无面板的角色沿用库内旧面板，
    库中不存在且无面板数据的新角色跳过（新角色必须由面板表引入），且不做 prune 清理。
    skill_overrides：技能原文手动覆盖（源表未同步的人工确认稿），合表后最后应用。"""
    talent_by_key = talent_by_key or {}
    if skill_overrides is None:
        try:
            skill_overrides = _build_module()._load_skill_overrides()
        except Exception:
            skill_overrides = {}
    updated = 0

    def _name_of(mk):
        for src in (panel_by_key, normal_by_key, skill_by_key, talent_by_key):
            if mk in src:
                return src[mk][0]
        return None

    all_keys = set(panel_by_key) | set(normal_by_key) | set(skill_by_key) | set(talent_by_key)
    for mk in all_keys:
        name = _name_of(mk)
        if not name:
            continue
        old = store.get(name) or {}
        if mk in panel_by_key:
            panel_info = panel_by_key[mk][1]
        else:
            # 无面板源（定向导入）：沿用旧面板；没有旧数据的角色无法构建条目，跳过
            panel_info = old.get('panel')
            if not panel_info:
                continue
        if mk in normal_by_key:
            normal_attacks = normal_by_key[mk][1] or []
        else:
            normal_attacks = old.get('normal_attacks') or []
        if mk in skill_by_key:
            skills = skill_by_key[mk][1] or {}
            has_src = True
        else:
            skills = old.get('skills') or {}
            has_src = False
        old_skills = old.get('skills') or {}
        cleaned_skills = {}
        for sk in ['Name', 'LeaderSkill', 'SupportSkill', 'BattleSkill1', 'BattleSkill2']:
            if sk == 'Name':
                cleaned_skills['Name'] = skills.get('Name', name) or name
                continue
            if has_src and sk in skills:
                cv = _clean_skill_value(skills.get(sk))
                if cv is not None:
                    cleaned_skills[sk] = cv
            else:
                ocv = _clean_skill_value(old_skills.get(sk))
                if ocv is not None:
                    cleaned_skills[sk] = ocv
        # 技能原文手动覆盖：源表未同步的人工确认稿，最后应用（优先级最高）
        for _f, _v in (skill_overrides or {}).get(name, {}).items():
            if _f in ('LeaderSkill', 'SupportSkill', 'BattleSkill1', 'BattleSkill2') \
                    and isinstance(_v, str) and _v.strip():
                cleaned_skills[_f] = _v
        if mk in talent_by_key:
            talents = talent_by_key[mk][1]
        else:
            talents = old.get('talents')
        raw_entry = {
            'panel': panel_info,
            'normal_attacks': normal_attacks,
            'skills': cleaned_skills,
        }
        if talents:
            raw_entry['talents'] = talents
        store.upsert(name, raw_entry)
        updated += 1
    # prune 仅在有面板源且未显式跳过时执行；保留集 = 面板正规名 ∪ 计算表(data.json)现有角色。
    # 注意：不可用 match_key（会小写化/去间隔，storage 键是精确正规名）；
    # 并入 data.json 键是为了兼容面板表命名写法差异（‧ vs ·、皮肤变体）导致的非全集误删。
    removed = 0
    if panel_by_key and not skip_prune:
        keep = {v[0] for v in panel_by_key.values()}
        data_path = _resolve_path(os.environ.get('DATA_OUTPUT_PATH'))
        try:
            if data_path and os.path.exists(data_path):
                with open(data_path, encoding='utf-8') as f:
                    keep |= set(json.load(f).keys())
        except Exception:
            pass
        removed = store.prune(keep)
    return updated, removed


# 改造改名对（源表「XX（YY·改）」全量扫描，共 3 对，无其他）：
# 改造改名角色天赋沿用本体，重建时由此处自动继承，不随导入覆盖丢失
_KAI_RENAME_MAP = {
    '丹阳': '雪风',
    '信赖': '响',
    '埃罗芒什': '巨像',
}


def _fill_kai_talents(store):
    """改造角色天赋补全：·改 后缀角色与改造改名角色，天赋均沿用本体。"""
    storage = store.storage
    filled = 0
    for name in list(storage.keys()):
        if name.endswith('·改'):
            base = name[:-2]
        elif name in _KAI_RENAME_MAP:
            base = _KAI_RENAME_MAP[name]
        else:
            continue
        entry = storage.get(name) or {}
        if entry.get('talents'):
            continue
        src = None
        # 优先本体，其次本体·改，再本体(皮肤)变体
        for cand in (base, base + '·改'):
            cand_entry = storage.get(cand)
            if cand_entry and cand_entry.get('talents'):
                src = cand_entry
                break
        if src is None:
            for k in storage:
                if k.startswith(base + '(') and storage[k].get('talents'):
                    src = storage[k]
                    break
        if src is not None:
            entry['talents'] = src['talents']
            storage[name] = entry
            filled += 1
    if filled:
        store.save()


def _build_module():
    """惰性导入 CLI 构建模块（`开发者入口/build/角色数据构建.py`）。

    与 `build/规则补全.py` 同样的做法：**规则只保留一份**，GUI 侧委托 CLI 实现，
    避免两套 `_generate_talent_pvp` / 技能覆盖规则漂移。
    """
    import sys as _sys
    _bd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'build')
    if os.path.isdir(_bd) and _bd not in _sys.path:
        _sys.path.insert(0, _bd)
    import 角色数据构建 as _m
    return _m


def _generate_talent_pvp(raw_path: str, raw_storage: dict, kb_dir: str = None):
    """生成 talent_pvp.json（委托 CLI 实现：读源表 + 一格多名字 + 双点法取增伤那种）。"""
    try:
        _build_module()._generate_talent_pvp(raw_path, raw_storage, kb_dir)
    except Exception:
        pass

# ======================================================================
# 导入差异对照 + 批量确认（开发者端表格导入：先记录对照→询问→确认后写入）
# 手动更新页（update_* 系列）保持直接落库，不在此流程内。
# ======================================================================

_SOURCE_LABEL_CN = {'panel': '面板', 'normal': '普攻', 'skill': '技能', 'talent': '天赋'}


def _canon_equals(a, b) -> bool:
    """数值规范化后比较（100 vs 100.0 视为相同）。"""
    try:
        return json.dumps(_canonical_numbers(a), ensure_ascii=False, sort_keys=True) == \
               json.dumps(_canonical_numbers(b), ensure_ascii=False, sort_keys=True)
    except Exception:
        return False


def _val_str(v, limit=36) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = (s or '').replace('\n', '⏎')
    return s if len(s) <= limit else s[:limit] + '…'


def _diff_payload_detail(source: str, old, new, cap=3) -> list:
    """单源 payload 差异明细（最多 cap 条摘要）。old/new 之一可为 None（源首次/删除）。"""
    if old is None and new is None:
        return []
    if old is None:
        return ['（新增数据，原库无该源）']
    if new is None:
        return ['（库中原有，本次未提供——未勾选导入会保持原值，不会覆盖）']
    out = []
    if source == 'panel':
        keys = list(dict.fromkeys(list(old.keys()) + list(new.keys())))
        for k in keys:
            if k == '面板':
                continue  # 下面按面板属性逐项
            if not _canon_equals(old.get(k), new.get(k)):
                out.append(f'{k}: {_val_str(old.get(k))} → {_val_str(new.get(k))}')
        po, pn = old.get('面板') or {}, new.get('面板') or {}
        for k in list(dict.fromkeys(list(po.keys()) + list(pn.keys()))):
            if not _canon_equals(po.get(k), pn.get(k)):
                out.append(f'面板·{k}: {_val_str(po.get(k))} → {_val_str(pn.get(k))}')
    elif source == 'normal':
        if len(old) != len(new):
            out.append(f'普攻段数: {len(old)} → {len(new)}')
        _fields = ('multiplier', 'cd', 'hit', 'weapon_type', 'ammo_type', 'slot', 'weapon_code')
        for i in range(max(len(old or []), len(new or []))):
            o = old[i] if i < len(old) else None
            n = new[i] if i < len(new) else None
            if o is None or n is None:
                out.append(f'第{i+1}段: 新增/删除')
                continue
            for f in _fields:
                if not _canon_equals(o.get(f), n.get(f)):
                    out.append(f'第{i+1}段·{f}: {_val_str(o.get(f))} → {_val_str(n.get(f))}')
                    if len(out) >= cap + 10:
                        break
            if len(out) >= cap + 10:
                break
    elif source == 'skill':
        for f in ('LeaderSkill', 'SupportSkill', 'BattleSkill1', 'BattleSkill2'):
            o = (old.get(f) or '') if isinstance(old, dict) else ''
            n = (new.get(f) or '') if isinstance(new, dict) else ''
            if o != n:
                tag = {'LeaderSkill': '旗舰技', 'SupportSkill': '支援技',
                       'BattleSkill1': '通常技', 'BattleSkill2': '必杀技'}.get(f, f)
                out.append(f'{tag}: {_val_str(o, 120) or "（空）"} → {_val_str(n, 120) or "（空）"}'
                           f'（将触发增量LLM重解析）')
    elif source == 'talent':
        rows = list(dict.fromkeys(list((old or {}).keys()) + list((new or {}).keys())))
        for rk in rows:
            ro, rn = (old or {}).get(rk) or {}, (new or {}).get(rk) or {}
            for opt in ('A', 'B'):
                oo = (ro or {}).get(opt) or {}
                nn = (rn or {}).get(opt) or {}
                if not _canon_equals(oo.get('name'), nn.get('name')) or \
                        not _canon_equals(oo.get('effect'), nn.get('effect')):
                    o_txt = (oo.get('name') or '') + (' ' + str(oo.get('effect') or '') if oo.get('effect') else '')
                    n_txt = (nn.get('name') or '') + (' ' + str(nn.get('effect') or '') if nn.get('effect') else '')
                    out.append(f'{rk}{opt}: {_val_str(o_txt.strip(), 30) or "（空）"} → {_val_str(n_txt.strip(), 30) or "（空）"}')
    return out[:cap]



def preview_import(kb_dir: str = None, slots: list = None, only_files: list = None) -> dict:
    """对照预览（只读，不写盘）：提取本次参与源 → 与 raw_data.json 现状逐角色 diff。
    返回 {rows:[{ship, source, label, kind, changes, detail:[...]}], has_diff, checked_sources}。
    kind: added(源首次/新角色) | updated(有字段变化)。无差异源不产生行。"""
    raw_path = _resolve_path(os.environ.get('RAW_DATA_OUTPUT_PATH'))
    if not raw_path:
        return {'error': '未配置 RAW_DATA_OUTPUT_PATH'}
    result = _extract_and_normalize(kb_dir or DEFAULT_KB_DIR, selected_paths=only_files,
                                    selected_slots=slots)
    if result[0] is None:
        return {'error': '未找到可识别的表格文件'}
    panel_by_key, normal_by_key, skill_by_key, talent_by_key, talent_map = result
    store = RawDataStore(raw_path)
    rows = []
    seen = set()
    for src, bk in (('panel', panel_by_key), ('normal', normal_by_key),
                    ('skill', skill_by_key), ('talent', talent_by_key)):
        for mk, (cname, payload) in bk.items():
            old_entry = store.get(cname)
            old = None if old_entry is None else old_entry.get(
                {'panel': 'panel', 'normal': 'normal_attacks',
                 'skill': 'skills', 'talent': 'talents'}[src])
            kind = 'added' if old_entry is None or old is None else 'updated'
            if kind == 'added' and not payload and src != 'panel':
                continue
            detail = [] if (_canon_equals(old, payload)) else \
                _diff_payload_detail(src, old, payload)
            if detail:
                rows.append({'ship': cname, 'source': src, 'label': _SOURCE_LABEL_CN[src],
                             'kind': kind, 'changes': len(detail), 'detail': detail})
    rows.sort(key=lambda r: (r['ship'], r['source']))
    return {'rows': rows, 'has_diff': bool(rows),
            'sources': [_SOURCE_LABEL_CN[s] for s in
                        ('panel', 'normal', 'skill', 'talent')
                        if {'panel': panel_by_key, 'normal': normal_by_key,
                            'skill': skill_by_key, 'talent': talent_by_key}[s]]}


def _filter_unconfirmed(store, by_key, source, confirm: set):
    """把「有差异但未被确认」的角色从 by_key 剔除（无差异角色照常保留）。
    confirm: {"source:canon"} 集合。返回 (过滤后 dict, 被跳过数)。"""
    key = {'panel': 'panel', 'normal': 'normal_attacks',
           'skill': 'skills', 'talent': 'talents'}[source]
    out = {}
    skipped = 0
    for mk, (cname, payload) in by_key.items():
        old_entry = store.get(cname)
        old = None if old_entry is None else old_entry.get(key)
        if not _canon_equals(old, payload):
            if f'{source}:{cname}' not in confirm:
                skipped += 1
                continue
        out[mk] = (cname, payload)
    return out, skipped
