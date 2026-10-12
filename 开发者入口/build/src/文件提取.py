# -*- coding: utf-8 -*-
"""
文件提取模块

包含三部分：
1. extract_text_from_file：通用 txt/md/json/docx/xlsx 文本提取（含双行表头 xlsx 特殊处理）
2. extract_panel_data：舰灵数值表专用提取（双段双行表头，取 lv100 6星 强化MAX 面板组）
3. extract_normal_attacks：普攻倍率表专用提取（7 个 sheet 按舰种分，3 种列结构）

面板与普攻由代码确定，不走 LLM；仅技能/buff 由 parser.py 调 LLM 解析。
"""
import io
import json
import os
import re

try:
    from .名称归一化 import canonicalize_name
except ImportError:  # 直跑（非包导入）时按同目录模块加载
    from 名称归一化 import canonicalize_name


# ======================================================================
# 通用文本提取（保留原逻辑）
# ======================================================================

def extract_text_from_file(filepath: str):
    """从本地文件提取文本，返回 (content, error_str)。
    支持 txt/md/json/docx/xlsx。"""
    if not os.path.exists(filepath):
        return '', f'文件不存在: {filepath}'
    suffix = filepath.lower().rsplit('.', 1)[-1] if '.' in filepath else ''
    try:
        with open(filepath, 'rb') as f:
            raw = f.read()
    except Exception as e:
        return '', f'读取文件失败: {e}'

    if suffix in ('txt', 'md', 'text', 'markdown', 'json'):
        try:
            return raw.decode('utf-8', errors='ignore'), None
        except Exception as e:
            return '', f'文本解码失败: {e}'
    if suffix == 'docx':
        try:
            from docx import Document
            doc = Document(io.BytesIO(raw))
            return '\n'.join(p.text for p in doc.paragraphs), None
        except Exception as e:
            return '', f'docx 解析失败: {e}'
    if suffix == 'xlsx':
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(raw), data_only=False)
            all_rows = []
            for ws in wb.worksheets:
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    continue
                row0 = list(rows[0])
                row1 = list(rows[1]) if len(rows) > 1 else []
                nonempty0 = sum(1 for v in row0 if v is not None)
                nonempty1 = sum(1 for v in row1 if v is not None)
                # 判断是否双行表头：第0行非空值远少于第1行，且含等级标识
                is_two_row = (nonempty0 > 0 and nonempty0 < nonempty1 / 2
                              and any(any(kw in str(v) for kw in ['lv', '强化MAX', '满强化', '满星满强化']) for v in row0 if v))
                if is_two_row and len(rows) > 2:
                    # 双行表头：第0行是等级组（合并单元格），第1行是字段名，数据从第2行起
                    basic_map = {}
                    for i, v in enumerate(row1):
                        if v is None:
                            continue
                        sv = str(v).strip()
                        if sv in ('舰名', '舰种', '所属', '稀有', '装甲类型', '弹种', '装填'):
                            basic_map[sv] = i
                    # 定位"100级满星满强化"面板组
                    group_cols = [(i, str(v)) for i, v in enumerate(row0) if v is not None]
                    target_start = None
                    target_group = None
                    for i, sv in group_cols:
                        if '强化MAX' in sv or '满强化' in sv or '满星满强化' in sv:
                            target_start = i
                            target_group = sv
                            break
                    if target_start is None and group_cols:
                        for i, sv in reversed(group_cols):
                            if '6星' in sv or 'MAX' in sv or '满' in sv:
                                target_start = i
                                target_group = sv
                                break
                    next_start = len(row0)
                    for i, _ in group_cols:
                        if i > (target_start or 0):
                            next_start = i
                            break
                    panel_field_names = {'血量', '炮击', '雷击', '航空', '对空', '装甲', '命中', '机动', '索敌', '幸运'}
                    panel_fields = []
                    if target_start is not None:
                        for j in range(target_start, next_start):
                            if j < len(row1) and row1[j] is not None and str(row1[j]).strip() in panel_field_names:
                                panel_fields.append((str(row1[j]).strip(), j))
                    for row in rows[2:]:
                        if not row or row[0] is None:
                            continue
                        obj = {}
                        for fname, ci in basic_map.items():
                            if ci < len(row) and row[ci] is not None:
                                obj[fname] = row[ci]
                        if target_group and panel_fields:
                            panel = {}
                            for fname, ci in panel_fields:
                                if ci < len(row) and row[ci] is not None:
                                    panel[fname] = row[ci]
                            if panel:
                                obj[f'面板_{target_group}'] = panel
                        if obj:
                            all_rows.append(obj)
                else:
                    # 单行表头
                    header = [str(c).strip() if c is not None else '' for c in rows[0]]
                    for row in rows[1:]:
                        obj = {}
                        for h, c in zip(header, row):
                            if h and c is not None and str(c).strip():
                                obj[h] = str(c)
                        if obj:
                            all_rows.append(obj)
            if not all_rows:
                return '', 'xlsx 未读取到任何单元格数据，请确认文件为标准 .xlsx 格式'
            return json.dumps(all_rows, ensure_ascii=False, indent=2), None
        except Exception as e:
            return '', f'xlsx 解析失败: {e}'
    if suffix == 'xls':
        return '', '暂不支持 .xls 老格式，请另存为 .xlsx 后重试'
    return '', f'不支持的文件类型: .{suffix}（仅支持 txt/md/json/docx/xlsx）'


# ======================================================================
# 舰灵数值表专用提取（双段双行表头，代码确定面板与基础信息）
# ======================================================================

PANEL_FIELD_NAMES = {'血量', '炮击', '雷击', '航空', '对空', '装甲', '命中', '机动', '索敌', '幸运'}
BASIC_FIELDS = {'舰名', '舰种', '所属', '稀有', '装甲类型', '弹种', '装填'}
# 基础面板字段：等级无关的常量属性，不随 lv/星级/强化 变化，因此只出现在 lv1 分组
# 的列里（强化MAX 组没有该列）。需跨全部分组单独采集，取首个非空值。
BASE_PANEL_FIELDS = {'航速'}

