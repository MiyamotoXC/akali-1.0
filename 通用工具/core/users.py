# -*- coding: utf-8 -*-
"""内部测试账号：**全部由 .env 控制**（用户口径 2026-09-25）。

配置项（都写在 .env 里，只有服务器管理员可见，云端可直接改、改完重启生效）：

    # 准入总开关：true=测试用户可用；false=只有开发者本人可用
    AKALI_ALLOW_OTHER_USERS=true

    # 开发者本人（0 号用户）
    AKALI_OWNER_UID=000000
    AKALI_OWNER_TOKEN=<你的口令>          # 留空则回落 AKALI_INVITE_CODE / API_TOKEN

    # 测试用户：uid:口令[:备注]，多个用英文逗号分隔
    AKALI_USERS=000001:abcdefgh12345678:测试用户一,000002:ijklmnop87654321:测试用户二

设计要点
--------
· **无状态**：不落任何 JSON 文件，每次直接读环境变量 —— 改 .env 重启即生效。
· **删用户 = 删 .env 里那一段**。对方浏览器 localStorage 里可能还存着旧口令，
  但那不影响服务端判定：他下次请求会拿到 401（前端会重新弹口令框），
  也就是说"删掉 uid 数据"不需要去动任何客户端。
· 口令比较用 hmac.compare_digest（恒定时间）。
· 只有开发者（AKALI_OWNER_UID）能访问管理接口，且管理接口会回显这些配置，
  所以"只有我自己能看见" —— 前提是别把 .env 给别人、别把管理接口暴露给非 owner。

为什么不做 JSON 用户表
----------------------
用户口径要的是"开关和口令都在 .env、我能直接改"。落 JSON 会多一份需要同步的
状态（谁是真源？改 .env 还是改表？），反而容易不一致。这里让 .env 做唯一真源。
"""
import hmac
import os

OWNER_UID_DEFAULT = '000000'
UID_WIDTH = 6
TOKEN_ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789'


def owner_uid() -> str:
    """开发者本人 uid（0 号用户），默认 000000。"""
    return (os.environ.get('AKALI_OWNER_UID') or OWNER_UID_DEFAULT).strip() or OWNER_UID_DEFAULT


def owner_token() -> str:
    """开发者口令：AKALI_OWNER_TOKEN 优先，兼容旧的 AKALI_INVITE_CODE / API_TOKEN。"""
    for name in ('AKALI_OWNER_TOKEN', 'AKALI_INVITE_CODE', 'API_TOKEN'):
        v = (os.environ.get(name) or '').strip()
        if v:
            return v
    return ''


def allow_other_users() -> bool:
    """准入总开关。默认 True（可放测试用户进来）；设 false 则只允许开发者本人。

    注意：总开关为 false 时，**开发者自己不受影响**（见 auth()）。
    """
    raw = (os.environ.get('AKALI_ALLOW_OTHER_USERS') or '').strip().lower()
    if raw == '':
        return True
    return raw in ('1', 'true', 'on', 'yes', 'y')


def parse_users() -> list:
    """解析 AKALI_USERS → [{'uid','token','note','enabled'}, ...]。

    格式：`uid:口令[:备注]`，多用户用英文逗号分隔；也容忍中文逗号/分号分隔。
    容错：字段不足或 uid 非法的条目直接跳过（不让一行写错影响整体）。
    """
    raw = (os.environ.get('AKALI_USERS') or '').strip()
    if not raw:
        return []
    raw = raw.replace('，', ',').replace('；', ';').replace(';', ',')
    out = []
    seen = set()
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        parts = item.split(':')
        if len(parts) < 2:
            continue                      # 至少要有 uid:口令
        uid = parts[0].strip()
        token = parts[1].strip()
        note = ':'.join(parts[2:]).strip()
        if not uid or not token or uid in seen:
            continue
        seen.add(uid)
        out.append({'uid': uid, 'token': token, 'note': note, 'enabled': True})
    return out


def get_account(uid: str):
    """按 uid 找账号：owner 或测试用户；找不到返回 None。"""
    uid = str(uid or '').strip()
    if not uid:
        return None
    if uid == owner_uid():
        return {'uid': uid, 'token': owner_token(), 'note': '开发者（本人）',
                'enabled': True, 'role': 'owner'}
    for u in parse_users():
        if u['uid'] == uid:
            return dict(u, role='user')
    return None


def list_accounts() -> list:
    """列出全部账号（含口令）—— 仅供 owner 的管理接口调用。"""
    accs = []
    ot = owner_token()
    if ot:
        accs.append({'uid': owner_uid(), 'name': '开发者（本人）', 'token': ot,
                     'note': '内置账号', 'enabled': True, 'role': 'owner'})
    for u in parse_users():
        accs.append({'uid': u['uid'], 'name': u['note'] or ('测试用户 %s' % u['uid']),
                     'token': u['token'], 'note': u['note'], 'enabled': True, 'role': 'user'})
    return accs


def auth(token) -> dict:
    """口令 → 准入判定。

    返回 {'ok': bool, 'uid': str, 'user': dict|None, 'reason': str}
      reason: ''           放行
              'no_token'   未带口令
              'bad_token'  口令无效
              'paused'     总开关已关闭（服务未对测试用户开放）
              'unset'      开发者口令未配置（尚未启用账号体系）
    """
    provided = str(token or '')
    ot = owner_token()
    ouid = owner_uid()

    if provided and ot:
        try:
            if hmac.compare_digest(provided, ot):
                return {'ok': True, 'uid': ouid,
                        'user': {'uid': ouid, 'name': '开发者（本人）', 'role': 'owner'}, 'reason': ''}
        except Exception:
            pass

    # 开发者口令未配置 → 视为"未启用账号体系"，交由上层按旧逻辑（无口令即放行）处理
    if not ot:
        return {'ok': False, 'uid': '', 'user': None, 'reason': 'unset'}

    for u in parse_users():
        try:
            if hmac.compare_digest(provided, u['token']):
                if not allow_other_users():
                    return {'ok': False, 'uid': u['uid'], 'user': u, 'reason': 'paused'}
                return {'ok': True, 'uid': u['uid'],
                        'user': {'uid': u['uid'], 'name': u['note'] or ('测试用户 %s' % u['uid']),
                                 'role': 'user'},
                        'reason': ''}
        except Exception:
            continue

    return {'ok': False, 'uid': '', 'user': None,
            'reason': 'bad_token' if provided else 'no_token'}


def new_token() -> str:
    """生成一个适合人工分发的口令（16 位、无易混字符）。"""
    import secrets
    return ''.join(secrets.choice(TOKEN_ALPHABET) for _ in range(16))


def next_uid() -> str:
    """给出下一个建议 uid（六位，000001 起）—— 仅用于 .env 注释里做提示。"""
    used = {owner_uid()} | {u['uid'] for u in parse_users()}
    for i in range(1, 1000000):
        k = '%0*d' % (UID_WIDTH, i)
        if k not in used:
            return k
    return ''
