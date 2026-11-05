using System.Buffers.Binary;
using System.Net.WebSockets;
using System.Text.Json.Nodes;
using Jeff.Sdk.Testing;
using Jeff.Sdk.Tests.Conformance;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;

namespace Jeff.Sdk.Tests.Helpers;

/// <summary>A fake Jeff server for the channel tests: GET /v1/functions answers the listing recorded in remote.json, and
/// a WebSocket at /v1/channel answers hello, auth, refresh, bind, call, call_many, decide and release over a local
/// client with a fake engine, as jeff-server's channel does. As jeff-server, a request arriving while 256 are
/// outstanding on the connection fails at once with RateLimited.</summary>
internal sealed class ChannelServer : IAsyncDisposable
{
    private const int MaxOutstanding = 256;
    public static readonly string[] Installed = ["jeff.support.triage", "jeff.safety.spam"];
    private static readonly double[] Unsure = [0, 0.5, 0.5, 0, 0];

    /// <summary>Silent: ops that get no answer; Version: what bind reports for every adapter (the catalogue's when not
    /// given); Token: the only token auth and refresh accept (any when not given); Gated: ops whose requests wait until
    /// OpenGate(); Reorder: replies to call, call_many and decide are held until that many are ready, then sent in reverse
    /// order; BadLowConfidenceDecision: every call, call_many and decide fails with a LowConfidence whose decision does
    /// not match contract/schemas/decision; StallAfter: once a request with this op has arrived, the server stops reading
    /// the connection (so the client's sends stall once the buffers between them are full) until it is disposed; SlowAfter:
    /// once a request with this op has arrived, the server reads at most SlowRead bytes (16 KiB when not given) every
    /// 20 ms.</summary>
    public sealed class Settings
    {
        public string? Version { get; init; }
        public string[] Silent { get; init; } = [];
        public string? Token { get; init; }
        public string[] Gated { get; init; } = [];
        public int? Reorder { get; init; }
        public bool BadLowConfidenceDecision { get; init; }
        public string? StallAfter { get; init; }
        public string? SlowAfter { get; init; }
        public int SlowRead { get; init; } = 16 * 1024;

        /// <summary>When set, a bind is answered with this handle in place of the next number.</summary>
        public Func<JsonNode>? BindHandle { get; init; }
    }

    private readonly Settings settings;
    private readonly JeffClient jeff;
    private readonly Catalogue catalogue = Catalogue.Shipped();
    private readonly TaskCompletionSource gate = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource disposed = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly List<byte[]> held = new();
    private readonly Dictionary<long, JsonObject> handles = new();
    private readonly List<HttpContext> contexts = new();
    private readonly List<(WebSocket Socket, SemaphoreSlim Sending)> sockets = new();
    private long nextHandle = 1;
    private int waiting;
    private HttpServer? http;

    private ChannelServer(Settings settings, JeffClient jeff)
    {
        this.settings = settings;
        this.jeff = jeff;
    }

    public string Url => http!.Url;

    public List<JsonObject> Received { get; } = new();

    public List<string> Headers { get; } = new();

    public int Waiting => Volatile.Read(ref waiting);

    /// <summary>The fake engine's answers: a score function's level 2, confidently, unless the input contains "unsure";
    /// a choice's first criterion; any other question's first option.</summary>
    public static IReadOnlyList<double> Answers(string adapterId, Row row)
    {
        if (row.Question.Type == QuestionType.Score) return Json.Write(row.State).Contains("unsure") ? Unsure : [0, 0, 0.9, 0.1, 0];
        int count = row.Question.Criteria switch { JsonObject criteria => criteria.Count, JsonArray levels => levels.Count, _ => 2 };
        return Enumerable.Range(0, count).Select(index => index == 0 ? 1.0 : 0.0).ToList();
    }

    private static string Listing => Fixtures.Cases("remote.json").First()["exchanges"]![0]!["response"]!["body"]!.GetValue<string>();

    public static async Task<ChannelServer> StartAsync(Settings? settings = null)
    {
        var server = new ChannelServer(settings ?? new Settings(), await JeffClient.CreateAsync(new FakeEngine(Answers, Installed), Catalogue.Shipped()));
        server.http = await HttpServer.StartAsync(server.HandleAsync, app => app.UseWebSockets());
        return server;
    }

