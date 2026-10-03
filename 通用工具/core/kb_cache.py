# -*- coding: utf-8 -*-
"""知识库问答（from_kb）命中缓存 —— 用户口径 2026-10-03。

背景：攻略问答每次都要走「向量检索 → LLM 依据片段作答 → 证据校对」，
同一个问题（换个人再问 / 同一个人再问）都要完整重算一遍，重复烧 token。
本模块**只缓存知识库问答类**结果（calc_flow._handle_guide_qa 的返回，
即 from_kb 的攻略答复）：这类答复是「知识库原文直出」、与提问者无关，
缓存最安全；闲聊 / 调情 / 计算 / 查询 / 表单类一律不进缓存
（它们依赖会话上下文与人设，缓存会串味）。

缓存键 = 归一化(问题 + 玩家身份 + 实际检索 query) + 知识库版本
    —— 这三项正是 `_handle_guide_qa` 的全部入参，故命中即为等价重算；
    知识库版本取相关文件 mtime 指纹（30s 内不重复 stat），建库/改库后整体失效。

存储：进程内 LRU（与检索索引缓存同生命周期，重启即空）。

⚠️ 命中时**照常记账**（默认开）：按首次真实消耗的调用明细复现一遍，
   保证「额度百分比 / 用量统计」与"真的算过一遍"完全一致 ——
   否则命中缓存就等于给用户白送额度。开关见下方环境变量。

环境变量：
    AKALI_KB_CACHE=0            关闭本缓存（默认开）
    AKALI_KB_CACHE_TTL=86400    条目有效期（秒）
    AKALI_KB_CACHE_MAX=256      最多条目数（超出丢最旧）
    AKALI_KB_CACHE_CHARGE=0     命中时不复现记账（默认复现）
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import time

__all__ = ["enabled", "key", "get", "charge", "store", "snapshot_calls",
           "stats", "clear"]

_DEF_TTL = 86400.0
_DEF_MAX = 256


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == '':
        return default
    return str(v).strip().lower() not in ('0', 'false', 'off', 'no')


def _num(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name) or '').strip() or default)
    except Exception:
        return default


def enabled() -> bool:
    """总开关：AKALI_KB_CACHE=0 关闭（默认开）。"""
    return _flag('AKALI_KB_CACHE', True)


def _charge_on() -> bool:
    """命中是否复现记账：AKALI_KB_CACHE_CHARGE=0 关闭（默认开）。"""
    return _flag('AKALI_KB_CACHE_CHARGE', True)


def _ttl() -> float:
    return max(0.0, _num('AKALI_KB_CACHE_TTL', _DEF_TTL))


def _maxn() -> int:
    return max(1, int(_num('AKALI_KB_CACHE_MAX', _DEF_MAX)))


# ============================================================
# 知识库版本（mtime 指纹）：30s 内复用，避免每次请求都 stat
# ============================================================
_VER_LOCK = threading.Lock()
_VER = {'ts': 0.0, 'value': '0'}

_KB_PATHS = (
    # 注意：这里必须用**真实包名**（tools.*）—— 项目把「通用工具」目录本身加入了 sys.path，
    # 容器里 `通用工具` 并不是可导入的包；写成 `通用工具.tools.*` 会取不到路径、
    # 导致版本恒为 '0'（改库不失效，只剩 TTL 兜底）。本地/容器两边都只认 tools.*。
    ('tools.knowledge', '_GUIDE_QA_JSON'),
    ('tools.knowledge', '_GUIDE_QA_NPZ'),
    ('tools.common', 'GUIDE_KB_FILE'),
    ('tools.common', 'GUIDE_INDEX_FILE'),
    ('tools.common', 'AGENT_KB_FILE'),
)


def _fingerprint() -> str:
    parts = []
    for mod_name, attr in _KB_PATHS:
        try:
            mod = __import__(mod_name.replace('/', '.'), fromlist=[attr])
            path = getattr(mod, attr, None)
        except Exception:
            continue
        if not path:
            continue
        try:
            parts.append('%s:%s' % (os.path.basename(str(path)),
                                    os.path.getmtime(str(path))))
        except OSError:
            parts.append('%s:-' % os.path.basename(str(path)))
    if not parts:
        return '0'
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]


def kb_version() -> str:
    """知识库内容指纹（文件 mtime），改库后缓存自动整体失效。"""
    now = time.time()
    with _VER_LOCK:
        if _VER['value'] != '0' and (now - _VER['ts']) < 30.0:
            return _VER['value']
    value = _fingerprint()
    with _VER_LOCK:
        _VER['ts'] = now
        _VER['value'] = value
    return value


# ============================================================
# 键归一化
# ============================================================
_WS_RE = re.compile(r'\s+')


def _norm(text: str) -> str:
    """问题归一化：去空白、统一大小写、去掉尾部感叹/问号等标点。

    只做「同一句话的不同写法」归一（空格/大小写/句末标点），
    不做同义改写 —— 语义相近但字面不同的问题不共用缓存，
    避免把「某个问题」的答案答到「另一个问题」上。
    """
    t = _WS_RE.sub('', str(text or '')).strip()
    t = t.strip('。.！!？?~～、,，;；')
    return t.lower()


def key(msg: str, identity: str = '', search_query: str = '') -> str:
    """缓存键：问题 + 玩家身份 + 实际检索 query + 知识库版本。"""
    raw = '\x1f'.join((_norm(msg), _norm(identity), _norm(search_query), kb_version()))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


# ============================================================
# 存储（进程内 LRU）
# ============================================================
_LOCK = threading.Lock()
_STORE = {}          # key -> {'ts': float, 'result': dict, 'calls': dict}
_HITS = {'n': 0}
_MISSES = {'n': 0}
_STORED = {'n': 0}


def _prune_locked(now: float) -> None:
    ttl = _ttl()
    if ttl > 0:
        dead = [k for k, v in _STORE.items() if (now - float(v.get('ts') or 0)) > ttl]
        for k in dead:
            _STORE.pop(k, None)
    while len(_STORE) > _maxn():
        _STORE.pop(next(iter(_STORE)), None)   # dict 保序 → 丢最旧


def get(k: str):
    """取缓存条目（深拷贝由调用方决定）；未命中返回 None。命中/未命中都计入统计。"""
    if not enabled() or not k:
        return None
    now = time.time()
    with _LOCK:
        item = _STORE.get(k)
        if item is not None:
            ttl = _ttl()
            if ttl > 0 and (now - float(item.get('ts') or 0)) > ttl:
                _STORE.pop(k, None)
                item = None
        if item is None:
            _MISSES['n'] += 1
            return None
        _HITS['n'] += 1
        item['ts'] = now          # LRU：命中即续期
        return {'result': copy.deepcopy(item.get('result') or {}),
                'calls': copy.deepcopy(item.get('calls') or {}),
                'tok': int(item.get('tok') or 0)}


def charge(entry: dict) -> int:
    """命中时按首次真实消耗**复现记账**（保证额度口径与真算一遍一致）。

    记账对象 = 当前请求的配额键（core.req_ctx），与 llm_accounting 同一口径；
    无请求上下文（离线脚本）或关闭复现时直接跳过。
    """
    if not entry or not _charge_on():
        return 0
    calls = entry.get('calls') or {}
    if not calls:
        return 0
    try:
        from core import req_ctx, quota
        qkey = req_ctx.get_quota_key()
        if not qkey:
            return 0
        return int(quota.replay_llm_calls(qkey, calls) or 0)
    except Exception:
        return 0


def snapshot_calls() -> dict:
    """当前请求配额键的 calls 明细快照（供计算后做差得到本轮真实消耗）。"""
    try:
        from core import req_ctx, quota
        qkey = req_ctx.get_quota_key()
        if not qkey:
            return {}
        return quota.entry_calls(qkey)
    except Exception:
        return {}


def _delta(base: dict, now: dict) -> dict:
    """b - a（按 kind 聚合的 n / tok_in / tok_out）。"""
    out = {}
    for kind, cur in (now or {}).items():
        old = (base or {}).get(kind) or {}
        n = int(cur.get('n') or 0) - int(old.get('n') or 0)
        tin = int(cur.get('tok_in') or 0) - int(old.get('tok_in') or 0)
        tout = int(cur.get('tok_out') or 0) - int(old.get('tok_out') or 0)
        if n > 0 or tin > 0 or tout > 0:
            out[str(kind)] = {'n': max(0, n), 'tok_in': max(0, tin), 'tok_out': max(0, tout)}
    return out


def store(k: str, result, base_calls: dict = None) -> bool:
    """写入缓存。

    - 只收**知识库问答类**成功结果（from_kb 或 ok 的攻略答复）：其余一律不缓存；
    - base_calls：进入计算前的 calls 快照（由 snapshot_calls() 得到），
      用于算出本轮真实消耗，命中时按它复现记账。
    """
    if not enabled() or not k or not isinstance(result, dict):
        return False
    if result.get('no_history') or result.get('fill_form') or result.get('encounter_form'):
        return False                      # 表单类/违规轮：不缓存
    if not (result.get('from_kb') or result.get('ok')):
        return False
    body = copy.deepcopy(result)
    body.pop('_tok', None)                # 出口打点字段不入缓存
    body.pop('expression', None)          # 表情按当前轮重算，缓存会串味
    body.pop('sticker', None)
    body.pop('expression_hint', None)
    calls = _delta(base_calls or {}, snapshot_calls())
    tok = sum(int(v.get('tok_in') or 0) + int(v.get('tok_out') or 0) for v in calls.values())
    now = time.time()
    with _LOCK:
        _STORE[k] = {'ts': now, 'result': body, 'calls': calls, 'tok': tok}
        _STORED['n'] += 1
        _prune_locked(now)
    return True


def clear() -> int:
    """清空缓存，返回清掉的条目数（缓存改库/排障用）。"""
    with _LOCK:
        n = len(_STORE)
        _STORE.clear()
    return n


def stats() -> dict:
    """运行统计（供开发者控制台展示）。"""
    hits = int(_HITS['n'])
    miss = int(_MISSES['n'])
    total = hits + miss
    return {
        'enabled': bool(enabled()),
        'charge': bool(_charge_on()),
        'size': len(_STORE),
        'max': _maxn(),
        'ttl': int(_ttl()),
        'hits': hits,
        'misses': miss,
        'stored': int(_STORED['n']),
        'hit_rate': round(hits / total, 3) if total else 0.0,
        'kb_version': kb_version(),
    }
