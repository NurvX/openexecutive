from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from openexecutive.api.models import SessionSummary
from openexecutive.api.routes.chat import _resolve_caller_person_id
from openexecutive.memory.session_store import (
    delete_session,
    get_session_metadata,
    get_session_owner,
    list_sessions,
    load_messages,
    set_message_feedback,
)
from openexecutive.people.store import is_principal_or_self

router = APIRouter()


@router.get("/sessions", response_model=list[SessionSummary])
def get_sessions(request: Request) -> list[SessionSummary]:
    caller_person_id = _resolve_caller_person_id(request)
    if caller_person_id is None:
        # Either a signed-in user whose email isn't in the roster, or no
        # principal is configured yet (fresh install). Either way they
        # have no chats to see — return empty rather than leaking the
        # legacy NULL-owner rows.
        return []
    return [SessionSummary(**s) for s in list_sessions(caller_person_id)]


@router.get("/sessions/{session_id}", response_model=SessionSummary)
def get_session(session_id: str) -> SessionSummary:
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionSummary(**meta)


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str) -> list[dict]:
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return load_messages(session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session_route(session_id: str) -> Response:
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MessageFeedback(BaseModel):
    """👍/👎 on one assistant reply; ``null`` clears it."""

    feedback: Literal["up", "down"] | None
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/sessions/{session_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
def post_message_feedback(
    session_id: str, message_id: int, body: MessageFeedback, request: Request
) -> Response:
    """Record explicit feedback on an assistant reply.

    Only the session's own caller, or the principal, may rate it: feedback
    feeds per-person learning, so a rating left on someone else's session
    would be read as that person's reaction."""
    exists, owner = get_session_owner(session_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Session not found")
    caller = _resolve_caller_person_id(request)
    if not is_principal_or_self(caller, owner):
        raise HTTPException(status_code=403, detail="Not your session")
    if not set_message_feedback(
        session_id, message_id, body.feedback, body.note, by_person_id=caller
    ):
        raise HTTPException(status_code=404, detail="Message not found")

    from openexecutive.audit import log_event

    log_event(
        "attunement",
        f"feedback={body.feedback or 'cleared'} message_id={message_id}",
        session_id=session_id,
        actor="user",
        details={
            "op": "feedback",
            "message_id": message_id,
            "feedback": body.feedback,
            "person_id": caller,
            "has_note": bool(body.note),
        },
    )
    if body.feedback == "down":
        # A thumbs-down is the clearest style signal there is: re-learn the
        # rater's working style now rather than after the next N messages
        # (still paced and budgeted inside).
        from openexecutive.attunement.style import schedule_style_pass

        schedule_style_pass(caller, force=True, session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
