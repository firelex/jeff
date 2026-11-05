import threading
import time
from typing import Any

import pytest

from jeff.engine import EngineResult, Prepared
from jeff.errors import Busy, DeadlineExceeded, EngineError, InputTooLong, InvalidRequest
from jeff.testing import FakeEngine, fake_tokens
from jeff_runtime.scheduler import Scheduler, Settings

ROW = {"state": {"message": "hi"}, "question": {"type": "noul", "instructions": "?"}}
LONG = {"state": {"__too_long__": 1}, "question": ROW["question"]}


class Slow(FakeEngine):
    """A fake engine whose predict takes a while, so requests pile up."""
    def __init__(self, delay: float) -> None:
        super().__init__(lambda adapter, row: [0.3, 0.7], {"a.b.c", "a.b.d"})
        self.delay = delay

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        time.sleep(self.delay)
        return super().predict(adapter_id, rows)


def run(scheduler: Scheduler, jobs: list[tuple[str, list[dict[str, Any]], float | None]]) -> list[Any]:
    results: list[Any] = [None] * len(jobs)
    done = threading.Event()
    remaining = [len(jobs)]
    lock = threading.Lock()

    def finisher(i: int):
        def on_done(result: Any) -> None:
            results[i] = result
            with lock:
                remaining[0] -= 1
                if remaining[0] == 0:
                    done.set()
        return on_done
    for i, (adapter, rows, deadline) in enumerate(jobs):
        scheduler.submit(adapter, rows, deadline, finisher(i))
    assert done.wait(10)
    return results


def make(engine: FakeEngine, window_ms: float = 20, max_rows: int = 64, limit: int = 4096) -> Scheduler:
    scheduler = Scheduler(engine, Settings(window_ms=window_ms, max_batch_rows=max_rows, queue_limit_rows=limit))
    scheduler.start()
    return scheduler


def test_concurrent_requests_share_one_engine_call_with_one_group_per_adapter() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=50)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.c", [ROW, ROW], None), ("a.b.d", [ROW], None)])
    scheduler.stop()
    assert [len(r.probabilities) for r in results] == [1, 2, 1]
    assert engine.batches == [[("a.b.c", 3), ("a.b.d", 1)]]
    assert sorted((adapter, len(rows)) for adapter, rows in engine.calls) == [("a.b.c", 3), ("a.b.d", 1)]
    # batch_rows: the rows of the whole engine call (the micro-batch), every adapter's
    assert all(r.timing["batch_rows"] == 4 for r in results) and results[1].timing["queue_ms"] >= 0


def test_a_bad_request_fails_alone() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=50)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.c", [ROW, LONG], None), ("a.b.c", [ROW], None)])
    scheduler.stop()
    assert isinstance(results[1], InputTooLong)
    assert isinstance(results[0], EngineResult) and isinstance(results[2], EngineResult)
    assert engine.calls == [("a.b.c", [ROW, ROW])]


def test_large_requests_are_chunked_and_reassembled() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=1, max_rows=4)
    [result] = run(scheduler, [("a.b.c", [ROW] * 10, None)])
    scheduler.stop()
    assert len(result.probabilities) == 10
    assert [len(rows) for _, rows in engine.calls] == [4, 4, 2]


def test_queue_limit_gives_busy() -> None:
    engine = Slow(0.3)
    scheduler = make(engine, window_ms=1, max_rows=2, limit=3)
    results = run(scheduler, [("a.b.c", [ROW, ROW], None), ("a.b.c", [ROW, ROW], None), ("a.b.c", [ROW, ROW], None)])
    scheduler.stop()
    assert any(isinstance(r, Busy) for r in results)
    assert scheduler.stats()["busy"] >= 1


def test_expired_deadline_never_runs() -> None:
    engine = Slow(0.3)
    scheduler = make(engine, window_ms=1)
    got: list[Any] = []
    finished = threading.Event()
    scheduler.submit("a.b.c", [ROW], None, got.append)
    time.sleep(0.05)  # the first batch (0.3 s) is running; the next request waits behind it

    def last(result: Any) -> None:
        got.append(result)
        finished.set()
    scheduler.submit("a.b.c", [ROW], time.monotonic() + 0.05, last)
    assert finished.wait(5)
    scheduler.stop()
    assert isinstance(got[-1], DeadlineExceeded)
    assert [len(rows) for _, rows in engine.calls] == [1]


