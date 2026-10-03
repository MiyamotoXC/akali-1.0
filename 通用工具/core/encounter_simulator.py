# -*- coding: utf-8 -*-
"""
遭遇战模拟器 - 6角色组队时间轴伤害模拟与寻优

核心流程：
1. 从 知识库/拉古兹.json 加载 boss 数据
2. 加载 6 个角色，叠加 raguz_bonus 到 buffs['全伤害增加']
3. 预计算 DPS 表（每角色 × 白天/黑夜 × 前排/后排）
4. 1s 粒度动态规划求解最优交换时机和必杀技时机
5. 输出操作时间轴和伤害明细

规则约束：
- 总时间 150s，分为白天和黑夜
- 黑夜期间 boss 装甲降为白天的 10%（**装甲减 90%**，见 `_NIGHT_ARMOR_RATIO`）。航母/轻母/装母在黑夜不"主动释放"技能/普攻；但白天已释放的航空伤害（延后1s）、必杀类伤害（延后1s，含航空类必杀，已去掉 2s 叠加）均落点于释放后的对应时刻，可落于黑夜之后。
- 前3为前排（可普攻/通常技/必杀技），后3为后排（仅支援技，不普攻）
- 普攻仅前排：切到前排时立即发射一次普攻并重置 CD 计时（按各自武器 CD）；后排不普攻
- 通常技（前排）/支援技（后排）按各自 CD 自动释放（仅普攻享受"切前排即时发射+重置"）
- 航空伤害（航母/轻母/装母 的全部攻击）与所有必杀类/支援类伤害统一延后 1s 结算（delay=1，已去掉「航母类 2s」旧机制，无叠加）；释放记 release_t，实际在 release_t+1 落点并判定 buff
- 必杀技可多次释放，必须在前排时才能释放，时机由 DP 寻优
- 交换：0-5s 禁止交换，5s 后每次交换 CD=10s（**按换人操作发起时刻计算**，与该次交换实际
  何时完成无关），无次数上限（交换不改变 10s CD）
- **交换不停摆**：交换只是「前后排实际交换成功」的时点被推迟，队伍全程照常行动
  （普攻/通常技/必杀/支援技全按各自 CD），**不存在因交换而停摆的秒**。
- 交换生效时点：发起 s → 正常 **s+1** 新前排生效（当秒 s 仍属旧前排，旧前排照常行动）。
- **通常技后摇（release-based）**：通常技释放后 2s 内换人会被「卡」——交换生效 = max(s+1, 最晚在恢复窗内的通常技释放+2)；
  多船取最晚。**必杀技释放不阻断换人**，仅通常技后摇约束交换时机。交换 CD 仍按发起时刻 s 计算。
"""
import copy
import json
import os
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from core.damage_formula import DamageCalculator
from core.output_handler import OutputHandler
from core.skill_buff_extractor import match_ship_class, _SPECIAL_TYPE_MEMBERS
from core.reload_cd import reload_reduced_cd


# boss 数据文件路径：拉古兹.json 为人工维护的源数据（只读），放 源文件/
# 优先 底层数据/源文件/拉古兹.json（v2 格式），兼容旧 知识库/拉古兹.json
# encounter_simulator.py 位于 通用工具/core/，上溯三级到项目根（阿卡莉1.0/）
_KB_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "知识库"))
_BOSS_FILE = os.path.join(_KB_DIR, "底层数据", "源文件", "拉古兹.json")
_BOSS_FILE_LEGACY = os.path.join(_KB_DIR, "拉古兹.json")

# 黑夜禁用的舰种：航母/轻母/装母 在黑夜无法发动任何普攻、通常技、必杀技、支援技（伤害全为 0）
_NIGHT_BANNED_SHIP_TYPES = {"航母", "轻母", "装母"}

# 黑夜期间 boss 装甲降为白天的该比例（0.1 = 装甲减 90%）
_NIGHT_ARMOR_RATIO = 0.1
# 文案用（随 _NIGHT_ARMOR_RATIO 自动更新，避免多处硬编码漂移）
_NIGHT_ARMOR_DESC = '减 %d%%' % int(round((1 - _NIGHT_ARMOR_RATIO) * 100))

# 开场禁止交换的时间窗口
_SWAP_OPEN_DELAY = 5
# 交换冷却时间（按「换人操作发起时刻」计算，与该次交换实际何时完成无关）
_SWAP_CD = 10
# 通常技「后摇」时长（秒）：通常技释放后的这 2s 内换人会被卡
_SKILL_RECOVERY = 2
# 交换生效推迟（秒）：正常 s+1 生效；命中通常技后摇 → s+2 才生效（不停摆，仅生效时点推迟）
_SWAP_ACTION_NORMAL = 1
_SWAP_ACTION_IN_RECOVERY = 2

# PVE 等级压制（仅遭遇战 PVE 生效）：双方每差 1 级，高等级一方对低等级一方伤害 ×(1±2%)，
# 加算累计、上限 ±50%（即等级差封顶 25 级）。玩家舰按底层「100级 满强化」基线；
# 遭遇战 boss 默认 Lv.110（用户 2026-09-21 设定），故玩家对 boss 伤害 ×0.80。
_ENCOUNTER_BOSS_LEVEL = 110
_PLAYER_SHIP_LEVEL = 100


def load_encounter_bosses() -> Dict[str, dict]:
    """加载拉古兹 boss 字典（键为 boss 名）。
    数据源 = 知识库/底层数据/拉古兹.json（遭遇战 boss 统一从此文件识别）。
    兼容多种格式：
    - 实际 v2 格式（知识库/底层数据/拉古兹.json）：顶层 enemies 数组，stats 嵌套
    - 别名：encounter_simulation.enemies
    - 旧版（知识库/拉古兹.json）：bosses 扁平数组
    """
    path = _BOSS_FILE if os.path.exists(_BOSS_FILE) else _BOSS_FILE_LEGACY
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    raw_bosses = []
    # 实际格式：顶层 enemies（拉古兹.json v2）
    if isinstance(data.get("enemies"), list):
        raw_bosses = data["enemies"]
    elif isinstance(data.get("bosses"), list):
        raw_bosses = data["bosses"]  # 旧格式
    sim = data.get("encounter_simulation") or {}
    if isinstance(sim.get("enemies"), list):
        raw_bosses = sim["enemies"]  # 兼容别名
    result = {}
    for b in raw_bosses:
        if not isinstance(b, dict) or not b.get("name"):
            continue
        # v2：stats 嵌套 → 展平
        stats = b.get("stats") or {}
        flat = dict(b)
        flat["armor"] = float(b.get("armor", stats.get("armor", 0)) or 0)
        flat["anti_air"] = float(b.get("anti_air", stats.get("anti_air", 0)) or 0)
        flat["evasion"] = float(b.get("evasion", stats.get("evasion", 0)) or 0)
        flat["luck"] = float(b.get("luck", stats.get("luck", 0)) or 0)
        flat["level"] = float(b.get("level", _ENCOUNTER_BOSS_LEVEL) or _ENCOUNTER_BOSS_LEVEL)
        result[b["name"]] = flat
    return result


def resolve_encounter_boss(bosses: Dict[str, dict], boss_name: str):
    """从加载的 boss 字典中识别用户输入的 boss 名。
    先精确匹配，未命中再用「包含/被包含」兜底（输入可能含空格、别名、前后缀），
    返回命中的标准 boss 名；都不命中返回 None。"""
    if not boss_name:
        return None
    if boss_name in bosses:
        return boss_name
    for k in bosses:
        if boss_name in k or k in boss_name:
            return k
    return None


def build_encounter_enemy(boss: dict, is_day: bool, enemy_aura: dict = None) -> dict:
    """从 boss 数据构造 enemy 字典（结构对齐 _build_enemy_from_data）。
    黑夜时装甲降为白天的 10%（装甲减 90%）；减免/防暴击/回避等字段从 boss 读取（默认 0），
    兼容 PVP 防守方（含默认天赋减免）作为 boss 传入。

    boss 两种形态都支持：
    - 拉古兹 boss 原始数据（英文键 armor/anti_air/evasion/luck）
    - **PVP 防守方**：已是 _build_enemy_from_data 产出的中文键 enemy 字典
      （装甲/对空/机动/幸运/装甲类型/各类减免）。此前只按英文键读取，会把 PVP 防守方的
      装甲/对空/机动/幸运 全部清 0、装甲类型退回"中甲"，导致 PVP 伤害严重高估、
      敌方减伤天赋与旗舰 enemy 光环全部失效——故中文键 dict 一律直接透传。

    enemy_aura：旗舰技中 target=enemy 的光环减益（如埃塞克斯「敌方单位【对空-20%】」），
    按 _ENEMY_BUFF_APPLY 白名单叠加到敌方面板（键不在白名单内表示只影响敌方输出，
    不影响我方伤害，忽略）。"""
    if "装甲" in boss:      # PVP 防守方：已是 enemy 字典，直接透传
        view = dict(boss)
        if not is_day:
            view["装甲"] = float(view.get("装甲", 0) or 0) * _NIGHT_ARMOR_RATIO
        if enemy_aura:
            view = _apply_enemy_buffs(view, [enemy_aura])
        return view
    armor = float(boss.get("armor", 0))
    if not is_day:
        armor = armor * _NIGHT_ARMOR_RATIO
    view = {
        "装甲": armor,
        "对空": float(boss.get("anti_air", 0)),
        "机动": float(boss.get("evasion", 0)),
        "幸运": float(boss.get("luck", 0)),
        "索敌": 0.0,
        "防暴击率": float(boss.get("防暴击率", 0) or 0),
        "回避率": float(boss.get("回避率", 0) or 0),
        "装甲类型": boss.get("armor_type", "中甲"),
        "全伤害减免": float(boss.get("全伤害减免", 0) or 0),
        "炮击伤害减免": float(boss.get("炮击伤害减免", 0) or 0),
        "雷击伤害减免": float(boss.get("雷击伤害减免", 0) or 0),
        "航空伤害减免": float(boss.get("航空伤害减免", 0) or 0),
        "轰炸伤害减免": float(boss.get("轰炸伤害减免", 0) or 0),
        "鱼雷伤害减免": float(boss.get("鱼雷伤害减免", 0) or 0),
        "近战伤害减免": float(boss.get("近战伤害减免", 0) or 0),
        "小型攻击伤害减免": float(boss.get("小型攻击伤害减免", 0) or 0),
        "中型攻击伤害减免": float(boss.get("中型攻击伤害减免", 0) or 0),
        "大型攻击伤害减免": float(boss.get("大型攻击伤害减免", 0) or 0),
        "点燃抵抗": float(boss.get("点燃抵抗", 0) or 0),
        "ship_type": boss.get("ship_type", ""),
        "name": boss.get("name", ""),
        "ship_type_damage_reduction": boss.get("ship_type_damage_reduction") or {},
        "level": float(boss.get("level", _ENCOUNTER_BOSS_LEVEL) or _ENCOUNTER_BOSS_LEVEL),
    }
    # 注：PVE 等级压制不在此处注入——按用户 2026-09-21 口径，压制与「增伤区」加算，
    # 由 load_encounter_attacker 以「负增伤」形式加进攻击方 全伤害增加，避免与减伤区乘算
    # 导致与其他增伤 buff（raguz_bonus/旗舰光环等）叠加结果偏差。此处仅透传 boss 面板。
    if enemy_aura:
        # _apply_enemy_buffs 定义在下方，这里直接调用（模块级函数，导入期无关）
        view = _apply_enemy_buffs(view, [enemy_aura])
    return view


def load_encounter_attacker(app, ship_name: str, boss_level: float = _ENCOUNTER_BOSS_LEVEL):
    """加载角色并叠加 raguz_bonus 与 PVE 等级压制罚项到 buffs['全伤害增加']。
    遭遇战 boss 默认属于拉古兹阵营，故 raguz_bonus 一律生效。
    PVE 等级压制（仅遭遇战 PVE）：玩家对 boss（默认 Lv110）伤害按等级差乘算，
    罚项 = _level_suppress_factor(玩家, boss) − 1（玩家 Lv100 / boss Lv110 → −0.20），作为「负增伤」
    加进全伤害增加——即与增伤区加算（用户 2026-09-21 口径：压制乘区与增伤区加算，而非独立乘区乘算）。

    玩家等级来源（用户口径 2026-10-03）：**该舰在「我的舰船·装备面板」里填的等级**
    （app.get_panel_level，云端=请求面板覆盖 / 本地=用户副本）；没填过则回落
    _PLAYER_SHIP_LEVEL（底层基线 Lv100）。等级同时挂到 attacker.player_level，
    供输出行 [PVE等级压制] 显示真实等级（不再固定打印 Lv100）。

    角色面板以用户副本（user_data.json）为基础：get_ship_for_calc 副本优先、缺失回退底层。"""
    attacker = app.get_ship_for_calc(ship_name)
    if attacker is None:
        return None
    attacker = copy.deepcopy(attacker)
    raguz_bonus = float(getattr(attacker, "raguz_bonus", 0.0) or 0.0)
    if raguz_bonus > 0:
        cur = float(attacker.buffs.get("全伤害增加", 0.0) or 0.0)
        attacker.buffs["全伤害增加"] = cur + raguz_bonus
    # 玩家等级：面板里填过就用它，否则回溯默认 Lv100（get_panel_level 失败一律回落，不影响算）
    _lvl = None
    try:
        _lvl = app.get_panel_level(ship_name)
    except Exception:
        _lvl = None
    _lvl = float(_lvl) if _lvl else float(_PLAYER_SHIP_LEVEL)
    try:
        attacker.player_level = _lvl
    except Exception:
        pass
    # PVE 等级压制：玩家对 boss 伤害惩罚（负增伤），与增伤区加算（非独立乘区乘算）
    _supp = _level_suppress_factor(_lvl, float(boss_level or _ENCOUNTER_BOSS_LEVEL))
    if _supp != 1.0:
        cur = float(attacker.buffs.get("全伤害增加", 0.0) or 0.0)
        attacker.buffs["全伤害增加"] = cur + (_supp - 1.0)
    return attacker


def _flagship_aura_entries(flagship) -> List[dict]:
    """取旗舰的光环条目列表，兼容结构化列表（新）与扁平 dict（旧）。"""
    fb = getattr(flagship, "flagship_buffs", None)
    if isinstance(fb, list):
        return [x for x in fb if isinstance(x, dict) and x.get("buffs")]
    if isinstance(fb, dict) and fb:
        return [{"target": "team", "scope": [], "buffs": fb}]
    return []


def _count_matching(attackers, filters, exclude_idx=None) -> int:
    """统计队伍中满足过滤条件的舰灵数量（「存在 N 名…」类条件用）。"""
    n = 0
    for j, a in enumerate(attackers):
        if exclude_idx is not None and j == exclude_idx:
            continue
        if _filters_match(a, filters):
            n += 1
    return n


def _aura_condition_met(cond: dict, attackers: List, flagship, ctx: dict) -> bool:
    """判定旗舰光环的触发条件是否成立。

    支持：exists（队伍中存在 N 名满足门槛的舰灵，含幸运≥X/航速≤X）、
          team_count（队伍人数）、hp（旗舰自身血量）、formation（阵型）、
          mode（pvp=演习/竞技）。未知 kind 视为成立（不静默吞掉光环）。
    与时段相关的 period 条件不由本函数处理（交给 apply_flagship_aura 分派）。
    """
    if not cond or not isinstance(cond, dict):
        return True
    kind = cond.get("kind")
    if kind == "exists":
        try:
            need = int(cond.get("count", 1) or 1)
        except (ValueError, TypeError):
            need = 1
        exc = None
        return _count_matching(attackers, cond.get("of") or [], exc) >= need
    if kind == "team_count":
        n = len(attackers)
        op = cond.get("op")
        if op == "between":
            try:
                return int(cond.get("min", 0)) <= n <= int(cond.get("max", 99))
            except (ValueError, TypeError):
                return True
        return _cmp_op(float(n), op or "=", cond.get("value"))
    if kind == "hp":
        # 玩家侧无实时血量追踪 → 按满血（1.0）判定开场状态
        ratio = 1.0
        hp_now = (ctx or {}).get("flagship_hp_ratio")
        if hp_now is not None:
            try:
                ratio = float(hp_now)
            except (ValueError, TypeError):
                ratio = 1.0
        return _cmp_op(ratio, cond.get("op") or ">", cond.get("value"))
    if kind == "formation":
        return (ctx or {}).get("formation") == cond.get("value")
    if kind == "mode":
        if cond.get("value") == "pvp":
            return bool((ctx or {}).get("is_pvp"))
        return True
    return True


