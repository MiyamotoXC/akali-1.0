# 阿卡莉智能体

拂晓舰灵助手 — 伤害计算 / 角色查询 / 攻略检索 / 遭遇战模拟 / 全队技能反查

## 项目简介

阿卡莉智能体是一个基于大语言模型的《拂晓：胜利之刻》舰灵战斗分析工具。用户通过自然语言对话即可完成伤害计算、角色查询、攻略检索、遭遇战模拟等操作，人设为「腹黑傲娇秘书阿卡莉」。

核心设计理念：**原子化工具集 + 自由组合**。所有计算公式、解析逻辑、数据查询能力被拆分为独立的原子工具与确定性路由分支，用户端和开发者端按各自需求自由组合调用。

路由哲学（**规则优先，LLM 兜底**）：

1. **能用确定工具就绝不调 LLM** —— 公式计算、概率查表、名单筛选、攻略检索全部走硬编码路由，输出固定结构（标题加书名号 + 编号），保证数值准确、零口胡；
2. **规则漏网 → LLM 只做「意图分析 + 条件翻译」** —— 不生成内容，仅分类/翻译成严格校验的短结构；
3. **仍是问意 → 知识检索兜底（RAG）** —— 依据攻略库片段作答，不标注来源篇目，查不到就明说，不编造；
4. **生活闲聊 → 最后才交 LLM**，以傲娇式关心口吻应答。

---

## 项目结构