# 国籍标准映射：面板表的国家全称 → 游戏阵营名（2026-08-08 用户定义）
_NATION_TO_CAMP = {
    '美国': '尤奈特', '英国': '洛蒙瑞亚', '中国': '凤栖', '日本': '八咫',
    '德国': '奥鲁加', '意大利': '列加杜', '法国': '纳谢尔', '苏联': '沃尔克',
    '美': '尤奈特', '英': '洛蒙瑞亚', '中': '凤栖', '日': '八咫',
    '德': '奥鲁加', '意': '列加杜', '法': '纳谢尔', '苏': '沃尔克',
}


def _is_level_group_row(row) -> bool:
    """判断该行是否为等级组表头行。
    用严格关键词 'lv'/'强化MAX'/'满强化' 避免误判含'星''级'的舰名（如'列克星敦'）。"""
    nonempty = [v for v in row if v is not None]
    if not nonempty:
        return False
    return any(any(kw in str(v) for kw in ['lv', '强化MAX', '满强化', '满星满强化']) for v in nonempty)


def extract_panel_data(filepath: str):
    """从舰灵数值表提取每个角色的基础信息 + lv100 6星 强化MAX 面板。

    该表为双段双行表头：
    - 第一段 row0=等级组(A1:T1 "lv1 1星 0强化")，row1=字段名(col0-19)，row2+ 数据
    - row178='改造' 分隔行；row179=第二段等级组，row180=第二段字段名，row181+ 改造舰灵
    - 4 个面板组列：col0-19(lv1)、col21-30(lv100 1星)、col33-42(lv100 6星 0强化)、col45-54(lv100 6星 强化MAX，目标)

    返回 dict: {舰名: {'舰种','所属','稀有','装甲类型','弹种','装填','面板':{...}}}
    面板除 10 个随等级变化的属性外，还并入 BASE_PANEL_FIELDS（航速）等常量字段。
    """
    from openpyxl import load_workbook
    wb = load_workbook(filepath, data_only=True)
    ws = wb['舰灵数值'] if '舰灵数值' in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))

    # 扫描所有"表头段"：等级组行 + 紧随其后的字段名行
    segments = []  # [(level_row_idx, field_row_idx, data_start_idx)]
    i = 0
    while i < len(rows):
        if _is_level_group_row(rows[i]):
            if i + 1 < len(rows):
                segments.append((i, i + 1, i + 2))
                i += 2
                continue
        i += 1

    out = {}
    base_vals = {}   # 舰名 → {常量面板字段: 值}（跨分组采集，首个非空值优先）
    for seg_idx, (lvl_i, fld_i, ds) in enumerate(segments):
        # 数据段结束于下一段的 level_row_idx，或 sheet 末尾
        de = segments[seg_idx + 1][0] if seg_idx + 1 < len(segments) else len(rows)
        # 回退掉中间的 '改造' 分隔行
        while de > ds and rows[de - 1] and rows[de - 1][0] == '改造':
            de -= 1

        row0 = list(rows[lvl_i])
        row1 = list(rows[fld_i])

        # 基础字段列索引（舰名/舰种/所属/稀有/装甲类型/弹种/装填）
        basic_map = {str(v).strip(): i for i, v in enumerate(row1)
                     if v is not None and str(v).strip() in BASIC_FIELDS}

        # 定位 lv100 6星 强化MAX 面板组
        group_cols = [(i, str(v)) for i, v in enumerate(row0) if v is not None]
        target_start = None
        for i, sv in group_cols:
            if '强化MAX' in sv or '满强化' in sv or '满星满强化' in sv:
                target_start = i
                break
        if target_start is None:
            # 回退：最后一个含 6星/MAX/满 的组
            for i, sv in reversed(group_cols):
                if '6星' in sv or 'MAX' in sv or '满' in sv:
                    target_start = i
                    break
        if target_start is None:
            continue

        # 该面板组的列范围 [target_start, next_group_col)
        next_start = len(row0)
        for i, _ in group_cols:
            if i > target_start:
                next_start = i
                break
        panel_fields = [(str(row1[j]).strip(), j) for j in range(target_start, next_start)
                        if j < len(row1) and row1[j] is not None and str(row1[j]).strip() in PANEL_FIELD_NAMES]

        # 常量面板字段（航速）：不依赖 target 面板组，单独在本段数据行采集
        base_fields = [(str(row1[j]).strip(), j) for j in range(len(row1))
                       if row1[j] is not None and str(row1[j]).strip() in BASE_PANEL_FIELDS]
        for row in rows[ds:de]:
            if not row or row[0] is None:
                continue
            nm = str(row[0]).strip()
            if not nm or nm == '改造':
                continue
            slot = base_vals.setdefault(nm, {})
            for fname, ci in base_fields:
                if fname in slot or ci >= len(row) or row[ci] is None:
                    continue
                try:
                    slot[fname] = float(row[ci])
                except (ValueError, TypeError):
                    pass

        for row in rows[ds:de]:
            if not row or row[0] is None:
                continue
            name = str(row[0]).strip()
            if not name or name == '改造':
                continue
            if name in out:
                continue  # 第一段优先，同名不覆盖

            obj = {}
            for fname, ci in basic_map.items():
                if ci < len(row) and row[ci] is not None:
                    val = row[ci]
                    if fname == '所属':
                        val = _NATION_TO_CAMP.get(str(val).strip(), val)
                    obj[fname] = val
            if panel_fields:
                panel = {}
                for fname, ci in panel_fields:
                    if ci < len(row) and row[ci] is not None:
                        try:
                            panel[fname] = float(row[ci])
                        except (ValueError, TypeError):
                            panel[fname] = 0.0
                if panel:
                    obj['面板'] = panel
            out[name] = obj

    # 常量面板字段并入（不覆盖已解析到的同名值；仅对已入库的角色生效）
    for nm, vals in base_vals.items():
        obj = out.get(nm)
        if obj is None or not vals:
            continue
        panel = obj.setdefault('面板', {})
        for k, v in vals.items():
            panel.setdefault(k, v)
    return out


# ======================================================================
# 普攻倍率表专用提取（7 个 sheet 按舰种分，代码确定 normal_attacks）
# ======================================================================

# sheet 名 → 舰种前缀（用于 weapon_type 推断）
SHIP_TYPE_BY_SHEET = {
    '驱逐': '驱逐', '轻巡雷巡': '轻巡', '重巡': '重巡',
    '战列战巡重炮': '战列', '轻母': '轻母',
    '航母装母': '航母', '航战水母': '航战',
}


