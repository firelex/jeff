import inspect

import jeff.errors as errors
from jeff.contract import error_catalogue
from jeff.errors import JeffError


def error_classes() -> dict[str, type[JeffError]]:
    return {name: cls for name, cls in inspect.getmembers(errors, inspect.isclass)
            if issubclass(cls, JeffError)}


def test_every_error_class_is_in_the_catalogue_and_back() -> None:
    assert set(error_classes()) == set(error_catalogue())


def test_statuses_are_http_or_null() -> None:
    for spec in error_catalogue().values():
        assert spec.status is None or 400 <= spec.status <= 599, spec


def test_known_statuses() -> None:
    catalogue = error_catalogue()
    assert catalogue["NotInstalled"].status == 424
    assert catalogue["Busy"].status == 529
    assert catalogue["LowConfidence"].status == 409
    assert catalogue["ProtocolMismatch"].status is None
    assert catalogue["UsageLimitReached"].status == 402


def test_all_exports_every_error() -> None:
    assert set(error_classes()) <= set(errors.__all__)