```
阿卡莉1.0/
├── 用户端特用/                    # 用户访问入口（Web 聊天界面）
│   ├── web_app.py                # FastAPI 服务入口（LLM Agent + 确定性路由）
│   ├── web/index.html            # 前端聊天界面（页内 token 输入）
│   ├── agent/agent_app.py        # CLI 调试入口
│   ├── user_data.json            # 用户数据（自定义角色/练度等）
│   ├── 启动拂晓服务.bat           # 一键启动脚本（Windows 本地）
│   ├── 启动拂晓服务_云端.sh       # 云端启动脚本（Linux / macOS）
│   └── workspace/                # 运行期产物（临时）
│
├── 开发者入口/                    # 开发者工具（Streamlit Web 应用）
│   ├── app.py                    # Streamlit 主入口（表格导入/手动更新/伤害计算器）
│   ├── core/
│   │   ├── business_logic.py     # 业务逻辑层（对比/配置/格式化）
│   │   ├── data_api.py           # 数据 API 层（三源提取、差异对照、增量导入管线）
│   │   └── user_engine.py        # 用户端口径时间轴模拟引擎
│   ├── build/                    # 知识库构建链（逐文件逐函数说明见 开发者入口/构建说明.md）
│   │   ├── 构建入口.py            # ★ 一键入口：按依赖链增量重建全部产物（--suite a|b|all / --full / --list）
│   │   ├── 角色数据构建.py         # 套A：Excel 源表 → raw_data.json / data.json / talent_*.json（stage 1/2/3）
│   │   ├── 规则补全.py            # stage 3 规则补全（点燃/持续buff/航速/CD/光环/暴击/条件增伤，幂等不调 LLM）
│   │   ├── 标准化副本.py          # data.json 标准化副本（面板比例调整 + 去重，手动单独跑）
│   │   ├── 攻略转文档.py          # 套B：攻略 docx → 攻略_md/
│   │   ├── 攻略索引.py            # → guide_index.json（攻略滑块）
│   │   ├── 知识库增量.py          # → 拂晓知识库_agent.json（agent kb_search 用）
│   │   ├── 攻略问答库构建.py       # → 攻略QA库.json（chunk → LLM 生成问句）
│   │   ├── 问答变体生成.py        # QA 口语问法增强（alt_questions）
│   │   ├── 问答向量化.py          # → 攻略QA向量库.npz（语义召回）
│   │   ├── 补充问答录入.py / 补充技巧录入.py   # 问答补充.txt → QA 库 / tips.json
│   │   ├── 技巧概览.py / 头像裁剪.py / 生成节庆素材.py    # 只读工具 / 一次性脚本
│   │   ├── src/                  # 构建公共库（表格读取/技能解析/舰船命名/名称归一化/模型客户端/提示词/…）
│   │   └── config/               # ship_name_map.json 等
│   └── requirements.txt          # 开发者端依赖（streamlit/pandas/openpyxl/pypinyin…）
│
├── 通用工具/                      # 核心工具库（用户端/开发者端共用）
│   ├── core/
│   │   ├── formula_engine.py     # 原子计算引擎（纯函数集合）
│   │   ├── constants.py          # 常量/映射表加载（克制、点燃、击穿）
│   │   ├── combat_data.py        # 战斗数据结构（normal_attacks 段结构）
│   │   ├── damage_formula.py     # DamageCalculator（完整封装）
│   │   ├── damage_calc.py        # SimpleDamageCalculator（简化编排）
│   │   ├── encounter_simulator.py# 遭遇战 / PVP 时间轴模拟（含旗舰 phase 作用域）
│   │   ├── calc_flow.py          # route_work 路由裁决（确定性分支 + RAG 兜底）
│   │   ├── quota.py              # 多用户算力配额（免费/月卡限额 + 云端可插拔存储后端）
│   │   ├── skill_parser.py       # 技能文本解析
│   │   ├── skill_buff_extractor.py # Buff / 旗舰光环 / 点燃 / 持续扣除提取
│   │   ├── name_service.py       # 舰灵名称服务
│   │   ├── reminder.py           # 主动提醒（活动日历/月度资源）
│   │   └── output_handler.py     # 输出格式化
│   ├── tools/                    # LLM 可调用工具实现（build_tools 注册）
│   │   ├── calc.py               # 伤害计算
│   │   ├── query.py              # 角色查询 / 技能条件反查 query_ships_by_skill
│   │   ├── knowledge.py          # 知识问答 / 弹种速查 ammo_mech_qa / RAG
│   │   ├── encounter.py          # 遭遇战模拟
│   │   ├── talent.py / session.py / favor.py / table_io.py / reload_cd.py
│   │   └── common.py             # 工具共用逻辑
│   ├── data/                     # App 类、用户数据装载
│   └── llm/llm_client.py         # LLM 客户端配置（agicto/dashscope）
│
├── 知识库/                        # 游戏数据（只读消费者视角）
│   ├── 底层数据/
│   │   ├── 源文件/               # 人工维护源（Excel/JSON，见下方清单）
│   │   └── 产物/                 # 程序生成（raw_data.json / data.json / talent_map.json /
│   │                             #   talent_pvp.json / custom_ship_config.json + .bak_* 备份）
│   ├── 攻略/                     # 攻略原文 + guide_index.json / tips.json
│   ├── 攻略_md/                  # 攻略 Markdown 版本（LLM 检索/QA 构建源）
│   ├── 遭遇战历史数据/            # 历史遭遇战记录
│   ├── 活动/                     # 活动日历（主动提醒数据源）
│   ├── 图片素材/                  # 角色头像、装备图标
│   └── llm/                      # system_prompt.md（人设）、拂晓知识库_agent.json、
│                                 #   攻略QA库.json、攻略QA向量库.npz
│
├── 调试/                          # 从当前知识库生成的随机自测
│   ├── 随机测试.py               # 统一入口：工具路由 / 随机问答 / 计算工具
│   ├── 测试集/                  # 本次种子生成的可复现 JSONL 输入
│   └── 测试结果/              # 工具调用、LLM 置信度、计算输出与汇总
│
├── 浏览器扩展/                    # PC 浏览器插件（插件本体 = 用户端界面，非跳转壳）
│   ├── manifest.json             # MV3 清单（图标 / 快捷键 / 右键菜单）
│   ├── bridge.js                 # 服务器地址与口令 → 用户端跨域入口（最先执行）
│   ├── app.html / app.js         # ★构建产物（由 构建扩展.py 从 web/index.html 生成）
│   ├── launcher.html / launcher.js  # 「连接设置」页（填服务器地址 / 口令）
│   ├── background.js             # 开窗口 / 快捷键 Alt+Shift+A / 划词查询
│   ├── 构建扩展.py                # 改完网页重跑即同步（内联脚本外置 + 注入 bridge）
│   └── 安装说明.md                # 加载步骤与常见问题
├── 接口契约.md                    # 给插件 / 外部客户端的接口约定（鉴权/字段/限额）
├── deploy/                       # 云部署件
│   └── nginx.conf                # 反向代理（真实 IP 透传，云端按 IP 限额依赖）
├── .env.example                  # 【本地自用】env 模板（复制为 .env 即可跑，默认无需改）
├── .env.cloud.example            # 【云服务器】env 模板（cp 后填 3 项必填，配 docker compose）
├── Dockerfile                    # 云端镜像（Python 3.12 + 依赖分层，自带知识库兜底）
├── docker-compose.yml            # 云端编排：akali(FastAPI) + nginx + redis
├── .dockerignore / .gitignore    # 构建忽略（密钥零进镜像）
├── 一键发布到GitHub.bat           # git add + commit + push 三连（双击即发布）
├── README.md                     # 本文件（项目说明：架构 / 功能 / 本地运行 / 开发）
├── 云端部署运维.md                # 云服务器部署与运维手册（部署步骤 + 命令速查 + 踩坑记录）
│
├── AGENT/                        # 历史遗留（死代码，仅个别路径被引用，勿改动）
└── .workbuddy/                   # 工作记忆/协作数据（勿删）
```

