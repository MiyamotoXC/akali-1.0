#!/usr/bin/env python3
"""
Incremental knowledge base builder for 拂晓：胜利之刻 Agent.

Strategy:
  - File-level: MD5 hash of raw file content → skip unchanged files entirely
  - Section-level: MD5 hash of cleaned section text → only re-chunk changed sections
  - Stable chunk IDs: {file_prefix}_{section_hash[:8]}_{chunk_seq}
    → unchanged sections keep their IDs, changed sections get new IDs
  - Image chunks and reference data: kept as-is (updated separately)

Usage:
  python3 知识库增量.py          # incremental update
  python3 知识库增量.py --full   # force full rebuild
"""

import json
import re
import os
import hashlib
import sys
import argparse

# ============================================================
# 1. Configuration (mirrors build_unified_kb.py)
# ============================================================
# 本地化：相对脚本目录自动定位知识库（避免硬编码开发机路径）
_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（build/ → 开发者入口/ → 阿卡莉1.0/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_KB_DIR_LOCAL = os.path.join(_PROJECT_ROOT, "知识库")
WORK_DIR = os.path.join(_KB_DIR_LOCAL, "攻略_md")
OUTPUT_PATH = os.path.join(_KB_DIR_LOCAL, "llm", "拂晓知识库_agent.json")
RAW_DATA_PATH = os.path.join(_KB_DIR_LOCAL, "底层数据", "产物", "raw_data.json")
TALENT_MAP_PATH = os.path.join(_KB_DIR_LOCAL, "底层数据", "产物", "talent_map.json")
TALENT_PVP_PATH = os.path.join(_KB_DIR_LOCAL, "底层数据", "产物", "talent_pvp.json")
ENEMY_DATA_PATH = os.path.join(_KB_DIR_LOCAL, "底层数据", "源文件", "拉古兹.json")
SHIP_ATTACK_PATH = os.path.join(_KB_DIR_LOCAL, "底层数据", "源文件", "拂晓舰灵普攻倍率cd1.1.xlsx")
SIMPLIFIED_KB_PATH = os.path.join(_KB_DIR_LOCAL, "攻略", "拂晓攻略知识库_精简版.json")
CATEGORIZED_KB_PATH = os.path.join(_KB_DIR_LOCAL, "拂晓攻略知识库_分类版.json")
INDEX_KB_PATH = os.path.join(_KB_DIR_LOCAL, "拂晓攻略知识库_索引.json")

MD_FILES = [
    ("1 前言.md", "前言", "元信息"),
    # ("2 游戏简介.md", ...) 已移除（2026-09-10 用户指示）：游戏简介不走 agent KB chunk，
    # 运行时由 calc_flow._load_game_intro_realtime 实时直读 知识库/游戏简介.docx
    ("萌新入坑攻略.md", "萌新入坑攻略", "萌新入门"),
    ("6.1 练级百科.md", "练级百科", "练级攻略"),
    ("6.2 给萌新的活动推图攻略食用指南.md", "活动推图攻略食用指南", "活动玩法"),
    ("6.3 给萌新的PVE配队思路.md", "PVE配队思路", "PVE攻略"),
    ("7.1 争锋竞技场简易百科.md", "争锋竞技场简易百科", "PVP攻略"),
    ("7.2 遭遇战攻略.md", "遭遇战攻略", "PVE攻略"),
    ("7.3 无尽海域配队思路.md", "无尽海域配队思路", "PVE攻略"),
    ("8.1 拂晓伤害计算构成.md", "伤害计算构成", "数值机制"),
    ("8.3 弹种与护甲机制.md", "弹种与护甲机制", "数值机制"),
    ("8.4 主副炮循环装填数值表.md", "主副炮循环装填数值表", "数值机制"),
    ("咨询问答表P1.md", "咨询问答表P1", "咨询问答"),
    ("咨询问答表P2.md", "咨询问答表P2", "咨询问答"),
    ("咨询问答表P3.md", "咨询问答表P3", "咨询问答"),
    ("9 拉古兹专题.md", "拉古兹专题", "敌人图鉴"),
    ("遭遇战_亚斯塔露.md", "遭遇战_亚斯塔露", "遭遇战"),
    ("遭遇战_雅蒙.md", "遭遇战_雅蒙", "遭遇战"),
    ("遭遇战_列拉金.md", "遭遇战_列拉金", "遭遇战"),
    ("遭遇战_巴巴托斯.md", "遭遇战_巴巴托斯", "遭遇战"),
    ("遭遇战_夕茉莉.md", "遭遇战_夕茉莉", "遭遇战"),
    ("遭遇战_拜蒙.md", "遭遇战_拜蒙", "遭遇战"),
    ("遭遇战_河豚王.md", "遭遇战_河豚王", "遭遇战"),
    ("遭遇战_布耶尔.md", "遭遇战_布耶尔", "遭遇战"),
    ("遭遇战_阿丝莫德.md", "遭遇战_阿丝莫德", "遭遇战"),
    ("遭遇战_沙克斯.md", "遭遇战_沙克斯", "遭遇战"),
    ("遭遇战_嘉波.md", "遭遇战_嘉波", "遭遇战"),
    ("改造优先级2.0.3.md", "拂晓舰灵改造优先级", "改造推荐"),
]

# ============================================================
# 2. Text cleaning (imported from build_unified_kb.py)
# ============================================================
def strip_images(text):
    text = re.sub(r'!\[.*?\]\(.*?\)(\{.*?\})?', '', text)
    return text

def strip_md_artifacts(text):
    text = re.sub(r'^[\s\|:-]+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|', '', text, flags=re.MULTILINE)
    text = re.sub(r'\|$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*\|\s*', ' ', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-=]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\\\[[\d]+\\\]', '', text)
    return text

def clean_text(text):
    text = strip_images(text)
    text = strip_md_artifacts(text)
    lines = text.split('\n')
    cleaned_lines = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        if stripped == '':
            if not prev_blank:
                cleaned_lines.append('')
                prev_blank = True
        else:
            cleaned_lines.append(line)
            prev_blank = False
    while cleaned_lines and cleaned_lines[0] == '':
        cleaned_lines.pop(0)
    while cleaned_lines and cleaned_lines[-1] == '':
        cleaned_lines.pop()
    return '\n'.join(cleaned_lines)

# ============================================================
# 3. Chunk splitting (imported from build_unified_kb.py)
# ============================================================
def split_text_into_chunks(text, max_len=1000, min_len=450):
    if len(text) <= max_len:
        return [text] if text.strip() else []
    paragraphs = re.split(r'\n\s*\n', text)
    chunks = []
    current_chunk = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current_chunk) + len(para) + 2 <= max_len:
            if current_chunk:
                current_chunk += '\n\n' + para
            else:
                current_chunk = para
        else:
            if current_chunk:
                chunks.append(current_chunk)
            if len(para) > max_len:
                sub_lines = para.split('\n')
                sub_chunk = ""
                for line in sub_lines:
                    line = line.strip()
                    if not line:
                        continue
                    if len(sub_chunk) + len(line) + 1 <= max_len:
                        if sub_chunk:
                            sub_chunk += '\n' + line
                        else:
                            sub_chunk = line
                    else:
                        if sub_chunk:
                            chunks.append(sub_chunk)
                        sub_chunk = line
                if sub_chunk:
                    current_chunk = sub_chunk
                else:
                    current_chunk = ""
            else:
                current_chunk = para
    if current_chunk:
        chunks.append(current_chunk)
    merged = []
    for chunk in chunks:
        if merged and len(chunk) < min_len:
            if len(merged[-1]) + len(chunk) + 2 <= max_len:
                merged[-1] += '\n\n' + chunk
                continue
        merged.append(chunk)
    return merged

