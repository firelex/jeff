"""The shipped catalogue (packages/python/src/jeff/_shipped/catalogue) must match the adapter versions the live API
serves: ServedFunctions (jeff.remote) refuses to connect when a served adapter's version differs from the local
catalogue's (see jeff.remote.version_mismatch). This guards against the SDK's shipped catalogue drifting from a v1.4
server."""

from jeff.catalogue import Catalogue
from jeff.remote import ServedFunctions


class Transport:
    def functions(self):
        return {"base_format": {"format": "bf16"},
                "functions": [{"id": "jeff.safety.guard.attempt", "adapter_version": "1.4.0", "calibration": None}]}


def test_shipped_catalogue_matches_the_served_v14_versions():
    ServedFunctions(Transport(), Catalogue.shipped(), "http://x")
