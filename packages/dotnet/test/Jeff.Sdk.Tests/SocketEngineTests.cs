using System.Diagnostics;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using Jeff.Sdk.Tests.Helpers;

namespace Jeff.Sdk.Tests;

public class SocketEngineTests
{
    private static JsonObject Ctx() => new() { ["company"] = "A shop.", ["channel"] = "email" };

    /// <summary>The level named by the message's last character, as a certain score.</summary>
    private static IReadOnlyList<double> LevelOf(string adapter, JsonObject row)
    {
        string message = row["state"]!["message"]!.GetValue<string>();
        int level = message[^1] - '0';
        return Enumerable.Range(0, 5).Select(index => index == level ? 1.0 : 0.0).ToList();
    }

    private static Row Noul() => new(JsonValue.Create("x"), new RowQuestion(QuestionType.Noul, "?", null));

    [Fact]
    public async Task LocalDecidesOverTheEngineSocket()
    {
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"]);
        await using var jeff = await JeffClient.LocalAsync(server.Path, new LocalOptions { Timeout = TimeSpan.FromSeconds(5) });
        var decision = (await jeff.DecideAsync([new Call("triage.urgency", "level 3", Ctx())]))[0];
        Assert.Equal(3L, decision.Value.GetValue<long>());
    }

    [Fact]
    public async Task ConcurrentCallersShareOneConnectionAndEachGetsItsOwnAnswer()
    {
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"]);
        await using var jeff = await JeffClient.LocalAsync(server.Path, new LocalOptions { Timeout = TimeSpan.FromSeconds(5) });
        var inputs = Enumerable.Range(0, 40).Select(index => $"case {index % 5}").ToList();
        var decisions = await Task.WhenAll(inputs.Select(async input => (await jeff.DecideAsync([new Call("triage.urgency", input, Ctx())]))[0]));
        Assert.Equal(inputs.Select(input => (long)(input[^1] - '0')), decisions.Select(decision => decision.Value.GetValue<long>()));
    }

    [Fact]
    public async Task NoEngineOnTheSocketNamesThePathAndTheCommandThatStartsOne()
    {
        var error = await Assert.ThrowsAsync<EngineError>(() => JeffClient.LocalAsync("/tmp/jeff-no-such-engine.sock", new LocalOptions { Timeout = TimeSpan.FromSeconds(1) }));
        Assert.Matches("^No engine is listening at /tmp/jeff-no-such-engine\\.sock: .*Start one with: jeff runtime serve --socket /tmp/jeff-no-such-engine\\.sock",
            error.Message);
        await Assert.ThrowsAsync<InvalidRequest>(() => JeffClient.LocalAsync("/tmp/x.sock", new LocalOptions { Timeout = TimeSpan.Zero }));
    }