# ============================================================
# 4. Section detection (imported from build_unified_kb.py)
# ============================================================
def detect_sections(lines, source_name):
    sections = []
    current_section_title = source_name
    current_lines = []
    for i, line in enumerate(lines):
        is_header = False
        header_text = ""
        m = re.match(r'^(#{1,6})\s+(.+)', line)
        if m:
            is_header = True
            header_text = m.group(2).strip()
        if i + 1 < len(lines) and re.match(r'^[=-]{3,}$', lines[i+1].strip()):
            if line.strip() and not line.strip().startswith('!'):
                is_header = True
                header_text = line.strip()
        if not is_header and line.strip() and len(line.strip()) < 30:
            stripped = line.strip()
            if re.match(r'^[一二三四五六七八九十〇]+[、\.]', stripped):
                is_header = True
                header_text = stripped
            elif re.match(r'^\d+[\.\s]', stripped) and not re.match(r'^\d+\s*$', stripped):
                is_header = True
                header_text = stripped
        if is_header and header_text:
            if current_lines:
                sections.append((current_section_title, current_lines))
            current_section_title = header_text
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_section_title, current_lines))
    return sections

# ============================================================
# 5. Keyword extraction (imported from build_unified_kb.py)
# ============================================================
GAME_TERM_DICT = [
    '拂晓', '舰灵', '推图', '练级', '抽卡', '建造', '改造', '强化', '升星',
    '装备', '紫装', '金装', '彩装', '雷达', '索敌', '阵型', '旗舰', '天赋', '技能',
    '必杀技', '羁绊技', '支援技', '通常技', '旗舰技',
    '甲弹克制', '高爆', '穿甲', '通常弹', '装甲', '轻甲', '中甲', '重甲',
    '炮击', '雷击', '航空', '轰炸', '鱼雷', '轰炸机', '鱼雷机', '战斗机',
    '战列', '战列舰', '航母', '驱逐', '驱逐舰', '重巡', '重巡洋舰',
    '轻巡', '轻巡洋舰', '轻母', '航战', '战巡', '雷巡', '装母', '重炮', '水母',
    '油', '晶币', '钻石', '抽卡券', '零件', 'GE', '灵晶', '徽章', '核心', '星砂',
    '炼油厂', '温泉', '造币厂', '远征', '演习', '沙滩', '档案',
    '咨询中心', '技能学院', '日常关卡', '便利店',
    '竞技场', '争锋', '遭遇战', '防卫战', '无尽海域', '打捞活动',
    '补给姬', '多倍经验', '通行证', '月卡', '自选', '88自选',
    '好感', '誓约', '婚戒', '节能', '省油', '节能天赋', '节能科技',
    '老板', '打手', 'MVP', 'T优', 'T劣', '同航', '反航',
    '夜战', '昼战', '手操', '托管', '保底',
    '回溯装置', '誓约礼盒', '船坞', '金马桶', '胜利之刻勋章',
    'SG对海雷达', '紫雷达', '食物', '椰子', '经验',
    '兑换凭证', '设备零件', 'SSR密钥',
    '单纵阵', '复纵阵', '轮型阵', '梯形阵',
    '5-10', '12-10', '18-7', '19-5', '19-10', '20-5', '9-1', '10-1', '13-5',
    '1-1', '4-10', '5-1', '6-1', '8-10', '5-9', '10-15', '16章',
    '时雨', '长门', '陆奥', '伊势', '前卫', '女灶神', '赤城', '加贺',
    '胡德', '欧根亲王', '吹雪', '吸血鬼', '拉菲', '大拉菲', '小拉菲',
    '雪风', '海伦娜', '华盛顿', '齐柏林', '齐柏林伯爵', '萨拉托加',
    '绫波', '晓', '雷', '哈曼', '伦敦', '日向',
    '基洛夫', '逸仙', '列克星敦', '企业', '埃塞克斯',
    '威尔士亲王', '瑞鹤', '圣女贞德', '南达科他',
    '贝尔法斯特', '俾斯麦', '提尔比茨', '厌战',
    '空想', '维内托', '罗德尼', '北卡', '俄克拉荷马', '纳尔逊',
    '白雪', '伏尔塔', '岛风', 'Z1', '响', '巨像', '北上', '大井',
    '霞飞', '爱宕', '大黄蜂', '翔鹤', '飞鹰', '龙骧', '独角兽',
    '凤栖', '攻略组', '甲弹', '护甲', '星级', '稀有度',
    '强化素材', '改造图纸', '芯片', '换装', '皮肤',
    '好感度', '相识', '誓约', '8槽', '六星',
    '通行证任务', '日常任务', '限时远征', '远征队', '档案回溯装置',
    '加成试用', '带路条件', '累伤奖励', '排名奖励',
    '专用BOX', '出战次数', '羁绊之物',
    '人机', '保底奖励', '钻1', '争锋竞技场',
    '先进动力系统', '搓澡', '排版功能',
    'SSR', 'SR', 'UR',
    '新手等级礼包', '自选箱', 'SR自选箱',
    '贸易商店', '补给兑换',
    '夜战图', '昼战图', '夜战阵容', '昼战阵容',
    '穿甲弹战列舰', '高爆弹战列舰', '雷击驱逐',
    '鱼雷航系', '穿甲战列', '穿甲轻巡', '穿甲重巡',
    '推图阵容', '黄金练级点', '地狱难度', '地狱EX',
    '困难', '困难主线', '换装券',
    '逐鹰计划', '船坞建料',
    '88自选', '新手调遣', '初心者调遣', '春信启程', '常规调遣',
    '活动调遣', '自选UP',
    '产油', '产油量', '产油效率', '产能',
    '复刻', '闪回', '常驻', '限定',
    '退役',
    '装备箱', '紫色装备箱',
    '伤害类支援技', 'buff类支援技',
    '镜头', '技能演出', '暂停',
    '自爆船', '激光', '弹幕', '火海', '电海',
    '穿甲弹', '高爆弹',
    '好感剧情', '剧情榜',
    '面板数值', '计算公式', '基础值', '装备值', '百分比加成',
    '加算', '乘算', '向下取整',
    '主副炮', '循环', '装填', '装填阈值',
    '巡洋舰', '155mm', 'BB', 'BC', 'CVL', '九三式鱼雷', '长矛',
    'VT引信', '无线电近炸', 'Big Seven', '460mm', '厌战号', '24.6km',
    'T0', 'T1', 'T2', 'T3', 'T4', '梯度',
    '配装', '毕业装', '输出设备', '生存设备', '下位替代',
    '主炮', '副炮', '防空炮', '舰载机', '设备',
    '一键装备', '预设方案', '卸装',
    'PVP天赋', '天赋加点', '增伤', '减伤',
    '1212', '1221', '1111', '索敌优先',
    '改造优先级', '性价比',
    '劳模舰灵', '1+1配队', '3倍好感',
    '奶与拐', '奶', '拐', '自回', '自奶',
    '课金', '充值', '月卡', '大月卡', '小月卡',
    '新手调遣特令', '30日暖心换装卡',
    '碎钻',
    'BOSS', 'boss', '小怪', '敌人',
    '遭遇战模拟', '拉古兹', '装甲类型', '对空', '回避', '幸运',
    '嘉波', '巴巴托斯', '阿丝莫德', '亚斯塔露', '列拉金',
    '沙克斯', '夕茉莉', '河豚王', '拜蒙', '雅蒙', '布耶尔',
    '有效弹种', '弹种补正', '独立乘区',
    '命中', '机动', '闪避', '幸运值',
    '分差补偿', '防守阵容', '赛季天气',
    '信物', '蓝场', '紫场', '金场',
    '模组', 'AOE', '单体输出', '生存拐', '输出拐',
    '排轴', '冲分', '文字轴', '技能CD',
    '食物+', '椰子',
    '新区', '老区', '星星海',
    '邮箱', '邮件', '100抽',
    '活动代币', '活动奖励', '活动特色', '活动技巧',
    '强化素材', '基础资源',
    '阵容', '配队', '配装', '站位',
    '武器运输', '战场探索', '改造图纸',
    '扫荡', '扫荡不耗油',
    '工作舰灵', '工作位', '泡澡位',
    '限时卡池', 'UP舰灵', 'UP池',
    '困难2-5', '困难2图', '驱逐旗舰',
    '五星', '六星', '复制人',
    '平铺', '+3', '+6', '+10',
    '八咫', '尤奈特', '奥鲁加', '洛蒙瑞亚', '纳榭尔', '列加杜',
    '阵营', '二创',
    '凤栖攻略组',
    '建造栏', '建造栏扩展', '远征任务栏',
    '温泉竹', '温泉樱',
    '练级百科', '省流版',
    '一路3星', '3星',
    '高爆克轻甲', '穿甲克重甲', '通常克中甲',
    '航系', '炮系', '小船', '中船', '大船',
    '补给舰',
    'EX难度',
    '18图', '19图', '20图',
    '主线', '主线图', '主线最终图',
    '活动地狱', '活动困难',
    '经验获取', '经验提升',
    '升至3星', '平铺1星',
    '炼油厂需要12个时雨',
    '推图战斗力', '开图鉴',
    '君羊', '老玩家',
    '数值天花板', '必要舰灵',
    '建造玩法', '获取概率',
    '礼包',
    '时雨今天下班没',
    '装填阈值', '命中雷达', '火控',
    '彩火控', '方火控', '长火控',
    '超重弹', '九一式', '91',
    '高脚杯', '蛋糕', '勋章', '柴油机',
    '彩轰炸', '毁灭者', '幸运SBD', '80番',
    '金剑鱼', '紫剑鱼', '彩鱼雷机', '天河',
    '彩460', '彩三联406', '彩381', '彩双联406', 'MK6', '381',
    '彩轻巡', '152', '305', '180',
    '彩六联', '彩五联', '610',
    '炮击防空', '命中防空', '彩防空', '金防空',
    '液压弹射器',
    '黎塞留', '新泽西', '卡约', '杜伊利奥', '金刚', '沙恩霍斯特',
    '敦刻尔克', '伊丽莎白', '利托里奥', '维内托', '德弗林格尔',
    '神通', '苍龙', '飞龙', '天鹰',
    '小拉菲', '小拉菲改', '萨拉托加改',
    '列克星敦改', '齐柏林改',
    '波拉', '宁海', '平海', '重庆',
    '光辉', '杰维斯', '欧根',
    '深雪', '火力', 'Z16',
    '凉月', '初雪', '基辅', '夕立',
    '应瑞', '奥马哈', '约克城',
    '卡米契亚', '内拉', '埃罗芒什',
    '凌波', '加贺',
    '镇海', '独角兽',
    '新奥尔良', '唐斯', '卡约杜伊利奥',
    '北卡罗来纳', '纳尔逊', '威尔士亲王',
    '损害公式', '伤害公式',
    '固伤点燃', '固有点燃', '进水', '火坑',
    '减速', '眩晕', '控制',
    '导驱', '导弹', '电磁脉冲',
    '自爆舰', 'A型自爆', 'B型自爆',
    '国籍', '甲乙丙丁戊己',
    '安德玛丽', '巴伐利亚',
    '模组平衡', '占模',
    '等级压制', 'T优势', '空占',
    # Encounter battle character names and nicknames
    '亚斯塔露', '雅蒙', '列拉金', '巴巴托斯', '夕茉莉', '拜蒙',
    '河豚王', '布耶尔', '阿丝莫德', '沙克斯', '嘉波',
    '墩子', '花生', '饼拓', '利拓', '太太', '小姨子', '姨太',
    '780', '德芙', '吃喝', '飞鹰', '苍龙', '飞龙', '天鹰',
    '神通', '大井', '北上', 'Z16', '长门',
    '文字轴', '切换', '秒点', '连点', '支援技', '通常技',
    '夜战', '全白', '50S夜', '30S夜',
    '普攻倍率', '普攻cd', '有效射程', '炮弹数', '伤害类型',
    '主炮', '副炮', '鱼雷', '轰炸', '轰炸机', '鱼雷机',
    '战列穿甲', '战列高爆', '战列通常',
    '轻巡穿甲', '轻巡高爆', '轻巡通常',
    '重巡穿甲', '重巡高爆', '重巡通常',
    '驱逐穿甲', '驱逐高爆', '驱逐通常',
    '模组', '共用cd', '巡航',
    '田纳西', '加利福尼亚', '内华达', '雾岛', '比叡',
    '约克公爵', '甘古特', '黑暗界', '恐怖',
    '圣地亚哥', '长良', '川内', '那珂', '柯尼斯堡',
    '卡尔斯鲁厄', '科隆', '里士满', '天龙', '奈亚',
    '阿瑞托莎', '谢菲尔德', '恰巴耶夫', '蒙彼利埃',
    '高雄', '羽黑', '青叶', '衣笠', '妙高', '昆西',
    '威奇塔', '盐湖城', '诺福克', '萨福克', '约克',
    '坎伯兰', '利根', '明尼阿波利斯',
    '巨像', '兰利', '博格', '百眼巨人', '龙骧', '隼鹰',
    '皇家方舟', '贝亚恩', '鹰', '大凤', '光辉',
    '朱塞佩·加里波第', '卡约·杜伊利奥', '布列塔尼',
    '舍尔海军上将',
]

