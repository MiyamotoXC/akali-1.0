# -*- coding: utf-8 -*-
"""
请求级上下文：客户端随请求携带的用户面板覆盖
================================================================

背景
----
改造前「用户面板」的真相源是服务器上的 `用户端特用/user_data.json`（用户副本），
多用户云端部署下该单文件无法按用户隔离。

云端模式（AKALI_RUN_MODE=cloud）改为：**面板存在客户端本地**，每次请求把面板随
`ChatRequest.panel_overrides` 一起发给服务器，服务器只在本次请求内使用、绝不落盘。

结构（与 user_data.apply_panel_to 的 atk_set/def_set 同构，绝对赋值）::

    {
      "胡德·改": {"attack_panel": {"炮击": 1234.0, ...},
                   "defense_panel": {...}},
      ...
    }

用法（在 /api/chat 入口）::

    from core.req_ctx import set_panel_overrides, reset_panel_overrides
    _tok = set_panel_overrides(req.panel_overrides)
    try:
        ...  # 计算路径 App.get_ship_for_calc -> get_panel_override(name)
    finally:
        reset_panel_overrides(_tok)

并发安全：set 返回 token，**由调用方持有并在 finally 用同一 token reset**；
不要用模块级全局变量保存 token（并发请求会互相覆盖）。
"""
from __future__ import annotations

import contextvars
from typing import Optional

__all__ = [
    "set_panel_overrides", "reset_panel_overrides",
    "get_panel_overrides", "get_panel_override", "current_token",
    "set_quota_key", "reset_quota_key", "get_quota_key",
]

_panel_overrides: contextvars.ContextVar = contextvars.ContextVar(
    'akali_panel_overrides', default=None)

# 本次请求的配额键（uid 优先）：LLM 记账（core/llm_accounting）在 SDK 层拿不到
# Request 对象，靠这里把「这次 API 调用该记到谁头上」传给深层调用点。
_quota_key: contextvars.ContextVar = contextvars.ContextVar(
    'akali_quota_key', default=None)


def set_quota_key(key) -> contextvars.Token:
    """设置本次请求的配额键（uid 优先），返回 token（调用方在 finally 里 reset）。"""
    return _quota_key.set(str(key or '').strip() or None)


def reset_quota_key(token: Optional[contextvars.Token]) -> None:
    """还原上下文（token 为 None 时忽略）。"""
    if token is None:
        return
    try:
        _quota_key.reset(token)
    except (ValueError, LookupError):
        pass


def get_quota_key():
    """当前请求的配额键；无请求上下文（离线脚本）返回 None（不记账）。"""
    return _quota_key.get()


def set_panel_overrides(mapping) -> contextvars.Token:
    """设置本次请求的面板覆盖，返回 token（交由调用方在 finally 里 reset）。"""
    val = mapping if isinstance(mapping, dict) and mapping else None
    return _panel_overrides.set(val)


def reset_panel_overrides(token: Optional[contextvars.Token]) -> None:
    """还原上下文（token 为 None 时忽略）。"""
    if token is None:
        return
    try:
        _panel_overrides.reset(token)
    except (ValueError, LookupError):
        # token 已在其它上下文使用过 / 不匹配 —— 忽略，不影响主流程
        pass


def get_panel_overrides() -> dict:
    """当前请求的面板覆盖全量（可能为空 dict）。"""
    m = _panel_overrides.get()
    return m if isinstance(m, dict) else {}


def get_panel_override(name: str) -> Optional[dict]:
    """取指定角色的面板覆盖；无则 None。name 建议用「已解析的标准名」。"""
    if not name:
        return None
    m = _panel_overrides.get()
    if not isinstance(m, dict):
        return None
    e = m.get(name)
    return e if isinstance(e, dict) else None


def current_token():
    """仅供诊断/测试：查看当前是否处于有覆盖的上下文。"""
    return _panel_overrides.get()
