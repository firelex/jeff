"""Routing between adapters: walk a tree of router functions to the function that should answer (spec Section 6).

A router's labels are the subset form over its routes_to: at each level dispatch offers the router only the targets
that implement the requested interface, or that are routers leading to a function that does."""

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from jeff.binding import JSON, Labels
from jeff.catalogue import Catalogue
from jeff.decision import Decision, RouteStep
from jeff.engine import serves
from jeff.errors import EngineError, InvalidRequest, LowConfidence, ManifestError, NotInstalled, UnknownContext, \
    UnknownFunction
from jeff.manifest import FunctionSpec

if TYPE_CHECKING:
    from jeff.client import Call, Jeff
    from jeff.escalation import Policy

MAX_DEPTH = 8
DEFAULT_ROUTER = "jeff.core.router"


def _resolve_target(catalogue: Catalogue, router_id: str, target: str) -> tuple[str, str]:
    """("router", adapter ID) or ("function", function ID) for one of a router's targets."""
    try:
        manifest = catalogue.adapter(target)
        if manifest.is_router:
            return "router", manifest.id
        return "function", catalogue.function(target).id
    except UnknownFunction as error:
        raise ManifestError(f"Router {router_id} routes to {target}: {error}") from error


def interfaces_reached(catalogue: Catalogue, router_id: str, path: tuple[str, ...] = ()) -> set[str]:
    """Every interface a router's targets implement or lead to, through nested routers. A loop raises."""
    if router_id in path:
        raise ManifestError(f"The routing tree has a loop: {' -> '.join(path + (router_id,))}")
    reached: set[str] = set()
    for target in catalogue.adapter(router_id).routes_to:
        reached |= _target_interfaces(catalogue, router_id, target, path + (router_id,))
    return reached


def check_router_reaches(catalogue: Catalogue, router: str, interface: str, *, direct: str) -> None:
    """A dispatch starting from `router` must lead to a function implementing `interface`; otherwise dispatch would
    only find out partway down the tree (ManifestError). InvalidRequest names what the router reaches; `direct` says
    how to call the function directly instead, in the terms of the door the dispatch came through."""
    manifest = catalogue.adapter(router)
    reached = interfaces_reached(catalogue, manifest.id)
    if interface not in reached:
        raise InvalidRequest(
            f"Router {manifest.id} does not lead to any function implementing {interface} (it reaches: "
            f"{', '.join(sorted(reached)) or 'nothing; it is not a router'}); name another router, or {direct}")


def _target_interfaces(catalogue: Catalogue, router_id: str, target: str, path: tuple[str, ...]) -> set[str]:
    kind, target_id = _resolve_target(catalogue, router_id, target)
    if kind == "router":
        return interfaces_reached(catalogue, target_id, path)
    return {catalogue.function(target_id).interface}


def check_routers(catalogue: Catalogue, installed: set[str]) -> dict[str, dict[str, str]]:
    """Every installed router's targets resolve, are installed and form no loop, so dispatch can always work out
    which targets lead to an interface. Returns each installed router's exact keys (exact_keys_of), worked out here
    once so that a dispatch only looks its context keys up."""
    exact_keys: dict[str, dict[str, str]] = {}
    for manifest in catalogue.manifests():
        if not manifest.is_router or manifest.id not in installed:
            continue
        for target in manifest.routes_to:
            kind, target_id = _resolve_target(catalogue, manifest.id, target)
            adapter_id = target_id if kind == "router" else catalogue.function(target_id).adapter_id
            if adapter_id not in installed:
                raise NotInstalled(target, f"router {manifest.id} routes to it")
        interfaces_reached(catalogue, manifest.id)
        exact_keys[manifest.id] = exact_keys_of(catalogue, manifest.id)
    return exact_keys


def _reachable_adapters(catalogue: Catalogue, router_id: str) -> list[str]:
    """The router and every adapter its routing tree reaches (routers and the adapters of target functions), each
    once, in the order first met."""
    seen: list[str] = []
    pending = [router_id]
    while pending:
        adapter_id = pending.pop(0)
        if adapter_id in seen:
            continue
        seen.append(adapter_id)
        manifest = catalogue.adapter(adapter_id)
        for target in manifest.routes_to:
            kind, target_id = _resolve_target(catalogue, manifest.id, target)
            pending.append(target_id if kind == "router" else catalogue.function(target_id).adapter_id)
    return seen


def exact_keys_of(catalogue: Catalogue, router_id: str) -> dict[str, str]:
    """Exact key -> code name of every context field, of every function of every adapter reachable from the router,
    whose exact key differs from its code name (e.g. 'counterparty name' -> counterparty_name). It depends only on
    the catalogue and the router, so it is worked out once per router (check_routers when the Jeff is built, or the
    router's first dispatch), never per call."""
    exact: dict[str, str] = {}
    for adapter_id in _reachable_adapters(catalogue, router_id):
        for function in catalogue.adapter(adapter_id).functions.values():
            for field in function.context:
                if field.key != field.code_name:
                    exact.setdefault(field.key, field.code_name)
    return exact


