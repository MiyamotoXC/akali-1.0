# -*- coding: utf-8 -*-
"""把 知识库/攻略/tips.json 渲染成可搜索、可按分类筛选的单页 HTML（只读，不改知识库）。

用法（在 阿卡莉1.0 目录，用项目 venv）：
  .venv/Scripts/python.exe 开发者入口/build/技巧概览.py
  .venv/Scripts/python.exe 开发者入口/build/技巧概览.py --out 调试/我的总览.html

产出默认为 调试/tips_总览.html（自包含：数据内嵌 + 分类筛选 + 全文搜索 + 关键词高亮）。
知识库只读——本脚本只读取 tips.json。
"""
import argparse
import collections
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
DEFAULT_SRC = os.path.join(_PROJECT_ROOT, "知识库", "攻略", "tips.json")
DEFAULT_OUT = os.path.join(_PROJECT_ROOT, "调试", "tips_总览.html")

_TPL = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tips.json 总览 · %(name)s</title>
<style>
* { box-sizing: border-box; }
body { margin:0; padding:24px 20px 60px; background:#f6f7f9; color:#1f2328;
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; font-size:14px; line-height:1.6; }
.wrap { max-width: 940px; margin: 0 auto; }
h1 { font-size:19px; font-weight:600; margin:0 0 6px; }
.sub { color:#6b7280; font-size:13px; margin-bottom:18px; }
.sub code { background:#eef0f3; padding:1px 5px; border-radius:4px; }
.bar { position: sticky; top:0; z-index:5; background:#f6f7f9; padding:10px 0 12px; margin-bottom:6px; }
#q { width:100%%; padding:10px 12px; border:1px solid #d8dce1; border-radius:8px; font-size:14px; background:#fff; }
#q:focus { outline:none; border-color:#7f77dd; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
.chip { border:1px solid #d8dce1; background:#fff; color:#3f4650; border-radius:999px;
  padding:4px 11px; font-size:12.5px; cursor:pointer; font-family:inherit; }
.chip:hover { border-color:#b9bec6; }
.chip.active { background:#534AB7; border-color:#534AB7; color:#fff; }
.chip .n { opacity:.6; margin-left:5px; font-size:11.5px; }
.card { background:#fff; border:1px solid #e5e8ec; border-radius:10px; padding:13px 15px; margin-bottom:9px; }
.top { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
.id { color:#9aa1ab; font-size:12px; font-family:ui-monospace, Consolas, monospace; }
.title { font-weight:600; font-size:14.5px; }
.cat { font-size:11.5px; color:#534AB7; background:#EEEDFE; border-radius:5px; padding:1px 7px; }
.body { margin-top:7px; color:#2f353d; }
.tags { margin-top:8px; display:flex; flex-wrap:wrap; gap:5px; }
.tag { font-size:11.5px; color:#5F5E5A; background:#F1EFE8; border-radius:5px; padding:1px 7px; }
.src { margin-top:7px; font-size:11.5px; color:#9aa1ab; }
mark { background:#FAC775; padding:0 2px; border-radius:2px; }
#empty { display:none; color:#6b7280; padding:24px 4px; }
.foot { margin-top:26px; color:#9aa1ab; font-size:12px; text-align:center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>%(name)s</h1>
  <div class="sub">version %(ver)s · 共 <b>%(total)d</b> 条 · %(ncat)d 个分类 · 来源 <code>知识库/攻略/tips.json</code></div>
  <div class="bar">
    <input id="q" type="search" placeholder="搜索标题 / 内容 / 标签（如 配装、练级、夜战）">
    <div class="chips">%(chips)s</div>
  </div>
  <div id="list"></div>
  <div id="empty">没有匹配的条目。</div>
  <div class="foot">只读渲染，未改动知识库</div>
</div>
<script>
const TIPS = %(payload)s;
const list = document.getElementById('list');
const empty = document.getElementById('empty');
const q = document.getElementById('q');
let cat = '全部';
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}
function hl(text, kw) {
  const s = esc(text);
  if (!kw) return s;
  const k = esc(kw).replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  try { return s.replace(new RegExp(k, 'gi'), function (m) { return '<mark>' + m + '</mark>'; }); }
  catch (e) { return s; }
}
function render() {
  const kw = q.value.trim();
  const low = kw.toLowerCase();
  let shown = 0;
  const html = TIPS.map(function (t) {
    const okCat = (cat === '全部') || (t.category === cat);
    const blob = ((t.title || '') + ' ' + (t.content || '') + ' ' + (t.tags || []).join(' ')).toLowerCase();
    const okKw = !low || blob.indexOf(low) >= 0;
    if (!okCat || !okKw) return '';
    shown++;
    const tags = (t.tags || []).map(function (x) { return '<span class="tag">' + esc(x) + '</span>'; }).join('');
    return '<div class="card">'
      + '<div class="top"><span class="id">' + esc(t.id) + '</span>'
      + '<span class="title">' + hl(t.title, kw) + '</span>'
      + '<span class="cat">' + esc(t.category) + '</span></div>'
      + '<div class="body">' + hl(t.content, kw) + '</div>'
      + (tags ? '<div class="tags">' + tags + '</div>' : '')
      + (t.source ? '<div class="src">来源：' + esc(t.source) + '</div>' : '')
      + '</div>';
  }).join('');
  list.innerHTML = html;
  empty.style.display = shown ? 'none' : 'block';
}
document.querySelectorAll('.chip').forEach(function (c) {
  c.addEventListener('click', function () {
    document.querySelectorAll('.chip').forEach(function (x) { x.classList.remove('active'); });
    c.classList.add('active');
    cat = c.dataset.cat;
    render();
  });
});
q.addEventListener('input', render);
render();
</script>
</body>
</html>
'''

_CHIP = '<button type="button" class="chip{c}" data-cat="{v}">{v}<span class="n">{n}</span></button>'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    with open(args.src, encoding="utf-8") as f:
        d = json.load(f)
    tips = d.get("tips") or []
    counts = collections.Counter(t.get("category") for t in tips)
    order = list(d.get("categories") or [])
    order += [c for c in dict.fromkeys(t.get("category") for t in tips) if c not in order]

    chips = [_CHIP.format(c=" active", v="全部", n=len(tips))]
    chips += [_CHIP.format(c="", v=c, n=counts.get(c, 0)) for c in order if counts.get(c)]

    html = _TPL % {
        "name": d.get("name") or "tips",
        "ver": d.get("version") or "",
        "total": len(tips),
        "ncat": len([c for c in order if counts.get(c)]),
        "chips": "".join(chips),
        "payload": json.dumps(tips, ensure_ascii=False),
    }
    out = args.out if os.path.isabs(args.out) else os.path.join(_PROJECT_ROOT, args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("written: %s | bytes = %d | tips = %d | 分类 = %d"
          % (out, os.path.getsize(out), len(tips), len(chips) - 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
