# -*- coding: utf-8 -*-
"""
开发者入口 · 知识库构建 —— 角色数据（交互式控制台 / 批处理）

三层架构：
- 底层信息（raw_data.json）：表格导入 + 手动更新，以面板表为基准覆盖合并
- 计算表（data.json）：LLM 处理底层信息，增量解析（已解析的默认跳过，可强制重解析）
- 计算器：LLM 只做意图识别（本工具不涉及）

用法（在 阿卡莉1.0 目录下，用项目 venv）：
    .venv/Scripts/python.exe 开发者入口/build/角色数据构建.py              交互式控制台（默认）
    .venv/Scripts/python.exe 开发者入口/build/角色数据构建.py --stage 1    仅合并表格 → raw_data.json（不调 LLM）
    .venv/Scripts/python.exe 开发者入口/build/角色数据构建.py --stage 2    仅增量 LLM 补全 → data.json
    .venv/Scripts/python.exe 开发者入口/build/角色数据构建.py --stage 3    仅规则补全（代码提取，不调 LLM）
    .venv/Scripts/python.exe 开发者入口/build/角色数据构建.py --stage all  完整流程 = 1 + 2 + 3（构建入口.py 调这个）
    .venv/Scripts/python.exe 开发者入口/build/角色数据构建.py --stage 2 --force  强制全量重解析
    .venv/Scripts/python.exe 开发者入口/build/角色数据构建.py --msg "..."  附加用户指令到技能解析

规则补全（stage 3）= 原先散在补丁脚本里的 11 项一次性修补（点燃/持续 buff/航速/CD/递增光环/
必定暴击/条件增伤/buffs 归一/换行修正/普攻基准比对）统一收口，见 build/规则补全.py。

一键增量构建请用 build/构建入口.py；逐函数说明见 开发者入口/构建说明.md。
"""
import json
import os
import re
import sys

# BASE_DIR 指向开发者入口/build/ 目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（开发者入口/build/ → 开发者入口/ → 阿卡莉1.0/）
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))

sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv

from src.战斗数据 import CombatData
from src.战斗数据 import DataStore
from src.文件提取 import (
    extract_panel_data,
    extract_normal_attacks,
    extract_skill_rows,
    extract_talents,
)
from src.名称归一化 import canonicalize_name, match_key
from src.原始数据存档 import RawDataStore
from src.技能解析 import parse_skills_batch
from src.技能增益提取 import extract_raguz_bonus, extract_cd_effects, KAI_BASE as _KAI_BASE
from src.技能增益提取 import extract_static_buffs_from_text
from src.技能增益提取 import extract_flagship_auras, extract_loop_cd



def _resolve_path(p):
    """相对路径 → 基于脚本目录解析为绝对路径（.env / 默认值中的相对路径用）。
    绝对路径原样返回。"""
    if not p:
        return p
    p = str(p).strip()
    if os.path.isabs(p) or (len(p) > 1 and p[1] == ':'):
        return p
    return os.path.normpath(os.path.join(BASE_DIR, p))


# 知识库默认路径（指向项目根目录下的 知识库/）
DEFAULT_KB_DIR = os.path.join(PROJECT_ROOT, '知识库')

# 面板字段 → CombatData 面板字段映射
PANEL_TO_ATTACK = {
    '炮击': '炮击', '雷击': '雷击', '航空': '航空',
    '索敌': '索敌', '命中': '命中', '幸运': '幸运',
}
PANEL_TO_DEFENSE = {
    '血量': '血量', '对空': '对空', '装甲': '装甲', '机动': '机动',
}

# 装甲类型别名 → 标准名（L=轻甲, N/M=中甲, H=重甲）
ARMOR_TYPE_MAP = {
    'L': '轻甲', 'N': '中甲', 'M': '中甲', 'H': '重甲',
    '轻': '轻甲', '中': '中甲', '重': '重甲',
    '轻甲': '轻甲', '中甲': '中甲', '重甲': '重甲',
}


def scan_knowledge_base(kb_dir: str):
    """扫描知识库文件夹，返回三类文件的路径。

    识别规则（按文件名关键词）：
    - 面板表：含'舰灵数值'
    - 普攻表：含'普攻倍率'
    - 技能表：含'CharSkill'或'技能'
    排除 ~$ 开头的 Excel 临时文件与 .bak 备份文件。
    扫描目录：知识库顶层 + 底层数据/（统一知识库分类后 xlsx 源位于 底层数据/）。
    """
    panel_file = normal_file = skill_file = talent_file = None
    if not os.path.isdir(kb_dir):
        return None, None, None, None
    scan_dirs = [kb_dir]
    sub = os.path.join(kb_dir, '底层数据', '源文件')
    if os.path.isdir(sub):
        scan_dirs.append(sub)
    for d in scan_dirs:
        for fn in os.listdir(d):
            if fn.startswith('~$'):
                continue
            lower = fn.lower()
            if not lower.endswith('.xlsx') or '.bak' in lower:
                continue
            path = os.path.join(d, fn)
            if '舰灵数值' in fn and panel_file is None:
                panel_file = path
            elif '普攻倍率' in fn and normal_file is None:
                normal_file = path
            elif ('charskill' in lower or '技能' in fn) and skill_file is None:
                skill_file = path
            elif '数值自动化' in fn and talent_file is None:
                talent_file = path
    return panel_file, normal_file, skill_file, talent_file


