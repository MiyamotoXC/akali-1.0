# -*- coding: utf-8 -*-
"""标准化副本生成脚本（知识库构建 → 阿卡莉智能体）

流程：
1. 读取原始信息表 raw_data.json（原始面板 + 阵营名国籍 + talents）
2. 读取现有副本 data.json（技能/buff 标准化产物，来自 LLM 解析 + regen_skill_buffs）
3. 面板数值调整（基于原始面板，比例集中在文件顶部 PANEL_RATIO）：
   航空+17%、装甲+17%、雷击+10%、血量+10%、对空+10%、命中+10%、幸运+5%（其余字段不变）
4. 国籍同步为阵营名（美→尤奈特 等，与 raw_data.json 一致）
5. 每角色写入 talent_default（PVP 推荐编码，普通计算默认；来自 talent_pvp.json）
6. 写回 data.json（先备份）

用法（在 阿卡莉1.0 目录下，用项目 venv）：
  .venv/Scripts/python.exe 开发者入口/build/标准化副本.py

注意：本脚本**不在 构建入口.py 增量链上**（它依赖 data.json 已由 角色数据构建.py 产出）。
逐函数说明见 开发者入口/构建说明.md。
"""
import json
import os
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
BASE = os.path.dirname(os.path.dirname(_HERE))
RAW_FILE = os.path.join(BASE, '知识库', '底层数据', '产物', 'raw_data.json')
DATA_FILE = os.path.join(BASE, '知识库', '底层数据', '产物', 'data.json')
PVP_FILE = os.path.join(BASE, '知识库', '底层数据', '产物', 'talent_pvp.json')

# 面板调整系数（原始面板 × 系数，写入副本）
PANEL_RATIO = {
    '航空': 1.17, '雷击': 1.10, '血量': 1.10, '对空': 1.10,
    '装甲': 1.17, '命中': 1.10, '幸运': 1.05,
}

# raw 面板键 → data.json 面板键（attack/defense）
RAW_PANEL_KEY = '面板'  # raw_data.json panel 下的数值 dict
PANEL_ATK_MAP = {'炮击': '炮击', '雷击': '雷击', '航空': '航空', '轰炸': '轰炸',
                 '鱼雷': '鱼雷', '索敌': '索敌', '命中': '命中', '幸运': '幸运', '装填': '装填'}
PANEL_DEF_MAP = {'装甲': '装甲', '对空': '对空', '机动': '机动', '血量': '血量',
                 '回避率': '回避率', '防暴击率': '防暴击率'}


def main():
    with open(RAW_FILE, encoding='utf-8') as f:
        raw = json.load(f)
    with open(DATA_FILE, encoding='utf-8') as f:
        data = json.load(f)
    pvp = {}
    if os.path.exists(PVP_FILE):
        with open(PVP_FILE, encoding='utf-8') as f:
            pvp = json.load(f)

    bak = DATA_FILE + '.bak_copy'
    shutil.copy2(DATA_FILE, bak)

    adjusted = 0
    for name, ship in data.items():
        raw_ship = raw.get(name)
        if not raw_ship:
            continue
        # 1) 面板调整（基于原始面板，只调有系数的字段，其余保留副本原值）
        rp = raw_ship.get('panel', {}).get(RAW_PANEL_KEY, {}) or {}
        ap = ship.get('attack_panel', {}) or {}
        dp = ship.get('defense_panel', {}) or {}
        for rk, dk in PANEL_ATK_MAP.items():
            ratio = PANEL_RATIO.get(rk)
            if ratio and rk in rp and rp[rk]:
                ap[dk] = round(float(rp[rk]) * ratio, 2)
        for rk, dk in PANEL_DEF_MAP.items():
            ratio = PANEL_RATIO.get(rk)
            if ratio and rk in rp and rp[rk]:
                dp[dk] = round(float(rp[rk]) * ratio, 2)
        ship['attack_panel'] = ap
        ship['defense_panel'] = dp
        # 1b) 航速（等级无关常量，不参与系数调整、也不进战斗面板，直接同步到 speed 字段）
        sp = rp.get('航速')
        if sp is not None:
            try:
                ship['speed'] = float(sp)
            except (ValueError, TypeError):
                pass
        # 2) 国籍同步（阵营名）
        camp = raw_ship.get('panel', {}).get('所属', '')
        if camp:
            ship['nation'] = camp
        # 3) 默认天赋（PVP 推荐编码）
        if name in pvp:
            ship['talent_default'] = pvp[name]
        adjusted += 1

    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'已更新副本 {adjusted} 个角色 → {DATA_FILE}')
    print(f'备份: {bak}')
    print(f'面板调整系数: {PANEL_RATIO}')


if __name__ == '__main__':
    main()
