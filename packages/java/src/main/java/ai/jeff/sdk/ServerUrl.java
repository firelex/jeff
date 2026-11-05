package ai.jeff.sdk;

import java.net.URI;
import java.net.URISyntaxException;

/** A Jeff server's URL, checked before anything is sent: http:// or https://, a host, a port (when written) that is a
 * number from 0 to 65535, no user name or password, no query and no fragment. A part that is wrong is refused by name,
 * never given a default. `url` is the URL without trailing slashes; `basePath` is its path (no trailing slash), which
 * every request path follows, as httpx's base_url has it. */
record ServerUrl(String url, boolean tls, String host, int port, String hostHeader, String basePath) {
    static ServerUrl parse(String url) {
        boolean tls;
        if (url.startsWith("https://")) tls = true;
        else if (url.startsWith("http://")) tls = false;
        else throw new JeffError.InvalidRequest("The server URL " + PyFormat.str(url) + " must start with http:// or https://");
        String trimmed = url.replaceAll("/+$", "");
        String scheme = tls ? "https://" : "http://";
        String rest = trimmed.length() > scheme.length() ? trimmed.substring(scheme.length()) : "";
        if (rest.contains("#")) throw invalid(url, "must not have a fragment (the part after '#')");
        if (rest.contains("?")) throw invalid(url, "must not have a query (the part after '?')");
        int slash = rest.indexOf('/');
        String authority = slash < 0 ? rest : rest.substring(0, slash);
        String path = slash < 0 ? "" : rest.substring(slash);
        if (authority.contains("@")) throw invalid(url, "must not carry a user name or password; pass the token instead");
        String host;
        String port;
        if (authority.startsWith("[")) {
            int close = authority.indexOf(']');
            if (close < 0) throw invalid(url, "is not a URL: its IPv6 host has no closing ']'");
            host = authority.substring(1, close);
            String after = authority.substring(close + 1);
            if (!after.isEmpty() && after.charAt(0) != ':') throw invalid(url, "is not a URL: text follows its IPv6 host");
            port = after.isEmpty() ? null : after.substring(1);
        } else {
            int colon = authority.indexOf(':');
            host = colon < 0 ? authority : authority.substring(0, colon);
            port = colon < 0 ? null : authority.substring(colon + 1);
        }
        if (host.isEmpty()) throw invalid(url, "has no host");
        int defaultPort = tls ? 443 : 80;
        int number = defaultPort;
        // "" (as in http://h:/) is the scheme's default port (RFC 3986); anything else written must be a port number
        if (port != null && !port.isEmpty()) {
            if (port.length() > 5 || !port.chars().allMatch(character -> character >= '0' && character <= '9')
                || Integer.parseInt(port) > 65535) {
                throw invalid(url, "has an invalid port " + PyFormat.str(port));
            }
            number = Integer.parseInt(port);
        }
        try {
            new URI(trimmed);
        } catch (URISyntaxException error) {
            throw invalid(url, "is not a URL: " + error.getMessage());
        }
        String shownHost = authority.startsWith("[") ? "[" + host + "]" : host;
        // as httpx writes the Host header: the port only when it is not the scheme's default
        String hostHeader = number == defaultPort ? shownHost : shownHost + ":" + number;
        return new ServerUrl(trimmed, tls, host, number, hostHeader, path);
    }

    private static JeffError.InvalidRequest invalid(String url, String problem) {
        return new JeffError.InvalidRequest("The server URL " + PyFormat.str(url) + " " + problem);
    }
}
