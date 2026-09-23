# -*- coding: utf-8 -*-
"""qwen_agent BaseTool → OpenAI function calling 适配层。

原则：
- 工具的 name / description / parameters(JSON-Schema) 已是 OpenAI 兼容格式，**原样透传**，
  不重建 pydantic schema，避免嵌套 object/array/default 上的边界漂移。
- 工具输出（str，含 <<AVATAR>>/<<STORY>>/<<GUIDE_LIB>> 等 marker）**逐字透传**，
  绝不改写、截断或二次加工——这是"工具回复原文直出"铁律的落点。
- 执行异常向上抛出，由调用方（dispatcher / route_dispatch）回落 legacy 正则路由。
"""
import copy

_EMPTY_PARAMS = {'type': 'object', 'properties': {}}


def to_openai_tool(t) -> dict:
    """qwen BaseTool → OpenAI 原生 function 描述（LangChain bind_tools 直接接受此格式）。"""
    return {
        'type': 'function',
        'function': {
            'name': getattr(t, 'name', '') or '',
            'description': getattr(t, 'description', '') or '',
            'parameters': copy.deepcopy(getattr(t, 'parameters', None) or _EMPTY_PARAMS),
        },
    }


def tool_map(tools) -> dict:
    """name → 工具实例；重名只保留首个（如 knowledge 内有两个 SkillQuery 同名）。"""
    out = {}
    for t in tools or []:
        name = getattr(t, 'name', '') or ''
        if name and name not in out:
            out[name] = t
    return out


def invoke_tool(t, args) -> str:
    """确定性执行工具，返回逐字字符串。工具内部会自行 _verify_json_format_args 校验参数。"""
    out = t.call(dict(args or {}))
    return out if isinstance(out, str) else str(out)
