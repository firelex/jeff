using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>Every known adapter manifest, looked up by ID, alias, version or interface (port of jeff/catalogue.py).
/// Immutable: Replacing gives a new catalogue.</summary>
public sealed class Catalogue
{
    private static readonly Lazy<Catalogue> ShippedCatalogue = new(LoadShipped);
    private readonly OrderedMap<Manifest> byId = new();
    private readonly Dictionary<string, string> byAlias = new(StringComparer.Ordinal);
    private readonly JsonObject bases;

    /// <summary>A catalogue of these manifests and bases (bases.yaml as JSON, copied). ManifestError when two adapters share an ID
    /// or alias.</summary>
    public Catalogue(IEnumerable<Manifest> manifests, JsonObject bases)
    {
        foreach (var manifest in manifests)
        {
            foreach (var name in manifest.Aliases.Prepend(manifest.Id))
            {
                if (byId.ContainsKey(name) || byAlias.ContainsKey(name))
                {
                    throw new ManifestError($"{manifest.Path}: the name {name} is already used by another adapter");
                }
            }
            byId.Set(manifest.Id, manifest);
            foreach (var alias in manifest.Aliases) byAlias[alias] = manifest.Id;
        }
        this.bases = (JsonObject)Json.Canonical(bases)!;
    }

    /// <summary>A catalogue folder: bases.yaml and one folder per adapter holding its manifest.yaml, read in code-point
    /// order of the folder names (Python's sorted(path.glob("*/manifest.yaml")), which orders by folder name). As in
    /// Python, a hidden folder counts, a folder reached through a link is followed, and an entry that is not a folder,
    /// or a folder without a manifest.yaml, is passed over. A folder that cannot be read is a ManifestError naming it
    /// (Python's glob passes it over silently, which would hide an adapter).</summary>
    public static Catalogue FromDir(string path)
    {
        string basesFile = System.IO.Path.Combine(path, "bases.yaml");
        if (!File.Exists(basesFile)) throw new ManifestError($"{basesFile} is missing");
        var manifests = new List<Manifest>();
        foreach (var name in PyFormat.Sorted(Directory.EnumerateFileSystemEntries(path).Select(entry => System.IO.Path.GetFileName(entry))))
        {
            string folder = System.IO.Path.Combine(path, name);
            if (!Directory.Exists(folder)) continue;
            try
            {
                using var entries = Directory.EnumerateFileSystemEntries(folder).GetEnumerator();
                entries.MoveNext();
            }
            catch (Exception error) when (error is UnauthorizedAccessException or IOException)
            {
                throw new ManifestError($"{folder} cannot be read, so whether it holds an adapter's manifest.yaml is unknown ({error.Message})");
            }
            string file = System.IO.Path.Combine(folder, "manifest.yaml");
            // Python's glob also lists a manifest.yaml that is a folder or a broken link, and reading it then fails
            if (File.Exists(file) || Directory.Exists(file) || new FileInfo(file).LinkTarget is not null) manifests.Add(Manifest.LoadFile(file));
        }
        return new Catalogue(manifests, BasesOf(PyYaml.Load(Manifest.ReadUtf8(basesFile), basesFile), basesFile));
    }

    /// <summary>The catalogue shipped with the package (read once).</summary>
    public static Catalogue Shipped() => ShippedCatalogue.Value;

    private static Catalogue LoadShipped()
    {
        var folders = ShippedFiles.Files("catalogue")
            .Select(file => file.Split('/'))
            .Where(parts => parts.Length == 3 && parts[2] == "manifest.yaml")
            .Select(parts => parts[1]);
        var manifests = PyFormat.Sorted(folders).Select(name =>
        {
            string file = $"catalogue/{name}/manifest.yaml";
            // the shipped manifests are embedded resources with no path on disk: messages name them shipped/catalogue/...,
            // where Python and Node print the installed file's absolute path (no fixture records such a message)
            return Manifest.Load(ShippedFiles.ReadText(file), $"shipped/{file}");
        }).ToList();
        return new Catalogue(manifests, BasesOf(PyYaml.Load(ShippedFiles.ReadText("catalogue/bases.yaml"), "shipped/catalogue/bases.yaml"),
            "shipped/catalogue/bases.yaml"));
    }

    private static JsonObject BasesOf(JsonNode? document, string path) =>
        document as JsonObject ?? throw new ManifestError($"{path} is not a mapping of base IDs to their versions");

