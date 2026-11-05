using Jeff.Sdk.Tests.Helpers;

namespace Jeff.Sdk.Tests;

/// <summary>The channel against a server written at the level of WebSocket frames: keepalive pings (as the Python
/// SDK's websockets sends them), a ping between the frames of one message, and close codes RFC 6455 does not allow.</summary>
public class ChannelFrameTests
{
    private static readonly IReadOnlyList<KeyValuePair<string, string>> NoProxies = [];

    private static Task<ChannelTransport> Open(RawChannelServer server, TimeSpan keepAlive) =>
        ChannelTransport.OpenAsync(server.Url, null, TimeSpan.FromSeconds(5), NoProxies, keepAlive, CancellationToken.None);

#if NET9_0_OR_GREATER
    [Fact]
    public void TheNet9BuildPings() => Assert.True(ChannelProtocol.Pings);

    [Fact]
    public async Task AMissingPongEndsTheChannelWithBackendUnavailableNamingIt()
    {
        await using var server = RawChannelServer.Start(new RawChannelServer.Settings { AnswerPings = false });
        await using var channel = await Open(server, TimeSpan.FromMilliseconds(300));
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => channel.ReleaseAsync(1, CancellationToken.None));
        Assert.True(server.Pings >= 1, "the channel sent no keepalive ping");
        Assert.Equal($"{channel.WsUrl}: the channel closed (the server sent no pong to a keepalive ping within 300 ms)", error.Message);
        Assert.True(channel.Closed);
    }

    [Fact]
    public async Task AServerThatAnswersPingsKeepsTheChannelOpen()
    {
        await using var server = RawChannelServer.Start(new RawChannelServer.Settings());
        await using var channel = await Open(server, TimeSpan.FromMilliseconds(100));
        await Task.Delay(700);
        Assert.True(server.Pings >= 3, $"the channel sent {server.Pings} keepalive pings in 700 ms, one every 100 ms expected");
        Assert.False(channel.Closed);
    }

#else
    /// <summary>The net8.0 build cannot ping (the .NET 8 ClientWebSocket has no way to send one), so it sends none.</summary>
    [Fact]
    public async Task TheNet8BuildSendsNoPings()
    {
        Assert.False(ChannelProtocol.Pings);
        await using var server = RawChannelServer.Start(new RawChannelServer.Settings { AnswerPings = false });
        await using var channel = await Open(server, TimeSpan.FromMilliseconds(100));
        await Task.Delay(500);
        Assert.Equal(0, server.Pings);
        Assert.False(channel.Closed);
    }

#endif
    [Fact]
    public void TheChannelPingsAsPythonsWebsocketsDoes() => Assert.Equal(TimeSpan.FromSeconds(20), ChannelProtocol.KeepAlive);

    [Fact]
    public async Task APingBetweenTheFramesOfOneMessageDoesNotLoseTheMessage()
    {
        await using var server = RawChannelServer.Start(new RawChannelServer.Settings { FragmentHello = true });
        await using var channel = await Open(server, ChannelProtocol.KeepAlive); // the hello, sent as two frames around a ping, is read whole
        Assert.False(channel.Closed);
    }

    [Theory]
    [InlineData(999)]
    [InlineData(1004)]
    [InlineData(1005)]
    [InlineData(1006)]
    [InlineData(1015)]
    [InlineData(1016)]
    [InlineData(2999)]
    [InlineData(5000)]
    public async Task ACloseCodeTheRfcDoesNotAllowEndsTheChannelWithBackendUnavailable(int code)
    {
        await using var server = RawChannelServer.Start(new RawChannelServer.Settings { CloseAfterHello = (ushort)code });
        await using var channel = await Open(server, ChannelProtocol.KeepAlive);
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => channel.ReleaseAsync(1, CancellationToken.None));
        Assert.StartsWith($"{channel.WsUrl}: the channel closed (the server sent a WebSocket frame that RFC 6455 does not allow, such as a close code outside "
            + "1000-1003, 1007-1014 and 3000-4999: WebSocketException: ", error.Message);
    }

    [Theory]
    [InlineData(1000)]
    [InlineData(1001)]
    [InlineData(1011)]
    [InlineData(3000)]
    [InlineData(4999)]
    public async Task AnAllowedCloseCodeIsNamed(int code)
    {
        await using var server = RawChannelServer.Start(new RawChannelServer.Settings { CloseAfterHello = (ushort)code });
        await using var channel = await Open(server, ChannelProtocol.KeepAlive);
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() => channel.ReleaseAsync(1, CancellationToken.None));
        Assert.Equal($"{channel.WsUrl}: the channel closed (WebSocket close code {code})", error.Message);
    }
}
