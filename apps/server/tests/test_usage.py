"""The usage book: totals per account and billing period, appended to a JSON Lines file and compacted on start, so a
restart keeps every count; a broken file stops the server naming the line."""

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jeff import contract
from jeff.errors import InvalidRequest
from jeff_server.usage import UsageBook, utc_text

NOV, DEC, JAN = "2026-11-03T10:00:00Z", "2026-12-03T10:00:00Z", "2027-01-03T10:00:00Z"


def test_counts_add_up_per_account_and_period(tmp_path: Path) -> None:
    book = UsageBook(tmp_path / "usage.jsonl")
    assert book.total("a1", NOV) == 0
    assert book.add("a1", NOV, 100) == 100
    assert book.add("a1", NOV, 25) == 125
    book.add("a2", NOV, 7)
    book.add("a1", DEC, 1)
    assert (book.total("a1", NOV), book.total("a2", NOV), book.total("a1", DEC)) == (125, 7, 1)


def test_a_restart_keeps_every_total(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    book = UsageBook(path)
    book.add("a1", NOV, 100)
    book.add("a1", NOV, 50)
    book.close()
    again = UsageBook(path)
    assert again.total("a1", NOV) == 150


def test_start_compacts_to_one_line_per_account_and_period_keeping_the_two_latest_periods(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    book = UsageBook(path)
    for period in (NOV, DEC, JAN):
        book.add("a1", period, 10)
        book.add("a1", period, 10)
    book.close()
    UsageBook(path).close()
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [{"account": "a1", "period_start": DEC, "input_tokens": 20},
                     {"account": "a1", "period_start": JAN, "input_tokens": 20}]


def test_a_new_period_drops_the_oldest_from_memory_too(tmp_path: Path) -> None:
    book = UsageBook(tmp_path / "usage.jsonl")
    for period in (NOV, DEC, JAN):
        book.add("a1", period, 10)
    assert [entry["period_start"] for entry in book.report()["accounts"]] == [DEC, JAN]


def test_a_torn_last_line_stops_the_start_naming_the_file_and_line(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({"account": "a1", "period_start": NOV, "input_tokens": 5}) + "\n"
                    + '{"account": "a1", "period_st')
    with pytest.raises(InvalidRequest, match=r"usage.jsonl line 2 is incomplete") as raised:
        UsageBook(path)
    assert "remove" in str(raised.value)
    assert path.read_text().endswith('"period_st')  # nothing was rewritten


@pytest.mark.parametrize("line", [
    "not json",
    json.dumps({"account": "a1", "period_start": NOV}),
    json.dumps({"account": "a1", "period_start": "3 Nov", "input_tokens": 5}),
    json.dumps({"account": "a1", "period_start": NOV, "input_tokens": -5}),
    json.dumps({"account": "a1", "period_start": NOV, "input_tokens": True}),
    json.dumps([1, 2]),
    "",
])
def test_a_broken_line_stops_the_start_naming_it(tmp_path: Path, line: str) -> None:
    path = tmp_path / "usage.jsonl"
    path.write_text(line + "\n")
    with pytest.raises(InvalidRequest, match="usage.jsonl line 1"):
        UsageBook(path)


def test_the_report_is_a_valid_usage_document_with_last_used_times(tmp_path: Path) -> None:
    book = UsageBook(tmp_path / "usage.jsonl")
    book.add("a1", NOV, 12)
    book.add("playground", "2026-11-01T00:00:00Z", 3)
    book.touch("f" * 64, "2026-11-03T11:00:00Z")
    book.touch("f" * 64, "2026-11-03T11:00:05Z")
    report = book.report()
    assert contract.is_valid("accounts_usage", report), contract.first_problem("accounts_usage", report)
    assert report["keys"] == [{"hash": "f" * 64, "last_used": "2026-11-03T11:00:05Z"}]
    assert {"account": "a1", "period_start": NOV, "input_tokens": 12} in report["accounts"]


def test_adds_from_many_threads_all_count(tmp_path: Path) -> None:
    book = UsageBook(tmp_path / "usage.jsonl")
    threads = [threading.Thread(target=lambda: [book.add("a1", NOV, 1) for _ in range(200)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert book.total("a1", NOV) == 1600
    book.close()
    assert UsageBook(tmp_path / "usage.jsonl").total("a1", NOV) == 1600


def test_a_negative_count_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="negative"):
        UsageBook(tmp_path / "usage.jsonl").add("a1", NOV, -1)


def test_utc_text_is_seconds_with_a_z() -> None:
    assert utc_text(datetime(2026, 11, 3, 10, 0, 7, 999_000, tzinfo=UTC)) == "2026-11-03T10:00:07Z"


# --- one process per usage file; a failed write leaves memory as the file is ----------------------------------------

def test_a_second_process_on_the_same_usage_file_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    book = UsageBook(path)
    book.add("a1", NOV, 5)
    second = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; "
                             "from jeff_server.usage import UsageBook; UsageBook(Path(sys.argv[1]))", str(path)],
                            capture_output=True, text=True, timeout=60)
    assert second.returncode != 0
    assert f"JEFF_USAGE_FILE {path} is in use by another jeff-server process" in second.stderr
    with pytest.raises(InvalidRequest, match="is in use by another jeff-server process"):
        UsageBook(path)  # another open in this process is refused the same way
    book.add("a1", NOV, 5)
    book.close()
    again = UsageBook(path)  # closed: the lock is given back
    assert again.total("a1", NOV) == 10
    again.close()


def test_a_book_that_fails_to_open_gives_its_lock_back(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    path.write_text("not json\n")
    with pytest.raises(InvalidRequest, match="is not JSON"):
        UsageBook(path)
    path.write_text("")
    UsageBook(path).close()


def test_a_write_that_fails_leaves_the_total_as_the_file_holds_it(tmp_path: Path) -> None:
    class FullDisk:
        def write(self, text: str) -> int:
            raise OSError(28, "No space left on device")

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    book = UsageBook(tmp_path / "usage.jsonl")
    book.add("a1", NOV, 5)
    real, book._file = book._file, FullDisk()  # type: ignore[assignment]
    with pytest.raises(OSError, match="No space left"):
        book.add("a1", NOV, 7)
    with pytest.raises(OSError, match="No space left"):
        book.add("a1", DEC, 7)  # a new period is not kept either
    assert (book.total("a1", NOV), book.total("a1", DEC)) == (5, 0)
    book._file = real
    book.close()
