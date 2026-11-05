using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json.Nodes;
using Jeff.Sdk.Testing;
using Jeff.Sdk.Tests.Helpers;

namespace Jeff.Sdk.Tests;

/// <summary>RemoteAsync with Channel = true (port of the Node SDK's channel tests, against a fake channel server: there
/// are no recorded channel exchanges).</summary>
public class ChannelTests
{
    private static JsonObject Ctx() => new() { ["company"] = "A shop.", ["channel"] = "email" };

    private static List<string> Ops(ChannelServer server)
    {
        lock (server.Received) return server.Received.Select(message => message["op"]!.GetValue<string>()).ToList();
    }

    private static string Jwt(long exp)
    {
        static string Part(string json) => Convert.ToBase64String(Encoding.UTF8.GetBytes(json)).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        return $"{Part("{\"alg\":\"EdDSA\"}")}.{Part($"{{\"sub\":\"alice\",\"exp\":{exp}}}")}.c2ln";
    }

    private static long Now() => DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    private static async Task<ChannelJeffClient> Open(string url, TokenSource? token, int timeoutMs = 2000) =>
        (ChannelJeffClient)await JeffClient.RemoteAsync(url, token, new RemoteOptions { Timeout = TimeSpan.FromMilliseconds(timeoutMs), Channel = true });

    private static Task<ChannelJeffClient> Open(string url) => Open(url, TokenSource.Of("secret-token"));

    private static Call Urgency(string input) => new("triage.urgency", input, Ctx());

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

    private static async Task Until(Func<bool> condition)
    {
        var deadline = Stopwatch.GetTimestamp() + Stopwatch.Frequency * 5;
        while (!condition())
        {
            if (Stopwatch.GetTimestamp() > deadline) throw new TimeoutException("condition not met in time");
            await Task.Delay(10);
        }
    }

    [Fact]
    public void TheChannelUrlFollowsTheServerUrlAndAJwtsExpIsReadWithoutVerifyingIt()
    {
        Assert.Equal("ws://h:1/jeff/v1/channel", ChannelProtocol.Url("http://h:1/jeff/"));
        Assert.Equal("wss://h/v1/channel", ChannelProtocol.Url("https://h"));
        Assert.Equal("The server URL 'ftp://h' must start with http:// or https://", Assert.Throws<InvalidRequest>(() => ChannelProtocol.Url("ftp://h")).Message);
        Assert.Null(ChannelProtocol.TokenExpiry("an-api-key"));
        Assert.Equal(1234.0, ChannelProtocol.TokenExpiry(Jwt(1234)));
        // the message names only that the payload is not JSON, never quoting back part of the token
        Assert.Equal("The token is shaped like a JWT, but its payload is not base64-encoded JSON",
            Assert.Throws<Unauthorised>(() => ChannelProtocol.TokenExpiry("a.!!!.c")).Message);
        string noExp = $"a.{Convert.ToBase64String(Encoding.UTF8.GetBytes("{\"sub\":\"alice\"}")).TrimEnd('=')}.c";
        Assert.Equal("The token is a JWT without a numeric exp (got None); the server needs one", Assert.Throws<Unauthorised>(() => ChannelProtocol.TokenExpiry(noExp)).Message);
    }

    [Fact]
    public async Task ABoundFunctionSendsOneBindThenOnlyCallFramesAndDisposingReleasesItsHandle()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        Assert.Equal(2L, (await bound.CallAsync("Fire")).Value.GetValue<long>());
        Assert.Equal(new[] { 2L, 2L }, (await bound.ManyAsync([JsonValue.Create("a"), JsonValue.Create("b")])).Select(decision => decision.Value.GetValue<long>()));
        await bound.DisposeAsync();
        await Assert.ThrowsAsync<InvalidRequest>(() => bound.CallAsync("again"));
        Assert.Equal(new[] { "hello", "auth", "bind", "call", "call_many", "release" }, Ops(server));
        JsonAssert.SameContent(new JsonObject
        {
            ["op"] = "bind", ["function"] = "jeff.support.triage.urgency", ["on_low_confidence"] = "return", ["context"] = Ctx(), ["id"] = 2L,
        }, server.Received[2]);
        Assert.Equal(new[] { "Bearer secret-token" }, server.Headers);
    }

    [Fact]
    public async Task WithoutATokenThereIsNoAuthAndNoAuthorizationHeader()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url, null);
        await jeff.DecideAsync([Urgency("x")]);
        Assert.Equal(new[] { "hello", "decide" }, Ops(server));
        Assert.Equal(new[] { "" }, server.Headers);
    }

    [Fact]
    public async Task ATokenTheServerRefusesIsUnauthorisedAtConnectAndTheMessageNeverShowsAToken()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Token = "the-key" });
        var error = await Assert.ThrowsAsync<Unauthorised>(() => Open(server.Url, TokenSource.Of("the-kez")));
        Assert.DoesNotContain("the-kez", error.Message);
        await using var jeff = await Open(server.Url, TokenSource.Of("the-key"));
        Assert.Equal(2L, (await jeff.DecideAsync([Urgency("x")]))[0].Value.GetValue<long>());
    }

    [Fact]
    public async Task TheBindCarriesThePolicyTheServerRunsAndOneWithAnLlmStepIsBoundAsReturnAndRunHere()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url);
        await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx(), OnLowConfidence = Policy.Escalate(Steps.Function("triage.urgency")) });
        await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx(), OnLowConfidence = Policy.Escalate(Steps.Llm((_, _) => Task.FromResult<string?>("3"))) });
        var binds = server.Received.Where(message => message["op"]!.GetValue<string>() == "bind").Select(message => Json.Write(message["on_low_confidence"]));
        Assert.Equal(new[] { "{\"escalate\":[\"jeff.support.triage.urgency\"]}", "\"return\"" }, binds);
    }

    [Fact]
    public async Task ABindAnsweredWithAnotherAdapterVersionIsRefusedAndItsHandleReleasedBeforeTheNextRequest()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Version = "9.9.9" });
        await using var jeff = await Open(server.Url);
        string local = jeff.Catalogue.Adapter("jeff.support.triage").Version;
        var error = await Assert.ThrowsAsync<ContractError>(() => jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() }));
        Assert.Equal($"jeff.support.triage: the server serves version 9.9.9, this client's catalogue has {local}; pass catalogue= with the server's manifests", error.Message);
        await jeff.DecideAsync([Urgency("x")]);
        Assert.Equal(new[] { "hello", "auth", "bind", "release", "decide" }, Ops(server));
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static async Task BindAndDropAsync(ChannelJeffClient jeff) => await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });

    [Fact]
    public async Task ABoundFunctionTheGarbageCollectorTakesHasItsHandleReleasedBeforeTheNextRequest()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url);
        await BindAndDropAsync(jeff);
        // The bind's reply resumed this method inline, under the stack frames that returned the bound function: yield so
        // they unwind before collecting.
        await Task.Yield();
        GC.Collect();
        GC.WaitForPendingFinalizers();
        GC.Collect();
        await jeff.DecideAsync([Urgency("x")]);
        Assert.Equal(new[] { "release", "decide" }, Ops(server).TakeLast(2));
        jeff.Channel.ReleaseLater(99);
        await jeff.DecideAsync([Urgency("y")]);
        Assert.Equal(new[] { "release", "decide" }, Ops(server).TakeLast(2));
    }

    [Fact]
    public async Task AFailureTheServerSendsCarriesTheConnectionsIdAsChannelId()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        await jeff.Channel.ReleaseAsync(1, default);
        var error = await Assert.ThrowsAsync<InvalidRequest>(() => bound.CallAsync("x"));
        Assert.Equal("handle 1 is not bound", error.Message);
        JsonAssert.Equal(new JsonObject { ["channel_id"] = "0badc0de" }, error.Details);
        Assert.Null(error.RequestId);
    }

    [Fact]
    public async Task ALowConfidenceWhoseDecisionDoesNotMatchTheSchemaIsAContractErrorNotALowConfidence()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { BadLowConfidenceDecision = true });
        await using var jeff = await Open(server.Url);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        await Assert.ThrowsAsync<ContractError>(() => bound.CallAsync("x"));
    }

    [Fact]
    public async Task AFunctionTokenThatIsAJwtIsRefreshedBeforeItsExp()
    {
        await using var server = await ChannelServer.StartAsync();
        long now = Now();
        string[] tokens = [Jwt(now + 61), Jwt(now + 3600)];
        int calls = 0;
        // the handshake header and the auth read the first token; GET /v1/functions and the refresh read the second
        await using var jeff = await Open(server.Url, TokenSource.From(_ => ValueTask.FromResult<string?>(Interlocked.Increment(ref calls) <= 2 ? tokens[0] : tokens[1])));
        await Task.Delay(1600);
        var refresh = server.Received.Single(message => message["op"]!.GetValue<string>() == "refresh");
        Assert.Equal(tokens[1], refresh["token"]!.GetValue<string>());
    }

    [Fact]
    public async Task AStringTokenIsNeverRefreshedEvenAJwtInsideTheRefreshWindow()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url, TokenSource.Of(Jwt(Now() + 30)));
        await Task.Delay(300);
        Assert.Equal(new[] { "hello", "auth" }, Ops(server));
    }

    [Fact]
    public async Task ATokenFunctionThatNeverRenewsClosesTheChannelAtExpWithUnauthorisedCarryingEveryFailure()
    {
        await using var server = await ChannelServer.StartAsync();
        string fixedToken = Jwt(Now() + 2);
        var warnings = new StringWriter();
        var before = Console.Error;
        Console.SetError(warnings);
        try
        {
            await using var jeff = await Open(server.Url, TokenSource.From(_ => ValueTask.FromResult<string?>(fixedToken)));
            await Task.Delay(3200);
            var error = await Assert.ThrowsAsync<Unauthorised>(() => jeff.DecideAsync([Urgency("x")]));
            Assert.Matches("^The channel's token expired at \\d+ before the token function returned one", error.Message);
            Assert.True(error.Notes.Count >= 2);
            Assert.All(error.Notes, note => Assert.Contains("not after the channel's current token", note));
            Assert.Equal(error.Notes.Count, warnings.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries).Length);
            Assert.DoesNotContain(fixedToken, warnings.ToString());
            Assert.DoesNotContain("refresh", Ops(server));
        }
        finally
        {
            Console.SetError(before);
        }
    }

    [Fact]
    public async Task AnUnansweredRequestIsDeadlineExceededPromptlyItsLateReplyIsDroppedAndTheChannelStaysOpen()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["decide"] });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 300);
        var (elapsed, error) = await Rejection(() => jeff.DecideAsync([Urgency("x")]));
        Assert.Equal($"The channel {ChannelProtocol.Url(server.Url)} did not answer 'decide' within 300 ms; the server may still have run the request",
            Assert.IsType<DeadlineExceeded>(error).Message);
        Assert.InRange(elapsed, 290, 1000);
        var late = server.Received.Single(message => message["op"]!.GetValue<string>() == "decide");
        await server.SendAsync(new JsonObject { ["id"] = late["id"]!.GetValue<long>(), ["ok"] = true, ["result"] = new JsonObject { ["decisions"] = new JsonArray() } });
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        Assert.Equal(2L, (await bound.CallAsync("Fire")).Value.GetValue<long>());
    }

    [Fact]
    public async Task ACancelledRequestLeavesTheChannelOpen()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["decide"] });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 5000);
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(50));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => jeff.DecideAsync([Urgency("x")], cancel.Token));
        Assert.False(jeff.Channel.Closed);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        Assert.Equal(2L, (await bound.CallAsync("Fire")).Value.GetValue<long>());
    }

    [Fact]
    public async Task AServerThatNeverAnswersTheOpeningHandshakeIsBackendUnavailablePromptly()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var accepted = listener.AcceptSocketAsync();
        string url = $"http://127.0.0.1:{((IPEndPoint)listener.LocalEndpoint).Port}";
        var (elapsed, error) = await Rejection(() => Open(url, TokenSource.Of("secret-token"), 200));
        Assert.Equal($"Cannot reach the Jeff server at {url}: opening {ChannelProtocol.Url(url)} took more than 200 ms", Assert.IsType<BackendUnavailable>(error).Message);
        Assert.InRange(elapsed, 190, 900);
        (await accepted).Dispose();
    }

    [Fact]
    public async Task AServerThatSendsNoHelloIsBackendUnavailablePromptly()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["hello"] });
        var (elapsed, error) = await Rejection(() => Open(server.Url, TokenSource.Of("secret-token"), 200));
        Assert.Equal($"No hello from the channel {ChannelProtocol.Url(server.Url)} within 200 ms", Assert.IsType<BackendUnavailable>(error).Message);
        Assert.True(elapsed < 900);
    }

    [Fact]
    public async Task AServerRefusingTheWebSocketIsAnEngineErrorNamingTheHttpStatus()
    {
        await using var server = await ChannelServer.StartAsync();
        string elsewhere = $"{server.Url}/elsewhere";
        var error = await Assert.ThrowsAsync<EngineError>(() => Open(elsewhere));
        Assert.Equal($"The Jeff server at {elsewhere} refused to open the channel {ChannelProtocol.Url(elsewhere)}: HTTP status 400", error.Message);
    }

    [Fact]
    public async Task AReplyForAnIdNobodySentEndsTheChannelWithEngineError()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url);
        await server.SendAsync(new JsonObject { ["id"] = 999L, ["ok"] = true, ["result"] = new JsonObject() });
        await Task.Delay(50);
        var error = await Assert.ThrowsAsync<EngineError>(() => jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() }));
        Assert.Equal($"The channel {ChannelProtocol.Url(server.Url)} sent a reply for id 999, which no request is waiting for; the channel is closed", error.Message);
    }

    [Fact]
    public async Task AFailureWithIdZeroEndsTheChannelWithTheErrorItNames()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url);
        await server.SendAsync(new JsonObject
        {
            ["id"] = 0L, ["ok"] = false,
            ["error"] = new JsonObject { ["type"] = "Unauthorised", ["message"] = "the channel's token expired", ["details"] = new JsonObject { ["channel_id"] = "0badc0de" } },
        });
        await Task.Delay(50);
        var error = await Assert.ThrowsAsync<Unauthorised>(() => jeff.DecideAsync([Urgency("x")]));
        Assert.Equal("the channel's token expired", error.Message);
        JsonAssert.Equal(new JsonObject { ["channel_id"] = "0badc0de" }, error.Details);
    }

    [Fact]
    public async Task TheServerGoingAwayFailsWaitingAndLaterRequestsWithBackendUnavailable()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["decide"] });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 5000);
        var waiting = jeff.DecideAsync([Urgency("x")]);
        await Task.Delay(50);
        server.DropConnections();
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => waiting);
        Assert.Equal($"{ChannelProtocol.Url(server.Url)}: the channel closed (no close frame received)", error.Message);
        await Assert.ThrowsAsync<BackendUnavailable>(() => jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() }));
    }

    [Fact]
    public async Task ClosingEndsTheChannelWithACloseFrameAndLaterRequestsAreInvalidRequest()
    {
        await using var server = await ChannelServer.StartAsync();
        var jeff = await Open(server.Url);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        await jeff.DisposeAsync();
        Assert.Equal($"This remote client of {server.Url} is closed; connect again with JeffClient.RemoteAsync",
            (await Assert.ThrowsAsync<InvalidRequest>(() => jeff.DecideAsync([Urgency("x")]))).Message);
        await Assert.ThrowsAsync<InvalidRequest>(() => bound.CallAsync("x"));
        await bound.DisposeAsync(); // nothing to release: the handles ended with the connection
        await jeff.DisposeAsync();
        Assert.Equal(new[] { "hello", "auth", "bind" }, Ops(server));
        Assert.Null(jeff.Channel.CloseProblem);
    }

    private static JsonObject WithoutTiming(Decision decision)
    {
        var json = decision.ToJson();
        json.Remove("timing");
        return json;
    }

    [Fact]
    public async Task ConcurrentRequestsShareOneChannelAndEachGetsItsOwnAnswerWhateverOrderTheRepliesComeIn()
    {
        var inputs = Enumerable.Range(0, 160).Select(index => $"message {index}{(index % 3 == 0 ? " unsure" : "")}").ToList();
        var local = await JeffClient.CreateAsync(new FakeEngine(ChannelServer.Answers, ChannelServer.Installed), Catalogue.Shipped());
        var expectedBound = await local.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        var expected = new Dictionary<string, JsonObject>();
        foreach (var input in inputs) expected[input] = WithoutTiming(await expectedBound.CallAsync(input));
        Assert.Equal(2, expected.Values.Select(decision => decision["abstained"]!.GetValue<bool>()).Distinct().Count());
        // 160 calls, 2 call_many and 2 decide: every reply is held until all 164 are ready, then sent in reverse order
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Reorder = 164 });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 10000);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        var calls = Task.WhenAll(inputs.Select(input => bound.CallAsync(input)));
        var many = Task.WhenAll(bound.ManyAsync(inputs.Take(5).Select(input => (JsonNode?)JsonValue.Create(input)).ToList()),
            bound.ManyAsync(inputs.Skip(5).Take(7).Select(input => (JsonNode?)JsonValue.Create(input)).ToList()));
        var decided = Task.WhenAll(new[] { 12, 13 }.Select(index => jeff.DecideAsync([Urgency(inputs[index])])));
        await Task.WhenAll(calls, many, decided);
        for (int index = 0; index < inputs.Count; index++) JsonAssert.Equal(expected[inputs[index]], WithoutTiming((await calls)[index]));
        var flat = (await many).SelectMany(list => list).ToList();
        for (int index = 0; index < 12; index++) JsonAssert.Equal(expected[inputs[index]], WithoutTiming(flat[index]));
        JsonAssert.Equal(expected[inputs[12]], WithoutTiming((await decided)[0][0]));
        JsonAssert.Equal(expected[inputs[13]], WithoutTiming((await decided)[1][0]));
        Assert.Single(server.Headers);
        var ids = server.Received.Where(message => message["op"]!.GetValue<string>() != "hello").Select(message => message["id"]!.GetValue<long>()).ToList();
        Assert.Equal(ids.Count, ids.Distinct().Count());
        Assert.Equal(2 + 164, ids.Count); // auth and bind, then the requests above
    }

    [Fact]
    public async Task RequestsWaitingWhenTheChannelDropsEachFailOnceWithTheirOwnCopyAndSoDoesEveryLaterOne()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Gated = ["call"] });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 5000);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        var waiting = Enumerable.Range(0, 4).Select(index => Rejection(() => bound.CallAsync($"pending {index}"))).ToList();
        await Until(() => server.Waiting == 4);
        server.DropConnections();
        string closed = $"{ChannelProtocol.Url(server.Url)}: the channel closed (no close frame received)";
        var errors = (await Task.WhenAll(waiting)).Select(outcome => outcome.Error).ToList();
        Assert.All(errors, error => Assert.Equal(closed, Assert.IsType<BackendUnavailable>(error).Message));
        Assert.Equal(4, errors.Distinct(ReferenceEqualityComparer.Instance).Count());
        Assert.Equal(closed, (await Assert.ThrowsAsync<BackendUnavailable>(() => bound.CallAsync("later"))).Message);
        server.OpenGate();
    }

    [Fact]
    public async Task The257thOutstandingRequestIsRateLimitedAndTheChannelStaysOpen()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Gated = ["call"] });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 10000);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        var results = Enumerable.Range(0, 257).Select(async index =>
        {
            try
            {
                return (object)(await bound.CallAsync($"call {index}")).Value.GetValue<long>();
            }
            catch (RateLimited limited)
            {
                return limited;
            }
        }).ToList();
        await Until(() => server.Waiting == 256);
        await Until(() => results.Any(result => result.IsCompleted));
        server.OpenGate();
        var settled = await Task.WhenAll(results);
        var refused = settled.OfType<RateLimited>().ToList();
        Assert.Single(refused);
        Assert.Equal(1.0, refused[0].RetryAfterS);
        Assert.Equal("0badc0de", refused[0].Details["channel_id"]!.GetValue<string>());
        Assert.Equal(Enumerable.Repeat((object)2L, 256), settled.Where(value => value is not RateLimited));
        Assert.False(jeff.Channel.Closed);
        Assert.Equal(2L, (await bound.CallAsync("after")).Value.GetValue<long>());
    }

    // ---- beyond the brief: Python's token decoding, the frame limits, the writer

    /// <summary>JWT payloads (the middle part) and what jeff.channel_client.token_expiry gives for each (generated with
    /// the Python SDK: a number, "payload" for the not-base64-JSON Unauthorised, "exp" for the no-numeric-exp one).</summary>
    private static readonly string[][] PythonTokenExpiries =
    [
        ["eyJleHAiOjV9", "5.0"],
        ["eyJleHAiOjV9=", "5.0"],
        ["eyJleHAiOjV9==", "5.0"],
        ["eyJ=leHAiOjV9", "5.0"],
        ["ey!JleHAiOjV9", "5.0"],
        ["eyJleHAiOjV9 ", "5.0"],
        ["-yJl-HAiOjV9", "payload"],
        ["eyJleHAiOjV", "payload"],
        ["eyJleHAiOjV9A", "payload"],
        ["=eyJleHAiOjV9", "5.0"],
        ["eyJleHAiOjEuNX0", "1.5"],
        ["eyJleHAiOjEuNX0=", "1.5"],
        ["eyJleHAiOjEuNX0==", "1.5"],
        ["eyJ=leHAiOjEuNX0", "payload"],
        ["ey!JleHAiOjEuNX0", "payload"],
        ["eyJleHAiOjEuNX0 ", "payload"],
        ["-yJl-HAiOjEuNX0", "payload"],
        ["eyJleHAiOjEuNX", "payload"],
        ["eyJleHAiOjEuNX0A", "payload"],
        ["=eyJleHAiOjEuNX0", "payload"],
        ["eyJleHAiOnRydWV9", "exp"],
        ["eyJleHAiOnRydWV9=", "exp"],
        ["eyJleHAiOnRydWV9==", "exp"],
        ["eyJ=leHAiOnRydWV9", "exp"],
        ["ey!JleHAiOnRydWV9", "exp"],
        ["eyJleHAiOnRydWV9 ", "exp"],
        ["-yJl-HAiOnRydWV9", "payload"],
        ["eyJleHAiOnRydWV", "payload"],
        ["eyJleHAiOnRydWV9A", "payload"],
        ["=eyJleHAiOnRydWV9", "exp"],
        ["eyJleHAiOiI1In0", "exp"],
        ["eyJleHAiOiI1In0=", "exp"],
        ["eyJleHAiOiI1In0==", "exp"],
        ["eyJ=leHAiOiI1In0", "payload"],
        ["ey!JleHAiOiI1In0", "payload"],
        ["eyJleHAiOiI1In0 ", "payload"],
        ["-yJl-HAiOiI1In0", "payload"],
        ["eyJleHAiOiI1In", "payload"],
        ["eyJleHAiOiI1In0A", "payload"],
        ["=eyJleHAiOiI1In0", "payload"],
        ["W10", "exp"],
        ["W10=", "exp"],
        ["W10==", "exp"],
        ["W1!0", "payload"],
        ["W10 ", "payload"],
        ["W1", "payload"],
        ["W10A", "payload"],
        ["=W10", "payload"],
        ["eyJleHAiOm51bGx9", "exp"],
        ["eyJleHAiOm51bGx9=", "exp"],
        ["eyJleHAiOm51bGx9==", "exp"],
        ["eyJ=leHAiOm51bGx9", "exp"],
        ["ey!JleHAiOm51bGx9", "exp"],
        ["eyJleHAiOm51bGx9 ", "exp"],
        ["-yJl-HAiOm51bGx9", "payload"],
        ["eyJleHAiOm51bGx", "payload"],
        ["eyJleHAiOm51bGx9A", "payload"],
        ["=eyJleHAiOm51bGx9", "exp"],
        ["e30", "exp"],
        ["e30=", "exp"],
        ["e30==", "exp"],
        ["e3!0", "payload"],
        ["e30 ", "payload"],
        ["-30", "payload"],
        ["e3", "payload"],
        ["e30A", "payload"],
        ["=e30", "payload"],
        ["77u_eyJleHAiOjd9", "7.0"],
        ["77u_eyJleHAiOjd9=", "7.0"],
        ["77u_eyJleHAiOjd9==", "7.0"],
        ["77u=_eyJleHAiOjd9", "7.0"],
        ["77!u_eyJleHAiOjd9", "7.0"],
        ["77u_eyJleHAiOjd9 ", "7.0"],
        ["77u_-yJl-HAiOjd9", "payload"],
        ["77u_eyJleHAiOjd", "payload"],
        ["77u_eyJleHAiOjd9A", "payload"],
        ["=77u_eyJleHAiOjd9", "7.0"],
        ["eyJleHAiOjEyMzQ1Njc4OTAxfQ", "12345678901.0"],
        ["eyJleHAiOjEyMzQ1Njc4OTAxfQ=", "12345678901.0"],
        ["eyJleHAiOjEyMzQ1Njc4OTAxfQ==", "12345678901.0"],
        ["eyJ=leHAiOjEyMzQ1Njc4OTAxfQ", "payload"],
        ["ey!JleHAiOjEyMzQ1Njc4OTAxfQ", "payload"],
        ["eyJleHAiOjEyMzQ1Njc4OTAxfQ ", "payload"],
        ["-yJl-HAiOjEyMzQ1Njc4OTAxfQ", "payload"],
        ["eyJleHAiOjEyMzQ1Njc4OTAxf", "payload"],
        ["eyJleHAiOjEyMzQ1Njc4OTAxfQA", "payload"],
        ["=eyJleHAiOjEyMzQ1Njc4OTAxfQ", "payload"],
        ["eyJleHAiOi0zfQ", "-3.0"],
        ["eyJleHAiOi0zfQ=", "-3.0"],
        ["eyJleHAiOi0zfQ==", "-3.0"],
        ["eyJ=leHAiOi0zfQ", "payload"],
        ["ey!JleHAiOi0zfQ", "payload"],
        ["eyJleHAiOi0zfQ ", "payload"],
        ["-yJl-HAiOi0zfQ", "payload"],
        ["eyJleHAiOi0zf", "payload"],
        ["eyJleHAiOi0zfQA", "payload"],
        ["=eyJleHAiOi0zfQ", "payload"],
        ["eyJleHAiOjFlM30", "1000.0"],
        ["eyJleHAiOjFlM30=", "1000.0"],
        ["eyJleHAiOjFlM30==", "1000.0"],
        ["eyJ=leHAiOjFlM30", "payload"],
        ["ey!JleHAiOjFlM30", "payload"],
        ["eyJleHAiOjFlM30 ", "payload"],
        ["-yJl-HAiOjFlM30", "payload"],
        ["eyJleHAiOjFlM3", "payload"],
        ["eyJleHAiOjFlM30A", "payload"],
        ["=eyJleHAiOjFlM30", "payload"],
        ["Ingi", "exp"],
        ["Ingi=", "exp"],
        ["Ingi==", "exp"],
        ["Ing=i", "exp"],
        ["In!gi", "exp"],
        ["Ingi ", "exp"],
        ["Ing", "payload"],
        ["IngiA", "payload"],
        ["=Ingi", "exp"],
        ["eyJleHAiOjUsImV4cCI6Nn0", "6.0"],
        ["eyJleHAiOjUsImV4cCI6Nn0=", "6.0"],
        ["eyJleHAiOjUsImV4cCI6Nn0==", "6.0"],
        ["eyJ=leHAiOjUsImV4cCI6Nn0", "payload"],
        ["ey!JleHAiOjUsImV4cCI6Nn0", "payload"],
        ["eyJleHAiOjUsImV4cCI6Nn0 ", "payload"],
        ["-yJl-HAiOjUsImV4cCI6Nn0", "payload"],
        ["eyJleHAiOjUsImV4cCI6Nn", "payload"],
        ["eyJleHAiOjUsImV4cCI6Nn0A", "payload"],
        ["=eyJleHAiOjUsImV4cCI6Nn0", "payload"],
        ["eyJlHAiOjQyfQX", "payload"],
        ["eyleHAiOjQyfQ", "payload"],
        ["eyJleHAiOjQyfbQ", "payload"],
        ["jyhleHQAiOjQyfQ", "payload"],
        ["eyg-leHAiOjQyfQ", "payload"],
        ["eyJleHOjQyQ", "payload"],
        ["eyJleHAiOhjQyfQ", "payload"],
        ["ey4leHAOQyfQ", "payload"],
        ["eyJWjleHAOjQyfQ", "payload"],
        ["eyJleHAiOQyfQ", "payload"],
        ["eyJleHAiOjQufQ", "payload"],
        ["eyJleHAiOj7QyfQ", "payload"],
        ["eyJleHFAiOjQyfQ4", "payload"],
        ["eyJleHAisjQ/0yfQ", "payload"],
        ["eyJFeHAiOeQyf", "payload"],
        ["tyJlYeHAiOQyfQ", "payload"],
        ["eyJleHAiOjQyfQ", "42.0"],
        ["eyJleHEAiOjQyfQ", "payload"],
        ["eyleHAiOjLyfQ", "payload"],
        ["eylieHAiOjQyfQ", "payload"],
        ["oyJleHAiOjQyf", "payload"],
        ["eyJleHAi OjQyf", "payload"],
        ["eyJlHAiOjQyfQ", "payload"],
        ["eyJleHAiOjQUyfQ", "payload"],
        ["eyJleHbtA2OjQyfQ", "payload"],
        ["eyJleHiOjQyfQ", "payload"],
        ["eyJleHALiOjQyfQ", "payload"],
        ["eyJleHAiOAQyfQ", "payload"],
        ["leyJ5eHAijQyfQ", "payload"],
        ["OebyleHAiOjQyfQ", "payload"],
        ["eyJlMeHlAiOjQyfQ", "payload"],
        ["eyJleHFiOQyfQ", "payload"],
        ["pyleHAiOjQyfQ", "payload"],
        ["eyJleHAiQjQyfQ", "payload"],
        ["eyJleHAiOjQyfT", "42.0"],
        ["eyJleAiOjQyfQ", "payload"],
        ["eyJleHAiOjQybfQ", "payload"],
        ["eyJleHAiOjeyfQ", "payload"],
        ["eOJleHGAiOjQy4Q", "payload"],
        ["eyJleHAimvjQyfQ", "payload"]
    ];

    [Fact]
    public void TokenExpiryDecodesThePayloadAsPythonsUrlsafeBase64AndJsonLoadsDo()
    {
        foreach (var row in PythonTokenExpiries)
        {
            string token = $"h.{row[0]}.s";
            switch (row[1])
            {
                case "payload":
                    Assert.Equal("The token is shaped like a JWT, but its payload is not base64-encoded JSON",
                        Assert.Throws<Unauthorised>(() => ChannelProtocol.TokenExpiry(token)).Message);
                    break;
                case "exp":
                    Assert.Contains("without a numeric exp", Assert.Throws<Unauthorised>(() => ChannelProtocol.TokenExpiry(token)).Message);
                    break;
                default:
                    Assert.True(double.Parse(row[1], System.Globalization.CultureInfo.InvariantCulture) == ChannelProtocol.TokenExpiry(token), row[0]);
                    break;
            }
        }
        Assert.Null(ChannelProtocol.TokenExpiry("a.b"));
        Assert.Throws<Unauthorised>(() => ChannelProtocol.TokenExpiry("a.\u00e9.c"));
    }

    [Fact]
    public async Task AFrameOverTheChannelsLimitIsRefusedBeforeAnythingIsSentAndTheChannelStaysOpen()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        var error = await Assert.ThrowsAsync<InvalidRequest>(() => bound.CallAsync(new string('x', 50 * 1024 * 1024)));
        Assert.Matches("^A 'call' frame of \\d+ bytes is over the channel's limit of 52428800 bytes \\(50 MiB\\)$", error.Message);
        Assert.Equal(new[] { "hello", "auth", "bind" }, Ops(server));
        Assert.False(jeff.Channel.Closed);
        Assert.Equal(2L, (await bound.CallAsync("Fire")).Value.GetValue<long>());
    }

    [Fact]
    public async Task AMessageLongerThanOneFrameEndsTheChannelWithBackendUnavailable()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["decide"] });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 5000);
        var waiting = jeff.DecideAsync([Urgency("x")]);
        await Until(() => Ops(server).Contains("decide"));
        await server.SendBytesAsync(new byte[4 + 64 * 1024 * 1024 + 1]);
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => waiting);
        Assert.Equal($"{ChannelProtocol.Url(server.Url)}: the channel closed (the server sent a WebSocket message of more than 67108868 bytes, more than "
            + "one frame of at most 64 MiB)", error.Message);
        Assert.True(jeff.Channel.Closed);
    }

    /// <summary>256 inputs of 120 KB: a call_many frame of about 30 MiB, far more than the buffers between a client and
    /// a server that has stopped reading.</summary>
    /// <summary>How late this machine runs a 50 ms timer now (the worst of five), in milliseconds.</summary>
    private static async Task<double> TimerLateness()
    {
        double late = 0;
        for (int round = 0; round < 5; round++)
        {
            long started = Stopwatch.GetTimestamp();
            await Task.Delay(50);
            late = Math.Max(late, Stopwatch.GetElapsedTime(started).TotalMilliseconds - 50);
        }
        return late;
    }

    private static List<JsonNode?> Big() => Enumerable.Range(0, 256).Select(index => (JsonNode?)JsonValue.Create(new string((char)('a' + index % 26), 120_000))).ToList();

    /// <summary>Makes the server stop reading: a decide it receives (and never answers) then cancelled here.</summary>
    private static async Task StallAsync(ChannelServer server, ChannelJeffClient jeff)
    {
        using var cancel = new CancellationTokenSource();
        var decide = jeff.DecideAsync([Urgency("stall")], cancel.Token);
        await Until(() => Ops(server).Contains("decide"));
        cancel.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => decide);
    }

    [Fact]
    public async Task ASendTheServerStopsTakingEndsTheChannelAfterTimeoutWhileARequestWaitingForItsTurnKeepsItsCancellation()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["decide"], StallAfter = "decide" });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 1000);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        await StallAsync(server, jeff);
        var big = Rejection(() => bound.ManyAsync(Big()));
        await Task.Delay(100);
        // waits for its turn behind the stuck frame, and is cancelled there, having sent nothing
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(100));
        var (cancelledAfter, cancelled) = await Rejection(() => bound.CallAsync("small", cancel.Token));
        Assert.IsAssignableFrom<OperationCanceledException>(cancelled);
        Assert.True(cancelledAfter < 800, $"cancelled after {cancelledAfter} ms");
        Assert.False(jeff.Channel.Closed);
        // the big request has no deadline while it is written; the stall watch ends the channel, and it, by name
        var (bigAfter, bigError) = await big;
        string stalled = $"{ChannelProtocol.Url(server.Url)}: the socket accepted no bytes of a frame for 1000 ms (the server or the network stopped taking data); "
            + "the channel closed";
        Assert.Equal(stalled, Assert.IsType<BackendUnavailable>(bigError).Message);
        Assert.InRange(bigAfter, 950, 3000);
        Assert.True(jeff.Channel.Closed);
        Assert.Equal(stalled, (await Assert.ThrowsAsync<BackendUnavailable>(() => bound.CallAsync("later"))).Message);
        var (closedAfter, _) = await Rejection(() => jeff.DisposeAsync().AsTask());
        Assert.True(closedAfter < 2000, $"closed after {closedAfter} ms");
    }

    [Fact]
    public async Task ARequestWhoseTurnToWriteDoesNotComeWithinTimeoutIsDeadlineExceededAndNeverSent()
    {
        // The server reads slowly but steadily: the big frame's write goes on for many seconds. The timeout also bounds
        // each piece of the frame the socket must accept (the stall watch), and on a loaded machine the server's reads
        // can pause for over a second, so it is 3 s: a pause that long is a stall, a shorter one is load.
        const int timeoutMs = 3000;
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["decide"], SlowAfter = "decide" });
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), timeoutMs);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        await StallAsync(server, jeff);
        var big = Rejection(() => bound.ManyAsync(Big()));
        await Task.Delay(100);
        double late = await TimerLateness();
        var (after, error) = await Rejection(() => bound.CallAsync("small"));
        Assert.Equal($"The channel {ChannelProtocol.Url(server.Url)} could not send 'call' within {timeoutMs} ms: the frames before it were still being "
            + "written; it was not sent", error is DeadlineExceeded ? error.Message : $"{error?.GetType().Name}: {error?.Message} (after {after:F0} ms)");
        Assert.InRange(after, timeoutMs - 50, timeoutMs + Math.Max(1000, 4 * late));
        Assert.False(jeff.Channel.Closed); // the big frame is still being written, slowly and steadily
        Assert.DoesNotContain("call", Ops(server));
        await jeff.DisposeAsync();
        Assert.IsType<InvalidRequest>((await big).Error);
    }

    [Fact]
    public async Task AFrameOnASlowButSteadyLinkTakesAsLongAsItNeedsAndTheTimeoutStartsOnceItIsSent()
    {
        // the reviewer's live case: 6 MiB through a 2 MiB/s uplink with a 1 s timeout, answered as the Python SDK answers it
        await using var server = await ChannelServer.StartAsync();
        await using var uplink = TestProxy.Relay(new Uri(server.Url).Port, 2 * 1024 * 1024);
        await using var jeff = await Open(uplink.Url, TokenSource.Of("secret-token"), 1000);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        var (after, error) = await Rejection(async () => Assert.Equal(2L, (await bound.CallAsync(new string('x', 6 * 1024 * 1024))).Value.GetValue<long>()));
        Assert.Null(error);
        Assert.True(after > 1500, $"answered after {after} ms: the link was not slow");
        Assert.False(jeff.Channel.Closed);
    }

    [Fact]
    public async Task DisposingWhileASendIsStuckAbortsTheSocketWithinTimeout()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { Silent = ["decide"], StallAfter = "decide" });
        var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 1000);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        await StallAsync(server, jeff);
        var big = Rejection(() => bound.ManyAsync(Big()));
        await Task.Delay(100);
        var (closedAfter, _) = await Rejection(() => jeff.DisposeAsync().AsTask());
        Assert.True(closedAfter < 1500, $"closed after {closedAfter} ms");
        Assert.Equal($"This remote client of {server.Url} is closed; connect again with JeffClient.RemoteAsync", Assert.IsType<InvalidRequest>((await big).Error).Message);
        Assert.NotNull(jeff.Channel.CloseProblem);
    }

    [Fact]
    public async Task RequestsCancelledWhileFramesAreBeingWrittenNeverLeaveHalfAMessageOnTheSocket()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var jeff = await Open(server.Url, TokenSource.Of("secret-token"), 10000);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() });
        var random = new Random(13);
        var calls = Enumerable.Range(0, 200).Select(index =>
        {
            var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(random.Next(0, 20)));
            return Rejection(() => bound.CallAsync(new string((char)('a' + index % 26), 300_000), cancel.Token));
        }).ToList();
        var outcomes = await Task.WhenAll(calls);
        Assert.All(outcomes, outcome => Assert.True(outcome.Error is null or OperationCanceledException, outcome.Error?.ToString()));
        Assert.Contains(outcomes, outcome => outcome.Error is OperationCanceledException);
        // every frame the server read was whole (a cut one would have ended its connection), and the channel goes on
        Assert.Equal(2L, (await bound.CallAsync("after")).Value.GetValue<long>());
        Assert.False(jeff.Channel.Closed);
        Assert.True(Ops(server).Count(op => op == "call") <= 201);
    }

    [Fact]
    public async Task ABindHandlePastALongIsAContractErrorNamingTheAnswer()
    {
        await using var server = await ChannelServer.StartAsync(new ChannelServer.Settings { BindHandle = () => Json.Whole(System.Numerics.BigInteger.Pow(2, 64) - 1) });
        await using var jeff = await Open(server.Url);
        var error = await Assert.ThrowsAsync<ContractError>(() => jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx() }));
        Assert.StartsWith($"{server.Url.Replace("http", "ws")}/v1/channel answered bind 'jeff.support.triage.urgency' with {{'handle': 18446744073709551615,", error.Message);
        Assert.EndsWith("expected a handle (a whole number from 1) for 'jeff.support.triage.urgency' and its adapter's version", error.Message);
    }
}
