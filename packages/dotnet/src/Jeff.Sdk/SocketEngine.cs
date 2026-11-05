using System.Diagnostics;
using System.Net.Sockets;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>An engine over the engine socket (contract/socket.md; port of jeff/socket_engine.py and the Node SDK's
/// socket-engine.ts): one connection, requests multiplexed by id and sent as they come, so the engine batches
/// concurrent callers. After the engine closes the connection, the next call makes one fresh connection (at most one
/// attempt per ReconnectInterval); a call in flight when it closed fails and is never resent. It never starts an engine.</summary>
public sealed class SocketEngine : IEngine
{
    /// <summary>The least time between two reconnect attempts. A call made sooner after an attempt fails at once, naming
    /// that attempt, so that while the engine is down every caller learns so immediately and its socket is not hammered.</summary>
    public static readonly TimeSpan ReconnectInterval = TimeSpan.FromSeconds(1);

    /// <summary>Replies to these ops carry the engine's whole installed list beside its version; any other reply carries
    /// only the version, and a version different from the cached one means asking for the list with `installed`.</summary>
    private static readonly HashSet<string> CarryAdapters = new(StringComparer.Ordinal) { "installed", "serve", "reload" };

    private readonly object gate = new();
    private readonly Dictionary<long, TaskCompletionSource<JsonObject>> pending = new();
    /// <summary>The reconnect attempt in progress (set and cleared under gate), which every call that finds the
    /// connection closed or being checked waits for.</summary>
    private Task? running;

    /// <summary>Cancelled by DisposeAsync: ends a reconnect attempt under way, closing its socket.</summary>
    private readonly CancellationTokenSource disposing = new();

    /// <summary>The error DisposeAsync ended the engine with; a call still waiting for a reconnect attempt then fails
    /// with it, as a call waiting for a reply does.</summary>
    private EngineError? disposedWith;
    private Connection? connection;
    private JeffError? closed;
    private bool closedByCaller;
    private long nextId;
    private long snapshotVersion = -1;
    private HashSet<string> snapshotInstalled = new(StringComparer.Ordinal);
    private Attempt? lastAttempt;
    private BaseFormat format = new("");
    private (string Id, string Version) served = ("", "");
    private int reconnects;

    private SocketEngine(string path, TimeSpan timeout)
    {
        Path = path;
        Timeout = timeout;
    }

    /// <summary>The engine socket's path.</summary>
    public string Path { get; }

    /// <summary>How long to wait for the engine's hello and for each reply.</summary>
    public TimeSpan Timeout { get; }

    /// <summary>How many times a call has reconnected to the engine after its connection closed.</summary>
    public int Reconnects => Volatile.Read(ref reconnects);

    /// <summary>The base's weight format, read once at connect.</summary>
    public BaseFormat BaseFormat => format;

    /// <summary>The base the engine serves as adapter manifests name it, read once at connect.</summary>
    public (string Id, string Version) Base => served;

    /// <summary>The engine's adapters_version as of the last reply read from it (-1 from a reconnect until its first reply).</summary>
    public long AdaptersVersion
    {
        get
        {
            lock (gate) return snapshotVersion;
        }
    }

    internal Task ReadLoop
    {
        get
        {
            lock (gate) return connection?.Reading ?? Task.CompletedTask;
        }
    }

    private sealed class Attempt(long at)
    {
        public long At { get; } = at;

        public EngineError? Failure { get; set; }
    }

    /// <summary>Nothing listens at the path: the socket's own error, which connect and reconnect each word.</summary>
    private sealed class DialFailure(string message) : Exception(message);

    /// <summary>The command that starts an engine listening at `path`.</summary>
    internal static string StartCommand(string path) =>
        $"jeff runtime serve --socket {path} --adapter <adapter id> --batch-window-ms <milliseconds> --max-batch-rows <rows> "
        + "--queue-limit-rows <rows> --prepare-workers <threads>";

    /// <summary>Connects to the engine at `path`, exchanges hellos, and reads its installed list and base once.</summary>
    public static async Task<SocketEngine> ConnectAsync(string path, TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        Timeouts.Check(timeout);
        var engine = new SocketEngine(path, timeout);
        try
        {
            await engine.OpenAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (DialFailure failure)
        {
            throw new EngineError($"No engine is listening at {path}: {failure.Message}. Start one with: {StartCommand(path)}");
        }
        try
        {
            // no reconnect while connecting: a connection that closes now fails the connect, naming why
            var (version, installed) = await engine.InstalledNowAsync(null, false, cancellationToken).ConfigureAwait(false);
            engine.Merge(version, installed);
            (engine.format, engine.served) = await engine.BaseNowAsync(false, cancellationToken).ConfigureAwait(false);
        }
        catch
        {
            await engine.DisposeAsync().ConfigureAwait(false);
            throw;
        }
        return engine;
    }

    /// <summary>One connection: its socket, its reading task, and the handshake stages. "Handshake" until the hello is
    /// read; "Early" from then until it is the engine's current connection, while any frame (or stream failure) that came
    /// in the same bytes as the hello is kept; "Ready" after.</summary>
    private sealed class Connection
    {
        private enum Stage { Handshake, Early, Ready }

        private readonly SocketEngine engine;
        private readonly object sync = new();
        private readonly SemaphoreSlim writing = new(1, 1);
        private readonly FrameReader reader = new();
        private readonly List<byte[]> early = new();
        private JeffError? earlyFailure;
        private Stage stage = Stage.Handshake;

        public Connection(SocketEngine engine, Socket socket)
        {
            this.engine = engine;
            Socket = socket;
        }

        public Socket Socket { get; }

        public TaskCompletionSource<byte[]> Greeting { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public Task Reading { get; private set; } = Task.CompletedTask;

        public void StartReading() => Reading = Task.Run(ReadAsync);

        private async Task ReadAsync()
        {
            var buffer = new byte[64 * 1024];
            try
            {
                while (true)
                {
                    int count = await Socket.ReceiveAsync(buffer.AsMemory(), SocketFlags.None).ConfigureAwait(false);
                    if (count == 0)
                    {
                        Broken(null);
                        return;
                    }
                    foreach (var body in reader.Push(buffer.AsSpan(0, count))) Deliver(body);
                    if (reader.Failure is { } failure)
                    {
                        Broken(failure);
                        return;
                    }
                }
            }
            catch (Exception error)
            {
                // Every way reading can end (a socket error, a frame refused, a reply this side cannot handle) ends the
                // connection with an error naming it, as Python's reader thread does; the reading task itself never faults.
                Broken(error);
            }
        }

        private EngineError ReadyError(Exception? cause) => cause is null
            ? new EngineError($"The engine at {engine.Path} closed the connection")
            : new EngineError($"The engine socket to {engine.Path} failed: {Errors.Described(cause)}");

        /// <summary>The stream ended: closed by the engine (cause null), a socket error, or a frame refused.</summary>
        private void Broken(Exception? cause)
        {
            lock (sync)
            {
                switch (stage)
                {
                    case Stage.Handshake:
                        Exception failure = cause switch
                        {
                            InvalidRequest refused => refused,
                            null => new EngineError($"The engine at {engine.Path} closed the connection during the handshake"),
                            _ => new EngineError($"The engine at {engine.Path} closed the connection during the handshake: {cause.Message}"),
                        };
                        Greeting.TrySetException(failure);
                        break;
                    case Stage.Early:
                        earlyFailure ??= ReadyError(cause);
                        break;
                    default:
                        engine.Fail(this, ReadyError(cause));
                        break;
                }
            }
        }

        private void Deliver(byte[] body)
        {
            lock (sync)
            {
                switch (stage)
                {
                    case Stage.Handshake:
                        stage = Stage.Early;
                        Greeting.TrySetResult(body);
                        break;
                    case Stage.Early:
                        early.Add(body);
                        break;
                    default:
                        engine.OnReply(this, body);
                        break;
                }
            }
        }

        /// <summary>Makes this the engine's connection and hands it the frames (and the failure) that came with the hello.</summary>
        public void MakeCurrent()
        {
            lock (sync)
            {
                engine.Adopt(this);
                stage = Stage.Ready;
                foreach (var body in early) engine.OnReply(this, body);
                early.Clear();
                if (earlyFailure is not null) engine.Fail(this, earlyFailure);
            }
        }

        /// <summary>Writes the hello, the first frame on a new connection, and waits for it to be written.</summary>
        public async Task SendHelloAsync(byte[] frame)
        {
            await writing.WaitAsync().ConfigureAwait(false);
            try
            {
                int sent = 0;
                while (sent < frame.Length) sent += await Socket.SendAsync(frame.AsMemory(sent), SocketFlags.None).ConfigureAwait(false);
            }
            finally
            {
                writing.Release();
            }
        }

        /// <summary>Waits at most `wait` (and until cancelled) for this connection's turn to write, then starts writing
        /// `frame` in a task of its own and returns without waiting for it: false when the turn did not come in time, and
        /// nothing was sent. Frames never interleave, and a frame once started is always finished, or its failure ends
        /// the connection for every call by name; a caller that times out or is cancelled only gives up its place in
        /// line or stops waiting.</summary>
        public async Task<bool> StartSendAsync(byte[] frame, TimeSpan wait, CancellationToken cancellationToken)
        {
            if (!await writing.WaitAsync(wait, cancellationToken).ConfigureAwait(false)) return false;
            _ = WriteAsync(frame); // never faults: a failure ends the connection
            return true;
        }

        private async Task WriteAsync(byte[] frame)
        {
            try
            {
                int sent = 0;
                while (sent < frame.Length) sent += await Socket.SendAsync(frame.AsMemory(sent), SocketFlags.None).ConfigureAwait(false);
            }
            catch (Exception error)
            {
                engine.Fail(this, new EngineError($"The engine socket to {engine.Path} failed: {Errors.Described(error)}"));
            }
            finally
            {
                writing.Release();
            }
        }

        public void Dispose() => Socket.Dispose();
    }

    /// <summary>Opens a connection, exchanges hellos (bounded by Timeout) and makes it the connection every call uses. A
    /// failure closes the new socket and leaves the current state as it was.</summary>
    private async Task OpenAsync(CancellationToken cancellationToken)
    {
        UnixDomainSocketEndPoint endpoint;
        try
        {
            endpoint = new UnixDomainSocketEndPoint(Path);
        }
        catch (ArgumentException error)
        {
            // e.g. a path longer than the platform's limit, which Python reports as the OSError "AF_UNIX path too long"
            throw new DialFailure(error.Message);
        }
        var socket = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        try
        {
            await socket.ConnectAsync(endpoint, cancellationToken).ConfigureAwait(false);
        }
        catch (SocketException error)
        {
            socket.Dispose();
            throw new DialFailure(error.Message);
        }
        catch
        {
            socket.Dispose();
            throw;
        }
        var current = new Connection(this, socket);
        current.StartReading();
        try
        {
            try
            {
                await current.SendHelloAsync(Protocol.EncodeFrame(Protocol.Hello())).ConfigureAwait(false);
            }
            catch (SocketException error)
            {
                throw new EngineError($"The engine at {Path} closed the connection during the handshake: {error.Message}");
            }
            byte[] body;
            try
            {
                body = await current.Greeting.Task.WaitAsync(Timeout, cancellationToken).ConfigureAwait(false);
            }
            catch (TimeoutException)
            {
                throw new EngineError($"No hello from the engine at {Path} within {PyFormat.Millis(Timeout)} ms");
            }
            var message = Protocol.DecodeBody(body);
            if (Json.IsFalse(message["ok"])) throw Rebuild(JsonRead.Object(message, "error", "the engine's hello failure"));
            Protocol.CheckHello(message);
            current.MakeCurrent();
        }
        catch
        {
            current.Dispose();
            Observe(current.Greeting.Task); // a hello that fails after this one gave up is nobody's to raise
            throw;
        }
    }

    /// <summary>Marks a task nobody will await again (a call that timed out or was cancelled, a handshake that gave up)
    /// as observed, so a failure set on it later is not reported as an unobserved task exception. Its caller has
    /// already been given its own error.</summary>
    internal static void Observe(Task task) => task.ContinueWith(static done => _ = done.Exception, CancellationToken.None,
        TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);

    private void Adopt(Connection current)
    {
        lock (gate)
        {
            // checked under the same lock DisposeAsync sets it under: a connection opened while the caller closed the
            // engine is never made current
            if (closedByCaller) throw Dead();
            connection = current;
            snapshotVersion = -1; // a restarted engine counts from 0 again; the list is kept
            closed = null;
        }
    }

    /// <summary>The error a failure reply describes. The engine puts its adapters_version in every failure's details;
    /// that is protocol information, not an attribute of the error, so it is left out.</summary>
    private static JeffError Rebuild(JsonObject failure)
    {
        // required by the socket schema every incoming message is checked against (contract/schemas/socket.schema.json)
        var details = (JsonObject)Json.Canonical(JsonRead.Object(failure, "details", "an engine failure"))!;
        details.Remove("adapters_version");
        return Errors.FromWire(JsonRead.String(failure, "type", "an engine failure"), JsonRead.String(failure, "message", "an engine failure"), details);
    }

    private void OnReply(Connection source, byte[] body)
    {
        JsonObject message;
        try
        {
            message = Protocol.DecodeBody(body);
        }
        catch (InvalidRequest error)
        {
            Fail(source, new EngineError($"The engine socket to {Path} failed: {Errors.Described(error)}"));
            return;
        }
        if (Json.KindOf(message["id"]) != JsonKind.Int)
        {
            Fail(source, new EngineError($"The engine socket to {Path} failed: the engine sent a message with no request id (op {PyFormat.Repr(message["op"])})"));
            return;
        }
        if (!Json.TryLong(message["id"], out long id))
        {
            Fail(source, new EngineError($"The engine socket to {Path} failed: the engine sent a message whose request id {PyFormat.Repr(message["id"])} is "
                + $"outside -2**63 to 2**63-1, so it answers no request this client sent (op {PyFormat.Repr(message["op"])})"));
            return;
        }
        if (id == 0 && Json.IsFalse(message["ok"]))
        {
            // not a reply to any call (this side's ids start at 1): a connection-level failure every waiting call fails with
            Fail(source, Rebuild(JsonRead.Object(message, "error", "an engine failure")));
            return;
        }
        TaskCompletionSource<JsonObject>? waiting;
        lock (gate)
        {
            if (!pending.Remove(id, out waiting)) return; // a late reply to a call that timed out or was cancelled
        }
        waiting.TrySetResult(message);
    }

    /// <summary>Ends `source` if it is still the current connection: the first error wins and every waiting call fails
    /// with its own copy of it. A connection that is no longer the current one is only closed.</summary>
    private void Fail(Connection source, JeffError error)
    {
        List<TaskCompletionSource<JsonObject>>? waiting = null;
        JeffError? reason = null;
        lock (gate)
        {
            if (source == connection)
            {
                closed ??= error;
                reason = closed;
                waiting = pending.Values.ToList();
                pending.Clear();
            }
        }
        if (waiting is not null) foreach (var call in waiting) call.TrySetException(Errors.Copy(reason!));
        // disposed outside the gate: disposing can end the connection's pending receive on this thread, and its reading
        // task then takes the connection's lock, which is always taken before the gate, never after
        source.Dispose();
    }

    /// <summary>The error for a call made once the connection is closed for good.</summary>
    private EngineError Dead()
    {
        var cause = closed ?? throw new InvalidOperationException("Dead() is only called once the connection has closed");
        return new EngineError($"The engine at {Path} closed the connection after: {Errors.Described(cause)}");
    }

    /// <summary>The error for a call made within ReconnectInterval of the last attempt, naming what it found.</summary>
    private EngineError TooSoon(long now)
    {
        var last = lastAttempt ?? throw new InvalidOperationException("TooSoon() is only called after an attempt");
        string ago = PyFormat.Fixed(Stopwatch.GetElapsedTime(last.At, now).TotalSeconds, 2);
        string interval = PyFormat.Float(ReconnectInterval.TotalSeconds);
        if (last.Failure is not null)
        {
            return new EngineError($"No engine is listening at {Path}: the last reconnect attempt, {ago}s ago, failed: {last.Failure.Message}. "
                + $"The next attempt may be made {interval}s after it");
        }
        return new EngineError($"The engine at {Path} closed the connection {ago}s ago, just after this side reconnected: {Errors.Described(closed!)}. "
            + $"The next attempt may be made {interval}s after the last");
    }

    /// <summary>Called by a call that found the connection closed (or a reconnect attempt still checking it): one
    /// attempt at a time makes one fresh connection (connect, hello, installed, health, the base check); a call that
    /// waited meanwhile then finds the connection open and uses it, or fails naming that attempt. At most one attempt per
    /// ReconnectInterval; never after DisposeAsync. The attempt is shared by every waiting call, so it runs without any
    /// caller's token (bounded by Timeout, as in Python): a caller that cancels only stops waiting for it, and the new
    /// connection is still checked, or discarded with the attempt failing by name. A call's own timeout does not bound its
    /// wait for an attempt (each step of the attempt is bounded by Timeout instead), as in Python. DisposeAsync ends an
    /// attempt at once, and its waiting calls fail with the error it closed the engine with.</summary>
    private async Task ReconnectAsync(CancellationToken cancellationToken)
    {
        while (true)
        {
            Task waitFor;
            Attempt? mine = null;
            lock (gate)
            {
                if (closedByCaller) throw Dead();
                if (running is not null)
                {
                    waitFor = running;
                }
                else
                {
                    if (closed is null && connection is not null) return;
                    long now = Stopwatch.GetTimestamp();
                    if (lastAttempt is not null && Stopwatch.GetElapsedTime(lastAttempt.At, now) < ReconnectInterval) throw TooSoon(now);
                    mine = new Attempt(now);
                    lastAttempt = mine;
                    var attempt = mine;
                    // started on the thread pool, so none of it runs under this lock
                    waitFor = running = Task.Run(() => AttemptAsync(attempt));
                }
            }
            // the attempt task itself never faults: its outcome is attempt.Failure
            await waitFor.WaitAsync(cancellationToken).ConfigureAwait(false);
            lock (gate)
            {
                if (disposedWith is not null) throw Errors.Copy(disposedWith);
            }
            if (mine is not null)
            {
                if (mine.Failure is not null) throw mine.Failure;
                return;
            }
            // another call's attempt ended: look again (the connection open, or a failure within the interval named)
        }
    }

    /// <summary>One reconnect attempt. It never throws: how it failed is attempt.Failure, which its own caller raises and
    /// later calls within the interval name.</summary>
    private async Task AttemptAsync(Attempt attempt)
    {
        try
        {
            (long Version, List<string> Installed) listing;
            (BaseFormat Format, (string Id, string Version) Base) found;
            try
            {
                await OpenAsync(disposing.Token).ConfigureAwait(false);
                listing = await InstalledNowAsync(null, false, disposing.Token).ConfigureAwait(false);
                found = await BaseNowAsync(false, disposing.Token).ConfigureAwait(false);
            }
            catch (Exception cause)
            {
                // every way the attempt can fail is one named error, as in Python's _reconnect
                var error = new EngineError($"No engine is listening at {Path}: {cause.Message} (reconnect attempt)");
                attempt.Failure = error;
                Shutdown(error);
                return;
            }
            if (PyFormat.Repr(found.Format.ToJson()) != PyFormat.Repr(format.ToJson()) || found.Base != served)
            {
                // Functions bound through this engine chose their calibration for the format read at connect; the restarted
                // engine serves other weights, so nothing more is sent to it from this process.
                var error = new EngineError($"The engine at {Path} restarted serving a base of format {PyFormat.Repr(found.Format.ToJson())} "
                    + $"({found.Base.Id}@{found.Base.Version}), not {PyFormat.Repr(format.ToJson())} ({served.Id}@{served.Version}) as when this process "
                    + "connected; the functions bound here use that format's calibration, and the adapters listed are the ones trained for that base. "
                    + "Restart this process to serve the new base");
                attempt.Failure = error;
                Shutdown(error);
                return;
            }
            Merge(listing.Version, listing.Installed);
            Interlocked.Increment(ref reconnects);
        }
        finally
        {
            lock (gate) running = null;
        }
    }

    private void Shutdown(JeffError error)
    {
        Connection? current;
        lock (gate) current = connection;
        if (current is not null) Fail(current, error);
    }

    private long VersionOf(JsonObject body, string op)
    {
        if (!body.ContainsKey("adapters_version"))
        {
            throw new EngineError($"The engine at {Path} answered {PyFormat.Str(op)} without an adapters_version; every reply must carry one (contract/socket.md)");
        }
        return JsonRead.Long(body, "adapters_version", $"the engine's {op} reply");
    }

    /// <summary>Sends one request and waits for its reply. `reconnect` false is a call made while connecting or by a
    /// reconnect attempt itself: a connection already closed again fails at once.</summary>
    private async Task<JsonObject> CallAsync(JsonObject message, TimeSpan? timeoutOverride, bool reconnect, CancellationToken cancellationToken)
    {
        if (timeoutOverride is { } given) Timeouts.Check(given);
        var timeout = timeoutOverride ?? Timeout;
        cancellationToken.ThrowIfCancellationRequested();
        string op = JsonRead.String(message, "op", "a socket request");
        long id = Interlocked.Increment(ref nextId);
        var framed = (JsonObject)Json.Canonical(message)!;
        framed["id"] = id;
        // encoded before anything is registered: an oversized or invalid message leaves nothing behind
        byte[] frame = Protocol.EncodeFrame(framed);
        while (true)
        {
            Connection? current;
            lock (gate)
            {
                // while an attempt is still checking a new connection, only the attempt's own calls use it
                current = closed is null && (running is null || !reconnect) ? connection : null;
                if (current is null && !reconnect) throw closedByCaller || lastAttempt is null ? Dead() : TooSoon(Stopwatch.GetTimestamp());
            }
            if (current is null)
            {
                await ReconnectAsync(cancellationToken).ConfigureAwait(false);
                continue;
            }
            var waiting = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
            lock (gate)
            {
                if (closed is not null || connection != current || (reconnect && running is not null)) continue;
                pending[id] = waiting;
            }
            // one deadline for the whole call: the wait for its turn to write and the wait for its reply
            long started = Stopwatch.GetTimestamp();
            JsonObject reply;
            try
            {
                if (!await current.StartSendAsync(frame, timeout, cancellationToken).ConfigureAwait(false)) throw new TimeoutException();
                var remaining = timeout - Stopwatch.GetElapsedTime(started);
                reply = await waiting.Task.WaitAsync(remaining > TimeSpan.Zero ? remaining : TimeSpan.Zero, cancellationToken).ConfigureAwait(false);
            }
            catch (TimeoutException)
            {
                lock (gate) pending.Remove(id);
                Observe(waiting.Task);
                throw new EngineError($"No reply from the engine at {Path} within {PyFormat.Millis(timeout)} ms");
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                lock (gate) pending.Remove(id);
                Observe(waiting.Task);
                throw;
            }
            return await AnswerAsync(op, reply, timeout, reconnect, cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Every reply carries the engine's adapters_version: a reply to installed/serve/reload sets the cached
    /// list from its own; any other reply with another version makes this side ask for the list (except a failed
    /// `installed`). A failure is raised as the error it describes, after that; a failure reading its version or
    /// refreshing the list is a note on it.</summary>
    private async Task<JsonObject> AnswerAsync(string op, JsonObject reply, TimeSpan timeout, bool reconnect, CancellationToken cancellationToken)
    {
        if (!Json.IsTrue(reply["ok"]))
        {
            var failure = JsonRead.Object(reply, "error", "an engine failure reply");
            var original = Rebuild(failure);
            long version;
            try
            {
                version = VersionOf(JsonRead.Object(failure, "details", "an engine failure reply"), op);
            }
            catch (EngineError error)
            {
                original.Notes.Add($"Also failed to read adapters_version from this failure reply: {Errors.Described(error)}");
                throw original;
            }
            if (op != "installed")
            {
                try
                {
                    await FollowAsync(version, timeout, reconnect, cancellationToken).ConfigureAwait(false);
                }
                catch (JeffError error)
                {
                    original.Notes.Add($"Also failed to refresh the adapter list after this error: {Errors.Described(error)}");
                }
            }
            throw original;
        }
        var result = JsonRead.Object(reply, "result", "an engine reply");
        reply.Remove("result"); // handed to the caller (HealthAsync, StatsAsync) as an object of its own, with no parent
        long current = VersionOf(result, op);
        if (CarryAdapters.Contains(op)) Merge(current, JsonRead.Strings(result, "adapters", $"the engine's {op} reply"));
        else await FollowAsync(current, timeout, reconnect, cancellationToken).ConfigureAwait(false);
        return result;
    }

    /// <summary>Asks for the list when `version` is not the cached one. `reconnect` is the asking call's: a call made
    /// while connecting or by a reconnect attempt never starts another attempt (which would wait for itself).</summary>
    private async Task FollowAsync(long version, TimeSpan? timeout, bool reconnect, CancellationToken cancellationToken)
    {
        long known;
        lock (gate) known = snapshotVersion;
        if (version == known) return;
        var (fresh, installed) = await InstalledNowAsync(timeout, reconnect, cancellationToken).ConfigureAwait(false);
        Merge(fresh, installed);
    }

    private async Task<(long Version, List<string> Installed)> InstalledNowAsync(TimeSpan? timeout, bool reconnect, CancellationToken cancellationToken)
    {
        var result = await CallAsync(new JsonObject { ["op"] = "installed" }, timeout, reconnect, cancellationToken).ConfigureAwait(false);
        return (JsonRead.Long(result, "adapters_version", "the engine's installed reply"), JsonRead.Strings(result, "adapters", "the engine's installed reply"));
    }

    /// <summary>The one place the cached pair is written. Replies may arrive out of order, so an older version never
    /// replaces a newer one.</summary>
    private void Merge(long version, IEnumerable<string> installed)
    {
        lock (gate)
        {
            if (version < snapshotVersion) return;
            snapshotVersion = version;
            snapshotInstalled = new HashSet<string>(installed, StringComparer.Ordinal);
        }
    }

    /// <summary>From the engine's health: its base format (format, and format_id for an INT4 base) and the base it serves.</summary>
    private async Task<(BaseFormat Format, (string Id, string Version) Base)> BaseNowAsync(bool reconnect, CancellationToken cancellationToken)
    {
        var health = await CallAsync(new JsonObject { ["op"] = "health" }, null, reconnect, cancellationToken).ConfigureAwait(false);
        if (health["base"] is not JsonObject block || Json.KindOf(block["format"]) != JsonKind.String)
        {
            throw new EngineError($"The engine at {Path} answered 'health' without base.format (got base {PyFormat.Repr(health["base"])}); every engine "
                + "reports its base's weight format (contract/socket.md)");
        }
        if (Json.KindOf(block["id"]) != JsonKind.String || Json.KindOf(block["version"]) != JsonKind.String)
        {
            throw new EngineError($"The engine at {Path} answered 'health' without base.id and base.version (got base {PyFormat.Repr(block)}); every engine "
                + "reports the base it serves as adapter manifests name it (contract/socket.md)");
        }
        var baseFormat = new BaseFormat(block["format"]!.GetValue<string>(), block.ContainsKey("format_id") ? JsonRead.String(block, "format_id", "the engine's base") : null);
        return (baseFormat, (block["id"]!.GetValue<string>(), block["version"]!.GetValue<string>()));
    }

    /// <summary>The cached list of adapters the engine has loaded; with refresh, asked of the engine first.</summary>
    public async Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default)
    {
        if (refresh)
        {
            var (version, installed) = await InstalledNowAsync(null, true, cancellationToken).ConfigureAwait(false);
            Merge(version, installed);
        }
        lock (gate) return new HashSet<string>(snapshotInstalled, StringComparer.Ordinal);
    }

    /// <summary>The engine's (adapters_version, installed list) pair from one reply, which also updates the cache.</summary>
    public async Task<(long Version, IReadOnlySet<string> Installed)> InstalledVersionAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default)
    {
        var (version, installed) = await InstalledNowAsync(timeout, true, cancellationToken).ConfigureAwait(false);
        Merge(version, installed);
        return (version, installed.ToHashSet(StringComparer.Ordinal));
    }

    /// <summary>Runs the adapter on the rows (one list of probabilities per row) with no deadline on the engine's side. The task
    /// fails with the engine's own error (NotInstalled, InputTooLong, Busy, ...) or EngineError for a broken reply or a
    /// socket that failed or gave no reply within Timeout.</summary>
    public Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default) =>
        PredictAsync(adapterId, rows, null, cancellationToken);

    /// <summary>deadlineMs: how long the engine may hold the request before it runs (sent as deadline_ms).</summary>
    public async Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, long? deadlineMs, CancellationToken cancellationToken = default)
    {
        var message = new JsonObject
        {
            ["op"] = "predict", ["adapter"] = adapterId, ["rows"] = new JsonArray(rows.Select(row => (JsonNode?)row.ToJson()).ToArray()),
        };
        if (deadlineMs is { } deadline) message["deadline_ms"] = deadline;
        var result = await CallAsync(message, null, true, cancellationToken).ConfigureAwait(false);
        const string where = "the engine's predict reply";
        var probabilities = JsonRead.Array(result, "probabilities", where).Select(row => (IReadOnlyList<double>)JsonRead.Numbers(row, where)).ToList();
        IReadOnlyList<long>? tokens = null;
        // optional in the reply: an engine that does not count tokens leaves it out
        if (result.ContainsKey("input_tokens"))
        {
            var listed = JsonRead.Array(result, "input_tokens", where);
            tokens = listed.Select(item => Json.TryLong(item, out long count) ? count
                : throw new EngineError($"{where} has input_tokens {PyFormat.Repr(listed)}; each must be a whole number from -2**63 to 2**63-1")).ToList();
        }
        return new EngineResult(probabilities, (JsonObject)Json.Canonical(JsonRead.Object(result, "timing", where))!, tokens);
    }

    /// <summary>The engine's health reply (status, backend, adapters, base). timeout: how long to wait for the reply (Timeout
    /// when not given), so a health check need not wait as long as a prediction may.</summary>
    public Task<JsonObject> HealthAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default) =>
        CallAsync(new JsonObject { ["op"] = "health" }, timeout, true, cancellationToken);

    /// <summary>The engine's statistics reply. timeout: how long to wait for the reply (Timeout when not given), so a
    /// metrics scrape need not wait as long as a prediction may.</summary>
    public Task<JsonObject> StatsAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default) =>
        CallAsync(new JsonObject { ["op"] = "stats" }, timeout, true, cancellationToken);

    /// <summary>Final: later calls fail, and there is no reconnect. Calling it again does nothing.</summary>
    public async ValueTask DisposeAsync()
    {
        var error = new EngineError($"The SocketEngine for {Path} was closed by its caller");
        Connection? current;
        Task? attempt;
        lock (gate)
        {
            if (closedByCaller) return;
            closedByCaller = true;
            disposedWith = error;
            current = connection;
            closed ??= error;
            attempt = running;
        }
        // ends a reconnect attempt under way at once (its socket closed); the calls waiting for it fail with `error`
        disposing.Cancel();
        if (current is not null) Fail(current, error);
        if (attempt is not null)
        {
            await attempt.ConfigureAwait(false); // never faults: it ends promptly once cancelled
            Connection? opened;
            lock (gate) opened = connection;
            if (opened is not null && opened != current)
            {
                Fail(opened, error);
                current = opened;
            }
        }
        if (current is not null) await current.Reading.ConfigureAwait(false);
    }
}
