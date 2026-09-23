# -*- coding: utf-8 -*-
"""encounter tools."""
from qwen_agent.tools.base import BaseTool
from core.encounter_simulator import simulate_encounter, simulate_pvp
from .common import _validate_course_air

# ======================================================================
# 工具 13：遭遇战模拟（6角色组队时间轴伤害寻优）
# ======================================================================

class EncounterSimulator(BaseTool):
    """【遭遇战】6角色组队时间轴伤害模拟。输入 boss 名（知识库/拉古兹.json）、白天/黑夜时间（合计150s）、
    前排3+后排3角色名，按 1s 粒度动态规划求解理论最高伤害的操作时间轴（交换时机、必杀技释放时机）。
    规则：前3为前排（普攻/通常技/必杀技），后3为后排（仅支援技）；0-5s 禁止交换，之后每次交换 CD=10s；
    黑夜 boss 装甲减 90%，航母/轻母/装母黑夜禁用；必杀技仅前排可放，时机由 DP 寻优。
    可选 flagship_name：6 人中选 1 个作为旗舰，其旗舰技光环对全队生效（增益不论前后排，取决于条件与时间）。"""
    name = 'encounter_simulator'
    description = '【遭遇战】6角色组队时间轴伤害模拟。输入 boss 名、白天时间（黑夜自动=150-白天）、前排3+后排3角色名（可选旗舰1名），输出理论最高伤害的操作时间轴与伤害明细。'
    parameters = {
        'type': 'object',
        'properties': {
            'boss_name': {'type': 'string', 'description': 'boss 名（即 知识库/拉古兹.json 中的名字，自动识别）'},
            'day_time': {'type': 'number', 'description': '白天时间（秒，0~150）。黑夜时间自动按 150-白天 推导，无需输入'},
            'front_ships': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '前排 3 个角色名（可释放普攻/通常技/必杀技）',
            },
            'back_ships': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '后排 3 个角色名（仅支援技）',
            },
            'flagship_name': {
                'type': 'string',
                'description': '旗舰角色名（6 人中选 1 个，可空）：其旗舰技光环对全队生效，增益不论前后排都生效，只取决于增益条件与时间',
                'default': '',
            },
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': 'T优'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '确保'},
        },
        'required': ['boss_name', 'day_time', 'front_ships', 'back_ships'],
    }

    def call(self, params, **kwargs):
        app = self.cfg['app']
        params = self._verify_json_format_args(params)
        boss_name = params['boss_name']
        day_time = float(params['day_time'])
        # 黑夜时间自动推导 = 150 - 白天，忽略/不再要求用户输入的黑夜
        night_time = 150.0 - day_time
        front_ships = params['front_ships']
        back_ships = params['back_ships']
        course = params.get('course', 'T优')
        air = params.get('air', '确保')
        flagship_name = params.get('flagship_name', '') or ''
        # 航向键归一化：前端/默认用 "T优"/"T劣"，常量表用 "T优势"/"T劣势"
        course = {"T优": "T优势", "T劣": "T劣势"}.get(course, course)
        err = _validate_course_air(course, air)
        if err:
            return err
        # simulate_encounter 返回 {text, detail}；工具层只回传 text（详细计算模块由route_work折叠展示）
        res = simulate_encounter(app, boss_name, day_time, night_time,
                                 front_ships, back_ships, course, air, flagship_name)
        return res['text'] if isinstance(res, dict) else res


# ======================================================================
# 工具 16：模拟PVP（6人组队打防守角色，无buff，定时总伤害）
# ======================================================================

class PvpSimulator(BaseTool):
    """【组队-模拟PVP】6 人组队（不分前后排、不算支援技，必填 1 名旗舰）打 1 个防守角色，
    无需 buff 输入，按各自 CD 自动循环，计算一定时间内的全队总伤害。
    战斗时长总 120s：白天由用户输入（0~120），黑夜 = 120 − 白天（自动推导）。"""
    name = 'simulate_pvp'
    description = '【模拟PVP】6人组队打防守角色。输入防守角色名、白天战斗时长（秒，0~120，黑夜自动 = 120 - 白天）、6个角色名（不分前后排、不算支援技，普攻/通常技/必杀技）、旗舰1名（必填），输出各角色伤害与全队总伤害（无buff输入、无拉古兹）。'
    parameters = {
        'type': 'object',
        'properties': {
            'defender_name': {'type': 'string', 'description': '防守角色名（已有角色）'},
            'day_time': {'type': 'number', 'description': '白天战斗时长（秒，0~120）。黑夜自动按 120-白天 推导，无需输入', 'default': 60},
            'front_ships': {
                'type': 'array', 'items': {'type': 'string'},
                'description': '前排 3 个角色名（普攻/通常技/必杀技）',
            },
            'back_ships': {
                'type': 'array', 'items': {'type': 'string'},
                'description': '后排 3 个角色名（仅支援技）',
            },
            'flagship_name': {
                'type': 'string',
                'description': '旗舰角色名（6 人中选 1 个，可空）：其旗舰技光环对全队生效',
                'default': '',
            },
            'course': {'type': 'string', 'description': '航向：同航/反航/T优势/T劣势', 'default': '同航'},
            'air': {'type': 'string', 'description': '制空状态：确保/优势/均势/劣势/丧失', 'default': '均势'},
        },
        'required': ['defender_name', 'front_ships', 'back_ships'],
    }

    def call(self, params, **kwargs):
        app = self.cfg['app']
        params = self._verify_json_format_args(params)
        err = _validate_course_air(params.get('course', '同航'), params.get('air', '均势'))
        if err:
            return err
        # simulate_pvp 返回 {text, detail}；工具层只回传 text（详细计算模块由 route_work 折叠展示）
        day_time = float(params.get('day_time', 60) or 60)
        res = simulate_pvp(app,
                           params['defender_name'],
                           day_time,
                           params['front_ships'],
                           params['back_ships'],
                           params.get('flagship_name', '') or '',
                           params.get('course', '同航'),
                           params.get('air', '均势'),
                           night_time=120.0 - day_time)
        return res['text'] if isinstance(res, dict) else res
