#!/bin/bash
# <xbar.title>LLM Savings</xbar.title>
# <xbar.version>1</xbar.version>
# <xbar.author>Andrew Taylor</xbar.author>
# <xbar.desc>Live Headroom, llmtrim, RTK, and jCodeMunch savings.</xbar.desc>
# <xbar.dependencies>bash,python3,headroom,llmtrim,rtk</xbar.dependencies>
# SwiftBar statistics plugin for the local LLM optimisation stack.
set -euo pipefail

export PATH="/opt/homebrew/bin:/Users/andrew/.local/bin:/Users/andrew/.nvm/versions/node/v24.18.0/bin:${PATH:-}"
export PYTHONDONTWRITEBYTECODE=1

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /opt/homebrew/bin/python3 "$PLUGIN_DIR/.support/stats.py"
