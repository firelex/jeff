package ai.jeff.sdk;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

/** The proxy a request to a Jeff server goes through, chosen from the environment exactly as httpx chooses it with
 * trust_env=True (the Python SDK's httpx.Client): the variables named <scheme>_proxy in any case, the lower-case
 * spelling winning (urllib's getproxies_environment, HTTP_PROXY ignored when REQUEST_METHOD is set, as for a CGI
 * script); HTTP_PROXY for http://, HTTPS_PROXY for https://, ALL_PROXY for both, a value without "://" read as
 * http://; NO_PROXY's comma-separated entries as httpx turns them into patterns ("*" turns every proxy off; an IPv4 or
 * IPv6 address or "localhost" matches that host; a name matches itself and its subdomains, ".name" only its
 * subdomains; an entry with "://" is a URL pattern), the most specific pattern winning as httpx's URLPattern priority
 * orders them. A proxy URL is http:// or https://; its user name and password become a Proxy-Authorization header. */
final class Proxies {
    /** A proxy: its scheme (http or https), host, port, and the Proxy-Authorization value (null without credentials). */
    record Proxy(String scheme, String host, int port, String authorization) {
        boolean tls() {
            return scheme.equals("https");
        }

        /** As the tests compare it with httpx's choice: scheme://host:port|Proxy-Authorization=... */
        String described() {
            String shownHost = host.contains(":") ? "[" + host + "]" : host;
            return scheme + "://" + shownHost + ":" + port + "|" + (authorization == null ? "" : "Proxy-Authorization=" + authorization);
        }

        @Override
        public String toString() {
            return "Proxy[" + scheme + "://" + host + ":" + port + (authorization == null ? "" : ", with credentials (not shown)") + "]";
        }
    }

    private Proxies() {}

    /** urllib.request.getproxies_environment: scheme -> proxy URL. */
    static Map<String, String> fromEnvironment(Map<String, String> environment) {
        Map<String, String> proxies = new LinkedHashMap<>();
        List<String[]> found = new ArrayList<>();
        for (Map.Entry<String, String> entry : environment.entrySet()) {
            String name = entry.getKey();
            if (name.length() > 5 && name.charAt(name.length() - 6) == '_'
                && name.substring(name.length() - 5).toLowerCase(Locale.ROOT).equals("proxy")) {
                String scheme = name.substring(0, name.length() - 6).toLowerCase(Locale.ROOT);
                found.add(new String[] {name, entry.getValue(), scheme});
                if (!entry.getValue().isEmpty()) proxies.put(scheme, entry.getValue());
            }
        }
        if (environment.containsKey("REQUEST_METHOD")) proxies.remove("http");
        for (String[] each : found) {
            if (each[0].endsWith("_proxy")) {
                if (!each[1].isEmpty()) proxies.put(each[2], each[1]);
                else proxies.remove(each[2]);
            }
        }
        return proxies;
    }

