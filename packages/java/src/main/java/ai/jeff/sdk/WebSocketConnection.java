package ai.jeff.sdk;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.UnknownHostException;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.function.Supplier;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/** One client WebSocket (RFC 6455) over a blocking socket, as the Python SDK's websockets client opens and speaks one
 * with compression off: directly, or through an HTTP proxy's CONNECT tunnel (for ws:// and wss:// alike, as websockets
 * does), then TLS for wss:// with the server's certificate and name checked, then the upgrade request. Messages are
 * sent unfragmented, each written in pieces of at most SEND_CHUNK bytes (masked, as a client must), and every piece
 * the socket accepts is reported as progress. Messages read are gathered from their fragments and refused once they
 * would exceed `maxMessage` bytes, before anything is allocated for them. Nothing here starts a thread of its own:
 * one caller reads, one caller writes (ChannelTransport's reader and writer), and abort() from any thread ends both at
 * once. */
final class WebSocketConnection {
    /** The largest piece of a message handed to the socket at once (the Python SDK's SEND_CHUNK). */
    static final int SEND_CHUNK = 64 * 1024;
    /** The largest head (the proxy's answer to CONNECT, or the server's to the upgrade) read. */
    static final int MAX_HEAD = 64 * 1024;
    static final int CONTINUATION = 0;
    static final int TEXT = 1;
    static final int BINARY = 2;
    static final int CLOSE = 8;
    static final int PING = 9;
    static final int PONG = 10;
    private static final String GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final SecureRandom RANDOM = new SecureRandom();

    /** Opening failed: the connection could not be made (`cause` names why), the proxy refused the tunnel (`proxyStatus`
     * set), the proxy's answer was not HTTP, the server answered the upgrade with another status (`status` set), or
     * the server's 101 did not complete a WebSocket handshake. `timedOut` when the time ran out first. */
    static final class OpenFailure extends Exception {
        final boolean timedOut;
        final int status;
        final int proxyStatus;

        OpenFailure(String message, Throwable cause, boolean timedOut, int status, int proxyStatus) {
            super(message, cause);
            this.timedOut = timedOut;
            this.status = status;
            this.proxyStatus = proxyStatus;
        }
    }

    /** The server broke RFC 6455 (a masked frame, reserved bits, an unknown opcode, a bad continuation, an invalid
     * close frame, ...). `closeCode` is the code this side answers with, as websockets answers it: 1002 (protocol
     * error), or 1007 (invalid data) for a close reason that is not UTF-8. */
    static final class ProtocolViolation extends IOException {
        final int closeCode;

        ProtocolViolation(String message) {
            this(message, 1002);
        }

        ProtocolViolation(String message, int closeCode) {
            super(message);
            this.closeCode = closeCode;
        }
    }

    /** A message that would be larger than the limit; nothing of it was kept. */
    static final class TooLarge extends IOException {
        TooLarge(long size) {
            super("a message of at least " + size + " bytes");
        }
    }

    /** A whole message read: a data message (TEXT or BINARY, its fragments joined) or a control frame. */
    record Incoming(int opcode, byte[] data) {}

    private final SocketChannel channel;
    private final InputStream in;
    private final OutputStream out;
    private final long maxMessage;
    private final byte[] piece = new byte[SEND_CHUNK];

    private WebSocketConnection(SocketChannel channel, InputStream in, OutputStream out, long maxMessage) {
        this.channel = channel;
        this.in = in;
        this.out = out;
        this.maxMessage = maxMessage;
    }

    /** What to open: the server's host, port and whether it is wss://, the request path, the extra header fields
     * (Authorization), the proxy (null: directly), how connections are opened and TLS sockets made. */
    record Target(String host, int port, boolean secure, String path, Map<String, String> headers, Proxies.Proxy proxy,
                  HttpConnection.Opener opener, Supplier<SSLSocketFactory> tls) {}

    /** Everything opening holds, so the deadline (or the caller) can end it from another thread at once. */
    private static final class Opening {
        final List<SocketChannel> channels = new CopyOnWriteArrayList<>();
        final CompletableFuture<InetAddress[]> lookup = new CompletableFuture<>();
        /** OPENING until either the deadline expires it or opening finishes it, whichever comes first, once. */
        final java.util.concurrent.atomic.AtomicInteger state = new java.util.concurrent.atomic.AtomicInteger(OPENING);
        volatile boolean expired;
        static final int OPENING = 0;
        static final int OPENED = 1;
        static final int EXPIRED = 2;

