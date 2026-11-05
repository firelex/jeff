"""The jeff-server command. Subcommands:

    jeff-server token keygen --out <dir>          an Ed25519 key pair (private.pem, public.pem) for development tokens
    jeff-server token issue --key <private PEM> --sub <who> [--licence <adapter id> ...] --ttl-s <seconds <= 3600>
    jeff-server users <file> add|remove <login> | list | sign-out-all
    jeff-server serve --host <host> --port <port> --workers <n>   run the HTTP API with uvicorn
    jeff-server factory worker --engine-socket <path> [--engine-socket <path> ...]   run the factory's build steps

`serve`'s settings (which engine sockets to connect to, the model name, credentials, ...) all come from the process
environment, exactly as `ServerSettings.from_env` (settings.py) reads them -- not from command-line flags, so the
same environment that would configure one worker process configures every one of `--workers` of them identically.
`--host`, `--port` and `--workers` all have no default: binding to every interface (`--host 0.0.0.0`) or running
more than one worker process (each one opens its own connections to the engine process(es) named by
JEFF_ENGINE_SOCKETS) are both deliberate choices, never something to fall into silently.

With more than one worker, uvicorn needs the app given as an import string so it can import it fresh in each worker
process it forks, rather than forking a single app object (and the engine connections already open inside it) from
the parent; `app_factory` below (`jeff_server.cli:app_factory`) is that import string's target, and is used the same
way even with a single worker, so one worker process behaves exactly like any other -- its app, settings and engine
connections are always built fresh in that process, never inherited.

Before any of that, `_run_serve` checks the settings and every engine socket (`_check_ready_to_serve`) and exits
loudly, naming the problem, if either is broken. Any other startup failure stops each worker with exit code 3 (see
`app_factory`)."""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jeff_server import auth, users

if TYPE_CHECKING:
    from fastapi import FastAPI


def app_factory() -> "FastAPI":
    """uvicorn's import-string factory target: called with no arguments, once per worker process, so each worker
    reads the environment itself and builds its own ServerSettings, FastAPI app and engine connections, rather than
    sharing ones built in another process.

    Any failure here exits the worker process with uvicorn's own STARTUP_FAILURE code (3), after printing the
    error, rather than letting the exception escape uncaught. This matters specifically under `--workers > 1`:
    uvicorn's `Config.load()` calls this factory and only recognises a *TypeError* as "the factory is broken"
    (uvicorn/config.py's `except TypeError: ... sys.exit(STARTUP_FAILURE)`); any other exception -- for example
    `ServerSettings.from_env` raising `InvalidRequest` for a missing setting -- propagates out of the worker
    subprocess uncaught, which Python's `multiprocessing` then reports as plain exit code 1. uvicorn's own
    multi-worker supervisor (`uvicorn/supervisors/multiprocess.py`, `Multiprocess.keep_subprocess_alive`) only
    stops retrying a worker whose exit code is exactly `STARTUP_FAILURE`; exit code 1 looks to it like a worker
    that merely died, so it replaces it -- which fails exactly the same way, forever (confirmed by reproducing it:
    a missing JEFF_ENGINE_SOCKETS with `--workers 2` logged 1000+ lines of the same traceback in six seconds and
    never stopped on its own). `_run_serve` checks the settings and every engine socket before any worker is
    spawned; this exit code covers every other startup failure, and those two checks turning false afterwards (an
    engine process dying before this worker starts)."""
    from uvicorn.config import STARTUP_FAILURE

    from jeff_server.app import create_app
    from jeff_server.settings import ServerSettings

    try:
        return create_app(ServerSettings.from_env(os.environ))
    except Exception as error:
        print(f"jeff-server serve: {error}", file=sys.stderr)
        sys.exit(STARTUP_FAILURE)


