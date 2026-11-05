using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using Jeff.Sdk.Tests.Conformance;
using Jeff.Sdk.Tests.Helpers;
using Microsoft.AspNetCore.Http;

namespace Jeff.Sdk.Tests;

public class RemoteTests
{
    /// <summary>The GET /v1/functions answer recorded in remote.json (the shipped catalogue's functions, base bf16).</summary>
    private static string Listing => Fixtures.Cases("remote.json").First()["exchanges"]![0]!["response"]!["body"]!.GetValue<string>();

    private static JsonObject RecordedDecision() =>
        (JsonObject)Json.Parse(Fixtures.Cases("remote.json").ElementAt(1)["exchanges"]![1]!["response"]!["body"]!.GetValue<string>(), "a recorded answer")!["decisions"]![0]!.AsObject().DeepClone();

    private static JsonObject Ctx() => new() { ["company"] = "A furniture shop.", ["channel"] = "email" };

    private static RemoteOptions Options(int milliseconds) => new() { Timeout = TimeSpan.FromMilliseconds(milliseconds) };

    private static async Task<(double Ms, Exception? Error)> Rejection(Func<Task> action)
    {
        long started = Stopwatch.GetTimestamp();
        try
        {
            await action();
        }
        catch (Exception error)
        {
            return (Stopwatch.GetElapsedTime(started).TotalMilliseconds, error);
        }
        return (Stopwatch.GetElapsedTime(started).TotalMilliseconds, null);
    }

    [Fact]
    public async Task AServerThatCannotBeReachedIsBackendUnavailable() =>
        await Assert.ThrowsAsync<BackendUnavailable>(() => JeffClient.RemoteAsync("http://127.0.0.1:9", null, Options(2000)));

    [Fact]
    public async Task AServerThatDoesNotSendHeadersInTimeIsDeadlineExceededPromptly()
    {
        await using var server = await HttpServer.StartAsync((context, _) => Task.Delay(Timeout.Infinite, context.RequestAborted));
        var (elapsed, error) = await Rejection(() => JeffClient.RemoteAsync(server.Url, null, Options(200)));
        Assert.Equal($"The Jeff server at {server.Url} did not answer GET /v1/functions within 200 ms; the server may still have run the request",
            Assert.IsType<DeadlineExceeded>(error).Message);
        Assert.InRange(elapsed, 190, 400);
    }

    [Fact]
    public async Task AnAnswerThatIsNotAJeffErrorIsAnEngineErrorQuotingIt()
    {
        await using var server = await HttpServer.StartAsync((context, _) =>
        {
            context.Response.StatusCode = 502;
            return context.Response.WriteAsync("Bad gateway");
        });
        var error = await Assert.ThrowsAsync<EngineError>(() => JeffClient.RemoteAsync(server.Url, null, Options(2000)));
        Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with status 502 and a body that is not a Jeff error: Bad gateway", error.Message);
    }

    [Fact]
    public async Task TheUrlTheTokenAndTheTimeoutAreChecked()
    {
        Assert.Equal("The server URL 'ftp://x' must start with http:// or https://",
            Assert.Throws<InvalidRequest>(() => new RestTransport("ftp://x", null, TimeSpan.FromSeconds(1))).Message);
        await Assert.ThrowsAsync<InvalidRequest>(() => JeffClient.RemoteAsync("http://x", null, new RemoteOptions { Timeout = TimeSpan.Zero }));
        await using var transport = new RestTransport("http://127.0.0.1:9", TokenSource.From(_ => ValueTask.FromResult<string?>("")), TimeSpan.FromSeconds(1));
        Assert.Equal("The token for http://127.0.0.1:9 must be a non-empty string; the token function returned an empty string",
            (await Assert.ThrowsAsync<Unauthorised>(() => transport.FunctionsAsync(default))).Message);
        Assert.Equal("The token for http://h must be a non-empty string; the token function returned null",
            (await Assert.ThrowsAsync<Unauthorised>(() => Tokens.ReadAsync(TokenSource.From(_ => ValueTask.FromResult<string?>(null)), "http://h", default))).Message);
    }

    [Fact]
    public async Task ATokenIsNeverPutInAnErrorMessage()
    {
        var error = await Assert.ThrowsAsync<Unauthorised>(() => Tokens.ReadAsync(TokenSource.From(_ => ValueTask.FromResult<string?>("s3cret\nvalue")), "http://h", default));
        Assert.DoesNotContain("s3cret", error.Message);
    }

    [Fact]
    public async Task TheTokenFunctionIsCalledBeforeEveryRequestAndSentAsABearerToken()
    {
        var seen = new List<string>();
        await using var server = await HttpServer.StartAsync((context, _) =>
        {
            lock (seen) seen.Add(context.Request.Headers.Authorization.ToString());
            return HttpServer.Json(context, 200, Listing);
        });
        int count = 0;
        await using var jeff = await JeffClient.RemoteAsync(server.Url, TokenSource.From(_ => ValueTask.FromResult<string?>($"token-{Interlocked.Increment(ref count)}")),
            Options(2000));
        await jeff.Engine.InstalledAsync(true);
        Assert.Equal(new[] { "Bearer token-1", "Bearer token-2" }, seen);
    }

    [Fact]
    public async Task AWireErrorBecomesItsJeffErrorWithTheAnswersRequestId()
    {
        await using var server = await HttpServer.StartAsync((context, _) => context.Request.Method == "GET"
            ? HttpServer.Json(context, 200, Listing)
            : HttpServer.Json(context, 401, "{\"error\": {\"type\": \"Unauthorised\", \"message\": \"The token has expired\", \"details\": {}}}", "rid-42"));
        await using var jeff = await JeffClient.RemoteAsync(server.Url, "t", Options(2000));
        var error = await Assert.ThrowsAsync<Unauthorised>(() => jeff.DecideAsync([new Call("spam.is_spam", "Hi", new JsonObject { ["channel"] = "sms" })]));
        Assert.Equal(("The token has expired", "rid-42"), (error.Message, error.RequestId));
    }

