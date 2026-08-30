from __future__ import annotations

from pathlib import Path

import pytest


_CORE_RUNNER = Path(__file__).with_name("anti_entropy_core_runner.py")


@pytest.fixture(autouse=True)
def _explicit_test_core_runner(monkeypatch):
    """Every conversion test crosses the explicit isolated Core boundary."""
    monkeypatch.setenv("ANTI_ENTROPY_CORE_RUNNER", str(_CORE_RUNNER.resolve()))