    /// <summary>The same bases, holding `manifests` too, each in place of this catalogue's manifest of the same ID.</summary>
    public Catalogue Replacing(IEnumerable<Manifest> manifests)
    {
        var merged = byId.Copy();
        foreach (var manifest in manifests) merged.Set(manifest.Id, manifest);
        return new Catalogue(merged.Values, bases);
    }

    /// <summary>Every adapter ID and alias in the catalogue.</summary>
    public IReadOnlySet<string> Names() => byId.Keys.Concat(byAlias.Keys).ToHashSet(StringComparer.Ordinal);

    /// <summary>Every manifest, in catalogue order (a new list each call).</summary>
    public IReadOnlyList<Manifest> Manifests() => byId.Values;

    /// <summary>A copy of bases.yaml.</summary>
    public JsonObject Bases => (JsonObject)Json.Canonical(bases)!;

    /// <summary>The manifest a reference names (an adapter ID or alias, with or without a function and @version). InvalidReference
    /// for a malformed reference; UnknownFunction when no adapter or version matches.</summary>
    public Manifest Adapter(string reference)
    {
        var parsed = Ids.ParseRef(reference);
        string name = byAlias.TryGetValue(parsed.Adapter, out var id) ? id : parsed.Adapter;
        if (!byId.TryGetValue(name, out var manifest)) throw new UnknownFunction($"No adapter {PyFormat.Str(parsed.Adapter)} in the catalogue");
        if (parsed.Version is not null && !Ids.VersionMatches(parsed.Version, manifest.Version))
        {
            throw new UnknownFunction($"{manifest.Id}@{parsed.Version} is not in the catalogue; it has {manifest.Version}");
        }
        return manifest;
    }

    /// <summary>The function a reference names (the adapter's only function when the reference names none). InvalidReference for a
    /// malformed reference; UnknownFunction when no adapter, version or function matches, or the adapter has several
    /// functions and the reference names none.</summary>
    public FunctionSpec Function(string reference)
    {
        var parsed = Ids.ParseRef(reference);
        var manifest = Adapter(reference);
        var keys = manifest.Functions.Keys;
        if (parsed.Key is null)
        {
            // a manifest read by Manifest.FromDict always has a function (the schema's minProperties); one built by hand may not
            if (keys.Count == 0) throw new UnknownFunction($"{manifest.Id} has no functions");
            if (keys.Count != 1)
            {
                throw new UnknownFunction($"{manifest.Id} has {keys.Count} functions ({string.Join(", ", keys)}); name one, e.g. {manifest.Id}.{keys[0]}");
            }
            return manifest.Functions[keys[0]];
        }
        if (!manifest.Functions.TryGetValue(parsed.Key, out var found))
        {
            throw new UnknownFunction($"{manifest.Id} has no function {PyFormat.Str(parsed.Key)}; its functions are {string.Join(", ", keys)}");
        }
        return found;
    }

    /// <summary>The manifest of the adapter `fn` belongs to; UnknownFunction when that adapter is not in this catalogue.</summary>
    public Manifest ManifestOf(FunctionSpec fn) =>
        byId.TryGetValue(fn.AdapterId, out var manifest) ? manifest : throw new UnknownFunction($"No adapter {PyFormat.Str(fn.AdapterId)} in the catalogue");

    /// <summary>Every function of a non-router adapter that implements `iface`, in catalogue order.</summary>
    public IReadOnlyList<FunctionSpec> Implementing(string iface) =>
        byId.Values.Where(manifest => !manifest.IsRouter).SelectMany(manifest => manifest.Functions.Values.Where(fn => fn.Interface == iface)).ToList();

    /// <summary>Every (base ID, version) pair bases.yaml lists; ManifestError for a base that is not a mapping of versions.</summary>
    public IReadOnlyList<(string Id, string Version)> BaseVersions() =>
        bases.SelectMany(pair => (pair.Value as JsonObject
            ?? throw new ManifestError($"bases.yaml: base {pair.Key} holds {PyFormat.Repr(pair.Value)}, not a mapping of its versions"))
            .Select(version => (pair.Key, version.Key))).ToList();

    /// <summary>A copy of bases.yaml's entry for this base version; ManifestError when it has none.</summary>
    public JsonObject Base(string baseId, string version)
    {
        if (bases[baseId] is not JsonObject versions || !versions.TryGetPropertyValue(version, out var entry) || entry is not JsonObject block)
        {
            throw new ManifestError($"Base {baseId} {version} is not in bases.yaml (known: {PyFormat.Repr(bases)})");
        }
        return (JsonObject)Json.Canonical(block)!;
    }
}
