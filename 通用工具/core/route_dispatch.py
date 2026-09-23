# -*- coding: utf-8 -*-
"""用户输入的五层处理入口。

1. web_app 的直接操作（按钮/结束流程）；
2. agent.guard 的会话状态与固定守卫；
3. agent.dispatcher 唯一一次 LLM 意图分析；
4. 数据/检索/文档/实时/后端处理器；
5. legacy 确定性兼容规则与 web_app 闲聊。

agent 模式下的任何回落都传 allow_intent_llm=False，避免第二次分类。
显式设置 AKALI_ROUTE_MODE=legacy 仅用于排障。
"""
import os
import traceback


def route_work_dispatch(tools, overrides, msg, agent=None, history=None):
    mode = os.environ.get('AKALI_ROUTE_MODE', 'agent').strip().lower()
    if mode == 'agent':
        try:
            from agent.dispatcher import route_work_agent
            from agent.guard import track_unanswered_result
            result = route_work_agent(tools, overrides, msg, agent=agent, history=history)
            return track_unanswered_result(overrides, msg, result)
        except Exception as exc:
            # 仍允许 legacy 保底，但必须留下真实原因；此前静默吞错会让测试只看到
            # “问答服务无法返回/旧路由结果”，无法判断 Agent 层是否根本没加载。
            print('[route_dispatch] agent 路由失败，回落 legacy：%s: %s'
                  % (type(exc).__name__, exc), flush=True)
            traceback.print_exc()
    from core.calc_flow import route_work
    result = route_work(tools, overrides, msg, agent=agent,
                        allow_intent_llm=(mode != 'agent'), update_recent=(mode != 'agent'))
    try:
        from agent.guard import track_unanswered_result
        return track_unanswered_result(overrides, msg, result)
    except Exception:
        return result
