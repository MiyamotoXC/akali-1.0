# -*- coding: utf-8 -*-
"""
好感度计算器（确定性公式，数据源：拂晓好感度汇总表 + 社区研究交叉验证）

核心公式（对表全行验证吻合）：
- 好感度为"每级独立池"模型：每级从 0 攒满该级需求值后升入下一级；游戏内只显示级别 + 该级内进度百分比
- 级别池（点）：陌生5 相识10 亲近25 喜欢50 [爱:已满] 誓约60 依偎70 守护80 至爱90 铭心100 [永恒:MAX]
  交叉验证（备注原文，旗舰 0.15 点/场、30 油/场；婚=誓约）：
  陌生—爱 600 场 = 0.15 x (5+10+25+50)；婚—至爱 1400 场 = 0.15 x (60+70+80)；至—永恒 1267 场 = 0.15 x (90+100)
  每级需场数 = 池/每场（33/67/167/333/400/467/533/600/667）；
  每场百分比 = 每场点/该级池（3%/1.5%/0.6%/0.3%/0.25%/0.21%/0.19%/0.17%/0.15%）
- 特殊级别（社区研究确认，TapTap《好感度的研究》）：
  爱：好感已满，出击/秘书舰/答题/礼物不再增长，需誓约（结婚）后升级为"誓约"
  永恒：最终阶段，铭心攒满 100 后自动进入，无后续需求
- 到目标还需点数 = 当前级剩余池 x (1 - 进度%) + 途经各级池（"到X级别"=进入X，不含X自身池；
  表注验证：婚—至爱 1400 场 = 0.15 x (60+70+80)，不含至爱池；唯一例外"到永恒"=攒满铭心才进入，含铭心池）
- 每场点数：3星 旗舰0.15/非旗舰0.05；2星 旗舰0.10/非旗舰0.01；1星 旗舰0.10/非旗舰0
- 3倍好感活动：每场点数 x3
- 只挂秘书舰：每天 +2.8 点；答题：每次 +2 点；沉没不涨好感
- 礼物按阶段固定品质对应：白(陌生)/绿(相识)/蓝(亲近)/紫(喜欢)/金(誓约起)；每件点数：白1 绿2 蓝3 紫4 金5
"""
import math

from qwen_agent.tools.base import BaseTool

# 阶段（名称, 该级需求池点数, 攻击加成%, 减伤%, 备注）；
# 池=0 表示特殊级别：爱（已满待结婚）、永恒（最终阶段）；
# 攻%：誓约起 15/16/17/18/19/20 为表头原文，前段 3/6/9 按 陌生0/相识3/亲近6 递推（原表列头模糊，推断）；
# 减%：与攻击加成同值（攻略7.1 实证：永恒好感提供 20% 增伤和 20% 减伤）
_STAGES = [
    ("陌生", 5, 0, 0, ""),
    ("相识", 10, 3, 3, ""),
    ("亲近", 25, 6, 6, ""),
    ("喜欢", 50, 9, 9, "开天赋3"),
    ("爱", 0, 9, 9, "已满：待誓约(结婚)后升级"),
    ("誓约", 60, 15, 15, "开启槽1"),
    ("依偎", 70, 16, 16, "开启槽2"),
    ("守护", 80, 17, 17, "开启槽3"),
    ("至爱", 90, 18, 18, "开启雷达槽"),
    ("铭心", 100, 19, 19, ""),
    ("永恒", 0, 20, 20, "最终阶段：铭心攒满即进入"),
]
_STAGE_INDEX = {name: i for i, (name, *_) in enumerate(_STAGES)}
# 级别口语同义词 → 标准名（用户可能说"熟悉"指"相识"）
_STAGE_ALIASES = {'熟悉': '相识'}


def _norm_stage(name: str) -> str:
    """级别名归一化（同义词映射 + 去空白）。"""
    name = (name or '').strip()
    return _STAGE_ALIASES.get(name, name)

# 每场好感度点数（星级 → 出击位置）
_PER_BATTLE = {
    3: {"flagship": 0.15, "non_flagship": 0.05},
    2: {"flagship": 0.10, "non_flagship": 0.01},
    1: {"flagship": 0.10, "non_flagship": 0.00},
}
_SECRETARY_PER_DAY = 2.8   # 只挂秘书舰每天好感度
_ANSWER_PER_TIME = 2       # 答题每次好感度
_GIFT_COLORS = {'白': 1, '绿': 2, '蓝': 3, '紫': 4, '金': 5}   # 礼物品质 → 每件好感点数
# 每阶段固定对应品质礼物：白色只对陌生、绿色只对相识、蓝色只对亲近、紫色只对喜欢，
# 誓约及之后只能用金色（"爱"需结婚无礼物、"永恒"为最终阶段）
_STAGE_GIFT = {'陌生': '白', '相识': '绿', '亲近': '蓝', '喜欢': '紫',
               '誓约': '金', '依偎': '金', '守护': '金', '至爱': '金', '铭心': '金'}

