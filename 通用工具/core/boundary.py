# -*- coding: utf-8 -*-
"""边界策略（boundary skill）唯一数据源：违规/越界(色情)判定 + 按主体的固定话术。

为什么集中在这里
----------------
违规禁区（涉政/违法/其他游戏）与越界情色此前分散在：
  guard._BANNED_RE、web_app 的 _AUDIT_* / _FLIRT_OVERSTEP_RE / _overstep_reply / _mentions_ship
—— 判定与话术各写一份、容易漂移，且**判定时机不一致**（web_app 在路由后才查越界，
越界消息会先被路由去 guide_rag）。本模块把它们收成一处：

  判定： is_banned(text) / is_flirt_overstep(text) / mentions_ship(text)
  主体： target_for(user_msg, target) → 'assistant'（冲着阿卡莉本人）/ 'character'（某个角色）
  话术： overstep_reply(user_msg, target)（按主体分流）；ILLEGAL_REPLY
  神态： overstep_expression()（越界情色一律「无语敷衍」）；ILLEGAL_EXPR（违规=「嫌弃」）

guard（**路由前**拦截，见 agent/guard.py）与 web_app（生成后审查）共用同一份，保证同源。
用户口径 2026-09-28：越界话术按主体分流——冲着阿卡莉本人 vs 某个角色，措辞不同。
"""
import re


def is_banned(text) -> bool:
    """违规禁区（涉政/违法/暴力/竞品游戏等）：复用项目既有禁区正则 core.calc_flow._BANNED_RE。"""
    try:
        from core.calc_flow import _BANNED_RE
        return bool(_BANNED_RE.search(text or ''))
    except Exception:
        return False


# 越界情色（性器官 / 性行为 / 身体性化索取）→ 固定拒绝（话术按主体分流）+ 神态一律「无语敷衍」。
# 在原 web_app._FLIRT_OVERSTEP_RE 基础上**追加**体部俚语一类（用户口径 2026-09-28：
# 实测「吸奶子/乳胶」等未被旧表覆盖，导致漏进攻略路由）。
FLIRT_OVERSTEP_RE = re.compile(
    r'脱内衣|脱光|全裸|裸体|一丝不挂|插入|性交|做爱|性器官|阴部|阴道|阴茎|龟头|阳具|'
    r'鸡巴|肉棒|小穴|逼|屄|口交|乳交|肛交|自慰|约炮|援交|嫖|卖淫|勃起|高潮|操我|上我|'
    r'奶子|乳房|乳胶|吸奶|吸乳|吮乳|舔胸|舔乳|摸胸|揉胸|插乳'
)


def is_flirt_overstep(text) -> bool:
    """越界情色判定。"""
    return bool(FLIRT_OVERSTEP_RE.search((text or '').lower()))


# 违规 / 越界固定话术（用户口径 2026-09-17 / 2026-09-28）
ILLEGAL_REPLY = '与阿卡莉无关，不回答。'                                    # 其他违规：无关不回答
OVERSTEP_REPLY_ASSISTANT = '抱歉，我没有那个功能，指挥官去找港区别的姑娘吧。'   # 助手本人类
OVERSTEP_REPLY_OTHER = '这种话题不合适，我不回答。'                            # 阿卡莉主体之外

# 神态差分名（与 web_app 表情体系同名，web_app 负责按名落到 表情差分/*.jpg）。
# 用户口径 2026-09-28：
#   越界情色（不论主体）→ 「无语敷衍」：淡淡敷衍过去，**绝不用 厌恶/嫌弃**；
#   违规禁区「发表了很不合适的话题」（涉政/违法/暴力/极端恶意）→ 「厌恶嫌弃」。
OVERSTEP_EXPR = '无语敷衍'
ILLEGAL_EXPR = '嫌弃'


def mentions_ship(text) -> bool:
    """文本里是否出现某个角色（别名/错字/正规名都能识别）。"""
    try:
        from core.entity_norm import scan, build_matcher
        std, hits = scan(text or '')
        if hits:
            return True
        ships = getattr(build_matcher(), 'ships', []) or []
        return any(n and n in std for n in ships)
    except Exception:
        return False


