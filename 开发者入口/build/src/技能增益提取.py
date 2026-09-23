# -*- coding: utf-8 -*-
"""
技能持续 buff 提取器 - 薄封装

本模块从通用工具加载 skill_buff_extractor，保持向后兼容。
所有实际逻辑位于 通用工具/core/skill_buff_extractor.py
"""
import os
import sys
import importlib.util

# 计算项目根目录
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_COMMON_TOOLS_DIR = os.path.join(_PROJECT_ROOT, "通用工具", "core")

# 通过 importlib 加载通用工具中的 skill_buff_extractor
_spec = importlib.util.spec_from_file_location(
    "common_skill_buff_extractor",
    os.path.join(_COMMON_TOOLS_DIR, "skill_buff_extractor.py")
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

# 导出所有公共接口，保持向后兼容
TRIGGER_MAP = _module.TRIGGER_MAP
_SKIP_KEYWORDS = _module._SKIP_KEYWORDS
_ATTR_PCT = _module._ATTR_PCT
_ATTR_FIXED = _module._ATTR_FIXED
_SPECIAL_PCT = _module._SPECIAL_PCT
_DMG_INC = _module._DMG_INC
_DMG_RED = _module._DMG_RED
_SIZE_INC = _module._SIZE_INC
_SIZE_RED = _module._SIZE_RED
_BUFF_RE = _module._BUFF_RE

# 导出函数
load_reference = _module.load_reference
_get_ref = _module._get_ref
_normalize_desc = _module._normalize_desc
_map_buff = _module._map_buff
_scope_clause = _module._scope_clause
_parse_filters = _module._parse_filters
_infer_scope = _module._infer_scope
_infer_condition = _module._infer_condition
extract_skill_buffs = _module.extract_skill_buffs
extract_flagship_buffs = _module.extract_flagship_buffs
extract_flagship_auras = _module.extract_flagship_auras
extract_duration_contrib = _module.extract_duration_contrib
extract_raguz_bonus = _module.extract_raguz_bonus
extract_cd_effects = _module.extract_cd_effects
extract_static_buffs_from_text = _module.extract_static_buffs_from_text
LOOP_CD_RE = _module.LOOP_CD_RE
extract_loop_cd = _module.extract_loop_cd
match_ship_class = _module.match_ship_class
KAI_BASE = _module.KAI_BASE