def apply_flagship_aura(attackers: List, flagship_name: str, ctx: dict = None,
                        phase: str = "all") -> dict:
    """旗舰光环广播：被选为旗舰的角色，其旗舰技光环按范围/条件生效。

    结构（skill_buff_extractor.extract_flagship_auras 产出）：
        [{"target": self|team|enemy, "scope": [...], "condition": {...}|None,
          "buffs": {键: 值}}] 或带 "scaling"（「每多存在1名X，额外…（最高N%）」）

    处理口径：
    - target=team：按 scope 过滤（国籍/舰种/舰级/角色名/航速门槛/幸运门槛/特型驱逐）
      命中者才获得；target=self 仅旗舰自身；target=enemy 归入 enemy 结果（敌方减益）
    - condition 不满足 → 整条不生效（如「存在幸运≥30的舰灵时」队内无人达标则跳过）
    - scaling：按 (达标数 - base) × step 叠加，总幅度封顶 cap
    - period（昼战/夜战）条件：不在此处叠加，写入每船 flagship_periodic 由
      build_dps_table 按昼夜状态取用，避免白天吃到夜战光环（或反之）

    返回 dict：{"team": {...已叠加到全队的聚合}, "enemy": {...},
                "periodic": {"day": {...}, "night": {...}}, "skipped": [...]}
    """
    result: dict = {"team": {}, "enemy": {}, "periodic": {"day": {}, "night": {}},
                    "skipped": []}
    if not flagship_name:
        return result
    flagship = None
    f_idx = None
    for i, atk in enumerate(attackers):
        if atk.name == flagship_name:
            flagship = atk
            f_idx = i
            break
    if flagship is None:
        return result

    ctx = ctx or {}
    for entry in _flagship_aura_entries(flagship):
        target = entry.get("target") or "team"
        cond = entry.get("condition")
        scope = entry.get("scope") or []
        buffs = entry.get("buffs") or {}
        scaling = entry.get("scaling")

        # 缩放量：按达标人数叠加，封顶
        if scaling is not None:
            try:
                step = float(scaling.get("step", 0) or 0)
            except (ValueError, TypeError):
                continue
            try:
                base = int(scaling.get("base", 0) or 0)
            except (ValueError, TypeError):
                base = 0
            n = _count_matching(attackers, scaling.get("of") or [],
                                f_idx if entry.get("exclude_self") else None)
            total = step * max(0, n - base)
            cap = scaling.get("cap")
            if cap is not None:
                try:
                    cap = abs(float(cap))
                    total = max(-cap, min(cap, total)) if total >= 0 else max(-cap, total)
                except (ValueError, TypeError):
                    pass
            if not total:
                result["skipped"].append(dict(entry, reason="scaling_zero"))
                continue
            buffs = {k: total if v == step else v for k, v in buffs.items()}

        is_period = isinstance(cond, dict) and cond.get("kind") == "period"
        period = "day" if (is_period and cond.get("value") != "night") else "night"

        # 计算光环去向 sink（落实旗舰作用域约定）：
        #   "skip"        整条跳过（night_only 下的昼条件）
        #   "immediate"   即时改 a.buffs（常驻 / PVP 连续战斗）
        #   "store_day"   写入旗舰昼战 store（all 模式昼条件）
        #   "store_night" 写入旗舰夜战 store（all 模式夜条件 / night_only 常驻+夜条件）
        if is_period:
            if phase == "night_only" and period == "day":
                sink = "skip"
            elif phase == "pvp":
                sink = "immediate"
            else:  # all
                sink = "store_day" if period == "day" else "store_night"
        else:  # 常驻（无 period 条件）
            sink = "night_store" if phase == "night_only" else "immediate"

        # 时段条件已在上面按 phase 路由；其余条件（exists/team_count/hp/formation/mode）仍判定
        if not is_period:
            if not _aura_condition_met(cond, attackers, flagship, ctx):
                result["skipped"].append(dict(entry, reason="condition"))
                continue

        if sink == "skip":
            result["skipped"].append(dict(entry, reason="night_only_day"))
            continue

        if target == "enemy":
            if sink == "immediate":
                _merge_aura(result["enemy"], buffs)          # 常驻/PVP：全时段生效
            elif sink == "night_store":
                _merge_aura(result["periodic"].setdefault("enemy_night", {}), buffs)
            else:  # store_day / store_night（all 模式昼/夜条件）
                _merge_aura(result["periodic"].setdefault("enemy_" + period, {}), buffs)
            continue

        # team / self：按 sink 落地
        hit_any = False
        for j, a in enumerate(attackers):
            if not _aura_target_match(a, j, f_idx, target, scope, entry):
                continue
            if sink == "immediate":
                for k, v in buffs.items():
                    try:
                        a.buffs[k] = float(a.buffs.get(k, 0.0) or 0.0) + float(v)
                    except (ValueError, TypeError):
                        continue
            else:  # 写入旗舰时段 store（store_day / store_night / night_store）
                store = getattr(a, "flagship_periodic", None)
                if not isinstance(store, dict):
                    store = {"day": {}, "night": {}}
                    a.flagship_periodic = store
                _dst = store.setdefault(period, {})
                _merge_aura(_dst, buffs)
                _merge_aura(result["periodic"][period], buffs)
            hit_any = True
        if hit_any:
            _merge_aura(result["team"], buffs)
        else:
            result["skipped"].append(dict(entry, reason="scope"))
    return result


def _aura_target_match(a, j: int, f_idx, target: str, scope: list, entry: dict) -> bool:
    """光环目标判定：target=self 仅旗舰；team 再按 scope 过滤；exclude_self 排除旗舰。"""
    if target == "self":
        if j != f_idx:
            return False
    if entry.get("exclude_self") and j == f_idx:
        return False
    return _filters_match(a, scope)


def _merge_aura(dst: dict, src: dict) -> None:
    """把光环 buff 累加进聚合 dict（展示用）。"""
    for k, v in (src or {}).items():
        try:
            dst[k] = round(float(dst.get(k, 0.0) or 0.0) + float(v), 4)
        except (ValueError, TypeError):
            continue


def _skill_expected(factors) -> float:
    """从单类型/混合技能因子取期望伤害。"""
    if not factors:
        return 0.0
    if factors.get("type") == "混合技能":
        return sum(
            OutputHandler.calculate_final_damage(p)["expected"]
            for p in factors.get("parts", [])
        )
    return OutputHandler.calculate_final_damage(factors)["expected"]


def _with_static_buffs(atk_dict, atk):
    """技能内条件增伤（数据层回填 static_buffs，如「如对方处于减速状态，造成：50%额外伤害」）
    注入攻击方视图：直接并入该技能本次出手的单次静态计算（加法进对应增伤区）；
    普攻不调用本函数；其余持续类 buff 仍走窗口管线正常计算。
    返回浅拷贝视图（无条件增伤时原样返回），不污染原角色对象。"""
    sb = (atk_dict or {}).get('static_buffs') if isinstance(atk_dict, dict) else None
    if not (isinstance(sb, dict) and sb):
        return atk
    view = copy.copy(atk)
    view.buffs = dict(atk.buffs)
    for k, v in sb.items():
        view.buffs[k] = view.buffs.get(k, 0.0) + float(v)
    return view


def _level_suppress_factor(attacker_level: float, defender_level: float) -> float:
    """PVE 等级压制倍率（仅遭遇战 PVE 生效）。
    规则（攻略《争锋竞技场简易百科》/《拂晓伤害计算构成》）：双方每差 1 级，高等级一方
    对低等级一方伤害 ×(1 ± 2%)，加算累计、上限 ±50%（等级差封顶 25 级）。
    返回「攻击方 → 防守方」伤害倍率：1 + (攻击方等级 − 防守方等级) × 2%，钳制到 [0.5, 1.5]。
    例：玩家 Lv100 打 Boss Lv110 → 1 + (100−110)×0.02 = 0.80（玩家伤害 ×0.80）。"""
    diff = (float(attacker_level) - float(defender_level)) * 0.02
    if diff > 0.5:
        diff = 0.5
    elif diff < -0.5:
        diff = -0.5
    return 1.0 + diff


def compute_dps(attacker, enemy, is_front: bool, course: str, air: str) -> dict:
    """计算角色在指定状态下的 DPS 分项（每秒期望伤害）。
    返回 {'normal', 'skill', 'support', 'burst_one', 'burst_cd'}。
    - normal：普攻按 CD 自动触发的每秒伤害（仅前排；切前排立即发射并重置 CD，由模拟器逐秒推演）
    - skill：通常技按 CD 自动触发的每秒伤害（仅前排）
    - support：支援技按 CD 自动触发的每秒伤害（仅后排）
    - burst_one：单次必杀技期望伤害（仅前排可放，时机由 DP 寻优）
    - burst_cd：必杀技 CD（秒）"""
    result = {"normal": 0.0, "skill": 0.0, "support": 0.0, "burst_one": 0.0, "burst_cd": 0.0,
              "normal_attacks": [], "skill_one": 0.0, "skill_cd": 0.0,
              "support_one": 0.0, "support_cd": 0.0}
    # 普攻（仅前排；切到前排立即发射一次并重置 CD，由 _simulate_encounter_buff_aware 逐秒推演）
    if is_front:
        normal_factors = DamageCalculator.calculate_normal_all(attacker, enemy, course, air)
        for f in normal_factors or []:
            cd = f.get("cd", 0)
            if cd <= 0:
                continue
            one = OutputHandler.calculate_final_damage(f)["expected"]
            result["normal"] += one / cd
            result["normal_attacks"].append({"one": one, "cd": cd})
    # 前排：通常技 + 必杀技（条件增伤仅该技能出手生效，注入视图副本）
    if is_front:
        sf = DamageCalculator.calculate_skill(
            _with_static_buffs(attacker.skill_attack, attacker), enemy, course, air)
        if sf:
            cd = float(attacker.skill_attack.get("cd", 0) or 0)
            if cd > 0:
                result["skill"] = _skill_expected(sf) / cd
                result["skill_one"] = _skill_expected(sf)
                result["skill_cd"] = cd
        bf = DamageCalculator.calculate_burst(
            _with_static_buffs(attacker.burst_attack, attacker), enemy, course, air)
        if bf:
            result["burst_one"] = _skill_expected(bf)
            result["burst_cd"] = float(attacker.burst_attack.get("cd", 0) or 0)
    # 后排：支援技
    if not is_front:
        spf = DamageCalculator.calculate_support(
            _with_static_buffs(attacker.support_attack, attacker), enemy, course, air)
        if spf:
            cd = float(attacker.support_attack.get("cd", 0) or 0)
            if cd > 0:
                result["support"] = _skill_expected(spf) / cd
                result["support_one"] = _skill_expected(spf)
                result["support_cd"] = cd
    return result


def _attacker_with_period_aura(atk, period: str):
    """返回按昼夜光环（旗舰技 period 条件）调整后的角色视图。

    白天/黑夜光环互斥，DPS 表按状态分开预计算，故此处 shallow copy 后只改 buffs，
    不污染 attackers 本体。无对应光环时直接返回原对象（零开销）。"""
    store = getattr(atk, "flagship_periodic", None)
    aura = (store or {}).get(period) if isinstance(store, dict) else None
    if not aura:
        return atk
    view = copy.copy(atk)
    view.buffs = dict(atk.buffs or {})
    for k, v in aura.items():
        try:
            view.buffs[k] = float(view.buffs.get(k, 0.0) or 0.0) + float(v)
        except (ValueError, TypeError):
            continue
    return view


def build_dps_table(attackers: List, boss: dict, day_time: float, course: str, air: str,
                    enemy_aura: dict = None,
                    enemy_periodic: dict = None) -> Dict:
    """预计算 DPS 表：dps_table[(idx, is_day, is_front)] = dps dict。
    黑夜时航母/轻母/装母 DPS 全 0。

    enemy_aura：旗舰技 target=enemy 的常驻减益；enemy_periodic：其昼夜分支
    （{"enemy_day": {...}, "enemy_night": {...}}）。"""
    table = {}
    for idx, atk in enumerate(attackers):
        for is_day in [True, False]:
            aura = dict(enemy_aura or {})
            if enemy_periodic:
                aura.update(enemy_periodic.get("enemy_day" if is_day else "enemy_night") or {})
            enemy = build_encounter_enemy(boss, is_day, aura or None)
            # 黑夜航母/轻母/装母禁用
            if not is_day and atk.ship_type in _NIGHT_BANNED_SHIP_TYPES:
                _zero = {"normal": 0.0, "skill": 0.0, "support": 0.0, "burst_one": 0.0, "burst_cd": 0.0,
                         "normal_attacks": [], "skill_one": 0.0, "skill_cd": 0.0,
                         "support_one": 0.0, "support_cd": 0.0}
                table[(idx, is_day, True)] = dict(_zero)
                table[(idx, is_day, False)] = dict(_zero)
                continue
            atk_v = _attacker_with_period_aura(atk, "day" if is_day else "night")
            table[(idx, is_day, True)] = compute_dps(atk_v, enemy, True, course, air)
            table[(idx, is_day, False)] = compute_dps(atk_v, enemy, False, course, air)
    return table


def _apply_enemy_buffs(enemy_base: dict, active_enemy_buffs: List[dict]) -> dict:
    """叠加当前生效的敌方持续减益，返回 enemy 视图（不修改原 dict）。
    仅 _ENEMY_BUFF_APPLY 中登记的键生效（与单体计算的持续 buff 规则一致）。"""
    if not active_enemy_buffs:
        return enemy_base
    view = dict(enemy_base)
    for b in active_enemy_buffs:
        for k, v in b.items():
            apply = _ENEMY_BUFF_APPLY.get(k)
            if apply is None:
                continue
            kind, attr = apply
            if kind == "percent":
                view[attr] = view.get(attr, 0.0) * (1.0 + v)
            else:
                view[attr] = view.get(attr, 0.0) + v
    return view


def _summarize_buffs(active: List[dict]) -> dict:
    """把多个生效中的 buff dict 合并为便于展示的聚合 dict。"""
    agg = {}
    for b in active:
        for k, v in b.items():
            agg[k] = agg.get(k, 0.0) + v
    return agg


def _cd_effect_targets(attackers, idx: int, scope: dict, flagship_idx) -> List[int]:
    """解析 CD 效果的目标船列表（scope 结构见 skill_buff_extractor.extract_cd_effects）。"""
    if scope.get("target") == "self":
        return [idx]
    out: List[int] = []
    fname = scope.get("flagship_name")
    for j in range(6):
        if scope.get("exclude_self") and j == idx:
            continue
        if scope.get("flagship_only") and j != flagship_idx:
            continue
        if fname and (j != flagship_idx or (attackers[j].name or "") != fname):
            continue
        ok = True
        for f in (scope.get("scope") or []):
            kind, val = f.get("kind"), f.get("value")
            if kind == "nationality":
                ok = (attackers[j].nation or "") == val
            elif kind == "ship_type":
                ok = val in (attackers[j].ship_type or "")
            elif kind == "name":
                ok = val in (attackers[j].name or "")
            elif kind == "ship_class":
                ok = match_ship_class(attackers[j].name or "", val)   # 舰级按原型舰历史表比对
            if not ok:
                break
        if ok:
            out.append(j)
    return out


def _ship_speed(a) -> float:
    """角色当前航速（底层面板常量 + 技能/光环的航速固定增减）。"""
    try:
        base = float(getattr(a, "speed", 0) or 0)
    except (ValueError, TypeError):
        base = 0.0
    try:
        return base + float((a.buffs or {}).get("航速值固定增加", 0) or 0)
    except (ValueError, TypeError):
        return base


