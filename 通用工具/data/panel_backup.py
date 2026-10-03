# -*- coding: utf-8 -*-
"""按内部 uid 的装备面板**云端备份**（用户口径 2026-10-03）。

背景：云端模式面板存浏览器本地（AKALI_PANEL_STORE=client），随 /api/chat 当
panel_overrides 携带，服务器无状态。本模块提供服务器侧的**一份按 uid 备份**：
客户端保存面板时推一份；本地缓存丢失（换设备 / 清了缓存）时可回填（本地为主，
本地有数据则不覆盖）。

存储：一个 uid 一个 JSON 文件，目录 `用户端特用/panels/`（可用环境变量
AKALI_PANEL_BACKUP_DIR 覆盖，云端挂持久卷时指向卷内即可）。文件内容形如：
    {"角色名": {"attack_panel": {...}, "defense_panel": {...}, "level": 120}}
（`level` 为「我的舰船·装备面板」里的等级，可选；供遭遇战 PVE 等级压制使用。
  本模块整体 JSON 原样读写、不做字段校验，故新增字段天然随面板一起备份/回填。）
"""
import json
import os
import threading

# panel_backup.py 位于 通用工具/data/，上溯三级到项目根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_LOCK = threading.Lock()


def _dir() -> str:
    d = (os.environ.get('AKALI_PANEL_BACKUP_DIR') or '').strip()
    return d or os.path.join(_PROJECT_ROOT, '用户端特用', 'panels')


def _file(uid: str) -> str:
    # uid 只可能是 6 位数字 / 自填规则，but 仍做白名单化，避免路径穿越
    safe = ''.join(c for c in str(uid or '') if c.isalnum() or c in '-_') or '_'
    return os.path.join(_dir(), safe + '.json')


def load(uid: str) -> dict:
    """读取该 uid 的面板备份；无文件 / 解析失败返回 {}。"""
    uid = str(uid or '').strip()
    if not uid:
        return {}
    try:
        with open(_file(uid), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(uid: str, panels) -> bool:
    """整体写回该 uid 的面板备份。panels 须为 dict，否则拒绝。"""
    uid = str(uid or '').strip()
    if not uid or not isinstance(panels, dict):
        return False
    with _LOCK:
        p = _file(uid)
        try:
            os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(panels, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, p)
            except OSError:
                # 单文件 bind mount 等不能跨设备 rename 的场景 → 退回原地重写
                if os.path.exists(tmp):
                    os.remove(tmp)
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(panels, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False