    /** One of httpx's URLPattern: an empty scheme or host matches any; a host is matched by httpx's regular expression. */
    private record UrlPattern(String scheme, String host, Integer port, Pattern hostRegex, Proxy proxy, int order) {
        static UrlPattern of(String text, Proxy proxy, int order) {
            int colon = text.indexOf("://");
            String scheme = text.substring(0, colon).toLowerCase(Locale.ROOT);
            String rest = text.substring(colon + 3);
            int end = rest.length();
            for (char stop : new char[] {'/', '?', '#'}) if (rest.indexOf(stop) >= 0) end = Math.min(end, rest.indexOf(stop));
            String authority = rest.substring(0, end);
            int at = authority.lastIndexOf('@');
            if (at >= 0) authority = authority.substring(at + 1);
            String host;
            String port = null;
            if (authority.startsWith("[")) {
                int close = authority.indexOf(']');
                host = close < 0 ? authority.substring(1) : authority.substring(1, close);
                if (close >= 0 && authority.length() > close + 1 && authority.charAt(close + 1) == ':') port = authority.substring(close + 2);
            } else {
                int portColon = authority.lastIndexOf(':');
                host = portColon < 0 ? authority : authority.substring(0, portColon);
                if (portColon >= 0) port = authority.substring(portColon + 1);
            }
            host = host.toLowerCase(Locale.ROOT);
            if (port != null && !port.isEmpty() && (port.length() > 5 || !port.chars().allMatch(c -> c >= '0' && c <= '9'))) {
                throw new JeffError.InvalidRequest("The proxy pattern " + PyFormat.str(text) + " (from NO_PROXY) has an invalid port "
                    + PyFormat.str(port));
            }
            Integer number = port == null || port.isEmpty() ? null : Integer.valueOf(port);
            if (number != null && number.equals(defaultPort(scheme))) number = null;
            Pattern regex = null;
            if (!host.isEmpty() && !host.equals("*")) {
                if (host.startsWith("*.")) regex = Pattern.compile("^.+\\." + Pattern.quote(host.substring(2)) + "$");
                else if (host.startsWith("*")) regex = Pattern.compile("^(.+\\.)?" + Pattern.quote(host.substring(1)) + "$");
                else regex = Pattern.compile("^" + Pattern.quote(host) + "$");
            }
            return new UrlPattern(scheme.equals("all") ? "" : scheme, host.equals("*") ? "" : host, number, regex, proxy, order);
        }

        boolean matches(String urlScheme, String urlHost, Integer urlPort) {
            if (!scheme.isEmpty() && !scheme.equals(urlScheme)) return false;
            if (!host.isEmpty() && hostRegex != null && !hostRegex.matcher(urlHost).find()) return false;
            return port == null || port.equals(urlPort);
        }

        /** httpx's URLPattern.priority: a port first, then longer hosts, then longer schemes; ties keep their order. */
        int compare(UrlPattern other) {
            int mine = port != null ? 0 : 1;
            int theirs = other.port != null ? 0 : 1;
            if (mine != theirs) return Integer.compare(mine, theirs);
            if (host.length() != other.host.length()) return Integer.compare(other.host.length(), host.length());
            if (scheme.length() != other.scheme.length()) return Integer.compare(other.scheme.length(), scheme.length());
            return Integer.compare(order, other.order);
        }
    }

    private static Integer defaultPort(String scheme) {
        return switch (scheme) {
            case "http" -> 80;
            case "https" -> 443;
            default -> null;
        };
    }

    /** The proxy the environment names for `url`, or null to connect directly. A proxy URL that cannot be used is an
     * InvalidRequest naming it (its password masked), as httpx refuses one when the client is made. */
    static Proxy forUrl(ServerUrl url, Map<String, String> environment) {
        Map<String, String> proxies = fromEnvironment(environment);
        Map<String, Proxy> mounts = new LinkedHashMap<>();
        for (String scheme : List.of("http", "https", "all")) {
            String value = proxies.get(scheme);
            if (value != null && !value.isEmpty()) mounts.put(scheme + "://", proxyOf(value.contains("://") ? value : "http://" + value));
        }
        String no = proxies.getOrDefault("no", "");
        for (String entry : no.split(",", -1)) {
            String hostname = entry.strip();
            if (hostname.equals("*")) return null;
            if (hostname.isEmpty()) continue;
            if (hostname.contains("://")) mounts.put(hostname, null);
            else if (isIpv4(hostname.split("/", -1)[0])) mounts.put("all://" + hostname, null);
            else if (isIpv6(hostname.split("/", -1)[0])) mounts.put("all://[" + hostname + "]", null);
            else if (hostname.toLowerCase(Locale.ROOT).equals("localhost")) mounts.put("all://" + hostname, null);
            else mounts.put("all://*" + hostname, null);
        }
        List<UrlPattern> patterns = new ArrayList<>();
        int order = 0;
        for (Map.Entry<String, Proxy> mount : mounts.entrySet()) patterns.add(UrlPattern.of(mount.getKey(), mount.getValue(), order++));
        patterns.sort(UrlPattern::compare);
        String scheme = url.tls() ? "https" : "http";
        Integer port = url.port() == (url.tls() ? 443 : 80) ? null : url.port();
        String host = url.host().toLowerCase(Locale.ROOT);
        for (UrlPattern pattern : patterns) if (pattern.matches(scheme, host, port)) return pattern.proxy();
        return null;
    }

