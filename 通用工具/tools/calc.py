# -*- coding: utf-8 -*-
"""calc tools."""
import copy
from qwen_agent.tools.base import BaseTool
from core.timeline import (
    apply_single_skill_buffs,
    active_buffs as _active_buffs,
    active_enemy as _active_enemy,
    build_buff_windows,
    expected_damage,
    extract_skill_ignites as _extract_skill_ignites,
)
from core.damage_formula import DamageCalculator
from core.output_handler import OutputHandler
from .common import _load_combatants, _resolve_calc_context, _resolve_course_air, _validate_course_air, _normalize_buff_key

# ======================================================================
# 工具 2-4：单次伤害计算（普攻/通常技/必杀技/支援技）通用基类
# ======================================================================

class _SingleCombatBase(BaseTool):
    """单次伤害计算工具基类，子类设置 _choice 与 _label。"""

    _choice: int = 1
    _label: str = "普攻"

    def _do_calc(self, params) -> str:
        app = self.cfg['app']
        session_overrides = self.cfg.get('session_overrides', {})
        params = self._verify_json_format_args(params)

        # 从参数读取攻防方，缺失时回退到会话级 calc_context
        attacker_name, enemy_name = _resolve_calc_context(params, session_overrides)
        if not attacker_name:
            return "缺少攻击方角色名，请先提供攻击方。"
        # enemy_name 可空：不填则输出原始伤害（无削减）

        course, air = _resolve_course_air(params, session_overrides)

        err = _validate_course_air(course, air)
        if err:
            return err
        attacker, enemy, err = _load_combatants(app, attacker_name, enemy_name, session_overrides)
        if err:
            return err
        # 单次技能释放：应用该技能自带 skill_buffs（描述中先于伤害的 buff 当次生效；
        # self→攻击方、enemy→敌方、air（空中单位专属）不应用）。
        # 空会话时 _load_combatants 不深拷贝，attacker 可能共享 storage 原对象——
        # 先深拷贝再临时叠加，避免污染存档数据。
        attacker = copy.deepcopy(attacker)
        apply_single_skill_buffs(attacker, enemy, self._choice, _normalize_buff_key)
        res = DamageCalculator.compute_all_attacks(attacker, enemy, course, air, self._choice)
        if "error" in res:
            return f"计算{self._label}时出错: {res['error']}"
        if not res:
            return f"该角色未配置{self._label}，无法计算。"
        return OutputHandler.format_results(res, [self._label])


class CalcNormalAttack(_SingleCombatBase):
    """【伤害计算-分支1】计算攻击方所有普攻的单次释放伤害（命中率/暴击率/未削减/正常/暴击/期望）。"""
    name = 'calc_normal_attack'
    description = '【伤害计算-分支1】计算攻击方所有普攻的单次释放伤害（命中率/暴击率/未削减/正常/暴击/期望）。'
    parameters = {
        'type': 'object',
        'properties': {
            'attacker_name': {'type': 'string', 'description': '攻击方角色名'},
            'enemy_name': {'type': 'string', 'description': '敌方角色名（从已有角色加载其防御属性）'},
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': '同航'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '均势'},
        },
        'required': [],
    }
    _choice = 1
    _label = "普攻"

    def call(self, params, **kwargs):
        return self._do_calc(params)


class CalcSkillAttack(_SingleCombatBase):
    """【伤害计算-分支2】计算通常技的单次释放伤害（支持混合技能/纯点燃技能）。"""
    name = 'calc_skill_attack'
    description = '【伤害计算-分支2】计算通常技的单次释放伤害（支持混合技能/纯点燃技能）。'
    parameters = {
        'type': 'object',
        'properties': {
            'attacker_name': {'type': 'string', 'description': '攻击方角色名'},
            'enemy_name': {'type': 'string', 'description': '敌方角色名（从已有角色加载其防御属性）'},
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': '同航'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '均势'},
        },
        'required': [],
    }
    _choice = 2
    _label = "通常技"

    def call(self, params, **kwargs):
        return self._do_calc(params)


