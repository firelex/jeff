using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

internal static partial class Fixtures
{
    private static readonly ConcurrentDictionary<string, Catalogue> Catalogues = new(StringComparer.Ordinal);

    /// <summary>A case's catalogue: the shipped one, or the shipped one with a variant's manifests in place
    /// (catalogues.json), each loaded with the path conformance/&lt;variant&gt;/&lt;adapter id&gt;.</summary>
    public static Catalogue CatalogueFor(string name) => Catalogues.GetOrAdd(name, variant =>
    {
        if (variant == "shipped") return Catalogue.Shipped();
        if (File("catalogues.json")["variants"]![variant] is not JsonArray manifests) throw new InvalidOperationException($"catalogues.json has no variant {variant}");
        return Catalogue.Shipped().Replacing(manifests.Select(raw =>
            Manifest.FromDict(raw, $"conformance/{variant}/{raw!["id"]!.GetValue<string>()}")));
    });
}
