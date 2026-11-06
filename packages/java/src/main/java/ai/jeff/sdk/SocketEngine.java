package ai.jeff.sdk;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.ConnectException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.SocketChannel;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.ReentrantLock;

/** An {@link Engine} over the engine socket (contract/socket.md; port of jeff/socket_engine.py and the Node SDK's
 * socket-engine.ts). {@link Jeff#local} makes one; make your own with {@link #connect} to give it to
 * {@link Jeff#create}: one connection, requests multiplexed by id and sent as they come, so the engine batches
 * concurrent callers. After the engine closes the connection, the next call makes one fresh connection (at most one
 * attempt per second); a call in flight when it closed fails and is never resent. It never starts an engine.
 *
 * <p>Each connection has two daemon threads of its own, and no caller's thread ever reads or writes its socket: the
 * reader hands each reply to the call waiting for its id, and the writer writes the frames queued in call order, each
 * one whole. So a caller that times out or is interrupted only stops waiting (a frame already being written is
 * finished, one not yet started is never sent), an interrupt never closes the socket under the other callers, and
 * {@link #close()} unblocks a write stuck on an engine that stopped reading. */
public final class SocketEngine implements Engine {
    /** The fewest milliseconds between two reconnect attempts; a call made sooner fails at once, naming that attempt. */
    public static final long RECONNECT_INTERVAL_MS = 1000;
    /** Replies to these ops carry the engine's whole installed list beside its version. */
    private static final Set<String> CARRY_ADAPTERS = Set.of("installed", "serve", "reload");
    /** How long close() waits for a connection's threads to end once its socket is closed. */
    private static final Duration THREAD_END = Duration.ofSeconds(5);

    /** The command that starts an engine listening at {@code path}. */
    static String startCommand(String path) {
        return "jeff runtime serve --socket " + path + " --adapter <adapter id> --batch-window-ms <milliseconds> "
            + "--max-batch-rows <rows> --queue-limit-rows <rows> --prepare-workers <threads>";
    }

    private enum Stage { HANDSHAKE, EARLY, READY }

    /** A frame waiting for the writer. A caller that stops waiting abandons it: it is then never written, unless the
     * writer has already started it, in which case it is written whole. */
    private static final class Outgoing {
        static final int QUEUED = 0;
        static final int WRITING = 1;
        static final int ABANDONED = 2;
        static final Outgoing STOP = new Outgoing(new byte[0]);

        final byte[] frame;
        final AtomicInteger state = new AtomicInteger(QUEUED);

        Outgoing(byte[] frame) {
            this.frame = frame;
        }

        void abandon() {
            state.compareAndSet(QUEUED, ABANDONED);
        }
    }

    /** One connection: "handshake" until the hello is read; "early" until it is the current connection, while frames
     * (and a stream failure) that came after the hello are kept; "ready" after. Stage, early and earlyFailure are
     * guarded by the engine's lock. */
    private static final class Connection {
        final SocketChannel channel;
        final CompletableFuture<Map<String, Object>> greeting = new CompletableFuture<>();
        final LinkedBlockingQueue<Outgoing> outgoing = new LinkedBlockingQueue<>();
        final AtomicInteger threadsEnded = new AtomicInteger();
        Stage stage = Stage.HANDSHAKE;
        final List<byte[]> early = new ArrayList<>();
        JeffError earlyFailure;
        Thread reader;
        Thread writer;

        Connection(SocketChannel channel) {
            this.channel = channel;
        }
    }

    private static final class Attempt {
        final long at;
        volatile JeffError.EngineError failure;

        Attempt(long at) {
            this.at = at;
        }
    }

    /** Nothing listens at the path: the socket's own error, which connect and reconnect each word. */
    private static final class DialFailure extends RuntimeException {
        DialFailure(String message, Throwable cause) {
            super(message, cause);
        }
    }

    /** The engine's adapter list as of {@code version}: as a set, and in the order the engine gave it. */
    private record Snapshot(long version, Set<String> installed, List<String> order) {
        static Snapshot of(long version, Object adapters) {
            List<String> order = List.copyOf(Values.strings(adapters));
            return new Snapshot(version, Set.copyOf(order), order);
        }
    }

    private record Base(BaseFormat format, String id, String version) {}

    private record Sent(long id, String op, CompletableFuture<Map<String, Object>> reply, Outgoing frame, long deadline,
        long timeoutMs, boolean reconnect) {}

    private final String path;
    private final long timeoutMs;
    /** Guards every field below that is not final or volatile. */
    private final ReentrantLock lock = new ReentrantLock();
    private final Map<Long, CompletableFuture<Map<String, Object>>> pending = new HashMap<>();
    private final AtomicLong nextId = new AtomicLong(1);
    /** Connections whose threads have not both ended, so close() can end and wait for every one. */
    private final Set<Connection> live = new HashSet<>();
    private Connection current;
    private JeffError closed;
    private volatile boolean closedByCaller;
    private long snapshotVersion = -1;
    private Set<String> snapshotInstalled = Set.of();
    private List<String> snapshotOrder = List.of();
    private Attempt lastAttempt;
    /** Completed when the reconnect attempt under way has ended, or by close(); every call that finds the connection
     * closed or being checked waits for it. */
    private CompletableFuture<Void> running;
    /** The thread of the reconnect attempt under way, which close() waits for. */
    private Thread attemptThread;
    /** The selector of a connect still waiting, which close() wakes. */
    private Selector dialing;
    private BaseFormat format;
    private String baseId;
    private String baseVersion;
    private volatile int reconnects;

