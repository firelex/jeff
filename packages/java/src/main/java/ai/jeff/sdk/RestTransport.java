package ai.jeff.sdk;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.regex.Pattern;

/** The client's HTTP/1.1 connection to a Jeff server (port of jeff/remote.py's RestTransport), for sending
 * {@code POST /v1/decide} calls yourself. Most callers want {@link Jeff#remote(String, TokenSource, RemoteOptions)}
 * instead, which reads the server's functions and checks them against the catalogue. Every non-2xx answer is thrown as
 * the JeffError its body names, with the answer's {@code x-request-id} as the error's request id (null when it carried
 * none).
 *
 * <p>{@code timeoutMs} bounds each phase of a request on its own, as httpx's Timeout(timeout_s) does in the Python SDK:
 * waiting for a place among the connections (httpx's pool timeout; places are handed out in arrival order),
 * connecting (TCP and TLS), waiting for the answer's headers (from the request's first byte written, so sending it
 * counts), and each wait for more of the answer's body. A slow connect followed by a fast answer never adds up to a
 * timeout. Running out while connecting is BackendUnavailable "Cannot reach"; in any other phase it is
 * DeadlineExceeded. At most 100 connections are open, at most 20 of them idle, none reused after 5 s idle (httpx's
 * default Limits). A request is never sent twice, a redirect is never followed (a 3xx is an answer like any other),
 * and close() ends every request in flight with the closed error. The proxy variables of the environment
 * ({@code HTTPS_PROXY}, {@code HTTP_PROXY}, {@code ALL_PROXY}, {@code NO_PROXY}) are honoured as httpx honours them. */
public final class RestTransport implements AutoCloseable {
    static final String REQUEST_ID_HEADER = "x-request-id";
    /** Characters of a non-Jeff error body quoted in the EngineError. */
    private static final int BODY_EXCERPT = 300;

    private final ServerUrl server;
    private final TokenSource token;
    private final long timeoutMs;
    private final Proxies.Proxy proxy;
    private final Http1Client http;

    /** A transport to the server at {@code url}. Nothing is sent until the first request.
     *
     * @param url the server's {@code http://} or {@code https://} address
     * @param token the bearer token: {@link TokenSource#of}, {@link TokenSource#from} or {@link TokenSource#none}
     * @param timeoutMs the limit of each phase of a request, in milliseconds
     * @throws JeffError.InvalidRequest when the timeout is not above 0 or the address is not one the SDK can call */
    public RestTransport(String url, TokenSource token, long timeoutMs) {
        this(url, token, timeoutMs, HttpOptions.standard());
    }

    /** With the pool, the network, the TLS sockets and the environment given (tests). */
    RestTransport(String url, TokenSource token, long timeoutMs, HttpOptions options) {
        Objects.requireNonNull(url, "url");
        this.server = ServerUrl.parse(url);
        this.token = Objects.requireNonNull(token, "token: use TokenSource.none() for a server with no credentials");
        Jeff.checkTimeoutMs(timeoutMs);
        this.timeoutMs = timeoutMs;
        this.proxy = Proxies.forUrl(server, options.environment());
        this.http = new Http1Client(server, proxy, timeoutMs, options.pool(), options.opener(), options.tls());
    }

    /** The server's address as the transport calls it (no trailing slash).
     *
     * @return the address */
    public String url() {
        return server.url();
    }

    int inFlightCount() {
        return http.inFlightCount();
    }

    ConnectionPool pool() {
        return http.pool();
    }

    private static JeffError.InvalidRequest closedError(String url) {
        return new JeffError.InvalidRequest("This remote client of " + url + " is closed; connect again with Jeff.remote");
    }

    private static Throwable root(Throwable error) {
        Throwable cause = error;
        while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
        return cause;
    }

