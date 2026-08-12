#!/usr/bin/env bash

set -euo pipefail

NODE_VERSION=24.15.0
SMARTPERFETTO_COMMIT=1508f99788bfcf18cc861e4bf4f8b472e84240c3
ANDROID_MEMORY_COMMIT=d5514972ced78c3faa7fc17589c1ea9231645056
SERVER_IP="${PERFPILOT_SERVER_IP:-10.166.0.125}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${PERFPILOT_SERVER_ROOT:-$HOME/perfpilot}"
ENGINE_ROOT="$INSTALL_ROOT/engines"
CONFIG_ROOT="$INSTALL_ROOT/config"
DATA_ROOT="$INSTALL_ROOT/data"
STATE_ROOT="$INSTALL_ROOT/state"
ADMIN_PASSWORD_FILE="${PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE:-$STATE_ROOT/local-control/bootstrap-admin-password.txt}"
LOCAL_BIN="$HOME/.local/bin"
LOCAL_OPT="$HOME/.local/opt"
NODE_ARCHIVE="node-v$NODE_VERSION-linux-x64.tar.xz"
NODE_ROOT="$LOCAL_OPT/node-v$NODE_VERSION-linux-x64"

if [[ "$(id -u)" -eq 0 ]]; then
  printf '%s\n' '请使用普通 Ubuntu 用户运行，不要使用 root。' >&2
  exit 1
fi

for command_name in curl git python3 sha256sum tar xz; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '缺少命令：%s\n' "$command_name" >&2
    exit 1
  fi
done

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempt

  for attempt in $(seq 1 120); do
    if curl --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1; then
      printf '%s 已就绪：%s\n' "$label" "$url"
      return 0
    fi
    sleep 0.5
  done
  printf '%s 启动超时：%s\n' "$label" "$url" >&2
  systemctl --user --no-pager --full status \
    perfpilot-smartperfetto.service perfpilot-api.service perfpilot-web.service >&2 || true
  return 1
}

install_node() {
  local temporary_dir
  local checksum_line

  if [[ ! -x "$NODE_ROOT/bin/node" ]]; then
    temporary_dir="$(mktemp -d)"
    trap 'rm -rf -- "$temporary_dir"' RETURN
    curl --fail --location --silent --show-error \
      "https://nodejs.org/dist/v$NODE_VERSION/$NODE_ARCHIVE" \
      --output "$temporary_dir/$NODE_ARCHIVE"
    curl --fail --location --silent --show-error \
      "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt" \
      --output "$temporary_dir/SHASUMS256.txt"
    checksum_line="$(grep "  $NODE_ARCHIVE\$" "$temporary_dir/SHASUMS256.txt")"
    if [[ -z "$checksum_line" ]]; then
      printf '%s\n' 'Node.js 校验信息缺失。' >&2
      exit 1
    fi
    (
      cd "$temporary_dir"
      printf '%s\n' "$checksum_line" | sha256sum --check --strict
    )
    mkdir -p "$LOCAL_OPT"
    tar -xJf "$temporary_dir/$NODE_ARCHIVE" -C "$LOCAL_OPT"
    rm -rf -- "$temporary_dir"
    trap - RETURN
  fi

  mkdir -p "$LOCAL_BIN"
  for executable in node npm npx corepack; do
    ln -sfn "$NODE_ROOT/bin/$executable" "$LOCAL_BIN/$executable"
  done
  export PATH="$LOCAL_BIN:/usr/local/bin:/usr/bin:/bin"
  if [[ "$(node --version)" != "v$NODE_VERSION" ]]; then
    printf '%s\n' 'Node.js 版本校验失败。' >&2
    exit 1
  fi
}