def _serve_command(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    serve = commands.add_parser("serve", help="Run the HTTP API with uvicorn; settings come from the environment "
                                               "(see ServerSettings.from_env), not from flags")
    serve.add_argument("--host", required=True,
                       help="interface to bind, e.g. 127.0.0.1 (private) or 0.0.0.0 (every interface); no default, "
                            "so binding to every interface is a deliberate choice, never a silent one")
    serve.add_argument("--port", type=int, required=True, help="port to bind")
    serve.add_argument("--workers", type=int, required=True,
                       help="uvicorn worker processes; each builds its own app, settings and engine connections, "
                            "and keeps its own rate limits, sign-in lockouts and request counters: JEFF_RATE_LIMIT "
                            "applies per worker, so a caller can get up to N times the rate with N workers; set it "
                            "accordingly")


def _check_ready_to_serve(env: Mapping[str, str], workers: int) -> None:
    """Checks the settings and every engine socket before `uvicorn.run` is called, so a broken setting or an engine
    that does not answer stops `serve` here, naming the problem, before any port is bound or any worker spawned.
    Builds the settings and opens (then closes) a connection to every socket in JEFF_ENGINE_SOCKETS: SocketEngine's
    constructor does the connect-and-hello handshake and a first `installed()` snapshot.

    It checks only those two things. Every other startup failure (the catalogue, the graphs, the model name against
    the catalogue, credentials files, the access log) is found when each worker builds its app, and stops the server
    through `app_factory`'s exit code 3 (uvicorn's STARTUP_FAILURE) from each worker. In accounts mode it also
    refuses any --workers but 1."""
    from jeff.errors import JeffError
    from jeff.socket_engine import SocketEngine

    from jeff_server.settings import ServerSettings

    try:
        settings = ServerSettings.from_env(env)
        if settings.accounts is not None and workers != 1:
            raise SystemExit("jeff-server serve: accounts mode (JEFF_ACCOUNTS_URL) keeps each account's usage and "
                             "allowance in one process; run it with --workers 1")
        for path in settings.engine_sockets:
            SocketEngine(path, timeout_s=settings.engine_timeout_s).close()
    except JeffError as error:
        raise SystemExit(f"jeff-server serve: {error}") from error


def websocket_options() -> dict[str, Any]:
    """How uvicorn runs /v1/channel's WebSockets, which bounds the memory one connection can hold:

    - ws_max_size: the largest WebSocket message accepted (uvicorn's default is 16 MiB): one channel frame, a 4-byte
      header and a body of up to jeff.protocol.MAX_BODY (50 MiB). A larger message is refused while it
      arrives, with close code 1009.
    - ws="websockets-sansio", named rather than left to "auto" (which picks it today, with websockets installed), so
      the bound below does not change with what is installed: it stops reading from the socket as soon as one whole
      message is waiting for the app, and reads on once the app has taken it. A connection therefore holds at most
      the frame the channel is reading, one waiting frame and one frame arriving, each up to ws_max_size.
    - ws_max_queue=1: the same bound for uvicorn's other websockets implementation ("websockets"), which honours it
      (the sansio one has no queue size to set; it pauses after each message, as above).
    - ws_per_message_deflate=False: no compression. With it, a 50 MiB message of repeated bytes travels in about
      50 KB, so one read from the socket would hold several whole messages, each inflating to 50 MiB, before reading
      pauses; without it, every byte held arrived over the network. Channel frames are msgpack, which gains little
      from compression anyway.

    uvicorn sets these once for the whole server and hands them to each connection when it opens; an ASGI app
    cannot change them for one connection afterwards, so a smaller cap before auth is the channel's own check of each
    frame's 4-byte length (jeff.protocol.PREAUTH_MAX_BODY), which refuses a large frame before unpacking it."""
    from jeff.protocol import HEADER, MAX_BODY

    return {"ws": "websockets-sansio", "ws_max_size": HEADER.size + MAX_BODY, "ws_max_queue": 1,
            "ws_per_message_deflate": False}


def _run_serve(args: argparse.Namespace) -> None:
    import uvicorn

    _check_ready_to_serve(os.environ, args.workers)
    uvicorn.run("jeff_server.cli:app_factory", factory=True, host=args.host, port=args.port, workers=args.workers,
                **websocket_options())


def _factory_command(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    factory = commands.add_parser("factory", help="the adapter factory")
    actions = factory.add_subparsers(dest="factory_command", required=True)
    worker = actions.add_parser(
        "worker", help="run the build steps the API queues, one at a time; settings come from the environment "
                       "(JEFF_FACTORY=owner and the other factory variables, JEFF_ADAPTER_DIRS, JEFF_CATALOGUE, "
                       "JEFF_MODEL_NAME), as for serve")
    worker.add_argument("--engine-socket", type=Path, action="append", required=True, metavar="PATH",
                        help="an engine process's socket; repeat for each engine. The worker trains on the base they "
                             "serve and reloads them after publishing")


def _run_factory(args: argparse.Namespace) -> None:
    """The worker's settings: the factory variables (jeff_server.factory.config), JEFF_ADAPTER_DIRS, whose first
    folder is where the factory publishes new adapters, JEFF_CATALOGUE and JEFF_MODEL_NAME, as the API reads them."""
    from jeff.errors import JeffError

    from jeff_server.factory.config import FactoryConfig
    from jeff_server.factory.worker import run_worker
    from jeff_server.settings import adapter_dirs_from_env, catalogue_from_env, model_name_from_env

    try:
        config = FactoryConfig.from_env(os.environ)
        adapter_dirs = adapter_dirs_from_env(os.environ)
        if not adapter_dirs:
            raise SystemExit("jeff-server factory worker: set JEFF_ADAPTER_DIRS; the factory publishes new adapters "
                             "into its first folder")
        run_worker(config, args.engine_socket, adapter_dirs, catalogue_from_env(os.environ),
                   model_name_from_env(os.environ))
    except JeffError as error:
        raise SystemExit(f"jeff-server factory worker: {error}") from error
    except KeyboardInterrupt:
        print("jeff-server factory worker: stopped", file=sys.stderr)


def _token_command(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    token = commands.add_parser("token", help="development tokens, signed like the hub's, until the hub exists")
    actions = token.add_subparsers(dest="token_command", required=True)
    keygen = actions.add_parser("keygen", help="write an Ed25519 key pair; point JEFF_HUB_PUBLIC_KEY at public.pem")
    keygen.add_argument("--out", type=Path, required=True, help="the folder for private.pem and public.pem")
    issue = actions.add_parser("issue", help="print a signed token")
    issue.add_argument("--key", type=Path, required=True, help="the Ed25519 private key (PEM) to sign with")
    issue.add_argument("--sub", required=True, help="who the token is for; rate limits are per sub")
    issue.add_argument("--licence", action="append", default=[], metavar="ADAPTER_ID",
                       help="a proprietary adapter the token may call; repeat for several; none: open adapters only")
    issue.add_argument("--ttl-s", type=int, required=True, help="seconds the token lasts, at most 3600")


def _run_token(args: argparse.Namespace) -> None:
    if args.token_command == "keygen":
        auth.keygen(args.out.expanduser())
        print(f"Wrote {args.out / 'private.pem'} (keep it secret) and {args.out / 'public.pem'}")
        return
    print(auth.issue(args.key.expanduser(), args.sub, list(args.licence), args.ttl_s))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jeff-server", description="Jeff's HTTP API server")
    commands = parser.add_subparsers(dest="command", required=True)
    _token_command(commands)
    users.add_arguments(commands.add_parser("users", help="manage console sign-in (the JEFF_UI_USERS file)"))
    _serve_command(commands)
    _factory_command(commands)
    args = parser.parse_args(argv)
    if args.command == "token":
        _run_token(args)
    elif args.command == "users":
        users.run(args)
    elif args.command == "serve":
        _run_serve(args)
    elif args.command == "factory":
        _run_factory(args)
    else:
        raise SystemExit(f"Unknown command {args.command!r}")


if __name__ == "__main__":
    main()
