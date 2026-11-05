using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>The bearer token: a fixed string, or a function returning the current one (called before every HTTP
/// request, so it can refresh). A null TokenSource means a server with no credentials configured.</summary>
public abstract class TokenSource
{
    private protected TokenSource()
    {
    }

    /// <summary>A fixed bearer token, sent with every request.</summary>
    public static TokenSource Of(string token) => new Fixed(token);

    /// <summary>A function returning the current bearer token, called before every HTTP request (and, on the channel, before a
    /// JWT expires) so it can refresh; Unauthorised when it returns null, an empty string or characters a header cannot carry.</summary>
    public static TokenSource From(Func<CancellationToken, ValueTask<string?>> current) => new FromFunction(current);

    /// <summary>A string as a fixed token (TokenSource.Of); null stays null, for a server with no credentials.</summary>
    public static implicit operator TokenSource?(string? token) => token is null ? null : new Fixed(token);

    internal abstract bool IsFunction { get; }

    internal abstract ValueTask<string?> CurrentAsync(CancellationToken cancellationToken);

    /// <summary>Never shows the token.</summary>
    public override string ToString() => IsFunction ? "TokenSource(function)" : "TokenSource(fixed)";

    private sealed class Fixed(string token) : TokenSource
    {
        internal override bool IsFunction => false;

        internal override ValueTask<string?> CurrentAsync(CancellationToken cancellationToken) => ValueTask.FromResult<string?>(token);
    }

    private sealed class FromFunction(Func<CancellationToken, ValueTask<string?>> current) : TokenSource
    {
        internal override bool IsFunction => true;

        internal override ValueTask<string?> CurrentAsync(CancellationToken cancellationToken) => current(cancellationToken);
    }
}

internal static class Tokens
{
    /// <summary>The current token, or null for no credentials. Unauthorised when it is not a non-empty string an HTTP
    /// header can carry; no message ever shows the token.</summary>
    public static async Task<string?> ReadAsync(TokenSource? source, string url, CancellationToken cancellationToken)
    {
        if (source is null) return null;
        string? token = await source.CurrentAsync(cancellationToken).ConfigureAwait(false);
        string what = source.IsFunction ? "the token function returned" : "got";
        if (string.IsNullOrEmpty(token))
        {
            throw new Unauthorised($"The token for {url} must be a non-empty string; {what} {(token is null ? "null" : "an empty string")}");
        }
        if (!token.All(character => character >= 0x20 && character <= 0x7e))
        {
            throw new Unauthorised($"The token for {url} must be printable ASCII, as an HTTP header carries it; {what} a string with other characters (not shown)");
        }
        return token;
    }
}

/// <summary>How RemoteJeffClient reaches the server: one round trip per method, with the JSON shapes of
/// contract/schemas/decide_request and decide_response.</summary>
internal interface ITransport : IAsyncDisposable
{
    string Url { get; }

    /// <summary>The decisions for `calls`, in order, with `policy` (its wire form) applied by the server.</summary>
    Task<JsonArray> DecideCallsAsync(JsonArray calls, JsonNode policy, CancellationToken cancellationToken);

    /// <summary>The routed decision for a {"dispatch": ..., "on_low_confidence": ...} body.</summary>
    Task<JsonObject> DispatchAsync(JsonObject body, CancellationToken cancellationToken);

    /// <summary>The graph result for a {"graph": ..., "input", "params", ...} body.</summary>
    Task<JsonObject> GraphAsync(JsonObject body, CancellationToken cancellationToken);

    /// <summary>The server's GET /v1/functions answer (contract/schemas/functions_response).</summary>
    Task<JsonObject> FunctionsAsync(CancellationToken cancellationToken);
}

/// <summary>The phases of one request, each timed separately: waiting for one of the transport's connection slots
/// (httpx's pool timeout), connecting (connect and TLS), waiting for the answer's headers (sending the request
/// included), and each wait for more of the answer's body.</summary>
internal enum Phase { Connect, Headers, Body, Pool }

/// <summary>The timers of one HTTP request: one precise timer, restarted as the request moves on (connect, then headers,
/// then each wait for more body); when it runs out it records the phase it timed and cancels Token. The source is never
/// disposed: a plain CancellationTokenSource holds nothing to release, so a late Abort never meets a disposed one.</summary>
internal sealed class PhaseTimers : IDisposable
{
    private readonly object sync = new();
    private readonly CancellationTokenSource source = new();
    private readonly Timer timer;
    private long deadline;
    private Phase phase = Phase.Headers;
    private bool done;
    private bool running;
    private bool disposed;
    private Phase? fired;

    public PhaseTimers(TimeSpan timeout)
    {
        Timeout = timeout;
        timer = new Timer(_ => Fire(), null, System.Threading.Timeout.Infinite, System.Threading.Timeout.Infinite);
    }

    public TimeSpan Timeout { get; }

    public CancellationToken Token => source.Token;

    public Phase? Fired
    {
        get
        {
            lock (sync) return fired;
        }
    }

    /// <summary>The phase the request is in (or was in when it stopped): Connect only while this transport's own connect
    /// callback opens a connection for it (and its TLS session, if any).</summary>
    public Phase Current
    {
        get
        {
            lock (sync) return phase;
        }
    }

    public bool Running
    {
        get
        {
            lock (sync) return running;
        }
    }

    /// <summary>Starts timing `next` (a full Timeout from now); does nothing once the request settled, failed or timed out.</summary>
    public void Start(Phase next)
    {
        lock (sync)
        {
            if (done) return;
            phase = next;
            running = true;
            deadline = Stopwatch.GetTimestamp() + (long)(Timeout.TotalSeconds * Stopwatch.Frequency);
            timer.Change(Timeout, System.Threading.Timeout.InfiniteTimeSpan);
        }
    }

    /// <summary>The request's bytes are being written on a connection: it now waits for its answer (each write restarts
    /// that wait, so a slow but steady upload is not cut off).</summary>
    public void Sent()
    {
        bool sending;
        lock (sync) sending = phase is Phase.Connect or Phase.Headers;
        if (sending) Start(Phase.Headers);
    }

    private void Fire()
    {
        lock (sync)
        {
            if (done) return;
            long left = deadline - Stopwatch.GetTimestamp();
            if (left > 0)
            {
                // a callback queued for an earlier due time before Start moved the deadline on, or a timer that ran a
                // little early: wait for the rest
                timer.Change(TimeSpan.FromSeconds((double)left / Stopwatch.Frequency), System.Threading.Timeout.InfiniteTimeSpan);
                return;
            }
            done = true;
            running = false;
            fired = phase;
        }
        // cancelled outside the lock, so the request's own cancellation callbacks never run under it
        source.Cancel();
    }

    /// <summary>Stops the timer: the request settled or failed.</summary>
    public void Finish()
    {
        lock (sync)
        {
            done = true;
            running = false;
            if (!disposed) timer.Change(System.Threading.Timeout.Infinite, System.Threading.Timeout.Infinite);
        }
    }

    /// <summary>Gives the request up now (the transport was closed, or the caller cancelled).</summary>
    public void Abort()
    {
        Finish();
        source.Cancel();
    }

    public void Dispose()
    {
        lock (sync)
        {
            done = true;
            running = false;
            if (disposed) return;
            disposed = true;
            timer.Dispose();
        }
    }
}

/// <summary>Opens the byte stream of one connection to `endpoint` (a test can put a slow one in).</summary>
internal delegate ValueTask<Stream> Connector(DnsEndPoint endpoint, CancellationToken cancellationToken);

/// <summary>The HTTP bytes of one connection, so a request is never sent twice: SocketsHttpHandler sends a request with
/// no body again, on another connection, when its connection ends before the first byte of the answer (it takes the
/// server for one that closed an idle connection). httpx does not, and the server may have run the request, so here a
/// connection that ends while a request waits for its answer fails it with an IOException, which the handler does
/// not retry. An end met while no request waits (an idle connection the server closed) is passed on as an end.</summary>
internal sealed class NoRetryStream(Stream inner, Action sending) : Stream
{
    /// <summary>1 while a request was written and no byte of its answer has arrived.</summary>
    private int awaiting;

    public override bool CanRead => inner.CanRead;

    public override bool CanWrite => inner.CanWrite;

    public override bool CanSeek => false;

    public override long Length => throw new NotSupportedException();

    public override long Position
    {
        get => throw new NotSupportedException();
        set => throw new NotSupportedException();
    }

    private int Received(int count, int asked)
    {
        if (count > 0) Volatile.Write(ref awaiting, 0);
        // a read of no bytes returning 0 says data may be read, not that the connection ended
        else if (asked > 0 && Volatile.Read(ref awaiting) == 1)
        {
            throw new IOException("The server closed the connection without answering; the request is not sent again, as the server may have run it");
        }
        return count;
    }

    /// <summary>Bytes of a request are about to go out: its answer is awaited, and the request (whose code is
    /// writing) is now in its headers phase.</summary>
    private void Writing(int count)
    {
        if (count == 0) return;
        Volatile.Write(ref awaiting, 1);
        sending();
    }

    public override int Read(byte[] buffer, int offset, int count) => Received(inner.Read(buffer, offset, count), count);

    public override int Read(Span<byte> buffer) => Received(inner.Read(buffer), buffer.Length);

    public override async Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) =>
        Received(await inner.ReadAsync(buffer.AsMemory(offset, count), cancellationToken).ConfigureAwait(false), count);

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default) =>
        Received(await inner.ReadAsync(buffer, cancellationToken).ConfigureAwait(false), buffer.Length);

    public override void Write(byte[] buffer, int offset, int count)
    {
        Writing(count);
        inner.Write(buffer, offset, count);
    }

    public override void Write(ReadOnlySpan<byte> buffer)
    {
        Writing(buffer.Length);
        inner.Write(buffer);
    }

    public override Task WriteAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) =>
        WriteAsync(buffer.AsMemory(offset, count), cancellationToken).AsTask();

    public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
    {
        Writing(buffer.Length);
        return inner.WriteAsync(buffer, cancellationToken);
    }

    public override void Flush() => inner.Flush();

    public override Task FlushAsync(CancellationToken cancellationToken) => inner.FlushAsync(cancellationToken);

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();

    public override void SetLength(long value) => throw new NotSupportedException();

    protected override void Dispose(bool disposing)
    {
        if (disposing) inner.Dispose();
        base.Dispose(disposing);
    }

}

