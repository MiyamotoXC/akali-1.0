# -*- coding: utf-8 -*-
"""
FX 开发者工具 - Streamlit Web 应用

功能：
- 数据查询 / 手动更新 / 表格导入
- 自定义伤害计算器

启动：streamlit run app.py
"""
import json
import copy
import re
import sys
import os
import streamlit as st
import pandas as pd

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from core import data_api, business_logic, user_engine
from core.data_api import _extract_skill_buffs_from_text, extract_loop_cd

st.set_page_config(page_title='FX 开发者工具', page_icon='⚙️', layout='wide')


def _single_factor_row(name: str, p: dict, raw_only: bool = False):
    """把单次释放的因子结果收集为一行表格数据。

    raw_only=False：与用户端同口径（未削减/实际/期望/最高暴击）。
    raw_only=True：无防守方口径——无实际伤害列，
    最高暴击/期望伤害以原始伤害为基数派生（不做防御衰减/敌方减伤/克制修正）。"""
    hit_count = p.get('hit_count', 1)
    original = p.get('base_damage', 0.0) * hit_count
    if raw_only:
        critical = original * (1 + p.get('crit_dmg_mult', 0.0))
        expected = original * p.get('crit_expect', 1.0) * p.get('hit_rate', 1.0)
        normal = None
    else:
        normal = original * p.get('defense_decay', 1.0) * p.get('enemy_reduction', 1.0) * p.get('modifier', 1.0)
        critical = normal * (1 + p.get('crit_dmg_mult', 0.0))
        expected = normal * p.get('crit_expect', 1.0) * p.get('hit_rate', 1.0)
    return {
        '项': name,
        '命中率': f"{p.get('hit_rate', 1.0)*100:.1f}%",
        '暴击率': f"{p.get('crit_rate', 0.0)*100:.1f}%",
        '命中数': hit_count,
        '原始伤害': round(original),
        '实际伤害': '—' if normal is None else round(normal),
        '期望伤害': round(expected),
        '最高暴击': round(critical),
    }


def _render_single_result(label: str, res, raw_only: bool = False, sub: str = ''):
    """渲染单次计算结果（普攻列表/单技能/混合技能）：表格 + 折叠的伤害明细（计算过程）。"""
    rows = []
    factors = []          # [(名称, factor)] —— 供下方折叠明细取 proc
    if isinstance(res, list):
        for item in res:
            nm = f"{sub} 第{item.get('index', '?')}组"
            rows.append(_single_factor_row(nm, item, raw_only))
            factors.append((nm, item))
    elif res.get('type') == '混合技能':
        for p in res.get('parts', []):
            nm = f"{sub}（{p.get('damage_type', '')}段）"
            rows.append(_single_factor_row(nm, p, raw_only))
            factors.append((nm, p))
    else:
        nm = sub or label
        rows.append(_single_factor_row(nm, res, raw_only))
        factors.append((nm, res))
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # 伤害明细（计算过程）：逐段列出完整运算式，默认折叠（结果区不被过程冲散）
    details = [(nm, f.get('proc') or []) for nm, f in factors
               if isinstance(f, dict) and f.get('proc')]
    if details:
        with st.expander('伤害明细（计算过程）', expanded=False):
            for nm, proc in details:
                st.markdown(f'**{nm}**')
                st.text('\n'.join(str(x) for x in proc))


def _build_custom_combat_data(ship: dict, skill_versions: dict):
    """由自定义角色输入构建引擎用 CombatData（技能无 CD，仅支持单次计算）。"""
    atk = user_engine.CombatData()
    atk.name = ship.get('name', '自定义')
    atk.ship_type = ship.get('ship_type', '')
    atk.ammo_type = ship.get('ammo_type', '') or '通常'
    for k in atk.attack_panel:
        atk.attack_panel[k] = float(ship.get('panel', {}).get(k, 0) or 0)
    for k, v in (ship.get('buffs') or {}).items():
        try:
            atk.buffs[user_engine._normalize_buff_key(k)] = float(v)
        except (TypeError, ValueError):
            pass
    field_map = {'通常技': 'skill_attack', '必杀技': 'burst_attack', '支援技': 'support_attack'}
    for label, field in field_map.items():
        sv = skill_versions.get(label)
        if not sv:
            continue
        segments, _sb = data_api._resolve_skill(sv)
        if not segments:
            continue
        parts = [{'hit': int(s.get('hit_count', 1) or 1),
                  'multiplier': f"{s['multiplier'] * 100:g}%",
                  'damage_type': s.get('damage_type', '炮击')} for s in segments]
        setattr(atk, field, {'cd': 0.0, 'parts': parts})
    return atk