    [Fact]
    public async Task ACallWithoutAReplyWithinTheTimeoutFailsPromptlyNamingTheEngine()
    {
        await using var server = EngineServer.Start((_, _) => [1, 0], ["jeff.safety.spam"]);
        await using var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromMilliseconds(200));
        server.StopAnswering();
        var started = Stopwatch.GetTimestamp();
        var error = await Assert.ThrowsAsync<EngineError>(() => engine.PredictAsync("jeff.safety.spam", [Noul()]));
        var elapsed = Stopwatch.GetElapsedTime(started).TotalMilliseconds;
        Assert.Equal($"No reply from the engine at {server.Path} within 200 ms", error.Message);
        Assert.InRange(elapsed, 190, 400);
    }

    [Fact]
    public async Task AfterTheEngineRestartsTheNextCallReconnectsOnceAndACallRightAfterAFailedAttemptFailsAtOnce()
    {
        using var paths = SocketPaths.New();
        var first = EngineServer.Start((_, _) => [0.2, 0.8], ["jeff.safety.spam"], paths.Path);
        await using var engine = await SocketEngine.ConnectAsync(paths.Path, TimeSpan.FromSeconds(2));
        await first.DisposeAsync();
        await Task.Delay(50);
        Assert.Contains("(reconnect attempt)", (await Assert.ThrowsAsync<EngineError>(() => engine.PredictAsync("jeff.safety.spam", [Noul()]))).Message);
        Assert.Contains("the last reconnect attempt", (await Assert.ThrowsAsync<EngineError>(() => engine.PredictAsync("jeff.safety.spam", [Noul()]))).Message);
        await Task.Delay(1100);
        await using var second = EngineServer.Start((_, _) => [0.2, 0.8], ["jeff.safety.spam"], paths.Path);
        Assert.Equal(new[] { 0.2, 0.8 }, (await engine.PredictAsync("jeff.safety.spam", [Noul()])).Probabilities[0]);
        Assert.Equal(1, engine.Reconnects);
    }

    [Fact]
    public async Task AClosedEngineRefusesLaterCallsAndClosingTwiceDoesNothing()
    {
        await using var server = EngineServer.Start((_, _) => [1, 0], []);
        var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(1));
        await engine.DisposeAsync();
        await engine.DisposeAsync();
        Assert.Contains("was closed by its caller", (await Assert.ThrowsAsync<EngineError>(() => engine.HealthAsync())).Message);
        Assert.True(engine.ReadLoop.IsCompleted);
    }

    /// <summary>An engine that writes `onHello` after reading the hello and `onInstalled` after reading an installed
    /// request; it counts connections and records each op it reads.</summary>
    private sealed class RawEngine : IAsyncDisposable
    {
        private readonly SocketPaths paths = SocketPaths.New();
        private readonly Socket listener = new(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        private readonly Task accepting;
        public readonly List<string> Ops = new();
        public int Connections;

        public RawEngine(byte[] onHello, Func<long, byte[]>? onInstalled = null)
        {
            listener.Bind(new UnixDomainSocketEndPoint(paths.Path));
            listener.Listen();
            accepting = AcceptAsync(onHello, onInstalled);
        }

        public string Path => paths.Path;

        private async Task AcceptAsync(byte[] onHello, Func<long, byte[]>? onInstalled)
        {
            while (true)
            {
                Socket socket;
                try
                {
                    socket = await listener.AcceptAsync();
                }
                catch (Exception error) when (error is SocketException or ObjectDisposedException)
                {
                    return; // the test closed the listener
                }
                Interlocked.Increment(ref Connections);
                _ = Task.Run(async () =>
                {
                    using var connection = socket;
                    var reader = new FrameReader();
                    var buffer = new byte[4096];
                    while (true)
                    {
                        int count;
                        try
                        {
                            count = await connection.ReceiveAsync(buffer, SocketFlags.None);
                        }
                        catch (Exception error) when (error is SocketException or ObjectDisposedException)
                        {
                            return; // the client ended the connection
                        }
                        if (count == 0) return;
                        foreach (var body in reader.Push(buffer.AsSpan(0, count)))
                        {
                            var message = MsgPack.Decode(body)!.AsObject();
                            string op = message["op"]!.GetValue<string>();
                            lock (Ops) Ops.Add(op);
                            if (op == "hello") await connection.SendAsync(onHello, SocketFlags.None);
                            else if (op == "installed" && onInstalled is not null) await connection.SendAsync(onInstalled(message["id"]!.GetValue<long>()), SocketFlags.None);
                        }
                    }
                });
            }
        }

        public async ValueTask DisposeAsync()
        {
            listener.Dispose();
            await accepting;
            paths.Dispose();
        }
    }

    private static readonly byte[] EngineHello = EngineServer.Frame(new JsonObject { ["op"] = "hello", ["version"] = 2L, ["adapters_version"] = 1L });

    private static byte[] IdZeroFailure() => EngineServer.Frame(new JsonObject
    {
        ["id"] = 0L, ["ok"] = false,
        ["error"] = new JsonObject { ["type"] = "InvalidRequest", ["message"] = "Frame could not be read", ["details"] = new JsonObject { ["adapters_version"] = 1L } },
    });

    // In these two tests the failure frame reaches the reading task while the connect is already sending its next
    // request, so the connect fails with the failure itself (InvalidRequest) when that request was registered first, or
    // with the EngineError naming it when the failure came first. Either way it fails at once, names the failure, and
    // never reconnects.
    private static void AssertFailedWithTheFrameError(JeffError error, string path)
    {
        Assert.True(error is InvalidRequest || error.Message == $"The engine at {path} closed the connection after: InvalidRequest: Frame could not be read",
            $"unexpected {error.Type}: {error.Message}");
        Assert.EndsWith("Frame could not be read", error.Message);
    }

    [Fact]
    public async Task AFailureWithIdZeroInTheSameBytesAsTheHelloFailsTheConnectWithoutAReconnect()
    {
        await using var engine = new RawEngine([.. EngineHello, .. IdZeroFailure()]);
        var error = await Assert.ThrowsAnyAsync<JeffError>(() => SocketEngine.ConnectAsync(engine.Path, TimeSpan.FromSeconds(2)));
        AssertFailedWithTheFrameError(error, engine.Path);
        Assert.Equal(1, engine.Connections);
    }

    [Fact]
    public async Task AConnectionThatClosesWhileConnectingFailsTheConnectAtOnceWithoutAReconnect()
    {
        byte[] InstalledReply(long id) =>
        [
            .. EngineServer.Frame(new JsonObject
            {
                ["id"] = id, ["ok"] = true, ["result"] = new JsonObject { ["adapters"] = new JsonArray("base"), ["adapters_version"] = 1L },
            }),
            .. IdZeroFailure(),
        ];
        await using var engine = new RawEngine(EngineHello, InstalledReply);
        var error = await Assert.ThrowsAnyAsync<JeffError>(() => SocketEngine.ConnectAsync(engine.Path, TimeSpan.FromSeconds(2)));
        AssertFailedWithTheFrameError(error, engine.Path);
        Assert.Equal(new[] { "hello", "installed" }, engine.Ops.Take(2));
        Assert.Equal(1, engine.Connections);
    }

    [Fact]
    public async Task InstalledVersionAsksTheEngineForItsVersionAndListInOneReply()
    {
        await using var server = EngineServer.Start((_, _) => [0.3, 0.7], ["jeff.safety.spam"]);
        await using var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(5));
        var (version, installed) = await engine.InstalledVersionAsync();
        Assert.Equal(1L, version);
        Assert.Equal(new[] { "base", "jeff.safety.spam" }, installed.Order(StringComparer.Ordinal));
        Assert.Equal(1L, engine.AdaptersVersion);
    }

    private static JsonValue PastALong() => Json.Whole(System.Numerics.BigInteger.Pow(2, 64) - 1);

    [Fact]
    public async Task InputTokensPastALongAreAnEngineErrorNamingThem()
    {
        await using var server = EngineServer.Start((_, _) => [0.3, 0.7], ["jeff.safety.spam"]);
        await using var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(5));
        server.Rewrite = reply =>
        {
            if (reply["result"]?["probabilities"] is not null) reply["result"]!["input_tokens"] = new JsonArray(PastALong());
            return reply;
        };
        var error = await Assert.ThrowsAsync<EngineError>(() => engine.PredictAsync("jeff.safety.spam", [Noul()]));
        Assert.Equal("the engine's predict reply has input_tokens [18446744073709551615]; each must be a whole number from -2**63 to 2**63-1", error.Message);
    }

    [Fact]
    public async Task AReplyIdPastALongEndsTheConnectionWithAnEngineErrorNamingIt()
    {
        await using var server = EngineServer.Start((_, _) => [0.3, 0.7], ["jeff.safety.spam"]);
        await using var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(5));
        server.Rewrite = reply =>
        {
            reply["id"] = PastALong();
            return reply;
        };
        var error = await Assert.ThrowsAsync<EngineError>(() => engine.PredictAsync("jeff.safety.spam", [Noul()]));
        Assert.Contains("the engine sent a message whose request id 18446744073709551615 is outside -2**63 to 2**63-1", error.Message);
        Assert.DoesNotContain("InvalidOperationException", error.Message);
    }

    [Fact]
    public void ATimeoutOverTheLongestTimerIsRefused()
    {
        Timeouts.Check(Timeouts.Longest);
        Assert.Equal("Timeout must be at most 2147483647 milliseconds (about 24.8 days), the longest .NET's timers keep; got 2147483648 ms",
            Assert.Throws<InvalidRequest>(() => Timeouts.Check(TimeSpan.FromMilliseconds(2147483648))).Message);
        Assert.Throws<InvalidRequest>(() => Timeouts.Check(Timeout.InfiniteTimeSpan));
    }

    [Fact]
    public async Task ACancelledCallLeavesTheConnectionServingOthers()
    {
        await using var server = EngineServer.Start((_, _) => [0.3, 0.7], ["jeff.safety.spam"], predictDelay: TimeSpan.FromMilliseconds(300));
        await using var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(5));
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(50));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => engine.PredictAsync("jeff.safety.spam", [Noul()], cancel.Token));
        Assert.Equal(new[] { 0.3, 0.7 }, (await engine.PredictAsync("jeff.safety.spam", [Noul()])).Probabilities[0]);
        await Task.Delay(400); // the cancelled call's late reply arrives and is dropped
        Assert.Equal(new[] { 0.3, 0.7 }, (await engine.PredictAsync("jeff.safety.spam", [Noul()])).Probabilities[0]);
    }

    /// <summary>A context that never runs what is posted to it, as a UI thread blocked on a task never does.</summary>
    private sealed class NeverPumpedContext : SynchronizationContext
    {
        public override void Post(SendOrPostCallback callback, object? state)
        {
        }
    }

    [Fact]
    public async Task ABlockingCallerOnASingleThreadedContextDoesNotDeadlock()
    {
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"]);
        await using var jeff = await JeffClient.LocalAsync(server.Path, new LocalOptions { Timeout = TimeSpan.FromSeconds(5) });
        Decision? decision = null;
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            SynchronizationContext.SetSynchronizationContext(new NeverPumpedContext());
            try
            {
                decision = jeff.DecideAsync([new Call("triage.urgency", "level 4", Ctx())]).GetAwaiter().GetResult()[0];
            }
            catch (Exception error)
            {
                failure = error;
            }
        }) { IsBackground = true };
        thread.Start();
        Assert.True(thread.Join(TimeSpan.FromSeconds(10)), "the blocked caller never got its decision: an await inside the SDK waits for the caller's context");
        Assert.Null(failure);
        Assert.Equal(4L, decision!.Value.GetValue<long>());
    }

    [Fact]
    public async Task AWaitingCallCompletesAndAnUndisposedEngineDoesNotKeepTheProcessAlive()
    {
        await using var server = EngineServer.Start((_, _) => [0.3, 0.7], ["jeff.safety.spam"], predictDelay: TimeSpan.FromMilliseconds(300));
        string program = Path.Combine(AppContext.BaseDirectory, "Jeff.Sdk.ProcessCheck.dll");
        Assert.True(File.Exists(Path.Combine(AppContext.BaseDirectory, "Jeff.Sdk.ProcessCheck.runtimeconfig.json")), "the process check was not built next to the tests");
        var start = new ProcessStartInfo("dotnet") { RedirectStandardOutput = true, RedirectStandardError = true };
        start.ArgumentList.Add(program);
        start.ArgumentList.Add(server.Path);
        using var process = Process.Start(start)!;
        var output = process.StandardOutput.ReadToEndAsync();
        Assert.True(process.WaitForExit(TimeSpan.FromSeconds(20)), "the process did not end: something the SDK holds keeps it alive");
        Assert.Equal("[[0.3,0.7]]", (await output).Trim());
        Assert.Equal(0, process.ExitCode);
    }

    [Fact]
    public async Task AnEngineThatClosesFailsEveryPendingCallPromptlyNamingIt()
    {
        var server = EngineServer.Start((_, _) => [0.3, 0.7], ["jeff.safety.spam"], predictDelay: TimeSpan.FromSeconds(30));
        await using var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(20));
        var calls = Enumerable.Range(0, 20).Select(_ => engine.PredictAsync("jeff.safety.spam", [Noul()])).ToList();
        await Task.Delay(100);
        var started = Stopwatch.GetTimestamp();
        string path = server.Path;
        await server.DisposeAsync();
        foreach (var call in calls)
        {
            Assert.Equal($"The engine at {path} closed the connection", (await Assert.ThrowsAsync<EngineError>(() => call)).Message);
        }
        Assert.InRange(Stopwatch.GetElapsedTime(started).TotalMilliseconds, 0, 2000);
    }

    [Fact]
    public async Task DisposingFailsEveryPendingCallAndLeavesNoUnobservedFailure()
    {
        var unobserved = new List<Exception>();
        void Record(object? sender, UnobservedTaskExceptionEventArgs args)
        {
            lock (unobserved) unobserved.AddRange(args.Exception.InnerExceptions.Where(error => error.Message.Contains("e.sock", StringComparison.Ordinal)));
        }
        TaskScheduler.UnobservedTaskException += Record;
        try
        {
            await using (var server = EngineServer.Start((_, _) => [0.3, 0.7], ["jeff.safety.spam"], predictDelay: TimeSpan.FromSeconds(30)))
            {
                var engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(20));
                // a call that times out (this fake engine never answers stats), whose wait is abandoned
                Assert.Equal($"No reply from the engine at {server.Path} within 50 ms",
                    (await Assert.ThrowsAsync<EngineError>(() => engine.StatsAsync(TimeSpan.FromMilliseconds(50)))).Message);
                var calls = Enumerable.Range(0, 10).Select(_ => engine.PredictAsync("jeff.safety.spam", [Noul()])).ToList();
                await Task.Delay(100);
                await engine.DisposeAsync();
                foreach (var call in calls)
                {
                    Assert.Equal($"The SocketEngine for {server.Path} was closed by its caller", (await Assert.ThrowsAsync<EngineError>(() => call)).Message);
                }
                Assert.True(engine.ReadLoop.IsCompletedSuccessfully);
            }
            // a handshake that gives up before the engine says hello, then the engine closing
            using var paths = SocketPaths.New();
            using (var silent = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified))
            {
                silent.Bind(new UnixDomainSocketEndPoint(paths.Path));
                silent.Listen();
                var accepting = silent.AcceptAsync();
                Assert.Equal($"No hello from the engine at {paths.Path} within 100 ms",
                    (await Assert.ThrowsAsync<EngineError>(() => SocketEngine.ConnectAsync(paths.Path, TimeSpan.FromMilliseconds(100)))).Message);
                (await accepting).Dispose();
            }
            await Task.Delay(200);
            GC.Collect();
            GC.WaitForPendingFinalizers();
            GC.Collect();
            lock (unobserved) Assert.Empty(unobserved);
        }
        finally
        {
            TaskScheduler.UnobservedTaskException -= Record;
        }
    }
}
