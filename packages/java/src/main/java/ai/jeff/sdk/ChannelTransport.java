package ai.jeff.sdk;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;

/** The Transport over the server's low-latency channel (contract/socket.md, "The channel"; port of
 * jeff/channel_client.py), plus the handle operations bind, call, callMany and release.
 *
 * <p>One WebSocket (WebSocketConnection: this SDK's own client, compression off) to &lt;url&gt;/v1/channel, ws:// for
 * http://, wss:// for https://. Opening sends hello and, when there is a token, auth; every request is then one binary
 * message holding exactly one frame with an id, and the reader thread hands each reply to the request waiting for that
 * id. GET /v1/functions goes over HTTP (a RestTransport with the same URL, token and timeout): the channel has no
 * operation for it.
 *
 * <p>Writing: one writer thread owns the sending side. Requests reach it through a queue, in order; it writes each
 * message whole, unfragmented, in pieces of at most WebSocketConnection.SEND_CHUNK bytes. A caller that stops waiting
 * (its time ran out, or it was interrupted) never leaves half a message on the socket: a message not yet started is
 * never sent, one started is finished. close() and every other end of the channel abort the socket, which ends a write
 * the server stopped taking.
 *
 * <p>Timing, as the Python SDK times it: timeoutMs bounds opening the WebSocket (the lookup, TCP, the proxy's tunnel,
 * TLS and the upgrade together), the hello, and the closing handshake. A request has three phases, each bounded on its
 * own: its turn to be written (at most timeoutMs behind earlier messages, else DeadlineExceeded, and it is never sent);
 * its upload (as long as it takes on a slow but working link, watched for stalls: when the socket accepts no piece of
 * a message for timeoutMs, the channel ends with BackendUnavailable naming the stall); and the wait for its reply (at
 * most timeoutMs from the moment its message was sent, else DeadlineExceeded, and its late reply is dropped).
 *
 * <p>Keepalive, as websockets' (ping_interval=20, ping_timeout=20): a ping every KEEPALIVE_MS, and a pong due within
 * KEEPALIVE_MS of it being sent, else the channel ends with BackendUnavailable.
 *
 * <p>Proxies: the WebSocket goes through the proxy the environment names for it, chosen as websockets (proxy=True)
 * chooses it (Proxies.forChannel), in a CONNECT tunnel; GET /v1/functions through the one httpx would choose.
 *
 * <p>Token: a token function is called for the token on the WebSocket handshake (as Authorization: Bearer, for a proxy
 * in front of the server), for the token sent with auth, in each refresh window, and for each GET /v1/functions. A
 * function token that is a JWT is refreshed from REFRESH_BEFORE_S before its exp, retried every REFRESH_RETRY_S, and
 * the channel closes with Unauthorised carrying every failure as a note if exp passes first. No token is ever logged
 * or put in a message.
 *
 * <p>An error for a failure the server sent carries no request id (there is no HTTP response) but the server
 * connection's id as details.channel_id. When the channel closes (a failure with id 0, the socket closing, or
 * close()), every request waiting fails with its own copy of that error and every later request throws the same
 * error. There is no automatic reconnect and no retry: connect again with Jeff.remote. */
final class ChannelTransport implements Transport {
    /** How long before a token's exp the refresher starts asking the token function for a fresh token, and how often
     * it asks again while the token it returns does not expire later (or the function or the refresh fails). */
    public static final long REFRESH_BEFORE_S = 60;
    public static final long REFRESH_RETRY_S = 5;
    /** websockets' ping_interval and ping_timeout (20 s each). */
    static final long KEEPALIVE_MS = 20_000;
    /** The largest WebSocket message read: one frame of the socket protocol (4 bytes and at most 64 MiB), as the
     * Python SDK's max_size. */
    static final long MAX_MESSAGE = Protocol.HEADER_SIZE + (long) Protocol.MAX_FRAME;
    private static final System.Logger LOG = System.getLogger("ai.jeff.sdk.channel");

    public record Handle(long handle, String version) {}

    /** A message for the writer. A request's message can be abandoned while it waits (never written then); a started
     * one is written whole. `started` completes when the writer takes it, `sent` once the socket accepted all of it;
     * both fail with the channel's error when it cannot be sent. */
    private static final class Outgoing {
        static final int QUEUED = 0;
        static final int WRITING = 1;
        static final int ABANDONED = 2;

        final int opcode;
        final byte[] payload;
        final AtomicInteger state = new AtomicInteger(QUEUED);
        final CompletableFuture<Void> started = new CompletableFuture<>();
        final CompletableFuture<Void> sent = new CompletableFuture<>();

        Outgoing(int opcode, byte[] payload) {
            this.opcode = opcode;
            this.payload = payload;
        }

        boolean abandon() {
            return state.compareAndSet(QUEUED, ABANDONED);
        }

        void fail(JeffError error) {
            started.completeExceptionally(error);
            sent.completeExceptionally(error);
        }
    }

