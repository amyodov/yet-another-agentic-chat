"""Shared fixtures.

Every test gets its own rendezvous port, so tests never see each other's traffic and can run in any order.
"""

import itertools

import pytest

_ports = itertools.count(19820)


@pytest.fixture
def endpoint() -> str:
    """A rendezvous endpoint nobody else is using."""
    return f"tcp://127.0.0.1:{next(_ports)}"
