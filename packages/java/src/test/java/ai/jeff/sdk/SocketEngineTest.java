package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.LongFunction;
import org.junit.jupiter.api.Test;

class SocketEngineTest {
    private static final Map<String, Object> CTX = Fixtures.ordered("company", "A shop.", "channel", "email");
    private static final Row SPAM_ROW = new Row("x", new RowQuestion("noul", "?", null));

    /** The urgency level a fake engine answers: the last digit of the message, certainly. */
    private static double[] levelOf(String adapter, Map<String, Object> row) {
        String message = (String) Values.map(row.get("state")).get("message");
        int level = message.charAt(message.length() - 1) - '0';
        double[] answer = new double[5];
        answer[level] = 1;
        return answer;
    }

    @Test
    void jeffLocalDecidesOverTheEngineSocket() {
        try (EngineServer server = EngineServer.start(SocketEngineTest::levelOf, List.of("jeff.support.triage"));
             Jeff jeff = Jeff.local(server.path().toString(), LocalOptions.timeoutMs(5000))) {
            assertEquals(3L, jeff.decide(List.of(new Call("triage.urgency", "level 3", CTX, null))).get(0).value());
        }
    }

    @Test
    void concurrentCallersShareOneConnectionAndEachGetsItsOwnAnswer() {
        try (EngineServer server = EngineServer.start(SocketEngineTest::levelOf, List.of("jeff.support.triage"));
             Jeff jeff = Jeff.local(server.path().toString(), LocalOptions.timeoutMs(5000))) {
            List<CompletableFuture<List<Decision>>> running = new ArrayList<>();
            for (int index = 0; index < 40; index++) {
                running.add(jeff.decideAsync(List.of(new Call("triage.urgency", "case " + (index % 5), CTX, null))));
            }
            for (int index = 0; index < 40; index++) {
                assertEquals((long) (index % 5), Async.join(running.get(index)).get(0).value());
            }
        }
    }

    @Test
    void noEngineOnTheSocketNamesThePathAndTheCommandThatStartsOne() {
        String message = assertThrows(JeffError.EngineError.class,
            () -> Jeff.local("/tmp/jeff-no-such-engine.sock", LocalOptions.timeoutMs(1000))).getMessage();
        assertTrue(message.startsWith("No engine is listening at /tmp/jeff-no-such-engine.sock: "), message);
        assertTrue(message.contains(". Start one with: jeff runtime serve --socket /tmp/jeff-no-such-engine.sock --adapter "), message);
        assertEquals("timeoutMs must be a whole number of milliseconds above 0; got 0",
            assertThrows(JeffError.InvalidRequest.class, () -> Jeff.local("/tmp/x.sock", LocalOptions.timeoutMs(0))).getMessage());
    }

    @Test
    void aCallWithoutAReplyWithinTheTimeoutFailsPromptlyNamingTheEngine() {
        try (EngineServer server = EngineServer.start((adapter, row) -> new double[] {1, 0}, List.of("jeff.safety.spam"))) {
            SocketEngine engine = SocketEngine.connect(server.path().toString(), 200);
            server.stopAnswering();
            long started = System.nanoTime();
            JeffError.EngineError error = assertThrows(JeffError.EngineError.class, () -> engine.predict("jeff.safety.spam", List.of(SPAM_ROW)));
            long elapsedMs = (System.nanoTime() - started) / 1_000_000;
            assertEquals("No reply from the engine at " + server.path() + " within 200 ms", error.getMessage());
            assertTrue(elapsedMs >= 190 && elapsedMs < 400, elapsedMs + " ms");
            engine.close();
        }
    }

    @Test
    void afterTheEngineRestartsTheNextCallReconnectsOnceAndACallRightAfterAFailedAttemptFailsAtOnce() throws Exception {
        Path directory = EngineServer.socketDirectory();
        Path path = directory.resolve("e.sock");
        try {
            EngineServer first = EngineServer.start((adapter, row) -> new double[] {0.2, 0.8}, List.of("jeff.safety.spam"), path, 0);
            SocketEngine engine = SocketEngine.connect(path.toString(), 2000);
            first.close();
            Thread.sleep(50);
            assertTrue(assertThrows(JeffError.EngineError.class, () -> engine.predict("jeff.safety.spam", List.of(SPAM_ROW)))
                .getMessage().contains("(reconnect attempt)"));
            assertTrue(assertThrows(JeffError.EngineError.class, () -> engine.predict("jeff.safety.spam", List.of(SPAM_ROW)))
                .getMessage().contains("the last reconnect attempt"));
            Thread.sleep(1100);
            try (EngineServer second = EngineServer.start((adapter, row) -> new double[] {0.2, 0.8}, List.of("jeff.safety.spam"), path, 0)) {
                assertEquals(path, second.path());
                assertEquals(0.8, engine.predict("jeff.safety.spam", List.of(SPAM_ROW)).probabilities().get(0)[1]);
                assertEquals(1, engine.reconnects());
                engine.close();
            }
        } finally {
            Files.deleteIfExists(path);
            Files.deleteIfExists(directory);
        }
    }

