"""Helpers shared by the server's tests: a free TCP port, a hub key pair and the tokens it signs, and a copy of a
fixture catalogue with some adapters made proprietary. (The `keys` fixture, a hub key pair per test, is in
conftest.py.)"""

import shutil
import socket
import time
from pathlib import Path
from typing import Any

import jwt
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def hub_keys(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    """A hub key pair: the private key (to sign tokens) and the public key's PEM file (JEFF_HUB_PUBLIC_KEY)."""
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "hub.pub.pem"
    path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, path


def token(private: Ed25519PrivateKey, sub: str = "alice", licences: list[str] | None = None, ttl_s: int = 600,
          **claims: Any) -> str:
    """A hub-signed token for `sub` holding `licences`, lasting ttl_s seconds; a claim given as None is left out."""
    now = int(time.time())
    payload: dict[str, Any] = {"sub": sub, "licences": licences if licences is not None else [], "iat": now,
                               "exp": now + ttl_s, **claims}
    return jwt.encode({key: value for key, value in payload.items() if value is not None}, private, algorithm="EdDSA")


def proprietary(source: Path, tmp_path: Path, *adapters: str, backup: bool = False) -> Path:
    """A copy of a fixture catalogue in which `adapters` are proprietary; with backup, it also has
    jeff.support.backup (a second, uncalibrated urgency function) for escalation."""
    catalogue = tmp_path / f"catalogue-{source.name}"
    shutil.copytree(source, catalogue)
    if backup:
        second = yaml.safe_load((source / "jeff.support.triage" / "manifest.yaml").read_text())
        second["id"], second["aliases"] = "jeff.support.backup", ["backup"]
        second["functions"] = {"urgency": {**second["functions"]["urgency"], "calibration": None}}
        (catalogue / "jeff.support.backup").mkdir()
        (catalogue / "jeff.support.backup" / "manifest.yaml").write_text(yaml.safe_dump(second))
    for adapter in adapters:
        path = catalogue / adapter / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        manifest["licence"] = {"kind": "proprietary", "adapter": "Commercial"}
        manifest["weights"] = {"proprietary": {"repo": "jeffhub/private", "path": adapter}}
        path.write_text(yaml.safe_dump(manifest))
    return catalogue