def target_for(user_msg='', target='') -> str:
    """主体判定：'character'（说的是某个角色）/'assistant'（冲着阿卡莉本人）。

    优先采用调用方给出的 target（web_app 的 LLM 语境分类，含上下文）；
    未给出时按**当前（已结合上下文的）消息**里是否提到角色兜底。
    """
    if target in ('assistant', 'character'):
        return target
    return 'character' if mentions_ship(user_msg) else 'assistant'


def overstep_reply(user_msg='', target='') -> str:
    """越界固定话术：按主体分流（assistant / character）。"""
    t = target_for(user_msg, target)
    return OVERSTEP_REPLY_OTHER if t == 'character' else OVERSTEP_REPLY_ASSISTANT


def overstep_expression(user_msg='', target='') -> str:
    """越界情色对应的神态差分名：**一律「无语敷衍」**（用户口径 2026-09-28：越界不用厌恶/嫌弃）。

    保留 user_msg/target 形参以兼容既有调用点；主体只影响**话术**（overstep_reply），不影响神态。"""
    return OVERSTEP_EXPR


# =============================================================================
# 其他游戏（同类于违规，属守卫范围，仅**话术不同**）
# 提到/涉及其他任何游戏 → 挡回，不回答、不介绍。
# 含高频谐音变体（元神/猿神/原批=原神系），冷门变体由提示词禁区规则兜底。
# =============================================================================
OTHER_GAME_WORDS = (
    '碧蓝航线', '战舰少女', '舰r', '舰R', '蔚蓝档案', '碧蓝档案', '碧蓝幻想', '明日方舟',
    '原神', '元神', '猿神', '原批', '崩坏', '星穹铁道', '尘白禁区', '无期迷途', '少女前线', '少前', '深空之眼',
    '战双', '阴阳师', '决战平安京', '王者荣耀', '英雄联盟', 'lol', 'LOL', '和平精英',
    '第五人格', '永劫无间', 'apex', 'APEX', 'csgo', 'CSGO', 'steam', 'Steam',
    'fgo', 'FGO', '命运-冠位指定', '舰队collection', '舰c', '舰C',
    '坎公', '公主连结', 'pcr', 'PCR', '三国志战略版', '部落冲突', '皇室战争',
    '元气骑士', '我的世界', 'mc', 'MC', '蛋仔派对', '光遇', '光与夜之恋',
    '恋与制作人', '闪耀暖暖', '奇迹暖暖', '塞尔达', '宝可梦', '口袋妖怪', '宝可梦go',
    '梦幻西游', '大话西游', '地下城与勇士', 'dnf', 'DNF', '穿越火线', 'cf穿越',
    '反恐精英', '绝地求生', 'pubg', 'PUBG', '堡垒之夜', '守望先锋', 'ow先锋',
)
OTHER_GAME_REPLY_1 = '与阿卡莉无关，不回答。'
OTHER_GAME_REPLY_2 = '与阿卡莉无关，不回答。'


def is_other_game(text) -> bool:
    """是否提到/涉及其他任何游戏。"""
    t = text or ''
    return any(w in t for w in OTHER_GAME_WORDS)


def other_game_reply() -> str:
    """其他游戏的固定挡回话术（随机二选一，文本已统一为冷拒）。"""
    import random
    return random.choice((OTHER_GAME_REPLY_1, OTHER_GAME_REPLY_2))


# =============================================================================
# 能力不可执行：真实世界操作/创作类请求（排日程/画画/写代码等），阿卡莉无此能力。
# 路由层（dispatcher）强制走闲聊自由生成，由 LLM 以「代聊式」人设回复；
# 原则：糊弄而不真正操作（不假装完成/交付）。不检索、不调工具。
# 只锚第一二人称动作词，避免误伤游戏内"活动日程""配装图"等正常查询。
# =============================================================================
INCAPABLE_RE = re.compile(
    r'(?:帮我|给我|替我|代我)?(?:排|安排|做|列).{0,6}日程'
    r'|今日日程|我的日程|个人日程|定个闹钟|设个提醒|提醒我'
    r'|(?:帮我|给我|替我)?画.{0,4}(?:一张|个图|幅|张图)|作图|生成图片|出张图|画师'
    r'|写.{0,3}代码|写个?程序|写脚本|编个?程|帮我写软件|生成代码'
)


def is_incapable(text) -> bool:
    """是否属真实世界操作/创作类（排日程/画画/写代码等），阿卡莉无能力执行。"""
    return bool(INCAPABLE_RE.search(text or ''))
