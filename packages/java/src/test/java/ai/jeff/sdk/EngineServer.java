package ai.jeff.sdk;

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
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;

/** A fake engine on a Unix socket, like `jeff runtime serve` over a fake model: answers hello, installed, health and
 * predict (with `answers`), and refuses every serve (recording its adapter list) with an UnknownFunction, as an
 * engine refuses adapters it was not given at its start. */
final class EngineServer implements AutoCloseable {
    interface Answers {
        double[] answer(String adapter, Map<String, Object> row);
    }

    private final Path path;
    private final Path ownDirectory;
    private final ServerSocketChannel server;
    private final Set<SocketChannel> sockets = ConcurrentHashMap.newKeySet();
    private volatile boolean answering = true;
    /** The adapter list of each serve request read, in order. */
    final List<List<String>> serveRequests = java.util.Collections.synchronizedList(new ArrayList<>());

    private EngineServer(Path path, Path ownDirectory, ServerSocketChannel server) {
        this.path = path;
        this.ownDirectory = ownDirectory;
        this.server = server;
    }

    static Path socketDirectory() {
        try {
            return Files.createTempDirectory("jeffj");
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }

    /** A frame as an engine writes it: no schema check, any map. */
    static byte[] frame(Map<String, Object> message) {
        byte[] frame = MsgPack.encode(message, 4);
        int length = frame.length - 4;
        frame[0] = (byte) (length >>> 24);
        frame[1] = (byte) (length >>> 16);
        frame[2] = (byte) (length >>> 8);
        frame[3] = (byte) length;
        return frame;
    }

    static EngineServer start(Answers answers, List<String> installed) {
        return start(answers, installed, null, 0);
    }

    static EngineServer start(Answers answers, List<String> installed, Path path, long predictDelayMs) {
        Path own = path == null ? socketDirectory() : null;
        Path socket = path == null ? own.resolve("e.sock") : path;
        try {
            ServerSocketChannel server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            server.bind(UnixDomainSocketAddress.of(socket));
            EngineServer engine = new EngineServer(socket, own, server);
            Thread.ofVirtual().start(() -> engine.accept(answers, installed, predictDelayMs));
            return engine;
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }

    Path path() {
        return path;
    }

    void stopAnswering() {
        answering = false;
    }

    private void accept(Answers answers, List<String> installed, long predictDelayMs) {
        while (true) {
            SocketChannel socket;
            try {
                socket = server.accept();
            } catch (IOException closed) {
                return; // the server was closed by close()
            }
            sockets.add(socket);
            Thread.ofVirtual().start(() -> serve(socket, answers, installed, predictDelayMs));
        }
    }

    private void serve(SocketChannel socket, Answers answers, List<String> installed, long predictDelayMs) {
        ReentrantLock writing = new ReentrantLock();
        try {
            while (true) {
                byte[] body = Protocol.readFrameBody(socket);
                if (body == null) return;
                Map<String, Object> message = Values.map(MsgPack.decode(body));
                Object id = message.get("id");
                Map<String, Object> reply;
                switch ((String) message.get("op")) {
                    case "hello" -> reply = Fixtures.ordered("op", "hello", "version", 2L, "adapters_version", 1L);
                    case "installed" -> {
                        List<Object> adapters = new ArrayList<>(List.of("base"));
                        adapters.addAll(installed);
                        reply = Fixtures.ordered("id", id, "ok", true, "result", Fixtures.ordered("adapters", adapters, "adapters_version", 1L));
                    }
                    case "health" -> reply = Fixtures.ordered("id", id, "ok", true, "result", Fixtures.ordered("status", "ready",
                        "backend", "fake", "adapters", new ArrayList<Object>(installed), "adapters_version", 1L, "base",
                        Fixtures.ordered("id", "jeff-base", "version", "v1.3", "checkpoint", "fake", "max_options", 252L, "format", "bf16")));
                    case "serve" -> {
                        List<String> asked = Values.strings(message.get("adapters"));
                        serveRequests.add(asked);
                        List<String> unknown = new ArrayList<>();
                        for (String adapter : asked) if (!installed.contains(adapter)) unknown.add(adapter);
                        reply = Fixtures.ordered("id", id, "ok", false, "error", Fixtures.ordered("type", "UnknownFunction",
                            "message", "not available to serve from this engine: " + String.join(", ", PyFormat.sorted(unknown)),
                            "details", Fixtures.ordered("adapters_version", 1L)));
                    }
                    case "predict" -> {
                        if (!answering) continue;
                        List<Object> probabilities = new ArrayList<>();
                        for (Object row : Values.list(message.get("rows"))) {
                            List<Object> answer = new ArrayList<>();
                            for (double value : answers.answer((String) message.get("adapter"), Values.map(row))) answer.add(value);
                            probabilities.add(answer);
                        }
                        reply = Fixtures.ordered("id", id, "ok", true, "result", Fixtures.ordered("probabilities", probabilities,
                            "timing", Fixtures.ordered("path", "fake", "model_ms", 0.5, "format", "bf16"), "adapters_version", 1L));
                    }
                    default -> throw new AssertionError("EngineServer does not answer " + message.get("op"));
                }
                byte[] bytes = frame(reply);
                if (message.get("op").equals("predict") && predictDelayMs > 0) {
                    Async.TIMERS.schedule(() -> write(socket, writing, bytes), predictDelayMs, TimeUnit.MILLISECONDS);
                } else {
                    write(socket, writing, bytes);
                }
            }
        } catch (IOException closed) {
            // the client or close() ended the connection
        }
    }

    private static void write(SocketChannel socket, ReentrantLock writing, byte[] bytes) {
        writing.lock();
        try {
            ByteBuffer buffer = ByteBuffer.wrap(bytes);
            while (buffer.hasRemaining()) socket.write(buffer);
        } catch (IOException closed) {
            // the client went away before its reply
        } finally {
            writing.unlock();
        }
    }

    @Override
    public void close() {
        try {
            server.close();
            for (SocketChannel socket : sockets) socket.close();
            Files.deleteIfExists(path);
            if (ownDirectory != null) Files.deleteIfExists(ownDirectory);
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }
}
