# LLM Stack Menu

SwiftBar plugins that provide menu-bar controls and live savings statistics for a Claude Code-focused token-optimisation setup on macOS, with Codex support.

The controller manages Headroom, llmtrim, RTK and jCodeMunch from one menu:

- **Optimised mode** starts llmtrim, enables RTK's Claude Code command-filtering hook, and registers the managed MCP integrations. Headroom is included only when its participation setting is enabled.
- **Native mode** stops the background services and removes the routes, hooks and MCP registrations managed by this plugin, returning Claude Code and Codex to their native configuration when you are not coding.
- **Mixed mode** appears automatically when only some tools are enabled. Individual switches let you choose the exact combination.

Headroom has a separate **Headroom in Optimised mode** setting. Leave it off to keep Headroom available for an explicitly launched CLI session without routing the Claude GUI or affecting the green Optimised status. Turn it on only when you want Headroom to participate in the managed client configuration.

The separate statistics item combines each tool's reported token savings while keeping today, rolling-window and lifetime scopes distinct.

## Features

- One-click Optimised and Native modes
- Mixed-state detection and independent tool switches
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
