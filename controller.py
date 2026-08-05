#!/usr/bin/env python3
"""Reversible SwiftBar controller for the local LLM optimization stack."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HEADROOM_PROVIDER_BLOCK = '''# --- Headroom init provider ---
model_provider = "headroom"
openai_base_url = "http://127.0.0.1:8787/v1"

[model_providers.headroom]
name = "Headroom init proxy"
base_url = "http://127.0.0.1:8787/v1"
supports_websockets = true
requires_openai_auth = true
# --- end Headroom init provider ---'''

HEADROOM_MCP_BLOCK = '''# --- Headroom MCP server ---
[mcp_servers.headroom]
command = "/Users/andrew/.local/bin/headroom"
args = ["mcp", "serve"]
# --- end Headroom MCP server ---'''

HOME = Path.home()
STATE_DIR = HOME / ".llm-stack-controller"
STATE_FILE = STATE_DIR / "state.json"
BACKUP_DIR = STATE_DIR / "backups"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CODEX_CONFIG = HOME / ".codex" / "config.toml"
HEADROOM_PROFILE = "init-user"
UVX = "/opt/homebrew/bin/uvx"
HEADROOM_BIN = "/Users/andrew/.local/bin/headroom"
CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"


def default_state() -> dict[str, Any]:
    return {
        "mode": "native",
        "remote_control": True,
        "headroom_in_mode": False,
        "components": {
            "headroom": False,
            "llmtrim": False,
            "rtk": False,
            "jcodemunch": False,
        },
        "needs_restart": False,
        "restart_pending": {},
        "busy": False,
        "busy_action": "",
        "message": "Native mode",
    }


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command_for(component: str, action: str) -> list[str]:
    if component == "headroom":
        return ["headroom", "install", action, "--profile", HEADROOM_PROFILE]
    if component == "llmtrim":
        return ["llmtrim", action]
    raise ValueError(f"Unsupported runtime component: {component}")


def headroom_apply_command() -> list[str]:
    return [
        "headroom", "install", "apply", "--profile", HEADROOM_PROFILE,
        "--preset", "persistent-service", "--runtime", "python", "--scope", "user",
        "--providers", "manual", "--target", "claude", "--backend", "anthropic",
        "--port", "8787", "--no-telemetry", "--code-aware",
    ]


def mcp_commands(action: str) -> list[list[str]]:
    if action == "add":
        return [
            ["claude", "mcp", "add", "-s", "user", "jcodemunch", UVX, "jcodemunch-mcp"],
            [CODEX_BIN, "mcp", "add", "jcodemunch", "--", UVX, "jcodemunch-mcp"],
        ]
    if action == "remove":
        return [["claude", "mcp", "remove", "jcodemunch"], [CODEX_BIN, "mcp", "remove", "jcodemunch"]]
    raise ValueError(f"Unsupported MCP action: {action}")


def headroom_claude_mcp_commands(action: str) -> list[str]:
    if action == "add":
        return ["claude", "mcp", "add", "-s", "user", "headroom", HEADROOM_BIN, "mcp", "serve"]
    if action == "remove":
        return ["claude", "mcp", "remove", "headroom"]
    raise ValueError(f"Unsupported Headroom Claude MCP action: {action}")


def _set_headroom_claude_mcp(enabled: bool) -> None:
    if enabled:
        _run(headroom_claude_mcp_commands("remove"), allow_failure=True)
        _run(headroom_claude_mcp_commands("add"), allow_failure=True)
    else:
        _run(headroom_claude_mcp_commands("remove"), allow_failure=True)


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        state = default_state()
        atomic_json_write(STATE_FILE, state)
        return state
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = default_state()
    state = default_state()
    state.update(loaded if isinstance(loaded, dict) else {})
    state["components"] = {**default_state()["components"], **dict(state.get("components") or {})}
    state["headroom_in_mode"] = bool(state.get("headroom_in_mode", False))
    state["restart_pending"] = dict(state.get("restart_pending") or {})
    state["busy"] = bool(state.get("busy", False))
    state["busy_action"] = str(state.get("busy_action", ""))
    return state


def _save_state(state: dict[str, Any]) -> None:
    atomic_json_write(STATE_FILE, state)


def _backup(path: Path) -> None:
    if not path.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)


def _run(command: list[str], *, allow_failure: bool = False) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    if result.returncode and not allow_failure:
        return False, output or f"exit {result.returncode}"
    return result.returncode == 0, output


def _read_claude() -> dict[str, Any]:
    if not CLAUDE_SETTINGS.exists():
        return {}
    return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))


def _write_claude(payload: dict[str, Any]) -> None:
    _backup(CLAUDE_SETTINGS)
    atomic_json_write(CLAUDE_SETTINGS, payload)


def _write_codex(text: str) -> None:
    _backup(CODEX_CONFIG)
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = CODEX_CONFIG.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, CODEX_CONFIG)


def _add_headroom_claude_env(payload: dict[str, Any]) -> dict[str, Any]:
    result = remove_headroom_claude_env(payload)
    env = result.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
    env["ENABLE_TOOL_SEARCH"] = "true"
    hooks = result.setdefault("hooks", {})
    for event, matcher in (("PreToolUse", "Bash"), ("SessionStart", "startup|resume")):
        entries = hooks.setdefault(event, [])
        command = "/Users/andrew/.local/bin/headroom init hook ensure --profile init-user --marker headroom-init-claude"
        if not any("headroom-init-claude" in _hook_command(entry) for entry in entries if isinstance(entries, list)):
            entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command, "timeout": 15}]}
            if matcher:
                entry["matcher"] = matcher
            entries.append(entry)
    return result


def set_headroom_plugin_enabled(payload: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Toggle only the installed Claude Headroom plugin, not other plugins."""

    result = copy.deepcopy(payload)
    plugins = result.setdefault("enabledPlugins", {})
    if isinstance(plugins, dict):
        plugins["headroom@headroom-marketplace"] = enabled
    return result


