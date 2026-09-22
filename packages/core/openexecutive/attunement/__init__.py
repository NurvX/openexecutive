"""Attunement — the Executive adapting to each person it works with.

Built on one prerequisite: knowing who actually said what. ``chat_messages``
records the resolved rostered sender of every user message
(``sender_person_id``) and explicit 👍/👎 on replies (``feedback``), so nothing
here ever reads an outsider's words as someone on the roster.

- :mod:`openexecutive.attunement.open_loops` — commitments and asks from anyone
  on the roster become open loops the nudge engine chases once due, and close
  when the owner says they're done.
"""
