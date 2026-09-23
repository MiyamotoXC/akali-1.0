# -*- coding: utf-8 -*-
"""
LLM 客户端模块（OpenAI 兼容接口）

数据管理工具是单次结构化提取任务，不需要 qwen_agent Assistant 的对话循环，
故直接用 openai SDK 走 OpenAI 兼容接口拿 JSON 输出。

配置来源（全部本地 .env 读取）：
- LLM_PROVIDER：服务商（agicto / dashscope），决定接口地址与读取哪个 API Key 变量
- AGICTO_API_KEY / DASHSCOPE_API_KEY：API Key（按所选服务商读取其一）
- LLM_MODEL：模型名
- EMBEDDING_MODEL：向量模型（攻略问答库向量化用）
- LLM_ENABLE_THINKING：思考模式开关（默认 0 关闭）
"""
import os

import json5
from dotenv import load_dotenv
from openai import OpenAI

# 加载项目根目录 .env（显式定位，避免 CWD 依赖）
# override=False：系统环境变量优先，.env 只补缺
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
load_dotenv(os.path.join(_PROJECT_ROOT, '.env'), override=False)

# 服务商注册表：LLM_PROVIDER → (OpenAI 兼容 base_url, API Key 环境变量名)
_PROVIDERS = {
    'agicto': ('https://api.agicto.cn/v1', 'AGICTO_API_KEY'),
    'dashscope': ('https://dashscope.aliyuncs.com/compatible-mode/v1', 'DASHSCOPE_API_KEY'),
}

# 从 .env 读取服务商（默认 agicto）
provider = os.environ.get('LLM_PROVIDER', 'agicto').strip().lower()
if provider not in _PROVIDERS:
    raise ValueError(f"LLM_PROVIDER 无效：{provider}（可选：{'/'.join(_PROVIDERS)}）")

# 当前服务商的接口地址与 API Key 环境变量名
MODEL_SERVER, API_KEY_VAR = _PROVIDERS[provider]

# 从环境变量读取 API Key（变量名随服务商）
api_key = os.getenv(API_KEY_VAR)

# 从 .env 读取模型名
model = os.environ.get('LLM_MODEL', 'qwen-plus')

# 思考模式开关（.env LLM_ENABLE_THINKING，默认 0 关闭）：
# qwen3 系列混合思考模型非流式调用必须显式关闭思考模式，否则上游返回 400
_ENABLE_THINKING = os.environ.get('LLM_ENABLE_THINKING', '0').strip().lower() in ('1', 'true', 'on', 'yes')

_client = None


def _get_client() -> OpenAI:
    """惰性构造 OpenAI 客户端（单例）"""
    global _client
    if _client is None:
        _client = OpenAI(api_key=api_key, base_url=MODEL_SERVER, timeout=30)
    return _client


def _strip_code_fence(text: str) -> str:
    """去除 LLM 输出可能包裹的 markdown 代码块（```json ... ```）。
    消除 json.loads 失败场景，而非加兜底重试。"""
    text = text.strip()
    if not text.startswith('```'):
        return text
    lines = text.split('\n')
    # 去掉首行 ```json 或 ```
    if lines and lines[0].startswith('```'):
        lines = lines[1:]
    # 去掉末尾 ```
    if lines and lines[-1].strip() == '```':
        lines = lines[:-1]
    return '\n'.join(lines).strip()


def chat_json(system_prompt: str, user_content: str, temperature: float = 0.2) -> dict:
    """调用 LLM 拿 JSON 输出。返回解析后的 dict。

    要求 LLM 直接输出纯 JSON（system prompt 已约束），再用 json5.loads 解析。
    json5 容忍尾逗号、单引号等小瑕疵；_strip_code_fence 去除 markdown 包裹。
    不使用 response_format=json_object，因为兼容模式下未必支持。"""
    client = _get_client()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ],
        temperature=temperature,
        # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        extra_body={'enable_thinking': _ENABLE_THINKING},
    )
    text = resp.choices[0].message.content or ''
    text = _strip_code_fence(text)
    return json5.loads(text)