def build_combatdata(name: str, panel_info: dict, normal_attacks: list, skill_dict: dict,
                     raw_skills: dict = None) -> CombatData:
    """把三源数据合并到一个 CombatData 对象。

    - panel_info：extract_panel_data 的输出（基础信息 + 面板）
    - normal_attacks：extract_normal_attacks 的输出列表
    - skill_dict：parse_skills_batch 的输出（技能 + buffs），可为 None
    - raw_skills：raw_data 的原始技能文本 dict（递增光环正则提取用）
    """
    obj = CombatData()
    obj.name = name

    # 基础信息（来自面板表）
    obj.ship_type = str(panel_info.get('舰种', '') or '')
    obj.nation = str(panel_info.get('所属', '') or '')
    obj.rarity = str(panel_info.get('稀有', '') or '')
    armor_raw = str(panel_info.get('装甲类型', '') or '')
    obj.armor_type = ARMOR_TYPE_MAP.get(armor_raw, armor_raw)
    obj.ammo_type = str(panel_info.get('弹种', '') or '')

    # 装填（面板表基础字段列）
    zhuangtian = panel_info.get('装填', 0)
    try:
        obj.attack_panel['装填'] = float(zhuangtian or 0)
    except (ValueError, TypeError):
        pass

    # 面板数值（来自强化MAX面板组）
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

    # 航速（通用面板字段，等级无关常量，不进攻防面板）
    if '航速' in panel:
        try:
            obj.speed = float(panel['航速'] or 0)
        except (ValueError, TypeError):
            obj.speed = 0.0

    # 普攻（来自普攻表）
    if normal_attacks:
        obj.normal_attacks = normal_attacks

    # 技能 + buffs（来自 LLM）
    if skill_dict:
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
        # 持续 buff（技能释放后按时间轴生效）
        sb = skill_dict.get('skill_buffs', [])
        if isinstance(sb, list):
            obj.skill_buffs = sb

    # 递增光环（固定规则后处理，只依赖原文文本，不依赖 LLM 输出）：
    # LLM 会把「每5秒+5%，最多+80%」误解析成常驻固定值（取了单次增量或上限），
    # 这里按正则提取生成 ramp_buffs，并把对应属性百分比 buff 清零，
    # 由遭遇战模拟器按时间轴递增（每 interval 秒 +step，封顶 cap，夜战禁用）
    ramp = _extract_ramp_buffs(raw_skills)
    if ramp:
        obj.ramp_buffs = ramp
        for rb in ramp:
            key = rb.get('attr', '')
            if key in obj.buffs:
                obj.buffs[key] = 0.0

    # 拉古兹阵营增伤（代码规则提取，不依赖 LLM；仅遭遇战模块读取）
    obj.raguz_bonus = extract_raguz_bonus(raw_skills)
    # 技能 CD 效果（重置/固定减秒，代码规则提取；仅遭遇战时间轴读取）
    obj.cd_effects = extract_cd_effects(raw_skills)
    # 旗舰光环（LeaderSkill 结构化光环，代码规则提取；仅该角色被选为旗舰时对全队生效。
    # LLM 不产出该字段，重建必须代码回填，否则手动更新重解析后旗舰光环丢失）
    obj.flagship_buffs = extract_flagship_auras(raw_skills or {})

    # 必定暴击标记（代码规则提取，不依赖 LLM；计算层整次出手按 100% 暴击率）
    # 与 postprocess.step_alwayscrit 同一套正则与映射，避免两套实现漂移
    _ALWAYS_CRIT_RE = re.compile(r'必定暴击|必然暴击|必定会暴击')
    _ALWAYS_CRIT_MAP = (('LeaderSkill', 'flagship_attack'), ('SupportSkill', 'support_attack'),
                        ('BattleSkill1', 'skill_attack'), ('BattleSkill2', 'burst_attack'))
    if isinstance(raw_skills, dict):
        for field, attr in _ALWAYS_CRIT_MAP:
            text = raw_skills.get(field)
            if isinstance(text, str) and _ALWAYS_CRIT_RE.search(text):
                skill = getattr(obj, attr, None)
                if isinstance(skill, dict):
                    skill['always_crit'] = True

    # 条件增伤静态回填（代码规则提取，不依赖 LLM）：描述含「XX条件下造成/增加 X% 额外伤害」
    # → 该技能桶写 static_buffs（键=标准增伤键），计算层仅该技能出手期间临时叠加进增伤区；
    # 无命中的技能桶清除残留键，保证幂等。与 postprocess.step_condextra 同一提取函数，
    # 避免两套实现漂移
    _STATIC_BUFF_MAP = _ALWAYS_CRIT_MAP
    if isinstance(raw_skills, dict):
        for field, attr in _STATIC_BUFF_MAP:
            text = raw_skills.get(field)
            sb = extract_static_buffs_from_text(text) if isinstance(text, str) and text else {}
            skill = getattr(obj, attr, None)
            if isinstance(skill, dict):
                if sb:
                    skill['static_buffs'] = sb
                elif skill.get('static_buffs'):
                    skill.pop('static_buffs')

    # 技能循环 CD 回填（代码规则提取，不依赖 LLM）：LLM 未给出 CD（cd=0）而描述含
    # 「冷却时间X秒 / 每X秒+动词 / 每X秒内最多N次」循环表达时回填；已有 CD 不覆盖。
    # 与 postprocess.step_skill_cd、开发者端 app.py 对比页共用 extract_loop_cd
    # （权威实现在通用工具 skill_buff_extractor，防止多份正则漂移）
    if isinstance(raw_skills, dict):
        for field, attr in _ALWAYS_CRIT_MAP:
            skill = getattr(obj, attr, None)
            if isinstance(skill, dict) and float(skill.get('cd', 0) or 0) == 0:
                cd_val = extract_loop_cd(raw_skills.get(field))
                if cd_val:
                    skill['cd'] = cd_val

    return obj


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
    """从技能描述提取「每X秒+Y%，最多+Z%」递增光环 → ramp_buffs 列表。
    仅支持"最多"封顶型（时间线性递增、达到上限后不变）。"""
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
    # 去重（同一 attr 同字段多次匹配合并 step/cap 取最大值）
    seen = {}
    for rb in out:
        k = (rb['attr'], rb['interval'])
        if k in seen:
            seen[k]['step'] = max(seen[k]['step'], rb['step'])
            seen[k]['cap'] = max(seen[k]['cap'], rb['cap'])
        else:
            seen[k] = rb
    return list(seen.values())