/// <summary>A count of slots handed out in arrival order: a waiter that comes later never overtakes one that came
/// earlier, as requests queue in httpcore's pool.</summary>
internal sealed class FifoGate(int slots)
{
    private readonly object sync = new();
    private readonly LinkedList<TaskCompletionSource> waiters = new();
    private int free = slots;

    public async Task WaitAsync(CancellationToken cancellationToken)
    {
        LinkedListNode<TaskCompletionSource> node;
        lock (sync)
        {
            if (free > 0 && waiters.Count == 0)
            {
                free--;
                return;
            }
            node = waiters.AddLast(new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously));
        }
        using var registration = cancellationToken.Register(() =>
        {
            bool removed;
            lock (sync)
            {
                removed = node.List is not null;
                if (removed) waiters.Remove(node);
            }
            if (removed) node.Value.TrySetCanceled(cancellationToken);
        });
        await node.Value.Task.ConfigureAwait(false);
    }

    /// <summary>Gives a slot back: to the waiter that came first, else to the free count.</summary>
    public void Release()
    {
        lock (sync)
        {
            if (waiters.First is { } first)
            {
                waiters.RemoveFirst();
                first.Value.TrySetResult();
                return;
            }
            free++;
        }
    }
}

/// <summary>The checks a server URL passes before anything is sent: http:// or https://, a host, a port (when written)
/// that is a number from 0 to 65535, no user name or password, no query and no fragment. Never a default for a part
/// that is wrong.</summary>
internal static class ServerUrls
{
    public static string Check(string url)
    {
        string scheme = url.StartsWith("https://", StringComparison.Ordinal) ? "https://"
            : url.StartsWith("http://", StringComparison.Ordinal) ? "http://"
            : throw new InvalidRequest($"The server URL {PyFormat.Str(url)} must start with http:// or https://");
        InvalidRequest Invalid(string problem) => new($"The server URL {PyFormat.Str(url)} {problem}");
        string trimmed = url.TrimEnd('/');
        string rest = trimmed.Length > scheme.Length ? trimmed[scheme.Length..] : "";
        if (rest.Contains('#')) throw Invalid("must not have a fragment (the part after '#')");
        if (rest.Contains('?')) throw Invalid("must not have a query (the part after '?')");
        int slash = rest.IndexOf('/');
        string authority = slash < 0 ? rest : rest[..slash];
        if (authority.Contains('@')) throw Invalid("must not carry a user name or password; pass the token instead");
        string host;
        string? port;
        if (authority.StartsWith('['))
        {
            int close = authority.IndexOf(']');
            if (close < 0) throw Invalid("is not a URL");
            host = authority[1..close];
            string after = authority[(close + 1)..];
            if (after.Length > 0 && after[0] != ':') throw Invalid("is not a URL");
            port = after.Length > 0 ? after[1..] : null;
        }
        else
        {
            int colon = authority.IndexOf(':');
            host = colon < 0 ? authority : authority[..colon];
            port = colon < 0 ? null : authority[(colon + 1)..];
        }
        if (host.Length == 0) throw Invalid("has no host");
        // "" (as in http://h:/) is the scheme's default port (RFC 3986); anything else written must be a port number
        if (!string.IsNullOrEmpty(port) && !(port.Length <= 5 && port.All(char.IsAsciiDigit) && int.Parse(port, System.Globalization.CultureInfo.InvariantCulture) <= 65535))
        {
            throw Invalid($"has an invalid port {PyFormat.Str(port)}");
        }
        if (!Uri.TryCreate(trimmed, UriKind.Absolute, out var parsed) || parsed.Host.Length == 0) throw Invalid("is not a URL");
        return trimmed;
    }
}