    [Fact]
    public async Task A402UsageLimitReachedAnswerIsRaisedAsUsageLimitReachedWithItsDetails()
    {
        await using var server = await HttpServer.StartAsync((context, _) => context.Request.Method == "GET"
            ? HttpServer.Json(context, 200, Listing)
            : HttpServer.Json(context, 402, "{\"error\": {\"type\": \"UsageLimitReached\", \"message\": \"This account has used its 50,000,000 input tokens "
                + "for the period ending 2026-12-03 10:00 UTC; calls resume then\", \"details\": {\"account\": \"u_1\", \"used\": 50000123, "
                + "\"allowance\": 50000000, \"period_end\": \"2026-12-03T10:00:00Z\"}}}", "rid-402"));
        await using var jeff = await JeffClient.RemoteAsync(server.Url, "t", Options(2000));
        var error = await Assert.ThrowsAsync<UsageLimitReached>(() => jeff.DecideAsync([new Call("spam.is_spam", "Hi", new JsonObject { ["channel"] = "sms" })]));
        Assert.Equal(("u_1", 50_000_123L, 50_000_000L, "2026-12-03T10:00:00Z", "rid-402"),
            (error.Account, error.Used, error.Allowance, error.PeriodEnd, error.RequestId));
    }

    [Fact]
    public async Task AShorthandNamingAFunctionOfAnotherInterfaceIsRefusedBeforeAnyRequest()
    {
        int posts = 0;
        await using var server = await HttpServer.StartAsync((context, _) =>
        {
            if (context.Request.Method == "GET") return HttpServer.Json(context, 200, Listing);
            Interlocked.Increment(ref posts);
            return HttpServer.Json(context, 500, "unexpected");
        });
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        var error = await Assert.ThrowsAsync<InterfaceMismatch>(() => jeff.ClassifyAsync("x", new ShorthandOptions { Function = "jeff.support.triage.urgency", Context = Ctx() }));
        Assert.Contains("implements rate, not classify", error.Message);
        Assert.Equal(0, posts);
    }

    [Fact]
    public async Task AServerDecisionThatDoesNotMatchTheDecisionSchemaIsAContractError()
    {
        var decision = RecordedDecision();
        decision["abstained"] = "no";
        await using var server = await HttpServer.StartAsync((context, _) => context.Request.Method == "GET"
            ? HttpServer.Json(context, 200, Listing)
            : HttpServer.Json(context, 200, Json.Write(new JsonObject { ["decisions"] = new JsonArray(Json.Canonical(decision)) })));
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        await Assert.ThrowsAsync<ContractError>(() => jeff.RateAsync("x", new ShorthandOptions { Function = "jeff.support.triage.urgency", Context = Ctx() }));
    }

    [Fact]
    public async Task ALowConfidenceWhoseDecisionDoesNotMatchTheSchemaIsAContractErrorWithTheRequestId()
    {
        await using var server = await HttpServer.StartAsync((context, _) => context.Request.Method == "GET"
            ? HttpServer.Json(context, 200, Listing)
            : HttpServer.Json(context, 409, "{\"error\": {\"type\": \"LowConfidence\", \"message\": \"abstained\", \"details\": {\"decision\": {\"value\": 1}}}}"));
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        var error = await Assert.ThrowsAsync<ContractError>(() => jeff.RateAsync("x",
            new ShorthandOptions { Function = "jeff.support.triage.urgency", Context = Ctx(), OnLowConfidence = Policy.Error }));
        Assert.Equal("rid-1", error.RequestId);
    }

    [Fact]
    public async Task AnAnswerWhoseBodyStopsArrivingIsDeadlineExceededPromptly()
    {
        await using var server = await HttpServer.StartAsync(async (context, _) =>
        {
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            await context.Response.WriteAsync("{\"base_format\":");
            await context.Response.Body.FlushAsync();
            await Task.Delay(Timeout.Infinite, context.RequestAborted);
        });
        var (elapsed, error) = await Rejection(() => JeffClient.RemoteAsync(server.Url, null, Options(300)));
        Assert.Equal($"The Jeff server at {server.Url} did not answer GET /v1/functions within 300 ms; the server may still have run the request",
            Assert.IsType<DeadlineExceeded>(error).Message);
        Assert.InRange(elapsed, 290, 500);
    }

    [Fact]
    public async Task APhaseTimerFiresPromptlyNamingItsPhaseAndAFinishedOneNeverFires()
    {
        using var timers = new PhaseTimers(TimeSpan.FromMilliseconds(100));
        long started = Stopwatch.GetTimestamp();
        timers.Start(Phase.Connect);
        await Task.Delay(Timeout.Infinite, timers.Token).ContinueWith(_ => { }, TaskScheduler.Default);
        Assert.InRange(Stopwatch.GetElapsedTime(started).TotalMilliseconds, 90, 250);
        Assert.Equal(Phase.Connect, timers.Fired);
        Assert.False(timers.Running);
        using var finished = new PhaseTimers(TimeSpan.FromMilliseconds(50));
        finished.Start(Phase.Headers);
        finished.Finish();
        await Task.Delay(120);
        Assert.Null(finished.Fired);
        Assert.False(finished.Token.IsCancellationRequested);
    }

