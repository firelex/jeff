using System.Buffers.Binary;
using System.Net.Sockets;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Helpers;

/// <summary>A fake engine on a Unix socket: answers hello, installed, health and predict (with `answers`, given the
/// adapter and each row's JSON), like `jeff runtime serve` over a fake model; with predictDelay, each predict is
/// answered that long after it arrives. A serve is recorded in ServeRequests and refused as an engine refuses an
/// adapter it cannot load, unless AcceptServe is set, when the list asked for becomes the installed one. Without `path` it listens in a temporary folder of its own.</summary>
internal sealed class EngineServer : IAsyncDisposable
{
    private readonly Socket listener;
    private readonly CancellationTokenSource stopping = new();
    private readonly List<Socket> connections = new();
    private readonly Func<string, JsonObject, IReadOnlyList<double>> answers;
    private volatile IReadOnlyList<string> installed;
    private readonly TimeSpan? predictDelay;
    private readonly SocketPaths? ownFolder;
    private readonly Task accepting;
    private volatile bool answering = true;

    private EngineServer(string path, SocketPaths? ownFolder, Func<string, JsonObject, IReadOnlyList<double>> answers, IEnumerable<string> installed,
        TimeSpan? predictDelay)
    {
        Path = path;
        this.ownFolder = ownFolder;
        this.answers = answers;
        this.installed = installed.ToList();
        this.predictDelay = predictDelay;
        listener = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        listener.Bind(new UnixDomainSocketEndPoint(path));
        listener.Listen();
        accepting = AcceptAsync();
    }

    public string Path { get; }

    public static EngineServer Start(Func<string, JsonObject, IReadOnlyList<double>> answers, IEnumerable<string> installed, string? path = null,
        TimeSpan? predictDelay = null)
    {
        var folder = path is null ? SocketPaths.New() : null;
        return new EngineServer(path ?? folder!.Path, folder, answers, installed, predictDelay);
    }

    public void StopAnswering() => answering = false;

    /// <summary>Whether a serve request is carried out (true) or refused with UnknownFunction (false, the default).</summary>
    public bool AcceptServe { get; set; }

    /// <summary>The adapters of every serve request received, in order.</summary>
    public List<List<string>> ServeRequests { get; } = new();

    /// <summary>When set, every reply to a request (not the hello) is passed through it before it is written, so a test
    /// can send a malformed answer.</summary>
    public Func<JsonObject, JsonObject>? Rewrite { get; set; }

    public static byte[] Frame(JsonObject message)
    {
        var body = MsgPack.Encode(message);
        var frame = new byte[4 + body.Length];
        BinaryPrimitives.WriteUInt32BigEndian(frame, (uint)body.Length);
        body.CopyTo(frame, 4);
        return frame;
    }

    private async Task AcceptAsync()
    {
        while (!stopping.IsCancellationRequested)
        {
            Socket socket;
            try
            {
                socket = await listener.AcceptAsync(stopping.Token);
            }
            catch (Exception error) when (error is OperationCanceledException or SocketException or ObjectDisposedException)
            {
                return; // the test is closing the server
            }
            lock (connections) connections.Add(socket);
            _ = ServeAsync(socket);
        }
    }