def extract_keywords(text, section_name, topic, source_name, max_keywords=15):
    keywords = []
    seen = set()
    def add_kw(kw):
        kw = kw.strip()
        if kw and kw not in seen and len(keywords) < max_keywords:
            keywords.append(kw)
            seen.add(kw)
    add_kw(topic)
    add_kw(source_name)
    for part in re.split(r'[-/（）\(\)]', section_name):
        part = part.strip()
        if part and len(part) >= 2:
            add_kw(part)
    sorted_terms = sorted(set(GAME_TERM_DICT), key=len, reverse=True)
    for term in sorted_terms:
        if len(keywords) >= max_keywords:
            break
        if term and term in text:
            add_kw(term)
    stage_patterns = re.findall(r'\d+-\d+', text)
    for p in stage_patterns:
        add_kw(p)
    tier_patterns = re.findall(r'T[0-4]', text)
    for p in tier_patterns:
        add_kw(p)
    return keywords[:max_keywords]

# ============================================================
# 6. Hashing utilities
# ============================================================
def file_hash(filepath):
    """MD5 hash of file content."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def text_hash(text):
    """MD5 hash of text content (used for section-level change detection)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def make_file_prefix(source_name):
    """Create a short stable prefix from source name for chunk IDs."""
    # Use pinyin-like abbreviation or first few chars
    # For Chinese names, use a hash-based short prefix
    h = hashlib.md5(source_name.encode("utf-8")).hexdigest()[:6]
    return f"f{h}"

def make_chunk_id(file_prefix, section_hash, chunk_seq):
    """Stable chunk ID: {file_prefix}_{section_hash[:8]}_{seq}"""
    return f"{file_prefix}_{section_hash[:8]}_{chunk_seq}"

# ============================================================
# 7. Topic auto-detection (imported from build_unified_kb.py)
# ============================================================
def detect_topic(default_topic, section_title, cleaned_text):
    topic = default_topic
    # 游戏概述/元信息 为顶层分类，不应被正文关键词（如"活动"/"养成"）误判覆盖。
    # 2 游戏简介 default_topic=游戏概述（正文含"活动"会被误判为活动玩法）；
    # 1 前言 default_topic=元信息（与攻略精简版 D01 一致，避免混入游戏概述回答）。
    if default_topic in ('游戏概述', '元信息', '改造推荐'):
        return default_topic
    section_lower = section_title.lower() + cleaned_text[:200]
    if any(kw in section_lower for kw in ['抽卡', '建造', '选区', '新号', '开局']):
        topic = "萌新入门"
    elif any(kw in section_lower for kw in ['推图', '主线', '1-1', '4-10', '5-1', '8-10', '9-1', '12-10']):
        topic = "推图攻略"
    elif any(kw in section_lower for kw in ['练级', '经验', '老板', '打手', '劳模']):
        topic = "练级攻略"
    elif any(kw in section_lower for kw in ['活动', '打捞', '带路']):
        topic = "活动玩法"
    elif any(kw in section_lower for kw in ['配队', '阵容', 'PVE']):
        topic = "PVE攻略"
    elif any(kw in section_lower for kw in ['竞技场', '争锋', 'PVP']):
        topic = "PVP攻略"
    elif any(kw in section_lower for kw in ['遭遇战', 'BOSS', 'boss', '冲分', '排轴']):
        topic = "遭遇战"
    elif any(kw in section_lower for kw in ['无尽海域', '爬塔']):
        topic = "无尽海域"
    elif any(kw in section_lower for kw in ['伤害', '计算', '公式', '面板', '装甲']):
        topic = "数值机制"
    elif any(kw in section_lower for kw in ['拉古兹', '小怪', 'BOSS介绍']):
        topic = "敌人图鉴"
    elif any(kw in section_lower for kw in ['装备', '配装', '主炮', '副炮', '鱼雷', '舰载机']):
        topic = "装备系统"
    elif any(kw in section_lower for kw in ['天赋', '加点']):
        topic = "天赋系统"
    elif any(kw in section_lower for kw in ['资源', '油', '晶币', '钻石']):
        topic = "资源系统"
    return topic

