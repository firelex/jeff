using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Helpers;

/// <summary>A channel server written at the level of WebSocket frames (RFC 6455), for what a WebSocket library on the
/// server side would never do: leave pings unanswered, put a ping between the frames of one message, or send a close
/// frame with a code the RFC does not allow. It answers the channel's hello and nothing else.</summary>
internal sealed class RawChannelServer : IAsyncDisposable
{
    private readonly TcpListener listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource stopping = new();
    private readonly List<TcpClient> clients = new();
    private readonly Task accepting;
    private int pings;

    public sealed class Settings
    {
        /// <summary>Answer each ping with a pong, as every WebSocket library does.</summary>
        public bool AnswerPings { get; init; } = true;

        /// <summary>Send the hello's reply as two frames of one message with a ping between them.</summary>
        public bool FragmentHello { get; init; }

        /// <summary>Right after the hello's reply, send a close frame with this code.</summary>
        public ushort? CloseAfterHello { get; init; }
    }

    private readonly Settings settings;

    private RawChannelServer(Settings settings)
    {
        this.settings = settings;
        listener.Start();
        accepting = AcceptAsync();
    }

    public static RawChannelServer Start(Settings settings) => new(settings);

    public string Url => $"http://127.0.0.1:{((IPEndPoint)listener.LocalEndpoint).Port}";

    /// <summary>How many pings the client has sent.</summary>
    public int Pings => Volatile.Read(ref pings);

    private async Task AcceptAsync()
    {
        while (true)
        {
            TcpClient client;
            try
            {
                client = await listener.AcceptTcpClientAsync(stopping.Token);
            }
            catch (Exception error) when (error is OperationCanceledException or SocketException or ObjectDisposedException)
            {
                return; // the test is closing the server
            }
            lock (clients) clients.Add(client);
            _ = Task.Run(async () =>
            {
                try
                {
                    using (client) await ServeAsync(client.GetStream()); // the server closes the TCP connection after the closing handshake
                }
                catch (Exception error) when (error is IOException or SocketException or ObjectDisposedException or OperationCanceledException or EndOfStreamException)
                {
                    // the client or the test ended the connection
                }
            });
        }
    }

    private async Task ServeAsync(NetworkStream stream)
    {
        // the opening handshake: read the request's head, answer 101 with the key's accept value
        var head = new StringBuilder();
        var one = new byte[1];
        while (!head.ToString().EndsWith("\r\n\r\n", StringComparison.Ordinal))
        {
            if (await stream.ReadAsync(one, stopping.Token) == 0) return;
            head.Append((char)one[0]);
        }
        string key = head.ToString().Split("\r\n").First(line => line.StartsWith("Sec-WebSocket-Key:", StringComparison.OrdinalIgnoreCase))[18..].Trim();
        string accept = Convert.ToBase64String(SHA1.HashData(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
        await stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"),
            stopping.Token);
        var message = new MemoryStream();
        bool closeSent = false;
        while (true)
        {
            var (fin, opcode, payload) = await ReadFrameAsync(stream);
            if (opcode == 0x9)
            {
                Interlocked.Increment(ref pings);
                if (settings.AnswerPings) await WriteFrameAsync(stream, true, 0xA, payload);
                continue;
            }
            if (opcode == 0xA) continue; // a pong to the server's own ping
            if (opcode == 0x8)
            {
                if (!closeSent) await WriteFrameAsync(stream, true, 0x8, payload); // the closing handshake: echo the client's close
                return;
            }
            message.Write(payload);
            if (!fin) continue;
            var body = message.ToArray().AsSpan(4).ToArray();
            message.SetLength(0);
            var request = MsgPack.Decode(body)!.AsObject();
            if (request["op"]?.GetValue<string>() != "hello") continue;
            var reply = Frame(new JsonObject { ["op"] = "hello", ["version"] = 2L });
            if (settings.FragmentHello)
            {
                int half = reply.Length / 2;
                await WriteFrameAsync(stream, false, 0x2, reply[..half]);
                await WriteFrameAsync(stream, true, 0x9, Encoding.ASCII.GetBytes("between"));
                await WriteFrameAsync(stream, true, 0x0, reply[half..]);
            }
            else await WriteFrameAsync(stream, true, 0x2, reply);
            if (settings.CloseAfterHello is { } code)
            {
                var close = new byte[2];
                BinaryPrimitives.WriteUInt16BigEndian(close, code);
                await WriteFrameAsync(stream, true, 0x8, close);
                closeSent = true;
            }
        }
    }

    private static byte[] Frame(JsonObject message)
    {
        var body = MsgPack.Encode(message);
        var frame = new byte[4 + body.Length];
        BinaryPrimitives.WriteUInt32BigEndian(frame, (uint)body.Length);
        body.CopyTo(frame, 4);
        return frame;
    }

    private async Task<(bool Fin, int Opcode, byte[] Payload)> ReadFrameAsync(NetworkStream stream)
    {
        var header = new byte[2];
        await stream.ReadExactlyAsync(header, stopping.Token);
        bool fin = (header[0] & 0x80) != 0;
        int opcode = header[0] & 0x0F;
        long length = header[1] & 0x7F;
        if (length == 126)
        {
            var extended = new byte[2];
            await stream.ReadExactlyAsync(extended, stopping.Token);
            length = BinaryPrimitives.ReadUInt16BigEndian(extended);
        }
        else if (length == 127)
        {
            var extended = new byte[8];
            await stream.ReadExactlyAsync(extended, stopping.Token);
            length = (long)BinaryPrimitives.ReadUInt64BigEndian(extended);
        }
        var mask = new byte[4];
        if ((header[1] & 0x80) != 0) await stream.ReadExactlyAsync(mask, stopping.Token);
        var payload = new byte[length];
        await stream.ReadExactlyAsync(payload, stopping.Token);
        for (int index = 0; index < payload.Length; index++) payload[index] ^= mask[index % 4];
        return (fin, opcode, payload);
    }

    private async Task WriteFrameAsync(NetworkStream stream, bool fin, int opcode, byte[] payload)
    {
        var frame = new MemoryStream();
        frame.WriteByte((byte)((fin ? 0x80 : 0) | opcode));
        if (payload.Length < 126) frame.WriteByte((byte)payload.Length);
        else if (payload.Length <= ushort.MaxValue)
        {
            frame.WriteByte(126);
            var extended = new byte[2];
            BinaryPrimitives.WriteUInt16BigEndian(extended, (ushort)payload.Length);
            frame.Write(extended);
        }
        else
        {
            frame.WriteByte(127);
            var extended = new byte[8];
            BinaryPrimitives.WriteUInt64BigEndian(extended, (ulong)payload.Length);
            frame.Write(extended);
        }
        frame.Write(payload);
        await stream.WriteAsync(frame.ToArray(), stopping.Token);
    }

    public async ValueTask DisposeAsync()
    {
        stopping.Cancel();
        listener.Stop();
        lock (clients) foreach (var client in clients) client.Dispose();
        await accepting;
    }
}