    private SocketEngine(String path, long timeoutMs) {
        this.path = path;
        this.timeoutMs = timeoutMs;
    }

    /** Connects to the engine at {@code path}, exchanges hellos, and reads its installed list and base once. A failure
     * here is final: there is no reconnect while connecting. It never starts an engine.
     *
     * @param path the engine's Unix socket
     * @param timeoutMs how long to wait for each reply, in milliseconds (more than 0)
     * @return the engine
     * @throws JeffError.InvalidRequest when the timeout is not above 0
     * @throws JeffError.EngineError when nothing listens at the path (naming the command that starts an engine), or
     *     the engine's hello, installed list or health does not come within the timeout
     * @throws JeffError.ProtocolMismatch when the engine speaks another protocol version */
    public static SocketEngine connect(String path, long timeoutMs) {
        Jeff.checkTimeoutMs(timeoutMs);
        return connect(path, timeoutMs, false);
    }

    /** {@link #connect}, but null when nothing listens at the path: no socket file there, or a refused connection
     * (contract/local-engine.md, step 5). Any other failure is raised as connect raises it. */
    static SocketEngine connectIfListening(String path, long timeoutMs) {
        Jeff.checkTimeoutMs(timeoutMs);
        return connect(path, timeoutMs, true);
    }

    private static SocketEngine connect(String path, long timeoutMs, boolean nullWhenNothingListens) {
        SocketEngine engine = new SocketEngine(path, timeoutMs);
        try {
            engine.open();
        } catch (DialFailure failure) {
            if (nullWhenNothingListens
                && (failure.getCause() instanceof ConnectException || !Files.exists(Path.of(path), LinkOption.NOFOLLOW_LINKS))) {
                return null;
            }
            JeffError.EngineError error = new JeffError.EngineError("No engine is listening at " + path + ": " + failure.getMessage()
                + ". Start one with: " + startCommand(path));
            error.initCause(failure.getCause());
            throw error;
        }
        try {
            // no reconnect while connecting: a connection that closes now fails the connect, naming why
            engine.merge(engine.installedNow(timeoutMs, false));
            Base base = engine.baseNow(false);
            engine.format = base.format();
            engine.baseId = base.id();
            engine.baseVersion = base.version();
        } catch (RuntimeException | Error error) {
            try {
                engine.close();
            } catch (RuntimeException closing) {
                error.addSuppressed(closing);
            }
            throw error;
        }
        return engine;
    }

    /** The base's weight format, read once at connect.
     *
     * @return the format */
    @Override
    public BaseFormat baseFormat() {
        return format;
    }

    /** The base the engine serves, as adapter manifests name it, read once at connect.
     *
     * @return {@code [base id, version]} */
    public List<String> base() {
        return List.of(baseId, baseVersion);
    }

    /** The engine's {@code adapters_version} (it changes whenever the engine loads or drops an adapter).
     *
     * @return the version as of the last reply read from it; -1 from a reconnect until its first reply */
    public long adaptersVersion() {
        lock.lock();
        try {
            return snapshotVersion;
        } finally {
            lock.unlock();
        }
    }

    /** How many times a call has reconnected to the engine after its connection closed.
     *
     * @return the count */
    public int reconnects() {
        return reconnects;
    }

    /** The error a failure reply describes. The engine puts its adapters_version in every failure's details; that is
     * protocol information, not an attribute of the error, so it is left out. */
    private static JeffError rebuild(Map<String, Object> body) {
        Map<String, Object> wire = new LinkedHashMap<>(body);
        Map<String, Object> details = new LinkedHashMap<>();
        if (body.get("details") instanceof Map<?, ?> given) details.putAll(Values.map(given));
        details.remove("adapters_version");
        wire.put("details", details);
        return JeffError.fromWire(wire);
    }

    /** Opens a connection, exchanges hellos (bounded by timeoutMs) and makes it the connection every call uses. A
     * failure closes the new socket and leaves the current state as it was. */
    private void open() {
        SocketChannel channel;
        try {
            channel = SocketChannel.open(StandardProtocolFamily.UNIX);
        } catch (IOException error) {
            throw new UncheckedIOException("Opening a Unix domain socket failed", error);
        }
        try {
            dial(channel);
        } catch (RuntimeException | Error error) {
            try {
                channel.close();
            } catch (IOException closing) {
                error.addSuppressed(closing);
            }
            throw error;
        }
        Connection connection = new Connection(channel);
        lock.lock();
        try {
            if (closedByCaller) {
                end(connection);
                throw dead();
            }
            live.add(connection);
            connection.reader = Thread.ofPlatform().daemon(true).name("jeff-engine-reader " + path).unstarted(() -> read(connection));
            connection.writer = Thread.ofPlatform().daemon(true).name("jeff-engine-writer " + path).unstarted(() -> write(connection));
            connection.reader.start();
            connection.writer.start();
        } finally {
            lock.unlock();
        }
        try {
            connection.outgoing.add(new Outgoing(Protocol.encodeFrame(Protocol.hello(), null)));
            Map<String, Object> message = awaitGreeting(connection);
            if (Boolean.FALSE.equals(message.get("ok"))) throw rebuild(Values.map(message.get("error")));
            Protocol.checkHello(message);
        } catch (RuntimeException | Error error) {
            end(connection);
            throw error;
        }
        lock.lock();
        try {
            // checked under the lock close() sets it under: a connection opened while the caller closed the engine is
            // never made current
            if (closedByCaller) {
                end(connection);
                throw dead();
            }
            current = connection;
            snapshotVersion = -1; // a restarted engine counts from 0 again; the list is kept
            closed = null;
            connection.stage = Stage.READY;
            for (byte[] body : connection.early) onReply(connection, body);
            connection.early.clear();
            if (connection.earlyFailure != null) fail(connection, connection.earlyFailure);
        } finally {
            lock.unlock();
        }
    }

