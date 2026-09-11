"""The fail-closed guard on OE_PUBLIC_DEPLOYMENT.

An internet-reachable instance with no BACKEND_SHARED_SECRET serves an
unauthenticated API. Deployments declare themselves public by setting
OE_PUBLIC_DEPLOYMENT, and create_app() then refuses to boot without the secret
rather than logging a warning and carrying on.
"""

from __future__ import annotations

import pytest

from openexecutive.api.main import _is_public_deployment, create_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)


def test_public_deployment_without_secret_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")

    with pytest.raises(RuntimeError, match="BACKEND_SHARED_SECRET is required"):
        create_app()


def test_public_deployment_with_secret_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s" * 32)

    assert create_app() is not None


def test_local_dev_without_secret_still_boots() -> None:
    """The unset default stays permissive — `make dev` must keep working."""
    assert create_app() is not None


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "no", "off", "  "])
def test_falsey_values_do_not_arm_the_guard(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", value)

    assert _is_public_deployment() is False
    assert create_app() is not None


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "production"])
def test_truthy_and_unrecognised_values_arm_the_guard(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An unrecognised value must arm the guard, not silently skip it — for a
    fail-closed check the safe default is to require the secret."""
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", value)

    assert _is_public_deployment() is True
    with pytest.raises(RuntimeError, match="BACKEND_SHARED_SECRET is required"):
        create_app()
