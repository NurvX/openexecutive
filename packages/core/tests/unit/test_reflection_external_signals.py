"""Test that PR-C's executive_reflection extension renders external
signals correctly in its context block."""
from __future__ import annotations

from openexecutive.workflows.executive_reflection import (
    _render_reflection_context,
)


def _empty_today() -> dict:
    return {"departments": [], "people": [], "proposals": []}


def test_render_includes_external_signals_section() -> None:
    """An EXTERNAL SIGNALS OVERNIGHT block must appear when signals are
    passed — that's the spine of the reflection synthesis prompt."""
    signals = [
        {
            "severity_hint": "urgent",
            "source_kind": "stock",
            "normalized_summary": "Apple (AAPL) down 7.0% vs prev close",
            "processed_outcome": "alerted",
        },
        {
            "severity_hint": "high",
            "source_kind": "vendor_status",
            "normalized_summary": "[stripe] Payments degraded — investigating",
            "processed_outcome": "alerted",
        },
    ]
    rendered = _render_reflection_context(
        period_label="2026-05-28",
        today_data=_empty_today(),
        activity=[],
        recent_alerts=[],
        external_signals=signals,
    )
    assert "EXTERNAL SIGNALS OVERNIGHT" in rendered
    assert "stock" in rendered
    assert "vendor_status" in rendered
    assert "urgent" in rendered
    assert "Apple (AAPL)" in rendered
    assert "alerted" in rendered


def test_render_skips_external_section_when_empty() -> None:
    """No EXTERNAL SIGNALS block when the list is empty — keeps the
    reflection prompt terse on quiet days."""
    rendered = _render_reflection_context(
        period_label="2026-05-28",
        today_data=_empty_today(),
        activity=[],
        recent_alerts=[],
        external_signals=[],
    )
    assert "EXTERNAL SIGNALS OVERNIGHT" not in rendered
    # When everything is empty the function still emits the "nothing
    # worth acting on" fallback — verify that still triggers.
    assert "No signals worth acting on" in rendered


def test_render_truncates_long_signals_list() -> None:
    """The render cap (10) keeps a runaway scan from blowing the prompt budget."""
    signals = [
        {
            "severity_hint": "low",
            "source_kind": "rss",
            "normalized_summary": f"Entry {i}",
            "processed_outcome": "alerted",
        }
        for i in range(50)
    ]
    rendered = _render_reflection_context(
        period_label="2026-05-28",
        today_data=_empty_today(),
        activity=[],
        recent_alerts=[],
        external_signals=signals,
    )
    # The 10th is in, the 11th is out.
    assert "Entry 9" in rendered
    assert "Entry 10" not in rendered


def test_render_handles_pending_outcome_label() -> None:
    """Unprocessed signals (processed_outcome=None) show as 'pending'
    so the model knows they're in flight, not silently dropped."""
    signals = [{
        "severity_hint": "medium",
        "source_kind": "stock",
        "normalized_summary": "Some movement",
        "processed_outcome": None,
    }]
    rendered = _render_reflection_context(
        period_label="2026-05-28",
        today_data=_empty_today(),
        activity=[],
        recent_alerts=[],
        external_signals=signals,
    )
    assert "pending" in rendered




# --------------------------------------------------------------------------- #
# Yesterday's standup + review verdicts (alert lifecycle)
# --------------------------------------------------------------------------- #


def test_render_includes_previous_reflection_block_and_caps_it() -> None:
    rendered = _render_reflection_context(
        period_label="2026-09-11",
        today_data=_empty_today(),
        activity=[],
        recent_alerts=[],
        external_signals=[],
        previous_reflection="**Acted on:** DM'd Dana about the renewal\n" + ("x" * 5000),
    )
    assert "YESTERDAY'S STANDUP (already handled — do not repeat):" in rendered
    assert "DM'd Dana about the renewal" in rendered
    assert len(rendered) < 2500


def test_render_skips_previous_block_when_none() -> None:
    rendered = _render_reflection_context(
        period_label="2026-09-11",
        today_data=_empty_today(),
        activity=[],
        recent_alerts=[],
        external_signals=[],
    )
    assert "YESTERDAY'S STANDUP" not in rendered


def test_render_open_alerts_carry_alert_id_and_review_verdict() -> None:
    rendered = _render_reflection_context(
        period_label="2026-09-11",
        today_data=_empty_today(),
        activity=[],
        recent_alerts=[
            {
                "alert_id": 42, "severity": "high", "headline": "Acme renewal at risk",
                "topic_tags": ["customer"], "review_verdict": "relevant",
                "review_note": "Dana has not replied in 3 days", "recommended_move": "nudge",
            },
            {"alert_id": 43, "severity": "low", "headline": "plain", "topic_tags": []},
        ],
        external_signals=[],
    )
    assert "alert_id=42 [high] Acme renewal at risk tags=customer review=relevant (Dana has not replied in 3 days) next=nudge" in rendered
    assert "alert_id=43 [low] plain tags=" in rendered
    assert "review=" not in rendered.split("alert_id=43")[1]


def test_reflection_prompt_has_memory_rule_and_never_instructs_ack_alert() -> None:
    from openexecutive.workflows.executive_reflection import _build_reflection_system

    prompt = _build_reflection_system({"discord"}, True)
    assert "YESTERDAY'S STANDUP" in prompt
    assert "Do NOT re-act" in prompt
    assert "ack_alert" not in prompt
