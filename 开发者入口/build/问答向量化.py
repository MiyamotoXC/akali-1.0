# -*- coding: utf-8 -*-
"""攻略 QA 问答库向量化（增量，AGICTO /v1/embeddings）。

- 嵌入文本 = question + "\\n" + content（question 为空时为纯正文）；
- 增量：以嵌入文本 MD5 为键复用旧向量，仅新增/变更 chunk 调 API；
- 换向量模型（维度变化）→ 全量重建；
- 产物：知识库/llm/攻略QA向量库.npz（ids / hashes / matrix，L2 归一化 float32）。

用法（在 开发者入口 目录下）：
  python build/问答向量化.py
"""
import hashlib
import json
import os
import sys

import numpy as np
from openai import OpenAI

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))

# 复用构建侧 LLM 配置（AGICTO_API_KEY / MODEL_SERVER，本地 .env 读取）
from src.模型客户端 import api_key, MODEL_SERVER  # noqa: E402

# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
QA_PATH = os.path.join(KB_DIR, "llm", "攻略QA库.json")
NPZ_PATH = os.path.join(KB_DIR, "llm", "攻略QA向量库.npz")

# 向量模型（.env EMBEDDING_MODEL 可换）
EMBED_MODEL = os.environ.get('EMBEDDING_MODEL', 'text-embedding-v4')
BATCH = 10  # text-embedding-v4 单请求上限 10 条


def _embed_texts(texts, client):
    """批量嵌入，返回 np.float32 矩阵（按输入顺序）。"""
    out = [None] * len(texts)
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch, encoding_format="float")
        if not resp.data:
            # 中转站用 HTTP 200 包错误（如超批量/鉴权失败），直接抛出响应内容定位问题
            raise RuntimeError(f"嵌入接口返回异常: {resp}")
        # 按 index 对位，防服务端乱序
        by_idx = {d.index: d.embedding for d in resp.data}
        for j, emb in by_idx.items():
            out[i + j] = emb
        print(f"  已嵌入 {min(i + BATCH, len(texts))}/{len(texts)}")
    return np.asarray(out, dtype=np.float32)


def main():
    if not os.path.exists(QA_PATH):
        print(f"缺少 QA 库: {QA_PATH}（请先运行 build/攻略问答库构建.py）")
        return 1
    with open(QA_PATH, encoding="utf-8") as f:
        chunks = json.load(f)["chunks"]
    if not chunks:
        print("QA 库为空。")
        return 1

    # 嵌入文本与增量键（标题也是知识：文档名与小节标题编入嵌入文本，标题类提问可命中）
    texts = []
    ids = []
    for c in chunks:
        q = (c.get('question') or '').strip()
        # 语境信息（2026-09-18）：向量文本前置场景标注（主题+小节），
        # 让攻略库向量携带语境，避免通用问法误召回 Boss/场景专属片段（如把拉古兹自爆机群
        # 的防空炮建议当成通用防空策略）。同时检索展示层（_guide_rag_search）也据此给 LLM 补语境。
        _ctx = f"【语境：{c.get('topic') or ''}｜{c.get('section') or ''}】\n" if (c.get('topic') or c.get('section')) else ""
        head = f"《{str(c.get('source') or '').removesuffix('.md')}》{c.get('section') or ''}\n"
        texts.append(_ctx + head + (q + "\n" if q else "") + c['content'])
        ids.append(c['id'])
        # 口语问法增强（问答变体生成.py 生成）：每条变体单独成一行向量，
        # 指向同一 chunk id —— 玩家口语 query 命中口语向量（检索层按 chunk 归并取最高分）
        for aq in (c.get('alt_questions') or []):
            if not str(aq).strip():
                continue
            texts.append(f"{_ctx}{head}{str(aq).strip()}\n{c['content'][:200]}")
            ids.append(c['id'])
        # Query 改写增强（2026-09-18，同脚本生成）：改写风格问法（对比/反问/多意图/
        # 长口语）单独成一行向量，指向同一 chunk id —— 让向量库携带精确检索句形态，
        # 提升对比/复合/反问类问法的召回（检索层同样按 chunk 归并取最高分）。
        for aq in (c.get('rewrite_questions') or []):
            if not str(aq).strip():
                continue
            texts.append(f"{_ctx}{head}{str(aq).strip()}\n{c['content'][:200]}")
            ids.append(c['id'])
    hashes = [hashlib.md5(t.encode("utf-8")).hexdigest() for t in texts]

    # 旧向量缓存
    old_map = {}
    old_dim = None
    if os.path.exists(NPZ_PATH):
        z = np.load(NPZ_PATH)
        old_dim = int(z['matrix'].shape[1])
        for h, v in zip(z['hashes'], z['matrix']):
            old_map[str(h)] = v

    need = [i for i, h in enumerate(hashes) if h not in old_map]
    if need and old_map:
        # 维度变化（换模型）→ 全量重建
        client = OpenAI(api_key=api_key, base_url=MODEL_SERVER, timeout=60)
        probe = _embed_texts([texts[need[0]]], client)
        if probe.shape[1] != old_dim:
            print(f"[提示] 向量维度变化（{old_dim} → {probe.shape[1]}，可能更换了 EMBEDDING_MODEL），全量重建")
            old_map = {}
            need = list(range(len(hashes)))
        else:
            old_map[hashes[need[0]]] = probe[0]
            need = need[1:]

    rows = {}
    reused = 0
    for i, h in enumerate(hashes):
        if h in old_map:
            rows[i] = old_map[h]
            reused += 1
    if need:
        print(f"向量模型: {EMBED_MODEL}（新增/变更 {len(need)} 条，复用 {reused} 条）")
        client = OpenAI(api_key=api_key, base_url=MODEL_SERVER, timeout=60)
        vecs = _embed_texts([texts[i] for i in need], client)
        for i, v in zip(need, vecs):
            rows[i] = v
    else:
        print(f"向量模型: {EMBED_MODEL}（全部 {reused} 条复用缓存，无需调 API）")

    # 行数 = 向量行数（原 chunk + 口语变体），勿用 len(chunks)（会与 ids 错位）
    matrix = np.stack([rows[i] for i in range(len(texts))]).astype(np.float32)
    # L2 归一化（余弦检索 = 点积）
    matrix /= (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12)
    np.savez(NPZ_PATH, ids=np.array(ids), hashes=np.array(hashes), matrix=matrix)

    print(f"\n{'=' * 60}")
    print(f"向量库已写入: {NPZ_PATH}")
    print(f"chunks: {len(chunks)} | 向量行: {len(ids)}（含口语/改写变体） | 维度: {matrix.shape[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