/// <summary>The client's HTTP/1.1 connection to a Jeff server, for sending POST /v1/decide calls yourself. Most callers
/// want <see cref="JeffClient.RemoteAsync"/> instead, which reads the server's functions and checks them against the
/// catalogue. Every non-2xx answer is thrown as the JeffError its body names, with the answer's x-request-id as the
/// error's RequestId.
///
/// Timeout bounds each phase of a request separately, with one precise timer (PhaseTimers), as httpx's
/// Timeout(timeout_s) has it in the Python SDK: waiting for one of the 100 request slots (httpx's pool timeout; slots
/// are handed out in arrival order), getting a connection (a pooled one, or a new one: connect and TLS), waiting for
/// the answer's headers (from the request's first byte written, so sending it is included), and each wait for more of
/// the answer's body. The connect phase running out is BackendUnavailable "Cannot reach"; the others are
/// DeadlineExceeded, as httpx's PoolTimeout, WriteTimeout and ReadTimeout are in jeff/remote.py.
///
/// One SocketsHttpHandler per transport is its connection pool. With at most 100 requests in flight (the slots) and
/// HTTP/1.1, at most about 100 connections are open (httpx's max_connections=100); the handler itself never queues a
/// request. Idle connections are kept for at most 5 seconds (httpx's keepalive_expiry). Each connection attempt is
/// bounded by Timeout on its own, whichever request it ends up serving. Redirects are not followed: a 3xx is an answer
/// like any other. The proxy variables of the environment (HTTPS_PROXY, HTTP_PROXY, ALL_PROXY, NO_PROXY) are honoured
/// as httpx honours them. Disposing the transport aborts every request in flight with the closed error.</summary>
public sealed class RestTransport : IAsyncDisposable
{
    /// <summary>How long a connection may sit idle in the pool and still be reused (httpx's default keepalive_expiry):
    /// one that a NAT or load balancer dropped without telling either side is not written into.</summary>
    internal static readonly TimeSpan KeepaliveExpiry = TimeSpan.FromSeconds(5);

    /// <summary>The most requests in flight at once, each on its own HTTP/1.1 connection (httpx's default max_connections).</summary>
    internal const int MaxConnections = 100;

    /// <summary>The timers of the request whose code is running: the connection's stream reads it when the request's
    /// first bytes are written, whichever connection the handler gave it.</summary>
    private static readonly AsyncLocal<PhaseTimers?> Sending = new();

    private const int BodyExcerpt = 300;
    private readonly HttpClient client;
    private readonly TokenSource? token;
    private readonly Connector connector;
    private readonly HashSet<PhaseTimers> inFlight = new();
    private readonly FifoGate slots = new(MaxConnections);
    private bool closed;

    /// <summary>A transport to the server at <paramref name="url"/>. Nothing is sent until the first request.</summary>
    /// <param name="url">the server's http:// or https:// address</param>
    /// <param name="token">the bearer token (<see cref="TokenSource.Of"/> or <see cref="TokenSource.From"/>), or null
    /// for a server with no credentials configured</param>
    /// <param name="timeout">the limit of each phase of a request</param>
    public RestTransport(string url, TokenSource? token, TimeSpan timeout) : this(url, token, timeout, ConnectSocketAsync, KeepaliveExpiry)
    {
    }

    internal RestTransport(string url, TokenSource? token, TimeSpan timeout, Connector connector, TimeSpan keepaliveExpiry)
        : this(url, token, timeout, connector, keepaliveExpiry, PyProxies.ProcessEnvironment())
    {
    }

    /// <summary>`environment`: the variables the proxy is read from (PyProxies.ForHttp, as httpx's trust_env reads them).</summary>
    internal RestTransport(string url, TokenSource? token, TimeSpan timeout, IReadOnlyList<KeyValuePair<string, string>> environment)
        : this(url, token, timeout, ConnectSocketAsync, KeepaliveExpiry, environment)
    {
    }

    private RestTransport(string url, TokenSource? token, TimeSpan timeout, Connector connector, TimeSpan keepaliveExpiry,
        IReadOnlyList<KeyValuePair<string, string>> environment)
    {
        Url = ServerUrls.Check(url);
        Timeouts.Check(timeout);
        // as httpx (trust_env): HTTP_PROXY, HTTPS_PROXY, ALL_PROXY and NO_PROXY; a CONNECT tunnel for https://
        var proxy = PyProxies.ForHttp(environment, Url);
        this.token = token;
        this.connector = connector;
        Timeout = timeout;
        var handler = new SocketsHttpHandler
        {
            AllowAutoRedirect = false, // as httpx's Client in jeff/remote.py: a 3xx is a non-2xx answer like any other
            ConnectCallback = ConnectAsync,
            PlaintextStreamFilter = ConnectedAsync,
            ConnectTimeout = System.Threading.Timeout.InfiniteTimeSpan, // ConnectAsync bounds each attempt by Timeout
            PooledConnectionIdleTimeout = keepaliveExpiry,
            // no handler limit: the slots bound the requests in flight, so the handler never makes one wait
            UseCookies = false,
            UseProxy = proxy is not null,
            Proxy = proxy?.WebProxy(),
            // undone here as httpx undoes them (PyHttpBody.Decoded), to report a corrupt body as Python does
            AutomaticDecompression = DecompressionMethods.None,
        };
        client = new HttpClient(handler, disposeHandler: true) { Timeout = System.Threading.Timeout.InfiniteTimeSpan };
    }

    /// <summary>The server's address as the transport calls it (no trailing slash).</summary>
    public string Url { get; }

    internal TimeSpan Timeout { get; }

    internal int InFlight
    {
        get
        {
            lock (inFlight) return inFlight.Count;
        }
    }

    private static async ValueTask<Stream> ConnectSocketAsync(DnsEndPoint endpoint, CancellationToken cancellationToken)
    {
        var socket = new Socket(SocketType.Stream, ProtocolType.Tcp) { NoDelay = true };
        try
        {
            await socket.ConnectAsync(endpoint, cancellationToken).ConfigureAwait(false);
        }
        catch
        {
            socket.Dispose();
            throw;
        }
        return new NetworkStream(socket, ownsSocket: true);
    }

    /// <summary>Opens a connection, bounded by Timeout on its own: the handler may give the request that asked for it
    /// another connection that came free, and the attempt must not outlive that by the system's connect timeout.</summary>
    private async ValueTask<Stream> ConnectAsync(SocketsHttpConnectionContext context, CancellationToken cancellationToken)
    {
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        bounded.CancelAfter(Timeout);
        return await connector(context.DnsEndPoint, bounded.Token).ConfigureAwait(false);
    }

    /// <summary>The connection's HTTP bytes (after TLS, if any) go through a NoRetryStream, which also moves the request
    /// writing on it to the headers phase.</summary>
    private static ValueTask<Stream> ConnectedAsync(SocketsHttpPlaintextStreamFilterContext context, CancellationToken cancellationToken) =>
        ValueTask.FromResult<Stream>(new NoRetryStream(context.PlaintextStream, () => Sending.Value?.Sent()));

