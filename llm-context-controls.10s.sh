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

# controller.py itself now holds an exclusive lock and manages the busy flag
# for every mutating command (toggle/optimized/native/remote-control/etc.), so
# the wrapper just forwards args. This closed a race where two overlapping
# invocations of this script (e.g. clicking a toggle while "Optimised mode"
# was still applying) could each read-modify-write the same config files and
# silently clobber each other's change.
if [[ $# -gt 0 ]]; then
  exec "$PYTHON" "$CONTROLLER" "$@"
fi

exec "$PYTHON" "$CONTROLLER" menu