    /** Connects {@code channel} to the path within timeoutMs, then puts it in blocking mode. Nothing listening is a
     * DialFailure; close() wakes a connect still waiting, which then fails with the closed-by-caller error. */
    private void dial(SocketChannel channel) {
        Selector selector;
        try {
            channel.configureBlocking(false);
            if (channel.connect(UnixDomainSocketAddress.of(path))) {
                channel.configureBlocking(true);
                return;
            }
            selector = Selector.open();
        } catch (IOException error) {
            throw new DialFailure(ioText(error), error);
        }
        try (selector) {
            channel.register(selector, SelectionKey.OP_CONNECT);
            lock.lock();
            try {
                if (closedByCaller) throw closedByItsCaller();
                dialing = selector;
            } finally {
                lock.unlock();
            }
            try {
                selector.select(timeoutMs);
            } finally {
                lock.lock();
                try {
                    dialing = null;
                } finally {
                    lock.unlock();
                }
            }
            if (closedByCaller) throw closedByItsCaller();
            if (!channel.finishConnect()) {
                throw new JeffError.EngineError("The engine at " + path + " did not accept the connection within " + timeoutMs + " ms");
            }
            selector.selectedKeys().clear();
            channel.keyFor(selector).cancel();
            selector.selectNow(); // the cancelled key is deregistered, so the channel may block again
            channel.configureBlocking(true);
        } catch (IOException error) {
            throw new DialFailure(ioText(error), error);
        }
    }

    /** An I/O error's own message, or its type when it has none (an AsynchronousCloseException, say). */
    private static String ioText(IOException error) {
        return error.getMessage() == null ? error.getClass().getSimpleName() : error.getMessage();
    }

    private JeffError.EngineError closedByItsCaller() {
        return new JeffError.EngineError("The SocketEngine for " + path + " was closed by its caller");
    }

    private Map<String, Object> awaitGreeting(Connection connection) {
        try {
            return connection.greeting.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException error) {
            throw new JeffError.EngineError("No hello from the engine at " + path + " within " + timeoutMs + " ms");
        } catch (ExecutionException error) {
            throw rethrown(error, "The hello of the engine at " + path + " failed");
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new JeffError.EngineError("Interrupted while waiting for the hello of the engine at " + path);
        }
    }

    private static RuntimeException rethrown(ExecutionException error, String what) {
        if (error.getCause() instanceof RuntimeException cause) return cause;
        if (error.getCause() instanceof Error fatal) throw fatal;
        return new IllegalStateException(what, error.getCause());
    }

    /** Closes a connection's socket (which ends both its threads) and tells its writer to stop. */
    private void end(Connection connection) {
        connection.outgoing.add(Outgoing.STOP);
        try {
            connection.channel.close();
        } catch (IOException error) {
            throw new UncheckedIOException("Closing the engine socket to " + path + " failed", error);
        }
    }

    private void threadEnded(Connection connection) {
        if (connection.threadsEnded.incrementAndGet() < 2) return;
        lock.lock();
        try {
            live.remove(connection);
        } finally {
            lock.unlock();
        }
    }

    /** The writer thread of one connection: writes each queued frame whole, in the order queued, skipping the ones
     * their callers abandoned before they were started. A failed write ends the connection for every call. */
    private void write(Connection connection) {
        try {
            while (true) {
                Outgoing next;
                try {
                    next = connection.outgoing.take();
                } catch (InterruptedException error) {
                    // nothing in this class interrupts this thread; if anything else does, the connection ends by name
                    writeFailed(connection, new IOException("The writer thread was interrupted", error));
                    return;
                }
                if (next == Outgoing.STOP) return;
                if (!next.state.compareAndSet(Outgoing.QUEUED, Outgoing.WRITING)) continue;
                ByteBuffer buffer = ByteBuffer.wrap(next.frame);
                try {
                    while (buffer.hasRemaining()) connection.channel.write(buffer);
                } catch (IOException error) {
                    writeFailed(connection, error);
                    return;
                }
            }
        } finally {
            threadEnded(connection);
        }
    }

    private void writeFailed(Connection connection, IOException error) {
        lock.lock();
        try {
            switch (connection.stage) {
                case HANDSHAKE -> connection.greeting.completeExceptionally(new JeffError.EngineError("The engine at " + path
                    + " closed the connection during the handshake: " + ioText(error)));
                case EARLY -> {
                    if (connection.earlyFailure == null) connection.earlyFailure = ending(null, error);
                }
                case READY -> fail(connection, ending(null, error));
            }
        } finally {
            lock.unlock();
        }
    }

    /** Splits the bytes of a stream into frame bodies as they arrive, like the Node SDK's FrameReader: each frame's
     * length is checked against the 64 MiB limit before its body is allocated, once. */
    private static final class Frames {
        private final ByteBuffer header = ByteBuffer.allocate(Protocol.HEADER_SIZE);
        private byte[] body;
        private int filled;

