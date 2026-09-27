# -*- coding: utf-8 -*-
"""回复标记（marker）的提取与剥离 —— 从 web_app.py 拆出的纯文本处理层。

LLM 回复里用一组 `<<XXX>>` 标记携带结构化信息（填空表单、建议按钮、头像、攻略文件、
攻略库触发、遭遇战表单…），本模块负责把它们解析成 dict/list，以及从回复文本里剥掉
标记、只留正文。

为什么要拆出来
--------------
这一块约 360 行，特点是**只吃字符串、吐结构化数据**，与聊天编排/路由几乎无耦合，
堆在 web_app.py 里只会让它更"上帝文件"。搬出来后 web_app 少一大块，且这组函数
可以单独看懂、单独改。

依赖注入（拆得动的前提）
------------------------
- `KB_STATIC_DIR`（知识库目录）由 web_app 启动时 `configure()` 注入 —— 本模块不自己
  推导项目根，避免把"项目根/知识库"的路径约定复制到两个地方。
- 原实现里 `_fill_form_with_pvp_default` / `_auto_fill_form` 直接引用 web_app 的全局
  `agent` 实例，搬出去就会形成 `web_app ↔ markers` 循环导入，故改为**显式传参**
  （`session_overrides` / `name_matcher`）。调用方传 `agent.session_overrides`、
  `agent.raw_name_matcher` 即可，语义不变。
"""
import json
import os
import re

from core.encounter_simulator import load_encounter_bosses

_KB_STATIC_DIR = ''


def configure(kb_static_dir: str):
    """注入知识库目录（web_app 启动时调用一次）。

    未注入时 `_load_pvp_defaults()` 返回空字典（PVP 默认点法不可用，其余功能不受影响）。
    """
    global _KB_STATIC_DIR
    _KB_STATIC_DIR = kb_static_dir or ''


# 工具名 → 按钮显示名映射
TOOL_BTN_LABELS = {
    'calc_normal_attack': '普攻',
    'calc_skill_attack': '通常技',
    'calc_burst_attack': '必杀技',
    'calc_support_attack': '支援技',
    'calc_burn_damage': '点燃',
    'calc_total_damage': '总伤害',
    'encounter_guide': '往期遭遇战攻略',
    'encounter_simulator': '遭遇战模拟',
    'end_round': '结束本轮计算',
}

# 标记正则：<<SUGGEST_CALC>>calc_normal_attack|calc_skill_attack|...
_SUGGEST_PATTERN = re.compile(r'<<SUGGEST_CALC>>([\w|_]+)')
# 填空表单标记：<<FILL_FORM>>attacker=火力|attacker_buff=|defender=|defender_buff=
# 支持 key 或 key=value 两种形式，value 含等号后面的原始文本（可为空）
_FILL_FORM_PATTERN = re.compile(r'<<FILL_FORM>>([^<\n]+)')

# 填空表单字段元信息
FILL_FORM_FIELDS = {
    'attacker': {'label': '攻击方', 'placeholder': '如 吹雪改', 'required': True},
    'attacker_buff': {'label': '攻击方buff', 'placeholder': '如 全伤害+15%、暴击率+20%', 'required': False},
    'attacker_talent': {'label': '攻击方天赋', 'placeholder': '不填则用默认 PVP 点法', 'required': False},
    'defender': {'label': '防守方', 'placeholder': '如 南达科他', 'required': False},
    'defender_buff': {'label': '防守方buff', 'placeholder': '如 全伤害减免+20%', 'required': False},
    'defender_talent': {'label': '防守方天赋', 'placeholder': '不填则用默认 PVP 点法', 'required': False},
    'course': {'label': '航向', 'placeholder': '同航 / 反航 / T优势 / T劣势（默认同航）', 'required': False},
    'air': {'label': '制空', 'placeholder': '确保 / 优势 / 均势 / 劣势 / 丧失（默认均势）', 'required': False},
}


def _extract_fill_form(reply: str) -> list:
    """从回复中解析 <<FILL_FORM>> 标记，返回 [{key, label, placeholder, required, value}, ...]。
    支持 key 或 key=value 两种形式，value 为预填值（可为空字符串）。无标记返回空列表。"""
    if not reply:
        return []
    m = _FILL_FORM_PATTERN.search(reply)
    if not m:
        return []
    raw = m.group(1).strip()
    parts = [p.strip() for p in raw.split('|') if p.strip()]
    result = []
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            k = k.strip()
            v = v.strip()
        else:
            k, v = p, ''
        meta = FILL_FORM_FIELDS.get(k, {'label': k, 'placeholder': '', 'required': False})
        result.append({'key': k, 'label': meta['label'], 'placeholder': meta['placeholder'], 'required': meta['required'], 'value': v})
    return result


