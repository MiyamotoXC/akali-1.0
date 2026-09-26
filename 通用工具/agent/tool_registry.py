# -*- coding: utf-8 -*-
"""绑定给 LLM 的工具策展（Tier-1）+ 三分支分类。

LLM 每次只做一次判定：①调「数据工具」 ②调「检索工具」 ③都不调（闲聊/调情）。
两类工具的执行语义不同：
- **数据工具**（DATA_TOOLS）：确定性执行，回复**原文直出**（LLM 不得转述）。
- **检索工具**（RETRIEVAL_TOOLS）：向量检索片段 → LLM **依据片段**简答（约束式生成，
  这是"片段例外"，与闲聊的自由生成不同）。
- 都不调 → 交回 web_app 闲聊链路（闲聊/调情是**唯一**自由生成通路）。

为什么不全绑 30 个：全量描述 ≈3–4k tokens/次，且命名高度易混
（query_guide / guide_rag；query_ship_info / query_ships_by_skill / query_talent_ships）。
策展到无状态「读/检索」类后歧义大降，且**不损失能力**——其余分支
（伤害计算状态机、表单流程、会话写入）本就由确定性代码更可靠地处理。

环境变量 AKALI_AGENT_TOOLS=core|full 控制绑定范围。
"""
import os

# 数据工具：确定性读/查，回复原文直出
DATA_TOOLS = (
    'query_ship_info',
    'query_ships_by_skill',
    'ship_filter',
    'query_talent_ships',
    'query_scout_sum',
    'query_guide',
    'ammo_mech_qa',
    'query_activity',
    'query_weather',
    'calc_favor',
    'calc_reload_cd',
    # PVP 参考阵容统计（"pvp练谁/阵容推荐"的确定性固定输出，与 legacy 同源）
    'pvp_role_recommend',
)

# 检索工具：向量检索片段 → LLM 依据片段简答
# kb_search 不绑：它是 legacy 攻略流程内部的兜底检索（输出原始摘录），
# 不是面向用户的作答出口；知识问答统一走 guide_rag。
RETRIEVAL_TOOLS = (
    'guide_rag',
)

TIER1 = DATA_TOOLS + RETRIEVAL_TOOLS

# full 模式追加的只读工具（默认不绑，避免 prompt 膨胀）
TIER1_FULL_EXTRA = (
    'query_ship_copy',
    'list_user_ships',
)

# 有界工具循环白名单（增强②）：仅这些确定性、原文直出的**数据查询**工具可作为同轮第二个调用
# （数据→数据）。禁止 guide_rag / query_guide / 闲聊进入循环，避免把检索/自由生成塞进串行链、
# 破坏"原文直出"口径。示例：先 query_ship_info 再 query_talent_ships / query_ships_by_skill。
LOOPABLE_DATA_TOOLS = (
    'query_ship_info', 'query_talent_ships', 'query_ships_by_skill',
    'ship_filter', 'query_scout_sum', 'ammo_mech_qa',
)


def is_retrieval(name: str) -> bool:
    """该工具是否属于「检索→依据片段作答」分支。"""
    return (name or '') in RETRIEVAL_TOOLS


def selected_names() -> tuple:
    scope = os.environ.get('AKALI_AGENT_TOOLS', 'core').strip().lower()
    if scope == 'full':
        return TIER1 + TIER1_FULL_EXTRA
    return TIER1


def select_tools(tools):
    """从 build_tools() 结果中筛出 Tier-1（保持原顺序、按 name 去重）。"""
    want = set(selected_names())
    seen = set()
    out = []
    for t in tools or []:
        name = getattr(t, 'name', '') or ''
        if name in want and name not in seen:
            seen.add(name)
            out.append(t)
    return out
