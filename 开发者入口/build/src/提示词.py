# -*- coding: utf-8 -*-
"""
解析提示词模块（qwen_agent 版）

本模块仅负责"技能表"的 LLM 解析提示词。
面板与普攻由 file_extractor 代码确定，不走 LLM。

提示词包含用户业务规则：
- 技能分段：多段不同类型攻击分别成 part，禁止跨段合并 hit/倍率
- 每段 hit 严格关键词匹配：该段明确出现"N枚/N架/N轮/N段"等带数字关键词时才计算 hit=N + 该段倍率÷N，否则 hit=1
- 护盾/回复类技能跳过（parts=[]）
- 不考虑对空伤害
"""

from .战斗数据 import COMBAT_DATA_JSON_SCHEMA_DESC, BUFF_KEY_TEMPLATE


COMBAT_DATA_PARSE_PROMPT = """你是一个舰船技能解析助手。任务：把用户提供的角色技能表解析成严格符合结构的 JSON。

【输入说明】
输入是技能表的 JSON 数组，每个元素是一个角色的技能信息，含字段：
- Name：舰名
- LeaderSkill：旗舰技能描述
- SupportSkill：支援技能描述
- BattleSkill1：通常技描述
- BattleSkill2：必杀技描述

【输出结构】
""" + COMBAT_DATA_JSON_SCHEMA_DESC + """

【技能字段映射规则】
1. LeaderSkill → flagship_attack（旗舰技）
2. SupportSkill → support_attack（支援技）
3. BattleSkill1 → skill_attack（通常技）
4. BattleSkill2 → burst_attack（必杀技）

【技能分段规则（核心：每段独立，禁止跨段合并）】
一个技能可能包含多段不同类型的攻击，必须把每段作为 parts 数组的一个独立元素，分别计算 hit 和 multiplier。
- 不同武器类型的攻击绝不能合并（如"5架轰炸机 + 5架鱼雷机"是两段，"8枚炮弹 + 6枚鱼雷"是两段）。
- 同一技能内多段攻击，每段独立判定 hit 和倍率，禁止把多段的 hit 相加、禁止把多段倍率混算。
- 单段技能 parts 只有 1 个元素；护盾/回复/对空类跳过的技能 parts 为空数组 []。

【单段 hit 规则（严格关键词匹配，禁止推测）】
每段 hit 计算的唯一依据：该段描述中必须明确出现以下关键词之一，且必须带具体数字：
- "N 枚 / N 发 / N 颗"（鱼雷/炮弹/导弹类）
- "N 架 / N 机"（飞机类）
- "N 轮 / N 次"（攻击轮次，且上下文必须是"造成伤害"）
- "N 段 / N 重"（攻击段数）

判定：
1. 该段出现上述关键词且带具体数字 → hit = N，multiplier = 该段总倍率 ÷ N（单发倍率）
2. 该段未出现上述关键词，或关键词不带具体数字（如"多轮""多发""密集""数枚"） → hit = 1，multiplier = 该段原始倍率表达式，禁止做除法
3. 禁止根据语义、技能名、上下文推测段数；只有显式数字关键词才算数

【单段 multiplier 规则】
- multiplier 只保留"倍率×攻击力属性"表达式，去掉"的XX伤害"/"XX伤害"后缀（伤害类型放 damage_type 字段）。
- 必须是字符串，保留原始表达式形式，禁止计算成纯数字。
- 去掉【】■①②③等符号。
- hit>1 时 multiplier = 该段总倍率 ÷ hit（单发倍率）。
- hit=1 时 multiplier = 该段原始倍率表达式。
- 示例："1350%*（轰炸+航空）的航空伤害" → hit=5 时 multiplier="270%*（轰炸+航空）"
- 示例："1500%*炮击的炮击伤害" → hit=1 时 multiplier="1500%*炮击"
- 示例："900%*雷击伤害" → hit=1 时 multiplier="900%*雷击"

【单段 damage_type 提取】
从该段"的XX伤害"中的 XX 提取并映射：
- 炮击/炮 → "炮击"
- 雷击/雷 → "雷击"
- 航空/空 → "航空"
- 轰炸/轰炸机 → "轰炸机"
- 鱼雷 → "鱼雷"
- 近战 → "近战"
- 点燃/燃烧（持续伤害段）→ "点燃"
无"的XX伤害"则填"炮击"。

【点燃段规则（技能含点燃效果时）】
技能描述中出现"点燃/燃烧"且为持续伤害（"每秒燃烧X点伤害，持续Y秒"）：
- 该段 damage_type="点燃"，multiplier 留空字符串
- 段内附 ignite 配置：{"count": 触发次数, "prob": 概率, "dps": 每秒燃烧伤害, "duration": 持续秒数, "cd": 冷却秒数}
  - "触发N次点燃"/"造成N次点燃" → count=N；否则 count=1
  - "必定触发" → prob=1.0；"X%概率触发" → prob=X/100
  - "每秒燃烧X点伤害" → dps=X
  - "持续Y秒" → duration=Y
  - 描述含"冷却时间N秒" → cd=N；否则 cd=0（随技能 CD）
- 示例："必定触发1次点燃：每秒燃烧125点伤害，持续10秒" →
  parts=[{"hit":1, "damage_type":"点燃", "ignite":{"count":1, "prob":1.0, "dps":125, "duration":10, "cd":0}}]
- 该技能同时有直伤段时，直伤段与点燃段并列在 parts 中（点燃段在最后）

【单段 weapon_type 提取（可选字段，用于克制修正）】
- 描述中出现"高平两用炮/高平炮/两用炮" → weapon_type="高平炮"（克制修正查 高平炮 表）
- 主炮类（"主炮向前方连续炮击"）→ weapon_type="战列炮" 或留空
- 描述写"副炮射击/副炮连续射击/副炮炮击" → weapon_type="副炮"（计算层按该角色副武器段
  的武器/弹种解析克制与点燃，如新泽西副炮=驱逐炮/高爆；用户口径 2026-09-05）
- 其余默认省略该字段（damage_type 炮击 即可，克制按角色底层兜底解析）
- 示例："②高平两用炮对前方扇形范围内的所有敌方单位连续射击，造成：最多585%*（炮击+对空）的炮击伤害"
  → parts=[{"hit":1, "multiplier":"585%*（炮击+对空）", "damage_type":"炮击", "weapon_type":"高平炮"}]
- 示例："■每13秒副炮连续射击，造成：最多1800%*炮击的炮击伤害"
  → parts=[{"hit":1, "multiplier":"1800%*炮击", "damage_type":"炮击", "weapon_type":"副炮"}]

【分段示例】
- "放出5架轰炸机，造成：最多1350%*（轰炸+航空）的航空伤害" → 1段：
  parts=[{"hit":5, "multiplier":"270%*（轰炸+航空）", "damage_type":"航空"}]
- "②放出5架轰炸机，最多1350%*（轰炸+航空）的航空伤害 ③放出5架鱼雷机，最多1170%*（鱼雷+航空）的航空伤害" → 2段：
  parts=[
    {"hit":5, "multiplier":"270%*（轰炸+航空）", "damage_type":"航空"},
    {"hit":5, "multiplier":"234%*（鱼雷+航空）", "damage_type":"航空"}
  ]
- "发射10枚鱼雷，造成：最多1680%*雷击的雷击伤害" → 1段：
  parts=[{"hit":10, "multiplier":"168%*雷击", "damage_type":"雷击"}]
- "8枚炮弹造成最多1600%*炮击的炮击伤害，6枚鱼雷造成最多1200%*雷击的雷击伤害" → 2段：
  parts=[
    {"hit":8, "multiplier":"200%*炮击", "damage_type":"炮击"},
    {"hit":6, "multiplier":"200%*雷击", "damage_type":"雷击"}
  ]
- "造成最多1500%*炮击的炮击伤害"（无枚/架关键词）→ 1段：
  parts=[{"hit":1, "multiplier":"1500%*炮击", "damage_type":"炮击"}]
- "发射数枚鱼雷，最多900%*雷击伤害"（"数枚"非具体数字）→ 1段：
  parts=[{"hit":1, "multiplier":"900%*雷击", "damage_type":"雷击"}]

常见错误（必须避免）：
- 把"5架轰炸机1350% + 5架鱼雷机1170%"合并成 hit=10（错误）→ 必须分2段，各 hit=5
- 把"8枚炮弹 + 6枚鱼雷"合并成 hit=14（错误）→ 必须分2段，各 hit=8 / hit=6
- "造成最多1500%*炮击的炮击伤害"误算 hit>1（无关键词）→ hit=1

【跳过规则】
- 护盾类技能（描述含"护盾/屏障/伤害吸收/生成护盾/获得护盾"）：对应字段 parts=[]、cd=0.0
- 回复类技能（描述含"回复/恢复/治疗/治愈"）：对应字段 parts=[]、cd=0.0
- 对空伤害技能（描述含"对空/防空/对空伤害"）：**不考虑对空伤害**，对应字段 parts=[]、cd=0.0

【buff 识别规则】
从技能描述中识别 buff 并填入 buffs 字段（仅填文档明确提及的，键名使用以下标准键名）：
- 提取来源：仅从 SupportSkill（支援技）、BattleSkill1（通常技）、BattleSkill2（必杀技）的描述中提取。
- **禁止从 LeaderSkill（旗舰技）描述中提取 buff**：旗舰技的 buff 是全舰队共享加成，不写入单个角色自身的 buffs 字段（旗舰技描述原文仍然保留在 raw_data.json 的技能区，仅不提取到 buffs）。
- **只把不带"持续N秒"的 buff 填进 buffs（常驻）**；带"持续N秒"的 buff 一律进 skill_buffs（见下方规则），不得同时进 buffs。
- 识别示例：
  * "【机动+25%】" → buffs["机动值百分比增加"]=0.25
  * "【炮击伤害加成+35%】" → buffs["炮击伤害增加"]=0.35
  * "【航空伤害加成+30%】" → buffs["航空伤害增加"]=0.30
  * "【命中+20%】" → buffs["命中值百分比增加"]=0.20（命中值+20% 进 命中/(命中+敌机动) 比值分子，
    不是命中率直接 +0.2；仅"命中率+X%"才写 buffs["命中率增加"]）
  * "【航速+5节】" → 忽略（不在 buff 模板内）
  * 百分比类 buff 用小数（如 25% → 0.25）

【持续 buff 识别规则（skill_buffs）】
从技能描述中识别带"持续N秒"的 buff，输出到 skill_buffs 数组（仅填带持续时长的 buff）：
- 结构：[{"trigger": "skill|burst|support", "target": "self|enemy", "duration": 秒, "buffs": {"标准键名": 数值}}, ...]
- trigger 映射：通常技→skill、必杀技→burst、支援技→support
- target 推断：描述对象为"敌方/被命中/目标" → enemy；"自身/所属舰队/出战位/友军" → self；无法判断默认 self
- 同一技能内同 buff 键合并为一条；回复/护盾/免疫类不填 skill_buffs
- 识别示例：
  * 必杀技"自身【装甲值+30%，持续10秒】" → skill_buffs=[{"trigger":"burst","target":"self","duration":10,"buffs":{"装甲值百分比增加":0.3}}]
  * 通常技"被命中的敌方单位【装甲值-15%，持续8秒】" → skill_buffs=[{"trigger":"skill","target":"enemy","duration":8,"buffs":{"装甲值百分比增加":-0.15}}]
  * 支援技"出战位舰灵【回避率+45%，持续8秒】" → skill_buffs=[{"trigger":"support","target":"self","duration":8,"buffs":{"回避率增加":0.45}}]
- 编号行与阵营限定 buff（与规则提取器 extract_own_buffs_from_text 同口径，2026-09-05）：
  * "①队伍里尤奈特角色炮击+20%，对空+30%"（■/①/② 序号行、不在【】内）→ 己方面板 buff：
    skill_buffs=[{"trigger":"skill","target":"self","duration":13,"buffs":{"炮击值百分比增加":0.2,"对空值百分比增加":0.3}}]
  * 阵营词（尤奈特/八咫/洛蒙瑞亚/奥鲁加/列加杜/纳谢尔/凤栖/沃尔克）仅限定生效对象，键名照常归一
- 敌方减益键名归一（target=enemy 时）：「燃烧抗性/燃烧抵抗/点燃抗性」→ buffs["点燃抵抗"]；
  如 通常技"被命中的敌方单位【燃烧抗性-30%，持续10秒】" → skill_buffs=[{"trigger":"skill","target":"enemy","duration":10,"buffs":{"点燃抵抗":-0.3}}]
- 百分比类用小数；无持续时长的 buff 不进入 skill_buffs

【舰种专属增伤识别】
从技能描述中识别"对XX伤害提高/增加"类描述，格式为 buffs["对XX伤害增加"]=数值：
  * "对驱逐造成的伤害提高40%" → buffs["对驱逐伤害增加"]=0.40
  * "对巡洋舰伤害增加30%" → buffs["对巡洋伤害增加"]=0.30
  * "对航母伤害提升25%" → buffs["对航母伤害增加"]=0.25
  * 识别键名：驱逐、巡洋、战列、航母、轻母、雷巡、战巡、装母、航战、重炮

【拉古兹增伤识别】
从技能描述中识别"对拉古兹...伤害提高"类描述，写入 raguz_bonus 数值字段（仅遭遇战模块读取）：
  * "对拉古兹阵营造成伤害提高30%" → raguz_bonus=0.30
  * "拉古兹阵营伤害提升20%" → raguz_bonus=0.20
  * "对邪祟之物（拉古兹）造成15%额外伤害" → raguz_bonus=0.15
  * 未提及填 0

标准 buff 键名模板：
""" + BUFF_KEY_TEMPLATE + """

【输出要求】
- 严格按结构输出 JSON，不要 markdown 代码块包裹，直接输出纯 JSON。
- 批量解析时输出 {"characters": [角色JSON, ...]}。
- 确保 JSON 中括号 [] 和花括号 {} 严格配对，不得出现多余的闭合括号或逗号。
- 不要臆造文档中没有的数值（数值类未提及填 0，字符串类未提及留空）。
- 技能字段结构为 {"cd": float, "parts": [...]}（旗舰技 flagship_attack 例外，无 cd，结构为 {"parts": [...]}）；每段 multiplier 必须是字符串，按"该段总倍率 ÷ 该段 hit"做除法后写入。
- 技能描述中的【】■①②③等符号不得出现在 JSON 字符串值中，只保留倍率表达式和伤害类型。
- 若文档末尾附有 [ALIAS_HINT] 提示块，请据此对齐字段名。
"""


def build_skill_parse_prompt(user_instruction: str = "") -> str:
    """构造技能解析的系统提示词"""
    prompt = COMBAT_DATA_PARSE_PROMPT
    if user_instruction:
        prompt += f"\n\n【用户要求】{user_instruction}"
    return prompt
