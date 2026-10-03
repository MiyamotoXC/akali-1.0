# -*- coding: utf-8 -*-
"""knowledge tools."""
import os
import re
import json
import datetime
from qwen_agent.tools.base import BaseTool
from .common import (
    EQUIPMENT_FILE, _load_equipment_db, _norm_type, _format_equipment,
    GUIDE_DIR, GUIDE_INDEX_FILE, GUIDE_KB_FILE, KB_DIR, _load_guide_index, _load_guide_kb,
    _clean_guide_keyword, _fetch_weather,
    ACTIVITY_CALENDAR_XLSX_FILE, ACTIVITY_CALENDAR_SHEET, HOLIDAY_CALENDAR_FILE,
)

# ======================================================================
# 工具 14：装备查询（知识库/装备.xlsx）
# ======================================================================

class QueryEquipment(BaseTool):
    """【装备】查询知识库装备表（紫/金/彩 装备）。可按装备名关键词、装备类型（如 驅逐炮/战列炮/雷击）、
    稀有度（紫/金/彩）筛选。"""
    name = 'query_equipment'
    description = '【装备】查询知识库装备表（仅紫/金/彩）。参数：keyword（装备名关键词）、eq_type（装备类型，如 驅逐炮/輕巡炮/重巡炮/戰列炮/艦載機/魚雷管/設備/防空炮，简体繁体均可）、rarity（稀有度：紫/金/彩）、max_results（返回条数，默认8）。返回装备名称、类型、所属、稀有度与属性 Init/Final。'
    parameters = {
        'type': 'object',
        'properties': {
            'keyword': {'type': 'string', 'description': '装备名关键词（子串匹配，可空）'},
            'eq_type': {'type': 'string', 'description': '装备类型筛选（如 驅逐炮/轻巡炮/重巡炮/战列炮/雷击/鱼雷/轰炸机/高平炮，可空）'},
            'rarity': {'type': 'string', 'description': '稀有度筛选（紫/金/彩，可空）'},
            'max_results': {'type': 'integer', 'description': '最多返回条数，默认 8', 'default': 8},
        },
        'required': [],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        keyword = (params.get('keyword') or '').strip()
        eq_type = (params.get('eq_type') or '').strip()
        rarity = (params.get('rarity') or '').strip()
        try:
            max_results = int(params.get('max_results') or 8)
        except (ValueError, TypeError):
            max_results = 8
        max_results = max(1, min(max_results, 20))

        db = _load_equipment_db()
        if not db:
            return f"未找到装备数据文件: {EQUIPMENT_FILE}。"

        matched = []
        for eq in db:
            if keyword and keyword not in eq["name"]:
                continue
            if eq_type and _norm_type(eq_type) not in _norm_type(eq["eq_type"]):
                continue
            if rarity and eq["rarity"] != rarity:
                continue
            matched.append(eq)
            if len(matched) >= max_results:
                break

        if not matched:
            conds = [c for c in (f"关键词={keyword}" if keyword else "",
                                 f"類型={eq_type}" if eq_type else "",
                                 f"稀有={rarity}" if rarity else "") if c]
            return f"未找到符合条件的装备（{' '.join(conds) or '无筛选条件'}）。装备类型示例：驅逐炮/轻巡炮/重巡炮/战列炮/雷击/鱼雷/轰炸机。"

        lines = [f"装备查询结果（共 {len(matched)}/{len(db)} 条展示前 {len(matched)} 条）:"]
        for eq in matched:
            lines.append("")
            lines.append(_format_equipment(eq))
        lines.append("")
        lines.append(f"数据源: {EQUIPMENT_FILE}")
        return "\n".join(lines)

# 攻略四大类（用户口径 2026-09-22：窗口就是四类，工具口径必须与其一致）。
# 类目与归属**不在这里硬编码**：直接取 guide_index.json 的 category 字段
# （与前端攻略库面板同一数据源、同一 tab 顺序），避免"窗口四类、工具三类"两套口径。
_CATEGORY_ORDER = ['萌新入坑', '一图速查', '练级须知', '进阶必备']
# 兜底：guide_index 未收录该篇时，用旧的三类 id 映射折算到四类
_GUIDE_CATEGORY_ADV = {'D20', 'D21', 'D22', 'D23', 'D25'}          # 进阶：争锋竞技场/遭遇战/无尽/伤害计算/拉古兹
_GUIDE_CATEGORY_CHART = {'D06', 'D08', 'D12', 'D13', 'D14a', 'D14b', 'D14c', 'D15'}  # 知识图表：速查/数值/装填/问答/教程
_LEGACY_CAT_MAP = {'新人相关': '萌新入坑', '知识图表': '一图速查', '进阶': '进阶必备'}


def _legacy_category_of(doc_id: str) -> str:
    """旧三类 id 映射：进阶 / 知识图表 / 新人相关（仅供兜底）。"""
    if doc_id in _GUIDE_CATEGORY_ADV:
        return '进阶'
    if doc_id in _GUIDE_CATEGORY_CHART:
        return '知识图表'
    return '新人相关'


def _guide_cat_maps() -> tuple:
    """返回 (path→类, title→类)。数据源 guide_index.json，与前端窗口同源。"""
    by_path, by_title = {}, {}
    try:
        from .common import _load_guide_index
        for g in (_load_guide_index() or []):
            cat = (g.get('category') or '').strip()
            if not cat:
                continue
            p = (g.get('path') or '').strip()
            if p:
                by_path[os.path.normpath(p)] = cat
            t = (g.get('title') or '').strip()
            if t:
                by_title[t] = cat
    except Exception:
        pass
    return by_path, by_title


def _category_of(doc, maps=None) -> str:
    """文档所属大类（四类，与窗口一致）：file 路径 → 标题 → 旧三类折算兜底。"""
    if isinstance(doc, str):          # 兼容只传 doc_id 的旧调用
        doc = {'id': doc}
    by_path, by_title = maps if maps else _guide_cat_maps()
    p = (doc.get('file') or '').strip()
    if p:
        c = by_path.get(os.path.normpath(p))
        if c:
            return c
    t = (doc.get('title') or '').strip()
    if t and t in by_title:
        return by_title[t]
    return _LEGACY_CAT_MAP.get(_legacy_category_of(doc.get('id', '')), '一图速查')


def _group_by_category(docs: list) -> dict:
    """把文档列表按四大类分组，返回 {大类: [docs, ...]}（按窗口 tab 顺序，未知类排最后）。"""
    maps = _guide_cat_maps()
    groups = {}
    for d in docs:
        groups.setdefault(_category_of(d, maps), []).append(d)
    out = {c: groups[c] for c in _CATEGORY_ORDER if c in groups}
    for c, v in groups.items():
        if c not in out:
            out[c] = v
    return out


# ======================================================================
# 攻略篇名精确匹配：输入能唯一对应某篇攻略时直接给那一篇（不走主题路由/多篇列表）
# ======================================================================

# 口语/简称 → 文档标题（别名表；精确匹配优先于 topic_routing / query_patterns）
_GUIDE_TITLE_ALIASES = {
    '装备一图流': '装备速查表',
    '装备速查': '装备速查表',
    '配装推荐': '萌新入门配装推荐',
    '一键装备': '一键装备设置流程',
    '一键卸装': '一键装备设置流程',
    '预设装备': '一键装备设置流程',
    'pvp天赋': 'PVP天赋表',
    'pvp天赋表': 'PVP天赋表',
    '改造优先级': '舰灵改造优先级',
    '练级省流': '练级百科省流版',
    '省流版': '练级百科省流版',
    '剧情榜': '拂晓剧情榜',
    '面板公式': '舰灵面板数值计算公式',
    '数值公式': '舰灵面板数值计算公式',
    '装填数值': '主副炮循环装填数值表',
    '主副炮': '主副炮循环装填数值表',
    '咨询问答': '咨询问答表P1',
    '性能优化': '性能优化小开关教程',
    '思维导图': '新手入门思维导图',
    '新手导图': '新手入门思维导图',
    '舰灵推荐': '新人向PVE舰灵推荐表',
    '活动推图': '给萌新的活动推图攻略食用指南',
    '配队思路': '给萌新的PVE配队思路',
    'pve配队': '给萌新的PVE配队思路',
    '竞技场百科': '争锋竞技场简易百科',
    '争锋百科': '争锋竞技场简易百科',
    '遭遇战攻略': '遭遇战攻略',
    '无尽海域': '无尽海域配队思路',
    '伤害构成': '拂晓伤害计算构成',
    '伤害计算构成': '拂晓伤害计算构成',
    '普攻倍率': '拂晓舰灵普攻倍率cd',
    '拉古兹专题': '拉古兹专题',
}


def _clean_guide_query(msg: str) -> str:
    """清洗攻略查询输入：去首尾空白/标点、去常见口语尾词（攻略/大全/的/啊/呢/吧/嘛 等）。"""
    s = (msg or '').strip().strip('？?！!。，,、~～ ')
    for tail in ('攻略', '大全', '的', '啊', '呢', '吧', '嘛', '呀', '啦', '哦', '喔'):
        if s.endswith(tail) and len(s) > len(tail) + 1:
            s = s[:-len(tail)].strip()
    return s


def _exact_guide_doc(msg: str, docs: list):
    """精确篇名匹配：输入能唯一对应某篇攻略 → 返回该 doc；否则返回 None。
    顺序：①别名表命中（如 装备一图流→装备速查表）→ ②title 完全相等 →
    ③title 完整出现在输入中（如"给我装备速查表"）→ ④输入完整出现在 title 中。
    命中后必须恰好唯一 1 篇才返回（多篇命中视为不精确，回落主题路由）。"""
    if not msg or not docs:
        return None
    clean = _clean_guide_query(msg)
    if len(clean) < 2:
        return None
    # ① 别名表（先清口语尾词再查，兼容"装备一图流攻略"）
    raw_strip = msg.strip().strip('？?！!。，,、~～ ')
    alias_title = _GUIDE_TITLE_ALIASES.get(clean) or _GUIDE_TITLE_ALIASES.get(raw_strip)
    if alias_title:
        hits = [d for d in docs if (d.get('title') or '').strip() == alias_title]
        if len(hits) == 1:
            return hits[0]
    # ②③④ 标题匹配（要求 title/输入长度 >= 4，避免「装备」「速查」这类宽泛词误配单篇）
    matched = []
    for d in docs:
        title = (d.get('title') or '').strip()
        if len(title) < 4:
            continue
        if title == clean:
            matched.append(d)
        elif len(clean) >= 4 and title in clean:
            matched.append(d)
        elif len(clean) >= 4 and clean in title:
            matched.append(d)
    return matched[0] if len(matched) == 1 else None


def _single_guide_lines(doc: dict) -> str:
    """精确命中单篇 → 只允许输出经原文人工核验并显式标记的摘要。"""
    title = (doc.get('title') or '').strip()
    lines = [f"已为你定位《{title}》"]
    if doc.get('summary_verified') is True:
        summary = (doc.get('summary') or '').strip()
        if summary:
            lines.append(f"  {summary}")
    fl = _guide_file_lines([doc])
    if fl:
        lines.append("")
        lines.extend(fl)
    lines.append(f"<<GUIDE_LIB>>{title}")
    return "\n".join(lines)


class QueryGuide(BaseTool):
    """【攻略】按类型从索引库定位攻略文档。不带 keyword 时只给四大类概览（不列全目录）；带 keyword 时按
    标题/标签匹配。**只做定位，不读取、不转述任何正文**：查询到攻略时输出 <<GUIDE_LIB>> 标记
    （可带聚焦词，如 <<GUIDE_LIB>>遭遇战），前端会打开攻略库面板由用户直接查看文档内容。"""
    name = 'query_guide'
    description = '【攻略】查询拂晓攻略知识库（拂晓攻略知识库_精简版.json）。参数：keyword（主题关键词，如 新人/萌新/遭遇战/练级/配装/装备/PVE/伤害计算/拉古兹/竞技场/无尽；可空则只给四大类概览，不列全目录）。泛化询问舰灵改造优先级、哪些舰灵值得优先改造或要求总结改造原图时，keyword 必须用“舰灵改造优先级”。笼统说法（如"新人"）返回该主题全部文档（大全）；指名具体话题定位原文档，仅可附带已标记为人工核验的原文摘要，不得自行补充。查询到攻略时在回复末尾输出 <<GUIDE_LIB>>聚焦关键词标记，前端自动打开攻略库面板。'
    parameters = {
        'type': 'object',
        'properties': {
            'keyword': {'type': 'string', 'description': '攻略类型/主题关键词，如 新人/萌新/遭遇战/练级/配装/装备/PVE/伤害计算/拉古兹/竞技场/无尽（可空则只给四大类概览，不列全目录）'},
        },
        'required': [],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        keyword = (params.get('keyword') or '').strip()

        kb = _load_guide_kb()
        if not kb:
            return f"未找到攻略知识库: {GUIDE_KB_FILE}（请用 FX数据管理工具 生成）。"

        docs = kb.get('documents', [])
        topic_routing = kb.get('topic_routing', {})
        query_patterns = kb.get('query_patterns', [])

        # 与实时文档对应：只保留本地文件真实存在的攻略（file 字段非空且文件存在），
        # 被删除/未同步文件路径的文档不输出（避免目录与面板出现打不开的条目）
        def _file_exists(doc) -> bool:
            f = (doc.get('file') or '').strip()
            if not f:
                return False
            p = os.path.normpath(os.path.join(KB_DIR, f))
            return os.path.isfile(p)
        docs = [d for d in docs if _file_exists(d)]
        valid_ids = {d['id'] for d in docs}
        topic_routing = {t: [i for i in ids if i in valid_ids] for t, ids in topic_routing.items()}
        topic_routing = {t: ids for t, ids in topic_routing.items() if ids}
        query_patterns = [qp for qp in query_patterns if any(i in valid_ids for i in qp.get('docs', []))]

        # 笼统说法归一化：新人/新手 → 萌新（给萌新入门大全）
        # 包含 topic_routing 的键 '萌新入门'，避免被精确篇名匹配误命中
        if keyword in ('新人', '新手', '萌新', '入坑', '刚玩', '开局', '萌新入门'):
            keyword = '萌新'

        # 0) 精确篇名匹配：输入能唯一对应某篇攻略 → 直接给那一篇（优先于主题路由/多篇列表）
        exact = _exact_guide_doc(keyword, docs)
        if exact is not None:
            text = _single_guide_lines(exact)
            return text

        if not keyword:
            # 全部攻略：只给「四大类 + 各类篇数」概览，不逐条贴全目录
            # （用户口径 2026-09-22：查攻略不要输出全部的目录名字——前端滑块面板
            #  已按分类展示全部攻略，工具再贴 25 行只是刷屏；要点名看哪篇就带类型/篇名）
            groups = _group_by_category(docs)
            lines = [f"拂晓攻略知识库（共 {len(docs)} 篇，{len(groups)} 类）："]
            for cat, gdocs in groups.items():
                lines.append(f"【{cat}】{len(gdocs)} 篇")
            lines.append("输入攻略类型（如「新人」「练级」「遭遇战」「拉古兹」）可查看对应文档；也可点击下方目录查看详情。")
            lines.append("<<GUIDE_LIB>>")
            return "\n".join(lines)

        # 1) topic_routing 主题名匹配（优先：主题名精准命中）
        matched_docs = None
        for topic, ids in topic_routing.items():
            if topic in keyword or keyword in topic:
                matched_docs = [d for d in docs if d['id'] in ids]
                break
        # 2) query_patterns 正则匹配（按「命中词最长优先」：如"争锋竞技场"胜过宽泛"竞技场"）
        if matched_docs is None:
            import re as _re
            best_len, best_docs = 0, None
            for qp in query_patterns:
                pat = qp.get('pattern', '')
                if not pat:
                    continue
                # keyword 命中 pattern 词，或 pattern 词出现在 keyword 中
                words = [w.strip() for w in _re.split(r'\|', pat) if w.strip()]
                hit = _re.search(pat, keyword)
                if hit is None:
                    for w in words:
                        if w and w in keyword:
                            hit = w
                            break
                if hit:
                    hit_len = len(hit.group(0)) if hasattr(hit, 'group') else len(hit)
                    if hit_len > best_len:
                        best_len = hit_len
                        best_docs = [d for d in docs if d['id'] in qp.get('docs', [])]
            matched_docs = best_docs
        # 3) 文档 title 子串匹配（指名具体文档）
        if matched_docs is None:
            kw_low = keyword.lower()
            matched_docs = [d for d in docs if kw_low in d['title'].lower()]
        # 4) summary 全文兜底匹配
        if matched_docs is None:
            kw_low = keyword.lower()
            matched_docs = [d for d in docs if kw_low in (d.get('summary') or '').lower()]

        if not matched_docs:
            return f"未找到与「{keyword}」相关的攻略。可用主题：萌新入门、练级攻略、装备系统、阵容阵型、PVP天赋/争锋、舰灵改造、活动、PVE配队、遭遇战、无尽海域、数值机制、伤害计算、拉古兹等；输入「攻略」可查看全部清单。"


        # 先指引所属大类，再给细分：目录标注所属大类（四类，与窗口一致）
        _maps = _guide_cat_maps()
        cats = sorted({_category_of(g, _maps) for g in matched_docs},
                      key=lambda c: _CATEGORY_ORDER.index(c) if c in _CATEGORY_ORDER else len(_CATEGORY_ORDER))
        cat_note = "【" + "/".join(cats) + "】" if cats else ""
        lines = [f"攻略「{keyword}」对应文档（{len(matched_docs)} 篇）{cat_note}："]
        # 指定类型的文档可以列出，但不加序号（用户口径 2026-09-22：不要序号）
        for g in matched_docs:
            lines.append(f"  ·《{g['title']}》")
        # 对应可下载文件：按标题从 guide_index 匹配路径，输出 <<GUIDE_FILE>> 标记（前端渲染目录卡片）
        file_lines = _guide_file_lines(matched_docs)
        if file_lines:
            lines.append("")
            lines.extend(file_lines)
        # 触发前端攻略库面板（聚焦关键词），正文/下载由前端面板提供
        lines.append(f"<<GUIDE_LIB>>{keyword}")
        return "\n".join(lines)


def _guide_file_lines(docs: list) -> list:
    """为匹配的攻略文档生成 <<GUIDE_FILE>> 标记行。
    优先使用知识库 JSON 已固化的 file 字段（数据管理工具 build_guide_index.py 同步）；
    缺失时按标题从 guide_index 兜底匹配。"""
    title_to_path = {}
    try:
        from .common import _load_guide_index
        index = _load_guide_index() or []
        for g in index:
            t = g.get('title', '').strip()
            p = g.get('path', '').strip()
            if t and p:
                title_to_path.setdefault(t, p)
    except Exception:
        pass
    lines = []
    for d in docs:
        title = d.get('title', '').strip()
        path = (d.get('file') or '').strip()
        if not path:
            path = title_to_path.get(title, '')
        if not path:
            for t, p in title_to_path.items():
                if title and (title in t or t in title):
                    path = p
                    break
        if path:
            lines.append(f"<<GUIDE_FILE>>{path}|{title}")
    return lines

# ======================================================================
# 工具：活动咨询（游戏内活动 = 知识库/活动/活动日历.xlsx，实时读取；只放游戏内活动）
#   真实日历（法定节假日）另走 holiday_cn + 知识库/活动/节假日.json，与游戏内活动严格区分
# ======================================================================

def _cell_to_plain(v):
    """归一化单元格值：datetime→date（去掉 00:00 时分，避免 isoformat 出现 T00:00:00），
    字符串去空白，其余原样返回。"""
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, str):
        return v.strip()
    return v


def _calendar_from_xlsx():
    """实时读取活动日历表格 → {'events': [...]}；表格不存在或解析失败返回 None。

    表格首列为序号、其后为字段列；表头行取「前 10 行里第一个含『活动名称/名称』的行」，
    表头文字即 event 字典的键，交给 _norm_event 做中英文键与自由文本时间的智能归一化。
    空行、缺活动名的行自动跳过。每次都重新读盘，编辑表格即时生效。"""
    if not os.path.isfile(ACTIVITY_CALENDAR_XLSX_FILE):
        return None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(ACTIVITY_CALENDAR_XLSX_FILE, data_only=True, read_only=True)
        try:
            ws = wb[ACTIVITY_CALENDAR_SHEET] if ACTIVITY_CALENDAR_SHEET in wb.sheetnames else (
                wb[wb.sheetnames[0]] if wb.sheetnames else None)
            if ws is None:
                return None
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    except Exception:
        return None
    if not rows:
        return None
    # 定位表头行（容忍标题行在前）
    header_idx, headers = None, []
    for i, row in enumerate(rows[:10]):
        cells = [str(c).strip() if c is not None else '' for c in row]
        if any(c in ('活动名称', '名称', '活动名', 'name') for c in cells):
            header_idx, headers = i, cells
            break
    if header_idx is None:
        return None
    events = []
    for row in rows[header_idx + 1:]:
        ev = {}
        for h, v in zip(headers, row):
            if not h or v is None:
                continue
            v = _cell_to_plain(v)
            if v == '':
                continue
            ev[h] = v
        # 必须有活动名才算一条有效活动（空行 / 说明行自动跳过）
        if any(str(ev.get(k) or '').strip() for k in ('活动名称', '名称', '活动名', 'name')):
            events.append(ev)
    return {'events': events,
            'updated': '表格源：知识库/活动/活动日历.xlsx（实时读取）'}


def _load_activity_calendar() -> dict:
    """读取**游戏内活动**日历：唯一数据源 = 表格 知识库/活动/活动日历.xlsx（实时读盘）。

    本表**只放游戏内活动**；法定节假日属「真实日历」，见 load_holiday_events()。
    表格缺失 / 不可解析时返回 {}（不再回退任何 JSON）。"""
    return _calendar_from_xlsx() or {}


def _parse_event_date(s):
    """解析日期。接受 datetime.date 对象或字符串（2026-08-01 / 2026/8/1 / 2026.8.1 / 2026年8月1日），
    失败返回 None。"""
    if isinstance(s, datetime.date):
        return s
    s = (s or '').strip()
    if not s:
        return None
    m = re.search(r'(\d{4})\s*[./\-年]\s*(\d{1,2})\s*[./\-月]\s*(\d{1,2})\s*日?', s)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except (ValueError, TypeError):
        return None


# 日期 token：可带 4 位年份，也支持「9.25」「9月25日」这类缺年写法
_EVENT_TOKEN_RE = re.compile(
    r'(?:(\d{4})\s*[./\-年]\s*)?(\d{1,2})\s*[./\-月]\s*(\d{1,2})\s*日?')


def _parse_date_range(text, base_year=None):
    """从自由文本识别「起止日期」。支持的写法（含混合）：
      「2026-09-25 ~ 2026-10-08」「2026.09.25-2026.10.08」「2026年9月25日至10月8日」
      「9.25-10.8」「09/25至10/08」「9月25日 - 10月8日」「9.25」（单点=起止同日）
    返回 (start_date, end_date)。缺年自动补 base_year（默认今年）；仅末段缺年且月份回退时自动跨年 +1。
    解析不到返回 (None, None)。"""
    s = str(text or '').strip()
    if not s:
        return None, None
    # 先剔除时间部分（HH:MM / HH:MM:SS），避免「10:00」里的数字被误当月份/日期
    s = re.sub(r'\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?', ' ', s)
    base = base_year or datetime.date.today().year
    found = []
    for m in _EVENT_TOKEN_RE.finditer(s):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        found.append((int(y) if y else None, mo, d))
        if len(found) >= 2:
            break
    if not found:
        return None, None
    y0, m0, d0 = found[0]
    start_year = y0 or base
    try:
        start = datetime.date(start_year, m0, d0)
    except ValueError:
        try:
            start = datetime.date(start_year, m0, min(d0, 28))
        except ValueError:
            return None, None
    if len(found) == 1:
        return start, start
    y1, m1, d1 = found[1]
    end_year = y1 or start_year
    if y1 is None and (m1, d1) < (m0, d0):
        end_year = start_year + 1      # 跨年，如 12.20-1.5
    try:
        end = datetime.date(end_year, m1, d1)
    except ValueError:
        end = start
    return start, end


# ---- 截止时刻口径（用户口径 2026-09-14）----
# 游戏内活动的「活动截止」「兑换截止」统一为截止日当天上午 10:00；
# 提醒措辞、剩余天数、状态相位都须带上这个时刻，避免用户按「当天结束」理解而超时。
DEADLINE_TIME = datetime.time(10, 0)
DEADLINE_TIME_LABEL = '上午10:00'

# ---- 字段智能识别：中文键 / 英文键 / 自由文本 ----
_NORM_FLAG = '_normalized'
# (活动开始, 活动结束, 兑换开始, 兑换结束) 的键候选；顺序即优先级
_EV_START_KEYS = ('start', '开始', '活动开始', '开始时间', '起始')
_EV_END_KEYS = ('end', '活动结束', '活动截止', '结束', '截止', '结束时间', '截止时间')
_EX_START_KEYS = ('exchange_start', 'ex_start', '兑换开始', '兑换起始', '兑换开始时间')
_EX_END_KEYS = ('exchange_end', 'ex_end', '兑换结束', '兑换截止', '兑换截止时间')
_EV_RANGE_KEYS = ('活动时间', '活动日期', 'activity_time', 'date_range', '时间')
_EX_RANGE_KEYS = ('兑换时间', '兑换日期', 'exchange_time', 'exchange_range')


def _first_val(ev, keys):
    """按候选键顺序取第一个非空值（字符串去空白）。"""
    for k in keys:
        v = ev.get(k)
        if isinstance(v, str):
            v = v.strip()
            if v:
                return v
        elif v is not None:
            return v
    return ''


def _norm_event(ev):
    """把活动日历的一条 event 归一化为统一结构（幂等，已归一化则原样返回）。

    智能识别：兼容英文键（name/type/start/end/summary/notes/greeting/guide）与
    中文键（名称/类型/活动时间/兑换时间/简介/注意事项/祝福/攻略），且「活动时间」
    「兑换时间」支持自由文本区间（如「9.25-10.8」「2026-09-25 ~ 2026-10-08」）。
    归一化后：start/end = 活动期，ex_start/ex_end = 兑换期，均为 datetime.date 或 None。"""
    if not isinstance(ev, dict):
        return {}
    if ev.get(_NORM_FLAG):
        return ev
    year = datetime.date.today().year

    start = _parse_event_date(_first_val(ev, _EV_START_KEYS))
    end = _parse_event_date(_first_val(ev, _EV_END_KEYS))
    if not (start and end):
        for k in _EV_RANGE_KEYS:
            raw = _first_val(ev, (k,))
            if raw:
                rs, re_ = _parse_date_range(raw, year)
                start = start or rs
                end = end or re_
                if start and end:
                    break

    ex_start = _parse_event_date(_first_val(ev, _EX_START_KEYS))
    ex_end = _parse_event_date(_first_val(ev, _EX_END_KEYS))
    if not (ex_start and ex_end):
        for k in _EX_RANGE_KEYS:
            raw = _first_val(ev, (k,))
            if raw:
                rs, re_ = _parse_date_range(raw, year)
                ex_start = ex_start or rs
                ex_end = ex_end or re_
                if ex_start and ex_end:
                    break
    # 兑换期只写一端时向另一端补齐（单日兑换）
    if ex_start and not ex_end:
        ex_end = ex_start
    elif ex_end and not ex_start:
        ex_start = ex_end

    return {
        _NORM_FLAG: True,
        'name': str(_first_val(ev, ('name', '名称', '活动名称', '活动名')) or '').strip(),
        'type': str(_first_val(ev, ('type', '类型', '活动类型')) or '').strip(),
        'start': start, 'end': end,
        'ex_start': ex_start, 'ex_end': ex_end,
        'summary': str(_first_val(ev, ('summary', '简介', '说明', '一句话简介')) or '').strip(),
        'notes': str(_first_val(ev, ('notes', '注意事项', '备注', '提示')) or '').strip(),
        'greeting': str(_first_val(ev, ('greeting', '祝福', '祝福语')) or '').strip(),
        # opening：融合开场句（把祝福与放假安排说成一句话，随初始问候自然带出；
        # 用户口径 2026-09-25。旧的 greeting/summary 字段保留，未配置 opening 的节日自动回退。）
        'opening': str(_first_val(ev, ('opening', '融合开场', '开场')) or '').strip(),
        'guide': str(_first_val(ev, ('guide', '攻略', '攻略参考', '攻略关键词')) or '').strip(),
        'raw': ev,
    }


def load_events_normalized() -> list:
    """**游戏内活动**（知识库/活动/活动日历.xlsx）→ 归一化活动列表（状态判定/展示/提醒统一入口）。

    本入口只含游戏内活动，**不含法定节假日**（真实日历见 load_holiday_events）。"""
    cal = _load_activity_calendar()
    return [_norm_event(e) for e in (cal.get('events') or []) if isinstance(e, dict)]


def load_holiday_events() -> list:
    """**真实日历**（中国法定节假日，知识库/活动/节假日.json）→ 归一化事件列表。

    与游戏内活动（活动日历.xlsx）严格区分，只服务两件事：
      ① holiday_cn 联网不可用时的**离线兜底判定**；
      ② 按节假日名匹配阿卡莉口吻**祝福语**。
    文件缺失 / 解析失败返回 []。"""
    try:
        with open(HOLIDAY_CALENDAR_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return []
    events = data.get('events') if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    return [_norm_event(e) for e in events if isinstance(e, dict)]


def _deadline_passed(deadline, today, now=None) -> bool:
    """截止日是否已过。截止时刻为当天上午 10:00（DEADLINE_TIME）：
    传入 now（datetime）时，截止日当天 10:00 之后即视为已过期；未传 now 时按「整天」判定。"""
    if not deadline:
        return False
    if deadline != today:
        return deadline < today
    return bool(now) and now.time() >= DEADLINE_TIME


def _event_status(ev: dict, today, now=None) -> str:
    """判定活动状态：未开始 / 进行中 / 兑换中 / 已结束。入参可为原始 event 或已归一化 dict。

    - 进行中：活动期内（start ≤ today，且未过活动截止时刻）
    - 兑换中：活动已结束、但兑换期未结束——仍属未结束，可查询、可提醒
    - 已结束：活动期与兑换期都已过
    截止日以当天上午 10:00 为准（传 now 时精确到时刻，不传则按整天）。
    """
    ev = _norm_event(ev)
    start, end = ev.get('start'), ev.get('end')
    ex_end = ev.get('ex_end')
    finals = [d for d in (end, ex_end) if d]
    final_end = max(finals) if finals else None
    if start and today < start:
        return '未开始'
    if final_end and _deadline_passed(final_end, today, now):
        return '已结束'
    if end and _deadline_passed(end, today, now):
        return '兑换中'
    return '进行中'


def _remain_suffix(d, today, now=None) -> str:
    """剩余天数后缀。截止日本身的时刻已由时间行尾「上午10:00」承载，故此处不再重复；
    已过 10:00 → 标注已于今日 10:00 截止；已过期返回空。"""
    if not d:
        return ''
    r = (d - today).days
    if r < 0:
        return ''
    if r == 0:
        if now and now.time() >= DEADLINE_TIME:
            return '（已于今日10:00截止）'
        return '（今日截止）'
    return '（剩余%d天）' % r


def _format_activity(ev: dict, status: str, today, now=None) -> list:
    """单个活动 → 展示行列表（活动时间 / 兑换时间各含截止时刻与剩余天数；攻略行供 LLM 转述并触发
    GUIDE_LIB 标记）。入参可为原始 event 或已归一化 dict。截止时刻统一为当天上午 10:00。"""
    ev = _norm_event(ev)
    lines = ['【%s】' % (ev.get('name') or '').strip()]
    if ev.get('type'):
        lines.append('  类型：%s | 状态：%s' % (ev['type'], status))
    else:
        lines.append('  状态：%s' % status)
    start, end = ev.get('start'), ev.get('end')
    ex_start, ex_end = ev.get('ex_start'), ev.get('ex_end')
    if start or end:
        end_txt = ('%s %s' % (end.isoformat(), DEADLINE_TIME_LABEL)) if end else '?'
        span = '%s ~ %s' % (start.isoformat() if start else '?', end_txt)
        span += _remain_suffix(end, today, now)
        lines.append('  活动时间：%s' % span)
    if ex_start or ex_end:
        ex_end_txt = ('%s %s' % (ex_end.isoformat(), DEADLINE_TIME_LABEL)) if ex_end else '?'
        span = '%s ~ %s' % (ex_start.isoformat() if ex_start else '?', ex_end_txt)
        span += _remain_suffix(ex_end, today, now)
        lines.append('  兑换时间：%s' % span)
    if ev.get('summary'):
        lines.append('  简介：%s' % ev['summary'])
    if ev.get('notes'):
        lines.append('  注意事项：%s' % ev['notes'])
    # 法定节假日：展示祝福语（阿卡莉口吻，简短）
    if (ev.get('type') or '') == '法定节假日' and ev.get('greeting'):
        lines.append('  祝福：%s' % ev['greeting'])
    guide = (ev.get('guide') or '').strip()
    if guide:
        lines.append('  攻略参考：%s' % guide)
    return lines


def load_calendar_names() -> list:
    """**游戏内活动**名列表（供服务端硬路由做关键词提取：用户消息包含哪个活动名就查哪个）。

    只含游戏内活动（表格 活动日历.xlsx）。法定节假日**不接受询问**，不进本列表——
    节假日只在当天由 `core.calc_flow._today_holiday()` 生成祝福语（用户口径 2026-09-16）。"""
    return [str(e.get('name') or '').strip() for e in load_events_normalized()
            if str(e.get('name') or '').strip()]


class QueryActivity(BaseTool):
    """【活动】查询拂晓**游戏内活动**日历（知识库/活动/活动日历.xlsx）：当前进行中/即将开始的活动、
    截止日期与剩余天数、简介、注意事项。攻略正文不在本工具内展开，返回的「攻略参考」关键词可再调
    query_guide 定位文档。**法定节假日不在本工具范围**（节假日不接受询问，只在当天由问候语祝福）。"""
    name = 'query_activity'
    description = ('【活动】查询拂晓**游戏内活动**日历（知识库/活动/活动日历.xlsx）：当前有哪些活动，'
                   '各活动的活动时间（起止/剩余天数）与兑换时间（兑换截止/剩余天数）、简介与注意事项。'
                   '注意：活动与兑换截止时刻统一为截止日当天上午 10:00（转述时必须带上，避免用户超时）。'
                   '**法定节假日不接受询问、不在本工具范围**（节假日只在当天自动出现祝福语）。'
                   '用户问"有什么活动/活动什么时候开始或结束/兑换时间/XX活动攻略或注意事项"时调用。'
                   '参数 keyword：活动名关键词，不填则列出全部未结束的活动（含活动期结束但兑换期未结束的）；'
                   '填了则按名称匹配（含已结束的活动，会标注状态）。')
    parameters = {
        'type': 'object',
        'properties': {
            'keyword': {'type': 'string', 'description': '活动名关键词（可空=列出全部未结束活动）'},
        },
        'required': [],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        keyword = (params.get('keyword') or '').strip()

        events = load_events_normalized()
        if not events:
            return '活动日历暂未收录任何活动信息（知识库/活动/活动日历.xlsx 为空）。'

        now = datetime.datetime.now()
        today = now.date()

        # 关键词过滤：双向子串匹配（活动名/类型/简介）
        if keyword:
            k_low = keyword.lower()
            matched = []
            for e in events:
                hay = ' '.join(str(e.get(k, '')) for k in ('name', 'type', 'summary')).lower()
                if k_low in hay or any(kw and kw.lower() in k_low for kw in re.split(r'[\s、，,]+', e.get('name') or '') if len(kw) >= 2):
                    matched.append(e)
            if not matched:
                return f'活动日历中没有与「{keyword}」相关的活动。'
        else:
            matched = [e for e in events if _event_status(e, today, now) != '已结束']

        # 排序：进行中按活动截止升序（最紧急在前）→ 兑换中按兑换截止 → 未开始按开始日期 → 已结束垫底
        def _sort_key(e):
            st = _event_status(e, today, now)
            pri = {'进行中': 0, '兑换中': 1, '未开始': 2, '已结束': 3}[st]
            if st == '进行中':
                anchor = e.get('end')
            elif st == '兑换中':
                anchor = e.get('ex_end')
            else:
                anchor = e.get('start') or e.get('end')
            # 打捞活动恒置顶（用户口径：打捞活动排最前、颜色醒目）
            salvage = 0 if '打捞' in ((e.get('name') or '') + (e.get('type') or '') + (e.get('notes') or '')) else 1
            return (salvage, pri, anchor.toordinal() if anchor else 99999999)
        matched.sort(key=_sort_key)

        lines = []
        guides = []
        for e in matched:
            if lines:
                lines.append('')
            st = _event_status(e, today, now)
            blk = _format_activity(e, st, today, now)
            # 打捞活动标题加醒目标记（前端渲染为红色高亮），配合置顶
            if '打捞' in ((e.get('name') or '') + (e.get('type') or '') + (e.get('notes') or '')):
                blk = ['<<SALVAGE_HL>>' + blk[0]] + blk[1:]
            lines.extend(blk)
            g = (e.get('guide') or '').strip()
            if g and g not in guides:
                guides.append(g)
        # 有攻略参考时输出标记：前端打开攻略库面板聚焦第一个关键词
        if guides:
            lines.append('')
            lines.append(f"<<GUIDE_LIB>>{guides[0]}")

        # 注意：天气查询不在此处绑定触发。仅当用户输入本身含天气意图（问天气/冷热/下雨等）
        # 时，由独立 query_weather 工具按需调用，避免节日检索被动附带无关天气信息。

        return "\n".join(lines)


class QueryWeather(BaseTool):
    """【天气】查询用户所在地的实时天气（需浏览器定位授权）。无定位权限时提示开启。"""
    name = 'query_weather'
    description = '【天气】查询用户所在地的实时天气。基于浏览器定位（用户授权后自动上报位置）。用户问"天气/热不热/冷不冷/下雨吗"等闲聊时调用；返回温度、湿度、风力、天气描述。若未授权定位，提示用户开启浏览器定位权限。'
    parameters = {
        'type': 'object',
        'properties': {},
        'required': [],
    }

    def call(self, params, **kwargs):
        try:
            location_ref = self.cfg.get('location_ref')
            loc = getattr(location_ref, 'user_location', None) if location_ref else None
            if not loc:
                return "未获取到您的定位。请在浏览器地址栏允许本站点使用定位权限后，再试一次（页面会自动上报位置）。"
            try:
                lat = float(loc['lat'])
                lon = float(loc['lon'])
            except (KeyError, ValueError, TypeError):
                return "定位数据无效，请重新允许浏览器定位。"
            w = _fetch_weather(lat, lon)
            if not w:
                return "天气服务暂时不可用（网络波动），稍后再试。"
            lines = [
                f"您所在地实时天气：{w['desc']}",
                f"温度：{w['temp_c']}℃",
            ]
            if w.get("humidity") is not None:
                lines.append(f"湿度：{w['humidity']}%")
            if w.get("wind_kmh") is not None:
                lines.append(f"风速：{w['wind_kmh']} km/h")
            lines.append(f"（数据源：{w['source']}）")
            return "\n".join(lines)
        except Exception:
            return "天气查询暂时不可用，稍后再试。"

# ======================================================================
# 工具 17：知识库全文检索（LLM 对话辅助 RAG）
# ======================================================================

class KbSearch(BaseTool):
    """【知识库检索】拂晓完整攻略知识库（495+ 分块 + 16 张图片 OCR + 结构化引用表）。
    LLM 对话辅助用：用户咨询攻略/机制/配队/遭遇战/数值等闲聊类问题且无更合适工具时，
    按关键词检索相关知识块供回答。"""
    name = 'kb_search'
    description = '【知识库】拂晓完整攻略知识库检索（对话辅助）。参数 query：用户问题/关键词；max_results：返回块数（默认4）。按 主题路由+关键词 匹配返回最相关知识片段（来源/主题/内容）。适用于配队、练级、装备、阵型、机制、遭遇战等攻略类咨询。'
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '检索问题或关键词（如 练级 配队 阵型 遭遇战 嘉波 装备）'},
            'max_results': {'type': 'integer', 'description': '最多返回知识块数', 'default': 4},
        },
        'required': ['query'],
    }

    def call(self, params, **kwargs):
        from .common import _load_agent_kb
        params = self._verify_json_format_args(params)
        query = (params.get('query') or '').strip()
        max_results = int(params.get('max_results', 4) or 4)
        if not query:
            return '请输入检索关键词。'
        kb = _load_agent_kb()
        if not kb or not kb.get('chunks'):
            return '知识库（拂晓知识库_agent.json）未找到，请先在 FX数据管理工具 构建。'
        chunks = kb['chunks']
        # ① 主题路由：query_patterns 命中 → 优先该 topic
        topic = None
        for pat in (kb.get('query_patterns') or []):
            if pat.get('pattern') and pat['pattern'] in query:
                topic = pat.get('topic')
                break
        # ② 打分：content/keywords/topic 命中 query 词
        scored = []
        for c in chunks:
            score = 0
            kw = c.get('keywords') or []
            content = c.get('content') or ''
            c_topic = c.get('topic') or ''
            for qw in _query_words(query):
                if not qw:
                    continue
                if qw in content:
                    score += 2
                if any(qw in k for k in kw):
                    score += 3
                if qw in c_topic:
                    score += 4
            if topic and c_topic == topic:
                score += 5
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        top = scored[:max_results]
        if not top:
            return '知识库未找到与「' + query + '」相关的内容。'
        # 输出清理后的知识片段（去掉 检索标题/来源标注/作者/致谢/观前提示 等元信息）
        lines = []
        for score, c in top:
            content = _clean_kb_chunk_content(c.get('content') or '')[:400]
            if not content:
                continue
            lines.append(content)
            lines.append('')
        if not lines:
            return '知识库未找到与「' + query + '」相关的内容。'
        return chr(10).join(lines).rstrip()


