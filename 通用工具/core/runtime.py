# -*- coding: utf-8 -*-
"""
运行模式（local / cloud）单一主开关
================================================================

用一个环境变量在「本地单机」与「云端多用户」两种运行方式间切换，
**其余模块不再各自散读环境变量**，统一调用本模块的访问器 —— 便于收口与回滚。

    AKALI_RUN_MODE = cloud  ← 默认（2026-09-30 起由 local 改为 cloud，全程云端）
                   = cloud   ← 云端多用户模式

兼容别名：老的 FX_CLOUD_MODE=1/true/on/yes 仍保留作配额开关（见 quota_enabled），但不再影响运行模式（run_mode 缺省即 cloud）。
（老脚本/老测试继续可用）。

两模式的差异一览（详见 §1 方案表）：

| 维度       | local                     | cloud（默认）                  | 本模块访问器            |
|-----------|---------------------------|--------------------------------|------------------------|
| 面板存储   | 服务器 user_data.json      | 客户端本地 + 请求携带           | panel_store()          |
| LLM 限额   | 不限（仅统计）             | 按 IP 日 token 总量限额         | quota_enabled()/quota_key()/quota_daily_tok() |
| 准入       | 免口令（本机豁免）          | 邀请口令必填                    | invite_code()/auth_required() |
| 知识库     | 本地磁盘                   | 启动时从对象存储同步到本地缓存    | kb_sync_enabled()/kb_base_url()/kb_manifest_url() |
| 监听       | 127.0.0.1:8000            | 0.0.0.0:8000 + 反代头           | host()/port()/proxy_headers() |
| 跨域       | 同源                       | 前端域名白名单                  | allow_origins()        |
| 心跳自动关机| 可选开                     | 保持关闭                        | auto_shutdown()        |

设计约束：
- 缺省不设任何变量 → 云端多用户模式（0.0.0.0 监听、按 IP 限额、邀请口令必填）；显式设 local 即回滚到本地个人模式。
- 切回 local 即完全恢复现状 —— **本模块即回滚开关**。
- 面板存储与限额维度在 cloud 下必须联动（面板到客户端 → 服务器无用户文件
  → 只能按 IP 识别），故两者都由 run_mode 统一推导默认值，避免半开状态。
"""
from __future__ import annotations

import os
from typing import List

__all__ = [
    "run_mode", "is_cloud", "is_local",
    "panel_store", "quota_enabled", "quota_key", "quota_daily_tok",
    "invite_code", "auth_required",
    "host", "port", "proxy_headers", "allow_origins", "cors_enabled",
    "kb_sync_enabled", "kb_base_url", "kb_manifest_url",
    "auto_shutdown", "summary",
]

# 加载项目根 .env（与 web_app / quota 共用同一套配置；幂等）
try:
    from dotenv import load_dotenv
    _PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load_dotenv(os.path.join(_PROJ_ROOT, '.env'), override=False)
except Exception:
    pass


def _env(*names: str, default: str = '') -> str:
    """按顺序取第一个非空环境变量值（用于「新名优先、旧名兼容」）。"""
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != '':
            return str(v).strip()
    return default


def _flag(v: str) -> bool:
    return str(v).strip().lower() in ('1', 'true', 'on', 'yes', 'cloud')


# ---------------- .env 实时读取（改文件即生效，无需重建/重启容器） ----------------
# 背景：docker 的 env_file 只在**容器创建时**注入 os.environ —— 之后改宿主机 .env，
#       已有进程里的 os.environ 不会变；而「算力额度上限」这类配置要求改完立即生效。
# 做法：直接读 .env 文件（compose 已把宿主机 ./.env 只读挂载到容器 /app/.env）；
#       按 mtime+size 缓存，只有文件真的变了才重新解析，避免每请求都读盘。
_ENV_FILE_CACHE: dict = {}

# 项目根（与上面 load_dotenv 的目标一致）：本文件在 <根>/通用工具/core/runtime.py
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def env_file_path() -> str:
    """实时读取用的 .env 路径：AKALI_ENV_FILE 优先，否则项目根/.env。"""
    p = (os.environ.get('AKALI_ENV_FILE') or '').strip()
    return p or os.path.join(_ROOT, '.env')


