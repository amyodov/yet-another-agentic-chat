"""Shared fixtures.

Every test gets its own rendezvous port and its own runtime directory, so tests
never see each other's traffic or files and can run in any order.
"""

import itertools

import pytest

from yaac import inbox

_ports = itertools.count(19820)


@pytest.fixture
def endpoint() -> str:
    """A rendezvous endpoint nobody else is using."""
    return f"tcp://127.0.0.1:{next(_ports)}"


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Redirect inbox files into the test's own directory."""
    base = tmp_path / "runtime"
    monkeypatch.setattr(inbox, "runtime_dir", lambda: base)
    return base
