# -*- coding: utf-8 -*-
"""中国法定节假日来源。

用户口径 2026-09-14：法定节假日应从「实际联网日历」读取，
而不是知识库的手工数据（后者需手动维护、易过期、且与国务院放假安排可能脱节）。

与「游戏内活动」严格区分：游戏内活动在表格 知识库/活动/活动日历.xlsx；
法定节假日（真实日历）在本模块 + 兜底文件 知识库/活动/节假日.json。

实现策略（三级）：
- 优先请求免费公开的中国法定节假日 API（timor.tech 为主，javax 备选）；
  联网「明确返回」的结果（无论是否是节假日）一律采纳，不回退知识库，
  避免知识库过期数据把普通日子误报成节假日（如中秋日期写错）。
- 联网不可用时，回退 知识库/活动/节假日.json（仅作离线兜底，按「法定节假日」类型过滤）。
- 祝福语（阿卡莉口吻 greeting / summary）按节假日名从 节假日.json 匹配；
  在线名称未在知识库收录时退回通用祝福。
"""
import datetime
import json
import urllib.request
import urllib.error

_ONLINE_CACHE = {}  # date_iso -> result dict（进程内按日期缓存，避免重复请求 / 重复超时）

_PRIMARY = "https://timor.tech/api/holiday/info/{date}"
_SECONDARY = "https://api.javax.net.cn/holiday/info/{date}"
_TIMEOUT = 4


def _fetch_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; AkaliAgent/1.0)"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_timor(d):
    """timor.tech 响应：
    type.type: 0=工作日 1=周末 2=节假日 3=调休补班；节假日时 name 为节名（如 国庆节）。
    非节假日 holiday 字段为 null。"""
    if not isinstance(d, dict):
        return None
    t = d.get("type") or {}
    if t.get("type") == 2:
        hol = d.get("holiday") or {}
        name = str((hol.get("name") or t.get("name") or "")).strip() or None
        return {"holiday": True, "name": name}
    return {"holiday": False, "name": None}


def _parse_javax(d):
    """javax 响应：{"code":0,"data":{"holiday":true,"name":"..."}}；holiday=false 为非节假日。"""
    if not isinstance(d, dict):
        return None
    data = d.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("holiday") is True:
        return {"holiday": True, "name": str(data.get("name") or "").strip() or None}
    if data.get("holiday") is False:
        return {"holiday": False, "name": None}
    return None


def _online_lookup(date_iso):
    """按优先级请求在线 API，返回 {'holiday':bool,'name':str|None} 或 None（全部失败）。"""
    for url_tmpl, parser in ((_PRIMARY, _parse_timor), (_SECONDARY, _parse_javax)):
        try:
            d = _fetch_json(url_tmpl.format(date=date_iso))
            r = parser(d)
            if r is not None:
                return r
        except Exception:
            continue
    return None


def statutory_holiday(dt=None):
    """给定日期（默认今天）的中国法定节假日判定。

    返回 dict：{'date': iso, 'is_holiday': bool, 'name': str|None, 'source': 'online'|'kb'|'none'}
    is_holiday=True 时 name 为节假日名（如 '中秋节'）。

    联网明确返回「非节假日」时直接采纳（source='online'），即便知识库有旧数据也不误报。
    """
    dt = dt or datetime.date.today()
    date_iso = dt.isoformat()
    cached = _ONLINE_CACHE.get(date_iso)
    if cached is not None:
        return cached

    result = None
    online = _online_lookup(date_iso)
    if online is not None:
        # 联网有结果（无论真假）一律采纳，不回退知识库，避免过期数据误报
        result = {"date": date_iso, "is_holiday": bool(online["holiday"]),
                  "name": online["name"], "source": "online"}
    else:
        # 联网不可用 → 回退知识库活动日历（仅作离线兜底）
        try:
            from tools.knowledge import load_holiday_events, _event_status
            hit = None
            for e in load_holiday_events():
                if (e.get("type") or "") != "法定节假日":
                    continue
                if _event_status(e, dt) == "进行中":
                    hit = e
                    break
            if hit is not None:
                result = {"date": date_iso, "is_holiday": True,
                          "name": str(hit.get("name") or "").strip() or None,
                          "source": "kb"}
            else:
                result = {"date": date_iso, "is_holiday": False, "name": None, "source": "kb"}
        except Exception:
            result = {"date": date_iso, "is_holiday": False, "name": None, "source": "none"}

    _ONLINE_CACHE[date_iso] = result
    return result


def holiday_greeting(name, default=None):
    """按节假日名从真实日历（知识库/活动/节假日.json）匹配阿卡莉口吻祝福。

    先做精确名匹配，未命中再做子串模糊匹配（兼容在线名与知识库名细微差异，
    如 '中秋' vs '中秋节'）。在线名称未收录时退回通用祝福。
    返回 {'greeting':str,'summary':str,'opening':str}。

    opening（用户口径 2026-09-25）：**融合开场句** —— 把祝福与放假安排合成一句
    顺口说出来的话，随初始问候自然带出；不再用「节日名｜祝福」的通知式前缀。
    未配置 opening 的节日回退用 greeting，消费方无需特判。
    """
    greeting = ""
    summary = ""
    opening = ""
    try:
        from tools.knowledge import load_holiday_events
        nm = (name or "").strip()
        exact = None
        fuzzy = None
        for e in load_holiday_events():
            if (e.get("type") or "") != "法定节假日":
                continue
            en = str(e.get("name") or "").strip()
            if not en:
                continue
            if en == nm:
                exact = e
                break
            if not fuzzy and nm and (nm in en or en in nm):
                fuzzy = e
        hit = exact or fuzzy
        if hit is not None:
            greeting = str(hit.get("greeting") or "").strip()
            summary = str(hit.get("summary") or "").strip()
            opening = str(hit.get("opening") or "").strip()
    except Exception:
        pass
    if not greeting:
        greeting = default or "节日快乐，指挥官。今天也好好休息。"
    return {"greeting": greeting, "summary": summary, "opening": opening}
