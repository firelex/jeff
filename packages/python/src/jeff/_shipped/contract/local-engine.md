# Starting the engine on demand (local mode in the Node, Rust, C# and Java SDKs)

The models run only in the Jeff engine, the Python package `jeff-runtime`, run as `jeff runtime serve`. The Python SDK
loads that engine into its own process and is not covered here. The Node, Rust, C# and Java SDKs talk to an engine
process over its Unix socket (`contract/socket.md`). This document says how those four SDKs find a running engine, or
start one themselves, so that a developer installs uv once, installs the SDK package, and calls local mode with a list
of adapters, without knowing the engine is written in Python. Every rule here is the same in the four SDKs;
`contract/conformance/local_engine.json` holds the cases each SDK tests against.

## The two forms of local mode

| | Start or find the engine (this document) | Use exactly this engine (unchanged) |
| - | - | - |
| Node | `Jeff.local(["guard"], options?)` | `Jeff.local(socketPath, { timeoutMs })` |
| Rust | `Jeff::local_adapters(["guard"], StartOptions::default())` | `Jeff::local(socket_path, LocalOptions::new(ms))` |
| C# | `JeffClient.LocalAsync(new[] { "guard" }, options?)` | `JeffClient.LocalAsync(socketPath, new LocalOptions { Timeout })` |
| Java | `Jeff.local(List.of("guard"))`, `Jeff.local(List.of("guard"), options)` | `Jeff.local(socketPath, LocalOptions.timeoutMs(ms))` |

- **With a list of adapters** (adapter IDs or aliases), the SDK uses the engine running for this user on this machine,
  or starts one, as described below.
- **With a socket path** (or a server address, through remote mode), the SDK uses exactly that, as before: it never
  starts an engine, never looks in the engine folder and never needs uv. This is the way to use an engine you start
  and manage yourself, for example one with proprietary adapters (`--path`), an INT4 base (`--base`) or your own
  batching settings.