class CalcBurstAttack(_SingleCombatBase):
    """【伤害计算-分支3】计算必杀技的单次释放伤害（支持混合技能/纯点燃技能）。"""
    name = 'calc_burst_attack'
    description = '【伤害计算-分支3】计算必杀技的单次释放伤害（支持混合技能/纯点燃技能）。'
    parameters = {
        'type': 'object',
        'properties': {
            'attacker_name': {'type': 'string', 'description': '攻击方角色名'},
            'enemy_name': {'type': 'string', 'description': '敌方角色名（从已有角色加载其防御属性）'},
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': '同航'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '均势'},
        },
        'required': [],
    }
    _choice = 3
    _label = "必杀技"

    def call(self, params, **kwargs):
        return self._do_calc(params)


class CalcSupportAttack(_SingleCombatBase):
    """【伤害计算-分支4】计算支援技的单次释放伤害。"""
    name = 'calc_support_attack'
    description = '【伤害计算-分支4】计算支援技的单次释放伤害。'
    parameters = {
        'type': 'object',
        'properties': {
            'attacker_name': {'type': 'string', 'description': '攻击方角色名'},
            'enemy_name': {'type': 'string', 'description': '敌方角色名（从已有角色加载其防御属性）'},
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': '同航'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '均势'},
        },
        'required': [],
    }
    _choice = 4
    _label = "支援技"

    def call(self, params, **kwargs):
        return self._do_calc(params)


# ======================================================================
# 工具 5：点燃伤害计算（需时间）
# ======================================================================

class CalcBurnDamage(BaseTool):
    """【伤害计算-分支5】计算点燃总伤害（普攻高爆弹点燃 + 可选技能点燃），需提供燃烧总时间。"""
    name = 'calc_burn_damage'
    description = '【伤害计算-分支5】计算点燃总伤害（普攻高爆弹点燃 + 可选技能点燃），需提供燃烧总时间。'
    parameters = {
        'type': 'object',
        'properties': {
            'attacker_name': {'type': 'string', 'description': '攻击方角色名'},
            'enemy_name': {'type': 'string', 'description': '敌方角色名（可空，不填则输出原始伤害）'},
            'burn_time': {'type': 'number', 'description': '燃烧总时间（秒）'},
            'burn_skills': {
                'type': 'array',
                'description': '技能点燃配置列表（可选）。若用户描述了技能点燃（如 6次100%概率100伤害10秒 CD30），按字段解析填入；普攻自带点燃无需填',
                'items': {
                    'type': 'object',
                    'properties': {
                        'count': {'type': 'integer', 'description': '触发次数，如 6'},
                        'prob': {'type': 'number', 'description': '概率，小数（如 1.0 表示 100%）'},
                        'damage': {'type': 'number', 'description': '固定伤害值'},
                        'duration': {'type': 'number', 'description': '持续时间（秒）'},
                        'cd': {'type': 'number', 'description': '技能 CD（秒）'},
                    },
                    'required': ['count', 'prob', 'damage', 'duration', 'cd'],
                },
            },
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': '同航'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '均势'},
        },
        'required': ['burn_time'],
    }

    def call(self, params, **kwargs):
        app = self.cfg['app']
        session_overrides = self.cfg.get('session_overrides', {})
        params = self._verify_json_format_args(params)

        # 从参数读取攻防方，缺失时回退到会话级 calc_context
        attacker_name, enemy_name = _resolve_calc_context(params, session_overrides)
        if not attacker_name:
            return "缺少攻击方角色名，请先提供攻击方。"

        burn_time = params.get('burn_time')
        if not burn_time or burn_time <= 0:
            return "请提供燃烧总时间（秒），例如「燃烧20秒」。"
        burn_skills = params.get('burn_skills', [])
        course, air = _resolve_course_air(params, session_overrides)

        err = _validate_course_air(course, air)
        if err:
            return err
        attacker, enemy, err = _load_combatants(app, attacker_name, enemy_name, session_overrides)
        if err:
            return err
        # 把 burn_skills 列表转为 calculate_burn 需要的 dict 列表；
        # 用户未提供时自动从攻击方技能提取点燃段（技能描述中的点燃效果）
        if not burn_skills:
            burn_skills = _extract_skill_ignites(attacker)
        skill_list = []
        for bs in burn_skills or []:
            if hasattr(bs, "model_dump"):
                bs = bs.model_dump()
            skill_list.append({
                "count": int(bs.get("count", 0)),
                "prob": float(bs.get("prob", 0)),
                "damage": float(bs.get("damage", 0)),
                "duration": float(bs.get("duration", 0)),
                "cd": float(bs.get("cd", 0)),
            })
        burn_result = DamageCalculator.calculate_burn(attacker, enemy, burn_time, skill_list, course, air)
        return OutputHandler.format_burn_result(burn_result)


