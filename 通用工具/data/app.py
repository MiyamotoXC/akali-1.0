# -*- coding: utf-8 -*-
"""
App 数据类：加载 data.json / raw_data.json，提供角色查询与敌方构造（库，非入口）。
"""
import json
import os

from core import req_ctx, runtime
from core.combat_data import CombatData
from core.name_service import NameMatcher
from data import user_data

# 项目根目录 = 通用工具 的上级（即 阿卡莉1.0 目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 知识库目录
_KB_DIR = os.path.normpath(os.path.join(_PROJECT_ROOT, "知识库"))
# 底层数据（标准计算副本 data.json 等）：源文件为人工维护表格，产物为程序读写的数据
_RAW_DIR = os.path.join(_KB_DIR, "底层数据", "产物")
DATA_FILE = os.path.join(_RAW_DIR, "data.json")
# 用户自定义角色缓存
USER_SHIPS_FILE = os.path.join(_PROJECT_ROOT, "user_ships.json")
RAW_DATA_FILE = os.path.join(_RAW_DIR, "raw_data.json")
TALENT_MAP_FILE = os.path.join(_RAW_DIR, "talent_map.json")
TALENT_PVP_FILE = os.path.join(_RAW_DIR, "talent_pvp.json")


class App:
    def __init__(self):
        self.storage = {}
        self.current_draft = None
        self.enemy = {}

        self._talent_map_cache = None
        self._ship_name_map_cache = None
        self._user_ships_cache = None
        self._user_ships_mtime = None
        self._story_cache = None
        self._story_mtime = None
        self.load_data()
        # 初始化名称匹配器（基于 data.json 中的所有正规舰名）
        self.name_matcher = NameMatcher(list(self.storage.keys()))
        # 原始信息表（用户查询展示用，不参与计算）
        self.raw_storage = {}
        self.raw_name_matcher = None
        self.load_raw_data()

    def load_data(self):
        if not os.path.exists(DATA_FILE):
            raise FileNotFoundError(f"data.json 未找到: {DATA_FILE}")
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for name, item in raw.items():
            self.storage[name] = CombatData.from_dict(item)

    def load_raw_data(self):
        """加载原始信息表 raw_data.json（用户查询展示用，不参与计算）"""
        if not os.path.exists(RAW_DATA_FILE):
            self.raw_storage = {}
            self.raw_name_matcher = None
            return
        with open(RAW_DATA_FILE, "r", encoding="utf-8") as f:
            self.raw_storage = json.load(f)
        self.raw_name_matcher = NameMatcher(list(self.raw_storage.keys()))

    def get_ship_story(self, ship_name: str):
        """查原型舰历史（原型舰历史.json，ship_name 精确 + 子串匹配）；无则 None。
        带 mtime 感知缓存：文件被外部更新后自动重读。"""
        file = os.path.join(_KB_DIR, "底层数据", "源文件", "原型舰历史.json")
        if not os.path.exists(file):
            return None
        cur = os.path.getmtime(file)
        if self._story_cache is None or self._story_mtime != cur:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._story_cache = {
                r.get("ship_name"): r
                for r in (data.get("data") or [])
                if r.get("ship_name")
            }
            self._story_mtime = cur
        if ship_name in self._story_cache:
            return self._story_cache[ship_name]
        # 子串兜底（昵称/简称/改造前后差异）
        for k, v in self._story_cache.items():
            if ship_name and (ship_name in k or k in ship_name):
                return v
        return None

    def _load_user_ships_raw(self) -> dict:
        """读取项目根目录的用户自定义角色缓存（user_ships.json），带 mtime 感知缓存。
        返回 {角色名: {attack_panel, defense_panel}}。文件被外部更新后自动重读。"""
        if not os.path.exists(USER_SHIPS_FILE):
            self._user_ships_cache = None
            return {}
        cur = os.path.getmtime(USER_SHIPS_FILE)
        if self._user_ships_cache is not None and self._user_ships_mtime == cur:
            return self._user_ships_cache
        try:
            with open(USER_SHIPS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self._user_ships_cache = d if isinstance(d, dict) else {}
        except Exception:
            self._user_ships_cache = {}
        self._user_ships_mtime = cur
        return self._user_ships_cache

    def _load_talent_map(self) -> dict:
        """读取 知识库/talent_map.json（天赋名 → {desc, v1, v2}），带模块级缓存。"""
        if self._talent_map_cache is not None:
            return self._talent_map_cache
        if not os.path.exists(TALENT_MAP_FILE):
            self._talent_map_cache = {}
            return self._talent_map_cache
        try:
            with open(TALENT_MAP_FILE, encoding="utf-8") as f:
                self._talent_map_cache = json.load(f)
        except Exception:
            self._talent_map_cache = {}
        return self._talent_map_cache

    def _load_ship_name_map(self) -> dict:
        """舰名映射（别名/昵称 → 正规名）：已合并进 ship_nicknames.json，
        由 name_service.build_hard_map 统一提供（含昵称/谐音/错别字全量硬映射）。"""
        if self._ship_name_map_cache is not None:
            return self._ship_name_map_cache
        from core.name_service import build_hard_map
        self._ship_name_map_cache = build_hard_map()
        return self._ship_name_map_cache

    def get_raw_ship(self, name):
        """从原始信息表查询：返回 (正规名, raw_data) 或 (None, None)。
        匹配顺序：①昵称/别名硬映射（ship_nicknames.json，昵称/谐音/错别字最高优先级）→
        ②精确名（含括号编号，如 拉菲(DD-459)，避免被归一化去括号误配）→
        ③X改→X·改 → ④模糊匹配（NameMatcher 子串/编辑距离）。"""
        if not name or not self.raw_name_matcher:
            return None, None
        # ① 昵称/别名硬映射：命中昵称/谐音/错别字立即返回对应角色
        # （统一由 name_service 提供；原 ship_name_map 的 雪风改→丹阳 等已并入）
        from core.name_service import build_hard_map, nick_variant
        _hard = build_hard_map()
        _n0 = name.strip()
        for _k in (_n0, nick_variant(_n0)):
            _h = _hard.get(_k)
            if _h and _h in self.raw_storage:
                return _h, self.raw_storage[_h]
        # ② 精确名优先：raw_storage 键直接命中即返回（括号编号类必须走这里，
        #    否则 _normalize 去括号会使 (DD-459)/(DD-724) 归一化冲突）
        exact = name.strip()
        if exact in self.raw_storage:
            return exact, self.raw_storage[exact]
        # ③ 「X改」→ 兜底匹配改造角色「X·改」（如 夕立改 → 夕立·改）
        if name.strip().endswith('改'):
            alt = name.strip()[:-1] + '·改'
            if alt in self.raw_storage:
                return alt, self.raw_storage[alt]
        # ④ 模糊匹配（昵称/子串/编辑距离）
        matched = self.raw_name_matcher.match(name)
        if matched and matched in self.raw_storage:
            return matched, self.raw_storage[matched]
        return None, None

    def is_ship_complete(self, name: str) -> bool:
        """判定角色信息是否完整（查询与计算唯一前置校验，全项目共用）。

        「完整」= 四块基础信息全齐（用户口径 2026-09-14：缺任何一块都不得查询与计算）：
          ① 面板（raw.panel.面板 有数值）
          ② 技能描述（skills 里有非空且非 ??? 的描述）
          ③ 普攻（raw.normal_attacks 非空）
          ④ 天赋（talents 四行二列齐全，且每格都有天赋名与效果文本）
        信息不全的角色按用户约定等同"没有该角色"：查询返回与未找到一致的提示、
        计算一律拒绝、名录/反查不收录。
        以 raw_data.json 的完整性为准（副本由 raw 生成）。
        返回 True=完整可用；False=信息不全（视同不存在）。
        特例：女灶神（维修舰）无普攻，属正常设定，允许查询与技能/必杀/支援计算，普攻忽略不计。"""
        if not name:
            return False
        matched, raw = self.get_raw_ship(name)
        if raw is None:
            return False
        # ① 面板
        panel = raw.get('panel', {}).get('面板', {}) or {}
        if not panel or not any(v for v in panel.values()):
            return False
        # ② 技能描述
        skills = raw.get('skills', {}) or {}
        has_skill = any(
            v for k, v in skills.items()
            if k != 'Name' and isinstance(v, str)
            and v.strip() and v.strip() not in ('???', '？？？')
        )
        if not has_skill:
            return False
        # ③ 普攻：无普攻 → 仅女灶神（维修舰）例外，其余视为信息不全
        if not raw.get('normal_attacks') and matched != '女灶神':
            return False
        # ④ 天赋：四行二列齐全；某格缺名或缺效果 → 天赋无法解析为数值（查询展示与伤害/索敌
        #    计算都会失真），一律视为信息不全
        talents = raw.get('talents') or {}
        if not isinstance(talents, dict) or len(talents) < 4:
            return False
        for row in talents.values():
            if not isinstance(row, dict):
                return False
            for opt in ('A', 'B'):
                cell = row.get(opt)
                tname = cell.get('name') if isinstance(cell, dict) else cell
                teffect = cell.get('effect') if isinstance(cell, dict) else ''
                if not str(tname or '').strip() or not str(teffect or '').strip():
                    return False
        return True

    def format_raw_ship_summary(self, name: str, raw: dict) -> str:
        """格式化原始信息（直接展示 raw_data.json 的原始结构，不做计算用转换）。
        排版紧凑：基本信息/面板数值各一行；不含普攻；天赋（含具体效果数值）在技能之前。
        面板固定为底层 100级 满星 满强化数据。"""
        lines = [f"\n--- {name} ---"]
        p = raw.get('panel', {})
        # 基本信息：一行紧凑列出
        meta_parts = []
        for k in ['舰名', '舰种', '所属', '稀有', '弹种', '装甲类型']:
            if p.get(k) is not None and p.get(k) != '':
                meta_parts.append(f"{k}:{p.get(k)}")
        if meta_parts:
            lines.append("  基本信息: " + " ".join(meta_parts))
        # 面板数值：一行紧凑列出（血量/炮击/雷击/航空/对空/装甲/命中/机动/索敌/幸运/航速）
        pn = dict(p.get('面板', {}) or {})
        if pn:
            order = ['血量', '炮击', '雷击', '航空', '对空', '装甲', '命中', '机动', '索敌', '幸运']
            panel_parts = [f"{k}:{pn.get(k)}" for k in order if pn.get(k) is not None and pn.get(k) != 0]
            # 航速：等级无关常量（节），旗舰技门槛会读它，附在面板行末
            if pn.get('航速') is not None and pn.get('航速') != 0:
                panel_parts.append(f"航速:{pn.get('航速')}节")
            if panel_parts:
                lines.append("  面板（100级 满星 满强化）: " + " ".join(panel_parts))
        # 天赋展示（含具体效果数值；在技能之前）——四行二列：name(效果数值)
        talents = raw.get('talents') or {}
        if talents:
            lines.append("  --- 天赋 ---")
            for row in ['1', '2', '3', '4']:
                rd = talents.get(row)
                if not rd:
                    continue
                cells = []
                for opt in ('A', 'B'):
                    od = rd.get(opt) or {}
                    # 兼容新格式 {name, effect} 与旧格式字符串
                    if isinstance(od, dict):
                        tname = (od.get('name') or '').strip()
                        effect = (od.get('effect') or '').strip()
                    else:
                        tname = (od or '').strip()
                        effect = ''
                    if not tname:
                        cells.append('-')
                        continue
                    cells.append("%s(%s)" % (tname, effect) if effect else tname)
                lines.append("    第%s行: %s | %s" % (row, cells[0], cells[1]))
        # 技能描述原文直接输出（保留■①②③【】等符号，过滤空和???）
        # 顺序：通常技 → 必杀技 → 旗舰技 → 支援技
        skills = raw.get('skills', {})
        if skills:
            lines.append("  --- 技能描述 ---")
            for key, label in [('BattleSkill1','通常技'), ('BattleSkill2','必杀技'), ('LeaderSkill','旗舰技'), ('SupportSkill','支援技')]:
                desc = skills.get(key, '')
                if desc and isinstance(desc, str):
                    s = desc.strip()
                    if s == '' or s == '???' or s == '？？？':
                        continue
                    lines.append(f"  [{label}]")
                    lines.append(f"  {desc}")
        lines.append("---------------------")
        return "\n".join(lines)

    def get_ship_from_all(self, name):
        """查询角色：精确名优先（括号编号类防归一化去括号误配），
        再走 NameMatcher（精确→归一化→别名→子串→编辑距离）。
        查询路径一律读底层 data.json（100级 满星 满强化，用户端不修改面板）。"""
        if not name:
            return None
        exact = name.strip()
        if exact in self.storage:
            return self.storage[exact]
        matched = self.name_matcher.match(name)
        if matched and matched in self.storage:
            return self.storage[matched]
        return None

    def get_ship_for_calc(self, name):
        """计算路径加载角色：底层 data.json 为基，仅覆盖用户自定义面板。
        副本历史快照为整船全量（技能段/新增字段会随底层更新而过时），
        故技能段等结构数据一律以底层为准；用户自定义只作用于 attack_panel/defense_panel。
        该面板即「用户自己的装备面板」：默认无装备=底层基础值，仅含装备属性、
        刻意不含天赋/羁绊加成（天赋由计算流程单独以会话级 buff 叠加，见 calc_flow），
        是给用户显示与计算用的面板。

        用户面板来源按运行模式（AKALI_RUN_MODE）分流：
        - local：服务器用户副本 user_data.json —— 显式 local 时行为与改造前完全一致；
        - cloud（默认）：客户端随请求携带的 panel_overrides（服务器无用户文件、无状态），
          由 /api/chat 入口写入 core.req_ctx 请求上下文（绝对赋值，覆盖同名字段）。
        """
        if not name:
            return None
        # 计算路径强制按括号编号精确命中（对齐 get_raw_ship 的 ② 护栏）：
        # 先经 get_raw_ship 解析规范名，避免 _normalize 去括号使
        # 拉菲(DD-459)/(DD-724) 等编号船被误归一化碰撞、或裸编号（拉菲459）无法解析
        _canon, _raw = self.get_raw_ship(name)
        if _canon:
            name = _canon
        base = self.get_ship_from_all(name)
        if base is None:
            return None
        # 本地模式读用户副本；云端模式不读（面板来自请求上下文）
        d = None if runtime.is_cloud() else user_data.get_ship_dict(base.name)
        ov = req_ctx.get_panel_override(base.name) or req_ctx.get_panel_override(name)
        if d is None and not ov:
            return base
        import copy as _copy
        obj = _copy.deepcopy(base)
        for panel_name in ("attack_panel", "defense_panel"):
            panel = getattr(obj, panel_name)
            # 先应用用户副本（local），再应用请求覆盖（cloud）——云端 d 为 None 时等价
            for src in ((d or {}).get(panel_name) or {}, (ov or {}).get(panel_name) or {}):
                for k, v in src.items():
                    try:
                        x = float(v)
                    except (TypeError, ValueError):
                        continue
                    if x != x:            # NaN
                        continue
                    # 用户面板覆盖统一钳制：不得为负、不超过 20000（用户口径 2026-09-17）
                    panel[k] = max(0.0, min(20000.0, x))
        return obj

    def get_panel_level(self, name):
        """「我的舰船·装备面板」里保存的**等级**（未设置返回 None）。

        与面板同源（用户口径 2026-10-03）：
        - local：用户副本 user_data.json 的 'level' 字段；
        - cloud：请求携带的 panel_overrides（前端本地缓存里存的 'level'），
          由 /api/chat 写入 core.req_ctx 请求上下文。
        用途：遭遇战加载攻击方时，按**各舰自身等级**计算 PVE 等级压制
        （见 core.encounter_simulator.load_encounter_attacker）——此前玩家等级
        是写死的常量 Lv100，与真实副本等级不符。

        合法范围钳制到 1~200（与「面板值 0~20000」同思路的护栏）；
        非法值 / 未设置一律回 None，由上层回落 encounter_simulator 的默认等级。
        """
        if not name:
            return None
        _canon, _raw = self.get_raw_ship(name)
        keys = [k for k in (_canon, name) if k]
        d = None
        if keys and not runtime.is_cloud():
            try:
                d = user_data.get_ship_dict(keys[0])
            except Exception:
                d = None
        ov = None
        for k in keys:                     # 请求覆盖优先（cloud），其次用户副本（local）
            ov = req_ctx.get_panel_override(k)
            if ov:
                break
        for src in (ov, d):
            if not isinstance(src, dict):
                continue
            v = src.get('level')
            if v in (None, ''):
                continue
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if x != x or x <= 0:           # NaN / 非正数 = 未设置（0 不可当 Lv1，会误判成重度压制）
                continue
            return max(1.0, min(200.0, x))
        return None

    def _build_enemy_from_data(self, data):
        # 减免类与点燃抵抗从防守方 buffs 读取
        # ship_type、name、ship_type_damage_reduction 供舰种专属减伤使用
        return data.to_enemy_dict()
