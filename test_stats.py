import unittest
from datetime import date

from stats import (
    _decode_json_output,
    combined_saved_usd,
    format_tokens,
    parse_headroom,
    parse_jcodemunch,
    parse_llmtrim,
    parse_rtk,
    render_menu,
)


class StatsParsingTests(unittest.TestCase):
    def test_json_decoder_ignores_headroom_provider_banner(self):
        payload = _decode_json_output("\n\\x1b[31mProvider List: https://example.test\\x1b[0m\n{\"saved\": 1}\n")
        self.assertEqual(payload, {"saved": 1})

    def test_headroom_parses_tokens_percentage_tool_savings_and_model_cost(self):
        source = parse_headroom(
            {
                "window_hours": 168.0,
                "total_tokens_before": 1_000_000,
                "total_tokens_after": 700_000,
                "tokens_saved": 300_000,
                "tool_saved": 12_000,
                "savings_pct": 30.0,
                "total_requests": 7,
                "cache_read_tokens": 4_000,
                "cache_write_tokens": 2_000,
                "overhead": {"optimization_ms": {"average_ms": 12.5, "p95_ms": 33.0, "slow_request_count": 1, "slow_request_pct": 14.3}},
                "by_transform": [{"transform": "content_router", "uses": 3, "tokens_saved": 100}],
                "by_model": [
                    {"tokens_saved": 200_000, "list_price_per_mtok": 5.0},
                    {"tokens_saved": 100_000, "list_price_per_mtok": 2.0},
                ],
            }
        )
        self.assertEqual(source.name, "Headroom")
        self.assertEqual(source.tokens_saved, 300_000)
        self.assertEqual(source.savings_pct, 30.0)
        self.assertEqual(source.saved_usd, 1.2)
        self.assertIn("12,000", " ".join(source.detail))
        self.assertEqual(source.scope_label, "Last 168 hours")
        details = " ".join(source.detail)
        self.assertIn("Requests: 7", details)
        self.assertIn("Cache read/write: 4K / 2K", details)
        self.assertIn("Overhead: 12.5 ms avg · 33.0 ms p95 · 1 slow (14.3%)", details)
        self.assertIn("Top transform: content_router · 3 uses", details)

        today = parse_headroom(
            {"window_hours": 168, "tokens_saved": 300_000, "savings_pct": 30.0, "tool_saved": 12_000},
            scope="today",
        )
        self.assertEqual(today.tokens_saved, 300_000)
        self.assertEqual(today.scope_label, "Last 168 hours")

    def test_llmtrim_uses_input_tokens_and_reported_money(self):
        source = parse_llmtrim(
            {
                "input": {"before": 10_000_000, "after": 8_000_000, "saved_pct": 20.0},
                "output": {"before": 0, "after": 100_000, "saved_pct": 0.0},
                "requests": 42,
                "added_latency_ms": 28.9,
                "money": {
                    "saved_usd": 7.8056,
                    "paid_usd": 55.32,
                    "would_have_usd": 63.13,
                    "saved_today_usd": 7.52,
                    "turns": 41,
                    "coverage": {"compressions_events": 42, "breakdown_turns": 41, "ratio": 41 / 42},
                },
                "by_model": [
                    {"model": "gpt-5.6-sol", "requests": 20, "saved_pct": 25.0, "cost_saved_usd": 3.30},
                    {"model": "claude-sonnet-5", "requests": 5, "saved_pct": 40.0, "cost_saved_usd": 0.45},
                ],
            }
        )
        self.assertEqual(source.tokens_saved, 2_000_000)
        self.assertEqual(source.savings_pct, 20.0)
        self.assertEqual(source.saved_usd, 7.8056)
        details = " ".join(source.detail)
        self.assertIn("Input/output: 10M → 8M / 0 → 100K", details)
        self.assertIn("Cost: $55.32 paid vs $63.13 untrimmed", details)
        self.assertIn("Savings: $7.81 total ($7.52 today)", details)
        self.assertIn("Added delay: ~28.9 ms/request", details)
        self.assertIn("Turns covered: 41/42", details)
        self.assertIn("Top models", details)
        self.assertIn("gpt-5.6-sol · 20 requests", details)

    def test_llmtrim_today_uses_the_calendar_day_bucket(self):
        source = parse_llmtrim(
            {
                "input": {"before": 99_000, "after": 1_000, "saved_pct": 99.0},
                "output": {"before": 0, "after": 9_000},
                "requests": 999,
                "added_latency_ms": 28.9,
                "money": {"saved_usd": 9.99, "saved_today_usd": 0.42, "paid_usd": 55.32, "would_have_usd": 63.13, "turns": 41, "coverage": {"compressions_events": 42, "ratio": 41 / 42}},
                "breakdown": {"sessions": [{"id": "one"}, {"id": "two"}]},
                "by_model": [{"model": "gpt-5.6-sol", "requests": 20, "saved_pct": 25.0, "cost_saved_usd": 3.30}],
                "by_period": [
                    {"period": date.today().isoformat(), "requests": 3, "input_before": 10_000, "input_after": 7_000, "output_before": 100, "output_after": 80}
                ],
            },
            scope="today",
        )
        self.assertEqual(source.tokens_saved, 3_020)
        self.assertAlmostEqual(source.savings_pct, 29.900990099)
        self.assertEqual(source.saved_usd, 0.42)
        details = " ".join(source.detail)
        self.assertEqual(source.scope_label, "Today")
        self.assertIn("Requests handled: 3", details)
        self.assertIn("Savings today: $0.42", details)
        self.assertIn("Lifetime cost: $55.32 paid vs $63.13 untrimmed", details)
        self.assertIn("Lifetime added delay: ~28.9 ms/request", details)
        self.assertIn("Lifetime sessions covered: 2", details)
        self.assertIn("Top models · lifetime", details)
        self.assertIn("gpt-5.6-sol · 20 requests", details)
        self.assertNotIn("Cost: $9.99", details)

    def test_rtk_uses_global_summary_without_inventing_dollars(self):
        source = parse_rtk(
            {"summary": {"total_commands": 3733, "total_input": 50_400_000, "total_output": 1_900_000, "total_saved": 48_500_000, "avg_savings_pct": 96.3, "total_time_ms": 11266000, "avg_time_ms": 3010}}
        )
        self.assertEqual(source.tokens_saved, 48_500_000)
        self.assertEqual(source.savings_pct, 96.3)
        self.assertIsNone(source.saved_usd)
        self.assertEqual(source.scope_label, "Lifetime")
        self.assertIn("Commands: 3,733", source.detail)
        self.assertIn("Input/output: 50.4M / 1.9M", source.detail)

    def test_rtk_today_uses_the_calendar_day_row(self):
        source = parse_rtk(
            {
                "summary": {"total_commands": 10_000, "total_saved": 9_000, "avg_savings_pct": 90},
                "daily": [
                    {"date": date.today().isoformat(), "commands": 4, "input_tokens": 1_000, "output_tokens": 100, "saved_tokens": 700, "savings_pct": 63.6, "total_time_ms": 2_000, "avg_time_ms": 500}
                ],
            },
            scope="today",
        )
        self.assertEqual(source.tokens_saved, 700)
        self.assertAlmostEqual(source.savings_pct, 63.6)
        self.assertEqual(source.scope_label, "Today")
        self.assertIn("Commands: 4", source.detail)

    def test_combined_dollars_only_sums_priced_sources(self):
        headroom = parse_headroom(
            {"tokens_saved": 100, "savings_pct": 1, "tool_saved": 0, "by_model": [{"tokens_saved": 100, "list_price_per_mtok": 5}]}
        )
        llmtrim = parse_llmtrim({"input": {}, "output": {}, "money": {"saved_usd": 2.25}})
        rtk = parse_rtk({"summary": {"total_saved": 50, "avg_savings_pct": 5}})
        self.assertAlmostEqual(combined_saved_usd([headroom, llmtrim, rtk]), 2.2505)

    def test_jcodemunch_receipt_reports_measured_savings(self):
        source = parse_jcodemunch(
            {
                "totals": {"calls": 12, "actual_tokens": 800, "baseline_tokens": 10_000, "savings_tokens": 9_200},
                "savings_usd": 0.42,
                "model": "opus",
                "window": {"days": 30, "since": "2026-07-06T00:00:00Z"},
            }
        )
        self.assertEqual(source.symbol, "⌘")
        self.assertEqual(source.tokens_saved, 9_200)
        self.assertEqual(source.savings_pct, 92.0)
        self.assertEqual(source.saved_usd, 0.42)
        self.assertIn("Calls: 12", source.detail)

    def test_jcodemunch_zero_day_receipt_is_labelled_all_time(self):
        source = parse_jcodemunch(
            {"totals": {"calls": 1, "actual_tokens": 10, "baseline_tokens": 20, "savings_tokens": 10}, "window": {"days": 0}}
        )
        self.assertEqual(source.scope_label, "All recorded usage")

    def test_jcodemunch_today_uses_the_calendar_day_row(self):
        source = parse_jcodemunch(
            {
                "totals": {"calls": 100, "actual_tokens": 20_000, "baseline_tokens": 30_000, "savings_tokens": 10_000},
                "savings_usd": 1.2,
                "model": "opus",
                "window": {"days": 30, "since": "2026-07-06T00:00:00Z"},
                "by_day": [
                    {"date": date.today().isoformat(), "calls": 2, "actual_tokens": 800, "baseline_tokens": 1_000, "savings_tokens": 200}
                ],
            },
            scope="today",
        )
        self.assertEqual(source.tokens_saved, 200)
        self.assertEqual(source.savings_pct, 20.0)
        self.assertEqual(source.scope_label, "Today")
        self.assertIn("Calls: 2", source.detail)
        self.assertNotIn("Since:", " ".join(source.detail))