# ======================================================================
# 工具 16：攻略问答检索（向量 RAG：攻略QA库 + 向量库，具体问题检索作答）
# ======================================================================

# 攻略 QA 库与向量库（build/guide_qa_build.py + guide_qa_embed.py 产物）
_GUIDE_QA_JSON = os.path.join(KB_DIR, "llm", "攻略QA库.json")
_GUIDE_QA_NPZ = os.path.join(KB_DIR, "llm", "攻略QA向量库.npz")
# 检索相似度阈值（低于视为未命中，可按建库效果调整）
_GUIDE_QA_SCORE_MIN = 0.35
# 片段输出阈值：低于此分的命中说明相关性不足，贴出来只会得到"回复不太清楚"的含糊作答，
# 不如不输出并提示指挥官自行查阅（自测：0.56~0.83 命中方向正确，0.49~0.54 对错参半，0.49 以下基本错文档）
_GUIDE_QA_SCORE_SHOW = 0.50
# 轻量自检触发区间：首跳命中但处于 [SHOW, SHOW+MARGIN) 边缘时，做一次覆盖度校验，
# 不足则补充检索（增强③）。低于 SHOW 走多查询改写（增强①）。
_SELFCHECK_MARGIN = 0.08
_QA_CACHE = {"mtime": None, "data": None}


