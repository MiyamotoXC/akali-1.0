# -*- coding: utf-8 -*-
"""攻略 QA 问答库构建（增量）。

从 知识库/攻略_md/ 全量正文切 chunk（复用 kb_incremental 的清洗/分节/切分），
每个 chunk 由 LLM 生成一条口语化问题，输出 QA 问答库 JSON（供向量化）。

增量策略（与 kb_incremental 同语义）：
- file-level：md 文件 MD5 未变 → 整文件沿用旧 QA，不调 LLM；
- chunk-level：chunk id（含 section hash）未变 → 沿用旧问题，不调 LLM；
- 仅新增/变更 chunk 调 LLM。

用法（在 开发者入口 目录下）：
  python build/攻略问答库构建.py           # 增量
  python build/攻略问答库构建.py --full    # 强制全部重新生成问题
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))
sys.path.insert(0, _HERE)  # 供 import kb_incremental

from 知识库增量 import (
    MD_FILES, WORK_DIR, file_hash, make_file_prefix, process_markdown_file,
)
from src.模型客户端 import chat_json

# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
OUTPUT_PATH = os.path.join(KB_DIR, "llm", "攻略QA库.json")

# 问题生成 prompt：站在玩家角度，问题句要能被真实问法命中
_QUESTION_SYSTEM = (
    '你是游戏攻略知识库的问答对生成器。根据给定的攻略片段，站在玩家角度生成 1 条'
    '最可能命中该片段内容的用户提问。要求：口语化、15~40 字、包含片段的核心关键词'
    '（玩法名/角色名/数值/关卡号等，片段里有什么就写什么）。只输出 JSON：{"question": "..."}'
)


def _gen_question(chunk: dict) -> str:
    """调 LLM 为单个 chunk 生成问题；失败返回 ''（嵌入时退化为纯正文）。"""
    try:
        d = chat_json(
            _QUESTION_SYSTEM,
            f"文档《{chunk['source']}》小节「{chunk['section']}」（主题：{chunk['topic']}）内容：\n{chunk['content']}",
        )
        return str(d.get('question') or '').strip()
    except Exception as e:
        print(f"  [问题生成失败] {chunk['id']}: {e}")
        return ''


def main():
    parser = argparse.ArgumentParser(description="攻略 QA 问答库构建（增量）")
    parser.add_argument("--full", action="store_true", help="强制全部重新生成问题")
    args = parser.parse_args()

    old = None
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            old = json.load(f)
    old_file_hashes = {} if args.full or old is None else old.get("metadata", {}).get("file_hashes", {})
    old_by_id = {} if args.full or old is None else {c["id"]: c for c in old.get("chunks", [])}

    all_chunks = []
    used_ids = set()
    new_file_hashes = {}
    kept = 0
    pending = []  # 需要调 LLM 生成问题的 chunk

    # 排除清单：这些 md 不进入攻略 QA 向量库（改走总向量库直答，如游戏简介）。
    # 注意：agent 总知识库（拂晓知识库_agent.json）仍由 kb_incremental 构建保留，
    # 仅攻略 QA 库 + 向量库不再收录，避免"游戏简介"类问题被路由到攻略路线。
    _EXCLUDE_MD = {"2 游戏简介.md"}
    for md_file, source_name, default_topic in MD_FILES:
        if md_file in _EXCLUDE_MD:
            print(f"[排除] {md_file}（不进入攻略 QA 库，改走总向量库直答）")
            continue
        md_path = os.path.join(WORK_DIR, md_file)
        if not os.path.exists(md_path):
            print(f"[缺失] {md_file}（旧条目随之移除）")
            continue
        cur = file_hash(md_path)
        new_file_hashes[md_file] = cur
        if old_file_hashes.get(md_file) == cur:
            # 文件未变：问题已生成的沿用旧 QA（不调 LLM）；问题为空的（上次生成失败）重新生成
            n = 0
            for c in old_by_id.values():
                if c["source"] == md_file and c["id"] not in used_ids:
                    used_ids.add(c["id"])
                    n += 1
                    if c.get("question"):
                        all_chunks.append(c)
                        kept += 1
                    else:
                        pending.append(c)
            print(f"[未变] {md_file}（沿用 {n} 条）")
            continue
        tag = "变更" if md_file in old_file_hashes else "新增"
        print(f"[{tag}] {md_file}")
        chunks, _ = process_markdown_file(md_file, source_name, default_topic, make_file_prefix(source_name))
        for c in chunks:
            if c["id"] in used_ids:
                continue
            used_ids.add(c["id"])
            oc = old_by_id.get(c["id"])
            if oc and oc.get("question"):
                # chunk 内容未变（id 含 section hash）→ 沿用旧问题
                c["question"] = oc["question"]
                all_chunks.append(c)
                kept += 1
            else:
                pending.append(c)

    # LLM 并发生成问题（4 并发，串行过慢）
    if pending:
        print(f"\n生成问题：{len(pending)} 个 chunk（LLM x4 并发）...")
        with ThreadPoolExecutor(max_workers=4) as ex:
            for c, q in zip(pending, ex.map(_gen_question, pending)):
                c["question"] = q
                all_chunks.append(c)
        fail = [c["id"] for c in pending if not c["question"]]
        if fail:
            print(f"[警告] {len(fail)} 个 chunk 问题生成失败（嵌入时退化为纯正文）：{fail[:10]}{' ...' if len(fail) > 10 else ''}")

    # 保留「问答补充.txt」已生成的条目（id 前缀 qa_supp_），避免重建时丢失。
    # 这些条目由 开发者入口/build/补充问答录入.py 负责生成/更新（按 id 幂等增量），
    # 不属于 MD_FILES，故在此从旧 JSON 透传，而非重新走 MD 构建逻辑（--full 也保留）。
    if old is not None:
        kept_supp = 0
        for c in old.get("chunks", []):
            cid = c.get("id", "")
            if cid.startswith("qa_supp_") and cid not in used_ids:
                used_ids.add(cid)
                all_chunks.append(c)
                kept_supp += 1
        if kept_supp:
            print(f"[保留] 问答补充.txt 条目 {kept_supp} 条（qa_supp_*，不重建；新增/修订请跑 补充问答录入.py）")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    out = {
        "metadata": {
            "name": "拂晓攻略QA问答库",
            "source": "知识库/攻略_md 全量正文 + LLM 逐 chunk 问题生成（max 1000 字/chunk）",
            "total_chunks": len(all_chunks),
            "file_hashes": new_file_hashes,
        },
        "chunks": all_chunks,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 60}")
    print(f"QA 库已写入: {OUTPUT_PATH}")
    print(f"chunks: {len(all_chunks)}（沿用 {kept} / 新生成 {len(pending)}）")
    sizes = [len(c['content']) for c in all_chunks]
    if sizes:
        print(f"chunk 正文长度: min={min(sizes)} max={max(sizes)} avg={sum(sizes) // len(sizes)}")
    no_q = [c['id'] for c in all_chunks if not c.get('question')]
    print(f"无问题 chunk: {len(no_q)}")


if __name__ == "__main__":
    main()
