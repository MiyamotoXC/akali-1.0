# -*- coding: utf-8 -*-
"""拂晓知识库 · 构建入口（增量自动构建 + 两套分区）。

检测知识库源数据（Excel 底层数据 / 攻略 docx / 遭遇战历史数据）的变化，
按依赖链增量更新所有产物（供 AGENT 消费），无需手动逐个跑脚本。

两套分区：
  套A · 表格增量更新底层信息
      覆盖：角色数据（raw_data / data.json / talent）
      产物：知识库/底层数据/产物/raw_data.json、data.json、talent_*.json
      （另：标准化副本.py 可手动单独生成 data.json 标准化副本，不在自动链上）
  套B · 向量知识库与问答构建
      覆盖：攻略转文档 → 攻略索引 → 知识库增量 → 攻略问答库构建
            → 补充问答录入 → 问答向量化 → 补充技巧录入
      产物：攻略QA库.json / 攻略QA向量库.npz / tips.json / 拂晓知识库_agent.json / guide_index.json

用法（在 阿卡莉1.0 目录下，用项目 venv）：
  .venv/Scripts/python.exe 开发者入口/build/构建入口.py            # 交互选择套A/套B/全部
  .venv/Scripts/python.exe 开发者入口/build/构建入口.py --suite a   # 非交互：只跑套A
  .venv/Scripts/python.exe 开发者入口/build/构建入口.py --suite b   # 非交互：只跑套B
  .venv/Scripts/python.exe 开发者入口/build/构建入口.py --suite all # 非交互：两套全跑
  .venv/Scripts/python.exe 开发者入口/build/构建入口.py --full     # 强制全量重建（配合 --suite）
  .venv/Scripts/python.exe 开发者入口/build/构建入口.py --verbose  # 显示每个脚本的完整输出
  .venv/Scripts/python.exe 开发者入口/build/构建入口.py --list     # 只看各环节是否需要更新

逐文件、逐函数说明见 开发者入口/构建说明.md。

产物与消费方：
  raw_data.json / data.json / talent_*.json   → AGENT 伤害计算/角色查询
  攻略/guide_index.json                       → AGENT 攻略滑块
  攻略/tips.json                              → AGENT 小技巧结构化直答（core/tips_index.py）
  拂晓知识库_agent.json                        → AGENT kb_search（LLM 对话辅助）
  攻略QA库.json / 攻略QA向量库.npz              → AGENT 攻略 RAG 语义召回
要改本体数据处理规则，见 开发者入口/build/规则补全.py（stage 3 规则补全）；要加小技巧条目，见 知识库/问答补充.txt。
"""
import os
import sys
import argparse
import subprocess
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
RAW_DIR = os.path.join(KB_DIR, "底层数据", "产物")   # 生成的数据
SRC_DIR = os.path.join(KB_DIR, "底层数据", "源文件")  # 人工维护的表格源
GUIDE_DIR = os.path.join(KB_DIR, "攻略")
HISTORY_DIR = os.path.join(KB_DIR, "遭遇战历史数据", "遭遇战历史数据")
MD_DIR = os.path.join(KB_DIR, "攻略_md")
# 使用项目根目录的虚拟环境
PY = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable  # 回退：当前解释器

# 产物
RAW_JSON = os.path.join(RAW_DIR, "raw_data.json")
GUIDE_INDEX = os.path.join(GUIDE_DIR, "guide_index.json")
AGENT_KB = os.path.join(KB_DIR, "llm", "拂晓知识库_agent.json")
GUIDE_QA_JSON = os.path.join(KB_DIR, "llm", "攻略QA库.json")
GUIDE_QA_NPZ = os.path.join(KB_DIR, "llm", "攻略QA向量库.npz")
# 人工维护的小技巧/常识补充（两个 append_* 脚本的共同输入）
SUPP_TXT = os.path.join(KB_DIR, "问答补充.txt")
TIPS_JSON = os.path.join(GUIDE_DIR, "tips.json")

# 输入源
XLSX_SOURCES = [
    os.path.join(SRC_DIR, f) for f in
    ("舰灵数值（整理版）.xlsx", "拂晓舰灵普攻倍率cd1.1.xlsx", "CharSkillInfoLv10 260728.xlsx")
    if os.path.exists(os.path.join(SRC_DIR, f))
]
# PVP 天赋推荐表也是源表（生成 talent_pvp.json）：文件名带日期，动态匹配；
# 否则「只改了这张表」时套A 不会判定需要重建。
if os.path.isdir(SRC_DIR):
    XLSX_SOURCES += [
        os.path.join(SRC_DIR, f) for f in sorted(os.listdir(SRC_DIR))
        if f.lower().endswith('.xlsx') and not f.startswith('~$')
        and ('pvp' in f.lower() or '天赋表' in f)
        and os.path.join(SRC_DIR, f) not in XLSX_SOURCES
    ]