sync_engine() {
  local name="$1"
  local url="$2"
  local commit="$3"
  local destination="$ENGINE_ROOT/$name"

  if [[ -d "$destination/.git" ]]; then
    if [[ -n "$(git -C "$destination" status --short --untracked-files=no)" ]]; then
      printf '内核目录存在未提交修改：%s\n' "$destination" >&2
      exit 1
    fi
  else
    git clone --quiet "$url" "$destination"
  fi
  if ! git -C "$destination" cat-file -e "$commit^{commit}" 2>/dev/null; then
    git -C "$destination" fetch --quiet origin "$commit"
  fi
  git -C "$destination" checkout --quiet --detach "$commit"
  if [[ "$(git -C "$destination" rev-parse HEAD)" != "$commit" ]]; then
    printf '内核版本校验失败：%s\n' "$name" >&2
    exit 1
  fi
}

configure_smartperfetto_environment() {
  local environment_file="$ENGINE_ROOT/SmartPerfetto/backend/.env"
  local key

  if [[ ! -f "$environment_file" ]]; then
    install -m 0600 /dev/null "$environment_file"
  fi
  for key in PORT SMARTPERFETTO_BACKEND_PORT; do
    if grep -q "^$key=" "$environment_file"; then
      sed -i -E "s/^$key=.*/$key=3001/" "$environment_file"
    else
      printf '%s=3001\n' "$key" >> "$environment_file"
    fi
  done
  chmod 600 "$environment_file"
}

write_initial_ai_environment() {
  local source_file="$ENGINE_ROOT/SmartPerfetto/backend/.env"
  local target_file="$CONFIG_ROOT/perfpilot-ai.env"
  local temporary_file="$CONFIG_ROOT/.perfpilot-ai.env.new"
  local model_line
  local token_line

  if [[ -f "$target_file" ]]; then
    return 0
  fi
  model_line="$(grep -m 1 '^CLAUDE_MODEL=' "$source_file" || true)"
  token_line="$(grep -m 1 '^ANTHROPIC_AUTH_TOKEN=' "$source_file" || true)"
  if [[ -z "$model_line" || -z "$token_line" ]]; then
    printf '%s\n' '未发现可复用的 AI 配置，PerfPilot 单轮 AI 报告暂不启用。'
    return 0
  fi
  umask 077
  {
    printf 'PERFPILOT_LOCAL_AI_BASE_URL=https://api.deepseek.com/v1/\n'
    printf 'PERFPILOT_LOCAL_AI_MODEL=%s\n' "${model_line#*=}"
    printf 'PERFPILOT_LOCAL_AI_TOKEN=%s\n' "${token_line#*=}"
    printf 'PERFPILOT_LOCAL_AI_PROVIDER_NAME=deepseek\n'
    printf 'PERFPILOT_LOCAL_AI_THINKING=disabled\n'
  } > "$temporary_file"
  chmod 600 "$temporary_file"
  mv "$temporary_file" "$target_file"
}

write_initial_environment() {
  local environment_file="$CONFIG_ROOT/perfpilot.env"
  local proxy_secret
  local temporary_file

  if [[ -f "$environment_file" ]]; then
    return 0
  fi
  proxy_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  temporary_file="$CONFIG_ROOT/.perfpilot.env.new"
  umask 077
  {
    printf 'PERFPILOT_API_ORIGIN=http://127.0.0.1:8000\n'
    printf 'PERFPILOT_PROXY_SECRET=%s\n' "$proxy_secret"
    printf 'PERFPILOT_LOCAL_SMARTPERFETTO_URL=http://127.0.0.1:3001\n'
    printf 'PERFPILOT_LOCAL_DATA_DIR=%s/local-runtime\n' "$DATA_ROOT"
    printf 'PERFPILOT_LOCAL_STATE_DIR=%s/local-control\n' "$STATE_ROOT"
    printf 'PERFPILOT_RESET_ANALYSIS_DATA=true\n'
    printf 'PERFPILOT_EXPECTED_ANALYSIS_ROOT=%s/local-runtime\n' "$DATA_ROOT"
    printf 'PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE=%s\n' "$ADMIN_PASSWORD_FILE"
    printf 'PERFPILOT_LOCAL_API_ORIGIN=http://%s:8000\n' "$SERVER_IP"
    printf 'PERFPILOT_LOCAL_WEB_ORIGIN=http://%s:3000\n' "$SERVER_IP"
    printf 'PERFPILOT_LOCAL_ANDROID_MEMORY_ROOT=%s/Android-App-Memory-Analysis\n' \
      "$ENGINE_ROOT"
    printf 'PERFPILOT_LOCAL_ADB=/usr/bin/adb\n'
  } > "$temporary_file"
  chmod 600 "$temporary_file"
  mv "$temporary_file" "$environment_file"
}

