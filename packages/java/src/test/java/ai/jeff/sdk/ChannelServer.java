package ai.jeff.sdk;

import ai.jeff.sdk.testing.FakeEngine;
import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.ReentrantLock;

/** A fake Jeff server for the channel tests, on one port: GET /v1/functions answers the listing recorded in
 * remote.json, and a WebSocket at /v1/channel (a minimal RFC 6455 server) answers hello, auth, refresh, bind, call,
 * call_many, decide and release over a local Jeff with a fake engine, as jeff-server's channel does
 * (contract/socket.md, "The channel"). */
final class ChannelServer implements AutoCloseable {
    static final List<String> INSTALLED = List.of("jeff.support.triage", "jeff.safety.spam");
    /** The most requests a connection may have outstanding (contract/socket.md, "Limits"). */
    private static final int MAX_OUTSTANDING = 256;
    private static final String GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    /** Below triage.urgency's calibration threshold 0.6. */
    private static final double[] UNSURE = {0, 0.5, 0.5, 0, 0};
    private static final String LISTING = (String) Values.map(Values.map(Values.list(
        Fixtures.cases("remote.json").get(0).get("exchanges")).get(0)).get("response")).get("body");

    /** `version`: what bind reports for every adapter (null: the catalogue's); `silent`: ops that get no answer;
     * `token`: the only token auth and refresh accept (null: any); `gated`: ops whose requests wait until openGate();
     * `reorder`: when above 0, the replies to call, call_many and decide are held until that many are ready, then sent
     * in reverse order; `badLowConfidenceDecision`: call, call_many and decide fail with a LowConfidence whose decision
     * does not match the decision schema; `ignorePings`: pings get no pong; `stopReadingAfter`: once a message with
     * this op has been read, the connection is never read again (its socket buffers fill: the client's sends stall);
     * `bytesPerSecond`: when above 0, the connection is read at about this rate, through a small receive buffer;
     * `interleave`: every reply is two fragments with this control frame between them (see withInterleaved). As
     * jeff-server, a request arriving while 256 are outstanding fails at once with RateLimited. */
    record Options(String version, Set<String> silent, String token, Set<String> gated, int reorder, boolean badLowConfidenceDecision,
                   boolean ignorePings, String stopReadingAfter, long bytesPerSecond, String interleave) {
        static Options defaults() {
            return new Options(null, Set.of(), null, Set.of(), 0, false, false, null, 0, null);
        }

        Options withVersion(String value) { return new Options(value, silent, token, gated, reorder, badLowConfidenceDecision, ignorePings, stopReadingAfter, bytesPerSecond, interleave); }
        Options withSilent(String... ops) { return new Options(version, Set.of(ops), token, gated, reorder, badLowConfidenceDecision, ignorePings, stopReadingAfter, bytesPerSecond, interleave); }
        Options withToken(String value) { return new Options(version, silent, value, gated, reorder, badLowConfidenceDecision, ignorePings, stopReadingAfter, bytesPerSecond, interleave); }
        Options withGated(String... ops) { return new Options(version, silent, token, Set.of(ops), reorder, badLowConfidenceDecision, ignorePings, stopReadingAfter, bytesPerSecond, interleave); }
        Options withReorder(int count) { return new Options(version, silent, token, gated, count, badLowConfidenceDecision, ignorePings, stopReadingAfter, bytesPerSecond, interleave); }
        Options withBadLowConfidenceDecision() { return new Options(version, silent, token, gated, reorder, true, ignorePings, stopReadingAfter, bytesPerSecond, interleave); }
        Options withIgnoredPings() { return new Options(version, silent, token, gated, reorder, badLowConfidenceDecision, true, stopReadingAfter, bytesPerSecond, interleave); }
        Options withStopReadingAfter(String op) { return new Options(version, silent, token, gated, reorder, badLowConfidenceDecision, ignorePings, op, bytesPerSecond, interleave); }
        /** Every reply is sent as two fragments with a control frame between them: "ping", "pong" or "close" (1000). */
        Options withInterleaved(String control) { return new Options(version, silent, token, gated, reorder, badLowConfidenceDecision, ignorePings, stopReadingAfter, bytesPerSecond, control); }
        Options withBytesPerSecond(long rate) { return new Options(version, silent, token, gated, reorder, badLowConfidenceDecision, ignorePings, stopReadingAfter, rate, interleave); }
    }

