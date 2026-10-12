# -*- coding: utf-8 -*-
"""docx → md 转换脚本（供 知识库增量.py 使用）。

把 知识库/攻略/ 下的主题攻略 docx + 知识库/遭遇战历史数据/ 的遭遇战文字轴 docx
转换为 build_kb_incremental.MD_FILES 对应的 md 文件，输出到 知识库/攻略_md/。

用法：
  python 攻略转文档.py            # 全量转换（缺的 md 生成，已存在跳过）
  python 攻略转文档.py --force    # 强制覆盖已有 md
"""
import os
import sys
import argparse

from docx import Document

_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
GUIDE_DIR = os.path.join(KB_DIR, "攻略")
HISTORY_DIR = os.path.join(KB_DIR, "遭遇战历史数据", "遭遇战历史数据")
OUT_DIR = os.path.join(KB_DIR, "攻略_md")

# docx → MD_FILES 文件名映射（key=docx 相对 GUIDE_DIR 的子串，value=(md 文件名, 默认主题)）
TOPIC_MAP = [
    ("1 前言.docx", "1 前言.md"),
    # ("2 游戏简介.docx", "2 游戏简介.md") 已移除（2026-09-10）：游戏简介不再进管道，
    # 运行时由 calc_flow._load_game_intro_realtime 实时直读 知识库/游戏简介.docx
    ("3 萌新入坑攻略.docx", "萌新入坑攻略.md"),
    ("6.1 练级百科.docx", "6.1 练级百科.md"),
    ("6.2 给萌新的活动推图攻略食用指南.docx", "6.2 给萌新的活动推图攻略食用指南.md"),
    ("6.3 给萌新的PVE配队思路.docx", "6.3 给萌新的PVE配队思路.md"),
    ("7.2 遭遇战攻略.docx", "7.2 遭遇战攻略.md"),
    ("7.3 无尽海域配队思路.docx", "7.3 无尽海域配队思路.md"),
    ("7.1 争锋竞技场简易百科.docx", "7.1 争锋竞技场简易百科.md"),
    ("8.1 拂晓伤害计算构成.docx", "8.1 拂晓伤害计算构成.md"),
    ("9 拉古兹专题.docx", "9 拉古兹专题.md"),
]

# 遭遇战文字轴 boss 名（对应 build_kb MD_FILES 的 遭遇战_<boss>.md）
ENCOUNTER_BOSSES = [
    "亚斯塔露", "雅蒙", "列拉金", "巴巴托斯", "夕茉莉", "拜蒙",
    "河豚王", "布耶尔", "阿丝莫德", "沙克斯", "嘉波",
]


def docx_to_md(path: str) -> str:
    """docx → markdown 文本（标题/表格转换，图片省略，段落保留）。"""
    doc = Document(path)
    lines = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if not t:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        style = (para.style.name if para.style else "") or ""
        if "Heading 1" in style or "Title" in style:
            lines.append("# " + t)
        elif "Heading 2" in style:
            lines.append("## " + t)
        elif "Heading 3" in style:
            lines.append("### " + t)
        else:
            lines.append(t)
    # 表格 → markdown 表
    for table in doc.tables:
        lines.append("")
        rows = []
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                rows.append(cells)
        if rows:
            ncol = max(len(r) for r in rows)
            header = rows[0] + [""] * (ncol - len(rows[0]))
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * ncol) + " |")
            for r in rows[1:]:
                r = r + [""] * (ncol - len(r))
                lines.append("| " + " | ".join(r) + " |")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def convert_topic(force: bool) -> int:
    n = 0
    for sub, md_name in TOPIC_MAP:
        src = None
        for root, _dirs, files in os.walk(GUIDE_DIR):
            for fn in files:
                if fn == sub:
                    src = os.path.join(root, fn)
                    break
            if src:
                break
        if not src:
            print(f"  [SKIP] 未找到 {sub}")
            continue
        out = os.path.join(OUT_DIR, md_name)
        if os.path.exists(out) and not force:
            print(f"  [SKIP] 已存在 {md_name}")
            continue
        text = docx_to_md(src)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  [OK] {md_name} ← {os.path.relpath(src, GUIDE_DIR)}（{len(text)} 字）")
        n += 1
    return n


def convert_encounter(force: bool) -> int:
    n = 0
    if not os.path.isdir(HISTORY_DIR):
        print(f"  [SKIP] 遭遇战历史数据目录不存在: {HISTORY_DIR}")
        return 0
    # boss → 候选 docx（同名多期取最新）
    for boss in ENCOUNTER_BOSSES:
        cands = []
        for fn in os.listdir(HISTORY_DIR):
            if fn.endswith(".docx") and boss in fn:
                cands.append(os.path.join(HISTORY_DIR, fn))
        if not cands:
            print(f"  [SKIP] 未找到 {boss} 文字轴 docx")
            continue
        src = max(cands, key=os.path.getmtime)  # 取最新一期
        out = os.path.join(OUT_DIR, f"遭遇战_{boss}.md")
        if os.path.exists(out) and not force:
            print(f"  [SKIP] 已存在 遭遇战_{boss}.md")
            continue
        text = docx_to_md(src)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  [OK] 遭遇战_{boss}.md ← {os.path.basename(src)}（{len(text)} 字）")
        n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description="docx → md 转换（供 build_kb_incremental）")
    parser.add_argument("--force", action="store_true", help="强制覆盖已存在的 md")
    args = parser.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    print("== 主题攻略 docx → md ==")
    n1 = convert_topic(args.force)
    print("== 遭遇战文字轴 docx → md ==")
    n2 = convert_encounter(args.force)
    print(f"\n完成：主题攻略 {n1} 个、遭遇战 {n2} 个，输出目录：{OUT_DIR}")
    print("下一步：python 知识库增量.py 重建知识库")


if __name__ == "__main__":
    main()
