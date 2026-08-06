#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$PROJECT_DIR/.perfpilot/run"
USER_HOME_DIR="$(
  "$PROJECT_DIR/.venv/bin/python" -c 'from pathlib import Path; print(Path.home())'
)"
SMARTPERFETTO_DIR="${PERFPILOT_LOCAL_SMARTPERFETTO_ROOT:-$USER_HOME_DIR/SmartPerfetto/backend}"
WEB_PID_FILE="$RUNTIME_DIR/web.pid"
API_PID_FILE="$RUNTIME_DIR/api.pid"
SMARTPERFETTO_PID_FILE="$RUNTIME_DIR/smartperfetto.pid"
WEB_LOG="$RUNTIME_DIR/web.log"
API_LOG="$RUNTIME_DIR/api.log"
SMARTPERFETTO_LOG="$RUNTIME_DIR/smartperfetto.log"
DATA_DIR="${PERFPILOT_LOCAL_DATA_DIR:-$PROJECT_DIR/.perfpilot/local-runtime}"
WEB_PORT=3000
API_PORT=8000
SMARTPERFETTO_PORT=3001
WEB_URL="http://localhost:$WEB_PORT"
API_URL="http://127.0.0.1:$API_PORT/v1/health"
SMARTPERFETTO_URL="http://127.0.0.1:$SMARTPERFETTO_PORT/health"
RESET_ONLY=0

usage() {
  printf '%s\n' \
    "一键重启 PerfPilot 本地服务" \
    "" \
    "用法：npm run dev:restart" \
    "说明：每次重启都会删除本地历史分析数据" \
    "网页：$WEB_URL" \
    "API：$API_URL" \
    "SmartPerfetto：$SMARTPERFETTO_URL"
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --reset-only)
    RESET_ONLY=1
    ;;
  "")
    ;;
  *)
    printf '未知参数：%s\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac

mkdir -p "$RUNTIME_DIR"