        void expire() {
            if (!state.compareAndSet(OPENING, EXPIRED)) return; // opened just in time: the deadline no longer applies
            expired = true;
            lookup.completeExceptionally(new TimeoutException());
            for (SocketChannel channel : channels) close(channel);
        }

        /** True when opening finished before the deadline; after this, the deadline never closes the connection. */
        boolean finish() {
            return state.compareAndSet(OPENING, OPENED);
        }
    }

    private static void close(SocketChannel channel) {
        try {
            channel.close();
        } catch (IOException error) {
            // SocketChannel.close marks the channel closed before releasing it: it is closed whatever this says
        }
    }

    /** Opens the WebSocket, at most timeoutMs for all of it (the lookup, TCP, the tunnel, TLS and the upgrade). */
    static WebSocketConnection open(Target target, long timeoutMs, long maxMessage) throws OpenFailure {
        Opening opening = new Opening();
        ScheduledFuture<?> deadline = Async.TIMERS.schedule(opening::expire, timeoutMs, TimeUnit.MILLISECONDS);
        long started = System.nanoTime();
        try {
            return openWithin(target, timeoutMs, maxMessage, opening, started);
        } catch (OpenFailure failure) {
            for (SocketChannel channel : opening.channels) close(channel);
            throw failure;
        } catch (IOException | RuntimeException error) {
            for (SocketChannel channel : opening.channels) close(channel);
            throw new OpenFailure(JeffError.describe(root(error)), error, opening.expired, 0, 0);
        } catch (Error error) {
            // cleanup only: the Error goes on unchanged
            for (SocketChannel channel : opening.channels) close(channel);
            throw error;
        } finally {
            deadline.cancel(false);
        }
    }

    private static Throwable root(Throwable error) {
        Throwable cause = error;
        while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
        return cause;
    }

    private static WebSocketConnection openWithin(Target target, long timeoutMs, long maxMessage, Opening opening, long started)
        throws IOException, OpenFailure {
        Proxies.Proxy proxy = target.proxy();
        String host = proxy == null ? target.host() : proxy.host();
        int port = proxy == null ? target.port() : proxy.port();
        Thread.ofVirtual().start(() -> {
            try {
                opening.lookup.complete(target.opener().resolve(host));
            } catch (UnknownHostException | RuntimeException error) {
                opening.lookup.completeExceptionally(error);
            }
        });
        InetAddress[] addresses;
        try {
            addresses = opening.lookup.get();
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new OpenFailure("interrupted while opening the channel", error, false, 0, 0);
        } catch (ExecutionException error) {
            if (error.getCause() instanceof TimeoutException) throw new OpenFailure("the time ran out", error, true, 0, 0);
            if (error.getCause() instanceof IOException failure) throw failure;
            throw new IllegalStateException("The lookup of " + host + " failed", error.getCause());
        }
        SocketChannel channel = null;
        IOException last = null;
        for (InetAddress address : addresses) {
            long left = timeoutMs - (System.nanoTime() - started) / 1_000_000;
            if (left <= 0 || opening.expired) throw new OpenFailure("the time ran out", null, true, 0, 0);
            SocketChannel attempt = SocketChannel.open();
            opening.channels.add(attempt);
            try {
                target.opener().connect(attempt, new InetSocketAddress(address, port), (int) Math.min(left, Integer.MAX_VALUE));
                attempt.socket().setTcpNoDelay(true);
                channel = attempt;
                break;
            } catch (IOException error) {
                close(attempt);
                if (opening.expired) throw new OpenFailure("the time ran out", error, true, 0, 0);
                last = error;
            }
        }
        if (channel == null) throw last;
        Socket socket = channel.socket();
        if (proxy != null) {
            if (proxy.tls()) socket = handshake(target.tls(), socket, proxy.host(), proxy.port());
            tunnel(socket, target, proxy, opening);
        }
        if (target.secure()) socket = handshake(target.tls(), socket, target.host(), target.port());
        InputStream in = new BufferedInputStream(socket.getInputStream(), 64 * 1024);
        OutputStream out = socket.getOutputStream();
        String key = Base64.getEncoder().encodeToString(randomBytes(16));
        String hostHeader = hostOf(target.host(), target.port(), target.secure(), false);
        StringBuilder request = new StringBuilder("GET " + target.path() + " HTTP/1.1\r\nHost: " + hostHeader
            + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n");
        for (Map.Entry<String, String> header : target.headers().entrySet()) {
            request.append(header.getKey()).append(": ").append(header.getValue()).append("\r\n");
        }
        request.append("User-Agent: jeff-sdk-java\r\n\r\n");
        out.write(request.toString().getBytes(StandardCharsets.ISO_8859_1));
        out.flush();
        Head answer = readHead(in, "the server");
        if (answer.status != 101) {
            throw new OpenFailure("HTTP status " + answer.status, null, false, answer.status, 0);
        }
        String upgrade = answer.fields.get("upgrade");
        if (upgrade == null || !upgrade.strip().equalsIgnoreCase("websocket")) {
            throw new OpenFailure("InvalidUpgrade: invalid Upgrade header: " + PyFormat.repr(upgrade), null, false, 0, 0);
        }
        String connection = answer.fields.get("connection");
        boolean upgrades = false;
        if (connection != null) for (String token : connection.split(",")) if (token.strip().equalsIgnoreCase("upgrade")) upgrades = true;
        if (!upgrades) throw new OpenFailure("InvalidUpgrade: invalid Connection header: " + PyFormat.repr(connection), null, false, 0, 0);
        String accept;
        try {
            accept = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-1")
                .digest((key + GUID).getBytes(StandardCharsets.ISO_8859_1)));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-1, which every JDK provides, is missing", error);
        }
        if (!accept.equals(answer.fields.get("sec-websocket-accept"))) {
            throw new OpenFailure("InvalidHeaderValue: invalid Sec-WebSocket-Accept header", null, false, 0, 0);
        }
        for (String offered : List.of("sec-websocket-extensions", "sec-websocket-protocol")) {
            if (answer.fields.containsKey(offered)) {
                throw new OpenFailure("NegotiationError: the server chose a " + offered + " this client did not offer", null, false, 0, 0);
            }
        }
        if (!opening.finish()) throw new OpenFailure("the time ran out", null, true, 0, 0);
        return new WebSocketConnection(channel, in, out, maxMessage);
    }

