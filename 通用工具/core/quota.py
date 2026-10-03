# -*- coding: utf-8 -*-
"""
多用户算力配额模块（阿卡莉 1.0 / 拂晓助手）
================================================================

设计目标（2026-09-15 改版：无账号 · 按 IP 限额）
- 云端部署下**没有账号体系**，用户自由使用（仅需全局邀请口令准入）。
- 因此限额以**客户端 IP** 为维度：同一 IP **每个周期**的 LLM token 消耗总量有上限。
- 周期口径（用户口径 2026-10-03）：**每周刷新，周四 00:00 → 下周四 00:00**（本地时间）。
- 超限额：停用 LLM 闲聊（返回固定提示），但**基础工具（查询/计算/攻略/遭遇战）
  不受影响**、正常工作；到下周四零点自动恢复。

限额口径：
    每个周期（周四 → 下周四）token 总量（输入 + 输出）/ IP。各档额度直接读 env（0 = 不限量，可随意设置）：
        developer（开发者本人）→ 恒不限量
        whitelist（白名单）    → FX_QUOTA_WHITELIST（默认 300000＝30w，类似开发者但可调限量）
        paid（付费用户）       → FX_QUOTA_PAID（默认 300000＝30w）
        free（免费用户）       → FX_QUOTA_FREE（默认 300000＝30w）
    计费换算仍照 Qwen 定价给出参考成本（仅展示，不参与闸控）：
        输入 0.0024 元/千 token；输出 0.0144 元/千 token

运行模式（见 core/runtime.py）：
    AKALI_RUN_MODE=local         → 不限额（仅内存统计，行为同改造前）
    AKALI_RUN_MODE=cloud（默认） → 启用按 IP 限额

限额维度与白名单：
    AKALI_QUOTA_KEY=ip|session   （默认：cloud→ip，local→session）
    FX_QUOTA_WHITELIST_KEYS：逗号分隔的 IP/uid 白名单，名单内走 whitelist 额度（类似开发者，独立可调，
    不参与 free/paid 检测）；其余用户按 users.json 的 plan 字段分 free / paid 两档。

配额存储后端（可插拔，FX_QUOTA_BACKEND）：
    memory  → 纯内存，重启清零（默认；本地个人/单机够用）
    local   → FX_QUOTA_DIR 指定目录落盘 JSON（单机持久）
    redis   → FX_REDIS_URL 指定的 Redis（云端多实例共享、跨重启不丢）
    云端未显式指定时默认尝试 redis，连不上自动降级 memory 并告警。
"""
import os
import json
import threading
from datetime import date, datetime, timedelta
from typing import Optional

from core import runtime

# ===== 价格（qwen 实测，仅用于展示参考成本）=====
PRICE_IN_YUAN_PER_K = 0.0024   # 元 / 千 token（输入）
PRICE_OUT_YUAN_PER_K = 0.0144  # 元 / 千 token（输出）

# ===== 限额（token 总量 / 天 / 维度键）=====
# 四档额度（0 = 不限量，可随意设置）：
#   developer（开发者本人）→ 恒不限量（无需配置）
#   whitelist（白名单）    → FX_QUOTA_WHITELIST（类似开发者，独立可调限量）
#   paid（付费用户）       → FX_QUOTA_PAID
#   free（免费用户）       → FX_QUOTA_FREE
#
# ⚠️ 这些上限**每次实时读宿主机 .env 文件**（runtime.live_env）：改完立即生效，
#    不需要重启容器、也不需要重建。百分比（usage_brief）与闸控（check_llm_quota）
#    都汇到 _tok_limit() 取值，所以「上限 / 剩余百分比 / 是否超额」永远一致。
def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v in (None, ''):
        return default
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def _env_int_live(name: str, default: int) -> int:
    """额度上限：实时从 .env 取值（改文件即生效）→ 回落进程环境 → 回落默认值。"""
    v = runtime.live_env(name)
    if v in (None, ''):
        v = os.getenv(name)
    if v in (None, ''):
        return default
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def quota_free() -> int:
    """免费档每周期（周四→下周四）token 上限（实时；默认 300000＝30w；0 = 不限量）。"""
    return _env_int_live('FX_QUOTA_FREE', 300000)


