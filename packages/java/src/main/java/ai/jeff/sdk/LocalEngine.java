package ai.jeff.sdk;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.channels.SeekableByteChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;
import java.util.function.Predicate;

/** Local mode with a list of adapters: finds the Jeff engine running for this user on this machine, or starts one
 * through uvx, then makes sure it serves the adapters asked for (contract/local-engine.md). The pure parts (platform,
 * folder, finding uvx, resolving adapters, the command line, the log's last lines, every message) are functions of
 * their inputs, so the conformance cases call them directly. */
final class LocalEngine {
    /** How often the lock and the starting engine are tried again. */
    static final long POLL_MS = 200;
    /** A lock file holding no process ID is stale once it is older than this. */
    static final long EMPTY_LOCK_STALE_MS = 10_000;
    static final String ENGINE_RUNS_ON = "The Jeff engine runs on macOS with Apple silicon, or on Linux with an NVIDIA GPU; ";
    static final String USE_A_SERVER = ", so local mode cannot start it here. Use a Jeff server instead (remote mode)";
    /** Waits up to 5 seconds for the engine to lead a process group of its own; if it is still running without one,
     * stops it and exits with status 125. */
    static final String GROUP_CHECK = "i=0; until kill -0 -$pid 2>/dev/null; do kill -0 $pid 2>/dev/null || break; i=$((i+1)); "
        + "if [ $i -ge 50 ]; then kill -TERM $pid; exit 125; fi; sleep 0.1; done";
    /** macOS: bash's job control gives the command after the script a process group of its own (Java cannot start a
     * process in its own group); a TERM to the shell is passed on to that group; the exit status is the command's. */
    static final String MAC_SCRIPT = "set -m; \"$0\" \"$@\" </dev/null >>engine.log 2>&1 & pid=$!; set +m; " + GROUP_CHECK
        + "; trap \"kill -TERM -$pid\" TERM; wait $pid";
    /** Linux: the command after the script is setsid, which makes the engine a new session and process group (dash's
     * set -m needs a terminal). */
    static final String LINUX_SCRIPT = "\"$0\" \"$@\" </dev/null >>engine.log 2>&1 & pid=$!; " + GROUP_CHECK
        + "; trap \"kill -TERM -$pid\" TERM; wait $pid";
    /** The status the start script exits with when the engine did not get a process group of its own. */
    static final int NOT_OWN_GROUP = 125;

    private LocalEngine() {}

    /** What local mode reads from the process: the variables it needs, the system, the NVIDIA probe, and where the
     * lines it prints go. Tests give their own; {@link #real()} is the process's own.
     *
     * @param home HOME (null when not set)
     * @param path PATH (null when not set)
     * @param xdgCacheHome XDG_CACHE_HOME (null when not set)
     * @param os the system, as the contract names it ({@code macos}, {@code linux}, {@code windows} or Java's name)
     * @param arch the processor, as the contract names it ({@code arm64}, {@code x86_64} or Java's name)
     * @param nvidia whether this machine has an NVIDIA GPU, asked only on Linux
     * @param executable whether a path is an executable file (for uvx and setsid)
     * @param stderr where the starting and waiting lines are printed */
    record Environment(String home, String path, String xdgCacheHome, String os, String arch, BooleanSupplier nvidia,
                       Predicate<String> executable, Consumer<String> stderr) {
        static Environment real() {
            String path = System.getenv("PATH");
            return new Environment(System.getenv("HOME"), path, System.getenv("XDG_CACHE_HOME"),
                osName(System.getProperty("os.name")), archName(System.getProperty("os.arch")),
                () -> Files.exists(Path.of("/proc/driver/nvidia/version")) || findOnPath(path, "nvidia-smi", LocalEngine::isExecutable) != null,
                LocalEngine::isExecutable, System.err::println);
        }
    }

    /** Java's os.name as the contract names the system. */
    static String osName(String javaName) {
        if (javaName.equals("Mac OS X") || javaName.equals("Darwin")) return "macos";
        if (javaName.equals("Linux")) return "linux";
        if (javaName.startsWith("Windows")) return "windows";
        return javaName;
    }

