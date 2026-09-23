# -*- coding: utf-8 -*-
"""
攻略索引生成脚本（开发者用）

扫描 知识库/攻略/** 与根目录的竞技场百科，生成 知识库/攻略/guide_index.json：
- 自动提取标题（去数字前缀、去冗余词）
- 生成默认标签（按文件名关键词）
- 保留已有人工维护的 tags/desc（合并更新，不覆盖）

用法：
    python build_guide_index.py          # 全量重建（保留手工 tags）
    python build_guide_index.py --force  # 全量重建（覆盖手工 tags）
"""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
GUIDE_DIR = os.path.join(KB_DIR, "攻略")
INDEX_FILE = os.path.join(GUIDE_DIR, "guide_index.json")

# 文件名关键词 → 默认标签
DEFAULT_TAGS = {
    "萌新入坑": ["新人", "萌新", "入坑", "新手", "入门"],
    "遭遇战": ["遭遇战", "boss", "BOSS"],
    "练级": ["练级", "升级", "省油"],
    "配装": ["配装", "装备", "搭配"],
    "装备速查": ["装备", "速查", "配装"],
    "一键装备": ["装备", "一键", "设置"],
    "PVE": ["PVE", "推图", "活动"],
    "舰灵推荐": ["舰灵", "推荐", "PVE"],
    "伤害计算": ["伤害", "计算", "公式", "构成"],
    "拉古兹": ["拉古兹", "拉古茨"],
    "竞技场": ["竞技场", "争锋"],
    "无尽海域": ["无尽", "海域", "配队"],
    "游戏简介": ["简介", "介绍", "游戏"],
    "前言": ["前言"],
    "企划书": ["企划", "手册"],
    "面板数值": ["面板", "数值", "公式"],
    "改造优先级": ["改造", "优先级"],
    "阵容分析": ["阵容", "分析"],
    "天赋": ["天赋", "PVP"],
    "咨询问答": ["问答", "咨询"],
    "模板": ["模板"],
}

_STRIP_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)?\s*[- ]?\s*")  # 数字前缀


def _clean_title(fn: str) -> str:
    """从文件名提取干净标题：去扩展名、去数字前缀、去版本号尾。"""
    base = os.path.splitext(fn)[0]
    base = _STRIP_PREFIX.sub("", base)
    base = re.sub(r"[Vv]\d+(?:\.\d+)?\s*$", "", base).strip()
    base = re.sub(r"\d{6,}$", "", base).strip()  # 去掉 260718 这类日期尾
    base = base.strip(" -—_")
    return base or os.path.splitext(fn)[0]


def _category_for(title: str, path: str) -> str:
    """攻略分类：萌新入坑 / 一图速查 / 练级须知 / 进阶必备。
    规则：萌新入坑（萌新入坑攻略/思维导图/性能优化）、练级（练级百科）、
    进阶必备（文件夹 7/8/9 或标题：争锋/遭遇战/无尽/伤害计算/拉古兹）、其余一图速查。"""
    if path.startswith('攻略/3 ') or '萌新入坑' in title:
        return '萌新入坑'
    if '新手入门思维导图' in title or '性能优化' in title:
        return '萌新入坑'
    if '练级' in title:
        return '练级须知'
    if any(w in title for w in ('争锋竞技场', '遭遇战', '无尽海域', '伤害计算构成', '拉古兹')):
        return '进阶必备'
    if path.startswith(('攻略/7 ', '攻略/8 ', '攻略/9 ')):
        return '进阶必备'
    return '一图速查'


def _default_tags(title: str, path: str) -> list:
    tags = set()
    hay = title + path
    for kw, tlist in DEFAULT_TAGS.items():
        if kw in hay:
            tags.update(tlist)
    return sorted(tags)


