namespace Jeff.Sdk;

/// <summary>A reference to an adapter or a function: jeff.&lt;domain&gt;.&lt;name&gt;[.&lt;function&gt;][@&lt;version&gt;],
/// or &lt;alias&gt;[.&lt;function&gt;] (port of jeff/ids.py).</summary>
internal sealed record FunctionRef(string Adapter, string? Key, string? Version);

internal static class Ids
{
    public static FunctionRef ParseRef(string reference)
    {
        int at = reference.IndexOf('@');
        string text = at < 0 ? reference : reference[..at];
        string version = at < 0 ? "" : reference[(at + 1)..];
        string[] parts = text.Split('.');
        if (text.Length == 0 || parts.Any(part => part.Length == 0))
        {
            throw new InvalidReference($"Malformed reference {PyFormat.Str(reference)}: use jeff.<domain>.<name>[.<function>] or "
                + "<alias>[.<function>], each part non-empty");
        }
        string adapter;
        string? key;
        if (parts[0] == "jeff")
        {
            if (parts.Length < 3) throw new InvalidReference($"Malformed reference {PyFormat.Str(reference)}: use jeff.<domain>.<name>[.<function>]");
            if (parts.Length > 4) throw new InvalidReference($"Malformed reference {PyFormat.Str(reference)}: too many parts");
            (adapter, key) = parts.Length == 4 ? (string.Join(".", parts[..3]), parts[3]) : (text, null);
        }
        else
        {
            if (parts.Length > 2)
            {
                throw new InvalidReference($"Malformed reference {PyFormat.Str(reference)}: use <alias>[.<function>] (aliases have no dots)");
            }
            (adapter, key) = parts.Length == 2 ? (parts[0], parts[1]) : (parts[0], null);
        }
        return new FunctionRef(adapter, key, version.Length > 0 ? version : null);
    }

    /// <summary>Whether `actual` is `wanted` or starts with it part by part ("1.3" matches "1.3.0", not "1.30.0").</summary>
    public static bool VersionMatches(string wanted, string actual)
    {
        string[] want = wanted.Split('.');
        string[] have = actual.Split('.').Take(want.Length).ToArray();
        return have.Length == want.Length && have.SequenceEqual(want, StringComparer.Ordinal);
    }
}