---

## 架构说明

### 分层调用关系

```
用户端 web_app.py（FastAPI）
    ↓ route_work 路由裁决（calc_flow.py）
通用工具/core/*.py + tools/*.py（确定性工具层）
    ↓ 委托
通用工具/core/formula_engine.py（原子计算引擎）→ constants.py（映射表）
    ↓ 规则漏网
RAG 知识检索兜底（tools/knowledge.py → 攻略QA库/向量库）→ 最后才是 LLM 闲聊

开发者端 app.py（Streamlit）
    ↓ 调用
开发者端/core/data_api.py（三源提取 + preview_import 差异对照 + import_full 增量管线）
开发者端/core/business_logic.py / user_engine.py
    ↓ 加载（importlib/sys.path）
通用工具/core/*.py（与用户端同一套实现，口径一致）
```

### 数据与计算约定（近期硬化）

| 约定 | 内容 |
|------|------|
| 普攻倍率口径 | `multiplier` = **单发倍率**、`hit` = **发数**（整轮伤害 = multiplier × hit = 总倍率）；每个普攻段带 `slot`（主武器/副武器/…）与 `weapon_code`（DD2/BB4/CV2…）独立字段 |
| 装甲克制/点燃 | 按段 `weapon_type × ammo_type` 查表：克制修正 54 项（与克制表一致）；点燃只对**高爆**生效、**逐发独立判定**（如 战列高爆对重甲 10%） |
| 旗舰作用域 | 旗舰光环**仅在组队 PVP 与遭遇战夜战**生效；单人计算与遭遇战昼战不生效（`apply_flagship_aura(phase=)`：`pvp` / `night_only`） |
| 遭遇战参数 | Boss 读取自 `拉古兹.json` 顶层 `enemies`（11 个）；默认航向 **T优**、制空 **确保**（PVP/单体仍为 同航/均势）；黑夜时间 = 120 − 白天 |
| 「全队 buff」语义 | 只统计**主动技能**（支援/通常/必杀）给全队的强化增益，**旗舰光环与天赋不计**（用户铁律） |
| Buff 展示 | 正值不带 `+`（`15%`），负值保留 `-`（`-10%`） |

---

## 快速开始

### 1. 环境准备（用户端）

```bash
cd 阿卡莉1.0
python -m venv .venv
.venv\Scripts\activate        # Windows

# 核心依赖
pip install fastapi uvicorn qwen-agent dashscope python-dotenv numpy openpyxl requests pypinyin
```

