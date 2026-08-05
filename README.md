# LLM Stack Menu

SwiftBar plugins for controlling and monitoring a Claude Code-focused token-optimisation stack on macOS, with Codex support.

The controller switches Headroom, llmtrim, RTK and jCodeMunch between optimised and normal modes. The statistics item combines each tool's reported token savings while keeping their scopes and lifetime figures separate.

## Features

- Optimised, mixed and normal operating modes
- Independent tool switches
- Claude remote-control routing for Headroom
- Live llmtrim, RTK, Headroom and jCodeMunch statistics
- Today, rolling-window and lifetime breakdowns where the tools provide them
- Quick access to the Headroom dashboard, `llmtrim status --watch`, `rtk gain` and the jCodeMunch receipt
- Configuration backups under `~/.llm-stack-controller/backups`

## Requirements

- macOS
- [SwiftBar](https://github.com/swiftbar/SwiftBar)
- Python 3
- Headroom
- llmtrim
- RTK
- `uv`/`uvx`
- Claude Code and/or the Codex executable bundled with the ChatGPT app

## Installation

Choose a SwiftBar plugin directory, then install the two plugins and their Python helpers:

```sh
mkdir -p "$HOME/Documents/Swiftbar/.support"
cp llm-context-controls.10s.sh controller.py "$HOME/Documents/Swiftbar/"
cp llm-savings-stats.10s.sh "$HOME/Documents/Swiftbar/"
cp stats.py "$HOME/Documents/Swiftbar/.support/stats.py"
chmod +x "$HOME/Documents/Swiftbar/llm-context-controls.10s.sh" \
  "$HOME/Documents/Swiftbar/llm-savings-stats.10s.sh" \
  "$HOME/Documents/Swiftbar/controller.py" \
  "$HOME/Documents/Swiftbar/.support/stats.py"
```

Select that directory in SwiftBar and enable both plugins.

This repository currently reflects a personal Apple Silicon setup and contains paths for Andrew's Homebrew, NVM and user-local installations. Review the constants and `PATH` declarations before installing it on another Mac.

## Tests

```sh
python3 -m unittest discover -s . -p 'test*.py'
```