    /** The JeffError for a request that got no answer, as jeff/remote.py maps httpx's exceptions: no connection in time,
     * or a failure to open one, is BackendUnavailable "Cannot reach ..." (httpx ConnectTimeout, ConnectError); no place
     * in the pool, no headers or no further body in time is DeadlineExceeded (PoolTimeout, WriteTimeout, ReadTimeout);
     * any other failure once connected is BackendUnavailable "The connection ... failed during ..." (httpx's other
     * TransportErrors, such as RemoteProtocolError). */
    static JeffError noAnswerError(Http1Client.NoAnswer error, String url, String method, String path, long timeoutMs) {
        return switch (error.kind) {
            case CLOSED -> closedError(url);
            case CONNECT_TIMEOUT -> new JeffError.BackendUnavailable("Cannot reach the Jeff server at " + url + ": no connection within "
                + timeoutMs + " ms");
            case LOOKUP_TIMEOUT -> new JeffError.BackendUnavailable("Cannot reach the Jeff server at " + url + ": the address of "
                + ((HttpConnection.LookupTimeout) error.getCause()).host + " was not found within " + timeoutMs + " ms");
            case CONNECT_FAILED -> new JeffError.BackendUnavailable("Cannot reach the Jeff server at " + url + ": "
                + JeffError.describe(root(error.getCause())));
            case POOL_TIMEOUT, READ_TIMEOUT -> new JeffError.DeadlineExceeded("The Jeff server at " + url + " did not answer " + method
                + " " + path + " within " + timeoutMs + " ms; the server may still have run the request");
            case BROKEN -> new JeffError.BackendUnavailable("The connection to the Jeff server at " + url + " failed during " + method
                + " " + path + ": " + JeffError.describe(error.getCause()));
            case INTERRUPTED -> new JeffError.BackendUnavailable("Interrupted while waiting for the Jeff server at " + url
                + " to answer " + method + " " + path);
        };
    }

    private static String excerpt(String text) {
        int[] codes = text.codePoints().limit(BODY_EXCERPT).toArray();
        return new String(codes, 0, codes.length);
    }

    /** The request's bytes: its head (the proxy's credentials when forwarded through one, the headers httpx sends by
     * default, then the token and the body's) and body. */
    private byte[] requestBytes(String method, String path, String bearer, byte[] body) {
        StringBuilder head = new StringBuilder();
        // through a proxy, an http:// request names its whole URL (an https:// one goes through a tunnel as it is)
        String target = proxy != null && !server.tls() ? "http://" + server.hostHeader() + server.basePath() + path : server.basePath() + path;
        head.append(method).append(' ').append(target).append(" HTTP/1.1\r\n");
        if (proxy != null && !server.tls() && proxy.authorization() != null) {
            head.append("Proxy-Authorization: ").append(proxy.authorization()).append("\r\n");
        }
        head.append("Host: ").append(server.hostHeader()).append("\r\n");
        head.append("Accept: */*\r\n");
        head.append("Accept-Encoding: ").append(HttpBody.ACCEPT_ENCODING).append("\r\n");
        head.append("Connection: keep-alive\r\n");
        head.append("User-Agent: jeff-sdk-java\r\n");
        if (bearer != null) head.append("Authorization: Bearer ").append(bearer).append("\r\n");
        if (body != null) {
            head.append("Content-Length: ").append(body.length).append("\r\n");
            head.append("Content-Type: application/json\r\n");
        }
        head.append("\r\n");
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        bytes.writeBytes(head.toString().getBytes(StandardCharsets.ISO_8859_1));
        if (body != null) bytes.writeBytes(body);
        return bytes.toByteArray();
    }

    /** An answer body past HttpConnection.MAX_BODY, as sent or once decoded (`what`): as the C# SDK, the client is
     * closed (a server that sends one is not trusted with later requests, as the channel ends on a message past its
     * limit), and the error names the limit. */
    private JeffError.BackendUnavailable tooLarge(String method, String path, String what) {
        close();
        return new JeffError.BackendUnavailable("The Jeff server at " + url() + " answered " + method + " " + path + " with " + what
            + " " + HttpConnection.MAX_BODY + " bytes, the most this client reads; the client is closed, connect again with Jeff.remote");
    }

