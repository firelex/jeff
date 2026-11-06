using System.ComponentModel;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace Jeff.Sdk;

/// <summary>How JeffClient.LocalAsync with a list of adapters finds or starts the Jeff engine on this machine
/// (contract/local-engine.md). Both are whole numbers of milliseconds of at least 1.</summary>
public sealed class StartOptions
{
    /// <summary>How long to wait for the engine's hello and for each reply (60 seconds when not given).</summary>
    public TimeSpan Timeout { get; init; } = TimeSpan.FromSeconds(60);

    /// <summary>How long everything before the client is ready may take: waiting for another program that is starting
    /// the engine, and starting it (the first start downloads about 2 GB). 30 minutes when not given.</summary>
    public TimeSpan StartTimeout { get; init; } = TimeSpan.FromMinutes(30);
}

/// <summary>What local mode reads from the machine: the system and processor (the contract's names), whether an
/// NVIDIA GPU is there, HOME, PATH, XDG_CACHE_HOME, and where the start line is printed. Current() is the real
/// process's; tests give their own.</summary>
internal sealed class EngineEnvironment
{
    public required string Os { get; init; }

    public required string Arch { get; init; }

    public required Func<bool> Nvidia { get; init; }

    public required string? Home { get; init; }

    public required string? Path { get; init; }

    public required string? XdgCacheHome { get; init; }

    public required TextWriter Stderr { get; init; }

    /// <summary>Whether a path is an executable file, for finding uvx and setsid (the real file system's unless a test gives its own).</summary>
    public Func<string, bool> IsExecutable { get; init; } = LocalEngine.IsExecutable;

    public static EngineEnvironment Current()
    {
        string? path = Environment.GetEnvironmentVariable("PATH");
        return new EngineEnvironment
        {
            Os = LocalEngine.OsName(),
            Arch = LocalEngine.ArchName(RuntimeInformation.ProcessArchitecture),
            Nvidia = () => File.Exists("/proc/driver/nvidia/version") || LocalEngine.FindOnPath(path, "nvidia-smi", LocalEngine.IsExecutable) is not null,
            Home = Environment.GetEnvironmentVariable("HOME"),
            Path = path,
            XdgCacheHome = Environment.GetEnvironmentVariable("XDG_CACHE_HOME"),
            Stderr = Console.Error,
        };
    }
}

/// <summary>The engine folder's three files.</summary>
internal sealed record EnginePaths(string Folder)
{
    public string Socket => System.IO.Path.Join(Folder, "engine.sock");

    public string Lock => System.IO.Path.Join(Folder, "engine.lock");

    public string Log => System.IO.Path.Join(Folder, "engine.log");
}

/// <summary>Local mode with a list of adapters (contract/local-engine.md): find the engine running for this user on
/// this machine, or start one through uvx, then build the client over it as the socket form does. The pure parts
/// (platform, folder, uvx, adapters, command, log tail, messages) are functions the conformance cases call directly.</summary>
internal static partial class LocalEngine
{
    private static readonly TimeSpan Poll = TimeSpan.FromMilliseconds(200);

    /// <summary>A lock file holding no process ID is stale only once it is older than this (its writer may be about to write it).</summary>
    private static readonly TimeSpan UnwrittenLockAge = TimeSpan.FromSeconds(10);

    /// <summary>Makes sure the engine leads a process group of its own: waits up to 5 seconds for the group to exist; if
    /// the engine is still running without one, stops it and exits with status 125 (contract/local-engine.md, Starting).</summary>
    internal const string GroupCheck = "i=0; until kill -0 -$pid 2>/dev/null; do kill -0 $pid 2>/dev/null || break; i=$((i+1)); "
        + "if [ $i -ge 50 ]; then kill -TERM $pid; exit 125; fi; sleep 0.1; done";

    /// <summary>macOS, under /bin/bash: `set -m` gives the engine its own process group, `set +m` at once after so bash
    /// writes no job notice. The engine's input and output are redirected here (the shell runs in the engine folder),
    /// since .NET cannot hand a child process a file as its output; a TERM sent to the shell goes on to the engine's group.</summary>
    internal const string MacScript = "set -m; \"$0\" \"$@\" </dev/null >>engine.log 2>&1 & pid=$!; set +m; " + GroupCheck
        + "; trap \"kill -TERM -$pid\" TERM; wait $pid";

