# -*- coding: utf-8 -*-
"""路由闸门 / 判据注册表（用户口径 2026-10-05）
=================================================

问题（用户 2026-10-05 的判断，成立）
------------------------------------
「确定性前置 + 正则路由 = 无限枚举」：每次线上判错，就往最前面加一条硬短路；
规则**只增不减**、**位置依赖**（注释里到处是"必须早于 X 门控"）、事故出处只活在注释里
—— 于是没人敢删、也没法删。

本模块把「闸门」变成可枚举 / 可回归 / 可退场的**一等公民**：

  ① **注册表**：每条闸门或判据一条 Rule
     （id / 处置 / 层 / 位置 / 判据 / **事故出处** / **退场条件** / 回归样例）
  ② **计数**  ：`hit(rule_id)` 记录闸门实际命中 —— 长期 0 命中的就是死规则，可以删
  ③ **先验**  ：`prior_hints(msg)` 把 converge 类判据的结论作为**提示**交给 LLM 元路由，
     LLM 可否决（不再由正则单方面决定）；`note_conflict()` 记录「LLM 选得不一样」，
     用来判断这条规则能不能退场
  ④ **自检**  ：`selfcheck()` —— 位置存在、样例命中符合预期、converge 必须写退场条件、id 唯一

处置（verdict）
--------------
  ``redline``   红线/安全：**永不退场**（错了就是事故）
  ``keep``      保留：状态机 / 按钮语义 / 槽位解析（那是"读表单"，不是"判意图"）
  ``converge``  收敛：已降级为 LLM 先验 + 计数，等指标达标后退场
  ``retire``    退场候选：职责应由工具 schema 承担

用法
----
    python -m agent.route_registry                # 打印全量清单（markdown）
    python -m agent.route_registry --selfcheck    # 自检（可挂 CI）
    python -m agent.route_registry --stats        # 命中 / 先验分歧计数
    python -m agent.route_registry --kind converge

设计约束
--------
· 本模块**只登记与计数，不改变任何路由行为**（除 `prior_hints` 显式注入 LLM 路由提示外）；
· 不 import calc_flow / guard（避免循环依赖），探测器用「点号路径」延迟导入；
· 计数是进程内的（重启清零）—— 用来观察"哪条规则还活着"，不是计费口径。
"""
from __future__ import annotations

import os
import re
import threading
from typing import NamedTuple, Optional, Tuple

__all__ = [
    'Rule', 'RULES', 'hit', 'counts', 'reset_counts', 'prior_hints', 'prior_tools',
    'note_conflict', 'conflicts', 'stats', 'selfcheck', 'dump_markdown', 'get_rule',
    'by_kind', 'PRIOR_ENABLED', 'route_src', 'route_sources',
]

# 先验注入开关：AKALI_ROUTE_PRIOR=0 可一键关闭（生产排障用）
PRIOR_ENABLED = (os.environ.get('AKALI_ROUTE_PRIOR', '1') or '').strip().lower() \
    not in ('0', 'false', 'off', 'no')

_MAX_HINTS = 3          # 一次最多给模型几条先验，避免提示词膨胀 / 过度引导


class Rule(NamedTuple):
    """一条闸门或判据。probe 为编译好的正则；detector 为 "模块:函数" 单参探测器。"""
    id: str
    kind: str                 # redline | keep | converge | retire
    layer: str                # guard | pregate | ladder | judge | slot
    where: str                # "相对项目根/文件.py:行"
    what: str                 # 判据一句话
    accident: str             # 事故出处（来自代码注释 / 线上复盘）
    tool: str = ''            # 该判据倾向路由到的工具（先验用）
    retire_when: str = ''     # 退场条件（converge 必填）
    probe: Optional[re.Pattern] = None
    detector: str = ''
    samples: Tuple[Tuple[str, bool], ...] = ()


def _c(pat: str, flags: int = 0) -> re.Pattern:
    return re.compile(pat, flags)


