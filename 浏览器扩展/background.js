/* 阿卡莉插件 · 后台（Manifest V3 Service Worker）
 * 职责：① 点图标 / 快捷键 → 打开「用户端本体」窗口；② 右键划词 → 带着文字直接问阿卡莉。
 * 未配置服务器地址时，先打开配置页（launcher.html）做首次引导。
 */
const APP_PAGE = 'app.html';
const LAUNCHER_PAGE = 'launcher.html';
const MENU_ID = 'akali-ask-selection';

function clean(v) {
  let s = (v || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
  return s.replace(/\/+$/, '');
}

async function getCfg() {
  const d = await chrome.storage.local.get(['baseUrl', 'token']);
  return { baseUrl: clean(d.baseUrl), token: (d.token || '').trim() };
}

/** 拼扩展页 URL：把服务器地址/口令随 URL 传给 bridge.js（同步注入，避免先后时序问题） */
function pageUrl(page, base, token, extra) {
  const p = new URLSearchParams();
  if (base) p.set('base', base);
  if (token) p.set('token', token);
  if (extra) Object.keys(extra).forEach((k) => p.set(k, extra[k]));
  const qs = p.toString();
  return chrome.runtime.getURL(page) + (qs ? '?' + qs : '');
}

async function openLauncher() {
  await chrome.windows.create({
    url: chrome.runtime.getURL(LAUNCHER_PAGE),
    type: 'popup', width: 560, height: 680, focused: true
  });
}

async function openApp(extra) {
  const { baseUrl, token } = await getCfg();
  if (!baseUrl) { await openLauncher(); return; }   // 首次使用 → 先配置
  await chrome.windows.create({
    url: pageUrl(APP_PAGE, baseUrl, token, extra),
    type: 'popup', width: 470, height: 920, focused: true
  });
}

chrome.action.onClicked.addListener(() => { openApp(); });

chrome.commands.onCommand.addListener((cmd) => {
  if (cmd === 'open-akali') openApp();
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: '用阿卡莉查询：“%s”',
      contexts: ['selection']
    });
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === MENU_ID && info.selectionText) {
    openApp({ q: info.selectionText.trim().slice(0, 200) });
  }
});
