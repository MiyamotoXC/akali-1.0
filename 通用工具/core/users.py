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
import datetime as _dt
import hmac
import json
import os
import threading

# 注册要写回 users.json（读-改-写），多请求并发时必须串行化，避免互相覆盖。
_LOCK = threading.RLock()

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


def _is_test_users_file() -> bool:
    """当前用户表是否 AI 自测专用（users.test.json）。

    是则新注册账号默认名标记为「ai自测」，以便和正式发放的「测试用户」区分，
    避免两者混淆（用户口径 2026-09-30）。判定只看文件名，不碰正式 users.json。
    """
    return os.path.basename(users_file()) == 'users.test.json'


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
        'plan': str(raw.get('plan') or '').strip(),   # 套餐档：unlimited/free/month；留空则走默认档
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
        fallback = ('ai自测 %s' % u['uid']) if _is_test_users_file() else ('测试用户 %s' % u['uid'])
        accs.append({'uid': u['uid'], 'name': u['note'] or fallback,
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
            if safe_eq(provided, ot):      # 不能用 hmac.compare_digest 直接比 str：中文会抛 TypeError
                return {'ok': True, 'uid': ouid,
                        'user': {'uid': ouid, 'name': '开发者（本人）', 'role': 'owner'},
                        'reason': ''}
        except Exception:
            pass

    if not ot:      # 未启用账号体系：交由上层按旧逻辑处理
        return {'ok': False, 'uid': '', 'user': None, 'reason': 'unset'}

    for u in parse_users():
        try:
            if safe_eq(provided, u['token']):
                if not u.get('enabled', True):
                    return {'ok': False, 'uid': u['uid'], 'user': u, 'reason': 'disabled'}
                if not allow_other_users():
                    return {'ok': False, 'uid': u['uid'], 'user': u, 'reason': 'paused'}
                return {'ok': True, 'uid': u['uid'],
                        'user': {'uid': u['uid'],
                                 'name': u['note'] or (('ai自测 %s' % u['uid'])
                                                       if _is_test_users_file()
                                                       else ('测试用户 %s' % u['uid'])),
                                 'role': 'user'},
                        'reason': ''}
        except Exception:
            continue

    return {'ok': False, 'uid': '', 'user': None,
            'reason': 'bad_token' if provided else 'no_token'}


def register_invite() -> str:
    """**注册邀请码**（入场券，所有人共用），用户口径 2026-09-25。

    读取 AKALI_INVITE_CODE（与 owner 口令共用同一变量）。
    留空时 register() 会拒绝新注册（只允许已有账号登录）。
    """
    v = (os.environ.get('AKALI_INVITE_CODE') or '').strip()
    return v


def allow_register() -> bool:
    """是否允许自助注册。默认跟随「总开关」：AKALI_ALLOW_OTHER_USERS=true 即允许。

    想单独关掉注册（已注册的人照常用）就设 AKALI_ALLOW_REGISTER=false。
    """
    raw = (os.environ.get('AKALI_ALLOW_REGISTER') or '').strip().lower()
    if raw == '':
        return allow_other_users()
    return raw in ('1', 'true', 'on', 'yes', 'y')


def safe_eq(provided, expected) -> bool:
    """恒定时间比较**任意字符串**（含中文）。

    为什么要单独包一层（用户口径 2026-09-25）：`hmac.compare_digest` 只接受
    **纯 ASCII** 的 str，传入中文会抛 `TypeError: comparing strings with non-ASCII
    characters is not supported`；而调用处普遍写成 `except: return False`，
    于是【修复前】**中文口令/邀请码会被静默判为"无效"**（历史实测：AKALI_INVITE_CODE=我是小猪
    时 invite_ok('我是小猪') 返回 False，逐字符 codepoint 却完全一致）。
    下面统一 encode 成 bytes 再比较即修复此问题 —— 当前 safe_eq 已支持中文，比较语义不变，仍防时序侧信道。
    """
    return hmac.compare_digest(str(provided or '').encode('utf-8'),
                               str(expected or '').encode('utf-8'))


def invite_ok(provided) -> bool:
    """校验用户提交的邀请码（恒定时间比较，支持中文）。"""
    want = register_invite()
    if not want:
        return False
    try:
        return safe_eq(provided, want)
    except Exception:
        return False


def _load_file_raw() -> dict:
    """读取用户表**文件原文**（不走 mtime 缓存）—— 注册要写回，必须以磁盘为准。"""
    p = users_file()
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError('bad root')
    except Exception:
        d = {'version': 1, 'config': {'allow_other_users': True}, 'users': {}}
    d.setdefault('config', {}).setdefault('allow_other_users', True)
    d.setdefault('users', {})
    if not isinstance(d['users'], dict):     # 数组写法 → 转成对象写法再写回
        arr = d['users'] if isinstance(d['users'], list) else []
        conv = {}
        for it in arr:
            if isinstance(it, dict) and it.get('uid'):
                conv[str(it['uid'])] = it
        d['users'] = conv
    return d


def _write_file(d: dict) -> None:
    """写回用户表，并让只读缓存失效（改完立即生效）。

    优先「同目录临时文件 + os.replace」原子替换；但云端 compose 把 users.json 做成了
    **单文件 bind mount**（- ./users.json:/app/users.json），而**挂载点不能跨设备 rename** ——
    此时 os.replace 抛 OSError(EXDEV/EBUSY)（实测 Errno 16 Device or resource busy，
    本地无此问题，只在容器里复现）。故捕获后退回「原地重写」：有 _LOCK 串行化、
    文件很小（几 KB），风险可控。
    """
    p = users_file()
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    tmp = p + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)     # 清掉残留，别让它挡住下次
        except Exception:
            pass
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    try:
        os.chmod(p, 0o600)          # 含口令
    except Exception:
        pass
    _CACHE.update(mtime=(None, None, None), data=[])


def _public(uid, rec) -> dict:
    return {'uid': str(uid), 'name': str((rec or {}).get('name') or ''),
            'role': 'user', 'created': str((rec or {}).get('created') or '')}


def find_by_device(device_id: str):
    """按**设备标识**（浏览器 localStorage 里的随机 id）找账号。

    为什么用它而不是 IP 当主标识（用户口径 2026-09-25）：
      · IP 会变（手机切网、家宽重播）→ 同一人反复注册出新 uid；
      · 同一 WiFi 下多人共用一个出口 IP → 会被认成同一个人。
    设备标识由浏览器生成并持久化，稳定且一人一号。
    """
    did = str(device_id or '').strip()
    if not did:
        return None
    with _LOCK:
        d = _load_file_raw()
        for uid, u in (d.get('users') or {}).items():
            if isinstance(u, dict) and str(u.get('device_id') or '').strip() == did and u.get('token'):
                return {'uid': str(uid), 'token': u['token'], 'name': u.get('name') or '',
                        'created': u.get('created') or '', 'enabled': bool(u.get('enabled', True))}
    return None


def register(device_id: str = '', ip: str = '', name: str = '') -> dict:
    """**首次打开自动注册**：按**设备标识**认人（用户口径 2026-09-25）。

    返回 {'ok','created','uid','token','user','reason'}
      · 该设备已有账号 → created=False，直接返回原账号（老用户无感，不必重注册）
      · 新设备 → 分配下一个 uid（000001 起）+ 生成口令，写回 users.json
      · 关闭注册 → ok=False reason='register_off'

    认人：**只认 device_id**（稳定、一人一号）；IP 仅作记录（排障用），不参与身份识别 ——
    IP 会因切网/家宽重播而变（→ 同人反复注册），也会因同 WiFi 多人共用而撞车
    （→ 多人被认成一人），故不当主标识。
    口令现阶段**不需要用户输入**（前端自动保存并随请求带上），将来可升级为用户
    自设的登录密码，那时换设备也能凭它找回旧号。
    """
    did = str(device_id or '').strip()
    ip = str(ip or '').strip()
    with _LOCK:
        d = _load_file_raw()
        users = d['users']
        # ① 设备标识命中 → 老用户直接回位
        if did:
            for uid, u in users.items():
                if isinstance(u, dict) and str(u.get('device_id') or '').strip() == did and u.get('token'):
                    if did and str(u.get('ip') or '') != ip:      # IP 变了就顺手更新，便于排障
                        u['ip'] = ip
                        _write_file(d)
                    return {'ok': True, 'created': False, 'uid': str(uid),
                            'token': u['token'], 'user': _public(uid, u), 'reason': 'exists'}
        # ② 不带设备标识时**不再按 IP 认人**（用户口径 2026-09-25，公网实测）：
        #    同一 NAT 出口下（同 WiFi / 同运营商）多台设备 IP 相同，按 IP 认人会把
        #    后来者当成老用户 → 绕过邀请码校验（实测：换新 device_id 但同 IP 时，
        #    带错误邀请码也能注册成功）。缺少 device_id 就当作新设备走正常注册。
        if not allow_register():
            return {'ok': False, 'created': False, 'uid': '', 'token': '',
                    'user': None, 'reason': 'register_off'}
        # 找下一个空位：直接用刚读到的 users 算，避免为了编号再读一次文件
        uid = ''
        for i in range(1, 1000000):
            k = '%0*d' % (UID_WIDTH, i)
            if k not in users:
                uid = k
                break
        if not uid:
            return {'ok': False, 'created': False, 'uid': '', 'token': '',
                    'user': None, 'reason': 'uid_exhausted'}
        token = new_token()
        default_name = ('ai自测 %s' % uid) if _is_test_users_file() else ('测试用户 %s' % uid)
        rec = {
            'name': (name or '').strip() or default_name,
            'token': token,
            'enabled': True,
            'device_id': did,
            'ip': ip,
            'created': _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        users[uid] = rec
        _write_file(d)
        return {'ok': True, 'created': True, 'uid': uid, 'token': token,
                'user': _public(uid, rec), 'reason': 'created'}


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
