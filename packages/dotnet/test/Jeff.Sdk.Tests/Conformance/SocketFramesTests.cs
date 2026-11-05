using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>The frame-level parts of socket.json, replayed without a connection: every frame the Python client wrote
/// and the scripted engine answered is read, checked and packed again byte for byte; the hello, an oversized header and
/// an oversized message are refused with the recorded messages. The conversations themselves are replayed through the
/// socket engine by its own tests.</summary>
public class SocketFramesTests
{
    public static TheoryData<string> Cases => Fixtures.CaseNames("socket.json");

    private static IEnumerable<byte[]> Steps(JsonObject item, string side) => item["steps"]!.AsArray()
        .Select(step => step!.AsObject())
        .Where(step => step.ContainsKey(side))
        .Select(step => Fixtures.FromBase64(step[side]!.GetValue<string>()));

    private const string OversizedCase = "a frame header over 64 MiB fails the waiting call";

    [Theory]
    [MemberData(nameof(Cases))]
    public void EveryClientFrameIsOneFrameThatPacksBackToItsBytes(string name)
    {
        var item = Fixtures.Case("socket.json", name);
        var frames = Steps(item, "client").ToList();
        Assert.Equal(Fixtures.ToBase64(Protocol.EncodeFrame(Protocol.Hello())), Fixtures.ToBase64(frames[0]));
        foreach (var frame in frames)
        {
            var reader = new FrameReader();
            var body = Assert.Single(reader.Push(frame));
            Assert.Equal((long)frame.Length, reader.CopiedBytes);
            Assert.Equal(Fixtures.ToBase64(frame), Fixtures.ToBase64(Protocol.EncodeFrame(Protocol.DecodeBody(body))));
        }
    }

    [Theory]
    [MemberData(nameof(Cases))]
    public void TheEnginesBytesReadAsTheFramesTheyHoldWhateverTheChunks(string name)
    {
        var item = Fixtures.Case("socket.json", name);
        byte[] stream = Steps(item, "engine").SelectMany(bytes => bytes).ToArray();
        foreach (int chunkSize in new[] { 1, 3, 7, stream.Length })
        {
            var reader = new FrameReader();
            var bodies = new List<byte[]>();
            JeffError? refused = null;
            try
            {
                for (int start = 0; start < stream.Length; start += chunkSize)
                {
                    bodies.AddRange(reader.Push(stream.AsSpan(start, Math.Min(chunkSize, stream.Length - start))));
                }
            }
            catch (InvalidRequest error)
            {
                refused = error;
            }
            refused ??= reader.Failure;
            var whole = Steps(item, "engine").Where(bytes => bytes.Length > Protocol.HeaderSize).ToList();
            Assert.Equal(whole.Count, bodies.Count);
            for (int index = 0; index < bodies.Count; index++)
            {
                Assert.Equal(Fixtures.ToBase64(whole[index]), Fixtures.ToBase64(Protocol.EncodeFrame(Protocol.DecodeBody(bodies[index]))));
            }
            if (name == OversizedCase)
            {
                Assert.Equal("Socket frame of 67108865 bytes is over the 64 MiB limit", Assert.IsType<InvalidRequest>(refused).Message);
            }
            else
            {
                Assert.Null(refused);
            }
        }
    }

    [Fact]
    public void TheOversizedHeaderCaseIsWhereTheFixtureSaysItIs()
    {
        var expected = Fixtures.Case("socket.json", OversizedCase)["results"]![1]!["error"]!["message"]!.GetValue<string>();
        Assert.EndsWith(": InvalidRequest: Socket frame of 67108865 bytes is over the 64 MiB limit", expected);
    }

    [Theory]
    [MemberData(nameof(Cases))]
    public void TheEnginesHelloIsCheckedAsThePythonClientChecksIt(string name)
    {
        var item = Fixtures.Case("socket.json", name);
        var hello = Protocol.DecodeBody(Steps(item, "engine").First().AsSpan(Protocol.HeaderSize));
        var connect = item["results"]![0]!.AsObject();
        if (connect.ContainsKey("error") && connect["error"]!["type"]!.GetValue<string>() == "ProtocolMismatch")
        {
            Assert.Equal(connect["error"]!["message"]!.GetValue<string>(), Assert.Throws<ProtocolMismatch>(() => Protocol.CheckHello(hello)).Message);
        }
        else
        {
            Protocol.CheckHello(hello);
        }
    }

    [Fact]
    public void AMessageOver64MiBIsRefusedBeforeItIsSentWithTheRecordedMessage()
    {
        var item = Fixtures.Case("socket.json", "a message over 64 MiB is refused before it is sent");
        var action = item["actions"]![1]!.AsObject();
        var generated = action["rows_generated"]!.AsObject();
        var rows = new JsonArray(new JsonObject
        {
            ["state"] = new string('x', checked((int)generated["state_length"]!.GetValue<long>())),
            ["question"] = generated["question"]!.DeepClone(),
        });
        // The client's third request: installed and health at connect took ids 1 and 2
        var message = new JsonObject { ["op"] = "predict", ["adapter"] = action["adapter"]!.DeepClone(), ["rows"] = rows, ["id"] = 3L };
        var error = Assert.Throws<InvalidRequest>(() => Protocol.EncodeFrame(message, "predict"));
        Assert.Equal(item["results"]![1]!["error"]!["message"]!.GetValue<string>(), error.Message);
    }
}
