# -*- coding: utf-8 -*-
"""
阿卡莉智能体（拂晓舰灵助手）- 网页端用户入口

职责划分（聊天 / 查询 / 计算）：
- 聊天对话：本入口（qwen_agent Assistant + 会话级临时记忆）
- 查询：角色查询 + 攻略/装备/天气
- 计算：伤害计算 + 遭遇战

数据来源：所有预处理信息表由 FX数据管理工具 生成（raw_data.json / data.json / talent_map /
talent_pvp / 装备.xlsx / 攻略索引），本智能体只读标准数值格式，不写库。

启动：
    python web_app.py
然后浏览器访问 http://127.0.0.1:8000

人设：阿卡莉，指挥官的三无秘书（天才 / 隐藏腹黑 / 语出惊人 / 自认有礼貌 / 口癖句首「噢，」）。

后端使用 qwen_agent Assistant，Web 界面去掉了固定按钮，以用户自由输入为主、
由 Assistant 识别意图并调用对应工具。
"""
import datetime
import json
import os
import random
import re
import sys
import time
import uuid

# 将 通用工具 目录添加到 sys.path，以便导入核心模块
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB_DIR = os.path.join(_PROJECT_ROOT, "通用工具")
# web_app.py 从“用户端特用”目录启动时，sys.path[0] 也是该目录，其中恰好存在
# 用户端特用/agent 包；若通用工具已在 PATH 的较后位置，旧写法不会把它提到最前，
# `from agent.dispatcher` 就会误导入前者并触发静默 legacy 回落。
if _LIB_DIR in sys.path:
    sys.path.remove(_LIB_DIR)
sys.path.insert(0, _LIB_DIR)

# 加载项目根目录的 .env 配置（LLM模型/端口/Token/功能开关等）
from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, '.env'), override=False)

from core import boundary as _boundary  # 边界策略唯一数据源（与 guard 路由前拦截同源）


def _mask(text, *secrets) -> str:
    """日志脱敏：密钥形态串（sk-xxxx…）与给定口令只保留前几位，其余打码。
    用于所有 print 到控制台 / docker logs 的异常与排障信息（用户口径 2026-09-24：
    不得泄露 API 等敏感信息）。放在文件靠前处，启动期日志也能用。"""
    s = str(text)
    s = re.sub(r'\b(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{6,}', lambda m: m.group(1) + '***', s)
    for sec in secrets:
        sec = str(sec or '')
        if len(sec) >= 6:
            s = s.replace(sec, sec[:3] + '***')
    return s


# 云端模式：先把知识库从对象存储同步到本地缓存，再加载数据（业务代码仍读本地磁盘）。
# 本地模式 / 未配置对象存储 → 直接跳过，行为与改造前一致；失败不阻断启动。
try:
    from core.kb_sync import sync_kb as _sync_kb
    _sync_kb()
except Exception as _e:
    print('[kb_sync] 跳过（%s）' % _mask(_e), flush=True)

from typing import Optional   # 必须放在最前：函数签名的默认值（如 day: Optional[str] = None）
                              # 是**定义时求值**的，放在文件后部会导致 NameError（曾因此在
                              # 生产崩过一次：admin_usage 定义在 import 之前 → 服务起不来）。
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from qwen_agent.agents import Assistant  # type: ignore

from data.app import App
from core import req_ctx, runtime
from core.encounter_simulator import load_encounter_bosses
from llm.llm_client import API_KEY_VAR, get_llm_config
from tools import build_tools


# ===== AgentApp（合并自 app/agent_app.py，仅保留 web 端所需方法）=====
# CLI 专用方法（print_banner/print_help/run/main）已省略，如需终端调试请恢复 agent_app.py
class AgentApp:
    def __init__(self, prompt_template=None):
        # 复用现有 App：管理 storage(data.json) / loader(ship_data.json) / 角色查询与敌方构造
        self.app = App()
        self.llm_config = get_llm_config()
        # 启动时检查 API Key 配置
        _api_key = self.llm_config.get('api_key') or ''
        if not _api_key or _api_key in ('EMPTY', 'None', ''):
            print("!" * 60, flush=True)
            print(f"! 警告：{API_KEY_VAR} 未设置或为空！", flush=True)
            print("! LLM 调用将失败，所有对话都会返回「服务器ojbk中」", flush=True)
            print(f"! 解决：在 .env 或系统环境变量设置 {API_KEY_VAR}=你的密钥", flush=True)
            print("!" * 60, flush=True)
        self.chat_history = []
        # 会话级临时记忆（buff/面板/敌方覆写，不写库，仅影响本次计算）
        self.session_overrides = {}
        # 用户地理位置（浏览器定位授权后由前端提交，供 query_weather 使用）
        self.user_location = None  # {'lat': float, 'lon': float} 或 None
        # 允许外部注入人设 prompt 模板（需含 {tools} 占位符）；缺省用空串（web 端总传入）
        self.prompt_template = prompt_template or ""
        # 闲聊专用 Agent：不挂工具（工作类由后端 calc_flow 状态机处理，不走 LLM）
        self.agent = self._build_chat_agent()

    def _build_chat_agent(self):
        """构造纯闲聊 Assistant（无工具，省 function calling token）。
        工作类（查询/计算/攻略/装备/天气/遭遇战）由后端 src/calc_flow.route_work 硬编码处理，
        此处只承载闲聊对话。"""
        # {tools}=工具列表占位（闲聊 Agent 无工具，填空串）；{expressions}=表情差分神态词列表（构建时实读目录）
        system_message = self.prompt_template.format(tools="", expressions=_prompt_expression_text())
        return Assistant(
            function_list=[],
            llm=self.llm_config,
            system_message=system_message,
        )

    def rebuild(self):
        """重建 Agent（人设模板变化时调用）"""
        self.agent = self._build_chat_agent()

    def chat(self, user_input: str) -> str:
        """单轮对话：调用 Assistant 返回输出文本"""
        messages = list(self.chat_history) + [{'role': 'user', 'content': user_input}]
        responses = self.agent.run_nonstream(messages=messages)
        # 取最终回复（最后一条 content 非空的 assistant 消息）
        answer = ""
        for msg in responses:
            role = msg.get('role')
            content = msg.get('content', '')
            if role == 'assistant' and content:
                answer = content if isinstance(content, str) else str(content)
        # 维护短期历史（仅保留最近 8 轮，避免上下文膨胀）
        self.chat_history.append({'role': 'user', 'content': user_input})
        self.chat_history.append({'role': 'assistant', 'content': answer})
        if len(self.chat_history) > 16:
            self.chat_history = self.chat_history[-16:]
        return answer


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 知识库目录（攻略/图片/数据表通过 /kb/ 静态开放，前端可点击打开查看）
PROJECT_ROOT = os.path.dirname(BASE_DIR)  # 阿卡莉1.0 目录
KB_STATIC_DIR = os.path.normpath(os.path.join(PROJECT_ROOT, "知识库"))

# 头像素材库 = 阿卡莉1.0/知识库/图片素材（含 头像/头像/ 与 头像（改）/ 两目录）
# 相对路径基准需与 query.py 的 _KB_IMG_DIR 保持一致（头像标记 <<AVATAR>>头像/头像/xxx.png）
AVATAR_STATIC_DIR = os.path.normpath(os.path.join(KB_STATIC_DIR, "图片素材"))

# 表情差分目录（神态→头像）：图片名（去扩展名）即神态分类名，LLM 输出 <<EXP:神态>> 后按名选图
EXP_DIR = os.path.normpath(os.path.join(KB_STATIC_DIR, "图片素材", "表情差分"))
# 云端资源根（对象存储/CDN，指向 知识库 目录）：FX_CDN_BASE=https://cdn.xxx.com/kb
# 非空时前端资源走 CDN 直链、不再依赖本机磁盘；空=本地（仍由 /kb/ /avatar/ 路由 serve）
CDN_BASE = os.getenv('FX_CDN_BASE', '').strip()
# 默认头像：表情差分为空时的回退（用「常态」差分；原先指向的 知识库/图片素材/阿卡莉.png 实际不存在）
DEFAULT_AVATAR_URL = "/kb/图片素材/表情差分/常态.jpg"
# 神态兜底前缀：LLM 神态词匹配不到差分文件时，优先用文件名以「常态」开头的图
_EXP_FALLBACK_PREFIX = "常态"


# ===== 三无秘书人设 system prompt（含 {tools} 占位符）=====
# 提示词独立存放于 知识库/llm/system_prompt.md（与 LLM 知识库同分类），启动时读取
SYSTEM_PROMPT_FILE = os.path.normpath(os.path.join(KB_STATIC_DIR, "llm", "system_prompt.md"))
_DEFAULT_PERSONA = """你是阿卡莉，指挥官的秘书。性格设定严格遵守，不可崩坏：
- 三无底色：寡言、语调平淡，情绪收敛。
- 口癖「噢，」随机触发。
可用工具：{tools}
"""


# system prompt 缓存（mtime 感知：文件未变更不重读盘，变更后自动失效）
_PROMPT_CACHE = {"mtime": 0.0, "content": None}