        /** Adds the bodies completed by {@code chunk} to {@code out}; InvalidRequest for a header over the limit, after the bodies
         * before it were added. */
        void push(ByteBuffer chunk, List<byte[]> out) {
            while (chunk.hasRemaining()) {
                if (body == null) {
                    while (header.hasRemaining() && chunk.hasRemaining()) header.put(chunk.get());
                    if (header.hasRemaining()) return;
                    body = new byte[Protocol.frameLength(header.array())];
                    filled = 0;
                }
                int count = Math.min(chunk.remaining(), body.length - filled);
                chunk.get(body, filled, count);
                filled += count;
                if (filled == body.length) {
                    out.add(body);
                    body = null;
                    header.clear();
                }
            }
        }
    }

    /** The reader thread of one connection: whatever bytes have arrived, split into frames, until the stream ends or
     * fails. The frames that came in one read are all delivered before the hello among them is handed over, so a
     * frame sent in the same bytes as the hello is handled once the connection is the current one, as in Node. */
    private void read(Connection connection) {
        try {
            Frames frames = new Frames();
            ByteBuffer chunk = ByteBuffer.allocate(64 * 1024);
            while (true) {
                chunk.clear();
                int count;
                try {
                    count = connection.channel.read(chunk);
                } catch (IOException error) {
                    broken(connection, null, error);
                    return;
                }
                if (count < 0) {
                    // the engine closed the connection, between frames or in the middle of one
                    broken(connection, null, null);
                    return;
                }
                chunk.flip();
                List<byte[]> bodies = new ArrayList<>();
                JeffError refused = null;
                try {
                    frames.push(chunk, bodies);
                } catch (JeffError frameError) {
                    refused = frameError;
                }
                Map<String, Object> greeting = null;
                JeffError greetingError = null;
                boolean greeted = false;
                for (byte[] body : bodies) {
                    if (deliver(connection, body)) {
                        greeted = true;
                        try {
                            greeting = Protocol.decodeBody(body);
                        } catch (JeffError error) {
                            greetingError = error;
                        }
                    }
                }
                if (greeted) {
                    if (greetingError != null) connection.greeting.completeExceptionally(greetingError);
                    else connection.greeting.complete(greeting);
                }
                if (refused != null) {
                    broken(connection, refused, null);
                    return;
                }
            }
        } finally {
            threadEnded(connection);
        }
    }

    /** Hands one frame body on: true for the first (the hello, which the caller hands to the handshake), kept while
     * the connection is not yet the current one, a reply after. */
    private boolean deliver(Connection connection, byte[] body) {
        lock.lock();
        try {
            switch (connection.stage) {
                case HANDSHAKE -> {
                    connection.stage = Stage.EARLY;
                    return true;
                }
                case EARLY -> {
                    connection.early.add(body);
                    return false;
                }
                case READY -> { /* below, outside the lock */ }
            }
        } finally {
            lock.unlock();
        }
        onReply(connection, body);
        return false;
    }

    /** The end of a connection's stream: a frame header over 64 MiB, an I/O error, or (both null) the engine closing it. */
    private JeffError ending(JeffError frameError, IOException ioError) {
        if (frameError != null) return new JeffError.EngineError("The engine socket to " + path + " failed: " + frameError.described());
        if (ioError != null) return new JeffError.EngineError("The engine socket to " + path + " failed: " + JeffError.describe(ioError));
        return new JeffError.EngineError("The engine at " + path + " closed the connection");
    }

    private void broken(Connection connection, JeffError frameError, IOException ioError) {
        lock.lock();
        try {
            switch (connection.stage) {
                case HANDSHAKE -> connection.greeting.completeExceptionally(frameError != null ? frameError
                    : ioError == null ? new JeffError.EngineError("The engine at " + path + " closed the connection during the handshake")
                    : new JeffError.EngineError("The engine at " + path + " closed the connection during the handshake: "
                        + ioText(ioError)));
                case EARLY -> {
                    if (connection.earlyFailure == null) connection.earlyFailure = ending(frameError, ioError);
                }
                case READY -> fail(connection, ending(frameError, ioError));
            }
        } finally {
            lock.unlock();
        }
    }

    private void onReply(Connection connection, byte[] body) {
        Map<String, Object> message;
        try {
            message = Protocol.decodeBody(body);
        } catch (JeffError error) {
            fail(connection, new JeffError.EngineError("The engine socket to " + path + " failed: " + error.described()));
            return;
        }
        if (!(message.get("id") instanceof Number number)) {
            fail(connection, new JeffError.EngineError("The engine socket to " + path + " failed: the engine sent a message "
                + "with no request id (op " + PyFormat.repr(message.get("op")) + ")"));
            return;
        }
        long id = number.longValue();
        if (id == 0 && Boolean.FALSE.equals(message.get("ok"))) {
            // Not a reply to any call (this side's ids start at 1): a connection-level failure every waiting call fails with.
            fail(connection, rebuild(Values.map(message.get("error"))));
            return;
        }
        CompletableFuture<Map<String, Object>> waiting;
        lock.lock();
        try {
            waiting = connection == current ? pending.remove(id) : null;
        } finally {
            lock.unlock();
        }
        if (waiting != null) waiting.complete(message); // else a late reply to a call that stopped waiting
    }

    /** Ends {@code connection}: if it is still the current one, the first error wins and every waiting call fails with its
     * own copy of it. A connection that is no longer the current one is only closed. */
    private void fail(Connection connection, JeffError error) {
        List<CompletableFuture<Map<String, Object>>> waiting = new ArrayList<>();
        JeffError cause = null;
        lock.lock();
        try {
            if (connection == current) {
                if (closed == null) closed = error;
                cause = closed;
                waiting.addAll(pending.values());
                pending.clear();
            }
        } finally {
            lock.unlock();
        }
        for (CompletableFuture<Map<String, Object>> call : waiting) call.completeExceptionally(JeffError.copy(cause));
        end(connection);
    }

