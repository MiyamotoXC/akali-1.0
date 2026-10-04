
// 本次启动标识（服务端注入）：与心跳回报对比，判断本页是否为上次运行遗留的旧页面
window.AKALI_BOOT_ID = '__AKALI_BOOT_ID__';
// 可选：跨域/嵌入场景由宿主页设置 window.ARKLY_API_BASE（如 'https://api.example.com'）；
// 默认空 = 同源（网页直接打开 / PWA 安装 均为同源，无需设置）。
(function () {
  const B = (window.ARKLY_API_BASE || '').replace(/\/$/, '');
  if (!B) return;
  const _f = window.fetch.bind(window);
  window.fetch = function (u, o) {
    if (typeof u === 'string' && u.charAt(0) === '/') u = B + u;
    return _f(u, o);
  };
})();

// ===== 全局 API 超时保护（用户口径 2026-10-04：**所有回复**都算，不止对话）=====
// 任何 /api/ 请求超过 API_TIMEOUT_MS 仍未返回 → 直接 abort（调用方 catch 到超时错误，
// 给「系统超时，可以换个问法或者重启服务器试试」提示），并**自动反馈**一次（提问随包上报，开发者不必
// 等用户长按反馈）。覆盖对话 / 天气 / 面板备份 / 名录 等一切接口 —— 不再只保护对话。
// 可调（排障/自测）：URL 带 ?chat_timeout_ms=1234，或 window.ARKLY_API_TIMEOUT_MS /
// ARKLY_CHAT_TIMEOUT_MS；设 0 关闭。
var API_TIMEOUT_MS = (function () {
  try {
    var sp = new URLSearchParams(location.search);
    var q = sp.get('chat_timeout_ms') || sp.get('api_timeout_ms');
    if (q !== null && q !== '' && !isNaN(Number(q))) return Math.max(0, Number(q));
  } catch (e) {}
  if (typeof window.ARKLY_API_TIMEOUT_MS === 'number') return window.ARKLY_API_TIMEOUT_MS;
  if (typeof window.ARKLY_CHAT_TIMEOUT_MS === 'number') return window.ARKLY_CHAT_TIMEOUT_MS;
  // 默认 90s（用户口径 2026-10-04：原 60s 对"装备/攻略问答"这类链路偏紧 ——
  // 它要走「意图分类 → 检索(重排) → 生成 → 证据校对」最多 5 次串行 LLM，
  // 常态 20~60s，遇到模型端抖动就会误伤。90s 仍能给真卡死兜底）。
  return 90000;
})();

// 调用方据此判断「这次失败是超时中止」而非网络/服务异常
function isTimeoutError(e) {
  return !!(e && (e.name === 'AbortError' || /abort/i.test(String(e.message || ''))));
}

// 超时自动反馈：与长按表情反馈同一接口（/api/feedback），类型 timeout
function _reportTimeout(url, opts, secs) {
  if (String(url || '').indexOf('/api/feedback') >= 0) return;   // 防自身递归
  var q = '';
  try {
    if (opts && typeof opts.body === 'string') {
      var b = JSON.parse(opts.body);
      q = b.message || b.query || b.name || '';
    }
  } catch (e) {}
  var headers = { 'Content-Type': 'application/json' };
  try { if (typeof _apiHeaders === 'function') headers = Object.assign(headers, _apiHeaders()); } catch (e) {}
  var sid = '';
  try { if (typeof _sessionId === 'function') sid = _sessionId(); } catch (e) {}
  try {
    var raw = window.__arklyRawFetch || window.fetch;
    raw('/api/feedback', {
      method: 'POST', headers: headers, credentials: 'include',
      body: JSON.stringify({
        session_id: sid,
        message_id: 'timeout-' + Date.now(),
        user_input: q || ('（接口 ' + url + ' 请求超时）'),
        assistant_reply: '（响应超时 ' + secs + 's，已自动中止：' + url + '）',
        feedback_type: 'timeout', from_kb: false,
        timestamp: new Date().toISOString(), conversation: []
      })
    }).then(function (r) { return r.json(); })
      .then(function (d) { console.log('[timeout-feedback]', url, d); })
      .catch(function () {});
  } catch (e) {}
}

(function () {
  // 装在 ARKLY_API_BASE 那层之内（顺序：超时 → BASE 重写 → 原生），故 u 还是原始
  // 相对路径（/api/...），跨域场景也能正确命中判断。
  var raw = window.fetch.bind(window);
  window.__arklyRawFetch = raw;
  window.fetch = function (u, o) {
    var url = String(u || '');
    o = o || {};
    if (url.indexOf('/api/') < 0 || API_TIMEOUT_MS <= 0 || o.signal) return raw(u, o);
    var ctl = new AbortController();
    var t = setTimeout(function () {
      try { ctl.abort(); } catch (e) {}
      _reportTimeout(url, o, Math.round(API_TIMEOUT_MS / 1000));
    }, API_TIMEOUT_MS);
    var opts = {};
    for (var k in o) { if (Object.prototype.hasOwnProperty.call(o, k)) opts[k] = o[k]; }
    opts.signal = ctl.signal;
    return raw(u, opts).then(function (r) { clearTimeout(t); return r; },
                             function (e) { clearTimeout(t); throw e; });
  };
})();

// ===== 凭证持久化：localStorage 为主 + cookie 兜底（用户口径 2026-09-27）=====
// 现象：老用户「页面挂久了 / 过几天再回来」被重新要求输入邀请码。
// 根因：口令与设备标识**只存在 localStorage**，而 iOS Safari 的 ITP 会清掉
//       「7 天以上未访问」站点的 localStorage（cookie 不受此策略影响）→ 页面
//       把老用户当成新设备 → 自动注册被邀请码闸门拦下（/api/register 403，
//       云端日志已实证）→ 弹「输入邀请码」遮罩。
// 方案：口令 / 设备标识 / 邀请码 同时写一份一年期 cookie；读取时 localStorage
//       缺失就回落 cookie 并回写 localStorage —— 做到「待再久也不用重新输入」。
function _cookieRead(name) {
  try {
    const m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  } catch (e) { return ''; }
}
function _cookieWrite(name, val) {
  try {
    document.cookie = name + '=' + encodeURIComponent(val)
      + '; Max-Age=31536000; Path=/; SameSite=Lax'
      + (location.protocol === 'https:' ? '; Secure' : '');
  } catch (e) {}
}
function _cookieDel(name) {
  try { document.cookie = name + '=; Max-Age=0; Path=/'; } catch (e) {}
}
// 读凭证：localStorage → cookie（命中 cookie 时回写 localStorage，后续走快路径）
function credGet(key) {
  let v = '';
  try { v = localStorage.getItem(key) || ''; } catch (e) { v = ''; }
  if (v) return v;
  v = _cookieRead('akali_' + key);
  if (v) { try { localStorage.setItem(key, v); } catch (e) {} }
  return v;
}
// 写凭证：localStorage + cookie 双份
function credSet(key, val) {
  try { localStorage.setItem(key, val); } catch (e) {}
  _cookieWrite('akali_' + key, val);
}
function credDel(key) {
  try { localStorage.removeItem(key); } catch (e) {}
  _cookieDel('akali_' + key);
}

const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');

/* ================= 我的舰船·装备面板（用户本地副本管理） ================= */
const UP_ATK_FIELDS = ['炮击', '雷击', '航空', '轰炸', '鱼雷', '索敌', '命中', '幸运', '装填'];
const UP_DEF_FIELDS = ['装甲', '对空', '机动', '血量', '回避率', '防暴击率'];
// level：该舰「等级」输入（可选，null=留空）。与面板一起保存/读取，供遭遇战 PVE 等级压制。
let upState = { name: '', curAtk: {}, curDef: {}, baseAtk: {}, baseDef: {}, level: null };

// 面板存储位置：server=本地模式（服务器 user_data.json）/ client=云端模式（浏览器本地缓存）。
// 启动时由 /api/auth/status 返回的 panel_store 决定；缺省 server（与改造前一致）。
let ARKLY_PANEL_STORE = 'server';
// 当前账号内部 uid（服务器身份，由 /api/auth/status 或注册结果填入）。
// 用途：① 本地面缓存按账号隔离（同一浏览器换账号不串）；② 云端备份按 uid 存取。
let ARKLY_UID = '';

// 本地面缓存的键：有账号 → arkly_panels_<uid>；无账号（未鉴权）→ arkly_panels。
function panelKey() { return 'arkly_panels' + (ARKLY_UID ? ('_' + ARKLY_UID) : ''); }
function panelLSLoad() {
  try {
    const m = JSON.parse(localStorage.getItem(panelKey()) || '{}');
    return (m && typeof m === 'object') ? m : {};
  } catch (e) { return {}; }
}
function panelLSSave(m) { try { localStorage.setItem(panelKey(), JSON.stringify(m || {})); } catch (e) {} }
// 旧版单一键 arkly_panels → 迁移到当前账号名下（仅当前账号还没有数据时），并清掉旧键
function panelMigrateLegacy() {
  if (!ARKLY_UID) return;
  try {
    const old = localStorage.getItem('arkly_panels');
    if (old == null) return;
    const nk = panelKey();
    if (localStorage.getItem(nk) == null) localStorage.setItem(nk, old);
    localStorage.removeItem('arkly_panels');
  } catch (e) {}
}
// 云端模式：随 /api/chat 携带的面板覆盖（{角色名:{attack_panel,defense_panel,level}}）；本地模式返回 null
// level（可选）= 该舰「等级」，后端遭遇战按它算 PVE 等级压制（未填则按 Lv100 基线）
function panelOverrides() { return ARKLY_PANEL_STORE === 'client' ? panelLSLoad() : null; }
function panelMerge(base, ov) { return Object.assign({}, base || {}, ov || {}); }

// 面板云端备份（用户口径 2026-10-03）：云端只作备份、本地为主。
// 保存面板时推一份上去；进入时若本地为空则回填（本地有数据则不动）。
async function panelCloudPush() {
  if (ARKLY_PANEL_STORE !== 'client' || !ARKLY_UID || typeof upFetch !== 'function') return;
  try { await upFetch('/api/me/panel', { method: 'POST', body: JSON.stringify({ panels: panelLSLoad() }) }); } catch (e) {}
}
async function panelCloudRestore() {
  if (ARKLY_PANEL_STORE !== 'client' || !ARKLY_UID || typeof upFetch !== 'function') return;
  const local = panelLSLoad();
  if (local && Object.keys(local).length) return;      // 本地有 → 不回填
  try {
    const d = await upFetch('/api/me/panel');
    const p = d && d.panels;
    if (p && Object.keys(p).length) {
      panelLSSave(p);
      if (typeof upRefresh === 'function') upRefresh();
    }
  } catch (e) {}
}

/* ===== 本地面板库：全部舰船的底层基础面板（用户口径 2026-10-03）=====
   「载入全部基础面板」按钮一次把全部舰船的基础面板存到本机，之后在舰名框里
   直接选（带联想）即可载入编辑，不用逐个输舰名；单舰请求只做兜底。
   注意：这份库只用于「基线 + 选择联想」，**不会**混进随聊天携带的 panel_overrides
   —— 那份只放你真正改过的舰，否则每条消息都要多传几百 KB。 */
function panelLibKey() { return 'arkly_panels_lib' + (ARKLY_UID ? ('_' + ARKLY_UID) : ''); }
function panelLibLoad() {
  try {
    const m = JSON.parse(localStorage.getItem(panelLibKey()) || '{}');
    return (m && typeof m === 'object') ? m : {};
  } catch (e) { return {}; }
}
function panelLibSave(m) { try { localStorage.setItem(panelLibKey(), JSON.stringify(m || {})); } catch (e) {} }
// 用库里的舰名填舰名框的联想列表（<datalist>），并刷新状态提示
function panelLibFillDatalist() {
  const names = Object.keys(panelLibLoad());
  const dl = document.getElementById('upShipList');
  if (dl) dl.innerHTML = names.map(function (n) { return '<option value="' + esc(n) + '"></option>'; }).join('');
  const info = document.getElementById('upLibInfo');
  if (info) {
    info.textContent = names.length
      ? ('已载入基础面板 ' + names.length + ' 艘 —— 舰名框里可直接输入或下拉选择（本机保存，换设备/清缓存需重新载入）')
      : '';
  }
  return names.length;
}
// 「载入全部基础面板」：一次请求取全部舰船底层面板 → 存本机面板库 → 刷新联想列表
async function upLoadAllBase() {
  const btn = document.getElementById('upLoadAll');
  const _label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '载入中…'; }
  try {
    const d = await upFetch('/api/ship_panels');
    const ships = (d && d.ships) || {};
    const n = Object.keys(ships).length;
    if (!n) { alert('服务器没有返回基础面板数据'); return; }
    panelLibSave(ships);
    panelLibFillDatalist();
    alert('已载入 ' + n + ' 艘的基础面板到本机。\n之后在舰名框里直接输入或下拉选择，点「载入面板」即可编辑。');
  } catch (e) {
    alert('载入全部基础面板失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = _label || '载入全部基础面板'; }
  }
}

