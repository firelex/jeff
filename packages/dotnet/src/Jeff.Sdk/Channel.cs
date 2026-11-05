using System.Buffers.Binary;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.WebSockets;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>Remote mode over the server's low-latency channel (contract/socket.md, "The channel"; port of
/// jeff/channel_client.py and the Node SDK's channel.ts): the channel's URL and how a token's expiry is read.</summary>
internal static class ChannelProtocol
{
    /// <summary>How long before a token's exp the refresher starts asking the token function for a fresh token, and how
    /// often it asks again while the token it returns does not expire later (or the function or the refresh fails).</summary>
    public static readonly TimeSpan RefreshBefore = TimeSpan.FromSeconds(60);
    public static readonly TimeSpan RefreshRetry = TimeSpan.FromSeconds(5);

    /// <summary>As the Python SDK's websockets (ping_interval=20, ping_timeout=20): a ping every 20 s, and a pong that
    /// has not come 20 s after its ping ends the channel.</summary>
    public static readonly TimeSpan KeepAlive = TimeSpan.FromSeconds(20);

    /// <summary>Whether this build of the package pings: the net9.0 build sets ClientWebSocketOptions.KeepAliveTimeout,
    /// which .NET 9 added (with it, KeepAliveInterval sends a ping and a pong missing for that long aborts the socket).
    /// The net8.0 build cannot: the .NET 8 ClientWebSocket has no way to send a ping (its KeepAliveInterval sends
    /// unsolicited pongs, which detect nothing), so it sends no pings.</summary>
#if NET9_0_OR_GREATER
    internal const bool Pings = true;
#else
    internal const bool Pings = false;
#endif

    /// <summary>The channel's WebSocket URL for a server URL: ws:// for http://, wss:// for https://, path /v1/channel.</summary>
    public static string Url(string url)
    {
        foreach (var (http, ws) in new[] { ("http://", "ws://"), ("https://", "wss://") })
        {
            if (url.StartsWith(http, StringComparison.Ordinal)) return ws + url[http.Length..].TrimEnd('/') + Protocol.ChannelPath;
        }
        throw new InvalidRequest($"The server URL {PyFormat.Str(url)} must start with http:// or https://");
    }

    /// <summary>The exp of a JWT (header.payload.signature), read from its payload without verifying the signature; null
    /// for a token that is not a JWT (an API key, a console session token). The payload is decoded as Python's
    /// base64.urlsafe_b64decode and json.loads read it. A JWT whose payload cannot be read or has no numeric exp is
    /// Unauthorised: the server would refuse it too. No message quotes any part of the token.</summary>
    public static double? TokenExpiry(string token)
    {
        string[] parts = token.Split('.');
        if (parts.Length != 3) return null;
        var bytes = PythonUrlSafeBase64(parts[1]);
        if (bytes is null || !PyHttpBody.TryParseJson(bytes, out var payload))
        {
            throw new Unauthorised("The token is shaped like a JWT, but its payload is not base64-encoded JSON");
        }
        var expires = payload is JsonObject fields && fields.TryGetPropertyValue("exp", out var exp) ? exp : null;
        // A NaN or infinite exp (json.loads reads NaN and Infinity) is refused too: no clock reaches it.
        return JsonRead.NumberOf(expires) is { } seconds && double.IsFinite(seconds)
            ? seconds
            : throw new Unauthorised($"The token is a JWT without a numeric exp (got {PyFormat.Repr(expires)}); the server needs one");
    }

    /// <summary>base64.urlsafe_b64decode(text + padding) as Python 3.13 decodes it (binascii.a2b_base64, not strict):
    /// '-' and '_' read as '+' and '/', every other character outside the alphabet and '=' skipped, the bits left over
    /// after the last whole byte ignored; null where Python raises (a non-ASCII string, one data character more than a
    /// multiple of four, or a last group too short for its padding).</summary>
    private static byte[]? PythonUrlSafeBase64(string text)
    {
        if (text.Any(character => character > 0x7f)) return null;
        string padded = text + new string('=', (4 - text.Length % 4) % 4);
        var output = new List<byte>(padded.Length * 3 / 4);
        int quad = 0, pads = 0, bits = 0, accumulated = 0;
        foreach (char character in padded)
        {
            if (character == '=')
            {
                if (quad >= 2) pads++;
                continue;
            }
            int value = character switch
            {
                >= 'A' and <= 'Z' => character - 'A',
                >= 'a' and <= 'z' => character - 'a' + 26,
                >= '0' and <= '9' => character - '0' + 52,
                '+' or '-' => 62,
                '/' or '_' => 63,
                _ => -1,
            };
            if (value < 0) continue;
            pads = 0;
            quad = (quad + 1) % 4;
            accumulated = (accumulated << 6 | value) & 0xffffff;
            bits += 6;
            if (bits >= 8)
            {
                bits -= 8;
                output.Add((byte)(accumulated >> bits));
            }
        }
        if (quad == 1 || (quad != 0 && quad + pads < 4)) return null;
        return output.ToArray();
    }
}

/// <summary>The ITransport over the server's low-latency channel, plus the handle operations bind, call, call_many and
/// release. Requests from any caller are multiplexed by id on one WebSocket; one reading task hands each reply to the
/// request waiting for that id. GET /v1/functions goes over HTTP (a RestTransport with the same URL, token and
/// timeout): the channel has no operation for it.
///
/// Writing: one frame is written at a time. A request waits for its turn to write within its own deadline and
/// cancellation (and gives up its place in line, having sent nothing, when either ends first); once its turn comes its
/// frame is written in a task of its own and always finished, so a caller that stops waiting never leaves half a
/// message on the socket. Frames are written in pieces of at most SendChunk bytes (one WebSocket message, fragmented);
/// a piece the socket does not accept within Timeout (the server or the network stopped taking data) ends the channel
/// with BackendUnavailable, as the Python SDK's send watchdog does.
///
/// Timeout bounds opening the WebSocket, each wait for the turn to write, each piece of a frame, each wait for a reply
/// (the hello's included), which starts once the request is written whole, as in the Python SDK, and the closing
/// handshake. A large frame on a slow but working link takes as long as it takes.</summary>
internal sealed class ChannelTransport : ITransport
{
    /// <summary>The largest piece of a frame handed to the socket at once: each accepted piece counts as progress.</summary>
    internal const int SendChunk = 64 * 1024;