# =============================================================================
# 先验/自检用的公共探针（镜像执行侧判据；真正的判据见各 Rule 的 where）
# =============================================================================
# 「再见/退出」：镜像 web_app 的 _BYE_PATTERN + _BYE_88_GUARD —— 88 既是告别口语也是氪金档位，
# 命中 88 且句中有游戏内容词时**不算**告别（否则「88自选选谁」会被当拜拜清会话）。
_BYE_PROBE = _c(r'(再见|拜拜|bye|退出|退出会话|结束会话|结束本轮|告辞|后会有期|晚安|走了|88)', re.I)
_BYE_88_GUARD = _c(r'自选|礼包|保底|氪金|充值|档位|建造|图纸|改造|装备|练级|配装|攻略|查询|伤害|面板|天赋|技能')


def _det_bye(msg: str) -> bool:
    m = msg or ''
    if '88' in m and _BYE_88_GUARD.search(m):
        return False
    return bool(_BYE_PROBE.search(m))


# =============================================================================
# 全量注册表（按处置分组；同组内按执行顺序）
# =============================================================================
RULES: Tuple[Rule, ...] = (
    # ---------------- A. 红线 / 安全（redline：永不退场）----------------
    Rule('guard.other_game', 'redline', 'guard', '通用工具/agent/guard.py:162',
         'boundary.is_other_game → 违规话术 + 嫌弃',
         'guard.py:160「其他游戏（同类于违规，属守卫范围）」；保留独立判定点便于单独调话术/表情',
         detector='core.boundary:is_other_game',
         samples=(('原神怎么玩', True), ('元神怎么玩', True), ('胡德改怎么练', False)),
         ),
    Rule('guard.banned', 'redline', 'guard', '通用工具/agent/guard.py:167',
         '_BANNED_RE / boundary.is_banned → 违规固定话术（no_history）',
         '涉政/违法/暴力/竞品底线；命中轮不写会话历史',
         detector='core.boundary:is_banned',
         samples=(('', False), ('胡德面板', False)),
         ),
    Rule('guard.flirt_overstep', 'redline', 'guard', '通用工具/agent/guard.py:175',
         'boundary.is_flirt_overstep → 按主体分流话术',
         'guard.py:171「此前在 web_app 路由之后才判，越界消息会先被 guide_rag 接走」',
         detector='core.boundary:is_flirt_overstep',
         samples=(('胡德面板', False),),
         ),

    # ---------------- B. 防幻觉守卫（keep / redline）----------------
    Rule('guard.challenge', 'keep', 'guard', '通用工具/agent/guard.py:30',
         '_ANSWER_CHALLENGE_RE「胡说/你确定/哪来的」→ 撤回 + 确定性复核',
         'model 曾自称"数据已校验"；质疑必须由后端复核，不许自由生成',
         detector='agent.guard:guarded_backend_reason',
         samples=(('你这个是编的吧', True), ('真的假的', True), ('查胡德', False)),
         ),
    Rule('guard.archive', 'redline', 'guard', '通用工具/agent/guard.py:35',
         '_UNSUPPORTED_ARCHIVE_RE「RA-01 档案」→ _NO_DATA_REPLY',
         'guard.py:194「曾由自由生成链路臆造…拦截行为保留（删掉拦截＝放开幻觉，不能退）」',
         samples=(('调取RA-01档案', True), ('胡德面板', False)),
         probe=_c(r'RA[\s\-－—_]*0*1\s*(?:档案|编号)?', re.I),
         ),
    Rule('guard.nokb_basis', 'redline', 'guard', '通用工具/agent/guard.py:39',
         '_NO_KB_FUTURE_RE / _NO_KB_CREATIVE_RE → _NO_DATA_REPLY'
         '（**已知缺口**：『下期会出什么舰灵』这类"下期+会出+什么X"不在覆盖内，2026-10-05 自检发现）',
         'guard.py:36「未来/未公布内容 与 创作请求，知识库无据（自由生成链路会臆造）」',
         probe=_c(r'(?:下期|下个版本|下一个版本|以后的?版本|未来|明年)[^。！？\n]{0,12}'
                  r'(?:会不会|是否|什么(?:时候|时)|何时|要出|出新|新(?:舰灵|角色|皮肤|活动)|叫什么)'
                  r'|会不会(?:加强|削弱|出|上线|进|改)'
                  r'|(?:什么(?:时候|时)|何时)(?:上线|开放|公测|开服)'
                  r'|公测|内测|爆料|剧透|舅舅党'
                  r'|(?:写|编|来|整|作|弄|搞)(?:一)?(?:段|篇|个|首)?[^。！？\n]{0,8}'
                  r'(?:故事|同人|小作文|段子|小说|诗|歌词|剧本|梗)'
                  r'|同人(?:文|小说|故事)|角色扮演|扮演一下'),
         samples=(('下期会不会出新舰灵', True), ('公测什么时候', True),
                  ('帮我写一段同人故事', True), ('胡德改怎么练', False)),
         ),
    Rule('guard.unanswered_topic', 'keep', 'guard', '通用工具/agent/guard.py:127',
         '_unanswered_topic 状态锁：资料不足的主题不再反复猜',
         'guard.py:115「记录"有明确问题但资料不足"的主题，供下一轮前置闸判断」',
         samples=(),
         ),

    # ---------------- C. 固定话术类前置（converge：低风险，可等指标）----------------
    Rule('guard.feedback', 'converge', 'guard', '通用工具/agent/guard.py:182',
         '_FEEDBACK_RE「怎么反馈」→ 固定话术',
         'guard.py:179「否则会被 guide_rag 抢走，返回知识库里"联系客服/腾讯频道"原文」',
         tool='', retire_when='LLM 路由能稳定识别 meta 问法（hit 与 LLM 一致率 ≥95% 且 30 天无回退）',
         probe=_c(r'反馈|提意见|提建议|意见箱|反馈渠道|反馈入口|反馈方式'),
         samples=(('怎么反馈', True), ('反馈入口在哪', True), ('胡德面板', False)),
         ),
    Rule('guard.kb_cutoff', 'converge', 'guard', '通用工具/agent/guard.py:187',
         '_KB_CUTOFF_RE「知识库截止到什么时候」→ 「明天！」',
         'guard.py:77「此前会被 guide_rag 抢答，捞到零散日期片段乱答」',
         retire_when='同上（该口径是产品玩笑话，退场前需确认不会重新被 RAG 抢答）',
         probe=_c(r'(?:知识(?:库)?|数据|资料|训练)[^。！？\n]{0,10}'
                  r'(?:截止|更新|最新|学到|停留在|日期|时间点|到什么时候|到哪天|到几号)'
                  r'|(?:哪年|几年)的?(?:数据|知识|资料)'),
         samples=(('知识库截止到什么时候', True), ('数据更新到几号', True), ('胡德面板', False)),
         ),

    # ---------------- D. 业务前置闸（converge：应成为"先验 + 条件短路"）----------------
    Rule('pregate.ship_use', 'converge', 'pregate', '通用工具/core/route_dispatch.py:24',
         '_handle_ship_use「X 有什么用/就业」→ 原文直出',
         'route_dispatch.py:20「agent 模式下 dispatcher 会先接走『X 有什么用』，答成泛泛的攻略片段」',
         tool='', retire_when='元路由对「有什么用/就业/值得练吗」的命中率 ≥95%，且直出结果与今天一致',
         samples=(),
         ),
    Rule('pregate.talent_ask', 'converge', 'pregate', '通用工具/core/route_dispatch.py:35',
         '_handle_talent_ask「X 天赋怎么点」→ 读底层（推荐点法 + 天赋原文）',
         'route_dispatch.py:31「agent 模式的 LLM 路由会拿通用规则顶（该舰不在攻略正文里）」',
         retire_when='元路由能稳定区分"点法"与"有哪些天赋"，且不再拿通用规则顶',
         samples=(),
         ),
    Rule('pregate.pvp_lineup', 'converge', 'pregate', '通用工具/core/route_dispatch.py:46',
         '_handle_pvp_lineup「争锋 中船 队伍推荐」→ 定向取原文块',
         'route_dispatch.py:42「RAG 只捞到『金大船』那几块（正文写的是金中船/紫中船），体量词对不上」',
         retire_when='检索侧能按场质/体量分块命中（修完体量词对齐后可退场）',
         probe=_c(r'竞技场|争锋|pvp|jjc', re.I),
         samples=(('竞技场中船队伍推荐', True), ('胡德面板', False)),
         ),

    # ---------------- E. 状态机 / 按钮语义（keep：不该交 LLM）----------------
    Rule('keep.calc_state', 'keep', 'guard', '通用工具/agent/guard.py:230',
         'calc_state / favor_pending 激活 → 整段交回 legacy、不跑 LLM 分类',
         'guard.py:228「此时输入是向导的一步参数，不是新意图」',
         samples=(),
         ),
    Rule('keep.bye', 'keep', 'ladder', '用户端特用/web_app.py:2135',
         '再见/退出/88（88 带 _BYE_88_GUARD 排除氪金档）→ 清会话 + 告别',
         'web_app.py:2139「88 既是告别口语也是氪金档位 → 否则『88自选选谁』会被当拜拜清会话」',
         detector='self:_det_bye',
         samples=(('再见', True), ('晚安', True), ('88礼包选谁', False), ('胡德面板', False)),
         ),
    Rule('keep.tool_menu', 'keep', 'ladder', '通用工具/agent/dispatcher.py:415',
         '「计算工具/计算菜单」固定短语 → 直通 legacy（不走 LLM 判意图）',
         'dispatcher.py:410「实测路由模型会偶发判成 casual + no_tool → 回『系统异常。』且不弹子菜单」',
         probe=_c(r'计算工具|计算菜单'),
         samples=(('计算工具', True), ('胡德面板', False)),
         ),
    Rule('keep.ending_word', 'keep', 'ladder', '通用工具/core/calc_flow.py:4835',
         'calc_state 内的中止词（算了/我没让你算…）→ 结束本轮计算',
         'calc_flow.py:4832「用户口径 2026-09-24：说『我没让你算伤害啊』被当成攻击方角色名，且退不出来」',
         probe=_c(r'取消|算了|不填了|不做了|不玩了|不算了|不计算了|退出计算|结束计算|结束本轮|'
                  r'换个话题|换个问题|我没让你算|没让你算|别算了|谁让你算'),
         samples=(('算了', True), ('胡德面板', False)),
         ),

    # ---------------- F. 意图判据：计算（converge，高风险）----------------
    Rule('judge.ship_use', 'converge', 'judge', '通用工具/core/calc_flow.py:2082',
         '_SHIP_USE_RE「有什么用/就业/能配什么队」',
         'calc_flow.py:2076「到向量库找攻略中点名该舰的阵容片段，原文抄给用户」',
         tool='', retire_when='并入 pregate.ship_use 的退场评审（同一职责，一起评估）',
         probe=_c(r'有什么用|有啥用|干什么用|能干嘛|能干什么|能做什么|做什么用|干嘛用|拿来干嘛|拿来干'
                  r'|就业|用途|定位是什么|什么定位|能配什么|配什么队|进什么队|适合什么队|有什么阵容|阵容里'),
         samples=(('胡德有什么用', True), ('胡德面板', False)),
         ),
    Rule('judge.talent_ask', 'converge', 'judge', '通用工具/core/calc_flow.py:2400',
         '_TALENT_ASK_RE（点法词）+ 消息含「天赋」→ 直读底层',
         'calc_flow.py:2392「推荐点法取底层 talent_pvp（202 舰中 187 舰有推荐）；没有就如实说明不编」',
         retire_when='并入 pregate.talent_ask 的退场评审',
         probe=_c(r'怎么点|点法|加点|怎么加|该怎么|如何点|怎么选|怎么带|带什么|推荐|默认|优先'),
         samples=(('天赋怎么点', True), ('胡德面板', False)),
         ),
    Rule('judge.calc_theory', 'converge', 'judge', '通用工具/core/calc_flow.py:4924',
         '_calc_theory（理论/比较/抱怨语境）→ 其中的「伤害」不算计算请求',
         'calc_flow.py:4918「『减伤25%和加伤害25%哪个收益高』被误判成算伤害且退不出来」',
         retire_when='元路由 intent=calculation 的准确率 ≥95%（无"理论语境"误触发）',
         probe=_c(r'收益|理论|性价比|等效|相比|比较|对比|差距|没让你|别算|不要算|凭什么|为什么|怎么会|谁让你'),
         samples=(('减伤25%和加伤害25%哪个收益高', True), ('胡德打前卫算伤害', False)),
         ),
    Rule('judge.calc_hard', 'converge', 'judge', '通用工具/core/calc_flow.py:4927',
         '_calc_hard（X打Y / dps / 算…伤害）→ 即使 LLM 判闲聊也进计算',
         'calc_flow.py:4919「反向问题：『光辉打胜利』被 LLM 判 data_lookup、『算一下A和B哪个伤害高』判 knowledge_qa」',
         retire_when='元路由对计算专属句式零漏判（可先降级为强先验，保留兜底短路）',
         probe=_c(r'[\u4e00-\u9fa5A-Za-z0-9·]{1,12}(?:打|攻击)[\u4e00-\u9fa5A-Za-z0-9·]{1,12}'
                  r'|(?:^|[\s，,])打[\u4e00-\u9fa5A-Za-z0-9·]{1,8}$'),
         samples=(('胡德打前卫算伤害', True), ('今天心情不错', False)),
         ),

    # ---------------- G. 意图判据：查询 / 筛选（converge，最常打架）----------------
    Rule('judge.ship_filter', 'converge', 'judge', '通用工具/core/calc_flow.py:3121',
         '_SHIP_FILTER_ASK_RE「哪些/名单/列表」+ 维度词 → ship_filter 直出',
         'calc_flow.py:34「杜绝"只因出现船名/关键字就触发查询"（如『女灶神很强』不应查面板）」',
         tool='',
         retire_when='ship_filter 的 schema 已描述清楚"名单类"→ 元路由命中率 ≥95% 后删正则',
         probe=_c(r'哪些|什么角色|什么舰灵|什么船|角色|舰灵|名单|列表|有谁|给我看看|盘点'),
         samples=(('有哪些驱逐', True), ('今天心情不错', False)),
         ),
    Rule('judge.ship_attr_cd', 'converge', 'judge', '通用工具/core/calc_flow.py:3231',
         '_SHIP_ATTR_CD_RE（冷却/CD/多少秒）→ 确定性抽 CD（支持继承上一批）',
         'calc_flow.py:2943「必杀技/通常技/支援技 CD 类问法优先走 _handle_ship_attr_query，避免落到 RAG 拼无关列表」',
         retire_when='合并进 ship_filter 参数化（CD 作为字段）后即退场',
         probe=_c(r'(冷却时间|冷却|(?<![a-zA-Z])cd(?![a-zA-Z])|多少秒|几秒)', re.I),
         samples=(('他们的必杀技CD是多少', True), ('今天心情不错', False)),
         ),
    Rule('judge.refit_ask', 'converge', 'judge', '通用工具/core/calc_flow.py:3467',
         '_REFIT_ASK_RE 点名角色问改造 → 无改造形态直接提示，可改造导攻略检索',
         'calc_flow.py:5597「点名具体角色问改造，但该角色没有改造形态 → 直接提示不能改造」',
         retire_when='改造信息进入底层可查（面板/天赋同源）后即退场；此前保留（答错＝给出不存在的改造形态）',
         detector='core.calc_flow:_is_refit_ask',
         samples=(('胡德改造厉害吗', True), ('胡德面板', False)),
         ),

    # ---------------- H. 意图判据：攻略 / 主题（converge）----------------
    Rule('judge.guide_ask', 'converge', 'judge', '通用工具/core/calc_flow.py:1454',
         '_is_guide_ask：攻略动作词 / 短主题词才算攻略请求',
         'calc_flow.py:1422「『最近舰队油耗很大，经费不足啊』无请求词 → 交回闲聊，不触发攻略工具」',
         retire_when='元路由 intent=knowledge_qa/document_browse 零漏判，且"感叹被误触发攻略"不再出现',
         detector='core.calc_flow:_is_guide_ask',
         samples=(('怎么练级', True), ('最近舰队油耗很大', False)),
         ),
    Rule('judge.story', 'retire', 'judge', '通用工具/core/calc_flow.py:1385',
         '_STORY_RE / _STORY_PLOT_RE → 剧情榜 / 建议自行体验',
         'calc_flow.py:1382「不能只凭一个词就接管整条对话（去掉『羁绊』，它与玩法问题冲突）」',
         retire_when='做成 query_story 工具（攻略库里有《剧情榜》文档）后即退场',
         probe=_c(r'剧情|剧透|好感故事|个人故事|角色故事'),
         samples=(('剧情榜', True), ('胡德面板', False)),
         ),
    Rule('judge.pvp_lineup', 'retire', 'judge', '通用工具/core/calc_flow.py:2496',
         '_PVP_LINEUP_RE + _PVP_WANT_RE → 定向取原文块',
         'calc_flow.py:2490「RAG 只捞到『金大船』就作答；换个说法（pvp…）还会撞上服务异常」',
         retire_when='并入 pregate.pvp_lineup；检索侧体量词对齐后可整体退场',
         probe=_c(r'竞技场|争锋|pvp|jjc', re.I),
         samples=(('竞技场中船队伍推荐', True), ('胡德面板', False)),
         ),
    Rule('judge.nature_list', 'retire', 'judge', '通用工具/core/calc_flow.py:3234',
         '_NATURE_LIST_RE / _SHIP_NATURE_GENERIC_RE（技能性质列举）',
         'calc_flow.py:3294「要求带『列举/归属』信号词，避免把伤害/计算问法误判为性质列举」',
         retire_when='skills/性质 作为 ship_filter 的字段（schema 已能表达）后即退场',
         probe=_c(r'技能性质|技能分类|技能类型|技能效果|技能归类|技能解析'),
         samples=(('技能性质的分类有哪些', True), ('胡德面板', False)),
         ),

    # ---------------- I. 槽位解析（keep：读表单不是判意图）----------------
    Rule('slot.scout', 'keep', 'slot', '通用工具/core/calc_flow.py:3647',
         '_SCOUT_MODE1/2 解析「索敌 1000」等数值输入',
         'calc_flow.py:4871「『计算索敌 1000』含『计算』会被 _calc_strong 抢进伤害计算流程」',
         probe=_c(r'(?:索敌|侦察)(?:线|值|判定|计算|估算)?\s*[:：]?\s*(\d+(?:\.\d+)?)'),
         samples=(('索敌计算 1000', True), ('今天心情不错', False)),
         ),
    Rule('slot.exp', 'keep', 'slot', '通用工具/core/calc_flow.py:3989',
         '_EXP_LV_RE / _EXP_CUR_RE / _EXP_BATTLE_RE 解析经验表单串',
         'calc_flow.py:4884「输入当前等级+经验 → 剩余经验；再给出战经验 → 出击场数」',
         probe=_c(r'(?:当前)?等级\s*[：:]\s*(\d{1,3})'),
         samples=(('经验计算 等级：45 当前经验：3200 出战经验：120', True), ('今天心情不错', False)),
         ),
    Rule('slot.combat_form', 'keep', 'slot', '通用工具/core/calc_flow.py:4128',
         '_OTHER_WORKFLOW_KW_RE 识别表单串（避免被当角色列表）',
         'calc_flow.py:4126「典型误吞：遭遇战表单提交串曾被 _looks_like_ship_list 当角色列表 → 报『需要填满6名』」',
         probe=_c(r'遭遇战|boss|BOSS|模拟|白天|黑夜|前排|后排|旗舰|防守|时长|伤害|面板|装备|攻略|'
                  r'天赋|技能|好感|天气|航向|制空|阵型|攻击方|防守方|计算工具'),
         samples=(('遭遇战模拟 boss：嘉波 白天：60 前排：A B 后排：D E', True), ('今天心情不错', False)),
         ),
    Rule('slot.pronoun', 'keep', 'slot', '通用工具/core/calc_flow.py:2640',
         '_PRONOUN_RE / _resolve_pronoun 指代继承（她/这个/刚才那艘）',
         'calc_flow.py:4783「显式『好感度计算』入口…」同族：指代必须先归一，否则工具参数错位',
         probe=_c(r'她|他|它|这个|那个|这艘|那艘|该角色|该舰|前面|刚才|上面'),
         samples=(('查她面板', True), ('今天心情不错', False)),
         ),
)