def quota_paid() -> int:
    """付费档每周期 token 上限（实时；默认 300000＝30w；0 = 不限量）。"""
    return _env_int_live('FX_QUOTA_PAID', 300000)


def quota_whitelist() -> int:
    """白名单每周期 token 上限（实时；默认 300000＝30w；0 = 不限量）。"""
    return _env_int_live('FX_QUOTA_WHITELIST', 300000)


def whitelist_keys() -> set:
    """白名单集合（IP/uid，逗号分隔；**实时读 .env**，改完立即生效）。"""
    raw = runtime.live_env('FX_QUOTA_WHITELIST_KEYS') or (os.getenv('FX_QUOTA_WHITELIST_KEYS') or '')
    return {k.strip() for k in raw.split(',') if k.strip()}


_QUOTA_DIR = os.getenv('FX_QUOTA_DIR', '').strip()
_BACKEND = os.getenv('FX_QUOTA_BACKEND', '').strip().lower()

_lock = threading.Lock()


def _period() -> str:
    """当前算力周期的**起始日**（用户口径 2026-10-03：每周刷新，周四 → 下周四）。

    取「最近一个周四（含当天）」：周四→0 天、周五→1 天 … 周三→6 天。
    返回 ISO 日期串，用作配额存储的分桶键（key = "<周期起始日>|<维度键>"）；
    到下周四零点自动进入下一个周期（旧周期的数据自然不再被读取）。
    """
    d = date.today()
    return (d - timedelta(days=(d.weekday() - 3) % 7)).isoformat()