# ============================================================
# 8. Image/XLSX chunks (static, preserved across updates)
# ============================================================
# These are manually defined OCR/summary chunks for images and spreadsheets.
# They are NOT auto-generated from markdown, so they are preserved as-is
# during incremental updates unless explicitly modified here.
IMAGE_CHUNKS = [
    {"id": "img_001", "source": "性能优化小开关教程.png", "topic": "性能优化", "section": "性能优化小开关教程", "content": "性能优化小开关教程。配置文件路径: Android/data/com.megagame.fuxiaou.fuxiaou/files/extraConfig.json。修改参数: Normal_fps 30改为90, Limit_fps 30改为90, Localization 保持1。操作步骤: 找到extraConfig.json文件 → 将后缀名改为txt → 用文本编辑器打开并修改参数 → 保存后将后缀改回json。优化后可提升舰灵立绘表现流畅度，让名字显示更清晰，便于查看攻略和社区交流。", "keywords": ["性能优化", "帧率", "90帧", "extraConfig", "Normal_fps", "Limit_fps", "Localization", "配置文件", "流畅度", "立绘"]},
    {"id": "img_002", "source": "新手入门思维导图_v2.4.png", "topic": "萌新入门", "section": "新手入门思维导图 v2.4", "content": "新手入门思维导图 v2.4。以树状分支结构展示入门全流程框架，包含游戏各系统与玩法脉络。核心流程: 捞船→练级→捞船→练级→进阶玩法。涵盖选区、性能优化、开局抽卡、推图攻略、练级方法、舰灵养成、装备配装、日常玩法、活动参与、课金建议等各个入门环节的系统性导览，帮助萌新快速建立对游戏整体框架的认知。", "keywords": ["思维导图", "萌新入门", "捞船", "练级", "进阶玩法", "入门流程", "游戏框架", "v2.4", "攻略"]},
    {"id": "img_003", "source": "PVE舰灵推荐表_v2.4.png", "topic": "舰灵推荐", "section": "PVE舰灵推荐表 v2.4", "content": "PVE舰灵推荐表 v2.4。按舰种分类(小船/中船/大船炮系/大船航系)，分T0-T4梯度推荐，区分常驻/限定/档案获取途径。T0小船: 吹雪、白雪、女灶神、伏尔塔。T0中船: 欧根亲王、基洛夫、逸仙。T0大船炮系: 陆奥、胡德、前卫。T0大船航系: 列克星敦、萨拉托加、企业、埃塞克斯。表格覆盖各舰种强力PVE舰灵，为萌新选船练船提供梯度参考。", "keywords": ["PVE推荐", "舰灵推荐", "T0", "梯度", "吹雪", "白雪", "女灶神", "欧根亲王", "陆奥", "胡德", "前卫", "列克星敦", "企业", "埃塞克斯", "v2.4"]},
    {"id": "img_004", "source": "萌新入门配装推荐_v2.1.png", "topic": "装备系统", "section": "萌新入门配装推荐 v2.1", "content": "萌新入门配装推荐 v2.1。按舰种分类(小型/中型/大型舰船)，每艘舰灵提供通用毕业配装和输出/生存下位替代方案。装备分类: ①通用毕业 ②输出设备 ③生存设备 ④下位替代。覆盖驱逐、轻巡、重巡、轻母、战列、战巡、航母等各舰种的主流配装推荐，帮助萌新快速完成装备搭配。同级紫装不输金装，部分输出紫装甚至远强于金装。", "keywords": ["配装推荐", "毕业装", "输出设备", "生存设备", "下位替代", "装备系统", "驱逐", "重巡", "战列", "航母", "v2.1"]},
    {"id": "img_005", "source": "装备速查表.png", "topic": "装备系统", "section": "装备速查表", "content": "装备速查表。按类型分类，以颜色边框标识推荐程度: 红色=必做、黄色=推荐、紫色=可选、蓝色=特殊用途。涵盖主炮、副炮、鱼雷、舰载机(轰炸机/鱼雷机/战斗机)、防空炮、设备等各类装备。为玩家提供一目了然的装备制作与强化优先级参考，帮助快速决策装备投入方向。", "keywords": ["装备速查表", "主炮", "副炮", "鱼雷", "舰载机", "防空炮", "设备", "必做", "推荐", "可选", "特殊用途", "装备系统"]},
    {"id": "img_006", "source": "阵容分析推荐.png", "topic": "阵容阵型", "section": "阵容分析推荐", "content": "阵容分析推荐。四种阵型详解: 单纵阵(炮击雷击+15%，PVE推图首选)、复纵阵(机动+30%约12-15%有效回避，高难/PVP首选)、轮型阵(航空对空+20%炮击雷击-5%，航系专用)、梯形阵(雷击+15%暴伤+5%被暴击+5%，雷爆特化高风险)。不同阵型适用于不同场景，需根据敌方阵容和己方舰种合理选择。", "keywords": ["阵型", "单纵阵", "复纵阵", "轮型阵", "梯形阵", "炮击", "雷击", "机动", "回避", "航空", "对空", "暴伤", "PVE", "PVP", "阵容"]},
    {"id": "img_007", "source": "一键装备设置流程.png", "topic": "装备系统", "section": "一键装备设置流程", "content": "一键装备设置流程。预设方案1=竞技场金船/演习/主线活动，预设方案2=竞技场紫蓝船/遭遇战。操作流程: 卸下全部装备→装好队伍1的装备→保存为预设方案1→一键卸装→装好队伍2的装备→保存为预设方案2→之后切换项目时一键装备即可快速切换。适用于竞技场、演习、遭遇战、主线活动等场景间的快速装备切换。", "keywords": ["一键装备", "预设方案", "卸装", "竞技场", "演习", "遭遇战", "主线活动", "装备切换", "装备系统"]},
    {"id": "img_008", "source": "PVP天赋表_v3.3.png", "topic": "PVP天赋", "section": "PVP天赋表 v3.3", "content": "PVP天赋表 v3.3。按舰种分类提供4位数字加点方案。核心原则: 索敌优先、非自身船型增伤优先于减伤。常见方案: 1212通用型、1221输出型、1111特化型。涵盖补给舰、驱逐、轻母、重巡、重炮、轻巡、雷巡、装母、航母、战巡、战列、航战等各舰种的PVP天赋加点推荐。4位数字分别对应4层天赋的选择(A=1, B=2等)。", "keywords": ["PVP天赋", "天赋加点", "索敌", "增伤", "减伤", "1212", "1221", "1111", "驱逐", "轻母", "重巡", "航母", "战列", "v3.3"]},
    {"id": "img_009", "source": "9-拂晓舰灵改造优先级2.0.3.png", "topic": "改造推荐", "section": "拂晓舰灵改造优先级2.0.3", "content": "拂晓舰灵改造优先级详解 2.0.3（游戏版本3.4）。改造价格表: SR/紫需灵晶9000/图纸50/钻石500，SSR/金需灵晶15000/图纸150/钻石500，UR/彩需灵晶40000/图纸200/钻石500；改造需舰灵等级达到45级且强化满。通用向改造推荐（优先级从左到右、从上到下递减）: 小拉菲（破空者，优先改，PVE与PVP表现均不错，无尽海域优质清杂打手）、胡德（纯白女皇，改后无尽海域挂炮击减伤大范围清杂，遭遇战中甲可选，PVP英队组件，练级阶段和改前拉不开差距，需求资源多）、Z1（泽斯托尔-1）、萨拉托加（空之罗宾）、列克星敦（灰夫人）、绫波（春柚）、吹雪（风舞姬）、萤火虫（流萤）。PVE向改造推荐: 大井（多多）、北上（木田神）、神通（雷鸟）、齐柏林伯爵（飞艇伯爵）、初雪（羽露草）、深雪（美由纪），多为雷巡队/遭遇战顶分/无尽海域60层后处理BOSS强者。PVP向改造推荐: 雪风（祥瑞御免）、夕立（夏雨临）、响（知枢）、维内托（薇妮特）、海伦娜（海纶）、白雪（诗炉雪）、巨像（巨人）、飞鹰（云鸱）、黎塞留（红衣公爵）、伊势（乌鹭）、日向（乌鹭）、大凤（山凤）。", "keywords": ["改造优先级", "改造推荐", "胡德", "纯白女皇", "小拉菲", "破空者", "Z1", "萨拉托加", "列克星敦", "绫波", "吹雪", "萤火虫", "大井", "北上", "神通", "齐柏林伯爵", "初雪", "深雪", "雪风", "夕立", "响", "维内托", "海伦娜", "白雪", "巨像", "飞鹰", "黎塞留", "伊势", "日向", "大凤", "改造价格", "灵晶", "图纸"]},
    {"id": "img_010", "source": "练级百科省流版.png", "topic": "练级攻略", "section": "练级百科省流版", "content": "练级百科省流版。劳模舰灵: 吹雪/Z1/吸血鬼/拉菲DD724，推荐1+1配队。旗舰提供3倍好感+50%经验，MVP+50%经验乘算叠加。采用老板-打手循环练级法。黄金练级点: 18-7/19-5/19-10/20-5/活动地狱EX。索敌1.25倍可获T优。复纵阵+先进动力系统提升生存。奶与拐推荐舰灵辅助练级。通过好感度相识点节能天赋降低油耗。", "keywords": ["练级", "劳模舰灵", "吹雪", "Z1", "吸血鬼", "拉菲", "1+1配队", "旗舰", "MVP", "老板", "打手", "18-7", "19-5", "索敌", "T优"]},
    {"id": "img_011", "source": "剧情榜_v2.0.png", "topic": "剧情评价", "section": "剧情榜 v2.0", "content": "剧情榜 v2.0。舰灵剧情质量分级排行。T0(已修改/新增好感剧情，高品质推荐): 欧根亲王、吸血鬼、俾斯麦、长门、胡德、前卫等。T1(良好评价): 企业、女灶神等。T3(不推荐): 萨拉托加、海伦娜等。T4(下架修复中): 爱宕、大黄蜂、维内托、华盛顿等。为玩家选择誓约/好感培养对象提供剧情质量参考。", "keywords": ["剧情榜", "剧情评价", "T0", "T1", "T3", "T4", "欧根亲王", "吸血鬼", "俾斯麦", "长门", "胡德", "前卫", "企业", "女灶神", "好感剧情"]},
    {"id": "img_012", "source": "面板数值计算公式_Ver3.2.png", "topic": "数值机制", "section": "面板数值计算公式 Ver3.2", "content": "面板数值计算公式 Ver3.2。计算公式: 最终值=(基础值+装备值)×(1+百分比加成总和)。百分比加成为加算而非乘算，结果向下取整。这意味着多个百分比加成效果之间是相加关系而非相乘关系，因此堆叠同类百分比加成的收益会递减。理解此公式对于装备选择、天赋加点和舰灵培养策略具有重要指导意义。", "keywords": ["面板数值", "计算公式", "基础值", "装备值", "百分比加成", "加算", "乘算", "向下取整", "数值机制", "Ver3.2"]},
    {"id": "img_013", "source": "主副炮循环装填阈值_V1.0.1.png", "topic": "数值机制", "section": "主副炮循环装填阈值 V1.0.1", "content": "主副炮循环装填阈值 V1.0.1。按装填值高低分为各档位: 低阈值(185+/204+): 俄克拉荷马/长门/陆奥。中阈值(205-209/215+): 提尔比茨/厌战/伊势等。高阈值(276+/296-307+): 胡德/前卫/俾斯麦等。特殊(417): 空想。无法循环: 维内托/罗德尼/北卡等14艘舰灵。指导战列战巡类舰灵的主副炮循环配装，达到阈值可实现主副炮交替循环射击。", "keywords": ["主副炮", "循环", "装填", "阈值", "长门", "陆奥", "胡德", "前卫", "俾斯麦", "空想", "维内托", "数值机制", "V1.0.1"]},
    {"id": "img_014", "source": "咨询问答表P1.png", "topic": "咨询问答", "section": "咨询问答表P1", "content": "咨询问答表P1。38题海军知识问答题库。涵盖内容: 巡洋舰划分标准(155mm为主炮口径分界)、舰种缩写含义(BB=战列舰/BC=战列巡洋舰/CVL=轻型航母等)、九三式鱼雷又称长矛鱼雷、VT引信即无线电近炸引信、Big Seven指七艘拥有410mm以上主炮的战列舰(460mm不属于)、厌战号创造24.6km最远命中记录等海军历史知识。用于咨询中心答题。", "keywords": ["咨询问答", "答题", "海军知识", "巡洋舰", "155mm", "BB", "BC", "CVL", "九三式鱼雷", "长矛", "VT引信", "无线电近炸", "Big Seven", "460mm", "厌战号"]},
    {"id": "img_015", "source": "咨询问答表P2.jpg", "topic": "咨询问答", "section": "咨询问答表P2(第39-63题)", "content": "咨询问答表P2。第39-63题。舰灵舰种与国籍问答: 风舞姬(吹雪)为驱逐/八咫国籍; 黑桧(赤城)为航母/八咫国籍，与金泽(加贺)释放羁绊技; 奈雅纪(长门)与无津(陆奥)释放羁绊技; 宁洋(宁海)为轻巡洋舰; 破空者(拉菲DD724)为驱逐/尤奈特国籍; 云鸱(飞鹰)为轻母/八咫国籍。国籍判断: 洋平(平海)为凤棲国籍, 微妮特(维内托)为列加社国籍, 红衣公爵(黎塞留)为纳榭尔国籍。灰色幽灵=闪灵(企业), 蓝色幽灵=蓝女士(列克星敦CV-16)。辉煌(光辉)是唯一不受夜战影响的航母, 能在夜晚发动舰载机攻击。灰夫人(列克星敦)拥有合体技。山淀(大淀)担任旗舰时不会陷入T劣危机。禾椛(秋月)不属于特型驱逐。", "keywords": ["咨询问答", "答题", "吹雪", "赤城", "加贺", "长门", "陆奥", "宁海", "平海", "维内托", "黎塞留", "企业", "列克星敦", "光辉", "大淀", "秋月", "羁绊技", "国籍", "夜战"]},
    {"id": "img_016", "source": "咨询问答表P3.jpg", "topic": "咨询问答", "section": "咨询问答表P3(第64-100题)", "content": "咨询问答表P3。第64-100题。游戏机制问答: 技能3级后用初级技能书突破; 对付驱逐用高爆弹; 航母优势属性为航空; 副炮开火频率受装填影响; 舰灵最多突破到6星; 降低燃油消耗的天赋叫节能科技; 每队1位旗舰; 每位舰灵最多学5个天赋; 好感阶段无「熟悉」; SSR金色宝箱无法合成获得; 索敌影响航向形势; T劣势导致不利; 单纵阵为炮击特化; 驱逐优势属性为机动; 航母不适合夜战; 远征无法获取舰灵; 学习技能不增加好感; 建造不需要燃料; 轮型阵提高防空; 战斗机拦截敌机保证制空; 装备共5种类型; 战列优势属性为炮击; 制空值取决于战斗机数量。日常关卡: 装备强化素材奖励/每天3次。远征: 特殊派遣18点出现/最多4队。演习: 非6点刷新/每次5个目标。便利店6个工作位。体力8分钟恢复1点。港区无神社。技能学院6个训练位。炼油厂最高10级/产量不受工作舰灵等级影响。温泉6个恢复位。", "keywords": ["咨询问答", "答题", "技能书", "高爆弹", "航空", "装填", "6星", "节能科技", "旗舰", "天赋", "好感", "SSR", "索敌", "T劣势", "单纵阵", "机动", "夜战", "远征", "制空", "战斗机", "日常关卡", "炼油厂", "温泉"]},
    {"id": "img_017", "source": "8.2 拂晓舰灵普攻倍率cd.xlsx", "topic": "数值机制", "section": "舰灵普攻倍率CD表", "content": "舰灵普攻倍率CD表。按舰种分7类共196艘舰灵的普攻数据。驱逐导驱50艘: 普攻炮击+鱼雷/导弹，如吹雪炮击倍率2.4/CD2.4/通常弹15发+鱼雷0.92/CD4。轻巡雷巡38艘: 普攻炮击+鱼雷，如神通炮击2.4/CD2.4/穿甲12发+鱼雷0.92/CD4。重巡26艘: 普攻炮击+鱼雷，如欧根亲王炮击2.16/CD3/穿甲8发+鱼雷0.6/CD5。战列战巡重炮43艘: 主炮+副炮，如胡德主炮1.76/CD4.2/穿甲4发+副炮0.4/CD2/穿甲8发。轻母11艘: 轰炸/鱼雷随机1架，如巨像轰炸1.66+鱼雷1.66/CD3.5。航母装母25艘: 轰炸/鱼雷随机2架，如赤城轰炸1.6+鱼雷2.4/CD5。航战水母3艘: 炮击+轰炸，如伊势改主炮1.76/CD4.2+轰炸0.6/CD3.5。航系普攻同模组共用CD，战斗机巡航不造成伤害。详细数据见reference_data.ship_attack_data。", "keywords": ["普攻倍率", "普攻CD", "有效射程", "炮弹数", "伤害类型", "战列", "轻巡", "重巡", "驱逐", "航母", "轻母", "航战", "主炮", "副炮", "鱼雷", "轰炸"]},
]

