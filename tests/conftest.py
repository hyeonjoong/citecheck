"""Test-suite guards.

Enforce the project's offline-test invariant: no unit test may make a real
network connection. Any accidental live call (e.g. a CrossrefClient created
without an injected transport) fails loudly instead of silently hitting the
network — which would make CI flaky and could leak a real ``--mailto`` address.
"""

import socket

import pytest


class _NoNetwork(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def guard(*args, **kwargs):
        raise _NoNetwork(
            "A test attempted a real network connection. Inject a fake transport "
            "(_fetch/_resolve) or a fake client instead."
        )

    # Block the lowest-level entry points urllib uses.
    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    yield
