using System.Buffers.Binary;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>The engine socket protocol (contract/socket.md; port of jeff/protocol.py): each frame is a 4-byte
/// big-endian length, then a MessagePack map. Replies carry the request id and may arrive in any order.</summary>
internal static class Protocol
{
    /// <summary>2: the base served as adapter "base", and input_tokens in predict results.</summary>
    public const int Version = 2;
    public const int MaxFrame = 64 * 1024 * 1024;
    public const int HeaderSize = 4;

    /// <summary>The server's low-latency channel: its path, the largest frame body it reads (the HTTP API's body limit,
    /// 50 MiB) and the largest before the connection is authenticated. FrameReader enforces only MaxFrame: a reader of
    /// the channel checks these limits itself.</summary>
    public const string ChannelPath = "/v1/channel";
    public const int MaxBody = 50 * 1024 * 1024;
    public const int PreauthMaxBody = 64 * 1024;

    /// <summary>The socket schema's definition for each request op (contract/schemas/socket.schema.json).</summary>
    private static readonly IReadOnlyDictionary<string, string> OpDefinitions = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["installed"] = "simple", ["reload"] = "simple", ["stats"] = "simple", ["health"] = "simple", ["predict"] = "predict",
        ["serve"] = "serve", ["auth"] = "auth", ["refresh"] = "auth", ["bind"] = "bind", ["call"] = "call", ["call_many"] = "call_many",
        ["release"] = "release", ["decide"] = "decide", ["dispatch"] = "dispatch", ["graph"] = "graph",
    };

    /// <summary>The one $defs entry of the socket schema a message says it is (its op, or for a reply its ok); null when
    /// the tag names no entry, and the whole schema is then checked.</summary>
    public static string? SocketDefinition(JsonNode? message)
    {
        if (message is not JsonObject record) return null;
        if (record.ContainsKey("op"))
        {
            if (Json.KindOf(record["op"]) != JsonKind.String) return null;
            string op = record["op"]!.GetValue<string>();
            if (op == "hello") return record.ContainsKey("adapters_version") ? "engine_hello" : "hello";
            return OpDefinitions.TryGetValue(op, out var definition) ? definition : null;
        }
        if (Json.IsTrue(record["ok"])) return "ok_reply";
        if (Json.IsFalse(record["ok"])) return "failure_reply";
        return null;
    }

    /// <summary>The message if it matches the socket schema (the definition given, else the one its tag names);
    /// InvalidRequest listing every problem if not.</summary>
    public static JsonObject ValidateMessage(JsonNode? message, string where, string? definition = null)
    {
        try
        {
            Contract.Validate("socket", message, where, definition ?? SocketDefinition(message));
        }
        catch (ContractError error)
        {
            throw new InvalidRequest(error.Message);
        }
        return (JsonObject)message!;
    }

    /// <summary>One frame: the message checked against the socket schema, packed, behind its 4-byte length.
    /// InvalidRequest for a message that does not match, cannot be packed, or packs larger than 64 MiB.</summary>
    public static byte[] EncodeFrame(JsonObject message, string? definition = null)
    {
        ValidateMessage(message, "outgoing socket message", definition);
        byte[] payload;
        try
        {
            payload = MsgPack.Encode(message);
        }
        catch (MsgPackError error)
        {
            // MsgPack's messages already carry the Python exception's name where Python's do (OverflowError: ...)
            throw new InvalidRequest($"The outgoing socket message cannot be packed with msgpack: {error.Message}");
        }
        if (payload.Length > MaxFrame) throw new InvalidRequest($"Socket message of {payload.Length} bytes is over the 64 MiB limit");
        var frame = new byte[HeaderSize + payload.Length];
        BinaryPrimitives.WriteUInt32BigEndian(frame, (uint)payload.Length);
        payload.CopyTo(frame, HeaderSize);
        return frame;
    }

    /// <summary>The body length a frame header announces; InvalidRequest over 64 MiB.</summary>
    public static int FrameLength(ReadOnlySpan<byte> header)
    {
        uint length = BinaryPrimitives.ReadUInt32BigEndian(header);
        if (length > MaxFrame) throw new InvalidRequest($"Socket frame of {length} bytes is over the 64 MiB limit");
        return (int)length;
    }

    /// <summary>A frame body unpacked and checked against the socket schema.</summary>
    public static JsonObject DecodeBody(ReadOnlySpan<byte> body)
    {
        JsonNode? message;
        try
        {
            message = MsgPack.Decode(body);
        }
        catch (MsgPackError error)
        {
            throw new InvalidRequest($"Socket frame is not valid msgpack: {error.Message}");
        }
        return ValidateMessage(message, "incoming socket message");
    }

    /// <summary>The hello this side sends: an engine's carries the version of its set of adapters.</summary>
    public static JsonObject Hello(long? adaptersVersion = null)
    {
        var message = new JsonObject { ["op"] = "hello", ["version"] = (long)Version };
        if (adaptersVersion is { } version) message["adapters_version"] = version;
        return message;
    }

    /// <summary>ProtocolMismatch unless the message is a hello of this side's version (compared as Python compares:
    /// 2.0 is 2). The op is quoted with repr() and the version with str(), as Python quotes them.</summary>
    public static void CheckHello(JsonObject message)
    {
        if (Json.KindOf(message["op"]) != JsonKind.String || message["op"]!.GetValue<string>() != "hello")
        {
            throw new ProtocolMismatch($"Expected a hello, got {PyFormat.Repr(message["op"])}");
        }
        var version = message["version"];
        if (!PyFormat.PyEquals(version, JsonValue.Create((long)Version)))
        {
            throw new ProtocolMismatch($"This side speaks protocol {Version}, the other side {PyFormat.StrOf(version)}; upgrade the older one");
        }
    }
}

