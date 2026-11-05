using System.Net;
using System.Net.Sockets;
using System.Text;

namespace Jeff.Sdk.Tests.Helpers;

/// <summary>A local proxy on 127.0.0.1. Http(): an HTTP proxy recording the first line of each request it is sent:
/// CONNECT host:port opens a tunnel, an absolute-form request (GET http://host:port/path) is passed on as it is to that
/// host; with `credentials` ("user:password") it answers 407 until a request carries them as Proxy-Authorization
/// Basic. Relay(): every connection passed to one upstream port, the client's bytes at most `bytesPerSecond`, through a
/// small receive buffer (a slow uplink).</summary>
internal sealed class TestProxy : IAsyncDisposable
{
    private readonly TcpListener listener = new(IPAddress.Loopback, 0);
    private readonly int? upstream;
    private readonly int? bytesPerSecond;
    private readonly string? credentials;
    private readonly List<Socket> sockets = new();
    private readonly CancellationTokenSource stopping = new();
    private readonly Task accepting;

    private TestProxy(int? upstream, int? bytesPerSecond, string? credentials)
    {
        this.upstream = upstream;
        this.bytesPerSecond = bytesPerSecond;
        this.credentials = credentials;
        listener.Start();
        accepting = Task.Run(AcceptAsync);
    }

    public static TestProxy Http(string? credentials = null) => new(null, null, credentials);

    public static TestProxy Relay(int upstreamPort, int bytesPerSecond) => new(upstreamPort, bytesPerSecond, null);

    public string Url => $"http://127.0.0.1:{((IPEndPoint)listener.LocalEndpoint).Port}";

    /// <summary>The first line of each request the proxy was sent ("CONNECT 127.0.0.1:5000 HTTP/1.1").</summary>
    public List<string> Requests { get; } = new();

    /// <summary>The Proxy-Authorization header of each request that carried one.</summary>
    public List<string> Authorizations { get; } = new();

    public List<string> Seen()
    {
        lock (Requests) return Requests.ToList();
    }

    private async Task AcceptAsync()
    {
        while (!stopping.IsCancellationRequested)
        {
            Socket client;
            try
            {
                client = await listener.AcceptSocketAsync(stopping.Token);
            }
            catch (Exception error) when (error is OperationCanceledException or SocketException or ObjectDisposedException)
            {
                return;
            }
            lock (sockets) sockets.Add(client);
            _ = Task.Run(() => ServeAsync(client));
        }
    }

    private async Task ServeAsync(Socket client)
    {
        try
        {
            if (upstream is { } port)
            {
                client.ReceiveBufferSize = 32 * 1024;
                await PipeAsync(client, await ConnectAsync("127.0.0.1", port), []);
                return;
            }
            var pending = new List<byte>();
            while (true)
            {
                var (head, rest) = await ReadHeadAsync(client, pending);
                if (head is null) return;
                string[] lines = head.Split("\r\n");
                lock (Requests) Requests.Add(lines[0]);
                string? authorization = lines.Skip(1).Where(line => line.StartsWith("Proxy-Authorization:", StringComparison.OrdinalIgnoreCase))
                    .Select(line => line["Proxy-Authorization:".Length..].Trim()).FirstOrDefault();
                if (authorization is not null) lock (Authorizations) Authorizations.Add(authorization);
                if (credentials is not null && authorization != $"Basic {Convert.ToBase64String(Encoding.UTF8.GetBytes(credentials))}")
                {
                    await client.SendAsync(Encoding.ASCII.GetBytes("HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"test\"\r\n"
                        + "Content-Length: 0\r\n\r\n"));
                    pending = rest;
                    continue;
                }
                string[] parts = lines[0].Split(' ');
                if (parts[0] == "CONNECT")
                {
                    int colon = parts[1].LastIndexOf(':');
                    var server = await ConnectAsync(parts[1][..colon].Trim('[', ']'), int.Parse(parts[1][(colon + 1)..]));
                    await client.SendAsync(Encoding.ASCII.GetBytes("HTTP/1.1 200 Connection established\r\n\r\n"));
                    await PipeAsync(client, server, rest.ToArray());
                }
                else
                {
                    var target = new Uri(parts[1]);
                    var server = await ConnectAsync(target.Host, target.Port);
                    await PipeAsync(client, server, [.. Encoding.ASCII.GetBytes(head + "\r\n\r\n"), .. rest]);
                }
                return;
            }
        }
        catch (Exception error) when (error is SocketException or IOException or ObjectDisposedException or OperationCanceledException)
        {
            // the client or the test ended the connection
        }
        finally
        {
            client.Dispose();
        }
    }

    private async Task<Socket> ConnectAsync(string host, int port)
    {
        var server = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
        await server.ConnectAsync(host, port, stopping.Token);
        lock (sockets) sockets.Add(server);
        return server;
    }

    /// <summary>The request head (without its blank line) and the bytes read after it; null head when the client closed.</summary>
    private async Task<(string? Head, List<byte> After)> ReadHeadAsync(Socket client, List<byte> pending)
    {
        var buffer = new byte[4096];
        while (true)
        {
            var text = Encoding.ASCII.GetString(pending.ToArray());
            int end = text.IndexOf("\r\n\r\n", StringComparison.Ordinal);
            if (end >= 0) return (text[..end], pending.Skip(end + 4).ToList());
            int read = await client.ReceiveAsync(buffer, SocketFlags.None, stopping.Token);
            if (read == 0) return (null, pending);
            pending.AddRange(buffer.AsSpan(0, read).ToArray());
        }
    }

    private async Task PipeAsync(Socket client, Socket server, byte[] first)
    {
        if (first.Length > 0) await server.SendAsync(first);
        var up = CopyAsync(client, server, bytesPerSecond);
        var down = CopyAsync(server, client, null);
        await Task.WhenAny(up, down);
        foreach (var side in new[] { client, server })
        {
            try
            {
                side.Shutdown(SocketShutdown.Both);
            }
            catch (Exception error) when (error is SocketException or ObjectDisposedException)
            {
                // already closed
            }
        }
        await Task.WhenAll(up, down);
    }

    private async Task CopyAsync(Socket from, Socket to, int? rate)
    {
        var buffer = new byte[rate is null ? 64 * 1024 : 16 * 1024];
        try
        {
            while (true)
            {
                int read = await from.ReceiveAsync(buffer, SocketFlags.None, stopping.Token);
                if (read == 0) return;
                await to.SendAsync(buffer.AsMemory(0, read), SocketFlags.None, stopping.Token);
                if (rate is { } bps) await Task.Delay(TimeSpan.FromSeconds((double)read / bps), stopping.Token);
            }
        }
        catch (Exception error) when (error is SocketException or IOException or ObjectDisposedException or OperationCanceledException)
        {
            // one side ended
        }
    }

    public async ValueTask DisposeAsync()
    {
        stopping.Cancel();
        listener.Stop();
        lock (sockets) foreach (var socket in sockets) socket.Dispose();
        await accepting;
    }
}
