# -*- coding: utf-8 -*-
"""
CombatData 数据模型 - 定义战斗中使用的所有属性结构
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

        # 普攻列表（每个元素包含 weapon_type, ammo_type, hit, multiplier, cd, preset_damage_type；
        # slot/weapon_code 由 patch.py normal 按 数值自动化表「普攻列表」基准写入：
        # slot=主武器/副武器，weapon_code=段代号（如 DD2/BB4/CV2））
        self.normal_attacks = []

        # 技能（每段独立攻击：{cd, parts: [{hit, multiplier, damage_type}, ...]}）
        self.skill_attack = {"cd": 0.0, "parts": []}
        self.burst_attack = {"cd": 0.0, "parts": []}
        self.support_attack = {"cd": 0.0, "parts": []}
        # 旗舰技能（无 cd，旗舰技能没有冷却概念，仅作为面板信息展示）
        self.flagship_attack = {"parts": []}

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
            "攻击力": 0,
            "炮击值固定": 0, "炮击值百分比": 0,
            "雷击值固定": 0, "雷击值百分比": 0,
            "航空值固定": 0, "航空值百分比": 0,
            "轰炸值固定": 0, "轰炸值百分比": 0,
            "鱼雷值固定": 0, "鱼雷值百分比": 0,
            "装甲值固定": 0, "装甲值百分比": 0,
            "对空值固定": 0, "对空值百分比": 0,
            "机动值固定": 0, "机动值百分比": 0,
            "命中值固定": 0, "命中值百分比": 0,
            "幸运值固定": 0, "幸运值百分比": 0,
            "装填值固定": 0, "装填值百分比": 0,
            "命中值": 0, "命中率": 0,
            "回避率": 0, "防暴击率": 0,
            "暴击率": 0, "暴击伤害": 0,
            "全伤害增加": 0.2,   # 默认 20%
            "炮击伤害": 0, "雷击伤害": 0,
            "航空伤害": 0, "轰炸伤害": 0,
            "鱼雷伤害": 0,
            "近战伤害": 0,
            "高平炮伤害": 0,
            "小型攻击伤害加成": 0, "中型攻击伤害加成": 0, "大型攻击伤害加成": 0,
            "全伤害减免": 0.2,   # 默认 20%
            "炮击伤害减免": 0, "雷击伤害减免": 0,
            "航空伤害减免": 0, "轰炸伤害减免": 0,
            "鱼雷伤害减免": 0,
            "近战伤害减免": 0,
            "小型攻击伤害减免": 0, "中型攻击伤害减免": 0, "大型攻击伤害减免": 0,
            "点燃抵抗": 0,
            "炮击值": 0, "雷击值": 0, "航空值": 0,
            "轰炸值": 0, "鱼雷值": 0,
            "装甲值": 0, "对空值": 0, "机动值": 0,
            "血量值": 0,
            # 航速 buff（旗舰技「【航速+5节】」等），仅用于航速门槛判定，不进伤害公式
            "航速值固定增加": 0,
        }
        # 舰种专属增伤（攻击方，键为防守方舰种名，值为增伤幅度）
        self.ship_type_damage_bonus = {}
        # 舰种专属减伤（防守方，键为攻击方舰种名，值为减伤幅度）
        self.ship_type_damage_reduction = {}
        # 拉古兹阵营增伤（仅遭遇战模块读取，叠加到 buffs['全伤害增加']）
        # 由数据管理工具解析技能描述中的"对拉古兹...伤害提高X%"写入
        self.raguz_bonus = 0.0
        # 技能 CD 效果（重置/固定减秒，仅遭遇战时间轴读取）
        # 结构：{slot: [{"action": "reset"|"reduce", "target": "skill"|"burst",
        #        "scope": {...}, 可选 "seconds": N}]}，slot 为释放源技能 skill/burst/support
        # 由 skill_buff_extractor.extract_cd_effects 按规则提取
        self.cd_effects = {}
        # 预留：技能释放后触发的持续 buff 列表（本次不实现解析）
        # 结构：[{trigger: 'burst', duration: 10, buffs: {'全伤害': 0.2}}]
        self.skill_buffs = []
        # 旗舰光环（LeaderSkill 提取，仅该角色被选为旗舰时对全队生效）
        self.flagship_buffs = {}
        # 递增光环（如支援技「航空值每5秒+5%，最多+80%」）：按时间线性递增、封顶后不变
        # 结构：[{attr: '航空值百分比增加', step: 0.05, interval: 5.0, cap: 0.8, night_disabled: true}]
        self.ramp_buffs = []
        self.damage_extra = {}
        # 航速（节）：角色底层面板通用字段，等级无关常量，来自 舰灵数值（整理版）.xlsx
        # 「航速」列。用于旗舰技的航速门槛判定（如「所属舰队中航速≤25的舰灵」），
        # 不参与攻防面板与伤害公式。
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
            "ship_type_damage_bonus": self.ship_type_damage_bonus,
            "ship_type_damage_reduction": self.ship_type_damage_reduction,
            "raguz_bonus": self.raguz_bonus,
            "cd_effects": self.cd_effects,
            "skill_buffs": self.skill_buffs,
            "flagship_buffs": self.flagship_buffs,
            "ramp_buffs": self.ramp_buffs,
            "damage_extra": self.damage_extra,
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

    def to_enemy_dict(self):
        """将 CombatData 转换为敌方伤害减免字典（供伤害计算使用）。"""
        return {
            "装甲": float(self.defense_panel.get("装甲", 0) or 0),
            "对空": float(self.defense_panel.get("对空", 0) or 0),
            "机动": float(self.defense_panel.get("机动", 0) or 0),
            "幸运": float(self.attack_panel.get("幸运", 0) or 0),
            "索敌": float(self.attack_panel.get("索敌", 0) or 0),
            # 回避率 / 防暴击率：面板值 + 该角色自身 buff（技能「回避率+X%」等）。
            # 二者都是「被攻击方」属性——该角色作为防守方时进 enemy 字典，供伤害公式读取
            # （用户口径 2026-09-14：回避率必须被正确识别，做防守方时参与计算）。
            "防暴击率": float(self.defense_panel.get("防暴击率", 0) or 0)
                        + float(self.buffs.get("防暴击率增加", 0) or 0),
            "回避率": float(self.defense_panel.get("回避率", 0) or 0)
                      + float(self.buffs.get("回避率增加", 0) or 0),
            "装甲类型": getattr(self, "armor_type", "轻甲") or "轻甲",
            "全伤害减免": float(self.buffs.get("全伤害减免", 0) or 0),
            "炮击伤害减免": float(self.buffs.get("炮击伤害减免", 0) or 0),
            "雷击伤害减免": float(self.buffs.get("雷击伤害减免", 0) or 0),
            "航空伤害减免": float(self.buffs.get("航空伤害减免", 0) or 0),
            "轰炸伤害减免": float(self.buffs.get("轰炸伤害减免", 0) or 0),
            "鱼雷伤害减免": float(self.buffs.get("鱼雷伤害减免", 0) or 0),
            "近战伤害减免": float(self.buffs.get("近战伤害减免", 0) or 0),
            "高平炮伤害减免": float(self.buffs.get("高平炮伤害减免", 0) or 0),
            "高爆弹伤害减免": float(self.buffs.get("高爆弹伤害减免", 0) or 0),
            "穿甲弹伤害减免": float(self.buffs.get("穿甲弹伤害减免", 0) or 0),
            "通常弹伤害减免": float(self.buffs.get("通常弹伤害减免", 0) or 0),
            "小型攻击伤害减免": float(self.buffs.get("小型攻击伤害减免", 0) or 0),
            "中型攻击伤害减免": float(self.buffs.get("中型攻击伤害减免", 0) or 0),
            "大型攻击伤害减免": float(self.buffs.get("大型攻击伤害减免", 0) or 0),
            "点燃抵抗": float(self.buffs.get("点燃抵抗", 0) or 0),
            "ship_type": getattr(self, "ship_type", "") or "",
            "name": getattr(self, "name", "") or "",
            "ship_type_damage_reduction": getattr(self, "ship_type_damage_reduction", {}) or {},
        }

    @staticmethod
    def create_empty_enemy(name="", ship_type=""):
        """构造无削减虚拟敌人（防守方未提供时使用）。"""
        return {
            "装甲": 0.0, "对空": 0.0, "机动": 0.0, "幸运": 0.0, "索敌": 0.0,
            "防暴击率": 0.0, "回避率": 0.0, "装甲类型": "轻甲",
            "全伤害减免": 0.0, "炮击伤害减免": 0.0, "雷击伤害减免": 0.0,
            "航空伤害减免": 0.0, "轰炸伤害减免": 0.0, "鱼雷伤害减免": 0.0,
            "近战伤害减免": 0.0, "小型攻击伤害减免": 0.0, "中型攻击伤害减免": 0.0,
            "大型攻击伤害减免": 0.0, "高爆弹伤害减免": 0.0, "穿甲弹伤害减免": 0.0,
            "通常弹伤害减免": 0.0, "点燃抵抗": 0.0,
            "ship_type": ship_type, "name": name, "ship_type_damage_reduction": {},
        }