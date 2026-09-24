# -*- coding: utf-8 -*-
"""内部测试账号体系：**开关在 .env，用户表独立成一个外层 JSON**（用户口径 2026-09-25）。

两处配置，各管各的
------------------
① `.env`（只有服务器管理员看得到）—— 管**开关**与**开发者本人**：
       AKALI_ALLOW_OTHER_USERS=true     # 总开关：false = 只有开发者能用
       AKALI_OWNER_UID=000000           # 开发者 uid
       AKALI_OWNER_TOKEN=<你的口令>     # 留空则回落 AKALI_INVITE_CODE / API_TOKEN

② `users.json`（**项目根目录下的外层文件**）—— 管**测试用户列表**：
       {
         "users": {
           "000001": {"name": "小明", "token": "k7m2p9qrs4tvwx3z", "enabled": true},
           "000002": {"name": "小红", "token": "a3b4c5d6e7f8g9h2", "enabled": true}
         }
       }

   路径可用 `AKALI_USERS_FILE` 覆盖（默认 项目根/users.json；云端 compose 已把它
   挂载到宿主机，**直接编辑该文件即可，改完立即生效、无需重启**）。
   uid 六位、从 000001 起编号；000000 是开发者本人，不写在这张表里。

删用户
------
从 users.json 里删掉那一项（或把它 `enabled` 改成 false）即可。对方浏览器 localStorage
里可能还缓存着旧口令，但服务端已经不认——他下次请求会拿到 401，前端会自动重新弹口令框，
**不需要动任何客户端**。

读取策略
--------
按文件 mtime 做缓存：文件没变就直接用内存里的解析结果（高频鉴权不重复读盘），
文件一改立即重新加载 —— 这就是"改完生效、无需重启"的实现方式。
"""
import hmac
import json
import os

OWNER_UID_DEFAULT = '000000'
UID_WIDTH = 6
TOKEN_ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789'

_CACHE = {'mtime': None, 'size': None, 'data': []}


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
    """准入总开关。默认 True；设 false 则只允许开发者本人（他自己不受影响）。"""
    raw = (os.environ.get('AKALI_ALLOW_OTHER_USERS') or '').strip().lower()
    if raw == '':
        return True
    return raw in ('1', 'true', 'on', 'yes', 'y')


def admin_uids() -> set:
    """**运营/管理侧**的访问白名单（用户口径 2026-09-25）。

    能看/能改运营类内容（用量视图、账号列表、反馈等）的 uid 集合。
    默认只含开发者本人（AKALI_OWNER_UID）；想授权给别人时在 .env 里加：

        AKALI_ADMIN_UIDS=000000,000003

    注意：普通用户**不在**此表里 —— 他们只能访问只读的「我的信息」接口，
    任何编辑类操作都会被 403 拒绝（"用户相关的内容只可读"）。
    """
    raw = (os.environ.get('AKALI_ADMIN_UIDS') or '').strip()
    out = {owner_uid()}
    for u in raw.replace('，', ',').replace(';', ',').split(','):
        u = u.strip()
        if u:
            out.add(u)
    return out


def is_admin(uid: str) -> bool:
    """该 uid 是否有运营/管理侧权限。"""
    return str(uid or '').strip() in admin_uids()


def users_file() -> str:
    """用户表 JSON 路径：AKALI_USERS_FILE 优先；否则 项目根/users.json（外层文件）。"""
    p = (os.environ.get('AKALI_USERS_FILE') or '').strip()
    if p:
        return p
    root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
    return os.path.join(root, 'users.json')


def _norm_entry(uid, raw):
    """把 JSON 里的一项规范化成账号记录；字段不全就丢弃（不让一条写错影响整体）。"""
    if not isinstance(raw, dict):
        return None
    uid = str(uid or '').strip()
    token = str(raw.get('token') or '').strip()
    if not uid or not token:          # uid 或口令缺失 → 整条丢弃（曾把 None 写成 'None'）
        return None
    return {
        'uid': uid,
        'token': token,
        'note': str(raw.get('name') or raw.get('note') or '').strip(),
        'enabled': bool(raw.get('enabled', True)),
        'created': str(raw.get('created') or '').strip(),
    }