    /** websockets' build_host: an IPv6 address in brackets, the port when it is not the scheme's default (or always). */
    static String hostOf(String host, int port, boolean secure, boolean alwaysPort) {
        String shown = Proxies.isIpv6(host) ? "[" + host + "]" : host;
        return alwaysPort || port != (secure ? 443 : 80) ? shown + ":" + port : shown;
    }

    private static Socket handshake(Supplier<SSLSocketFactory> tls, Socket socket, String host, int port) throws IOException {
        SSLSocket secure = (SSLSocket) tls.get().createSocket(socket, host, port, true);
        SSLParameters parameters = secure.getSSLParameters();
        parameters.setEndpointIdentificationAlgorithm("HTTPS");
        secure.setSSLParameters(parameters);
        secure.startHandshake();
        return secure;
    }

    /** websockets' CONNECT (prepare_connect_request): the target always with its port, Host as build_host writes it,
     * User-Agent, then the proxy's credentials; an answer outside 2xx is refused with its status. The answer is read a
     * byte at a time, so nothing the server sends through the tunnel is taken. */
    private static void tunnel(Socket socket, Target target, Proxies.Proxy proxy, Opening opening) throws IOException, OpenFailure {
        OutputStream out = socket.getOutputStream();
        StringBuilder request = new StringBuilder("CONNECT " + hostOf(target.host(), target.port(), target.secure(), true)
            + " HTTP/1.1\r\nHost: " + hostOf(target.host(), target.port(), target.secure(), false) + "\r\nUser-Agent: jeff-sdk-java\r\n");
        if (proxy.authorization() != null) request.append("Proxy-Authorization: ").append(proxy.authorization()).append("\r\n");
        request.append("\r\n");
        out.write(request.toString().getBytes(StandardCharsets.ISO_8859_1));
        out.flush();
        Head answer;
        try {
            answer = readHead(socket.getInputStream(), "the proxy");
        } catch (OpenFailure unreadable) {
            if (opening.expired) throw new OpenFailure("the time ran out", unreadable, true, 0, 0);
            throw new OpenFailure("InvalidProxyMessage: did not receive a valid HTTP response from proxy", unreadable, false, 0, 0);
        }
        if (answer.status < 200 || answer.status > 299) {
            throw new OpenFailure("InvalidProxyStatus: proxy rejected connection: HTTP " + answer.status, null, false, 0, answer.status);
        }
    }