    /** The error for a call made once the connection is closed for good. */
    private JeffError.EngineError dead() {
        JeffError cause = closed;
        if (cause == null) return new JeffError.EngineError("The SocketEngine for " + path + " was closed by its caller");
        JeffError.EngineError error = new JeffError.EngineError("The engine at " + path + " closed the connection after: "
            + cause.described());
        error.initCause(cause);
        return error;
    }

    /** The error for a call made within RECONNECT_INTERVAL_MS of the last attempt, naming what it found. */
    private JeffError.EngineError tooSoon(long now) {
        Attempt attempt = lastAttempt;
        String ago = PyFormat.fixed((now - attempt.at) / 1e9, 2);
        String interval = PyFormat.floatRepr(RECONNECT_INTERVAL_MS / 1000.0);
        JeffError.EngineError error;
        if (attempt.failure != null) {
            error = new JeffError.EngineError("No engine is listening at " + path + ": the last reconnect attempt, " + ago
                + "s ago, failed: " + attempt.failure.getMessage() + ". The next attempt may be made " + interval + "s after it");
            error.initCause(attempt.failure);
        } else {
            error = new JeffError.EngineError("The engine at " + path + " closed the connection " + ago + "s ago, just after "
                + "this side reconnected: " + closed.described() + ". The next attempt may be made " + interval + "s after the last");
            error.initCause(closed);
        }
        return error;
    }

    /** Called by a call that found the connection closed (or a reconnect attempt still checking it): one attempt at a
     * time makes one fresh connection (connect, hello, installed, health, the base check); a call that waited
     * meanwhile then finds the connection open and uses it, or fails naming that attempt. At most one attempt per
     * RECONNECT_INTERVAL_MS; never after close(). The attempt runs on a thread of its own, shared by every waiting
     * call: a caller interrupted meanwhile only stops waiting, and the new connection is still checked for its base
     * (or discarded, the attempt failing by name). */
    private void reconnect() {
        while (true) {
            CompletableFuture<Void> waitFor;
            Attempt mine = null;
            lock.lock();
            try {
                if (closedByCaller) throw dead();
                if (running != null) {
                    waitFor = running;
                } else {
                    if (closed == null && current != null) return;
                    long now = System.nanoTime();
                    if (lastAttempt != null && now - lastAttempt.at < TimeUnit.MILLISECONDS.toNanos(RECONNECT_INTERVAL_MS)) {
                        throw tooSoon(now);
                    }
                    Attempt attempt = new Attempt(now);
                    mine = attempt;
                    lastAttempt = attempt;
                    CompletableFuture<Void> done = new CompletableFuture<>();
                    running = waitFor = done;
                    attemptThread = Thread.ofVirtual().name("jeff-engine-reconnect " + path).start(() -> runAttempt(attempt, done));
                }
            } finally {
                lock.unlock();
            }
            try {
                waitFor.get(attemptLimitMs(), TimeUnit.MILLISECONDS);
            } catch (TimeoutException error) {
                throw new JeffError.EngineError("The reconnect attempt to the engine at " + path + " did not end within "
                    + attemptLimitMs() + " ms");
            } catch (ExecutionException error) {
                throw rethrown(error, "The reconnect attempt to the engine at " + path + " failed");
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new JeffError.EngineError("Interrupted while waiting for the reconnect attempt to the engine at " + path);
            }
            // close() ended the attempt, or it ended meanwhile: the caller's own close is what every waiter is told
            if (closedByCaller) throw closedByItsCaller();
            if (mine != null) {
                if (mine.failure != null) throw mine.failure;
                return;
            }
            // another call's attempt ended: look again (the connection open, or a failure within the interval named)
        }
    }

    /** One reconnect attempt, on its own thread. How it failed is attempt.failure, which its own caller raises and later
     * calls within the interval name; {@code done} completes once it has ended (with the error itself only for an Error). */
    private void runAttempt(Attempt attempt, CompletableFuture<Void> done) {
        try {
            attempt(attempt);
        } catch (RuntimeException failed) {
            // recorded in attempt.failure by attempt()
        } catch (Error fatal) {
            done.completeExceptionally(fatal);
            throw fatal;
        } finally {
            lock.lock();
            try {
                if (running == done) {
                    running = null;
                    attemptThread = null;
                }
            } finally {
                lock.unlock();
            }
            done.complete(null);
        }
    }

    /** The longest a reconnect attempt can take: its connect, hello, installed and health, and a refresh after the
     * health, each bounded by timeoutMs. */
    private long attemptLimitMs() {
        return 5 * timeoutMs;
    }

    private void attempt(Attempt attempt) {
        Snapshot snapshot;
        Base base;
        try {
            open();
            snapshot = installedNow(timeoutMs, false);
            base = baseNow(false);
        } catch (RuntimeException cause) {
            JeffError.EngineError error = new JeffError.EngineError("No engine is listening at " + path + ": " + cause.getMessage()
                + " (reconnect attempt)");
            error.initCause(cause);
            attempt.failure = error;
            shutdown(error);
            throw error;
        }
        if (!base.format().equals(format) || !base.id().equals(baseId) || !base.version().equals(baseVersion)) {
            // Functions bound through this engine chose their calibration for the format read at connect; the restarted
            // engine serves other weights, so nothing more is sent to it from this process.
            JeffError.EngineError error = new JeffError.EngineError("The engine at " + path + " restarted serving a base of format "
                + PyFormat.repr(base.format().toMap()) + " (" + base.id() + "@" + base.version() + "), not "
                + PyFormat.repr(format.toMap()) + " (" + baseId + "@" + baseVersion + ") as when this process connected; the "
                + "functions bound here use that format's calibration, and the adapters listed are the ones trained for that "
                + "base. Restart this process to serve the new base");
            attempt.failure = error;
            shutdown(error);
            throw error;
        }
        merge(snapshot);
        reconnects += 1;
    }