    /// <summary>The largest WebSocket message read: one frame of the socket protocol (a 4-byte header and at most 64
    /// MiB), as the Python SDK's max_size.</summary>
    internal const int MaxMessage = Protocol.HeaderSize + Protocol.MaxFrame;

    private readonly RestTransport rest;
    private readonly ClientWebSocket ws;
    private readonly TokenSource? token;
    private readonly object gate = new();
    private readonly Dictionary<long, TaskCompletionSource<JsonObject>> pending = new();
    /// <summary>Ids of releases sent for garbage-collected bound functions (no one waits for their replies).</summary>
    private readonly HashSet<long> unawaited = new();
    /// <summary>Ids of requests sent whose caller stopped waiting (DeadlineExceeded, cancelled): late replies are dropped.</summary>
    private readonly HashSet<long> abandoned = new();
    private readonly ConcurrentQueue<long> releases = new();
    private readonly List<string> refreshFailures = new();
    /// <summary>The turn to write: held from a frame's first byte to its last, and by the closing handshake.</summary>
    private readonly SemaphoreSlim writing = new(1, 1);
    /// <summary>Cancelled when the channel closes: wakes the refresher and every request waiting for its turn to write.</summary>
    private readonly CancellationTokenSource stopping = new();
    private readonly TaskCompletionSource readerEnded = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private long nextId;
    private JeffError? closedError;
    private WebSocketCloseStatus closeStatus = WebSocketCloseStatus.NormalClosure;
    private TaskCompletionSource<JsonObject>? greeting;
    private int readerStarted;
    private Task currentWrite = Task.CompletedTask;
    private Task refresher = Task.CompletedTask;
    private Task? closing;

    private readonly TimeSpan keepAlive;

    private ChannelTransport(RestTransport rest, ClientWebSocket ws, string wsUrl, TokenSource? token, TimeSpan timeout, TimeSpan keepAlive)
    {
        this.keepAlive = keepAlive;
        this.rest = rest;
        this.ws = ws;
        this.token = token;
        WsUrl = wsUrl;
        Timeout = timeout;
    }

    public string Url => rest.Url;

    public string WsUrl { get; }

    public TimeSpan Timeout { get; }

    public bool Closed => Volatile.Read(ref closedError) is not null;

    /// <summary>Why the closing handshake could not finish, once the channel had already ended with its own error
    /// (which every caller got); null when it finished or was not needed.</summary>
    internal string? CloseProblem { get; private set; }

    /// <summary>Opens the channel: the WebSocket, hello, and auth when there is a token; a token function's JWT is then
    /// refreshed before its exp.</summary>
    public static Task<ChannelTransport> OpenAsync(string url, TokenSource? token, TimeSpan timeout, CancellationToken cancellationToken = default) =>
        OpenAsync(url, token, timeout, PyProxies.ProcessEnvironment(), cancellationToken);

    internal static Task<ChannelTransport> OpenAsync(string url, TokenSource? token, TimeSpan timeout, IReadOnlyList<KeyValuePair<string, string>> environment,
        CancellationToken cancellationToken) =>
        OpenAsync(url, token, timeout, environment, ChannelProtocol.KeepAlive, cancellationToken);

    /// <summary>`environment`: the variables proxies are read from: the channel's as websockets reads them
    /// (PyProxies.ForChannel), GET /v1/functions's as httpx does (PyProxies.ForHttp).</summary>
    /// <summary>`keepAlive`: how often the channel pings, and how long it waits for each pong (ChannelProtocol.KeepAlive;
    /// tests make it short).</summary>
    internal static async Task<ChannelTransport> OpenAsync(string url, TokenSource? token, TimeSpan timeout, IReadOnlyList<KeyValuePair<string, string>> environment,
        TimeSpan keepAlive, CancellationToken cancellationToken)
    {
        var rest = new RestTransport(url, token, timeout, environment); // also checks the URL and the timeout
        ChannelTransport transport;
        try
        {
            string wsUrl = ChannelProtocol.Url(rest.Url);
            var proxy = PyProxies.ForChannel(environment, wsUrl);
            var ws = new ClientWebSocket();
#if NET9_0_OR_GREATER
            ws.Options.KeepAliveInterval = keepAlive;
            ws.Options.KeepAliveTimeout = keepAlive;
#else
            // the net8.0 build cannot ping (see ChannelProtocol.Pings); no unsolicited pongs either
            ws.Options.KeepAliveInterval = TimeSpan.Zero;
#endif
            ws.Options.CollectHttpResponseDetails = true;
            // as websockets (proxy=True): a CONNECT tunnel through the proxy the environment names for the channel's URL
            ws.Options.Proxy = proxy?.WebProxy();
            // DangerousDeflateOptions is never set: no compression (contract/socket.md, "Limits")
            // The token also goes on the handshake as Authorization: Bearer. The Jeff server ignores it there (it checks
            // the auth message sent after hello), but a proxy in front of the server may open the WebSocket only for a
            // request that carries credentials.
            if (token is not null) ws.Options.SetRequestHeader("Authorization", $"Bearer {await CurrentTokenAsync(token, rest.Url, cancellationToken).ConfigureAwait(false)}");
            transport = new ChannelTransport(rest, ws, wsUrl, token, timeout, keepAlive);
        }
        catch
        {
            await rest.DisposeAsync().ConfigureAwait(false);
            throw;
        }
        try
        {
            await transport.ConnectAsync(cancellationToken).ConfigureAwait(false);
            transport.StartReading();
            await transport.HandshakeAsync(cancellationToken).ConfigureAwait(false);
            double? expires = await transport.AuthenticateAsync(cancellationToken).ConfigureAwait(false);
            if (expires is { } exp) transport.refresher = Task.Run(() => transport.RefreshLoopAsync(exp));
        }
        catch
        {
            await transport.DisposeAsync().ConfigureAwait(false);
            throw;
        }
        return transport;
    }

    private static async Task<string> CurrentTokenAsync(TokenSource source, string url, CancellationToken cancellationToken) =>
        await Tokens.ReadAsync(source, url, cancellationToken).ConfigureAwait(false)
        ?? throw new InvalidOperationException("Tokens.ReadAsync gives null only for no token source");

    /// <summary>Marks a task nobody will await again (a reply waiter whose caller gave up, a send cut off by the stall
    /// watch) as observed, so a failure set on it later is not reported as an unobserved task exception: its caller
    /// already has its own error.</summary>
    private static void Observe(Task task) => task.ContinueWith(static done => _ = done.Exception, CancellationToken.None,
        TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);

