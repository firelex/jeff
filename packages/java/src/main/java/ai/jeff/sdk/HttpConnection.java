package ai.jeff.sdk;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.math.BigInteger;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.nio.ByteBuffer;
import java.nio.channels.AsynchronousCloseException;
import java.nio.channels.ClosedByInterruptException;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.function.Supplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/** One HTTP/1.1 connection to a Jeff server (directly, or through a proxy: forwarding for http://, a CONNECT tunnel
 * for https://, as httpcore's HTTPProxy does), read as httpcore and h11 read one in the Python SDK: an answer's head of
 * at most 100 KiB (httpcore's limit), informational (1xx) answers passed over, the body framed by chunked
 * transfer-encoding, by Content-Length, or by the end of the connection, every line checked with h11's grammar. A
 * request is written once and never sent again. Failures are IOExceptions; one the server's bytes cause is a
 * RemoteProtocolError, named as httpx names it. */
final class HttpConnection {
    /** The largest answer head, or trailer section, read (httpcore's h11 max_incomplete_event_size). */
    static final int MAX_HEAD = 100 * 1024;
    /** The largest answer body held in memory, as sent and once its Content-Encoding is undone (HttpBody): 64 MiB (the
     * channel's frame limit, the same in every Jeff SDK), far above any answer a Jeff server gives (a /v1/decide answer of 1,000 calls is a few MiB). httpx has no limit; this
     * one turns a broken or hostile server into a named error instead of exhausting the heap. */
    static final long MAX_BODY = 64L * 1024 * 1024;

    /** An answer that breaks HTTP/1.1, or that ends too soon (httpx's RemoteProtocolError, named so its messages read
     * as Python's do). */
    static class RemoteProtocolError extends IOException {
        RemoteProtocolError(String message) {
            super(message);
        }
    }

    /** An answer's body, as sent, longer than MAX_BODY: what is past the limit is not read. */
    static final class BodyTooLarge extends RemoteProtocolError {
        BodyTooLarge() {
            super("the answer's body is larger than " + MAX_BODY + " bytes, more than this SDK holds");
        }
    }

    /** A proxy that refused to open a tunnel (httpx's ProxyError, named so its message reads as Python's does). */
    static final class ProxyError extends IOException {
        ProxyError(String message) {
            super(message);
        }
    }

    /** The host name was not resolved within the connect timeout. */
    static final class LookupTimeout extends IOException {
        final String host;

        LookupTimeout(String host) {
            super("the address of " + host + " was not found in time");
            this.host = host;
        }
    }

    /** How a connection finds and reaches an address (tests put a slow resolver or connect in). */
    interface Opener {
        default InetAddress[] resolve(String host) throws UnknownHostException {
            return InetAddress.getAllByName(host);
        }

        /** Connects `channel` (already registered to be closed when the request is aborted) within timeoutMs. */
        default void connect(SocketChannel channel, InetSocketAddress address, int timeoutMs) throws IOException {
            channel.socket().connect(address, timeoutMs);
        }
    }

    static final Opener DIRECT = new Opener() {};

    /** A whole answer: its status, its header fields (names in lower case, bytes as sent) and its body as sent (any
     * Content-Encoding not yet undone). */
    record Answer(int status, List<byte[][]> headers, byte[] body) {}

    private static final Pattern STATUS_LINE = Pattern.compile("HTTP/([0-9])\\.([0-9]) ([0-9]{3})(?: ((?:[ \\t]|[^\\x00\\s])*))?");
    private static final Pattern HEADER_FIELD = Pattern.compile(
        "([-!#$%&'*+.^_`|~0-9a-zA-Z]+):[ \\t]*((?:[^\\x00\\s]+(?:[ \\t]+[^\\x00\\s]+)*)?)[ \\t]*");
    private static final Pattern CHUNK_HEADER = Pattern.compile("([0-9A-Fa-f]{1,20})(?:;[^\\n]*)?[ \\t]*\\r\\n");

    private final SocketChannel channel;
    private final BufferedInputStream in;
    private final OutputStream out;
    private volatile PhaseTimer timer;
    private long idleSince;

    private HttpConnection(SocketChannel channel, Socket socket, int buffer) throws IOException {
        this.channel = channel;
        this.in = new BufferedInputStream(new Timed(socket.getInputStream()), buffer);
        this.out = new BufferedOutputStream(socket.getOutputStream(), 16 * 1024);
    }