def main():
    force = "--force" in sys.argv
    existing = {}
    if os.path.exists(INDEX_FILE) and not force:
        try:
            existing = {g["path"]: g for g in json.load(open(INDEX_FILE, encoding="utf-8"))["guides"]}
        except Exception:
            existing = {}

    guides = []
    seen = set()
    if os.path.isdir(GUIDE_DIR):
        for root, _dirs, files in os.walk(GUIDE_DIR):
            rel_dir = os.path.relpath(root, KB_DIR).replace("\\", "/")
            for fn in sorted(files):
                if fn.startswith("~$"):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext not in (".docx", ".doc", ".png", ".jpg", ".jpeg", ".xlsx"):
                    continue
                rel = f"{rel_dir}/{fn}"
                if rel in seen:
                    continue
                seen.add(rel)
                title = _clean_title(fn)
                old = existing.get(rel, {})
                tags = old.get("tags") or _default_tags(title, rel)
                desc = old.get("desc", "")
                cat = old.get("category") or _category_for(title, rel)
                guides.append({
                    "id": old.get("id") or f"guide_{len(guides) + 1:03d}",
                    "title": old.get("title") or title,
                    "tags": tags,
                    "path": rel,
                    "kind": ext,
                    "desc": desc,
                    "category": cat,
                })
    # 根目录竞技场百科
    if os.path.isdir(KB_DIR):
        for fn in sorted(os.listdir(KB_DIR)):
            if fn.startswith("~$") or not fn.startswith("争锋竞技场"):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".docx", ".doc"):
                continue
            rel = fn
            if rel in seen:
                continue
            seen.add(rel)
            old = existing.get(rel, {})
            cat = old.get("category") or _category_for("争锋竞技场简易百科", rel)
            guides.append({
                "id": old.get("id") or f"guide_{len(guides) + 1:03d}",
                "title": old.get("title") or "争锋竞技场简易百科",
                "tags": old.get("tags") or ["竞技场", "争锋", "PVP"],
                "path": rel,
                "kind": ext,
                "desc": old.get("desc", ""),
                "category": cat,
            })

    out = {"updated": "", "guides": guides}
    import datetime
    out["updated"] = datetime.date.today().isoformat()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"已生成 {INDEX_FILE}，共 {len(guides)} 条攻略索引")
    print("提示：可在索引文件中为每条补 tags/desc，下次重建会保留。")

    # 同步：把每个文档的可下载文件路径固化进 拂晓攻略知识库_精简版.json 的 documents[].file
    _merge_file_paths(guides)


def _merge_file_paths(guides: list):
    """按标题匹配，把 guide_index 的 path 写入知识库精简版 JSON 的 documents[].file 字段。
    数据在数据层固化，智能体侧直接读 file 即可输出下载标记（不再运行时映射）。"""
    kb_file = os.path.join(KB_DIR, "攻略", "拂晓攻略知识库_精简版.json")
    if not os.path.exists(kb_file):
        print("跳过路径同步：未找到", kb_file)
        return
    idx_map = {}
    for g in guides:
        t = g.get("title", "").strip()
        p = g.get("path", "").strip()
        if t and p:
            idx_map.setdefault(t, p)
    with open(kb_file, encoding="utf-8") as f:
        data = json.load(f)
    kb = data.setdefault("knowledge_base", {})
    docs = kb.get("documents", [])
    filled = 0
    for d in docs:
        t = d.get("title", "").strip()
        path = idx_map.get(t)
        if not path:  # 子串/互含兜底
            for it, p in idx_map.items():
                if t and (t in it or it in t):
                    path = p
                    break
        if path:
            d["file"] = path
            filled += 1
    kb["documents"] = docs
    with open(kb_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已同步 {filled}/{len(docs)} 篇攻略的文件路径 → {kb_file}")


if __name__ == "__main__":
    if "--watch" in sys.argv:
        # 实时同步模式：监控攻略目录文件变化，变化自动重建索引+知识库（供智能体只读使用）
        import hashlib
        import time

        def _files_signature() -> str:
            sigs = []
            if os.path.isdir(GUIDE_DIR):
                for root, _dirs, files in os.walk(GUIDE_DIR):
                    rel_dir = os.path.relpath(root, KB_DIR).replace("\\", "/")
                    for fn in sorted(files):
                        if fn.startswith("~$"):
                            continue
                        ext = os.path.splitext(fn)[1].lower()
                        if ext not in (".docx", ".doc", ".png", ".jpg", ".jpeg", ".xlsx"):
                            continue
                        p = os.path.join(root, fn)
                        try:
                            st = os.stat(p)
                            sigs.append(f"{rel_dir}/{fn}|{st.st_mtime:.0f}|{st.st_size}")
                        except OSError:
                            pass
            if os.path.isdir(KB_DIR):
                for fn in sorted(os.listdir(KB_DIR)):
                    if fn.startswith("~$") or not fn.startswith("争锋竞技场"):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in (".docx", ".doc"):
                        continue
                    p = os.path.join(KB_DIR, fn)
                    try:
                        st = os.stat(p)
                        sigs.append(f"{fn}|{st.st_mtime:.0f}|{st.st_size}")
                    except OSError:
                        pass
            return hashlib.md5("\n".join(sigs).encode("utf-8")).hexdigest()

        print("攻略索引实时同步已启动（--watch）。增删改 知识库/攻略/ 文件后自动重建索引。Ctrl+C 停止。")
        last = _files_signature()
        while True:
            time.sleep(3)
            cur = _files_signature()
            if cur != last:
                last = cur
                try:
                    main()
                except Exception as e:
                    print(f"重建失败: {e}")
    else:
        main()