def refuse_exact_keys(exact_keys: Mapping[str, str], router_id: str, context: Mapping[str, JSON]) -> None:
    """Dispatch passes each router and the chosen leaf only the context fields its function has (its own, or its
    adapter's), by code name, so a field given by its exact key (one that differs from its code name) would be
    dropped silently. Refuse it up front, naming the code name, as binding a function directly does. `exact_keys` is
    the router's exact_keys_of; the check is one lookup per caller context key."""
    misnamed = [key for key in context if key in exact_keys]
    if misnamed:
        raise UnknownContext("context is given by code name; pass "
                             + ", ".join(f"{key!r} as {exact_keys[key]}" for key in misnamed)
                             + f" (fields of adapters reached from router {router_id})")


def _start(jeff: "Jeff", router: str) -> str:
    try:
        manifest = jeff.catalogue.adapter(router)
    except UnknownFunction as error:
        raise UnknownFunction(f"There is no router {router} in the catalogue, so this call cannot be routed; name "
                              f"the function to call with function=") from error
    if not serves(jeff.engine, manifest.id):
        raise NotInstalled(manifest.id, "there is no router installed to route this call; install it, or name the "
                           "function to call with function=")
    return manifest.id


def leaf_call(leaf: FunctionSpec, input: JSON, context: Mapping[str, JSON] | None,
              labels: Labels | None) -> "Call":
    """The call dispatch makes to the function the routers chose: the caller's context fields that it has (its own,
    or its adapter's), and the caller's labels unless its labels are fixed with no caller subset (such leaves, and
    every noul and score function, take none). A fixed-label leaf that allows a subset (labels.subset true) gets the
    caller's labels as a direct call would, so binding renders the same subset or refuses an invalid one
    (InvalidLabels) the same way."""
    from jeff.client import Call
    given = dict(context or {})
    leaf_context = {field.code_name: given[field.code_name] for field in leaf.context if field.code_name in given}
    takes_labels = leaf.labels.source != "fixed" or leaf.labels.subset is True
    return Call(leaf.id, input, leaf_context, labels if takes_labels else None)


def dispatch(jeff: "Jeff", input: JSON, *, interface: str, context: Mapping[str, JSON] | None,
             labels: Labels | None, router: str, on_low_confidence: "Policy") -> Decision:
    from jeff.client import Call
    from jeff.escalation import check_policy

    check_policy(jeff, interface, on_low_confidence)
    given = dict(context or {})
    route: list[RouteStep] = []
    current = _start(jeff, router)
    if given:
        exact_keys = jeff._exact_keys.get(current)
        if exact_keys is None:  # a router installed after the Jeff was built: worked out once, at its first dispatch
            exact_keys = jeff._exact_keys[current] = exact_keys_of(jeff.catalogue, current)
        refuse_exact_keys(exact_keys, current, given)
    for _ in range(MAX_DEPTH):
        manifest = jeff.catalogue.adapter(current)
        if not serves(jeff.engine, manifest.id):
            raise NotInstalled(manifest.id, f"router {route[-1].router} chose it")
        if not manifest.is_router:
            raise ManifestError(f"{manifest.id} is not a router")
        offered = [target for target in manifest.routes_to
                   if interface in _target_interfaces(jeff.catalogue, manifest.id, target, (manifest.id,))]
        if not offered:
            raise ManifestError(f"Router {manifest.id} has no target that implements or leads to {interface}")
        function = next(iter(manifest.functions.values()))
        router_context = {field.code_name: given[field.code_name] for field in function.context
                          if field.code_name in given}
        decision = jeff.decide([Call(function.id, input, router_context or None, offered)])[0]
        if decision.confidence is None:
            raise EngineError(f"Router {function.id} returned a decision without a confidence")
        route.append(RouteStep(function.id, str(decision.value), float(decision.confidence)))
        if decision.abstained:
            decision_with_route = replace(decision, route=tuple(route))
            if on_low_confidence == "return":
                return decision_with_route
            raise LowConfidence(decision_with_route)
        kind, target_id = _resolve_target(jeff.catalogue, manifest.id, str(decision.value))
        if kind == "router":
            current = target_id
            continue
        call = leaf_call(jeff.catalogue.function(target_id), input, given, labels)
        answered = jeff.decide([call])[0]
        return jeff._apply(call, replace(answered, route=tuple(route)), on_low_confidence)
    raise ManifestError(f"Routing from {router} went deeper than {MAX_DEPTH} levels")
