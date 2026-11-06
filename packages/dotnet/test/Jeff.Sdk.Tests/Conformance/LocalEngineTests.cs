using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>local_engine.json: the platform choice, the engine folder, finding uvx, the adapters, the command line, the
/// log's last lines and every message of local mode's engine start (contract/local-engine.md).</summary>
public class LocalEngineConformanceTests
{
    private static JsonObject File => Fixtures.File("local_engine.json");

    private static IEnumerable<JsonObject> Section(string name) => File[name]!.AsArray().Select(item => item!.AsObject());

    private static string? Text(JsonNode? node) => node?.GetValue<string>();

    private static void AssertError(JsonNode expected, Exception actual)
    {
        Assert.Equal(expected["type"]!.GetValue<string>(), Assert.IsAssignableFrom<JeffError>(actual).Type);
        Assert.Equal(expected["message"]!.GetValue<string>(), actual.Message);
    }

    private static void Outcome<T>(JsonObject item, Func<T> run, Action<T> check)
    {
        if (item["error"] is { } error)
        {
            AssertError(error, Record.Exception(() => run())!);
            return;
        }
        check(run());
    }

    [Fact]
    public void Platforms()
    {
        foreach (var item in Section("platforms"))
        {
            bool nvidia = item["nvidia"]!.GetValue<bool>();
            Outcome(item, () => LocalEngine.Extra(Text(item["os"])!, Text(item["arch"])!, () => nvidia),
                extra => Assert.Equal(Text(item["extra"]), extra));
        }
    }

    [Fact]
    public void Folders()
    {
        foreach (var item in Section("folders"))
        {
            Outcome(item, () => LocalEngine.Folder(Text(item["os"])!, Text(item["home"]), Text(item["xdg_cache_home"])),
                folder => Assert.Equal(Text(item["folder"]), folder));
        }
    }

    [Fact]
    public void Uvx()
    {
        foreach (var item in Section("uvx"))
        {
            var executables = item["executables"]!.AsArray().Select(Text).ToHashSet(StringComparer.Ordinal);
            Outcome(item, () => LocalEngine.FindUvx(Text(item["path"]), Text(item["home"])!, executables.Contains),
                found => Assert.Equal(Text(item["found"]), found));
        }
    }

    [Fact]
    public void Adapters()
    {
        foreach (var item in Section("adapters"))
        {
            var references = item["adapters"]!.AsArray().Select(node => Text(node)!).ToList();
            Outcome(item, () => LocalEngine.ResolveAdapters(Catalogue.Shipped(), references), resolved =>
            {
                Assert.Equal(item["adapter_ids"]!.AsArray().Select(Text), resolved.AdapterIds);
                Assert.Equal(Text(item["base"]), resolved.Base);
            });
        }
    }

    [Fact]
    public void Commands()
    {
        foreach (var item in Section("commands"))
        {
            var (ids, baseVersion) = LocalEngine.ResolveAdapters(Catalogue.Shipped(), item["adapters"]!.AsArray().Select(node => Text(node)!));
            var argv = LocalEngine.Command(Text(item["uvx"])!, Text(item["extra"])!, Text(item["version"])!, Text(item["folder"])!, ids, baseVersion);
            Assert.Equal(item["argv"]!.AsArray().Select(Text), argv);
        }
    }

    [Fact]
    public void ShellStarts()
    {
        foreach (var item in Section("shell_starts"))
        {
            var argv = LocalEngine.ShellStart(Text(item["os"])!, Text(item["setsid"]), item["uvx_argv"]!.AsArray().Select(node => Text(node)!).ToList());
            Assert.Equal(item["argv"]!.AsArray().Select(Text), argv);
        }
    }

    [Fact]
    public void Setsid()
    {
        foreach (var item in Section("setsid"))
        {
            var executables = item["executables"]!.AsArray().Select(Text).ToHashSet(StringComparer.Ordinal);
            Outcome(item, () => LocalEngine.FindSetsid(Text(item["path"]), executables.Contains), found => Assert.Equal(Text(item["found"]), found));
        }
    }

    [Fact]
    public void LogTails()
    {
        foreach (var item in Section("log_tails")) Assert.Equal(Text(item["tail"]), LocalEngine.LogTail(Text(item["log"])!));
    }

    [Fact]
    public void Messages()
    {
        var kinds = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in Section("messages"))
        {
            string kind = Text(item["kind"])!;
            kinds.Add(kind);
            switch (kind)
            {
                case "starting":
                    Assert.Equal(Text(item["text"]), LocalEngine.StartingLine(item["adapter_ids"]!.AsArray().Select(node => Text(node)!).ToList(), Text(item["log_path"])!));
                    break;
                case "waiting":
                    Assert.Equal(Text(item["text"]), LocalEngine.WaitingLine(Text(item["log_path"])!));
                    break;
                case "exited":
                    AssertError(item["error"]!, LocalEngine.Exited((int)item["status"]!.GetValue<long>(), Text(item["log_path"])!, Text(item["log"])!));
                    break;
                case "not_own_group":
                    AssertError(item["error"]!, LocalEngine.NotOwnGroup(Text(item["log_path"])!, Text(item["log"])!));
                    break;
                case "start_timeout":
                    AssertError(item["error"]!, LocalEngine.StartTimedOut(Text(item["socket_path"])!, item["start_timeout_ms"]!.GetValue<long>(),
                        Text(item["log_path"])!, Text(item["log"])!));
                    break;
                case "lock_timeout":
                    AssertError(item["error"]!, LocalEngine.LockTimedOut(item["start_timeout_ms"]!.GetValue<long>(), Text(item["lock_path"])!,
                        item["pid"]!.GetValue<long>().ToString(System.Globalization.CultureInfo.InvariantCulture), Text(item["log_path"])!));
                    break;
                case "not_loadable":
                    var engineError = Errors.FromWire(Text(item["engine_error"]!["type"])!, Text(item["engine_error"]!["message"])!, new JsonObject());
                    AssertError(item["error"]!, LocalEngine.NotLoadable(Text(item["socket_path"])!, item["missing"]!.AsArray().Select(node => Text(node)!).ToList(),
                        engineError));
                    break;
                case "proprietary":
                    var proprietary = LocalEngine.Proprietary(Text(item["adapter_id"])!);
                    AssertError(item["error"]!, proprietary);
                    Assert.Equal(Text(item["adapter_id"]), proprietary.AdapterId);
                    break;
                case "home_missing":
                    AssertError(item["error"]!, Record.Exception(() => LocalEngine.Folder("linux", null, null))!);
                    break;
                case "different_bases":
                    var bases = item["bases"]!.AsArray().Select(node => Text(node)!.Split('@')).Select(parts => (parts[0], parts[1]));
                    AssertError(item["error"]!, LocalEngine.DifferentBases(bases));
                    break;
                default:
                    Assert.Fail($"local_engine.json has a message of kind {kind}, which this test does not know");
                    break;
            }
        }
        Assert.Equal(new[] { "different_bases", "exited", "home_missing", "lock_timeout", "not_loadable", "not_own_group", "proprietary", "start_timeout", "starting", "waiting" },
            kinds.Order(StringComparer.Ordinal));
    }
}