    private static TimeSpan Left(long started, TimeSpan budget)
    {
        var left = budget - Stopwatch.GetElapsedTime(started);
        return left > TimeSpan.Zero ? left : TimeSpan.Zero;
    }

    // ---- opening

    /// <summary>Opens the WebSocket, at most Timeout for the whole of it (TCP, TLS and the upgrade).</summary>
    private async Task ConnectAsync(CancellationToken cancellationToken)
    {
        using var limit = new CancellationTokenSource(Timeout);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(limit.Token, cancellationToken);
        try
        {
            await ws.ConnectAsync(new Uri(WsUrl), linked.Token).ConfigureAwait(false);
        }
        catch (Exception error) when (error is OperationCanceledException or WebSocketException && !cancellationToken.IsCancellationRequested
            && limit.IsCancellationRequested)
        {
            throw new BackendUnavailable($"Cannot reach the Jeff server at {Url}: opening {WsUrl} took more than {PyFormat.Millis(Timeout)} ms");
        }
        catch (WebSocketException error)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (ws.HttpStatusCode != 0 && ws.HttpStatusCode != HttpStatusCode.SwitchingProtocols)
            {
                throw new EngineError($"The Jeff server at {Url} refused to open the channel {WsUrl}: HTTP status {(int)ws.HttpStatusCode}");
            }
            string cause = error.InnerException is { } inner ? $" ({Errors.Described(inner)})" : "";
            throw new BackendUnavailable($"Cannot reach the Jeff server at {Url} ({WsUrl}): {Errors.Described(error)}{cause}");
        }
    }

    /// <summary>Sends hello and reads the server's: at most Timeout for the turn to write and Timeout for the reply once
    /// the hello is written.</summary>
    private async Task HandshakeAsync(CancellationToken cancellationToken)
    {
        var hello = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (gate) greeting = hello;
        JsonObject message;
        try
        {
            var written = await StartWriteAsync(Protocol.EncodeFrame(Protocol.Hello(), "hello"), Timeout, cancellationToken).ConfigureAwait(false)
                ?? throw new TimeoutException();
            // when the channel ended before the hello was written, the hello waiting has already failed with why
            message = await (await written.WaitAsync(cancellationToken).ConfigureAwait(false) ? hello.Task.WaitAsync(Timeout, cancellationToken) : hello.Task)
                .ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            throw new BackendUnavailable($"No hello from the channel {WsUrl} within {PyFormat.Millis(Timeout)} ms");
        }
        finally
        {
            lock (gate) greeting = null;
            Observe(hello.Task);
        }
        if (Json.IsFalse(message["ok"])) throw Remote.WireError(JsonRead.Object(message, "error", "the channel's hello failure"), $"the channel {WsUrl} sent");
        Protocol.CheckHello(message);
        Protocol.ValidateMessage(message, $"the hello of {WsUrl}", "hello");
    }

    /// <summary>Sends auth when there is a token; the token's exp when it is a function's JWT (to refresh), else null.</summary>
    private async Task<double?> AuthenticateAsync(CancellationToken cancellationToken)
    {
        if (token is null) return null;
        string current = await CurrentTokenAsync(token, Url, cancellationToken).ConfigureAwait(false);
        await RequestAsync(new JsonObject { ["op"] = "auth", ["token"] = current }, Protocol.PreauthMaxBody, cancellationToken).ConfigureAwait(false);
        return token.IsFunction ? ChannelProtocol.TokenExpiry(current) : null;
    }

    // ---- reading

    private void StartReading()
    {
        Volatile.Write(ref readerStarted, 1);
        _ = Task.Run(async () =>
        {
            try
            {
                await ReceiveAsync().ConfigureAwait(false);
            }
            finally
            {
                readerEnded.TrySetResult();
            }
        });
    }

    /// <summary>Reads until the socket ends. Every way the reading can fail ends the channel with an error naming it.</summary>
    private async Task ReceiveAsync()
    {
        var buffer = new byte[64 * 1024];
        using var message = new MemoryStream();
        bool discarding = false; // the rest of a message over MaxMessage, read only so the closing handshake can finish
        try
        {
            while (true)
            {
                ValueWebSocketReceiveResult result;
                try
                {
                    result = await ws.ReceiveAsync(buffer.AsMemory(), CancellationToken.None).ConfigureAwait(false);
                }
                catch (Exception error) when (error is WebSocketException or IOException or ObjectDisposedException or OperationCanceledException)
                {
                    ChannelClosed(How(error));
                    return;
                }
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    string reason = ws.CloseStatusDescription is { Length: > 0 } text ? $": {text}" : "";
                    ChannelClosed($"WebSocket close code {(int?)ws.CloseStatus}{reason}");
                    return;
                }
                if (!discarding && message.Length + result.Count > MaxMessage)
                {
                    // as the Python SDK's websockets (max_size): the connection fails with close code 1009
                    discarding = true;
                    message.SetLength(0);
                    lock (gate) closeStatus = WebSocketCloseStatus.MessageTooBig;
                    Shutdown(new BackendUnavailable($"{WsUrl}: the channel closed (the server sent a WebSocket message of more than {MaxMessage} bytes, "
                        + "more than one frame of at most 64 MiB)"));
                }
                if (!discarding) message.Write(buffer, 0, result.Count);
                if (!result.EndOfMessage) continue;
                if (discarding)
                {
                    discarding = false;
                    continue;
                }
                var data = message.ToArray();
                message.SetLength(0);
                Dispatch(data, result.MessageType == WebSocketMessageType.Binary);
            }
        }
        catch (Exception failure)
        {
            // never an unobserved task failure: the channel ends with an error naming what went wrong
            Shutdown(failure as JeffError ?? new EngineError($"The channel {WsUrl} failed: {Errors.Described(failure)}"));
        }
    }

    private string How(Exception error) =>
        error is WebSocketException { WebSocketErrorCode: WebSocketError.ConnectionClosedPrematurely } ? "no close frame received"
        : MissedPong(error) ? $"the server sent no pong to a keepalive ping within {PyFormat.Millis(keepAlive)} ms"
        // .NET refuses a frame RFC 6455 does not allow (a close code outside 1000-1003, 1007-1014 and 3000-4999, a bad
        // opcode, a control frame in pieces) and fails the connection, as websockets does
        : error is WebSocketException { WebSocketErrorCode: WebSocketError.Faulted }
            ? $"the server sent a WebSocket frame that RFC 6455 does not allow, such as a close code outside 1000-1003, 1007-1014 and 3000-4999: {Errors.Described(error)}"
        : Errors.Described(error);

    /// <summary>Whether the socket was aborted because a keepalive ping got no pong: .NET then cancels the pending
    /// receive (OperationCanceledException "Aborted") with the heartbeat's WebSocketException (Faulted) inside. A
    /// protocol error the server makes is a WebSocketException thrown by the receive itself, not inside a cancellation;
    /// this side never cancels a receive (it passes no token) and aborts only after the channel has ended.</summary>
    private static bool MissedPong(Exception error)
    {
        if (error is not OperationCanceledException) return false;
        var pending = new Stack<Exception>();
        if (error.InnerException is { } inner) pending.Push(inner);
        while (pending.Count > 0)
        {
            var next = pending.Pop();
            if (next is WebSocketException { WebSocketErrorCode: WebSocketError.Faulted }) return true;
            if (next is AggregateException several) foreach (var item in several.InnerExceptions) pending.Push(item);
            else if (next.InnerException is { } deeper) pending.Push(deeper);
        }
        return false;
    }

    /// <summary>The socket ended (closed by the server, dropped, or aborted here): the hello waiting fails, then every
    /// request.</summary>
    private void ChannelClosed(string how)
    {
        TaskCompletionSource<JsonObject>? hello;
        lock (gate)
        {
            hello = greeting;
            greeting = null;
        }
        hello?.TrySetException(new BackendUnavailable($"{WsUrl}: the channel closed during the hello ({how})"));
        Shutdown(new BackendUnavailable($"{WsUrl}: the channel closed ({how})"));
    }

    /// <summary>The checked message one WebSocket message carries (exactly one frame).</summary>
    private JsonObject MessageOf(byte[] data, bool binary)
    {
        if (!binary) throw new EngineError($"The channel {WsUrl} sent a text message; it carries binary frames only");
        if (data.Length < Protocol.HeaderSize) throw new EngineError($"The channel {WsUrl} sent a message of {data.Length} bytes, shorter than a frame header");
        uint length = BinaryPrimitives.ReadUInt32BigEndian(data);
        if (length != data.Length - Protocol.HeaderSize)
        {
            throw new EngineError($"The channel {WsUrl} sent a message that is not exactly one frame: its header announces {length} bytes and the message "
                + $"carries {data.Length - Protocol.HeaderSize}");
        }
        try
        {
            return Protocol.DecodeBody(data.AsSpan(Protocol.HeaderSize));
        }
        catch (InvalidRequest error)
        {
            throw new EngineError($"The channel {WsUrl} sent a frame that does not match the socket schema: {error.Message}");
        }
    }

    private void Dispatch(byte[] data, bool binary)
    {
        TaskCompletionSource<JsonObject>? hello;
        lock (gate)
        {
            hello = greeting;
            greeting = null;
        }
        if (hello is not null)
        {
            try
            {
                hello.TrySetResult(MessageOf(data, binary));
            }
            catch (JeffError error)
            {
                hello.TrySetException(error);
            }
            return;
        }
        if (Closed) return; // read only to finish the closing handshake: every request has already failed
        OnMessage(data, binary);
    }

    private void OnMessage(byte[] data, bool binary)
    {
        JsonObject message;
        try
        {
            message = MessageOf(data, binary);
        }
        catch (JeffError error)
        {
            Shutdown(error);
            return;
        }
        if (!message.ContainsKey("ok"))
        {
            Shutdown(new EngineError($"The channel {WsUrl} sent a message that is not a reply (op {PyFormat.Repr(message["op"])}); the channel is closed"));
            return;
        }
        long id = JsonRead.Long(message, "id", $"a reply of {WsUrl}");
        if (id == 0 && Json.IsFalse(message["ok"]))
        {
            // A failure of the whole connection (the token expired, a refresh for another caller, a frame the server
            // could not read): the server closes the channel after it.
            Shutdown(Remote.WireError(JsonRead.Object(message, "error", "a channel failure"), $"the channel {WsUrl} sent"));
            return;
        }
        TaskCompletionSource<JsonObject>? waiting;
        bool releaseReply = false, lateReply = false;
        lock (gate)
        {
            if (!pending.Remove(id, out waiting))
            {
                releaseReply = unawaited.Remove(id);
                lateReply = !releaseReply && abandoned.Remove(id);
            }
        }
        if (waiting is not null)
        {
            waiting.TrySetResult(message);
            return;
        }
        if (releaseReply)
        {
            if (!Json.IsTrue(message["ok"]))
            {
                var error = JsonRead.Object(message, "error", "a release failure");
                Console.Error.WriteLine($"Releasing the handle of a garbage-collected bound function on {WsUrl} failed: {error["type"]}: {error["message"]}");
            }
            return;
        }
        if (lateReply) return; // a late reply to a request whose caller got DeadlineExceeded or cancelled: dropped
        // A reply to an id this side never sent, or one already answered: the two sides disagree about what is
        // outstanding, so no later reply can be trusted to be matched right.
        Shutdown(new EngineError($"The channel {WsUrl} sent a reply for id {id}, which no request is waiting for; the channel is closed"));
    }

    /// <summary>Ends the channel once: the first error wins (with every refresh failure as a note); every waiting request
    /// fails with its own copy of it, every request waiting for its turn to write is woken, and the closing handshake
    /// starts.</summary>
    private void Shutdown(JeffError error)
    {
        List<TaskCompletionSource<JsonObject>> waiting;
        TaskCompletionSource<JsonObject>? hello;
        JeffError reason;
        lock (gate)
        {
            if (closedError is null)
            {
                error.Notes.AddRange(refreshFailures);
                Volatile.Write(ref closedError, error);
            }
            reason = closedError!;
            hello = greeting;
            greeting = null;
            waiting = pending.Values.ToList();
            pending.Clear();
            unawaited.Clear();
            abandoned.Clear();
            closing ??= Task.Run(EndSocketAsync);
        }
        hello?.TrySetException(Errors.Copy(reason));
        foreach (var call in waiting) call.TrySetException(Errors.Copy(reason));
        stopping.Cancel();
    }

    /// <summary>The closing handshake, at most Timeout in all: the turn to write (the frame being written is finished
    /// first, never cut in half), the close frame, the server's close frame; then the socket is aborted, which also ends
    /// a write still stuck. Never throws: what kept the handshake from finishing is kept in CloseProblem.</summary>
    private async Task EndSocketAsync()
    {
        long started = Stopwatch.GetTimestamp();
        try
        {
            if (ws.State is WebSocketState.Open or WebSocketState.CloseReceived)
            {
                if (!await writing.WaitAsync(Left(started, Timeout)).ConfigureAwait(false))
                {
                    CloseProblem = $"a frame was still being written after {PyFormat.Millis(Timeout)} ms; the socket was aborted without a closing handshake";
                }
                else if (ws.State is not (WebSocketState.Open or WebSocketState.CloseReceived))
                {
                    // the write that held the turn ended the socket (a stall, a broken connection): the channel's own
                    // error says why, and there is no handshake left to make
                    writing.Release();
                    CloseProblem = $"the socket had already ended ({ws.State}) when the closing handshake could start: {closedError?.Message}";
                }
                else
                {
                    Task closed;
                    try
                    {
                        WebSocketCloseStatus status;
                        lock (gate) status = closeStatus;
                        closed = ws.CloseOutputAsync(status, null, CancellationToken.None);
                        try
                        {
                            await closed.WaitAsync(Left(started, Timeout)).ConfigureAwait(false);
                        }
                        catch (TimeoutException)
                        {
                            Observe(closed);
                            throw;
                        }
                    }
                    finally
                    {
                        writing.Release();
                    }
                    await ReaderEnded.WaitAsync(Left(started, Timeout)).ConfigureAwait(false);
                }
            }
        }
        catch (TimeoutException)
        {
            CloseProblem = $"the closing handshake did not finish within {PyFormat.Millis(Timeout)} ms; the socket was aborted";
        }
        catch (Exception problem) when (problem is WebSocketException or IOException or ObjectDisposedException or InvalidOperationException)
        {
            // The channel already ended with its own error, which every caller got; a socket already broken leaves only
            // the socket to abort, recorded here.
            CloseProblem = Errors.Described(problem);
        }
        ws.Abort();
        await ReaderEnded.ConfigureAwait(false);
        await Volatile.Read(ref currentWrite).ConfigureAwait(false);
        ws.Dispose();
    }

    private Task ReaderEnded => Volatile.Read(ref readerStarted) == 1 ? readerEnded.Task : Task.CompletedTask;

    // ---- writing

    private (long Id, byte[] Frame) Register(JsonObject message, int limit)
    {
        long id = Interlocked.Increment(ref nextId);
        var framed = (JsonObject)Json.Canonical(message)!;
        framed["id"] = id;
        var frame = Protocol.EncodeFrame(framed);
        int size = frame.Length - Protocol.HeaderSize;
        if (size > limit)
        {
            throw new InvalidRequest($"A {PyFormat.Repr(message["op"])} frame of {size} bytes is over the channel's limit of {limit} bytes"
                + (limit == Protocol.PreauthMaxBody ? " before auth" : " (50 MiB)"));
        }
        return (id, frame);
    }

    /// <summary>Waits for the turn to write, at most `wait` and until cancelled or the channel closes (then the channel's
    /// error); then starts writing the releases ReleaseLater queued and `frame`, in a task of its own, and returns a task
    /// that ends when `frame` has been written whole (true) or the channel ended first (false). Null: the turn did not
    /// come within `wait`, and nothing was sent.</summary>
    private async Task<Task<bool>?> StartWriteAsync(byte[] frame, TimeSpan wait, CancellationToken cancellationToken)
    {
        bool turn;
        using (var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, stopping.Token))
        {
            try
            {
                turn = await writing.WaitAsync(wait, linked.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                throw Errors.Copy(Volatile.Read(ref closedError)!); // stopping is cancelled only after closedError is set
            }
        }
        if (!turn) return null;
        var frames = new List<byte[]>();
        JeffError? closed;
        lock (gate)
        {
            closed = closedError;
            while (closed is null && releases.TryDequeue(out long handle))
            {
                var (id, release) = Register(new JsonObject { ["op"] = "release", ["handle"] = handle }, Protocol.MaxBody);
                unawaited.Add(id);
                frames.Add(release);
            }
        }
        if (closed is not null)
        {
            writing.Release();
            throw Errors.Copy(closed);
        }
        frames.Add(frame);
        var written = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        Volatile.Write(ref currentWrite, WriteAsync(frames, written));
        return written.Task;
    }

    /// <summary>Writes `frames` in order, each as one binary WebSocket message in pieces of at most SendChunk bytes, then
    /// gives the turn back; `written` is true once the last frame is written whole, false when the channel ended first.
    /// Never faults: a failure, or a piece not accepted within Timeout, ends the channel. No deadline bounds the whole
    /// write: a large frame on a slow but working link takes as long as it takes, as in the Python SDK.</summary>
    private async Task WriteAsync(List<byte[]> frames, TaskCompletionSource<bool> written)
    {
        try
        {
            foreach (var frame in frames)
            {
                for (int offset = 0; offset < frame.Length; offset += SendChunk)
                {
                    int size = Math.Min(SendChunk, frame.Length - offset);
                    var sent = ws.SendAsync(frame.AsMemory(offset, size), WebSocketMessageType.Binary, offset + size == frame.Length, CancellationToken.None).AsTask();
                    try
                    {
                        await sent.WaitAsync(Timeout).ConfigureAwait(false);
                    }
                    catch (TimeoutException)
                    {
                        Observe(sent);
                        // the error first, then the abort: the reader, woken by the abort, finds the channel already closed
                        Shutdown(new BackendUnavailable($"{WsUrl}: the socket accepted no bytes of a frame for {PyFormat.Millis(Timeout)} ms (the server or the "
                            + "network stopped taking data); the channel closed"));
                        ws.Abort();
                        return;
                    }
                }
            }
            written.TrySetResult(true);
        }
        catch (Exception error)
        {
            // A send fails when the socket has already ended; the reader then reports why (the server's close frame, a
            // frame that breaks the protocol, a missing pong), which says more than the send's "invalid state".
            if (Volatile.Read(ref readerStarted) == 1 && ws.State is WebSocketState.Closed or WebSocketState.Aborted or WebSocketState.CloseReceived)
            {
                await readerEnded.Task.WaitAsync(Timeout).ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
            }
            Shutdown(new BackendUnavailable($"{WsUrl}: the channel closed ({How(error)})"));
        }
        finally
        {
            written.TrySetResult(false); // the channel ended before the frame was whole (Shutdown ran first)
            writing.Release();
        }
    }

    // ---- requests

    /// <summary>Sends one request and waits for its reply; its result, or the error its failure names. As in the Python
    /// SDK, Timeout bounds the wait for the reply, which starts once the frame is written whole; the write itself is
    /// bounded by the stall watch (each piece accepted within Timeout), and the wait for the turn to write by Timeout of
    /// its own. Cancellation is honoured at every step. A request given up before its turn came was never sent; one
    /// given up after has its late reply dropped.</summary>
    private async Task<JsonObject> RequestAsync(JsonObject message, int limit, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var (id, frame) = Register(message, limit);
        string op = message["op"]!.GetValue<string>();
        var waiter = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (gate)
        {
            if (closedError is not null) throw Errors.Copy(closedError);
            pending[id] = waiter;
        }
        bool sent = false;
        JsonObject reply;
        try
        {
            var written = await StartWriteAsync(frame, Timeout, cancellationToken).ConfigureAwait(false);
            if (written is null)
            {
                GiveUp(id, sent: false, waiter);
                throw new DeadlineExceeded($"The channel {WsUrl} could not send {PyFormat.Str(op)} within {PyFormat.Millis(Timeout)} ms: the frames before it "
                    + "were still being written; it was not sent");
            }
            sent = true; // its first byte may be on the wire: a reply may come even if this caller stops waiting
            if (!await written.WaitAsync(cancellationToken).ConfigureAwait(false))
            {
                GiveUp(id, sent, waiter);
                throw Errors.Copy(Volatile.Read(ref closedError)!); // the write ended the channel, or the channel ended it
            }
            reply = await waiter.Task.WaitAsync(Timeout, cancellationToken).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            GiveUp(id, sent, waiter);
            throw new DeadlineExceeded($"The channel {WsUrl} did not answer {PyFormat.Str(op)} within {PyFormat.Millis(Timeout)} ms; the server may still have run "
                + "the request");
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            GiveUp(id, sent, waiter);
            throw;
        }
        catch (JeffError) when (!sent)
        {
            GiveUp(id, sent: false, waiter); // the channel closed while this request waited for its turn
            throw;
        }
        if (!Json.IsTrue(reply["ok"])) throw Remote.WireError(JsonRead.Object(reply, "error", "a channel failure"), $"the channel {WsUrl} sent");
        return JsonRead.Object(reply, "result", $"a reply of {WsUrl}");
    }

    /// <summary>The caller stopped waiting for request `id`: a reply that comes later is dropped if it was sent.</summary>
    private void GiveUp(long id, bool sent, TaskCompletionSource<JsonObject> waiter)
    {
        lock (gate)
        {
            if (pending.Remove(id) && sent) abandoned.Add(id);
        }
        Observe(waiter.Task);
    }

    /// <summary>A request answered as /v1/decide answers: its result checked against decide_response, holding `key`.</summary>
    private async Task<JsonNode?> DecideAnswerAsync(JsonObject message, string key, CancellationToken cancellationToken)
    {
        var result = await RequestAsync(message, Protocol.MaxBody, cancellationToken).ConfigureAwait(false);
        bool valid = Contract.IsValid("decide_response", result);
        if (!valid || !result.ContainsKey(key))
        {
            string problem = valid ? PyFormat.Repr(PyFormat.Sorted(result.Select(pair => pair.Key))) : Contract.FirstProblem("decide_response", result);
            throw new ContractError($"The answer of {WsUrl} to {PyFormat.Repr(message["op"])} does not match contract/schemas/decide_response with "
                + $"{PyFormat.Str(key)}: {problem}");
        }
        return result[key];
    }

    private static JsonObject WithOp(string op, JsonObject body)
    {
        var message = new JsonObject { ["op"] = op };
        foreach (var (key, value) in body) message[key] = Json.Canonical(value);
        return message;
    }

    public async Task<JsonArray> DecideCallsAsync(JsonArray calls, JsonNode policy, CancellationToken cancellationToken)
    {
        var decisions = (await DecideAnswerAsync(new JsonObject { ["op"] = "decide", ["calls"] = Json.Canonical(calls), ["on_low_confidence"] = Json.Canonical(policy) },
            "decisions", cancellationToken).ConfigureAwait(false))!.AsArray();
        if (decisions.Count != calls.Count) throw new ContractError($"{WsUrl} answered {decisions.Count} decisions for {calls.Count} calls");
        return decisions;
    }

    public async Task<JsonObject> DispatchAsync(JsonObject body, CancellationToken cancellationToken) =>
        (await DecideAnswerAsync(WithOp("dispatch", body), "decision", cancellationToken).ConfigureAwait(false))!.AsObject();

    public async Task<JsonObject> GraphAsync(JsonObject body, CancellationToken cancellationToken) =>
        (await DecideAnswerAsync(WithOp("graph", body), "graph_result", cancellationToken).ConfigureAwait(false))!.AsObject();

    public Task<JsonObject> FunctionsAsync(CancellationToken cancellationToken) => rest.FunctionsAsync(cancellationToken);

    // ---- handles

    /// <summary>Binds `function` (its full ID) with this context, labels and policy (wire forms); the handle's number and
    /// the version of the function's adapter the server serves.</summary>
    public async Task<(long Handle, string Version)> BindAsync(string function, JsonObject? context, JsonNode? labels, JsonNode policy, CancellationToken cancellationToken)
    {
        var message = new JsonObject { ["op"] = "bind", ["function"] = function, ["on_low_confidence"] = Json.Canonical(policy) };
        if (context is not null) message["context"] = Json.Canonical(context);
        if (labels is not null) message["labels"] = Json.Canonical(labels);
        var result = await RequestAsync(message, Protocol.MaxBody, cancellationToken).ConfigureAwait(false);
        // a handle past a long (Python accepts any int) is refused by name, as every other count this SDK reads
        if (!Json.TryLong(result["handle"], out long handle) || handle < 1 || Json.KindOf(result["function"]) != JsonKind.String
            || result["function"]!.GetValue<string>() != function || Json.KindOf(result["version"]) != JsonKind.String)
        {
            throw new ContractError($"{WsUrl} answered bind {PyFormat.Str(function)} with {PyFormat.Repr(result)}; expected a handle (a whole number from 1) "
                + $"for {PyFormat.Str(function)} and its adapter's version");
        }
        return (handle, result["version"]!.GetValue<string>());
    }

    public async Task<JsonObject> CallAsync(long handle, JsonNode? input, CancellationToken cancellationToken) =>
        (await DecideAnswerAsync(new JsonObject { ["op"] = "call", ["handle"] = handle, ["input"] = Json.Canonical(input) }, "decision", cancellationToken)
            .ConfigureAwait(false))!.AsObject();

    public async Task<JsonArray> CallManyAsync(long handle, IReadOnlyList<JsonNode?> inputs, CancellationToken cancellationToken)
    {
        var message = new JsonObject
        {
            ["op"] = "call_many", ["handle"] = handle, ["inputs"] = new JsonArray(inputs.Select(input => Json.Canonical(input)).ToArray()),
        };
        var decisions = (await DecideAnswerAsync(message, "decisions", cancellationToken).ConfigureAwait(false))!.AsArray();
        if (decisions.Count != inputs.Count) throw new ContractError($"{WsUrl} answered {decisions.Count} decisions for {inputs.Count} inputs");
        return decisions;
    }

    public Task ReleaseAsync(long handle, CancellationToken cancellationToken) =>
        RequestAsync(new JsonObject { ["op"] = "release", ["handle"] = handle }, Protocol.MaxBody, cancellationToken);

    /// <summary>Queues a release, sent before the next request. It sends nothing itself and never throws, so a finalizer
    /// may call it. Nothing to do once the channel is closed: its handles ended with it.</summary>
    public void ReleaseLater(long handle)
    {
        if (!Closed) releases.Enqueue(handle);
    }

    // ---- refreshing

    /// <summary>Waits `wait`; true when the channel closed meanwhile.</summary>
    private async Task<bool> WaitAsync(TimeSpan wait)
    {
        while (true)
        {
            if (stopping.IsCancellationRequested) return true;
            var chunk = wait > Timeouts.Longest ? Timeouts.Longest : wait;
            try
            {
                await Task.Delay(chunk, stopping.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return true; // stopping is cancelled exactly when the channel closes
            }
            if (wait <= chunk) return false;
            wait -= chunk;
        }
    }

    /// <summary>The time from now until `unixSeconds` (negative once it has passed), held within TimeSpan's range.</summary>
    private static TimeSpan Until(double unixSeconds)
    {
        double ms = unixSeconds * 1000 - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        if (ms >= TimeSpan.MaxValue.TotalMilliseconds) return TimeSpan.MaxValue;
        if (ms <= TimeSpan.MinValue.TotalMilliseconds) return TimeSpan.MinValue;
        return TimeSpan.FromMilliseconds(ms);
    }

    /// <summary>One refresh attempt: the function's token, sent with `refresh` when it expires after `expires`; its exp.</summary>
    private async Task<double> RefreshOnceAsync(double expires)
    {
        string current = await CurrentTokenAsync(token!, Url, stopping.Token).ConfigureAwait(false);
        double? renewed = ChannelProtocol.TokenExpiry(current);
        if (renewed is null || renewed <= expires)
        {
            throw new Unauthorised($"the token function returned a token expiring at {(renewed is null ? "None" : PyFormat.Number(renewed.Value))}, not after the "
                + $"channel's current token (exp {PyFormat.Number(expires)})");
        }
        await RequestAsync(new JsonObject { ["op"] = "refresh", ["token"] = current }, Protocol.MaxBody, stopping.Token).ConfigureAwait(false);
        return renewed.Value;
    }

    /// <summary>From RefreshBefore before each token's exp (and at least RefreshRetry after the last successful refresh,
    /// so a function minting short-lived tokens is not called in a tight loop), asks the token function for a token
    /// expiring later and sends `refresh` with it; each failure is kept (and written to standard error) and the function
    /// asked again every RefreshRetry. If exp passes first, the channel closes with Unauthorised carrying every failure
    /// as a note. Never faults.</summary>
    private async Task RefreshLoopAsync(double first)
    {
        double expires = first;
        var earliest = DateTimeOffset.MinValue;
        while (true)
        {
            var wait = new[] { TimeSpan.Zero, Until(expires - ChannelProtocol.RefreshBefore.TotalSeconds), earliest - DateTimeOffset.UtcNow }.Max();
            if (await WaitAsync(wait).ConfigureAwait(false)) return;
            double? renewed = null;
            while (renewed is null)
            {
                try
                {
                    renewed = await RefreshOnceAsync(expires).ConfigureAwait(false);
                }
                catch (Exception error)
                {
                    // every failure of the token function or of the refresh is kept and reported; the loop asks again
                    if (Closed) return;
                    string failure = $"Refreshing the channel's token (exp {PyFormat.Number(expires)}) at "
                        + $"{PyFormat.Fixed(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0, 1)} failed: {Errors.Described(error)}";
                    int count;
                    lock (gate)
                    {
                        refreshFailures.Add(failure);
                        count = refreshFailures.Count;
                    }
                    Console.Error.WriteLine($"{WsUrl}: {failure}");
                    var remaining = Until(expires);
                    if (remaining <= TimeSpan.Zero)
                    {
                        Shutdown(new Unauthorised($"The channel's token expired at {PyFormat.Number(expires)} before the token function returned one expiring "
                            + $"later ({count} attempts failed; see the notes); the channel is closed"));
                        return;
                    }
                    if (await WaitAsync(remaining < ChannelProtocol.RefreshRetry ? remaining : ChannelProtocol.RefreshRetry).ConfigureAwait(false)) return;
                }
            }
            lock (gate) refreshFailures.Clear();
            expires = renewed.Value;
            earliest = DateTimeOffset.UtcNow + ChannelProtocol.RefreshRetry;
        }
    }

    // ---- closing

    /// <summary>Ends the channel (with a closing handshake of at most Timeout) and the HTTP connections, and waits for
    /// the refresher at most Timeout more (a token function it is waiting on is passed a cancelled token; one that
    /// ignores it is left to return on its own, as the Python SDK leaves its thread). Calling it again does nothing
    /// more; it never throws.</summary>
    public async ValueTask DisposeAsync()
    {
        Shutdown(new InvalidRequest($"This remote client of {Url} is closed; connect again with JeffClient.RemoteAsync"));
        await rest.DisposeAsync().ConfigureAwait(false);
        Task ending;
        lock (gate) ending = closing!;
        await ending.ConfigureAwait(false);
        // at most Timeout for the refresher to stop (its own failures are already reported); WaitAsync drops its timer
        // once the refresher ends, where a Task.Delay would keep one for the whole Timeout
        await refresher.WaitAsync(Timeout).ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
    }

    /// <summary>Closes the channel from a finalizer: queued on the thread pool, never throwing.</summary>
    internal void CloseFromFinalizer() => ThreadPool.QueueUserWorkItem(static transport => _ = transport.DisposeAsync().AsTask(), this, preferLocal: false);
}