def _parse_hit_count(s) -> int:
    """从 '2发' '15发' '无鱼雷伤害' 解析 hit 数"""
    if s is None:
        return 1
    s = str(s).strip()
    if not s or '无' in s:
        return 0
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else 1


def _parse_ammo_from_damage_type(dmg_type: str) -> str:
    """从 '驱逐穿甲' '轻巡高爆' '战列通常' 提取弹种"""
    if not dmg_type:
        return ''
    for ammo in ('穿甲', '高爆', '通常'):
        if ammo in str(dmg_type):
            return ammo
    return ''


def _parse_weapon_type(dmg_type: str, sheet_name: str) -> str:
    """从伤害类型 + sheet 名推断武器类型"""
    if not dmg_type:
        return ''
    dt = str(dmg_type)
    # 按舰种前缀推断主炮类型
    prefix_map = {'驱逐': '驱逐炮', '轻巡': '轻巡炮', '重巡': '重巡炮', '战列': '战列炮'}
    for prefix, weapon in prefix_map.items():
        if dt.startswith(prefix):
            return weapon
    if '轰炸' in dt:
        return '轰炸机'
    if '鱼雷' in dt:
        return '鱼雷'
    # 按 sheet 名兜底
    ship_prefix = SHIP_TYPE_BY_SHEET.get(sheet_name, '')
    if ship_prefix in prefix_map:
        return prefix_map[ship_prefix]
    return ''


def _to_preset_damage_type(weapon_type: str) -> str:
    """由 weapon_type 推断 preset_damage_type"""
    if weapon_type in ('驱逐炮', '轻巡炮', '重巡炮', '战列炮', '高平炮', '三式弹'):
        return '炮击'
    if weapon_type == '轰炸机':
        return '轰炸机'
    if weapon_type == '鱼雷':
        return '鱼雷'
    if weapon_type == '雷击':
        return '雷击'
    if weapon_type == '近战':
        return '近战'
    return ''


def _to_float(v, default=0.0) -> float:
    """安全转 float"""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _find_header_row(rows) -> int:
    """找表头行：第一个 row[0] == '舰灵名' 的行"""
    for ri, row in enumerate(rows):
        if row and row[0] is not None and str(row[0]).strip() == '舰灵名':
            return ri
    return -1


def _find_col_by_keyword(header, keyword, start=0) -> int:
    """从 start 起，找第一个 header 含 keyword 的列索引"""
    for i in range(start, len(header)):
        if header[i] and keyword in header[i]:
            return i
    return -1


def _build_attack(dmg_type, multiplier, cd, hit, sheet_name) -> dict:
    """构造一条 normal_attack dict"""
    weapon_type = _parse_weapon_type(dmg_type, sheet_name)
    ammo_type = _parse_ammo_from_damage_type(dmg_type)
    return {
        'multiplier': _to_float(multiplier),
        'cd': _to_float(cd),
        'weapon_type': weapon_type,
        'ammo_type': ammo_type,
        'preset_damage_type': _to_preset_damage_type(weapon_type),
        'hit': hit,
    }


def extract_normal_attacks(filepath: str):
    """从普攻倍率表提取每个角色的 normal_attacks 列表。

    7 个 sheet 按舰种分，3 种列结构：
    - 驱逐/轻巡雷巡/重巡：每行 1 角色，炮击段(倍率/cd/伤害类型/炮弹数) + 雷击段(倍率/cd)
    - 战列战巡重炮：每行 1 角色，主炮段 + 副炮段（均炮击）
    - 轻母/航母装母/航战水母：每舰 2 行（轰炸+鱼雷/战列炮+轰炸机），第二行舰灵名 None 时继承上一行

    返回 dict: {舰名: [normal_attack_dict, ...]}
    """
    from openpyxl import load_workbook
    wb = load_workbook(filepath, data_only=True)
    out = {}

    for sheet_name in wb.sheetnames:
        if sheet_name not in SHIP_TYPE_BY_SHEET:
            continue
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        header_idx = _find_header_row(rows)
        if header_idx < 0:
            continue
        header = [str(c).strip() if c is not None else '' for c in rows[header_idx]]

        # 判断 sheet 类型：是否为"航系"（轻母/航母装母/航战水母，多行成对记录）
        is_aviation = sheet_name in ('轻母', '航母装母', '航战水母')

        if is_aviation:
            _extract_aviation_sheet(rows, header_idx, header, sheet_name, out)
        else:
            _extract_gunship_sheet(rows, header_idx, header, sheet_name, out)

    return out