    private InvalidRequest ClosedError() => new($"This remote client of {Url} is closed; connect again with JeffClient.RemoteAsync");

    /// <summary>The JeffError for a request that got no answer, as jeff/remote.py maps httpx's exceptions: the connect
    /// timer, or a failure while opening the connection (phase Connect) or its TLS session, is BackendUnavailable "Cannot
    /// reach"; the headers or body timer is DeadlineExceeded; any other failure once connected (a reset, an answer that
    /// is not HTTP, one cut off) is BackendUnavailable "The connection ... failed during".</summary>
    internal JeffError NoAnswerError(Exception error, Phase? fired, Phase phase, string method, string path)
    {
        if (fired == Phase.Connect) return new BackendUnavailable($"Cannot reach the Jeff server at {Url}: no connection within {PyFormat.Millis(Timeout)} ms");
        if (fired is Phase.Pool or Phase.Headers or Phase.Body)
        {
            return new DeadlineExceeded($"The Jeff server at {Url} did not answer {method} {path} within {PyFormat.Millis(Timeout)} ms; the server may "
                + "still have run the request");
        }
        if (phase is Phase.Connect or Phase.Pool || error is HttpRequestException { HttpRequestError: HttpRequestError.NameResolutionError or HttpRequestError.SecureConnectionError })
        {
            return new BackendUnavailable($"Cannot reach the Jeff server at {Url}: {error.Message}");
        }
        string cause = error.InnerException is IOException inner ? $" ({Errors.Described(inner)})" : "";
        return new BackendUnavailable($"The connection to the Jeff server at {Url} failed during {method} {path}: {Errors.Described(error)}{cause}");
    }

