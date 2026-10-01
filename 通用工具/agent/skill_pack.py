# -*- coding: utf-8 -*-
"""技能包（Skill Pack）：把工具按「能力」打包，作为 Agent 能力的组织与启用单元。

为什么要有这一层
----------------
此前工具是**平铺**的：`tool_registry` 直接列 13 个工具名，开关只有 core/full 两档，
带来三个实际问题：

  ① 想只关掉某一类能力（比如"这轮不要查天气"），只能改代码里的元组，不能按能力开关；
  ② 工具与 `prompts.ROUTER_SYSTEM` 里那一大段**用法与边界约定**是分开维护的，
     关掉某工具后提示词还在讲它，LLM 会照着提示去调一个根本没绑定的工具；
  ③ 新增一项能力要同时改三处（`tools/__init__.build_tools` / `tool_registry` / `prompts`）。

技能包把「工具集合 + 什么时候用 + 输出口径」收在一处声明：**一个包 = 一项能力**，
开关以能力为粒度，新增能力只改这一处。

设计约束（与现有架构保持一致，这层不得突破）
--------------------------------------------
- **不做"选包→选工具"的两跳**：技能包是**组织与启用单元**，不是路由单元；包内工具由同一
  次元路由按 when/示例直接选。两跳会显著增加延迟与误判面。
  （用户口径 2026-09-28：允许**多次 LLM 意图分析**——仅"进入明确工具流程"豁免；
   但"选包再选工具"的两跳仍不引入。）
- **原文直出**：数据类包沿用 `OUTPUT_VERBATIM`，工具输出逐字返回、不回灌 LLM
  （见 `tool_adapter.invoke_tool`）。
- **不膨胀 prompt**：默认只启 `default=True` 的包；`prompt_hint` 只作**留位**，
  默认不注入 `ROUTER_SYSTEM`（那段提示是长期实战调出来的整体口径，裁剪需单独评估）。

零工具的包（casual / reminder / calc_wizard）
--------------------------------------------
这三项能力**不经过工具**：闲聊走 web_app 闲聊链路，日程提醒由 `core.reminder` 确定性
生成后织入首页横幅，多轮计算由 `core.calc_flow` 状态机承接。它们在这里只是**能力声明**
（便于统一展示/将来挂提示），真正的开关需由消费方调用 `is_enabled()` 判断 —— 目前
`reminder` 已接线（web_app.index 拼装横幅前判断），`casual` / `calc_wizard` 待接。

用法
----
    AKALI_AGENT_SKILLS=lookup,guide_qa       # 只开这两项
    AKALI_AGENT_SKILLS=+panel_manage         # 默认集之上追加
    AKALI_AGENT_SKILLS=-reminder             # 默认集之上移除（关掉日程提醒）
    AKALI_AGENT_SKILLS=-query_weather        # 工具级逃生口：按工具名剔除
    AKALI_AGENT_TOOLS=full                   # 旧开关仍有效：全开（含 default=False）
"""
import os

# ---- 输出口径 ----
OUTPUT_VERBATIM = 'verbatim'   # 原文直出（确定性数据工具）
OUTPUT_GROUNDED = 'grounded'   # 依据检索片段作答（检索工具）
OUTPUT_FLOW = 'flow'           # 不直出，交后端状态机 / 闲聊链路

# ---- 能力类别 ----
KIND_DATA = 'data'             # 确定性查询 / 计算
KIND_RETRIEVAL = 'retrieval'   # 向量检索 → 约束式作答
KIND_BACKEND = 'backend'       # 由后端确定性流程承接


class SkillPack(object):
    """一项能力 = 工具集合 + 触发场景 + 输出口径。

    name:        唯一标识（用于 AKALI_AGENT_SKILLS）
    title:       人类可读名（控制台/日志展示）
    when:        「什么时候该用这项能力」的自然语言约定，与 ROUTER_SYSTEM 同口径；
                 目前只作文档与将来裁剪提示词的依据，不参与运行时判定。
    tools:       该能力依赖的工具名（必须是 build_tools() 里已构造的名字）
    kind:        data / retrieval / backend
    output:      verbatim / grounded / flow
    default:     是否随 core 档默认启用（False = 只在 full 或显式点名时启用）
    prompt_hint: 该能力专属提示片段。当前**不注入**：ROUTER_SYSTEM 是长期实战调出来的
                 整体口径，按包裁剪会让描述被切碎。留位待单独评估。
    """

    __slots__ = ('name', 'title', 'when', 'tools', 'kind', 'output', 'default', 'prompt_hint')

    def __init__(self, name, title, when, tools, kind=KIND_DATA,
                 output=OUTPUT_VERBATIM, default=True, prompt_hint=''):
        self.name = name
        self.title = title
        self.when = when or ''
        self.tools = tuple(tools or ())
        self.kind = kind
        self.output = output
        self.default = bool(default)
        self.prompt_hint = prompt_hint or ''

    def __repr__(self):
        return '<SkillPack %s (%s/%s) tools=%d>' % (
            self.name, self.kind, self.output, len(self.tools))

    def as_dict(self):
        """供控制台 / 排障输出（不含 when 与 prompt_hint，避免长文本刷进日志）。"""
        return {'name': self.name, 'title': self.title, 'kind': self.kind,
                'output': self.output, 'default': self.default,
                'tools': list(self.tools)}


