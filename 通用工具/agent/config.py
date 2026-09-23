# -*- coding: utf-8 -*-
"""agent 层配置：复用 llm/llm_client 的 OpenAI 兼容端点，构造 LangChain ChatOpenAI。

关键点：extra_body={'enable_thinking': ...} 必须保留——qwen3 系列混合思考模型
非流式调用若不显式关闭思考模式，上游（DashScope）会返回 400。
"""
import os

from llm.llm_client import MODEL_SERVER, api_key, model, _ENABLE_THINKING


def route_mode() -> str:
    """返回当前路由模式：默认 agent；显式 legacy 时退回旧路由。"""
    return os.environ.get('AKALI_ROUTE_MODE', 'agent').strip().lower()


def get_chat_model(temperature: float = 0.0):
    """构造 LangChain ChatOpenAI（复用 .env 的服务商 / 模型 / 思考开关）。"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        api_key=api_key or 'EMPTY',
        base_url=MODEL_SERVER,
        temperature=temperature,
        extra_body={'enable_thinking': _ENABLE_THINKING},
    )
