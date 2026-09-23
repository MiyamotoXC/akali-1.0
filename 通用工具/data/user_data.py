# -*- coding: utf-8 -*-
"""
用户本地副本（user_data.json）：所有计算的用户面板真相源。

设计约束：
- 查询（面板展示）一律走底层 data.json，不受副本影响；
- 只有「更新工具」（save_user_ship / web 管理接口）允许更改副本；
- 计算路径 get_ship_for_calc 以副本为基础，副本缺失回退底层；
- 底层数据更新后副本不自动同步（副本即用户基线）；删除 user_data.json 即恢复底层默认。
"""
import copy
import json
import os

# user_data.py 位于 通用工具/data/，上溯三级到项目根（阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
USER_DATA_FILE = os.path.join(_PROJECT_ROOT, "用户端特用", "user_data.json")
# 底层标准计算副本（副本初始化源 + diff 基准）
_DATA_FILE = os.path.join(_PROJECT_ROOT, "知识库", "底层数据", "产物", "data.json")

# 底层 data.json 缓存（mtime 感知，diff/list 批量查询避免反复全量读取）
_BASE_CACHE = None
_BASE_MTIME = None

# 用户面板数值统一钳制：不得为负，且不超过 20000（用户口径 2026-09-17）
_PANEL_MIN = 0.0
_PANEL_MAX = 20000.0


def _clamp_panel(v):
    """面板数值钳制到 [0, 20000]；无法解析或 NaN 返回 None（调用方跳过该项）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:            # NaN
        return None
    return max(_PANEL_MIN, min(_PANEL_MAX, x))


def _load_base() -> dict:
    """读取底层 data.json 全量（带 mtime 缓存）。"""
    global _BASE_CACHE, _BASE_MTIME
    if not os.path.exists(_DATA_FILE):
        return {}
    cur = os.path.getmtime(_DATA_FILE)
    if _BASE_CACHE is not None and _BASE_MTIME == cur:
        return _BASE_CACHE
    try:
        with open(_DATA_FILE, encoding="utf-8") as f:
            _BASE_CACHE = json.load(f)
    except Exception:
        _BASE_CACHE = {}
    _BASE_MTIME = cur
    return _BASE_CACHE


def load_user_data() -> dict:
    """读取用户副本全文；文件缺失返回空结构（初始化由 ensure_copy 负责）。"""
    if not os.path.exists(USER_DATA_FILE):
        return {"ships": {}}
    try:
        with open(USER_DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"ships": {}}
    except Exception:
        return {"ships": {}}


def save_user_data(data: dict) -> bool:
    """整体写回用户副本。"""
    try:
        os.makedirs(os.path.dirname(USER_DATA_FILE), exist_ok=True)
        with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def ensure_copy() -> None:
    """首次运行初始化：把底层 data.json 全量拷贝为用户副本（副本即用户基线）。"""
    if os.path.exists(USER_DATA_FILE):
        return
    base = _load_base()
    if base:
        save_user_data({"ships": copy.deepcopy(base)})


def get_ships() -> dict:
    """副本中的全部角色条目 {角色名: 战斗数据 dict}。"""
    return load_user_data().setdefault("ships", {})


def get_ship_dict(name):
    """取副本中指定角色的完整战斗数据 dict（可直接 CombatData.from_dict）；无则 None。"""
    entry = get_ships().get(name)
    return copy.deepcopy(entry) if entry is not None else None


def apply_panel_to(base, atk_set: dict, def_set: dict, d_atk: dict, d_def: dict) -> bool:
    """把面板赋值/增量写入副本（base 为该角色当前数据：dict 或 CombatData）。
    绝对赋值：atk_set/def_set 直接覆盖；增量：d_atk/d_def 在副本当前值上累加。
    写回 user_data.json，成功返回 True。"""
    # 统一为 dict（CombatData 走 to_dict，保留完整字段）
    if not isinstance(base, dict):
        try:
            base = base.to_dict()
        except Exception:
            return False
    entry = copy.deepcopy(base)
    entry.setdefault('attack_panel', {})
    entry.setdefault('defense_panel', {})
    # 用户输入统一钳制到 [0, 20000]（不得为负、不超过上限）——用户口径 2026-09-17
    for k, v in (atk_set or {}).items():
        c = _clamp_panel(v)
        if c is not None:
            entry['attack_panel'][k] = c
    for k, v in (def_set or {}).items():
        c = _clamp_panel(v)
        if c is not None:
            entry['defense_panel'][k] = c
    # 增量在副本当前值上累加后同样钳制到 [0, 20000]（负增量致负则归 0）
    for k, v in (d_atk or {}).items():
        cur = float(entry['attack_panel'].get(k, 0) or 0)
        c = _clamp_panel(cur + float(v))
        if c is not None:
            entry['attack_panel'][k] = c
    for k, v in (d_def or {}).items():
        cur = float(entry['defense_panel'].get(k, 0) or 0)
        c = _clamp_panel(cur + float(v))
        if c is not None:
            entry['defense_panel'][k] = c
    data = load_user_data()
    data.setdefault('ships', {})[entry.get('name') or ''] = entry
    return save_user_data(data)


def diff_panels(name: str) -> dict:
    """副本与底层的面板差异（展示/管理用）。
    base_* = 底层 data.json 面板；cur_* = 副本当前面板（副本缺失字段回退底层）；
    delta_* = cur - base（仅列出有差异的字段，已 round 两位）。"""
    base = _load_base().get(name) or {}
    cur = get_ships().get(name) or {}
    base_atk = dict(base.get('attack_panel') or {})
    base_def = dict(base.get('defense_panel') or {})
    cur_atk = {**base_atk, **dict(cur.get('attack_panel') or {})}
    cur_def = {**base_def, **dict(cur.get('defense_panel') or {})}
    delta_atk = {k: round(cur_atk[k] - base_atk.get(k, 0), 2) for k in cur_atk
                 if abs(cur_atk[k] - base_atk.get(k, 0)) > 1e-9}
    delta_def = {k: round(cur_def[k] - base_def.get(k, 0), 2) for k in cur_def
                 if abs(cur_def[k] - base_def.get(k, 0)) > 1e-9}
    return {'base_atk': base_atk, 'base_def': base_def,
            'cur_atk': cur_atk, 'cur_def': cur_def,
            'delta_atk': delta_atk, 'delta_def': delta_def}


def list_modified():
    """列出副本中相对底层有面板修改的角色名。"""
    out = []
    for name in get_ships():
        dp = diff_panels(name)
        if dp['delta_atk'] or dp['delta_def']:
            out.append(name)
    return out


def remove_ship(name) -> bool:
    """从副本移除该角色（计算回退底层默认面板）。"""
    data = load_user_data()
    ships = data.setdefault('ships', {})
    if name not in ships:
        return True
    ships.pop(name, None)
    return save_user_data(data)
