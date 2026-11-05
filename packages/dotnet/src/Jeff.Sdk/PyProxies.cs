using System.Net;
using System.Text.RegularExpressions;

namespace Jeff.Sdk;

/// <summary>A proxy to connect through: its scheme (http or https: the connection to the proxy itself is TLS for https),
/// host (without brackets), port and the Basic credentials its URL carries.</summary>
internal sealed record ProxyTarget(string Scheme, string Host, int Port, string? User, string? Password)
{
    public Uri Uri => new($"{Scheme}://{(Host.Contains(':') ? $"[{Host}]" : Host)}:{Port}/");

    /// <summary>What a test compares: "proxy scheme://host:port", with " auth" when it carries credentials.</summary>
    public override string ToString() => $"proxy {Scheme}://{(Host.Contains(':') ? $"[{Host}]" : Host)}:{Port}{(User is null ? "" : " auth")}";

    /// <summary>The IWebProxy SocketsHttpHandler and ClientWebSocket connect through: this proxy for every request
    /// (each transport has one server), a CONNECT tunnel for https and ws(s), the credentials for a 407 challenge.</summary>
    public IWebProxy WebProxy() => new Fixed(Uri, User is null ? null : new NetworkCredential(User, Password));

    private sealed class Fixed(Uri address, ICredentials? credentials) : IWebProxy
    {
        public ICredentials? Credentials { get; set; } = credentials;

        public Uri GetProxy(Uri destination) => address;

        public bool IsBypassed(Uri host) => false;
    }
}

/// <summary>Which proxy the Python SDK connects through, read from the environment as Python reads it
/// (urllib.request.getproxies_environment): every variable named &lt;scheme&gt;_proxy in any case, a lower-case one
/// winning over the others and an empty lower-case one removing its scheme, HTTP_PROXY ignored when REQUEST_METHOD is
/// set (a CGI script). Two rule sets follow, because the Python SDK's two libraries differ:
/// <list type="bullet">
/// <item>ForHttp: httpx (trust_env=True), for the HTTP requests: http_proxy for http://, https_proxy for https://,
/// all_proxy for both; no_proxy entries as httpx reads them (curl's rules: "*" turns every proxy off, "example.com"
/// matches it and its subdomains, ".example.com" only the subdomains, an IP address or "localhost" itself, an entry
/// with "://" a URL pattern, a port when it has one).</item>
/// <item>ForChannel: websockets (proxy=True), for the channel: wss_proxy, then socks_proxy, then https_proxy for wss://;
/// ws_proxy, socks_proxy, https_proxy, then http_proxy for ws://; all_proxy is not read. no_proxy as
/// urllib.request.proxy_bypass_environment reads it (a suffix match on the host, or host:port).</item>
/// </list>
/// What Python does with no proxy variable at all on macOS and Windows (reading the system's proxy settings) is not
/// ported: without variables there is no proxy. SOCKS proxies are refused by name: the Python SDK cannot use one either,
/// as neither socksio (httpx) nor python-socks (websockets) is installed with it. No message shows a proxy's credentials.</summary>
internal static class PyProxies
{
    /// <summary>For tests: the environment every transport built without an explicit one reads proxies from, in place
    /// of the process's (the test assembly pins it to none, so the machine's proxy variables never reach a test that
    /// does not test proxies). Null: the process's environment.</summary>
    internal static IReadOnlyList<KeyValuePair<string, string>>? PinnedEnvironment { get; set; }

    /// <summary>The process's environment variables, in name order (Python reads them in the order the process holds
    /// them, which matters only for two spellings of one name that are both not lower case).</summary>
    public static IReadOnlyList<KeyValuePair<string, string>> ProcessEnvironment() => PinnedEnvironment ??
        System.Environment.GetEnvironmentVariables().Cast<System.Collections.DictionaryEntry>()
            .Select(entry => new KeyValuePair<string, string>((string)entry.Key, (string?)entry.Value ?? ""))
            .OrderBy(pair => pair.Key, StringComparer.Ordinal).ToList();