    /** The JDK's default TLS sockets (its trust store, so the server's certificate and name are checked). */
    static SSLSocketFactory defaultTls() {
        return (SSLSocketFactory) SSLSocketFactory.getDefault();
    }

    /** Opens a connection under `timer`, which the caller started in its connect phase: the host's addresses looked up
     * (stopped when the timer runs out or the transport closes), then each address tried with what is left of the
     * timeout, then TLS with the certificate checked for the host name. Through a proxy: the connection goes to the
     * proxy (TLS first for an https:// proxy); for an https:// server a CONNECT tunnel is opened (its answer awaited
     * as an answer's headers are) and TLS to the server runs through it, timed anew as connecting. */
    static HttpConnection open(ServerUrl url, Proxies.Proxy proxy, long timeoutMs, PhaseTimer timer, Opener opener,
                               Supplier<SSLSocketFactory> tls) throws IOException {
        long started = System.nanoTime();
        String host = proxy == null ? url.host() : proxy.host();
        int port = proxy == null ? url.port() : proxy.port();
        InetAddress[] addresses = lookup(host, timeoutMs, timer, opener);
        SocketChannel channel = null;
        IOException last = null;
        for (InetAddress address : addresses) {
            long left = timeoutMs - (System.nanoTime() - started) / 1_000_000;
            if (left <= 0) throw new SocketTimeoutException("Connect timed out");
            SocketChannel attempt = SocketChannel.open();
            timer.onAbort(() -> close(attempt));
            try {
                opener.connect(attempt, new InetSocketAddress(address, port), (int) Math.min(left, Integer.MAX_VALUE));
                attempt.socket().setTcpNoDelay(true);
                channel = attempt;
                break;
            } catch (IOException error) {
                close(attempt);
                if (timer.fired() != null || error instanceof AsynchronousCloseException) throw error;
                last = error;
            }
        }
        if (channel == null) throw last;
        SocketChannel opened = channel;
        try {
            Socket socket = opened.socket();
            if (proxy != null && proxy.tls()) socket = handshake(tls, socket, proxy.host(), proxy.port());
            if (proxy != null && url.tls()) {
                new HttpConnection(opened, socket, 1).tunnel(url, proxy, timer);
                timer.start(PhaseTimer.Phase.CONNECT);
            }
            if (url.tls()) socket = handshake(tls, socket, url.host(), url.port());
            return new HttpConnection(opened, socket, 16 * 1024);
        } catch (IOException | RuntimeException | Error error) {
            close(opened);
            throw error;
        }
    }

    /** The host's addresses, looked up on a thread of its own so that the timer running out, or the transport closing,
     * ends the wait at once (the lookup itself cannot be stopped, and ends on its own). */
    private static InetAddress[] lookup(String host, long timeoutMs, PhaseTimer timer, Opener opener) throws IOException {
        CompletableFuture<InetAddress[]> found = new CompletableFuture<>();
        timer.onAbort(() -> found.completeExceptionally(new AsynchronousCloseException()));
        Thread.ofVirtual().start(() -> {
            try {
                found.complete(opener.resolve(host));
            } catch (UnknownHostException | RuntimeException error) {
                found.completeExceptionally(error);
            }
        });
        try {
            return found.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException late) {
            throw new LookupTimeout(host);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new ClosedByInterruptException();
        } catch (ExecutionException failed) {
            if (failed.getCause() instanceof AsynchronousCloseException stopped) {
                // the timer ran out (or the transport closed) while the name was still being looked up
                if (timer.fired() != null) throw new LookupTimeout(host);
                throw stopped;
            }
            if (failed.getCause() instanceof IOException error) throw error;
            if (failed.getCause() instanceof RuntimeException error) throw error;
            throw new IllegalStateException("The lookup of " + host + " failed", failed.getCause());
        }
    }

    private static Socket handshake(Supplier<SSLSocketFactory> tls, Socket socket, String host, int port) throws IOException {
        SSLSocket secure = (SSLSocket) tls.get().createSocket(socket, host, port, true);
        SSLParameters parameters = secure.getSSLParameters();
        parameters.setEndpointIdentificationAlgorithm("HTTPS");
        parameters.setApplicationProtocols(new String[] {"http/1.1"});
        secure.setSSLParameters(parameters);
        secure.startHandshake();
        return secure;
    }

