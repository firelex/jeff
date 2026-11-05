using System.Buffers.Binary;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using Jeff.Sdk.Tests.Helpers;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>socket.json: an engine that plays each case's steps on the first connection and records every frame it
/// reads; the SDK's results and the frames it sent must be the recorded ones, byte for byte.</summary>
public class SocketTests
{
    public static TheoryData<string> Cases => Fixtures.CaseNames("socket.json");

    private sealed class ScriptedServer : IAsyncDisposable
    {
        private readonly SocketPaths paths = SocketPaths.New();
        private readonly Socket listener = new(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        public readonly List<string> Received = new();
        public Task Done = Task.CompletedTask;

        public ScriptedServer(JsonArray steps)
        {
            listener.Bind(new UnixDomainSocketEndPoint(paths.Path));
            listener.Listen();
            Done = PlayAsync(steps);
        }

        public string Path => paths.Path;

        private static async Task<byte[]?> ReadFrameAsync(Socket socket, List<byte> buffer)
        {
            using var limit = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var chunk = new byte[64 * 1024];
            while (true)
            {
                if (buffer.Count >= 4)
                {
                    int length = 4 + (int)BinaryPrimitives.ReadUInt32BigEndian(buffer.Take(4).ToArray());
                    if (buffer.Count >= length)
                    {
                        var frame = buffer.Take(length).ToArray();
                        buffer.RemoveRange(0, length);
                        return frame;
                    }
                }
                int count = await socket.ReceiveAsync(chunk, SocketFlags.None, limit.Token);
                if (count == 0) return null;
                buffer.AddRange(chunk.AsSpan(0, count).ToArray());
            }
        }

        private async Task PlayAsync(JsonArray steps)
        {
            using var socket = await listener.AcceptAsync();
            var buffer = new List<byte>();
            foreach (var step in steps.Select(item => item!.AsObject()))
            {
                if (step.ContainsKey("client"))
                {
                    byte[]? frame;
                    try
                    {
                        frame = await ReadFrameAsync(socket, buffer);
                    }
                    catch (OperationCanceledException)
                    {
                        Received.Add("<timeout>");
                        break;
                    }
                    Received.Add(frame is null ? "<closed>" : Fixtures.ToBase64(frame));
                    if (frame is null) break;
                }
                else if (step.ContainsKey("engine"))
                {
                    await socket.SendAsync(Fixtures.FromBase64(step["engine"]!.GetValue<string>()), SocketFlags.None);
                }
                else
                {
                    socket.Shutdown(SocketShutdown.Send);
                    break;
                }
            }
        }

        public async ValueTask DisposeAsync()
        {
            listener.Dispose();
            await Done;
            paths.Dispose();
        }
    }

    private static IReadOnlyList<Row> RowsOf(JsonObject action)
    {
        if (action["rows"] is JsonArray rows)
        {
            return rows.Select(item => item!.AsObject()).Select(row => new Row(Json.Canonical(row["state"])!, new RowQuestion(
                QuestionTypes.Parse(row["question"]!["type"]!.GetValue<string>(), "a fixture row"), row["question"]!["instructions"]!.GetValue<string>(),
                Json.Canonical(row["question"]!["criteria"])))).ToList();
        }
        var generated = action["rows_generated"]!.AsObject();
        var question = generated["question"]!.AsObject();
        return [new Row(JsonValue.Create(new string('x', (int)generated["state_length"]!.GetValue<long>())), new RowQuestion(
            QuestionTypes.Parse(question["type"]!.GetValue<string>(), "a fixture row"), question["instructions"]!.GetValue<string>(), null))];
    }

    private static async Task<JsonNode?> ActAsync(SocketEngine engine, JsonObject action)
    {
        switch (action["op"]!.GetValue<string>())
        {
            case "predict":
                long? deadline = action["deadline_ms"] is JsonValue value ? value.GetValue<long>() : null;
                var result = await engine.PredictAsync(action["adapter"]!.GetValue<string>(), RowsOf(action), deadline);
                return new JsonObject
                {
                    ["probabilities"] = new JsonArray(result.Probabilities.Select(row => (JsonNode?)new JsonArray(row.Select(p => (JsonNode?)JsonValue.Create(p)).ToArray())).ToArray()),
                    ["timing"] = Json.Canonical(result.Timing),
                    ["input_tokens"] = result.InputTokens is null ? null : new JsonArray(result.InputTokens.Select(tokens => (JsonNode?)JsonValue.Create(tokens)).ToArray()),
                };
            case "installed":
                return new JsonObject { ["installed"] = Json.Strings(PyFormat.Sorted(await engine.InstalledAsync(true))) };
            case "health":
                return await engine.HealthAsync();
            default:
                throw new InvalidOperationException($"Unknown socket action {action["op"]}");
        }
    }

    [Theory]
    [MemberData(nameof(Cases))]
    public async Task TheConversationIsByteForByteThePythonSdks(string name)
    {
        var item = Fixtures.Case("socket.json", name);
        var actions = item["actions"]!.AsArray().Select(action => action!.AsObject()).ToList();
        var server = new ScriptedServer(item["steps"]!.AsArray());
        JsonObject Failed(JeffError error) => new()
        {
            ["error"] = new JsonObject { ["type"] = error.Type, ["message"] = error.Message.Replace(server.Path, "{socket}", StringComparison.Ordinal) },
        };
        var results = new JsonArray();
        SocketEngine? engine = null;
        await using (server)
        {
            try
            {
                engine = await SocketEngine.ConnectAsync(server.Path, TimeSpan.FromSeconds(5));
                results.Add(new JsonObject
                {
                    ["result"] = new JsonObject
                    {
                        ["base_format"] = engine.BaseFormat.ToJson(), ["base"] = new JsonArray(engine.Base.Id, engine.Base.Version),
                        ["installed"] = Json.Strings(PyFormat.Sorted(await engine.InstalledAsync())),
                    },
                });
            }
            catch (JeffError error)
            {
                results.Add(Failed(error));
            }
            int index = 1;
            while (engine is not null && index < actions.Count)
            {
                var group = new List<int> { index };
                while (Json.IsTrue(actions[group[^1]]["concurrent"]) && group[^1] + 1 < actions.Count && Json.IsTrue(actions[group[^1] + 1]["concurrent"]))
                {
                    group.Add(group[^1] + 1);
                }
                var running = group.Select(async member =>
                {
                    try
                    {
                        return (JsonNode)new JsonObject { ["result"] = await ActAsync(engine, actions[member]) };
                    }
                    catch (JeffError error)
                    {
                        return Failed(error);
                    }
                }).ToList();
                foreach (var outcome in await Task.WhenAll(running)) results.Add(outcome);
                index = group[^1] + 1;
            }
            if (engine is not null) await engine.DisposeAsync();
        }
        JsonAssert.Equal(item["results"], results);
        Assert.Equal(item["steps"]!.AsArray().Select(step => step!.AsObject()).Where(step => step.ContainsKey("client"))
            .Select(step => step["client"]!.GetValue<string>()), server.Received);
    }
}