    private async Task<JsonObject> RequestAsync(HttpMethod method, string path, JsonObject? body, string schema, CancellationToken cancellationToken)
    {
        lock (inFlight)
        {
            if (closed) throw ClosedError();
        }
        string? bearer = await Tokens.ReadAsync(token, Url, cancellationToken).ConfigureAwait(false);
        using var timers = new PhaseTimers(Timeout);
        using var request = new HttpRequestMessage(method, Url + path);
        if (bearer is not null) request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        request.Headers.TryAddWithoutValidation("Accept-Encoding", PyHttpBody.AcceptEncoding);
        if (body is not null)
        {
            // A caller's own JsonObject (DecideCallsAsync's calls) may hold values backed by int, float and the like:
            // copy it into the SDK's canonical form first, as every other path that writes to the wire does.
            request.Content = new ByteArrayContent(Encoding.UTF8.GetBytes(Json.Write(Json.Canonical(body)!)));
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        }
        lock (inFlight)
        {
            if (closed) throw ClosedError();
            inFlight.Add(timers);
        }
        int status;
        byte[] raw;
        string? requestId;
        string? contentType;
        List<string> contentEncodings;
        bool holding = false;
        try
        {
            using var cancelling = cancellationToken.Register(timers.Abort);
            timers.Start(Phase.Pool);
            await slots.WaitAsync(timers.Token).ConfigureAwait(false);
            holding = true;
            timers.Start(Phase.Connect);
            Sending.Value = timers;
            using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timers.Token).ConfigureAwait(false);
            status = (int)response.StatusCode;
            // several x-request-id headers read as httpx's headers.get reads them: joined by ", "
            requestId = response.Headers.TryGetValues(Remote.RequestIdHeader, out var values) ? string.Join(", ", values) : null;
            contentType = response.Content.Headers.TryGetValues("Content-Type", out var types) ? string.Join(", ", types) : null;
            contentEncodings = response.Content.Headers.TryGetValues("Content-Encoding", out var encodings) ? encodings.ToList() : [];
            timers.Start(Phase.Body);
            using var stream = await response.Content.ReadAsStreamAsync(timers.Token).ConfigureAwait(false);
            using var collected = new MemoryStream();
            var buffer = new byte[16 * 1024];
            while (true)
            {
                timers.Start(Phase.Body);
                int count = await stream.ReadAsync(buffer, timers.Token).ConfigureAwait(false);
                if (count == 0) break;
                if (collected.Length + count > PyHttpBody.MaxBody) throw new BodyTooLarge(decoded: false);
                collected.Write(buffer, 0, count);
            }
            timers.Finish();
            raw = collected.ToArray();
        }
        catch (Exception error) when (error is HttpRequestException or OperationCanceledException or IOException or ObjectDisposedException)
        {
            timers.Finish();
            lock (inFlight)
            {
                if (closed) throw ClosedError();
            }
            cancellationToken.ThrowIfCancellationRequested();
            throw NoAnswerError(error, timers.Fired, timers.Current, method.Method, path);
        }
        catch (BodyTooLarge tooLarge)
        {
            timers.Finish();
            throw await TooLargeAsync(method.Method, path, tooLarge).ConfigureAwait(false);
        }
        finally
        {
            if (holding) slots.Release();
            lock (inFlight) inFlight.Remove(timers);
        }
        byte[] decoded;
        try
        {
            decoded = PyHttpBody.Decoded(raw, contentEncodings);
        }
        catch (BodyTooLarge tooLarge)
        {
            throw await TooLargeAsync(method.Method, path, tooLarge).ConfigureAwait(false);
        }
        catch (DecodingFailure error)
        {
            // httpx decodes while reading, so this is its RequestError branch in jeff/remote.py, before any answer
            throw new EngineError($"The answer of the Jeff server at {Url} to {method.Method} {path} could not be read: DecodingError: {error.Message}");
        }
        try
        {
            return Answer(method.Method, path, status, decoded, contentType, schema);
        }
        catch (JeffError error)
        {
            error.RequestId = requestId;
            throw;
        }
    }

    /// <summary>An answer body past PyHttpBody.MaxBody: the transport is closed (a server that sends one is not trusted
    /// with later requests, as the channel ends on a message past its limit), and the error names the limit.</summary>
    private async Task<BackendUnavailable> TooLargeAsync(string method, string path, BodyTooLarge tooLarge)
    {
        await DisposeAsync().ConfigureAwait(false);
        string what = tooLarge.Decoded ? "a body that decodes (Content-Encoding) to more than" : "a body of more than";
        return new BackendUnavailable($"The Jeff server at {Url} answered {method} {path} with {what} {PyHttpBody.MaxBody} bytes, the most this client "
            + "reads; the client is closed, connect again with JeffClient.RemoteAsync");
    }

    /// <summary>The first 300 characters of the body as httpx's Response.text decodes it.</summary>
    private static string Excerpt(byte[] body, string? contentType) => PyFormat.CodePointPrefix(PyHttpBody.Text(body, contentType), BodyExcerpt);

    private JsonObject Answer(string method, string path, int status, byte[] body, string? contentType, string schema)
    {
        if (status < 200 || status > 299) throw ErrorOf(method, path, status, body, contentType);
        if (!PyHttpBody.TryParseJson(body, out var answer))
        {
            throw new EngineError($"The Jeff server at {Url} answered {method} {path} with status {status} and a body that is not JSON: {Excerpt(body, contentType)}");
        }
        if (answer is not JsonObject obj)
        {
            throw new EngineError($"The Jeff server at {Url} answered {method} {path} with JSON that is not an object: {Excerpt(body, contentType)}");
        }
        if (!Contract.IsValid(schema, obj))
        {
            throw new ContractError($"The answer of {Url} to {method} {path} does not match contract/schemas/{schema}: {Contract.FirstProblem(schema, obj)}");
        }
        return obj;
    }

    /// <summary>The JeffError a non-2xx answer names; a decision it carries (LowConfidence) is checked against the
    /// decision schema first, as the Python SDK builds it at once.</summary>
    private JeffError ErrorOf(string method, string path, int status, byte[] text, string? contentType)
    {
        if (PyHttpBody.TryParseJson(text, out var body) && body is JsonObject obj && obj["error"] is JsonObject wire
            && Json.KindOf(wire["type"]) == JsonKind.String && Json.KindOf(wire["message"]) == JsonKind.String)
        {
            return Remote.WireError(wire, $"the Jeff server at {Url} answered {method} {path} with");
        }
        return new EngineError($"The Jeff server at {Url} answered {method} {path} with status {status} and a body that is not a Jeff error: {Excerpt(text, contentType)}");
    }

    private async Task<JsonNode?> DecideAnswerAsync(JsonObject body, string key, CancellationToken cancellationToken)
    {
        var answer = await RequestAsync(HttpMethod.Post, "/v1/decide", body, "decide_response", cancellationToken).ConfigureAwait(false);
        if (!answer.ContainsKey(key))
        {
            throw new ContractError($"The answer of {Url} to POST /v1/decide has {PyFormat.Repr(PyFormat.Sorted(answer.Select(pair => pair.Key)))}, not {PyFormat.Str(key)}");
        }
        return answer[key];
    }

    private static T Shaped<T>(JsonNode? node, string key, string url) where T : JsonNode =>
        node as T ?? throw new ContractError($"The answer of {url} to POST /v1/decide has a {PyFormat.Str(key)} that is {PyFormat.TypeName(node)}");

    /// <summary>Sends <paramref name="calls"/> in one POST /v1/decide and returns the server's decisions, in order,
    /// checked against contract/schemas/decide_response. Read one with <see cref="Decision.FromJson"/>.</summary>
    /// <param name="calls">the calls in their wire form (contract/schemas/decide_request): each a JsonObject with
    /// "function", "input" and the optional fields</param>
    /// <param name="policy">the low-confidence policy in its wire form, such as "return"</param>
    /// <param name="cancellationToken">cancels the wait for the answer; a request already sent still runs</param>
    /// <returns>one decision per call, as JSON objects</returns>
    /// <exception cref="JeffError">the error the server answered with (such as UnknownFunction or Unauthorised), with
    /// its RequestId; BackendUnavailable when the server cannot be reached; DeadlineExceeded when a phase takes longer
    /// than the timeout; ContractError or EngineError when the answer is not one the contract allows; InvalidRequest
    /// when the transport is closed</exception>
    public async Task<JsonArray> DecideCallsAsync(JsonArray calls, JsonNode policy, CancellationToken cancellationToken)
    {
        var decisions = Shaped<JsonArray>(await DecideAnswerAsync(new JsonObject { ["calls"] = calls, ["on_low_confidence"] = policy }, "decisions", cancellationToken)
            .ConfigureAwait(false), "decisions", Url);
        if (decisions.Count != calls.Count) throw new ContractError($"{Url} answered {decisions.Count} decisions for {calls.Count} calls");
        return decisions;
    }

    internal async Task<JsonObject> DispatchAsync(JsonObject body, CancellationToken cancellationToken) =>
        Shaped<JsonObject>(await DecideAnswerAsync(body, "decision", cancellationToken).ConfigureAwait(false), "decision", Url);

    internal async Task<JsonObject> GraphAsync(JsonObject body, CancellationToken cancellationToken) =>
        Shaped<JsonObject>(await DecideAnswerAsync(body, "graph_result", cancellationToken).ConfigureAwait(false), "graph_result", Url);

    internal Task<JsonObject> FunctionsAsync(CancellationToken cancellationToken) =>
        RequestAsync(HttpMethod.Get, "/v1/functions", null, "functions_response", cancellationToken);

    /// <summary>Closes the connections; a request still in flight fails with the closed error. Calling it again does nothing.</summary>
    public ValueTask DisposeAsync()
    {
        List<PhaseTimers> flying;
        lock (inFlight)
        {
            if (closed) return ValueTask.CompletedTask;
            closed = true;
            flying = inFlight.ToList();
        }
        foreach (var timers in flying) timers.Abort();
        client.Dispose();
        return ValueTask.CompletedTask;
    }

    /// <summary>This transport as RemoteJeffClient reaches it: the internal operations (dispatch, graph, functions)
    /// stay off the public surface, reachable only through ITransport.</summary>
    internal ITransport AsTransport() => new Adapter(this);

    private sealed class Adapter(RestTransport rest) : ITransport
    {
        public string Url => rest.Url;

        public Task<JsonArray> DecideCallsAsync(JsonArray calls, JsonNode policy, CancellationToken cancellationToken) =>
            rest.DecideCallsAsync(calls, policy, cancellationToken);

        public Task<JsonObject> DispatchAsync(JsonObject body, CancellationToken cancellationToken) => rest.DispatchAsync(body, cancellationToken);

        public Task<JsonObject> GraphAsync(JsonObject body, CancellationToken cancellationToken) => rest.GraphAsync(body, cancellationToken);

        public Task<JsonObject> FunctionsAsync(CancellationToken cancellationToken) => rest.FunctionsAsync(cancellationToken);

        public ValueTask DisposeAsync() => rest.DisposeAsync();
    }
}

/// <summary>The engine a RemoteJeffClient holds: the adapters the server serves and the base format its engines serve,
/// read from GET /v1/functions when it is built and again on InstalledAsync(refresh: true). Each read checks that every
/// served adapter the local catalogue also has is the same version, and that the base format has not changed. It never
/// predicts: RemoteJeffClient sends every decision to the server.</summary>
internal sealed class ServedFunctions : IEngine
{
    private const string Where = "GET /v1/functions";
    private readonly ITransport transport;
    private readonly Catalogue catalogue;
    private readonly string url;
    private readonly object sync = new();
    private IReadOnlySet<string> served;
    private OrderedMap<(Calibration? Calibration, OrderedMap<Calibration> ByFormat)> calibrations = new();

    private ServedFunctions(ITransport transport, Catalogue catalogue, string url, JsonObject answer)
    {
        this.transport = transport;
        this.catalogue = catalogue;
        this.url = url;
        BaseFormat = BaseFormat.FromJson(JsonRead.Object(answer, "base_format", Where), $"{Where} base_format");
        served = ReadListing(answer);
    }

    public BaseFormat BaseFormat { get; }

    /// <summary>Function ID to its bfloat16 calibration and its calibration_by_format entries, as the server lists them
    /// (the thresholds the server decides with).</summary>
    internal OrderedMap<(Calibration? Calibration, OrderedMap<Calibration> ByFormat)> Calibrations
    {
        get
        {
            lock (sync) return calibrations;
        }
    }

