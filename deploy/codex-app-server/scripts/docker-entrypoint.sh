#!/bin/sh
set -eu

: "${TEMPLATE_DIR:=/opt/agent-template}"
: "${WORKSPACE_DIR:=/agent}"
: "${CODEX_HOME_DIR:=/root/.codex}"

reset_codex_runtime_dirs() {
  rm -rf "${CODEX_HOME_DIR}/tmp" "${CODEX_HOME_DIR}/.tmp"
  mkdir -p \
    "${CODEX_HOME_DIR}" \
    "${CODEX_HOME_DIR}/log" \
    "${CODEX_HOME_DIR}/tmp" \
    "${CODEX_HOME_DIR}/.tmp" \
    "${CODEX_HOME_DIR}/cache" \
    "${CODEX_HOME_DIR}/memories" \
    "${CODEX_HOME_DIR}/sessions" \
    "${CODEX_HOME_DIR}/skills" \
    "${CODEX_HOME_DIR}/shell_snapshots"
}

seed_runtime_workspace() {
  if [ ! -d "${TEMPLATE_DIR}" ]; then
    return
  fi

  cp -n "${TEMPLATE_DIR}/AGENTS.md" "${WORKSPACE_DIR}/AGENTS.md" || true
  cp -n "${TEMPLATE_DIR}/README.md" "${WORKSPACE_DIR}/README.md" || true
}

install_codex_hooks() {
  if [ ! -d "${TEMPLATE_DIR}/.codex" ] || [ ! -f "${TEMPLATE_DIR}/.codex/hooks.json" ]; then
    return
  fi

  mkdir -p "${CODEX_HOME_DIR}/hooks"
  cp -f "${TEMPLATE_DIR}/.codex/hooks.json" "${CODEX_HOME_DIR}/hooks.json"
  if [ -d "${TEMPLATE_DIR}/.codex/hooks" ]; then
    cp -f "${TEMPLATE_DIR}/.codex/hooks/"*.py "${CODEX_HOME_DIR}/hooks/" 2>/dev/null || true
  fi
}

install_codex_config() {
  if [ ! -f "${TEMPLATE_DIR}/.codex/config.toml" ]; then
    return
  fi

  if [ ! -f "${CODEX_HOME_DIR}/config.toml" ]; then
    cp -f "${TEMPLATE_DIR}/.codex/config.toml" "${CODEX_HOME_DIR}/config.toml"
    return
  fi

  if ! grep -q '^model_provider = "openai"$' "${CODEX_HOME_DIR}/config.toml"; then
    tmp_file="$(mktemp)"
    cat "${TEMPLATE_DIR}/.codex/config.toml" >"${tmp_file}"
    printf '\n' >>"${tmp_file}"
    cat "${CODEX_HOME_DIR}/config.toml" >>"${tmp_file}"
    mv "${tmp_file}" "${CODEX_HOME_DIR}/config.toml"
  fi

  if ! grep -q '^realtime_conversation = ' "${CODEX_HOME_DIR}/config.toml"; then
    tmp_file="$(mktemp)"
    awk '
      /^\[features\]$/ && inserted == 0 {
        print
        print "realtime_conversation = true"
        inserted = 1
        next
      }
      { print }
      END {
        if (inserted == 0) {
          print ""
          print "[features]"
          print "realtime_conversation = true"
        }
      }
    ' "${CODEX_HOME_DIR}/config.toml" >"${tmp_file}"
    mv "${tmp_file}" "${CODEX_HOME_DIR}/config.toml"
  fi
}

install_codex_skills() {
  if [ ! -d "${TEMPLATE_DIR}/.codex/skills" ]; then
    return
  fi

  mkdir -p "${CODEX_HOME_DIR}/skills"
  cp -R "${TEMPLATE_DIR}/.codex/skills/." "${CODEX_HOME_DIR}/skills/"
}

repair_codex_config_compatibility() {
  for config_file in \
    "${CODEX_HOME_DIR}/config.toml" \
    "${WORKSPACE_DIR}/.codex/config.toml"
  do
    if [ ! -f "${config_file}" ]; then
      continue
    fi
    if ! grep -q '^approvals_reviewer = "auto_review"$' "${config_file}"; then
      continue
    fi
    tmp_file="$(mktemp)"
    sed 's/^approvals_reviewer = "auto_review"$/approvals_reviewer = "guardian_subagent"/' \
      "${config_file}" >"${tmp_file}"
    mv "${tmp_file}" "${config_file}"
  done
}

should_enable_app_server_ws_auth() {
  has_app_server=0
  has_ws_auth=0
  for arg in "$@"; do
    if [ "${arg}" = "app-server" ]; then
      has_app_server=1
    fi
    if [ "${arg}" = "--ws-auth" ]; then
      has_ws_auth=1
    fi
  done
  if [ "${has_app_server}" -eq 1 ] && [ "${has_ws_auth}" -eq 0 ]; then
    return 0
  fi
  return 1
}

is_codex_app_server_command() {
  if [ "${1:-}" != "codex" ]; then
    return 1
  fi
  for arg in "$@"; do
    if [ "${arg}" = "app-server" ]; then
      return 0
    fi
  done
  return 1
}

mkdir -p "${WORKSPACE_DIR}"
reset_codex_runtime_dirs
seed_runtime_workspace
install_codex_hooks
install_codex_config
install_codex_skills
repair_codex_config_compatibility

if should_enable_app_server_ws_auth "$@"; then
  token_file="${CODEX_APP_SERVER_WS_TOKEN_FILE:-${CODEX_HOME_DIR}/ws-capability-token}"
  if [ -n "${CODEX_APP_SERVER_WS_TOKEN:-}" ]; then
    umask 077
    mkdir -p "$(dirname "${token_file}")"
    printf '%s' "${CODEX_APP_SERVER_WS_TOKEN}" > "${token_file}"
  fi
  if [ -f "${token_file}" ]; then
    set -- "$@" --ws-auth capability-token --ws-token-file "${token_file}"
  fi
fi

if is_codex_app_server_command "$@"; then
  : "${RUST_LOG:=codex_app_server=debug,info}"
  : "${RUST_BACKTRACE:=1}"
  : "${CODEX_APP_SERVER_LOG_FILE:=${CODEX_HOME_DIR}/log/app-server.log}"
  export RUST_LOG RUST_BACKTRACE
  exec "$@" >>"${CODEX_APP_SERVER_LOG_FILE}" 2>&1
fi

exec "$@"