def _load_talent_module():
    """以合成包方式加载 通用工具/tools/talent.py（权威天赋解析实现，含相对导入 .common）。
    开发者端只用解析函数；talent.py 顶部的 BaseTool 依赖 qwen_agent，
    开发者端运行环境缺失时用最小桩替身（SetSessionTalent 类不会被实例化）。"""
    import importlib.util
    import types
    tools_dir = os.path.normpath(os.path.join(_SCRIPT_DIR, '..', '通用工具', 'tools'))
    pkg_name = 'ak_dev_tools_pkg'
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [tools_dir]
        sys.modules[pkg_name] = pkg
    try:
        import qwen_agent.tools.base  # noqa: F401
    except ImportError:
        stub_base = types.ModuleType('qwen_agent.tools.base')

        class BaseTool:
            def __init__(self, cfg=None):
                self.cfg = cfg or {}

        stub_base.BaseTool = BaseTool
        sys.modules.setdefault('qwen_agent', types.ModuleType('qwen_agent'))
        sys.modules.setdefault('qwen_agent.tools', types.ModuleType('qwen_agent.tools'))
        sys.modules['qwen_agent.tools.base'] = stub_base
    for mod_name, filename in ((pkg_name + '.common', 'common.py'), (pkg_name + '.talent', 'talent.py')):
        spec = importlib.util.spec_from_file_location(mod_name, os.path.join(tools_dir, filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[pkg_name + '.talent']


_talent_mod = _load_talent_module()
_talent_resolve = _talent_mod.resolve_talents


def _resolve_ship_talent(ship_name: str, code: str, store):
    """解析角色天赋编码（复用用户端 talent.py 实现）。
    resolve_talents 需要 get_raw_ship 接口，开发者端按正规名精确取 raw 存储。"""

    class _RawStoreShim:
        def __init__(self, s):
            self._store = s

        def get_raw_ship(self, name):
            if self._store is None:
                return name, None
            return name, self._store.storage.get(name)

    return _talent_resolve(_RawStoreShim(store), ship_name, code)


def _apply_talent_effects(cd, eff):
    """把天赋效果叠加到 CombatData：面板增量（攻/防面板归桶）、增伤 buff、舰种专属增伤、
    技能 CD 缩减（百分比按四舍五入取整为实际 CD）。
    敌方减免类效果（被攻击伤害下降等）是角色作为防守方的属性，攻击侧不计。"""
    for k, v in (eff.get('panel') or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if k in cd.attack_panel:
            cd.attack_panel[k] = float(cd.attack_panel.get(k, 0) or 0) + fv
        elif k in cd.defense_panel:
            cd.defense_panel[k] = float(cd.defense_panel.get(k, 0) or 0) + fv
    for k, v in (eff.get('buffs') or {}).items():
        try:
            cd.buffs[k] = float(cd.buffs.get(k, 0) or 0) + float(v)
        except (TypeError, ValueError):
            pass
    if eff.get('ship_bonus'):
        stb = dict(getattr(cd, 'ship_type_damage_bonus', None) or {})
        for k, v in eff['ship_bonus'].items():
            try:
                stb[k] = float(stb.get(k, 0) or 0) + float(v)
            except (TypeError, ValueError):
                pass
        cd.ship_type_damage_bonus = stb
    if eff.get('cd'):
        _talent_mod.apply_cd_effects(cd, eff['cd'])


def _apply_defender_talent_effects(cd, eff):
    """把天赋效果按防守方口径叠加到 CombatData（叠加后再 to_enemy_dict 输出）。
    - panel：仅防御相关字段生效（装甲/对空/机动/幸运/索敌/回避率/防暴击率/血量），
      纯攻击面板（炮击/雷击/航空/装填/命中）对防守无意义，跳过
    - enemy：被攻击减免类（全伤害/各伤害类型/大小型攻击伤害减免）→ buffs，防暴击率 → 防御面板
    - buffs / ship_bonus：攻击侧增伤，防守方不适用"""
    _DEF_PANEL = {'装甲', '对空', '机动', '幸运', '索敌', '血量', '防暴击率', '回避率'}
    for k, v in (eff.get('panel') or {}).items():
        if k not in _DEF_PANEL:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if k in ('幸运', '索敌'):
            cd.attack_panel[k] = float(cd.attack_panel.get(k, 0) or 0) + fv
        else:
            cd.defense_panel[k] = float(cd.defense_panel.get(k, 0) or 0) + fv
    for k, v in (eff.get('enemy') or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if k == '防暴击率':
            cd.defense_panel['防暴击率'] = float(cd.defense_panel.get('防暴击率', 0) or 0) + fv
        else:
            cd.buffs[k] = float(cd.buffs.get(k, 0) or 0) + fv


def _render_damage_result(result: dict):
    """渲染伤害结果（分段表格 + 总计）。
    原始伤害 = 最终攻击力 × 增伤系数 × 技能倍率（enemy_used=False）；
    传了防守方（enemy_used=True）时附加 防御衰减/克制修正/实际伤害 列。"""
    enemy_used = bool(result.get('enemy_used'))
    rows = []
    for seg in result['segments']:
        # 公式因子 + 单次伤害结果（段内值为单次，未乘命中数）
        row = {
            '段': f"段{seg.get('segment_index', '?')}",
            '伤害类型': seg['damage_type'],
            '最终攻击力': round(seg['final_attack']),
            '增伤系数': f"{seg['damage_increase']:.4f}",
            '倍率': f"{seg['multiplier']:.4f}",
            '命中数': seg['hit_count'],
            '单次原始伤害': round(seg['base_damage']),
            '单次暴击伤害': round(seg['critical_damage']),
            '单次期望伤害': round(seg['expected_damage']),
        }
        if enemy_used:
            actual_single = seg['base_damage'] * seg.get('defense_decay', 1.0) \
                * seg.get('enemy_reduction', 1.0) * seg.get('modifier', 1.0)
            row['防御衰减'] = f"{seg.get('defense_decay', 1.0):.4f}"
            row['克制修正'] = f"{seg.get('modifier', 1.0):.4f}"
            row['单次实际伤害'] = round(actual_single)
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # 总计（已乘命中数）
    if enemy_used:
        total_cols = st.columns(4)
        with total_cols[0]:
            st.metric('总原始伤害', f"{result.get('total_original', 0):.0f}")
        with total_cols[1]:
            st.metric('总实际伤害', f"{result.get('total_normal', 0):.0f}")
        with total_cols[2]:
            st.metric('总暴击伤害', f"{result.get('total_critical', 0):.0f}")
        with total_cols[3]:
            st.metric('总期望伤害', f"{result.get('total_expected', 0):.0f}")
    else:
        total_cols = st.columns(3)
        with total_cols[0]:
            st.metric('总原始伤害', f"{result.get('total_original', 0):.0f}")
        with total_cols[1]:
            st.metric('总暴击伤害', f"{result.get('total_critical', 0):.0f}")
        with total_cols[2]:
            st.metric('总期望伤害', f"{result.get('total_expected', 0):.0f}")


# 侧边栏
with st.sidebar:
    st.title('FX 开发者工具')
    page = st.radio('功能选择', ['角色查询', '手动更新', '表格导入', '伤害计算器'])

    st.divider()
    try:
        raw_store, data_store_obj = data_api.load_stores()
        st.success(f'底层 {len(raw_store.storage)} 个 | 计算 {len(data_store_obj.storage)} 个')
    except Exception as e:
        st.error(str(e))
        raw_store, data_store_obj = None, None


# ======================================================================
# 角色查询
# ======================================================================

if page == '角色查询':
    st.header('角色查询')

    if raw_store is None:
        st.error('存储未初始化，请检查 .env 配置')
    else:
        query = st.text_input('输入 角色名/昵称（实时联想，如：拉菲、北卡、太太改）',
                               placeholder='拉菲 / 北卡 / 翔鹤…')
        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button('查询', type='primary'):
                result = data_api.query_ship(raw_store, data_store_obj, query)
                if 'error' in result:
                    st.error(result['error'])
                else:
                    st.session_state['query_result'] = result

        with col2:
            if st.button('加载全部列表'):
                ships = data_api.list_all_ships(raw_store, data_store_obj)
                st.session_state['all_ships'] = ships

        # 输入联想：键入即列出匹配的完整角色（昵称/别名/部分名），选中立即查看
        if (query or '').strip():
            try:
                suggests = data_api.suggest_ships(raw_store, query)
            except Exception:
                suggests = []
            if suggests:
                name_by_label = {c['label']: c['name'] for c in suggests}
                sel = st.selectbox('联想候选（键入即匹配，选中即查看详情）',
                                   [''] + list(name_by_label.keys()), key='ship_suggest_sel')
                if sel:
                    st.session_state['query_result'] = data_api.query_ship(
                        raw_store, data_store_obj, name_by_label[sel])
            else:
                st.caption('无匹配候选——可点上方「查询」按 昵称/精确名 检索')

        if 'all_ships' in st.session_state:
            ships = st.session_state['all_ships']

            # 缺失信息标注：缺什么写什么，信息齐全不写备注
            def _missing_mark(s):
                raw = raw_store.get(s['name']) or {}
                missing = []
                if not s['has_data']:
                    missing.append('计算表')
                if not raw.get('normal_attacks'):
                    missing.append('普攻')
                sk = raw.get('skills') or {}
                if not any(sk.get(f) for f in ('LeaderSkill', 'SupportSkill', 'BattleSkill1', 'BattleSkill2')):
                    missing.append('技能')
                if not raw.get('talents'):
                    missing.append('天赋')
                return f"（缺{'、'.join(missing)}）" if missing else ''

            # 阵营按钮顺序与游戏阵营栏一致，未收录的阵营排其后，无阵营最后
            _nation_order = ['凤栖', '洛蒙瑞亚', '尤奈特', '纳谢尔', '沃尔克', '奥鲁加', '八咫', '列加杜', '伽纳伊']
            by_nation = {}
            for s in ships:
                nat = '无阵营' if s['nation'] == '未知阵营' else s['nation']
                by_nation.setdefault(nat, []).append(s)
            extras = sorted(n for n in by_nation if n not in _nation_order and n != '无阵营')
            nation_tabs = (['全部'] + [n for n in _nation_order if n in by_nation]
                           + extras + (['无阵营'] if '无阵营' in by_nation else []))
            nation = st.radio('阵营', nation_tabs, horizontal=True)
            pool = ships if nation == '全部' else by_nation[nation]
            options = [''] + [s['name'] + _missing_mark(s) for s in pool]
            selected = st.selectbox(f'选择角色（{nation}，共 {len(pool)} 个）', options, key='ship_sel_nation')
            ship_name = selected.split('（缺')[0]
            if ship_name and st.button('查看详情', key='ship_view_nation'):
                result = data_api.query_ship(raw_store, data_store_obj, ship_name)
                if 'error' in result:
                    st.error(result['error'])
                else:
                    st.session_state['query_result'] = result

        if 'query_result' in st.session_state:
            result = st.session_state['query_result']
            if 'error' not in result:
                st.subheader(result['name'])

                tab1, tab2, tab3, tab4, tab5 = st.tabs(['基础信息', '普攻', '技能', '天赋', 'Buff 数据'])

                with tab1:
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.markdown('**基础属性**')
                        st.write(f"舰种: {result['panel']['舰种']}")
                        st.write(f"所属: {result['panel']['所属']}")
                        st.write(f"稀有: {result['panel']['稀有']}")
                        st.write(f"装甲类型: {result['panel']['装甲类型']}")
                        st.write(f"弹种: {result['panel']['弹种']}")
                    with col_b:
                        st.markdown('**面板数值**')
                        panel_df = pd.DataFrame([result['panel']['面板']])
                        st.dataframe(panel_df.T, use_container_width=True)

                with tab2:
                    if result['normal_attacks']:
                        # 拍平成中文列：normal_attacks 为段级列表（每条自带 倍率/间隔/命中/弹种），
                        # 每行一段（普攻结构里的英文键不直接暴露）
                        na_rows = []
                        for i, p in enumerate(result['normal_attacks'], 1):
                            na_rows.append({
                                '段': i,
                                '倍率': p.get('multiplier', ''),
                                '命中数': p.get('hit', 1),
                                '间隔(秒)': p.get('cd', ''),
                                '伤害类型': p.get('preset_damage_type', ''),
                                '武器': p.get('weapon_type', ''),
                                '弹种': p.get('ammo_type', ''),
                            })
                        st.dataframe(pd.DataFrame(na_rows), use_container_width=True, hide_index=True)
                    else:
                        st.info('无普攻数据')

                with tab3:
                    field_map = [
                        ('LeaderSkill', '旗舰技'), ('SupportSkill', '支援技'),
                        ('BattleSkill1', '通常技'), ('BattleSkill2', '必杀技'),
                    ]
                    for field, label in field_map:
                        txt = result['skills'].get(field, '')
                        if txt:
                            st.markdown(f"**{label}**:")
                            st.text(txt)
                        else:
                            st.markdown(f"**{label}**: (空)")

                with tab4:
                    talents = result.get('talents')
                    if talents:
                        for row in ['1', '2', '3', '4']:
                            rd = talents.get(row)
                            if not rd:
                                continue
                            cells = []
                            for opt in ('A', 'B'):
                                od = rd.get(opt) or {}
                                # 兼容 {name, effect} 新格式与纯字符串旧格式
                                if isinstance(od, dict):
                                    tname = (od.get('name') or '').strip()
                                    effect = (od.get('effect') or '').strip()
                                else:
                                    tname = (od or '').strip()
                                    effect = ''
                                cells.append(f"{tname}（{effect}）" if effect else (tname or '-'))
                            st.write(f"第{row}行: {cells[0]} ｜ {cells[1]}")
                    else:
                        st.info('无天赋数据（stage 1 合表会从天赋表自动补全；也可在 build/角色数据构建.py 控制台「手动更新」里录入）')

                with tab5:
                    bs = result.get('buffs_summary') or {}
                    if not bs:
                        st.info('data.json 未收录该角色（无计算表数据）')
                    else:
                        buffs = bs.get('buffs') or {}
                        if buffs:
                            st.markdown('**常驻 buff**')
                            st.dataframe(pd.DataFrame([buffs]).T, use_container_width=True)
                        sb = bs.get('skill_buffs') or []
                        if sb:
                            st.markdown('**技能持续 buff**')
                            # 英文键映射为中文列（触发值也译作中文）
                            _trig_cn = {'skill': '通常技', 'burst': '必杀技', 'support': '支援技'}
                            _sb_cols = [('trigger', '触发'), ('duration', '持续(秒)'),
                                        ('count', '次数'), ('before_damage', '当次生效'),
                                        ('target', '目标'), ('scope', '范围'), ('buffs', '加成')]
                            sb_rows = []
                            for w in sb:
                                if not isinstance(w, dict):
                                    continue
                                row = {}
                                for key, label in _sb_cols:
                                    v = w.get(key)
                                    if key == 'trigger' and v in _trig_cn:
                                        v = _trig_cn[v]
                                    elif key == 'buffs' and isinstance(v, dict):
                                        v = '、'.join(f'{k}{bv}' for k, bv in v.items())
                                    elif isinstance(v, list):
                                        v = '、'.join(str(x) for x in v)
                                    if v is not None:
                                        row[label] = v
                                sb_rows.append(row)
                            st.dataframe(pd.DataFrame(sb_rows), use_container_width=True, hide_index=True)
                        fb = bs.get('flagship_buffs') or []
                        if fb:
                            st.markdown('**旗舰光环（flagship_buffs：目标/范围/条件 → 加成）**')
                            if isinstance(fb, dict):
                                fb = [{'target': 'team', 'buffs': fb}]
                            rows = []
                            for a in fb:
                                if not isinstance(a, dict):
                                    continue
                                desc = []
                                for f in (a.get('scope') or []):
                                    op = f.get('op') or ''
                                    desc.append(f"{f.get('kind')}{op}{f.get('value')}")
                                c = a.get('condition')
                                if c:
                                    if c.get('kind') == 'exists':
                                        ofs = '、'.join(
                                            f"{x.get('kind')}{x.get('op') or ''}{x.get('value')}"
                                            for x in (c.get('of') or []))
                                        desc.append(f"条件:队伍中≥{c.get('count')}名[{ofs}]")
                                    else:
                                        desc.append(f"条件:{c.get('kind')}={c.get('value') or c.get('op')}")
                                sc = a.get('scaling')
                                if sc:
                                    ofs = '、'.join(str(x.get('value')) for x in (sc.get('of') or []))
                                    desc.append(
                                        f"缩放:每多1名[{ofs}]{sc.get('step')}"
                                        + (f"封顶{sc.get('cap')}" if sc.get('cap') else '')
                                        + (f"(基准{sc.get('base')})" if sc.get('base') else ''))
                                rows.append({
                                    '目标': a.get('target'),
                                    '范围/条件': '；'.join(desc) or '全队',
                                    '加成': '、'.join(f"{k}{v}" for k, v in (a.get('buffs') or {}).items()),
                                })
                            st.dataframe(pd.DataFrame(rows), use_container_width=True)
                        de = bs.get('damage_extra') or {}
                        if de:
                            st.markdown('**额外伤害（damage_extra）**')
                            st.dataframe(pd.DataFrame([de]).T, use_container_width=True)
                        ds = result.get('data_summary') or {}
                        if ds:
                            st.markdown('**技能数值概况（data.json）**')
                            for label, info in ds.items():
                                segs = '，'.join(
                                    f"{s['damage_type']}x{s['hit']}" for s in info.get('segments', [])
                                )
                                st.write(f"{label}: 循环CD(秒)={info.get('cd', 0)}｜{segs}")
                        if not (buffs or sb or fb or de or ds):
                            st.info('该角色计算表为空')


# ======================================================================
# 手动更新
# ======================================================================

elif page == '手动更新':
    st.header('手动更新')

    if raw_store is None:
        st.error('存储未初始化')
    else:
        ship_name = st.text_input('舰名', placeholder='要更新的舰名')
        if not ship_name:
            st.info('请输入舰名')
        else:
            result = data_api.query_ship(raw_store, data_store_obj, ship_name)
            if 'error' in result:
                # 角色不存在，询问是否新建
                col1, col2 = st.columns([1, 3])
                with col1:
                    if st.button('新建角色', type='primary'):
                        with st.spinner('正在创建角色...'):
                            create_result = data_api.create_ship(raw_store, data_store_obj, ship_name)
                        if 'error' in create_result:
                            st.error(create_result['error'])
                        else:
                            st.success(f'已创建角色: {create_result["name"]}')
                            st.rerun()
                with col2:
                    st.warning(f'未找到角色: {ship_name}，点击左侧按钮新建该角色')
            else:
                st.success(f'已定位: {result["name"]}')

                tab_skill, tab_panel, tab_talent, tab_reparse = st.tabs(
                    ['技能描述', '基础信息', '天赋', '重新解析']
                )

                with tab_skill:
                    st.markdown('**技能描述编辑**')
                    field_map = [
                        ('BattleSkill1', '通常技'), ('BattleSkill2', '必杀技'),
                        ('SupportSkill', '支援技'), ('LeaderSkill', '旗舰技'),
                    ]
                    for field, label in field_map:
                        current = result['skills'].get(field, '')
                        new_val = st.text_area(
                            f'{label} ({field})',
                            value=current,
                            key=f'skill_{field}',
                            height=80,
                        )
                        if st.button(f'保存 {label}', key=f'save_skill_{field}'):
                            with st.spinner('正在保存并解析...'):
                                r = data_api.update_skill(raw_store, data_store_obj, ship_name, field, new_val)
                            if 'error' in r:
                                st.error(r['error'])
                            elif r.get('warning'):
                                st.warning(r['warning'])
                            else:
                                if r.get('config_synced'):
                                    st.toast('伤害计算器配置已同步')
                                st.success(f'{label} 已保存，计算表已更新')
                                st.rerun()

                with tab_panel:
                    st.markdown('**基础信息编辑**')
                    panel = result['panel']
                    sub = panel.get('面板', {})

                    col1, col2 = st.columns(2)
                    with col1:
                        base_fields = ['舰种', '所属', '稀有', '装填', '装甲类型', '弹种']
                        for f in base_fields:
                            current = panel.get(f, '')
                            new_val = st.text_input(f, value=str(current), key=f'base_{f}')
                            if new_val != str(current):
                                if st.button(f'保存 {f}', key=f'save_base_{f}'):
                                    r = data_api.update_panel_field(raw_store, data_store_obj, ship_name, f, new_val)
                                    if 'error' in r:
                                        st.error(r['error'])
                                    else:
                                        if r.get('config_synced'):
                                            st.toast('伤害计算器配置已同步')
                                        st.success(f'{f} 已保存')
                                        st.rerun()

                    with col2:
                        stat_fields = ['血量', '炮击', '雷击', '航空', '对空', '装甲', '命中', '机动', '索敌', '幸运']
                        for f in stat_fields:
                            current = sub.get(f, 0)
                            new_val = st.number_input(f'面板-{f}', value=float(current), key=f'stat_{f}')
                            if new_val != float(current):
                                if st.button(f'保存 面板-{f}', key=f'save_stat_{f}'):
                                    r = data_api.update_panel_stat(raw_store, data_store_obj, ship_name, f, new_val)
                                    if 'error' in r:
                                        st.error(r['error'])
                                    else:
                                        if r.get('config_synced'):
                                            st.toast('伤害计算器配置已同步')
                                        st.success(f'面板-{f} 已保存')
                                        st.rerun()

                with tab_talent:
                    st.markdown('**天赋编辑**（四行二列）')
                    talents = result.get('talents') or {'1': {}, '2': {}, '3': {}, '4': {}}
                    labels = [f'{r}{c}' for r in ('1', '2', '3', '4') for c in ('A', 'B')]

                    cols = st.columns(4)
                    for i, lab in enumerate(labels):
                        col_idx = i // 2
                        row_col = i % 2
                        cell = talents.get(lab[0], {}).get(lab[1], {})
                        cell_name = cell.get('name', '')
                        cell_eff = cell.get('effect', '')

                        with cols[col_idx]:
                            st.markdown(f"**{lab}**")
                            t_name = st.text_input('名称', value=cell_name, key=f'talent_name_{lab}')
                            t_eff = st.text_input('效果', value=cell_eff, key=f'talent_eff_{lab}')
                            if st.button(f'保存 {lab}', key=f'save_talent_{lab}'):
                                if t_name:
                                    r = data_api.update_talent(
                                        raw_store, ship_name, lab[0], lab[1],
                                        {'name': t_name, 'effect': t_eff}
                                    )
                                    if 'error' in r:
                                        st.error(r['error'])
                                    else:
                                        st.success(f'{lab} 已保存')
                                        st.rerun()

                with tab_reparse:
                    st.markdown('**重新解析**')
                    st.info('强制使用 LLM 重新解析该角色的技能描述，生成计算表')
                    if st.button('强制重新解析', type='primary'):
                        with st.spinner('正在调用 LLM 解析...'):
                            r = data_api.reparse_ship(raw_store, data_store_obj, ship_name)
                        if 'error' in r:
                            st.error(r['error'])
                        else:
                            if r.get('config_synced'):
                                st.toast('伤害计算器配置已同步')
                            st.success('解析完成')
                            st.rerun()


# ======================================================================
# 表格导入（三表：技能表 / 面板表 / 数值自动化表·综合表）
# ======================================================================

elif page == '表格导入':
    st.header('表格导入（三表）')

    def _show_import_result(r):
        """统一展示一次 import_full 的结果；返回 True 表示出错。"""
        if 'error' in r:
            st.error(r['error'])
            return True
        m, l = r['merge'], r['llm']
        st.success('导入完成')
        lines = [
            f"- 合并：更新 {m.get('updated', 0)} 个，清理 {m.get('removed', 0)} 个，共 {m.get('total', 0)} 个",
            f"- 面板同步：{m.get('panel_synced', 0)} 个（按 panel_hash，不经 LLM）",
            "- talent_map.json / talent_pvp.json 已重新生成",
        ]
        if m.get('skipped'):
            _sk = m['skipped']
            lines.append('- 未确认保持原值（跳过）: '
                         + '、'.join(f"{k} {v}" for k, v in _sk.items() if v) or '无')
        if l.get('status') == '无需解析':
            lines.append(f"- 技能解析：无变化，跳过（清理失效 {l.get('removed', 0)} 个）")
        else:
            lines.append(f"- 技能解析：{l.get('parsed', 0)}/{l.get('pending', 0)} 个（增量 LLM）")
            if l.get('removed'):
                lines.append(f"- 清理 data.json 失效角色 {l['removed']} 个")
        ns = l.get('normal_synced', 0)
        if ns:
            lines.append(f'- 普攻轻同步：{ns} 个（未调用 LLM）')
        if m.get('config_synced') or l.get('config_synced'):
            lines.append('- custom_ship_config.json 已同步')
        st.markdown('\n'.join(lines))
        if l.get('error_msg'):
            st.warning(l['error_msg'])
        return False

    st.markdown("""
    **自动流程（差异化和增量更新，一次点击完成）：**
    1. 扫描所选表 → 差异化合并到 raw_data.json，并重新生成 talent_map.json / talent_pvp.json
    2. 面板变化 → 按 panel_hash 直接刷新 data.json 对应角色（不经 LLM）
    3. 技能缺失或描述变化 → 增量 LLM 重解析（skill_buffs/旗舰光环/点燃/持续扣除一并重建）
    4. 仅普攻变化 → 轻量同步计算表 normal_attacks（跳过 LLM）
    5. 全程同步 custom_ship_config.json 的角色引用与技能缓存

    **三张数据表**（未勾选的源保持原值不动）：
    - **技能表**：角色技能描述（CharSkillInfoLv10）→ 差异角色增量 LLM 重解析
    - **面板表**：舰灵数值（整理版）→ 面板字段按 panel_hash 直刷
    - **数值自动化表（综合表）**：普攻列表 + 天赋 + 装备等 → 普攻轻同步 + 天赋/配置同步
    """)

    # 三表卡片：勾选参与导入；未勾选源保持原值
    slots_info = data_api.list_source_slots()
    slot_desc = {
        'skill': '角色技能描述 → 差异角色增量 LLM 重解析',
        'panel': '舰灵数值 → 面板按 panel_hash 直刷 data.json',
        'combo': '普攻列表 + 天赋 → 普攻轻同步 + 天赋/配置同步',
    }
    cols = st.columns(3)
    sel = {}
    for col, slot in zip(cols, slots_info):
        with col:
            st.markdown(f"##### {slot['label']}")
            st.caption(slot_desc.get(slot['slot'], ''))
            if slot['files']:
                names = '\n'.join(f"· {f['name']}" for f in slot['files'])
                st.caption(f"✅ 识别到源文件：\n{names}")
            else:
                st.warning('知识库中未找到该表源文件')
            sel[slot['slot']] = st.checkbox(
                f"导入{slot['label']}",
                value=True,
                disabled=not slot['files'],
                key=f'imp_slot_{slot["slot"]}',
            )

    with st.expander('高级选项'):
        force = st.checkbox('强制全量重解析（忽略差异化，所有角色重新走 LLM）', value=False)
        st.caption('指定文件识别更新：勾选需要导入的表，未勾选的源保持原值；一键点击下方按钮执行。')

    selected_slots = [s['slot'] for s in slots_info if sel.get(s['slot']) and s['has_source']]
    total_sel = len(selected_slots)

    # 流程：先对照（preview）→ 展示差异 → 用户批量确认后才写库（非手动模式）；
    # 手动更新页（update_*）仍直接落库，不受本流程影响。
    if st.button(f'① 扫描对照（{total_sel}/3 表）', type='primary',
                 disabled=total_sel == 0):
        with st.spinner('对照预览：读取表格并比对底层 json ...'):
            st.session_state.pop('imp_preview', None)
            st.session_state['imp_preview'] = {'slots': selected_slots, 'force': force,
                                               'rows': None, 'error': None}
            r = data_api.preview_import(slots=selected_slots)
            if 'error' in r:
                st.session_state['imp_preview']['error'] = r['error']
            else:
                st.session_state['imp_preview']['rows'] = r['rows']
        st.rerun()

    _pv = st.session_state.get('imp_preview')
    if _pv is not None and _pv.get('error'):
        st.error(_pv['error'])
    elif _pv is not None and _pv.get('rows') is not None:
        rows = _pv['rows']
        if not rows:
            st.success('✅ 对照结果：与底层 json 一致，无差异。可直接应用（等价直接更新）。')
            if st.button('② 应用（无差异，直接执行）', type='primary'):
                with st.spinner('合并表格 + 差异化增量更新...'):
                    r = data_api.import_full(force=_pv['force'], slots=_pv['slots'])
                st.session_state.pop('imp_preview', None)
                _show_import_result(r)
                st.rerun()
        else:
            st.markdown(f"**② 差异对照（{len(rows)} 处）**：与底层 json 不同，请确认后批量更新。")
            keys = [f"{r['ship']}·{r['label']}({'新增' if r['kind'] == 'added' else '变更'})"
                    for r in rows]
            st.caption('默认全部应用；可在下方排除个别行后确认。差异明细（旧值 → 新值）随行展示。')
            import pandas as pd
            _df = pd.DataFrame([{
                '舰名': r['ship'], '源': r['label'],
                '类型': '新增' if r['kind'] == 'added' else '变更',
                '变更项数': r['changes'],
                '差异摘要': '；'.join(r['detail'][:2]),
            } for r in rows])
            st.dataframe(_df, use_container_width=True, hide_index=True)
            with st.expander('差异明细（逐条旧→新对照，确认前请核对）'):
                for r in rows:
                    _kc = '新增' if r['kind'] == 'added' else '变更'
                    st.markdown(f"**{r['ship']} · {r['label']}（{_kc}，{r['changes']} 项）**")
                    for _d in r['detail']:
                        st.markdown(f"- {_d}")
            exclude = st.multiselect(
                '排除以下行（不更新，保持底层原值）',
                options=keys,
                key='imp_exclude',
                placeholder='选择要排除的差异行…',
            )
            excl_idx = set(keys.index(x) for x in exclude)
            confirm = {f"{rows[i]['source']}:{rows[i]['ship']}"
                       for i in range(len(rows)) if i not in excl_idx}
            _c1, _c2, _c3 = st.columns(3)
            with _c1:
                if st.button(f'✔ 确认更新（{len(confirm)}/{len(rows)} 处）',
                             type='primary', disabled=not confirm):
                    with st.spinner('按确认结果合并 + 差异化增量更新...'):
                        r = data_api.import_full(force=_pv['force'], slots=_pv['slots'],
                                                 confirm=confirm)
                    st.session_state.pop('imp_preview', None)
                    _show_import_result(r)
                    st.rerun()
            with _c2:
                if st.button('全部放弃（本次不更新）'):
                    st.session_state.pop('imp_preview', None)
                    st.rerun()
            with _c3:
                if st.button('⇄ 重新对照'):
                    st.session_state.pop('imp_preview', None)
                    st.rerun()


# ======================================================================
# 伤害计算器
# ======================================================================

elif page == '伤害计算器':
    st.header('自定义伤害计算器')

    config = business_logic.ConfigManager.load_config()
    calculator = business_logic.SimpleDamageCalculator()
    stat_fields = ['血量', '炮击', '雷击', '航空', '对空', '装甲', '命中', '机动', '索敌', '幸运']

    # 记忆基准：本轮渲染开始时已持久化的选择（角色 + 敌方 + 时间，检测到变化时自动保存）
    def _sel_snapshot(cfg):
        """提取需要记忆的选择信息。"""
        return {
            'mode': cfg.get('attacker_mode'),
            'ship': cfg.get('attacker_ship_name'),
            'enemy_mode': cfg.get('enemy_mode'),
            'enemy_ship': cfg.get('enemy_ship_name'),
            'time': cfg.get('calc_time'),
            'attacker_buffs': cfg.get('attacker_buffs'),
            'defender_buffs': cfg.get('defender_buffs'),
            'formation': cfg.get('formation'),
        }

    _persisted_sel = _sel_snapshot(config)
    _needs_save = False

    def _sync_skills_from_ship(ship_name):
        """从已有角色同步技能到技能配置（角色选择变化时自动触发，也可手动重同步）。"""
        raw_obj = raw_store.get(ship_name)
        if not raw_obj:
            return
        skills = data_api.build_ship_skill_versions(raw_obj)
        if skills:
            config['skill_versions'] = skills
            config['skill_source'] = f"ship:{ship_name}"

    tab_main, tab_compare = st.tabs(
        ['角色配置与伤害', 'Buff对比']
    )

    with tab_main:
        existing_ships = sorted(raw_store.storage.keys()) if raw_store else []
        ship = config['custom_ship']

        # 第一步：选择角色
        st.markdown('**选择角色**')
        config['attacker_mode'] = st.radio(
            '角色来源', ['已有角色', '自定义'],
            index=0 if config.get('attacker_mode') == 'ship' else 1,
            horizontal=True, key='attacker_mode'
        )
        attacker_is_ship = config['attacker_mode'] == '已有角色'

        if attacker_is_ship:
            config['attacker_mode'] = 'ship'
            config['attacker_ship_name'] = st.selectbox(
                '选择角色', [''] + existing_ships,
                index=([''] + existing_ships).index(config.get('attacker_ship_name', ''))
                if config.get('attacker_ship_name', '') in existing_ships else 0,
                key='attacker_ship_select'
            )
            if config['attacker_ship_name']:
                ship_data = raw_store.get(config['attacker_ship_name'])
                if ship_data:
                    # 每次从原始数据构建角色对象，不写入 custom_ship，避免覆盖自定义输入的记忆
                    panel_info = ship_data.get('panel', {})
                    inner_panel = panel_info.get('面板', {})
                    ship = {
                        'name': config['attacker_ship_name'],
                        'ship_type': panel_info.get('舰种', ''),
                        'nation': panel_info.get('所属', ''),
                        'ammo_type': panel_info.get('弹种', ''),
                        # 副武器段武器类型：技能描述标记「副炮」时按此解析克制/点燃（新泽西=驱逐炮）
                        'secondary_weapon': next((a.get('weapon_type', '') for a in
                                                  (ship_data.get('normal_attacks') or [])
                                                  if (a.get('slot') or '') == '副武器'), ''),
                        'secondary_ammo': next((a.get('ammo_type', '') for a in
                                                (ship_data.get('normal_attacks') or [])
                                                if (a.get('slot') or '') == '副武器'), ''),
                        'buffs': {},  # 已有角色不使用自定义 buffs
                        'panel': {f: float(inner_panel.get(f, 0) or 0) for f in stat_fields},
                    }
                    st.info(f"{ship['ship_type']} | {ship['nation']} | 弹种: {ship['ammo_type']}")
                # 技能自动同步：选择角色后直接可用（不再依赖手动点按钮），数据更新后可点下方重同步
                if config.get('skill_source') != f"ship:{config['attacker_ship_name']}":
                    _sync_skills_from_ship(config['attacker_ship_name'])
                    _needs_save = True
                # 天赋编码：4 位数字，第 i 位为第 i 行选择（1=A / 2=B，如 1212），留空不计天赋
                talent_code = st.text_input(
                    '天赋编码（如 1212，留空不计天赋）',
                    value=config.get('attacker_talent_code', ''),
                    key='attacker_talent_code_input',
                    help='4 位数字，第 i 位为第 i 行天赋选择：1=A 选项、2=B 选项；'
                         '解析后天赋的面板/增伤 buff 自动参与本页与对比页全部计算',
                )
                if talent_code != config.get('attacker_talent_code', ''):
                    config['attacker_talent_code'] = talent_code
                    _needs_save = True
        else:
            config['attacker_mode'] = 'custom'
            ship['name'] = st.text_input('舰名', value=ship.get('name', '自定义航母'), key='custom_ship_name')
            type_options = ['驱逐', '轻巡', '重巡', '战列', '战巡', '重炮',
                            '航母', '轻母', '装母', '航战', '雷巡']
            ship['ship_type'] = st.selectbox('舰种', type_options,
                index=type_options.index(ship.get('ship_type', '航母')) if ship.get('ship_type') in type_options else 0,
                key='custom_ship_type')
            nation_options = ['', '八咫', '尤奈特', '洛蒙瑞亚', '奥鲁加', '列加杜', '纳谢尔', '凤栖', '沃尔克', '其他']
            ship['nation'] = st.selectbox('国籍', nation_options,
                index=nation_options.index(ship.get('nation', '')) if ship.get('nation', '') in nation_options else 0,
                key='custom_ship_nation')
            ammo_options = ['', '穿甲', '高爆', '通常']
            ship['ammo_type'] = st.selectbox('弹种', ammo_options,
                index=ammo_options.index(ship.get('ammo_type', '')) if ship.get('ammo_type', '') in ammo_options else 0,
                key='custom_ship_ammo')

            st.markdown('面板属性')
            panel = ship['panel']
            stat_cols = st.columns(5)
            for i, f in enumerate(stat_fields):
                with stat_cols[i % 5]:
                    panel[f] = st.number_input(f, value=float(panel.get(f, 0)), key=f'ship_{f}')

        # Buffs 配置（仅自定义角色可配置）
        if not attacker_is_ship:
            with st.expander('Buffs 配置', expanded=False):
                buffs_json = st.text_area('Buffs (JSON)', value=json.dumps(ship.get('buffs', {}), ensure_ascii=False, indent=2), height=80, key='attacker_buffs')
                try:
                    ship['buffs'] = json.loads(buffs_json)
                except Exception:
                    st.warning('Buffs JSON 格式无效')

            # 技能配置：自定义角色会话内输入（仅存 st.session_state，不入底层，
            # 切换功能页/标签可记忆，关闭网页后清除）
            with st.expander('技能配置（自定义，仅本次会话有效）', expanded=False):
                for sk_label in ('通常技', '必杀技', '支援技'):
                    st.text_area(
                        f'{sk_label}描述', key=f'custom_skill_{sk_label}', height=70,
                        help='支持如「270%x5 炮击伤害」「【命中+45%，持续10秒】」等描述格式',
                    )
                    txt = (st.session_state.get(f'custom_skill_{sk_label}') or '').strip()
                    if txt:
                        segs, _sb = data_api._resolve_skill({'raw_text': txt})
                        if segs:
                            st.caption(f'{sk_label} 识别成功：{len(segs)} 段')
                        else:
                            st.caption(f'{sk_label} 未识别出有效技能段，请检查格式（如 270%x5 炮击伤害）')

            if st.button('保存配置'):
                business_logic.ConfigManager.save_config(config)
                st.success('配置已保存')

        # 技能配置来源统一：已有角色实时从底层原文构建（config 里的缓存可能按旧规则
        # 同步、缺纯 buff 技能如空想必杀，实时构建保证"修改技能描述"处可选全部技能）；
        # 自定义角色用会话输入（不入库）
        if attacker_is_ship:
            _ship_name = str(config.get('attacker_ship_name') or '')
            _raw_obj = raw_store.get(_ship_name) if (_ship_name and raw_store) else None
            if _raw_obj:
                skill_versions = data_api.build_ship_skill_versions(_raw_obj)
            else:
                skill_versions = config.get('skill_versions', {})
        else:
            skill_versions = {}
            for sk_label in ('通常技', '必杀技', '支援技'):
                txt = (st.session_state.get(f'custom_skill_{sk_label}') or '').strip()
                if txt:
                    skill_versions[sk_label] = {'name': sk_label, 'raw_text': txt, 'description': txt}

        # 攻击方额外 Buff（已有角色/自定义通用）：自然语言条目，与对比页/用户端同格式。
        # 参与配置页全部计算，并自动带入 Buff 对比页基准/修改后两侧（受控变量）。
        with st.expander('攻击方额外 Buff（参与本页与对比页全部计算）', expanded=bool((config.get('attacker_buffs') or '').strip())):
            ab_txt = st.text_area(
                '额外 Buff', value=config.get('attacker_buffs', ''),
                height=80, key='attacker_extra_buffs',
                help='示例：全伤害+15%、炮击伤害+20%、高平炮-50%、暴击率+0.1、对驱逐伤害增加+30%、炮击=800（面板赋值）；'
                     '不支持「持续N秒」')
            config['attacker_buffs'] = ab_txt
            if ab_txt.strip():
                _ab_b, _ab_p, _ab_e = data_api.parse_buff_input(ab_txt)
                if _ab_e:
                    st.warning(_ab_e)
                elif _ab_b or _ab_p:
                    _ab_desc = '、'.join(f'{k}{v:g}' for k, v in _ab_b.items())
                    if _ab_p:
                        _ab_desc += '；面板赋值 ' + '、'.join(f'{k}={v:g}' for k, v in _ab_p.items())
                    st.caption('将生效：' + _ab_desc)

        # 第二步：选择敌方
        st.divider()
        st.markdown('**选择敌方**')
        enemy_mode = st.radio(
            '敌方来源', ['不选（原始伤害）', '已有角色', '自定义靶子'],
            index={'none': 0, 'ship': 1, 'custom': 2}.get(config.get('enemy_mode', 'none'), 0),
            horizontal=True, key='enemy_mode'
        )
        existing_enemy_ships = sorted(data_store_obj.storage.keys()) if data_store_obj else []
        # 默认无削减虚拟敌人（与用户端"不填敌方=原始伤害"口径一致）
        enemy_dict = user_engine.CombatData.create_empty_enemy()
        if enemy_mode == '已有角色':
            config['enemy_mode'] = 'ship'
            config['enemy_ship_name'] = st.selectbox(
                '选择敌方', [''] + existing_enemy_ships,
                index=([''] + existing_enemy_ships).index(config.get('enemy_ship_name', ''))
                if config.get('enemy_ship_name', '') in existing_enemy_ships else 0,
                key='enemy_ship_select'
            )
            if config['enemy_ship_name'] and data_store_obj:
                enemy_cd = data_store_obj.get(config['enemy_ship_name'])
                if enemy_cd is not None:
                    # 敌方天赋编码：防守方口径参与计算（4 位数字，1=A / 2=B，留空不计）
                    enemy_talent_code = st.text_input(
                        '敌方天赋编码（如 1212，留空不计天赋）',
                        value=config.get('defender_talent_code', ''),
                        key='defender_talent_code_input',
                        help='第 i 位为该角色第 i 行天赋选择：1=A 选项、2=B 选项；'
                             '防守方口径：防御面板增量与被攻击减免类生效，攻击增伤类不计',
                    )
                    if enemy_talent_code != config.get('defender_talent_code', ''):
                        config['defender_talent_code'] = enemy_talent_code
                        _needs_save = True
                    # src 版 CombatData 缺 to_enemy_dict，转成引擎同源对象后输出敌方字典；
                    # 敌方天赋叠加到该临时对象（from_dict 产生新对象，不污染存储）
                    enemy_full = user_engine.CombatData.from_dict(enemy_cd.to_dict())
                    _etcode = (config.get('defender_talent_code') or '').strip()
                    if _etcode:
                        _eteff = _resolve_ship_talent(config['enemy_ship_name'], _etcode, raw_store)
                        if _eteff.get('ok'):
                            _apply_defender_talent_effects(enemy_full, _eteff)
                            _et_parts = ['天赋 ' + '、'.join(_eteff.get('names') or [])]
                            _et_def_panel = {k: v for k, v in (_eteff.get('panel') or {}).items()
                                             if k in ('装甲', '对空', '机动', '幸运', '索敌', '血量',
                                                      '防暴击率', '回避率')}
                            if _et_def_panel:
                                _et_parts.append('面板 ' + '、'.join(f'{k}+{v:g}' for k, v in _et_def_panel.items()))
                            if _eteff.get('enemy'):
                                _et_parts.append('减免 ' + '、'.join(f'{k}+{v:g}' for k, v in _eteff['enemy'].items()))
                            if _eteff.get('buffs') or _eteff.get('ship_bonus'):
                                _et_parts.append('攻击增伤类（作为攻击方时生效，防守侧不计）')
                            st.info('敌方天赋编码 %s：%s' % (_etcode, '；'.join(_et_parts)))
                        else:
                            st.warning('敌方天赋解析失败：%s' % _eteff.get('msg', ''))
                    enemy_dict = enemy_full.to_enemy_dict()
                    st.info(f"敌方: {config['enemy_ship_name']} | 装甲类型: {enemy_dict.get('装甲类型', '')}")
                else:
                    st.warning('该角色缺少计算表数据，无法作为敌方')
        elif enemy_mode == '自定义靶子':
            config['enemy_mode'] = 'custom'
            cfg_enemy = config['enemy']
            armor_options = ['轻甲', '中甲', '重甲']
            e_panel = cfg_enemy.setdefault('panel', {})
            e_buffs = cfg_enemy.setdefault('buffs', {})
            e_row1 = st.columns(4)
            with e_row1[0]:
                cfg_enemy['armor_type'] = st.selectbox('装甲类型', armor_options,
                    index=armor_options.index(cfg_enemy.get('armor_type', '轻甲')) if cfg_enemy.get('armor_type', '轻甲') in armor_options else 0,
                    key='enemy_armor_type')
            for i, f in enumerate(['装甲', '对空', '机动']):
                with e_row1[i + 1]:
                    e_panel[f] = st.number_input(f, value=float(e_panel.get(f, 0) or 0), key=f'enemy_{f}')
            e_row2 = st.columns(4)
            for i, f in enumerate(['幸运', '索敌', '血量']):
                with e_row2[i]:
                    e_panel[f] = st.number_input(f, value=float(e_panel.get(f, 0) or 0), key=f'enemy_{f}')
            with e_row2[3]:
                pass
            e_row3 = st.columns(2)
            with e_row3[0]:
                e_buffs['全伤害减免'] = st.number_input('全伤害减免 (%)', min_value=0.0, max_value=100.0,
                    value=float(e_buffs.get('全伤害减免', 0) or 0) * 100, key='enemy_dmg_red') / 100
            with e_row3[1]:
                e_buffs['点燃抵抗'] = st.number_input('点燃抵抗 (%)', min_value=0.0, max_value=100.0,
                    value=float(e_buffs.get('点燃抵抗', 0) or 0) * 100, key='enemy_ignite_res') / 100

            # 构建敌方字典（口径与用户端 to_enemy_dict 一致）
            enemy_dict = user_engine.CombatData.create_empty_enemy(name='自定义靶子')
            enemy_dict['装甲类型'] = cfg_enemy.get('armor_type', '轻甲')
            for f in ['装甲', '对空', '机动', '幸运', '索敌', '血量']:
                enemy_dict[f] = float(e_panel.get(f, 0) or 0)
            enemy_dict['全伤害减免'] = float(e_buffs.get('全伤害减免', 0) or 0)
            enemy_dict['点燃抵抗'] = float(e_buffs.get('点燃抵抗', 0) or 0)

            # 自定义靶子输入变化时自动保存（记忆功能）
            enemy_snap = json.dumps(cfg_enemy, ensure_ascii=False, sort_keys=True)
            if st.session_state.get('enemy_cfg_snap') != enemy_snap:
                st.session_state['enemy_cfg_snap'] = enemy_snap
                _needs_save = True
        else:
            config['enemy_mode'] = 'none'

        # 防守方状态：
        # raw_only：敌方"不选"=原始伤害口径，只出原始伤害派生的暴击/期望，不出实际伤害
        # enemy_ready=False：防守方未确定，底部不渲染任何伤害
        #   - 已有角色：必须选中具体角色且该角色存在计算表
        #   - 自定义靶子：防御属性（装甲/对空/机动/幸运/索敌/血量）不能全为空
        raw_only = enemy_mode == '不选（原始伤害）'
        enemy_ready = True
        if enemy_mode == '已有角色':
            enemy_ready = (
                bool(config.get('enemy_ship_name'))
                and data_store_obj is not None
                and data_store_obj.get(config.get('enemy_ship_name')) is not None
            )
        elif enemy_mode == '自定义靶子':
            e_panel_now = (config.get('enemy') or {}).get('panel') or {}
            enemy_ready = any(
                abs(float(e_panel_now.get(f, 0) or 0)) > 0
                for f in ('装甲', '对空', '机动', '幸运', '索敌', '血量')
            )

        # 敌方额外 Buff（已有角色/自定义靶子通用）：减免类/回避/防暴击，并入 enemy_dict——
        # 本页全部计算与对比页共用同一敌方视图；攻击增伤类对防守无意义，填了会提示忽略
        # 对比页独立基准（不含配置页敌方额外 Buff）：并入前快照（角色面板/天赋保留）
        enemy_base_dict = dict(enemy_dict) if (enemy_mode != '不选（原始伤害）' and enemy_ready) else None
        if enemy_mode != '不选（原始伤害）' and enemy_ready:
            with st.expander('敌方额外 Buff（减免类，参与本页与对比页全部计算）',
                             expanded=bool((config.get('defender_buffs') or '').strip())):
                db_txt = st.text_area(
                    '敌方 Buff', value=config.get('defender_buffs', ''),
                    height=70, key='defender_extra_buffs',
                    help='防守方口径字段：全伤害减免、炮击/雷击/航空/轰炸/鱼雷/近战/高平炮伤害减免、'
                         '对小型/中型/大型伤害减免、点燃抵抗、回避率、防暴击率；'
                         '面板赋值支持 装甲=3000 等（防御属性绝对覆盖）')
                config['defender_buffs'] = db_txt
                _DEF_ENEMY_KEYS = {
                    '全伤害减免', '炮击伤害减免', '雷击伤害减免', '航空伤害减免', '轰炸伤害减免',
                    '鱼雷伤害减免', '近战伤害减免', '高平炮伤害减免',
                    '小型攻击伤害减免', '中型攻击伤害减免', '大型攻击伤害减免',
                    '点燃抵抗', '回避率', '防暴击率',
                }
                _DEF_PANEL_KEYS = {'装甲', '对空', '机动', '幸运', '索敌', '血量'}
                _db_txt_strip = (db_txt or '').strip()
                if _db_txt_strip:
                    _db_buffs, _db_panel, _db_err = data_api.parse_buff_input(_db_txt_strip)
                    if _db_err:
                        st.warning(f'敌方额外 Buff 解析失败：{_db_err}')
                    else:
                        _ignored = []
                        for dk, dv in _db_buffs.items():
                            if dk in _DEF_ENEMY_KEYS:
                                enemy_dict[dk] = float(enemy_dict.get(dk, 0) or 0) + float(dv)
                            else:
                                _ignored.append(dk)
                        for dk, dv in _db_panel.items():
                            if dk in _DEF_PANEL_KEYS or dk in ('回避率', '防暴击率'):
                                enemy_dict[dk] = float(dv)
                            else:
                                _ignored.append(f'{dk}=（面板赋值仅支持防御属性）')
                        _db_parts = ['、'.join(f'{k}{v:g}' for k, v in _db_buffs.items() if k in _DEF_ENEMY_KEYS)]
                        _db_p_valid = {k: v for k, v in _db_panel.items()
                                       if k in _DEF_PANEL_KEYS or k in ('回避率', '防暴击率')}
                        if _db_p_valid:
                            _db_parts.append('面板赋值 ' + '、'.join(f'{k}={v:g}' for k, v in _db_p_valid.items()))
                        if any(p for p in _db_parts):
                            st.caption('将生效：' + '；'.join(p for p in _db_parts if p))
                        if _ignored:
                            st.caption(f'已忽略（非防守方口径）：{"、".join(sorted(set(_ignored)))}')

        st.divider()

        # 战斗参数
        col_p1, col_p2, col_p3, col_p4 = st.columns(4)
        with col_p1:
            course = st.selectbox('航向', ['同航', 'T优势', 'T劣势', '反航'], index=0, key='calc_course')
        with col_p2:
            air_status = st.selectbox('制空状态', ['确保', '优势', '均势', '劣势', '丧失'], index=2, key='calc_air')
        with col_p3:
            formation = st.selectbox('阵型', ['机动', '单纵', '复纵', '轮型', '梯型'],
                                     index=0, key='calc_formation',
                                     help='单纵：炮击/雷击伤害+12%；复纵：机动+25%；'
                                          '轮型：航空/对空+15%，炮击/雷击-5%；梯型：暴击率+15%，暴击伤害+5%')
        config['formation'] = formation
        with col_p4:
            if config.get('attacker_mode') == 'custom':
                ammo_type = st.selectbox('弹药类型', ['通常', '穿甲', '高爆'], index=0, key='calc_ammo')
            else:
                ammo_type = ship.get('ammo_type', '通常') or '通常'
                st.info(f"弹药类型: {ammo_type}")

        # 自动保存：选择与上次持久化不一致时落盘（保留上次选择/输入的记忆）
        if _needs_save or _sel_snapshot(config) != _persisted_sel:
            business_logic.ConfigManager.save_config(config)
            st.toast('已记住当前配置')
            _persisted_sel.update(_sel_snapshot(config))
            _needs_save = False

        # ==================== 计算引擎准备 ====================

        # 攻击方 CombatData（与用户端计算引擎同源）
        attacker_cd = None
        if attacker_is_ship:
            if config.get('attacker_ship_name') and data_store_obj:
                # 深拷贝后叠加天赋，避免污染计算表存储对象
                attacker_cd = copy.deepcopy(data_store_obj.get(config['attacker_ship_name']))
                if attacker_cd is None:
                    st.warning('该角色缺少计算表数据，无法计算')
            else:
                st.info('尚未选择角色')
        else:
            # 自定义角色：由输入面板 + 会话级技能描述构建（技能无 CD，仅支持单次）
            attacker_cd = _build_custom_combat_data(ship, skill_versions)

        # 天赋效果：按角色配置页编码解析并叠加到攻击方（面板增量/增伤 buff/舰种专属增伤）
        talent_eff = None
        if attacker_is_ship and attacker_cd is not None:
            _tcode = (config.get('attacker_talent_code') or '').strip()
            if _tcode:
                talent_eff = _resolve_ship_talent(config['attacker_ship_name'], _tcode, raw_store)
                if talent_eff.get('ok'):
                    _apply_talent_effects(attacker_cd, talent_eff)
                    _t_parts = ['天赋 ' + '、'.join(talent_eff.get('names') or [])]
                    if talent_eff.get('panel'):
                        _t_parts.append('面板 ' + '、'.join(f'{k}+{v:g}' for k, v in talent_eff['panel'].items()))
                    if talent_eff.get('buffs'):
                        _t_parts.append('buff ' + '、'.join(f'{k}+{v:g}' for k, v in talent_eff['buffs'].items()))
                    if talent_eff.get('enemy'):
                        _t_parts.append('减伤类（作为防守方时生效，攻击侧不计）')
                    st.info('已应用天赋编码 %s：%s' % (_tcode, '；'.join(_t_parts)))
                else:
                    st.warning('天赋解析失败：%s' % talent_eff.get('msg', ''))

        # 攻击方额外 Buff（配置页）：并入 attacker_cd——本页全部计算与对比页基准同源生效；
        # 面板赋值为绝对覆盖（口径与对比页「炮击=800」一致）
        attacker_extra_buffs, attacker_extra_panel = {}, {}
        _ab_txt = (config.get('attacker_buffs') or '').strip()
        # 对比页独立基准快照：不含配置页额外 Buff/面板覆盖（角色自带 buffs 与天赋保留；
        # 用户口径 2026-09-05：对比的技能修改与 Buff 区间不同步配置界面）
        attacker_cd_clean = copy.deepcopy(attacker_cd) if attacker_cd is not None else None
        if _ab_txt and attacker_cd is not None:
            attacker_extra_buffs, attacker_extra_panel, _ab_err = data_api.parse_buff_input(_ab_txt)
            if _ab_err:
                st.warning(f'攻击方额外 Buff 解析失败：{_ab_err}')
                attacker_extra_buffs, attacker_extra_panel = {}, {}
            else:
                for bk, bv in attacker_extra_buffs.items():
                    try:
                        std = user_engine._normalize_buff_key(bk)
                        attacker_cd.buffs[std] = float(attacker_cd.buffs.get(std, 0) or 0) + float(bv)
                    except (TypeError, ValueError):
                        continue
                for pk, pv in attacker_extra_panel.items():
                    if pk in attacker_cd.attack_panel:
                        attacker_cd.attack_panel[pk] = float(pv)
                    elif pk in attacker_cd.defense_panel:
                        attacker_cd.defense_panel[pk] = float(pv)
                if attacker_extra_buffs or attacker_extra_panel:
                    _ab_info = '、'.join(f'{k}{v:g}' for k, v in attacker_extra_buffs.items())
                    if attacker_extra_panel:
                        _ab_info += '；面板赋值 ' + '、'.join(f'{k}={v:g}' for k, v in attacker_extra_panel.items())
                    st.info('已应用攻击方额外 Buff：' + _ab_info)

        # 阵型 buff（与用户端口径一致：叠加到攻击方 buffs；机动=无修正）
        _fb = user_engine.FORMATION_BUFFS.get(formation or '', {})
        if _fb and attacker_cd is not None:
            for bk, bv in _fb.items():
                std = user_engine._normalize_buff_key(bk)
                attacker_cd.buffs[std] = float(attacker_cd.buffs.get(std, 0) or 0) + float(bv)
            st.info('已应用阵型 %s：%s' % (formation, '、'.join(f'{k}{v:g}' for k, v in _fb.items())))

        raw_skills = {}
        if attacker_is_ship and config.get('attacker_ship_name') and raw_store:
            raw_skills = (raw_store.get(config['attacker_ship_name']) or {}).get('skills', {}) or {}

        # ==================== 计算模式（项目选择，选中项标红） ====================

        st.markdown('**计算**')
        ccol1, ccol2 = st.columns([2, 5])
        with ccol1:
            calc_time = st.number_input('时间（秒）', min_value=0.0, step=1.0,
                value=float(config.get('calc_time', 0) or 0), key='calc_time_input')
        config['calc_time'] = float(calc_time)
        with ccol2:
            # 选中项标红：仅定位本 pills 部件（data-testid），不影响页面其它控件
            st.markdown(
                '<style>[data-testid="stPills"] [aria-pressed="true"],'
                '[data-testid="stPills"] [data-selected="true"]{'
                'background:#e03131!important;border-color:#e03131!important;color:#fff!important}'
                '</style>', unsafe_allow_html=True)
            calc_mode = st.pills('计算项目',
                ['普攻', '通常技', '必杀技', '支援技', '点燃', '总伤害'],
                selection_mode='single', default='普攻', key='calc_mode_choice',
                label_visibility='collapsed')

        # 点燃/总伤害需要战斗时长与角色数据，条件不满足时提示并回退默认项
        if calc_mode in ('点燃', '总伤害') and (calc_time <= 0 or attacker_cd is None):
            st.warning('点燃/总伤害需要战斗时长（秒）且已选择角色')
            calc_mode = '普攻'

        st.markdown(f"### {ship.get('name', '')} 伤害结果")

        # 防守方未确定：不渲染任何伤害结果
        if not enemy_ready:
            st.info('尚未确定防守方：敌方选"已有角色"需选中具体角色，选"自定义靶子"需填写防御属性')
        elif calc_mode == '总伤害':
            # 总伤害：时间轴模拟（实时 buff 窗口精准作用于普攻/技能）
            if not attacker_is_ship:
                st.warning('自定义角色技能无 CD 数据，仅支持单次计算')
            else:
                tl = user_engine.calc_total_damage_timeline(
                    copy.deepcopy(attacker_cd), enemy_dict, course, air_status,
                    calc_time, raw_skills=raw_skills)
                st.markdown(f"**总时间 {tl['total_time']} 秒 · 时间轴模拟（实时 buff）**")
                tl_rows = []
                for lb in ('普攻', '通常技', '必杀技'):
                    tl_rows.append({'项': lb, '总伤害': f"{tl['totals'][lb]:.0f}", '触发次数': tl['counts'][lb]})
                tl_rows.append({'项': '点燃', '总伤害': f"{tl['ignite']['actual']:.0f}", '触发次数': '—'})
                tl_rows.append({'项': '合计实际总伤害', '总伤害': f"{tl['grand']:.0f}", '触发次数': '—'})
                st.dataframe(pd.DataFrame(tl_rows), use_container_width=True, hide_index=True)
                active_windows = [w for w in tl['windows'] if w['count'] > 0 or w['trigger'] == 'user']
                if active_windows:
                    with st.expander('生效的 buff 窗口'):
                        trig_cn = {'skill': '通常技', 'burst': '必杀技', 'user': '用户'}
                        for w in active_windows:
                            desc = '、'.join(f"{k}{v:.0%}" if abs(v) < 1 else f"{k}{v:g}" for k, v in w['buffs'].items())
                            order_mark = '（先于伤害）' if w.get('before_damage') else '（后于伤害）'
                            if w['trigger'] == 'user':
                                st.markdown(f"- {desc} 持续{w['duration']:g}s（开局生效）")
                            else:
                                st.markdown(f"- {trig_cn.get(w['trigger'], w['trigger'])}(CD{w['cd']:g}s): {desc} 持续{w['duration']:g}s → 触发{w['count']}次{order_mark}")
                if tl['ignite']['details']:
                    with st.expander('点燃明细'):
                        for d in tl['ignite']['details']:
                            st.markdown(f"- {d.get('source', '')}: 触发{d.get('触发次数', 0)}次，实际总伤害 {d.get('实际总伤害', 0):.0f}")
                # 总伤害明细（时间轴逐次出手，两级折叠：类别 → 第N次；用户口径 2026-09-05）
                _tl_procs = tl.get('procs') or {}
                # DPS 曲线：每秒伤害（技能/普攻出手）+ 点燃均摊
                _ignite_ps = (tl['ignite']['actual'] / tl['total_time']) if tl['total_time'] else 0
                _df = pd.DataFrame({
                    '秒': [t + 1 for t in range(tl['total_time'])],
                    '每秒伤害': [round(v + _ignite_ps, 1) for v in tl.get('series') or []],
                }).set_index('秒')
                st.line_chart(_df, height=220)
                st.caption('DPS 曲线：每秒伤害（技能/普攻出手 + 点燃均摊），峰值为出手秒')
                if any(_tl_procs.values()):
                    with st.expander('伤害明细（按类别逐次出手，点开查看计算过程）'):
                        for lb in ('通常技', '必杀技', '支援技', '普攻'):
                            _items = _tl_procs.get(lb) or []
                            if not _items:
                                continue
                            with st.expander(f'{lb}（出手 {_items[-1]["seq"]} 次）'):
                                for it in _items:
                                    with st.expander(f'第{it["seq"]}次（t≈{it["t"]:g}s）'):
                                        for sub, proc_lines in it['blocks']:
                                            if sub:
                                                st.markdown(f'{sub}')
                                            st.text('\n'.join(proc_lines))

        elif calc_mode == '点燃':
            # 点燃伤害：普攻高爆点燃 + 技能点燃
            burn = user_engine.DamageCalculator.calculate_burn(
                copy.deepcopy(attacker_cd), enemy_dict, calc_time,
                user_engine._extract_skill_ignites(attacker_cd), course, air_status)
            st.markdown(f"**点燃伤害（{calc_time:g} 秒）**")
            bcols = st.columns(2)
            with bcols[0]:
                st.metric('点燃原始总伤害', f"{burn.get('total_raw_ignite', 0):.0f}")
            with bcols[1]:
                st.metric('点燃实际总伤害', f"{burn.get('total_actual_ignite', 0):.0f}")
            for d in burn.get('details', []):
                st.markdown(f"- {d.get('source', '')}: 触发{d.get('触发次数', 0)}次，实际总伤害 {d.get('实际总伤害', 0):.0f}")

        else:
            # 单次伤害：仅计算所选项目（普攻/通常技/必杀技/支援技），
            # 与用户端 calc_normal/skill/burst/support 同源
            if attacker_cd is None:
                st.info('尚未选择角色')
            else:
                label = calc_mode
                choice = {'普攻': 1, '通常技': 2, '必杀技': 3, '支援技': 4}[label]

                def _has_calc_data(cd, ch):
                    if cd is None:
                        return False
                    if ch == 1:
                        return bool(getattr(cd, 'normal_attacks', None))
                    field = {2: 'skill_attack', 3: 'burst_attack', 4: 'support_attack'}.get(ch)
                    if not field:
                        return False
                    return bool((getattr(cd, field, None) or {}).get('parts'))

                if not _has_calc_data(attacker_cd, choice):
                    st.info(f'{label}缺数据，无法计算（请先补齐该角色的普攻/技能数据）')
                else:
                    # 攻防方按次深拷贝：单次技能自带 buff 只作用于当次，不跨技能残留
                    atk_obj = copy.deepcopy(attacker_cd)
                    enemy_cur = copy.deepcopy(enemy_dict)
                    if choice > 1:
                        user_engine._apply_single_skill_buffs(atk_obj, enemy_cur, raw_skills, choice)
                    res = user_engine.DamageCalculator.compute_all_attacks(atk_obj, enemy_cur, course, air_status, choice)
                    if 'error' in res:
                        st.error(f"计算{label}出错: {res['error']}")
                    elif res:
                        with st.expander(label, expanded=True):
                            for key, val in res.items():
                                if key == 'error':
                                    continue
                                _render_single_result(label, val, raw_only, sub=key)
                    else:
                        st.warning(f'该角色没有{label}数据')


    with tab_compare:
        # 对比项跟随"角色配置与伤害"页的计算项目选择（pills）：一次只显示当前所选的伤害对比
        calc_mode = st.session_state.get('calc_mode_choice', '普攻')
        is_single_skill = calc_mode in ('通常技', '必杀技', '支援技')
        is_timeline = calc_mode in ('点燃', '总伤害')
        st.markdown(f'**修改技能描述与额外 Buff，对比伤害变化（当前对比项：{calc_mode}，跟随配置页选择）**')

        saved_skills = skill_versions
        # 编辑对象跟随计算项：单次技能项固定编辑该技能；点燃/总伤害可选要改哪条技能；
        # 普攻无技能描述，只吃额外 Buff
        if is_single_skill:
            skill_id = calc_mode
        elif is_timeline:
            if not saved_skills:
                st.warning('暂无技能配置：已有角色请先在"角色配置与伤害"页选择角色，'
                           '自定义角色请先在"技能配置"中输入技能描述')
                skill_id = None
            else:
                skill_id = st.selectbox('要修改的技能类型', list(saved_skills.keys()), key='cmp_skill_type')
        else:
            skill_id = None  # 普攻：无描述编辑

        ver = saved_skills.get(skill_id, {}) if skill_id else {}
        # 所有技能的描述修改**同时展示（折叠状态，点开编辑）**——方便同时调试多个技能；
        # 描述按「角色|技能类型」独立记忆，以本会话临时为准，不写回正式版本与底层数据；
        # 与配置界面不同步（用户口径 2026-09-05）
        _desc_map = st.session_state.setdefault('cmp_desc_map', {})
        for _sid, _ver in saved_skills.items():
            _ck = f"{config.get('attacker_mode')}|{config.get('attacker_ship_name')}|{config.get('custom_name') or ''}|{_sid}"
            if _ck not in _desc_map:
                _desc_map[_ck] = _ver.get('raw_text') or _ver.get('description', '') or ''
            with st.expander(f'{_sid} 描述修改', expanded=False):
                _desc_map[_ck] = st.text_area(
                    '描述（修改后本会话生效，不写回底层数据）',
                    value=_desc_map[_ck], key=f'cmp_desc_{_sid}', height=110,
                    label_visibility='collapsed')
        if skill_id:
            _ck = f"{config.get('attacker_mode')}|{config.get('attacker_ship_name')}|{config.get('custom_name') or ''}|{skill_id}"
            desc_input = _desc_map.get(_ck, ver.get('raw_text') or ver.get('description', '') or '')
        else:
            desc_input = ''

        with st.container():
            # 额外 Buff（自然语言条目，字段规则与用户端一致；JSON 兼容）——折叠区间
            # **独立于配置页**：对比基准不带入配置页的 攻击方/敌方额外 Buff，仅吃此处输入
            _cmp_buffs_default = (config.get('cmp_buffs') or '').strip()
            if _cmp_buffs_default == '{}':
                _cmp_buffs_default = ''  # 历史遗留的花括号默认值清空
            with st.expander('额外 Buff（对比专用，独立于配置页）', expanded=bool(_cmp_buffs_default)):
                buffs_json = st.text_area(
                    '额外 Buff',
                    value=_cmp_buffs_default,
                    height=90, key='cmp_buffs',
                    help='每行/逗号一条，字段名与用户端一致（简化名，不带"增加"）：\n'
                         '暴击伤害+100%、炮击+155、全伤害+10%、全伤害减免+5%、'
                         '对小型伤害增加+20%、对驱逐伤害增加+30%、命中+50（固定）/%（百分比）\n'
                         '兼容旧写法/描述式写法：全伤害加成10%、攻击伤害减免-5%、航空伤害额外+15% 等\n'
                         '不支持时间类输入（持续N秒）\n'
                         '面板赋值（等号，两侧生效）：炮击=800、幸运=50\n'
                         'JSON 格式（兼容）: {"全伤害增加": 0.1}\n\n'
                         '注：对比基准不含配置页的 攻击方/敌方额外 Buff（独立调试环境）',
                    label_visibility='collapsed')
            config['cmp_buffs'] = buffs_json

            # 战斗参数（航向/制空/阵型 随「角色配置与伤害」页的选择同步，本页不再单独设置；
            # 总伤害/点燃对比的统计时间强制取配置页的战斗时长，本页不再单独设时间）
            tl_time = float(config.get('calc_time') or 0)
            st.caption(f'战斗参数（随配置页）：航向 {course}｜制空 {air_status}｜阵型 {formation or "机动"}'
                       f'｜时长 {tl_time:g} 秒')
            cmp_field_map = {
                '通常技': ('skill_attack', 'skill', 'BattleSkill1'),
                '必杀技': ('burst_attack', 'burst', 'BattleSkill2'),
                '支援技': ('support_attack', 'support', 'SupportSkill'),
            }

            buffs_input, panel_override, buff_err = data_api.parse_buff_input(buffs_json)
            if buff_err:
                st.warning(buff_err)

            # 第三步：确认后全项输出（与"角色配置与伤害"页同口径：
            # 单次伤害逐项 + 点燃/总伤害时间轴），无对比项选择
            if st.button('确认并计算对比', type='primary'):
                if buffs_input is None:
                    st.stop()
                # 防守方状态显式提示：未设防守方=原始伤害口径（不含防御衰减/克制/敌方减伤）
                if not enemy_ready:
                    st.caption('⚠ 未设防守方：本次对比只输出原始伤害口径。'
                               '需要实际伤害请先在上方「敌方来源」选择 已有角色（须有计算表）或自定义靶子（面板不能全为0）')
                else:
                    st.caption(f"防守方: {enemy_base_dict.get('name') or '自定义靶子'}"
                               f"（装甲 {float(enemy_base_dict.get('装甲', 0) or 0):g}，"
                               f"装甲类型 {enemy_base_dict.get('装甲类型', '轻甲')}）"
                               f"——单次与时间轴对比均按真实防守方计算（含防御衰减/克制/敌方减伤）；"
                               f"对比基准不含配置页额外 Buff")

                def _calc_single(title, base_segments, base_sbuffs, new_segments, new_sbuffs, note=None,
                                 base_edeb=None, new_edeb=None):
                    """单次伤害对比（普攻或单个技能）：基准=角色原配置（不含配置页额外 Buff），
                    修改组=新描述+对比页额外 Buff。"""
                    # 基准 buffs：取 attacker_cd_clean（角色计算表真实 buffs+天赋，
                    # **不含配置页额外 Buff**——对比 Buff 区间独立于配置页，用户口径 2026-09-05）
                    base_buffs = dict(attacker_cd_clean.buffs) \
                        if attacker_cd_clean is not None \
                        else dict(ship.get('buffs') or {})
                    # 面板赋值（如「炮击=800」）：受控变量，两侧同时覆盖（深拷贝避免污染原配置）
                    ship_base = copy.deepcopy(ship)
                    ship_new = copy.deepcopy(ship)
                    ship_base['buffs'] = dict(base_buffs)
                    ship_new['buffs'] = dict(base_buffs)
                    # 天赋效果同步进对比两侧：buffs 已随 attacker_cd_clean 同源带入，面板增量在此补齐
                    if talent_eff and talent_eff.get('ok'):
                        for tk, tv in (talent_eff.get('panel') or {}).items():
                            try:
                                fv = float(tv)
                            except (TypeError, ValueError):
                                continue
                            ship_base.setdefault('panel', {})[tk] = float(ship_base['panel'].get(tk, 0) or 0) + fv
                            ship_new.setdefault('panel', {})[tk] = float(ship_new['panel'].get(tk, 0) or 0) + fv
                    if panel_override:
                        disp = '、'.join(f'{k}={v:g}' for k, v in panel_override.items())
                        st.caption(f'面板赋值（对比两侧同时生效）: {disp}')
                        for pk, pv in panel_override.items():
                            ship_base.setdefault('panel', {})[pk] = float(pv)
                            ship_new.setdefault('panel', {})[pk] = float(pv)
                    # 已设防守方 → 两侧都按真实防守方计算：
                    # 防御衰减/敌方减伤/克制修正生效（enemy=None 则保持原始口径）；
                    # enemy 用独立基准（不含配置页敌方额外 Buff）
                    _cmp_enemy = enemy_base_dict if (enemy_ready and enemy_base_dict) else None

                    def _enemy_with_debuffs(base_enemy, edeb):
                        """技能内敌方减益（被命中敌方 装甲-15% 等）→ 防守方修正副本。"""
                        if not edeb or base_enemy is None:
                            return base_enemy
                        e = dict(base_enemy)
                        armor_pct = float(edeb.get('装甲值百分比增加', 0) or 0)
                        if armor_pct:
                            e['装甲'] = float(e.get('装甲', 0) or 0) * (1 + armor_pct)
                        return e

                    # 基准：当前角色 + 原描述原 buff；对比组：新描述 + 新技能内 buff + 额外 buff
                    result_base = calculator.calculate_raw_damage(
                        ship_base, base_segments, base_sbuffs,
                        course=course, air_status=air_status, ammo_type=ammo_type,
                        enemy=_enemy_with_debuffs(_cmp_enemy, base_edeb)
                    )
                    for bk, bv in buffs_input.items():
                        try:
                            # 简化键 → 引擎标准键；**叠加**而非覆盖：额外 Buff 是在基准配置上追加，
                            # 覆盖会把基准里同名 buff（如 角色自带 攻击力增加0.2）清掉导致伤害反降
                            std = user_engine._normalize_buff_key(bk)
                            ship_new['buffs'][std] = float(ship_new['buffs'].get(std, 0) or 0) + float(bv)
                        except (TypeError, ValueError):
                            st.warning(f'额外 Buff "{bk}" 数值无效，已忽略')
                    result_new = calculator.calculate_raw_damage(
                        ship_new, new_segments, new_sbuffs,
                        course=course, air_status=air_status, ammo_type=ammo_type,
                        enemy=_enemy_with_debuffs(_cmp_enemy, new_edeb)
                    )
                    _ig_b = float((base_edeb or {}).get('点燃抵抗', 0) or 0)
                    _ig_n = float((new_edeb or {}).get('点燃抵抗', 0) or 0)
                    if abs(_ig_n - _ig_b) > 1e-9:
                        st.caption(f'注：燃烧/点燃抵抗 {_ig_b:+.0%} → {_ig_n:+.0%} 影响点燃伤害，'
                                   f'不计入本次直接伤害对比')

                    if _cmp_enemy is not None:
                        _enemy_label = _cmp_enemy.get('name') or '自定义靶子'
                        _cmp_title = f'{title}实际伤害（防守方: {_enemy_label}，含防御衰减/敌方减伤/克制修正）'
                    else:
                        _cmp_title = f'{title}原始伤害（未设防守方，不含衰减/克制）'
                    # 单表对比：段 x 指标逐行排列，基准/修改后并列（全宽展示，替代左右两表与逐段文字列表）
                    enemy_used = bool(result_base.get('enemy_used'))

                    def _fmt_pct(b, n):
                        if b:
                            return f"{(n - b) / b * 100:+.2f}%"
                        return '新增' if n else '—'

                    _cmp_metrics = [
                        ('最终攻击力', 'final_attack'),
                        ('增伤系数', 'damage_increase'),
                        ('单次原始伤害', 'base_damage'),
                        ('单次暴击伤害', 'critical_damage'),
                        ('单次期望伤害', 'expected_damage'),
                    ]
                    cmp_rows = []
                    for sb, sn in zip(result_base.get('segments', []),
                                      result_new.get('segments', [])):
                        label = f"段{sb.get('segment_index', '?')}（{sb.get('damage_type', '')}）"
                        pairs = [(mn, float(sb.get(mk, 0) or 0), float(sn.get(mk, 0) or 0))
                                 for mn, mk in _cmp_metrics]
                        if enemy_used:
                            ab = sb['base_damage'] * sb.get('defense_decay', 1.0) \
                                * sb.get('enemy_reduction', 1.0) * sb.get('modifier', 1.0)
                            an = sn['base_damage'] * sn.get('defense_decay', 1.0) \
                                * sn.get('enemy_reduction', 1.0) * sn.get('modifier', 1.0)
                            pairs.append(('单次实际伤害', ab, an))
                        for mn, bv, nv in pairs:
                            cmp_rows.append({
                                '段': label, '指标': mn,
                                '基准': round(bv, 4), '修改后': round(nv, 4),
                                '变化': round(nv - bv, 4), '变化率': _fmt_pct(bv, nv),
                            })
                    # 总计行（已乘命中数）
                    _total_pairs = [
                        ('总原始伤害', result_base.get('total_original', 0), result_new.get('total_original', 0)),
                        ('总暴击伤害', result_base.get('total_critical', 0), result_new.get('total_critical', 0)),
                        ('总期望伤害', result_base.get('total_expected', 0), result_new.get('total_expected', 0)),
                    ]
                    if enemy_used:
                        _total_pairs.insert(1, ('总实际伤害', result_base.get('total_normal', 0),
                                                result_new.get('total_normal', 0)))
                    for tn, bv, nv in _total_pairs:
                        cmp_rows.append({
                            '段': '总计', '指标': tn,
                            '基准': round(bv, 2), '修改后': round(nv, 2),
                            '变化': round(nv - bv, 2), '变化率': _fmt_pct(bv, nv),
                        })
                    st.markdown(f'**{_cmp_title}对比**')
                    st.dataframe(pd.DataFrame(cmp_rows), use_container_width=True, hide_index=True)
                    if note:
                        st.caption(note)
                    st.caption('注：增伤系数 = 1 + 增伤类 buff + 航向/制空修正'
                               '（如同航炮击 +8%、制空优势 +8%），基准侧已包含角色自带 buff')

                # ==================== 单次对比（仅当前所选计算项） ====================
                if not is_timeline:
                    if calc_mode == '普攻':
                        # 普攻：无技能文本，两侧段相同，差异只来自额外 Buff（多段普攻合并为一次释放）
                        _na_ok = attacker_is_ship and attacker_cd is not None
                        _na_parts = []
                        if _na_ok:
                            for _na in (attacker_cd.normal_attacks or []):
                                _na_parts.extend(_na.get('parts') or [])
                        if not _na_ok:
                            st.info('需已有角色数据（自定义角色无普攻结构）')
                        elif not _na_parts:
                            st.info('该角色没有普攻数据')
                        else:
                            _na_segments = [{'segment_index': i + 1,
                                             'multiplier': float(p.get('multiplier', 0) or 0),
                                             'damage_type': p.get('damage_type', '炮击') or '炮击',
                                             'hit_count': int(p.get('hit', 1) or 1)}
                                            for i, p in enumerate(_na_parts)]
                            _calc_single('普攻', _na_segments, {}, _na_segments, {},
                                         note='普攻无技能描述：对比体现的是额外 Buff 对普攻伤害的影响')
                    else:
                        # 所选技能的单次对比（基准=原文，修改=编辑文本+额外Buff）
                        if not ver:
                            st.info(f'无{calc_mode}数据')
                        else:
                            _b_seg, _b_sb = data_api._resolve_skill(ver)
                            _n_seg, _n_sb = data_api._resolve_skill({'description': desc_input})
                            _b_edeb = data_api._extract_enemy_debuffs_from_text(
                                ver.get('raw_text') or ver.get('description', ''))
                            _n_edeb = data_api._extract_enemy_debuffs_from_text(desc_input or '')
                            if not _b_seg and not _n_seg:
                                st.info(f'{calc_mode}描述无伤害段（纯 buff 技能无单次伤害），请选择「总伤害」对比')
                            else:
                                _calc_single(calc_mode, _b_seg, _b_sb, _n_seg, _n_sb,
                                             base_edeb=_b_edeb, new_edeb=_n_edeb)

                # ==================== 点燃 / 总伤害对比（时间轴模拟，仅当前所选） ====================
                # 时长强制取配置页战斗时长
                _tl_missing = None
                if is_timeline:
                    if not enemy_ready:
                        _tl_missing = '需防守方：请先在"角色配置与伤害"页选择敌方或自定义靶子'
                    elif not attacker_is_ship or attacker_cd is None:
                        _tl_missing = '需已有角色数据（自定义角色技能无 CD，不支持时间轴模拟）'
                    elif tl_time <= 0:
                        _tl_missing = '需战斗时长：请先在"角色配置与伤害"页填写战斗时长（秒）'
                    elif skill_id not in cmp_field_map:
                        _tl_missing = '请选择要修改的技能类型（通常技/必杀技/支援技）'
                if is_timeline and not _tl_missing:
                    st.markdown('---')
                    if skill_id == '支援技':
                        st.caption('注：时间轴模拟不含支援技释放（与用户端口径一致），'
                                   '此处对比体现的是额外 Buff 与新描述对普攻/通常技/必杀技/点燃的影响')
                    st.markdown(f'### {calc_mode}对比（时间轴模拟 {tl_time:g} 秒，时长取配置页战斗时长）')
                    field, trig, raw_field = cmp_field_map[skill_id]
                    # 两侧技能段由描述文本解析（当前所选技能）；纯 buff 描述（无伤害段）同样支持：
                    # 时间轴按 has_dmg=False 触发该技能的 buff 窗口，只跳过伤害累计
                    # （与用户端/自测口径一致）
                    base_segments, base_sbuffs = data_api._resolve_skill(ver)
                    if not desc_input.strip():
                        st.error('技能描述为空')
                        st.stop()
                    new_segments, new_sbuffs = data_api._resolve_skill({'description': desc_input})
                    if not new_segments and not new_sbuffs:
                        # 无伤害段也无技能内静态增伤时，仍接受纯 buff 描述：
                        # 持续窗口/时段 buff/循环 CD 任一存在即可参与时间轴对比
                        _rf = (cmp_field_map.get(skill_id) or ('', '', ''))[2]
                        _has_win = bool(data_api.extract_skill_buffs({_rf: desc_input})) if _rf else False
                        if not _has_win \
                                and not data_api.extract_period_buffs_from_text(desc_input) \
                                and extract_loop_cd(desc_input) is None:
                            st.error('修改后的描述未解析出伤害段或任何 buff / 循环CD，请检查格式')
                            st.stop()
                    # 时间轴基准：独立于配置页额外 Buff（attacker_cd_clean，角色自带 buffs/天赋保留）
                    base_cd = copy.deepcopy(attacker_cd_clean) if attacker_cd_clean is not None else copy.deepcopy(attacker_cd)
                    new_cd = copy.deepcopy(base_cd)
                    # 面板赋值：受控变量，两侧同时覆盖
                    for pk, pv in panel_override.items():
                        if pk in base_cd.attack_panel:
                            base_cd.attack_panel[pk] = float(pv)
                            new_cd.attack_panel[pk] = float(pv)
                    # 两侧技能段均由描述文本重建（与单次对比同口径），
                    # 并同步解析「每X秒」的循环 CD——否则新描述改了循环周期
                    # （如 22秒→16秒）不会体现在总伤害里（出手次数不变，结果失真）
                    # 技能内无持续时间的静态增伤（对航母/全伤害等 sbuffs）挂到技能结构的
                    # static_buffs：仅该技能每次出手结算时生效，不吃普攻/必杀/点燃。
                    # 例外：夜战/昼战时段条件且无持续时间的增伤——绑定对应技能按时间轴
                    # 计算（首次触发后一直生效），不走单次静态，也不并入基础 buffs
                    for cd_obj, segs, txt, sbufs in (
                            (base_cd, base_segments, ver.get('raw_text') or ver.get('description') or '', base_sbuffs),
                            (new_cd, new_segments, desc_input, new_sbuffs)):
                        parts = [{'hit': int(s.get('hit_count', 1) or 1),
                                  'multiplier': f"{s['multiplier'] * 100:g}%",
                                  'damage_type': s.get('damage_type', '炮击'),
                                  'weapon_type': s.get('weapon_type', '') or ''} for s in segs]
                        entry = getattr(cd_obj, field)
                        entry['parts'] = parts
                        # 循环 CD 解析（共用通用工具 extract_loop_cd，与重建管线同一实现）
                        cd_val = extract_loop_cd(txt or '')
                        if cd_val:
                            entry['cd'] = cd_val
                        _pb_keys = {user_engine._normalize_buff_key(k)
                                    for k in data_api.extract_period_buffs_from_text(txt or '')}
                        if sbufs:
                            _static = {}
                            for bk, bv in sbufs.items():
                                try:
                                    std = user_engine._normalize_buff_key(bk)
                                    fv = float(bv)
                                except (TypeError, ValueError):
                                    continue
                                if std in _pb_keys:
                                    continue  # 时段buff（无持续时间）→ 下面按时间轴窗口挂载
                                _static[std] = fv
                            if _static:
                                entry['static_buffs'] = _static
                    # 额外 Buff 仅作用于对比组（与单次对比一致）；**叠加**而非覆盖，
                    # 避免清掉基准配置里同名 buff（如 角色自带 攻击力增加）导致数值反向
                    for bk, bv in buffs_input.items():
                        std = user_engine._normalize_buff_key(bk)
                        new_cd.buffs[std] = float(new_cd.buffs.get(std, 0) or 0) + float(bv)
                    # 修改描述中的持续 buff 窗口替换该技能原窗口（同管线提取规则）
                    new_windows = [w for w in data_api.extract_skill_buffs({raw_field: desc_input})
                                   if w.get('trigger') == trig]
                    new_cd.skill_buffs = (
                        [w for w in new_cd.skill_buffs if w.get('trigger') != trig] + new_windows)
                    # 时段buff（夜战/昼战且无持续时间）：绑定对应技能——首次触发后一直生效
                    # 到战斗结束，按时间轴窗口计算；在窗口替换之后追加，避免被同触发替换清掉
                    for cd_obj, ptxt in ((base_cd, ver.get('raw_text') or ver.get('description') or ''),
                                         (new_cd, desc_input)):
                        _pw = {}
                        for _k, _v in data_api.extract_period_buffs_from_text(ptxt or '').items():
                            try:
                                _pw[user_engine._normalize_buff_key(_k)] = float(_v)
                            except (TypeError, ValueError):
                                continue
                        if _pw:
                            cd_obj.skill_buffs = (cd_obj.skill_buffs or []) + [
                                {'trigger': trig, 'target': 'self',
                                 'duration': float(tl_time), 'buffs': _pw}]
                    # buff 先后顺序按各自描述原文判定
                    raw_skills_new = dict(raw_skills or {})
                    raw_skills_new[raw_field] = desc_input
                    tl_base = user_engine.calc_total_damage_timeline(
                        base_cd, enemy_base_dict or enemy_dict, course, air_status, tl_time, raw_skills=raw_skills)
                    tl_new = user_engine.calc_total_damage_timeline(
                        new_cd, enemy_base_dict or enemy_dict, course, air_status, tl_time, raw_skills=raw_skills_new)

                    def _pct(new_v, base_v):
                        if base_v:
                            return f"{(new_v - base_v) / base_v * 100:+.2f}%"
                        return '新增' if new_v else '—'

                    cmp_rows = []
                    # buff 列展示修改后侧该技能的持续窗口（伤害为 0 的纯 buff 技能也可见改了什么 buff）
                    def _buff_desc(tl, trig):
                        if not trig:
                            return '—'
                        parts = []
                        for w in tl['windows']:
                            if w.get('trigger') != trig:
                                continue
                            bs = '、'.join(f"{k}{float(v):+.0%}" if abs(float(v)) < 1 else f"{k}{float(v):g}"
                                           for k, v in (w.get('buffs') or {}).items())
                            if bs:
                                parts.append(f"{bs} 持续{float(w['duration']):g}秒，触发{w.get('count', 0)}次")
                        return '；'.join(parts) or '—'

                    for lb, trig in (('普攻', ''), ('通常技', 'skill'),
                                     ('必杀技', 'burst'), ('支援技', 'support')):
                        b, n = tl_base['totals'][lb], tl_new['totals'][lb]
                        cb, cn = tl_base['counts'][lb], tl_new['counts'][lb]
                        # 纯 buff 技能伤害为 0：只要触发过（计次>0）就保留行，写明释放几次与 buff
                        if abs(b) < 1e-9 and abs(n) < 1e-9 and cb == 0 and cn == 0:
                            continue
                        cmp_rows.append({
                            '项': lb,
                            '变化率': _pct(n, b),
                            '基准': f"{b:.0f}（{cb}次）",
                            '修改后': f"{n:.0f}（{cn}次）",
                            '变化': f"{n - b:+.0f}",
                            '修改后buff': _buff_desc(tl_new, trig),
                        })
                    b_ig, n_ig = tl_base['ignite']['actual'], tl_new['ignite']['actual']
                    if abs(b_ig) > 1e-9 or abs(n_ig) > 1e-9:
                        cmp_rows.append({
                            '项': '点燃', '变化率': _pct(n_ig, b_ig),
                            '基准': f"{b_ig:.0f}", '修改后': f"{n_ig:.0f}",
                            '变化': f"{n_ig - b_ig:+.0f}",
                        })
                    # 合计实际总伤害行（总伤害模式展示；点燃模式只看点燃行）
                    gb, gn = tl_base['grand'], tl_new['grand']
                    cmp_rows.append({
                        '项': '合计实际总伤害', '变化率': _pct(gn, gb),
                        '基准': f"{gb:.0f}", '修改后': f"{gn:.0f}",
                        '变化': f"{gn - gb:+.0f}",
                    })
                    if calc_mode == '点燃':
                        # 只看点燃：过滤掉其余攻击行与合计行
                        cmp_rows = [r for r in cmp_rows if r['项'] == '点燃']
                        if not cmp_rows:
                            st.info('该时间窗内没有点燃伤害（角色无点燃段或未触发点燃）')
                    if cmp_rows:
                        st.dataframe(pd.DataFrame(cmp_rows), use_container_width=True, hide_index=True)

                    # DPS 曲线（基准 vs 修改后，每秒伤害 + 点燃均摊；用户口径 2026-09-05）
                    _ips_b = (tl_base['ignite']['actual'] / tl_time) if tl_time else 0
                    _ips_n = (tl_new['ignite']['actual'] / tl_time) if tl_time else 0
                    _df = pd.DataFrame({
                        '秒': [t + 1 for t in range(tl_time)],
                        '基准': [round(v + _ips_b, 1) for v in tl_base.get('series') or []],
                        '修改后': [round(v + _ips_n, 1) for v in tl_new.get('series') or []],
                    }).set_index('秒')
                    st.line_chart(_df, height=240)
                    st.caption('DPS 曲线：每秒伤害（技能/普攻出手 + 点燃均摊），峰值为出手秒')

                    # 时间轴明细（两级折叠：基准/修改后 → 类别 → 第N次）
                    def _render_tl_procs(tl, tag):
                        _tl_procs = tl.get('procs') or {}
                        if not any(_tl_procs.values()):
                            st.caption(f'{tag}：无出手明细')
                            return
                        for lb in ('通常技', '必杀技', '支援技', '普攻'):
                            _items = _tl_procs.get(lb) or []
                            if not _items:
                                continue
                            with st.expander(f'{lb}（出手 {_items[-1]["seq"]} 次）'):
                                for it in _items:
                                    with st.expander(f'第{it["seq"]}次（t≈{it["t"]:g}s）'):
                                        for sub, proc_lines in it['blocks']:
                                            if sub:
                                                st.markdown(f'{sub}')
                                            st.text('\n'.join(proc_lines))

                    _want_detail = calc_mode if calc_mode != '点燃' else '总伤害'
                    with st.expander(f'伤害明细（{_want_detail}对比，按类别逐次出手）'):
                        with st.expander('基准（原描述）'):
                            _render_tl_procs(tl_base, '基准')
                        with st.expander('修改后（新描述 + 额外 Buff）'):
                            _render_tl_procs(tl_new, '修改后')

    st.divider()