# -*- coding: utf-8 -*-
"""开发者控制台页面（运营/管理侧，用户口径 2026-09-25）。

设计
----
· 运营类内容（用量视图、账号与准入、反馈等）**集中在这一页**，与用户端分开。
· 页面本身**不含任何数据**，公开访问也无害；所有数据都通过 /api/admin/* 拉取，
  而那些接口只认 `AKALI_ADMIN_UIDS` 白名单里的口令 —— 不在白名单直接 403。
· 用户侧只有只读的 /api/me（"用户相关的内容只可读"）。

口令来源：优先 URL 的 `?token=`，其次浏览器 localStorage（与主站共用同一份）。
"""
from fastapi.responses import HTMLResponse

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>阿卡莉 · 开发者控制台</title>
<style>
  :root { --bg:#f5f2ea; --card:#fffdf8; --line:#e2dbcc; --ink:#3d3830;
          --soft:#8b8272; --accent:#6b8caa; --warn:#c05050; }
  * { box-sizing:border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--ink);
         font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  h1 { font-size:19px; margin:0 0 4px; }
  .sub { color:var(--soft); font-size:13px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:14px 16px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 10px; display:flex; align-items:center; gap:8px; }
  .card h2 .tag { font-size:11px; font-weight:400; color:var(--soft);
                  border:1px solid var(--line); border-radius:8px; padding:1px 7px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line);
          white-space:nowrap; }
  th { color:var(--soft); font-weight:600; font-size:12px; }
  tr:last-child td { border-bottom:none; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .bar { position:relative; height:7px; border-radius:4px; background:#eee7d9; min-width:60px; }
  .bar > i { position:absolute; inset:0 auto 0 0; border-radius:4px; background:var(--accent); }
  .bar.warn > i { background:var(--warn); }
  .pill { display:inline-block; font-size:11px; border-radius:8px; padding:1px 7px;
          border:1px solid var(--line); color:var(--soft); }
  .pill.on { color:#3f7a4f; border-color:#bcd9c4; background:#f2faf4; }
  .pill.off { color:var(--warn); border-color:#e6c3c3; background:#fdf4f4; }
  .mono { font-family:ui-monospace,Consolas,monospace; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  button { font:inherit; padding:6px 13px; border:1px solid var(--accent); color:var(--accent);
           background:transparent; border-radius:9px; cursor:pointer; }
  button:hover { background:rgba(107,140,170,.08); }
  input { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:9px;
          background:#fff; min-width:230px; }
  .msg { color:var(--warn); font-size:13px; }
  .empty { color:var(--soft); font-size:13px; padding:8px 0; }
  .foot { color:var(--soft); font-size:12px; margin-top:18px; }
</style>
</head>
<body>
  <h1>阿卡莉 · 开发者控制台</h1>
  <div class="sub">运营侧集中入口 —— 仅 <span class="mono">AKALI_ADMIN_UIDS</span> 白名单内的口令可读取数据。</div>

  <div class="card">
    <h2>访问口令 <span class="tag">必填，仅存本机浏览器</span></h2>
    <div class="row">
      <input id="tok" type="password" placeholder="粘贴开发者口令">
      <button id="save">保存并加载</button>
      <button id="reload">刷新数据</button>
      <span id="who"></span>
    </div>
    <div class="msg" id="err"></div>
  </div>

  <div class="card">
    <h2>今日用量 <span class="tag" id="usageMeta">每 uid 消耗</span></h2>
    <div id="usage"></div>
  </div>

  <div class="card">
    <h2>账号与准入 <span class="tag">开关在 .env · 用户表在 users.json</span></h2>
    <div id="acct"></div>
  </div>

  <div class="foot">
    用户相关的内容只可读（用户侧仅 /api/me）；本页所有数据来自 /api/admin/*，均受白名单限制。
  </div>

<script>
(function () {
  var KEY = 'akali_admin_token';
  var $ = function (id) { return document.getElementById(id); };

  // 口令来源：URL ?token= 优先（便于分享），否则读本机 localStorage
  var qs = new URLSearchParams(location.search).get('token');
  if (qs) { try { localStorage.setItem(KEY, qs); } catch (e) {} }
  var token = qs || '';
  try { token = token || localStorage.getItem(KEY) || ''; } catch (e) {}
  $('tok').value = token;

  function fmt(n) { return (n || 0).toLocaleString('en-US'); }

  function req(path) {
    return fetch(path, { headers: token ? { 'X-API-Token': token } : {} })
      .then(function (r) { return r.json().then(function (d) { return { s: r.status, d: d }; }); });
  }

  function renderUsage(d) {
    var rows = d.rows || [];
    if (!rows.length) {
      $('usage').innerHTML = '<div class="empty">今天还没有用量记录。（维度：' + d.dim + '，存储：' + d.backend + '）</div>';
      return;
    }
    var h = '<table><tr><th>uid</th><th>备注</th><th>套餐</th><th class="num">轮次</th>'
          + '<th class="num">输入</th><th class="num">输出</th><th class="num">合计</th>'
          + '<th>占额度</th><th class="num">参考成本</th></tr>';
    rows.forEach(function (r) {
      var pct = r.limit ? Math.min(100, r.tok_total / r.limit * 100) : 0;
      h += '<tr>'
        + '<td class="mono">' + r.key + '</td>'
        + '<td>' + (r.name || '') + '</td>'
        + '<td>' + r.plan + '</td>'
        + '<td class="num">' + r.rounds + '</td>'
        + '<td class="num">' + fmt(r.tok_in) + '</td>'
        + '<td class="num">' + fmt(r.tok_out) + '</td>'
        + '<td class="num"><b>' + fmt(r.tok_total) + '</b></td>'
        + '<td><div class="bar' + (pct >= 80 ? ' warn' : '') + '"><i style="width:' + pct.toFixed(1) + '%"></i></div>'
        + '<span class="pill">' + pct.toFixed(1) + '% / ' + fmt(r.limit) + '</span></td>'
        + '<td class="num">￥' + (r.cost || 0).toFixed(4) + '</td>'
        + '</tr>';
    });
    h += '</table><div class="empty">合计 ' + fmt(d.total_tok) + ' token，参考成本 ￥'
       + (d.total_cost || 0).toFixed(4) + '　（' + d.day + '，维度：' + d.dim + '，存储：' + d.backend + '）</div>';
    $('usage').innerHTML = h;
  }

  function renderAcct(d) {
    var users = d.users || [];
    var h = '<div class="row" style="margin-bottom:10px">'
          + '<span class="pill ' + (d.allow_other_users ? 'on' : 'off') + '">'
          + '总开关 allow_other_users = ' + d.allow_other_users + '</span>'
          + '<span class="pill">开发者 ' + d.owner_uid + '</span>'
          + '<span class="pill">下一个 uid ' + d.next_uid + '</span>'
          + '<span class="pill">管理白名单 ' + (d.admin_uids || []).join(', ') + '</span></div>';
    h += '<table><tr><th>uid</th><th>名称</th><th>角色</th><th>状态</th><th>口令</th></tr>';
    users.forEach(function (u) {
      h += '<tr><td class="mono">' + u.uid + '</td><td>' + (u.name || '') + '</td>'
        + '<td>' + u.role + '</td>'
        + '<td><span class="pill ' + (u.enabled ? 'on' : 'off') + '">'
        + (u.enabled ? '启用' : '停用') + '</span></td>'
        + '<td class="mono">' + (u.token || '') + '</td></tr>';
    });
    h += '</table><div class="empty">' + (d.hint || '') + '</div>';
    $('acct').innerHTML = h;
  }

  function load() {
    $('err').textContent = '';
    if (!token) { $('err').textContent = '请先填写口令。'; return; }
    req('/api/admin/usage').then(function (r) {
      if (r.s === 403) { $('err').textContent = '该口令不在管理白名单内（AKALI_ADMIN_UIDS）。'; return; }
      if (r.s === 401) { $('err').textContent = '口令无效或已失效。'; return; }
      renderUsage(r.d);
      $('who').innerHTML = '<span class="pill on">已连接</span>';
    }).catch(function (e) { $('err').textContent = '请求失败：' + e; });
    req('/api/admin/accounts').then(function (r) { if (r.s === 200) renderAcct(r.d); });
  }

  $('save').addEventListener('click', function () {
    token = $('tok').value.trim();
    try { localStorage.setItem(KEY, token); } catch (e) {}
    load();
  });
  $('reload').addEventListener('click', load);
  if (token) load();
})();
</script>
</body>
</html>
"""


def admin_page() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)