    [Fact]
    public async Task FailuresMapToPythonsErrorsConnectingIsBackendUnavailableWaitingDeadlineExceeded()
    {
        await using var transport = new RestTransport("http://h", null, TimeSpan.FromMilliseconds(500));
        JeffError Map(Exception error, Phase? fired, Phase phase) => transport.NoAnswerError(error, fired, phase, "GET", "/v1/functions");
        var timedOut = new TaskCanceledException();
        Assert.Equal("Cannot reach the Jeff server at http://h: no connection within 500 ms",
            Assert.IsType<BackendUnavailable>(Map(timedOut, Phase.Connect, Phase.Connect)).Message);
        var refused = new HttpRequestException(HttpRequestError.ConnectionError, "Connection refused (10.0.0.1:80)");
        Assert.Equal("Cannot reach the Jeff server at http://h: Connection refused (10.0.0.1:80)",
            Assert.IsType<BackendUnavailable>(Map(refused, null, Phase.Connect)).Message);
        var unknownHost = new HttpRequestException(HttpRequestError.NameResolutionError, "No such host is known. (h:80)");
        Assert.StartsWith("Cannot reach the Jeff server at http://h: ", Assert.IsType<BackendUnavailable>(Map(unknownHost, null, Phase.Headers)).Message);
        const string deadline = "The Jeff server at http://h did not answer GET /v1/functions within 500 ms; the server may still have run the request";
        Assert.Equal(deadline, Assert.IsType<DeadlineExceeded>(Map(timedOut, Phase.Headers, Phase.Headers)).Message);
        Assert.Equal(deadline, Assert.IsType<DeadlineExceeded>(Map(timedOut, Phase.Body, Phase.Body)).Message);
        var ended = new HttpRequestException(HttpRequestError.ResponseEnded, "The response ended prematurely.");
        Assert.Equal("The connection to the Jeff server at http://h failed during GET /v1/functions: HttpRequestException: The response ended prematurely.",
            Assert.IsType<BackendUnavailable>(Map(ended, null, Phase.Headers)).Message);
    }

