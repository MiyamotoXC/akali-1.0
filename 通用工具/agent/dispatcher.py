# -*- coding: utf-8 -*-
"""agent 路由编排：三分支判定 —— ①数据工具 ②检索工具 ③闲聊。

LLM 对每条输入先判定走哪条分支（不生成回复）：

  guard（确定性前置闸，不进 LLM）
    └─ 命中 → 固定话术 / 状态机续接（整段交回 legacy）
  ①数据工具   → 逐字执行，**回复原文直出**（LLM 不得转述）
  ②检索工具   → 向量检索片段 → LLM **依据片段**简答（约束式生成，见 calc_flow._handle_guide_qa）
  ③都不调     → 交回 legacy 处理确定性固定分支；legacy 也 None → 返回 None，
                由 web_app 闲聊链路用 LLM 生成（**闲聊/调情是唯一自由生成通路**）

核心约束：工具结果执行后**直接返回，永不回灌 LLM**。
灰度：本模块仅在 AKALI_ROUTE_MODE=agent 时被 core/route_dispatch 懒加载。
"""
from core.calc_flow import (
    _activity_guide_reply,
    _guide_reply_from_text,
    _handle_guide_qa,
    _handle_tips,
    _resolve_pronoun,
    _ship_panel_reply,
    _DIM_KW_RE,
    route_work as _legacy_route_work,
)

import json
import re

from .config import get_chat_model
from .guard import guard, guarded_backend_reason
from .prompts import ROUTER_SYSTEM
from .tool_adapter import invoke_tool, tool_map
from .tool_registry import is_retrieval, select_tools

_NOT_FOUND_PREFIX = '未找到角色'

# 属性筛选确定性前置闸：命中则强制走 ship_filter（跳过 LLM 选工具），
# 避免 LLM 误选 query_ships_by_skill（该工具无 refit/完整性维度）导致改造舰/缺信息舰泄漏。
# 用户 2026-09-15 收口：「明确工具调用不能被误选拦截」。
_ATTR_FILTER_RE = re.compile(
    r'没\s*改造|未\s*改造|无\s*改造|非\s*改造|不\s*改造|没有\s*改造|仅\s*改造|改造\s*舰?|'
    r'稀有度|[彩金紫蓝绿白]色?(?:角色|舰灵|船)|'
    r'(?:小|中|大)(?:船|型)|'
    r'(?:舰种|阵营|国籍)筛选|筛选(?:舰种|阵营|国籍|稀有度)|'
    r'(?:驱逐|轻巡|重巡|战巡|战列|航母|轻母|装母|潜艇|水母|补给|雷巡)'
)
_SKILL_EFFECT_RE = re.compile(
    r'护盾|回血|吸血|炮击|雷击|航空|命中|装填|暴击|减伤|增伤|回避|攻速|移速|点燃|控制|自奶|奶|增益'
)

# 对阿卡莉本人的轻度吐槽/逗弄属于关系互动，不是知识问题。只在明确指向助手、
# 且没有工作诉求时生效，避免把“哪个舰灵笨/呆”之类角色问题误判成闲聊。
_ASSISTANT_TEASE_RE = re.compile(
    r'(?:你|阿卡莉|莉酱|秘书).{0,8}(?:笨|呆呆|发呆|傻乎乎|木头|迟钝|笨蛋)'
    r'|(?:笨|呆呆|傻乎乎|木头|迟钝).{0,6}(?:阿卡莉|秘书)'
)
_TEASE_WORK_RE = re.compile(
    r'攻略|知识库|资料|查询|查一下|计算|伤害|面板|技能|天赋|装备|配装|阵容|'
    r'舰灵|角色|关卡|活动|索敌|数值|为什么答错|哪里错'
)

# “某舰天赋有哪些”是面板查询；“点天赋后实际减伤多少/等效提升多少”问的是
# 实测机制与攻略结论。后者必须检索攻略，不能被“天赋”二字截成角色面板。
_TALENT_EFFECT_Q_RE = re.compile(
    r'天赋.{0,20}(?:实际|等效|点亮|点了|生效|提升|增加|降低|减少|减伤|增伤|前后|怎么算|多少|几成)'
    r'|(?:实际|等效|点亮|点了|生效|提升|增加|降低|减少|减伤|增伤|前后).{0,20}天赋'
)