def _extract_gunship_sheet(rows, header_idx, header, sheet_name, out):
    """处理炮击舰 sheet（驱逐/轻巡雷巡/重巡/战列战巡重炮）：每行 1 角色，含炮击段 + 可能的雷击/副炮段"""
    # 定位各段：含'倍率'的列作为每段起始
    # 驱逐: [舰灵名, 炮击倍率, cd, 射程, 伤害类型, 雷击倍率, cd, 射程, 炮弹数]
    # 轻巡/重巡: [舰灵名, 炮击倍率, cd, 射程, 伤害类型, 炮弹数, 雷击倍率, cd, 射程]
    # 战列: [舰灵名, 主炮倍率, cd, 射程, 伤害类型, 炮弹数, 副炮倍率, cd, 射程, 伤害类型, 炮弹数]
    name_col = 0  # 舰灵名固定在 col0

    # 找所有"倍率"列（攻击段起始）
    rate_cols = [i for i, h in enumerate(header) if '倍率' in h]

    for row in rows[header_idx + 1:]:
        if not row or row[0] is None:
            continue
        ship_name = str(row[0]).strip()
        if not ship_name:
            continue

        attacks = []
        # 炮弹数列：找 header 含'炮弹数'的列（驱逐在末尾共享，轻巡/重巡/战列在各段内）
        shell_cols = [i for i, h in enumerate(header) if '炮弹数' in h]

        for seg_idx, rate_col in enumerate(rate_cols):
            # 该段范围 [rate_col, 下一段起始 或 行尾)
            seg_end = rate_cols[seg_idx + 1] if seg_idx + 1 < len(rate_cols) else len(header)
            # 段内找 cd / 伤害类型
            cd_col = _find_col_by_keyword(header, 'cd', rate_col)
            if cd_col >= seg_end:
                cd_col = -1
            dtype_col = _find_col_by_keyword(header, '伤害类型', rate_col)
            if dtype_col >= seg_end:
                dtype_col = -1

            header_lower = header[rate_col]
            multiplier = row[rate_col] if rate_col < len(row) else None
            # 跳过"无鱼雷伤害"等非数值倍率
            if multiplier is None:
                continue
            mult_str = str(multiplier)
            if '无' in mult_str:
                continue
            try:
                float(multiplier)
            except (ValueError, TypeError):
                continue

            cd = row[cd_col] if cd_col >= 0 and cd_col < len(row) else 0.0
            dmg_type = row[dtype_col] if dtype_col >= 0 and dtype_col < len(row) else ''

            # 判断段类型：header 含'雷击' → 雷击段；含'副炮' → 副炮段（炮击）；否则炮击段
            if '雷击' in header_lower:
                weapon_type = '雷击'
                ammo_type = ''
                preset = '雷击'
                # 雷击段伤害类型默认空（雷击无弹种）
                if dmg_type:
                    ammo_type = _parse_ammo_from_damage_type(dmg_type)
            else:
                # 炮击段/副炮段：用伤害类型推断武器
                weapon_type = _parse_weapon_type(dmg_type, sheet_name)
                ammo_type = _parse_ammo_from_damage_type(dmg_type)
                preset = _to_preset_damage_type(weapon_type)

            # hit：取该段内最近的炮弹数列；若无则取共享炮弹数列；否则 1
            hit = 1
            seg_shell_cols = [c for c in shell_cols if rate_col <= c < seg_end]
            if seg_shell_cols:
                hit = _parse_hit_count(row[seg_shell_cols[0]] if seg_shell_cols[0] < len(row) else None)
            elif shell_cols:
                # 驱逐 sheet 炮弹数在末尾共享（雷击段之后）
                shared = shell_cols[-1]
                hit = _parse_hit_count(row[shared] if shared < len(row) else None)

            attacks.append({
                'multiplier': _to_float(multiplier),
                'cd': _to_float(cd),
                'weapon_type': weapon_type,
                'ammo_type': ammo_type,
                'preset_damage_type': preset,
                'hit': hit,
            })

        if attacks:
            if ship_name.endswith('副炮'):
                # 副炮行（如"丹阳副炮"）合并到主角色名下（主/副模组交替）
                base_name = ship_name[:-2].strip()
                out.setdefault(canonicalize_name(base_name), []).extend(attacks)
            else:
                # 条目 key 统一规范化：'丹阳（雪风·改）'与'丹阳副炮'都归到'丹阳'名下，
                # 避免同名（match_key 相同）条目在下游合并时相互覆盖丢失模组
                out[canonicalize_name(ship_name)] = attacks


def _extract_aviation_sheet(rows, header_idx, header, sheet_name, out):
    """处理航系 sheet（轻母/航母装母/航战水母）：每舰 2 行（轰炸+鱼雷），第二行舰灵名 None 继承"""
    # header: [舰灵名, 普攻伤害倍率/%, 普攻cd/s, 有效射程/标准单位, 伤害类型(, 炮弹数/轮)]
    rate_col = _find_col_by_keyword(header, '倍率')
    cd_col = _find_col_by_keyword(header, 'cd')
    dtype_col = _find_col_by_keyword(header, '伤害类型')
    shell_col = _find_col_by_keyword(header, '炮弹数')

    last_ship_name = ''
    for row in rows[header_idx + 1:]:
        if not row:
            continue
        # 舰灵名 None 时继承上一行
        if row[0] is None:
            ship_name = last_ship_name
        else:
            ship_name = str(row[0]).strip()
            last_ship_name = ship_name
        if not ship_name:
            continue

        multiplier = row[rate_col] if rate_col >= 0 and rate_col < len(row) else None
        if multiplier is None:
            continue
        try:
            float(multiplier)
        except (ValueError, TypeError):
            continue

        cd = row[cd_col] if cd_col >= 0 and cd_col < len(row) else 0.0
        dmg_type = row[dtype_col] if dtype_col >= 0 and dtype_col < len(row) else ''
        hit = 1
        if shell_col >= 0 and shell_col < len(row):
            hit = _parse_hit_count(row[shell_col])

        weapon_type = _parse_weapon_type(dmg_type, sheet_name)
        # 条目 key 统一规范化（与炮击舰 sheet 一致，见 _extract_gunship_sheet）
        attacks = out.setdefault(canonicalize_name(ship_name), [])
        attacks.append({
            'multiplier': _to_float(multiplier),
            'cd': _to_float(cd),
            'weapon_type': weapon_type,
            'ammo_type': _parse_ammo_from_damage_type(dmg_type),
            'preset_damage_type': _to_preset_damage_type(weapon_type),
            'hit': hit,
        })


# ======================================================================
# 技能表专用提取（表头行0，数据行 Name 非空即有效）
# ======================================================================

# 技能表需提取的列名（Transform Skill / BattleSkill3 为改造/额外技能，暂不提取）
SKILL_COLUMNS = ('Name', 'LeaderSkill', 'SupportSkill', 'BattleSkill1', 'BattleSkill2')


def extract_skill_rows(filepath: str):
    """从技能表提取每个角色的技能描述行。

    技能表 sheet 'CharSkillInfoLv10'，表头行0：
    CharInfoId/Name/LeaderSkill/Transform Skill/SupportSkill/BattleSkill1/BattleSkill2/BattleSkill3
    数据行 Name 列非空即有效。

    返回 list: [{Name, LeaderSkill, SupportSkill, BattleSkill1, BattleSkill2}, ...]
    """
    from openpyxl import load_workbook
    wb = load_workbook(filepath, data_only=True)
    ws = wb['CharSkillInfoLv10'] if 'CharSkillInfoLv10' in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    header = list(rows[0])
    col_map = {}
    for i, h in enumerate(header):
        if h is None:
            continue
        sh = str(h).strip()
        if sh in SKILL_COLUMNS:
            col_map[sh] = i

    if 'Name' not in col_map:
        return []

    out = []
    for row in rows[1:]:
        if not row:
            continue
        ci_name = col_map['Name']
        if ci_name >= len(row) or row[ci_name] is None:
            continue
        name_val = str(row[ci_name]).strip()
        if not name_val:
            continue
        item = {'Name': name_val}
        for key in ('LeaderSkill', 'SupportSkill', 'BattleSkill1', 'BattleSkill2'):
            ci = col_map.get(key)
            if ci is not None and ci < len(row) and row[ci] is not None:
                # 技能表单元格内换行是字面 "\n"（反斜杠+n 两字符），统一转真实换行
                item[key] = str(row[ci]).strip().replace('\\n', '\n')
            else:
                item[key] = ''
        out.append(item)
    return out