    /** Java's os.arch as the contract names the processor. */
    static String archName(String javaName) {
        return switch (javaName) {
            case "aarch64", "arm64" -> "arm64";
            case "x86_64", "amd64", "x64" -> "x86_64";
            default -> javaName;
        };
    }

    /** The version of this package, from the resource its build writes (ai/jeff/sdk/version.txt). */
    static String sdkVersion() {
        try (InputStream in = LocalEngine.class.getResourceAsStream("version.txt")) {
            if (in == null) {
                throw new IllegalStateException("The resource ai/jeff/sdk/version.txt is missing from the Jeff SDK's jar; "
                    + "the build writes it (the generateVersion task)");
            }
            String version = new String(in.readAllBytes(), StandardCharsets.UTF_8).strip();
            if (version.isEmpty()) throw new IllegalStateException("The resource ai/jeff/sdk/version.txt of the Jeff SDK is empty");
            return version;
        } catch (IOException error) {
            throw new IllegalStateException("Reading the resource ai/jeff/sdk/version.txt of the Jeff SDK failed", error);
        }
    }

    // ---- the pure parts ----

    /** The jeff-runtime extra for this machine ("Platforms"), or BackendUnavailable. */
    static String extra(String os, String arch, BooleanSupplier nvidia) {
        if (os.equals("macos")) {
            if (arch.equals("arm64")) return "mac";
            if (arch.equals("x86_64")) {
                throw new JeffError.BackendUnavailable(ENGINE_RUNS_ON + "this is an Intel Mac (or a program running under Rosetta)" + USE_A_SERVER);
            }
            throw new JeffError.BackendUnavailable(ENGINE_RUNS_ON + "this Mac's processor is " + arch + USE_A_SERVER);
        }
        if (os.equals("linux")) {
            if (nvidia.getAsBoolean()) return "cuda";
            throw new JeffError.BackendUnavailable(ENGINE_RUNS_ON + "this Linux machine has no NVIDIA GPU (no /proc/driver/nvidia/version, "
                + "and no nvidia-smi on PATH)" + USE_A_SERVER);
        }
        if (os.equals("windows")) {
            throw new JeffError.BackendUnavailable(ENGINE_RUNS_ON + "the engine cannot run on Windows itself, so local mode cannot start it "
                + "here. Run your program in WSL 2 on a machine with an NVIDIA GPU, or use a Jeff server instead (remote mode)");
        }
        throw new JeffError.BackendUnavailable(ENGINE_RUNS_ON + "this machine runs " + os + USE_A_SERVER);
    }

    static JeffError.EngineError homeMissing() {
        return new JeffError.EngineError("HOME is not set, so local mode cannot find the folder for the Jeff engine (the user's cache folder)");
    }

    /** HOME, which must be set and not empty. */
    static String home(String home) {
        if (home == null || home.isEmpty()) throw homeMissing();
        return home;
    }

    /** The engine folder ("The engine folder") of this system, HOME and XDG_CACHE_HOME (null: not set). */
    static Path engineFolder(String os, String home, String xdgCacheHome) {
        String user = home(home);
        if (os.equals("macos")) return Path.of(user, "Library", "Caches", "jeff", "engine");
        if (xdgCacheHome != null && xdgCacheHome.startsWith("/")) return Path.of(xdgCacheHome, "jeff", "engine");
        return Path.of(user, ".cache", "jeff", "engine");
    }

    /** A file that exists, is a regular file and may be run. */
    static boolean isExecutable(String path) {
        Path file = Path.of(path);
        return Files.isRegularFile(file) && Files.isExecutable(file);
    }

    /** The first executable {@code name} in the folders of PATH (split at ':', empty and relative entries skipped, so
     * never the current folder), or null. */
    static String findOnPath(String path, String name, Predicate<String> executable) {
        if (path == null) return null;
        for (String folder : path.split(":", -1)) {
            if (folder.isEmpty() || !folder.startsWith("/")) continue;
            String candidate = Path.of(folder, name).toString();
            if (executable.test(candidate)) return candidate;
        }
        return null;
    }