    /// <summary>Linux, under /bin/sh with setsid as the command's first word (dash's `set -m` needs a terminal, so job
    /// control is not used): setsid makes the engine a new session and process group.</summary>
    internal const string LinuxScript = "\"$0\" \"$@\" </dev/null >>engine.log 2>&1 & pid=$!; " + GroupCheck + "; trap \"kill -TERM -$pid\" TERM; wait $pid";

    /// <summary>The shell's exit status when the engine could not be given a process group of its own.</summary>
    internal const int NotOwnGroupStatus = 125;

    private const string PlatformHead = "The Jeff engine runs on macOS with Apple silicon, or on Linux with an NVIDIA GPU; ";

    internal const string HomeMissing = "HOME is not set, so local mode cannot find the folder for the Jeff engine (the user's cache folder)";

    // ---- the pure parts ----

    /// <summary>This system as the contract names it: macos, linux, windows, or .NET's own name for any other.</summary>
    internal static string OsName()
    {
        if (OperatingSystem.IsMacOS()) return "macos";
        if (OperatingSystem.IsLinux()) return "linux";
        if (OperatingSystem.IsWindows()) return "windows";
        if (OperatingSystem.IsFreeBSD()) return "freebsd";
        return RuntimeInformation.OSDescription.Split(' ')[0].ToLowerInvariant();
    }

    /// <summary>A processor as the contract names it: arm64, x86_64, or .NET's own name (lower case) for any other.</summary>
    internal static string ArchName(Architecture architecture) => architecture switch
    {
        Architecture.Arm64 => "arm64",
        Architecture.X64 => "x86_64",
        _ => architecture.ToString().ToLowerInvariant(),
    };

    /// <summary>The jeff-runtime extra for this machine (mac or cuda); BackendUnavailable for every other machine.
    /// `nvidia` is asked only on Linux.</summary>
    internal static string Extra(string os, string arch, Func<bool> nvidia) => os switch
    {
        "macos" when arch == "arm64" => "mac",
        "macos" when arch == "x86_64" => throw new BackendUnavailable(PlatformHead
            + "this is an Intel Mac (or a program running under Rosetta), so local mode cannot start it here. Use a Jeff server instead (remote mode)"),
        "macos" => throw new BackendUnavailable(PlatformHead
            + $"this Mac's processor is {arch}, so local mode cannot start it here. Use a Jeff server instead (remote mode)"),
        "linux" when nvidia() => "cuda",
        "linux" => throw new BackendUnavailable(PlatformHead + "this Linux machine has no NVIDIA GPU (no /proc/driver/nvidia/version, and no nvidia-smi on "
            + "PATH), so local mode cannot start it here. Use a Jeff server instead (remote mode)"),
        "windows" => throw new BackendUnavailable(PlatformHead + "the engine cannot run on Windows itself, so local mode cannot start it here. Run your "
            + "program in WSL 2 on a machine with an NVIDIA GPU, or use a Jeff server instead (remote mode)"),
        _ => throw new BackendUnavailable(PlatformHead + $"this machine runs {os}, so local mode cannot start it here. Use a Jeff server instead (remote mode)"),
    };

    /// <summary>The engine folder: $HOME/Library/Caches/jeff/engine on macOS; on Linux $XDG_CACHE_HOME/jeff/engine when
    /// that is an absolute path, else $HOME/.cache/jeff/engine. EngineError when HOME is not set or empty.</summary>
    internal static string Folder(string os, string? home, string? xdgCacheHome)
    {
        if (string.IsNullOrEmpty(home)) throw new EngineError(HomeMissing);
        return os switch
        {
            "macos" => System.IO.Path.Join(home, "Library/Caches/jeff/engine"),
            "linux" when xdgCacheHome is not null && xdgCacheHome.StartsWith('/') => System.IO.Path.Join(xdgCacheHome, "jeff/engine"),
            "linux" => System.IO.Path.Join(home, ".cache/jeff/engine"),
            _ => throw new ArgumentException($"The engine folder is only defined on macos and linux, not {os} (the platform check refuses every other system first)", nameof(os)),
        };
    }

    /// <summary>Whether `path` is a file (not a folder) with an execute permission bit set (never on Windows, which the
    /// platform check refuses before uvx is looked for).</summary>
    internal static bool IsExecutable(string path) =>
        !OperatingSystem.IsWindows() && File.Exists(path) && (File.GetUnixFileMode(path) & (UnixFileMode.UserExecute | UnixFileMode.GroupExecute | UnixFileMode.OtherExecute)) != 0;