# ======================================================================
# 天赋表专用提取（数值自动化表.xlsx 的「天赋」sheet）
# 四行二列结构：1A/1B、2A/2B、3A/3B、4A/4B（每行两个可选天赋）
# 返回 (talents_by_ship, talent_map)
#   talents_by_ship: {正规舰名: {'1': {'A': 天赋名, 'B': 天赋名}, ...}}
#   talent_map: {天赋名: {'desc': 设定, 'v1': 数值, 'v2': 数值}}（名称字典区）
# ======================================================================

TALENT_ROW_KEYS = ('1', '2', '3', '4')


def extract_talents(filepath: str):
    """从数值自动化表.xlsx 的「天赋」sheet 提取每个角色的天赋（四行二列）。

    天赋 sheet 表头（行0）：
      ID/艦名(CHS)/艦種/所屬/稀有/1A/1B/2A/2B/3A/3B/4A/4B
    名称字典区（行0 第 16 列起）：ID/名称/条件/设定/Value 1/Value 2

    返回 (talents_by_ship, talent_map)：
      - talents_by_ship: {正规舰名: {'1': {'A': ..., 'B': ...}, ..., '4': {...}}}
      - talent_map: {天赋名: {'desc': ..., 'v1': ..., 'v2': ...}}
    """
    try:
        from python_calamine import CalamineWorkbook
    except ImportError:
        return {}, {}
    try:
        from zhconv import convert
    except ImportError:
        convert = None

    wb = CalamineWorkbook.from_path(filepath)
    if '天赋' not in wb.sheet_names:
        return {}, {}
    data = wb.get_sheet_by_name('天赋').to_python()
    if not data:
        return {}, {}

    # 定位角色表头行：首行是标题占位（如“舰灵天赋”），真实表头行首列为 'ID'
    hdr_idx = 0
    for i, r in enumerate(data):
        first = next((c for c in r if c is not None and str(c).strip() != ''), None)
        if first is not None and str(first).strip() == 'ID':
            hdr_idx = i
            break
    hdr = [str(c).strip() if c is not None else '' for c in data[hdr_idx]]

    def _no_space(s):
        # 表格换行会在天赋名中插入内部空格（如“节 能科技”），统一去掉全部空白
        return ''.join(str(s).split())

    def col_idx(name):
        for i, h in enumerate(hdr):
            if h == name:
                return i
        return None

    # 舰名列：表头可能是繁体“艦名(CHS)”或简体“舰名(CHS)”，也兼容裸“舰名”
    name_idx = col_idx('艦名(CHS)') or col_idx('舰名(CHS)') or col_idx('舰名')
    talent_cols = {}
    for rk in TALENT_ROW_KEYS:
        for opt in ('A', 'B'):
            c = col_idx('{}{}'.format(rk, opt))
            if c is not None:
                talent_cols['{}{}'.format(rk, opt)] = c

    # 名称字典区：找第二个 'ID' 列（第一个是角色 ID）
    id_positions = [i for i, h in enumerate(hdr) if h == 'ID']
    dict_start = id_positions[1] if len(id_positions) > 1 else None

    talent_map = {}
    if dict_start is not None:
        d_name = dict_start + 1
        d_desc = dict_start + 3
        d_v1 = dict_start + 4
        d_v2 = dict_start + 5
        for row in data[hdr_idx + 1:]:
            nm = row[d_name] if d_name < len(row) else None
            if nm is None or str(nm).strip() == '':
                continue
            talent_map[_no_space(nm)] = {
                'desc': str(row[d_desc]).strip() if d_desc < len(row) and row[d_desc] else '',
                'v1': row[d_v1] if d_v1 < len(row) else None,
                'v2': row[d_v2] if d_v2 < len(row) else None,
            }

    # 补充映射：右侧“天赋一览”大字典区（表头在标题行：ID/名稱 CHT/條件/Desc ID/設定/
    # Attrib Type 1/Value 1/Attrib Type 2/Value 2/CHS），部分天赋只在该区有定义
    sup_hdr_row = None
    for i in range(max(0, hdr_idx - 1), -1, -1):
        cells = [str(c).strip() if c is not None else '' for c in data[i]]
        if '名稱 CHT' in cells or '名称(CHT)' in cells:
            sup_hdr_row = cells
            break
    if sup_hdr_row is not None:
        def _sup_col(*names):
            for nm in names:
                if nm in sup_hdr_row:
                    return sup_hdr_row.index(nm)
            return None
        s_chs = _sup_col('CHS')
        s_name = _sup_col('名稱 CHT', '名称(CHT)')
        s_desc = _sup_col('設定', '设定')
        s_v1 = _sup_col('Value 1')
        s_v2 = _sup_col('Value 2')
        for row in data[hdr_idx:]:
            if s_chs is not None and s_chs < len(row) and row[s_chs] and str(row[s_chs]).strip():
                key = _no_space(row[s_chs])
            elif s_name is not None and s_name < len(row) and row[s_name] and str(row[s_name]).strip():
                key = _no_space(row[s_name])
            else:
                continue
            if key in talent_map:
                continue
            desc = str(row[s_desc]).strip() if s_desc is not None and s_desc < len(row) and row[s_desc] else ''
            if convert is not None and desc:
                desc = convert(desc, 'zh-cn')
            v1 = row[s_v1] if s_v1 is not None and s_v1 < len(row) else None
            v2 = row[s_v2] if s_v2 is not None and s_v2 < len(row) else None
            # 该区 Value 1 为百分数（如 15.0=15%），统一折算成小数与主字典区语义一致；Value 2 为固定值不折算
            try:
                if v1 is not None and float(v1) >= 1:
                    v1 = float(v1) / 100.0
            except (ValueError, TypeError):
                pass
            talent_map[key] = {'desc': desc, 'v1': v1, 'v2': v2}

    talents_by_ship = {}
    if name_idx is None:
        return talents_by_ship, talent_map
    # 天赋效果文本构建：desc + v1/v2 拼成"对小型舰及中型舰伤害提升 15% 15%"
    def _effect_text(tname):
        info = talent_map.get(tname)
        if not info:
            return ''
        desc = info.get('desc') or ''
        parts = [desc] if desc else []
        for v in (info.get('v1'), info.get('v2')):
            if v in (None, ''):
                continue
            try:
                fv = float(v)
            except (ValueError, TypeError):
                continue
            parts.append('%d%%' % round(fv * 100) if abs(fv) < 1 else ('%d' % fv))
        return ' '.join(parts)

    for row in data[hdr_idx + 1:]:
        nm = row[name_idx] if name_idx < len(row) else None
        if nm is None or str(nm).strip() == '':
            continue
        raw_name = str(nm).strip()
        if convert is not None:
            cname = convert(raw_name, 'zh-cn')
        else:
            cname = raw_name
        talents = {}
        valid = False
        for rk in TALENT_ROW_KEYS:
            a = row[talent_cols['{}A'.format(rk)]] if talent_cols.get('{}A'.format(rk)) is not None and talent_cols['{}A'.format(rk)] < len(row) else None
            b = row[talent_cols['{}B'.format(rk)]] if talent_cols.get('{}B'.format(rk)) is not None and talent_cols['{}B'.format(rk)] < len(row) else None
            a_s = str(a).strip() if a is not None else ''
            b_s = str(b).strip() if b is not None else ''
            # 天赋名繁→简（天赋选项列是繁体，名称字典区是简体，需统一才能映射）
            if convert is not None:
                a_s = convert(a_s, 'zh-cn')
                b_s = convert(b_s, 'zh-cn')
            a_s = _no_space(a_s)
            b_s = _no_space(b_s)
            if a_s or b_s:
                valid = True
            # 结构化存储：name=天赋名（识别/兼容用），effect=具体效果数值文本（展示用）
            talents[rk] = {
                'A': {'name': a_s, 'effect': _effect_text(a_s)} if a_s else {'name': '', 'effect': ''},
                'B': {'name': b_s, 'effect': _effect_text(b_s)} if b_s else {'name': '', 'effect': ''},
            }
        if valid:
            talents_by_ship[cname] = talents

    return talents_by_ship, talent_map