# Sources that are NOT markdown files (images, xlsx) - always use IMAGE_CHUNKS
NON_MD_SOURCES = set(c["source"] for c in IMAGE_CHUNKS)

# ============================================================
# 9. Process a single markdown file → list of chunks
# ============================================================
def process_markdown_file(md_file, source_name, default_topic, file_prefix):
    """Process a single markdown file and return (chunks, section_hashes).

    section_hashes: dict of {section_hash: section_title} for change tracking.
    chunks: list of chunk dicts with stable IDs.
    """
    md_path = os.path.join(WORK_DIR, md_file)
    with open(md_path, "r", encoding="utf-8") as f:
        md_lines = f.readlines()
    md_lines = [line.rstrip("\n") for line in md_lines]

    sections = detect_sections(md_lines, source_name)
    chunks = []
    section_hashes = {}

    for section_title, section_lines in sections:
        raw_text = '\n'.join(section_lines)
        cleaned = clean_text(raw_text)
        if not cleaned or len(cleaned.strip()) < 50:
            continue

        s_hash = text_hash(cleaned)
        section_hashes[s_hash] = section_title
        topic = detect_topic(default_topic, section_title, cleaned)
        text_chunks = split_text_into_chunks(cleaned, max_len=1000, min_len=450)

        for i, text_chunk in enumerate(text_chunks):
            chunk_id = make_chunk_id(file_prefix, s_hash, i + 1)
            if len(text_chunks) > 1:
                section_label = f"{section_title}(第{i+1}部分)"
            else:
                section_label = section_title
            keywords = extract_keywords(text_chunk, section_title, topic, source_name)
            chunk = {
                "id": chunk_id,
                "source": md_file,
                "topic": topic,
                "section": section_label,
                "content": text_chunk,
                "keywords": keywords
            }
            chunks.append(chunk)

    # Fallback: if very few chunks, process entire file
    file_total_chars = len(clean_text('\n'.join(md_lines)))
    if len(chunks) == 0 or (file_total_chars > 5000 and len(chunks) < 3):
        full_text = clean_text('\n'.join(md_lines))
        if full_text and len(full_text.strip()) > 50:
            s_hash = text_hash(full_text)
            section_hashes[s_hash] = source_name
            text_chunks = split_text_into_chunks(full_text, max_len=1000, min_len=450)
            chunks = []
            for i, text_chunk in enumerate(text_chunks):
                chunk_id = make_chunk_id(file_prefix, s_hash, i + 1)
                section_label = f"{source_name}(第{i+1}部分)" if len(text_chunks) > 1 else source_name
                keywords = extract_keywords(text_chunk, source_name, default_topic, source_name)
                chunk = {
                    "id": chunk_id,
                    "source": md_file,
                    "topic": default_topic,
                    "section": section_label,
                    "content": text_chunk,
                    "keywords": keywords
                }
                chunks.append(chunk)

    return chunks, section_hashes