    private final RestTransport rest;
    private final String url;
    private final String wsUrl;
    private final TokenSource token;
    private final long timeoutMs;
    private final long keepaliveMs;
    private final WebSocketConnection socket;
    private final ReentrantLock lock = new ReentrantLock();
    private final Condition queued = lock.newCondition();
    private final ArrayDeque<Outgoing> outbox = new ArrayDeque<>();
    private final Map<Long, CompletableFuture<Map<String, Object>>> pending = new HashMap<>();
    /** Ids of releases sent for garbage-collected bound functions (no one waits for their replies). */
    private final Set<Long> unawaited = new HashSet<>();
    /** Ids of requests sent whose caller stopped waiting: their late replies are dropped. An id stays until its late
     * reply comes or the channel ends, so a server that never answers requests that timed out keeps one id (a Long) per
     * DeadlineExceeded here for the channel's life; jeff-server answers every request it read. */
    private final Set<Long> abandoned = new HashSet<>();
    private final ConcurrentLinkedQueue<Long> releases = new ConcurrentLinkedQueue<>();
    private final AtomicLong nextId = new AtomicLong(1);
    private final List<String> refreshFailures = new CopyOnWriteArrayList<>();
    /** Counted down once the channel closes: wakes the refresher and the keepalive. */
    private final CountDownLatch closing = new CountDownLatch(1);
    /** Completed once the reader has ended (the connection is over). */
    private final CompletableFuture<Void> wsClosed = new CompletableFuture<>();
    private final AtomicLong messages = new AtomicLong();
    /** System.nanoTime() when the message being written was started or last had a piece accepted. */
    private volatile long progressAt;
    private volatile Outgoing writing;
    private volatile CompletableFuture<Void> pong;
    private volatile byte[] pingPayload;
    private JeffError closedError;
    private CompletableFuture<Map<String, Object>> greeting;
    private boolean closeQueued;
    /** The close frame the server sent, if any: its code (1005 for none) and reason. */
    private volatile String receivedClose;

    private ChannelTransport(RestTransport rest, TokenSource token, long timeoutMs, long keepaliveMs, WebSocketConnection socket) {
        this.rest = rest;
        this.url = rest.url();
        this.wsUrl = channelUrl(rest.url());
        this.token = token;
        this.timeoutMs = timeoutMs;
        this.keepaliveMs = keepaliveMs;
        this.socket = socket;
    }

    /** The channel's WebSocket URL for a server URL: ws:// for http://, wss:// for https://, path /v1/channel. */
    public static String channelUrl(String serverUrl) {
        for (String[] scheme : new String[][] {{"http://", "ws://"}, {"https://", "wss://"}}) {
            if (serverUrl.startsWith(scheme[0])) {
                return scheme[1] + serverUrl.substring(scheme[0].length()).replaceAll("/+$", "") + Protocol.CHANNEL_PATH;
            }
        }
        throw new JeffError.InvalidRequest("The server URL " + PyFormat.str(serverUrl) + " must start with http:// or https://");
    }

    /** The exp of a JWT (header.payload.signature), read from its payload without verifying the signature (decoded as
     * Python's base64.urlsafe_b64decode and json.loads read it); null for a token that is not a JWT. A JWT whose
     * payload cannot be read, or whose exp is not a finite number, is Unauthorised. No message quotes the token. */
    public static Number tokenExpiry(String jwt) {
        String[] parts = jwt.split("\\.", -1);
        if (parts.length != 3) return null;
        byte[] decoded = PyBase64.urlSafeDecode(parts[1]);
        Object payload;
        try {
            if (decoded == null) throw new HttpBody.NotJson("not base64");
            payload = HttpBody.parseJson(decoded);
        } catch (HttpBody.NotJson unreadable) {
            throw new JeffError.Unauthorised("The token is shaped like a JWT, but its payload is not base64-encoded JSON");
        }
        Object expires = payload instanceof Map<?, ?> fields ? fields.get("exp") : null;
        boolean numeric = expires instanceof Long || expires instanceof java.math.BigInteger || expires instanceof Double;
        if (!numeric || !Double.isFinite(((Number) expires).doubleValue())) {
            throw new JeffError.Unauthorised("The token is a JWT without a numeric exp (got " + PyFormat.repr(expires) + "); the "
                + "server needs one");
        }
        return (Number) expires;
    }

    /** Opens the channel: the WebSocket, hello, and auth when there is a token; a function's JWT is then refreshed. */
    public static ChannelTransport open(String url, TokenSource token, long timeoutMs) {
        return open(url, token, timeoutMs, HttpOptions.standard(), KEEPALIVE_MS);
    }

    /** With the network, the TLS sockets and the environment given, and the keepalive interval (tests). */
    static ChannelTransport open(String url, TokenSource token, long timeoutMs, HttpOptions options, long keepaliveMs) {
        RestTransport rest = new RestTransport(url, token, timeoutMs, options); // also checks the URL, the token and the timeout
        ChannelTransport transport;
        try {
            WebSocketConnection socket = connect(rest.url(), token, timeoutMs, options);
            transport = new ChannelTransport(rest, token, timeoutMs, keepaliveMs, socket);
        } catch (RuntimeException | Error error) {
            rest.close();
            throw error;
        }
        try {
            transport.start();
            transport.handshake();
            Number expires = transport.authenticate();
            if (expires != null) Thread.ofVirtual().name("jeff-channel-refresh").start(() -> transport.refreshLoop(expires));
        } catch (RuntimeException | Error error) {
            try {
                transport.close();
            } catch (RuntimeException closing) {
                error.addSuppressed(closing);
            }
            throw error;
        }
        return transport;
    }

    public String url() {
        return url;
    }

    public String wsUrl() {
        return wsUrl;
    }

