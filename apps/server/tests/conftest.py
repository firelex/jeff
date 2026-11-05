"""Fixtures shared by the server's tests (helpers that are not fixtures are in server_testing.py)."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server_testing import hub_keys


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    """A hub key pair: the private key (to sign tokens) and the public key's PEM file (JEFF_HUB_PUBLIC_KEY)."""
    return hub_keys(tmp_path)
