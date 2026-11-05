using System.Diagnostics;
using System.Net.Sockets;
using System.Runtime.CompilerServices;
using System.Text.Json.Nodes;
using Jeff.Sdk.Tests.Helpers;

namespace Jeff.Sdk.Tests;

/// <summary>The socket client under engines that misbehave: replies out of order, an engine that stops reading, a
/// restart onto another base while the caller cancels, a reconnect that fails as it finishes (from the Task 11 review).</summary>
public class SocketEngineStressTests
{
    private static byte[] F(JsonObject message) => EngineServer.Frame(message);

    private static byte[] HelloF() => F(new JsonObject { ["op"] = "hello", ["version"] = 2L, ["adapters_version"] = 1L });

    private static byte[] InstalledF(long id, long version) => F(new JsonObject
    {
        ["id"] = id, ["ok"] = true, ["result"] = new JsonObject { ["adapters"] = new JsonArray("base", "jeff.safety.spam"), ["adapters_version"] = version },
    });

    private static byte[] HealthF(long id, long version, string format = "bf16", string baseVersion = "v1.3")
    {
        var block = new JsonObject { ["id"] = "jeff-base", ["version"] = baseVersion, ["checkpoint"] = "fake", ["max_options"] = 252L, ["format"] = format };
        if (format != "bf16") block["format_id"] = "q";
        return F(new JsonObject
        {
            ["id"] = id, ["ok"] = true, ["result"] = new JsonObject
            {
                ["status"] = "ready", ["backend"] = "fake", ["adapters"] = new JsonArray("jeff.safety.spam"), ["adapters_version"] = version, ["base"] = block,
            },
        });
    }

    private static byte[] PredictF(long id, double p) => F(new JsonObject
    {
        ["id"] = id, ["ok"] = true, ["result"] = new JsonObject
        {
            ["probabilities"] = new JsonArray(new JsonArray(p, 1 - p)), ["timing"] = new JsonObject { ["path"] = "fake", ["model_ms"] = 0.5, ["format"] = "bf16" },
            ["adapters_version"] = 1L,
        },
    });

    private static byte[] IdZeroFailure() => F(new JsonObject
    {
        ["id"] = 0L, ["ok"] = false,
        ["error"] = new JsonObject { ["type"] = "InvalidRequest", ["message"] = "Frame could not be read", ["details"] = new JsonObject { ["adapters_version"] = 1L } },
    });

    private static Row R(string state) => new(JsonValue.Create(state), new RowQuestion(QuestionType.Noul, "?", null));

    private static long IdOf(JsonObject message) => message["id"]!.GetValue<long>();

    /// <summary>Listens at a fresh path; connection number `index` (from 0) is served by `serve(index, socket)`.</summary>
    private sealed class Fake : IAsyncDisposable
    {
        private readonly SocketPaths paths = SocketPaths.New();
        private readonly Socket listener = new(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        private readonly List<Socket> sockets = new();
        private readonly Task accepting;

        public Fake(Func<int, Socket, Task> serve)
        {
            listener.Bind(new UnixDomainSocketEndPoint(paths.Path));
            listener.Listen();
            accepting = Task.Run(async () =>
            {
                for (int index = 0; ; index++)
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
                    lock (sockets) sockets.Add(socket);
                    int number = index;
                    _ = Task.Run(async () =>
                    {
                        try
                        {
                            await serve(number, socket);
                        }
                        catch (Exception error) when (error is SocketException or ObjectDisposedException)
                        {
                            // the client or the test ended the connection
                        }
                    });
                }
            });
        }

        public string Path => paths.Path;

        public void DropAll()
        {
            lock (sockets)
            {
                foreach (var socket in sockets) socket.Dispose();
                sockets.Clear();
            }
        }

        public async ValueTask DisposeAsync()
        {
            listener.Dispose();
            DropAll();
            await accepting;
            paths.Dispose();
        }
    }

    /// <summary>Reads frames from `socket` and hands each message to `on`, until the socket ends.</summary>
    private static async Task Frames(Socket socket, Func<JsonObject, Task> on)
    {
        var reader = new FrameReader();
        var buffer = new byte[1 << 20];
        while (true)
        {
            int count = await socket.ReceiveAsync(buffer, SocketFlags.None);
            if (count == 0) return;
            foreach (var body in reader.Push(buffer.AsSpan(0, count))) await on(MsgPack.Decode(body)!.AsObject());
        }
    }