def _agent_enhance_on() -> bool:
    """三项增强（多查询改写/有界工具循环/轻量自检）总开关，默认开；AKALI_AGENT_ENHANCE=off 全关。"""
    return (os.environ.get('AKALI_AGENT_ENHANCE') or 'on').strip().lower() != 'off'


def _load_guide_qa():
    """mtime 感知加载 QA 库 + 向量库（任一更新后自动重读，无需重启）。"""
    if not (os.path.exists(_GUIDE_QA_JSON) and os.path.exists(_GUIDE_QA_NPZ)):
        return None
    mt = (os.path.getmtime(_GUIDE_QA_JSON), os.path.getmtime(_GUIDE_QA_NPZ))
    if _QA_CACHE["mtime"] == mt:
        return _QA_CACHE["data"]
    import numpy as np
    with open(_GUIDE_QA_JSON, encoding="utf-8") as f:
        qa = json.load(f)
    z = np.load(_GUIDE_QA_NPZ)
    data = {
        "meta": {c["id"]: c for c in qa.get("chunks", [])},
        "ids": z["ids"],
        "matrix": z["matrix"],
    }
    _QA_CACHE["mtime"] = mt
    _QA_CACHE["data"] = data
    return data


# ======================================================================
# 检索增强（2026-09-07 起；2026-09-25 改词级）：① query 归一（实体标准化 +
# 机制/玩法口语 → 书面术语）② jieba 词级 BM25 与向量分 RRF 全量融合。
# 背景（实测诊断）：纯向量 top1/top2 差经常 <0.05、专名口语（北卡/T优）易错召回，
# 排序不可靠 → 引入关键词路做硬召回。判定阈值仍沿用 top1 的向量余弦分。
# ======================================================================
_QA_KW_CACHE = {"mtime": None, "index": None}
_NICK_MAP = None
# 选择性语义 rerank：只在向量候选分差小或问题含多个子问题时调用。
# 不替换向量召回，也不引入候选集外的片段。
_SEMANTIC_RERANK = os.environ.get('AKALI_RAG_RERANK', '1').strip().lower() not in ('0', 'false', 'off', 'no')
_SEMANTIC_RERANK_GAP = 0.035
_RERANK_CACHE = {}
# 口语变体向量折扣（2026-09-07 扫描实测）：变体分 × 0.92 参与 chunk 归并——
# 变体只做"低分兜底/补召回"，不抢 top1（penalty=1.0 时 hit@1 81.0%→76.2%，负收益）
_VARIANT_DISCOUNT = 0.92
# BM25 召回片段放行门槛：向量分低于「输出线 - 此值」的片段不放行。
# 词级 BM25 修好前实测出现过 0.32 的无关片段被 BM25 顶到输出首位，故加向量下限兜住。
_BM25_PASS_MARGIN = 0.12


def _load_nick_map():
    """昵称/别名 → 正规名（舰名等专名归一用；加载一次）。"""
    global _NICK_MAP
    if _NICK_MAP is not None:
        return _NICK_MAP
    m = {}
    try:
        p = os.path.join(KB_DIR, "底层数据", "源文件", "ship_nicknames.json")
        d = json.load(open(p, encoding="utf-8"))
        for canon, aliases in d.items():
            if canon == "_comment" or not isinstance(aliases, list):
                continue
            for a in aliases:
                a = (a or "").strip()
                if a and a != canon:
                    m[a] = canon
    except Exception:
        pass
    _NICK_MAP = m
    return m


# 机制/玩法口语 → 书面术语（可与昵称表共存；命中即追加到 query 尾部）
_TERM_ALIAS = {
    't优': 'T优势', 't劣': 'T劣势', 't不利': 'T劣势', '反航': '反航战', '同航': '同航战',
    '制空': '制空状态', '确保': '制空确保', '空优': '制空优势',
    '穿甲': '穿甲弹', '高爆': '高爆弹', '通常弹': '通常',
    '点燃': '点燃 燃烧', '燃烧': '点燃 燃烧',
    '暴击': '暴击率 暴击伤害', '命中': '命中值 命中率',
    '练级': '练级 经验', '推图': '推图 主线', '配队': '配队 阵容',
    '竞技场': '争锋竞技场', 'jjc': '争锋竞技场',
    '拉古兹': '拉古兹', '遭遇战': '遭遇战',
    '旗舰': '旗舰', '支援技': '支援技', '必杀': '必杀技', '通常技': '通常技',
    '架': '舰载机 飞机', '门': '火炮 主炮',
}


def _expand_query(query: str) -> str:
    """query 归一：① 昵称表硬性对齐 ② 实体标准化 ③ 机制/玩法口语 → 书面术语。

    2026-09-25 口径（用户要求昵称表**硬性**）：扫**原始 query**，凡命中昵称表
    （≥2 字别名）即强制把正规名追加到 query 尾部，确保 BM25/向量两路都能对齐；
    在实体标准化**之前**扫，是因为 entity_norm 的原地替换会消耗掉别名，标准化后再扫
    就扫不到（硬性保证不能依赖 entity_norm 的拼音模糊是否命中）。
    """
    if not query:
        return query
    adds = []
    # ① 昵称表硬性对齐：仅收 ≥2 字别名（单字昵称如"雷/电"在长句里极易误命中、稀释语义）
    try:
        nicks = _load_nick_map() or {}
        hits = sorted([a for a in nicks if len(a) >= 2 and a in query], key=len, reverse=True)
        for a in hits[:2]:
            if nicks[a] not in adds:
                adds.append(nicks[a])
    except Exception:
        pass
    # ② 实体标准化（检索前预处理）：把输入里的实体名与知识库用词对齐。
    #    BOSS 名一字之差若不纠正，向量与关键词两路都对不上——这正是
    #    「遭遇战阿斯莫德配装配队有吗」检索落空的根因之一（库内记为 阿丝莫德）。
    #    查询期标准化，无需重建/重新嵌入向量库。
    q = query
    try:
        from core.entity_norm import standardize
        q = standardize(q)
    except Exception:
        pass
    low = q.lower()
    # ③ 机制/玩法口语：昵称已占满 2 个则不再追加；最多补 2 个术语，避免长 query 被无关术语稀释
    if len(adds) < 2:
        for k, v in sorted(_TERM_ALIAS.items(), key=lambda x: -len(x[0])):
            if k in low:
                for term in v.split():
                    if term not in adds:
                        adds.append(term)
                        break
            if len(adds) >= 2:
                break
    return q if not adds else (q + " " + " ".join(adds[:2]))


# jieba 词级分词（BM25 词项来源）：用户词典由内置词典表生成——
# 舰名昵称表 ship_nicknames.json（正规名 + 各别名）与术语表 _TERM_ALIAS（口语 key + 书面词）。
# 词典用于「分词」；昵称表另在 _expand_query 做匹配期硬性对齐（追加正规名），两者并存。
_JIEBA = None


def _user_dict_words():
    """jieba 用户词典词集：舰名昵称 + 术语表（仅用于分词，不参与匹配）。

    只收 ≥2 字词：单字（雷/电/架/门 等）入词典会被 jieba 与相邻字合并成怪词
    （实测「2架飞机」→「架飞机」），且原昵称表本就只取 ≥2 字别名。"""
    words = set()

    def _add(w):
        w = (w or '').strip()
        if len(w) >= 2:
            words.add(w)

    try:
        for a, canon in (_load_nick_map() or {}).items():
            _add(a)
            _add(canon)
    except Exception:
        pass
    # BOSS 名（拉古兹.json）：与舰名同理必须整体成词，否则 BM25 对不上专名
    # （实测未加时「阿丝莫德」被切成「阿丝」「莫德配」）。
    try:
        from core.entity_norm import boss_names
        for b in boss_names():
            _add(b)
    except Exception:
        pass
    for k, v in _TERM_ALIAS.items():
        _add(k)
        for t in (v or '').split():
            _add(t)
    return words


def _jieba_mod():
    """懒加载 jieba 并注入用户词典（仅分词用）。"""
    global _JIEBA
    if _JIEBA is None:
        import jieba as _jb
        _jb.initialize()
        for w in _user_dict_words():
            _jb.add_word(w)
        _JIEBA = _jb
    return _JIEBA


def _terms(text: str):
    """BM25 词项：jieba 词级切分（小写；纯标点/空白丢弃，其余含单字保留，噪声交由 idf 压制）。"""
    t = (text or "").lower()
    if not t.strip():
        return []
    out = []
    for w in _jieba_mod().lcut(t):
        w = w.strip()
        if w and re.search(r'\w', w):
            out.append(w)
    return out


def _bigrams(text: str):
    """字符 2-gram 词项（无分词依赖，中文可用；英文/数字按词保留）。
    仍供精准问句命中的模糊覆盖率计算使用；BM25 词项已改用 _terms。"""
    t = re.sub(r"\s+", "", (text or "").lower())
    if not t:
        return []
    grams = [t[i:i + 2] for i in range(len(t) - 1)]
    words = re.findall(r"[a-z0-9]{2,}", (text or "").lower())
    return grams + words


def _build_kw_index(data):
    """关键词索引（BM25，jieba 词级）：doc 文本 = question/keywords 加权 + content。"""
    import numpy as np
    ids = list(data["ids"])
    docs, df = [], {}
    for cid in ids:
        m = data["meta"].get(str(cid)) or {}
        head = " ".join([m.get("question", "")] + list(m.get("keywords") or []))
        # 头部（问题+关键词）重复 2 次以提升权重
        txt = (head + " ") * 2 + (m.get("content") or "")
        terms = _terms(txt)
        docs.append(terms)
        for t in set(terms):
            df[t] = df.get(t, 0) + 1
    n = max(len(docs), 1)
    # BM25 标准 idf（log 形式、非负）：共现于全部文档的词（「吗/有」等）≈0，避免
    # 长文档靠常见字刷分——旧的下限式（1.0+…）给共现词保底 1.0，实测会让无关长文
    # 章靠「吗/有」把 BM25 顶到最高分。
    idf = {t: float(np.log(1.0 + (n - c + 0.5) / (c + 0.5))) for t, c in df.items()}
    return {"ids": ids, "docs": docs, "idf": idf,
            "lens": [len(d) for d in docs], "avg": (sum(len(d) for d in docs) / n)}


def _kw_index(data):
    try:
        mt = os.path.getmtime(_GUIDE_QA_JSON)
    except OSError:
        mt = None
    if _QA_KW_CACHE["mtime"] == mt and _QA_KW_CACHE["index"]:
        return _QA_KW_CACHE["index"]
    idx = _build_kw_index(data)
    _QA_KW_CACHE["mtime"] = mt
    _QA_KW_CACHE["index"] = idx
    return idx


def _kw_scores(data, query):
    """BM25 关键词分（与向量同长度数组，未归一化）。"""
    import numpy as np
    idx = _kw_index(data)
    qterms = _terms(_expand_query(query))
    if not qterms:
        return np.zeros(len(idx["ids"]), dtype=np.float32)
    k1, b = 1.5, 0.75
    out = np.zeros(len(idx["ids"]), dtype=np.float32)
    from collections import Counter
    qc = Counter(qterms)
    for j, terms in enumerate(idx["docs"]):
        if not terms:
            continue
        tf = Counter(terms)
        dl = idx["lens"][j] or 1
        s = 0.0
        for t, qn in qc.items():
            f = tf.get(t, 0)
            if not f:
                continue
            idf = idx["idf"].get(t, 1.0)
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (idx["avg"] or 1)))
        out[j] = s
    return out


def _hybrid_order(data, query, vec_scores, k, vec_top=15, bm25_top=15, rrf_k=60,
                  w_vec=2.0, w_bm25=1.0):
    """向量 + BM25 全量 RRF 融合（2026-09-25：由「候选内局部重排」改为真·混合）。

    两路各自召回 topN，再按名次 Σ w/(rrf_k+rank) 融合——只比名次、免分数校准。
    **向量权重大于 BM25（2:1）**：实测等权时 BM25 的噪声能把向量 top1 挤下去
    （「遭遇战阿斯莫德配队」精确页 0.74 被无关页顶掉），故以向量为准、BM25 做补充。
    变体行（同一 chunk 多行）仍按 _VARIANT_DISCOUNT 归并，取该 chunk 的代表行。

    返回 (rows, bm25_ids)：
      rows     ：融合排序后的代表行号（每 chunk 一行，可直接索引 scores/meta）
      bm25_ids ：进入 BM25 topN 且关键词分 > 0 的 chunk id 集合——供输出端放行
                 「词汇强命中」片段（其向量分可能略低于输出线，但不该被整段丢弃）。
    """
    import numpy as np
    ids_all = data.get("ids")
    vec = np.asarray(vec_scores, dtype=np.float32)
    try:
        kw = np.asarray(_kw_scores(data, query), dtype=np.float32)
        if len(kw) != len(vec):
            kw = np.zeros(len(vec), dtype=np.float32)
    except Exception:
        kw = np.zeros(len(vec), dtype=np.float32)
    n_all = len(vec)
    if not n_all:
        return [], set()
    if ids_all is None or len(ids_all) != n_all:
        k = max(1, min(int(k), n_all))
        return [int(i) for i in np.argsort(-vec)[:k]], set()
    # 行 → chunk 归并：代表行取「折价后向量最高」的行；两路分各取该 chunk 的最大值
    best = {}
    seen = set()
    for i in range(n_all):
        cid = str(ids_all[i])
        v = float(vec[i])
        w = v if cid not in seen else v * _VARIANT_DISCOUNT
        seen.add(cid)
        rec = best.get(cid)
        if rec is None or w > rec[0]:
            best[cid] = [w, int(i), float(kw[i])]
        elif float(kw[i]) > rec[2]:
            rec[2] = float(kw[i])
    k = max(1, min(int(k), len(best)))
    vec_ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:max(vec_top, k)]
    bm25_ranked = sorted(best.items(), key=lambda kv: -kv[1][2])[:max(bm25_top, k)]
    score, bm25_ids = {}, set()
    for rank, (cid, _) in enumerate(vec_ranked):
        score[cid] = score.get(cid, 0.0) + w_vec / (rrf_k + rank + 1)
    for rank, (cid, _) in enumerate(bm25_ranked):
        score[cid] = score.get(cid, 0.0) + w_bm25 / (rrf_k + rank + 1)
        # 只放行「词汇强命中且向量分不太低」的片段，避免 BM25 噪声片段入输出
        if best[cid][2] > 0 and best[cid][0] >= _GUIDE_QA_SCORE_SHOW - _BM25_PASS_MARGIN:
            bm25_ids.add(cid)
    ordered = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
    return [best[cid][1] for cid, _ in ordered[:k]], bm25_ids


def _needs_semantic_rerank(query: str, order, scores) -> bool:
    """是否需要 LLM 语义重排：向量分差小 / 多子问 / 含量词时触发。

    保留（未随词级 BM25 移除）：BM25 是词袋模型，无法区分「2架」与「2门」这类
    数字+量词的组合约束，多子问的覆盖也需语义裁决，故这些触发条件继续保留。
    """
    if not _SEMANTIC_RERANK or len(order) < 2:
        return False
    try:
        gap = float(scores[order[0]] - scores[order[1]])
    except Exception:
        gap = 1.0
    multi_ask = (len(re.findall(r'[？?]', query or '')) >= 2
                 or bool(re.search(r'分别|同时|以及|并且|到底.{0,12}(?:几|多少|怎么)', query or '')))
    unit_sensitive = bool(re.search(r'\d+\s*[架门艘次]', query or ''))
    return gap < _SEMANTIC_RERANK_GAP or multi_ask or unit_sensitive


def _semantic_rerank(query: str, order, scores, data):
    """Qwen 只对少量向量候选排序，目标是覆盖用户的全部子问题。"""
    if not _needs_semantic_rerank(query, order, scores):
        return list(order)
    ids = tuple(str(data['ids'][int(i)]) for i in order)
    cache_key = (query, ids)
    if cache_key in _RERANK_CACHE:
        ranked_ids = _RERANK_CACHE[cache_key]
    else:
        candidates = []
        for rank, i in enumerate(order):
            meta = data['meta'].get(str(data['ids'][int(i)])) or {}
            candidates.append({
                'id': str(data['ids'][int(i)]),
                'vector_rank': rank + 1,
                'section': meta.get('section') or '',
                'question': meta.get('question') or '',
                'alt_questions': list(meta.get('alt_questions') or [])[:3],
                'content': (meta.get('content') or '')[:1400],
            })
        try:
            from openai import OpenAI
            from llm.llm_client import get_llm_config, MODEL_SERVER
            cfg = get_llm_config(temperature=0.0)
            client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=20)
            resp = client.chat.completions.create(
                model=cfg['model'],
                messages=[
                    {'role': 'system', 'content':
                        '你是知识库候选片段重排器，不回答问题。'
                        '按“直接支持用户结论”、“覆盖所有子问题”、“实体、数字与量词条件一致”的顺序重排。'
                        '架、门、艘、次等量词必须一致，例如用户问2架装备时，2门发射器不相关。'
                        '不得新增、删除或改写 id。只输出 JSON：{"ranked_ids":["id1","id2"]}'},
                    {'role': 'user', 'content': '用户问题：%s\n\n候选：%s' % (
                        query, json.dumps(candidates, ensure_ascii=False))},
                ],
                temperature=0.0,
                extra_body=cfg['generate_cfg']['extra_body'],
            )
            raw = (resp.choices[0].message.content or '').strip()
            m = re.search(r'\{[\s\S]*\}', raw)
            ranked_ids = list((json.loads(m.group(0)) if m else {}).get('ranked_ids') or [])
            if set(ranked_ids) != set(ids) or len(ranked_ids) != len(ids):
                return list(order)
            if len(_RERANK_CACHE) > 256:
                _RERANK_CACHE.clear()
            _RERANK_CACHE[cache_key] = ranked_ids
        except Exception:
            return list(order)
    pos = {cid: i for i, cid in enumerate(ranked_ids)}
    return sorted(order, key=lambda i: pos.get(str(data['ids'][int(i)]), len(pos)))


