"""The scheduler's throttled alert expiry sweep (scheduler/runner.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from openexecutive.alerts import lifecycle
from openexecutive.scheduler import runner


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_last_alert_sweep_at", None)


def test_maybe_sweep_throttles_to_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[datetime] = []

    def fake_expire(now: datetime, db_path=None) -> int:  # noqa: ARG001
        calls.append(now)
        return 2

    monkeypatch.setattr(lifecycle, "expire_stale_alerts", fake_expire)
    t0 = datetime.now(UTC)
    assert runner._maybe_sweep_alerts(t0) == 2
    assert runner._maybe_sweep_alerts(t0 + timedelta(minutes=5)) == 0
    assert runner._maybe_sweep_alerts(t0 + timedelta(minutes=14)) == 0
    assert runner._maybe_sweep_alerts(t0 + timedelta(minutes=15)) == 2
    assert calls == [t0, t0 + timedelta(minutes=15)]


def test_maybe_sweep_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(now: datetime, db_path=None) -> int:  # noqa: ARG001
        raise RuntimeError("db locked")

    monkeypatch.setattr(lifecycle, "expire_stale_alerts", boom)
    assert runner._maybe_sweep_alerts(datetime.now(UTC)) == 0
    # The failed attempt still consumed the interval slot (no hot retry loop).
    assert runner._last_alert_sweep_at is not None