# ===== 配额存储后端（可插拔）=====
class QuotaStore:
    """key = "date|client_key" -> {'rounds','tok_in','tok_out','cost'}。子类决定持久化方式。
    reqlog = date -> [{'date','ts','key','tok'}...]：请求级流水，按天分桶持久化，可回看历史日。"""
    def __init__(self):
        self._lock = threading.Lock()
        self.usage: dict = {}
        self.reqlog: dict = {}

    def get_entry(self, key: str) -> dict:
        with self._lock:
            e = self.usage.get(key)
            if e is None:
                e = {'rounds': 0, 'tok_in': 0, 'tok_out': 0, 'cost': 0.0}
                self.usage[key] = e
            return e

    def log_request(self, key: str, tok: int):
        """把一次 /api/chat 请求记进本周期的流水桶；每周期最多保留 _REQLOG_MAX_PER_DAY 条。"""
        day = _period()
        entry = {'date': day, 'ts': datetime.now().strftime('%H:%M:%S'),
                 'key': str(key or ''), 'tok': int(tok or 0)}
        with self._lock:
            lst = self.reqlog.setdefault(day, [])
            lst.append(entry)
            if len(lst) > _REQLOG_MAX_PER_DAY:
                del lst[:-_REQLOG_MAX_PER_DAY]

    def recent_requests(self, day: str, limit: int = 100) -> list:
        with self._lock:
            items = list(self.reqlog.get(day or _period(), []))
        return items[-max(1, int(limit)):]

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
        rp = os.path.join(self._dir, 'quota_reqlog.json')
        if os.path.isfile(rp):
            try:
                with open(rp, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    with self._lock:
                        self.reqlog.update(data)
            except Exception:
                pass

    def persist(self):
        try:
            os.makedirs(self._dir, exist_ok=True)
            with self._lock:
                usage = dict(self.usage)
                reqlog = {d: list(v) for d, v in self.reqlog.items()}
            with open(os.path.join(self._dir, 'quota_usage.json'), 'w', encoding='utf-8') as f:
                json.dump(usage, f, ensure_ascii=False)
            with open(os.path.join(self._dir, 'quota_reqlog.json'), 'w', encoding='utf-8') as f:
                json.dump(reqlog, f, ensure_ascii=False)
        except Exception:
            pass


class RedisStore(QuotaStore):
    """Redis 持久（云端多实例共享、跨重启不丢）。全量 JSON set，按日粒度量小、可接受。"""
    def __init__(self, url: str):
        import redis
        self._r = redis.from_url(url, decode_responses=True)
        self._key = 'fx:quota:usage'
        self._reqlog_key = 'fx:quota:reqlog'
        super().__init__()
        self.load()

    def load(self):
        try:
            raw = self._r.get(self._key)
            if raw:
                with self._lock:
                    self.usage.update(json.loads(raw))
            rraw = self._r.get(self._reqlog_key)
            if rraw:
                with self._lock:
                    self.reqlog.update(json.loads(rraw))
        except Exception:
            pass

    def persist(self):
        try:
            with self._lock:
                usage = dict(self.usage)
                reqlog = {d: list(v) for d, v in self.reqlog.items()}
            self._r.set(self._key, json.dumps(usage, ensure_ascii=False))
            self._r.set(self._reqlog_key, json.dumps(reqlog, ensure_ascii=False))
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


def daily_limit(key: str) -> int:
    """某维度键的每周期 token 上限（按套餐档）；**0 = 不限量**（开发者本人）。"""
    return _tok_limit(get_user_plan(key))


# ===== 请求级用量明细（按天分桶持久化，可回看任意历史日；日汇总仍以 usage 记账为准）=====
# 每请求记一条到 STORE.reqlog[date]，随用量一起落盘/入 Redis，故历史日也能查流水。
_REQLOG_MAX_PER_DAY = 500    # 每天最多保留条数，避免无界增长


def log_request(key: str, tok: int):
    """记录一次 /api/chat 请求的 token 消耗（tok=0 = 本请求未走 LLM，如攻略/计算）。按天持久化。"""
    STORE.log_request(str(key or ''), int(tok or 0))
    # 每次请求都落盘/入 Redis，保证非 LLM 请求（攻略/计算）的流水也不丢
    try:
        STORE.persist()
    except Exception:
        pass


def recent_requests(day: Optional[str] = None, limit: int = 100) -> list:
    """某天的请求级流水（持久化保留，可回看历史日）。"""
    return STORE.recent_requests((day or _period()).strip(), limit)


def usage_snapshot(day: Optional[str] = None) -> list:
    """导出某天（默认今天）各维度键的用量 —— 供**开发者侧**查看谁在用（用户口径 2026-09-25）。

    量级说明：配额存储本身就是「全量 JSON」（Redis 后端也只是整串 set/get），
    所以这里直接读内存快照即可，不产生额外查询。返回按 token 总量倒序的列表。
    """
    d = (day or _period()).strip()
    try:
        with STORE._lock:
            items = list(STORE.usage.items())
    except Exception:
        items = []
    rows = []
    for key, e in items:
        if not isinstance(key, str) or '|' not in key:
            continue
        kday, dim = key.split('|', 1)
        if kday != d:
            continue
        tin = int(e.get('tok_in') or 0)
        tout = int(e.get('tok_out') or 0)
        calls = e.get('calls') or {}
        rows.append({
            'key': dim,
            'rounds': int(e.get('rounds') or 0),
            'tok_in': tin,
            'tok_out': tout,
            'tok_total': tin + tout,
            'cost': round(float(e.get('cost') or 0), 4),
            # 调用类型细则（闲聊/攻略生成/…）：控制台点开才显示，默认不下发到用户侧
            'calls': {k: dict(v) for k, v in calls.items() if isinstance(v, dict)},
            'plan': get_user_plan(dim),
            'limit': daily_limit(dim),
        })
    rows.sort(key=lambda x: -x['tok_total'])
    return rows


def usage_of(key: str, day: Optional[str] = None) -> dict:
    """单个维度键在某天的用量（**含绝对数值**）。

    ⚠️ 不要用它直接回给**用户侧**接口（用户口径 2026-09-25：用户只能看到剩余百分比）。
    用户侧请用 `usage_brief()`；本函数留给开发者侧/排障使用。
    """
    d = (day or _period()).strip()
    try:
        with STORE._lock:
            e = STORE.usage.get('%s|%s' % (d, key)) or {}
    except Exception:
        e = {}
    tin = int(e.get('tok_in') or 0)
    tout = int(e.get('tok_out') or 0)
    return {'day': d, 'rounds': int(e.get('rounds') or 0), 'tok_in': tin, 'tok_out': tout,
            'tok_total': tin + tout, 'cost': round(float(e.get('cost') or 0), 4),
            'plan': get_user_plan(key), 'limit': daily_limit(key)}


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
#   ② get_user_plan(key)：按 uid 查库取套餐；白名单走 FX_QUOTA_WHITELIST_KEYS，其余按 users.json 的 plan 分 free/paid
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
    if not uid:
        # ChatRequest 是 Pydantic 模型、没有 .state；/api/chat 路由会把中间件解析出的
        # uid 透传到它的 uid 字段上（见 web_app.chat）。
        uid = str(getattr(req, 'uid', None) or '').strip()
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


def get_user_plan(key: str) -> str:
    """返回额度档：'developer' / 'whitelist' / 'paid' / 'free'。

    - developer：开发者本人（AKALI_OWNER_UID）或 plan=unlimited，恒不限量；
    - whitelist：FX_QUOTA_WHITELIST_KEYS 名单内，额度 = FX_QUOTA_WHITELIST
      （类似开发者，显式成员、不参与 free/paid 检测，但限量可调）；
    - 其余用户按 users.json 的 plan 字段（大小写不敏感）：
      paid / month → FX_QUOTA_PAID，unlimited → 不限量，free/未知/留空 → FX_QUOTA_FREE
      （均可在 .env 随意设置，0=不限量）。
    """
    try:
        from core import users as _users
        if key and key == _users.owner_uid():
            return 'developer'
        if key in whitelist_keys():
            return 'whitelist'
        for u in _users.parse_users():
            if u['uid'] == key:
                p = str(u.get('plan') or '').strip().lower()
                if p in ('paid', 'month'):     # month 为旧名，等同付费档
                    return 'paid'
                if p == 'unlimited':           # 不限量
                    return 'developer'
                if p == 'free':
                    return 'free'
    except Exception:
        pass
    return 'free'


def _tok_limit(plan: str) -> int:
    """每周期 token 上限；**0 表示不限量**（check_llm_quota 里以 limit > 0 判闸）。

    **实时读 .env**：改了上限立即生效。百分比（usage_brief）与闸控（check_llm_quota）
    都取用本函数，故「上限 / 剩余百分比 / 是否超额」三者始终一致。
    """
    if plan == 'developer':
        return 0
    if plan == 'whitelist':
        return quota_whitelist()
    if plan == 'paid':
        return quota_paid()
    return quota_free()   # free 及未知档位统一回落免费档


def _get_entry(key: str) -> dict:
    return STORE.get_entry('%s|%s' % (_period(), key))


def check_llm_quota(key: str, plan: str = None) -> dict:
    """返回该维度键**本周期（周四→下周四）**是否还能调用 LLM。
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


def record_llm_call(key: str, kind: str, tok_in: int, tok_out: int):
    """记录一次**真实** LLM API 调用（openai SDK 层统一挂钩，见 core/llm_accounting.py）。

    与 record_llm_usage（按请求估算）**互斥使用**：同一轮只能走其一，否则双算。
    token 来自 API 响应 usage（非估算）；kind = 调用类型（闲聊/攻略生成/检索重排…），
    按类型细分累计进 entry['calls']，供控制台「同一个用户只显总量、点开才显示细则」
    （用户口径 2026-09-26）。
    """
    with _lock:
        e = _get_entry(key)
        e['rounds'] += 1
        e['tok_in'] += int(tok_in)
        e['tok_out'] += int(tok_out)
        e['cost'] += tok_in / 1000.0 * PRICE_IN_YUAN_PER_K + tok_out / 1000.0 * PRICE_OUT_YUAN_PER_K
        calls = e.setdefault('calls', {})
        c = calls.setdefault(str(kind or 'LLM'), {'n': 0, 'tok_in': 0, 'tok_out': 0})
        c['n'] += 1
        c['tok_in'] += int(tok_in)
        c['tok_out'] += int(tok_out)
    STORE.persist()


def entry_calls(key: str) -> dict:
    """本周期该配额键的 calls 明细快照（深拷贝）。

    供「知识库命中缓存」在计算前后做差，得到本轮真实消耗
    （见 core/kb_cache：命中缓存时按这份明细复现记账）。
    """
    with _lock:
        e = _get_entry(key)
        return {str(k): dict(v) for k, v in (e.get('calls') or {}).items()
                if isinstance(v, dict)}


def replay_llm_calls(key: str, calls: dict) -> int:
    """按 calls 明细**复现**一轮真实调用记账（命中缓存、未真调 LLM 时用）。

    与 record_llm_call 同口径（rounds / tok / cost / calls 明细），
    区别是**按快照整体加**（n 也照抄），保证「额度百分比」与真的算过一遍一致。
    返回复现的 token 总数。
    """
    total = 0
    with _lock:
        e = _get_entry(key)
        for kind, v in (calls or {}).items():
            n = int(v.get('n') or 0)
            tin = int(v.get('tok_in') or 0)
            tout = int(v.get('tok_out') or 0)
            if n <= 0 and tin <= 0 and tout <= 0:
                continue
            e['rounds'] += n
            e['tok_in'] += tin
            e['tok_out'] += tout
            e['cost'] += tin / 1000.0 * PRICE_IN_YUAN_PER_K + tout / 1000.0 * PRICE_OUT_YUAN_PER_K
            c = e.setdefault('calls', {}).setdefault(str(kind or 'LLM'),
                                                    {'n': 0, 'tok_in': 0, 'tok_out': 0})
            c['n'] += n
            c['tok_in'] += tin
            c['tok_out'] += tout
            total += tin + tout
    STORE.persist()
    return total


def quota_prompt(key: str, plan: str, info: dict) -> str:
    """额度用尽时的固定提示文案（不调 LLM）。基础功能仍可正常使用。

    只报**百分比**、不报具体 token（用户口径 2026-09-25：用户只能看到剩余百分比，
    看不到具体用量）。
    """
    return ('本周算力已用尽（同一网络/设备共享，剩余 0%）。'
            '基础功能（查询 / 计算 / 攻略）仍可使用，下周四零点自动恢复。')


def usage_brief(key: str, day: Optional[str] = None) -> dict:
    """**用户侧**用量视图：只给剩余百分比，不含任何绝对数值（用户口径 2026-09-25）。

    用户看不到具体消耗的 token / 成本 / 轮次 —— 那些只在开发者侧
    `/api/admin/usage` 里暴露。这里刻意连 limit、used 都不返回，
    避免从响应体反推出真实用量。
    """
    d = (day or _period()).strip()
    plan = get_user_plan(key)
    limit = _tok_limit(plan)
    if not runtime.quota_enabled() or limit <= 0:
        # 本地模式，或**不限量档**（开发者本人）：不显示进度条
        return {'day': d, 'cloud_mode': bool(runtime.quota_enabled()),
                'unlimited': True, 'plan': plan, 'remain_percent': 100.0}
    try:
        with STORE._lock:
            e = STORE.usage.get('%s|%s' % (d, key)) or {}
    except Exception:
        e = {}
    used = int(e.get('tok_in') or 0) + int(e.get('tok_out') or 0)
    pct = max(0.0, min(100.0, (limit - used) * 100.0 / limit))
    return {'day': d, 'cloud_mode': True, 'unlimited': False,
            'plan': plan, 'remain_percent': round(pct, 1)}


def quota_status(key: str, plan: str = None) -> dict:
    """供前端查询当前剩余额度（开发者侧/排障用，含绝对数值）。

    注意：**用户侧不要直接用这个**（会暴露具体 token），用户在 /api/me 与
    /api/quota 拿到的是 usage_brief（只给百分比）。
    """
    if plan is None:
        plan = get_user_plan(key)
    if not runtime.quota_enabled():
        return {'cloud_mode': False, 'plan': plan, 'unlimited': True}
    limit = _tok_limit(plan)
    e = _get_entry(key)
    used = int(e['tok_in']) + int(e['tok_out'])
    if limit <= 0:      # 不限量档（开发者本人）
        return {'cloud_mode': True, 'plan': plan, 'unlimited': True,
                'dim': runtime.quota_key(), 'rounds': e['rounds'], 'tok': used,
                'tok_limit': 0, 'remain_tok': 0, 'cost': round(e['cost'], 4)}
    return {'cloud_mode': True, 'plan': plan, 'dim': runtime.quota_key(),
            'rounds': e['rounds'], 'tok': used, 'tok_limit': limit,
            'remain_tok': max(0, limit - used),
            'cost': round(e['cost'], 4)}
