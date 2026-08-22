#!/usr/bin/env bash
# 一键重启本地开发服务（后台）：用法 ./dev_restart.sh [frontend|backend|all]
set -euo pipefail

cd "$(dirname "$0")"
source ./dev_lib.sh

TARGET="${1:-all}"

case "$TARGET" in
  frontend)
    stop_frontend
    start_frontend
    echo "==> 前端已重启: http://localhost:5173/ui/"
    ;;
  backend)
    stop_backend
    start_backend
    echo "==> 后端已重启: http://127.0.0.1:8000"
    ;;
  all)
    stop_frontend
    stop_backend
    start_backend
    start_frontend
    echo "==> 已全部重启: 前端 http://localhost:5173/ui/ | 后端 http://127.0.0.1:8000"
    ;;
  *)
    echo "用法: ./dev_restart.sh [frontend|backend|all]" >&2
    exit 1
    ;;
esac