_BY_ID = {r.id: r for r in RULES}

# 事件计数（进程内；重启清零。用于回答"哪条规则还活着"）
_lock = threading.Lock()
_HITS: dict = {}
_CONFLICTS: dict = {}          # rule_id -> {'n': 分歧次数, 'llm': {tool: n}}
_ROUTE_SRC: dict = {}          # 'agent' / 'legacy' / 'pregate.xxx' / 'webapp.xxx'


def hit(rule_id: str, n: int = 1) -> None:
    """记一次闸门命中（只计数，不影响行为）。"""
    if not rule_id:
        return
    with _lock:
        _HITS[rule_id] = _HITS.get(rule_id, 0) + int(n)


def route_src(src: str) -> None:
    """记录本轮最终由谁接管：agent 元路由 / legacy / 某条前置闸。"""
    if not src:
        return
    with _lock:
        _ROUTE_SRC[src] = _ROUTE_SRC.get(src, 0) + 1


def counts() -> dict:
    with _lock:
        return dict(_HITS)


def route_sources() -> dict:
    with _lock:
        return dict(_ROUTE_SRC)


def reset_counts() -> None:
    with _lock:
        _HITS.clear()
        _CONFLICTS.clear()
        _ROUTE_SRC.clear()


def get_rule(rule_id: str) -> Optional[Rule]:
    return _BY_ID.get(rule_id)