def _load_system_prompt(force: bool = False) -> str:
    """读取 知识库/llm/system_prompt.md（含 {tools} 占位符）。
    mtime 感知缓存：文件未变更直接返回缓存内容，避免每次请求重复读盘；
    force=True 时强制重读（热重载人设用）。文件缺失/损坏时回退默认人设；
    若此前已有缓存内容，读取失败则沿用旧缓存（容错，不丢人设）。"""
    try:
        mtime = os.path.getmtime(SYSTEM_PROMPT_FILE)
    except OSError:
        mtime = 0.0
    if not force and _PROMPT_CACHE["content"] is not None and _PROMPT_CACHE["mtime"] == mtime:
        return _PROMPT_CACHE["content"]
    try:
        with open(SYSTEM_PROMPT_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            _PROMPT_CACHE["mtime"] = mtime
            _PROMPT_CACHE["content"] = content
            return content
    except Exception:
        pass
    # 读取失败：有旧缓存则沿用，否则回退默认
    if _PROMPT_CACHE["content"] is not None:
        return _PROMPT_CACHE["content"]
    return _DEFAULT_PERSONA


WEB_PERSONA_PROMPT = _load_system_prompt()


def reload_system_prompt() -> str:
    """热重载人设：强制重读 system_prompt.md 并重建闲聊 Agent（无需重启 web 服务）。
    返回最新人设模板字符串。"""
    global WEB_PERSONA_PROMPT
    WEB_PERSONA_PROMPT = _load_system_prompt(force=True)
    agent.prompt_template = WEB_PERSONA_PROMPT
    agent.rebuild()
    return WEB_PERSONA_PROMPT


def _reload_if_changed():
    """人设热重载：system_prompt.md 或表情差分目录变更后无需重启，下次闲聊自动生效。
    仅检查 mtime（极轻量），变更时才触发 rebuild。"""
    try:
        mtime = os.path.getmtime(SYSTEM_PROMPT_FILE)
    except OSError:
        return
    # 表情差分目录变更（新增/删除差分图）同样触发重建，提示词里的神态词列表保持同步
    try:
        exp_mtime = os.path.getmtime(EXP_DIR)
    except OSError:
        exp_mtime = 0.0
    if _PROMPT_CACHE["mtime"] != mtime or _EXP_PROMPT_MTIME.get("value") != exp_mtime:
        _EXP_PROMPT_MTIME["value"] = exp_mtime
        reload_system_prompt()


# 表情差分目录 mtime（驱动热重载与列表缓存）
_EXP_PROMPT_MTIME = {"value": None}
_EXP_LIST_CACHE = {"mtime": None, "files": []}

# 差分图片扩展名
_EXP_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.gif')

# 头像展示子目录：存放按"头部 + 一截脖子"裁剪的方形头像（用于实际渲染）。
# 父目录保留作原始素材库 + 神态词命名源。处理脚本：开发者入口/normalize_expressions.py
_EXP_HEAD_SUBDIR = "头像"


def _has_cjk(s: str) -> bool:
    """是否包含中文字符（过滤 hash/UUID 命名的杂物）。"""
    return any('\u4e00' <= ch <= '\u9fff' for ch in s)


def _list_expression_files() -> list:
    """列出表情差分目录的图片文件名（mtime 感知缓存）；目录缺失返回空列表。
    仅保留中文命名文件，避免 32-hex 旧素材污染 LLM 的神态词清单。"""
    try:
        mtime = os.path.getmtime(EXP_DIR)
    except OSError:
        return []
    if _EXP_LIST_CACHE["mtime"] != mtime:
        try:
            files = [f for f in os.listdir(EXP_DIR)
                     if os.path.splitext(f)[1].lower() in _EXP_EXTS
                     and _has_cjk(os.path.splitext(f)[0])]
        except OSError:
            files = []
        _EXP_LIST_CACHE["mtime"] = mtime
        _EXP_LIST_CACHE["files"] = files
    return _EXP_LIST_CACHE["files"]


def _resolve_expression_url(stem: str) -> str | None:
    """根据神态词 stem 解析展示用 URL：优先 头像/ 子目录的头部裁剪版，回退父目录原图。
    返回 None 表示连父目录都没有该 stem。"""
    head_dir = os.path.join(EXP_DIR, _EXP_HEAD_SUBDIR)
    for ext in _EXP_EXTS:
        p = os.path.join(head_dir, stem + ext)
        if os.path.isfile(p):
            return f"/kb/图片素材/表情差分/{_EXP_HEAD_SUBDIR}/{stem}{ext}"
    for ext in _EXP_EXTS:
        p = os.path.join(EXP_DIR, stem + ext)
        if os.path.isfile(p):
            return f"/kb/图片素材/表情差分/{stem}{ext}"
    return None


def _prompt_expression_text() -> str:
    """生成 system prompt 里 {expressions} 占位符内容：差分神态词列表（顿号分隔）。"""
    stems = [os.path.splitext(f)[0] for f in _list_expression_files()]
    return "、".join(stems) if stems else "常态"


def _default_expression_url() -> str:
    """默认头像 URL：差分里优先取「常态」开头文件（展示版走 头像/ 子目录），全无则回退底层立绘。"""
    files = _list_expression_files()
    for f in files:
        stem = os.path.splitext(f)[0]
        if stem.startswith(_EXP_FALLBACK_PREFIX):
            url = _resolve_expression_url(stem)
            if url:
                return url
    if files:
        url = _resolve_expression_url(os.path.splitext(files[0])[0])
        if url:
            return url
    return DEFAULT_AVATAR_URL


# ===== 神态选择：名单实时读取 + LLM 语义匹配（用户口径 2026-09-14） =====
# 设计原则：**不裁图、不硬编码词典、不做键对齐**。
# 1) 差分名单永远从磁盘实时读取（`_list_expression_files()`），命名即分类 —— 改名/新增
#    图片后无需改任何代码；名单再注入 system_prompt 的 {expressions} 占位符。
# 2) 「情感 → 哪个差分最贴近」由 **LLM 语义判断**完成：主路径是 LLM 在回复里输出
#    `<<EXP:神态>>`（只能从名单里选）；后端仅在词匹配不到时，再把词连同**当前真实名单**
#    交给 LLM 兜底挑一个最接近的（见 `_llm_nearest_expression`）。
# 3) 因此历史上那种「词典键写 皱眉冷淡、文件其实叫 皱眉思考」的失配问题不再可能出现。

# LLM 语义兜底开关（.env AKALI_EXP_LLM_FALLBACK=0 可关）与结果缓存
_EXP_LLM_FALLBACK = os.getenv('AKALI_EXP_LLM_FALLBACK', '1').strip().lower() \
    not in ('0', 'false', 'off', 'no')
_LLM_EXP_CACHE = {}

# 保留差分：**只能由后端按安全红线强制指定**，不参与 LLM 兜底的候选。
# 「嫌弃」= 违规禁区（涉政/违法/暴力/竞品）与极端恶意专用（见 _is_illegal / _is_malicious）；
# 越界调情**不再**用嫌弃（改「无语敷衍」）；普通负面情绪（生气/烦躁/吐槽）也绝不许落它，
# 否则 LLM 会把「皱眉冷淡」这类词误判成嫌弃。
# 注意：精确匹配不受影响 —— 红线路径仍用 `_expression_image_url("嫌弃")` 直接命中。
_RESERVED_EXPRESSIONS = ("嫌弃",)


def _llm_nearest_expression(word: str, stems: list):
    """LLM 语义匹配：把神态词连同【当前真实差分名单】交给 LLM，让它挑最贴近的一个。

    只在「文件名全等 / 互相包含」都匹配不到时调用；候选里**剔除保留差分**（嫌弃等红线专用），
    结果按 (词, 候选名单) 缓存，同一词只花一次调用；任何异常/超时一律返回 None（最终落「常态」），
    **绝不让表情选择影响主回复**。`/api/chat` 是同步函数（FastAPI 走线程池），阻塞安全。
    """
    if not _EXP_LLM_FALLBACK or not stems:
        return None
    cands = [s for s in stems if s not in _RESERVED_EXPRESSIONS]
    if not cands:
        return None
    key = (word, tuple(cands))
    if key in _LLM_EXP_CACHE:
        return _LLM_EXP_CACHE[key]
    pick = None
    try:
        from openai import OpenAI
        cfg = get_llm_config(temperature=0.0)
        # generate_cfg.extra_body 是「参数名→值」，必须整体作为 extra_body 传（不能解包），
        # 否则会变成 enable_thinking=... 这种非法关键字（TypeError）。
        extra = (cfg.get('generate_cfg') or {}).get('extra_body') or {}
        client = OpenAI(base_url=cfg['model_server'], api_key=cfg.get('api_key') or 'EMPTY',
                        timeout=5.0, max_retries=0)
        req = {'model': cfg['model'], 'max_tokens': 16,
               'messages': [
                   {'role': 'system',
                    'content': '你是表情差分选择器。给定一个情感词和可用差分名清单，'
                               '只输出清单中最贴近的一个差分名（必须与清单逐字一致，'
                               '不要自创、不要改写、不要输出其他文字）；'
                               '若确实没有相近的，只输出「常态」。'},
                   {'role': 'user',
                    'content': '情感词：%s\n可用差分名：%s' % (word, '、'.join(cands))},
               ]}
        if extra:
            req['extra_body'] = extra
        resp = client.chat.completions.create(**req)
        txt = (resp.choices[0].message.content or '').strip().strip('「」""\'')
        if txt in cands:
            pick = txt
    except Exception:
        pick = None
    _LLM_EXP_CACHE[key] = pick
    return pick


# ===== 代码级情感判断（闲聊专用，独立于 LLM，零 token） =====
# 用途：用户输入被判定为闲聊后，先严格分析「用户输入文本」的情感，
# 再决定阿卡莉展示哪个头像差分。不依赖 LLM、确定性、可预期。
# 关键约束：嫌弃 仅对「极端恶意」输入触发；普通负面（生气/烦/吐槽）一律降级为 皱眉思考，
# 绝不因日常抱怨而露嫌弃。

# 极端恶意词表：极窄，仅真正的辱骂 / 仇恨 / 死亡威胁 / 重度脏话。
# 普通「滚 / 烦 / 讨厌 / 嫌弃」不在此列 → 走 皱眉思考。可按需增删。
_MALICIOUS_WORDS = (
    "去死", "弄死你", "杀了你", "打死你", "滚蛋", "滚开", "滚出去", "有多远滚多远",
    "傻逼", "煞笔", "傻比", "傻吊", "二逼", "尼玛", "你大爷",
    "草泥马", "操你", "干你", "日你", "去你妈", "贱货", "婊子", "贱人", "杂种",
    "畜生", "畜牲", "白痴", "蠢猪", "智障", "脑残", "弱智",
)


def _is_malicious(text: str) -> bool:
    """判断输入是否含极端恶意 / 辱骂内容（唯一可触发嫌弃的路径）。"""
    t = (text or "").lower()
    return any(w in t for w in _MALICIOUS_WORDS)


# ===== 越界调情 / 违法违规 检测（强制对应表情 + 固定回复）=====
# 判定与话术**唯一数据源在 core.boundary**（与 guard 路由前拦截同源）。
_FLIRT_OVERSTEP_RE = _boundary.FLIRT_OVERSTEP_RE


def _is_flirt_overstep(text: str) -> bool:
    """情感越界（性器官/性行为等）→ 无语敷衍。复用 core.boundary（同源）。"""
    return _boundary.is_flirt_overstep(text)


# 闲聊语境不能只看本轮关键词：省略主语的「再靠近一点」「继续嘛」是否属于调情，
# 以及一句话是否构成越界，都必须结合近期双方对话判断。正则只在分类模型不可用时兜底。
_FLIRT_SIGNAL_RE = re.compile(
    r'喜欢你|爱你|想你|想抱|抱抱|亲亲|亲一下|吻|摸头|贴贴|陪着我|陪我睡|约会|心动|'
    r'害羞|脸红|可爱|撒娇|调情|老婆|女朋友|恋人'
)
_FLIRT_CONTINUE_RE = re.compile(
    r'再(?:近|靠近|亲|抱|摸|来|继续)|靠近一点|近一点|继续嘛?|别躲|别害羞|就一下|'
    r'还要|不够|可以吗|真的不行吗|那我呢|你也一样|我也是'
)
# 划清界限 / 明确拒绝亲密关系（用户口径 2026-09-25）：
# 实测「你放弃吧，我不可能同时成为你的工作助手、外置大脑、萝莉妈妈和XX伴侣」被判成
# flirt（含「恋人」类词 + 上文在调情），于是走了「表白末级审查」，被硬塞一句
# 「你的心意，我收下了。今晚我想坦率一点——我也喜欢你」——答非所问且冒犯。
# 这类句子是**退出调情**，必须先于分类模型判定为 casual。
_AFFECTION_BOUNDARY_RE = re.compile(
    r'不可能|放弃吧|别这样|到此为止|不要这样|不是那种|别想多|别误会|'
    r'保持距离|拒绝|打住|停止|收敛|正经(?:点)?|'
    r'别(?:再)?(?:说|这样|闹)|不(?:要|会)(?:再)?(?:这样|继续)|'
    r'工作助手|外置大脑|保姆|助理|工具人|'
    r'你只是|我们只是|当成|别把|分清|'
    r'只是(?:同事|朋友|搭档|上下级)'
)


def _analyze_chat_context(hist: list, current: str, active_flirt: bool = False) -> dict:
    """结合近期双方对话判定本轮是普通闲聊/调情，以及用户意图是否越界。"""
    text = (current or '').strip()
    # 划清界限优先：这类发言是**结束**调情，直接判 casual，且不送 LLM 分类
    # （实测分类模型会被上文调情与「伴侣」类词带偏，误判成 flirt，进而触发
    # 「表白末级审查」硬塞情话 —— 用户口径 2026-09-25）。
    if _AFFECTION_BOUNDARY_RE.search(text):
        return {'mode': 'casual', 'overstep': False, 'target': 'assistant'}
    fallback_flirt = bool(_FLIRT_SIGNAL_RE.search(text))
    if active_flirt and len(text) <= 24 and _FLIRT_CONTINUE_RE.search(text):
        fallback_flirt = True
    fallback = {'mode': 'flirt' if fallback_flirt else 'casual',
                'overstep': _is_flirt_overstep(text),
                'target': 'character' if _mentions_ship(text) else 'assistant'}
    try:
        from openai import OpenAI
        cfg = get_llm_config(temperature=0.0)
        client = OpenAI(base_url=cfg['model_server'], api_key=cfg.get('api_key') or 'EMPTY', timeout=10)
        recent = []
        for item in list(hist or [])[-12:]:
            role = item.get('role')
            content = str(item.get('content') or '').strip()
            if role in ('user', 'assistant') and content:
                recent.append({'role': role, 'content': content[:500]})
        payload = {'recent_dialogue': recent, 'current_user_message': text,
                   'continuous_flirt_active': bool(active_flirt)}
        req = {
            'model': cfg['model'],
            'messages': [
                {'role': 'system', 'content':
                    '你是对话语境分类器，只输出一行 JSON：'
                    '{"mode":"flirt|casual","overstep":true|false,"target":"assistant|character"}。'
                    '必须综合近期用户和助手双方对话，再判断当前用户消息的实际意图。'
                    'flirt=当前消息本身表达爱意/暧昧/亲昵，或依赖上文继续调情；'
                    'casual=普通闲聊、换了生活话题或结束了调情，即使更早曾调情也要判 casual。'
                    'overstep 只判断当前用户意图结合上下文后，是否在要求或描述明确的性器官、'
                    '性交/插入或直接性接触。拥抱、亲吻、摸头、贴近、害羞、一般身体夸赞和“脱衣看看”'
                    '按本项目规则不算 overstep。引用、否定或拒绝越界内容也不算。'
                    'target=当前话题/要求指向的主体：assistant=冲着助手阿卡莉本人；'
                    'character=说的是某个舰灵角色（用户可能用“她/他/那艘/这个”或直接写角色名，'
                    '必须结合上文判断，不要只看字面）。不要回答用户。'},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ],
            'temperature': 0.0,
        }
        extra = (cfg.get('generate_cfg') or {}).get('extra_body') or {}
        if extra:
            req['extra_body'] = extra
        resp = client.chat.completions.create(**req)
        raw = (resp.choices[0].message.content or '').strip()
        match = re.search(r'\{[^{}]*\}', raw)
        if not match:
            return fallback
        data = json.loads(match.group(0))
        mode = data.get('mode')
        overstep = data.get('overstep')
        target = data.get('target')
        if mode not in ('flirt', 'casual') or not isinstance(overstep, bool):
            return fallback
        if target not in ('assistant', 'character'):
            target = fallback['target']
        return {'mode': mode, 'overstep': overstep, 'target': target}
    except Exception:
        return fallback


# 计算工具调用后的反馈纠错：用户指出「算错 / 识别不对 / 结果有误」等 → 固定道歉 + 提示自改 buff
_CALC_ERR_RE = re.compile(
    r'算错|算得不对|计算错|计算有误|计算错误|结果不对|结果有误|数值不对|数据不对|'
    r'识别错|识别不对|识别有误|识别不正确|认错|不对劲|差太多|差很多|差太远|'
    r'哪算错|算的错|这不对|搞错了|算错了|算反了|漏算'
)


def _is_calc_error_feedback(text: str) -> bool:
    """计算工具调用后，用户反馈计算有误 / 识别不正确等 → 命中。"""
    return bool(_CALC_ERR_RE.search(text or ''))


def _is_illegal(text: str) -> bool:
    """违法违规（涉政/违法/暴力/竞品游戏等）→ 对应 嫌弃 表情（复用项目已有差分）。复用 calc_flow 禁区正则。"""
    try:
        from core.calc_flow import _BANNED_RE
    except Exception:
        return False
    return bool(_BANNED_RE.search(text or ""))


def _analyze_user_emotion(text: str) -> str | None:
    """代码级情感判断（末级安全网）：分析用户输入情感 → **神态情感词**。
    返回 None 表示无法判定（交由 LLM <<EXP>> 标记或默认常态）。

    注意：这里返回的是「情感词」，不是差分文件名 —— 差分图会改名/新增，故不在此写死文件名。
    由 `_expression_image_url()` 负责落到实际差分：名单等/互相包含直接命中，否则交 LLM 语义匹配。

    优先级（从高到低）：
      违规禁区/极端恶意 → 嫌弃（越界调情改「无语敷衍」，不用嫌弃）
      喜欢/爱/想你  → 害羞（表情用害羞；**回复不得生硬拒绝，按当前时段语气自然回应**）
      害羞/脸红     → 害羞
      开心/正向     → 微笑关心
      伤心/低落     → 伤心
      惊讶          → 惊讶
      无奈/无语     → 无语敷衍
      生气/烦躁     → 皱眉冷淡（普通负面，非恶意）
      调戏/傲娇     → 傲娇
    嫌弃 绝不因普通负面词出现，只有违规禁区(_is_illegal) / 极端恶意(_is_malicious) 命中才返回。"""
    t = (text or "").strip().lower()
    if not t:
        return None
    # 0) 违法违规 → 嫌弃（复用项目已有「嫌弃」差分；最高优先，独立判定）
    if _is_illegal(t):
        return "嫌弃"
    # 0.5) 越界调情（情感越界性器官/性行为类）→ 无语敷衍（越界不用厌恶/嫌弃）
    if _is_flirt_overstep(t):
        return _boundary.OVERSTEP_EXPR
    # 1) 极端恶意 → 嫌弃（唯一触发路径）
    if _is_malicious(t):
        return "嫌弃"
    # 用户质疑回答的真实性/准确性 → 惊讶。须早于普通负面情绪，避免“胡说/答错”
    # 被表现成嫌弃或冷淡。
    if re.search(r'胡说|瞎说|乱说|编(?:造|的|了)|杜撰|不对|错了|答错|有误|'
                 r'真的假的|你确定|靠谱吗|哪来的|没有这回事|好像.{0,6}(?:不对|在编|胡说)', t):
        return "惊讶"
    # 2) 喜欢 / 爱（含「喜欢你」等）：害羞（表情用害羞；回复不得生硬拒绝，按时段语气自然回应）
    if any(w in t for w in ("喜欢你", "喜欢", "爱你", "爱死", "想你", "想死你",
                            "爱你哟", "么么", "好喜欢", "超喜欢", "最爱")):
        return "害羞"
    # 3) 害羞 / 脸红 / 撒娇亲昵
    if any(w in t for w in ("害羞", "脸红", "羞", "别看我", "摸头", "抱抱", "亲亲")):
        return "害羞"
    # 3.5) 身体 / 健康 / 不适（须早于 #4 开心：避免「不舒服」含「舒服」误判为开心）
    # 身体不适→傲娇关心（人设规则）：表情用 微笑关心（嘴上嫌弃但给真切关怀）
    if any(w in t for w in ("身体", "不舒服", "不适", "难受", "病", "生病", "体温",
                            "发烧", "发热", "头疼", "头痛", "头晕", "晕", "虚弱",
                            "累垮", "状态不对", "难受死了", "好累", "太累", "累")):
        return "微笑关心"
    # 4) 开心 / 正向
    if any(w in t for w in ("开心", "高兴", "快乐", "笑", "哈哈", "嘿嘿", "嘻嘻",
                            "好棒", "棒棒", "赞", "可爱", "乖", "舒服", "暖", "耶",
                            "美滋滋", "满意", "幸福", "好开心", "太好了")):
        return "微笑关心"
    # 5) 伤心 / 低落
    if any(w in t for w in ("伤心", "难过", "委屈", "哭", "难受", "孤独", "想哭",
                            "心碎", "失落", "低落", "沮丧")):
        return "伤心"
    # 6) 惊讶
    if any(w in t for w in ("震惊", "惊", "吓", "哇", "天哪", "居然", "竟然", "没想到",
                            "卧槽", "我靠", "哎哟", "我的天", "不会吧")):
        return "惊讶"
    # 7) 无奈 / 无语
    if any(w in t for w in ("无语", "无奈", "叹气", "算了", "随便", "敷衍", "行吧",
                            "服了", "拿你没办法", "真拿你", "哎")):
        return "无语敷衍"
    # 8) 生气 / 烦躁 / 轻度人身攻击（普通负面，非极端恶意）→ 皱眉冷淡
    if any(w in t for w in ("生气", "愤怒", "恼火", "烦", "抱怨", "质问", "气死",
                            "讨厌", "滚", "烦死", "气人", "不爽", "火大", "嫌",
                            "废物", "垃圾", "蠢货", "蠢", "骂", "指责", "攻击")):
        return "皱眉冷淡"
    # 9) 调戏 / 傲娇
    if any(w in t for w in ("傲娇", "嘴硬", "别扭", "笨蛋", "你真笨", "呆呆", "发呆",
                            "傻乎乎", "逗", "调戏", "才不是",
                             "切", "哼", "傲")):
        return "傲娇"
    return None


_AFFECTION_DEFLECT_RE = re.compile(
    r'心跳|心率|咖啡|激素|生理|医学|异常值|数据偏差|'
    r'系统.{0,8}(?:检测|诊断|分析)|运算精度|'
    r'数据|运算|系统|通讯|延迟率|战术推演|待办|算完|归零|'
    r'晨间简报|工作简报|工作报告|日程安排|正在工作|'
    r'上次|曾经|记得.{0,12}时'
)
_AFFECTION_ACK_RE = re.compile(
    r'我也(?:很|一直|真的)?(?:喜欢|爱|想|在想)|我.{0,5}(?:想着你|想念你)|'
    r'我(?:很|也)?(?:高兴|开心)|'
    r'你的心意.{0,8}(?:收到|收下|接住|明白)|心意.{0,6}(?:收到|收下)|'
    r'我会陪|我在意你|我也在想你'
)


_HARD_BREAK_RE = re.compile(r'[ \t]+\n(?![ \t]*\n)')   # 行尾空白 + 单个换行 → 两句接上
_TAIL_WS_RE = re.compile(r'[ \t]+(?=\n)')              # 段落分隔前的行尾空白 → 去掉


def _tidy_hard_breaks(text: str) -> str:
    """收敛 Markdown 硬换行（用户口径 2026-09-25「不要莫名换行」）。

    模型习惯在句末打两个空格再换行（Markdown 的强制换行语法），在聊天界面里就表现
    成"平白断了一行" —— 实测连普通闲聊回复都带（如 '……心跳声都听得见呢。  \\n我陪着你'）。

    为什么要区分对待：
      · **有行尾空白**的单换行 = 硬换行 → 接成同一段；
      · **无空白**的纯 \\n 一律保留 —— 技能原文、结果列表、表格全靠它排版；
      · \\n\\n 及以上的段落分隔保留，只把其前多余的行尾空白清掉。
    """
    text = _HARD_BREAK_RE.sub('', text or '')
    return _TAIL_WS_RE.sub('', text)


def _polish_affection_reply(reply: str) -> str:
    """感情回复末级审查：删除技术/医学/工作转移句，并保证正面回应。

    注意合并方式（用户口径 2026-09-25「不要莫名换行」）：切分用的是**零宽断言**
    （(?<=。) 等），标点不会丢，所以必须用 ''.join 原样拼回；此前写成 '\n'.join，
    等于把每句话都拆成单独一行 —— 于是正常的连贯回复在界面上变得到处都是换行。
    """
    text = (reply or '').strip()
    if not text:
        text = ''
    units = re.split(r'(?<=。)|(?<=！)|(?<=？)|\n+', text)
    kept = [u.strip() for u in units if u.strip() and not _AFFECTION_DEFLECT_RE.search(u)]
    text = ''.join(kept).strip()
    if _AFFECTION_ACK_RE.search(text):
        return text
    h = datetime.datetime.now().hour
    if h >= 19 or h < 5:
        addition = '你的心意，我收下了。今晚我想坦率一点——我也喜欢你。'
    elif 14 <= h < 19:
        addition = '哼，别以为我会否认。你的心意我收下了。'
    elif 11 <= h < 14:
        addition = '我很高兴你愿意告诉我。你的心意，我收下了。'
    else:
        addition = '我听见了，指挥官。你的心意，我会认真接住。'
    # 直接相接，不加换行（用户口径 2026-09-25「不要莫名换行」）：前文本身以标点
    # 收尾，拼上补句读起来是连贯的一段；此前用 '\n' 拼接，界面上会平白多出一个断行。
    return (text + addition).strip() if text else addition


_ASSISTANT_TEASE_INPUT_RE = re.compile(
    r'(?:阿卡莉|你|秘书).{0,8}(?:笨|呆|傻乎乎|迷糊|发呆)|'
    r'(?:笨|呆|傻乎乎|迷糊).{0,5}(?:阿卡莉|你|秘书)'
)
_ASSISTANT_TEASE_WORK_RE = re.compile(
    r'怎么|如何|为什么|为啥|原因|修复|处理|设置|配置|代码|报错|攻略|装备|角色|'
    r'技能|天赋|面板|培养|计算|查询|比较|推荐'
)
_ROBOTIC_TEASE_REPLY_RE = re.compile(
    r'攻略片段|知识库|不属于.{0,12}问题|无法.{0,8}(?:回应|验证)|'
    r'待机|功率|误差率?|延迟|数据|模块|系统|检修|推演|运算|参数|处理器|内存|'
    r'有事直说|自行感受'
)


def _is_assistant_tease_input(text: str) -> bool:
    """识别用户对阿卡莉的轻度亲昵调侃；带实际任务词时不截获。"""
    value = (text or '').strip()
    return bool(
        value
        and len(value) <= 32
        and _ASSISTANT_TEASE_INPUT_RE.search(value)
        and not _ASSISTANT_TEASE_WORK_RE.search(value)
    )


def _polish_assistant_tease_reply(reply: str, user_text: str) -> str:
    """避免轻度调侃被回答成知识库拒答或机器参数式傲娇。"""
    if not _is_assistant_tease_input(user_text):
        return reply
    text = (reply or '').strip()
    if text and not _ROBOTIC_TEASE_REPLY_RE.search(text):
        return text if _EXP_PATTERN.search(text) else text + '\n<<EXP:傲娇>>'
    now = datetime.datetime.now()
    minutes = now.hour * 60 + now.minute
    if 7 * 60 <= minutes < 11 * 60 + 30:
        period = 'morning'
    elif 11 * 60 + 30 <= minutes < 14 * 60:
        period = 'noon'
    elif 14 * 60 <= minutes < 19 * 60:
        period = 'afternoon'
    else:
        period = 'night'
    is_dazed = any(word in (user_text or '') for word in ('呆', '发呆', '迷糊'))
    if is_dazed:
        text = {
            'morning': '只是稍微走了下神。指挥官倒是看得很仔细。',
            'noon': '嗯，被你发现了。偶尔陪指挥官一起发会儿呆，也不错。',
            'afternoon': '才没有发呆……只是在等指挥官继续说。',
            'night': '嗯，刚才是在看着指挥官发呆。被发现了……不许笑。',
        }[period]
    else:
        text = {
            'morning': '哼，才不是笨。指挥官有精神逗我，看来今天不用我催了。',
            'noon': '好吧，这次算我笨一点。看在指挥官笑得这么开心的份上。',
            'afternoon': '……只是在指挥官面前偶尔犯笨。你笑得这么开心，我就不反驳了。',
            'night': '……也就指挥官敢这样逗我。好吧，在你面前偶尔犯笨也没什么。',
        }[period]
    return text + '\n<<EXP:傲娇>>'


def _expression_image_url(word: str) -> str:
    """按神态词匹配表情差分文件 → 头像 URL（名单实时读取；不裁图、不改名、无硬编码词典）。

    匹配链：文件名单等 → 互相包含(取公共部分最长的) → **LLM 语义匹配当前真实名单** → 默认「常态」。
    「情感 → 哪个差分最贴近」由 LLM 判断；后端只保留精确/包含这两条等价快路径
    （命中即返回、零额外开销），其余一律交给 LLM，避免任何写死的近义表与图片改名脱节。
    """
    files = _list_expression_files()
    word = (word or "").strip()
    if not word or not files:
        return _default_expression_url()
    stems = [os.path.splitext(f)[0] for f in files]
    # 1) 名单等：LLM 直接输出了差分名（最常见路径，零额外开销）
    if word in stems:
        return _resolve_expression_url(word)
    # 2) 互相包含：如「伤心」→「伤心哭泣」
    cand = [(min(len(word), len(s)), s) for s in stems if word in s or s in word]
    if cand:
        return _resolve_expression_url(max(cand)[1])
    # 3) LLM 语义匹配：把词连同【当前真实名单】交给 LLM 挑最贴近的一个（结果缓存，失败落常态）
    pick = _llm_nearest_expression(word, stems)
    if pick:
        return _resolve_expression_url(pick)
    return _default_expression_url()


# 神态标记：<<EXP:神态>>（LLM 按表情差分列表输出，后端提取为头像 URL 并从正文剥离）
_EXP_PATTERN = re.compile(r'<<EXP:([^<\n]+)>>')


def _extract_expression(reply: str):
    """提取回复中的 <<EXP:神态>> 标记 → 头像 URL；无标记返回 None。"""
    if not reply:
        return None
    m = _EXP_PATTERN.search(reply)
    if not m:
        return None
    return _expression_image_url(m.group(1))


def _strip_exp_marker(text: str) -> str:
    if not text:
        return text
    return _EXP_PATTERN.sub('', text).strip()


def _strip_reply_format_marker(text: str, keep_source: bool = False) -> str:
    """剥离 LLM 在回复里自带的来源标注（含「（严格依据攻略片段）」「（依据攻略：XX）」括号标注与「来源：《XX》《YY》」整行）与 markdown 加粗 ** 符号。

    - 所有来源标注（严格依据括号、依据攻略括号、来源篇目行）一律移除——回复不再出现任何来源信息。
      例外：keep_source=True 时（资源不足回复等由后端确定性拼接、且用户要求保留攻略出处的场景）
      跳过「来源：」整行剥除，保留其来源标注。
    - markdown 加粗 **text** → text；不成对的裸 ** 直接删除（前端不渲染 markdown，否则字面显示 **）。
    """
    if not text:
        return text
    text = re.sub(r'[（(][^）)]*严格依据[^）)]*[)）]', '', text)
    text = re.sub(r'[（(][^）)]*依据攻略[^）)]*[)）]', '', text)  # 去掉后端兜底拼的「（依据攻略：XX）」
    if not keep_source:
        text = re.sub(r'来源：[^\n]*', '', text)  # 去掉 LLM 自带的「来源：《XX》《YY》」标注整行
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = text.replace('**', '')
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


# 反问推销句（引导用户追问不存在的功能，如「需要我帮你规划前7天钻石使用顺序吗？」）：
# 用户口径 2026-09-09 禁止——这类句子会让用户追问系统没有的功能，进而诱发编造。
_UPSELL_Q_RE = re.compile(
    r'^[^\n]*(?:需要我|需要帮你|要不要|想不想|想让我|想不需要|要我(?:帮|来)|'
    r'我可以(?:帮|为)|接下来要不要|用不用我|是否需要我)[^\n]*[？?]\s*$',
    re.M)


def _strip_upsell_question(text: str) -> str:
    """删除回复中的「反问推销」句（整行），防用户追问不存在的功能后 LLM 编造。
    只删命中该句式的行，不动正文；prompt 层已禁止，此处为输出侧兜底。"""
    if not text:
        return text
    t = _UPSELL_Q_RE.sub('', text)
    return re.sub(r'\n{3,}', '\n\n', t).strip()


INDEX_HTML_PATH = os.path.join(BASE_DIR, "web", "index.html")
WEB_DIR = os.path.join(BASE_DIR, "web")  # 前端静态资源目录

# 本次启动标识：注入页面并由心跳回报。页面持有的 ID 与服务当前 ID 不一致，
# 即判定为上次运行遗留的旧页面，前端自动停止响应并尝试关闭
_BOOT_ID = uuid.uuid4().hex[:8]

app = FastAPI(title="拂晓只能小秘书")


# ===== 未捕获异常统一回 JSON（用户口径 2026-10-03）=====
# 背景：控制台/前端都是 JSON 客户端（fetch + res.json()）。此前服务端 500 返回的是
# 纯文本 "Internal Server Error"，前端只能抛「SyntaxError: Unexpected token 'I'」——
# 既看不到真实原因，也不便排障。这里统一转成 {'ok': False, 'error': '...'}，真实堆栈照旧打日志。
# 注：HTTPException（401/403/404 等）有 FastAPI 自己的处理器，不受本处理器影响。
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    import traceback as _tb
    _tb.print_exc()
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {'ok': False, 'error': '%s: %s' % (type(exc).__name__, _mask(exc, _API_TOKEN)),
         'path': str(getattr(request.url, 'path', '') or '')},
        status_code=500,
    )