    private static String masked(String url) {
        int scheme = url.indexOf("://");
        int start = scheme < 0 ? 0 : scheme + 3;
        int end = url.indexOf('/', start);
        String authority = url.substring(start, end < 0 ? url.length() : end);
        int at = authority.lastIndexOf('@');
        if (at < 0) return url;
        String user = authority.substring(0, at);
        int colon = user.indexOf(':');
        String shown = colon < 0 ? user : user.substring(0, colon) + ":[secure]";
        return url.substring(0, start) + shown + url.substring(start + at);
    }

    private static JeffError.InvalidRequest refused(String url, String problem) {
        return new JeffError.InvalidRequest("The proxy URL " + PyFormat.str(masked(url)) + " from the environment " + problem);
    }

    /** A proxy URL as httpx's Proxy reads it: http or https (socks5 needs a package the Python SDK does not require,
     * and is not supported here), the user name and password (percent-decoded) as Basic credentials. */
    static Proxy proxyOf(String url) {
        int colon = url.indexOf("://");
        String scheme = url.substring(0, colon).toLowerCase(Locale.ROOT);
        if (scheme.equals("socks5") || scheme.equals("socks5h")) throw refused(url, "is a SOCKS proxy, which this SDK does not support");
        if (!scheme.equals("http") && !scheme.equals("https")) throw refused(url, "has an unknown scheme; use http:// or https://");
        String rest = url.substring(colon + 3);
        int end = rest.length();
        for (char stop : new char[] {'/', '?', '#'}) if (rest.indexOf(stop) >= 0) end = Math.min(end, rest.indexOf(stop));
        String authority = rest.substring(0, end);
        String authorization = null;
        int at = authority.lastIndexOf('@');
        if (at >= 0) {
            String userInfo = authority.substring(0, at);
            authority = authority.substring(at + 1);
            int split = userInfo.indexOf(':');
            String user = percentDecoded(split < 0 ? userInfo : userInfo.substring(0, split));
            String password = split < 0 ? "" : percentDecoded(userInfo.substring(split + 1));
            if (!user.isEmpty() || !password.isEmpty()) {
                authorization = "Basic " + Base64.getEncoder().encodeToString((user + ":" + password).getBytes(StandardCharsets.UTF_8));
            }
        }
        String host;
        String port = null;
        if (authority.startsWith("[")) {
            int close = authority.indexOf(']');
            if (close < 0) throw refused(url, "is not a URL: its IPv6 host has no closing ']'");
            host = authority.substring(1, close);
            if (authority.length() > close + 1) {
                if (authority.charAt(close + 1) != ':') throw refused(url, "is not a URL: text follows its IPv6 host");
                port = authority.substring(close + 2);
            }
        } else {
            int portColon = authority.lastIndexOf(':');
            host = portColon < 0 ? authority : authority.substring(0, portColon);
            if (portColon >= 0) port = authority.substring(portColon + 1);
        }
        if (host.isEmpty()) throw refused(url, "has no host");
        int number = scheme.equals("https") ? 443 : 80;
        if (port != null && !port.isEmpty()) {
            if (port.length() > 5 || !port.chars().allMatch(character -> character >= '0' && character <= '9')
                || Integer.parseInt(port) > 65535) {
                throw refused(url, "has an invalid port " + PyFormat.str(port));
            }
            number = Integer.parseInt(port);
        }
        return new Proxy(scheme, host.toLowerCase(Locale.ROOT), number, authorization);
    }

