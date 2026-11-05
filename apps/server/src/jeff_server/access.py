"""Private adapters: who may call an adapter, from its manifest's access field (contract/schemas/manifest.schema.json).

- absent: every caller the server admits, as for any adapter;
- "owner": only the owner -- a caller holding every licence (jeff_server.auth: the API key, a console session, or
  anyone on a server with no credentials);
- {"accounts": [<account id>, ...]}: the owner and the hub accounts listed: an account key of one of them, or the
  hub playground's key with X-Jeff-Account naming one (jeff_server.accounts makes both Caller("account", <id>)).
  The playground on its own, a hub-signed token and any other caller are not admitted.

A caller not admitted is never told the adapter exists: every door works on the caller's own view of the catalogue
(visible_catalogue), which holds every manifest but those of the adapters the caller may not use. Their IDs and
aliases are then unknown names in it, so a call, a dispatch through them, an escalation step or a graph node naming
one is answered exactly as for a name the server does not have (UnknownFunction, or the error the door gives an
unknown name), and every listing leaves them out (GET /v1/functions, /v1/adapters, /v1/models, /v1/graphs, /health,
MCP tools/list, the console's examples). A registered graph with a node that calls one is not registered for that
caller (visible_graphs).

So that a view is closed under routing, the catalogue is refused at build time (check_access, ManifestError) when a
router routes to an adapter that some caller who may use the router may not use: a router open to everyone routes
only to adapters open to everyone, an owner-only router to anything, and a router for some accounts to adapters
open to everyone or to at least those accounts.

Views are built once per catalogue and set of hidden adapters, and kept for as long as the catalogue lives, so a
request costs one pass over the private adapters (a few) and one dictionary lookup."""

import copy
import re
import threading
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import ManifestError, UnknownFunction
from jeff.manifest import Manifest

from jeff_server.auth import Caller

if TYPE_CHECKING:
    from jeff_server.decide import RegisteredGraph

OWNER = "owner"
# The account id pattern of contract/schemas/accounts_keys.schema.json, matched against the whole value: the schema's
# pattern is matched with re.search, whose $ also matches before a final newline.
ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class Access:
    """A private adapter's access: the accounts admitted besides the owner (none: the owner only)."""
    accounts: frozenset[str]

    def admits(self, caller: Caller) -> bool:
        if caller.licences is None:  # the owner
            return True
        return caller.kind == "account" and caller.subject in self.accounts

    def covers(self, other: "Access | None") -> bool:
        """Whether every caller `other` admits is admitted here too (None: everyone)."""
        if other is None:
            return False
        return other.accounts <= self.accounts


def access_of(manifest: Manifest) -> Access | None:
    """The manifest's access (None when it has none: everyone). The schema has checked its shape; an account id is
    checked here against the whole pattern (ManifestError)."""
    if "access" not in manifest.raw:
        return None
    raw = manifest.raw["access"]
    if raw == OWNER:
        return Access(frozenset())
    accounts = raw["accounts"]
    for place, account in enumerate(accounts):
        if not ACCOUNT_ID.fullmatch(account):
            raise ManifestError(f"{manifest.path}: access.accounts[{place}] is not an account id: it has characters "
                                f"outside {ACCOUNT_ID.pattern}")
    return Access(frozenset(accounts))


@dataclass
class _Private:
    """One catalogue's private adapters (ID -> access) and the views built from it (hidden IDs -> catalogue)."""
    access: dict[str, Access]
    views: dict[frozenset[str], Catalogue] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_BY_CATALOGUE: "weakref.WeakKeyDictionary[Catalogue, _Private]" = weakref.WeakKeyDictionary()
_BY_CATALOGUE_LOCK = threading.Lock()


def _private(catalogue: Catalogue) -> _Private:
    with _BY_CATALOGUE_LOCK:
        known = _BY_CATALOGUE.get(catalogue)
    if known is not None:
        return known
    access = {manifest.id: found for manifest in catalogue.manifests()
              if (found := access_of(manifest)) is not None}
    built = _Private(access)
    with _BY_CATALOGUE_LOCK:
        return _BY_CATALOGUE.setdefault(catalogue, built)


def check_access(catalogue: Catalogue) -> None:
    """At every catalogue build: each access is valid, and each router routes only to adapters every caller who may
    use the router may use too (ManifestError naming the router and the target). A target that does not resolve is
    left to the router check (jeff.routing.check_routers)."""
    private = _private(catalogue)
    for manifest in catalogue.manifests():
        if not manifest.is_router:
            continue
        router_access = private.access.get(manifest.id)
        for target in manifest.routes_to:
            try:
                target_id = catalogue.adapter(target).id
            except UnknownFunction:
                continue
            target_access = private.access.get(target_id)
            if target_access is None or target_access.covers(router_access):
                continue
            raise ManifestError(f"{manifest.path}: router {manifest.id} routes to {target_id}, whose access "
                                f"({_describe(target_access)}) is narrower than the router's "
                                f"({_describe(router_access)}); a caller who may use the router must be able to use "
                                "every adapter it routes to. Give the router the same access, or the target a wider "
                                "one")