    /** The fake engine's answers: a score function's level 2, confidently, unless the input contains "unsure"; a
     * choice's first criterion; every other type's first option. */
    static double[] answers(String adapter, Row row) {
        if (row.question().type().equals("score")) return Json.write(row.state()).contains("unsure") ? UNSURE : new double[] {0, 0, 0.9, 0.1, 0};
        Object criteria = row.question().criteria();
        int count = criteria == null ? 2 : criteria instanceof Map<?, ?> options ? options.size() : ((List<?>) criteria).size();
        double[] answer = new double[count];
        answer[0] = 1;
        return answer;
    }

    private final class Connection {
        final Socket socket;
        final OutputStream out;
        final ReentrantLock writing = new ReentrantLock();
        final AtomicInteger outstanding = new AtomicInteger();

        Connection(Socket socket) throws IOException {
            this.socket = socket;
            this.out = socket.getOutputStream();
        }

        /** `payload` as a binary message in two fragments, with a ping, pong or close (1000) between them. */
        void fragmented(byte[] payload, String control) {
            int half = payload.length / 2;
            java.io.ByteArrayOutputStream bytes = new java.io.ByteArrayOutputStream();
            frameInto(bytes, 0x02, Arrays.copyOfRange(payload, 0, half));
            switch (control) {
                case "ping" -> frameInto(bytes, 0x89, new byte[] {1, 2, 3, 4});
                case "pong" -> frameInto(bytes, 0x8a, new byte[] {5, 6});
                case "close" -> frameInto(bytes, 0x88, new byte[] {0x03, (byte) 0xe8});
                default -> throw new AssertionError("no control frame " + control);
            }
            frameInto(bytes, 0x80, Arrays.copyOfRange(payload, half, payload.length));
            raw(bytes.toByteArray());
        }

        void raw(byte[] bytes) {
            writing.lock();
            try {
                out.write(bytes);
                out.flush();
            } catch (IOException closed) {
                // the client went away
            } finally {
                writing.unlock();
            }
        }

        void write(int opcode, byte[] payload) {
            writing.lock();
            try {
                out.write(0x80 | opcode);
                if (payload.length < 126) out.write(payload.length);
                else if (payload.length < 65536) {
                    out.write(126);
                    out.write(payload.length >>> 8);
                    out.write(payload.length);
                } else {
                    out.write(127);
                    for (int shift = 56; shift >= 0; shift -= 8) out.write((int) ((long) payload.length >>> shift));
                }
                out.write(payload);
                out.flush();
            } catch (IOException closed) {
                // the client went away; its requests end with the connection
            } finally {
                writing.unlock();
            }
        }
    }

    private final Options options;
    private final ServerSocket server;
    private final Catalogue catalogue = Catalogue.shipped();
    private final Jeff jeff = Jeff.create(new FakeEngine(ChannelServer::answers, INSTALLED), catalogue);
    private final List<Map<String, Object>> received = Collections.synchronizedList(new ArrayList<>());
    private final List<String> headers = Collections.synchronizedList(new ArrayList<>());
    private final Set<Connection> connections = ConcurrentHashMap.newKeySet();
    private final Map<Long, Map<String, Object>> handles = new ConcurrentHashMap<>();
    private final AtomicLong nextHandle = new AtomicLong(1);
    private final CountDownLatch gate = new CountDownLatch(1);
    private final AtomicInteger waiting = new AtomicInteger();
    private final ReentrantLock holding = new ReentrantLock();
    private final List<Map.Entry<Connection, byte[]>> held = new ArrayList<>();
    private final AtomicInteger pings = new AtomicInteger();
    private final AtomicInteger pongs = new AtomicInteger();
    private final List<byte[]> closes = Collections.synchronizedList(new ArrayList<>());

    private final boolean tls;

    private ChannelServer(Options options, javax.net.ssl.SSLContext tls) throws IOException {
        this.options = options;
        this.tls = tls != null;
        this.server = tls == null ? new ServerSocket(0, 512, InetAddress.getLoopbackAddress())
            : tls.getServerSocketFactory().createServerSocket(0, 512, InetAddress.getLoopbackAddress());
        Thread.ofVirtual().start(this::accept);
    }