### 2. 开发者端环境（可选）

```bash
cd 开发者入口
pip install -r requirements.txt
pip install streamlit python-calamine    # 开发者端实际运行依赖
```

### 3. 配置环境变量

```bash
copy .env.example .env       # 【本地自用】模板：复制即可，默认一项都不用改
# 部署到云服务器则改用：cp .env.cloud.example .env   （详见 云端部署运维.md）
```

### 4. 启动用户端（Web 聊天）

```bash
cd 用户端特用
python web_app.py            # 或双击 启动拂晓服务.bat
```

浏览器访问：`http://127.0.0.1:8000`（本机自用；邀请口令见 `.env` 的 `AKALI_INVITE_CODE`，留空则不校验）

> 对外提供服务的正式做法是**部署到云服务器**（见上文「云端部署」与 `docker-compose.yml`），
> 不再使用内网穿透（cpolar）把本机端口暴露到公网 —— 该功能已于 2026-09-24 移除。
> 若需临时把本机文件传给云服务器，见「传输大文件到服务器」一节。

### 5. 启动开发者端（Streamlit 工具）

```bash
cd 开发者入口
streamlit run app.py         # 或双击 start.bat
```

浏览器访问：`http://localhost:8501`

### 6. 数据构建 / 自测

```bash
python 开发者入口/build/构建入口.py            # 知识库一键增量构建（推荐，交互选套A/套B）
python 开发者入口/build/构建入口.py --list     # 只看哪些环节需要重建
python 开发者入口/build/角色数据构建.py --stage 3   # 仅重跑规则补全（不调 LLM，幂等）
python 调试\随机测试.py --suite all            # 三类随机测试
```

---

## 功能说明

### 用户端功能（自然语言对话）

| 功能 | 描述 | 示例 |
|------|------|------|
| 角色查询 | 角色面板/技能/天赋/装备 | 「查询赤城的信息」 |
| 伤害计算 | 普攻/通常技/必杀技/支援技/点燃，逐因子展开 | 「用赤城打南达科他，算普攻伤害」 |
| 总伤害计算 | 时间轴模拟统计总伤害 | 「赤城打嘉波 60 秒总伤害」 |
| 会话 Buff | 对话内设置 buff/天赋，后续计算沿用 | 「全伤害加成10%」 |
| 弹种机制速查 | 装甲克制/点燃/击穿概率（确定性数值，前置拦截） | 「高爆战列对重甲的点燃概率是多少」→ 10% |
| 技能条件反查 | 按效果/目标筛舰灵名单（支持全队增益/护盾/点燃等） | 「哪些船能给全队buff」「哪些船带点燃伤害」 |
| 时装/皮肤询问 | 信息库未收录时装数据，固定引导至游戏内商店 + 兑换商店（微笑关心差分） | 「赤城有什么皮肤」「时装怎么获得」 |
| 遭遇战模拟 | 6 人组队时间轴伤害仿真（昼/夜、旗舰夜战生效） | 「模拟嘉波遭遇战最优打法」 |
| PVP 模拟 | 组队 PVP 伤害对比（旗舰光环全时段生效） | 「胡德队打前卫队谁赢」 |
| 攻略检索/问答 | 关键词直连 → LLM 辅助识题 → RAG 依据攻略作答（不标注来源） | 「查练级攻略」「士气系统怎么运作」 |
| 天赋查询 | 各舰灵天赋效果 | 「查约克城的天赋」 |
| 主动提醒 | 活动日历、月初/月末资源兑换提醒 | 每次对话自动附带 |
| 表情/头像差分 | 神态由头像差分体现（正文不写神态括号）；身体/情感话题走傲娇式关心，仅极端恶意用「嫌弃」 | 闲聊自然呈现 |
| 多用户算力配额 | 免费 15 轮 / 月卡 0.3 元/天，超限停用 LLM、基础工具照常、次日零点恢复 | （默认 cloud 即生效；`AKALI_RUN_MODE=local` 或 `FX_CLOUD_MODE=off` 关闭） |