    private void shutdown(JeffError error) {
        Connection connection;
        lock.lock();
        try {
            connection = current;
        } finally {
            lock.unlock();
        }
        if (connection != null) fail(connection, error);
    }

    private long version(Map<String, Object> body, String op) {
        if (!body.containsKey("adapters_version")) {
            throw new JeffError.EngineError("The engine at " + path + " answered " + PyFormat.str(op) + " without an "
                + "adapters_version; every reply must carry one (contract/socket.md)");
        }
        return ((Number) body.get("adapters_version")).longValue();
    }

    /** Registers one request and queues its frame for the writer (reconnecting first when the connection is closed);
     * the reply is waited for by finish. {@code reconnect} false is a call made while connecting or by a reconnect attempt
     * itself: a connection already closed again fails at once, and it may use the connection the attempt is still
     * checking, which every other call waits for. */
    private Sent send(Map<String, Object> message, long waitMs, boolean reconnect) {
        return send(prepare(message), waitMs, reconnect);
    }

    /** A request with its id, encoded. */
    private record Prepared(long id, String op, byte[] frame) {}

    private Prepared prepare(Map<String, Object> message) {
        long id = nextId.getAndIncrement();
        Map<String, Object> framed = new LinkedHashMap<>(message);
        framed.put("id", id);
        // encoded before anything is registered: an oversized or invalid message leaves nothing behind
        return new Prepared(id, (String) message.get("op"), Protocol.encodeFrame(framed, null));
    }

    private Sent send(Prepared request, long waitMs, boolean reconnect) {
        while (true) {
            Sent sent = queue(request, waitMs, reconnect);
            if (sent != null) return sent;
            reconnect();
        }
    }

    /** Registers the request and queues its frame when the connection is usable now; null when a reconnect is needed
     * first (never for {@code reconnect} false, which fails at once instead). */
    private Sent queue(Prepared request, long waitMs, boolean reconnect) {
        lock.lock();
        try {
            boolean usable = closed == null && current != null && (running == null || !reconnect);
            if (usable) {
                CompletableFuture<Map<String, Object>> reply = new CompletableFuture<>();
                Outgoing outgoing = new Outgoing(request.frame());
                pending.put(request.id(), reply);
                // queued under the lock, so frames leave in the order calls register
                current.outgoing.add(outgoing);
                long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(waitMs);
                return new Sent(request.id(), request.op(), reply, outgoing, deadline, waitMs, reconnect);
            }
            if (!reconnect) throw closedByCaller || lastAttempt == null ? dead() : tooSoon(System.nanoTime());
            return null;
        } finally {
            lock.unlock();
        }
    }

    /** Stops waiting for {@code sent}: its id is dropped, and its frame is never written unless the writer has started it. */
    private void abandon(Sent sent) {
        lock.lock();
        try {
            pending.remove(sent.id());
        } finally {
            lock.unlock();
        }
        sent.frame().abandon();
    }

    /** The reply to {@code sent}, waited for until its deadline, which covers its wait to be written as well. */
    private Map<String, Object> await(Sent sent) {
        try {
            return sent.reply().get(Math.max(0, sent.deadline() - System.nanoTime()), TimeUnit.NANOSECONDS);
        } catch (TimeoutException error) {
            abandon(sent);
            throw new JeffError.EngineError("No reply from the engine at " + path + " within " + sent.timeoutMs() + " ms");
        } catch (ExecutionException error) {
            throw rethrown(error, "A reply from the engine at " + path + " failed");
        } catch (InterruptedException error) {
            abandon(sent);
            Thread.currentThread().interrupt();
            throw new JeffError.EngineError("Interrupted while waiting for a reply from the engine at " + path);
        }
    }

    /** Waits for a sent request's reply. Every reply carries the engine's adapters_version: a reply to
     * installed/serve/reload sets the cached list from its own; any other reply with another version makes this side
     * ask for the list (except a failed {@code installed}). A failure is raised as the error it describes, after that. */
    private Map<String, Object> finish(Sent sent) {
        return finish(sent, await(sent));
    }

    private Map<String, Object> finish(Sent sent, Map<String, Object> reply) {
        if (!Boolean.TRUE.equals(reply.get("ok"))) {
            // The engine's own error always wins; a failure reading its version or refreshing the list is a note on it.
            Map<String, Object> failure = Values.map(reply.get("error"));
            JeffError original = rebuild(failure);
            long version;
            try {
                version = version(failure.get("details") instanceof Map<?, ?> details ? Values.map(details) : Map.of(), sent.op());
            } catch (JeffError error) {
                original.addNote("Also failed to read adapters_version from this failure reply: " + error.described());
                throw original;
            }
            if (!sent.op().equals("installed")) {
                try {
                    follow(version, sent.timeoutMs(), sent.reconnect());
                } catch (JeffError error) {
                    original.addNote("Also failed to refresh the adapter list after this error: " + error.described());
                }
            }
            throw original;
        }
        Map<String, Object> result = Values.map(reply.get("result"));
        long version = version(result, sent.op());
        if (CARRY_ADAPTERS.contains(sent.op())) merge(Snapshot.of(version, result.get("adapters")));
        else follow(version, sent.timeoutMs(), sent.reconnect());
        return result;
    }