def check_no_wider_access(catalogue: Catalogue, replacing: Iterable[Manifest]) -> None:
    """A manifest that replaces the catalogue's manifest of the same ID (an adapter folder's, or one being
    published) may not open the adapter to more callers than the catalogue's does: a folder copy that has lost its
    access line would otherwise make a private adapter public. ManifestError naming the adapter, both accesses and
    both files."""
    known = {manifest.id: manifest for manifest in catalogue.manifests()}
    for manifest in replacing:
        if manifest.id not in known:
            continue
        before, after = access_of(known[manifest.id]), access_of(manifest)
        if before is None or before.covers(after):
            continue
        raise ManifestError(f"{manifest.path} replaces the catalogue's manifest of {manifest.id} "
                            f"({known[manifest.id].path}) with a wider access: {_field(after)} instead of "
                            f"{_field(before)}. Give the folder's manifest the catalogue's access line, or change "
                            "the catalogue's manifest first")


def check_private_adapters(catalogue: Catalogue, required: Iterable[str]) -> None:
    """When `required` (JEFF_PRIVATE_ADAPTERS) lists any adapter, it and the catalogue's private adapters (those
    whose manifest has an access field) must agree exactly: every listed ID is in the catalogue with an access
    field (a server whose manifests for them are copies that lost the line would otherwise serve them to everyone),
    and every adapter with an access field is listed (so the list, kept by hand, cannot fall behind the manifests).
    ManifestError naming each ID that is missing, each listed one that has no access with its manifest file, and
    each private one that is not listed with its manifest file. An empty `required` (the setting unset, allowed
    only outside accounts mode: jeff_server.settings) checks nothing."""
    required = list(required)
    if not required:
        return
    known = {manifest.id: manifest for manifest in catalogue.manifests()}
    missing = [adapter_id for adapter_id in required if adapter_id not in known]
    open_ones = [f"{adapter_id} ({known[adapter_id].path})" for adapter_id in required
                 if adapter_id in known and access_of(known[adapter_id]) is None]
    unlisted = [f"{manifest.id} ({manifest.path})" for manifest in sorted(known.values(), key=lambda m: m.id)
                if access_of(manifest) is not None and manifest.id not in required]
    problems = []
    if missing:
        problems.append(f"it lists adapters not in the catalogue or any adapter folder: {', '.join(missing)}")
    if open_ones:
        problems.append(f"it lists adapters without an access field, so open to everyone: {', '.join(open_ones)}")
    if unlisted:
        problems.append(f"it does not list adapters whose manifest has an access field: {', '.join(unlisted)}")
    if problems:
        raise ManifestError("JEFF_PRIVATE_ADAPTERS must list exactly the adapters whose manifest has an access "
                            "field, but " + "; ".join(problems) + ". Give each listed adapter's manifest its "
                            "access line (access: owner, or access: {accounts: [...]}), and list in "
                            "JEFF_PRIVATE_ADAPTERS every adapter whose manifest has one")


def _field(access: Access | None) -> str:
    """An access as its manifest line says it."""
    if access is None:
        return "no access field (everyone)"
    if not access.accounts:
        return "access: owner"
    return f"access: {{accounts: [{', '.join(sorted(access.accounts))}]}}"


def describe_private(catalogue: Catalogue) -> str:
    """Every private adapter of the catalogue and its access, by adapter ID, for the log; "none" when there is
    none."""
    access = _private(catalogue).access
    if not access:
        return "none"
    return "; ".join(f"{adapter_id}: {_describe(access[adapter_id])}" for adapter_id in sorted(access))


def _describe(access: Access | None) -> str:
    if access is None:
        return "everyone"
    if not access.accounts:
        return "the owner only"
    return "the owner and account(s) " + ", ".join(sorted(access.accounts))


def private_ids(catalogue: Catalogue) -> frozenset[str]:
    """The IDs of the catalogue's adapters that have an access field (hidden from a caller not signed in)."""
    return frozenset(_private(catalogue).access)


def hidden_from(caller: Caller, catalogue: Catalogue) -> frozenset[str]:
    """The IDs of the catalogue's adapters this caller may not use."""
    if caller.licences is None:
        return frozenset()
    return frozenset(adapter_id for adapter_id, access in _private(catalogue).access.items()
                     if not access.admits(caller))


def visible_catalogue(caller: Caller, catalogue: Catalogue) -> Catalogue:
    """The catalogue as this caller sees it: `catalogue` itself when the caller may use every adapter, otherwise the
    one without the adapters it may not use (built once per catalogue and hidden set)."""
    hidden = hidden_from(caller, catalogue)
    if not hidden:
        return catalogue
    private = _private(catalogue)
    with private.lock:
        view = private.views.get(hidden)
        if view is None:
            view = private.views[hidden] = catalogue.without(hidden)
    return view


def visible_jeff(shared: Jeff, caller: Caller) -> Jeff:
    """`shared` with the caller's view of its catalogue: `shared` itself when nothing is hidden, otherwise a shallow
    copy sharing its engine, log and caches (nothing on the shared Jeff changes)."""
    catalogue = visible_catalogue(caller, shared.catalogue)
    if catalogue is shared.catalogue:
        return shared
    view = copy.copy(shared)
    view.catalogue = catalogue
    return view


def visible_graphs(caller: Caller, catalogue: Catalogue,
                   graphs: Mapping[str, "RegisteredGraph"]) -> Mapping[str, "RegisteredGraph"]:
    """The registered graphs this caller may see: those with no node calling an adapter it may not use (a node
    whose function does not resolve stays, to fail as it does for everyone)."""
    hidden = hidden_from(caller, catalogue)
    if not hidden:
        return graphs
    return {graph_id: registered for graph_id, registered in graphs.items()
            if not any(_adapter_of(catalogue, node.call) in hidden
                       for node in registered.graph.nodes.values() if node.call is not None)}


def _adapter_of(catalogue: Catalogue, ref: str) -> str | None:
    try:
        return catalogue.adapter(ref).id
    except UnknownFunction:
        return None