The start options (each SDK's own names, all optional):

| Option | Default | What it bounds |
| - | - | - |
| reply timeout (`timeoutMs`, `timeout_ms`, `Timeout`, `timeoutMs`) | 60,000 ms | The hello and each reply from the engine, as the socket form's required timeout does. |
| start timeout (`startTimeoutMs`, `start_timeout_ms`, `StartTimeout`, `startTimeoutMs`) | 1,800,000 ms (30 minutes) | Everything before the client is ready: waiting for another program that is starting the engine, and starting it (the first start downloads about 2 GB). |

Both are whole numbers of milliseconds of at least 1 (C# takes a `TimeSpan` whose total milliseconds is a whole number
of at least 1); anything else is refused with `InvalidRequest` before anything else happens. The language's own upper
limits for timers still apply (Node refuses more than 2,147,483,647 ms, as for `timeoutMs` today).

The adapters are bound with the catalogue shipped with the SDK; this form takes no catalogue, because the engine it
starts reads the catalogue shipped with `jeff-runtime` of the same version.

## The steps, in order

1. **Check the options** (above).
2. **Resolve the adapters** in the shipped catalogue: each reference is an adapter ID or alias; an unknown one is the
   catalogue's own error (`UnknownFunction`). The list is reduced to adapter IDs, each kept once, in the order first
   named. An empty list is a `ManifestError`: `local mode needs at least one adapter (none were given)`. Adapters
   trained on different bases are a `ManifestError`: `The adapters are trained on different bases (<id>@<version>,
   <id>@<version>); load one base at a time`, the bases sorted. An adapter whose manifest has no `open` weights is a
   `ProprietaryWeights` error: `<adapter id> is proprietary: its weights download only through JeffHub with a
   licence, so local mode cannot start an engine for it. Start an engine yourself (jeff runtime serve --path <adapter
   id>=<folder>) and give its socket instead of the adapters`.
3. **Choose the engine's build for this machine** ("Platforms" below), or refuse with `BackendUnavailable`.
4. **Find the engine folder** ("The engine folder" below).
5. **Look for a running engine**: connect to `engine.sock` in the folder (hello, `installed`, `health`, as the socket
   form connects), bounded by the reply timeout. If it connects, go to step 9. If nothing listens there (no socket
   file, or a refused connection), go on. Any other failure (for example `ProtocolMismatch`, or an engine that does
   not answer its hello in time) is raised as it is: an engine is there and it is not one this SDK can use.
6. **Find uvx** ("Finding uv" below), or refuse with `RuntimeMissing`; Java and C# on Linux then find `setsid`
   ("Starting" below), or refuse with `RuntimeMissing`.
7. **Take the start lock** ("The start lock" below). Once it is held, look for a running engine again, as in step 5
   (another program may have started one while this one waited); if one answers, release the lock and go to step 9.
8. **Start the engine** ("Starting" below), then release the lock, whether the start succeeded or not.
9. **Make sure the engine serves every adapter asked for** ("Sharing" below).
10. **Build the client** over that connection exactly as the socket form does (the base format, the routers), with
    the shipped catalogue. If this fails, the connection is closed and the error raised.

## Platforms

The engine listens on a Unix socket and runs on two kinds of machine, each with its own extra of `jeff-runtime`:

| Machine | Extra |
| - | - |
| macOS on Apple silicon (`arm64`) | `mac` (MLX) |
| Linux with an NVIDIA GPU, on any processor | `cuda` |

A Linux machine has an NVIDIA GPU when the file `/proc/driver/nvidia/version` exists (the NVIDIA driver is loaded), or
when an executable `nvidia-smi` is found on `PATH` by the same search as uvx (below; on WSL 2 it is
`/usr/lib/wsl/lib/nvidia-smi`, which WSL puts on `PATH`). Each SDK names the operating system `macos`, `linux`,
`windows` or the name its language gives any other, and the processor `arm64` (Apple's and Arm's 64-bit processors,
which Rust and Java call `aarch64`), `x86_64` (`x64` and `amd64` too) or the name its language gives any other.

Every other machine is refused with `BackendUnavailable`, before anything is looked for or started. The message is
`The Jeff engine runs on macOS with Apple silicon, or on Linux with an NVIDIA GPU; ` followed by:

- macOS on `x86_64`: `this is an Intel Mac (or a program running under Rosetta), so local mode cannot start it here. Use a Jeff server instead (remote mode)`;
- macOS on any other processor `<arch>`: `this Mac's processor is <arch>, so local mode cannot start it here. Use a Jeff server instead (remote mode)`;
- Linux without an NVIDIA GPU: `this Linux machine has no NVIDIA GPU (no /proc/driver/nvidia/version, and no nvidia-smi on PATH), so local mode cannot start it here. Use a Jeff server instead (remote mode)`;
- Windows: `the engine cannot run on Windows itself, so local mode cannot start it here. Run your program in WSL 2 on a machine with an NVIDIA GPU, or use a Jeff server instead (remote mode)`;
- any other system `<os>`: `this machine runs <os>, so local mode cannot start it here. Use a Jeff server instead (remote mode)`.

There is no CPU build to start: `jeff-runtime` can run on a CPU (its `torch` backend) when you start it yourself, but
local mode does not start one, because it is far too slow to be what a developer expects from a call.

## The engine folder

One folder per user and machine holds everything, so every program of this user finds the same engine:

- macOS: `$HOME/Library/Caches/jeff/engine`;
- Linux: `$XDG_CACHE_HOME/jeff/engine` when `XDG_CACHE_HOME` is set to an absolute path, otherwise
  `$HOME/.cache/jeff/engine`.

`HOME` is read from the environment (Java reads the environment variable too, not the `user.home` property), and is
needed on every machine, since finding uv needs it too. When it is not set, or empty, local mode raises `EngineError`:
`HOME is not set, so local mode cannot find the folder for the Jeff engine (the user's cache folder)`. The folder is
created when missing, with every missing parent; the `engine` folder itself with permissions `0700`. It holds:

| File | What it is |
| - | - |
| `engine.sock` | The engine's socket. |
| `engine.lock` | Held while a program starts the engine. |
| `engine.log` | The output (standard output and standard error) of the last engine started here, emptied at each start. |

## Finding uv

Local mode needs the `uvx` command, which comes with uv (`uv tool run`). It looks, in order:

1. in each folder on `PATH` (split at `:`, empty and relative entries skipped, so never the current folder), for a
   file `uvx` that is executable;
2. at `$HOME/.local/bin/uvx`, where uv's installer puts it;
3. at `$HOME/.cargo/bin/uvx`, where `cargo install` puts it.

The first found is used. When none is, local mode raises `RuntimeMissing`:

`Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at <home>/.local/bin/uvx or
<home>/.cargo/bin/uvx. Install uv once with: curl -LsSf https://astral.sh/uv/install.sh | sh (see
https://docs.astral.sh/uv/). Or start an engine yourself (jeff runtime serve) and give its socket instead of the
adapters`

There is no other way to start the engine: never `pip`, `python` or anything else found on the machine.

## The start lock

`engine.lock` makes sure only one program starts an engine at a time. A program takes it by creating the file
exclusively (it must not exist; `O_CREAT | O_EXCL` or the language's own form) and writing its process ID and a line
feed into it. It releases the lock by deleting the file, if it still holds its own process ID.

When the file exists, the program reads it. If it holds the ID of a process that is still running, the program waits:
it prints the waiting line (below) once, then tries again every 200 ms. If it holds the ID of a process that is no
longer running, or (written more than 10 seconds ago) no process ID at all, the lock is stale: the program deletes the
file and tries again at once. Waiting is bounded by the start timeout, counted from the call; when it runs out the
program raises `EngineError`:

`Another program has been starting the Jeff engine for longer than <start timeout> ms (the lock <lock path>, held by
process <pid>). Its log is <log path>`, where `<pid>` is `(none written yet)` when the file holds no process ID.

## Starting

The program prints one line to standard error, so a first start (which downloads the engine and the base model) is
never silent:

`Jeff: starting the Jeff engine for <adapter ids, joined by ", ">; the first time, this downloads the engine and the base model (about 2 GB, once). Its log: <log path>`

The line printed while waiting for another program's start is:

`Jeff: waiting for the Jeff engine that another program is starting. Its log: <log path>`

It then empties `engine.log` and runs this command (as an argument list, not through a shell), with the folder as its
working folder, standard input empty, standard output and standard error written to `engine.log`, and the program's
own environment:

```
<uvx> --from jeff-runtime[<extra>]==<SDK version> jeff runtime serve
  --socket <folder>/engine.sock
  --base-version <base id>@<base version>
  --adapter <adapter id>            (once per adapter, in the order of step 2)
  --batch-window-ms 0.1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
  --max-adapters <the larger of 8 and the number of adapters> --prefix-pool-mb 7345     (cuda only)
  --exit-when-unused-s 300
```

- `<uvx>` is the path found above, `<extra>` is `mac` or `cuda`, and `<SDK version>` is the version of the SDK
  package itself: every SDK runs the engine of its own version, the one it was tested with. `<base id>@<base
  version>` is the base the adapters' manifests name (for example `jeff-base@v1.4`).
- The batching values are the ones the docs give for starting an engine by hand. On an NVIDIA GPU the fast backend
  also needs room for adapters and a prefix pool; `7345` MB holds 64 prompt prefixes of 8,192 tokens.
- `--exit-when-unused-s 300` makes the engine stop by itself once no program has been connected to it for 5 minutes
  (see "Lifetime").

The engine is started in a process group of its own, so that it outlives the program that started it and is not
stopped by a Ctrl+C (or any signal) meant for that program's group. Node and Rust do this with their own process API
(Node: a detached process, which is a new session; Rust: `process_group(0)`). Java and C# cannot, so they start it
through a shell script, the uvx argument list following the script, with the engine folder as the working folder and
after `engine.log` was emptied. The engine's input and output are redirected inside the script (some languages cannot
point a child's output at a file); the shell's own output may be read and dropped. Job control (`set -m`) is used only
where it works without a terminal, never with dash (Debian's and Ubuntu's `/bin/sh`), whose `set -m` needs one:

- **macOS:** `/bin/bash -c '<script>' <uvx> --from ...`, where the script is

  ```
  set -m; "$0" "$@" </dev/null >>engine.log 2>&1 & pid=$!; set +m; <check>; trap "kill -TERM -$pid" TERM; wait $pid
  ```

  (`set -m` gives the background command its own process group; `set +m` at once after, so bash writes no job notice
  such as `[1]+ Done(3)`).
- **Linux:** `/bin/sh -c '<script>' <setsid> <uvx> --from ...`, where `<setsid>` is the `setsid` command (from
  util-linux; BusyBox has one too), which makes the engine a new session and process group, and the script is

  ```
  "$0" "$@" </dev/null >>engine.log 2>&1 & pid=$!; <check>; trap "kill -TERM -$pid" TERM; wait $pid
  ```

`<check>` makes sure the engine really leads a process group of its own before anything relies on it:

```
i=0; until kill -0 -$pid 2>/dev/null; do kill -0 $pid 2>/dev/null || break; i=$((i+1)); if [ $i -ge 50 ]; then kill -TERM $pid; exit 125; fi; sleep 0.1; done
```

It waits up to 5 seconds for the group to exist (with `setsid`, the engine makes it as it starts); if the engine is
still running and no group of its ID exists, it stops the engine and the shell exits with status 125. The program
raises `EngineError` for status 125, whatever the log holds:

`The Jeff engine could not be started in a process group of its own, so it was stopped.` followed by the log as for an
engine that exited (below).

A `TERM` sent to the shell is passed on to the engine's group, and the shell's exit status is otherwise the engine's.

`<setsid>` is looked for (Linux, Java and C# only, when an engine is to be started, after uvx) as uvx is: in each
absolute folder on `PATH`, then at `/usr/bin/setsid` and `/bin/setsid`. When none is found, local mode raises
`RuntimeMissing`:

`Local mode needs the setsid command to start the Jeff engine in a session of its own, and setsid is not on PATH, nor
at /usr/bin/setsid or /bin/setsid. Install util-linux (for example: apt-get install util-linux, or dnf install
util-linux). Or start an engine yourself (jeff runtime serve) and give its socket instead of the adapters`

The program does not wait for the engine to exit and does not keep it alive.

The program then waits for the engine to answer: every 200 ms it tries to connect to `engine.sock` (hello,
`installed`, `health`, bounded by the reply timeout); the engine binds its socket only once its model is loaded, so a
connection that completes means the engine is ready. Two things end the wait with an `EngineError`:

- **The engine exits** before it answers:

  `The Jeff engine exited while starting, with status <status>. Its log is <log path>; its last lines:` and then, on
  the following lines, the log's last lines (below). `<status>` is the process's exit status; a process ended by
  signal `n` has status `128 + n`, as a shell reports it.
- **The start timeout runs out** (counted from the call): the program sends `SIGTERM` to the engine's process group
  (it started that engine, so it may stop it) and raises:

  `The Jeff engine did not answer on <socket path> within <start timeout> ms, so it was stopped. Its log is <log
  path>; its last lines:` and then the log's last lines.

The log's last lines are found by splitting the log's text at line feeds (`\n`), dropping the empty piece after a final
line feed, and keeping the last 20 pieces, each as it is, joined with line feeds. An empty log gives instead the
message's ending `Its log, <log path>, is empty` in place of `Its log is <log path>; its last lines:` and the lines.

## Sharing

The engine found or started serves every program of this user on this machine: a second program, or a second call in
the same program, finds it at `engine.sock` (step 5) and uses it, and never starts another.

When the engine does not serve every adapter asked for (its `installed` list lacks some), the program asks it to, with
one `serve` request (`contract/socket.md`) listing the adapters the engine serves now (without `base`, in the order
`installed` gave them) followed by the missing ones in the order of step 2, so nothing another program uses is
unloaded. An engine can only load an adapter it was given at its start or has found in a folder, so a `serve` for a new
catalogue adapter is usually refused. A refusal raises `NotInstalled`:

`The Jeff engine already running at <socket path> does not serve <missing adapter ids, joined by ", ">, and refused to
load them: <the engine's error type>: <the engine's message>. Start an engine yourself with every adapter you need
(jeff runtime serve) and give its socket instead of the adapters; the shared engine stops by itself 5 minutes after the
last program using it closes`

No second engine is ever started for the missing adapters.

## Lifetime

An engine started by local mode stops by itself 300 seconds (5 minutes) after the last program connected to it
disconnects (`jeff runtime serve --exit-when-unused-s 300`): a program holds its connection open for as long as its
client is open, and the system closes it when the program exits or crashes. The wait lets a program that is run again
soon find the engine still loaded. The SDKs never stop an engine themselves, except one they started that did not
answer within the start timeout, and so never stop an engine started by hand (`jeff runtime serve` without
`--exit-when-unused-s` runs until it is stopped).

`jeff runtime serve --exit-when-unused-s SECONDS` (a number above 0): the engine closes its socket and exits with status
0 once no client has been connected for `SECONDS` seconds in a row, counted from when it starts listening and from
each time its last client disconnects.

## What an SDK tests

Each SDK tests, against `contract/conformance/local_engine.json` (no GPU needed): the platform choice, the engine
folder, finding uvx, the command line, the log's last lines and the wording of every message and error above. With a
stub `uvx` on `PATH` that starts a stub engine on the socket, its unit tests cover (Java and C# also run their start
command, under `/bin/dash` for the Linux form, with a stand-in `setsid` where the real one is missing, and show the
engine leads its own process group, and that the check exits with 125 when job control cannot give it one): starting an engine, a second client
reusing it, the `serve` for a missing adapter and its refusal, uv missing, an unsupported platform, the socket form
never starting anything, the start timeout, and the engine exiting during its start.
