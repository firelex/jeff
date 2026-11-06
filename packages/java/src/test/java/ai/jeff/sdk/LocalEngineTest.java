package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/** Local mode with a list of adapters, against a fake uvx: a script on the test's PATH that records how it was run
 * and then plays the engine (sleeping while the test starts a stub engine on the socket it was given), exits, or
 * sleeps without listening. Nothing here starts a real engine or runs the real uvx. */
class LocalEngineTest {
    private static final Map<String, Object> CTX = Fixtures.ordered("company", "A shop.", "channel", "email");
    private static final String SLEEPS = "exec sleep 60\n";

    private static double[] levelOf(String adapter, Map<String, Object> row) {
        String message = (String) Values.map(row.get("state")).get("message");
        double[] answer = new double[5];
        answer[message.charAt(message.length() - 1) - '0'] = 1;
        return answer;
    }

    /** A short temporary HOME (Unix socket paths are limited to about 104 bytes on macOS), a PATH folder for the fake
     * uvx, and the lines local mode prints. Every process the fake uvx started is killed on close. */
    private static final class Sandbox implements AutoCloseable {
        final Path root;
        final Path home;
        final Path bin;
        final List<String> stderr = Collections.synchronizedList(new ArrayList<>());

        Sandbox() {
            try {
                root = Files.createTempDirectory(Path.of("/tmp"), "jj");
                home = Files.createDirectory(root.resolve("h"));
                bin = Files.createDirectory(root.resolve("b"));
            } catch (IOException error) {
                throw new UncheckedIOException(error);
            }
        }

        LocalEngine.Environment environment() {
            return environment("macos", "arm64", () -> false);
        }

        LocalEngine.Environment environment(String os, String arch, BooleanSupplier nvidia) {
            return new LocalEngine.Environment(home.toString(), bin.toString(), null, os, arch, nvidia, LocalEngine::isExecutable, stderr::add);
        }

        Path folder() {
            return home.resolve("Library/Caches/jeff/engine");
        }

        Path socket() {
            return folder().resolve("engine.sock");
        }

        Path uvxPath() {
            return bin.resolve("uvx");
        }

        /** The fake uvx: records its process ID, its command line ($0 first) and each run, then runs {@code body}. */
        void uvx(String body) throws IOException {
            uvx("", body);
        }

        /** The fake uvx, running {@code first} before it records anything. */
        void uvx(String first, String body) throws IOException {
            String script = "#!/bin/sh\n" + first
                + "echo $$ >> '" + root.resolve("pids") + "'\n"
                + "echo run >> '" + root.resolve("runs") + "'\n"
                + "printf '%s\\n' \"$0\" \"$@\" > '" + root.resolve("args.tmp") + "'\n"
                + "mv '" + root.resolve("args.tmp") + "' '" + root.resolve("args") + "'\n"
                + body;
            Files.writeString(uvxPath(), script);
            Files.setPosixFilePermissions(uvxPath(), PosixFilePermissions.fromString("rwxr-xr-x"));
        }

        int runs() throws IOException {
            Path runs = root.resolve("runs");
            return Files.exists(runs) ? Files.readAllLines(runs).size() : 0;
        }

        /** The fake uvx's command line, once it has run. */
        List<String> awaitArgs() throws Exception {
            Path args = root.resolve("args");
            long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
            while (!Files.exists(args)) {
                if (System.nanoTime() > until) throw new AssertionError("the fake uvx did not run within 15 s");
                Thread.sleep(20);
            }
            return Files.readAllLines(args);
        }

        void awaitLine(String line) throws InterruptedException {
            long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
            while (!stderr.contains(line)) {
                if (System.nanoTime() > until) throw new AssertionError("not printed within 15 s: " + line + "; printed " + stderr);
                Thread.sleep(20);
            }
        }

