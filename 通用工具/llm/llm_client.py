# -*- coding: utf-8 -*-
"""
LLM 客户端配置模块（qwen_agent / OpenAI 兼容接口）

配置来源（全部本地 .env 读取）：
- LLM_PROVIDER：服务商（agicto / dashscope），决定接口地址与读取哪个 API Key 变量
- AGICTO_API_KEY / DASHSCOPE_API_KEY：API Key（按所选服务商读取其一）
- LLM_MODEL：模型名（默认 qwen-plus）
- LLM_ENABLE_THINKING：思考模式开关（默认 0 关闭）

qwen_agent 的 Assistant 接受 LLM 配置字典，走 OpenAI 兼容接口，支持 function calling。
"""
import os

from dotenv import load_dotenv

# 加载项目根目录的 .env 文件（通用工具/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENV_PATH = os.path.join(_PROJECT_ROOT, '.env')
# override=False：系统环境变量优先，.env 只补缺
load_dotenv(_ENV_PATH, override=False)

# 服务商注册表：LLM_PROVIDER → (OpenAI 兼容 base_url, API Key 环境变量名)
_PROVIDERS = {
    'agicto': ('https://api.agicto.cn/v1', 'AGICTO_API_KEY'),
    'dashscope': ('https://dashscope.aliyuncs.com/compatible-mode/v1', 'DASHSCOPE_API_KEY'),
}

# 从 .env 读取服务商（默认 agicto）
provider = os.environ.get('LLM_PROVIDER', 'agicto').strip().lower()
if provider not in _PROVIDERS:
    raise ValueError(f"LLM_PROVIDER 无效：{provider}（可选：{'/'.join(_PROVIDERS)}）")

# 当前服务商的接口地址与 API Key 环境变量名（其他模块引用这两个名字）
MODEL_SERVER, API_KEY_VAR = _PROVIDERS[provider]

# 从环境变量读取 API Key（变量名随服务商）
api_key = os.getenv(API_KEY_VAR)

# 从 .env 读取模型名
model = os.environ.get('LLM_MODEL', 'qwen-plus')

# 思考模式开关（.env LLM_ENABLE_THINKING，默认 0 关闭）：
# qwen3 系列混合思考模型非流式调用必须显式关闭思考模式，否则上游返回 400
_ENABLE_THINKING = os.environ.get('LLM_ENABLE_THINKING', '0').strip().lower() in ('1', 'true', 'on', 'yes')


def get_llm_config(temperature: float = 0.2) -> dict:
    """返回 qwen_agent LLM 配置字典。

    qwen_agent 检测到 model_server 为 http 地址时，自动使用 oai（TextChatAtOAI）类型，
    走 OpenAI 兼容接口，支持 function calling。"""
    return {
        'model': model,
        'model_server': MODEL_SERVER,
        'api_key': api_key,
        # qwen_agent 会把 generate_cfg 原样展开为 chat.completions.create 的 kwargs，
        # extra_body 是 OpenAI SDK 原生参数，最终以 enable_thinking 合入请求体
        'generate_cfg': {'temperature': temperature, 'extra_body': {'enable_thinking': _ENABLE_THINKING}},
    }
