"""MorningBriefWorkflow contract: it must use the STANDALONE brief prompt.

The morning brief is delivered as a DM with no cards beside it, so it must
enumerate actionables (standalone=True) rather than the /today header synthesis
that assumes a card list renders below it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openexecutive.api.routes import today as today_route
from openexecutive.api.routes.today import ActivityResponse, TodayResponse
from openexecutive.briefing import brief_state, narrative_cache
from openexecutive.briefing import narrative as briefing_narrative
from openexecutive.workflows.morning_brief import (
    MorningBriefInput,
    MorningBriefWorkflow,
)


@pytest.fixture(autouse=True)
def _isolated_brief_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")
    # The "handled overnight" block reads the audit log; keep it empty and
    # deterministic here regardless of what other modules audited.
    monkeypatch.setattr(brief_state, "handled_since", lambda since, limit=20: [])


@pytest.mark.asyncio
async def test_morning_brief_uses_standalone_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _synth(**kw: object) -> str:
        captured.update(kw)
        return "MORNING BRIEF BODY"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    # Avoid touching the DB — stub the aggregators.
    monkeypatch.setattr(
        today_route, "_build_today",
        lambda: TodayResponse(departments=[], people=[], proposals=[]),
    )
    monkeypatch.setattr(
        today_route, "_build_activity", lambda limit, since=None: ActivityResponse(items=[])
    )

    wf = MorningBriefWorkflow()
    events = [
        e async for e in wf.run(MorningBriefInput(period_label="2026-05-29"), MagicMock())
    ]

    # The morning brief must request the standalone (enumerated) prompt.
    assert captured.get("standalone") is True
    assert captured.get("viewer") is None
    # And the synthesized body becomes the artifact.
    artifacts = [e for e in events if getattr(e, "type", "") == "artifact"]
    assert artifacts and "MORNING BRIEF BODY" in artifacts[0].content


def _stub_aggregators(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes.today import ProposalItem

    monkeypatch.setattr(
        today_route, "_build_today",
        lambda: TodayResponse(departments=[], people=[], proposals=[
            ProposalItem(
                alert_id=1, headline="Renew Acme", body="b", routed_to_person_id=None,
                suggested_action="", created_at="2026-01-01T00:00:00+00:00", topic_tags=[],
            ),
        ]),
    )
    monkeypatch.setattr(
        today_route, "_build_activity", lambda limit, since=None: ActivityResponse(items=[])
    )


@pytest.mark.asyncio
async def test_morning_brief_passes_window_and_emits_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _synth(**kw: object) -> str:
        captured.update(kw)
        return "FULL BRIEF"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    _stub_aggregators(monkeypatch)

    events = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    result = next(e for e in events if e.type == "result")
    assert result.data["suppressed"] is False
    assert len(result.data["brief_fingerprint"]) == 64
    assert captured["since"] is not None
    assert captured["handled"] == []
    assert any(e.type == "artifact" and e.content == "FULL BRIEF" for e in events)


@pytest.mark.asyncio
async def test_morning_brief_suppressed_when_fingerprint_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def _synth(**kw: object) -> str:
        calls["n"] += 1
        return "FULL BRIEF"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    _stub_aggregators(monkeypatch)

    first = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    fp = next(e for e in first if e.type == "result").data["brief_fingerprint"]
    assert calls["n"] == 1
    # The scheduler records a delivery; the next run sees an identical fingerprint.
    brief_state.record_delivered("principal_brief_morning", fp, "FULL BRIEF")

    second = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    result = next(e for e in second if e.type == "result")
    artifact = next(e for e in second if e.type == "artifact")
    assert result.data["suppressed"] is True
    assert calls["n"] == 1  # no model call
    assert artifact.content == "Nothing new since yesterday's brief — 1 item still waiting on you."

    # force_full bypasses the suppression.
    third = [
        e async for e in MorningBriefWorkflow().run(MorningBriefInput(force_full=True), MagicMock())
    ]
    assert next(e for e in third if e.type == "result").data["suppressed"] is False
    assert calls["n"] == 2
