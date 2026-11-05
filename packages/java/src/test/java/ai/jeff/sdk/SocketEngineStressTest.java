package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

/** The socket client under engines that misbehave: replies out of order, an engine that stops reading, an engine that
 * drops every call, a restart onto another base while the caller is interrupted, a reconnect that fails as it finishes
 * (the faults the Rust and C# ports met). */
class SocketEngineStressTest {
    private interface Serve {
        void serve(int index, SocketChannel socket) throws IOException, InterruptedException;
    }

    /** Listens at a fresh path; connection number `index` (from 0) is served by `serve(index, socket)`. */
    private static final class Fake implements AutoCloseable {
        final Path directory = EngineServer.socketDirectory();
        final Path path = directory.resolve("e.sock");
        private final ServerSocketChannel server;
        private final List<SocketChannel> sockets = Collections.synchronizedList(new ArrayList<>());

        Fake(Serve serve) throws IOException {
            server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            server.bind(UnixDomainSocketAddress.of(path));
            Thread.ofVirtual().start(() -> {
                for (int index = 0; ; index++) {
                    SocketChannel socket;
                    try {
                        socket = server.accept();
                    } catch (IOException closed) {
                        return; // close() closed the server
                    }
                    sockets.add(socket);
                    int number = index;
                    Thread.ofVirtual().start(() -> {
                        try (socket) {
                            serve.serve(number, socket);
                        } catch (IOException | InterruptedException ended) {
                            // the client or the test ended the connection
                        }
                    });
                }
            });
        }

        void dropAll() throws IOException {
            synchronized (sockets) {
                for (SocketChannel socket : sockets) socket.close();
                sockets.clear();
            }
        }

        @Override
        public void close() throws IOException {
            server.close();
            dropAll();
            Files.deleteIfExists(path);
            Files.deleteIfExists(directory);
        }
    }

    private interface OnMessage {
        void on(Map<String, Object> message) throws IOException, InterruptedException;
    }

    /** Reads frames from `socket` and hands each message to `on`, until the socket ends. */
    private static void frames(SocketChannel socket, OnMessage on) throws IOException, InterruptedException {
        for (byte[] body = Protocol.readFrameBody(socket); body != null; body = Protocol.readFrameBody(socket)) {
            on.on(Values.map(MsgPack.decode(body)));
        }
    }

    private static void send(SocketChannel socket, byte[]... frames) throws IOException {
        ByteArrayOutputStream all = new ByteArrayOutputStream();
        for (byte[] frame : frames) all.writeBytes(frame);
        ByteBuffer buffer = ByteBuffer.wrap(all.toByteArray());
        synchronized (socket) {
            while (buffer.hasRemaining()) socket.write(buffer);
        }
    }

    private static long idOf(Map<String, Object> message) {
        return ((Number) message.get("id")).longValue();
    }

    private static byte[] hello() {
        return EngineServer.frame(Fixtures.ordered("op", "hello", "version", 2L, "adapters_version", 1L));
    }

    private static byte[] installed(long id, long version) {
        return EngineServer.frame(Fixtures.ordered("id", id, "ok", true, "result",
            Fixtures.ordered("adapters", List.of("base", "jeff.safety.spam"), "adapters_version", version)));
    }

    private static byte[] health(long id, long version, String format, String baseVersion) {
        Map<String, Object> base = Fixtures.ordered("id", "jeff-base", "version", baseVersion, "checkpoint", "fake",
            "max_options", 252L, "format", format);
        if (!format.equals("bf16")) base.put("format_id", "q");
        return EngineServer.frame(Fixtures.ordered("id", id, "ok", true, "result", Fixtures.ordered("status", "ready",
            "backend", "fake", "adapters", List.of("jeff.safety.spam"), "adapters_version", version, "base", base)));
    }

    private static byte[] health(long id) {
        return health(id, 1, "bf16", "v1.3");
    }

    private static byte[] predicted(long id, double p) {
        return EngineServer.frame(Fixtures.ordered("id", id, "ok", true, "result", Fixtures.ordered(
            "probabilities", List.of(List.of(p, 1 - p)), "timing", Fixtures.ordered("path", "fake", "model_ms", 0.5, "format", "bf16"),
            "adapters_version", 1L)));
    }

