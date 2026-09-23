# -*- coding: utf-8 -*-
"""
知识库启动同步（云端模式）
================================================================

云端部署下知识库（≈480MB）放在对象存储，服务启动时按 manifest 拉取到本地缓存目录，
**业务代码继续读本地磁盘**（零改动）——即「对象存储 + 启动同步」方案。

    FX_KB_SYNC       = on/off    （默认：cloud 模式为 on，见 core/runtime.py）
    FX_KB_BASE_URL   = https://<bucket>/kb        （文件根；与 manifest 内相对路径拼接）
    FX_KB_MANIFEST_URL = https://<bucket>/kb/manifest.json
    FX_CLOUD_CACHE_DIR（可选）= 落地目录，默认项目根下的 知识库/
    FX_KB_SYNC_TIMEOUT（可选）= 单文件下载超时秒数，默认 120

manifest 约定（JSON）::

    {
      "version": "2026-09-15T10:00:00Z",
      "files": {
        "知识库/底层数据/产物/data.json": {"sha256": "…", "size": 123456},
        "知识库/攻略/guide_index.json":  {"sha256": "…", "size": 2345}
      }
    }

行为：
- 只下载「本地缺失 / 大小不符 / sha256 不符」的文件（增量）。
- 先写临时文件再原子替换，失败回退到本地既有文件（**永不因同步失败而中断启动**）。
- 本地模式（AKALI_RUN_MODE=local）或未配置 manifest → 直接跳过，行为与改造前一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

from core import runtime

__all__ = ["sync_kb", "fetch_manifest", "manifest_status"]

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_LOCAL_ROOT = os.path.join(_PROJ_ROOT, "知识库")


def _log(msg: str) -> None:
    print("[kb_sync] %s" % msg, flush=True)


def _local_root() -> str:
    """落地根目录：FX_CLOUD_CACHE_DIR 指定则用之，否则项目内 知识库/。"""
    d = (os.getenv('FX_CLOUD_CACHE_DIR') or '').strip()
    return os.path.normpath(d) if d else _DEFAULT_LOCAL_ROOT


def _http_get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'akali-kb-sync/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch_manifest(url: str = None, timeout: int = 60) -> dict:
    """下载并解析 manifest；未配置 URL 返回 {}。"""
    url = url or runtime.kb_manifest_url()
    if not url:
        return {}
    raw = _http_get(url, timeout)
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('files'), dict):
        raise ValueError('manifest 格式非法：缺少 files 映射')
    return data


def _needs_download(dest: str, meta: dict) -> bool:
    if not os.path.isfile(dest):
        return True
    try:
        if meta.get('size') is not None and os.path.getsize(dest) != int(meta['size']):
            return True
        want = (meta.get('sha256') or '').strip().lower()
        if want and _sha256_file(dest) != want:
            return True
    except Exception:
        return True
    return False


def sync_kb(force: bool = False) -> dict:
    """执行一次同步。返回统计 dict。任何异常都被吞掉并记录（不阻断启动）。"""
    if not runtime.kb_sync_enabled() and not force:
        _log('未启用（AKALI_RUN_MODE=local 或 FX_KB_SYNC=off），跳过')
        return {'skipped': True, 'reason': 'disabled'}

    base = runtime.kb_base_url().rstrip('/')
    if not base:
        _log('未配置 FX_KB_BASE_URL，跳过（继续使用本地既有知识库）')
        return {'skipped': True, 'reason': 'no_base_url'}

    try:
        manifest = fetch_manifest()
    except Exception as e:
        _log('拉取 manifest 失败，继续使用本地既有知识库: %s' % e)
        return {'skipped': True, 'reason': 'manifest_error', 'error': str(e)}

    if not manifest:
        _log('manifest 为空，跳过')
        return {'skipped': True, 'reason': 'empty_manifest'}

    root = _local_root()
    timeout = int(os.getenv('FX_KB_SYNC_TIMEOUT') or '120')
    files = manifest.get('files') or {}
    downloaded, skipped, failed = 0, 0, 0

    for rel, meta in files.items():
        rel_norm = rel.replace('\\', '/').lstrip('/')
        dest = os.path.normpath(os.path.join(root, rel_norm))
        # 防目录穿越：目标必须落在 root 内
        if not dest.startswith(os.path.normpath(root)):
            _log('跳过越界路径: %s' % rel)
            failed += 1
            continue
        try:
            if not _needs_download(dest, meta or {}):
                skipped += 1
                continue
            url = base + '/' + urllib.parse.quote(rel_norm)
            blob = _http_get(url, timeout)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix='.kbtmp')
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(blob)
                os.replace(tmp, dest)   # 原子替换
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            downloaded += 1
        except Exception as e:
            failed += 1
            _log('下载失败（保留本地既有）: %s -> %s' % (rel, e))

    _log('同步完成：版本=%s 更新=%d 已最新=%d 失败=%d 落地=%s'
         % (manifest.get('version') or '-', downloaded, skipped, failed, root))
    return {'skipped': False, 'version': manifest.get('version'),
            'downloaded': downloaded, 'up_to_date': skipped, 'failed': failed,
            'root': root, 'total': len(files)}


def manifest_status() -> dict:
    """诊断用：当前配置与是否启用。"""
    return {
        'enabled': runtime.kb_sync_enabled(),
        'base_url': runtime.kb_base_url(),
        'manifest_url': runtime.kb_manifest_url(),
        'local_root': _local_root(),
    }


if __name__ == '__main__':
    print(json.dumps(manifest_status(), ensure_ascii=False, indent=2))
    print(json.dumps(sync_kb(), ensure_ascii=False, indent=2))
