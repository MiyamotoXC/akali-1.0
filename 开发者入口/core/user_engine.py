# -*- coding: utf-8 -*-
"""
用户端完整伤害引擎桥接（开发者端专用）

职责：
1. 加载 通用工具/core 的 DamageCalculator / OutputHandler（与用户端完全一致的计算引擎）。
   damage_formula.py 内部使用 `from core.xxx import ...` 绝对导入，而开发者端自身占用
   `core` 包名，因此这里预先在 sys.modules 注册 `core.xxx` 别名，使绝对导入落到通用工具模块上。
2. 时间轴总伤害模拟（实时 buff 窗口：技能持续 buff 在持续期内作用于普攻/技能伤害），
   逻辑复制自 通用工具/tools/calc.py 的 _calc_total_damage_timeline，按开发者端数据源适配。
3. 单次技能释放的自带 buff 应用（描述中先于伤害的 buff 当次生效），复制自 tools/calc.py。
"""
import importlib.util
import os
import re
import sys

_ENGINE_CORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "通用工具", "core",
)


def _load_engine_module(filename, module_name):
    """从 通用工具/core 加载模块文件。"""
    filepath = os.path.join(_ENGINE_CORE_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _register_core_alias(filename, alias):
    """加载模块并注册为 `core.<alias>` 别名（供 damage_formula 的绝对导入解析）。"""
    mod = _load_engine_module(filename, f'dev_engine_{alias}')
    sys.modules[f'core.{alias}'] = mod
    return mod


# 注册顺序即依赖顺序：formula_engine / constants / combat_data 必须先于 damage_formula
_engine_formula = _register_core_alias('formula_engine.py', 'formula_engine')
# `from core import formula_engine as engine` 需要父包上有该属性
setattr(sys.modules['core'], 'formula_engine', _engine_formula)
_register_core_alias('constants.py', 'constants')
_register_core_alias('combat_data.py', 'combat_data')
_damage_formula = _register_core_alias('damage_formula.py', 'damage_formula')

DamageCalculator = _damage_formula.DamageCalculator
OutputHandler = _load_engine_module('output_handler.py', 'dev_engine_output_handler').OutputHandler
# 与引擎同源的 CombatData（构建自定义攻防方用）
CombatData = sys.modules['core.combat_data'].CombatData
# 阵型 → buff 映射（单一来源：通用工具/core/constants.py，与用户端对话路由共用）
FORMATION_BUFFS = sys.modules['core.constants'].FORMATION_BUFFS


# ==================== buff 键标准化（复制自 tools/common.py） ====================

def _normalize_buff_key(key: str) -> str:
    """简化 buff 键 → damage_formula 标准键（无语义后缀的补"增加"）。"""
    if not key:
        return key
    if key.endswith(("增加", "减免", "抵抗", "攻击伤害加成")):
        return key
    return key + "增加"


# ==================== 单次技能自带 buff 应用（复制自 tools/calc.py，适配数据源） ====================

_SINGLE_CHOICE_TRIGGER = {2: 'skill', 3: 'burst', 4: 'support'}
_TRIGGER_FIELD = {'support': 'SupportSkill', 'skill': 'BattleSkill1', 'burst': 'BattleSkill2'}
_BUFF_DUR_RE = re.compile(
    r'【([^】]*?[^0-9+\-])([+-]?\d+(?:\.\d+)?)\s*(%?)\s*(?:[，,、]\s*(?:持续|效果持续)\s*(\d+(?:\.\d+)?)\s*秒)?】')
_DMG_SEG_RE = re.compile(r'造成|放出\s*\d+\s*架|发射\s*\d+\s*轮|【最多|发动\s*(?:炮击|雷击|鱼雷|空袭|轰炸|攻击)')


def _resolve_buff_order(raw_skills, trigger):
    """判断角色 trigger 技能里 buff 段相对伤害段的先后（按技能描述原文顺序）。
    raw_skills: raw_store 中该角色的 skills 字典（含 BattleSkill1 等原文）。
    True=先 buff 后伤害（当次技能伤害吃到）；False=先伤害后 buff（当次吃不到）。"""
    field = _TRIGGER_FIELD.get(trigger)
    text = (raw_skills or {}).get(field, '')
    if not isinstance(text, str) or not text:
        return True
    buff_pos = [m.start() for m in _BUFF_DUR_RE.finditer(text) if m.group(4)]
    if not buff_pos:
        return True
    dmg = _DMG_SEG_RE.search(text)
    if not dmg:
        return True  # 无伤害段（纯 buff 技能），buff 必先生效
    return all(bp < dmg.start() for bp in buff_pos)


def _apply_single_skill_buffs(attacker, enemy, raw_skills, choice):
    """单次技能释放：应用该技能自带的 skill_buffs（与用户端时间轴同规则）。
    仅当描述顺序为「先于伤害」时当次生效；target=self→攻击方、enemy→敌方、air→不应用。
    直接修改传入对象（调用方需保证为副本）。"""
    trigger = _SINGLE_CHOICE_TRIGGER.get(choice)
    if not trigger:
        return
    # 技能内条件增伤（数据层回填 static_buffs，如前卫必杀「如对方正处于减速状态，
    # ①、②造成：20%额外伤害」→ 全伤害增加+0.2）：直接并入本次单次静态计算，
    # 不受 buff 先后顺序门槛影响（与 tools/calc.py._apply_single_skill_buffs 同段逻辑，
    # 2026-09-05 补齐——此前开发者端单次必杀漏计该 20%）
    atk = {'skill': attacker.skill_attack, 'burst': attacker.burst_attack,
           'support': attacker.support_attack}.get(trigger)
    _static = (atk or {}).get('static_buffs') if isinstance(atk, dict) else None
    if isinstance(_static, dict) and _static:
        for k, v in _static.items():
            std = _normalize_buff_key(k)
            attacker.buffs[std] = attacker.buffs.get(std, 0.0) + float(v)
    if not (attacker.skill_buffs or []):
        return
    # 顺序门槛已移除（用户口径 2026-09-05：技能自带 buff 单次伤害必须计入）——
    # 原 all-or-nothing 判定会因「被命中敌方减益写在伤害段之后」（如 前卫 移动速度-30%）
    # 把前面的 self buff（对空/命中+20%）一起丢掉
    for sb in attacker.skill_buffs:
        if sb.get('trigger') != trigger:
            continue
        tgt = sb.get('target', 'self') or 'self'
        if tgt == 'air':
            continue
        target_obj = enemy if tgt == 'enemy' else attacker.buffs
        for k, v in (sb.get('buffs') or {}).items():
            std = _normalize_buff_key(k)
            target_obj[std] = target_obj.get(std, 0.0) + float(v)


def _extract_skill_ignites(attacker):
    """从攻击方技能 parts 自动提取点燃配置（复制自 tools/calc.py）。"""
    out = []
    for skill in (getattr(attacker, 'skill_attack', {}) or {},
                  getattr(attacker, 'burst_attack', {}) or {},
                  getattr(attacker, 'support_attack', {}) or {}):
        cd = float(skill.get('cd', 0) or 0)
        for p in skill.get('parts', []) or []:
            ig = p.get('ignite') if isinstance(p, dict) else None
            if not ig:
                continue
            out.append({
                'count': int(ig.get('count', 1) or 1),
                'prob': float(ig.get('prob', 1.0) or 1.0),
                'damage': float(ig.get('dps', 0) or 0),
                'duration': float(ig.get('duration', 0) or 0),
                'cd': float(ig.get('cd', 0) or 0) or cd,
            })
    return out


# ==================== 时间轴总伤害模拟（复制自 tools/calc.py，适配返回结构化结果） ====================

def _timeline_windows(attacker, raw_skills, user_windows=None):
    """收集时间轴 buff 窗口（技能窗口 + 用户窗口），结构同用户端。"""
    windows = []
    cds = {
        'skill': float(attacker.skill_attack.get('cd', 0) or 0),
        'burst': float(attacker.burst_attack.get('cd', 0) or 0),
        'support': float(attacker.support_attack.get('cd', 0) or 0),
    }
    for sb in attacker.skill_buffs or []:
        trig = sb.get('trigger', '')
        cd = cds.get(trig, 0)
        if cd <= 0:
            continue
        buffs = {_normalize_buff_key(k): float(v) for k, v in (sb.get('buffs') or {}).items() if v}
        if not buffs:
            continue
        # before/after 按 target 判定（用户口径 2026-09-05）：self/team 增益先于伤害
        # （技能本体当次吃到，如前卫通常技 对空/命中+20%）；enemy 减益后于伤害（下轮起持续）。
        # 原 _resolve_buff_order all-or-nothing 会因「enemy 减益写在伤害段后」把 self buff
        # 的窗口也标成 after → 本体吃不到（前卫案例）
        windows.append({'trigger': trig, 'cd': cd, 'duration': float(sb.get('duration', 0) or 0),
                        'buffs': buffs, 'remaining': 0, 'count': 0,
                        'target': sb.get('target', 'self') or 'self',
                        'before_damage': (sb.get('target', 'self') or 'self') != 'enemy'})
    for w in user_windows or []:
        if w.get('source') != 'user':
            continue
        buffs = {_normalize_buff_key(k): float(v) for k, v in (w.get('buffs') or {}).items() if v}
        if not buffs:
            continue
        windows.append({'trigger': 'user', 'cd': 0, 'duration': float(w.get('duration', 0) or 0),
                        'buffs': buffs, 'remaining': float(w.get('duration', 0) or 0), 'count': 1,
                        'target': 'self', 'before_damage': True})
    return windows


def _active_enemy(base_enemy, windows):
    """当前生效敌方面板 = 基础 + Σ(remaining>0 且 target=enemy 的窗口 buffs)。"""
    active = dict(base_enemy)
    for w in windows:
        if w['remaining'] > 0 and w.get('target') == 'enemy':
            for k, v in w['buffs'].items():
                active[k] = active.get(k, 0.0) + v
    return active


def _active_buffs(base_buffs, windows):
    """当前生效 buffs = 基础 + Σ(remaining>0 窗口 buffs)。"""
    active = dict(base_buffs)
    for w in windows:
        if w['remaining'] > 0:
            for k, v in w['buffs'].items():
                active[k] = active.get(k, 0.0) + v
    return active


def _timeline_expected(factors):
    """技能/普攻 factors → 期望伤害（混合技能对 parts 求和）。"""
    if not factors:
        return 0.0
    if isinstance(factors, list):
        return sum(OutputHandler.calculate_final_damage(f)["expected"] for f in factors if f)
    if factors.get("type") == "混合技能":
        return sum(OutputHandler.calculate_final_damage(p)["expected"] for p in factors.get("parts", []) if p)
    return OutputHandler.calculate_final_damage(factors)["expected"]


def calc_total_damage_timeline(attacker, enemy, course, air, total_time,
                               raw_skills=None, user_windows=None, include_ignite=True):
    """时间轴模拟总伤害（1s 粒度，实时 buff 窗口），与用户端 calc_total_damage 同源同规则。

    - 触发时序：普攻首击 t=2；技能首击 t=CD（0s 不触发）；
    - before_damage 窗口先开启（当次伤害吃到），after 窗口后开启（影响后续）；
    - 点燃按基础 buff 全程静态计算。
    返回结构化结果 dict（totals/counts/skill_cds/windows/ignite/grand）。"""
    total = int(total_time)
    if total <= 0:
        total = 1
    base_buffs = dict(attacker.buffs)
    base_enemy = dict(enemy)
    windows = [w for w in _timeline_windows(attacker, raw_skills, user_windows)
               if w['duration'] > 0 and w['trigger'] != 'support' and w.get('target') != 'air']
    normals = [(i, float(f.get('cd', 0) or 0)) for i, f in enumerate(attacker.normal_attacks or [])
               if (f.get('cd', 0) or 0) > 0]
    skill_defs = [
        ('skill', '通常技', attacker.skill_attack, DamageCalculator.calculate_skill),
        ('burst', '必杀技', attacker.burst_attack, DamageCalculator.calculate_burst),
    ]
    # 无伤害段但带 CD 的纯 buff 技能（如光辉/加里波第通常技）同样进触发列表：
    # 触发时正常开/关 buff 窗口，只是没有伤害段可结算（has_dmg=False 跳过伤害累计）
    skill_cds = [(trig, label, float(atk.get('cd', 0) or 0), fn, bool(atk.get('parts')))
                 for trig, label, atk, fn in skill_defs
                 if (atk.get('cd', 0) or 0) > 0]

    # 技能内无持续时间的静态增伤（如「对航母造成：额外50%伤害」）：
    # 规则为一律仅对该技能出手结算生效——只在该技能触发瞬间临时注入，
    # 不影响普攻/其他技能/点燃（点燃仍按基础 buff 静态计算）。
    # 同技能自带持续窗口（如「全伤害增加15%，持续5秒」）已覆盖的键从静态中剔除，
    # 避免窗口 + 静态双重生效
    static_map = {}
    for _trig, _label, _atk, _fn in skill_defs:
        _sb = _atk.get('static_buffs')
        if not (isinstance(_sb, dict) and _sb):
            continue
        _wkeys = set()
        for _w in windows:
            if _w.get('trigger') == _trig and _w.get('target', 'self') == 'self' \
                    and isinstance(_w.get('buffs'), dict):
                _wkeys.update(_normalize_buff_key(k) for k in _w['buffs'])
        _sb = {k: v for k, v in _sb.items() if _normalize_buff_key(k) not in _wkeys}
        if _sb:
            static_map[_trig] = _sb

    totals = {'普攻': 0.0, '通常技': 0.0, '必杀技': 0.0, '支援技': 0.0}
    counts = {'普攻': 0, '通常技': 0, '必杀技': 0, '支援技': 0}
    # 时间轴逐次出手明细（proc）：供配置页「总伤害明细」折叠展示（用户口径 2026-09-05）
    proc_log = {'普攻': [], '通常技': [], '必杀技': [], '支援技': []}
    # 每秒伤害序列（不含点燃，点燃由 UI 均摊叠加）：供 DPS 曲线
    sec_dmg = [0.0] * max(total, 1)
    _proc_seq = {'普攻': 0, '通常技': 0, '必杀技': 0, '支援技': 0}

    def _record_proc(label, t, factors):
        """收集一次出手的伤害计算过程（单 factor / list / 混合技能 parts 皆可）。"""
        if not factors:
            return
        if isinstance(factors, dict) and factors.get('type') == '混合技能':
            items = [(f"（{p.get('damage_type', '')}段）", p) for p in factors.get('parts', [])]
        elif isinstance(factors, list):
            items = [(f"第{it.get('index', '?')}组", it) for it in factors]
        else:
            items = [('', factors)]
        blocks = []
        for sub, it in items:
            proc = it.get('proc') or []
            if proc:
                blocks.append((sub, [str(x) for x in proc]))
        if blocks:
            _proc_seq[label] += 1
            proc_log[label].append({'seq': _proc_seq[label], 't': float(t) + 1.0, 'blocks': blocks})
    # 技能/普攻触发采用"下次触发时刻"累积推进（替代整数取模）：
    # 1s 粒度下取模对 1.8/13.7 这类浮点 CD 会失配（9 % 1.8 ≈ 1.8 ≠ 0），
    # 导致该普攻/技能在整条时间轴几乎永不触发（如空想炮击普攻 60s 只打 1 次）
    skill_next = {trig: cd for trig, label, cd, fn, has_dmg in skill_cds}
    normal_next = {i: 2.0 for i, cd in normals}

    for t in range(total):
        # 1. 技能触发（首击 t=CD；触发点落在本秒 [t, t+1) 即结算，每秒至多一次；
        # before 窗口先开 → 算伤害 → after 窗口后开）
        for trig, label, cd, fn, has_dmg in skill_cds:
            if skill_next[trig] >= t + 1 - 1e-9:
                continue
            skill_next[trig] += cd
            while skill_next[trig] < t + 1 - 1e-9:
                skill_next[trig] += cd
            for w in windows:
                if w['trigger'] == trig and w.get('before_damage'):
                    w['remaining'] = w['duration']
                    w['count'] += 1
            attacker.buffs = _active_buffs(base_buffs, windows)
            # 技能内静态增伤仅本次出手生效：按加法并入对应增伤区
            # （下一步循环会重算 buffs，自动还原；与 calculate_raw_damage 的 sbuffs 合并同语义）
            _sb = static_map.get(trig)
            if _sb:
                for _k, _v in _sb.items():
                    _std = _normalize_buff_key(_k)
                    attacker.buffs[_std] = attacker.buffs.get(_std, 0.0) + float(_v)
            enemy_cur = _active_enemy(base_enemy, windows)
            if has_dmg:
                _factors = fn(attacker, enemy_cur, course, air)
                _exp = _timeline_expected(_factors)
                totals[label] += _exp
                if 0 <= t < len(sec_dmg):
                    sec_dmg[t] += _exp
                _record_proc(label, t, _factors)
            # 纯 buff 技能（无伤害段）同样计次：对比总轴需体现释放节奏（伤害为 0 也要写触发几次）
            counts[label] += 1
            for w in windows:
                if w['trigger'] == trig and not w.get('before_damage'):
                    w['remaining'] = w['duration']
                    w['count'] += 1
        # 2. 普攻触发（首击 t=2，吃到当步已生效窗口；触发点累积推进，浮点 CD 不再失配）
        if t >= 2 and normals:
            triggered = []
            for i, cd in normals:
                if normal_next[i] < t + 1 - 1e-9:
                    triggered.append((i, cd))
                    normal_next[i] += cd
                    while normal_next[i] < t + 1 - 1e-9:
                        normal_next[i] += cd
            if triggered:
                attacker.buffs = _active_buffs(base_buffs, windows)
                enemy_cur = _active_enemy(base_enemy, windows)
                all_factors = DamageCalculator.calculate_normal_all(attacker, enemy_cur, course, air) or []
                for i, cd in triggered:
                    if i < len(all_factors) and all_factors[i]:
                        _exp = _timeline_expected(all_factors[i])
                        totals['普攻'] += _exp
                        counts['普攻'] += 1
                        if 0 <= t < len(sec_dmg):
                            sec_dmg[t] += _exp
                        _record_proc('普攻', t, [all_factors[i]])
        # 3. 窗口递减
        for w in windows:
            if w['remaining'] > 0:
                w['remaining'] -= 1

    attacker.buffs = base_buffs

    # 点燃（静态：按基础 buff 全程计算）
    ignite = {'raw': 0.0, 'actual': 0.0, 'details': []}
    if include_ignite:
        burn = DamageCalculator.calculate_burn(attacker, enemy, total,
                                               _extract_skill_ignites(attacker), course, air)
        ignite['raw'] = burn.get('total_raw_ignite', 0.0)
        ignite['actual'] = burn.get('total_actual_ignite', 0.0)
        ignite['details'] = burn.get('details', [])

    return {
        'totals': totals,
        'counts': counts,
        'skill_cds': [(trig, label, cd) for trig, label, cd, _fn, _hd in skill_cds],
        'windows': windows,
        'ignite': ignite,
        'grand': sum(totals.values()) + ignite['actual'],
        'total_time': total,
        'procs': proc_log,
        'series': sec_dmg,
    }