/// <summary>A bound function that is a handle on the channel: bound here (context, labels, policy checked) and then on
/// the server with one `bind`; each call is one `call` frame, ManyAsync one `call_many`. A policy the server can run is
/// bound with the handle; one with an llm, human or remote step is bound as "return" and run here on each decision.
/// DisposeAsync releases the handle; a bound function dropped without it has its handle released by its finalizer
/// (queued, sent before the channel's next request).</summary>
public sealed class ChannelBoundFunction : RemoteBoundFunction
{
    private readonly ChannelTransport transport;
    private readonly JsonNode? wire;
    private readonly long handle;
    private int released;

    private ChannelBoundFunction(JeffClient client, Call template, Policy policy, Binding bound, ChannelTransport transport, JsonNode? wire, long handle)
        : base(client, template, policy, bound)
    {
        this.transport = transport;
        this.wire = wire;
        this.handle = handle;
    }

    /// <summary>Queues the handle's release, sent before the channel's next request, when the function was dropped without DisposeAsync.</summary>
    ~ChannelBoundFunction()
    {
        if (Volatile.Read(ref released) == 0) transport.ReleaseLater(handle);
    }

    internal static async Task<ChannelBoundFunction> CreateAsync(ChannelJeffClient client, Call template, Policy policy, CancellationToken cancellationToken)
    {
        var bound = await client.BindCallAsync(template, cancellationToken).ConfigureAwait(false);
        await Escalation.CheckPolicyAsync(client, bound.Function.Interface, policy, cancellationToken).ConfigureAwait(false);
        var wire = client.WirePolicy(policy);
        var transport = client.Channel;
        // a policy with a step only this process can run is bound as "return", and run here on each decision
        var sent = wire ?? JsonValue.Create("return");
        var (handle, version) = await transport.BindAsync(bound.Function.Id, template.Context, template.Labels, sent, cancellationToken).ConfigureAwait(false);
        if (version != bound.Manifest.Version)
        {
            // the adapter changed on the server since connecting: the handle is released before the next request
            transport.ReleaseLater(handle);
            throw Remote.VersionMismatch(bound.Manifest.Id, version, bound.Manifest.Version);
        }
        return new ChannelBoundFunction(client, template, policy, bound, transport, wire, handle);
    }