# =============================================================================
# 技能包注册表
#
# 分组口径（用户口径 2026-09-28）：查询归一类、计算归一类、闲聊归一类、日程提醒归一类；
# 剩下按"输出口径差异"单列 —— guide_qa 是唯一 retrieval 类（依据片段作答），
# calc_wizard 是多轮状态机（与单次计算的 calculation 区分）。
#
# 默认展开结果与改造前的 TIER1 **完全一致**（13 个工具），保证零行为变化。
# =============================================================================
SKILL_PACKS = (
    SkillPack(
        name='lookup',
        title='查询',
        when='用户要查确定性数据：单角色面板/技能/天赋 → query_ship_info；'
             '按技能效果条件找角色 → query_ships_by_skill；按天赋名/效果反查 → query_talent_ships；'
             '按舰种/尺寸/阵营/稀有度/改造状态组合筛选 → ship_filter；'
             '多角色索敌合计 → query_scout_sum（输出的是合计值，仍属查询口径）；'
             '攻略目录/清单与培养改造优先级 → query_guide；'
             '弹种/装甲克制的确切数值 → ammo_mech_qa；'
             '活动信息 → query_activity；天气 → query_weather；'
             'PVP 阵容参考 → pvp_role_recommend。'
             '含"适合/高伤/实战用途"等攻略评价结论时不属本能力，走 guide_qa。',
        tools=('query_ship_info', 'query_ships_by_skill', 'query_talent_ships',
               'ship_filter', 'query_scout_sum', 'query_guide', 'ammo_mech_qa',
               'query_activity', 'query_weather', 'pvp_role_recommend'),
        kind=KIND_DATA, output=OUTPUT_VERBATIM, default=True,
    ),
    SkillPack(
        name='calculation',
        title='单次计算',
        when='单次即可算完、无需多轮填参：好感升级需求 → calc_favor；'
             '装填对普攻 CD 的缩减 → calc_reload_cd。'
             '需要多轮收集参数的伤害计算/遭遇战/PVP 模拟属 calc_wizard。',
        tools=('calc_favor', 'calc_reload_cd'),
        kind=KIND_DATA, output=OUTPUT_VERBATIM, default=True,
    ),
    SkillPack(
        name='guide_qa',
        title='攻略知识问答',
        when='玩法/机制/怎么打/怎么练/配装/阵容/装备自选/技能等级换算等需查攻略库的问题 → guide_rag，'
             '参数 query 用用户原话；LLM 依据检索片段作答，不得脱离片段自由发挥。',
        tools=('guide_rag',),
        kind=KIND_RETRIEVAL, output=OUTPUT_GROUNDED, default=True,
    ),
    SkillPack(
        name='calc_wizard',
        title='多轮计算向导',
        when='伤害计算、遭遇战模拟、PVP 模拟等需多轮收集参数的计算流程，'
             '由后端 core.calc_flow 状态机承接（不绑工具）。'
             '询问"计算规则/为什么这样处理"属 guide_qa。',
        tools=(),
        kind=KIND_BACKEND, output=OUTPUT_FLOW, default=True,
    ),
    SkillPack(
        name='casual',
        title='闲聊',
        when='问候、情绪、生活话题、调情、对阿卡莉本人的轻度吐槽、询问助手自身能力 —— '
             '不调任何工具，交回 web_app 闲聊链路（唯一自由生成通路）。',
        tools=(),
        kind=KIND_BACKEND, output=OUTPUT_FLOW, default=True,
    ),
    SkillPack(
        name='reminder',
        title='日程提醒',
        when='礼包/活动截止/月度兑换提醒（core.reminder.collect_reminders）与邮件、限时远征'
             '（web_app._affair_hints），确定性生成后织入首页顶部事务提醒横幅。'
             '已在 web_app.index() 接线：关闭时不注入横幅。',
        tools=(),
        kind=KIND_BACKEND, output=OUTPUT_FLOW, default=True,
    ),
    SkillPack(
        name='boundary',
        title='边界与违规',
        when='违规（涉政/违法/暴力/竞品）与越界情色的判定与固定话术，统一由 core.boundary 提供；'
             '由 guard **在路由前**拦截（避免漏进工具/攻略路由），话术按「主体」分流'
             '（冲着阿卡莉本人 / 某个角色）。不绑工具。',
        tools=(),
        kind=KIND_BACKEND, output=OUTPUT_FLOW, default=True,
    ),
    # ---- 默认不启（保持原 full 档语义）----
    SkillPack(
        name='panel_manage',
        title='我的舰船副本',
        when='读取/列出用户自己保存的舰船面板副本。默认不绑，避免 prompt 膨胀。',
        tools=('query_ship_copy', 'list_user_ships'),
        kind=KIND_DATA, output=OUTPUT_VERBATIM, default=False,
    ),
)