    /** Asks the proxy for a tunnel to the server, as httpcore's TunnelHTTPConnection does (Host and Accept, then the
     * proxy's credentials); an answer outside 2xx is a ProxyError "<status> <reason>". The answer is read a byte at a
     * time, so nothing the server sends through the tunnel is taken. */
    private void tunnel(ServerUrl url, Proxies.Proxy proxy, PhaseTimer timer) throws IOException {
        this.timer = timer;
        String target = (url.host().contains(":") ? "[" + url.host() + "]" : url.host()) + ":" + url.port();
        StringBuilder request = new StringBuilder("CONNECT " + target + " HTTP/1.1\r\nHost: " + target + "\r\nAccept: */*\r\n");
        if (proxy.authorization() != null) request.append("Proxy-Authorization: ").append(proxy.authorization()).append("\r\n");
        request.append("\r\n");
        timer.start(PhaseTimer.Phase.HEADERS);
        out.write(request.toString().getBytes(StandardCharsets.ISO_8859_1));
        out.flush();
        Head answer = readHead();
        while (answer.status >= 100 && answer.status < 200) answer = readHead();
        if (answer.status < 200 || answer.status > 299) {
            throw new ProxyError(answer.status + " " + answer.reason.replaceAll("[^\\x00-\\x7f]", ""));
        }
        this.timer = null;
    }

    /** Closes the connection now, from any thread: a read or write blocked on it fails at once. The raw channel is
     * closed (not the TLS socket, whose close could wait for a blocked write). */
    void abort() {
        close(channel);
    }

    private static void close(SocketChannel channel) {
        try {
            channel.close();
        } catch (IOException error) {
            // SocketChannel.close marks the channel closed before releasing it, so it is closed whatever this says;
            // there is nothing left to do with the connection
        }
    }

    boolean isOpen() {
        return channel.isOpen();
    }

    long idleSince() {
        return idleSince;
    }

    void markIdle() {
        idleSince = System.nanoTime();
    }

    /** Whether an idle connection can take another request: open, and with nothing to read (a server that closed it, or
     * sent bytes nobody asked for, makes it readable), as httpcore checks a pooled connection before reusing it. */
    boolean usable() {
        if (!channel.isOpen()) return false;
        try {
            if (in.available() > 0) return false;
            channel.configureBlocking(false);
            int read;
            try {
                read = channel.read(ByteBuffer.allocate(1));
            } finally {
                channel.configureBlocking(true);
            }
            return read == 0;
        } catch (IOException error) {
            // a connection that fails to be read (reset, say) cannot take a request: it is closed and not used
            abort();
            return false;
        }
    }

    /** Writes the request (once) and reads its whole answer, under `timer`: the headers phase starts with the first
     * byte written, the body phase when the head has been read. `reusable[0]` tells whether the connection can take
     * another request afterwards. */
    Answer exchange(byte[] request, PhaseTimer timer, boolean[] reusable) throws IOException {
        this.timer = timer;
        timer.onAbort(this::abort);
        timer.start(PhaseTimer.Phase.HEADERS);
        out.write(request);
        out.flush();
        Head answer = readHead();
        while (answer.status >= 100 && answer.status < 200) answer = readHead();
        timer.start(PhaseTimer.Phase.BODY);
        boolean keepAlive = answer.major > 1 || (answer.major == 1 && answer.minor >= 1);
        for (String token : tokens(answer.all("connection"))) if (token.equals("close")) keepAlive = false;
        List<String> transfer = answer.all("transfer-encoding");
        BigInteger length = contentLength(answer.all("content-length"));
        byte[] body;
        if (answer.status == 204 || answer.status == 304) {
            body = new byte[0];
        } else if (!transfer.isEmpty()) {
            if (transfer.size() > 1) throw new RemoteProtocolError("multiple Transfer-Encoding headers");
            if (!transfer.get(0).toLowerCase(Locale.ROOT).equals("chunked")) {
                throw new RemoteProtocolError("Only Transfer-Encoding: chunked is supported");
            }
            body = readChunked();
            // an answer framed both ways must not be followed by another on the same connection (RFC 9112, 6.3)
            if (length != null) keepAlive = false;
        } else if (length != null) {
            body = readLength(length);
        } else {
            body = readToEnd();
            keepAlive = false;
        }
        reusable[0] = keepAlive;
        this.timer = null;
        return new Answer(answer.status, answer.headers, body);
    }

