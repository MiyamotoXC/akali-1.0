# -*- coding: utf-8 -*-
"""用户反馈上报前的轻量脱敏（`/api/feedback` 调用）。

为什么需要
----------
用户反馈会原样写入服务器上的文件、并可能被开发者翻阅，其中的密钥形态串
（`sk-xxx`、Bearer token、API Key）不应落盘（用户口径 2026-09-24：
不得泄露 API 等敏感信息）。

设计取舍
--------
**不做内容审查**。反馈的唯一用途是让开发者看到「哪句话答得不好」，答得越完整
越有价值；如果把角色名/游戏名词也模糊掉，反馈就失去排障意义了。因此这里只做：
  ① 遮蔽密钥形态串（`sk-…` / `AIza…` / `Bearer …` / `api_key=…`）；
  ② 抹掉明显的个人信息（邮箱、11 位手机号）。
其余原样保留。

注意
----
本模块曾被 `web_app.py:2555` 引用但**文件缺失**，导致 `/api/feedback` 一直抛
`ModuleNotFoundError` → 接口恒返回 500（用户 2026-09-25 反馈"反馈按钮好像没了"，
实为按钮隐藏 + 接口不可用两个问题叠加）。此处补回实现。
"""
import re as _re

# 依次作用于文本的脱敏规则（顺序：先长密钥，再个信）
_PATTERNS = (
    # OpenAI 风格密钥：sk-AbCdEfGh1234...
    _re.compile(r'\bsk-[A-Za-z0-9_\-]{4}[A-Za-z0-9_\-]{6,}'),
    # Google 风格密钥：AIza...
    _re.compile(r'\bAIza[A-Za-z0-9_\-]{10,}'),
    # 通用「带标签的密钥」：api_key=xxx / token: xxx / Authorization: Bearer xxx
    _re.compile(r'(?i)\b(?:api[_\-]?key|access[_\-]?token|auth[_\-]?token|token|secret|'
                r'authorization|bearer)\b\s*[:=]?\s*(?:bearer\s+)?[A-Za-z0-9_\-\.]{8,}'),
    # 邮箱
    _re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'),
    # 中国大陆手机号（11 位，1 开头）
    _re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),
)


def filter_sensitive_words(text) -> str:
    """返回脱敏后的文本；非字符串入参先转成字符串，异常一律回退原文。

    绝不因脱敏本身抛错而让反馈提交失败 —— 反馈链路必须可靠。
    """
    try:
        s = text if isinstance(text, str) else str(text or '')
        for pat in _PATTERNS:
            s = pat.sub('***', s)
        return s
    except Exception:
        return str(text or '')
