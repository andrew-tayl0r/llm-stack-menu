import unittest
from unittest.mock import patch

import controller

from controller import (
    add_headroom_codex_config,
    remove_headroom_claude_env,
    remove_headroom_codex_config,
    remove_llmtrim_claude_integration,
    set_rtk_claude_hook,
    set_headroom_plugin_enabled,
    status_from_values,
)


class ConfigTransformTests(unittest.TestCase):
    def test_remove_headroom_claude_env_preserves_other_hooks(self):
        payload = {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
                "ENABLE_TOOL_SEARCH": "true",
                "USER_SETTING": "keep",
            },
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]},
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "headroom init hook ensure --marker headroom-init-claude"}]},
                ],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "llmtrim guard"}]}
                ],
            },
        }
        result = remove_headroom_claude_env(payload)
        self.assertNotIn("ANTHROPIC_BASE_URL", result["env"])
        self.assertNotIn("ENABLE_TOOL_SEARCH", result["env"])
        self.assertIn("USER_SETTING", result["env"])
        self.assertEqual(len(result["hooks"]["PreToolUse"]), 1)
        self.assertIn("rtk hook claude", str(result["hooks"]))
        self.assertIn("llmtrim guard", str(result["hooks"]))

    def test_rtk_hook_can_be_enabled_and_disabled_without_touching_llmtrim(self):
        payload = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "llmtrim guard"}]}]}}
        enabled = set_rtk_claude_hook(payload, True)
        self.assertIn("rtk hook claude", str(enabled))
        disabled = set_rtk_claude_hook(enabled, False)
        self.assertNotIn("rtk hook claude", str(disabled))
        self.assertIn("llmtrim guard", str(disabled))

    def test_codex_headroom_blocks_are_idempotent_and_reversible(self):
        base = "model = \"gpt-5.6-sol\"\n\n"
        once = add_headroom_codex_config(base)
        twice = add_headroom_codex_config(once)
        self.assertEqual(once, twice)
        self.assertIn("# --- llm-stack-headroom-start ---", once)
        restored = remove_headroom_codex_config(twice)
        self.assertEqual(restored, base.rstrip() + "\n")

    def test_codex_removes_legacy_unmarked_headroom_mcp_block(self):
        text = (
            "[mcp_servers.headroom]\n"
            "command = \"headroom\"\n"
            "args = [\"mcp\", \"serve\"]\n\n"
            "[shell_environment_policy.set]\n"
            "X = \"1\"\n"
        )
        restored = remove_headroom_codex_config(text)
        self.assertNotIn("mcp_servers.headroom", restored)
        self.assertIn("shell_environment_policy.set", restored)

    def test_remove_headroom_claude_env_does_not_collaterally_delete_a_hook_merged_into_the_same_entry(self):
        # set_rtk_claude_hook() reuses an existing matcher="Bash" entry rather
        # than creating its own, so RTK's hook can end up merged into the
        # SAME entry object as Headroom's ensure-hook. remove_headroom_claude_env
        # must strip only Headroom's own hook item, not the whole entry --
        # otherwise re-applying Headroom's routing silently deletes RTK's hook.
        payload = {
            "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787", "ENABLE_TOOL_SEARCH": "true"},
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/Users/andrew/.local/bin/headroom init hook ensure --profile init-user --marker headroom-init-claude",
                                "timeout": 15,
                            },
                            {"type": "command", "command": "rtk hook claude"},
                        ],
                        "matcher": "Bash",
                    },
                ],
            },
        }
        result = remove_headroom_claude_env(payload)
        self.assertIn("rtk hook claude", str(result["hooks"]))
        self.assertNotIn("headroom-init-claude", str(result["hooks"]))
        self.assertNotIn("headroom init hook ensure", str(result["hooks"]))

    def test_codex_removes_stray_provider_block_from_direct_headroom_apply(self):
        # `headroom install apply` can write its own provider block directly,
        # with no matching "# --- end ... ---" marker and different comment
        # text than the controller's own templates use.
        text = (
            "model = \"gpt-5.6-luna\"\n\n"
            "# --- Headroom persistent provider ---\n"
            "model_provider = \"headroom\"\n"
            "openai_base_url = \"http://127.0.0.1:8787/v1\"\n\n"
            "[model_providers.headroom]\n"
            "name = \"Headroom persistent proxy\"\n"
            "base_url = \"http://127.0.0.1:8787/v1\"\n"
            "supports_websockets = true\n"
            "requires_openai_auth = true\n\n"
            "[marketplaces.openai-bundled]\n"
            "source_type = \"local\"\n"
        )
        restored = remove_headroom_codex_config(text)
        self.assertNotIn("headroom", restored.lower())
        self.assertIn("model = \"gpt-5.6-luna\"", restored)
        self.assertIn("[marketplaces.openai-bundled]", restored)

    def test_headroom_claude_env_is_idempotent(self):
        payload = {"env": {"OTHER": "keep"}, "hooks": {"SessionStart": []}}
        once = controller._add_headroom_claude_env(payload)
        twice = controller._add_headroom_claude_env(once)
        self.assertEqual(twice, once)

    def test_normal_mode_removes_llmtrim_hooks_and_statusline_only(self):
        payload = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "llmtrim guard"}]},
                    {"hooks": [{"type": "command", "command": "keep this"}]},
                ]
            },
            "statusLine": {"type": "command", "command": "llmtrim statusline"},
            "env": {"OTHER": "keep"},
        }
        result = remove_llmtrim_claude_integration(payload)
        self.assertNotIn("llmtrim", str(result))
        self.assertIn("keep this", str(result))
        self.assertIn("OTHER", result["env"])

    def test_headroom_plugin_can_be_disabled_without_touching_other_plugins(self):
        payload = {"enabledPlugins": {"headroom@headroom-marketplace": True, "other": True}}
        result = set_headroom_plugin_enabled(payload, False)
        self.assertFalse(result["enabledPlugins"]["headroom@headroom-marketplace"])
        self.assertTrue(result["enabledPlugins"]["other"])

    def test_running_headroom_counts_as_enabled_even_when_upstream_unhealthy(self):
        with patch.object(controller, "_run", return_value=(True, "Status:     running\nHealthy:    no")):
            self.assertTrue(controller._headroom_running())

    def test_status_title_uses_compact_symbol(self):
        self.assertEqual(status_from_values({"mode": "optimized"})["title"], "◈ Optimised")
        self.assertEqual(status_from_values({"mode": "native"})["title"], "◉ Native")
        self.assertEqual(status_from_values({"mode": "mixed"})["title"], "◐ Mixed")


if __name__ == "__main__":
    unittest.main()