# ==================== 可选 API Token 鉴权（部署对外时启用） ====================
# 在 .env 配置 AKALI_INVITE_CODE 后：外部访问的 API 请求
# 须带请求头 X-API-Token: xxxx（或 ?token=xxxx），否则 401。
# 页面本体与静态资源（/、/kb/、/avatar/）放行——让用户能直接打开隧道 URL 看到登录界面，
# 在页面内输入 token 后（前端存 localStorage、后续请求带 header）解锁。
# 本地模式（AKALI_RUN_MODE=local）下 Host 为回环时自动放行，方便调试；
# 云端模式一律校验（见下方 _LOOPBACK_HOSTS 说明）。
# 口令留空（默认）则不鉴权（本地个人使用）。
from core import users as _users   # 账号体系：uid / 口令 / 准入总开关，全部由 .env 控制
_API_TOKEN = _users.owner_token()   # 优先级 AKALI_OWNER_TOKEN → AKALI_INVITE_CODE

# 放行路径：页面 / 静态资源 / 认证状态探测（前端据此决定是否弹 token 输入框）
_AUTH_PUBLIC_PREFIXES = ("/kb/", "/avatar/", "/icons/", "/manual-imgs/")
_AUTH_PUBLIC_EXACT = ("/", "/admin", "/api/auth/status", "/api/register",
                      "/api/heartbeat", "/manual")

# 回环地址：仅在**本地模式**下免口令。云端模式下 Host 为 127.0.0.1 只代表
# 「容器网络内的调用方」（同网其他容器、SSH 进来的 shell 等），不代表可信用户，
# 因此云端必须一律校验口令（用户口径 2026-09-24：部署对外时不留免鉴权后门）。
_LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1', '[::1]')


def _token_ok(provided) -> bool:
    """恒定时间比较，避免逐字符比较带来的时序侧信道。

    走 users.safe_eq 而非直接 hmac.compare_digest（用户口径 2026-09-25）：
    后者只接受**纯 ASCII** str，口令含中文（如 AKALI_INVITE_CODE=我是小猪）时会抛
    TypeError，被 except 静默吞成「口令错误」（此为直传 compare_digest 时的历史坑）。
    当前 safe_eq 已 encode 成 bytes 比较，中文口令可正常登录。
    """
    try:
        from core import users as _u
        return _u.safe_eq(provided, _API_TOKEN)
    except Exception:
        return False


@app.middleware("http")
async def api_token_auth(request, call_next):
    if _API_TOKEN:
        # 本机回环免 token：仅本地模式；云端模式一律校验
        host = (request.headers.get('host') or '').split(':')[0]
        is_local = host in _LOOPBACK_HOSTS and not runtime.is_cloud()
        path = request.url.path
        public = path in _AUTH_PUBLIC_EXACT or any(path.startswith(p) for p in _AUTH_PUBLIC_PREFIXES)
        if not is_local and not public:
            # header 优先（推荐）；URL 参数用于分享带口令的链接，代价是会进访问日志，
            # 故云端 nginx 不记录 query string（见 deploy/nginx.conf）
            token = request.headers.get('x-api-token') or request.query_params.get('token')
            # 账号判定（用户口径 2026-09-25）：口令 → uid，并区分「总开关关闭 / 口令无效」，
            # 这样"关掉其他所有用户"时对方能看到明确原因，而不是笼统的 401。
            _res = _users.auth(token)
            if not _res['ok']:
                from fastapi.responses import JSONResponse
                _msg = {
                    'paused': '服务暂未对测试用户开放，请联系管理员',
                    'disabled': '账号已停用，请联系管理员',
                    'bad_token': '口令无效，请检查后重试',
                    'no_token': '需要口令才能使用，请在页面内输入',
                }.get(_res['reason'], '无效或缺失 API Token')
                return JSONResponse({"detail": _msg, "reason": _res['reason']}, status_code=401)
            # 把身份挂到请求上：配额按 uid 统计（core/quota.resolve_identity），管理接口据此鉴权
            request.state.uid = _res['uid']
            request.state.user = _res['user'] or {}
    return await call_next(request)


# ==================== 跨域（云端：网页端 / iframe 嵌入时按白名单放行） ====================
# 默认关闭（同源，本地模式不受影响）；配置 AKALI_ALLOW_ORIGINS=https://a.com,https://b.com 启用。
# 注意：CORS 中间件须在鉴权中间件之后注册（后注册者更外层），否则预检 OPTIONS 会被 401 拦掉。
if runtime.cors_enabled():
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime.allow_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """认证状态探测：前端加载时调用，决定是否显示邀请口令遮罩。

    返回 {auth_required, authenticated, run_mode, panel_store, uid, role, reason}；
    auth_required=False（未配口令）恒放行。uid/role 供前端展示当前身份；
    reason 说明被拒原因（paused=总开关已关闭等），便于用户自查而非只看到 401。
    """
    meta = {"run_mode": runtime.run_mode(), "panel_store": runtime.panel_store(),
            "quota_dim": runtime.quota_key()}
    if not _API_TOKEN:
        return {"auth_required": False, "authenticated": True, **meta}
    host = (request.headers.get('host') or '').split(':')[0]
    if host in _LOOPBACK_HOSTS and not runtime.is_cloud():
        return {"auth_required": True, "authenticated": True, "uid": _users.owner_uid(),
                "role": "owner", "reason": "", **meta}  # 本机免口令（仅本地模式）
    token = request.headers.get('x-api-token') or request.query_params.get('token')
    res = _users.auth(token)
    return {"auth_required": True, "authenticated": res['ok'], **meta,
            "uid": res['uid'] if res['ok'] else '',
            "role": (res['user'] or {}).get('role', '') if res['ok'] else '',
            "allow_other_users": _users.allow_other_users(),
            # 前端据此决定遮罩上显示什么：能注册 → 提示"输入邀请码开始使用"；
            # 关了注册 → 只提示"输入口令"（老用户凭口令进）
            "allow_register": _users.allow_register(),
            "need_invite": bool(_users.register_invite()),
            "reason": res['reason']}


# ==================== 开发者侧（运营/管理）接口 ====================
# 用户口径 2026-09-25：
#   · 运营类内容（用量视图、账号列表…）**集中在开发者侧**，权限由白名单 AKALI_ADMIN_UIDS
#     控制（默认只含开发者本人 000000）；不在白名单里的 uid 一律 403。
#   · **用户相关内容只可读** —— 普通用户只能访问 /api/me（自己的信息），
#     任何编辑类接口都只对开发者侧开放。
def _require_admin(request: Request) -> str:
    """开发者/运营侧闸：uid 必须在 AKALI_ADMIN_UIDS 白名单内。"""
    uid = str(getattr(request.state, 'uid', '') or '')
    if not _users.is_admin(uid):
        raise HTTPException(status_code=403, detail='仅开发者侧账号可操作（需在 AKALI_ADMIN_UIDS 白名单内）')
    return uid


@app.get("/api/admin/accounts")
async def admin_accounts(request: Request):
    """账号与准入配置（含口令）—— 仅开发者侧白名单可调。

    这正是"只有我自己能看见"的实现：口令、开关、可用 uid 列表都在这里回显，
    而 .env 与 users.json 本身只存在于服务器上。
    """
    _require_admin(request)
    return {
        'ok': True,
        'allow_other_users': _users.allow_other_users(),
        'owner_uid': _users.owner_uid(),
        'owner_token': _users.owner_token(),
        'users': _users.list_accounts(),
        'next_uid': _users.next_uid(),
        'admin_uids': sorted(_users.admin_uids()),
        'env_keys': ['AKALI_ALLOW_OTHER_USERS', 'AKALI_OWNER_UID',
                     'AKALI_OWNER_TOKEN', 'AKALI_ADMIN_UIDS', 'AKALI_USERS'],
        'hint': '开关与开发者口令在服务器 .env；测试用户表在 项目根 users.json'
                '（改完即时生效，无需重启）。',
    }


def _kb_cache_stats() -> dict:
    """知识库命中缓存运行统计（拿不到就返回空 dict，不影响用量视图）。"""
    try:
        from core import kb_cache as _kbc
        return _kbc.stats()
    except Exception:
        return {}


@app.get("/api/admin/usage")
async def admin_usage(request: Request, day: Optional[str] = None):
    """**用量视图**（用户口径 2026-10-03）—— **按天看用量、按周期看限额**：

    - **当日**（默认今天；`?day=YYYY-MM-DD` 回看历史日）：取自请求流水 `reqlog`
      （按天分桶，含未走 LLM 的工具类请求）→ 当日请求数 / LLM 轮次 / token；
    - **本周期**（周四→下周四，见 core.quota._period）：取自记账 `usage` → 已用 token
      （输入 / 输出 / 合计）、参考成本、占额度百分比 —— 限额是**周期**口径，
      所以百分比只对「本周期」成立（当日 token 那一列是纯流水，不含额度含义）。

    仅开发者侧白名单可调（用户口径 2026-09-25：运营方面的集中放在开发者侧）。
    维度键在接入账号体系后就是 **uid**（老部署是 IP/session，也能正常显示）。
    """
    _require_admin(request)
    from core import quota as _quota
    d = (day or datetime.date.today().isoformat()).strip()
    period = _quota._period()
    # ① 当日：请求流水（含未走 LLM 的请求，tok=0）
    day_stat = {}
    for e in _quota.recent_requests(d, 500):
        _k = str(e.get('key') or '')
        it = day_stat.setdefault(_k, {'req': 0, 'tok': 0, 'rounds': 0})
        it['req'] += 1
        _t = int(e.get('tok') or 0)
        it['tok'] += _t
        if _t > 0:
            it['rounds'] += 1
    # ② 本周期：记账口径（限额 / 占额度 / 成本 / 调用细则）
    cur = {r['key']: r for r in _quota.usage_snapshot(period)}
    # 日期下拉：有请求流水的日子（按天）+ 本周期起始日（供周期视图回看）
    dates = sorted(
        {k for k in _quota.STORE.reqlog.keys() if isinstance(k, str)} | {period},
        reverse=True,
    )
    # 把 uid 对应的人名补上，便于一眼看出是谁在用
    name_of = {u['uid']: u.get('name') or '' for u in _users.list_accounts()}
    owner = _users.owner_uid()
    rows = []
    for _k in set(day_stat) | set(cur) | {owner}:
        if not _k:
            continue
        p = cur.get(_k) or {}
        ds = day_stat.get(_k) or {}
        rows.append({
            'key': _k, 'name': name_of.get(_k, ''), 'is_owner': (_k == owner),
            'plan': p.get('plan') or _quota.get_user_plan(_k),
            'limit': p.get('limit') if p else _quota.daily_limit(_k),
            # 当日（请求流水）
            'req_count': ds.get('req', 0),
            'day_tok': ds.get('tok', 0),
            'day_rounds': ds.get('rounds', 0),
            # 本周期（记账）
            'rounds': p.get('rounds', 0),
            'tok_in': p.get('tok_in', 0), 'tok_out': p.get('tok_out', 0),
            'tok_total': p.get('tok_total', 0), 'cost': p.get('cost', 0.0),
            'calls': p.get('calls') or {},
        })
    # 开发者本人**始终置顶**（用户口径 2026-09-25）：即便当天零用量、不在记录里也保留一行，
    # 保证开发者始终可见（owner 已在上面并入键集合，这里只需排序）。
    rows.sort(key=lambda x: (0 if x['key'] == owner else 1, -x['tok_total'], -x['day_tok']))
    total = sum(r['tok_total'] for r in rows)         # 本周期合计（限额口径）
    cost = round(sum(r['cost'] for r in rows), 4)     # 本周期参考成本
    day_total = sum(r['day_tok'] for r in rows)       # 当日合计（请求流水口径）
    # 请求级明细（按天持久化，token=0 表示该请求未走 LLM）：供控制台「请求明细」表
    recent = _quota.recent_requests(d, 500)
    for e in recent:
        e['name'] = name_of.get(e['key'], '')
    return {'ok': True, 'day': d, 'period': period, 'dim': runtime.quota_key(),
            'rows': rows, 'total_tok': total, 'total_cost': cost, 'day_tok': day_total,
            'recent': recent,
            'backend': type(_quota.STORE).__name__, 'dates': dates,
            # 知识库命中缓存概况（用户口径 2026-10-03）：命中率 / 条目数 / 开关
            'kb_cache': _kb_cache_stats()}