def _unit_consistent_order(query: str, order, data):
    """显式数量词优先于语义相似度，防止“2架飞机”召回“2门发射器”。

    保留（未随词级 BM25 移除）：jieba 把「2架」「2门」切成「2」+「架/门」，
    BM25 无法保证“数字+单位”整体一致，故仍需这层确定性硬约束兜住量词。
    """
    pairs = re.findall(r'(\d+)\s*([架门艘次])', query or '')
    if not pairs:
        return list(order)
    exact, neutral, conflict = [], [], []
    units = '架门艘次'
    for i in order:
        meta = data['meta'].get(str(data['ids'][int(i)])) or {}
        body = ' '.join((meta.get('question') or '', meta.get('content') or ''))
        has_exact = any(re.search(rf'{re.escape(number)}\s*{unit}', body)
                        for number, unit in pairs)
        has_conflict = any(re.search(rf'{re.escape(number)}\s*[{units.replace(unit, "")}]', body)
                           for number, unit in pairs)
        (exact if has_exact else conflict if has_conflict else neutral).append(i)
    return exact + neutral + conflict if exact else list(order)


def retrieve_guide(query: str, topk: int = 4):
    """对外检索入口（供工具与评测脚本共用）：返回 [(chunk_meta, 向量余弦分)]。"""
    import numpy as np
    data = _load_guide_qa()
    if not data:
        return []
    qv = _embed_query(_expand_query(query))
    vec = data["matrix"] @ qv
    order, _ = _hybrid_order(data, query, vec, topk * 3)
    out = []
    for i in order:
        m = data["meta"].get(str(data["ids"][int(i)]))
        if m:
            out.append((m, float(vec[int(i)])))
    return out


def _embed_query(text: str):
    """查询向量化（服务商 embeddings，单条；失败抛异常由调用方转可读话术）。"""
    import numpy as np
    from openai import OpenAI
    from llm.llm_client import api_key, MODEL_SERVER
    client = OpenAI(api_key=api_key, base_url=MODEL_SERVER, timeout=20)
    resp = client.embeddings.create(
        model=os.environ.get('EMBEDDING_MODEL', 'text-embedding-v4'),
        input=[text[:2000]], encoding_format="float")
    v = np.asarray([resp.data[0].embedding], dtype=np.float32)
    v /= (np.linalg.norm(v, axis=1, keepdims=True) + 1e-12)
    return v[0]


# 图片/案例引用句式（"案例请看上图所示"等）——聊天里无图可看，这类句子贴出来只会困惑
_IMG_REF_RE = re.compile(r'如[上下]?图|见[上下]图|[上下]图所示|请看[上下]?图|案例请看|参考[上下]?图|配图|截图')


def _strip_case_refs(text):
    """按句移除含图片/案例引用的内容（"…案例请看上图所示。"整句删掉），其余原样保留。"""
    if not text:
        return text
    parts = re.split(r'([。！？!?；;\n])', text)
    out = []
    for i in range(0, len(parts) - 1, 2):
        if _IMG_REF_RE.search(parts[i]):
            continue
        out.append(parts[i] + parts[i + 1])
    if len(parts) % 2 and not _IMG_REF_RE.search(parts[-1]):
        out.append(parts[-1])
    return re.sub(r'\n{3,}', '\n\n', ''.join(out)).strip()


def _clean_doc_name(doc: str) -> str:
    """来源文件名 → 展示用篇目名（去 .md、去编号前缀）。

    "6.3 给萌新的PVE配队思路.md" → "给萌新的PVE配队思路"
    """
    doc = (doc or "").strip()
    if doc.endswith(".md"):
        doc = doc[:-3]
    return re.sub(r'^\d+(?:\.\d+)?[\s._-]+', '', doc).strip()


def _is_qa_supplement(meta: dict) -> bool:
    """问答补充是人工校正知识，不作为可展示的攻略篇目来源。"""
    return (str((meta or {}).get('id') or '').startswith('qa_supp_')
            or _clean_doc_name((meta or {}).get('source') or '') == '问答补充')


def _format_rag_hit(meta: dict, score: float) -> tuple[str, str]:
    """格式化召回片段；问答补充不写来源名，返回的标题为空。"""
    content = _strip_case_refs((meta or {}).get('content', ''))
    if _is_qa_supplement(meta):
        return f"【知识片段】（相似度 {score:.2f}）\n{content}", ''
    doc = _clean_doc_name((meta or {}).get('source') or '')
    text = f"【《{doc}》· {(meta or {}).get('section', '')}】（相似度 {score:.2f}）\n{content}"
    return text, doc


_QA_QUERY_WRAPPER_RE = re.compile(
    r'^(?:想问(?:一下|下)?|请问|麻烦(?:问下|说下|讲下|告诉我)?|帮我(?:查下|看看|问下)?|'
    r'能不能(?:直接)?(?:说下|讲下|告诉我)?|可以(?:直接)?(?:说下|讲下|告诉我)?|'
    r'我想知道|求问|问下)[，,：:\s]*'
)


def _normalize_qa_question(text: str) -> str:
    """把口语包装剥掉，仅用于命中 QA 库里已有的问题/改写问法。"""
    value = (text or '').strip().lower()
    for _ in range(2):
        value = _QA_QUERY_WRAPPER_RE.sub('', value)
    return re.sub(r'[\s，。！？、；：,.!?;:"“”‘’（）()【】\[\]<>《》]+', '', value)


def _exact_qa_meta(data, query: str):
    """优先命中人工写入的 question/alt_questions，避免相邻 chunk 污染答案。

    随机测试会把库内问题改成自然口语。只要剥掉礼貌前缀后与某条问题完全一致，
    就只交付这一条证据；无需再让向量相似度在同主题的相邻段落间猜测。
    """
    target = _normalize_qa_question(query)
    if len(target) < 4:
        return None
    ids = data.get('ids')
    if ids is None:
        return None
    exact_matches = []
    fuzzy_by_phrase = {}
    from difflib import SequenceMatcher
    target_pairs = set(_bigrams(target))
    for raw_id in ids:
        meta = data.get('meta', {}).get(str(raw_id)) or {}
        variants = [meta.get('question') or ''] + list(meta.get('alt_questions') or [])
        best = 0.0
        best_phrase = ''
        for item in variants:
            candidate = _normalize_qa_question(item)
            if not candidate:
                continue
            if candidate == target:
                exact_matches.append(meta)
                continue
            # 允许“萌新优先…”/“萌新要优先…”这类轻量改写，以及删掉“怎么搞”等
            # 口语成分后的核心问题。要求至少 8 字且 top1 与次名有明显差距。
            if min(len(target), len(candidate)) >= 8:
                seq = SequenceMatcher(None, target, candidate).ratio()
                cand_pairs = set(_bigrams(candidate))
                shorter_coverage = (len(target_pairs & cand_pairs)
                                    / max(1, min(len(target_pairs), len(cand_pairs))))
                score = max(seq, shorter_coverage * 0.95)
                if score > best:
                    best, best_phrase = score, candidate
        if best:
            # 同一个标准问题可能因分块而对应多个相邻内容；它们不是两个竞争意图。
            # 以标准问法归组，并优先保留正文更完整的记录。
            previous = fuzzy_by_phrase.get(best_phrase)
            if (previous is None or best > previous[0]
                    or (best == previous[0]
                        and len(meta.get('content') or '') > len(previous[1].get('content') or ''))):
                fuzzy_by_phrase[best_phrase] = (best, meta)
    if exact_matches:
        return max(exact_matches, key=lambda item: len(item.get('content') or ''))
    fuzzy = list(fuzzy_by_phrase.values())
    fuzzy.sort(key=lambda item: item[0], reverse=True)
    if fuzzy:
        top = fuzzy[0][0]
        second = fuzzy[1][0] if len(fuzzy) > 1 else 0.0
        if top >= 0.78 and top - second >= 0.06:
            return fuzzy[0][1]
    return None


def match_guide_question(query: str):
    """返回与库内 question/alt_questions 高置信匹配的记录；供路由一致性校验复用。"""
    data = _load_guide_qa()
    return _exact_qa_meta(data, query) if data else None


def _split_rag_segments(text: str):
    """GuideRAG 片段文本 → [(doc, section, content, sim)]（丢来源页脚；解析失败项跳过）。"""
    body = re.sub(r'\n*【来源】[^\n]*\s*$', '', text or '')
    out = []
    for seg in re.split(r'\n\n+(?=【(?:《|知识片段))', body):
        m = re.match(r'^【(?:《(?P<doc>[^》]+)》·\s*(?P<section>[^】]*)|知识片段)】'
                     r'\s*（相似度\s*(?P<sim>[\d.]+)）\s*\n?(?P<content>[\s\S]*)$', seg)
        if not m:
            continue
        out.append((_clean_doc_name(m.group('doc') or ''), (m.group('section') or '').strip(),
                    m.group('content').strip(), float(m.group('sim'))))
    return out


# 框架类问法：问二选一、对应关系、区别归属——答案需要完整的分支/对应证据，
# 单个 chunk 往往只含其中一个分支，精确命中也不做单块短路，放行进多块检索。
_FRAME_Q_RE = re.compile(r'还是|分别|对应|归属|区别|对比')


# ======================================================================
# 检索增强（仅首跳弱/空才触发，平时零额外开销）
#   ① 多查询改写：1 次 LLM 扩成 2~3 子查询 → 各 retrieve_guide → RRF 融合去重
#   ③ 轻量自检：命中但处置信边缘时，校验片段覆盖度，不足则「问题+缺口」补充检索
# ======================================================================

def _expand_queries_llm(query: str):
    """低置信闸门触发：1 次 LLM 把问题拆成 2~3 个聚焦子查询。失败返回 []。"""
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=20)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                 '你是攻略问答的查询扩展器，不回答。把用户的具体游戏攻略问题，'
                 '拆成 2~3 个更聚焦、互不重复的子查询，分别覆盖问题的不同侧面，'
                 '便于多路向量检索召回更全。保持原意与专有名词，不要改写风格。'
                 '只输出 JSON：{"queries":["子查询1","子查询2","子查询3"]}'},
                {'role': 'user', 'content': query},
            ],
            temperature=0.0,
            extra_body=cfg['generate_cfg']['extra_body'],
        )
        raw = (resp.choices[0].message.content or '').strip()
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return []
        obj = json.loads(m.group(0))
        qs = [str(x).strip() for x in (obj.get('queries') or []) if str(x).strip()]
        return qs[:3]
    except Exception:
        return []


def _multi_query_retrieve(query: str, max_results: int = 4):
    """低置信闸门触发：扩展子查询 → 各 retrieve_guide → RRF 融合去重 → (order_indices, scores)。

    返回 None 表示无有效子查询或检索失败，调用方回落原 low 行为。
    scores 为各 chunk 在子查询中的最高向量分（供下方阈值过滤/重排复用）。
    """
    subs = _expand_queries_llm(query)
    if not subs:
        return None
    data = _load_guide_qa()
    if not data:
        return None
    ids = data.get('ids')
    if ids is None:
        return None
    n = len(ids)
    id_to_idx = {str(x): i for i, x in enumerate(ids)}
    max_scores = [-1.0] * n
    rank_maps = []
    for sq in subs:
        try:
            res = retrieve_guide(sq, max_results)
        except Exception:
            continue
        if not res:
            continue
        rm = {}
        for rank, (meta, sc) in enumerate(res):
            cid = str((meta or {}).get('id') or '')
            idx = id_to_idx.get(cid)
            if idx is None:
                continue
            rm[idx] = rank
            if sc > max_scores[idx]:
                max_scores[idx] = sc
        if rm:
            rank_maps.append(rm)
    if not rank_maps:
        return None
    fused = {}
    for rm in rank_maps:
        for idx, rank in rm.items():
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (60 + rank + 1)
    order = sorted(fused, key=lambda i: -fused[i])[: max(8, max_results * 3)]
    return order, max_scores


def _selfcheck_llm(query: str, frag: str):
    """轻量自检：片段是否足够直接回答用户问题。充足返回 None；不足返回 {'gap':...}；失败返回 None。"""
    try:
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=15)
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                 '你是答案覆盖度校验器，不回答。给定用户问题与一段知识片段，'
                 '判断该片段是否足以直接、完整地回答用户问题。'
                 '只输出 JSON：{"sufficient":true/false,"gap":"若不足，用户还需知道什么（一句话，用于补充检索）"}'},
                {'role': 'user', 'content': '用户问题：%s\n\n知识片段：%s' % (query, frag)},
            ],
            temperature=0.0,
            extra_body=cfg['generate_cfg']['extra_body'],
        )
        raw = (resp.choices[0].message.content or '').strip()
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return None
        obj = json.loads(m.group(0))
        if obj.get('sufficient'):
            return None
        return obj
    except Exception:
        return None


def _selfcheck_supplement(query: str, base_order, base_scores, max_results: int, data):
    """增强③：当前 top 片段处置信边缘时，校验覆盖度；不足则「问题+缺口」补充检索并并入。

    返回 (order, scores, top) 或 None（不补充/补充后仍弱）。
    """
    try:
        top_i = base_order[0]
        meta0 = data['meta'].get(str(data['ids'][int(top_i)])) or {}
        frag = (meta0.get('content') or '')[:1200]
    except Exception:
        return None
    if not frag:
        return None
    suf = _selfcheck_llm(query, frag)
    if not suf:
        return None
    gap = str(suf.get('gap') or '').strip()
    if not gap:
        return None
    aug = (query + ' ' + gap).strip()
    mq = _multi_query_retrieve(aug, max_results)
    if not mq:
        return None
    order2, scores2 = mq
    n = len(data['ids'])
    comb = list(base_scores)
    while len(comb) < n:
        comb.append(-1.0)
    for i in range(n):
        if scores2[i] > comb[i]:
            comb[i] = scores2[i]
    merged = list(order2) + [i for i in base_order if i not in set(order2)]
    merged = _semantic_rerank(query, merged, comb, data)
    merged = _unit_consistent_order(query, merged, data)[:max_results]
    top2 = max((float(comb[i]) for i in merged), default=0.0)
    if top2 < _GUIDE_QA_SCORE_SHOW:
        return None
    return merged, comb, top2


def _guide_rag_search(query: str, max_results: int = 4):
    """攻略片段检索核心（向量 + 关键词 RRF 融合）→ (text, reason)。

    把原 GuideRAG.call 里的检索逻辑抽出来复用——GuideRAG（问题为中心）与
    find_ship_mentions（实体为中心）走同一套检索，避免两份实现各自漂移。
      text  ：命中片段文本（含【来源】页脚）；未命中为 ''
      reason：'' 命中 ／ 'empty' 空查询 ／ 'nobuild' 未建库 ／ 'embed:…' 向量化失败
              ／ 'low:0.42' 最高分够不到输出线
    """
    query = (query or '').strip()
    if not query:
        return '', 'empty'
    data = _load_guide_qa()
    if not data:
        return '', 'nobuild'
    # QA 库的问题和人工改写就是高置信度索引。精确命中时只返回该记录，
    # 防止同篇相邻段落把回答带到另一个数字、角色或玩法上。
    # 例外：框架类问法（还是/对应/区别等）答案需完整分支，单块常只含一个分支，
    # 不做单块短路，放行进多块检索（精确块仍在下方钉到首位）。
    exact = _exact_qa_meta(data, query)
    if exact and exact.get('content') and not _FRAME_Q_RE.search(query):
        line, doc = _format_rag_hit(exact, 1.0)
        if doc:
            line += f"\n\n【来源】《{doc}》"
        return line, ''
    try:
        import numpy as np
        qv = _embed_query(_expand_query(query))
    except Exception as e:
        # 向量服务短时超载时，不能把具体知识问题回落成整篇攻略目录。
        # 用同一 QA 库的 BM25 索引做严格 top-1 降级：覆盖不足则仍报错，
        # 不扩大候选、不生成新事实。
        try:
            import numpy as np
            kw = _kw_scores(data, query)
            if not len(kw):
                return '', 'embed:%s' % (e,)
            order_kw = np.argsort(-kw)
            best_i = int(order_kw[0])
            best_kw = float(kw[best_i])
            meta = data["meta"].get(str(data["ids"][best_i])) or {}
            head = ' '.join([meta.get('question', '')]
                            + list(meta.get('alt_questions') or [])
                            + list(meta.get('keywords') or []))
            candidate = head + ' ' + (meta.get('content') or '')
            q_terms = set(_bigrams(_expand_query(query)))
            coverage = (len(q_terms & set(_bigrams(candidate))) / max(1, len(q_terms)))
            # kw 阈值随 BM25 改词级 + 标准 log idf 重标定（量级从数百降到十余）
            if best_kw < 6.0 or coverage < 0.30 or not meta.get('content'):
                return '', 'embed:%s' % (e,)
            shown = max(_GUIDE_QA_SCORE_SHOW, min(0.95, coverage))
            text, doc = _format_rag_hit(meta, shown)
            if doc:
                text += f"\n\n【来源】《{doc}》"
            return text, ''
        except Exception:
            return '', 'embed:%s' % (e,)
    scores = data["matrix"] @ qv
    # 向量 + BM25 全量 RRF 融合（输出线仍取向量 top1 余弦分，保持低置信闸门语义）
    candidate_n = max(max_results, 6)
    try:
        order, bm25_ids = _hybrid_order(data, query, scores, candidate_n)
    except Exception:
        order, bm25_ids = list(np.argsort(-scores)[:candidate_n]), set()
    top = max((float(s) for s in scores), default=0.0)
    if not len(order) or top < _GUIDE_QA_SCORE_SHOW:
        # 增强①：低置信闸门触发多查询改写（RRF 融合）；命中则就地提升、复用下方格式化
        if _agent_enhance_on():
            mq = _multi_query_retrieve(query, max_results)
            if mq:
                order2, scores2 = mq
                top2 = max((float(scores2[i]) for i in order2), default=0.0)
                if top2 >= _GUIDE_QA_SCORE_SHOW:
                    order, scores, top = order2, scores2, top2
        # 仍未命中（或增强关/失败）→ 维持低置信回落
        if not len(order) or top < _GUIDE_QA_SCORE_SHOW:
            return '', 'low:%.2f' % top
    else:
        # 增强③：命中但处置信边缘 [SHOW, SHOW+MARGIN) 时，轻量自检是否真正覆盖问题，
        # 不足则用「问题+缺口」补充检索（复用多查询改写）。高分命中时零额外开销。
        if _agent_enhance_on() and top < _GUIDE_QA_SCORE_SHOW + _SELFCHECK_MARGIN:
            supp = _selfcheck_supplement(query, list(order), scores, max_results, data)
            if supp:
                order, scores, top = supp
    order = _semantic_rerank(query, list(order), scores, data)
    order = _unit_consistent_order(query, order, data)[:max_results]
    # 框架类问法放行后，精确命中块仍是最高置信度索引，钉到首位，
    # 防止相邻段落抢位；其余块作为分支/对应证据补充。
    if exact and exact.get('content'):
        exact_id = exact.get('id')
        pinned, rest = [], []
        for i in order:
            if str(data["ids"][i]) == str(exact_id):
                pinned.append(i)
            else:
                rest.append(i)
        if not pinned:
            for raw_id in data["ids"]:
                if str(raw_id) == str(exact_id):
                    pinned.append(int(np.where(data["ids"] == raw_id)[0][0]))
                    break
        order = pinned + rest
    lines, titles = [], []
    pinned_ids = set()
    if exact and exact.get('content'):
        pinned_ids = {str(exact.get('id'))}
    for i in order:
        sc = float(scores[i])
        cid = str(data["ids"][i])
        # 精确命中块（人工索引）与 BM25 词汇强命中块即使向量分略低于输出线也保留；
        # 其余低于输出线的片段仍丢弃，避免弱相关噪声灌进回答。
        if sc < _GUIDE_QA_SCORE_SHOW and cid not in pinned_ids and cid not in bm25_ids:
            continue
        m = data["meta"].get(str(data["ids"][i]))
        if not m:
            continue
        line, doc = _format_rag_hit(m, sc)
        if doc and doc not in titles:
            titles.append(doc)
        lines.append(line)
    if not lines:
        return '', 'low:%.2f' % top
    if titles:
        lines.append("【来源】" + "、".join(f"《{t}》" for t in titles))
    return "\n\n".join(lines), ''


