# -*- coding: utf-8 -*-
"""确定性前置闸：这些分支**绝不进 LLM**。

两类：
  1) 违规禁区固定话术。
  2) 状态机续接（计算向导 calc_state / 好感补参 favor_pending）——整段交回 legacy 确定性处理，
     避免 LLM 把向导中的一步输入（如船名）误当成新查询。

未实装舰名不再放在前置闸：同一名称可能出现在攻略/海军知识问答中，
必须先由模型区分「查游戏角色」和「问知识事实」；前者在工具未命中后仍会回落 legacy 得到未实装提示。
"""
from core.calc_flow import (
    _BANNED_RE,
    _resolve_pronoun,
    route_work as _legacy_route_work,
)

import re


# 统一「资料不足 / 查不到」话术（用户口径 2026-09-22）：全项目未命中一律用这一句，
# 不再为单个分支写专属话术。文案与 core/calc_flow.py::_query_text 的兜底完全一致
# （两处同文，改文案要一起动）。
_NO_DATA_REPLY = '我暂时没查到相关的信息，指挥官。'


# 用户指出回答不实/答错时，必须先在确定性闸门里撤回并复核上一条问题，
# 不能再把质疑当普通闲聊交给自由生成模型。
_ANSWER_CHALLENGE_RE = re.compile(
    r'胡说|瞎说|乱说|编(?:造|的|了|吧|故事)|杜撰|不对|错了|答错|有误|'
    r'真的假的|你确定|靠谱吗|哪来的|没有这回事|知识库.{0,6}(?:没有|不存在)|'
    r'好像.{0,6}(?:不对|在编|胡说)'
)
_UNSUPPORTED_ARCHIVE_RE = re.compile(r'RA[\s\-－—_]*0*1\s*(?:档案|编号)?', re.I)
# 库外问法（2026-09-25 随机问答发现）：未来/未公布内容 与 创作请求，知识库无据——
# 与「不存在的 RA-01 档案」同族（自由生成链路会臆造），统一在前置闸止住并回 _NO_DATA_REPLY。
# 只拦「下期…会不会/什么时候/出什么/叫什么」这类猜测式问法，「下期活动怎么打/带路条件」不拦。
_NO_KB_FUTURE_RE = re.compile(
    r'(?:下期|下个版本|下一个版本|以后的?版本|未来|明年)[^。！？\n]{0,12}'
    r'(?:会不会|是否|什么(?:时候|时)|何时|要出|出新|新(?:舰灵|角色|皮肤|活动)|叫什么)'
    r'|会不会(?:加强|削弱|出|上线|进|改)'
    r'|(?:什么(?:时候|时)|何时)(?:上线|开放|公测|开服)'
    r'|公测|内测|爆料|剧透|舅舅党'
)
_NO_KB_CREATIVE_RE = re.compile(
    r'(?:写|编|来|整|作|弄|搞)(?:一)?(?:段|篇|个|首)?[^。！？\n]{0,8}'
    r'(?:故事|同人|小作文|段子|小说|诗|歌词|剧本|梗)'
    r'|同人(?:文|小说|故事)|角色扮演|扮演一下'
)
_ARCHIVE_ACTION_RE = re.compile(r'^(?:帮我)?(?:调取|打开|展开|查看|看下|看看)(?:一下)?[？?。.!！]*$')
_UNANSWERABLE_REPLY_RE = re.compile(
    r'知识库.{0,12}(?:没有|未找到|找不到|缺少|不足)|'
    r'没有足够信息|暂无.{0,8}(?:信息|资料|依据)|暂时不支持该角色|'
    r'我暂时没查到|无法根据.{0,12}(?:回答|判断)|无法回答'
)
_FOLLOWUP_RE = re.compile(
    r'^(?:那|那么|所以|可是|但是|然后|还有|再说|继续|具体|到底|为啥|为什么|凭什么|'
    r'哪个|哪一个|谁|谁更|哪个更|怎么说|咋说|然后呢|那呢|呢|吗|真的|确定)[\s\S]{0,24}$|'
    r'她|他|它|这个|那个|这艘|那艘|该角色|该舰|前面|刚才|上面|比较|对比|更好|更强|高下'
)
_CLEAR_TOPIC_RE = re.compile(
    r'天赋|技能|必杀|通常技|支援技|面板|炮击|雷击|航空|索敌|装填|舰种|稀有|'
    r'配装|装备|攻略|怎么练|怎么养|培养|获取|抽取|计算|模拟|活动|天气|时间|'
    r'你好|早上好|中午好|晚上好|晚安|换个话题|说点别的|另一个问题|新问题|你能做什么'
)


def _last_history(history, role):
    for item in reversed(list(history or [])):
        if str((item or {}).get('role') or '') != role:
            continue
        content = str((item or {}).get('content') or '').strip()
        if content:
            return content
    return ''


