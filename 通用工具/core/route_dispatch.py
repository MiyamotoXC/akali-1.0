# -*- coding: utf-8 -*-
"""用户输入的五层处理入口。

1. web_app 的直接操作（按钮/结束流程）；
2. agent.guard 的会话状态与固定守卫；
3. agent.dispatcher 的 LLM 元路由（允许多次意图分析）；
4. 数据/检索/文档/实时/后端处理器；
5. legacy 确定性兼容规则与 web_app 闲聊。

用户口径 2026-09-28：**允许多次 LLM 意图分析**（含 agent 模式的回落）——仅"进入明确工具流程
（calc_state / favor_pending）"豁免，该豁免由 calc_flow.route_work 内部保证。
显式设置 AKALI_ROUTE_MODE=legacy 仅用于排障。
"""
import os
import traceback


def _pregate_result(tools, overrides, msg):
    """业务/主题前置闸（性能优化 / 就业 / 天赋 / 争锋）逐个探。返回 (rule_id, result) 或 None。

    抽成函数是为了让「前置闸是**门**还是**网**」变成运行期策略（见 route_work_dispatch）：
      gate （默认）→ 前置**先短路**（与改造前逐字一致）
      prior        → 让元路由先判；模型判成"闲聊/无工具"时才回落到这里兜底
    这样"这条前置能不能退场"就不再是改代码的问题，而是一个可 A/B 的开关。
    """
    if (overrides or {}).get('calc_state'):
        return None
    from core import calc_flow as _cf
    for rule_id, fn_name in (('pregate.perf_toggle', '_handle_perf_toggle'),
                             ('pregate.ship_use', '_handle_ship_use'),
                             ('pregate.talent_ask', '_handle_talent_ask'),
                             ('pregate.pvp_lineup', '_handle_pvp_lineup')):
        try:
            # 争锋阵容反问式需要跨轮记忆（pvp_lineup_pending），故把 overrides 传给它；
            # 天赋点法追问需要上下文继承上一轮查过的角色（overrides['last_role']），同样传 overrides。
            if fn_name in ('_handle_pvp_lineup', '_handle_talent_ask'):
                r = getattr(_cf, fn_name)(tools, msg, overrides)
            else:
                r = getattr(_cf, fn_name)(tools, msg)
        except Exception:
            continue
        if r is not None:
            return rule_id, r
    return None


def route_work_dispatch(tools, overrides, msg, agent=None, history=None):
    # 闸门命中计数（用户口径 2026-10-05）：三条业务前置谁在真接管、agent/legacy 各占多少，
    # 都记进 agent.route_registry —— 用来回答"这条前置能不能退场"（长期 0 命中=死规则）。
    def _hit(rule_id):
        try:
            from agent import route_registry as _rr
            _rr.hit(rule_id)
            _rr.route_src(rule_id)
        except Exception:
            pass

    # —— 策略开关：前置闸是「门」还是「网」——（用户口径 2026-10-05）
    # 判据只增不减的头号根因：前置闸**先于**意图分析短路，模型没机会判断；于是每次事故
    # 就往最前面再加一条。现在两条通道并存：
    #   AKALI_PREGATE_MODE=gate （默认）→ 前置先短路，与改造前**逐字一致**（回归基线可证）
    #   AKALI_PREGATE_MODE=prior        → 元路由先判；模型判成闲聊/无工具时，前置闸当**网**兜底
    # 用 prior 跑一段时间，看 /api/admin/routes 里的命中与分歧，就能决定哪条前置能真正退场。
    _pg_mode = (os.environ.get('AKALI_PREGATE_MODE', 'gate') or 'gate').strip().lower()

    # 舰船「用途 / 就业」问答（用户口径 2026-10-03）：放在**五层分发最前** ——
    # agent 模式下 dispatcher 的 LLM 路由会先接走「X 有什么用」，答成泛泛的攻略片段；
    # 这里确定性拦下（不调 LLM），agent / legacy 两种模式都覆盖。
    # 角色「天赋怎么点」（用户口径 2026-10-04）：同上 —— 「格奈森瑙天赋怎么点」这类点名角色的
    # 问题必须读底层数据（推荐点法 + 天赋原文），否则会被 LLM 路由拿通用规则顶。
    # 争锋竞技场「队伍/阵容推荐」（用户口径 2026-10-03）：同上 —— RAG 只捞到「金大船」那几块
    # （正文写的是"金中船/紫中船"，体量词对不上），这里按场质/体量定向取原文块直出。
    if _pg_mode != 'prior':
        _pg = _pregate_result(tools, overrides, msg)
        if _pg is not None:
            _hit(_pg[0])
            return _pg[1]

    mode = os.environ.get('AKALI_ROUTE_MODE', 'agent').strip().lower()
    if mode == 'agent':
        try:
            from agent.dispatcher import route_work_agent
            from agent.guard import track_unanswered_result
            result = route_work_agent(tools, overrides, msg, agent=agent, history=history)
            _hit('route.agent')
            if result is not None:
                return track_unanswered_result(overrides, msg, result)
            if _pg_mode == 'prior':
                # 模型判成「闲聊 / 无工具」→ 前置闸作为**网**兜底（仅 prior 模式）
                _pg = _pregate_result(tools, overrides, msg)
                if _pg is not None:
                    _hit(_pg[0])
                    return _pg[1]
            return track_unanswered_result(overrides, msg, result)
        except Exception as exc:
            # 仍允许 legacy 保底，但必须留下真实原因；此前静默吞错会让测试只看到
            # "问答服务无法返回/旧路由结果"，无法判断 Agent 层是否根本没加载。
            print('[route_dispatch] agent 路由失败，回落 legacy：%s: %s'
                  % (type(exc).__name__, exc), flush=True)
            traceback.print_exc()
    from core.calc_flow import route_work
    result = route_work(tools, overrides, msg, agent=agent,
                        allow_intent_llm=True, update_recent=(mode != 'agent'))
    _hit('route.legacy')
    try:
        from agent.guard import track_unanswered_result
        return track_unanswered_result(overrides, msg, result)
    except Exception:
        return result