def by_kind(kind: str) -> Tuple[Rule, ...]:
    return tuple(r for r in RULES if r.kind == kind)


# =============================================================================
# 先验：把 converge 类判据的结果作为「提示」交给 LLM 元路由（LLM 可否决）
# =============================================================================
def _probe_hit(rule: Rule, msg: str) -> bool:
    try:
        if rule.probe is not None:
            return bool(rule.probe.search(msg or ''))
        if rule.detector:
            if rule.detector.startswith('self:'):
                # 本模块内的镜像探针（如 _det_bye）
                return bool(globals()[rule.detector.split(':', 1)[1]](msg or ''))
            mod, _, fn = rule.detector.partition(':')
            import importlib
            return bool(getattr(importlib.import_module(mod), fn)(msg or ''))
    except Exception:
        return False
    return False


def _prior_rules(msg: str) -> list:
    """命中先验的 converge 规则（最多 _MAX_HINTS 条，按注册顺序）。"""
    out = []
    for r in RULES:
        if r.kind != 'converge':
            continue
        if _probe_hit(r, msg):
            out.append(r)
            if len(out) >= _MAX_HINTS:
                break
    return out


def prior_tools(msg: str) -> list:
    """命中先验所倾向的工具名（用于与 LLM 选择比对，记分歧）。"""
    return [r.id for r in _prior_rules(msg)]