def guarded_backend_reason(msg, history=None):
    """供完整分发层和路由测试共用的确定性拦截判据。"""
    m = (msg or '').strip()
    if _ANSWER_CHALLENGE_RE.search(m):
        return 'answer_challenge'
    if _UNSUPPORTED_ARCHIVE_RE.search(m):
        return 'unsupported_archive'
    if (_ARCHIVE_ACTION_RE.match(m)
            and _UNSUPPORTED_ARCHIVE_RE.search(_last_history(history, 'assistant'))):
        return 'unsupported_archive'
    if _NO_KB_FUTURE_RE.search(m) or _NO_KB_CREATIVE_RE.search(m):
        return 'no_kb_basis'
    return ''


def track_unanswered_result(overrides, msg, result):
    """记录“有明确问题但资料不足”的主题，供下一轮前置闸判断。"""
    if not isinstance(result, dict) or overrides is None:
        return result
    reply = str(result.get('reply') or '')
    if result.get('ok') is False or '服务器ojbk' in reply or '额度已耗尽' in reply or '认证失败' in reply:
        return result
    if _UNANSWERABLE_REPLY_RE.search(reply):
        overrides['_unanswered_topic'] = {'question': (msg or '').strip()[:240]}
    return result


def _guard_unanswered_topic(overrides, msg):
    state = (overrides or {}).get('_unanswered_topic')
    if not state:
        return None
    m = (msg or '').strip()
    # 明确提出可独立识别的新任务/字段，视为换题并解除状态；之后照常走完整路由。
    if _CLEAR_TOPIC_RE.search(m) and not re.search(r'比较|对比|哪个更|谁更|高下', m):
        overrides.pop('_unanswered_topic', None)
        return None
    # 省略追问、质疑原因、继续比较等仍属于原问题，不再反复猜测或生成内容。
    if len(m) <= 8 or _FOLLOWUP_RE.search(m):
        return _work_dict('这个问题没有足够信息继续回答，换个话题吧。')
    # 一条完整的新问题默认视为换题，避免状态锁死后续对话。
    overrides.pop('_unanswered_topic', None)
    return None


def _work_dict(reply, **extra):
    d = {'reply': reply, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
         'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    d.update(extra)
    return d


def guard(tools, overrides, msg, agent=None, history=None):
    """返回 (handled, result)。handled=True 时 result 即最终路由结果（dict 或 None）。"""
    m = (msg or '').strip()

    # 1) 违规/禁区：固定话术，且不写入会话历史（no_history）
    if _BANNED_RE.search(m):
        return True, _work_dict('与阿卡莉无关，不回答。', no_history=True)

    unanswered = _guard_unanswered_topic(overrides, m)
    if unanswered is not None:
        return True, unanswered

    # 2) 不存在的“RA-01 档案”曾由自由生成链路臆造。相关追问和“调取”动作都在
    # 前置闸门止住 —— **拦截行为保留**（安全阀：删掉拦截＝放开幻觉，不能退），
    # 但不再给专属解释话术（用户口径 2026-09-22），统一用 _NO_DATA_REPLY；
    # 附带动作 expression_hint='惊讶' 按要求保留。
    guarded_reason = guarded_backend_reason(m, history)
    if guarded_reason == 'unsupported_archive':
        return True, _work_dict(_NO_DATA_REPLY, expression_hint='惊讶')

    # 2b) 库外问法（未来/未公布内容、创作请求）：知识库无据，统一 _NO_DATA_REPLY，不进自由生成。
    if guarded_reason == 'no_kb_basis':
        return True, _work_dict(_NO_DATA_REPLY)

    # 3) 用户质疑上一条回答：先撤回，再把最近一条用户问题补全指代后送回确定性
    # 业务处理层复核。复核失败则只承认无依据，不允许自由生成模型自行“校验通过”。
    if guarded_reason == 'answer_challenge':
        previous_question = _last_history(history, 'user')
        checked = None
        if previous_question and previous_question != m:
            replay = _resolve_pronoun(previous_question, overrides)
            checked = _legacy_route_work(
                tools, overrides, replay, agent=agent, allow_intent_llm=False,
                update_recent=False)
        if checked is not None:
            checked = dict(checked)
            checked['reply'] = ('你质疑得对。上一条没有可靠依据，我撤回。\n\n'
                                '重新按角色数据和知识库复核：\n' +
                                str(checked.get('reply') or '').strip())
            checked['expression_hint'] = '惊讶'
            return True, checked
        return True, _work_dict(
            '你质疑得对。上一条没有通过角色数据或知识库验证，我撤回；'
            '目前没有可靠内容可以支持那项说法。',
            expression_hint='惊讶')

    # 4) 状态机续接：整段交回 legacy（确定性、结构化解参），禁止 LLM 介入
    if overrides.get('calc_state') or overrides.get('favor_pending'):
        return True, _legacy_route_work(tools, overrides, m, agent=agent, allow_intent_llm=False)

    return False, None