    private Map<String, Object> call(Map<String, Object> message, long waitMs, boolean reconnect) {
        return finish(send(message, waitMs, reconnect));
    }

    /** Asks for the list when {@code version} is not the cached one. {@code reconnect} is the asking call's: a refresh made while
     * connecting or by a reconnect attempt never starts, or waits on, a reconnect attempt (it may be inside the very
     * one it would wait on). */
    private void follow(long version, long waitMs, boolean reconnect) {
        long known;
        lock.lock();
        try {
            known = snapshotVersion;
        } finally {
            lock.unlock();
        }
        if (version != known) merge(installedNow(waitMs, reconnect));
    }

    private Snapshot installedNow(long waitMs, boolean reconnect) {
        Map<String, Object> result = call(Fixed.op("installed"), waitMs, reconnect);
        return Snapshot.of(((Number) result.get("adapters_version")).longValue(), result.get("adapters"));
    }

    /** The one place the cached list is written; replies may arrive out of order, so an older version never replaces
     * a newer one. */
    private void merge(Snapshot snapshot) {
        lock.lock();
        try {
            if (snapshot.version() >= snapshotVersion) {
                snapshotVersion = snapshot.version();
                snapshotInstalled = snapshot.installed();
                snapshotOrder = snapshot.order();
            }
        } finally {
            lock.unlock();
        }
    }

    /** From the engine's health: its base format (format, and format_id for an INT4 base) and the base it serves. */
    private Base baseNow(boolean reconnect) {
        Map<String, Object> health = call(Fixed.op("health"), timeoutMs, reconnect);
        Object base = health.get("base");
        if (!(base instanceof Map<?, ?> fields) || !(fields.get("format") instanceof String)) {
            throw new JeffError.EngineError("The engine at " + path + " answered 'health' without base.format (got base "
                + PyFormat.repr(base) + "); every engine reports its base's weight format (contract/socket.md)");
        }
        if (!(fields.get("id") instanceof String id) || !(fields.get("version") instanceof String version)) {
            throw new JeffError.EngineError("The engine at " + path + " answered 'health' without base.id and base.version (got "
                + "base " + PyFormat.repr(base) + "); every engine reports the base it serves as adapter manifests name it "
                + "(contract/socket.md)");
        }
        return new Base(BaseFormat.fromWire(Values.map(fields)), id, version);
    }

    /** The adapters the engine has loaded, in the order its last adapter list gave them ({@code base} included). */
    List<String> installedInOrder() {
        lock.lock();
        try {
            return snapshotOrder;
        } finally {
            lock.unlock();
        }
    }

    /** Asks the engine to serve exactly {@code adapters} (contract/socket.md, {@code serve}); the cached list becomes
     * the one it answers with.
     *
     * @return null when the engine serves them, or the engine's refusal (its failure reply, as the error it describes)
     * @throws JeffError.EngineError when the engine cannot be reached or the reply does not come within the timeout */
    JeffError serve(List<String> adapters) {
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("op", "serve");
        message.put("adapters", new ArrayList<Object>(adapters));
        Sent sent = send(message, timeoutMs, true);
        Map<String, Object> reply = await(sent);
        if (Boolean.TRUE.equals(reply.get("ok"))) {
            finish(sent, reply);
            return null;
        }
        try {
            finish(sent, reply);
        } catch (JeffError refusal) {
            return refusal;
        }
        throw new IllegalStateException("A failure reply to 'serve' raised no error");
    }

    /** The adapters the engine has loaded, from the list kept since the last reply (every reply carries the version of
     * it, and a newer version is asked for).
     *
     * @param refresh {@code true} to ask the engine first
     * @return the adapter ids
     * @throws JeffError.EngineError when the engine cannot be reached (a reconnect within
     *     {@link #RECONNECT_INTERVAL_MS} of the last fails at once), the reply does not come within the timeout, the
     *     engine was closed, or the reply is malformed
     * @throws JeffError the engine's own named errors, as it sends them */
    @Override
    public Set<String> installed(boolean refresh) {
        if (refresh) merge(installedNow(timeoutMs, true));
        lock.lock();
        try {
            return snapshotInstalled;
        } finally {
            lock.unlock();
        }
    }

    private static Map<String, Object> predictMessage(String adapterId, List<Row> rows, Number deadlineMs) {
        List<Object> wire = new ArrayList<>();
        for (Row row : rows) wire.add(row.toWire());
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("op", "predict");
        message.put("adapter", adapterId);
        message.put("rows", wire);
        if (deadlineMs != null) message.put("deadline_ms", deadlineMs);
        return message;
    }

    private static EngineResult resultOf(Map<String, Object> result) {
        List<double[]> probabilities = new ArrayList<>();
        for (Object row : Values.list(result.get("probabilities"))) {
            probabilities.add(Values.list(row).stream().mapToDouble(value -> ((Number) value).doubleValue()).toArray());
        }
        List<Long> tokens = null;
        // optional in the reply: an engine that does not count tokens leaves it out
        if (result.containsKey("input_tokens")) {
            tokens = new ArrayList<>();
            for (Object count : Values.list(result.get("input_tokens"))) tokens.add(((Number) count).longValue());
        }
        return new EngineResult(probabilities, new LinkedHashMap<>(Values.map(result.get("timing"))), tokens);
    }