    public static async Task<ServedFunctions> ReadAsync(ITransport transport, Catalogue catalogue, string url, CancellationToken cancellationToken = default) =>
        new(transport, catalogue, url, await transport.FunctionsAsync(cancellationToken).ConfigureAwait(false));

    private IReadOnlySet<string> ReadListing(JsonObject answer)
    {
        var format = BaseFormat.FromJson(JsonRead.Object(answer, "base_format", Where), $"{Where} base_format");
        if (format != BaseFormat)
        {
            throw new ContractError($"The server at {url} now serves a base of format {PyFormat.Repr(format.ToJson())}, not {PyFormat.Repr(BaseFormat.ToJson())} "
                + "as when this client connected; the functions bound here use that format's calibration. Connect again");
        }
        var versions = new OrderedMap<string>();
        var listedCalibrations = new OrderedMap<(Calibration?, OrderedMap<Calibration>)>();
        foreach (var listed in JsonRead.Array(answer, "functions", Where).Select(item => item!.AsObject()))
        {
            string id = JsonRead.String(listed, "id", Where);
            // Python: function["id"].rsplit(".", 1)[0], which is the whole id when it has no '.'
            int dot = id.LastIndexOf('.');
            versions.Set(dot < 0 ? id : id[..dot], JsonRead.String(listed, "adapter_version", Where));
            Calibration? block = listed["calibration"] is JsonObject calibration ? Manifest.CalibrationFromWire(calibration, $"{Where} {id}") : null;
            // calibration_by_format is optional in functions_response: a function listed without it has no INT4 entries
            var byFormat = new OrderedMap<Calibration>();
            if (listed["calibration_by_format"] is JsonObject formats)
            {
                foreach (var (formatId, entry) in formats) byFormat.Set(formatId, Manifest.CalibrationFromWire(entry!.AsObject(), $"{Where} {id} {formatId}"));
            }
            listedCalibrations.Set(id, (block, byFormat));
        }
        lock (sync) calibrations = listedCalibrations;
        var local = catalogue.Manifests().ToDictionary(manifest => manifest.Id, manifest => manifest.Version, StringComparer.Ordinal);
        foreach (var (adapterId, version) in versions)
        {
            if (local.TryGetValue(adapterId, out var mine) && mine != version) throw Remote.VersionMismatch(adapterId, version, mine);
        }
        return versions.Keys.ToHashSet(StringComparer.Ordinal);
    }

    public async Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default)
    {
        if (refresh)
        {
            var listing = ReadListing(await transport.FunctionsAsync(cancellationToken).ConfigureAwait(false));
            lock (sync) served = listing;
        }
        lock (sync) return new HashSet<string>(served, StringComparer.Ordinal);
    }

    public Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default) =>
        Task.FromException<EngineResult>(new EngineError($"RemoteJeffClient sends every decision to the server at {url}; nothing may call its engine's "
            + $"predict (asked for {rows.Count} row(s) of {adapterId})"));

    /// <summary>Nothing to release: the transport belongs to the RemoteJeffClient.</summary>
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

/// <summary>How a remote client connects: the timeout, the catalogue, and whether to use the WebSocket channel.</summary>
public sealed class RemoteOptions
{
    /// <summary>How long each phase of a request may take: connecting, the answer's headers (which also covers sending
    /// the request body), and each wait for more of the answer's body.</summary>
    public required TimeSpan Timeout { get; init; }

    /// <summary>The manifests calls are bound with; the catalogue shipped with the package when not given.</summary>
    public Catalogue? Catalogue { get; init; }

    /// <summary>Use the server's WebSocket channel (lower latency; bound functions become handles on the server) instead
    /// of one HTTP request per operation. Timeout then bounds opening the channel, each request's wait for its turn to
    /// be written, each piece of a frame the socket must accept (a send that stalls longer ends the channel), each wait
    /// for a reply (the hello's included), which starts once the request is written, and the closing handshake.</summary>
    public bool Channel { get; init; }

    /// <summary>For tests: the environment variables proxies are read from, in place of the process's.</summary>
    internal IReadOnlyList<KeyValuePair<string, string>>? Environment { get; init; }
}

internal static class Remote
{
    /// <summary>The most calls one request carries (contract/schemas/decide_request); more are refused, never split.</summary>
    public const int MaxCalls = 256;
    public const string RequestIdHeader = "x-request-id";

    /// <summary>The wire form of a policy the server can run: "return", "error", or {escalate: [function refs],
    /// budget_ms?}; null when the policy has an llm, human or remote step, which only this process can run.</summary>
    public static JsonNode? ServerPolicy(Policy policy)
    {
        switch (policy)
        {
            case NamedPolicy { Name: "return" or "error" } named:
                return JsonValue.Create(named.Name);
            case EscalatePolicy escalate:
                Escalation.RefuseNonSteps(escalate.Steps);
                if (escalate.Steps.Any(step => step is AskStep or RemoteStep)) return null;
                var wire = new JsonObject { ["escalate"] = Json.Strings(escalate.Steps.Cast<FunctionStep>().Select(step => step.Ref)) };
                if (escalate.Budget is { } budget) wire["budget_ms"] = BudgetMs(budget);
                return wire;
            default:
                throw Escalation.UnknownPolicy(policy);
        }
    }

    /// <summary>A budget as the number of milliseconds a caller would have written: an int when whole, else a float.</summary>
    internal static JsonNode BudgetMs(TimeSpan budget)
    {
        double ms = budget.TotalMilliseconds;
        return ms == Math.Floor(ms) && Math.Abs(ms) < 1e15 ? JsonValue.Create((long)ms) : JsonValue.Create(ms);
    }

    public static void CheckBatch(int count, string what)
    {
        if (count > MaxCalls)
        {
            throw new InvalidRequest($"{count} {what} in one request: a Jeff server answers at most {MaxCalls} per request; send them in batches of at most {MaxCalls}");
        }
    }

    /// <summary>A server serving another version of an adapter than the local catalogue has: labels would be mapped by
    /// the wrong manifest.</summary>
    public static ContractError VersionMismatch(string adapterId, string served, string local) =>
        new($"{adapterId}: the server serves version {served}, this client's catalogue has {local}; pass catalogue= with the server's manifests");

    /// <summary>A call as the server takes it, naming the function by the ID the local catalogue resolved, so the server
    /// answers exactly the function this client bound.</summary>
    internal static JsonObject CallToWire(Call call, Binding binding)
    {
        var wire = new JsonObject { ["function"] = binding.Function.Id, ["input"] = Json.Canonical(call.Input) };
        if (call.Context is not null) wire["context"] = Json.Canonical(call.Context);
        if (call.Labels is not null) wire["labels"] = Json.Canonical(call.Labels);
        return wire;
    }

    /// <summary>A decision the server answered, checked against contract/schemas/decision before it is built.</summary>
    internal static Decision ServerDecision(JsonNode? data, string url)
    {
        if (!Contract.IsValid("decision", data))
        {
            throw new ContractError($"A decision from {url} does not match contract/schemas/decision: {Contract.FirstProblem("decision", data)}");
        }
        return Decision.FromJson(data!.AsObject());
    }