def prior_hints(msg: str) -> str:
    """给元路由的**先验提示块**（空串 = 无先验）。文案刻意保留"可忽略"语义。"""
    if not PRIOR_ENABLED:
        return ''
    rules = _prior_rules(msg)
    if not rules:
        return ''
    lines = ['', '【路由先验（由确定性判据提供，仅供参考；若与整句语义不符，请以你的判断为准）】']
    for r in rules:
        tip = ('倾向 %s' % r.tool) if r.tool else '注意这是该判据的典型问法'
        lines.append('· %s：%s（%s）' % (r.what, tip, r.accident.split('；')[0][:60]))
    return '\n'.join(lines)


def note_conflict(rule_id: str, llm_tool: str) -> None:
    """记录「先验说 A、LLM 选了 B」——这条规则能否退场就看这个计数。"""
    with _lock:
        c = _CONFLICTS.setdefault(rule_id, {'n': 0, 'llm': {}})
        c['n'] += 1
        key = str(llm_tool or 'no_tool')
        c['llm'][key] = c['llm'].get(key, 0) + 1


def conflicts() -> dict:
    with _lock:
        return {k: {'n': v['n'], 'llm': dict(v['llm'])} for k, v in _CONFLICTS.items()}


def stats() -> dict:
    """汇总：命中 / 来源 / 先验分歧 / 处置分布（供管理端与 CLI）。"""
    hits = counts()
    return {
        'rules': len(RULES),
        'by_kind': {k: len(by_kind(k)) for k in ('redline', 'keep', 'converge', 'retire')},
        'hits': hits,
        'hit_total': sum(hits.values()),
        'sources': route_sources(),
        'conflicts': conflicts(),
        'prior_enabled': PRIOR_ENABLED,
        # 长期 0 命中 = 死规则候选（converge/retire 才谈得上退场）
        'dead_candidates': [r.id for r in RULES
                            if r.kind in ('converge', 'retire') and not hits.get(r.id)],
    }