    @Test
    void aClosedEngineRefusesLaterCalls() {
        try (EngineServer server = EngineServer.start((adapter, row) -> new double[] {1, 0}, List.of())) {
            SocketEngine engine = SocketEngine.connect(server.path().toString(), 1000);
            engine.close();
            assertTrue(assertThrows(JeffError.EngineError.class, engine::health).getMessage().contains("was closed by its caller"));
        }
    }

    /** An engine that writes `onHello` after reading the hello and onInstalled(id) after reading an installed
     * request; it counts connections and records each op it reads. */
    private record RawEngine(Path directory, Path path, ServerSocketChannel server, List<String> ops, AtomicInteger connections)
        implements AutoCloseable {
        static RawEngine start(byte[] onHello, LongFunction<byte[]> onInstalled) throws IOException {
            Path directory = EngineServer.socketDirectory();
            Path path = directory.resolve("e.sock");
            ServerSocketChannel server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            server.bind(UnixDomainSocketAddress.of(path));
            RawEngine engine = new RawEngine(directory, path, server, Collections.synchronizedList(new ArrayList<>()), new AtomicInteger());
            Thread.ofVirtual().start(() -> {
                while (true) {
                    SocketChannel socket;
                    try {
                        socket = server.accept();
                    } catch (IOException closed) {
                        return; // close() closed the server
                    }
                    engine.connections().incrementAndGet();
                    Thread.ofVirtual().start(() -> {
                        try (socket) {
                            for (byte[] body = Protocol.readFrameBody(socket); body != null; body = Protocol.readFrameBody(socket)) {
                                Map<String, Object> message = Values.map(MsgPack.decode(body));
                                engine.ops().add((String) message.get("op"));
                                byte[] reply = message.get("op").equals("hello") ? onHello
                                    : message.get("op").equals("installed") && onInstalled != null
                                        ? onInstalled.apply(((Number) message.get("id")).longValue()) : null;
                                if (reply != null) socket.write(ByteBuffer.wrap(reply));
                            }
                        } catch (IOException ended) {
                            // the client destroyed the connection once it failed
                        }
                    });
                }
            });
            return engine;
        }

        @Override
        public void close() throws IOException {
            server.close();
            Files.deleteIfExists(path);
            Files.deleteIfExists(directory);
        }
    }

    private static byte[] concat(byte[] a, byte[] b) {
        byte[] out = new byte[a.length + b.length];
        System.arraycopy(a, 0, out, 0, a.length);
        System.arraycopy(b, 0, out, a.length, b.length);
        return out;
    }

    private static final byte[] ENGINE_HELLO = EngineServer.frame(Fixtures.ordered("op", "hello", "version", 2L, "adapters_version", 1L));
    private static final byte[] ID_ZERO_FAILURE = EngineServer.frame(Fixtures.ordered("id", 0L, "ok", false, "error",
        Fixtures.ordered("type", "InvalidRequest", "message", "Frame could not be read", "details", Fixtures.ordered("adapters_version", 1L))));

    @Test
    void aFailureWithIdZeroSentInTheSameBytesAsTheHelloFailsTheConnectWithoutAReconnect() throws Exception {
        try (RawEngine engine = RawEngine.start(concat(ENGINE_HELLO, ID_ZERO_FAILURE), null)) {
            assertEquals("The engine at " + engine.path() + " closed the connection after: InvalidRequest: Frame could not be read",
                assertThrows(JeffError.EngineError.class, () -> SocketEngine.connect(engine.path().toString(), 2000)).getMessage());
            assertEquals(1, engine.connections().get());
        }
    }

    /** The failure with id 0 comes right after the installed reply. The Node SDK reads both frames before its caller
     * resumes, so it always fails the connect with the closed-connection error; here the reader thread and the
     * connecting thread run side by side, so the health request may already be waiting when the failure is read and
     * then fails with the failure itself. Either way the connect fails naming it, and no reconnect is made. */
    @Test
    void aConnectionThatClosesWhileConnectingFailsTheConnectAtOnceWithoutAReconnect() throws Exception {
        LongFunction<byte[]> installedReply = id -> concat(EngineServer.frame(Fixtures.ordered("id", id, "ok", true, "result",
            Fixtures.ordered("adapters", List.of("base"), "adapters_version", 1L))), ID_ZERO_FAILURE);
        try (RawEngine engine = RawEngine.start(ENGINE_HELLO, installedReply)) {
            String message = assertThrows(JeffError.class, () -> SocketEngine.connect(engine.path().toString(), 2000)).getMessage();
            assertTrue(message.endsWith("Frame could not be read"), message);
            assertEquals(List.of("hello", "installed"), List.copyOf(engine.ops()).subList(0, 2));
            assertEquals(1, engine.connections().get());
        }
    }

    @Test
    void aWaitingCallKeepsTheJvmAliveAlthoughTheIdleSocketDoesNot() throws Exception {
        try (EngineServer server = EngineServer.start((adapter, row) -> new double[] {0.3, 0.7}, List.of("jeff.safety.spam"), null, 300)) {
            String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
            Process process = new ProcessBuilder(java, "-cp", System.getProperty("java.class.path"), "ai.jeff.sdk.OneCall",
                server.path().toString()).redirectErrorStream(true).start();
            assertTrue(process.waitFor(20, TimeUnit.SECONDS), "the child JVM did not exit: an idle connection kept it alive");
            assertEquals("[[0.3,0.7]]", new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8).trim());
        }
    }
}
