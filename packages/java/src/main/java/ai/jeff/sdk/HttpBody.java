package ai.jeff.sdk;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.Charset;
import java.nio.charset.CharsetDecoder;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.IllegalCharsetNameException;
import java.nio.charset.StandardCharsets;
import java.nio.charset.UnsupportedCharsetException;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.zip.CRC32;
import java.util.zip.DataFormatException;
import java.util.zip.Inflater;

/** An HTTP answer read as the Python SDK reads it through httpx: its header values decoded as httpx decodes them,
 * its Content-Encoding undone as httpx undoes it (gzip and deflate), its JSON parsed from the bytes as json.loads(bytes)
 * parses it, and the text quoted in messages decoded as httpx's Response.text decodes it. */
final class HttpBody {
    /** What this client accepts, as httpx's default header says (br and zstd only when their Python packages are
     * installed, which the Python SDK does not require). */
    static final String ACCEPT_ENCODING = "gzip, deflate";

    /** A body that could not be decoded (httpx's DecodingError), with zlib's message as Python shows it. */
    static class DecodingError extends Exception {
        DecodingError(String message) {
            super(message);
        }
    }

    /** A decoded body past the limit: never retried as another encoding. */
    static final class TooLarge extends DecodingError {
        TooLarge(long limit) {
            super("the decoded answer is larger than " + limit + " bytes, more than this SDK holds");
        }
    }

    private HttpBody() {}

    /** The header fields' text, as httpx's Headers decodes them: all as ASCII when every name and value is ASCII, else
     * all as UTF-8 when every one is UTF-8, else all as Latin-1. */
    static List<String[]> headers(List<byte[][]> fields) {
        Charset charset = StandardCharsets.ISO_8859_1;
        if (fields.stream().allMatch(field -> ascii(field[0]) && ascii(field[1]))) charset = StandardCharsets.US_ASCII;
        else if (fields.stream().allMatch(field -> utf8(field[0]) && utf8(field[1]))) charset = StandardCharsets.UTF_8;
        List<String[]> text = new ArrayList<>();
        for (byte[][] field : fields) text.add(new String[] {new String(field[0], charset), new String(field[1], charset)});
        return text;
    }

    private static boolean ascii(byte[] bytes) {
        for (byte item : bytes) if (item < 0) return false;
        return true;
    }