def test_changes_wait_for_the_running_batch() -> None:
    engine = Slow(0.2)
    scheduler = make(engine, window_ms=1)
    order: list[str] = []
    finished = threading.Event()

    def on_result(result: Any) -> None:
        order.append("batch")

    scheduler.submit("a.b.c", [ROW], None, on_result)
    time.sleep(0.05)  # the batch is running

    def work() -> None:
        order.append("change")
        engine.remove_adapter("a.b.d")

    scheduler.change(work, lambda error: finished.set())
    assert finished.wait(5)
    scheduler.stop()
    assert order == ["batch", "change"] and engine.installed() == {"a.b.c"}


def test_engine_failure_reaches_every_request_of_the_group_and_scheduler_survives() -> None:
    class Broken(Slow):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            raise RuntimeError("CUDA error: illegal memory access")
    engine = Broken(0.0)
    scheduler = make(engine, window_ms=20)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.c", [ROW], None)])
    assert all(isinstance(r, EngineError) and "illegal memory access" in str(r) for r in results)
    engine.predict = Slow(0.0).predict  # type: ignore[method-assign]
    assert isinstance(run(scheduler, [("a.b.c", [ROW], None)])[0], EngineResult)
    scheduler.stop()


def test_stop_fails_pending_requests() -> None:
    engine = Slow(0.5)
    scheduler = make(engine, window_ms=1)
    got: list[Any] = []
    scheduler.submit("a.b.c", [ROW], None, got.append)
    time.sleep(0.05)  # the first batch (0.5 s) is running; the next request waits behind it
    scheduler.submit("a.b.c", [ROW], None, got.append)
    scheduler.stop()
    assert len(got) == 2
    assert isinstance(got[0], EngineResult)
    assert isinstance(got[1], EngineError) and "stopping" in str(got[1])


def test_change_after_stop_is_rejected() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=1)
    scheduler.stop()
    got: list[Any] = []
    scheduler.change(lambda: None, got.append)
    assert len(got) == 1
    assert isinstance(got[0], EngineError) and "stopping" in str(got[0])


def test_change_racing_stop_never_loses_the_delivery() -> None:
    for _ in range(300):
        engine = Slow(0.0)
        scheduler = make(engine, window_ms=1)
        got: list[Any] = []
        changer = threading.Thread(target=scheduler.change, args=(lambda: None, got.append))
        changer.start()
        scheduler.stop()
        changer.join(5)
        assert len(got) == 1
        assert got[0] is None or (isinstance(got[0], EngineError) and "stopping" in str(got[0]))


def test_stop_from_the_gpu_thread_raises() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=1)
    caught: list[Any] = []
    done = threading.Event()

    def on_result(result: Any) -> None:
        try:
            scheduler.stop()
        except EngineError as error:
            caught.append(error)
        done.set()

    scheduler.submit("a.b.c", [ROW], None, on_result)
    assert done.wait(5)
    assert len(caught) == 1 and "GPU thread" in str(caught[0])
    scheduler.stop()


def test_start_twice_raises() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=1)
    with pytest.raises(EngineError):
        scheduler.start()
    scheduler.stop()


def test_mismatched_probabilities_length_fails_the_group() -> None:
    class Mismatched(Slow):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            return EngineResult([[0.5, 0.5]], {"path": "fake"})  # always one row, regardless of input

    engine = Mismatched(0.0)
    scheduler = make(engine, window_ms=20)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.c", [ROW], None)])
    scheduler.stop()
    assert all(isinstance(r, EngineError) for r in results)
    assert all("1 probabilities for 2 rows" in str(r) for r in results)


def test_chunked_request_fails_whole_if_a_later_chunk_fails() -> None:
    class FailsSecondChunk(Slow):
        def __init__(self) -> None:
            super().__init__(0.0)
            self.attempts = 0

        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            self.attempts += 1
            if self.attempts == 2:
                raise RuntimeError("boom")
            return super().predict(adapter_id, rows)

    engine = FailsSecondChunk()
    scheduler = make(engine, window_ms=1, max_rows=4)
    [result] = run(scheduler, [("a.b.c", [ROW] * 10, None)])
    scheduler.stop()
    assert isinstance(result, EngineError) and "boom" in str(result)
    assert engine.attempts == 2  # the request fails as a whole; the third chunk never runs