def _check_suspicious_names(names: list):
    """检查可疑近似舰名，提示用户可能需要补充 ship_name_map.json"""
    import re
    def _norm(n):
        n = re.sub(r'[·‧.、，．\.]', '', n)
        n = re.sub(r'\s+', '', n)
        return n.lower()

    norm_names = [(n, _norm(n)) for n in names]
    suspicious = []
    for i, (n1, nn1) in enumerate(norm_names):
        for n2, nn2 in norm_names[i + 1:]:
            if nn1 == nn2:
                continue  # 已合并
            if abs(len(nn1) - len(nn2)) > 2:
                continue
            d = _levenshtein(nn1, nn2)
            if 1 <= d <= 2:
                suspicious.append((n1, n2, d))

    if suspicious:
        print('\n[提示] 以下舰名高度相似，可能是错别字/变体，请检查 config/ship_name_map.json：')
        for n1, n2, d in suspicious:
            print(f'  {n1} ↔ {n2} (距离={d})')


def _levenshtein(a: str, b: str) -> int:
    """编辑距离"""
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


# ======================================================================
# 表格提取与归一化
# ======================================================================

def _extract_and_normalize(kb_dir: str):
    """扫描知识库，提取四源数据并舰名归一化。
    返回 (panel_by_key, normal_by_key, skill_by_key, talent_by_key, talent_map)，
    前四个均为 {match_key: (正规名, data)}，talent_map 为 {天赋名: 效果}。
    未找到任何文件时返回 (None, None, None, None, None)。"""
    panel_file, normal_file, skill_file, talent_file = scan_knowledge_base(kb_dir)
    print(f'知识库目录: {kb_dir}')
    print(f'面板表: {panel_file or "未找到"}')
    print(f'普攻表: {normal_file or "未找到"}')
    print(f'技能表: {skill_file or "未找到"}')
    print(f'天赋表: {talent_file or "未找到"}')
    if not any([panel_file, normal_file, skill_file]):
        print('错误: 知识库中未找到任何可识别的 xlsx 文件')
        return None, None, None, None, None

    panel_data = {}
    if panel_file:
        print('正在提取面板数据...')
        panel_data = extract_panel_data(panel_file)
        print(f'面板表提取到 {len(panel_data)} 个角色')
    normal_data = {}
    if normal_file:
        print('正在提取普攻数据...')
        normal_data = extract_normal_attacks(normal_file)
        print(f'普攻表提取到 {len(normal_data)} 个角色')
    skill_rows = []
    if skill_file:
        print('正在提取技能描述...')
        skill_rows = extract_skill_rows(skill_file)
        print(f'技能表提取到 {len(skill_rows)} 个角色')
    talent_data = {}
    talent_map = {}
    if talent_file:
        print('正在提取天赋数据...')
        talent_data, talent_map = extract_talents(talent_file)
        print(f'天赋表提取到 {len(talent_data)} 个角色，{len(talent_map)} 个天赋映射')

    # 舰名归一化 + match_key 去重（同源内同名取最后一条）
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


# ======================================================================
# 合并：xlsx → raw_data.json（以面板表为基准覆盖更新）
# ======================================================================

def _clean_skill_value(v):
    """清理技能字段值：空字符串 / ??? / 纯空白 → 返回 None（从 dict 中删除该 key）"""
    if v is None:
        return None
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == '' or s == '???' or s == '？？？':
        return None
    return v