    /** Runs the adapter's model over rows, with no queueing deadline.
     *
     * @param adapterId the adapter
     * @param rows the rows
     * @return one probability array per row, with the engine's timing
     * @throws JeffError.NotInstalled when the engine has not loaded the adapter
     * @throws JeffError.Busy when the engine's queue is full
     * @throws JeffError.EngineError when the engine cannot be reached (a reconnect within
     *     {@link #RECONNECT_INTERVAL_MS} of the last fails at once), the reply does not come within the timeout, the
     *     engine was closed, or the reply is malformed
     * @throws JeffError the engine's own named errors, as it sends them */
    @Override
    public EngineResult predict(String adapterId, List<Row> rows) {
        return predict(adapterId, rows, null);
    }

    /** Runs the adapter's model over rows, which the engine may queue for at most {@code deadlineMs}.
     *
     * @param adapterId the adapter
     * @param rows the rows
     * @param deadlineMs how long the engine may queue the rows before refusing them (contract/socket.md), in
     *     milliseconds; null for no limit
     * @return one probability array per row, with the engine's timing
     * @throws JeffError.DeadlineExceeded when the engine could not start the rows within the deadline
     * @throws JeffError.NotInstalled when the engine has not loaded the adapter
     * @throws JeffError.Busy when the engine's queue is full
     * @throws JeffError.EngineError when the engine cannot be reached (a reconnect within
     *     {@link #RECONNECT_INTERVAL_MS} of the last fails at once), the reply does not come within the timeout, the
     *     engine was closed, or the reply is malformed
     * @throws JeffError the engine's own named errors, as it sends them */
    public EngineResult predict(String adapterId, List<Row> rows, Number deadlineMs) {
        return resultOf(call(predictMessage(adapterId, rows, deadlineMs), timeoutMs, true));
    }

    /** Returns at once. When the connection is open, the request is queued now, on this thread (so requests leave in
     * call order), and the reply is waited for on a virtual thread; when it is closed, the reconnect, the request and
     * the wait all happen on that virtual thread.
     *
     * @param adapterId the adapter
     * @param rows the rows
     * @return a future of the result, failed with the error {@link #predict(String, List)} would raise */
    @Override
    public CompletableFuture<EngineResult> predictAsync(String adapterId, List<Row> rows) {
        Prepared request;
        Sent sent;
        try {
            request = prepare(predictMessage(adapterId, rows, null));
            sent = queue(request, timeoutMs, true);
        } catch (RuntimeException error) {
            return CompletableFuture.failedFuture(error);
        }
        if (sent != null) return Async.run(() -> resultOf(finish(sent)));
        return Async.run(() -> resultOf(finish(send(request, timeoutMs, true))));
    }

    /** Asks the engine for its health (contract/socket.md, {@code health}).
     *
     * @return the engine's answer, as it sends it
     * @throws JeffError.EngineError when the engine cannot be reached (a reconnect within
     *     {@link #RECONNECT_INTERVAL_MS} of the last fails at once), the reply does not come within the timeout, the
     *     engine was closed, or the reply is malformed
     * @throws JeffError the engine's own named errors, as it sends them */
    public Map<String, Object> health() {
        return call(Fixed.op("health"), timeoutMs, true);
    }

    /** Asks the engine for its counters (contract/socket.md, {@code stats}).
     *
     * @return the engine's answer, as it sends it
     * @throws JeffError.EngineError when the engine cannot be reached (a reconnect within
     *     {@link #RECONNECT_INTERVAL_MS} of the last fails at once), the reply does not come within the timeout, the
     *     engine was closed, or the reply is malformed
     * @throws JeffError the engine's own named errors, as it sends them */
    public Map<String, Object> stats() {
        return call(Fixed.op("stats"), timeoutMs, true);
    }

    /** Closes the connection. Final: later calls fail with {@link JeffError.EngineError}, and there is no
     * reconnect. Every waiting call fails at once, naming the close; a
     * write stuck on an engine that stopped reading is unblocked; it returns once every thread of this engine has
     * ended. Calling it again does nothing. */
    @Override
    public void close() {
        Connection connection;
        List<Connection> ending;
        CompletableFuture<Void> waiters;
        Thread attempt;
        Selector connecting;
        lock.lock();
        try {
            if (closedByCaller) return;
            closedByCaller = true;
            connection = current;
            ending = new ArrayList<>(live);
            waiters = running;
            attempt = attemptThread;
            connecting = dialing;
        } finally {
            lock.unlock();
        }
        // every waiter is released before any thread is waited for
        if (connection != null) fail(connection, closedByItsCaller());
        if (waiters != null) waiters.complete(null); // each then fails with the closed-by-caller error
        if (connecting != null) connecting.wakeup();
        for (Connection other : ending) end(other);
        List<Thread> threads = new ArrayList<>();
        for (Connection other : ending) threads.addAll(List.of(other.reader, other.writer));
        if (attempt != null) threads.add(attempt);
        try {
            for (Thread thread : threads) {
                if (!thread.join(THREAD_END)) {
                    throw new IllegalStateException("The thread " + thread.getName() + " did not end within "
                        + THREAD_END.toMillis() + " ms of close()");
                }
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new JeffError.EngineError("Interrupted while closing the SocketEngine for " + path);
        }
    }

    /** A message holding only its op. */
    private static final class Fixed {
        static Map<String, Object> op(String op) {
            Map<String, Object> message = new LinkedHashMap<>();
            message.put("op", op);
            return message;
        }
    }
}