def _mtime(path):
    return os.path.getmtime(path) if path and os.path.exists(path) else 0.0


def _newest(sources):
    return max((_mtime(s) for s in sources), default=0.0)


def _walk_docx(dirs):
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.endswith(".docx"):
                    out.append(os.path.join(root, fn))
    return out


def _newest_path(paths):
    """一组文件里 mtime 最新的那个路径（空则返回 ""）。

    `need_build` 的 target 用它当基准：原先直接用 os.listdir()[0]，而 listdir 顺序不确定，
    可能拿到一个「很新」的 md 从而误判成「已最新」，漏掉真正需要转换的 docx。
    """
    paths = [p for p in (paths or []) if p]
    return max(paths, key=_mtime) if paths else ""


def need_build(sources, target, force):
    """任一输入比产物新（或产物缺失）→ 需要重建。"""
    if force:
        return True
    if not os.path.exists(target):
        return True
    return _newest(sources) > _mtime(target)


def run(script, args_list, verbose):
    # 脚本位于 build/ 目录下
    script_path = os.path.join(_HERE, script)
    if not os.path.exists(script_path):
        print(f"  [错误] 找不到脚本：{script_path}")
        return False
    cmd = [PY, script_path] + args_list
    print(f"\n> python {script} {' '.join(args_list)}")
    if verbose:
        r = subprocess.run(cmd, cwd=_HERE, capture_output=True, text=True, encoding="utf-8")
        if r.stdout:
            print(r.stdout)
        if r.returncode != 0 and r.stderr:
            print(r.stderr, file=sys.stderr)
    else:
        r = subprocess.run(cmd, cwd=_HERE, capture_output=True, text=True, encoding="utf-8")
        tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-4:]
        print("  " + "\n  ".join(tail))
        if r.returncode != 0:
            print("  [失败] " + ((r.stderr or "")[-300:]), file=sys.stderr)
    return r.returncode == 0


def _build_steps(force, verbose):
    """构造全部环节（带所属套标记 A/B）。need 在构造时求值（早于任何重建）。"""
    docx_sources = _walk_docx([GUIDE_DIR, HISTORY_DIR])
    md_files = [os.path.join(MD_DIR, f) for f in os.listdir(MD_DIR)] if os.path.isdir(MD_DIR) else []
    return [
        ("A", "角色数据（raw_data/data.json/talent）", need_build(XLSX_SOURCES, RAW_JSON, force),
         lambda: run("角色数据构建.py", ["--stage", "all"], verbose)),
        ("B", "攻略 docx → md", need_build(docx_sources, _newest_path(md_files), force),
         lambda: run("攻略转文档.py", [], verbose)),
        ("B", "攻略索引（guide_index.json）", need_build(docx_sources, GUIDE_INDEX, force),
         lambda: run("攻略索引.py", [], verbose)),
        ("B", "Agent 知识库（拂晓知识库_agent.json）",
         need_build(md_files + XLSX_SOURCES + [RAW_JSON, os.path.join(RAW_DIR, "talent_map.json")], AGENT_KB, force),
         lambda: run("知识库增量.py", [], verbose)),
        ("B", "攻略 QA 问答库（攻略QA库.json，调 LLM 生成问题）",
         need_build(md_files, GUIDE_QA_JSON, force),
         lambda: run("攻略问答库构建.py", [], verbose)),
        # 问答补充.txt 是人工维护的常识条目，不进 MD_FILES；由 补充问答录入.py 以
        # qa_supp_* 形式追加（攻略问答库构建 会透传保留，见其源码注释）。
        # 注意：need 全部在 steps 构造时求值（早于任何重建），所以本步判定用的是
        # 「重建前」的 mtime，不会被上一步刚刷新 QA 库而误判为「已最新」。
        ("B", "攻略 QA 库：问答补充条目（qa_supp_*，调 LLM）",
         need_build([SUPP_TXT], GUIDE_QA_JSON, force),
         lambda: run("补充问答录入.py", [], verbose)),
        ("B", "攻略 QA 向量库（攻略QA向量库.npz，调 embeddings）",
         need_build([GUIDE_QA_JSON], GUIDE_QA_NPZ, force),
         lambda: run("问答向量化.py", [], verbose)),
        # 同一份 问答补充.txt 也进「小技巧结构化集」（agent 侧 core/tips_index.py 直答用）。
        # 按 content 原文去重，重跑幂等；只有新增条目才会调 LLM。
        ("B", "小技巧集（攻略/tips.json，调 LLM 生成 title/分类/标签）",
         need_build([SUPP_TXT], TIPS_JSON, force),
         lambda: run("补充技巧录入.py", [], verbose)),
    ]