        @Override
        public void close() throws IOException {
            Path pids = root.resolve("pids");
            if (Files.exists(pids)) {
                for (String pid : Files.readAllLines(pids)) {
                    ProcessHandle.of(Long.parseLong(pid.strip())).ifPresent(process -> {
                        process.descendants().forEach(ProcessHandle::destroyForcibly);
                        process.destroyForcibly();
                    });
                }
            }
            try (Stream<Path> files = Files.walk(root)) {
                for (Path file : files.sorted(Comparator.reverseOrder()).toList()) Files.deleteIfExists(file);
            }
        }
    }

    private static StartOptions quick() {
        return StartOptions.defaults().withTimeoutMs(5000).withStartTimeoutMs(30_000);
    }

    private static CompletableFuture<Jeff> localInBackground(List<String> adapters, StartOptions options, LocalEngine.Environment environment) {
        CompletableFuture<Jeff> call = new CompletableFuture<>();
        Thread.ofPlatform().daemon(true).start(() -> {
            try {
                call.complete(Jeff.local(adapters, options, environment));
            } catch (Throwable error) {
                call.completeExceptionally(error);
            }
        });
        return call;
    }

    private static long urgency(Jeff jeff, String message) {
        return (Long) jeff.decide(List.of(new Call("triage.urgency", message, CTX, null))).get(0).value();
    }

    @Test
    void startsTheEngineThroughUvxWithTheContractsCommandAndASecondClientReusesIt() throws Exception {
        try (Sandbox box = new Sandbox()) {
            box.uvx(SLEEPS);
            CompletableFuture<Jeff> call = localInBackground(List.of("triage"), quick(), box.environment());
            List<String> argv = box.awaitArgs();
            assertEquals(LocalEngine.command(box.uvxPath().toString(), "mac", "1.4.1", box.folder(), List.of("jeff.support.triage"),
                "jeff-base@v1.4"), argv);
            assertEquals(box.socket().toString(), argv.get(argv.indexOf("--socket") + 1));
            try (EngineServer server = EngineServer.start(LocalEngineTest::levelOf, List.of("jeff.support.triage"), box.socket(), 0);
                 Jeff jeff = call.get(20, TimeUnit.SECONDS)) {
                assertEquals(3L, urgency(jeff, "level 3"));
                assertEquals(List.of(LocalEngine.startingLine(List.of("jeff.support.triage"), box.folder().resolve("engine.log"))), box.stderr);
                assertFalse(Files.exists(box.folder().resolve("engine.lock")), "the start lock is released");
                assertEquals("rwx------", PosixFilePermissions.toString(Files.getPosixFilePermissions(box.folder())));
                // a second client, and a second call in the same program, use the running engine
                try (Jeff second = Jeff.local(List.of("triage", "jeff.support.triage"), quick(), box.environment())) {
                    assertEquals(4L, urgency(second, "level 4"));
                }
                try (Jeff third = Jeff.local(List.of("triage"), quick(), box.environment())) {
                    assertEquals(1L, urgency(third, "level 1"));
                }
                assertEquals(1, box.runs(), "uvx ran once");
                assertEquals(1, box.stderr.size());
                assertEquals(List.of(), server.serveRequests);
            }
        }
    }

    @Test
    void anEngineLackingAnAdapterIsAskedToServeItOnceAndItsRefusalIsNotInstalled() throws Exception {
        try (Sandbox box = new Sandbox()) {
            Files.createDirectories(box.folder());
            try (EngineServer server = EngineServer.start(LocalEngineTest::levelOf, List.of("jeff.support.triage"), box.socket(), 0)) {
                JeffError.NotInstalled error = assertThrows(JeffError.NotInstalled.class,
                    () -> Jeff.local(List.of("triage", "spam", "guard"), quick(), box.environment()));
                assertEquals(LocalEngine.notLoadable(box.socket(), List.of("jeff.safety.spam", "jeff.safety.guard"), "UnknownFunction",
                    "not available to serve from this engine: jeff.safety.guard, jeff.safety.spam").getMessage(), error.getMessage());
                assertEquals(List.of(List.of("jeff.support.triage", "jeff.safety.spam", "jeff.safety.guard")), server.serveRequests);
                assertEquals(0, box.runs());
                assertEquals(List.of(), box.stderr);
            }
        }
    }

