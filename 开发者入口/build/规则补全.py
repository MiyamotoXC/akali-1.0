# -*- coding: utf-8 -*-
"""构建流程 · 规则补全阶段（stage 3）。

定位
----
早期这些逻辑散在 `build/patch.py` 的 11 个一次性子命令里（点燃 / skill_buffs / 航速 /
CD / 递增光环 / 必定暴击 / 条件增伤 / buffs 清洗 / 换行修正 / 普攻基准…），每次重建完
都要**手动**挑着跑一遍，漏跑就出现「agent 算出的字段缺失」。

本模块把它们收进**完整流程**：`角色数据构建.py --stage all`（= 构建入口.py 第 1 步）在
「合表(stage 1) + 增量 LLM(stage 2)」之后自动执行本阶段，规则与原 patch 完全一致。

原则
----
- **全部幂等**：重复执行结果不变（已有的不覆盖、缺失的才补、脏的归一）。
- **只做代码规则提取，不调 LLM**：凡描述文本里能正则确定的，都不依赖 LLM 输出。
- **一次读写**：raw_data.json / data.json / custom_ship_config.json 各只读写一次，
  开头统一备份（`.bak-post-时间戳`）。
- **不碰数值**：普攻段只补 `slot` / `weapon_code` 元数据，**不改倍率/hit/CD**
  （旧 `patch.py normal --write fix` 的全量同步模式已废弃——stage 1 的普攻本来就
  取自源表，两表口径差异只出报告供人工核对）。

覆盖的字段（对应原 patch 子命令）
--------------------------------
| 本模块 | 原 patch 子命令 | 说明 |
|---|---|---|
| 换行规范化 | newline_fix | raw_data.json + custom_ship_config.json 的字面 `\n` |
| 航速 | speed | raw.panel.面板.航速 → data.speed（panel 由 stage 1 从源表带出）|
| buffs 归一 | buffs_reset | 常驻 buffs 恢复引擎默认（全伤害增减 0.2），清 LLM 误归项 |
| skill_buffs / flagship_buffs / raguz_bonus / cd_effects | skill_buffs | 代码提取，非空覆盖 |
| ramp_buffs | ramp | 递增光环 + 对应常驻键清零 |
| 技能循环 CD | skill_cd | 仅 cd==0 的技能回填 |
| always_crit | alwayscrit | 置位/清残留 |
| static_buffs | condextra | 置位/清残留 |
| 点燃 part | ignite | 仅当该技能桶还没有点燃段 |

天赋（talents / talent_map.json / ·改 复制）由 **stage 1** 的 `_merge_to_raw` +
`_fill_kai_talents` 负责，不在本阶段重复（原 `patch.py talents` 是修复历史丢失用）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
RAW_PATH = os.path.join(KB_DIR, "底层数据", "产物", "raw_data.json")
DATA_PATH = os.path.join(KB_DIR, "底层数据", "产物", "data.json")
CUSTOM_PATH = os.path.join(KB_DIR, "底层数据", "产物", "custom_ship_config.json")
TALENT_FILE = os.path.join(KB_DIR, "底层数据", "源文件", "数值自动化表.xlsx")
MAPPINGS_PATH = os.path.join(KB_DIR, "底层数据", "源文件", "mappings.json")
PANEL_FILE = os.path.join(KB_DIR, "底层数据", "源文件", "舰灵数值（整理版）.xlsx")
NORMAL_REPORT = os.path.join(_PROJECT_ROOT, "调试", "普攻基准识别报告.txt")

# 技能字段 → data.json 技能桶 的固定映射（与 main.build_combatdata / parser 一致）
SKILL_MAP = [
    ("LeaderSkill", "flagship_attack", False),
    ("SupportSkill", "support_attack", True),
    ("BattleSkill1", "skill_attack", True),
    ("BattleSkill2", "burst_attack", True),
]
SKILL_BUCKETS = ("flagship_attack", "support_attack", "skill_attack", "burst_attack")

# 常驻 buffs 引擎默认（持续 buff / 旗舰光环由 skill_buffs / flagship_buffs 代码提取承担）
BUFFS_DEFAULT = {"全伤害增加": 0.2, "全伤害减免": 0.2}

_IGNITE_MAIN = re.compile(r'(?:每秒)?燃烧\s*(\d+(?:\.\d+)?)\s*点伤害\s*[，,，\s]*持续\s*(\d+(?:\.\d+)?)\s*秒')
_PROB = re.compile(r'(\d+(?:\.\d+)?)\s*%\s*(?:几率|概率)')
_CNT = re.compile(r'触发\s*(\d+)\s*次点燃|(\d+)\s*次点燃')
_CD = re.compile(r'冷却时间\s*(\d+(?:\.\d+)?)\s*秒|每\s*(\d+(?:\.\d+)?)\s*秒(?:[^\n]{0,8}?(?:释放|发射|施放))?')
_ALWAYS_CRIT = re.compile(r'必定暴击|必然暴击|必定会暴击')
_LIT_NEWLINE = re.compile(r'\\n')


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, obj, backup=True):
    if backup:
        shutil.copy2(path, path + ".bak-post-" + time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _extract_ignite(text):
    """从技能描述提取点燃配置（每跳伤害/持续/概率/次数/CD）；无点燃返回 None。"""
    if not isinstance(text, str) or ("点燃" not in text and "燃烧" not in text):
        return None
    m = _IGNITE_MAIN.search(text)
    if not m:
        return None
    m_cnt, m_prob, m_cd = _CNT.search(text), _PROB.search(text), _CD.search(text)
    cnt = int(m_cnt.group(1) or m_cnt.group(2) or 1) if m_cnt else 1
    cd = float(m_cd.group(1) or m_cd.group(2) or 0) if m_cd else 0.0
    return {"count": cnt,
            "prob": float(m_prob.group(1)) / 100.0 if m_prob else 1.0,
            "dps": float(m.group(1)),
            "duration": float(m.group(2)),
            "cd": cd}


def _baseline_for(baseline: dict, name: str):
    """取普攻基准段：先按原名，·改 后缀回退原型舰（改造前后普攻同源）。"""
    if name in baseline:
        return baseline[name]
    if "·改" in name and name.replace("·改", "") in baseline:
        return baseline[name.replace("·改", "")]
    return None


def _preset_of(weapon_type: str) -> str:
    """weapon_type → preset_damage_type（补建缺失普攻段用）。"""
    if weapon_type in ("驱逐炮", "轻巡炮", "重巡炮", "战列炮", "高平炮", "三式弹"):
        return "炮击"
    if weapon_type == "轰炸机":
        return "轰炸机"
    if weapon_type == "鱼雷":
        return "鱼雷"
    if weapon_type == "雷击":
        return "雷击"
    if weapon_type == "近战":
        return "近战"
    return "炮击"


def _label_to_weapon_ammo(label: str):
    """克制表弹种标签 → (weapon_type, ammo_type)。'驱逐通常'→('驱逐炮','通常')。"""
    from src.文件提取 import _parse_dtype_to_weapon_ammo
    special = {"高平炮": "高平炮", "三式弹": "三式弹", "近战": "近战"}
    if label in special:
        return special[label], ""
    return _parse_dtype_to_weapon_ammo(label)


def _fix_literal_newlines(obj) -> int:
    """就地把容器内字符串的字面 `\n`（反斜杠+n 两字符）转成真实换行，返回处理个数。"""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and _LIT_NEWLINE.search(v):
                obj[k] = _LIT_NEWLINE.sub("\n", v)
                n += 1
            else:
                n += _fix_literal_newlines(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str) and _LIT_NEWLINE.search(v):
                obj[i] = _LIT_NEWLINE.sub("\n", v)
                n += 1
            else:
                n += _fix_literal_newlines(v)
    return n


# ----------------------------------------------------------------------
# 各规则步骤（全部只改内存 dict，落盘统一在 run() 里做）
# ----------------------------------------------------------------------
def step_newlines(raw, log) -> int:
    """字面 `\n` → 真实换行（raw_data.json 内存对象；custom_ship_config.json 单独落盘）。"""
    n = _fix_literal_newlines(raw)
    if os.path.exists(CUSTOM_PATH):
        cust = _load(CUSTOM_PATH)
        m = _fix_literal_newlines(cust)
        if m:
            _save(CUSTOM_PATH, cust)
        n += m
        log.append(f"换行规范化: raw {n - m} 处 + custom {m} 处")
    else:
        log.append(f"换行规范化: raw {n} 处")
    return n


def step_speed(raw, data, log) -> int:
    """航速：raw.panel.面板.航速（stage 1 从源表带出）→ data.speed。"""
    n = 0
    for name, rec in raw.items():
        sp = ((rec.get("panel") or {}).get("面板") or {}).get("航速")
        try:
            sp = float(sp)
        except (TypeError, ValueError):
            continue
        dship = data.get(name)
        if dship is not None and dship.get("speed") != sp:
            dship["speed"] = sp
            n += 1
    log.append(f"航速同步: {n} 个角色的 data.speed 与 raw 不一致，已对齐")
    return n


def step_buffs_normalize(data, log) -> int:
    """常驻 buffs 恢复引擎默认（清 LLM 误归进常驻的持续 buff / 旗舰光环 / 幻觉值）。"""
    n = 0
    for name, obj in data.items():
        buffs = obj.get("buffs") or {}
        dirty = {k: v for k, v in buffs.items()
                 if v and (k not in BUFFS_DEFAULT or v != BUFFS_DEFAULT[k])}
        if dirty:
            n += 1
            if n <= 10:
                log.append(f"  buffs 清洗 {name}: {json.dumps(dirty, ensure_ascii=False)}")
        obj["buffs"] = dict(BUFFS_DEFAULT)
    log.append(f"buffs 归一: {n}/{len(data)} 个角色存在非默认常驻项，已恢复引擎默认")
    return n


def step_skill_buffs(raw, data, log) -> int:
    """skill_buffs / flagship_buffs / raguz_bonus / cd_effects：代码规则提取，非空覆盖。"""
    from src.技能增益提取 import (
        extract_skill_buffs, extract_flagship_auras, extract_raguz_bonus, extract_cd_effects)
    n_sb = n_fa = n_rb = n_ce = 0
    for name, ship in raw.items():
        dship = data.get(name)
        if not dship:
            continue
        skills = ship.get("skills") or {}
        sb = extract_skill_buffs(skills)
        fa = extract_flagship_auras(skills)
        rb = extract_raguz_bonus(skills)
        ce = extract_cd_effects(skills)
        if dship.get("skill_buffs") != sb:
            dship["skill_buffs"] = sb
            n_sb += 1
        if dship.get("flagship_buffs") != fa:
            dship["flagship_buffs"] = fa
            n_fa += 1
        if dship.get("raguz_bonus") != rb:
            dship["raguz_bonus"] = rb
            n_rb += 1
        if dship.get("cd_effects") != ce:
            dship["cd_effects"] = ce
            n_ce += 1
    log.append(f"持续 buff/光环: skill_buffs 更新 {n_sb}、flagship_buffs {n_fa}、"
               f"raguz_bonus {n_rb}、cd_effects {n_ce}")
    return n_sb + n_fa + n_rb + n_ce


def step_ramp(raw, data, log) -> int:
    """递增光环 ramp_buffs（「每X秒+Y%，最多+Z%」）+ 对应常驻键清零。"""
    from 角色数据构建 import _extract_ramp_buffs  # 与 stage 1 / 重建管线同一份实现，防正则漂移
    n = 0
    for name, ship in raw.items():
        dship = data.get(name)
        if not dship:
            continue
        ramp = _extract_ramp_buffs(ship.get("skills") or {})
        if not ramp:
            continue
        if dship.get("ramp_buffs") != ramp:
            dship["ramp_buffs"] = ramp
            n += 1
        for rb in ramp:
            key = rb.get("attr", "")
            if key in (dship.get("buffs") or {}):
                dship["buffs"][key] = 0.0
    log.append(f"递增光环: {n} 个角色 ramp_buffs 有变化")
    return n


def step_skill_cd(raw, data, log) -> int:
    """技能循环 CD 回填：仅 cd==0 的技能桶（已有 CD 不覆盖）。"""
    from src.技能增益提取 import extract_loop_cd
    n = 0
    for name, ship in raw.items():
        dship = data.get(name)
        if not dship:
            continue
        skills = ship.get("skills") or {}
        for field, key, _wc in SKILL_MAP:
            text = skills.get(field)
            if not isinstance(text, str) or not text:
                continue
            skill = dship.get(key)
            if not isinstance(skill, dict) or float(skill.get("cd", 0) or 0) > 0:
                continue
            cd_val = extract_loop_cd(text)
            if cd_val:
                skill["cd"] = cd_val
                n += 1
    log.append(f"技能循环 CD 回填: {n} 个技能（仅 cd==0 者）")
    return n


def step_alwayscrit(raw, data, log) -> int:
    """必定暴击标记：描述含「必定/必然暴击」置位，未命中清残留（幂等）。"""
    n = 0
    for name, ship in raw.items():
        dship = data.get(name)
        if not dship:
            continue
        skills = ship.get("skills") or {}
        for field, key, _wc in SKILL_MAP:
            skill = dship.get(key)
            if not isinstance(skill, dict):
                continue
            text = skills.get(field)
            if isinstance(text, str) and _ALWAYS_CRIT.search(text):
                if not skill.get("always_crit"):
                    skill["always_crit"] = True
                    n += 1
            elif skill.get("always_crit"):
                skill.pop("always_crit")
                n += 1
    log.append(f"必定暴击: {n} 处置位/清残留")
    return n


def step_condextra(raw, data, log) -> int:
    """条件增伤 static_buffs：描述含「XX条件下造成/增加X%额外伤害」置位，未命中清残留。"""
    from src.技能增益提取 import extract_static_buffs_from_text
    n = 0
    for name, ship in raw.items():
        dship = data.get(name)
        if not dship:
            continue
        skills = ship.get("skills") or {}
        for field, key, _wc in SKILL_MAP:
            skill = dship.get(key)
            if not isinstance(skill, dict):
                continue
            text = skills.get(field)
            sb = extract_static_buffs_from_text(text) if isinstance(text, str) and text else {}
            if sb:
                if skill.get("static_buffs") != sb:
                    skill["static_buffs"] = sb
                    n += 1
            elif skill.get("static_buffs"):
                skill.pop("static_buffs")
                n += 1
    log.append(f"条件增伤: {n} 处置位/清残留")
    return n


def step_ignite(raw, data, log) -> int:
    """点燃 part 补全：仅当该技能桶还没有点燃段（LLM 兜底，规则提取）。"""
    n = 0
    for name, ship in raw.items():
        dship = data.get(name)
        if not dship:
            continue
        skills = ship.get("skills") or {}
        for raw_key, data_key, _wc in SKILL_MAP:
            ig = _extract_ignite(skills.get(raw_key, ""))
            if not ig:
                continue
            skill = dship.get(data_key)
            if not isinstance(skill, dict):
                skill = {"cd": 0.0, "parts": []}
                dship[data_key] = skill
            parts = skill.setdefault("parts", [])
            if any(isinstance(p, dict) and p.get("damage_type") == "点燃" for p in parts):
                continue
            parts.append({"hit": 1, "multiplier": "", "damage_type": "点燃", "ignite": ig})
            n += 1
    log.append(f"点燃 part 补全: {n} 处（仅缺失者）")
    return n


def step_normal_baseline(data, write_slot=True, log=None) -> int:
    """普攻基准比对（基准 = 数值自动化表.xlsx「普攻列表」）。

    - 自动补齐 `slot` / `weapon_code` 元数据（**不碰倍率/hit/CD 等数值**）；
    - 全量比对报告写 `调试/普攻基准识别报告.txt`（两表口径差异供人工核对）。
    """
    from src.文件提取 import extract_normal_list, extract_counter_table
    if not os.path.exists(TALENT_FILE):
        if log is not None:
            log.append("普攻基准: 未找到数值自动化表.xlsx，跳过")
        return 0
    baseline = extract_normal_list(TALENT_FILE)
    if not baseline:
        if log is not None:
            log.append("普攻基准: 数值自动化表.xlsx 无「普攻列表」sheet，跳过")
        return 0
    counter = extract_counter_table(TALENT_FILE)
    mp = _load(MAPPINGS_PATH)
    damage_modifier = {tuple(k.split("|")): v for k, v in (mp.get("damage_modifier") or {}).items()}

    lines = ["=" * 78,
             "普攻基准识别报告（基准 = 数值自动化表.xlsx「普攻列表」，%d 舰）" % len(baseline),
             "=" * 78]
    mism, matched, missing = [], 0, []
    for label, armors in counter.items():
        wt, ammo = _label_to_weapon_ammo(label)
        if not wt:
            missing.append(label)
            continue
        for armor, val in armors.items():
            cur = damage_modifier.get((wt, ammo, armor))
            if cur is None:
                missing.append(f"{label}/{armor}")
            elif abs(float(cur) - float(val)) > 1e-9:
                mism.append(f"{(wt, ammo, armor)}: mappings={cur} vs 基准={val}")
            else:
                matched += 1
    lines += ["", "【1】装甲克制修正表一致性（普攻列表 克制修正 vs mappings.json damage_modifier）",
              f"  一致 {matched} 项；不一致 {len(mism)} 项；基准有而 mappings 无 {len(missing)} 项"]
    lines += [f"    [差异] {m}" for m in mism[:20]] + [f"    [缺失] {m}" for m in missing[:20]]
    if not mism and not missing:
        lines.append("  → 克制表与基准完全一致，装甲克制/点燃查表无需改动")

    seg_fixed = 0
    no_base, seg_cnt, weapon, cd, mult_other, hit_diff = [], [], [], [], [], []
    checked = 0
    for name, dship in sorted(data.items()):
        na = dship.get("normal_attacks") or []
        base = _baseline_for(baseline, name)
        if not base:
            if na:
                no_base.append(name)
            continue
        checked += 1
        if len(na) != len(base):
            seg_cnt.append(f"{name}(数据{len(na)}段/基准{len(base)}段)")
            continue
        for i, (seg, bseg) in enumerate(zip(na, base)):
            if write_slot:
                slot = bseg["slot"]
                if seg.get("slot") != slot:
                    seg["slot"] = slot
                    seg_fixed += 1
                if seg.get("weapon_code") != bseg["weapon_code"]:
                    seg["weapon_code"] = bseg["weapon_code"]
            if (seg.get("weapon_type") or "", seg.get("ammo_type") or "") != (bseg["weapon_type"], bseg["ammo_type"]):
                weapon.append(f"{name}第{i+1}段: 数据{(seg.get('weapon_type') or '', seg.get('ammo_type') or '')} vs 基准{(bseg['weapon_type'], bseg['ammo_type'])}")
            try:
                if abs(float(seg.get("cd", 0)) - float(bseg["cd"])) > 1e-6:
                    cd.append(f"{name}第{i+1}段: 数据{seg.get('cd')} vs 基准{bseg['cd']}")
            except (ValueError, TypeError):
                pass
            try:
                mult = float(seg.get("multiplier"))
            except (ValueError, TypeError):
                continue
            if abs(mult - float(bseg["multiplier"])) > 1e-6 and abs(mult - float(bseg["volley_multiplier"])) > 1e-6:
                mult_other.append(f"{name}第{i+1}段: 数据{mult} vs 基准 单发{bseg['multiplier']}/总倍率{bseg['volley_multiplier']}")
            elif abs(mult - float(bseg["volley_multiplier"])) <= 1e-6 and int(seg.get("hit", 0) or 0) != int(bseg["hit"]):
                hit_diff.append(f"{name}第{i+1}段({bseg['slot']}): hit 数据{int(seg.get('hit', 0) or 0)} vs 基准{int(bseg['hit'])}")

    lines += ["", "【2】逐船普攻段识别（data.json vs 基准）",
              f"  已比对 {checked} 舰；基准无此舰（有普攻数据）: {len(no_base)} 舰"
              + (f"：{'、'.join(no_base[:10])}" if no_base else ""),
              f"  段数不一致: {len(seg_cnt)} 舰" + (f"：{'；'.join(seg_cnt[:10])}" if seg_cnt else ""),
              f"  slot/weapon_code 已自动补齐: {seg_fixed} 段",
              f"  武器/弹种识别差异: {len(weapon)} 段（前 15）"] + [f"    {s}" for s in weapon[:15]]
    lines += [f"  CD 差异: {len(cd)} 段（前 15）"] + [f"    {s}" for s in cd[:15]]
    lines += [f"  倍率口径外: {len(mult_other)} 段（前 15）"] + [f"    {s}" for s in mult_other[:15]]
    lines += [f"  hit 数差异: {len(hit_diff)} 段（前 15）"] + [f"    {s}" for s in hit_diff[:15]]
    lines += ["", "说明: 普攻数值以 stage 1 的「拂晓舰灵普攻倍率cd1.1.xlsx」为准；"
              "本报告仅列出与「数值自动化表/普攻列表」的口径差异，供人工核对，不自动改数值。"]
    os.makedirs(os.path.dirname(NORMAL_REPORT), exist_ok=True)
    with open(NORMAL_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    if log is not None:
        log.append(f"普攻基准: 比对 {checked} 舰，slot/weapon_code 补齐 {seg_fixed} 段，"
                   f"报告 → {os.path.relpath(NORMAL_REPORT, _PROJECT_ROOT)}")
    return seg_fixed


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def run(verbose: bool = False) -> int:
    """执行规则补全阶段。返回 0 成功 / 1 失败（角色数据构建.py --stage 3 与 构建入口.py 调用）。"""
    if not os.path.exists(RAW_PATH) or not os.path.exists(DATA_PATH):
        print("[规则补全] 缺少 raw_data.json / data.json，请先跑 stage 1/2")
        return 1
    print("=" * 60)
    print("stage 3 · 规则补全（代码规则提取，不调 LLM，全部幂等）")
    print("=" * 60)
    raw = _load(RAW_PATH)
    data = _load(DATA_PATH)
    log: List[str] = []

    try:
        step_newlines(raw, log)
        step_speed(raw, data, log)
        step_buffs_normalize(data, log)
        step_skill_buffs(raw, data, log)
        step_ramp(raw, data, log)
        step_skill_cd(raw, data, log)
        step_alwayscrit(raw, data, log)
        step_condextra(raw, data, log)
        step_ignite(raw, data, log)
        step_normal_baseline(data, write_slot=True, log=log)
    except Exception as e:  # 任何一步失败都不落盘，保持原产物可用
        print(f"[规则补全] 失败，未写盘: {e}")
        raise

    _save(RAW_PATH, raw)
    _save(DATA_PATH, data)
    print("\n".join("  " + x for x in log))
    print(f"\n规则补全完成 ✓（已备份 → *.bak-post-*）| 角色 {len(data)} 个")
    if verbose:
        print("普攻基准报告: " + NORMAL_REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(verbose=True))