def _print_menu():
    print("\n请选择要执行哪一套（输入编号后回车）：")
    print("  1  套A · 表格增量更新底层信息")
    print("        覆盖：角色数据（raw_data / data.json / talent）")
    print("        产物：知识库/底层数据/产物/raw_data.json、data.json、talent_*.json")
    print("        （另：标准化副本.py 可手动单独生成 data.json 标准化副本）")
    print("  2  套B · 向量知识库与问答构建")
    print("        覆盖：攻略转文档 → 攻略索引 → 知识库增量 → 攻略问答库构建")
    print("              → 补充问答录入 → 问答向量化 → 补充技巧录入")
    print("        产物：攻略QA库.json / 攻略QA向量库.npz / tips.json /")
    print("              拂晓知识库_agent.json / guide_index.json")
    print("  3  全部（套A + 套B，按依赖链增量）")
    print("  l  只列出各环节是否需要更新（不执行）")
    print("  q  退出")


def _choose_suite():
    """交互选择套；非 TTY / EOF 时提示用 --suite 并退出。"""
    _print_menu()
    while True:
        try:
            choice = input("选择 [1/2/3/l/q]：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n非交互环境请加参数：--suite a|b|all（或 --list）。")
            sys.exit(2)
        if choice == "q":
            print("已取消。")
            sys.exit(0)
        if choice == "l":
            return "LIST"
        if choice in ("1", "a"):
            return "A"
        if choice in ("2", "b"):
            return "B"
        if choice in ("3", "all"):
            return "ALL"
        print("无效输入，请重新选择（1/2/3/l/q）。")


def main():
    parser = argparse.ArgumentParser(description="拂晓知识库 构建入口（增量自动构建 + 两套分区）")
    parser.add_argument("--full", action="store_true", help="强制全量重建（配合 --suite）")
    parser.add_argument("--verbose", action="store_true", help="显示完整输出")
    parser.add_argument("--list", action="store_true", help="只显示当前各环节是否需要更新")
    parser.add_argument("--suite", choices=["a", "b", "all"], help="非交互：直接指定套A/套B/全部")
    args = parser.parse_args()
    force = args.full

    if args.list:
        suite = "LIST"
    elif args.suite:
        suite = {"a": "A", "b": "B", "all": "ALL"}[args.suite]
    else:
        suite = _choose_suite()

    print("=" * 60)
    print("拂晓知识库 · 构建入口")
    print("时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if suite == "LIST":
        print("模式：仅列出环节状态")
    elif suite == "A":
        print("模式：套A · 表格增量更新底层信息")
    elif suite == "B":
        print("模式：套B · 向量知识库与问答构建")
    else:
        print("模式：全部（套A + 套B）")
    print("=" * 60)

    steps = _build_steps(force, args.verbose)

    if suite == "LIST":
        print("\n环节状态（标注所属套）：")
        for suite_tag, name, need, _fn in steps:
            print(f"  [{suite_tag}] {'需要更新' if need else '已最新':<6} {name}")
        return

    if suite == "A":
        steps = [s for s in steps if s[0] == "A"]
    elif suite == "B":
        steps = [s for s in steps if s[0] == "B"]
    # ALL → 全跑

    ok = True
    for suite_tag, name, need, fn in steps:
        print(f"\n【{name}】")
        if not need:
            print("  已是最新，跳过。")
            continue
        if not fn():
            ok = False
            print(f"  x {name} 失败，终止后续环节。")
            break

    print("\n" + "=" * 60)
    if ok:
        print("构建完成 OK")
        print("产物：")
        for label, p in [("角色数据", RAW_JSON), ("攻略索引", GUIDE_INDEX), ("Agent 知识库", AGENT_KB),
                         ("攻略QA库", GUIDE_QA_JSON), ("攻略QA向量库", GUIDE_QA_NPZ),
                         ("小技巧集 tips.json", TIPS_JSON)]:
            if os.path.exists(p):
                print(f"  {label}: {p}（{os.path.getsize(p)//1024} KB）")
        print("\n重启 AGENT 服务后自动读取最新知识库。")
    else:
        print("构建中断（见上方错误）。")
    print("=" * 60)


if __name__ == "__main__":
    main()