def _is_assistant_tease(msg):
    m = (msg or '').strip()
    return bool(m and len(m) <= 40 and _ASSISTANT_TEASE_RE.search(m)
                and not _TEASE_WORK_RE.search(m))

# 「培养/改造优先级」问句不是属性筛选（用户口径 2026-09-17）：
# 「哪些舰灵值得优先改造」要的是**优先级结论**（知识库《舰灵改造优先级》按有据内容作答），
# 不是按「改造」维度列出的名单——否则 `_ATTR_FILTER_RE` 的 `改造` 会把它强制送进 ship_filter。
# 豁免条件收紧：含优先级词 + 含列举问法 + **不含**舰种/尺寸/阵营/稀有度维度
#（"哪些值得改造的航母"仍走筛选名单，与 core/intents.py 的 evaluate 处理器 `_DIM_KW_RE` 守卫同口径）。
_PRIORITY_Q_RE = re.compile(r'值得|优先|优先级|先练|先养|推荐|培养优先')
_FILTER_ASK_RE = re.compile(r'哪些|哪艘|哪个|哪条|什么|谁|有没有|列举')


def _is_priority_question(msg):
    m = msg or ''
    if not _PRIORITY_Q_RE.search(m) or _DIM_KW_RE.search(m):
        return False
    # “改造优先级”“培养优先推荐”这类名词式短问同样是在索要结论，
    # 不要求必须带“哪些/哪个”等疑问词，否则会被“改造”误送到属性名单。
    return bool(_FILTER_ASK_RE.search(m)
                or re.search(r'优先级|值得|先练|先养|培养优先', m)
                or (re.search(r'推荐', m) and re.search(r'改造|培养|练|养', m)))


def _is_attribute_filter(msg):
    """属性筛选判定：含筛选维度信号且无技能效果词 → 强制 ship_filter（确定性，不经 LLM）。"""
    m = msg or ''
    if not m or len(m) > 80:
        return False
    if _SKILL_EFFECT_RE.search(m):
        return False
    if _is_priority_question(m):
        return False
    return bool(_ATTR_FILTER_RE.search(m))


