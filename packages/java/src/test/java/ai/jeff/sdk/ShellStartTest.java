package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

/** The start command run by real shells (contract/local-engine.md, "Starting"), with a fake engine that writes its
 * process ID and process group ID and sleeps: the engine leads a process group of its own, and the check exits with
 * 125 when job control cannot give it one. No engine and no uvx are run. */
class ShellStartTest {
    private final Path folder;
    private final List<Long> started = new ArrayList<>();

    ShellStartTest() throws IOException {
        folder = Files.createTempDirectory(Path.of("/tmp"), "jg");
    }

    @AfterEach
    void cleanUp() throws IOException {
        Path pid = folder.resolve("ids");
        if (Files.exists(pid)) {
            String text = Files.readString(pid).strip();
            if (!text.isEmpty()) started.add(Long.parseLong(text.split("\\s+")[0]));
        }
        for (long id : started) ProcessHandle.of(id).ifPresent(ProcessHandle::destroyForcibly);
        try (Stream<Path> files = Files.walk(folder)) {
            for (Path file : files.sorted(Comparator.reverseOrder()).toList()) Files.deleteIfExists(file);
        }
    }

    private Path script(String name, String text) throws IOException {
        Path file = folder.resolve(name);
        Files.writeString(file, text, StandardCharsets.UTF_8);
        Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rwxr-xr-x"));
        return file;
    }

    /** A fake engine that writes "<pid> <pgid>" into ids (whole, through a rename) and sleeps. */
    private Path fakeEngine(int sleepSeconds) throws IOException {
        return script("engine", "#!/bin/sh\n"
            + "echo \"$$ $(ps -o pgid= -p $$)\" > '" + folder.resolve("ids.tmp") + "'\n"
            + "mv '" + folder.resolve("ids.tmp") + "' '" + folder.resolve("ids") + "'\n"
            + "sleep " + sleepSeconds + "\n");
    }

    private Process run(List<String> argv) throws IOException {
        Files.write(folder.resolve("engine.log"), new byte[0]);
        Process process = new ProcessBuilder(argv).directory(folder.toFile())
            .redirectInput(ProcessBuilder.Redirect.from(new File("/dev/null")))
            .redirectOutput(ProcessBuilder.Redirect.DISCARD).redirectError(ProcessBuilder.Redirect.DISCARD).start();
        started.add(process.pid());
        return process;
    }

    private static long groupOf(long pid) throws IOException, InterruptedException {
        Process ps = new ProcessBuilder("ps", "-o", "pgid=", "-p", Long.toString(pid)).redirectErrorStream(true).start();
        String text = new String(ps.getInputStream().readAllBytes(), StandardCharsets.UTF_8).strip();
        assertEquals(0, ps.waitFor(), text);
        return Long.parseLong(text);
    }

    /** Runs the command to its end and checks the fake engine led a process group of its own. */
    private void assertOwnGroup(List<String> argv) throws Exception {
        Process process = run(argv);
        assertTrue(process.waitFor(20, TimeUnit.SECONDS), "the start command did not end");
        assertEquals(0, process.exitValue(), "the shell's status is the engine's; log: " + Files.readString(folder.resolve("engine.log")));
        String[] ids = Files.readString(folder.resolve("ids")).strip().split("\\s+");
        assertEquals(ids[0], ids[1], "the engine leads its own process group");
        assertNotEquals(groupOf(ProcessHandle.current().pid()), Long.parseLong(ids[1]), "the engine is not in the caller's group");
    }

    @Test
    void theLinuxCommandUnderDashGivesTheEngineItsOwnGroup() throws Exception {
        assumeTrue(Files.isExecutable(Path.of("/bin/dash")), "/bin/dash is not installed");
        String setsid = Files.isExecutable(Path.of("/usr/bin/setsid")) ? "/usr/bin/setsid"
            : script("setsid", "#!/usr/bin/perl\nuse POSIX; POSIX::setsid() or die \"setsid: $!\"; exec @ARGV or die \"exec: $!\";\n").toString();
        List<String> argv = new ArrayList<>(LocalEngine.shellStart("linux", setsid, List.of(fakeEngine(2).toString())));
        argv.set(0, "/bin/dash");
        assertOwnGroup(argv);
    }

    @Test
    void theMacCommandUnderBashGivesTheEngineItsOwnGroup() throws Exception {
        assumeTrue(Files.isExecutable(Path.of("/bin/bash")), "/bin/bash is not installed");
        assertOwnGroup(LocalEngine.shellStart("macos", null, List.of(fakeEngine(2).toString())));
    }

    @Test
    void theCheckStopsAnEngineLeftInTheCallersGroupAndExitsWith125() throws Exception {
        assumeTrue(Files.isExecutable(Path.of("/bin/dash")), "/bin/dash is not installed");
        List<String> argv = new ArrayList<>(LocalEngine.shellStart("macos", null, List.of(fakeEngine(8).toString())));
        argv.set(0, "/bin/dash"); // dash's set -m needs a terminal: the engine stays in the shell's group
        long begun = System.nanoTime();
        Process process = run(argv);
        assertTrue(process.waitFor(10, TimeUnit.SECONDS), "the start command did not end");
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - begun);
        assertEquals(LocalEngine.NOT_OWN_GROUP, process.exitValue());
        assertTrue(elapsedMs < 7000, elapsedMs + " ms");
        String[] ids = Files.readString(folder.resolve("ids")).strip().split("\\s+");
        assertNotEquals(ids[0], ids[1], "the engine was left in another program's group");
    }
}