def remove_llmtrim_claude_integration(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove llmtrim-owned Claude hooks/statusline while preserving other entries."""

    result = copy.deepcopy(payload)
    hooks = result.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            retained_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    retained_entries.append(entry)
                    continue
                nested = entry.get("hooks")
                if isinstance(nested, list):
                    kept_nested = [
                        hook
                        for hook in nested
                        if not (isinstance(hook, dict) and "llmtrim" in str(hook.get("command", "")))
                    ]
                    if kept_nested:
                        updated = copy.deepcopy(entry)
                        updated["hooks"] = kept_nested
                        retained_entries.append(updated)
                elif "llmtrim" not in _hook_command(entry):
                    retained_entries.append(entry)
            hooks[event] = retained_entries
    status_line = result.get("statusLine")
    if isinstance(status_line, dict) and "llmtrim" in str(status_line.get("command", "")):
        result.pop("statusLine", None)
    return result


def _set_headroom_routes(enabled: bool, *, claude: bool = True, codex: bool = True) -> None:
    if claude:
        payload = _read_claude()
        payload = _add_headroom_claude_env(payload) if enabled else remove_headroom_claude_env(payload)
        _write_claude(payload)
    if codex and CODEX_CONFIG.exists():
        _write_codex(add_headroom_codex_config(CODEX_CONFIG.read_text(encoding="utf-8")) if enabled else remove_headroom_codex_config(CODEX_CONFIG.read_text(encoding="utf-8")))


def _set_rtk(enabled: bool) -> None:
    payload = _read_claude()
    _write_claude(set_rtk_claude_hook(payload, enabled))


def _set_jcodemunch(enabled: bool) -> tuple[bool, str]:
    messages: list[str] = []
    if enabled:
        ok, output = _run(["uv", "tool", "install", "jcodemunch-mcp"], allow_failure=True)
        if not ok and "already installed" not in output.lower():
            return False, output
        for command in mcp_commands("remove"):
            _run(command, allow_failure=True)
        for command in mcp_commands("add"):
            ok, output = _run(command)
            if not ok:
                return False, output
            messages.append(output)
        return True, "jCodeMunch registered"
    for command in mcp_commands("remove"):
        ok, output = _run(command, allow_failure=True)
        if output:
            messages.append(output)
    return True, "jCodeMunch disabled"


def _headroom_running() -> bool:
    ok, output = _run(["headroom", "install", "status", "--profile", HEADROOM_PROFILE], allow_failure=True)
    # The service can be running while its upstream health probe is temporarily
    # unavailable (for example, when the network/API is down). Routing is still
    # enabled in that state, so the menu should report the actual process state.
    return ok and "Status:     running" in output


def _client_processes() -> dict[str, list[dict[str, float | int]]]:
    """Return the long-lived Claude/Codex client processes and start times."""

    found: dict[str, list[dict[str, float | int]]] = {"claude": [], "codex": []}
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return found
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) != 7:
            continue
        try:
            pid = int(parts[0])
            started = datetime.strptime(
                " ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"
            ).replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
        command = parts[6]
        if command.startswith(("/bin/", "/usr/bin/", "rtk ")):
            continue
        client = None
        if command.startswith("/Applications/ChatGPT.app/Contents/Resources/codex ") and " app-server" in command:
            client = "codex"
        elif re.search(r"(?:^|/)claude(?:\\s|$)", command) and " mcp " not in command:
            client = "claude"
        if client:
            found[client].append({"pid": pid, "started_at": started})
    return found


def mark_restart_required(state: dict[str, Any], clients: list[str]) -> None:
    current = _client_processes()
    pending = state.setdefault("restart_pending", {})
    for client in clients:
        if current.get(client):
            pending[client] = current[client]
        else:
            pending.pop(client, None)
    state["needs_restart"] = bool(pending)


def refresh_restart_state(state: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("restart_pending") or {}
    if not pending:
        state["needs_restart"] = False
        return state
    current = _client_processes()
    refreshed: dict[str, list[dict[str, float | int]]] = {}
    for client, baseline in pending.items():
        baseline_keys = {(item.get("pid"), item.get("started_at")) for item in baseline}
        current_items = current.get(client, [])
        if any((item.get("pid"), item.get("started_at")) in baseline_keys for item in current_items):
            refreshed[client] = baseline
    state["restart_pending"] = refreshed
    state["needs_restart"] = bool(refreshed)
    return state


def _wait_for_headroom(timeout: float = 8.0) -> bool:
    """Wait briefly for the user service to become running after start."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _headroom_running():
            return True
        time.sleep(0.5)
    return _headroom_running()


def _llmtrim_running() -> bool:
    ok, output = _run(["llmtrim", "status", "--json"], allow_failure=True)
    if not ok:
        return False
    try:
        return bool(json.loads(output).get("daemon", {}).get("running"))
    except json.JSONDecodeError:
        return False


def _rtk_enabled() -> bool:
    try:
        return "rtk hook claude" in CLAUDE_SETTINGS.read_text(encoding="utf-8")
    except OSError:
        return False


def _jcodemunch_enabled() -> bool:
    claude_ok, claude_output = _run(["claude", "mcp", "list"], allow_failure=True)
    codex_ok, codex_output = _run([CODEX_BIN, "mcp", "list"], allow_failure=True)
    return claude_ok and codex_ok and "jcodemunch" in (claude_output + codex_output)


def current_status() -> dict[str, Any]:
    state = _load_state()
    previous_pending = copy.deepcopy(state.get("restart_pending"))
    previous_needs_restart = state.get("needs_restart")
    refresh_restart_state(state)
    components = state["components"]
    components["headroom"] = _headroom_running()
    components["llmtrim"] = _llmtrim_running()
    components["rtk"] = _rtk_enabled()
    components["jcodemunch"] = _jcodemunch_enabled()
    state["components"] = components
    claude_routed = "ANTHROPIC_BASE_URL" in CLAUDE_SETTINGS.read_text(encoding="utf-8")
    codex_routed = CODEX_CONFIG.exists() and "llm-stack-headroom-start" in CODEX_CONFIG.read_text(encoding="utf-8")
    included_components = {
        key: value for key, value in components.items()
        if key != "headroom" or state.get("headroom_in_mode", False)
    }
    routes_ok = not state.get("headroom_in_mode", False) or (claude_routed and codex_routed)
    routes_off = not state.get("headroom_in_mode", False) or (not claude_routed and not codex_routed)
    all_enabled = all(included_components.values()) and routes_ok
    all_disabled = not any(included_components.values()) and routes_off
    state["mode"] = "optimized" if all_enabled else ("native" if all_disabled else "mixed")
    state.update(status_from_values(state))
    if state.get("restart_pending") != previous_pending or state.get("needs_restart") != previous_needs_restart:
        _save_state(state)
    return state


def _set_mode(mode: str) -> tuple[bool, str]:
    state = _load_state()
    if mode == "optimized":
        include_headroom = bool(state.get("headroom_in_mode", False))
        llmtrim_was_running = _llmtrim_running()
        if not llmtrim_was_running:
            _run(command_for("llmtrim", "start"), allow_failure=True)
            _run(["llmtrim", "ensure", "-q"], allow_failure=True)
        startup_note = ""
        if include_headroom:
            _run(headroom_apply_command(), allow_failure=True)
            ok, message = _run(command_for("headroom", "start"), allow_failure=True)
            running = _headroom_running() if not ok else _wait_for_headroom()
            if not running:
                if not llmtrim_was_running:
                    _run(command_for("llmtrim", "stop"), allow_failure=True)
                    _run(["llmtrim", "autostart", "--off"], allow_failure=True)
                state["mode"] = "native"
                state["needs_restart"] = False
                state["restart_pending"] = {}
                state["message"] = (
                    "Headroom could not start; no optimisation changes were applied"
                    + (f": {message}" if message else "")
                )
                _save_state(state)
                return False, state["message"]
            startup_note = "" if ok else "; startup health check pending"
            _set_headroom_routes(True)
            _set_headroom_claude_mcp(True)
        else:
            _set_headroom_routes(False)
            _set_headroom_claude_mcp(False)
        _run(["llmtrim", "autostart", "--off"], allow_failure=True)
        _set_rtk(True)
        if not state["components"].get("jcodemunch"):
            _set_jcodemunch(True)
        state["remote_control"] = False
        state["mode"] = mode
        mark_restart_required(state, ["claude", "codex"])
        suffix = "; Headroom excluded" if not include_headroom else "; relaunch Claude/Codex"
        state["message"] = f"Optimised mode enabled{startup_note}{suffix}"
    else:
        _run(command_for("headroom", "stop"), allow_failure=True)
        _set_headroom_routes(False)
        _set_headroom_claude_mcp(False)
        _run(command_for("llmtrim", "stop"), allow_failure=True)
        _run(["llmtrim", "autostart", "--off"], allow_failure=True)
        _set_rtk(False)
        payload = _read_claude()
        payload = remove_llmtrim_claude_integration(payload)
        payload = set_headroom_plugin_enabled(payload, False)
        _write_claude(payload)
        _set_jcodemunch(False)
        state["remote_control"] = True
        state["mode"] = "native"
        mark_restart_required(state, ["claude", "codex"])
        state["message"] = "Normal mode enabled; Remote Control available after relaunching Claude"
    _save_state(state)
    return True, state["message"]


def _set_headroom_only(enabled: bool, *, route_clients: bool = True) -> tuple[bool, str]:
    if enabled:
        _run(headroom_apply_command(), allow_failure=True)
        ok, message = _run(command_for("headroom", "start"), allow_failure=True)
        running = _headroom_running() if not ok else _wait_for_headroom()
        if not running:
            return False, message or "Headroom could not start"
        if route_clients:
            _set_headroom_routes(True)
            _set_headroom_claude_mcp(True)
            return True, "Headroom enabled; relaunch Claude/Codex"
        _set_headroom_routes(False)
        _set_headroom_claude_mcp(False)
        return True, "Headroom CLI service enabled; GUI routing unchanged"
    _run(command_for("headroom", "stop"), allow_failure=True)
    _set_headroom_routes(False)
    _set_headroom_claude_mcp(False)
    return True, "Headroom disabled; other components unchanged"


def toggle_headroom_participation(enabled: bool | None = None) -> tuple[bool, str]:
    state = _load_state()
    desired = not bool(state.get("headroom_in_mode", False)) if enabled is None else bool(enabled)
    state["headroom_in_mode"] = desired
    if desired:
        message = "Headroom included in Optimised mode"
        if state["components"].get("headroom"):
            _set_headroom_routes(True)
            _set_headroom_claude_mcp(True)
            mark_restart_required(state, ["claude", "codex"])
    else:
        _set_headroom_routes(False)
        _set_headroom_claude_mcp(False)
        message = "Headroom excluded from Optimised mode; CLI use remains available"
        mark_restart_required(state, ["claude", "codex"])
    state["message"] = message
    _save_state(state)
    return True, message


def toggle_component(component: str) -> tuple[bool, str]:
    current = current_status()
    enabled = bool(current["components"].get(component))
    state = _load_state()
    if component == "headroom":
        ok, output = _set_headroom_only(not enabled, route_clients=bool(state.get("headroom_in_mode", False)))
        state["components"][component] = not enabled if ok else enabled
        mark_restart_required(state, ["claude", "codex"])
        state["message"] = output
        _save_state(state)
        return ok, output
    if component == "llmtrim":
        ok, output = _run(command_for(component, "stop" if enabled else "start"), allow_failure=True)
        state["components"][component] = not enabled
        mark_restart_required(state, ["claude", "codex"])
        state["message"] = f"llmtrim {'enabled' if not enabled else 'disabled'}; relaunch clients"
    elif component == "rtk":
        _set_rtk(not enabled)
        state["components"][component] = not enabled
        mark_restart_required(state, ["claude"])
        state["message"] = f"RTK Claude hook {'enabled' if not enabled else 'disabled'}"
    elif component == "jcodemunch":
        ok, output = _set_jcodemunch(not enabled)
        state["components"][component] = not enabled if ok else enabled
        mark_restart_required(state, ["claude", "codex"])
        state["message"] = output
    else:
        return False, f"Unknown component: {component}"
    _save_state(state)
    return ok if component in ("llmtrim", "jcodemunch") else True, state["message"]


def set_remote_control(enabled: bool) -> tuple[bool, str]:
    state = _load_state()
    headroom_on = _headroom_running()
    if enabled:
        _set_headroom_routes(False, claude=True, codex=False)
        state["remote_control"] = True
        mark_restart_required(state, ["claude"])
        state["message"] = "Claude Remote Control enabled; relaunch Claude"
    elif headroom_on:
        _set_headroom_routes(True, claude=True, codex=False)
        state["remote_control"] = False
        mark_restart_required(state, ["claude"])
        state["message"] = "Claude Headroom routing restored; relaunch Claude"
    else:
        state["message"] = "Start Headroom before disabling Native Claude mode"
        _save_state(state)
        return False, state["message"]
    _save_state(state)
    return True, state["message"]


SECTION_COLOR = "#172033,#F3F4F6"
DETAIL_COLOR = "#334155,#D1D5DB"
TOOL_COLORS = {
    "headroom": "#C2410C,#FDBA74",
    "llmtrim": "#1D4ED8,#93C5FD",
    "rtk": "#15803D,#86EFAC",
    "jcodemunch": "#7E22CE,#D8B4FE",
}


def _menu_item(
    text: str,
    color: str | None = None,
    *,
    bold: bool = False,
) -> str:
    leading = text[: len(text) - len(text.lstrip())]
    title = f"{leading}**{text.lstrip()}**" if bold else text
    attributes = []
    if bold:
        attributes.append("md=true")
    if color:
        attributes.append(f"color={color}")
    if text[:1].isspace():
        attributes.append("trim=false")
    return f"{title} | {' '.join(attributes)}" if attributes else title


def _swiftbar_action(action: str, component: str | None = None) -> str:
    plugin = str(Path(__file__).with_name("llm-context-controls.10s.sh"))
    args = f"param1={action}"
    if component:
        args += f" param2={component}"
    return f"bash={plugin} {args} terminal=false refresh=true"


def open_terminal_tool(tool: str) -> tuple[bool, str]:
    commands = {
        "llmtrim": ("llmtrim status --watch", "llmtrim Watch"),
        "rtk": ("rtk gain", "RTK Gain"),
    }
    command, label = commands[tool]
    script = (
        'tell application "Terminal"\n'
        "activate\n"
        f"do script {json.dumps(command)}\n"
        "end tell"
    )
    ok, output = _run(["/usr/bin/osascript", "-e", script], allow_failure=True)
    if not ok:
        return False, output or f"Could not open {label}"
    return True, f"Opened {label}"


def render_menu() -> None:
    state = current_status()
    title = state["title"]
    if state.get("busy"):
        title += " · ⧖ Working"
    pending = [client.title() for client in ("claude", "codex") if state.get("restart_pending", {}).get(client)]
    if pending:
        title += " · ↻ " + " + ".join(pending)
    color = "#35c759" if state["mode"] == "optimized" else ("#ff9f0a" if state["mode"] == "mixed" else "#8e8e93")
    print(f"{title} | color={color}")
    print("---")
    visible_mode = {"optimized": "Optimised", "mixed": "Mixed", "native": "Normal"}[state["mode"]]
    print(_menu_item("Mode", SECTION_COLOR, bold=True))
    print(_menu_item(f"  Current  —  {visible_mode}", DETAIL_COLOR))
    print(f"  Optimised mode | {_swiftbar_action('optimized')} trim=false")
    print(f"  Normal mode (all off) | {_swiftbar_action('native')} trim=false")
    print("---")
    print(_menu_item("Tools", SECTION_COLOR, bold=True))
    symbols = {"headroom": "⌁", "llmtrim": "◒", "rtk": "▱", "jcodemunch": "⌘"}
    labels = {"headroom": "Headroom", "llmtrim": "llmtrim", "rtk": "RTK · Claude hook", "jcodemunch": "jCodeMunch"}
    for component in ("headroom", "llmtrim", "rtk", "jcodemunch"):
        enabled = bool(state["components"].get(component))
        mark = "ON" if enabled else "OFF"
        row_color = TOOL_COLORS[component] if enabled else DETAIL_COLOR
        print(f"  {symbols[component]} {labels[component]}  —  {mark} | {_swiftbar_action('toggle', component)} color={row_color} trim=false")
    remote = bool(state.get("remote_control"))
    remote_color = "#1D4ED8,#93C5FD" if remote else DETAIL_COLOR
    print(f"  ◉ Claude Remote Control  —  {'ON' if remote else 'OFF'} | {_swiftbar_action('remote-control')} color={remote_color} trim=false")
    print("---")
    message = state.get("message", "")
    if state.get("busy"):
        message = f"Working: {state.get('busy_action') or 'processing'}…"
    if pending:
        message = "Relaunch " + " and ".join(pending)
    elif "relaunch" in message.lower() or "restart" in message.lower():
        message = "Settings active"
    print(_menu_item("Status", SECTION_COLOR, bold=True))
    status_color = "#C2410C,#FDBA74" if state.get("busy") or pending else DETAIL_COLOR
    print(_menu_item(f"  {message}", status_color))
    print("---")
    print(_menu_item("GUIs", SECTION_COLOR, bold=True))
    print("  Headroom Dashboard | bash=/usr/bin/open param1=-a param2=Safari param3=http://127.0.0.1:8787/dashboard terminal=false color=" + DETAIL_COLOR + " trim=false")
    print(f"  llmtrim Watch | {_swiftbar_action('open-llmtrim-watch')} color={DETAIL_COLOR} trim=false")
    print(f"  RTK Gain | {_swiftbar_action('open-rtk-gain')} color={DETAIL_COLOR} trim=false")
    print("  jCodeMunch Receipt | bash=/opt/homebrew/bin/uvx param1=jcodemunch-mcp param2=receipt param3=--days param4=0 param5=--by-day terminal=true color=" + DETAIL_COLOR + " trim=false")
    print("---")
    print(_menu_item("Maintenance", SECTION_COLOR, bold=True))
    plugin = str(Path(__file__).with_name("llm-context-controls.10s.sh"))
    print(f"  Check for tool updates | bash={plugin} param1=check-updates terminal=true color={DETAIL_COLOR} trim=false")
    print(f"  Repair menu-bar plugins | bash={plugin} param1=install-plugins terminal=true refresh=true color={DETAIL_COLOR} trim=false")
    print("---")
    print(_menu_item("Settings", SECTION_COLOR, bold=True))
    included = bool(state.get("headroom_in_mode", False))
    included_mark = "ON" if included else "OFF"
    included_color = TOOL_COLORS["headroom"] if included else DETAIL_COLOR
    print(f"  Include Headroom in Optimised mode  —  {included_mark} | {_swiftbar_action('headroom-scope', 'off' if included else 'on')} color={included_color} trim=false")


def set_busy(action: str) -> None:
    state = _load_state()
    state["busy"] = True
    state["busy_action"] = action
    state["message"] = f"Working: {action}…"
    _save_state(state)


def clear_busy() -> None:
    state = _load_state()
    state["busy"] = False
    state["busy_action"] = ""
    if str(state.get("message", "")).startswith("Working:"):
        state["message"] = "Ready"
    _save_state(state)


def check_updates() -> tuple[bool, str]:
    """Print installed versions and non-mutating package update checks."""
    checks = [
        ("Headroom", [HEADROOM_BIN, "--version"]),
        ("llmtrim", ["llmtrim", "--version"]),
        ("RTK", ["rtk", "--version"]),
        ("jCodeMunch", [UVX, "jcodemunch-mcp", "--version"]),
    ]
    lines = ["LLM tool update check (read-only)", ""]
    for name, command in checks:
        ok, output = _run(command, allow_failure=True)
        lines.append(f"{name}: {output or ('unavailable' if not ok else 'no version output')}")
    npm_ok, npm_output = _run(["npm", "outdated", "-g", "--json", "@llmtrim/cli"], allow_failure=True)
    lines.extend(["", "npm outdated (@llmtrim/cli):", npm_output or ("up to date" if npm_ok else "no result")])
    lines.extend(["", "No packages were changed."])
    return True, "\n".join(lines)


def install_plugins() -> tuple[bool, str]:
    """Verify and repair permissions for the two local SwiftBar plugins."""
    plugin_dir = Path(__file__).resolve().parent
    expected = [
        plugin_dir / "llm-context-controls.10s.sh",
        plugin_dir / "llm-savings-stats.10s.sh",
        plugin_dir / ".support" / "stats.py",
    ]
    missing = [path.name for path in expected if not path.exists()]
    if missing:
        return False, "Missing SwiftBar plugins: " + ", ".join(missing)
    for path in expected:
        path.chmod(path.stat().st_mode | 0o111)
    _run(["open", "-a", "SwiftBar"], allow_failure=True)
    return True, f"Verified SwiftBar plugins and support files in {plugin_dir}"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "status"
    if command == "menu":
        render_menu()
        return 0
    if command == "remote-control":
        enabled = not bool(_load_state().get("remote_control", True))
        ok, message = set_remote_control(enabled)
        print(message)
        return 0 if ok else 1
    if command == "status":
        print(json.dumps(current_status(), ensure_ascii=False))
        return 0
    if command == "busy":
        set_busy(" ".join(argv[1:]) or "processing")
        return 0
    if command == "clear-busy":
        clear_busy()
        return 0
    if command in {"optimized", "native"}:
        ok, message = _set_mode(command)
    elif command == "toggle" and len(argv) > 1:
        ok, message = toggle_component(argv[1])
    elif command == "install-jcodemunch":
        ok, message = _set_jcodemunch(True)
        state = _load_state()
        state["components"]["jcodemunch"] = ok
        state["message"] = message
        _save_state(state)
    elif command == "check-updates":
        ok, message = check_updates()
    elif command == "install-plugins":
        ok, message = install_plugins()
    elif command == "headroom-scope":
        requested = argv[1].lower() if len(argv) > 1 else "toggle"
        ok, message = toggle_headroom_participation(None if requested == "toggle" else requested == "on")
    elif command == "open-llmtrim-watch":
        ok, message = open_terminal_tool("llmtrim")
    elif command == "open-rtk-gain":
        ok, message = open_terminal_tool("rtk")
    else:
        print("usage: controller.py status|optimized|native|toggle <component>|headroom-scope [on|off]|install-jcodemunch|check-updates|install-plugins|open-llmtrim-watch|open-rtk-gain")
        return 2
    print(message)
    return 0 if ok else 1


def _hook_command(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return ""
    return " ".join(
        str(item.get("command", "")) for item in hooks if isinstance(item, dict)
    )


def remove_headroom_claude_env(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove only Headroom-owned Claude env and ensure-hook entries."""

    result = copy.deepcopy(payload)
    env = result.get("env")
    if isinstance(env, dict):
        for key in ("ANTHROPIC_BASE_URL", "ENABLE_TOOL_SEARCH"):
            env.pop(key, None)

    hooks = result.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            hooks[event] = [
                entry
                for entry in entries
                if "headroom-init-claude" not in _hook_command(entry)
                and "headroom init hook ensure" not in _hook_command(entry)
            ]
    return result


def set_rtk_claude_hook(payload: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Add/remove the RTK Claude Bash hook while preserving other hooks."""

    result = copy.deepcopy(payload)
    hooks = result.setdefault("hooks", {})
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        entries = []
        hooks["PreToolUse"] = entries

    retained = []
    for entry in entries:
        if not isinstance(entry, dict):
            retained.append(entry)
            continue
        nested = entry.get("hooks")
        if isinstance(nested, list):
            entry = copy.deepcopy(entry)
            entry["hooks"] = [
                hook
                for hook in nested
                if not (isinstance(hook, dict) and hook.get("command") == "rtk hook claude")
            ]
            if entry["hooks"]:
                retained.append(entry)
        elif "rtk hook claude" not in _hook_command(entry):
            retained.append(entry)
    if enabled:
        for entry in retained:
            if isinstance(entry, dict) and entry.get("matcher") == "Bash":
                entry_hooks = entry.setdefault("hooks", [])
                if isinstance(entry_hooks, list):
                    entry_hooks.append({"type": "command", "command": "rtk hook claude"})
                    hooks["PreToolUse"] = retained
                    return result
        retained.insert(
            0,
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "rtk hook claude"}],
            },
        )
    hooks["PreToolUse"] = retained
    return result


