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
  tr.owner > td { background:#eef4f9; }
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
  select { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:9px;
           background:#fff; }
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
    <h2>用量统计 <span class="tag" id="usageMeta">每 uid 消耗</span></h2>
    <div class="row" style="margin-bottom:10px">
      <label for="usageDay">查看日期：</label>
      <select id="usageDay"><option value="">加载中…</option></select>
    </div>
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

  // 口令来源，按优先级：
  //   ① URL ?token=（便于收藏一个免输入的直达链接）
  //   ② 本页自己的 localStorage（点过「保存并加载」）
  //   ③ **主站共用的 arkly_api_token** —— 用户口径 2026-09-25：此前只读 ① ②，
  //      而主站存的是另一个 key，于是在主站登录过、到控制台仍要手动粘口令。
  var qs = new URLSearchParams(location.search).get('token');
  var token = qs || '';
  try {
    token = token || localStorage.getItem(KEY) || localStorage.getItem('arkly_api_token') || '';
  } catch (e) {}
  if (token) { try { localStorage.setItem(KEY, token); } catch (e) {} }
  $('tok').value = token;

  function fmt(n) { return (n || 0).toLocaleString('en-US'); }

  function req(path) {
    return fetch(path, { headers: token ? { 'X-API-Token': token } : {} })
      .then(function (r) { return r.json().then(function (d) { return { s: r.status, d: d }; }); });
  }

  // 同一个用户只统计总量，点开才显示细则（用户口径 2026-09-26）：
  // 细则 = 该用户按调用类型（闲聊/攻略生成/检索重排…）的 次数/输入/输出 + 带日期的请求明细。
  function renderDetail(r, d) {
    var h = '';
    var calls = r.calls || {};
    var kinds = Object.keys(calls);
    if (kinds.length) {
      h += '<table style="margin:6px 0"><tr><th>调用类型</th><th class="num">次数</th>'
         + '<th class="num">输入</th><th class="num">输出</th><th class="num">合计</th></tr>';
      kinds.forEach(function (k) {
        var c = calls[k];
        h += '<tr><td>' + k + '</td><td class="num">' + fmt(c.n) + '</td>'
          + '<td class="num">' + fmt(c.tok_in) + '</td><td class="num">' + fmt(c.tok_out) + '</td>'
          + '<td class="num">' + fmt((c.tok_in || 0) + (c.tok_out || 0)) + '</td></tr>';
      });
      h += '</table>';
    } else {
      h += '<div class="empty">无 LLM 调用记录。</div>';
    }
    var rc = (d.recent || []).filter(function (e) { return e.key === r.key; });
    if (rc.length) {
      h += '<table><tr><th>日期</th><th>时间</th><th class="num">本轮token(估算)</th></tr>';
      rc.slice().reverse().forEach(function (e) {
        h += '<tr><td class="mono">' + (e.date || '') + '</td><td class="mono">' + e.ts + '</td>'
          + '<td class="num">' + fmt(e.tok) + '</td></tr>';
      });
      h += '</table>';
    }
    return h;
  }

  function renderUsage(d) {
    var rows = d.rows || [];
    if (!rows.length) {
      $('usage').innerHTML = '<div class="empty">' + d.day + ' 还没有用量记录。（维度：' + d.dim + '，存储：' + d.backend + '）</div>';
      return;
    }
    var h = '<table><tr><th>uid</th><th>备注</th><th>套餐</th><th class="num">流量(请求)</th>'
          + '<th class="num">LLM调用</th>'
          + '<th class="num">输入</th><th class="num">输出</th><th class="num">合计</th>'
          + '<th>占额度</th><th class="num">参考成本</th><th>细则</th></tr>';
    rows.forEach(function (r) {
      // limit = 0 表示不限量（开发者本人），不画进度条
      var unlimited = !r.limit;
      var pct = unlimited ? 0 : Math.min(100, r.tok_total / r.limit * 100);
      h += '<tr' + (r.is_owner ? ' class="owner"' : '') + '>'
        + '<td class="mono">' + r.key + '</td>'
        + '<td>' + (r.name || '') + (r.is_owner ? ' <span class="pill on">我</span>' : '') + '</td>'
        + '<td>' + (r.is_owner ? '开发者' : ({'developer':'开发者','whitelist':'白名单','paid':'付费','free':'免费','unlimited':'不限量'}[r.plan] || r.plan)) + '</td>'
        + '<td class="num">' + (r.req_count || 0) + '</td>'
        + '<td class="num">' + fmt(r.rounds) + '</td>'
        + '<td class="num">' + fmt(r.tok_in) + '</td>'
        + '<td class="num">' + fmt(r.tok_out) + '</td>'
        + '<td class="num"><b>' + fmt(r.tok_total) + '</b></td>'
        + '<td>' + (unlimited
            ? '<span class="pill on">不限量</span>'
            : '<div class="bar' + (pct >= 80 ? ' warn' : '') + '"><i style="width:' + pct.toFixed(1) + '%"></i></div>'
              + '<span class="pill">' + pct.toFixed(1) + '% / ' + fmt(r.limit) + '</span>')
        + '</td>'
        + '<td class="num">￥' + (r.cost || 0).toFixed(4) + '</td>'
        + '<td><details><summary style="cursor:pointer">点开</summary>' + renderDetail(r, d) + '</details></td>'
        + '</tr>';
    });
    h += '</table><div class="empty">合计 ' + fmt(d.total_tok) + ' token，参考成本 ￥'
       + (d.total_cost || 0).toFixed(4) + '　（' + d.day + '，维度：' + d.dim + '，存储：' + d.backend + '）</div>';
    // 请求明细：全部请求的流水（本轮token=0 表示该请求未走 LLM，如攻略/计算）
    var rc = d.recent || [];
    h += '<div style="margin-top:12px;font-size:12px;color:var(--soft);font-weight:600">'
       + '请求明细（最近 ' + rc.length + ' 条 · 按天持久化，可回看历史日）</div>';
    if (!rc.length) {
      h += '<div class="empty">暂无请求记录。</div>';
    } else {
      h += '<table style="margin-top:6px"><tr><th>日期</th><th>时间</th><th>uid</th><th>备注</th><th class="num">本轮token</th></tr>';
      rc.slice().reverse().forEach(function (e) {
        h += '<tr><td class="mono">' + (e.date || '') + '</td><td class="mono">' + e.ts + '</td>'
          + '<td class="mono">' + e.key + '</td>'
          + '<td>' + (e.name || '') + '</td><td class="num">' + fmt(e.tok) + '</td></tr>';
      });
      h += '</table>';
    }
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
    h += '<table><tr><th>uid</th><th>用户uid</th><th>名称</th><th>角色</th><th>状态</th><th>口令</th></tr>';
    users.forEach(function (u) {
      h += '<tr><td class="mono">' + u.uid + '</td>'
        + '<td class="mono">' + (u.user_uid || '') + '</td>'
        + '<td>' + (u.name || '') + '</td>'
        + '<td>' + u.role + '</td>'
        + '<td><span class="pill ' + (u.enabled ? 'on' : 'off') + '">'
        + (u.enabled ? '启用' : '停用') + '</span></td>'
        + '<td class="mono">' + (u.token || '') + '</td></tr>';
    });
    h += '</table><div class="empty">' + (d.hint || '') + '</div>';
    $('acct').innerHTML = h;
  }

  // 拉取并渲染某天用量；day 为空则取当天。同时用返回的 dates 填充日期选择器，
  // 便于开发者在历史视图里随时切换查阅（历史用量永不自动清理，用户口径 2026-10-02）。
  function loadUsage(day) {
    $('err').textContent = '';
    if (!token) { $('err').textContent = '请先填写口令，或用 /admin?token=<开发者口令> 打开（可存为书签，免每次输入）。'; return; }
    var url = '/api/admin/usage' + (day ? ('?day=' + encodeURIComponent(day)) : '');
    req(url).then(function (r) {
      if (r.s === 403) { $('err').textContent = '该口令不在管理白名单内（AKALI_ADMIN_UIDS）。'; return; }
      if (r.s === 401) { $('err').textContent = '口令无效或已失效。'; return; }
      var d = r.d;
      // 填充日期下拉：当前所选日期置顶，其后为所有有记录的日期（降序）
      var sel = $('usageDay');
      sel.innerHTML = '';
      var opt0 = document.createElement('option');
      opt0.value = d.day; opt0.textContent = d.day + '（当前）';
      sel.appendChild(opt0);
      (d.dates || []).forEach(function (dt) {
        if (dt === d.day) return;
        var o = document.createElement('option');
        o.value = dt; o.textContent = dt;
        sel.appendChild(o);
      });
      sel.value = d.day;
      renderUsage(d);
      $('who').innerHTML = '<span class="pill on">已连接</span>';
    }).catch(function (e) { $('err').textContent = '请求失败：' + e; });
  }

  function load() {
    if (!token) { $('err').textContent = '请先填写口令，或用 /admin?token=<开发者口令> 打开（可存为书签，免每次输入）。'; return; }
    loadUsage(null);
    req('/api/admin/accounts').then(function (r) { if (r.s === 200) renderAcct(r.d); });
  }

  $('usageDay').addEventListener('change', function () { loadUsage(this.value); });

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
