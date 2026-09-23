#!/usr/bin/env bash
# =============================================================================
# 阿卡莉 1.0 云端启动脚本（Linux / macOS）
# 对应 Windows 的「启动拂晓服务.bat」，语义一致：设置云端默认值后启动服务。
#
# 用法：
#   chmod +x 用户端特用/启动拂晓服务_云端.sh
#   ./用户端特用/启动拂晓服务_云端.sh
#
# 生产环境更推荐：docker compose up -d --build（见项目根 docker-compose.yml）
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."   # 切到项目根（阿卡莉1.0）

# ---- 云端模式默认值（可在 .env 中覆盖；此处仅兜底）----
export AKALI_RUN_MODE="${AKALI_RUN_MODE:-cloud}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8000}"
export AKALI_NO_BROWSER="${AKALI_NO_BROWSER:-1}"     # 服务器无需自动开浏览器
export AKALI_AUTO_SHUTDOWN=""                        # 云端恒不自动关机
export AKALI_PROXY_HEADERS="${AKALI_PROXY_HEADERS:-1}"  # 信任反代头（按 IP 限额依赖）

# 配额：优先 redis，其次本地文件（未装 redis 客户端库时自动降级）
export FX_QUOTA_BACKEND="${FX_QUOTA_BACKEND:-redis}"
export FX_QUOTA_DIR="${FX_QUOTA_DIR:-./.runtime/quota}"
mkdir -p "$FX_QUOTA_DIR"

# 知识库对象存储同步（留空则仍用本地磁盘的知识库）
# export FX_KB_BASE_URL="https://<bucket>/kb"
# export FX_KB_MANIFEST_URL="https://<bucket>/kb/manifest.json"

PY="${PYTHON:-python3}"
echo "[启动] 运行模式=$AKALI_RUN_MODE 监听=$HOST:$PORT"
exec "$PY" 用户端特用/web_app.py