def _remove_marked_block(text: str, start: str, end: str) -> str:
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(start)}\n.*?^\s*{re.escape(end)}\n?"
    )
    return pattern.sub("", text)


def remove_headroom_codex_config(text: str) -> str:
    """Remove controller/Headroom marked Codex blocks without touching others."""

    result = _remove_marked_block(text, "# --- llm-stack-headroom-start ---", "# --- llm-stack-headroom-end ---")
    result = _remove_marked_block(result, "# --- Headroom init provider ---", "# --- end Headroom init provider ---")
    result = _remove_marked_block(result, "# --- Headroom MCP server ---", "# --- end Headroom MCP server ---")
    result = re.sub(r"(?ms)^\[mcp_servers\.headroom\]\n.*?(?=^\[|\Z)", "", result)
    result = result.replace("# --- end Headroom MCP server ---\n", "")
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.lstrip("\n")


def add_headroom_codex_config(text: str) -> str:
    """Install the current Headroom Codex provider and MCP blocks idempotently."""

    base = remove_headroom_codex_config(text).rstrip()
    block = (
        "# --- llm-stack-headroom-start ---\n"
        f"{HEADROOM_PROVIDER_BLOCK}\n\n"
        f"{HEADROOM_MCP_BLOCK}\n"
        "# --- llm-stack-headroom-end ---"
    )
    return f"{base}\n\n{block}\n"


def status_from_values(values: dict[str, Any]) -> dict[str, str]:
    mode = str(values.get("mode", "native"))
    if mode == "optimized":
        return {"title": "◈ Optimised", "mode": "optimized"}
    if mode == "mixed":
        return {"title": "◐ Mixed", "mode": "mixed"}
    return {"title": "◉ Native", "mode": "native"}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