class GuideRAG(BaseTool):
    """【攻略问答】对具体攻略问题做知识库向量检索，返回最相关的攻略片段原文与来源篇目。
    仅定位与检索，不生成内容（内容由 calc_flow 交 LLM 依据片段组织）。"""
    name = 'guide_rag'
    description = ('【攻略问答】具体攻略问题的知识库向量检索（如 遭遇战嘉波怎么打/前期练级点哪个经验高/'
                   '穿甲和高爆区别/自选装备三推荐啥/指自选装备二呢）。装备自选的一二三型问题即使未写 SSR 也用本工具。'
                   '参数：query（用户完整问题原话）、max_results（返回片段数，默认4）。'
                   '返回最相关攻略片段原文及来源篇目；笼统要攻略清单请改用 query_guide。')
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '用户的完整问题原话'},
            'max_results': {'type': 'integer', 'description': '返回片段数，默认 4', 'default': 4},
        },
        'required': ['query'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        query = (params.get('query') or '').strip()
        try:
            max_results = max(1, min(int(params.get('max_results') or 4), 8))
        except (ValueError, TypeError):
            max_results = 4
        if not query:
            return "请输入要检索的攻略问题。"
        text, reason = _guide_rag_search(query, max_results)
        if text:
            return text
        if reason == 'nobuild':
            return (f"攻略问答库未构建（缺 {_GUIDE_QA_JSON} 或向量库）。"
                    f"请先运行 build/guide_qa_build.py 与 build/guide_qa_embed.py。")
        if reason.startswith('embed:'):
            from llm.llm_client import API_KEY_VAR
            return (f"查询向量化失败（请检查 {API_KEY_VAR} / EMBEDDING_MODEL 配置）："
                    f"{reason[6:]}。")
        # 最高分都够不到输出线 → 不贴弱相关片段，直接提示自行查阅
        top = reason[4:] if reason.startswith('low:') else ''
        tail = f"（最高相似度 {top}）" if top else ""
        return (f"命中不足：攻略库中未找到与「{query}」足够相关的内容，"
                f"阿卡莉回复不太清楚{tail}。建议指挥官自行查阅相关攻略。")


# ======================================================================
# 舰灵「点名片段」检索（2026-09-17）：对比 / 评估处理器的取材入口
# ----------------------------------------------------------------------
# 与 GuideRAG 的分工：GuideRAG 答的是「这个**问题**怎么解」（问题为中心）；
# 本函数答的是「知识库里**哪些片段点名了这位舰灵**、讲了她的什么」（实体为中心）。
#
# 为什么不能只用向量检索：实测（2026-09-17，现场诊断脚本）——
# 「齐柏林伯爵 培养」这类查询最高分 0.49~0.57 都够不到 0.50 输出线，且召回的
# 多是「练级点位表」这类**不含她名字**的片段；但语料里其实有三处**真点名**她
# （萌新入坑攻略「次优先推荐改…齐柏林伯爵（航母）」、PVE 配队思路「齐柏林伯爵
# 建造，档案打捞…改后更强力」、萌新推图「齐柏林伯爵因前排技能范围大…可特殊对待」）。
# 只看向量结果 → 会把「有内容」的她判成「暂时不支持」，属于**漏答**。
# 故此处做**三路并集召回**：
#   ① 向量检索（走 _expand_query 实体归一化，能召回用昵称写的片段，如 太太=列克星敦·改）
#   ② 攻略 QA 语料全量扫名（兜底：向量分不够但名字确实在库里，且能按出现次数排序）
#   ③ 篇目摘要点名（_load_guide_kb 的 summary，供前端出「篇目卡片」）
# 三路都只保留「名字/别名附近出现**描述性词**」的片段——把「名字被列进名单表格」
# 这类假命中挡在门外；最终取舍仍由 LLM 在「只依据片段」约束下完成（见 calc_flow）。
#
# 覆盖率和实际回答可用 `python 调试/随机测试.py --suite qa`
# 从当前 QA 库随机取样复测，并将证据评分保存为 JSONL。
# ======================================================================

# 描述性判据（2026-09-25 改）：替掉硬编码描述词表 _MENTION_DESC_RE——
# 用 jieba 词性标注判断「这段在讲她」而不是「把她排进名单」：名字附近出现
# **动词类**（推荐/建造/培养/对待/值得……等动作）才算描述；纯名词并列（名单/阵容/
# 表格）不算。判据来自语法，不再维护词表。
# 只取动词类（v/vn/vd）：9 条样本实测对「名单/操作轴 vs 描述」完全分离；
# 纳入形容词/副词会被「大范围」「通常」这类词误判（把名单/操作轴当成描述）。
_MENTION_DESC_POS = frozenset(('v', 'vn', 'vd'))
_MENTION_POS_CACHE = {}


def _desc_words(text: str):
    """text 里的描述性词集合（动词/形容词/副词）；带缓存。"""
    t = (text or '').strip()
    if not t:
        return frozenset()
    cached = _MENTION_POS_CACHE.get(t)
    if cached is not None:
        return cached
    try:
        import jieba.posseg as _ps
        _jieba_mod()  # 先注入用户词典，保证专名完整成词
        words = frozenset(w.word for w in _ps.cut(t) if w.flag in _MENTION_DESC_POS)
    except Exception:
        words = frozenset()
    if len(_MENTION_POS_CACHE) > 4096:
        _MENTION_POS_CACHE.clear()
    _MENTION_POS_CACHE[t] = words
    return words
# 实体为中心的检索查询（单条，控时延；召回兜底靠全语料扫名）
_MENTION_QUERY = '%s 练级 配队 阵容推荐 强度 获取方式 定位'
# 摘录窗口：名字所在**行**过长时，只取名字附近 ±win 字（而不是整段 chunk）。
# 为什么必须做摘录：QA chunk 最长数千字（遭遇战「文字轴」动辄 3~5000 字），
# 在整段上做「名字附近有描述词」判定必然误命中——实测（2026-09-17）
# 「044S 780通常后苍龙企业780大」这类操作序列会把真正的描述句挤下去。
_MENTION_EXCERPT_WIN = 90
# 紧窗（在摘录内 ±20 字）命中 = 「这句话就是在讲她」；只有整条摘录命中 = 弱相关。
_MENTION_PROX_WIN = 20
# 单字舰名（雷/响/鹰/晓/电）的紧窗更窄——它们在长句里极易被词组吞掉（鹰击/飞鹰）
_MENTION_CHAR_WIN = 14
# 操作时序表噪声：摘录里成串的「数字+S」（135S/121S/092S…）说明这是**操作轴**，
# 不是对舰灵的说明——哪怕里面出现名字也不该当作简介素材。
_MENTION_AXIS_RE = re.compile(r'\d{2,3}S')
_MENTION_AXIS_MAX = 2
# 扫名路的基准分与加权（向量路另加 _MENTION_RAG_BONUS，因为召回本身已带相关性）
_MENTION_BASE = 0.60
_MENTION_PROX_BONUS = 0.35
_MENTION_RAG_BONUS = 0.45


def _mention_aliases(name: str):
    """名字 + 指向它的别名（≥2 字，防单字误命中）。最多 12 个。

    别名很有价值：语料里大量用昵称写（780=齐柏林·改、太太=列克星敦·改），
    只认正规名会漏掉「大范围AOE：胡德、780」这类**真正讲她定位**的片段。
    """
    out = [name]
    if '·' in name:
        out.append(name.replace('·', ''))
    try:
        nicks = _load_nick_map()
    except Exception:
        nicks = {}
    bare = name.replace('·', '')
    for a, canon in (nicks or {}).items():
        if len(a) < 2 or a in out or not canon:
            continue
        if canon == name or canon.replace('·', '') == bare:
            out.append(a)
            if len(out) >= 12:
                break
    return out


def _mention_excerpt(content: str, alias: str, win: int = _MENTION_EXCERPT_WIN) -> str:
    """取名字所在的**行**作为摘录；该行过长时只取名字附近 ±win 字。"""
    content = content or ''
    for line in content.split('\n'):
        if alias not in line:
            continue
        line = line.strip()
        if len(line) <= 2 * win + len(alias):
            return line
        i = line.find(alias)
        return line[max(0, i - win): i + win + len(alias)].strip()
    i = content.find(alias)
    if i < 0:
        return ''
    return content[max(0, i - win): i + win + len(alias)].strip()


def _mention_excerpt_level(exc: str, alias: str) -> str:
    """摘录的相关档位：'prox' 名字紧邻描述词 ／ 'wide' 摘录里有描述词 ／ '' 只是被列进名单。"""
    if not exc:
        return ''
    if len(_MENTION_AXIS_RE.findall(exc)) >= _MENTION_AXIS_MAX:
        return ''
    m = re.search(re.escape(alias), exc)
    if not m:
        return ''
    # 纯数字昵称（780=齐柏林·改）的误命中守卫：「攻击倍率相同（780%）」不是舰名
    if alias.isdigit() and exc[m.end():m.end() + 1] in ('%', '％'):
        return ''
    win = _MENTION_CHAR_WIN if len(alias) <= 1 else _MENTION_PROX_WIN
    seg = exc[max(0, m.start() - win): m.end() + win]
    if _desc_words(seg):
        # 单字舰名的词组防误切（复用 PVP 解析既有的守卫表：鹰/响/晓/电/雷）
        g = _PVP_1CHAR_GUARD.get(alias)
        if len(alias) <= 1 and g:
            lg, rg = g
            if m.start() > 0 and exc[m.start() - 1] in lg:
                return ''
            if m.end() < len(exc) and exc[m.end()] in rg:
                return ''
        return 'prox'
    if len(alias) <= 1:
        return ''
    return 'wide' if _desc_words(exc) else ''


def _best_mention(content: str, aliases):
    """从一段语料里取这批别名的最强点名摘录 → (摘录, 档位)。档位 '' = 不算点名。"""
    best = ('', '')
    for a in aliases:
        if a not in content:
            continue
        exc = _mention_excerpt(content, a)
        lv = _mention_excerpt_level(exc, a)
        if lv == 'prox':
            return exc, 'prox'
        if lv == 'wide' and not best[0]:
            best = (exc, 'wide')
    return best


def find_ship_mentions(name: str, limit: int = 5, use_vector: bool = True):
    """检索「点名某位舰灵」的知识库片段（三路并集，见上方注释）。

    use_vector=False 只走全语料扫名（供批量核验用——全量 200 位逐个走向量检索
    要打 200 次 embedding，太贵；扫名路的别名集与向量路一致，召回是超集，
    所以扫名结果就是覆盖率的**可靠下界**）。

    mentions 里存的是**摘录**（名字所在行／名字附近 ±90 字），不是整段 chunk——
    喂给 LLM 的应是一句句「讲她的话」，而不是裹着名字的长文档。

    返回 {'name': 正规名,
          'mentions': [{'doc','section','content','via','score'}]（按 score 降序、去重、截断）}
    mentions 为空 = 知识库里没有任何点名她的描述（调用方按「暂时不支持该角色」处理）。
    """
    name = (name or '').strip()
    res = {'name': name, 'mentions': []}
    if not name:
        return res
    aliases = _mention_aliases(name)
    got = {}

    def _add(doc, section, exc, via, score):
        if not exc:
            return
        # 微调：摘录里不同描述词的种数越多，越可能真在讲她（防止「名单式」并列拿同分）
        score += 0.03 * min(len(_desc_words(exc)), 5)
        key = (doc, section, exc[:48])
        cur = got.get(key)
        if cur is None:
            got[key] = {'doc': doc, 'section': section, 'content': exc,
                        'via': via, 'score': score}
        else:
            if score > cur['score']:
                cur['score'] = score
                cur['content'] = exc
            if via not in cur['via']:
                cur['via'] += '+' + via

    # ① 向量路（能召回用昵称写的片段）
    try:
        text, _reason = ('', 'skip') if not use_vector else \
            _guide_rag_search(_MENTION_QUERY % name, max_results=6)
        for doc, section, content, _sim in _split_rag_segments(text):
            exc, lv = _best_mention(content, aliases)
            if lv:
                _add(doc, section, exc, 'rag',
                     _MENTION_BASE + (_MENTION_PROX_BONUS if lv == 'prox' else 0.0)
                     + _MENTION_RAG_BONUS)
    except Exception:
        pass

    # ② 全语料扫名（兜底漏召回；专名是精确匹配，比向量分可靠）
    try:
        data = _load_guide_qa()
        for c in (data or {}).get('meta', {}).values():
            content = c.get('content') or ''
            if not any(a in content for a in aliases):
                continue
            exc, lv = _best_mention(content, aliases)
            if lv:
                _add(_clean_doc_name(c.get('source') or ''), c.get('section') or '',
                     exc, 'scan',
                     _MENTION_BASE + (_MENTION_PROX_BONUS if lv == 'prox' else 0.0))
    except Exception:
        pass

    res['mentions'] = sorted(got.values(), key=lambda x: -x['score'])[:max(1, limit)]
    return res


# ======================================================================
# PVP 角色推荐（争锋参考阵容统计）：解析《7.1 争锋竞技场简易百科》
# 「三、参考阵容推荐」章节，按 场质(金/紫/蓝) × 体量(小/中/大) 统计角色出现频次，
# 自动从底层 data.json 识别稀有度，高频者即推荐角色（后端固定输出，不走 LLM）。
# ======================================================================

_PVP_GUIDE_PATH = os.path.join(KB_DIR, "攻略_md", "7.1 争锋竞技场简易百科.md")
_PVP_DATA_JSON = os.path.join(KB_DIR, "底层数据", "产物", "data.json")
# 攻略常用昵称 → data.json 正规舰名（仅收无歧义的；两人组合昵称如"吃喝/黎北"不映射）
_PVP_ALIAS = {
    '小拉菲改': '拉菲·改', '小拉菲': '拉菲(DD-724)',
    '贝法': '贝尔法斯特', '明尼': '明尼阿波利斯', '北卡': '北卡罗来纳',
}
# 单字舰名的词组防误切：左邻/右邻出现这些字时视为普通词组（鱼雷/影响/拂晓/闪电…），不计舰名
_PVP_1CHAR_GUARD = {
    '雷': ('鱼水扫布避炮雷', '击巡达区电动雨鸣暴'),
    '响': ('影音声', '起'),
    '鹰': ('隼飞雄雏', ''),
    '晓': ('破拂', ''),
    '电': ('闪雷发导放', ''),
}
_PVP_CACHE = {"mtime": None, "data": None, "rarity_mtime": None, "rarity": None}


def _pvp_rarity_map():
    """舰名 → 稀有度（data.json，mtime 感知缓存）；含去中点变体（夕立·改 → 夕立改）。"""
    if _PVP_CACHE["rarity_mtime"] == (os.path.getmtime(_PVP_DATA_JSON)
                                      if os.path.exists(_PVP_DATA_JSON) else None):
        return _PVP_CACHE["rarity"]
    rarity = {}
    if os.path.exists(_PVP_DATA_JSON):
        with open(_PVP_DATA_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        for name, item in raw.items():
            r = (item or {}).get("rarity", "")
            rarity[name] = r
            if "·" in name:
                rarity.setdefault(name.replace("·", ""), r)
    _PVP_CACHE["rarity_mtime"] = os.path.getmtime(_PVP_DATA_JSON) if os.path.exists(_PVP_DATA_JSON) else None
    _PVP_CACHE["rarity"] = rarity
    return rarity


def _pvp_parse_lineups():
    """解析攻略参考阵容章节 → {(场质, 体量): 文本}，mtime 感知缓存。
    章节结构：## （一）金场 → ### 1.金小船 / 2.金中船 / 3.金大船（紫/蓝同构）。"""
    if not os.path.exists(_PVP_GUIDE_PATH):
        return {}
    mt = os.path.getmtime(_PVP_GUIDE_PATH)
    if _PVP_CACHE["mtime"] == mt:
        return _PVP_CACHE["data"]
    with open(_PVP_GUIDE_PATH, encoding="utf-8") as f:
        lines = f.read().splitlines()
    start = end = None
    for i, ln in enumerate(lines):
        if start is None and re.match(r'^#\s*三、\s*参考阵容推荐', ln):
            start = i + 1
        elif start is not None and re.match(r'^#\s+', ln):
            end = i
            break
    blocks, cur = {}, None
    for ln in (lines[start:end] if start is not None else []):
        if ln.startswith('#'):
            m = re.search(r'([金紫蓝])([小中大])(?:船|场)', ln)
            cur = (m.group(1), m.group(2)) if m else None
            continue
        if cur is not None:
            blocks.setdefault(cur, []).append(ln)
    data = {k: '\n'.join(v) for k, v in blocks.items()}
    _PVP_CACHE["mtime"] = mt
    _PVP_CACHE["data"] = data
    return data


def _pvp_count_block(text, names_sorted):
    """在单个场次文本里统计舰名出现次数（长名优先替换，防「绫波」吃掉「绫波改」；
    单字舰名按 _PVP_1CHAR_GUARD 加邻字守卫，防吃进 鱼雷/影响 等词组）。"""
    counts = {}

    def _pattern(n):
        g = _PVP_1CHAR_GUARD.get(n)
        if g and len(n) == 1:
            left = '(?<![' + re.escape(g[0]) + '])' if g[0] else ''
            right = '(?![' + re.escape(g[1]) + '])' if g[1] else ''
            return left + re.escape(n) + right
        return re.escape(n)

    def _repl(m):
        counts[m.group(0)] = counts.get(m.group(0), 0) + 1
        return '□'

    for n in names_sorted:
        pat = _pattern(n)
        if re.search(pat, text):
            text = re.sub(pat, _repl, text)
    return counts


class PvpRoleRecommend(BaseTool):
    """【PVP角色推荐】从争锋竞技场攻略的参考阵容统计各场次高频角色，自动附稀有度。"""
    name = 'pvp_role_recommend'
    description = ('【PVP角色推荐】按争锋竞技场（PVP）参考阵容统计角色出场频次，'
                   '自动识别稀有度与场次（金/紫/蓝 × 小/中/大船）。'
                   '参数：query（用户原话，可含 场质/体量 过滤词，如"金中"）。')
    parameters = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': '用户原话'},
        },
        'required': ['query'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        query = (params.get('query') or '').strip()
        blocks = _pvp_parse_lineups()
        if not blocks:
            return "参考阵容数据不可用（缺《7.1 争锋竞技场简易百科》）。"
        rarity = _pvp_rarity_map()
        _rec_app = self.cfg.get('app')   # 用于过滤信息不全的角色
        # 匹配名表：正规舰名 + 攻略昵称，长名优先（别名命中折算回正规名再取稀有度）
        names_sorted = sorted(set(rarity) | set(_PVP_ALIAS), key=len, reverse=True)
        # 用户过滤词：场质（金/紫/蓝）与体量（小/中/大）
        q_filter = [q for q in '金紫蓝' if q in query]
        s_filter = [s for s in '小中大' if s in query]
        keys = sorted(blocks, key=lambda k: ('金紫蓝'.index(k[0]), '小中大'.index(k[1])))
        out_lines = ['以下是争锋竞技场参考阵容的角色统计（来源：《争锋竞技场简易百科·参考阵容推荐》），出现越多越主流：']
        multi = {}   # 跨场次泛用统计：正规名 → 出现场次数
        any_row = False
        for q, s in keys:
            if q_filter and q not in q_filter:
                continue
            if s_filter and s not in s_filter:
                continue
            counts = _pvp_count_block(blocks[(q, s)], names_sorted)
            if not counts:
                continue
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            parts = []
            for n, c in ranked:
                real = _PVP_ALIAS.get(n, n)
                # 信息不全的角色不推荐（缺任何基础信息视同"没有该角色"，推荐了也查不了/算不了）
                if _rec_app is not None and real in _rec_app.storage \
                        and not _rec_app.is_ship_complete(real):
                    continue
                r = rarity.get(real, '')
                tag = f'（{r}）' if r else ''
                parts.append(f'{n}{tag}×{c}')
                multi[real] = multi.get(real, 0) + 1
            out_lines.append(f'◆ {q}·{s}船场：' + '、'.join(parts))
            any_row = True
        if not any_row:
            out_lines.append('（该场次参考阵容以图片为主，文本未列出具体角色，建议查阅原攻略。）')
        vers = [n for n, c in sorted(multi.items(), key=lambda kv: -kv[1]) if c >= 3]
        if vers:
            out_lines.append(f'——多场次泛用（≥3 种场次在列）：{"、".join(vers[:8])}')
        out_lines.append('提示：天气与赛季会影响具体强度，建议按阵容思路自行微调（回复「争锋攻略」可看原攻略）。')
        return '\n'.join(out_lines)


# ======================================================================
# 弹种机制概率速查（确定性数值，不走 RAG/LLM）：8.3 弹种与护甲机制表硬编码。
# 穿甲弹 → 未击穿/击穿/过穿 三概率；高爆弹 → 点燃几率。数据源与
# 《8.3 弹种与护甲机制》攻略一致，表变更时两侧同步。
# ======================================================================

# 穿甲命中表：{护甲: {舰种: (未击穿, 击穿, 过穿)}}（百分比数值）
_PEN_TABLE = {
    '轻甲': {'驱逐': (30, 70, 0), '轻巡': (20, 80, 0), '重巡': (0, 40, 60), '战列': (0, 30, 70)},
    '中甲': {'驱逐': (35, 65, 0), '轻巡': (30, 70, 0), '重巡': (10, 80, 10), '战列': (0, 60, 40)},
    '重甲': {'驱逐': (90, 10, 0), '轻巡': (80, 20, 0), '重巡': (60, 40, 0), '战列': (20, 80, 0)},
}
# 高爆点燃表：{护甲: {舰种: 点燃几率}}
_IGNITE_TABLE = {
    '轻甲': {'驱逐': 15, '轻巡': 20, '重巡': 25, '战列': 40},
    '中甲': {'驱逐': 5, '轻巡': 10, '重巡': 15, '战列': 20},
    '重甲': {'驱逐': 5, '轻巡': 5, '重巡': 10, '战列': 10},
}

# 克制系数查表：与公式层同一数据源（mappings.json → DAMAGE_MODIFIER），
# 禁止在此硬编码第二份数值（曾出现 LLM 口胡「高平两用炮对重甲 0.8」，真实为 0.6）。
try:
    from core.formula_engine import compute_modifier as _compute_modifier
except Exception:  # pragma: no cover - 开发者端独立运行等场景
    _compute_modifier = None

# 特殊武器整体词（克制表键为 武器||装甲，弹种无关；别名归一，按长度降序防子串误吞）
_SPECIAL_WEAPON_WORDS = (
    ('高平两用炮', '高平炮'), ('两用炮', '高平炮'), ('高平炮', '高平炮'),
    ('三式弹', '三式弹'), ('近战', '近战'), ('鱼雷机', '鱼雷机'),
    ('轰炸机', '轰炸机'), ('雷击', '雷击'), ('鱼雷', '鱼雷'), ('轰炸', '轰炸机'),
)
_SHIP_GUN_PREFIX = (('战列', '战列炮'), ('驱逐', '驱逐炮'), ('轻巡', '轻巡炮'), ('重巡', '重巡炮'))

# 弹种机制总览（穿甲/高爆/通常 区别）：确定性说明，避免「弹种机制」类问句回落 LLM 游戏简介口胡
_SHIP_MAIN_GUN = {'驱逐': '驱逐炮', '轻巡': '轻巡炮', '重巡': '重巡炮', '战列': '战列炮',
                 '战巡': '战列炮', '航战': '战列炮', '重炮': '战列炮', '雷巡': '轻巡炮'}

_PVP_SHIP_AMMO_CACHE = {"mtime": None, "data": None}


def _pvp_ship_ammo_map():
    """舰名 → {ship_type, ammo_type, na_kinds}（data.json，mtime 感知缓存）；含去中点变体。
    na_kinds = 通常技伤害类型（去重保序）：'轰炸机' / '雷击'（鱼雷/鱼雷机/雷击 归一为雷击），
    供航系角色判 轰航/雷航（用户模板 2026-09-10）。"""
    _mtime = os.path.getmtime(_PVP_DATA_JSON) if os.path.exists(_PVP_DATA_JSON) else None
    if _PVP_SHIP_AMMO_CACHE["mtime"] == _mtime:
        return _PVP_SHIP_AMMO_CACHE["data"]
    mp = {}
    if os.path.exists(_PVP_DATA_JSON):
        with open(_PVP_DATA_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        for name, item in raw.items():
            st = (item or {}).get("ship_type", "")
            am = (item or {}).get("ammo_type", "")
            kinds = []
            for seg in ((item or {}).get("normal_attacks") or []):
                dt = (seg.get("preset_damage_type") or seg.get("weapon_type") or "")
                if "轰炸" in dt:
                    k = "轰炸机"
                elif ("鱼雷" in dt) or ("雷击" in dt):
                    k = "雷击"
                else:
                    continue
                if k not in kinds:
                    kinds.append(k)
            mp[name] = {"ship_type": st, "ammo_type": am, "na_kinds": kinds}
            if "·" in name:
                mp.setdefault(name.replace("·", ""), {"ship_type": st, "ammo_type": am, "na_kinds": kinds})
    _PVP_SHIP_AMMO_CACHE["mtime"] = _mtime
    _PVP_SHIP_AMMO_CACHE["data"] = mp
    return mp


def _match_ship_name(q: str):
    """从问句中识别已知舰名（最长优先，防子串误吞）；含 ·改→改 与昵称归一。返回正规名或 ''。"""
    mp = _pvp_ship_ammo_map()
    if not mp:
        return ''
    norm = q
    for nick, real in _PVP_ALIAS.items():
        if nick in norm:
            norm = norm.replace(nick, real)
    norm2 = norm.replace("·", "")
    for name in sorted(mp.keys(), key=len, reverse=True):
        if name in norm or name.replace("·", "") in norm2:
            return name
    return ''


def _resolve_ship_weapon_ammo(q: str):
    """问句含舰名 → (武器类, 弹种)，如「雪风对重甲」→ ('驱逐炮','高爆')；否则 ('','')。"""
    name = _match_ship_name(q)
    if not name:
        return ('', '')
    rec = _pvp_ship_ammo_map().get(name)
    if not rec:
        return ('', '')
    weapon = _SHIP_MAIN_GUN.get(rec.get("ship_type", ""), "")
    return (weapon, rec.get("ammo_type", ""))


_AIR_SHIP_TYPES = ('航母', '轻母', '装母', '水母')


def _ship_modifier_reply(name: str, armor: str) -> str:
    """指定舰名的克制系数应答（用户模板 2026-09-10）：
    - 非航系：先回复 舰种（弹种），再给对应克制系数；
    - 航系（航母/轻母/装母/水母）：按通常技伤害类型判 轰航（轰炸）/雷航（鱼雷）并告知；
      通常技兼有轰炸与鱼雷 → 输出两种系数，不判型。
    组不出内容返回 ''（回落上层）。"""
    rec = _pvp_ship_ammo_map().get(name) or {}
    st = rec.get("ship_type", "")
    ammo = rec.get("ammo_type", "")
    if st in _AIR_SHIP_TYPES:
        kinds = rec.get("na_kinds") or []
        if not kinds:
            return ''
        lines = [f'{name}：{st}']
        if len(kinds) == 1:
            k = kinds[0]
            line = _modifier_line(k, '', armor)
            if not line:
                return ''
            lines.append(f'通常技为{"轰炸" if k == "轰炸机" else "鱼雷"}伤害，属于{"轰航" if k == "轰炸机" else "雷航"}')
            lines.append(line)
        else:
            lines.append('通常技兼有轰炸与鱼雷伤害')
            for k in kinds:
                line = _modifier_line(k, '', armor)
                if line:
                    lines.append(line)
        return '\n'.join(lines)
    # 非航系：舰种 → 主炮武器 + 弹种
    weapon = _SHIP_MAIN_GUN.get(st, '')
    if not weapon:
        return ''
    line = _modifier_line(weapon, ammo, armor)
    if not line:
        return ''
    head = f'{name}：{st}（{ammo}弹）' if ammo else f'{name}：{st}'
    return f'{head} → {line}'


def _ammo_mech_modifier(q: str):
    """克制系数解析 → 直接查表返回固定文案；解析不出返回 ''（回落上层）。

    支持句式：'{特殊武器|舰炮前缀[炮]}{弹种?} 对 {轻/中/重}甲 克制[系数/修正/系数多少]'
    例：'高平两用炮对重甲克制系数'→高平炮 0.6；'战列穿甲对重甲克制'→1.2。
    特殊武器（高平炮/三式弹/近战/雷击/鱼雷机/轰炸机等）按 武器||装甲 直查，弹种忽略；
    舰炮按 (武器, 弹种, 甲) 查。数值与公式层一致，绝不自行编造。
    """
    if not q or not re.search(r'克制|修正|系数|伤害|打|增伤', q):
        return ''
    armor = next((a for a in ('轻甲', '中甲', '重甲') if a in q), None)
    if not armor:
        return ''
    # 1) 特殊武器（含别名）
    for word, weapon in _SPECIAL_WEAPON_WORDS:
        if word in q:
            ammo = next((a for a in ('穿甲', '高爆', '通常') if a in q), '')
            return _modifier_line(weapon, ammo, armor)
    # 2) 舰炮前缀 + 弹种
    for prefix, weapon in _SHIP_GUN_PREFIX:
        if prefix in q:
            ammo = next((a for a in ('穿甲', '高爆', '通常') if a in q), '')
            if ammo:
                return _modifier_line(weapon, ammo, armor)
            return ''
    # 3) 舰名 → 用户模板应答（2026-09-10）：先报 舰种（弹种）/航系判轰航雷航，再给系数
    ship_name = _match_ship_name(q)
    if ship_name:
        rep = _ship_modifier_reply(ship_name, armor)
        if rep:
            return rep
    return ''


def _modifier_line(weapon: str, ammo: str, armor: str) -> str:
    """按公式层查表组克制系数应答（特殊武器 ammo='' → 兜底 武器||装甲）。"""
    if _compute_modifier is None:
        return ''
    try:
        val = float(_compute_modifier(weapon, ammo, armor))
    except Exception:
        return ''
    specials = {w for _, w in _SPECIAL_WEAPON_WORDS}
    if weapon in specials:
        label = weapon            # 特殊武器克制与弹种无关，只显示武器名
    elif ammo:
        label = f'{weapon[:-1]}{ammo}' if weapon.endswith('炮') else f'{weapon}{ammo}'
    else:
        label = weapon
    hint = '（>1 对该护甲有增伤修正，<1 被削减，1.0 无修正）'
    return f'{label}对{armor}的克制系数：{val:g}。{hint}'


class AmmoMechQA(BaseTool):
    """【弹种机制速查】穿甲击穿/高爆点燃概率 + 克制系数，确定性数值。"""
    name = 'ammo_mech_qa'
    description = ('【弹种机制速查】查询穿甲弹未击穿/击穿/过穿概率、高爆弹点燃几率、'
                   '武器(弹种)对装甲的克制系数（如「战列穿甲打重甲的击穿几率」'
                   '「高爆驱逐对轻甲的点燃几率」「高平两用炮对重甲的克制系数」）。'
                   '参数：query（用户原话）。解析不出完整条件返回空串。')
    parameters = {
        'type': 'object',
        'properties': {'query': {'type': 'string', 'description': '用户原话'}},
        'required': ['query'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        q = (params.get('query') or '').strip()
        # 克制/系数/修正 → 克制查表（确定性数值，防 LLM 口胡）
        if re.search(r'克制|修正|系数', q):
            mod = _ammo_mech_modifier(q)
            if mod:
                return mod
            return ''
        ship = next((s for s in ('驱逐', '轻巡', '重巡', '战列')
                     if re.search(s + r'[舰]?', q)), None)
        armors = [a for a in ('轻甲', '中甲', '重甲') if a in q]
        # 「燃烧」视为「点燃」同义；穿甲/高爆按弹种关键词识别
        ammo = '穿甲' if '穿甲' in q else ('高爆' if '高爆' in q or '燃烧' in q or '点燃' in q else None)
        if not (ship and armors and ammo):
            return ''
        if ammo == '穿甲':
            rows = []
            for armor in armors:
                miss, pen, over = _PEN_TABLE[armor][ship]
                rows.append(f'{ship}穿甲对{armor}：未击穿 {miss}%、击穿 {pen}%、过穿 {over}%。')
            return ('\n'.join(rows) + '\n'
                    '「未击穿」必定不会暴击；「击穿」必定暴击；「过穿」正常计算是否暴击。')
        rows = [f'{ship}高爆对{armor}的点燃几率：{_IGNITE_TABLE[armor][ship]}%。'
                for armor in armors]
        return ('\n'.join(rows) + '\n'
                '每颗子弹（hit）命中后单独判定点燃，点燃成功后每秒造成 10%*炮击值的伤害，持续 10 秒。'
                + ('（注意：只有高爆弹会点燃，穿甲/通常弹点燃几率为 0）' if '穿甲' in q else ''))


def _clean_kb_chunk_content(text):
    """清理知识块内容：移除 作者署名/致谢名单/观前提示/本攻略所属/文档小标题 等
    与知识无关的元信息。"""
    import re as _re
    lines = (text or '').split('\n')
    out = []
    skip = False
    _num_short = _re.compile(r'^\d{1,2}[、.．]\S{0,30}$')
    _num_cn = _re.compile(r'^[一二三四五六七八九十〇]+[、.．]\S{0,24}$')
    for i, ln in enumerate(lines):
        s = ln.strip()
        if skip:
            if not s:
                skip = False
            continue
        if s.startswith('在此感谢') or s.startswith('感谢') and '提供' in s:
            skip = True
            continue
        if _re.match(r'^作者[:：]', s):
            continue
        if '本攻略为' in s and ('所有' in s or '版权所有' in s):
            continue
        if s.startswith('【观前提示】') or s.startswith('【使用说明】') or s.startswith('【读者须知】'):
            skip = True
            continue
        if _re.match(r'^第[一二三四五六七八九十\d]+章', s) and len(s) < 30 and out:
            out.append('')  # 章节标题前留空行
        # 文档小标题（不输出）：markdown 标题 / 前言结语引言概述
        if _re.match(r'^#{1,6}\s+\S', s):
            continue
        if s in ('前言', '结语', '引言', '概述', '简介', '后记', '正文', '目录'):
            continue
        # 数字/汉字序号短行（无句号逗号分号等正文标点）：
        #   下一行仍是序号条目 → 列表内容（保留，如舰灵名单）；否则 → 标题（删除）
        if _num_short.match(s) and not _re.search(r'[。！？；，]', s):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ''
            if not (_num_short.match(nxt) or _num_cn.match(nxt)):
                continue
        if _num_cn.match(s) and not _re.search(r'[。！？；，]', s):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ''
            if not (_num_short.match(nxt) or _num_cn.match(nxt)):
                continue
        if _re.match(r'^[（(][一二三四五六七八九十\d]+[）)]\S{0,20}$', s) and not _re.search(r'[。！？；，]', s):
            continue
        out.append(ln)
    result = chr(10).join(out)
    # 合并多余空行
    result = _re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def _dedup_lines(lines):
    """行级去重：规范化（去标点/空白）后相同、或互为子串（长度≥6）的行只保留首次出现。
    用于跨知识块合并时去除重复陈述。"""
    import re as _re
    seen = []
    out = []
    norm = lambda l: _re.sub(r'[\s，。！？；：、,.!?;:()（）《》<>“”"\'\-]', '', l)
    for ln in lines:
        s = (ln or '').strip()
        if not s:
            out.append('')
            continue
        n = norm(s)
        if not n:
            continue
        dup = False
        for k in seen:
            if k == n or (len(k) > 5 and len(n) > 5 and (k in n or n in k)):
                dup = True
                break
        if not dup:
            seen.append(n)
            out.append(ln)
    return out


def _pick_diverse_chunks(chunks, max_n=2, sim_thresh=0.5):
    """从候选知识块里挑选内容差异大的块（行集 Jaccard 相似度 < thresh），
    避免同一主题抽出的多个片段内容重复。"""
    import re as _re
    def norm_lines(text):
        out = set()
        for ln in (text or '').split('\n'):
            s = _re.sub(r'[\s，。！？；：、,.!?;:()（）《》<>“”"\'\-]', '', ln.strip())
            if len(s) >= 4:
                out.add(s)
        return out
    kept, kept_sets = [], []
    for c in chunks:
        ls = norm_lines(c.get('content') or '')
        if not ls:
            continue
        sim_max = 0.0
        for ks in kept_sets:
            inter = len(ls & ks)
            sim = inter / max(len(ls | ks), 1)
            sim_max = max(sim_max, sim)
        if not kept or sim_max < sim_thresh:
            kept.append(c)
            kept_sets.append(ls)
        if len(kept) >= max_n:
            break
    return kept


def merge_chunks_dedup(chunks, max_len=500):
    """主题介绍用：多知识块 → 各自清理（去标题/元信息/来源标注）→ 行级去重 → 合并文本。
    含图片/案例引用的案例讲解块整体不贴出（聊天里无图可看，且案例路线对玩法介绍无意义）。
    返回纯知识内容（不含〔主题｜来源〕标注）。"""
    parts = []
    for c in chunks:
        content = _clean_kb_chunk_content(c.get('content') or '')
        if not content:
            continue
        if _IMG_REF_RE.search(content):
            continue  # 案例讲解块（"案例请看上图所示"等）不进回答
        parts.append(content.split('\n'))
    if not parts:
        return ''
    merged = []
    for p in parts:
        if merged:
            merged.append('')  # 片段间空行分隔
        merged.extend(p)
    merged = _dedup_lines(merged)
    result = '\n'.join(merged)
    import re as _re
    result = _re.sub(r'\n{3,}', '\n\n', result)
    return _strip_case_refs(result)[:max_len]


def _query_words(text):
    """简单切词：整句 + 2~4 字连续子串（去掉标点/数字段），供 KB 检索打分。"""
    import re as _re
    parts = _re.split(r'[？?！!。，,、；;：:\s]+', text)
    words = []
    for p in parts:
        if not p:
            continue
        words.append(p)
        if len(p) >= 4:
            words.append(p[:2])
            words.append(p[:4])
    seen = set()
    out = []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out

"""技能条件筛选工具（query_ships_by_skill）。

设计原则（用户铁律）：所有查询输出一律来自信息库/工具原文，不走 LLM 逐字生成。
- 条件词规则表（25 种 buff 效果 × 5 类目标 × 弹种/舰种）命中 → 0 token；
- 规则表未命中且明显在问"哪些舰灵具备某能力" → LLM 只把条件翻译成严格校验的短 JSON
  （temperature=0、字段白名单校验，失败即放弃），检索与名单输出仍 100% 由后端从信息库产生；
- 输出仅角色名列表，不贴技能原文。
"""
import re
from qwen_agent.tools.base import BaseTool

# ======================================================================
# 条件词 → 索引键 规则表（规则命中即 0 token，不进 LLM）
# ======================================================================

# 目标词（用户在问题里的说法 → 归一化目标；含敌方：debuff 类效果对象）
_TARGET_ALIASES = {
    '自己': ('自身', '自己', '本人', '本舰'),
    '前排': ('前排', '前卫'),
    '全体': ('全体', '全队', '全员', '我方', '友方', '全舰', '所属舰队', '出战位', '舰队', '队友', '同伴'),
    '后排': ('后排',),
    '敌方': ('敌方', '敌人', '敌军', '对手', '被命中的敌方单位', '敌方单位', '目标单位'),
}

# 加成词（用户说法 → buff 键）
_BUFF_ALIASES = {
    # 回复类
    '回血': ('自奶', '回血', '治疗', '恢复血量', '回复血量', '修理', '维修', '自愈', '治愈', '奶', '加血'),
    '吸血': ('吸血', '生命汲取', '吸取血量', '吸血回复'),
    # 属性数值类
    '炮击': ('炮击',),
    '雷击': ('雷击',),
    '航空': ('航空',),
    '轰炸': ('轰炸',),
    '鱼雷': ('鱼雷',),
    '命中': ('命中',),
    '装填': ('装填',),
    '装甲': ('装甲',),
    '对空': ('对空',),
    '机动': ('机动',),
    '幸运': ('幸运',),
    '索敌': ('索敌',),
    '血量': ('血量上限', '最大血量', '加血量', '血量加成'),
    # 战斗效果类
    '移速': ('移动速度', '移速', '航速', '提速', '加速'),
    '暴击': ('暴击', '暴伤'),
    '护盾': ('护盾', '盾', '护甲盾'),
    '减伤': ('减伤', '免伤', '无敌', '抗揍', '抗伤', '减伤率', '伤害减免'),
    '增伤': ('增伤', '加伤', '伤害加成', '伤害提升'),
    '回避': ('回避', '闪避', '闪避率'),
    '攻速': ('攻速', '攻击速度'),
    '点燃': ('点燃', '灼烧', '燃烧', '持续伤害'),
    '控制': ('眩晕', '沉默', '减速', '定身', '嘲讽', '击退', '拉拽', '聚怪', '冰冻', '麻痹', '混乱'),
    '反伤': ('反伤', '反弹伤害', '反弹'),
}

# 弹种 / 舰种（查询词做包含匹配，键来自数据实际值）
_AMMO_QUERY_WORDS = ('高爆', '穿甲', '鱼雷', '通常')
_SHIP_TYPE_QUERY_WORDS = ('战列', '战巡', '航母', '轻母', '装母', '巡洋', '重巡', '轻巡',
                          '驱逐', '维修', '潜艇', '补给', '重炮', '水母', '雷巡', '航战')

# 技能原文判定：属性数值 buff 须伴随提升/加成类动词；效果类按专属短语
_ATTR_BOOST = r'(提升|增加|加成|提高|强化|加强|增幅|加|\+)'
# 属性类：仅「增加炮击属性」计入（炮击+30%/炮击值提升/提升炮击），
# 排除「炮击伤害提升/炮击伤害加成」等伤害类描述：
# 属性词后紧跟"伤害"不算（负前瞻）；加成词须在属性词 4 字内（炮击值提升/提升我方炮击），
# 隔太远（如"炮击科技…炮击伤害提升"）不算。
_ATTR_PAT = lambda a: r'%s(?!伤害).{0,4}%s|%s.{0,4}%s(?!伤害)' % (a, _ATTR_BOOST, _ATTR_BOOST, a)
_BUFF_PATTERNS = {
    '炮击': (_ATTR_PAT('炮击'),),
    '雷击': (_ATTR_PAT('雷击'),),
    '航空': (_ATTR_PAT('航空'),),
    '轰炸': (_ATTR_PAT('轰炸'),),
    '鱼雷': (_ATTR_PAT('鱼雷'),),
    '命中': (_ATTR_PAT('命中'),),
    '装填': (_ATTR_PAT('装填'),),
    '装甲': (_ATTR_PAT('装甲'),),
    '对空': (_ATTR_PAT('对空'),),
    '机动': (_ATTR_PAT('机动'),),
    '幸运': (_ATTR_PAT('幸运'),),
    '索敌': (_ATTR_PAT('索敌'),),
    '暴击': (_ATTR_PAT('暴击'),),
    '血量': (r'血量.{0,8}(上限|提升|增加|加成|提高)|(上限|提升|增加|加成|提高).{0,8}血量|最大(血量|耐久)',),
    '回血': (r'(回复|恢复|治疗|修理|回血|维修|治愈|补给).{0,12}(血量|耐久|自身|自己|我方|前排|全体|HP|hp|船只)|(血量|耐久).{0,6}(回复|恢复|治疗|回血)',),
    '吸血': (r'吸血|吸取血量|生命汲取|吸血回复',),
    '移速': (r'(移动速度|移速|航速|航行速度).{0,10}(提升|增加|加成|提高|加快|加速|\+|提升至|增加至)|(提升|增加|加成|提高|加快|加速).{0,10}(移动速度|移速|航速|航行速度)',),
    '护盾': (r'护盾|添加「护盾」|获得护盾|护甲盾|能量护盾',),
    '减伤': (r'减伤|免伤|无敌|免疫伤害|伤害减免(?!-\d)',),
    '增伤': (r'增伤|伤害加成|伤害提升|伤害增加|伤害提高',),
    '回避': (r'回避率|闪避率|闪避',),
    '攻速': (r'攻速|攻击速度',),
    '点燃': (r'点燃|灼烧|燃烧|持续伤害',),
    '控制': (r'眩晕|沉默|减速|定身|嘲讽|击退|拉拽|聚怪|冰冻|麻痹|混乱',),
    '反伤': (r'反伤|反弹伤害|反弹',),
}

# 目标语义扩展：问「前排」时，全体型 buff（所属舰队/全队）同样覆盖前排；
# 问「自己」严格只算自身（自奶）；敌方只匹配敌方。
_TARGET_EXPAND = {
    '自己': ('自己',),
    '前排': ('前排', '全体'),
    '后排': ('后排', '全体'),
    '全体': ('全体',),
    '敌方': ('敌方',),
}


def parse_skill_conditions(msg: str) -> dict:
    """把用户中文问题解析为结构化条件（规则表，0 token）。
    返回 {"targets": [...], "buffs": [...], "ammo": [...], "ship_type": [...]}；
    解析不到任何条件时返回 {}。"""
    cond = {}
    targets = [t for t, aliases in _TARGET_ALIASES.items() if any(a in msg for a in aliases)]
    if targets:
        cond['targets'] = targets
    buffs = [b for b, aliases in _BUFF_ALIASES.items() if any(a in msg for a in aliases)]
    if buffs:
        cond['buffs'] = buffs
    ammo = [w for w in _AMMO_QUERY_WORDS if w in msg]
    if ammo:
        cond['ammo'] = ammo
    stype = [w for w in _SHIP_TYPE_QUERY_WORDS if w in msg]
    if stype:
        cond['ship_type'] = stype
    # 「奶妈/治疗」= 必须能奶队伍/队友（治疗他人）→ 限目标为友方（全体桶含 我方/舰队/队友）
    if re.search(r'奶妈|治疗队友|治疗队伍|奶队友|奶全队|奶队伍|奶全团|群奶', msg) and 'targets' not in cond:
        cond['targets'] = ['全体']
    # 语义层级（用户约定）：「自奶」= 自我回血（严格，目标=自己）；
    # 「回血船」= 大类（any：自我回复 + 他人治疗 + 回复天赋 都算）。
    # 「奶自己/回自己/回复自身/恢复自身」等已由"自己/自身"目标词命中，这里补不含目标词的「自奶/自我回血」
    if re.search(r'自奶|自我回血|自我恢复|自回', msg):
        cond.setdefault('targets', [])
        if '自己' not in cond['targets']:
            cond['targets'].append('自己')
    # 「全队/全体/群体 + buff/增益/强化」泛问（未指明具体效果）→ 增益大类 + 我方全体：
    # 用户问的是"能给全队上强化 buff 的船"，不是"目标为全体的角色"；排除对敌语境。
    if re.search(r'buff|增益|强化|光环|辅助|加成', msg, re.I) \
            and re.search(r'全队|全体|全员|全团|队伍|团队|队友|全舰|舰队|群体|群', msg) \
            and 'buffs' not in cond and not re.search(r'敌|对方', msg):
        cond.setdefault('targets', [])
        if '全体' not in cond['targets']:
            cond['targets'].append('全体')
        cond['buffs'] = ['增益']
    return cond


def llm_parse_conditions(msg: str) -> dict:
    """规则表未命中时的 LLM 兜底：只把条件翻译成严格校验的短 JSON（不生成查询内容）。
    字段白名单校验，非法值剔除；全空/出错返回 {}（调用方据此不触发筛选）。"""
    try:
        import json
        from openai import OpenAI
        from llm.llm_client import get_llm_config, MODEL_SERVER
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(api_key=cfg['api_key'], base_url=MODEL_SERVER, timeout=15)
        allowed = ('targets', 'buffs', 'ammo', 'ship_type')
        vocab = {
            'targets': tuple(_TARGET_ALIASES),
            'buffs': tuple(_BUFF_ALIASES),
            'ammo': _AMMO_QUERY_WORDS,
            'ship_type': _SHIP_TYPE_QUERY_WORDS,
        }
        resp = client.chat.completions.create(
            model=cfg['model'],
            messages=[
                {'role': 'system', 'content':
                 '你是舰灵技能条件解析器。把用户问题解析为 JSON，只输出 JSON 不输出其他：'
                 '{"targets":[],"buffs":[],"ammo":[],"ship_type":[]}。'
                 '取值限定：targets∈%s；buffs∈%s；ammo∈%s；ship_type∈%s。'
                 '无法确定的不填；至少填一项，无法解析则输出四个空数组。'
                 % (list(vocab['targets']), list(vocab['buffs']), list(vocab['ammo']), list(vocab['ship_type']))},
                {'role': 'user', 'content': msg},
            ],
            temperature=0.0,
            # qwen3 系列混合思考模型非流式调用必须显式关闭思考模式，否则 DashScope 返回 400
            extra_body=cfg['generate_cfg']['extra_body'],  # 思考开关由 .env LLM_ENABLE_THINKING 统一控制
        )
        text = (resp.choices[0].message.content or '').strip()
        m = re.search(r'\{.*\}', text, re.S)
        cond = json.loads(m.group(0)) if m else {}
        if not isinstance(cond, dict):
            return {}
        out = {}
        for k in allowed:
            vals = cond.get(k)
            if isinstance(vals, (list, tuple)):
                v = [str(x).strip() for x in vals if str(x).strip() in vocab[k]]
                if v:
                    out[k] = v
        return out if any(out.values()) else {}
    except Exception:
        return {}


# ======================================================================
# 技能倒排索引（构建期一次，纯内存检索）
# ======================================================================

def build_skill_index(app):
    """扫描 raw_data（信息库）：buff×目标 倒排 + 弹种 + 舰种 索引。
    只收录信息完整（is_ship_complete）的角色；文本来源：技能原文 + 天赋。
    每个 buff 额外维护 'any' 桶（该效果能力全量，含未指明目标的文本）。"""
    raw = getattr(app, 'raw_storage', {}) or {}
    buff_index = {b: ({t: set() for t in _TARGET_ALIASES} | {'any': set()})
                  for b in _BUFF_PATTERNS}
    # 主动命中集：只统计 支援技/通常技/必杀技（主动施加），旗舰技与天赋不算"给队友上 buff"
    act_index = {b: ({t: set() for t in _TARGET_ALIASES} | {'any': set()})
                 for b in _BUFF_PATTERNS}
    # 强全队主动集：文本确为全队级目标（全队/全体/全员/所属舰队/全舰/全团…）的主动技能命中
    act_all_index = {b: set() for b in _BUFF_PATTERNS}
    _TEAM_WORD = re.compile(r'全队|全体|全员|所属舰队|全舰|全团|我方全体|全体我方|舰队全体|我方舰队')
    ammo_index = {}
    stype_index = {}
    for name, data in raw.items():
        if not app.is_ship_complete(name):
            continue
        panel = (data.get('panel') or {}) if isinstance(data, dict) else {}
        ammo = panel.get('弹种')
        if ammo:
            ammo_index.setdefault(str(ammo), set()).add(name)
        st = panel.get('舰种')
        if st:
            stype_index.setdefault(str(st), set()).add(name)
        skills = (data.get('skills') or {}) if isinstance(data, dict) else {}
        texts = _slot_texts(data)
        for buff, pats in _BUFF_PATTERNS.items():
            for text, _slot in texts:
                if not any(re.search(p, text) for p in pats):
                    continue
                # 任何命中（技能/天赋、是否指明目标）都进 any 桶
                buff_index[buff]['any'].add(name)
                if _slot in ('SupportSkill', 'BattleSkill1', 'BattleSkill2'):
                    act_index[buff]['any'].add(name)
                    if _TEAM_WORD.search(text):
                        act_all_index[buff].add(name)
                if buff == '回血':
                    # 自奶 = 具备"自我回复能力"（用户约定）：
                    # ① 文本含「自身/自己」；或
                    # ② 治疗对象是友方单位（出战位/我方/舰队/前排/后排/队友…）——自己也是友方，
                    #    血量低时同样会被奶到（女灶神"为出战位血量最低舰灵维修"→ 算自奶）；
                    # ③ 天赋自我回复（天赋文本已并入 texts，同样走①②）。
                    _self = ('自身' in text or '自己' in text) or any(
                        a in text for a in ('出战位', '我方', '友方', '所属舰队', '全队', '全体', '舰队',
                                            '队友', '同伴', '前排', '后排', '友军'))
                    if _self:
                        buff_index['回血']['自己'].add(name)
                    for t in ('全体', '前排', '后排'):
                        if any(a in text for a in _TARGET_ALIASES[t]):
                            buff_index['回血'][t].add(name)
                    continue
                tgts = [t for t, aliases in _TARGET_ALIASES.items() if any(a in text for a in aliases)]
                if not tgts:
                    # 移速等目标含糊（如"移动速度+30%"未指明对象），不做默认推断
                    if buff in ('移速',):
                        continue
                    tgts = ['自己']
                for t in tgts:
                    buff_index[buff][t].add(name)
                    if _slot in ('SupportSkill', 'BattleSkill1', 'BattleSkill2'):
                        act_index[buff][t].add(name)
    # 「增益」泛桶：全队 buff 问法的匹配维度（强化类并集，排除 治疗/吸血/敌方效果）
    # ——「哪些船能给全队buff」≠ 目标全体筛选，必须是"能给我方全体上强化增益"；
    # 回血/吸血/点燃/控制 不算 buff 强化（奶/控是另一类），故不入桶。
    # 全体目标桶只算**主动技能命中**（支援/通常/必杀）——用户铁律：给全队 buff 不算旗舰光环/天赋。
    _GEN_BUFF_EXCLUDE = {'回血', '吸血', '点燃', '控制'}
    gen = {'any': set()}
    gen.update({t: set() for t in _TARGET_ALIASES})
    for b, bm in buff_index.items():
        if b in _GEN_BUFF_EXCLUDE:
            continue
        am = act_index.get(b, {})
        for t, s in bm.items():
            # 全体目标桶只算**主动技能且文本确为全队级**的命中（支援/通常/必杀）——
            # 用户铁律：给全队 buff 不算旗舰光环/天赋，也不要"出战位/我方"等泛指队友的宽词
            gen[t] |= (act_all_index.get(b, set()) if t == '全体' else (am.get(t, set()) if t == 'any' else s))
    buff_index['增益'] = gen
    return {'buffs': buff_index, 'ammo': ammo_index, 'ship_type': stype_index}


_INDEX_CACHE = {}


def _get_index(app):
    key = id(getattr(app, 'raw_storage', None)) if app else 0
    if key and key in _INDEX_CACHE:
        return _INDEX_CACHE[key]
    idx = build_skill_index(app)
    if key:
        _INDEX_CACHE[key] = idx
    return idx


def _match_sets(index: dict, key: str, words) -> set:
    """弹种/舰种：查询词对实际值做包含匹配（如 高爆 → 高爆/高爆弹）。"""
    s = set()
    for w in words:
        for v, st in index.get(key, {}).items():
            if w in v:
                s |= st
    return s


# 本体/改造去重：夕立 与 夕立·改、拉菲(DD-459) 与 拉菲·改(DD-724) 只算一次
_EVO_SUFFIX_RE = re.compile(r'·改$')


def _base_name(name: str) -> str:
    """归一化到本体名：去「·改」后缀、去括号编号（拉菲·改(DD-724) → 拉菲）。"""
    n = _EVO_SUFFIX_RE.sub('', name or '')
    n = re.sub(r'[（(].*$', '', n)
    return n.strip()


def _dedup_evolutions(names: list) -> list:
    """本体与改造只算一次；展示优先保留改造版（含·改）。"""
    best = {}
    for n in names:
        b = _base_name(n)
        if b not in best or ('·改' in n and '·改' not in best[b]):
            best[b] = n
    return list(best.values())


def _describe(cond: dict) -> str:
    parts = []
    for key, label in (('targets', '目标'), ('buffs', '加成'), ('ammo', '弹种'), ('ship_type', '舰种')):
        vals = cond.get(key) or []
        if vals:
            parts.append('%s:%s' % (label, '/'.join(vals)))
    return '，'.join(parts) or '未指定'


# ======================================================================
# 工具：query_ships_by_skill
# ======================================================================

class SkillQuery(BaseTool):
    """按技能条件列举舰灵（信息库检索）。conditions: {"targets":[...], "buffs":[...],
    "ammo":[...], "ship_type":[...]}，至少一项；输出仅角色名列表。"""
    name = 'query_ships_by_skill'
    description = ('列举满足技能效果条件的舰灵（信息库检索，只返回角色名列表）：'
                   '按 目标(自己/前排/全体/后排/敌方) × 加成(回血/吸血/炮击/雷击/航空/命中/装填/暴击/'
                   '护盾/减伤/增伤/回避/攻速/移速/点燃/控制等) × 弹种(高爆/穿甲等) × 舰种 筛选。'
                   '（注意：纯属性筛选——稀有度/阵营/是否改造组合，如「蓝白绿的驱逐」「没改造的金航母」——请用 ship_filter，不要混淆。）')
    parameters = {
        'type': 'object',
        'properties': {
            'conditions': {
                'type': 'object',
                'description': '结构化条件：{"targets":["自己","前排"],"buffs":["炮击"],"ammo":["高爆"]}，均可不填但至少填一项',
            },
        },
        'required': ['conditions'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        cond = params.get('conditions') or {}
        idx = _get_index(app)

        sets = []
        buffs = cond.get('buffs') or []
        targets = cond.get('targets') or []
        if buffs:
            s = set()
            for b in buffs:
                bm = idx['buffs'].get(b)
                if not bm:
                    continue
                if targets:
                    for t in targets:
                        for it in _TARGET_EXPAND.get(t, (t,)):
                            s |= bm.get(it, set())
                else:
                    # 未指定目标：「自奶」等宽口径 → 该效果的全量能力桶（含天赋/未指明目标的文本）
                    s |= bm.get('any', set())
            sets.append(s)
        elif targets:
            s = set()
            for t in targets:
                for it in _TARGET_EXPAND.get(t, (t,)):
                    for bm in idx['buffs'].values():
                        s |= bm.get(it, set())
            sets.append(s)
        if cond.get('ammo'):
            sets.append(_match_sets(idx, 'ammo', cond['ammo']))
        if cond.get('ship_type'):
            sets.append(_match_sets(idx, 'ship_type', cond['ship_type']))

        if not sets:
            return ('筛选需要至少一个条件（目标/加成/弹种/舰种）。'
                    '支持的说法如「自奶船」「给前排加移速的舰灵」「加炮击的高爆舰灵」「哪些船有护盾」。')
        result = set.intersection(*sets)
        order = {n: i for i, n in enumerate(getattr(app, 'raw_storage', {}) or {})}
        names = sorted(result, key=lambda n: order.get(n, 1 << 30))
        # 本体与改造只算一次（夕立/夕立·改、拉菲(DD-459)/拉菲·改(DD-724)）
        names = _dedup_evolutions(names)
        cond_desc = _describe(cond)
        if not names:
            return '信息库里没有满足「%s」的舰灵。' % cond_desc
        # 一次性全部列出（用户要求，不做 20 条截断）
        text = '%d 位舰灵满足「%s」：%s。' % (len(names), cond_desc, '、'.join(names))
        if '增益' in (buffs or []):
            text += '\n（以上为能给我方全体上强化增益的舰灵，涵盖 攻击/防御/命中/暴击/护盾/机动/移速 等效果；'
            text += '若要更精确可按具体效果问，如「给全队加炮击」「全队加护盾」。）'
        return text



def _slot_texts(data: dict):
    """角色可检索文本带技能槽位标记：[(text, slot)]。
    slot ∈ SupportSkill/BattleSkill1/BattleSkill2/LeaderSkill/talent。
    主动技能（支援/通常/必杀）与 旗舰技/天赋 分开——「给全队 buff 不算旗舰」铁律：团队增益查询
    只统计主动施加（支援/通常/必杀），旗舰光环与天赋不计入（用户 2026-09-04 约定）。"""
    out = []
    skills = (data.get('skills') or {}) if isinstance(data, dict) else {}
    for f in ('LeaderSkill', 'SupportSkill', 'BattleSkill1', 'BattleSkill2'):
        v = skills.get(f)
        if isinstance(v, str) and v.strip():
            out.append((v, f))
    talents = (data.get('talents') or {}) if isinstance(data, dict) else {}
    for row in talents.values():
        if not isinstance(row, dict):
            continue
        for opt in ('A', 'B'):
            od = row.get(opt) or {}
            if isinstance(od, dict):
                t = ('%s %s' % (od.get('name') or '', od.get('effect') or '')).strip()
            elif isinstance(od, str):
                t = od.strip()
            else:
                t = ''
            if t:
                out.append((t, 'talent'))
    return out


# ======================================================================
# 工具：ship_filter —— 角色属性组合筛选（舰种/阵营/稀有度/是否改造）
# 用户 2026-09-15 需求：①多重稀有度（蓝白绿）②改造状态（没改造/有改造）
# ③完整性门控（信息不全的舰灵不收录）④多条件查询由 LLM 自然分析组织回复（非死板清单）
# 数据 100% 由后端从底层 storage 确定性产生，LLM 仅负责把确定名单"说得好听"，不得增删舰灵。
# ======================================================================

_SHIP_FILTER_TYPE_WORDS = ('驱逐', '轻巡', '重巡', '战巡', '战列', '航空母舰', '航母', '轻母', '装母',
                           '潜艇', '潜舰', '航战', '重炮', '雷巡', '水母', '维修')
_SHIP_FILTER_TYPE_CANON = {'航空母舰': '航母', '潜舰': '潜艇'}
_SHIP_FILTER_NATION_WORDS = ('尤奈特', '八咫', '洛蒙瑞亚', '奥鲁加', '列加杜', '纳谢尔', '凤栖', '沃尔克', '其他')
_SHIP_FILTER_RARITY_WORDS = ('彩', '金', '紫', '蓝', '绿', '白')
# 大小分类（与 formula_engine._SHIP_SIZE_MAP 同源）：小船=驱逐/补给/水母/潜艇，中船=轻巡等，大船=战列/航母等
_SHIP_FILTER_SIZE_WORDS = ('小船', '小型', '中船', '中型', '大船', '大型')
_SHIP_FILTER_SIZE_CANON = {'小船': '小型', '中船': '中型', '大船': '大型'}
# 单字稀有度（彩/金/紫/蓝/绿/白）的合法上下文：见 _extract_rarities
_RARITY_NEIGHBOR_WORDS = ('角色', '舰灵', '舰娘', '船', '驱逐', '轻巡', '重巡', '战巡', '战列',
                          '航母', '轻母', '装母', '潜艇', '潜舰', '航战', '重炮', '雷巡', '水母',
                          '维修', '小船', '中船', '大船')
_RARITY_CLAUSE_BOUND = '，,。.、；;：:！!？?　 \t\n（(【[「'


def _extract_rarities(m):
    """从原话抽取稀有度维度（保序，按 _SHIP_FILTER_RARITY_WORDS 顺序）。

    稀有度是单字，纯子串匹配会误命中词内字（实测「说明白」→白），使与筛选无关的
    长问句被拦成「稀有度 白」名单。故单字须满足其一：
      ① 后接「色」；② 与另一稀有度字相邻（蓝白绿）；③ 前面是「稀有/稀有度」；
      ④ 处于子句开头或标点/空白之后；⑤ 后接舰种类或角色类词（金航母、金角色）。
    """
    ok = set()
    n = len(m or '')
    for w in _SHIP_FILTER_RARITY_WORDS:
        for mt in re.finditer(re.escape(w), m or ''):
            i, j = mt.start(), mt.end()
            prev = m[i - 1] if i else ''
            nxt = m[j] if j < n else ''
            if (nxt == '色'
                    or prev in _SHIP_FILTER_RARITY_WORDS or nxt in _SHIP_FILTER_RARITY_WORDS
                    or m[max(0, i - 3):i].endswith('稀有度') or m[max(0, i - 2):i].endswith('稀有')
                    or i == 0 or prev in _RARITY_CLAUSE_BOUND
                    or any(m.startswith(d, j) for d in _RARITY_NEIGHBOR_WORDS)):
                ok.add(w)
                break
    return [w for w in _SHIP_FILTER_RARITY_WORDS if w in ok]


def _ship_talent_names(raw: dict) -> dict:
    """取角色天赋名 → 效果 映射（raw_storage 单角色结构 {等级:{槽位:{name,effect}}}）。"""
    out = {}
    for slots in ((raw or {}).get('talents') or {}).values():
        if isinstance(slots, dict):
            for s in slots.values():
                if isinstance(s, dict) and s.get('name'):
                    out[s['name']] = s.get('effect') or ''
    return out


def _refit_name_set(app) -> set:
    """改造舰名集合：① storage 键以「·改」结尾；② nickname 别名含「改」的 remap 改造（雪风改→丹阳 等）。
    **只认信息完整的改造形态**——不完整视同不存在（项目铁律）；否则仅有空壳数据的 ·改
    （如 电·改/雷·改，完整=False）会被当作存在，导致本体被误判为「有改造」。"""
    def _ok(name):
        try:
            return bool(app.is_ship_complete(name))
        except Exception:
            return False

    refit = set()
    raw = getattr(app, 'raw_storage', {}) or {}
    for k in raw:
        if isinstance(k, str) and k.endswith('·改') and _ok(k):
            refit.add(k)
    try:
        from core.name_service import load_nicknames
        for std, aliases in (load_nicknames() or {}).items():
            if std in raw and _ok(std) and any('改' in str(a) for a in (aliases or [])):
                refit.add(std)
    except Exception:
        pass
    return refit


def _refittable_base_set(app) -> set:
    """「可改造」本体舰名集合：存在对应改造版本的本体。
    关系推导：① 改造键以「·改」结尾 → 本体 = 去后缀（如 夕立·改→夕立）；
    ② remap 改造名（丹阳/信赖/埃罗芒什）→ 别名含「改」者去「改」后缀即本体（丹阳→雪风）。"""
    storage = getattr(app, 'storage', {}) or {}
    bases = set()
    for r in _refit_name_set(app):
        if r.endswith('·改'):
            base = r[:-2]
            if base in storage:
                bases.add(base)
            else:
                # 如「拉菲·改」→ 本体「拉菲(DD-459)」：按「本体(…」前缀唯一匹配
                for k in storage:
                    if k.startswith(base + '('):
                        bases.add(k)
        try:
            from core.name_service import load_nicknames
            for a in (load_nicknames().get(r) or []):
                if '改' in str(a):
                    cand = re.sub(r'[·\s]?改$', '', str(a)).strip()
                    if cand in storage:
                        bases.add(cand)
        except Exception:
            pass
    return bases


def _size_allowed_stypes(app, size: str):
    """返回属于某尺寸类的全部舰种（用 formula_engine.get_ship_size 作唯一真相源）。"""
    try:
        from core.formula_engine import get_ship_size
    except Exception:
        from formula_engine import get_ship_size
    storage = getattr(app, 'storage', None) or {}
    all_types = set()
    for ship in storage.values():
        st = getattr(ship, 'ship_type', '') or ''
        if st:
            all_types.add(st)
    return {st for st in all_types if get_ship_size(st) == size}


_CLASS_RE = re.compile(r'([一-鿿]{1,8})级')
_CLASS_JOIN = ('的', '且', '与', '及', '中', '至', '或')


def _ship_class_ref():
    """返回 (ship_class_by_name, class_values)。懒加载并缓存（复用 skill_buff_extractor 权威舰级表）。"""
    try:
        from core import skill_buff_extractor as _sbe
        ref = _sbe.load_reference()
        sc = ref.get('ship_class_by_name') or {}
        return sc, set(sc.values())
    except Exception:
        return {}, set()


def _extract_ship_class(m):
    """从筛选原话抽取舰级（如「约克城级」）。仅接受权威表里存在的级名，避免「一级」误命中。返回 'X级' 或 None。"""
    sc_by_name, class_vals = _ship_class_ref()
    if not class_vals:
        return None
    for mm in _CLASS_RE.finditer(m or ''):
        val = mm.group(1)
        for sep in _CLASS_JOIN:
            if val.startswith(sep):
                val = val[len(sep):]
        if not val:
            continue
        cand = val + '级'
        if cand in class_vals:
            return cand
    return None


def _ship_class_match(name, target):
    """判断 name 是否属于 target 舰级（处理改造前后/姊妹船，复用 skill_buff_extractor.match_ship_class）。"""
    try:
        from core import skill_buff_extractor as _sbe
        if _sbe.match_ship_class(name, target):
            return True
    except Exception:
        pass
    sc_by_name, _ = _ship_class_ref()
    return sc_by_name.get(name) == target


def _parse_filter_conditions(msg, app):
    """解析属性筛选条件。返回 dict（含 stype/nation/rarities/refit/talents/size/ship_class 中命中的键）或 None（无任何维度）。"""
    m = msg or ''
    stype = next((w for w in _SHIP_FILTER_TYPE_WORDS if w in m), None)
    if stype:
        stype = _SHIP_FILTER_TYPE_CANON.get(stype, stype)
    nation = next((w for w in _SHIP_FILTER_NATION_WORDS if w in m), None)
    rarities = _extract_rarities(m)
    refit = None
    refittable = False
    if re.search(r'没\s*改造|未\s*改造|无\s*改造|非\s*改造|不\s*改造|没有\s*改造', m):
        refit = 'base'
    elif re.search(r'可以改造|可改造|能改造|可以改|能改|值得改造|改造资格|可改', m):
        refittable = True  # 「可以改造」= 存在改造版本的本体（≠「改造的」= 已改造）
    elif '改造' in m:
        refit = 'refit'
    size = None
    m_size = re.search(r'(小|中|大)(?:船|型)', m)
    if m_size:
        w = m_size.group(0)
        size = _SHIP_FILTER_SIZE_CANON.get(w, w)
    raw_storage = getattr(app, 'raw_storage', {}) or {}
    talent_names = set()
    for raw in raw_storage.values():
        talent_names.update(_ship_talent_names(raw).keys())
    hit_talents = [t for t in sorted(talent_names, key=len, reverse=True)
                   if len(t) >= 3 and t in m]
    # 防碰撞：「大型特攻」等天赋名含尺寸词时，尺寸维度是天赋名的子串，丢弃尺寸（只走天赋）
    if size and hit_talents and any(size in t for t in hit_talents):
        size = None
    ship_class = _extract_ship_class(m)
    if not (stype or nation or rarities or refit or refittable or hit_talents or size or ship_class):
        return None
    return {'stype': stype, 'nation': nation, 'rarities': rarities,
            'refit': refit, 'refittable': refittable, 'talents': hit_talents, 'size': size,
            'ship_class': ship_class}


def _run_ship_filter(app, conds):
    """按条件过滤 + 完整性门控。返回 [(name, nation, ship_type, rarity), ...]（按舰名排序）。"""
    storage = getattr(app, 'storage', None) or {}
    raw_storage = getattr(app, 'raw_storage', {}) or {}
    refit = _refit_name_set(app)
    # 「有改造形态」本体集合：refit='refit'（有改造的）、refittable（可以改造）、refit='base'（没有改造的，需反向用）均需要
    need_refittable = conds.get('refittable') or conds.get('refit') in ('base', 'refit')
    refittable = _refittable_base_set(app) if need_refittable else set()
    size_allowed = _size_allowed_stypes(app, conds['size']) if conds.get('size') else None
    rows = []
    for name, ship in storage.items():
        # 完整性门控：信息不全视同不存在（用户 2026-09-14/15 约定：名录/反查不收录）
        try:
            if not app.is_ship_complete(name):
                continue
        except Exception:
            continue
        st = getattr(ship, 'ship_type', '') or ''
        na = getattr(ship, 'nation', '') or ''
        ra = getattr(ship, 'rarity', '') or ''
        if conds.get('stype') and st != conds['stype']:
            continue
        if size_allowed is not None and st not in size_allowed:
            continue
        if conds.get('nation') and na != conds['nation']:
            continue
        if conds.get('rarities') and ra not in conds['rarities']:
            continue
        # 改造维度（统一「本体名」口径，用户 2026-09-15 确认）：
        #   「没有改造的/没改造/未改造」= 无改造形态 —— 既非改造形态(name in refit)，也不存在改造形态(name in refittable)
        #   「有改造的/改造的/可以改造的/可改造」= 有改造形态的本体(name in refittable)
        if conds.get('refit') == 'base':
            if name in refit or name in refittable:
                continue
        if conds.get('refit') == 'refit' or conds.get('refittable'):
            if name not in refittable:
                continue
        if conds.get('talents'):
            tn = _ship_talent_names(raw_storage.get(name) or {})
            if not any(t in tn for t in conds['talents']):
                continue
        if conds.get('ship_class'):
            if not _ship_class_match(name, conds['ship_class']):
                continue
        rows.append((name, na, st, ra))
    rows.sort(key=lambda r: r[0])
    return rows


def _filter_cond_desc(conds):
    dims = []
    if conds.get('nation'):
        dims.append('阵营 %s' % conds['nation'])
    if conds.get('stype'):
        dims.append('舰种 %s' % conds['stype'])
    if conds.get('size'):
        dims.append('大小 %s' % conds['size'])
    if conds.get('rarities'):
        dims.append('稀有度 %s' % '/'.join(conds['rarities']))
    if conds.get('refit') == 'base':
        dims.append('无改造形态')
    elif conds.get('refit') == 'refit' or conds.get('refittable'):
        dims.append('有改造形态')
    if conds.get('talents'):
        dims.append('天赋 %s' % '/'.join(conds['talents']))
    if conds.get('ship_class'):
        dims.append('舰级 %s（已实装）' % conds['ship_class'])
    return ' · '.join(dims) or '未指定'


def _structured_filter_reply(conds, rows):
    title = _filter_cond_desc(conds)
    lines = ['《舰灵筛选 · %s》' % title]
    if rows:
        lines.append('共 %d 名：' % len(rows))
        for i, (name, na, st, ra) in enumerate(rows[:40], 1):
            lines.append('%d. %s（%s · %s · %s）' % (i, name, na, st, ra))
        if len(rows) > 40:
            lines.append('……等共 %d 名，可再加维度缩小范围。' % len(rows))
    else:
        lines.append('底层舰库里没有符合条件的角色，不编造。')
        lines.append('可换维度再筛：舰种（驱逐/轻巡/重巡/战列/航母…）、舰级（约克城级/翔鹤级…）、'
                     '尺寸（小船=驱逐/潜艇/水母/补给、中船=轻巡等、大船=战列/航母等）、阵营（尤奈特/八咫/洛蒙瑞亚…）、'
                     '稀有度（彩/金/紫/蓝/绿/白）、是否改造（没改造/改造）。')
    # 舰级维度依据史实原型表（原型舰历史.json）解析：只列游戏库内已实装角色，
    # 未实装的史实同级舰船不列入（用户 2026-09-25 口径）。
    if conds.get('ship_class'):
        lines.append('（舰级依据史实原型；以上仅列游戏内已实装舰灵，未实装的史实同级舰船不计入）')
    return '\n'.join(lines)


# 追问增量判定：短句、无问句词、仅含「可叠加」维度（稀有度/尺寸/阵营）→ 视为继承上次筛选的追问
_FILTER_FOLLOWUP_MAXLEN = 12
_FILTER_FOLLOWUP_Q_RE = re.compile(r'哪些|哪几|哪(?:艘|条|个|种|位)|有哪些|都有|列表|多少个|多少艘|推荐|排行|谁')


def _resolve_filter_conds(msg, app, last=None):
    """解析筛选条件；若本次是「追问增量」（如上一句筛完中船后只说「紫的」）且给了 last，
    则继承 last 并叠加本次增量维度。返回 (conds, merged)；无任何条件返回 (None, False)。"""
    conds = _parse_filter_conditions(msg, app)
    if conds is None:
        return None, False
    if last:
        m = (msg or '').strip()
        q_word = bool(_FILTER_FOLLOWUP_Q_RE.search(m))
        # 追问增量特征：短句 + 具有"指代性"（含「的/呢」或本就不带问句词）+ 不以问句词开头 + 仅含可叠加维度
        fragment = ('的' in m) or ('呢' in m) or (not q_word)
        thin = (len(m) <= _FILTER_FOLLOWUP_MAXLEN
                and fragment
                and not re.match(r'^(?:有哪些|哪些|哪几|什么|谁)', m)
                and not conds.get('stype')
                and not (conds.get('refit') or conds.get('refittable'))
                and not conds.get('talents'))
        if thin:
            merged = dict(last)
            for k in ('stype', 'nation', 'size', 'refit', 'refittable', 'rarities', 'talents'):
                if conds.get(k):
                    merged[k] = conds[k]
            return merged, True
    return conds, False


def _ship_filter_text_from_conds(app, conds):
    """按给定条件过滤并生成确定性结构化回复（供追问继承复用）。"""
    if conds is None:
        return None
    return _structured_filter_reply(conds, _run_ship_filter(app, conds))


def _ship_filter_text(msg, app):
    """属性筛选统一入口：解析 → 过滤（含完整性门控）→ **确定性结构化输出**。
    返回最终回复文本；无法识别任何条件返回 None（交回上层路由）。
    用户 2026-09-15 收口：筛选回复**只输出对应信息、不走 LLM**——LLM 会自行追加建议、
    甚至改写舰名（把「伊势」写成「伊势·改」），违反零幻觉/零编造铁律。"""
    conds = _parse_filter_conditions(msg, app)
    return _ship_filter_text_from_conds(app, conds)


class ShipFilter(BaseTool):
    """按角色属性筛选并列举舰灵（信息库检索，返回自然分析式回复）。
    支持 舰种、尺寸（小船/中船/大船）、阵营、稀有度（可多重，如「蓝白绿」）、是否改造（「没改造」=仅本体/「改造」=仅改造舰）
    的组合筛选，并自动剔除信息不全的舰灵。适用于「蓝白绿小船有哪些」「没改造的金航母」等属性组合查询。"""
    name = 'ship_filter'
    description = ('按角色属性（非技能效果）筛选并列举舰灵，返回自然分析式回复（非死板编号清单）：'
                   '维度=舰种（驱逐/航母/…）、**舰级（如约克城级/翔鹤级，查「X级有哪些」）**、'
                   '尺寸（小船=驱逐/潜艇/水母/补给，中船=轻巡等，大船=战列/航母等）、'
                   '阵营（尤奈特/八咫/…）、稀有度（可多重，如「蓝白绿」同时指蓝/绿/白）、'
                   '改造状态（口径=「有无改造形态」，均列本体名：「没有改造的/没改造/未改造」=无任何改造形态的本体；'
                   '「有改造的/改造的/可以改造的/可改造」=有改造形态的本体；·改 形态与 remap 改造如 雪风→信赖 都算）。'
                   '自动剔除信息不全的舰灵。'
                   '适用于「没有改造的蓝白绿小船有哪些」「有改造的大船有哪些」「可以改造的绿色小船」'
                   '「八咫的驱逐」「中型船有哪些」「约克城级有哪些」「和企业同舰级的船」等属性组合查询。'
                   '（注意：按技能效果/能力问"哪些船有护盾/自奶船"请用 query_ships_by_skill，不要混淆。）')
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '用户的筛选原话，如「没有改造的蓝白绿的驱逐有哪些」',
            },
        },
        'required': ['query'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        q = (params.get('query') or '').strip()
        text = _ship_filter_text(q, app)
        if not text:
            return ('我没能从这句话里识别筛选条件（舰种/阵营/稀有度/是否改造）。'
                    '可以换个说法，比如「蓝白绿的驱逐有哪些」「没改造的金航母」。')
        return text