configure_deployment_environment() {
  local environment_file="$CONFIG_ROOT/perfpilot.env"
  local key
  local value

  for entry in \
    "PERFPILOT_LOCAL_DATA_DIR=$DATA_ROOT/local-runtime" \
    "PERFPILOT_LOCAL_STATE_DIR=$STATE_ROOT/local-control" \
    "PERFPILOT_RESET_ANALYSIS_DATA=true" \
    "PERFPILOT_EXPECTED_ANALYSIS_ROOT=$DATA_ROOT/local-runtime"; do
    key="${entry%%=*}"
    value="${entry#*=}"
    if grep -q "^$key=" "$environment_file"; then
      sed -i -E "s|^$key=.*|$key=$value|" "$environment_file"
    else
      printf '%s=%s\n' "$key" "$value" >> "$environment_file"
    fi
  done
  if ! grep -q '^PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE=' "$environment_file"; then
    printf 'PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE=%s\n' "$ADMIN_PASSWORD_FILE" \
      >> "$environment_file"
  fi
  chmod 600 "$environment_file"
}

install_node
install -d -m 0700 "$STATE_ROOT" "$STATE_ROOT/local-control" "$DATA_ROOT/local-runtime"
mkdir -p "$ENGINE_ROOT" "$CONFIG_ROOT"

sync_engine \
  SmartPerfetto \
  https://github.com/Gracker/SmartPerfetto.git \
  "$SMARTPERFETTO_COMMIT"
sync_engine \
  Android-App-Memory-Analysis \
  https://github.com/Gracker/Android-App-Memory-Analysis.git \
  "$ANDROID_MEMORY_COMMIT"
configure_smartperfetto_environment
write_initial_ai_environment

printf '%s\n' '正在安装 PerfPilot Python 依赖……'
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --quiet \
  --editable "$PROJECT_DIR/services/api" \
  --editable "$PROJECT_DIR/agents/device-agent"

printf '%s\n' '正在构建 PerfPilot 网页……'
(
  cd "$PROJECT_DIR"
  npm ci --no-audit --no-fund
  npm run build
)

printf '%s\n' '正在构建 SmartPerfetto……'
(
  cd "$ENGINE_ROOT/SmartPerfetto/backend"
  npm ci --no-audit --no-fund
  npm run build
  npm run trace-processor:ensure
)

write_initial_environment
configure_deployment_environment

set -a
# shellcheck disable=SC1090
source "$CONFIG_ROOT/perfpilot.env"
set +a
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/bootstrap-local-users.py"

mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "$PROJECT_DIR"/infra/ubuntu-user/systemd/*.service \
  "$PROJECT_DIR"/infra/ubuntu-user/systemd/*.target \
  "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user disable perfpilot-smartperfetto.service perfpilot-api.service \
  perfpilot-web.service 2>/dev/null || true
systemctl --user enable perfpilot.target
"$PROJECT_DIR/scripts/restart-ubuntu-perfpilot.sh"

wait_for_url http://127.0.0.1:3001/health SmartPerfetto
wait_for_url "http://$SERVER_IP:8000/v1/health" PerfPilot-API
wait_for_url "http://$SERVER_IP:3000" PerfPilot-Web

printf '%s\n' \
  '' \
  'Ubuntu 服务器测试版已启动。' \
  "网页：http://$SERVER_IP:3000" \
  "API：http://$SERVER_IP:8000/v1/health" \
  '重启（永久清空分析数据）：bash scripts/restart-ubuntu-perfpilot.sh' \
  '状态：systemctl --user status perfpilot.target'
