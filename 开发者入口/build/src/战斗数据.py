# -*- coding: utf-8 -*-
"""
CombatData 数据模型 - 定义战斗中使用的所有属性结构

数据管理工具专用版本，用于解析原始数据并生成标准化 data.json。
"""


class CombatData:
    def __init__(self):
        self.name = ""
        self.damage_type = ""
        self.ammo_type = ""
        self.target_count = 0
        self.sou_value = 0
        self.weapon_type = ""

        # 元数据（从 JSON 加载）
        self.ship_type = ""
        self.nation = ""
        self.rarity = ""
        self.armor_type = ""

        # 普攻列表（每个元素包含 weapon_type, ammo_type, hit, multiplier, cd, preset_damage_type）
        self.normal_attacks = []

        # 技能（每段独立攻击：{cd, parts: [{hit, multiplier, damage_type}, ...]}）
        self.skill_attack = {"cd": 0.0, "parts": []}
        self.burst_attack = {"cd": 0.0, "parts": []}
        self.support_attack = {"cd": 0.0, "parts": []}
        # 旗舰技能（结构同通常技，作为面板信息展示）
        self.flagship_attack = {"cd": 0.0, "parts": []}

        # 护盾
        self.shield = {
            "炮击次数": 0, "雷击次数": 0, "航空次数": 0,
            "轰炸机次数": 0, "鱼雷次数": 0, "全属性次数": 0,
            "炮击自身血量": 0, "雷击自身血量": 0, "航空自身血量": 0,
            "轰炸机自身血量": 0, "鱼雷自身血量": 0, "全属性自身血量": 0,
            "炮击固定值": 0, "雷击固定值": 0, "航空固定值": 0,
            "轰炸机固定值": 0, "鱼雷固定值": 0, "全属性固定值": 0,
        }

        # 面板
        self.attack_panel = {
            "炮击": 0, "雷击": 0, "航空": 0,
            "轰炸": 0, "鱼雷": 0,
            "索敌": 0,
            "命中": 0, "幸运": 0, "装填": 0
        }
        self.defense_panel = {
            "装甲": 0, "对空": 0, "机动": 0,
            "血量": 0, "回避率": 0, "防暴击率": 0
        }

        self.multiplier = {"type": "", "cd": 0.0, "extra_params": {}}
        self.buffs = {
            "攻击力增加": 0,
            "炮击值固定增加": 0, "炮击值百分比增加": 0,
            "雷击值固定增加": 0, "雷击值百分比增加": 0,
            "航空值固定增加": 0, "航空值百分比增加": 0,
            "轰炸值固定增加": 0, "轰炸值百分比增加": 0,
            "鱼雷值固定增加": 0, "鱼雷值百分比增加": 0,
            "装甲值固定增加": 0, "装甲值百分比增加": 0,
            "对空值固定增加": 0, "对空值百分比增加": 0,
            "机动值固定增加": 0, "机动值百分比增加": 0,
            "命中值固定增加": 0, "命中值百分比增加": 0,
            "幸运值固定增加": 0, "幸运值百分比增加": 0,
            "装填值固定增加": 0, "装填值百分比增加": 0,
            "命中值增加": 0, "命中率增加": 0,
            "回避率增加": 0, "防暴击率增加": 0,
            "暴击率增加": 0, "暴击伤害增加": 0,
            "全伤害增加": 0.2,   # 默认 20%
            "炮击伤害增加": 0, "雷击伤害增加": 0,
            "航空伤害增加": 0, "轰炸伤害增加": 0,
            "鱼雷伤害增加": 0,
            "近战伤害增加": 0,
            "小型攻击伤害加成": 0, "中型攻击伤害加成": 0, "大型攻击伤害加成": 0,
            "全伤害减免": 0.2,   # 默认 20%
            "炮击伤害减免": 0, "雷击伤害减免": 0,
            "航空伤害减免": 0, "轰炸伤害减免": 0,
            "鱼雷伤害减免": 0,
            "近战伤害减免": 0,
            "小型攻击伤害减免": 0, "中型攻击伤害减免": 0, "大型攻击伤害减免": 0,
            "炮击值增加": 0, "雷击值增加": 0, "航空值增加": 0,
            "轰炸值增加": 0, "鱼雷值增加": 0,
            "装甲值增加": 0, "对空值增加": 0, "机动值增加": 0,
            "血量值增加": 0,
            # 航速 buff（旗舰技「【航速+5节】」等），仅用于航速门槛判定，不进伤害公式
            "航速值固定增加": 0,
        }
        self.damage_extra = {}
        # 技能释放后触发的持续 buff 列表（战斗时间轴用）
        # 结构：[{trigger: 'skill|burst|support|flagship', target: 'self|enemy', duration: 秒, buffs: {键名: 数值}}]
        self.skill_buffs = []
        # 旗舰光环（LeaderSkill 提取，仅该角色被选为旗舰时对全队生效）
        # 结构化光环列表（extract_flagship_auras 产出），统一为 list
        self.flagship_buffs = []

        # 递增光环（「每X秒+Y%，最多+Z%」，遭遇战模拟器按在场秒数递增）
        # 结构：[{attr, step, interval, cap, night_disabled}]
        # 由 build/角色数据构建.py _extract_ramp_buffs 按规则提取；必须进 to_dict，
        # 否则 DataStore.save 全库序列化时会把 patch 写入的该字段抹掉
        self.ramp_buffs = []

        # 拉古兹阵营增伤（仅遭遇战模块读取，叠加到「全伤害增加」；
        # 由 skill_buff_extractor.extract_raguz_bonus 按规则提取）
        self.raguz_bonus = 0.0

        # 技能 CD 效果（重置/固定减秒，仅遭遇战时间轴读取）
        # 结构：{slot: [{"action": "reset"|"reduce", "target": "skill"|"burst",
        #        "scope": {...}, 可选 "seconds": N}]}，slot 为释放源技能 skill/burst/support
        # 由 skill_buff_extractor.extract_cd_effects 按规则提取
        self.cd_effects = {}

        # 技能文本指纹（用于增量解析检测描述变化）
        self.skill_text_hash = ""

        # 面板信息指纹（用于表格导入/手动更新后检测面板变化，触发 data.json 同步）
        self.panel_hash = ""

        # 航速（节）：角色底层面板通用字段，等级无关常量。
        # 读取自 舰灵数值（整理版）.xlsx 的「航速」列，用于旗舰技的航速门槛判定
        # （如「所属舰队中航速≤25的舰灵」）；不参与攻防面板与伤害公式。
        self.speed = 0.0

    def to_dict(self):
        return {
            "name": self.name,
            "speed": self.speed,
            "damage_type": self.damage_type,
            "ammo_type": self.ammo_type,
            "target_count": self.target_count,
            "sou_value": self.sou_value,
            "weapon_type": self.weapon_type,
            "ship_type": self.ship_type,
            "nation": self.nation,
            "rarity": self.rarity,
            "armor_type": self.armor_type,
            "normal_attacks": self.normal_attacks,
            "skill_attack": self.skill_attack,
            "burst_attack": self.burst_attack,
            "support_attack": self.support_attack,
            "flagship_attack": self.flagship_attack,
            "shield": self.shield,
            "attack_panel": self.attack_panel,
            "defense_panel": self.defense_panel,
            "multiplier": self.multiplier,
            "buffs": self.buffs,
            "skill_buffs": self.skill_buffs,
            "flagship_buffs": self.flagship_buffs,
            "ramp_buffs": self.ramp_buffs,
            "raguz_bonus": self.raguz_bonus,
            "cd_effects": self.cd_effects,
            "damage_extra": self.damage_extra,
            "skill_text_hash": self.skill_text_hash,
            "panel_hash": self.panel_hash,
        }

    @staticmethod
    def from_dict(data):
        obj = CombatData()
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        for panel in [obj.attack_panel, obj.defense_panel, obj.buffs]:
            for key in panel:
                try:
                    panel[key] = float(panel[key])
                except (ValueError, TypeError):
                    panel[key] = 0.0
        return obj