    /// <summary>A graph result the server answered, checked against contract/schemas/graph_result before it is built.</summary>
    internal static GraphResult ServerGraphResult(JsonNode? data, string url)
    {
        if (!Contract.IsValid("graph_result", data))
        {
            throw new ContractError($"A graph result from {url} does not match contract/schemas/graph_result: {Contract.FirstProblem("graph_result", data)}");
        }
        return GraphResult.FromJson(data!.AsObject());
    }

    /// <summary>The JeffError a wire error names (type and message strings), refused as a ContractError when the decision
    /// it carries does not match contract/schemas/decision. `source` reads "&lt;who&gt; answered ... with".</summary>
    internal static JeffError WireError(JsonObject wire, string source)
    {
        var made = Errors.FromWire(wire["type"]!.GetValue<string>(), wire["message"]!.GetValue<string>(), wire["details"]);
        if (made.Details.ContainsKey("decision") && !Contract.IsValid("decision", made.Details["decision"]))
        {
            return new ContractError($"The {made.Type} error {source} carries a decision that does not match contract/schemas/decision: "
                + Contract.FirstProblem("decision", made.Details["decision"]));
        }
        return made;
    }
}

/// <summary>A bound function of a RemoteJeffClient: bound here once (context, labels and policy checked), each call or
/// ManyAsync one request.</summary>
public class RemoteBoundFunction : BoundFunction
{
    private protected RemoteBoundFunction(JeffClient client, Call template, Policy policy, Binding bound) : base(client, template, policy, bound)
    {
    }

    internal static new async Task<RemoteBoundFunction> CreateAsync(JeffClient client, Call template, Policy policy, CancellationToken cancellationToken)
    {
        var bound = await client.BindCallAsync(template, cancellationToken).ConfigureAwait(false);
        await Escalation.CheckPolicyAsync(client, bound.Function.Interface, policy, cancellationToken).ConfigureAwait(false);
        return new RemoteBoundFunction(client, template, policy, bound);
    }

    /// <summary>Decides every input in one request (InvalidRequest past 256 inputs), with the bound context, labels and policy;
    /// the decisions come back in input order.</summary>
    public override Task<IReadOnlyList<Decision>> ManyAsync(IReadOnlyList<JsonNode?> inputs, CancellationToken cancellationToken = default) =>
        ((RemoteJeffClient)Client).DecidePolicyAsync(inputs.Select(input => new BoundCall(CallOf(input), Bound)).ToList(), Policy, cancellationToken);
}

/// <summary>A client over a Jeff server (port of jeff/remote.py). It keeps a local catalogue, so binding, labels and
/// policies are worked out as in local mode, and each operation is one POST /v1/decide. A policy the server can run
/// ("return", "error", function steps only) is sent with the request; one with an llm, human or remote step runs here
/// on the decision the server returns. Connecting reads GET /v1/functions once.</summary>
public class RemoteJeffClient : JeffClient
{
    private protected RemoteJeffClient(JeffParts parts, ITransport transport, string url) : base(parts)
    {
        Transport = transport;
        Url = url;
    }

    internal ITransport Transport { get; }

    /// <summary>The server's URL, as the client uses it (no trailing slash).</summary>
    public string Url { get; }

