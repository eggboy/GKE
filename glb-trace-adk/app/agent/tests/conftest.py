"""Shared fixtures for the agent tests.

``GOOGLE_CLOUD_PROJECT`` must exist before ``main`` is imported because logging is configured at import time.
"""

import os

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

from collections.abc import Iterator

import pytest

import main
from tests.support import FakeRunner, remaining_sessions


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[FakeRunner]:
    """Replace the module-level runner; ``indirect`` parametrize with ``True`` makes it fail mid-stream."""
    runner = FakeRunner(main._direct_runner.session_service, fail=getattr(request, "param", False))
    monkeypatch.setattr(main, "_direct_runner", runner)
    yield runner
    assert remaining_sessions() == [], "every /order session must be deleted when the request ends"