    /** urllib.request.proxy_bypass_environment for "host:port": NO_PROXY "*" bypasses every host; otherwise each
     * entry, without its leading dots and in lower case, matches the host itself, "host:port", or any host (or
     * "host:port") ending in "." + entry. */
    static boolean bypassedForChannel(String host, int port, Map<String, String> proxies) {
        String no = proxies.get("no");
        if (no == null) return false;
        if (no.equals("*")) return true;
        String hostPort = (host + ":" + port).toLowerCase(Locale.ROOT);
        String hostOnly = host.toLowerCase(Locale.ROOT);
        for (String entry : no.split(",", -1)) {
            String name = entry.strip();
            if (name.isEmpty()) continue;
            name = name.replaceFirst("^\\.+", "").toLowerCase(Locale.ROOT);
            if (hostOnly.equals(name) || hostPort.equals(name)) return true;
            if (hostOnly.endsWith("." + name) || hostPort.endsWith("." + name)) return true;
        }
        return false;
    }

    /** The proxy the channel's WebSocket to host:port goes through, chosen as the Python SDK's websockets (proxy=True)
     * chooses it, or null to connect directly: NO_PROXY as urllib's proxy_bypass_environment reads it; then for wss://
     * the first of WSS_PROXY, SOCKS_PROXY, HTTPS_PROXY, for ws:// the first of WS_PROXY, SOCKS_PROXY, HTTPS_PROXY,
     * HTTP_PROXY (ALL_PROXY is not read; a SOCKS_PROXY of http://... means socks5h://...). The proxy is checked as
     * websockets' parse_proxy checks it; a SOCKS proxy is refused by name. Both ws:// and wss:// go through a CONNECT
     * tunnel. */
    static Proxy forChannel(String host, int port, boolean secure, Map<String, String> environment) {
        Map<String, String> proxies = fromEnvironment(environment);
        if (bypassedForChannel(host, port, proxies)) return null;
        for (String scheme : secure ? List.of("wss", "socks", "https") : List.of("ws", "socks", "https", "http")) {
            String value = proxies.get(scheme);
            if (value == null) continue;
            if (scheme.equals("socks") && value.startsWith("http://")) value = "socks5h://" + value.substring(7);
            return channelProxyOf(value);
        }
        return null;
    }

    private static JeffError.InvalidRequest refusedForChannel(String url, String problem) {
        return new JeffError.InvalidRequest("The proxy URL " + PyFormat.str(masked(url)) + " from the environment cannot be used "
            + "for the channel: " + problem);
    }

    /** websockets' parse_proxy: scheme http or https (SOCKS refused: the Python SDK does not install python-socks, so it
     * cannot use one either), a host, a path of "" or "/", no query, no fragment, and a user name only with a
     * password (percent-decoded, sent as Proxy-Authorization: Basic). */
    static Proxy channelProxyOf(String url) {
        int colon = url.indexOf(':');
        String scheme = colon > 0 && url.substring(0, colon).matches("[a-zA-Z][a-zA-Z0-9+.-]*")
            ? url.substring(0, colon).toLowerCase(Locale.ROOT) : "";
        if (List.of("socks5h", "socks5", "socks4a", "socks4").contains(scheme)) {
            throw refusedForChannel(url, "it is a SOCKS proxy, which this SDK does not support; use an HTTP proxy, or add the "
                + "server's host to NO_PROXY");
        }
        if (!scheme.equals("http") && !scheme.equals("https")) throw refusedForChannel(url, "scheme " + scheme + " isn't supported");
        if (!url.startsWith("//", colon + 1)) throw refusedForChannel(url, "hostname isn't provided");
        String rest = url.substring(colon + 3);
        int end = rest.length();
        for (char stop : new char[] {'/', '?', '#'}) if (rest.indexOf(stop) >= 0) end = Math.min(end, rest.indexOf(stop));
        String after = rest.substring(end);
        int fragment = after.indexOf('#');
        String beforeFragment = fragment < 0 ? after : after.substring(0, fragment);
        int query = beforeFragment.indexOf('?');
        String path = query < 0 ? beforeFragment : beforeFragment.substring(0, query);
        if (!path.isEmpty() && !path.equals("/")) throw refusedForChannel(url, "path is meaningless");
        if (query >= 0 && query + 1 < beforeFragment.length()) throw refusedForChannel(url, "query is meaningless");
        if (fragment >= 0 && fragment + 1 < after.length()) throw refusedForChannel(url, "fragment is meaningless");
        String authority = rest.substring(0, end);
        int at = authority.lastIndexOf('@');
        if (at >= 0 && !authority.substring(0, at).contains(":")) throw refusedForChannel(url, "username provided without password");
        String hostAndPort = authority.substring(at + 1);
        if (hostAndPort.isEmpty() || hostAndPort.startsWith(":")) throw refusedForChannel(url, "hostname isn't provided");
        Proxy parsed;
        try {
            parsed = proxyOf(scheme + "://" + authority);
        } catch (JeffError.InvalidRequest error) {
            String message = error.getMessage();
            throw refusedForChannel(url, message.substring(message.indexOf(" from the environment ") + " from the environment ".length()));
        }
        if (at >= 0 && parsed.authorization() == null) {
            // ":@host" names an empty user name and password, which websockets still sends
            parsed = new Proxy(parsed.scheme(), parsed.host(), parsed.port(),
                "Basic " + Base64.getEncoder().encodeToString(":".getBytes(StandardCharsets.UTF_8)));
        }
        return parsed;
    }

