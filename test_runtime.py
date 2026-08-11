import json
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import controller
from controller import (
    atomic_json_write,
    command_for,
    default_state,
    mcp_commands,
    xcode_mcp_commands,
    enable_xcode_mcp,
    headroom_claude_mcp_commands,
    refresh_restart_state,
    render_menu,
)


class RuntimeHelperTests(unittest.TestCase):
    def test_default_state_is_native_and_has_all_components(self):
        state = default_state()
        self.assertEqual(state["mode"], "native")
        self.assertFalse(state["headroom_in_mode"])
        self.assertEqual(set(state["components"]), {"headroom", "llmtrim", "rtk", "jcodemunch", "xcode"})
        self.assertEqual(set(state["component_errors"]), {"headroom", "llmtrim", "rtk", "jcodemunch", "xcode"})
        self.assertTrue(all(value is None for value in state["component_errors"].values()))

    def test_atomic_json_write_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_json_write(path, {"mode": "optimized"})
            self.assertEqual(json.loads(path.read_text()), {"mode": "optimized"})

    def test_runtime_commands_are_explicit(self):
        self.assertEqual(command_for("llmtrim", "stop"), ["llmtrim", "stop"])
        # headroom is deliberately unsupported here: the controller must never
        # apply/start/stop the always-on, externally-managed persistent
        # service (see HEADROOM_PROFILE) -- only check its status and toggle
        # client routing.
        with self.assertRaises(ValueError):
            command_for("headroom", "start")

    def test_mcp_commands_use_uvx_without_hooks(self):
        commands = mcp_commands("add")
        self.assertEqual(commands[0], ["claude", "mcp", "add", "-s", "user", "jcodemunch", "/opt/homebrew/bin/uvx", "jcodemunch-mcp"])
        self.assertEqual(commands[1], ["/Applications/ChatGPT.app/Contents/Resources/codex", "mcp", "add", "jcodemunch", "--", "/opt/homebrew/bin/uvx", "jcodemunch-mcp"])
        self.assertNotIn("--hooks", " ".join(" ".join(command) for command in commands))

    def test_xcode_mcp_commands_use_official_bridge(self):
        self.assertEqual(xcode_mcp_commands(), [
            ["claude", "mcp", "add", "-s", "user", "xcode", "xcrun", "mcpbridge"],
            ["/Applications/ChatGPT.app/Contents/Resources/codex", "mcp", "add", "xcode", "--", "xcrun", "mcpbridge"],
        ])

    def test_enable_xcode_mcp_replaces_both_registrations(self):
        with patch.object(controller, "_run", return_value=(True, "")) as run:
            ok, message = enable_xcode_mcp()
        self.assertTrue(ok)
        self.assertIn("configured", message)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["claude", "mcp", "remove", "xcode"],
            ["claude", "mcp", "add", "-s", "user", "xcode", "xcrun", "mcpbridge"],
            ["/Applications/ChatGPT.app/Contents/Resources/codex", "mcp", "remove", "xcode"],
            ["/Applications/ChatGPT.app/Contents/Resources/codex", "mcp", "add", "xcode", "--", "xcrun", "mcpbridge"],
        ])

    def test_restart_state_stays_pending_for_the_same_client_process(self):
        state = default_state()
        state["restart_pending"] = {
            "claude": [{"pid": 42, "started_at": 100.0}],
        }
        with patch.object(
            controller,
            "_client_processes",
            return_value={"claude": [{"pid": 42, "started_at": 100.0}], "codex": []},
        ):
            refresh_restart_state(state)
        self.assertTrue(state["needs_restart"])
        self.assertEqual(state["restart_pending"], {"claude": [{"pid": 42, "started_at": 100.0}]})

    def test_restart_state_clears_when_client_relaunches_or_is_not_running(self):
        state = default_state()
        state["restart_pending"] = {
            "claude": [{"pid": 42, "started_at": 100.0}],
            "codex": [{"pid": 84, "started_at": 100.0}],
        }
        with patch.object(
            controller,
            "_client_processes",
            return_value={"claude": [{"pid": 43, "started_at": 200.0}], "codex": []},
        ):
            refresh_restart_state(state)
        self.assertFalse(state["needs_restart"])
        self.assertEqual(state["restart_pending"], {})

    def test_restart_warning_is_per_client_and_skips_never_started_codex(self):
        state = default_state()
        with patch.object(
            controller,
            "_client_processes",
            return_value={"claude": [{"pid": 42, "started_at": 100.0}], "codex": []},
        ):
            controller.mark_restart_required(state, ["claude", "codex"])
        self.assertIn("claude", state["restart_pending"])
        self.assertNotIn("codex", state["restart_pending"])
        self.assertTrue(state["needs_restart"])

    def test_client_process_detection_finds_claude_cli(self):
        ps_output = "1234 Wed Aug  5 13:00:00 2026 /Users/andrew/bin/claude --resume abc\n"
        completed = subprocess.CompletedProcess(["ps"], 0, ps_output, "")
        with patch.object(controller.subprocess, "run", return_value=completed):
            clients = controller._client_processes()
        self.assertEqual(clients["claude"], [{"pid": 1234, "started_at": 1785934800.0}])

    def test_turning_headroom_off_does_not_disable_other_components(self):
        state = default_state()
        state["components"] = {"headroom": True, "llmtrim": True, "rtk": True, "jcodemunch": True, "xcode": True}
        current = {
            **state,
            "components": dict(state["components"]),
            "mode": "optimized",
            "title": "◈ Optimised",
        }
        with (
            patch.object(controller, "current_status", return_value=current),
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
            patch.object(controller, "_run", return_value=(True, "")) as run,
            patch.object(controller, "_set_headroom_routes") as routes,
            patch.object(controller, "_set_headroom_claude_mcp") as mcp,
            patch.object(controller, "mark_restart_required"),
        ):
            ok, _ = controller.toggle_component("headroom")
        self.assertTrue(ok)
        # Disabling only unrotes clients -- the always-on persistent service
        # must never be started/stopped by the controller.
        run.assert_not_called()
        routes.assert_called_once_with(False)
        mcp.assert_called_once_with(False)
        self.assertTrue(state["components"]["llmtrim"])
        self.assertTrue(state["components"]["rtk"])
        self.assertTrue(state["components"]["jcodemunch"])

    def test_excluded_headroom_does_not_turn_optimised_mode_mixed(self):
        state = default_state()
        state["headroom_in_mode"] = False
        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_headroom_routed", return_value=False),
            patch.object(controller, "_llmtrim_running", return_value=True),
            patch.object(controller, "_rtk_enabled", return_value=True),
            patch.object(controller, "_jcodemunch_enabled", return_value=True),
            patch.object(controller, "_xcode_mcp_enabled", return_value=True),
            patch.object(controller, "_save_state"),
        ):
            status = controller.current_status()
        self.assertEqual(status["mode"], "optimized")

    def test_headroom_participation_toggle_removes_routes_when_excluded(self):
        state = default_state()
        state["headroom_in_mode"] = True
        state["components"]["headroom"] = True
        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
            patch.object(controller, "_set_headroom_routes") as routes,
            patch.object(controller, "_set_headroom_claude_mcp") as mcp,
        ):
            ok, message = controller.toggle_headroom_participation(False)
        self.assertTrue(ok)
        self.assertFalse(state["headroom_in_mode"])
        routes.assert_called_once_with(False)
        mcp.assert_called_once_with(False)
        self.assertIn("excluded", message.lower())

    def test_headroom_claude_mcp_commands_are_explicit(self):
        self.assertEqual(
            headroom_claude_mcp_commands("add"),
            ["claude", "mcp", "add", "-s", "user", "headroom", "/Users/andrew/.local/bin/headroom", "mcp", "serve"],
        )
        self.assertEqual(headroom_claude_mcp_commands("remove"), ["claude", "mcp", "remove", "headroom"])

    def test_optimized_mode_routes_headroom_immediately_when_already_running(self):
        # No apply/start/wait step anymore -- the persistent service is
        # always-on and externally managed, so enabling routing is a single
        # synchronous health check plus a config write.
        state = default_state()
        state["headroom_in_mode"] = True

        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
            patch.object(controller, "_run", return_value=(True, "")),
            patch.object(controller, "_headroom_running", return_value=True),
            patch.object(controller, "_set_headroom_routes") as set_routes,
            patch.object(controller, "_set_headroom_claude_mcp"),
            patch.object(controller, "_set_rtk"),
        ):
            ok, message = controller._set_mode("optimized")
        self.assertTrue(ok)
        self.assertIn("relaunch Claude/Codex", message)
        set_routes.assert_called_once_with(True)

    def test_optimized_mode_reports_failure_without_routing_when_service_is_down(self):
        state = default_state()
        state["headroom_in_mode"] = True

        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state") as save_state,
            patch.object(controller, "_run", return_value=(True, "")),
            patch.object(controller, "_headroom_running", return_value=False),
            patch.object(controller, "_set_headroom_routes") as set_routes,
        ):
            ok, message = controller._set_mode("optimized")

        self.assertFalse(ok)
        self.assertIn("isn't running", message)
        save_state.assert_called_once()
        set_routes.assert_not_called()
        self.assertEqual(state["mode"], "native")

    def test_optimized_mode_starts_llmtrim_and_checks_headroom_health(self):
        state = default_state()
        state["headroom_in_mode"] = True
        commands = []

        def fake_run(command, *, allow_failure=False):
            commands.append(command)
            return True, ""

        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
            patch.object(controller, "_run", side_effect=fake_run),
            patch.object(controller, "_llmtrim_running", return_value=False),
            patch.object(controller, "_headroom_running", return_value=True) as running,
            patch.object(controller, "_set_headroom_routes"),
            patch.object(controller, "_set_headroom_claude_mcp"),
            patch.object(controller, "_set_rtk"),
            patch.object(controller, "_set_jcodemunch", return_value=(True, "")),
        ):
            ok, _ = controller._set_mode("optimized")

        self.assertTrue(ok)
        self.assertIn(["llmtrim", "start"], commands)
        running.assert_called_once()

    def test_remote_control_only_removes_claude_route(self):
        state = default_state()
        state["remote_control"] = False
        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
            patch.object(controller, "_headroom_running", return_value=True),
            patch.object(controller, "_set_headroom_routes") as set_routes,
        ):
            ok, _ = controller.set_remote_control(True)
        self.assertTrue(ok)
        set_routes.assert_called_once_with(False, claude=True, codex=False)

    def test_menu_names_the_two_user_modes(self):
        state = {
            "title": "◉ Native",
            "mode": "native",
            "needs_restart": False,
            "components": {"headroom": False, "llmtrim": False, "rtk": False, "jcodemunch": False, "xcode": False},
            "remote_control": True,
            "message": "Native mode",
        }
        with patch.object(controller, "current_status", return_value=state), patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            render_menu()
        self.assertIn("Optimised mode", output.getvalue())
        self.assertIn("Native mode (all off)", output.getvalue())
        self.assertIn("Headroom Dashboard", output.getvalue())
        self.assertIn("GUIs", output.getvalue())
        self.assertIn("llmtrim Watch", output.getvalue())
        self.assertIn("RTK Gain", output.getvalue())
        self.assertIn("param1=open-llmtrim-watch terminal=false", output.getvalue())
        self.assertIn("param1=open-rtk-gain terminal=false", output.getvalue())
        self.assertNotIn("llmtrim-tray", output.getvalue())
        self.assertNotIn("param1=gain", output.getvalue())
        self.assertIn("jCodeMunch Receipt", output.getvalue())
        self.assertIn("param3=--days param4=0", output.getvalue())
        self.assertIn("Check for tool updates", output.getvalue())
        self.assertIn("Repair menu-bar plugins", output.getvalue())
        self.assertIn("Enable Xcode MCP bridge", output.getvalue())
        self.assertIn("Xcode MCP", output.getvalue())
        self.assertIn("**Mode** | md=true color=", output.getvalue())
        self.assertIn("**Tools** | md=true color=", output.getvalue())
        self.assertIn("**Status** | md=true color=", output.getvalue())
        self.assertIn("**GUIs** | md=true color=", output.getvalue())
        self.assertIn("**Maintenance** | md=true color=", output.getvalue())
        self.assertNotIn("\nSTATUS\n", output.getvalue())
        self.assertNotIn("\nMAINTENANCE\n", output.getvalue())
        rendered = output.getvalue()
        self.assertLess(rendered.index("Status"), rendered.index("GUIs"))
        self.assertLess(rendered.index("GUIs"), rendered.index("Maintenance"))
        self.assertIn('  Current  —  Native | color=', rendered)
        self.assertIn('  ⌁ Headroom  —  OFF |', rendered)
        self.assertIn('  ◉ Claude Remote Control  —  ON |', rendered)
        self.assertIn("Include Headroom in Optimised mode", rendered)
        self.assertLess(rendered.index("Maintenance"), rendered.index("Settings"))
        self.assertNotIn('badge=', rendered)
        self.assertLess(rendered.index("  Current  —  Native"), rendered.index("GUIs"))
        indented_rows = [line for line in rendered.splitlines() if line.startswith("  ") and line.strip()]
        self.assertTrue(indented_rows)
        self.assertTrue(all("trim=false" in line for line in indented_rows))
        self.assertTrue(all("md=true" not in line and "**" not in line for line in indented_rows))
        self.assertNotIn("Open Claude GUI (jCodeMunch MCP)", output.getvalue())
        self.assertNotIn("Open Codex GUI (jCodeMunch MCP)", output.getvalue())
        self.assertNotIn("RTK — no standalone GUI", output.getvalue())
        self.assertNotIn("Open Headroom status in Terminal", output.getvalue())
        self.assertNotIn("Open Headroom performance report in Terminal", output.getvalue())
        self.assertNotIn("Check LLM tool updates", output.getvalue())
        self.assertNotIn("Repair SwiftBar plugins", output.getvalue())
        self.assertNotIn("Install / refresh jCodeMunch", output.getvalue())

    def test_run_exclusive_rejects_instead_of_racing_when_already_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "controller.lock"
            state_dir = Path(directory)
            state = default_state()
            state["busy_action"] = "RTK · Claude hook"
            calls = []

            def fake_func():
                calls.append("ran")
                return True, "ok"

            with (
                patch.object(controller, "LOCK_FILE", lock_path),
                patch.object(controller, "STATE_DIR", state_dir),
                patch.object(controller, "_load_state", return_value=state),
            ):
                # Simulate another invocation already holding the lock.
                holder = open(lock_path, "a+")
                import fcntl as _fcntl
                _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                try:
                    ok, message, ran = controller.run_exclusive("headroom", fake_func)
                finally:
                    _fcntl.flock(holder, _fcntl.LOCK_UN)
                    holder.close()
        self.assertFalse(ok)
        self.assertFalse(ran)
        self.assertIn("RTK · Claude hook", message)
        self.assertIn("Busy", message)
        self.assertEqual(calls, [])

    def test_run_exclusive_runs_and_clears_busy_when_lock_is_free(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "controller.lock"
            state_dir = Path(directory)
            state = default_state()
            saved = []
            with (
                patch.object(controller, "LOCK_FILE", lock_path),
                patch.object(controller, "STATE_DIR", state_dir),
                patch.object(controller, "_load_state", return_value=state),
                patch.object(controller, "_save_state", side_effect=lambda s: saved.append(dict(s))),
            ):
                ok, message, ran = controller.run_exclusive("rtk", lambda: (True, "done"))
        self.assertTrue(ok)
        self.assertTrue(ran)
        self.assertEqual(message, "done")
        # busy was set true then cleared back to false around the call
        busy_values = [entry["busy"] for entry in saved]
        self.assertIn(True, busy_values)
        self.assertFalse(saved[-1]["busy"])

    def test_record_component_result_stores_and_clears_errors(self):
        state = default_state()
        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
        ):
            controller._record_component_result("rtk", False, "start failed")
        self.assertEqual(state["component_errors"]["rtk"], "start failed")
        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
        ):
            controller._record_component_result("rtk", True, "started")
        self.assertIsNone(state["component_errors"]["rtk"])

    def test_menu_shows_per_component_error(self):
        state = {
            "title": "◐ Mixed",
            "mode": "mixed",
            "needs_restart": False,
            "restart_pending": {},
            "busy": False,
            "busy_action": "",
            "components": {"headroom": False, "llmtrim": True, "rtk": False, "jcodemunch": True, "xcode": True},
            "component_errors": {"rtk": "start failed: exit 1"},
            "remote_control": True,
            "message": "Ready",
        }
        with patch.object(controller, "current_status", return_value=state), patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            render_menu()
        rendered = output.getvalue()
        self.assertIn("start failed: exit 1", rendered)
        # The error shows in Status, attributed to the component, not just a
        # generic "X is off" restatement of what the Tools row already shows.
        self.assertIn("RTK · Claude hook: start failed: exit 1", rendered)

    def test_menu_shows_turning_on_progress_for_the_busy_component(self):
        state = {
            "title": "◐ Mixed",
            "mode": "mixed",
            "needs_restart": False,
            "restart_pending": {},
            "busy": True,
            "busy_action": "RTK · Claude hook",
            "components": {"headroom": False, "llmtrim": True, "rtk": False, "jcodemunch": True, "xcode": True},
            "component_errors": {},
            "remote_control": True,
            "message": "Working: RTK · Claude hook…",
        }
        with patch.object(controller, "current_status", return_value=state), patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            render_menu()
        rendered = output.getvalue()
        rtk_line = next(line for line in rendered.splitlines() if "RTK · Claude hook" in line and "▱" in line)
        self.assertIn("working…", rtk_line)

    def test_menu_shows_working_state_during_an_action(self):
        state = {
            "title": "◉ Native",
            "mode": "native",
            "needs_restart": False,
            "restart_pending": {},
            "busy": True,
            "busy_action": "Optimised mode",
            "components": {"headroom": False, "llmtrim": False, "rtk": False, "jcodemunch": False},
            "remote_control": True,
            "message": "Working…",
        }
        with patch.object(controller, "current_status", return_value=state), patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            render_menu()
        self.assertIn("⧖ Working", output.getvalue())
        self.assertIn("Optimised mode", output.getvalue())

    def test_swiftbar_wrapper_exposes_homebrew_and_local_tools(self):
        plugin = Path("/Users/andrew/Documents/Swiftbar/llm-context-controls.10s.sh")
        content = plugin.read_text()
        self.assertIn("/opt/homebrew/bin", content)
        self.assertIn("/Users/andrew/.local/bin", content)
        self.assertIn("/Users/andrew/.nvm/versions/node/v24.18.0/bin", content)
        # controller.py now owns the busy flag and locking for every mutating
        # command, so the wrapper just forwards args instead of orchestrating
        # separate busy/clear-busy calls around each action.
        self.assertNotIn('"$PYTHON" "$CONTROLLER" busy', content)
        self.assertNotIn('"$PYTHON" "$CONTROLLER" clear-busy', content)
        self.assertIn('"$PYTHON" "$CONTROLLER" "$@"', content)

    def test_terminal_shortcuts_run_the_requested_commands(self):
        with patch.object(controller, "_run", return_value=(True, "")) as run:
            ok, message = controller.open_terminal_tool("llmtrim")
        self.assertTrue(ok)
        self.assertEqual(message, "Opened llmtrim Watch")
        llmtrim_command = run.call_args.args[0]
        self.assertEqual(llmtrim_command[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('do script "llmtrim status --watch"', llmtrim_command[2])

        with patch.object(controller, "_run", return_value=(True, "")) as run:
            ok, message = controller.open_terminal_tool("rtk")
        self.assertTrue(ok)
        self.assertEqual(message, "Opened RTK Gain")
        rtk_command = run.call_args.args[0]
        self.assertEqual(rtk_command[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('do script "rtk gain"', rtk_command[2])

    def test_rejected_toggle_does_not_pollute_the_target_components_own_error(self):
        # Regression: a click rejected because another action is busy must not
        # be recorded as component_errors["rtk"] = "Busy: ..." — RTK never ran
        # and isn't actually broken, it just didn't get a turn.
        with (
            patch.object(controller, "run_exclusive", return_value=(False, "Busy: Headroom is still running — try again in a moment", False)),
            patch.object(controller, "_record_component_result") as record,
            patch.object(sys, "stdout", new_callable=io.StringIO),
        ):
            status = controller.main(["toggle", "rtk"])
        self.assertEqual(status, 1)
        record.assert_not_called()

    def test_terminal_shortcut_actions_are_dispatched(self):
        with (
            patch.object(controller, "open_terminal_tool", return_value=(True, "Opened")) as launch,
            patch.object(sys, "stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(controller.main(["open-llmtrim-watch"]), 0)
            self.assertEqual(controller.main(["open-rtk-gain"]), 0)
        self.assertEqual([call.args[0] for call in launch.call_args_list], ["llmtrim", "rtk"])

    def test_swiftbar_controller_does_not_override_disabled_preference(self):
        plugin = Path("/Users/andrew/Documents/Swiftbar/llm-context-controls.10s.sh").read_text()
        plist = Path("/Users/andrew/Library/LaunchAgents/com.andrew.swiftbar-llm-stack.plist").read_text()
        self.assertIn("swiftbar.hideDisablePlugin", plugin)
        self.assertNotIn("swiftbar://enableplugin?name=llm-context-controls.10s.sh", plist)



if __name__ == "__main__":
    unittest.main()