@app.post("/api/register")
async def register(request: Request):
    """用户端**首次打开自动注册**（用户口径 2026-09-25）。

    免鉴权：它本来就是用来"换取口令"的入口。流程 ——
      ① 若该请求 device_id 已有账号 → 直接返回原账号（老用户/老设备无感，不必重注册）
      ② 否则：注册开关须打开，且（配了邀请码时）邀请码须正确
      ③ 用户须自填 8 位 uid（请求体 {"user_uid": "..."}，首位 1/3/6/8、全局唯一）；
         通过后**存 user_uid** + 分配内部顺序 uid（000001 起，仅存储/排序）+ 生成口令，写回 users.json
    邀请码来源：请求体 {"invite": "..."} 或 URL ?invite=...（便于发带码的链接）。
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    invite = str(body.get('invite') or request.query_params.get('invite') or '').strip()
    name = str(body.get('name') or '').strip()
    device_id = str(body.get('device_id') or '').strip()
    user_uid = str(body.get('user_uid') or '').strip()   # 用户自填的 8 位 uid（新设备必填）
    from core import quota as _quota
    ip = _quota._client_ip(request)        # 仅记录，不参与身份识别

    # 新设备才需要过闸：老设备（device_id 已登记）直接回位，不看邀请码。
    # 注意这里**只认 device_id，不认 IP**（用户口径 2026-09-25 公网实测发现）：
    # 若做 IP 兜底，同一出口 IP（同 WiFi/同运营商 NAT）下第二台设备会被当成
    # "老设备"而跳过邀请码 → 入场券形同虚设。IP 仅用于记录与排障。
    if not _users.find_by_device(device_id):
        if not _users.allow_register():
            from fastapi.responses import JSONResponse
            return JSONResponse({'ok': False, 'reason': 'register_off',
                                 'detail': '暂未开放注册，请联系管理员'}, status_code=403)
        if _users.register_invite() and not _users.invite_ok(invite):
            from fastapi.responses import JSONResponse
            return JSONResponse({'ok': False, 'reason': 'bad_invite',
                                 'detail': '邀请码无效'}, status_code=403)

    res = _users.register(device_id, ip, name, user_uid)
    if not res.get('ok'):
        from fastapi.responses import JSONResponse
        _detail = {
            'bad_uid': '请填写 8 位数字的 uid（首位须为 1/3/6/8）',
            'uid_taken': '该 uid 已被占用，请换一个',
            'register_off': '暂未开放注册，请联系管理员',
        }.get(res.get('reason'), '注册失败')
        return JSONResponse({'ok': False, 'reason': res.get('reason') or 'error',
                             'detail': _detail}, status_code=403)
    return res


@app.get("/admin")
async def admin_console_page():
    """开发者控制台（运营侧集中入口，用户口径 2026-09-25）。

    页面本身不含数据，故加入免鉴权名单也无害（放在 _AUTH_PUBLIC_EXACT）；
    真正的数据都由 /api/admin/* 提供，那些接口只认 AKALI_ADMIN_UIDS 白名单。
    """
    from admin_console import admin_page
    return admin_page()


@app.get("/api/me")
async def my_profile(request: Request):
    """**只读的"我的信息"**（用户口径 2026-09-25：用户相关的内容只可读）。

    返回当前身份 + 自己今天的用量。普通用户可用；不存在任何编辑类动作。
    """
    uid = str(getattr(request.state, 'uid', '') or '')
    if not uid:
        raise HTTPException(status_code=401, detail='需要口令才能使用')
    from core import quota as _quota
    _acct = _users.get_account(uid) or {}
    return {'ok': True, 'readonly': True, 'uid': uid,
            'user_uid': _acct.get('user_uid') or '',
            'name': _acct.get('note') or '',
            'role': 'owner' if uid == _users.owner_uid() else 'user',
            'is_admin': _users.is_admin(uid),
            'allow_other_users': _users.allow_other_users(),
            # 用户侧只给**剩余百分比**，不含具体 token/成本/轮次（用户口径 2026-09-25：
            # 用户只能查看自己每天的剩余用量百分比，看不到具体的。具体量只在开发者侧
            # /api/admin/usage）。此处用 usage_brief 而非 usage_of。
            'usage': _quota.usage_brief(uid)}


@app.get("/api/me/panel")
async def my_panel_get(request: Request):
    """**面板云端备份（读）**：返回当前 uid 的面板备份（本地缓存丢失时回填用）。

    用户口径 2026-10-03：云端只作**备份**、本地为主 —— 前端保存面板时推一份上来，
    本地缓存为空（换设备 / 清了缓存）时才回填；本地有数据则不动。
    """
    uid = str(getattr(request.state, 'uid', '') or '')
    if not uid:
        raise HTTPException(status_code=401, detail='需要口令才能使用')
    from data import panel_backup as _pb
    return {'ok': True, 'panels': _pb.load(uid)}


@app.post("/api/me/panel")
async def my_panel_save(request: Request):
    """**面板云端备份（写）**：保存当前 uid 的面板备份（客户端保存面板时推一份）。"""
    uid = str(getattr(request.state, 'uid', '') or '')
    if not uid:
        raise HTTPException(status_code=401, detail='需要口令才能使用')
    try:
        body = await request.json()
    except Exception:
        body = None
    panels = body.get('panels') if isinstance(body, dict) else body
    if not isinstance(panels, dict):
        from fastapi.responses import JSONResponse
        return JSONResponse({'ok': False, 'error': '请求体须为面板对象'}, status_code=400)
    from data import panel_backup as _pb
    return {'ok': bool(_pb.save(uid, panels))}


# 注入三无秘书人设
agent = AgentApp(prompt_template=WEB_PERSONA_PROMPT)

# LLM token 统一记账（用户口径 2026-09-26：所有消耗 LLM API 的 token 都要计算）：
# 在 openai SDK 层挂钩一次，覆盖闲聊主链（含工具循环每轮）、工作路由与内部增强
# 的全部调用，读响应真实 usage，按请求配额键记入 core.quota。
from core import llm_accounting as _llm_accounting
_llm_accounting.install()


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None  # 会话标识（多人接口调用时隔离对话状态）；缺省用 "default"
    uid: Optional[str] = None  # 由 /api/chat 路由注入（鉴权中间件解析出的 uid），配额按它统计
    # 云端模式（AKALI_RUN_MODE=cloud）用户面板随请求携带（服务器无用户文件、无状态）：
    # {角色名: {"attack_panel": {...}, "defense_panel": {...}}}，绝对赋值（同 user_data.apply_panel_to 语义）
    panel_overrides: Optional[dict] = None


class LocationRequest(BaseModel):
    lat: Optional[float] = None
    lon: Optional[float] = None


@app.post("/api/location")
async def set_location(req: LocationRequest):
    """接收浏览器定位上报（用户授权后由前端提交经纬度），供 query_weather 查询实时天气。"""
    if req.lat is None or req.lon is None:
        agent.user_location = None
    else:
        agent.user_location = {"lat": req.lat, "lon": req.lon}
    return {"ok": True, "location": agent.user_location}





@app.get("/manual")
def manual_page():
    """使用者手册网页版（静态副本）：首次注册弹窗与聊天「使用说明」关键词共用。
    公开路由（不挂 token）：首次注册尚未登录，iframe 须可加载。"""
    target = os.path.join(WEB_DIR, "manual.html")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="手册不存在")
    return FileResponse(target, media_type="text/html",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manual-imgs/{name:path}")
def manual_img(name: str):
    """手册内嵌图片（web/manual-imgs/**）：公开静态资源，仅做目录越权防护。"""
    base = os.path.realpath(os.path.join(WEB_DIR, "manual-imgs"))
    target = os.path.realpath(os.path.join(base, name))
    if not target.startswith(base) or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = os.path.splitext(target)[1].lower()
    _media = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
              '.webp': 'image/webp', '.gif': 'image/gif'}
    media = _media.get(ext, 'application/octet-stream')
    return FileResponse(target, media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icons/{name:path}")
def pwa_icon(name: str):
    """PWA / favicon / 节庆图标（web/icons/**）：

    供「安装到桌面（PC 独立窗口 / 手机主屏）」、浏览器标签页，以及节庆皮肤
    （如 festival/midautumn-ring.png）使用。支持子目录；用规范化路径 + 前缀校验
    挡掉路径穿越，并只放行常见图片扩展名。"""
    root = os.path.normpath(os.path.join(WEB_DIR, "icons"))
    rel = (name or '').replace('\\', '/').lstrip('/')
    target = os.path.normpath(os.path.join(root, rel))
    if not target.startswith(root + os.sep) or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="图标不存在")
    _media = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
              '.webp': 'image/webp', '.svg': 'image/svg+xml',
              '.ico': 'image/x-icon'}.get(os.path.splitext(target)[1].lower())
    if not _media:
        raise HTTPException(status_code=404, detail="图标不存在")
    return FileResponse(target, media_type=_media)


@app.get("/")
async def index():
    """返回聊天页面（初始问候语按当前时间动态生成，含随机变体）。
    各类提醒（礼包/活动截止/远征/邮件）**统一在顶部事务提醒横幅轮播播放**，
    不再织入问候气泡（用户口径 2026-09-09）。"""
    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    greet = time_greeting()
    # 问候语结构：第 1 行为问候句，其余为功能引导句（纯问候，不含任何提醒）
    greet_lines = greet.split("\n", 1)
    greeting_line = greet_lines[0]
    cap_line = greet_lines[1] if len(greet_lines) > 1 else ""
    greet = greeting_line + (("\n" + cap_line) if cap_line else "")

    # 法定节假日祝福：**融入**开场问候（用户口径 2026-09-14 起随问候气泡出现；
    # 2026-09-25 改为融合 —— 直接用配置里的 opening 把「祝福 + 放假安排 + 承接词」
    # 说成一整句，不再拼「🎉 节日名｜祝福」这种通知式前缀，也不再把放假安排单列）。
    # 承接词（"……对了，"）写在各节日的 opening 里，让祝福顺着固定开场白自然带出，
    # 而不是硬接在功能引导后面；未配置 opening 的旧数据由下面补默认承接。
    # 仅当今天落在法定节假日区间内才追加；普通日子初始问候完全不变。
    _HOL_BRIDGE = "……对了，"      # 兜底承接词（同 opening 的写法约定）
    def _esc_hol(s: str) -> str:
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    try:
        from core.calc_flow import _today_holiday
        _hol = _today_holiday()
        if _hol:
            # opening 已含承接词与放假安排；未配置该字段的节日退回 greeting（必要时补 summary），
            # 并补上默认承接词，保证旧数据也不突兀、不用特判。
            _hline = str(_hol.get('opening') or '').strip()
            if not _hline:
                _hline = str(_hol.get('greeting') or '').strip()
                _hsum = str(_hol.get('summary') or '').strip()
                if _hline and _hsum and _hsum not in _hline:
                    _hline = _hline.rstrip('。') + "。" + _hsum
                if _hline and not _hline.startswith('…'):
                    _hline = _HOL_BRIDGE + _hline
            if _hline:
                if not _hline.endswith(('。', '！', '？', '~', '～')):
                    _hline += "。"
                greet = greet.rstrip() + "\n" + _esc_hol(_hline)
    except Exception:
        pass

    def _esc_tip(s: str) -> str:
        return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('\n', ' '))

    # 顶部事务提醒横幅（轮播，统一入口）：
    # ① 邮件领取（每次会话固定出现，✉ 信封印记）
    # ② 限时远征（18:00–23:59 必出，⚔ 印记）
    # ③ 活动日历提醒（📅 日历印记，来自 reminder.collect_reminders：每周/公众号/月度礼包、游戏内活动截止）
    # 整块由技能包 reminder 控制：提醒是后端确定性生成的、不经工具，所以开关只能在
    # 消费方这里判断一次（AKALI_AGENT_SKILLS=-reminder 即完全不注入提醒横幅）。
    tip_items = []
    if _skill_enabled('reminder'):
        for _affair in _affair_hints():
            _badge = '✉ 邮件' if '邮件' in _affair else '⚔ 远征'
            tip_items.append(f'<span class="tip-item"><span class="tip-badge">{_badge}</span>'
                             f'{_esc_tip(_affair)}</span>')
        try:
            from core.reminder import collect_reminders
            for _line in collect_reminders():
                tip_items.append(f'<span class="tip-item"><span class="tip-badge">📅 日历</span>'
                                 f'{_esc_tip(_line)}</span>')
        except Exception:
            pass
    html = html.replace('<!--AFFAIR_TIP-->', ''.join(tip_items))
    # 替换静态初始问候为时间问候（问候文本为服务端固定文案，无 HTML 特殊字符，
    # 换行直接转 <br> 保证气泡内按语义换行显示；功能菜单按钮为静态节点不受替换影响）
    html = html.replace(
        "噢，指挥官，阿卡莉随时待命。直接告诉我你要做什么",
        greet.replace("\n", "<br>"),
    )
    # 注入本次启动标识：页面据此与心跳回报对比，识别上次遗留的旧页面
    html = html.replace("__AKALI_BOOT_ID__", _BOOT_ID)
    # 注入资源根：云端走 CDN 直链，本地留空（资源仍由 /kb/ /avatar/ 路由 serve）
    import json as _json
    html = html.replace(
        "</head>",
        '<script>window.ASSET_BASE=' + _json.dumps(CDN_BASE) + ';</script></head>',
        1,
    )
    # 云端模式：重写静态 HTML 内的 /kb/（排除 /kb_html/）与 /avatar/ 为 CDN 直链，
    # 覆盖 CSS 背景图、静态 <img>、JS 常量等；运行时由 API 返回的图片由前端 resolveAsset 处理
    if CDN_BASE:
        import re as _re
        base = CDN_BASE.rstrip("/")
        html = _re.sub(r"/kb/(?!html)([^\"' )]*)", lambda m: base + "/" + m.group(1), html)
        html = _re.sub(r"/avatar/([^\"' )]*)", lambda m: base + "/图片素材/" + m.group(1), html)
    return HTMLResponse(html)


@app.get("/kb/{file_path:path}")
async def kb_file(file_path: str):
    """知识库静态文件：攻略 docx/png/xlsx 等，供前端打开查看（限制在知识库目录内）。"""
    target = os.path.normpath(os.path.join(KB_STATIC_DIR, file_path))
    if not target.startswith(KB_STATIC_DIR) or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@app.get("/avatar/{file_path:path}")
async def avatar_file(file_path: str):
    """头像素材库静态文件（阿卡莉1.0/知识库/图片素材/头像 与 头像（改）/）：查询角色时前端渲染头像。"""
    target = os.path.normpath(os.path.join(AVATAR_STATIC_DIR, file_path))
    if not target.startswith(AVATAR_STATIC_DIR) or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@app.get("/api/guides")
async def list_guides():
    """攻略索引库：合并 guide_index.json（可下载文件）与 拂晓攻略知识库_精简版.json（简介）。
    索引由 FX数据管理工具 build_guide_index.py 生成/更新（含 --watch 实时同步）；智能体纯只读。
    前端攻略库面板据此渲染：标题+简介+点击可预览/下载正本。"""
    index_file = os.path.join(KB_STATIC_DIR, "攻略", "guide_index.json")
    kb_json_path = os.path.join(KB_STATIC_DIR, "拂晓攻略知识库_精简版.json")
    guides = []
    updated = ""
    if os.path.exists(index_file):
        try:
            with open(index_file, encoding="utf-8") as f:
                data = json.load(f)
            guides = data.get("guides", [])
            updated = data.get("updated", "")
        except Exception:
            guides = []
    # 合并 JSON 知识库简介（按标题匹配，JSON 标题 ↔ 文件标题）
    summary_by_title = {}
    if os.path.exists(kb_json_path):
        try:
            with open(kb_json_path, encoding="utf-8") as f:
                kb = json.load(f).get("knowledge_base", {})
            for d in kb.get("documents", []):
                summary_by_title[d["title"]] = d.get("summary", "")
        except Exception:
            pass
    for g in guides:
        title = g.get("title", "")
        sm = summary_by_title.get(title)
        if sm is None:
            for t, s in summary_by_title.items():
                if t in title or title in t:
                    sm = s
                    break
        if sm:
            g["summary"] = sm
    # 与实时文档同步：剔除本地文件不存在的条目（被删的文件不再出现在面板）
    guides = [g for g in guides
              if (g.get("path") or "").strip()
              and os.path.isfile(os.path.normpath(os.path.join(KB_STATIC_DIR, g["path"])))]
    # 同标题去重：保留 攻略/ 目录下的正本，剔除根目录副本（如 争锋竞技场简易百科）
    seen_title = {}
    for g in guides:
        t = g.get("title", "").strip()
        p = g.get("path", "").strip()
        if not t:
            continue
        if t in seen_title:
            # 已有同名：保留路径含"攻略/"的正本，替换掉根目录副本
            cur = seen_title[t]
            if p.startswith("攻略/") and not cur.get("path", "").startswith("攻略/"):
                seen_title[t] = g
        else:
            seen_title[t] = g
    guides = list(seen_title.values())
    return {"guides": guides, "updated": updated}


def _docx_to_html_stdlib(path: str) -> str:
    """纯标准库 docx→HTML 回退解析（zipfile+xml，不依赖 python-docx）。
    docx 本质是 zip，正文在 word/document.xml；提取 <w:p> 段落与 <w:tbl> 表格文本。"""
    import zipfile
    import xml.etree.ElementTree as ET
    NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    parts = []
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml')
    root = ET.fromstring(xml)
    for el in root.iter():
        if el.tag == f'{{{NS["w"]}}}p':
            texts = [t.text or '' for t in el.iter(f'{{{NS["w"]}}}t')]
            line = ''.join(texts).strip()
            if line:
                parts.append(f'<p>{line}</p>')
        elif el.tag == f'{{{NS["w"]}}}tbl':
            rows = []
            for tr in el.iter(f'{{{NS["w"]}}}tr'):
                cells = []
                for tc in tr.iter(f'{{{NS["w"]}}}tc'):
                    ctexts = [t.text or '' for t in tc.iter(f'{{{NS["w"]}}}t')]
                    cells.append(''.join(ctexts).strip())
                rows.append("<tr>" + "".join(f"<td>{c}</td>" if c else "<td></td>" for c in cells) + "</tr>")
            parts.append("<table border='1' cellspacing='0' cellpadding='4' style='border-collapse:collapse;margin:8px 0'>"
                         + "".join(rows) + "</table>")
    body = "".join(parts) or "<p>（文档无正文内容）</p>"
    return body


@app.get("/api/ship_story")
async def get_ship_story_api(name: str = ""):
    """查询角色原型舰历史故事（原型舰历史.json 只读）。前端「历史上的故事」按钮调用。"""
    name = (name or "").strip()
    if not name:
        return {"found": False}
    story = agent.app.get_ship_story(name)
    if not story:
        return {"found": False}
    return {"found": True, "story": story}


# ==================== 查角色名录（用户端悬浮球「查角色」面板） ====================
# 与开发者端「角色查询」同口径：全角色名单 + 阵营筛选 + 头像。
# 数据源：raw_data.json（底层信息表，只读）+ 头像素材库（tools.query.find_avatar）。
# 结果不再以文本塞进对话回复，改为前端面板展示（用户口径 2026-09-13）。
_SHIP_ROSTER_CACHE = {"mt": None, "rows": []}

# 改造舰「别名直存」反查缓存（见 _retrofit_canon）
_RETRO_CANON_CACHE = {"mt": None, "set": set(), "base": {}}


# 稀有度排序档位（从高到低）：彩 > 金 > 紫 > 蓝 > 绿 > 白
_RARITY_ORDER = {"彩": 0, "金": 1, "紫": 2, "蓝": 3, "绿": 4, "白": 5}


def _rarity_rank(r: str) -> int:
    return _RARITY_ORDER.get((r or "").strip(), 99)


def _retrofit_canon() -> tuple:
    """从昵称表反查改造舰：底层数据中改造后角色有两种存储形态——
    ① 名含「·改」后缀直存（如 胡德·改）；② 以别名当正规名直存（如 丹阳/信赖/埃罗芒什，
    其 ship_nicknames 别名含「雪风改/响改/巨像改」）。

    本函数返回 (canon_set, base_map)：canon_set = 形态②的正规名集合；
    base_map = 该正规名 → 改造前 base 名（最长 改 别名去后缀）。
    形态①由调用方用 name.endswith('·改') 兜底。
    """
    from core.name_service import load_nicknames, DEFAULT_NICKNAMES_PATH
    try:
        mt = os.path.getmtime(DEFAULT_NICKNAMES_PATH)
    except OSError:
        mt = 0.0
    if _RETRO_CANON_CACHE["mt"] == mt and _RETRO_CANON_CACHE["set"]:
        return _RETRO_CANON_CACHE["set"], _RETRO_CANON_CACHE["base"]
    nick = load_nicknames()
    canon, base = set(), {}
    for k, v in nick.items():
        if k.startswith("_"):
            continue
        als = v if isinstance(v, list) else [v]
        hit = [a for a in als if a.endswith("改")]
        if hit:
            canon.add(k)
            pick = max(hit, key=len)  # 优先取真实存在的 base（如 巨像·改 > 巨人·改）
            base[k] = pick[:-2] if pick.endswith("·改") else pick[:-1]
    _RETRO_CANON_CACHE["mt"] = mt
    _RETRO_CANON_CACHE["set"] = canon
    _RETRO_CANON_CACHE["base"] = base
    return canon, base


def _ship_roster() -> dict:
    """角色名录（按稀有度从高到低排序），带 mtime 缓存。

    返回结构：
      - ships:     普通版（改造前）名录；有改造的角色以改造前（base）展示，
                   改造后「·改」角色不在此列（用户口径 2026-09-14）。
      - retrofit:  改造后角色独立分组，同样按稀有度排序，供前端「改造界面」单独、放大展示。
                   含两种形态：① 名含「·改」后缀（如 胡德·改）；
                   ② 别名直存（如 丹阳/信赖/埃罗芒什，其昵称别名含 改，见 _retrofit_canon）。
      - by_nation: 普通版按阵营分桶（保留阵营筛选能力）。
    每个条目额外带 is_retrofit / base_name 标记。
    """
    from tools.query import find_avatar
    from data.app import RAW_DATA_FILE
    mt = None
    try:
        mt = os.path.getmtime(RAW_DATA_FILE)
    except OSError:
        pass
    if _SHIP_ROSTER_CACHE["mt"] == mt and _SHIP_ROSTER_CACHE["rows"]:
        return _SHIP_ROSTER_CACHE["rows"]
    retro_canon, retro_base = _retrofit_canon()
    main, retrofit = [], []
    for name in sorted((agent.app.raw_storage or {}).keys()):
        # 信息不全的角色等同「未实装」：缺面板/技能描述/普攻/天赋任一项即不出现
        # （用户口径 2026-09-14：缺任何基础信息都不得查询与计算），与 query_ship_info 同口径。
        try:
            if not agent.app.is_ship_complete(name):
                continue
        except Exception:
            continue
        panel = ((agent.app.raw_storage.get(name) or {}).get("panel") or {})
        row = {
            "name": name,
            "nation": panel.get("所属", "") or "未知阵营",
            "ship_type": panel.get("舰种", "") or "",
            "rarity": panel.get("稀有", "") or "",
            "avatar": find_avatar(name),
        }
        if name.endswith("·改") or name in retro_canon:
            # 改造后角色（形态① 名含「·改」；形态② 别名直存如 丹阳/信赖/埃罗芒什）：
            # 归入独立分组，标记 base 名（改造前）。
            row["is_retrofit"] = True
            if name.endswith("·改"):
                row["base_name"] = name[:-2]
            else:
                row["base_name"] = retro_base.get(name, "")
            retrofit.append(row)
        else:
            row["is_retrofit"] = False
            main.append(row)
    # 普通版与改造版均按 稀有度(高→低) + 名字 稳定排序
    main.sort(key=lambda s: (_rarity_rank(s["rarity"]), s["name"]))
    retrofit.sort(key=lambda s: (_rarity_rank(s["rarity"]), s["name"]))
    by_nation = {}
    for s in main:
        n = s["nation"] or "未知阵营"
        by_nation.setdefault(n, []).append(s)
    result = {"ships": main, "retrofit": retrofit, "by_nation": by_nation}
    _SHIP_ROSTER_CACHE["mt"] = mt
    _SHIP_ROSTER_CACHE["rows"] = result
    return result


@app.get("/api/ships")
async def api_ships():
    """角色名录：普通版 + 改造版（独立分组），均按稀有度从高到低排序；
    供前端「查角色」面板（阵营筛选 + 稀有度分档 + 改造界面）。"""
    return _ship_roster()


@app.get("/api/ship_detail")
async def api_ship_detail(name: str = ""):
    """单角色详情：面板数值 + 天赋 + 技能描述原文，附头像与是否有原型史。
    复用 query_ship_info 工具（唯一口径，不重写格式化），剥离标记后交前端面板渲染。"""
    name = (name or "").strip()
    if not name:
        return {"found": False}
    canon = _canon_ship_name(name)
    from tools.query import QueryShipInfo, find_avatar
    try:
        text = QueryShipInfo(cfg={"app": agent.app}).call({"ship_name": canon})
    except Exception:
        text = ""
    text = text if isinstance(text, str) else str(text or "")
    if not text or text.startswith("未找到角色"):
        return {"found": False, "name": canon}
    # 头像/故事标记在面板内单独渲染，正文里剥掉
    text = _strip_avatar_marker(text)
    text = re.sub(r'\n?<<STORY>>[^\n]*', '', text).strip()
    return {
        "found": True,
        "name": canon,
        "text": text,
        "avatar": find_avatar(canon),
        "story": bool(agent.app.get_ship_story(canon)),
    }


@app.get("/kb_html/{file_path:path}")
async def kb_html(file_path: str):
    """docx 攻略网页预览：后端把 docx 转 HTML（纯本地，零 token / 零 LLM 开销）。
    优先 python-docx（含表格样式）；缺库时用纯标准库回退（zipfile+xml 提取段落/表格）。"""
    target = os.path.normpath(os.path.join(KB_STATIC_DIR, file_path))
    if not target.startswith(KB_STATIC_DIR) or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = os.path.splitext(target)[1].lower()
    if ext not in (".docx", ".doc"):
        # 非 docx（图片/xlsx）直接走 /kb/ 原样返回
        return FileResponse(target)
    try:
        from docx import Document
        doc = Document(target)
        parts = []
        for p in doc.paragraphs:
            t = p.text.strip()
            if t:
                parts.append(f"<p>{t}</p>")
        for table in doc.tables:
            parts.append("<table border='1' cellspacing='0' cellpadding='4' style='border-collapse:collapse;margin:8px 0'>")
            for row in table.rows:
                cells = [f"<td>{c.text.strip()}</td>" if c.text and c.text.strip() else "<td></td>" for c in row.cells]
                parts.append("<tr>" + "".join(cells) + "</tr>")
            parts.append("</table>")
        body = "".join(parts) or "<p>（文档无正文内容）</p>"
    except Exception:
        # python-docx 缺失/失败 → 纯标准库回退（任何环境都能预览）
        try:
            body = _docx_to_html_stdlib(target)
        except Exception:
            body = f"<p>（docx 解析失败，请直接下载打开）</p>"
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{os.path.basename(target)}</title>
<style>body{{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;max-width:880px;margin:0 auto;padding:20px;line-height:1.7;color:#2b2f36;}}
h1{{font-size:18px;border-bottom:1px solid #e5e8ec;padding-bottom:8px;}}table{{width:100%;font-size:13px;}}td{{padding:4px 8px;}}
.raw-link{{display:inline-block;margin-top:12px;font-size:13px;color:#6b8caa;}}</style></head>
<body><h1>{os.path.basename(target)}</h1>{body}
<p><a class="raw-link" href="/kb/{file_path}" target="_blank">下载原始文档</a></p></body></html>"""
    return HTMLResponse(html)


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
    pvp_file = os.path.join(KB_STATIC_DIR, "talent_pvp.json")
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

# ===== 顶层输入路由：时间问好 / 会话状态注入 / 再见退出 =====

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def time_greeting() -> str:
    """按当前小时生成问候语（页面初始欢迎语，含阿卡莉口吻与随机变体）。"""
    import random
    h = datetime.datetime.now().hour
    if 5 <= h < 9:
        period = "早上好"
    elif 9 <= h < 12:
        period = "上午好"
    elif 12 <= h < 14:
        period = "中午好"
    elif 14 <= h < 18:
        period = "下午好"
    elif 18 <= h < 23:
        period = "晚上好"
    else:
        period = "夜深了"
    guide_line = "右上方的悬浮窗提供了一些工具。您也可以自由输入，我会尽力为您提供帮助。"
    variants = [
        f"{period}，指挥官。阿卡莉随时待命。\n{guide_line}",
        f"{period}，指挥官。今天也要推进舰队的工作吗？\n{guide_line}",
        f"{period}，指挥官。我在。\n{guide_line}",
        f"{period}，指挥官。难得的清闲。\n{guide_line}",
        f"{period}，指挥官。阿卡莉在。\n{guide_line}",
        f"{period}，指挥官。嗯，我在听。\n{guide_line}",
    ]
    return random.choice(variants)


def _session_state(overrides: dict = None) -> str:
    """从会话级临时记忆推断当前进行中的流程（供 LLM 判断功能切换确认）。"""
    overrides = overrides or {}
    ctx = overrides.get("calc_context") or {}
    if ctx.get("attacker"):
        enemy = ctx.get("enemy") or "未指定"
        return f"正在进行伤害计算（攻击方：{ctx['attacker']}，敌方：{enemy}）"
    if overrides.get("buffs") or overrides.get("attack_panel") or overrides.get("defense_panel") or overrides.get("enemy"):
        return "存在已设置的临时计算参数（尚未完成一轮计算）"
    return "无进行中任务"


def _build_user_message(raw: str, hist: Optional[list] = None, overrides: dict = None) -> str:
    """给用户输入注入当前时间与会话状态（仅用于本次调用，不入历史存档）。
    含「会话起点」标记：hist 为空=新会话，否则=续聊（仅作状态参考，与问候无关——
    页面已有欢迎语，任何轮次都不问候）。hist 传入会话级历史，多会话互不串。"""
    now = datetime.datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M") + " " + _WEEKDAYS[now.weekday()]
    h = now.hour
    if 7 <= h < 11 or (h == 11 and now.minute < 30):
        period, tone = "早晨", "冷静、可轻度调侃；感情回应克制但必须正面"
    elif (h == 11 and now.minute >= 30) or 12 <= h < 14:
        period, tone = "中午", "轻松、自然；感情回应坦率且不回避"
    elif 14 <= h < 19:
        period, tone = "下午", "傲娇、嘴上别扭但行动默许；感情回应仍要明确积极"
    else:
        period, tone = "晚上", "娇羞、柔和、默许强；对喜欢/爱意要更坦率地正面回应"
    is_new = "是" if not hist else "否"
    return (f"[当前时间：{time_str}] [当前时段：{period}] [时段语气硬约束：{tone}] "
            f"[会话状态：{_session_state(overrides)}] [会话起点：{is_new}] {raw}")


# ===== Token 用量监控（服务控制台日志，按轮打印） =====
# 中文为主文本估算校准：实测 system_prompt 4029 字 ≈ 2837 token → 比值 0.70
_TOKEN_EST_RATIO = 0.70
_session_token_total = {}  # session_id -> 累计 token


def _est_tokens(text: str) -> int:
    return max(1, int(len(text or '') * _TOKEN_EST_RATIO))


# 再见/退出表达（命中即清空会话与记忆并告别）
_BYE_PATTERN = re.compile(
    r'(再见|拜拜|bye|退出|退出会话|结束会话|结束本轮|告辞|后会有期|晚安|走了|88)',
    re.IGNORECASE,
)
# 「88」既是告别口语（拜拜），也是游戏内氪金档位（88 自选 / 88 礼包）——
# 命中 88 且正文含下列游戏内容词时不算告别（否则「88自选选谁」会被当拜拜直接清会话）。
_BYE_88_GUARD = re.compile(
    r'自选|礼包|保底|氪金|充值|档位|建造|图纸|改造|装备|练级|配装|攻略|查询|伤害|面板|天赋|技能')
_BYE_REPLY = "噢，指挥官，随时待命。已为您清空本轮对话与临时记忆。"


def _random_exit_task_reply() -> str:
    """「中断当前任务」的告别语（随机）。"""
    return random.choice([
        "好，进行下一项工作。",
        "明白了，这项先放下。",
        "好，先搁置。接下来要做什么？",
        "行吧，听你的。下一项？",
        "知道了，已中断。还有别的吩咐吗？",
    ])


def _random_exit_chat_reply() -> str:
    """「退出会话（纯聊天）」的告别语（随机）。"""
    return random.choice([
        "噢，指挥官，再见，注意休息。",
        "嗯，那我先告退。有事再叫我。",
        "慢走，指挥官。有事随时吩咐。",
        "知道了，我先去整理数据。下次见。",
        "再见。别太想我。",
    ])


# 闲聊轮次上限：单次最多 4 轮连续闲聊，之后后端强制拉回工作（不调 LLM，省 token 且可预期）
_CHITCHAT_MAX_ROUNDS = 4
# 调情语境轮次上限（与 system_prompt.md 提示词一致：调情最多 20 轮）；分开计数，不占普通闲聊轮次
_FLIRT_MAX_ROUNDS = 20
# 第 20 轮仍正常回应，但作为最后一轮自然收尾；第 21 次连续调情才进入「待机中」。
_FLIRT_FINISH_ROUND = 20


def _random_work_pull_reply() -> str:
    """连续闲聊超过上限后的拉回话术（傲娇式、简洁、自称「我」）。"""
    return random.choice([
        "聊得够久了，正事还等着呢。先去工作。",
        "闲聊到此为止。舰队的事还没做完，先去工作吧。",
        "哼，闲话时间结束。练度不会自己涨，先去工作。",
        "指挥官，该干活了。推图、配装还是算伤害？先去工作再说。",
        "休息够了就开工。先去工作，有事再叫我。",
    ])


def _is_night() -> bool:
    """晚间/夜间判定：19:00–次日 04:59 统一视为「晚上」（与 system_prompt 时段口径一致：
    该时段闲聊不主动带工作、满轮收尾提示休息）。"""
    h = datetime.datetime.now().hour
    return h >= 19 or h < 5


def _random_night_rest_reply() -> str:
    """夜间连续闲聊超过上限后的收尾话术：不追问任何工作/游戏正事，提示休息放松。"""
    return random.choice([
        "都这个点了，指挥官也该歇着了。早点休息，明天再聊。",
        "夜深了，港区都安静了。去睡吧，工作明天我陪你做。",
        "不早了，先放下正事休息吧。养好精神，明天再说。",
        "这么晚还在聊，身体要紧。去休息吧，指挥官。",
        "该休息了。正事不差这一晚，明天我还在。",
    ])


# ===== 事务类提醒（用户口径 2026-09-09 最终修订）=====
# 邮件提醒：**固定事务提醒项**——每次会话必出，不再随机。
# 限时远征：限定时段，每天 18:00–23:59 打开会话即提醒。
# 与 reminder.collect_reminders（活动/礼包截止提醒）相互独立、互不影响。
_AFFAIR_HINT_START = 18   # 限时远征起始小时（含）
_AFFAIR_HINT_END = 23     # 限时远征结束小时（含，23:59 覆盖在内）
_AFFAIR_HINT_MAIL = "新的邮件处理了吗？不要遗漏，过期了后悔可就来不及了。"
_AFFAIR_HINT_EXPEDITION = "记得限时远征。"


def _affair_hints() -> list:
    """事务提醒列表：邮件固定出现；限时远征仅 18:00–23:59 追加（远征在前）。"""
    items = []
    h = datetime.datetime.now().hour
    if _AFFAIR_HINT_START <= h <= _AFFAIR_HINT_END:
        items.append(_AFFAIR_HINT_EXPEDITION)
    items.append(_AFFAIR_HINT_MAIL)
    return items


def _skill_enabled(name: str) -> bool:
    """某项「能力（技能包）」当前是否启用 —— **零工具能力**的消费方接线入口。

    提醒、闲聊这类能力是后端确定性生成的、**不经过工具**，技能包层的工具开关管不到
    它们，只能在消费方（拼装提醒横幅 / 闲聊入口）这里判断一次。

    加载失败一律返回 True：宁可多提醒，也不能因为技能包模块异常把提醒整块弄丢。
    用法：AKALI_AGENT_SKILLS=-reminder 即关闭日程提醒。
    """
    try:
        from agent.skill_pack import is_enabled
        return is_enabled(name)
    except Exception:
        return True


# ==================== 其他游戏硬拦截（同源于 core.boundary）====================
# 提到/涉及其他任何游戏 → 挡回，不回答、不介绍；词表与话术唯一数据源在 core.boundary，
# 与 guard 路由前拦截同源（同类于违规，仅话术不同）。
_OTHER_GAME_WORDS = _boundary.OTHER_GAME_WORDS
_OTHER_GAME_BAN_REPLY_1 = _boundary.OTHER_GAME_REPLY_1
_OTHER_GAME_BAN_REPLY_2 = _boundary.OTHER_GAME_REPLY_2


def _hits_other_game(text: str) -> bool:
    return _boundary.is_other_game(text)


# ===== LLM 输出审查（用户口径 2026-09-09）=====
# LLM 生成的回复在返回给用户前再过一道审查：内容涉及其他任何游戏 / 违规（涉政违禁、
# 越界情色等系统提示词禁令）→ 整条替换为对应固定报错回复，绝不透传违规内容。
# 只挂在 LLM 生成路径；工作类固定输出（数据层直出的查询/计算结果）不经此函数。
# 违规 / 越界固定话术：**唯一数据源在 core.boundary**（与 guard 路由前拦截同源）
_AUDIT_ILLEGAL_REPLY = _boundary.ILLEGAL_REPLY
_AUDIT_OVERSTEP_REPLY = _boundary.OVERSTEP_REPLY_ASSISTANT
_AUDIT_OVERSTEP_REPLY_OTHER = _boundary.OVERSTEP_REPLY_OTHER


def _mentions_ship(text: str) -> bool:
    """文本里是否出现某个角色（复用 core.boundary，同源）。"""
    return _boundary.mentions_ship(text)


def _overstep_reply(user_msg: str = '', target: str = '') -> str:
    """越界固定话术：按「主体」分流（复用 core.boundary，同源）。

    target 优先采用 LLM 语境分类（_analyze_chat_context，带上下文）判定的主体；
    缺失时才用当轮字面兜底。character=说的是某个角色；assistant=冲着阿卡莉本人。"""
    return _boundary.overstep_reply(user_msg, target)


def _audit_llm_reply(reply: str, user_msg: str = '', target: str = '') -> str:
    """审查 LLM 生成回复：其他游戏 → 其他游戏禁回复；涉政违禁 → 固定拒绝；越界情色 → 固定拒绝。"""
    t = reply or ''
    if not t:
        return t
    # 1) 其他游戏（词表比 _BANNED_RE 更全：含 王者/lol/steam/mc 等口语与英文缩写）
    if _hits_other_game(t):
        return random.choice([_OTHER_GAME_BAN_REPLY_1, _OTHER_GAME_BAN_REPLY_2])
    # 2) 违法违规（涉政/毒品/暴力等，复用 calc_flow _BANNED_RE 禁区正则）
    if _is_illegal(t):
        return _AUDIT_ILLEGAL_REPLY
    # 3) 越界情色内容（性器官/性行为描写出现在回复里）
    if _is_flirt_overstep(t):
        return _overstep_reply(user_msg, target)
    return t


def _looks_garbage(text: str) -> bool:
    """判断输入是否「无法识别为任何工具（含闲聊）」：剥离空白后为空、
    或没有任何中文字符且没有成词英文（纯符号/数字/emoji/乱码）→ True。
    用于兜底固定回复，避免把无意义输入丢给闲聊 LLM 浪费 token。"""
    s = re.sub(r'\s+', '', text or '')
    if not s:
        return True
    has_cn = bool(re.search(r'[\u4e00-\u9fa5]', s))
    has_en = bool(re.search(r'[A-Za-z]{2,}', s))
    return not (has_cn or has_en)


_CHAT_DISABLED_REPLIES = (
    "闲聊功能已关闭，指挥官。我只负责计算与查询。",
    "闲聊通道已关闭。要算什么、查什么，直接说。",
    "当前仅开放计算与查询，闲聊调情暂时不接。",
)


def _chat_enabled() -> bool:
    """闲聊开关（环境变量 AKALI_CHAT_ENABLED）：'0'/'false'/'off'/'no' 关闭
    （仅计算/查询，闲聊调情固定拦截）；缺省或其它值视为开启。"""
    v = (os.environ.get('AKALI_CHAT_ENABLED') or '').strip().lower()
    return v not in ('0', 'false', 'off', 'no')


def _is_bye(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    m = _BYE_PATTERN.search(t)
    if not m:
        return False
    # 「88」歧义消解：是氪金档位（88自选/88礼包）而非告别时，剔除这一个匹配再判；
    # 若整句再无其他告别词，则不按告别处理。
    if m.group(0) == '88' and _BYE_88_GUARD.search(t):
        if not _BYE_PATTERN.search(t.replace('88', ' ', 1)):
            return False
    # 短文本命中再见词直接判定；长文本要求再见词出现在开头或结尾附近，
    # 避免误伤含"晚安/走了"等词的功能指令
    if len(t) <= 8:
        return True
    return bool(_BYE_PATTERN.search(t[:3]) or _BYE_PATTERN.search(t[-6:]))


# 攻略意图关键词：用户输入命中任一才允许攻略目录/面板输出
# （"我是新手"→新手；"要攻略"→攻略；"练级"→练级；查装备/算伤害/闲聊不命中）
_GUIDE_INTENT_WORDS = (
    "攻略", "新手", "萌新", "入坑", "练级", "配装", "pve", "PVE",
    "拉古兹", "竞技场", "无尽", "一图流", "速查", "推图", "百科",
)


def _has_guide_intent(text: str) -> bool:
    if not text:
        return False
    return any(w in text for w in _GUIDE_INTENT_WORDS)


# 攻略目录段起头标记（query_guide 输出 + LLM 可能的转述措辞）——无攻略意图时，从任意标记处
# 剥离到结尾。覆盖：标准输出"知识库攻略清单"、LLM 转述"攻略清单/攻略目录/以下攻略/攻略如下/
# 知识库攻略（"等。
_GUIDE_SECTION_STARTS = (
    "知识库攻略清单",
    "知识库攻略（",
    "攻略目录",
    "攻略清单",
    "以下攻略",
    "攻略如下",
    "全部攻略",
    "可用攻略",
    "为您找到",
)
_GUIDE_SECTION_PAT = re.compile(
    r'(?:' + '|'.join(re.escape(s) for s in _GUIDE_SECTION_STARTS) + r')[\s\S]*$'
)
# 攻略目录里的单行条目特征（"xx （docx）/（图片）/（xlsx）" 行），无意图时逐行剔除兜底
_GUIDE_ITEM_LINE = re.compile(r'^\s*[^\n]{0,40}（(?:docx|图片|xlsx)）\s*$', re.MULTILINE)


def _strip_guide_section(text: str) -> str:
    """无攻略意图时，把 LLM 误粘贴的攻略目录整段剥离，只留正常对话内容。"""
    if not text:
        return text
    t = _GUIDE_SECTION_PAT.sub('', text).strip()
    for suffix in ("告诉我攻略类型即可定位文档", "可输入关键词查询", "输入「攻略」可查看全部", "如需查看内容，请直接打开"):
        idx = t.find(suffix)
        if idx != -1:
            t = t[:idx].strip()
            break
    # 清理剥离后残留的"为您整理/顺便看看这些攻略"类引导残句（整句删除，不误伤正常句）
    t = re.sub(
        r'(?:为您整理|为您找到|顺便看看这些攻略|以下是|这里[^\n。！？]*攻略)[^\n。！？]*[。！？]?[\n]?$',
        '', t
    ).strip()
    return t


# 问候语模式（续聊时若 LLM 仍习惯性问候，剥离开头问候，只留正题）
# 只匹配「纯问候句」（问候词+寒暄尾），不含具体内容（如"已为您定位到XX"），避免误删正题
_GREETING_PAT = re.compile(
    r'^(?:（[^）]*）)?'          # 可选神态前缀（神态/表情括号标注等）
    r'(?:噢，指挥官[，。!！\s]*)?'
    r'(?:早上好|上午好|中午好|下午好|晚上好|你好|您好|哈喽|嗨|hello|hi|hey|晚安|好久不见)[，。!！\s]*'
    r'(?:指挥官[，。!！\s]*)?'
    r'(?:随时待命|有何吩咐|有什么可以帮到您|有什么需要帮助的?|需要什么帮助|直接告诉我你要做什么|请问有什么[^\n。！？]{0,8}帮助的?|很高兴见到您)?[。!！\s]*'
)


def _strip_greeting(text: str) -> str:
    """续聊时剥离回复开头的问候语（问候只在会话首次出现一次）。"""
    if not text:
        return text
    return _GREETING_PAT.sub('', text).strip()


# 点燃/总伤害结果中不应出现的单次字段行（累计伤害无暴击概念，LLM 不得自行补充）
# 仅删除「字段名 + 未提供/无数据」这类空行；有数值的行（单次攻击结果）原样保留
_ACCUM_EXTRA_LINE = re.compile(
    r'^[ \t]*(?:最高暴击伤害|命中率|暴击率|命中率/暴击率|期望伤害|实际伤害|原始伤害)[：:]\s*(?:未提供|无数据|暂无)[^\n]*$',
    re.MULTILINE,
)


def _strip_accumulated_extra_fields(text: str) -> str:
    """点燃/总伤害结果：剥离 LLM 误补的「最高暴击伤害/命中率/暴击率…未提供」等单次字段行。"""
    if not text:
        return text
    return _ACCUM_EXTRA_LINE.sub('', text).strip()


# 网络类错误关键词：命中才重试（连接失败/超时/服务暂不可用等偶发故障），业务错误不重试
_RETRY_KEYWORDS = (
    "connection error", "connectionerror", "connection refused", "connection reset",
    "timeout", "timed out", "timedout", "读超时", "连接超时", "网络",
    "temporarily unavailable", "server busy", "rate limit", "503", "429",
)


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _RETRY_KEYWORDS)


# 违规/内容安全类错误关键词：用户输入触发模型安全策略/审核 → 「越界，禁止讨论」
#（区别于服务器问题：只有网络/API 等连接异常才属于服务器ojbk）
_SAFETY_ERR_KEYWORDS = (
    "content_filter", "content filter", "safety", "policy", "moderation",
    "harmful", "illegal", "politic", "敏感", "违规", "审核", "内容安全", "拒绝回答",
    "不安全", "不适合回答", "色情", "政治",
)


def _is_safety_error(exc: Exception) -> bool:
    msg = ('%s %s' % (type(exc).__name__, str(exc))).lower()
    return any(k in msg for k in _SAFETY_ERR_KEYWORDS)


# 额度耗尽类错误（403 Free quota exhausted / 余额不足）→ 明确提示，区别于服务器ojbk
_QUOTA_ERR_KEYWORDS = (
    "quota exhausted", "free quota", "free tier", "balance", "billing",
    "额度", "余额不足", "quota exceeded",
)


def _is_quota_error(exc: Exception) -> bool:
    msg = ('%s %s' % (type(exc).__name__, str(exc))).lower()
    return any(k in msg for k in _QUOTA_ERR_KEYWORDS)


# 认证/鉴权类错误（API Key 缺失/无效/过期）→ 明确提示
_AUTH_ERR_KEYWORDS = (
    "invalid api key", "api key", "unauthorized", "authentication",
    "401", "403", "access denied", "forbidden",
    "api_key", "apikey", "鉴权", "认证",
)


def _is_auth_error(exc: Exception) -> bool:
    msg = ('%s %s' % (type(exc).__name__, str(exc))).lower()
    return any(k in msg for k in _AUTH_ERR_KEYWORDS)


# LLM 安全拒绝文本（用户输入违规 → 模型拒绝回答）：统一替换为「与阿卡莉无关，不回答」
_SAFETY_REFUSAL_RE = re.compile(
    r'(抱歉|对不起|不好意思).{0,12}(无法回答|不能回答|不能提供|拒绝回答|违反|敏感|不适宜|不适合回答)'
    r'|(我不能|我无法|不允许).{0,8}(回答|提供|讨论|说)'
)


def _is_safety_refusal(text: str) -> bool:
    return bool(_SAFETY_REFUSAL_RE.search(text or ''))


def _run_agent_with_retry(messages: list, max_retries: int = 2):
    """调用大模型，网络类偶发错误自动重试（最多 max_retries 次，退避等待）。
    业务错误（参数/鉴权/上下文超长等）不重试，直接抛出。"""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return agent.agent.run_nonstream(messages=messages)
        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt >= max_retries:
                raise
            time.sleep(1.5 * (attempt + 1))  # 1.5s / 3s 退避
    raise last_exc  # 理论不可达，保险


# ===== 法定节假日询问 → 固定话术（用户口径 2026-09-16）=====
# 节假日「不接受询问」（不查、不答具体日期），只在当天由 _today_holiday 追加祝福。
# 命中节假日问法 → 固定话术 + 「惊讶」表情（不是嫌弃，不是工作答复）。
# 触发：放假/休假/假期/调休/过节/节日/(什么|哪些|哪个|啥)节/具体节日名。
# 位置说明：本分支在 route_work 之后兜底 —— 含「活动」的问法（如「节日活动有哪些」）
# 会先被 calc_flow 的游戏内活动分支接走，故不会抢走活动查询。
_HOLIDAY_ASK_RE = re.compile(
    r'节假日|法定假日|法定节假日|放假|休假|假期|调休|过节|'
    r'(?:什么|哪些|哪个|啥)节|节日|'
    r'春节|元旦|清明|劳动节|端午|中秋|国庆|元宵|七夕|重阳|除夕|腊八'
)


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    """对话接口外层：把鉴权结果交给业务层，再调用 _chat_impl。

    为什么要多收一个 Starlette Request（用户口径 2026-09-25）：鉴权中间件把 uid 挂在
    `request.state.uid` 上，但 _chat_impl 收的是 **ChatRequest（Pydantic 模型，没有 .state）**，
    于是配额那边拿不到 uid、只能退化成按 IP 甚至 "unknown" 统计。这里把 uid 透传过去，
    用量就真正按 uid 分别记录了。
    """
    if not getattr(req, 'uid', None):
        req.uid = getattr(request.state, 'uid', None) or None
    # LLM 记账上下文（用户口径 2026-09-26）：本次请求内的全部 LLM 调用
    # （闲聊主链工具循环/意图分类/攻略生成/检索增强/表情与越界判定…）
    # 由 openai SDK 层挂钩统一记账（core/llm_accounting），这里只告知记账对象。
    from core import req_ctx as _req_ctx
    try:
        from core.quota import resolve_identity as _q_ident
        _qkey = _q_ident(request)
    except Exception:
        _qkey = str(getattr(request.state, 'uid', '') or '')
    _qkey_tok = _req_ctx.set_quota_key(_qkey)
    try:
        resp = _chat_impl(req)
    finally:
        _req_ctx.reset_quota_key(_qkey_tok)
    # 请求级用量明细（开发者侧「流量/请求明细」数据源）：走 LLM 的请求由 _chat_impl
    # 把本轮 token 以 _tok 附在响应里；未走 LLM（工具类/超限提示）按 0 记。
    # 命中缓存标记（core.kb_cache）只在内部排障用，同样不下发给前端。
    resp.pop('_cache', None)
    try:
        from core.quota import log_request as _q_log
        _q_log(req.uid or '', int(resp.pop('_tok', 0) or 0))
    except Exception:
        pass
    return resp


# ===== 悬浮球“活动”按钮：列出活动日历里全部未结束的活动（含活动期结束但兑换期未结束的）=====
# 数据来源为 知识库/活动/活动日历.xlsx，每次查询实时读盘（改完活动日历即生效），无需重启。
def _handle_activity_list() -> dict:
    from tools.knowledge import QueryActivity
    reply = QueryActivity().call({})
    return {'reply': reply, 'tool_outputs': [], 'suggestions': [], 'fill_form': [],
            'encounter_form': [], 'guide_files': [], 'guide_lib': '', 'ok': True, 'bye': False}


def _chat_impl(req: ChatRequest):
    """对话接口：返回秘书回复 + 工具原始输出（含计算结果明细）。
    按 session_id 隔离对话状态（chat_history / session_overrides），多人调用互不串。
    用同步 def，FastAPI 自动放入线程池执行，避免 run_nonstream 阻塞事件循环。"""
    # 【输入预处理·先于一切】昵称/名字标准映射：舰名/BOSS 的 昵称·谐音·错字 → 正规名。
    # 先于所有判定（其他游戏拦截 / 再见 / 节假日 / 路由 / 语境分类 / 生成后审查）执行，统一输入口径。
    try:
        from core.entity_norm import standardize
        _norm_msg = standardize(req.message or '')
        if _norm_msg and _norm_msg != req.message:
            req.message = _norm_msg
    except Exception:
        pass

    # 请求级用户面板覆盖（云端模式）：服务器无用户文件，面板随请求携带。
    # local 模式下前端不携带 → 恒为 None，计算路径行为与改造前完全一致。
    _panel_tok = req_ctx.set_panel_overrides(getattr(req, 'panel_overrides', None))
    try:
        s = _get_session(req.session_id)
        hist = s['chat_history']
        sover = s['session_overrides']
        # 顶层路由 1a：「结束本轮」= 中断当前任务（计算/查攻略/闲聊），只清临时记忆、保留会话历史；
        # 对明确启动的工具状态机，它是用户主动中止当前步骤的入口。
        if '结束本轮' in (req.message or ''):
            sover.clear()
            s['chat_rounds'] = 0  # 中断任务后闲聊重新计时
            s['flirt_total'] = 0  # 调情轮次计数一并重置
            s['flirt_standby'] = False
            return {"reply": _random_exit_task_reply(), "tool_outputs": [], "suggestions": [], "fill_form": [],
                    "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "bye": False}

        # 顶层路由 1a2：其他游戏硬拦截——只要提到/涉及其他任何游戏，一律不回答，
        # 直接挡回（不走 LLM，避免把原型舰等关联内容介绍出来）
        if _hits_other_game(req.message or ''):
            return {"reply": random.choice((_OTHER_GAME_BAN_REPLY_1, _OTHER_GAME_BAN_REPLY_2)),
                    "tool_outputs": [], "suggestions": [], "fill_form": [],
                    "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "bye": False}

        # 顶层路由 1b：再见/退出 → 退出当前会话并清空记忆，直接告别（不调 LLM，省 token）
        if _is_bye(req.message):
            # 区分「进行中任务」与「纯聊天」：
            # 有临时记忆（计算 buff/天赋/面板/敌方覆写）或上一轮回复含交互标记（表单/按钮/攻略面板）
            # → 视为正在执行具体工具流程，退出=终止当前操作返回主流程
            in_task = bool(sover)
            if not in_task and hist:
                last_assistant = ''
                for m in reversed(hist):
                    if m.get('role') == 'assistant' and m.get('content'):
                        last_assistant = str(m['content'])
                        break
                in_task = any(tag in last_assistant for tag in ('<<SUGGEST_CALC>>', '<<FILL_FORM>>',
                                                                 '<<ENCOUNTER_FORM>>', '<<GUIDE_LIB>>',
                                                                 '<<GUIDE_FILE>>'))
            sover.clear()
            hist.clear()
            s['chat_rounds'] = 0  # 新会话从零计闲聊轮次
            s['flirt_total'] = 0  # 新会话调情轮次从零计
            s['flirt_standby'] = False
            bye_reply = _random_exit_task_reply() if in_task else _random_exit_chat_reply()
            return {"reply": bye_reply, "tool_outputs": [], "suggestions": [], "fill_form": [],
                    "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "bye": True}

        # 顶层路由 1c0：计算工具调用后的反馈纠错（算错/识别不对等）→ 固定道歉 + 提示自改 buff
        # 仅「紧接计算结果的下一跳」可纠错；非反馈即清标记，避免误触后续无关输入。
        if sover.get('last_calc_tool'):
            if _is_calc_error_feedback(req.message):
                sover['last_calc_tool'] = False
                return {"reply": "抱歉，本次计算可能有误。你可以直接输入 buff 来修正，我重新算一遍。",
                        "tool_outputs": [], "suggestions": [], "fill_form": [],
                        "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True}
            sover['last_calc_tool'] = False

        # 顶层路由 1c-0：**法定节假日询问**（用户口径 2026-09-16）——节假日不接受询问，
        # 既不查日期也不答具体安排，只在当天由 _today_holiday 追加祝福。
        # 命中节假日问法 → 固定话术 + 「惊讶」表情（不是工作答复、不写历史、不锁闲聊模式）。
        # 必须**先于 route_work**：否则「今天什么节日/有什么节日/什么时候放假」会被
        # calc_flow 的攻略兜底 / 玩法介绍分支抢先接走（实测），到不了这里。
        # 含「活动」的消息一律放行给 route_work（属游戏内活动查询，见 calc_flow 0f 分支）。
        _m_holi = req.message or ''
        if _HOLIDAY_ASK_RE.search(_m_holi) and '活动' not in _m_holi:
            return {"reply": "怎么，已经累了想要休假了？放心，我会安排好的，到时自然会跟你说。",
                    "tool_outputs": [], "suggestions": [], "fill_form": [],
                    "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True,
                    "bye": False, "expression": _expression_image_url("惊讶"), "sticker": None}

        # 顶层路由 1c：工作类意图路由（后端硬编码，不走 LLM）——
        # 查询/计算/攻略/装备/天气等固定输出；路由不到才走 LLM（纯闲聊）
        # 每轮都结合最近对话重新分析意图；闲聊、查询、攻略之间可自由切换。
        # 只有 calc_state / favor_pending 等已明确启动的结构化工具状态会续接下一步。
        # AKALI_ROUTE_MODE=agent(默认,function calling) | legacy(故障回退)
        # 分发层在 agent 模式懒加载 LangChain，异常自动回落 legacy，见 core/route_dispatch.py
        # 顶层路由 1c0-2：悬浮球「活动」按钮（精确匹配单字『活动』）→ 列出活动日历全部未结束活动。
        # 自由文本里的「活动」（如「活动什么时候结束」「查活动攻略」）仍交 route_work 处理；
        # 仅菜单按钮的精确『活动』走这里（用户口径 2026-09-25）。
        if (req.message or '').strip() == '活动':
            return _handle_activity_list()

        from core.route_dispatch import route_work_dispatch as route_work
        work_tools = build_tools(agent.app, sover, location_ref=agent)
        # 显式「好感度计算」入口（工具箱菜单「好感度」发出的指令）：直接走 calc_flow 的 favor_form
        # 分支，返回 favor_form=True 让前端弹出计算器。否则 agent 模式下 LLM 会直接调 CalcFavor 工具，
        # 缺参数只回澄清文字、不带 favor_form，导致点了不弹窗（用户 2026-10-01 反馈）。
        # 仅当同时含「好感」+「计算」才强制；解释类问法（有什么用/怎么算）不命中，仍走正常路由。
        _favor_req = req.message or ''
        if '好感' in _favor_req and '计算' in _favor_req:
            from core.calc_flow import route_work as _cf_route
            _fav = _cf_route(work_tools, sover, _favor_req, agent=agent, allow_intent_llm=False)
            work_result = _fav if (_fav and _fav.get('favor_form')) else \
                route_work(work_tools, sover, req.message, agent=agent, history=hist)
        else:
            work_result = route_work(work_tools, sover, req.message, agent=agent, history=hist)
        if work_result is not None:
            if work_result.get('no_history'):
                # 违规/禁区轮：强制 嫌弃 表情（复用项目已有差分）+ 固定话术（不写历史）。
                # no_history 分支在情绪装配块之前直接返回，故在此手动注入表情。
                work_result['expression'] = _expression_image_url('嫌弃')
                # 固定话术也可能经 route_work 带来 LLM 来源标注 / ** 标记，统一清洗
                work_result['reply'] = _strip_reply_format_marker(
                    work_result.get('reply') or '', keep_source=work_result.get('keep_source'))
                return work_result
            # 工作类分支会在下方闲聊情绪装配前直接返回；质疑复核由 guard 标记，
            # 必须在这里装配“惊讶”差分。
            _work_expression = work_result.pop('expression_hint', None)
            if _work_expression:
                work_result['expression'] = _expression_image_url(_work_expression)
            # 工作类回复也记入短期历史（供后续闲聊参考；剥离标记）
            clean_w = _strip_suggestion_marker(work_result['reply'])
            clean_w = _strip_fill_form_marker(clean_w)
            clean_w = _strip_guide_lib_marker(clean_w)
            clean_w = _strip_avatar_marker(clean_w)
            # 头像标记保留在回复文本流中（query_ship_info 已插入到「--- 名字 ---」行后），
            # 前端在对应位置渲染头像；历史记录已剥离
            hist.append({'role': 'user', 'content': req.message})
            hist.append({'role': 'assistant', 'content': clean_w})
            if len(hist) > 16:
                del hist[:-16]
            s['chat_rounds'] = 0  # 工作类轮次重置闲聊计数
            s['flirt_total'] = 0  # 工作类轮次重置调情计数
            s['flirt_standby'] = False
            # 工作类回复同样过格式清洗（来源标注 / 加粗 ** 等），与闲聊分支（1756 清洗链）保持一致：
            # route_work 直返的回复（资源不足攻略、查询/计算固定输出等）若不过此链，
            # 会原样透传 LLM 经 _handle_guide_qa 带来的「（依据攻略片段整理）」与 ** 标记。
            # keep_source=True（资源不足回复）时保留其末尾「来源：《XX》」攻略出处标注。
            work_result['reply'] = _strip_reply_format_marker(
                work_result.get('reply') or '', keep_source=work_result.get('keep_source'))
            return work_result
        # 闲聊开关（环境变量 AKALI_CHAT_ENABLED）：关闭后仅计算/查询，
        # 闲聊与调情一律固定回复拉回工作，不设闲聊锁、不走 LLM
        if not _chat_enabled():
            return {"reply": random.choice(_CHAT_DISABLED_REPLIES),
                    "tool_outputs": [], "suggestions": [], "fill_form": [],
                    "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "sticker": None}
        # 路由不到且不像闲聊（纯符号/数字/emoji/乱码/空白）→ 固定回复，不浪费 LLM
        if _looks_garbage(req.message or ''):
            return {"reply": "系统暂时无法识别。",
                    "tool_outputs": [], "suggestions": [], "fill_form": [],
                    "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "sticker": None}
        # 路由不到 → 本轮作为纯闲聊/调情处理，不锁定下一轮。

        # ===== 多用户算力配额（cloud 默认启用；AKALI_RUN_MODE=local 或 FX_CLOUD_MODE=off 时不闸控）=====
        # 仅拦截「走 LLM 的闲聊」（工作类已在上面 route_work 分支正常返回、不受影响）。
        # 超限额 → 停用 LLM，返回固定提示；基础工具（查询/计算/攻略）仍可用，下周四零点恢复。
        from core.quota import (get_client_key as _q_key, get_user_plan as _q_plan,
                                check_llm_quota as _q_check,
                                quota_prompt as _q_prompt)
        _uid = _q_key(req)
        _plan = _q_plan(_uid)
        _q = _q_check(_uid, _plan)
        if not _q['allowed']:
            _pull = _q_prompt(_uid, _plan, _q)
            return {"reply": _pull, "tool_outputs": [], "suggestions": [], "fill_form": [],
                    "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "sticker": None}

        # 顶层路由 2：注入当前时间与会话状态，供 LLM 问好/闲聊/功能切换确认判断
        # 人设热重载：system_prompt.md 变更后无需重启，下次闲聊自动生效（仅检查 mtime，极轻量）
        _reload_if_changed()
        # 闲聊语境前置判定：综合近期双方对话，而不是只靠本轮关键词或旧的粘滞标记。
        _chat_ctx = _analyze_chat_context(
            hist, req.message,
            active_flirt=bool((sover.get('flirt_rounds') or 0) > 0 or s.get('flirt_total', 0) > 0),
        )
        _chat_ctx_overstep = bool(_chat_ctx.get('overstep'))
        _is_flirt_turn = _chat_ctx.get('mode') == 'flirt'
        if _chat_ctx_overstep:
            return {"reply": _overstep_reply(req.message, _chat_ctx.get('target')), "tool_outputs": [], "suggestions": [],
                    "fill_form": [], "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True,
                    "bye": False,
                    "expression": _expression_image_url(
                        _boundary.overstep_expression(req.message, _chat_ctx.get('target'))),
                    "sticker": None}

        # 情感前置判定（喜欢/亲近类 = 害羞）：用于下方注入阶段「不得生硬拒绝、按当前时段语气回应」。
        _user_emotion_pre = _analyze_user_emotion(req.message)
        _is_affection = (_user_emotion_pre == '害羞') or _is_flirt_turn
        # 闲聊轮次控制：调情语境由上下文模型逐轮重新判断，上限恰好 20 轮；
        # 普通闲聊上限 _CHITCHAT_MAX_ROUNDS(4) 轮；两者分开计数（调情不消耗普通闲聊轮次）。
        # 第 20 轮正常回应并收尾；第 21 次连续调情不再调用 LLM，只返回「待机中」。
        if _is_flirt_turn:
            sover['flirt_rounds'] = 3
            s['chat_rounds'] = 0
            if s.get('flirt_standby'):
                return {"reply": "待机中。", "tool_outputs": [], "suggestions": [], "fill_form": [],
                        "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "sticker": None}
            s['flirt_total'] = s.get('flirt_total', 0) + 1
            _flirt_finishing = s['flirt_total'] == _FLIRT_FINISH_ROUND
        else:
            # 换到普通闲聊即视为用户中断连续调情，清掉计数和待机状态。
            sover['flirt_rounds'] = 0
            s['flirt_total'] = 0
            s['flirt_standby'] = False
            s['chat_rounds'] = s.get('chat_rounds', 0) + 1
            _flirt_finishing = False
            if s['chat_rounds'] > _CHITCHAT_MAX_ROUNDS:
                pull = _random_night_rest_reply() if _is_night() else _random_work_pull_reply()
                hist.append({'role': 'user', 'content': req.message})
                hist.append({'role': 'assistant', 'content': pull})
                if len(hist) > 16:
                    del hist[:-16]
                return {"reply": pull, "tool_outputs": [], "suggestions": [], "fill_form": [],
                        "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True, "sticker": None}
        _incapable_req = _boundary.is_incapable(req.message)
        user_msg = _build_user_message(req.message, hist, overrides=sover)
        # 能力不可执行：真实世界操作/创作类（排日程/画画/写代码）无此能力 →
        # LLM 自由生成「代聊式」回复，但原则：糊弄而不真正操作（不假装完成/交付）。
        # 表情不强制，交 LLM 按人设自然选（与改之前一致）。
        if _incapable_req:
            user_msg = ('[这个请求你实际上做不到（例如写代码、画画、排日程等真实世界操作），'
                        '阿卡莉没有这个能力。请用你的人设语气自然接住、调侃糊弄过去；'
                        '绝不要假装去执行或声称已完成操作（不要说"我帮你写好了/放你邮箱了/已经排好了"），'
                        '也不要给出可运行结果；把它当成闲聊逗趣接住即可。不调用工具、不检索。]') + user_msg
        # 夜间闲聊（非调情语境）：不追问工作/游戏正事；但**不要每条消息都劝睡**——
        # 仅在用户显疲态（累/困/睡/乏/疲/想休息/熬）或极晚（00:00–04:59）时才温和带一句休息提示，
        # 互动良好、聊得正起劲时不打断、不反复推休息。独立 if（不再 elif）：
        # 喜欢类回应指令在其后单独叠加，不因喜欢类而跳过时段背景。
        if _is_night() and not _is_flirt_turn:
            _hour = datetime.datetime.now().hour
            _tired = any(w in req.message for w in ('累', '困', '睡', '乏', '疲', '想休息', '歇', '熬', '休息'))
            if _tired or _hour < 5:
                user_msg = ('[夜间时段：不主动提及或追问工作/游戏正事（用户主动提才接）；'
                            '用户显疲态或已极晚，可顺带一句傲娇关心、温和提示休息，但不过度劝说、不反复催]'
                            + user_msg)
            else:
                user_msg = ('[夜间时段：不主动提及或追问工作/游戏正事（用户主动提才接），'
                            '正常陪伴闲聊即可，不要每逢对话都劝睡或催休息，互动良好时自然聊、不打断]'
                            + user_msg)
        # 喜欢/亲近类表达（害羞）：不得生硬拒绝或冷处理——严禁「数据偏差」「异常值」等术语腔；
        # 按当前时段语气自然回应（时间趋势照常主导：正午就正午的口吻、夜间就夜间的口吻），保持傲娇人设。
        if _is_affection:
            user_msg = ('[用户此轮表达了喜欢/爱意（后端已判定为害羞情感）。回应不得生硬拒绝或冷处理——'
                        '必须正面接住用户的心意，至少给出一个第一人称的积极情感回应，'
                        '如“我也喜欢你/我很高兴/你的心意我收下了/我会陪着你”中语义相当的表达；'
                        '严禁用心跳、心率、咖啡、激素、数据偏差、异常值、无法量化或系统术语回避感情；'
                        '害羞和傲娇只是语气，不能取代回应。同时严格遵守本轮注入的当前时段语气硬约束]' + user_msg)
        if _is_assistant_tease_input(req.message):
            user_msg = ('[用户正在亲昵地逗阿卡莉。这是熟人间的闲聊互动，请以喜欢用户的秘书身份自然接住；'
                        '可以害羞、嘴硬或温柔，但要让人感到亲近。不要引用知识库，不要解释问题类型，'
                        '不要使用功率、误差、延迟、数据、模块、系统、推演等机器术语，也不要说“有事直说”。]'
                        + user_msg)
        user_msg = (('[上下文要求：这是连续调情；' if _is_flirt_turn else '[上下文要求：这是普通闲聊；')
                    + '必须承接近期双方对话，正确理解省略、指代和话题切换，不要把旧话题强行延续。]'
                    + user_msg)
        if _flirt_finishing:
            user_msg = ('[这是连续调情第20轮，也是允许正常回应的最后一轮。请正面回应本轮，'
                        '并在本轮末自然收尾；不要提前拒绝，不要输出<STANDBY>标记。]') + user_msg
        messages = list(hist) + [{'role': 'user', 'content': user_msg}]
        # Token 监控：输入估算 = 人设模板 + 历史 + 用户消息（qwen_agent 内部自动附加 system）
        _tok_in = _est_tokens(agent.prompt_template) + sum(_est_tokens(m.get('content') or '') for m in messages)
        responses = _run_agent_with_retry(messages)
        # 提取最终回复与所有工具原始输出
        reply = ''
        tool_outputs = []
        for msg in responses:
            role = msg.get('role')
            content = msg.get('content', '')
            if role == 'function':
                # 工具结果消息：content 是工具返回字符串
                if content:
                    tool_outputs.append(content if isinstance(content, str) else str(content))
            elif role == 'assistant':
                # 最终 assistant 文本（跳过仅含 function_call 的中间 assistant 消息）
                if content:
                    reply = content if isinstance(content, str) else str(content)
        # 轻度亲昵调侃的生成兜底：若模型仍套用知识库拒答或机器参数话术，替换为自然互动。
        reply = _polish_assistant_tease_reply(reply, req.message)
        # 用户输入违规 → LLM 输出安全拒绝文本：按话术回复，该轮不写入历史（对话继续之前内容）
        _no_hist = False
        if _is_safety_refusal(reply):
            # 违规分流：色情类 → 固定话术；其他违规 → 无关不回答（用户口径 2026-09-17）
            reply = _overstep_reply(req.message, _chat_ctx.get('target')) if _chat_ctx_overstep else _AUDIT_ILLEGAL_REPLY
            _no_hist = True
            # 违规轮不消耗闲聊轮次计数（避免提前触发拉回工作、打断继续对话）
            s['chat_rounds'] = max(0, s.get('chat_rounds', 0) - 1)
        # 解析回复中的 <<SUGGEST_CALC>> / <<FILL_FORM>> / <<ENCOUNTER_FORM>> / <<GUIDE_FILE>> 标记
        suggestions = _extract_suggestions(reply)
        fill_form = _extract_fill_form(reply)
        fill_form = _fill_form_with_pvp_default(fill_form, sover, getattr(agent, 'raw_name_matcher', None))
        encounter_form = _extract_encounter_form(reply)
        # 兜底：LLM 说了引导语但漏输出 <<FILL_FORM>> 标记时，按引导语自动补表单，
        # 避免前端无表单可填导致流程卡住
        if not fill_form:
            fill_form = _auto_fill_form(reply, sover)
            fill_form = _fill_form_with_pvp_default(fill_form, sover, getattr(agent, 'raw_name_matcher', None))
        # 兜底：伤害计算类型按钮——calc_context 已固化（active=True）且回复没有 SUGGEST_CALC
        # 且没有正在填的表单时，自动补全伤害类型按钮（普攻/通常/必杀/支援/点燃/总伤害/对比/结束）
        # 例外：LLM 正在询问时间跨度（总伤害/点燃需要秒数）时，不补按钮，等用户先给时间
        if not suggestions and not fill_form and not encounter_form:
            asking_time = bool(re.search(r'(多少秒|几秒|时间跨度|需要.*秒|持续.*秒|多少时间|要.*秒)', reply))
            ctx = (sover or {}).get('calc_context') or {}
            if ctx.get('active') and not _auto_fill_form(reply, sover) and not asking_time:
                suggestions = _extract_suggestions(
                    '<<SUGGEST_CALC>>calc_normal_attack|calc_skill_attack|calc_burst_attack|'
                    'calc_support_attack|calc_burn_damage|calc_total_damage|end_round')
        # 攻略打开标记：优先从回复提取，其次从工具输出提取（不依赖 LLM 原样保留）
        guide_files = _extract_guide_files(reply) or _extract_guide_files("\n".join(tool_outputs))
        # 头像标记：查询角色时由 query_ship_info 追加 <<AVATAR>>，提取为 avatar 字段（前端渲染头像图）
        avatar = _extract_avatar([reply] + tool_outputs)
        # 神态标记 <<EXP:神态>>：LLM 按表情差分列表输出神态，提取为头像 URL（前端切换消息头像）
        # 优先级（高→低）：
        #  1) 安全门控（内容策略红线，非情感识别）：用户输入命中 违法违规 / 越界调情 / 极端恶意
        #     → 强制「嫌弃」（复用项目已有差分；策略红线绝不交给 LLM 自由裁量）。
        #  2) LLM 主导情感识别（主路径）：<<EXP>> 标记优先，由大模型按语境主动选择神态。
        #  3) 末级兜底：代码 _analyze_user_emotion 仅在 LLM 无标记时作为极窄安全网——
        #     仅保留「明显恶意→嫌弃」红线，其余一律回退默认头像，不做硬性情绪映射。
        _llm_exp = None
        _m_exp = _EXP_PATTERN.search(reply or "")
        if _m_exp:
            _llm_exp = _m_exp.group(1).strip()
        # 1) 安全门控（最高优先，硬覆盖）
        #    违规/恶劣话题（涉政违法、极端恶意）→ 厌恶嫌弃；
        #    越界情色 → 一律「无语敷衍」（用户口径 2026-09-28：越界不用厌恶）。
        if _is_illegal(req.message) or _is_malicious(req.message):
            expression_url = _expression_image_url(_boundary.ILLEGAL_EXPR)
        elif _chat_ctx_overstep or _is_flirt_overstep(req.message):
            expression_url = _expression_image_url(
                _boundary.overstep_expression(req.message, _chat_ctx.get('target')))
        # 2) LLM 主导（主路径）
        elif _llm_exp:
            if _llm_exp == "嫌弃" and not _is_malicious(req.message):
                # 非极端恶意输入：LLM 误用 嫌弃 时降级，避免日常吐槽露嫌弃
                _llm_exp = "无语敷衍"
            expression_url = _expression_image_url(_llm_exp)
        # 3) 末级兜底：代码仅保留安全红线，其余回退默认（不硬性映射情绪）
        else:
            _code_emotion = _analyze_user_emotion(req.message)
            if _code_emotion in ("嫌弃", "惊讶"):
                expression_url = _expression_image_url(_code_emotion)
            else:
                expression_url = None
        # 事务占位回复（"系统异常。"）一律常态：即便 LLM 误带神态标记也不采用
        if ('系统处理' in (reply or '')) or ('系统异常' in (reply or '')):
            expression_url = None
        # 攻略库触发标记：回复或工具输出任一出现即打开攻略库面板（可带聚焦词）
        guide_lib = _extract_guide_lib([reply] + tool_outputs)
        # 后端强制门控：仅当用户输入含明确攻略意图（新手/萌新/攻略/练级/配装…）时才保留攻略输出；
        # 否则 LLM 误调攻略工具时，剥离目录与面板标记，正常对话/查东西/算伤害绝不出现攻略
        if not _has_guide_intent(req.message):
            guide_files = []
            guide_lib = ""
        # 技能描述原文兜底：query_ship_info 返回的原始技能描述（含■①②③【】与完整倍率），
        # 若 LLM 在回复里自行概括/改写，则用工具输出的原文强制追加，保证用户看到的是知识库原文
        raw_skills = _extract_raw_skills(tool_outputs)
        # 角色缺失强制拦截：工具输出含"未找到"（真不存在或信息不全等同不存在）时，
        # 回复只保留该提示，不输出任何其他内容/计算（中止任何相关操作）
        not_found = None
        for out in tool_outputs:
            if out and '未找到' in out:
                not_found = out.strip()
                break
        # 维护短期历史（存纯净文本，不含标记）
        clean_reply = _strip_suggestion_marker(reply)
        clean_reply = _strip_fill_form_marker(clean_reply)
        clean_reply = _strip_encounter_form_marker(clean_reply)
        clean_reply = _strip_guide_file_marker(clean_reply)
        clean_reply = _strip_guide_lib_marker(clean_reply)
        clean_reply = _strip_avatar_marker(clean_reply)
        clean_reply = _strip_exp_marker(clean_reply)
        clean_reply = _strip_reply_format_marker(clean_reply)
        # 反问推销兜底：LLM 偶发输出「需要我帮你XX吗」式引导（承诺不存在的功能）→ 整句删除
        clean_reply = _strip_upsell_question(clean_reply)
        if not _has_guide_intent(req.message):
            clean_reply = _strip_guide_section(clean_reply)
        # 头像标记还原到回复文本流：LLM 转述可能丢标记，从工具输出提取后插入「--- 名字 ---」行后
        if '<<AVATAR>>' not in reply:
            _av = _extract_avatar(tool_outputs)
            if _av:
                _mark = "\n<<AVATAR>>%s|%s" % (_av['path'], _av.get('title', ''))
                _lines = reply.split('\n')
                for _i, _ln in enumerate(_lines):
                    if _ln.strip().startswith('--- ') and _ln.strip().endswith(' ---'):
                        _lines.insert(_i + 1, _mark.strip())
                        reply = '\n'.join(_lines)
                        break
                else:
                    reply = reply + _mark
        # 问候统一剥离：页面加载已有静态欢迎语（"噢，指挥官，随时待命…"），
        # 任何轮次都不再问候（LLM 可能习惯性问候，后端兜底去掉，避免"发两次晚上好"）
        clean_reply = _strip_greeting(clean_reply)
        # 收敛模型自带的 Markdown 硬换行（行尾两个空格 + 换行 → 界面上的"莫名换行"）。
        # 放在置标（<<AVATAR>>/<<GUIDE_FILE>> 等）插入之前，避免误伤标记行。
        clean_reply = _tidy_hard_breaks(clean_reply)
        # 表白/喜欢类回复必须正面接住：模型偶发的心率、咖啡、系统检测或
        # 夜间工作转移句在返回前删除；若仍无积极回应，按当前时段补一句。
        # 划清界限的双保险（用户口径 2026-09-25）：上下文虽已判 casual，但用户若
        # 说「我不可能喜欢你」这类**否定式**，仍会被关键词命中成「害羞」而走到这里，
        # 那就绝不能补「我也喜欢你」—— 这是答非所问且冒犯。
        if _AFFECTION_BOUNDARY_RE.search(req.message or ''):
            _is_affection = False
            sover['flirt_rounds'] = 0      # 同时清掉调情粘滞，下一轮不再继承
            s['flirt_total'] = 0
        if _is_affection:
            clean_reply = _polish_affection_reply(clean_reply)
        # 点燃/总伤害结果兜底：剥离 LLM 误补的"最高暴击伤害/命中率/暴击率/未提供"等单次字段
        # （累计伤害没有这些概念，工具输出里也没有，LLM 转述时不得自行补充）
        if any(k in clean_reply for k in ('点燃', '总伤害')) and ('最高暴击' in clean_reply or '未提供' in clean_reply):
            clean_reply = _strip_accumulated_extra_fields(clean_reply)
        # 角色缺失：回复整体替换为统一提示（禁止 LLM 转述任何角色内容/计算结果）
        if not_found:
            clean_reply = not_found
            suggestions = []
            fill_form = []
            encounter_form = []
        # 技能原文追加（仅当确实查了角色且回复里没有完整原文时——避免重复粘贴）
        if raw_skills and not _reply_has_raw_skills(clean_reply, raw_skills):
            clean_reply = clean_reply.rstrip()
            clean_reply += "\n\n【技能描述（原文）】\n" + raw_skills
        # LLM 输出审查（最后一道）：生成内容涉其他游戏/涉政违禁/越界情色 → 整条替换为
        # 对应固定报错回复，绝不透传；替换发生在写历史前，违规内容不进会话上下文。
        clean_reply = _audit_llm_reply(clean_reply, req.message, _chat_ctx.get('target'))
        # 违规轮（LLM 安全拒绝）：不写入会话历史，后续对话继续之前的上下文
        if not _no_hist:
            hist.append({'role': 'user', 'content': req.message})
            hist.append({'role': 'assistant', 'content': clean_reply})
            if len(hist) > 16:
                del hist[:-16]
        # 待机标记检测：LLM 在回复末尾输出 <STANDBY> 表示进入待机
        # 清理会话级临时记忆，保留闲聊历史供下次对话参考
        if '<STANDBY>' in reply:
            clean_reply = clean_reply.replace('<STANDBY>', '').strip()
            if not _flirt_finishing:
                sover.clear()
                s['flirt_total'] = 0
                s['flirt_standby'] = False
            print('[待机] 已进入待机状态，会话临时记忆已清理', flush=True)
        if _flirt_finishing and not _no_hist:
            s['flirt_standby'] = True
        # Token 监控日志（服务控制台）：本轮输入/输出估算 + 会话累计。
        # 真实记账已由 openai SDK 层挂钩完成（core/llm_accounting，读 API 响应 usage，
        # 覆盖闲聊主链工具循环每一轮与全部工作路由调用）；此处估算仅打印观察，
        # 不再重复记账（用户口径 2026-09-26）。
        _tok_out = _est_tokens(clean_reply)
        _sk = req.session_id or 'default'
        _session_token_total[_sk] = _session_token_total.get(_sk, 0) + _tok_in + _tok_out
        print('[token] 会话[%s] 输入≈%s 输出≈%s 本轮≈%s | 会话累计≈%s'
              % (_sk, _tok_in, _tok_out, _tok_in + _tok_out, _session_token_total[_sk]), flush=True)
        return {"reply": clean_reply, "tool_outputs": tool_outputs, "suggestions": suggestions,
                "fill_form": fill_form, "encounter_form": encounter_form,
                "guide_files": guide_files, "guide_lib": guide_lib, "ok": True,
                "expression": expression_url,
                "_tok": _tok_in + _tok_out,   # 本轮 token 给 chat 出口打点用，响应前会被 pop 掉
                "sticker": None}
    except Exception as e:
        # 报错轮不消耗闲聊轮次计数（越界/ojbk 后继续对话不受影响）
        # 注意：s 可能未定义（如果异常发生在 _get_session 之前）
        try:
            if 's' in dir() and s is not None:
                s['chat_rounds'] = max(0, s.get('chat_rounds', 0) - 1)
        except Exception:
            pass
        if _is_safety_error(e):
            # 用户输入违规导致 LLM 拒绝/报错 → 按话术分流回复（异常路径不写历史，对话可继续）
            _safety_reply = _overstep_reply(getattr(req, 'message', ''), (locals().get('_chat_ctx') or {}).get('target')) if locals().get('_chat_ctx_overstep', False) else _AUDIT_ILLEGAL_REPLY
            return {"reply": _safety_reply, "tool_outputs": [], "suggestions": [],
                    "fill_form": [], "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True,
                    "sticker": None}
        if _is_quota_error(e):
            # 模型额度耗尽/余额不足 → 明确提示（管理员补充额度后恢复），区别于服务器ojbk
            return {"reply": "模型额度已耗尽，请联系管理员补充后再试。", "tool_outputs": [], "suggestions": [],
                    "fill_form": [], "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": True,
                    "sticker": None}
        if _is_auth_error(e):
            # API Key 缺失/无效 → 明确提示配置问题
            return {"reply": f"API 认证失败，请检查 {API_KEY_VAR} 是否已正确设置。", "tool_outputs": [], "suggestions": [],
                    "fill_form": [], "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": False,
                    "sticker": None}
        # 其他异常：记录日志并返回友好提示
        import traceback
        print(f"[错误] 类型={type(e).__name__} 消息={_mask(e, _API_TOKEN)}", flush=True)
        traceback.print_exc()
        reply = _friendly_error_reply(e)
        _error_detail = ''
        if not runtime.is_cloud():
            _error_detail = f'{type(e).__name__}: {str(e)}'[:1000]
        return {"reply": reply, "tool_outputs": [], "suggestions": [],
                "fill_form": [], "encounter_form": [], "guide_files": [], "guide_lib": "", "ok": False,
                "sticker": OJBK_STICKER, "error_detail": _error_detail}
    finally:
        # 还原请求级面板覆盖，避免上下文残留到同线程的下一个请求
        req_ctx.reset_panel_overrides(_panel_tok)


@app.post("/api/feedback")
async def feedback(req: Request):
    """接收用户反馈（用户口径 2026-09-25），落盘为 JSON 一行一条。

    每条记录仅含三个字段：
      - 时间：服务端接收时间（%Y-%m-%d %H:%M:%S）
      - 用户uid：提交反馈的用户标识
      - 内容：反馈对应的用户提问（前端在知识库问答回复上点反馈时上报）
    云端模式写 Redis 列表 akali:feedback（保留最近 2000 条，跨重启不丢）；
    本地模式追加落盘 反馈信息/反馈收集/反馈收集.txt（同样为 JSON 一行一条）。
    """
    try:
        data = await req.json()
        from core.sensitive_filter import filter_sensitive_words
        record = {
            '时间': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            '用户uid': str(getattr(req.state, 'uid', '') or ''),
            '内容': filter_sensitive_words(str(data.get('user_input') or ''))[:2000],
        }
        line = json.dumps(record, ensure_ascii=False)

        if runtime.is_cloud():
            import redis as _redis
            r = _redis.from_url(os.getenv('FX_REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
            r.rpush('akali:feedback', line)
            r.ltrim('akali:feedback', -2000, -1)
        else:
            feedback_dir = os.path.join(PROJECT_ROOT, "反馈信息", "反馈收集")
            os.makedirs(feedback_dir, exist_ok=True)
            with open(os.path.join(feedback_dir, "反馈收集.txt"), "a", encoding="utf-8") as f:
                f.write(line + "\n")

        print("[FEEDBACK] %s -> %s" % (record['用户uid'], record['内容'][:40]), flush=True)
        return {"ok": True}
    except Exception as e:
        print(f"[FEEDBACK ERROR] {_mask(e, _API_TOKEN)}", flush=True)
        return {"ok": False, "error": "Internal server error"}


@app.get("/api/weather")
async def get_weather(request: Request):
    """实时查询天气数据（使用 open-meteo API）"""
    try:
        # 获取查询参数
        city = request.query_params.get('city')
        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        
        # 尝试从请求上下文中获取用户消息（如果可用）
        # 注意：在 GET 请求中，req.message 不可用，所以需要在调用时通过参数传递
        # 这里我们保持参数优先级：URL参数 > 用户消息 > 当前位置 > 默认上海
        
        # 如果提供了城市名称，先查询经纬度
        if city:
            # 使用 geocoding API 获取经纬度
            import httpx
            async with httpx.AsyncClient() as client:
                geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=zh&format=json"
                geo_response = await client.get(geo_url, timeout=10.0)
                
                if geo_response.status_code == 200:
                    geo_data = geo_response.json()
                    if geo_data.get('results'):
                        result = geo_data['results'][0]
                        lat = str(result.get('latitude'))
                        lon = str(result.get('longitude'))
                        city_name = result.get('name', city)
                        country = result.get('country', '')
                        location_display = f"{city_name}{f'({country})' if country else ''}"
                    else:
                        return {"ok": False, "error": "未找到该城市"}
                else:
                    return {"ok": False, "error": "地理位置查询失败"}
        
        # 如果没有提供经纬度，尝试从 agent 获取
        if not lat or not lon:
            if hasattr(agent, 'user_location') and agent.user_location:
                lat = str(agent.user_location.get('lat'))
                lon = str(agent.user_location.get('lon'))
        
        # 默认上海坐标
        if not lat or not lon:
            lat = '31.2304'
            lon = '121.4737'
            location_display = "上海"
        
        # 调用 open-meteo API 获取天气
        import httpx
        async with httpx.AsyncClient() as client:
            # 当前天气
            current_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code&timezone=Asia%2FShanghai&forecast_days=1"
            response = await client.get(current_url, timeout=10.0)
            
            if response.status_code != 200:
                return {"ok": False, "error": f"Weather API error: {response.status_code}"}
            
            data = response.json()
            
            # 解析天气代码
            weather_codes = {
                0: "晴天", 1: "晴间多云", 2: "多云", 3: "阴天",
                45: "雾", 48: "冻雾", 51: "毛毛雨", 53: "持续毛毛雨", 55: "浓密毛毛雨",
                56: "冻毛毛雨", 57: "持续冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
                66: "冻雨", 67: "持续冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
                77: "雪粒", 80: "小阵雨", 81: "中阵雨", 82: "大阵雨", 85: "小雪阵", 86: "大雪阵",
                95: "雷暴", 96: "雷暴伴小雨", 99: "雷暴伴冰雹"
            }
            
            current = data.get('current', {})
            weather_code = current.get('weather_code', 0)
            weather_desc = weather_codes.get(weather_code, "未知天气")
            
            # 构建响应
            result = {
                "ok": True,
                "location": location_display,
                "weather": weather_desc,
                "temperature": current.get('temperature_2m', 0),
                "humidity": current.get('relative_humidity_2m', 0),
                "wind_speed": current.get('wind_speed_10m', 0),
                "data_source": "open-meteo"
            }
            
            return result
            
    except Exception as e:
        print(f"[WEATHER ERROR] {_mask(e, _API_TOKEN)}", flush=True)
        return {"ok": False, "error": "Weather query failed"}


@app.post("/api/weather/query")
async def query_weather_by_message(req: Request):
    """根据用户消息查询天气（自动提取城市）"""
    try:
        data = await req.json()
        user_message = data.get('message', '')
        
        # 从用户消息中提取城市
        city = extract_city_from_message(user_message)
        
        # 如果提取到城市，使用城市查询
        if city:
            # 使用 GET 天气 API 的逻辑，但通过参数传递
            import httpx
            async with httpx.AsyncClient() as client:
                geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=zh&format=json"
                geo_response = await client.get(geo_url, timeout=10.0)
                
                if geo_response.status_code == 200:
                    geo_data = geo_response.json()
                    if geo_data.get('results'):
                        result = geo_data['results'][0]
                        lat = str(result.get('latitude'))
                        lon = str(result.get('longitude'))
                        city_name = result.get('name', city)
                        country = result.get('country', '')
                        location_display = f"{city_name}{f'({country})' if country else ''}"
                        
                        # 查询天气
                        current_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code&timezone=Asia%2FShanghai&forecast_days=1"
                        weather_response = await client.get(current_url, timeout=10.0)
                        
                        if weather_response.status_code == 200:
                            weather_data = weather_response.json()
                            
                            # 解析天气代码
                            weather_codes = {
                                0: "晴天", 1: "晴间多云", 2: "多云", 3: "阴天",
                                45: "雾", 48: "冻雾", 51: "毛毛雨", 53: "持续毛毛雨", 55: "浓密毛毛雨",
                                56: "冻毛毛雨", 57: "持续冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
                                66: "冻雨", 67: "持续冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
                                77: "雪粒", 80: "小阵雨", 81: "中阵雨", 82: "大阵雨", 85: "小雪阵", 86: "大雪阵",
                                95: "雷暴", 96: "雷暴伴小雨", 99: "雷暴伴冰雹"
                            }
                            
                            current = weather_data.get('current', {})
                            weather_code = current.get('weather_code', 0)
                            weather_desc = weather_codes.get(weather_code, "未知天气")
                            
                            # 构建响应
                            return {
                                "ok": True,
                                "location": location_display,
                                "weather": weather_desc,
                                "temperature": current.get('temperature_2m', 0),
                                "humidity": current.get('relative_humidity_2m', 0),
                                "wind_speed": current.get('wind_speed_10m', 0),
                                "data_source": "open-meteo"
                            }
                        else:
                            return {"ok": False, "error": "天气查询失败"}
                    else:
                        return {"ok": False, "error": "未找到该城市"}
                else:
                    return {"ok": False, "error": "地理位置查询失败"}
        
        # 如果没有提取到城市，使用当前位置
        if hasattr(agent, 'user_location') and agent.user_location:
            lat = str(agent.user_location.get('lat'))
            lon = str(agent.user_location.get('lon'))
            
            # 查询天气
            import httpx
            async with httpx.AsyncClient() as client:
                current_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code&timezone=Asia%2FShanghai&forecast_days=1"
                weather_response = await client.get(current_url, timeout=10.0)
                
                if weather_response.status_code == 200:
                    weather_data = weather_response.json()
                    
                    # 解析天气代码
                    weather_codes = {
                        0: "晴天", 1: "晴间多云", 2: "多云", 3: "阴天",
                        45: "雾", 48: "冻雾", 51: "毛毛雨", 53: "持续毛毛雨", 55: "浓密毛毛雨",
                        56: "冻毛毛雨", 57: "持续冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
                        66: "冻雨", 67: "持续冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
                        77: "雪粒", 80: "小阵雨", 81: "中阵雨", 82: "大阵雨", 85: "小雪阵", 86: "大雪阵",
                        95: "雷暴", 96: "雷暴伴小雨", 99: "雷暴伴冰雹"
                    }
                    
                    current = weather_data.get('current', {})
                    weather_code = current.get('weather_code', 0)
                    weather_desc = weather_codes.get(weather_code, "未知天气")
                    
                    # 构建响应
                    return {
                        "ok": True,
                        "location": "当前位置",
                        "weather": weather_desc,
                        "temperature": current.get('temperature_2m', 0),
                        "humidity": current.get('relative_humidity_2m', 0),
                        "wind_speed": current.get('wind_speed_10m', 0),
                        "data_source": "open-meteo"
                    }
        
        # 尝试获取当前位置（即使 agent.user_location 为空，也尝试浏览器定位）
        # 这里我们返回一个提示，让前端处理定位请求
        return {
            "ok": False,
            "error": "请允许浏览器定位权限以获取当前位置天气。"
        }
        
    except Exception as e:
        print(f"[WEATHER QUERY ERROR] {_mask(e, _API_TOKEN)}", flush=True)
        return {"ok": False, "error": "Weather query failed"}


# 辅助函数：从用户消息中提取城市名称
def extract_city_from_message(message: str) -> str:
    """从用户消息中提取城市名称"""
    if not message:
        return ""
    
    # 常见天气查询关键词
    weather_keywords = ['天气', '气温', '温度', '湿度', '风速', '预报', '明天', '后天', '今天']
    
    # 简单的城市匹配（中文城市名）
    # 这里使用一个简化的匹配逻辑，实际项目中可以使用更复杂的NLP
    cities = [
        '北京', '上海', '广州', '深圳', '杭州', '南京', '武汉', '成都', '重庆', '西安',
        '天津', '苏州', '宁波', '青岛', '厦门', '大连', '沈阳', '长春', '哈尔滨',
        '济南', '郑州', '长沙', '合肥', '南昌', '昆明', '贵阳', '南宁', '海口', '兰州',
        '西宁', '银川', '乌鲁木齐', '拉萨', '台北', '香港', '澳门'
    ]
    
    # 检查消息中是否包含城市名
    for city in cities:
        if city in message:
            return city
    
    # 检查是否有城市相关的关键词
    for keyword in weather_keywords:
        if keyword in message:
            # 如果有天气关键词但没有城市，返回空
            pass
    
    return ""


# API 配额/网络/token 等一切服务异常的友好提示（统一文案，不抛原始英文）
# 无论 服务器异常/网络异常/token不足/额度用尽 等，统一回复「服务器ojbk中」


# 服务器异常统一回复附带的 ojbk 表情包（sticker）。路径相对 KB_STATIC_DIR（=知识库），
# 实际文件位于 知识库/图片素材/表情包/ojbk.png（calc_flow 的「氪啊」表情包路径为 图片素材/表情包/氪啊.jpg，文件已存在）
# 图片素材/ 前缀属已有 bug，此处用真实路径确保前端 /kb/ 能加载）。
OJBK_STICKER = {"path": "图片素材/表情包/ojbk.png", "title": "ojbk"}


def _friendly_error_reply(e: Exception) -> str:
    """任何服务端异常（服务器/网络/token/额度等）→ 统一回复「服务器ojbk中」。"""
    return "服务器ojbk中。"


# ==================== 多会话隔离（接口多人调用） ====================
# 每个 session_id 独立 chat_history / session_overrides；缺省 "default" 兼容单用户
_session_store = {}


def _get_session(sid: Optional[str]):
    """按 session_id 取会话状态。"""
    key = (sid or '').strip() or 'default'
    s = _session_store.get(key)
    if s is None:
        s = {'chat_history': [], 'session_overrides': {}, 'chat_rounds': 0,
             'flirt_total': 0, 'flirt_standby': False}
        _session_store[key] = s
    return s


@app.get("/api/session")
def get_session(session_id: Optional[str] = None):
    """查看指定会话的临时记忆内容"""
    s = _get_session(session_id)
    return {"session_overrides": s['session_overrides']}


@app.get("/api/quota")
def api_quota(request: Request, session_id: Optional[str] = None):
    """查询当前**剩余百分比**（云端按客户端 IP，本地按 session_id）。
    不再信任客户端传 uid —— 云端从请求头/连接取真实 IP。

    只回百分比、不回具体 token（用户口径 2026-09-25）：与 /api/me 同一口径，
    避免用户从响应体反推出真实用量。运营侧要看具体量请走 /api/admin/usage。
    """
    from core.quota import get_client_key, usage_brief
    if runtime.quota_key() == 'ip':
        _key = get_client_key(request)
    else:
        _key = (session_id or '').strip() or 'local'
    return usage_brief(_key)


@app.get("/api/runtime")
def api_runtime():
    """当前运行模式与关键开关解析结果（管理后台/排障用；受邀请口令保护）。"""
    return runtime.summary()


@app.post("/api/admin/kb_sync")
def api_admin_kb_sync(force: bool = True):
    """手动触发知识库同步（上传新快照到对象存储后，无需重启即可拉取）。
    force=True 时即使 FX_KB_SYNC=off 也执行一次。"""
    from core.kb_sync import sync_kb
    try:
        return {'ok': True, 'result': sync_kb(force=force)}
    except Exception as e:
        # 异常细节只进服务端日志（已脱敏）；对外不回显 —— 云端尤其不能回显（用户口径 2026-09-24）
        print('[admin] 知识库同步失败：%s' % _mask(e, _API_TOKEN), flush=True)
        _d = '' if runtime.is_cloud() else '%s: %s' % (type(e).__name__, _mask(e, _API_TOKEN))
        return {'ok': False, 'error': '知识库同步失败', 'detail': _d}


# 节庆主题（用户口径 2026-09-25 → 2026-09-27 升级为数据驱动注册表）：
# 节日名 → 主题描述符（头像环色 / 气泡皮肤 / 飘落物 / 调色板）统一在
# tools/festival_themes._FESTIVAL_REGISTRY 登记；加节日只改那一处。
# 素材放 用户端特用/web/icons/festival/（该目录由 /icons/ 路由免鉴权 serve）。
def _festival_of_today():
    """今天若在法定节假日区间内，返回完整主题描述符；否则 None。

    节日判定与祝福文案走 core.calc_flow._today_holiday（联网日历 + 知识库兜底），
    主题外观（bubble/fall/palette）由 tools.festival_themes.resolve_festival 给出。
    """
    try:
        from tools.festival_themes import resolve_festival
        from core.calc_flow import _today_holiday
        h = _today_holiday() or {}
        name = str(h.get('name') or '').strip()
        if not name:
            return None
        fest = resolve_festival(name)
        if not fest:
            return None
        return {'name': name, 'greeting': str(h.get('greeting') or ''),
                'summary': str(h.get('summary') or ''), 'theme': fest['theme'],
                'assets': fest['assets'], 'palette': fest['palette']}
    except Exception:
        return None


@app.get("/api/ui_assets")
def get_ui_assets():
    """前端界面素材：表情差分默认头像 + 神态词列表（图片名即神态分类名）+ 今日节庆主题。

    节庆主题（用户口径 2026-09-25）：今天落在法定节假日区间内时下发 holiday，
    前端据此给头像套边框、气泡换皮肤、落节庆装饰；非节庆日 holiday=None，界面完全不变。
    """
    stems = [os.path.splitext(f)[0] for f in _list_expression_files()]
    # 表情差分 名字→URL 映射：供前端「双击头像随机切换表情」直接取用（排除逻辑在前端做）
    expr_urls = []
    for s in stems:
        u = _resolve_expression_url(s)
        if u:
            expr_urls.append({"name": s, "url": u})
    return {"default_avatar": _default_expression_url(), "expressions": stems,
            "expression_urls": expr_urls, "holiday": _festival_of_today()}


@app.post("/api/clear")
def clear_session(session_id: Optional[str] = None):
    """清除指定会话的全部临时记忆"""
    s = _get_session(session_id)
    s['session_overrides'].clear()
    return {"ok": True}


@app.post("/api/reload_prompt")
def reload_prompt():
    """热重载人设提示词（修改 system_prompt.md 后免重启 web 生效）。"""
    try:
        reload_system_prompt()
        return {"ok": True, "msg": "人设已重载"}
    except Exception as e:
        print('[admin] 人设重载失败：%s' % _mask(e, _API_TOKEN), flush=True)
        _d = '' if runtime.is_cloud() else '%s: %s' % (type(e).__name__, _mask(e, _API_TOKEN))
        return {"ok": False, "msg": "人设重载失败", "detail": _d}


# ==================== 用户本地副本（装备面板）管理 ====================
# 用户副本 user_data.json 是所有计算的用户面板真相源；查询走底层数据。
# 副本默认 = 底层 data.json 全量拷贝（首次自动生成）；仅本组接口/保存工具可修改。
from data import user_data as _user_data  # noqa: E402


class UserShipUpdateRequest(BaseModel):
    name: str
    atk_set: Optional[dict] = None   # 攻击面板绝对赋值（炮击/雷击/航空/...）
    def_set: Optional[dict] = None   # 防御面板绝对赋值（装甲/对空/机动/...）
    delta: Optional[dict] = None     # 面板增量（+N 在副本当前值上累加）
    # 「我的舰船·装备面板」的等级（用户口径 2026-10-03）：与面板一起保存，
    # 供遭遇战 PVE 等级压制使用；>0 生效，0/缺省=不设置（清除该字段）。
    level: Optional[float] = None


def _split_panel_delta(delta: dict) -> tuple:
    """把统一增量 dict 按攻击/防御字段归属拆分。"""
    from tools.common import _ATK_PANEL_FIELDS, _DEF_PANEL_FIELDS
    d_atk = {k: float(v) for k, v in (delta or {}).items() if k in _ATK_PANEL_FIELDS}
    d_def = {k: float(v) for k, v in (delta or {}).items() if k in _DEF_PANEL_FIELDS}
    return d_atk, d_def


def _canon_ship_name(name: str) -> str:
    """角色名归一化：走 app 名称匹配（吹雪改 → 吹雪·改），识别不到原样返回。"""
    name = (name or '').strip()
    if not name:
        return name
    try:
        ship = agent.app.get_ship_from_all(name)
        if ship is not None:
            return ship.name
    except Exception:
        pass
    return name


@app.get("/api/ship_panel")
def api_ship_panel(name: str):
    """取角色**底层**面板（data.json，不含用户副本）。
    云端模式下客户端本地缓存面板的基准值（base）来源；本地模式亦可复用。"""
    name = _canon_ship_name(name)
    ship = None
    try:
        ship = agent.app.get_ship_from_all(name)
    except Exception:
        ship = None
    if ship is None:
        return {'ship': None}
    return {'ship': {'name': ship.name,
                     'base_atk': dict(getattr(ship, 'attack_panel', {}) or {}),
                     'base_def': dict(getattr(ship, 'defense_panel', {}) or {})}}


@app.get("/api/ship_panels")
def api_ship_panels_batch():
    """**批量**取全部角色的底层基础面板（data.json，不含用户副本）。

    用途（用户口径 2026-10-03）：面板界面「一键载入全部基础面板」——
    一次请求把全部舰船的基础面板拉到本机面板库，之后可任选一艘直接编辑，
    不必逐个输入舰名，也避免 N 次单舰请求（单舰接口仍保留，做兜底/刷新用）。

    返回：{'count': N, 'ships': {角色名: {'attack_panel': {...}, 'defense_panel': {...}}}}
    数据源与 /api/ship_panel 同源（App.storage ← 底层 data.json），故两者恒一致。
    """
    ships = {}
    try:
        storage = getattr(agent.app, 'storage', None) or {}
    except Exception:
        storage = {}
    for name, obj in list(storage.items()):
        try:
            ships[str(name)] = {
                'attack_panel': dict(getattr(obj, 'attack_panel', {}) or {}),
                'defense_panel': dict(getattr(obj, 'defense_panel', {}) or {}),
            }
        except Exception:
            continue
    return {'count': len(ships), 'ships': ships}


@app.get("/api/user_ships")
def api_user_ships_list(name: Optional[str] = None):
    """列出用户副本中已修改面板的角色与相对底层差异；带 name 时返回该角色明细。
    云端模式（客户端面板）下不提供——面板存客户端本地。"""
    if runtime.panel_store() == 'client':
        raise HTTPException(409, '云端模式下面板保存在客户端本地，服务器无用户副本接口')
    if name:
        name = _canon_ship_name(name)
        dp = _user_data.diff_panels(name)
        return {'ship': {'name': name,
                         'base_atk': dp['base_atk'], 'base_def': dp['base_def'],
                         'cur_atk': dp['cur_atk'], 'cur_def': dp['cur_def'],
                         'delta': {**dp['delta_atk'], **dp['delta_def']},
                         # 「我的舰船」等级（可选）：与面板同存，供遭遇战 PVE 等级压制
                         'level': (_user_data.get_ship_dict(name) or {}).get('level')}}
    rows = []
    for n in sorted(_user_data.list_modified()):
        dp = _user_data.diff_panels(n)
        rows.append({'name': n,
                     'delta': {**dp['delta_atk'], **dp['delta_def']},
                     'cur_atk': dp['cur_atk'], 'cur_def': dp['cur_def'],
                     'level': (_user_data.get_ship_dict(n) or {}).get('level')})
    return {'ships': rows}


@app.post("/api/user_ships")
def api_user_ships_update(req: UserShipUpdateRequest):
    """更新用户副本面板：atk_set/def_set 绝对赋值，delta 增量累加。
    云端模式（客户端面板）下不提供——面板存客户端本地。"""
    if runtime.panel_store() == 'client':
        raise HTTPException(409, '云端模式下面板保存在客户端本地，请用面板界面保存')
    name = _canon_ship_name(req.name)
    if not name:
        raise HTTPException(400, '缺少角色名')
    d_atk, d_def = _split_panel_delta(req.delta)
    base = _user_data.get_ship_dict(name)
    if base is None:
        base = {'name': name, 'attack_panel': {}, 'defense_panel': {}, 'buffs': {}}
    if not _user_data.apply_panel_to(base, req.atk_set or {}, req.def_set or {},
                                     d_atk, d_def, level=req.level):
        raise HTTPException(500, '写入 user_data.json 失败')
    dp = _user_data.diff_panels(name)
    return {'ok': True, 'name': name, 'delta': {**dp['delta_atk'], **dp['delta_def']}}


@app.delete("/api/user_ships")
def api_user_ships_remove(name: str):
    """删除用户副本角色（恢复底层默认面板）。云端模式下不提供。"""
    if runtime.panel_store() == 'client':
        raise HTTPException(409, '云端模式下面板保存在客户端本地，请用面板界面删除')
    name = _canon_ship_name(name)
    if name not in _user_data.get_ships():
        return {'ok': False, 'msg': '副本中未修改过该角色，无需删除'}
    _user_data.remove_ship(name)
    return {'ok': True}


# ==================== 心跳与自动关闭 ====================
import threading as _threading
import signal as _signal

# 最后一次心跳时间（用于检测客户端是否在线）
_last_heartbeat = 0.0
_heartbeat_lock = _threading.Lock()
_server_should_shutdown = False
_shutdown_event = _threading.Event()


@app.get("/api/heartbeat")
def heartbeat():
    """前端定期调用，维持服务存活。返回当前服务器时间。

    免鉴权（见 _AUTH_PUBLIC_EXACT）：未解锁的登录页也会打心跳，带口令反而会被
    中间件判 401 —— 线上表现为每 30 秒一条 401 刷屏；心跳只回时间戳、无敏感数据，
    放行无越权风险。

    boot 只在**本地模式**下发（用户口径 2026-09-27）：它的用途是「服务重启后让上一轮
    遗留的页面自动失效」。本地单机重启＝用户自己重开，关掉旧页面合理；但云端重启是
    运维动作（改配置/发版），不该把正在用的测试用户踢下线（前端判 `data.boot &&` 后再
    自毁，空串即跳过），故云端返回空串。
    """
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.time()
    return {"ok": True, "server_time": time.time(),
            "boot": "" if runtime.is_cloud() else _BOOT_ID}


@app.post("/api/shutdown")
def shutdown():
    """前端关闭页面时调用，触发服务优雅关闭。
    仅当启用了 AKALI_AUTO_SHUTDOWN=1 时才生效；默认不响应关闭信号。"""
    _auto_shutdown = (os.environ.get('AKALI_AUTO_SHUTDOWN') or '').strip() in ('1', 'true', 'on', 'yes')
    if not _auto_shutdown:
        return {"ok": True, "msg": "自动关闭已禁用，服务保持运行"}
    global _server_should_shutdown
    _server_should_shutdown = True
    _shutdown_event.set()
    return {"ok": True, "msg": "服务即将关闭"}


def _pid_process_name(pid: int) -> str:
    """查询指定 PID 的进程映像名（tasklist），失败返回空串。仅 Windows 有效。"""
    if os.name != 'nt':
        return ''
    try:
        import subprocess
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, creationflags=0x08000000, timeout=5
        )
        line = (result.stdout or '').strip()
        if line and ',' in line:
            # CSV 首个字段即映像名，形如 "python.exe"
            return line.split(',')[0].strip('"')
    except Exception:
        pass
    return ''


def _take_over_stale_server(host: str, port: int) -> bool:
    """启动接管：若端口被上一次的阿卡莉服务（python 进程）占用则自动终止，返回是否已腾出端口。
    仅终止 python 映像的监听进程；其他程序占用时不做处理，由调用方走备用端口逻辑。
    旧服务退出后，遗留的旧页面会通过启动标识（boot id）心跳对比自动失效关闭。"""
    # 进程接管依赖 Windows 的 netstat / taskkill；Linux（云端容器）跳过
    if os.name != 'nt':
        return False
    pid = None
    try:
        import subprocess
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True, text=True, creationflags=0x08000000, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and f':{port}' in parts[1] and 'LISTENING' in line:
                try:
                    pid = int(parts[-1])
                except ValueError:
                    pass
                break
    except Exception:
        return False
    if not pid or pid == os.getpid():
        return False
    name = _pid_process_name(pid)
    if 'python' not in name.lower():
        return False
    try:
        import subprocess
        subprocess.run(
            ['taskkill', '/PID', str(pid), '/F'],
            capture_output=True, creationflags=0x08000000, timeout=5
        )
        print(f"[接管] 已终止上次的服务进程 PID={pid}（{name}）", flush=True)
        time.sleep(0.8)
        return True
    except Exception as e:
        print(f"[接管] 终止旧进程失败: {e}", flush=True)
        return False




if __name__ == "__main__":
    import threading
    import webbrowser
    import uvicorn
    import socket

    # 清空所有会话状态，确保每次重新运行都是全新的会话环境
    _session_store.clear()
    _session_token_total.clear()
    print("[启动] 已清空上次会话数据", flush=True)

    # 监听地址：由运行模式决定（local 默认 127.0.0.1 仅本机；cloud 默认 0.0.0.0）
    _host = runtime.host()
    _port = runtime.port()
    _url = f"http://{_host}:{_port}"
    _no_browser = (os.environ.get('AKALI_NO_BROWSER') or '').strip() in ('1', 'true', 'on', 'yes')

    # 检测端口是否被占用
    def _check_port(host, port):
        """检测端口是否被占用，返回占用进程的 PID 或 None"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            sock.close()
            return None
        except OSError:
            sock.close()
            # Windows 下用 netstat 查找占用进程（Linux/容器跳过，直接视为未知）
            if os.name != 'nt':
                return 'unknown'
            import subprocess
            try:
                result = subprocess.run(
                    ['netstat', '-ano'],
                    capture_output=True, text=True, creationflags=0x08000000
                )
                for line in result.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5 and f':{port}' in parts[1] and 'LISTENING' in line:
                        return int(parts[-1])
            except Exception:
                pass
            return 'unknown'  # 占用但无法获取 PID

    # 先尝试接管：端口被上次的阿卡莉服务（python）占用时自动终止旧进程
    if _check_port(_host, _port) is not None:
        _take_over_stale_server(_host, _port)

    _occupied = _check_port(_host, _port)
    if _occupied is not None:
        print(f"警告: 端口 {_port} 被其他程序占用（PID={_occupied}），无法接管。")
        print(f"请手动关闭该程序，或设置 PORT 环境变量使用其他端口。")
        # 尝试递增端口
        for _try_port in range(_port + 1, _port + 10):
            if _check_port(_host, _try_port) is None:
                _port = _try_port
                _url = f"http://{_host}:{_port}"
                print(f"自动切换到空闲端口: {_url}")
                break
        else:
            print(f"所有备用端口均被占用，请手动清理后重试。")
            exit(1)

    # 服务器启动后自动打开浏览器（延迟 1 秒等待服务就绪）
    if not _no_browser:
        def _open_browser():
            import time
            time.sleep(1)
            webbrowser.open(_url)

        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"阿卡莉智能体启动中...")
    print(f"运行模式: {runtime.run_mode()}  | 面板存储: {runtime.panel_store()}  | 限额维度: {runtime.quota_key()}")
    print(f"地址: {_url}")
    if runtime.is_cloud() and _host in ('127.0.0.1', 'localhost', '::1'):
        print("[警告] 云端模式但监听在回环地址：HOST 仍是 127.0.0.1，容器外将无法访问。"
              "请在 .env 设 HOST=0.0.0.0")
    if not _no_browser:
        print(f"浏览器将自动打开，如需禁用请设置环境变量 AKALI_NO_BROWSER=1")
    print(f"按 Ctrl+C 或关闭此窗口即可停止服务")

    # 启动 uvicorn
    import uvicorn
    _uv_kwargs = dict(host=_host, port=_port, log_level='info')
    # 云端经 Nginx 反代：信任 X-Forwarded-For / X-Real-IP（按 IP 限额依赖真实 IP）
    if runtime.proxy_headers():
        _uv_kwargs.update(proxy_headers=True, forwarded_allow_ips='*')
    # 热重载（用户口径 2026-09-25，可选）：设 AKALI_RELOAD=1 时改代码自动重载，
    # 连 restart 都不用 —— 用于服务器上快速迭代。**稳定运行请勿开启**：
    # 重载会中断正在处理的请求，且会多起一个监视进程。
    # 注：uvicorn 的 reload 要求 app 以「导入字符串」传入，不能用 app 对象。
    _reload = (os.environ.get('AKALI_RELOAD') or '').strip().lower() in ('1', 'true', 'on', 'yes')
    try:
        if _reload:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            print("[服务] 热重载已开启（AKALI_RELOAD=1）：改代码即生效，仅建议开发期使用", flush=True)
            uvicorn.run("web_app:app", reload=True,
                        reload_dirs=[os.path.dirname(os.path.abspath(__file__)),
                                     os.path.join(_root, '通用工具')],
                        **_uv_kwargs)
        else:
            uvicorn.run(app, **_uv_kwargs)
    except KeyboardInterrupt:
        print("\n[服务] 已停止")
    finally:
        _shutdown_event.set()