_POSITION_LABEL = {"flagship": "旗舰位", "non_flagship": "非旗舰位"}


def _ceil(x: float) -> int:
    """向上取整（带浮点容差）。"""
    return int(math.ceil(x - 1e-9))


def _fmt_num(x: float) -> str:
    """点数展示：统一保留两位小数（好感度点数口径）。"""
    return f"{x:.2f}"


class CalcFavor(BaseTool):
    """【好感度计算】按好感度汇总表公式，计算从当前好感度到目标阶段还需要多少出击场次/秘书舰天数/答题次数/礼物。
    当用户询问好感度进度、还需要多少礼物或出击时调用。"""
    name = 'calc_favor'
    description = ('【好感度计算】计算从当前好感度到目标阶段还需要的出击场数（含3倍活动）、'
                   '只挂秘书舰天数、答题次数、礼物件数。'
                   '前提：必须同时提供 current_stage（当前级别名）与 current_percent（该级别内进度百分比），'
                   '缺任一个不计算；target_stage 为目标级别（默认下一阶段）；'
                   'position（flagship旗舰位/non_flagship非旗舰位，默认flagship）；'
                   'stars（星级1/2/3，默认3）；'
                   '礼物：给 gift_color（品质 白/绿/蓝/紫/金）或 gift_points（每件点数）即可，'
                   'gift_mode=gold 时全用金色礼物计算件数，缺省按各阶段固定对应品质（梯度）计算。')
    parameters = {
        'type': 'object',
        'properties': {
            'ship_name': {'type': 'string', 'description': '舰灵名（可空，仅展示用）'},
            'current_stage': {'type': 'string',
                              'description': '当前级别名（必填）：陌生/相识/亲近/喜欢/爱/誓约/依偎/守护/至爱/铭心/永恒'},
            'current_percent': {'type': 'number', 'description': '该级别内的进度百分比（0-100，缺省按 0 算）'},
            'target_stage': {'type': 'string', 'description': '目标级别名（默认下一阶段）'},
            'target_percent': {'type': 'number', 'description': '目标级别内的进度百分比（如「到誓约50%」给 50，可空）'},
            'position': {'type': 'string', 'description': '出击位置：flagship（旗舰位，默认）/ non_flagship（非旗舰位）'},
            'stars': {'type': 'integer', 'description': '舰灵星级：1/2/3（默认3）'},
            'gift_color': {'type': 'string', 'description': '礼物品质：白/绿/蓝/紫/金（仅表示要算礼物），可空'},
            'gift_points': {'type': 'number', 'description': '每件礼物提供的好感点数（可空；与 gift_color 二选一，仅表示要算礼物）'},
            'gift_mode': {'type': 'string', 'description': '礼物模式：gold=全用金色礼物计算件数；缺省=按各阶段固定对应品质（梯度）计算'},
        },
        'required': [],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        ship_name = (params.get('ship_name') or '').strip()

        # ---- 当前好感度：级别（阶段名）+ 该级别内进度百分比（百分比缺省按 0% 算）----
        stage = _norm_stage(params.get('current_stage'))
        pct = params.get('current_percent')
        if not stage:
            stage_list = ' / '.join(s[0] for s in _STAGES)
            return ("请说清楚当前好感度级别。"
                    f"可选级别：{stage_list}。"
                    "例如「现在喜欢，进度30%」（不说进度按 0% 算）；目标可选（如「到誓约」，默认算到下一阶段，"
                    "也可带目标进度如「到誓约50%」）；算礼物的话再说明品质（白/绿/蓝/紫/金，每件1/2/3/4/5点）。")
        if stage not in _STAGE_INDEX:
            return f"未知级别「{stage}」。可用级别：{'/'.join(s[0] for s in _STAGES)}。"
        try:
            pct = float(str(pct).strip().rstrip('%')) if pct not in (None, '') else 0.0
        except (ValueError, TypeError):
            return "进度百分比格式无法识别，请给数字（如「30%」）。"
        if not (0.0 <= pct <= 100.0):
            return f"进度百分比应在 0-100 之间，收到 {pct:g}%，请核对后重试。"
        cur_stage_idx = _STAGE_INDEX[stage]

        # ---- 特殊级别：爱（已满待结婚）→ 提示先誓约，婚后按誓约0%继续计算 ----
        love_switched = False
        if stage == '爱':
            t_probe = _norm_stage(params.get('target_stage'))
            if t_probe and t_probe in _STAGE_INDEX and _STAGE_INDEX[t_probe] <= _STAGE_INDEX['誓约']:
                if t_probe == '誓约':
                    return ("「爱」阶段的好感度已经满了，到「誓约」需要结婚（使用誓约道具），"
                            "婚后即进入「誓约」0%（升级还需 60 点）。")
                return (f"「爱」阶段的好感度已经满了，且目标「{t_probe}」不高于「誓约」："
                        "结婚（使用誓约道具）后即已达标。")
            stage = '誓约'
            cur_stage_idx = _STAGE_INDEX['誓约']
            pct = 0.0
            love_switched = True
            cur_from = "「爱」已满，需先誓约（结婚）；婚后从 誓约（进度 0%）"
        elif stage == '永恒':
            return "「永恒」是最终好感阶段，已经满好感，没有后续需求了。"
        else:
            cur_from = f"从 {stage}（进度 {pct:g}%）"

        cur_pool = _STAGES[cur_stage_idx][1]

        # ---- 目标级别：默认下一级别（"爱"转换后用户视角的下一级=誓约本身）；特殊级别（爱/永恒）回退做部分计算 ----
        t_stage = (params.get('target_stage') or '').strip()
        partial_note = ''
        if t_stage:
            if t_stage not in _STAGE_INDEX:
                return f"未知级别「{t_stage}」。可用级别：{'/'.join(s[0] for s in _STAGES)}。"
            t_idx = _STAGE_INDEX[t_stage]
            if t_idx <= cur_stage_idx:
                return f"{cur_from}，目标 {t_stage}：已处于该级别或更高，无需再刷。"
        elif love_switched:
            t_idx = _STAGE_INDEX['誓约']   # 用户视角"下一阶段"=誓约（婚后从0%攒60点）
        else:
            t_idx = cur_stage_idx + 1 if cur_stage_idx + 1 < len(_STAGES) else None
            if t_idx is None:
                return f"{cur_from}：已是表内最后级别，无下一级可算。"
        # 目标为特殊级别（爱：需结婚；永恒：最终阶段）→ 回退到最后有池级别，算"进入该级别前"的部分；
        # 显示名保留用户请求的特殊级（爱/永恒），避免回退成前置级（如 爱→喜欢）让目标显示错乱
        to_eternal = False
        if _STAGES[t_idx][1] <= 0:
            req_target_name = _STAGES[t_idx][0]
            j = t_idx - 1
            while j >= 0 and _STAGES[j][1] <= 0:
                j -= 1
            if j < cur_stage_idx:
                return (f"{cur_from}，目标 {req_target_name}：当前级别之后暂无可刷的好感需求。")
            if req_target_name == '爱':
                partial_note = f"攒满「{_STAGES[j][0]}」即进入「爱」（好感已满，到「誓约」还需结婚）"
            else:
                partial_note = f"攒满「{_STAGES[j][0]}」即进入「{req_target_name}」（最终阶段）"
                to_eternal = True   # "到永恒"=攒满铭心才进入，需含铭心池
            t_idx = j
            tgt_name = req_target_name
        else:
            tgt_name = _STAGES[t_idx][0]
        tgt_note = _stage_note(t_idx)
        if partial_note:
            tgt_note = (f"{tgt_note}；" if tgt_note else "") + partial_note
        # 目标级别内进度（"到誓约50%"=攒到誓约50%，计入目标级池对应部分）；"到永恒"必须攒满铭心，目标进度无意义
        tp = params.get('target_percent')
        try:
            tp = float(str(tp).strip().rstrip('%')) if tp not in (None, '') else 0.0
        except (ValueError, TypeError):
            tp = 0.0
        if not (0.0 <= tp <= 100.0):
            return f"目标进度百分比应在 0-100 之间，收到 {tp:g}%，请核对后重试。"
        tgt_extra = 0.0
        if tp > 0 and not to_eternal and _STAGES[t_idx][1] > 0:
            tgt_extra = round(_STAGES[t_idx][1] * tp / 100.0, 2)
            tgt_name = f"{tgt_name}（进度 {tp:g}%）"

        # ---- 剩余点数：当前级剩余 + 途经各级池（每级独立池模型）----
        # "到X级别"=进入X（X进度0%），不含X级别自身池；唯一例外"到永恒"=攒满铭心才进入（to_eternal 含铭心池）
        # 途经"爱"（好感已满）：可刷部分止于爱之前，誓约起的池需结婚后才能继续，单独列出
        remain = cur_pool * (100.0 - pct) / 100.0
        cur_left = round(remain, 2)
        segs = []
        after_marriage = []
        pairs = []          # 途经级别（名称, 池, 是否婚后），礼物按级别对应品质输出用
        crossed_love = False
        for i in range(cur_stage_idx + 1, t_idx + 1 if to_eternal else t_idx):
            nm, v = _STAGES[i][0], _STAGES[i][1]
            if nm == '爱':
                crossed_love = True
                continue
            remain += v
            pairs.append((nm, v, crossed_love))
            if crossed_love:
                after_marriage.append(f"{nm} {_fmt_num(v)}")
            else:
                segs.append(f"{nm} {_fmt_num(v)}")
        # 目标级池的部分进度（"到誓约50%"）：计入总量与途经显示，婚后归属随目标级别
        if tgt_extra > 0:
            remain += tgt_extra
            pairs.append((tgt_name, tgt_extra, crossed_love))
            if crossed_love:
                after_marriage.append(f"{tgt_name} {_fmt_num(tgt_extra)}")
            else:
                segs.append(f"{tgt_name} {_fmt_num(tgt_extra)}")
        remain = round(remain, 2)
        if remain <= 1e-9:
            if crossed_love:
                return (f"{cur_from}：好感已攒满并进入「爱」（好感已满不再增长）；"
                        f"到「{tgt_name}」需要结婚（誓约道具），"
                        f"婚后继续累计：{' / '.join(after_marriage)}。")
            return (f"{cur_from}，目标 {tgt_name}：已达标，无需再刷。"
                    + (f"（{tgt_note}）" if tgt_note else ""))

        # ---- 出击参数 ----
        try:
            stars = int(params.get('stars') or 3)
        except (ValueError, TypeError):
            stars = 3
        if stars not in _PER_BATTLE:
            stars = 3
        position = (params.get('position') or 'flagship').strip()
        if position not in ('flagship', 'non_flagship'):
            position = 'flagship'
        per = _PER_BATTLE[stars][position]

        # 礼物：仅表示"要算礼物"（品质/点数任意）；gift_points<0 为路由占位标记；
        # gift_mode=gold 时全用金色礼物计算件数，缺省按各阶段固定对应品质（梯度）
        gift_points = params.get('gift_points')
        gift_color = (params.get('gift_color') or '').strip()
        gift_mode = (params.get('gift_mode') or '').strip()
        try:
            gift_points = float(gift_points) if gift_points not in (None, '', 0) else 0.0
        except (ValueError, TypeError):
            gift_points = 0.0
        if gift_points < 0:
            gift_points = 1.0
        if gift_points <= 0 and gift_color and gift_color[0] in _GIFT_COLORS:
            gift_points = float(_GIFT_COLORS[gift_color[0]])
        gift_points = min(gift_points, 20000.0)   # 用户输入上限 20000（用户口径 2026-09-17）

        # ---- 输出 ----
        head = f"好感度计算：{ship_name + ' ' if ship_name else ''}{cur_from}，目标 {tgt_name}"
        if tgt_note:
            head += f"（{tgt_note}）"
        lines = [head, f"剩余好感度：{_fmt_num(remain)} 点"
                 + (f"（含当前级剩余 {_fmt_num(cur_left)} 点）" if pct > 0 else "")]

        # 攻击加成变化（当前级别 → 目标级别）；"到永恒"取永恒的加成（20%）而非回退后的铭心
        tgt_bonus_idx = _STAGE_INDEX['永恒'] if to_eternal else t_idx
        cur_bonus, tgt_bonus = _stage_bonus(cur_stage_idx), _stage_bonus(tgt_bonus_idx)
        if cur_bonus is not None and tgt_bonus is not None and tgt_bonus != cur_bonus:
            lines.append(f"攻击加成：{cur_bonus}% → {tgt_bonus}%")
            # 减伤与攻击加成同表同值，同步写出
            lines.append(f"减伤：{cur_bonus}% → {tgt_bonus}%")

        lines.append("")
        # 跨"爱"需分段计算：每级独立池，升入"爱"最后一场/最后一天的点数溢出浪费，
        # 结婚后从誓约 0% 重新攒，两段各自向上取整（合并 ceil 会漏算溢出差一场）
        # 婚前段 = 当前级剩余 + 全部婚前途经级池（相识/亲近/喜欢…），婚后段 = 誓约起的池
        split_love = crossed_love and cur_left > 0 and cur_stage_idx < _STAGE_INDEX['爱']
        pre_left = round(cur_left + sum(v for _nm, v, m in pairs if not m), 2)
        marry_left = round(remain - pre_left, 2) if split_love else 0.0
        lines.append(f"【出击】{stars}星·{_POSITION_LABEL[position]} 每场 {per:g} 点（沉没不涨好感）")
        if per <= 0:
            lines.append("- 出击无法获得好感（1星非旗舰每场 0 点），请改用旗舰位/秘书舰/礼物。")
        elif split_love:
            pre_b, post_b = _ceil(pre_left / per), _ceil(marry_left / per)
            overflow = round(per * pre_b - pre_left, 2)
            lines.append(f"- 结婚前（刷满「{stage}」升入「爱」）：{pre_b} 场"
                         + (f"（最后 1 场满级溢出 {overflow:g} 点，好感已满不结转）" if overflow > 1e-9 else ""))
            lines.append(f"- 结婚后（誓约 0% 起）：{post_b} 场")
            lines.append(f"- 合计：{pre_b + post_b} 场")
            pre_3, post_3 = _ceil(pre_left / (per * 3)), _ceil(marry_left / (per * 3))
            lines.append(f"- 三倍出击：婚前 {pre_3} 场 + 婚后 {post_3} 场 = {pre_3 + post_3} 场")
        else:
            battles = _ceil(remain / per)
            x3 = _ceil(remain / (per * 3))
            lines.append(f"- 出击场数：{battles} 场")
            lines.append(f"- 三倍出击：{x3} 场")
        lines.append("")
        lines.append("【其它途径】")
        if split_love:
            pre_d, post_d = _ceil(pre_left / _SECRETARY_PER_DAY), _ceil(marry_left / _SECRETARY_PER_DAY)
            lines.append(f"- 只挂秘书舰：结婚前 {pre_d} 天 + 结婚后 {post_d} 天 = {pre_d + post_d} 天"
                         f"（每天 {_SECRETARY_PER_DAY:g} 点）")
            pre_a, post_a = _ceil(pre_left / _ANSWER_PER_TIME), _ceil(marry_left / _ANSWER_PER_TIME)
            lines.append(f"- 答题：结婚前 {pre_a} 次 + 结婚后 {post_a} 次 = {pre_a + post_a} 次（每次 +{_ANSWER_PER_TIME} 点）")
        else:
            lines.append(f"- 只挂秘书舰：{_ceil(remain / _SECRETARY_PER_DAY)} 天（每天 {_SECRETARY_PER_DAY:g} 点）")
            lines.append(f"- 答题：{_ceil(remain / _ANSWER_PER_TIME)} 次（每次 +{_ANSWER_PER_TIME} 点）")
        if gift_points > 0:
            # 礼物两种模式：梯度（每阶段固定对应品质：白只对陌生、绿只对相识、蓝只对亲近、
            # 紫只对喜欢、誓约起只能金色）或全用金色（gold）
            gold_only = gift_mode == 'gold'
            if cur_left > 0 and (stage in _STAGE_GIFT or gold_only):
                gc = '金' if gold_only else _STAGE_GIFT[stage]
                lines.append(f"- {gc}礼物（{stage}剩余 {_fmt_num(cur_left)} 点）："
                             f"{_ceil(cur_left / _GIFT_COLORS[gc])} 件（每件 {_GIFT_COLORS[gc]} 点）")
            for nm, v, married in pairs:
                gc = '金' if gold_only else _STAGE_GIFT.get(nm, '金')
                tag = "，婚后" if married else ""
                lines.append(f"- {gc}礼物（{nm}{tag} {_fmt_num(v)} 点）："
                             f"{_ceil(v / _GIFT_COLORS[gc])} 件（每件 {_GIFT_COLORS[gc]} 点）")

        # 途经级别各池（每级独立池：攒满该级数值才升下一级）
        if segs:
            lines.append("")
            lines.append(f"途经级别需求池：{stage} {_fmt_num(cur_pool)} / {' / '.join(segs)}")

        # 途经"爱"：好感已满，需结婚后才能继续累计后续级别
        if crossed_love:
            if after_marriage:
                lines.append(f"「爱」阶段好感已满不再增长，需誓约（结婚）后继续：{' / '.join(after_marriage)}")
            else:
                lines.append("「爱」阶段好感已满不再增长，需誓约（结婚）后即进入「誓约」。")

        return "\n".join(lines)


def _stage_note(idx: int) -> str:
    note = _STAGES[idx][4]
    return note


def _stage_bonus(idx: int):
    if idx < 0:
        return None
    return _STAGES[idx][2]
