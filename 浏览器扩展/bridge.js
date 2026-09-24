/* 阿卡莉插件 · 配置桥接（由 构建扩展.py 注入到 app.html 的 <head> 最前面，**最先执行**）
 *
 * 作用：把「服务器地址 / 邀请口令」注入用户端页面，让**用户端本体**能在浏览器扩展里运行。
 *
 * 注入的三个变量都是用户端原生支持的跨域入口（见 用户端特用/web/index.html 顶部）：
 *   window.ARKLY_API_BASE      → 所有 fetch('/api/…') 自动改写为 <服务器>/api/…
 *   window.ASSET_BASE          → /kb/ /avatar/ 静态资源指向服务器
 *                                （不设则会在扩展里解析成 chrome-extension:// 导致图裂）
 *   window.AKALI_NO_SELFDESTRUCT → 关闭「服务端重启即让旧页面自毁」逻辑
 *                                （插件窗口是常驻应用窗口，不该被自动关掉）
 *
 * 配置来源（**同步**读取，保证在用户端脚本执行前就绪）：
 *   1) URL 参数 ?base=…&token=…   —— background.js 打开窗口时拼上（最高优先级）
 *   2) localStorage 镜像          —— 配置页保存时同步写入（扩展域内共享，故可同步读）
 *   3) 缺省 http://127.0.0.1:8000 —— 本机调试用
 */
(function () {
  const DEFAULT_BASE = 'http://127.0.0.1:8000';

  function clean(v) {
    let s = (v || '').trim();
    if (!s) return '';
    if (!/^https?:\/\//i.test(s)) s = 'http://' + s;   // 允许只填 IP / 域名
    return s.replace(/\/+$/, '');
  }

  let qs;
  try { qs = new URLSearchParams(location.search); } catch (e) { qs = new URLSearchParams(''); }

  let base = clean(qs.get('base'));
  let token = (qs.get('token') || '').trim();

  if (!base) { try { base = clean(localStorage.getItem('arkly_ext_base')); } catch (e) {} }
  if (!token) { try { token = (localStorage.getItem('arkly_ext_token') || '').trim(); } catch (e) {} }
  if (!base) base = DEFAULT_BASE;

  window.ARKLY_API_BASE = base;
  window.ASSET_BASE = base + '/kb';
  window.AKALI_NO_SELFDESTRUCT = true;
  window.AKALI_BOOT_ID = '__AKALI_EXT__';   // 占位（自毁已关闭，仅保持字段存在）

  // 口令：用户端自身会读 localStorage.arkly_api_token 并随请求带 X-API-Token
  if (token) { try { localStorage.setItem('arkly_api_token', token); } catch (e) {} }

  // 记住本次配置（同域 localStorage 在扩展内共享，配置页下次可同步读取）
  try {
    localStorage.setItem('arkly_ext_base', base);
    if (token) localStorage.setItem('arkly_ext_token', token);
  } catch (e) {}
})();