    /** One request; the answer's JSON object, checked against `schema`. Every error thrown after the server answered
     * carries the answer's x-request-id. */
    private Map<String, Object> request(String method, String path, Map<String, Object> body, String schema) {
        if (http.closed()) throw closedError(url());
        String bearer = readToken(token, url());
        byte[] payload = body == null ? null : Json.write(body).getBytes(StandardCharsets.UTF_8);
        HttpConnection.Answer raw;
        try {
            raw = http.send(requestBytes(method, path, bearer, payload));
        } catch (Http1Client.NoAnswer error) {
            if (error.kind == Http1Client.NoAnswer.Kind.BROKEN && error.getCause() instanceof HttpConnection.BodyTooLarge) {
                throw tooLarge(method, path, "a body of more than");
            }
            throw noAnswerError(error, url(), method, path, timeoutMs);
        }
        List<String[]> headers = HttpBody.headers(raw.headers());
        byte[] decoded;
        try {
            decoded = HttpBody.decoded(raw.body(), headers);
        } catch (HttpBody.TooLarge tooLarge) {
            throw tooLarge(method, path, "a body that decodes (Content-Encoding) to more than");
        } catch (HttpBody.DecodingError error) {
            // httpx decodes the body while reading it, so this is jeff/remote.py's RequestError branch: no answer yet
            throw new JeffError.EngineError("The answer of the Jeff server at " + url() + " to " + method + " " + path
                + " could not be read: DecodingError: " + error.getMessage());
        }
        try {
            return answer(method, path, raw.status(), decoded, headers, schema);
        } catch (JeffError error) {
            error.setRequestId(HttpBody.header(headers, REQUEST_ID_HEADER));
            throw error;
        }
    }

    private Map<String, Object> answer(String method, String path, int status, byte[] body, List<String[]> headers, String schema) {
        if (status < 200 || status > 299) throw error(method, path, status, body, headers);
        Object answer;
        try {
            answer = HttpBody.parseJson(body);
        } catch (HttpBody.NotJson notJson) {
            throw new JeffError.EngineError("The Jeff server at " + url() + " answered " + method + " " + path + " with status " + status
                + " and a body that is not JSON: " + excerpt(HttpBody.text(body, headers)));
        }
        if (!(answer instanceof Map<?, ?>)) {
            throw new JeffError.EngineError("The Jeff server at " + url() + " answered " + method + " " + path + " with JSON that is "
                + "not an object: " + excerpt(HttpBody.text(body, headers)));
        }
        if (!Contract.isValid(schema, answer)) {
            throw new JeffError.ContractError("The answer of " + url() + " to " + method + " " + path + " does not match "
                + "contract/schemas/" + schema + ": " + Contract.firstProblem(schema, answer));
        }
        return Values.map(answer);
    }

    /** The JeffError a non-2xx answer names. As the Python SDK does, a decision its details carry (LowConfidence) is
     * checked against the decision schema before the error is built, whatever the error's type. */
    private JeffError error(String method, String path, int status, byte[] text, List<String[]> headers) {
        Object body;
        try {
            body = HttpBody.parseJson(text);
        } catch (HttpBody.NotJson notJson) {
            // a body that is not JSON is exactly what the EngineError below reports, quoting it
            body = null;
        }
        if (body instanceof Map<?, ?> fields && fields.get("error") instanceof Map<?, ?> wire
            && wire.get("type") instanceof String type && wire.get("message") instanceof String) {
            Object decision = wire.get("details") instanceof Map<?, ?> details ? details.get("decision") : null;
            if (decision != null && !Contract.isValid("decision", decision)) {
                return new JeffError.ContractError("The " + type + " error the Jeff server at " + url() + " answered " + method
                    + " " + path + " with carries a decision that does not match contract/schemas/decision: "
                    + Contract.firstProblem("decision", decision));
            }
            return JeffError.fromWire(Values.map(wire));
        }
        return new JeffError.EngineError("The Jeff server at " + url() + " answered " + method + " " + path + " with status " + status
            + " and a body that is not a Jeff error: " + excerpt(HttpBody.text(text, headers)));
    }