    private String currentToken() {
        return RestTransport.readToken(token, url);
    }

    /** Opens the WebSocket, at most timeoutMs for the whole of it. The token also goes on the handshake as
     * Authorization: Bearer: the Jeff server checks the auth message sent after hello, but a proxy in front of it may
     * open the WebSocket only for a request that carries credentials. */
    private static WebSocketConnection connect(String url, TokenSource token, long timeoutMs, HttpOptions options) {
        ServerUrl server = ServerUrl.parse(url);
        String wsUrl = channelUrl(url);
        Map<String, String> headers = new LinkedHashMap<>();
        if (!(token instanceof TokenSource.None)) headers.put("Authorization", "Bearer " + RestTransport.readToken(token, url));
        Proxies.Proxy proxy = Proxies.forChannel(server.host(), server.port(), server.tls(), options.environment());
        WebSocketConnection.Target target = new WebSocketConnection.Target(server.host(), server.port(), server.tls(),
            server.basePath() + Protocol.CHANNEL_PATH, headers, proxy, options.opener(), options.tls());
        try {
            return WebSocketConnection.open(target, timeoutMs, MAX_MESSAGE);
        } catch (WebSocketConnection.OpenFailure failure) {
            if (failure.timedOut) {
                throw new JeffError.BackendUnavailable("Cannot reach the Jeff server at " + url + ": opening " + wsUrl + " took more than "
                    + timeoutMs + " ms");
            }
            if (failure.status != 0) {
                throw new JeffError.EngineError("The Jeff server at " + url + " refused to open the channel " + wsUrl + ": HTTP status "
                    + failure.status);
            }
            JeffError.BackendUnavailable error = new JeffError.BackendUnavailable("Cannot reach the Jeff server at " + url + " (" + wsUrl
                + "): " + failure.getMessage());
            error.initCause(failure);
            throw error;
        }
    }

    private void start() {
        Thread.ofVirtual().name("jeff-channel-reader").start(this::readLoop);
        Thread.ofVirtual().name("jeff-channel-writer").start(this::writeLoop);
        if (keepaliveMs > 0) Thread.ofVirtual().name("jeff-channel-keepalive").start(this::keepalive);
    }

    private static Throwable unwrap(Throwable error) {
        Throwable cause = error;
        while (cause instanceof ExecutionException && cause.getCause() != null) cause = cause.getCause();
        return cause;
    }

    /** Sends hello and reads the server's: at most timeoutMs for its turn to be written, and timeoutMs for the reply
     * once it is sent. */
    private void handshake() {
        CompletableFuture<Map<String, Object>> hello = new CompletableFuture<>();
        Outgoing outgoing = new Outgoing(WebSocketConnection.BINARY, Protocol.encodeFrame(Protocol.hello(), "hello"));
        lock.lock();
        try {
            if (closedError != null) throw JeffError.copy(closedError);
            greeting = hello;
            enqueue(outgoing);
        } finally {
            lock.unlock();
        }
        Map<String, Object> message;
        try {
            awaitSent(outgoing, "hello");
            message = hello.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException error) {
            throw new JeffError.BackendUnavailable("No hello from the channel " + wsUrl + " within " + timeoutMs + " ms");
        } catch (ExecutionException error) {
            throw rethrown(error);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new JeffError.BackendUnavailable("Interrupted while waiting for the hello of " + wsUrl);
        } finally {
            lock.lock();
            try {
                if (greeting == hello) greeting = null;
            } finally {
                lock.unlock();
            }
        }
        if (Boolean.FALSE.equals(message.get("ok"))) throw wireError(Values.map(message.get("error")));
        Protocol.checkHello(message);
        Protocol.validateMessage(message, "the hello of " + wsUrl, "hello");
    }

    private static RuntimeException rethrown(ExecutionException error) {
        Throwable cause = unwrap(error);
        if (cause instanceof RuntimeException failure) return failure;
        return new IllegalStateException("A channel operation failed", cause);
    }

    /** Sends auth when there is a token; the token's exp when it is a function's JWT (to refresh), else null. */
    private Number authenticate() {
        if (token instanceof TokenSource.None) return null;
        String current = currentToken();
        request(Ordered.of("op", "auth", "token", current), Protocol.PREAUTH_MAX_BODY);
        return token instanceof TokenSource.FromFunction ? tokenExpiry(current) : null;
    }

    // ---- reading

