from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    basetemp = getattr(session.config.option, "basetemp", None)
    if basetemp:
        basetemp_path = Path(basetemp)
        basetemp_path.parent.mkdir(parents=True, exist_ok=True)
        _remove_existing_basetemp(basetemp_path)


def _remove_existing_basetemp(path: Path) -> None:
    """Make repeated --basetemp runs reliable on Windows."""

    if not path.exists():
        return

    def onerror(func, name, _exc_info):
        try:
            os.chmod(name, stat.S_IRWXU)
        except OSError:
            pass
        func(name)

    shutil.rmtree(path, onerror=onerror)


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch):
    """Tests must not call real OpenAI/Jira/GitHub or other network APIs."""

    def fail_network(*args, **kwargs):
        raise AssertionError("Real network calls are blocked in tests")

    monkeypatch.setattr("requests.sessions.Session.request", fail_network)
