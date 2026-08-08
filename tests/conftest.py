"""Shared fixtures.

Every test gets its own rendezvous port, so tests never see each other's traffic and can run in any order.
"""

import asyncio
import itertools
import sys
from collections.abc import Callable, Mapping

import pytest

_ports = itertools.count(19820)


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> Mapping[str, Callable]:
    """Choose each test's event-loop flavor. Only Windows has one to choose.

    There the suite needs two: `asyncio.create_subprocess_exec` in test_hard_rules is implemented only by the
    proactor loop, while the in-process Backends everywhere else wait on ZMQ sockets via `loop.add_reader`, which
    only the selector loop provides -- pyzmq (zmq/asyncio.py) raises RuntimeError on a proactor loop. This hook is
    pytest-asyncio's (>= 1.4) replacement for the loop-policy fixtures, which stand on the API 3.14 deprecates.

    Defining the hook obliges it: returning None for any item is a UsageError, hence the explicit default branch.
    """
    match sys.platform, item.path.stem:
        case "win32", "test_hard_rules":
            return {"proactor": asyncio.ProactorEventLoop}
        case "win32", _:
            return {"selector": asyncio.SelectorEventLoop}
        case _:
            return {"default": asyncio.new_event_loop}


@pytest.fixture
def endpoint() -> str:
    """A rendezvous endpoint nobody else is using."""
    return f"tcp://127.0.0.1:{next(_ports)}"