    /// <summary>The first executable `name` in the folders of PATH (split at ':'; empty and relative entries skipped, so
    /// never the current folder), or null.</summary>
    internal static string? FindOnPath(string? path, string name, Func<string, bool> isExecutable)
    {
        foreach (var folder in (path ?? "").Split(':'))
        {
            if (!folder.StartsWith('/')) continue;
            string candidate = System.IO.Path.Join(folder, name);
            if (isExecutable(candidate)) return candidate;
        }
        return null;
    }

    /// <summary>uvx: on PATH, else at $HOME/.local/bin/uvx (uv's installer), else at $HOME/.cargo/bin/uvx (cargo install);
    /// RuntimeMissing when none is there.</summary>
    /// <summary>setsid (Linux): in each absolute folder on PATH, then at /usr/bin/setsid and /bin/setsid; RuntimeMissing when none is there.</summary>
    internal static string FindSetsid(string? path, Func<string, bool> isExecutable)
    {
        if (FindOnPath(path, "setsid", isExecutable) is { } found) return found;
        foreach (var place in new[] { "/usr/bin/setsid", "/bin/setsid" })
        {
            if (isExecutable(place)) return place;
        }
        throw new RuntimeMissing("Local mode needs the setsid command to start the Jeff engine in a session of its own, and setsid is not on PATH, nor "
            + "at /usr/bin/setsid or /bin/setsid. Install util-linux (for example: apt-get install util-linux, or dnf install util-linux). Or start an "
            + "engine yourself (jeff runtime serve) and give its socket instead of the adapters");
    }

    /// <summary>The argument list this SDK runs to start the engine in a process group of its own: on macOS
    /// /bin/bash -c MacScript and the uvx argument list; on Linux /bin/sh -c LinuxScript, setsid, and the uvx argument list.</summary>
    internal static IReadOnlyList<string> ShellStart(string os, string? setsid, IReadOnlyList<string> uvxArgv) => os switch
    {
        "macos" => ["/bin/bash", "-c", MacScript, .. uvxArgv],
        "linux" => ["/bin/sh", "-c", LinuxScript, setsid ?? throw new ArgumentNullException(nameof(setsid), "The Linux start needs the setsid found"), .. uvxArgv],
        _ => throw new ArgumentException($"The engine is only started on macos and linux, not {os} (the platform check refuses every other system first)", nameof(os)),
    };

    internal static string FindUvx(string? path, string home, Func<string, bool> isExecutable)
    {
        if (FindOnPath(path, "uvx", isExecutable) is { } found) return found;
        string installer = System.IO.Path.Join(home, ".local/bin/uvx");
        if (isExecutable(installer)) return installer;
        string cargo = System.IO.Path.Join(home, ".cargo/bin/uvx");
        if (isExecutable(cargo)) return cargo;
        throw new RuntimeMissing($"Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at {installer} or {cargo}. Install uv once "
            + "with: curl -LsSf https://astral.sh/uv/install.sh | sh (see https://docs.astral.sh/uv/). Or start an engine yourself (jeff runtime serve) "
            + "and give its socket instead of the adapters");
    }