"""
数据存取模块

精简版数据存取，替代 App.load_data/save_data。
只做读/写 data.json，不引入 ShipLoader/enemy/查询逻辑。
"""
import json
import os

from .名称归一化 import match_key


class DataStore:
    """数据存取：读 data.json -> storage / storage -> 写 data.json"""

    def __init__(self, output_path: str):
        self.output_path = output_path
        self.storage: dict = {}
        self._key_index: dict = {}
        self.load()

    def _rebuild_index(self):
        """重建 match_key 索引（load/upsert 后调用）"""
        self._key_index = {match_key(n): n for n in self.storage if n}

    def load(self):
        """读 data.json -> storage；文件不存在则空字典（首次写入场景）"""
        if not os.path.exists(self.output_path):
            self.storage = {}
            self._rebuild_index()
            return
        try:
            with open(self.output_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                for name, item in raw.items():
                    self.storage[name] = CombatData.from_dict(item)
        except Exception:
            self.storage = {}
        self._rebuild_index()

    def save(self):
        """storage -> 写 data.json"""
        raw = {name: obj.to_dict() for name, obj in self.storage.items()}
        parent = os.path.dirname(self.output_path)
        if parent and not os.path.exists(parent):
            raise RuntimeError(f"输出路径父目录不存在: {parent}，请检查 .env 的 DATA_OUTPUT_PATH")
        with open(self.output_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, name: str):
        """按正规名精确获取，不存在返回 None"""
        return self.storage.get(name)

    def has(self, name: str) -> bool:
        """按 match_key 判断是否已存在（增量 LLM 跳过判断用）"""
        return match_key(name) in self._key_index

    def find_by_match_key(self, name: str):
        """按 match_key 模糊定位正规名，未命中返回 None"""
        return self._key_index.get(match_key(name))

    def upsert(self, name: str, obj: CombatData):
        """新增或更新角色（同步刷新索引）"""
        self.storage[name] = obj
        self._key_index[match_key(name)] = name

    def prune(self, keep_keys: set) -> int:
        """删除 match_key 不在 keep_keys 里的角色，返回删除数。"""
        names_to_remove = [n for n in self.storage if match_key(n) not in keep_keys]
        for n in names_to_remove:
            del self.storage[n]
        self._rebuild_index()
        return len(names_to_remove)


# LLM 应输出的 JSON 结构描述
COMBAT_DATA_JSON_SCHEMA_DESC = """输出一个 JSON 对象，结构如下：
{
  "name": "舰名（取表格 Name 列的值）",
  "ship_type": "舰种，如 驱逐/轻巡/重巡/战列/航母/轻母/航战 等",
  "nation": "所属国家/阵营",
  "rarity": "稀有度，如 白/蓝/紫/金/彩",
  "armor_type": "自身装甲类型：轻甲/中甲/重甲（L→轻甲、N→中甲、H→重甲）",
  "ammo_type": "默认弹种：通常/穿甲/高爆，无则空字符串",
  "skill_attack": {"cd": 0.0, "parts": [攻击段, ...]},
  "burst_attack": {"cd": 0.0, "parts": [攻击段, ...]},
  "support_attack": {"cd": 0.0, "parts": [攻击段, ...]},
  "flagship_attack": {"parts": [攻击段, ...]},
  "buffs": {"Buff字段名": 数值, ...},
  "skill_buffs": [{"trigger": "skill|burst|support", "target": "self|enemy", "duration": 秒, "buffs": {"Buff字段名": 数值}}, ...]
}

攻击段结构（parts 数组的元素）：
{"hit": 1, "multiplier": "单段倍率表达式字符串", "damage_type": "炮击/雷击/航空/轰炸机/鱼雷/近战/点燃"}

点燃段（技能描述含点燃效果）：
该段 damage_type 填 "点燃"，并附 ignite 配置：
{"hit": 1, "damage_type": "点燃", "ignite": {"count": 1, "prob": 1.0, "dps": 125, "duration": 10, "cd": 0}}

skill_buffs 说明：
- trigger：触发技能（skill/burst/support）
- target：self（自身）或 enemy（敌方）
- duration：持续时间（秒）
- buffs：与 buffs 字段相同的标准键名，数值百分比类用小数

字段类型说明：
- cd：float，技能冷却时间（秒）
- parts：数组，每一段独立攻击
- hit：int，该段命中数
- multiplier：string，该段单发倍率表达式
- damage_type：string，该段伤害类型
- buffs 中的数值：百分比类用小数（如 0.25 表示 25%）
"""

# Buff 标准键名模板（供 LLM 参考）
BUFF_KEY_TEMPLATE = """攻击力增加、炮击值固定增加、炮击值百分比增加、雷击值固定增加、雷击值百分比增加、
航空值固定增加、航空值百分比增加、轰炸值固定增加、轰炸值百分比增加、
鱼雷值固定增加、鱼雷值百分比增加、装甲值固定增加、装甲值百分比增加、
对空值固定增加、对空值百分比增加、机动值固定增加、机动值百分比增加、
命中值固定增加、命中值百分比增加、幸运值固定增加、幸运值百分比增加、
装填值固定增加、装填值百分比增加、命中率增加、回避率增加、防暴击率增加、
暴击率增加、暴击伤害增加、全伤害增加、炮击伤害增加、雷击伤害增加、航空伤害增加、
轰炸伤害增加、鱼雷伤害增加、近战伤害增加、对空伤害增加、高平炮伤害增加、
小型攻击伤害加成、中型攻击伤害加成、大型攻击伤害加成、全伤害减免、炮击伤害减免、
雷击伤害减免、航空伤害减免、轰炸伤害减免、鱼雷伤害减免、近战伤害减免、对空伤害减免、
小型攻击伤害减免、中型攻击伤害减免、大型攻击伤害减免、点燃抵抗

舰种专属增伤（攻击方）：
对驱逐伤害增加、对巡洋伤害增加、对战列伤害增加、对航母伤害增加、对轻母伤害增加、
对雷巡伤害增加、对战巡伤害增加、对装母伤害增加、对航战伤害增加、对重炮伤害增加

舰种专属减伤（防守方）：
对驱逐伤害减免、对巡洋伤害减免、对战列伤害减免、对航母伤害减免、对轻母伤害减免、
对雷巡伤害减免、对战巡伤害减免、对装母伤害减免

拉古兹增伤：raguz_bonus 字段，填入数值如 0.3 表示 30%
百分比类用小数（如 20% → 0.2）。"""
