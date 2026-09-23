# -*- coding: utf-8 -*-
"""
原始数据存取模块

读写 Stage 1 产物 raw_data.json。结构与 data.json 不同：
- data.json: {舰名: CombatData.to_dict()}
- raw_data.json: {正规舰名: {"panel": {...}, "normal_attacks": [...], "skills": {Name, LeaderSkill, ...}}}
"""
import json
import os

from .名称归一化 import match_key


class RawDataStore:
    def __init__(self, output_path: str):
        self.output_path = output_path
        self.storage: dict = {}
        self._key_index: dict = {}
        self.load()

    def _rebuild_index(self):
        """重建 match_key 索引（load/upsert 后调用）"""
        self._key_index = {match_key(n): n for n in self.storage if n}

    def load(self):
        """从文件加载（文件不存在时初始化为空）"""
        if not os.path.exists(self.output_path):
            self.storage = {}
            self._rebuild_index()
            return
        try:
            with open(self.output_path, "r", encoding="utf-8") as f:
                self.storage = json.load(f)
        except Exception:
            self.storage = {}
        self._rebuild_index()

    def save(self):
        """写入文件"""
        parent = os.path.dirname(self.output_path)
        if parent and not os.path.exists(parent):
            raise RuntimeError(f"输出路径父目录不存在: {parent}")
        with open(self.output_path, "w", encoding="utf-8") as f:
            json.dump(self.storage, f, ensure_ascii=False, indent=2)

    def get(self, name: str) -> dict:
        """按正规名精确获取"""
        return self.storage.get(name)

    def has(self, name: str) -> bool:
        """按 match_key 判断是否已存在（增量合并跳过判断用）"""
        return match_key(name) in self._key_index

    def find_by_match_key(self, name: str):
        """按 match_key 模糊定位正规名，未命中返回 None（控制台查询/更新用）"""
        return self._key_index.get(match_key(name))

    def upsert(self, name: str, raw: dict):
        """新增或更新角色（同步刷新索引）"""
        self.storage[name] = raw
        self._key_index[match_key(name)] = name

    def prune(self, keep_names: set) -> int:
        """删除正规名不在 keep_names 里的角色，返回删除数。
        以面板表为基准清理历史残留用。
        按正规名精确比对：存储键与 keep_names 均为 canonicalize_name 产物，
        不能用 match_key 比对（match_key 会转小写，'拉菲(dd-459)' != '拉菲(DD-459)'
        会把带编号括号/大写的正常角色误删）。"""
        names_to_remove = [n for n in self.storage if n not in keep_names]
        for n in names_to_remove:
            del self.storage[n]
        self._rebuild_index()
        return len(names_to_remove)