/// <summary>Splits a byte stream into frame bodies. A body's buffer grows with the bytes that have actually arrived
/// (doubling, never past the length its header announced), so a peer that announces 64 MiB and sends a few bytes costs
/// a few bytes, not 64 MiB; once the body is whole the buffer is the body, with no further copy. A header announcing
/// more than 64 MiB is refused as soon as it is read: Push throws it, or, when frames before it came in the same chunk,
/// returns those and keeps the error in Failure; every later Push throws it. FrameReader enforces only
/// Protocol.MaxFrame; a caller with a lower limit (the channel's MaxBody, PreauthMaxBody) checks it itself.</summary>
internal sealed class FrameReader
{
    /// <summary>The first buffer a body gets (or the body's length, if smaller).</summary>
    private const int FirstCapacity = 4096;

    private readonly byte[] header = new byte[Protocol.HeaderSize];
    private int headerFilled;
    private int expected = -1;
    private byte[] body = [];
    private int bodyFilled;

    /// <summary>The error that ended the stream; set when a push refused a header.</summary>
    public JeffError? Failure { get; private set; }

    /// <summary>Every byte copied so far: each received byte once, plus the bytes moved when a body's buffer grows,
    /// which by doubling stay under twice the body's length (reading stays linear in the frame size).</summary>
    internal long CopiedBytes { get; private set; }

    public List<byte[]> Push(ReadOnlySpan<byte> chunk)
    {
        if (Failure is not null) throw Failure;
        var bodies = new List<byte[]>();
        while (true)
        {
            if (expected < 0)
            {
                int take = Math.Min(Protocol.HeaderSize - headerFilled, chunk.Length);
                chunk[..take].CopyTo(header.AsSpan(headerFilled));
                headerFilled += take;
                CopiedBytes += take;
                chunk = chunk[take..];
                if (headerFilled < Protocol.HeaderSize) break;
                try
                {
                    expected = Protocol.FrameLength(header);
                }
                catch (InvalidRequest error)
                {
                    Failure = error;
                    if (bodies.Count == 0) throw;
                    break;
                }
                headerFilled = 0;
                body = [];
                bodyFilled = 0;
            }
            int part = Math.Min(expected - bodyFilled, chunk.Length);
            if (bodyFilled + part > body.Length) Grow(bodyFilled + part);
            chunk[..part].CopyTo(body.AsSpan(bodyFilled));
            bodyFilled += part;
            CopiedBytes += part;
            chunk = chunk[part..];
            if (bodyFilled < expected) break;
            bodies.Add(body);
            body = [];
            expected = -1;
        }
        return bodies;
    }

    /// <summary>A body buffer holding at least `needed` bytes: double the current one, at least FirstCapacity, never
    /// more than the announced length.</summary>
    private void Grow(int needed)
    {
        long doubled = Math.Max((long)body.Length * 2, FirstCapacity);
        int capacity = (int)Math.Min(expected, Math.Max(needed, doubled));
        var grown = new byte[capacity];
        body.AsSpan(0, bodyFilled).CopyTo(grown);
        CopiedBytes += bodyFilled;
        body = grown;
    }
}