    private record Head(int major, int minor, int status, String reason, List<byte[][]> headers) {
        List<String> all(String name) {
            List<String> values = new ArrayList<>();
            for (byte[][] field : headers) {
                if (new String(field[0], StandardCharsets.ISO_8859_1).equals(name)) {
                    values.add(new String(field[1], StandardCharsets.ISO_8859_1));
                }
            }
            return values;
        }
    }

    private static List<String> tokens(List<String> values) {
        List<String> found = new ArrayList<>();
        for (String value : values) for (String token : value.split(",")) found.add(token.strip().toLowerCase(Locale.ROOT));
        return found;
    }

    /** h11's Content-Length: in each field, the comma-separated parts all the same, digits, at most 20 of them; every
     * field the same. */
    private static BigInteger contentLength(List<String> values) throws RemoteProtocolError {
        String seen = null;
        for (String value : values) {
            List<String> parts = new ArrayList<>();
            for (String part : value.split(",", -1)) if (!parts.contains(part.strip())) parts.add(part.strip());
            if (parts.size() != 1) throw new RemoteProtocolError("conflicting Content-Length headers");
            String length = parts.get(0);
            if (!length.matches("[0-9]+") || length.length() > 20) throw new RemoteProtocolError("bad Content-Length");
            if (seen != null && !seen.equals(length)) throw new RemoteProtocolError("conflicting Content-Length headers");
            seen = length;
        }
        return seen == null ? null : new BigInteger(seen);
    }

    private static String shown(byte[] line) {
        return "bytearray(" + PyFormat.repr(line) + ")";
    }

    private static String latin1(byte[] bytes) {
        return new String(bytes, StandardCharsets.ISO_8859_1);
    }