    private static final byte[] ID_ZERO_FAILURE = EngineServer.frame(Fixtures.ordered("id", 0L, "ok", false, "error",
        Fixtures.ordered("type", "InvalidRequest", "message", "Frame could not be read", "details", Fixtures.ordered("adapters_version", 1L))));

    private static Row row(String state) {
        return new Row(state, new RowQuestion("noul", "?", null));
    }

    private static final String BIG = "x".repeat(8 * 1024 * 1024);

    private static Throwable failureOf(CompletableFuture<?> future, long withinMs) {
        CompletionException error = assertThrows(CompletionException.class, () -> future.orTimeout(withinMs, TimeUnit.MILLISECONDS).join());
        return error.getCause();
    }

    /** The threads of every SocketEngine for `path` still alive. */
    private static List<String> engineThreads(Path path) {
        List<String> names = new ArrayList<>();
        for (Thread thread : Thread.getAllStackTraces().keySet()) {
            if (thread.getName().startsWith("jeff-engine-") && thread.getName().endsWith(path.toString())) names.add(thread.getName());
        }
        return names;
    }

    @Test
    void twoHundredConcurrentCallsAnsweredInReverseEachGetTheirOwnReply() throws Exception {
        int count = 200;
        try (Fake fake = new Fake((index, socket) -> {
            List<long[]> held = new ArrayList<>();
            frames(socket, message -> {
                switch ((String) message.get("op")) {
                    case "hello" -> send(socket, hello());
                    case "installed" -> send(socket, installed(idOf(message), 1));
                    case "health" -> send(socket, health(idOf(message)));
                    case "predict" -> {
                        String state = (String) Values.map(Values.list(message.get("rows")).get(0)).get("state");
                        held.add(new long[] {idOf(message), Long.parseLong(state)});
                        if (held.size() == count) {
                            // every reply in one write, in reverse order: the reader meets many frames at once
                            List<byte[]> replies = new ArrayList<>();
                            for (long[] item : held.reversed()) replies.add(predicted(item[0], item[1] / 1000.0));
                            send(socket, replies.toArray(byte[][]::new));
                        }
                    }
                    default -> throw new AssertionError(message.get("op"));
                }
            });
        })) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 10_000);
            List<CompletableFuture<EngineResult>> calls = new ArrayList<>();
            for (int n = 0; n < count; n++) {
                String state = Integer.toString(n);
                calls.add(n % 2 == 0 ? Async.run(() -> engine.predict("jeff.safety.spam", List.of(row(state))))
                    : engine.predictAsync("jeff.safety.spam", List.of(row(state))));
            }
            for (int n = 0; n < count; n++) {
                assertEquals(n / 1000.0, calls.get(n).orTimeout(15, TimeUnit.SECONDS).join().probabilities().get(0)[0]);
            }
            engine.close();
            assertEquals(List.of(), engineThreads(fake.path));
        }
    }

    /** An engine that answers every request, but stops reading after the second health until `resume` counts down. */
    private static Fake wedged(CountDownLatch resume, List<String> ops) throws IOException {
        return new Fake((index, socket) -> {
            int[] healths = {0};
            frames(socket, message -> {
                String op = (String) message.get("op");
                ops.add(op);
                switch (op) {
                    case "hello" -> send(socket, hello());
                    case "installed" -> send(socket, installed(idOf(message), 1));
                    case "health" -> send(socket, health(idOf(message)));
                    case "predict" -> send(socket, predicted(idOf(message), 0.25));
                    default -> throw new AssertionError(op);
                }
                if (op.equals("health") && ++healths[0] == 2) resume.await(); // stop reading
            });
        });
    }

    @Test
    void aWriteBlockedByAnEngineThatStoppedReadingStillHonoursEachCallsTimeout() throws Exception {
        CountDownLatch resume = new CountDownLatch(1);
        List<String> ops = Collections.synchronizedList(new ArrayList<>());
        try (Fake fake = wedged(resume, ops)) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 300);
            engine.health(); // the engine stops reading after this
            long started = System.nanoTime();
            CompletableFuture<EngineResult> stuck = Async.run(() -> engine.predict("jeff.safety.spam", List.of(row(BIG))));
            Thread.sleep(50);
            CompletableFuture<Map<String, Object>> queued = Async.run(engine::health);
            assertEquals("No reply from the engine at " + fake.path + " within 300 ms", failureOf(stuck, 2000).getMessage());
            long stuckMs = (System.nanoTime() - started) / 1_000_000;
            assertTrue(stuckMs >= 290 && stuckMs < 1000, stuckMs + " ms");
            assertEquals("No reply from the engine at " + fake.path + " within 300 ms", failureOf(queued, 2000).getMessage());
            long queuedMs = (System.nanoTime() - started) / 1_000_000;
            assertTrue(queuedMs >= 340 && queuedMs < 1100, queuedMs + " ms");

            // the engine reads again: the big frame was written whole, so the next request is read and answered; the
            // health that gave up before it was written was never sent
            resume.countDown();
            assertEquals(0.25, engine.predict("jeff.safety.spam", List.of(row("small"))).probabilities().get(0)[0]);
            assertEquals(List.of("hello", "installed", "health", "health", "predict", "predict"), List.copyOf(ops));
            engine.close();
        }
    }

    @Test
    void closeUnblocksAStuckWriteFailsEveryWaitingCallByNameAndLeavesNoThread() throws Exception {
        CountDownLatch resume = new CountDownLatch(1);
        try (Fake fake = wedged(resume, Collections.synchronizedList(new ArrayList<>()))) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 30_000);
            engine.health();
            CompletableFuture<EngineResult> stuck = Async.run(() -> engine.predict("jeff.safety.spam", List.of(row(BIG))));
            CompletableFuture<Map<String, Object>> queued = Async.run(engine::health);
            Thread.sleep(200);
            assertFalse(stuck.isDone(), "the big write should be blocked");
            assertEquals(2, engineThreads(fake.path).size());
            long started = System.nanoTime();
            engine.close();
            assertTrue((System.nanoTime() - started) / 1_000_000 < 3000, "close() took too long");
            for (CompletableFuture<?> call : List.of(stuck, queued)) {
                Throwable error = failureOf(call, 3000);
                assertInstanceOf(JeffError.EngineError.class, error);
                assertEquals("The SocketEngine for " + fake.path + " was closed by its caller", error.getMessage());
            }
            assertEquals(List.of(), engineThreads(fake.path));
            resume.countDown();
        }
    }

    @Test
    void aCallerInterruptedMidWriteLeavesNoHalfFrameAndTheConnectionWhole() throws Exception {
        CountDownLatch resume = new CountDownLatch(1);
        List<String> ops = Collections.synchronizedList(new ArrayList<>());
        try (Fake fake = wedged(resume, ops)) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 5000);
            engine.health();
            AtomicReference<Throwable> failure = new AtomicReference<>();
            AtomicReference<Boolean> stillInterrupted = new AtomicReference<>();
            Thread caller = Thread.ofPlatform().start(() -> {
                try {
                    engine.predict("jeff.safety.spam", List.of(row(BIG)));
                } catch (RuntimeException error) {
                    failure.set(error);
                    stillInterrupted.set(Thread.currentThread().isInterrupted());
                }
            });
            Thread.sleep(300);
            assertTrue(caller.isAlive());
            caller.interrupt();
            // released at once, while the engine still reads nothing
            assertTrue(caller.join(java.time.Duration.ofSeconds(1)), "the interrupted caller was not released");
            assertEquals("Interrupted while waiting for a reply from the engine at " + fake.path, failure.get().getMessage());
            assertTrue(stillInterrupted.get());
            resume.countDown();
            assertEquals(0.25, engine.predict("jeff.safety.spam", List.of(row("small"))).probabilities().get(0)[0]);
            assertEquals(2, ops.stream().filter("predict"::equals).count());
            assertEquals(0, engine.reconnects());
            engine.close();
        }
    }

    @Test
    void theEngineClosingFailsEveryWaitingCallAtOnceByName() throws Exception {
        CountDownLatch read = new CountDownLatch(50);
        try (Fake fake = new Fake((index, socket) -> frames(socket, message -> {
            switch ((String) message.get("op")) {
                case "hello" -> send(socket, hello());
                case "installed" -> send(socket, installed(idOf(message), 1));
                case "health" -> send(socket, health(idOf(message)));
                case "predict" -> read.countDown(); // never answered
                default -> throw new AssertionError(message.get("op"));
            }
        }))) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 30_000);
            List<CompletableFuture<EngineResult>> calls = new ArrayList<>();
            for (int n = 0; n < 50; n++) calls.add(engine.predictAsync("jeff.safety.spam", List.of(row("x"))));
            assertTrue(read.await(5, TimeUnit.SECONDS));
            fake.dropAll();
            for (CompletableFuture<EngineResult> call : calls) {
                assertEquals("The engine at " + fake.path + " closed the connection", failureOf(call, 1000).getMessage());
            }
            engine.close();
        }
    }

    @Test
    void aCallerInterruptedDuringAReconnectStillGetsTheNewConnectionCheckedForItsBase() throws Exception {
        // connection 0 serves bf16 jeff-base@v1.3; every later one (the restarted engine) serves int4 jeff-base@v9, and
        // is slow to list its adapters, so the caller is interrupted while the attempt is still under way
        try (Fake fake = new Fake((index, socket) -> frames(socket, message -> {
            switch ((String) message.get("op")) {
                case "hello" -> send(socket, hello());
                case "installed" -> {
                    if (index > 0) Thread.sleep(500);
                    send(socket, installed(idOf(message), 1));
                }
                case "health" -> send(socket, index == 0 ? health(idOf(message)) : health(idOf(message), 1, "int4", "v9"));
                default -> throw new AssertionError(message.get("op"));
            }
        }))) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 5000);
            fake.dropAll();
            Thread.sleep(100);
            AtomicReference<Throwable> interrupted = new AtomicReference<>();
            Thread caller = Thread.ofPlatform().start(() -> {
                try {
                    engine.health();
                } catch (RuntimeException error) {
                    interrupted.set(error);
                }
            });
            Thread.sleep(150);
            caller.interrupt();
            assertTrue(caller.join(java.time.Duration.ofSeconds(1)));
            assertEquals("Interrupted while waiting for the reconnect attempt to the engine at " + fake.path, interrupted.get().getMessage());
            // a call made while that attempt is still checking waits for it and fails naming it
            String during = assertThrows(JeffError.EngineError.class, engine::health).getMessage();
            String refused = "The engine at " + fake.path + " restarted serving a base of format {'format': 'int4', 'format_id': 'q'} "
                + "(jeff-base@v9), not {'format': 'bf16'} (jeff-base@v1.3) as when this process connected";
            assertTrue(during.startsWith("No engine is listening at " + fake.path + ": the last reconnect attempt, "), during);
            assertTrue(during.contains(refused), during);
            assertEquals(0, engine.reconnects());
            Thread.sleep(1100);
            // the next attempt meets the same engine and refuses it again; nothing is ever answered by it
            String again = assertThrows(JeffError.EngineError.class, engine::health).getMessage();
            assertTrue(again.startsWith(refused), again);
            assertEquals(0, engine.reconnects());
            engine.close();
            assertEquals(List.of(), engineThreads(fake.path));
        }
    }

    @Test
    void aReconnectWhoseHealthBumpsTheVersionAndThenFailsDoesNotHang() throws Exception {
        for (int round = 0; round < 10; round++) {
            try (Fake fake = new Fake((index, socket) -> frames(socket, message -> {
                switch ((String) message.get("op")) {
                    case "hello" -> send(socket, hello());
                    case "installed" -> send(socket, installed(idOf(message), 1));
                    case "health" -> {
                        if (index == 0) send(socket, health(idOf(message)));
                        else send(socket, health(idOf(message), 2, "bf16", "v1.3"), ID_ZERO_FAILURE);
                    }
                    default -> throw new AssertionError(message.get("op"));
                }
            }))) {
                SocketEngine engine = SocketEngine.connect(fake.path.toString(), 2000);
                fake.dropAll();
                Thread.sleep(100);
                CompletableFuture<EngineResult> call = engine.predictAsync("jeff.safety.spam", List.of(row("x")));
                Throwable error = failureOf(call, 5000);
                assertTrue(error.getMessage().contains("(reconnect attempt)"), error.getMessage());
                engine.close();
            }
        }
    }

    /** An engine whose first connection serves normally and whose later ones stall at `stallAt` ("hello" or
     * "installed"): they read that request and never answer it. */
    private static Fake stallingAfterRestart(String stallAt) throws IOException {
        return new Fake((index, socket) -> frames(socket, message -> {
            String op = (String) message.get("op");
            if (index > 0 && op.equals(stallAt)) return; // never answered
            switch (op) {
                case "hello" -> send(socket, hello());
                case "installed" -> send(socket, installed(idOf(message), 1));
                case "health" -> send(socket, health(idOf(message)));
                default -> throw new AssertionError(op);
            }
        }));
    }

    private static void closeDuringAReconnect(String stallAt) throws Exception {
        try (Fake fake = stallingAfterRestart(stallAt)) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 30_000);
            fake.dropAll();
            Thread.sleep(100);
            CompletableFuture<Map<String, Object>> starter = Async.run(engine::health);
            Thread.sleep(200); // the starter's attempt is now waiting on the restarted engine
            CompletableFuture<Map<String, Object>> joiner = Async.run(engine::health);
            Thread.sleep(100);
            assertFalse(starter.isDone() || joiner.isDone(), "both calls should be waiting on the reconnect");
            long started = System.nanoTime();
            engine.close();
            assertTrue((System.nanoTime() - started) / 1_000_000 < 3000, "close() took too long");
            for (CompletableFuture<?> call : List.of(starter, joiner)) {
                Throwable error = failureOf(call, 1000);
                assertInstanceOf(JeffError.EngineError.class, error);
                assertEquals("The SocketEngine for " + fake.path + " was closed by its caller", error.getMessage());
            }
            assertEquals(List.of(), engineThreads(fake.path));
            assertEquals(0, engine.reconnects());
        }
    }

    @Test
    void closeDuringAReconnectToAnEngineThatNeverSaysHelloFailsTheStarterAndTheJoinerAsClosedByTheCaller() throws Exception {
        closeDuringAReconnect("hello");
    }

    @Test
    void closeDuringAReconnectToAnEngineThatNeverListsItsAdaptersFailsTheStarterAndTheJoinerAsClosedByTheCaller() throws Exception {
        closeDuringAReconnect("installed");
    }

    @Test
    void predictAsyncReturnsAtOnceWhileAReconnectIsUnderWay() throws Exception {
        try (Fake fake = stallingAfterRestart("hello")) {
            SocketEngine engine = SocketEngine.connect(fake.path.toString(), 500);
            fake.dropAll();
            Thread.sleep(100);
            long started = System.nanoTime();
            CompletableFuture<EngineResult> call = engine.predictAsync("jeff.safety.spam", List.of(row("x")));
            long returnedMs = (System.nanoTime() - started) / 1_000_000;
            assertTrue(returnedMs < 100, "predictAsync blocked its caller for " + returnedMs + " ms");
            Throwable error = failureOf(call, 3000);
            assertEquals("No engine is listening at " + fake.path + ": No hello from the engine at " + fake.path
                + " within 500 ms (reconnect attempt)", error.getMessage());
            engine.close();
        }
    }

    @Test
    void aFailedFirstConnectIsNeverRetried() throws Exception {
        int[] connections = {0};
        try (Fake fake = new Fake((index, socket) -> {
            synchronized (connections) {
                connections[0] += 1;
            }
            // reads the hello, then closes the connection without answering
            Protocol.readFrameBody(socket);
        })) {
            String message = assertThrows(JeffError.EngineError.class, () -> SocketEngine.connect(fake.path.toString(), 2000)).getMessage();
            assertEquals("The engine at " + fake.path + " closed the connection during the handshake", message);
            Thread.sleep(1200);
            synchronized (connections) {
                assertEquals(1, connections[0]);
            }
            assertEquals(List.of(), engineThreads(fake.path));
        }
    }
}