    [Fact]
    public async Task TwoHundredConcurrentCallsAnsweredInReverseEachGetTheirOwnReply()
    {
        const int Count = 200;
        await using var fake = new Fake(async (_, socket) =>
        {
            var held = new List<(long Id, int N)>();
            await Frames(socket, async message =>
            {
                switch (message["op"]!.GetValue<string>())
                {
                    case "hello": await socket.SendAsync(HelloF(), SocketFlags.None); break;
                    case "installed": await socket.SendAsync(InstalledF(IdOf(message), 1), SocketFlags.None); break;
                    case "health": await socket.SendAsync(HealthF(IdOf(message), 1), SocketFlags.None); break;
                    case "predict":
                        held.Add((IdOf(message), int.Parse(message["rows"]![0]!["state"]!.GetValue<string>())));
                        if (held.Count == Count)
                        {
                            // every reply in one write, in reverse order: the reader sees many frames in one chunk
                            await socket.SendAsync(held.AsEnumerable().Reverse().SelectMany(item => PredictF(item.Id, item.N / 1000.0)).ToArray(), SocketFlags.None);
                        }
                        break;
                }
            });
        });
        await using var engine = await SocketEngine.ConnectAsync(fake.Path, TimeSpan.FromSeconds(10));
        var calls = Enumerable.Range(0, Count).Select(n => Task.Run(() => engine.PredictAsync("jeff.safety.spam", [R(n.ToString(System.Globalization.CultureInfo.InvariantCulture))]))).ToList();
        var results = await Task.WhenAll(calls).WaitAsync(TimeSpan.FromSeconds(15));
        for (int n = 0; n < Count; n++) Assert.Equal(n / 1000.0, results[n].Probabilities[0][0]);
    }

    /// <summary>An engine that answers every request, but stops reading after the second health until `resume` is set.</summary>
    private static Fake Wedged(TaskCompletionSource resume, List<string> ops) => new(async (_, socket) =>
    {
        int healths = 0;
        await Frames(socket, async message =>
        {
            string op = message["op"]!.GetValue<string>();
            lock (ops) ops.Add(op);
            switch (op)
            {
                case "hello": await socket.SendAsync(HelloF(), SocketFlags.None); break;
                case "installed": await socket.SendAsync(InstalledF(IdOf(message), 1), SocketFlags.None); break;
                case "health": await socket.SendAsync(HealthF(IdOf(message), 1), SocketFlags.None); break;
                case "predict": await socket.SendAsync(PredictF(IdOf(message), 0.25), SocketFlags.None); break;
            }
            if (op == "health" && ++healths == 2) await resume.Task; // stop reading
        });
    });

    private static readonly string Big = new('x', 8 * 1024 * 1024);

