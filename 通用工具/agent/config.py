# -*- coding: utf-8 -*-
"""agent 层配置：复用 llm/llm_client 的 qwen_agent LLM 配置，构造底层 chat 模型。

用 qwen_agent 的底层 chat 模型（与 llm_client 同一套 OpenAI 兼容端点）做单次
function calling 分类；不再依赖 LangChain。关键点：extra_body={'enable_thinking': ...}
必须保留——qwen3 系列混合思考模型非流式调用若不显式关闭思考模式，上游会返回 400；
该参数由 llm_client.get_llm_config() 的 generate_cfg.extra_body 统一携带。
"""
import os

from llm.llm_client import get_llm_config


def route_mode() -> str:
    """返回当前路由模式：默认 agent；显式 legacy 时退回旧路由。"""
    return os.environ.get('AKALI_ROUTE_MODE', 'agent').strip().lower()


def get_chat_model(temperature: float = 0.0):
    """构造 qwen_agent 底层 chat 模型（复用 .env 的服务商 / 模型 / 思考开关）。"""
    from qwen_agent.llm import get_chat_model as _qwen_get_chat_model
    return _qwen_get_chat_model(get_llm_config(temperature=temperature))