    /** Where uvx is ("Finding uv"), or RuntimeMissing. */
    static String findUvx(String path, String home, Predicate<String> executable) {
        String onPath = findOnPath(path, "uvx", executable);
        if (onPath != null) return onPath;
        String installer = Path.of(home, ".local", "bin", "uvx").toString();
        if (executable.test(installer)) return installer;
        String cargo = Path.of(home, ".cargo", "bin", "uvx").toString();
        if (executable.test(cargo)) return cargo;
        throw new JeffError.RuntimeMissing("Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at " + installer
            + " or " + cargo + ". Install uv once with: curl -LsSf https://astral.sh/uv/install.sh | sh (see "
            + "https://docs.astral.sh/uv/). Or start an engine yourself (jeff runtime serve) and give its socket instead of the adapters");
    }

    /** Where setsid is (Linux, when an engine is to be started), or RuntimeMissing. */
    static String findSetsid(String path, Predicate<String> executable) {
        String onPath = findOnPath(path, "setsid", executable);
        if (onPath != null) return onPath;
        for (String place : List.of("/usr/bin/setsid", "/bin/setsid")) if (executable.test(place)) return place;
        throw new JeffError.RuntimeMissing("Local mode needs the setsid command to start the Jeff engine in a session of its own, and "
            + "setsid is not on PATH, nor at /usr/bin/setsid or /bin/setsid. Install util-linux (for example: apt-get install "
            + "util-linux, or dnf install util-linux). Or start an engine yourself (jeff runtime serve) and give its socket instead "
            + "of the adapters");
    }

    /** The argument list that starts {@code uvxArgv} in a process group of its own: through /bin/bash with job control
     * on macOS, through /bin/sh and setsid on Linux ({@code setsid} null on macOS). */
    static List<String> shellStart(String os, String setsid, List<String> uvxArgv) {
        List<String> argv = new ArrayList<>();
        if (os.equals("macos")) {
            argv.addAll(List.of("/bin/bash", "-c", MAC_SCRIPT));
        } else {
            argv.addAll(List.of("/bin/sh", "-c", LINUX_SCRIPT, Objects.requireNonNull(setsid, "setsid is needed on " + os)));
        }
        argv.addAll(uvxArgv);
        return argv;
    }

    /** The adapters asked for, as adapter IDs each kept once in the order first named, and the base they share. */
    record Resolved(List<String> adapterIds, String base) {}

    static JeffError.ManifestError differentBases(List<String> bases) {
        return new JeffError.ManifestError("The adapters are trained on different bases (" + String.join(", ", PyFormat.sorted(bases))
            + "); load one base at a time");
    }

    static JeffError.ProprietaryWeights proprietary(String adapterId) {
        return new JeffError.ProprietaryWeights(adapterId + " is proprietary: its weights download only through JeffHub with a licence, "
            + "so local mode cannot start an engine for it. Start an engine yourself (jeff runtime serve --path " + adapterId
            + "=<folder>) and give its socket instead of the adapters");
    }

    /** Resolves adapter IDs and aliases in the catalogue (step 2). */
    static Resolved resolve(Catalogue catalogue, List<String> references) {
        if (references.isEmpty()) throw new JeffError.ManifestError("local mode needs at least one adapter (none were given)");
        Set<String> ids = new LinkedHashSet<>();
        List<Manifest> manifests = new ArrayList<>();
        Set<String> bases = new LinkedHashSet<>();
        for (String reference : references) {
            Manifest manifest = catalogue.adapter(Objects.requireNonNull(reference, "an adapter reference is null"));
            if (ids.add(manifest.id())) {
                manifests.add(manifest);
                bases.add(manifest.baseId() + "@" + manifest.baseVersion());
            }
        }
        if (bases.size() > 1) throw differentBases(new ArrayList<>(bases));
        for (Manifest manifest : manifests) {
            if (!manifest.weights().containsKey("open")) throw proprietary(manifest.id());
        }
        return new Resolved(List.copyOf(ids), bases.iterator().next());
    }