    [Fact]
    public async Task AWriteBlockedByAnEngineThatStoppedReadingStillHonoursEachCallsTimeoutAndCancellation()
    {
        var resume = new TaskCompletionSource();
        var ops = new List<string>();
        await using var fake = Wedged(resume, ops);
        await using var engine = await SocketEngine.ConnectAsync(fake.Path, TimeSpan.FromMilliseconds(300));
        await engine.HealthAsync(); // the engine stops reading after this
        var watch = Stopwatch.StartNew();
        var stuck = engine.PredictAsync("jeff.safety.spam", [R(Big)]);
        var queued = engine.HealthAsync(TimeSpan.FromMilliseconds(200));
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(100));
        var cancelled = engine.HealthAsync(null, cancel.Token);

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => cancelled.WaitAsync(TimeSpan.FromSeconds(3)));
        Assert.InRange(watch.ElapsedMilliseconds, 90, 1000);
        Assert.Equal($"No reply from the engine at {fake.Path} within 200 ms", (await Assert.ThrowsAsync<EngineError>(() => queued.WaitAsync(TimeSpan.FromSeconds(3)))).Message);
        Assert.InRange(watch.ElapsedMilliseconds, 190, 1000);
        Assert.Equal($"No reply from the engine at {fake.Path} within 300 ms", (await Assert.ThrowsAsync<EngineError>(() => stuck.WaitAsync(TimeSpan.FromSeconds(3)))).Message);
        Assert.InRange(watch.ElapsedMilliseconds, 290, 1100);

        // the engine reads again: the big frame was written whole, so the next request is read and answered
        resume.SetResult();
        Assert.Equal(0.25, (await engine.PredictAsync("jeff.safety.spam", [R("small")]).WaitAsync(TimeSpan.FromSeconds(3))).Probabilities[0][0]);
        lock (ops) Assert.Equal(new[] { "predict", "predict" }, ops.Where(op => op == "predict"));
    }

    [Fact]
    public async Task DisposeUnblocksAStuckWriteAndFailsItByName()
    {
        var resume = new TaskCompletionSource();
        await using var fake = Wedged(resume, new List<string>());
        var engine = await SocketEngine.ConnectAsync(fake.Path, TimeSpan.FromSeconds(30));
        await engine.HealthAsync();
        var stuck = engine.PredictAsync("jeff.safety.spam", [R(Big)]);
        var queued = engine.HealthAsync();
        await Task.Delay(200);
        Assert.False(stuck.IsCompleted, "the big write should be blocked");
        await engine.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(3));
        foreach (var call in new Task[] { stuck, queued })
        {
            Assert.Equal($"The SocketEngine for {fake.Path} was closed by its caller",
                (await Assert.ThrowsAsync<EngineError>(() => call.WaitAsync(TimeSpan.FromSeconds(3)))).Message);
        }
        resume.SetResult();
    }

    [Fact]
    public async Task ACallCancelledMidWriteLeavesNoHalfFrame()
    {
        var resume = new TaskCompletionSource();
        var ops = new List<string>();
        await using var fake = Wedged(resume, ops);
        await using var engine = await SocketEngine.ConnectAsync(fake.Path, TimeSpan.FromSeconds(5));
        await engine.HealthAsync();
        using var cancel = new CancellationTokenSource();
        var big = engine.PredictAsync("jeff.safety.spam", [R(Big)], cancel.Token);
        await Task.Delay(300);
        Assert.False(big.IsCompleted);
        cancel.Cancel();
        // released at once, while the engine still reads nothing
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => big.WaitAsync(TimeSpan.FromSeconds(1)));
        resume.SetResult();
        Assert.Equal(0.25, (await engine.PredictAsync("jeff.safety.spam", [R("small")])).Probabilities[0][0]);
        lock (ops) Assert.Equal(2, ops.Count(op => op == "predict"));
    }

    [Fact]
    public async Task ACallerCancelledDuringAReconnectStillGetsTheNewConnectionCheckedForItsBase()
    {
        // connection 0 serves bf16 jeff-base@v1.3; every later one (the restarted engine) serves int4 jeff-base@v9, and
        // is slow to list its adapters, so the caller's token is cancelled while the attempt is still under way
        await using var fake = new Fake(async (index, socket) => await Frames(socket, async message =>
        {
            switch (message["op"]!.GetValue<string>())
            {
                case "hello": await socket.SendAsync(HelloF(), SocketFlags.None); break;
                case "installed":
                    if (index > 0) await Task.Delay(500);
                    await socket.SendAsync(InstalledF(IdOf(message), 1), SocketFlags.None);
                    break;
                case "health":
                    await socket.SendAsync(index == 0 ? HealthF(IdOf(message), 1) : HealthF(IdOf(message), 1, "int4", "v9"), SocketFlags.None);
                    break;
            }
        }));
        await using var engine = await SocketEngine.ConnectAsync(fake.Path, TimeSpan.FromSeconds(5));
        fake.DropAll();
        await Task.Delay(100);
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(150));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => engine.HealthAsync(null, cancel.Token));
        // a call made while that attempt is still checking waits for it and fails naming it
        var during = await Assert.ThrowsAsync<EngineError>(() => engine.HealthAsync());
        Assert.Contains($"the last reconnect attempt", during.Message, StringComparison.Ordinal);
        string refused = $"The engine at {fake.Path} restarted serving a base of format {{'format': 'int4', 'format_id': 'q'}} (jeff-base@v9), "
            + "not {'format': 'bf16'} (jeff-base@v1.3) as when this process connected";
        Assert.Contains(refused, during.Message, StringComparison.Ordinal);
        Assert.Equal(0, engine.Reconnects);
        await Task.Delay(1100);
        // the next attempt meets the same engine and refuses it again; nothing is ever answered by it
        Assert.StartsWith(refused, (await Assert.ThrowsAsync<EngineError>(() => engine.HealthAsync())).Message, StringComparison.Ordinal);
        Assert.Equal(0, engine.Reconnects);
    }

    [Fact]
    public async Task AReconnectWhoseHealthBumpsTheVersionAndThenFailsDoesNotHang()
    {
        for (int round = 0; round < 10; round++)
        {
            await using var fake = new Fake(async (index, socket) => await Frames(socket, async message =>
            {
                switch (message["op"]!.GetValue<string>())
                {
                    case "hello": await socket.SendAsync(HelloF(), SocketFlags.None); break;
                    case "installed": await socket.SendAsync(InstalledF(IdOf(message), 1), SocketFlags.None); break;
                    case "health":
                        if (index == 0) await socket.SendAsync(HealthF(IdOf(message), 1), SocketFlags.None);
                        else await socket.SendAsync(HealthF(IdOf(message), 2).Concat(IdZeroFailure()).ToArray(), SocketFlags.None);
                        break;
                }
            }));
            await using var engine = await SocketEngine.ConnectAsync(fake.Path, TimeSpan.FromSeconds(2));
            fake.DropAll();
            await Task.Delay(100);
            var call = engine.PredictAsync("jeff.safety.spam", [R("x")]);
            Assert.True(await Task.WhenAny(call, Task.Delay(5000)) == call, "the reconnect hung");
            Assert.Contains("(reconnect attempt)", (await Assert.ThrowsAnyAsync<JeffError>(() => call)).Message, StringComparison.Ordinal);
        }
    }

    /// <summary>A wait abandoned the way CallAsync abandons one (WaitAsync times out first, the source fails later),
    /// marked with SocketEngine.Observe or not.</summary>
    [MethodImpl(MethodImplOptions.NoInlining)]
    private static async Task Abandon(string tag, bool observe)
    {
        var source = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        try
        {
            await source.Task.WaitAsync(TimeSpan.FromMilliseconds(10)).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            if (observe) SocketEngine.Observe(source.Task);
        }
        await Task.Delay(50).ConfigureAwait(false); // the wait has cleaned up: it no longer observes the source
        source.TrySetException(new EngineError(tag));
    }

    [Fact]
    public async Task ObserveIsWhatKeepsALateFailureOfAnAbandonedWaitFromBeingUnobserved()
    {
        var seen = new List<string>();
        void Record(object? sender, UnobservedTaskExceptionEventArgs args)
        {
            lock (seen) seen.AddRange(args.Exception.InnerExceptions.Select(error => error.Message));
        }
        TaskScheduler.UnobservedTaskException += Record;
        try
        {
            await Abandon("abandoned without Observe", observe: false);
            await Abandon("abandoned with Observe", observe: true);
            for (int i = 0; i < 5; i++)
            {
                GC.Collect();
                GC.WaitForPendingFinalizers();
            }
            lock (seen)
            {
                Assert.Contains("abandoned without Observe", seen); // the check can see an unobserved failure
                Assert.DoesNotContain("abandoned with Observe", seen); // and Observe prevents it
            }
        }
        finally
        {
            TaskScheduler.UnobservedTaskException -= Record;
        }
    }

    [Fact]
    public async Task DisposingDuringAReconnectToAnEngineThatNeverSaysHelloEndsTheAttemptAtOnce()
    {
        // connection 0 is a working engine; the restarted engine accepts every later connection but never sends a hello
        var accepted = new TaskCompletionSource<Socket>(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var fake = new Fake(async (index, socket) =>
        {
            if (index > 0)
            {
                accepted.TrySetResult(socket);
                return;
            }
            await Frames(socket, async message =>
            {
                switch (message["op"]!.GetValue<string>())
                {
                    case "hello": await socket.SendAsync(HelloF(), SocketFlags.None); break;
                    case "installed": await socket.SendAsync(InstalledF(IdOf(message), 1), SocketFlags.None); break;
                    case "health": await socket.SendAsync(HealthF(IdOf(message), 1), SocketFlags.None); break;
                }
            });
        });
        var engine = await SocketEngine.ConnectAsync(fake.Path, TimeSpan.FromSeconds(30));
        fake.DropAll();
        await Task.Delay(100);
        var starter = engine.PredictAsync("jeff.safety.spam", [R("x")]); // starts the attempt, which waits for a hello
        var restarted = await accepted.Task.WaitAsync(TimeSpan.FromSeconds(3));
        var joiner = engine.HealthAsync(); // waits for that attempt
        await Task.Delay(200);
        Assert.False(starter.IsCompleted || joiner.IsCompleted, "the attempt should still be waiting for the hello");
        var watch = Stopwatch.StartNew();
        await engine.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(3));
        foreach (var call in new[] { (Task)starter, joiner })
        {
            Assert.Equal($"The SocketEngine for {fake.Path} was closed by its caller",
                (await Assert.ThrowsAsync<EngineError>(() => call.WaitAsync(TimeSpan.FromSeconds(3)))).Message);
        }
        Assert.InRange(watch.ElapsedMilliseconds, 0, 1000);
        // the attempt's socket was closed: the restarted engine reads the hello and then the end of the stream
        var buffer = new byte[1024];
        int total = 0;
        while (true)
        {
            int count = await restarted.ReceiveAsync(buffer, SocketFlags.None).WaitAsync(TimeSpan.FromSeconds(3));
            if (count == 0) break;
            total += count;
        }
        Assert.Equal(Protocol.EncodeFrame(Protocol.Hello()).Length, total);
        restarted.Dispose();
    }
}
