# -*- coding: utf-8 -*-
"""确定性路由回归基线（用户口径 2026-10-05）
=============================================

为什么需要
----------
改造「确定性前置 + 正则路由」最大的风险是**悄悄改变既有行为**：规则之间的顺序依赖
（"必须早于 X 门控"）意味着动一条就可能牵动一条链。没有基线，就没人敢动、也没人敢删。

本模块把**当前路由结论**固化成可重复执行的基线：

    钉什么：guard 是否接管 / 三条业务前置闸是否命中 / legacy 是否接住 / 是否来自知识库
    不钉什么：知识库问答（KB-LLM）的**作答措辞** —— 那由模型生成，本来就会变
              （`from_kb=True` 的用例只比对路由结论）

用法
----
    python -m agent.route_cases --save     # 把当前行为记为基线（改路由前后各跑一次）
    python -m agent.route_cases --check    # 与基线对比；不一致 → 退出码 1（可挂 CI）
    python -m agent.route_cases --list     # 打印用例清单
    python -m agent.route_cases --save --update   # 确认是有意变更后，就地更新基线

基线文件：`agent/route_baseline.json`（随代码提交；不含任何用户数据）

说明
----
· 全程 **不调 LLM 意图分类**（allow_intent_llm=False）——只跑确定性阶梯，故可离线、可复现；
· 少数用例会走到「知识库问答」而触发一次 LLM（首次无缓存），因此只钉路由结论；
· 与 `route_registry --selfcheck`（登记表自检）配合使用，见项目根 `路由自检.bat`。
"""
from __future__ import annotations

import json
import os
import sys

__all__ = ['CASES', 'snapshot', 'save', 'check']

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.dirname(_HERE)                 # 通用工具/
_ROOT = os.path.dirname(_TOOLS)                 # 项目根
for _p in (_TOOLS, os.path.join(_ROOT, '用户端特用')):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

BASELINE = os.path.join(_HERE, 'route_baseline.json')

# 用例来源：每条都对应 route_registry 里某条闸门/判据的**事故出处**或典型问法。
# 新增闸门时应同时补一条用例（route_registry.selfcheck 会检查 converge 类必须有退场条件）。
CASES = (
    # —— guard 红线 / 防幻觉 ——
    '原神怎么玩', '元神怎么玩', '帮我写一段同人故事', '下期会不会出新舰灵',
    # 2026-10-05 注册表自检发现的口子：『下期会出什么舰灵』此前不在 _NO_KB_FUTURE_RE 覆盖内
    # （未来内容无据，必须前置拦下，不能漏给自由生成）→ 补 `会出(什么|哪些|啥)` 后加此用例钉住。
    '下期会出什么舰灵',
    '调取RA-01档案', '你这个是编的吧', '知识库截止到什么时候', '怎么反馈',
    # —— 状态机 / 按钮语义 ——
    '结束本轮', '再见', '88礼包选谁', '计算工具', '活动',
    # —— 三条业务前置闸 ——
    '胡德有什么用', '女灶神值得练吗', '格奈森瑙天赋怎么点', '竞技场中船队伍推荐',
    # —— 输入归一（entity_norm）：4 字错字仍要纠（下限抬到 4 之后的有效性证明）——
    '艾塞克斯面板', '阿斯莫德怎么打',
    # —— 意图判据：计算 ——
    '胡德打前卫算伤害', '减伤25%和加伤害25%哪个收益高', '算了我信你吧',
    '算经验值', '索敌计算 1000', '好感度计算',
    # —— 意图判据：查询 / 筛选 ——
    '查胡德', '女灶神很强', '哪些舰灵有大型减伤天赋', '对比780和太太',
    '胡德改造厉害吗', '查胡德面板', '有哪些驱逐', '他们的必杀技CD是多少',
    # —— 意图判据：攻略 / 主题 ——
    '最近舰队油耗很大', '怎么练级', '装备速查', '当期活动怎么打', '剧情榜',
    '主线剧情讲了什么', '活动剧情什么时候结束',
    # —— 槽位解析（表单串）——
    '经验计算 等级：45 当前经验：3200 出战经验：120',
    '遭遇战模拟 boss：嘉波 白天：60 前排：A B 后排：D E',
    # —— 兜底 / 闲聊（应放行给闲聊）——
    '今天心情不错', '谢谢你',
)


def _norm(rec: dict) -> dict:
    """归一后再比对：KB-LLM 作答的措辞不参与比较（只有路由结论算契约）。"""
    d = dict(rec)
    if d.get('work_from_kb'):
        d.pop('work_reply', None)
    return d


_COMPARE_SKIP = ('_unstable',)


