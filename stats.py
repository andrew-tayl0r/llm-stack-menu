#!/usr/bin/env python3
"""SwiftBar statistics for the local LLM optimisation tools."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class StatsSource:
    key: str
    name: str
    symbol: str
    tokens_saved: int | None
    savings_pct: float | None
    saved_usd: float | None
    detail: tuple[str, ...] = ()
    available: bool = True
    scope_label: str = ""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_headroom(payload: dict[str, Any], scope: str = "lifetime") -> StatsSource:
    tokens_saved = _integer(payload.get("tokens_saved"))
    savings_pct = _number(payload.get("savings_pct"))
    tool_saved = _integer(payload.get("tool_saved"))
    priced_total = 0.0
    has_priced_model = False
    for model in payload.get("by_model", []) or []:
        saved = _integer(model.get("tokens_saved"))
        price = model.get("list_price_per_mtok")
        if price is not None:
            priced_total += saved * _number(price) / 1_000_000
            has_priced_model = True
    window_hours = _number(payload.get("window_hours"), 168)
    details_list = [f"Tool tokens: {tool_saved:,}"]
    if "total_requests" in payload:
        details_list.append(f"Requests: {_integer(payload.get('total_requests')):,}")
    if "cache_read_tokens" in payload or "cache_write_tokens" in payload:
        details_list.append(f"Cache read/write: {format_tokens(_integer(payload.get('cache_read_tokens')))} / {format_tokens(_integer(payload.get('cache_write_tokens')))}")
    overhead = payload.get("overhead") or {}
    optimisation = overhead.get("optimization_ms") or {}
    if optimisation:
        details_list.append(
            f"Overhead: {_number(optimisation.get('average_ms')):.1f} ms avg · "
            f"{_number(optimisation.get('p95_ms')):.1f} ms p95 · "
            f"{_integer(optimisation.get('slow_request_count')):,} slow ({_number(optimisation.get('slow_request_pct')):.1f}%)"
        )
    transforms = sorted((row for row in (payload.get("by_transform") or []) if isinstance(row, dict)), key=lambda row: _integer(row.get("uses")), reverse=True)
    for row in transforms[:3]:
        details_list.append(f"Top transform: {row.get('transform', 'unknown')} · {_integer(row.get('uses')):,} uses")
    # cli_filtering is intentionally not shown: Headroom reports RTK's savings there.
    details = tuple(details_list)
    return StatsSource(
        key="headroom",
        name="Headroom",
        symbol="⌁",
        tokens_saved=tokens_saved,
        savings_pct=savings_pct,
        saved_usd=priced_total if has_priced_model and priced_total > 0 else None,
        detail=details,
        scope_label=f"Last {window_hours:g} hours",
    )


def _llmtrim_ledger_details(payload: dict[str, Any], requests: int, *, lifetime_label: bool = False) -> list[str]:
    money = payload.get("money") or {}
    prefix = "Lifetime " if lifetime_label else ""
    details: list[str] = []
    if money.get("paid_usd") is not None and money.get("would_have_usd") is not None:
        cost_label = f"{prefix}cost" if lifetime_label else "Cost"
        details.append(f"{cost_label}: {_format_usd(_number(money.get('paid_usd')))} paid vs {_format_usd(_number(money.get('would_have_usd')))} untrimmed")
        saved_usd = money.get("saved_usd")
        if saved_usd is not None:
            saved_today = money.get("saved_today_usd")
            savings_label = f"{prefix}savings" if lifetime_label else "Savings"
            details.append(f"{savings_label}: {_format_usd(_number(saved_usd))}" + (f" total ({_format_usd(_number(saved_today))} today)" if saved_today is not None and not lifetime_label else " total"))
    if payload.get("added_latency_ms") is not None:
        details.append(f"{prefix + 'added delay' if lifetime_label else 'Added delay'}: ~{_number(payload.get('added_latency_ms')):.1f} ms/request")
    coverage = money.get("coverage") or {}
    turns = _integer(money.get("turns"))
    events = _integer(coverage.get("compressions_events"), requests)
    if turns or events:
        ratio = _number(coverage.get("ratio"), turns / events if events else 0.0)
        details.append(f"{prefix + 'turns covered' if lifetime_label else 'Turns covered'}: {turns:,}/{events:,} ({ratio * 100:.1f}%)")
    sessions = payload.get("breakdown", {}).get("sessions")
    if isinstance(sessions, list):
        details.append(f"{prefix + 'sessions covered' if lifetime_label else 'Sessions covered'}: {len(sessions):,}")
    periods = payload.get("by_period") or []
    if isinstance(periods, (list, dict)):
        details.append(f"{prefix + 'days with data' if lifetime_label else 'Days with data'}: {len(periods):,}")
    model_totals: dict[str, dict[str, float]] = {}
    for row in (payload.get("by_model") or []):
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or "unknown")
        requests_for_model = _integer(row.get("requests"))
        aggregate = model_totals.setdefault(model, {"requests": 0.0, "saved_usd": 0.0, "weighted_pct": 0.0})
        aggregate["requests"] += requests_for_model
        aggregate["saved_usd"] += _number(row.get("cost_saved_usd"))
        aggregate["weighted_pct"] += _number(row.get("saved_pct")) * requests_for_model
    models = sorted(model_totals.items(), key=lambda item: item[1]["saved_usd"], reverse=True)
    if models:
        details.append("")
        details.append("Top models · lifetime" if lifetime_label else "Top models")
    for model, row in models[:5]:
        model_requests = int(row["requests"])
        model_pct = row["weighted_pct"] / row["requests"] if row["requests"] else 0.0
        details.append(f"{model} · {model_requests:,} requests · {model_pct:.1f}% · {_format_usd(row['saved_usd'])}")
    return details


def parse_llmtrim(payload: dict[str, Any], scope: str = "lifetime") -> StatsSource:
    input_data = payload.get("input") or {}
    output_data = payload.get("output") or {}
    input_before = _integer(input_data.get("before"))
    input_after = _integer(input_data.get("after"))
    output_before = _integer(output_data.get("before"))
    output_after = _integer(output_data.get("after"))
    requests = _integer(payload.get("requests"))
    money = payload.get("money") or {}
    saved_usd = money.get("saved_usd")
    periods = payload.get("by_period") or []
    if scope == "today":
        today = date.today().isoformat()
        row = next((item for item in periods if isinstance(item, dict) and str(item.get("period", ""))[:10] == today), None)
        if row is None:
            input_before = input_after = output_before = output_after = requests = 0
        else:
            input_before = _integer(row.get("input_before"))
            input_after = _integer(row.get("input_after"))
            output_before = _integer(row.get("output_before"))
            output_after = _integer(row.get("output_after"))
            requests = _integer(row.get("requests"))
        saved_usd = money.get("saved_today_usd")
    input_saved = max(input_before - input_after, 0)
    output_saved = max(output_before - output_after, 0)
    total_before = input_before + output_before
    savings_pct = (input_saved + output_saved) / total_before * 100 if total_before else None
    details = [f"Input/output: {format_tokens(input_before)} → {format_tokens(input_after)} / {format_tokens(output_before)} → {format_tokens(output_after)}", f"Requests handled: {requests:,}"]
    if scope == "today":
        if saved_usd is not None:
            details.append(f"Savings today: {_format_usd(_number(saved_usd))}")
        details.append("")
        details.append("Lifetime")
        details.extend(_llmtrim_ledger_details(payload, _integer(payload.get("requests")), lifetime_label=True))
        details.append("")
    else:
        details.extend(_llmtrim_ledger_details(payload, requests))
        details.append("")
    return StatsSource(
        key="llmtrim",
        name="llmtrim",
        symbol="→│←",
        tokens_saved=input_saved + output_saved,
        savings_pct=savings_pct if scope == "today" else _number(input_data.get("saved_pct")),
        saved_usd=_number(saved_usd) if saved_usd is not None else None,
        detail=tuple(details),
        scope_label="Today" if scope == "today" else "Lifetime",
    )


def parse_rtk(payload: dict[str, Any], scope: str = "lifetime") -> StatsSource:
    summary = payload.get("summary") or {}
    if scope == "today":
        today = date.today().isoformat()
        summary = next((item for item in (payload.get("daily") or []) if isinstance(item, dict) and str(item.get("date", ""))[:10] == today), {})
        details = [
            f"Commands: {_integer(summary.get('commands')):,}",
            f"Input/output: {format_tokens(_integer(summary.get('input_tokens')))} / {format_tokens(_integer(summary.get('output_tokens')))}",
        ]
        if summary.get("total_time_ms") is not None:
            details.append(f"Exec time: {_format_duration_ms(_number(summary.get('total_time_ms')))}")
        if summary.get("avg_time_ms") is not None:
            details.append(f"Average command: {_number(summary.get('avg_time_ms')):.1f} ms")
        return StatsSource("rtk", "RTK", "▱", _integer(summary.get("saved_tokens")), _number(summary.get("savings_pct")) if summary else None, None, tuple(details), scope_label="Today")
    details = [
        f"Commands: {_integer(summary.get('total_commands')):,}",
        f"Input/output: {format_tokens(_integer(summary.get('total_input')))} / {format_tokens(_integer(summary.get('total_output')))}",
    ]
    if summary.get("total_time_ms") is not None:
        details.append(f"Exec time: {_format_duration_ms(_number(summary.get('total_time_ms')))}")
    if summary.get("avg_time_ms") is not None:
        details.append(f"Average command: {_number(summary.get('avg_time_ms')):.1f} ms")
    return StatsSource(
        key="rtk",
        name="RTK",
        symbol="▱",
        tokens_saved=_integer(summary.get("total_saved")),
        savings_pct=_number(summary.get("avg_savings_pct")),
        saved_usd=None,
        detail=tuple(details),
        scope_label="Lifetime",
    )


def parse_jcodemunch(payload: dict[str, Any], scope: str = "lifetime") -> StatsSource:
    totals = payload.get("totals") or {}
    if scope == "today":
        today = date.today().isoformat()
        totals = next((item for item in (payload.get("by_day") or []) if isinstance(item, dict) and str(item.get("date", ""))[:10] == today), {})
    baseline = _integer(totals.get("baseline_tokens"))
    saved = _integer(totals.get("savings_tokens"))
    pct = saved / baseline * 100 if baseline else None
    details = [
        f"Calls: {_integer(totals.get('calls')):,}",
        f"Actual tokens: {format_tokens(_integer(totals.get('actual_tokens')))}",
        f"Baseline tokens: {format_tokens(baseline)}",
        f"Model estimate: {payload.get('model', 'opus')}",
    ]
    since = (payload.get("window") or {}).get("since")
    if since and scope != "today":
        details.append(f"Since: {since[:10]}")
    window_days = _integer((payload.get("window") or {}).get("days"), 30)
    return StatsSource(
        key="jcodemunch",
        name="jCodeMunch",
        symbol="⌘",
        tokens_saved=saved,
        savings_pct=pct,
        saved_usd=(
            _number(totals.get("savings_usd"))
            if scope == "today" and totals.get("savings_usd") is not None and _number(totals.get("savings_usd")) > 0
            else (_number(payload.get("savings_usd")) if scope != "today" and payload.get("savings_usd") is not None and _number(payload.get("savings_usd")) > 0 else None)
        ),
        detail=tuple(details),
        scope_label="Today" if scope == "today" else ("All recorded usage" if window_days == 0 else f"Last {window_days} days"),
    )


def combined_saved_usd(sources: Iterable[StatsSource]) -> float:
    return sum(source.saved_usd or 0.0 for source in sources)


def combined_tokens_saved(sources: Iterable[StatsSource]) -> int:
    return sum(source.tokens_saved or 0 for source in sources)


def format_tokens(value: int | None) -> str:
    if value is None:
        return "—"
    value = int(value)
    if abs(value) < 1_000:
        return f"{value:,}"
    if abs(value) < 1_000_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return f"{value / 1_000_000:.1f}M".replace(".0M", "M")


def _format_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _format_usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _format_duration_ms(value: float) -> str:
    seconds = value / 1_000
    if seconds >= 60:
        return f"{seconds / 60:.1f} min"
    return f"{seconds:.1f} s"


def _compact_source(source: StatsSource) -> str:
    pct = "—" if source.savings_pct is None else f"{source.savings_pct:.0f}%"
    return f"{source.symbol} {format_tokens(source.tokens_saved)} / {pct}"


def _source_order(source: StatsSource) -> int:
    return {"llmtrim": 0, "rtk": 1, "headroom": 2, "jcodemunch": 3}.get(source.key, 9)


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


def _detail_item(text: str, indent: str = "") -> str:
    label, separator, value = text.partition(":")
    if separator:
        return _menu_item(f"{indent}{label}  —  {value.strip()}", DETAIL_COLOR)
    return _menu_item(f"{indent}{text}", DETAIL_COLOR)


def _headroom_metrics_enabled() -> bool:
    state_path = Path.home() / ".llm-stack-controller" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(state.get("headroom_in_mode", False))


SECTION_COLOR = "#172033,#F3F4F6"
DETAIL_COLOR = "#334155,#D1D5DB"
TOOL_COLORS = {
    "llmtrim": "#1D4ED8,#93C5FD",
    "rtk": "#15803D,#86EFAC",
    "headroom": "#C2410C,#FDBA74",
    "jcodemunch": "#7E22CE,#D8B4FE",
}


def render_menu(
    sources: list[StatsSource],
    error: str | None = None,
    lifetime_sources: list[StatsSource] | None = None,
    *,
    headroom_enabled: bool = True,
) -> str:
    if not headroom_enabled:
        sources = [source for source in sources if source.key != "headroom"]
        if lifetime_sources is not None:
            lifetime_sources = [source for source in lifetime_sources if source.key != "headroom"]
    total = _format_usd(combined_saved_usd(sources)) if sources else "—"
    total_tokens = format_tokens(combined_tokens_saved(sources)) if sources else "—"
    visible_sources = [
        source for source in sources
        if source.key == "headroom" or (source.tokens_saved is not None and source.tokens_saved > 0)
    ]
    if visible_sources:
        visible_sources.sort(key=_source_order)
    else:
        visible_sources = sorted(sources, key=_source_order)
    compact = "   ".join(_compact_source(source) for source in visible_sources)
    compact = f"{compact}" if compact else "⌁— · ‹‹— · ▱— · ⌘—"
    lines = [compact]
    lines.append("---")
    if lifetime_sources is None:
        lines.append(_menu_item("Totals", SECTION_COLOR, bold=True))
        lines.append(_detail_item(f"Total tokens saved: {total_tokens}", "  "))
        lines.append(_detail_item(f"Total tracked savings: {total} USD", "  "))
    else:
        lifetime_counted = [source for source in lifetime_sources if source.key != "headroom"]
        lifetime_total = _format_usd(combined_saved_usd(lifetime_counted))
        lifetime_tokens = format_tokens(combined_tokens_saved(lifetime_counted))
        lines.append(_menu_item("Today", SECTION_COLOR, bold=True))
        lines.append(_detail_item(f"Tokens saved: {total_tokens}", "  "))
        lines.append(_detail_item(f"Tracked savings (priced sources): {total} USD", "  "))
    if error:
        lines.append("Stats unavailable")
        lines.append(error)
    if lifetime_sources is not None:
        lines.append("---")
        lines.append(_menu_item("Lifetime totals", SECTION_COLOR, bold=True))
        lines.append(_detail_item(f"Tokens saved: {lifetime_tokens}", "  "))
        lines.append(_detail_item(f"Tracked savings (priced sources): {lifetime_total} USD", "  "))
    lines.append("---")
    lines.append(_menu_item("Tools", SECTION_COLOR, bold=True))
    lifetime_by_key = {source.key: source for source in (lifetime_sources or [])}
    for index, source in enumerate(sorted(sources, key=_source_order)):
        if index:
            lines.append("---")
        scope = f" · {source.scope_label}" if source.scope_label else ""
        lines.append(_menu_item(f"{source.symbol} {source.name}{scope}", TOOL_COLORS.get(source.key), bold=True))
        lines.append(_detail_item(f"Saved: {format_tokens(source.tokens_saved)} · {_format_pct(source.savings_pct)}", "  "))
        subsection = ""
        for detail in source.detail:
            if detail == "":
                lines.append(" ")
            elif detail in {"Lifetime", "Top models", "Top models · lifetime"}:
                heading = "Top models" if detail.startswith("Top models") else detail
                lines.append(_menu_item(f"  {heading}", SECTION_COLOR, bold=True))
                subsection = heading
            else:
                indent = "    " if subsection else "  "
                if subsection == "Top models" and " · " in detail:
                    model, metrics = detail.split(" · ", 1)
                    lines.append(_menu_item(f"{indent}{model}  —  {metrics}", DETAIL_COLOR))
                else:
                    lines.append(_detail_item(detail, indent))
        lifetime = lifetime_by_key.get(source.key)
        if lifetime is not None and source.key in {"rtk", "jcodemunch"}:
            lines.append(" ")
            lines.append(_menu_item("  Lifetime", SECTION_COLOR, bold=True))
            lines.append(_detail_item(f"Saved: {format_tokens(lifetime.tokens_saved)} · {_format_pct(lifetime.savings_pct)}", "    "))
            for detail in lifetime.detail:
                lines.append(_detail_item(detail, "    "))
        if lines[-1] != " ":
            lines.append(" ")
    if not any(source.key == "jcodemunch" for source in sources):
        lines.append(_menu_item("⌘ jCodeMunch · unavailable · no receipt data", TOOL_COLORS["jcodemunch"]))
    lines.append("---")
    lines.append(_menu_item("Actions", SECTION_COLOR, bold=True))
    lines.append("  Refresh stats | refresh=true trim=false")
    return "\n".join(lines)


def _executable(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def _json_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"exit {completed.returncode}")
    return _decode_json_output(completed.stdout)


def _jcodemunch_receipt() -> dict[str, Any]:
    """Read jCodeMunch's measured receipt without leaving a persistent file."""
    fd, path = tempfile.mkstemp(prefix="jcodemunch-receipt-", suffix=".json")
    os.close(fd)
    try:
        command = [_executable("uvx", "/opt/homebrew/bin/uvx"), "jcodemunch-mcp", "receipt", "--days", "0", "--by-day", "--export", path]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"exit {completed.returncode}")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("receipt returned a non-object")
        return value
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _decode_json_output(output: str) -> dict[str, Any]:
    """Decode JSON even when a CLI prints an informational banner first."""
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output).strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start < 0 or end < start:
        raise ValueError("command returned no JSON object")
    value = json.loads(clean[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("command returned a JSON value instead of an object")
    return value


def collect_sources() -> tuple[list[StatsSource], list[StatsSource], str | None]:
    commands = [
        ("Headroom", [_executable("headroom", "/Users/andrew/.local/bin/headroom"), "perf", "--format", "json"], parse_headroom),
        ("llmtrim", [_executable("llmtrim", "/Users/andrew/.nvm/versions/node/v24.18.0/bin/llmtrim"), "status", "--json", "--breakdown"], parse_llmtrim),
        ("RTK", [_executable("rtk", "/opt/homebrew/bin/rtk"), "gain", "--daily", "--format", "json"], parse_rtk),
        ("jCodeMunch", None, parse_jcodemunch),
    ]
    today_sources: list[StatsSource] = []
    lifetime_sources: list[StatsSource] = []
    errors: list[str] = []
    for name, command, parser in commands:
        try:
            payload = _jcodemunch_receipt() if command is None else _json_command(command)
            today_sources.append(parser(payload, scope="today"))
            lifetime_sources.append(parser(payload, scope="lifetime"))
        except Exception as exc:  # SwiftBar should show the other sources if one is down.
            errors.append(f"{name}: {exc}")
    return today_sources, lifetime_sources, "; ".join(errors) if errors else None


def main() -> None:
    sources, lifetime_sources, error = collect_sources()
    print(render_menu(sources, error, lifetime_sources, headroom_enabled=_headroom_metrics_enabled()))


if __name__ == "__main__":
    main()
