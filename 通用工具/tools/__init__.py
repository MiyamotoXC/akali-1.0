# -*- coding: utf-8 -*-
"""tools package."""
from .query import QueryShipInfo, QueryShipCopy, QueryScoutSum, SaveUserShip, ListUserShips, RemoveUserShip, QueryTalentShips
from .knowledge import SkillQuery, ShipFilter
from .calc import (CalcNormalAttack, CalcSkillAttack, CalcBurstAttack, CalcSupportAttack,
                   CalcBurnDamage, CalcTotalDamage)
from .session import SetSessionBuff, SetCalcContext
from .talent import SetSessionTalent
from .encounter import EncounterSimulator, PvpSimulator
from .knowledge import QueryEquipment, QueryGuide, QueryWeather, KbSearch, QueryActivity, GuideRAG, PvpRoleRecommend, AmmoMechQA
from .favor import CalcFavor
from .reload_cd import CalcReloadCd
from .table_io import ExcelAnalyzer


def build_tools(app, session_overrides: dict, location_ref=None):
    """
    构造用户端工具列表（只读 + 伤害计算 + 会话级临时记忆 + 天气）。
    session_overrides: 会话级临时覆写字典（buff/面板/敌方），由调用方维护，计算工具读取叠加。
    location_ref: 持有 user_location 的对象（AgentApp），供 query_weather 读取用户定位。
    """
    cfg_compute = {'app': app, 'session_overrides': session_overrides}
    cfg_query = {'app': app}
    if location_ref is not None:
        cfg_query['location_ref'] = location_ref
    cfg_session = {'session_overrides': session_overrides}

    return [
        QueryShipInfo(cfg=cfg_query),
        QueryTalentShips(cfg=cfg_query),
        QueryScoutSum(cfg=cfg_query),
        SkillQuery(cfg=cfg_query),
        ShipFilter(cfg=cfg_query),
        SaveUserShip(cfg=cfg_query),
        ListUserShips(cfg=cfg_query),
        RemoveUserShip(cfg=cfg_query),
        QueryShipCopy(cfg=cfg_query),
        CalcNormalAttack(cfg=cfg_compute),
        CalcSkillAttack(cfg=cfg_compute),
        CalcBurstAttack(cfg=cfg_compute),
        CalcSupportAttack(cfg=cfg_compute),
        CalcBurnDamage(cfg=cfg_compute),
        CalcTotalDamage(cfg=cfg_compute),
        SetSessionBuff(cfg=cfg_session),
        SetCalcContext(cfg=cfg_compute),
        SetSessionTalent(cfg=cfg_compute),
        EncounterSimulator(cfg=cfg_compute),
        PvpSimulator(cfg=cfg_compute),
        # QueryEquipment 停用（2026-09-10 用户指示）：具体装备查询改为给「常用装备速览」表
        #（calc_flow._common_equip_table_reply），配装走向量检索。保留类定义，随时可恢复。
        QueryGuide(cfg=cfg_query),
        QueryActivity(cfg=cfg_query),
        CalcFavor(cfg=cfg_query),
        CalcReloadCd(cfg=cfg_query),
        ExcelAnalyzer(cfg=cfg_query),
        KbSearch(cfg=cfg_query),
        GuideRAG(cfg=cfg_query),
        PvpRoleRecommend(cfg=cfg_query),
        AmmoMechQA(cfg=cfg_query),
        QueryWeather(cfg=cfg_query),
    ]
