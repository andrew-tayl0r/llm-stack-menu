#!/bin/bash
# <xbar.title>LLM Context Controls</xbar.title>
# <xbar.version>1</xbar.version>
# <xbar.author>Andrew Taylor</xbar.author>
# <xbar.desc>Control Claude Code and Codex optimisation tools. Headroom is CLI-only and optional because its routing can disable Claude GUI Remote Control.</xbar.desc>
# <xbar.dependencies>bash,python3,headroom,llmtrim,rtk,uv,claude,codex</xbar.dependencies>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# SwiftBar controller for the local LLM optimisation stack.
set -euo pipefail

# SwiftBar launches plugins with a minimal system PATH. Expose the installed
# Homebrew and user binaries so menu actions can find headroom, llmtrim, uv,
# claude, and codex just as they do from an interactive shell.
export PATH="/opt/homebrew/bin:/Users/andrew/.local/bin:/Users/andrew/.nvm/versions/node/v24.18.0/bin:${PATH:-}"
export PYTHONDONTWRITEBYTECODE=1

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/opt/homebrew/bin/python3"
CONTROLLER="$PLUGIN_DIR/controller.py"

if [[ "${1:-}" == "toggle" && -n "${2:-}" ]]; then
  "$PYTHON" "$CONTROLLER" busy "${2}"
  set +e
  "$PYTHON" "$CONTROLLER" toggle "$2"
  status=$?
  "$PYTHON" "$CONTROLLER" clear-busy
  exit "$status"
fi

if [[ "${1:-}" == "optimized" || "${1:-}" == "native" || "${1:-}" == "remote-control" ]]; then
  "$PYTHON" "$CONTROLLER" busy "$1"
  set +e
  "$PYTHON" "$CONTROLLER" "$1"
  status=$?
  "$PYTHON" "$CONTROLLER" clear-busy
  exit "$status"
fi

if [[ "${1:-}" == "check-updates" || "${1:-}" == "install-plugins" || "${1:-}" == "enable-xcode-mcp" || "${1:-}" == "headroom-scope" || "${1:-}" == "open-llmtrim-watch" || "${1:-}" == "open-rtk-gain" ]]; then
  exec "$PYTHON" "$CONTROLLER" "$@"
fi

exec "$PYTHON" "$CONTROLLER" menu
