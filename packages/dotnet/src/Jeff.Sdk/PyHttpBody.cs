using System.IO.Compression;
using System.Text;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>An answer body that could not be decoded (httpx's DecodingError).</summary>
internal sealed class DecodingFailure(string message) : Exception(message);

/// <summary>An answer body, or one of its Content-Encoding layers once undone, longer than PyHttpBody.MaxBody.</summary>
internal sealed class BodyTooLarge(bool decoded) : Exception
{
    /// <summary>True when the body as sent was within the limit and undoing an encoding passed it.</summary>
    public bool Decoded { get; } = decoded;
}

/// <summary>An HTTP answer's body read as the Python SDK reads it through httpx: Content-Encoding undone as httpx does
/// (gzip and deflate), the JSON parsed from the bytes as json.loads(bytes) parses it, and the text quoted in messages
/// decoded with the Content-Type charset (errors replaced), as httpx's Response.text.</summary>
internal static class PyHttpBody
{
    /// <summary>What this client accepts, as httpx's default header says (gzip, deflate; br and zstd only when their
    /// Python packages are installed, which the Python SDK does not require).</summary>
    public const string AcceptEncoding = "gzip, deflate";

    /// <summary>The longest answer body this client reads, as sent and after each Content-Encoding layer is undone (a
    /// few kilobytes of stacked gzip can decode to gigabytes): the channel's frame limit, 64 MiB. httpx has no limit.</summary>
    public const int MaxBody = Protocol.MaxFrame;

    private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    /// <summary>The body with every listed encoding httpx knows undone, the last applied first; encodings httpx does not
    /// know are passed over, as httpx passes them over.
    /// BodyTooLarge when the body or any layer of it decodes to more than `limit` bytes.</summary>
    public static byte[] Decoded(byte[] raw, IEnumerable<string> contentEncodings, int limit = MaxBody)
    {
        var known = contentEncodings.SelectMany(value => value.Split(','))
            .Select(value => value.Trim().ToLowerInvariant()).Where(value => value is "gzip" or "deflate" or "identity").ToList();
        byte[] body = raw;
        for (int index = known.Count - 1; index >= 0; index--)
        {
            body = known[index] switch
            {
                "gzip" => Inflate(body, data => new GZipStream(data, CompressionMode.Decompress), limit),
                // as httpx's DeflateDecoder: zlib-wrapped deflate, else raw deflate
                "deflate" => TryInflate(body, data => new ZLibStream(data, CompressionMode.Decompress), limit, out var zlib)
                    ? zlib : Inflate(body, data => new DeflateStream(data, CompressionMode.Decompress), limit),
                _ => body,
            };
        }
        return body;
    }

    /// <summary>One layer undone, reading at most `limit` bytes of its output: BodyTooLarge past it, so a bomb is
    /// never decoded in full.</summary>
    private static byte[] Inflate(byte[] body, Func<Stream, Stream> open, int limit)
    {
        try
        {
            using var input = open(new MemoryStream(body, writable: false));
            using var output = new MemoryStream();
            var buffer = new byte[64 * 1024];
            while (true)
            {
                int count = input.Read(buffer, 0, buffer.Length);
                if (count == 0) break;
                if (output.Length + count > limit) throw new BodyTooLarge(decoded: true);
                output.Write(buffer, 0, count);
            }
            return output.ToArray();
        }
        catch (InvalidDataException error)
        {
            throw new DecodingFailure(error.Message);
        }
    }

    private static bool TryInflate(byte[] body, Func<Stream, Stream> open, int limit, out byte[] decoded)
    {
        try
        {
            decoded = Inflate(body, open, limit);
            return true;
        }
        catch (DecodingFailure)
        {
            // httpx tries raw deflate next; that attempt's failure is the one reported
            decoded = [];
            return false;
        }
    }

    /// <summary>json.loads(bytes): the encoding found as json.detect_encoding finds it (a UTF-8, UTF-16 or UTF-32 byte
    /// order mark, else the pattern of zero bytes, else UTF-8), decoded strictly, then parsed. Bytes that do not decode
    /// are not JSON, as Python's UnicodeDecodeError is a ValueError.</summary>
    public static bool TryParseJson(byte[] body, out JsonNode? value)
    {
        value = null;
        string text;
        try
        {
            text = DecodeForJson(body);
        }
        catch (DecoderFallbackException)
        {
            return false;
        }
        return Json.TryParse(text, out value, out _);
    }

    private static string DecodeForJson(byte[] b)
    {
        static bool Starts(byte[] b, params byte[] prefix) => b.Length >= prefix.Length && b.AsSpan(0, prefix.Length).SequenceEqual(prefix);
        if (Starts(b, 0x00, 0x00, 0xFE, 0xFF)) return new UTF32Encoding(true, false, true).GetString(b, 4, b.Length - 4);
        if (Starts(b, 0xFF, 0xFE, 0x00, 0x00)) return new UTF32Encoding(false, false, true).GetString(b, 4, b.Length - 4);
        if (Starts(b, 0xFE, 0xFF)) return new UnicodeEncoding(true, false, true).GetString(b, 2, b.Length - 2);
        if (Starts(b, 0xFF, 0xFE)) return new UnicodeEncoding(false, false, true).GetString(b, 2, b.Length - 2);
        if (Starts(b, 0xEF, 0xBB, 0xBF)) return StrictUtf8.GetString(b, 3, b.Length - 3);
        if (b.Length >= 4)
        {
            if (b[0] == 0) return b[1] != 0 ? new UnicodeEncoding(true, false, true).GetString(b) : new UTF32Encoding(true, false, true).GetString(b);
            if (b[1] == 0) return b[2] != 0 || b[3] != 0 ? new UnicodeEncoding(false, false, true).GetString(b) : new UTF32Encoding(false, false, true).GetString(b);
        }
        else if (b.Length == 2)
        {
            if (b[0] == 0) return new UnicodeEncoding(true, false, true).GetString(b);
            if (b[1] == 0) return new UnicodeEncoding(false, false, true).GetString(b);
        }
        return StrictUtf8.GetString(b);
    }

    /// <summary>httpx's Response.text: the body decoded with the Content-Type's charset when it names an encoding,
    /// else UTF-8 (httpx's default_encoding), undecodable bytes replaced by U+FFFD.</summary>
    public static string Text(byte[] body, string? contentType)
    {
        if (body.Length == 0) return "";
        var encoding = EncodingOf(CharsetOf(contentType)) ?? Encoding.UTF8;
        var replacing = Encoding.GetEncoding(encoding.CodePage, EncoderFallback.ReplacementFallback, new DecoderReplacementFallback("�"));
        return replacing.GetString(body);
    }

    /// <summary>The charset parameter of a Content-Type, lower-cased and unquoted (email.message's get_content_charset).</summary>
    internal static string? CharsetOf(string? contentType)
    {
        if (contentType is null) return null;
        foreach (var parameter in contentType.Split(';').Skip(1))
        {
            int equals = parameter.IndexOf('=');
            if (equals < 0 || !parameter[..equals].Trim().Equals("charset", StringComparison.OrdinalIgnoreCase)) continue;
            string charset = parameter[(equals + 1)..].Trim();
            if (charset.Length >= 2 && charset[0] == '"' && charset[^1] == '"') charset = charset[1..^1];
            return charset.ToLowerInvariant();
        }
        return null;
    }

    /// <summary>The .NET encoding of a charset name, with Python's spellings of the common ones; null for a name this
    /// runtime does not know (httpx then decodes as UTF-8, its default, when Python does not know it either).</summary>
    private static Encoding? EncodingOf(string? charset)
    {
        if (charset is null) return null;
        string name = charset.Replace('_', '-') switch
        {
            "utf8" or "u8" or "utf" or "utf-8-sig" => "utf-8",
            "latin-1" or "latin1" or "l1" or "iso8859-1" or "8859" or "cp819" or "latin" => "iso-8859-1",
            "ascii" or "646" => "us-ascii",
            var other when other.StartsWith("cp125", StringComparison.Ordinal) && other.Length == 6 => "windows-" + other[2..],
            var other => other,
        };
        try
        {
            return Encoding.GetEncoding(name);
        }
        catch (ArgumentException)
        {
            return CodePagesEncodingProvider.Instance.GetEncoding(name);
        }
    }
}