def _merge_to_raw(store: RawDataStore, panel_by_key, normal_by_key, skill_by_key, talent_by_key=None):
    """以面板表为基准合并四源数据 → raw_data.json（差异化更新）。

    - 角色集合严格等于面板表：遍历 panel_by_key，覆盖更新（面板以面板表为准）
    - **差异化保留**：技能/普攻/天赋源表缺失的舰或字段，继承 raw 既有值
      （手动更新补充的数据不被导入覆盖）；源表字段存在但值为空/???/纯空白 → 删除该 key
    - 清理 raw_data 里不在面板表的历史残留角色
    - 天赋存储为 talents（四行二列，置于 skills 之后）
    返回 (更新数, 删除数)
    """
    talent_by_key = talent_by_key or {}
    updated = 0
    for mk, (name, panel_info) in panel_by_key.items():
        old = store.get(name) or {}
        # 普攻：源表缺失保留旧值
        if mk in normal_by_key:
            normal_attacks = normal_by_key[mk][1] or []
        else:
            normal_attacks = old.get('normal_attacks') or []
        # 技能：字段级差异——源表有的字段按源表（空/???/空白删除），没有的字段保留旧值；
        # 整舰缺失时全部保留旧值
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
        # 天赋：源表缺失保留旧值；有则覆盖
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
    # 清理面板表没有的历史残留（按正规名精确比对，存储键与 cname 均为 canonicalize_name 产物）
    canonical_names = {cname for _, (cname, _info) in panel_by_key.items()}
    removed = store.prune(canonical_names)
    return updated, removed


def _merge_tables(kb_dir: str = None) -> list:
    """合并表格 → raw_data.json（不调 LLM）。返回面板表舰名列表。"""
    kb_dir = kb_dir or _resolve_path(os.environ.get('KNOWLEDGE_BASE_DIR')) or DEFAULT_KB_DIR
    raw_path = _resolve_path(os.environ.get('RAW_DATA_OUTPUT_PATH'))
    if not raw_path:
        print('错误: 未配置 RAW_DATA_OUTPUT_PATH，请在 .env 文件中设置')
        return []

    result = _extract_and_normalize(kb_dir)
    if result[0] is None:
        return []
    panel_by_key, normal_by_key, skill_by_key, talent_by_key, talent_map = result

    store = RawDataStore(raw_path)
    updated, removed = _merge_to_raw(store, panel_by_key, normal_by_key, skill_by_key, talent_by_key)
    store.save()
    print(f'\n底层信息: 以面板表为基准更新 {updated} 个，清理历史残留 {removed} 个，共 {len(store.storage)} 个 → {raw_path}')
    # 天赋映射表写入用户侧知识库（供计算器解析天赋编码用）
    if talent_map:
        talent_map_path = os.path.join(os.path.dirname(raw_path), 'talent_map.json')
        try:
            with open(talent_map_path, 'w', encoding='utf-8') as f:
                json.dump(talent_map, f, ensure_ascii=False, indent=2)
            print(f'天赋映射表: {len(talent_map)} 条 → {talent_map_path}')
        except Exception as e:
            print(f'警告: 天赋映射表写入失败 {e}')

    # ·改 角色天赋补全：改造前后天赋一致，从基础名复制（如 飞鹰·改 ← 飞鹰）
    _fill_kai_talents(store)

    # PVP 推荐表（用户侧知识库/天赋表pvp260719.xlsx）→ talent_pvp.json
    _generate_talent_pvp(raw_path, store.storage)

    # 检查面板表舰名中的可疑近似写法（提示可能需补充 ship_name_map.json）
    _check_suspicious_names([v[0] for v in panel_by_key.values()])
    return [v[0] for v in panel_by_key.values()]


# 改造船基座歧义映射（_KAI_BASE）已在文件头从 core.skill_buff_extractor.KAI_BASE 导入，
# 舰级匹配（match_ship_class）与天赋补全共用同一份权威映射，勿在此重复定义


def _fill_kai_talents(store):
    """·改 角色天赋补全：改造前后天赋一致，从原版复制 talents。
    基座确定规则：_KAI_BASE 显式映射 → 同名基础角色/唯一同名含括号角色；
    多候选且无映射时不猜测，记入缺失并提示。仅补全缺天赋的 ·改 角色。"""
    storage = store.storage
    filled = 0
    skipped = []
    for name in list(storage.keys()):
        if not name.endswith('·改'):
            continue
        entry = storage.get(name) or {}
        if entry.get('talents'):
            continue
        # 显式映射精确命中基座键时直接采用（基名可能同时存在 bare 与括号多角色，
        # 如 列克星敦 与 列克星敦(CV-16)，映射已消歧，不做扫描）
        explicit = _KAI_BASE.get(name)
        if explicit and explicit in storage and storage[explicit].get('talents'):
            candidates = [explicit]
        else:
            base = explicit or name[:-2]
            candidates = [k for k in storage
                          if (k == base or k.startswith(base + '(')) and storage[k].get('talents')]
        if len(candidates) > 1 and name not in _KAI_BASE:
            skipped.append(name)
            print(f'警告: {name} 基座歧义 {candidates}，需在 _KAI_BASE 显式指定，已跳过')
            continue
        if not candidates:
            skipped.append(name)
            continue
        entry['talents'] = storage[candidates[0]]['talents']
        storage[name] = entry
        filled += 1
    if filled:
        store.save()
        print(f'·改 天赋补全: {filled} 个（从原版复制），缺失 {len(skipped)} 个 → {skipped}')
    else:
        print(f'·改 天赋补全: 无需处理（0 个），缺失 {len(skipped)} 个 → {skipped}')