def test_broken_gpu_thread_fails_pending_work_and_future_calls() -> None:
    class Malformed(Slow):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            time.sleep(self.delay)
            return EngineResult(None, {})  # type: ignore[arg-type]  # deliberately malformed: this escapes the
            # scheduler's own per-group try block (unlike a predict() exception, which is already handled)

    engine = Malformed(0.1)
    scheduler = make(engine, window_ms=1)
    req_got: list[Any] = []
    req_done = threading.Event()

    def on_request(result: Any) -> None:
        req_got.append(result)
        req_done.set()

    scheduler.submit("a.b.c", [ROW], None, on_request)
    time.sleep(0.02)  # the crashing batch (0.1 s) is already running; this change is still pending when it crashes
    change_got: list[Any] = []
    change_done = threading.Event()

    def on_change(result: Any) -> None:
        change_got.append(result)
        change_done.set()

    scheduler.change(lambda: None, on_change)
    assert req_done.wait(5) and change_done.wait(5)
    assert isinstance(req_got[0], EngineError) and "GPU thread failed" in str(req_got[0])
    assert isinstance(change_got[0], EngineError) and "GPU thread failed" in str(change_got[0])

    later: list[Any] = []
    scheduler.submit("a.b.c", [ROW], None, later.append)
    assert len(later) == 1
    assert isinstance(later[0], EngineError) and "GPU thread failed" in str(later[0])

    later_change: list[Any] = []
    scheduler.change(lambda: None, later_change.append)
    assert len(later_change) == 1
    assert isinstance(later_change[0], EngineError) and "GPU thread failed" in str(later_change[0])

    scheduler.stop()  # the GPU thread is already dead; stop() must still return cleanly


def test_stats() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=1)
    run(scheduler, [("a.b.c", [ROW], None)])
    stats = scheduler.stats()
    scheduler.stop()
    assert stats["batches"] == 1 and stats["rows"] == 1 and stats["model_ms_p50"] is not None


# ---- final review fixes


def test_a_small_request_is_not_held_up_by_a_chunked_one_on_the_same_adapter() -> None:
    """A large request is split into chunks of max_batch_rows; a small request that arrives while the large one is
    part-way through must run in the very next batch, not wait for every remaining chunk."""
    engine = Slow(0.05)
    scheduler = make(engine, window_ms=1, max_rows=4)
    got: dict[str, Any] = {}
    both = threading.Event()

    def finisher(name: str):
        def on_done(result: Any) -> None:
            got[name] = result
            if len(got) == 2:
                both.set()
        return on_done

    scheduler.submit("a.b.c", [ROW] * 40, None, finisher("big"))
    time.sleep(0.01)  # the big request's first chunk (0.05 s) is running
    scheduler.submit("a.b.c", [ROW], None, finisher("small"))
    assert both.wait(10)
    scheduler.stop()
    assert isinstance(got["small"], EngineResult) and isinstance(got["big"], EngineResult)
    assert len(got["big"].probabilities) == 40
    # about one batch (the running first chunk), never the ten chunks of the big request
    assert got["small"].timing["queue_ms"] < 120, got["small"].timing
    assert engine.calls[1][1].count(ROW) == 4 and len(engine.calls) == 11  # 1 small + 3 big rows share batch 2


def test_queue_ms_is_waiting_time_only_not_model_time() -> None:
    engine = Slow(0.3)
    scheduler = make(engine, window_ms=1)
    [result] = run(scheduler, [("a.b.c", [ROW], None)])
    scheduler.stop()
    assert result.timing["queue_ms"] < 100, result.timing


@pytest.mark.parametrize(("settings", "named"), [
    ({"window_ms": 0, "max_batch_rows": 4, "queue_limit_rows": 8}, "window_ms"),
    ({"window_ms": -1, "max_batch_rows": 4, "queue_limit_rows": 8}, "window_ms"),
    ({"window_ms": 1, "max_batch_rows": 0, "queue_limit_rows": 8}, "max_batch_rows"),
    ({"window_ms": 1, "max_batch_rows": 8, "queue_limit_rows": 4}, "queue_limit_rows"),
])
def test_settings_are_validated(settings: dict[str, Any], named: str) -> None:
    with pytest.raises(InvalidRequest, match=named):
        Settings(**settings)


