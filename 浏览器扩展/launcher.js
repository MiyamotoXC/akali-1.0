/* 阿卡莉插件 · 配置页逻辑
 * 保存服务器地址/口令到 chrome.storage.local（后台读取）与 localStorage（bridge 同步读取），
 * 然后打开用户端窗口。
 */
const $ = (id) => document.getElementById(id);

function clean(v) {
  let s = (v || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
  return s.replace(/\/+$/, '');
}

function setStatus(text, cls) {
  const el = $('status');
  el.textContent = text || '';
  el.className = 'status' + (cls ? ' ' + cls : '');
}

async function load() {
  const d = await chrome.storage.local.get(['baseUrl', 'token']);
  // 兜底：storage 为空但 localStorage 有（例如直接用 URL 打开过 app.html）
  let base = d.baseUrl || '';
  let token = d.token || '';
  try {
    if (!base) base = localStorage.getItem('arkly_ext_base') || '';
    if (!token) token = localStorage.getItem('arkly_ext_token') || '';
  } catch (e) {}
  $('base').value = base || '';
  $('token').value = token || '';
}

async function readForm() {
  const baseUrl = clean($('base').value);
  const token = ($('token').value || '').trim();
  await chrome.storage.local.set({ baseUrl, token });
  try {   // 供 bridge.js 同步读取（app 页与配置页同域，localStorage 共享）
    localStorage.setItem('arkly_ext_base', baseUrl);
    if (token) localStorage.setItem('arkly_ext_token', token);
    else localStorage.removeItem('arkly_ext_token');
  } catch (e) {}
  return { baseUrl, token };
}

async function test() {
  const { baseUrl, token } = await readForm();
  if (!baseUrl) { setStatus('请先填服务器地址', 'err'); return; }
  setStatus('正在连接…');
  try {
    const r = await fetch(baseUrl + '/api/auth/status', {
      headers: token ? { 'X-API-Token': token } : {}
    });
    const d = await r.json();
    if (!r.ok) { setStatus('服务返回 HTTP ' + r.status, 'err'); return; }
    if (d.auth_required && !d.authenticated) { setStatus('连得上，但口令不对或不完整', 'err'); return; }
    setStatus('连接成功 · ' + (d.run_mode === 'cloud' ? '云端模式' : '本地模式'), 'ok');
  } catch (e) {
    setStatus('连不上：' + ((e && e.message) || e), 'err');
  }
}

async function saveAndOpen() {
  const { baseUrl, token } = await readForm();
  if (!baseUrl) { setStatus('请先填服务器地址', 'err'); return; }
  const p = new URLSearchParams();
  p.set('base', baseUrl);
  if (token) p.set('token', token);
  await chrome.windows.create({
    url: chrome.runtime.getURL('app.html') + '?' + p.toString(),
    type: 'popup', width: 470, height: 920, focused: true
  });
  window.close();
}

$('save').addEventListener('click', saveAndOpen);
$('test').addEventListener('click', test);
$('base').addEventListener('change', test);

load();
