#!/usr/bin/env bash
# Shared helpers for local dev scripts (后台进程/PID/日志管理)

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.dev"
mkdir -p "$DEV_DIR"

pidfile() { echo "$DEV_DIR/$1.pid"; }
logfile() { echo "$DEV_DIR/$1.log"; }

port_pids() { lsof -ti "tcp:$1" 2>/dev/null || true; }

kill_by_port() {
  local port="$1" name="$2" pids
  pids="$(port_pids "$port")"
  if [ -n "$pids" ]; then
    echo "==> stopping $name on :$port ($(echo "$pids" | tr '\n' ' '))"
    kill $pids 2>/dev/null || true
    sleep 1
    pids="$(port_pids "$port")"
    if [ -n "$pids" ]; then
      kill -9 $pids 2>/dev/null || true
    fi
  fi
  rm -f "$(pidfile "$name")"
}

start_in_bg() {
  local name="$1" port="$2"
  shift 2
  kill_by_port "$port" "$name"
  echo "==> starting $name (log: $(logfile "$name"))"
  nohup "$@" > "$(logfile "$name")" 2>&1 &
  echo $! > "$(pidfile "$name")"
}

stop_frontend() { kill_by_port 5173 frontend; }
stop_backend()  { kill_by_port 8000 backend; }

start_frontend() {
  start_in_bg frontend 5173 npm run dev --prefix ./frontend
}

start_backend() {
  start_in_bg backend 8000 uv run trove serve --host 127.0.0.1 --port 8000 --datasource demo
}