# ======================================================================
# 工具 6：总伤害计算（需时间，普攻+通常技+必杀技+点燃）
# ======================================================================

class CalcTotalDamage(BaseTool):
    """【伤害计算-分支6】计算总时间内的累计总伤害（普攻/通常技/必杀技按 CD 计算触发次数，可选点燃），需提供总时间。"""
    name = 'calc_total_damage'
    description = '【伤害计算-分支6】计算总时间内的累计总伤害（普攻/通常技/必杀技按 CD 计算触发次数，可选点燃），需提供总时间。'
    parameters = {
        'type': 'object',
        'properties': {
            'attacker_name': {'type': 'string', 'description': '攻击方角色名'},
            'enemy_name': {'type': 'string', 'description': '敌方角色名（可空，不填则输出原始伤害）'},
            'total_time': {'type': 'number', 'description': '总时间（秒）'},
            'include_burn': {'type': 'boolean', 'description': '是否包含点燃伤害，默认 True', 'default': True},
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': '同航'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '均势'},
        },
        'required': ['total_time'],
    }

    def call(self, params, **kwargs):
        app = self.cfg['app']
        session_overrides = self.cfg.get('session_overrides', {})
        params = self._verify_json_format_args(params)

        # 从参数读取攻防方，缺失时回退到会话级 calc_context
        attacker_name, enemy_name = _resolve_calc_context(params, session_overrides)
        if not attacker_name:
            return "缺少攻击方角色名，请先提供攻击方。"

        total_time = params.get('total_time')
        if not total_time or total_time <= 0:
            return "请提供总时间（秒），例如「60秒总伤害」。"
        include_burn = params.get('include_burn', True)
        course, air = _resolve_course_air(params, session_overrides)

        err = _validate_course_air(course, air)
        if err:
            return err
        attacker, enemy, err = _load_combatants(app, attacker_name, enemy_name, session_overrides)
        if err:
            return err
        # 时间轴模拟：buff 窗口（用户"持续N秒" + 角色技能 skill_buffs 持续 buff）在其持续期内
        # 作用于所有普攻/技能伤害——总伤害必须按时间轴模拟才能体现 buff 覆盖。
        user_windows = session_overrides.get("buff_windows", [])
        return _calc_total_damage_timeline(
            app, attacker, enemy,
            non_burn_types=["普攻", "通常技", "必杀技"],
            has_ignite=include_burn,
            course=course, air=air, total_time=total_time,
            attacker_name=attacker_name, enemy_name=enemy_name,
            user_windows=user_windows,
        )


# ======================================================================
# 总伤害计算（参考 flows.py:241-372 的逻辑，去掉交互输入）
# ======================================================================

# ======================================================================
# 总伤害 · 时间轴模拟（buff 窗口：技能持续 buff + 用户"持续N秒"）
# ======================================================================

