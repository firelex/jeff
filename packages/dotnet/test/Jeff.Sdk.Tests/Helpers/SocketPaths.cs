namespace Jeff.Sdk.Tests.Helpers;

/// <summary>A Unix socket path in a fresh temporary folder (short: macOS allows 104 bytes), removed with the folder.</summary>
internal sealed class SocketPaths : IDisposable
{
    private SocketPaths(string folder)
    {
        Folder = folder;
        Path = System.IO.Path.Combine(folder, "e.sock");
    }

    public string Folder { get; }

    public string Path { get; }

    public static SocketPaths New()
    {
        string folder = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "jeff" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(folder);
        return new SocketPaths(folder);
    }

    public void Dispose() => Directory.Delete(Folder, recursive: true);
}
