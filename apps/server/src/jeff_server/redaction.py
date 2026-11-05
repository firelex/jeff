"""Errors worded again for a caller who is not the owner, so an error never names a private adapter
(jeff_server.access) or a private manifest to someone who may not know it exists.

Some errors carry text only the owner should read: a disagreement between engines lists every engine's served
adapters, private ones included (jeff_server.router, jeff_server.operations), and a failed catalogue rebuild quotes
the manifest problem that stopped it (jeff_server.catalogue_refresh). Where such an error is raised, it is registered
here with how to word it for a caller who is not the owner (reword). Every door, just before it sends an error, passes
it through as_seen_by: the owner gets the error as raised; anyone else gets the rewording, and the full text goes to
the server's log. Two things are cut from every error a non-owner gets, registered or not, since errors from the
engines arrive already made (over the engine socket): the lists of loaded adapters an engine names, cut to the
adapters the caller may see, and the engine socket paths.

The registry is weak (an error is forgotten once nothing holds it) and lives beside the error rather than on it,
because an error's attributes become the details it is sent with (jeff.wire.error_to_wire)."""

import logging
import re
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from jeff.catalogue import Catalogue
from jeff.engine import BASE_ADAPTER
from jeff.errors import EngineError, JeffError

from jeff_server.access import visible_catalogue
from jeff_server.auth import Caller

log = logging.getLogger("jeff_server")

# How to word an error for a caller who is not the owner: given the adapter IDs that caller may see.
type Wording = Callable[[frozenset[str]], JeffError]

_WORDINGS: "weakref.WeakKeyDictionary[BaseException, Wording]" = weakref.WeakKeyDictionary()
_LOCK = threading.Lock()


def reword[E: JeffError](error: E, wording: Wording) -> E:
    """Registers how `error` is worded for a caller who is not the owner, and returns it, to raise."""
    with _LOCK:
        _WORDINGS[error] = wording
    return error


def visible_ids(caller: Caller | None, catalogue: Catalogue) -> frozenset[str]:
    """The adapter IDs an error may name to this caller: those of its view of the catalogue, and the base. None
    (a request whose caller is not known yet) sees only the adapters open to everyone."""
    if caller is None:
        caller = Caller("hub", "nobody", frozenset())  # not the owner, and admitted by no access list
    return frozenset(manifest.id for manifest in visible_catalogue(caller, catalogue).manifests()) | {BASE_ADAPTER}


# A list of adapters an engine names in an error ("not loaded in this engine (loaded: ['a', 'b'])", built by
# jeff_runtime.wording.loaded for the local engine and its fast path; the server does not depend on the runtime, so
# the wording is matched here, and test_access_leaks builds its engine message with that helper): cut to the adapters
# the caller may see.
LOADED_LIST = re.compile(r"\(loaded: \[([^\]]*)\]\)")
QUOTED = re.compile(r"'([^']*)'")
# A reworded error is logged at most once per error type per LOG_INTERVAL_S, with the count of the others.
LOG_INTERVAL_S = 60.0
_LOGGED: dict[str, tuple[float, int]] = {}  # error type -> (when last logged, rewordings since then not logged)


def as_seen_by(error: JeffError, caller: Caller | None, catalogue: Callable[[], Catalogue],
               sockets: Callable[[], Sequence[Path]], where: str) -> JeffError:
    """`error` as this caller may see it. The owner gets it as raised. Anyone else gets its registered rewording,
    if it has one, against the catalogue now in force (catalogue(), read only when needed), and then, in whatever
    text is sent, every list of loaded adapters an engine gave cut to the adapters the caller may see, and every
    engine socket path (sockets(), the server's JEFF_ENGINE_SOCKETS) replaced by "engine <n>", its place in that
    list. When the text changed, the full text is logged (`where` names the request), at most once per error type
    per minute with the count of the others."""
    if caller is not None and caller.licences is None:
        return error
    with _LOCK:
        wording = _WORDINGS.get(error)
    shown = error if wording is None else wording(visible_ids(caller, catalogue()))
    text = cut(str(shown), lambda: visible_ids(caller, catalogue()), sockets)
    if text != str(shown):
        shown = _with_message(shown, text)
    if str(shown) != str(error):
        _log_reworded(error, caller, where)
    return shown


def cut(text: str, visible: Callable[[], frozenset[str]], sockets: Callable[[], Sequence[Path]]) -> str:
    """`text` with every list of loaded adapters an engine gave cut to the adapters visible() names (read only when
    there is such a list), and every engine socket path (sockets(), the server's JEFF_ENGINE_SOCKETS) replaced by
    "engine <n>", its place in that list: what as_seen_by does to the text of any error a caller who is not the
    owner gets, also used for engine text that is not an error (GET /health)."""
    if LOADED_LIST.search(text):
        allowed = visible()
        text = LOADED_LIST.sub(lambda found: "(loaded: " + str(
            [name for name in QUOTED.findall(found.group(1)) if name in allowed]) + ")", text)
    # longest first, so a path that begins another (/s/e.sock and /s/e.sock2) never replaces part of it
    for place, path in sorted(enumerate(map(str, sockets())), key=lambda item: -len(item[1])):
        text = text.replace(path, f"engine {place}")
    return text


def _with_message[E: JeffError](error: E, text: str) -> E:
    """A copy of `error` (same type, same details) with another message."""
    copied = type(error).__new__(type(error))
    Exception.__init__(copied, text)
    copied.__dict__.update(vars(error))
    return copied


def _log_reworded(error: JeffError, caller: Caller | None, where: str) -> None:
    name = type(error).__name__
    now = time.monotonic()
    with _LOCK:
        last, unlogged = _LOGGED.get(name, (None, 0))
        if last is not None and now - last < LOG_INTERVAL_S:
            _LOGGED[name] = (last, unlogged + 1)
            return
        _LOGGED[name] = (now, 0)
    others = f" ({unlogged} more {name} reworded since the last such line, not logged)" if unlogged else ""
    log.error("%s: answered a caller who is not the owner (%s) with a shorter %s%s; the full text: %s", where,
              caller.log_user if caller is not None else "not signed in", name, others, error)


def served_lists_error(prefix: str, named: Sequence[tuple[str, Iterable[str]]], item: Callable[[str, list[str]], str],
                       separator: str, suffix: str = "") -> EngineError:
    """An EngineError naming each engine's served adapters (prefix, then item(engine, sorted adapters) for each,
    joined by separator, then suffix), registered so a caller who is not the owner sees each list cut to the adapters
    it may see."""
    lists = [(engine, sorted(adapters)) for engine, adapters in named]

    def worded(visible: frozenset[str] | None) -> EngineError:
        shown = [(engine, adapters if visible is None else [adapter for adapter in adapters if adapter in visible])
                 for engine, adapters in lists]
        return EngineError(prefix + separator.join(item(engine, adapters) for engine, adapters in shown) + suffix)

    return reword(worded(None), worded)