    private long Live() => Volatile.Read(ref released) != 0
        ? throw new InvalidRequest($"This bound function of {Bound.Function.Id} is closed; bind it again with FunctionAsync")
        : handle;

    /// <summary>Decides one input with one call frame on the handle. InvalidRequest once the bound function is disposed;
    /// BackendUnavailable when the channel has closed; DeadlineExceeded when no reply comes within the timeout.</summary>
    public override async Task<Decision> CallAsync(JsonNode? input, CancellationToken cancellationToken = default)
    {
        var decision = Decision.FromJson(await transport.CallAsync(Live(), input, cancellationToken).ConfigureAwait(false));
        if (wire is not null) return decision;
        return await Client.ApplyPolicyAsync(CallOf(input), decision, Policy, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>Decides every input with one call_many frame on the handle (InvalidRequest past 256 inputs); the decisions come back
    /// in input order.</summary>
    public override async Task<IReadOnlyList<Decision>> ManyAsync(IReadOnlyList<JsonNode?> inputs, CancellationToken cancellationToken = default)
    {
        long live = Live();
        if (inputs.Count == 0) return [];
        Remote.CheckBatch(inputs.Count, "inputs");
        var decisions = (await transport.CallManyAsync(live, inputs, cancellationToken).ConfigureAwait(false)).Select(item => Decision.FromJson(item!.AsObject())).ToList();
        if (wire is not null) return decisions;
        var answered = new List<Decision>(decisions.Count);
        for (int index = 0; index < inputs.Count; index++)
        {
            answered.Add(await Client.ApplyPolicyAsync(CallOf(inputs[index]), decisions[index], Policy, cancellationToken).ConfigureAwait(false));
        }
        return answered;
    }

    /// <summary>Releases the handle (waiting for the server's reply). Nothing to release once the channel is closed.</summary>
    public override async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref released, 1) != 0) return;
        GC.SuppressFinalize(this);
        if (!transport.Closed) await transport.ReleaseAsync(handle, default).ConfigureAwait(false);
    }
}