    static ChannelServer start(Options options) {
        return start(options, null);
    }

    /** With TLS (`tls` presents the server's certificate): https:// and wss://. */
    static ChannelServer start(Options options, javax.net.ssl.SSLContext tls) {
        try {
            return new ChannelServer(options, tls);
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }

    String url() {
        return (tls ? "https://localhost:" : "http://127.0.0.1:") + server.getLocalPort();
    }

    /** Writes `bytes` as they are (one or more WebSocket frames the test made) on every open connection. */
    void sendRaw(byte[] bytes) {
        for (Connection connection : connections) connection.raw(bytes);
    }

    /** How many pings the server has read. */
    int pings() {
        return pings.get();
    }

    /** How many pongs the server has read. */
    int pongs() {
        return pongs.get();
    }

    /** The payload of every close frame the clients sent. */
    List<byte[]> closes() {
        return List.copyOf(closes);
    }

    List<Map<String, Object>> received() {
        return List.copyOf(received);
    }

    List<String> headers() {
        return List.copyOf(headers);
    }

    List<Object> ops() {
        List<Object> ops = new ArrayList<>();
        for (Map<String, Object> message : received()) ops.add(message.get("op"));
        return ops;
    }

    /** Sends `message` as one frame on every open connection. */
    void send(Map<String, Object> message) {
        for (Connection connection : connections) connection.write(2, EngineServer.frame(message));
    }

    void openGate() {
        gate.countDown();
    }

    int waiting() {
        return waiting.get();
    }

    /** Ends every connection at once, without a close frame. */
    void dropConnections() {
        for (Connection connection : connections) {
            try {
                connection.socket.close();
            } catch (IOException error) {
                throw new UncheckedIOException(error);
            }
        }
    }

    private void accept() {
        while (true) {
            Socket socket;
            try {
                socket = server.accept();
            } catch (IOException closed) {
                return; // close() closed the server
            }
            Thread.ofVirtual().start(() -> handle(socket));
        }
    }

    private static void frameInto(java.io.ByteArrayOutputStream out, int first, byte[] payload) {
        out.write(first);
        if (payload.length < 126) {
            out.write(payload.length);
        } else if (payload.length < 65536) {
            out.write(126);
            out.write(payload.length >>> 8);
            out.write(payload.length);
        } else {
            out.write(127);
            for (int shift = 56; shift >= 0; shift -= 8) out.write((int) ((long) payload.length >>> shift));
        }
        out.writeBytes(payload);
    }

    private static String line(InputStream in) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        for (int b = in.read(); b != -1 && b != '\n'; b = in.read()) if (b != '\r') bytes.write(b);
        return bytes.toString(StandardCharsets.ISO_8859_1);
    }

    private static byte[] read(InputStream in, long count) throws IOException {
        byte[] data = in.readNBytes((int) count);
        if (data.length != count) throw new IOException("the connection ended in a frame");
        return data;
    }

