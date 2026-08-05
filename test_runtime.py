import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import controller
from controller import (
    atomic_json_write,
    command_for,
    headroom_apply_command,
    default_state,
    mcp_commands,
    headroom_claude_mcp_commands,
    refresh_restart_state,
    render_menu,
)


class RuntimeHelperTests(unittest.TestCase):
    def test_default_state_is_native_and_has_all_components(self):
        state = default_state()
        self.assertEqual(state["mode"], "native")
        self.assertFalse(state["headroom_in_mode"])
        self.assertEqual(set(state["components"]), {"headroom", "llmtrim", "rtk", "jcodemunch"})

    def test_atomic_json_write_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_json_write(path, {"mode": "optimized"})
            self.assertEqual(json.loads(path.read_text()), {"mode": "optimized"})

    def test_runtime_commands_are_explicit(self):
        self.assertEqual(command_for("headroom", "start"), ["headroom", "install", "start", "--profile", "init-user"])
        self.assertEqual(command_for("llmtrim", "stop"), ["llmtrim", "stop"])

    def test_headroom_apply_command_recreates_the_user_service(self):
        command = headroom_apply_command()
        self.assertEqual(command[:4], ["headroom", "install", "apply", "--profile"])
        self.assertIn("--providers", command)
        self.assertIn("manual", command)
        self.assertIn("--target", command)
        self.assertIn("claude", command)

    def test_mcp_commands_use_uvx_without_hooks(self):
        commands = mcp_commands("add")
        self.assertEqual(commands[0], ["claude", "mcp", "add", "-s", "user", "jcodemunch", "/opt/homebrew/bin/uvx", "jcodemunch-mcp"])
        self.assertEqual(commands[1], ["/Applications/ChatGPT.app/Contents/Resources/codex", "mcp", "add", "jcodemunch", "--", "/opt/homebrew/bin/uvx", "jcodemunch-mcp"])
        self.assertNotIn("--hooks", " ".join(" ".join(command) for command in commands))

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

    def test_turning_headroom_off_does_not_disable_other_components(self):
        state = default_state()
        state["components"] = {"headroom": True, "llmtrim": True, "rtk": True, "jcodemunch": True}
        current = {
            **state,
            "components": dict(state["components"]),
            "mode": "optimized",
            "title": "◈ Optimized",
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
        run.assert_called_once_with(["headroom", "install", "stop", "--profile", "init-user"], allow_failure=True)
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
            patch.object(controller, "_headroom_running", return_value=False),
            patch.object(controller, "_llmtrim_running", return_value=True),
            patch.object(controller, "_rtk_enabled", return_value=True),
            patch.object(controller, "_jcodemunch_enabled", return_value=True),
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

    def test_optimized_mode_continues_when_service_runs_but_readiness_times_out(self):
        state = default_state()
        state["headroom_in_mode"] = True

        def fake_run(command, *, allow_failure=False):
            if command[:3] == ["headroom", "install", "start"]:
                return False, "Deployment did not become ready after start."
            return True, ""

        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state"),
            patch.object(controller, "_run", side_effect=fake_run),
            patch.object(controller, "_headroom_running", return_value=True),
            patch.object(controller, "_set_headroom_routes"),
            patch.object(controller, "_set_rtk"),
        ):
            ok, message = controller._set_mode("optimized")
        self.assertTrue(ok)
        self.assertIn("relaunch Claude/Codex", message)

    def test_optimized_mode_reports_start_failure_without_routing_clients(self):
        state = default_state()
        state["headroom_in_mode"] = True

        with (
            patch.object(controller, "_load_state", return_value=state),
            patch.object(controller, "_save_state") as save_state,
            patch.object(controller, "_run", return_value=(True, "")),
            patch.object(controller, "_wait_for_headroom", return_value=False),
            patch.object(controller, "_set_headroom_routes") as set_routes,
        ):
            ok, message = controller._set_mode("optimized")

        self.assertFalse(ok)
        self.assertIn("Headroom could not start", message)
        save_state.assert_called_once()
        set_routes.assert_not_called()
        self.assertEqual(state["mode"], "native")

    def test_optimized_mode_starts_llmtrim_before_headroom(self):
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
            patch.object(controller, "_wait_for_headroom", return_value=True),
            patch.object(controller, "_set_headroom_routes"),
            patch.object(controller, "_set_headroom_claude_mcp"),
            patch.object(controller, "_set_rtk"),
            patch.object(controller, "_set_jcodemunch", return_value=(True, "")),
        ):
            ok, _ = controller._set_mode("optimized")

        self.assertTrue(ok)
        self.assertLess(
            commands.index(["llmtrim", "start"]),
            commands.index(["headroom", "install", "start", "--profile", "init-user"]),
        )

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
            "components": {"headroom": False, "llmtrim": False, "rtk": False, "jcodemunch": False},
            "remote_control": True,
            "message": "Normal mode",
        }
        with patch.object(controller, "current_status", return_value=state), patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            render_menu()
        self.assertIn("Optimised mode", output.getvalue())
        self.assertIn("Normal mode (all off)", output.getvalue())
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
        self.assertIn('  Current  —  Normal | color=', rendered)
        self.assertIn('  ⌁ Headroom  —  OFF |', rendered)
        self.assertIn('  ◉ Claude Remote Control  —  ON |', rendered)
        self.assertIn("Headroom in Optimised mode", rendered)
        self.assertNotIn('badge=', rendered)
        self.assertLess(rendered.index("  Current  —  Normal"), rendered.index("GUIs"))
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
        self.assertIn('"$PYTHON" "$CONTROLLER" busy', content)
        self.assertIn('"$PYTHON" "$CONTROLLER" clear-busy', content)

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
