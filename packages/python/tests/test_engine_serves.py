"""jeff.engine.serves: whether an engine serves an adapter, asking it afresh only when its cached list says no."""

from typing import Any

from jeff.engine import EngineResult, serves


class CachedEngine:
    """An engine whose cached list is `cached` and whose fresh answer is `fresh`; it records each question."""

    def __init__(self, cached: set[str], fresh: set[str]) -> None:
        self.cached, self.fresh = cached, fresh
        self.asked: list[bool] = []

    def installed(self, refresh: bool = False) -> set[str]:
        self.asked.append(refresh)
        return self.fresh if refresh else self.cached

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        raise AssertionError("not called")


def test_an_adapter_in_the_cached_list_is_served_without_asking_again() -> None:
    engine = CachedEngine({"a.b.c"}, set())
    assert serves(engine, "a.b.c")
    assert engine.asked == [False]


def test_an_adapter_missing_from_the_cached_list_is_asked_about_once_more() -> None:
    engine = CachedEngine(set(), {"a.b.c"})
    assert serves(engine, "a.b.c")
    assert engine.asked == [False, True]


def test_an_adapter_missing_from_both_is_not_served() -> None:
    engine = CachedEngine(set(), set())
    assert not serves(engine, "a.b.c")
    assert engine.asked == [False, True]
