# -*- coding: utf-8 -*-
"""
文本预处理模块

在送入 LLM 前对原始文本做轻量清洗 + 别名提示注入，降低 LLM 误识别率。
不做语义解析（语义解析交给 LLM），只做标注 + 清洗 + 别名提示。
"""
import re




def normalize_text(content: str) -> str:
    """清洗文本：去 BOM、全角空格统一、控制字符移除、连续空白折叠"""
    if not content:
        return ""
    # 去 BOM
    text = content.replace('\ufeff', '')
    # 全角空格转半角
    text = text.replace('\u3000', ' ')
    # 移除控制字符（保留换行符和制表符）
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # 连续空格折叠为单个（保留换行）
    text = re.sub(r'[ ]{2,}', ' ', text)
    # 连续空行折叠为最多两个换行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def preprocess(content: str) -> tuple:
    """
    文本预处理入口：清洗文本 + 生成别名提示块。

    返回 (cleaned_content, hint_block)。
    hint_block 由 parser 拼到 user 消息末尾，帮助 LLM 对齐字段名。
    """
    cleaned = normalize_text(content)
    hint = build_alias_hint(cleaned)
    return cleaned, hint
