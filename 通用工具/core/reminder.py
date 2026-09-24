# -*- coding: utf-8 -*-
"""
礼包与活动截止提醒（web 会话层使用，用户口径 2026-09-03 修订）

触发规则（全部不去重：每次调用 collect_reminders 都返回当日应提醒内容）：
- 每周四：微信公众号领取礼包（提示带周期周数）
- 每周一、周日：每周礼包（提示带周期周数）
- 每月 1 号与最后一天：月度资源兑换
- 活动日历（游戏内活动）＝表格 知识库/活动/活动日历.xlsx，实时读取：
  · 活动截止（end）与兑换截止（ex_end）各自在截止前 2 天 / 1 天 / 当天持续提醒；
  · **截止时刻统一为截止日当天上午 10:00**（用户口径 2026-09-14）——提醒措辞必须带上
    该时刻；截止日当天一旦超过 10:00 即视为已结束，不再提醒（避免让用户按「当天结束」理解而超时）；
  · 活动开启（start）在开启前 1 天预告、开启当日（10:00 起）至开启后第 2 天在顶端事务提醒展示
    （用户口径 2026-09-16）；开启当日 10:00 前与开启前 1 天均显示「将于 … 10:00 开启」预告，
    不提前宣布已开启；
  · 排除法定节假日（中秋/春节等属节假日祝福，不是游戏活动，避免被误报成「活动截止」）；
  · 活动截止与兑换截止同一天（ex_end == end）时合并为一条，不重复刷屏。

不做状态持久化，也不做自然日去重：同一天反复调用会反复返回相同内容。
返回值为纯提醒内容行（不含称呼/动作前缀）——口吻整合由消费方负责
（web_app 的 GET / 路由把提醒织入初始问候语的语义段落中）。
"""
import json
import calendar as _calmod
import datetime

from tools.knowledge import _norm_event, DEADLINE_TIME, DEADLINE_TIME_LABEL


def _load_events(path=None):
    """活动提醒用的事件列表（**只含游戏内活动**）。

    · 不传 path → 走统一入口 `load_events_normalized()`（实时读 知识库/活动/活动日历.xlsx）；
    · 传 path（测试注入）→ 读该 JSON，归一化（_norm_event 智能识别中英文键与自由文本区间）。
    法定节假日属「真实日历」，已与游戏内活动分离（知识库/活动/节假日.json），不在本入口。"""
    if path is None:
        from tools.knowledge import load_events_normalized
        return load_events_normalized()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        evs = data.get('events') if isinstance(data, dict) else None
        if not isinstance(evs, list):
            return []
        return [_norm_event(e) for e in evs if isinstance(e, dict)]
    except (OSError, ValueError):
        return []


def _deadline_when(days_left):
    """截止日相对今天的口语说法。"""
    return {0: '今日', 1: '明天', 2: '后天'}.get(days_left, '')


# 每周礼包周期周数锚点（用户口径 2026-09-24）：
# 周期每 7 天更新，更新时刻在周四之后（周五 0 点起进入新一期）。
# 锚定 2026-09-25（周五）= 第 185 期，等价于 2026-09-24（周四）仍属第 184 期。
_WEEK_CYCLE_ANCHOR = datetime.date(2026, 9, 25)
_WEEK_CYCLE_ANCHOR_NO = 185


def _week_cycle_no(today):
    """今天所属的每周礼包周期周数（周五 0 点起 +1）。"""
    return _WEEK_CYCLE_ANCHOR_NO + (today - _WEEK_CYCLE_ANCHOR).days // 7


def _deadline_line(name, deadline, today, now, tail_prefix, label=''):
    """生成一条截止提醒行；不在提醒窗口（前 2 天）内、或截止日已过 10:00 → 返回 None。"""
    d = (deadline - today).days
    if not (0 <= d <= 2):
        return None
    if d == 0 and now and now.time() >= DEADLINE_TIME:
        return None      # 截止日 10:00 已过 → 已结束，不再提醒，避免让用户误以为还来得及
    return '活动「%s」%s将于 %s（%s）%s 截止%s' % (
        name, label, deadline.strftime('%m月%d日'), _deadline_when(d),
        DEADLINE_TIME_LABEL, tail_prefix)