def _work_dict(reply, tool_outputs=None, **extra):
    d = {'reply': reply, 'tool_outputs': list(tool_outputs or []), 'suggestions': [],
         'fill_form': [], 'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True}
    d.update(extra)
    return d


def _usable(text) -> bool:
    """工具是否产出了可交付结果，参数/字段错误不得当成成功回答。"""
    t = (text or '').strip()
    invalid = (_NOT_FOUND_PREFIX, '不支持的字段', '工具调用参数错误')
    return bool(t) and not t.startswith(invalid)


def _select_route(msg, sel_tools, history=None):
    """用一个强制调用的元路由函数，让模型先判意图，再选唯一业务工具及参数。

    直接绑定全部业务工具时，部分 OpenAI 兼容端点会偶发返回空 tool_calls，
    或在「多个舰名+具体知识问题」上同时调多个角色查询工具。
    元路由保留一次模型意图分析，并在协议层保证唯一选择。
    """
    # 与实际执行时的 guard 共用同一判据：用户质疑回答、追问虚构档案时，
    # 路由测试也应如实显示 backend_flow，而不是依赖模型碰巧分类正确。
    if guarded_backend_reason(msg, history):
        return {'intent': 'backend_flow', 'name': 'no_tool', 'args': {}}
    if _is_assistant_tease(msg):
        return {'intent': 'casual', 'name': 'no_tool', 'args': {}}

    llm = get_chat_model(temperature=0.0)
    names = [getattr(t, 'name', '') or '' for t in sel_tools]
    names = [name for name in names if name]
    route_tool = {
        'type': 'function',
        'function': {
            'name': 'select_route',
            'description': '分析用户输入，选择唯一业务工具；闲聊或后端向导选 no_tool。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'intent': {
                        'type': 'string',
                        'enum': ['data_lookup', 'knowledge_qa', 'document_browse',
                                 'live_lookup', 'calculation', 'backend_flow', 'casual'],
                        'description': '先对用户整句话做意图分析，不受工具名影响。',
                    },
                    'tool_name': {'type': 'string', 'enum': names + ['no_tool']},
                    'arguments': {
                        'type': 'object',
                        'description': '所选工具的参数对象；no_tool 时为空对象。',
                    },
                },
                'required': ['intent', 'tool_name', 'arguments'],
            },
        },
    }
    # 元路由未携带各工具的 schema，因此在 system 中附上精简参数目录。
    catalog = []
    for tool in sel_tools:
        params = getattr(tool, 'parameters', None) or {}
        desc = str(getattr(tool, 'description', '') or '').strip()[:240]
        # 元路由的 arguments 是通用 object，因此必须把每个业务工具的
        # 完整嵌套 schema 给 Qwen；只给参数名会导致 conditions 等 object 被误传成字符串。
        catalog.append('%s：%s\n参数Schema=%s' % (
            getattr(tool, 'name', ''), desc,
            json.dumps(params, ensure_ascii=False, separators=(',', ':'))))
    system = ROUTER_SYSTEM + '\n可用工具参数目录：\n' + '\n'.join(catalog)
    bound = llm.bind_tools([route_tool], tool_choice='select_route')
    messages = [('system', system)]
    for item in list(history or [])[-6:]:
        role = str((item or {}).get('role') or '')
        content = str((item or {}).get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            messages.append((role, content[:1600]))
    messages.append(('user', msg))
    resp = bound.invoke(messages)
    calls = list(getattr(resp, 'tool_calls', None) or [])
    if not calls:
        # 兼容端点在并发限流时偶尔返回空 tool_calls。模型已经完成过本轮尝试，
        # 若当前句恰好是人工维护的标准攻略问法，则直接走对应检索，避免掉进旧规则。
        try:
            from tools.knowledge import match_guide_question
            if 'guide_rag' in names and match_guide_question(msg):
                return {'intent': 'knowledge_qa', 'name': 'guide_rag', 'args': {'query': msg}}
        except Exception:
            pass
        return {'intent': '', 'name': 'no_tool', 'args': {}}
    meta = calls[0]
    meta_args = _tc_field(meta, 'args', {}) or {}
    intent = str(meta_args.get('intent') or '')
    name = str(meta_args.get('tool_name') or '')
    args = meta_args.get('arguments') or {}
    # 人工维护的标准问题/改写问法本身就是明确的攻略问答意图。模型仍先完成意图分析，
    # 此处只做结果一致性校验，防止“温泉…在哪看”被误解成打开文档、
    # “装备优先级”被误解成装备目录、“立绘太糊”被误解成主观皮肤评价。
    try:
        from tools.knowledge import match_guide_question
        exact_guide = match_guide_question(msg)
    except Exception:
        exact_guide = None
    if exact_guide and 'guide_rag' in names:
        return {'intent': 'knowledge_qa', 'name': 'guide_rag', 'args': {'query': msg}}
    # 协议层校验“先意图、后工具”的结果是否自洽。不自洽时不猜第二条路由，
    # 交给统一意图分发降级链。
    if intent == 'knowledge_qa' and name != 'guide_rag':
        return {'intent': intent, 'name': 'no_tool', 'args': {}}
    if intent == 'document_browse' and name != 'query_guide':
        return {'intent': intent, 'name': 'no_tool', 'args': {}}
    if intent in ('backend_flow', 'casual') and name != 'no_tool':
        return {'intent': intent, 'name': 'no_tool', 'args': {}}
    if name == 'guide_rag' and intent != 'knowledge_qa':
        return {'intent': intent, 'name': 'no_tool', 'args': {}}
    if name == 'query_guide' and intent != 'document_browse':
        return {'intent': intent, 'name': 'no_tool', 'args': {}}
    if name == 'query_scout_sum' and intent != 'data_lookup':
        return {'intent': intent, 'name': 'no_tool', 'args': {}}
    if name == 'no_tool' or name not in names or not isinstance(args, dict):
        return {'intent': intent, 'name': 'no_tool', 'args': {}}
    # 对路由模型的字段关键词误判做语义一致性校正；这仍是同一层意图分析，
    # 只把“天赋实测效果”从面板字段恢复为攻略知识问答。
    if _TALENT_EFFECT_Q_RE.search(msg or '') and 'guide_rag' in names:
        return {'intent': 'knowledge_qa', 'name': 'guide_rag', 'args': {'query': msg}}
    # 参数保真：当前句明确写了“大拉菲/小拉菲”时不得在上下文补全后把限定词丢掉。
    # 这不是独立路由，只校正模型已选中的角色查询参数。
    if name == 'query_ship_info':
        explicit_laffey = re.search(r'[大小]拉菲', msg or '')
        if explicit_laffey:
            args['ship_name'] = explicit_laffey.group(0)
        # 当前句明确点名字段时，以原文为准补回字段，防止模型只填舰名后误开整张角色面板。
        for field in ('必杀技', '通常技', '支援技', '旗舰技', '天赋', '舰种', '稀有',
                      '装填', '炮击', '雷击', '航空', '索敌', '命中', '幸运', '装甲',
                      '对空', '机动', '血量', '技能'):
            if field in (msg or ''):
                args['field'] = field
                break
    return {'name': name, 'args': args, 'intent': intent}


def _select_tool_calls(msg, sel_tools, history=None):
    """兼容测试/调试调用；业务分发使用 _select_route 保留 no_tool 意图。"""
    decision = _select_route(msg, sel_tools, history=history)
    return [] if decision.get('name') == 'no_tool' else [decision]


def _tc_field(tc, key, default=None):
    if isinstance(tc, dict):
        return tc.get(key, default)
    return getattr(tc, key, default)


def _execute(tool_calls, tools, overrides=None):
    """①数据工具分支：逐字执行，原文直出。返回 None 表示"未命中"（交上层回落）。"""
    tm = tool_map(tools)
    texts = []
    for tc in tool_calls:
        name = _tc_field(tc, 'name', '') or ''
        args = _tc_field(tc, 'args', {}) or {}
        print('[route.execute] tool=%s args=%s' % (
            name, json.dumps(args, ensure_ascii=False)), flush=True)
        t = tm.get(name)
        if t is None:
            continue
        if name == 'query_guide':
            text = invoke_tool(t, args)
            kw = (args or {}).get('keyword') or ''
            res = _guide_reply_from_text(text, kw)
            files = res.get('guide_files') or []
            # 唯一命中一篇时，聚焦词与 legacy 精确匹配口径一致（用文档标题而非关键词）
            if len(files) == 1:
                res['guide_lib'] = files[0].get('title') or res.get('guide_lib') or ''
            return res
        if name == 'query_ship_info' and not ((args or {}).get('field') or '').strip():
            # 完整角色查询：走「查角色」面板（用户口径 2026-09-13：不在对话里输出底层面板文本）
            panel = _ship_panel_reply((args or {}).get('ship_name') or '', tools)
            if panel is not None:
                # Agent 数据分支此前绕开了 legacy，导致“光辉是啥”打开面板后没有保存
                # 当前角色，下一句“评价下她”无法补全主语。
                focus = str(panel.get('ship_focus') or '').strip()
                if focus and overrides is not None:
                    overrides['last_role'] = focus
                return panel
        text = invoke_tool(t, args)
        print('[route.execute] result_prefix=%r' % str(text)[:120], flush=True)
        if not _usable(text):
            return None
        texts.append(text)
    if not texts:
        return None
    return _work_dict('\n\n'.join(texts), texts)


def route_work_agent(tools, overrides, msg, agent=None, history=None):
    """返回固定回复 dict，或 None（交回 web_app 闲聊链路）。异常由 route_dispatch 兜底回落。"""
    handled, res = guard(tools, overrides, msg, agent=agent, history=history)
    if handled:
        return res

    # 悬浮球「计算工具」按钮直通（用户口径 2026-09-23）：
    # 前端该按钮发的就是这个固定短语，语义唯一、不需要模型理解。实测路由模型会偶发
    # 把它判成 intent=casual + no_tool（[route] input='计算工具' intent=casual tool=no_tool），
    # 于是落到闲聊自由生成链路 —— 回一句「系统异常。」且不弹子菜单（用户点按钮必现/偶现）。
    # 固定指令一律不走 LLM 判意图，直接交 legacy 确定性分支（calc_flow._handle_calc_menu）。
    try:
        if re.search(r'计算工具|计算菜单', msg or ''):
            return _legacy_route_work(tools, overrides, msg, agent=agent,
                                      allow_intent_llm=False, update_recent=False)
    except Exception:
        pass

    # 普通对话每轮都重新判定意图。历史只用来理解指代、省略、纠正与话题切换，
    # 不保存“上一轮路由”。只有 guard 中的明确工具状态机会跳过本层。
    recent_prev = list((overrides or {}).get('_recent') or [])
    if history is None and msg and overrides is not None:
        overrides['_recent'] = (recent_prev + [msg])[-6:]
    route_history = history
    if route_history is None:
        route_history = [{'role': 'user', 'content': text} for text in recent_prev]

    # 确定性补全明确的角色代词，再让路由器分析完整意图。只替换“她/该舰/这艘”等
    # 真正回指词，不继承上一轮路由，因而新话题仍会逐轮重新判定。
    context_msg = _resolve_pronoun(msg, overrides)

    sel = select_tools(tools)
    if not sel:
        return _legacy_route_work(tools, overrides, context_msg, agent=agent, allow_intent_llm=False,
                                  update_recent=False)

    # 【当期活动攻略】统一话术（用户口径 2026-09-23）：当期活动名/指代 + 攻略类问法
    # 不进 LLM、不选工具，与 legacy 的 tips 闸前拦截同口径；表单提交消息不拦。
    if not re.search(r'(?:前排|后排|防守|白天)\s*[：:]', context_msg):
        _agr = _activity_guide_reply(context_msg)
        if _agr:
            return _work_dict(_agr)

    try:
        decision = _select_route(context_msg, sel, history=route_history)
    except Exception:
        # 路由模型限流/超时时，人工维护的标准问题仍可安全回到攻略检索；
        # 这是首次意图分析失败后的确定性降级，不再启动第二个分类模型。
        try:
            from tools.knowledge import match_guide_question
            if match_guide_question(context_msg):
                r = _handle_guide_qa(tools, context_msg, search_query=context_msg)
                if r is not None:
                    return r
        except Exception:
            pass
        return _legacy_route_work(tools, overrides, context_msg, agent=agent, allow_intent_llm=False,
                                  update_recent=False)

    name = decision.get('name') or 'no_tool'
    intent = decision.get('intent') or ''
    print('[route] input=%r intent=%s tool=%s args=%s'
          % (context_msg[:160], intent, name,
             json.dumps(decision.get('args') or {}, ensure_ascii=False)), flush=True)
    calls = [] if name == 'no_tool' else [decision]
    if calls:
        # ②检索分支：向量检索 → LLM 依据片段简答（约束式生成）；无命中则回落
        if is_retrieval(name):
            args = decision.get('args') or {}
            r = _handle_guide_qa(tools, context_msg, search_query=(args.get('query') or ''))
            if r is not None:
                return r
            return _legacy_route_work(tools, overrides, context_msg, agent=agent, allow_intent_llm=False,
                                      update_recent=False)
        # ①数据分支：逐字执行、原文直出
        res = _execute(calls, tools, overrides=overrides)
        if res is not None:
            return res

    # 闲聊是模型已做出的明确结论，直接交回 web_app 的会话层。
    # 但先过一遍小技巧确定性层（纯离线、零 LLM 成本）：模型把「咋给舰灵搓澡」
    # 这类游戏内操作问题误判成闲聊时，知识库里若已有条目就原文直出，
    # 不再让会话层自由生成（曾编出「维护协议」「《港区日常养护手册》第7章」）。
    if intent == 'casual':
        tip = _handle_tips(context_msg)
        if tip:
            return tip
        return None

    # backend_flow / 工具未命中：仅运行确定性兼容处理器。
    return _legacy_route_work(tools, overrides, context_msg, agent=agent, allow_intent_llm=False,
                              update_recent=False)
