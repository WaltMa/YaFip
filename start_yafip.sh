#!/usr/bin/env bash
# yafip 启动脚本：start（默认） / stop / restart
# 首次使用如无执行权限：chmod +x start_yafip.sh
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs

log() {
  echo "[yafip] $1"
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> logs/start_yafip.log
}

if ! command -v uv >/dev/null 2>&1; then
  log "未找到 uv。请先安装：https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

if [ ! -d ".venv" ]; then
  log "首次启动：创建环境并安装依赖..."
  if ! uv sync; then
    log "依赖安装失败，请检查网络。"
    exit 1
  fi
fi

exec uv run --quiet python -m yafip.cli "$@"
