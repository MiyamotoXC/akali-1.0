# -*- coding: utf-8 -*-
"""
表格文件分析插件（qwen_agent BaseTool）：原始表格 → 项目统一格式 JSON

用途：统一处理策划/攻略原始表格文档（xlsx/csv），输出结构化 JSON
（中文键：表头 / 数据行数 / 列统计 / 前 N 行数据），供 LLM 阅读与
后续入库处理。项目内数据信息读取一律使用处理好的 JSON 格式，
本工具是"原始表格 → JSON"的统一入口。

依赖：xlsx 用 openpyxl（运行时已有），csv 用标准库，不引入 pandas。
"""
import os
import csv
import json
import datetime

from qwen_agent.tools.base import BaseTool


def _read_xlsx(path, sheet_name=None):
    """读 xlsx，返回 [(sheet名, 行数组)]；sheet_name 指定单个工作表。"""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        names = [sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.sheetnames
        sheets = []
        for nm in names:
            rows = []
            for row in wb[nm].iter_rows(values_only=True):
                rows.append(['' if v is None else v for v in row])
            sheets.append((nm, rows))
        return sheets
    finally:
        wb.close()


def _read_csv(path):
    """读 csv，依次尝试 utf-8 / gbk 编码，返回 [(文件名, 行数组)]。"""
    for enc in ('utf-8-sig', 'gbk'):
        try:
            with open(path, 'r', encoding=enc, newline='') as f:
                rows = [r for r in csv.reader(f)]
            return [(os.path.splitext(os.path.basename(path))[0], rows)]
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError('无法识别 CSV 编码（已尝试 utf-8 与 gbk）')


def _cell_str(v):
    """单元格值统一转字符串（日期转 ISO 格式，整值浮点去 .0）。"""
    if isinstance(v, datetime.datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def _build_sheet_json(name, rows, max_rows):
    """行数组 → 项目格式 JSON（中文键）。第一行为表头，自动剔除全空行。"""
    rows = [r for r in rows if any(_cell_str(c).strip() for c in r)]
    if not rows:
        return {'名称': name, '表头': [], '数据行数': 0, '数据': []}
    header = [_cell_str(c).strip() for c in rows[0]]
    data = rows[1:]
    # 列统计：数值列输出 最小/最大/均值（大半行为数值才视为数值列）
    stats = {}
    for ci, h in enumerate(header):
        if not h:
            continue
        vals = []
        for r in data:
            if ci < len(r):
                try:
                    vals.append(float(_cell_str(r[ci]).strip()))
                except ValueError:
                    pass
        if vals and len(vals) >= len(data) * 0.6:
            stats[h] = {'类型': '数值', '最小': min(vals), '最大': max(vals),
                        '均值': round(sum(vals) / len(vals), 2)}
    records = []
    for r in data[:max_rows]:
        rec = {}
        for ci, h in enumerate(header):
            if h and ci < len(r):
                rec[h] = _cell_str(r[ci])
        records.append(rec)
    out = {'名称': name, '表头': header, '数据行数': len(data), '列统计': stats,
           '数据': records}
    if len(data) > max_rows:
        out['说明'] = f'仅展示前 {max_rows} 行（共 {len(data)} 行），如需更多请分页读取'
    return out


class ExcelAnalyzer(BaseTool):
    """【表格处理】读取 xlsx/csv 原始文档，输出项目统一格式的 JSON
    （中文键：表头/数据行数/列统计/前 N 行数据），供整理入库与分析。"""
    name = 'excel_analyzer'
    description = ('【表格处理】读取 xlsx/csv 原始表格文档并输出项目统一格式的 JSON'
                   '（中文键：工作表名、表头、数据行数、数值列统计、前 N 行数据）。'
                   '用户要求把表格/Excel 转成 JSON、分析原始文档、或将表格信息整理入库时调用。')
    parameters = {
        'type': 'object',
        'properties': {
            'file_path': {'type': 'string', 'description': '表格文件路径（.xlsx/.csv）'},
            'sheet_name': {'type': 'string', 'description': '工作表名（可空=全部 sheet；csv 忽略此参数）'},
            'max_rows': {'type': 'number', 'description': '返回的数据行数上限（默认 50，最大 500）'},
        },
        'required': ['file_path'],
    }

    def call(self, params, **kwargs):
        params = self._verify_json_format_args(params)
        path = str(params.get('file_path') or '').strip()
        if not path or not os.path.isfile(path):
            return f'文件不存在：{path}'
        ext = os.path.splitext(path)[1].lower()
        if ext == '.csv':
            sheets = _read_csv(path)
        elif ext in ('.xlsx', '.xlsm'):
            sheets = _read_xlsx(path, str(params.get('sheet_name') or '').strip() or None)
        else:
            return '暂只支持 .xlsx/.csv 表格文件（.xls 旧格式请先另存为 .xlsx）。'
        try:
            max_rows = int(float(params.get('max_rows') or 50))
        except (ValueError, TypeError):
            max_rows = 50
        max_rows = max(1, min(max_rows, 500))
        result = {'文件': os.path.basename(path), 'sheet数': len(sheets),
                  'sheets': [_build_sheet_json(nm, rows, max_rows) for nm, rows in sheets]}
        return json.dumps(result, ensure_ascii=False, indent=2)
