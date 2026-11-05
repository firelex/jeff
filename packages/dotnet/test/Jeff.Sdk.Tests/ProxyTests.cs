using System.Text.Json.Nodes;
using Jeff.Sdk.Tests.Helpers;

namespace Jeff.Sdk.Tests;

/// <summary>Proxies from the environment, as the Python SDK's httpx (HTTP requests) and websockets (the channel) use
/// them. The environment is passed in (RemoteOptions.Environment), never set on the test process.</summary>
public class ProxyTests
{
    private static List<KeyValuePair<string, string>> Env(params (string Name, string Value)[] variables) =>
        variables.Select(variable => new KeyValuePair<string, string>(variable.Name, variable.Value)).ToList();

    private static Task<RemoteJeffClient> Open(string url, List<KeyValuePair<string, string>> environment, bool channel) =>
        JeffClient.RemoteAsync(url, TokenSource.Of("secret-token"), new RemoteOptions { Timeout = TimeSpan.FromSeconds(3), Channel = channel, Environment = environment });

    private static Call Urgency() => new("triage.urgency", "x", new JsonObject { ["company"] = "A shop.", ["channel"] = "email" });

    /// <summary>Data/python-proxies.json: for 33 environments and 8 server URLs, what httpx and websockets chose
    /// ("direct", "proxy scheme://host:port[ auth]", "error Type"), generated with the Python SDK's environment.</summary>
    [Fact]
    public void TheProxyChosenForEveryEnvironmentAndUrlIsTheOneHttpxAndWebsocketsChoose()
    {
        var rows = Json.Parse(File.ReadAllText(Repo.PathOf("packages", "dotnet", "test", "Jeff.Sdk.Tests", "Data", "python-proxies.json")), "python-proxies.json")!.AsArray();
        Assert.Equal(264, rows.Count);
        foreach (var row in rows.Select(item => item!.AsObject()))
        {
            var environment = row["env"]!.AsObject().Select(pair => new KeyValuePair<string, string>(pair.Key, pair.Value!.GetValue<string>())).ToList();
            string url = row["url"]!.GetValue<string>();
            string where = $"{row["env"]!.ToJsonString()} {url}";
            Check(row["http"]!.GetValue<string>(), () => PyProxies.ForHttp(environment, url), where + " (http)");
            Check(row["channel"]!.GetValue<string>(), () => PyProxies.ForChannel(environment, ChannelProtocol.Url(url)), where + " (channel)");
        }
    }

    private static void Check(string expected, Func<ProxyTarget?> choose, string where)
    {
        if (expected.StartsWith("error ", StringComparison.Ordinal) || expected.StartsWith("proxy socks", StringComparison.Ordinal))
        {
            // Python fails at connect for these (a bad proxy URL; a SOCKS proxy without its optional packages)
            var error = Assert.Throws<InvalidRequest>(() => choose());
            if (expected is "error ImportError" || expected.StartsWith("proxy socks", StringComparison.Ordinal)) Assert.Contains("SOCKS", error.Message);
            Assert.DoesNotContain("pa%40ss", error.Message);
            return;
        }
        Assert.True(expected == (choose()?.ToString() ?? "direct"), $"{where}: expected {expected}, got {choose()?.ToString() ?? "direct"}");
    }

