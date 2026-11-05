using System.Buffers.Binary;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class ProtocolTests
{
    private static JsonObject Installed() => new() { ["op"] = "installed", ["id"] = 1L };

    private static JsonObject Health() => new() { ["op"] = "health", ["id"] = 2L };

    [Fact]
    public void AHeaderSplitByteByByteGivesTheFrameOnceItIsWhole()
    {
        var frame = Protocol.EncodeFrame(Installed());
        var reader = new FrameReader();
        var bodies = new List<byte[]>();
        foreach (byte item in frame) bodies.AddRange(reader.Push([item]));
        Assert.Single(bodies);
        JsonAssert.Equal(Installed(), Protocol.DecodeBody(bodies[0]));
    }

    [Fact]
    public void TwoFramesInOneChunkAreBothRead()
    {
        var reader = new FrameReader();
        var bodies = reader.Push([.. Protocol.EncodeFrame(Installed()), .. Protocol.EncodeFrame(Health())]);
        Assert.Equal(2, bodies.Count);
        JsonAssert.Equal(Health(), Protocol.DecodeBody(bodies[1]));
    }

    [Fact]
    public void AnEightMegabyteBodyIn64KilobyteChunksIsCopiedInLinearTimeNotOncePerChunk()
    {
        var rows = new JsonArray(new JsonObject
        {
            ["state"] = new string('x', 8 * 1024 * 1024), ["question"] = new JsonObject { ["type"] = "noul", ["instructions"] = "?" },
        });
        var message = new JsonObject { ["op"] = "predict", ["adapter"] = "jeff.safety.spam", ["rows"] = rows, ["id"] = 3L };
        var frame = Protocol.EncodeFrame(message);
        var reader = new FrameReader();
        var bodies = new List<byte[]>();
        for (int start = 0; start < frame.Length; start += 64 * 1024) bodies.AddRange(reader.Push(frame.AsSpan(start, Math.Min(64 * 1024, frame.Length - start))));
        Assert.Single(bodies);
        // each byte once, plus the doubling buffer's moves, which stay under twice the body's length
        Assert.InRange(reader.CopiedBytes, frame.Length, 3L * frame.Length);
        Assert.Equal(frame.Length - Protocol.HeaderSize, bodies[0].Length);
        Assert.Equal(Convert.ToBase64String(frame), Convert.ToBase64String(Protocol.EncodeFrame(Protocol.DecodeBody(bodies[0]))));
    }

    [Fact]
    public void FramesBeforeAnOversizedHeaderInTheSameChunkAreReturnedAndTheHeadersErrorIsKept()
    {
        var reader = new FrameReader();
        var oversized = new byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(oversized, 64 * 1024 * 1024 + 1);
        var bodies = reader.Push([.. Protocol.EncodeFrame(Installed()), .. oversized]);
        Assert.Single(bodies);
        Assert.Equal("Socket frame of 67108865 bytes is over the 64 MiB limit", Assert.IsType<InvalidRequest>(reader.Failure).Message);
        Assert.Same(reader.Failure, Assert.Throws<InvalidRequest>(() => reader.Push([0])));
    }

    [Fact]
    public void AHeaderAnnouncing64MiBFollowedByAFewBytesDoesNotAllocate64MiB()
    {
        var reader = new FrameReader();
        var header = new byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(header, (uint)Protocol.MaxFrame);
        long before = GC.GetAllocatedBytesForCurrentThread();
        Assert.Empty(reader.Push([.. header, 1, 2, 3, 4, 5, 6, 7, 8]));
        long allocated = GC.GetAllocatedBytesForCurrentThread() - before;
        Assert.InRange(allocated, 0, 64 * 1024);

        // a megabyte more costs a few megabytes of buffers, not the 64 MiB announced
        before = GC.GetAllocatedBytesForCurrentThread();
        Assert.Empty(reader.Push(new byte[1024 * 1024]));
        allocated = GC.GetAllocatedBytesForCurrentThread() - before;
        Assert.InRange(allocated, 0, 3 * 1024 * 1024);
    }

    [Fact]
    public void ABodyArrivingInUnevenPiecesIsTheExactBodyWithNoSpareBytes()
    {
        var message = new JsonObject { ["op"] = "predict", ["adapter"] = "jeff.safety.spam", ["id"] = 7L,
            ["rows"] = new JsonArray(new JsonObject { ["state"] = new string('y', 100_003), ["question"] = new JsonObject { ["type"] = "noul", ["instructions"] = "?" } }) };
        byte[] frame = [.. Protocol.EncodeFrame(message), .. Protocol.EncodeFrame(Installed())];
        var reader = new FrameReader();
        var bodies = new List<byte[]>();
        int start = 0;
        foreach (int size in Enumerable.Range(1, 1000).Select(step => step * 37 % 5000 + 1))
        {
            if (start >= frame.Length) break;
            int take = Math.Min(size, frame.Length - start);
            bodies.AddRange(reader.Push(frame.AsSpan(start, take)));
            start += take;
        }
        if (start < frame.Length) bodies.AddRange(reader.Push(frame.AsSpan(start)));
        Assert.Equal(2, bodies.Count);
        Assert.Equal(Convert.ToBase64String(frame), Convert.ToBase64String([.. Protocol.EncodeFrame(Protocol.DecodeBody(bodies[0])), .. Protocol.EncodeFrame(Protocol.DecodeBody(bodies[1]))]));
    }

    [Fact]
    public void AnOversizedHeaderAloneIsThrownAtOnceAndKept()
    {
        var reader = new FrameReader();
        var error = Assert.Throws<InvalidRequest>(() => reader.Push([0xff, 0xff, 0xff, 0xff]));
        Assert.Equal("Socket frame of 4294967295 bytes is over the 64 MiB limit", error.Message);
        Assert.Same(error, reader.Failure);
        Assert.Same(error, Assert.Throws<InvalidRequest>(() => reader.Push([])));
    }

    [Fact]
    public void AHeaderAtTheLimitIsAcceptedAndOneOverIsNot()
    {
        var header = new byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(header, (uint)Protocol.MaxFrame);
        Assert.Equal(Protocol.MaxFrame, Protocol.FrameLength(header));
        BinaryPrimitives.WriteUInt32BigEndian(header, (uint)Protocol.MaxFrame + 1);
        Assert.Throws<InvalidRequest>(() => Protocol.FrameLength(header));
    }

    [Fact]
    public void AnEmptyChunkAndAnEmptyBodyAreRead()
    {
        var reader = new FrameReader();
        Assert.Empty(reader.Push([]));
        var bodies = reader.Push([0, 0, 0, 0]);
        Assert.Empty(Assert.Single(bodies));
        Assert.Null(reader.Failure);
    }

    [Fact]
    public void ABodyThatIsNotMsgPackOrNotAMessageIsRefusedNamingIt()
    {
        Assert.StartsWith("Socket frame is not valid msgpack: ", Assert.Throws<InvalidRequest>(() => Protocol.DecodeBody([0xc1])).Message);
        Assert.StartsWith("Socket frame is not valid msgpack: ", Assert.Throws<InvalidRequest>(() => Protocol.DecodeBody([0x81])).Message);
        Assert.StartsWith("incoming socket message does not match the socket schema:",
            Assert.Throws<InvalidRequest>(() => Protocol.DecodeBody(MsgPack.Encode(new JsonArray(1L)))).Message);
        Assert.StartsWith("incoming socket message does not match predict in the socket schema:",
            Assert.Throws<InvalidRequest>(() => Protocol.DecodeBody(MsgPack.Encode(new JsonObject { ["op"] = "predict" }))).Message);
    }

    [Fact]
    public void AMessageMsgPackCannotPackIsRefusedAsPythonWordsIt()
    {
        var message = new JsonObject { ["op"] = "installed", ["id"] = Json.Whole(System.Numerics.BigInteger.Pow(2, 64)) };
        Assert.Equal("The outgoing socket message cannot be packed with msgpack: OverflowError: Integer value out of range",
            Assert.Throws<InvalidRequest>(() => Protocol.EncodeFrame(message, "simple")).Message);
    }

    [Fact]
    public void AnOutgoingMessageMustMatchTheSocketSchema()
    {
        var error = Assert.Throws<InvalidRequest>(() => Protocol.EncodeFrame(new JsonObject { ["op"] = "predict", ["id"] = 1L, ["adapter"] = "a" }));
        Assert.StartsWith("outgoing socket message does not match predict in the socket schema:", error.Message);
        Assert.Equal("predict", Protocol.SocketDefinition(new JsonObject { ["op"] = "predict" }));
        Assert.Equal("engine_hello", Protocol.SocketDefinition(Protocol.Hello(3)));
        Assert.Equal("hello", Protocol.SocketDefinition(Protocol.Hello()));
        Assert.Equal("failure_reply", Protocol.SocketDefinition(new JsonObject { ["ok"] = false }));
        Assert.Equal("ok_reply", Protocol.SocketDefinition(new JsonObject { ["ok"] = true }));
        Assert.Null(Protocol.SocketDefinition(new JsonObject { ["op"] = 5L }));
        Assert.Null(Protocol.SocketDefinition(new JsonObject { ["op"] = "toString" }));
        Assert.Null(Protocol.SocketDefinition(new JsonObject { ["ok"] = 1L }));
        Assert.Null(Protocol.SocketDefinition(new JsonArray()));
    }

    [Fact]
    public void AHelloOfAnotherVersionIsRefusedNamingIt()
    {
        Protocol.CheckHello(Protocol.Hello());
        Assert.Equal("This side speaks protocol 2, the other side 1; upgrade the older one",
            Assert.Throws<ProtocolMismatch>(() => Protocol.CheckHello(new JsonObject { ["op"] = "hello", ["version"] = 1L })).Message);
        Assert.Equal("Expected a hello, got 'installed'", Assert.Throws<ProtocolMismatch>(() => Protocol.CheckHello(Installed())).Message);
        Protocol.CheckHello(new JsonObject { ["op"] = "hello", ["version"] = 2.0 }); // Python: 2.0 == 2
    }

    [Fact]
    public void AHelloIsQuotedAsPythonQuotesIt()
    {
        // Python: the op with repr(), the version with str(); a missing field is None
        Assert.Equal("Expected a hello, got None", Assert.Throws<ProtocolMismatch>(() => Protocol.CheckHello(new JsonObject())).Message);
        Assert.Equal("This side speaks protocol 2, the other side None; upgrade the older one",
            Assert.Throws<ProtocolMismatch>(() => Protocol.CheckHello(new JsonObject { ["op"] = "hello" })).Message);
        Assert.Equal("This side speaks protocol 2, the other side two; upgrade the older one",
            Assert.Throws<ProtocolMismatch>(() => Protocol.CheckHello(new JsonObject { ["op"] = "hello", ["version"] = "two" })).Message);
        Assert.Equal("This side speaks protocol 2, the other side True; upgrade the older one",
            Assert.Throws<ProtocolMismatch>(() => Protocol.CheckHello(new JsonObject { ["op"] = "hello", ["version"] = true })).Message);
    }
}