/// <summary>A RemoteJeffClient over the channel: everything as RemoteJeffClient, with bound functions as handles. A
/// client dropped without DisposeAsync closes its channel when the garbage collector takes it (the transport and its
/// tasks hold no reference to the client).</summary>
public sealed class ChannelJeffClient : RemoteJeffClient
{
    private ChannelJeffClient(JeffParts parts, ChannelTransport transport, string url) : base(parts, transport, url) => Channel = transport;

    /// <summary>Closes the channel when the client was dropped without DisposeAsync.</summary>
    ~ChannelJeffClient() => Channel.CloseFromFinalizer();

    internal ChannelTransport Channel { get; }

    internal static async Task<ChannelJeffClient> OpenAsync(string url, TokenSource? token, TimeSpan timeout, Catalogue catalogue,
        IReadOnlyList<KeyValuePair<string, string>> environment, CancellationToken cancellationToken)
    {
        var transport = await ChannelTransport.OpenAsync(url, token, timeout, environment, cancellationToken).ConfigureAwait(false);
        try
        {
            var served = await ServedFunctions.ReadAsync(transport, catalogue, transport.Url, cancellationToken).ConfigureAwait(false);
            return new ChannelJeffClient(await PartsAsync(served, catalogue, cancellationToken).ConfigureAwait(false), transport, transport.Url);
        }
        catch
        {
            await transport.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    /// <summary>Binds `reference` here (context, labels and policy checked) and then on the server with one bind frame; the bound
    /// function is a handle there, released by DisposeAsync. ContractError when the server serves another version of the
    /// adapter than this client's catalogue; TooManyHandles when the channel holds the most handles it may.</summary>
    public override async Task<BoundFunction> FunctionAsync(string reference, CallOptions? options = null, CancellationToken cancellationToken = default) =>
        await ChannelBoundFunction.CreateAsync(this, new Call(reference, null, options?.Context, options?.Labels), options?.OnLowConfidence ?? Policy.Return,
            cancellationToken).ConfigureAwait(false);

    /// <summary>Closes the channel; calling it again does nothing more.</summary>
    public override async ValueTask DisposeAsync()
    {
        GC.SuppressFinalize(this);
        await Channel.DisposeAsync().ConfigureAwait(false);
    }
}