### 开发者端功能（Streamlit 8501）

| 功能 | 描述 | 入口 |
|------|------|------|
| 表格导入（三表） | **技能表 / 面板表 / 数值自动化表（综合表）** 卡片勾选；自动流程 = 差异化合并 → 面板按 hash 直刷 → 技能增量 LLM 重解析 → 普攻轻量同步；未选源保持原值 | 「表格导入」页 |
| 导入先对照后确认 | 非手动模式：差异先**逐处列出 旧值→新值**，批量确认后才写入；未确认差异保持原值（confirm 流程自动跳过 prune） | 表格导入 → 扫描对照 |
| 手动更新 | **保存即直写**底层角色 json（面板/技能/天赋/普攻），不走对照 | 「手动更新」页 |
| 角色查询/伤害计算器 | 自定义角色配置、单次伤害、Buff 对比、时间轴总伤害对比、遭遇战模拟 | 侧边栏各页 |
| 数据管理 | 源表解析 → raw_data.json → 标准副本 → talent_map/talent_pvp → custom_ship_config 同步 | `build/角色数据构建.py` / Streamlit |
| 规则补全（stage 3） | 点燃/持续 buff/旗舰光环/航速/技能 CD/递增光环/必定暴击/条件增伤/buffs 归一/普攻基准比对，代码规则自动产出（幂等，不调 LLM） | `build/规则补全.py`（随 stage 链自动执行） |

---

## 配置说明

### 环境变量

在根目录 `.env` 中配置。模板二选一：**本地自用** `.env.example`、**云服务器** `.env.cloud.example`
（云端那一套及运维操作见 [`云端部署运维.md`](云端部署运维.md)）。
各变量的完整说明、默认值与样例见 [`环境变量配置说明.md`](环境变量配置说明.md)。

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_PROVIDER` | 是 | 服务商：`agicto` / `dashscope` |
| `AGICTO_API_KEY` / `DASHSCOPE_API_KEY` | 视情况 | 按服务商填对应密钥 |
| `LLM_MODEL` | 否 | 模型名，默认 `qwen-plus` |
| `LLM_ENABLE_THINKING` | 否 | 思考模式开关，默认 `0`（qwen3 非流式必须关闭） |
| `EMBEDDING_MODEL` | 否 | 向量模型，默认 `text-embedding-v4` |
| `HOST` / `PORT` | 否 | 监听地址，默认 `127.0.0.1:8000` |
| `AKALI_NO_BROWSER` | 否 | `1` 不自动打开浏览器 |
| `AKALI_AUTO_SHUTDOWN` | 否 | `1` 页面关闭后自动终止服务 |
| `AKALI_CHAT_ENABLED` | 否 | `0` 禁用聊天功能 |
| `KNOWLEDGE_BASE_DIR` / `RAW_DATA_OUTPUT_PATH` / `DATA_OUTPUT_PATH` | 否 | 开发者数据管线路径覆盖（默认指向 `知识库/`） |
| `FX_CLOUD_MODE` | 否 | 旧版配额开关别名：未设时跟随 `AKALI_RUN_MODE`（默认 cloud，故默认即云端多用户+配额生效）；`on` 显式开启、`off` 可强制关闭配额（运行模式由 `AKALI_RUN_MODE` 决定） |
| `FX_QUOTA_FREE` | 否 | 免费档每日 token 上限（默认 60000；设 0 = 不限量） |
| `FX_QUOTA_PAID` | 否 | 付费档每日 token 上限（默认 200000；设 0 = 不限量） |
| `FX_QUOTA_WHITELIST` | 否 | 白名单每日 token 上限（默认 200000；类似开发者但可调；设 0 = 不限量） |
| `FX_QUOTA_WHITELIST_KEYS` | 否 | 白名单：逗号分隔的 IP/uid 列表，名单内走白名单额度（类似开发者，独立可调，不参与 free/paid 检测） |
| `FX_QUOTA_BACKEND` | 否 | 配额存储后端：`memory`/`local`/`redis`（云端默认 `redis`，连不上自动降级 `memory` 并告警） |
| `FX_QUOTA_DIR` | 否 | `local` 后端落盘目录（JSON）；空=纯内存，重启清零 |
| `FX_REDIS_URL` | 否 | Redis 连接串（如 `redis://127.0.0.1:6379/0`），多实例共享配额状态 |
| `FX_CDN_BASE` | 否 | 资源 CDN 基址（指向 `知识库/图片素材/`），如 `https://cdn.xxx.com/kb`；设值后前端静态/运行时资源走 CDN 直链 |

