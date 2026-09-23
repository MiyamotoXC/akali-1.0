# -*- coding: utf-8 -*-
"""把 知识库/问答补充.txt 的常识条目补充进 知识库/攻略/tips.json（小技巧集）。

与 补充问答录入.py 的关系
--------------------------------
- 补充问答录入.py 把同一份 txt 进「攻略 QA 向量库」（question/alt_questions/keywords + 嵌入）；
- 本脚本把它进「小技巧结构化集」tips.json —— tips 是**结论式短条目**，供前端/路由**原文直出**，
  不走向量、不调 LLM 作答，因此这里只生成 title/category/tags 三个索引字段。

流程
----
  1. 解析 txt（每行 `编号.内容`）→ N 条；
  2. 调 LLM 为每条生成 title(≤12字) / category / tags(2~5)；
     content **保持原文逐字**（防编造，且保证重跑幂等：按 content 去重即可跳过已入库条目）；
  3. 备份 tips.json 后按 content 去重追加，id 从现有最大号续排（t101、t102…）；
  4. 更新 version / updated / source / categories。

用法（在 阿卡莉1.0 目录，用项目 venv）：
  .venv/Scripts/python.exe 开发者入口/build/补充技巧录入.py
  .venv/Scripts/python.exe 开发者入口/build/补充技巧录入.py --dry-run   # 只看结果不写盘
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))
from src.模型客户端 import chat_json  # noqa: E402

_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
KB_DIR = os.path.join(_PROJECT_ROOT, "知识库")
TIPS_PATH = os.path.join(KB_DIR, "攻略", "tips.json")
TXT_PATH = os.path.join(KB_DIR, "问答补充.txt")
SUPP_SOURCE = "问答补充.txt"

# 既有 11 类 + 新增「系统与界面」（问答补充里多为港区/设置/界面操作，归入既有类会失真）
_CAT_DESC = [
    ("萌新入门", "入坑选区、开局路线、新手培养与舰灵获取"),
    ("练级", "练级点、经验与好感机制、油耗、老板打手循环"),
    ("推图与活动", "主线推图、活动玩法与加成、关卡机制"),
    ("配队与阵型", "阵容搭配、阵型选择、舰种分工与羁绊"),
    ("装备", "装备选择与搭配、配装推荐"),
    ("数值机制", "公式与乘区、暴击、甲弹克制、强化改造、装填阈值"),
    ("遭遇战", "遭遇战玩法与 BOSS 冲分"),
    ("PVP竞技场", "争锋竞技场"),
    ("无尽海域", "无尽海域"),
    ("拉古兹BOSS", "拉古兹 BOSS 应对"),
    ("资源与日常", "资源获取与规划、日常流程、商店与礼包"),
    ("系统与界面", "港区操作、设置项、UI 功能、界面按钮、账号类"),
]
NEW_CATEGORY = "系统与界面"
ALLOWED = [c for c, _ in _CAT_DESC]
_CAT_BLOCK = "\n".join("   - %s：%s" % (c, d) for c, d in _CAT_DESC)

_COMMON_SYSTEM = (
    "你是手游《拂晓胜利之刻》知识库的条目整理器。给定一条游戏常识陈述，输出它的结构化索引字段。\n"
    "1) title：名词短语，≤12 个汉字，概括该条讲的是什么（如「温泉搓澡互动」「钻石用途」「天赋解锁条件」）；"
    "不要写成问句，不要带编号。\n"
    "2) category：从下列固定分类中选**最贴切的一个**，只能选一个：\n"
    + _CAT_BLOCK + "\n"
    "3) tags：2~5 个检索关键词，含系统名/玩法名/资源名/界面名（如 温泉、设置、钻石、好感度、改造）。\n"
    "【严禁】不要改写、补充或推断陈述里没有的事实；不要输出 content 字段。\n"
    '只输出 JSON：{"title":"...","category":"...","tags":["...","..."]}'
)


def _norm_content(s: str) -> str:
    """内容归一（去空白与标点）用于去重比对。"""
    return re.sub(r"[\s，。！？、；：,.!?;:~～·\-—_（）()\[\]【】《》\"'“”‘’]", "", s or "")


def _next_id_num(tips) -> int:
    nums = []
    for t in tips:
        m = re.match(r"^t(\d+)$", str(t.get("id") or ""))
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _gen(item):
    """为单条常识生成 (title, category, tags)，失败退化为标题=正文前 12 字。"""
    num, content = item
    for attempt in range(3):
        try:
            d = chat_json(_COMMON_SYSTEM, "常识陈述：%s" % content, temperature=0.2)
            title = str(d.get("title") or "").strip().strip("《》")
            cat = str(d.get("category") or "").strip()
            tags = [str(x).strip() for x in (d.get("tags") or []) if str(x).strip()]
            if cat not in ALLOWED:
                cat = NEW_CATEGORY
            if not title:
                title = content[:12]
            return title[:16], cat, tags[:5]
        except Exception as e:
            print("  [生成重试 %d] 条目%d: %s" % (attempt + 1, num, str(e)[:70]))
            time.sleep(2 * (attempt + 1))
    return content[:12], NEW_CATEGORY, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印将要新增的条目，不写盘")
    args = ap.parse_args()

    # ---------- 1. 解析 txt ----------
    if not os.path.exists(TXT_PATH):
        print("缺少补充文件: %s" % TXT_PATH)
        return 1
    raw = open(TXT_PATH, encoding="utf-8").read()
    items = []
    for ln in raw.splitlines():
        # 编号后的点号可能缺失（如「15天赋解锁…」），故点号可选
        m = re.match(r"^\s*(\d+)\s*[.．、。]?\s*(\S.*)$", ln)
        if m:
            items.append((int(m.group(1)), m.group(2).strip()))
    print("[解析] 问答补充.txt -> %d 条" % len(items))

    # ---------- 2. 读现有 tips ----------
    data = json.load(open(TIPS_PATH, encoding="utf-8"))
    tips = data["tips"]
    seen = {_norm_content(t.get("content")) for t in tips}
    print("[现状] tips.json -> %d 条，分类 %d 个" % (len(tips), len(set(t.get("category") for t in tips))))

    todo = [(n, c) for n, c in items if _norm_content(c) not in seen]
    skipped = len(items) - len(todo)
    if skipped:
        print("[去重] 已在 tips 中，跳过 %d 条" % skipped)
    if not todo:
        print("无新增，结束。")
        return 0

    # ---------- 3. LLM 生成 title/category/tags ----------
    with ThreadPoolExecutor(max_workers=4) as ex:
        metas = list(ex.map(_gen, todo))
    ok = sum(1 for m in metas if m[2])
    print("[生成] 成功 %d/%d（含 tags）；失败退化为正文前 12 字" % (ok, len(metas)))

    # ---------- 4. 续号构造并追加 ----------
    start = _next_id_num(tips)
    new_tips = []
    for i, ((num, content), (title, cat, tags)) in enumerate(zip(todo, metas)):
        new_tips.append({
            "id": "t%03d" % (start + i),
            "category": cat,
            "title": title,
            "content": content,
            "source": SUPP_SOURCE,
            "tags": tags or [title],
        })

    print("\n--- 将新增 %d 条 ---" % len(new_tips))
    for t in new_tips:
        print("  %s [%s] %s | tags=%s" % (t["id"], t["category"], t["title"], "/".join(t["tags"])))

    if args.dry_run:
        print("\n[dry-run] 未写盘。")
        return 0

    # ---------- 5. 备份并写回 ----------
    ts = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TIPS_PATH, TIPS_PATH + ".bak-%s" % ts)
    tips.extend(new_tips)
    data["tips"] = tips
    data["updated"] = time.strftime("%Y-%m-%d")
    data["version"] = "1.1"
    srcs = data.get("source")
    if isinstance(srcs, list):
        if SUPP_SOURCE not in srcs:
            srcs.append(SUPP_SOURCE)
    elif isinstance(srcs, str):
        data["source"] = [srcs, SUPP_SOURCE] if SUPP_SOURCE not in srcs else srcs
    cats = list(data.get("categories") or [])
    for c in [t["category"] for t in new_tips]:
        if c not in cats:
            cats.append(c)
    data["categories"] = cats

    json.dump(data, open(TIPS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n[写回] %s" % TIPS_PATH)
    print("  tips: %d -> %d | 分类 %d 个 | version %s | 备份 %s"
          % (len(tips) - len(new_tips), len(tips), len(cats), data["version"], ts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
