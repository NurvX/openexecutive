"""On-disk cache for the on-page briefing narrative.

Mirrors `openexecutive.people.insights_cache`: a dedicated table in the
shared `./episodic_memory.db`, validated against an `input_hash` of the
briefing state. The narrative is one LLM call, so we never generate it on
the request hot path — `/today` serves the cached text instantly and, when
the state hash has moved, regenerates in a FastAPI background task (same
pattern as the per-person insight notes).

Keyed by a `scope` string rather than a person id: the principal and any
unrostered/unresolved viewer share the whole-company narrative under
`"principal"` (the default), while each non-principal teammate gets their own
role-scoped narrative under `person:<id>` (see `today._attach_narrative`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from openexecutive.memory import episodic as _episodic

logger = logging.getLogger(__name__)

# Explicit override slot. Left None in production so the cache follows the
# episodic DB dynamically (see _resolve_db_path) — same rationale as
# insights_cache: capturing episodic.DB_PATH at import time would make a
# test's monkeypatch of episodic.DB_PATH silently miss this table.
DB_PATH: Path | None = None

DEFAULT_SCOPE = "principal"


class BriefingNarrative(BaseModel):
    """A cached briefing narrative for one scope."""

    scope: str
    input_hash: str
    narrative_text: str
    generated_at: str  # ISO-8601 UTC


def _resolve_db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    if DB_PATH is not None:
        return DB_PATH
    return _episodic.DB_PATH


@contextmanager
def _get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    resolved = _resolve_db_path(db_path)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefing_narrative (
                scope TEXT PRIMARY KEY,
                input_hash TEXT NOT NULL,
                narrative_text TEXT NOT NULL,
                generated_at TEXT NOT NULL
            )
            """
        )


# Bump this whenever the narrative PROMPTS change (BRIEFING_NARRATIVE_SYSTEM /
# _viewer_system_prompt / STANDALONE_BRIEF_SYSTEM in narrative.py). It is folded
# into the input hash so a prompt change invalidates every cached narrative on
# the next view — otherwise a wording fix wouldn't surface until the underlying
# state changed (or the daily date rollover).
NARRATIVE_PROMPT_VERSION = "4"


def build_narrative_input_hash(
    today_data: dict[str, Any], scope: str = DEFAULT_SCOPE
) -> str:
    """Stable hash of the briefing state the narrative is derived from.

    Keyed on the signals that should trigger a re-write — each proposal's
    headline, category AND the review fields that change what the narrative
    would say about it, at-risk department counts, and who's awaiting — plus
    the UTC date so the narrative refreshes at least daily even if nothing
    structural changed, plus the viewer `scope` so two viewers whose slices
    happen to coincide still cache under distinct keys, plus
    NARRATIVE_PROMPT_VERSION so a prompt rewrite invalidates the cache.
    Deliberately ignores volatile fields (timestamps, ids) so identical
    content doesn't churn the cache.

    The review fields matter because the alert review rewrites an open alert's
    note, why-now, recommended move or deadline IN PLACE, keeping the headline:
    on headline alone the header kept describing the pre-rewrite situation.

    Activity is deliberately NOT hashed. It looked like the fix for a frozen
    header, but the 20-row window moves on almost every DM, decision, advice
    row and completed workflow, so hashing it regenerates every viewer's
    narrative — a real model call each — on nearly any Executive action, with
    no floor. `today._nothing_needs_attention` is what keeps a frozen header
    from recurring: the state that froze it (nothing on the board) is now
    answered by a deterministic quiet line instead of a model call.
    """
    proposals = [
        (
            p.get("headline", ""),
            p.get("category", ""),
            p.get("review_verdict", ""),
            str(p.get("review_note", ""))[:120],
            str(p.get("why_now", ""))[:120],
            p.get("recommended_move", ""),
            p.get("due_at", "") or "",
        )
        for p in today_data.get("proposals", [])
    ]
    depts = [
        (d.get("slug", ""), d.get("at_risk_count", 0), d.get("off_track_count", 0))
        for d in today_data.get("departments", [])
        if d.get("at_risk_count", 0) or d.get("off_track_count", 0)
    ]
    awaiting = sorted(
        p.get("id", 0)
        for p in today_data.get("people", [])
        if p.get("awaiting_count", 0)
    )
    payload = {
        "scope": scope,
        "prompt_version": NARRATIVE_PROMPT_VERSION,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "proposals": proposals,
        "depts": depts,
        "awaiting": awaiting,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(scope: str = DEFAULT_SCOPE, db_path: Path | None = None) -> BriefingNarrative | None:
    initialize_db(db_path)
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT scope, input_hash, narrative_text, generated_at "
            "FROM briefing_narrative WHERE scope = ?",
            (scope,),
        ).fetchone()
    if row is None:
        return None
    return BriefingNarrative(
        scope=row["scope"],
        input_hash=row["input_hash"],
        narrative_text=row["narrative_text"],
        generated_at=row["generated_at"],
    )


def put(narrative: BriefingNarrative, db_path: Path | None = None) -> None:
    initialize_db(db_path)
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO briefing_narrative
              (scope, input_hash, narrative_text, generated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
              input_hash = excluded.input_hash,
              narrative_text = excluded.narrative_text,
              generated_at = excluded.generated_at
            """,
            (
                narrative.scope,
                narrative.input_hash,
                narrative.narrative_text,
                narrative.generated_at,
            ),
        )


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "DEFAULT_SCOPE",
    "BriefingNarrative",
    "build_narrative_input_hash",
    "get",
    "initialize_db",
    "put",
    "utc_now_iso",
]
