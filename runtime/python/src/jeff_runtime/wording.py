"""Wording the engine's errors share with the server that reads them.

The server cuts the list of loaded adapters out of an engine error before a caller who is not the owner sees it
(jeff_server.redaction, which matches exactly this wording), so every message that lists them builds it here."""

from collections.abc import Iterable


def loaded(adapters: Iterable[str]) -> str:
    """The loaded adapters as an error names them: "(loaded: ['a', 'b'])", sorted."""
    return f"(loaded: {sorted(adapters)})"
