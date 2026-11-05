package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/** socket.json: byte-exact conversations of the Python SDK's socket client with an engine, replayed against an engine
 * that plays the recorded steps on the first connection and records every frame it reads. */
class SocketConformanceTest {
    private static final byte[] END = new byte[0];

    private static final class Scripted implements AutoCloseable {
        final Path directory = EngineServer.socketDirectory();
        final Path path = directory.resolve("e.sock");
        final ServerSocketChannel server;
        final List<String> received = Collections.synchronizedList(new ArrayList<>());
        final AtomicInteger arrived = new AtomicInteger();
        final CountDownLatch done = new CountDownLatch(1);
        private final BlockingQueue<byte[]> frames = new LinkedBlockingQueue<>();

        Scripted(List<Object> steps) throws IOException {
            server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            server.bind(UnixDomainSocketAddress.of(path));
            Thread.ofVirtual().start(() -> play(steps));
        }

        private void readRaw(SocketChannel socket) {
            try {
                while (true) {
                    ByteBuffer header = ByteBuffer.allocate(4);
                    while (header.hasRemaining()) if (socket.read(header) < 0) throw new IOException("end");
                    int length = header.getInt(0);
                    ByteBuffer frame = ByteBuffer.allocate(4 + length);
                    frame.put(header.array());
                    while (frame.hasRemaining()) if (socket.read(frame) < 0) throw new IOException("end");
                    frames.add(frame.array());
                    arrived.incrementAndGet();
                }
            } catch (IOException ended) {
                frames.add(END); // the client closed the connection (or the script closed it)
            }
        }

        private void play(List<Object> steps) {
            try (SocketChannel socket = server.accept()) {
                Thread.ofVirtual().start(() -> readRaw(socket));
                for (Object item : steps) {
                    Map<String, Object> step = Values.map(item);
                    if (step.containsKey("client")) {
                        byte[] got = frames.poll(5, TimeUnit.SECONDS);
                        received.add(got == null ? "<timeout>" : got == END ? "null" : Fixtures.base64(got));
                        if (got == null || got == END) break;
                    } else if (step.containsKey("engine")) {
                        ByteBuffer bytes = ByteBuffer.wrap(Fixtures.fromBase64((String) step.get("engine")));
                        while (bytes.hasRemaining()) socket.write(bytes);
                    } else {
                        break; // {"close": true}: the try-with-resources closes the connection
                    }
                }
            } catch (IOException | InterruptedException error) {
                throw new IllegalStateException("the scripted engine failed", error);
            } finally {
                done.countDown();
            }
        }

        void awaitArrived(int count) throws InterruptedException {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            while (arrived.get() < count) {
                if (System.nanoTime() > deadline) throw new AssertionError("the engine never read frame " + count);
                Thread.sleep(1);
            }
        }

        @Override
        public void close() throws IOException {
            server.close();
            Files.deleteIfExists(path);
            Files.deleteIfExists(directory);
        }
    }

    private static Map<String, Object> failed(JeffError error, Path path) {
        Map<String, Object> wire = new LinkedHashMap<>();
        wire.put("type", error.type());
        wire.put("message", error.getMessage().replace(path.toString(), "{socket}"));
        return Map.of("error", wire);
    }

    private static List<Row> rowsOf(Map<String, Object> action) {
        if (action.containsKey("rows")) {
            List<Row> rows = new ArrayList<>();
            for (Object row : Values.list(action.get("rows"))) rows.add(Row.fromWire(Values.map(row)));
            return rows;
        }
        Map<String, Object> generated = Values.map(action.get("rows_generated"));
        Map<String, Object> question = Values.map(generated.get("question"));
        return List.of(new Row("x".repeat(((Number) generated.get("state_length")).intValue()),
            new RowQuestion((String) question.get("type"), (String) question.get("instructions"), question.get("criteria"))));
    }

    private static Object act(SocketEngine engine, Map<String, Object> action, Path path) {
        try {
            Object result = switch ((String) action.get("op")) {
                case "predict" -> {
                    EngineResult answer = engine.predict((String) action.get("adapter"), rowsOf(action), (Number) action.get("deadline_ms"));
                    List<Object> probabilities = new ArrayList<>();
                    for (double[] row : answer.probabilities()) {
                        List<Object> values = new ArrayList<>();
                        for (double value : row) values.add(value);
                        probabilities.add(values);
                    }
                    Map<String, Object> out = new LinkedHashMap<>();
                    out.put("probabilities", probabilities);
                    out.put("timing", new LinkedHashMap<>(answer.timing()));
                    out.put("input_tokens", answer.inputTokens() == null ? null : new ArrayList<>(answer.inputTokens()));
                    yield out;
                }
                case "installed" -> Map.of("installed", PyFormat.sorted(engine.installed(true)));
                case "health" -> engine.health();
                default -> throw new AssertionError("Unknown socket action " + action.get("op"));
            };
            Map<String, Object> wrapped = new LinkedHashMap<>();
            wrapped.put("result", result);
            return wrapped;
        } catch (JeffError error) {
            return failed(error, path);
        }
    }

    private static boolean concurrent(List<Object> actions, int index) {
        return index < actions.size() && Boolean.TRUE.equals(Values.map(actions.get(index)).get("concurrent"));
    }

    static Stream<Arguments> cases() {
        return Fixtures.named(Fixtures.cases("socket.json"));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    void theConversationIsThePythonSdks(String name, Map<String, Object> item) throws Exception {
        List<Object> steps = Values.list(item.get("steps"));
        try (Scripted server = new Scripted(steps)) {
            List<Object> results = new ArrayList<>();
            SocketEngine engine = null;
            try {
                engine = SocketEngine.connect(server.path.toString(), 5000);
                Map<String, Object> connected = new LinkedHashMap<>();
                connected.put("base_format", engine.baseFormat().toMap());
                connected.put("base", engine.base());
                connected.put("installed", PyFormat.sorted(engine.installed(false)));
                results.add(Map.of("result", connected));
            } catch (JeffError error) {
                results.add(failed(error, server.path));
            }
            List<Object> actions = Values.list(item.get("actions"));
            int index = 1;
            while (engine != null && index < actions.size()) {
                List<Integer> group = new ArrayList<>(List.of(index));
                while (concurrent(actions, group.getLast()) && concurrent(actions, group.getLast() + 1)) group.add(group.getLast() + 1);
                List<CompletableFuture<Object>> running = new ArrayList<>();
                for (int member : group) {
                    int before = server.arrived.get();
                    SocketEngine target = engine;
                    Map<String, Object> action = Values.map(actions.get(member));
                    running.add(Async.run(() -> act(target, action, server.path)));
                    // each concurrent action starts after the engine has read the previous one's frame
                    if (group.size() > 1) server.awaitArrived(before + 1);
                }
                for (CompletableFuture<Object> future : running) results.add(future.join());
                index = group.getLast() + 1;
            }
            if (engine != null) engine.close();
            assertTrue(server.done.await(10, TimeUnit.SECONDS), "the scripted engine did not finish");
            assertEquals(item.get("results"), results);
            List<Object> clientFrames = new ArrayList<>();
            for (Object step : steps) if (Values.map(step).containsKey("client")) clientFrames.add(Values.map(step).get("client"));
            assertEquals(clientFrames, new ArrayList<>(server.received));
        } catch (UncheckedIOException error) {
            throw error.getCause();
        }
    }
}