# ======================================================================
# PVP 天赋推荐表提取（知识库/天赋表pvp260719.xlsx）
# 结构：角色行（DISPIMG 图片 + "原名/昵称"）→ 下一行是对应推荐编码
# 返回 {正规舰名: 第一个 4 位推荐编码}（如 1212）
# ======================================================================

_PVP_CODE_RE = re.compile(r'^\d{4}')


def _pvp_extract_names(r):
    """按列序提取角色名（跳过 DISPIMG 与舰种标签列）"""
    names = []
    pending_img = False
    for c in r:
        s = str(c) if c is not None else ''
        if 'DISPIMG' in s:
            pending_img = True
            continue
        if pending_img and s.strip():
            names.append(s.strip())
            pending_img = False
    return names


def _pvp_extract_codes(r):
    """按列序提取推荐编码（'1222（1212）'/'1112/1122'/'1112.0' → 取首个 4 位）"""
    codes = []
    for c in r:
        s = str(c).strip() if c is not None else ''
        if _PVP_CODE_RE.match(s.replace('.0', '')):
            codes.append(s)
    return codes


def extract_talent_pvp(filepath: str):
    """从 PVP 天赋表提取 角色名（原名/昵称）→ 推荐编码列表。
    返回 {表名: [编码, ...]}（表名如 '夕立/夏雨临'，需再归一化）。"""
    try:
        from python_calamine import CalamineWorkbook
    except ImportError:
        return {}
    wb = CalamineWorkbook.from_path(filepath)
    sheet = 'Sheet1' if 'Sheet1' in wb.sheet_names else wb.sheet_names[0]
    rows = wb.get_sheet_by_name(sheet).to_python()

    pairs = {}
    i = 2  # 跳过标题/作者行
    while i < len(rows):
        names = _pvp_extract_names(rows[i])
        if names and i + 1 < len(rows):
            codes = _pvp_extract_codes(rows[i + 1])
            if codes:
                for nm, cd in zip(names, codes):
                    pairs[nm] = pairs.get(nm, []) + [cd]
                i += 2
                continue
        i += 1
    return pairs


# ======================================================================
# PVE 天赋推荐表提取（知识库/底层数据/源文件/天赋表pve*.xlsx）
# 布局与 PVP 表同款：名字行 → 下一行是编码，名字与编码在**同一列**。
# 差异：① 图片列是空串（无 =DISPIMG），故按「下一行同列有编码」判定名字；
#       ② 编码格可能是文本 '改前1211，改后1212'（本体取改前、·改取改后）。
# 返回 {表名: (编码, 改造后编码或 None)}（表名如 '夕立/夏雨临'，需再归一化）。
# ======================================================================

_PVE_CODE_RE = re.compile(r'(\d{4})')


def _parse_pve_code_cell(cell):
    """PvE 编码格 → (编码, 改造后编码或 None)；非编码格返回 None。
    '1122' / 1122.0      → ('1122', None)
    '1111/1112'          → ('1111', None)  多套点法取第一套
    '改前1211，改后1212' → ('1211', '1212')
    """
    if cell is None:
        return None
    s = str(cell).strip()
    if not s:
        return None
    if '改前' in s and '改后' in s:
        codes = _PVE_CODE_RE.findall(s)
        return (codes[0], codes[1]) if len(codes) >= 2 else None
    s = re.sub(r'\.0+$', '', s)          # 数字单元格 1122.0 → 1122
    if _PVP_CODE_RE.match(s):
        return s[:4], None
    return None