    private async Task HandleAsync(HttpContext context, string body)
    {
        if (context.Request.Path == "/v1/functions")
        {
            await HttpServer.Json(context, 200, Listing);
            return;
        }
        if (context.Request.Path != "/v1/channel" || !context.WebSockets.IsWebSocketRequest)
        {
            context.Response.StatusCode = 400;
            return;
        }
        lock (Headers) Headers.Add(context.Request.Headers.Authorization.ToString());
        using var socket = await context.WebSockets.AcceptWebSocketAsync();
        var sending = new SemaphoreSlim(1, 1);
        lock (sockets)
        {
            contexts.Add(context);
            sockets.Add((socket, sending));
        }
        try
        {
            await ServeAsync(socket, sending);
        }
        finally
        {
            lock (sockets)
            {
                contexts.Remove(context);
                sockets.Remove((socket, sending));
            }
        }
    }

    private async Task ServeAsync(WebSocket socket, SemaphoreSlim sending)
    {
        int outstanding = 0;
        bool slow = false;
        var buffer = new byte[64 * 1024];
        using var message = new MemoryStream();
        while (true)
        {
            WebSocketReceiveResult result;
            try
            {
                if (slow) await Task.Delay(20);
                result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer, 0, slow ? settings.SlowRead : buffer.Length), CancellationToken.None);
            }
            catch (Exception error) when (error is WebSocketException or OperationCanceledException or IOException)
            {
                return; // the client or the test ended the connection
            }
            if (result.MessageType == WebSocketMessageType.Close)
            {
                await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, null, CancellationToken.None);
                return;
            }
            message.Write(buffer, 0, result.Count);
            if (!result.EndOfMessage) continue;
            var data = message.ToArray();
            message.SetLength(0);
            var request = MsgPack.Decode(data.AsSpan(4))!.AsObject();
            lock (Received) Received.Add(request);
            _ = AnswerAsync(socket, sending, request, () => Interlocked.Increment(ref outstanding), () => Interlocked.Decrement(ref outstanding),
                () => Volatile.Read(ref outstanding));
            if (settings.SlowAfter == request["op"]!.GetValue<string>()) slow = true;
            if (settings.StallAfter == request["op"]!.GetValue<string>())
            {
                await disposed.Task;
                return;
            }
        }
    }

    /// <summary>One frame of the engine protocol (packed as it is, unchecked, so a test can send anything).</summary>
    private static byte[] Frame(JsonObject message)
    {
        var body = MsgPack.Encode(message);
        var frame = new byte[4 + body.Length];
        BinaryPrimitives.WriteUInt32BigEndian(frame, (uint)body.Length);
        body.CopyTo(frame, 4);
        return frame;
    }

    private static async Task Write(WebSocket socket, SemaphoreSlim sending, byte[] frame)
    {
        await sending.WaitAsync().ConfigureAwait(false);
        try
        {
            await socket.SendAsync(new ArraySegment<byte>(frame), WebSocketMessageType.Binary, true, CancellationToken.None).ConfigureAwait(false);
        }
        finally
        {
            sending.Release();
        }
    }

    private async Task AnswerAsync(WebSocket socket, SemaphoreSlim sending, JsonObject message, Func<int> enter, Func<int> leave, Func<int> current)
    {
        string op = message["op"]!.GetValue<string>();
        var id = message["id"]?.GetValue<long>();
        if (settings.Silent.Contains(op)) return;
        if (op != "hello" && current() >= MaxOutstanding)
        {
            await Write(socket, sending, Frame(new JsonObject
            {
                ["id"] = id, ["ok"] = false, ["error"] = new JsonObject
                {
                    ["type"] = "RateLimited", ["message"] = $"{MaxOutstanding} requests are outstanding on this connection",
                    ["details"] = new JsonObject { ["retry_after_s"] = 1L, ["channel_id"] = "0badc0de" },
                },
            }));
            return;
        }
        async Task Reply(JsonObject body)
        {
            if (op != "hello") leave();
            var reply = new JsonObject { ["id"] = id };
            foreach (var (key, value) in body) reply[key] = Json.Canonical(value);
            var bytes = Frame(reply);
            if (settings.Reorder is null || op is not ("call" or "call_many" or "decide"))
            {
                await Write(socket, sending, bytes);
                return;
            }
            List<byte[]>? release = null;
            lock (held)
            {
                held.Add(bytes);
                if (held.Count == settings.Reorder)
                {
                    release = held.AsEnumerable().Reverse().ToList();
                    held.Clear();
                }
            }
            if (release is not null) foreach (var item in release) await Write(socket, sending, item);
        }
        Task Ok(JsonNode result) => Reply(new JsonObject { ["ok"] = true, ["result"] = result });
        Task Fail(string type, string text, JsonObject? details = null)
        {
            var all = new JsonObject { ["channel_id"] = "0badc0de" };
            if (details is not null) foreach (var (key, value) in details) all[key] = Json.Canonical(value);
            return Reply(new JsonObject { ["ok"] = false, ["error"] = new JsonObject { ["type"] = type, ["message"] = text, ["details"] = all } });
        }
        if (op != "hello") enter();
        if (settings.Gated.Contains(op))
        {
            Interlocked.Increment(ref waiting);
            await gate.Task;
            Interlocked.Decrement(ref waiting);
        }
        if (settings.BadLowConfidenceDecision && op is "call" or "call_many" or "decide")
        {
            await Fail("LowConfidence", "abstained", new JsonObject { ["decision"] = new JsonObject { ["value"] = 1L } });
            return;
        }
        switch (op)
        {
            case "hello":
                await Write(socket, sending, Frame(new JsonObject { ["op"] = "hello", ["version"] = 2L }));
                break;
            case "auth" or "refresh":
                if (settings.Token is not null && message["token"]!.GetValue<string>() != settings.Token) await Fail("Unauthorised", "the token is not valid");
                else await Ok(new JsonObject());
                break;
            case "bind":
                long handle;
                lock (handles)
                {
                    handle = nextHandle++;
                    handles[handle] = message;
                }
                string function = message["function"]!.GetValue<string>();
                string version = settings.Version ?? catalogue.Adapter(catalogue.Function(function).AdapterId).Version;
                await Ok(new JsonObject { ["handle"] = settings.BindHandle is { } made ? made() : handle, ["function"] = function, ["version"] = version });
                break;
            case "call" or "call_many":
                JsonObject? bound;
                lock (handles) bound = handles.GetValueOrDefault(message["handle"]!.GetValue<long>());
                if (bound is null)
                {
                    await Fail("InvalidRequest", $"handle {message["handle"]} is not bound");
                    break;
                }
                var inputs = op == "call" ? new List<JsonNode?> { message["input"] } : message["inputs"]!.AsArray().ToList();
                var decisions = await jeff.DecideAsync(inputs.Select(input => new Call(bound["function"]!.GetValue<string>(), input, bound["context"] as JsonObject,
                    bound["labels"])).ToList());
                await Ok(op == "call"
                    ? new JsonObject { ["decision"] = decisions[0].ToJson() }
                    : new JsonObject { ["decisions"] = new JsonArray(decisions.Select(decision => (JsonNode?)decision.ToJson()).ToArray()) });
                break;
            case "decide":
                var decided = await jeff.DecideAsync(RunDecisions.CallsOf(message["calls"]));
                await Ok(new JsonObject { ["decisions"] = new JsonArray(decided.Select(decision => (JsonNode?)decision.ToJson()).ToArray()) });
                break;
            case "release":
                bool removed;
                lock (handles) removed = handles.Remove(message["handle"]!.GetValue<long>());
                if (removed) await Ok(new JsonObject());
                else await Fail("InvalidRequest", $"handle {message["handle"]} is not bound");
                break;
        }
    }

    /// <summary>Sends `message` as one frame on every open connection.</summary>
    public async Task SendAsync(JsonObject message)
    {
        List<(WebSocket Socket, SemaphoreSlim Sending)> open;
        lock (sockets) open = sockets.ToList();
        foreach (var (socket, sending) in open) await Write(socket, sending, Frame(message));
    }

    /// <summary>Sends `data` as one binary WebSocket message on every open connection.</summary>
    public async Task SendBytesAsync(byte[] data)
    {
        List<(WebSocket Socket, SemaphoreSlim Sending)> open;
        lock (sockets) open = sockets.ToList();
        foreach (var (socket, sending) in open) await Write(socket, sending, data);
    }

    public void OpenGate() => gate.TrySetResult();

    /// <summary>Ends every connection at once, without a close frame.</summary>
    public void DropConnections()
    {
        lock (sockets) foreach (var context in contexts) context.Abort();
    }

    public async ValueTask DisposeAsync()
    {
        OpenGate();
        disposed.TrySetResult();
        DropConnections();
        await http!.DisposeAsync();
    }
}