    [Fact]
    public async Task ARedirectIsNotFollowedButAnsweredAsANonJeffErrorWithinTheTimeout()
    {
        var paths = new List<string>();
        await using var server = await HttpServer.StartAsync((context, _) =>
        {
            lock (paths) paths.Add(context.Request.Path.Value!);
            context.Response.StatusCode = 302;
            context.Response.Headers.Location = "/never";
            return context.Response.WriteAsync("moved");
        });
        var (elapsed, error) = await Rejection(() => JeffClient.RemoteAsync(server.Url, null, Options(200)));
        Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with status 302 and a body that is not a Jeff error: moved",
            Assert.IsType<EngineError>(error).Message);
        Assert.True(elapsed < 200);
        Assert.Equal(new[] { "/v1/functions" }, paths);
    }

    [Fact]
    public async Task AnAnswerThatIsNotHttpIsABrokenConnectionNotAnUnreachableServer()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var serving = Task.Run(async () =>
        {
            using var socket = await listener.AcceptSocketAsync();
            await socket.SendAsync(Encoding.ASCII.GetBytes("this is not HTTP\r\n\r\n"), SocketFlags.None);
            socket.Shutdown(SocketShutdown.Both);
        });
        string url = $"http://127.0.0.1:{((IPEndPoint)listener.LocalEndpoint).Port}";
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => JeffClient.RemoteAsync(url, null, Options(2000)));
        Assert.StartsWith($"The connection to the Jeff server at {url} failed during GET /v1/functions: ", error.Message);
        await serving;
    }

    [Fact]
    public async Task ClosingAbortsARequestInFlightWithTheClosedError()
    {
        await using var server = await HttpServer.StartAsync((context, _) => Task.Delay(Timeout.Infinite, context.RequestAborted));
        var transport = new RestTransport(server.Url, null, TimeSpan.FromSeconds(5));
        var pending = transport.FunctionsAsync(default);
        await Task.Delay(50);
        await transport.DisposeAsync();
        Assert.Equal($"This remote client of {server.Url} is closed; connect again with JeffClient.RemoteAsync",
            (await Assert.ThrowsAsync<InvalidRequest>(() => pending)).Message);
        Assert.Equal(0, transport.InFlight);
        await Assert.ThrowsAsync<InvalidRequest>(() => transport.FunctionsAsync(default));
    }

    [Fact]
    public void PoliciesTheServerRunsAreSentAndOnesWithAnLlmStepAreRunHere()
    {
        Assert.Equal("\"return\"", Json.Write(Remote.ServerPolicy(Policy.Return)));
        Assert.Equal("{\"escalate\":[\"a.b.c\"],\"budget_ms\":50}",
            Json.Write(Remote.ServerPolicy(Policy.Escalate([Steps.Function("a.b.c")], TimeSpan.FromMilliseconds(50)))));
        Assert.Null(Remote.ServerPolicy(Policy.Escalate(Steps.Function("a.b.c"), Steps.Llm((_, _) => Task.FromResult<string?>("x")))));
    }

    [Fact]
    public void MoreCallsThanOneRequestCarriesAreRefusedNotSplit()
    {
        Remote.CheckBatch(Remote.MaxCalls, "calls");
        Assert.Equal("257 calls in one request: a Jeff server answers at most 256 per request; send them in batches of at most 256",
            Assert.Throws<InvalidRequest>(() => Remote.CheckBatch(Remote.MaxCalls + 1, "calls")).Message);
    }

    [Fact]
    public async Task ARegisteredGraphWithAnLlmNodePolicyIsRefusedBeforeAnyRequest()
    {
        int requests = 0;
        await using var server = await HttpServer.StartAsync((context, _) =>
        {
            Interlocked.Increment(ref requests);
            return HttpServer.Json(context, 200, Listing);
        });
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        var error = await Assert.ThrowsAsync<InvalidPolicy>(() => jeff.RunServerGraphAsync("g", "x", new JsonObject(),
            new Dictionary<string, Policy> { ["n"] = Policy.Escalate(Steps.Llm((_, _) => Task.FromResult<string?>("1"))) }));
        Assert.StartsWith("Node n: a graph registered on the server runs there", error.Message);
        Assert.Equal(1, requests);
    }

    /// <summary>A server on 127.0.0.1 that speaks raw bytes: each accepted connection goes to `serve`; Accepted counts them.</summary>
    private sealed class RawServer : IAsyncDisposable
    {
        private readonly TcpListener listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource stop = new();
        private readonly Task accepting;
        private readonly List<Task> serving = new();
        private int accepted;

        public RawServer(Func<Socket, Task> serve)
        {
            listener.Start();
            Url = $"http://127.0.0.1:{((IPEndPoint)listener.LocalEndpoint).Port}";
            accepting = Task.Run(async () =>
            {
                while (!stop.IsCancellationRequested)
                {
                    Socket socket;
                    try
                    {
                        socket = await listener.AcceptSocketAsync(stop.Token);
                    }
                    catch (OperationCanceledException)
                    {
                        return;
                    }
                    Interlocked.Increment(ref accepted);
                    var task = Task.Run(async () =>
                    {
                        using (socket) await serve(socket);
                    });
                    lock (serving) serving.Add(task);
                }
            });
        }

        public string Url { get; }

        public int Accepted => Volatile.Read(ref accepted);

        public async ValueTask DisposeAsync()
        {
            stop.Cancel();
            listener.Stop();
            await accepting;
            // a serve that failed (an assert, a socket error) fails the test here instead of being lost
            List<Task> all;
            lock (serving) all = serving.ToList();
            await Task.WhenAll(all).WaitAsync(TimeSpan.FromSeconds(5));
        }
    }

    /// <summary>Reads one HTTP/1.1 request (head and Content-Length body) from the socket; null when it closed first.</summary>
    private static async Task<string?> ReadRequest(Socket socket, MemoryStream pending)
    {
        var buffer = new byte[64 * 1024];
        while (true)
        {
            string seen = Encoding.ASCII.GetString(pending.ToArray());
            int end = seen.IndexOf("\r\n\r\n", StringComparison.Ordinal);
            if (end >= 0)
            {
                var length = seen[..end].Split("\r\n").Select(line => line.Split(':', 2)).Where(parts => parts.Length == 2
                    && parts[0].Trim().Equals("content-length", StringComparison.OrdinalIgnoreCase)).Select(parts => int.Parse(parts[1].Trim())).FirstOrDefault();
                if (pending.Length >= end + 4 + length)
                {
                    var rest = pending.ToArray()[(end + 4 + length)..];
                    pending.SetLength(0);
                    pending.Write(rest);
                    return seen[..end];
                }
            }
            int count = await socket.ReceiveAsync(buffer, SocketFlags.None);
            if (count == 0) return null;
            pending.Write(buffer, 0, count);
        }
    }

    private static byte[] Answer(string status, string body, string headers = "") =>
        Encoding.UTF8.GetBytes($"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {Encoding.UTF8.GetByteCount(body)}\r\n{headers}\r\n{body}");

    [Fact]
    public async Task AConnectionThatDoesNotOpenInTimeIsBackendUnavailablePromptly()
    {
        int connects = 0;
        await using var transport = new RestTransport("http://127.0.0.1:1", null, TimeSpan.FromMilliseconds(200), async (_, cancellationToken) =>
        {
            Interlocked.Increment(ref connects);
            await Task.Delay(Timeout.Infinite, cancellationToken);
            throw new InvalidOperationException("unreachable");
        }, RestTransport.KeepaliveExpiry);
        // how late this machine runs a timer now: "promptly" is the 200 ms timeout plus at most a few times that
        double late = 0;
        for (int round = 0; round < 5; round++)
        {
            long started = Stopwatch.GetTimestamp();
            await Task.Delay(50);
            late = Math.Max(late, Stopwatch.GetElapsedTime(started).TotalMilliseconds - 50);
        }
        var (elapsed, error) = await Rejection(() => transport.FunctionsAsync(default));
        Assert.Equal("Cannot reach the Jeff server at http://127.0.0.1:1: no connection within 200 ms", Assert.IsType<BackendUnavailable>(error).Message);
        Assert.InRange(elapsed, 190, 200 + Math.Max(200, 4 * late));
        Assert.Equal(1, connects);
        Assert.Equal(0, transport.InFlight);
    }

    [Fact]
    public void AServerUrlWithABadPortNoHostUserInfoAQueryOrAFragmentIsRefusedNamingIt()
    {
        string Refusal(string url) => Assert.Throws<InvalidRequest>(() => new RestTransport(url, null, TimeSpan.FromSeconds(1))).Message;
        Assert.Equal("The server URL 'http://h:99999' has an invalid port '99999'", Refusal("http://h:99999"));
        Assert.Equal("The server URL 'http://h:65536/jeff' has an invalid port '65536'", Refusal("http://h:65536/jeff"));
        Assert.Equal("The server URL 'http://h:abc' has an invalid port 'abc'", Refusal("http://h:abc"));
        Assert.Equal("The server URL 'http://h:-1' has an invalid port '-1'", Refusal("http://h:-1"));
        Assert.Equal("The server URL 'http://' has no host", Refusal("http://"));
        Assert.Equal("The server URL 'https://:8080' has no host", Refusal("https://:8080"));
        Assert.Equal("The server URL 'http://user:pw@h:1' must not carry a user name or password; pass the token instead", Refusal("http://user:pw@h:1"));
        Assert.Equal("The server URL 'http://h/x?q=1' must not have a query (the part after '?')", Refusal("http://h/x?q=1"));
        Assert.Equal("The server URL 'http://h/#top' must not have a fragment (the part after '#')", Refusal("http://h/#top"));
        Assert.Equal("http://h:65535/jeff", new RestTransport("http://h:65535/jeff/", null, TimeSpan.FromSeconds(1)).Url);
        Assert.Equal("http://h:", new RestTransport("http://h:/", null, TimeSpan.FromSeconds(1)).Url);
        Assert.Equal("https://[::1]:8443", new RestTransport("https://[::1]:8443", null, TimeSpan.FromSeconds(1)).Url);
    }

    [Fact]
    public async Task AConnectionIdleLongerThanTheKeepaliveExpiryIsNotReused()
    {
        Assert.Equal(TimeSpan.FromSeconds(5), RestTransport.KeepaliveExpiry);
        var connections = new HashSet<string>();
        await using var server = await HttpServer.StartAsync((context, _) =>
        {
            lock (connections) connections.Add(context.Connection.Id);
            return HttpServer.Json(context, 200, Listing);
        });
        await using var transport = new RestTransport(server.Url, null, TimeSpan.FromSeconds(2), (endpoint, cancellationToken) =>
            new ValueTask<Stream>(ConnectAsync(endpoint, cancellationToken)), TimeSpan.FromMilliseconds(300));
        foreach (int pause in new[] { 0, 30, 2500 })
        {
            await Task.Delay(pause);
            await transport.FunctionsAsync(default);
        }
        // the second request reused the first connection (idle 30 ms); the third found it expired (idle 2500 ms: .NET's
        // pool checks the idle time on a clock of about a second, so the pause is well past it; at 5 s it was found exact)
        Assert.Equal(2, connections.Count);
    }

    private static async Task<Stream> ConnectAsync(DnsEndPoint endpoint, CancellationToken cancellationToken)
    {
        var socket = new Socket(SocketType.Stream, ProtocolType.Tcp) { NoDelay = true };
        await socket.ConnectAsync(endpoint, cancellationToken);
        return new NetworkStream(socket, ownsSocket: true);
    }

    [Fact]
    public async Task AKeptAliveConnectionClosedBeforeTheAnswerIsABrokenConnectionAndTheRequestIsNotSentAgain()
    {
        int posts = 0;
        await using var server = new RawServer(async socket =>
        {
            var pending = new MemoryStream();
            while (await ReadRequest(socket, pending) is { } head)
            {
                if (head.StartsWith("GET", StringComparison.Ordinal))
                {
                    await socket.SendAsync(Answer("200 OK", Listing), SocketFlags.None);
                    continue;
                }
                // the POST was read and the server goes away without answering: it may have run the request
                Interlocked.Increment(ref posts);
                socket.Shutdown(SocketShutdown.Both);
                return;
            }
        });
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => jeff.DecideAsync([new Call("spam.is_spam", "Hi", new JsonObject { ["channel"] = "sms" })]));
        Assert.StartsWith($"The connection to the Jeff server at {server.Url} failed during POST /v1/decide: HttpRequestException: ", error.Message);
        Assert.EndsWith("(IOException: The server closed the connection without answering; the request is not sent again, as the server may have run it)", error.Message);
        await Task.Delay(100);
        Assert.Equal(1, posts);
        Assert.Equal(1, server.Accepted);
    }

    [Fact]
    public async Task AGetOnAKeptAliveConnectionClosedBeforeTheAnswerIsNotSentAgainEither()
    {
        int gets = 0;
        await using var server = new RawServer(async socket =>
        {
            var pending = new MemoryStream();
            while (await ReadRequest(socket, pending) is not null)
            {
                if (Interlocked.Increment(ref gets) == 1)
                {
                    await socket.SendAsync(Answer("200 OK", Listing), SocketFlags.None);
                    continue;
                }
                socket.Shutdown(SocketShutdown.Both);
                return;
            }
        });
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => jeff.Engine.InstalledAsync(true));
        Assert.StartsWith($"The connection to the Jeff server at {server.Url} failed during GET /v1/functions: HttpRequestException: ", error.Message);
        Assert.EndsWith("(IOException: The server closed the connection without answering; the request is not sent again, as the server may have run it)", error.Message);
        await Task.Delay(100);
        Assert.Equal((2, 1), (gets, server.Accepted));
    }

    [Fact]
    public async Task AnAnswerCutOffBeforeItsLengthIsABrokenConnection()
    {
        await using var server = new RawServer(async socket =>
        {
            await ReadRequest(socket, new MemoryStream());
            await socket.SendAsync(Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"base_"), SocketFlags.None);
            socket.Shutdown(SocketShutdown.Both);
        });
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => JeffClient.RemoteAsync(server.Url, null, Options(2000)));
        Assert.StartsWith($"The connection to the Jeff server at {server.Url} failed during GET /v1/functions: HttpIOException: The response ended prematurely", error.Message);
    }

    [Fact]
    public async Task AnswersThatAreNotJsonNotAnObjectOrNotTheSchemaAreNamedErrorsWithTheRequestId()
    {
        string body = "";
        await using var server = await HttpServer.StartAsync((context, _) => HttpServer.Json(context, 200, body, "rid-7"));
        async Task<JeffError> Refusal(string answer)
        {
            body = answer;
            return (JeffError)(await Rejection(() => JeffClient.RemoteAsync(server.Url, null, Options(2000)))).Error!;
        }
        var notJson = await Refusal("<html>oops</html>");
        Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with status 200 and a body that is not JSON: <html>oops</html>",
            Assert.IsType<EngineError>(notJson).Message);
        Assert.Equal("rid-7", notJson.RequestId);
        Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with JSON that is not an object: [1]",
            Assert.IsType<EngineError>(await Refusal("[1]")).Message);
        var schema = Assert.IsType<ContractError>(await Refusal("{\"functions\": []}"));
        Assert.StartsWith($"The answer of {server.Url} to GET /v1/functions does not match contract/schemas/functions_response: ", schema.Message);
        Assert.Equal("rid-7", schema.RequestId);
        var longBody = new string('x', 400);
        Assert.EndsWith(": " + new string('x', 300), (await Refusal(longBody)).Message);
    }

    [Fact]
    public async Task AnAnswerWithAnotherNumberOfDecisionsThanCallsIsAContractError()
    {
        var decision = RecordedDecision();
        await using var server = await HttpServer.StartAsync((context, _) => context.Request.Method == "GET"
            ? HttpServer.Json(context, 200, Listing)
            : HttpServer.Json(context, 200, Json.Write(new JsonObject { ["decisions"] = new JsonArray(Json.Canonical(decision), Json.Canonical(decision)) })));
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        var error = await Assert.ThrowsAsync<ContractError>(() => jeff.RateAsync("x", new ShorthandOptions { Function = "jeff.support.triage.urgency", Context = Ctx() }));
        Assert.Equal($"{server.Url} answered 2 decisions for 1 calls", error.Message);
    }

    [Fact]
    public async Task ACallerCancellingGetsTheCancellationAndNothingStaysInFlight()
    {
        await using var server = await HttpServer.StartAsync((context, _) => Task.Delay(Timeout.Infinite, context.RequestAborted));
        await using var transport = new RestTransport(server.Url, null, TimeSpan.FromSeconds(5));
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(100));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => transport.FunctionsAsync(cancel.Token));
        Assert.Equal(0, transport.InFlight);
    }

    [Fact]
    public async Task EachWaitForMoreBodyIsTimedSeparatelySoASlowButSteadyAnswerArrives()
    {
        await using var server = await HttpServer.StartAsync(async (context, _) =>
        {
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            string listing = Listing;
            int part = listing.Length / 4 + 1;
            for (int start = 0; start < listing.Length; start += part)
            {
                await context.Response.WriteAsync(listing.Substring(start, Math.Min(part, listing.Length - start)));
                await context.Response.Body.FlushAsync();
                await Task.Delay(150);
            }
        });
        // four pauses of 150 ms: 600 ms in all, but no single wait reaches the 400 ms timeout
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(400));
        Assert.Contains("jeff.support.triage", await jeff.Engine.InstalledAsync());
    }

    [Fact]
    public async Task AKeptAliveConnectionResetBeforeTheAnswerIsABrokenConnectionAndTheRequestIsNotSentAgain()
    {
        int gets = 0;
        await using var server = new RawServer(async socket =>
        {
            var pending = new MemoryStream();
            while (await ReadRequest(socket, pending) is not null)
            {
                if (Interlocked.Increment(ref gets) == 1)
                {
                    await socket.SendAsync(Answer("200 OK", Listing), SocketFlags.None);
                    continue;
                }
                socket.LingerState = new LingerOption(true, 0); // closing now sends a reset
                socket.Close();
                return;
            }
        });
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => jeff.Engine.InstalledAsync(true));
        Assert.StartsWith($"The connection to the Jeff server at {server.Url} failed during GET /v1/functions: ", error.Message);
        await Task.Delay(100);
        Assert.Equal((2, 1), (gets, server.Accepted));
    }

    /// <summary>The timings of the two slot tests below, from what this machine measures now: `count` requests at once
    /// to `server` (answering with no delay) through a fresh transport, cold connections and all, after a warm-up round. The longest of them,
    /// `overhead`, is how much a loaded machine adds to a request; the margin is three times that (at least 300 ms),
    /// the server's delay two margins and the timeout three, so every comparison the tests make (a wait of one delay
    /// within the timeout, a wait of two delays past it) holds by a whole margin.</summary>
    private static async Task<(int Delay, int Timeout, double Overhead)> SlotTimings(HttpServer server, int count)
    {
        // a first round warms the server and the code paths up (as they are warm for the run measured); the second, on
        // a fresh transport so its connections are cold as the run's are, is the one measured
        await using (var warm = new RestTransport(server.Url, null, TimeSpan.FromSeconds(60)))
        {
            await Task.WhenAll(Enumerable.Range(0, count).Select(_ => Rejection(() => warm.FunctionsAsync(default))));
        }
        await using var probe = new RestTransport(server.Url, null, TimeSpan.FromSeconds(60));
        var measured = await Task.WhenAll(Enumerable.Range(0, count).Select(_ => Rejection(() => probe.FunctionsAsync(default))));
        Assert.All(measured, outcome => Assert.Null(outcome.Error));
        double overhead = measured.Max(outcome => outcome.Ms);
        int margin = Math.Max(300, (int)Math.Ceiling(3 * overhead));
        return (2 * margin, 3 * margin, overhead);
    }

    [Fact]
    public async Task MoreRequestsThanConnectionSlotsWaitForASlotWithoutSpendingTheirAnswerTime()
    {
        int answered = 0, delay = 0;
        await using var server = await HttpServer.StartAsync(async (context, _) =>
        {
            await Task.Delay(Volatile.Read(ref delay));
            Interlocked.Increment(ref answered);
            await HttpServer.Json(context, 200, Listing);
        });
        var (delayMs, timeoutMs, overhead) = await SlotTimings(server, 150);
        Volatile.Write(ref answered, 0);
        Volatile.Write(ref delay, delayMs);
        await using var transport = new RestTransport(server.Url, null, TimeSpan.FromMilliseconds(timeoutMs));
        // 150 at once: 50 wait about one delay (2 margins) for a slot, then get their own timeout (3 margins) for the
        // answer; counted together their wait (4 margins) would pass the timeout
        var outcomes = await Task.WhenAll(Enumerable.Range(0, 150).Select(_ => Rejection(() => transport.FunctionsAsync(default))));
        Assert.True(outcomes.All(outcome => outcome.Error is null), $"delay {delayMs} ms, timeout {timeoutMs} ms, measured overhead {overhead:F0} ms: "
            + string.Join("; ", outcomes.Where(outcome => outcome.Error is not null).Select(outcome => outcome.Error!.Message).Distinct()));
        Assert.Equal(150, answered);
        Assert.True(outcomes.Max(outcome => outcome.Ms) > timeoutMs, "no request waited for a slot");
    }

    [Fact]
    public async Task ARequestWaitingLongerThanTheTimeoutForASlotIsDeadlineExceededAndNeverSent()
    {
        int received = 0, delay = 0;
        await using var server = await HttpServer.StartAsync(async (context, _) =>
        {
            Interlocked.Increment(ref received);
            await Task.Delay(Volatile.Read(ref delay));
            await HttpServer.Json(context, 200, Listing);
        });
        var (delayMs, timeoutMs, overhead) = await SlotTimings(server, 300);
        Volatile.Write(ref received, 0);
        Volatile.Write(ref delay, delayMs);
        await using var transport = new RestTransport(server.Url, null, TimeSpan.FromMilliseconds(timeoutMs));
        // three rounds of 100: the second waits about one delay (2 margins) for a slot, within the pool timeout (3
        // margins), the third about two delays (4 margins), past it (as in httpx)
        var outcomes = await Task.WhenAll(Enumerable.Range(0, 300).Select(_ => Rejection(() => transport.FunctionsAsync(default))));
        var failed = outcomes.Where(outcome => outcome.Error is not null).ToList();
        Assert.True(failed.Count is >= 50 and <= 100, $"{failed.Count} failed; delay {delayMs} ms, timeout {timeoutMs} ms, measured overhead {overhead:F0} ms");
        Assert.All(failed, outcome => Assert.Equal(
            $"The Jeff server at {server.Url} did not answer GET /v1/functions within {timeoutMs} ms; the server may still have run the request",
            Assert.IsType<DeadlineExceeded>(outcome.Error).Message));
        Assert.Equal(300 - failed.Count, received);
    }

    [Fact]
    public async Task ConnectionSlotsAreHandedOutInArrivalOrderAndACancelledWaiterLeavesTheQueue()
    {
        var gate = new FifoGate(1);
        Assert.True(gate.WaitAsync(default).IsCompletedSuccessfully);
        using var cancel = new CancellationTokenSource();
        var first = gate.WaitAsync(default);
        var cancelled = gate.WaitAsync(cancel.Token);
        var second = gate.WaitAsync(default);
        cancel.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => cancelled);
        gate.Release();
        await first.WaitAsync(TimeSpan.FromSeconds(1));
        Assert.False(second.IsCompleted);
        gate.Release();
        await second.WaitAsync(TimeSpan.FromSeconds(1));
        gate.Release();
        Assert.True(gate.WaitAsync(default).IsCompletedSuccessfully);
    }

    [Fact]
    public async Task ARequestGivenAnotherConnectionThanTheOneItAskedForIsTimedOnThatOneAndTheAttemptEndsOnItsOwn()
    {
        int attempts = 0;
        var attemptEnded = new TaskCompletionSource<double>();
        long started = Stopwatch.GetTimestamp();
        await using var server = await HttpServer.StartAsync(async (context, _) =>
        {
            await Task.Delay(300);
            await HttpServer.Json(context, 200, Listing);
        });
        await using var transport = new RestTransport(server.Url, null, TimeSpan.FromMilliseconds(500), async (endpoint, cancellationToken) =>
        {
            if (Interlocked.Increment(ref attempts) == 1) return await ConnectAsync(endpoint, cancellationToken);
            try
            {
                await Task.Delay(Timeout.Infinite, cancellationToken);
            }
            finally
            {
                attemptEnded.TrySetResult(Stopwatch.GetElapsedTime(started).TotalMilliseconds);
            }
            throw new InvalidOperationException("unreachable");
        }, RestTransport.KeepaliveExpiry);
        var one = transport.FunctionsAsync(default);
        await Task.Delay(50);
        // the second asks for a connection that never opens, then is given the first one's when it comes free at
        // about 300 ms; its answer comes about 300 ms later, within its own 500 ms
        var two = transport.FunctionsAsync(default);
        await one;
        await two;
        Assert.Equal(2, attempts);
        Assert.InRange(await attemptEnded.Task.WaitAsync(TimeSpan.FromSeconds(2)), 500, 900);
    }

    private static async Task<HttpServer> Raw(int status, byte[] body, params (string Name, string Value)[] headers) =>
        await HttpServer.StartAsync(async (context, _) =>
        {
            context.Response.StatusCode = status;
            foreach (var (name, value) in headers) context.Response.Headers[name] = value;
            await context.Response.Body.WriteAsync(body);
        });

    [Fact]
    public async Task AJsonAnswerIsParsedFromItsBytesAsJsonLoadsParsesThem()
    {
        // s09: a byte order mark before the listing, which json.loads(bytes) accepts
        var bom = Encoding.UTF8.GetPreamble().Concat(Encoding.UTF8.GetBytes(Listing)).ToArray();
        await using (var server = await Raw(200, bom, ("Content-Type", "application/json")))
        {
            await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        }
        // UTF-16 without a byte order mark, found by its zero bytes as json.detect_encoding finds it
        await using (var server = await Raw(200, Encoding.BigEndianUnicode.GetBytes(Listing), ("Content-Type", "application/json")))
        {
            await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        }
        // s11: an invalid UTF-8 byte means the body is not JSON, so not a Jeff error; the excerpt replaces the byte
        var bad = Encoding.UTF8.GetBytes("{\"error\": {\"type\": \"EngineError\", \"message\": \"bad \xff here\", \"details\": {}}}")
            .Select(item => item == 0xC3 ? (byte)0xFF : item).Where(item => item != 0xBF).ToArray();
        await using (var server = await Raw(500, bad, ("Content-Type", "application/json")))
        {
            var error = await Assert.ThrowsAsync<EngineError>(() => JeffClient.RemoteAsync(server.Url, null, Options(2000)));
            Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with status 500 and a body that is not a Jeff error: "
                + "{\"error\": {\"type\": \"EngineError\", \"message\": \"bad \uFFFD here\", \"details\": {}}}", error.Message);
        }
        // s17: the excerpt is decoded with the Content-Type's charset
        await using (var server = await Raw(503, Encoding.Latin1.GetBytes("café down"), ("Content-Type", "text/plain; charset=\"ISO-8859-1\"")))
        {
            var error = await Assert.ThrowsAsync<EngineError>(() => JeffClient.RemoteAsync(server.Url, null, Options(2000)));
            Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with status 503 and a body that is not a Jeff error: café down", error.Message);
        }
    }

    private static byte[] Compressed(Func<Stream, Stream> open, string text)
    {
        using var output = new MemoryStream();
        using (var compressing = open(output)) compressing.Write(Encoding.UTF8.GetBytes(text));
        return output.ToArray();
    }

    [Fact]
    public async Task ACompressedAnswerIsDecodedAsHttpxDecodesItAndACorruptOneCouldNotBeRead()
    {
        string? accepted = null;
        // s10: gzip, which httpx asks for and decodes; deflate both zlib-wrapped and raw, as httpx's DeflateDecoder takes both
        foreach (var (encoding, body) in new[]
        {
            ("gzip", Compressed(output => new System.IO.Compression.GZipStream(output, System.IO.Compression.CompressionLevel.Fastest), Listing)),
            ("deflate", Compressed(output => new System.IO.Compression.ZLibStream(output, System.IO.Compression.CompressionLevel.Fastest), Listing)),
            ("deflate", Compressed(output => new System.IO.Compression.DeflateStream(output, System.IO.Compression.CompressionLevel.Fastest), Listing)),
            ("br, GZIP", Compressed(output => new System.IO.Compression.GZipStream(output, System.IO.Compression.CompressionLevel.Fastest), Listing)),
        })
        {
            await using var server = await HttpServer.StartAsync(async (context, _) =>
            {
                accepted = context.Request.Headers.AcceptEncoding.ToString();
                context.Response.Headers.ContentEncoding = encoding;
                context.Response.ContentType = "application/json";
                await context.Response.Body.WriteAsync(body);
            });
            await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        }
        Assert.Equal("gzip, deflate", accepted);
        await using (var server = await Raw(200, Encoding.ASCII.GetBytes("not gzip at all"), ("Content-Encoding", "gzip"), ("x-request-id", "rid-9")))
        {
            var error = await Assert.ThrowsAsync<EngineError>(() => JeffClient.RemoteAsync(server.Url, null, Options(2000)));
            Assert.StartsWith($"The answer of the Jeff server at {server.Url} to GET /v1/functions could not be read: DecodingError: ", error.Message);
            Assert.Null(error.RequestId);
        }
    }

    /// <summary>contract/schemas/functions_response allows any string as a function id; Python takes the adapter as
    /// id.rsplit(".", 1)[0], which is the whole id when it has no '.'.</summary>
    [Fact]
    public async Task AListedFunctionIdWithNoDotIsTakenAsItsOwnAdapterAsPythonDoes()
    {
        var listing = (JsonObject)Json.Parse(Listing, "the recorded listing")!;
        var first = listing["functions"]![0]!.AsObject();
        first["id"] = "nodot";
        await using var server = await HttpServer.StartAsync((context, _) => HttpServer.Json(context, 200, Json.Write(listing)));
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(2000));
        Assert.Contains("nodot", await jeff.Engine.InstalledAsync());
    }

    private static byte[] Gzip(byte[] data)
    {
        using var output = new MemoryStream();
        using (var gzip = new System.IO.Compression.GZipStream(output, System.IO.Compression.CompressionLevel.SmallestSize)) gzip.Write(data);
        return output.ToArray();
    }

    /// <summary>A few kilobytes of stacked gzip that decode to more than the 64 MiB an answer may hold: refused by name
    /// before it is decoded in full, and the client is closed.</summary>
    [Fact]
    public async Task AStackedGzipBombIsRefusedByNameAndClosesTheClient()
    {
        var bomb = Gzip(Gzip(new byte[PyHttpBody.MaxBody + 1]));
        Assert.True(bomb.Length < 10_000, $"the bomb is {bomb.Length} bytes");
        int requests = 0;
        await using var server = await HttpServer.StartAsync(async (context, path) =>
        {
            if (Interlocked.Increment(ref requests) == 1)
            {
                await HttpServer.Json(context, 200, Listing);
                return;
            }
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            context.Response.Headers.ContentEncoding = "gzip, gzip";
            await context.Response.Body.WriteAsync(bomb);
        });
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(10_000));
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => jeff.Engine.InstalledAsync(true));
        Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with a body that decodes (Content-Encoding) to more than 67108864 bytes, "
            + "the most this client reads; the client is closed, connect again with JeffClient.RemoteAsync", error.Message);
        Assert.Equal($"This remote client of {server.Url} is closed; connect again with JeffClient.RemoteAsync",
            (await Assert.ThrowsAsync<InvalidRequest>(() => jeff.Engine.InstalledAsync(true))).Message);
    }

    [Fact]
    public void ADecodedBodyAtTheLimitIsReadAndEachLayerIsCapped()
    {
        var exact = new byte[1000];
        Assert.Equal(1000, PyHttpBody.Decoded(Gzip(Gzip(exact)), ["gzip, gzip"], 1000).Length);
        Assert.True(Assert.Throws<BodyTooLarge>(() => PyHttpBody.Decoded(Gzip(new byte[1001]), ["gzip"], 1000)).Decoded);
        // the outer layer is small, the inner one is past the limit
        Assert.Throws<BodyTooLarge>(() => PyHttpBody.Decoded(Gzip(Gzip(new byte[1001])), ["gzip", "gzip"], 1000));
    }

    [Fact]
    public async Task ABodyLongerThanTheLimitAsSentIsRefusedByNameAndClosesTheClient()
    {
        int requests = 0;
        await using var server = await HttpServer.StartAsync(async (context, path) =>
        {
            if (Interlocked.Increment(ref requests) == 1)
            {
                await HttpServer.Json(context, 200, Listing);
                return;
            }
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            var chunk = new byte[1024 * 1024];
            for (int sent = 0; sent <= PyHttpBody.MaxBody; sent += chunk.Length) await context.Response.Body.WriteAsync(chunk, context.RequestAborted);
        });
        await using var jeff = await JeffClient.RemoteAsync(server.Url, null, Options(10_000));
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => jeff.Engine.InstalledAsync(true));
        Assert.Equal($"The Jeff server at {server.Url} answered GET /v1/functions with a body of more than 67108864 bytes, the most this client reads; "
            + "the client is closed, connect again with JeffClient.RemoteAsync", error.Message);
    }
}
