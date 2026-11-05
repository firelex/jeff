namespace Jeff.Sdk.Tests;

/// <summary>The repository the tests run in: the first folder above the test binary that holds contract/conformance
/// and packages/dotnet.</summary>
internal static class Repo
{
    public static readonly string Root = Find();

    private static string Find()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            if (Directory.Exists(Path.Combine(directory.FullName, "contract", "conformance"))
                && Directory.Exists(Path.Combine(directory.FullName, "packages", "dotnet")))
            {
                return directory.FullName;
            }
        }
        throw new InvalidOperationException(
            $"No folder above {AppContext.BaseDirectory} holds contract/conformance and packages/dotnet; run the tests inside the repository");
    }

    public static string PathOf(params string[] parts) => Path.Combine([Root, .. parts]);
}