def parse_users() -> list:
    """读取用户表 → [{'uid','token','note','enabled','created'}, ...]（按 mtime 缓存）。

    兼容两种写法：
      A) {"users": {"000001": {...}}}          ← 推荐（uid 作为键，一眼看到编号）
      B) {"users": [{"uid": "000001", ...}]}   ← 也支持（数组形式）
    另外，为兼容早期版本，仍会读取 .env 里的 AKALI_USERS（`uid:口令[:备注]` 逗号分隔）；
    两边同时存在时**以 users.json 为准**。
    """
    path = users_file()
    try:
        st = os.stat(path)
        # 缓存键带上 inode：同名文件被"删了重建"或快速改写成同样大小时，仅靠
        # mtime+size 可能判不出变化，加 inode 后能可靠刷新（用户口径 2026-09-25：
        # 改完 users.json 必须立即生效）。
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        if _CACHE['mtime'] == key:
            return _CACHE['data']
    except OSError:
        # 文件不存在：仍允许走 .env 兼容路径
        if not (os.environ.get('AKALI_USERS') or '').strip():
            _CACHE.update(mtime=(None, None, None), data=[])
            return []
        key = (None, None, None)

    out = []
    data = None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = None

    if isinstance(data, dict):
        users = data.get('users')
        if isinstance(users, dict):
            for uid, raw in users.items():
                rec = _norm_entry(uid, raw)
                if rec:
                    out.append(rec)
        elif isinstance(users, list):
            for item in users:
                if isinstance(item, dict):
                    rec = _norm_entry(item.get('uid'), item)
                    if rec:
                        out.append(rec)

    if not out:
        # 兼容旧配置（.env 的 AKALI_USERS）：uid:口令[:备注]，逗号分隔
        raw_env = (os.environ.get('AKALI_USERS') or '').strip()
        raw_env = raw_env.replace('，', ',').replace('；', ';').replace(';', ',')
        for item in raw_env.split(','):
            parts = [x.strip() for x in item.split(':')] if item.strip() else []
            if len(parts) < 2 or not parts[0] or not parts[1]:
                continue
            out.append({'uid': parts[0], 'token': parts[1],
                        'note': ':'.join(parts[2:]).strip(), 'enabled': True, 'created': ''})

    seen = set()
    uniq = []
    for r in out:
        if r['uid'] in seen:
            continue
        seen.add(r['uid'])
        uniq.append(r)
    uniq.sort(key=lambda x: x['uid'])
    _CACHE.update(mtime=key, data=uniq)
    return uniq


def get_account(uid: str):
    """按 uid 找账号（owner 或测试用户）；找不到返回 None。"""
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
                     'note': '内置账号（.env）', 'enabled': True, 'role': 'owner'})
    for u in parse_users():
        accs.append({'uid': u['uid'], 'name': u['note'] or ('测试用户 %s' % u['uid']),
                     'token': u['token'], 'note': u['note'],
                     'enabled': u.get('enabled', True), 'role': 'user'})
    return accs


def auth(token) -> dict:
    """口令 → 准入判定。

    返回 {'ok', 'uid', 'user', 'reason'}
      reason: ''  放行 / 'no_token' 未带 / 'bad_token' 无效
              'paused' 总开关关闭 / 'disabled' 账号被停用 / 'unset' 开发者口令未配置
    """
    provided = str(token or '')
    ot = owner_token()
    ouid = owner_uid()

    if provided and ot:
        try:
            if hmac.compare_digest(provided, ot):
                return {'ok': True, 'uid': ouid,
                        'user': {'uid': ouid, 'name': '开发者（本人）', 'role': 'owner'},
                        'reason': ''}
        except Exception:
            pass

    if not ot:      # 未启用账号体系：交由上层按旧逻辑处理
        return {'ok': False, 'uid': '', 'user': None, 'reason': 'unset'}

    for u in parse_users():
        try:
            if hmac.compare_digest(provided, u['token']):
                if not u.get('enabled', True):
                    return {'ok': False, 'uid': u['uid'], 'user': u, 'reason': 'disabled'}
                if not allow_other_users():
                    return {'ok': False, 'uid': u['uid'], 'user': u, 'reason': 'paused'}
                return {'ok': True, 'uid': u['uid'],
                        'user': {'uid': u['uid'],
                                 'name': u['note'] or ('测试用户 %s' % u['uid']),
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
    """给出下一个建议 uid（六位，000001 起）——用于给新用户编号时的提示。"""
    used = {owner_uid()} | {u['uid'] for u in parse_users()}
    for i in range(1, 1000000):
        k = '%0*d' % (UID_WIDTH, i)
        if k not in used:
            return k
    return ''