def test_a_request_larger_than_the_queue_limit_is_invalid_not_busy() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=1, max_rows=2, limit=3)
    [result] = run(scheduler, [("a.b.c", [ROW] * 4, None)])
    scheduler.stop()
    assert isinstance(result, InvalidRequest) and "4 rows" in str(result) and "3" in str(result)


def test_busy_without_any_batch_history_says_retry_after_one_second() -> None:
    engine = Slow(0.3)
    scheduler = make(engine, window_ms=1, max_rows=2, limit=3)
    scheduler.submit("a.b.c", [ROW], None, lambda result: None)
    time.sleep(0.05)  # the first batch is running and has not finished: there is no batch time to go on yet
    scheduler.submit("a.b.c", [ROW, ROW], None, lambda result: None)
    busy: list[Any] = []
    scheduler.submit("a.b.c", [ROW, ROW], None, busy.append)
    scheduler.stop()
    assert isinstance(busy[0], Busy) and busy[0].retry_after_s == 1.0


def test_busy_estimates_retry_after_from_recent_batch_times() -> None:
    engine = Slow(0.1)
    scheduler = make(engine, window_ms=1, max_rows=2, limit=4)
    run(scheduler, [("a.b.c", [ROW], None)])  # one batch of history: about 0.1 s
    got: list[Any] = []
    scheduler.submit("a.b.c", [ROW, ROW], None, got.append)
    time.sleep(0.03)  # that batch is running; its rows are no longer queued
    scheduler.submit("a.b.c", [ROW, ROW], None, got.append)
    scheduler.submit("a.b.c", [ROW, ROW], None, got.append)  # 4 rows queued: the limit
    busy: list[Any] = []
    scheduler.submit("a.b.c", [ROW, ROW], None, busy.append)
    scheduler.stop()
    assert isinstance(busy[0], Busy)
    assert 0.05 < busy[0].retry_after_s < 0.5, busy[0].retry_after_s  # one 2-row batch must drain first


def test_queued_rows_counts_only_rows_not_yet_running() -> None:
    engine = Slow(0.3)
    scheduler = make(engine, window_ms=1)
    scheduler.submit("a.b.c", [ROW], None, lambda result: None)
    time.sleep(0.05)  # that row is running
    assert scheduler.stats()["queued_rows"] == 0
    scheduler.submit("a.b.c", [ROW, ROW], None, lambda result: None)
    assert scheduler.stats()["queued_rows"] == 2
    scheduler.stop()


def test_broken_is_exposed_and_listeners_hear_it_once() -> None:
    class Malformed(Slow):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            return EngineResult(None, {})  # type: ignore[arg-type]  # escapes the per-group error handling

    scheduler = make(Malformed(0.0), window_ms=1)
    assert scheduler.broken is None
    heard: list[Any] = []
    heard_once = threading.Event()

    def listener(error: Any) -> None:
        heard.append(error)
        heard_once.set()

    scheduler.when_broken(listener)
    scheduler.submit("a.b.c", [ROW], None, lambda result: None)
    assert heard_once.wait(5)
    assert isinstance(scheduler.broken, EngineError) and heard == [scheduler.broken]
    late: list[Any] = []
    scheduler.when_broken(late.append)  # registered after the break: told at once
    assert late == [scheduler.broken]
    scheduler.stop()


# ---- carry-over from 2a-1