def _generate_talent_pvp(raw_path: str, raw_storage: dict):
    """从用户侧知识库的 天赋表pvp260719.xlsx 生成 talent_pvp.json（角色名→推荐编码）。
    表名格式 '原名/昵称'（如 '夕立/夏雨临'），取第一个 4 位编码；归一化到 raw 正规舰名。"""
    try:
        from src.文件提取 import extract_talent_pvp
    except ImportError:
        return
    kb_dir = os.path.dirname(raw_path)
    pvp_file = None
    for fn in os.listdir(kb_dir):
        if fn.startswith('~$') or not fn.lower().endswith('.xlsx'):
            continue
        if 'pvp' in fn.lower() or '天赋表' in fn:
            pvp_file = os.path.join(kb_dir, fn)
            break
    if not pvp_file:
        return
    try:
        raw_pairs = extract_talent_pvp(pvp_file)
    except Exception as e:
        print(f'警告: PVP 天赋表解析失败 {e}')
        return
    # 表名 → 正规舰名归一化（原名/昵称 → raw 键）
    # 共用 ship_nicknames.json（与 agent NameMatcher 同一张表），别名如 花生→华盛顿/太太→列克星敦 均识别
    from src.舰船命名 import normalize_ship_name

    def norm(table_name):
        main = table_name.split('/')[0].strip()
        # 直接命中 / 去·改 / 昵称表反向映射（含别名）
        rn = normalize_ship_name(table_name, raw_storage)
        if rn:
            return rn
        # 手动补全（拉菲系/列克星敦系等特殊编号舰）——旧逻辑兜底
        manual = {
            '拉菲DD459/萨罗蒙': '拉菲', '拉菲DD724/破空者': '拉菲(DD-724)',
            '加里波第': '朱塞佩·加里波第', '北上改/木田神改': '北上·改',
            '大井改/多多改': '大井·改', '列克星敦CV2/灰夫人': '列克星敦',
            '列克星敦CV16/蓝女士': '列克星敦·改', '提尔皮茨/北之女王': '提尔比茨',
            '卡约杜伊利奥/伊莉欧星语': '卡约·杜伊利奥', '朱利奥凯撒/尤莉娅西撒': '朱利奥·凯撒',
            '甘谷特': '甘古特', '日向改/乌螯改': '日向·改', '伊势改/乌鳌改': '伊势·改',
        }
        return manual.get(table_name)

    import re as _re
    result = {}
    for table_name, codes in raw_pairs.items():
        rn = norm(table_name)
        if not rn:
            continue
        m = _re.match(r'(\d{4})', codes[0])
        if m:
            result[rn] = m.group(1)
    # ·改 角色从基础名补齐（改造前后推荐一致）
    for n in raw_storage:
        if n.endswith('·改') and n not in result:
            base = n[:-2]
            if base in result:
                result[n] = result[base]
    # 特例：拉菲·改 ← 拉菲(DD-724)（同船改造，非拉菲 DD-459）
    if '拉菲·改' in raw_storage and '拉菲·改' not in result and '拉菲(DD-724)' in result:
        result['拉菲·改'] = result['拉菲(DD-724)']

    pvp_path = os.path.join(kb_dir, 'talent_pvp.json')
    try:
        with open(pvp_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f'PVP 天赋推荐表: {len(result)} 条 → {pvp_path}')
    except Exception as e:
        print(f'警告: talent_pvp.json 写入失败 {e}')


def import_tables(kb_dir: str = None):
    """控制台"导入表格"：合并 raw_data.json + 增量 LLM 补全 data.json + 规则补全"""
    _merge_tables(kb_dir)
    print('\n检查计算表缺失角色...')
    _incremental_llm(force=False)
    print('\n规则补全...')
    _run_postprocess()


# ======================================================================
# 增量 LLM：raw_data.json → data.json（已解析的跳过，可强制）
# ======================================================================

def _skills_empty(obj) -> bool:
    """判断角色技能是否全空（之前解析失败留下的空壳，需重解析）"""
    for attr in ('skill_attack', 'burst_attack', 'support_attack', 'flagship_attack'):
        skill = getattr(obj, attr, {})
        if skill.get('parts'):
            return False
    return True


