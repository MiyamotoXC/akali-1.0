# -*- coding: utf-8 -*-
"""攻略 QA 问法增强：为每个 chunk 生成「玩家口语问法」+「Query 改写问法」两类变体。

背景（2026-09-07 诊断）：库内 question 是书面问句，与玩家真实口语问法存在语义鸿沟
——用库内 question 自测 hit@1 98~100%，真实口语集仅 81%。本脚本为每个 chunk 补两类
变体，均由 问答向量化.py 作为**独立向量行**嵌入（指向同一 chunk id），检索时按
chunk 归并取最高分：
  · alt_questions     —— 3 条口语化短问（玩家群里原话风格）；
  · rewrite_questions —— 3 条 Query 改写风格问法（对比/反问/多意图/长口语，参考
                        《RAG 技术与应用》的 Query 改写方法论），扩充向量库对复合/
                        对比/反问类问法的召回形态。

增量：两类变体各自独立判断（已有则跳过）；--full 强制全部重生成。

用法（在 开发者入口 目录下）：
  python build/问答变体生成.py            # 增量全量
  python build/问答变体生成.py --limit 6  # 先小批验证
  python build/问答变体生成.py --full
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))

from src.模型客户端 import chat_json  # noqa: E402

_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
QA_PATH = os.path.join(KB_DIR, "llm", "攻略QA库.json")

_VARIANT_SYSTEM = (
    '你是手游《拂晓胜利之刻》攻略库的「问法增强器」。给定一段攻略正文，'
    '请站在**真实玩家在群里/对话里提问**的角度，写出 3 条能命中该片段的口语问法。\n'
    '要求：\n'
    '1) 口语化、短（10~30 字），可以带简称/口头禅/省略主语/轻微错别字感'
    '（如「北卡值得练吗」「18-7和活动EX哪个练级好」「穿甲和高爆到底啥区别」）；\n'
    '2) 每条都必须能被这段内容回答，且包含片段里的核心关键词'
    '（玩法名/舰名/关卡号/数值/机制名，片段里有什么就写什么）；\n'
    '3) 3 条之间尽量不同角度（是什么/怎么选/哪个好/数值多少）；\n'
    '4) 不要写成书面标题，不要复述原文句子。\n'
    '只输出 JSON：{"questions": ["...", "...", "..."]}'
)

# Query 改写增强（2026-09-18，参考《RAG 技术与应用》）：在「口语变体」之外，
# 额外生成改写风格问法——覆盖 对比型/反问型/多意图型/长口语 等玩家歧义/复合问法，
# 让向量库本身携带这些「精确检索句」形态，提升口语/对比/复合问法的召回。
_REWRITE_SYSTEM = (
    '你是手游《拂晓胜利之刻》攻略库的「Query 改写增强器」（参考 RAG 技术：把玩家可能的'
    '口语/歧义问法改写成可被向量命中的精确问句）。给定一段攻略正文，生成 3 条「改写问法」，'
    '从下列风格中挑选本片段适用的（不必全有，按内容选）：\n'
    '1) 对比型：「A 和 B 哪个好 / 区别」——若正文涉及可对比的概念（如 穿甲 vs 高爆、'
    '不同舰/装备/玩法）；\n'
    '2) 反问型：「X 真的没必要练吗 / X 值得投入吗 / 不 X 行不行」；\n'
    '3) 多意图型：把正文核心拆成 2 个独立子问（如「怎么获得 + 怎么培养」「数值多少 + 怎么配」）；\n'
    '4) 长口语：玩家群里啰嗦的一句长问（含简称/错别字感，15~40 字）。\n'
    '要求：每条都必须能被这段内容回答、包含核心关键词（片段里有什么写什么），且不要复述原文句子。\n'
    '只输出 JSON：{"questions": ["...", "...", "..."]}'
)


def _gen_variants(chunk: dict):
    """调 LLM 生成口语问法；返回 list（失败返回 []）。"""
    try:
        d = chat_json(
            _VARIANT_SYSTEM,
            f"文档《{chunk.get('source','')}》小节「{chunk.get('section','')}」"
            f"（主题：{chunk.get('topic','')}）\n"
            f"原问题：{chunk.get('question','')}\n"
            f"内容：\n{chunk.get('content','')[:800]}",
            temperature=0.6,
        )
        qs = d.get('questions') or []
        out = []
        for q in qs:
            q = str(q or '').strip()
            if 4 <= len(q) <= 60 and q not in out:
                out.append(q)
        return out[:3]
    except Exception as e:
        print(f"  [变体生成失败] {chunk.get('id')}: {str(e)[:80]}")
        return []


def _gen_rewrites(chunk: dict):
    """调 LLM 生成改写风格问法（对比/反问/多意图/长口语）；返回 list（失败返回 []）。"""
    try:
        d = chat_json(
            _REWRITE_SYSTEM,
            f"文档《{chunk.get('source','')}》小节「{chunk.get('section','')}」"
            f"（主题：{chunk.get('topic','')}）\n"
            f"原问题：{chunk.get('question','')}\n"
            f"内容：\n{chunk.get('content','')[:800]}",
            temperature=0.7,
        )
        qs = d.get('questions') or []
        out = []
        for q in qs:
            q = str(q or '').strip()
            if 4 <= len(q) <= 60 and q not in out:
                out.append(q)
        return out[:3]
    except Exception as e:
        print(f"  [改写生成失败] {chunk.get('id')}: {str(e)[:80]}")
        return []


def main():
    ap = argparse.ArgumentParser(description="攻略 QA 口语问法增强")
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 个待生成 chunk（调试用）')
    ap.add_argument('--full', action='store_true', help='忽略已有 alt_questions，全部重生成')
    args = ap.parse_args()

    if not os.path.exists(QA_PATH):
        print(f"缺少 QA 库: {QA_PATH}（先跑 build/攻略问答库构建.py）")
        return 1
    with open(QA_PATH, encoding='utf-8') as f:
        data = json.load(f)
    chunks = data.get('chunks', [])
    if not chunks:
        print('QA 库无 chunk')
        return 1

    pending = [c for c in chunks if args.full or not (c.get('alt_questions') or [])]
    pending_rw = [c for c in chunks if args.full or not (c.get('rewrite_questions') or [])]
    if args.limit:
        pending = pending[:args.limit]
        pending_rw = pending_rw[:args.limit]
    print(f"待生成口语变体: {len(pending)} / {len(chunks)} chunk")
    print(f"待生成改写问法: {len(pending_rw)} / {len(chunks)} chunk")

    if pending:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, qs in zip(pending, ex.map(_gen_variants, pending)):
                c['alt_questions'] = qs
        fail = [c['id'] for c in pending if not c.get('alt_questions')]
        print(f"口语变体生成完成：成功 {len(pending) - len(fail)}，失败 {len(fail)}")
    if pending_rw:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, qs in zip(pending_rw, ex.map(_gen_rewrites, pending_rw)):
                c['rewrite_questions'] = qs
        fail_rw = [c['id'] for c in pending_rw if not c.get('rewrite_questions')]
        print(f"改写问法生成完成：成功 {len(pending_rw) - len(fail_rw)}，失败 {len(fail_rw)}")

    with open(QA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    total = sum(len(c.get('alt_questions') or []) for c in chunks)
    total_rw = sum(len(c.get('rewrite_questions') or []) for c in chunks)
    print(f"QA 库已更新: {QA_PATH} | 口语变体 {total} + 改写问法 {total_rw}（{len(chunks)} chunk）")
    return 0


if __name__ == '__main__':
    sys.exit(main())