def test_a_started_chunked_request_keeps_at_least_half_of_each_batch_while_starved() -> None:
    """Adapted from the reviewer's starvation probe (/private/tmp/claude-501/rr/starve.py): a 20-row request is
    chunked at max_batch_rows=4, while 1-row requests on the same adapter keep arriving faster than the batches
    drain. Before the fix, not-yet-started requests always filled the whole batch first, so the big request could
    be skipped (0 of its rows) batch after batch as long as fresh small requests kept arriving. After the fix it
    must keep making progress every single batch until it is done: never skipped, and carrying at least half of
    max_batch_rows (2) of its rows except possibly in its last (tail) batch."""
    engine = Slow(0.02)
    scheduler = make(engine, window_ms=5, max_rows=4, limit=400)
    big_row = {**ROW, "marker": "big"}
    small_row = {**ROW, "marker": "small"}
    big_done = threading.Event()
    scheduler.submit("a.b.c", [big_row] * 20, None, lambda r: big_done.set())
    deadline = time.monotonic() + 2
    sent = 0
    while time.monotonic() < deadline and not big_done.is_set():
        for _ in range(4):
            scheduler.submit("a.b.c", [small_row], None, lambda r: None)
            sent += 1
        time.sleep(0.01)
    finished = big_done.wait(0.5)  # only enough to let an in-flight batch land; no long drain grace period,
                                    # which would let a starved big request "finish late" once sending stops
    scheduler.stop()
    assert finished, "the big request was starved and never finished"
    assert sent > 20, "small requests must really have arrived faster than the batches drained"
    big_counts = [sum(1 for row in rows if row is big_row) for _, rows in engine.calls]
    last_progress = max(i for i, count in enumerate(big_counts) if count > 0)
    before_completion = big_counts[:last_progress + 1]
    assert all(count > 0 for count in before_completion), big_counts  # never skipped before it is done
    assert sum(before_completion) == 20
    assert all(count >= 2 for count in before_completion[:-1]), big_counts  # at least half the batch, but the tail


def test_a_chunked_requests_failed_batch_calls_on_done_exactly_once() -> None:
    """Regression for fix round 1: the starvation fix's `take()` can give one started request room in both the
    reserved pass and the leftover pass of the very same batch (e.g. a solo chunked request with no competing
    fresh requests: pass 1 reserves half the batch for it, pass 2 (fresh) has nothing to give, pass 3 hands it
    the rest). Before this fix that created two separate parts for the same request in one group; a failed batch
    delivers its error once per part via `_finish_failed`, so that request got on_done called twice. Adapted from
    the reviewer's probe /private/tmp/claude-501/rr2/double.py (which showed exactly this: 2 deliveries for one
    submitted request)."""
    class FailsSecondBatch(Slow):
        def __init__(self) -> None:
            super().__init__(0.0)
            self.attempts = 0

        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            self.attempts += 1
            if self.attempts == 2:  # the second batch of this solo chunked request is the one with merged parts
                raise EngineError("boom")
            return super().predict(adapter_id, rows)

    engine = FailsSecondBatch()
    scheduler = make(engine, window_ms=5, max_rows=4, limit=400)
    got: list[Any] = []
    done = threading.Event()

    def on_done(result: Any) -> None:
        got.append(result)
        done.set()

    scheduler.submit("a.b.c", [ROW] * 20, None, on_done)
    assert done.wait(5)
    time.sleep(0.1)  # give a (buggy) second delivery, if any, time to arrive before we check
    scheduler.stop()
    assert len(got) == 1, got
    assert isinstance(got[0], EngineError) and "boom" in str(got[0])


def test_a_chunked_requests_mismatched_batch_calls_on_done_exactly_once() -> None:
    """Same merged-parts regression as above (see test_a_chunked_requests_failed_batch_calls_on_done_exactly_once),
    through the other failure path in `_run`: a batch whose returned probabilities don't match the row count."""
    class MismatchedSecondBatch(Slow):
        def __init__(self) -> None:
            super().__init__(0.0)
            self.attempts = 0

        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            self.attempts += 1
            if self.attempts == 2:
                return EngineResult([[0.5, 0.5]], {"path": "fake"})  # always one row, regardless of input
            return super().predict(adapter_id, rows)

    engine = MismatchedSecondBatch()
    scheduler = make(engine, window_ms=5, max_rows=4, limit=400)
    got: list[Any] = []
    done = threading.Event()

    def on_done(result: Any) -> None:
        got.append(result)
        done.set()

    scheduler.submit("a.b.c", [ROW] * 20, None, on_done)
    assert done.wait(5)
    time.sleep(0.1)
    scheduler.stop()
    assert len(got) == 1, got
    assert isinstance(got[0], EngineError) and "probabilities" in str(got[0])