def _incremental_llm(force: bool = False, only_names=None, user_instruction: str = ''):
    """增量 LLM 解析技能 → 更新 data.json。

    - only_names 非空：只解析这些角色（用 match_key 定位正规名），force 被忽略
    - force=False：只解析 raw_data 有但 data.json 无的
    - force=True：强制重解析全部
    """
    raw_path = _resolve_path(os.environ.get('RAW_DATA_OUTPUT_PATH'))
    output_path = _resolve_path(os.environ.get('DATA_OUTPUT_PATH'))
    if not output_path:
        print('错误: 未配置 DATA_OUTPUT_PATH，请在 .env 文件中设置')
        return
    if not raw_path or not os.path.exists(raw_path):
        print('错误: raw_data.json 不存在，请先导入表格')
        return

    raw_store = RawDataStore(raw_path)
    data_store = DataStore(output_path)

    # 以 raw_data 为基准清理 data.json 历史残留（不在底层信息中的角色）
    raw_keys = {match_key(n) for n in raw_store.storage}
    removed = data_store.prune(raw_keys)
    if removed:
        print(f'清理计算表历史残留 {removed} 个（不在底层信息中）')
        data_store.save()

    # 筛选待解析角色
    if only_names:
        pending = []
        for uname in only_names:
            canon = raw_store.find_by_match_key(uname)
            if canon:
                pending.append(canon)
            else:
                print(f'未找到角色: {uname}')
    elif force:
        pending = list(raw_store.storage.keys())
    else:
        # 只解析 raw_data 有但 data.json 无的，或 data.json 有但技能 parts 全空的（历史解析失败空壳）
        pending = []
        for n in raw_store.storage:
            if not data_store.has(n):
                pending.append(n)
            elif _skills_empty(data_store.get(n)):
                pending.append(n)

    if not pending:
        print('无需解析（计算表已全部覆盖底层信息）')
        return

    print(f'待解析 {len(pending)} 个角色，调用 LLM...')
    skill_rows = [raw_store.storage[n]['skills'] for n in pending]
    parsed, err = parse_skills_batch(skill_rows, user_instruction=user_instruction)
    if parsed:
        print(f'LLM 解析成功 {len(parsed)} 个角色')
    if err:
        print(f'注意: {err}')

    parsed_by_name = {}
    if parsed:
        for n, sd in parsed:
            parsed_by_name[n] = sd

    # build_combatdata → upsert data.json（不清空，增量更新）
    for name in pending:
        raw = raw_store.storage[name]
        skill_dict = parsed_by_name.get(name)
        obj = build_combatdata(name, raw['panel'], raw['normal_attacks'], skill_dict,
                               raw.get('skills'))
        data_store.upsert(name, obj)
    data_store.save()
    print(f'计算表已更新: 解析 {len(parsed_by_name)}/{len(pending)} 个 → {output_path}')


# ======================================================================
# 控制台交互
# ======================================================================

def _menu_query(raw_store: RawDataStore, data_store: DataStore):
    """查询角色底层信息 + 计算表摘要"""
    q = input('输入舰名（或 all 列全部，q 返回）: ').strip()
    if not q or q == 'q':
        return
    if q.lower() == 'all':
        names = sorted(raw_store.storage.keys())
        print(f'\n=== 全部角色（底层 {len(names)} 个，计算表 {len(data_store.storage)} 个）===')
        for n in names:
            mark = '√' if data_store.has(n) else '×'
            print(f'  [{mark}] {n}')
        return
    canon = raw_store.find_by_match_key(q)
    if not canon:
        print(f'未找到角色: {q}')
        return
    raw = raw_store.get(canon)
    print(f'\n=== {canon} ===')
    panel = raw.get('panel', {})
    print(f'  舰种: {panel.get("舰种","")} | 所属: {panel.get("所属","")} | 稀有: {panel.get("稀有","")} | 装甲: {panel.get("装甲类型","")} | 弹种: {panel.get("弹种","")}')
    pobj = panel.get('面板', {})
    if pobj:
        print(f'  面板: ' + ' '.join(f'{k}={v}' for k, v in pobj.items()))
    na = raw.get('normal_attacks', [])
    print(f'  普攻({len(na)}条):')
    for i, a in enumerate(na, 1):
        print(f'    {i}. {a.get("weapon_type","")} hit={a.get("hit",1)} 倍率={a.get("multiplier",0)} cd={a.get("cd",0)}')
    sk = raw.get('skills', {})
    for label, key in [('旗舰技', 'LeaderSkill'), ('支援技', 'SupportSkill'),
                       ('通常技', 'BattleSkill1'), ('必杀技', 'BattleSkill2')]:
        txt = sk.get(key, '')
        if txt:
            print(f'  {label}: {txt[:80]}{"..." if len(txt) > 80 else ""}')
    # 计算表摘要
    if data_store.has(canon):
        obj = data_store.get(canon)
        print('  --- 计算表 ---')
        for label, attr in [('通常技', 'skill_attack'), ('必杀技', 'burst_attack'),
                            ('支援技', 'support_attack'), ('旗舰', 'flagship_attack')]:
            skill = getattr(obj, attr, {})
            parts = skill.get('parts', [])
            if parts:
                seg = ' | '.join(f'hit={p.get("hit",1)},{p.get("damage_type","")}' for p in parts)
                print(f'    {label}(cd={skill.get("cd",0)}): {seg}')
    else:
        print('  [计算表未生成，请跑导入表格]')