    /** One line of a head, without its line ending (\r\n, or \n alone, as h11 splits a head into lines); null when the
     * stream ends before the line does. `used[0]` counts the bytes against MAX_HEAD. */
    private byte[] readLine(int[] used) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        while (true) {
            int next = in.read();
            if (next < 0) return null;
            if (++used[0] > MAX_HEAD) throw new RemoteProtocolError("Receive buffer too long");
            if (next == '\n') break;
            line.write(next);
        }
        byte[] bytes = line.toByteArray();
        if (bytes.length > 0 && bytes[bytes.length - 1] == '\r') bytes = Arrays.copyOf(bytes, bytes.length - 1);
        return bytes;
    }

    /** httpcore's error for a connection that ends before an answer's head is complete. */
    private static RemoteProtocolError disconnected() {
        return new RemoteProtocolError("Server disconnected without sending a response.");
    }

    /** The header lines up to the blank line, with h11's obsolete line folding (a line starting with spaces or tabs
     * continues the one before, joined by one space) and each field checked with h11's grammar (a value holds no
     * control character but tab, and no NUL). */
    private List<byte[][]> readFields(int[] used, boolean trailers) throws IOException {
        List<byte[]> lines = new ArrayList<>();
        while (true) {
            byte[] line = readLine(used);
            if (line == null) {
                if (trailers) throw incompleteChunked();
                throw disconnected();
            }
            if (line.length == 0) break;
            int fold = 0;
            while (fold < line.length && (line[fold] == ' ' || line[fold] == '\t')) fold++;
            if (fold > 0) {
                if (lines.isEmpty()) throw new RemoteProtocolError("continuation line at start of headers");
                byte[] previous = lines.remove(lines.size() - 1);
                byte[] joined = Arrays.copyOf(previous, previous.length + 1 + line.length - fold);
                joined[previous.length] = ' ';
                System.arraycopy(line, fold, joined, previous.length + 1, line.length - fold);
                lines.add(joined);
            } else {
                lines.add(line);
            }
        }
        List<byte[][]> fields = new ArrayList<>();
        for (byte[] line : lines) {
            Matcher field = HEADER_FIELD.matcher(latin1(line));
            if (!field.matches()) throw new RemoteProtocolError("illegal header line: " + shown(line));
            fields.add(new byte[][] {field.group(1).toLowerCase(Locale.ROOT).getBytes(StandardCharsets.ISO_8859_1),
                field.group(2).getBytes(StandardCharsets.ISO_8859_1)});
        }
        return fields;
    }

    /** One answer head (each counted on its own against MAX_HEAD, as h11 counts each event). */
    private Head readHead() throws IOException {
        int[] used = new int[1];
        byte[] statusLine = readLine(used);
        if (statusLine == null) throw disconnected();
        Matcher status = STATUS_LINE.matcher(latin1(statusLine));
        if (!status.matches()) throw new RemoteProtocolError("illegal status line: " + shown(statusLine));
        List<byte[][]> headers = readFields(used, false);
        return new Head(Integer.parseInt(status.group(1)), Integer.parseInt(status.group(2)), Integer.parseInt(status.group(3)),
            status.group(4) == null ? "" : status.group(4), headers);
    }

    /** Reads up to `count` bytes into `body`; false when the connection ended first. */
    private boolean readInto(ByteArrayOutputStream body, BigInteger count) throws IOException {
        if (count.add(BigInteger.valueOf(body.size())).compareTo(BigInteger.valueOf(MAX_BODY)) > 0) {
            // more than this SDK holds: read on to the limit (or the connection's end, as h11 would), then report it
            byte[] buffer = new byte[64 * 1024];
            long room = MAX_BODY - body.size();
            while (room > 0) {
                int read = in.read(buffer, 0, (int) Math.min(buffer.length, room));
                if (read < 0) return false;
                body.write(buffer, 0, read);
                room -= read;
            }
            throw new BodyTooLarge();
        }
        long left = count.longValueExact();
        byte[] buffer = new byte[(int) Math.min(64 * 1024, Math.max(left, 1))];
        while (left > 0) {
            int read = in.read(buffer, 0, (int) Math.min(buffer.length, left));
            if (read < 0) return false;
            body.write(buffer, 0, read);
            left -= read;
        }
        return true;
    }

    /** A body framed by the end of the connection, at most MAX_BODY bytes. */
    private byte[] readToEnd() throws IOException {
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        byte[] buffer = new byte[64 * 1024];
        while (true) {
            int read = in.read(buffer);
            if (read < 0) return body.toByteArray();
            if (body.size() + (long) read > MAX_BODY) {
                throw new BodyTooLarge();
            }
            body.write(buffer, 0, read);
        }
    }

    private byte[] readLength(BigInteger length) throws IOException {
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        if (!readInto(body, length)) {
            throw new RemoteProtocolError("peer closed connection without sending complete message body (received "
                + body.size() + " bytes, expected " + length + ")");
        }
        return body.toByteArray();
    }

    /** A chunk-size line, with its line ending (h11 takes a line only up to \r\n, and shows it whole). */
    private byte[] readChunkHeader(int[] used) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        int previous = -1;
        while (true) {
            int next = in.read();
            if (next < 0) return null;
            if (++used[0] > MAX_HEAD) throw new RemoteProtocolError("Receive buffer too long");
            line.write(next);
            if (previous == '\r' && next == '\n') return line.toByteArray();
            previous = next;
        }
    }

    private byte[] readChunked() throws IOException {
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        while (true) {
            byte[] header = readChunkHeader(new int[1]);
            if (header == null) throw incompleteChunked();
            Matcher size = CHUNK_HEADER.matcher(latin1(header));
            if (!size.matches()) throw new RemoteProtocolError("illegal chunk header: " + shown(header));
            BigInteger count = new BigInteger(size.group(1), 16);
            if (count.signum() == 0) break;
            if (!readInto(body, count)) throw incompleteChunked();
            byte[] footer = in.readNBytes(2);
            if (footer.length > 0 && (footer[0] != '\r' || footer.length > 1 && footer[1] != '\n')) {
                throw new RemoteProtocolError("malformed chunk footer: " + shown(footer) + " (expected b'\\r\\n')");
            }
            if (footer.length < 2) throw incompleteChunked();
        }
        // the trailer fields, up to the empty line that ends the answer: one section, bounded as a head is
        readFields(new int[1], true);
        return body.toByteArray();
    }

    private static RemoteProtocolError incompleteChunked() {
        return new RemoteProtocolError("peer closed connection without sending complete message body (incomplete chunked read)");
    }

    /** Counts arriving answer bytes as progress for the request's timer (so each wait for more body is timed anew). */
    private final class Timed extends FilterInputStream {
        Timed(InputStream source) {
            super(source);
        }

        @Override
        public int read() throws IOException {
            int value = super.read();
            if (value >= 0) progressed();
            return value;
        }

        @Override
        public int read(byte[] buffer, int offset, int length) throws IOException {
            int count = super.read(buffer, offset, length);
            if (count > 0) progressed();
            return count;
        }

        private void progressed() {
            PhaseTimer current = timer;
            if (current != null) current.progress();
        }
    }
}
