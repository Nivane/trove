#!/usr/bin/env bash
# 一键启动本地前端开发服务（后台常驻 HMR）：更新代码 + 安装依赖 + 启动
set -euo pipefail

cd "$(dirname "$0")"
source ./dev_lib.sh

echo "==> 拉取最新代码"
git pull --ff-only origin main

echo "==> 安装前端依赖"
npm install --prefix ./frontend

start_frontend

echo "==> 前端已在后台运行: http://localhost:5173/ui/"
echo "    日志 .dev/frontend.log, PID $(cat "$(pidfile frontend)")"