    private record Head(int status, Map<String, String> fields) {}

    /** An HTTP/1.1 answer's head: its status and its fields (names in lower case; a repeated field joined by ", "). */
    private static Head readHead(InputStream in, String who) throws IOException, OpenFailure {
        List<String> lines = new ArrayList<>();
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        int used = 0;
        while (true) {
            int next = in.read();
            if (next < 0) {
                throw new OpenFailure("the connection closed before " + who + " answered (no complete HTTP head)", new EOFException(), false, 0, 0);
            }
            if (++used > MAX_HEAD) throw new OpenFailure("the head " + who + " sent is longer than " + MAX_HEAD + " bytes", null, false, 0, 0);
            if (next != '\n') {
                line.write(next);
                continue;
            }
            String text = line.toString(StandardCharsets.ISO_8859_1);
            line.reset();
            if (text.endsWith("\r")) text = text.substring(0, text.length() - 1);
            if (text.isEmpty()) break;
            lines.add(text);
        }
        java.util.regex.Matcher status = java.util.regex.Pattern.compile("HTTP/1\\.[01] ([0-9]{3})(?: .*)?").matcher(lines.isEmpty() ? "" : lines.get(0));
        if (!status.matches()) {
            throw new OpenFailure("the answer " + who + " sent is not HTTP: " + PyFormat.repr(lines.isEmpty() ? "" : lines.get(0)), null, false, 0, 0);
        }
        Map<String, String> fields = new LinkedHashMap<>();
        for (String field : lines.subList(1, lines.size())) {
            int colon = field.indexOf(':');
            if (colon <= 0) throw new OpenFailure("the answer " + who + " sent has a malformed header line: " + PyFormat.repr(field), null, false, 0, 0);
            String name = field.substring(0, colon).strip().toLowerCase(Locale.ROOT);
            String value = field.substring(colon + 1).strip();
            fields.merge(name, value, (before, added) -> before + ", " + added);
        }
        return new Head(Integer.parseInt(status.group(1)), fields);
    }

    private static byte[] randomBytes(int count) {
        byte[] bytes = new byte[count];
        RANDOM.nextBytes(bytes);
        return bytes;
    }

    static byte[] randomPayload() {
        return randomBytes(4);
    }

    // ---- reading (one reader)

    private int readByte() throws IOException {
        int value = in.read();
        if (value < 0) throw new EOFException("the connection ended in the middle of a WebSocket frame");
        return value;
    }

    private byte[] readExactly(int count) throws IOException {
        byte[] data = in.readNBytes(count);
        if (data.length != count) throw new EOFException("the connection ended in the middle of a WebSocket frame");
        return data;
    }

    /** The data message being gathered from its fragments (type -1: none), kept across read() calls so a control frame
     * between two fragments (RFC 6455, section 5.4) is returned without losing the message. One reader only. */
    private int type = -1;
    private final List<byte[]> parts = new ArrayList<>();
    private long size;

    /** The close codes a server may send, as websockets' Close.check allows them: 1000-1003, 1007-1014, 3000-4999. */
    static boolean validCloseCode(int code) {
        return (code >= 1000 && code <= 1003) || (code >= 1007 && code <= 1014) || (code >= 3000 && code <= 4999);
    }

    /** A close frame's payload checked as websockets' Close.parse checks it: empty (no status), or a valid code and a
     * reason in strict UTF-8. */
    private static void checkClose(byte[] data) throws ProtocolViolation {
        if (data.length == 0) return;
        if (data.length == 1) throw new ProtocolViolation("a close frame of 1 byte (too short for a status code)");
        int code = ((data[0] & 0xff) << 8) | (data[1] & 0xff);
        if (!validCloseCode(code)) throw new ProtocolViolation("a close frame with the invalid status code " + code);
        try {
            StandardCharsets.UTF_8.newDecoder().decode(java.nio.ByteBuffer.wrap(data, 2, data.length - 2));
        } catch (java.nio.charset.CharacterCodingException error) {
            throw new ProtocolViolation("a close frame whose reason is not valid UTF-8", 1007);
        }
    }