clear_analysis_history() {
  local resolved_data_dir
  local safe_root="$PROJECT_DIR/.perfpilot"

  resolved_data_dir="$(
    "$PROJECT_DIR/.venv/bin/python" -c \
      'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DATA_DIR"
  )"

  case "$resolved_data_dir" in
    "$safe_root"/*)
      ;;
    *)
      printf '拒绝清理项目目录之外的数据：%s\n' "$resolved_data_dir" >&2
      return 1
      ;;
  esac

  case "$resolved_data_dir" in
    "$RUNTIME_DIR"|"$RUNTIME_DIR"/*)
      printf '拒绝把服务运行目录当作分析数据删除：%s\n' \
        "$resolved_data_dir" >&2
      return 1
      ;;
  esac

  rm -rf -- "$resolved_data_dir"
  mkdir -p "$resolved_data_dir"
  printf '已清空历史分析数据：%s\n' "$resolved_data_dir"
}

if [[ "$RESET_ONLY" -eq 1 ]]; then
  clear_analysis_history
  exit 0
fi

process_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

is_managed_process() {
  local pid="$1"
  local allowed_root="$2"
  local cwd

  cwd="$(process_cwd "$pid")"
  case "$cwd" in
    "$allowed_root"|"$allowed_root"/*)
      return 0
      ;;
  esac

  return 1
}

tree_pids() {
  local root_pid="$1"
  local child_pid

  printf '%s\n' "$root_pid"
  for child_pid in $(pgrep -P "$root_pid" 2>/dev/null || true); do
    tree_pids "$child_pid"
  done
}

terminate_tree() {
  local root_pid="$1"
  local label="$2"
  local allowed_root="$3"
  local pids
  local remaining=""
  local pid
  local attempt

  if ! kill -0 "$root_pid" 2>/dev/null; then
    return 0
  fi

  if ! is_managed_process "$root_pid" "$allowed_root"; then
    printf '跳过 %s：PID %s 不属于允许的目录。\n' "$label" "$root_pid" >&2
    return 1
  fi

  pids="$(tree_pids "$root_pid" | tr '\n' ' ')"
  kill $pids 2>/dev/null || true

  for attempt in $(seq 1 40); do
    remaining=""
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        remaining="$remaining $pid"
      fi
    done
    if [[ -z "$remaining" ]]; then
      printf '已停止 %s。\n' "$label"
      return 0
    fi
    sleep 0.1
  done

  kill -9 $remaining 2>/dev/null || true
  printf '已强制停止 %s。\n' "$label"
}

stop_from_pid_file() {
  local pid_file="$1"
  local label="$2"
  local allowed_root="$3"
  local pid

  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi

  pid="$(sed -n '1p' "$pid_file")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    terminate_tree "$pid" "$label" "$allowed_root"
  fi
  rm -f "$pid_file"
}

top_managed_ancestor() {
  local pid="$1"
  local allowed_root="$2"
  local parent

  while true; do
    parent="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')"
    if [[ ! "$parent" =~ ^[0-9]+$ ]] || [[ "$parent" -le 1 ]]; then
      break
    fi
    if ! is_managed_process "$parent" "$allowed_root"; then
      break
    fi
    pid="$parent"
  done

  printf '%s\n' "$pid"
}

stop_managed_listener() {
  local port="$1"
  local label="$2"
  local allowed_root="$3"
  local listener_pid
  local root_pid

  for listener_pid in $(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
    if ! is_managed_process "$listener_pid" "$allowed_root"; then
      printf '端口 %s 被允许目录之外的 PID %s 占用，未停止。\n' \
        "$port" "$listener_pid" >&2
      return 1
    fi
    root_pid="$(top_managed_ancestor "$listener_pid" "$allowed_root")"
    terminate_tree "$root_pid" "$label" "$allowed_root"
  done
}

assert_port_free() {
  local port="$1"
  local label="$2"
  local attempt

  for attempt in $(seq 1 30); do
    if ! lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done

  printf '%s 端口 %s 仍被占用，无法重启。\n' "$label" "$port" >&2
  return 1
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local pid="$3"
  local log_file="$4"
  local attempt

  for attempt in $(seq 1 160); do
    if curl --fail --silent --show-error --max-time 1 "$url" >/dev/null 2>&1; then
      printf '%s 已就绪：%s\n' "$label" "$url"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done

  printf '%s 启动失败，最近日志如下：\n' "$label" >&2
  tail -n 30 "$log_file" >&2 || true
  return 1
}

printf '正在停止旧服务……\n'
if [[ ! -f "$SMARTPERFETTO_DIR/package.json" ]]; then
  printf '找不到 SmartPerfetto 后端：%s\n' "$SMARTPERFETTO_DIR" >&2
  printf '可设置 PERFPILOT_LOCAL_SMARTPERFETTO_ROOT 指向 backend 目录。\n' >&2
  exit 1
fi
SMARTPERFETTO_DIR="$(cd "$SMARTPERFETTO_DIR" && pwd)"

stop_from_pid_file "$WEB_PID_FILE" "网页服务" "$PROJECT_DIR"
stop_from_pid_file "$API_PID_FILE" "API 服务" "$PROJECT_DIR"
stop_from_pid_file \
  "$SMARTPERFETTO_PID_FILE" \
  "SmartPerfetto 服务" \
  "$SMARTPERFETTO_DIR"
stop_managed_listener "$WEB_PORT" "网页服务" "$PROJECT_DIR"
stop_managed_listener "$API_PORT" "API 服务" "$PROJECT_DIR"
stop_managed_listener \
  "$SMARTPERFETTO_PORT" \
  "SmartPerfetto 服务" \
  "$SMARTPERFETTO_DIR"
assert_port_free "$WEB_PORT" "网页服务"
assert_port_free "$API_PORT" "API 服务"
assert_port_free "$SMARTPERFETTO_PORT" "SmartPerfetto 服务"
clear_analysis_history

printf '正在启动新服务……\n'
(
  cd "$SMARTPERFETTO_DIR"
  SMARTPERFETTO_BACKEND_PORT="$SMARTPERFETTO_PORT" \
    PORT="$SMARTPERFETTO_PORT" \
    nohup npm run dev >"$SMARTPERFETTO_LOG" 2>&1 < /dev/null &
  printf '%s\n' "$!" > "$SMARTPERFETTO_PID_FILE"
)
smartperfetto_pid="$(sed -n '1p' "$SMARTPERFETTO_PID_FILE")"
wait_for_url \
  "$SMARTPERFETTO_URL" \
  "SmartPerfetto 服务" \
  "$smartperfetto_pid" \
  "$SMARTPERFETTO_LOG"

cd "$PROJECT_DIR"

nohup npm run dev:api >"$API_LOG" 2>&1 < /dev/null &
api_pid=$!
printf '%s\n' "$api_pid" > "$API_PID_FILE"

nohup npm run dev >"$WEB_LOG" 2>&1 < /dev/null &
web_pid=$!
printf '%s\n' "$web_pid" > "$WEB_PID_FILE"

wait_for_url "$API_URL" "API 服务" "$api_pid" "$API_LOG"
wait_for_url "$WEB_URL" "网页服务" "$web_pid" "$WEB_LOG"

printf '%s\n' \
  "" \
  "重启完成。" \
  "打开：$WEB_URL" \
  "SmartPerfetto：$SMARTPERFETTO_URL" \
  "日志：$RUNTIME_DIR"
