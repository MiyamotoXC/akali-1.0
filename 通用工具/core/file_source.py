# -*- coding: utf-8 -*-
"""
统一文件访问层（本地 / 云文档 双后端，环境变量切换）
================================================================

背景
----
阿卡莉目前所有知识库/数据/文档都是直接扫本地磁盘（各种 os.path.join(KB_DIR, ...)）。
本模块把「读文件」这一件事收口成一个入口：业务代码只认**逻辑 key**，
底层是读本地还是拉云文档，由环境变量决定，业务代码零改动。

关于「云文档能不能只读一个文件夹链接」
--------------------------------------
能。已核实两条云的文件夹枚举能力：

* 腾讯文档（docs.qq.com）：支持按文件夹列出内容
  —— 开放接口 `list_folder` / MCP 的 `manage.folder_list` / SDK 的 `list_folder_contents`
  都能拿到「某文件夹下的文件与子文件夹」，再用 `get_content`（或导出）取单文件内容。
  所以**攻略合集只要给一个文件夹链接/文件夹 ID 即可**，不用配 28 个单独链接。
* WorkBuddy 资料库（workbuddy.cn/space）：`manage` 支持按父节点拉一层子节点
  （不传父节点则取空间根目录），kind=drive 为网盘文件；同样可以「一个目录链接」展开。

因此下面 `FX_CLOUD_DIR_*` 允许直接填**文件夹链接**，模块会展开该目录并按文件名匹配；
只有个别文件需要单独指定时，才用 `FX_CLOUD_FILE_URLS` 覆盖。

环境变量
--------
| 变量 | 默认 | 说明 |
|---|---|---|
| FX_FILE_BACKEND | local | `local`=本地磁盘；`cloud`=云文档 |
| FX_PROJECT_ROOT | 本文件往上 2 级（阿卡莉1.0） | 本地根，所有相对路径基于它 |
| FX_CLOUD_PROVIDER | tencent_docs | `tencent_docs` / `http` / `workbuddy` |
| FX_CLOUD_DIR_LLM | （空） | 知识库/llm 目录的云链接（攻略QA库、agent知识库、system_prompt…） |
| FX_CLOUD_DIR_GUIDE | （空） | 知识库/攻略 目录的云链接（攻略 docx 合集、guide_index、精简版库） |
| FX_CLOUD_DIR_DATA_PRODUCT | （空） | 底层数据/产物 目录（data/raw_data/talent_map/talent_pvp） |
| FX_CLOUD_DIR_DATA_SOURCE | （空） | 底层数据/源文件 目录（ship_nicknames/mappings/拉古兹/原型舰历史/xlsx） |
| FX_CLOUD_DIR_GUIDE_MD | （空） | 攻略_md 目录（构建源 md） |
| FX_CLOUD_DIR_ACTIVITY | （空） | 活动 目录（活动日历） |
| FX_CLOUD_DIR_APP | （空） | 用户端特用 目录（user_data） |
| FX_CLOUD_FILE_URLS | （空） | JSON，单文件级覆盖：{"agent_kb": "https://...", ...} |
| FX_CLOUD_TOKEN | （空） | 云访问令牌 |
| FX_CLOUD_CLIENT_ID / FX_CLOUD_CLIENT_SECRET | （空） | 腾讯文档 OAuth 凭据（换 token 用） |
| FX_CLOUD_TOKEN_URL / FX_CLOUD_API_BASE | （空） | 腾讯文档接口地址（按开放平台文档填） |
| FX_CLOUD_CACHE_DIR | <root>/.cache/file_source | 云端文件落地缓存目录 |
| FX_CLOUD_CACHE_TTL | 300 | 缓存有效期（秒），0=每次都回源校验 |

用法
----
    from core.file_source import fs

    text = fs.read_text("agent_kb")
    data = fs.read_json("data")
    p    = fs.path("guide_qa_vec")      # 需要真实路径的库（np.load/openpyxl）用这个
    docs = fs.list_group("guide")       # 列出攻略 docx 合集

命令行自检（列出每个 key 的解析结果与存在性）：
    python core/file_source.py --check
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = ["fs", "FileSource", "Resource", "RESOURCES", "ConfigError", "CloudNotConfigured"]


# ---------------------------------------------------------------------------
# 资源登记表：逻辑 key -> (相对路径, 分组, 是否二进制)
# 分组决定云端用哪个「文件夹链接」去解析。
# ---------------------------------------------------------------------------
_B = True  # binary
_T = False  # text

RESOURCES: Dict[str, Tuple[str, str, bool]] = {
    # ---- 知识库/llm：向量库 + agent 总知识库 + 人设 ----
    "guide_qa":        ("知识库/llm/攻略QA库.json",        "llm",   _T),
    "guide_qa_vec":    ("知识库/llm/攻略QA向量库.npz",      "llm",   _B),
    "agent_kb":        ("知识库/llm/拂晓知识库_agent.json", "llm",   _T),
    "system_prompt":   ("知识库/llm/system_prompt.md",      "llm",   _T),

    # ---- 知识库/攻略：攻略文档清单 + 可下载 docx 合集 ----
    "guide_index":     ("知识库/攻略/guide_index.json",              "guide", _T),
    "guide_kb":        ("知识库/攻略/拂晓攻略知识库_精简版.json",      "guide", _T),

    # ---- 知识库/底层数据/产物：运行时数据 ----
    "data":            ("知识库/底层数据/产物/data.json",        "data_product", _T),
    "raw_data":        ("知识库/底层数据/产物/raw_data.json",    "data_product", _T),
    "talent_map":      ("知识库/底层数据/产物/talent_map.json",  "data_product", _T),
    "talent_pvp":      ("知识库/底层数据/产物/talent_pvp.json",  "data_product", _T),

    # ---- 知识库/底层数据/源文件：构建源 + 映射表 ----
    "ship_nicknames":  ("知识库/底层数据/源文件/ship_nicknames.json", "data_source", _T),
    "boss_nicknames":  ("知识库/底层数据/源文件/boss_nicknames.json", "data_source", _T),
    "mappings":        ("知识库/底层数据/源文件/mappings.json",       "data_source", _T),
    "boss_laguz":      ("知识库/底层数据/源文件/拉古兹.json",         "data_source", _T),
    "prototype_hist":  ("知识库/底层数据/源文件/原型舰历史.json",     "data_source", _T),
    "equipment_xlsx":  ("知识库/底层数据/源文件/装备.xlsx",           "data_source", _B),
    "ship_values_xlsx": ("知识库/底层数据/源文件/舰灵数值（整理版）.xlsx", "data_source", _B),

    # ---- 其他 ----
    # 游戏内活动 = 表格（实时读取、专用）；法定节假日（真实日历）= 独立 JSON，二者严格区分
    "activity_calendar": ("知识库/活动/活动日历.xlsx", "activity", _B),
    "holiday_calendar": ("知识库/活动/节假日.json", "activity", _T),
    "user_data":         ("用户端特用/user_data.json", "app",      _T),
    "user_ships":        ("user_ships.json",           "app",      _T),
}

# 分组 -> 云目录环境变量名（整体目录，填文件夹链接即可）
GROUP_ENV: Dict[str, str] = {
    "llm":          "FX_CLOUD_DIR_LLM",
    "guide":        "FX_CLOUD_DIR_GUIDE",
    "data_product": "FX_CLOUD_DIR_DATA_PRODUCT",
    "data_source":  "FX_CLOUD_DIR_DATA_SOURCE",
    "guide_md":     "FX_CLOUD_DIR_GUIDE_MD",
    "activity":     "FX_CLOUD_DIR_ACTIVITY",
    "app":          "FX_CLOUD_DIR_APP",
}

# 分组 -> 本地目录（用于 list_group 列目录，如攻略 docx 合集、攻略_md）
GROUP_LOCAL_DIR: Dict[str, str] = {
    "llm":          "知识库/llm",
    "guide":        "知识库/攻略",
    "data_product": "知识库/底层数据/产物",
    "data_source":  "知识库/底层数据/源文件",
    "guide_md":     "知识库/攻略_md",
    "activity":     "知识库/活动",
    "app":          "用户端特用",
}

_TEXT_EXT = {".json", ".md", ".txt", ".csv", ".yml", ".yaml"}

# 可选资源：user_ships.json 仅在用户保存自定义角色后生成，缺失不算自检失败。
OPTIONAL_RESOURCES = {"user_ships"}


class ConfigError(Exception):
    """配置错误（如未知 key）。"""


class CloudNotConfigured(Exception):
    """云后端已选但未配置对应目录/凭据。"""


class Resource:
    __slots__ = ("key", "rel", "group", "binary")

    def __init__(self, key: str, rel: str, group: str, binary: bool):
        self.key, self.rel, self.group, self.binary = key, rel, group, binary

    @property
    def name(self) -> str:
        return os.path.basename(self.rel)

    def __repr__(self) -> str:
        return f"<Resource {self.key} -> {self.rel}>"


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def _load_dotenv_if_any(root: str) -> None:
    """可选：项目根有 .env 就并入环境变量（不覆盖已存在的），无第三方依赖。"""
    p = os.path.join(root, ".env")
    if not os.path.isfile(p):
        return
    try:
        for line in open(p, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 本地后端
# ---------------------------------------------------------------------------
class LocalBackend:
    name = "local"

    def __init__(self, root: str):
        self.root = root

    def abspath(self, rel: str) -> str:
        return os.path.normpath(os.path.join(self.root, rel))

    def read_bytes(self, rel: str) -> bytes:
        with open(self.abspath(rel), "rb") as f:
            return f.read()

    def exists(self, rel: str) -> bool:
        return os.path.isfile(self.abspath(rel))

    def mtime(self, rel: str) -> float:
        try:
            return os.path.getmtime(self.abspath(rel))
        except OSError:
            return 0.0

    def list_dir(self, rel_dir: str, recursive: bool = False) -> List[str]:
        """列目录。recursive=True 时递归返回相对该目录的相对路径（posix 风格）。

        攻略 docx 在本地是嵌套存放的（攻略/2 游戏简介/2 游戏简介.docx），
        要枚举整个「攻略合集」必须递归。
        """
        d = self.abspath(rel_dir)
        if not os.path.isdir(d):
            return []
        if not recursive:
            return sorted(os.listdir(d))
        out = []
        for dp, _dn, fn in os.walk(d):
            for f in fn:
                out.append(os.path.relpath(os.path.join(dp, f), d).replace("\\", "/"))
        return sorted(out)


# ---------------------------------------------------------------------------
# 云后端：目录列举 + 下载（带本地缓存）
# ---------------------------------------------------------------------------
class CloudBackend:
    """云后端。

    目录解析优先级：
      1) FX_CLOUD_FILE_URLS 里的单文件直链；
      2) 该资源所属分组的 FX_CLOUD_DIR_* 文件夹链接 -> 展开目录按文件名匹配。
    """

    name = "cloud"

    def __init__(self, root: str, driver):
        self.root = root
        self.driver = driver
        self.cache_dir = _env("FX_CLOUD_CACHE_DIR", os.path.join(root, ".cache", "file_source"))
        self.ttl = int(_env("FX_CLOUD_CACHE_TTL", "300") or 300)
        self._file_urls: Dict[str, str] = {}
        raw = _env("FX_CLOUD_FILE_URLS")
        if raw:
            try:
                self._file_urls = {str(k): str(v) for k, v in json.loads(raw).items()}
            except Exception as e:  # 配置写错要立刻暴露，不要静默
                raise ConfigError(f"FX_CLOUD_FILE_URLS 不是合法 JSON: {e}")
        self._dir_index: Dict[str, Dict[str, str]] = {}  # group -> {文件名: url}

    # ---- 解析 ----
    def dir_url(self, group: str) -> str:
        env_name = GROUP_ENV.get(group)
        if not env_name:
            raise CloudNotConfigured(f"分组 {group} 没有对应的云目录环境变量")
        url = _env(env_name)
        if not url:
            raise CloudNotConfigured(
                f"云模式：{env_name} 未配置（分组 {group}）。"
                f"该项填**文件夹链接**即可，模块会自动展开目录按文件名匹配。"
            )
        return url

    def resolve_url(self, res: Resource) -> str:
        if res.key in self._file_urls:
            return self._file_urls[res.key]
        group = res.group
        if group not in self._dir_index:
            self._dir_index[group] = self.driver.list_dir(self.dir_url(group))
        idx = self._dir_index[group]
        if res.name in idx:
            return idx[res.name]
        # 允许云端用相对路径做 key（有的目录列表会带子路径）
        for k, v in idx.items():
            if k.replace("\\", "/").endswith(res.name):
                return v
        raise CloudNotConfigured(
            f"云端目录（{GROUP_ENV.get(group)}）里找不到文件 {res.name}。"
            f"可在 FX_CLOUD_FILE_URLS 中单独指定：{{\"{res.key}\": \"<直链>\"}}"
        )

    # ---- 缓存 ----
    def _cache_path(self, url: str) -> str:
        import hashlib
        h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.cache_dir, h + "_" + os.path.basename(urllib.parse.urlparse(url).path))

    def fetch(self, url: str) -> bytes:
        """带缓存地取远端内容。"""
        cp = self._cache_path(url)
        if os.path.isfile(cp):
            age = time.time() - os.path.getmtime(cp)
            if self.ttl <= 0 or age < self.ttl:
                with open(cp, "rb") as f:
                    return f.read()
        data = self.driver.download(url)
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = cp + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cp)
        return data

    # ---- 对外同构接口 ----
    def read_bytes(self, res_or_rel):
        url = self.resolve_url(res_or_rel)
        return self.fetch(url)

    def exists(self, res: Resource) -> bool:
        try:
            self.resolve_url(res)
            return True
        except Exception:
            return False

    def mtime(self, res: Resource) -> float:
        """云端无可靠 mtime 语义：返回缓存文件时间（未下载则 0）。"""
        url = self.resolve_url(res)
        cp = self._cache_path(url)
        return os.path.getmtime(cp) if os.path.isfile(cp) else 0.0

    def list_dir(self, group: str, recursive: bool = False) -> List[str]:
        return sorted(self.driver.list_dir(self.dir_url(group), recursive=recursive).keys())

    def path(self, res: Resource) -> str:
        """物化到本地缓存并返回真实路径（给 np.load / openpyxl 这类必须吃路径的库）。"""
        url = self.resolve_url(res)
        self.fetch(url)  # 确保已落地
        return self._cache_path(url)


# ---------------------------------------------------------------------------
# 云驱动
# ---------------------------------------------------------------------------
class BaseCloudDriver:
    def list_dir(self, folder_url: str, recursive: bool = False) -> Dict[str, str]:
        raise NotImplementedError

    def download(self, url: str) -> bytes:
        raise NotImplementedError


class HttpFolderDriver(BaseCloudDriver):
    """通用 HTTP 目录驱动：适用于任何能给出目录列表的网盘/静态托管。

    支持两种目录响应：
      * JSON：[{"name": "...", "url": "..."}] 或 {"name": "url"}；
      * HTML：解析 <a href="..."> 链接。
    """

    def __init__(self):
        self.timeout = float(_env("FX_CLOUD_TIMEOUT", "20") or 20)

    def _get(self, url: str, accept: str = "*/*") -> Tuple[bytes, str]:
        req = urllib.request.Request(url, headers={
            "Accept": accept,
            "Authorization": f"Bearer {_env('FX_CLOUD_TOKEN')}" if _env("FX_CLOUD_TOKEN") else "",
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read(), (r.headers.get("Content-Type") or "")

    def _parse_dir(self, folder_url: str) -> Dict[str, str]:
        body, _ctype = self._get(folder_url, "application/json,text/html;q=0.9,*/*;q=0.8")
        txt = body.decode("utf-8", "ignore").lstrip()
        out: Dict[str, str] = {}
        # JSON
        if txt[:1] in "[{":
            try:
                obj = json.loads(txt)
                items = obj if isinstance(obj, list) else obj.get("files") or obj.get("items") or []
                for it in items:
                    if isinstance(it, dict):
                        n = it.get("name") or it.get("title") or it.get("filename")
                        u = it.get("url") or it.get("link") or it.get("download_url") or it.get("href")
                        if n and u:
                            out[str(n)] = urllib.parse.urljoin(folder_url, str(u))
            except Exception:
                out = {}
        # HTML 兜底
        if not out:
            import re as _re
            for m in _re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', txt, _re.S | _re.I):
                href, label = m.group(1), _re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if not href or href.startswith(("#", "mailto:")):
                    continue
                name = label or os.path.basename(urllib.parse.urlparse(href).path)
                if name:
                    out[name] = urllib.parse.urljoin(folder_url, href)
        if not out:
            raise CloudNotConfigured(f"无法从目录链接解析出文件列表: {folder_url}")
        return out

    def list_dir(self, folder_url: str, recursive: bool = False) -> Dict[str, str]:
        flat = self._parse_dir(folder_url)
        if not recursive:
            return flat
        # 递归：把「没有扩展名」的条目当子目录继续展开（HTTP 目录列表的通用启发式）
        out: Dict[str, str] = {}
        for name, url in flat.items():
            if os.path.splitext(name)[1]:          # 有扩展名 -> 视为文件
                out[name] = url
            else:                                   # 无扩展名 -> 视为子目录
                try:
                    for sub, sub_url in self._parse_dir(url).items():
                        out[f"{name}/{sub}"] = sub_url
                except Exception:
                    out[name] = url
        return out

    def download(self, url: str) -> bytes:
        body, _ = self._get(url)
        return body


class TencentDocsDriver(BaseCloudDriver):
    """腾讯文档（docs.qq.com）驱动骨架。

    已核实能力：支持按文件夹列举（`list_folder` / `manage.folder_list` /
    `list_folder_contents`），因此**攻略合集给一个文件夹链接/文件夹 ID 就够**。

    鉴权与接口地址按腾讯文档开放平台填写下列环境变量后即可工作；
    未配置时抛 CloudNotConfigured 提示，不臆造接口。
      FX_CLOUD_TOKEN           访问令牌
      FX_CLOUD_CLIENT_ID / FX_CLOUD_CLIENT_SECRET   OAuth 换票
      FX_CLOUD_TOKEN_URL       换票地址
      FX_CLOUD_API_BASE        接口根地址
    """

    def __init__(self):
        self.api_base = _env("FX_CLOUD_API_BASE")
        self.token = _env("FX_CLOUD_TOKEN")

    def _require(self):
        if not (self.api_base or self.token):
            raise CloudNotConfigured(
                "腾讯文档驱动未配置：请先填 FX_CLOUD_API_BASE 与 FX_CLOUD_TOKEN"
                "（或用 FX_CLOUD_CLIENT_ID/SECRET + FX_CLOUD_TOKEN_URL 换票）。"
            )

    def list_dir(self, folder_url: str, recursive: bool = False) -> Dict[str, str]:
        self._require()
        raise CloudNotConfigured(
            "腾讯文档目录列举需按开放平台文档接线：在此实现 "
            "`list_folder(folder_id)` -> {文件名: 导出/下载直链}。"
            "（文件夹链接 / folder_id 均可，支持按文件夹整体列举）"
        )

    def download(self, url: str) -> bytes:
        self._require()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()


class WorkBuddyLibraryDriver(BaseCloudDriver):
    """WorkBuddy 资料库（workbuddy.cn/space）驱动骨架。

    已核实能力：`manage` 支持按父节点拉一层子节点（不传取空间根目录），
    kind=drive 为网盘文件 —— 同样支持「一个目录链接展开」。

    注意：该空间的访问令牌由 agent 侧 MCP（`connect_open_platform`）临时换取，
    按规范**不得落盘/进环境变量**，所以本驱动要求运行时注入：
        fs.set_cloud_token(token)
    若你的服务进程能安全拿到长期 token，也可走 FX_CLOUD_TOKEN。
    """

    def __init__(self):
        self.injected_token = ""

    def list_dir(self, folder_url: str, recursive: bool = False) -> Dict[str, str]:
        raise CloudNotConfigured(
            "资料库驱动需运行时注入令牌：fs.set_cloud_token(token) 后，"
            "按 manage 的「按父节点拉一层子节点」实现目录展开。"
        )

    def download(self, url: str) -> bytes:
        raise CloudNotConfigured("资料库驱动未接线（需先注入令牌并实现下载）。")


_DRIVERS = {
    "tencent_docs": TencentDocsDriver,
    "http": HttpFolderDriver,
    "workbuddy": WorkBuddyLibraryDriver,
}


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------
class FileSource:
    """唯一入口。业务代码只用 key，不碰路径。"""

    def __init__(self):
        self.root = os.path.abspath(_env("FX_PROJECT_ROOT")
                                    or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        _load_dotenv_if_any(self.root)
        self.mode = _env("FX_FILE_BACKEND", "local").lower()
        if self.mode not in ("local", "cloud"):
            raise ConfigError(f"FX_FILE_BACKEND 只能是 local/cloud，当前={self.mode}")
        self.local = LocalBackend(self.root)
        self.cloud: Optional[CloudBackend] = None
        if self.mode == "cloud":
            prov = _env("FX_CLOUD_PROVIDER", "tencent_docs").lower()
            if prov not in _DRIVERS:
                raise ConfigError(f"未知 FX_CLOUD_PROVIDER={prov}，可选：{', '.join(_DRIVERS)}")
            self.cloud = CloudBackend(self.root, _DRIVERS[prov]())

    # ---- 元数据 ----
    def res(self, key: str) -> Resource:
        if key not in RESOURCES:
            raise ConfigError(f"未知资源 key={key}；可用：{', '.join(sorted(RESOURCES))}")
        rel, group, binary = RESOURCES[key]
        return Resource(key, rel, group, binary)

    def set_cloud_token(self, token: str) -> None:
        """运行时注入云令牌（给资料库这类禁止落盘令牌的后端）。"""
        os.environ["FX_CLOUD_TOKEN"] = token
        if self.cloud and hasattr(self.cloud.driver, "injected_token"):
            self.cloud.driver.injected_token = token

    # ---- 读取 ----
    def read_bytes(self, key: str) -> bytes:
        r = self.res(key)
        if self.mode == "local":
            return self.local.read_bytes(r.rel)
        return self.cloud.read_bytes(r)

    def read_text(self, key: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(key).decode(encoding, "ignore")

    def read_json(self, key: str):
        return json.loads(self.read_text(key))

    def path(self, key: str) -> str:
        """返回可用文件路径。云模式下先落到缓存再返回缓存路径。"""
        r = self.res(key)
        if self.mode == "local":
            return self.local.abspath(r.rel)
        return self.cloud.path(r)

    def exists(self, key: str) -> bool:
        r = self.res(key)
        return self.local.exists(r.rel) if self.mode == "local" else self.cloud.exists(r)

    def mtime(self, key: str) -> float:
        r = self.res(key)
        return self.local.mtime(r.rel) if self.mode == "local" else self.cloud.mtime(r)

    def list_group(self, group: str, recursive: bool = False,
                   exts: Optional[Iterable[str]] = None) -> List[str]:
        """列出某分组目录下的文件（如攻略 docx 合集）。

        recursive=True 展开嵌套目录 —— 本地攻略 docx 是 攻略/xx/xx.docx 嵌套存放，
        要枚举整个「攻略合集」必须递归；
        exts 按扩展名过滤，如 exts=(".docx",)。
        """
        if group not in GROUP_LOCAL_DIR:
            raise ConfigError(f"未知分组={group}；可选：{', '.join(GROUP_LOCAL_DIR)}")
        names = (self.local.list_dir(GROUP_LOCAL_DIR[group], recursive=recursive)
                 if self.mode == "local" else self.cloud.list_dir(group, recursive=recursive))
        if exts:
            exts = tuple(e.lower() for e in exts)
            names = [n for n in names if n.lower().endswith(exts)]
        return names

    # ---- 自检 ----
    def resolve(self, key: str) -> str:
        """只解析不下载：本地返回绝对路径，云模式返回解析出的 URL。

        用于自检——配置类错误（目录没配、文件找不到）要**把原因说清楚**，
        而不是简单报「缺失」。
        """
        r = self.res(key)
        if self.mode == "local":
            return self.local.abspath(r.rel)
        return self.cloud.resolve_url(r)

    def check(self) -> List[Tuple[str, str, str, str]]:
        rows = []
        for key in sorted(RESOURCES):
            r = self.res(key)
            try:
                loc = self.resolve(key)
            except Exception as e:
                rows.append((key, r.group, "ERR", f"{type(e).__name__}: {e}"))
                continue
            if self.mode == "local":
                if os.path.isfile(loc):
                    rows.append((key, r.group, "OK", loc))
                else:
                    rows.append((key, r.group, "可选缺失" if key in OPTIONAL_RESOURCES else "缺失", loc))
            else:
                rows.append((key, r.group, "已解析", loc))
        return rows


fs = FileSource()


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------
def _main(argv: List[str]) -> int:
    if "--check" in argv:
        print(f"mode = {fs.mode}   root = {fs.root}")
        if fs.mode == "cloud":
            print(f"provider = {_env('FX_CLOUD_PROVIDER', 'tencent_docs')}")
        print(f"{'key':<18}{'group':<15}{'状态':<8}路径")
        print("-" * 100)
        miss = 0
        for key, group, st, loc in fs.check():
            if st in ("缺失", "ERR"):   # 「可选缺失」是已知的历史遗留引用，不算失败
                miss += 1
            print(f"{key:<18}{group:<15}{st:<8}{loc}")
        print("-" * 100)
        print(f"共 {len(RESOURCES)} 项，异常 {miss} 项")
        return 1 if miss else 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