# ============================================================
# 9. Incremental update logic
# ============================================================
def load_existing_kb():
    """Load existing knowledge base JSON, or return None if not found."""
    if not os.path.exists(OUTPUT_PATH):
        return None
    with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def build_file_hash_map(existing_kb):
    """Extract file-level hashes from existing KB metadata."""
    if existing_kb and "metadata" in existing_kb:
        return existing_kb["metadata"].get("file_hashes", {})
    return {}

def build_section_hash_index(existing_kb):
    """Build a map: (source_file, section_hash) → list of existing chunks."""
    index = {}
    if not existing_kb or "chunks" not in existing_kb:
        return index
    for chunk in existing_kb["chunks"]:
        # Extract section hash from chunk ID: {prefix}_{hash8}_{seq}
        parts = chunk["id"].split("_")
        if len(parts) >= 3:
            section_hash = parts[1]  # The 8-char hash part
            key = (chunk["source"], section_hash)
            if key not in index:
                index[key] = []
            index[key].append(chunk)
    return index

def incremental_update(force_full=False):
    """Main incremental update function.

    force_full=True: re-process all markdown files (ignore file hashes),
    but still preserve existing image/reference chunks.
    """
    # Always load existing KB to preserve non-markdown chunks (images, XLSX summary, etc.)
    existing_kb = load_existing_kb()
    old_file_hashes = {} if force_full else build_file_hash_map(existing_kb)
    old_section_index = {} if force_full else build_section_hash_index(existing_kb)

    # Stats
    stats = {
        "files_total": 0,
        "files_unchanged": 0,
        "files_changed": 0,
        "files_new": 0,
        "files_missing": 0,
        "sections_unchanged": 0,
        "sections_changed": 0,
        "sections_new": 0,
        "chunks_kept": 0,
        "chunks_rebuilt": 0,
    }

    all_chunks = []
    new_file_hashes = {}
    used_chunk_ids = set()

    # Image/XLSX chunks are always included (they're static, not from markdown)
    preserved_non_md_chunks = list(IMAGE_CHUNKS)
    for c in preserved_non_md_chunks:
        used_chunk_ids.add(c["id"])

    print("=" * 60)
    print("Incremental Knowledge Base Update")
    print("=" * 60)

    for md_file, source_name, default_topic in MD_FILES:
        stats["files_total"] += 1
        md_path = os.path.join(WORK_DIR, md_file)

        if not os.path.exists(md_path):
            print(f"  [MISSING] {md_file}")
            stats["files_missing"] += 1
            continue

        current_hash = file_hash(md_path)
        new_file_hashes[md_file] = current_hash
        file_prefix = make_file_prefix(source_name)

        old_hash = old_file_hashes.get(md_file)

        if old_hash is None:
            # New file
            print(f"  [NEW]     {md_file}")
            stats["files_new"] += 1
            chunks, section_hashes = process_markdown_file(md_file, source_name, default_topic, file_prefix)
            for c in chunks:
                if c["id"] not in used_chunk_ids:
                    all_chunks.append(c)
                    used_chunk_ids.add(c["id"])
            stats["chunks_rebuilt"] += len(chunks)

        elif old_hash == current_hash:
            # File unchanged → keep all existing chunks for this file
            print(f"  [SAME]    {md_file}")
            stats["files_unchanged"] += 1
            kept = 0
            if existing_kb:
                for chunk in existing_kb["chunks"]:
                    if chunk["source"] == md_file and chunk["id"] not in used_chunk_ids:
                        all_chunks.append(chunk)
                        used_chunk_ids.add(chunk["id"])
                        kept += 1
            stats["chunks_kept"] += kept

        else:
            # File changed → section-level diff
            print(f"  [CHANGED] {md_file}")
            stats["files_changed"] += 1
            new_chunks, new_section_hashes = process_markdown_file(
                md_file, source_name, default_topic, file_prefix
            )

            # Check each new section against old section index
            for chunk in new_chunks:
                # Extract section hash from chunk ID
                parts = chunk["id"].split("_")
                section_hash_full = parts[1] if len(parts) >= 3 else ""
                key = (md_file, section_hash_full)

                if key in old_section_index:
                    # Section content unchanged → keep old chunks
                    old_chunks = old_section_index[key]
                    for oc in old_chunks:
                        if oc["id"] not in used_chunk_ids:
                            all_chunks.append(oc)
                            used_chunk_ids.add(oc["id"])
                            stats["chunks_kept"] += 1
                    stats["sections_unchanged"] += 1
                else:
                    # Section changed or new → use new chunk
                    if chunk["id"] not in used_chunk_ids:
                        all_chunks.append(chunk)
                        used_chunk_ids.add(chunk["id"])
                        stats["chunks_rebuilt"] += 1
                    if any(key == (md_file, sh[:8]) for sh in new_section_hashes if sh[:8] == section_hash_full):
                        stats["sections_changed"] += 1
                    else:
                        stats["sections_new"] += 1

    # Preserve non-markdown chunks (images, XLSX summary, etc.)
    all_chunks.extend(preserved_non_md_chunks)

    print(f"\n{'─' * 60}")
    print(f"Files:   {stats['files_total']} total | {stats['files_unchanged']} same | {stats['files_changed']} changed | {stats['files_new']} new | {stats['files_missing']} missing")
    print(f"Chunks:  {stats['chunks_kept']} kept | {stats['chunks_rebuilt']} rebuilt | {len(preserved_non_md_chunks)} preserved (images/refs)")
    print(f"Total chunks: {len(all_chunks)}")

    return all_chunks, new_file_hashes, stats, existing_kb