def _ship_luck(a) -> float:
    """角色当前幸运（attack_panel 面板值）。"""
    try:
        return float((a.attack_panel or {}).get("幸运", 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def _cmp_op(value: float, op: str, target: float) -> bool:
    """数值门槛比较（op 归一为 >=,<=,>,<,=）。"""
    try:
        target = float(target)
    except (ValueError, TypeError):
        return True
    if op == ">=":
        return value >= target
    if op == "<=":
        return value <= target
    if op == ">":
        return value > target
    if op == "<":
        return value < target
    if op == "=":
        return abs(value - target) < 1e-9
    return True


def _filter_match(a, f: dict) -> bool:
    """单个范围过滤项是否命中该角色（kind 见 skill_buff_extractor.SCOPE_KIND_*）。"""
    kind = f.get("kind")
    val = f.get("value")
    if kind == "nationality":
        return bool(val) and val == (a.nation or "")
    if kind == "name":
        nm = a.name or ""
        return bool(val) and (val == nm if len(val) == 1 else val in nm)
    if kind == "name_contains":
        return bool(val) and (val in (a.name or ""))
    if kind == "ship_class":
        return bool(val) and match_ship_class(a.name or "", val)
    if kind == "ship_type":
        return bool(val) and val in (a.ship_type or "")
    if kind == "speed":
        return _cmp_op(_ship_speed(a), f.get("op") or "<=", val)
    if kind == "luck":
        return _cmp_op(_ship_luck(a), f.get("op") or ">=", val)
    if kind == "special_type":
        return bool(val) and (a.name or "") in _SPECIAL_TYPE_MEMBERS.get(val, ())
    if kind == "position":
        # 站位由调用方按前后排提供；无上下文时按"前排"通过（不拦截）
        return True
    return True   # 未知 kind 不拦截


def _filters_match(a, filters, is_front: bool = None) -> bool:
    """过滤列表整体是否命中：同 kind 多值"任一匹配"，跨 kind 取交集。"""
    if not filters:
        return True
    kinds: Dict[str, list] = {}
    for f in filters or []:
        kinds.setdefault(f.get("kind"), []).append(f)
    for kind, flist in kinds.items():
        if kind == "position":
            want = (flist[0].get("value") or "front")
            if is_front is None:
                continue
            if (want == "front") != bool(is_front):
                return False
            continue
        if not any(_filter_match(a, f) for f in flist):
            return False
    return True


def _buff_scope_targets(attackers, filters, flagship_idx=None) -> List[int]:
    """team 类持续 buff 的目标船解析（filters 结构见 skill_buff_extractor._parse_filters）。

    - 空过滤 = 全队
    - 同 kind 多值"任一匹配"，跨 kind 取交集
    - 舰级：与原型舰历史表的舰级比对（match_ship_class，本级与姊妹船都覆盖）
    - 指定角色名：单字名要求全等（避免误匹配），多字名包含即可
    - 航速/幸运门槛：与角色当前面板值比较（航速含光环与技能的固定增减）
    无人命中时由调用方回退释放者自身（避免模糊匹配把 buff 弄丢）。
    """
    if not filters:
        return list(range(len(attackers)))
    out: List[int] = []
    for j, a in enumerate(attackers):
        if _filters_match(a, filters):
            out.append(j)
    return out


def _apply_cd_effects(attackers, idx: int, slot: str, t: int,
                      skill_next, burst_set, support_next, flagship_idx) -> None:
    """技能释放后触发 CD 效果（重置/固定减秒/全槽位减秒），仅影响目标下一次对应技能的释放。

    - target=skill：重置 → skill_next 置为 t；减秒 → 下一轮释放提前 N 秒（不早于 t）。
    - target=burst：DP 时间表上 t 之后的最近一次必杀提前 N 秒（重置则到 t+1），仅一次。
    - target=all：对 scope 内每艘船的 通常技/必杀/支援 全槽位减秒 N 秒（重置则各置为 t/t+1）。
    """
    effs = getattr(attackers[idx], "cd_effects", None) or {}
    for eff in effs.get(slot, []):
        seconds = float(eff.get("seconds", 0) or 0)
        action = eff.get("action")
        targets = _cd_effect_targets(attackers, idx, eff.get("scope") or {}, flagship_idx)
        if eff.get("target") == "all":
            for j in targets:
                if action == "reset":
                    if skill_next[j] is not None:
                        skill_next[j] = t
                    ts = burst_set.get(j)
                    if ts:
                        fut = [x for x in ts if x > t]
                        if fut:
                            ts.discard(min(fut))
                            ts.add(t + 1)
                    if support_next[j] is not None:
                        support_next[j] = t
                else:
                    if skill_next[j] is not None:
                        skill_next[j] = max(t, skill_next[j] - seconds)
                    ts = burst_set.get(j)
                    if ts:
                        fut = [x for x in ts if x > t]
                        if fut:
                            s = min(fut)
                            ts.discard(s)
                            ts.add(max(t + 1, s - int(seconds)))
                    if support_next[j] is not None:
                        support_next[j] = max(t, support_next[j] - seconds)
            continue
        for j in targets:
            if eff.get("target") == "skill":
                cur = skill_next[j]
                if cur is None:
                    continue
                skill_next[j] = t if action == "reset" else max(t, cur - seconds)
            elif eff.get("target") == "burst":
                times = burst_set.get(j)
                if not times:
                    continue
                future = [x for x in times if x > t]
                if not future:
                    continue
                s = min(future)
                times.discard(s)
                times.add(t + 1 if action == "reset" else max(t + 1, s - int(seconds)))


def _simulate_encounter_buff_aware(attackers, boss, day_time, course, air,
                                   swap_schedule, burst_schedule,
                                   total_time: int = 150, all_front: bool = False,
                                   flagship_idx=None, enemy_aura: dict = None,
                                   enemy_periodic: dict = None,
                                   defender_ship=None) -> dict:
    """6 船时间轴模拟：应用技能绑定持续 buff（skill_buffs）窗口。

    - 敌方减益（target=enemy）全队共享：任意船释放对应技能后，boss 在 duration 内承受该减益，
      全队对其伤害随之变化（如 全伤害减免 -15% → 全队伤害 +15%）。
    - 自身增益（target=self）仅影响释放者自身后续伤害。
    - 使用 DP 给出的 交换/必杀 时间表作为节奏，每秒按 CD 自动触发 普攻/通常技/支援技。
    - 释放当次伤害按「释放前」的 buff 视图计算（与单体计算的持续 buff 规则一致：
      先结算本次伤害，再注册本次技能触发的持续 buff，影响窗口内后续伤害）。
    - all_front=True（PVP 模式）：6 人全按前排（普攻/通常技/必杀），无交换、无支援技、
      无递增光环（无支援位）；total_time 为模拟时长。
    - defender_ship（PVP 防守方角色对象）：防守方按自身 CD 自动释放通常技/必杀技，
      释放的 自身/全队 持续增益（如 全伤害减免 X% 持续 Ys）在结算落点注册为防守方窗口，
      生效期间叠加进敌方视图 → 攻击方各落点按防守方当时实际面板计算伤害。

    返回 {total, role_damage:{(i,bool):float}, per_release:[...], enemy_window_log:[...]}。"""
    total_time = int(total_time or 150)
    day_time = int(day_time)
    base_enemy = {}
    for _d in (True, False):
        _aura = dict(enemy_aura or {})
        if enemy_periodic:
            _aura.update(enemy_periodic.get("enemy_day" if _d else "enemy_night") or {})
        base_enemy[_d] = build_encounter_enemy(boss, _d, _aura or None)
    base_attackers = [copy.deepcopy(a) for a in attackers]

    # ---- 交换时序：发起时刻 → 生效时刻（受「通常技后摇」影响，release-based）----
    # **交换不停摆**：仅"前后排实际交换成功"的时点被推迟，队伍全程照常行动。
    # 正常：发起 s → s+1 新前排生效（当秒 s 仍属旧前排，旧前排照常行动）。
    # 命中通常技后摇：生效 = max(s+1, 最晚在恢复窗内的通常技释放+2)；必杀技释放不阻断换人。
    # 交换 CD 仍按发起时刻 s 计算（_can_swap / _valid_swap_schedule 语义不变）。
    swap_inits = sorted(set(int(x) for x in (swap_schedule or [])))
    _swap_init_set = set(swap_inits)
    swap_eff: List[int] = []        # 已解析的交换生效时刻（升序累积）
    skill_release_r = set()         # {(idx, 通常技释放秒)} —— 用于判定后摇

    def front_set_at(t):
        if all_front:
            return set(range(6))
        # 交换生效时刻 e 起新前排接管（e 之前仍属旧前排，期间旧前排照常行动）
        k = sum(1 for e in swap_eff if e <= t)
        return set([0, 1, 2]) if k % 2 == 0 else set([3, 4, 5])

    def _swap_eff_time(s, front_now):
        """s 秒发起的交换，生效时刻 = max(s+1 基础转移, 最晚在恢复窗内的通常技释放+2)。
        通常技后摇：前排船在 (s-2, s] 内释放过通常技且其恢复窗 (r, r+2) 仍覆盖发起时刻 s，
        则该次交换推迟到 r+2 生效；多船取最晚。基础转移 1s 始终成立（换人动作本身耗时），
        故生效不早于 s+1。必杀技释放不阻断换人——仅通常技后摇约束交换时机。"""
        eff = s + _SWAP_ACTION_NORMAL  # s+1
        latest = None
        for (idx, r) in skill_release_r:
            if idx in front_now and r <= s and r + _SKILL_RECOVERY > s:
                if latest is None or r > latest:
                    latest = r
        if latest is not None:
            eff = max(eff, latest + _SKILL_RECOVERY)
        return eff

    # 持续 buff 窗口：敌方减益全队共享；自身增益按船索引
    enemy_windows = []            # {buffs, start, expire}
    self_windows = [[] for _ in range(6)]

    def register(idx, trigger, t):
        for sb in (base_attackers[idx].skill_buffs or []):
            if sb.get("trigger") != trigger:
                continue
            dur = float(sb.get("duration", 0) or 0)
            if dur <= 0:
                continue
            entry = {"buffs": dict(sb.get("buffs", {})),
                     "start": float(t), "expire": float(t) + dur}
            if sb.get("target") == "enemy":
                enemy_windows.append(entry)
            elif sb.get("target") == "team":
                # 范围从句决定目标船（国籍/舰种/舰级/指定角色）；无人命中回退释放者
                ships = _buff_scope_targets(attackers, sb.get("scope") or [], flagship_idx)
                for j in (ships or [idx]):
                    self_windows[j].append(entry)
            else:
                self_windows[idx].append(entry)

    # 装填计入伤害链路：普攻 CD 按各角色面板「装填」缩减（≤25%），影响时间轴触发频率
    reload_vals = [float((getattr(atk, 'attack_panel', {}) or {}).get('装填', 0) or 0)
                   for atk in attackers]
    # ---- 普攻计时器：按"前排在场"推进；切到前排立即发射一次并重置 CD（q-1：后排不普攻）----
    normal_cds = [[reload_reduced_cd(reload_vals[idx], float(na.get("cd", 0) or 0))
                   for na in (atk.normal_attacks or [])]
                  for idx, atk in enumerate(attackers)]
    basic_next = [None] * 6
    init_front = front_set_at(0)
    for idx in range(6):
        if idx in init_front:
            basic_next[idx] = [cd if cd > 0 else None for cd in normal_cds[idx]]
        else:
            basic_next[idx] = [None] * len(normal_cds[idx])
    skill_cds = [float((atk.skill_attack or {}).get("cd", 0) or 0) for atk in attackers]
    support_cds = [float((atk.support_attack or {}).get("cd", 0) or 0) for atk in attackers]
    # 通常技独立 CD 计时：前后排全时推进；切前排时若 CD 已刷新好（next<=t）立即释放一次
    skill_next = [scd if scd > 0 else None for scd in skill_cds]
    # 支援技独立 CD 计时：全时推进；在后排且 CD 已刷新好（next<=t）即释放。
    # 交换不停摆语义：交换当秒原后排照常放完支援技（该秒仍属后排）；
    # 原前排若支援 CD 恰在交换发起秒到点，因当秒还在前排不释放，到生效时刻转后排后 next<=t 成立即补放。
    support_next = [spcd if spcd > 0 else None for spcd in support_cds]
    burst_set = {}
    for idx, ts in (burst_schedule or {}).items():
        burst_set[idx] = set(int(x) for x in ts)

    # 延后结算（统一 1s）：普攻/通常技/必杀/支援 与航空类全部攻击，释放记 release_t，
    # 实际在 release_t+1 落点并判定 buff；已去掉「航母类 2s」旧机制（delay 恒为 1）。
    aviation_types = _NIGHT_BANNED_SHIP_TYPES
    pending = []  # {idx, kind, wi, release_t}

    # 防守方技能时间轴（仅 PVP 传入 defender_ship 时启用）：防守方按自身 CD 自动释放
    # 通常技/必杀技，与攻击方同一套延迟结算规则（统一延后 1s，无航空 2s 叠加），
    # 在结算落点把 自身/全队 持续增益（如 全伤害减免 X% 持续 Ys）注册为防守方窗口；
    # 生效期间叠加进敌方视图，攻击方各落点按防守方当时实际面板计算伤害。
    defender_windows = []   # 结构同 enemy_windows（buffs/start/expire）
    def_pending = []        # 防守方释放的延后结算 {kind, release_t, delay}
    def_skill_cd = def_burst_cd = 0.0
    def_skill_next = def_burst_next = None
    def_is_aviation = False
    if defender_ship is not None:
        def_skill_cd = float((defender_ship.skill_attack or {}).get("cd", 0) or 0)
        def_burst_cd = float((defender_ship.burst_attack or {}).get("cd", 0) or 0)
        def_skill_next = def_skill_cd if def_skill_cd > 0 else None
        def_burst_next = def_burst_cd if def_burst_cd > 0 else None
        def_is_aviation = defender_ship.ship_type in aviation_types

    def register_defender(kind, t):
        """防守方技能结算落点：把该技能触发的 自身/全队 持续增益注册为防守方窗口。
        target=enemy（防守方对攻击方的减益）当前口径不进时间轴，跳过。"""
        for sb in (defender_ship.skill_buffs or []):
            if sb.get("trigger") != kind:
                continue
            dur = float(sb.get("duration", 0) or 0)
            if dur <= 0:
                continue
            if sb.get("target") == "enemy":
                continue
            defender_windows.append({"buffs": dict(sb.get("buffs", {})),
                                     "start": float(t), "expire": float(t) + dur})

    total = 0.0
    role_damage = {(i, True): 0.0 for i in range(6)}
    role_damage.update({(i, False): 0.0 for i in range(6)})
    per_release = []
    enemy_window_log = []

    def _record(kind, idx, settle_t, is_day, is_front, factors, self_active, enemy_active,
                release_t=None, delay=0):
        nonlocal total, role_damage
        d = _skill_expected(factors)
        total += d
        role_damage[(idx, is_day)] += d
        rt = settle_t if release_t is None else release_t
        per_release.append({
            "t": rt,                         # 释放时刻（轴排序/显示用）
            "settle_t": settle_t,            # 实际结算时刻（release_t + delay）
            "delay": delay,                  # 延后秒数（0/1/2）
            "idx": idx, "name": base_attackers[idx].name,
            "ship_type": base_attackers[idx].ship_type,
            "kind": kind, "is_day": is_day, "is_front": is_front,
            "damage": d, "factors": factors,
            "active_self": _summarize_buffs(self_active),
            "active_enemy": _summarize_buffs(enemy_active),
        })

    def _compute_factors(kind, idx, wi, view, enemy_view):
        if kind == "normal":
            norm = DamageCalculator.calculate_normal_all(view, enemy_view, course, air)
            return norm[wi] if wi < len(norm) else None
        # 条件增伤仅该技能出手结算生效（普攻不吃；视图可能是原对象，助手内部做浅拷贝）
        atk_dict = {"skill": base_attackers[idx].skill_attack,
                    "burst": base_attackers[idx].burst_attack,
                    "support": base_attackers[idx].support_attack}.get(kind)
        view = _with_static_buffs(atk_dict, view)
        if kind == "skill":
            return DamageCalculator.calculate_skill(view, enemy_view, course, air)
        if kind == "support":
            return DamageCalculator.calculate_support(view, enemy_view, course, air)
        if kind == "burst":
            return DamageCalculator.calculate_burst(view, enemy_view, course, air)
        return None

    # 当前昼夜（供 _make_view 取用旗舰技 period 光环；白天/黑夜互斥）
    cur_period = ["day"]

    def _make_view(idx, self_active):
        atk = base_attackers[idx]
        # 旗舰技的昼战/夜战专属光环：按当前时段叠加，避免白天吃到夜战光环（或反之）
        per_add = (getattr(atk, "flagship_periodic", None) or {}).get(cur_period[0]) or {}
        if self_active or ramp_add or per_add:
            view = copy.deepcopy(atk)
            for b in self_active:
                for k, v in b.items():
                    view.buffs[k] = view.buffs.get(k, 0.0) + v
            for k, v in ramp_add.items():
                view.buffs[k] = view.buffs.get(k, 0.0) + v
            for k, v in per_add.items():
                view.buffs[k] = view.buffs.get(k, 0.0) + v
        else:
            view = atk
        return view

    # 递增光环（ramp_buffs，如支援技「航空值每5秒+5%，最多+80%」）：
    # 持有者处于后排（支援位）且白天时对全队生效，每 interval 秒 +step，封顶 cap 后不变。
    # 累计按"后排在场时间"计算：上前排/夜间期间累计暂停，回后排后从原累计值继续。
    ramp_acc = [{} for _ in range(6)]   # idx → {attr: 后排累计秒数}
    ramp_add = {}

    prev_front = init_front
    for t in range(total_time):
        is_day = t < day_time
        cur_period[0] = "day" if is_day else "night"
        # 交换：t 秒发起的交换此刻解析生效时刻（正常 s+1；命中通常技后摇 → 最晚释放+2）。
        # **交换不停摆**——只登记 swap_eff；发起当秒与推迟期间前排照常行动，不做任何停摆。
        if t in _swap_init_set:
            swap_eff.append(_swap_eff_time(t, front_set_at(t)))
        front_set = front_set_at(t)

        # 每秒重算递增光环：仅白天且持有者在后排（支援位）时，累计 +1s 并叠加到全队。
        # t=0 为战斗开始瞬间不累计（第 1 秒末才满 1s），保证每 5 秒 +step 从第 5 秒起生效
        ramp_add = {}
        if is_day and t > 0:
            for idx in range(6):
                if idx in front_set:
                    continue
                for rb in (base_attackers[idx].ramp_buffs or []):
                    key = rb.get("attr", "航空值百分比增加")
                    acc = ramp_acc[idx].get(key, 0.0) + 1.0   # 后排每过 1 秒累计 1
                    ramp_acc[idx][key] = acc
                    val = min(float(rb.get("step", 0) or 0) * (acc // int(rb.get("interval", 5) or 5)),
                              float(rb.get("cap", 1.0) or 1.0))
                    if val > 0:
                        ramp_add[key] = ramp_add.get(key, 0.0) + val

        # 剪枝到期窗口
        enemy_windows = [w for w in enemy_windows if w["expire"] > t]
        defender_windows = [w for w in defender_windows if w["expire"] > t]
        for idx in range(6):
            self_windows[idx] = [w for w in self_windows[idx] if w["expire"] > t]

        # 敌方视图 = 基础面板 + 攻击方减益窗口 + 防守方自身增益窗口（PVP）
        enemy_active = [w["buffs"] for w in enemy_windows + defender_windows
                        if w["start"] <= t < w["expire"]]
        enemy_view = _apply_enemy_buffs(base_enemy[is_day], enemy_active)

        # 1) 结算延期释放、本秒落点的延后伤害（release_t+delay == t；已释放的照常落点，不受"夜间禁释放"影响）
        if pending:
            still = []
            for p in pending:
                if p["release_t"] + p.get("delay", 1) == t:
                    idx = p["idx"]
                    self_active = [w["buffs"] for w in self_windows[idx] if w["start"] <= t < w["expire"]]
                    view = _make_view(idx, self_active)
                    f = _compute_factors(p["kind"], idx, p.get("wi"), view, enemy_view)
                    if f:
                        settle_is_front = idx in front_set_at(t)
                        _record(p["kind"], idx, t, is_day, settle_is_front,
                                f, self_active, enemy_active,
                                release_t=p["release_t"], delay=p.get("delay", 1))
                        if p["kind"] != "normal":
                            register(idx, p["kind"], t)
                else:
                    still.append(p)
            pending = still

        # 1.5) 防守方技能时间轴（PVP）：按 CD 释放通常技/必杀技，延后结算落点注册增益窗口。
        # 注册进 defender_windows 后从下一秒起进入敌方视图（与攻击方 enemy 窗口同一 sink 时序）。
        if defender_ship is not None:
            if def_skill_next is not None and def_skill_next <= t:
                def_pending.append({"kind": "skill", "release_t": t, "delay": 1})
                def_skill_next = t + def_skill_cd
            if def_burst_next is not None and def_burst_next <= t:
                def_pending.append({"kind": "burst", "release_t": t, "delay": 1})
                def_burst_next = t + def_burst_cd
            _still_def = []
            for p in def_pending:
                if p["release_t"] + p["delay"] == t:
                    register_defender(p["kind"], t)
                else:
                    _still_def.append(p)
            def_pending = _still_def

        # 2) 交换导致的前后排切换：切到前排立即发射一次普攻并重置 CD；切到后排清掉普攻计时
        if front_set != prev_front:
            for idx in range(6):
                if idx in front_set and idx not in prev_front:
                    basic_next[idx] = [t if cd > 0 else None for cd in normal_cds[idx]]
                elif idx in prev_front and idx not in front_set:
                    basic_next[idx] = [None] * len(normal_cds[idx])
            prev_front = front_set

        for idx in range(6):
            atk = base_attackers[idx]
            is_front = idx in front_set
            is_aviation = atk.ship_type in aviation_types
            # 夜间：航母/轻母/装母 不在夜间主动释放（已释放航空伤害在步骤1照常落点）
            release_allowed = not ((not is_day) and is_aviation)
            self_active = [w["buffs"] for w in self_windows[idx] if w["start"] <= t < w["expire"]]
            view = _make_view(idx, self_active)

            # 普攻：仅前排；切前排时已置 basic_next=t 立即发射，之后按 CD 续推
            # 结算规则：普攻（通常）与必杀类一致，统一延后 1s 落点（航空普攻本就延后 1s）
            # 交换不停摆：交换当秒前排照常发射（旧前排仍在场，直到生效时刻才换下）
            if is_front:
                norm_factors_all = None
                for wi in range(len(normal_cds[idx])):
                    cd = normal_cds[idx][wi]
                    if cd > 0 and basic_next[idx][wi] is not None and basic_next[idx][wi] == t:
                        if norm_factors_all is None:
                            norm_factors_all = DamageCalculator.calculate_normal_all(view, enemy_view, course, air)
                        if wi < len(norm_factors_all):
                            f = norm_factors_all[wi]
                            if not is_aviation or release_allowed:
                                pending.append({"idx": idx, "kind": "normal", "wi": wi,
                                                "release_t": t, "delay": 1})
                        basic_next[idx][wi] = t + cd

            # 通常技（仅前排）：独立 CD 计时（全时推进）；切前排时若 CD 已刷新好（next<=t）
            # 立即释放一次；释放统一延后 1s 结算（与普攻一致，buff 在结算落点注册）
            # 交换不停摆：交换当秒前排照常释放通常技
            scd = skill_cds[idx]
            if is_front and scd > 0 and skill_next[idx] is not None and skill_next[idx] <= t:
                f = _compute_factors("skill", idx, None, view, enemy_view)
                if f:
                    if not is_aviation or release_allowed:
                        pending.append({"idx": idx, "kind": "skill", "wi": None,
                                        "release_t": t, "delay": 1})
                        # 记录通常技释放时刻 → 之后 2s 内为「后摇」，期间发起的交换推迟到 s+2 生效。
                        # 仅造成伤害的通常技才有后摇；纯增益（伤害为 0）技不卡换人。
                        if _skill_expected(f) > 0:
                            skill_release_r.add((idx, t))
                skill_next[idx] = t + scd
                # CD 效果（重置/固定减秒）：本技能释放后触发
                _apply_cd_effects(base_attackers, idx, "skill", t, skill_next, burst_set, support_next, flagship_idx)

            # 支援技（仅后排，按 CD 计时）：全时推进；在后排且 CD 已刷新好（next<=t）即释放。
            # 交换不停摆：交换当秒原后排照常放完（该秒仍属后排）；原前排若 CD 恰在交换发起秒到点，
            # 当秒仍在前排不释放，到生效时刻转后排时 next<=t 成立即补放（如 14s 发起→15s 到后排释放）。
            # 注意：只在前排期间不释放但 CD 继续走（next 保持），回后排即补放。
            spcd = support_cds[idx]
            if (not is_front) and spcd > 0 and support_next[idx] is not None and support_next[idx] <= t:
                f = _compute_factors("support", idx, None, view, enemy_view)
                if f and release_allowed:   # 夜间航母/轻母/装母 不主动释放；其余支援伤害统一延后 1s 结算
                    pending.append({"idx": idx, "kind": "support", "wi": None, "release_t": t, "delay": 1})
                support_next[idx] = t + spcd
                # CD 效果（重置/固定减秒）：本技能释放后触发
                _apply_cd_effects(base_attackers, idx, "support", t, skill_next, burst_set, support_next, flagship_idx)

            # 必杀技（仅前排，按 DP 时间表）：所有伤害统一延后 1s 结算（含航空类必杀，
            # 已去掉「航母类伤害延后 2s」的旧机制，不再叠加）。
            # 交换不停摆：交换当秒前排照常按时间表释放必杀技；必杀释放不阻断换人。
            if is_front and idx in burst_set and t in burst_set[idx]:
                if release_allowed:
                    pending.append({"idx": idx, "kind": "burst", "wi": None,
                                    "release_t": t, "delay": 1})
                    # CD 效果（重置/固定减秒）：本技能释放后触发
                    _apply_cd_effects(base_attackers, idx, "burst", t, skill_next, burst_set, support_next, flagship_idx)

    # 收尾：结算因延迟而落在主循环外（release_t+delay == total_time）的延迟伤害
    if pending:
        t = total_time
        is_day = t < day_time
        front_set = front_set_at(t)
        enemy_active = [w["buffs"] for w in enemy_windows + defender_windows
                        if w["start"] <= t < w["expire"]]
        enemy_view = _apply_enemy_buffs(base_enemy[is_day], enemy_active)
        still = []
        for p in pending:
            if (p["release_t"] + p.get("delay", 1)) != total_time:
                still.append(p)   # 真正超出时间轴的（如航空必杀@119 +2s=121）丢弃
                continue
            idx = p["idx"]
            self_active = [w["buffs"] for w in self_windows[idx] if w["start"] <= t < w["expire"]]
            view = _make_view(idx, self_active)
            f = _compute_factors(p["kind"], idx, p.get("wi"), view, enemy_view)
            if f:
                settle_is_front = idx in front_set
                _record(p["kind"], idx, t, is_day, settle_is_front,
                        f, self_active, enemy_active,
                        release_t=p["release_t"], delay=p.get("delay", 1))
                if p["kind"] != "normal":
                    register(idx, p["kind"], t)
        pending = still

    # 敌方减益窗口日志（供详情与说明展示）
    for w in enemy_windows:
        enemy_window_log.append({"buffs": w["buffs"], "start": w["start"], "expire": w["expire"]})

    # 交换时序日志：init=发起时刻（CD 记时点），eff=集结生效时刻（release-based：max(init+1, 最晚通常技释放+2)），
    # delayed=True 表示命中「通常技后摇」→ 生效晚于基础 1s（整数秒离散下多数情形 eff-init==1，不额外延迟）
    swap_log = [{"init": s, "eff": e, "dur": e - s,
                 "delayed": (e - s) > _SWAP_ACTION_NORMAL}
                for s, e in zip(swap_inits, swap_eff)]

    return {
        "total": total,
        "role_damage": role_damage,
        "per_release": per_release,
        "enemy_window_log": enemy_window_log,
        "swap_log": swap_log,
    }


def _normal_slot_tag(atk, idx: int) -> str:
    """普攻段槽位标签：'·主武器'/'·副武器'（数据无 slot 字段时返回空串）。
    slot 由 开发者入口/build 构建流程（stage 1 源表 / stage 3 普攻基准比对）按基准写入。"""
    na = getattr(atk, "normal_attacks", None) or []
    if 0 <= idx - 1 < len(na) and isinstance(na[idx - 1], dict):
        slot = na[idx - 1].get("slot")
        if slot:
            return f"·{slot}"
    return ""


def _second_damage(dps_table, front_set, t, day_time):
    """返回该秒（0-based 第二索引）的离散伤害 (total, per_char)。
    普攻/通常技/支援技按各自 CD 触发：触发点 k*CD（k≥1 正整数）落在本秒 [t, t+1) 即触发，
    首次触发在 t≈cd——即"第一次触发至少要满足 CD"。
    不用 t % cd 取模：1s 粒度下浮点 CD（如 1.8）取模失配会导致几乎永不触发。
    必杀技由 DP / burst_at_time 另行叠加，此处不计。"""
    is_day = t < day_time
    per = {i: 0.0 for i in range(6)}
    total = 0.0
    for idx in range(6):
        is_front = idx in front_set
        tab = dps_table[(idx, is_day, is_front)]
        # 普攻：仅前排（切前排即时发射等由模拟器逐秒推演，此处仅作粗略离散估算）
        if is_front:
            for na in (tab.get("normal_attacks") or []):
                cd = na.get("cd", 0)
                if cd > 0 and t > 0:
                    k = int((t + 1 - 1e-9) // cd)
                    if k >= 1 and k * cd >= t - 1e-9:
                        d = na["one"]
                        per[idx] += d
                        total += d
        # 通常技：仅前排
        scd = tab.get("skill_cd", 0)
        if is_front and scd > 0 and t > 0:
            k = int((t + 1 - 1e-9) // scd)
            if k >= 1 and k * scd >= t - 1e-9:
                d = tab.get("skill_one", 0.0)
                per[idx] += d
                total += d
        # 支援技：仅后排
        spcd = tab.get("support_cd", 0)
        if (not is_front) and spcd > 0 and t > 0:
            k = int((t + 1 - 1e-9) // spcd)
            if k >= 1 and k * spcd >= t - 1e-9:
                d = tab.get("support_one", 0.0)
                per[idx] += d
                total += d
    return total, per


class EncounterDP:
    """1s 粒度动态规划求解最优交换时机和必杀技时机。

    状态：(front_tuple, last_swap_time, burst_cd_tuple)
    - front_tuple：前排3个角色索引的有序元组（排序后作为 key）
    - last_swap_time：上次交换时间（-1 表示未交换过）
    - burst_cd_tuple：6个角色必杀技剩余 CD 的元组（整数秒）

    转移：每个时间步 t→t+1
    1. 累积所有角色 DPS 伤害
    2. 决策：不操作 / 交换（若满足约束）/ 释放必杀技（对每个前排且 CD=0 的角色）

    为控制状态空间，每个时间步只保留 Top-K 状态。"""

    def __init__(self, attackers, dps_table, day_time, total_time):
        self.attackers = attackers
        self.dps_table = dps_table
        self.day_time = int(day_time)
        self.total_time = int(total_time)
        # 初始前排 [0,1,2]，后排 [3,4,5]
        self.init_front = tuple(sorted([0, 1, 2]))
        self.init_back = tuple(sorted([3, 4, 5]))
        # 必杀技 CD 列表
        self.burst_cds = [
            int(self.dps_table[(i, True, True)]["burst_cd"])
            for i in range(6)
        ]
        # 初始必杀 CD 置为 cd+1：使首次释放最早在第 cd 秒（满足开场 CD，不可在 t=0 直接放）
        self.init_burst_cds = tuple(self.burst_cds[i] + 1 for i in range(6))
        # Top-K 剪枝
        self.top_k = 30

    def _is_day(self, t: int) -> bool:
        return t < self.day_time

    def _accumulate_dps(self, front: tuple, t: int) -> float:
        """计算时间步 t 的离散伤害（普攻/通常技/支援技按各自 CD 触发，首次在 t=cd）。"""
        total, _ = _second_damage(self.dps_table, set(front), t, self.day_time)
        return total

    def _can_swap(self, t: int, last_swap: int) -> bool:
        """检查是否可以交换：t>=5 且 t - last_swap >= 10。"""
        if t < _SWAP_OPEN_DELAY:
            return False
        if last_swap < 0:
            return True
        return (t - last_swap) >= _SWAP_CD

    def _swap_front(self, front: tuple) -> tuple:
        """交换前后排，返回新的前排元组。"""
        back = tuple(i for i in range(6) if i not in front)
        return tuple(sorted(back))

    def solve(self) -> dict:
        """主求解入口。返回最优方案 {damage, swaps, burst_timings, path}。"""
        # 初始状态：必杀 CD 从 cd+1 起算（首次释放最早在第 cd 秒）
        init_burst_cds = self.init_burst_cds
        init_state = (self.init_front, -1, init_burst_cds)
        # dp[t] = {state_key: (damage, back_pointer, action)}
        # state_key = (front, last_swap, burst_cds)
        # action 描述从 t-1 到 t 的操作：('none',) / ('swap',) / ('burst', {idx: t})
        dp = {init_state: {"damage": 0.0, "path": []}}

        for t in range(self.total_time):
            if not dp:
                break
            next_dp = {}
            is_day = self._is_day(t)
            # 累积 DPS 基础伤害
            for state, info in dp.items():
                front, last_swap, burst_cds = state
                base_dmg = self._accumulate_dps(front, t)
                new_damage = info["damage"] + base_dmg
                # CD 全局减 1
                new_burst_cds_base = tuple(max(0, c - 1) for c in burst_cds)
                # 决策1：不操作（CD 正常递减）
                new_state_none = (front, last_swap, new_burst_cds_base)
                self._push_state(next_dp, new_state_none, new_damage, info["path"], ("none", t))
                # 决策2：交换
                if self._can_swap(t, last_swap):
                    new_front = self._swap_front(front)
                    new_state = (new_front, t, new_burst_cds_base)
                    self._push_state(next_dp, new_state, new_damage, info["path"], ("swap", t))
                # 决策3：释放必杀技（对前排且 CD=0 的角色，全释放）
                ready = [i for i in front if new_burst_cds_base[i] == 0 and self.burst_cds[i] > 0]
                if ready:
                    burst_dmg = 0.0
                    new_cds = list(new_burst_cds_base)
                    for idx in ready:
                        burst_dmg += self.dps_table[(idx, is_day, True)]["burst_one"]
                        new_cds[idx] = self.burst_cds[idx]
                    new_state = (front, last_swap, tuple(new_cds))
                    self._push_state(next_dp, new_state, new_damage + burst_dmg,
                                     info["path"], ("burst", t, tuple(ready)))
            # Top-K 剪枝
            dp = self._prune(next_dp)
        # 取最终最大伤害
        if not dp:
            return {"damage": 0.0, "path": []}
        best_state = max(dp.keys(), key=lambda s: dp[s]["damage"])
        best_info = dp[best_state]
        return {
            "damage": best_info["damage"],
            "path": best_info["path"],
        }

    def _push_state(self, next_dp, state, damage, prev_path, action):
        """更新 next_dp[state]，保留伤害最大的路径。"""
        # action 含时间戳，用于回溯
        new_path = prev_path + [action]
        if state not in next_dp or damage > next_dp[state]["damage"]:
            next_dp[state] = {"damage": damage, "path": new_path}

    def _prune(self, dp: dict) -> dict:
        """Top-K 剪枝：只保留伤害最高的 K 个状态。"""
        if len(dp) <= self.top_k:
            return dp
        sorted_states = sorted(dp.items(), key=lambda x: x[1]["damage"], reverse=True)
        return dict(sorted_states[:self.top_k])


def _reconstruct_timeline(path: list, attackers: List) -> dict:
    """从 DP 路径回溯操作时间轴。
    返回 {swaps: [t1, t2, ...], burst_timings: {idx: [t1, t2, ...]}, events: [(t, type, data)]}"""
    swaps = []
    burst_timings = {}
    events = []
    for action in path:
        if action[0] == "swap":
            t = action[1]
            swaps.append(t)
            events.append((t, "swap", None))
        elif action[0] == "burst":
            t = action[1]
            indices = action[2]
            for idx in indices:
                burst_timings.setdefault(idx, []).append(t)
                events.append((t, "burst", idx))
    # 去重并排序
    swaps = sorted(set(swaps))
    events.sort(key=lambda e: e[0])
    return {"swaps": swaps, "burst_timings": burst_timings, "events": events}


def _fmt_level(x) -> str:
    """等级显示：整数不带小数（100 → '100'），带小数保留一位（120.5 → '120.5'）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = float(_PLAYER_SHIP_LEVEL)
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 else ('%.1f' % v)


def _suppress_lines(attackers: List, boss: dict) -> List[str]:
    """[PVE等级压制] 输出行（时间轴与「详细伤害计算」共用一个口径）。

    等级取各攻击方自身的 player_level（load_encounter_attacker 按「我的舰船」面板
    里填的等级写入；缺失回落默认 Lv100）。全队同等级 → 一行（与改造前格式一致）；
    等级不同 → 按等级分组分别列出 —— 伤害本就是**按各舰自身等级**压制计算的，
    只打印一个等级会与实际伤害不符。倍率为 1.00 的档不列出（该档无压制）。
    """
    _bl = float(boss.get("level", _ENCOUNTER_BOSS_LEVEL) or _ENCOUNTER_BOSS_LEVEL)
    groups = {}
    for a in (attackers or []):
        try:
            lv = float(getattr(a, "player_level", _PLAYER_SHIP_LEVEL) or _PLAYER_SHIP_LEVEL)
        except (TypeError, ValueError):
            lv = float(_PLAYER_SHIP_LEVEL)
        groups[lv] = groups.get(lv, 0) + 1
    if not groups:
        groups[float(_PLAYER_SHIP_LEVEL)] = 1
    out = []
    for lv in sorted(groups):
        f = _level_suppress_factor(lv, _bl)
        if f == 1.0:
            continue
        tag = '' if len(groups) == 1 else '（%d 艘）' % groups[lv]
        out.append('[PVE等级压制] 玩家Lv%s%s vs Boss Lv%d → 玩家对Boss伤害 ×%.2f'
                   % (_fmt_level(lv), tag, int(_bl), f))
    return out


def _format_timeline(best: dict, attackers: List, boss: dict, day_time: float,
                     total_time: float, dps_table: dict, course: str, air: str,
                     flagship_name: str = "", flagship_aura: dict = None,
                     buff_aware_role_damage: dict = None, buff_aware_total: float = None,
                     timeline_override: dict = None, swap_log: list = None) -> str:
    """格式化输出操作时间轴和伤害明细。
    swap_log：模拟器返回的交换时序日志 [{init, eff, dur, delayed}]，
    用于在时间轴上标注「命中通常技后摇 → 交换动作耗时 2s」。"""
    if timeline_override is not None:
        timeline = timeline_override
    else:
        path = best.get("path", [])
        timeline = _reconstruct_timeline(path, attackers)
    swaps = timeline["swaps"]
    # init 秒 → 该次交换的时序信息（用于时间轴标注）
    _swap_info = {int(x.get("init")): x for x in (swap_log or []) if isinstance(x, dict)}
    burst_timings = timeline["burst_timings"]
    events = timeline["events"]

    lines = []
    lines.append("=" * 60)
    lines.append("             遭遇战模拟结果（理论最高伤害）")
    lines.append("=" * 60)
    lines.append(f"Boss: {boss.get('name', '')} | 白天: {int(day_time)}s | 黑夜: {int(total_time - day_time)}s | 总时间: {int(total_time)}s")
    # PVE 等级压制行：按各攻击方自身等级（「我的舰船」面板里填的等级）逐档列出
    lines.extend(_suppress_lines(attackers, boss))
    lines.append(f"航向: {course} | 制空: {air}")
    lines.append(f"初始前排: {', '.join(attackers[i].name for i in [0,1,2])}")
    lines.append(f"初始后排: {', '.join(attackers[i].name for i in [3,4,5])}")
    if flagship_name:
        # 正值不带 + 号、负值保留 - 号（避免 增加+-10% 奇怪格式）
        aura_desc = "、".join(f"{k}{v:.0%}" if abs(v) < 1 else f"{k}{v:.0f}"
                              for k, v in (flagship_aura or {}).items())
        if aura_desc:
            # 光环已按范围/条件细分；遭遇战旗舰仅在夜战生效（用户约定），标注以免误读为全时段
            lines.append(f"旗舰: {flagship_name}（光环夜战生效: {aura_desc}）")
        else:
            lines.append(f"旗舰: {flagship_name}（无光环生效：该角色无旗舰技或条件未达成）")
    lines.append("")

    # --- 操作时间轴 ---
    lines.append("--- 操作时间轴 ---")
    cur_front = [0, 1, 2]
    cur_back = [3, 4, 5]
    # 构建"事件点"列表（含战斗开始/黑夜切换/战斗结束）
    all_events = [(0.0, "start", None)]
    for t in swaps:
        # 事件时刻 = **生效时刻**（正常 s+1，命中通常技后摇 s+2）；附发起时刻供文案标注
        _si = _swap_info.get(int(t)) or {}
        all_events.append((float(_si.get("eff", int(t) + 1)), "swap", int(t)))
    for idx, ts in burst_timings.items():
        for t in ts:
            all_events.append((float(t), "burst", idx))
    all_events.append((float(day_time), "night", None))
    all_events.append((float(total_time), "end", None))
    all_events.sort(key=lambda e: e[0])

    for t, etype, idx in all_events:
        if etype == "start":
            lines.append(f"[第 {int(t)}s] 战斗开始，前排: {', '.join(attackers[i].name for i in cur_front)} | 后排: {', '.join(attackers[i].name for i in cur_back)}")
        elif etype == "swap":
            _init = int(idx) if idx is not None else int(t)
            _si = _swap_info.get(_init) or {}
            if _si.get("delayed"):
                _note = (f'第 {_init}s 发起，被通常技后摇卡住 → 推迟到本秒才换成功'
                         f'（{_init}-{_init + 1}s 仍由旧前排行动，不停摆；交换 CD 按 {_init}s 计）')
            else:
                _note = f'第 {_init}s 发起（换人不影响行动；交换 CD 按 {_init}s 计）'
            cur_front, cur_back = cur_back, cur_front
            lines.append(f"[第 {int(t)}s] 交换前后排（{_note}）→ 前排: {', '.join(attackers[i].name for i in cur_front)} | 后排: {', '.join(attackers[i].name for i in cur_back)}")
        elif etype == "burst":
            lines.append(f"[第 {int(t)}s] {attackers[idx].name} 释放必杀技")
        elif etype == "night":
            lines.append(f"[第 {int(t)}s] 进入黑夜，boss 装甲{_NIGHT_ARMOR_DESC}，航母/轻母/装母禁用")
        elif etype == "end":
            lines.append(f"[第 {int(t)}s] 战斗结束")
    lines.append("")

    # --- 伤害明细 ---
    lines.append("--- 伤害明细 ---")
    # 若传入 buff 感知结果（含技能绑定持续 buff 窗口），直接使用其角色伤害；
    # 否则回退为恒定 buff 的 DP 路径重建。
    if buff_aware_role_damage is not None:
        role_damage = buff_aware_role_damage
    else:
        # 根据路径重建时段，计算每个角色白天/黑夜伤害
        role_damage = {(i, True): 0.0 for i in range(6)}
        role_damage.update({(i, False): 0.0 for i in range(6)})
        # 重建前后排状态序列
        cur_front_set = set([0, 1, 2])
        # 交换时间点列表
        swap_set = set(swaps)
        # 必杀技时间点：{t: [idx, ...]}
        burst_at_time = {}
        for idx, ts in burst_timings.items():
            for t in ts:
                burst_at_time.setdefault(t, []).append(idx)
        for t in range(int(total_time)):
            is_day = t < day_time
            # 普攻/通常技/支援技：按各自 CD 离散触发（首次在 t=cd，不在开场 t=0）
            _, per = _second_damage(dps_table, cur_front_set, t, day_time)
            for idx in range(6):
                role_damage[(idx, is_day)] += per[idx]
            # 必杀技伤害（在 t 时刻释放，仅前排生效）
            if t in burst_at_time:
                for idx in burst_at_time[t]:
                    if idx in cur_front_set:
                        role_damage[(idx, is_day)] += dps_table[(idx, is_day, True)]["burst_one"]
            # 交换（在 t 时刻末尾交换，影响 t+1 秒）
            if t in swap_set:
                cur_front_set = set(range(6)) - cur_front_set

    for i in range(6):
        atk = attackers[i]
        day_d = role_damage[(i, True)]
        night_d = role_damage[(i, False)]
        role_total = day_d + night_d
        note = ""
        if atk.ship_type in _NIGHT_BANNED_SHIP_TYPES:
            note = " (航母/轻母/装母黑夜禁用)"
        lines.append(f"  {atk.name}({atk.ship_type}){note}:")
        lines.append(f"    白天: {day_d:.0f} | 黑夜: {night_d:.0f} | 合计: {role_total:.0f}")

    lines.append("-" * 60)
    total_val = buff_aware_total if buff_aware_total is not None else best.get('damage', 0.0)
    lines.append(f"  总伤害: {total_val:.0f}" + ("（已计入技能绑定持续 buff 窗口）" if buff_aware_total is not None else ""))
    lines.append("=" * 60)
    return "\n".join(lines)


def _append_factors_detail(lines: list, label: str, factors: dict):
    """把单个攻击类型（单类型或混合技能）的详细式子追加到 lines。"""
    if not factors:
        return
    if factors.get("type") == "混合技能":
        for part in factors.get("parts", []):
            dmg_type = part.get("damage_type", "")
            lines.append(OutputHandler.format_detail_expression(f"{label}-{dmg_type}", part))
    else:
        lines.append(OutputHandler.format_detail_expression(label, factors))


def _fmt_buff_agg(agg: dict, prefix: str) -> str:
    """把聚合后的 buff dict 格式化为展示串（如 '敌方减益: 全伤害减免15%'）。
    正值不带 + 号、负值保留 - 号（避免出现 增加+-10% 这类奇怪格式）。"""
    if not agg:
        return ""
    parts = []
    for k, v in agg.items():
        if abs(v) < 1:
            parts.append(f"{k}{v:.0%}")
        else:
            parts.append(f"{k}{v:.0f}")
    return f"{prefix}: " + "、".join(parts)


def _build_detail_legacy(attackers, boss, day_time, course, air,
                         flagship_name="", flagship_aura=None) -> str:
    """回退展示：每角色每技能一个代表式（白天状态，恒定 buff，不含窗口）。"""
    lines = []
    lines.append("=" * 64)
    lines.append("            详细伤害计算（每次技能的运算式子）")
    lines.append("=" * 64)
    lines.append(f"Boss: {boss.get('name', '')} | 航向: {course} | 制空: {air} | 计算状态: 白天（boss 满甲）")
    lines.append("说明：下方为每个角色各技能的完整伤害公式（含 buff 运算，与单体伤害计算一致）。")
    lines.append(f"      黑夜 boss 装甲{_NIGHT_ARMOR_DESC}、航母/轻母/装母禁用，buff 结构相同，仅基础伤害随装甲变化。")
    lines.append("")

    lines.append("【全局 buff 来源】")
    raguz_items = []
    for atk in attackers:
        rb = float(getattr(atk, "raguz_bonus", 0.0) or 0.0)
        if rb > 0:
            raguz_items.append(f"{atk.name}(+{rb:.0%})")
    if raguz_items:
        lines.append(f"  · 拉古兹阵营增伤：{'、'.join(raguz_items)} 已并入各自「全伤害增加」")
    else:
        lines.append("  · 拉古兹阵营增伤：无（角色未配置 raguz_bonus）")
    if flagship_name and flagship_aura:
        aura_txt = "、".join(f"{k}{v:.0%}" if abs(v) < 1 else f"{k}{v:.0f}"
                             for k, v in flagship_aura.items())
        lines.append(f"  · 旗舰光环（{flagship_name}）：{aura_txt}（已按范围/条件生效）")
    else:
        lines.append("  · 旗舰光环：无")
    lines.append("")

    day_enemy = build_encounter_enemy(boss, True)

    for i, atk in enumerate(attackers):
        is_front_initial = i in (0, 1, 2)
        role_tag = "前排" if is_front_initial else "后排"
        lines.append("-" * 64)
        lines.append(f"【{i + 1}. {atk.name}】 {atk.ship_type} · 初始{role_tag}")
        rb = float(getattr(atk, "raguz_bonus", 0.0) or 0.0)
        if rb > 0:
            lines.append(f"    · 拉古兹阵营增伤 +{rb:.0%}（已并入 全伤害增加）")
        if flagship_name and flagship_aura:
            lines.append(f"    · 旗舰光环（{flagship_name}）已生效")
        normals = DamageCalculator.calculate_normal_all(atk, day_enemy, course, air)
        if normals:
            for idx, f in enumerate(normals, 1):
                lines.append(f"  ▶ 普攻{idx}{_normal_slot_tag(atk, idx)}：")
                _append_factors_detail(lines, f"普攻{idx}", f)
        else:
            lines.append("  ▶ 普攻：无配置")
        if is_front_initial:
            sk = DamageCalculator.calculate_skill(atk, day_enemy, course, air)
            if sk:
                lines.append("  ▶ 通常技：")
                _append_factors_detail(lines, "通常技", sk)
            else:
                lines.append("  ▶ 通常技：无配置")
            bs = DamageCalculator.calculate_burst(atk, day_enemy, course, air)
            if bs:
                lines.append("  ▶ 必杀技：")
                _append_factors_detail(lines, "必杀技", bs)
            else:
                lines.append("  ▶ 必杀技：无配置")
        else:
            sp = DamageCalculator.calculate_support(atk, day_enemy, course, air)
            if sp:
                lines.append("  ▶ 支援技：")
                _append_factors_detail(lines, "支援技", sp)
            else:
                lines.append("  ▶ 支援技：无配置")

    lines.append("=" * 64)
    return "\n".join(lines)


def _delay_annotation(r: dict) -> str:
    """为延迟结算的释放条目生成「释放X→结算Y」标注（航空必杀双延迟2s）。

    r 来自 _simulate_encounter_buff_aware 的 per_release，含字段：
    t(释放时刻)、settle_t(结算时刻)、delay(延后秒数)、kind、ship_type。
    """
    d = r.get("delay", 0)
    if d <= 0:
        return ""
    is_av = r.get("ship_type", "") in _NIGHT_BANNED_SHIP_TYPES
    if r["kind"] == "burst":
        return f"（释放{r['t']}s→结算{r['settle_t']}s · 必杀延迟{d}s）"
    if is_av:
        return f"（释放{r['t']}s→结算{r['settle_t']}s · 航空延迟{d}s）"
    return f"（释放{r['t']}s→结算{r['settle_t']}s · 延迟{d}s）"


def _fmt_calc_line(factors: dict, damage: float) -> str:
    """把单次释放的 factors 压缩成一行核心计算式（时间轴总览用）。

    乘数链：攻击 × 倍率 × 段数 × 防御衰减 × 减伤 × 克制（× 暴击/命中，若 ≠1）；
    混合技能取各段式子分号拼接；无可用字段时回退只显示期望值。
    """
    def _one(f: dict) -> str:
        parts = []
        fa = f.get("final_attack")
        if fa:
            parts.append(f"攻击 {fa:.0f}")
        if f.get("multiplier") is not None:
            parts.append(f"倍率 {f['multiplier']:.2f}")
        hc = f.get("hit_count", 1)
        if hc != 1:
            parts.append(f"段数 {hc}")
        for key, label in (("defense_decay", "防御"), ("enemy_reduction", "减伤"), ("modifier", "克制")):
            v = f.get(key)
            if v is not None:
                parts.append(f"{label} {v:.4f}")
        ce = f.get("crit_expect")
        if ce is not None and abs(ce - 1) > 1e-6:
            parts.append(f"暴击 {ce:.4f}")
        hr = f.get("hit_rate")
        if hr is not None and abs(hr - 1) > 1e-6:
            parts.append(f"命中 {hr:.4f}")
        return " × ".join(parts)

    if factors.get("type") == "混合技能":
        segs = [_one(p) for p in (factors.get("parts") or [])]
        segs = [s for s in segs if s]
        return ("；".join(segs) + f" = {damage:.0f}") if segs else f"= {damage:.0f}"
    s = _one(factors)
    return (s + f" = {damage:.0f}") if s else f"= {damage:.0f}"


def build_detail_module(attackers: List, boss: dict, day_time: float,
                        course: str, air: str,
                        flagship_name: str = "", flagship_aura: dict = None,
                        sim: dict = None, is_pvp: bool = False,
                        night_time: float = None, total_time: float = None) -> str:
    """构建"详细伤害计算"折叠模块（逐次释放）。
    传入 sim（_simulate_encounter_buff_aware 结果）时：按每个角色的
    通常技/必杀技/支援技【逐次释放】列出完整式子与期望伤害，并标注当时生效的
    技能绑定持续 buff 窗口（敌方减益全队共享、自身增益仅影响释放者）——对应 q-0/q-1。
    未传入 sim 时回退为原"每角色每技能一个代表式"展示。
    is_pvp=True：标题/字段适配 PVP（防守方、时长、全出战位、无黑夜/拉古兹、无支援技）。"""
    if sim is None:
        return _build_detail_legacy(attackers, boss, day_time, course, air,
                                     flagship_name=flagship_name, flagship_aura=flagship_aura)

    lines = []
    lines.append("=" * 64)
    if is_pvp:
        lines.append("        模拟组队 · 详细伤害计算（逐次释放 · 含技能绑定 buff 窗口）")
    else:
        lines.append("        详细伤害计算（逐次释放 · 含技能绑定 buff 窗口）")
    lines.append("=" * 64)
    if is_pvp:
        _nt = int(night_time) if night_time is not None else int(120 - day_time)
        _tt = int(total_time) if total_time is not None else 120
        lines.append(f"防守方: {boss.get('name', '')} | 航向: {course} | 制空: {air} | "
                     f"白天: {int(day_time)}s | 黑夜: {_nt}s | 总时长: {_tt}s（黑夜 = 120 - 白天）")
    else:
        lines.append(f"Boss: {boss.get('name', '')} | 航向: {course} | 制空: {air} | 白天: {int(day_time)}s")
        lines.extend(_suppress_lines(attackers, boss))
    lines.append("说明：下列为每个角色各技能【逐次释放】的完整式子与期望伤害；")
    lines.append("      带「持续buff」的技能释放后，其绑定 buff 在 duration 内生效——")
    lines.append("      敌方减益(全伤害减免/各类型减免等)为全队共享，会使窗口内全队伤害变化；")
    lines.append("      自身增益仅影响释放者后续伤害。故处于 buff 窗口内的后续释放伤害会更高。")
    lines.append("      航空类攻击与所有必杀/支援技均统一延后结算：伤害在「释放时刻+1s」落点并判定 buff；")
    lines.append("      已去掉「航母类 2s」旧机制，所有伤害延迟恒为 1s。标注形如")
    lines.append("      「释放Xs→结算Ys · 延迟1s」。")
    if is_pvp:
        lines.append("      模拟组队：全出战位、无交换、不算支援技；天赋=默认PVP推荐。")
    else:
        lines.append(f"      黑夜 boss 装甲{_NIGHT_ARMOR_DESC}、航母/轻母/装母禁用；普攻作紧凑汇总（详见各船普攻段）。")
    lines.append("")

    # 全局 buff 来源
    lines.append("【全局 buff 来源】")
    if is_pvp:
        lines.append("  · 拉古兹阵营增伤：无（模拟组队 无拉古兹增伤）")
        if flagship_name and flagship_aura:
            aura_txt = "、".join(f"{k}{v:.0%}" if abs(v) < 1 else f"{k}{v:.0f}" for k, v in flagship_aura.items())
            lines.append(f"  · 旗舰光环（{flagship_name}）：{aura_txt}（已按范围/条件生效）")
        else:
            lines.append("  · 旗舰光环：无")
    else:
        raguz_items = []
        for atk in attackers:
            rb = float(getattr(atk, "raguz_bonus", 0.0) or 0.0)
            if rb > 0:
                raguz_items.append(f"{atk.name}(+{rb:.0%})")
        lines.append("  · 拉古兹阵营增伤：" + ("、".join(raguz_items) + " 已并入各自「全伤害增加」" if raguz_items else "无"))
        if flagship_name and flagship_aura:
            aura_txt = "、".join(f"{k}{v:.0%}" if abs(v) < 1 else f"{k}{v:.0f}" for k, v in flagship_aura.items())
            lines.append(f"  · 旗舰光环（{flagship_name}）：{aura_txt}（已按范围/条件生效）")
        else:
            lines.append("  · 旗舰光环：无")
    lines.append("")

    per_release = sim.get("per_release", [])
    by_ship_kind = defaultdict(list)
    normals_by_ship = defaultdict(list)
    for r in per_release:
        if r["kind"] == "normal":
            normals_by_ship[r["idx"]].append(r)
        else:
            by_ship_kind[(r["idx"], r["kind"])].append(r)

    # 技能绑定持续 buff 一览
    lines.append("【技能绑定持续 buff 一览（释放后生效，窗口内持续）】")
    any_sb = False
    for i, atk in enumerate(attackers):
        sbs = getattr(atk, "skill_buffs", None) or []
        if not sbs:
            continue
        any_sb = True
        items = []
        for sb in sbs:
            buff_txt = "、".join(
                (f"{k}{v:+.0%}" if abs(v) < 1 else f"{k}{v:+.0f}") for k, v in sb.get("buffs", {}).items())
            items.append(f"{sb.get('trigger')}→{sb.get('target')} [{buff_txt}] {float(sb.get('duration', 0)):.0f}s")
        lines.append(f"  · {atk.name}：{'；'.join(items)}")
    if not any_sb:
        lines.append("  · 无（本队角色均无技能绑定持续 buff）")
    lines.append("")

    # 逐次释放时间轴（按秒序，全角色 · 技能类），标注单次技能伤害
    kind_label = {"skill": "通常技", "burst": "必杀技", "support": "支援技"}
    lines.append("【逐次释放时间轴（按秒序 · 全角色 · 技能类）】")
    lines.append("  说明：下列为模拟时间轴内「每一次技能释放」（通常技/必杀技/支援技，不含普攻）的")
    lines.append("        单次期望伤害；「窗口内」表示该次释放正处于某技能绑定 buff 持续窗口中，伤害已受影响。")
    release_sorted = sorted(
        (r for r in per_release if r["kind"] != "normal"),
        key=lambda r: (r["t"], r["idx"], r["kind"]))
    if release_sorted:
        for r in release_sorted:
            if is_pvp:
                state = "出战位"
            else:
                state = ("白天" if r["is_day"] else "黑夜") + ("·前排" if r["is_front"] else "·后排")
            notes = [x for x in (_fmt_buff_agg(r["active_self"], "自身buff"),
                                 _fmt_buff_agg(r["active_enemy"], "敌方减益")) if x]
            buff_note = ("  「窗口内 " + "；".join(notes) + "」") if notes else ""
            lines.append(f"  · 第{r['t']}s {r['name']}（{state}）{kind_label[r['kind']]} → 期望 {r['damage']:.0f}{buff_note}{_delay_annotation(r)}")
            calc = _fmt_calc_line(r["factors"], r["damage"])
            if calc:
                lines.append(f"      计算：{calc}")
    else:
        lines.append("  · 无技能释放记录（仅普攻）")
    lines.append("")

    day_enemy = build_encounter_enemy(boss, True)

    for i, atk in enumerate(attackers):
        # PVP 无前后排之分，所有角色都算"出战位"，不使用 is_front_initial 概念
        if is_pvp:
            role_tag = "出战位"
        else:
            is_front_initial = i in (0, 1, 2)
            role_tag = "前排" if is_front_initial else "后排"
        lines.append("-" * 64)
        if is_pvp:
            lines.append(f"【{i + 1}. {atk.name}】 {atk.ship_type} · 出战位")
        else:
            lines.append(f"【{i + 1}. {atk.name}】 {atk.ship_type} · 初始{role_tag}")
        rb = float(getattr(atk, "raguz_bonus", 0.0) or 0.0)
        if rb > 0:
            lines.append(f"    · 拉古兹阵营增伤 +{rb:.0%}（已并入 全伤害增加）")
        if flagship_name and flagship_aura:
            lines.append(f"    · 旗舰光环（{flagship_name}）已生效")

        # 普攻汇总（紧凑）
        normals = normals_by_ship.get(i, [])
        base_normals = DamageCalculator.calculate_normal_all(atk, day_enemy, course, air)
        if base_normals:
            dmg_vals = [r["damage"] for r in normals]
            dmin = min(dmg_vals) if dmg_vals else 0
            dmax = max(dmg_vals) if dmg_vals else 0
            lines.append(f"  ▶ 普攻（共 {len(normals)} 次；期望伤害随 buff 窗口在 {dmin:.0f}~{dmax:.0f} 间浮动）：")
            _state_label = "出战位" if is_pvp else "前排"
            for idx, f in enumerate(base_normals, 1):
                lines.append(f"    普攻{idx}{_normal_slot_tag(atk, idx)} 基准式（白天·{_state_label}，无 buff 窗口）：")
                _append_factors_detail(lines, f"普攻{idx}", f)
        else:
            lines.append("  ▶ 普攻：无配置")

        # 技能逐次释放（PVP 无支援技，跳过 support 段）
        for kind in ["skill", "burst"] + ([] if is_pvp else ["support"]):
            releases = by_ship_kind.get((i, kind), [])
            if not releases:
                if is_pvp:
                    # PVP 下所有人都是战位，都可释放 skill/burst（support 已被跳过）
                    _has = kind in ("skill", "burst")
                else:
                    _has = kind in ("skill", "burst") and is_front_initial or kind == "support" and (not is_front_initial)
                has_cfg = False
                if kind == "skill" and _has:
                    has_cfg = bool((atk.skill_attack or {}).get("parts"))
                elif kind == "burst" and _has:
                    has_cfg = bool((atk.burst_attack or {}).get("parts"))
                elif kind == "support" and _has:
                    has_cfg = bool((atk.support_attack or {}).get("parts"))
                if has_cfg:
                    if is_pvp:
                        lines.append(f"  ▶ {kind_label[kind]}：在模拟时间轴内未释放（受 CD 时序影响）")
                    else:
                        lines.append(f"  ▶ {kind_label[kind]}：在模拟时间轴内未释放（受交换/前后排时序影响）")
                else:
                    lines.append(f"  ▶ {kind_label[kind]}：无配置（{role_tag}不释放）")
                continue
            lines.append(f"  ▶ {kind_label[kind]}（共 {len(releases)} 次释放，均由 {atk.name} 释放）：")
            for r in releases:
                if is_pvp:
                    state = "出战位"
                else:
                    state = ("白天" if r["is_day"] else "黑夜") + ("·前排" if r["is_front"] else "·后排")
                notes = [x for x in (_fmt_buff_agg(r["active_self"], "自身buff"),
                                     _fmt_buff_agg(r["active_enemy"], "敌方减益")) if x]
                buff_note = ("  「窗口内 " + "；".join(notes) + "」") if notes else ""
                # 每次释放明确标注释放者（满足「明细里每次谁释放的技能要标注」）
                lines.append(f"    · {atk.name} 第{r['t']}s（{state}）释放{kind_label[kind]} → 期望 {r['damage']:.0f}{buff_note}{_delay_annotation(r)}")
                factors = r["factors"]
                if factors.get("type") == "混合技能":
                    for p in factors.get("parts", []):
                        lines.append(OutputHandler.format_detail_expression(
                            f"{atk.name}·{kind_label[kind]}-{p.get('damage_type', '')}", p))
                else:
                    lines.append(OutputHandler.format_detail_expression(
                        f"{atk.name}·{kind_label[kind]}", factors))

    lines.append("=" * 64)
    return "\n".join(lines)


def _suggest_ship_names(app, raw: str, limit: int = 5):
    """为未识别的船名提供近似候选（子串包含/被包含匹配）。"""
    if not raw:
        return []
    raw = raw.strip()
    names = getattr(app, "all_ship_names", None)
    if not names:
        try:
            names = [s.name for s in getattr(app, "ships", {}).values()]
        except Exception:
            names = []
    seen, out = set(), []
    for n in names:
        if raw in n or n in raw:
            if n not in seen:
                seen.add(n)
                out.append(n)
        if len(out) >= limit:
            break
    return out


def _validate_ship_list(app, ships, field_prefix, label_prefix, start_index, errors):
    """校验一组舰名（raw 字符串列表），逐个识别，未识别的写入 errors。
    返回规范名列表（未识别项原样保留 raw 以便回填表单）。
    信息不全（is_ship_complete=False：缺面板/技能/普攻/天赋任一项）等同"没有该角色"，
    按未找到处理（用户口径 2026-09-14：缺基础信息不得计算）。"""
    canon = []
    for idx, s in enumerate(ships):
        i = start_index + idx
        s = (s or "").strip()
        if not s:
            errors.append({'field': f'{field_prefix}{i}', 'label': f'{label_prefix}{i}',
                           'raw': s, 'message': '角色名不能为空'})
            canon.append(s)
            continue
        r = app.get_ship_from_all(s)
        if r is None or not app.is_ship_complete(s):
            cands = _suggest_ship_names(app, s)
            sug = ('。近似候选：' + '、'.join(cands)) if cands else ''
            errors.append({'field': f'{field_prefix}{i}', 'label': f'{label_prefix}{i}',
                           'raw': s, 'message': f'未找到角色「{s}」{sug}'})
            canon.append(s)
        else:
            canon.append(r.name)
    return canon


def validate_encounter_input(app, boss_name, day_time, front_ships, back_ships,
                             flagship_name=''):
    """校验遭遇战输入的全部项目，识别「所有」无法识别的项（不止第一个）。
    返回 {'ok', 'errors':[{field,label,raw,message}], 'resolved':{...}}。
    resolved 含已识别项的规范名与白天时间（float）；ok=True 时可直接用于模拟。
    field 取值：boss / day_time / front1~3 / back1~3 / flagship。"""
    errors = []
    resolved = {}
    bosses = load_encounter_bosses()
    rb = resolve_encounter_boss(bosses, boss_name or '')
    if rb is None:
        avail = "、".join(sorted(bosses.keys())) or "（无）"
        errors.append({'field': 'boss', 'label': 'Boss', 'raw': boss_name or '',
                       'message': f'未找到 boss「{boss_name or ""}」。可用：{avail}'})
        resolved['boss_name'] = boss_name or ''
    else:
        resolved['boss_name'] = rb
    # 白天时间
    try:
        dt = float(day_time)
    except (TypeError, ValueError):
        errors.append({'field': 'day_time', 'label': '白天时间', 'raw': str(day_time),
                       'message': '白天时间必须是数字（秒，0~150）'})
        dt = None
    if dt is not None and (dt < 0 or dt > 150):
        errors.append({'field': 'day_time', 'label': '白天时间', 'raw': str(day_time),
                       'message': f'白天时间需在 0~150s 之间，当前 {dt}s'})
    resolved['day_time'] = dt
    # 前排 / 后排
    resolved['front_ships'] = _validate_ship_list(app, front_ships or [], 'front', '前排', 1, errors)
    resolved['back_ships'] = _validate_ship_list(app, back_ships or [], 'back', '后排', 1, errors)
    # 数量校验（缺填）
    if len([x for x in (front_ships or []) if x]) != 3:
        errors.append({'field': 'front1', 'label': '前排', 'raw': '',
                       'message': '前排需填 3 个角色'})
    if len([x for x in (back_ships or []) if x]) != 3:
        errors.append({'field': 'back1', 'label': '后排', 'raw': '',
                       'message': '后排需填 3 个角色'})
    # 旗舰（信息不全同样按未找到处理）
    if flagship_name:
        rf = app.get_ship_from_all(flagship_name)
        if rf is None or not app.is_ship_complete(flagship_name):
            cands = _suggest_ship_names(app, flagship_name)
            sug = ('。近似候选：' + '、'.join(cands)) if cands else ''
            errors.append({'field': 'flagship', 'label': '旗舰', 'raw': flagship_name,
                           'message': f'未找到旗舰角色「{flagship_name}」{sug}'})
            resolved['flagship_name'] = flagship_name
        else:
            resolved['flagship_name'] = rf.name
    else:
        resolved['flagship_name'] = ''
    return {'ok': len(errors) == 0, 'errors': errors, 'resolved': resolved}


def validate_pvp_input(app, defender_name, day_time, front_ships, back_ships,
                       flagship_name=''):
    """校验模拟组队输入（与遭遇战同款：识别全部无法识别项）。
    field 取值：defender / day_time / front1~3 / back1~3 / flagship。
    战斗时长分白天/黑夜：白天由用户输入（0~120），黑夜 = 120 − 白天（自动推导）。"""
    errors = []
    resolved = {}
    rd = app.get_ship_from_all(defender_name or '')
    # 信息不全（缺面板/技能/普攻/天赋任一项）等同"没有该角色"，与未找到同口径
    if rd is None or not app.is_ship_complete(defender_name or ''):
        cands = _suggest_ship_names(app, defender_name or '')
        sug = ('。近似候选：' + '、'.join(cands)) if cands else ''
        errors.append({'field': 'defender', 'label': '防守角色', 'raw': defender_name or '',
                       'message': f'未找到防守角色「{defender_name or ""}」{sug}'})
        resolved['defender_name'] = defender_name or ''
    else:
        resolved['defender_name'] = rd.name
    try:
        dt = float(day_time)
    except (TypeError, ValueError):
        errors.append({'field': 'day_time', 'label': '战斗时长·白天', 'raw': str(day_time),
                       'message': '白天战斗时长必须是数字（秒，0~120）'})
        dt = 60.0
    if dt < 0 or dt > 120:
        errors.append({'field': 'day_time', 'label': '战斗时长·白天', 'raw': str(day_time),
                       'message': f'白天战斗时长需在 0~120s 之间，当前 {dt}s（黑夜 = 120 - 白天）'})
        dt = max(0.0, min(120.0, dt))
    resolved['day_time'] = dt
    resolved['night_time'] = 120.0 - dt
    resolved['front_ships'] = _validate_ship_list(app, front_ships or [], 'front', '角色', 1, errors)
    resolved['back_ships'] = _validate_ship_list(app, back_ships or [], 'back', '角色', 4, errors)
    if len([x for x in (front_ships or []) if x]) != 3:
        errors.append({'field': 'front1', 'label': '角色', 'raw': '',
                       'message': '需填 3 个角色'})
    if len([x for x in (back_ships or []) if x]) != 3:
        errors.append({'field': 'back1', 'label': '角色', 'raw': '',
                       'message': '需填 3 个角色'})
    if flagship_name:
        rf = app.get_ship_from_all(flagship_name)
        # 信息不全同样按未找到处理
        if rf is None or not app.is_ship_complete(flagship_name):
            cands = _suggest_ship_names(app, flagship_name)
            sug = ('。近似候选：' + '、'.join(cands)) if cands else ''
            errors.append({'field': 'flagship', 'label': '旗舰', 'raw': flagship_name,
                           'message': f'未找到旗舰角色「{flagship_name}」{sug}'})
            resolved['flagship_name'] = flagship_name
        else:
            resolved['flagship_name'] = rf.name
    else:
        # 组队 PVP 必须有旗舰（用户口径 2026-09-17）：未勾选旗舰 → 报错拦截
        errors.append({'field': 'flagship', 'label': '旗舰', 'raw': '',
                       'message': '模拟组队必须指定 1 名旗舰（在 6 人中勾选）'})
        resolved['flagship_name'] = ''
    return {'ok': len(errors) == 0, 'errors': errors, 'resolved': resolved}


def _valid_swap_schedule(swaps, total_time=150):
    """校验交换表是否满足 _can_swap 约束：每交换 t>=5，相邻间隔>=10，且 <= total-1。"""
    s = sorted(set(int(x) for x in swaps))
    if not s:
        return True
    if s[0] < 5 or s[-1] > total_time - 1:
        return False
    for j in range(len(s) - 1):
        if s[j + 1] - s[j] < 10:
            return False
    return True


def _gen_burst_schedule_from_delays(burst_cds, swap_schedule, delays, total_time=150,
                                    attackers=None, day_time=None):
    """按每船「初始延迟(>=0)」生成必杀时间表：满足 >=CD 的刚性平移（前排才放）。
    整列必杀等距平移，保持 CD 间隔；后排时机若不在前排则跳过。返回 {idx:[t1,t2,...]}。
    若传入 attackers/day_time：黑夜对航母/轻母/装母不排必杀（禁主动释放），CD 照常转。"""
    swap_times = sorted(set(int(x) for x in swap_schedule))

    def front_set_at(t):
        # 与 _simulate_encounter_buff_aware 一致：交换 s 秒发起 → s 秒仍属旧前排，s+1 生效（不停摆）。
        # 注：本函数只是「必杀相位」候选生成器，不模拟通常技，故**不感知通常技后摇导致的
        #     交换 2s 延迟**；真实伤害由 _simulate_encounter_buff_aware 打分（每次交换都会
        #     按后摇规则重算），寻优仍以真实总伤为准。
        k = sum(1 for s in swap_times if s < t)
        return set([0, 1, 2]) if k % 2 == 0 else set([3, 4, 5])

    cd = [int(burst_cds[i]) + 1 + int(delays.get(i, 0) or 0) for i in range(6)]
    out = {i: [] for i in range(6)}
    for t in range(total_time):
        cd = [max(0, c - 1) for c in cd]
        front = front_set_at(t)
        is_day = (day_time is None) or (t < day_time)
        for i in front:
            if cd[i] == 0 and burst_cds[i] > 0:
                # 黑夜航母/轻母/装母禁主动释放必杀：不排相位，CD 继续转
                if attackers is not None and not is_day and attackers[i].ship_type in _NIGHT_BANNED_SHIP_TYPES:
                    continue
                out[i].append(t)
                cd[i] = int(burst_cds[i])
    return out


def optimize_encounter_timing(attackers, boss, day_time, course, air,
                               init_swap_schedule, burst_cds, total_time=150,
                               max_rounds=3, swap_shift_K=2,
                               flagship_idx=None, enemy_aura: dict = None,
                               enemy_periodic: dict = None) -> tuple:
    """以「真实 buff 感知总伤害」为目标，对 交换表 + 每船必杀相位延迟 做联合坐标上升搜索，
    求（局部）最优方案。返回 (best_swaps, best_burst_schedule, best_total)。

    动机：原 EncounterDP 用 buff 盲常数( burst_one )打分，只对「尽早放+前排时间多」优化，
    找到的必杀/交换时机并非真实总伤最优。本函数把已验证的 buff 感知模拟器当作打分器，
    直接最大化真实总伤——对齐 buff 窗口只是其自然结果。

    实现：坐标上升，循环若干轮，每轮依次
      (a) 优化每船必杀相位延迟 d∈[0,cd-1]（主要杠杆）；
      (b) 平移每个已有交换 ±K 秒（轻量，仅首轮尝试增/删一个交换）。
    从 DP 给出的交换表初值出发，只接受更优解，故绝不劣于 DP 方案。
    注：贪心局部最优，非全局保证；但目标即真实总伤，显著优于 buff 盲 DP。"""
    cur_swap = sorted(set(int(x) for x in init_swap_schedule))
    delays = {i: 0 for i in range(6)}
    _cache = {}

    def score(swap_sched, dl):
        key = (tuple(swap_sched), tuple(sorted(dl.items())))
        if key in _cache:
            return _cache[key]
        bs = _gen_burst_schedule_from_delays(burst_cds, swap_sched, dl, total_time,
                                             attackers=attackers, day_time=day_time)
        val = _simulate_encounter_buff_aware(
            attackers, boss, day_time, course, air,
            swap_schedule=swap_sched, burst_schedule=bs,
            flagship_idx=flagship_idx, enemy_aura=enemy_aura,
            enemy_periodic=enemy_periodic)["total"], bs
        _cache[key] = val
        return val

    cur_total, cur_bs = score(cur_swap, delays)

    for rnd in range(max_rounds):
        improved = False
        # (a) 每船必杀相位
        for i in range(6):
            if burst_cds[i] <= 0:
                continue
            best_d, best_t = delays[i], cur_total
            for d in range(0, int(burst_cds[i])):
                if d == delays[i]:
                    continue
                cand = dict(delays)
                cand[i] = d
                tt, _ = score(cur_swap, cand)
                if tt > best_t + 1e-6:
                    best_t, best_d = tt, d
            if best_d != delays[i]:
                delays[i] = best_d
                cur_total = best_t
                improved = True
        # (b) 平移每个交换 ±K
        for pos in range(len(cur_swap)):
            orig = cur_swap[pos]
            best_s, best_t = list(cur_swap), cur_total
            for delta in range(-swap_shift_K, swap_shift_K + 1):
                if delta == 0:
                    continue
                cand = sorted(set(list(cur_swap) + [orig + delta]))
                if not _valid_swap_schedule(cand, total_time):
                    continue
                tt, _ = score(cand, delays)
                if tt > best_t + 1e-6:
                    best_t, best_s = tt, list(cand)
            if best_s != cur_swap:
                cur_swap = best_s
                cur_total = best_t
                improved = True
        # (c) 仅首轮尝试增/删一个交换（交换收益通常很小，省时间）
        if rnd == 0:
            for pos in range(len(cur_swap)):
                cand = [s for j, s in enumerate(cur_swap) if j != pos]
                if _valid_swap_schedule(cand, total_time):
                    tt, _ = score(cand, delays)
                    if tt > cur_total + 1e-6:
                        cur_swap = cand
                        cur_total = tt
                        improved = True
            ins_cands = [5]
            for k in range(len(cur_swap)):
                nxt = cur_swap[k + 1] if k + 1 < len(cur_swap) else total_time - 1
                ins_cands.append((cur_swap[k] + nxt) // 2)
            ins_cands.append(total_time - 5)
            for ins in ins_cands:
                if ins < 5 or ins > total_time - 1:
                    continue
                cand = sorted(set(cur_swap + [ins]))
                if not _valid_swap_schedule(cand, total_time):
                    continue
                tt, _ = score(cand, delays)
                if tt > cur_total + 1e-6:
                    cur_swap = cand
                    cur_total = tt
                    improved = True
        if not improved:
            break

    best_bs = _gen_burst_schedule_from_delays(burst_cds, cur_swap, delays, total_time,
                                              attackers=attackers, day_time=day_time)
    return cur_swap, best_bs, cur_total


def render_timeline_svg(attackers, timeline, day_time, total_time, boss_name=""):
    """把遭遇战时间轴（_reconstruct_timeline 的返回值）渲染为 SVG 字符串。

    attackers: 6 个 CombatData（顺序即前排[0,1,2]+后排[3,4,5]）
    timeline : _reconstruct_timeline 的返回值 {swaps, burst_timings, events}
    时间轴只画两类事件：必杀技释放（每船一条泳道，圆点标注秒数）、交换前后排（红色竖虚线）。
    """
    swaps = sorted(set(timeline["swaps"]))
    burst_timings = timeline["burst_timings"]
    total = int(total_time)
    day = int(day_time)

    margin_left = 150
    margin_right = 50
    margin_top = 86
    axis_h = 38
    lane_h = 42
    margin_bottom = 54
    W = 1240
    plot_w = W - margin_left - margin_right
    plot_top = margin_top + axis_h
    plot_bottom = plot_top + len(attackers) * lane_h
    H = plot_bottom + margin_bottom

    def x(t):
        return margin_left + (t / total) * plot_w

    palette = ["#e4572e", "#2e86ab", "#3cb44b", "#f6a000", "#8e44ad", "#16a085"]

    parts = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">')
    parts.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

    # 标题
    title = "遭遇战操作时间轴"
    if boss_name:
        title += f" — Boss {boss_name}"
    parts.append(
        f'<text x="{margin_left}" y="34" font-size="21" font-weight="bold" fill="#222">{title}</text>')
    parts.append(
        f'<text x="{margin_left}" y="58" font-size="13" fill="#666">'
        f'白天 {day}s · 黑夜 {total - day}s · 总时间 {total}s · 仅显示「必杀技释放」与「交换前后排」</text>')

    # 白天/黑夜底色
    day_w = x(day) - margin_left
    night_w = x(total) - x(day)
    parts.append(f'<rect x="{margin_left}" y="{plot_top}" width="{day_w:.1f}" height="{plot_bottom - plot_top}" fill="#fff8e1"/>')
    parts.append(f'<rect x="{x(day):.1f}" y="{plot_top}" width="{night_w:.1f}" height="{plot_bottom - plot_top}" fill="#eef3fb"/>')
    parts.append(f'<text x="{margin_left + 10}" y="{plot_top + 20}" font-size="12" fill="#b8860b">☀ 白天</text>')
    parts.append(f'<text x="{x(day) + 10:.1f}" y="{plot_top + 20}" font-size="12" fill="#4169e1">🌙 黑夜</text>')

    # 时间轴基线 + 刻度（每 10s）
    parts.append(f'<line x1="{margin_left}" y1="{plot_top}" x2="{margin_left + plot_w:.1f}" y2="{plot_top}" stroke="#bbb"/>')
    for t in range(0, total + 1, 10):
        tx = x(t)
        parts.append(f'<line x1="{tx:.1f}" y1="{plot_top}" x2="{tx:.1f}" y2="{plot_top + 6}" stroke="#999"/>')
        anchor = "start" if t == 0 else ("end" if t == total else "middle")
        dx = 0 if anchor == "middle" else (-4 if anchor == "end" else 4)
        parts.append(f'<text x="{tx + dx:.1f}" y="{plot_top + 20}" font-size="11" fill="#666" text-anchor="{anchor}">{t}</text>')
    parts.append(
        f'<text x="{margin_left + plot_w / 2:.1f}" y="{plot_top + 34}" font-size="12" fill="#444" text-anchor="middle">时间 (s)</text>')

    # 黑夜分界线
    nx = x(day)
    parts.append(f'<line x1="{nx:.1f}" y1="{plot_top}" x2="{nx:.1f}" y2="{plot_bottom}" stroke="#4169e1" stroke-width="1.5" stroke-dasharray="2 3"/>')

    # 交换竖虚线 + hover 说明
    cur = set([0, 1, 2])
    swap_front_after = {}
    for s in sorted(swaps):
        cur = set(range(6)) - cur
        swap_front_after[s] = set(cur)
    for s in sorted(swaps):
        sx = x(s)
        front_names = "、".join(attackers[i].name for i in sorted(swap_front_after[s]))
        parts.append(f'<line x1="{sx:.1f}" y1="{plot_top}" x2="{sx:.1f}" y2="{plot_bottom}" stroke="#c0392b" stroke-width="1.3" stroke-dasharray="5 3">'
                     f'<title>第 {s}s 交换前后排 → 新前排: {front_names}</title></line>')
        parts.append(f'<text x="{sx:.1f}" y="{plot_top - 8}" font-size="11" fill="#c0392b" text-anchor="middle">交换</text>')

    # 每船一条泳道 + 必杀技圆点
    for i, atk in enumerate(attackers):
        lane_top = plot_top + i * lane_h
        ly = lane_top + lane_h / 2
        color = palette[i % len(palette)]
        parts.append(f'<line x1="{margin_left}" y1="{lane_top + lane_h:.1f}" x2="{margin_left + plot_w:.1f}" y2="{lane_top + lane_h:.1f}" stroke="#eee"/>')
        parts.append(f'<text x="{margin_left - 12}" y="{ly + 4:.1f}" font-size="12.5" fill="#333" text-anchor="end">{atk.name}</text>')
        ts = sorted(burst_timings.get(i, []))
        if len(ts) > 1:
            pts = " ".join(f"{x(t):.1f},{ly:.1f}" for t in ts)
            parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1" opacity="0.35"/>')
        for t in ts:
            bx = x(t)
            is_av = atk.ship_type in _NIGHT_BANNED_SHIP_TYPES
            delay = 2 if is_av else 1
            settle = t + delay
            note = (f"{atk.name} 第 {t}s 释放必杀技 → 结算第 {settle}s"
                    + ("（航空+必杀双延迟2s）" if is_av else "（必杀延迟1s）"))
            # 航空必杀双延迟：橙色外环区分
            if is_av:
                parts.append(f'<circle cx="{bx:.1f}" cy="{ly:.1f}" r="9.5" fill="none" stroke="#e67e22" stroke-width="1.6"/>')
            parts.append(f'<circle cx="{bx:.1f}" cy="{ly:.1f}" r="6.5" fill="{color}" stroke="#fff" stroke-width="1.5">'
                         f'<title>{note}</title></circle>')
            parts.append(f'<text x="{bx:.1f}" y="{ly - 11:.1f}" font-size="10" fill="#333" text-anchor="middle">{t}s</text>')
            # 结算时刻标签（延迟结算明显化）：橙色=航空双延迟，灰色=必杀单延迟
            settle_color = "#e67e22" if is_av else "#888"
            parts.append(f'<text x="{bx:.1f}" y="{ly + 13:.1f}" font-size="9" fill="{settle_color}" text-anchor="middle">↓{settle}s</text>')

    # 图例
    ly0 = H - 22
    parts.append(f'<circle cx="{margin_left}" cy="{ly0 - 4}" r="6.5" fill="#888"/>')
    parts.append(f'<text x="{margin_left + 14}" y="{ly0}" font-size="12.5" fill="#333">必杀技释放</text>')
    parts.append(f'<line x1="{margin_left + 130}" y1="{ly0 - 4}" x2="{margin_left + 156}" y2="{ly0 - 4}" stroke="#c0392b" stroke-width="1.3" stroke-dasharray="5 3"/>')
    parts.append(f'<text x="{margin_left + 164}" y="{ly0}" font-size="12.5" fill="#333">交换前后排</text>')
    parts.append(f'<rect x="{margin_left + 290}" y="{ly0 - 13}" width="16" height="11" fill="#fff8e1" stroke="#ddd"/>')
    parts.append(f'<text x="{margin_left + 312}" y="{ly0}" font-size="12.5" fill="#333">白天</text>')
    parts.append(f'<rect x="{margin_left + 372}" y="{ly0 - 13}" width="16" height="11" fill="#eef3fb" stroke="#ddd"/>')
    parts.append(f'<text x="{margin_left + 394}" y="{ly0}" font-size="12.5" fill="#333">黑夜</text>')
    # 必杀延迟结算图例
    parts.append(f'<circle cx="{margin_left + 510:.1f}" cy="{ly0 - 4}" r="6.5" fill="#aaa" stroke="#fff" stroke-width="1.5"/>')
    parts.append(f'<circle cx="{margin_left + 510:.1f}" cy="{ly0 - 4}" r="9.5" fill="none" stroke="#e67e22" stroke-width="1.6"/>')
    parts.append(f'<text x="{margin_left + 524:.1f}" y="{ly0}" font-size="12.5" fill="#333">必杀延迟结算（橙环=航空类必杀，延迟统一1s）</text>')

    parts.append('</svg>')
    return "\n".join(parts)


def simulate_encounter(app, boss_name: str, day_time: float, night_time: float = None,
                       front_ships: List[str] = None, back_ships: List[str] = None,
                       course: str = "T优", air: str = "确保",
                       flagship_name: str = "") -> str:
    """遭遇战模拟主入口。
    返回格式化的时间轴和伤害明细字符串。
    flagship_name：被选为旗舰的角色名（可空），其旗舰光环对全队生效。
    黑夜时间由白天自动推导：黑夜 = 150 - 白天，无需（也不要求）用户输入黑夜。"""
    front_ships = front_ships or []
    back_ships = back_ships or []
    # 黑夜时间自动由白天推导：黑夜 = 150 - 白天（不再要求用户输入黑夜）
    day_time = float(day_time)
    if day_time < 0 or day_time > 150:
        return f"白天时间需在 0~150s 之间，当前白天={day_time}s"
    night_time = 150.0 - day_time
    total_time = 150.0
    # 航向键归一化：常量表用 "T优势"/"T劣势"，前端/默认用 "T优"/"T劣"，统一为常量键
    # 否则 COURSE_DAMAGE_MOD.get("T优") 取不到加成（恒为 0）
    _COURSE_ALIAS = {"T优": "T优势", "T劣": "T劣势", "T劣势优势": "T优势"}
    course = _COURSE_ALIAS.get(course, course)
    if len(front_ships) != 3 or len(back_ships) != 3:
        return "前排和后排必须各 3 个角色"
    if len(set(front_ships) & set(back_ships)) > 0:
        return "前排和后排角色不能重复"

    # 加载 boss（统一从 知识库/底层数据/拉古兹.json 识别）
    bosses = load_encounter_bosses()
    resolved = resolve_encounter_boss(bosses, boss_name)
    if resolved is None:
        avail = ", ".join(sorted(bosses.keys())) or "（无）"
        return f"未找到 boss: {boss_name}。可用 boss: {avail}"
    if resolved != boss_name:
        boss_name = resolved  # 识别兜底：用文件中的标准名继续

    boss = bosses[boss_name]

    # 加载 6 个角色（叠加 raguz_bonus）
    all_names = list(front_ships) + list(back_ships)
    attackers = []
    for name in all_names:
        atk = load_encounter_attacker(app, name, boss.get("level", _ENCOUNTER_BOSS_LEVEL))
        if atk is None:
            return f"未找到角色: {name}，请检查角色名。"
        attackers.append(atk)

    # 旗舰：先用 name_matcher 归一化旗舰名，再按范围/条件广播旗舰光环
    flagship_aura: dict = {}
    flagship_result: dict = {}
    # 旗舰索引（技能 CD 效果的"旗舰/指定旗舰名"目标定位用）
    flagship_idx = None
    if flagship_name:
        flagship_canon = app.get_ship_from_all(flagship_name)
        if flagship_canon is None:
            return f"未找到旗舰角色: {flagship_name}，请检查角色名。"
        flagship_result = apply_flagship_aura(attackers, flagship_canon.name,
                                               phase="night_only")
        flagship_aura = flagship_result.get("team", {}) or {}
        for _i, _a in enumerate(attackers):
            if (_a.name or "") == (flagship_canon.name or ""):
                flagship_idx = _i
                break

    # 预计算 DPS 表（带上旗舰技对敌方的减益光环）
    _enemy_aura = (flagship_result or {}).get("enemy") or {}
    _enemy_per = (flagship_result or {}).get("periodic") or {}
    dps_table = build_dps_table(attackers, boss, day_time, course, air,
                                enemy_aura=_enemy_aura, enemy_periodic=_enemy_per)

    # 1s 粒度 DP 求解（产出交换表初值）
    dp = EncounterDP(attackers, dps_table, day_time, total_time)
    best = dp.solve()
    base_timeline = _reconstruct_timeline(best.get("path", []), attackers)

    # 以「真实 buff 感知总伤害」为目标，联合优化 交换表 + 每船必杀相位（≥CD），取最大总伤
    burst_cds = {i: int(dps_table[(i, True, True)]["burst_cd"]) for i in range(6)}
    opt_swaps, opt_burst_schedule, _opt_total = optimize_encounter_timing(
        attackers, boss, day_time, course, air,
        init_swap_schedule=base_timeline["swaps"],
        burst_cds=burst_cds, total_time=int(total_time),
        flagship_idx=flagship_idx,
        enemy_aura=_enemy_aura, enemy_periodic=_enemy_per,
    )
    # 从优化后的时间表跑 buff 感知模拟（一次性作为所有输出的唯一真相源）
    sim = _simulate_encounter_buff_aware(
        attackers, boss, day_time, course, air,
        swap_schedule=opt_swaps,
        burst_schedule=opt_burst_schedule,
        flagship_idx=flagship_idx,
        enemy_aura=_enemy_aura, enemy_periodic=_enemy_per,
    )

    # 构造优化后的时间轴，供 文字轴/明细/SVG 一致使用。
    # 必杀事件以「模拟器实际接受的释放」为准（p["t"]=释放时刻）；
    # 夜间航母/轻母/装母的必杀在模拟器里被 release_allowed 过滤，不会出现在这里。
    _burst_events = [("burst", p["t"], (p["idx"],))
                     for p in sim["per_release"] if p["kind"] == "burst"]
    opt_timeline = _reconstruct_timeline(
        [("swap", t) for t in opt_swaps] + _burst_events,
        attackers,
    )

    # 主输出：操作时间轴 + 伤害明细（使用 buff 感知的角色伤害与总伤害）
    text = _format_timeline(best, attackers, boss, day_time, total_time, dps_table, course, air,
                            flagship_name=flagship_name, flagship_aura=flagship_aura,
                            buff_aware_role_damage=sim["role_damage"],
                            buff_aware_total=sim["total"],
                            timeline_override=opt_timeline,
                            swap_log=sim.get("swap_log"))
    # 折叠模块：逐次释放的完整运算式子（含技能绑定 buff 窗口）
    detail = build_detail_module(attackers, boss, day_time, course, air,
                                 flagship_name=flagship_name, flagship_aura=flagship_aura,
                                 sim=sim)
    # 操作时间轴 SVG：与文字轴/明细共用同一份优化后的时间表（opt_timeline）
    svg = render_timeline_svg(attackers, opt_timeline, day_time, total_time, boss_name=boss_name)
    return {"text": text, "detail": detail, "svg": svg}


def _apply_default_talent_pvp(app, atk, name):
    """把角色默认 PVP 天赋（talent_pvp.json 推荐编码）应用到攻击方对象：
    面板绝对值叠加 + buffs 叠加 + 舰种增伤叠加 + 技能 CD 缩减（四舍五入取整）。
    无推荐/无天赋时静默跳过。"""
    try:
        from tools.talent import resolve_default_talent, apply_cd_effects
        r = resolve_default_talent(app, name)
    except Exception:
        return
    if not r['ok']:
        return
    for k, v in (r['panel'] or {}).items():
        if k in atk.attack_panel:
            atk.attack_panel[k] = atk.attack_panel.get(k, 0.0) + v
        elif k in atk.defense_panel:
            atk.defense_panel[k] = atk.defense_panel.get(k, 0.0) + v
    for k, v in (r['buffs'] or {}).items():
        atk.buffs[k] = atk.buffs.get(k, 0.0) + v
    for k, v in (r['ship_bonus'] or {}).items():
        atk.ship_type_damage_bonus[k] = atk.ship_type_damage_bonus.get(k, 0.0) + v
    # 技能 CD 缩减（百分比 → 四舍五入取整为实际 CD）
    if r.get('cd'):
        apply_cd_effects(atk, r['cd'])


def simulate_pvp(app, defender_name: str, day_time: float, front_ships: List[str],
                 back_ships: List[str], flagship_name: str = '',
                 course: str = '同航', air: str = '均势',
                 night_time: float = None) -> str:
    """模拟组队：6 人（可带 1 名旗舰）打 1 个防守角色。
    规则（用户约定）：6 人一律按前排逻辑（普攻/通常技/必杀技，必杀按 CD 自动释放），
    不算支援技、无 buff 输入；天赋采用各角色默认 PVP 推荐点法（talent_pvp.json）。
    战斗时长：总时长固定 120s，分白天/黑夜两段——白天由用户输入（0~120），
    黑夜 = 120 − 白天（自动推导，无需输入）；黑夜防守方装甲按白天 10% 计。
    防守方技能（自身/全队持续增益，如 全伤害减免）按其自身 CD 自动释放并进时间轴，
    生效窗口内攻击方伤害按防守方实际面板计算。"""
    defender_data = app.get_ship_for_calc(defender_name)
    # 信息不全拦截：缺数据等同"没有该角色"（口径与未找到一致）
    if defender_data is None or not app.is_ship_complete(defender_name):
        return f"未找到防守角色: {defender_name}，请检查角色名。"
    enemy = app._build_enemy_from_data(defender_data)
    # 防守方默认 PVP 天赋：减免/回避/防暴击写入敌方属性
    _talent_txt = []
    try:
        from tools.talent import resolve_default_talent
        dr = resolve_default_talent(app, defender_name)
        if dr['ok']:
            for k, v in (dr['enemy'] or {}).items():
                enemy[k] = enemy.get(k, 0.0) + v
            # 回避率/防暴击率（被攻击方属性）用面板字段名落地：「X增加」写法统一折算，
            # 供伤害公式读 enemy['回避率']（用户口径 2026-09-14）
            for k, v in (dr['buffs'] or {}).items():
                if k in ('回避率增加', '防暴击率', '防暴击率增加'):
                    ek = {'回避率增加': '回避率', '防暴击率增加': '防暴击率'}.get(k, k)
                    enemy[ek] = enemy.get(ek, 0.0) + v
            if dr['names']:
                _talent_txt.append('防守方天赋: ' + '、'.join(dr['names']))
    except Exception:
        pass

    all_names = list(front_ships) + list(back_ships)
    if len(all_names) != 6 or len(front_ships) != 3 or len(back_ships) != 3:
        return "需要 6 个角色：前排 3 + 后排 3。"
    attackers = []
    for name in all_names:
        atk = app.get_ship_for_calc(name)
        # 信息不全拦截：缺数据等同"没有该角色"（口径与未找到一致）
        if atk is None or not app.is_ship_complete(name):
            return f"未找到角色: {name}，请检查角色名。"
        atk = copy.deepcopy(atk)
        # 攻击方默认 PVP 天赋：面板/buffs/舰种增伤
        _apply_default_talent_pvp(app, atk, name)
        attackers.append(atk)

    flagship_aura = {}
    _res = {}
    if flagship_name:
        fdata = app.get_ship_for_calc(flagship_name)
        if fdata is None:
            return f"未找到旗舰角色: {flagship_name}，请检查角色名。"
        # PVP（演习/竞技）场景：带「演习、竞技中」条件的光环在此生效
        _res = apply_flagship_aura(attackers, fdata.name, ctx={"is_pvp": True},
                                   phase="pvp")
        flagship_aura = _res.get("team", {})
    # 旗舰索引（技能 CD 效果的"旗舰/指定旗舰名"目标定位用）
    pvp_flagship_idx = None
    if flagship_name:
        for _i, _a in enumerate(attackers):
            if (_a.name or "") == (fdata.name or ""):
                pvp_flagship_idx = _i
                break

    duration = float(day_time if day_time is not None else 60)
    if duration < 0 or duration > 120:
        return f"白天战斗时长需在 0~120s 之间，当前白天={duration:.0f}s（黑夜 = 120 - 白天）"
    if night_time is None:
        night_time = 120.0 - duration
    total_time = int(round(duration + float(night_time)))
    rows = []
    # 逐秒时间轴推演（PVP 模式：全前排、无交换、必杀按 CD 自动循环）
    burst_cds = [float((atk.burst_attack or {}).get("cd", 0) or 0) for atk in attackers]
    burst_schedule = _gen_burst_schedule_from_delays(
        burst_cds, [], {i: 0 for i in range(6)},
        total_time, attackers=attackers, day_time=int(duration))
    sim = _simulate_encounter_buff_aware(
        attackers, enemy, int(duration), course, air,
        swap_schedule=[], burst_schedule=burst_schedule,
        total_time=total_time, all_front=True,
        flagship_idx=pvp_flagship_idx,
        enemy_aura=(_res or {}).get("enemy") or None,
        enemy_periodic=(_res or {}).get("periodic") or None,
        defender_ship=copy.deepcopy(defender_data))
    total = sim.get("total", 0.0)
    role_damage = sim.get("role_damage", {})
    for i, atk in enumerate(attackers):
        d = role_damage.get((i, True), 0.0)
        rows.append((atk.name, d / max(total_time, 1), d))
        total = max(total, 0.0)

    lines = []
    lines.append("=" * 64)
    lines.append(f"  模拟组队：6人组队 → {enemy.get('name') or defender_name}")
    lines.append(f"  战斗时长: 白天 {int(duration)}s | 黑夜 {int(night_time)}s | 总时长 {total_time}s"
                 f"（黑夜 = 120 - 白天） | 航向: {course} | 制空: {air} | 逐秒时间轴")
    lines.append("=" * 64)
    lines.append(f"  {'角色':<12}{'总伤害':>14}")
    lines.append("-" * 64)
    for name, d, dmg in rows:
        lines.append(f"  {name:<12}{dmg:>14,.0f}")
    lines.append("-" * 64)
    lines.append(f"  全队总伤害: {total:,.0f}")
    if flagship_aura:
        aura_txt = '、'.join(
            f"{k}{v * 100:.0f}%" if abs(v) < 2 else f"{k}{v:.2f}"
            for k, v in flagship_aura.items())
        lines.append(f"  旗舰光环（{flagship_name}）: {aura_txt}")
    for txt in _talent_txt:
        lines.append(f"  {txt}")
    lines.append("=" * 64)
    lines.append("（逐秒时间轴推演；全出战位、无交换、不算支援技；普攻/通常技延后1s结算，必杀按CD自动释放；")
    lines.append(" 天赋=默认PVP推荐；无 buff 输入、无拉古兹增伤；防守方技能增益按CD自动释放并进时间轴）")
    text = "\n".join(lines)
    # 详细伤害计算（逐次释放，含技能绑定 buff 窗口）——与遭遇战一致供前端折叠
    try:
        detail = build_detail_module(attackers, enemy, int(duration), course, air,
                                     flagship_name, flagship_aura, sim, is_pvp=True,
                                     night_time=int(night_time), total_time=total_time)
    except Exception:
        detail = ""
    return {"text": text, "detail": detail}

"""
战斗时间轴模拟器 - 技能持续 buff（skill_buffs）生效/到期机制

对单个攻击方 vs 单个敌方，按 1 秒粒度推进战斗：
- 普攻 / 通常技 / 支援技按 CD 自动触发；必杀技按指定时间点（或按 CD 自动）触发
- 技能释放时，把该技能关联的 skill_buffs 注册到时间轴（start_time + duration）
- 每秒钟构造"时点视图"：攻击方 buffs = 常驻 buffs + 生效中的 self 持续 buff；
  敌方属性 = 基础属性 + 生效中的 enemy 持续减益
- 到期自动移除，不影响后续计算

用法：
    result = simulate_battle(attacker, enemy, duration=60, burst_times=[20, 55])
"""
import copy
from typing import Dict, List, Optional

from core.combat_data import CombatData
from core.damage_formula import DamageCalculator
from core.output_handler import OutputHandler
from core.skill_buff_extractor import match_ship_class


# 敌方持续减益键 → 敌方字典属性应用方式
#   ("percent", key): 敌方[attr] *= (1 + v)      （v 为负表示削弱，如 装甲值-15% → *0.85）
#   ("flat", key):     敌方[attr] += v
_ENEMY_BUFF_APPLY = {
    "装甲值百分比增加": ("percent", "装甲"),
    "装甲值固定增加": ("flat", "装甲"),
    "机动值百分比增加": ("percent", "机动"),
    "机动值固定增加": ("flat", "机动"),
    "命中值百分比增加": ("percent", "命中"),
    "回避率增加": ("flat", "回避率"),
    "全伤害减免": ("flat", "全伤害减免"),
    "炮击伤害减免": ("flat", "炮击伤害减免"),
    "雷击伤害减免": ("flat", "雷击伤害减免"),
    "航空伤害减免": ("flat", "航空伤害减免"),
    "轰炸伤害减免": ("flat", "轰炸伤害减免"),
    "鱼雷伤害减免": ("flat", "鱼雷伤害减免"),
    "近战伤害减免": ("flat", "近战伤害减免"),
    "防暴击率增加": ("flat", "防暴击率"),
    # 旗舰技 enemy 光环常见键（如埃塞克斯「敌方单位【对空-20%】」）
    "对空值百分比增加": ("percent", "对空"),
    "对空值固定增加": ("flat", "对空"),
}


class BuffWindow:
    """一个生效中的持续 buff 实例（技能释放时创建，duration 秒后到期）。"""

    def __init__(self, skill_buff: dict, start_time: float):
        self.trigger = skill_buff.get("trigger", "burst")
        self.target = skill_buff.get("target", "self")
        self.duration = float(skill_buff.get("duration", 0))
        self.buffs = dict(skill_buff.get("buffs", {}))
        self.start_time = float(start_time)

    @property
    def expire_time(self) -> float:
        return self.start_time + self.duration


class BattleSimulator:
    """1 秒粒度战斗模拟：自动普攻/通常技/支援技 + 指定时机必杀技，含持续 buff 时间轴。"""

    def __init__(self, attacker: CombatData, enemy: dict,
                 course: str = "同航", air: str = "均势"):
        self.base_attacker = attacker
        self.base_enemy = dict(enemy)
        self.course = course
        self.air = air
        # 生效中的 buff 窗口
        self._self_windows: List[BuffWindow] = []   # target=self
        self._enemy_windows: List[BuffWindow] = []  # target=enemy

    # ---------- buff 时间轴 ----------

    def register_skill_buffs(self, trigger: str, cast_time: float):
        """技能释放时注册该技能关联的持续 buff（只匹配 trigger）。
        team 类带范围从句（国籍/舰种/舰级/指定角色）时，1v1 中仅攻击方自身命中才生效。"""
        for sb in getattr(self.base_attacker, "skill_buffs", []) or []:
            if sb.get("trigger") != trigger:
                continue
            w = BuffWindow(sb, cast_time)
            if w.duration <= 0:
                continue
            if w.target == "enemy":
                self._enemy_windows.append(w)
            elif w.target == "team":
                if sb.get("scope") and not _buff_scope_targets([self.base_attacker], sb["scope"]):
                    continue
                self._self_windows.append(w)
            else:
                self._self_windows.append(w)

    def _prune(self, t: float):
        self._self_windows = [w for w in self._self_windows if w.expire_time > t]
        self._enemy_windows = [w for w in self._enemy_windows if w.expire_time > t]

    # ---------- 时点视图 ----------

    def _attacker_view(self) -> CombatData:
        """深拷贝攻击方，并叠加当前生效的 self 持续 buff。"""
        view = copy.deepcopy(self.base_attacker)
        if self._self_windows:
            view.buffs = dict(self.base_attacker.buffs)
            for w in self._self_windows:
                for k, v in w.buffs.items():
                    view.buffs[k] = view.buffs.get(k, 0.0) + v
        return view

    def _enemy_view(self) -> dict:
        """深拷贝敌方，并叠加当前生效的 enemy 持续减益。"""
        if not self._enemy_windows:
            return dict(self.base_enemy)
        view = dict(self.base_enemy)
        for w in self._enemy_windows:
            for k, v in w.buffs.items():
                apply = _ENEMY_BUFF_APPLY.get(k)
                if apply is None:
                    continue
                kind, attr = apply
                if kind == "percent":
                    view[attr] = view.get(attr, 0.0) * (1.0 + v)
                else:
                    view[attr] = view.get(attr, 0.0) + v
        return view

    # ---------- 伤害 ----------

    @staticmethod
    def _expected(factors) -> float:
        if not factors:
            return 0.0
        if factors.get("type") == "混合技能":
            return sum(OutputHandler.calculate_final_damage(p)["expected"]
                       for p in factors.get("parts", []))
        return OutputHandler.calculate_final_damage(factors)["expected"]

    def _normal_damage(self) -> float:
        view = self._attacker_view()
        enemy = self._enemy_view()
        return sum(self._expected(f)
                   for f in DamageCalculator.calculate_normal_all(view, enemy, self.course, self.air) or [])

    @staticmethod
    def _agg_buffs(windows) -> dict:
        """把生效中的 buff 窗口聚合为一个 dict（用于逐次释放记录）。"""
        agg = {}
        for w in windows:
            for k, v in w.buffs.items():
                agg[k] = agg.get(k, 0.0) + v
        return agg

    def _skill_calc(self, which: str):
        """计算单次技能释放，返回 (factors, 期望伤害)。条件增伤仅该技能出手生效。"""
        view = self._attacker_view()
        enemy = self._enemy_view()
        atk_dict = {"skill": self.base_attacker.skill_attack,
                    "burst": self.base_attacker.burst_attack,
                    "support": self.base_attacker.support_attack}[which]
        calc = {
            "skill": DamageCalculator.calculate_skill,
            "burst": DamageCalculator.calculate_burst,
            "support": DamageCalculator.calculate_support,
        }[which]
        factors = calc(_with_static_buffs(atk_dict, view), enemy, self.course, self.air)
        return factors, self._expected(factors)

    def _skill_damage(self, which: str) -> float:
        _, dmg = self._skill_calc(which)
        return dmg

    # ---------- 主循环 ----------

    def simulate(self, duration: float = 60.0, burst_times: Optional[List[int]] = None,
                 use_skill: bool = True, use_support: bool = True,
                 use_buffs: bool = True) -> dict:
        """按 1s 粒度模拟战斗。

        - burst_times: 必杀技释放秒列表；为 None 时按 CD 从第 0 秒自动释放
        - use_buffs: False 时忽略 skill_buffs（用于对比"无持续 buff"基线）
        - 返回 {total, detail: {sec: {sec_damage, active_self_buffs, active_enemy_buffs}}, casts}
        """
        duration = int(duration)
        if not use_buffs:
            self._self_windows = []
            self._enemy_windows = []

        total = 0.0
        detail = {}
        # 逐次释放记录：{t, kind, factors, damage, active_self, active_enemy}（供调试逐算子输出）
        per_release = []
        casts = {"normal": 0, "skill": 0, "burst": 0, "support": 0}
        total_burst = 0
        total_normal = 0.0
        total_skill = 0.0
        total_support = 0.0

        # 技能 CD 计时器
        skill_cd = 0
        support_cd = 0
        burst_cd = 0
        normal_cds = [0] * len(self.base_attacker.normal_attacks)

        burst_attack = self.base_attacker.burst_attack or {}
        burst_interval = float(burst_attack.get("cd", 0) or 0)
        skill_attack = self.base_attacker.skill_attack or {}
        skill_interval = float(skill_attack.get("cd", 0) or 0)
        support_attack = self.base_attacker.support_attack or {}
        support_interval = float(support_attack.get("cd", 0) or 0)

        burst_set = set(burst_times) if burst_times is not None else None

        for t in range(duration):
            self._prune(t)
            sec_dmg = 0.0

            # 必杀技（手动时间点 或 按 CD 自动：0, cd, 2cd...）
            burst_now = False
            if burst_interval > 0 and (self.base_attacker.burst_attack or {}).get("parts"):
                if burst_set is not None:
                    if t in burst_set:
                        burst_now = True
                else:
                    if burst_cd <= 0:
                        burst_now = True
            if burst_now:
                factors, dmg = self._skill_calc("burst")
                total_burst += dmg
                sec_dmg += dmg
                casts["burst"] += 1
                # 记录释放前 buff 视图（与六船模拟口径一致：先结算本次，再注册 buff）
                per_release.append({"t": t, "kind": "burst", "factors": factors, "damage": dmg,
                                    "active_self": self._agg_buffs(self._self_windows),
                                    "active_enemy": self._agg_buffs(self._enemy_windows)})
                if use_buffs:
                    self.register_skill_buffs("burst", float(t))
                burst_cd = burst_interval

            # 通常技（按 CD：首次 cd 秒时触发）
            if use_skill and skill_interval > 0 and skill_attack.get("parts"):
                if skill_cd <= 0:
                    factors, dmg = self._skill_calc("skill")
                    total_skill += dmg
                    sec_dmg += dmg
                    casts["skill"] += 1
                    per_release.append({"t": t, "kind": "skill", "factors": factors, "damage": dmg,
                                        "active_self": self._agg_buffs(self._self_windows),
                                        "active_enemy": self._agg_buffs(self._enemy_windows)})
                    if use_buffs:
                        self.register_skill_buffs("skill", float(t))
                    skill_cd = skill_interval

            # 支援技（按 CD：首次 cd 秒时触发）
            if use_support and support_interval > 0 and support_attack.get("parts"):
                if support_cd <= 0:
                    factors, dmg = self._skill_calc("support")
                    total_support += dmg
                    sec_dmg += dmg
                    casts["support"] += 1
                    per_release.append({"t": t, "kind": "support", "factors": factors, "damage": dmg,
                                        "active_self": self._agg_buffs(self._self_windows),
                                        "active_enemy": self._agg_buffs(self._enemy_windows)})
                    if use_buffs:
                        self.register_skill_buffs("support", float(t))
                    support_cd = support_interval

            # 普攻（每武器独立 CD，首次第 0 秒触发）
            view = self._attacker_view()
            enemy = self._enemy_view()
            norm_factors = DamageCalculator.calculate_normal_all(view, enemy, self.course, self.air) or []
            for i, f in enumerate(norm_factors):
                cd = float(f.get("cd", 0) or 0)
                if i < len(normal_cds) and normal_cds[i] > 0:
                    continue
                if cd <= 0:
                    continue
                d = self._expected(f)
                total_normal += d
                sec_dmg += d
                casts["normal"] += 1
                normal_cds[i] = cd

            # 记录每秒伤害与生效 buff
            self_b = {}
            for w in self._self_windows:
                for k, v in w.buffs.items():
                    self_b[k] = self_b.get(k, 0.0) + v
            enemy_b = {}
            for w in self._enemy_windows:
                for k, v in w.buffs.items():
                    enemy_b[k] = enemy_b.get(k, 0.0) + v
            detail[t] = {
                "sec_damage": sec_dmg,
                "active_self_buffs": self_b,
                "active_enemy_buffs": enemy_b,
            }

            # CD 递减
            skill_cd = max(0, skill_cd - 1)
            support_cd = max(0, support_cd - 1)
            burst_cd = max(0, burst_cd - 1)
            normal_cds = [max(0, c - 1) for c in normal_cds]

        total = total_burst + total_skill + total_support + total_normal
        return {
            "total": total,
            "burst": total_burst,
            "skill": total_skill,
            "support": total_support,
            "normal": total_normal,
            "casts": casts,
            "detail": detail,
            "per_release": per_release,
        }


def simulate_battle(attacker: CombatData, enemy: dict, duration: float = 60.0,
                    burst_times: Optional[List[int]] = None, use_skill: bool = True,
                    use_support: bool = True, use_buffs: bool = True,
                    course: str = "同航", air: str = "均势") -> dict:
    """便捷入口：模拟一场战斗，返回结果 dict。"""
    sim = BattleSimulator(attacker, enemy, course=course, air=air)
    return sim.simulate(duration=duration, burst_times=burst_times,
                        use_skill=use_skill, use_support=use_support,
                        use_buffs=use_buffs)


def verify_buff_windows(attacker: CombatData, enemy: dict, duration: float = 60.0,
                        burst_times: Optional[List[int]] = None) -> str:
    """验证持续 buff 生效/到期：输出带 buff 与不带 buff 的伤害对比和 buff 窗口。"""
    lines = []
    with_buffs = simulate_battle(attacker, enemy, duration=duration, burst_times=burst_times)
    without = simulate_battle(attacker, enemy, duration=duration, burst_times=burst_times,
                              use_buffs=False)
    lines.append("=" * 56)
    lines.append(f"持续 buff 验证: {attacker.name} vs 敌方")
    lines.append("=" * 56)
    lines.append(f"总时长 {int(duration)}s | 总伤害(带buff)={with_buffs['total']:.0f} | "
                 f"总伤害(无buff)={without['total']:.0f} | "
                 f"差值={with_buffs['total'] - without['total']:.0f}")
    lines.append(f"分项(带buff): 普攻={with_buffs['normal']:.0f} 通常技={with_buffs['skill']:.0f} "
                 f"必杀技={with_buffs['burst']:.0f} 支援技={with_buffs['support']:.0f}")

    # buff 窗口摘要：按秒聚合
    prev_self = {}
    prev_enemy = {}
    windows = []
    for t in sorted(with_buffs["detail"].keys()):
        d = with_buffs["detail"][t]
        if d["active_self_buffs"] != prev_self or d["active_enemy_buffs"] != prev_enemy:
            windows.append((t, dict(d["active_self_buffs"]), dict(d["active_enemy_buffs"])))
            prev_self = d["active_self_buffs"]
            prev_enemy = d["active_enemy_buffs"]
    lines.append(f"--- buff 生效窗口（变化点）---")
    for t, sb, eb in windows:
        parts = []
        if sb:
            parts.append("self: " + ", ".join(f"{k}{v:+.0%}" if abs(v) < 10 else f"{k}{v:+.0f}" for k, v in sb.items()))
        if eb:
            parts.append("enemy: " + ", ".join(f"{k}{v:+.0%}" if abs(v) < 10 else f"{k}{v:+.0f}" for k, v in eb.items()))
        lines.append(f"  第{t:>3}s: {'; '.join(parts) if parts else '（无）'}")
    return "\n".join(lines)

