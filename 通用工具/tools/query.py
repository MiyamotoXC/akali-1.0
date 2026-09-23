# -*- coding: utf-8 -*-
"""query tools."""
import re
from qwen_agent.tools.base import BaseTool
from .talent import load_talent_pvp

# ======================================================================
# 头像标记：查询角色信息时从图片素材库匹配头像，找到则插入到「--- 名字 ---」行后
# ======================================================================

def _avatar_mark(app, ship_name: str) -> str:
    """查角色头像相对路径（素材库：桌面/FX杂项/头像 与 头像（改）/）。
    返回 '\\n<<AVATAR>>path|title'（web_app 解析为 avatar 字段渲染）；找不到返回 ''。"""
    try:
        p = find_avatar_for_app(app, ship_name)
        if p:
            return "\n<<AVATAR>>%s|%s" % (p, ship_name)
    except Exception:
        pass
    return ''


def _insert_avatar_after_name(text: str, app, ship_name: str) -> str:
    """把头像标记插入到输出第一行「--- 名字 ---」之后（头像显示在名字下面）；
    找不到头像或没有名字行则原样返回。"""
    av = _avatar_mark(app, ship_name)
    if not av:
        return text
    # 找第一行 `--- 名字 ---`（format_raw_ship_summary 首行格式）
    lines = text.split('\n')
    for i, ln in enumerate(lines):
        if ln.strip().startswith('--- ') and ln.strip().endswith(' ---'):
            lines.insert(i + 1, av.strip())
            return '\n'.join(lines)
    return text + av

# ======================================================================
# 工具 1：查询角色信息
# ======================================================================