async function upFetch(url, opts) {
  const _t = credGet('arkly_api_token') || new URLSearchParams(location.search).get('token') || '';
  const _h = Object.assign({ 'Content-Type': 'application/json' }, _t ? { 'X-API-Token': _t } : {});
  const r = await fetch(url, Object.assign({ headers: _h }, opts || {}));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function upRenderFields(data) {
  const box = document.getElementById('upFields');
  upState.name = data.name;
  upState.curAtk = data.cur_atk || {};
  upState.curDef = data.cur_def || {};
  upState.baseAtk = data.base_atk || {};
  upState.baseDef = data.base_def || {};
  // 等级（可选）：回填输入框；没有/非法 → 留空（=不覆盖，后端按底层基线 Lv100 算压制）
  const _lv = parseFloat(data.level);
  upState.level = (isFinite(_lv) && _lv > 0) ? _lv : null;
  const _lvEl = document.getElementById('upLevel');
  if (_lvEl) _lvEl.value = (upState.level == null ? '' : upState.level);
  let html = '<div class="up-field"><label>攻击</label><span style="color:#64748b">（输入=你的副本当前值；灰字=底层基础值·默认无装备；仅装备属性，不含天赋/羁绊）</span></div>';
  UP_ATK_FIELDS.forEach(function (f) {
    const cur = upState.curAtk[f] || 0;
    const base = upState.baseAtk[f] || 0;
    const mark = Math.abs(cur - base) > 1e-9 ? ' style="border-color:#8b5cf6"' : '';
    html += '<div class="up-field"><label>' + f + '</label><input data-atk="1" data-f="' + f + '" value="' + (Math.round(cur * 100) / 100) + '"' + mark + '>'
      + '<span class="up-base">' + (Math.round(base * 100) / 100) + '</span></div>';
  });
  html += '<div class="up-field"><label>防御</label></div>';
  UP_DEF_FIELDS.forEach(function (f) {
    const cur = upState.curDef[f] || 0;
    const base = upState.baseDef[f] || 0;
    const mark = Math.abs(cur - base) > 1e-9 ? ' style="border-color:#8b5cf6"' : '';
    html += '<div class="up-field"><label>' + f + '</label><input data-def="1" data-f="' + f + '" value="' + (Math.round(cur * 100) / 100) + '"' + mark + '>'
      + '<span class="up-base">' + (Math.round(base * 100) / 100) + '</span></div>';
  });
  box.innerHTML = html;
}

// 读取「等级」输入：留空/非法 → null（=不覆盖，后端按 Lv100 基线）；越界钳制 1~200（与后端同口径）
function readUpLevel() {
  const el = document.getElementById('upLevel');
  if (!el) return null;
  const v = parseFloat(el.value);
  if (!isFinite(v) || v <= 0) return null;
  return Math.max(1, Math.min(200, Math.round(v * 100) / 100));
}

function upReadInputs() {
  const atkSet = {}, defSet = {}, delta = {};
  document.querySelectorAll('#upFields input').forEach(function (inp) {
    const v = parseFloat(inp.value);
    if (isNaN(v)) return;
    const f = inp.dataset.f;
    if (inp.dataset.atk) {
      atkSet[f] = v;
      delta[f] = v - (upState.curAtk[f] || 0);
    } else if (inp.dataset.def) {
      defSet[f] = v;
      delta[f] = v - (upState.curDef[f] || 0);
    }
  });
  return { atkSet: atkSet, defSet: defSet, delta: delta };
}

async function upLoadShip() {
  const name = document.getElementById('upShip').value.trim();
  if (!name) { alert('请输入舰名'); return; }
  try {
    if (ARKLY_PANEL_STORE === 'client') {
      // 云端：基础面板优先用**本地面板库**（点过「载入全部基础面板」就不必再请求）；
      // 库里没有（或输入的不是规范名）→ 单舰请求兜底，再回库取基线
      const lib = panelLibLoad();
      let shipName = '', baseAtk = {}, baseDef = {};
      if (lib[name]) {
        shipName = name;
        baseAtk = (lib[name].attack_panel) || {};
        baseDef = (lib[name].defense_panel) || {};
      } else {
        const b = await upFetch('/api/ship_panel?name=' + encodeURIComponent(name));
        if (!b.ship) { alert('未找到角色'); return; }
        shipName = b.ship.name;
        const lb = lib[shipName];
        baseAtk = (lb && lb.attack_panel) || b.ship.base_atk || {};
        baseDef = (lb && lb.defense_panel) || b.ship.base_def || {};
      }
      const ov = panelLSLoad()[shipName] || {};
      upRenderFields({
        name: shipName,
        base_atk: baseAtk, base_def: baseDef,
        cur_atk: panelMerge(baseAtk, ov.attack_panel),
        cur_def: panelMerge(baseDef, ov.defense_panel),
        level: ov.level                       // 本地缓存里存的等级（可选）
      });
      return;
    }
    const d = await upFetch('/api/user_ships?name=' + encodeURIComponent(name));
    if (!d.ship) { alert('未找到角色'); return; }
    upRenderFields(d.ship);
  } catch (e) { alert('载入失败：' + e.message); }
}

async function upSave(mode) {
  if (!upState.name) { alert('请先载入面板'); return; }
  const r = upReadInputs();
  if (ARKLY_PANEL_STORE === 'client') {
    // 云端：只存与底层不一致的字段，存绝对值为本地缓存
    const atk = {}, def = {};
    Object.keys(r.atkSet).forEach(function (k) {
      if (Math.abs((r.atkSet[k] || 0) - (upState.baseAtk[k] || 0)) > 1e-9) atk[k] = r.atkSet[k];
    });
    Object.keys(r.defSet).forEach(function (k) {
      if (Math.abs((r.defSet[k] || 0) - (upState.baseDef[k] || 0)) > 1e-9) def[k] = r.defSet[k];
    });
    const lv = readUpLevel();
    const m = panelLSLoad();
    if (Object.keys(atk).length || Object.keys(def).length || lv != null) {
      const obj = { attack_panel: atk, defense_panel: def };
      if (lv != null) obj.level = lv;  // 等级与面板一起存（可选，供遭遇战 PVE 等级压制）
      m[upState.name] = obj;
    } else delete m[upState.name];
    panelLSSave(m);
    panelCloudPush();                 // 推一份到云端备份（失败不影响本地）
    const diff = Object.assign({}, atk, def);
    upRefresh(); upLoadShip();
    alert('已保存「' + upState.name + '」到本机\n当前差异：' +
      (Object.keys(diff).length ? Object.entries(diff).map(function (e) { return e[0] + (e[1] > 0 ? '+' : '') + e[1]; }).join(' ') : '无')
      + '\n等级：' + (lv != null ? lv : '未设置（按底层基线 Lv100）'));
    return;
  }
  const body = { name: upState.name };
  if (mode === 'abs') { body.atk_set = r.atkSet; body.def_set = r.defSet; }
  else { body.delta = r.delta; }
  // 等级为绝对值：留空发 0（=清除该字段）；后端 >0 才写入
  const lvS = readUpLevel();
  body.level = (lvS == null ? 0 : lvS);
  try {
    const d = await upFetch('/api/user_ships', { method: 'POST', body: JSON.stringify(body) });
    alert('已保存「' + upState.name + '」' + (mode === 'abs' ? '（设为输入值）' : '（增量）') + '\n当前差异：' +
      (Object.keys(d.delta || {}).length ? Object.entries(d.delta).map(function (e) { return e[0] + (e[1] > 0 ? '+' : '') + e[1]; }).join(' ') : '无'));
    upRefresh();
    upLoadShip();
  } catch (e) { alert('保存失败：' + e.message); }
}

async function upRemove() {
  if (!upState.name) { alert('请先载入面板'); return; }
  if (!confirm('删除「' + upState.name + '」的面板修改，恢复底层默认面板？')) return;
  try {
    if (ARKLY_PANEL_STORE === 'client') {
      const m = panelLSLoad(); delete m[upState.name]; panelLSSave(m);
      panelCloudPush();               // 同步云端备份
    } else {
      await upFetch('/api/user_ships?name=' + encodeURIComponent(upState.name), { method: 'DELETE' });
    }
    alert('已删除「' + upState.name + '」');
    upState = { name: '', curAtk: {}, curDef: {}, baseAtk: {}, baseDef: {}, level: null };
    const _lvEl = document.getElementById('upLevel');
    if (_lvEl) _lvEl.value = '';
    document.getElementById('upFields').innerHTML = '<div class="up-note">输入舰名后点「载入面板」。</div>';
    upRefresh();
  } catch (e) { alert('删除失败：' + e.message); }
}

async function upRefresh() {
  const box = document.getElementById('upChips');
  try {
    let ships;
    if (ARKLY_PANEL_STORE === 'client') {
      const m = panelLSLoad();
      ships = await Promise.all(Object.keys(m).map(async function (n) {
        let baseAtk = {}, baseDef = {};
        try {
          const b = await upFetch('/api/ship_panel?name=' + encodeURIComponent(n));
          if (b.ship) { baseAtk = b.ship.base_atk || {}; baseDef = b.ship.base_def || {}; }
        } catch (e) {}
        const atk = (m[n] && m[n].attack_panel) || {}, def = (m[n] && m[n].defense_panel) || {};
        const delta = {};
        Object.keys(atk).forEach(function (k) { delta[k] = Math.round((atk[k] - (baseAtk[k] || 0)) * 100) / 100; });
        Object.keys(def).forEach(function (k) { delta[k] = Math.round((def[k] - (baseDef[k] || 0)) * 100) / 100; });
        return { name: n, delta: delta, level: (m[n] && m[n].level) || null };
      }));
    } else {
      const d = await upFetch('/api/user_ships');
      ships = d.ships || [];
    }
    if (!ships.length) { box.innerHTML = '<span class="up-note">暂无修改，副本=底层数据（所有计算用底层面板）。</span>'; return; }
    box.innerHTML = ships.map(function (s) {
      const diff = Object.entries(s.delta || {}).map(function (e) { return e[0] + (e[1] > 0 ? '+' : '') + e[1]; }).join(' ');
      // 悬停提示带上等级（有才显示），一眼看出该舰按多少级算 PVE 等级压制
      const lvTxt = (s.level ? ('等级 ' + s.level) : '');
      const title = [lvTxt, diff].filter(Boolean).join(' | ');
      return '<span class="up-chip" data-name="' + esc(s.name) + '" title="' + esc(title) + '">' + esc(s.name) + '</span>';
    }).join('');
    box.querySelectorAll('.up-chip').forEach(function (c) {
      c.addEventListener('click', function () {
        document.getElementById('upShip').value = c.dataset.name;
        upLoadShip();
      });
    });
  } catch (e) { box.innerHTML = '<span class="up-note">列表加载失败：' + esc(e.message) + '</span>'; }
}

(function () {
  const wrap = document.getElementById('upToggle');
  const panel = document.getElementById('userPanel');
  wrap.addEventListener('click', function () { panel.classList.toggle('open'); });
  document.getElementById('upLoad').addEventListener('click', upLoadShip);
  document.getElementById('upRefresh').addEventListener('click', upRefresh);
  const _upAllBtn = document.getElementById('upLoadAll');
  if (_upAllBtn) _upAllBtn.addEventListener('click', upLoadAllBase);
  panelLibFillDatalist();   // 恢复上次「载入全部基础面板」的舰名联想列表
  document.getElementById('upSaveAbs').addEventListener('click', function () { upSave('abs'); });
  document.getElementById('upSaveDelta').addEventListener('click', function () { upSave('delta'); });
  document.getElementById('upRemove').addEventListener('click', upRemove);
  document.getElementById('upShip').addEventListener('keydown', function (e) { if (e.key === 'Enter') upLoadShip(); });
  // 首次列表刷新交由 initAuth（拿到 panel_store 后再刷，避免云端模式误调服务器副本接口）
})();

// HTML 转义
function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 渲染消息正文：支持 ```代码块``` 与普通文本换行
function render(text) {
  const parts = text.split(/```/);
  let html = '';
  parts.forEach((p, i) => {
    if (i % 2 === 1) {
      // 代码块：去除首行语言标记（如 json）
      let body = p;
      const nl = body.indexOf('\n');
      if (nl > 0 && nl < 20 && !/\s/.test(body.slice(0, nl).trim())) {
        body = body.slice(nl + 1);
      }
      body = body.replace(/\n+$/, '');
      html += '<pre class="code"><code>' + esc(body) + '</code></pre>';
    } else {
      // 普通文本：真实换行 \n 与字面 "\n"（数据里的 \n 转义）都转 <br>
      html += esc(p).replace(/\\n/g, '<br>').replace(/\n/g, '<br>');
    }
  });
  // 兜底：去掉气泡首尾由换行产生的空行（如输入法上屏残留的回车），正文内部换行不受影响
  return html.replace(/^(?:<br>)+/, '').replace(/(?:<br>)+$/, '');
}

// 资源路径解析：云端（window.ASSET_BASE 非空）把 /kb/ /avatar/ 重写成 CDN 直链；本地原样。
// 覆盖由 API 响应动态返回的资源：头像标记、sticker、guide 图片、伤害公式图、默认头像。
function resolveAsset(p) {
  const base = window.ASSET_BASE || '';
  if (!base || !p) return p;
  if (p.startsWith('/kb/')) return base + p.slice(4);
  if (p.startsWith('/avatar/')) return base + '/图片素材' + p.slice(8);
  return p;
}

// 嵌入/跨域（浏览器插件）场景：把 HTML 里写死的 /kb/ /avatar/ 绝对路径改写到 ASSET_BASE，
// 同时把背景图 CSS 变量覆盖为服务器绝对地址 —— 否则扩展页会把这些路径解析到
// chrome-extension:// 域下导致图裂。同源（ASSET_BASE 为空）时本函数直接返回，行为不变。
(function fixStaticAssets() {
  const base = window.ASSET_BASE || '';
  if (!base) return;
  const map = (v) => {
    if (!v || v.charAt(0) !== '/') return v;
    if (v.startsWith('/kb/')) return base + v.slice(4);
    if (v.startsWith('/avatar/')) return base + '/图片素材' + v.slice(8);
    return v;
  };
  document.querySelectorAll('img[src^="/"], iframe[src^="/"], link[href^="/"]').forEach((el) => {
    ['src', 'href'].forEach((attr) => {
      const v = el.getAttribute(attr);
      if (v) el.setAttribute(attr, map(v));
    });
  });
  // CSS 变量（background 用）：与变量默认值同名覆盖即可
  const kbRoot = base.endsWith('/kb') ? base : base + '/kb';
  const css = document.documentElement.style;
  css.setProperty('--img-bg-day', "url('" + kbRoot + '/图片素材/背景/白天.png' + "')");
  css.setProperty('--img-bg-dawn', "url('" + kbRoot + '/图片素材/背景/清晨.png' + "')");
  css.setProperty('--img-bg-evening', "url('" + kbRoot + '/图片素材/背景/黄昏.png' + "')");
  css.setProperty('--img-bg-night', "url('" + kbRoot + '/图片素材/背景/夜晚.png' + "')");
  css.setProperty('--img-akali', "url('" + kbRoot + '/图片素材/阿卡莉.png' + "')");
})();

// 阿卡莉头像：表情差分（启动时 /api/ui_assets 提供默认头像=「常态」差分；差分为空回退底层立绘）
const ARKLY_FALLBACK_AVATAR = resolveAsset('/kb/图片素材/表情差分/常态.jpg');
let ARKLY_AVATAR = ARKLY_FALLBACK_AVATAR;
// 表情差分 名字→URL 映射（来自 /api/ui_assets 的 expression_urls），供双击头像随机切换表情
let ARKLY_EXPR_URLS = [];
// 指挥官（用户）头像：与秘书侧一样走图片素材（默认 指挥官头像/用户.png；
// 独立于 表情差分 神态目录，避免污染 LLM 神态词清单导致 <<EXP:用户>> 误切秘书脸）
// 指挥官（用户）头像：初始值作为回退；服务端 /api/ui_assets 会用带 ?v=mtime 的实时 URL 覆盖，
// 并由下方轮询保持「换图即更新」。
let USER_AVATAR = resolveAsset('/kb/图片素材/指挥官头像/用户.png');

function addMsg(role, text, avatarUrl) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  // 自己的头像挂提示：双击（手机长按）可看自己的 uid 与口令（事件见下方委托绑定）
  const ava = role === 'user'
    ? '<img loading="lazy" src="' + esc(USER_AVATAR) + '" alt="指挥官" title="双击（手机长按）查看我的 UID 与口令">'
    : '<img loading="lazy" src="' + esc(resolveAsset(avatarUrl || ARKLY_AVATAR)) + '" alt="阿卡莉">';
  wrap.innerHTML = '<div class="ava">' + ava + '</div>'
                 + '<div class="bubble">' + (role === 'user' ? '' : '<div class="role-tag">阿卡莉</div>')
                 + render(text) + '</div>';
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

// ===== 我的身份卡片（用户口径 2026-09-25）=====
// 双击自己的头像（手机端长按）弹出，显示 uid 与口令 —— 换设备 / 清了缓存后，
// 在主站解锁框里填这个口令即可回到原账号（不填则会自动注册成一个新的 uid）。
// 口令默认打码，点「显示」才明文，免得旁边的人一眼看到。
function _maskToken(s) {
  if (!s) return '（未获取到）';
  if (s.length <= 6) return '••••••';
  return s.slice(0, 3) + '••••••' + s.slice(-2);
}

function showMyIdentity() {
  let uid = '';
  const token = credGet('arkly_api_token') || '';
  const ov = document.createElement('div');
  ov.className = 'me-card-mask';
  ov.innerHTML =
      '<div class="me-card">'
    +   '<h3>我的身份</h3>'
    +   '<div class="sub">换设备或清了浏览器缓存后，在主站输入下面的口令，就能回到这个账号。建议截图或复制保存。</div>'
    +   '<div class="me-row"><span class="k">UID</span><span class="v" id="meUid">—</span>'
    +     '<button type="button" data-copy="">复制</button></div>'
    +   '<div class="me-row"><span class="k">口令</span><span class="v" id="meTok">' + esc(_maskToken(token)) + '</span>'
    +     '<button type="button" id="meEye">显示</button>'
    +     '<button type="button" data-copy="' + esc(token) + '">复制</button></div>'
    +   '<div class="me-actions"><button type="button" id="meClose">关闭</button></div>'
    + '</div>';
  document.body.appendChild(ov);

  let shown = false;
  ov.querySelector('#meEye').addEventListener('click', function () {
    shown = !shown;
    ov.querySelector('#meTok').textContent = shown ? (token || '（未获取到）') : _maskToken(token);
    this.textContent = shown ? '隐藏' : '显示';
  });
  ov.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      const v = b.getAttribute('data-copy') || '';
      if (!v) { b.textContent = '无'; setTimeout(function () { b.textContent = '复制'; }, 1000); return; }
      const done = function () {
        b.textContent = '已复制';
        setTimeout(function () { b.textContent = '复制'; }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(v).then(done).catch(function () {
          b.textContent = '请手动选';        // 无剪贴板权限（http 非安全上下文等）时提示手抄
          setTimeout(function () { b.textContent = '复制'; }, 1600);
        });
      } else {
        b.textContent = '请手动选';
        setTimeout(function () { b.textContent = '复制'; }, 1600);
      }
    });
  });
  ov.querySelector('#meClose').addEventListener('click', function () { ov.remove(); });
  ov.addEventListener('click', function (e) { if (e.target === ov) ov.remove(); });

  // uid 到手后回填（口令本地即时可取，uid 需问服务端）
  fetch('/api/me', { headers: _apiHeaders() })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      uid = (d && (d.user_uid || d.uid)) || '';
      const el = ov.querySelector('#meUid');
      if (el && uid) el.textContent = uid;
      const cp = ov.querySelector('[data-copy=""]');
      if (cp) cp.setAttribute('data-copy', uid);
    })
    .catch(function () {});
}

// 触发方式：PC 单击自己的头像；手机长按（事件委托，动态消息也能命中）
messagesEl.addEventListener('click', function (e) {
  if (e.target && e.target.closest && e.target.closest('.msg.user .ava')) showMyIdentity();
});
var _identityLp = null;
messagesEl.addEventListener('touchstart', function (e) {
  if (!(e.target && e.target.closest && e.target.closest('.msg.user .ava'))) return;
  _identityLp = setTimeout(function () { _identityLp = null; showMyIdentity(); }, 550);
}, { passive: true });
['touchend', 'touchmove', 'touchcancel'].forEach(function (ev) {
  messagesEl.addEventListener(ev, function () {
    if (_identityLp) { clearTimeout(_identityLp); _identityLp = null; }
  }, { passive: true });
});

// 打开攻略预览弹层（docx→iframe 转HTML；png/jpg→img；xlsx→下载）
function openGuidePreview(g) {
  const ext = (g.path.split('.').pop() || '').toLowerCase();
  // 预览/下载走浏览器直接请求（iframe/img/a），须在 URL 上带 token（外部访问时防 401）
  const t = credGet('arkly_api_token') || '';
  const q = t ? '?token=' + encodeURIComponent(t) : '';
  const url = resolveAsset('/kb/' + encodeURI(g.path));
  let body = '';
  if (ext === 'png' || ext === 'jpg' || ext === 'jpeg') {
    // 图片类攻略：**直接加载**（用户口径 2026-10-04）。预览是用户主动点开的，懒加载
    // 只会让大图（攻略图常见数 MB）在空弹窗里等滚动才出图；改 eager + 异步解码，
    // 并在未就绪时给占位提示（onload/onerror 时替换/移除）。
    body = '<div class="guide-img-tip" id="guideImgTip">图片加载中…（攻略图较大，请稍候）</div>'
      + '<img loading="eager" decoding="async" src="' + url + '" alt="' + esc(g.title) + '"'
      + ' onload="var t=document.getElementById(\'guideImgTip\');if(t)t.remove();"'
      + ' onerror="var t=document.getElementById(\'guideImgTip\');if(t)t.textContent=\'图片加载失败。可点右上角「⬇ 下载」保存到本地查看。\';">';
  } else if (ext === 'docx' || ext === 'doc') {
    // 预览路由 /kb_html/ 不在 ASSET_BASE 的 /kb/ 映射内：跨域/扩展场景需显式拼 API 基址
    const _htmlBase = String(window.ARKLY_API_BASE || '').replace(/\/$/, '');
    body = '<iframe src="' + _htmlBase + '/kb_html/' + encodeURI(g.path) + q + '"></iframe>';
  } else {
    body = '<div class="dl-tip">该文件为数据表，请下载查看。<br><br><a class="guide-link" href="' + url + '" target="_blank">下载 ' + esc(g.title) + '</a></div>';
  }
  const mask = document.createElement('div');
  mask.className = 'guide-preview-mask';
  mask.innerHTML = '<div class="guide-preview">'
    + '<div class="guide-preview-head"><b>' + esc(g.title) + '</b>'
    + '<button class="guide-preview-download">⬇ 下载</button>'
    + '<button class="guide-preview-close">关闭</button></div>'
    + '<div class="guide-preview-body">' + body + '</div></div>';
  document.body.appendChild(mask);
  mask.querySelector('.guide-preview-close').addEventListener('click', () => mask.remove());
  mask.querySelector('.guide-preview-download').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = resolveAsset('/kb/' + encodeURI(g.path));
    a.download = (g.title || 'guide') + (g.path.split('.').pop() || '');
    document.body.appendChild(a); a.click(); a.remove();
  });
  mask.addEventListener('click', (e) => { if (e.target === mask) mask.remove(); });
}

// 使用者手册弹窗：首次注册成功 / 聊天「想看说明」意图触发，复用 .sb-mask 风格，
// 内含 iframe 载入 /manual，底部「确认/关闭」。onClose 为关闭后回调（首次注册用于进应用）。
function openManual(onClose) {
  if (document.getElementById('manual-mask')) return;
  const mask = document.createElement('div');
  mask.id = 'manual-mask';
  mask.className = 'sb-mask';
  mask.innerHTML =
    '<div class="sb-box" style="height:min(82vh,720px);">'
    + '<div class="sb-head"><b>使用说明</b>'
    + '<button id="manualClose" style="border:none;background:var(--accent,#6b8caa);color:#fff;border-radius:8px;padding:6px 14px;font-size:14px;cursor:pointer;">确认/关闭</button></div>'
    + '<iframe src="/manual" style="flex:1;border:0;width:100%;background:#fff;"></iframe>'
    + '</div>';
  document.body.appendChild(mask);
  const close = () => { mask.remove(); if (typeof onClose === 'function') onClose(); };
  mask.querySelector('#manualClose').addEventListener('click', close);
  mask.addEventListener('click', e => { if (e.target === mask) close(); });
}

// 攻略库面板：fetch 索引 → 渲染列表。
// 全目录模式（focusKw='*'）：分类滑块 + 搜索框；
// 具体主题模式：滑块只显示回复携带的 guide_files（scopeFiles，"回复了啥就给啥"），无搜索/无分类。
// 下载按钮在打开预览后的预览窗口右上角。
async function openGuideLib(focusKw, msgEl, scopeFiles) {
  try {
    const resp = await fetch('/api/guides', { headers: _apiHeaders() });
    const data = await resp.json();
    const guides = data.guides || [];
    if (!guides.length && !(scopeFiles && scopeFiles.length)) return;
    const wrap = document.createElement('div');
    wrap.className = 'guide-lib';
    const CATS = ['萌新入坑', '一图速查', '练级须知', '进阶必备'];
    const kw = (focusKw || '').trim();
    const isAll = !kw || kw === '*';   // 全目录（攻略大全）模式
    const specific = (scopeFiles && scopeFiles.length) ? scopeFiles : null;   // 具体主题：回复携带的文档
    const hitCat = CATS.find(c => kw.includes(c) || c.includes(kw)) || '';
    // 头部标题 + 搜索框（仅全目录模式）
    const headLabel = specific ? '相关攻略（' + specific.length + ' 篇）'
      : hitCat ? hitCat + '（' + guides.filter(g => g.category === hitCat).length + ' 篇）'
      : '攻略合集（' + guides.length + ' 篇）';
    wrap.innerHTML = '<div class="guide-lib-head"><b>' + headLabel + '</b>'
      + (isAll ? '<input class="guide-lib-search" placeholder="搜索攻略，如 遭遇战 / 新人 / 练级 / 配装…">' : '')
      + '</div>'
      + (isAll ? '<div class="guide-lib-tabs"><button class="gl-tab active" data-cat="">全部</button>'
        + CATS.map(c => '<button class="gl-tab" data-cat="' + c + '">' + c + '</button>').join('')
        + '</div>' : '')
      + '<div class="guide-lib-list"></div>';
    const bubble = msgEl.querySelector('.bubble');
    bubble.appendChild(wrap);
    const searchEl = wrap.querySelector('.guide-lib-search');
    const tabsEl = wrap.querySelector('.guide-lib-tabs');
    const listEl = wrap.querySelector('.guide-lib-list');
    const tabs = tabsEl ? Array.from(tabsEl.querySelectorAll('.gl-tab')) : [];
    let curCat = hitCat;
    const extName = (kind) => kind === '.docx' || kind === '.doc' ? '文档'
      : (kind === '.png' || kind === '.jpg' || kind === '.jpeg') ? '图片' : '表格';
    function kindOf(p) { const e = (p || '').split('.').pop().toLowerCase(); return '.' + e; }
    function renderItem(g, title, kind, summary) {
      const item = document.createElement('div');
      item.className = 'guide-lib-item';
      const sm = summary ? '<div class="gl-summary">' + esc(String(summary).slice(0, 110)) + '</div>' : '';
      item.innerHTML = '<div class="gl-title"><span>' + esc(title) + '</span><span class="gl-tag">' + extName(kind) + '</span></div>' + sm;
      item.addEventListener('click', () => openGuidePreview({ path: g.path, title: title, kind: kind }));
      listEl.appendChild(item);
    }
    function renderSpecific() {
      // 具体主题：精确渲染回复携带的 guide_files（按标题匹配索引拿 summary/kind，匹配不到直接用返回字段）
      listEl.innerHTML = '';
      specific.forEach(f => {
        const t = (f.title || '').trim();
        const hit = guides.find(g => g.title === t || (t && (t.includes(g.title) || g.title.includes(t))));
        const kind = hit ? hit.kind : (f.kind || kindOf(f.path || ''));
        const sm = f.summary || (hit && hit.summary) || '';
        renderItem({ path: f.path }, t, kind, sm);
      });
      if (!listEl.children.length) listEl.innerHTML = '<div class="guide-lib-empty">未找到匹配攻略</div>';
    }
    function renderList(kw2) {
      if (specific) { renderSpecific(); return; }
      const q = (kw2 || '').trim().toLowerCase();
      let arr = guides;
      if (curCat) arr = arr.filter(g => g.category === curCat);
      if (q && q !== '*') arr = arr.filter(g => (g.title + ' ' + (g.tags || []).join(' ')).toLowerCase().includes(q));
      if (!arr.length) { listEl.innerHTML = '<div class="guide-lib-empty">未找到匹配攻略</div>'; return; }
      listEl.innerHTML = '';
      arr.forEach(g => renderItem(g, g.title, g.kind, g.summary));
    }
    tabs.forEach(btn => btn.addEventListener('click', () => {
      curCat = btn.dataset.cat || '';
      tabs.forEach(b => b.classList.toggle('active', b === btn));
      renderList(searchEl ? searchEl.value : '');
    }));
    if (searchEl) searchEl.addEventListener('input', () => renderList(searchEl.value));
    // 初始渲染：具体主题→回复携带的文档；分类直达（命中分类名）→按 category 显示该分类；
    // 全目录→全部；其他关键词→按词过滤
    if (specific) {
      renderSpecific();
    } else if (hitCat) {
      renderList('');
    } else if (isAll) {
      renderList('');
    } else {
      renderList(kw);
    }
  } catch (e) { /* 面板加载失败则忽略 */ }
}

// 历史上的故事：点击按钮 → 拉取原型舰历史 → 弹窗展示
async function openShipStory(encodedName) {
  const name = decodeURIComponent(encodedName || '');
  if (!name) return;
  const ta = credGet('arkly_api_token') || '';
  const qa = ta ? '?token=' + encodeURIComponent(ta) : '';
  try {
    const resp = await fetch('/api/ship_story?name=' + encodeURIComponent(name) + qa, { headers: _apiHeaders() });
    const d = await resp.json();
    if (!d.found) { alert('该角色暂无原型舰历史资料'); return; }
    showStoryModal(d.story);
  } catch (e) { alert('历史故事加载失败，请稍后再试'); }
}

function showStoryModal(st) {
  const old = document.getElementById('story-modal');
  if (old) old.remove();
  const wrap = document.createElement('div');
  wrap.id = 'story-modal';
  wrap.className = 'story-modal';
  const rows = [
    ['原型舰', st.real_ship], ['所属', st.country], ['舰级', st.ship_class],
    ['年份', st.launched], ['结局', st.fate],
  ].filter(r => {
    const v = r[1];
    return v !== undefined && v !== null && String(v) !== '' && String(v) !== 'None';
  });
  let html = '<div class="story-modal-box"><div class="story-modal-head"><b>📖 ' + esc(st.ship_name || '') + ' · 历史上的故事</b>'
    + '<button class="story-modal-close">✕</button></div><div class="story-modal-body">';
  html += '<div class="story-desc">' + esc(st.history || '') + '</div>';
  html += rows.map(r => '<div class="story-row"><span class="story-k">' + esc(r[0]) + '</span><span class="story-v">' + esc(String(r[1])) + '</span></div>').join('');
  html += '</div></div>';
  wrap.innerHTML = html;
  document.body.appendChild(wrap);
  wrap.querySelector('.story-modal-close').addEventListener('click', () => wrap.remove());
  wrap.addEventListener('click', e => { if (e.target === wrap) wrap.remove(); });
}

/* ================= 查角色面板（悬浮球「查角色」/ 输入查询角色时打开） =================
   与开发者端「角色查询」同款：全角色名单 + 阵营筛选 + 头像；点角色卡查看详情。
   数据源：/api/ships（名录）与 /api/ship_detail（详情）——后端只读，前端只渲染。 */
const SB_NATION_ORDER = ['凤栖', '洛蒙瑞亚', '尤奈特', '纳谢尔', '沃尔克', '奥鲁加', '八咫', '列加杜', '伽纳伊'];
let sbCache = null;   // {ships:[], byNation:{}}
let sbState = { nation: '全部', keyword: '', view: 'normal' };   // view: normal=普通版(改造前) / retro=改造界面
let sbModal = null;

async function sbFetchShips() {
  if (sbCache) return sbCache;
  const resp = await fetch('/api/ships', { headers: _apiHeaders() });
  const d = await resp.json();
  const ships = d.ships || [];
  const retrofit = d.retrofit || [];
  const byNation = {};
  ships.forEach(s => { const n = s.nation || '未知阵营'; (byNation[n] = byNation[n] || []).push(s); });
  sbCache = { ships: ships, retrofit: retrofit, byNation: byNation };
  return sbCache;
}
// 稀有度档位顺序（从高到低），用于分档标题与分组
const SB_RARITY_ORDER = ['彩', '金', '紫', '蓝', '绿', '白'];
function sbRarityGroups(list) {
  const g = {};
  list.forEach(s => { const r = s.rarity || '其他'; (g[r] = g[r] || []).push(s); });
  const keys = SB_RARITY_ORDER.filter(r => g[r]).concat(Object.keys(g).filter(r => SB_RARITY_ORDER.indexOf(r) < 0));
  return keys.map(r => ({ rarity: r, items: g[r] }));
}
function sbCardHtml(s, big) {
  return '<div class="sb-card" data-name="' + esc(s.name) + '">'
    + '<img src="' + sbAvatarUrl(s.avatar) + '" alt="' + esc(s.name) + '" loading="lazy">'
    + '<div class="sb-name">' + esc(s.name) + '</div>'
    + '<div class="sb-meta">' + esc(s.ship_type || '')
    + (s.rarity ? ' · ' + esc(s.rarity) : '') + '</div></div>';
}
function sbTabList(ships) {
  const cnt = {};
  ships.forEach(s => { const n = s.nation || '未知阵营'; cnt[n] = (cnt[n] || 0) + 1; });
  const extras = Object.keys(cnt).filter(n => SB_NATION_ORDER.indexOf(n) < 0 && n !== '未知阵营').sort();
  const tabs = ['全部'].concat(SB_NATION_ORDER.filter(n => cnt[n]))
    .concat(extras).concat(cnt['未知阵营'] ? ['未知阵营'] : []);
  // 改造界面作为独立的末位大按钮（与阵营筛选并列、互不混用）
  tabs.push('__retro__');
  return tabs;
}
function sbAvatarUrl(p) {
  if (!p) return resolveAsset('/kb/图片素材/表情差分/常态.jpg');
  const t = credGet('arkly_api_token') || '';
  const q = t ? '?token=' + encodeURIComponent(t) : '';
  return resolveAsset('/avatar/' + String(p).replace(/\\/g, '/')) + q;
}
function closeShipBrowser() {
  if (sbModal) { sbModal.remove(); sbModal = null; }
}
// 打开面板；focusName 非空时直接进该角色详情（输入「查丹阳」等）
async function openShipBrowser(focusName) {
  closeShipBrowser();
  sbState = { nation: '全部', keyword: '', view: 'normal' };
  sbModal = document.createElement('div');
  sbModal.className = 'sb-mask';
  sbModal.innerHTML =
    '<div class="sb-box">'
    + '<div class="sb-head"><span class="sb-title">⚓ 查角色</span>'
    + '<input class="sb-search" id="sbSearch" placeholder="搜索角色名 / 昵称…">'
    + '<button class="sb-close" title="关闭">✕</button></div>'
    + '<div class="sb-tabs" id="sbTabs"></div>'
    + '<div class="sb-body" id="sbBody"><div class="sb-empty">加载中…</div></div>'
    + '</div>';
  document.body.appendChild(sbModal);
  sbModal.querySelector('.sb-close').addEventListener('click', closeShipBrowser);
  sbModal.addEventListener('click', function (e) { if (e.target === sbModal) closeShipBrowser(); });
  document.getElementById('sbSearch').addEventListener('input', function () {
    sbState.keyword = this.value.trim();
    sbRenderGrid();
  });
  try { await sbFetchShips(); } catch (e) {
    document.getElementById('sbBody').innerHTML = '<div class="sb-empty">角色名录加载失败，请稍后再试。</div>';
    return;
  }
  if (focusName) { sbShowDetail(focusName); }
  else { sbRenderTabs(); sbRenderGrid(); }
}
function sbRenderTabs() {
  const tabsEl = document.getElementById('sbTabs');
  if (!tabsEl || !sbCache) return;
  tabsEl.innerHTML = sbTabList(sbCache.ships).map(function (t) {
    if (t === '__retro__') {
      const on = sbState.view === 'retro' ? ' on' : '';
      return '<button class="sb-tab sb-tab-retro' + on + '" data-n="__retro__">🔧 改造舰灵</button>';
    }
    const on = (sbState.view === 'normal' && t === sbState.nation) ? ' on' : '';
    return '<button class="sb-tab' + on + '" data-n="' + esc(t) + '">' + esc(t) + '</button>';
  }).join('');
  tabsEl.querySelectorAll('.sb-tab').forEach(function (b) {
    b.addEventListener('click', function () {
      if (b.dataset.n === '__retro__') { sbState.view = 'retro'; }
      else { sbState.view = 'normal'; sbState.nation = b.dataset.n; }
      sbRenderTabs(); sbRenderGrid();
    });
  });
}
function sbRenderGrid() {
  const body = document.getElementById('sbBody');
  if (!body || !sbCache) return;
  sbRenderTabs();
  if (sbState.view === 'retro') { sbRenderRetrofit(); return; }
  const kw = sbState.keyword;
  let list = sbState.nation === '全部' ? sbCache.ships : (sbCache.byNation[sbState.nation] || []);
  if (kw) list = list.filter(function (s) { return s.name.indexOf(kw) >= 0; });
  if (!list.length) { body.innerHTML = '<div class="sb-empty">没有符合条件的角色。</div>'; return; }
  const groups = sbRarityGroups(list);
  let html = '<div class="sb-count">共 ' + list.length + ' 名'
    + (sbState.nation === '全部' ? ' · 按稀有度排序' : ' · ' + esc(sbState.nation)) + '</div>';
  groups.forEach(function (g) {
    html += '<div class="sb-rarity-h">' + esc(g.rarity) + '</div>'
      + '<div class="sb-grid">' + g.items.map(function (s) { return sbCardHtml(s, false); }).join('') + '</div>';
  });
  body.innerHTML = html;
  body.querySelectorAll('.sb-card').forEach(function (c) {
    c.addEventListener('click', function () { sbShowDetail(c.dataset.name); });
  });
}
// 改造界面：独立、更大的网格，改造后角色（名含「·改」）按稀有度排序展示
function sbRenderRetrofit() {
  const body = document.getElementById('sbBody');
  if (!body || !sbCache) return;
  const kw = sbState.keyword;
  const list = (sbCache.retrofit || []).filter(function (s) {
    return !kw || s.name.indexOf(kw) >= 0;
  });
  if (!list.length) {
    body.innerHTML = '<div class="sb-empty">'
      + (kw ? '没有符合条件的改造舰灵。' : '暂无任何改造舰灵资料。') + '</div>';
    return;
  }
  const groups = sbRarityGroups(list);
  let html = '<div class="sb-count sb-count-retro">改造舰灵 共 ' + list.length + ' 名（按稀有度排序）</div>';
  groups.forEach(function (g) {
    html += '<div class="sb-rarity-h">' + esc(g.rarity) + '</div>'
      + '<div class="sb-grid sb-grid-big">' + g.items.map(function (s) { return sbCardHtml(s, true); }).join('') + '</div>';
  });
  body.innerHTML = html;
  body.querySelectorAll('.sb-card').forEach(function (c) {
    c.addEventListener('click', function () { sbShowDetail(c.dataset.name); });
  });
}
async function sbShowDetail(name) {
  const body = document.getElementById('sbBody');
  if (!body) return;
  body.innerHTML = '<div class="sb-empty">加载中…</div>';
  try {
    const resp = await fetch('/api/ship_detail?name=' + encodeURIComponent(name), { headers: _apiHeaders() });
    const d = await resp.json();
    if (!d.found) { body.innerHTML = '<div class="sb-empty">未找到该角色。</div>'; return; }
    let html = '<div class="sb-detail-actions"><button class="sb-tab" id="sbBack">← 返回名单</button></div>'
      + '<div class="sb-detail">';
    if (d.avatar) html += '<img loading="lazy" class="sb-detail-ava" src="' + sbAvatarUrl(d.avatar) + '" alt="' + esc(d.name) + '">';
    html += '<div class="sb-detail-text">' + esc(d.text || '') + '</div></div>';
    if (d.story) html += '<div class="sb-detail-actions"><button class="story-btn" data-story="'
      + encodeURIComponent(d.name) + '">📖 历史上的故事</button></div>';
    body.innerHTML = html;
    const back = document.getElementById('sbBack');
    if (back) back.addEventListener('click', sbRenderGrid);
    body.querySelectorAll('.story-btn').forEach(function (b) {
      b.addEventListener('click', function () { openShipStory(b.getAttribute('data-story')); });
    });
  } catch (e) {
    body.innerHTML = '<div class="sb-empty">详情加载失败，请稍后再试。</div>';
  }
}

// 渲染秘书回复（含可选的填空表单或计算类型按钮）
function renderSecretaryReply(data, msgEl) {
  // 正式回复到达：移除换算力拉满中的脉动占位态
  if (msgEl && msgEl.classList) msgEl.classList.remove('computing');
  // 神态差分头像：后端按 LLM 神态标记（<<EXP:神态>>）解析出的差分图，替换本条消息头像
  if (msgEl && data.expression) {
    const expImg = msgEl.querySelector('.ava img');
    if (expImg) expImg.src = resolveAsset(data.expression);
  }
  let html = '<div class="role-tag">阿卡莉</div>';
  // 伤害公式一图流：后端调用伤害计算工具时固定附带（top_image），渲染在回复文本之前
  if (data.top_image && data.top_image.path) {
    const tf = credGet('arkly_api_token') || '';
    const qf = tf ? '?token=' + encodeURIComponent(tf) : '';
    html += '<div class="calc-formula-wrap"><img loading="lazy" class="calc-formula-img" src="' + resolveAsset('/kb/' + esc(data.top_image.path)) + qf
      + '" alt="' + esc(data.top_image.title || '伤害公式一图流') + '"></div>';
  }
  html += render(data.reply || '（无回复）');
  // 头像标记 <<AVATAR>>path|title：查询角色时由后端插入到「--- 名字 ---」行后，
  // render() 已把 < 转义为 &lt;，这里把转义后的标记替换为头像图片（名字下面的位置）
  if (/&lt;&lt;AVATAR&gt;&gt;/.test(html)) {
    const ta = credGet('arkly_api_token') || '';
    const qa = ta ? '?token=' + encodeURIComponent(ta) : '';
    html = html.replace(/&lt;&lt;AVATAR&gt;&gt;([^<\n]+?)\|([^<\n]+)/g, function (m, p, t) {
      return '<div class="avatar-wrap"><img loading="lazy" class="avatar-img" src="' + resolveAsset('/avatar/' + p.replace(/\\/g, '/')) + qa
        + '" alt="' + t + '"></div>';
    });
  }
  // 原型史标记 <<STORY>>舰名：渲染为「历史上的故事」小按钮，点击弹窗查看
  if (/&lt;&lt;STORY&gt;&gt;/.test(html)) {
    html = html.replace(/&lt;&lt;STORY&gt;&gt;([^<\n]+)/g, function (m, name) {
      return '<button class="story-btn" data-story="' + encodeURIComponent(name) + '">📖 历史上的故事</button>';
    });
  }
  // 打捞活动标记 <<SALVAGE_HL>>标题：渲染为醒目红字高亮
  // 注意：render() 已把换行转成 <br>，此处必须以 < 为边界（同 AVATAR/STORY 标记），
  // 否则 [^\n]* 会一路吞到末尾，导致整段回复全部变红。
  if (/&lt;&lt;SALVAGE_HL&gt;&gt;/.test(html)) {
    html = html.replace(/&lt;&lt;SALVAGE_HL&gt;&gt;([^<\n]*)/g, function (m, t) {
      return '<span class="salvage-hl">' + t + '</span>';
    });
  }
  // 第一步：识别到算伤害 → 渲染填空表单（不渲染按钮）
  if (Array.isArray(data.fill_form) && data.fill_form.length) {
    // 填空期间禁用底部输入框
    inputEl.disabled = true;
    inputEl.placeholder = '请点击上方表单按钮选择...';
    // buff 字段说明只在让用户输入 buff（攻击方/防守方）时才展示；
    // 航向/制空、角色选择等表单不显示（避免无关字段干扰）
    const hasBuffField = data.fill_form.some(f => f.key === 'attacker_buff' || f.key === 'defender_buff');
    if (hasBuffField) {
    // 固定渲染完整 buff 字段说明（开头格式提示 + 字段名列表）
    html += '<div class="fill-hint">';
    html += '<div class="hint-title">默认誓约满好感带20%全增伤和20%全减伤。数值支持百分比（如 +20%）和负数（如 炮击伤害-20%）；面板相关支持固定值和百分比，示例：航空+15%，航空+200；多字段用顿号、逗号或空格分隔，示例：全伤害+15%、暴击率+20%。字段无法识别将提示重新填空输入。</div>';
    html += '<div class="hint-group"><b>攻击方buff</b>（可选）：</div>';
    html += '<div class="hint-group">增伤类：<code>全伤害</code> <code>炮击伤害</code> <code>雷击伤害</code> <code>航空伤害</code> <code>轰炸伤害</code> <code>鱼雷伤害</code> <code>近战伤害</code> <code>高平炮伤害</code> <code>普攻伤害</code></div>';
    html += '<div class="hint-group">大小型加成：<code>对小型伤害增加</code> <code>对中型伤害增加</code> <code>对大型伤害增加</code></div>';
    html += '<div class="hint-group">舰种专属增伤：<code>对驱逐伤害增加</code> <code>对巡洋伤害增加</code> <code>对战列伤害增加</code> <code>对航母伤害增加</code> <code>对轻母伤害增加</code> <code>对雷巡伤害增加</code> <code>对战巡伤害增加</code> <code>对装母伤害增加</code></div>';
    html += '<div class="hint-group">注：拉古兹增伤由角色技能自带（无需输入），仅在遭遇战模式生效</div>';
    html += '<div class="hint-group">暴击类：<code>暴击率</code> <code>暴击伤害</code></div>';
    html += '<div class="hint-group">点燃类：<code>点燃抵抗</code></div>';
    html += '<div class="hint-group">面板类：<code>炮击</code> <code>雷击</code> <code>航空</code> <code>轰炸</code> <code>鱼雷</code> <code>装甲</code> <code>对空</code> <code>机动</code> <code>命中</code> <code>幸运</code> <code>装填</code> <code>血量</code> <code>攻击力</code></div>';
    html += '<div class="hint-group" style="margin-top:6px"><b>防守方buff</b>（可选）：</div>';
    html += '<div class="hint-group">减免类：<code>全伤害减免</code> <code>炮击伤害减免</code> <code>雷击伤害减免</code> <code>航空伤害减免</code> <code>轰炸伤害减免</code> <code>鱼雷伤害减免</code> <code>近战伤害减免</code></div>';
    html += '<div class="hint-group">大小型减免：<code>对小型伤害减免</code> <code>对中型伤害减免</code> <code>对大型伤害减免</code></div>';
    html += '<div class="hint-group">舰种专属减伤：<code>对驱逐伤害减免</code> <code>对巡洋伤害减免</code> <code>对战列伤害减免</code> <code>对航母伤害减免</code> <code>对轻母伤害减免</code> <code>对雷巡伤害减免</code></div>';
    html += '<div class="hint-group">闪避类：<code>回避率</code> <code>防暴击率</code></div>';
    html += '</div>';
    }
    html += '<div class="fill-form">';
    let hasEnv = false;   // 是否含 阵型/航向/制空 选择（三行按钮）
    data.fill_form.forEach(f => {
      if (f.key === 'course' || f.key === 'air') {
        if (!hasEnv) {
          hasEnv = true;
          // 阵型/航向/制空 一次性给出三行按钮 + 默认按钮
          html += '<div class="env-panel">'
            + '<button class="env-default">默认（机动 / 同航 / 制空均势）</button>'
            + '<div class="env-row"><span class="env-label">阵型</span>'
            + ['单纵（炮击、雷击伤害+12%）', '复纵（机动+25%）', '轮型（航空、对空+15%，炮击、雷击-5%）', '梯型（暴击率+15%，暴击伤害+5%）'].map(function (o) { return '<button class="env-opt" data-env="formation" data-val="' + o.split('（')[0] + '">' + o + '</button>'; }).join('')
            + '</div>'
            + '<div class="env-row"><span class="env-label">航向</span>'
            + ['T优（炮击、雷击伤害+15%，命中+20%）', '同航（炮击、雷击伤害+8%，命中+10%）', '反航（炮击、雷击伤害-8%，命中-10%）', 'T劣（炮击、雷击伤害-15%，命中-10%）'].map(function (o) { return '<button class="env-opt" data-env="course" data-val="' + o.split('（')[0] + '">' + o + '</button>'; }).join('')
            + '</div>'
            + '<div class="env-row"><span class="env-label">制空</span>'
            + ['确保（航空伤害+15%，命中+10%）', '优势（航空伤害+8%，命中+5%）', '均势（无修正）', '劣势（航空伤害-8%，命中-5%）', '丧失（航空伤害-15%，命中-10%）'].map(function (o) { return '<button class="env-opt" data-env="air" data-val="' + o.split('（')[0] + '">' + o + '</button>'; }).join('')
            + '</div>'
            + '<button class="env-confirm">确认选择</button></div>';
        }
        return; // 跳过普通输入框
      }
      const req = f.required ? ' <span class="fill-req">*</span>' : '';
      const val = f.value || '';
      const errCls = f.error ? ' fill-input-error' : '';
      html += '<div class="fill-row"><label class="fill-label">' + esc(f.label) + req + '</label>';
      html += '<input class="fill-input' + errCls + '" data-key="' + esc(f.key) + '" data-required="' + (f.required ? '1' : '0') + '" placeholder="' + esc(f.placeholder) + '" value="' + esc(val) + '">';
      if (f.error && f.error_msg) {
        html += '<div class="fill-error-msg">' + esc(f.error_msg) + '</div>';
      }
      html += '</div>';
    });
    if (!hasEnv) html += '<button class="fill-submit">填好了，继续</button>'
                       + '<button class="fill-cancel">结束本轮</button></div>';
    msgEl.querySelector('.bubble').innerHTML = html;
    // 阵型/航向/制空 面板：默认按钮 / 选项单选 / 确认发送
    const envPanel = msgEl.querySelector('.env-panel');
    if (envPanel) {
      const envSel = { formation: '', course: '', air: '' };
      const envBtn = (name) => envPanel.querySelector('.env-' + name);
      const syncEnv = () => {
        const sel = [];
        if (envSel.formation) sel.push('阵型：' + envSel.formation);
        if (envSel.course) sel.push('航向：' + envSel.course);
        if (envSel.air) sel.push('制空：' + envSel.air);
        return sel.join(' ');
      };
      envPanel.querySelectorAll('.env-opt').forEach(btn => {
        btn.addEventListener('click', () => {
          envPanel.querySelectorAll('.env-opt[data-env="' + btn.dataset.env + '"]').forEach(b => b.classList.remove('env-on'));
          btn.classList.add('env-on');
          envSel[btn.dataset.env] = btn.dataset.val;
        });
      });
      envBtn('default').addEventListener('click', () => sendChat('默认'));
      envBtn('confirm').addEventListener('click', () => {
        const text = syncEnv();
        sendChat(text || '默认');
      });
    } else {
      msgEl.querySelector('.fill-submit').addEventListener('click', () => {
        const inputs = msgEl.querySelectorAll('.fill-input');
        const labelMap = {'attacker':'攻击方','attacker_buff':'攻击方buff','attacker_talent':'攻击方天赋','defender':'防守方','defender_panel':'守方面板','defender_buff':'防守方buff','defender_talent':'防守方天赋','course':'航向','air':'制空'};
        const parts = [];
        // 只提交表单中实际渲染的字段（支持分步表单：攻击方 → buff/天赋 → 防守方 → buff/天赋）
        let missing = false;
        const nonEmptyParts = [];
        inputs.forEach(inp => {
          const key = inp.dataset.key;
          let val = inp.value.trim();
          if (inp.dataset.required === '1' && !val) { missing = true; }
          // 航向/制空未填 → 用默认值（同航/均势），不写"无"
          if (key === 'course' && !val) val = '同航';
          if (key === 'air' && !val) val = '均势';
          // 空字段不写"无"：跳过；等价用户接受默认值（buff/天赋默认由后端与提示词处理）
          if (val) nonEmptyParts.push(labelMap[key] + '：' + val);
        });
        if (missing) { alert('请填写必填项'); return; }
        const firstKey = inputs[0] ? inputs[0].dataset.key : '';
        if (firstKey === 'attacker' || firstKey === 'defender') {
          // 角色选择步骤：只发送角色名；防守方可带 面板（N=赋值、±N=增量）+防御buff
          const roleVal = inputs[0].value.trim();
          if (!roleVal) { alert('请填写角色名'); return; }
          if (firstKey === 'defender') {
            const panelInp = msgEl.querySelector('.fill-input[data-key="defender_panel"]');
            const buffInp = msgEl.querySelector('.fill-input[data-key="defender_buff"]');
            const panelVal = panelInp ? panelInp.value.trim() : '';
            const buffVal = buffInp ? buffInp.value.trim() : '';
            let send = '防守方：' + roleVal;
            if (panelVal) send += ' 面板：' + panelVal;
            if (buffVal) send += ' 防御buff：' + buffVal;
            sendChat(send);
          } else {
            sendChat(labelMap[firstKey] + '：' + roleVal);
          }
        } else {
          // 有填写内容就发内容；全部为空发"继续"（后端按默认值确认）
          sendChat(nonEmptyParts.length ? nonEmptyParts.join(' ') : '继续');
        }
      });
      // 表单内「结束本轮」：与页脚「中断」同路径（发「结束本轮」→ 后端清 calc_state）。
      // 用户口径 2026-09-24：此前填表时只能去页脚找中断，且打字反驳会被当成角色名。
      const _cancelBtn = msgEl.querySelector('.fill-cancel');
      if (_cancelBtn) {
        _cancelBtn.addEventListener('click', () => sendChat('结束本轮'));
      }
    }
    // 填空输入按回车 = 点击确认按钮（普通表单）
    msgEl.querySelectorAll('.fill-input').forEach(inp => {
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const btn = msgEl.querySelector('.fill-submit');
          if (btn) btn.click();
        }
      });
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return;
  }
  // 好感度缺参引导 → 自动弹出「好感度计算器」网页表单（后端 favor_form 标记，不走文本多轮）
  if (data.favor_form) {
    setTimeout(openFavorForm, 60);
  }
  // 遭遇战模拟表单
  if (Array.isArray(data.encounter_form) && data.encounter_form.length) {
    // 有字段报错时，把后端返回的「错误汇总」显示在表单上方（正常引导时不显示，保持原样）
    const hasError = data.encounter_form.some(f => f.error);
    if (hasError && data.reply) {
      html += '<div class="fill-summary">' + esc(data.reply).replace(/\n/g, '<br>') + '</div>';
    }
    html += '<div class="fill-hint">';
    // 按表单字段区分：PVP（defender 字段）显示 PVP 提示，遭遇战（boss 字段）显示遭遇战提示
    const isPvpForm = data.encounter_form.some(f => f.key === 'defender');
    if (isPvpForm) {
      html += '<div class="hint-title">模拟组队：选 6 人（不分前后排、不算支援技，普攻/通常技/必杀技）、1 名旗舰、1 个防守角色，无需 buff，直接计算一定时间内的全队伤害。</div>';
      html += '<div class="hint-group" style="margin-top:6px"><b>战斗时长</b>：总时长 120s，分「白天」与「黑夜」——填白天（0~120 秒），黑夜自动 = 120 − 白天，无需输入。</div>';
      html += '<div class="hint-group" style="margin-top:6px"><b>旗舰</b>：在任意角色名旁勾选「旗舰」（有且仅能选 1 个），该角色的旗舰技光环将对全队生效。</div>';
    } else {
      html += '<div class="hint-title">遭遇战模拟：点选 boss、填写白天时间（黑夜自动 = 150 - 白天）、前排 3 + 后排 3 角色名。</div>';
      html += '<div class="hint-group">规则：前3为前排（普攻/通常技/必杀技），后3为后排（仅支援技）；0-5s 禁止交换，之后每次交换 CD=10s（按换人发起时刻计）；换人不停摆，但通常技释放后 2s 内换人会被后摇卡住、推迟 1s 才换成功；黑夜 boss 装甲减 90%，航母/轻母黑夜禁用。拉古兹增伤由角色技能自带，无需输入。</div>';
      html += '<div class="hint-group" style="margin-top:6px"><b>旗舰</b>：在任意角色名旁勾选「旗舰」（有且仅能选 1 个），该角色的旗舰技光环将对全队生效。增益不论前后排都生效，只取决于增益条件与时间。</div>';
    }
    html += '</div>';
    html += '<div class="fill-form">';
    // 单字段行（boss/day_time/defender/duration）
    const encRow = (f) => {
      const req = f.required ? ' <span class="fill-req">*</span>' : '';
      const val = f.value || '';
      const errCls = f.error ? ' fill-input-error' : '';
      const chk = f.flagship ? ' checked' : '';
      let h = '<div class="fill-row"><label class="fill-label">' + esc(f.label) + req + '</label>';
      if (f.key === 'boss' && Array.isArray(f.options) && f.options.length) {
        // boss 按钮选择（点击写入隐藏 input，提交逻辑复用 .fill-input 读取）
        h += '<div class="fill-boss-list">';
        f.options.forEach(b => {
          const sel = f.value === b ? ' env-on' : '';
          h += '<button type="button" class="env-opt fill-boss' + sel + '" data-boss="' + esc(b) + '">' + esc(b) + '</button>';
        });
        h += '</div>';
        h += '<input type="hidden" class="fill-input' + errCls + '" data-key="boss" value="' + esc(val) + '">';
      } else {
        h += '<input class="fill-input' + errCls + '" data-key="' + esc(f.key) + '" placeholder="' + esc(f.placeholder) + '" value="' + esc(val) + '">';
      }
      if (f.error && f.error_msg) h += '<div class="fill-error-msg">' + esc(f.error_msg) + '</div>';
      if (f.key.startsWith('front') || f.key.startsWith('back')) {
        h += '<label class="fill-flagship"><input type="checkbox" class="fill-flag" data-ship="' + esc(f.key) + '"' + chk + '>旗舰</label>';
      }
      return h + '</div>';
    };
    const encCell = (f) => {
      const req = f.required ? ' <span class="fill-req">*</span>' : '';
      const val = f.value || '';
      const errCls = f.error ? ' fill-input-error' : '';
      const chk = f.flagship ? ' checked' : '';
      const isShip = f.key.startsWith('front') || f.key.startsWith('back');
      let h = '<div class="fill-cell">';
      if (isShip) {
        // 旗舰勾选与「角色N」标签同一行（不与输入框同排，避免抢输入宽度）
        h += '<div class="fill-label-row"><label class="fill-label">' + esc(f.label) + req + '</label>';
        h += '<label class="fill-flagship"><input type="checkbox" class="fill-flag" data-ship="' + esc(f.key) + '"' + chk + '>旗舰</label>';
        h += '</div>';
      } else {
        h += '<label class="fill-label">' + esc(f.label) + req + '</label>';
      }
      h += '<input class="fill-input' + errCls + '" data-key="' + esc(f.key) + '" placeholder="' + esc(f.placeholder) + '" value="' + esc(val) + '">';
      if (f.error && f.error_msg) h += '<div class="fill-error-msg">' + esc(f.error_msg) + '</div>';
      return h + '</div>';
    };
    data.encounter_form.forEach(f => {
      if (f.key.startsWith('front') || f.key.startsWith('back')) return;
      html += encRow(f);
    });
    // 前排 3 个一行
    const frontFields = data.encounter_form.filter(f => f.key.startsWith('front'));
    if (frontFields.length) {
      html += '<div class="fill-grid-label">前排</div><div class="fill-grid-3">';
      frontFields.forEach(f => { html += encCell(f); });
      html += '</div>';
    }
    // 后排 3 个一行
    const backFields = data.encounter_form.filter(f => f.key.startsWith('back'));
    if (backFields.length) {
      html += '<div class="fill-grid-label">后排</div><div class="fill-grid-3">';
      backFields.forEach(f => { html += encCell(f); });
      html += '</div>';
    }
    if (isPvpForm) html += '<div class="fill-night-note"></div>';
    html += '<button class="fill-submit">填好了，开始模拟</button></div>';
    msgEl.querySelector('.bubble').innerHTML = html;
    // 遭遇战表单：填空按回车 = 开始模拟
    msgEl.querySelectorAll('.fill-input').forEach(inp => {
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const btn = msgEl.querySelector('.fill-submit');
          if (btn) btn.click();
        }
      });
    });
    // boss 按钮：单选写入隐藏 input（提交逻辑复用 .fill-input 读取）
    msgEl.querySelectorAll('.fill-boss').forEach(btn => {
      btn.addEventListener('click', () => {
        const inp = msgEl.querySelector('.fill-input[data-key="boss"]');
        if (inp) inp.value = btn.dataset.boss;
        msgEl.querySelectorAll('.fill-boss').forEach(b => { if (b !== btn) b.classList.remove('env-on'); });
        btn.classList.add('env-on');
      });
    });
    // 旗舰单选：勾选新的自动取消上一个
    msgEl.querySelectorAll('.fill-flag').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) {
          msgEl.querySelectorAll('.fill-flag').forEach(o => { if (o !== cb) o.checked = false; });
        }
      });
    });
    // PVP 战斗时长：白天输入 → 实时显示 黑夜 = 120 - 白天
    if (isPvpForm) {
      const dayInp = msgEl.querySelector('.fill-input[data-key="day_time"]');
      const note = msgEl.querySelector('.fill-night-note');
      const upd = () => {
        let d = parseFloat(dayInp && dayInp.value);
        if (isNaN(d)) d = 0;
        d = Math.max(0, Math.min(120, d));
        if (note) note.textContent = '战斗时长：白天 ' + d + 's ｜ 黑夜 ' + (120 - d) + 's（黑夜 = 120 − 白天，总 120s）';
      };
      if (dayInp) dayInp.addEventListener('input', upd);
      upd();
    }
    msgEl.querySelector('.fill-submit').addEventListener('click', () => {
      const inputs = msgEl.querySelectorAll('.fill-input');
      const isPvp = Array.from(inputs).some(inp => inp.dataset.key === 'defender');
      let bossVal = '', dayVal = '', flagshipVal = '', defenderVal = '';
      const front = [], back = [];
      inputs.forEach(inp => {
        const key = inp.dataset.key;
        const val = inp.value.trim();
        if (key === 'boss') bossVal = val;
        else if (key === 'day_time') dayVal = val;
        else if (key === 'defender') defenderVal = val;
        else if (key.startsWith('front')) front.push(val);
        else if (key.startsWith('back')) back.push(val);
      });
      msgEl.querySelectorAll('.fill-flag').forEach(cb => {
        if (cb.checked) {
          const inp = msgEl.querySelector('.fill-input[data-key="' + cb.dataset.ship + '"]');
          flagshipVal = inp ? inp.value.trim() : '';
        }
      });
      if (isPvp) {
        if (!defenderVal) { alert('请填写防守角色名'); return; }
        if (front.length !== 3 || front.some(v => !v) || back.length !== 3 || back.some(v => !v)) { alert('请填写全部 6 个角色名'); return; }
        if (!flagshipVal) { alert('模拟组队必须指定 1 名旗舰（在角色名右侧勾选「旗舰」）'); return; }
        if (front.concat(back).indexOf(flagshipVal) === -1) { alert('旗舰必须是这 6 个角色之一'); return; }
        const dayNum = parseFloat(dayVal);
        if (dayVal === '' || isNaN(dayNum) || dayNum < 0 || dayNum > 120) { alert('请填写白天战斗时长（0~120 秒，黑夜自动 = 120 - 白天）'); return; }
        const nightNum = 120 - dayNum;
        let msg = '模拟pvp 防守：' + defenderVal + ' 白天：' + dayNum + ' 黑夜：' + nightNum + ' 前排：' + front.join(' ') + ' 后排：' + back.join(' ');
        if (flagshipVal) msg += ' 旗舰：' + flagshipVal;
        sendChat(msg);
        return;
      }
      if (!bossVal) { alert('请选择 boss'); return; }
      if (!dayVal) { alert('请填写白天时间（黑夜自动 = 150 - 白天）'); return; }
      if (front.length !== 3 || front.some(v => !v) || back.length !== 3 || back.some(v => !v)) { alert('请填写全部 6 个角色名'); return; }
      if (flagshipVal) {
        const allNames = front.concat(back);
        if (allNames.indexOf(flagshipVal) === -1) { alert('旗舰必须是前排或后排中的角色'); return; }
      }
      let msg = '遭遇战模拟 boss：' + bossVal + ' 白天：' + dayVal + ' 前排：' + front.join(' ') + ' 后排：' + back.join(' ');
      if (flagshipVal) msg += ' 旗舰：' + flagshipVal;
      sendChat(msg);
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return;
  }
  // 索敌计算：后端 scout_form 标记 → 两种模式可切换的填空表单
  if (data.scout_form) {
    html += '<div class="fill-hint">';
    html += '<div class="hint-title">索敌计算：选模式，填参数。</div>';
    html += '<div class="hint-group">① <b>数值索敌线</b>：填我方索敌值，分别算「我方进攻/敌方防守」「敌方进攻/我方防守」的 T优 / T劣 线（不列具体 buff）。</div>';
    html += '<div class="hint-group">② <b>队伍最高索敌</b>：<b>必须填满 6 名角色</b>；每角色 = 基础索敌×(1+各组索敌天赋%之和) + 98×2 + 额外索敌，再 ×乘算系数，求和下取整。1/2/3/4 每组各可选 1 个索敌天赋。</div>';
    html += '<div class="hint-group">例：<code>胡德·改 14*3+10 1.1</code> </div>';
    html += '</div>';
    html += '<div class="fill-form">';
    html += '<div class="env-row scout-modes">'
          + '<button type="button" class="env-opt scout-mode env-on" data-mode="line">数值索敌线</button>'
          + '<button type="button" class="env-opt scout-mode" data-mode="team">队伍最高索敌</button>'
          + '</div>';
    // 模式一：数值索敌线
    html += '<div class="scout-pane" data-pane="line">';
    html += '<div class="fill-row"><label class="fill-label">我方索敌数值 <span class="fill-req">*</span></label>'
          + '<input class="fill-input" data-key="scout_value" placeholder="如 1000"></div>';
    html += '<button class="fill-submit">算索敌线</button></div>';
    // 模式二：队伍最高索敌（6 名角色全部必填，额外算式/系数可留空）
    html += '<div class="scout-pane" data-pane="team" style="display:none">';
    html += '<div class="scout-toolbar">'
          + '<span class="scout-toolbar-label">统一系数</span>'
          + '<input class="fill-input scout-mult-all" placeholder="如 1.1（默认 1.0）">'
          + '<button type="button" class="env-opt scout-fill-all">一键填入全部</button>'
          + '</div>';
    // 列名只出现一次（表头），6 行输入框不放重复占位提示
    html += '<div class="scout-head"><span class="scout-idx"></span>'
          + '<span>角色名</span><span>额外索敌（可空）</span><span>乘算系数（可空）</span></div>';
    let scoutRows = '';
    for (let i = 1; i <= 6; i++) {
      scoutRows += '<div class="scout-team-row"><span class="scout-idx">' + i + '</span>'
                 + '<input class="fill-input" data-key="scout_name_' + i + '">'
                 + '<input class="fill-input" data-key="scout_extra_' + i + '">'
                 + '<input class="fill-input" data-key="scout_mult_' + i + '">'
                 + '</div>';
    }
    html += scoutRows;
    html += '<button class="fill-submit">算最高索敌</button></div>';
    html += '</div>';
    msgEl.querySelector('.bubble').innerHTML = html;
    // 模式切换
    msgEl.querySelectorAll('.scout-mode').forEach(btn => {
      btn.addEventListener('click', () => {
        msgEl.querySelectorAll('.scout-mode').forEach(b => b.classList.toggle('env-on', b === btn));
        msgEl.querySelectorAll('.scout-pane').forEach(p => {
          p.style.display = (p.dataset.pane === btn.dataset.mode) ? '' : 'none';
        });
      });
    });
    // 一键填入全部系数：把「统一系数」写进 6 行（留空 = 1.0）
    const fillAllBtn = msgEl.querySelector('.scout-fill-all');
    if (fillAllBtn) {
      fillAllBtn.addEventListener('click', () => {
        const pane = msgEl.querySelector('.scout-pane[data-pane="team"]');
        const v = (pane.querySelector('.scout-mult-all').value || '').trim() || '1.0';
        pane.querySelectorAll('.fill-input[data-key^="scout_mult_"]').forEach(inp => { inp.value = v; });
      });
    }
    // 提交：模式一 → 「索敌 <数值>」；模式二 → 「索敌队伍 名 算式 系数, …」（6 名必须填满）
    msgEl.querySelectorAll('.scout-pane').forEach(pane => {
      const mode = pane.dataset.pane;
      pane.querySelector('.fill-submit').addEventListener('click', () => {
        if (mode === 'line') {
          const v = (pane.querySelector('.fill-input[data-key="scout_value"]').value || '').trim();
          if (!v) { alert('请填写我方索敌数值'); return; }
          sendChat('索敌 ' + v);
          return;
        }
        const segs = [];
        const missing = [];
        for (let i = 1; i <= 6; i++) {
          const nm = (pane.querySelector('.fill-input[data-key="scout_name_' + i + '"]').value || '').trim();
          if (!nm) { missing.push(i); continue; }
          const ex = (pane.querySelector('.fill-input[data-key="scout_extra_' + i + '"]').value || '').trim();
          let mt = (pane.querySelector('.fill-input[data-key="scout_mult_' + i + '"]').value || '').trim();
          let seg = nm;
          if (ex) seg += ' ' + ex;
          if (mt) {
            if (/^\d+$/.test(mt)) mt = mt + '.0';   // 纯整数系数补小数点，避免被当成「额外索敌+整数」
            seg += ' ' + mt;
          }
          segs.push(seg);
        }
        if (missing.length) { alert('队伍最高索敌必须填满 6 名角色，第 ' + missing.join('、') + ' 行还空着'); return; }
        sendChat('索敌队伍 ' + segs.join(', '));
      });
    });
    // 回车 = 提交当前模式；「统一系数」输入框回车 = 一键填入
    msgEl.querySelectorAll('.scout-pane .fill-input').forEach(inp => {
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          if (inp.classList.contains('scout-mult-all')) {
            const b = msgEl.querySelector('.scout-fill-all');
            if (b) b.click();
            return;
          }
          const p = inp.closest('.scout-pane');
          const btn = p && p.querySelector('.fill-submit');
          if (btn) btn.click();
        }
      });
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return;
  }
  // 经验值计算：后端 exp_form 标记 → 填空表单（等级 / 当前经验 / 出战经验可选）
  if (data.exp_form) {
    html += '<div class="fill-hint">';
    html += '<div class="hint-title">经验值计算：填当前等级与经验，自动算剩余经验。</div>';
    html += '<div class="hint-group">出战经验（每场）可留空——填了会一并算出「所需出击场数」与「三倍经验场次」；ge 晶体按 500 经验/个 折算。</div>';
    html += '</div>';
    html += '<div class="fill-form">';
    html += '<div class="fill-row"><label class="fill-label">当前等级 <span class="fill-req">*</span></label>'
          + '<input class="fill-input" data-key="exp_level" placeholder="1-100"></div>';
    html += '<div class="fill-row"><label class="fill-label">当前经验值（本级已累积） <span class="fill-req">*</span></label>'
          + '<input class="fill-input" data-key="exp_current" value="0"></div>';
    html += '<div class="fill-row"><label class="fill-label">出战经验（每场，可空）</label>'
          + '<input class="fill-input" data-key="exp_per_battle"></div>';
    html += '<button class="fill-submit">开始计算</button></div>';
    msgEl.querySelector('.bubble').innerHTML = html;
    msgEl.querySelector('.fill-submit').addEventListener('click', () => {
      const lv = (msgEl.querySelector('.fill-input[data-key="exp_level"]').value || '').trim();
      const cur = (msgEl.querySelector('.fill-input[data-key="exp_current"]').value || '').trim() || '0';
      const bat = (msgEl.querySelector('.fill-input[data-key="exp_per_battle"]').value || '').trim();
      if (!/^\d+$/.test(lv)) { alert('请填写当前等级（数字，1-100）'); return; }
      if (!/^\d+$/.test(cur)) { alert('当前经验请填数字（可填 0）'); return; }
      if (bat && !/^\d+$/.test(bat)) { alert('出战经验请填数字（或留空）'); return; }
      let m = '经验计算 等级：' + lv + ' 当前经验：' + cur;
      if (bat) m += ' 出战经验：' + bat;
      sendChat(m);
    });
    msgEl.querySelectorAll('.fill-input').forEach(inp => {
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const b = msgEl.querySelector('.fill-submit');
          if (b) b.click();
        }
      });
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return;
  }
  // 第二步：填空提交后 → 渲染计算类型按钮
  if (Array.isArray(data.suggestions) && data.suggestions.length) {
    html += '<div class="suggestions">';
    data.suggestions.forEach(s => {
      const cls = s.tool === 'end_round' ? 'sug-btn sug-end' : 'sug-btn';
      const cmdAttr = s.cmd ? ' data-cmd="' + esc(s.cmd) + '"' : '';
      html += '<button class="' + cls + '" data-tool="' + esc(s.tool || '') + '"' + cmdAttr + '>' + esc(s.label) + '</button>';
    });
    html += '</div>';
  }
  // 攻略文档不再渲染成回复里的目录卡片（避免"预览文档"堆在回复里）——
  // 文档只出现在攻略库滑块弹窗（openGuideLib 用 guide_files 精确渲染）
  // 计算过程折叠块：工具输出的计算明细（伤害计算过程等）默认收起，用户主动点开查看
  if (Array.isArray(data.tool_outputs) && data.tool_outputs.length) {
    // 只匹配**伤害计算引擎产出的明细**。判定收紧（用户口径 2026-09-24）：此前只要含
    // 「实际伤害」四个字就折叠展示，而攻略正文里也有这句（如《争锋竞技场简易百科》的
    // 「实际伤害增幅为1.45/1.2-1=0.2083」），于是整段带相似度的检索原文被当成计算明细
    // 显示给了用户。检索原文（含「（相似度 0.85）」标注与多个片段）只供开发者排障，
    // 不得从阿卡莉界面输出；现在要求命中计算引擎的强特征。
    const calcDetails = data.tool_outputs.filter(o => o && (o.indexOf('伤害计算结果') !== -1
      || /^\s*[=-]{10,}/.test(o) || o.indexOf('点燃实际伤害') !== -1));
    if (calcDetails.length) {
      html += '<details class="calc-details"><summary>查看详细计算过程（点击展开）</summary><pre class="calc-detail-body">'
        + esc(calcDetails.join('\n\n')) + '</pre></details>';
    }
  }
  // 遭遇战详细伤害计算折叠块：每次技能的完整运算式子（含 buff 明细），默认收起。
  // 与上方单体计算的折叠块共享 .calc-details 样式；此处为结构化字段，不依赖 tool_outputs 特征匹配。
  if (data.encounter_detail) {
    html += '<details class="calc-details"><summary>展开详细伤害计算（每次技能运算式子 · 含 buff 明细）</summary>'
      + '<pre class="calc-detail-body">' + esc(data.encounter_detail) + '</pre></details>';
  }
  // 遭遇战操作时间轴 SVG：直接内联渲染模拟产出的时间轴图（必杀释放 / 交换前后排）
  if (data.encounter_timeline_svg) {
    const svgStr = data.encounter_timeline_svg;
    const b64 = btoa(unescape(encodeURIComponent(svgStr)));
    html += '<div class="encounter-timeline">'
      + '<div class="etl-head">⏱ 操作时间轴（必杀释放 / 交换前后排）<span class="etl-hint">← 左右拖动查看 →</span></div>'
      + '<div class="etl-canvas">' + svgStr + '</div>'
      + '<div class="etl-actions"><a class="etl-dl" download="encounter_timeline.svg"'
      + ' href="data:image/svg+xml;base64,' + b64 + '">下载 SVG</a></div>'
      + '</div>';
  }
  // 表情包（sticker）：后端返回时直接渲染图片（如资源不足追问 → 「氪啊」），
  // 不走 guide_files/攻略面板，避免被攻略意图门控清除
  if (data.sticker && data.sticker.path) {
    const t2 = credGet('arkly_api_token') || '';
    const q2 = t2 ? '?token=' + encodeURIComponent(t2) : '';
    html += '<div class="sticker-wrap"><img loading="lazy" class="sticker-img" src="' + resolveAsset('/kb/' + esc(data.sticker.path)) + q2
      + '" alt="' + esc(data.sticker.title || '表情包') + '"></div>';
  }
  msgEl.querySelector('.bubble').innerHTML = html;
  // 历史上的故事按钮：绑定点击 → 调 /api/ship_story 弹窗
  msgEl.querySelectorAll('.story-btn').forEach(btn => {
    btn.addEventListener('click', () => openShipStory(btn.getAttribute('data-story')));
  });
  // 攻略库面板：仅当后端确认命中攻略意图（guide_lib 非空）时才弹出，
  // 空串/未命中一律不弹（避免闲聊/查询时误弹攻略面板）
  if (data.guide_lib) {
    openGuideLib(data.guide_lib, msgEl, data.guide_files);
  }
  // 查角色面板：后端 ship_browser 标记（输入查询角色时）→ 打开角色面板，详情在面板展开
  // （不再把面板/技能底层文本直接塞进对话回复）；带 ship_focus 时直接定位该角色
  if (data.ship_browser) {
    setTimeout(function () { openShipBrowser(data.ship_focus || ''); }, 40);
    // 回复内附「查全部角色」按钮：弹窗被误关后可再次打开总角色名录（用户口径 2026-09-14）
    try {
      const bubble = msgEl.querySelector('.bubble');
      if (bubble) {
        const rb = document.createElement('button');
        rb.type = 'button';
        rb.className = 'sb-reopen-btn';
        rb.textContent = '🔍 查全部角色';
        rb.addEventListener('click', function () { openShipBrowser(''); });
        bubble.appendChild(rb);
      }
    } catch (e) { /* 不影响主流程 */ }
  }
  msgEl.querySelectorAll('.sug-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const tool = btn.dataset.tool;
      const label = btn.textContent.trim();
      const cmd = btn.dataset.cmd;
      if (tool === 'end_round') { sendChat('结束本轮计算'); return; }
      if (tool === 'encounter_simulator') { sendChat('遭遇战模拟'); return; }
      if (cmd) { sendChat(cmd); return; }   // 计算工具子菜单：优先发 cmd 指令短语，回到对应流程
      try {
        const sessResp = await fetch('/api/session?session_id=' + encodeURIComponent(_sessionId()), { headers: _apiHeaders() });
        const sessData = await sessResp.json();
        const ctx = sessData.session_overrides?.calc_context;
        let prefix = label;
        if (ctx?.attacker && ctx?.enemy) {
          prefix = label + '：' + ctx.attacker + '打' + ctx.enemy;
        }
        sendChat(prefix);
      } catch (err) { sendChat(label); }
    });
  });
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// 发送消息到 /api/chat
// 会话隔离：浏览器本地生成稳定 session_id（localStorage），多人/多标签互不串；
// token 鉴权：支持 URL 带 ?token=xxx（自动记忆）或 localStorage 存口令
function _sessionId() {
  let sid = credGet('arkly_session_id');
  if (!sid) { sid = 'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8); credSet('arkly_session_id', sid); }
  return sid;
}
function _apiHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const q = new URLSearchParams(location.search);
  const t = credGet('arkly_api_token') || q.get('token') || '';
  if (t) { credSet('arkly_api_token', t); h['X-API-Token'] = t; }
  return h;
}
// 算力提醒（用户口径 2026-09-25）：用户**不需要常驻查看**用量 ——
// 只在剩余不足 10% 时自动提醒：「本周系统算力不足10%」。
//   · 底部出现一条警示横条（阈值以上整条隐藏）；
//   · 同时在对话里让阿卡莉主动说一句（每会话仅一次，避免每轮刷屏）。
// 具体 token / 成本用户看不到 —— /api/quota 只回 remain_percent，明细走 /admin。
var QUOTA_ALERT_PCT = 10;
function checkQuotaAlert() {
  const bar = document.getElementById('quotaBar');
  const txt = document.getElementById('quotaText');
  fetch('/api/quota', { headers: _apiHeaders() })
    .then(r => (r.ok ? r.json() : null))
    .then(d => {
      const p = (d && !d.unlimited && d.cloud_mode !== false) ? Number(d.remain_percent) : NaN;
      // 移动端右上角角标：只要拿到有效百分比就常驻显示（桌面端被 CSS 隐藏）
      const fabBallEl = document.getElementById('fabBall');
      var pctTxt = isNaN(p) ? '' : Math.max(0, Math.round(p)) + '%';
      // 悬浮球环形底色：剩余算力百分比（≥30% 绿 / 10%~30% 黄 / <10% 红，中心蓝底白字）
      if (fabBallEl) {
        // 开发者/不限量模式：p 为 NaN，按"满额"处理，整圈显示绿色
        const unlimited = isNaN(p);
        fabBallEl.style.setProperty('--p', unlimited ? 100 : Math.max(0, Math.round(p)));
        // 环形三档配色：≥30 绿，10~30 黄，<10 红（不限量直接绿）
        let ring = '#2bb673', danger = false;
        if (!unlimited) {
          if (p < 10) { ring = '#e74c3c'; danger = true; }
          else if (p < 30) { ring = '#f1c40f'; }
        }
        fabBallEl.style.setProperty('--ring', ring);
        fabBallEl.classList.toggle('low', danger);
        const pctEl = document.getElementById('fabPct');
        if (pctEl) pctEl.textContent = pctTxt;
      }
      if (isNaN(p) || p > QUOTA_ALERT_PCT) { if (bar) bar.style.display = 'none'; return; }
      const msg = '本周系统算力不足' + QUOTA_ALERT_PCT + '%';
      if (bar && txt) { txt.textContent = msg + '（基础功能不受影响，下周四零点自动恢复）'; bar.style.display = ''; }
      let told = '';
      try { told = sessionStorage.getItem('arkly_quota_alert') || ''; } catch (e) {}
      if (told) return;
      try { sessionStorage.setItem('arkly_quota_alert', '1'); } catch (e) {}
      // 让阿卡莉主动提醒一次（addMsg 定义在其后，运行时已就绪）
      if (typeof addMsg === 'function') {
        addMsg('secretary', msg + '。基础功能（查询 / 计算 / 攻略）仍可正常使用，下周四零点自动恢复。');
      }
    })
    .catch(() => { if (bar) bar.style.display = 'none'; });
}
document.addEventListener('DOMContentLoaded', checkQuotaAlert);
// 功能菜单（悬浮球）：默认位于标题栏最右侧，可拖动换位（位置不持久化，页面级）；
// 短按（未拖动）展开/收起菜单；查角色→直接开角色面板（不发对话），
// 计算工具→发指令后端在回复中弹子菜单（角色伤害/好感度/索敌计算/经验值计算/模拟组队），
// 查攻略/遭遇战模拟→发对应指令。好感度表单由后端 favor_form 标记、索敌表单由 scout_form
// 标记、经验值表单由 exp_form 标记触发（菜单项全部来自后端 suggestions，前端不硬编码）。
var fabWrap = document.getElementById('fabWrap');
var fabBall = document.getElementById('fabBall');
var fabMenu = document.getElementById('fabMenu');
var FAB_PAD = 20, FAB_DEF_TOP = 8;   // 默认位：标题栏（72px）内右侧居中
function fabSize() { return window.matchMedia('(max-width: 640px)').matches ? 56 : 72; }   // 与媒体查询的 .fab-ball 尺寸同步
function fabClamp(x, y) {
  var s = fabSize();
  return [Math.min(Math.max(x, FAB_PAD), window.innerWidth - s - FAB_PAD),
          Math.min(Math.max(y, FAB_DEF_TOP), window.innerHeight - s - FAB_PAD)];
}
function fabSetPos(x, y) {
  var c = fabClamp(x, y);
  fabWrap.style.left = c[0] + 'px';
  fabWrap.style.top = c[1] + 'px';
  fabWrap.dataset.x = c[0];
  fabWrap.dataset.y = c[1];
}
function fabAlignMenu() {   // 菜单与球「居中对齐」；靠视口边缘时自动平移防溢出
  // 手机端与桌面端统一：以球心为基准居中。悬浮球默认离右边缘 20px，而菜单更宽，
  // 居中后可能超出屏幕，故必须按视口边缘夹取（否则菜单右侧会被切掉）。
  fabMenu.style.right = 'auto';
  fabMenu.style.left = '50%';
  var r = fabMenu.getBoundingClientRect();
  var dx = 0;
  if (r.width) {                        // 菜单隐藏(display:none)时矩形全 0，跳过校正
    var min = 12, max = window.innerWidth - 12;
    if (r.right > max) dx = max - r.right;
    else if (r.left < min) dx = min - r.left;
  }
  // 始终显式设置 transform：否则上一次的非零平移会残留（窗口变宽后菜单仍偏着）
  fabMenu.style.transform = dx ? 'translateX(calc(-50% + ' + dx + 'px))' : 'translateX(-50%)';
}
function fabDefaultPos() { fabSetPos(window.innerWidth - fabSize() - FAB_PAD, FAB_DEF_TOP); }
fabDefaultPos();
fabAlignMenu();   // 首屏即按断点定位菜单（与球居中对齐 + 靠边防溢出）

// 悬浮球自动翻转：原图 ⇄ 现在工具箱，每 10s 一次；
// 展开菜单时强制停在「现在工具箱」面并暂停翻转，收起后恢复
var FAB_FLIP_MS = 10000;
var fabFlipTimer = null;
function fabFlipStart() {
  if (fabFlipTimer) return;
  fabFlipTimer = setInterval(function () { fabBall.classList.toggle('flipped'); }, FAB_FLIP_MS);
}
function fabFlipStop() { if (fabFlipTimer) { clearInterval(fabFlipTimer); fabFlipTimer = null; } }

// 初始：悬浮球默认展开 → 停在工具箱面并暂停翻转；收起则每 10s 自动翻转
fabBall.classList.toggle('flipped', fabWrap.classList.contains('open'));
if (fabWrap.classList.contains('open')) fabFlipStop(); else fabFlipStart();
window.addEventListener('resize', function () {
  fabSetPos(+fabWrap.dataset.x || 0, +fabWrap.dataset.y || FAB_DEF_TOP);
  fabAlignMenu();   // 旋转 / 改宽度后重新校正菜单（居中 + 防溢出）
});
// 拖动：pointer 事件统一鼠标/触摸；位移超过 6px 判为拖动，否则视为点击展开菜单
var fabDrag = null;
fabBall.addEventListener('pointerdown', function (e) {
  fabDrag = { sx: e.clientX, sy: e.clientY, ox: +fabWrap.dataset.x, oy: +fabWrap.dataset.y, moved: false };
  fabBall.setPointerCapture(e.pointerId);
});
fabBall.addEventListener('pointermove', function (e) {
  if (!fabDrag) return;
  var dx = e.clientX - fabDrag.sx, dy = e.clientY - fabDrag.sy;
  if (!fabDrag.moved && Math.abs(dx) + Math.abs(dy) > 6) fabDrag.moved = true;
  if (fabDrag.moved) fabSetPos(fabDrag.ox + dx, fabDrag.oy + dy);
});
fabBall.addEventListener('pointerup', function () {
  if (fabDrag && !fabDrag.moved) {
    var opening = !fabWrap.classList.contains('open');
    fabWrap.classList.toggle('open');
    fabAlignMenu();
    // 打开菜单：停在「现在工具箱」面并暂停翻转；收起后恢复每 10s 翻转
    if (opening) { fabBall.classList.add('flipped'); fabFlipStop(); }
    else { fabFlipStart(); }
  }
  fabDrag = null;
});
document.addEventListener('click', function (e) {
  if (!fabWrap.contains(e.target) && fabWrap.classList.contains('open')) {
    fabWrap.classList.remove('open');
    fabFlipStart();   // 收起菜单后恢复每 10s 翻转
  }
});
document.querySelectorAll('#fabMenu .sug-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    fabWrap.classList.remove('open');
    fabFlipStart();   // 选完功能后恢复每 10s 翻转
    var cmd = btn.dataset.cmd;
    if (cmd === '查角色') { openShipBrowser(); return; }   // 「查角色」直接开角色面板，不发对话
    if (cmd === '使用帮助') { openManual(); return; }       // 「使用帮助」直接弹手册，不发对话
    if (cmd === '退出应用') { requestExit(); return; }      // 「退出应用」关闭网页，不触发后端退出/清记忆
    sendChat(cmd);   // 查攻略/计算工具/遭遇战模拟 → 发对应指令，后端按路由展开
  });
});
// 好感度计算表单：级别用选项按钮单选（11 档），进度手输数字（% 自带，默认 0，最高 100），
// 礼物三选一：不算礼物（默认）/ 低级礼物梯度计算（按阶段固定品质）/ 全用金色礼物。
// 提交时拼成后端 favor 路由可解析的自然语句（如「好感度：现在喜欢，进度30%，到誓约50%，全用金色礼物」）
var FAVOR_STAGES = ['陌生', '相识', '亲近', '喜欢', '爱', '誓约', '依偎', '守护', '至爱', '铭心', '永恒'];
function favorChips(defIdx) {
  return FAVOR_STAGES.map(function (s, i) {
    return '<button type="button" class="favor-opt' + (i === defIdx ? ' on' : '') + '" data-i="' + i + '">' + s + '</button>';
  }).join('');
}
function favorBindOpts(root) {
  root.addEventListener('click', function (e) {
    var b = e.target.closest('.favor-opt');
    if (!b) return;
    root.querySelectorAll('.favor-opt').forEach(function (x) { x.classList.remove('on'); });
    b.classList.add('on');
  });
}
function favorSelIdx(root) {
  var on = root.querySelector('.favor-opt.on');
  return on ? +on.dataset.i : 0;
}
function openFavorForm() {
  var old = document.getElementById('favor-modal');
  if (old) old.remove();
  var wrap = document.createElement('div');
  wrap.id = 'favor-modal';
  wrap.className = 'story-modal';
  wrap.innerHTML =
    '<div class="story-modal-box"><div class="story-modal-head"><b>好感度计算器</b>'
    + '<button class="story-modal-close">✕</button></div>'
    + '<div class="story-modal-body">'
    + '<div class="favor-col"><div class="favor-lab">当前级别</div>'
    + '<div class="favor-opts" id="favor-cur">' + favorChips(0) + '</div></div>'
    + '<div class="favor-row"><label>当前进度</label>'
    + '<div class="favor-pct"><input type="number" id="favor-cur-pct" min="0" max="100" step="0.5" value="0"><span>%</span></div></div>'
    + '<div class="favor-row"><label><input type="checkbox" id="favor-tgt-on"> 指定目标级别（不勾选默认算到下一阶段）</label></div>'
    + '<div id="favor-tgt-wrap" style="display:none">'
    + '<div class="favor-col"><div class="favor-opts" id="favor-tgt">' + favorChips(1) + '</div></div>'
    + '<div class="favor-row"><label>目标进度</label>'
    + '<div class="favor-pct"><input type="number" id="favor-tgt-pct" min="0" max="100" step="0.5" placeholder="不填"><span>%</span></div></div>'
    + '</div>'
    + '<div class="favor-col"><div class="favor-lab">礼物</div><div class="favor-opts" id="favor-gift">'
    + '<button type="button" class="favor-opt on" data-g="">不算礼物</button>'
    + '<button type="button" class="favor-opt" data-g="auto">低级礼物梯度计算</button>'
    + '<button type="button" class="favor-opt" data-g="gold">全用金色礼物</button>'
    + '</div></div>'
    + '<div class="favor-actions"><button id="favor-cancel" class="sug-btn">取消</button>'
    + '<button id="favor-submit" class="sug-btn">开始计算</button></div>'
    + '</div></div>';
  document.body.appendChild(wrap);
  var cur = wrap.querySelector('#favor-cur'), tgt = wrap.querySelector('#favor-tgt');
  var tgtOn = wrap.querySelector('#favor-tgt-on');
  favorBindOpts(cur);
  favorBindOpts(tgt);
  favorBindOpts(wrap.querySelector('#favor-gift'));
  tgtOn.addEventListener('change', function () {
    wrap.querySelector('#favor-tgt-wrap').style.display = tgtOn.checked ? 'block' : 'none';
    if (tgtOn.checked) {   // 目标默认当前级别的下一阶段
      var next = Math.min(favorSelIdx(cur) + 1, 10);
      tgt.querySelectorAll('.favor-opt').forEach(function (x) {
        x.classList.toggle('on', +x.dataset.i === next);
      });
    }
  });
  var close = function () { wrap.remove(); };
  wrap.querySelector('.story-modal-close').addEventListener('click', close);
  wrap.querySelector('#favor-cancel').addEventListener('click', close);
  wrap.addEventListener('click', function (e) { if (e.target === wrap) close(); });
  wrap.querySelector('#favor-submit').addEventListener('click', function () {
    var msg = '好感度：现在' + FAVOR_STAGES[favorSelIdx(cur)];
    var cp = wrap.querySelector('#favor-cur-pct').value;
    if (cp !== '' && +cp > 0) msg += '，进度' + (+cp) + '%';
    if (tgtOn.checked) {
      msg += '，到' + FAVOR_STAGES[favorSelIdx(tgt)];
      var tp = wrap.querySelector('#favor-tgt-pct').value;
      if (tp !== '') msg += (+tp) + '%';
    }
    var g = wrap.querySelector('#favor-gift .favor-opt.on').dataset.g;
    if (g === 'auto') msg += '，低级礼物梯度计算';
    else if (g === 'gold') msg += '，全用金色礼物';
    close();
    sendChat(msg);
  });
}
function _ojbkBubbleHtml() {
  return '<div class="role-tag">阿卡莉</div>服务器ojbk中。'
    + '<div class="sticker-wrap"><img loading="lazy" class="sticker-img" src="'
    + resolveAsset('/kb/图片素材/表情包/ojbk.png') + '" alt="ojbk"></div>';
}

async function sendChat(text) {
  // 发送消息时重新启用输入框（用户提交表单后）
  inputEl.disabled = false;
  inputEl.placeholder = '输入消息...';
  addMsg('user', text);
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;
  
  // 检查是否为天气查询
  const isWeatherQuery = /天气|气温|温度|湿度|风速|预报/.test(text);
  
  if (isWeatherQuery) {
    // 天气查询：调用 queryWeather 函数
    const typing = addMsg('secretary', '（正在查询天气信息…）');
    try {
      const weatherData = await queryWeather(text);
      
      // 构建天气回复
      const weatherReply = `<div class="role-tag">阿卡莉</div>
${weatherData.location}实时天气：${weatherData.weather}
温度：${weatherData.temperature}℃
湿度：${weatherData.humidity}%
风速：${weatherData.wind_speed} km/h
（数据源：${weatherData.data_source}）`; 
      
      typing.querySelector('.bubble').innerHTML = weatherReply;
    } catch (error) {
      // 超时（全局包装已中止并自动反馈）→ 明确提示，不走"定位/失败"分支
      if (isTimeoutError(error)) {
        typing.querySelector('.bubble').innerHTML =
          '<div class="role-tag">阿卡莉</div>系统超时，可以换个问法或者重启服务器试试';
        sendBtn.disabled = false;
        return;
      }
      // 检查是否是定位提示
      if (error.message && error.message.includes('请允许浏览器定位权限')) {
        // 触发浏览器定位
        if (navigator.geolocation) {
          navigator.geolocation.getCurrentPosition(
            (pos) => {
              // 成功获取位置，重新查询天气
              queryWeather(text).then(weatherData => {
                const weatherReply = `<div class="role-tag">阿卡莉</div>
${weatherData.location}实时天气：${weatherData.weather}
温度：${weatherData.temperature}℃
湿度：${weatherData.humidity}%
风速：${weatherData.wind_speed} km/h
（数据源：${weatherData.data_source}）`;
                typing.querySelector('.bubble').innerHTML = weatherReply;
              }).catch(e => {
                typing.querySelector('.bubble').innerHTML = '<div class="role-tag">阿卡莉</div>定位成功但天气查询失败，请稍后重试。';
              });
            },
            (err) => {
              typing.querySelector('.bubble').innerHTML = '<div class="role-tag">阿卡莉</div>定位失败，请检查浏览器设置。';
            }
          );
        } else {
          typing.querySelector('.bubble').innerHTML = '<div class="role-tag">阿卡莉</div>您的浏览器不支持定位功能。';
        }
      } else {
        typing.querySelector('.bubble').innerHTML = '<div class="role-tag">阿卡莉</div>天气查询失败，请稍后重试。';
        console.error('天气查询错误:', error);
      }
    }
  } else {
    // 遭遇战模拟：计算时间轴/必杀相位对齐约需数秒，预判意图后展示「算力拉满中」占位
    const _isEncounter = /遭遇战/.test(text) && /(boss|白天|前排)/i.test(text);
    const _placeholder = _isEncounter
      ? '阿卡莉算力拉满中…（正在推演时间轴与必杀相位对齐）'
      : '（面无表情地思考中…）';
    const typing = addMsg('secretary', _placeholder);
    if (_isEncounter) typing.classList.add('computing');
    // 慢响应提示（用户口径 2026-10-04）：超过 40s 还没回，气泡给出等待提示；
    // 拿到响应 / 超时中止后由 finally 清掉计时器（不会串到下一轮）。
    let _slowTimer = setTimeout(function () {
      try {
        typing.querySelector('.bubble').innerHTML =
          '<div class="role-tag">阿卡莉</div>系统努力ing...请指挥官耐心等候一会儿';
      } catch (e) {}
    }, 40000);
    try {
      const _payload = { message: text, session_id: _sessionId() };
      // 云端模式：用户面板存浏览器本地，随请求携带（服务器无状态、不落盘）
      const _ov = panelOverrides();
      if (_ov) _payload.panel_overrides = _ov;
      // 超时由**全局 fetch 包装**统一兜（见文件顶部 API_TIMEOUT_MS）：到点中止 + 自动反馈
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: _apiHeaders(),
        body: JSON.stringify(_payload)
      });
      if (resp.status === 401) { typing.innerHTML = _ojbkBubbleHtml(); sendBtn.disabled = false; return; }
      const data = await resp.json();
      renderSecretaryReply(data, typing);
      checkQuotaAlert();   // 本轮消耗了 LLM 额度 → 若已跌破 10% 则自动提醒
    } catch (e) {
      if (isTimeoutError(e)) {
        // 响应过久：本轮已中止，自动反馈已由全局包装发出（用户口径 2026-10-04）
        typing.querySelector('.bubble').innerHTML =
          '<div class="role-tag">阿卡莉</div>系统超时，可以换个问法或者重启服务器试试'
          + '<div class="sticker-wrap"><img loading="lazy" class="sticker-img" src="'
          + resolveAsset('/kb/图片素材/表情包/ojbk.png') + '" alt="ojbk"></div>';
      } else {
        typing.querySelector('.bubble').innerHTML = _ojbkBubbleHtml();
      }
    } finally {
      if (_slowTimer) { clearTimeout(_slowTimer); _slowTimer = null; }
      sendBtn.disabled = false;
      inputEl.focus();
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }
}

// 弹手册的「意图」判定：整句话是在索要使用说明/帮助才弹，而非句中恰好带「帮助」二字。
// 例如「请帮助我算伤害」「这个帮助很大」不应触发——前者是计算请求、后者只是顺带提到。
// 做法：剥掉首尾语气/引导词后，若剩余内容可被「帮助类核心词」完全解释，则判为意图成立。
function wantManual(text) {
  if (!text) return false;
  let s = text.toLowerCase().replace(/[\s　]/g, '');
  // 帮助类「核心词」（整词匹配，避免「使用赤城」误触发）
  const CORE = ['使用说明', '使用帮助', '使用手册', '使用指引', '操作说明', '操作指南',
                '玩法说明', '说明', '帮助', 'help', 'manual', '手册', '指南', '指引',
                '教程', '怎么用', '如何使用', '不会用', '怎么玩', '怎么操作', '如何操作', '怎么使用'];
  // 可出现在首尾的语气/引导词（忽略后不影响「要帮助」意图判断）
  const FILL = ['我想', '我要', '我看', '看下', '看看', '看一', '打开', '给我', '来个', '要个',
                '需要', '麻烦', '请', '想', '要', '看', '的', '了', '吗', '呢', '吧', '啊',
                '哦', '嗯', '嘛', '你', '您', '我', '它', '这', '那', '下', '一', '个', '帮', 'me'];
  // 反复剥首尾 filler，直到稳定
  let changed = true;
  while (changed) {
    changed = false;
    for (const f of FILL) {
      if (s.startsWith(f)) { s = s.slice(f.length); changed = true; }
      else if (s.endsWith(f)) { s = s.slice(0, s.length - f.length); changed = true; }
    }
  }
  // 剥掉所有帮助核心词；再清标点与首尾 filler；若最终清空 → 整句即「要帮助」意图
  let left = s;
  for (const c of CORE) left = left.split(c).join('');
  left = left.replace(/[，。、！？!?.,~·\-_'"「」『』""''()（）]/g, '');
  changed = true;
  while (changed) {
    changed = false;
    for (const f of FILL) {
      if (left.startsWith(f)) { left = left.slice(f.length); changed = true; }
      else if (left.endsWith(f)) { left = left.slice(0, left.length - f.length); changed = true; }
    }
  }
  return left.length === 0;
}

// 发送按钮
function send() {
  const text = inputEl.value.trim();
  if (!text) return;
  // 聊天里表达「想看使用说明/帮助」的**意图**才弹手册（不把该元指令发往后端）；
  // 用意图而非关键字：避免「请帮助我算伤害」「这个帮助很大」这类请求误触发。
  if (wantManual(text)) {
    openManual();
    inputEl.value = '';
    inputEl.style.height = 'auto';
    return;
  }
  sendChat(text);
}

sendBtn.addEventListener('click', send);
inputEl.addEventListener('keydown', (e) => {
  // 中文输入法确认（选字/上屏）的回车不算发送：isComposing 或 keyCode 229 时直接忽略，
  // 否则确认拼音的回车会误发消息、提交字符残留（输入框显示多余的回车/残留文本）
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
// 「结束」按钮：中断当前任务（计算/查攻略等），只清临时记忆、保留对话（回复"好，进行下一项工作"）
const endTaskBtn = document.getElementById('endTaskBtn');
endTaskBtn.addEventListener('click', () => {
  if (endTaskBtn.disabled) return;
  sendChat('结束本轮');
});
// 「退出」：直接关闭网页（不触发后端退出/清记忆）；由悬浮球菜单「退出」项调用
function requestExit() {
  // 确认对话框：阿卡莉休眠提示，并说明会重置对话
  if (!confirm('阿卡莉即将进入休眠，会重置对话。确定要退出吗？')) return;
  // 直接请求关闭网页；部分浏览器/容器策略可能拦截脚本关闭
  window.close();
  // 兜底：若窗口未被关闭（脚本关闭被拦截），提示用户手动关闭
  setTimeout(() => {
    if (!window.closed) {
      alert('阿卡莉已进入休眠。请手动关闭此网页/标签页。');
    }
  }, 300);
}
// textarea 自适应高度
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
});

inputEl.focus();

// 浏览器定位：用户首次聚焦输入框时请求授权，成功后自动上报位置（供 query_weather 查实时天气）
let located = false;
function reportLocation() {
  if (located || !navigator.geolocation) return;
  located = true;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      fetch('/api/location', {
        method: 'POST',
        headers: _apiHeaders(),
        body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude })
      }).catch(() => {});
    },
    () => {
      // 用户拒绝定位：上报空，天气工具会提示开启权限
      fetch('/api/location', {
        method: 'POST',
        headers: _apiHeaders(),
        body: JSON.stringify({ lat: null, lon: null })
      }).catch(() => {});
    },
    { enableHighAccuracy: false, timeout: 8000, maximumAge: 600000 }
  );
}
inputEl.addEventListener('focus', reportLocation, { once: true });

// ==================== 自动注册 + 登录遮罩（用户口径 2026-09-25）====================
// 设计：用户拿到「网址 + 邀请码」后**打开即用**，不需要自己注册、也不需要记住 uid。
//   ① 老用户/老设备：后端按 device_id 认人（或本机已存口令）→ 静默就位，直接进
//   ② 新用户：带 ?invite=<邀请码> 打开 → 自动注册 → 分配 uid + 生成个人口令 → 直接进
//   ③ 没带邀请码的新用户 → 弹遮罩，让他输邀请码
// 未注册成功前 authUnlocked=false，避免其他请求打出去吃 401。
let authUnlocked = true;

// 注册成功后的展示（把 uid 与口令明确给出，提醒保存 —— 换设备时要靠它登录）
function showRegisterResult(j) {
  const ov = document.createElement('div');
  ov.id = 'register-result';
  ov.style.cssText = 'position:fixed;inset:0;z-index:10000;background:var(--bg,#f4f6f8);display:flex;align-items:center;justify-content:center;';
  ov.innerHTML = `
    <div style="background:var(--panel,#fff);border:1px solid var(--line,#e5e8ec);border-radius:12px;padding:28px 32px;max-width:380px;width:90%;box-shadow:0 8px 30px rgba(0,0,0,.08);text-align:center;">
      <div style="font-size:17px;font-weight:600;margin-bottom:14px;">注册成功</div>
      <div style="font-size:14px;color:var(--ink-soft,#6b7280);margin-bottom:4px;">你的账号</div>
      <div style="font-size:22px;font-weight:700;letter-spacing:1px;margin-bottom:12px;">${esc((j.user && j.user.user_uid) || j.user_uid || j.uid || '')}</div>
      <div style="font-size:14px;color:var(--ink-soft,#6b7280);margin-bottom:4px;">你的口令</div>
      <div style="font-size:17px;font-weight:600;font-family:ui-monospace,Consolas,monospace;word-break:break-all;background:#f6f4ee;border:1px solid var(--line,#e5e8ec);border-radius:8px;padding:9px 12px;margin-bottom:14px;">${esc(j.token || '')}</div>
      <div style="font-size:13px;color:#c0392b;line-height:1.6;margin-bottom:16px;">请截图或抄下这两项。<br>本机已自动记住，换手机/换浏览器时需要用它登录。</div>
      <button id="reg-go" style="width:100%;padding:11px;border:none;border-radius:9px;background:var(--accent,#6b8caa);color:#fff;font-size:15px;cursor:pointer;">开始使用</button>
    </div>`;
  document.body.appendChild(ov);
  const enter = () => { location.reload(); };
  // 进入应用前，先移除残留的「邀请码遮罩」(auth-overlay, z9999) 与本成功遮罩(register-result, z10000)，
  // 否则随后弹出的手册(.sb-mask, z9998)会被压在下面、看不到也关不掉 → 表现为「开始使用」点了没反应。
  const cleanup = () => {
    ['auth-overlay', 'register-result'].forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.remove();   // 含本成功层(ov)一并移除，否则手册(.sb-mask,z9998)会被压在下面
    });
    ov.remove();
  };
  const go = () => {
    // 首次注册：进入应用前自动弹一次手册（底部「确认/关闭」），仅弹一次
    if (!localStorage.getItem('akali_manual_seen')) {
      cleanup();   // 先清掉自己与邀请码遮罩，让手册成为最上层
      openManual(function () { localStorage.setItem('akali_manual_seen', '1'); enter(); });
    } else {
      enter();
    }
  };
  ov.querySelector('#reg-go').addEventListener('click', go);
  setTimeout(go, 8000);   // 不点也会自动弹手册（首次）/ 直接进（非首次）
}

// 设备标识：浏览器生成一次并持久化，作为"我是谁"的主标识传给服务端。
// 为什么不用 IP（用户口径 2026-09-25）：IP 会因切网/家宽重播而变（同一人反复注册），
// 也会因同 WiFi 多人共用而撞车（多人被认成一人）。设备标识稳定、一人一号。
function deviceId() {
  const KEY = 'arkly_device_id';
  let v = credGet(KEY);
  if (!v) {
    try {
      const a = new Uint8Array(12);
      (window.crypto || window.msCrypto).getRandomValues(a);
      v = Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
    } catch (e) {
      v = 'd' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    }
    credSet(KEY, v);
  }
  return v;
}

// 静默"就位"：能自动注册/认领就返回 true（随后页面重载为已登录状态）
async function tryAutoRegister(info) {
  // 邀请码来源：URL 的 ?invite= 优先，其次用上次记住的（从主屏幕图标/书签打开时
  // URL 常不带参数；此时若设备标识还在，服务端按 device_id 直接回位、根本不校验
  // 邀请码；只有设备标识也没了才会用到它去重新注册，避免又弹遮罩）。
  const invite = ((new URLSearchParams(location.search).get('invite') || '').trim())
                 || credGet('arkly_invite');
  const canRegister = !!(info && info.allow_register);
  // 既没带邀请码、后端也没开放注册 → 不必尝试（直接走遮罩）
  if (!invite && !canRegister) return false;
  try {
    const r = await fetch('/api/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ invite: invite, device_id: deviceId() })
    });
    const j = await r.json();
    if (!j || !j.ok || !j.token) return false;
    credSet('arkly_api_token', j.token);
    if (invite) credSet('arkly_invite', invite);  // 记住邀请码，供以后无参数打开时回位
    if (j.created) {
      showRegisterResult(j);                     // 新注册：展示 uid+口令，8 秒后自动进
    } else {
      setTimeout(() => location.reload(), 60);   // 老账号回位：立刻重载
    }
    return true;
  } catch (e) { return false; }
}

function buildAuthOverlay(info) {
  const canRegister = !!(info && info.allow_register);
  const needInvite = !(info && info.need_invite === false);
  const inpStyle = 'width:100%;padding:10px 12px;border:1px solid var(--line,#e5e8ec);border-radius:8px;font-size:15px;box-sizing:border-box;outline:none;margin-bottom:10px;';
  const tabOn = 'flex:1;padding:8px 0;border:1px solid var(--accent,#6b8caa);background:var(--accent,#6b8caa);color:#fff;border-radius:8px;font-size:14px;cursor:pointer;';
  const tabOff = 'flex:1;padding:8px 0;border:1px solid var(--line,#e5e8ec);background:#fff;color:var(--ink-soft,#6b7280);border-radius:8px;font-size:14px;cursor:pointer;';
  const ov = document.createElement('div');
  ov.id = 'auth-overlay';
  ov.style.cssText = 'position:fixed;inset:0;z-index:9999;background:var(--bg,#f4f6f8);display:flex;align-items:center;justify-content:center;';
  // 两种模式：register=注册（邀请码 + uid 都必填）/ login=已有口令
  let mode = canRegister ? 'register' : 'login';
  ov.innerHTML = `
    <div style="background:var(--panel,#fff);border:1px solid var(--line,#e5e8ec);border-radius:12px;padding:32px 36px;max-width:360px;width:90%;box-shadow:0 8px 30px rgba(0,0,0,.08);text-align:center;">
      <div style="width:48px;height:48px;border-radius:50%;overflow:hidden;background:#fff;border:1px solid var(--line,#e5e8ec);margin:0 auto 14px;display:flex;align-items:center;justify-content:center;"><img src="/kb/图片素材/表情差分/常态.jpg" alt="阿卡莉" style="width:100%;height:100%;object-fit:cover;display:block;" onerror="var p=this.parentNode;this.remove();p.textContent='阿';p.style.background='linear-gradient(135deg,#8fa6b8,#5d7a91)';p.style.color='#fff';p.style.fontSize='19px';"></div>
      <div style="font-size:17px;font-weight:600;margin-bottom:6px;">阿卡莉：拂晓智能助手</div>
      <div id="auth-hint" style="font-size:14px;color:var(--ink-soft,#6b7280);margin-bottom:14px;"></div>
      ${canRegister ? '<div style="display:flex;gap:8px;margin-bottom:14px;"><button type="button" id="tab-reg">注册</button><button type="button" id="tab-login">已有口令</button></div>' : ''}
      <div id="reg-fields">
        ${needInvite ? `<input id="auth-invite" type="text" autocomplete="off" placeholder="邀请码" style="${inpStyle}" />` : ''}
        <input id="auth-uid" type="text" inputmode="numeric" maxlength="8" autocomplete="off" placeholder="你的 uid" style="${inpStyle}" />
      </div>
      <div id="login-fields" style="display:none;">
        <input id="auth-token" type="password" placeholder="输入你的口令" style="${inpStyle}" />
      </div>
      <div id="auth-err" style="color:#c0392b;font-size:13px;min-height:16px;margin-top:2px;display:none;">口令错误，请重试</div>
      <button id="auth-submit"
        style="width:100%;margin-top:12px;padding:10px 0;background:var(--accent,#6b8caa);color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer;"></button>
    </div>`;
  document.body.appendChild(ov);
  const inviteEl = ov.querySelector('#auth-invite');
  const uidEl = ov.querySelector('#auth-uid');
  const tokenEl = ov.querySelector('#auth-token');
  const err = ov.querySelector('#auth-err');
  const btn = ov.querySelector('#auth-submit');
  const regFields = ov.querySelector('#reg-fields');
  const loginFields = ov.querySelector('#login-fields');
  const tabReg = ov.querySelector('#tab-reg');
  const tabLogin = ov.querySelector('#tab-login');
  const hintEl = ov.querySelector('#auth-hint');
  // 预填邀请码（记住的 / URL ?invite=）
  if (needInvite && inviteEl) {
    const remembered = credGet('arkly_invite') || (new URLSearchParams(location.search).get('invite') || '');
    if (remembered) inviteEl.value = remembered;
  }
  function validUid(u) { return /^[1368][0-9]{7}$/.test(u); }
  function curField() { return (mode === 'register' && canRegister) ? (inviteEl || uidEl) : tokenEl; }
  function applyMode() {
    if (mode === 'register' && canRegister) {
      regFields.style.display = '';
      loginFields.style.display = 'none';
      hintEl.textContent = needInvite ? '新用户注册：邀请码 + 你的 uid，两个都要填' : '新用户注册：填写你的 8 位 uid';
      btn.textContent = '开始使用';
      if (tabReg) tabReg.style.cssText = tabOn;
      if (tabLogin) tabLogin.style.cssText = tabOff;
    } else {
      regFields.style.display = 'none';
      loginFields.style.display = '';
      hintEl.textContent = '已有账号：输入你的口令登录';
      btn.textContent = '登录';
      if (tabReg) tabReg.style.cssText = tabOff;
      if (tabLogin) tabLogin.style.cssText = tabOn;
    }
    const f = curField(); if (f) f.focus();
  }
  // 用已有口令验证（老用户换设备登录 / 管理端口令）
  function verifyToken(t) {
    credSet('arkly_api_token', t);
    fetch('/api/auth/status', { headers: { 'X-API-Token': t } })
      .then(r => r.json())
      .then(d => {
        if (d && d.authenticated) {
          err.style.display = 'none';
          document.body.removeChild(ov);
          authUnlocked = true;
          if (d.panel_store) { ARKLY_PANEL_STORE = d.panel_store; }
          if (d.uid) { ARKLY_UID = d.uid; panelMigrateLegacy(); }
          if (typeof upRefresh === 'function') { upRefresh(); }
          panelCloudRestore();               // 本地为空时从云端备份回填
          // 可能有过期/占位的初始问候（401 时显示 ojbk），解锁后重发一次以刷新
          const lastMsg = messagesEl.lastElementChild;
          if (lastMsg && lastMsg.textContent.includes('服务器ojbk中')) {
            const t0 = document.querySelector('.msg.user:last-of-type .text');
            if (t0) sendChat(t0.textContent.trim());
          }
          inputEl.focus();
        } else {
          credDel('arkly_api_token');
          err.textContent = (d && d.reason === 'paused') ? '服务暂未对测试用户开放' : '口令错误，请重试';
          err.style.display = 'block';
          const f = curField(); if (f) f.focus();
        }
      })
      .catch(() => { err.textContent = '网络错误，请稍后重试'; err.style.display = 'block'; const f = curField(); if (f) f.focus(); });
  }
  function submit() {
    err.style.display = 'none';
    // ① 注册：邀请码 + uid 一起输（都必填）
    if (mode === 'register' && canRegister) {
      const inv = inviteEl ? (inviteEl.value || '').trim() : '';
      const u = (uidEl.value || '').trim();
      if (needInvite && !inv) {
        err.textContent = '请填写邀请码'; err.style.display = 'block'; if (inviteEl) inviteEl.focus(); return;
      }
      if (!u) {
        err.textContent = '请填写你的 uid'; err.style.display = 'block'; uidEl.focus(); return;
      }
      if (!validUid(u)) {
        err.textContent = 'uid 须为 8 位数字、首位为 1/3/6/8'; err.style.display = 'block'; uidEl.focus(); return;
      }
      fetch('/api/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ invite: inv, device_id: deviceId(), user_uid: u })
      }).then(r => r.json()).then(j => {
        if (j && j.ok && j.token) {
          credSet('arkly_api_token', j.token);
          if (inv) credSet('arkly_invite', inv);   // 记住邀请码，下次打开无需再输
          if (j.created) { showRegisterResult(j); return; }
          location.reload();
          return;
        }
        const reason = (j && j.reason) || '';
        err.textContent = {
          bad_uid: 'uid 须为 8 位数字、首位为 1/3/6/8',
          uid_taken: '该 uid 已被占用，请换一个',
          bad_invite: '邀请码无效',
          register_off: '暂未开放注册，请联系管理员',
        }[reason] || '注册失败，请检查邀请码与 uid';
        err.style.display = 'block';
        if (reason === 'uid_taken' || reason === 'bad_uid') uidEl.focus(); else if (inviteEl) inviteEl.focus();
      }).catch(() => { err.textContent = '网络错误，请稍后重试'; err.style.display = 'block'; });
      return;
    }
    // ② 已有口令登录
    const t = (tokenEl.value || '').trim();
    if (!t) return;
    verifyToken(t);
  }
  if (tabReg) tabReg.addEventListener('click', function () { mode = 'register'; err.style.display = 'none'; applyMode(); });
  if (tabLogin) tabLogin.addEventListener('click', function () { mode = 'login'; err.style.display = 'none'; applyMode(); });
  btn.addEventListener('click', submit);
  [inviteEl, uidEl, tokenEl].forEach(function (el) {
    if (el) el.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit(); });
  });
  applyMode();
  authUnlocked = false;
}

// 页面加载：探测认证状态，需要解锁时弹遮罩
(async function initAuth() {
  let _runMode = '';
  try {
    const r = await fetch('/api/auth/status', { headers: _apiHeaders() });
    const d = await r.json();
    _runMode = (d && d.run_mode) || '';
    // 面板存储位置由后端运行模式决定：server=本地(服务器副本) / client=云端(浏览器本地缓存)
    if (d && d.panel_store) { ARKLY_PANEL_STORE = d.panel_store; }
    if (d && d.uid) { ARKLY_UID = d.uid; panelMigrateLegacy(); }
    if (d && d.auth_required && !d.authenticated) {
      // 先静默尝试"就位"：老用户按 IP 直接回位、带邀请码的新用户自动注册；
      // 两者都不成立才弹遮罩（用户口径 2026-09-25：第一次打开尽量无感）
      const auto = await tryAutoRegister(d);
      if (!auto) { authUnlocked = false; buildAuthOverlay(d); }
    } else {
      if (typeof upRefresh === 'function') { upRefresh(); }
      panelCloudRestore();               // 本地为空时从云端备份回填
    }
  } catch (e) {
    // 探测失败（网络/后端旧版无此接口）→ 不弹遮罩，保持原行为
  }
  // 会话重置（用户口径 2026-09-27）：本地模式（单机、刷新即重开）保持原行为
  // 「每次页面加载都清」；**云端**改为只在**本标签页首次加载**时清 —— 移动端
  // 「页面挂久了被系统回收后自动重载」时，用户算伤害填到一半回来发现记忆被清空，
  // 又被要求重新输入。用 sessionStorage 打标：同一标签的重载（含系统回收恢复）
  // 不再清，只有真正新开的标签/新会话才清。
  let _doClear = true;
  if (_runMode === 'cloud') {
    try {
      _doClear = !sessionStorage.getItem('arkly_session_boot');
      sessionStorage.setItem('arkly_session_boot', '1');
    } catch (e) { _doClear = true; }
  }
  if (_doClear) {
    try {
      await fetch('/api/clear?session_id=' + encodeURIComponent(_sessionId()), {
        method: 'POST',
        headers: _apiHeaders()
      });
      console.log('会话已重置');
    } catch (e) {
      console.warn('重置会话失败:', e);
    }
  }
})();

// 背景时段划分（四档，按**分钟**判定；用户口径 2026-09-13，聊天背景与顶端共用口径）：
//   清晨 05:30–07:30 / 白天 07:30–17:00 / 黄昏 17:00–19:00 / 夜间 19:00–05:30
const BG_DAWN = 5 * 60 + 30, BG_DAY = 7 * 60 + 30, BG_DUSK = 17 * 60, BG_NIGHT = 19 * 60;
// 返回 'dawn' | 'day' | 'evening' | 'night'
function bgPeriod() {
  const m = new Date().getHours() * 60 + new Date().getMinutes();
  return (m >= BG_DAWN && m < BG_DAY) ? 'dawn'
       : (m >= BG_DUSK && m < BG_NIGHT) ? 'evening'
       : (m >= BG_NIGHT || m < BG_DAWN) ? 'night'
       : 'day';
}
// 聊天背景按时间轮换：白天不挂 class（走 CSS 基准 main::before）；
// 每 60s 复查一次，跨时段自动切换。
(function initBgByTime() {
  function apply() {
    const p = bgPeriod();
    document.body.classList.remove('bg-dawn', 'bg-evening', 'bg-night');
    if (p !== 'day') document.body.classList.add('bg-' + p);
  }
  apply();
  setInterval(apply, 60 * 1000);
})();
// 顶端背景：现由 CSS 的 header 直接以「当前聊天背景的淡化版」呈现（白纱 + 毛玻璃透出
//   main::before，自动跟随时段）；原先「顶端.png 专用顶栏图」逻辑已停用（has-top-bg 不再有样式）。

// 节庆主题（数据驱动：2026-09-27 升级）。后端 /api/ui_assets 下发 holiday：
// { theme, assets:{bubble, fall:[{file,size,speed,rotate}]}, palette:{cssVar:value} }。
// 本函数零主题分支：挂 .festival-on、注入调色板变量、按 assets.fall 建飘落层。
// 非节庆日不会调用（initUiAssets 仅在 data.holiday 存在时调用），界面保持原样。
function applyFestivalTheme(hol) {
  const theme = (hol && hol.theme) || '';
  if (!theme) return;
  document.body.classList.add('festival-on', 'festival-' + theme);
  // 注入调色板 CSS 变量（头像环 / 气泡皮肤 / 用户气泡描边）
  const pal = (hol.palette) || {};
  for (const k in pal) {
    if (Object.prototype.hasOwnProperty.call(pal, k)) document.documentElement.style.setProperty(k, pal[k]);
  }
  // 飘落层：按 assets.fall 动态创建，每项用描述符里的 size/speed/rotate
  const fall = ((hol.assets) && hol.assets.fall) || [];
  if (!fall.length) return;
  const layer = document.createElement('div');
  layer.className = 'festival-fall';
  layer.setAttribute('aria-hidden', 'true');
  const rnd = (r) => r[0] + Math.random() * (r[1] - r[0]);
  for (let i = 0; i < 16; i++) {                 // 数量：够热闹又不干扰阅读
    const spec = fall[i % fall.length];
    const el = document.createElement('i');
    const sz = rnd(spec.size || [14, 26]);
    el.style.backgroundImage = 'url("/icons/festival/' + spec.file + '")';
    el.style.left = (Math.random() * 100).toFixed(2) + 'vw';
    el.style.width = sz.toFixed(0) + 'px';
    el.style.height = sz.toFixed(0) + 'px';
    const dur = rnd(spec.speed || [9, 16]);
    el.style.animationDuration = dur.toFixed(1) + 's';      // 落到底的时长
    el.style.animationDelay = (-Math.random() * dur).toFixed(1) + 's';  // 负延迟：一进页面就有
    el.style.opacity = (0.55 + Math.random() * 0.4).toFixed(2);
    layer.appendChild(el);
  }
  document.body.appendChild(layer);
}

// 界面素材初始化：拉取默认头像（表情差分「常态」），替换欢迎消息与全局默认头像
(async function initUiAssets() {
  try {
    const resp = await fetch('/api/ui_assets', { headers: _apiHeaders() });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.default_avatar) {
      ARKLY_AVATAR = resolveAsset(data.default_avatar);
      // 页头头像 + 欢迎消息头像一并切换为差分默认图
      document.querySelectorAll('.avatar img, .msg.secretary .ava img').forEach(img => { img.src = ARKLY_AVATAR; });
    }
    if (data.user_avatar) {
      USER_AVATAR = resolveAsset(data.user_avatar);
      // 应用到已渲染的指挥官头像（初始通常无用户消息，这里仅作兜底）
      document.querySelectorAll('.msg.user .ava img').forEach(img => { if (img) img.src = USER_AVATAR; });
    }
    // 节庆主题（今天在法定节假日区间内才有）：换头像边框 / 气泡皮肤 / 落节庆装饰
    if (data.holiday) applyFestivalTheme(data.holiday);
    // 表情差分映射（名字→URL）：双击阿卡莉头像随机切换表情用
    if (Array.isArray(data.expression_urls)) ARKLY_EXPR_URLS = data.expression_urls;
  } catch (e) { /* 素材拉取失败沿用回退头像，不打断对话 */ }
})();

// ===== 双击阿卡莉头像：随机切换表情（排除「常态」「厌恶」）持续 3 秒 =====
// 作用于标题栏头像（.avatar img）与每条秘书回复头像（.msg.secretary .ava img）。
// 双击时随机挑一张非排出表情的差分图，3 秒后还原到切换前本来的头像。
let _akaliExprTimer = null;
function cycleAkaliExpression() {
  if (!ARKLY_EXPR_URLS.length) return;
  const EXCLUDE = new Set(['常态', '厌恶']);
  const pool = ARKLY_EXPR_URLS.filter(e => e && e.name && !EXCLUDE.has(e.name));
  if (!pool.length) return;
  const pick = pool[Math.floor(Math.random() * pool.length)];
  const url = resolveAsset(pick.url);
  if (_akaliExprTimer) clearTimeout(_akaliExprTimer);
  const imgs = document.querySelectorAll('.avatar img, .msg.secretary .ava img');
  imgs.forEach(function (img) {
    if (!img.dataset._exprPrev) img.dataset._exprPrev = img.src;
    img.src = url;
  });
  _akaliExprTimer = setTimeout(function () {
    document.querySelectorAll('.avatar img, .msg.secretary .ava img').forEach(function (img) {
      if (img.dataset._exprPrev) { img.src = img.dataset._exprPrev; img.dataset._exprPrev = ''; }
    });
  }, 3000);
}
// 事件委托：单击阿卡莉头像任意一处即触发（标题栏 + 消息内头像，动态新增也能命中）
// 守卫：仅当该头像当前就是「常态」时才响应 —— 覆盖 <<EXP>> 神态或随机切换中的临时表情时点击无效，
// 切回常态（3 秒后或本就是默认）后才可再次触发（用户口径：必须是常态的时候点击才有效）。
document.addEventListener('click', function (ev) {
  const img = ev.target.closest('.avatar img, .msg.secretary .ava img');
  if (!img) return;
  ev.preventDefault();
  // 归一化为绝对地址再比：img.src 恒为绝对 URL，而 ARKLY_AVATAR 在同源部署下是相对路径，
  // 直接 !== 会因「绝对 vs 相对」永远不相等，导致守卫误判「非常态」而拦截全部单击。
  const def = new URL(ARKLY_AVATAR, location.href).href;
  if (img.src !== def) return;
  cycleAkaliExpression();
});

// ===== 心跳与自动关闭 =====
(function() {
  const HEARTBEAT_INTERVAL = 30000; // 30秒发送一次心跳（常驻，不因页面隐藏暂停）
  const BOOT_KEY = 'arkly_live_boot';
  let heartbeatTimer = null;
  let isClosing = false;

  // 发送心跳
  async function sendHeartbeat() {
    try {
      const resp = await fetch('/api/heartbeat', {
        method: 'GET',
        cache: 'no-store'
      });
      if (!resp.ok) throw new Error('心跳失败');
      const data = await resp.json();
      // 记录服务当前启动标识（同源共享，供其他标签页通过 storage 事件感知）
      try { localStorage.setItem(BOOT_KEY, data.boot || ''); } catch (e) {}
      // 启动标识不符：本页是上一次运行遗留的旧页面，自动失效关闭
      // 嵌入场景（浏览器插件，window.AKALI_NO_SELFDESTRUCT）不做自毁：插件窗口是用户
      // 主动打开的应用窗口，服务端重启/换页不应把它自动关掉（用户口径 2026-09-24）。
      if (data.boot && data.boot !== String(window.AKALI_BOOT_ID) && !window.AKALI_NO_SELFDESTRUCT) {
        selfDestruct();
      }
    } catch (e) {
      // 心跳失败可能是服务已关闭，停止重试
      console.warn('心跳失败:', e.message);
    }
  }

  // 旧页面自动失效：覆盖全屏提示并尝试自行关闭标签页
  function selfDestruct() {
    if (isClosing) return;
    isClosing = true;
    stopHeartbeat();
    // 清除广播标记，避免再触发其他逻辑
    try { localStorage.removeItem(BOOT_KEY); } catch (e) {}
    try {
      const ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:100%;z-index:99999;'
        + 'background:rgba(10,10,14,.94);color:#eee;display:flex;align-items:center;'
        + 'justify-content:center;text-align:center;font-size:16px;line-height:2;';
      ov.innerHTML = '<div>阿卡莉已在新的窗口重新上线。<br>本页面属于上一次会话，已自动失效。</div>';
      document.body.appendChild(ov);
    } catch (e) {}
    // 尝试自行关闭标签页；被浏览器拦截时保留上方遮罩提示
    setTimeout(function() {
      try { window.open('', '_self'); window.close(); } catch (e) {}
    }, 400);
  }

  // 本地存储广播：同一浏览器内，新页面加载写入 boot 后，
  // 所有持有旧标识的标签页经 storage 事件立即失效（无需等下一轮心跳）
  window.addEventListener('storage', function(ev) {
    if (ev.key === BOOT_KEY && ev.newValue && ev.newValue !== String(window.AKALI_BOOT_ID)
        && !window.AKALI_NO_SELFDESTRUCT) {
      selfDestruct();
    }
  });

  // 发送关闭信号
  async function sendShutdown() {
    if (isClosing) return;
    isClosing = true;
    try {
      // 使用 navigator.sendBeacon 确保在页面关闭时也能发送
      const data = JSON.stringify({});
      navigator.sendBeacon('/api/shutdown', data);
    } catch (e) {
      // 忽略错误
    }
  }

  // 页面加载完成后启动心跳
  function startHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    // 立即发送一次
    sendHeartbeat();
    // 定时发送
    heartbeatTimer = setInterval(sendHeartbeat, HEARTBEAT_INTERVAL);
  }

  // 横幅滚动轮播：温馨提示 / 事务提醒（后端注入的第二条 tip-item）每 4s 淡入淡出交替
  (function () {
    const tip = document.getElementById('warmTip');
    if (!tip) return;
    const items = tip.querySelectorAll('.tip-item');
    if (items.length <= 1) { if (items[0]) items[0].classList.add('on'); return; }
    let idx = 0;
    items.forEach((el, i) => el.classList.toggle('on', i === 0));
    setInterval(function () {
      items[idx].classList.remove('on');
      idx = (idx + 1) % items.length;
      items[idx].classList.add('on');
    }, 5000);
  })();

  // 停止心跳
  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  // 监听页面关闭事件
  window.addEventListener('beforeunload', function(e) {
    sendShutdown();
  });

  // 页面重新可见/获得焦点时立即校验一次（配合常驻心跳兜底）
  window.addEventListener('focus', sendHeartbeat);
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) sendHeartbeat();
  });

  // 页面加载完成后启动心跳（提醒已在服务端拼入初始问候气泡，无需额外拉取）
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startHeartbeat);
  } else {
    startHeartbeat();
  }
})();

// ===== 反馈互动（用户口径 2026-10-04 改版）=====
// 旧版：知识库问答回复右下角常显 💡 按钮，点一下上报。
// 新版：**长按任意阿卡莉回复**弹出表情面板（🍬 / 🐷），选中的表情显示在该条回复下方；
//   效果与旧按钮等价（POST /api/feedback，feedback_type=unsatisfied）。长按对**所有回复**
//   都可触发，但只有「可反馈路由」（后端 from_kb 标记 data-from-kb="1"）的消息才真正生效；
//   同一轮对话（自上一个进程边界起、连续的 kb 回复）只上报一次，重复选择不再重复提交。

// 提取气泡正文：克隆后剔除角色标签、表情徽章，避免污染上报文本
function _fbBubbleText(node) {
  const bubble = (node && node.querySelector) ? node.querySelector('.bubble') : null;
  if (!bubble) return '';
  const clone = bubble.cloneNode(true);
  clone.querySelectorAll('.role-tag, .fb-reaction, .feedback-btn').forEach(function (el) { el.remove(); });
  return (clone.textContent || '').trim();
}

// 本轮对话的消息节点：从目标消息向前，连续收集 data-from-kb="1" 的秘书消息（遇进程边界即止），
// 时间正序返回。
function _fbRoundNodes(msgEl) {
  const nodes = [];
  let node = msgEl;
  while (node && node.classList && node.classList.contains('secretary')) {
    if (node.getAttribute('data-from-kb') !== '1') break;   // 进程边界 / 非可反馈回复
    nodes.unshift(node);
    let p = node.previousElementSibling;
    while (p && !(p.classList && p.classList.contains('secretary'))) p = p.previousElementSibling;
    node = p;
  }
  return nodes;
}

// 组装上报用的 (用户问, 阿卡莉答) 配对（沿用旧逻辑，保证反馈内容不变）
function _fbCollectConversation(msgEl) {
  return _fbRoundNodes(msgEl).map(function (node) {
    let u = node.previousElementSibling, userText = '';
    while (u) {
      if (u.classList && u.classList.contains('user')) { userText = _fbBubbleText(u); break; }
      if (u.classList && u.classList.contains('secretary')) break;   // 无对应用户提问
      u = u.previousElementSibling;
    }
    return { user: userText, assistant: _fbBubbleText(node) };
  });
}

// 同一轮是否已上报过（同一轮只反馈一次）
function _fbRoundDone(msgEl) {
  return _fbRoundNodes(msgEl).some(function (n) { return n.getAttribute('data-fb-done') === '1'; });
}
function _fbMarkRoundDone(msgEl) {
  _fbRoundNodes(msgEl).forEach(function (n) { n.setAttribute('data-fb-done', '1'); });
}

// 在回复下方显示已选表情
function _fbShowReaction(msgEl, emoji) {
  const bubble = msgEl.querySelector('.bubble');
  if (!bubble) return;
  let box = bubble.querySelector('.fb-reaction');
  if (!box) { box = document.createElement('div'); box.className = 'fb-reaction'; bubble.appendChild(box); }
  box.innerHTML = '<span>' + emoji + '</span>';
}

// 提交一次反馈（等价旧 💡：POST /api/feedback，feedback_type=unsatisfied）
function _fbSubmit(msgEl, emoji) {
  const conv = _fbCollectConversation(msgEl);
  const lastPair = conv.length ? conv[conv.length - 1] : null;
  const payload = {
    session_id: _sessionId(),
    message_id: msgEl.getAttribute('data-msg-id') || '',
    user_input: lastPair ? lastPair.user : '',
    assistant_reply: lastPair ? lastPair.assistant : _fbBubbleText(msgEl),
    timestamp: new Date().toISOString(),
    feedback_type: 'unsatisfied',
    from_kb: msgEl.getAttribute('data-from-kb') === '1',
    conversation: conv.slice(-30)   // 封顶 30 轮，防止异常膨胀
  };
  return fetch('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ..._apiHeaders() },
    credentials: 'include',
    body: JSON.stringify(payload)
  }).then(function (r) { return r.json(); });
}

// —— 长按 → 表情面板（🍬 / 🐷）——
let _fbPickerEl = null;
let _fbLastFire = 0;

function _fbClosePicker() {
  if (_fbPickerEl && _fbPickerEl.parentNode) _fbPickerEl.parentNode.removeChild(_fbPickerEl);
  _fbPickerEl = null;
}

// 事件是否落在表情面板自身内（面板上的操作不能把面板关掉）
function _fbInPicker(node) {
  return !!(node && node.closest && node.closest('.fb-picker'));
}

// 选中表情：先显示在回复下方。**只有「可反馈路由」才真正上报**；同一轮只上报一次。
function _fbPick(msgEl, emoji) {
  _fbShowReaction(msgEl, emoji);
  if (msgEl.getAttribute('data-from-kb') !== '1') return;   // 非可反馈回复：只显示，不上报
  if (_fbRoundDone(msgEl)) return;                          // 本轮已上报 → 不重复提交
  _fbSubmit(msgEl, emoji).then(function (d) {
    if (d && d.ok) { _fbMarkRoundDone(msgEl); }
  }).catch(function () { /* 静默：本地已显示，不打断对话 */ });
}

function _fbOpenPicker(msgEl) {
  _fbClosePicker();
  const bubble = msgEl.querySelector('.bubble');
  if (!bubble) return;
  const picker = document.createElement('div');
  picker.className = 'fb-picker';
  ['🍬', '🐷'].forEach(function (emoji) {
    const btn = document.createElement('button');
    btn.type = 'button'; btn.textContent = emoji;
    btn.setAttribute('aria-label', '反馈 ' + emoji);
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      _fbClosePicker();
      _fbPick(msgEl, emoji);
    });
    picker.appendChild(btn);
  });
  document.body.appendChild(picker);
  // 定位在气泡正上方居中；上方空间不足则落到下方（并夹在视口内）
  const r = bubble.getBoundingClientRect();
  const pw = picker.offsetWidth || 108, ph = picker.offsetHeight || 52;
  const left = Math.max(8, Math.min(r.left + r.width / 2 - pw / 2, window.innerWidth - pw - 8));
  let top = r.top - ph - 8;
  if (top < 8) top = Math.min(r.bottom + 8, Math.max(8, window.innerHeight - ph - 8));
  picker.style.left = left + 'px';
  picker.style.top = top + 'px';
  _fbPickerEl = picker;
}

// 长按检测（触摸 + 鼠标）：**任意阿卡莉回复**长按都弹表情面板（界面一致）；
// 是否真正上报由该条回复的 data-from-kb 决定（见 _fbPick）。
(function initLongPressFeedback() {
  const HOLD_MS = 450;
  let timer = null, holdEl = null, startX = 0, startY = 0;

  function clearHold() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (holdEl) holdEl.classList.remove('fb-pressing');
    holdEl = null;
  }

  function onDown(e) {
    if (e.button != null && e.button !== 0) return;   // 仅左键 / 触摸
    if (_fbInPicker(e.target)) return;                // 面板自身的操作不打断
    const msgEl = (e.target && e.target.closest) ? e.target.closest('.msg.secretary') : null;
    if (!msgEl) { _fbClosePicker(); return; }         // 按在别处 → 关面板
    _fbClosePicker();
    clearHold();
    holdEl = msgEl;
    const pt = e.touches ? e.touches[0] : e;
    startX = pt.clientX || 0; startY = pt.clientY || 0;
    msgEl.classList.add('fb-pressing');
    timer = setTimeout(function () {
      timer = null;
      const el = holdEl;
      holdEl = null;
      if (el) {
        el.classList.remove('fb-pressing');
        _fbLastFire = Date.now();
        _fbOpenPicker(el);                            // 所有回复都弹面板
      }
    }, HOLD_MS);
  }

  function onMove(e) {
    if (!timer) return;
    const pt = e.touches ? e.touches[0] : e;
    const dx = Math.abs((pt.clientX || 0) - startX), dy = Math.abs((pt.clientY || 0) - startY);
    if (dx > 14 || dy > 14) clearHold();              // 明显滑动 → 视为滚动，不触发
  }

  document.addEventListener('touchstart', onDown, { passive: true });
  document.addEventListener('touchmove', onMove, { passive: true });
  document.addEventListener('touchend', clearHold, { passive: true });
  // 注意：触摸被系统打断（touchcancel，长按菜单/选择抢手势）**不**取消 —— 面板照弹
  document.addEventListener('mousedown', onDown);
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', clearHold);
  // 长按已触发时吞掉随后的一次 click，避免误触回复内按钮（面板自身放行）
  document.addEventListener('click', function (e) {
    if (_fbInPicker(e.target)) return;
    if (Date.now() - _fbLastFire < 800) { _fbLastFire = 0; e.stopPropagation(); e.preventDefault(); }
  }, true);
  // 系统长按菜单会抢走手势 → 在阿卡莉回复上禁掉（桌面右键同理）
  document.addEventListener('contextmenu', function (e) {
    if (e.target && e.target.closest && e.target.closest('.msg.secretary')) e.preventDefault();
  }, true);
  // 滚动 / 缩放 → 关面板
  document.addEventListener('scroll', _fbClosePicker, true);
  window.addEventListener('resize', _fbClosePicker);
})();

// 每条阿卡莉回复都打上「可反馈」标记（data-from-kb="1"）与 msg-id，供长按面板使用。
// 只有「向量库 QA 检索类回复」（后端 from_kb）才是可反馈路由 —— 固定话术/计算面板等
// 长按不生效（没有可反馈的问答价值，避免污染收集数据）。
function initFeedbackButtons() {
  const originalRenderSecretaryReply = window.renderSecretaryReply;
  window.renderSecretaryReply = function(data, msgEl) {
    const result = originalRenderSecretaryReply.apply(this, arguments);
    const messagesEl = document.getElementById('messages');
    let target = (msgEl && msgEl.classList && msgEl.classList.contains('secretary')) ? msgEl : null;
    if (!target) {
      const last = messagesEl ? messagesEl.lastElementChild : null;
      target = (last && last.classList && last.classList.contains('secretary')) ? last : null;
    }
    if (target && !target.getAttribute('data-msg-id')) {
      target.setAttribute('data-msg-id', 'msg-' + Date.now() + '-' + Math.random().toString(36).substr(2, 6));
      target.setAttribute('data-from-kb', data.from_kb ? '1' : '0');
    }
    return result;
  };
}

// 初始化反馈互动
initFeedbackButtons();

// 天气查询功能
async function queryWeather(message) {
  try {
    const response = await fetch('/api/weather/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._apiHeaders() },
      credentials: 'include',
      body: JSON.stringify({ message: message })
    });
    
    const data = await response.json();
    
    if (data.ok) {
      return {
        location: data.location,
        weather: data.weather,
        temperature: data.temperature,
        humidity: data.humidity,
        wind_speed: data.wind_speed,
        data_source: data.data_source
      };
    } else {
      throw new Error(data.error || '天气查询失败');
    }
  } catch (error) {
    console.error('天气查询错误:', error);
    throw error;
  }
}



// URL 参数 ?q=<内容>：打开页面后自动发一条消息，用于「快捷方式直达」场景
// （浏览器书签 / 浏览器扩展划词查询，见 接口契约.md）。
// 云端需口令时先等解锁（authUnlocked=false 期间不发，避免 401），最多等 60 秒。
(function initAutoQuery() {
  const q = (new URLSearchParams(location.search).get('q') || '').trim();
  if (!q) return;
  let waited = 0;
  const timer = setInterval(function () {
    waited += 400;
    let unlocked = true;
    try {   // authUnlocked 由认证逻辑维护；探不到（旧版/未声明）就不等待
      unlocked = (typeof authUnlocked === 'undefined') ? true : authUnlocked;
    } catch (e) { unlocked = true; }
    if (unlocked === false && waited < 60000) return;
    clearInterval(timer);
    // 稍等首屏问候渲染完成，再发用户消息，避免气泡顺序颠倒
    setTimeout(function () {
      if (typeof sendChat === 'function') sendChat(q);
    }, 600);
  }, 400);
})();
