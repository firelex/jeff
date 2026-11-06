using System.Diagnostics;
using System.Runtime.Versioning;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Jeff.Sdk.Tests.Helpers;

namespace Jeff.Sdk.Tests;

/// <summary>Local mode with a list of adapters, against a fake uvx: a small shell script put first on the (test-given)
/// PATH that records its arguments and then sleeps as "the engine process" (the test starts the stub engine on the
/// socket it was given), prints a few lines and exits, or sleeps with nothing listening.</summary>
[UnsupportedOSPlatform("windows")]
public class LocalEngineTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(15);

    private static JsonObject Ctx() => new() { ["company"] = "A shop.", ["channel"] = "email" };

    private static IReadOnlyList<double> LevelOf(string adapter, JsonObject row)
    {
        string message = row["state"]!["message"]!.GetValue<string>();
        int level = message[^1] - '0';
        return Enumerable.Range(0, 5).Select(index => index == level ? 1.0 : 0.0).ToList();
    }

    /// <summary>A temporary machine: a short HOME (Unix socket paths are limited to about 104 bytes on macOS), a bin
    /// folder for the fake uvx, and the processes the fake started, stopped on Dispose.</summary>
    private sealed class Machine : IDisposable
    {
        public Machine()
        {
            Root = "/tmp/jl" + Guid.NewGuid().ToString("N")[..8];
            Directory.CreateDirectory(Home);
            Directory.CreateDirectory(Bin);
        }

        public string Root { get; }

        public string Home => Path.Join(Root, "h");

        public string Bin => Path.Join(Root, "b");

        public string Uvx => Path.Join(Bin, "uvx");

        public string ArgsFile => Path.Join(Root, "args");

        public string RunsFile => Path.Join(Root, "runs");

        public string PidsFile => Path.Join(Root, "pids");

        private readonly StringWriter printed = new();

        public TextWriter Stderr => TextWriter.Synchronized(printed);

        public string Printed
        {
            get
            {
                lock (printed) return printed.ToString();
            }
        }

        public EnginePaths Paths => new(Path.Join(Home, "Library/Caches/jeff/engine"));

        public int Runs => File.Exists(RunsFile) ? File.ReadAllLines(RunsFile).Length : 0;

        public EngineEnvironment Env(string os = "macos", string arch = "arm64", bool nvidia = false, string? path = null) => new()
        {
            Os = os, Arch = arch, Nvidia = () => nvidia, Home = Home, Path = path ?? $"{Bin}:/usr/bin:/bin", XdgCacheHome = null, Stderr = Stderr,
        };

        /// <summary>The fake uvx: records its process ID, its arguments and the run, then runs `body`.</summary>
        public void FakeUvx(string body)
        {
            File.WriteAllText(Uvx, "#!/bin/sh\n"
                + $"echo $$ >> {PidsFile}\n"
                + $"printf '%s\\n' \"$0\" \"$@\" > {ArgsFile}.tmp && mv {ArgsFile}.tmp {ArgsFile}\n"
                + $"echo run >> {RunsFile}\n"
                + body + "\n");
            File.SetUnixFileMode(Uvx, (UnixFileMode)0b111_101_101);
        }

        /// <summary>An engine that prints a progress line and runs until stopped.</summary>
        public void EngineUvx() => FakeUvx("echo 'Fetching 11 files: 9%'\nexec sleep 60");

        public async Task<List<string>> ArgsAsync()
        {
            var watch = Stopwatch.StartNew();
            while (!File.Exists(ArgsFile))
            {
                if (watch.Elapsed > Wait) throw new TimeoutException("The fake uvx was not run");
                await Task.Delay(20);
            }
            return File.ReadAllLines(ArgsFile).ToList();
        }

        public int[] Pids() => File.Exists(PidsFile) ? File.ReadAllLines(PidsFile).Select(int.Parse).ToArray() : [];

        public void Dispose()
        {
            foreach (int pid in Pids())
            {
                Process process;
                try
                {
                    process = Process.GetProcessById(pid);
                }
                catch (ArgumentException)
                {
                    continue; // already gone
                }
                using (process) process.Kill();
            }
            Directory.Delete(Root, recursive: true);
        }
    }

    private static bool Running(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            using var ps = Process.Start(new ProcessStartInfo("ps", ["-o", "stat=", "-p", pid.ToString()]) { RedirectStandardOutput = true })!;
            string state = ps.StandardOutput.ReadToEnd().Trim();
            ps.WaitForExit();
            return state.Length > 0 && !state.StartsWith('Z');
        }
        catch (ArgumentException)
        {
            return false;
        }
    }

    private static string ProcessGroupOf(int pid)
    {
        using var ps = Process.Start(new ProcessStartInfo("ps", ["-o", "pgid=", "-p", pid.ToString()]) { RedirectStandardOutput = true })!;
        string group = ps.StandardOutput.ReadToEnd().Trim();
        ps.WaitForExit();
        return group;
    }

    private static string SocketOf(List<string> argv) => argv[argv.IndexOf("--socket") + 1];

    private static StartOptions Quick(double startSeconds = 20) =>
        new() { Timeout = TimeSpan.FromSeconds(5), StartTimeout = TimeSpan.FromSeconds(startSeconds) };

    private static string Version()
    {
        string project = File.ReadAllText(Repo.PathOf("packages", "dotnet", "src", "Jeff.Sdk", "Jeff.Sdk.csproj"));
        return Regex.Match(project, "<Version>([^<]+)</Version>").Groups[1].Value;
    }

    [Fact]
    public async Task AutoStartRunsTheContractsCommandPrintsTheLineAndDecidesThroughTheEngine()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        var starting = LocalEngine.LocalAsync(["triage"], Quick(), machine.Env(), default);
        var argv = await machine.ArgsAsync();
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"], SocketOf(argv));
        await using var jeff = await starting;

        Assert.Equal(new[]
        {
            machine.Uvx, "--from", $"jeff-runtime[mac]=={Version()}", "jeff", "runtime", "serve", "--socket", machine.Paths.Socket,
            "--base-version", "jeff-base@v1.4", "--adapter", "jeff.support.triage", "--batch-window-ms", "0.1", "--max-batch-rows", "64",
            "--queue-limit-rows", "4096", "--prepare-workers", "2", "--exit-when-unused-s", "300",
        }, argv);
        Assert.Equal("1.4.1", Version());
        Assert.Equal(LocalEngine.StartingLine(["jeff.support.triage"], machine.Paths.Log) + Environment.NewLine, machine.Printed);
        Assert.StartsWith("Jeff: starting the Jeff engine for jeff.support.triage; the first time", machine.Printed);
        var decision = (await jeff.DecideAsync([new Call("triage.urgency", "level 3", Ctx())]))[0];
        Assert.Equal(3L, decision.Value.GetValue<long>());
        Assert.False(File.Exists(machine.Paths.Lock));
        Assert.Equal("Fetching 11 files: 9%\n", File.ReadAllText(machine.Paths.Log));
        Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute, File.GetUnixFileMode(machine.Paths.Folder));
        // the engine runs in a process group of its own
        int engine = machine.Pids().Single();
        Assert.Equal(engine.ToString(), ProcessGroupOf(engine));
        Assert.NotEqual(ProcessGroupOf(Environment.ProcessId), ProcessGroupOf(engine));
    }

    [Fact]
    public async Task ASecondCallReusesTheRunningEngine()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        var starting = LocalEngine.LocalAsync(["triage"], Quick(), machine.Env(), default);
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"], SocketOf(await machine.ArgsAsync()));
        await using var first = await starting;
        await using var second = await LocalEngine.LocalAsync(["jeff.support.triage"], Quick(), machine.Env(), default);
        await using var third = await LocalEngine.LocalAsync(["triage", "triage"], Quick(), machine.Env(), default);
        Assert.Equal(1, machine.Runs);
        Assert.Empty(server.ServeRequests);
        var decision = (await third.DecideAsync([new Call("triage.urgency", "level 2", Ctx())]))[0];
        Assert.Equal(2L, decision.Value.GetValue<long>());
        Assert.Single(Regex.Matches(machine.Printed, "starting the Jeff engine"));
    }

    [Fact]
    public async Task AMissingAdapterIsAskedForWithOneServeAndARefusalIsNotInstalled()
    {
        using var machine = new Machine();
        Directory.CreateDirectory(machine.Paths.Folder);
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"], machine.Paths.Socket);
        var error = await Assert.ThrowsAsync<NotInstalled>(() => LocalEngine.LocalAsync(["triage", "spam", "guard"], Quick(), machine.Env(), default));
        Assert.Equal($"The Jeff engine already running at {machine.Paths.Socket} does not serve jeff.safety.spam, jeff.safety.guard, and refused to load "
            + "them: UnknownFunction: not available to serve from this engine: jeff.safety.guard, jeff.safety.spam. Start an engine yourself with every "
            + "adapter you need (jeff runtime serve) and give its socket instead of the adapters; the shared engine stops by itself 5 minutes after "
            + "the last program using it closes", error.Message);
        Assert.Equal(new[] { "jeff.support.triage", "jeff.safety.spam", "jeff.safety.guard" }, server.ServeRequests.Single());
        Assert.Equal(0, machine.Runs);
        Assert.Equal("", machine.Printed);
    }

    [Fact]
    public async Task AMissingAdapterTheEngineCanLoadIsServed()
    {
        using var machine = new Machine();
        Directory.CreateDirectory(machine.Paths.Folder);
        await using var server = EngineServer.Start((_, _) => [0.1, 0.9], ["jeff.support.triage"], machine.Paths.Socket);
        server.AcceptServe = true;
        await using var jeff = await LocalEngine.LocalAsync(["spam"], Quick(), machine.Env(), default);
        Assert.Equal(new[] { "jeff.support.triage", "jeff.safety.spam" }, server.ServeRequests.Single());
        Assert.Contains("jeff.safety.spam", await jeff.Engine.InstalledAsync());
    }

    [Fact]
    public async Task WithoutUvxLocalModeRaisesRuntimeMissing()
    {
        using var machine = new Machine();
        var error = await Assert.ThrowsAsync<RuntimeMissing>(() => LocalEngine.LocalAsync(["guard"], Quick(), machine.Env(path: machine.Bin), default));
        Assert.Equal($"Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at {machine.Home}/.local/bin/uvx or "
            + $"{machine.Home}/.cargo/bin/uvx. Install uv once with: curl -LsSf https://astral.sh/uv/install.sh | sh (see https://docs.astral.sh/uv/). "
            + "Or start an engine yourself (jeff runtime serve) and give its socket instead of the adapters", error.Message);
        Assert.False(File.Exists(machine.Paths.Lock));
    }

    [Fact]
    public async Task ASocketFileNobodyListensOnCountsAsNoEngine()
    {
        using var machine = new Machine();
        Directory.CreateDirectory(machine.Paths.Folder);
        // a socket file left behind by an engine that is gone: .NET removes the file it bound when the socket closes,
        // so a second name (a hard link) for that file is what stays
        string bound = Path.Join(machine.Root, "s.sock");
        using (var listener = new System.Net.Sockets.Socket(System.Net.Sockets.AddressFamily.Unix, System.Net.Sockets.SocketType.Stream,
            System.Net.Sockets.ProtocolType.Unspecified))
        {
            listener.Bind(new System.Net.Sockets.UnixDomainSocketEndPoint(bound));
            using var link = Process.Start("ln", [bound, machine.Paths.Socket]);
            link.WaitForExit();
            Assert.Equal(0, link.ExitCode);
        }
        Assert.True(new FileInfo(machine.Paths.Socket).Exists);
        await Assert.ThrowsAsync<RuntimeMissing>(() => LocalEngine.LocalAsync(["guard"], Quick(), machine.Env(path: machine.Bin), default));
    }

    [Fact]
    public async Task AnUnsupportedMachineIsRefusedBeforeAnythingIsLookedForOrRun()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        var error = await Assert.ThrowsAsync<BackendUnavailable>(() =>
            LocalEngine.LocalAsync(["guard"], Quick(), machine.Env(os: "linux", arch: "x86_64", nvidia: false), default));
        Assert.StartsWith("The Jeff engine runs on macOS with Apple silicon, or on Linux with an NVIDIA GPU; this Linux machine has no NVIDIA GPU", error.Message);
        Assert.Equal(0, machine.Runs);
        Assert.Empty(Directory.EnumerateFileSystemEntries(machine.Home));
        await Assert.ThrowsAsync<BackendUnavailable>(() => LocalEngine.LocalAsync(["guard"], Quick(), machine.Env(arch: "x86_64"), default));
        await Assert.ThrowsAsync<BackendUnavailable>(() => LocalEngine.LocalAsync(["guard"], Quick(), machine.Env(os: "windows", arch: "x86_64", nvidia: true), default));
        Assert.Empty(Directory.EnumerateFileSystemEntries(machine.Home));
    }

    [Fact]
    public async Task TheSocketFormNeverStartsAnythingNorTouchesTheEngineFolder()
    {
        using var machine = new Machine();
        var real = EngineEnvironment.Current();
        string? folder = real.Home is { Length: > 0 } && real.Os is "macos" or "linux" ? LocalEngine.Folder(real.Os, real.Home, real.XdgCacheHome) : null;
        string Snapshot() => folder is null || !Directory.Exists(folder) ? "absent"
            : string.Join("\n", Directory.EnumerateFileSystemEntries(folder).Order(StringComparer.Ordinal).Select(entry => $"{entry} {File.GetLastWriteTimeUtc(entry):O}"));
        string before = Snapshot();
        string socket = Path.Join(machine.Root, "x.sock");
        var error = await Assert.ThrowsAsync<EngineError>(() => JeffClient.LocalAsync(socket, new LocalOptions { Timeout = TimeSpan.FromSeconds(1) }));
        Assert.StartsWith($"No engine is listening at {socket}: ", error.Message);
        Assert.Equal(before, Snapshot());
        Assert.False(File.Exists(socket));
    }

    [Fact]
    public async Task TheStartTimeoutStopsTheEnginesProcessGroupAndNamesTheLogsLastLines()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        var watch = Stopwatch.StartNew();
        var error = await Assert.ThrowsAsync<EngineError>(() => LocalEngine.LocalAsync(["triage"], Quick(startSeconds: 1), machine.Env(), default));
        Assert.Equal($"The Jeff engine did not answer on {machine.Paths.Socket} within 1000 ms, so it was stopped. Its log is {machine.Paths.Log}; "
            + "its last lines:\nFetching 11 files: 9%", error.Message);
        Assert.InRange(watch.Elapsed.TotalMilliseconds, 1000, 5000);
        Assert.False(File.Exists(machine.Paths.Lock));
        int engine = machine.Pids().Single();
        var stopped = Stopwatch.StartNew();
        while (Running(engine))
        {
            Assert.True(stopped.Elapsed < TimeSpan.FromSeconds(5), $"The fake engine (process {engine}) was not stopped");
            await Task.Delay(50);
        }
    }

    [Fact]
    public async Task AnEngineThatExitsWhileStartingIsAnEngineErrorWithItsStatusAndLog()
    {
        using var machine = new Machine();
        machine.FakeUvx("echo 'warning: something'\necho 'ManifestError: bad' >&2\nexit 3");
        var error = await Assert.ThrowsAsync<EngineError>(() => LocalEngine.LocalAsync(["triage"], Quick(), machine.Env(), default));
        Assert.Equal($"The Jeff engine exited while starting, with status 3. Its log is {machine.Paths.Log}; its last lines:\nwarning: something\n"
            + "ManifestError: bad", error.Message);
        Assert.False(File.Exists(machine.Paths.Lock));

        machine.FakeUvx("kill -KILL $$");
        error = await Assert.ThrowsAsync<EngineError>(() => LocalEngine.LocalAsync(["triage"], Quick(), machine.Env(), default));
        Assert.Equal($"The Jeff engine exited while starting, with status 137. Its log, {machine.Paths.Log}, is empty", error.Message);
    }

    [Fact]
    public async Task AStaleLockIsTakenOver()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        Directory.CreateDirectory(machine.Paths.Folder);
        File.WriteAllText(machine.Paths.Lock, "99999999\n");
        var starting = LocalEngine.LocalAsync(["triage"], Quick(), machine.Env(), default);
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"], SocketOf(await machine.ArgsAsync()));
        await using var jeff = await starting;
        Assert.DoesNotContain("waiting", machine.Printed);
        Assert.False(File.Exists(machine.Paths.Lock));
        Assert.Equal(1, machine.Runs);
    }

    [Fact]
    public async Task ALiveLockIsWaitedForAndTheEngineItsHolderStartedIsUsed()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        Directory.CreateDirectory(machine.Paths.Folder);
        File.WriteAllText(machine.Paths.Lock, $"{Environment.ProcessId}\n"); // "another program": a running process
        var starting = LocalEngine.LocalAsync(["triage"], Quick(), machine.Env(), default);
        var watch = Stopwatch.StartNew();
        while (!machine.Printed.Contains("waiting"))
        {
            Assert.True(watch.Elapsed < Wait, "The waiting line was not printed");
            await Task.Delay(20);
        }
        await Task.Delay(300);
        Assert.False(starting.IsCompleted);
        await using var server = EngineServer.Start(LevelOf, ["jeff.support.triage"], machine.Paths.Socket);
        File.Delete(machine.Paths.Lock); // the other program is done starting
        await using var jeff = await starting;
        Assert.Equal(LocalEngine.WaitingLine(machine.Paths.Log) + Environment.NewLine, machine.Printed);
        Assert.Equal(0, machine.Runs);
        Assert.False(File.Exists(machine.Paths.Lock));
    }

    [Fact]
    public async Task WaitingForALiveLockIsBoundedByTheStartTimeout()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        Directory.CreateDirectory(machine.Paths.Folder);
        File.WriteAllText(machine.Paths.Lock, $"{Environment.ProcessId}\n");
        var error = await Assert.ThrowsAsync<EngineError>(() => LocalEngine.LocalAsync(["triage"], Quick(startSeconds: 0.6), machine.Env(), default));
        Assert.Equal($"Another program has been starting the Jeff engine for longer than 600 ms (the lock {machine.Paths.Lock}, held by process "
            + $"{Environment.ProcessId}). Its log is {machine.Paths.Log}", error.Message);
        Assert.True(File.Exists(machine.Paths.Lock)); // not this call's to release
        Assert.Equal(0, machine.Runs);
    }

    [Fact]
    public async Task OptionsAreCheckedFirstAndDefaultToTheContractsValues()
    {
        var defaults = new StartOptions();
        Assert.Equal(TimeSpan.FromMilliseconds(60_000), defaults.Timeout);
        Assert.Equal(TimeSpan.FromMilliseconds(1_800_000), defaults.StartTimeout);
        var nowhere = new EngineEnvironment
        {
            Os = "linux", Arch = "x86_64", Nvidia = () => false, Home = null, Path = null, XdgCacheHome = null, Stderr = TextWriter.Null,
        };
        foreach (var options in new[]
        {
            new StartOptions { Timeout = TimeSpan.Zero }, new StartOptions { Timeout = TimeSpan.FromTicks(15_000) },
            new StartOptions { StartTimeout = TimeSpan.FromMilliseconds(-5) }, new StartOptions { StartTimeout = TimeSpan.FromTicks(5_000) },
        })
        {
            await Assert.ThrowsAsync<InvalidRequest>(() => LocalEngine.LocalAsync(["nope"], options, nowhere, default));
        }
        await Assert.ThrowsAsync<UnknownFunction>(() => LocalEngine.LocalAsync(["nope"], null, nowhere, default));
        Assert.Equal("local mode needs at least one adapter (none were given)",
            (await Assert.ThrowsAsync<ManifestError>(() => LocalEngine.LocalAsync([], null, nowhere, default))).Message);
        var mac = new EngineEnvironment
        {
            Os = "macos", Arch = "arm64", Nvidia = () => false, Home = "", Path = null, XdgCacheHome = null, Stderr = TextWriter.Null,
        };
        Assert.Equal(LocalEngine.HomeMissing, (await Assert.ThrowsAsync<EngineError>(() => LocalEngine.LocalAsync(["guard"], null, mac, default))).Message);
    }

    [Fact]
    public void ProprietaryAdaptersAndMixedBasesAreRefusedWhenResolving()
    {
        var shipped = Catalogue.Shipped();
        var guard = shipped.Adapter("guard");
        var mixed = shipped.Replacing([guard with { BaseVersion = "v1.5" }]);
        Assert.Equal("The adapters are trained on different bases (jeff-base@v1.4, jeff-base@v1.5); load one base at a time",
            Assert.Throws<ManifestError>(() => LocalEngine.ResolveAdapters(mixed, ["guard", "triage"])).Message);
        var closed = shipped.Replacing([guard with { Weights = new JsonObject { ["proprietary"] = new JsonObject() } }]);
        var error = Assert.Throws<ProprietaryWeights>(() => LocalEngine.ResolveAdapters(closed, ["triage", "guard"]));
        Assert.Equal("jeff.safety.guard", error.AdapterId);
        Assert.StartsWith("jeff.safety.guard is proprietary: ", error.Message);
    }

    [Fact]
    public void TheVersionIsThePackagesAndBothOverloadsAreChosenByTheirArguments()
    {
        Assert.Equal(Version(), LocalEngine.SdkVersion());
        // compiled, never run: a list of adapters picks the start form, a string the socket form
        Func<Task<JeffClient>> start = () => JeffClient.LocalAsync(new[] { "guard" });
        Func<Task<JeffClient>> startWith = () => JeffClient.LocalAsync(["guard"], new StartOptions { StartTimeout = TimeSpan.FromMinutes(5) });
        Func<Task<JeffClient>> socket = () => JeffClient.LocalAsync("/tmp/jeff.sock", new LocalOptions { Timeout = TimeSpan.FromSeconds(1) });
        Assert.NotNull(start);
        Assert.NotNull(startWith);
        Assert.NotNull(socket);
        Assert.Equal(typeof(IEnumerable<string>), typeof(JeffClient).GetMethods().Where(method => method.Name == "LocalAsync")
            .Select(method => method.GetParameters()[0].ParameterType).Single(type => type != typeof(string)));
    }

    [Fact]
    public async Task OnLinuxAMissingSetsidIsRuntimeMissingAndNothingIsRun()
    {
        using var machine = new Machine();
        machine.EngineUvx();
        var linux = new EngineEnvironment
        {
            Os = "linux", Arch = "x86_64", Nvidia = () => true, Home = machine.Home, Path = $"{machine.Bin}:/usr/bin:/bin", XdgCacheHome = null,
            Stderr = machine.Stderr, IsExecutable = path => path == machine.Uvx,
        };
        var error = await Assert.ThrowsAsync<RuntimeMissing>(() => LocalEngine.LocalAsync(["guard"], Quick(), linux, default));
        Assert.StartsWith("Local mode needs the setsid command to start the Jeff engine in a session of its own", error.Message);
        Assert.Equal(0, machine.Runs);
        Assert.False(File.Exists(Path.Join(machine.Home, ".cache/jeff/engine/engine.lock")));
    }

    /// <summary>Runs a shell start argument list in `folder` with an empty standard input (no terminal), and returns
    /// its exit status and how long it took.</summary>
    private static async Task<(int Status, TimeSpan Took)> RunShellAsync(IReadOnlyList<string> argv, string folder)
    {
        var info = new ProcessStartInfo(argv[0])
        {
            WorkingDirectory = folder, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
        };
        foreach (var argument in argv.Skip(1)) info.ArgumentList.Add(argument);
        var watch = Stopwatch.StartNew();
        using var shell = Process.Start(info)!;
        shell.StandardInput.Close();
        var output = shell.StandardOutput.ReadToEndAsync();
        var errors = shell.StandardError.ReadToEndAsync();
        using var limit = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        await shell.WaitForExitAsync(limit.Token);
        await Task.WhenAll(output, errors);
        return (shell.ExitCode, watch.Elapsed);
    }

    /// <summary>A fake engine that writes its process ID and process group into `out` in the folder, then sleeps.</summary>
    private static string GroupReporter(Machine machine, int seconds)
    {
        string path = Path.Join(machine.Bin, "engine");
        File.WriteAllText(path, $"#!/bin/sh\necho $$ $(ps -o pgid= -p $$) > {machine.Root}/out\nsleep {seconds}\n");
        File.SetUnixFileMode(path, (UnixFileMode)0b111_101_101);
        return path;
    }

    private static void AssertLeadsItsOwnGroup(Machine machine)
    {
        var parts = File.ReadAllText(Path.Join(machine.Root, "out")).Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        Assert.Equal(parts[0], parts[1]);
        Assert.NotEqual(ProcessGroupOf(Environment.ProcessId), parts[1]);
    }

    [Fact]
    public async Task TheLinuxStartUnderDashGivesTheEngineAGroupOfItsOwn()
    {
        if (!File.Exists("/bin/dash")) return; // the contract's test runs the Linux form under dash; this machine has none
        using var machine = new Machine();
        string setsid = "/usr/bin/setsid";
        if (!File.Exists(setsid))
        {
            setsid = Path.Join(machine.Bin, "setsid");
            File.WriteAllText(setsid, "#!/usr/bin/perl\nuse POSIX; POSIX::setsid() or die \"setsid: $!\"; exec @ARGV or die \"exec: $!\";\n");
            File.SetUnixFileMode(setsid, (UnixFileMode)0b111_101_101);
        }
        var argv = LocalEngine.ShellStart("linux", setsid, [GroupReporter(machine, 2)]).ToList();
        Assert.Equal("/bin/sh", argv[0]);
        argv[0] = "/bin/dash";
        var (status, _) = await RunShellAsync(argv, machine.Root);
        Assert.Equal(0, status);
        AssertLeadsItsOwnGroup(machine);
    }

    [Fact]
    public async Task TheMacStartUnderBashGivesTheEngineAGroupOfItsOwn()
    {
        using var machine = new Machine();
        var argv = LocalEngine.ShellStart("macos", null, [GroupReporter(machine, 2)]);
        Assert.Equal("/bin/bash", argv[0]);
        var (status, _) = await RunShellAsync(argv, machine.Root);
        Assert.Equal(0, status);
        AssertLeadsItsOwnGroup(machine);
    }

    [Fact]
    public async Task TheGroupCheckRefusesAnEngineLeftInTheCallersGroup()
    {
        if (!File.Exists("/bin/dash")) return; // needs a shell whose set -m does nothing without a terminal
        using var machine = new Machine();
        var argv = LocalEngine.ShellStart("macos", null, [GroupReporter(machine, 8)]).ToList();
        argv[0] = "/bin/dash";
        var (status, took) = await RunShellAsync(argv, machine.Root);
        Assert.Equal(LocalEngine.NotOwnGroupStatus, status);
        Assert.InRange(took.TotalSeconds, 4, 7);
        var pid = int.Parse(File.ReadAllText(Path.Join(machine.Root, "out")).Split(' ', StringSplitOptions.RemoveEmptyEntries)[0]);
        var stopped = Stopwatch.StartNew();
        while (Running(pid))
        {
            Assert.True(stopped.Elapsed < TimeSpan.FromSeconds(3), $"The fake engine (process {pid}) was not stopped");
            await Task.Delay(50);
        }
    }
}