    /// <summary>The adapters' IDs (each once, in the order first named) and their base as id@version. The catalogue's
    /// own error for an unknown reference; ManifestError for no adapters or adapters of different bases;
    /// ProprietaryWeights for an adapter without open weights.</summary>
    internal static (IReadOnlyList<string> AdapterIds, string Base) ResolveAdapters(Catalogue catalogue, IEnumerable<string> references)
    {
        var manifests = new List<Manifest>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var reference in references)
        {
            var manifest = catalogue.Adapter(reference);
            if (seen.Add(manifest.Id)) manifests.Add(manifest);
        }
        if (manifests.Count == 0) throw new ManifestError("local mode needs at least one adapter (none were given)");
        var bases = manifests.Select(manifest => (manifest.BaseId, manifest.BaseVersion)).Distinct().ToList();
        if (bases.Count != 1) throw DifferentBases(bases);
        foreach (var manifest in manifests)
        {
            if (!manifest.Weights.ContainsKey("open")) throw Proprietary(manifest.Id);
        }
        return (manifests.Select(manifest => manifest.Id).ToList(), $"{bases[0].BaseId}@{bases[0].BaseVersion}");
    }

    /// <summary>The argument list that starts the engine (contract/local-engine.md, Starting).</summary>
    internal static IReadOnlyList<string> Command(string uvx, string extra, string version, string folder, IReadOnlyList<string> adapterIds, string baseVersion)
    {
        var argv = new List<string>
        {
            uvx, "--from", $"jeff-runtime[{extra}]=={version}", "jeff", "runtime", "serve",
            "--socket", new EnginePaths(folder).Socket, "--base-version", baseVersion,
        };
        foreach (var id in adapterIds) argv.AddRange(["--adapter", id]);
        argv.AddRange(["--batch-window-ms", "0.1", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"]);
        if (extra == "cuda") argv.AddRange(["--max-adapters", Math.Max(8, adapterIds.Count).ToString(System.Globalization.CultureInfo.InvariantCulture), "--prefix-pool-mb", "7345"]);
        argv.AddRange(["--exit-when-unused-s", "300"]);
        return argv;
    }

    /// <summary>The log's last 20 lines: split at line feeds, the empty piece after a final line feed dropped, joined with line feeds.</summary>
    internal static string LogTail(string log)
    {
        var pieces = log.Split('\n').ToList();
        if (log.EndsWith('\n')) pieces.RemoveAt(pieces.Count - 1);
        return string.Join("\n", pieces.Skip(Math.Max(0, pieces.Count - 20)));
    }

    private static string WithLog(string head, string logPath, string log) =>
        log.Length == 0 ? $"{head}. Its log, {logPath}, is empty" : $"{head}. Its log is {logPath}; its last lines:\n{LogTail(log)}";

    internal static string StartingLine(IReadOnlyList<string> adapterIds, string logPath) =>
        $"Jeff: starting the Jeff engine for {string.Join(", ", adapterIds)}; the first time, this downloads the engine and the base model (about 2 GB, once). Its log: {logPath}";

    internal static string WaitingLine(string logPath) => $"Jeff: waiting for the Jeff engine that another program is starting. Its log: {logPath}";

    internal static EngineError Exited(int status, string logPath, string log) =>
        new(WithLog($"The Jeff engine exited while starting, with status {status}", logPath, log));

    internal static EngineError NotOwnGroup(string logPath, string log) =>
        new(WithLog("The Jeff engine could not be started in a process group of its own, so it was stopped", logPath, log));

    /// <summary>The error for a shell that exited before the engine answered: status 125 is the group check's refusal.</summary>
    private static EngineError ExitedOrNotOwnGroup(int status, string logPath) =>
        status == NotOwnGroupStatus ? NotOwnGroup(logPath, File.ReadAllText(logPath)) : Exited(status, logPath, File.ReadAllText(logPath));

    internal static EngineError StartTimedOut(string socketPath, long startTimeoutMs, string logPath, string log) =>
        new(WithLog($"The Jeff engine did not answer on {socketPath} within {startTimeoutMs} ms, so it was stopped", logPath, log));

    internal static EngineError LockTimedOut(long startTimeoutMs, string lockPath, string holder, string logPath) =>
        new($"Another program has been starting the Jeff engine for longer than {startTimeoutMs} ms (the lock {lockPath}, held by process {holder}). Its log is {logPath}");

    internal static NotInstalled NotLoadable(string socketPath, IReadOnlyList<string> missing, JeffError engineError) =>
        new($"The Jeff engine already running at {socketPath} does not serve {string.Join(", ", missing)}, and refused to load them: "
            + $"{Errors.Described(engineError)}. Start an engine yourself with every adapter you need (jeff runtime serve) and give its socket instead "
            + "of the adapters; the shared engine stops by itself 5 minutes after the last program using it closes");

    internal static ProprietaryWeights Proprietary(string adapterId) =>
        new($"{adapterId} is proprietary: its weights download only through JeffHub with a licence, so local mode cannot start an engine for it. "
            + $"Start an engine yourself (jeff runtime serve --path {adapterId}=<folder>) and give its socket instead of the adapters",
            new System.Text.Json.Nodes.JsonObject { ["adapter_id"] = adapterId });

    internal static ManifestError DifferentBases(IEnumerable<(string Id, string Version)> bases)
    {
        var sorted = bases.OrderBy(pair => pair.Id, StringComparer.Ordinal).ThenBy(pair => pair.Version, StringComparer.Ordinal).Select(pair => $"{pair.Id}@{pair.Version}");
        return new ManifestError($"The adapters are trained on different bases ({string.Join(", ", sorted)}); load one base at a time");
    }

    /// <summary>The version of this package (its informational version, which the build sets from the project's Version).</summary>
    internal static string SdkVersion()
    {
        var version = typeof(JeffClient).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion
            ?? throw new InvalidOperationException("The Jeff.Sdk assembly carries no informational version, so local mode cannot name the jeff-runtime to start");
        if (version.Contains('+'))
        {
            throw new InvalidOperationException($"The Jeff.Sdk assembly's informational version {version} carries build metadata; it must be the package version alone");
        }
        return version;
    }

    internal static void CheckOptions(StartOptions options)
    {
        CheckWholeMillis(options.Timeout, "Timeout");
        Timeouts.Check(options.Timeout);
        CheckWholeMillis(options.StartTimeout, "StartTimeout");
    }

    private static void CheckWholeMillis(TimeSpan value, string name)
    {
        if (value.Ticks % TimeSpan.TicksPerMillisecond != 0 || value < TimeSpan.FromMilliseconds(1))
        {
            throw new InvalidRequest($"{name} must be a whole number of milliseconds of at least 1; got {PyFormat.Millis(value)} ms");
        }
    }

    private static long WholeMillis(TimeSpan value) => value.Ticks / TimeSpan.TicksPerMillisecond;

    // ---- the steps ----

    /// <summary>The steps of contract/local-engine.md, in order.</summary>
    internal static async Task<JeffClient> LocalAsync(IEnumerable<string> adapters, StartOptions? options, EngineEnvironment environment,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(adapters);
        long started = Stopwatch.GetTimestamp();
        options ??= new StartOptions();
        CheckOptions(options);
        var catalogue = Catalogue.Shipped();
        var (adapterIds, baseVersion) = ResolveAdapters(catalogue, adapters);
        string extra = Extra(environment.Os, environment.Arch, environment.Nvidia);
        var paths = new EnginePaths(Folder(environment.Os, environment.Home, environment.XdgCacheHome));
        CreateFolder(paths.Folder);
        var engine = await SocketEngine.ConnectIfListeningAsync(paths.Socket, options.Timeout, cancellationToken).ConfigureAwait(false);
        if (engine is null)
        {
            string uvx = FindUvx(environment.Path, environment.Home!, environment.IsExecutable);
            string? setsid = environment.Os == "linux" ? FindSetsid(environment.Path, environment.IsExecutable) : null;
            await TakeLockAsync(paths, options.StartTimeout, started, environment.Stderr, cancellationToken).ConfigureAwait(false);
            try
            {
                engine = await SocketEngine.ConnectIfListeningAsync(paths.Socket, options.Timeout, cancellationToken).ConfigureAwait(false)
                    ?? await StartAsync(paths, ShellStart(environment.Os, setsid, Command(uvx, extra, SdkVersion(), paths.Folder, adapterIds, baseVersion)), adapterIds, options, started,
                        environment.Stderr, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                ReleaseLock(paths.Lock);
            }
        }
        try
        {
            await ServeMissingAsync(engine, adapterIds, cancellationToken).ConfigureAwait(false);
            return await JeffClient.CreateAsync(engine, catalogue, cancellationToken).ConfigureAwait(false);
        }
        catch
        {
            await engine.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    /// <summary>The folder with every missing parent; the engine folder itself, when it is made here, with permissions 0700.</summary>
    private static void CreateFolder(string folder)
    {
        if (OperatingSystem.IsWindows()) throw new InvalidOperationException("The engine folder is never made on Windows: the platform check refuses it first");
        if (Directory.Exists(folder)) return;
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(folder)!);
        Directory.CreateDirectory(folder, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
    }

    [GeneratedRegex("^[0-9]+\n?$")]
    private static partial Regex ProcessIdText();

    /// <summary>Whether a process with this ID is running.</summary>
    private static bool Running(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            return true;
        }
        catch (ArgumentException)
        {
            return false; // no process has this ID
        }
    }

    /// <summary>Takes engine.lock (contract/local-engine.md, The start lock), waiting while a running process holds it
    /// and taking over a stale one, bounded by the start timeout counted from the call.</summary>
    private static async Task TakeLockAsync(EnginePaths paths, TimeSpan startTimeout, long started, TextWriter stderr, CancellationToken cancellationToken)
    {
        bool told = false;
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                using var stream = new FileStream(paths.Lock, FileMode.CreateNew, FileAccess.Write, FileShare.None);
                stream.Write(Encoding.ASCII.GetBytes($"{Environment.ProcessId}\n"));
                return;
            }
            catch (IOException) when (File.Exists(paths.Lock))
            {
                // held: read who holds it, below
            }
            string text;
            DateTime written;
            try
            {
                text = File.ReadAllText(paths.Lock);
                written = File.GetLastWriteTimeUtc(paths.Lock);
            }
            catch (FileNotFoundException)
            {
                continue; // released meanwhile: take it
            }
            int? holder = ProcessIdText().IsMatch(text) && int.TryParse(text.TrimEnd('\n'), out int pid) && pid > 0 ? pid : null;
            bool stale = holder is { } id ? !Running(id) : DateTime.UtcNow - written > UnwrittenLockAge;
            if (stale)
            {
                File.Delete(paths.Lock);
                continue;
            }
            var elapsed = Stopwatch.GetElapsedTime(started);
            if (elapsed >= startTimeout)
            {
                throw LockTimedOut(WholeMillis(startTimeout), paths.Lock, holder?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "(none written yet)", paths.Log);
            }
            if (!told)
            {
                stderr.WriteLine(WaitingLine(paths.Log));
                stderr.Flush();
                told = true;
            }
            await Task.Delay(Min(Poll, startTimeout - elapsed), cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Deletes engine.lock if it still holds this process's ID.</summary>
    private static void ReleaseLock(string lockPath)
    {
        string text;
        try
        {
            text = File.ReadAllText(lockPath);
        }
        catch (FileNotFoundException)
        {
            return; // no longer there, so not held by this process
        }
        if (text == $"{Environment.ProcessId}\n") File.Delete(lockPath);
    }

    private static TimeSpan Min(TimeSpan first, TimeSpan second) => first < second ? first : second;

    [DllImport("libc", EntryPoint = "kill", SetLastError = true)]
    private static extern int Kill(int pid, int signal);

    private const int SigTerm = 15;

    /// <summary>Starts the engine (prints the start line, empties engine.log, runs the shell start argument list in the
    /// folder) and waits until it answers on its socket, it exits, or the start timeout runs out.</summary>
    private static async Task<SocketEngine> StartAsync(EnginePaths paths, IReadOnlyList<string> argv, IReadOnlyList<string> adapterIds, StartOptions options,
        long started, TextWriter stderr, CancellationToken cancellationToken)
    {
        stderr.WriteLine(StartingLine(adapterIds, paths.Log));
        stderr.Flush();
        File.WriteAllText(paths.Log, "");
        var info = new ProcessStartInfo(argv[0])
        {
            UseShellExecute = false,
            WorkingDirectory = paths.Folder,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var argument in argv.Skip(1)) info.ArgumentList.Add(argument);
        using var shell = Process.Start(info) ?? throw new InvalidOperationException("Process.Start gave no process for the shell that starts the engine");
        shell.StandardInput.Close();
        // the shell's own output (its job notices) is read and dropped, so it never blocks on a full pipe
        shell.BeginOutputReadLine();
        shell.BeginErrorReadLine();
        while (true)
        {
            if (shell.HasExited) throw ExitedOrNotOwnGroup(shell.ExitCode, paths.Log);
            var engine = await SocketEngine.ConnectIfListeningAsync(paths.Socket, options.Timeout, cancellationToken).ConfigureAwait(false);
            if (engine is not null) return engine;
            var elapsed = Stopwatch.GetElapsedTime(started);
            if (elapsed >= options.StartTimeout)
            {
                if (Kill(shell.Id, SigTerm) != 0)
                {
                    int errno = Marshal.GetLastPInvokeError();
                    if (shell.HasExited) throw ExitedOrNotOwnGroup(shell.ExitCode, paths.Log);
                    throw new Win32Exception(errno, $"Sending SIGTERM to the shell (process {shell.Id}) that started the Jeff engine failed");
                }
                throw StartTimedOut(paths.Socket, WholeMillis(options.StartTimeout), paths.Log, File.ReadAllText(paths.Log));
            }
            await Task.Delay(Min(Poll, options.StartTimeout - elapsed), cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>One serve request when the engine lacks some of the adapters: the ones it serves now (without base, in
    /// its order) and then the missing ones; a refusal is NotInstalled.</summary>
    private static async Task ServeMissingAsync(SocketEngine engine, IReadOnlyList<string> adapterIds, CancellationToken cancellationToken)
    {
        var installed = engine.InstalledInOrder;
        var missing = adapterIds.Where(id => !installed.Contains(id)).ToList();
        if (missing.Count == 0) return;
        var wanted = installed.Where(id => id != "base").Concat(missing).ToList();
        try
        {
            await engine.ServeAsync(wanted, cancellationToken).ConfigureAwait(false);
        }
        catch (JeffError refused) when (refused.Refusal)
        {
            throw NotLoadable(engine.Path, missing, refused);
        }
    }
}