def _menu_update(raw_store: RawDataStore):
    """手动更新技能描述/天赋 → 保存底层信息 → 重解析该角色 → 更新计算表"""
    q = input('输入要更新的舰名（q 返回）: ').strip()
    if not q or q == 'q':
        return
    canon = raw_store.find_by_match_key(q)
    if not canon:
        print(f'未找到角色: {q}')
        return
    raw = raw_store.get(canon)
    sk = raw.get('skills', {})
    field_map = {
        '1': ('通常技', 'BattleSkill1'), '2': ('必杀技', 'BattleSkill2'),
        '3': ('支援技', 'SupportSkill'), '4': ('旗舰技', 'LeaderSkill'),
    }
    print(f'\n=== {canon} 当前内容 ===')
    for k, (label, field) in field_map.items():
        txt = sk.get(field, '')
        print(f'  {k}.{label}: {txt[:60]}{"..." if len(txt) > 60 else ""}')
    talents = raw.get('talents')
    has_t = isinstance(talents, dict) and talents.get('1')
    print(f'  6.天赋: {"已填" if has_t else "无"}')
    print('  7.基础信息: 舰名/舰种/所属/稀有/装填/装甲类型/面板属性')
    choice = input('选择修改项编号（q 返回）: ').strip()
    if choice == 'q':
        return
    if choice == '6':
        _edit_talents(raw_store, canon, raw)
        return
    if choice == '7':
        _edit_panel(raw_store, canon, raw)
        return
    if choice not in field_map:
        return
    label, field = field_map[choice]
    print(f'当前{label}:\n  {sk.get(field, "")}')
    new_txt = input(f'输入新的{label}描述（空行取消）:\n').strip()
    if not new_txt:
        print('已取消')
        return
    sk[field] = new_txt
    raw_store.save()
    print(f'已保存到底层信息，重新解析 {canon} ...')
    _incremental_llm(force=True, only_names=[canon])
    print('更新完成')


def _edit_panel(raw_store: RawDataStore, canon: str, raw: dict):
    """交互式编辑角色基础信息（panel：舰名/舰种/所属/稀有/装填/装甲类型 + 面板十项属性）。
    输入编号 → 输入新值；留空 = 保留原值；q = 结束。数值自动转 int/float。
    改舰名 = 重命名底层信息 key（与新名冲突时报错并忽略）。"""
    panel = raw.setdefault('panel', {})
    base_fields = ['舰名', '舰种', '所属', '稀有', '装填', '装甲类型']
    stat_fields = ['血量', '炮击', '雷击', '航空', '对空', '装甲', '命中', '机动', '索敌', '幸运']
    sub = panel.setdefault('面板', {})
    print(f'\n=== {canon} 基础信息 ===')
    for i, f in enumerate(base_fields, 1):
        print(f'  {i}.{f}: {panel.get(f, "")}')
    for i, f in enumerate(stat_fields, 7):
        print(f'  {i}.面板-{f}: {sub.get(f, "")}')
    print('-' * 30)
    while True:
        choice = input('选择修改项编号（q 结束）: ').strip()
        if not choice or choice == 'q':
            break
        if not choice.isdigit() or not (1 <= int(choice) <= 6 + len(stat_fields)):
            print('无效编号')
            continue
        n = int(choice)
        if n <= 6:
            field = base_fields[n - 1]
            cur = panel.get(field, '')
            new = input(f'{field}（当前: {cur or "空"}，留空保留）: ').strip()
            if not new:
                continue
            if field == '装填':
                try:
                    new = int(float(new))
                except ValueError:
                    print('装填需为数字，已忽略')
                    continue
            if field == '舰名' and new != canon:
                if raw_store.get(new):
                    print(f'错误: {new} 已存在，无法重命名')
                    continue
                raw_store.storage[new] = raw_store.storage.pop(canon)
                canon = new
            panel[field] = new
            print(f'  已更新 {field} = {new}')
        else:
            field = stat_fields[n - 7]
            cur = sub.get(field, '')
            new = input(f'面板-{field}（当前: {cur or "空"}，留空保留）: ').strip()
            if not new:
                continue
            try:
                new = float(new)
            except ValueError:
                print(f'{field} 需为数字，已忽略')
                continue
            sub[field] = new
            print(f'  已更新 面板-{field} = {new}')
    raw_store.save()
    print(f'已保存到底层信息，重新解析 {canon} ...')
    _incremental_llm(force=True, only_names=[canon])
    _run_postprocess()  # 手动改动后同样走规则补全（幂等，保证字段口径一致）
    print('更新完成')