class QueryShipInfo(BaseTool):
    """查询已有角色信息。支持昵称/简称/谐音匹配（如"大哥"→威尔士亲王、"凌波"→绫波）。
    可选 field 参数查询具体字段（如航空、天赋、必杀技、舰种等），不填则返回完整面板。"""
    name = 'query_ship_info'
    description = '查询已有角色信息。支持昵称/简称/谐音匹配。可选 field 参数查询具体字段（如 天赋/航空/炮击/必杀技/通常技/舰种/装甲 等），不填返回完整面板。'
    parameters = {
        'type': 'object',
        'properties': {
            'ship_name': {
                'type': 'string',
                'description': '要查询的角色名',
            },
            'field': {
                'type': 'string',
                'description': '可选，查询具体字段。支持：天赋/天赋树；航空/炮击/雷击/轰炸/鱼雷/索敌/命中/幸运/装填/装甲/对空/机动/血量/回避率/防暴击率/航速（面板数值）；舰种/装甲类型/弹种/稀有/所属（元信息）；旗舰技/支援技/通常技/必杀技/技能（技能描述）；buff。',
            },
        },
        'required': ['ship_name'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        ship_name = params['ship_name']

        # 引导性输入（无具体名字）→ 反问查阅哪位舰灵
        if ship_name.strip() in ('角色', '舰灵', '舰娘', '舰船', '船', '舰', '哪位', '哪些'):
            return "指挥官具体要查阅哪位舰灵的信息呢？直接告诉我名字即可。"

        matched_name, raw_data = app.get_raw_ship(ship_name)
        if raw_data is None:
            return f"未找到角色: {ship_name}。"
        # 信息不全拦截：按用户要求，缺信息的角色等同"没有该角色"，回复口径与未找到完全一致
        if not app.is_ship_complete(ship_name):
            return f"未找到角色: {ship_name}。"

        field = (params.get('field') or '').strip()
        if not field:
            text = app.format_raw_ship_summary(matched_name, raw_data)
        else:
            text = _format_field_result(matched_name, raw_data, field, app)
        # 头像：从图片素材库匹配，插入到「--- 名字 ---」行后（前端渲染），找不到不加
        text = _insert_avatar_after_name(text, app, matched_name)
        # 原型舰历史按钮：完整查询（无 field）且该角色有历史资料时，末尾插标记
        # <<STORY>>舰名（前端渲染为「历史上的故事」小按钮，点击调 /api/ship_story 弹窗）
        if not field and app.get_ship_story(matched_name):
            text = text.rstrip() + f"\n<<STORY>>{matched_name}"
        return text


# ======================================================================
# 工具 1b：查询角色信息（标准化面板 + 天赋 + 技能；防守方可只取面板+天赋）
# ======================================================================

class QueryShipCopy(BaseTool):
    """查询角色的标准化信息：面板数值（已计算羁绊和天赋）+ 天赋四行二列 + 原始技能描述。
    用于伤害计算分步向导中展示攻防方角色详情。"""
    name = 'query_ship_copy'
    description = '查询角色信息（伤害计算向导用）：面板数值（已计算羁绊和天赋，含 航空+17%/雷击+10%/血量+10%/对空+10%/装甲+17%/命中+10%/幸运+5% 加成）+ 天赋四行二列（含效果数值）+ 原始信息表技能描述原文；include_skills=false 时只输出 面板+天赋（防守方用，不输出技能）。'
    parameters = {
        'type': 'object',
        'properties': {
            'ship_name': {'type': 'string', 'description': '角色名'},
            'include_skills': {'type': 'boolean', 'description': '是否输出技能描述（默认 true 输出；防守方查询时传 false 只输出面板+天赋）'},
        },
        'required': ['ship_name'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        ship_name = params['ship_name']
        include_skills = params.get('include_skills', True)
        # 副本（storage）与原始信息表（raw_storage）都需要
        ship = app.get_ship_from_all(ship_name)
        matched_name, raw_data = app.get_raw_ship(ship_name)
        if ship is None and raw_data is None:
            return f"未找到角色: {ship_name}。"
        # 信息不全拦截：等同"没有该角色"，与未找到同口径（中止后续输出/计算）
        if not app.is_ship_complete(ship_name):
            return f"未找到角色: {ship_name}。"
        lines = [f"\n--- {matched_name or ship.name} ---"]
        # 面板（紧凑：攻击/防御各一行；已计算羁绊与天赋加成）
        if ship is not None:
            ap = ship.attack_panel
            dp = ship.defense_panel
            # 口径修正（2026-09-14 审计）：底层面板 = 100级满星满强化，**不含天赋**；
            # 天赋的面板增量在计算时按所选点法另行叠加，原「已计算羁绊和天赋」表述有误。
            lines.append("  面板数值（100级 满星 满强化；未含天赋，天赋按所选点法另行叠加）：")
            atk_fields = [('炮击', ap.get('炮击', 0)), ('雷击', ap.get('雷击', 0)), ('航空', ap.get('航空', 0)),
                          ('轰炸', ap.get('轰炸', 0)), ('鱼雷', ap.get('鱼雷', 0)), ('索敌', ap.get('索敌', 0)),
                          ('命中', ap.get('命中', 0)), ('幸运', ap.get('幸运', 0)), ('装填', ap.get('装填', 0))]
            def_fields = [('装甲', dp.get('装甲', 0)), ('对空', dp.get('对空', 0)), ('机动', dp.get('机动', 0)),
                          ('血量', dp.get('血量', 0)), ('回避率', dp.get('回避率', 0)), ('防暴击率', dp.get('防暴击率', 0)),
                          ('点燃抵抗', float(ship.buffs.get('点燃抵抗', 0) or 0))]
            lines.append("    攻击面板: " + " ".join(
                f"{k}={v:.1f}" + ("（实战影响不大）" if k == '装填' else "") for k, v in atk_fields if v))
            # 防御面板固定输出 7 字段（回避率/防暴击率/点燃抵抗 默认为 0，0 也显示）
            lines.append("    防御面板: " + " ".join(f"{k}={v:.1f}" for k, v in def_fields))
            # 航速：通用面板字段（等级无关常量，不随羁绊/天赋变化），旗舰技门槛会读它
            spd = float(getattr(ship, "speed", 0) or 0)
            if spd:
                lines.append(f"    航速: {spd:.0f} 节")
        # 天赋（含具体效果数值）→ 技能描述（复用 raw 格式化，天赋段在技能段之前）
        # include_skills=False（防守方）只输出 面板+天赋，不含技能
        # 仅计算器功能：天赋标题附带该角色默认 PVP 点法编码
        if raw_data is not None:
            raw_out = app.format_raw_ship_summary(matched_name, raw_data)
            # 定位天赋段（用「天赋 ---」标题行，避免命中面板里的"已计算羁绊和天赋"字样）
            talent_start = raw_out.find('天赋 ---')
            skill_start = raw_out.find('技能描述')
            if talent_start != -1:
                end = skill_start if skill_start != -1 else raw_out.find('---------------------', talent_start)
                talent_block = raw_out[talent_start:end].rstrip()
                # 标题「天赋 ---」→「天赋（默认PVP: XXXX）---」
                pvp_code = load_talent_pvp().get(matched_name or '', '')
                if pvp_code:
                    talent_block = re.sub(r'^天赋\s*---', '天赋（默认PVP: %s）---' % pvp_code, talent_block)
                lines.append(talent_block)
            if include_skills and skill_start != -1:
                # 技能描述完整输出（通常技/必杀技/旗舰技/支援技）
                skill_block = raw_out[skill_start:raw_out.find('---------------------', skill_start)]
                lines.append(skill_block)
        text = "\n".join(lines)
        # 头像：从图片素材库匹配，插入到「--- 名字 ---」行后（前端渲染），找不到不加
        return _insert_avatar_after_name(text, app, matched_name or ship.name)


# ======================================================================
# 工具 1c-2：多角色属性求和（索敌之和等）
# ======================================================================

class QueryScoutSum(BaseTool):
    """查询多个角色（最多 6 个）的「索敌」面板属性并求和（仅支持索敌之和）。
    用于「丹阳+女灶神的索敌是多少」「丹阳和女灶神索敌之和」等请求。
    每个角色取与查询界面一致的已计算索敌数值（含羁绊/天赋），找不到/信息不全的角色会标注并跳过。"""
    name = 'query_scout_sum'
    description = '查询多个角色（最多 6 个）的「索敌」之和。参数 ship_names：角色名列表（用 +/和/、 分隔或传数组，最多 6 个）。返回各角色索敌明细与总和。仅支持索敌，不支持其他属性。'
    parameters = {
        'type': 'object',
        'properties': {
            'ship_names': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '角色名列表，最多 6 个（如 ["丹阳", "女灶神"]）',
            },
        },
        'required': ['ship_names'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        # 兼容：ship_names 可能是字符串（"丹阳+女灶神"）→ 按分隔符拆
        raw_names = params.get('ship_names')
        if isinstance(raw_names, str):
            raw_names = re.split(r'[+\s，,、;；与和及&/]', raw_names)
        if not isinstance(raw_names, (list, tuple)):
            raw_names = [raw_names] if raw_names else []
        raw_names = [str(n).strip() for n in raw_names if str(n).strip()]
        # 去重保序，截断到 6 个
        seen, names = set(), []
        for n in raw_names:
            if n not in seen:
                seen.add(n)
                names.append(n)
            if len(names) >= 6:
                break

        field = '索敌'  # 仅支持索敌之和

        if not names:
            return "指挥官要查哪几位舰灵的%s之和？把名字列给我即可（最多 6 位）。" % field

        rows = []          # (展示名, 数值)
        missing = []       # 未找到（含信息不全：缺数据等同没有该角色）
        for nm in names:
            ship = app.get_ship_from_all(nm)
            if ship is None:
                missing.append(nm)
                continue
            if not app.is_ship_complete(nm):
                missing.append(nm)
                continue
            val = float(ship.attack_panel.get(field, 0) or 0)
            rows.append((ship.name, val))

        parts = ["%s %s %.1f" % (name, field, val) for name, val in rows]
        total = sum(v for _, v in rows)
        lines = []
        if parts:
            lines.append("、".join(parts))
        if missing:
            lines.append("（未找到：%s）" % "、".join(missing))
        if rows:
            lines.append("%d 位舰灵%s之和：%.1f。" % (len(rows), field, total))
        else:
            lines.append("没有可用数据。")
        return "\n".join(lines)


# ======================================================================
# 查询字段关键词映射表
# ======================================================================

# 字段关键词 → (类别, 字段名/键)
# 类别：'panel_meta'（元信息）、'panel_num'（面板数值）、'normal'（普攻）、'skill'（技能）、'buff'
_FIELD_KEYWORDS = {
    # 天赋四行 A/B 分支
    '天赋': ('talent', ''),
    '天赋树': ('talent', ''),
    # 面板数值（attack_panel / defense_panel）
    '航空': ('panel_num', '航空'),
    '炮击': ('panel_num', '炮击'),
    '雷击': ('panel_num', '雷击'),
    '轰炸': ('panel_num', '轰炸'),
    '鱼雷': ('panel_num', '鱼雷'),
    '索敌': ('panel_num', '索敌'),
    '命中': ('panel_num', '命中'),
    '幸运': ('panel_num', '幸运'),
    '装填': ('panel_num', '装填'),
    '装甲': ('panel_num', '装甲'),
    '对空': ('panel_num', '对空'),
    '机动': ('panel_num', '机动'),
    '血量': ('panel_num', '血量'),
    '回避率': ('panel_num', '回避率'),
    '防暴击率': ('panel_num', '防暴击率'),
    '航速': ('panel_num', '航速'),
    # 面板元信息
    '舰种': ('panel_meta', '舰种'),
    '装甲类型': ('panel_meta', '装甲类型'),
    '弹种': ('panel_meta', '弹种'),
    '稀有': ('panel_meta', '稀有'),
    '所属': ('panel_meta', '所属'),
    # 普攻
    '普攻': ('normal', ''),
    '普通攻击': ('normal', ''),
    # 技能
    '旗舰技': ('skill', 'LeaderSkill'),
    '旗舰': ('skill', 'LeaderSkill'),
    '支援技': ('skill', 'SupportSkill'),
    '支援': ('skill', 'SupportSkill'),
    '通常技': ('skill', 'BattleSkill1'),
    '技能1': ('skill', 'BattleSkill1'),
    '必杀技': ('skill', 'BattleSkill2'),
    '技能2': ('skill', 'BattleSkill2'),
    '技能': ('skill', 'ALL'),
    '全部技能': ('skill', 'ALL'),
    # buff
    'buff': ('buff', ''),
    '加成': ('buff', ''),
}


def _format_field_result(name: str, raw: dict, field: str, app) -> str:
    """根据 field 关键词格式化输出角色的指定字段。"""
    key = field.strip().lower()
    # 精确匹配关键词
    mapping = _FIELD_KEYWORDS.get(field.strip())
    if mapping is None:
        # 尝试模糊匹配：用户输入包含关键词
        for kw, val in _FIELD_KEYWORDS.items():
            if kw in field:
                mapping = val
                break
    if mapping is None:
        return f"不支持的字段: {field}。支持：天赋/航空/炮击/必杀技/通常技/舰种/装甲 等。"

    category, field_key = mapping
    lines = [f"\n--- {name} - {field} ---"]

    if category == 'talent':
        talents = raw.get('talents') or {}
        if not talents:
            lines.append("  天赋: 未配置")
        else:
            for row_key in sorted(talents, key=lambda x: int(x) if str(x).isdigit() else 99):
                row = talents.get(row_key) or {}
                for branch in ('A', 'B'):
                    item = row.get(branch) or {}
                    talent_name = str(item.get('name') or '').strip()
                    effect = str(item.get('effect') or '').strip()
                    if talent_name or effect:
                        lines.append(f"  [{row_key}{branch}] {talent_name or '未命名'}")
                        if effect:
                            lines.append(f"  {effect}")

    elif category == 'panel_num':
        # 面板数值：从 raw_data.panel.面板 读取
        pn = raw.get('panel', {}).get('面板', {})
        val = pn.get(field_key)
        if val is None or val == '':
            lines.append(f"  {field_key}: 未设置")
        else:
            # 装填属性：标注实战影响不大（普攻CD缩减公式上限25%，伤害计算不接入）
            tag = "（实战影响不大）" if field_key == '装填' else ""
            lines.append(f"  {field_key}: {val}{tag}")

    elif category == 'panel_meta':
        # 面板元信息：从 raw_data.panel 读取
        p = raw.get('panel', {})
        val = p.get(field_key)
        if val is None or val == '':
            lines.append(f"  {field_key}: 未设置")
        else:
            lines.append(f"  {field_key}: {val}")

    elif category == 'normal':
        # 普攻信息：用户要求不输出任何普攻相关内容
        lines.append("  普攻: 不展示（普攻信息已隐藏）")

    elif category == 'skill':
        # 技能描述
        skills = raw.get('skills', {})
        if field_key == 'ALL':
            # 列出全部技能
            if not skills:
                lines.append("  技能: 未配置")
            else:
                # 顺序：通常技 → 必杀技 → 旗舰技 → 支援技
                for key, label in [('BattleSkill1', '通常技'), ('BattleSkill2', '必杀技'),
                                   ('LeaderSkill', '旗舰技'), ('SupportSkill', '支援技')]:
                    desc = skills.get(key, '')
                    if desc and isinstance(desc, str) and desc.strip() and desc.strip() not in ('???', '？？？'):
                        lines.append(f"  [{label}]")
                        lines.append(f"  {desc}")
        else:
            # 单个技能
            label_map = {'LeaderSkill': '旗舰技', 'SupportSkill': '支援技',
                         'BattleSkill1': '通常技', 'BattleSkill2': '必杀技'}
            label = label_map.get(field_key, field_key)
            desc = skills.get(field_key, '')
            if not desc or not isinstance(desc, str) or not desc.strip() or desc.strip() in ('???', '？？？'):
                lines.append(f"  [{label}]: 未配置")
            else:
                lines.append(f"  [{label}]")
                lines.append(f"  {desc}")

    elif category == 'buff':
        # buff：从计算表（storage）读取，展示非零 buff
        ship = app.get_ship_from_all(name)
        if ship is None:
            lines.append("  Buff: 角色未加载计算表")
        else:
            buff_items = [(k, v) for k, v in ship.buffs.items() if v != 0]
            if not buff_items:
                lines.append("  Buff: 无")
            else:
                lines.append("  Buff:")
                for k, v in buff_items:
                    lines.append(f"    {k}: {v:.2f}")
            # 舰种专属增伤/减伤
            if ship.ship_type_damage_bonus:
                lines.append("  舰种专属增伤:")
                for k, v in ship.ship_type_damage_bonus.items():
                    lines.append(f"    对{k}: +{v:.0%}")
            if ship.ship_type_damage_reduction:
                lines.append("  舰种专属减伤:")
                for k, v in ship.ship_type_damage_reduction.items():
                    lines.append(f"    对{k}: {v:.0%}")
            # 拉古兹增伤（仅遭遇战模块读取，这里仅展示）
            if getattr(ship, 'raguz_bonus', 0) != 0:
                lines.append(f"  拉古兹增伤: {ship.raguz_bonus:.0%}")

    lines.append("---------------------")
    return "\n".join(lines)


# ======================================================================
# 工具 1c：用户自定义角色缓存（持久化基础面板）
# ======================================================================

class SaveUserShip(BaseTool):
    """【用户角色】保存/更新用户对指定角色的自定义面板（跨会话持久化，写入用户副本 user_data.json）。
    输入角色名 + 面板字段：无符号数值（炮击2000 命中300）为**绝对赋值**；
    带 +/-（炮击+200 雷击-50）为**增量**（在基础面板上 +值累加）。
    本地副本只接受增量写法，不支持绝对赋值（赋值请在计算模块内使用）。
    保存后**所有计算（遭遇战/PVP/单体）**均以副本面板为基础。"""
    name = 'save_user_ship'
    description = '【用户角色】在本地副本的基础面板上叠加增量。参数 name：角色名；panel_delta：面板增量（必须带 +/-，如 {"炮击": 200, "命中": -30}）。保存后所有计算以副本面板为基础。'
    parameters = {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': '角色名（已有角色名）'},
            'panel_delta': {'type': 'object', 'description': '面板增量（在基础面板上 +N/-N，如 {"炮击": 200, "命中": -30}）', 'default': {}},
        },
        'required': ['name'],
    }

    def call(self, params, **kwargs):
        from core import runtime
        if runtime.is_cloud():
            return ('云端模式下面板保存在你自己的设备（本地缓存），不写入服务器。'
                    '请在「船坞 / 面板」界面中编辑并保存。')
        from .common import _ATK_PANEL_FIELDS, _DEF_PANEL_FIELDS
        from data import user_data
        params = self._verify_json_format_args(params)
        name = (params.get('name') or '').strip()
        if not name:
            return '请输入角色名。'
        d_atk = {k: float(v) for k, v in (params.get('panel_delta') or {}).items() if k in _ATK_PANEL_FIELDS}
        d_def = {k: float(v) for k, v in (params.get('panel_delta') or {}).items() if k in _DEF_PANEL_FIELDS}
        if not d_atk and not d_def:
            return ('未解析到面板增量。本地副本只接受增量写法（基础面板+值），如「更新面板：名字 炮击+200」或'
                    '「给名字装备 XX炮 炮击+120」。可用攻击字段：炮击/雷击/航空/轰炸/鱼雷/索敌/命中/幸运/装填；'
                    '防御字段：装甲/对空/机动/血量/回避率/防暴击率。')
        # 归一化键：若名字可匹配到已有角色 → 用该角色正规名（"我的前卫" → "前卫"）
        app = (self.cfg or {}).get('app')
        canon = app.get_ship_from_all(name) if app else None
        if canon is None and app is not None:
            # 包含匹配："我的前卫" 含已有角色名 "前卫"
            for k in sorted(app.storage.keys(), key=len, reverse=True):
                if k and k in name:
                    canon = app.get_ship_from_all(k)
                    break
        # 信息不全的角色不得留副本（缺任何基础信息视同"没有该角色"，与查询/计算同口径）
        if canon is not None and app is not None and not app.is_ship_complete(canon.name):
            return f"未找到角色: {name}。"
        key = canon.name if canon else name
        # 取副本当前角色 dict（缺失回退底层）作为写入基座
        base = user_data.get_ship_dict(key)
        if base is None:
            app2 = (self.cfg or {}).get('app')
            if app2 is not None:
                base = app2.storage.get(key)
        if base is None:
            base = {'name': key, 'attack_panel': {}, 'defense_panel': {}, 'buffs': {}}
        # 面板值不能为负：增量在基础面板上累加后若为负 → 报错不写入
        neg_final = sorted({k for k, v in d_atk.items()
                            if float((base.get('attack_panel') or {}).get(k, 0) or 0) + v < 0}
                           | {k for k, v in d_def.items()
                              if float((base.get('defense_panel') or {}).get(k, 0) or 0) + v < 0})
        if neg_final:
            return ('面板值不能为负（增量在基础面板上累加后为负）：' + '、'.join(neg_final)
                    + '。请减小减量。')
        if not user_data.apply_panel_to(base, {}, {}, d_atk, d_def):
            return '保存失败（无法写入 user_data.json）。'
        # 回读最终面板用于展示
        dp = user_data.diff_panels(key)
        cur = {**dp['cur_atk'], **dp['cur_def']}
        out = '已更新用户副本「' + key + '」（所有计算以副本面板为基础）：\n'
        out += '  面板增量: ' + ('、'.join(str(k) + ('+%.0f' % v) if v > 0 else str(k) + ('%.0f' % v) for k, v in {**d_atk, **d_def}.items()) or '无') + '\n'
        out += '  当前面板: ' + ('、'.join(str(k) + ' ' + str(v).rstrip('0').rstrip('.') for k, v in cur.items()) or '无')
        return out


class ListUserShips(BaseTool):
    """【用户角色】列出已保存的用户自定义角色（副本中与底层面板不一致的船）。"""
    name = 'list_user_ships'
    description = '【用户角色】列出所有已修改过面板的用户角色及其与底层数据的差异。'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, params, **kwargs):
        from core import runtime
        if runtime.is_cloud():
            return '云端模式下面板保存在你自己的设备（本地缓存），请在「船坞 / 面板」界面中查看。'
        from data import user_data
        names = user_data.list_modified()
        if not names:
            return '暂无已修改面板的用户角色。可用「保存角色：名字 炮击2000 命中300」或「给名字 炮击+200」添加。'
        lines = ['已修改面板的用户角色（' + str(len(names)) + ' 个，计算以副本为准）：']
        for name in sorted(names):
            dp = user_data.diff_panels(name)
            diff = {**dp['delta_atk'], **dp['delta_def']}
            desc = '、'.join(('%s%+g' % (k, v)) for k, v in sorted(diff.items()))
            lines.append('  ' + name + '：' + (desc or '（无差异）'))
        return chr(10).join(lines)


class RemoveUserShip(BaseTool):
    """【用户角色】删除指定用户角色（从副本移除，恢复底层默认面板）。"""
    name = 'remove_user_ship'
    description = '【用户角色】删除指定用户角色（恢复底层默认面板）。参数 name：角色名。'
    parameters = {
        'type': 'object',
        'properties': {'name': {'type': 'string', 'description': '要删除的角色名'}},
        'required': ['name'],
    }

    def call(self, params, **kwargs):
        from core import runtime
        if runtime.is_cloud():
            return '云端模式下面板保存在你自己的设备（本地缓存），请在「船坞 / 面板」界面中删除。'
        from data import user_data
        params = self._verify_json_format_args(params)
        name = (params.get('name') or '').strip()
        # 归一化名字
        app = (self.cfg or {}).get('app')
        canon = app.get_ship_from_all(name) if app else None
        key = canon.name if canon else name
        if key not in user_data.get_ships():
            return '副本中未修改过「' + key + '」（无需删除）。'
        if user_data.remove_ship(key):
            return '已从用户副本删除「' + key + '」，恢复底层默认面板。'
        return '删除失败（无法写入 user_data.json）。'


# ======================================================================
# 工具 2：按天赋名反查角色（有XX天赋的角色 / 谁带XX天赋）
# ======================================================================

# 效果语义归一：口语/简称 → 天赋效果原文写法（effect 里的标准表述）
_TALENT_DIMENSIONS = (
    '大型', '中型', '小型', '航空', '鱼雷', '雷击', '轰炸', '炮击',
    '夜战', '暴击', '对空', '装甲', '血量', '机动', '回避', '命中', '索敌',
)


def _talent_match(kw: str, text: str) -> bool:
    """天赋关键词是否命中 天赋名+效果 文本。

    匹配顺序：
    ① 关键词子串直接命中（大型防御 / 减伤 在效果原文里）；
    ② 语义归一：减伤/减免/防御 → 伤害下降（效果原文"被XX舰攻击伤害下降"的写法）；
    ③ 拆维度词 AND 匹配：关键词中的维度词（大型/中型/航空…）必须全部出现在文本，
       剩余部分（如"伤害下降"）也必须出现。支持「大型减伤→大型防御」这类口语。
    """
    kw = (kw or '').strip()
    if not kw:
        return False
    if kw in text:
        return True
    kw_std = kw.replace('减伤', '伤害下降').replace('减免', '伤害下降').replace('防御', '伤害下降')
    if kw_std in text:
        return True
    rest = kw_std
    dims = []
    for d in _TALENT_DIMENSIONS:
        if d in rest:
            dims.append(d)
            rest = rest.replace(d, '', 1)
    rest = rest.strip('的与及和')
    for d in dims:
        if d not in text:
            return False
    if rest and rest not in text:
        return False
    return True


class QueryTalentShips(BaseTool):
    """按天赋名/效果关键词反查拥有该天赋的角色（如 大型防御/大型减伤/减伤/鱼雷防御）。
    遍历 raw_data 全量角色的 talents 四行二列，匹配天赋名或效果描述，返回角色列表。"""
    name = 'query_talent_ships'
    description = '按天赋名反查角色：输入天赋名或效果关键词（如 大型防御、大型减伤、减伤、鱼雷防御、航空防御），返回所有拥有该天赋的角色列表。'
    parameters = {
        'type': 'object',
        'properties': {
            'talent_name': {
                'type': 'string',
                'description': '天赋名或效果关键词，如 大型防御、大型减伤、减伤、鱼雷防御',
            },
        },
        'required': ['talent_name'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        kw = (params.get('talent_name') or '').strip()
        if not kw:
            return "要查哪个天赋？告诉我天赋名即可，比如「大型防御」「减伤」。"
        matched = {}  # 天赋名 → {effect, ships: []}
        for ship_name, raw in (app.raw_storage or {}).items():
            # 信息不全的角色不收录（与名录/查询同口径：缺任何基础信息视同不存在）
            if not app.is_ship_complete(ship_name):
                continue
            talents = raw.get('talents') or {}
            for row, opts in talents.items():
                if not isinstance(opts, dict):
                    continue
                for opt, data in opts.items():
                    if isinstance(data, dict):
                        tname = data.get('name') or ''
                        teffect = data.get('effect') or ''
                    else:
                        tname = data or ''
                        teffect = ''
                    if not tname:
                        continue
                    if _talent_match(kw, tname + ' ' + teffect):
                        info = matched.setdefault(tname, {'effect': teffect, 'ships': []})
                        if ship_name not in info['ships']:
                            info['ships'].append(ship_name)
        if not matched:
            return "未找到天赋「%s」相关的角色。可试试：大型防御、中型防御、小型防御、鱼雷防御、航空防御、防御科技 等。" % kw
        items = sorted(matched.items(), key=lambda kv: (-len(kv[1]['ships']), kv[0]))
        if len(items) == 1:
            tname, info = items[0]
            return "拥有天赋「%s」（%s）的角色（%d 位）：\n%s" % (
                tname, info['effect'] or '未知效果', len(info['ships']), '、'.join(info['ships']))
        total = sum(len(v['ships']) for _, v in items)
        lines = ['按「%s」找到 %d 个相关天赋、共 %d 位角色：' % (kw, len(items), total)]
        for tname, info in items:
            ships = info['ships']
            shown = '、'.join(ships[:12]) + ('…等 %d 位' % len(ships) if len(ships) > 12 else '')
            lines.append('■ %s（%s）— %d 位：%s' % (tname, info['effect'] or '未知效果', len(ships), shown))
        return '\n'.join(lines)

"""头像匹配模块：从图片素材库（桌面/头像/ 与 头像（改）/）查找角色头像。

- 索引构建：扫描素材目录，文件名去编号前缀 → 规范化名 → 头像相对路径。
- 匹配：角色名（昵称/正规名）→ 头像路径；找不到返回 None（不输出头像）。
- 支持：·改 → 改版头像（夕立·改 → 头像（改）/夕立改.png）、
  括号编号剥离（拉菲(DD-459) → 拉菲）、间隔号归一（朱利奥·凯撒 → 朱利奥凯撒）、
  子目录头像（敷波/沃克兰/Z21 等素材在子目录内，取 A01.png）、昵称别名匹配。

输出相对路径基于 桌面/FX杂项/，前端经 /avatar/ 静态路由访问。
"""
import json
import os
import re

# 素材库根：query.py 位于 阿卡莉1.0/通用工具/tools/，上溯 2 级即 阿卡莉1.0
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))  # 阿卡莉1.0
_KB_IMG_DIR = os.path.normpath(os.path.join(_PROJECT_ROOT, '知识库', '图片素材'))  # 头像库根（相对路径基准）
AVATAR_ROOT = os.path.join(_KB_IMG_DIR, '头像')           # 原版头像根目录
# 原版素材有两种历史布局，**两种都兼容**（用户口径 2026-09-23：素材目录被外部工具移动过）：
#   ① 平铺（当前实际）：知识库/图片素材/头像/001岛风.png、016敷波/…
#   ② 嵌套（旧版设计）：知识库/图片素材/头像/头像/001岛风.png
# 有嵌套层就用嵌套层，否则用 头像/ 本身；相对路径前缀随之切换，保证 /avatar/ 路由能命中文件。
_AVATAR_NESTED = os.path.join(AVATAR_ROOT, '头像')
AVATAR_DIR = _AVATAR_NESTED if os.path.isdir(_AVATAR_NESTED) else AVATAR_ROOT
_AVATAR_REL_PREFIX = '头像/头像/' if AVATAR_DIR == _AVATAR_NESTED else '头像/'
AVATAR_KAI_DIR = os.path.join(_KB_IMG_DIR, '头像（改）')  # 改版：与 头像/ 平级

# 昵称数据统一由 name_service 加载（唯一数据源；原 NICK_FILE/_nicknames 已合并入 name_service）
_INDEX = None  # {规范化名: 相对路径}
_INDEX_MTIME = None


def _norm_name(n: str) -> str:
    """头像文件名 → 规范化名：去编号前缀、去扩展名、间隔号归一（·→空，如 朱利奥·凯撒→朱利奥凯撒）。
    括号代号去括号保留内部字母数字（(CV-16)→CV16、(DD-459)→DD459），使 列克星敦(CV-16) 能匹配 113列克星敦CV16.png。"""
    n = os.path.splitext(n)[0]
    n = re.sub(r'^\d+', '', n).strip()
    n = n.replace('·', '').replace('‧', '').replace('．', '').replace('.', '').replace(' ', '')
    # 括号代号：(字母+数字+可选-) 去括号并去掉内部短横，如 列克星敦(CV-16)→列克星敦CV16
    n = re.sub(r'[（(]([A-Za-z0-9\-]+)[）)]', lambda m: m.group(1).replace('-', ''), n)
    return n


def _build_index() -> dict:
    """扫描素材库构建 {规范化名: 相对路径}。带 mtime 缓存，素材更新后自动重建。"""
    global _INDEX, _INDEX_MTIME
    cur = 0.0
    for d in (AVATAR_DIR, AVATAR_KAI_DIR):
        try:
            cur += os.path.getmtime(d)
        except OSError:
            pass
    if _INDEX is not None and _INDEX_MTIME == cur:
        return _INDEX
    idx = {}
    # 原版目录
    if os.path.isdir(AVATAR_DIR):
        for fn in os.listdir(AVATAR_DIR):
            p = os.path.join(AVATAR_DIR, fn)
            if os.path.isfile(p) and fn.lower().endswith('.png'):
                idx.setdefault(_norm_name(fn), _AVATAR_REL_PREFIX + fn)
            elif os.path.isdir(p):
                # 子目录头像（敷波/沃克兰/Z21 等）：取目录内 A01.png 或唯一 png
                inner = [f for f in os.listdir(p) if f.lower().endswith('.png')]
                if inner:
                    inner.sort()
                    pick = next((f for f in inner if re.match(r'^A0*1', f, re.I)), inner[0])
                    idx.setdefault(_norm_name(fn), _AVATAR_REL_PREFIX + fn + '/' + pick)
    # 改版目录
    if os.path.isdir(AVATAR_KAI_DIR):
        for fn in os.listdir(AVATAR_KAI_DIR):
            p = os.path.join(AVATAR_KAI_DIR, fn)
            if os.path.isfile(p) and fn.lower().endswith('.png'):
                idx.setdefault(_norm_name(fn), '头像（改）/' + fn)
    _INDEX, _INDEX_MTIME = idx, cur
    return idx


def find_avatar(ship_name: str) -> str:
    """按角色名（正规名/昵称/含括号编号）查找头像相对路径。
    匹配顺序：①精确规范化名 → ②昵称别名（优先，区分 大拉菲/小拉菲/小拉菲改 等
    同编号不同形态）→ ③·改→改版 → ④去括号编号（兜底）。
    找不到返回 ''（前端不输出头像）。"""
    if not ship_name or not ship_name.strip():
        return ''
    from core.name_service import load_nicknames
    idx = _build_index()
    name = ship_name.strip()
    # ① 精确（规范化：间隔号归一）
    nn = _norm_name(name)
    if nn in idx:
        return idx[nn]
    # ② 昵称别名优先（拉菲(DD-459) 昵称 大拉菲 → 121大拉菲.png；
    # 拉菲·改(DD-724) 昵称 小拉菲改 → 小拉菲改.png）
    for nick in load_nicknames().get(name, []) or []:
        nick_n = _norm_name(nick)
        if nick_n in idx:
            return idx[nick_n]
    # ③ ·改 → 改版头像（夕立·改 → 夕立改；再回退原版 夕立）
    if name.endswith('·改'):
        kai_n = _norm_name(name[:-2] + '改')
        if kai_n in idx:
            return idx[kai_n]
        plain_n = _norm_name(name[:-2])
        if plain_n in idx:
            return idx[plain_n]
    # ④ 去括号编号（拉菲(DD-724) → 拉菲）
    base = re.sub(r'[（(].*?[）)]', '', name).strip()
    base_n = _norm_name(base)
    if base_n and base_n in idx:
        return idx[base_n]
    return ''


def find_avatar_for_app(app, ship_name: str) -> str:
    """优先用 app 角色归一化（昵称→正规名）后匹配，兜底直接匹配。"""
    p = find_avatar(ship_name)
    if p:
        return p
    # app 归一化（如 雪风改 → 丹阳、口罩拉菲 → 前卫）
    try:
        matched, _ = app.get_raw_ship(ship_name) if app else (None, None)
        if matched and matched != ship_name:
            return find_avatar(matched)
    except Exception:
        pass
    return ''