class StatsRenderingTests(unittest.TestCase):
    def test_token_format_is_compact_and_stable(self):
        self.assertEqual(format_tokens(0), "0")
        self.assertEqual(format_tokens(1_234), "1.2K")
        self.assertEqual(format_tokens(3_400_000), "3.4M")

    def test_menu_has_always_visible_total_and_detailed_tool_rows(self):
        sources = [
            parse_headroom({"tokens_saved": 0, "savings_pct": 0, "tool_saved": 0, "by_model": []}),
            parse_llmtrim({"input": {"before": 10_000, "after": 8_000, "saved_pct": 20}, "output": {"before": 0, "after": 100_000}, "money": {"saved_usd": 1.5, "paid_usd": 8.0, "would_have_usd": 9.5, "saved_today_usd": 1.25}}),
            parse_rtk({"summary": {"total_saved": 50_000, "avg_savings_pct": 96.3}}),
            parse_jcodemunch({"totals": {}, "savings_usd": 0.0, "window": {"days": 30}}),
        ]
        output = render_menu(sources)
        self.assertFalse(output.splitlines()[0].startswith("Σ "))
        self.assertTrue(output.splitlines()[0].startswith("→│← 2K / 20%   ▱ 50K / 96%   ⌁ 0 / 0%"))
        self.assertNotIn("$", output.splitlines()[0])
        self.assertIn('  Total tokens saved  —  52K | color=', output)
        self.assertIn('  Total tracked savings  —  $1.50 USD | color=', output)
        self.assertNotIn("Savings are reported by each tool", output)
        self.assertIn("⌁ Headroom", output)
        self.assertIn("→│← llmtrim", output)
        self.assertIn("▱ RTK", output)
        self.assertIn("⌘ jCodeMunch", output)
        self.assertIn('  Saved  —  ', output)
        self.assertNotIn("Saved dollars", output)
        self.assertIn('  Cost  —  $8.00 paid vs $9.50 untrimmed', output)
        self.assertIn('  Savings  —  $1.50 total ($1.25 today)', output)
        self.assertIn('  Input/output  —  10K → 8K / 0 → 100K', output)
        self.assertIn("→│← llmtrim · Lifetime", output)
        self.assertLess(output.index("→│← llmtrim"), output.index("⌁ Headroom"))
        self.assertLess(output.index("▱ RTK"), output.index("⌁ Headroom"))
        self.assertNotIn("Global scope", output)
        self.assertNotIn("Dollar value: unavailable", output)
        self.assertNotIn("Open llmtrim full breakdown in Terminal", output)
        self.assertNotIn("Check for tool updates", output)
        self.assertNotIn("Repair menu-bar plugins", output)

    def test_menu_shows_today_first_and_lifetime_totals_below(self):
        today_sources = [
            parse_llmtrim(
                {"input": {"before": 10_000, "after": 8_000}, "output": {}, "money": {"saved_today_usd": 0.25}, "by_period": [{"period": date.today().isoformat(), "requests": 1, "input_before": 10_000, "input_after": 8_000, "output_before": 0, "output_after": 0}]},
                scope="today",
            ),
            parse_rtk({"summary": {}, "daily": [{"date": date.today().isoformat(), "commands": 1, "input_tokens": 100, "output_tokens": 0, "saved_tokens": 50, "savings_pct": 50}]}, scope="today"),
        ]
        lifetime_sources = [
            parse_llmtrim({"input": {"before": 100_000, "after": 80_000}, "output": {}, "money": {"saved_usd": 3.0}}),
            parse_rtk({"summary": {"total_saved": 500, "avg_savings_pct": 80}}),
        ]
        output = render_menu(today_sources, lifetime_sources=lifetime_sources)
        self.assertTrue(output.splitlines()[0].startswith("→│← 2K / 20%   ▱ 50").__bool__())
        self.assertIn('  Tokens saved  —  2K | color=', output)
        self.assertIn('  Tracked savings (priced sources)  —  $0.25 USD', output)
        self.assertIn('  Tokens saved  —  20.5K | color=', output)
        self.assertNotIn("Lifetime tokens saved", output)
        self.assertNotIn("Lifetime tracked savings", output)
        self.assertLess(output.index("Lifetime totals"), output.index("Tools"))
        self.assertLess(output.index("→│← llmtrim"), output.index("▱ RTK"))
        self.assertIn("**Today** | md=true color=", output)
        self.assertIn("**→│← llmtrim · Today** | md=true color=", output)
        self.assertIn("**▱ RTK · Today** | md=true color=", output)
        self.assertNotIn("CALENDAR DAY", output)
        self.assertNotIn("Today tokens saved", output)

    def test_tools_heading_is_not_followed_by_a_divider(self):
        output = render_menu([
            parse_llmtrim({"input": {}, "output": {}, "by_period": []}, scope="today"),
        ], lifetime_sources=[])
        lines = output.splitlines()
        tools_index = next(index for index, line in enumerate(lines) if "**Tools**" in line)
        self.assertNotEqual(lines[tools_index + 1], "---")

    def test_llmtrim_detail_subsections_have_hierarchy_and_indented_stats(self):
        output = render_menu([
            parse_llmtrim(
                {"input": {}, "output": {}, "money": {}, "by_model": [{"model": "opus", "requests": 1}]},
                scope="today",
            ),
        ])
        self.assertIn("  **Lifetime** | md=true color=", output)
        self.assertIn("  **Top models** | md=true color=", output)
        self.assertIn("    Lifetime days with data  —  0 | color=", output)
        self.assertIn("    opus  —  1 requests · 0.0% · $0.00", output)
        self.assertNotIn("    **Lifetime days with data:**", output)
        self.assertNotIn("Lifetime breakdown", output)
        self.assertNotIn("Top models · lifetime", output)

    def test_stat_rows_are_regular_and_indented_beneath_headings(self):
        output = render_menu([
            parse_rtk({"summary": {"total_saved": 100, "avg_savings_pct": 50, "total_commands": 2, "total_input": 200, "total_output": 20}}),
        ])
        self.assertIn("  Saved  —  100 · 50.0% | color=", output)
        self.assertIn("  Commands  —  2 | color=", output)
        self.assertIn("  Input/output  —  200 / 20 | color=", output)
        metric_rows = [line for line in output.splitlines() if line.startswith("  ") and not line.startswith("  **") and line.strip()]
        self.assertTrue(metric_rows)
        self.assertTrue(all("md=true" not in line and "**" not in line for line in metric_rows))

    def test_llmtrim_places_the_spacer_after_the_model_list(self):
        output = render_menu([
            parse_llmtrim(
                {"input": {}, "output": {}, "money": {}, "by_period": [{"period": "2026-08-05"}], "by_model": [{"model": "opus", "requests": 1}]},
                scope="today",
            ),
        ])
        self.assertIn('    Lifetime days with data  —  1 | color=#334155,#D1D5DB trim=false\n \n  **Top models**', output)
        self.assertIn('    opus  —  1 requests · 0.0% · $0.00 | color=#334155,#D1D5DB trim=false\n \n⌘ jCodeMunch', output)

    def test_rtk_and_jcodemunch_show_compact_lifetime_blocks(self):
        today_sources = [
            parse_rtk({"summary": {}, "daily": [{"date": date.today().isoformat(), "commands": 2, "input_tokens": 100, "output_tokens": 10, "saved_tokens": 80, "savings_pct": 80}]}, scope="today"),
            parse_jcodemunch({"totals": {}, "by_day": [{"date": date.today().isoformat(), "calls": 1, "actual_tokens": 10, "baseline_tokens": 20, "savings_tokens": 10}], "window": {"days": 0}}, scope="today"),
        ]
        lifetime_sources = [
            parse_rtk({"summary": {"total_commands": 20, "total_input": 1_000, "total_output": 100, "total_saved": 800, "avg_savings_pct": 80}}),
            parse_jcodemunch({"totals": {"calls": 5, "actual_tokens": 50, "baseline_tokens": 100, "savings_tokens": 50}, "window": {"days": 0}}),
        ]
        output = render_menu(today_sources, lifetime_sources=lifetime_sources)
        self.assertIn("  **Lifetime** | md=true color=", output)
        self.assertIn('    Saved  —  800 · 80.0% | color=', output)
        self.assertIn('    Commands  —  20 | color=', output)
        self.assertIn('    Calls  —  5 | color=', output)

    def test_nested_stat_rows_do_not_use_markdown_code_indentation(self):
        output = render_menu([
            parse_llmtrim({"input": {}, "output": {}, "money": {}}, scope="today"),
        ])
        nested_rows = [line for line in output.splitlines() if line.startswith("    ") and line.strip()]
        self.assertTrue(nested_rows)
        self.assertTrue(all("md=true" not in line and "**" not in line for line in nested_rows))

    def test_each_tool_has_breathing_room_before_its_divider(self):
        sources = [
            parse_rtk({"summary": {"total_saved": 10, "avg_savings_pct": 50}}),
            parse_headroom({"tokens_saved": 0, "savings_pct": 0, "tool_saved": 2}),
            parse_jcodemunch({"totals": {}, "window": {"days": 0}}),
        ]
        output = render_menu(sources, lifetime_sources=sources)
        self.assertGreaterEqual(output.count("\n \n---"), 3)

    def test_indented_rows_disable_swiftbar_whitespace_trimming(self):
        output = render_menu([
            parse_llmtrim(
                {"input": {}, "output": {}, "money": {}, "by_model": [{"model": "opus", "requests": 1}]},
                scope="today",
            ),
        ])
        indented_rows = [line for line in output.splitlines() if line.startswith(" ") and line.strip()]
        self.assertTrue(indented_rows)
        self.assertTrue(all("trim=false" in line for line in indented_rows))

    def test_headroom_is_visible_as_a_rolling_source_but_not_added_to_lifetime_totals(self):
        headroom = parse_headroom({"window_hours": 168, "tokens_saved": 1_000, "savings_pct": 25.0, "tool_saved": 200}, scope="today")
        lifetime_headroom = parse_headroom({"window_hours": 168, "tokens_saved": 1_000, "savings_pct": 25.0, "tool_saved": 200})
        output = render_menu([headroom], lifetime_sources=[lifetime_headroom])
        self.assertTrue(output.splitlines()[0].startswith("⌁ 1K / 25%"))
        self.assertNotIn("Headroom: rolling 168 hours (not lifetime)", output)
        self.assertIn('  Tokens saved  —  0 | color=', output)
        self.assertNotIn('badge=', output)

    def test_each_tool_exposes_a_clean_scope_label(self):
        sources = [
            parse_headroom({"window_hours": 168, "tokens_saved": 1, "savings_pct": 1}),
            parse_llmtrim({"input": {}, "output": {}, "by_period": []}),
            parse_rtk({"summary": {}}),
            parse_jcodemunch({"totals": {}, "window": {"days": 30}}),
        ]
        self.assertEqual([source.scope_label for source in sources], ["Last 168 hours", "Lifetime", "Lifetime", "Last 30 days"])

    def test_detail_sections_have_non_separator_spacing(self):
        source = parse_llmtrim(
            {"input": {}, "output": {}, "money": {"saved_today_usd": 0.1}, "by_model": [{"model": "opus", "requests": 1}]},
            scope="today",
        )
        self.assertIn("", source.detail)
        self.assertEqual(source.scope_label, "Today")

    def test_menu_reports_unavailable_data_without_question_mark_only_output(self):
        output = render_menu([], error="Headroom unavailable")
        self.assertIn("Stats unavailable", output)
        self.assertIn("Headroom unavailable", output)
        self.assertNotEqual(output.strip(), "?")


if __name__ == "__main__":
    unittest.main()