### 人设配置

编辑 `知识库/llm/system_prompt.md` 修改阿卡莉人设。**这是唯一支持热重载的文件** —— 保存后下一次对话自动生效（或调 `POST /api/reload_prompt`），其余 `通用工具/` 改动均需重启服务。

---

## 多用户算力配额与云端部署

项目默认即为**云端多用户模式**（`AKALI_RUN_MODE=cloud`，缺省时也默认 cloud）；如需纯本地单机，显式设 `AKALI_RUN_MODE=local` 回滚（此时配额不闸控）。

> 📘 **部署到云服务器 / 日常运维 → 见独立文档 [`云端部署运维.md`](云端部署运维.md)**
>
> 该文档包含：当前部署实况、从零部署步骤（装 Docker → 配 `.env` → 传知识库 → 起服务 → 验证）、
> **运维命令速查**（服务管理 / 改口令 / 换白名单 / 备份 / 防火墙 / 日志）、多用户配额设计、
> CDN 去本地化、实盘踩坑记录、大文件传输方法。
>
> 需要动手操作服务器时，直接看那份文档；本文档只管项目本身。

### 客户端形态（三种，可并存）

| 形态 | 怎么用 | 说明 |
|------|--------|------|
| **网页 / WebView APK** | 浏览器打开服务器地址；或打包 `android_app/` 的 WebView APK 直连云端（无需域名/HTTPS，见 `android_app/安装与分发说明.md`） | 零安装网页；APK 装完等同原生 App（桌面图标、全屏）。`?q=查攻略` 可直达提问 |
| **浏览器插件** | 把 `浏览器扩展/` 加载进 Chrome/Edge（见 `浏览器扩展/安装说明.md`） | **插件本体就是用户端界面**（非跳转壳）；支持快捷键 `Alt+Shift+A` 与**划词右键查询** |
| 外部客户端 | 按 `接口契约.md` 调用 `/api/chat` 等接口 | 任何能发 HTTP 的客户端都可对接；契约承诺向后兼容（升级账号体系不影响客户端） |

插件与网页**共用同一份界面源码**：改完 `用户端特用/web/index.html` 后，在 `浏览器扩展/` 下执行
`python 构建扩展.py` 即同步（脚本负责把内联脚本外置、注入 `bridge.js`、刷新图标）。

### 仓库与版本管理（GitHub 私有闭源）

本仓库为**私有仓库**（`akali-1.0`），只放源码与部署件；知识库与密钥一律不入库。

| 入库 | 不入库（见 `.gitignore`） |
|------|--------------------------|
| `用户端特用/`、`通用工具/`、`开发者入口/` 源码 | `.env`（LLM Key / 邀请口令等全部密钥） |
| `Dockerfile` / `docker-compose.yml` / `deploy/` | `知识库/`（约 355MB，部署时单独提供，见 `云端部署运维.md`） |
| `.env.example` / `.env.cloud.example`（模板，密钥值为空） | 各类 `.bak` 备份、`调试/`、运行时 `user_data.json` / `reminder_state.json` |

入库规模约 **190 个文件 / 5.5MB**（对比知识库 489MB）。

**部署时知识库从哪来**（仓库内没有）：① 直接上传 `知识库/` 目录（compose 已挂载 `./知识库:/app/知识库`）；② 对象存储 + 启动同步（`FX_KB_BASE_URL` / `FX_KB_MANIFEST_URL`）。

