# -*- coding: utf-8 -*-
"""
多用户算力配额模块（阿卡莉 1.0 / 拂晓助手）
================================================================

设计目标（2026-09-15 改版：无账号 · 按 IP 限额）
- 云端部署下**没有账号体系**，用户自由使用（仅需全局邀请口令准入）。
- 因此限额以**客户端 IP** 为维度：同一 IP 每日 LLM token 消耗总量有上限。
- 超限额：停用 LLM 闲聊（返回固定提示），但**基础工具（查询/计算/攻略/遭遇战）
  不受影响**、正常工作；次日零点（本地日期）自动恢复。

限额口径：
    每日 token 总量（输入 + 输出）/ IP，默认 60000（env FX_IP_DAILY_TOK 可调）。
    计费换算仍照 Qwen 定价给出参考成本（仅展示，不参与闸控）：
        输入 0.0024 元/千 token；输出 0.0144 元/千 token

运行模式（见 core/runtime.py）：
    AKALI_RUN_MODE=local（默认） → 不限额（仅内存统计，行为同改造前）
    AKALI_RUN_MODE=cloud         → 启用按 IP 限额

限额维度与白名单：
    AKALI_QUOTA_KEY=ip|session   （默认：cloud→ip，local→session）
    FX_USER_PLAN / FX_USER_PLAN_MAP 仍保留：可给特定 IP 更高的额度档
        free  → FX_IP_DAILY_TOK        （默认 60000）
        month → FX_IP_DAILY_TOK_MONTH  （默认 300000）

配额存储后端（可插拔，FX_QUOTA_BACKEND）：
    memory  → 纯内存，重启清零（默认；本地个人/单机够用）
    local   → FX_QUOTA_DIR 指定目录落盘 JSON（单机持久）
    redis   → FX_REDIS_URL 指定的 Redis（云端多实例共享、跨重启不丢）
    云端未显式指定时默认尝试 redis，连不上自动降级 memory 并告警。
"""
import os
import json
import threading
from datetime import date
from typing import Optional

from core import runtime

# ===== 价格（qwen 实测，仅用于展示参考成本）=====
PRICE_IN_YUAN_PER_K = 0.0024   # 元 / 千 token（输入）
PRICE_OUT_YUAN_PER_K = 0.0144  # 元 / 千 token（输出）

# ===== 限额（token 总量 / 天 / 维度键）=====
_TOK_LIMIT_FREE = runtime.quota_daily_tok()          # 默认 60000
try:
    _TOK_LIMIT_MONTH = max(0, int(os.getenv('FX_IP_DAILY_TOK_MONTH') or '300000'))
except (TypeError, ValueError):
    _TOK_LIMIT_MONTH = 300000

# 本地模拟用（云端部署时由 get_user_plan 接白名单/订阅系统）
_DEFAULT_PLAN = os.getenv('FX_USER_PLAN', 'free').strip().lower()
try:
    _PLAN_MAP = json.loads(os.getenv('FX_USER_PLAN_MAP', '{}') or '{}')
except Exception:
    _PLAN_MAP = {}
_QUOTA_DIR = os.getenv('FX_QUOTA_DIR', '').strip()
_BACKEND = os.getenv('FX_QUOTA_BACKEND', '').strip().lower()

_lock = threading.Lock()


def _today() -> str:
    return date.today().isoformat()


# ===== 配额存储后端（可插拔）=====
class QuotaStore:
    """key = "date|client_key" -> {'rounds','tok_in','tok_out','cost'}。子类决定持久化方式。"""
    def __init__(self):
        self._lock = threading.Lock()
        self.usage: dict = {}

    def get_entry(self, key: str) -> dict:
        with self._lock:
            e = self.usage.get(key)
            if e is None:
                e = {'rounds': 0, 'tok_in': 0, 'tok_out': 0, 'cost': 0.0}
                self.usage[key] = e
            return e

    def persist(self):
        pass

    def load(self):
        pass


class MemoryStore(QuotaStore):
    """纯内存，重启清零（本地个人 / 单机默认）。"""
    pass


