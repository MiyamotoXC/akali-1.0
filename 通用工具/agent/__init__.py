# -*- coding: utf-8 -*-
"""agent 包：用 LangChain function calling 替代正则做「选工具 + 抽参数」。

- 只负责"调用"（选工具、填参数）；不负责生成回复。
- 工具确定性执行、输出逐字透传（含 <<AVATAR>> 等 marker）。
- 生成权限只留给闲聊链路（web_app._chat_impl 现有 LLM 调用）。
- 由 core/route_dispatch.py 按 AKALI_ROUTE_MODE 开关启用；默认 agent，legacy 仅作回退。
"""