def _deadline_items(ev, today, now=None):
    """单个活动的截止提醒行：活动截止（end）与兑换截止（ex_end）各自在
    前 2 天 / 1 天 / 当天触发（截止时刻 = 当天上午 10:00）；两者同日时合并为一条。"""
    name = (ev.get('name') or '').strip() or '未命名活动'
    end, ex_end = ev.get('end'), ev.get('ex_end')
    out = []
    same_day = bool(end and ex_end and end == ex_end)
    if end:
        tail = '，记得推完进度并及时兑换奖励。' if same_day else '，记得推完进度。'
        line = _deadline_line(name, end, today, now, tail)
        if line:
            out.append(line)
    if ex_end and not same_day:
        line = _deadline_line(name, ex_end, today, now, '，别漏了兑换。', label='兑换期')
        if line:
            out.append(line)
    return out


def _start_items(ev, today, now=None):
    """单个活动的「开启」提醒：开始前 1 天预告、开始当日（维护完成后）与开启后第 2 天
    （最早两天）在顶端事务提醒展示（用户口径 2026-09-16）。

    口径：活动开启提示**仅在开始日 10:00 之后**（视作维护已完成）显示「已开启」；开始日
    维护完成前与开始前 1 天均显示「将于 … 维护完成后开启」预告，不提前宣布已开启。"""
    name = (ev.get('name') or '').strip() or '未命名活动'
    start = ev.get('start')
    if not start:
        return []
    d = (today - start).days
    if d == -1:
        return ['活动「%s」将于明日（%s）维护完成后开启，敬请期待。' % (
            name, start.strftime('%m月%d日'))]
    if d == 0:
        if now and now.time() >= DEADLINE_TIME:
            return ['活动「%s」已于今日开启，快去参与吧。' % name]
        return ['活动「%s」将于今日（%s）维护完成后开启，敬请期待。' % (
            name, start.strftime('%m月%d日'))]
    if d == 1:
        return ['活动「%s」已开启第 2 天，别错过限时内容。' % name]
    return []


def collect_reminders(calendar_path=None, today=None, now=None):
    """收集当前应触发的提醒内容行列表，不去重、不写状态。
    每行是一条完整语义句，供消费方按语义换行拼入问候。
    calendar_path / today / now 可注入，便于测试：
    · 只传 today（纯日期）时视作当天 00:00，未过上午 10:00 截止时刻；
    · 传 now（datetime）时精确判定截止日是否已过 10:00。"""
    if now is None:
        now = (datetime.datetime.combine(today, datetime.time(0, 0))
               if today is not None else datetime.datetime.now())
    today = now.date()
    items = []

    # 每周礼包：周一 / 周日（weekday：周一=0 … 周日=6），带当前周期周数
    if today.weekday() in (0, 6):
        day_name = '一' if today.weekday() == 0 else '日'
        items.append(f'今天是周{day_name}，记得领取每周礼包（第 {_week_cycle_no(today)} 周）。')

    # 微信公众号礼包：每周四，带当前周期周数
    if today.weekday() == 3:
        items.append(f'今天是周四，记得去微信公众号领取礼包（第 {_week_cycle_no(today)} 周）。')

    # 月度资源兑换：1 号与最后一天
    last_day = _calmod.monthrange(today.year, today.month)[1]
    if today.day in (1, last_day):
        edge = '月初' if today.day == 1 else '月底'
        items.append(f'已经{edge}了，记得兑换相关资源，而不是每月礼包。')

    # 活动 / 兑换截止：截止前 2 天、1 天与当天持续提醒（截止时刻 = 当天上午 10:00，
    # 过了 10:00 当天不再提醒）。只提醒「游戏内活动」——法定节假日属「真实日历」，
    # 已与游戏内活动分离（节假日祝福走 _today_holiday 初始问候块，不进截止横幅）。
    for ev in _load_events(calendar_path):
        if (ev.get('type') or '').strip() == '法定节假日':
            continue
        items.extend(_deadline_items(ev, today, now))
        items.extend(_start_items(ev, today, now))

    return items
