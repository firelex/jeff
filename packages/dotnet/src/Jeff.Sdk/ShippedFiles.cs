using System.Reflection;
using System.Text;

namespace Jeff.Sdk;

/// <summary>What the package carries from the repository: contract/schemas, interfaces.yaml, errors.yaml and
/// catalogue/, copied into packages/dotnet/shipped by scripts/sync-shipped.sh and embedded in the assembly under
/// "shipped/&lt;path&gt;".</summary>
internal static class ShippedFiles
{
    private const string Prefix = "shipped/";
    private static readonly Assembly Assembly = typeof(ShippedFiles).Assembly;
    private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    private static readonly Lazy<IReadOnlyDictionary<string, string>> Resources = new(() =>
        Assembly.GetManifestResourceNames()
            .Select(name => (Path: name.Replace('\\', '/'), Name: name))
            .Where(item => item.Path.StartsWith(Prefix, StringComparison.Ordinal))
            .ToDictionary(item => item.Path[Prefix.Length..], item => item.Name, StringComparer.Ordinal));

    /// <summary>Every shipped file's path (relative to shipped/), in code-point order.</summary>
    public static IReadOnlyList<string> All => PyFormat.Sorted(Resources.Value.Keys);

    /// <summary>The shipped files under `folder` (e.g. "catalogue"), in code-point order.</summary>
    public static IReadOnlyList<string> Files(string folder) => All.Where(path => path.StartsWith(folder + "/", StringComparison.Ordinal)).ToList();

    public static string ReadText(string path)
    {
        if (!Resources.Value.TryGetValue(path, out var name))
        {
            throw new ContractError($"The Jeff.Sdk assembly carries no shipped/{path}; run packages/dotnet/scripts/sync-shipped.sh and build again");
        }
        using var stream = Assembly.GetManifestResourceStream(name)
            ?? throw new ContractError($"The embedded resource {name} could not be opened");
        using var reader = new StreamReader(stream, StrictUtf8);
        return reader.ReadToEnd();
    }
}
