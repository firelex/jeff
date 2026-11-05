"""The `mode` fixture: a remote-mode test runs once over HTTP and once over the channel (served.Mode); and `keys`, a hub
key pair per test (served.hub_keys). The whole session runs in an empty proxy environment (below)."""

import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from served import Mode, hub_keys


@pytest.fixture(params=[False, True], ids=["http", "channel"])
def mode(request: pytest.FixtureRequest) -> Mode:
    return Mode(request.param)


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    return hub_keys(tmp_path)


# The servers these tests talk to are on this machine, and httpx and websockets follow the proxy variables (trust_env),
# so one set in the shell (HTTPS_PROXY=http://127.0.0.1:9, say) would send their requests to that proxy. No test here
# tests the proxy variables; each one that needs a proxy runs its own in front of the server and connects to it by URL.
# So they are unset, in either case, for the whole session (before any test module is imported, so no client made at
# import sees them, and for every server process a test starts), and put back after it.
_PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "WS_PROXY", "WSS_PROXY", "SOCKS_PROXY")
_saved_proxies: dict[str, str] = {}


def pytest_configure(config: pytest.Config) -> None:
    for name in list(os.environ):
        if name.upper() in _PROXY_VARIABLES:
            _saved_proxies[name] = os.environ.pop(name)


def pytest_unconfigure(config: pytest.Config) -> None:
    os.environ.update(_saved_proxies)
    _saved_proxies.clear()