def _calc_total_damage_timeline(app, attacker, enemy, non_burn_types, has_ignite,
                                course, air, total_time, attacker_name, enemy_name,
                                user_windows=None) -> str:
    """时间轴模拟（1s 粒度）总伤害：buff 窗口在其持续期内作用于所有普攻/技能伤害。
    - 触发时序：普攻首击 t=2；技能首击 t=CD（等第一个 CD 满才触发，0s 不触发）；
    - buff 生效按技能描述顺序：before_damage 窗口（描述中 buff 段在伤害段前）先开启，
      当次技能伤害吃到；after 窗口后开启，当次不吃、影响后续普攻/技能；
    - 普攻计算（吃到当步新开的全部窗口）→ 步末窗口 remaining-1；
    - 用户「持续N秒」窗口开局即生效；
    - 点燃保留静态（按基础 buff 全程计算，不细分窗口）。"""
    total = int(total_time)
    if total <= 0:
        total = 1
    base_buffs = dict(attacker.buffs)
    base_enemy = dict(enemy)
    # 单人模拟不算支援技（支援技为后排技能，单人对决无后排定位）；
    # 支援技触发的 buff 窗口同样排除（技能未释放，buff 无从开启）；
    # target='air'（仅对空中单位生效）排除——单人对决敌方为海上单位，不应用
    windows = [w for w in build_buff_windows(attacker, user_windows, _normalize_buff_key)
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

    # 技能内条件增伤（数据层回填 static_buffs，如「如对方处于减速状态，造成：50%额外伤害」）：
    # 直接并入该技能每次出手的单次静态计算（加法进对应增伤区）；其余持续类 buff
    # 仍走窗口管线正常计算，两者互不干涉
    static_map = {}
    for _trig, _label, _atk, _fn in skill_defs:
        _sb = _atk.get('static_buffs')
        if isinstance(_sb, dict) and _sb:
            static_map[_trig] = _sb

    totals = {'普攻': 0.0, '通常技': 0.0, '必杀技': 0.0, '支援技': 0.0}
    counts = {'普攻': 0, '通常技': 0, '必杀技': 0, '支援技': 0}
    # 技能/普攻触发采用"下次触发时刻"累积推进（替代整数取模）：
    # 1s 粒度下取模对 1.8/13.7 这类浮点 CD 会失配（9 % 1.8 ≈ 1.8 ≠ 0），
    # 导致该普攻/技能在整条时间轴几乎永不触发（如空想炮击普攻 60s 只打 1 次）
    skill_next = {trig: cd for trig, label, cd, fn, has_dmg in skill_cds}
    normal_next = {i: 2.0 for i, cd in normals}

    for t in range(total):
        # 1. 技能触发：首击 t=CD（触发点落在本秒 [t, t+1) 即结算，每秒至多一次）；
        #    按描述顺序——before_damage 窗口先开启（当次伤害吃到），
        #    再按当前窗口态算伤害，最后开启 after 窗口（当次不吃、影响后续）
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
            # 技能内条件增伤仅本次出手生效：按加法并入对应增伤区
            # （下一步循环会重算 buffs，自动还原；与 user_engine 时间轴同规则）
            _sb = static_map.get(trig)
            if _sb:
                for _k, _v in _sb.items():
                    _std = _normalize_buff_key(_k)
                    attacker.buffs[_std] = attacker.buffs.get(_std, 0.0) + float(_v)
            enemy_cur = _active_enemy(base_enemy, windows)
            if has_dmg:
                totals[label] += expected_damage(fn(attacker, enemy_cur, course, air), OutputHandler.calculate_final_damage)
            # 纯 buff 技能（无伤害段）同样计次：总轴需体现释放节奏（伤害为 0 也要写触发几次）
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
                        totals['普攻'] += expected_damage(all_factors[i], OutputHandler.calculate_final_damage)
                        counts['普攻'] += 1
        # 3. 窗口递减
        for w in windows:
            if w['remaining'] > 0:
                w['remaining'] -= 1

    attacker.buffs = base_buffs

    # 点燃（保留静态：按基础 buff 全程计算）
    ignite_actual = 0.0
    if has_ignite:
        burn = DamageCalculator.calculate_burn(attacker, enemy, total_time,
                                               _extract_skill_ignites(attacker), course, air)
        ignite_actual = burn.get("total_actual_ignite", 0.0)

    grand = sum(totals.values()) + ignite_actual
    lines = []
    lines.append("=" * 60)
    lines.append("   总伤害计算结果（时间轴模拟 · 1s 粒度）")
    lines.append("=" * 60)
    lines.append(f"总时间: {total_time:.1f}秒 | 攻击方: {attacker_name} | 敌方: {enemy_name}")
    lines.append(f"航向: {course} | 制空: {air}")
    shown = [w for w in windows if w['count'] > 0 or w['trigger'] == 'user']
    if shown:
        lines.append("-" * 60)
        lines.append("【buff 窗口】")
        trig_cn = {'skill': '通常技', 'burst': '必杀技', 'support': '支援技'}
        for w in shown:
            # 正值不带 + 号、负值保留 - 号（避免 增加+-10% 奇怪格式）
            desc = '、'.join(f"{k}{v:.0%}" if abs(v) < 1 else f"{k}{v:g}" for k, v in w['buffs'].items())
            order_mark = '' if w['trigger'] == 'user' else ('（先于伤害）' if w.get('before_damage') else '（后于伤害）')
            target_mark = '（对敌）' if w.get('target') == 'enemy' else ''
            if w['trigger'] == 'user':
                lines.append(f"  ·（用户）{desc} 持续{w['duration']:g}s（开局生效）")
            else:
                lines.append(f"  · {trig_cn.get(w['trigger'], w['trigger'])}(CD{w['cd']:g}s): "
                             f"{desc} 持续{w['duration']:g}s → 触发{w['count']}次{order_mark}{target_mark}")
    lines.append("-" * 60)
    lines.append(f"  普攻总伤害: {totals['普攻']:.2f}（{counts['普攻']}次）")
    for trig, label, cd, _fn, _has in skill_cds:
        if counts[label] > 0:
            lines.append(f"  {label}总伤害: {totals[label]:.2f}（CD={cd:g}s, 触发{counts[label]}次）")
    if ignite_actual > 0:
        lines.append(f"  点燃实际伤害: {ignite_actual:.2f}（按基础 buff 全程计算）")
    lines.append("-" * 60)
    lines.append(f"  合计实际总伤害: {grand:.2f}")
    lines.append("=" * 60)
    return "\n".join(lines)


def _skill_total_damage(skill_attack, attacker, enemy, course, air, total_time):
    """计算单个技能（通常技/必杀技/支援技）的总伤害，返回 (total, cd, note)"""
    parts = skill_attack.get("parts", [])
    cd = skill_attack.get("cd", 0)
    note = ""
    if not parts:
        return 0.0, 0.0, "（未配置）"
    if cd == 0:
        return 0.0, 0.0, "（CD 为 0，未触发）"
    factors = None
    if skill_attack is attacker.skill_attack:
        factors = DamageCalculator.calculate_skill(attacker, enemy, course, air)
    elif skill_attack is attacker.burst_attack:
        factors = DamageCalculator.calculate_burst(attacker, enemy, course, air)
    elif skill_attack is attacker.support_attack:
        factors = DamageCalculator.calculate_support(attacker, enemy, course, air)
    else:
        factors = None
    if not factors:
        return 0.0, cd, ""
    # 混合技能：对 parts 求和
    if factors.get("type") == "混合技能":
        expected = 0.0
        for part in factors.get("parts", []):
            expected += OutputHandler.calculate_final_damage(part)["expected"]
    else:
        expected = OutputHandler.calculate_final_damage(factors)["expected"]
    count = int(total_time // cd)
    total = expected * count
    # note 只用于补充说明（CD/触发次数由调用处统一拼接，避免重复）
    note = ""
    return total, cd, note