    /** urllib.parse.unquote: %XX sequences (ASCII hex digits only) as UTF-8 bytes, bad ones kept as written; bytes
     * that are not UTF-8 become U+FFFD, as Python's errors='replace'. */
    private static String percentDecoded(String text) {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        int index = 0;
        while (index < text.length()) {
            int code = text.codePointAt(index);
            if (code == '%' && index + 2 < text.length() && asciiHex(text.charAt(index + 1)) && asciiHex(text.charAt(index + 2))) {
                bytes.write(Integer.parseInt(text.substring(index + 1, index + 3), 16));
                index += 3;
            } else {
                bytes.writeBytes(new String(Character.toChars(code)).getBytes(StandardCharsets.UTF_8));
                index += Character.charCount(code);
            }
        }
        return new String(bytes.toByteArray(), StandardCharsets.UTF_8);
    }

    private static boolean asciiHex(char character) {
        return character >= '0' && character <= '9' || character >= 'a' && character <= 'f' || character >= 'A' && character <= 'F';
    }

    private static final Pattern OCTET = Pattern.compile("25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]");

    /** Python's ipaddress.IPv4Address accepts the text: four decimal octets, no leading zeros. */
    static boolean isIpv4(String text) {
        String[] parts = text.split("\\.", -1);
        if (parts.length != 4) return false;
        for (String part : parts) if (!OCTET.matcher(part).matches()) return false;
        return true;
    }

    /** Python's ipaddress.IPv6Address accepts the text: up to eight groups of 1 to 4 hex digits, one "::" at most, an
     * IPv4 address as the last two groups, an optional %scope. */
    static boolean isIpv6(String text) {
        int percent = text.indexOf('%');
        if (percent >= 0) {
            if (percent == text.length() - 1) return false;
            text = text.substring(0, percent);
        }
        if (!text.contains(":")) return false;
        int doubled = text.indexOf("::");
        if (doubled >= 0 && text.indexOf("::", doubled + 1) >= 0) return false;
        int groups = 0;
        String[] halves = doubled >= 0 ? new String[] {text.substring(0, doubled), text.substring(doubled + 2)} : new String[] {text};
        for (int half = 0; half < halves.length; half++) {
            if (halves[half].isEmpty()) continue;
            String[] parts = halves[half].split(":", -1);
            for (int index = 0; index < parts.length; index++) {
                String part = parts[index];
                boolean last = half == halves.length - 1 && index == parts.length - 1;
                if (last && part.contains(".")) {
                    if (!isIpv4(part)) return false;
                    groups += 2;
                } else if (part.matches("[0-9a-fA-F]{1,4}")) {
                    groups++;
                } else {
                    return false;
                }
            }
        }
        return doubled >= 0 ? groups <= 7 : groups == 8;
    }
}
