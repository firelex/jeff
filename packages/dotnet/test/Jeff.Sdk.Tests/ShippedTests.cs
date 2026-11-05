using System.Reflection;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class ShippedTests
{
    public static readonly string[] Items = ["contract/schemas", "contract/interfaces.yaml", "contract/errors.yaml", "catalogue"];

    private static List<string> FilesUnder(string path) => File.Exists(path)
        ? [""]
        : Directory.EnumerateFiles(path, "*", SearchOption.AllDirectories)
            .Select(file => Path.GetRelativePath(path, file).Replace('\\', '/'))
            .OrderBy(file => file, StringComparer.Ordinal).ToList();

    [Fact]
    public void TheShippedCopyMatchesTheRepository()
    {
        foreach (var item in Items)
        {
            var source = Repo.PathOf(item);
            var copy = Repo.PathOf("packages", "dotnet", "shipped", item);
            Assert.True(Path.Exists(copy), $"packages/dotnet/shipped/{item} is missing; run packages/dotnet/scripts/sync-shipped.sh");
            var names = FilesUnder(source);
            Assert.Equal(names, FilesUnder(copy));
            foreach (var name in names)
            {
                var original = name.Length == 0 ? source : Path.Combine(source, name);
                var copied = name.Length == 0 ? copy : Path.Combine(copy, name);
                Assert.True(File.ReadAllBytes(original).AsSpan().SequenceEqual(File.ReadAllBytes(copied)),
                    $"packages/dotnet/shipped/{item}/{name} differs from the repository; run packages/dotnet/scripts/sync-shipped.sh");
            }
        }
    }

    [Fact]
    public void TheAssemblyEmbedsExactlyTheShippedFolder()
    {
        var folder = Repo.PathOf("packages", "dotnet", "shipped");
        var onDisk = Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories)
            .Select(file => Path.GetRelativePath(folder, file).Replace('\\', '/')).OrderBy(file => file, PyFormat.CodePointOrder);
        Assert.Equal(onDisk, ShippedFiles.All);
        Assert.Equal(File.ReadAllText(Path.Combine(folder, "catalogue", "bases.yaml")), ShippedFiles.ReadText("catalogue/bases.yaml"));
        Assert.Contains("catalogue/jeff.safety.spam/manifest.yaml", ShippedFiles.Files("catalogue"));
        Assert.Contains("run packages/dotnet/scripts/sync-shipped.sh", Assert.Throws<ContractError>(() => ShippedFiles.ReadText("nope.yaml")).Message);
    }
    /// <summary>Catalogue.Shipped() is read once and shared by every caller in the process: a change one reader makes
    /// through anything it can reach must not be seen by another reader.</summary>
    [Fact]
    public void AChangeThroughOneReaderOfTheShippedCatalogueIsNotSeenByAnother()
    {
        string Snapshot() => string.Join("\n", Catalogue.Shipped().Manifests().Select(manifest => Json.Write(manifest.Raw) + "|"
            + Json.Write(manifest.Weights) + "|" + Json.Write(manifest.Tests) + "|" + string.Join(",", manifest.Aliases) + "|" + string.Join(",", manifest.RoutesTo)
            + "|" + string.Join(",", manifest.Context.Select(field => field.Key)) + "|" + string.Join(";", manifest.Functions.Select(pair => pair.Key + "="
                + string.Join(",", pair.Value.Labels.Fixed.Select(label => label.Key + ":" + Json.Write(label.Value))) + "/"
                + string.Join(",", pair.Value.Labels.FixedFirst.Select(label => label.Key + ":" + Json.Write(label.Value))) + "/"
                + string.Join(",", pair.Value.Labels.Levels) + "/" + string.Join(",", pair.Value.Labels.Required) + "/" + string.Join(",", pair.Value.Labels.Keys)
                + "/" + string.Join(",", pair.Value.AbstainLabels) + "/" + string.Join(",", pair.Value.Context.Select(field => field.Key)) + "/"
                + string.Join(",", pair.Value.CalibrationByFormat.Keys))))) + "|" + Json.Write(Catalogue.Shipped().Bases);

        string before = Snapshot();
        var reader = Catalogue.Shipped();
        foreach (var manifest in reader.Manifests())
        {
            manifest.Raw["injected"] = true;
            manifest.Weights["injected"] = true;
            manifest.Tests["injected"] = true;
            foreach (var list in new[] { manifest.Aliases, manifest.RoutesTo })
            {
                Assert.Throws<NotSupportedException>(() => ((IList<string>)list).Add("injected"));
            }
            Assert.Throws<NotSupportedException>(() => ((IList<ContextField>)manifest.Context).Clear());
            foreach (var (_, fn) in manifest.Functions)
            {
                // a description is a JSON node, which could be changed in place: every read gives its own copy
                foreach (var (key, description) in fn.Labels.Fixed.Concat(fn.Labels.FixedFirst))
                {
                    if (description is null) continue;
                    var again = fn.Labels.Fixed.TryGetValue(key, out var found) ? found : fn.Labels.FixedFirst[key];
                    Assert.NotSame(description, again);
                }
                foreach (var list in new[] { fn.Labels.Levels, fn.Labels.Required, fn.Labels.Keys, fn.AbstainLabels })
                {
                    Assert.Throws<NotSupportedException>(() => ((IList<string>)list).Add("injected"));
                }
                Assert.Throws<NotSupportedException>(() => ((IList<ContextField>)fn.Context).Clear());
            }
        }
        reader.Bases["injected"] = true;
        var (baseId, version) = reader.BaseVersions()[0];
        reader.Base(baseId, version)["injected"] = true;
        Assert.Equal(before, Snapshot());
        Assert.False(reader.Base(baseId, version).ContainsKey("injected"));
    }

    [Fact]
    public void AnOrderedMapCannotBeChangedFromOutsideTheSdk()
    {
        var map = typeof(OrderedMap<JsonNode?>);
        Assert.Null(map.GetMethod("Set", BindingFlags.Public | BindingFlags.Instance));
        Assert.Null(map.GetMethod("Add", BindingFlags.Public | BindingFlags.Instance));
        Assert.DoesNotContain(map.GetProperties(BindingFlags.Public | BindingFlags.Instance), property => property.SetMethod is { IsPublic: true });
    }
}