_BY_NAME = {p.name: p for p in SKILL_PACKS}

# 全部已注册工具名（用于 -工具名 逃生口：判断某个 -x 是包名还是工具名）
_ALL_TOOL_NAMES = frozenset(t for p in SKILL_PACKS for t in p.tools)


def all_skill_packs():
    """全部技能包（按声明顺序）。"""
    return SKILL_PACKS


def get_skill(name):
    """按名取技能包；不存在返回 None。"""
    return _BY_NAME.get((name or '').strip())


def default_skill_packs():
    """默认启用的包（core 档）。"""
    return tuple(p for p in SKILL_PACKS if p.default)


def tools_of(packs):
    """从若干技能包展开工具名（保持包声明顺序、按名去重）。"""
    seen = set()
    out = []
    for p in packs or ():
        for t in p.tools:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return tuple(out)


def resolve_skill_names():
    """解析 AKALI_AGENT_SKILLS → (启用的包名元组, 需剔除的工具名元组)。

    三种写法可混用（从左到右生效）：
        a,b          显式点名（以该列表为基准；只认包名）
        +x           在基准上追加包
        -x           在基准上移除包；若 x 不是包名而是已注册工具名，则记为"剔除该工具"
    """
    raw = (os.environ.get('AKALI_AGENT_SKILLS') or '').strip()
    items = [n.strip() for n in raw.replace('，', ',').split(',') if n.strip()]
    base = [p.name for p in default_skill_packs()]
    if not items:
        return tuple(base), ()

    picked = [n for n in items if not n.startswith(('+', '-')) and n in _BY_NAME]
    result = list(picked) if picked else list(base)
    drop_tools = []
    for n in items:
        if n.startswith('+'):
            add = n[1:].strip()
            if add in _BY_NAME and add not in result:
                result.append(add)
        elif n.startswith('-'):
            rm = n[1:].strip()
            if rm in _BY_NAME:
                if rm in result:
                    result.remove(rm)
            elif rm in _ALL_TOOL_NAMES:
                # 工具级逃生口：分组变粗后仍能单独摘掉某个工具
                if rm not in drop_tools:
                    drop_tools.append(rm)
    return tuple(result), tuple(drop_tools)


def enabled_skill_names():
    """当前生效的技能包名（考虑旧开关 AKALI_AGENT_TOOLS=full）。"""
    scope = (os.environ.get('AKALI_AGENT_TOOLS') or 'core').strip().lower()
    if scope == 'full':
        return tuple(p.name for p in SKILL_PACKS)
    return resolve_skill_names()[0]


def enabled_skill_packs():
    """当前生效的技能包对象列表。"""
    return tuple(_BY_NAME[n] for n in enabled_skill_names() if n in _BY_NAME)


def enabled_tool_names():
    """当前生效的工具名（由技能包展开，扣除工具级逃生口）。

    tool_registry.selected_names() 的新实现。
    """
    scope = (os.environ.get('AKALI_AGENT_TOOLS') or 'core').strip().lower()
    if scope == 'full':
        return tools_of(SKILL_PACKS)
    _names, drop = resolve_skill_names()
    return tuple(t for t in tools_of(enabled_skill_packs()) if t not in drop)


def is_enabled(name):
    """某项能力当前是否启用 —— 供零工具能力（reminder 等）的消费方接线判断。

    未设置 AKALI_AGENT_SKILLS 时一律视为启用（保持既有行为）。
    """
    n = (name or '').strip()
    if not n:
        return False
    if not (os.environ.get('AKALI_AGENT_SKILLS') or '').strip():
        return True
    return n in enabled_skill_names()


def summary():
    """当前启用的能力概况（控制台/排障用）。"""
    return {'skills': [p.as_dict() for p in enabled_skill_packs()],
            'tools': list(enabled_tool_names())}