class LocalFileStore(QuotaStore):
    """落盘 JSON 到 FX_QUOTA_DIR（单机持久）。"""
    def __init__(self, d: str):
        super().__init__()
        self._dir = d

    def _path(self) -> str:
        return os.path.join(self._dir, 'quota_usage.json')

    def load(self):
        p = self._path()
        if not os.path.isfile(p):
            return
        try:
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
            with self._lock:
                self.usage.update(data)
        except Exception:
            pass

    def persist(self):
        try:
            os.makedirs(self._dir, exist_ok=True)
            with self._lock:
                data = dict(self.usage)
            with open(os.path.join(self._dir, 'quota_usage.json'), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass


class RedisStore(QuotaStore):
    """Redis 持久（云端多实例共享、跨重启不丢）。全量 JSON set，按日粒度量小、可接受。"""
    def __init__(self, url: str):
        import redis
        self._r = redis.from_url(url, decode_responses=True)
        self._key = 'fx:quota:usage'
        super().__init__()
        self.load()

    def load(self):
        try:
            raw = self._r.get(self._key)
            if raw:
                with self._lock:
                    self.usage.update(json.loads(raw))
        except Exception:
            pass

    def persist(self):
        try:
            with self._lock:
                data = dict(self.usage)
            self._r.set(self._key, json.dumps(data, ensure_ascii=False))
        except Exception:
            pass


def _build_store() -> QuotaStore:
    """按 FX_QUOTA_BACKEND 选择后端；auto/空 时：cloud→redis，local→memory。
    redis 不可用则依次降级：本地文件（配了 FX_QUOTA_DIR）→ 内存。"""
    backend = _BACKEND
    if backend in ('', 'auto'):
        backend = 'redis' if runtime.quota_enabled() else 'memory'
    if backend == 'redis':
        try:
            return RedisStore(os.getenv('FX_REDIS_URL', 'redis://localhost:6379/0'))
        except Exception as e:
            import sys
            print('[quota] Redis 不可用，降级后备存储（配额不跨实例共享）: %s' % e, file=sys.stderr)
            backend = 'local' if _QUOTA_DIR else 'memory'
    if backend == 'local' and _QUOTA_DIR:
        st = LocalFileStore(_QUOTA_DIR)
        st.load()
        return st
    return MemoryStore()


STORE = _build_store()


# ===== 客户端标识 =====
def _client_ip(req) -> str:
    """取真实客户端 IP：反代头优先（X-Forwarded-For 首段 → X-Real-IP），
    再退 request.client.host。反代须透传 X-Forwarded-For（见 deploy/nginx.conf）。"""
    headers = getattr(req, 'headers', None)
    if headers is not None:
        fwd = None
        try:
            fwd = headers.get('x-forwarded-for')
        except Exception:
            fwd = None
        if fwd:
            ip = str(fwd).split(',')[0].strip()
            if ip:
                return ip
        try:
            xri = headers.get('x-real-ip')
        except Exception:
            xri = None
        if xri and str(xri).strip():
            return str(xri).strip()
    client = getattr(req, 'client', None)
    host = getattr(client, 'host', None) if client is not None else None
    return str(host).strip() if host else 'unknown'


# ============================================================================
# 身份解析 —— 限额维度的唯一入口（后续接 uid 账号体系的扩展点）
# ----------------------------------------------------------------------------
# 现状：云端按客户端 IP、本地按 session_id（维度由 runtime.quota_key() 决定）。
# 将来改「uid 注册 + 内部数据库」时，**只需改本文件两个函数**：
#   ① resolve_identity(req)：改为从凭证解析 uid（查库校验）
#   ② get_user_plan(key)：改为按 uid 查库取套餐（在 _lookup_plan_from_db 里实现）
# 其余（check_llm_quota / record_llm_usage / 存储后端 / 各调用方）都无需改动。
# ============================================================================
def resolve_identity(req) -> str:
    """把一次请求解析成「限额维度键」。

    接入账号体系后（用户口径 2026-09-25）：**优先用鉴权中间件解析出的 uid** —— 这样
    每个用户各算各的额度，不再按出口 IP 混在一起（同一局域网多人共用一个出口 IP 时，
    按 IP 计会让彼此互相吃额度）。

    未启用账号（或本地回环调试、未配口令）时回退：云端 = 客户端 IP（XFF 优先）；
    本地 = session_id（仅统计、不闸控）。行为与接入前一致。"""
    uid = str(getattr(getattr(req, 'state', None), 'uid', '') or '').strip()
    if uid:
        return uid
    if runtime.quota_key() == 'ip':
        return _client_ip(req)
    sid = getattr(req, 'session_id', None)
    return str(sid).strip() if sid and str(sid).strip() else 'local'


def get_client_key(req) -> str:
    """兼容旧调用：等同 resolve_identity(req)。"""
    return resolve_identity(req)


# 兼容旧调用名（老代码 import get_user_id；注意语义已改为 IP/会话，不再读 uid）
def get_user_id(req) -> str:
    return resolve_identity(req)


def _lookup_plan_from_db(key: str) -> str:
    """uid → 套餐。

    账号体系下（用户口径 2026-09-25）：**开发者本人（AKALI_OWNER_UID）视为 month（不限量）**，
    测试用户默认 free；其余（IP/session 维度）返回空串，走原环境变量白名单
    （FX_USER_PLAN_MAP / FX_USER_PLAN），保持向后兼容。
    """
    try:
        from core import users as _users
        if key and key == _users.owner_uid():
            return 'month'
        accts = {u['uid'] for u in _users.parse_users()}
        if key in accts:
            return 'free'
    except Exception:
        pass
    return ''


def get_user_plan(key: str) -> str:
    """返回 'free' 或 'month'（额度档）。

    顺序：数据库（占位未接入）→ FX_USER_PLAN_MAP（键为 IP/session）→ FX_USER_PLAN → free。
    签名（key → 套餐）保持不变，故调用方无需随账号体系升级而改动。"""
    plan = _lookup_plan_from_db(key)
    if plan in ('free', 'month'):
        return plan
    if key in _PLAN_MAP:
        return _PLAN_MAP[key]
    return _DEFAULT_PLAN if _DEFAULT_PLAN in ('free', 'month') else 'free'


def _tok_limit(plan: str) -> int:
    return _TOK_LIMIT_MONTH if plan == 'month' else _TOK_LIMIT_FREE


def _get_entry(key: str) -> dict:
    return STORE.get_entry('%s|%s' % (_today(), key))


def check_llm_quota(key: str, plan: str = None) -> dict:
    """返回该维度键今日是否还能调用 LLM。
    返回 {'allowed','reason','rounds','tok','tok_limit','cost','plan'}
    非云端模式（本地个人）恒 allowed=True（统计仍照常）。"""
    if plan is None:
        plan = get_user_plan(key)
    if not runtime.quota_enabled():
        return {'allowed': True, 'reason': 'local', 'rounds': 0,
                'tok': 0, 'tok_limit': 0, 'cost': 0.0, 'plan': plan}
    e = _get_entry(key)
    limit = _tok_limit(plan)
    used = int(e['tok_in']) + int(e['tok_out'])
    over = limit > 0 and used >= limit
    return {'allowed': not over, 'reason': 'quota_tok' if over else 'ok',
            'rounds': e['rounds'], 'tok': used, 'tok_limit': limit,
            'cost': round(e['cost'], 4), 'plan': plan}


def record_llm_usage(key: str, plan: str, tok_in: int, tok_out: int):
    """记录一轮 LLM 消耗的 token 与金额（按日累计，次日自动清零）。
    本地模式也累加内存统计（便于观察），不落盘；是否真正 gate 由 check_llm_quota 决定。"""
    with _lock:
        e = _get_entry(key)
        e['rounds'] += 1
        e['tok_in'] += int(tok_in)
        e['tok_out'] += int(tok_out)
        e['cost'] += tok_in / 1000.0 * PRICE_IN_YUAN_PER_K + tok_out / 1000.0 * PRICE_OUT_YUAN_PER_K
    STORE.persist()


def quota_prompt(key: str, plan: str, info: dict) -> str:
    """额度用尽时的固定提示文案（不调 LLM）。基础功能仍可正常使用。"""
    used = info.get('tok', 0) / 1000.0
    limit = info.get('tok_limit', 0) / 1000.0
    return ('今日算力已用尽（%.1fk / %.1fk token，同一网络/设备共享）。'
            '基础功能（查询 / 计算 / 攻略）仍可使用，明日零点自动恢复。' % (used, limit))


def quota_status(key: str, plan: str = None) -> dict:
    """供前端查询当前剩余额度（GET /api/quota）。"""
    if plan is None:
        plan = get_user_plan(key)
    if not runtime.quota_enabled():
        return {'cloud_mode': False, 'plan': plan, 'unlimited': True}
    e = _get_entry(key)
    limit = _tok_limit(plan)
    used = int(e['tok_in']) + int(e['tok_out'])
    return {'cloud_mode': True, 'plan': plan, 'dim': runtime.quota_key(),
            'rounds': e['rounds'], 'tok': used, 'tok_limit': limit,
            'remain_tok': max(0, limit - used),
            'cost': round(e['cost'], 4)}