# PVP 默认编码缓存（talent_pvp.json：角色名 → 4 位推荐编码）
_PVP_DEFAULT_CACHE = None


def _load_pvp_defaults() -> dict:
    """读取 知识库/talent_pvp.json（角色名 → PVP 推荐编码），带模块级缓存。"""
    global _PVP_DEFAULT_CACHE
    if _PVP_DEFAULT_CACHE is not None:
        return _PVP_DEFAULT_CACHE
    pvp_file = os.path.join(_KB_STATIC_DIR, "talent_pvp.json")
    if not os.path.exists(pvp_file):
        _PVP_DEFAULT_CACHE = {}
        return _PVP_DEFAULT_CACHE
    try:
        with open(pvp_file, encoding="utf-8") as f:
            _PVP_DEFAULT_CACHE = json.load(f)
    except Exception:
        _PVP_DEFAULT_CACHE = {}
    return _PVP_DEFAULT_CACHE


def _fill_form_with_pvp_default(fill_form: list, session_overrides: dict = None,
                                name_matcher=None) -> list:
    """把攻防双方天赋输入框的 placeholder 替换为「对应具体角色的 PVP 默认点法」。

    角色名来自 session_overrides.calc_context（攻击方/敌方）；未确定角色时保持通用提示。
    两个参数由调用方从 agent 实例取（agent.session_overrides / agent.raw_name_matcher）—
    本模块不反向依赖 web_app，避免循环导入。
    """
    if not fill_form:
        return fill_form
    overrides = session_overrides or {}
    ctx = overrides.get("calc_context") or {}
    attacker = ctx.get("attacker") or ""
    enemy = ctx.get("enemy") or ""
    pvp = _load_pvp_defaults()
    # 通过 name_matcher 归一化角色名（昵称→正规名），取 PVP 编码
    matcher = name_matcher

    def _resolve_pvp(name):
        if not name:
            return ""
        if matcher is not None:
            try:
                matched = matcher.match(name)
                if matched:
                    name = matched
            except Exception:
                pass
        return pvp.get(name, "")

    atk_code = _resolve_pvp(attacker)
    def_code = _resolve_pvp(enemy)
    for f in fill_form:
        if f["key"] == "attacker_talent" and atk_code:
            f["placeholder"] = f"{attacker} 默认 PVP 点法 {atk_code}"
        elif f["key"] == "defender_talent" and def_code:
            f["placeholder"] = f"{enemy} 默认 PVP 点法 {def_code}"
        elif f["key"] == "attacker_talent":
            f["placeholder"] = "不填则用默认 PVP 点法"
        elif f["key"] == "defender_talent":
            f["placeholder"] = "不填则用默认 PVP 点法"
    return fill_form


def _auto_fill_form(reply: str, session_overrides: dict = None) -> list:
    """LLM 漏输出 <<FILL_FORM>> 标记时的兜底：根据回复引导语补对应表单。

    返回 [{key,label,placeholder,required,value},...]；无法判断返回空列表。
    仅当回复是「主动请求输入某字段」（请求词与该字段名同句相邻，如"请输入航向与制空"）
    时才补表单；「已确认/请选择计算类型」等陈述/无关请求不补（避免误把确认语当输入请求）。
    """
    if not reply:
        return []
    # 请求词 + 字段名相邻匹配（请求词后 0~8 字内出现字段词才认定是请求输入该字段）
    def _ask_field(*field_words):
        for m in re.finditer(r'(请输入|请填写|请选择|请提供|麻烦填|需要填|请补充|请设置|请给出|请告诉我)', reply):
            seg = reply[m.end():m.end() + 10]
            if any(w in seg for w in field_words):
                return True
        # 兜底：字段词后紧跟"？/："且前面有"请"
        for w in field_words:
            for m in re.finditer(re.escape(w), reply):
                pre = reply[max(0, m.start() - 10):m.start()]
                if any(a in pre for a in ('请', '麻烦', '需要')):
                    return True
        return False

    # 攻击方/防守方 buff 天赋：先看是否指名"攻击方/防守方"，再看通用 buff/天赋
    # （"请输入攻击方buff与天赋"→攻击方；"请输入防守方buff"→防守方）
    if _ask_field('攻击方') and _ask_field('buff'):
        return _extract_fill_form('<<FILL_FORM>>attacker_buff=|attacker_talent=')
    if _ask_field('防守方') and _ask_field('buff'):
        return _extract_fill_form('<<FILL_FORM>>defender_buff=|defender_talent=')
    # 未指名攻防的通用 buff/天赋请求：按当前会话阶段判断（有 enemy 则防守方，否则攻击方）
    if _ask_field('buff'):
        ctx = (session_overrides or {}).get('calc_context') or {}
        if ctx.get('enemy'):
            return _extract_fill_form('<<FILL_FORM>>defender_buff=|defender_talent=')
        return _extract_fill_form('<<FILL_FORM>>attacker_buff=|attacker_talent=')
    # 角色选择引导（"请选择防守方""请输入攻击方"等，非 buff）：补对应角色填空表单
    if _ask_field('防守方') and not _ask_field('buff'):
        return _extract_fill_form('<<FILL_FORM>>defender=')
    if _ask_field('攻击方') and not _ask_field('buff'):
        return _extract_fill_form('<<FILL_FORM>>attacker=')
    # 航向/制空 引导
    if _ask_field('航向', '制空'):
        return _extract_fill_form('<<FILL_FORM>>course=|air=')
    return []


