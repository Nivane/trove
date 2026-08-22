#!/usr/bin/env bash
# 一键启动本地后端开发服务（后台常驻）：更新代码 + 安装依赖 + 启动
set -euo pipefail

cd "$(dirname "$0")"
source ./dev_lib.sh

echo "==> 拉取最新代码"
git pull --ff-only origin main

echo "==> 安装后端依赖"
uv sync

start_backend

echo "==> 后端已在后台运行: http://127.0.0.1:8000"
echo "    日志 .dev/backend.log, PID $(cat "$(pidfile backend)")"