**所有对外地址都不写死在代码里**，统一由 `.env` 注入 —— 链接类配置（跨域白名单 / CDN / 对象存储）在
`.env.cloud.example` 末尾集中列出，默认全部留空即可正常对外服务。

#### 本地更新 → GitHub（git 不会自动同步）

改动不会自己上云，必须手动三步：

```bash
git add -A
git commit -m "改了什么"
git push
```

懒得敲就双击项目根的 `一键发布到GitHub.bat`（等价于上面三步，自动附时间戳说明）；想只发一句说明就拖一个参数：`一键发布到GitHub.bat "修了攻略输出的类数"`。

---

## 数据源与底层数据

### 源文件清单（`知识库/底层数据/源文件/`）

| 文件 | 内容 | 对应表 |
|------|------|--------|
| `舰灵数值（整理版）.xlsx` | 角色面板（舰种/属性/装备） | 面板表（panel） |
| `CharSkillInfoLv10 260728.xlsx` | 技能文本（旗舰/支援/通常/必杀） | 技能表（skill） |
| `数值自动化表.xlsx` | **综合表**：天赋 sheet + 「普攻列表」sheet（主/副武器段：单发倍率/hit/总倍率/射程/CD/动作时长 + 右侧克制修正表） | combo（普攻 + 天赋） |
| `拉古兹.json` | 遭遇战 Boss 定义（`enemies` 11 个 + 伤害公式引用） | 遭遇战 |
| `mappings.json` | 克制/点燃/击穿等查表常量 | 公式层 |
| `ship_nicknames.json` / `原型舰历史.json` / `装备.xlsx` | 昵称映射 / 原型历史 / 装备数据 | 查询 |

> 注意：普攻的唯一权威源是 `数值自动化表.xlsx → 普攻列表` sheet（旧表 `拂晓舰灵普攻倍率cd1.1.xlsx` 已删除，全量重建链路不可再依赖 `extract_normal_attacks`）。该文件用 openpyxl 解析会崩，读取必须走 `build/src/xlsx_std.py`（纯标准库）。

### 产物链（`知识库/底层数据/产物/`）

```
源文件 xlsx ──差异化合并──▶ raw_data.json（原始提取，逐角色逐源）
        ──standard_copy/面板hash直刷 + 技能增量LLM──▶ data.json（计算表，含 skill_buffs）
        ──▶ talent_map.json / talent_pvp.json / custom_ship_config.json（同步引用）
```

- data.json 中每个普攻段：`slot`（主武器/副武器）、`weapon_code`、`weapon_type`、`ammo_type`、`multiplier`（单发）、`hit`（发数）、`cd`、`range`、`preset_damage_type`；
- 产物目录保留各次变更的 `.bak_*` 备份，回滚时按时间/用途名对照即可。

---

## 开发者模式

### 构建链（`开发者入口/build/`）

逐文件逐函数说明见 `开发者入口/构建说明.md`。常用：

```bash
python 开发者入口/build/构建入口.py --list    # 只看哪些环节需要重建
python 开发者入口/build/构建入口.py           # 增量重建（交互选套A/套B/全部）
python 开发者入口/build/角色数据构建.py --stage 3   # 仅重跑规则补全（不调 LLM，幂等）
```

规则补全（stage 3）覆盖：换行规范化 / 航速对齐 / 常驻 buffs 归一 / `skill_buffs`·`flagship_buffs`·`raguz_bonus`·`cd_effects`
代码提取 / 递增光环 `ramp_buffs` / 技能循环 CD 回填 / `always_crit` / 条件增伤 `static_buffs` /
点燃 part 补全 / 普攻基准比对（自动补 `slot`/`weapon_code`，**不碰数值**；报告 → `调试/普攻基准识别报告.txt`）。

### 自测脚本

```bash
python 调试\随机测试.py --suite all
python 调试\随机测试.py --suite qa --seed 20260920 --qa-n 30
```