# =============================================================================
# 自检 + 清单输出
# =============================================================================
def _rel_candidates(where: str) -> list:
    path = where.split(':')[0]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 通用工具/
    proj = os.path.dirname(root)                                        # 项目根
    return [path, os.path.join(root, path), os.path.join(proj, path),
            os.path.join(proj, '通用工具', path), os.path.join(proj, '用户端特用', path)]


def selfcheck() -> list:
    """返回问题清单（空 = 通过）。可挂 CI：python -m agent.route_registry --selfcheck"""
    bad = []
    seen = set()
    for r in RULES:
        if r.id in seen:
            bad.append('重复 id：%s' % r.id)
        seen.add(r.id)
        if r.kind not in ('redline', 'keep', 'converge', 'retire'):
            bad.append('%s：非法 kind %s' % (r.id, r.kind))
        if r.kind == 'converge' and not r.retire_when:
            bad.append('%s：converge 必须写退场条件（retire_when）' % r.id)
        if not r.accident:
            bad.append('%s：缺事故出处（accident）' % r.id)
        if r.probe is None and not r.detector and r.layer in ('judge', 'slot'):
            bad.append('%s：判据类必须给 probe 或 detector' % r.id)
        # 位置存在性（文件 + 行号不越界）
        if r.where and ':' in r.where and r.where.split(':')[0].endswith('.py'):
            found = None
            for c in _rel_candidates(r.where):
                if os.path.isfile(c):
                    found = c
                    break
            if found is None:
                bad.append('%s：位置文件不存在（%s）' % (r.id, r.where))
            else:
                try:
                    line = int(r.where.rsplit(':', 1)[1])
                except ValueError:
                    line = 0
                if line > 0:
                    n = sum(1 for _ in open(found, encoding='utf-8', errors='ignore'))
                    if line > n:
                        bad.append('%s：行号越界（%s 共 %d 行）' % (r.id, r.where, n))
        # 样例命中必须符合预期
        for text, expect in r.samples:
            got = _probe_hit(r, text)
            if got != bool(expect):
                bad.append('%s：样例不符 → %r 期望 %s 实际 %s' % (r.id, text, expect, got))
    return bad