# ============================================================
# 10. Assemble and write final JSON
# ============================================================
def assemble_and_write(all_chunks, new_file_hashes, existing_kb, force_full=False):
    """Assemble the final knowledge base JSON and write to disk.

    force_full=True: rebuild reference data from source files even if existing KB has it.
    """

    # Load reference data
    # - force_full: always rebuild from source files
    # - incremental: reuse existing reference_data, but check for new keys
    if existing_kb and "reference_data" in existing_kb and not force_full:
        reference_data = existing_kb["reference_data"]
        # Check if new reference data sources need to be added
        # 路径用顶部本地化常量

        if "document_summaries" not in reference_data and os.path.exists(SIMPLIFIED_KB_PATH):
            with open(SIMPLIFIED_KB_PATH, "r") as f:
                simplified_kb = json.load(f)
            doc_summaries = simplified_kb.get("knowledge_base", {}).get("documents", [])
            reference_data["document_summaries"] = {
                "description": f"{len(doc_summaries)}篇攻略文档的内容摘要索引，每篇含id/标题/主题/内容概要(不含详细数值)",
                "source_file": "拂晓攻略知识库_精简版.json",
                "count": len(doc_summaries),
                "structure": "每条: id(文档编号)/title(标题)/topic(主题分类)/summary(内容概要)",
                "data": doc_summaries
            }

        if "document_categories" not in reference_data and os.path.exists(CATEGORIZED_KB_PATH):
            with open(CATEGORIZED_KB_PATH, "r") as f:
                categorized_kb = json.load(f)
            categories = categorized_kb.get("knowledge_base", {}).get("categories", {})
            reference_data["document_categories"] = {
                "description": f"按难度分3大类: 萌新(3-5号文件)/基础玩法(6号)/进阶玩法(7-9号)，每类含对应文档列表",
                "source_file": "拂晓攻略知识库_分类版.json",
                "count": len(categories),
                "structure": "萌新/基础玩法/进阶玩法 → 每类含documents数组(id/title/topic/summary)",
                "data": categories
            }

        if "document_index" not in reference_data and os.path.exists(INDEX_KB_PATH):
            with open(INDEX_KB_PATH, "r") as f:
                index_kb = json.load(f)
            doc_index = index_kb.get("knowledge_base", {}).get("documents", [])
            reference_data["document_index"] = {
                "description": f"{len(doc_index)}篇文档的完整索引，含文件路径/关键词/摘要/来源类型(DOCX/PNG/XLSX)等详细元数据",
                "source_file": "拂晓攻略知识库_索引.json",
                "count": len(doc_index),
                "structure": "每条: id/title/topic/summary/keywords/file_path/source_type等完整元数据",
                "data": doc_index
            }
    else:
        # Load from source files（路径已本地化，见顶部配置；缺源文件时清晰报错）
        for _p, _name in ((RAW_DATA_PATH, "raw_data.json"), (TALENT_MAP_PATH, "talent_map.json"),
                          (TALENT_PVP_PATH, "talent_pvp.json"), (ENEMY_DATA_PATH, "拉古兹.json")):
            if not os.path.exists(_p):
                raise FileNotFoundError("缺少 reference 数据源: " + _name + " -> " + _p +
                                        "\n（增量更新保留旧 reference_data，仅 --full 重建时需要）")
        with open(RAW_DATA_PATH, "r", encoding="utf-8") as f: ship_database = json.load(f)
        with open(TALENT_MAP_PATH, "r", encoding="utf-8") as f: talent_map = json.load(f)
        with open(TALENT_PVP_PATH, "r", encoding="utf-8") as f: talent_pvp = json.load(f)
        with open(ENEMY_DATA_PATH, "r", encoding="utf-8") as f: enemy_data = json.load(f)
        # ship_attack_data：本地从普攻表 xlsx 统计（每舰种船数）；失败返回 {}
        ship_attack_data = {}
        if os.path.exists(SHIP_ATTACK_PATH):
            try:
                import openpyxl
                _wb = openpyxl.load_workbook(SHIP_ATTACK_PATH, read_only=True, data_only=True)
                for _sn in _wb.sheetnames:
                    if _sn in ("目录", "说明", "总览"):
                        continue
                    _ws = _wb[_sn]
                    _cnt = 0
                    for _row in _ws.iter_rows(values_only=True):
                        if _row and any(_c is not None and str(_c).strip() for _c in _row[:2]):
                            _cnt += 1
                    if _cnt:
                        ship_attack_data[_sn] = {"ship_count": _cnt,
                                                 "description": _sn + " " + str(_cnt) + "艘普攻数据",
                                                 "data": []}
                _wb.close()
            except Exception:
                ship_attack_data = {}

        # Load document summaries from simplified JSON
        doc_summaries = []
        if os.path.exists(SIMPLIFIED_KB_PATH):
            with open(SIMPLIFIED_KB_PATH, "r") as f:
                simplified_kb = json.load(f)
            doc_summaries = simplified_kb.get("knowledge_base", {}).get("documents", [])

        # Load categories from categorized JSON
        categories = {}
        if os.path.exists(CATEGORIZED_KB_PATH):
            with open(CATEGORIZED_KB_PATH, "r") as f:
                categorized_kb = json.load(f)
            categories = categorized_kb.get("knowledge_base", {}).get("categories", {})

        # Load detailed index from index JSON
        doc_index = []
        if os.path.exists(INDEX_KB_PATH):
            with open(INDEX_KB_PATH, "r") as f:
                index_kb = json.load(f)
            doc_index = index_kb.get("knowledge_base", {}).get("documents", [])

        ship_count = len(ship_database)
        talent_map_count = len(talent_map)
        talent_pvp_count = len(talent_pvp)
        enemy_count = len(enemy_data.get("encounter_simulation", {}).get("enemies", []))
        ship_attack_count = sum(v["ship_count"] for v in ship_attack_data.values())

        reference_data = {
            "ship_database": {
                "description": f"{ship_count}艘舰灵完整数据，包含面板数值、普攻倍率、技能描述、天赋选择",
                "source_file": "raw_data.json", "count": ship_count,
                "structure": {
                    "panel": "舰名/舰种/所属/稀有/装填/装甲类型/面板数值(血量/炮击/雷击/航空/对空/装甲/命中/机动/索敌/幸运/航速)",
                    "normal_attacks": "普攻列表(倍率multiplier/冷却cd/武器类型weapon_type/弹种ammo_type/伤害类型preset_damage_type/命中次数hit)",
                    "skills": "技能(旗舰技/支援技/通常技1/通常技2/必杀技)",
                    "talents": "4层天赋选择(每层A/B两个选项)"
                },
                "data": ship_database
            },
            "talent_map": {
                "description": f"{talent_map_count}个天赋科技及其数值描述",
                "source_file": "talent_map.json", "count": talent_map_count,
                "structure": "每个天赋包含desc(描述)/v1(一级数值)/v2(二级数值)",
                "data": talent_map
            },
            "talent_pvp": {
                "description": f"{talent_pvp_count}艘舰灵的PVP天赋4位数字加点方案",
                "source_file": "talent_pvp.json", "count": talent_pvp_count,
                "structure": "舰灵名→4位数字天赋代码(如1212)，每位数字对应一层天赋的选择",
                "data": talent_pvp
            },
            "enemy_data": {
                "description": f"{enemy_count}个遭遇战BOSS的面板属性数据，用于伤害模拟计算",
                "source_file": "拉古兹.json", "count": enemy_count,
                "structure": "包含伤害公式参考、弹种补正表、每个BOSS的装甲类型/舰种/体型/面板属性(装甲/对空/回避/幸运)/有效弹种",
                "data": enemy_data
            },
            "ship_attack_data": {
                "description": f"{ship_attack_count}艘舰灵的普攻倍率与CD数据，按舰种分7类",
                "source_file": "8.2 拂晓舰灵普攻倍率cd.xlsx", "count": ship_attack_count,
                "structure": "每个舰灵包含main_attack和secondary_attack(倍率/CD/射程/伤害类型/弹数)",
                "data": ship_attack_data
            },
            "document_summaries": {
                "description": f"{len(doc_summaries)}篇攻略文档的内容摘要索引，每篇含id/标题/主题/内容概要(不含详细数值)",
                "source_file": "拂晓攻略知识库_精简版.json",
                "count": len(doc_summaries),
                "structure": "每条: id(文档编号)/title(标题)/topic(主题分类)/summary(内容概要)",
                "data": doc_summaries
            },
            "document_categories": {
                "description": f"按难度分3大类: 萌新(3-5号文件)/基础玩法(6号)/进阶玩法(7-9号)，每类含对应文档列表",
                "source_file": "拂晓攻略知识库_分类版.json",
                "count": len(categories),
                "structure": "萌新/基础玩法/进阶玩法 → 每类含documents数组(id/title/topic/summary)",
                "data": categories
            },
            "document_index": {
                "description": f"{len(doc_index)}篇文档的完整索引，含文件路径/关键词/摘要/来源类型(DOCX/PNG/XLSX)等详细元数据",
                "source_file": "拂晓攻略知识库_索引.json",
                "count": len(doc_index),
                "structure": "每条: id/title/topic/summary/keywords/file_path/source_type等完整元数据",
                "data": doc_index
            }
        }

    # Load topic_routing and query_patterns
    if existing_kb and "topic_routing" in existing_kb:
        topic_routing = existing_kb["topic_routing"]
        query_patterns = existing_kb["query_patterns"]
    else:
        EXISTING_KB_PATH = SIMPLIFIED_KB_PATH  # 本地化路径（顶部常量）
        with open(EXISTING_KB_PATH, "r", encoding="utf-8") as f:
            existing_kb_simple = json.load(f)
        topic_routing = existing_kb_simple["knowledge_base"]["topic_routing"]
        query_patterns = existing_kb_simple["knowledge_base"]["query_patterns"]

    # Ensure new topics exist
    for t in ["敌人图鉴", "遭遇战攻略", "遭遇战"]:
        if t not in topic_routing:
            topic_routing[t] = []

    total_chunks = len(all_chunks)
    source_files = sorted(set(c["source"] for c in all_chunks))

    # Get reference counts for metadata
    ref_counts = {}
    for key in ["ship_database", "talent_map", "talent_pvp", "enemy_data", "ship_attack_data",
                 "document_summaries", "document_categories", "document_index"]:
        if key in reference_data:
            ref_counts[key] = reference_data[key].get("count", 0)

    # Determine version
    old_version = existing_kb["metadata"]["version"] if existing_kb else "5.0"
    # Auto-increment patch version
    version_parts = old_version.split(".")
    if len(version_parts) >= 2:
        major = version_parts[0]
        minor = int(version_parts[1]) if version_parts[1].isdigit() else 0
        new_version = f"{major}.{minor + 1}"
    else:
        new_version = "5.1"

    knowledge_base = {
        "metadata": {
            "name": "拂晓：胜利之刻 Agent知识库",
            "version": new_version,
            "created": "2026-08-14",
            "source": "凤栖攻略组",
            "description": "供智能体检索的完整知识库，整合全部攻略文档(11篇DOCX攻略+11篇遭遇战文字轴+17张图片/表格OCR)及8组结构化数据引用(舰灵数据库+普攻倍率表+天赋+敌人数据+文档摘要+分类索引+完整索引)。文本按语义段落切分(500-1000字符)，图片一图一chunk，结构化数据作为引用表不切分。支持增量更新。",
            "total_chunks": total_chunks,
            "chunk_strategy": "文本按语义段落切分(500-1000字符)，图片一图一chunk，结构化数据作为引用表不切分。chunk ID格式: {file_hash6}_{section_hash8}_{seq}，未变化的section保持ID不变。",
            "source_files": source_files,
            "file_hashes": new_file_hashes,
            "reference_files": [
                f"raw_data.json({ref_counts.get('ship_database', 0)}艘舰灵)",
                f"talent_map.json({ref_counts.get('talent_map', 0)}天赋)",
                f"talent_pvp.json({ref_counts.get('talent_pvp', 0)}舰PVP天赋)",
                f"拉古兹.json({ref_counts.get('enemy_data', 0)}BOSS)",
                f"ship_attack_data.json({ref_counts.get('ship_attack_data', 0)}舰普攻倍率)",
                f"拂晓攻略知识库_精简版.json({ref_counts.get('document_summaries', 0)}篇文档摘要)",
                f"拂晓攻略知识库_分类版.json({ref_counts.get('document_categories', 0)}大类分类)",
                f"拂晓攻略知识库_索引.json({ref_counts.get('document_index', 0)}篇完整索引)"
            ]
        },
        "chunks": all_chunks,
        "reference_data": reference_data,
        "topic_routing": topic_routing,
        "query_patterns": query_patterns
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(knowledge_base, f, ensure_ascii=False, indent=2)

    print(f"\nJSON written to {OUTPUT_PATH}")
    print(f"Version: {new_version}")
    print(f"Total chunks: {total_chunks}")

    return knowledge_base

# ============================================================
# 11. Main
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Incremental KB builder")
    parser.add_argument("--full", action="store_true", help="Force full rebuild (ignore existing hashes)")
    args = parser.parse_args()

    all_chunks, new_file_hashes, stats, existing_kb = incremental_update(force_full=args.full)
    kb = assemble_and_write(all_chunks, new_file_hashes, existing_kb, force_full=args.full)

    # Verification
    with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
        verified = json.load(f)
    print(f"\n{'=' * 60}")
    print(f"Verification: JSON is valid.")
    print(f"Metadata total_chunks: {verified['metadata']['total_chunks']}")
    print(f"Actual chunk count: {len(verified['chunks'])}")
    print(f"File hashes tracked: {len(verified['metadata'].get('file_hashes', {}))}")

    sizes = [len(c['content']) for c in verified['chunks']]
    print(f"\nChunk size: min={min(sizes)} max={max(sizes)} avg={sum(sizes)//len(sizes)}")
    print(f"  > 1000 chars: {len([s for s in sizes if s > 1000])}")
    print(f"  500-1000:     {len([s for s in sizes if 500 <= s <= 1000])}")
    print(f"  < 500:        {len([s for s in sizes if s < 500])}")
