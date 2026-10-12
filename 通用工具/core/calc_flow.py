# -*- coding: utf-8 -*-
"""
后端硬编码的意图路由 + 计算流程状态机。
工作类回复（查询/计算/攻略/装备/遭遇战/天气/时间）完全由后端固定输出，不走 LLM；
仅当无法路由到任何工作类时，才交回 chat 接口走 LLM（纯闲聊）。
"""
import datetime
import re
import random
import json
import math

from core.damage_formula import DamageCalculator
from core.output_handler import OutputHandler
from core import intents
from tools.session import _parse_buff_str
from tools.common import (_load_combatants, _resolve_calc_context, _resolve_course_air,
                              _normalize_buff_key, _ATK_PANEL_FIELDS, _DEF_PANEL_FIELDS,
                              _PANEL_MAIN_ATK, _PANEL_MAIN_DEF,
                              _load_guide_kb, _normalize_ship_input)
from core.encounter_simulator import (validate_encounter_input, validate_pvp_input,
                                      simulate_encounter, simulate_pvp, load_encounter_bosses)
from data.user_data import get_ship_dict


# ============ 意图关键词 ============
_GUIDE_WORDS = ('攻略', '新手', '萌新', '入坑', '练级', '配装', 'PVE', '拉古兹',
                '竞技场', '无尽', '一图流', '速查', '推图', '百科', '机制', '装备',
                '强化', '培养', '养成')
_CALC_WORDS = ('算', '伤害', '打', '计算')
_ENCOUNTER_WORDS = ('遭遇战', 'boss', 'BOSS', '打boss', '模拟')
_WEATHER_WORDS = ('天气', '热不热', '冷不冷', '下雨', '气温', '温度')
_QUERY_WORDS = ('查询', '查一下', '查', '面板', '技能', '天赋', '属性', '介绍')
# 查询意图硬信号：仅当输入同时含"查询信号词 + 可解析角色名"才路由查询面板，
# 杜绝"只因出现船名/关键字就触发查询"（如"女灶神很强"不应查面板，而应走闲聊/攻略）。
_QUERY_INTENT_RE = re.compile(
    r'查|查询|看看|面板|技能|天赋|属性|数据|详情|资料|信息|介绍|是啥|是什么|是谁|'
    r'怎么样|怎样|咋样|长啥样|如何|啥样|立绘|画像|长相')
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 角色名录裸查询（"查角色/角色列表/所有角色/有哪些舰灵"…）：无具体船名 → 前端角色面板
_SHIP_LIST_RE = re.compile(
    r'^(?:请?帮我)?(?:查|查询|查看|浏览|打开|列出|显示|看看|来)?'
    r'(?:角色|舰灵|舰娘|船坞|船)(?:列表|名单|目录|一览|查询|大全|有哪些|资料|信息)?[的]?$'
    r'|^(?:查|查询|查看|浏览|打开|列出|显示|看看)?'
    r'(?:所有|全部|有哪些|有什么|都有哪些|都有什么|全部的?)(?:角色|舰灵|舰娘|船坞|船)(?:列表|名单|目录|一览)?[的]?$'
)

# 伤害类型按钮：label → 工具名（SUGGEST_CALC 标记与 step6 识别共用）
DAMAGE_BUTTONS = [
    ('普攻', 'calc_normal_attack'),
    ('通常技', 'calc_skill_attack'),
    ('必杀技', 'calc_burst_attack'),
    ('支援技', 'calc_support_attack'),
    ('点燃', 'calc_burn_damage'),
    ('总伤害', 'calc_total_damage'),
    ('结束本轮计算', 'end_round'),
]

# 伤害公式一图流（进入计算类型菜单时 / 询问伤害计算公式时输出一次；/kb/ 静态路径）
_CALC_FORMULA_IMAGE = {'path': '图片素材/伤害公式一图流.webp', 'title': '伤害公式一图流'}

# 引导语模板（人设口吻，随机）
_ASK_ATTACKER = [
    "噢，指挥官，请先选择攻击方：",
    "指挥官，先告诉我攻击方是谁：",
    "好，先定攻击方：",
]
_ASK_DEFENDER = [
    "请输入防守方角色名（仅支持已有角色）：",
    "防守方选哪位？（仅支持已有角色）",
]
_ASK_DEFENDER_REQ = [
    "必须选择防守方，且仅支持已有角色，请输入防守方角色名：",
]
_ASK_BUFF_ATK = [
    "收到。请输入攻击方 buff/天赋（不填则用默认）：",
    "收到。请输入攻击方 buff/天赋（不填则用默认）：",
]
_ASK_BUFF_DEF = [
    "收到。请输入防守方 buff/天赋（不填则用默认）：",
    "收到。请输入防守方 buff/天赋（不填则用默认）：",
]
# 阵型/航向/制空 选项说明（询问航向时展示）。
# 制空描述只列航空增伤；轰炸/鱼雷不写出，但计算时制空对航空/轰炸机/鱼雷三类仍全部生效。
_COURSE_AIR_OPTIONS = (
    "默认（机动 / 同航 / 制空均势）\n"
    "阵型\n"
    "单纵（炮击、雷击伤害+12%）\n"
    "复纵（机动+25%）\n"
    "轮型（航空、对空+15%，炮击、雷击-5%）\n"
    "梯型（暴击率+15%，暴击伤害+5%）\n"
    "航向\n"
    "T优（炮击、雷击+15%，命中+20%）\n"
    "同航（炮击、雷击+8%，命中+10%）\n"
    "反航（炮击、雷击-8%，命中-10%）\n"
    "T劣（炮击、雷击-15%，命中-10%）\n"
    "制空\n"
    "确保（航空+15%，命中+10%）\n"
    "优势（航空+8%，命中+5%）\n"
    "均势（无修正）\n"
    "劣势（航空-8%，命中-5%）\n"
    "丧失（航空-15%，命中-10%）"
)
_ASK_COURSE_AIR = [
    "请指挥官选择 阵型、航向、制空状况。点默认按钮则使用 机动/同航/制空均势，点调整按钮则选择三行选项：",
    "请选择 阵型、航向、制空状况（默认 机动/同航/均势）：",
]
_ASK_TIME = [
    "噢，指挥官，需要指定时间跨度。请告诉我计算多少秒？",
    "要算累计伤害就得给个时间，多少秒？",
]
_ASK_SKILL_SELECT = [
    "噢，指挥官，请选择伤害计算类型：",
    "选一个伤害类型吧：",
]


def _pick(templates):
    return random.choice(templates)


# 游戏化时段描述（拂晓世界观：白天/黑夜/演习/推图/港区作息）
def _game_period_desc(hour: int) -> str:
    if 5 <= hour < 9:
        return "拂晓刚过，港区的日光洒在海面上，正是备战的好时候"
    if 9 <= hour < 12:
        return "上午的海风正好，适合推图和演习"
    if 12 <= hour < 14:
        return "正午，太阳正烈，舰队多在做补给休整"
    if 14 <= hour < 18:
        return "下午光线充足，出击练兵都不耽误"
    if 18 <= hour < 23:
        return "入夜了，港区灯火通明，夜战演习也可以安排"
    return "深夜，除了值守的舰灵，港区都歇下了"


def _time_reply() -> dict:
    """询问当前时间/日期 → 固定输出（含游戏化时段 + 阿卡莉口吻，随机变体）。"""
    now = datetime.datetime.now()
    date_str = "%d年%d月%d日" % (now.year, now.month, now.day)
    hm = now.strftime("%H:%M")
    week = _WEEKDAYS[now.weekday()]
    period = _game_period_desc(now.hour)
    variants = [
        f"噢，现在是{date_str}，{week}，{hm}。{period}。",
        f"{date_str}，{week}，{hm}。{period}，指挥官。",
        f"当前港区时间：{date_str} {week} {hm}。{period}。",
        f"{hm}，{week}。{date_str}。{period}。要安排出击吗？",
    ]
    return {'reply': random.choice(variants), 'tool_outputs': [], 'suggestions': [],
            'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


def _is_time_query(msg: str) -> bool:
    """判断是否为「询问当前时间/日期」的闲聊问法。
    - 命中时间问法（现在几点/几点了/几月几号/星期几/日期/现在时间/现在什么时候…）→ 是
    - 攻略/计算/刷新/维护类语境（什么时间打、几点刷新、几点开放、切换、算秒数 等）→ 否
    """
    m = msg.strip().rstrip('？?！!。，,、~～ ')
    if not m or len(m) > 16:
        return False
    # ① 攻略/计算/刷新/切换/维护类语境排除（含"几点方便打/几点是白天黑夜切换"等歧义）
    if re.search(
        r'什么时间[打刷开更上线切换]'
        r'|几点[^，。！？]{0,5}(?:方便|适合|可以|该|去打|开打|刷新|上线|开放|维护|重置|切换|开始|结束|开|更|刷|是白天|是黑夜)'
        r'|刷新|上线|开放|维护|重置|切换|方便打|适合打|开始打|结束'
        r'|攻略|推荐|阵容|怎么打|如何打|怎么玩|如何玩'
        r'|多少秒|几秒|时间跨度|持续|秒数|冷却|cd\b'
        r'|算.{0,6}(?:时间|伤害)|(?:时间|伤害).{0,6}算',
        m,
    ):
        return False
    # ② 明确时间问法
    return bool(re.search(
        r'(?:现在|当前|今天|今日)?(?:几点|几点钟|几点了|几点啦|几时|几月几号|几月几日|星期几|周几|几号|日期)'
        r'|现在(?:时间|什么时间|什么时候)|什么时候了|今天几号|今天周几|时间了',
        m,
    ))


# ============ 资源不足闲聊：规划建议 → 追问 → 表情包「氪啊」 ============

# 资源不足感叹识别（油耗/经费/钻石/资源 等 + 不足/紧张/大/缺）
# 仅限「游戏内资源」，词表全部取自知识库真实资源名（攻略QA库「二、主要资源简介」+ tips.json）：
#   油 / 晶币 / 钻石 / 抽卡券 / 弹药 / 核心 / 徽章 / 装备箱 / 资源（泛指）
# 现实财务词（钱/资金/没钱/经费/金币）及非库内词（钢材/铝材/物资/材料/油料/油耗）一律不在此列，
# 避免瞎编或现实语境误触发「氪啊」。
_RESOURCE_LACK_WORDS = ('油', '晶币', '钻石', '抽卡券', '弹药', '核心', '徽章', '装备箱', '资源')
_RESOURCE_LACK_SRC = (
    r'(?:{res})[^。！？]{{0,8}}(?:不够|不足|紧缺|紧张|好大|太大|很大|太高|太高了|缺|没了|没啦|花完|花光|用光|耗光|吃紧|见底|告急)'
    r'|(?:{res})[。！？!]?$'
    r'|(?:最近|现在|如今)[^。！？]{{0,12}}(?:{res})[^。！？]{{0,8}}(?:不够|不足|紧张|紧缺|很大|太大|高|贵|缺|花|耗|涨)'
    r'|(?:资源|油)[^。！？]{{0,6}}(?:规划|分配|怎么用|怎么花|安排)'
    r'|(?:好|太|很|有点|快)?(?:缺|没|少|紧)(?:{res})'
    r'|(?:缺|没|少)(?:{res})'
).format(res='|'.join(_RESOURCE_LACK_WORDS))
_RESOURCE_LACK_PAT = re.compile(_RESOURCE_LACK_SRC)

# 表情包追问判定：钩子后用户下一条输入只要意图是「疑问类」→ 都给表情包
#（疑问词/问号结尾/追问语气均算；不限定具体追问措辞）
_STICKER_Q_RE = re.compile(
    r'什么|怎么|咋|如何|为啥|为什么|哪|谁|啥|干嘛|干吗|吗|么|呢|'
    r'讲|说|继续|然后|还有|说来|听听|教|求|方法|办法|招|主意|指点|透露|求解|帮|真的|是啥|是么|讲讲'
)
_STICKER_Q_END = re.compile(r'[？?]$|[吗么呢]$|那?[咋怎].{0,4}吧$')

# 充值/氪金 意图（用户追问时点出「充值」即揭晓最佳解决办法）
_RECHARGE_INTENT_RE = re.compile(r'充值|氪|充钱|充钻石|花钱|砸钱|氪金|课金|648|首充|月卡|补款')

# 明确其它意图（新话题）：钩子后这类输入不是追问，须清钩子交回原路由
#（事故：/「明天天气如何」含「如何」被当追问 → 答「最佳解决办法，充值」）
_STICKER_OTHER_INTENT = re.compile(
    r'天气|气温|温度|下雨|几度|热不热|冷不冷|'
    r'几点|几号|星期|日期|现在什么时间|'
    r'查|查询|面板|属性|技能|天赋|装备|索敌|舰灵|角色|船'
    # 注意：不含「怎么办/怎么过」——钩子开启后的「怎么办」就是对办法的追问（应给氪啊）
    r'|怎么(?:打|配|玩|练|算|弄|强化|升级|培养|攻略|计算|刷|推)'
    r'|如何(?:打|配|玩|练|算|攻略|刷)|'
    r'攻略|推荐|阵容|配队|配装|'
    r'遭遇战|模拟|pvp|boss|拉古兹|无尽海域|争锋|竞技场|'
    r'活动|节日|礼包|签到|远征|委托|'
    r'算|计算|伤害|dps|'
    r'我的船|我的角色|保存|删除'
)


def _is_sticker_followup(msg: str) -> bool:
    """钩子后的疑问类输入判定（疑问词 / 问号结尾 / 疑问语气词 均算追问）。
    纯确认语气（好吧/就这样吧/行吧）不算。
    **明确其它意图（新话题）不算追问**：带天气/时间/查询/计算/攻略/活动/装备等实体词时
    是新的工作请求（如「明天天气如何」），须清钩子交回原路由，不能答「氪啊」。"""
    m = (msg or '').strip()
    if not m:
        return False
    # 明确其它意图 → 不是对钩子的追问（如「明天天气如何」「查胡德面板」「缺油怎么办」除外）
    if _STICKER_OTHER_INTENT.search(m):
        return False
    # 纯确认语气排除（好吧/就这样吧/行吧/算了吧 等；带问号的不算确认）
    if not _STICKER_Q_END.search(m) and re.fullmatch(
            r'(?:好|行|嗯|哦|这样|就(?:这样|算了)|算了吧?|行吧|好吧?|可以|没问题|知道了?|没别的了)[吧。！!\s]*', m):
        return False
    if _STICKER_Q_END.search(m):
        return True
    if _STICKER_Q_RE.search(m):
        return True
    return False


_GUIDE_TITLE_MAP = None


def _guide_title_of(src: str) -> str:
    """chunk source（如 '6.1 练级百科.md'）→ 攻略正式名（如 '练级百科'）。

    用户口径 2026-09-09：来源只写完整攻略名，不写 md 文件名与编号。
    优先用 KB documents 的 title（按 file 的 basename 去扩展名匹配，如 '6.1 练级百科'）；
    兜底剥前导编号（'6.1 '/ '7.2 '/ '1 ' / '4-'）与扩展名。问答补充类走兜底原样。"""
    s = (src or '').strip()
    if not s:
        return s
    global _GUIDE_TITLE_MAP
    base = re.sub(r'\.(?:md|txt|docx?|png|jpe?g)$', '', s, flags=re.I).strip()
    if _GUIDE_TITLE_MAP is None:
        m = {}
        try:
            for d in (_load_guide_kb().get('documents') or []):
                f = (d.get('file') or '').strip()
                t = (d.get('title') or '').strip()
                if f and t:
                    b = re.sub(r'\.[A-Za-z0-9]+$', '', f.replace('\\', '/').rsplit('/', 1)[-1])
                    m[b] = t
        except Exception:
            m = {}
        _GUIDE_TITLE_MAP = m
    if base in _GUIDE_TITLE_MAP:
        return _GUIDE_TITLE_MAP[base]
    return re.sub(r'^\d+(?:[.\-]\d+)*\s*[-、]?\s*', '', base).strip() or base


def _resource_acquire_from_guide(lacked: str):
    """资源不足场景专属检索：返回 (正文, 来源集合)。

    正文 = 攻略库里该资源的获取方式（已清洗）；来源集合 = 命中 chunk 的 source 名（去重）。
    无命中返回 (None, None)。

    不用 GuideRAG：其 RRF 融合 + 0.5 阈值会把唯一讲『获取』的片段过滤掉（如钻石的
    「二、主要资源简介」一句「每日都可白嫖获得」向量分仅 ~0.45–0.58，被融合重排踢出 top 且
    达不到 0.5 阈值），LLM 据此还会误报『攻略库中未提及』。这里改用 retrieve_guide 纯向量
    检索（低阈值 0.40），再用确定性规则抽取含该资源的原文句直接呈现，避免 LLM 编造/误判，
    保证『从向量库、不瞎编』。来源集合用于回复末尾标注对应攻略出处（问答补充类不写来源）。"""
    try:
        from tools.knowledge import retrieve_guide
    except Exception:
        return None, None
    q = f'{lacked} 资源简介 获取 获得 白嫖 产出'
    try:
        res = retrieve_guide(q, topk=10)
    except Exception:
        return None, None
    if not res:
        return None, None
    # 获取意图优先：优先抽取「含该资源 + 含获取类动词」的句子（获得/获取/白嫖/签到/任务/活动/每日…
    # 等），真正回答「怎么获取」；仅当没有任何获取类句子时，才回落到「含该资源」的全部句子，避免把
    # 用途/消耗说明（如钻石使用推荐、开建造栏）误当成获取方式。
    _ACQ_KW = re.compile(r'获得|获取|白嫖|产出|签到|礼包|开源|攒|入手|掉落|补给')
    seen = set()
    tier1, tier2 = [], []
    sources = set()
    for m, sc in res:
        if sc < 0.40:
            continue
        content = (m.get('content') or '').strip()
        if not content or lacked not in content:
            continue
        src = (m.get('source') or '').strip()
        added_any = False
        for seg in re.split(r'(?<=[。！？!?；;\n])', content):
            s = seg.strip()
            if not s or lacked not in s:
                continue
            s = re.sub(r'（相似度 [0-9.]+）', '', s).replace('**', '').strip()
            if not s or s in seen:
                continue
            seen.add(s)
            (tier1 if _ACQ_KW.search(s) else tier2).append(s)
            added_any = True
        if added_any and src:
            sources.add(src)
        if len(tier1) >= 8:
            break
    lines = tier1[:8] if tier1 else tier2[:8]
    if not lines:
        return None, None
    return '\n'.join(lines), sources


def _handle_resource_scarcity(tools, overrides: dict, msg: str):
    """资源不足闲聊（油耗大/经费不足/钻石不够等感叹）→ 先从攻略向量库检索该资源的获取方式，
    再综合规划建议 + 末句「最佳解决办法」钩子；钩子后用户疑问/充值意图 → 从知识库表情包回复「氪啊」。
    非资源场景返回 None（交回其他路由）。"""
    m = (msg or '').strip()
    if not m:
        return None
    # 检索 / 向量库 / 知识库 类问法（攻略检索语境）→ 不触发资源闲聊与「氪啊」，
    # 交回攻略检索等其他路由；即「除了检索向量库外」场景不误带 氪啊。
    if re.search(r'检索|向量库|知识库', m):
        overrides['resource_hint'] = False
        return None
    # 追问阶段：上一轮给过"最佳解决办法"钩子，本轮是疑问/追问/充值意图 → 直接给表情包
    #（钩子持续有效：只要用户在追问就一直给，直到用户停止疑问）
    if overrides.get('resource_hint') and (_is_sticker_followup(m) or _RECHARGE_INTENT_RE.search(m)):
        return {'reply': '最佳解决办法，充值。噢。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '', 'ok': True,
                'sticker': {'path': '图片素材/表情包/氪啊.jpg', 'title': '氪啊'}}
    # 非疑问输入（确认/告别/新话题）→ 清除钩子，交回其他路由
    if overrides.get('resource_hint'):
        overrides['resource_hint'] = False
    # 资源不足感叹识别（须是感叹/陈述，不含攻略请求词——请求词已由攻略路由优先处理）
    if not _RESOURCE_LACK_PAT.search(m):
        return None
    # 明确动作词 → 交回攻略/其他路由，不按感叹闲聊处理。
    # 例外：「怎么办 / 咋办 / 如何解决」是对"怎么解决资源不足"的追问，本身就属资源场景，
    # 不算攻略动作词（此前被 怎么 一律挡掉，导致「钻石不够了怎么办」漏触发「氪啊」钩子）；
    # 而「怎么获取 / 如何攒 / 怎么刷」这类仍是攻略请求，照旧排除。
    if re.search(r'(?:怎么|咋|如何)(?!办|解决)|攻略|推荐|求|帮|查|买|氪|充值', m):
        return None
    # 识别具体缺的资源：**支持多个**（「钻石和油不够了」→ 钻石、油都给获取方式）；
    # 按资源词在输入中出现的先后排序，输出顺序与用户说法一致；无具体词则泛称"资源"兜底。
    lacked_list = [w for w in _RESOURCE_LACK_WORDS if w in m]
    lacked_list.sort(key=lambda w: m.index(w))
    if not lacked_list:
        lacked_list = ['资源']
    plan = ('资源要提前规划才不被动——日常产出、活动奖励、限时收益都算进去，支出项排个先后，该省的省、该花的花。')
    sections = []
    for lacked in lacked_list:
        # 先从攻略向量库检索该资源的获取方式：专属纯向量检索 + 确定性抽取（见
        # _resource_acquire_from_guide 说明：绕过 GuideRAG 的 RRF+0.5 阈值误杀与 LLM 误报「未提及」）；
        # 单个资源命中不足则回落该资源的「没检索到」提示，不影响其它资源。
        guide_reply, sources = _resource_acquire_from_guide(lacked)
        if not guide_reply:
            sections.append(f'阿卡莉翻了攻略库，{lacked}获取的具体条目这次没检索到。')
            continue
        # 该资源对应的攻略来源：「问答补充」类不写来源，且**只写攻略正式名**
        #（用 KB documents 的 title，不写 md 文件名/编号，如「6.1 练级百科.md」→「练级百科」）
        real_srcs = []
        for s in sorted(sources or []):
            if '问答补充' in s:
                continue
            t = _guide_title_of(s)
            if t and t not in real_srcs:
                real_srcs.append(t)
        src_line = ('\n来源：' + ''.join(f'《{x}》' for x in real_srcs)) if real_srcs else ''
        sections.append(f'【{lacked}获取方式】\n{guide_reply}{src_line}')
    reply = ('\n\n'.join(sections) + '\n\n' + plan + '\n\n……还有一个最佳解决办法。')
    overrides['resource_hint'] = True
    return {'reply': reply, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
            'keep_source': True}


def _tool(tools, name):
    for t in tools:
        if t.name == name:
            return t
    return None


def _query_text(tools, name, kwargs):
    """统一安全调用数据查询工具，返回回复文本；异常/空结果一律返回固定诚实文案，**不返回 None**（除非工具未注册）。
    - 工具未注册（name 不在 tools）→ 返回 None，交回上层继续走其它分支（保留路由降级语义）；
    - 调用抛异常 → 返回『查询暂时出错，稍后再试。』，避免异常崩 route_work；
    - 返回 None/空串 → 返回『我暂时没查到相关的信息，指挥官。』，避免空结果漏给 LLM 编造。
    各明确工具（查角色/装备/技能条件/天赋反查/索敌求和…）统一走此兜底。"""
    tool = _tool(tools, name)
    if not tool:
        return None  # 工具未注册：交回上层继续走其它分支
    try:
        out = tool.call(kwargs or {})
    except Exception:
        return '查询暂时出错，稍后再试。'
    if out is None:
        return '我暂时没查到相关的信息，指挥官。'
    text = out if isinstance(out, str) else str(out)
    return text if text.strip() else '我暂时没查到相关的信息，指挥官。'


def _suggest_buttons():
    """构造建议按钮列表（与 web_app._extract_suggestions 同格式，避免循环导入）。"""
    return [{'tool': k, 'label': v} for v, k in DAMAGE_BUTTONS]


def _fill(mark: str) -> str:
    return '<<FILL_FORM>>' + mark


# 填空字段元信息（与 web_app.FILL_FORM_FIELDS 对应）
_FIELD_META = {
    'attacker': {'label': '攻击方', 'placeholder': '如 赤城', 'required': True},
    'attacker_buff': {'label': '攻击方buff', 'placeholder': '示例：面板2000（直接赋值）炮击+200（现有数值增加）全伤害+15%', 'required': False},
    'attacker_talent': {'label': '攻击方天赋', 'placeholder': '不填则用默认 PVP 点法', 'required': False},
    'defender': {'label': '防守方', 'placeholder': '如 南达科他', 'required': False},
    'defender_panel': {'label': '守方面板（可选）', 'placeholder': '如 血量20000 装甲100 对空200', 'required': False},
    'defender_buff': {'label': '防守方buff', 'placeholder': '示例：装甲3000（直接赋值）对空+200（现有数值增加）全伤害减免+20%', 'required': False},
    'defender_talent': {'label': '防守方天赋', 'placeholder': '不填则用默认 PVP 点法', 'required': False},
    'course': {'label': '航向', 'placeholder': '同航 / 反航 / T优势 / T劣势（默认同航）', 'required': False},
    'air': {'label': '制空', 'placeholder': '确保 / 优势 / 均势 / 劣势 / 丧失（默认均势）', 'required': False},
}


def _fill_form_list(mark: str) -> list:
    """把 <<FILL_FORM>>a=|b= 转成 [{key,label,placeholder,required,value},...]（前端格式）。"""
    inner = mark.split('<<FILL_FORM>>', 1)[-1].strip()
    result = []
    for p in inner.split('|'):
        p = p.strip()
        if not p:
            continue
        k, _, v = p.partition('=')
        k = k.strip()
        meta = _FIELD_META.get(k, {'label': k, 'placeholder': '', 'required': False})
        result.append({'key': k, 'label': meta['label'], 'placeholder': meta['placeholder'],
                       'required': meta['required'], 'value': v.strip()})
    return result


def _clean_role_name(name: str) -> str:
    """清理角色名：去「查/算/的/伤害/打」等查询前缀后缀，保留角色名本身。
    也去伤害项目后缀（如「前卫的普攻」→「前卫」）。"""
    n = re.sub(r'^(查询|查一下|查查|帮我查|帮我算|算一下|算算|计算|看看|来|查|算)', '', name)
    n = re.sub(r'(的伤害|伤害|的打|打|的|呢|吧|一下|有多少|是多少|的普攻|普攻|的通常技|通常技|通常|的必杀技|必杀技|必杀|的支援技|支援技|支援|的点燃|点燃|的总伤害|总伤害)$', '', n)
    n = n.strip(' ，,、。？?！!\t')
    return n


def _extract_roles(msg: str, app=None) -> tuple:
    """从"X打Y / X和Y / X vs Y / X对Y / X攻击Y"提取 (attacker, defender) 或 (None, None)。
    支持「查华盛顿对萨拉托加改的伤害」等带查询前缀/后缀的说法。
    app 非空时对攻防名做底层昵称归一化（口罩拉菲→前卫）。"""
    m = re.search(r'([\u4e00-\u9fa5A-Za-z0-9·()（）\-]{1,12})\s*(?:打|和|vs|VS|与|对|攻击)\s*([\u4e00-\u9fa5A-Za-z0-9·()（）\-]{1,12})', msg)
    if m:
        a, d = _clean_role_name(m.group(1)), _clean_role_name(m.group(2))
        if a and d and a != d:
            if app is not None:
                from tools.common import _normalize_ship_input
                a, d = _normalize_ship_input(a, app), _normalize_ship_input(d, app)
                if a == d:
                    return None, None
            return a, d
    return None, None


def _extract_single_attacker(msg: str, app=None) -> str:
    """从「查华盛顿的伤害」「算赤城伤害」等提取单个攻击方名（防守方留空）。
    app 非空时对角色名做底层昵称归一化。
    意图词（我想算/帮我算/普攻等）迭代剥离，剥完为空说明只是发起计算的口头语 → 返回 ''。"""
    t = msg.strip(' ，,、。？?！!\t')
    _PRE = r'^(我想算|我要算|我想计算|我要计算|帮我算|帮我查|帮忙算|帮忙查|我想|我要|想算|要算|帮我|查询|查一下|查查|算一下|算算|计算|看看|查|算|来)'
    _SUF = r'(的伤害|伤害|的普攻|普攻|的技能|技能|的必杀|必杀|的支援|支援|点燃|的dps|dps|有多少|是多少|怎么样|呢|吧|一下)$'
    for _ in range(4):
        t2 = re.sub(_PRE, '', t).strip(' ，,、。？?！!\t')
        t3 = re.sub(_SUF, '', t2, flags=re.I).strip(' ，,、。？?！!\t')
        if t3 == t:
            break
        t = t3
    t = t.strip(' ，,、。？?！!\t')
    # 剥完只剩量词/语气残渣或 dps 指代词 → 不是角色名
    if t.lower() in ('dps', '总dps', '的dps') or t in ('个', '下', '次', '看', '点', '把', '来', '总'):
        return ''
    if t and len(t) <= 12 and not any(w in t for w in ('buff', '天赋', 'Buff')):
        if app is not None:
            from tools.common import _normalize_ship_input
            t = _normalize_ship_input(t, app)
        return t
    return ''


def _get_app(tools):
    """从工具列表取 app 实例（用于昵称归一化等）。"""
    if not tools:
        return None
    for t in tools:
        if hasattr(t, 'cfg') and isinstance(t.cfg, dict) and t.cfg.get('app'):
            return t.cfg['app']
    return None


def _build_team_form(is_pvp, primary_v, time_v, fronts, backs, flagship_v,
                     bad_fields=None, error_msgs=None):
    """构建组队表单（遭遇战/PVP）回填项。
    primary_v = boss 或 防守角色；time_v = 白天时间/战斗时长·白天（字符串）。
    bad_fields：出错字段集合（boss/day_time/front1~3/back1~3/defender 等）；
    error_msgs：{字段: 错误文案}。旗舰勾选状态由 flagship_v 命中船名字段推断。
    返回可放入 encounter_form 的字段列表（含 error/error_msg/flagship 标记）。"""
    bad_fields = bad_fields or set()
    error_msgs = error_msgs or {}
    items = []
    if is_pvp:
        items.append({'key': 'defender', 'label': '防守角色', 'placeholder': '角色名',
                      'required': True, 'value': primary_v or ''})
        items.append({'key': 'day_time', 'label': '战斗时长·白天', 'placeholder': '秒（0~120，黑夜 = 120 - 白天）',
                      'required': True, 'value': (time_v if time_v != '' else '60')})
    else:
        # boss 字段带 options（拉古兹 boss 名单）→ 前端渲染为按钮选择
        items.append({'key': 'boss', 'label': 'Boss', 'placeholder': '嘉波/巴巴托斯/阿丝莫德/亚斯塔露/列拉金/沙克斯/夕茉莉/河豚王/拜蒙/雅蒙/布耶尔',
                      'required': True, 'value': primary_v or '',
                      'options': list(load_encounter_bosses().keys())})
        items.append({'key': 'day_time', 'label': '白天时间', 'placeholder': '秒（黑夜自动=150-白天）',
                      'required': True, 'value': time_v or ''})
    for i in range(3):
        label = ('角色' + str(i + 1)) if is_pvp else ('前排' + str(i + 1))
        items.append({'key': f'front{i+1}', 'label': label, 'placeholder': '角色名',
                      'required': True, 'value': fronts[i] if i < len(fronts) else ''})
    for i in range(3):
        label = ('角色' + str(i + 4)) if is_pvp else ('后排' + str(i + 1))
        items.append({'key': f'back{i+1}', 'label': label, 'placeholder': '角色名',
                      'required': True, 'value': backs[i] if i < len(backs) else ''})
    for it in items:
        if it['key'] in bad_fields:
            it['error'] = True
            it['error_msg'] = error_msgs.get(it['key'], '无法识别，请检查')
        if flagship_v and it['key'].startswith(('front', 'back')) and it['value'] == flagship_v:
            it['flagship'] = True
    return items


def _rerender_on_errors(reply_lead, errors, form_items):
    """拼装「部分无法识别」的标准返回：列出错误 + 回填表单（仅错误项标红）。"""
    lines = [reply_lead]
    for e in errors:
        lines.append(f"• {e['label']}：{e['message']}")
    return {'reply': '\n'.join(lines), 'tool_outputs': [], 'suggestions': [],
            'fill_form': [], 'encounter_form': form_items, 'guide_files': [], 'guide_lib': '', 'ok': True}


def _too_many_combatants(msg: str) -> bool:
    """检测是否超过 2 个攻防名字（计算伤害最多支持两组：攻击方 vs 防守方）。
    攻防分隔词（打/和/对/vs/与）出现 ≥2 次 → 至少 3 个名字；
    顿号/逗号分隔的名字片段 ≥3 → 不支持（仅计算伤害意图时判断）。

    注意「对」的排除：「对比」（比较语义，见 core/intents.py）里的「对」不是攻防分隔词，
    否则「对比780和太太」会被算成 2 个分隔词 → 误判多组战斗送进伤害计算。"""
    if len(re.findall(r'(?:打|和|vs|VS|与|(?<!对)对(?!比))', msg)) >= 2:
        return True
    if ('伤害' in msg or '算' in msg) and not _local_guide_topic(msg):
        parts = [p for p in re.split(r'[、，,，\s]+', msg) if p.strip()]
        if len(parts) >= 3:
            return True
    return False


def _ship_name_of(name: str, app) -> str:
    """候选串 → 库内正规舰名；查不到返回 ''。

    用于把用户问句里剥出来的碎片挡在角色查询之外：查询分支会先剥掉
    「查询/查一下/的/吗」等词，剩下的往往是整句话（「手滑误删了珍贵道具」），
    不校验就逐条查库会产出「未找到角色: …」这种垃圾回复。"""
    t = (name or '').strip()
    if not t or app is None:
        return ''
    nm = getattr(app, 'raw_name_matcher', None)
    if nm is not None:
        try:
            canon = nm.match_alias_exact(t)
        except Exception:
            canon = ''
        if canon:
            return canon
    try:
        mapped = getattr(app, '_load_ship_name_map', lambda: {})().get(t, '')
    except Exception:
        mapped = ''
    if mapped:
        return mapped
    if t in getattr(app, 'raw_storage', {}) or t in getattr(app, 'storage', {}):
        return t
    if t.endswith('改'):
        alt = t[:-1] + '·改'
        if alt in getattr(app, 'raw_storage', {}) or alt in getattr(app, 'storage', {}):
            return alt
    return ''


def _split_query_names(text: str, app) -> list:
    """拆分查询的角色名列表。
    支持顿号/逗号/空格分隔；连续无分隔的（如「赤城加贺飞龙翔鹤」）用知识库角色名
    按出现位置做包含匹配拆分。拆不出库内角色时返回空表（调用方据此放弃角色查询）。"""
    # 整串先查昵称表（口罩拉菲 → 前卫）与别名映射（雪风改 → 丹阳），
    # 避免被包含匹配拆成「拉菲」「雪风」
    if app is not None:
        nm = getattr(app, 'raw_name_matcher', None)
        if nm is not None:
            canon = nm.match_alias_exact(text.strip())
            if canon:
                return [canon]
        mapped = getattr(app, '_load_ship_name_map', lambda: {})().get(text.strip(), '')
        if mapped:
            return [mapped]
    # 整串「X改」→ 兜底匹配改造角色「X·改」（如 夕立改 → 夕立·改）
    if text.strip().endswith('改') and app is not None:
        alt = text.strip()[:-1] + '·改'
        if alt in getattr(app, 'raw_storage', {}):
            return [alt]
    parts = [p for p in re.split(r'[、，,，\s]+', text) if p]
    if len(parts) > 1:
        # 多片段必须逐个回库校验：剥词后的整句话被逗号切开就是一堆碎片
        #（「想问下，手滑误删了珍贵道具，找客服回档能恢复吗」曾产出三行「未找到角色」）。
        return [n for n in (_ship_name_of(p, app) for p in parts) if n]
    names = []
    if app is not None:
        keys = sorted([k for k in getattr(app, 'storage', {}) if k], key=len, reverse=True)
        rest = text
        guard = 0
        while rest and guard < 20:
            guard += 1
            cand = [(rest.index(k), k) for k in keys if k and k in rest]
            if not cand:
                break
            _pos, k = min(cand, key=lambda x: x[0])
            names.append(k)
            rest = rest.replace(k, '，', 1)
        if names:
            return names
    canon = _ship_name_of(text, app)
    return [canon] if canon else []


def _parse_field(msg: str, *labels) -> str:
    """解析「label：value」字段（value 为第一个空白前的内容）。返回 value 或 ''。"""
    pat = re.compile(r'(?:' + '|'.join(re.escape(l) for l in labels) + r')[：:]\s*([^\s：:]+)')
    m = pat.search(msg)
    return m.group(1).strip() if m else ''


def _extract_buff_talent(msg: str, side: str) -> tuple:
    """从「攻击方buff：XX 攻击方天赋：YY」或「防守方buff：XX 防守方天赋：YY」提取 (buff, talent)。"""
    pref = '攻击方' if side == 'attacker' else '防守方'
    buff = re.search(pref + r'buff[：:]\s*([^\s]+(?: [^\s]+)*?)(?=\s*' + pref + r'天赋|$)', msg)
    talent = re.search(pref + r'天赋[：:]\s*([^\s]+)', msg)
    b = (buff.group(1) if buff else '').strip()
    t = (talent.group(1) if talent else '').strip()
    return b, t


def _extract_course_air(msg: str) -> tuple:
    course = re.search(r'航向[：:]\s*([^\s]+)', msg)
    air = re.search(r'制空[：:]\s*([^\s]+)', msg)
    formation = re.search(r'阵型[：:]\s*([^\s]+)', msg)
    return (course.group(1).strip() if course else '',
            air.group(1).strip() if air else '',
            formation.group(1).strip() if formation else '')


# 阵型 → 生效 buff（标准键，计算时叠加到角色）——单一来源已迁至 core.constants
from core.constants import FORMATION_BUFFS


def _apply_formation(overrides: dict, st: dict, formation: str):
    """应用阵型 buff（叠加到现有 buff 上，不覆盖天赋/用户输入）；空/机动=无 buff。"""
    b = overrides.setdefault('buffs', {})
    # 先移除旧阵型的贡献（跟踪旧阵型 buff 值，只减不删，避免误删天赋/用户输入的同键 buff）
    old_fb = st.get('formation_buffs', {})
    for k, v in old_fb.items():
        b[k] = b.get(k, 0.0) - v
        if b[k] == 0.0:
            b.pop(k, None)
    # 叠加新阵型 buff
    new_fb = {}
    for k, v in FORMATION_BUFFS.get(formation, {}).items():
        b[k] = b.get(k, 0.0) + v
        new_fb[k] = v
    st['formation_buffs'] = new_fb
    st['formation'] = formation


# 面板字段表（buff 支持的属性名）——已从 common 导入
# 主属性（"面板N"直接赋值的范围）——已从 common 导入
# _ATK_PANEL_FIELDS / _DEF_PANEL_FIELDS / _PANEL_MAIN_ATK / _PANEL_MAIN_DEF 统一在 src.tools.common 定义


def _split_panel_cmds(buff_str: str, side: str) -> tuple:
    """从 buff 字符串中拆分「面板指令」，返回 (panel_set, panel_add, rest, neg_inputs)。
    规则：`面板2000` = 该方主属性（攻击：炮击/雷击/航空；防御：装甲/对空/机动）直接赋值 2000；
    `炮击2000` = 炮击值直接赋值 2000（计算模块允许面板赋值）；
    `炮击+200` = 增量 +200；`炮击-500` = 面板变化 -500（实际值<0 时在计算层报错）。
    注意：`面板-100` 这类**负数赋值**记入 neg_inputs 由调用方报错。
    其余内容（全伤害+15% 等）原样返回 rest 交 set_session_buff 处理。"""
    main_fields = _PANEL_MAIN_ATK if side == 'attacker' else _PANEL_MAIN_DEF
    fields = _ATK_PANEL_FIELDS if side == 'attacker' else _DEF_PANEL_FIELDS
    panel_set, panel_add = {}, {}
    neg_inputs = []
    # 面板N：该方主属性直接赋值（面板-N = 负数赋值 → 报错）
    for m in re.finditer(r'面板\s*(-)?\s*(\d+(?:\.\d+)?)', buff_str):
        if m.group(1):
            neg_inputs.append(m.group(0).strip())
            continue
        base = float(m.group(2))
        base = min(base, 20000.0)   # 用户输入上限 20000（用户口径 2026-09-17）
        for f in main_fields:
            panel_set[f] = base
    # 属性赋值/增量：无符号 N = 直接赋值；+N = 增量；-N = 减益增量（实际值为负时计算层报错）
    for f in fields:
        for m in re.finditer(f + r'\s*([+\-]?)(\d+(?:\.\d+)?)', buff_str):
            sign, num = m.group(1), float(m.group(2))
            num = min(num, 20000.0)   # 用户输入上限 20000
            if not sign:
                panel_set[f] = num
            elif sign == '+':
                panel_add[f] = panel_add.get(f, 0.0) + num
            else:
                panel_add[f] = panel_add.get(f, 0.0) - num
    # 剥离已识别的面板指令，剩余交 set_session_buff
    rest = re.sub(r'面板\s*-\s*\d+(?:\.\d+)?', '', buff_str)
    rest = re.sub(r'面板\s*\d+(?:\.\d+)?', '', rest)
    for f in fields:
        rest = re.sub(f + r'\s*[+\-]?\s*\d+(?:\.\d+)?', '', rest)
    rest = re.sub(r'[，,、\s]+', ' ', rest).strip()
    return panel_set, panel_add, rest, neg_inputs


def _split_custom_panel(text: str, side: str) -> tuple:
    """从角色名输入中分离「面板指令」。返回 (clean_name, panel_set, panel_add, neg_inputs)。
    支持「攻击方：名字 面板2000 炮击+200」或「名字 炮击2000」。
    例：「我的船 炮击2000 炮击+200」→ ('我的船', {炮击:2000}, {炮击:200}, [])。
    无符号 N = 面板赋值；±N = 增量（-N 为减益，实际值为负时计算层报错）。"""
    pref = '攻击方' if side == 'attacker' else '防守方'
    text = re.sub(pref + r'[：:]\s*', '', text, count=1)
    text = re.sub(r'面板[：:]\s*', '', text, count=1)
    panel_set, panel_add, rest, neg_inputs = _split_panel_cmds(text, side)
    clean = rest.strip(' ：:，,、 ')
    return clean, panel_set, panel_add, neg_inputs


def _extract_time_sec(msg: str) -> float:
    """从"60秒/60s/60"提取秒数，无则 0。"""
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:秒|s|S)?', msg)
    return float(m.group(1)) if m else 0.0


# ============ 计算流程状态机 ============

def handle_calc(tools, overrides, msg: str) -> dict:
    """计算流程状态机。返回固定回复 dict；无法处理返回 None。"""
    st = overrides.setdefault('calc_state', {'step': 0, 'attacker': '', 'enemy': ''})
    app = None
    for t in tools:
        if hasattr(t, 'cfg') and isinstance(t.cfg, dict) and t.cfg.get('app'):
            app = t.cfg['app']
            break

    def reply(text, sugg=None, form=None, tool_outputs=None, ok=True):
        ff = _fill_form_list(form) if isinstance(form, str) else (form or [])
        return {'reply': text, 'tool_outputs': tool_outputs or [], 'suggestions': sugg or [],
                'fill_form': ff, 'encounter_form': [], 'guide_files': [], 'guide_lib': '',
                'ok': ok, 'calc_active': True}

    step = st['step']

    # ---- 伤害类型选择（step>=5 后，按钮点击或直接输入）----
    if step >= 5:
        for label, tool_name in DAMAGE_BUTTONS:
            if label in msg and tool_name != 'end_round':
                # 点燃/总伤害需要时间
                if tool_name in ('calc_burn_damage', 'calc_total_damage'):
                    sec = _extract_time_sec(msg)
                    if sec <= 0:
                        st['pending_tool'] = tool_name
                        st['pending_label'] = label
                        return reply(_pick(_ASK_TIME))
                st.pop('pending_tool', None)
                return _run_damage_tool(tools, overrides, st, tool_name, label, reply, msg)
        # 纯数字时间输入（如 "150"）：继续执行上次等待时间的伤害工具（点燃/总伤害）
        if st.get('pending_tool') and re.fullmatch(r'\s*\d+(?:\.\d+)?\s*(?:秒|s|S)?\s*', msg):
            tool_name = st['pending_tool']
            label = st.get('pending_label') or '总伤害'
            st.pop('pending_tool', None)
            return _run_damage_tool(tools, overrides, st, tool_name, label, reply, msg)
        st.pop('pending_tool', None)

    # ---- step 0：初始化/双角色特例 ----
    if step == 0:
        # 超过 2 个攻防名字（如「华盛顿打俾斯麦和前卫」）→ 明确不支持
        if _too_many_combatants(msg):
            return reply('同时计算多组角色暂不支持，一次最多两组（攻击方 vs 防守方）。请分别查询。')
        a, d = _extract_roles(msg, _get_app(tools))
        if a and d:
            st['attacker'], st['enemy'] = a, d
            overrides['last_role'] = a  # 记录最近提及角色（支持后续代词指代）
            st['step'] = 2
            # 输入带了具体伤害项目（如「加贺打前卫的普攻」）→ 提示后续进行具体操作
            detail = next((label for label, tn in DAMAGE_BUTTONS
                           if tn != 'end_round' and label in msg), '')
            hint = (f"已识别攻击方 {a}、防守方 {d}。具体伤害项目（{detail}）请在后续步骤选择。\n"
                    if detail else '')
            return _show_attack_and_ask_def_buff(tools, overrides, st, reply, hint)
        # 只有一个名字（如「查华盛顿的伤害」）→ 自动只填攻击方，防守方留空（输出原始伤害）
        single = _extract_single_attacker(msg, _get_app(tools))
        if single:
            st['attacker'] = single
            st['enemy'] = ''
            st['step'] = 2
            return _show_attack_and_ask_def_buff(tools, overrides, st, reply)
        st['step'] = 1
        return reply(_pick(_ASK_ATTACKER), form=_fill('attacker='))

    # ---- step 1：等攻击方角色名（可带面板指令：炮击2000=赋值、炮击+200/-50=增量；实际值为负时计算层报错）----
    if step == 1:
        name, pset, padd, neg_panel = _split_custom_panel(msg, 'attacker')
        if neg_panel:
            return reply('面板赋值必须为正数，不支持负数赋值：' + '、'.join(neg_panel)
                         + '。请改为正数赋值（如 面板2000），或用增量写法（如 炮击-50）。',
                         form=_fill('attacker='))
        if not name or name in ('无', '没有'):
            return reply(_pick(_ASK_ATTACKER), form=_fill('attacker='))
        st['attacker'] = name
        st['step'] = 2
        hint = ''
        if pset or padd:
            # 面板指令 → 会话级覆写（赋值+增量，作用于所选已有角色）
            if pset:
                overrides.setdefault('attack_panel_set', {}).update(pset)
            if padd:
                ov_add = overrides.setdefault('attack_panel', {})
                for f, v in padd.items():
                    ov_add[f] = ov_add.get(f, 0.0) + v
            parts = [k + '=' + _fmt_num(v) + '（赋值）' for k, v in pset.items()] \
                    + [k + ('+' if v >= 0 else '') + _fmt_num(v) + '（增量）' for k, v in padd.items()]
            hint = '面板指令已记录：' + '、'.join(parts) + '。\n'
        return _show_attack_and_ask_def_buff(tools, overrides, st, reply, hint=hint)

    # ---- step 2：等攻击方 buff/天赋 ----
    if step == 2:
        buff, talent = _extract_buff_talent(msg, 'attacker')
        # 也支持通用格式 "buff：XX"
        if not buff:
            m = re.search(r'(?:buff|Buff)[：:]\s*([^\s]+(?: [^\s]+)*)', msg)
            if m:
                buff = m.group(1).strip()
        err = _apply_buff_talent(tools, overrides, st['attacker'], 'attacker', buff, talent)
        if err:
            return reply(err, form=_fill('attacker_buff=|attacker_talent='))
        if st.get('enemy'):
            # 双角色（X打Y/攻击/对）已自动填好防守方 → 跳过问防守方名字，
            # 直接显示防守方面板并进入防守方 buff 输入
            st['step'] = 4
            return _show_defender_and_ask_buff(tools, overrides, st, reply)
        st['step'] = 3
        return reply(_pick(_ASK_DEFENDER), form=_fill('defender='))

    # ---- step 3：等防守方角色名（必须选择，仅支持已有角色）----
    if step == 3:
        raw_msg = msg
        # 守方面板/防御buff 独立行（表单提交：防守方：X 面板：… 防御buff：…）：先剥离再取名字
        def_buff = ''
        mb2 = re.search(r'防御buff[：:]\s*([^；;]+)', raw_msg)
        if mb2:
            def_buff = mb2.group(1).strip()
            raw_msg = raw_msg.replace(mb2.group(0), ' ', 1)
        def_panel = ''
        mp2 = re.search(r'面板[：:]\s*([^；;]+)', raw_msg)
        if mp2:
            def_panel = mp2.group(1).strip()
            raw_msg = raw_msg.replace(mp2.group(0), ' ', 1)
        name = re.sub(r'^\s*防守方[：:]?\s*', '', raw_msg).strip()
        if not name or name in ('无', '没有', '不填', '跳过'):
            st['step'] = 3
            return reply(_pick(_ASK_DEFENDER_REQ), form=_fill('defender='))
        st['enemy'] = name
        st['step'] = 4
        if def_panel:
            # 守方面板指令 → 会话级覆写（赋值+增量，作用于所选已有防守方；增量同时合并进敌方）
            dp_set, dp_add, _dp_rest, dp_neg = _split_panel_cmds(def_panel, 'defender')
            if dp_neg:
                return reply('面板赋值必须为正数，不支持负数赋值：' + '、'.join(dp_neg)
                             + '。请改为正数赋值，或用增量写法。', form=_fill('defender=|defender_panel='))
            if dp_set:
                overrides.setdefault('defense_panel_set', {}).update(dp_set)
            if dp_add:
                ov_add = overrides.setdefault('defense_panel', {})
                for f, v in dp_add.items():
                    ov_add[f] = ov_add.get(f, 0.0) + v
        if def_buff:
            buff_err = _apply_buff_talent(tools, overrides, name, 'defender', def_buff, '')
            if buff_err:
                return reply(buff_err, form=_fill('defender=|defender_buff='))
            # 名字+防御buff 齐了 → 直接进入阵型/航向/制空（防守方 buff 步骤被覆盖）
            st['step'] = 5
            _set_calc_context(tools, overrides, st)
            return reply(f"已记住防守方「{name}」与防御buff。\n" + _pick(_ASK_COURSE_AIR),
                         form=_fill('course=|air='))
        return _show_defender_and_ask_buff(tools, overrides, st, reply)

    # ---- step 4：等防守方 buff/天赋 ----
    if step == 4:
        buff, talent = _extract_buff_talent(msg, 'defender')
        if not buff:
            m = re.search(r'(?:buff|Buff)[：:]\s*([^\s]+(?: [^\s]+)*)', msg)
            if m:
                buff = m.group(1).strip()
        err = _apply_buff_talent(tools, overrides, st['enemy'], 'defender', buff, talent)
        if err:
            return reply(err, form=_fill('defender_buff=|defender_talent='))
        _set_calc_context(tools, overrides, st)
        st['step'] = 5
        return reply(_pick(_ASK_COURSE_AIR), form=_fill('course=|air='))

    # ---- step 5：等阵型/航向/制空 ----
    if step == 5:
        course, air, formation = _extract_course_air(msg)
        if course or air or formation or ('默认' in msg):
            _apply_formation(overrides, st, formation or '')
            st['course'], st['air'] = course or '同航', air or '均势'
            _set_calc_context(tools, overrides, st, course=course or '同航', air=air or '均势')
        st['step'] = 6
        # 确认启动计算流程：弹出伤害类型菜单时固定附公式一图流（一次流程仅此一处）
        r = reply(_pick(_ASK_SKILL_SELECT), sugg=_extract_suggest_buttons())
        r['top_image'] = dict(_CALC_FORMULA_IMAGE)
        return r

    # 其他：明确计算意图（含 算/伤害）→ 重启新一轮；菜单阶段的其余输入一律弹回
    # 伤害类型菜单（计算轮排他：只有「结束本轮计算」等退出词才放行，防止流程外逃）
    if any(w in msg for w in ('算', '伤害')):
        st['step'] = 0
        a, d = _extract_roles(msg, _get_app(tools))
        if a and d:
            st['attacker'], st['enemy'] = a, d
            st['step'] = 2
            return _show_attack_and_ask_def_buff(tools, overrides, st, reply)
        return reply(_pick(_ASK_ATTACKER), form=_fill('attacker='))
    if step >= 6:
        return reply(_pick(_ASK_SKILL_SELECT), sugg=_extract_suggest_buttons())
    return None


def _extract_suggest_buttons():
    """构造建议按钮列表（与 web_app._extract_suggestions 同格式，避免循环导入）。"""
    return _suggest_buttons()


def _run_damage_tool(tools, overrides, st, tool_name, label, reply, msg):
    """执行伤害计算工具并固定输出结果（保留按钮供继续选择）。"""
    tool = _tool(tools, tool_name)
    if tool is None:
        return reply(f"工具 {tool_name} 不可用。")
    params = {}
    if tool_name in ('calc_burn_damage', 'calc_total_damage'):
        sec = _extract_time_sec(msg)
        params[tool_name == 'calc_burn_damage' and 'burn_time' or 'total_time'] = sec
    out = tool.call(params)
    overrides['last_calc_tool'] = True  # 标记：刚跑过计算工具，供下一跳识别「算错/识别不对」反馈
    out_text = out if isinstance(out, str) else str(out)
    # 总伤害：直接完整展示在回复里（不折叠、不截断）
    if tool_name == 'calc_total_damage':
        return reply(out_text, sugg=_extract_suggest_buttons())
    # 其他伤害类型：reply 只保留结果部分（"详细计算过程"之前的摘要），
    # 详细过程由 tool_outputs 折叠块承载，避免回复文本里重复粘贴
    result_part = out_text.split('详细计算过程')[0].rstrip()
    # 清理 split 残留的折叠块标题（如混合技能子项"【必杀技-炮击】"）
    result_part = re.sub(r'\s*【[^】]*】\s*$', '', result_part).rstrip()
    return reply(result_part, sugg=_extract_suggest_buttons(), tool_outputs=[out_text])


def _set_calc_context(tools, overrides, st, course='同航', air='均势'):
    tool = _tool(tools, 'set_calc_context')
    if tool:
        try:
            tool.call({'attacker_name': st['attacker'], 'enemy_name': st['enemy'] or '',
                       'course': course, 'air': air})
        except Exception:
            pass


def _apply_buff_talent(tools, overrides, ship_name, side, buff, talent) -> str:
    """应用 buff 与天赋。支持面板指令（面板N=直接赋值、属性±N=增减益增量；负数赋值报错，
    减益增量导致最终面板为负时在计算层报错）。返回错误信息或 ''。"""
    # 面板指令：直接写入会话级覆写（attack/defense_panel_set 覆盖、attack/defense_panel 增量）
    panel_set, panel_add, rest, neg_panel = _split_panel_cmds(buff or '', side)
    # 面板赋值必须为正数：负数赋值（面板-N）直接报错，不生效
    if neg_panel:
        return ('面板赋值必须为正数：' + '、'.join(neg_panel)
                + '。请改为正数赋值（如 面板2000），或用增量写法（如 炮击-50）。')
    # 记录调用前已有的 buffs 键（防守方转存只处理本次新增，避免误删攻击方已有增益）
    _before_buff_keys = set(overrides.get('buffs', {}).keys())
    key_set = 'attack_panel_set' if side == 'attacker' else 'defense_panel_set'
    key_add = 'attack_panel' if side == 'attacker' else 'defense_panel'
    if panel_set:
        overrides.setdefault(key_set, {}).update(panel_set)
    if panel_add:
        d = overrides.setdefault(key_add, {})
        for f, v in panel_add.items():
            d[f] = v
    if rest:
        tool = _tool(tools, 'set_session_buff')
        if tool:
            r = tool.call({'buff_str': rest})
            if isinstance(r, str) and '无法识别' in r:
                return r
    if side == 'attacker':
        tool = _tool(tools, 'set_session_talent')
        if tool:
            params = {'ship_name': ship_name, 'side': 'attacker'}
            if talent and talent != '无':
                params['talent_code'] = talent
            r = tool.call(params)
    else:
        # 防守方 buff：减免类字段应作用于敌方（enemy），而非攻击方。
        # set_session_buff 已把 rest 写入 buffs；这里只转存**本次新增**的键
        # （不触碰攻击方已有的增益，如 全伤害），避免误删。
        # 注意：只转存 减免/点燃抵抗 类字段，不移除任何攻击方增益。
        # 移除 '伤害增加' 字段会误删攻击方天赋的暴击伤害增加/鱼雷伤害增加等。
        before = _before_buff_keys
        b = overrides.get('buffs', {})
        en = overrides.setdefault('enemy', {})
        for k in list(b):
            # 回避率（用户口径 2026-09-14）：回避率是被攻击方属性，防守方填写时必须进
            # enemy 通道（否则会落到攻击方 buffs，既污染攻击方又不被公式读取）。
            # 注：key 已被 _normalize_buff_key 折算为「回避率增加」，由 _load_combatants 归一。
            if k not in before and ('减免' in k or '点燃抵抗' in k or k.startswith('回避率')):
                try:
                    # 直接赋值而非累积：set_session_buff 已在 buffs 中累积了正确值
                    en[k] = float(b.pop(k))
                except (ValueError, TypeError):
                    pass
        # 不再移除 '全伤害'/'攻击力'/'伤害增加' 字段，避免误删攻击方天赋增益
        tool = _tool(tools, 'set_session_talent')
        if tool:
            r = tool.call({'ship_name': ship_name, 'side': 'defender',
                           **({'talent_code': talent} if talent and talent != '无' else {})})
    return ''


def _reset_calc(overrides: dict):
    """清空本轮计算流程的全部临时状态（回到主对话）。"""
    for k in ('calc_state', 'calc_context', 'buffs', 'attack_panel', 'defense_panel',
              'attack_panel_set', 'defense_panel_set', 'ship_type_damage_bonus',
              'ship_type_damage_reduction', 'enemy', 'cd', 'talent_effects'):
        overrides.pop(k, None)


def _show_attack_and_ask_def_buff(tools, overrides, st, reply, hint=''):
    # 一律查库（攻击方必须是已有角色），未找到即标红报错
    tool = _tool(tools, 'query_ship_copy')
    if tool:
        try:
            out = tool.call({'ship_name': st['attacker']})
            text = out if isinstance(out, str) else str(out)
        except Exception as e:
            text = f"{e}"
    else:
        text = "查询工具不可用。"
    # 角色无法识别 → 原位标红报错，不重走流程（保留用户已填内容，改错处即可）
    if '未找到角色' in text:
        st['step'] = 1  # 退回角色名输入步，改正后重提会重新校验
        bad = [{'key': 'attacker', 'label': '攻击方', 'placeholder': '如 赤城', 'required': True,
                'value': st['attacker'], 'error': True,
                'error_msg': '无法识别该角色，请检查名称'}]
        return reply('攻击方「' + st['attacker'] + '」无法识别，请改正后重新提交。',
                     form=bad, ok=True)
    st['step'] = 2
    if hint:
        return reply(hint + "\n" + _pick(_ASK_BUFF_ATK) + "\n" + text,
                     form=_fill('attacker_buff=|attacker_talent='), tool_outputs=[text])
    return reply(_pick(_ASK_BUFF_ATK) + "\n" + text, form=_fill('attacker_buff=|attacker_talent='),
                 tool_outputs=[text])


def _show_defender_and_ask_buff(tools, overrides, st, reply):
    # 一律查库（防守方必须是已有角色），未找到即标红报错
    tool = _tool(tools, 'query_ship_copy')
    if tool:
        try:
            out = tool.call({'ship_name': st['enemy'], 'include_skills': False})
            text = out if isinstance(out, str) else str(out)
        except Exception as e:
            text = f"{e}"
    else:
        text = "查询工具不可用。"
    # 角色无法识别 → 原位标红报错，不重走流程（保留用户已填内容，改错处即可）
    if '未找到角色' in text:
        st['step'] = 3  # 退回守方名输入步，改正后重提会重新校验
        bad = [{'key': 'defender', 'label': '防守方', 'placeholder': '如 南达科他', 'required': False,
                'value': st['enemy'], 'error': True,
                'error_msg': '无法识别该角色，请检查名称'},
               {'key': 'defender_panel', 'label': '守方面板（可选）', 'placeholder': '如 血量20000 装甲+100 对空-50（N=赋值、±N=增量）',
                'required': False, 'value': ''},
               {'key': 'defender_buff', 'label': '防御buff（可选）', 'placeholder': '如 全伤害减免+15% 回避率+10%',
                'required': False, 'value': ''}]
        return reply('防守方「' + st['enemy'] + '」无法识别，请改正名字重新提交。', form=bad, ok=True)
    st['step'] = 4
    return reply(_pick(_ASK_BUFF_DEF) + "\n" + text, form=_fill('defender_buff=|defender_talent='),
                 tool_outputs=[text])


# ============ 工作类路由 ============

# 攻略主题识别：目录/文档内容一律后端固定输出（LLM 不生成内容）。
# 匹配顺序：①本地 query_patterns 关键词精确匹配（免费）→ ②LLM 识别模糊说法 → ③兜底全目录。
# 本地匹配直接复用知识库 JSON 的 query_patterns（37 条），与 JSON 分类完全一致，
# 不另维护手写关键词表（避免两处不一致/漏词）。
# _load_guide_kb 统一复用 src.tools.common（唯一实现，路径指向 知识库/攻略/精简版）


def _local_guide_topic(msg: str) -> str:
    """按知识库 JSON 本地匹配攻略主题（免费，优先）。返回主题名或 ''。
    匹配顺序：①topic_routing 主题名精准命中（如"遭遇战"）→ ②query_patterns 关键词。
    命中 pattern 返回其 topic 字段（可能不在 topic_routing，如 战斗机制/养成机制——
    此时 _route_guide 用用户原话交回 query_guide 内部二次匹配）。"""
    # 与 query_guide 内部一致的新人归一化（JSON pattern 无"新人"词，query_guide 有映射）
    if any(w in msg for w in ('新人', '新手', '萌新', '入坑', '刚玩', '开局')):
        return '萌新入门'
    kb = _load_guide_kb()
    # ① topic_routing 主题名优先（避免"遭遇战攻略"被宽泛的"活动|…|遭遇战" pattern 吸走）
    for topic in kb.get('topic_routing', {}):
        if topic and topic in msg:
            return topic
    # ② query_patterns 关键词：按「命中词最长优先」匹配（如"争锋竞技场"胜过宽泛"竞技场"）
    patterns = kb.get('query_patterns', [])
    msg_l = msg.lower()
    best_topic, best_len = '', 0
    for qp in patterns:
        pat = qp.get('pattern', '')
        if not pat:
            continue
        words = [w.strip().lower() for w in pat.split('|') if w.strip()]
        for w in words:
            if w and w in msg_l and len(w) > best_len:
                best_len = len(w)
                best_topic = qp.get('topic', '')
    return best_topic


def _local_guide_docs(msg: str) -> list:
    """本地命中 query_patterns 时返回对应文档 ID 列表（供确认，未命中返回空）。"""
    kb = _load_guide_kb()
    patterns = kb.get('query_patterns', [])
    msg_l = msg.lower()
    for qp in patterns:
        pat = qp.get('pattern', '')
        if not pat:
            continue
        words = [w.strip().lower() for w in pat.split('|') if w.strip()]
        if any(w and w in msg_l for w in words):
            return qp.get('docs', [])
    return []


def _classify_guide_topic(msg: str, topic_list: list) -> str:
    """本地匹配对不上时，用 LLM 辅助判断用户要什么攻略（仅识别主题，不生成内容）。
    返回主题名；识别不出/出错返回 ''（调用方回退）。"""
    if not topic_list:
        return ''
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content': '你是攻略库主题分类器。从给定主题列表中选出与用户需求最匹配的 1 个主题名，只输出主题名本身，禁止输出其他内容。都不匹配则输出"全部"。'},
                {'role': 'user', 'content': f'可用主题：{"、".join(topic_list)}\n用户需求：{msg}'},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式，否则 DashScope 返回 400
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        topic = (resp.choices[0].message.content or '').strip()
        if topic in topic_list:
            return topic
        return ''
    except Exception:
        return ''


def _classify_fuzzy_intent(msg: str) -> str:
    """模糊口语问句（"争锋是啥/啥玩意/干嘛的"等无法用正则穷尽的说法）→ LLM 意图分析。
    返回 '介绍'（玩法/主题介绍）/ '攻略'（查攻略）/ '其他'（与两者无关）。
    出错/超时返回 '其他'（交回 LLM 闲聊，不强行路由）。"""
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=20)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content': '你是拂晓游戏助手的意图分类器。只输出一个词，禁止输出其他内容：'
                                              '介绍=用户想了解某个玩法/系统/主题"是什么/怎么玩/干嘛的/介绍"；'
                                              '攻略=用户明确要攻略/建议/文档；其他=与玩法介绍、攻略都无关。'},
                {'role': 'user', 'content': msg},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式，否则 DashScope 返回 400
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        intent = (resp.choices[0].message.content or '').strip()
        return intent if intent in ('介绍', '攻略') else '其他'
    except Exception:
        return '其他'


def _llm_analyze_intent(msg: str) -> str:
    """末级 LLM 意图分析（关键字+映射+LLM 组合的最后一步）：
    规则/昵称映射都未命中时，让 LLM 只判断意图**类型**（不输出角色名——
    名字提取由系统映射层负责，避免 LLM 名字不稳定）。
    返回 '角色'|'介绍'|'攻略'|'计算'|'闲聊'。出错/超时返回 '闲聊'。"""
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=20)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content': '你是拂晓游戏助手（舰灵题材二次元海战手游）的意图分析器。'
                                              '分析用户输入的意图，只输出一个词，禁止输出其他内容：\n'
                                              '角色  用户想查某个舰灵的信息/问某个舰灵是谁\n'
                                              '介绍  用户想了解某个玩法/系统/主题是什么\n'
                                              '攻略  用户明确要攻略/建议/文档\n'
                                              '计算  用户想算伤害/数值\n'
                                              '闲聊  问候/寒暄/与拂晓无关的话题'},
                {'role': 'user', 'content': msg},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式，否则 DashScope 返回 400
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        text = (resp.choices[0].message.content or '').strip()
        if text in ('角色', '介绍', '攻略', '计算'):
            return text
        return '闲聊'
    except Exception:
        return '闲聊'


def _try_nickname_contains(msg: str) -> str:
    """从输入中提取昵称/角色名子串（含口语尾词包裹，如"口罩拉菲呢"→前卫、"花生啊"→华盛顿）。
    遍历昵称硬映射表（ship_nicknames.json），取命中的最长昵称 → 返回对应正规名；
    不依赖 app（硬映射全局生效）。无命中返回 ''。"""
    if not msg:
        return ''
    from core.name_service import build_hard_map
    hard = build_hard_map()
    best = ''
    for nick, canon in hard.items():
        if len(nick) >= 2 and nick in msg and len(nick) > len(best):
            best = canon
    return best


def _resolve_role_from_msg(msg: str, tools) -> str:
    """LLM 判「角色」意图后，由系统映射层从原输入提取角色正规名（不依赖 LLM 名字）。
    顺序：①昵称子串（含口语尾词，如"口罩拉菲呢"→前卫）→ ②单攻击方提取 → ③整句归一化。
    若均无法解析为**真实角色名**，返回 ''（调用方据此交回 LLM 闲聊，绝不把整句闲聊当角色名送查询工具）。"""
    canon = _try_nickname_contains(msg)
    if canon:
        return canon
    app = _get_app(tools)
    if app is None:
        return ''
    # ② 单攻击方提取（"查华盛顿伤害" → "华盛顿"），仅当确为真实角色才采用
    single = _extract_single_attacker(msg, app)
    if single and _is_known_ship(single, app):
        return _normalize_ship_input(single, app)
    # ③ 整句兜底：仅当整句确为真实角色名（用户就只发了一个名字）才返回，否则交回 LLM
    if _is_known_ship(msg, app):
        return _normalize_ship_input(msg, app)
    return ''



def _guide_topics() -> list:
    """读取攻略知识库主题列表（topic_routing keys），供 LLM 识别候选。"""
    return list(_load_guide_kb().get('topic_routing', {}).keys())


# 攻略分类直达词：输入分类名（含模糊词）→ 打开滑块并选中该分类，不当作攻略主题查询
_CATEGORY_DIRECT = (
    ('萌新入坑', ('萌新入坑',)),
    ('一图速查', ('一图速查', '一图',)),
    ('练级须知', ('练级须知', '练级百科')),
    ('进阶必备', ('进阶必备', '进阶攻略', '进阶玩法')),
)


def _match_category(msg: str) -> str:
    """识别用户输入是否指向攻略分类（返回分类名或空）。"""
    for cat, words in _CATEGORY_DIRECT:
        if any(w in msg for w in words):
            return cat
    return ''


def _guide_reply_from_text(text: str, keyword: str) -> dict:
    """解析 query_guide 输出文本 → route_work 返回 dict（guide_files/guide_lib/clean_reply）。"""
    # guide_lib：<<GUIDE_LIB>> 后的聚焦词，空则 '*'（前端打开全部面板）
    gl = re.search(r'<<GUIDE_LIB>>([^*]*?)$', text)
    guide_lib = (gl.group(1).strip() if gl and gl.group(1).strip() else ('*' if '<<GUIDE_LIB>>' in text else ''))
    # guide_files：解析 <<GUIDE_FILE>>path|title 标记（前端渲染窗口化目录卡片），
    # 卡片只允许注入经原文人工核验并显式标记的摘要。
    kb_summary = {}
    for d in _load_guide_kb().get('documents', []):
        if d.get('summary_verified') is True:
            kb_summary.setdefault(d.get('title', '').strip(), d.get('summary', ''))
    guide_files = []
    for m in re.finditer(r'<<GUIDE_FILE>>([^<\n]+?)\|([^<\n]+)', text):
        title = m.group(2).strip()
        sm = kb_summary.get(title, '')
        if not sm:
            for t, s in kb_summary.items():
                if t and (title in t or t in title):
                    sm = s
                    break
        guide_files.append({'path': m.group(1).strip(), 'title': title, 'summary': sm})
    # 回复文本剥离标记原文（前端已由 guide_files/guide_lib 渲染成目录卡片与面板，文本不显示标记）
    from tools.knowledge import _strip_case_refs
    clean_reply = re.sub(r'<<GUIDE_FILE>>[^<\n]+?\|[^<\n]+', '', text)
    clean_reply = re.sub(r'<<GUIDE_LIB>>[^*]*?$', '', clean_reply)
    clean_reply = _strip_case_refs(clean_reply.strip())
    # 具体主题攻略：回复保留标题列表（"攻略「XX」对应文档（N 篇）：1.《…》…"，纯标题、
    # 无简介预览、无"文档/图片"标注），详情与预览由弹窗滑块承载；
    # 滑块聚焦该主题（guide_lib=keyword），不回落全目录 '*'
    if guide_files and keyword:
        guide_lib = keyword
    return {'reply': clean_reply, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': guide_files, 'guide_lib': guide_lib,
            'ok': True}


# ======================================================================
# 剧情类问法（用户口径 2026-09-24）
# ----------------------------------------------------------------------
# 涉及剧情一律**不剧透**，按问的对象分两路：
#   ① 角色 / 好感剧情向（"光辉的剧情怎么样""谁的剧情值得看"）
#      → 给【剧情榜】（好感剧情质量 T0~T4 排行），并把攻略库里的剧情榜文档一并附上
#        （guide_lib 聚焦「剧情榜」，前端会自动弹攻略面板）。
#   ② 主线 / 活动向（"主线剧情讲了什么""活动剧情"）
#      → 建议指挥官自己体验，不给剧情内容。
# 必须是确定性拦截：先于 LLM 意图判定与 RAG 检索，避免检索到剧情相关正文造成剧透。
# ======================================================================
# 收紧（2026-09-24 复盘）：不能只凭一个词就接管整条对话 ——
#   · 去掉「羁绊」：它与「羁绊系统怎么玩」这类**玩法**问题冲突，不是剧情问法；
#   · 日程类问法（"活动剧情什么时候结束"）问的是时间，交回正常路由（见 _STORY_SCHEDULE_RE）。
_STORY_RE = re.compile(r'剧情|剧透|好感故事|个人故事|角色故事')
_STORY_SCHEDULE_RE = re.compile(
    r'什么时候(?:结束|开始|开)|几号|几时|截止|结束时间|开启时间|持续到|还有多久')
_STORY_PLOT_RE = re.compile(
    r'主线|活动|战役|关卡|第[一二三四五六七八九十百零\d]{1,4}[章话节]|'
    r'世界观|背景设定|讲了(?:什么|啥)|讲的(?:什么|啥)|说了(?:什么|啥)|内容是什么')
_STORY_DOC_KEY = '剧情榜'


def _handle_story(msg: str):
    """剧情类问法的确定性应答；非剧情问题返回 None。"""
    if not _STORY_RE.search(msg or ''):
        return None
    # 问的是日程（"活动剧情什么时候结束"）→ 不是剧情内容诉求，交回正常路由
    if _STORY_SCHEDULE_RE.search(msg):
        return None
    # ② 主线 / 活动剧情 → 引导自行体验（不给剧情内容）
    if _STORY_PLOT_RE.search(msg):
        return {'reply': '主线与活动剧情，建议指挥官亲自走一遍，我就不剧透了。\n'
                         '如果想挑一位姑娘培养感情，可以问我「剧情榜」——'
                         '那里按好感剧情质量排了 T0~T4。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '', 'ok': True}
    # ① 角色 / 好感剧情 → 剧情榜（附攻略库里的剧情榜文档，前端自动聚焦打开）
    body = ['剧情相关的问题，先看【剧情榜】：按好感剧情质量分级（T0 推荐 ~ T4 修复中）。'
            '具体情节建议指挥官自己体验，我不剧透。']
    for d in (_load_guide_kb().get('documents', []) or []):
        if _STORY_DOC_KEY in (d.get('title') or ''):
            if d.get('summary_verified') is True and d.get('summary'):
                body.append('　　%s' % d['summary'].strip())
            if d.get('file'):
                body.append('<<GUIDE_FILE>>%s|%s' % (d['file'], d.get('title') or _STORY_DOC_KEY))
            break
    return _guide_reply_from_text('\n'.join(body), _STORY_DOC_KEY)


# 攻略请求动作词：明确「要攻略/怎么/推荐/查」等请求信号，区别于感叹/陈述闲聊
#（如「最近舰队油耗很大，经费不足啊」无请求词 → 交回 LLM 闲聊，不触发攻略工具）
# 注意：主题名词（练级/配装/速查/百科 等）不算动作词，由 _is_guide_ask ③ 短词查询兜底
_GUIDE_ASK_RE = re.compile(
    r'攻略|怎么|如何|咋样|推荐|求|帮|教|想|要|查|看|有哪些|是什么|怎么办|咋办|'
    r'建议|值得|'
    r'(?:怎么|如何|求|推荐|查|要|看)[^。！？]{0,8}(?:省油|省|配|练|打|过|选|玩)'
    r'|怎么[打配练玩省]|如何[打配练玩省]|求.{0,6}攻略|有什么.{0,6}(?:办法|建议|攻略)'
)


def _extract_talent_query(msg: str) -> str:
    """从「有XX天赋的角色/哪些角色有XX天赋/谁带XX天赋」提取天赋名关键词。

    触发：消息含「天赋」且含群体反查指示词（角色/舰灵/舰娘/船/谁/哪些 等）；
    排除「查XX的天赋/XX有什么天赋」单角色查询（无群体词）与「天赋怎么点」攻略。
    返回天赋关键词（如 大型减伤/减伤/鱼雷防御）或 ''（非反查句式）。"""
    if '天赋' not in msg:
        return ''
    if not re.search(r'(角色|舰灵|舰娘|舰船|船|谁|哪些|有谁|都有谁)', msg):
        return ''
    idx = msg.find('天赋')
    prefix = msg[max(0, idx - 12):idx]  # 天赋前最多 12 字
    kw = re.sub(r'(有|带|拥有|哪些|什么|的|角色|舰灵|舰娘|舰船|船|谁|有谁|都有谁|'
                r'查一下|查询|查查|帮我查|帮我查下|查下|看看|呢|吗|啊|[？?！!。，,、\s]+)', '', prefix).strip()
    if not kw:
        # 兜底：XX天赋的角色（前缀无关键字时直接从「天赋」前取词）
        m = re.search(r'([^，。！？\s]{1,10}?)天赋', msg)
        if m:
            kw = re.sub(r'(有|带|拥有|哪些|什么|的|角色|舰灵|舰娘|舰船|船|谁|有谁|都有谁)', '', m.group(1)).strip()
    return kw


def _is_guide_ask(msg: str) -> bool:
    """判断输入是否为「明确攻略请求」（区别于感叹/陈述闲聊）。
    规则：①含攻略动作词（怎么/如何/攻略/推荐/求/帮/教/想/要/查/看/怎么办/建议 等）→ 是；
    ②纯陈述感叹（最近XX很大/不足/好难/吐槽，无请求词）→ 否（交回 LLM 闲聊）；
    ③主题词裸词（如「油耗」「钻石」「嘉波」等 query_patterns 词）→ 是（短词查询攻略）。
    返回 True 才允许进入攻略路由，避免「最近舰队油耗很大啊」这类闲聊被误路由成攻略。"""
    if not msg or not msg.strip():
        return False
    m = msg.strip()
    # ① 含明确攻略动作词 → 攻略请求
    if _GUIDE_ASK_RE.search(m):
        return True
    # ② 陈述/感叹句式：情绪词 + 无请求词 → 闲聊（如 最近油耗很大啊/经费不足/好难/太贵）
    if re.search(r'(?:最近|现在|如今|老是|总是|一直|天天)[^。！？]{0,10}(?:很大|太大|不足|不够|好难|太难|太贵|真难|烦|头疼|缺|涨了|贵了|难打|过不去|打不过)'
                 r'|[^。！？]{0,12}(?:啊|呀|唉|哎|哦|吧|嘛|呢)[^。！？]{0,6}(?:很大|不足|不够|好难|太难|太贵|缺)'
                 r'|(?:太大|太多|不够|不足|好难|太难|太贵|真贵|缺油|油耗大|不足|打不过|过不去)[。！？!]?$', m):
        return False
    # ③ 短词/纯主题查询：去语气尾词后 ≤6 字且命中主题词 → 视为攻略查询；
    # 含感叹情绪词的陈述（最近油耗好大啊/练级好累啊）不算查询
    s2 = re.sub(r'[啊呀唉哎哦吧嘛呢哈咯嘿]+$', '', m)
    if re.search(r'很大|太大|好大|好累|好难|太难|太贵|真贵|太非|不足|不够|缺油|油耗大|贵|穷|累|难|烦|头疼|涨了|打不过|过不去|抽光|花光|快.{0,2}(完|没|花|用)', s2):
        return False
    if len(s2) <= 6 and (_local_guide_topic(s2) or any(w in s2 for w in _GUIDE_WORDS)):
        return True
    return False


# ---- 具体攻略问题 → 向量检索作答（攻略QA库 RAG） ----

# 疑问/求答句式（「选择/比较/评估」类词统一取自 core.intents 的唯一词表，勿再写裸字面量）
_GUIDE_QA_RE = re.compile(
    r'怎么|如何|咋|什么|哪|多少|几[个层点只艘]|建议|'
    + intents.CHOICE_RE_SRC +
    r'|为什么|需要|能不能|可不可以|好不好|还是|谁|求'
)


def _llm_is_calc_request(msg: str) -> bool:
    """弱关键词（孤立的"算"字）命中后，LLM 二次确认是否真的在要求计算类操作。
    防止口语（"算了我信你吧""不算了"）被硬路由误触发计算流程。
    LLM 失败/超时按 False 处理（交回闲聊，宁可少算不可误触发）。"""
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=10)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                    '判断用户消息是否在要求执行计算类操作（伤害计算/好感度计算/数值计算/模拟等）。'
                    '日常口语中出现的"算"（如"算了""算了吧""不算了"）不算要求计算。只回答 是 或 否。'},
                {'role': 'user', 'content': msg},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        return (resp.choices[0].message.content or '').strip().startswith('是')
    except Exception:
        return False


# LLM 意图分类的合法取值（route_work 各分支按此门控，正则仅作分类失败兜底）
# scout/exp 为计算工具意图（用户口径 2026-09-15：工具调用先经 LLM 识别意图，不再靠正则枚举口语），
# 识别到即路由到对应工具——防止被 calc 门控抢走、把「下索敌」当攻击方舰名解析。
# compare/evaluate 为「选择/比较/评估」意图（用户口径 2026-09-17，见 core/intents.py）：
# 识别到即走确定性对比处理器——防止「对比780和太太」被末级「昵称取最长」捞成单舰面板。
_LLM_INTENTS = ('calc', 'formula', 'favor', 'equip', 'guide', 'theme_intro',
                'encounter', 'pvp', 'query', 'chat', 'capability', 'scout', 'exp',
                'compare', 'evaluate', 'subjective')

# 主观"比较"兜底词（用户口径 2026-09-28：主观比较以 LLM 意图 subjective 为准；
# 仅在 **LLM 分类不可用**（_li is None）时用这张小表兜底，防闲聊链路幻觉编造角色名）。
_SUBJ_FALLBACK_RE = re.compile(
    r'颜值|胸|欧派|美貌|最好看|最漂亮|最美|最可爱|最帅|漂亮|好看|'
    r'人气|投票|最强|最厉害|最猛|最棒|最喜欢|最好用|最好')


def _llm_intent_classify(msg: str, recent=None):
    """所有用户输入的第一步：LLM 意图分析（用户要求正则降级为兜底，防死板误路由）。
    recent: 最近几条用户输入（上下文继承——指代/追问按上文判意图，如筛选完说「紫的」仍归 query）。
    装备意图额外返回 equip_action（advice=选择/搭配建议，catalog=查看装备数据/表）。
    失败/超时/输出非法 → None（调用方 _li_is 全放行走原正则）。"""
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=10)
        _user = msg
        if recent:
            _hist = '\n'.join('· ' + str(x) for x in list(recent)[-4:])
            _user = ('【最近几轮用户输入（仅供上下文/指代/追问判断，不要回答它们）】\n%s\n'
                     '【本轮用户输入】\n%s' % (_hist, msg))
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                    '你是拂晓（舰灵海战手游）秘书舰助手的意图分析器。分析用户消息属于哪个意图，'
                    '只输出一行 JSON，禁止输出其他内容：'
                    '{"intent":"...","equip_action":"advice|catalog|"}\n'
                    'intent 取值：\n'
                    'calc=要求计算伤害/数值（算伤害/算dps/算总伤害/算X打Y/已发起计算）；'
                    '注意索敌、经验值不是 calc，见下\n'
                    'scout=索敌计算工具（算索敌/算下索敌/算一下索敌/计算索敌/查索敌/索敌线/队伍最高索敌）\n'
                    'exp=经验值计算工具（算经验值/算下经验/经验值计算/升级要多少经验）\n'
                    'formula=询问伤害计算公式或机制原理（怎么算的/公式是什么），不要求算具体数值\n'
                    'favor=好感度升级需求计算（还差多少出击/天数/礼物/答题）\n'
                    'equip=装备与配装咨询。intent=equip 时必须继续判断 equip_action：\n'
                    '  advice=用户要选择、推荐、搭配或纠正装备建议，例如“SSR装备自选二怎么选”'
                    '“自选二装备ssr怎么选”“指装备自选，不是角色自选”“某舰灵怎么配装”；\n'
                    '  catalog=用户只想查看装备数据、装备列表或装备速览表，例如“查装备”'
                    '“装备有哪些”“打开装备表”；\n'
                    'guide=玩法攻略、选择建议和获取方式求助（怎么打/怎么玩/怎么练/阵容推荐/'
                    '怎么获得舰灵/求攻略，含"XX攻略""boss怎么打"）；\n'
                    'theme_intro=询问玩法或系统是什么、求介绍（XX是什么玩法/XX介绍）\n'
                    'encounter=遭遇战模拟（6人打拉古兹boss，含遭遇战模拟参数）\n'
                    'pvp=模拟组队（6人打防守角色）\n'
                    'query=查询舰灵数据、筛选或实时资料（查面板/哪些舰灵有XX/属性求和/天赋反查/'
                    '看看某舰灵/按舰种或阵营或稀有度筛选角色/活动/天气/时间）；\n'
                    'compare=两个及以上舰灵横向比较（对比A和B/比较A与B/A和B哪个好/A还是B/谁更强/两个哪个值得）\n'
                    'evaluate=培养优先级或值不值得练（值得培养吗/先练哪个/哪些值得优先练/改造优先级/推荐练谁）\n'
                    'subjective=主观评价/比较，无客观数据可量化（颜值/好看/漂亮/最美/最帅/最可爱/人气/'
                    '投票/最强/最厉害/最棒这类"谁更好看/谁最强"，且未引用攻略榜单/数据表/具体数值）；\n'
                    '注意：compare/evaluate 的**舰灵名由系统映射层提取**，你只判意图；'
                    '若句中明确要求「算伤害/算数值/dps」，那是 calc 而不是 compare/evaluate\n'
                    'capability=询问助手阿卡莉本身的功能与用法（能做什么/能输入什么/我能输入啥/有哪些功能/能干啥/怎么用/帮助），问的是助手能力而不是游戏内容\n'
                    'chat=日常闲聊/调情/无关话题/无法归类\n'
                    '若【本轮用户输入】是**指代或追问碎片**（如「紫的」「她的技能」「那怎么打」），'
                    '请结合【最近几轮用户输入】判断它延续的是哪个意图（延续上文则归同一 intent）。'},
                {'role': 'user', 'content': _user},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        text = (resp.choices[0].message.content or '').strip()
        m = re.search(r'\{[^}]*\}', text)
        if not m:
            return None
        data = json.loads(m.group(0))
        intent = data.get('intent')
        if intent not in _LLM_INTENTS:
            return None
        out = {'intent': intent}
        if intent == 'equip':
            if data.get('equip_action') not in ('advice', 'catalog'):
                return None
            out['equip_action'] = data['equip_action']
        return out
    except Exception:
        return None


def _llm_guide_intent(msg: str):
    """攻略咨询入口：LLM 先理解用户意图与玩家身份，再决定路由（主题介绍/面板/检索）。
    返回 {'kind': 'newbie'|'docs'|'qa', 'identity': '萌新'|'普通'|'老手', 'topic': 主题名}
    kind: newbie=萌新向笼统问法（怎么玩/怎么开始/求带，硬检索分数不稳）；
          docs=明确要攻略文档/清单/大全；qa=针对具体玩法/机制的问题（值得检索原文回答）。
    LLM 失败/输出非法 → None（调用方走正则快速判定）。"""
    topics = _guide_topics()
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=15)
        sys_p = (
            '你是拂晓（舰灵海战手游）攻略助手的意图分析器。先理解用户意图与玩家身份，'
            '只输出一行 JSON，禁止输出其他内容：\n'
            '{"kind":"newbie|docs|qa","identity":"萌新|普通|老手","topic":"主题名或空"}\n'
            'kind 判定：newbie=完全笼统的新手/入门问法（如"这游戏怎么玩""前期怎么发展""啥都不懂求带"，'
            '说不出具体玩法或对象）；只要问题指向了某个具体玩法/机制/对象（哪怕顺带提到前期/新手），一律算 qa，'
            '如"前期在哪里练级好"（练级）、"怎么配装"（装备）；'
            'docs=明确要攻略文档/清单/合集/大全/目录（如"看下攻略大全""有什么攻略"），仅限这类字眼。\n'
            'identity 按提问口吻判断玩家身份。\n'
            'topic 从以下主题中选与需求最匹配的一个，都不匹配输出空字符串：'
            + ('、'.join(topics) if topics else '（无）')
        )
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content': sys_p},
                {'role': 'user', 'content': msg},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式，否则 DashScope 返回 400
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        data = json.loads((resp.choices[0].message.content or '').strip())
        kind = data.get('kind')
        if kind not in ('newbie', 'docs', 'qa'):
            return None
        identity = data.get('identity') or '普通'
        if identity not in ('萌新', '普通', '老手'):
            identity = '普通'
        topic = data.get('topic') or ''
        if topic and topics and topic not in topics:
            topic = ''
        return {'kind': kind, 'identity': identity, 'topic': topic}
    except Exception:
        return None


_ACQUIRE_ACTION_RE = re.compile(r'怎么抽|如何抽|怎么获取|如何获取|怎么获得|如何获得|怎么入手|如何入手')
_ACQUIRE_EVIDENCE_RE = re.compile(r'抽|调遣|卡池|建造|春信启程|UP|获取|获得|入手|持续', re.I)
_CULTIVATE_DETAIL_RE = re.compile(r'升星|平铺|炼油厂|产油|技能|培养|养成|强化|等级')


def _focus_guide_answer(answer: str, msg: str) -> str:
    """按用户明确询问的动作收口，防止同一召回块里的相邻信息答非所问。"""
    text = (answer or '').strip()
    if not text:
        return text
    if _ACQUIRE_ACTION_RE.search(msg or ''):
        units = re.split(r'(?<=。)|(?<=！)|(?<=？)|\n+', text)
        kept = []
        for unit in units:
            s = unit.strip()
            if (not s or not _ACQUIRE_EVIDENCE_RE.search(s) or _CULTIVATE_DETAIL_RE.search(s)
                    or re.search(r'攻略(?:库|中)?(?:未|没有)(?:提及|说明)|未提及|没有提及', s)):
                continue
            kept.append(s)
        return '\n'.join(kept).strip() or text
    if re.search(r'怎么培养|如何培养|怎么养|如何养', msg or ''):
        lines = [x for x in text.splitlines()
                 if not re.search(r'最勤劳|最热爱工作|为啥叫|作者|润色|'
                                  r'新手阶段|后期(?:目标|阶段)|先确保.*再|再考虑|最终目标|长期目标', x)]
        return '\n'.join(lines).strip()
    if (re.search(r'哪个更|谁更|哪种更|孰优|高下|比较', msg or '')
            and re.search(r'无法(?:确认|判断)|未.{0,60}(?:比较|对比|排序|定性)', text)
            and not text.startswith('具体场景具体分析')):
        return '具体场景具体分析。' + text
    return text


def _verify_guide_answer(client, cfg, question: str, evidence: str, draft: str) -> str:
    """Qwen 证据校对：修正遗漏子问题、数字矛盾和片段外外推。

    校对失败时保留初稿，不影响主链可用性。
    """
    try:
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                    '你是攻略回答的证据校对器。只能使用给定证据，不得使用常识补全。'
                    '先把用户问题拆成所有独立子问题，再检查初稿：'
                    '1. 每个有证据的子问题都已回答；'
                    '2. 每个数字、时间点、次数、公式、条件和结论都在证据中明写；'
                    '3. 结论与列举数量不矛盾，不自行重新计算时间轴；'
                    '4. “暂时不管”不得外推成“无封禁风险”，“可以”不得改成“必须”；'
                    '“保证/需要”不得改成“是为了/专门为了”这类目的或因果结论；'
                    '用户用“是不是为了/专门为了”求证时，证据只写必要条件（如“要保证X”）的，'
                    '不得以“是”确认该目的或因果；'
                    '涉及脚本安全时，只能陈述当前处理态度、外挂属性和原文警告；'
                    '除非证据明写，不得判断某个关卡挂脚本安全或属于安全范围；'
                    '5. 问题含“并且/并有/同时/且”时，每个列出的对象必须在同一条明确记录中满足全部条件；'
                    '6. 表格问题必须先锁定表头，再逐行对应；不得跨表、跨行拼接装备名、数值和提升率；'
                    '数字量词必须与问题一致：架、门、艘、次不能互换，用户问2架时不得回答2门；'
                    '用户明确问紫装时，只能采用证据中标为紫装或+6紫装的记录，不得改答+10装备；'
                    '7. 用户限定紫场/金场、PVE/PVP、某章节或某模式时，不得借用其他场景的数值；'
                    '8. 证据含对应、映射、归类或条件分支结构（如「A对应X、B对应Y」）时，'
                    '初稿只抽了其中一个分支当全部结论、省略其他分支的，按证据补齐完整对应关系；'
                    '9. 校对只能删错、补齐证据明写的答案或忠实改写，不得新增“固定、显著、安全、属于、仅、必须”等限定；'
                    '10. 最终答案不得出现书名号、来源篇名，以及“证据/片段/原文/知识库”等元话术。'
                    '只输出 JSON：'
                    '{"corrected_answer":"校对后的最终回答","issues":["修正项"]}'},
                {'role': 'user', 'content':
                    f'用户问题：{question}\n\n证据：\n{evidence[:14000]}\n\n初稿：\n{draft[:5000]}'},
            ],
            temperature=0.0,
            extra_body=cfg['generate_cfg']['extra_body'],
        )
        raw = (resp.choices[0].message.content or '').strip()
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return draft
        corrected = str((json.loads(m.group(0)) or {}).get('corrected_answer') or '').strip()
        return corrected or draft
    except Exception:
        return draft


def _clean_guide_meta(text: str) -> str:
    """清掉模型偶发输出的检索过程话术，保留面向用户的事实结论。"""
    # 相似度标注只用于检索排障（开发者看接口原始返回 tool_outputs 即可），
    # 不得出现在用户可见回答里（用户口径 2026-09-24）。
    text = re.sub(r'[（(]\s*相似度\s*[\d.]+\s*[）)]', '', text or '')
    text = re.sub(r'[（(][^）)\n]*(?:证据|片段|原文|知识库|表格中)[^）)\n]*[）)]', '', text or '')
    text = re.sub(r'(?:证据|片段|原文|知识库)(?:中)?(?:明确)?(?:明写|写明|指出|显示|给出)[：，]?', '', text or '')
    text = re.sub(r'(?:证据|片段|原文|知识库|攻略)(?:中)?仅', '', text)
    text = re.sub(r'(?:攻略|知识库|片段|原文)(?:中)?(?:未|没有)(?:明确)?(?:声明|提及|说明|写明)',
                  '现有信息无法确认', text)
    text = re.sub(r'证据中未(?:提供|给出)', '现有信息没有给出', text)
    text = re.sub(r'(?:攻略)?片段中未(?:提供|给出)', '现有信息没有给出', text)
    text = text.replace('证据中', '现有信息中').replace('证据未', '现有信息未')
    text = re.sub(r'^无法判断(.+?)(哪个|谁)更', r'具体场景具体分析。现有信息不足以判断\1\2更', text)
    text = re.sub(r'未说明([^，。；\n]+)', r'无法确认\1', text)
    text = re.sub(r'[，；]?亦?未给出[^。；\n]+', '', text)
    text = re.sub(r'《[^》]+》(?:·[^：，；。\n]+)?', '', text)
    text = re.sub(r'(?m)^\s*[：，；]\s*', '', text)
    return text.strip()


def _filter_conjunctive_entities(question: str, evidence: str, answer: str) -> str:
    """过滤只满足部分条件的相邻表格项，仅用于明确的多条件对象枚举题。"""
    if not (re.search(r'哪些|什么(?:角色|舰灵)|谁', question or '')
            and re.search(r'并且|并有|同时|且有', question or '')):
        return answer
    vocab = ('高伤', '鱼雷', '支援技', '必杀技', '通常技', '重甲', '中甲', '轻甲',
             '范围', '单体', '自奶', '护盾', '减伤', '索敌', '暴击', '点燃')
    required = [term for term in vocab if term in question]
    if len(required) < 2:
        return answer
    source_lines = [line.strip() for line in (evidence or '').splitlines() if line.strip()]
    kept = []
    for line in (answer or '').splitlines():
        stripped = re.sub(r'^\s*[-*•·\d.、]+\s*', '', line).strip()
        name = re.split(r'[：:，,；;（(]', stripped, maxsplit=1)[0].strip()
        if not name or len(name) > 24:
            kept.append(line)
            continue
        matching = [src for src in source_lines if name in src]
        if matching and any(all(term in src for term in required) for src in matching):
            kept.append(line)
    return '\n'.join(kept).strip() or answer


def _guide_qa_uncached(tools, msg: str, identity: str = '普通', search_query: str = '', history=None):
    """具体攻略问题：向量检索攻略片段 → LLM 依据片段作答（标注来源）。
    检索无命中/低分或 LLM 失败返回 None（调用方回落攻略面板路径）。

    ⚠️ 本函数是**真实计算**入口；对外请调用 `_handle_guide_qa`（带命中缓存）。"""
    tool = _tool(tools, 'guide_rag')
    if tool is None:
        return None
    # 多轮会话上下文（用户口径 2026-10-07）：在检索与作答之前注入最近几轮对话，
    # 使检索改写与最终作答 LLM 能结合上下文理解省略/指代/追问（如「重甲呢」「那中甲呢」）。
    _ctx = ''
    try:
        from tools.knowledge import _context_block_from_history
        _ctx = _context_block_from_history(history) if history else ''
    except Exception:
        _ctx = ''
    # 主题词增强检索：本地主题命中（如 PVP→争锋竞技场）时拼进 query，
    # 避免向量检索被字面相近的异类文档吸走（"pvp阵容"曾被 PVE 配队文档压过）
    # 优先使用意图路由器产出的检索参数（它应保留用户原话的实体和动作）。
    # 原始 msg 仍用于最终回答，避免改写后的 query 改变用户问题。
    q = (search_query or msg).strip()
    try:
        from tools.knowledge import match_guide_question
        _exact_question = bool(match_guide_question(q))
        _t = _local_guide_topic(msg)
        # “新人/萌新”只是身份修饰。对“新人怎么抽时雨”这类具体实体问题
        # 追加“萌新入门”会把检索重心拉向整篇新手总览，因此不做该宽泛增强。
        if not _exact_question and _t and _t != '萌新入门' and _t not in q:
            q = f'{q} {_t}'
    except Exception:
        pass
    out = tool.call({'query': q, 'context': _ctx})
    text = out if isinstance(out, str) else str(out)
    # 明确询问时间轴中的角色切换点时，从召回原文确定性抽取，避免并发下生成模型
    # 偶发漏掉首个或末个时间点。这里只负责组织已命中的时间轴，不新增独立路由。
    if '切换时间点' in (msg or ''):
        grouped = {}
        for line in text.splitlines():
            hit = re.match(r'^\s*(\d{2,3}S)\s*(.+)$', line.strip(), re.I)
            if not hit or '切换' not in hit.group(2):
                continue
            actor = next((name for name in ('赤城', '苍龙')
                          if name in (msg or '') and name in hit.group(2)
                          and hit.group(2).find(name) < hit.group(2).find('切换')), '')
            if actor:
                grouped.setdefault(actor, []).append(hit.group(1).upper())
        if grouped and all(grouped.get(name) for name in ('赤城', '苍龙') if name in (msg or '')):
            reply = '\n'.join('- %s切换时间点：%s' % (name, '、'.join(grouped[name]))
                              for name in ('赤城', '苍龙') if grouped.get(name))
            return {'reply': reply, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'from_kb': True, 'ok': True}
    # 原文里的公式若只以图片存在，文本抽取会留下“公式如下/其中”但没有等式。
    # 这种证据不能交给生成模型补全，否则它会凭训练记忆拼出一套看似完整的公式。
    if (re.search(r'伤害.*(?:怎么计算|怎么算|公式)', msg or '')
            and re.search(r'伤害公式[\s\S]{0,60}(?:如下|得出|其中)', text)
            and not re.search(r'[=＝]', text)):
        fluctuation = re.search(r'最终伤害[^。\n]*[。]?', text)
        reply = '当前文本没有保留伤害公式本体。'
        if fluctuation:
            reply += fluctuation.group(0).strip()
        return {'reply': reply, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    # 命中不足（最高分够不到输出线）→ 不硬答，提示自行查阅并打开攻略目录面板
    if text.lstrip().startswith('命中不足：'):
        return {'reply': '这个问题阿卡莉也没能从攻略库里找到足够相关的内容，回复不太清楚。\n\n'
                         '建议指挥官自行查阅相关攻略（已为你打开攻略目录）。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '*', 'from_kb': True, 'ok': True}
    # 检索无命中/未建库/向量化失败 → 一律回落攻略面板路径，不把错误文本交给 LLM。
    # 注意：这三类原先合并成同一个 return None，排障时**看不到真实原因**——
    # 「遭遇战阿斯莫德配装配队有吗」当时只看到配装分支的固定话术，无法判断是
    # "检索低分"还是"向量化失败"。此处补一条控制台日志（不改用户可见行为）。
    # 只检查工具错误前缀，不能在整段召回原文中搜「未找到/失败」。
    # 攻略正文本身常包含这些词（如「尚未找到具体数值」、「防止脚本过多找不到」），
    # 旧写法会把 0.81 高相似度的有效命中误判成检索失败。
    _tool_error = text.lstrip().startswith((
        '攻略问答库未构建', '查询向量化失败', '知识库未找到', '未找到与',
    ))
    if _tool_error:
        print('[guide_qa] 检索未命中，回落面板路径 | query=%r | 工具返回=%s'
              % (msg, text.strip().replace('\n', ' ')[:120]), flush=True)
        return None
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=30)
        # 身份口吻：萌新补术语解释与上手建议，老手直接给重点（数值/条目仍须摘抄原文）
        _tone = {
            '萌新': '提问的是新手玩家：解释用到的术语，适当补充上手建议。',
            '老手': '提问的是老玩家：直接给重点和结论，跳过基础科普。',
        }.get(identity, '')
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                    '你是拂晓（舰灵海战手游）的攻略助手。只依据给定的攻略片段回答用户问题。\n'
                    '【严格依据片段】片段里没有的信息一律不得写入回答；禁止以'
                    '「同类手游通用机制」「常规设计」「一般来说」「通常」「推测」「应该」「可能」'
                    '「建议每日查看」等方式补充或推断片段外内容；片段未涉及的点直接不写。'
                    '也不要输出「攻略库中未提及」这类元信息话术。\n'
                    '【禁止反问推销】回答中禁止出现任何反问或引导性提问'
                    '（如「需要我帮你……吗」「要不要我……」「接下来要不要……」），'
                    '尤其不得承诺或提及片段与本系统都不存在的功能（如「7天规划」「帮你规划顺序」）；'
                    '只输出本轮答案本身。\n'
                    '先在内部把用户问题拆成所有独立子问题，确保每个能从片段回答的点都有对应答复；'
                    '不要在输出中展示这个内部步骤。'
                    '回答简洁分点，关键结论（数值/优先级/条目）必须直接摘抄片段原文表述，'
                    '不得改动数字与条目，'
                    '不得根据 CD 自行生成新的时间点，不得根据样例反推新公式，'
                    '不得把“暂时不管”改写成“无封禁风险”等更强结论。'
                    '用户询问脚本安全时，只陈述当前处理态度、外挂属性和原文警告，'
                    '不得自行认定某个关卡挂脚本安全或属于安全范围。'
                    '问题含“并且/并有/同时/且”时，每个列出的对象必须由同一条明确记录完整满足全部条件；'
                    '表格题必须先识别用户指定的表头和指标，再逐行抄取对应单元格，'
                    '禁止把另一张表或另一行的装备名、伤害值、提升率拼到一起。'
                    '数字量词必须与用户问题一致，架、门、艘、次不得互换。'
                    '用户明确问紫装时，只能回答标为紫装或+6紫装的记录，不得用+10装备替代。'
                    '用户限定紫场/金场、PVE/PVP或某个玩法时，只能使用同一场景的结论和数值；'
                    '用户明确询问某一编号、型号或对象时，只回答该项，不连带罗列片段中的其他项；'
                    '用户问“怎么抽/怎么获取”时只答获取方式和对应条件，不展开升星、养成或用途；'
                    '用户问“怎么培养”时，若不同片段给出不同目标，只能分别转述原文；'
                    '原文没有明写“新手/后期/先/再”时，禁止自行添加阶段、先后顺序或时间线。'
                    '用户问「如何替换/怎么换」时，只回答片段明写的替换关系，'
                    '不自行展开时间轴或推导未写明的操作步骤。'
                    '用户问“哪个更好/更难/更省”等比较结论时，只有片段明确比较、排序或下结论才可判高下；'
                    '若片段只是分别介绍两者，回答“具体场景具体分析”，再简述片段明确写出的各自特点，'
                    '不得自行选出胜者，也不要改答其他相邻话题。'
                    '证据中存在对应、映射、归类或条件分支结构（如「A对应X、B对应Y」「A时用X、B时用Y」）时，'
                    '回答必须呈现与问题相关的完整对应关系或分支，逐项说明各自的适用条件或归属；'
                    '不得只抽取最贴合问题的那一句当作全部结论，也不得省略证据已给出的其他分支。'
                    '用户用“是不是为了/专门为了/是不是因为”等问法求证目的或因果时，'
                    '只有片段明写该目的或因果关系才可回答“是”；'
                    '片段若只写“要保证X”“需要满足X”这类必要条件（如“轰炸航母队保证列克星敦改的技能多释放”），'
                    '只能转述该条件本身，不得回答“是”，也不得把必要条件改写成目的或因果关系。'
                    '用户只问某个领取位置、数量、时间点或单一原因时，先直接回答该项；'
                    '除非是理解答案所必需，不得追加片段里的养成、强化、资源或其他邻近建议。'
                    '时间轴问题只能提取原文明确标注的动作和执行者；'
                    '「放大/开大」不等于「秒大」，不得把相邻角色名自行当作执行者。'
                    '结合装备语境理解口语编号，例如“自选二/自选2”指二型装备自选，'
                    '“自选三/自选3”指三型装备自选。'
                    + (_tone or '') +
                    '不要在回复里标注来源篇目（如「来源：《XX》《YY》」），也不要输出任何来源书名列表；'
                    '不得用“证据明写”“片段中”“原文指出”“知识库显示”等元话术。'},
                {'role': 'user', 'content': (f'用户问题：{msg}\n\n检索到的攻略片段：\n{text}'
                                            + ('\n\n【最近几轮对话（仅供理解指代/省略/追问，不要据此编造攻略库外内容）】\n' + _ctx
                                               if _ctx else ''))},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        answer = (resp.choices[0].message.content or '').strip()
        if answer:
            answer = _verify_guide_answer(client, cfg, msg, text, answer)
    except Exception:
        # 生成服务不可用时直接返回 top-1 召回原文，保证有据。
        body = re.sub(r'\n*【来源】[^\n]*\s*$', '', text).strip()
        first = re.split(r'\n\n+(?=【《)', body, maxsplit=1)[0]
        first = re.sub(r'^【[^\n]+】（相似度\s*[\d.]+）\s*\n?', '', first).strip()
        answer = first
    answer = _filter_conjunctive_entities(msg, text, answer)
    answer = _clean_guide_meta(answer)
    answer = _focus_guide_answer(answer, msg)
    if not answer:
        return None
    return {'reply': answer, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'from_kb': True, 'ok': True}


def _context_cache_tag(history):
    """会话上下文的稳定短标签，用于把『同一问句 + 不同上下文』区分进不同缓存键，避免跨上下文串答。"""
    if not history:
        return ''
    try:
        from tools.knowledge import _context_block_from_history
        return 'ctx:' + str(hash(_context_block_from_history(history)))
    except Exception:
        return ''


# ======================================================================
# 知识库问答**命中缓存**（用户口径 2026-10-03）：同一问题重复问时，
# 「向量检索 + 依据片段作答 + 证据校对」不再重算 —— 三个入参（问题 / 玩家身份 /
# 实际检索 query）加知识库版本就是本函数的全部输入，故命中即等价重算。
# 只缓存这一类 from_kb 的「知识库原文直出」答复；闲聊 / 调情 / 计算 / 查询 /
# 表单一律不缓存（它们依赖会话上下文与人设，缓存会串味）。命中时照常记账
# （core.kb_cache.charge：按首算真实消耗复现），额度口径不受影响。
# 开关：AKALI_KB_CACHE=0 关闭；AKALI_KB_CACHE_CHARGE=0 命中不记账。
# ======================================================================
def _handle_guide_qa(tools, msg: str, identity: str = '普通', search_query: str = '', history=None):
    """知识库问答（对外入口）：命中缓存直接返回，未命中才算并回填缓存。"""
    try:
        from core import kb_cache as _kbc
    except Exception:
        return _guide_qa_uncached(tools, msg, identity, search_query, history)
    _key = ''
    _base = None
    try:
        if _kbc.enabled():
            _key = _kbc.key(msg, identity, (search_query or msg) + ('|' + _context_cache_tag(history) if history else ''))
            _hit = _kbc.get(_key)
            if _hit is not None:
                _kbc.charge(_hit)                     # 命中：复现记账，额度口径不变
                cached = dict(_hit.get('result') or {})
                cached['_cache'] = 'hit'              # 仅供排障识别（不展示给用户）
                return cached
            _base = _kbc.snapshot_calls()             # 计算前基线（算完做差得真实消耗）
        else:
            _key = ''
    except Exception:
        _key, _base = '', None
    result = _guide_qa_uncached(tools, msg, identity, search_query, history)
    try:
        if _key and result:
            _kbc.store(_key, result, _base)           # 只收知识库问答类结果
    except Exception:
        pass
    return result


# 通用知识检索兜底：正则/固定分支全漏后，把"带问意、疑似拂晓内容"的输入交给
# GuideRAG（向量检索攻略库 → LLM 依据片段作答，标注来源）。目的是减少规则漏网点
# 对用户的暴露——除明确走工具（计算/模拟/查询/配装等上游域已覆盖）外，尽力检索知识回答，
# 而不是频繁让用户人工报漏。命中不足/失败 → None（交回闲聊 LLM，不硬答）。
_RAG_Q_WORD = re.compile(
    r'[？?]|吗|呢|怎么|如何|哪些|什么|多少|为什么|为啥|哪(?:个|种|类|艘|位)|谁|啥|'
    r'介绍|讲解|讲讲|机制|原理|' + intents.CHOICE_RE_SRC +
    r'|好不好|行不行|能不能|是否|是不是|强不强|优缺点|怎么看|注意|重要|必要')
# 生活/通用闲聊话题词：命中则不放行 RAG（避免攻略检索答非所问）
_RAG_CHAT_BLOCK = re.compile(
    r'吃饭|午餐|晚餐|宵夜|电影|电视剧|小说|音乐|游戏(?:推荐|好玩)|现实|工作|学习|'
    r'心情|累不累|睡(?:觉|了)|早安|晚安|天气|股票|基金|新闻|笑话|唱歌|减肥')


def _rag_knowledge_fallback(tools, msg: str, overrides=None, _li=None) -> dict:
    """route_work 末端兜底：RAG 检索作答（来源标注）。返回 None 表示不适合/检索失败。"""
    m = (msg or '').strip()
    if not m or len(m) < 4:
        return None
    if not _RAG_Q_WORD.search(m):
        return None
    if _RAG_CHAT_BLOCK.search(m):
        return None
    # 明确操作类意图（参数不全没走成工具）不硬答数值/流程，交回闲聊引导
    li = _li or {}
    if li.get('intent') in ('calc', 'encounter', 'pvp', 'favor', 'equip'):
        return None
    return _handle_guide_qa(tools, m)


# ======================================================================
# 遭遇战 BOSS 咨询（配队/打法/阵容）：结构化管线
# 触发条件（全部满足）：①能解析出正规 BOSS 名 ②含咨询类意图词 ③不是模拟表单参数
# 目的：绕开「整句向量检索」——先把 BOSS 归类（甲种/舰种/吨位），再定向取文
# （专属篇 + BOSS 表 + 应对表 + tips），最后 LLM 依据片段写建议。
# 背景：「遭遇战阿斯莫德配装配队有吗」原被「配装」关键词抢进 equip 分支 → 固定话术；
#      且实体名一字之差（阿斯/阿丝）导致检索必然落空。
# ======================================================================
_BOSS_ADV_INTENT = re.compile(
    r'配队|配装|阵容|怎么打|如何打|打法|怎么配|如何配|怎么组|如何组|搭配|攻略|推荐|建议|'
    r'怎么玩|有吗|有没有|用什么|带什么|上什么')
# 遭遇战/PVP「模拟」表单参数式输入 → 属于计算流程，不做咨询
_BOSS_SIM_FORM = re.compile(r'白天[：:]|黑夜[：:]|时间[：:]|时长[：:]|前排[：:]|后排[：:]')
# 明确的"发起计算"口吻 → 属于计算流程，让位给 calc（避免把「算嘉波配队伤害」抢成攻略建议）
_BOSS_CALC_OPEN = re.compile(r'帮我算|算一下|算一算|算算|发起计算|期望伤害|总伤害|dps', re.I)


def _handle_boss_advice(tools, msg: str, identity: str = '普通'):
    """遭遇战 BOSS 的配队/打法咨询 → 结构化建议。不适用返回 None。"""
    if not msg or _BOSS_SIM_FORM.search(msg) or _BOSS_CALC_OPEN.search(msg):
        return None
    if not _BOSS_ADV_INTENT.search(msg):
        return None
    try:
        from core.boss_guide import resolve_boss, build_advice
    except Exception:
        return None
    boss = resolve_boss(msg)
    if not boss:
        return None
    try:
        text, ctx = build_advice(boss, msg, identity)
    except Exception:
        return None
    if not text:
        return None
    return {'reply': text, 'tool_outputs': [ctx] if ctx else [], 'suggestions': [],
            'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '',
            'ok': True}


def _handle_tips(msg: str):
    """小技巧结构化直答（攻略检索的「结构化优先」一级）。

    知识库 攻略/tips.json 是**结论式短条目**（自带 category/title/tags），键命中即
    **原文直出**——不调 LLM、不会编造、零延迟。命中不了返回 None，让给后续的
    语义召回（攻略QA库 RAG）与攻略面板；「无键跳过」与「有键命中」同等重要。
    匹配口径见 core/tips_index（只认标题与 ≥4 字具体标签，2~3 字泛词一律忽略，防把答案锁窄）。"""
    if not msg:
        return None
    try:
        from core import tips_index
        text = tips_index.handle(msg)
    except Exception:
        return None
    if not text:
        return None
    return {'reply': text, 'tool_outputs': [], 'suggestions': [],
            'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '',
            'ok': True}


# ======================================================================
# 舰船「用途 / 就业」问答（用户口径 2026-10-03）
# 用户问「X 有什么用 / 有什么就业 / 能配什么队」时：到**向量库**里找攻略中**点名该舰**的
# 阵容 / 体系片段（实体为中心的检索 find_ship_mentions），把该舰在阵容里承担的**职责行**
# 原文抄给用户（连篇目·章节一起给）；攻略里没点名 / 没有阵容上下文 → 给诚实答复，不硬编。
# 取舍：这一步**不调 LLM** —— 就业本来就写在攻略的职责行里（如「输出拐：胡德、沙恩（旗舰）」），
# 原文直出最准、最快、零成本；回复同样带 from_kb 标记（前端会给 💡 反馈按钮）。
# ======================================================================
_SHIP_USE_RE = re.compile(
    r'有什么用|有啥用|干什么用|能干嘛|能干什么|能做什么|做什么用|干嘛用|拿来干嘛|拿来干'
    r'|就业|用途|定位是什么|什么定位|能配什么|配什么队|进什么队|适合什么队|有什么阵容|阵容里'
)
# 「阵容 / 体系 / 玩法场景」上下文特征：章节名或片段内容里出现即认为该片段在讲就业。
# 2026-10-03 补：练级 / 练船 / 值得练 / 活动 —— 这些同样是"什么地方能用"的答案
# （练级百科列的就是该练谁、怎么用），此前不在表里 → 该类证据被整段过滤掉，
# 于是"明明攻略写了"的船仍被答成没提到（用户反馈「同理检查练级、遭遇战之类」）。
_LINEUP_HINT_RE = re.compile(
    r'阵容|配队|体系|编队|站位|旗舰|推图|无尽|竞技|遭遇|演习|'
    r'练级|练船|值得练|活动|阵型|推荐舰灵|'
    r'AOE|aoe|输出|拐|奶|前排|后排|辅助|生存|单点|清杂|控场|坦克'
)
# 职责行：形如「输出拐：胡德、沙恩（旗舰）」「奶：杰维斯」——冒号前是短定位词
_DUTY_LINE_RE = re.compile(r'^[^：:=\n]{1,10}[：:]')
# 冒号前若是这些词，说明是机制/说明/因果句，不是"这艘船干什么"的就业行（如
# 「卡切换的原因是：…有胡德必杀…」曾被误收）。用于剔除这类句子。
_DUTY_BAD_PREFIX_RE = re.compile(r'是|原因|因为|所以|问题|说明|注意|机制|流程|步骤|操作|会导致|由于')


def _ship_use_llm_disabled() -> bool:
    """就业归纳是否停用（AKALI_SHIP_USE_LLM=0 关闭 → 直接走诚实兜底，不花 token）。"""
    import os as _os
    return str(_os.environ.get('AKALI_SHIP_USE_LLM', '1')).strip().lower() in (
        '0', 'false', 'no', 'off')


def _ship_use_facts(evid) -> str:
    """证据 → 给 LLM 的事实清单（攻略出处 + 原文短句）。"""
    rows = []
    for doc, sec, line in (evid or [])[:8]:
        w = ('《%s》' % doc) if doc else ''
        if sec:
            w += ('·' if w else '') + sec
        rows.append('- %s：%s' % (w or '攻略', line))
    return '\n'.join(rows) if rows else '（攻略里没有任何点名该舰的片段）'


def _ship_use_llm_summary(name: str, msg: str, evid) -> str:
    """就业归纳 + 自评把关：返回可用的简短归纳；未过关/失败/已关开关 → ''。

    生成与自评都**只依据给定证据**：证据里没有的阵容/舰名/数值/结论不得出现；
    证据为空时只能按舰种机制做通用判断，且必须显式标注"攻略未提、以下按舰种判断"，
    否则自评判不合格。调用方在返回 '' 时回落诚实兜底（用户口径 2026-10-03）。
    """
    if _ship_use_llm_disabled() or not name:
        return ''
    facts = _ship_use_facts(evid)
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=30)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                    '你是拂晓（舰灵海战手游）的攻略助手。任务：归纳某舰的「就业环境」'
                    '（什么地方能用 / 能进什么队）。\n'
                    '【只依据证据】下面是攻略里点名该舰的片段（含出处）。证据里没有的玩法、'
                    '阵容、舰名、数值一律不得写入；禁止把「同类手游通用机制」「一般来说」'
                    '「通常」这类常识当成结论。\n'
                    '【证据为空时】只能按该舰的舰种 / 技能机制做**通用判断**，并且开头必须写明'
                    '「攻略未提及该舰的具体就业，以下按舰种机制判断」，不许编具体阵容。\n'
                    '【输出】不超过 130 字，分 2~4 条短句，直说能用在什么地方；不确定就写不确定；'
                    '不要反问、不要推销、不要提「攻略库」等元信息。'},
                {'role': 'user', 'content':
                    '舰名：%s\n用户问：%s\n\n攻略证据：\n%s' % (name, msg, facts)},
            ],
        )
        draft = (resp.choices[0].message.content or '').strip()
    except Exception:
        return ''
    if not draft or len(draft) > 400:
        return ''
    try:
        if not _ship_use_verify(name, msg, facts, draft):
            print('[ship_use] 自评未通过 → 走诚实兜底 | %s' % name, flush=True)
            return ''
    except Exception:
        return ''       # 自评失败一律不放行（宁可不答，也不放未经把关的推断）
    return draft


def _ship_use_verify(name: str, msg: str, facts: str, draft: str) -> bool:
    """就业归纳的**自我评测**：核对是否忠于证据、有无编造 → 是否放行。

    典型不合格：出现证据外的具体阵容/舰名/数值；把推测写成确定；空证据时不标注
    "通用判断"就给具体打法；答非所问（没讲"能用在哪"）；含反问或推销。异常按不放行。
    """
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=30)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                    '你是严格的答案审核员。给你【攻略证据】和一份【草稿】，判断草稿能否发给用户。\n'
                    '不合格（ok=false）：出现证据里没有的具体阵容/舰名/数值/玩法结论；把推测写成确定；'
                    '证据为空却未标注「通用判断」就给出具体打法；没讲清"能用在哪"；含反问或推销。\n'
                    '合格（ok=true）：每句话都能在证据或舰种通识里找到支撑，且明确回答了就业方向。\n'
                    '只输出 JSON：{"ok": true/false, "reason": "一句话理由"}'},
                {'role': 'user', 'content':
                    '舰名：%s\n用户问：%s\n\n【攻略证据】\n%s\n\n【草稿】\n%s'
                    % (name, msg, facts, draft)},
            ],
        )
        raw = (resp.choices[0].message.content or '').strip()
    except Exception:
        return False
    _m = re.search(r'\{[\s\S]*\}', raw)
    if not _m:
        return False
    try:
        import json as _json
        obj = _json.loads(_m.group(0))
    except Exception:
        return False
    return obj.get('ok') is True


def _handle_ship_use(tools, msg: str):
    """「某舰有什么用 / 就业」→ 攻略向量库里找**阵容里点名该舰**的片段，原文作答。

    - 触发：消息里有用途/就业类问法 **且**能解析出具体舰名（BOSS 名不算，交回原链路）；
    - 命中：列出该舰在各阵容里承担的职责行（≤6 条）+ 篇目·章节出处；
    - 未命中：诚实答复"攻略未提及"（不返回 None，免得又被别的链路编一通）；
    - 非该类问法 / 解析不出舰名 → 返回 None（完全不影响原有链路）。
    """
    if not msg or not _SHIP_USE_RE.search(msg):
        return None
    # 舰名解析：先把昵称/错字换成正规名（standardize 返回的是**整句替换后文本**），
    # 再拿 App 里的正规舰名做子串匹配、取最长的那个（吹雪·改 优先于 吹雪）。
    # 用 App 的舰名表天然排除 BOSS（嘉波等不在 storage 里）→「嘉波配什么队」会交回遭遇战链路。
    try:
        from core.entity_norm import standardize
        _std = str(standardize(msg) or msg)
    except Exception:
        _std = msg
    app = None
    for _t in (tools or []):
        _cfg = getattr(_t, 'cfg', None)
        if isinstance(_cfg, dict) and _cfg.get('app') is not None:
            app = _cfg['app']
            break
    if app is None or not getattr(app, 'storage', None):
        return None
    name = ''
    for _n in (app.storage or {}):
        _n = str(_n)
        if _n and _n in _std and len(_n) > len(name):
            name = _n
    if not name:
        return None
    # 匹配变体（用户口径 2026-10-03）：改造舰在攻略里常按**未改造原名**写
    # （「吹雪·改」的阵容就写在「吹雪」名下），只用正规名精确匹配会漏证据。
    # 一并接受「去 ·改 / 去改」的写法；短于 2 字不收（单字极易误命中，如"雪/风"）。
    _names = [name]
    for _v in (name.replace('·改', '改'), name.replace('·改', ''), name.replace('改', '')):
        if _v and _v != name and len(_v) >= 2 and _v not in _names:
            _names.append(_v)

    def _mentions(txt: str) -> str:
        """该片段是否点名了本舰（含改造前原名），返回命中的那个写法（未命中 → ''）。"""
        for _v in _names:
            if _v in txt:
                return _v
        return ''

    # 候选片段两路取：① 定向阵容检索（query 直接带「阵容 配队 体系 定位」）
    # ② 实体为中心的检索（该舰被点名的片段）。两路都过下面同一套"职责行"过滤。
    cands = []                              # [(篇目, 章节, 内容)]
    try:
        from tools.knowledge import _guide_rag_search, _split_rag_segments
        # 查询词带上玩法场景（练级/遭遇战/活动），否则召回偏向"阵容"类篇章，
        # 练级百科、遭遇战攻略里点名该舰的片段进不了候选（用户口径 2026-10-03）。
        _text, _reason = _guide_rag_search(
            '%s 阵容 配队 体系 定位 练级 遭遇战 活动' % name, 8)
        for _doc, _sec, _content, _sim in _split_rag_segments(_text or ''):
            cands.append((_doc or '', _sec or '', _content or ''))
    except Exception:
        pass
    try:
        from tools.knowledge import find_ship_mentions
        for _mt in ((find_ship_mentions(name) or {}).get('mentions') or []):
            cands.append((str((_mt or {}).get('doc') or ''),
                          str((_mt or {}).get('section') or ''),
                          str((_mt or {}).get('content') or '')))
    except Exception:
        pass
    picks = []          # [(篇目, 章节, 职责行)]  强证据：「定位/职责：舰名…」这类带冒号的职责行
    evid = []           # [(篇目, 章节, 短句)]    弱证据：片段里点名该舰（如竞技场阵容「…埃塞克斯旗…」）
    _seen_seg = set()
    for doc, sec, content in cands:
        # 场景判定看**篇名 + 章节 + 正文**三处（用户口径 2026-10-03）：
        # 篇名本身就是场景（《争锋竞技场简易百科》《遭遇战攻略》《练级百科》…），
        # 而点名摘录常常很短、正文里未必出现"阵容/配队"字样 —— 只看正文会把
        # 「推荐舰灵：吹雪改，白雪改…」这类真正讲就业的句子误挡掉。
        if not (_LINEUP_HINT_RE.search(doc) or _LINEUP_HINT_RE.search(sec)
                or _LINEUP_HINT_RE.search(content)):
            continue                      # 与玩法/阵容无关的片段（数值表、获取方式…）不抄
        for raw in content.splitlines():
            line = raw.strip().lstrip('-*·• \u3000').strip()
            if not line or not _mentions(line):
                continue
            if ('=' in line) or ('表格记录' in line):
                continue                  # 表格行（名称=…；获取方式=…）不是就业描述
            if len(line) > 90:
                # 长句（如竞技场"默认为：女灶神，丹阳，…。备选：…"整段）→ 按句读切开，
                # 只留含该舰的那一小句，否则整句又长又杂、也过不了职责行正则。
                _parts = [p.strip() for p in re.split(r'[。；;]', line) if p.strip()]
                _hit = [p for p in _parts if _mentions(p) and len(p) <= 80]
                if not _hit:
                    continue
                line = _hit[0]
            if _DUTY_LINE_RE.match(line) and \
                    not _DUTY_BAD_PREFIX_RE.search(re.split(r'[：:]', line, 1)[0]):
                if (doc, sec, line) not in picks:
                    picks.append((doc, sec, line))
                break                     # 一个片段只取一行职责行，避免同一队伍刷屏
            # 点名了该舰但不是职责行（如竞技场「埃塞克斯旗为饺子旗雷击航」）→ 记弱证据，
            # 供后面归纳就业定位用；同一片段最多留一句，防刷屏。
            # 用户口径 2026-10-03：这类"攻略里明明写了"的船**必须找到并回复**，
            # 不能再谎报"攻略没提到"（「饺子就业」就是被旧逻辑的冒号要求漏掉的）。
            if (doc, sec) not in _seen_seg and len(line) <= 80:
                evid.append((doc, sec, line))
                _seen_seg.add((doc, sec))
    # 竞技场参考阵容是「谁在哪一队」的硬证据（正文按 金/紫/蓝 × 小/中/大 分块）。
    # 该舰在哪一块被点名就补一条证据 —— 不依赖 RAG 是否恰好召回该篇。
    try:
        from tools.knowledge import _pvp_parse_lineups
        for (_pq, _ps), _pblk in (_pvp_parse_lineups() or {}).items():
            for _pln in (_pblk or '').splitlines():
                _pln = _pln.strip()
                if _mentions(_pln) and len(_pln) <= 80:
                    _pe = ('争锋竞技场简易百科', '三、参考阵容推荐·%s%s船场' % (_pq, _ps), _pln)
                    if _pe not in evid:
                        evid.append(_pe)
                    break
    except Exception:
        pass
    def _fallback_reply():
        """没有职责行时的诚实兜底：有弱证据就列出"它在哪被点名"，不谎称"攻略没提到"。"""
        if not evid:
            return ('攻略里没有提到「%s」的阵容/就业定位，这个阿卡莉不能替它编。\n'
                    '（想看它自身的技能与面板，直接说「查%s」即可。）' % (name, name))
        _hits, _src = [], []
        for doc, sec, line in evid[:3]:
            _hits.append('- %s' % line)
        for doc, sec, _l in evid[:6]:
            _w = ('《%s》' % doc) if doc else ''
            if sec:
                _w += ('·' if _w else '') + sec
            if _w and _w not in _src:
                _src.append(_w)
        return ('攻略里没有写「%s」的就业定位（没有「定位：…」这类描述行），'
                '但它在这些地方被点名过：\n%s\n（出处：%s）\n'
                '（阿卡莉不替它编定位；想看它自身技能与面板说「查%s」。）'
                % (name, '\n'.join(_hits), '；'.join(_src) or '攻略相关章节', name))

    if not picks:
        # 用户口径 2026-10-03：找不到**直接对应**的攻略段落时走阶梯 ——
        # ① 先让 LLM 依据「攻略里点名该舰的片段 + 该舰面板/技能」做简短归纳；
        # ② 回复前**自我评测**（是否忠于证据、有没有编造、有没有答到"什么地方能用"）；
        # ③ 评测优秀才回给用户，否则回落诚实兜底（绝不硬编）。
        _syn = ''
        try:
            _syn = _ship_use_llm_summary(name, msg, evid)
        except Exception as _e:
            print('[ship_use] LLM 归纳失败，走兜底 | %s: %s' % (type(_e).__name__, _e), flush=True)
        if _syn:
            _src = []
            for doc, sec, _l in evid[:6]:
                _w = ('《%s》' % doc) if doc else ''
                if sec:
                    _w += ('·' if _w else '') + sec
                if _w and _w not in _src:
                    _src.append(_w)
            reply = '「%s」的就业：\n%s' % (name, _syn)
            if _src:
                reply += '\n\n参考出处：' + '；'.join(_src)
            return {'reply': reply, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '',
                    'from_kb': True, 'ok': True}
        _fb = _fallback_reply()
        return {'reply': _fb, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '',
                'from_kb': True, 'ok': True}
    _MAX = 6
    lines = []
    for doc, sec, line in picks[:_MAX]:
        where = ('《%s》' % doc) if doc else ''
        if sec:
            where += ('·' if where else '') + sec
        lines.append('· %s%s' % (line, ('　（%s）' % where) if where else ''))
    reply = ('「%s」在攻略里的就业：\n%s\n\n以上为攻略原文摘录（共 %d 处阵容/体系提及%s）。'
             % (name, '\n'.join(lines), len(picks),
                ('，此处列前 %d 处' % _MAX) if len(picks) > _MAX else ''))
    return {'reply': reply, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '',
            'from_kb': True, 'ok': True}


# ======================================================================
# 角色「天赋怎么点」（用户口径 2026-10-04）
# 问题：问「格奈森瑙天赋怎么点」答成了**通用规则**（增伤优先 / 不点对空 / 誓约八槽…）——
# 该舰不在攻略正文里（攻略 md 全库无「格奈森瑙」），检索没有依据，模型只能拿通识顶。
# 用户口径：**点名角色**的天赋 / 面板类问题必须读**底层数据**并输出对应内容，不给通用套路。
# 做法（确定性、零 LLM）：
#   ① 推荐点法取底层 talent_pvp 表（202 舰中 187 舰有推荐；键可能用改造名/别名，多重回退）；
#   ② 该舰天赋原文（四行二列的名字与效果）取 raw_data，**复用「查角色」同一段格式**，
#      保证与「查X」看到的内容完全一致；
#   ③ 推荐表里没有它 → 如实说明 + 列出四行供自选（绝不编点法）。
#   ④ 模式分支（用户口径 2026-10-06）：问法没说 PvE/PVP → PvP 与 PvE 两种点法都答；
#      只说 PvE → 只答 PvE，只说 PvP → 只答 PvP（"用户问哪个就回哪个"）。
#      PvE 表缺失时与 PVP 一致并明确标注，不冒充 PvE 专属。
# ======================================================================
_TALENT_WORD = '天赋'
# 「问点法」的问法词（不含"有哪些"——那种交回查角色链路，输出同一段原文）
_TALENT_ASK_RE = re.compile(
    r'怎么点|点什么|点啥|点哪个|点那个|点法|加点|怎么加|该怎么|如何点|怎么选|怎么带|带什么|'
    r'推荐|默认|优先|pve|pvp',
    re.I)   # pve/pvp 见用户口径 2026-10-05：「丹阳PVE天赋」这类正确提问要能检索到推荐点法


def _talent_block_of(app, name: str, raw) -> str:
    """复用「查角色」的天赋段（四行二列的名字与效果），与查询输出保持一致。"""
    try:
        out = app.format_raw_ship_summary(name, raw) or ''
    except Exception:
        return ''
    i = out.find('天赋 ---')
    if i < 0:
        return ''
    j = out.find('技能描述')
    if j < 0 or j < i:
        j = out.find('---------------------', i)
    return out[i:j].rstrip() if j > i else out[i:].rstrip()


# 底层天赋段有**两种**原始排版，解析必须都吃（用户口径 2026-10-05）：
#   ① 「第N行: 甲(效果) | 乙(效果)」           —— 一屏挤两列、行又长
#   ② 「[1A] 甲」换行「效果」…（含 `--- 丹阳 - 天赋 ---` 表头与结尾虚线）
# 统一重排成「行头 + 每行一个天赋」，并丢掉 `天赋 ---` / `-----` 等装饰线
# （原先装饰线会留在末尾，看着像多出来的括号/乱码）。
_TALENT_ROW_RE = re.compile(r'^第\s*(\d)\s*行\s*[:：]\s*(.*)$')
_TALENT_TAG_RE = re.compile(r'^\[\s*(\d)\s*([ABab])\s*\]\s*(.*)$')


def _format_talent_block(block: str, code: str = '') -> str:
    """把底层天赋段重排成好读的多行文本（纯展示，不改任何数值）。

    · 行头带点法编码字母（有推荐表时）：`第1行（A）`
    · 每个天赋单独一行，前缀 `· `；格式②把「名字 + 效果」合成与格式①一致的 `名字(效果)`
    """
    rows = {}            # 行号 -> [文本, …]（保序）
    pending = None       # 格式②：[行号, 名字, 效果累积]

    def _flush():
        nonlocal pending
        if pending and (pending[1] or pending[2].strip()):
            name, eff = pending[1].strip(), pending[2].strip()
            rows.setdefault(pending[0], []).append(('%s(%s)' % (name, eff)) if (name and eff) else (name or eff))
        pending = None

    for raw_line in (block or '').splitlines():
        ln = raw_line.strip()
        if not ln or set(ln) <= set('-—=_· '):        # 空行 / 纯装饰线（------）
            continue
        m1 = _TALENT_ROW_RE.match(ln)
        if m1:
            _flush()
            idx, body = int(m1.group(1)), m1.group(2)
            for c in (x.strip() for x in re.split(r'\s*[|｜]\s*', body) if x.strip()):
                rows.setdefault(idx, []).append(c)
            continue
        m2 = _TALENT_TAG_RE.match(ln)
        if m2:
            _flush()
            pending = [int(m2.group(1)), (m2.group(3) or '').strip(), '']
            continue
        if '天赋' in ln:                              # `天赋 ---` / `--- 丹阳 - 天赋 ---`
            _flush()
            continue
        if pending is not None:                       # 格式②：名字或效果续行
            if not pending[1]:
                pending[1] = ln                       # 名字在下一行（tag 只给了 [NA]）
            else:
                pending[2] += ln
            continue
    _flush()

    out = []
    for idx in sorted(rows):
        head = '第%d行' % idx
        pick = code[idx - 1] if 0 <= idx - 1 < len(code) else ''
        if pick:
            head += '（%s）' % ('A' if pick == '1' else ('B' if pick == '2' else pick))
        out.append('\n'.join([head] + ['· %s' % t for t in rows[idx]]))
    return '\n\n'.join(out) if out else (block or '').strip()


def _handle_talent_ask(tools, msg: str):
    """「X 天赋怎么点」→ 直读底层：推荐点法（有则给）+ 天赋原文；非该类问法返回 None。"""
    m = (msg or '').strip()
    if not m or _TALENT_WORD not in m or not _TALENT_ASK_RE.search(m):
        return None
    app = None
    for _t in (tools or []):
        _cfg = getattr(_t, 'cfg', None)
        if isinstance(_cfg, dict) and _cfg.get('app') is not None:
            app = _cfg['app']
            break
    if app is None or not getattr(app, 'storage', None):
        return None
    # 舰名：昵称/错字先归一，再取最长的正规名（与「就业」同一套解析，天然排除 BOSS）
    try:
        from core.entity_norm import standardize
        _std = str(standardize(m) or m)
    except Exception:
        _std = m
    name = ''
    for _n in (app.storage or {}):
        _n = str(_n)
        if _n and _n in _std and len(_n) > len(name):
            name = _n
    if not name:
        return None
    try:
        _canon, raw = app.get_raw_ship(name)
    except Exception:
        return None
    if raw is None:
        return None
    # ① 推荐点法：PVP / PvE 两张底层表（无 PvE 表时与 PVP 一致并明确标注）
    # 模式判定（用户口径 2026-10-06）：问法没说 PvE/PVP → 两种都答；说了哪个就只答哪个
    _has_pve = bool(re.search(r'pve', m, re.I))
    _has_pvp = bool(re.search(r'pvp', m, re.I))
    if _has_pve and not _has_pvp:
        _modes = ['pve']
    elif _has_pvp and not _has_pve:
        _modes = ['pvp']
    else:
        _modes = ['pve', 'pvp']
    try:
        from tools.talent import load_talent_pvp, load_talent_pve
        _pvp_tbl = load_talent_pvp() or {}
        _pve_tbl = load_talent_pve() or {}
    except Exception:
        _pvp_tbl, _pve_tbl = {}, {}
    def _lookup(tbl):
        for _k in (name, str(_canon or ''), name.replace('·', ''),
                   name.replace('·改', '改'), name.replace('·改', '')):
            if _k and tbl.get(_k):
                return str(tbl[_k]).strip()
        return ''
    _pvp_code = _lookup(_pvp_tbl)
    _pve_code = _lookup(_pve_tbl)
    _pve_fallback = False
    _codes = {}
    if _pvp_code:
        _codes['pvp'] = _pvp_code
    if _pve_code:
        _codes['pve'] = _pve_code
    elif _pvp_code:
        _codes['pve'] = _pvp_code      # 该舰无独立 PvE 配点 → 与 PVP 一致（下方标注，不冒充专属）
        _pve_fallback = True
    # ② 天赋原文（与「查X」一致）
    block = _talent_block_of(app, name, raw)
    _empty = {'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
              'guide_files': [], 'guide_lib': '', 'from_kb': True, 'ok': True}
    if not block:
        _r = dict(_empty)
        _r['reply'] = '底层数据里没有「%s」的天赋信息（信息不全），这个阿卡莉不能替它编点法。' % name
        return _r
    # 按模式过滤；过滤后若两张表都没该舰推荐 → 走「自选」兜底（与旧逻辑一致）
    _show = {k: v for k, v in _codes.items() if k in _modes}
    if not _show:
        reply = ('「%s」的底层推荐表里没有收录它的点法（全 202 舰中 187 舰有推荐），'
                 '下面的天赋原文供指挥官自选：\n\n%s\n\n'
                 '来源：底层数据 raw_data 天赋表。要按某个编码计算，直接把 4 位编码给我，'
                 '如 1212；也可先「查%s」看完整面板与技能。'
                 % (name, _format_talent_block(block), name))
        _r = dict(_empty)
        _r['reply'] = reply
        return _r
    _parts = []
    for _mode in _modes:
        _c = _show.get(_mode)
        if not _c:
            continue
        _note = '（该舰暂无独立 PvE 配点，暂与 PVP 一致）' if (_mode == 'pve' and _pve_fallback) else ''
        _label = 'PVP' if _mode == 'pvp' else 'PvE'
        _parts.append('【%s 点法】底层推荐表默认 %s%s\n\n%s'
                      % (_label, _c, _note, _format_talent_block(block, _c)))
    reply = ('「%s」天赋点法：\n\n%s\n\n'
             '来源：底层数据 raw_data 天赋表 ＋ talent_pvp / talent_pve 推荐表'
             % (name, '\n\n'.join(_parts)))
    _r = dict(_empty)
    _r['reply'] = reply
    return _r


# ======================================================================
# 争锋竞技场「队伍/阵容推荐」交互式路由（用户口径 2026-10-03 初版，2026-10-12 加反问）
# 问题：问「竞技场中船队伍推荐」，RAG 只捞到「金大船」那几块就作答（正文写的是
# 「金中船/紫中船」，体量词字面对不上），答案跑偏；换个说法（pvp…）还会撞上服务异常。
# 做法：攻略《7.1 争锋竞技场简易百科》的「三、参考阵容推荐」本来就按
# 【金/紫/蓝 × 小/中/大船】分块写好（tools.knowledge._pvp_docx_sections 直接解析原稿 docx）。
# 这里按用户问到的场质/体量**按原攻略格式输出**：场次标题 + 原文段落（说明/优点/缺点/↑注释）
# + 头像行（一排头像图 + 舰名，取自攻略头像素材，旗舰按原稿黄色高亮）—— 零 LLM、不改写。
# 头像行的舰名只存在 docx 表格单元格里（转 md 时丢了），故取 docx 原稿而非 md。
# 反问式（2026-10-12）：用户只说「推荐阵容」/「中船阵容」没指明场次/旗舰时，先反问具体信息
# （XX 旗舰 / XX 队 / 金紫蓝×小中大），把 pvp_lineup_pending 记进 overrides 跨轮记忆，
# 再根据后续回答定向出阵容。已明确场次的问法直接出。
# ======================================================================
_PVP_LINEUP_RE = re.compile(r'竞技场|争锋|pvp|jjc', re.I)
_PVP_WANT_RE = re.compile(r'阵容|队伍|配队|推荐|用哪些|带哪些|练哪些')
# 命名队伍 → 固定场次（用于后续回答里只说「索敌队」「凤栖队」「同航队」等不含场质体量的情形）
_PVP_TEAM_HINT_RE = re.compile(r'索敌队|利根旗|诺福克旗|贝尔法斯特旗|凤栖队')
_PVP_TEAM_HINT_SMALL_RE = re.compile(r'同航队|反航队|马桶队')
# 追问轮退出词：放弃反问，交回其它路由
_PVP_EXIT_RE = re.compile(r'退出|换话题|不用了|算了|取消|不看了|还是算了|不想要了|别的')


def _pvp_extract_spec(msg):
    """从文本解析场次规格：返回 (qs, ss, ships)。
    qs：场质集合（金/紫/蓝）；ss：体量集合（小/中/大）；ships：命中的已知舰名（已归一为正规名）。"""
    qs = [q for q in '金紫蓝' if q in msg]
    ss = [s for s in '小中大' if s in msg]
    ships = []
    try:
        from tools.knowledge import _PVP_ALIAS, _pvp_rarity_map
        rarity = _pvp_rarity_map() or {}
        names = sorted(set(rarity) | set(_PVP_ALIAS), key=len, reverse=True)
        canon = {}
        for n in names:
            if n and n in msg:
                c = _PVP_ALIAS.get(n, n)        # 昵称归一为正规名，再拿去块里检索
                if c not in canon:
                    canon[c] = True
        ships = list(canon)
    except Exception:
        pass
    if _PVP_TEAM_HINT_RE.search(msg):
        qs, ss = ['金'], ['中']
    elif _PVP_TEAM_HINT_SMALL_RE.search(msg):
        qs, ss = ['金'], ['小']
    return qs, ss, ships


def _pvp_ask_reply(overrides, partial=None):
    """反问：让用户指明场次 / 旗舰 / 队伍方向，并记 pending 跨轮（携带已给的局部规格）。"""
    if overrides is not None:
        overrides['pvp_lineup_pending'] = True
        if partial:
            overrides['pvp_lineup_partial'] = partial
    # 已给体量但缺场质（如「中船阵容」）→ 针对性反问「金/紫/蓝 哪个场的中船」
    if partial and partial.get('ss'):
        sizes = '、'.join(partial['ss'])
        reply = ('你想看 **%s船** 阵容里 **金/紫/蓝** 哪个场的？直接说场质即可'
                 '（如「金」=金%s船、「紫」=紫%s船）。' % (sizes, sizes, sizes))
    else:
        reply = ('想看哪一场的阵容？争锋竞技场按 **金/紫/蓝 × 小/中/大船** 分六档'
                 '（直接说「金大船」「紫中船」即可）。\n'
                 '也可以告诉我你想用的**旗舰或队伍方向**（比如「丹阳旗舰」「索敌队」），'
                 '我给你对应阵容 + 阵容分析推荐图。')
    return {
        'reply': reply,
        'tool_outputs': [], 'suggestions': [], 'fill_form': [],
        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
    }


def _pvp_answer(tools, qs, ss, ships, overrides):
    """按场次/舰名取《参考阵容推荐》，**按原攻略格式输出**：场次标题 + 原文段落
    （说明/优点/缺点/↑注释）+ 头像行（一排头像+舰名，取自攻略头像素材，旗舰高亮）。
    不统计数量、不把头像堆在末尾。头像行数据取自 docx 原稿（md 丢了行内舰名）。"""
    try:
        from tools.knowledge import _pvp_parse_lineups
        blocks = _pvp_parse_lineups() or {}
    except Exception:
        return None
    if not blocks:
        if overrides is not None:
            overrides['pvp_lineup_pending'] = None
        return None
    # qs/ss 都为空（只给了舰名）时不做场次过滤，留给下方舰名检索分支处理；
    # 否则 (not qs or ...) 在空列表下恒为真，会误把全部块当成命中
    if qs or ss:
        keys = [k for k in sorted(blocks, key=lambda k: ('金紫蓝'.index(k[0]), '小中大'.index(k[1])))
                if (not qs or k[0] in qs) and (not ss or k[1] in ss)]
    else:
        keys = []
    # 给了舰名但没给场次：搜含该舰的场次块（昵称已归一为正规名）
    if not keys and ships:
        hit = sorted({(q, s) for (q, s), text in blocks.items()
                      if any(sh in text for sh in ships)},
                     key=lambda k: ('金紫蓝'.index(k[0]), '小中大'.index(k[1])))
        if len(hit) == 1:
            keys = hit
        elif len(hit) > 1:
            # 舰名跨多场出场 → 反问具体场次（保持 pending，下一轮「金小」即可下钻）
            if overrides is not None:
                overrides['pvp_lineup_pending'] = True
            labels = '、'.join('%s%s' % (q, s) for q, s in hit)
            return {'reply': ('「%s」在多个场次都有出场：%s。你看哪个场次的阵容？直接说场次即可（如「金小」）。'
                             % ('、'.join(ships[:3]), labels)),
                    'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
        # hit == 0 → 落到下方「没检索到」
    if not keys:
        if overrides is not None:
            overrides['pvp_lineup_pending'] = None
        tip = ('参考阵容按场次（金/紫/蓝 × 小/中/大船）分类，没检索到「%s」所在的固定队伍。'
               '请直接告诉我场次，比如「金大船」「紫中船」。' % '、'.join(ships[:3])) \
            if ships else '没找到对应场次的固定阵容，请确认场次（金/紫/蓝 × 小/中/大船），比如「金大船」「紫中船」。'
        return {'reply': tip, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    # 按「原攻略格式」输出：场次标题 + 原文段落 + 头像行（一排头像+舰名，攻略头像素材）。
    # 数据取自 docx 原稿（md 丢了头像行里的舰名）。头像行由 tools.knowledge 出 <<SROW>> 标记，
    # 前端渲染成一排「头像图 + 名字」；旗舰按原稿黄色高亮。
    try:
        from tools.knowledge import _pvp_docx_sections, _pvp_row_marker
        secs = _pvp_docx_sections() or {}
    except Exception:
        secs = {}
    if not secs:
        return None
    CAP = 40                      # 单次最多渲染的头像行数（每行 6 艘），避免回复过长
    parts, rows_used, truncated = [], 0, False
    for (q, s) in keys[:3]:
        elems = secs.get((q, s)) or []
        if not elems:
            continue
        parts.append('◆ %s·%s船场' % (q, s))
        for el in elems:
            if el[0] == 'p':
                parts.append(el[1])
            elif rows_used < CAP:
                parts.append(_pvp_row_marker(el[1], el[2] if len(el) > 2 else ()))
                rows_used += 1
            else:
                truncated = True
    if not parts:
        return None
    if overrides is not None:
        overrides['pvp_lineup_pending'] = None
        overrides.pop('pvp_lineup_partial', None)
    head = ('《争锋竞技场简易百科·参考阵容推荐》（%s）：'
            % '＋'.join('%s%s' % (q, s) for q, s in keys[:3]))
    tail = '（攻略原文阵容，未改写；天气与赛季会影响强度，别照抄。想看整篇回复「争锋攻略」。）'
    if truncated:
        tail = '（篇幅所限仅显示前 %d 行，余下见原攻略。%s' % (CAP, tail[1:])
    reply = head + '\n\n' + '\n'.join(parts) + '\n\n' + tail
    return {'reply': reply, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


def _handle_pvp_lineup(tools, msg: str, overrides: dict = None):
    """争锋竞技场阵容推荐：先反问、再按后续回答定向出阵容（附表格图）。

    首次触发：消息含 阵容/队伍/推荐 等词，且带争锋特有标记
    （场质金/紫/蓝、体量小/中/大、具体舰名、命名队，或 竞技场/争锋/pvp 信号）。
    · 已唯一定位（有场质/舰名/命名队，如 金大船/丹阳旗舰）→ 直接出，不反问；
    · 仅体量或全无（如「中船」「推荐阵容」）→ 反问并记录 pvp_lineup_pending 跨轮，
      追问轮补的维度会叠加（如「中船」→「金」=金中船）。
    追问轮（pending 已置）：后续可只说「金大船」「丹阳旗舰」「索敌队」即定位，
    含退出词则放弃反问交回原链路；仍含糊则再次反问。
    """
    m = (msg or '').strip()
    if not m:
        return None
    pending = bool((overrides or {}).get('pvp_lineup_pending'))

    if pending:
        # 追问轮：不再要求含 pvp 关键词，按后续回答定位（并叠加上轮已给的局部规格）
        if _PVP_EXIT_RE.search(m):
            overrides['pvp_lineup_pending'] = None
            overrides.pop('pvp_lineup_partial', None)
            return None
        qs2, ss2, ships2 = _pvp_extract_spec(m)
        part = (overrides or {}).get('pvp_lineup_partial') or {}
        # 后续完全没新增信息（仍含糊）→ 继续反问（保留上轮局部规格）
        if not (qs2 or ss2 or ships2):
            return _pvp_ask_reply(overrides, part)
        # 后续补的维度优先；没补的维度沿用上轮反问时已携带的局部规格（如「中船」→「金」=金中）
        qs = qs2 or part.get('qs', [])
        ss = ss2 or part.get('ss', [])
        ships = ships2 or part.get('ships', [])
        return _pvp_answer(tools, qs, ss, ships, overrides)

    # 首次：必须命中「阵容/队伍/推荐」类问法
    if not _PVP_WANT_RE.search(m):
        return None
    qs, ss, ships = _pvp_extract_spec(m)
    _has_pvp = bool(_PVP_LINEUP_RE.search(m))
    # 无 pvp 信号时，必须带争锋特有标记（场质金/紫/蓝 / 体量小中大 / 具体舰名 / 命名队）才认作 PVP，
    # 否则可能是 PvE 配队问法，交回原链路（不反问、不抢答）。
    # 场质金/紫/蓝 是争锋独有稀有度档，见到即认作 PVP 阵容问法（如「紫场阵容推荐」→ 全部紫场）；
    # 仅体量（如「中船」）也认作 PVP（争锋独有分档），但缺场质无法唯一定位 → 走反问。
    _explicit = bool(qs or ss or ships
                     or _PVP_TEAM_HINT_RE.search(m) or _PVP_TEAM_HINT_SMALL_RE.search(m))
    if not (_has_pvp or _explicit):
        return None
    # 已有场质 / 舰名 / 命名队（team hint 内部已设 qs/ss）→ 能唯一定位，直接出；
    # 仅体量 / 全无 → 反问（携带已给的局部规格，后续补场质即可组合，如「中船」→「金」=金中）
    if qs or ships or _PVP_TEAM_HINT_RE.search(m) or _PVP_TEAM_HINT_SMALL_RE.search(m):
        return _pvp_answer(tools, qs, ss, ships, overrides)
    return _pvp_ask_reply(overrides, {'qs': qs, 'ss': ss, 'ships': ships})


def _route_guide(tools, msg: str) -> dict:
    """攻略路由：①精确篇名匹配 → 直接给那一篇攻略（免费）；
    ②分类直达 → 打开滑块并选中分类；
    ②b新手泛问 → 归类新人玩家，给萌新入门主题介绍；
    ③具体问题 → 向量检索攻略片段并作答（攻略QA库 RAG，LLM 依据片段组织回答）；
    ④本地 query_patterns 关键词直接映射 → 给对应主题攻略（免费）；
    ⑤对不上 → 先试向量检索，再 LLM 辅助判断主题 → 攻略面板（检索低分回落面板）。"""
    # 0. 遭遇战 BOSS 咨询优先（配队/打法/阵容）→ 结构化管线（归类→定向取文→LLM 综合）
    _ba = _handle_boss_advice(tools, msg)
    if _ba is not None:
        return _ba
    # 0a. 精确篇名匹配：输入能唯一对应某篇攻略（如 装备一图流→装备速查表）→ 直接给那一篇
    #（优先于分类直达，避免"装备一图流"落进"一图速查"滑块或装备多篇列表）
    try:
        from tools.knowledge import _exact_guide_doc, _single_guide_lines
        exact = _exact_guide_doc(msg, _load_guide_kb().get('documents', []))
    except Exception:
        exact = None
    if exact is not None:
        text = _single_guide_lines(exact)
        return _guide_reply_from_text(text, exact.get('title') or '')
    # 0b. 分类名直达：进阶/一图速查/萌新入坑/练级须知 → 打开滑块并选中该分类
    cat = _match_category(msg)
    if cat:
        return {'reply': f'已为你打开「{cat}」分类，按需查看。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': cat, 'ok': True}
    # 0b-2. LLM 先理解用户意图与玩家身份，再决定路由：
    #   newbie（萌新向笼统问法）→ 萌新入门主题介绍，不交 RAG 硬答（此类泛问检索分数不稳，
    #   自测 0.49~0.65 对错参半）；docs（要文档/清单）→ 攻略面板；
    #   qa（具体问题）→ 继续走向量检索，简答按身份口吻调整。
    # LLM 失败/超时 → 沿用正则泛问快速判定（确定性失败路径）
    intent = _llm_guide_intent(msg)
    if intent is None:
        if re.search(r'刚玩|刚下|刚入坑|刚接触|刚开始玩|新手|萌新|新人|小白|啥都不懂|什么都不懂|啥都不会|求带'
                     r'|这游戏怎么玩|这个游戏怎么玩|游戏怎么玩|拂晓怎么玩|该怎么玩|如何玩|怎么入门|如何入门|怎么开始玩|前期怎么', msg) \
                and not re.search(r'练|推荐|配|阵容|装备|竞技|遭遇|推图|伤害|好感|改造|技能|天赋|保底|排行|角色|舰灵|培养|强度', msg):
            r = _handle_theme_intro(tools, msg, '萌新')
            if r is not None:
                return r
        _identity = '普通'
    else:
        if intent['kind'] == 'newbie':
            # 点名了具体舰灵的新手问句（守卫拦下泛介绍）→ 继续走下方向量检索答具体问题
            r = _handle_theme_intro(tools, msg, '萌新')
            if r is not None:
                return r
        if intent['kind'] == 'docs':
            return {'reply': '攻略合集已展开，按分类查看即可。',
                    'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '*', 'ok': True}
        _identity = intent['identity']
    # 0c. 向量检索优先：攻略类问法先查攻略QA库（本地余弦相似度，毫秒级），
    #     最高分过输出线（0.50）即由 LLM 依据片段简答——智能与否交给检索分数，
    #     不靠词表枚举问法；命中不足/未命中/检索失败 → 回落主题与面板路径
    #     例外：名词式攻略请求（"练级攻略"/"萌新攻略"/"查攻略"等，以"攻略"结尾的短消息）
    #     要的是文档清单而非答案，跳过检索走面板/主题路径
    _s = msg.strip()
    if not (len(_s) <= 12 and re.search(r'攻略(大全|合集|目录|列表)?$', _s)):
        r = _handle_guide_qa(tools, msg, _identity)
        if r is not None:
            return r
    tool = _tool(tools, 'query_guide')
    if tool is None:
        return None
    topic = _local_guide_topic(msg)
    if topic:
        # 主题在 topic_routing 内 → 直接按主题给；否则（战斗机制/养成机制等）用原话
        # 交回 query_guide 内部 query_patterns 二次命中
        keyword = topic if topic in _guide_topics() else msg
    elif '攻略' in msg:
        # 含"攻略"但未指明具体主题（查攻略/看攻略/帮我看看攻略/攻略大全）→ 只开滑块面板，
        # 不贴全目录（前端滑块已按分类展示全部攻略）
        return {'reply': '攻略合集已展开，按分类查看即可。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '*', 'ok': True}
    else:
        # 本地没对上：向量检索已在 0c 尝试过（低分回落）。
        # 意图分析已给出主题 → 直接用（省一次 LLM 调用）；否则 LLM 辅助判断 → 面板
        topic = (intent.get('topic') if intent else '') or _classify_guide_topic(msg, _guide_topics())
        keyword = topic or ''
    if not keyword:
        # 分类识别不出 → 同样只开滑块，不贴全目录
        return {'reply': '攻略合集已展开，按分类查看即可。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '*', 'ok': True}
    out = tool.call({'keyword': keyword})
    text = out if isinstance(out, str) else str(out)
    return _guide_reply_from_text(text, keyword)


# 指代/回指词（上下文继承）：统一替换为最近提及的角色名。用多字词优先，避免误伤普通用词。
_PRONOUN_RE = re.compile(
    r'(这只舰灵|那个舰灵|这个舰灵|这艘船|那艘船|这个角色|那个角色|这位角色|该角色|'
    r'这个人|那个人|这艘|那艘|这只|那只|这条|那条|该舰|此舰|这位|此人|(?<!其)她|(?<!其)他|(?<!其)它)')


def _resolve_pronoun(msg: str, overrides: dict) -> str:
    """上下文继承：把指代词（她/他/它/这位/这只/这艘/那艘/该角色/该舰…）替换为**最近提及的角色**。"""
    last = overrides.get('last_role', '') or ''
    if not last:
        return msg
    return _PRONOUN_RE.sub(last, msg)


# 时装/皮肤固定话术 + 触发词（用户口径 2026-09-28）
_SKIN_KW_RE = re.compile(r'皮肤|时装|换装|外观')
_SKIN_FIXED_REPLY = '指挥官，想挑新衣服的话，自行去游戏内商店和兑换商店看看吧。'


def _handle_skin_query(tools, msg):
    """时装/皮肤询问：信息库未收录。
    · 含「券」（皮肤券/时装券/兑换券…）→ 走向量库检索（攻略 RAG）；
    · 其余皮肤/时装类 → 固定引导（游戏内商店 + 兑换商店），表情用「微笑关心」差分。
    未命中返回 None。
    """
    if not _SKIN_KW_RE.search(msg or ''):
        return None
    if re.search(r'券', msg or ''):
        try:
            g = _handle_guide_qa(tools, msg)
            if g is not None:
                return g
        except Exception:
            pass
    return {'reply': _SKIN_FIXED_REPLY,
            'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
            'expression_hint': '微笑关心'}


# ---- 性能优化小开关（战斗界面右上角「SKILL OFF/ON」）确定性路由意图（用户口径 2026-10-10）----
# 问「性能优化小开关怎么用 / 反和谐怎么弄 / SKILL OFF 怎么关技能动画 / 帧率怎么调 90 帧」这类问题
# （含反和谐相关问法：反和谐与性能优化共用同一个 extraConfig.json 配置，走同一路由）：
# 信息库已有《性能优化小开关教程》图（攻略/5 …/1-性能优化小开关教程260107.png）
# 与 OCR 摘要（img_001：配置路径 + Normal_fps/Limit_fps/Localization 参数），
# 直接**确定性作答 + 内联教学图**，不调 LLM / 不走向量检索，
# 避免「性能优化/反和谐」被当成泛化攻略问法交 RAG 后答偏或答成游戏简介。
_PERF_TOGGLE_RE = re.compile(
    r'性能优化|小开关|skill\s*(?:off|on)|帧率|帧数|\d{2,3}\s*帧|'
    r'extraConfig|normal_fps|limit_fps|\bfps\b|'
    r'反和谐|去和谐|去掉和谐|去除和谐|关和谐|开和谐|破和谐|被和谐|和谐了|和谐掉|'
    r'和谐模式|和谐开关|和谐补丁|和谐遮挡|'
    r'立绘和谐|模型和谐|和谐立绘|和谐模型', re.I)
# top_image.path 相对 知识库/（=web_app 的 KB_STATIC_DIR），前端拼 /kb/ 后内联渲染
_PERF_TOGGLE_IMAGE = {
    'path': '攻略/5 立香也能看懂的一图流攻略合集/1-性能优化小开关教程260107.png',
    'title': '性能优化小开关教程',
}
_PERF_TOGGLE_REPLY = (
    '战斗界面**右上角的「SKILL OFF/ON」**就是性能优化小开关，用来开关技能动画演出：\n'
    '· **SKILL OFF** → 关闭技能动画，低配机更流畅；\n'
    '· **SKILL ON** → 恢复技能动画演出。\n'
    '\n'
    '想把帧率也拉满到 90 帧，改配置文件即可：\n'
    '1. 路径：`Android/data/com.megagame.fuxiaou.fuxiaou/files/extraConfig.json`；\n'
    '2. 把 `Normal_fps`、`Limit_fps` 都由 30 改成 90，`Localization` 保持 1；\n'
    '3. 操作：把 `extraConfig.json` 后缀改成 `txt` → 文本编辑器打开改参数 → 保存后改回 `json`。\n'
    '\n'
    '反和谐也是改同一个 `extraConfig.json`：里面的 `Localization` 字段控制和谐/反和谐显示模式，'
    '按你想要的版本设好即可（教程里让它保持 1）。\n'
    '\n'
    '优化后舰灵立绘更流畅、名字显示更清晰，教学攻略图见下。'
)


def _handle_perf_toggle(tools, msg):
    """性能优化小开关（SKILL OFF/ON）怎么用：确定性答复 + 内联教学图（免费、不走 LLM）。
    未命中返回 None，交回其它路由。"""
    if not _PERF_TOGGLE_RE.search(msg or ''):
        return None
    return {'reply': _PERF_TOGGLE_REPLY,
            'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
            'top_image': dict(_PERF_TOGGLE_IMAGE)}


# 主题介绍映射：主题词 → (检索词, 判定性引导问题)
# 生活话题词（真实生活诉求：吃饭/累/心情/身体等）：命中即强制闲聊，不进任何工作/攻略路由。
# 作 LLM 意图分类的兜底矫正——防止 LLM 偶发把「能帮我做饭吗」这类请求语气误判为攻略建议。
_LIFE_TOPIC_RE = re.compile(
    r'饿|做饭|煮饭|吃饭|饭|渴|累|困|睡|病|不舒服|难受|身体|健康|'
    r'心情|近况|头疼|头痛|发烧|感冒|例假|生理期|大姨妈|疲惫')

_THEME_INTRO = {
    '拂晓': ('拂晓 游戏 介绍 玩法 舰灵', '想进一步了解什么？①入坑/新手攻略 ②某个舰灵/角色 ③具体玩法（练级/装备/竞技场等）？'),
    '这游戏': ('拂晓 游戏 介绍 玩法 舰灵', '想进一步了解什么？①入坑/新手攻略 ②某个舰灵/角色 ③具体玩法（练级/装备/竞技场等）？'),
    '遭遇战': ('遭遇战 玩法 介绍 规则', '指挥官是想**算遭遇战伤害**（遭遇战模拟），还是查**遭遇战攻略**（怎么打/配队/冲榜）？'),
    '竞技场': ('争锋竞技场 玩法 介绍 规则', '是想**模拟组队**伤害、查**竞技场攻略**（阵容/防守），还是看**天赋加点**？'),
    '争锋': ('争锋竞技场 玩法 介绍 规则', '是想**模拟组队**伤害、查**竞技场攻略**（阵容/防守），还是看**天赋加点**？'),
    'jjc': ('争锋竞技场 玩法 介绍 规则', '是想**模拟组队**伤害、查**竞技场攻略**（阵容/防守），还是看**天赋加点**？'),
    'jzc': ('争锋竞技场 玩法 介绍 规则', '是想**模拟组队**伤害、查**竞技场攻略**（阵容/防守），还是看**天赋加点**？'),
    '练级': ('练级 玩法 练级点 经验', '是想查**练级攻略**（怎么练/去哪练），还是查**练级点**（18-7/19-5 等具体图）？'),
    '无尽海域': ('无尽海域 玩法 介绍', '是想查**无尽海域配队/攻略**，还是查**通关/层数**相关的？'),
    '拉古兹': ('拉古兹 专题 boss 介绍', '是想查**拉古兹boss攻略/图鉴**，还是**算某个boss的遭遇战伤害**？'),
    '萌新': ('拂晓 萌新 入坑 新手 玩法', '是想看**入坑/新手攻略**（怎么开始玩），还是查**某个具体玩法/角色**？'),
    '新人': ('拂晓 萌新 入坑 新手 玩法', '是想看**入坑/新手攻略**（怎么开始玩），还是查**某个具体玩法/角色**？'),
    '改造': ('舰灵改造 优先级 介绍', '是想查**改造攻略/优先级**，还是**算改造后伤害**（如 丹阳/夕立·改）？'),
    '活动': ('活动玩法 介绍 攻略', '是想查**当前活动攻略/推图**，还是**打捞/活动奖励**相关的？'),
    '装备': ('装备 系统 类型 介绍', '是想**查装备数据**（装备表），还是看**装备搭配攻略**？'),
}


def _resolve_ship_query(msg: str, tools) -> str:
    """从「角色名 + 模糊问句」（光辉是啥/是什么/介绍一下/看看光辉）中识别角色。
    命中知识库角色 → 返回正规名；否则返回 ''（交回其他路由）。
    仅当去掉问句辅助词后的候选名能匹配到角色才触发——"争锋是啥"（争锋非角色）
    不会误进角色查询，仍走主题介绍。主题词（拂晓/装备/练级等）明确不查角色。"""
    if not msg or len(msg) > 24:
        return ''
    # 主题词（玩法/系统名）→ 不查角色（"拂晓是什么"是游戏介绍，不是查角色"晓"）
    if any(k in msg for k in _THEME_INTRO):
        return ''
    if not re.search(r'是啥|是什么|是谁|是哪位|介绍一下|介绍下|给我介绍|详情|属性|数据|看看|查查|查一下|长啥样|怎么样|怎样|咋样|如何|是$|是$', msg):
        return ''
    hint = re.sub(r'是啥|是什么|是谁|是哪位|介绍一下|介绍下|给我介绍|介绍|详情|属性|数据|看看|查查|查一下|长啥样|怎么样|怎样|咋样|如何|是|？|\?|！|!|。|，|,|、|\s', '', msg).strip()
    if not hint or len(hint) > 6:
        return ''
    qtool = _tool(tools, 'query_ship_info')
    app = qtool.cfg.get('app') if qtool else None
    if app is None:
        return ''
    # 精确/别名/昵称命中（get_ship_from_all 走 NameMatcher，含昵称表）
    ship = app.get_ship_from_all(hint)
    if ship is not None:
        return ship.name
    # 包含匹配（去间隔号归一化，长度差 ≤2）："光辉改"→"光辉·改"；防"拂晓"误中"晓"
    hn = hint.replace('·', '').replace('.', '').replace('-', '')
    raw = getattr(app, 'raw_storage', {}) or {}
    for n in raw:
        nn = n.replace('·', '').replace('.', '').replace('-', '')
        if hn and len(nn) >= 2 and (hn in nn or nn in hn) and abs(len(hn) - len(nn)) <= 2:
            return n
    return ''


# 多角色求和仅支持「索敌」（与 query_scout_sum 保持一致）
_SCOUT_SUM_FIELD = '索敌'
# 分隔符（角色名之间）
_NAME_SEPS = r'[+\s，,、;；与和及&/｜|~]'
# 片段尾部噪声词（属性名/求和词/疑问词，拆分后需剔除才能匹配角色）
_NAME_TAIL_NOISE = re.compile(
    r'(的|索敌|炮击|雷击|航空|轰炸|鱼雷|命中|幸运|装填|装甲|对空|机动|血量|'
    r'之和|相加|总和|加起来|总共|合计|一共|多少|是|？|\?|！|!|。|，|,|、)+$')


def _extract_multi_ship_names(msg: str, app, max_n: int = 6) -> list:
    """从「丹阳+女灶神的索敌是多少」类文本中提取最多 max_n 个角色正规名。
    策略：① 按分隔符拆分 → 整串匹配 / 去尾部噪声再匹配 / 片段含已知长名；
    ② 兜底：按分隔符提取不足 2 个时，遍历已知名（长名优先）从原文子串抽取。"""
    from .name_service import _normalize
    if not msg or app is None:
        return []
    names, seen = [], set()
    segs = re.split(_NAME_SEPS, msg)
    for part in segs:
        part = (part or '').strip()
        if not part:
            continue
        m = app.name_matcher.match_alias_exact(part)
        if not m:
            p2 = _NAME_TAIL_NOISE.sub('', part).strip()
            if p2 and p2 != part:
                m = app.name_matcher.match_alias_exact(p2)
        if not m:
            # 片段中是否包含某已知角色名（长名优先，避免短名先中）
            for cand in sorted(getattr(app, 'storage', {}).keys(), key=len, reverse=True):
                if cand and cand in part:
                    m = cand
                    break
        if m and m not in seen:
            names.append(m)
            seen.add(m)
        if len(names) >= max_n:
            break
    # 兜底：分隔符提取不足 2 个时，从原文遍历已知名（长名优先）补抽取
    if len(names) < 2:
        norm_msg = _normalize(msg)
        for cand in sorted(getattr(app, 'storage', {}).keys(), key=len, reverse=True):
            nc = _normalize(cand)
            if nc and nc in norm_msg and cand not in seen:
                names.append(cand)
                seen.add(cand)
            if len(names) >= max_n:
                break
    return names[:max_n]


def _is_theme_intro_ask(msg: str, tools=None) -> bool:
    """判定是否为「玩法介绍类」明确意图（用户规则 2026-10-07：游戏简介必须明确相关意图才答）：
    (1) 含主题映射词（拂晓/这游戏/玩法/装备/练级/改造/活动/遭遇战/竞技场/拉古兹/无尽/萌新/新人…）
        且 无具体操作词（怎么打/模拟/伤害/数据…）→ 即「带游戏名/系统名的咨询」；
    (2) 无主语的笼统玩法问讯（纯问游戏怎么玩/好不好玩/是什么玩法，且不含任何角色名）。
    不再用裸「是什么/什么类型」兜底——"是什么类型的装甲"这类角色属性追问不属游戏简介。"""
    if any(k in msg for k in _THEME_INTRO):
        if re.search(r'怎么|攻略|模拟|伤害|推荐|配|打|查|数据|保底|排行|加点|多少|强度|配队|就业', msg):
            return False  # 有具体操作词 → 走对应工具
        return True
    # 明确点名角色（如"介绍一下拜蒙/拜蒙的玩法"）→ 是角色查询，不是游戏简介
    try:
        if tools is not None and _resolve_ship_query(msg, tools):
            return False
    except Exception:
        pass
    # 无主语的笼统玩法问讯（不含任何主题/角色词，纯问游戏怎么玩/好不好玩/是什么玩法）
    return bool(_VAGUE_GAMEPLAY_RE.search(msg))


# 游戏简介实时读取（用户铁律 2026-09-10：游戏简介必须实时读取，不走预构建快照）。
# 源 = 知识库/游戏简介.docx（用户直接编辑）；攻略_md 的 md 与 agent KB chunk 均为管道产物，
# 源更新后会滞后数天，曾把旧 md 的署名尾行「制作组：Felix」一并答给用户。
_GAME_INTRO_CACHE = {'mtime': None, 'text': None}
_GAME_INTRO_JUNK_RE = re.compile(r'^(?:制作组|制作组名单|作者|署名|整理)[:：]')


def _load_game_intro_realtime() -> str:
    """实时读取 知识库/游戏简介.docx → 纯文本（mtime 感知缓存；过滤署名行）。
    docx 即 zip+XML，标准库解析（venv 无 python-docx）；失败返回 ''（调用方回落 KB chunk）。
    注意：本模块顶层无 os/sys，函数内按约定 import os as _os。"""
    import os as _os
    import sys as _sys
    try:
        from tools.knowledge import KB_DIR as _KB_DIR
        path = _os.path.join(_KB_DIR, '游戏简介.docx')
        if not _os.path.isfile(path):
            return ''
        mt = _os.path.getmtime(path)
        if _GAME_INTRO_CACHE['mtime'] == mt and _GAME_INTRO_CACHE['text']:
            return _GAME_INTRO_CACHE['text']
        import zipfile
        import xml.etree.ElementTree as ET
        _NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read('word/document.xml'))
        lines = []
        for p in root.iter(_NS + 'p'):
            t = ''.join(n.text or '' for n in p.iter(_NS + 't')).strip()
            if t and not _GAME_INTRO_JUNK_RE.match(t):
                lines.append(t)
        text = '\n'.join(lines).strip()
        if text:
            _GAME_INTRO_CACHE['mtime'] = mt
            _GAME_INTRO_CACHE['text'] = text
        return text
    except Exception as e:
        # 实时读取失败 → 回落 KB chunk；打一行告警便于定位（曾静默吞掉导致排查困难）
        try:
            print(f'[游戏简介实时读取失败，回落KB] {type(e).__name__}: {e}', file=_sys.stderr)
        except Exception:
            pass
        return ''


def _handle_theme_intro(tools, msg, _bare) -> dict:
    """玩法介绍：按知识库 topic_routing 分类定位主题 → 从该分类 chunks 抽取内容总结
    → 回复玩法说明 + 判定性引导问题；攻略类主题同时打开攻略滑块。"""
    # 主题 → agent kb 的 topic 分类名
    topic_map = {'遭遇战': '遭遇战', '竞技场': 'PVP争锋', '争锋': 'PVP争锋',
                 'jjc': 'PVP争锋', 'jzc': 'PVP争锋',
                 '拉古兹': '拉古兹专题', '改造': '舰灵改造', '活动': '活动玩法',
                 '装备': '装备系统', '练级': '练级攻略', '无尽海域': '无尽海域',
                 '萌新': '萌新入门', '新人': '萌新入门', '拂晓': '游戏概述', '这游戏': '游戏概述'}
    key = _bare if _bare in _THEME_INTRO else None
    if key is None:
        for k in sorted(_THEME_INTRO, key=len, reverse=True):
            if k in msg:
                key = k
                break
    if key is None:
        # 仅「无主语的笼统玩法问讯」才回落游戏概述；其余无明确相关意图的不强行答游戏简介
        if _VAGUE_GAMEPLAY_RE.search(msg):
            key = '拂晓'
        else:
            return None  # 无明确相关意图 → 不答游戏简介，交回上层路由
    # 泛新手词（萌新/新人）被子串命中，但整句其实点名了某个真实舰灵 →
    # 这是具体角色咨询（如「前卫厉害吗？新人自选88元选谁好」），不是泛泛入坑介绍。
    # 交回调用方回落角色/攻略链路，避免"新人"二字把具体问题截胡成萌新入门。
    if key in ('萌新', '新人'):
        _app = _get_app(tools)
        if _app is not None:
            _norm = None
            try:
                from .name_service import _normalize as _norm
            except Exception:
                _norm = None
            _scan = _norm(msg) if _norm else msg
            for _cand in sorted(getattr(_app, 'storage', {}) or {}, key=len, reverse=True):
                _nc = _norm(_cand) if _norm else _cand
                if _nc and len(_nc) >= 2 and _nc in _scan:
                    return None
    query, ask = _THEME_INTRO[key]
    topic = topic_map.get(key, '')
    info = ''
    if topic == '游戏概述':
        # 游戏简介（"拂晓/这游戏是什么游戏"）→ 仅实时直读 docx（用户铁律 2026-09-10）。
        # agent KB 已无游戏简介 chunk（kb_incremental/docx_to_md 源清单同步移除），无快照可回落；
        # docx 缺失/解析失败 → 诚实固定文案，不落 chunk 也不落 kb_search。
        info = _load_game_intro_realtime()
        if not info:
            return {'reply': '游戏简介文档（知识库/游戏简介.docx）暂时读取不到，请确认文件在位后再试。',
                    'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                    'guide_files': [], 'guide_lib': '', 'ok': True}
    else:
        # 从知识库 chunks 按 topic 分类抽取（对应 topic_routing 分类）
        from tools.common import _load_agent_kb
        kb = _load_agent_kb()
        if kb and kb.get('chunks'):
            from tools.knowledge import _pick_diverse_chunks, merge_chunks_dedup
            picked = [c for c in kb['chunks'] if c.get('topic') == topic]
            if not picked and topic:
                picked = [c for c in kb['chunks'] if topic in (c.get('topic') or '')]
            picked = [c for c in picked if (c.get('content') or '').strip()]
            # 按 query 词命中数排序（相关性优先，避免取到"更新公告"等无关片段）
            q_words = [w for w in re.split(r'[\s/、]+', query) if len(w) >= 2]
            def _score(c):
                content = c.get('content') or ''
                return sum(1 for w in q_words if w in content)
            picked.sort(key=_score, reverse=True)
            # 内容差异大的块（避免重复）→ 去标题/元信息 + 行级去重 → 合并
            picked = _pick_diverse_chunks(picked, max_n=2)
            info = merge_chunks_dedup(picked, max_len=520)
    if not info:
        # 兜底：kb_search 自由检索（输出同样过滤图片/案例引用句）
        kb_tool = _tool(tools, 'kb_search')
        if kb_tool:
            from tools.knowledge import _strip_case_refs
            out = kb_tool.call({'query': query, 'max_results': 2})
            info = _strip_case_refs(out if isinstance(out, str) else str(out))
    # 攻略相关主题 → 打开攻略滑块
    lib_map = {'遭遇战': '遭遇战', '竞技场': 'PVP争锋', '争锋': 'PVP争锋',
               '拉古兹': '拉古兹专题', '改造': '舰灵改造', '活动': '活动玩法',
               '装备': '装备系统', '练级': '练级攻略', '无尽海域': '无尽海域',
               '萌新': '萌新入门', '新人': '萌新入门'}
    lib = lib_map.get(key, '')
    _disp = {'jjc': '竞技场', 'jzc': '竞技场'}.get(key, key)
    # 游戏简介 / 新手类 → 除简介外，同时推荐新人相关攻略（打开攻略面板并列出新人类目）
    _newbie_rec = ''
    if key in ('拂晓', '这游戏', '萌新', '新人'):
        lib = '萌新入门'
        _newbie_rec = ('\n\n📘 新人推荐攻略：萌新入门 · 练级攻略 · 装备系统 · 舰灵改造。'
                       '可直接问我「XX攻略」（如「萌新入门攻略」「练级攻略」）查看详情，'
                       '或点开左侧攻略面板浏览。')
    return {'reply': ('关于「' + _disp + '」（分类：' + (topic or '综合') + '）：\n\n'
                      + (info if info else f'知识库「{key}」相关内容如下。') + '\n\n' + ask + _newbie_rec),
            'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
            'guide_files': [], 'guide_lib': lib, 'ok': True}


def _handle_user_ship(tools, msg):
    """处理用户本地副本（装备面板）指令（只接受增量：基础面板+值）：
    「更新面板：名字 炮击+200 雷击-50」增量；「给名字装备 XX 炮击+120」装备式增量；
    「我的设置/装备面板/我的船」查看列表；「删除角色：名字」删除。
    绝对赋值不支持（请到计算模块内使用「炮击2000」赋值）。
    返回固定回复 dict 或 None（无法处理）。"""
    # 删除
    dm = re.search(r'(?:删除角色|移除角色|删除缓存|删除我的船)[：:]\s*([^\s，,]+)', msg)
    if dm:
        tool = _tool(tools, 'remove_user_ship')
        if tool:
            try:
                out = tool.call({'name': dm.group(1)})
            except Exception:
                out = '删除操作暂时出错，稍后再试。'
            text = out if isinstance(out, str) else str(out)
            return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    # 提取角色名：保存/更新类前缀 + 名字；「给XX装备…」单独处理
    m = re.search(r'(?:保存角色|记录角色|我的角色|存角色|更新面板|更新我的|装备面板|我的设置|设置面板|面板增加|给)[：: ]?\s*([^\s，,]+)', msg)
    name = m.group(1) if m else ''
    # 「给XX装备」：跳过装备名，角色名取 给与 装备 之间
    if not name and '给' in msg and '装备' in msg:
        gm = re.search(r'给\s*([^\s，,]+?)\s*装备', msg)
        if gm:
            name = gm.group(1)
    d_atk, d_def = {}, {}
    bare = []
    if name:
        for f in ('炮击', '雷击', '航空', '轰炸', '鱼雷', '索敌', '命中', '幸运', '装填'):
            for mm in re.finditer(f + r'\s*([+\-]?)\s*(\d+(?:\.\d+)?)', msg):
                sign, v = mm.group(1), float(mm.group(2))
                if sign == '+':
                    d_atk[f] = d_atk.get(f, 0.0) + v
                elif sign == '-':
                    d_atk[f] = d_atk.get(f, 0.0) - v
                else:
                    bare.append(mm.group(0).strip())
        for f in ('装甲', '对空', '机动', '血量', '回避率', '防暴击率'):
            for mm in re.finditer(f + r'\s*([+\-]?)\s*(\d+(?:\.\d+)?)', msg):
                sign, v = mm.group(1), float(mm.group(2))
                if sign == '+':
                    d_def[f] = d_def.get(f, 0.0) + v
                elif sign == '-':
                    d_def[f] = d_def.get(f, 0.0) - v
                else:
                    bare.append(mm.group(0).strip())
    if not name:
        # 查看列表
        lst = _tool(tools, 'list_user_ships')
        if lst:
            try:
                out = lst.call({})
            except Exception:
                out = '读取本地副本暂时出错，稍后再试。'
            text = out if isinstance(out, str) else str(out)
            return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
        return None
    # 本地副本只接受增量（基础面板+值）：无符号绝对值 → 报错引导
    if bare:
        return {'reply': '本地副本只接受增量写法（在基础面板上 +值），不支持绝对赋值：'
                         + '、'.join(bare) + '。\n请改为带 +/- 的写法，如「更新面板：' + name + ' 炮击+200」。'
                         '（面板赋值请到计算模块内使用，如「攻击方：名字 炮击2000」）。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '', 'ok': True}
    if not d_atk and not d_def:
        # 只有名字无面板 → 帮助
        return {'reply': '请按格式输入：\n'
                         '· 面板增量：更新面板：名字 炮击+200 雷击-50（在基础面板上累加；结果面板不能为负）\n'
                         '· 装备式增量：给名字装备 XX炮 炮击+120 对空+80\n'
                         '可用字段：炮击/雷击/航空/轰炸/鱼雷/索敌/命中/幸运/装填（攻击）、装甲/对空/机动/血量/回避率/防暴击率（防御）。'
                         '输入「我的设置」查看已修改角色，输入「删除角色：名字」恢复底层默认。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '', 'ok': True}
    tool = _tool(tools, 'save_user_ship')
    if tool is None:
        return None
    try:
        out = tool.call({'name': name, 'panel_delta': {**d_atk, **d_def}})
    except Exception:
        out = '保存操作暂时出错，稍后再试。'
    text = out if isinstance(out, str) else str(out)
    return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


# 偏好/观点闲聊护栏：含「人称 + 喜好/感受动词」且无疑问/查询词的陈述句，
# 一律视为闲聊交回 LLM，避免「我喜欢大雷舰灵」被末级 LLM 意图分析误判为查角色。
_OPINION_RE = re.compile(
    r'(我|俺|咱|我们|咱们|你|你们|他|她|它|它们|大家|指挥官|提督)'
    r'.{0,10}?'
    r'(喜欢|爱|爱上|中意|看中|看重|欣赏|迷|着迷|沉迷|热衷|讨厌|恨|烦|反感|嫌弃|'
    r'觉得|感觉|认为|以为|想|想要|希望|盼|期待|看好|最爱|最中意|最讨厌|最迷)'
)
_QUERY_KW_RE = re.compile(
    r'(?<!检)(?<!考)查|多少|面板|怎么|如何|攻略|配装|配|数据|属性|介绍|是谁|长啥样|长什么样子|'
    r'怎么样|怎样|咋样|详情|资料|技能|天赋|装备|算|伤害|打|攻击|资讯|信息'
    r'|(?:怎么|如何)(?:配|打|玩|算|练|强化|搭配|升级|培养|攻略|计算|办|弄)'
)


def _is_opinion_casual(msg: str) -> bool:
    """判定是否为偏好/观点类闲聊（如「我喜欢大雷舰灵」「我觉得大凤好看」）。
    含「人称 + 喜好/感受动词」且不含任何查询/操作词时返回 True。"""
    if not _OPINION_RE.search(msg):
        return False
    if _QUERY_KW_RE.search(msg):
        return False
    return True


def _is_known_ship(name, app) -> bool:
    """判定 name 是否为已知角色（昵称/别名/精确/模糊均可）。
    用于拦截把整句闲聊当角色名送给查询工具。"""
    if not name or app is None:
        return False
    nm = getattr(app, 'raw_name_matcher', None)
    if nm is not None and nm.match_alias_exact(name):
        return True
    if app.get_ship_from_all(name) is not None:
        return True
    return False


# 违规/禁区输入（违反大模型底层规则）：涉政/违法/色情/暴力/竞品游戏/社区引战等 →
# 统一提示「与阿卡莉无关，不回答」，不展开、不评价。与服务器问题区分：
# 只有网络/API 等连接异常才属于服务器层面（如实说明）。
# 谐音/变体规避（如「元神」=原神、「猿神」「原批」）一律等同本体拦截；
# 硬正则覆盖高频谐音，其余冷门变体由 system_prompt 的禁区规则交 LLM 兜底。
_BANNED_RE = re.compile(
    r'政治|主席|共产党|台湾独立|台独|香港独立|港独|藏独|疆独|法轮功|六四|天安门|'
    r'吸毒|毒品|冰毒|海洛因|赌博|博彩|'
    r'诈骗|洗钱|黑产|黑客|恐怖|砍人|杀人|强奸|'
    r'碧蓝航线|原神|元神|猿神|原批|明日方舟|崩坏|战舰少女|舰娘R|舰队collection|舰队收藏|少女前线|'
    r'贴吧|知乎|微博热搜|抖音热搜'
)

# 未实装舰名表：知名度高但《拂晓》尚未实装的舰灵名（逐一对照 data.json 已实装 202 艘核对，
# 已剔除赤城等已实装舰，以及「最上/胜利/可畏/不挠/凯旋/帝国/罗马/豪」等日常高频词，防误拦）。
# 底层规则：输入命中即直接回复未实装，不调用任何工具、不进计算流程；新舰实装后从表中移除对应词。
_UNIMPLEMENTED_SHIPS = (
    # 日本海军
    '大和', '武藏', '信浓', '三隈', '铃谷', '熊野', '筑摩', '阿贺野', '能代', '矢矧', '酒匂',
    '大鲸', '翔凤', '龙凤', '凤翔', '千岁', '千代田', '夕云', '卷云', '长波', '冲波',
    '冬月', '初月', '若月', '花月',
    # 美国海军
    '衣阿华', '密苏里', '威斯康星', '蒙大拿', '马萨诸塞', '印第安纳', '亚拉巴马', '亚利桑那',
    '宾夕法尼亚', '科罗拉多', '马里兰', '西弗吉尼亚', '北安普顿', '布鲁克林', '菲尼克斯',
    '圣路易斯', '亚特兰大', '朱诺', '胡蜂', '普林斯顿', '得梅因', '巴尔的摩', '旧金山',
    # 英国海军
    '皇家橡树', '英王乔治五世', '巴勒姆', '马来亚', '竞技神', '安森', '怨仇',
    # 德国海军
    '吕佐夫', '斯佩伯爵', '兴登堡',
    # 意大利海军
    '扎拉', '阜姆', '戈里齐亚',
    # 法国海军
    '让巴尔', '让·巴尔', '斯特拉斯堡',
    # 苏联海军
    '苏维埃同盟', '喀琅施塔得', '塔什干', '巴库', '摩尔曼斯克',
    # 中国海军
    '肇和', '海圻',
)
_UNIMPLEMENTED_SHIP_RE = re.compile('|'.join(_UNIMPLEMENTED_SHIPS))

# ---- 能力/功能询问（用户口径 2026-09-09 二次修订：**LLM 意图分类驱动**，
# _llm_intent_classify 加 capability 类；不用正则堆词——正则版漏判「我能输入啥」等口语变体）----
_CAPABILITY_REPLY = (
    '我的功能范围如下，指挥官——\n'
    '1. 查角色：属性/技能/天赋（如「查胡德」「时雨面板」）\n'
    '2. 算伤害：1v1 单体计算，可自己输入 buff 修正（如「胡德改打前卫」）\n'
    '3. 遭遇战模拟：组队打 Boss 时间轴推演（如「模拟遭遇战」）\n'
    '4. 好感度计算器\n'
    '5. 找攻略：练级/推图/配装/机制/资源获取（如「查练级攻略」）\n'
    '6. 我的舰船面板：右侧本地副本，改数值可参与计算\n'
    '7. 杂项：天气、时间、活动日历、礼包提醒\n'
    '闲聊也可以，但工作优先。要做什么直接说。'
)


# ---- 角色分类筛选（用户口径 2026-09-09：舰种/阵营/稀有度/天赋能力 反查角色名单。
# 全部走 app.storage / app.raw_storage 底层真实数据直出，禁止 LLM 编造分类内容）----
_SHIP_TYPE_WORDS = ('驱逐', '轻巡', '重巡', '战巡', '战列', '航空母舰', '航母', '轻母', '装母',
                    '潜艇', '潜舰', '航战', '重炮', '雷巡', '水母', '维修')
_SHIP_TYPE_CANON = {'航空母舰': '航母', '潜舰': '潜艇'}
_NATION_WORDS = ('尤奈特', '八咫', '洛蒙瑞亚', '奥鲁加', '列加杜', '纳谢尔', '凤栖', '沃尔克', '其他')
_RARITY_WORDS = ('彩', '金', '紫', '蓝', '绿', '白')
# 筛选问法词：维度词须与问法词同现才拦截（"驱逐有哪些"/"八咫的角色"），避免误伤普通陈述
_SHIP_FILTER_ASK_RE = re.compile(r'哪些|什么角色|什么舰灵|什么船|角色|舰灵|名单|列表|有谁|给我看看|盘点')
# 操作词排除：含这些词交回原路由（攻略/计算/配装优先，如"八咫阵营怎么配队"）；
# 「对比/比较」等比较语义取自 core.intents 唯一词表——它们属于 compare 意图，
# 不该被当作「属性筛选」（"对比780和太太"不是筛选舰灵名单）。
_SHIP_FILTER_EXCLUDE_RE = re.compile(
    r'怎么|如何|攻略|配装|配队|阵容|打|算|推荐|模拟|伤害|练|' + intents.COMPARE_RE_SRC)


def _get_app_from_tools(tools, agent=None):
    """取 App 实例。route_work 的 agent 参数是 AgentApp（`.app` 才是真正的 App）——故取
    `agent.app`（其余调用点如 `_handle_scout` 也用 `agent.app.xxx`）；兜底从查询工具的 cfg 取。
    注意：绝不能直接 `return agent`——那会返回没有 storage/raw_storage 的 AgentApp，导致筛选遍历 0 艘船、恒空。"""
    if agent is not None:
        return getattr(agent, 'app', None) or agent
    t = _tool(tools, 'query_ship_info')
    return getattr(t, 'cfg', {}).get('app') if t else None


def _ship_talent_names(raw: dict) -> dict:
    """取角色天赋名 → 效果 映射（raw_storage 单角色结构 {等级:{槽位:{name,effect}}}）。"""
    out = {}
    for slots in ((raw or {}).get('talents') or {}).values():
        if isinstance(slots, dict):
            for s in slots.values():
                if isinstance(s, dict) and s.get('name'):
                    out[s['name']] = s.get('effect') or ''
    return out


def _is_ship_attr_filter(msg, tools, agent=None):
    """legacy 路由用：消息能否解析出属性/天赋筛选维度（舰种/尺寸/阵营/稀有度/改造状态/天赋）。
    返回 True → 应走 ship_filter（而非技能效果筛选 query_ships_by_skill）。
    技能效果查询（护盾/自奶/炮击/雷击…）无属性维度 → False，仍走 query_ships_by_skill。"""
    app = _get_app_from_tools(tools, agent)
    if app is None:
        return False
    try:
        from tools.knowledge import _parse_filter_conditions
        return _parse_filter_conditions(msg, app) is not None
    except Exception:
        return False


def _handle_ship_filter(tools, msg, agent=None, overrides=None):
    """角色分类筛选（属性组合反查舰灵名单）。底层真实数据直出 + 完整性门控；确定性结构化输出。
    支持**追问继承**：上一句筛完后只说「紫的」这类增量时，叠加到上次条件（overrides['_last_ship_filter']）。"""
    m = (msg or '').strip()
    if not m or len(m) > 40:
        return None
    if _SHIP_FILTER_EXCLUDE_RE.search(m):
        return None
    # 元请求（只说「筛选」未带维度）→ 维度引导，不编造
    if re.search(r'(?:舰种|阵营|国籍|稀有度|种族|天赋|能力)筛选'
                r'|筛选.{0,4}(?:舰种|阵营|国籍|稀有度|种族|天赋|能力)'
                r'|按.{0,4}(?:舰种|阵营|国籍|稀有度)筛选', m) and len(m) <= 16:
        return {'reply': (
            '《舰灵筛选 · 请指定维度》\n'
            '底层舰库收录 202 名角色，可直接这样说：\n'
            '1. 舰种：驱逐有哪些 / 航母角色 / 潜艇列表\n'
            '2. 阵营：八咫的角色 / 尤奈特舰灵列表\n'
            '3. 稀有度：金角色有哪些 / 稀有度彩的舰灵（支持多重：蓝白绿）\n'
            '4. 是否改造：没改造的驱逐 / 改造航母\n'
            '5. 天赋能力：有哪些角色有大型特攻 / 会节能科技的角色\n'
            '6. 尺寸：蓝白绿小船有哪些 / 中船有哪些 / 大船列表\n'
            '7. 组合：八咫的航母 / 金色驱逐 / 蓝白绿的驱逐'),
            'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    app = _get_app_from_tools(tools, agent)
    storage = getattr(app, 'storage', None)
    if not storage:
        return None
    # 含具体舰名 → 交回角色查询（避免"时雨会什么技能"被误拦）
    from .name_service import _normalize
    norm_msg = _normalize(m)
    for name in storage.keys():
        if name and (name in m or (len(name) >= 2 and _normalize(name) in norm_msg)):
            return None
    # 维度提取 + 追问继承（上一句筛完，本句只说「紫的」这类增量 → 叠加到上次条件）
    from tools.knowledge import _resolve_filter_conds, _ship_filter_text_from_conds
    last = (overrides or {}).get('_last_ship_filter')
    conds, merged = _resolve_filter_conds(m, app, last)
    if conds is None:
        return None
    dims = sum(1 for k in ('stype', 'nation', 'refit', 'size') if conds.get(k))
    if conds.get('rarities'):
        dims += 1
    if conds.get('talents'):
        dims += 1
    if conds.get('refittable'):
        dims += 1
    # 拦截判定：≥2 维度组合 或 含问法词 或 追问继承 才拦，防误伤单维度陈述
    if not (dims >= 2 or merged or _SHIP_FILTER_ASK_RE.search(m)):
        return None
    text = _ship_filter_text_from_conds(app, conds)
    if not text:
        return None
    if overrides is not None:  # 记住本次条件，供下一句追问继承
        overrides['_last_ship_filter'] = conds
    return {'reply': text, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


# ---- 按舰船集抽取「属性」确定性查询（CD / 技能性质）----
# 修复：原「金色驱逐的必杀技多少秒」只回了名单、且追问「他们的必杀技cd」没继承上一批筛选、
#       落到 RAG 拼出无关舰船列表（用户 2026-09-16 反馈）。
# 用户 2026-09-16 续：必杀技CD 这类属性要与「技能性质（回血/护盾…）」统一处理，并可自主查询+分析分类。
# 本处理器确定性抽属性、绝不走 LLM、绝不编造。
# 属性族：① cd（必杀技/通常技/支援技 CD）；② nature（技能性质，如 回血/护盾/自奶/炮击…，含分析分类）。
_SHIP_ATTR_SKILL_RE = re.compile(r'(必杀技|必杀|大招|通常技|支援技|旗舰技)')
# 注意：\bcd\b 在 CJK 相邻时失效（中文属 \w，与 c 之间无单词边界），故 cd 用「前后非拉丁字母」约束
_SHIP_ATTR_CD_RE = re.compile(r'(冷却时间|冷却|(?<![a-zA-Z])cd(?![a-zA-Z])|多少秒|几秒)', re.I)
_SHIP_PRONOUN_RE = re.compile(r'他们|她们|它们|这些|那些|这批|那批|上面的|之前的|刚才的|上一批|之前筛|刚才筛|这十|那十|这几种|那几种')
# 性质类问法的「列举/归属」信号词（避免把「X的炮击伤害」这类伤害/计算问法误判为性质列举）
_NATURE_LIST_RE = re.compile(r'哪些|哪艘|哪条|哪些船|哪些舰|列表|列举|有什么|有哪些|带|会\b|具备|归类|分类|性质|技能性质|技能类型|技能效果|技能解析')
_SHIP_NATURE_GENERIC_RE = re.compile(r'技能性质|技能分类|技能类型|技能效果|技能归类|技能解析')
# 技能类型 → raw_data skills 键
_SKILL_KEY = {
    '必杀技': 'BattleSkill2',
    '通常技': 'BattleSkill1',
    '支援技': 'SupportSkill',
    '旗舰技': 'LeaderSkill',
}
_SKILL_LABEL_KEYS = [('通常技', 'BattleSkill1'), ('必杀技', 'BattleSkill2'),
                     ('旗舰技', 'LeaderSkill'), ('支援技', 'SupportSkill')]


def _extract_skill_cd(app, name, kind):
    """取某舰灵指定技能类型的 CD（秒）。必杀技读「冷却时间XX秒」，通常技/支援技读「每XX秒」，
    兜底抓「XX秒」。取不到返回 None（交回上层，不编造）。"""
    _, raw = app.get_raw_ship(name)
    if not raw:
        return None
    key = _SKILL_KEY.get(kind, 'BattleSkill2')
    text = (raw.get('skills', {}) or {}).get(key, '') or ''
    if kind == '必杀技':
        m = re.search(r'冷却时间\s*(\d+)', text) or re.search(r'(\d+)\s*秒', text)
    else:
        m = re.search(r'每\s*(\d+)\s*秒', text) or re.search(r'冷却时间\s*(\d+)', text) \
            or re.search(r'(\d+)\s*秒', text)
    return int(m.group(1)) if m else None


def _classify_ship_natures(app, name):
    """按权威 _BUFF_PATTERNS 分类某舰灵四技能的性质。返回 {性质: [技能标签,...]}。
    性质判定与 query_ships_by_skill 同源（tools.knowledge._BUFF_PATTERNS），避免两套语义漂移。"""
    from tools.knowledge import _BUFF_PATTERNS
    _, raw = app.get_raw_ship(name)
    if not raw:
        return {}
    skills = (raw.get('skills', {}) or {})
    out = {}
    for nat, pats in _BUFF_PATTERNS.items():
        labels = []
        for label, key in _SKILL_LABEL_KEYS:
            text = skills.get(key, '') or ''
            if any(re.search(p, text) for p in pats):
                labels.append(label)
        if labels:
            out[nat] = labels
    return out


def _detect_nature(msg):
    """从消息里识别具体技能性质（护盾/回血/自奶/炮击…），返回性质名或 None。
    用 _BUFF_ALIASES 别名表匹配，与 query_ships_by_skill 同源。"""
    from tools.knowledge import _BUFF_ALIASES
    for nat, aliases in _BUFF_ALIASES.items():
        if any(a in msg for a in aliases):
            return nat
    return None


def _is_ship_attr_query(msg):
    """是否「按舰船集查某属性」类问法。返回 (is_q, kind, attr_type)。
    attr_type='cd' → kind∈{必杀技,通常技,支援技}；attr_type='nature' → kind=性质名 或 '__all__'（仅说技能性质未指定具体性质）。
    注意：对性质类，要求带「列举/归属」信号词（或明说技能性质），避免把伤害/计算问法误判为性质列举——
    具体且无疑问的「X的炮击伤害」类问法不在此拦截，交回计算/攻略路由。无舰船集范围的性质问法（如「哪些船有护盾」）
    也返回 False，交回成熟的 query_ships_by_skill 全量反查。"""
    m = (msg or '').strip()
    if not m or len(m) > 60:
        return False, None, None
    # CD 类
    if _SHIP_ATTR_CD_RE.search(m):
        km = _SHIP_ATTR_SKILL_RE.search(m)
        kind = '必杀技' if not km else km.group(1)
        if kind in ('必杀', '大招'):   # 口语归一化
            kind = '必杀技'
        return True, kind, 'cd'
    # 技能性质类：需带列举/归属信号，或明说「技能性质/分类」
    if _SHIP_NATURE_GENERIC_RE.search(m):
        return True, '__all__', 'nature'
    nat = _detect_nature(m)
    if nat and _NATURE_LIST_RE.search(m):
        return True, nat, 'nature'
    return False, None, None


def _handle_ship_attr_query(tools, msg, agent=None, overrides=None):
    """确定性返回「一组舰灵的某属性」。绝不走 LLM、绝不编造。
    属性族：cd（技能CD）/ nature（技能性质，含分析分类）。
    舰船集来源优先级：① 消息自带属性维度（舰种/稀有度…）→ ship_filter 条件；
    ② 代词（他们/这些…）+ overrides['_last_ship_filter'] → 继承上一批筛选；
    ③ 消息里出现的具体舰名。三者皆无 → 返回 None 交回上层路由（全量性质反查回退 query_ships_by_skill）。
    命中维度解析时同步写回 _last_ship_filter，供后续追问继承。"""
    m = (msg or '').strip()
    is_q, kind, attr_type = _is_ship_attr_query(m)
    if not is_q:
        return None
    app = _get_app_from_tools(tools, agent)
    storage = getattr(app, 'storage', None)
    if not storage:
        return None
    from tools.knowledge import (_resolve_filter_conds, _run_ship_filter,
                                 _filter_cond_desc)
    last = (overrides or {}).get('_last_ship_filter')
    has_dims = _is_ship_attr_filter(m, tools, agent)
    rows = []
    cond_desc = ''
    if has_dims:
        conds, _ = _resolve_filter_conds(m, app, last)
        if conds is None:
            return None
        rows = _run_ship_filter(app, conds)
        cond_desc = _filter_cond_desc(conds)
        if overrides is not None:
            overrides['_last_ship_filter'] = conds
    elif last and _SHIP_PRONOUN_RE.search(m):
        rows = _run_ship_filter(app, last)
        cond_desc = _filter_cond_desc(last) + '（继承上一批筛选）'
    else:
        # 解析消息中出现的已知舰名
        from .name_service import _normalize
        norm_msg = _normalize(m)
        names = []
        for nm in storage.keys():
            if not nm:
                continue
            if nm in m or (len(nm) >= 2 and _normalize(nm) in norm_msg):
                names.append(nm)
        if not names:
            return None
        for nm in names:
            if not app.is_ship_complete(nm):
                continue
            ship = storage.get(nm)
            if ship is not None:
                na = getattr(ship, 'nation', '') or ''
                st = getattr(ship, 'ship_type', '') or ''
                ra = getattr(ship, 'rarity', '') or ''
            else:
                _, raw = app.get_raw_ship(nm)
                if not raw:
                    continue
                na = raw.get('所属', '') or raw.get('nation', '')
                st = raw.get('舰种', '') or raw.get('ship_type', '')
                ra = raw.get('稀有', '') or raw.get('rarity', '')
            rows.append((nm, na, st, ra))
        cond_desc = '指定舰灵'
    if not rows:
        return None

    if attr_type == 'cd':
        lines = ['《舰灵%sCD · %s》' % (kind, cond_desc)]
        lines.append('共 %d 名：' % len(rows))
        for i, (name, na, st, ra) in enumerate(rows[:60], 1):
            cd = _extract_skill_cd(app, name, kind)
            cdstr = ('%d秒' % cd) if cd is not None else '（数据缺失）'
            lines.append('%d. %s（%s · %s · %s）：%sCD %s' % (i, name, na, st, ra, kind, cdstr))
        if len(rows) > 60:
            lines.append('……等共 %d 名。' % len(rows))
        return {'reply': '\n'.join(lines), 'tool_outputs': [], 'suggestions': [],
                'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # attr_type == 'nature' —— 分析分类
    if kind == '__all__':
        # 通用分析：逐舰列举其全部技能性质，并附分类汇总
        lines = ['《舰灵技能性质 · %s》' % cond_desc]
        lines.append('共 %d 名（逐舰按性质归类）：' % len(rows))
        summ = {}
        for i, (name, na, st, ra) in enumerate(rows[:60], 1):
            nat_map = _classify_ship_natures(app, name)
            tags = '、'.join('%s(%s)' % (n, '/'.join(l)) for n, l in nat_map.items()) or '（无显著性质）'
            lines.append('%d. %s（%s · %s · %s）：%s' % (i, name, na, st, ra, tags))
            for n in nat_map:
                summ[n] = summ.get(n, 0) + 1
        if summ:
            order = sorted(summ.items(), key=lambda kv: -kv[1])
            lines.append('分类汇总：' + '，'.join('%s %d 艘' % (n, c) for n, c in order))
        if len(rows) > 60:
            lines.append('……等共 %d 名。' % len(rows))
        return {'reply': '\n'.join(lines), 'tool_outputs': [], 'suggestions': [],
                'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 具体性质：在舰船集内筛出具备该性质的舰灵，并标注由哪个技能承载（分析分类）
    lines = ['《舰灵技能性质 · %s · %s》' % (kind, cond_desc)]
    hit = []
    for name, na, st, ra in rows:
        nat_map = _classify_ship_natures(app, name)
        if kind in nat_map:
            hit.append((name, na, st, ra, '/'.join(nat_map[kind])))
    if not hit:
        lines.append('该范围内没有检索到「%s」性质的舰灵（不编造）。' % kind)
    else:
        lines.append('共 %d 名具备「%s」：' % (len(hit), kind))
        for i, (name, na, st, ra, sk) in enumerate(hit[:60], 1):
            lines.append('%d. %s（%s · %s · %s）：%s' % (i, name, na, st, ra, sk))
        if len(hit) > 60:
            lines.append('……等共 %d 名。' % len(hit))
    return {'reply': '\n'.join(lines), 'tool_outputs': [], 'suggestions': [],
            'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


def _pvp_form_reply():
    """模拟组队 空表单引导（6 人 + 旗舰 + 防守角色）。供 争锋竞技场/模拟组队 伤害意图复用。

    文案只做一句话引导：规则细节（不分前后排 / 不算支援技 / 无需 buff / 黑夜自动 = 120−白天）
    与填写说明统一由前端表单提示承载 —— 避免气泡与表单两处重复同一段话（用户口径 2026-09-23）。"""
    return {'reply': '指挥官，模拟组队。请填写防守角色、白天时长与 6 人名单（含 1 名旗舰）：',
            'tool_outputs': [], 'suggestions': [],
            'fill_form': [], 'encounter_form': [
                {'key': 'defender', 'label': '防守角色', 'placeholder': '角色名', 'required': True, 'value': ''},
                {'key': 'day_time', 'label': '战斗时长·白天', 'placeholder': '秒（0~120，黑夜 = 120 - 白天）',
                 'required': True, 'value': '60'},
                {'key': 'front1', 'label': '角色1', 'placeholder': '角色名', 'required': True, 'value': ''},
                {'key': 'front2', 'label': '角色2', 'placeholder': '角色名', 'required': True, 'value': ''},
                {'key': 'front3', 'label': '角色3', 'placeholder': '角色名', 'required': True, 'value': ''},
                {'key': 'back1', 'label': '角色4', 'placeholder': '角色名', 'required': True, 'value': ''},
                {'key': 'back2', 'label': '角色5', 'placeholder': '角色名', 'required': True, 'value': ''},
                {'key': 'back3', 'label': '角色6', 'placeholder': '角色名', 'required': True, 'value': ''},
            ], 'guide_files': [], 'guide_lib': '', 'ok': True}


def _common_equip_table_reply():
    """具体装备查询：停用 query_equipment 工具，改为直接给「常用装备速览」表
    （装备速查表 PNG，guide_014）。route_work 结果直返前端，攻略文档走 guide_files 字段
    （前端渲染窗口卡片），标记不内联在 reply 文本里。"""
    _EQUIP_TABLE_PATH = '攻略/5 立香也能看懂的一图流攻略合集/5-拂晓：胜利之刻-装备速查表20260712高清原图.png'
    return {'reply': '这是常用装备速览表，指挥官对照即可；想看哪类装备的搭配思路，直接问「XX配装」即可。',
            'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [],
            'guide_files': [{'path': _EQUIP_TABLE_PATH, 'title': '常用装备速览',
                             'summary': '装备速查表（一图流）：按类型分类，以颜色边框标识推荐程度'
                                        '（必做/推荐/可选/特殊用途），涵盖主炮、副炮、鱼雷、舰载机、防空炮、设备等。'}],
            'guide_lib': '', 'ok': True}


_REFIT_ASK_RE = re.compile(
    r'改造|改如何|改后|改了|改完|改满|能不能改|能改吗|可以改|可改造|可以改造|'
    r'值得改造|有没有改造|能改造吗|改强吗|改造强|改造厉害|改造怎么样|改造好不好|改造后强|改造后')
def _is_refit_ask(msg: str) -> bool:
    """消息是否在问某角色的改造（仅在已点名具体角色名的上下文中调用；泛化攻略问法不含角色名）。"""
    return bool(_REFIT_ASK_RE.search(msg or ''))


def _is_ship_list_cmd(msg: str) -> bool:
    """是否「查角色/角色列表/所有角色/有哪些舰灵」类角色名录裸查询。"""
    return bool(_SHIP_LIST_RE.match(re.sub(r'[\s。.!！?？~～,，]', '', msg or '')))


def _ship_panel_reply(canon: str, tools):
    """角色查询统一出口：改为前端「查角色」面板展开（不再把面板/技能底层文本塞进对话回复）。

    - query_ship_info 未注册 → 返回 None（交回上层其它分支，保留路由降级语义）；
    - 未找到 / 查询出错 → 保留诚实文本回复，不弹面板；
    - 命中 → 短句回复 + ship_browser/ship_focus 标记，前端打开角色面板并定位该角色。
    用户口径 2026-09-13：查询角色走与开发者端同款「名单 + 阵营筛选 + 头像」界面。
    """
    text = _query_text(tools, 'query_ship_info', {'ship_name': canon})
    if text is None:
        return None
    if text.startswith('未找到角色') or text.startswith('查询暂时出错') or text.startswith('我暂时没查到'):
        return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    return {'reply': f'《{canon}》的资料已展开，在面板里查看即可。',
            'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
            'ship_browser': True, 'ship_focus': canon}


# ============ 每日事务（今日工作/事务/待办）============
# 后端固定输出，不走 LLM：随机抽 3 项，活动必选（活动名读自「游戏内活动」表格
# 知识库/活动/活动日历.xlsx；表内只有游戏内活动，为空则显示「暂无」）；
# 不再追问「优先处理哪一项」，改为「我会辅助你处理」（用户口径 2026-09-14）。
# 注意：法定节假日（真实日历，知识库/活动/节假日.json）的「提醒和祝福」不在本函数处理——
# 改为在 GET / 路由的「会话打开初始问候气泡」中按日期追加（用户口径 2026-09-14 续），与每日事务区分。
_DAILY_WORK_ITEMS = ["舰灵培养", "港区建设", "邮件处理", "扫荡拉古兹", "舰队远征", "海域巡逻"]
_DAILY_WORK_RE = re.compile(
    r'(?:今天|今日|每天|每日|这会儿|现在)(?:有)?(?:哪些|什么|啥|哪)?\s*'
    r'(?:工作|事务|待办|安排|任务)'
    r'|(?:每日|今日|今天)\s*事务'
    r'|(?:工作|事务|待办)\s*(?:安排|清单|有哪些|有什么)'
    r'|(?:该做|要做)\s*(?:哪些|什么|啥|哪)?\s*(?:工作|事务|任务|事)'
)


def _current_event_name() -> str:
    """读取当前「游戏内活动」名：优先进行中，否则最近 upcoming；无则空串。

    数据源＝「游戏内活动」表格（知识库/活动/活动日历.xlsx，load_events_normalized）；
    法定节假日属「真实日历」，不在本入口，由 _today_holiday() 单独处理，
    避免每日事务的「活动」项把节假日错误当成游戏活动。
    """
    try:
        from tools.knowledge import (load_events_normalized, _parse_event_date,
                                     _event_status)
        import datetime as _dt
        events = [e for e in load_events_normalized()
                  if e.get('name') and (e.get('type') or '') != '法定节假日']
        if not events:
            return ''
        now = _dt.datetime.now()
        today = now.date()
        # 进行中优先；活动期已结束但兑换期未结束（兑换中）也算「当前活动」
        # 传 now：截止日当天上午 10:00 之后即视为已截止，不再算进行中（口径与提醒一致）
        active = [e for e in events if _event_status(e, today, now) in ('进行中', '兑换中')]
        if active:
            return str(active[0].get('name') or '').strip()
        upcoming = []
        for e in events:
            s = _parse_event_date(e.get('start'))
            if s and s >= today:
                upcoming.append((s, e))
        if upcoming:
            upcoming.sort(key=lambda x: x[0])
            return str(upcoming[0][1].get('name') or '').strip()
        return ''
    except Exception:
        return ''


# ---- 【当期活动攻略】统一话术（用户口径 2026-09-23）----
# 触发必须来自活动日历：① 消息点到日历中当期（进行中/兑换中）活动名或其简称；
# ② 消息用「当期/本期/本次/这次/当前/最新… ＋ 活动」明确指代当期活动。
# 两者都还要带攻略类问法词。**不得凭「活动/复刻」字样匹配**——「宾果活动」
# 「七日登录活动（白枫糖果）」这两个活动本名就含「活动」二字，按字样匹配会误伤。
_ACTIVITY_GUIDE_REPLY = '关注拂晓qq频道凤栖攻略组，会有详细攻略。'
_ACTIVITY_GUIDE_WORD_RE = re.compile(r'攻略|打法|怎么打|如何打|怎么过|怎么玩|怎么推|推图|通关|配队|阵容|带路')
# 当期活动的明确指代：限定词必须紧邻「活动」二字，裸「活动」不触发
_ACTIVITY_CURRENT_RE = re.compile(r'(?:当期|本期|本次|这次|此次|当前|最新|最近|近期|这期|新)的?活动')
# 拆名所得但本身不代表具体活动的段，不作简称
_ACTIVITY_NAME_STOP = frozenset(('活动', '复刻', '限时活动', '登录活动'))


def _activity_name_hit(name: str, msg: str) -> bool:
    """消息是否点到了该活动名：全名 / 分隔符拆出的简称段（≥3字）/ 长名前3字。"""
    name = (name or '').strip()
    if len(name) < 2:
        return False
    if name in msg:
        return True
    for seg in re.split(r'[（）()【】\[\]·、,，\-—\s]+', name):
        seg = seg.strip()
        if len(seg) >= 3 and seg not in _ACTIVITY_NAME_STOP and seg in msg:
            return True
    # 长名取前 3 字作简称（「博览会的奇妙相遇」→「博览会」）；短名不取，防误伤
    return len(name) >= 5 and name[:3] in msg


def _activity_guide_reply(msg: str):
    """当期活动 + 攻略类问法 → 统一引流话术；否则 None（交回原路由）。

    活动名与当期口径同 _current_event_name：进行中/兑换中，截止日当天 10:00 后不再算当期。"""
    m = (msg or '').strip()
    if not m or not _ACTIVITY_GUIDE_WORD_RE.search(m):
        return None
    if _ACTIVITY_CURRENT_RE.search(m):
        return _ACTIVITY_GUIDE_REPLY
    import datetime as _dt
    from tools.knowledge import load_events_normalized, _event_status
    now = _dt.datetime.now()
    today = now.date()
    for ev in load_events_normalized():
        if _event_status(ev, today, now) not in ('进行中', '兑换中'):
            continue
        if _activity_name_hit(ev.get('name'), m):
            return _ACTIVITY_GUIDE_REPLY
    return None


def _today_holiday() -> dict:
    """今天若是法定节假日，返回事件 dict（含 greeting 祝福语 / summary），否则 None。

    节假日判定改由「联网读取实际中国法定日历」（tools.holiday_cn.statutory_holiday，
    用户口径 2026-09-14），知识库 节假日.json（真实日历）仅作：① 联网失败时的兜底；
    ② 提供阿卡莉口吻祝福语文本（按名匹配）。联网明确非节假日时即便知识库有旧数据也不误报。

    与「游戏内活动」区分：法定节假日走「节假日提醒和祝福」单独块，不进入每日事务的「活动」项。
    """
    try:
        from tools.holiday_cn import statutory_holiday, holiday_greeting
        import datetime as _dt
        h = statutory_holiday(_dt.date.today())
        if not h.get('is_holiday'):
            return None
        name = h.get('name') or ''
        g = holiday_greeting(name)
        return {'name': name, 'greeting': g['greeting'], 'summary': g['summary'],
                'opening': g.get('opening') or ''}
    except Exception:
        return None


def _handle_daily_work() -> dict:
    import random
    event_name = _current_event_name()
    # 活动项只读「游戏内活动」表格（不含法定节假日）；无进行中的游戏活动则标注「暂无」
    activity = "活动：" + (event_name if event_name else "（暂无进行中的游戏活动）")
    pool = list(_DAILY_WORK_ITEMS)
    random.shuffle(pool)
    picks = pool[:2]
    items = [activity] + picks
    random.shuffle(items)
    lines = "\n".join("· %s" % it for it in items)
    reply = ("今日事务已为你排好，我会辅助你处理：\n%s\n"
             "（直接告诉我要先处理哪一项即可。）" % lines)
    return {'reply': reply, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


# ============ 索敌计算（用户口径 2026-09-14）============
# 两种模式：
#  ① 输入指定数值 → 算 T优 / 不T劣 索敌线 + 航向判定与 buff 说明；
#  ② 选 6 个角色 → 各自「基础索敌×(1+各组索敌天赋%之和) + 98×2 + 算术额外」×乘算系数，
#     求和后下取整。支持每角色「算术额外」（如 14*3+10）与「乘算系数」（默认 1.0）。
#     天赋口径（用户 2026-09-15）：1/2/3/4 每组各可选 1 个天赋，各组内取索敌%最高，
#     四组合计——不是只取全舰最高一个（14 艘舰在 ≥2 个组都有索敌天赋，单最高会少算）。
_SCOUT_MODE1_RE = re.compile(r'(?:索敌|侦察)(?:线|值|判定|计算|估算)?\s*[:：]?\s*(\d+(?:\.\d+)?)')
_SCOUT_MODE2_RE = re.compile(
    r'(?:索敌|侦察)(?:队伍|编队|阵容|六|6\s*个|最高索敌|计算\s*(?:队伍|六|6)|六角色|最高)')


def _safe_arith(s: str):
    """安全算术求值：仅允许数字与 + - * / ( )，拒绝其他字符（防 eval 注入）。"""
    if not s:
        return 0.0
    s = s.strip()
    if not s:
        return 0.0
    if not re.fullmatch(r'[\d\.\+\-\*\/\(\)\s]+', s):
        raise ValueError('算术表达式含非法字符：%s' % s)
    import ast
    node = ast.parse(s, mode='eval')

    def _ev(n):
        if isinstance(n, ast.Expression):
            return _ev(n.body)
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -_ev(n.operand)
        if isinstance(n, ast.BinOp):
            a, b = _ev(n.left), _ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
            raise ValueError('不支持的运算符')
        raise ValueError('不支持的表达式')
    return float(_ev(node))


def _talent_scout_pct(effect: str) -> float:
    """从单条天赋 effect 文本抽取「索敌」百分比。

    模板规律（140 条实测）：
      - 索敌在句首（如「索敌及命中值提升 20% 10%」）→ 取第一个数；
      - 索敌在第二位（如「对空及索敌提升 15% 15%」「被攻击伤害下降及索敌提升 5% 15%」）→ 取第二个数。

    组间取舍由调用方决定（索敌 Mode2：1/2/3/4 每组各选 1 个，组内 A/B 取高，四组求和）。"""
    if '索敌' not in (effect or ''):
        return 0.0
    nums = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)\s*%', effect)]
    if not nums:
        return 0.0
    if (effect or '').startswith('索敌'):
        return nums[0]
    return nums[1] if len(nums) > 1 else nums[0]


def _parse_scout_segment(seg: str):
    """解析单角色片段：name [算术额外] [乘算系数]。
    返回 (name, arith_str, mult)。规则：
      - 名之后整段（去掉末尾的浮点系数）拼接为算术表达式（支持带/不带空格，
        如「14*3+10」「14 * 3 + 10」均解析为 14*3+10）；
      - 末尾裸浮点 token（含小数点，如 1.1）→ 乘算系数（默认 1.0）；
      - 末尾裸整数（如 98）→ 视为额外索敌（并入算术求值，+98）。
    名可紧贴算术（如「胡德·改14*3+10」）。"""
    seg = (seg or '').strip()
    if not seg:
        return None
    parts = seg.split()
    if not parts:
        return None
    first = parts[0]
    op_idx = min([i for i, c in enumerate(first) if c in '+-*/('] + [len(first)])
    name = first[:op_idx]
    rest_of_first = first[op_idx:]
    # 名紧贴算术时（如「胡德·改14*3+10」），首运算符前的尾随数字实为算术开头，
    # 需从名中剥离还给算术，避免把「14」误吞进舰名。
    if rest_of_first and rest_of_first[0] in '+-*/(':
        _md = re.search(r'\d+$', name)
        if _md:
            name = name[:_md.start()]
            rest_of_first = _md.group(0) + rest_of_first
    rest_tokens = []
    if rest_of_first:
        rest_tokens.append(rest_of_first)
    rest_tokens.extend(parts[1:])
    mult = 1.0
    if rest_tokens and re.fullmatch(r'\d+\.\d+', rest_tokens[-1]):
        mult = float(rest_tokens[-1])   # 末尾浮点 → 乘算系数
        rest_tokens = rest_tokens[:-1]
    arith_str = ''.join(rest_tokens)     # 其余整段即算术表达式
    return name, arith_str, mult


def _scout_char_breakdown(name, arith_str, mult, agent):
    """计算单角色索敌明细。依赖 agent.app.raw_storage / is_ship_complete。

    天赋口径（用户 2026-09-15）：**1/2/3/4 每组各可选 1 个天赋**，各组内取索敌%
    最高的那个（A/B 二选一），四组合计为该角色的索敌天赋加成——不是只取全舰最高一个。
    （数据实测：14 艘舰在 ≥2 个组都有索敌%天赋，只取单最高会少算。）"""
    rec = (agent.app.raw_storage or {}).get(name) if agent else None
    panel = (rec or {}).get('panel') or {}
    # 数值类属性（血量/炮击/索敌…）存于 panel['面板'] 内层；稀有/舰种等在外层。
    # 两层都兜底读取，避免取到 0。
    inner = panel.get('面板') if isinstance(panel, dict) else None
    stat_panel = inner if isinstance(inner, dict) else panel
    base = 0.0
    try:
        base = float((stat_panel or {}).get('索敌') or 0)
    except (TypeError, ValueError):
        base = 0.0
    tl = (rec or {}).get('talents') or {}
    picks = []          # [(组号, 天赋名, pct)]：每组各选 1 个索敌天赋（组内 A/B 取高）
    pct_sum = 0.0
    if isinstance(tl, dict):
        for g in ('1', '2', '3', '4'):
            opts = tl.get(g)
            if not isinstance(opts, dict):
                continue
            g_best, g_name = 0.0, ''
            for _k, o in opts.items():
                if not isinstance(o, dict):
                    continue
                p = _talent_scout_pct(o.get('effect') or '')
                if p > g_best:
                    g_best, g_name = p, o.get('name') or ''
            if g_best > 0:
                picks.append((g, g_name, g_best))
                pct_sum += g_best
    extra = 0.0
    extra_expr = ''
    if arith_str:
        try:
            extra = _safe_arith(arith_str)
            extra_expr = arith_str
        except Exception:
            extra_expr = arith_str + '（表达式无效，按0计）'
    # 用户输入钳制：算术额外与乘算系数不得为负、不超过 20000（用户口径 2026-09-17）
    extra = max(0.0, min(20000.0, float(extra)))
    mult = max(0.0, min(20000.0, float(mult)))
    inner = base * (1 + pct_sum / 100.0) + 196.0 + extra
    val = inner * mult
    return {
        'name': name, 'base': base, 'pct_sum': pct_sum, 'picks': picks,
        'best_pct': pct_sum, 'best_name': '；'.join('%s组·%s' % (g, n) for g, n, _p in picks) or '无',
        'extra': extra, 'extra_expr': extra_expr, 'mult': mult, 'val': val,
    }


def _handle_scout_mode1(v: float) -> dict:
    """Mode1：输入我方索敌基准值 V（始终视为我方，用户口径 2026-09-27），给出两类 T 优/T 劣 线
    （不列具体航向 buff）。

    判定口径（用户口径 2026-09-29）：**±25% 一律判在「防守方」的索敌上**——
      攻击方 T优 ⟺ 攻击方索敌 > 防守方索敌×1.25；攻击方 T劣 ⟺ 攻击方索敌 < 防守方索敌×0.75。
    ① 我方进攻/敌方防守（防守方=敌方，未知）→ 解出敌方阈值：
       我方 T优 ⟺ 敌方 < 我方÷1.25；我方 T劣 ⟺ 敌方 > 我方÷0.75。
    ② 敌方进攻/我方防守（防守方=**我方**，已知）→ 阈值直接取我方×0.25 带宽：
       敌方 T优 ⟺ 敌方 > 我方×1.25；敌方 T劣 ⟺ 敌方 < 我方×0.75。
    （修正前 ② 误复用 ① 的 ÷1.25/÷0.75，把 25% 判在了错的基准上。）"""
    v = max(0.0, min(20000.0, float(v)))   # 用户输入钳制：不得为负、≤20000（用户口径 2026-09-17）
    t_you_e = v / 1.25         # ① 我方 T优：敌方索敌需 < 此值（防守方=敌方，反解）
    t_li_e = v / 0.75          # ① 我方 T劣：敌方索敌需 > 此值
    t_you_a = v * 1.25         # ② 敌方 T优：敌方索敌需 > 此值（防守方=我方）
    t_li_a = v * 0.75          # ② 敌方 T劣：敌方索敌需 < 此值
    lines = []
    lines.append('《索敌线计算》')
    lines.append('基准索敌（我方）：%s' % _fmt_num(v))
    lines.append('')
    lines.append('· 我方进攻 / 敌方防守')
    lines.append('  T优（我方）：敌方索敌 < %s（即 我方 > 敌方×1.25）' % _fmt_num(t_you_e))
    lines.append('  T劣（我方）：敌方索敌 > %s（即 我方 < 敌方×0.75）' % _fmt_num(t_li_e))
    lines.append('')
    lines.append('· 敌方进攻 / 我方防守')
    lines.append('  T优（敌方）：敌方索敌 > %s（即 敌方 > 我方×1.25）' % _fmt_num(t_you_a))
    lines.append('  T劣（敌方）：敌方索敌 < %s（即 敌方 < 我方×0.75）' % _fmt_num(t_li_a))
    return {'reply': chr(10).join(lines), 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


_SCOUT_TEAM_SIZE = 6   # 队伍最高索敌：固定 6 人（用户口径 2026-09-14，必须填满）


def _handle_scout_mode2(msg: str, agent=None) -> dict:
    """Mode2（队伍最高索敌）：固定 6 名角色，各组索敌天赋合计（1/2/3/4 每组各选 1 个），
    +98×2+算术额外，×乘算系数，求和下取整。不足 6 名或含未识别舰名时给出明确补全提示，不做部分求和。"""
    def _err(text):
        return {'reply': text, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    matcher = _build_ship_matcher(agent) if agent is not None else None
    rest = re.sub(r'^(?:索敌|侦察)(?:线|值|判定|计算|估算|队伍|编队|阵容|最高)?\s*', '', msg).strip()
    # 有逗号/顿号/换行 → 按分隔符切；否则（如「索敌 名1 名2 … 名6」）按空白切，
    # 每个 token 视为一名角色（此形态不带算式/系数，算式/系数请用逗号分隔写法）。
    if re.search(r'[，,、\n]', rest):
        segs = [s for s in re.split(r'[，,、\n]+', rest) if s.strip()]
    else:
        segs = [s for s in re.split(r'\s+', rest) if s.strip()]
    if not segs:
        return _err('未识别到角色。队伍最高索敌需要填满 %d 名角色，'
                    '请按「索敌队伍 角色1, 角色2, …, 角色6」列出（逗号/顿号分隔），'
                    '可附算术额外（如 14*3+10）与乘算系数（如 1.1）。' % _SCOUT_TEAM_SIZE)
    if len(segs) != _SCOUT_TEAM_SIZE:
        if len(segs) < _SCOUT_TEAM_SIZE:
            head = ('队伍最高索敌需要填满 %d 名角色，当前只给了 %d 名，还差 %d 名。'
                    % (_SCOUT_TEAM_SIZE, len(segs), _SCOUT_TEAM_SIZE - len(segs)))
        else:
            head = ('队伍最高索敌固定 %d 名角色，当前给了 %d 名，请精简到 %d 名。'
                    % (_SCOUT_TEAM_SIZE, len(segs), _SCOUT_TEAM_SIZE))
        return _err(head + '\n格式：「索敌队伍 角色1, 角色2, …, 角色6」，可附算术额外（如 14*3+10）'
                           '与乘算系数（如 1.1）。')
    chars = []
    unresolved = []
    for seg in segs:
        parsed = _parse_scout_segment(seg)
        if not parsed:
            continue
        nm, arith_str, mult = parsed
        canon = matcher.match(nm) if matcher else None
        if not canon:
            unresolved.append(nm)
            continue
        chars.append(_scout_char_breakdown(canon, arith_str, mult, agent))
    if unresolved:
        return _err('队伍中有 %d 个无法识别的角色名：%s。\n请检查舰名后重新填写完整的 %d 名角色。'
                    % (len(unresolved), '、'.join(unresolved), _SCOUT_TEAM_SIZE))
    if len(chars) != _SCOUT_TEAM_SIZE:
        return _err('队伍最高索敌需要 %d 名可计算的角色，当前只有 %d 名，请补全后重试。'
                    % (_SCOUT_TEAM_SIZE, len(chars)))
    total = sum(c['val'] for c in chars)
    final = math.floor(total)
    out = []
    out.append('《队伍最高索敌计算》')
    out.append('（每角色 = 基础索敌×(1+各组索敌天赋%之和) + 98×2 + 算术额外，再 ×乘算系数；'
               '1/2/3/4 组各可选 1 个天赋）')
    out.append('')
    for i, c in enumerate(chars, 1):
        out.append('%d. %s' % (i, c['name']))
        if c['picks']:
            pick_txt = ' ＋ '.join('%s组·%s %s%%' % (g, n, _fmt_num(p)) for g, n, p in c['picks'])
            out.append('   基础索敌：%s ｜ 索敌天赋：%s ｜ 合计 %s%%'
                       % (_fmt_num(c['base']), pick_txt, _fmt_num(c['pct_sum'])))
        else:
            out.append('   基础索敌：%s ｜ 索敌天赋：无' % _fmt_num(c['base']))
        extra_disp = ('%s = %s' % (c['extra_expr'], _fmt_num(c['extra']))) if c['extra_expr'] else '无'
        out.append('   算术额外：%s ｜ 乘算系数：%s' % (extra_disp, _fmt_num(c['mult'])))
        out.append('   小计：(%s×%s + 196 + %s) × %s = %s'
                   % (_fmt_num(c['base']), _fmt_num(1 + c['pct_sum'] / 100.0),
                      _fmt_num(c['extra']), _fmt_num(c['mult']), _fmt_num(c['val'])))
        out.append('')
    out.append('合计（下取整）：%s' % _fmt_num(final))
    return {'reply': '\n'.join(out), 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


# 索敌「菜单词」fullmatch（无数值、无角色列表的纯工具入口词）。
# 模块级常量：`_handle_scout` 与 route_work 的「工具切换」判定共用，避免两处正则漂移。
# 注意：刻意不含数字与运算符 → 计算流程里的面板指令（如「索敌+50」「面板索敌=800」）不会被吞。
# 口语动词中缀（一下/下）与敬语前缀（帮我/给我/来/请）已覆盖：此前「算下索敌」因多一个
# 「下」字 fullmatch 失败，被 calc 门控抢走、把「下索敌」当攻击方舰名（用户口径 2026-09-15）。
_SCOUT_MENU_RE = re.compile(
    r'(?:(?:帮我|给我|来|请)?(?:计算|算|查)(?:一下|下)?)?'
    r'(?:队伍|编队|阵容)?(?:最高)?索敌(?:线|计算)?')


def _handle_scout(msg: str, agent=None) -> dict or None:
    """索敌计算路由：Mode1（数值）→ 索敌线；Mode2（队伍）→ 队伍最高索敌（固定 6 人）；
    纯索敌词（无参数）→ 交互表单引导。都不匹配返回 None（交回后续路由，如攻略问答）。"""
    m1 = _SCOUT_MODE1_RE.search(msg or '')
    if m1:
        return _handle_scout_mode1(float(m1.group(1)))
    if _SCOUT_MODE2_RE.search(msg or ''):
        return _handle_scout_mode2(msg, agent)
    # 宽松触发：去掉索敌词后像角色名列表（≥2 个可识别名）→ 仍走 Mode2
    if agent is not None and _looks_like_ship_list(msg, _build_ship_matcher(agent)):
        return _handle_scout_mode2(msg, agent)
    # 菜单/纯索敌触发（无数值也无角色列表）→ 弹交互表单（两种模式）：
    # 覆盖悬浮球「计算工具 → 索敌计算」、手动「算索敌」/「查索敌」/「计算索敌」等。
    # 允许可选 计算/算/查 前缀——用户自然输入会被 _calc_weak('算' in msg) 或 _calc_strong('计算' in msg)
    # 误抢进单体伤害计算流程（把「索敌」当攻击方舰名解析→无法识别）；此 fullmatch 在 calc 门控之前拦下。
    # 含数字/角色名的合法输入已在上面 m1 / MODE2 / ship_list 分支接走，不会落到这里。
    # 覆盖 算/查/计算 前缀 + 队伍/编队/阵容 前置 + 最高 等修饰（如「队伍最高索敌」）。
    # 正则提升为模块级 `_SCOUT_MENU_RE`，供 route_work 的「工具切换」判定复用（勿两处各维护一份）。
    if _SCOUT_MENU_RE.fullmatch((msg or '').strip()):
        return _handle_scout_form()
    return None


def _handle_scout_form():
    """索敌计算交互表单：悬浮球「计算工具 → 索敌计算」或手动「算索敌」进入。
    返回 scout_form 标记，前端渲染为两种模式可切换的填空界面，提交后拼回文本指令
    （「索敌 <数值>」/「索敌队伍 名 算式 系数, …」）再走 Mode1/Mode2 计算。"""
    return {
        'reply': '指挥官，选一种索敌计算方式：',
        'tool_outputs': [], 'suggestions': [],
        'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
        'scout_form': True,
    }


def _handle_calc_menu():
    """悬浮球「计算工具」子菜单：在回复中弹窗选项（suggestions）。
    每个选项带 cmd（指令短语），前端点击后直接发对应指令、回到对应计算流程。
    """
    return {
        'reply': '指挥官，选择要用的计算工具：',
        'tool_outputs': [],
        'suggestions': [
            {'label': '角色伤害', 'cmd': '算伤害'},
            {'label': '好感度', 'cmd': '好感度计算'},
            {'label': '索敌计算', 'cmd': '索敌线'},
            {'label': '经验值计算', 'cmd': '经验值计算'},
            {'label': '模拟组队', 'cmd': '模拟pvp'},
        ],
        'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
    }


# ============ 经验值计算（用户口径 2026-09-14）============
# 等级经验表（游戏内「必要経験値」= 到达该等级的累计经验；末级 100 无「下一级」）。
_EXP_MAX_LV = 100
# 下标即等级：_EXP_CUM[1]=0 … _EXP_CUM[100]=8858580
_EXP_CUM = [
    0, 0, 100, 200, 400, 700, 1100, 1600, 2200, 2900, 3700,
    4600, 5600, 6700, 7900, 9200, 10600, 12100, 13700, 15400, 17200,
    19100, 21100, 23200, 25400, 27700, 30100, 32600, 35200, 37900, 40700,
    43700, 46820, 50060, 53420, 56900, 60500, 64220, 68060, 72020, 76100,
    84540, 93260, 102260, 111540, 121100, 130940, 150380, 171440, 194420, 219620,
    247440, 295560, 352820, 423320, 513020, 630580, 750080, 871660, 995360, 1121220,
    1249280, 1379740, 1512780, 1648600, 1787380, 1929320, 2074600, 2223420, 2375960, 2532420,
    2692980, 2853540, 3014100, 3174660, 3335220, 3495780, 3656340, 3816900, 3977460, 4138020,
    4298580, 4538580, 4778580, 5018580, 5258580, 5498580, 5738580, 5978580, 6218580, 6458580,
    6698580, 6938580, 7178580, 7418580, 7658580, 7898580, 8138580, 8378580, 8618580, 8858580,
]
_EXP_CRYSTAL = 500        # ge 晶体：每个提供 500 经验
_EXP_CUBE_MULT = 3        # 三倍经验：每场 = 出战经验 × 3

# 菜单词（纯问法，不含参数）→ 弹表单
_EXP_MENU_RE = re.compile(
    r'(?:(?:帮我|给我|来|请)?(?:计算|算|查)(?:一下|下)?)?\s*(?:经验|EXP|exp)\s*(?:值)?\s*(?:计算|换算|计算器|查询)?'
    r'|(?:升|满)级(?:所需|需要|要的)?\s*(?:经验|EXP)')
# 提交串：含「等级：N」且含「经验」
_EXP_LV_RE = re.compile(r'(?:当前)?等级\s*[：:]\s*(\d{1,3})')
_EXP_CUR_RE = re.compile(r'当前经验(?:值)?\s*[：:]\s*(\d+)')
_EXP_BATTLE_RE = re.compile(r'出战经验(?:值)?\s*[：:]\s*(\d+)')


def _handle_exp_form():
    """经验值计算交互表单：悬浮球「计算工具 → 经验值计算」或输入「算经验值」等进入。"""
    return {
        'reply': '指挥官，填当前等级与经验，我来算剩余经验。',
        'tool_outputs': [], 'suggestions': [],
        'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
        'exp_form': True,
    }


def _fmt_int(x: float) -> str:
    """整数展示（带千分位）。"""
    return '{:,}'.format(int(round(x)))


def _exp_compute(msg: str):
    """按「等级：L 当前经验：X 出战经验：E」计算。X 为「本级已累积经验」，E 可缺省。"""
    def _err(text):
        return {'reply': text, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    m_lv = _EXP_LV_RE.search(msg or '')
    if not m_lv:
        return _err('没识别到等级。请按「经验计算 等级：45 当前经验：3200 出战经验：1200」填写'
                    '（出战经验可省略）。')
    lv = int(m_lv.group(1))
    if not (1 <= lv <= _EXP_MAX_LV):
        return _err('等级需在 1 ~ %d 之间，你填的是 %d。' % (_EXP_MAX_LV, lv))
    m_cur = _EXP_CUR_RE.search(msg or '')
    cur = int(m_cur.group(1)) if m_cur else 0
    m_bat = _EXP_BATTLE_RE.search(msg or '')
    battle = int(m_bat.group(1)) if m_bat else 0

    lv_need = (_EXP_CUM[lv + 1] - _EXP_CUM[lv]) if lv < _EXP_MAX_LV else 0
    total = _EXP_CUM[lv] + max(0, cur)          # 累计经验（含本级已累积）
    cap = _EXP_CUM[_EXP_MAX_LV]
    to_next = max(0, _EXP_CUM[lv + 1] - total) if lv < _EXP_MAX_LV else 0
    to_cap = max(0, cap - total)

    out = ['《经验值计算》']
    out.append('当前等级：Lv.%d ｜ 本级已累积 %s / %s'
               % (lv, _fmt_int(cur), _fmt_int(lv_need) if lv < _EXP_MAX_LV else '—（满级）'))
    out.append('累计经验：%s（满级 Lv.%d 需 %s）' % (_fmt_int(total), _EXP_MAX_LV, _fmt_int(cap)))
    out.append('')
    if lv >= _EXP_MAX_LV or to_cap <= 0:
        out.append('· 已经是满级，无需再练。')
        return {'reply': '\n'.join(out), 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    out.append('· 升至 Lv.%d 还需：%s' % (lv + 1, _fmt_int(to_next)))
    out.append('· 升至满级 Lv.%d 还需：%s' % (_EXP_MAX_LV, _fmt_int(to_cap)))
    if cur > lv_need > 0:
        out.append('  （提示：本级已累积 %s 超过升级所需 %s，实际应已升级，请核对等级。）'
                   % (_fmt_int(cur), _fmt_int(lv_need)))

    # ① 直接用 ge 晶体（500 经验/个）冲满级
    out.append('')
    out.append('【用 ge 晶体直冲满级（%s 经验/个）】' % _fmt_int(_EXP_CRYSTAL))
    out.append('· 需 %s ÷ %s = %s → %s 个'
               % (_fmt_int(to_cap), _fmt_int(_EXP_CRYSTAL),
                  ('%.2f' % (to_cap / _EXP_CRYSTAL)), _fmt_int(math.ceil(to_cap / _EXP_CRYSTAL))))

    # ② 按出战经验折算场数 / 三倍场次
    if battle > 0:
        per = battle * _EXP_CUBE_MULT
        out.append('')
        out.append('【按出战经验折算（%s 经验/场）】' % _fmt_int(battle))
        out.append('· 需出击：%s ÷ %s = %s → %s 场'
                   % (_fmt_int(to_cap), _fmt_int(battle),
                      ('%.2f' % (to_cap / battle)), _fmt_int(math.ceil(to_cap / battle))))
        out.append('· 三倍经验场次（%s × %d = %s 经验/场）：%s ÷ %s = %s → %s 场'
                   % (_fmt_int(battle), _EXP_CUBE_MULT, _fmt_int(per), _fmt_int(to_cap),
                      _fmt_int(per), ('%.2f' % (to_cap / per)), _fmt_int(math.ceil(to_cap / per))))
    else:
        out.append('')
        out.append('（填「出战经验」可一并算出所需出击场数与三倍经验场次。）')
    return {'reply': '\n'.join(out), 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


def _handle_exp_calc(msg: str):
    """经验值计算路由：提交串（含「等级：N」）→ 直接算；纯菜单词 → 弹表单；否则 None。"""
    s = (msg or '').strip()
    if _EXP_LV_RE.search(s) and re.search(r'经验|EXP|exp', s, re.IGNORECASE):
        return _exp_compute(s)
    if _EXP_MENU_RE.fullmatch(s):
        return _handle_exp_form()
    return None


def _is_tool_switch(msg: str) -> bool:
    """是否为「明确的计算工具切换指令」（计算工具菜单 / 索敌 / 经验值 / 好感度）。

    用途：伤害计算流程进行中（`overrides['calc_state']` 激活）原本是**排他**的——
    任何输入都进 handle_calc，导致用户点别的工具（如「算经验值」）被吞进伤害计算，
    把工具词当攻击方舰名解析 → 「攻击方「经验值」无法识别」（用户口径 2026-09-14）。
    命中即放行到下面的工具分支，并重置本轮计算状态。

    判定用 fullmatch 且正则可匹配串**不含数字/运算符**，因此计算流程里的面板/属性指令
    （「索敌+50」「面板索敌=800」「炮击2000」）不会被误判成工具切换。
    """
    s = (msg or '').strip()
    if not s:
        return False
    if re.search(r'计算工具|计算菜单', s):
        return True
    if _SCOUT_MENU_RE.fullmatch(s):
        return True
    if _EXP_MENU_RE.fullmatch(s):
        return True
    # 好感度：入口词与表单提交串都算（提交串形如「好感度：现在喜欢，进度30%」）
    if re.search(r'好感度|好感', s):
        return True
    return False


def _fmt_num(x: float) -> str:
    """数值格式化：整数去 .0，小数保留 2 位。"""
    if x is None:
        return '0'
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return ('%.2f' % x).rstrip('0').rstrip('.') if '.' in ('%.2f' % x) else str(x)


def _build_ship_matcher(agent):
    from core.name_service import NameMatcher
    names = [k for k in (agent.app.raw_storage or {}).keys()
             if agent.app.is_ship_complete(k)]
    return NameMatcher(names)


# 其他工作流的参数/字段关键词：出现即说明这条消息不是「索敌角色列表」
# （典型误吞：遭遇战表单提交串「遭遇战模拟 boss：… 白天：… 前排：… 后排：… 旗舰：…」，
#  其中含大量舰名，曾被 _looks_like_ship_list 当成队伍最高索敌的角色列表 → 报「需要填满6名」）
_OTHER_WORKFLOW_KW_RE = re.compile(
    r'遭遇战|boss|BOSS|模拟|白天|黑夜|前排|后排|旗舰|防守|时长|伤害|面板|装备|攻略|'
    r'天赋|技能|好感|天气|航向|制空|阵型|攻击方|防守方|计算工具')


def _looks_like_ship_list(msg: str, matcher) -> bool:
    """宽松触发：整条消息确实是「索敌/侦察 + 一串角色名」（≥2 个可识别名）。

    严格前置：必须含「索敌/侦察」词，且不得含其他工作流的参数关键词——否则一句
    含多个舰名的表单提交串（遭遇战/PVP/伤害计算）会被误当角色列表。"""
    msg = msg or ''
    if '索敌' not in msg and '侦察' not in msg:
        return False
    if _OTHER_WORKFLOW_KW_RE.search(msg):
        return False
    rest = re.sub(r'^(?:计算|算|查)?(?:队伍|编队|阵容)?(?:最高)?(?:索敌|侦察)'
                  r'(?:线|值|判定|计算|估算|队伍|编队|阵容|最高)?\s*', '', msg).strip()
    if not rest:
        return False
    parts = [p for p in re.split(r'[，,、\s]+', rest) if p.strip()]
    if len(parts) < 2:
        return False
    cnt = 0
    for p in parts:
        parsed = _parse_scout_segment(p)
        nm = parsed[0] if parsed else p
        if matcher.match(nm):
            cnt += 1
    return cnt >= 2


# ======================================================================
# 遭遇战模拟（表单带参数）→ 确定性拦截
# 触发签名：消息同时含「遭遇战」「前排」「后排」（即前端遭遇战模拟表单提交，
# 形如「遭遇战模拟 boss：X 白天：Y 前排：A B C 后排：D E F 旗舰：Z」）。
# **必须早于 _handle_tips 结构化层**：否则「阿丝莫德」会被 tips 键命中（阿丝莫德应对）
# 抢成《小技巧》，导致模拟工具根本跑不起来——用户铁律「明确工具里不得 LLM 干扰」。
# 关键词驱动、不依赖 LLM 意图分类，保证表单永远进模拟器。
# ======================================================================
def _handle_encounter_sim_params(msg: str, tools):
    """遭遇战模拟表单（含 boss/白天/前排/后排 参数）→ 校验并直接推演，或回显错误表单。"""
    if not ('遭遇战' in msg and '前排' in msg and '后排' in msg):
        return None
    tool = _tool(tools, 'encounter_simulator')
    if not tool:
        return None
    try:
        boss = re.search(r'boss[：:]\s*([^\s]+)', msg)
        day = re.search(r'白天[：:]\s*([^\s]+)', msg)
        front = re.search(r'前排[：:]\s*([^\n]+?)(?=后排|$)', msg)
        back = re.search(r'后排[：:]\s*([^\n]+?)(?=旗舰|$)', msg)
        flagship = re.search(r'旗舰[：:]\s*([^\s]+)', msg)
        _app = _get_app(tools)
        raw_boss = boss.group(1) if boss else ''
        raw_day = day.group(1) if day else ''
        raw_front = (front.group(1).split() if front else [])
        raw_back = (back.group(1).split() if back else [])
        raw_flagship = flagship.group(1) if flagship else ''
        # 全量校验：识别「所有」无法识别项（不止第一个）
        vres = validate_encounter_input(_app, raw_boss, raw_day, raw_front, raw_back, raw_flagship)
        if not vres['ok']:
            bad = {e['field'] for e in vres['errors'] if e['field'] in
                   {'boss', 'day_time', 'front1', 'front2', 'front3', 'back1', 'back2', 'back3'}}
            emsg = {e['field']: e['message'] for e in vres['errors']}
            lead = f"指挥官，有 {len(vres['errors'])} 项没能识别出来，已帮你保留能识别的内容，请只修改标红的格子后重新提交："
            form = _build_team_form(False, raw_boss, raw_day, raw_front, raw_back, raw_flagship, bad, emsg)
            return _rerender_on_errors(lead, vres['errors'], form)
        # 全部识别 → 直接模拟
        r = vres['resolved']
        sim_res = simulate_encounter(_app, r['boss_name'], r['day_time'], None,
                                     r['front_ships'], r['back_ships'], 'T优', '确保',
                                     r['flagship_name'])
        text = sim_res['text'] if isinstance(sim_res, dict) else sim_res
        detail = sim_res.get('detail', '') if isinstance(sim_res, dict) else ''
        svg = sim_res.get('svg', '') if isinstance(sim_res, dict) else ''
        return {'reply': text, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
                'encounter_detail': detail, 'encounter_timeline_svg': svg,
                'encounter_compute': True}
    except Exception as e:
        return {'reply': f"噢，指挥官，出错了。{e}", 'tool_outputs': [], 'suggestions': [],
                'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': False}


# ======================================================================
# 选择 / 比较 / 评估意图（compare / evaluate）处理器 —— 用户口径 2026-09-17
# ----------------------------------------------------------------------
# 口径演进（两段，别再退回）：
#   ① 「对比780和太太」原会被末级 `_llm_analyze_intent` → `_resolve_role_from_msg`
#      的「昵称子串取最长」捞成**单个**舰名（780→齐柏林伯爵），直接回了单舰面板
#      → 改为 compare/evaluate 意图唯一裁决（词表与注册表见 core/intents.py）。
#   ② 首版处理器做的是**面板逐字段差异**，用户口径改为——
#      「不再对比面板，而是根据知识库向量库找到各自的培养方法或者相关描述，
#        简单类比一下；如果没有找到相关，就回答暂时不支持该角色」。
#
# 现口径：拆实体 → 每位在知识库的**点名片段**里取材（tools.knowledge.
# find_ship_mentions：向量检索 + 全语料扫名兜底 + 篇目摘要点名，另加 tips 点名）
# → 有据者交 LLM 在「只依据片段、禁止编造、不下结论」约束下写 2~3 句并点出差异；
# 某位全库无点名 → 该位直答「暂时不支持该角色」；全部无点名 → 整条只回这句。
#
# 注意：本条**是**全项目唯一允许调 LLM 产出正文的确定性处理器（用户明确要求
# 「简单类比」），约束靠 ① 只喂点名片段 ② 系统提示禁编造 ③ 解析失败回落原文摘录。
# ======================================================================

# 拆实体时剔除的「比较/评估/疑问」噪声（**头尾锚定**，绝不裸删名字中间的字，
# 否则含常见字（的/好/谁/更…）的舰名会被削坏）
_CMP_HEAD_RE = re.compile(
    r'^(?:请|帮我|给我|麻烦|你|说说|说下|看看|看下|分析|分析下|评价|评价下|评估)?'
    r'(?:对比|比较|相比|分析|评估|评价|介绍|查下|查|看下|看看|说说|说下)?')
_CMP_TAIL_RE = re.compile(
    r'(?:的话|哪个好|哪个更好|哪一个|哪艘|哪个|比较好|更好|更值得|值不值|值得|划算|'
    r'优先级|优先|先练|先养|推荐练|推荐|该练|该养|培养|练谁|用谁|选谁|排行|排名|榜|'
    r'区别|差别|差异|怎么样|怎样|咋样|如何|谁|更|好|强|的|呢|吗|啊|呀|请|谢谢)+$')
# 一次对比/评估最多取几位实体：每位要一次向量检索 + 一段 LLM 正文，再多时延不可接受
_CMP_MAX_ENTITIES = 3
# 每位实体最多喂给 LLM 的点名片段数
_CMP_MAX_MENTIONS = 5
# 知识库中「培养/改造优先级」的唯一有据来源（文档标题关键词）
_CULTIVATE_DOC_KEY = '改造优先级'
# 属性维度词：出现即说明是「筛选名单」问句（"哪些值得改造的航母"），
# 不该被「泛化的培养优先级询问」接管——那种问句的正解是筛选结果，不是那篇攻略。
_DIM_KW_RE = re.compile(
    r'驱逐|战列|战巡|航母|轻母|装母|巡洋|重巡|轻巡|重炮|水母|雷巡|航战|维修|潜艇|'
    r'小船|中船|大船|小型|中型|大型|'
    r'凤栖|八咫|尤奈特|洛蒙瑞亚|奥鲁加|列加杜|纳谢尔|沃尔克|'
    r'彩|金|紫|蓝|绿|白')


def _cmp_strip_noise(seg: str) -> str:
    """剥掉一个片段头尾的比较/疑问噪声词与标点（最多反复 3 轮）。"""
    s = (seg or '').strip()
    for _ in range(3):
        s2 = _CMP_TAIL_RE.sub('', _CMP_HEAD_RE.sub('', s))
        s2 = s2.strip(' \t，。！？、,.!?;；:：~～·-—「」《》“”\'"()（）[]【】')
        if s2 == s:
            break
        s = s2
    return s


def _extract_compare_entities(msg: str, app) -> list:
    """从「对比A和B」类输入中拆出**全部**实体（正规名，按原文出现位置保序、去重）。

    顺序：① 逐段剥噪声后走 `get_raw_ship`——昵称/别名/错字全覆盖
             （780→齐柏林伯爵、太太→列克星敦、雪风改→丹阳）；
          ② 昵称子串兜底（未带分隔符的整句，如「口罩拉菲」）；
          ③ 通用多实体抽取补漏（如「丹阳+女灶神」式写法）。
    实体一律过 `is_ship_complete` 门控——信息不全视同不存在（全项目唯一判据）。
    """
    if app is None:
        return []
    raw_storage = getattr(app, 'raw_storage', {}) or {}
    cand, seen = [], set()

    def _push(pos, name):
        if not name or name in seen or name not in raw_storage:
            return
        try:
            if not app.is_ship_complete(name):
                return
        except Exception:
            return
        seen.add(name)
        cand.append((pos, name))

    # ① 逐段剥噪声 → get_raw_ship
    for m in re.finditer(r'[^+\s，,、;；与和及&/｜|~]+', msg):
        seg = _cmp_strip_noise(m.group(0))
        if not seg or len(seg) > 12:
            continue
        name, _raw = app.get_raw_ship(seg)
        if name:
            _push(m.start(), name)
    # ② 昵称子串兜底
    try:
        from core.name_service import build_hard_map
        hard = build_hard_map()
        for nick in sorted(hard, key=len, reverse=True):
            if len(nick) >= 2 and nick in msg:
                _push(msg.find(nick), hard.get(nick))
    except Exception:
        pass
    # ③ 通用多实体抽取补漏（无分隔符写法）
    for n in _extract_multi_ship_names(msg, app, max_n=6):
        if n not in seen:
            _push(len(msg) + 1, n)

    cand.sort(key=lambda x: x[0])
    return [n for _p, n in cand][:6]


def _cmp_collect(name):
    """取某位舰灵在知识库里的**点名片段**（攻略语料 + tips）。

    返回 {'name','mentions':[{'doc','section','content','via'}]}。
    mentions 为空 = 知识库里没有任何点名她的描述 → 调用方按「暂时不支持该角色」处理。
    攻略语料侧走 tools.knowledge.find_ship_mentions（向量 + 全语料扫名，已做摘录与
    描述性筛选）；tips 侧是原文直出，命中即算有据描述。
    """
    res = {'name': name, 'mentions': []}
    # ① 攻略语料点名片段（向量检索 + 全语料扫名兜底）
    try:
        from tools.knowledge import find_ship_mentions
        got = find_ship_mentions(name, limit=_CMP_MAX_MENTIONS)
        for m in (got.get('mentions') or []):
            res['mentions'].append({
                'doc': m.get('doc') or '', 'section': m.get('section') or '',
                'content': (m.get('content') or '').strip(), 'via': m.get('via') or ''})
    except Exception as e:
        print('[compare] 攻略语料点名检索失败 | %s | %s' % (name, e), flush=True)
    # ② tips 点名（原文直出，不走向量）
    try:
        from core import tips_index as _tips
        _n = 0
        for t in _tips.all_tips():
            blob = (t.get('title') or '') + (t.get('content') or '')
            if name in blob:
                res['mentions'].append({
                    'doc': '小技巧·%s' % (t.get('category') or ''),
                    'section': t.get('title') or '',
                    'content': (t.get('content') or '').strip(), 'via': 'tips'})
                _n += 1
                if _n >= 2:
                    break
    except Exception:
        pass
    # 去重（同一 doc+section+正文）后截断
    seen, out = set(), []
    for m in res['mentions']:
        key = (m['doc'], m['section'], m['content'][:40])
        if key in seen or not m['content']:
            continue
        seen.add(key)
        out.append(m)
    res['mentions'] = out[:_CMP_MAX_MENTIONS]
    return res


def _cmp_raw_block(ent) -> str:
    """LLM 段落缺失时的兜底：把点名片段按原文摘录直出——宁可摘抄，绝不编造。"""
    lines = []
    for m in ent.get('mentions') or []:
        src = m.get('doc') or ''
        if m.get('section'):
            src += ' · ' + m['section']
        body = re.sub(r'\s+', ' ', (m.get('content') or '').strip())
        if len(body) > 180:
            body = body[:180] + '…'
        lines.append('· 《%s》%s' % (src, body))
    return '\n'.join(lines) if lines else '（暂无可用片段）'


# LLM 类比生成的系统提示：所有约束都写死在这里——喂进去的只有点名片段，
# 模型只负责把它们换成通顺的 2~3 句并点出彼此差异，不得引入片段外内容。
_CMP_LLM_SYS = (
    '你是拂晓（舰灵海战手游）的资料整理助手。下面按舰灵分组，给出知识库中'
    '**点名她们**的原文片段。\n'
    '请为**每一位**舰灵写 2~3 句简介：说明片段里提到的她的培养方向、定位或用法特点；'
    '若能看出差异，用一句话点出与其他几位的不同。\n'
    '硬性要求：\n'
    '· 只依据给定片段；片段里没有的信息一律不得写入。\n'
    '· 禁止用「一般来说」「通常」「推测」「应该」「可能」「建议」等方式推断或补全'
    '片段外内容；片段未涉及的方面直接不写。\n'
    '· 舰名、数值、装备名、章节号一律照抄片段原文，不得改动、换算或补充。\n'
    '· 片段里若只是把舰灵列在名单／阵容／表格里而没有对她的说明，不得据此推测她的定位。\n'
    '· 不下「该练谁」的结论，不排序；不输出来源篇目列表，不反问，不要开场白或格式说明。\n'
    '输出格式（严格遵守）：按给出的顺序，每位先单独一行写【舰名】（照抄该舰灵名），'
    '下一行起写正文；不同舰灵之间空一行。'
)


def _cmp_llm_analogy(ents):
    """让 LLM 在「只依据片段」约束下为每位实体写简介并点出差异。

    ents：只传**有据**的实体（[{'name','mentions'}]）。返回 {舰名: 正文}；失败/解析不出
    返回 {}，调用方回落 `_cmp_raw_block`（原文摘录），绝不空回。
    """
    if not ents:
        return {}
    blocks = []
    for e in ents:
        seg = ['### 舰灵：%s ###' % e['name']]
        for m in e.get('mentions') or []:
            head = m.get('doc') or ''
            if m.get('section'):
                head += ' · ' + m['section']
            seg.append('【%s】%s' % (head, (m.get('content') or '').strip()))
        blocks.append('\n'.join(seg))
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.3)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=40)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[{'role': 'system', 'content': _CMP_LLM_SYS},
                      {'role': 'user', 'content': '\n\n'.join(blocks)}],
            temperature=0.3,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式
            extra_body=cfg['generate_cfg']['extra_body'],
        )
        answer = (resp.choices[0].message.content or '').strip()
    except Exception as e:
        print('[compare] 类比生成失败 | %s' % e, flush=True)
        return {}
    if not answer:
        return {}
    marks = list(re.finditer(r'【\s*([^】]{1,24}?)\s*】', answer))
    if not marks:
        return {}
    out = {}
    for i, mk in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(answer)
        body = answer[mk.end():end].strip()
        if not body:
            continue
        title = mk.group(1)
        for e in ents:
            if e['name'] == title or e['name'] in title or title in e['name']:
                out.setdefault(e['name'], body)
                break
    return out


_NOT_SUPPORTED = '暂时不支持该角色。'
# 「憋住」追问：返回「暂时不支持该角色」时，提示用户可直接报舰灵名重试（不保证准确）。
# 用户口径 2026-09-17 追问分支：只做轻量内联提示，不落地「支持哪些角色」清单（Option A）。
_NOT_SUPPORTED_HINT = '你可以直接报舰灵名让我帮类比，但我不保证能得出准确结果。'


def _handle_ship_compare(tools, msg, agent=None, overrides=None, intent='compare'):
    """compare / evaluate 意图处理器：**按知识库点名片段做培养/定位类比**（不再比面板）。

    compare ：≥2 实体 → 每位在知识库点名片段里取材，LLM「只依据片段」写 2~3 句并点出差异；
    evaluate：≥1 实体 → 同上（是否值得练不下结论）。
    某位实体全库无点名 → 该位直答「暂时不支持该角色」；全部无点名 → 整条只回这句。
    返回 None = 「本轮不接管」：实体不足、或句中其实是其它明确工具诉求
    （伤害计算/配装/攻略/模拟/表单参数…）→ 交回原正则阶梯，绝不硬吞。
    """
    if not tools and agent is None:
        return None
    app = _get_app_from_tools(tools, agent)
    if app is None:
        return None
    s = (msg or '').strip()
    # 明确的计算诉求 → 归 calc（「算A和B伤害」不是对比）
    if re.search(r'伤害|dps|DPS|期望|一击|一发|总伤', s):
        return None
    # 单舰具体字段查询必须返回真实面板/技能/天赋，不得因昵称
    # 可映射到多个角色而误进“舰灵对比”。
    if re.search(r'天赋|技能|必杀技|通常技|支援技|旗舰技|面板|炮击|雷击|航空|'
                 r'索敌|命中|幸运|装填|装甲|对空|机动|血量|舰种|稀有|所属|buff', s, re.I):
        return None
    # 其它明确工具诉求 → 不抢（配装/攻略/模拟/表单参数/玩法问法等）
    if re.search(r'配装|搭配|装备|攻略|阵容|加点|活动|关卡|遭遇战|模拟|pvp|PVP|boss|BOSS|'
                 r'前排|后排|白天|防守|怎么|如何|怎样|求助|求教', s):
        return None
    names = _extract_compare_entities(s, app)
    if len(names) < 2 if intent == 'compare' else not names:
        # 无实体的「泛化培养/改造优先级」询问（"哪些舰灵值得优先改造"）：
        # 正解是知识库那篇**有据**的《舰灵改造优先级》，而不是把「有改造形态的 30 名」
        # 当答案（那不是优先级）。含属性维度词的问句除外——那种该走筛选名单。
        if (intent == 'evaluate' and not names and not _DIM_KW_RE.search(s)
                and re.search(r'改造|培养|养成|练级|优先|值得', s)
                and re.search(r'哪些|哪艘|什么|谁|推荐|排行|优先', s)):
            body = []
            for d in (_load_guide_kb().get('documents', []) or []):
                if _CULTIVATE_DOC_KEY in (d.get('title') or ''):
                    body.append('《攻略》%s' % d.get('title'))
                    if d.get('summary_verified') is True and d.get('summary'):
                        body.append('　　%s' % d['summary'].strip())
                    if d.get('file'):
                        body.append('<<GUIDE_FILE>>%s|%s'
                                    % (d['file'], d.get('title') or _CULTIVATE_DOC_KEY))
                    break
            else:
                return None
            return _guide_reply_from_text('\n'.join(body), _CULTIVATE_DOC_KEY)
        return None
    if overrides is not None:
        overrides['last_role'] = names[0]

    names = names[:_CMP_MAX_ENTITIES]
    ents = [_cmp_collect(n) for n in names]
    have = [e for e in ents if e['mentions']]
    # 多实体一律按「对比」办——意图分类可能把「丹阳跟企业哪个更值得练」判成
    # evaluate，那种问句仍要两位都列，不能只答 names[0]（踩过：只回了丹阳）。
    is_cmp = (intent == 'compare' or len(names) >= 2)

    # 用户要求比较高下时，只要任一对象缺少有据描述，就没有公平比较基础：
    # 不摘录另一方、不硬判强弱，统一结束这个问题并提示换题。
    if not have or (is_cmp and len(have) < len(ents)):
        return {'reply': '具体场景具体分析。知识库没有足够信息判断这些角色的高下，换个话题吧。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    head = ('《舰灵对比 · %s》' % ' / '.join(names)) if is_cmp \
        else ('《舰灵评估 · %s》' % names[0])
    lines = [head, '']
    llm = _cmp_llm_analogy(have)
    for e in ents:
        lines.append('【%s】' % e['name'])
        if not e['mentions']:
            lines.append('%s——知识库里没有点名她的培养或相关描述。\n%s'
                         % (_NOT_SUPPORTED, _NOT_SUPPORTED_HINT))
        else:
            lines.append(llm.get(e['name']) or _cmp_raw_block(e))
        lines.append('')
    # 取材行列**真正命中片段**的篇目。不挂 <<GUIDE_FILE>>：前端已不把 guide_files
    # 渲成回复内卡片，而 guide_lib 为空时滑块不弹（`index.html:1740`）——挂了也是死标记。
    # 来源改由这一行文字交代，回复保持自洽、不误弹攻略面板。
    _srcs = []
    for e in have:
        for m in (e.get('mentions') or []):
            _d = m.get('doc') or ''
            if _d and '问答补充' not in _d and _d not in _srcs:
                _srcs.append(_d)
    if _srcs:
        lines.append('来源：%s' % '、'.join('《%s》' % t for t in _srcs[:5]))
    if not is_cmp:
        lines.append('是否练她取决于你的阵容与用途，我不替你下结论。')

    out = {'reply': '\n'.join(lines), 'tool_outputs': [], 'suggestions': [],
           'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '',
           'ok': True}
    # 单舰评估：顺带打开角色面板（与「查询角色」同款前端面板）
    if not is_cmp and names:
        out['ship_browser'] = True
        out['ship_focus'] = names[0]
    return out


def _intent_capability(tools, msg, agent=None, overrides=None, decision=None):
    return {'reply': _CAPABILITY_REPLY,
            'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


def _intent_scout(tools, msg, agent=None, overrides=None, decision=None):
    if re.search(r'哪些|什么|谁|哪(?:艘|条|种|个|位)|' + intents.CHOICE_RE_SRC, msg):
        return None
    return _handle_scout(msg, agent=agent) or _handle_scout_form()


def _intent_exp(tools, msg, agent=None, overrides=None, decision=None):
    if re.search(r'哪些|什么|谁|哪(?:艘|条|种|个|位)|' + intents.CHOICE_RE_SRC, msg):
        return None
    return _handle_exp_calc(msg) or _handle_exp_form()


def _intent_equip(tools, msg, agent=None, overrides=None, decision=None):
    """装备目录与装备建议共用一个语义处理器，不按具体装备名称建立独立路由。"""
    action = (decision or {}).get('equip_action')
    if action == 'catalog':
        return _common_equip_table_reply()
    if action == 'advice':
        result = _handle_guide_qa(tools, msg)
        if result is not None:
            return result
        return {'reply': '配装建议在攻略里查看——已为你打开配装相关目录，按分类翻阅即可。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '配装', 'ok': True}
    return None


def _intent_guide(tools, msg, agent=None, overrides=None, decision=None):
    tips = _handle_tips(msg)
    if tips is not None:
        return tips
    return _route_guide(tools, msg)


# ---- 上下文追问拦截（用户反馈 2026-10-07）：上轮已讨论某角色（overrides['last_role'] 已记录），
#   本句是无显式舰名、但明显是「关于那个角色的后续问题」（如「是什么类型的装甲」），
#   必须作为该角色的上下文回答走角色查询分支，而不能被 _handle_theme_intro 兜底成「游戏简介」。
#   返回该角色名表示「应按上下文追问处理」，否则返回 ''（走原路由）。
_FOLLOWUP_ATTR = ('装甲', '类型', '属性', '技能', '强度', '耐久', '血量', '炮击', '雷击',
                  '航空', '防空', '护甲', '特性', '就业', '配队', '阵容', '怎么打',
                  '如何打', '厉害', '强吗', '值不值', '缺点', '优点', '玩法')
_FOLLOWUP_Q = re.compile(r'是什么|怎么样|如何|多少|高吗|低吗|强不|弱不')
# 无主语的笼统玩法问讯（不带任何角色/主题词，纯问游戏怎么玩/好不好玩/是什么玩法）：
# 用于游戏简介「明确相关意图」判定，仅这类才回落游戏概述。
_VAGUE_GAMEPLAY_RE = re.compile(
    r'是什么玩法|什么玩法|什么游戏|怎么玩|怎么入门|怎么开始|怎么上手|玩法是什么|玩法介绍|怎么玩这个|好不好玩|好玩吗|玩法|游戏介绍')


def _context_followup_target(msg: str, overrides, tools) -> str:
    """识别「无主语的上下文追问」：返回应作为主语的 last_role，否则 ''。"""
    subj = (overrides or {}).get('last_role', '') or ''
    if not subj:
        return ''
    # 含玩法/系统主题词（遭遇战/装备/改造/活动/练级/拂晓/这游戏…）→ 是真正的玩法介绍，不抢
    if any(k in msg for k in _THEME_INTRO):
        return ''
    # 消息已显式点名某角色 → 走正常角色查询，不视为对 last_role 的追问
    try:
        if tools is not None and _resolve_ship_query(msg, tools):
            return ''
    except Exception:
        pass
    # 追问信号：属性词 或 泛问答词（是什么/怎么样/多少/强吗…）
    if any(w in msg for w in _FOLLOWUP_ATTR) or _FOLLOWUP_Q.search(msg):
        return subj
    return ''


def _intent_theme_intro(tools, msg, agent=None, overrides=None, decision=None):
    # 上下文追问优先：若本句是对上轮角色（last_role）的后续提问，转角色查询分支，
    # 避免被 _handle_theme_intro 兜底成「游戏简介」
    _subj = _context_followup_target(msg, overrides, tools)
    if _subj:
        _resolved = _subj + msg
        _r = _intent_query(tools, _resolved, agent=agent, overrides=overrides, decision=decision)
        if _r is not None:
            return _r
        _r = _ship_panel_reply(_subj, tools)
        if _r is not None:
            return _r
        return None  # 角色查询无结果 → 交回上层，绝不再回落游戏简介
    bare = (msg or '').strip().rstrip('！!？?。，,、 ')
    return _handle_theme_intro(tools, msg, bare)


def _intent_query(tools, msg, agent=None, overrides=None, decision=None):
    """先处理结构化查询；单舰面板等需要更多解析的查询交给后续兼容层。"""
    attr_query = _is_ship_attr_query(msg)
    if attr_query[0]:
        result = _handle_ship_attr_query(tools, msg, agent=agent, overrides=overrides)
        if result is not None:
            return result
    return _handle_ship_filter(tools, msg, agent=agent, overrides=overrides)


def _intent_compare(tools, msg, agent=None, overrides=None, decision=None):
    return _handle_ship_compare(tools, msg, agent=agent, overrides=overrides,
                                intent=(decision or {}).get('intent') or 'compare')


def _intent_formula(tools, msg, agent=None, overrides=None, decision=None):
    if '伤害' not in msg:
        return None
    return {'reply': '伤害计算公式一图流如下，对照即可；要算具体数值请输入「算伤害」发起计算。',
            'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
            'guide_files': [], 'guide_lib': '', 'ok': True,
            'top_image': dict(_CALC_FORMULA_IMAGE)}


def _intent_defer(tools, msg, agent=None, overrides=None, decision=None):
    """状态型意图暂交给 route_work 的确定性参数解析阶段。"""
    return None


# 所有主意图都登记在同一张表中。处理器返回 None 仅表示还需要状态/参数解析，
# 不代表重新猜测另一个意图；LLM 分类失败时才启用后面的规则兼容层。
_INTENT_HANDLERS = {
    'calc': _intent_defer,
    'formula': _intent_formula,
    'favor': _intent_defer,
    'equip': _intent_equip,
    'guide': _intent_guide,
    'theme_intro': _intent_theme_intro,
    'encounter': _intent_defer,
    'pvp': _intent_defer,
    'query': _intent_query,
    'chat': _intent_defer,
    'capability': _intent_capability,
    'scout': _intent_scout,
    'exp': _intent_exp,
    'compare': _intent_compare,
    'evaluate': _intent_compare,
}


def _dispatch_intent(tools, overrides, msg, agent, decision):
    """统一意图分发入口；所有自然语言主意图只从这里选择处理器。"""
    if not decision:
        return None
    handler = _INTENT_HANDLERS.get(decision.get('intent'))
    if handler is None:
        return None
    return handler(tools, msg, agent=agent, overrides=overrides, decision=decision)


def _favor_dispatch(tools, overrides, msg, _li_is_fn=None):
    """好感度计算分支：固定公式输出，不走 LLM（防编数字）。返回含 favor_form 标记的 dict 或 None。

    显式入口（工具箱菜单「好感度」发送的「好感度计算」）与 favor_pending 追问上下文都走这里。
    仅当消息确实属于好感度计算（含显式「好感度计算」或 favor_pending 追问且非解释类）才返回非空结果。
    """
    _favor_explain = re.search(r'有什么用|什么意思|什么效果|怎么(?:回事|来的?|运作)|机制|介绍', msg)
    # 工具箱「好感度」菜单发送「好感度计算」为显式计算入口：即便 LLM 将"计算"误判为 calc/公式
    # 意图，也强制进入好感分支并弹出网页表单（用户 2026-10-01 反馈：点了只回文字不弹窗）。
    # 仅当同时含「好感」+「计算」才强制，避免「查询好感度」等查询被误拦截进计算。
    _favor_force = ('好感' in msg and '计算' in msg)
    if not (('好感' in msg or overrides.get('favor_pending')) and not _favor_explain
            and (_favor_force or (_li_is_fn and _li_is_fn('favor')))):
        return None
    # 追问上下文中出现其它明确工作意图 → 放弃好感追问，放行后续路由
    if overrides.get('favor_pending') and '好感' not in msg \
            and re.search(r'查|攻略|伤害|遭遇战|模拟|pvp|boss|装备|天气|活动|保存|组队|面板|技能|天赋', msg):
        overrides['favor_pending'] = None
        return None
    tool = _tool(tools, 'calc_favor')
    if not tool:
        return {'reply': '好感度计算工具当前不可用。', 'tool_outputs': [], 'suggestions': [],
                'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    p = {}
    msg_f = msg.replace('熟悉', '相识')   # 口语同义词归一化（"熟悉55%到永恒"）
    # 长词优先（"至爱"须先于"爱"匹配，否则级别名被截断）
    _STAGE_ALT = '陌生|相识|亲近|喜欢|誓约|依偎|守护|至爱|铭心|永恒|爱'
    # 目标级别（"到誓约"/"刷到铭心"），可带目标进度（"到誓约50%"）→ 提取后从句中移除，避免混入当前百分比
    # 进度数字没带百分号也视为百分比（"到誓约50"= 到誓约50%）
    mtt = (re.search(rf'(?:到|刷到|冲到|直至|直到)\s*({_STAGE_ALT})\s*(\d+(?:\.\d+)?)\s*(?:%|％|百分之)?', msg_f)
           or re.search(rf'(?:到|刷到|冲到|直至|直到)\s*({_STAGE_ALT})\s*百分之\s*(\d+(?:\.\d+)?)', msg_f))
    if mtt:
        p['target_stage'] = mtt.group(1)
        p['target_percent'] = float(mtt.group(2))
        msg_f = msg_f.replace(mtt.group(0), ' ')
    else:
        mt = re.search(rf'(?:到|刷到|冲到|直至|直到)\s*({_STAGE_ALT})', msg_f)
        if mt:
            p['target_stage'] = mt.group(1)
    # 当前级别："现在/目前/当前 是/在 X"、"喜欢30%" 相邻形态、句尾 "现在喜欢"
    ms = (re.search(rf'(?:现在|目前|当前)(?:是|在)?\s*({_STAGE_ALT})', msg_f)
          or re.search(rf'({_STAGE_ALT})\s*[^\d%]{{0,4}}\d+(?:\.\d+)?\s*%', msg_f)
          or re.search(rf'({_STAGE_ALT})(?:阶段)?(?:了|啦|之间|左右)?$', msg_f))
    if ms and ms.group(1) != p.get('target_stage'):
        p['current_stage'] = ms.group(1)
    # 该级别内进度百分比（"进度30%" / "好感50%" / "百分之30"；无%号也视为百分比：
    # "进度30"、"好感50"；"好感50点" 是礼物点数不是进度——捕获后校验尾部非"点"，
    # 防止正则回溯把 50 吞成 5；"喜欢0" 按级别名后数字识别）
    _mp_candidates = [
        re.search(r'(?:进度|好感)[^\d%]{0,4}(\d+(?:\.\d+)?)\s*(?:%|％)?', msg_f),
        re.search(r'(\d+(?:\.\d+)?)\s*%', msg_f),
        re.search(r'百分之\s*(\d+(?:\.\d+)?)', msg_f),
    ]
    mp = next((m for m in _mp_candidates
               if m and msg_f[m.end():m.end() + 1] != '点'), None)
    if not mp:
        mn = re.search(rf'({_STAGE_ALT})\s*[^\d%]{{0,2}}(\d+(?:\.\d+)?)', msg_f)
        if mn and mn.group(1) == p.get('current_stage'):
            mp = mn
    if mp:
        p['current_percent'] = float(mp.group(2) if mp.lastindex and mp.lastindex >= 2 else mp.group(1))
    # 百分比缺省按 0% 算（"现在喜欢，到誓约"=喜欢 0% 起）
    p.setdefault('current_percent', 0.0)
    # 礼物：品质（白/绿/蓝/紫/金，"金色礼物"）、每件点数（"礼物加3点"）
    # 或仅说"算礼物/要多少礼物"——礼物按阶段固定品质输出，品质词只是触发标记
    mc = re.search(r'(白|绿|蓝|紫|金)色?(?:的)?礼物', msg)
    if mc:
        p['gift_color'] = mc.group(1)
    # 礼物模式：全用金色礼物（gift_mode=gold）或低级礼物梯度计算（按阶段固定品质，默认）。
    # 「全用金色礼物」同时会命中上面的品质正则（金色礼物）触发礼物段输出
    if re.search(r'全用?金|全金', msg):
        p['gift_mode'] = 'gold'
    elif re.search(r'低级礼物|梯度', msg):
        p['gift_points'] = -1   # 触发礼物段输出（按阶段梯度品质）
    mg = (re.search(r'礼物[^\d]{0,6}(\d+(?:\.\d+)?)\s*点', msg)
          or re.search(r'(\d+(?:\.\d+)?)\s*点[^，。]{0,4}礼物', msg))
    if mg:
        p['gift_points'] = float(mg.group(1))
    if 'gift_color' not in p and 'gift_points' not in p and re.search(r'(算|需要多少|要多少|用多少)[^，。]{0,4}礼物|礼物.{0,6}(需要|要|用)多少', msg):
        p['gift_points'] = -1   # 仅触发礼物段输出（按阶段固定品质），非真实点数
    # 出击位置与星级
    if re.search(r'非旗舰|僚舰|不带旗舰', msg):
        p['position'] = 'non_flagship'
    mstar = re.search(r'([123])\s*星', msg)
    if mstar:
        p['stars'] = int(mstar.group(1))
    # 必须说清当前好感度级别（百分比可省略），缺级别才引导补全（不算、不编）。
    # 引导不走文本多轮：返回 favor_form 标记，前端自动弹出「好感度计算器」网页表单
    #（用户 2026-09-10 指示「这里不在回复中回答，而是调用网页工具」）；
    # 文本补参路径（favor_pending + "现在喜欢，进度30%"）保留兼容，关掉表单仍可打字
    if 'current_stage' not in p:
        overrides['favor_pending'] = True   # 记住追问上下文，下一轮短句可直接补参
        return {'reply': ('请填写当前好感度级别（进度不填按 0% 算）——已为你打开好感度计算器。\n\n'
                '好感度说明（每级独立池模型：只显示级别 + 该级内进度百分比）：\n'
                '· 级别与每级需求池：陌生5 / 相识10 / 亲近25 / 喜欢50 / 爱(已满，需结婚) / '
                '誓约60 / 依偎70 / 守护80 / 至爱90 / 铭心100 / 永恒(MAX)\n'
                '· 出击：3星 旗舰0.15、非旗舰0.05；2星 旗舰0.10；1星 旗舰0.10(非旗舰0) 点/场；'
                '3倍好感活动 ×3；沉没不涨好感\n'
                '· 只挂秘书舰：每天 +2.8 点；答题：每次 +2 点\n'
                '· 礼物按阶段固定品质：白(陌生)/绿(相识)/蓝(亲近)/紫(喜欢)/金(誓约起)，每件 1/2/3/4/5 点'),
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_lib': '', 'guide_files': [], 'ok': True,
                'favor_form': True}
    overrides['favor_pending'] = None       # 参数齐全，清除追问标记
    try:
        out = tool.call(p)
        text = out if isinstance(out, str) else str(out)
        return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    except Exception:
        return {'reply': '好感度计算暂时出错，稍后再试。', 'tool_outputs': [], 'suggestions': [],
                'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}


def route_work(tools, overrides, msg: str, agent=None, allow_intent_llm=True, update_recent=True):
    """工作类意图路由。返回固定回复 dict 或 None（交回 LLM 闲聊）。
    agent 用于天气定位等。"""
    msg = (msg or '').strip()
    msg = _resolve_pronoun(msg, overrides)
    # 显式「好感度计算」入口（工具箱菜单「好感度」）：先于一切意图分发与早期 return，直接进好感分支，
    # 避免被 _dispatch_intent 误判为 calc 而走伤害计算（用户 2026-10-01 反馈：点了只回文字不弹窗）。
    if '好感' in msg and '计算' in msg:
        _r = _favor_dispatch(tools, overrides, msg)
        if _r is not None:
            return _r
    # 上下文继承记忆：滚动保存最近几轮用户输入（供意图分类/指代/追问判断）
    _recent_prev = list((overrides or {}).get('_recent') or [])
    if update_recent and msg and overrides is not None:
        overrides['_recent'] = (_recent_prev + [msg])[-6:]

    # 0. 违规/禁区输入（违反大模型底层规则）：按固定话术回复，但标记 no_history——
    # web_app 不把该轮写入会话历史，后续对话继续之前的上下文、不中断。
    # 与"服务器问题"区分——只有网络/API 等连接异常才属于服务器层面（如实说明）。
    if _BANNED_RE.search(msg):
        return {'reply': '与阿卡莉无关，不回答。', 'no_history': True,
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 0. 未实装舰名拦截（底层规则）：输入包含尚未实装的舰灵名 → 直接回复未实装，
    # 不调用任何工具、不进计算流程（避免把未实装舰送进数据查询/伤害计算）。
    m_unimpl = _UNIMPLEMENTED_SHIP_RE.search(msg)
    if m_unimpl:
        return {'reply': f'《{m_unimpl.group(0)}》暂未实装，指挥官。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 0. 舰船「用途 / 就业」问答（用户口径 2026-10-03）：在意图分发前确定性处理 ——
    #    否则「X 值得练吗」会被 evaluate（_handle_ship_compare）抢走、「X 有什么用」会被
    #    末端 RAG 兜底随机发挥。计算流程进行中不打断（放行给原流程）。
    if not overrides.get('calc_state'):
        _use_r = _handle_ship_use(tools, msg)
        if _use_r is not None:
            return _use_r
        # 0b. 角色「天赋怎么点」（用户口径 2026-10-04）：点名角色 → 直读底层（推荐点法 +
        #     天赋原文），不给通用套路；放这里是为了抢在攻略 RAG 之前。
        _tal_r = _handle_talent_ask(tools, msg)
        if _tal_r is not None:
            return _tal_r

    # 0. 计算流程排他（工具流程中禁止切闲聊/其他工具）：
    # calc_state 激活时（计算进行中），输入默认进 handle_calc 计算流程，
    # 用户需按「结束本轮」按钮（web_app 顶层处理）退出；异常状态兜底清除防卡死。
    # 例外（一律放行到下面的分支）：①舰名列表（查角色面板）②组队 ③结束本轮
    # ④**明确的其他工具指令**（经验值/索敌/好感度/计算工具菜单）——否则「算经验值」会被
    #    吞进伤害计算、把工具词当攻击方舰名解析 → 「攻击方「经验值」无法识别」（用户口径 2026-09-14）。
    _tool_switch = _is_tool_switch(msg)
    # 中止词：计算流程（含填表等待期）里用户明确想退出时，**不依赖按钮**也能退出
    # （用户口径 2026-09-24：说「我没让你算伤害啊」被当成攻击方角色名，且退不出来）。
    # 收窄两处：① 仅在 calc_state 激活时判定（不影响正常对话）；
    #            ② 不含单独的「结束」二字（避免「活动什么时候结束」被误伤）。
    if overrides.get('calc_state') and re.search(
            r'取消|算了|不填了|不做了|不玩了|不算了|不计算了|退出计算|结束计算|结束本轮|'
            r'换个话题|换个问题|我没让你算|没让你算|别算了|谁让你算', msg):
        _reset_calc(overrides)
        overrides.pop('favor_pending', None)
        return {'reply': '好，已结束本轮计算。', 'tool_outputs': [], 'suggestions': [],
                'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '',
                'ok': True}
    if overrides.get('calc_state') and '组队' not in msg and '结束本轮' not in msg \
            and not _is_ship_list_cmd(msg) and not _tool_switch:
        try:
            r = handle_calc(tools, overrides, msg)
        except Exception:
            overrides['calc_state'] = None
            r = None
        if r is not None:
            return r

    # 工具切换：结束进行中的计算流程（清 calc_state / buffs / 面板 / 天赋等）。
    # 否则后续输入（如经验表单提交串「经验计算 等级：45 当前经验：3200」）仍会被
    # calc_state 吞进伤害计算流程。
    if _tool_switch and overrides.get('calc_state'):
        _reset_calc(overrides)
        overrides.pop('favor_pending', None)

    # 0. 每日事务（今日工作/事务/待办）：后端固定输出，不走 LLM。
    # 必须早于「生活话题硬兜底」——该兜底会把含「工作」的输入误判为真实生活诉求交回闲聊。
    # 随机抽 3 项、活动必选（活动名读自活动日历），并改回「我会辅助你处理」，不再追问优先项。
    if _DAILY_WORK_RE.search(msg):
        return _handle_daily_work()

    # 0. 计算工具菜单（用户口径 2026-09-13）：悬浮球「计算工具」→ 回复中弹窗子选项
    # （角色伤害/好感度/索敌计算/经验值计算/模拟组队）。必须早于 calc 门控——「计算工具」含「计算」。
    if re.search(r'计算工具|计算菜单', msg):
        return _handle_calc_menu()

    # 0. 索敌计算（用户口径 2026-09-14）：必须早于 calc 门控——
    # 「计算索敌 1000」含「计算」会被 _calc_strong 抢进伤害计算流程，需在此先拦下。
    # Mode1：输入数值 → T优/不T劣 索敌线；Mode2：选 6 角色 → 最高索敌求和下取整。
    _scout_r = _handle_scout(msg, agent=agent)
    if _scout_r is not None:
        return _scout_r

    # 0. 剧情类问法（用户口径 2026-09-24）：角色问剧情 → 给【剧情榜】；主线/活动 →
    # 建议指挥官自行体验。必须早于攻略检索，避免搜到剧情相关正文造成剧透。
    _story_r = _handle_story(msg)
    if _story_r is not None:
        return _story_r

    # 0. 经验值计算（用户口径 2026-09-14）：同样含「计算」需早于 calc 门控。
    # 输入当前等级+经验 → 剩余经验；再给出战经验 → 出击场数 / 三倍场次 / ge 晶体数量。
    _exp_r = _handle_exp_calc(msg)
    if _exp_r is not None:
        return _exp_r

    # ---- LLM 意图分类前置（所有输入先经 LLM 分析，正则分支降级为兜底）----
    # 分类成功 → _li_is(*kinds) 按意图门控各分支，防止正则死板误路由；
    # 分类失败/超时 → 全放行走原正则链路（可用性兜底）；
    # 流程态输入（计算轮菜单/表单、好感补参追问）是结构化状态机输入，不过 LLM 分类。
    if overrides.get('calc_state') or overrides.get('favor_pending') or overrides.get('pvp_lineup_pending'):
        _li = None
        _li_gate = True
    elif allow_intent_llm:
        _li = _llm_intent_classify(msg, recent=_recent_prev)
        _li_gate = _li is None
    else:
        # 调用方显式关闭意图分类（如"明确工具流程"的续接）→ 只跑确定性兼容规则。
        # 注意（用户口径 2026-09-28）：默认**允许多次 LLM 意图分析**，不再限制为一次。
        _li = None
        _li_gate = True

    def _li_is(*kinds):
        return _li_gate or (_li or {}).get('intent') in kinds

    # 分类服务不可用时的生活话题兜底；分类成功时以模型分析为准。
    if _li is None and _LIFE_TOPIC_RE.search(msg) and not re.search(
            r'攻略|查询|查一下|查查|帮我查|查下|面板|数据|技能|天赋|装备|伤害|算|角色|舰灵|舰娘|'
            r'boss|遭遇战|pvp|配装|阵容|推荐|怎么打|怎么玩|练级|培养|加点|索敌', msg):
        return None

    # 唯一的自然语言意图分发点：模型成功时完全采用模型结论；只有模型失败时，
    # compare/evaluate 才使用共享词表作降级判断。规则不再与模型结果竞价。
    # ============ 计算意图的前置识别（必须早于意图分发/检索）============
    # 用户口径 2026-09-24：问「减伤25%和加伤害25%哪个收益高」被误判成算伤害且退不出来。
    # 实测还有反向问题：「光辉打胜利」被 LLM 判成 data_lookup、「算一下A和B哪个伤害高」
    # 判成 knowledge_qa，于是进了检索分支回「没能从攻略库找到内容」—— 所以计算专属句式
    # 必须在这里拦下，不能等下面的 calc 判定（那时已被分发走了）。
    #   ① _calc_theory：理论/比较/抱怨语境 → 其中的「伤害」二字不算计算请求；
    #   ② _calc_hard  ：计算专属句式（X打Y / dps / 算…伤害）→ 即使 LLM 判闲聊也照进计算。
    _calc_theory = re.search(
        r'收益|理论|性价比|等效|相比|比较|对比|差距|'
        r'没让你|别算|不要算|凭什么|为什么|怎么会|谁让你', msg)
    _calc_hard = (not _calc_theory) and bool(
        re.search(r'[\u4e00-\u9fa5A-Za-z0-9·]{1,12}(?:打|攻击)[\u4e00-\u9fa5A-Za-z0-9·]{1,12}', msg)
        or re.search(r'(?:^|[\s，,])打[\u4e00-\u9fa5A-Za-z0-9·]{1,8}$', msg)
        or 'dps' in msg.lower()
        or re.search(r'算[一二]?[下算]?[^，。！？]{0,8}(?:伤害|输出|数值)', msg))

    _decision = _li
    if _decision is None:
        _fallback_intent = intents.classify_by_rules(msg)
        _decision = {'intent': _fallback_intent} if _fallback_intent else None
    # 计算专属句式：跳过意图分发，直接交给下方计算判定
    _intent_result = None if _calc_hard else _dispatch_intent(tools, overrides, msg, agent, _decision)
    if _intent_result is not None:
        return _intent_result

    # 0.55 角色分类筛选 / 按舰船集查技能CD → **LLM 意图驱动**（query 类）+ 维度关键词辅助提取；
    # 底层 data/raw 真实数据直出，禁止 LLM 编造。必杀技/通常技/支援技 CD 类问法优先走
    # _handle_ship_attr_query（确定性抽 CD，支持继承上一批筛选代词），避免落到 RAG 拼无关列表。
    _attr_q = _is_ship_attr_query(msg)
    if _li is None and (_is_ship_attr_filter(msg, tools, agent) or _attr_q[0]):
        if _attr_q[0]:
            r = _handle_ship_attr_query(tools, msg, agent=agent, overrides=overrides)
            if r is not None:
                return r
        r = _handle_ship_filter(tools, msg, agent=agent, overrides=overrides)
        if r is not None:
            return r

    # 0. 攻略篇名精确匹配：输入能唯一对应某篇攻略（如 装备一图流→装备速查表）→ 直接给那一篇
    #（优先于主题介绍/配装咨询/多篇列表等所有路由，避免被更早分支拦截）
    try:
        from tools.knowledge import _exact_guide_doc, _single_guide_lines
        import os as _os
        from tools.knowledge import KB_DIR as _KB_DIR
        _docs = []
        for _d in _load_guide_kb().get('documents', []):
            _f = (_d.get('file') or '').strip()
            if _f and _os.path.isfile(_os.path.normpath(_os.path.join(_KB_DIR, _f))):
                _docs.append(_d)
        _exact = _exact_guide_doc(msg, _docs)
    except Exception:
        _exact = None
    if _li is None and _exact is not None:
        text = _single_guide_lines(_exact)
        return _guide_reply_from_text(text, _exact.get('title') or '')

    # ---- PVP 角色推荐：争锋参考阵容统计（后端固定输出，优先于攻略 RAG，防答非所问）----
    # 仅拦"推荐/练谁"类诉求；模拟/组队/信物/伤害语境不拦（各归各的既有流程）
    if re.search(r'pvp|争锋|竞技场', msg, re.I) \
            and re.search(r'角色|舰灵|阵容|推荐|练谁|用谁|哪些', msg) \
            and not re.search(r'模拟|组队|伤害|计算|信物|遭遇', msg, re.I):
        _ptool = _tool(tools, 'pvp_role_recommend')
        if _ptool:
            out = _ptool.call({'query': msg})
            text = out if isinstance(out, str) else str(out)
            return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 意图优先级：
    # ①明确计算意图（含"算/伤害/X打Y"，且非解释性问句）**优先于攻略**——避免"计算伤害"
    #   被攻略 pattern（数值|公式|面板|计算 命中"计算"）误拦成攻略；
    # ②攻略（本地 query_patterns 命中或含攻略词）；
    # ③查询角色 / 装备 / 天气。
    _explain_q = re.search(r'怎么算|如何算|怎么计算|机制|公式|原理|怎么打|怎么玩|划算|怎么强化|怎么搭配|怎么升级|怎么培养', msg)
    # ---- 模糊意图：不反问选择，而是结合知识库分析意图并直接提供相关信息 ----
    _bare = msg.strip().rstrip('！!？?。，,、 ')
    if _li is None and _bare in ('装备', '装备啊', '装备呢'):
        # 具体装备查询：停用 query_equipment，直接给「常用装备速览」表
        return _common_equip_table_reply()
    if _li is None and _bare in ('模拟', '模拟一下', '模拟下'):
        return {'reply': '模拟功能有两种：\n'
                         '①**遭遇战模拟**：6人组队打拉古兹boss，白天+时间轴排轴，求理论最高伤害（黑夜自动=150-白天）。'
                         '输入「遭遇战模拟 boss：嘉波 白天：60 前排：A B C 后排：D E F」即可。\n'
                         '②**模拟组队**：6人打1个防守角色，不分前后排、不算支援技，按CD自动循环算定时总伤害。'
                         '输入「模拟组队 防守：俾斯麦 时间：60 前排：A B C 后排：D E F」即可。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '', 'ok': True}
    if _li is None and _bare in ('boss', 'BOSS', 'boss呢'):
        kb = _tool(tools, 'kb_search')
        info = ''
        if kb:
            out = kb.call({'query': '拉古兹 boss 图鉴 打法', 'max_results': 2})
            info = (out if isinstance(out, str) else str(out))
        return {'reply': 'boss 主要来自**拉古兹阵营**（遭遇战敌人）。当前知识库收录 11 个遭遇战boss：'
                         '嘉波/巴巴托斯/阿丝莫德/亚斯塔露/列拉金/沙克斯/夕茉莉/河豚王/拜蒙/雅蒙/布耶尔，'
                         '各有装甲/机动/幸运/对空面板与建议弹种。\n\n' + (info + '\n\n' if info else '') +
                         '【算某个boss伤害】输入「遭遇战模拟 boss：嘉波 白天：60 前排：…」；'
                         '【boss详细攻略】已为你打开拉古兹专题，可按分类翻阅。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '拉古兹专题', 'ok': True}
    # ---- 遭遇战 BOSS 咨询（配队/打法/阵容）→ 结构化建议管线（归类→定向取文→LLM 综合）----
    # 必须**早于**下方 0b「配装」分支：否则「遭遇战阿斯莫德配装配队有吗」会被
    # _li_is('equip') + 「配装」关键词抢进配装目录的固定话术（实测踩过）。
    _ba_early = _handle_boss_advice(tools, msg) if _li is None else None
    if _ba_early is not None:
        return _ba_early

    # ---- 遭遇战模拟表单（含 前排/后排/白天 参数）→ 确定性拦截，必须在 tips 结构化层之前 ----
    # 否则「遭遇战模拟 boss：阿丝莫德 …」会被 tips 键命中（阿丝莫德应对）抢成小技巧，
    # 模拟工具跑不起来。用户铁律：明确工具不得被 LLM/检索干扰 → 关键词驱动、不依赖意图分类。
    _es = _handle_encounter_sim_params(msg, tools)
    if _es is not None:
        return _es

    # ---- 确定性工具区（优先级高于下方 小技巧/检索层）----
    # 用户铁律：所有可判定明确的工具调用，绝不能被 小技巧(RAG) 等检索层拦截。
    # 故凡带确定性签名（弹种系数/概率/克制查表）的工具分支一律排在 tips 结构化层之前；
    # tips 只兜底「纯问句/键短语」，不抢工具。

    # ---- 弹种机制概率类问题 → 弹种速查工具（确定性数值）→ 回落攻略问答 ----
    # 前置到 调情放行/闲聊 之前：机制问句（如「高爆战列对重甲的点燃概率是多少」）
    # 无论 LLM 意图分类结果如何，都必须答确定性数值（10%），杜绝落进闲聊 LLM 口胡
    # （曾出现 LLM 答「基础概率0%」）。且须先于舰灵条件筛选：
    # 「高爆战列对重甲燃烧概率」会被误解析成 弹种/舰种 筛角色；
    # 「哪些船能点燃」类无概率词且无目标甲，不受影响。工具三要素不全时回落攻略问答。
    _prob_q = re.search(r'概率|几率|概率是|几率是|点燃率', msg)
    # 变量名注意：这里是「弹种/护甲机制词」（本段专用），与下方伤害类型判定处的 _mech_q
    # （`什么甲|什么弹|克制|甲弹|弹种|打什么`）**语义不同**。此前两处同名复用、后者覆盖
    # 前者，读代码极易误判为同一含义，故此处改名为 _mech_word_early。
    _mech_word_early = re.search(r'点燃|燃烧|击穿|过穿|穿甲|高爆|通常弹|弹种|轻甲|中甲|重甲|护甲', msg)
    _armor_q = re.search(r'会不会|能不能|是否会|会点燃|能点燃|点燃吗|点燃几率', msg) \
        and re.search(r'轻甲|中甲|重甲|护甲|什么甲', msg)
    if (_prob_q and _mech_word_early) or _armor_q:
        _atool = _tool(tools, 'ammo_mech_qa')
        if _atool:
            try:
                _aout = _atool.call({'query': msg})
            except Exception:
                _aout = None
            if _aout:
                text = _aout if isinstance(_aout, str) else str(_aout)
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
        r = _route_guide(tools, msg)
        if r is not None:
            return r

    # ---- 弹种机制追问上下文拼接（用户反馈 2026-10-07）：上轮问过 克制/系数（overrides['last_mech'] 已记录），
    # 本轮只说「重甲呢/那中甲呢/轻甲？」这类缺武器词的短追问 → 用上轮武器+弹种 + 本轮护甲 + 上轮指标
    # 重组完整问句再查表，避免丢失原意落进 LLM（曾把"重甲呢"答成穿甲概率表）。
    _lm = (overrides or {}).get('last_mech') or {}
    _flw = re.search(r'轻甲|中甲|重甲', msg)
    if _lm and _flw and not re.search(r'高平|两用|三式|近战|雷击|鱼雷|轰炸|驱逐|轻巡|重巡|战列|穿甲|高爆|通常弹|弹种', msg):
        # 仅当整句基本就是「(那?)XX甲(呢/？)」式短追问：去掉甲词与 呢/那/？/对/的 后不应有残余
        _rest = re.sub(r'轻甲|中甲|重甲|呢|那|？|\?|对|的', '', msg).strip()
        if not _rest:
            from tools.knowledge import _mech_weapon_ammo_label, _mech_metric_word
            _wa = _lm.get('weapon_ammo') or _mech_weapon_ammo_label(msg)
            _mt = _lm.get('metric') or _mech_metric_word(msg)
            if _wa:
                _syn = _wa + '对' + _flw.group() + _mt
                _mtool = _tool(tools, 'ammo_mech_qa')
                if _mtool:
                    try:
                        _sout = _mtool.call({'query': _syn})
                    except Exception:
                        _sout = None
                    if _sout:
                        text = _sout if isinstance(_sout, str) else str(_sout)
                        return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 克制/修正/系数类问题 → 弹种速查工具（确定性数值查表）----
    # 用户禁止乱编数据：克制系数曾落进 LLM 口胡（「高平两用炮对重甲 0.8」，真实按表 0.6）。
    # 凡 甲类 + 武器/弹种词 + 克制/修正/系数 → 先走 ammo_mech_qa 直查 mappings 同源表；
    # 工具解析不出（如含具体舰名的计算题）回落后续正常路由，不空转。
    _gear_q = re.search(r'高平|两用|三式|近战|雷击|鱼雷|轰炸|驱逐|轻巡|重巡|战列|穿甲|高爆|通常弹|弹种', msg)
    _armor_w = re.search(r'轻甲|中甲|重甲', msg)
    if _armor_w and _gear_q and re.search(r'克制|修正|系数', msg) and not re.search(r'哪些|谁|什么舰|哪个角色', msg):
        _mtool = _tool(tools, 'ammo_mech_qa')
        if _mtool:
            try:
                _mout = _mtool.call({'query': msg})
            except Exception:
                _mout = None
            if _mout:
                text = _mout if isinstance(_mout, str) else str(_mout)
                # 记录弹种机制上下文（武器+弹种+指标），供后续「重甲呢」式追问拼接原意
                try:
                    from tools.knowledge import _mech_weapon_ammo_label, _mech_metric_word
                    if re.search(r'克制|修正|系数', msg):
                        overrides['last_mech'] = {'weapon_ammo': _mech_weapon_ammo_label(msg),
                                                  'metric': _mech_metric_word(msg)}
                except Exception:
                    pass
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 舰名 + 甲 + 克制/系数/伤害 → 弹种速查（覆盖「光辉·改对重甲伤害」类）----
    # 舰名出现即进（航系角色也能进，由 _ship_modifier_reply 按通常技判轰航/雷航）；
    # 仅「X 对 Y 甲 × 系数」这类计算题走此分支，排除列传式问句
    if _armor_w and re.search(r'克制|修正|系数|伤害|打|增伤', msg) and not re.search(r'哪些|谁|什么舰|哪个角色', msg):
        from tools.knowledge import _match_ship_name, _ammo_mech_modifier as _amm_mod
        if _match_ship_name(msg):
            _mout = _amm_mod(msg)
            if _mout:
                text = _mout if isinstance(_mout, str) else str(_mout)
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 【当期活动攻略】统一话术（用户口径 2026-09-23）：优先于 tips/活动咨询/攻略检索 ----
    # 表单签名保护与 tips 闸同口径：结构化工具提交消息不拦，交还下方工具分支。
    if _li is None and not re.search(r'(?:前排|后排|防守|白天)\s*[：:]', msg):
        _agr = _activity_guide_reply(msg)
        if _agr:
            return {'reply': _agr, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 时装/皮肤（用户口径 2026-09-28）：信息库未收录；含券走向量库，其余固定引导 ----
    _skin_r = _handle_skin_query(tools, msg)
    if _skin_r is not None:
        return _skin_r

    # ---- 攻略检索「结构化优先」：小技巧键命中 → 原文直出（不调 LLM、不走向量）----
    # 紧接 boss 咨询管线（同为结构化层），但**早于**下方所有模糊分支（kb_search 泛化、
    # 配装目录话术、向量召回）：实测「甲弹克制关系」「舰灵退役」「多倍经验」此前会被
    # 泛化回答成《拂晓》游戏简介、「88自选」甚至误判成告别语——都是因为没有确定性键层。
    # 只有当 tips 键命中不了（返回 None）时才放行后续路径，故对既有问答无影响。
    # **表单签名保护**：含 前排/后排/白天/防守 表单参数（结构化工具提交）一律跳过 tips，
    # 交还下方工具分支——防「遭遇战模拟 boss：阿丝莫德 前排：…」被阿丝莫德应对抢成小技巧。
    if _li is None and not re.search(r'(?:前排|后排|防守|白天)\s*[：:]', msg):
        _tips_r = _handle_tips(msg)
        if _tips_r is not None:
            return _tips_r

    # ---- 弹种机制 / 游戏简介类 → 实时检索攻略向量（不硬编码，找不到就不知道）----
    # 用户铁律：游戏简介须实时从攻略向量读取，不得固定回复；向量无命中即「不知道」。
    # 排除 克制/系数/概率/对X甲 等确定性数值问（那些走 ammo_mech_qa 查表）。
    if _li is None and re.search(r'弹种|穿甲弹|高爆弹|通常弹', msg) and re.search(r'机制|是什么|介绍|原理|区别|怎么[运作用]|概念|有哪|怎么区分', msg) \
            and not re.search(r'克制|系数|修正|概率|几率|点燃率|对.{1,4}甲|击穿|过穿|点燃|燃烧', msg):
        _g = _handle_guide_qa(tools, msg)
        if _g is not None:
            return _g
        return {'reply': '弹种机制相关的说明阿卡莉暂时没在攻略库里检索到，这个问题我没法确定回答。\n\n'
                         '建议指挥官自行查阅相关攻略（已为你打开攻略目录）。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '*', 'ok': True}

    # ---- 单体角色打 boss 伤害：不支持（boss 伤害仅存在于组队玩法）----
    # 「飞龙打boss」类误送单人计算流程 → 诚实引导到遭遇战模拟 / 模拟组队，不落「系统处理。」
    if re.search(r'打\s*boss|对\s*boss|boss\s*伤害|boss\s*输出', msg, re.I) \
            and not re.search(r'遭遇战|组队|模拟\s*pvp|pvp|副本', msg, re.I):
        return {'reply': '单个角色打 boss 的伤害暂不支持计算——boss 伤害只在**组队玩法**里：\n'
                         '①**遭遇战模拟**：6人打拉古兹boss（输入「遭遇战模拟 boss：嘉波 白天：60 前排：A B C 后排：D E F」）；\n'
                         '②**模拟组队**：6人打1个防守角色（输入「模拟组队 防守：俾斯麦 时间：60 前排：A B C 后排：D E F」）。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 调情/亲密放行（用户要求放宽判定，不做严格正则）----
    # 对象指向「你/阿卡莉/莉」本人 → 优先按调情/闲聊交回 LLM（符合人设则调情回应，越界则由
    # LLM 拒绝/无法回答，走「越界，禁止讨论」兜底）。仅排除明确工作意图词，避免
    # "你的大凤面板""你帮我查华盛顿"这类查询被误放行。
    if _li_is('chat') and re.search(r'(你|阿卡莉|莉)', msg) \
            and not re.search(r'(?<!检)(?<!考)查|算|攻略|配|面板|数据|属性|技能|天赋|装备|索敌|伤害|多少|'
                     r'(?:怎么|如何)(?:配|打|玩|算|练|强化|搭配|升级|培养|攻略|计算|办|弄)|'
                              r'遭遇战|模拟|pvp|boss|副本|保存|删除|我的船|我的角色|练级|榜|排名|'
                              r'哪些|谁|什么舰灵|哪个角色|推荐', msg):
        overrides['flirt_rounds'] = 3  # 进入调情语境，后续短句继续放行
        return None

    # ---- 省略主语的调情（"脱掉衣服让我康康胸部"）：动作/请求 + 身体部位 ----
    # 未写"你/阿卡莉"但语义指向秘书本人的调戏；排除舰灵/数据/攻略等语境防止误放行
    #（"舰灵胸部数据""哪些船护甲高"仍走工作路由）。
    if _li_is('chat') and re.search(r'(脱|扒|掀|撩|解|看|康康|摸|亲|吻|抱|蹭).{0,6}(衣|胸|身体|身|腿|臀|奶|肉|皮肤)'
                 r'|(让我|给我|我想).{0,6}(看|摸|亲|吻|抱|脱).{0,6}(胸|衣|腿|身|臀|奶)'
                 r'|(胸|奶|臀|腿|身材).{0,4}(好大|好挺|真大|好翘|好看|漂亮|性感|大吗|大不大|怎么样)', msg) \
            and not re.search(r'哪些|谁|什么|哪个角色|舰灵|舰娘|船|角色|比较|数据|面板|攻略|装备|'
                              r'计算|伤害|索敌|技能|天赋', msg):
        overrides['flirt_rounds'] = 3  # 进入调情语境，后续短句继续放行
        return None

    # ---- 主观评价/比较（用户口径 2026-09-28：改由 LLM 意图 subjective 判定，不再靠正则枚举）----
    # 钦定（用户指定）：**仅「身材」→ 萨拉托加**（信息库存在）；其余主观"比较"→ 固定"数据无法量化"。
    # 排除"怎么/攻略/配"等引导词（如"最强阵容怎么配"仍走攻略）。
    if not re.search(r'怎么|如何|攻略|配|搭配|阵容|算|伤害|推荐练', msg):
        if re.search(r'身材', msg) and _li_is('chat', 'subjective'):
            return {'reply': '不用问也知道——萨拉托加。',
                    'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
        # 主观"比较"：以 LLM 意图(subjective)为准；分类器不可用(_li is None)时才退化为极小兜底词表
        # （防闲聊链路幻觉编造角色名）。皮肤/时装 已由 _handle_skin_query 单独处理，不在此列。
        if (_li or {}).get('intent') == 'subjective' \
                or (_li is None and _SUBJ_FALLBACK_RE.search(msg)):
            return {'reply': '这种主观问题……数据无法量化，建议指挥官自行感受。',
                    'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 天赋反查：有XX天赋的角色 / 哪些角色有XX天赋 / 谁带XX天赋 ----
    #（置于技能条件筛选之前：避免「减伤」被 query_ships_by_skill 当技能 buff 条件截胡；
    #  也置于攻略路由之前：避免命中攻略 pattern "暴击率|幸运值|命中|机动|闪避|减伤"。
    #  仅识别"角色/谁/哪些"等反查句式，不干扰「查XX的天赋」单角色查询）
    talent_kw = _extract_talent_query(msg)
    if talent_kw and _li_is('query'):
        text = _query_text(tools, 'query_talent_ships', {'talent_name': talent_kw})
        if text is not None:
            return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 技能条件筛选（泛化）：任何「问舰灵群体 + 能力条件」→ 信息库检索 ----
    # 置于用户本地副本分支之前：防止「给…炮击」类条件查询被保存角色逻辑劫持；
    # 排除计算/攻略/配装/副本操作/求和/数值等意图；规则未命中且明显在问"哪些舰灵具备某能力"
    # 时，LLM 仅把条件翻译成严格校验的短 JSON（不生成查询内容），名单仍由信息库输出。
    # 反查句式（哪些/谁/有没有…）豁免 LLM 意图 gate：技能反查不因 intent 误判（guide/chat）
    # 而被攻略/闲聊截胡——如「哪些船带点燃伤害」应给名单而非攻略（曾误回 PVE 推荐表）。
    # 「伤害」不作硬排除（'带点燃伤害' 的伤害是效果词后缀）；但数值比较问法
    # （伤害高/多少/最高）不拦，保留原数值/计算语义。
    _skill_q_word = re.search(r'哪些|哪类|什么|谁|有没有|列举|有哪些|找找|筛', msg)
    _skill_cond_word = re.search(r'能|会|加|有|带|奶|盾|控|抗|回|提速|提升|强化|高爆|穿甲|减速|免伤|点燃|燃烧|灼烧', msg)
    if re.search(r'舰灵|舰娘|船|角色|驱逐|战列|航母|巡洋|维修|潜艇|重巡|轻巡|战巡|装母|轻母|'
                 r'重炮|水母|雷巡|航战|奶船|奶妈', msg) \
            and not re.search(r'怎么|如何|攻略|推荐|配|算|保存|更新|记录|我的|装备|面板|设置|缓存|删除|'
                              r'之和|相加|总和|加起来|总共|合计|一共|'
                              r'优先|值得|先练|练级|培养|毕业|强度|'
                              r'伤害?(?:高|低|多|强|最高|最多|多少)|\d', msg) \
            and (_li_is('query') or (_skill_q_word and _skill_cond_word)):
        # 属性/天赋筛选（舰种/尺寸/阵营/稀有度/改造状态/天赋，无技能效果词）→ 走 ship_filter，
        # 不抢去 query_ships_by_skill（后者无 refit/尺寸/完整性维度，会漏改造舰、混入中船）。
        if _is_ship_attr_filter(msg, tools, agent):
            _attr_app = _get_app_from_tools(tools, agent)
            if _attr_app is not None:
                from tools.knowledge import _ship_filter_text
                _attr_text = _ship_filter_text(msg, _attr_app)
                if _attr_text:
                    return {'reply': _attr_text, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
        from tools.knowledge import parse_skill_conditions, llm_parse_conditions
        _scond = parse_skill_conditions(msg)
        if not _scond and _skill_q_word and _skill_cond_word:
            _scond = llm_parse_conditions(msg)  # LLM 只翻译条件，不生成查询内容
        if _scond:
            text = _query_text(tools, 'query_ships_by_skill', {'conditions': _scond})
            if text is not None:
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 用户本地副本（保存/更新/装备面板/删除/查看，持久化到 user_data.json）----
    if re.search(r'保存角色|记录角色|我的角色|存角色|角色缓存|删除角色|移除角色|删除缓存|'
                 r'我的设置|更新面板|更新我的|装备面板|设置面板|面板增加|我的船|查看我的', msg) or \
       (('保存' in msg or '更新' in msg or '给' in msg) and
        re.search(r'炮击|雷击|航空|轰炸|鱼雷|命中|血量|装甲|对空|机动|幸运|索敌|装填', msg)):
        r = _handle_user_ship(tools, msg)
        if r is not None:
            return r
    # ---- 组队伤害计算优先：遭遇战模拟 / 模拟组队（不落入单人计算流程）----
    if '组队' in msg and not overrides.get('calc_state', {}).get('attacker'):
        # 组队建议/推荐/搭配 → 引导查相关攻略（非模拟）
        if re.search(r'建议|推荐|搭配|怎么组|如何组|怎么配|如何配|阵容|配置|求攻略|攻略|求教|求助|求推荐', msg):
            r = _route_guide(tools, msg)
            if r is not None:
                return r
            return {'reply': '组队建议相关攻略已展开，可按分类查看。',
                    'tool_outputs': [], 'suggestions': [],
                    'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '*', 'ok': True}
        # 已有明确子模式（遭遇战/boss/pvp/防守/前排参数）→ 交给下方组队分支
        if not ('pvp' in msg.lower() or ('防守' in msg or '前排' in msg or '遭遇战' in msg or 'boss' in msg)):
            return {'reply': '组队伤害计算：①遭遇战模拟（6人打boss，拉古兹+时间轴寻优最高伤害）；②模拟组队（6人打防守角色，无buff定时总伤害）。要哪种？',
                    'tool_outputs': [], 'suggestions': [
                        {'label': '遭遇战模拟', 'tool': 'encounter_simulator'},
                        {'label': '模拟组队', 'tool': 'simulate_pvp'}],
                    'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    # ---- 多角色求和：仅支持「索敌」之和（其他属性友好拒绝，避免交回 LLM 乱算） ----
    # 必须早于单角色查询分支，避免「丹阳+女灶神」被单角色逻辑截断
    _sum_kw = re.search(r'之和|相加|总和|加起来|总共|合计|一共|总共多少|总共是', msg)
    _sum_app = _tool(tools, 'query_scout_sum')
    _sum_app = _sum_app.cfg.get('app') if _sum_app else None
    _has_scout = _SCOUT_SUM_FIELD in msg
    _multi_names = _extract_multi_ship_names(msg, _sum_app, max_n=6) if _sum_app else []
    if _li_is('query') and (_has_scout or (bool(_sum_kw) and len(_multi_names) >= 2)):
        if len(_multi_names) >= 2 or (_sum_kw and _multi_names):
            if _has_scout:
                text = _query_text(tools, 'query_scout_sum', {'ship_names': _multi_names})
                if text is not None:
                    return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
                # 工具未注册：降级继续走其它分支
            else:
                text = '目前多位舰灵属性求和，仅支持「索敌」之和，其他属性暂不支持。'
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # ---- 「如何获得 / 怎么获取 XX」类获取方式问法 → 攻略向量库检索 ----
    # 必须先于下方「角色名模糊问句」分支：否则 _resolve_ship_query 会把 "如何获得女灶神"
    # 解析出船名并进 query_ship_info（返回面板/基本信息），绕开攻略QA向量库里已有的
    # 「XX 获取方法」答案（如「女灶神：88/168 氪金限定 SSR 自选，或建造概率获得」）。
    # 触发：含获取类关键词（获得/获取/入手/获取方式/获得方法…）+ 解析出船名，
    # 且不含查面板/技能/属性等数据问法（避免「查女灶神怎么获得」被误导向）。
    if re.search(r'获得|获取|入手|哪(?:里|儿)得|在哪得|获取方式|怎么拿到|怎么入手|获得方法|获取方法', msg) \
            and not re.search(r'查|面板|技能|天赋|属性|数据|详情|装备|阵容|强度|伤害|配装|推荐|怎么打|怎么玩|攻略|练级|培养|加点', msg):
        _acq_qt = _tool(tools, 'query_ship_info')
        _acq_app = _acq_qt.cfg.get('app') if _acq_qt else None
        _acq_names = _extract_multi_ship_names(msg, _acq_app, max_n=2) if _acq_app else []
        if _acq_names:
            r = _route_guide(tools, msg)
            if r is not None:
                return r

    # ---- 角色名录裸查询（"查角色/角色列表/所有角色/有哪些舰灵"，无具体船名）----
    # → 不输出底层文本，改为前端「查角色」面板（名单 + 阵营筛选 + 头像），用户口径 2026-09-13
    if _is_ship_list_cmd(msg):
        return {'reply': '角色名录已展开，按阵营筛选或直接选择想要的舰灵即可。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
                'ship_browser': True, 'ship_focus': ''}

    # ---- 角色名模糊问句（"光辉是啥/是什么/介绍一下"）→ 前端角色面板（不再在回复里塞底层信息）----
    canon = _resolve_ship_query(msg, tools)
    if canon:
        return _ship_panel_reply(canon, tools)
    # ---- 裸词昵称/角色名（"口罩拉菲"/"雪风改"）→ 前端角色面板 ----
    # 意图严格化：仅当 LLM 已判为 query 意图，或输入含查询硬信号词，
    # 才允许"仅凭船名/关键字"触发查询面板；否则交回 LLM（防"只因出现船名就查面板"）。
    if not any(k in msg for k in _THEME_INTRO) and '天气' not in msg and len(msg) <= 12 \
            and (_li_is('query') or _QUERY_INTENT_RE.search(msg)):
        qtool = _tool(tools, 'query_ship_info')
        canon = None
        if qtool:
            _app = qtool.cfg.get('app')
            nm = getattr(_app, 'raw_name_matcher', None) if _app else None
            canon = nm.match_alias_exact(msg) if nm else None
            if not canon and _app:
                canon = _app._load_ship_name_map().get(msg.strip(), '') or ''
        if canon:
            return _ship_panel_reply(canon, tools)
    # 0f. 【游戏内活动】咨询（有哪些活动/截止日期/注意事项/XX活动攻略）：后端固定输出，不走 LLM。
    # 必须先于攻略/玩法介绍分支：泛指问法（最近/近期/这次/新活动等）会被它们吸走。
    # 数据源＝游戏内活动表格（知识库/活动/活动日历.xlsx，实时读取），不让 LLM 凭训练知识编造。
    # **法定节假日不接受询问**（用户口径 2026-09-16）：只在当天由 _today_holiday 祝福，
    #   故此处**刻意不含** 节日/过节/法定节假日 等节假日触发词，也不会命中节假日名。
    # 触发：① 活动类问法（含泛指）；② 消息包含表格中的某个活动名 + 咨询意图词（如"夏日祭典攻略"）
    _act_kw = ''
    try:
        from tools.knowledge import load_calendar_names
        for _nm in load_calendar_names():
            if len(_nm) >= 2 and _nm in msg:
                _act_kw = _nm
                break
    except Exception:
        pass
    if _act_kw and not re.search(r'攻略|注意事项|结束|截止|什么时候|何时|时间|开始|开启|兑换|介绍|详情|怎么打', msg):
        # 含活动名但纯闲聊提及（无咨询意图词）→ 不拦截，交回后续流程/LLM
        _act_kw = ''
    if _act_kw or re.search(r'有(?:哪些|什么|没)[^，。\s]{0,6}活动|活动[^，。\s]{0,10}(?:什么时候|何时|结束|截止|开始|开启|时间|兑换)|(?:什么时候|何时)(?:结束|截止|开始|开)|活动的?(?:攻略|注意事项)|活动日历|兑换[^，。\s]{0,6}(?:时间|截止|结束|什么时候|何时)|(?:最近|近期|当前|这次|本次|进行中|正在进行的?)的?活动|新活动|活动预告|复刻', msg):
        tool = _tool(tools, 'query_activity')
        if tool:
            try:
                out = tool.call({'keyword': _act_kw})
                text = out if isinstance(out, str) else str(out)
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
            except Exception:
                return {'reply': '活动信息暂时无法获取，稍后再试。', 'tool_outputs': [], 'suggestions': [],
                        'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 0e-2. 具体装备查询：停用 query_equipment，直接给「常用装备速览」表（装备速查表 PNG）。
    # 必须先于玩法介绍/模糊口语分支（"装备有哪些/胡德的装备"会被 _handle_theme_intro 抢成
    # 「装备系统」介绍）；配装/搭配/装备面板/一键装备/装备推荐类不在此列（走攻略向量或原分支）
    if _li is None and '装备' in msg and not re.search(r'配装|搭配|装备系统|装备面板|一键装备', msg) \
            and not re.search(r'装备.*?(?:推荐|怎么穿|怎么选|怎么配|怎么带|如何选|怎么搭|怎么用)', msg):
        return _common_equip_table_reply()

    # ---- 玩法介绍类：非具体主题词/“XX是什么玩法” → 从知识库抽取介绍 + 判定性引导问题 ----
    # 伤害公式问句（"伤害计算公式是什么"）不当作玩法介绍，交下方公式拦截输出一图流
    if _li is None and _is_theme_intro_ask(msg, tools) and not ('伤害' in msg and _explain_q):
        # 上下文追问（无主语追问上轮角色）→ 转角色查询，不回落游戏简介
        _subj = _context_followup_target(msg, overrides, tools)
        if _subj:
            _r = _ship_panel_reply(_subj, tools)
            if _r is not None:
                return _r
            # 角色查询无结果 → 继续往下走（攻略/问答兜底），不进游戏简介
        else:
            r = _handle_theme_intro(tools, msg, _bare)
            if r is not None:
                return r
    # ---- 模糊口语问句（"争锋是啥/啥玩意/干嘛的"）→ LLM 意图分析，不靠正则补词 ----
    # 触发：主题词命中 + 无明确操作词 + 正则问句未接住 → 让 LLM 判断是 介绍/攻略/其他
    if _li is None and not re.search(r'攻略|模拟|伤害|查|配|数据|保底|排行|加点|多少|怎么|如何|推荐|阵容|天赋|boss|白天|黑夜|前排|后排|打|算', msg) \
            and any(k in msg for k in _THEME_INTRO):
        intent = _classify_fuzzy_intent(msg)
        if intent == '介绍':
            # 上下文追问（无主语追问上轮角色）→ 转角色查询，不回落游戏简介
            _subj = _context_followup_target(msg, overrides, tools)
            if _subj:
                _r = _ship_panel_reply(_subj, tools)
                if _r is not None:
                    return _r
            else:
                r = _handle_theme_intro(tools, msg, _bare)
                if r is not None:
                    return r
        elif intent == '攻略':
            r = _route_guide(tools, msg)
            if r is not None:
                return r
    # 5a-3. 好感度计算（还需多少出击/礼物/天数/答题）：固定公式输出，不走 LLM（防编数字）。
    # 必须先于 is_calc_ask 判定：「帮我算好感度」含"算"，否则会被截进伤害计算流程。
    # 机制解释类问法（有什么用/怎么算的）不拦截，交回 LLM。
    # favor_pending：上一轮已引导补参，追问短句（"现在喜欢，进度30%，到誓约"）不含"好感"也能接上。
    # —— 统一交给定点函数 _favor_dispatch 处理（显式「好感度计算」入口已在 route_work 顶部优先拦截）。
    _r = _favor_dispatch(tools, overrides, msg, _li_is)
    if _r is not None:
        return _r

    # 机制问句（克制/甲弹/弹种）不触发伤害计算："高爆打什么甲" 等应走攻略/问答
    _mech_q = re.search(r'什么甲|什么弹|克制|甲弹|弹种|打什么', msg)
    # 强信号：明确指向计算（含"伤害/计算"上下文、"X打Y"句式）→ 直接进计算流程；
    # "算算dps"等 dps 问法同属伤害计算意图，无需 LLM 二次确认。
    # 强计算信号（_calc_theory / _calc_hard 见函数前部；带理论/抱怨语境的已不算强信号）
    _calc_strong = (('伤害' in msg or '计算' in msg or 'dps' in msg.lower()
                     or bool(re.search(r'[\u4e00-\u9fa5A-Za-z0-9·]{1,12}(?:打|攻击)[\u4e00-\u9fa5A-Za-z0-9·]{1,12}', msg)
                             or re.search(r'(?:^|[\s，,])打[\u4e00-\u9fa5A-Za-z0-9·]{1,8}$', msg)))
                    and not _calc_theory)
    # 弱信号：只有孤立的"算"字（如"算了我信你吧"）→ 不再硬性触发，
    # 交 LLM 二次确认是否真的在要求计算（关键字 + LLM 共同识别）
    _calc_weak = '算' in msg and not _calc_strong
    # 反查/数值比较豁免：分类失败（_li None）时，"哪些船X伤害高/谁更强"类问句
    # 只是被裸"伤害/攻击"词拖进来的查询，并非要发起计算——放行给后续 检索/问答 兜底，
    # 避免误弹「攻击方无法识别」（曾让用户人工报漏的体验来源）。
    _calc_rq = re.search(r'哪些|什么|谁|有没有|哪(?:艘|条|种|个|位)|' + intents.CHOICE_RE_SRC, msg) \
        and re.search(r'高|低|强|弱|快|好|值|厉害|多|少|优先|值得', msg) \
        and not re.search(r'算一算|帮我算|发起计算|计算(?:伤害|一下)|期望伤害|总伤害|DPS|一击|一发', msg, re.I)
    # LLM 分类成功 → 意图直接决定（intent=calc 无需字面"算/伤害"信号）；
    # 分类失败 → 正则信号 + 弱信号 LLM 二次确认兜底
    # agent 层已用 LLM 判定过意图、且**明确不是计算类**（闲聊/知识问答/数据查询）时，
    # 不让「伤害」二字把用户拖进计算流程 —— 这正是「减伤25% 与 加伤害25% 哪个收益高」
    # 被误判为算伤害、之后每句反驳又被重新拉进表单的根因（用户口径 2026-09-24）。
    _agent_intent = (overrides or {}).pop('_agent_intent', None)   # 消费后清除，不影响后续轮次
    # 评估/咨询问法（"时雨伤害高吗""她的输出怎么样"）问的是**结论**，不是要算数值 ——
    # 与 _calc_theory 同类，只要不是计算专属句式就交给检索/问答（用户口径 2026-09-24：
    # 此前这类问法不含"哪些/谁/什么"、也不含理论词，会被单字「伤害」拉进表单并锁死）。
    _calc_eval = re.search(
        r'怎么样|好不好|行不行|强不强|高不高|值不值|厉不厉害|'
        r'(?:伤害|输出|dps|强度)(?:高|低|强|弱|好)?(?:吗|嘛)', msg, re.I)
    # 否决计算流程：① agent 明确判为「闲聊」；② 理论/比较/抱怨语境；③ 评估咨询问法 ——
    # 但**计算专属句式优先**（_calc_hard，见函数前部），避免拦掉正常计算请求。
    _agent_says_not_calc = (_agent_intent == 'casual' or bool(_calc_theory)
                            or bool(_calc_eval)) and not _calc_hard
    is_calc_ask = ((not _explain_q) and (not _mech_q) and not _agent_says_not_calc
                   and ((_li is not None and _li.get('intent') == 'calc')
                        or (_li is None and (not _calc_rq)
                            and (_calc_strong or (_calc_weak and _llm_is_calc_request(msg))))))
    # 组队意图：跳过单人计算流程（由下方遭遇战/PVP 分支处理）。
    # 遭遇战/boss/模拟组队 的明确意图词优先于"算…伤害"——"算遭遇战伤害"应弹遭遇战表单
    # 而非被 _calc_strong（含"伤害"）截进单人计算流程
    is_team = ((('组队' in msg) or bool(re.search(r'遭遇战|boss|BOSS|模拟\s*pvp', msg, re.I)))
               and not overrides.get('calc_state', {}).get('attacker'))
    # ---- 争锋竞技场 / 模拟组队 伤害意图 → 直接弹模拟组队表单（避免回落 LLM「系统处理。」）----
    # 关键词驱动（不依赖 LLM 意图分类），兜住 LLM 把「争锋竞技场伤害」判成 calc 的情况
    # 排除词补充（2026-09-24）："争锋竞技场的伤害怎么算 / 排行在哪看 / 收益如何"是**咨询**，
    # 不该弹模拟表单；只有真正要发起模拟时才进（残留的同类误伤）。
    if re.search(r'pvp|争锋|竞技场', msg, re.I) and re.search(r'伤害|计算|输出|打|dps|DPS|测算|多少', msg) \
            and not re.search(r'推荐|练谁|用谁|阵容|加点|攻略|怎么玩|怎么打|信物|天赋|'
                              r'怎么算|如何算|排行|榜单|在哪|哪里|收益|理论|比较|哪个|为什么', msg):
        return _pvp_form_reply()

    if (overrides.get('calc_state') or is_calc_ask) and not is_team:
        r = handle_calc(tools, overrides, msg)
        if r is not None:
            return r

    # 伤害计算公式问句（"伤害怎么算"/"伤害计算公式"等）：固定输出公式一图流 + 引导，
    # 不进计算流程、不交 LLM；要求命中 _explain_q 且含"伤害"（避免"克制机制/怎么玩"等误伤）；
    # 计算轮中问公式会被上方排他先截住弹回菜单，不会到这里（图一次流程只出一次）
    if _li is None and '伤害' in msg and _explain_q:
        return {'reply': '伤害计算公式一图流如下，对照即可；要算具体数值请输入「算伤害」发起计算。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                'guide_files': [], 'guide_lib': '', 'ok': True,
                'top_image': dict(_CALC_FORMULA_IMAGE)}

    # 0b. 装备咨询-搭配类（配装/搭配/装备怎么配）→ 攻略向量检索（正确查找攻略），
    # 命中不足或检索失败回落打开配装目录
    if _li is None and re.search(r'配装|搭配|装备.*?(?:推荐|怎么穿|怎么选|怎么配|怎么带|如何选|怎么搭|怎么用)', msg):
        g = _handle_guide_qa(tools, msg)
        if g is not None:
            return g
        return {'reply': '配装建议在攻略里查看——已为你打开配装相关目录，按分类翻阅即可。',
                'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                'encounter_form': [], 'guide_files': [], 'guide_lib': '配装', 'ok': True}

    # 0c. 具体装备查询：停用 query_equipment，给「常用装备速览」表 —— 已上移至玩法介绍
    # 拦截器之前（装备词问法会被 _handle_theme_intro 抢成「装备系统」介绍）

    # 4c. 模拟组队：无完整参数 → 表单引导；有参数（防守/前排）→ 直接模拟
    # 排除咨询类（天赋/加点/攻略/推荐/阵容/怎么玩 等）：这些是 PVP 攻略，不触发模拟
    _pvp_ask = re.search(r'天赋|加点|攻略|推荐|怎么[玩打配点加]|如何[玩打配点加]|阵容|强度|谁', msg)
    # 用户口径 2026-09-27：功能名改为「模拟组队」，显式写「模拟组队 / 模拟pvp」时
    # 不再依赖 LLM 意图分类，关键词直接确定性进入表单/模拟分支（兼容旧「模拟pvp」）。
    if (_li_is('pvp') or re.search(r'模拟\s*(组队|pvp)', msg, re.I)) \
            and (('pvp' in msg.lower() or ('组队' in msg and ('防守' in msg or '前排' in msg)))
                 and not _pvp_ask):
        tool = _tool(tools, 'simulate_pvp')
        if tool:
            if ('防守' in msg or 'defender' in msg.lower()) and '前排' in msg:
                try:
                    defender = re.search(r'防守[：:]\s*([^\s]+)', msg)
                    dur = (re.search(r'白天[：:]\s*([^\s]+)', msg)
                           or re.search(r'战斗时长[：:]\s*([^\s]+)', msg)
                           or re.search(r'时间[：:]\s*([^\s]+)', msg)
                           or re.search(r'(\d+(?:\.\d+)?)\s*秒', msg))
                    front = re.search(r'前排[：:]\s*([^\n]+?)(?=后排|$)', msg)
                    back = re.search(r'后排[：:]\s*([^\n]+?)(?=旗舰|$)', msg)
                    flagship = re.search(r'旗舰[：:]\s*([^\s]+)', msg)
                    _app = _get_app(tools)
                    raw_def = defender.group(1) if defender else ''
                    raw_day = dur.group(1) if dur else ''
                    raw_front = (front.group(1).split() if front else [])
                    raw_back = (back.group(1).split() if back else [])
                    raw_flagship = flagship.group(1) if flagship else ''
                    # 全量校验：识别「所有」无法识别项
                    vres = validate_pvp_input(_app, raw_def, raw_day, raw_front, raw_back, raw_flagship)
                    if not vres['ok']:
                        bad = {e['field'] for e in vres['errors'] if e['field'] in
                               {'defender', 'day_time', 'front1', 'front2', 'front3', 'back1', 'back2', 'back3', 'flagship'}}
                        emsg = {e['field']: e['message'] for e in vres['errors']}
                        lead = f"指挥官，有 {len(vres['errors'])} 项没能识别出来，已帮你保留能识别的内容，请只修改标红的格子后重新提交："
                        form = _build_team_form(True, raw_def, raw_day, raw_front, raw_back, raw_flagship, bad, emsg)
                        return _rerender_on_errors(lead, vres['errors'], form)
                    # 全部识别 → 直接模拟
                    r = vres['resolved']
                    res = simulate_pvp(_app, r['defender_name'], r['day_time'],
                                       r['front_ships'], r['back_ships'], r['flagship_name'],
                                       night_time=r['night_time'])
                    text = res['text'] if isinstance(res, dict) else res
                    detail = res.get('detail', '') if isinstance(res, dict) else ''
                    return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True,
                            'encounter_detail': detail, 'encounter_compute': True}
                except Exception as e:
                    return {'reply': f"噢，指挥官，出错了。{e}", 'tool_outputs': [], 'suggestions': [],
                            'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': False}
            # 表单引导
            return _pvp_form_reply()
    # 4b. 遭遇战模拟（带 前排/后排/白天 参数的完整表单）→ 已上移至 _handle_encounter_sim_params，
    # 在 tips 结构化层之前确定性拦截（关键词驱动，不依赖 LLM 意图分类），避免被 tips 抢成小技巧。
    # 此处仅保留裸「遭遇战」→ 表单引导分支。
    # 遭遇战相关：仅"遭遇战"三字触发（"遭遇战" / "遭遇战模拟" → 表单引导；
    # "遭遇战攻略/往期" → 攻略；"boss/模拟"等泛词单独不再触发遭遇战）
    if _li_is('encounter', 'guide') and '遭遇战' in msg:
        if '攻略' in msg or '往期' in msg or re.search(r'怎么(打|玩|配|过)|如何(打|玩|配)', msg):
            # 遭遇战攻略 = 通用攻略查询聚焦遭遇战（encounter_guide 已并入 query_guide）
            tool = _tool(tools, 'query_guide')
            if tool:
                out = tool.call({'keyword': '遭遇战'})
                text = out if isinstance(out, str) else str(out)
                # 收集攻略文件（供滑块精确渲染）
                gf = []
                for m in re.finditer(r'<<GUIDE_FILE>>([^<\n]+?)\|([^<\n]+)', text):
                    gf.append({'path': m.group(1).strip(), 'title': m.group(2).strip(), 'summary': ''})
                # 剥离标记（与攻略面板一致：文档由滑块承载，回复只留题目）
                text = re.sub(r'<<GUIDE_FILE>>[^<\n]+?\|[^<\n]+', '', text)
                text = re.sub(r'<<GUIDE_LIB>>[^*]*?$', '', text).strip()
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': gf, 'guide_lib': '遭遇战', 'ok': True}
        # 明确要模拟（含 模拟/参数）→ 表单引导（"遭遇战"裸词已由 _handle_theme_intro 处理）
        return {'reply': '噢，指挥官，遭遇战模拟。请填写 boss、白天时间（黑夜自动 = 150 - 白天）与舰队阵容：',
                'tool_outputs': [], 'suggestions': [],
                'fill_form': [],                 'encounter_form': [
                    {'key': 'boss', 'label': 'Boss', 'placeholder': '嘉波/巴巴托斯/阿丝莫德/亚斯塔露/列拉金/沙克斯/夕茉莉/河豚王/拜蒙/雅蒙/布耶尔', 'required': True, 'value': '',
                     'options': list(load_encounter_bosses().keys())},
                    {'key': 'day_time', 'label': '白天时间', 'placeholder': '秒（黑夜自动=150-白天）', 'required': True, 'value': ''},
                    {'key': 'front1', 'label': '前排1', 'placeholder': '角色名', 'required': True, 'value': ''},
                    {'key': 'front2', 'label': '前排2', 'placeholder': '角色名', 'required': True, 'value': ''},
                    {'key': 'front3', 'label': '前排3', 'placeholder': '角色名', 'required': True, 'value': ''},
                    {'key': 'back1', 'label': '后排1', 'placeholder': '角色名', 'required': True, 'value': ''},
                    {'key': 'back2', 'label': '后排2', 'placeholder': '角色名', 'required': True, 'value': ''},
                    {'key': 'back3', 'label': '后排3', 'placeholder': '角色名', 'required': True, 'value': ''},
                ], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 0. 资源不足闲聊（须优先于攻略路由）：识别「缺某游戏内资源」类意图
    # （我缺钻石了 / 油耗好大 / 经费不足 等），走规划建议 + 氪啊钩子；
    # 而非被「钻石」等攻略词误路由成资源用途攻略。
    # LLM 意图驱动（chat/query/guide——「我缺钻石了」常被判 guide，本分支价值就是把它从
    # 攻略路由抢回）+ 函数内关键词辅助门控（含攻略动作词 怎么/买/充值/查 → 交回攻略）。
    if _li_is('chat', 'query', 'guide'):
        r = _handle_resource_scarcity(tools, overrides, msg)
        if r is not None:
            return r

    # 1. 攻略（本地 query_patterns 命中或含攻略词 → 走攻略路由；_GUIDE_WORDS 是"攻略"大类的
    # 补充触发词，JSON pattern 词（钻石/单纵/嘉波/帧率等）由 _local_guide_topic 全覆盖）
    # 严格门槛：仅「明确攻略请求」触发（怎么/攻略/推荐/查 等），感叹/陈述闲聊（如
    #「最近舰队油耗很大，经费不足啊」）交回 LLM 闲聊，不误路由成攻略
    # compare/evaluate 也放行到此处：这两类处理器「不接管」（实体不足等）时，
    # 「哪些舰灵值得优先改造」这类问句的正解就是那篇攻略文档，不能因换了意图档而丢路径。
    if _li is None and _is_guide_ask(msg) \
            and (any(w in msg for w in _GUIDE_WORDS) or _local_guide_topic(msg)):
        r = _route_guide(tools, msg)
        if r is not None:
            return r

    # 2. 查询角色（支持多名字：查赤城加贺飞龙翔鹤 → 逐个输出；天气由下方独立分支处理）
    if _li_is('query') and re.search(r'(查询|查一下|查查|帮我查|查下|面板|介绍|(?<!检)(?<!考)查|询|问|请问|看看|数据|详情|检索|资料)', msg) and '天气' not in msg:
        # 笼统查询（未指明具体舰灵）→ 前端角色面板（名单+阵营筛选+头像），不再只回引导语
        if re.search(r'(?:查询|查一下|查查|帮我查|查下|(?<!检)(?<!考)查|询)\s*(角色|舰灵|舰娘|舰船|舰|船|哪位|哪些|谁)', msg):
            return {'reply': '角色名录已展开，按阵营筛选或直接选择想要的舰灵即可。',
                    'tool_outputs': [], 'suggestions': [], 'fill_form': [], 'encounter_form': [],
                    'guide_files': [], 'guide_lib': '', 'ok': True,
                    'ship_browser': True, 'ship_focus': ''}
        tool = _tool(tools, 'query_ship_info')
        if tool:
            clean = re.sub(r'(查询|查一下|查查|帮我查|帮我查下|查下|介绍一下|介绍|的|面板|信息|属性|舰灵|(?<!检)(?<!考)查|算|问|请问|看看|数据|详情|检索|资料|技能|天赋|装备)', '', msg).strip()
            clean = re.sub(r'[？?！!。，,、]+$', '', clean).strip()
            # 剥词后残留机制词（「问的是克制系数」→「是克制系数」）说明这不是角色查询，
            # 是对上一轮回答的澄清/机制追问——交回后续攻略/弹种分支，不发角色查询
            #（曾产出「未找到角色: 是克制系数。」）。
            if clean and re.search(r'克制|系数|修正|概率|几率|点燃|击穿|过穿|穿甲|高爆|通常弹|轻甲|中甲|重甲|增伤|减伤|配队|阵容', clean):
                clean = ''
            if clean:
                app = None
                for t in tools:
                    if hasattr(t, 'cfg') and isinstance(t.cfg, dict) and t.cfg.get('app'):
                        app = t.cfg['app']
                        break
                names = _split_query_names(clean, app)
                # 拆不出库内角色（用户其实问的是机制/操作，不是舰灵面板）→ 放弃查询，
                # 放行到后面的攻略/弹种检索分支，不发「未找到角色: …」
                if names:
                    overrides['last_role'] = names[0]  # 记录最近提及角色（支持后续代词指代）
                    # 改造意图拦截：点名具体角色问改造，但该角色没有改造形态 → 直接提示不能改造；
                    # 若角色可改造，则导向改造优先级攻略检索（确保单舰改造点评命中知识库）。
                    if _is_refit_ask(msg):
                        _bases = set()
                        try:
                            from tools.knowledge import _refittable_base_set
                            if app is not None:
                                _bases = _refittable_base_set(app)
                        except Exception:
                            _bases = set()
                        if names[0] not in _bases:
                            return {'reply': '《%s》目前没有开放改造，不能改造。\n（如需查看它的面板资料，直接和我说「查%s」即可。）' % (names[0], names[0]),
                                    'tool_outputs': [], 'suggestions': [], 'fill_form': [],
                                    'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
                        _g = _handle_guide_qa(tools, names[0] + ' 改造优先级')
                        if _g is not None:
                            return _g
                    # 单角色 → 前端角色面板（详情在面板展开）；多角色 → 保留逐个文本输出（多角色对照）
                    if len(names) == 1:
                        _r = _ship_panel_reply(names[0], tools)
                        if _r is not None:
                            return _r
                    outs = []
                    for n in names:
                        outs.append(_query_text(tools, 'query_ship_info', {'ship_name': n})
                                    or '我暂时没查到相关的信息，指挥官。')
                    text = '\n\n'.join(outs)
                    return {'reply': text, 'tool_outputs': outs, 'suggestions': [], 'fill_form': [],
                            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 4. 天气（LLM 意图驱动 query/chat + 天气关键词辅助；"天气不错"等感叹由 LLM 判 chat
    #    但无查询式关键词不触发，双重排除）
    if _li_is('query', 'chat') and re.search(r'(?:天气|气温|温度)(?:怎么样|怎么|如何|咋样|多少|呢|吗)?$|查.*天气|热不热|冷不冷|下雨(吗|么)|(?:多少|几)度', msg):
        tool = _tool(tools, 'query_weather')
        if tool:
            try:
                out = tool.call({})
                text = out if isinstance(out, str) else str(out)
                return {'reply': text, 'tool_outputs': [text], 'suggestions': [], 'fill_form': [],
                        'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
            except Exception:
                return {'reply': '天气查询暂时不可用，稍后再试。', 'tool_outputs': [], 'suggestions': [],
                        'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}

    # 5b. 时间/日期（LLM 意图驱动 chat/query + `_is_time_query` 关键词辅助；
    # 攻略问法已被 _is_time_query 内部排除）
    if _li_is('chat', 'query') and _is_time_query(msg):
        return _time_reply()

    # 5c. 资源不足闲聊：已在前置段（攻略路由之前）由 _handle_resource_scarcity 统一处理
    #（LLM chat/query 门控 + 函数内动作词门控），此处不再重复调用。

    # ---- 末级 LLM 意图分析（关键字+映射+LLM 组合：规则未命中时分析含义，不编造）----
    # 触发：非纯问候的短输入（"口罩拉菲呢/火焰拉菲是" 等口语说法由 LLM 判断含义）
    # LLM 只判意图类型（字符串：角色/介绍/攻略/计算/闲聊），角色名/主题由系统映射层提取
    if _li is None and len(msg) <= 24 and not re.search(r'^(你好|您好|嗨|hi|hello|在吗|谢谢|多谢|再见|拜拜|拜拜了|晚安|早安|辛苦了|嗯|哦|好|行)[\s！!？?。，,、~～]*$', msg, re.I):
        # 偏好/观点闲聊（"我喜欢大雷舰灵""我觉得大凤好看"）→ 直接交回 LLM 闲聊，
        # 不调 LLM 意图分类、不进查询工具
        if _is_opinion_casual(msg):
            return None
        intent = _llm_analyze_intent(msg)
        if intent == '角色':
            canon = _resolve_role_from_msg(msg, tools)
            if canon:
                r = _ship_panel_reply(canon, tools)
                if r is not None:
                    return r
        elif intent == '介绍':
            # 上下文追问（无主语追问上轮角色）→ 转角色查询，不回落游戏简介
            _subj = _context_followup_target(msg, overrides, tools)
            if _subj:
                _r = _ship_panel_reply(_subj, tools)
                if _r is not None:
                    return _r
            else:
                r = _handle_theme_intro(tools, msg, _bare)
                if r is not None:
                    return r
        elif intent == '攻略':
            r = _route_guide(tools, msg)
            if r is not None:
                return r
        else:
            # LLM 判闲聊/计算/其他：输入含昵称子串（口语尾词包裹，如"花生啊"含"花生"）
            # → 仍尝试查角色（映射层兜底）
            canon = _try_nickname_contains(msg)
            if canon:
                r = _ship_panel_reply(canon, tools)
                if r is not None:
                    return r

    # ---- 末端通用知识检索兜底（RAG 作答，标注来源）----
    # 走到这里说明所有固定/工具分支都未接住。若输入带明确问意且像拂晓/游戏内容
    # （非生活闲聊、非操作类意图），交给 GuideRAG 检索攻略库回答——不再把规则漏网点
    # 暴露给用户人工报漏。命中不足/失败 → None 交回闲聊 LLM。
    _fb = _rag_knowledge_fallback(tools, msg, overrides, _li) if _li is None else None
    if _fb is not None:
        return _fb

    return None