    internal static async Task<RemoteJeffClient> ConnectAsync(string url, TokenSource? token, RemoteOptions options, CancellationToken cancellationToken = default)
    {
        Timeouts.Check(options.Timeout);
        var catalogue = options.Catalogue ?? Catalogue.Shipped();
        var environment = options.Environment ?? PyProxies.ProcessEnvironment();
        if (options.Channel) return await ChannelJeffClient.OpenAsync(url, token, options.Timeout, catalogue, environment, cancellationToken).ConfigureAwait(false);
        var transport = new RestTransport(url, token, options.Timeout, environment);
        try
        {
            return await OverAsync(transport.AsTransport(), catalogue, transport.Url, cancellationToken).ConfigureAwait(false);
        }
        catch
        {
            await transport.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    internal static async Task<RemoteJeffClient> OverAsync(ITransport transport, Catalogue catalogue, string url, CancellationToken cancellationToken = default)
    {
        var served = await ServedFunctions.ReadAsync(transport, catalogue, url, cancellationToken).ConfigureAwait(false);
        return new RemoteJeffClient(await PartsAsync(served, catalogue, cancellationToken).ConfigureAwait(false), transport, url);
    }

    /// <summary>Closes the client's connections to the server; later calls fail with InvalidRequest.</summary>
    public override ValueTask DisposeAsync() => Transport.DisposeAsync();

    /// <summary>The calibration the server decides `fn` with, from its listing; NotInstalled for a function it does not list.</summary>
    private protected override Calibration? ResolveCalibrationOf(FunctionSpec fn)
    {
        if (!((ServedFunctions)Engine).Calibrations.TryGetValue(fn.Id, out var listed))
        {
            throw Errors.NotInstalledError(fn.Id, $"the server at {Url} does not list it");
        }
        return Manifest.ResolveCalibration(fn.Id, listed.Calibration, listed.ByFormat, FormatId);
    }

    /// <summary>Remote.ServerPolicy with every function step named by the full ID the local catalogue resolves, so the
    /// server runs exactly the steps this client checked.</summary>
    internal JsonNode? WirePolicy(Policy policy)
    {
        var wire = Remote.ServerPolicy(policy);
        if (wire is JsonObject escalate)
        {
            var refs = escalate["escalate"]!.AsArray().Select(reference => Catalogue.Function(reference!.GetValue<string>()).Id).ToList();
            escalate["escalate"] = Json.Strings(refs);
        }
        return wire;
    }

    /// <summary>One request for these calls; none for no calls (as local mode, which answers [] without the engine).</summary>
    private async Task<IReadOnlyList<Decision>> PostAsync(IReadOnlyList<BoundCall> bound, JsonNode policy, CancellationToken cancellationToken)
    {
        if (bound.Count == 0) return [];
        Remote.CheckBatch(bound.Count, "calls");
        var calls = new JsonArray(bound.Select(item => (JsonNode?)Remote.CallToWire(item.Call, item.Binding)).ToArray());
        var answered = await Transport.DecideCallsAsync(calls, policy, cancellationToken).ConfigureAwait(false);
        return answered.Select(item => Remote.ServerDecision(item, Url)).ToList();
    }

    internal override Task<IReadOnlyList<Decision>> DecideBoundAsync(IReadOnlyList<BoundCall> bound, CancellationToken cancellationToken = default) =>
        PostAsync(bound, JsonValue.Create("return"), cancellationToken);

    /// <summary>Already-bound calls in one request: with the policy when the server can run it, else with "return" and
    /// the policy then run here on each decision.</summary>
    internal async Task<IReadOnlyList<Decision>> DecidePolicyAsync(IReadOnlyList<BoundCall> bound, Policy policy, CancellationToken cancellationToken = default)
    {
        var wire = WirePolicy(policy);
        if (wire is not null) return await PostAsync(bound, wire, cancellationToken).ConfigureAwait(false);
        var decisions = await PostAsync(bound, JsonValue.Create("return"), cancellationToken).ConfigureAwait(false);
        var answered = new List<Decision>(decisions.Count);
        for (int index = 0; index < bound.Count; index++)
        {
            answered.Add(await ApplyPolicyAsync(bound[index].Call, decisions[index], policy, cancellationToken).ConfigureAwait(false));
        }
        return answered;
    }

    /// <summary>Binds `reference` here once (context, labels and policy checked, as in local mode); each call of the bound function
    /// is then one request. UnknownFunction, NotInstalled (a function the server does not list), MissingContext, InvalidLabels
    /// or InvalidPolicy when the binding fails.</summary>
    public override async Task<BoundFunction> FunctionAsync(string reference, CallOptions? options = null, CancellationToken cancellationToken = default) =>
        await RemoteBoundFunction.CreateAsync(this, new Call(reference, null, options?.Context, options?.Labels), options?.OnLowConfidence ?? Policy.Return,
            cancellationToken).ConfigureAwait(false);

    internal override async Task<Decision> InterfaceCallAsync(string iface, JsonNode? input, ShorthandOptions? options, CancellationToken cancellationToken = default)
    {
        var policy = options?.OnLowConfidence ?? Policy.Return;
        if (options?.Function is null)
        {
            return await DispatchAsync(input, new DispatchOptions { Interface = iface, Context = options?.Context, Labels = options?.Labels, OnLowConfidence = policy },
                cancellationToken).ConfigureAwait(false);
        }
        var spec = Catalogue.Function(options.Function);
        if (spec.Interface != iface) throw new InterfaceMismatch($"{spec.Id} implements {spec.Interface}, not {iface}; call jeff.{spec.Interface}");
        await Escalation.CheckPolicyAsync(this, iface, policy, cancellationToken).ConfigureAwait(false);
        var call = new Call(options.Function, input, options.Context, options.Labels);
        var binding = await BindCallAsync(call, cancellationToken).ConfigureAwait(false);
        return (await DecidePolicyAsync([new BoundCall(call, binding)], policy, cancellationToken).ConfigureAwait(false))[0];
    }

    /// <summary>Routes the input to a function implementing options.Interface, starting from options.Router, in one request; a
    /// policy only this process can run (an llm, human or remote step) runs here on the decision that comes back.</summary>
    public override async Task<Decision> DispatchAsync(JsonNode? input, DispatchOptions options, CancellationToken cancellationToken = default)
    {
        var policy = options.OnLowConfidence ?? Policy.Return;
        await Escalation.CheckPolicyAsync(this, options.Interface, policy, cancellationToken).ConfigureAwait(false);
        string routerId = await Routing.StartRouterAsync(this, options.Router ?? Routing.DefaultRouter, cancellationToken).ConfigureAwait(false);
        var wire = WirePolicy(policy);
        var request = new JsonObject { ["input"] = Json.Canonical(input), ["interface"] = options.Interface, ["router"] = routerId };
        if (options.Context is not null) request["context"] = Json.Canonical(options.Context);
        if (options.Labels is not null) request["labels"] = Json.Canonical(options.Labels);
        // a policy with a step only this process can run is sent as "return", and run here on the decision that comes back
        var sent = wire ?? JsonValue.Create("return");
        var decision = Remote.ServerDecision(await Transport.DispatchAsync(new JsonObject { ["dispatch"] = request, ["on_low_confidence"] = sent }, cancellationToken)
            .ConfigureAwait(false), Url);
        if (wire is not null || !decision.Abstained) return decision;
        // As in local mode, a router that abstains raises; an abstained leaf is escalated with the call dispatch made to it.
        var fn = Catalogue.Function(decision.Function);
        if (Catalogue.ManifestOf(fn).IsRouter) throw Errors.LowConfidenceError(decision);
        return await ApplyPolicyAsync(Routing.LeafCall(fn, input, options.Context, options.Labels), decision, policy, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>A graph with no code nodes, whose node policies the server can run, is one request carrying the graph
    /// inline (each node's function by the ID the local catalogue resolves); any other graph runs here stage by stage.
    /// Everything local mode checks before the first decision is checked here first, without a request.</summary>
    public override async Task<GraphResult> RunGraphAsync(Graph graph, JsonNode? input, JsonObject parameters, IReadOnlyDictionary<string, CodeFunction>? code = null,
        IReadOnlyDictionary<string, Policy>? policies = null, CancellationToken cancellationToken = default)
    {
        var functions = code ?? NoCode;
        var nodePolicies = policies ?? NoPolicies;
        var ownParameters = (JsonObject)Json.Canonical(parameters)!;
        var model = await Graphs.CheckAsync(this, graph, input, ownParameters, functions, nodePolicies, cancellationToken).ConfigureAwait(false);
        var wire = nodePolicies.Select(pair => (pair.Key, Wire: WirePolicy(pair.Value))).ToList();
        if (graph.Nodes.Values.Any(node => node.Code is not null) || wire.Any(item => item.Wire is null))
        {
            return await Graphs.RunCheckedAsync(this, graph, input, ownParameters, functions, model, cancellationToken).ConfigureAwait(false);
        }
        var inline = Graphs.ToDict(graph);
        foreach (var (name, node) in model) inline["nodes"]![name]!["call"] = node.Binding.Function.Id;
        var body = new JsonObject { ["graph"] = new JsonObject { ["inline"] = inline }, ["input"] = Json.Canonical(input), ["params"] = ownParameters };
        if (wire.Count > 0)
        {
            var byNode = new JsonObject();
            foreach (var (name, policy) in wire) byNode[name] = policy;
            body["on_low_confidence_by_node"] = byNode;
        }
        return Remote.ServerGraphResult(await Transport.GraphAsync(body, cancellationToken).ConfigureAwait(false), Url);
    }

    /// <summary>A graph registered on the server, by id, in one request; every node policy must be one the server can run.</summary>
    public async Task<GraphResult> RunServerGraphAsync(string graphId, JsonNode? input, JsonObject parameters, IReadOnlyDictionary<string, Policy>? policies = null,
        CancellationToken cancellationToken = default)
    {
        var byNode = new JsonObject();
        foreach (var (name, policy) in policies ?? NoPolicies)
        {
            byNode[name] = WirePolicy(policy) ?? throw new InvalidPolicy($"Node {name}: a graph registered on the server runs there, and the server cannot "
                + "call llm, human or remote escalation steps; use 'return', 'error' or Policy.Escalate(Steps.Function(...), ...) for this node");
        }
        var body = new JsonObject { ["graph"] = new JsonObject { ["id"] = graphId }, ["input"] = Json.Canonical(input), ["params"] = Json.Canonical(parameters) };
        if (byNode.Count > 0) body["on_low_confidence_by_node"] = byNode;
        return Remote.ServerGraphResult(await Transport.GraphAsync(body, cancellationToken).ConfigureAwait(false), Url);
    }
}