    private static boolean utf8(byte[] bytes) {
        try {
            StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes));
            return true;
        } catch (java.nio.charset.CharacterCodingException notUtf8) {
            return false;
        }
    }

    /** httpx's headers.get: every value of the field joined by ", "; null when there is none. */
    static String header(List<String[]> headers, String name) {
        List<String> values = new ArrayList<>();
        for (String[] field : headers) if (field[0].equals(name)) values.add(field[1]);
        return values.isEmpty() ? null : String.join(", ", values);
    }

    /** The body with every Content-Encoding httpx knows undone, the last applied first; the ones it does not know are
     * passed over, as httpx passes them over. What each layer decodes to is held to the same limit as a body as sent
     * (HttpConnection.MAX_BODY), so a few KiB of stacked gzip cannot fill the heap. */
    static byte[] decoded(byte[] raw, List<String[]> headers) throws DecodingError {
        return decoded(raw, headers, HttpConnection.MAX_BODY);
    }

    /** decoded() with each layer held to `limit` bytes (TooLarge past it). */
    static byte[] decoded(byte[] raw, List<String[]> headers, long limit) throws DecodingError {
        List<String> known = new ArrayList<>();
        for (String[] field : headers) {
            if (!field[0].equals("content-encoding")) continue;
            for (String value : field[1].split(",")) {
                String coding = value.strip().toLowerCase(Locale.ROOT);
                if (coding.equals("gzip") || coding.equals("deflate") || coding.equals("identity")) known.add(coding);
            }
        }
        byte[] body = raw;
        for (int index = known.size() - 1; index >= 0; index--) {
            String coding = known.get(index);
            if (coding.equals("gzip")) body = gunzip(body, limit);
            else if (coding.equals("deflate")) body = deflate(body, limit);
        }
        return body;
    }

    private static DecodingError zlibError(String message) {
        return new DecodingError("Error -3 while decompressing data: " + message);
    }

    /** As httpx's DeflateDecoder: zlib-wrapped deflate, else raw deflate (whose failure is the one reported). */
    private static byte[] deflate(byte[] body, long limit) throws DecodingError {
        try {
            return inflate(body, 0, false, limit).output;
        } catch (TooLarge tooLarge) {
            throw tooLarge;
        } catch (DecodingError notZlib) {
            return inflate(body, 0, true, limit).output;
        }
    }

    private record Inflated(byte[] output, int used, boolean finished) {}

    /** Inflates from `from` until the stream ends or the input runs out; a stream cut short gives what was decoded,
     * as zlib's decompressobj does (it reports no error for a truncated stream). More than `limit` bytes is TooLarge. */
    private static Inflated inflate(byte[] body, int from, boolean raw, long limit) throws DecodingError {
        Inflater inflater = new Inflater(raw);
        try {
            inflater.setInput(body, from, body.length - from);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[16 * 1024];
            while (!inflater.finished()) {
                int count = inflater.inflate(buffer);
                if (count > 0) {
                    if (out.size() + (long) count > limit) throw new TooLarge(limit);
                    out.write(buffer, 0, count);
                    continue;
                }
                if (inflater.needsInput() || inflater.needsDictionary()) break;
            }
            return new Inflated(out.toByteArray(), body.length - from - inflater.getRemaining(), inflater.finished());
        } catch (DataFormatException error) {
            throw zlibError(error.getMessage());
        } finally {
            inflater.end();
        }
    }

    /** As zlib.decompressobj(MAX_WBITS | 16), which httpx's GZipDecoder uses: one gzip member (bytes after it are
     * passed over), its header and trailer checked with zlib's messages; a member cut short gives what was decoded. */
    private static byte[] gunzip(byte[] body, long limit) throws DecodingError {
        if (body.length == 0) return body;
        int at = 0;
        if (body.length >= 1 && (body[0] & 0xff) != 0x1f || body.length >= 2 && (body[1] & 0xff) != 0x8b) {
            throw zlibError("incorrect header check");
        }
        if (body.length < 10) return new byte[0];
        if ((body[2] & 0xff) != 8) throw zlibError("unknown compression method");
        int flags = body[3] & 0xff;
        if ((flags & 0xe0) != 0) throw zlibError("unknown header flags set");
        at = 10;
        if ((flags & 0x04) != 0) {
            if (at + 2 > body.length) return new byte[0];
            at += 2 + ((body[at] & 0xff) | (body[at + 1] & 0xff) << 8);
        }
        if ((flags & 0x08) != 0) at = afterZero(body, at);
        if ((flags & 0x10) != 0) at = afterZero(body, at);
        if (at < 0 || at > body.length) return new byte[0];
        if ((flags & 0x02) != 0) {
            if (at + 2 > body.length) return new byte[0];
            CRC32 crc = new CRC32();
            crc.update(body, 0, at);
            int expected = (body[at] & 0xff) | (body[at + 1] & 0xff) << 8;
            if (((int) crc.getValue() & 0xffff) != expected) throw zlibError("header crc mismatch");
            at += 2;
        }
        Inflated inflated = inflate(body, at, true, limit);
        if (!inflated.finished()) return inflated.output();
        int trailer = at + inflated.used();
        if (trailer + 8 > body.length) return inflated.output();
        CRC32 crc = new CRC32();
        crc.update(inflated.output());
        long check = (body[trailer] & 0xffL) | (body[trailer + 1] & 0xffL) << 8 | (body[trailer + 2] & 0xffL) << 16
            | (body[trailer + 3] & 0xffL) << 24;
        if (check != crc.getValue()) throw zlibError("incorrect data check");
        long size = (body[trailer + 4] & 0xffL) | (body[trailer + 5] & 0xffL) << 8 | (body[trailer + 6] & 0xffL) << 16
            | (body[trailer + 7] & 0xffL) << 24;
        if (size != (inflated.output().length & 0xffffffffL)) throw zlibError("incorrect length check");
        return inflated.output();
    }

    private static int afterZero(byte[] body, int at) {
        if (at < 0) return at;
        for (int index = at; index < body.length; index++) if (body[index] == 0) return index + 1;
        return -1;
    }

    /** json.loads(bytes): the encoding found as json.detect_encoding finds it (a UTF-32, UTF-16 or UTF-8 byte order
     * mark, else the pattern of zero bytes, else UTF-8), decoded with Python's 'surrogatepass', then parsed. Bytes that
     * do not decode are not JSON (Python's UnicodeDecodeError is a ValueError, as JSONDecodeError is). */
    static Object parseJson(byte[] body) throws NotJson {
        String text;
        try {
            text = PyText.decodeForJson(body);
        } catch (PyText.Undecodable error) {
            throw new NotJson(error.getMessage());
        }
        try {
            return Json.parse(text);
        } catch (IllegalArgumentException error) {
            throw new NotJson(error.getMessage());
        }
    }

    /** A body that json.loads refuses. */
    static final class NotJson extends Exception {
        NotJson(String message) {
            super(message);
        }
    }

    /** httpx's Response.text: the body decoded with the charset its Content-Type names when Python knows that
     * encoding, else UTF-8 (httpx's default), bytes that do not decode replaced by U+FFFD. */
    static String text(byte[] body, List<String[]> headers) {
        if (body.length == 0) return "";
        Charset charset = charsetOf(header(headers, "content-type"));
        CharsetDecoder decoder = charset.newDecoder().onMalformedInput(CodingErrorAction.REPLACE)
            .onUnmappableCharacter(CodingErrorAction.REPLACE).replaceWith("�");
        try {
            CharBuffer decoded = decoder.decode(ByteBuffer.wrap(body));
            return decoded.toString();
        } catch (java.nio.charset.CharacterCodingException impossible) {
            throw new IllegalStateException("A decoder that replaces bad input reported it", impossible);
        }
    }

    /** Python's names of the common encodings that Java spells otherwise. */
    private static final Map<String, String> PYTHON_NAMES = Map.of("utf8", "UTF-8", "u8", "UTF-8", "utf", "UTF-8",
        "utf-8-sig", "UTF-8", "latin-1", "ISO-8859-1", "latin", "ISO-8859-1", "l1", "ISO-8859-1", "646", "US-ASCII");

    /** The charset parameter of a Content-Type (email.message's get_content_charset: lower-cased, unquoted) as a Java
     * charset; UTF-8 when there is none or this runtime does not know it. */
    static Charset charsetOf(String contentType) {
        if (contentType == null) return StandardCharsets.UTF_8;
        String[] parameters = contentType.split(";");
        for (int index = 1; index < parameters.length; index++) {
            int equals = parameters[index].indexOf('=');
            if (equals < 0 || !parameters[index].substring(0, equals).strip().equalsIgnoreCase("charset")) continue;
            String name = parameters[index].substring(equals + 1).strip();
            if (name.length() >= 2 && name.startsWith("\"") && name.endsWith("\"")) name = name.substring(1, name.length() - 1);
            name = name.toLowerCase(Locale.ROOT);
            String java = PYTHON_NAMES.getOrDefault(name.replace('_', '-'), name);
            try {
                return Charset.forName(java);
            } catch (IllegalCharsetNameException | UnsupportedCharsetException unknown) {
                // httpx decodes as UTF-8 when it does not know the charset named (_is_known_encoding is false)
                return StandardCharsets.UTF_8;
            }
        }
        return StandardCharsets.UTF_8;
    }
}
