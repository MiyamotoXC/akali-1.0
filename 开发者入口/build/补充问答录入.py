# -*- coding: utf-8 -*-
"""把 知识库/问答补充.txt 的常识条目补充进「攻略 QA 向量库」。

流程：
  1. 解析 txt（每行 `编号.内容`）→ N 条常识；
  2. 调 LLM 为每条生成 question（标准问法）+ 3 条 alt_questions（口语变体）+ keywords；
     （复用 guide_qa_build 的玩家视角问法口径，与既有库一致）
  3. 追加到 攻略QA库.json（备份后写回）；
  4. 增量嵌入 攻略QA向量库.npz（复用 guide_qa_embed 的文本构造 + 同款 text-embedding-v4，
     question+content 一行、每个 alt 变体一行，ids 重复指回 chunk id；带 429 重试）。

用法（在 阿卡莉1.0 目录，用项目 venv）：
  .venv/Scripts/python.exe 开发者入口/build/补充问答录入.py
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from openai import OpenAI

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))
from src.模型客户端 import chat_json, api_key, MODEL_SERVER  # noqa: E402

_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
QA_PATH = os.path.join(KB_DIR, "llm", "攻略QA库.json")
NPZ_PATH = os.path.join(KB_DIR, "llm", "攻略QA向量库.npz")
TXT_PATH = os.path.join(KB_DIR, "问答补充.txt")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-v4")
BATCH = 8
SUPP_SOURCE = "问答补充.txt"
SUPP_TOPIC = "咨询问答"  # 与既有「咨询问答表 P1/P2/P3」同分组，便于统一检索/路由

_COMMON_SYSTEM = (
    "你是手游《拂晓胜利之刻》攻略库的问答对生成器，处理用户提供的游戏常识补充条目。"
    "给定一条知识陈述，请完成三件事：\n"
    "1) 站在玩家角度生成 1 条最可能命中该内容的提问（口语化、15~40 字、含核心关键词"
    "（玩法名/舰名/数值/关卡号/系统名，片段里有啥写啥）；\n"
    "2) 再生成 3 条玩家口语问法变体（短、10~30字、可带简称/口头禅/省略主语/轻微错别字感，"
    "且 3 条尽量不同角度：是什么/怎么弄/数值多少）；\n"
    "3) 提取 3~6 个检索关键词（玩法名/舰名/数值/机制/系统名）。\n"
    '只输出 JSON：{"question":"...","alt_questions":["...","...","..."],"keywords":["...","..."]}'
)


def _gen(item):
    """为单条常识生成 (question, alt_questions, keywords)，失败退化为纯正文。"""
    num, content = item
    for attempt in range(3):
        try:
            d = chat_json(_COMMON_SYSTEM, f"常识条目：{content}", temperature=0.6)
            q = str(d.get("question") or "").strip()
            alts = [str(x).strip() for x in (d.get("alt_questions") or []) if str(x).strip()]
            kws = [str(x).strip() for x in (d.get("keywords") or []) if str(x).strip()]
            if not q:
                q = content
            return q, alts[:3], kws[:6]
        except Exception as e:
            print(f"  [生成重试 {attempt + 1}] 条目{num}: {str(e)[:60]}")
            time.sleep(2 * (attempt + 1))
    return content, [], []


def _embed(texts, client):
    """批量嵌入（带 429 重试），返回 np.float32 矩阵。"""
    out = [None] * len(texts)
    for i in range(0, len(texts), BATCH):
        batch = texts[i : i + BATCH]
        done = False
        for attempt in range(5):
            try:
                resp = client.embeddings.create(
                    model=EMBED_MODEL, input=batch, encoding_format="float"
                )
                by_idx = {d.index: d.embedding for d in resp.data}
                for j, emb in by_idx.items():
                    out[i + j] = emb
                done = True
                break
            except Exception as e:
                print(f"  [嵌入重试 {attempt + 1}] {str(e)[:60]}")
                time.sleep(3 * (attempt + 1))
        if not done:
            raise RuntimeError(f"embed 失败（段 {i}~{i + len(batch)}）")
        print(f"  已嵌入 {min(i + BATCH, len(texts))}/{len(texts)}")
    return np.asarray(out, dtype=np.float32)


def _build_texts_and_ids(chunks):
    texts, ids = [], []
    for c in chunks:
        q = (c.get("question") or "").strip()
        head = f"《{str(c.get('source') or '').removesuffix('.md')}》{c.get('section') or ''}\n"
        texts.append(head + (q + "\n" if q else "") + c["content"])
        ids.append(c["id"])
        for aq in c.get("alt_questions") or []:
            aq = str(aq).strip()
            if not aq:
                continue
            texts.append(f"{head}{aq}\n{c['content'][:200]}")
            ids.append(c["id"])
    return texts, ids


def main():
    # ---------- 1. 解析 txt ----------
    if not os.path.exists(TXT_PATH):
        print(f"缺少补充文件: {TXT_PATH}")
        return 1
    raw = open(TXT_PATH, encoding="utf-8").read()
    items = []
    for ln in raw.splitlines():
        # 编号后的点号可能缺失（如「15天赋解锁…」），故点号可选 —— 与 补充技巧录入.py 同一规则
        m = re.match(r"^\s*(\d+)\s*[.．、。]?\s*(\S.*)$", ln)
        if m:
            items.append((int(m.group(1)), m.group(2).strip()))
    print(f"[解析] 问答补充.txt -> {len(items)} 条")

    # ---------- 2. LLM 生成 question/alt/questions/keywords ----------
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for (num, content), (q, alts, kws) in zip(items, ex.map(_gen, items)):
            results.append(
                {
                    "num": num,
                    "content": content,
                    "question": q,
                    "alt_questions": alts,
                    "keywords": kws,
                }
            )
    ok = sum(1 for r in results if r["alt_questions"])
    print(f"[生成] 成功 {ok}/{len(results)}（含 question+变体）；失败退化为纯正文")

    # ---------- 3. 构造 chunk 并追加到 攻略QA库.json ----------
    ts = time.strftime("%Y%m%d-%H%M%S")
    new_chunks = []
    for r in results:
        cid = f"qa_supp_{r['num']:03d}"
        new_chunks.append(
            {
                "id": cid,
                "source": SUPP_SOURCE,
                "topic": SUPP_TOPIC,
                "section": "问答补充",
                "content": r["content"],
                "keywords": r["keywords"] or [r["content"][:6]],
                "question": r["question"],
                "alt_questions": r["alt_questions"],
            }
        )

    shutil.copy2(QA_PATH, QA_PATH + f".bak-{ts}")
    data = json.load(open(QA_PATH, encoding="utf-8"))
    existing = {c["id"] for c in data["chunks"]}
    add = [c for c in new_chunks if c["id"] not in existing]
    dup = len(new_chunks) - len(add)
    data["chunks"].extend(add)
    data["metadata"]["total_chunks"] = len(data["chunks"])
    json.dump(data, open(QA_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[追加] 攻略QA库.json +{len(add)}（跳过重复 {dup}）-> 现 {len(data['chunks'])} 条")
    if not add:
        print("无新增，跳过嵌入。")
        return 0

    # ---------- 4. 增量嵌入 npz ----------
    if os.path.exists(NPZ_PATH):
        shutil.copy2(NPZ_PATH, NPZ_PATH + f".bak-{ts}")
    all_chunks = data["chunks"]
    texts, ids = _build_texts_and_ids(all_chunks)
    hashes = [hashlib.md5(t.encode("utf-8")).hexdigest() for t in texts]

    old_map, old_dim = {}, None
    if os.path.exists(NPZ_PATH + f".bak-{ts}"):
        z = np.load(NPZ_PATH + f".bak-{ts}")
        old_dim = int(z["matrix"].shape[1])
        for h, v in zip(z["hashes"], z["matrix"]):
            old_map[str(h)] = v

    need = [i for i, h in enumerate(hashes) if h not in old_map]
    client = OpenAI(api_key=api_key, base_url=MODEL_SERVER, timeout=60)
    if need and old_map:
        probe = _embed([texts[need[0]]], client)
        if probe.shape[1] != old_dim:
            print(f"[提示] 向量维度变化（{old_dim} -> {probe.shape[1]}），全量重建")
            old_map = {}
            need = list(range(len(hashes)))
        else:
            old_map[hashes[need[0]]] = probe[0]
            need = need[1:]

    rows = {}
    for i, h in enumerate(hashes):
        if h in old_map:
            rows[i] = old_map[h]
    if need:
        print(f"[嵌入] 向量模型 {EMBED_MODEL}，新增/变更 {len(need)} 行，复用 {len(rows)} 行")
        vecs = _embed([texts[i] for i in need], client)
        for i, v in zip(need, vecs):
            rows[i] = v
    else:
        print(f"[嵌入] 全部 {len(rows)} 行复用缓存，无需调 API")

    matrix = np.stack([rows[i] for i in range(len(texts))]).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12
    np.savez(NPZ_PATH, ids=np.array(ids), hashes=np.array(hashes), matrix=matrix)

    print(f"\n{'=' * 60}")
    print(f"向量库已写入: {NPZ_PATH}")
    print(f"chunks: {len(all_chunks)} | 向量行: {len(ids)}（含口语变体） | 维度: {matrix.shape[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