    /** The next whole message or control frame; null when the connection ended cleanly between frames. A ping or pong
     * may come between the fragments of a message; a close may not (websockets refuses it as "incomplete fragmented
     * message"). */
    Incoming read() throws IOException {
        while (true) {
            int first = in.read();
            if (first < 0) {
                if (type >= 0) throw new EOFException("the connection ended in the middle of a WebSocket message");
                return null;
            }
            int second = readByte();
            boolean fin = (first & 0x80) != 0;
            if ((first & 0x70) != 0) throw new ProtocolViolation("a frame with reserved bits set (no extension was agreed)");
            int opcode = first & 0x0f;
            if ((second & 0x80) != 0) throw new ProtocolViolation("a masked frame (a server's frames are never masked)");
            long length = second & 0x7f;
            if (length == 126) {
                length = ((long) readByte() << 8) | readByte();
            } else if (length == 127) {
                length = 0;
                for (int index = 0; index < 8; index++) length = (length << 8) | readByte();
                if (length < 0) throw new ProtocolViolation("a frame whose 64-bit length has its top bit set");
            }
            if (opcode >= 8) {
                if (opcode != CLOSE && opcode != PING && opcode != PONG) throw new ProtocolViolation("a frame with the unknown opcode " + opcode);
                if (!fin) throw new ProtocolViolation("a fragmented control frame");
                if (length > 125) throw new ProtocolViolation("a control frame of " + length + " bytes (at most 125)");
                byte[] data = readExactly((int) length);
                if (opcode == CLOSE) {
                    if (type >= 0) throw new ProtocolViolation("a close frame in the middle of a fragmented message");
                    checkClose(data);
                }
                return new Incoming(opcode, data);
            }
            if (opcode != CONTINUATION && opcode != TEXT && opcode != BINARY) {
                throw new ProtocolViolation("a frame with the unknown opcode " + opcode);
            }
            if (opcode == CONTINUATION && type < 0) throw new ProtocolViolation("a continuation frame with no message to continue");
            if (opcode != CONTINUATION && type >= 0) throw new ProtocolViolation("a new message before the last one's final fragment");
            if (opcode != CONTINUATION) type = opcode;
            if (size + length > maxMessage) throw new TooLarge(size + length);
            parts.add(readExactly((int) length));
            size += length;
            if (fin) {
                byte[] message = parts.size() == 1 ? parts.get(0) : new byte[(int) size];
                if (parts.size() > 1) {
                    int at = 0;
                    for (byte[] part : parts) {
                        System.arraycopy(part, 0, message, at, part.length);
                        at += part.length;
                    }
                }
                int whole = type;
                type = -1;
                parts.clear();
                size = 0;
                return new Incoming(whole, message);
            }
        }
    }

    // ---- writing (one writer)

    /** Writes one unfragmented frame, masked, in pieces of at most SEND_CHUNK bytes; `progressed` runs after each piece
     * the socket accepted. */
    void write(int opcode, byte[] payload, Runnable progressed) throws IOException {
        byte[] mask = randomBytes(4);
        int length = payload.length;
        byte[] header;
        if (length < 126) {
            header = new byte[] {(byte) (0x80 | opcode), (byte) (0x80 | length)};
        } else if (length < 65536) {
            header = new byte[] {(byte) (0x80 | opcode), (byte) (0x80 | 126), (byte) (length >>> 8), (byte) length};
        } else {
            header = new byte[10];
            header[0] = (byte) (0x80 | opcode);
            header[1] = (byte) (0x80 | 127);
            for (int index = 0; index < 8; index++) header[2 + index] = (byte) ((long) length >>> (56 - 8 * index));
        }
        int filled = 0;
        System.arraycopy(header, 0, piece, 0, header.length);
        filled += header.length;
        System.arraycopy(mask, 0, piece, filled, 4);
        filled += 4;
        int offset = 0;
        while (true) {
            int take = Math.min(SEND_CHUNK - filled, length - offset);
            for (int index = 0; index < take; index++) piece[filled + index] = (byte) (payload[offset + index] ^ mask[(offset + index) & 3]);
            filled += take;
            offset += take;
            out.write(piece, 0, filled);
            out.flush();
            progressed.run();
            filled = 0;
            if (offset == length) return;
        }
    }

    /** Ends the connection now, from any thread: a read or write blocked on it fails at once. The raw channel is
     * closed (not a TLS socket, whose close could wait for a blocked write). */
    void abort() {
        close(channel);
    }

    boolean isOpen() {
        return channel.isOpen();
    }
}