每次运行会从当前攻略 QA 库和舰灵数据重新抽样：
1. **工具调用与路由**：记录期望工具、实际工具名、参数和 data/retrieval/none 路由；
2. **随机用户问答**：将库内问法随机改成长短混合的用户输入，调用真实服务，再由独立模型仅对照证据评分；
3. **计算工具**：随机选择完整舰灵、航向、制空和时间，保存六类工具的完整输入输出，并校验同参数重复计算一致。

`--seed` 用于复现同一批样本；可复现的 JSONL 保存在 `调试/测试集/`，每类测试的易读 TXT 结果和汇总保存在 `调试/测试结果/`。

---

## 常用命令速查

```bash
# 用户端 Web 服务（端口 8000）
cd 用户端特用 && python web_app.py

# 开发者端 Streamlit 工具（端口 8501）
cd 开发者入口 && streamlit run app.py

# 知识库一键增量构建（套A 角色数据 / 套B 攻略问答向量）
python 开发者入口/build/构建入口.py

# 普攻基准识别（只读审计，随 stage 3 自动产出报告）
python 开发者入口/build/角色数据构建.py --stage 3

# 自测（工具路由、随机问答、计算工具）
python 调试\随机测试.py --suite all
```

---

## 路径约定

| 路径 | 说明 |
|------|------|
| `知识库/底层数据/源文件/` | 人工维护源数据（清单见上） |
| `知识库/底层数据/产物/` | 程序生成（raw_data.json / data.json / talent_map.json / talent_pvp.json / custom_ship_config.json + .bak_*） |
| `知识库/攻略/` | 攻略原文 + guide_index.json |
| `知识库/攻略_md/` | 攻略 Markdown（LLM 检索 / QA 构建源） |
| `知识库/llm/` | 人设 system_prompt.md、拂晓知识库_agent.json、攻略QA库.json / .npz |
| `知识库/活动/活动日历.json` | 主动提醒的活动日历 |
| `开发者入口/build/config/` | ship_name_map.json 等构建配置 |
| `user_ships.json` / `用户端特用/user_data.json` | 用户自定义角色数据（运行期生成） |

脚本均自动解析项目根目录，无需手动配置路径。

---

## 注意事项

1. **通用工具是两端共用核心库**，改动后需重启用户端服务（`Ctrl+C` → 双击 `启动拂晓服务.bat`）；**仅 `system_prompt.md` 支持热重载**。
2. **口径不可回退**：普攻 `multiplier` 为单发倍率、`hit` 为发数；数据导入以 `数值自动化表.xlsx`（普攻列表 sheet）为基准，勿再写回总倍率口径。
3. **底层数据改字段/加字段**：优先走增量补丁子命令或 Streamlit 三表导入（差异对照确认后写入），不要全量重建 data.json，以免覆盖手工修正。
4. **数值自动化表.xlsx 读取**必须使用 `build/src/xlsx_std.py`（openpyxl 解析该文件会崩溃）。
5. **表格导入 = 非手动模式**：有差异先对照、确认后才写；**手动更新页 = 直写模式**：保存即改底层 json，两者行为不同。
6. **舰名点号异体**：面板表用 `‧`(U+2027)、库内为 `·`(U+00B7)，名称匹配一律经 `canonicalize_name`，勿用字符串直配；「加改才是改造」——`太太`=列克星敦、`太太改`=列克星敦·改。
7. **Windows 环境**：命令为 PowerShell 兼容语法；路径含中文，终端建议以 UTF-8 输出。
8. **人设/头像系统**：神态一律由 `<<EXP:神态>>` 头像差分体现，回复正文严禁写 `（抬眼）`/`（轻哼）`/`（微微颔首）` 等神态括号；改 `知识库/llm/system_prompt.md` 即可热重载。
9. **云端配额**：运行模式默认即 `cloud`（`AKALI_RUN_MODE` 缺省也默认 cloud），`FX_CLOUD_MODE` 为旧别名——未设时配额跟随模式（cloud 即生效）；配额状态建议用 `redis` 后端（跨实例/重启不丢），`local` 落盘也会跨重启保留。
