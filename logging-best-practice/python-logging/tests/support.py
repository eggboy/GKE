"""Helpers shared by the test modules."""

import json
import os
from typing import Any

PROJECT_ID = "unit-test-project"


def parse_line(line: str) -> dict[str, Any]:
    """Parse one captured line, failing on a newline inside it or a token that is not JSON."""
    assert line.endswith("\n")
    assert "\n" not in line[:-1], "log agents need one JSON document per line"
    return json.loads(line, parse_constant=_reject_constant)


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def ensure_project_env() -> None:
    """Set the project before ``python_logging.main`` is imported; logging is configured at import time."""
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