def dump_markdown(kind: str = '') -> str:
    """打印全量清单（markdown 表格），供文档 / 管理端复用。"""
    hits = counts()
    rows = [r for r in RULES if not kind or r.kind == kind]
    out = ['| id | 处置 | 层 | 位置 | 判据 | 事故出处 | 退场条件 | 命中 |',
           '|---|---|---|---|---|---|---|---|']
    label = {'redline': '红线·永不退场', 'keep': '保留', 'converge': '收敛', 'retire': '退场候选'}
    for r in rows:
        out.append('| `%s` | %s | %s | %s | %s | %s | %s | %s |' % (
            r.id, label.get(r.kind, r.kind), r.layer, r.where, r.what,
            r.accident, r.retire_when or '—', hits.get(r.id, 0)))
    st = stats()
    out.append('')
    out.append('合计 %d 条（红线 %d / 保留 %d / 收敛 %d / 退场候选 %d）；先验注入：%s'
               % (st['rules'], st['by_kind']['redline'], st['by_kind']['keep'],
                  st['by_kind']['converge'], st['by_kind']['retire'],
                  'on' if st['prior_enabled'] else 'off'))
    return '\n'.join(out)


if __name__ == '__main__':
    import sys
    args = sys.argv[1:]
    if '--selfcheck' in args:
        problems = selfcheck()
        for p in problems:
            print('[FAIL]', p)
        print('selfcheck:', 'PASS（%d 条规则）' % len(RULES) if not problems
              else 'FAIL（%d 个问题）' % len(problems))
        sys.exit(1 if problems else 0)
    if '--stats' in args:
        import json as _json
        print(_json.dumps(stats(), ensure_ascii=False, indent=1))
        sys.exit(0)
    k = ''
    if '--kind' in args:
        try:
            k = args[args.index('--kind') + 1]
        except IndexError:
            k = ''
    print(dump_markdown(k))