def _parse_env_file(path: str) -> dict:
    """极简 .env 解析：支持 # 整行注释、行内注释（引号内不动）、去引号、UTF-8 BOM。"""
    data: dict = {}
    try:
        with open(path, encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                if not k:
                    continue
                v = v.strip()
                if v[:1] not in ('"', "'"):
                    for i in range(1, len(v)):
                        if v[i] == '#' and v[i - 1] in ' \t':
                            v = v[:i].strip()
                            break
                data[k] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    return data


def env_file_values() -> dict:
    """返回 .env 解析结果（按 mtime+size 缓存；文件被改后自动重读）。"""
    path = env_file_path()
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _ENV_FILE_CACHE['sig'] = None
        _ENV_FILE_CACHE['data'] = {}
        return {}
    if _ENV_FILE_CACHE.get('sig') == sig:
        return _ENV_FILE_CACHE.get('data') or {}
    data = _parse_env_file(path)
    _ENV_FILE_CACHE['sig'] = sig
    _ENV_FILE_CACHE['data'] = data
    return data


def live_env(*names: str, default: str = '') -> str:
    """实时取值：**先读 .env 文件**（改文件即生效），没有再回落进程环境变量。

    与 _env 的区别：_env 只认进程环境（容器创建时注入，改 .env 必须重建容器）；
    live_env 每次看 .env 文件，适合「额度上限 / 白名单」这类要求实时生效的配置。
    """
    vals = env_file_values()
    for n in names:
        v = vals.get(n)
        if v is not None and str(v).strip() != '':
            return str(v).strip()
    return _env(*names, default=default)


def run_mode() -> str:
    """返回 'local' 或 'cloud'。AKALI_RUN_MODE 优先，兼容旧 FX_CLOUD_MODE。"""
    raw = _env('AKALI_RUN_MODE')
    if not raw:
        # 兼容别名：老的 FX_CLOUD_MODE=1 等价 cloud
        raw = 'cloud' if _flag(_env('FX_CLOUD_MODE')) else 'cloud'
    raw = raw.strip().lower()
    return 'cloud' if raw in ('cloud', 'server', 'remote') else 'local'


def is_cloud() -> bool:
    return run_mode() == 'cloud'


def is_local() -> bool:
    return run_mode() == 'local'


# ---------------- 面板存储 ----------------
def panel_store() -> str:
    """'server'（本地 user_data.json）| 'client'（客户端本地 + 请求携带）。"""
    v = _env('AKALI_PANEL_STORE').lower()
    if v in ('server', 'client'):
        return v
    return 'client' if is_cloud() else 'server'


# ---------------- 限额 ----------------
def quota_enabled() -> bool:
    """是否启用 LLM 限额闸控（cloud 默认开；可用 FX_CLOUD_MODE 显式覆盖）。"""
    explicit = _env('FX_CLOUD_MODE')
    if explicit:
        return _flag(explicit)
    return is_cloud()


def quota_key() -> str:
    """限额维度：'ip'（云端按客户端 IP）| 'session'（本地按 session_id）。"""
    v = _env('AKALI_QUOTA_KEY').lower()
    if v in ('ip', 'session'):
        return v
    return 'ip' if is_cloud() else 'session'


def quota_daily_tok() -> int:
    """每 IP 每周期（周四→下周四）token 总量上限（输入+输出），对应免费档 FX_QUOTA_FREE（**实时读 .env**）。"""
    try:
        return max(0, int(live_env('FX_QUOTA_FREE', default='300000')))
    except (TypeError, ValueError):
        return 300000


# ---------------- 准入（邀请口令） ----------------
def invite_code() -> str:
    """全局邀请口令；空 = 不校验（本地默认）。"""
    return _env('AKALI_INVITE_CODE')


def auth_required() -> bool:
    return bool(invite_code())


# ---------------- 网络 ----------------
def host() -> str:
    default = '0.0.0.0' if is_cloud() else '127.0.0.1'
    return _env('HOST', default=default)


def port() -> int:
    try:
        return int(_env('PORT', default='8000'))
    except (TypeError, ValueError):
        return 8000


def proxy_headers() -> bool:
    """是否信任反代头（cloud 默认开）。"""
    v = _env('AKALI_PROXY_HEADERS')
    if v:
        return _flag(v)
    return is_cloud()


def allow_origins() -> List[str]:
    """CORS 白名单，逗号分隔；空 = 不启用跨域（同源，本地默认）。"""
    raw = _env('AKALI_ALLOW_ORIGINS')
    return [o.strip() for o in raw.split(',') if o.strip()]


def cors_enabled() -> bool:
    return bool(allow_origins())


# ---------------- 知识库 ----------------
def kb_sync_enabled() -> bool:
    """是否在启动时从对象存储同步 KB 到本地缓存（cloud 默认开）。"""
    v = _env('FX_KB_SYNC')
    if v:
        return _flag(v)
    return is_cloud()


def kb_base_url() -> str:
    return _env('FX_KB_BASE_URL')


def kb_manifest_url() -> str:
    return _env('FX_KB_MANIFEST_URL', 'FX_CLOUD_MANIFEST_URL')


# ---------------- 生命周期 ----------------
def auto_shutdown() -> bool:
    """心跳超时自动关机（本地可选；云端恒关）。"""
    if is_cloud():
        return False
    return _flag(_env('AKALI_AUTO_SHUTDOWN'))


def summary() -> dict:
    """供启动日志/调试打印：一次性看清当前模式解析结果。"""
    return {
        'run_mode': run_mode(),
        'panel_store': panel_store(),
        'quota': {'enabled': quota_enabled(), 'key': quota_key(),
                  'daily_tok': quota_daily_tok()},
        'auth': {'required': auth_required()},
        'net': {'host': host(), 'port': port(), 'proxy_headers': proxy_headers(),
                'allow_origins': allow_origins()},
        'kb': {'sync': kb_sync_enabled(), 'base_url': kb_base_url(),
               'manifest_url': kb_manifest_url()},
        'auto_shutdown': auto_shutdown(),
    }


if __name__ == '__main__':
    import json
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