    /// <summary>urllib.request.getproxies_environment: scheme (lower case) to proxy URL, as the variables give it.</summary>
    public static Dictionary<string, string> Read(IReadOnlyList<KeyValuePair<string, string>> environment)
    {
        var proxies = new Dictionary<string, string>(StringComparer.Ordinal);
        var named = new List<(string Name, string Value, string Scheme)>();
        foreach (var (name, value) in environment)
        {
            if (name.Length > 5 && name[^6] == '_' && name[^5..].ToLowerInvariant() == "proxy")
            {
                string scheme = name[..^6].ToLowerInvariant();
                named.Add((name, value, scheme));
                if (value.Length > 0) proxies[scheme] = value;
            }
        }
        // CVE-2016-1000110: a CGI script's HTTP_PROXY may come from a client's "Proxy:" header
        if (environment.Any(pair => pair.Key == "REQUEST_METHOD")) proxies.Remove("http");
        foreach (var (name, value, scheme) in named)
        {
            if (!name.EndsWith("_proxy", StringComparison.Ordinal)) continue;
            if (value.Length > 0) proxies[scheme] = value;
            else proxies.Remove(scheme);
        }
        return proxies;
    }

    // ---- httpx

    /// <summary>One of httpx's proxy mounts: a URL pattern and the proxy for the URLs it matches (null: no proxy).</summary>
    private sealed record Mount(string Pattern, string Scheme, string Host, int? Port, Regex? HostPattern, string? Proxy, string? Variable);

    /// <summary>The proxy httpx uses for requests to `url` (the server's URL; every request of a transport goes to
    /// it); null for none. Every proxy the environment names is checked first, whatever `url` is, as httpx builds a
    /// transport for each when the client is made.</summary>
    public static ProxyTarget? ForHttp(IReadOnlyList<KeyValuePair<string, string>> environment, string url)
    {
        var proxies = Read(environment);
        var mounts = new List<Mount>();
        void Put(string pattern, string? proxy, string? variable)
        {
            int existing = mounts.FindIndex(mount => mount.Pattern == pattern);
            var mount = ParsePattern(pattern, proxy, variable);
            if (existing >= 0) mounts[existing] = mount; // a dict key keeps its first place
            else mounts.Add(mount);
        }
        foreach (string scheme in new[] { "http", "https", "all" })
        {
            if (proxies.TryGetValue(scheme, out var value)) Put($"{scheme}://", value.Contains("://", StringComparison.Ordinal) ? value : $"http://{value}", scheme);
        }
        foreach (string entry in (proxies.GetValueOrDefault("no") ?? "").Split(',').Select(item => item.Trim(PythonWhitespace)))
        {
            if (entry == "*")
            {
                mounts.Clear();
                break;
            }
            if (entry.Length == 0) continue;
            if (entry.Contains("://", StringComparison.Ordinal)) Put(entry, null, null);
            else if (IsIp(entry.Split('/')[0], System.Net.Sockets.AddressFamily.InterNetwork)) Put($"all://{entry}", null, null);
            else if (IsIp(entry.Split('/')[0], System.Net.Sockets.AddressFamily.InterNetworkV6)) Put($"all://[{entry}]", null, null);
            else if (entry.ToLowerInvariant() == "localhost") Put($"all://{entry}", null, null);
            else Put($"all://*{entry}", null, null);
        }
        // httpx.Proxy for each proxy: the scheme checked, the credentials taken out
        var targets = mounts.Where(mount => mount.Proxy is not null).ToDictionary(mount => mount.Pattern, mount => HttpxProxy(mount.Proxy!, mount.Variable!));
        var (targetScheme, host, port) = Target(url, nullDefaultPort: true);
        // sorted from the most specific pattern (a port, then the longer host, then the longer scheme); a stable sort
        foreach (var mount in mounts.OrderBy(mount => mount.Port is null ? 1 : 0).ThenBy(mount => -mount.Host.Length).ThenBy(mount => -mount.Scheme.Length))
        {
            if (mount.Scheme.Length > 0 && mount.Scheme != targetScheme) continue;
            if (mount.Host.Length > 0 && mount.HostPattern is not null && !mount.HostPattern.IsMatch(host)) continue;
            if (mount.Port is not null && mount.Port != port) continue;
            return mount.Proxy is null ? null : targets[mount.Pattern];
        }
        return null;
    }

    private static readonly char[] PythonWhitespace = [' ', '\t', '\n', '\r', '\v', '\f'];

    private static bool IsIp(string text, System.Net.Sockets.AddressFamily family) =>
        IPAddress.TryParse(text, out var address) && address.AddressFamily == family
        && (family == System.Net.Sockets.AddressFamily.InterNetworkV6 || text.Count(character => character == '.') == 3);

    private static int? DefaultPort(string scheme) => scheme switch { "http" or "ws" => 80, "https" or "wss" => 443, _ => null };

