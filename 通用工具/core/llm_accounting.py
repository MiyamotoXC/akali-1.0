# -*- coding: utf-8 -*-
"""
LLM API token 统一记账（用户口径 2026-09-26：所有消耗 LLM API 的 token 都要计算）
================================================================

问题：在线 LLM 调用点分散在 web_app（闲聊主链/表情差分/越界判定）、calc_flow
（意图分类/攻略生成/证据校对）、knowledge（重排/改写/自检/条件解析）、
boss_guide（遭遇战建议）以及 qwen_agent 内部的工具循环 —— 逐点埋针必然漏记，
此前只有闲聊主链按「字数×0.70 估算」记了一笔，其余全部漏记。

方案：项目所有在线调用最终都走 openai SDK 的
`ChatCompletions.create` / `Embeddings.create`。在这里对 SDK 方法统一挂钩一次，
从响应里读**真实 usage**（prompt_tokens / completion_tokens，非估算），按
req_ctx 里的请求配额键（uid 优先）记入 core.quota —— 一处挂钩覆盖全部调用点，
包括 qwen_agent 内部工具循环的每一轮。

- 记账对象：`req_ctx.get_quota_key()`；无请求上下文（构建脚本等离线进程）不记账。
- 类型细则：按调用方函数名映射 kind（闲聊/攻略生成/检索重排…），日汇总
  entry['calls'][kind] 按 次数/输入/输出 累计，供控制台「点开才显示细则」。
- 只在应用启动时 install() 一次（幂等）；兼容 openai 1.x（ChatCompletions）
  与 2.x（Completions）两种类名布局。
"""
from __future__ import annotations

import sys
import threading

__all__ = ["install"]

_installed = False
_install_lock = threading.Lock()

# 调用方函数名 → 细则类型名。未命中回退「LLM·函数名」；qwen_agent 内部调用 → 闲聊。
_KIND_BY_FUNC = {
    'chat': '闲聊',
    '_classify_guide_topic': '攻略主题分类',
    '_classify_fuzzy_intent': '意图分类',
    '_llm_analyze_intent': '意图分析',
    '_llm_is_calc_request': '计算意图判定',
    '_llm_intent_classify': '意图分类',
    '_llm_guide_intent': '攻略意图分析',
    '_verify_guide_answer': '攻略证据校对',
    '_handle_guide_qa': '攻略生成',
    '_semantic_rerank': '检索重排',
    '_expand_queries_llm': '查询改写',
    '_selfcheck_llm': '答案自检',
    'llm_parse_conditions': '条件解析',
    '_llm_nearest_expression': '表情差分选择',
    '_analyze_chat_context': '越界判定',
    'build_advice': '遭遇战建议',
}


def _kind_of_caller() -> str:
    """从调用栈取真实调用方（0=本函数 1=SDK 包装层 2=业务调用方），映射为细则类型。"""
    try:
        frame = sys._getframe(2)
        co_name = frame.f_code.co_name
        module = str(frame.f_globals.get('__name__') or '')
    except Exception:
        return 'LLM'
    if module.startswith('qwen_agent'):
        return '闲聊'      # qwen_agent 主链（含工具循环每一轮）
    return _KIND_BY_FUNC.get(co_name) or ('LLM·%s' % co_name)


def _record(kind: str, tok_in: int, tok_out: int):
    """记一笔到当前请求的配额键；无请求上下文（离线脚本）不记账。"""
    try:
        from core import req_ctx
        key = req_ctx.get_quota_key()
        if not key:
            return
        from core import quota
        quota.record_llm_call(key, kind, tok_in, tok_out)
    except Exception:
        pass


def _usage_tokens(completion, out_attr: str) -> tuple:
    """从 OpenAI 兼容响应对象提取 (tok_in, tok_out)；读不到按 0 记。"""
    usage = getattr(completion, 'usage', None)
    if usage is None:
        return 0, 0
    tin = int(getattr(usage, 'prompt_tokens', 0) or 0)
    tout = int(getattr(usage, out_attr, 0) or 0)
    return tin, tout


class _UsageStream:
    """流式响应包装：透传每个 chunk，记录末块携带的 usage（stream_options.include_usage），
    迭代结束或 close 时按真实 usage 记账。qwen_agent 内部固定 stream=True，须走此路。"""
    __slots__ = ('_it', '_kind', '_in', '_out', '_done')

    def __init__(self, stream, kind):
        self._it = iter(stream)
        self._kind = kind
        self._in = 0
        self._out = 0
        self._done = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._it)
        except StopIteration:
            self._finish()
            raise
        u = getattr(chunk, 'usage', None)
        if u is not None:
            self._in = int(getattr(u, 'prompt_tokens', 0) or 0)
            self._out = int(getattr(u, 'completion_tokens', 0) or 0)
        return chunk

    def close(self):
        try:
            self._it.close()
        except Exception:
            pass
        self._finish()

    def _finish(self):
        if not self._done:
            self._done = True
            if self._in or self._out:
                _record(self._kind, self._in, self._out)

    def __getattr__(self, name):
        # 其余属性（response/id 等）透传底层流
        return getattr(self._it, name)


def install():
    """包装 openai SDK 的 ChatCompletions.create / Embeddings.create，
    每次调用后自动按真实 usage 记账。幂等；应用启动时调用一次。"""
    global _installed
    with _install_lock:
        if _installed:
            return
        import openai.resources.chat.completions as _cc_mod
        import openai.resources.embeddings as _emb_mod

        # openai 2.x 类名 Completions；1.x 为 ChatCompletions
        chat_cls = getattr(_cc_mod, 'Completions', None) or getattr(_cc_mod, 'ChatCompletions', None)
        emb_cls = getattr(_emb_mod, 'Embeddings', None)
        if chat_cls is None or emb_cls is None:
            raise RuntimeError('openai SDK 类结构不识别，无法挂钩记账')

        orig_create = chat_cls.create

        def _create(self, *args, **kwargs):
            kind = _kind_of_caller()
            is_stream = bool(kwargs.get('stream'))
            if is_stream and not kwargs.get('stream_options'):
                # qwen_agent 固定流式调用；注入 include_usage 让末块携带真实 usage
                kwargs['stream_options'] = {'include_usage': True}
            resp = orig_create(self, *args, **kwargs)
            try:
                if is_stream:
                    resp = _UsageStream(resp, kind)
                else:
                    tin, tout = _usage_tokens(resp, 'completion_tokens')
                    if tin or tout:
                        _record(kind, tin, tout)
            except Exception:
                pass
            return resp

        orig_embed = emb_cls.create

        def _embed(self, *args, **kwargs):
            resp = orig_embed(self, *args, **kwargs)
            try:
                tin, _ = _usage_tokens(resp, 'total_tokens')
                if tin:
                    _record('查询向量化', tin, 0)
            except Exception:
                pass
            return resp

        chat_cls.create = _create
        emb_cls.create = _embed
        _installed = True