    private void handle(Socket socket) {
        try (socket) {
            if (options.bytesPerSecond() > 0 || options.stopReadingAfter() != null) socket.setReceiveBufferSize(16 * 1024);
            InputStream in = options.bytesPerSecond() > 0 ? new Throttled(socket.getInputStream(), options.bytesPerSecond())
                : new BufferedInputStream(socket.getInputStream());
            String[] request = line(in).split(" ");
            Map<String, String> fields = new LinkedHashMap<>();
            for (String field = line(in); !field.isEmpty(); field = line(in)) {
                int colon = field.indexOf(':');
                fields.put(field.substring(0, colon).trim().toLowerCase(Locale.ROOT), field.substring(colon + 1).trim());
            }
            OutputStream out = socket.getOutputStream();
            String path = request[1];
            boolean upgrade = "websocket".equalsIgnoreCase(fields.get("upgrade"));
            if (path.equals("/v1/functions") && !upgrade) {
                byte[] body = LISTING.getBytes(StandardCharsets.UTF_8);
                out.write(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + body.length
                    + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
                out.write(body);
                out.flush();
                return;
            }
            if (!path.equals("/v1/channel") || !upgrade) {
                out.write("HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
                out.flush();
                return;
            }
            String accept = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-1")
                .digest((fields.get("sec-websocket-key") + GUID).getBytes(StandardCharsets.ISO_8859_1)));
            out.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                + accept + "\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
            out.flush();
            headers.add(fields.containsKey("authorization") ? fields.get("authorization") : "");
            Connection connection = new Connection(socket);
            connections.add(connection);
            ByteArrayOutputStream message = new ByteArrayOutputStream();
            while (true) {
                int first = in.read();
                if (first == -1) return;
                int opcode = first & 0x0f;
                int second = in.read();
                long length = second & 0x7f;
                if (length == 126) length = ((long) in.read() << 8) | in.read();
                else if (length == 127) {
                    length = 0;
                    for (int index = 0; index < 8; index++) length = (length << 8) | in.read();
                }
                byte[] mask = (second & 0x80) != 0 ? read(in, 4) : null;
                byte[] payload = read(in, length);
                if (mask != null) for (int index = 0; index < payload.length; index++) payload[index] ^= mask[index % 4];
                if (opcode == 8) {
                    closes.add(payload);
                    connection.write(8, payload);
                    return;
                }
                if (opcode == 10) {
                    pongs.incrementAndGet();
                    continue;
                }
                if (opcode == 9) {
                    pings.incrementAndGet();
                    if (!options.ignorePings()) connection.write(10, payload);
                    continue;
                }
                if (opcode == 2 || opcode == 0) message.write(payload);
                if ((first & 0x80) != 0 && (opcode == 2 || opcode == 0)) {
                    String op = onMessage(connection, message.toByteArray());
                    message.reset();
                    if (op.equals(options.stopReadingAfter())) {
                        gate.await(); // never read again (until close())
                        return;
                    }
                }
            }
        } catch (IOException ended) {
            // the client closed the connection, or dropConnections() did
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        } finally {
            connections.removeIf(connection -> connection.socket == socket);
        }
    }

    private static byte[] reply(Object id, Map<String, Object> body) {
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("id", id);
        message.putAll(body);
        return EngineServer.frame(message);
    }

    private static Map<String, Object> failure(String type, String text, Map<String, Object> details) {
        Map<String, Object> all = new LinkedHashMap<>();
        all.put("channel_id", "0badc0de");
        all.putAll(details);
        return Fixtures.ordered("ok", false, "error", Fixtures.ordered("type", type, "message", text, "details", all));
    }

    /** On the connection's reader thread: recorded, refused at once past 256 outstanding, then answered on a thread
     * of its own (a gated request waits there). */
    private String onMessage(Connection connection, byte[] frame) {
        Map<String, Object> message = Values.map(MsgPack.decode(Arrays.copyOfRange(frame, 4, frame.length)));
        received.add(message);
        String op = (String) message.get("op");
        if (options.silent().contains(op)) return op;
        if (!op.equals("hello")) {
            if (connection.outstanding.get() >= MAX_OUTSTANDING) {
                connection.write(2, reply(message.get("id"), failure("RateLimited", MAX_OUTSTANDING + " requests are outstanding "
                    + "on this connection", Fixtures.ordered("retry_after_s", 1L))));
                return op;
            }
            connection.outstanding.incrementAndGet();
        }
        Thread.ofVirtual().start(() -> answer(connection, message, op));
        return op;
    }

    /** Reads at about `rate` bytes a second, in small pieces. */
    private static final class Throttled extends java.io.FilterInputStream {
        private final long rate;

        Throttled(InputStream in, long rate) {
            super(in);
            this.rate = rate;
        }

        @Override
        public int read() throws IOException {
            byte[] one = new byte[1];
            return read(one, 0, 1) < 0 ? -1 : one[0] & 0xff;
        }

        @Override
        public int read(byte[] buffer, int offset, int length) throws IOException {
            int count = super.read(buffer, offset, Math.min(length, 8 * 1024));
            if (count > 0) {
                try {
                    Thread.sleep(count * 1000L / rate);
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt();
                    throw new IOException("interrupted", error);
                }
            }
            return count;
        }
    }

    private void respond(Connection connection, Map<String, Object> message, String op, Map<String, Object> body) {
        connection.outstanding.decrementAndGet();
        byte[] bytes = reply(message.get("id"), body);
        if (options.interleave() != null) {
            connection.fragmented(bytes, options.interleave());
            return;
        }
        if (options.reorder() == 0 || !List.of("call", "call_many", "decide").contains(op)) {
            connection.write(2, bytes);
            return;
        }
        List<Map.Entry<Connection, byte[]>> ready = new ArrayList<>();
        holding.lock();
        try {
            held.add(Map.entry(connection, bytes));
            if (held.size() == options.reorder()) {
                ready.addAll(held);
                held.clear();
            }
        } finally {
            holding.unlock();
        }
        Collections.reverse(ready);
        for (Map.Entry<Connection, byte[]> item : ready) item.getKey().write(2, item.getValue());
    }

    private Call callOf(Map<String, Object> bound, Object input) {
        return new Call((String) bound.get("function"), input, Values.map(bound.get("context")), bound.get("labels"));
    }

    private void answer(Connection connection, Map<String, Object> message, String op) {
        if (options.gated().contains(op)) {
            waiting.incrementAndGet();
            try {
                gate.await();
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                return;
            }
            waiting.decrementAndGet();
        }
        if (options.badLowConfidenceDecision() && List.of("call", "call_many", "decide").contains(op)) {
            respond(connection, message, op, failure("LowConfidence", "abstained", Fixtures.ordered("decision", Fixtures.ordered("value", 1L))));
            return;
        }
        switch (op) {
            case "hello" -> connection.write(2, EngineServer.frame(Fixtures.ordered("op", "hello", "version", 2L)));
            case "auth", "refresh" -> {
                if (options.token() != null && !options.token().equals(message.get("token"))) {
                    respond(connection, message, op, failure("Unauthorised", "the token is not valid", Map.of()));
                } else {
                    respond(connection, message, op, Fixtures.ordered("ok", true, "result", new LinkedHashMap<>()));
                }
            }
            case "bind" -> {
                long handle = nextHandle.getAndIncrement();
                handles.put(handle, message);
                String adapter = catalogue.function((String) message.get("function")).adapterId();
                String version = options.version() == null ? catalogue.adapter(adapter).version() : options.version();
                respond(connection, message, op, Fixtures.ordered("ok", true, "result",
                    Fixtures.ordered("handle", handle, "function", message.get("function"), "version", version)));
            }
            case "call", "call_many" -> {
                Map<String, Object> bound = handles.get(((Number) message.get("handle")).longValue());
                if (bound == null) {
                    respond(connection, message, op, failure("InvalidRequest", "handle " + message.get("handle") + " is not bound", Map.of()));
                    return;
                }
                List<Object> inputs = op.equals("call") ? Collections.singletonList(message.get("input")) : Values.list(message.get("inputs"));
                List<Call> calls = new ArrayList<>();
                for (Object input : inputs) calls.add(callOf(bound, input));
                List<Object> decisions = new ArrayList<>();
                for (Decision decision : jeff.decide(calls)) decisions.add(decision.toJson());
                respond(connection, message, op, Fixtures.ordered("ok", true, "result",
                    op.equals("call") ? Fixtures.ordered("decision", decisions.get(0)) : Fixtures.ordered("decisions", decisions)));
            }
            case "decide" -> {
                List<Call> calls = new ArrayList<>();
                for (Object item : Values.list(message.get("calls"))) {
                    Map<String, Object> call = Values.map(item);
                    calls.add(new Call((String) call.get("function"), call.get("input"), Values.map(call.get("context")), call.get("labels")));
                }
                List<Object> decisions = new ArrayList<>();
                for (Decision decision : jeff.decide(calls)) decisions.add(decision.toJson());
                respond(connection, message, op, Fixtures.ordered("ok", true, "result", Fixtures.ordered("decisions", decisions)));
            }
            case "release" -> {
                if (handles.remove(((Number) message.get("handle")).longValue()) == null) {
                    respond(connection, message, op, failure("InvalidRequest", "handle " + message.get("handle") + " is not bound", Map.of()));
                } else {
                    respond(connection, message, op, Fixtures.ordered("ok", true, "result", new LinkedHashMap<>()));
                }
            }
            default -> throw new AssertionError("ChannelServer does not answer " + op);
        }
    }

    @Override
    public void close() {
        gate.countDown();
        try {
            server.close();
            for (Connection connection : connections) connection.socket.close();
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }
}
