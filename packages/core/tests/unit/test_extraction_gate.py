"""The turn gate in front of the episodic memory extractor.

Regression set for an install where the extractor ran 13 times, cost real
money, and stored nothing. The gate was a combined user+assistant floor of
1500 chars, and on real traffic it selected almost exactly the wrong turns:
of 16 exchanges it blocked 9, and those 9 held every instruction the principal
gave, while the 7 it admitted were long analytical exchanges with no
commitment in them at all.

The turns below are the real ones, with their real lengths.
"""
from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from openexecutive.memory import episodic

# (user message, assistant length, what it is) — measured from the tenant.
_COMMITMENTS = [
    ("It's a mistake on the lp purchased properties worksheet.  That's it. "
     "Correct it there. And record this as fixed.", 1200),
    ("Nope.  The briefing is incorrect.  We are not under water 2.9m that is "
     "just how the finicals look as received Q2", 920),
    ("This is not relevant for us.  Don't track this", 864),
    ("Well remember you fixed it or something. So we don't keep getting. "
     "Notified.", 612),
]

# Bare acknowledgements and one-line questions: nothing to extract from the
# user's own words, which is what the floor is actually for.
_NOISE = ["No", "Done", "Nope.", "Who am I?", "ok", "yes"]


@pytest.mark.parametrize("message,assistant_len", _COMMITMENTS)
def test_real_commitments_reach_the_extractor(message: str, assistant_len: int) -> None:
    """Every one of these was blocked by the old combined-length floor.

    Each is well under 1500 chars combined — `len(user) + assistant_len` — yet
    each is the principal giving a decision. Asserted against the real lengths
    so the test states the bug rather than restating the new constant.
    """
    assert len(message) + assistant_len < 1500, "should have been blocked before"
    assert len(message.strip()) >= episodic.MIN_USER_CHARS_FOR_EXTRACTION


@pytest.mark.parametrize("message", _NOISE)
def test_bare_acknowledgements_are_still_skipped(message: str) -> None:
    """The floor still has a job: an LLM call on "No" buys nothing."""
    assert len(message.strip()) < episodic.MIN_USER_CHARS_FOR_EXTRACTION


def test_a_long_assistant_reply_no_longer_drags_a_short_turn_in() -> None:
    """The inverse of the bug: verbosity on the Executive's side is not
    evidence the principal committed to anything, and the quote validator will
    only ever accept text from the user's message anyway."""
    assert len("Done".strip()) < episodic.MIN_USER_CHARS_FOR_EXTRACTION


def test_old_constant_still_resolves_for_external_callers() -> None:
    assert episodic.MIN_TURN_CHARS_FOR_EXTRACTION == episodic.MIN_USER_CHARS_FOR_EXTRACTION


@pytest.mark.asyncio
async def test_extraction_audits_proposed_stored_and_dropped() -> None:
    """A pass that proposes items and stores none must be visible.

    That is exactly what happened in production, and it was indistinguishable
    from "the model found nothing" because drops were logger.debug and stores
    wrote no row.
    """
    class _Block:
        type = "tool_use"
        name = "store_memories"
        input: dict[str, Any] = {
            "decisions": [
                # Valid: the quote is the user's own words, terminated.
                {"domain": "finance", "summary": "Cut burn to 400k",
                 "user_commitment_quote": "Cut burn to 400k"},
                # Invalid: quote is not in the user message at all.
                {"domain": "finance", "summary": "Sell the building",
                 "user_commitment_quote": "sell the building"},
            ],
        }

    class _Response:
        content = [_Block()]

    rows: list[dict[str, Any]] = []

    def _capture(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    with (
        mock.patch.object(episodic, "store_decision") as store,
        mock.patch("openexecutive.audit.log_event", _capture),
        mock.patch("openexecutive.audit.usage.log_model_usage"),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        provider.return_value.messages_create = mock.AsyncMock(
            return_value=_Response()
        )
        await episodic.extract_and_store(
            "Cut burn to 400k. Tell the team.", "Understood.", session_id="s-1"
        )

    assert store.call_count == 1
    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert len(audit) == 1
    assert audit[0]["details"]["proposed"]["decisions"] == 2
    assert audit[0]["details"]["stored"]["decisions"] == 1
    assert audit[0]["details"]["dropped_count"] == 1
    assert "decision:bad_quote:Sell the building" in audit[0]["details"]["dropped"]