    private void readLoop() {
        try {
            while (true) {
                WebSocketConnection.Incoming incoming;
                try {
                    incoming = socket.read();
                } catch (WebSocketConnection.TooLarge large) {
                    // answered with 1009 (message too big), as websockets answers it
                    failConnection(new JeffError.EngineError("The channel " + wsUrl + " sent a message of more than " + MAX_MESSAGE
                        + " bytes, over the 64 MiB frame limit"), 1009);
                    return;
                } catch (WebSocketConnection.ProtocolViolation violation) {
                    failConnection(new JeffError.EngineError("The channel " + wsUrl + " sent " + violation.getMessage() + ", which breaks "
                        + "the WebSocket protocol (RFC 6455); the channel is closed"), violation.closeCode);
                    return;
                } catch (IOException ended) {
                    ended(ended);
                    return;
                }
                if (incoming == null) {
                    ended(null);
                    return;
                }
                switch (incoming.opcode()) {
                    case WebSocketConnection.BINARY -> received(incoming.data());
                    case WebSocketConnection.TEXT -> {
                        shutdown(new JeffError.EngineError("The channel " + wsUrl + " sent a text message; it carries binary frames only"));
                        return;
                    }
                    case WebSocketConnection.PING -> control(WebSocketConnection.PONG, incoming.data());
                    case WebSocketConnection.PONG -> {
                        byte[] expected = pingPayload;
                        CompletableFuture<Void> waiting = pong;
                        if (expected != null && waiting != null && java.util.Arrays.equals(expected, incoming.data())) waiting.complete(null);
                    }
                    case WebSocketConnection.CLOSE -> {
                        byte[] data = incoming.data();
                        String how;
                        if (data.length >= 2) {
                            int code = ((data[0] & 0xff) << 8) | (data[1] & 0xff);
                            String reason = new String(data, 2, data.length - 2, StandardCharsets.UTF_8);
                            how = "WebSocket close code " + code + (reason.isEmpty() ? "" : ": " + reason);
                        } else {
                            how = "WebSocket close code 1005";
                        }
                        receivedClose = how;
                        // the reply echoes the server's close as it came (checked valid by the connection), as websockets does
                        shutdown(new JeffError.BackendUnavailable(wsUrl + ": the channel closed (" + how + ")"), data);
                    }
                    default -> throw new IllegalStateException("WebSocketConnection.read returned opcode " + incoming.opcode());
                }
            }
        } catch (RuntimeException failure) {
            // every way the reader can die ends the channel with an error naming what went wrong, never an unobserved crash
            JeffError error = new JeffError.EngineError("The channel " + wsUrl + " failed: " + JeffError.describe(failure));
            error.initCause(failure);
            shutdown(error);
        } catch (Error fatal) {
            // an Error (OutOfMemoryError, say) is named to every waiting caller, then thrown on as it is
            JeffError error = new JeffError.EngineError("The channel " + wsUrl + " failed: " + JeffError.describe(fatal));
            error.initCause(fatal);
            shutdown(error);
            throw fatal;
        } finally {
            socket.abort();
            wsClosed.complete(null);
        }
    }