    /// <summary>An httpx URLPattern ("all://*example.com", "https://", "http://h:8080").</summary>
    private static Mount ParsePattern(string pattern, string? proxy, string? variable)
    {
        int separator = pattern.IndexOf("://", StringComparison.Ordinal);
        string scheme = pattern[..separator].ToLowerInvariant();
        string authority = pattern[(separator + 3)..];
        int slash = authority.IndexOfAny(['/', '?', '#']);
        if (slash >= 0) authority = authority[..slash];
        var (host, port) = SplitAuthority(authority);
        host = host.ToLowerInvariant();
        if (port is not null && port == DefaultPort(scheme)) port = null; // httpx.URL drops a scheme's default port
        string matchScheme = scheme == "all" ? "" : scheme;
        string matchHost = host == "*" ? "" : host;
        Regex? regex = null;
        if (host.Length > 0 && host != "*")
        {
            regex = host.StartsWith("*.", StringComparison.Ordinal) ? new Regex($"^.+\\.{Regex.Escape(host[2..])}$", RegexOptions.CultureInvariant)
                : host.StartsWith('*') ? new Regex($"^(.+\\.)?{Regex.Escape(host[1..])}$", RegexOptions.CultureInvariant)
                : new Regex($"^{Regex.Escape(host)}$", RegexOptions.CultureInvariant);
        }
        return new Mount(pattern, matchScheme, matchHost, port, regex, proxy, variable);
    }

    /// <summary>host and port of "host:port", "[v6]:port" or "host" (the brackets taken off).</summary>
    private static (string Host, int? Port) SplitAuthority(string authority)
    {
        int at = authority.LastIndexOf('@');
        if (at >= 0) authority = authority[(at + 1)..];
        if (authority.StartsWith('['))
        {
            int close = authority.IndexOf(']');
            string inside = close < 0 ? authority[1..] : authority[1..close];
            string rest = close < 0 ? "" : authority[(close + 1)..];
            return (inside, rest.StartsWith(':') && int.TryParse(rest[1..], out int v6Port) ? v6Port : null);
        }
        int colon = authority.LastIndexOf(':');
        if (colon >= 0 && authority[(colon + 1)..].All(char.IsAsciiDigit))
        {
            return (authority[..colon], authority.Length > colon + 1 ? int.Parse(authority[(colon + 1)..], System.Globalization.CultureInfo.InvariantCulture) : null);
        }
        return (authority, null);
    }

    /// <summary>The scheme, host (lower case, no brackets) and port of a server or channel URL; the port null when it is
    /// the scheme's default and `nullDefaultPort` (as httpx.URL has it), else always set (as websockets has it).</summary>
    private static (string Scheme, string Host, int? Port) Target(string url, bool nullDefaultPort)
    {
        var uri = new Uri(url);
        string host = uri.Host.StartsWith('[') ? uri.Host[1..^1] : uri.Host;
        int port = uri.Port;
        return (uri.Scheme, host.ToLowerInvariant(), nullDefaultPort && port == DefaultPort(uri.Scheme) ? null : port);
    }

    /// <summary>httpx.Proxy(url): http, https, socks5 or socks5h; credentials taken out of the URL.</summary>
    private static ProxyTarget HttpxProxy(string url, string variable)
    {
        int separator = url.IndexOf("://", StringComparison.Ordinal);
        string scheme = url[..separator].ToLowerInvariant();
        if (scheme is "socks5" or "socks5h") throw Socks(variable, scheme);
        if (scheme is not ("http" or "https"))
        {
            throw new InvalidRequest($"The {variable}_proxy environment variable names a proxy with the scheme {PyFormat.Str(scheme)}; a proxy URL starts with "
                + "http:// or https://");
        }
        return Parsed(url, scheme, variable);
    }

    private static InvalidRequest Socks(string variable, string scheme) =>
        new($"The {variable}_proxy environment variable names a SOCKS proxy ({scheme}); the Jeff SDK connects through HTTP and HTTPS proxies only, as the "
            + "Python SDK does without its optional SOCKS packages");

    /// <summary>The host, port (the scheme's default when not given) and percent-decoded credentials of a proxy URL.</summary>
    private static ProxyTarget Parsed(string url, string scheme, string variable)
    {
        string rest = url[(url.IndexOf("://", StringComparison.Ordinal) + 3)..];
        int end = rest.IndexOfAny(['/', '?', '#']);
        string authority = end >= 0 ? rest[..end] : rest;
        string? user = null, password = null;
        int at = authority.LastIndexOf('@');
        if (at >= 0)
        {
            string info = authority[..at];
            int colon = info.IndexOf(':');
            user = Uri.UnescapeDataString(colon >= 0 ? info[..colon] : info);
            password = colon >= 0 ? Uri.UnescapeDataString(info[(colon + 1)..]) : null;
            authority = authority[(at + 1)..];
        }
        var (host, port) = SplitAuthority(authority);
        if (host.Length == 0) throw new InvalidRequest($"The {variable}_proxy environment variable names a proxy URL without a host");
        return new ProxyTarget(scheme, host.ToLowerInvariant(), port ?? DefaultPort(scheme)!.Value, user, password);
    }