def _stable_records(cases=CASES, tools=None, runs: int = 3) -> list:
    """跑 `runs` 遍，**只保留各遍都一致的字段**（不稳定字段登记进 `_unstable`，不作判定）。

    目的：回归集必须可信。随机话术变体（`random.choice`）、KB-LLM 措辞、进程内缓存预热
    都会让个别字段漂移；跑 3 遍取交集，比"整份基线偶尔红一次"要诚实得多。
    """
    runs_data = [snapshot(cases, tools) for _ in range(max(2, int(runs)))]
    out = []
    for i in range(len(cases)):
        recs = [r[i] for r in runs_data]
        rec, unstable = {}, []
        for k in recs[0]:
            vals = [r.get(k) for r in recs]
            if all(v == vals[0] for v in vals):
                rec[k] = vals[0]
            else:
                unstable.append(k)
        if unstable:
            rec['_unstable'] = unstable
        out.append(rec)
    return out


def _same(base_rec: dict, now_rec: dict) -> bool:
    """只比基线里**钉住**的字段（`_unstable` 等元信息不参与判定）。"""
    for k, v in (base_rec or {}).items():
        if k in _COMPARE_SKIP:
            continue
        if now_rec.get(k) != v:
            return False
    return True


def _build_tools():
    import web_app as w
    from tools import build_tools
    return build_tools(w.agent.app, {}, location_ref=w.agent)


def snapshot(cases=CASES, tools=None):
    """跑一遍确定性阶梯，返回每条的「路由结论」。不传 tools 则自行构造（会加载 App）。"""
    from agent import guard as g
    from core import calc_flow as cf
    if tools is None:
        tools = _build_tools()
    rows = []
    for one in cases:
        out = {'input': one}
        # 输入归一（entity_norm）：**必须先钉住** —— 它先于一切路由执行，改坏了会让整条
        # 链路对不上（2026-10-05 事故：「胡德有什么用」被归一成「胡德·改什么用」）。
        try:
            from core.entity_norm import standardize as _std
            out['norm'] = _std(one)
        except Exception as e:
            out['norm'] = 'ERR:%s' % type(e).__name__
        try:
            handled, res = g.guard(tools, {}, one, agent=None, history=[])
            out['guard_handled'] = bool(handled)
            out['guard_reply'] = str((res or {}).get('reply') or '')[:36] if handled else ''
        except Exception as e:
            out['guard_handled'] = 'ERR:%s' % type(e).__name__
            out['guard_reply'] = ''
        pre = {}
        for name, fn in (('ship_use', cf._handle_ship_use), ('talent', cf._handle_talent_ask),
                         ('pvp', cf._handle_pvp_lineup)):
            try:
                pre[name] = bool(fn(tools, one))
            except Exception as e:
                pre[name] = 'ERR:%s' % type(e).__name__
        out['pregate'] = pre
        try:
            r = cf.route_work(tools, {}, one, agent=None, allow_intent_llm=False)
            out['work_none'] = r is None
            out['work_reply'] = str((r or {}).get('reply') or '')[:36] if r else ''
            out['work_from_kb'] = bool((r or {}).get('from_kb'))
        except Exception as e:
            out['work_none'] = 'ERR:%s' % type(e).__name__
            out['work_reply'] = ''
            out['work_from_kb'] = False
        rows.append(out)
    return rows


def save(update: bool = False) -> int:
    """记录基线。已有基线且非 --update 时**拒绝覆盖**（防止随手把回归抹掉）。"""
    if os.path.isfile(BASELINE) and not update:
        print('[route_cases] 基线已存在，未覆盖：%s（确认有意变更后用 --update）' % BASELINE)
        return 2
    recs = _stable_records()
    with open(BASELINE, 'w', encoding='utf-8') as f:
        json.dump(recs, f, ensure_ascii=False, indent=1)
    n_un = sum(1 for r in recs if r.get('_unstable'))
    print('[route_cases] 基线已写入 %s（%d 条用例，其中 %d 条含不稳定字段已标出）'
          % (BASELINE, len(recs), n_un))
    return 0


def check(verbose: bool = True) -> int:
    """与基线对比。返回 0=一致 / 1=有回归 / 2=缺基线。"""
    if not os.path.isfile(BASELINE):
        print('[route_cases] 缺少基线，请先 python -m agent.route_cases --save')
        return 2
    with open(BASELINE, encoding='utf-8') as f:
        base = {_r['input']: _norm(_r) for _r in json.load(f)}
    now = {_r['input']: _norm(_r) for _r in snapshot()}
    bad = [k for k in base if k in now and not _same(base[k], now[k])]
    extra = [k for k in now if k not in base]
    if verbose:
        for k in bad:
            print('[DIFF] %r' % k)
            print('   基线:', json.dumps(base[k], ensure_ascii=False))
            print('   现在:', json.dumps(now[k], ensure_ascii=False))
        for k in extra:
            print('[NEW ] 用例不在基线内：%r' % k)
    print('[route_cases] %d 条用例，不一致 %d 条 → %s'
          % (len(base), len(bad), 'PASS' if not bad else 'FAIL'))
    return 0 if not bad else 1


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--list' in args:
        for i, c in enumerate(CASES):
            print('%2d  %s' % (i + 1, c))
        sys.exit(0)
    if '--save' in args:
        sys.exit(save(update=('--update' in args)))
    sys.exit(check())