    @Test
    void uvMissingIsRuntimeMissing() throws Exception {
        try (Sandbox box = new Sandbox()) {
            JeffError.RuntimeMissing error = assertThrows(JeffError.RuntimeMissing.class,
                () -> Jeff.local(List.of("guard"), quick(), box.environment()));
            assertEquals("Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at " + box.home + "/.local/bin/uvx or "
                + box.home + "/.cargo/bin/uvx. Install uv once with: curl -LsSf https://astral.sh/uv/install.sh | sh (see "
                + "https://docs.astral.sh/uv/). Or start an engine yourself (jeff runtime serve) and give its socket instead of the "
                + "adapters", error.getMessage());
        }
    }

    @Test
    void anUnsupportedMachineIsRefusedBeforeAnythingIsLookedForOrRun() throws Exception {
        try (Sandbox box = new Sandbox()) {
            box.uvx(SLEEPS);
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class,
                () -> Jeff.local(List.of("guard"), quick(), box.environment("linux", "x86_64", () -> false)));
            assertTrue(error.getMessage().contains("this Linux machine has no NVIDIA GPU"), error.getMessage());
            assertThrows(JeffError.BackendUnavailable.class,
                () -> Jeff.local(List.of("guard"), quick(), box.environment("macos", "x86_64", () -> true)));
            assertEquals(0, box.runs());
            try (Stream<Path> made = Files.list(box.home)) {
                assertEquals(List.of(), made.toList(), "no engine folder is made");
            }
        }
    }

    @Test
    void theOptionsAreCheckedFirst() throws Exception {
        try (Sandbox box = new Sandbox()) {
            assertEquals("startTimeoutMs must be a whole number of milliseconds above 0; got 0", assertThrows(JeffError.InvalidRequest.class,
                () -> Jeff.local(List.of("nope"), StartOptions.defaults().withStartTimeoutMs(0), box.environment())).getMessage());
            assertEquals("timeoutMs must be a whole number of milliseconds above 0; got -1", assertThrows(JeffError.InvalidRequest.class,
                () -> Jeff.local(List.of(), StartOptions.defaults().withTimeoutMs(-1), box.environment())).getMessage());
            assertEquals(new StartOptions(60_000, 1_800_000), StartOptions.defaults());
        }
    }

    @Test
    void theSocketFormNeverStartsAnythingNorTouchesTheEngineFolder() throws Exception {
        try (Sandbox box = new Sandbox()) {
            box.uvx(SLEEPS);
            String message = assertThrows(JeffError.EngineError.class,
                () -> Jeff.local(box.socket().toString(), LocalOptions.timeoutMs(1000))).getMessage();
            assertTrue(message.startsWith("No engine is listening at " + box.socket() + ": "), message);
            assertEquals(0, box.runs());
            assertFalse(Files.exists(box.home.resolve("Library")));
        }
    }

    @Test
    void anEngineThatDoesNotAnswerWithinTheStartTimeoutIsStopped() throws Exception {
        try (Sandbox box = new Sandbox()) {
            Path signalled = box.root.resolve("term");
            box.uvx("echo 'Fetching 1%'\n", "trap \"echo TERM > '" + signalled + "'; exit 143\" TERM\nsleep 60 &\nwait $!\n");
            long started = System.nanoTime();
            JeffError.EngineError error = assertThrows(JeffError.EngineError.class,
                () -> Jeff.local(List.of("guard"), StartOptions.defaults().withTimeoutMs(1000).withStartTimeoutMs(3000), box.environment()));
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started);
            assertEquals(LocalEngine.startTimeout(box.socket(), 3000, box.folder().resolve("engine.log"), "Fetching 1%\n").getMessage(),
                error.getMessage());
            assertTrue(elapsedMs >= 3000 && elapsedMs < 10_000, elapsedMs + " ms");
            long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (!Files.exists(signalled)) {
                if (System.nanoTime() > until) throw new AssertionError("the engine's process group was not sent SIGTERM");
                Thread.sleep(20);
            }
            assertEquals("TERM\n", Files.readString(signalled));
            assertFalse(Files.exists(box.folder().resolve("engine.lock")));
        }
    }

    @Test
    void anEngineThatExitsWhileStartingIsAnEngineErrorWithItsStatusAndLog() throws Exception {
        try (Sandbox box = new Sandbox()) {
            box.uvx("echo 'warning: something'\necho 'ManifestError: bad' >&2\nexit 3\n");
            JeffError.EngineError error = assertThrows(JeffError.EngineError.class,
                () -> Jeff.local(List.of("guard"), quick(), box.environment()));
            assertEquals(LocalEngine.exited(3, box.folder().resolve("engine.log"), "warning: something\nManifestError: bad\n").getMessage(),
                error.getMessage());
            assertFalse(Files.exists(box.folder().resolve("engine.lock")));
        }
    }

    @Test
    void aStaleLockIsTakenOver() throws Exception {
        try (Sandbox box = new Sandbox()) {
            box.uvx(SLEEPS);
            Process gone = new ProcessBuilder("/bin/sh", "-c", "exit 0").start();
            gone.waitFor();
            Files.createDirectories(box.folder());
            Files.writeString(box.folder().resolve("engine.lock"), gone.pid() + "\n", StandardCharsets.US_ASCII);
            CompletableFuture<Jeff> call = localInBackground(List.of("triage"), quick(), box.environment());
            box.awaitArgs();
            try (EngineServer server = EngineServer.start(LocalEngineTest::levelOf, List.of("jeff.support.triage"), box.socket(), 0);
                 Jeff jeff = call.get(20, TimeUnit.SECONDS)) {
                assertEquals(2L, urgency(jeff, "level 2"));
                assertEquals(List.of(), server.serveRequests);
                assertEquals(1, box.runs());
                assertFalse(box.stderr.contains(LocalEngine.waitingLine(box.folder().resolve("engine.log"))));
                assertFalse(Files.exists(box.folder().resolve("engine.lock")));
            }
        }
    }

    @Test
    void aLiveLockIsWaitedForAndTheEngineItsHolderStartedIsUsed() throws Exception {
        try (Sandbox box = new Sandbox()) {
            box.uvx(SLEEPS);
            Files.createDirectories(box.folder());
            Path lock = box.folder().resolve("engine.lock");
            Files.writeString(lock, ProcessHandle.current().pid() + "\n", StandardCharsets.US_ASCII);
            CompletableFuture<Jeff> call = localInBackground(List.of("triage"), quick(), box.environment());
            String waiting = LocalEngine.waitingLine(box.folder().resolve("engine.log"));
            box.awaitLine(waiting);
            try (EngineServer server = EngineServer.start(LocalEngineTest::levelOf, List.of("jeff.support.triage"), box.socket(), 0)) {
                Files.delete(lock); // the other program has started its engine and released the lock
                try (Jeff jeff = call.get(20, TimeUnit.SECONDS)) {
                    assertEquals(0L, urgency(jeff, "level 0"));
                }
                assertEquals(List.of(waiting), box.stderr, "the waiting line is printed once, and nothing is started");
                assertEquals(List.of(), server.serveRequests);
                assertEquals(0, box.runs());
                assertFalse(Files.exists(lock));
            }
        }
    }

    @Test
    void aLockHeldLongerThanTheStartTimeoutIsAnEngineError() throws Exception {
        try (Sandbox box = new Sandbox()) {
            box.uvx(SLEEPS);
            Files.createDirectories(box.folder());
            Path lock = box.folder().resolve("engine.lock");
            String pid = Long.toString(ProcessHandle.current().pid());
            Files.writeString(lock, pid + "\n", StandardCharsets.US_ASCII);
            JeffError.EngineError error = assertThrows(JeffError.EngineError.class,
                () -> Jeff.local(List.of("triage"), StartOptions.defaults().withStartTimeoutMs(600), box.environment()));
            assertEquals(LocalEngine.lockTimeout(600, lock, pid, box.folder().resolve("engine.log")).getMessage(), error.getMessage());
            assertEquals(0, box.runs());
            assertTrue(Files.exists(lock), "another program's lock is left alone");
        }
    }
}
