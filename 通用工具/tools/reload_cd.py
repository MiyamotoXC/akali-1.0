# -*- coding: utf-8 -*-
"""
装填对普攻 CD 影响计算器（确定性公式，独立参考工具）

公式（用户口径）：
- 普攻CD缩减 = 装填值 × 普攻基础CD / 60 （%），缩减上限 25%
- 缩减后普攻CD = 普攻基础CD × (1 - 缩减% / 100)

口径约定：
- 仅作普攻节奏参考；伤害计算链路（calc.py / damage_formula / formula_engine）
  一律不接入该缩减，工具输出固定附带"通常对普攻输出影响不大"提示
"""
from qwen_agent.tools.base import BaseTool

# 缩减上限（%）
_RELOAD_CD_CAP = 25.0


def calc_reload_cd(reload_value: float, base_cd: float):
    """按公式计算普攻 CD 缩减，返回 (缩减%, 缩减后CD秒)。"""
    cap = min(reload_value * base_cd / 60.0, _RELOAD_CD_CAP)
    return cap, base_cd * (1 - cap / 100.0)


class CalcReloadCd(BaseTool):
    """【装填-普攻CD计算】按公式 普攻CD缩减=装填值×普攻基础CD/60%（上限25%），
    计算装填属性对普攻 CD 的缩减。提供角色名时自动读取该角色装填值与普攻段 CD；
    也可直接手动给装填值与普攻 CD 计算。仅作节奏参考，伤害计算不考虑该缩减。"""
    name = 'calc_reload_cd'
    description = ('【装填-普攻CD计算】计算装填属性对普攻 CD 的缩减：'
                   '普攻CD缩减 = 装填值 × 普攻基础CD / 60（%），缩减上限 25%。'
                   '给角色名（ship_name）则自动读其装填值与普攻段 CD；'
                   '也可手动给 reload_value（装填值）与 normal_cd（普攻基础CD，秒）直接算。'
                   '注意：该缩减仅影响普攻节奏，伤害计算不考虑，通常对普攻输出影响不大。')
    parameters = {
        'type': 'object',
        'properties': {
            'ship_name': {'type': 'string', 'description': '角色名（可空；提供时自动读取装填值与普攻段 CD）'},
            'reload_value': {'type': 'number', 'description': '装填值（可空；提供时优先使用，不再读角色面板）'},
            'normal_cd': {'type': 'number', 'description': '普攻基础CD，秒（可空；提供时优先使用，不再读角色普攻段）'},
        },
        'required': [],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        app = self.cfg['app']
        ship_name = (params.get('ship_name') or '').strip()

        # ---- 手动参数优先，其次角色数据 ----
        reload_value = params.get('reload_value')
        normal_cd = params.get('normal_cd')
        try:
            reload_value = float(reload_value) if reload_value not in (None, '') else None
        except (ValueError, TypeError):
            reload_value = None
        try:
            normal_cd = float(normal_cd) if normal_cd not in (None, '') else None
        except (ValueError, TypeError):
            normal_cd = None

        manual_reload = reload_value is not None
        manual_cd = normal_cd is not None
        cds = [normal_cd] if manual_cd else []
        reload_note = ''

        # 角色数据补缺（昵称匹配与用户副本口径同 QueryShipInfo / 计算工具）
        ship = None
        if ship_name:
            ship = app.get_ship_for_calc(ship_name)
            # 信息不全（缺面板/技能/普攻/天赋任一项）等同"没有该角色"，与未找到同口径
            if ship is None or not app.is_ship_complete(ship_name):
                return f"未找到角色: {ship_name}。"
        if ship is not None:
            if not manual_reload:
                try:
                    reload_value = float(ship.attack_panel.get('装填', 0) or 0)
                except (ValueError, TypeError):
                    reload_value = 0.0
                reload_note = f"（取自 {ship.name} 面板）"
            if not manual_cd:
                seen = []
                for seg in (ship.normal_attacks or []):
                    try:
                        cd = float(seg.get('cd', 0) or 0)
                    except (ValueError, TypeError):
                        continue
                    if cd > 0 and cd not in seen:
                        seen.append(cd)
                cds = seen

        if reload_value is None:
            return ("请提供角色名（自动读取装填值与普攻 CD），或直接给装填值与普攻基础CD。"
                    "例如「前卫 装填对普攻CD的影响」或「装填900 CD5秒 普攻缩减多少」。")
        if not cds:
            return ("缺少普攻基础CD：该角色未配置普攻段，或未提供 normal_cd 参数。"
                    "可直接给普攻基础CD（秒）重算。")

        # ---- 逐段计算输出 ----
        head = "装填对普攻CD影响" + (f"（{ship.name}）" if ship is not None else "")
        lines = [head, f"装填值：{reload_value:g}{reload_note}（实战影响不大）", ""]
        for cd in sorted(cds):
            cap, new_cd = calc_reload_cd(reload_value, cd)
            cap_note = "（已达 25% 上限）" if cap >= _RELOAD_CD_CAP - 1e-9 else ""
            lines.append(f"- 普攻基础CD {cd:g} 秒 → 缩减 {cap:.2f}%{cap_note} → 缩减后 CD {new_cd:.3f} 秒")
        return "\n".join(lines)