def _strip_fill_form_marker(reply: str) -> str:
    """从回复中清除 <<FILL_FORM>> 标记。"""
    if not reply:
        return reply
    return _FILL_FORM_PATTERN.sub('', reply).strip()


# 遭遇战表单标记：<<ENCOUNTER_FORM>>boss=|day_time=|front1=|front2=|front3=|back1=|back2=|back3=
_ENCOUNTER_FORM_PATTERN = re.compile(r'<<ENCOUNTER_FORM>>([^<\n]+)')

# 遭遇战表单字段元信息（黑夜自动 = 150 - 白天，不要求用户输入）
ENCOUNTER_FORM_FIELDS = {
    'boss': {'label': 'boss名', 'placeholder': '嘉波/巴巴托斯/阿丝莫德/亚斯塔露/列拉金/沙克斯/夕茉莉/河豚王/拜蒙/雅蒙/布耶尔', 'required': True},
    'day_time': {'label': '白天时间(秒)', 'placeholder': '如 80（黑夜自动=150-白天）', 'required': True},
    'front1': {'label': '前排1', 'placeholder': '角色名', 'required': True},
    'front2': {'label': '前排2', 'placeholder': '角色名', 'required': True},
    'front3': {'label': '前排3', 'placeholder': '角色名', 'required': True},
    'back1': {'label': '后排1', 'placeholder': '角色名', 'required': True},
    'back2': {'label': '后排2', 'placeholder': '角色名', 'required': True},
    'back3': {'label': '后排3', 'placeholder': '角色名', 'required': True},
}


def _extract_encounter_form(reply: str) -> list:
    """从回复中解析 <<ENCOUNTER_FORM>> 标记，返回 [{key, label, placeholder, required, value}, ...]。
    支持 key 或 key=value 两种形式，value 为预填值（可为空字符串）。无标记返回空列表。
    boss 字段附带 options（拉古兹.json 的 boss 名单），前端渲染为按钮选择而非输入框。"""
    if not reply:
        return []
    m = _ENCOUNTER_FORM_PATTERN.search(reply)
    if not m:
        return []
    raw = m.group(1).strip()
    parts = [p.strip() for p in raw.split('|') if p.strip()]
    result = []
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            k = k.strip()
            v = v.strip()
        else:
            k, v = p, ''
        meta = ENCOUNTER_FORM_FIELDS.get(k, {'label': k, 'placeholder': '', 'required': False})
        result.append({'key': k, 'label': meta['label'], 'placeholder': meta['placeholder'], 'required': meta['required'], 'value': v})
    # boss 字段注入按钮选项（boss 名单 = 拉古兹.json enemies[].name）
    for item in result:
        if item['key'] == 'boss':
            try:
                item['options'] = list(load_encounter_bosses().keys())
            except (OSError, ValueError):
                item['options'] = []
    return result


def _strip_encounter_form_marker(reply: str) -> str:
    """从回复中清除 <<ENCOUNTER_FORM>> 标记。"""
    if not reply:
        return reply
    return _ENCOUNTER_FORM_PATTERN.sub('', reply).strip()


def _extract_suggestions(reply: str) -> list:
    """从回复文本中解析 <<SUGGEST_CALC>> 标记，返回 [{tool_key, label}, ...] 列表。
    无标记时返回空列表。"""
    if not reply:
        return []
    m = _SUGGEST_PATTERN.search(reply)
    if not m:
        return []
    keys = [k.strip() for k in m.group(1).split('|') if k.strip()]
    result = []
    for k in keys:
        label = TOOL_BTN_LABELS.get(k, k)
        result.append({'tool': k, 'label': label})
    return result


