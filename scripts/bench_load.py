"""Saturation of a running Jeff server: requests per second and end-to-end latency at increasing concurrency.

Each request is POST /v1/decide with one call, a test row of one of the given adapters rebuilt as the call a user
would make (jeff.evaluate.template_rows: the manifest's instructions and label texts). The rows are sampled with a
fixed seed and the adapters are cycled, so every level sends the same mix.

Closed loop: at concurrency C, C clients each send one request (on a new TCP connection with --connection fresh,
on the client's own kept-alive connection with --connection keepalive), wait for its answer, and send the next, for
--seconds after --warm-seconds of unrecorded warm-up. The clients are spread over --processes processes (each an
asyncio loop with its own keep-alive connections, TCP_NODELAY), so the load generator itself is not the limit at high concurrency. Latency is the
client's wall clock from sending the request to reading the whole answer. Any answer other than 200 stops the run
with the status and body.

The levels run in order and stop after the first level whose p99 is over --stop-p99-ms, or whose requests per second
rose by less than --min-gain (a fraction) over the best level before it.

Usage (on the server's machine; the key is read from the environment variable named by --key-env):
  JEFF_API_KEY=... .venv/bin/python scripts/bench_load.py --url http://127.0.0.1:8795 \\
      --adapters-dir ~/adapters --data-dir ~/data --adapter triage --adapter spam ... --out load.json
--data-dir holds <adapter folder name>/test.jsonl for each adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import random
import socket
import statistics
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LEVELS = (1, 2, 4, 8, 16, 32, 64, 128, 256)


def bodies(adapters_dir: Path, data_dir: Path, names: list[str], rows_each: int) -> list[bytes]:
    """The request bodies: rows_each template-mode test rows of each adapter, interleaved adapter by adapter."""
    from jeff.evaluate import template_rows
    from jeff.manifest import load_manifest

    per_adapter: list[list[bytes]] = []
    for name in names:
        manifest = load_manifest(adapters_dir / name / "manifest.yaml")
        lines = (data_dir / name / "test.jsonl").read_text(encoding="utf-8").split("\n")
        raws = [json.loads(line) for line in lines if line.strip()]
        raws = random.Random(0).sample(raws, min(len(raws), rows_each * 3))
        rows, skipped = template_rows(manifest, raws)
        if len(rows) < rows_each:
            raise RuntimeError(f"{name}: only {len(rows)} of {len(raws)} sampled test rows rebuild as calls "
                               f"(skipped {skipped}); need {rows_each}")
        out = []
        for row in rows[:rows_each]:
            call: dict[str, Any] = {"function": row.call.function, "input": row.call.input}
            if row.call.context is not None:
                call["context"] = dict(row.call.context)
            if row.call.labels is not None:
                labels = row.call.labels
                call["labels"] = dict(labels) if hasattr(labels, "keys") else list(labels)
            out.append(json.dumps({"calls": [call]}).encode())
        per_adapter.append(out)
    return [body for group in zip(*per_adapter, strict=True) for body in group]


class Connection:
    """One keep-alive HTTP/1.1 connection with TCP_NODELAY, each request written in one piece. (With httpx every
    request took about 55 ms on the Spark's loopback where curl took 12 ms, most likely Nagle's algorithm meeting
    delayed ACKs; this client avoids both.)"""

    def __init__(self, host: str, port: int, key: str) -> None:
        self.host, self.port = host, port
        self.head = (f"POST /v1/decide HTTP/1.1\r\nHost: {host}:{port}\r\nAuthorization: Bearer {key}\r\n"
                     "Content-Type: application/json\r\nConnection: keep-alive\r\n")
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        sock = self.writer.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    async def post(self, body: bytes) -> tuple[int, bytes]:
        assert self.reader is not None and self.writer is not None
        self.writer.write(f"{self.head}Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await self.writer.drain()
        status_line = await self.reader.readline()
        if not status_line:
            raise RuntimeError("the server closed the connection")
        status = int(status_line.split()[1])
        length, chunked, closing = None, False, False
        while True:
            line = await self.reader.readline()
            if line in (b"\r\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            name, value = name.strip().lower(), value.strip()
            if name == "content-length":
                length = int(value)
            elif name == "transfer-encoding" and "chunked" in value.lower():
                chunked = True
            elif name == "connection" and value.lower() == "close":
                closing = True
        if chunked:
            parts = []
            while True:
                size = int((await self.reader.readline()).strip(), 16)
                if size == 0:
                    await self.reader.readline()
                    break
                parts.append(await self.reader.readexactly(size))
                await self.reader.readline()
            payload = b"".join(parts)
        elif length is not None:
            payload = await self.reader.readexactly(length)
        else:
            raise RuntimeError(f"answer {status} has neither Content-Length nor chunked encoding")
        if closing:
            self.writer.close()
            await self.open()
        return status, payload


async def _clients(url: str, key: str, payloads: list[bytes], clients: int, offset: int, warm_until: float,
                   stop_at: float, fresh: bool) -> list[float]:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
        raise RuntimeError(f"--url must be http://HOST:PORT, got {url!r}")
    latencies: list[float] = []

    async def client(index: int) -> None:
        connection = Connection(parsed.hostname, parsed.port, key)
        if not fresh:
            await connection.open()
        i = offset + index * 7919
        while True:
            started = time.perf_counter()
            if started >= stop_at:
                if not fresh:
                    assert connection.writer is not None
                    connection.writer.close()
                return
            if fresh:
                await connection.open()
            status, payload = await connection.post(payloads[i % len(payloads)])
            ended = time.perf_counter()
            if fresh:
                assert connection.writer is not None
                connection.writer.close()
            if status != 200:
                raise RuntimeError(f"POST /v1/decide answered {status}: {payload[:500]!r}")
            if started >= warm_until:
                latencies.append((ended - started) * 1000)
            i += 1

    await asyncio.gather(*(client(k) for k in range(clients)))
    return latencies


def _process(args: tuple[str, str, list[bytes], int, int, float, float, bool]) -> list[float]:
    return asyncio.run(_clients(*args))


def level(pool: Any, url: str, key: str, payloads: list[bytes], concurrency: int, processes: int, warm: float,
          seconds: float, fresh: bool) -> dict[str, Any]:
    used = min(processes, concurrency)
    shares = [concurrency // used + (1 if k < concurrency % used else 0) for k in range(used)]
    start = time.perf_counter() + 0.5  # perf_counter is system-wide on Linux (CLOCK_MONOTONIC), so processes agree
    warm_until, stop_at = start + warm, start + warm + seconds
    jobs = [(url, key, payloads, share, k * 104729, warm_until, stop_at, fresh) for k, share in enumerate(shares)]
    latencies = [ms for part in pool.map(_process, jobs) for ms in part]
    if len(latencies) < 100:
        raise RuntimeError(f"concurrency {concurrency}: only {len(latencies)} timed requests; raise --seconds")
    cuts = statistics.quantiles(latencies, n=100, method="inclusive")
    return {"concurrency": concurrency, "requests": len(latencies), "seconds": seconds,
            "requests_per_s": round(len(latencies) / seconds, 1), "p50_ms": round(cuts[49], 2),
            "p90_ms": round(cuts[89], 2), "p99_ms": round(cuts[98], 2), "max_ms": round(max(latencies), 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--key-env", default="JEFF_API_KEY", help="the environment variable holding the API key")
    parser.add_argument("--adapters-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--adapter", action="append", required=True, help="an adapter folder name (repeatable)")
    parser.add_argument("--rows-each", type=int, default=200)
    parser.add_argument("--levels", default=",".join(map(str, LEVELS)))
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--warm-seconds", type=float, default=5)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--stop-p99-ms", type=float, default=100)
    parser.add_argument("--min-gain", type=float, default=0.03)
    parser.add_argument("--connection", choices=("fresh", "keepalive"), required=True,
                        help="fresh: a new TCP connection for every request (timed with it); keepalive: one per client")
    parser.add_argument("--label", required=True, help="what was measured (recorded in the output)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    key = os.environ.get(args.key_env)
    if not key:
        raise SystemExit(f"{args.key_env} is not set; it must hold the server's API key")
    payloads = bodies(args.adapters_dir.expanduser(), args.data_dir.expanduser(), args.adapter, args.rows_each)
    print(f"{len(payloads)} request bodies from {len(args.adapter)} adapters", flush=True)
    results: list[dict[str, Any]] = []
    stopped = "all levels run"
    with multiprocessing.get_context("spawn").Pool(args.processes) as pool:
        for concurrency in (int(x) for x in args.levels.split(",")):
            res = level(pool, args.url, key, payloads, concurrency, args.processes, args.warm_seconds, args.seconds,
                        args.connection == "fresh")
            results.append(res)
            print(json.dumps(res), flush=True)
            best_before = max((r["requests_per_s"] for r in results[:-1]), default=None)
            if res["p99_ms"] > args.stop_p99_ms:
                stopped = f"p99 over {args.stop_p99_ms} ms at concurrency {concurrency}"
                break
            if best_before is not None and res["requests_per_s"] < best_before * (1 + args.min_gain):
                stopped = f"requests per second rose less than {args.min_gain:.0%} at concurrency {concurrency}"
                break
    record = {"script": "scripts/bench_load.py", "date": time.strftime("%Y-%m-%d %H:%M"), "label": args.label,
              "url": args.url, "adapters": args.adapter, "request_bodies": len(payloads),
              "calls_per_request": 1, "connection": args.connection, "processes": args.processes, "warm_seconds": args.warm_seconds,
              "stopped": stopped, "levels": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps({"stopped": stopped}), flush=True)


if __name__ == "__main__":
    main()