    [Fact]
    public async Task WithHttpProxySetTheRequestsAndTheChannelGoThroughTheProxyAndNoProxyBypassesIt()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var proxy = TestProxy.Http();
        string authority = new Uri(server.Url).Authority;
        // the HTTP transport (the fake server answers only GET /v1/functions over HTTP, which connecting reads)
        await (await Open(server.Url, Env(("HTTP_PROXY", proxy.Url)), channel: false)).DisposeAsync();
        await using (var channel = await Open(server.Url, Env(("http_proxy", proxy.Url)), channel: true))
        {
            Assert.Equal(2L, (await channel.DecideAsync([Urgency()]))[0].Value.GetValue<long>());
        }
        // the HTTP requests in absolute form (forwarded), the channel through a CONNECT tunnel
        Assert.Equal(2, proxy.Seen().Count(line => line == $"GET http://{authority}/v1/functions HTTP/1.1"));
        Assert.Contains($"CONNECT {authority} HTTP/1.1", proxy.Seen());
        int before = proxy.Seen().Count;
        await using (var bypassed = await Open(server.Url, Env(("HTTP_PROXY", proxy.Url), ("NO_PROXY", "127.0.0.1")), channel: true))
        {
            Assert.Equal(2L, (await bypassed.DecideAsync([Urgency()]))[0].Value.GetValue<long>());
        }
        Assert.Equal(before, proxy.Seen().Count);
    }

    [Fact]
    public async Task HttpsProxyAloneCarriesTheChannelToAnHttpServerButNotItsHttpRequests()
    {
        // websockets tries https_proxy for ws:// too; httpx uses it for https:// only
        await using var server = await ChannelServer.StartAsync();
        await using var proxy = TestProxy.Http();
        string authority = new Uri(server.Url).Authority;
        await using var jeff = await Open(server.Url, Env(("HTTPS_PROXY", proxy.Url)), channel: true);
        Assert.Equal(2L, (await jeff.DecideAsync([Urgency()]))[0].Value.GetValue<long>());
        Assert.Equal(new[] { $"CONNECT {authority} HTTP/1.1" }, proxy.Seen());
    }

    [Fact]
    public async Task AnHttpsServerIsReachedThroughAConnectTunnelOverHttpAndOverTheChannel()
    {
        // the tunnel leads to a plain HTTP server, so TLS fails after it: what is checked is that the tunnel was asked for
        await using var server = await ChannelServer.StartAsync();
        await using var proxy = TestProxy.Http();
        string authority = new Uri(server.Url).Authority;
        string https = $"https://{authority}";
        await Assert.ThrowsAnyAsync<JeffError>(() => Open(https, Env(("HTTPS_PROXY", proxy.Url)), channel: false));
        Assert.Equal(new[] { $"CONNECT {authority} HTTP/1.1" }, proxy.Seen().Distinct());
        await using var wssOnly = TestProxy.Http();
        await Assert.ThrowsAnyAsync<JeffError>(() => ChannelTransport.OpenAsync(https, TokenSource.Of("secret-token"), TimeSpan.FromSeconds(3),
            Env(("WSS_PROXY", wssOnly.Url)), default));
        Assert.Equal(new[] { $"CONNECT {authority} HTTP/1.1" }, wssOnly.Seen());
    }

    [Fact]
    public async Task AProxyAskingForCredentialsGetsTheOnesInItsUrl()
    {
        await using var server = await ChannelServer.StartAsync();
        await using var proxy = TestProxy.Http("alice:s3cr@t");
        string url = proxy.Url.Replace("http://", "http://alice:s3cr%40t@");
        await using var jeff = await Open(server.Url, Env(("HTTP_PROXY", url)), channel: true);
        Assert.Equal(2L, (await jeff.DecideAsync([Urgency()]))[0].Value.GetValue<long>());
        Assert.Contains("Basic YWxpY2U6czNjckB0", proxy.Authorizations);
    }

    [Fact]
    public async Task ASocksOrUnusableProxyIsRefusedByNameBeforeAnythingIsSent()
    {
        await using var server = await ChannelServer.StartAsync();
        var socks = await Assert.ThrowsAsync<InvalidRequest>(() => Open(server.Url, Env(("ALL_PROXY", "socks5://u:pw@127.0.0.1:1")), channel: false));
        Assert.Equal("The all_proxy environment variable names a SOCKS proxy (socks5); the Jeff SDK connects through HTTP and HTTPS proxies only, as the Python "
            + "SDK does without its optional SOCKS packages", socks.Message);
        var bare = await Assert.ThrowsAsync<InvalidRequest>(() => Open(server.Url, Env(("NO_PROXY", "x"), ("HTTPS_PROXY", "proxy:3128")), channel: true));
        Assert.Equal("The https_proxy environment variable does not name a proxy the channel can use: the scheme 'proxy' isn't supported (it must be the URL of "
            + "an HTTP or HTTPS proxy, such as http://proxy:3128)", bare.Message);
        Assert.Empty(server.Headers);
    }
}

public class ProxyIsolationTests
{
    [Fact]
    public void TheTestAssemblyPinsAnEmptyProxyEnvironmentSoTheMachinesVariablesNeverReachATest()
    {
        Assert.Empty(PyProxies.ProcessEnvironment());
        Assert.Null(PyProxies.ForHttp(PyProxies.ProcessEnvironment(), "http://127.0.0.1:1"));
        Assert.Null(PyProxies.ForChannel(PyProxies.ProcessEnvironment(), "ws://127.0.0.1:1/v1/channel"));
    }
}