    private Object decideAnswer(Map<String, Object> body, String key) {
        Map<String, Object> answer = request("POST", "/v1/decide", body, "decide_response");
        if (!answer.containsKey(key)) {
            throw new JeffError.ContractError("The answer of " + url() + " to POST /v1/decide has "
                + PyFormat.repr(PyFormat.sorted(answer.keySet())) + ", not " + PyFormat.str(key));
        }
        return answer.get(key);
    }

    /** Sends {@code calls} in one {@code POST /v1/decide} and returns the server's decisions, in order, checked
     * against contract/schemas/decide_response. Read one with {@link Decision#fromJson(Map)}.
     *
     * @param calls the calls in their wire form (contract/schemas/decide_request): each a map with
     *     {@code function}, {@code input} and the optional fields
     * @param policy the low-confidence policy in its wire form, such as {@code "return"}
     * @return one decision per call, as JSON maps
     * @throws JeffError the error the server answered with (such as UnknownFunction or Unauthorised), with its
     *     request id; BackendUnavailable when the server cannot be reached; DeadlineExceeded when a phase takes longer
     *     than the timeout; ContractError or EngineError when the answer is not one the contract allows; InvalidRequest
     *     when the transport is closed */
    public List<Map<String, Object>> decideCalls(List<Map<String, Object>> calls, Object policy) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("calls", calls);
        body.put("on_low_confidence", policy);
        List<Map<String, Object>> decisions = new ArrayList<>();
        for (Object item : Values.list(decideAnswer(body, "decisions"))) decisions.add(Values.map(item));
        if (decisions.size() != calls.size()) {
            throw new JeffError.ContractError(url() + " answered " + decisions.size() + " decisions for " + calls.size() + " calls");
        }
        return decisions;
    }

    Map<String, Object> dispatch(Map<String, Object> body) {
        return Values.map(decideAnswer(body, "decision"));
    }

    Map<String, Object> graph(Map<String, Object> body) {
        return Values.map(decideAnswer(body, "graph_result"));
    }

    Map<String, Object> functions() {
        return request("GET", "/v1/functions", null, "functions_response");
    }

    /** Ends every request in flight (each fails with the closed error) and closes every connection. Calling it again
     * does nothing. */
    @Override
    public void close() {
        http.close();
    }

    /** This transport as RemoteJeff and ChannelTransport use it. */
    Transport asTransport() {
        RestTransport rest = this;
        return new Transport() {
            @Override
            public List<Map<String, Object>> decideCalls(List<Map<String, Object>> calls, Object policy) {
                return rest.decideCalls(calls, policy);
            }

            @Override
            public Map<String, Object> dispatch(Map<String, Object> body) {
                return rest.dispatch(body);
            }

            @Override
            public Map<String, Object> graph(Map<String, Object> body) {
                return rest.graph(body);
            }

            @Override
            public Map<String, Object> functions() {
                return rest.functions();
            }

            @Override
            public void close() {
                rest.close();
            }
        };
    }

    /** The current token (a function is called once per use), or null for none. Unauthorised when it is not a
     * non-empty string an HTTP header can carry; no message shows the token. */
    static String readToken(TokenSource source, String url) {
        if (source instanceof TokenSource.None) return null;
        boolean fromFunction = source instanceof TokenSource.FromFunction;
        String token = fromFunction ? ((TokenSource.FromFunction) source).supplier.get() : ((TokenSource.Fixed) source).token;
        String what = fromFunction ? "the token function returned" : "got";
        if (token == null || token.isEmpty()) {
            throw new JeffError.Unauthorised("The token for " + url + " must be a non-empty string; " + what + " "
                + (token == null ? "null" : "an empty string"));
        }
        if (!Pattern.matches("[\\x20-\\x7e]+", token)) {
            throw new JeffError.Unauthorised("The token for " + url + " must be printable ASCII, as an HTTP header carries it; "
                + what + " a string with other characters (not shown)");
        }
        return token;
    }
}