def _edit_talents(raw_store: RawDataStore, canon: str, raw: dict):
    """交互式编辑角色天赋（四行二列 1A/1B~4A/4B）。
    输入格式「天赋名：效果」（如 通商破坏：对小型舰及中型舰伤害提升 15% 15%）；
    留空 = 保留原值；输入 - = 清除该格；行首 0 = 结束。
    支持整段粘贴：每行一个天赋，行首带 1A/1B 等标签亦可（自动忽略标签）。"""
    talents = raw.get('talents')
    if not isinstance(talents, dict):
        talents = {'1': {}, '2': {}, '3': {}, '4': {}}
    print(f'\n=== {canon} 天赋编辑（四行二列）===')
    print('输入「天赋名：效果」，留空保留原值，- 清除该格，行首 0 或空行两次结束')
    # 显示当前
    for row in ('1', '2', '3', '4'):
        for col in ('A', 'B'):
            cell = talents.get(row, {}).get(col, {})
            name, eff = cell.get('name', ''), cell.get('effect', '')
            print(f'  {row}{col}: {name} {eff}'.strip())
    print('-' * 30)
    import re as _re
    labels = [f'{r}{c}' for r in ('1', '2', '3', '4') for c in ('A', 'B')]
    idx = 0
    while idx < 8:
        lab = labels[idx]
        cell = talents.get(lab[0], {}).get(lab[1], {})
        cur = f"{cell.get('name', '')}：{cell.get('effect', '')}".strip('：')
        inp = input(f'{lab}（当前: {cur or "空"}）: ').strip()
        if inp in ('0', 'q'):
            break
        if not inp:
            idx += 1
            continue
        if inp == '-':
            talents.setdefault(lab[0], {}).pop(lab[1], None)
            idx += 1
            continue
        # 支持行首标签（如 1A 通商破坏：效果 / 通商破坏 效果）
        body = _re.sub(r'^[1-4][AB][\s:：、]*', '', inp)
        if '：' in body or ':' in body:
            name, _, eff = body.partition('：') if '：' in body else body.partition(':')
            name, eff = name.strip(), eff.strip()
        else:
            parts = body.split(None, 1)
            name = parts[0] if parts else ''
            eff = parts[1] if len(parts) > 1 else ''
        if not name:
            print('格式无效，应输入「天赋名：效果」')
            continue
        talents.setdefault(lab[0], {})[lab[1]] = {'name': name, 'effect': eff}
        idx += 1
    raw['talents'] = talents
    raw_store.save()
    print(f'已保存 {canon} 天赋，重新解析 ...')
    _incremental_llm(force=True, only_names=[canon])
    _run_postprocess()  # 天赋改动会改变提取结果，同样走规则补全
    print('天赋更新完成')


def _menu_import():
    """导入表格：合并 + 增量 LLM 补全 + 规则补全"""
    print('\n--- 导入表格 ---')
    import_tables()


def _run_postprocess():
    """stage 3 · 规则补全：把原先散在一次性补丁脚本里的 11 项修补收进完整流程。

    代码规则提取（不调 LLM、全部幂等）：换行规范化 / 航速 / buffs 归一 / skill_buffs·
    flagship_buffs·raguz_bonus·cd_effects / ramp_buffs / 技能循环 CD / 必定暴击 /
    条件增伤 / 点燃 part / 普攻基准比对（仅补 slot/weapon_code，不碰数值）。
    天赋（talents / talent_map / ·改 复制）由 stage 1 负责，不在此重复。"""
    from 规则补全 import run as _pp_run
    return _pp_run()


def _console_loop():
    """交互式控制台主循环"""
    raw_path = _resolve_path(os.environ.get('RAW_DATA_OUTPUT_PATH'))
    output_path = _resolve_path(os.environ.get('DATA_OUTPUT_PATH'))
    if not raw_path or not output_path:
        print('错误: 未配置 RAW_DATA_OUTPUT_PATH / DATA_OUTPUT_PATH，请检查 .env')
        return
    raw_store = RawDataStore(raw_path)
    data_store = DataStore(output_path)
    print(f'知识库构建控制台（底层 {len(raw_store.storage)} 个角色，计算表 {len(data_store.storage)} 个）')
    while True:
        print('\n==========')
        print('1. 查询')
        print('2. 手动更新')
        print('3. 导入表格')
        print('4. 规则补全（skill_buffs/航速/CD/点燃… 重建后自动执行）')
        print('5. 退出')
        choice = input('选择: ').strip()
        if choice == '1':
            _menu_query(raw_store, data_store)
        elif choice == '2':
            _menu_update(raw_store)
            data_store.load()  # 重载计算表（手动更新后会重新解析）
        elif choice == '3':
            _menu_import()
            raw_store.load()  # 重载底层信息
            data_store.load()  # 重载计算表
        elif choice == '4':
            raw_store.load()
            data_store.load()
            _run_postprocess()
            raw_store.load()  # 重载（规则补全会写产物）
            data_store.load()
        elif choice == '5':
            print('再见')
            break
        else:
            print('无效选择')


# ======================================================================
# 批处理入口（--stage 兼容）
# ======================================================================

def _run_batch():
    """--stage 批处理：合并 / 增量 LLM"""
    args = sys.argv[1:]
    stage = 'all'
    force = False
    user_instruction = ''
    i = 0
    while i < len(args):
        if args[i] == '--stage' and i + 1 < len(args):
            stage = args[i + 1]
            i += 2
        elif args[i] == '--force':
            force = True
            i += 1
        elif args[i] == '--msg' and i + 1 < len(args):
            user_instruction = args[i + 1]
            i += 2
        else:
            i += 1
    if stage not in ('1', '2', '3', 'all'):
        print(f'错误: --stage 只能是 1/2/3/all，收到: {stage}')
        return
    if stage == '1':
        _merge_tables()
    elif stage == '2':
        _incremental_llm(force=force, user_instruction=user_instruction)
    elif stage == '3':
        _run_postprocess()
    else:  # all = 完整流程：合表 → 增量 LLM → 规则补全
        _merge_tables()
        _incremental_llm(force=force, user_instruction=user_instruction)
        _run_postprocess()


def main():
    load_dotenv(override=False)
    if '--stage' in sys.argv:
        _run_batch()
        return
    _console_loop()


if __name__ == '__main__':
    main()