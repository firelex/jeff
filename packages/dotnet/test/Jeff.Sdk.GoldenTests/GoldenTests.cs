using System.Text.Json.Nodes;
using Jeff.Sdk.Testing;

namespace Jeff.Sdk.GoldenTests;

/// <summary>A test that needs a real engine: skipped, with a reason naming JEFF_ENGINE_SOCKET, when that variable is not
/// set; run when it names the socket of a running `jeff runtime serve`.</summary>
public sealed class EngineFactAttribute : FactAttribute
{
    public const string SocketVariable = "JEFF_ENGINE_SOCKET";

    public EngineFactAttribute()
    {
        if (Environment.GetEnvironmentVariable(SocketVariable) is not { Length: > 0 })
        {
            Skip = $"{SocketVariable} is not set: set it to the socket of a running `jeff runtime serve` (it needs the weights), "
                + "and JEFF_GOLDEN_ADAPTERS to the comma-separated adapter IDs it serves whose golden cases to check";
        }
    }
}

/// <summary>contract/golden/step1.json against a real engine: `jeff runtime serve` running on this machine. JEFF_ENGINE_SOCKET
/// is its socket; JEFF_GOLDEN_ADAPTERS the comma-separated adapter IDs it serves whose golden cases to check. Without
/// JEFF_ENGINE_SOCKET the test is skipped; with it, a missing JEFF_GOLDEN_ADAPTERS, an adapter the engine does not serve
/// or an adapter with no golden case fails the run.</summary>
public class GoldenTests
{
    private static string RepositoryRoot()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            if (Directory.Exists(Path.Combine(directory.FullName, "contract", "golden"))) return directory.FullName;
        }
        throw new InvalidOperationException($"No folder above {AppContext.BaseDirectory} holds contract/golden");
    }

    [EngineFact]
    public async Task EveryGoldenCaseOfTheGivenAdaptersIsReproducedByTheRunningEngine()
    {
        string socket = Environment.GetEnvironmentVariable(EngineFactAttribute.SocketVariable)!;
        var adapters = (Environment.GetEnvironmentVariable("JEFF_GOLDEN_ADAPTERS") is { Length: > 0 } list
                ? list
                : throw new InvalidOperationException($"{EngineFactAttribute.SocketVariable} is set but JEFF_GOLDEN_ADAPTERS is not: set it to the "
                    + "comma-separated adapter IDs the engine serves whose golden cases to check"))
            .Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries);
        string path = Path.Combine(RepositoryRoot(), "contract", "golden", "step1.json");
        var golden = (JsonObject)Json.Parse(File.ReadAllText(path), path)!;
        var all = golden["cases"]!.AsArray().Select(item => GoldenCase.FromJson(item!.AsObject())).ToList();
        await using var jeff = await JeffClient.LocalAsync(socket, new LocalOptions { Timeout = TimeSpan.FromMinutes(10) });
        var installed = await jeff.Engine.InstalledAsync();
        var notServed = adapters.Where(adapter => !installed.Contains(adapter)).ToList();
        if (notServed.Count > 0)
        {
            throw new InvalidOperationException($"The engine at {socket} does not serve {string.Join(", ", notServed)} "
                + $"(it serves {string.Join(", ", installed.Order(StringComparer.Ordinal))})");
        }
        var problems = new List<string>();
        int checkedCases = 0;
        foreach (var adapter in adapters)
        {
            var cases = all.Where(item => jeff.Catalogue.Function(item.Call.Function).AdapterId == adapter).ToList();
            if (cases.Count == 0) throw new InvalidOperationException($"contract/golden/step1.json has no case for {adapter}");
            var decisions = await jeff.DecideAsync(cases.Select(item => item.Call).ToList());
            for (int index = 0; index < cases.Count; index++)
            {
                var calibration = jeff.CalibrationOf(jeff.Catalogue.Function(cases[index].Call.Function));
                problems.AddRange(Golden.Problems(cases[index], decisions[index], calibration?.Threshold).Select(problem => $"{cases[index].Name}: {problem}"));
            }
            checkedCases += cases.Count;
        }
        Assert.Empty(problems);
        Assert.True(checkedCases > 0);
    }
}