    private async Task ServeAsync(Socket socket)
    {
        var reader = new FrameReader();
        var buffer = new byte[64 * 1024];
        var writing = new SemaphoreSlim(1, 1);
        async Task Write(JsonObject message)
        {
            if (Rewrite is { } rewrite && message["op"] is null) message = rewrite(message);
            await writing.WaitAsync();
            try
            {
                await socket.SendAsync(Frame(message), SocketFlags.None);
            }
            finally
            {
                writing.Release();
            }
        }
        // a delayed reply whose connection has closed meanwhile is dropped, leaving no unobserved task failure behind
        async Task WriteLateAsync(TimeSpan delay, JsonObject reply)
        {
            await Task.Delay(delay);
            try
            {
                await Write(reply);
            }
            catch (Exception error) when (error is SocketException or ObjectDisposedException)
            {
            }
        }
        while (true)
        {
            int count;
            try
            {
                count = await socket.ReceiveAsync(buffer, SocketFlags.None);
            }
            catch (Exception error) when (error is SocketException or ObjectDisposedException)
            {
                return; // the client or the test ended the connection
            }
            if (count == 0) return;
            foreach (var body in reader.Push(buffer.AsSpan(0, count)))
            {
                var message = MsgPack.Decode(body)!.AsObject();
                string op = message["op"]!.GetValue<string>();
                var id = message["id"]?.GetValue<long>();
                const long version = 1;
                if (op == "hello") await Write(new JsonObject { ["op"] = "hello", ["version"] = 2L, ["adapters_version"] = version });
                else if (op == "installed")
                {
                    var adapters = new JsonArray("base");
                    foreach (var adapter in installed) adapters.Add(adapter);
                    await Write(new JsonObject { ["id"] = id, ["ok"] = true, ["result"] = new JsonObject { ["adapters"] = adapters, ["adapters_version"] = version } });
                }
                else if (op == "health")
                {
                    await Write(new JsonObject
                    {
                        ["id"] = id, ["ok"] = true, ["result"] = new JsonObject
                        {
                            ["status"] = "ready", ["backend"] = "fake", ["adapters"] = Json.Strings(installed), ["adapters_version"] = version,
                            ["base"] = new JsonObject
                            {
                                ["id"] = "jeff-base", ["version"] = "v1.3", ["checkpoint"] = "fake", ["max_options"] = 252L, ["format"] = "bf16",
                            },
                        },
                    });
                }
                else if (op == "serve")
                {
                    var asked = message["adapters"]!.AsArray().Select(item => item!.GetValue<string>()).ToList();
                    lock (ServeRequests) ServeRequests.Add(asked);
                    if (AcceptServe)
                    {
                        installed = asked.Where(adapter => adapter != "base").ToList();
                        var adapters = new JsonArray("base");
                        foreach (var adapter in installed) adapters.Add(adapter);
                        await Write(new JsonObject { ["id"] = id, ["ok"] = true, ["result"] = new JsonObject { ["adapters"] = adapters, ["adapters_version"] = version } });
                    }
                    else
                    {
                        var unknown = asked.Where(adapter => adapter != "base" && !installed.Contains(adapter)).Order(StringComparer.Ordinal);
                        await Write(new JsonObject
                        {
                            ["id"] = id, ["ok"] = false, ["error"] = new JsonObject
                            {
                                ["type"] = "UnknownFunction", ["message"] = $"not available to serve from this engine: {string.Join(", ", unknown)}",
                                ["details"] = new JsonObject { ["adapters_version"] = version },
                            },
                        });
                    }
                }
                else if (op == "predict" && answering)
                {
                    string adapter = message["adapter"]!.GetValue<string>();
                    var probabilities = new JsonArray(message["rows"]!.AsArray()
                        .Select(row => (JsonNode?)new JsonArray(answers(adapter, row!.AsObject()).Select(p => (JsonNode?)JsonValue.Create(p)).ToArray())).ToArray());
                    var reply = new JsonObject
                    {
                        ["id"] = id, ["ok"] = true, ["result"] = new JsonObject
                        {
                            ["probabilities"] = probabilities,
                            ["timing"] = new JsonObject { ["path"] = "fake", ["model_ms"] = 0.5, ["format"] = "bf16" },
                            ["adapters_version"] = version,
                        },
                    };
                    if (predictDelay is { } delay) _ = WriteLateAsync(delay, reply);
                    else await Write(reply);
                }
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        stopping.Cancel();
        listener.Dispose();
        lock (connections) foreach (var socket in connections) socket.Dispose();
        await accepting;
        ownFolder?.Dispose();
        if (ownFolder is null && File.Exists(Path)) File.Delete(Path);
    }
}