    /** The command that starts the engine ("Starting"), as an argument list. */
    static List<String> command(String uvx, String extra, String version, Path folder, List<String> adapterIds, String base) {
        List<String> argv = new ArrayList<>(List.of(uvx, "--from", "jeff-runtime[" + extra + "]==" + version, "jeff", "runtime", "serve",
            "--socket", folder.resolve("engine.sock").toString(), "--base-version", base));
        for (String id : adapterIds) argv.addAll(List.of("--adapter", id));
        argv.addAll(List.of("--batch-window-ms", "0.1", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"));
        if (extra.equals("cuda")) {
            argv.addAll(List.of("--max-adapters", Integer.toString(Math.max(8, adapterIds.size())), "--prefix-pool-mb", "7345"));
        }
        argv.addAll(List.of("--exit-when-unused-s", "300"));
        return argv;
    }

    /** The log's last lines: split at line feeds, the empty piece after a final line feed dropped, the last 20 kept. */
    static String logTail(String log) {
        List<String> pieces = new ArrayList<>(List.of(log.split("\n", -1)));
        if (pieces.size() > 1 && pieces.getLast().isEmpty()) pieces.removeLast();
        return String.join("\n", pieces.subList(Math.max(0, pieces.size() - 20), pieces.size()));
    }

    /** "Its log is <path>; its last lines:" and the lines, or "Its log, <path>, is empty". */
    private static String logEnding(Path logPath, String log) {
        if (log.isEmpty()) return "Its log, " + logPath + ", is empty";
        return "Its log is " + logPath + "; its last lines:\n" + logTail(log);
    }

    static String startingLine(List<String> adapterIds, Path logPath) {
        return "Jeff: starting the Jeff engine for " + String.join(", ", adapterIds) + "; the first time, this downloads the engine and "
            + "the base model (about 2 GB, once). Its log: " + logPath;
    }

    static String waitingLine(Path logPath) {
        return "Jeff: waiting for the Jeff engine that another program is starting. Its log: " + logPath;
    }

    static JeffError.EngineError exited(int status, Path logPath, String log) {
        return new JeffError.EngineError("The Jeff engine exited while starting, with status " + status + ". " + logEnding(logPath, log));
    }

    static JeffError.EngineError notOwnGroup(Path logPath, String log) {
        return new JeffError.EngineError("The Jeff engine could not be started in a process group of its own, so it was stopped. "
            + logEnding(logPath, log));
    }

    static JeffError.EngineError startTimeout(Path socketPath, long startTimeoutMs, Path logPath, String log) {
        return new JeffError.EngineError("The Jeff engine did not answer on " + socketPath + " within " + startTimeoutMs
            + " ms, so it was stopped. " + logEnding(logPath, log));
    }

    static JeffError.EngineError lockTimeout(long startTimeoutMs, Path lockPath, String pid, Path logPath) {
        return new JeffError.EngineError("Another program has been starting the Jeff engine for longer than " + startTimeoutMs
            + " ms (the lock " + lockPath + ", held by process " + pid + "). Its log is " + logPath);
    }

    static JeffError.NotInstalled notLoadable(Path socketPath, List<String> missing, String engineErrorType, String engineErrorMessage) {
        return new JeffError.NotInstalled("The Jeff engine already running at " + socketPath + " does not serve " + String.join(", ", missing)
            + ", and refused to load them: " + engineErrorType + ": " + engineErrorMessage + ". Start an engine yourself with every "
            + "adapter you need (jeff runtime serve) and give its socket instead of the adapters; the shared engine stops by itself "
            + "5 minutes after the last program using it closes");
    }

    // ---- the steps ----

    /** The engine serving every adapter in {@code references}, found or started (steps 1 to 9). */
    static SocketEngine engineFor(List<String> references, StartOptions options, Environment environment, Catalogue catalogue) {
        long started = System.nanoTime();
        Jeff.checkTimeoutMs(options.timeoutMs());
        if (options.startTimeoutMs() <= 0) {
            throw new JeffError.InvalidRequest("startTimeoutMs must be a whole number of milliseconds above 0; got " + options.startTimeoutMs());
        }
        long deadline = started + TimeUnit.MILLISECONDS.toNanos(options.startTimeoutMs());
        Resolved resolved = resolve(catalogue, Objects.requireNonNull(references, "adapters is null"));
        String extra = extra(environment.os(), environment.arch(), environment.nvidia());
        Path folder = engineFolder(environment.os(), environment.home(), environment.xdgCacheHome());
        createFolder(folder);
        Path socket = folder.resolve("engine.sock");
        SocketEngine engine = SocketEngine.connectIfListening(socket.toString(), options.timeoutMs());
        if (engine == null) {
            String uvx = findUvx(environment.path(), environment.home(), environment.executable());
            String setsid = environment.os().equals("linux") ? findSetsid(environment.path(), environment.executable()) : null;
            Lock lock = Lock.take(folder, options.startTimeoutMs(), deadline, environment.stderr());
            try {
                engine = SocketEngine.connectIfListening(socket.toString(), options.timeoutMs());
                if (engine == null) {
                    engine = start(environment.os(), setsid, command(uvx, extra, sdkVersion(), folder, resolved.adapterIds(), resolved.base()), folder,
                        resolved.adapterIds(), options, deadline, environment.stderr());
                }
            } finally {
                lock.release();
            }
        }
        try {
            share(engine, socket, resolved.adapterIds());
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

    /** Creates the engine folder and every missing parent; the engine folder itself with permissions 0700. */
    private static void createFolder(Path folder) {
        try {
            Files.createDirectories(folder.getParent());
            if (!Files.isDirectory(folder)) {
                try {
                    Files.createDirectory(folder, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rwx------")));
                } catch (FileAlreadyExistsException another) {
                    // another program made it at the same moment; it must be a folder
                    if (!Files.isDirectory(folder)) throw another;
                }
            }
        } catch (IOException error) {
            JeffError.EngineError failure = new JeffError.EngineError("Creating the folder for the Jeff engine, " + folder + ", failed: " + error);
            failure.initCause(error);
            throw failure;
        }
    }

    /** engine.lock ("The start lock"): held while this program starts the engine. */
    static final class Lock {
        private final Path path;
        private final String own;

        private Lock(Path path, String own) {
            this.path = path;
            this.own = own;
        }

        static Lock take(Path folder, long startTimeoutMs, long deadline, Consumer<String> stderr) {
            Path path = folder.resolve("engine.lock");
            Path logPath = folder.resolve("engine.log");
            String own = ProcessHandle.current().pid() + "\n";
            boolean printed = false;
            try {
                while (true) {
                    try (SeekableByteChannel file = Files.newByteChannel(path, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
                        file.write(ByteBuffer.wrap(own.getBytes(StandardCharsets.US_ASCII)));
                        return new Lock(path, own);
                    } catch (FileAlreadyExistsException held) {
                        // read below who holds it
                    }
                    String text;
                    long modified;
                    try {
                        text = Files.readString(path, StandardCharsets.ISO_8859_1);
                        modified = Files.getLastModifiedTime(path).toMillis();
                    } catch (NoSuchFileException released) {
                        continue; // released between the two looks: try again at once
                    }
                    String holder = text.strip();
                    boolean live;
                    if (holder.matches("[0-9]{1,18}")) {
                        live = ProcessHandle.of(Long.parseLong(holder)).map(ProcessHandle::isAlive).orElse(false);
                    } else {
                        // being written by its holder, unless it has held no process ID for too long
                        live = System.currentTimeMillis() - modified <= EMPTY_LOCK_STALE_MS;
                    }
                    if (!live) {
                        Files.deleteIfExists(path);
                        continue;
                    }
                    long left = deadline - System.nanoTime();
                    if (left <= 0) throw lockTimeout(startTimeoutMs, path, holder.matches("[0-9]{1,18}") ? holder : "(none written yet)", logPath);
                    if (!printed) {
                        stderr.accept(waitingLine(logPath));
                        printed = true;
                    }
                    Thread.sleep(Math.min(POLL_MS, Math.max(1, TimeUnit.NANOSECONDS.toMillis(left))));
                }
            } catch (IOException error) {
                JeffError.EngineError failure = new JeffError.EngineError("Taking the start lock " + path + " failed: " + error);
                failure.initCause(error);
                throw failure;
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                throw new JeffError.EngineError("Interrupted while waiting for the start lock " + path);
            }
        }

        /** Deletes the lock file if it still holds this program's process ID. */
        void release() {
            try {
                if (own.equals(Files.readString(path, StandardCharsets.ISO_8859_1))) Files.delete(path);
            } catch (NoSuchFileException gone) {
                // someone took it over as stale; it is not this program's to delete any more
            } catch (IOException error) {
                JeffError.EngineError failure = new JeffError.EngineError("Releasing the start lock " + path + " failed: " + error);
                failure.initCause(error);
                throw failure;
            }
        }
    }

    private static String readLog(Path logPath) {
        try {
            return new String(Files.readAllBytes(logPath), StandardCharsets.UTF_8);
        } catch (IOException error) {
            JeffError.EngineError failure = new JeffError.EngineError("Reading the Jeff engine's log " + logPath + " failed: " + error);
            failure.initCause(error);
            throw failure;
        }
    }

    /** Starts the engine ("Starting") and waits for it to answer on its socket. */
    private static SocketEngine start(String os, String setsid, List<String> argv, Path folder, List<String> adapterIds, StartOptions options, long deadline,
                                      Consumer<String> stderr) {
        Path logPath = folder.resolve("engine.log");
        Path socket = folder.resolve("engine.sock");
        stderr.accept(startingLine(adapterIds, logPath));
        List<String> shell = shellStart(os, setsid, argv);
        Process process;
        try {
            Files.write(logPath, new byte[0]);
            process = new ProcessBuilder(shell)
                .directory(folder.toFile())
                .redirectInput(ProcessBuilder.Redirect.from(new File("/dev/null")))
                // the script sends the engine's output to engine.log itself; the shell's own output is dropped
                .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                .redirectError(ProcessBuilder.Redirect.DISCARD)
                .start();
        } catch (IOException error) {
            JeffError.EngineError failure = new JeffError.EngineError("Starting the Jeff engine (" + String.join(" ", argv) + ") failed: " + error);
            failure.initCause(error);
            throw failure;
        }
        try {
            while (true) {
                SocketEngine engine = SocketEngine.connectIfListening(socket.toString(), options.timeoutMs());
                if (engine != null) return engine;
                if (!process.isAlive()) {
                    int status = process.exitValue();
                    if (status == NOT_OWN_GROUP) throw notOwnGroup(logPath, readLog(logPath));
                    throw exited(status, logPath, readLog(logPath));
                }
                long left = deadline - System.nanoTime();
                if (left <= 0) {
                    process.destroy(); // SIGTERM to the shell, which passes it on to the engine's process group
                    throw startTimeout(socket, options.startTimeoutMs(), logPath, readLog(logPath));
                }
                Thread.sleep(Math.min(POLL_MS, Math.max(1, TimeUnit.NANOSECONDS.toMillis(left))));
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new JeffError.EngineError("Interrupted while waiting for the Jeff engine to answer on " + socket);
        }
    }

    /** Makes the engine serve every adapter asked for ("Sharing"): one serve with what it serves now and the missing ones. */
    private static void share(SocketEngine engine, Path socket, List<String> adapterIds) {
        List<String> installed = engine.installedInOrder();
        List<String> missing = new ArrayList<>();
        for (String id : adapterIds) if (!installed.contains(id)) missing.add(id);
        if (missing.isEmpty()) return;
        List<String> wanted = new ArrayList<>();
        for (String id : installed) if (!id.equals("base")) wanted.add(id);
        wanted.addAll(missing);
        JeffError refusal = engine.serve(wanted);
        if (refusal != null) {
            JeffError.NotInstalled error = notLoadable(socket, missing, refusal.type(), refusal.getMessage());
            error.initCause(refusal);
            throw error;
        }
    }
}
