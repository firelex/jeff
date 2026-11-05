"""Who used the server, from where: an append-only log of sign-ins and API calls (ported from jeff-server's
access_log.py).

JEFF_ACCESS_LOG names the log file (JSON lines; several servers may append to the same file). It has no default:
unset, nothing is logged; set, every /v1 and /mcp request, every /v1/channel connection (jeff_server.channel), every
console page load (jeff_server.console, with no user), every console page view and every console sign-in is
appended. A console session is logged by its bare login, a hub token or the API key as "<kind>:<subject>"
(jeff_server.auth.Caller.log_user). JEFF_ADMINS lists the logins that may read it (the console's Access log page).
JEFF_GEOIP_DB names an IP location database in MaxMind format (for example DB-IP's free "IP to City Lite"), opened
at startup and used to add a city and country to each entry; the lookup runs on this machine, so no address is sent
anywhere. Private, loopback, link-local and Tailscale (100.64.0.0/10) addresses are never looked up.

The client's address is the last X-Forwarded-For entry when there is one: Tailscale Funnel appends the address it
received the request from, so earlier entries are whatever the visitor sent and cannot be trusted. Without the header
it is the connection's address. The whole header is kept too. That last entry is trusted as the client's address, so
run the server behind a proxy that sets X-Forwarded-For (Tailscale Funnel does); reached directly, a client can write
any address there.

The file is opened for appending once at startup, so a log that cannot be written stops the server before it starts.
"""

import ipaddress
import json
import threading
import time
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from jeff.errors import EngineError, InvalidRequest

PRIVATE = "private network (tailnet or LAN)"
NOT_AN_ADDRESS = "not an IP address"
TAILNET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's own addresses


class GeoReader(Protocol):
    """What the log needs from a MaxMind database reader (maxminddb.Reader)."""

    def get(self, ip: str) -> Any: ...


def open_geoip(path: Path) -> GeoReader:
    if not path.is_file():
        raise InvalidRequest(f"JEFF_GEOIP_DB={path} does not exist; point it at a MaxMind-format IP location "
                             "database (for example DB-IP's IP to City Lite), or leave it unset to log without "
                             "locations")
    import maxminddb

    try:
        reader: GeoReader = maxminddb.open_database(str(path))
    except (maxminddb.InvalidDatabaseError, ValueError, OSError) as error:
        raise InvalidRequest(f"JEFF_GEOIP_DB={path} is not a MaxMind-format database: {error}") from error
    return reader


def client_address(forwarded_for: str | None, peer: str | None) -> str:
    if forwarded_for:
        return forwarded_for.split(",")[-1].strip()
    if peer is None:
        raise EngineError("The request has neither an X-Forwarded-For header nor a peer address")
    return peer


class AccessLog:
    def __init__(self, path: Path, *, admins: frozenset[str], geoip: GeoReader | None, server: str) -> None:
        try:
            with path.open("a", encoding="utf-8"):
                pass
        except OSError as error:
            raise InvalidRequest(f"JEFF_ACCESS_LOG={path} cannot be opened for appending: {error}") from error
        self.path, self.geoip, self.server = path, geoip, server
        self.admins = frozenset(login.strip().lower() for login in admins)
        self._lock = threading.Lock()
        self.locate = lru_cache(maxsize=4096)(self._locate)

    def _locate(self, ip: str) -> str | None:
        """'City, Region, Country' for a public address, PRIVATE for a local or tailnet one, None without a
        database (location lookup off). An address that is not an IP at all (a test client's name) is said to
        be so."""
        if self.geoip is None:
            return None
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return NOT_AN_ADDRESS
        tailnet = address.version == 4 and address in TAILNET
        if address.is_private or address.is_loopback or address.is_link_local or tailnet:
            return PRIVATE
        record = self.geoip.get(ip)
        if not record:
            return "unknown"
        parts = [record.get("city", {}).get("names", {}).get("en"),
                 (record.get("subdivisions") or [{}])[0].get("names", {}).get("en"),
                 record.get("country", {}).get("names", {}).get("en")]
        return ", ".join(part for part in parts if part) or "unknown"

    def record(self, event: str, *, user: str | None, ip: str, peer: str | None, path: str,
               status: int | None = None, detail: str | None = None, agent: str | None = None,
               forwarded: str | None = None) -> None:
        """Appends one entry: when, what happened, who (the signed-in login or caller, None when not signed in),
        from where (address, its location, the connection's own address and the whole X-Forwarded-For header),
        the path and the response status."""
        entry = {"time": time.time(), "event": event, "user": user, "ip": ip, "peer": peer,
                 "location": self.locate(ip), "path": path, "status": status, "server": self.server,
                 "detail": detail, "agent": (agent or "")[:300], "forwarded": forwarded}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def record_request(self, event: str, *, user: str | None, headers: Mapping[str, str], peer: str | None,
                       path: str, status: int, detail: str | None = None) -> None:
        """record() for an HTTP request: the address from X-Forwarded-For or the connection, and the user agent."""
        forwarded = headers.get("x-forwarded-for")
        self.record(event, user=user, ip=client_address(forwarded, peer), peer=peer, path=path, status=status,
                    detail=detail, agent=headers.get("user-agent"), forwarded=forwarded)

    def may_read(self, login: str) -> bool:
        """Only the logins in JEFF_ADMINS read the log."""
        return login.strip().lower() in self.admins

    def read(self, limit: int) -> dict[str, Any]:
        """The newest `limit` entries, newest first, and a summary per signed-in user over the whole log."""
        entries = ([json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
                   if self.path.exists() else [])
        people: dict[str, dict[str, Any]] = {}
        for entry in entries:
            user = entry["user"]
            if not user:
                continue
            person = people.setdefault(user, {"user": user, "first": entry["time"], "last": entry["time"],
                                              "sign_ins": 0, "failed_sign_ins": 0, "ips": {}, "locations": {}})
            person["last"] = max(person["last"], entry["time"])
            if entry["event"] == "signin":
                person["sign_ins"] += 1
            if entry["event"] == "signin-failed":
                person["failed_sign_ins"] += 1
            person["ips"][entry["ip"]] = person["ips"].get(entry["ip"], 0) + 1
            place = entry["location"] or "location lookup off"
            person["locations"][place] = person["locations"].get(place, 0) + 1
        anonymous = [entry for entry in entries if not entry["user"]]
        return {
            "entries": entries[::-1][:limit],
            "total": len(entries),
            "people": sorted(people.values(), key=lambda person: -person["last"]),
            "anonymous_visits": len(anonymous),
            "anonymous_ips": sorted({entry["ip"] for entry in anonymous}),
            "locations_on": self.geoip is not None,
        }