    /** Fails the connection as websockets does: the error recorded, a close frame with `code` sent (waited for at most
     * timeoutMs), then the socket ended by the reader's end. */
    private void failConnection(JeffError error, int code) {
        Outgoing close = shutdown(error, new byte[] {(byte) (code >>> 8), (byte) code});
        if (close == null) return;
        try {
            close.sent.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException | ExecutionException notSent) {
            // the close frame could not go out in time, or the socket is gone: the connection ends without it
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    /** The connection ended (cleanly, or with `cause`). */
    private void ended(IOException cause) {
        String how = receivedClose != null ? receivedClose : "no close frame received";
        JeffError.BackendUnavailable during;
        lock.lock();
        CompletableFuture<Map<String, Object>> hello;
        try {
            hello = greeting;
            greeting = null;
        } finally {
            lock.unlock();
        }
        if (hello != null) {
            during = new JeffError.BackendUnavailable(wsUrl + ": the channel closed during the hello (" + how + ")");
            hello.completeExceptionally(during);
        }
        JeffError.BackendUnavailable closed = new JeffError.BackendUnavailable(wsUrl + ": the channel closed (" + how + ")");
        if (cause != null) closed.initCause(cause);
        shutdown(closed);
    }

    private void received(byte[] data) {
        CompletableFuture<Map<String, Object>> hello;
        lock.lock();
        try {
            hello = greeting;
            greeting = null;
        } finally {
            lock.unlock();
        }
        if (hello == null) {
            onMessage(data);
            return;
        }
        try {
            hello.complete(message(data));
        } catch (JeffError error) {
            hello.completeExceptionally(error);
        }
    }

    /** The checked message one WebSocket message carries (exactly one frame). */
    private Map<String, Object> message(byte[] data) {
        if (data.length < Protocol.HEADER_SIZE) {
            throw new JeffError.EngineError("The channel " + wsUrl + " sent a message of " + data.length + " bytes, shorter than a "
                + "frame header");
        }
        long length = ((data[0] & 0xffL) << 24) | ((data[1] & 0xffL) << 16) | ((data[2] & 0xffL) << 8) | (data[3] & 0xffL);
        if (length != data.length - Protocol.HEADER_SIZE) {
            throw new JeffError.EngineError("The channel " + wsUrl + " sent a message that is not exactly one frame: its header "
                + "announces " + length + " bytes and the message carries " + (data.length - Protocol.HEADER_SIZE));
        }
        try {
            return Protocol.decodeBody(java.util.Arrays.copyOfRange(data, Protocol.HEADER_SIZE, data.length));
        } catch (JeffError.InvalidRequest error) {
            throw new JeffError.EngineError("The channel " + wsUrl + " sent a frame that does not match the socket schema: "
                + error.getMessage());
        }
    }

    /** The error a wire body names, with the check RestTransport makes: a decision it carries must match the decision
     * schema first. */
    private JeffError wireError(Map<String, Object> wire) {
        JeffError made = JeffError.fromWire(wire);
        if (made.details().containsKey("decision") && !Contract.isValid("decision", made.details().get("decision"))) {
            return new JeffError.ContractError("The " + made.type() + " error the channel " + wsUrl + " sent carries a decision that "
                + "does not match contract/schemas/decision: " + Contract.firstProblem("decision", made.details().get("decision")));
        }
        return made;
    }

    private void onMessage(byte[] data) {
        Map<String, Object> message;
        try {
            message = message(data);
        } catch (JeffError error) {
            shutdown(error);
            return;
        }
        if (!message.containsKey("ok")) {
            shutdown(new JeffError.EngineError("The channel " + wsUrl + " sent a message that is not a reply (op "
                + PyFormat.repr(message.get("op")) + "); the channel is closed"));
            return;
        }
        long id = ((Number) message.get("id")).longValue();
        if (id == 0 && Boolean.FALSE.equals(message.get("ok"))) {
            // A failure of the whole connection (the token expired, a frame the server could not read): the server
            // closes the channel after it.
            JeffError error;
            try {
                error = wireError(Values.map(message.get("error")));
            } catch (JeffError malformed) {
                error = malformed;
            }
            shutdown(error);
            return;
        }
        CompletableFuture<Map<String, Object>> waiting;
        boolean release;
        boolean late;
        lock.lock();
        try {
            waiting = pending.remove(id);
            release = waiting == null && unawaited.remove(id);
            late = waiting == null && !release && abandoned.remove(id);
        } finally {
            lock.unlock();
        }
        if (waiting != null) {
            waiting.complete(message);
        } else if (release) {
            if (!Boolean.TRUE.equals(message.get("ok"))) {
                Map<String, Object> error = Values.map(message.get("error"));
                LOG.log(System.Logger.Level.WARNING, "Releasing the handle of a garbage-collected bound function on " + wsUrl
                    + " failed: " + error.get("type") + ": " + error.get("message"));
            }
        } else if (!late) {
            // A reply to an id this side never sent, or one already answered: the two sides disagree about what is
            // outstanding, so no later reply can be trusted to be matched right.
            shutdown(new JeffError.EngineError("The channel " + wsUrl + " sent a reply for id " + id + ", which no request is waiting "
                + "for; the channel is closed"));
        }
        // a late reply to a request whose caller stopped waiting is dropped
    }

    // ---- writing

    /** Queues a message for the writer; under the lock, with the channel open. */
    private void enqueue(Outgoing outgoing) {
        outbox.addLast(outgoing);
        queued.signal();
    }

    /** Queues a control frame (a pong, a ping) unless the channel is closing; null when it is. */
    private Outgoing control(int opcode, byte[] payload) {
        lock.lock();
        try {
            if (closedError != null || closeQueued) return null;
            Outgoing outgoing = new Outgoing(opcode, payload);
            enqueue(outgoing);
            return outgoing;
        } finally {
            lock.unlock();
        }
    }

    private void writeLoop() {
        while (true) {
            Outgoing next;
            lock.lock();
            try {
                while (outbox.isEmpty()) queued.awaitUninterruptibly();
                next = outbox.pollFirst();
            } finally {
                lock.unlock();
            }
            if (!next.state.compareAndSet(Outgoing.QUEUED, Outgoing.WRITING)) continue; // its caller stopped waiting first
            next.started.complete(null);
            long generation = messages.incrementAndGet();
            progressAt = System.nanoTime();
            writing = next;
            Async.TIMERS.schedule(() -> checkStall(generation), timeoutMs, TimeUnit.MILLISECONDS);
            try {
                socket.write(next.opcode, next.payload, () -> progressAt = System.nanoTime());
                writing = null;
                messages.incrementAndGet();
                next.sent.complete(null);
            } catch (IOException failure) {
                writing = null;
                messages.incrementAndGet();
                JeffError error;
                lock.lock();
                try {
                    error = closedError != null ? JeffError.copy(closedError)
                        : new JeffError.BackendUnavailable(wsUrl + ": the channel closed (" + (receivedClose != null ? receivedClose
                            : "no close frame received") + ")");
                } finally {
                    lock.unlock();
                }
                if (error.getCause() == null) error.initCause(failure);
                next.fail(error);
                shutdown(error);
                socket.abort();
                return;
            }
            if (next.opcode == WebSocketConnection.CLOSE) return; // nothing is written after the close frame
        }
    }

    /** Ends the channel when the message `generation` is still being written and its socket accepted no piece of it
     * for timeoutMs. */
    private void checkStall(long generation) {
        if (messages.get() != generation || writing == null) return;
        long idleMs = (System.nanoTime() - progressAt) / 1_000_000;
        if (idleMs < timeoutMs) {
            Async.TIMERS.schedule(() -> checkStall(generation), timeoutMs - idleMs, TimeUnit.MILLISECONDS);
            return;
        }
        // the error first, so the socket ending under it is reported as the stall
        shutdown(new JeffError.BackendUnavailable(wsUrl + ": the socket accepted no bytes of a frame for " + timeoutMs + " ms (the "
            + "server or the network stopped taking data); the channel closed"));
        socket.abort();
    }

    /** Waits for the message's turn to be written (at most timeoutMs, then it is never sent), then until the socket has
     * accepted all of it (bounded by the stall watch and by the channel's end). `op` names it in errors. */
    private void awaitSent(Outgoing outgoing, String op) throws ExecutionException, InterruptedException {
        try {
            outgoing.started.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException late) {
            if (outgoing.abandon()) {
                throw new JeffError.DeadlineExceeded("The channel " + wsUrl + " could not start sending " + PyFormat.repr(op) + " within "
                    + timeoutMs + " ms, as earlier messages were still being written; it was not sent");
            }
            outgoing.started.get(); // the writer took it meanwhile: it is completed already
        } catch (InterruptedException interrupted) {
            outgoing.abandon();
            throw interrupted;
        }
        outgoing.sent.get();
    }

    /** Sends `release` for each handle queued by releaseLater, without waiting for the replies. */
    private void sendReleases() {
        for (Long handle = releases.poll(); handle != null; handle = releases.poll()) {
            long id = nextId.getAndIncrement();
            byte[] frame = register(Ordered.of("op", "release", "handle", handle), id, Protocol.MAX_BODY);
            Outgoing outgoing = new Outgoing(WebSocketConnection.BINARY, frame);
            lock.lock();
            try {
                if (closedError != null) return; // the handles ended with the connection
                unawaited.add(id);
                enqueue(outgoing);
            } finally {
                lock.unlock();
            }
            // nobody waits for this reply; one that cannot be sent has met the channel's end, which every request reports
            outgoing.sent.whenComplete((sent, failure) -> {
                if (failure == null) return;
                lock.lock();
                try {
                    unawaited.remove(id);
                } finally {
                    lock.unlock();
                }
            });
        }
    }

    private byte[] register(Map<String, Object> message, long id, int limit) {
        Map<String, Object> framed = new LinkedHashMap<>(message);
        framed.put("id", id);
        byte[] frame = Protocol.encodeFrame(framed, null);
        if (frame.length - Protocol.HEADER_SIZE > limit) {
            throw new JeffError.InvalidRequest("A " + PyFormat.repr(message.get("op")) + " frame of " + (frame.length - Protocol.HEADER_SIZE)
                + " bytes is over the channel's limit of " + limit + " bytes"
                + (limit == Protocol.PREAUTH_MAX_BODY ? " before auth" : " (50 MiB)"));
        }
        return frame;
    }

    /** Sends one request and waits for its reply; its result, or the error its failure names. */
    private Map<String, Object> request(Map<String, Object> message, int limit) {
        sendReleases();
        long id = nextId.getAndIncrement();
        String op = (String) message.get("op");
        byte[] frame = register(message, id, limit);
        CompletableFuture<Map<String, Object>> reply = new CompletableFuture<>();
        Outgoing outgoing = new Outgoing(WebSocketConnection.BINARY, frame);
        lock.lock();
        try {
            if (closedError != null) throw JeffError.copy(closedError);
            pending.put(id, reply);
            enqueue(outgoing);
        } finally {
            lock.unlock();
        }
        Map<String, Object> answer;
        try {
            try {
                awaitSent(outgoing, op);
            } catch (JeffError.DeadlineExceeded notSent) {
                forget(id, false);
                throw notSent;
            } catch (ExecutionException failed) {
                forget(id, false);
                throw rethrown(failed);
            }
            try {
                answer = reply.get(timeoutMs, TimeUnit.MILLISECONDS);
            } catch (TimeoutException late) {
                forget(id, true);
                throw new JeffError.DeadlineExceeded("The channel " + wsUrl + " did not answer " + PyFormat.repr(op) + " within " + timeoutMs
                    + " ms; the server may still have run the request");
            } catch (ExecutionException failed) {
                throw rethrown(failed);
            }
        } catch (InterruptedException interrupted) {
            forget(id, outgoing.state.get() != Outgoing.ABANDONED);
            Thread.currentThread().interrupt();
            throw new JeffError.BackendUnavailable("Interrupted while waiting for " + wsUrl + " to answer " + PyFormat.repr(op));
        }
        if (!Boolean.TRUE.equals(answer.get("ok"))) throw wireError(Values.map(answer.get("error")));
        return Values.map(answer.get("result"));
    }

    /** The caller stopped waiting for request `id`: forgotten, and when it was sent its late reply is dropped. */
    private void forget(long id, boolean sent) {
        lock.lock();
        try {
            if (pending.remove(id) != null && sent) abandoned.add(id);
        } finally {
            lock.unlock();
        }
    }

    /** A request answered as /v1/decide answers: its result checked against decide_response, holding `key`. */
    private Object answerOf(Map<String, Object> message, String key) {
        Map<String, Object> result = request(message, Protocol.MAX_BODY);
        boolean valid = Contract.isValid("decide_response", result);
        if (!valid || !result.containsKey(key)) {
            String problem = valid ? PyFormat.repr(PyFormat.sorted(result.keySet())) : Contract.firstProblem("decide_response", result);
            throw new JeffError.ContractError("The answer of " + wsUrl + " to " + PyFormat.repr(message.get("op")) + " does not match "
                + "contract/schemas/decide_response with " + PyFormat.repr(key) + ": " + problem);
        }
        return result.get(key);
    }

    // ---- ending

    /** The body of the close frame this side starts the closing handshake with: code 1000, normal closure. */
    private static final byte[] NORMAL_CLOSURE = {(byte) 0x03, (byte) 0xe8};

    private void shutdown(JeffError error) {
        shutdown(error, NORMAL_CLOSURE);
    }

    /** Ends the channel once: the first error wins, carrying every refresh failure as a note; every waiting request
     * (and every message not yet written) fails with its own copy of it; a close frame is queued after the message
     * being written, and the socket is aborted timeoutMs later unless the server has ended it by then. Returns the close
     * frame queued, or null when one was queued before. */
    private Outgoing shutdown(JeffError error, byte[] closeBody) {
        List<CompletableFuture<Map<String, Object>>> waiting;
        List<Outgoing> unsent;
        CompletableFuture<Map<String, Object>> hello;
        JeffError cause;
        boolean startClose = false;
        Outgoing close = null;
        lock.lock();
        try {
            if (closedError == null) {
                refreshFailures.forEach(error::addNote);
                closedError = error;
            }
            cause = closedError;
            hello = greeting;
            greeting = null;
            waiting = new ArrayList<>(pending.values());
            pending.clear();
            unawaited.clear();
            abandoned.clear();
            unsent = new ArrayList<>(outbox);
            outbox.clear();
            if (!closeQueued) {
                closeQueued = true;
                startClose = true;
                close = new Outgoing(WebSocketConnection.CLOSE, closeBody);
                enqueue(close);
            }
        } finally {
            lock.unlock();
        }
        if (hello != null) hello.completeExceptionally(JeffError.copy(cause));
        for (CompletableFuture<Map<String, Object>> request : waiting) request.completeExceptionally(JeffError.copy(cause));
        for (Outgoing outgoing : unsent) outgoing.fail(JeffError.copy(cause));
        Outgoing current = writing;
        if (current != null) current.fail(JeffError.copy(cause));
        CompletableFuture<Void> waitingPong = pong;
        if (waitingPong != null) waitingPong.completeExceptionally(JeffError.copy(cause));
        closing.countDown();
        if (startClose) {
            Async.TIMERS.schedule(() -> {
                if (!wsClosed.isDone()) socket.abort();
            }, timeoutMs, TimeUnit.MILLISECONDS);
        }
        return close;
    }

    // ---- the Transport

    @Override
    public List<Map<String, Object>> decideCalls(List<Map<String, Object>> calls, Object policy) {
        List<Map<String, Object>> decisions = new ArrayList<>();
        for (Object item : Values.list(answerOf(Ordered.of("op", "decide", "calls", calls, "on_low_confidence", policy),
            "decisions"))) {
            decisions.add(Values.map(item));
        }
        if (decisions.size() != calls.size()) {
            throw new JeffError.ContractError(wsUrl + " answered " + decisions.size() + " decisions for " + calls.size() + " calls");
        }
        return decisions;
    }

    private static Map<String, Object> withOp(String op, Map<String, Object> body) {
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("op", op);
        message.putAll(body);
        return message;
    }

    @Override
    public Map<String, Object> dispatch(Map<String, Object> body) {
        return Values.map(answerOf(withOp("dispatch", body), "decision"));
    }

    @Override
    public Map<String, Object> graph(Map<String, Object> body) {
        return Values.map(answerOf(withOp("graph", body), "graph_result"));
    }

    @Override
    public Map<String, Object> functions() {
        return rest.functions();
    }

    // ---- handles

    /** Binds `function` (its full ID) with this context, labels (wire form) and policy (wire form). */
    public Handle bind(String function, Map<String, Object> context, Object labels, Object policy) {
        Map<String, Object> message = Ordered.of("op", "bind", "function", function, "on_low_confidence", policy);
        if (context != null) message.put("context", new LinkedHashMap<>(context));
        if (labels != null) message.put("labels", labels);
        Map<String, Object> result = request(message, Protocol.MAX_BODY);
        if (!(result.get("handle") instanceof Long handle) || handle < 1 || !function.equals(result.get("function"))
            || !(result.get("version") instanceof String version)) {
            throw new JeffError.ContractError(wsUrl + " answered bind " + PyFormat.repr(function) + " with " + PyFormat.repr(result)
                + "; expected a handle (a whole number from 1) for " + PyFormat.repr(function) + " and its adapter's version");
        }
        return new Handle(handle, version);
    }

    public Map<String, Object> call(long handle, Object input) {
        return Values.map(answerOf(Ordered.of("op", "call", "handle", handle, "input", input), "decision"));
    }

    public List<Map<String, Object>> callMany(long handle, List<?> inputs) {
        List<Map<String, Object>> decisions = new ArrayList<>();
        for (Object item : Values.list(answerOf(Ordered.of("op", "call_many", "handle", handle, "inputs", new ArrayList<>(inputs)),
            "decisions"))) {
            decisions.add(Values.map(item));
        }
        if (decisions.size() != inputs.size()) {
            throw new JeffError.ContractError(wsUrl + " answered " + decisions.size() + " decisions for " + inputs.size() + " inputs");
        }
        return decisions;
    }

    public void release(long handle) {
        request(Ordered.of("op", "release", "handle", handle), Protocol.MAX_BODY);
    }

    /** Queues a release, sent before the next request: safe from a Cleaner action (it takes no lock, sends nothing
     * itself and never throws). Nothing to do once the channel is closed: its handles ended with it. */
    public void releaseLater(long handle) {
        if (closing.getCount() > 0) releases.add(handle);
    }

    public boolean isClosed() {
        lock.lock();
        try {
            return closedError != null;
        } finally {
            lock.unlock();
        }
    }

    // ---- keepalive

    /** websockets' keepalive: a ping every keepaliveMs; no pong within keepaliveMs of it being sent ends the channel. */
    private void keepalive() {
        while (!waitOrClosed(keepaliveMs)) {
            byte[] payload = WebSocketConnection.randomPayload();
            CompletableFuture<Void> answered = new CompletableFuture<>();
            pingPayload = payload;
            pong = answered;
            Outgoing ping = control(WebSocketConnection.PING, payload);
            if (ping == null) return;
            try {
                ping.sent.get();
                answered.get(keepaliveMs, TimeUnit.MILLISECONDS);
            } catch (TimeoutException late) {
                shutdown(new JeffError.BackendUnavailable(wsUrl + ": the server did not answer a keepalive ping within " + keepaliveMs
                    + " ms; the channel closed"));
                socket.abort();
                return;
            } catch (ExecutionException | InterruptedException ended) {
                return; // the channel closed (it reports its own error), or the thread is being stopped
            }
        }
    }

    // ---- refreshing

    /** Waits `ms`; true when the channel closed meanwhile. */
    private boolean waitOrClosed(long ms) {
        try {
            return closing.await(ms, TimeUnit.MILLISECONDS);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            return true; // the thread is being stopped: it ends as when the channel closes
        }
    }

    /** One refresh attempt: the function's token, sent with `refresh` when it expires after `expires`; its exp. */
    private Number refreshOnce(Number expires) {
        String fresh = currentToken();
        Number renewed = tokenExpiry(fresh);
        if (renewed == null || renewed.doubleValue() <= expires.doubleValue()) {
            throw new JeffError.Unauthorised("the token function returned a token expiring at " + PyFormat.repr(renewed)
                + ", not after the channel's current token (exp " + PyFormat.repr(expires) + ")");
        }
        request(Ordered.of("op", "refresh", "token", fresh), Protocol.MAX_BODY);
        return renewed;
    }

    /** Milliseconds from now until `seconds` (a Unix time) less `beforeS`, at least 0; a time too far ahead to count in
     * milliseconds waits as long as a long can say. */
    private static long millisUntil(Number seconds, long beforeS) {
        double target = (seconds.doubleValue() - beforeS) * 1000 - System.currentTimeMillis();
        return target <= 0 ? 0 : target >= Long.MAX_VALUE ? Long.MAX_VALUE : (long) target;
    }

    /** From REFRESH_BEFORE_S seconds before each token's exp (and at least REFRESH_RETRY_S after the last successful
     * refresh), asks the token function for a token expiring later and sends `refresh` with it; each failure is kept
     * (and logged, without the token) and the function asked again every REFRESH_RETRY_S seconds. If exp passes first,
     * the channel closes with Unauthorised carrying every failure as a note. */
    private void refreshLoop(Number first) {
        Number expires = first;
        long earliest = 0;
        while (true) {
            long waitMs = Math.max(millisUntil(expires, REFRESH_BEFORE_S), Math.max(0, earliest - System.currentTimeMillis()));
            if (waitOrClosed(waitMs)) return;
            Number renewed = null;
            while (renewed == null) {
                try {
                    renewed = refreshOnce(expires);
                } catch (RuntimeException error) {
                    if (isClosed()) return;
                    String failure = "Refreshing the channel's token (exp " + PyFormat.repr(expires) + ") at "
                        + PyFormat.fixed(System.currentTimeMillis() / 1000.0, 1) + " failed: " + JeffError.describe(error);
                    refreshFailures.add(failure);
                    LOG.log(System.Logger.Level.WARNING, wsUrl + ": " + failure);
                    long remaining = millisUntil(expires, 0);
                    if (remaining <= 0) {
                        shutdown(new JeffError.Unauthorised("The channel's token expired at " + PyFormat.repr(expires) + " before the "
                            + "token function returned one expiring later (" + refreshFailures.size() + " attempts failed; see the "
                            + "notes); the channel is closed"));
                        return;
                    }
                    if (waitOrClosed(Math.min(REFRESH_RETRY_S * 1000, remaining))) return;
                }
            }
            refreshFailures.clear();
            expires = renewed;
            earliest = System.currentTimeMillis() + REFRESH_RETRY_S * 1000;
        }
    }

    // ---- closing

    /** Ends the channel (with a closing handshake of at most timeoutMs) and the HTTP connections. */
    @Override
    public void close() {
        shutdown(new JeffError.InvalidRequest("This remote client of " + url + " is closed; connect again with Jeff.remote"));
        rest.close();
        try {
            wsClosed.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException error) {
            socket.abort();
        } catch (ExecutionException error) {
            throw new IllegalStateException("Closing " + wsUrl + " failed", error);
        } catch (InterruptedException error) {
            socket.abort();
            Thread.currentThread().interrupt();
            throw new JeffError.BackendUnavailable("Interrupted while closing " + wsUrl);
        }
    }

    /** For a ChannelJeff the garbage collector took without close(): ends the channel at once (no closing handshake).
     * Never throws: it records the closed error, aborts the socket and closes the HTTP connections. */
    void abandon() {
        shutdown(new JeffError.InvalidRequest("This remote client of " + url + " is closed; connect again with Jeff.remote"));
        socket.abort();
        rest.close();
    }

    /** A message as an ordered map. */
    private static final class Ordered {
        static Map<String, Object> of(Object... keysAndValues) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (int index = 0; index < keysAndValues.length; index += 2) out.put((String) keysAndValues[index], keysAndValues[index + 1]);
            return out;
        }
    }
}