def test_each_request_gets_its_own_rows_input_tokens_from_a_shared_or_chunked_batch() -> None:
    engine = Slow(0.0)
    scheduler = make(engine, window_ms=50, max_rows=3)
    short, wordy = {"state": "a", "question": ROW["question"]}, {"state": "a b c d e", "question": ROW["question"]}
    results = run(scheduler, [("a.b.c", [short, wordy], None), ("a.b.c", [wordy, short, wordy, short], None)])
    scheduler.stop()
    assert results[0].input_tokens == [fake_tokens(short), fake_tokens(wordy)]
    assert results[1].input_tokens == [fake_tokens(wordy), fake_tokens(short), fake_tokens(wordy), fake_tokens(short)]


def test_an_engine_without_input_tokens_gives_results_without_them() -> None:
    class Untokenised(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            return EngineResult(super().predict(adapter_id, rows).probabilities, {"model_ms": 0.0})

    scheduler = make(Untokenised(lambda adapter, row: [0.5, 0.5], {"a.b.c"}), window_ms=1, max_rows=2)
    [result] = run(scheduler, [("a.b.c", [ROW] * 3, None)])
    scheduler.stop()
    assert len(result.probabilities) == 3 and result.input_tokens is None


def test_input_tokens_for_the_wrong_number_of_rows_fail_the_group() -> None:
    class Miscounted(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            return EngineResult(super().predict(adapter_id, rows).probabilities, {}, [1])

    scheduler = make(Miscounted(lambda adapter, row: [0.5, 0.5], {"a.b.c"}))
    [result] = run(scheduler, [("a.b.c", [ROW, ROW], None)])
    scheduler.stop()
    assert isinstance(result, EngineError) and "input token counts" in str(result)


# ---- plan 2b-1 Task 7: one engine call per micro-batch


THREE = {"a.b.c", "a.b.d", "a.b.e"}


def test_three_clients_on_three_adapters_land_in_one_predict_many() -> None:
    engine = FakeEngine(lambda adapter, row: [0.3, 0.7], THREE)
    scheduler = make(engine, window_ms=50)
    clients = [threading.Thread(target=run, args=(scheduler, [(adapter, [ROW, ROW], None)]))
               for adapter in sorted(THREE)]
    for client in clients:
        client.start()
    for client in clients:
        client.join(10)
    scheduler.stop()
    assert len(engine.batches) == 1, engine.batches
    assert sorted(engine.batches[0]) == [("a.b.c", 2), ("a.b.d", 2), ("a.b.e", 2)]


def test_a_micro_batch_holds_at_most_max_batch_rows_across_every_adapter() -> None:
    """max_batch_rows caps the whole engine call, every adapter's rows together (the fast backend runs them in one
    set of passes, and the prefix pool holds max_batch_rows prefixes at once), not each adapter's group."""
    engine = FakeEngine(lambda adapter, row: [0.3, 0.7], THREE)
    scheduler = make(engine, window_ms=50, max_rows=4)
    results = run(scheduler, [("a.b.c", [ROW] * 3, None), ("a.b.d", [ROW] * 3, None), ("a.b.e", [ROW] * 3, None)])
    scheduler.stop()
    assert all(len(r.probabilities) == 3 for r in results)
    assert [sum(rows for _, rows in batch) for batch in engine.batches] == [4, 4, 1], engine.batches


class Bucketed(FakeEngine):
    """A fake of the fast backend's predict_many: every group's rows are run together, as passes per bucket (a row's
    bucket is its "n"), smallest bucket first, so a group's rows are answered out of order; each row's answer comes
    back in its own group's row order, with per-row timing."""

    def __init__(self) -> None:
        super().__init__(lambda adapter, row: [row["id"], 1.0], THREE)

    def predict_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult]:
        self.batches.append([(adapter, len(prepared.rows)) for adapter, prepared in groups])
        flat = [(g, i, row) for g, (_, prepared) in enumerate(groups) for i, row in enumerate(prepared.rows)]
        answers: dict[tuple[int, int], tuple[float, int, int]] = {}
        for bucket in sorted({row["n"] for _, _, row in flat}):
            members = [(g, i, row) for g, i, row in flat if row["n"] == bucket]
            for g, i, row in members:
                answers[(g, i)] = (row["id"], bucket, len(members))
        results = []
        for g, (_, prepared) in enumerate(groups):
            mine = [answers[(g, i)] for i in range(len(prepared.rows))]
            results.append(EngineResult([[value, 1.0] for value, _, _ in mine], {"path": "bucketed", "model_ms": 1.0},
                                        [7] * len(mine),
                                        {"bucket": [b for _, b, _ in mine], "pass_rows": [p for _, _, p in mine]}))
        return results


def bucketed(i: int, n: int) -> dict[str, Any]:
    return {**ROW, "id": float(i), "n": n}


def test_a_request_whose_rows_fall_in_two_buckets_gets_its_results_in_its_own_order() -> None:
    engine = Bucketed()
    scheduler = make(engine, window_ms=50)
    mine = [bucketed(1, 256), bucketed(2, 32), bucketed(3, 256), bucketed(4, 32)]
    other = [bucketed(10, 32), bucketed(11, 64)]
    results = run(scheduler, [("a.b.c", mine, None), ("a.b.d", other, None)])
    scheduler.stop()
    assert len(engine.batches) == 1
    assert [row[0] for row in results[0].probabilities] == [1.0, 2.0, 3.0, 4.0]
    assert results[0].timing["bucket"] == [256, 32, 256, 32]
    assert results[0].timing["pass_rows"] == [2, 3, 2, 3]  # 32: rows 2 and 4 with the other request's row 10
    assert [row[0] for row in results[1].probabilities] == [10.0, 11.0]
    assert results[1].timing["bucket"] == [32, 64] and results[1].timing["pass_rows"] == [3, 1]
    assert results[0].timing["batch_rows"] == 6 and results[0].timing["path"] == "bucketed"


def test_per_row_timing_of_a_chunked_request_is_joined_across_its_micro_batches() -> None:
    engine = Bucketed()
    scheduler = make(engine, window_ms=1, max_rows=2)
    rows = [bucketed(i, 32 if i % 2 else 128) for i in range(5)]
    [result] = run(scheduler, [("a.b.c", rows, None)])
    scheduler.stop()
    assert [row[0] for row in result.probabilities] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert result.timing["bucket"] == [128, 32, 128, 32, 128] and result.input_tokens == [7] * 5


def test_per_row_timing_of_the_wrong_length_fails_the_group() -> None:
    class Short(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            return EngineResult([[0.5, 0.5]] * len(rows), {}, None, {"bucket": [32]})

    scheduler = make(Short(lambda adapter, row: [0.5, 0.5], {"a.b.c"}))
    [result] = run(scheduler, [("a.b.c", [ROW, ROW], None)])
    scheduler.stop()
    assert isinstance(result, EngineError) and "bucket" in str(result)


def test_a_started_request_is_not_starved_by_fresh_requests_on_other_adapters() -> None:
    """The 2a-1 starvation scenario across adapters: one micro-batch holds max_batch_rows rows of every adapter
    together, so fresh 1-row requests on two busy adapters could fill each micro-batch before a large request on a
    third adapter got any room. A request already part-way through keeps half of each micro-batch (max_batch_rows //
    2 rows, at least 1) until it is done."""
    engine = Slow(0.02)
    engine._installed |= THREE
    scheduler = make(engine, window_ms=5, max_rows=4, limit=400)
    big_row = {**ROW, "marker": "big"}
    big_done = threading.Event()
    scheduler.submit("a.b.e", [big_row] * 20, None, lambda r: big_done.set())
    deadline = time.monotonic() + 2
    sent = 0
    while time.monotonic() < deadline and not big_done.is_set():
        for adapter in ("a.b.c", "a.b.d", "a.b.c", "a.b.d"):
            scheduler.submit(adapter, [ROW], None, lambda r: None)
            sent += 1
        time.sleep(0.01)
    finished = big_done.wait(0.5)
    scheduler.stop()
    assert finished, "the big request was starved and never finished"
    assert sent > 20, "small requests must really have arrived faster than the micro-batches drained"
    per_batch: list[int] = []
    calls = iter(engine.calls)
    for batch in engine.batches:
        per_batch.append(sum(sum(1 for row in rows if row is big_row) for _, rows in (next(calls) for _ in batch)))
    last_progress = max(i for i, count in enumerate(per_batch) if count > 0)
    before_completion = per_batch[:last_progress + 1]
    assert all(count > 0 for count in before_completion), per_batch
    assert sum(before_completion) == 20
    assert all(count >= 2 for count in before_completion[:-1]), per_batch
    assert all(sum(rows for _, rows in batch) <= 4 for batch in engine.batches), engine.batches


def test_a_bad_row_is_refused_at_preparation_and_the_others_share_one_call() -> None:
    engine = FakeEngine(lambda adapter, row: [0.3, 0.7], THREE)
    scheduler = make(engine, window_ms=50)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.d", [ROW, LONG], None), ("a.b.e", [ROW], None)])
    scheduler.stop()
    assert isinstance(results[1], InputTooLong)
    assert isinstance(results[0], EngineResult) and isinstance(results[2], EngineResult)
    assert engine.batches == [[("a.b.c", 1), ("a.b.e", 1)]]


def test_requests_prepared_for_different_routes_never_share_a_group() -> None:
    class Routed(FakeEngine):
        def prepare(self, adapter_id: str, rows: list[dict[str, Any]]) -> Prepared:
            self.check(adapter_id, rows)
            return Prepared(rows, [row.get("route") for row in rows], rows[0].get("route"))

        def predict_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult]:
            assert all(len({item for item in prepared.items or []}) == 1 for _, prepared in groups)
            return super().predict_many(groups)

    engine = Routed(lambda adapter, row: [0.3, 0.7], {"a.b.c"})
    scheduler = make(engine, window_ms=50)
    fast, standard = {**ROW, "route": "fast"}, {**ROW, "route": "standard"}
    results = run(scheduler, [("a.b.c", [fast], None), ("a.b.c", [standard, standard], None),
                              ("a.b.c", [fast], None)])
    scheduler.stop()
    assert all(isinstance(r, EngineResult) for r in results)
    assert engine.batches == [[("a.b.c", 2), ("a.b.c", 2)]]


def test_an_expired_request_never_runs_while_others_of_its_micro_batch_do() -> None:
    engine = Slow(0.3)
    engine._installed |= THREE
    scheduler = make(engine, window_ms=1)
    got: dict[str, Any] = {}
    both = threading.Event()

    def finisher(name: str) -> Any:
        def on_done(result: Any) -> None:
            got[name] = result
            if len(got) == 3:
                both.set()
        return on_done

    scheduler.submit("a.b.c", [ROW], None, finisher("first"))
    time.sleep(0.05)  # the first micro-batch (0.3 s) is running; the next two wait behind it
    scheduler.submit("a.b.d", [ROW], time.monotonic() + 0.05, finisher("expires"))
    scheduler.submit("a.b.e", [ROW], None, finisher("live"))
    assert both.wait(5)
    scheduler.stop()
    assert isinstance(got["expires"], DeadlineExceeded) and isinstance(got["live"], EngineResult)
    assert engine.batches == [[("a.b.c", 1)], [("a.b.e", 1)]]


def test_a_failed_engine_call_fails_every_request_of_the_micro_batch_once() -> None:
    class Broken(FakeEngine):
        def predict_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult]:
            raise RuntimeError("CUDA error: illegal memory access")

    scheduler = make(Broken(lambda adapter, row: [0.5, 0.5], THREE), window_ms=50)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.d", [ROW], None), ("a.b.d", [ROW], None)])
    scheduler.stop()
    assert all(isinstance(r, EngineError) and "illegal memory access" in str(r) for r in results)


def test_an_engine_answering_the_wrong_number_of_groups_fails_the_micro_batch() -> None:
    class Dropped(FakeEngine):
        def predict_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult]:
            return super().predict_many(groups)[:-1]

    scheduler = make(Dropped(lambda adapter, row: [0.5, 0.5], THREE), window_ms=50)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.d", [ROW], None)])
    scheduler.stop()
    assert all(isinstance(r, EngineError) and "1 results for 2 groups" in str(r) for r in results)


def test_a_groups_typed_error_fails_that_group_alone() -> None:
    from jeff.errors import NotInstalled

    class Removed(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            if adapter_id == "a.b.d":
                raise NotInstalled(adapter_id, "removed since it arrived")
            return super().predict(adapter_id, rows)

    scheduler = make(Removed(lambda adapter, row: [0.5, 0.5], THREE), window_ms=50)
    results = run(scheduler, [("a.b.c", [ROW], None), ("a.b.d", [ROW], None), ("a.b.e", [ROW], None)])
    scheduler.stop()
    assert isinstance(results[1], NotInstalled)
    assert isinstance(results[0], EngineResult) and isinstance(results[2], EngineResult)