def extract_talent_pve(filepath: str):
    """从 PVE 天赋表提取 角色名（原名/昵称）→ (推荐编码, 改造后编码或 None)。
    返回 {表名: (编码, 改后编码或 None)}（表名如 '夕立/夏雨临'，需再归一化）。"""
    try:
        from python_calamine import CalamineWorkbook
    except ImportError:
        return {}
    wb = CalamineWorkbook.from_path(filepath)
    sheet = 'Sheet1' if 'Sheet1' in wb.sheet_names else wb.sheet_names[0]
    rows = wb.get_sheet_by_name(sheet).to_python()

    pairs = {}
    for i in range(len(rows) - 1):
        name_row, code_row = rows[i], rows[i + 1]
        width = max(len(name_row), len(code_row))
        for j in range(width):
            name = name_row[j] if j < len(name_row) else None
            if name is None or not str(name).strip():
                continue
            spec = _parse_pve_code_cell(code_row[j] if j < len(code_row) else None)
            if spec is None:
                continue
            pairs.setdefault(str(name).strip(), spec)
    return pairs


# ======================================================================
# 普攻列表专用提取（数值自动化表.xlsx 的「普攻列表」sheet）
# 基准来源：普攻相关识别以本 sheet 为准（旧表 拂晓舰灵普攻倍率cd1.1.xlsx 已废弃删除）。
# 7 个分节（驱逐舰/轻巡/重巡/战列、战巡/航母、装母/轻母/特殊），每船一行：
#   [ID, 舰名, 舰种, 所属, 稀有,
#    主要普攻, 伤害类型, hit数, 倍率(%), 总倍率(%), 射程, CD(秒), 动作时长,
#    次要普攻, 伤害类型, hit数, 倍率(%), 总倍率(%), 射程, CD(秒), 动作时长]
# 右侧另附「克制修正」武器表（武器类型/武器弹种/伤害类型/对应防御属性/轻甲/中甲/重甲）。
# 注意：openpyxl 对本文件样式解析会崩，必须用同目录 xlsx_std.py（纯标准库）读取。
# ======================================================================

_NORMAL_LIST_SHEET = '普攻列表'


def _load_xlsx_std(filepath: str):
    """加载纯标准库 xlsx 读取器（数值自动化表.xlsx 专用）。"""
    try:
        from .表格读取 import Xlsx
    except ImportError:
        from 表格读取 import Xlsx
    return Xlsx(filepath)


def _parse_dtype_to_weapon_ammo(dtype: str):
    """伤害类型 → (weapon_type, ammo_type)。
    '驱逐穿甲'→('驱逐炮','穿甲')；'战列高爆'→('战列炮','高爆')；
    '雷击'→('雷击','')；'鱼雷'→('鱼雷','')；'轰炸'→('轰炸机','')。

    特殊武器（克制表以「武器||装甲」直接查、不随弹种变）整体识别：
    '高平炮'/'高平炮高爆'→('高平炮','高爆')、'三式弹通常'→('三式弹','通常')、
    '近战'→('近战','') —— 一律不加 '炮' 后缀，否则克制键 ('高平炮炮',…) 查表
    落空回退 1.0。弹种保留但克制仍走 武器||装甲 兜底；点燃表无特殊武器→恒 0。"""
    dt = str(dtype or '').strip()
    if not dt:
        return '', ''
    # 特殊武器优先：拆弹种时不得拼接成 'xx炮'（克制直查 武器||装甲）
    for spec in ('鱼雷机', '高平炮', '三式弹', '近战'):
        if dt == spec:
            return spec, ''
        if dt.startswith(spec) and dt[len(spec):] in ('穿甲', '高爆', '通常', '弹'):
            return spec, dt[len(spec):] if dt[len(spec):] in ('穿甲', '高爆', '通常') else ''
    for ammo in ('穿甲', '高爆', '通常'):
        if ammo in dt:
            gun = dt.replace(ammo, '').strip()
            if gun in ('高平炮', '三式弹', '近战', '雷击', '鱼雷机'):
                return gun, ammo
            return (gun + '炮') if gun else '', ammo
    if '轰炸' in dt:
        return '轰炸机', ''
    if '鱼雷' in dt:
        return '鱼雷', ''
    if '雷击' in dt:
        return '雷击', ''
    return '', ''