def _strip_suggestion_marker(reply: str) -> str:
    """从回复文本中清除 <<SUGGEST_CALC>> 标记及其内容。"""
    if not reply:
        return reply
    return _SUGGEST_PATTERN.sub('', reply).strip()


# 攻略打开标记：<<GUIDE_FILE>>相对路径|标题（每篇一行）
_GUIDE_FILE_PATTERN = re.compile(r'<<GUIDE_FILE>>([^<\n]+?)\|([^<\n]+)')


def _extract_guide_files(reply: str) -> list:
    """从回复中解析 <<GUIDE_FILE>> 标记，返回 [{path, title}, ...]。"""
    if not reply:
        return []
    out = []
    for m in _GUIDE_FILE_PATTERN.finditer(reply):
        path = m.group(1).strip()
        title = m.group(2).strip()
        if path:
            out.append({"path": path, "title": title})
    return out


def _strip_guide_file_marker(reply: str) -> str:
    """从回复中清除 <<GUIDE_FILE>> 标记及其内容。"""
    if not reply:
        return reply
    return _GUIDE_FILE_PATTERN.sub('', reply).strip()


# 攻略库触发标记：<<GUIDE_LIB>>（可带聚焦关键词，如 <<GUIDE_LIB>>遭遇战）
_GUIDE_LIB_PATTERN = re.compile(r'<<GUIDE_LIB>>([^<\n]*)')

# 头像标记：<<AVATAR>>相对路径|角色名（query_ship_info 查询角色时由后端追加，找到才加）
_AVATAR_PATTERN = re.compile(r'<<AVATAR>>([^<\n]+?)\|([^<\n]+)')


def _extract_avatar(texts) -> dict:
    """从回复或工具输出中提取 <<AVATAR>> 标记 → {path, title}；无标记返回 None。"""
    if not texts:
        return None
    for text in texts:
        if not text:
            continue
        m = _AVATAR_PATTERN.search(text)
        if m:
            path = m.group(1).strip()
            if path:
                # 路径分隔符统一为正斜杠（Windows 素材库扫描出反斜杠，URL 需 /）
                path = path.replace('\\', '/')
                return {'path': path, 'title': m.group(2).strip() or '头像'}
    return None


def _strip_avatar_marker(text: str) -> str:
    if not text:
        return text
    return _AVATAR_PATTERN.sub('', text).strip()


def _extract_raw_skills(tool_outputs) -> str:
    """从工具输出中提取「技能描述」原文段（query_ship_info 返回的角色技能原始描述）。

    用于后端强制：技能描述必须是知识库原文（含■①②③【】与完整倍率），
    不允许 LLM 自行概括/改写。提取到则原样追加到回复末尾。
    返回技能段原文（从 [旗舰技] 起），无则空串。
    """
    if not tool_outputs:
        return ""
    for out in tool_outputs:
        if not out or '技能描述' not in out:
            continue
        start = out.find('技能描述')
        section = out[start:]
        end = section.find('---------------------')
        if end != -1:
            section = section[:end]
        # 去掉行首缩进（工具输出用 2 空格缩进，还原为原文）
        lines = [ln.strip() if ln.strip() else '' for ln in section.splitlines()]
        # 去掉首行标题（"技能描述 ---"）
        while lines and (not lines[0] or '技能描述' in lines[0]):
            lines.pop(0)
        text = '\n'.join(lines).strip()
        # 还原 raw 技能文本里的字面 \n 为真实换行（数据源将换行存为 \\n）
        text = text.replace('\\n', '\n')
        return text
    return ""


def _reply_has_raw_skills(reply: str, raw_skills: str) -> bool:
    """判断回复中是否已包含技能原文的关键行（含■的原文行），避免重复粘贴。"""
    if not reply or not raw_skills:
        return False
    key_lines = [ln for ln in raw_skills.splitlines() if '■' in ln or '【' in ln]
    if not key_lines:
        return False
    # 任一关键原文行出现在回复中即视为已含原文
    return any(ln in reply for ln in key_lines)


def _extract_guide_lib(texts) -> str:
    """从回复或工具输出中提取 <<GUIDE_LIB>> 标记的聚焦关键词（无则空串）。
    空标记 <<GUIDE_LIB>> 返回占位符 '*'，前端视为打开全部攻略面板。"""
    if not texts:
        return ""
    for text in texts:
        if not text:
            continue
        m = _GUIDE_LIB_PATTERN.search(text)
        if m:
            kw = m.group(1).strip()
            return kw if kw else "*"
    return ""


def _strip_guide_lib_marker(text: str) -> str:
    if not text:
        return text
    return _GUIDE_LIB_PATTERN.sub('', text).strip()
