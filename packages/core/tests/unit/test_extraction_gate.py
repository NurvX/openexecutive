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

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from openexecutive.memory import episodic
from openexecutive.memory.episodic import initialize_db


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """An isolated episodic DB.

    Without it these passes write real rows into `./episodic_memory.db`, which
    other modules then read back as production data — the audit-log pollution
    trap in CLAUDE.md.
    """
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    return db_path

# Real turns from the tenant, with the assistant length that accompanied them.
# Each is the principal giving an instruction, and each was discarded by the
# combined-length floor.
_COMMITMENTS = [
    ("It's a mistake on the lp purchased properties worksheet.  That's it. "
     "Correct it there. And record this as fixed.", 1200),
    ("Nope.  The briefing is incorrect.  We are not under water 2.9m that is "
     "just how the finicals look as received Q2", 920),
    ("This is not relevant for us.  Don't track this", 864),
    ("Well remember you fixed it or something. So we don't keep getting. "
     "Notified.", 612),
]

# The shape a length floor cannot handle: a long analysis answered in a few
# words. A floor high enough to skip "Done" also skips "Do B.".
_SHORT_APPROVALS = ["Approve option B.", "Do B.", "Kill it. Approved.", "Yes, ship it."]


@pytest.mark.parametrize("message,assistant_len", _COMMITMENTS)
def test_real_commitments_reach_the_extractor(message: str, assistant_len: int) -> None:
    """Drives the real gate, not arithmetic on literals."""
    assert len(message) + assistant_len < 1500, "blocked by the old combined floor"
    assert episodic.should_extract(message)


@pytest.mark.parametrize("message", _SHORT_APPROVALS)
def test_short_approvals_reach_the_extractor(message: str) -> None:
    """The canonical executive decision: long analysis, three-word answer.

    A user-side length floor regressed these — "Approve option B." is 17 chars
    and the old combined floor DID admit it after a long reply. Any floor that
    skips "Done" (4) also skips "Do B." (5), so there is no floor.
    """
    assert episodic.should_extract(message)


def test_empty_turns_are_skipped() -> None:
    for blank in ("", "   ", "\n\t "):
        assert not episodic.should_extract(blank)


async def _run_pass(
    payload: Any,
    db_path: Path,
    *,
    user_message: str = "Cut burn to 400k. Tell the team.",
) -> tuple[mock.MagicMock, list[dict[str, Any]]]:
    """Drive one real extraction pass over a model payload.

    The payload is whatever the model put in its `store_memories` tool block —
    it is NOT schema-checked anywhere, so tests hand it the shapes a model
    actually emits, including the malformed ones.
    """
    class _Block:
        type = "tool_use"
        name = "store_memories"
        input = payload

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
            user_message, "Understood.", db_path=db_path, session_id="s-1"
        )

    return store, [r for r in rows if r["event_type"] == "memory_extraction"]


@pytest.mark.asyncio
async def test_extraction_audits_proposed_stored_and_dropped(db: Path) -> None:
    """A pass that proposes items and stores none must be visible.

    That is exactly what happened in production, and it was indistinguishable
    from "the model found nothing" because drops were logger.debug and stores
    wrote no row.
    """
    store, audit = await _run_pass(db_path=db, payload={
        "decisions": [
            # Valid: the quote is the user's own words, terminated.
            {"domain": "finance", "summary": "Cut burn to 400k",
             "user_commitment_quote": "Cut burn to 400k"},
            # Invalid: quote is not in the user message at all.
            {"domain": "finance", "summary": "Sell the building",
             "user_commitment_quote": "sell the building"},
        ],
    })

    assert store.call_count == 1
    assert len(audit) == 1
    assert audit[0]["details"]["proposed"]["decisions"] == 2
    assert audit[0]["details"]["stored"]["decisions"] == 1
    assert audit[0]["details"]["dropped_count"] == 1
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "summary": "Sell the building"}
    ]
    assert audit[0]["details"]["failure"] == ""


# --------------------------------------------------------------------- #
# Malformed model payloads
#
# `block.input` is whatever the model emitted; nothing validates its shape
# before the loops index into it. A `null` or a list of bare strings used to
# raise out of the whole pass — which took the other two kinds with it AND
# skipped the audit row, leaving a log indistinguishable from "extraction
# never ran". That is the exact blind spot this work exists to remove, so the
# malformed shapes must still produce a row.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"decisions": "Cut burn"}, "not_a_list"),
        ({"decisions": 7}, "not_a_list"),
        ({"decisions": ["Cut burn to 400k"]}, "item_not_a_dict"),
        ({"decisions": [None]}, "item_not_a_dict"),
    ],
    ids=["str", "int", "list-of-str", "list-of-null"],
)
async def test_malformed_items_are_dropped_not_raised(
    payload: dict[str, Any], reason: str, db: Path
) -> None:
    store, audit = await _run_pass(payload, db)

    assert store.call_count == 0
    assert len(audit) == 1, "the pass must still audit itself"
    assert audit[0]["details"]["failure"] == "", "must not have raised"
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": reason}
    ], "the kind must match the singular spelling used by per-item drops"


@pytest.mark.asyncio
async def test_a_null_kind_is_not_a_drop(db: Path) -> None:
    """The model omitting a kind is normal, not malformed."""
    _, audit = await _run_pass(
        db_path=db, payload={"decisions": None, "initiatives": []}
    )

    assert audit[0]["details"]["dropped"] == []
    assert audit[0]["details"]["proposed"] == {
        "decisions": 0, "initiatives": 0, "advice": 0
    }


@pytest.mark.asyncio
async def test_one_malformed_kind_does_not_lose_the_others(db: Path) -> None:
    """A bad `decisions` value used to abort the pass before `initiatives`
    was ever read, silently discarding good items alongside the bad one."""
    _, audit = await _run_pass(db_path=db, payload={
        "decisions": "oops",
        "initiatives": [
            {"title": "Cut burn", "summary": "Down to 400k",
             "user_commitment_quote": "Cut burn to 400k"},
        ],
    })

    assert audit[0]["details"]["stored"]["initiatives"] == 1
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": "not_a_list"}
    ]


@pytest.mark.asyncio
async def test_a_non_dict_payload_is_dropped(db: Path) -> None:
    store, audit = await _run_pass(["decisions"], db)

    assert store.call_count == 0
    assert audit[0]["details"]["dropped"] == [
        {"kind": "pass", "reason": "payload_not_a_dict"}
    ]


@pytest.mark.asyncio
async def test_a_crashed_pass_still_audits_with_a_failure_reason(db: Path) -> None:
    """A provider outage that writes no row at all reads exactly like
    "extraction was never scheduled" — the state this row exists to rule out.
    """
    rows: list[dict[str, Any]] = []

    def _capture(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    with (
        mock.patch("openexecutive.audit.log_event", _capture),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        provider.return_value.messages_create = mock.AsyncMock(
            side_effect=RuntimeError("upstream 529")
        )
        await episodic.extract_and_store(
            "Cut burn to 400k.", "ok", db_path=db, session_id="s-1"
        )

    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert len(audit) == 1
    assert audit[0]["details"]["failure"] == "RuntimeError"
    assert audit[0]["summary"].startswith("FAILED(RuntimeError)")