def extract_normal_list(filepath: str) -> dict:
    """从数值自动化表.xlsx 的「普攻列表」sheet 提取每角色普攻段（主/副武器）。

    返回 {正规舰名: [segment, ...]}，segment 字段：
      slot             '主武器' | '副武器'
      weapon_code      主要/次要普攻代号（如 DD2 / SDD8 / BB4 / CV2 / DDT1）
      weapon_type      驱逐炮/轻巡炮/重巡炮/战列炮/雷击/鱼雷/轰炸机
      ammo_type        穿甲/高爆/通常（雷击/航系为空）
      multiplier       单发倍率（sheet「倍率（%）」列，0-1 小数）
      hit              发数（sheet「hit数」列）
      volley_multiplier整轮总倍率（sheet「总倍率(%)」列 = 单发 × 发数）
      cd               CD（秒）
      range            射程
      action_time      动作时长（秒）
    表头行按「主要普攻」「次要普攻」动态定位，分节行（驱逐舰/轻巡/…）自动跳过。
    """
    x = _load_xlsx_std(filepath)
    if _NORMAL_LIST_SHEET not in x.sheets:
        return {}
    rows = list(x.rows(_NORMAL_LIST_SHEET))
    if not rows:
        return {}

    # 定位所有表头行（首列 'ID'），并从表头解析两段各列的列号
    hdr_rows = [i for i, r in enumerate(rows)
                if r and any(str(c).strip() == 'ID' for c in r if c is not None)]
    if not hdr_rows:
        return {}

    def _find_col(header, keyword, start, end):
        for i in range(start, min(end, len(header))):
            if header[i] and keyword in str(header[i]):
                return i
        return -1

    seg_cols = []   # [(main_start, main_end, {字段: 列号}), (sub_start, ...)]
    for hi in hdr_rows:
        header = [str(c).strip() if c is not None else '' for c in rows[hi]]
        main_start = next((i for i, h in enumerate(header) if '主要普攻' in h), -1)
        sub_start = next((i for i, h in enumerate(header) if '次要普攻' in h), -1)
        if main_start < 0:
            continue
        seg_end = sub_start if sub_start > main_start else len(header)
        blocks = []
        for st, en, slot in ((main_start, seg_end, '主武器'),
                             (sub_start, len(header), '副武器')):
            if st < 0:
                continue
            cols = {
                'weapon_code': st,
                'dtype': _find_col(header, '伤害类型', st, en),
                'hit': _find_col(header, 'hit', st, en),
                'rate': _find_col(header, '倍率', st, en),
                'volley': _find_col(header, '总倍率', st, en),
                'cd': _find_col(header, 'CD', st, en),
                'range': _find_col(header, '射程', st, en),
                'action': _find_col(header, '动作时长', st, en),
            }
            blocks.append((st, en, slot, cols))
        seg_cols.append((hi, set(range(main_start, seg_end) if sub_start < 0 else range(main_start, len(header))), blocks))

    out = {}
    hdr_set = set(hdr_rows)
    # 每行数据只归属于其上方最近的表头行
    import bisect
    for ri, row in enumerate(rows):
        if ri in hdr_set or not row:
            continue
        first = row[0]
        # 数据行：首列为数字 ID；分节标题行（驱逐舰/轻巡…）与空行跳过
        if first is None or not str(first).strip().replace('.', '').isdigit():
            continue
        # 找上方最近表头
        hi = hdr_rows[bisect.bisect_right(hdr_rows, ri) - 1]
        name = str(row[1]).strip() if len(row) > 1 and row[1] else ''
        if not name:
            continue
        key = canonicalize_name(name)
        segments = []
        for _hdr_i, _skip, blocks in seg_cols:
            if _hdr_i != hi:
                continue
            for st, en, slot, cols in blocks:
                code = row[cols['weapon_code']] if cols['weapon_code'] < len(row) else None
                if code is None or not str(code).strip():
                    continue  # 无该段（如轻母仅轰炸）
                dtype = row[cols['dtype']] if cols['dtype'] >= 0 and cols['dtype'] < len(row) else ''
                wt, ammo = _parse_dtype_to_weapon_ammo(dtype)
                seg = {
                    'slot': slot,
                    'weapon_code': str(code).strip(),
                    'weapon_type': wt,
                    'ammo_type': ammo,
                }
                for field, ci in (('hit', cols['hit']), ('multiplier', cols['rate']),
                                  ('volley_multiplier', cols['volley']), ('cd', cols['cd']),
                                  ('range', cols['range']), ('action_time', cols['action'])):
                    v = row[ci] if ci >= 0 and ci < len(row) else None
                    try:
                        seg[field] = float(v) if v is not None else 0.0
                    except (ValueError, TypeError):
                        seg[field] = 0.0
                segments.append(seg)
            break
        if segments:
            # 改造前后同源（胡德·改 落到 胡德 基准）；同名行追加（不应出现）
            out.setdefault(key, []).extend(segments)
    return out


def extract_counter_table(filepath: str) -> dict:
    """从「普攻列表」右侧武器克制修正表提取：{弹种标签: {轻甲/中甲/重甲: 系数}}。

    表结构（锚=「武器类型」列）：
      表头: 武器类型 | 武器弹种 | 伤害类型 | 对应防御属性 | 轻甲 | 中甲 | 重甲
      数据: 驱逐炮    | 驱逐通常 | 炮击/轻型炮 | 装甲 | 1.1 | 0.9 | 0.3
            （续行）  | 驱逐穿甲 | …       | 装甲 | 1   | 1.1 | 0.15
            雷击      |（空）   | 雷击      | 装甲 | 0.7 | 0.9 | 1.4
    键：炮种取「武器弹种」列（驱逐通常/驱逐穿甲/…），雷击/鱼雷机/轰炸机/高平炮/
    三式弹/近战 的标签在「武器类型」列（武器弹种列为空）。"""
    x = _load_xlsx_std(filepath)
    if _NORMAL_LIST_SHEET not in x.sheets:
        return {}
    rows = list(x.rows(_NORMAL_LIST_SHEET))
    out = {}
    anchor = None
    for r in rows:
        if anchor is None:
            anchor = next((i for i, c in enumerate(r or [])
                           if c is not None and '武器类型' in str(c)), None)
            continue
        if anchor is None or not r:
            continue
        label = ''
        for ci in (anchor + 1, anchor):   # 先「武器弹种」列，空则取「武器类型」列
            if ci < len(r) and r[ci] is not None and str(r[ci]).strip():
                label = str(r[ci]).strip()
                break
        if not label:
            continue
        nums = []
        for k in (4, 5, 6):               # 轻甲/中甲/重甲 = 锚+4..6
            ci = anchor + k
            try:
                nums.append(float(r[ci]) if ci < len(r) and r[ci] is not None else None)
            except (ValueError, TypeError):
                nums.append(None)
        if all(v is not None for v in nums):
            out[label] = {'轻甲': nums[0], '中甲': nums[1], '重甲': nums[2]}
    return out


def extract_combo_normal(filepath: str) -> dict:
    """从数值自动化表.xlsx「普攻列表」提取普攻段 → raw_data.json/data.json 兼容结构。

    与 extract_normal_list 的差异：段结构对齐计算表（含 preset_damage_type /
    slot / weapon_code / volley_multiplier / range），multiplier 存单发倍率、
    hit 存发数（每轮 = 单发 × 发数 = 总倍率，基准口径，普攻数值以「拂晓舰灵普攻倍率cd1.1.xlsx」为准）。
    """
    base = extract_normal_list(filepath)
    out = {}
    for key, segs in base.items():
        segs_new = []
        for s in segs:
            rate = float(s.get('multiplier') or 0.0)
            volley = float(s.get('volley_multiplier') or 0.0) or rate * float(s.get('hit') or 1)
            segs_new.append({
                'slot': s['slot'],
                'weapon_code': s['weapon_code'],
                'weapon_type': s['weapon_type'],
                'ammo_type': s['ammo_type'],
                'multiplier': rate,
                'volley_multiplier': round(volley, 6),
                'hit': int(s.get('hit') or 1),
                'cd': float(s.get('cd') or 0.0),
                'range': float(s.get('range') or 0.0),
                'preset_damage_type': _to_preset_damage_type(s['weapon_type']),
            })
        out[key] = segs_new
    return out
