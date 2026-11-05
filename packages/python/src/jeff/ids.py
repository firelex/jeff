"""References to adapters and functions: jeff.<domain>.<name>[.<function>][@<version>], or an alias."""

from dataclasses import dataclass

from jeff.errors import InvalidReference


@dataclass(frozen=True)
class FunctionRef:
    adapter: str
    key: str | None
    version: str | None


def parse_ref(ref: str) -> FunctionRef:
    text, _, version = ref.partition("@")
    parts = text.split(".")
    if not text or any(not part for part in parts):
        raise InvalidReference(f"Malformed reference {ref!r}: use jeff.<domain>.<name>[.<function>] or "
                               "<alias>[.<function>], each part non-empty")
    if parts[0] == "jeff":
        if len(parts) < 3:
            raise InvalidReference(f"Malformed reference {ref!r}: use jeff.<domain>.<name>[.<function>]")
        adapter, key = (".".join(parts[:3]), parts[3]) if len(parts) == 4 else (text, None)
        if len(parts) > 4:
            raise InvalidReference(f"Malformed reference {ref!r}: too many parts")
    else:
        if len(parts) > 2:
            raise InvalidReference(f"Malformed reference {ref!r}: use <alias>[.<function>] (aliases have no dots)")
        adapter, key = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], None)
    return FunctionRef(adapter, key, version or None)


def version_matches(wanted: str, actual: str) -> bool:
    want = wanted.split(".")
    have = actual.split(".")
    return have[: len(want)] == want