    // ---- websockets

    /// <summary>The proxy websockets uses for the channel at `wsUrl` (websockets.proxy.get_proxy and parse_proxy);
    /// null for none.</summary>
    public static ProxyTarget? ForChannel(IReadOnlyList<KeyValuePair<string, string>> environment, string wsUrl)
    {
        var proxies = Read(environment);
        var (scheme, host, port) = Target(wsUrl, nullDefaultPort: false);
        if (Bypassed(proxies, $"{host}:{port}")) return null;
        foreach (string key in scheme == "wss" ? new[] { "wss", "socks", "https" } : new[] { "ws", "socks", "https", "http" })
        {
            if (!proxies.TryGetValue(key, out var proxy)) continue;
            if (key == "socks" && proxy.StartsWith("http://", StringComparison.Ordinal)) proxy = "socks5h://" + proxy[7..];
            return WebsocketsProxy(proxy, key);
        }
        return null;
    }

    /// <summary>urllib.request.proxy_bypass_environment(host): no_proxy "*", or an entry (leading dots dropped) that is
    /// the host (with or without its port) or a suffix of it after a dot.</summary>
    private static bool Bypassed(Dictionary<string, string> proxies, string hostAndPort)
    {
        if (!proxies.TryGetValue("no", out var noProxy)) return false;
        if (noProxy == "*") return true;
        string host = hostAndPort.ToLowerInvariant();
        var split = Regex.Match(host, "^(.*):([0-9]*)$", RegexOptions.Singleline);
        string hostOnly = split.Success && split.Groups[2].Length > 0 ? split.Groups[1].Value : host;
        foreach (string raw in noProxy.Split(','))
        {
            string name = raw.Trim(PythonWhitespace);
            if (name.Length == 0) continue;
            name = name.TrimStart('.').ToLowerInvariant();
            if (hostOnly == name || host == name) return true;
            name = "." + name;
            if (hostOnly.EndsWith(name, StringComparison.Ordinal) || host.EndsWith(name, StringComparison.Ordinal)) return true;
        }
        return false;
    }

    /// <summary>websockets.proxy.parse_proxy: the URL as urllib.parse.urlparse reads it (no "http://" is added).</summary>
    private static ProxyTarget WebsocketsProxy(string proxy, string variable)
    {
        string scheme = "";
        int colon = proxy.IndexOf(':');
        if (colon > 0 && char.IsAsciiLetter(proxy[0]) && proxy[..colon].All(character => char.IsAsciiLetterOrDigit(character) || character is '+' or '-' or '.'))
        {
            scheme = proxy[..colon].ToLowerInvariant();
        }
        if (scheme is "socks5h" or "socks5" or "socks4a" or "socks4") throw Socks(variable, scheme);
        if (scheme is not ("http" or "https"))
        {
            throw new InvalidRequest($"The {variable}_proxy environment variable does not name a proxy the channel can use: the scheme {PyFormat.Str(scheme)} "
                + "isn't supported (it must be the URL of an HTTP or HTTPS proxy, such as http://proxy:3128)");
        }
        if (!proxy[(colon + 1)..].StartsWith("//", StringComparison.Ordinal))
        {
            throw new InvalidRequest($"The {variable}_proxy environment variable does not name a proxy the channel can use: its URL has no host");
        }
        string rest = proxy[(colon + 3)..];
        int end = rest.IndexOfAny(['/', '?', '#']);
        string tail = end >= 0 ? rest[end..] : "";
        if (tail is not ("" or "/"))
        {
            throw new InvalidRequest($"The {variable}_proxy environment variable does not name a proxy the channel can use: its URL has a path, query or "
                + "fragment, which is meaningless for a proxy");
        }
        var target = Parsed(proxy, scheme, variable);
        if (target.User is not null && target.Password is null)
        {
            throw new InvalidRequest($"The {variable}_proxy environment variable does not name a proxy the channel can use: it has a username without a password");
        }
        return target;
    }
}
