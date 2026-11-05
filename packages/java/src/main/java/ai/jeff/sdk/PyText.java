package ai.jeff.sdk;

/** Bytes decoded to text as json.loads(bytes) decodes them: json.detect_encoding picks the encoding, and the codec
 * runs with Python's 'surrogatepass' error handler, which lets an encoded lone surrogate through and refuses every
 * other malformed sequence. */
final class PyText {
    /** Bytes the chosen codec refuses (Python's UnicodeDecodeError). */
    static final class Undecodable extends Exception {
        Undecodable(String codec, int position, String reason) {
            super("'" + codec + "' codec can't decode bytes in position " + position + ": " + reason);
        }
    }

    private PyText() {}

    private static boolean startsWith(byte[] bytes, int... prefix) {
        if (bytes.length < prefix.length) return false;
        for (int index = 0; index < prefix.length; index++) if ((bytes[index] & 0xff) != prefix[index]) return false;
        return true;
    }

    static String decodeForJson(byte[] b) throws Undecodable {
        if (startsWith(b, 0x00, 0x00, 0xFE, 0xFF)) return utf32(b, 4, true, "utf-32");
        if (startsWith(b, 0xFF, 0xFE, 0x00, 0x00)) return utf32(b, 4, false, "utf-32");
        if (startsWith(b, 0xFE, 0xFF)) return utf16(b, 2, true, "utf-16");
        if (startsWith(b, 0xFF, 0xFE)) return utf16(b, 2, false, "utf-16");
        if (startsWith(b, 0xEF, 0xBB, 0xBF)) return utf8(b, 3, "utf-8-sig");
        if (b.length >= 4) {
            if (b[0] == 0) return b[1] != 0 ? utf16(b, 0, true, "utf-16-be") : utf32(b, 0, true, "utf-32-be");
            if (b[1] == 0) return b[2] != 0 || b[3] != 0 ? utf16(b, 0, false, "utf-16-le") : utf32(b, 0, false, "utf-32-le");
        } else if (b.length == 2) {
            if (b[0] == 0) return utf16(b, 0, true, "utf-16-be");
            if (b[1] == 0) return utf16(b, 0, false, "utf-16-le");
        }
        return utf8(b, 0, "utf-8");
    }

    private static int continuation(byte[] b, int at) {
        return at < b.length && (b[at] & 0xc0) == 0x80 ? b[at] & 0x3f : -1;
    }

    /** Python's strict UTF-8 (no overlong forms, nothing above U+10FFFF), with encoded surrogates let through. */
    static String utf8(byte[] b, int from, String codec) throws Undecodable {
        StringBuilder out = new StringBuilder(b.length - from);
        int at = from;
        while (at < b.length) {
            int lead = b[at] & 0xff;
            if (lead < 0x80) {
                out.append((char) lead);
                at++;
                continue;
            }
            int code;
            int size;
            if (lead >= 0xc2 && lead <= 0xdf) {
                int c1 = continuation(b, at + 1);
                if (c1 < 0) throw new Undecodable(codec, at, "invalid continuation byte");
                code = (lead & 0x1f) << 6 | c1;
                size = 2;
            } else if (lead >= 0xe0 && lead <= 0xef) {
                int c1 = continuation(b, at + 1);
                if (c1 < 0 || lead == 0xe0 && c1 < 0x20) throw new Undecodable(codec, at, "invalid continuation byte");
                int c2 = continuation(b, at + 2);
                if (c2 < 0) throw new Undecodable(codec, at, "invalid continuation byte");
                code = (lead & 0x0f) << 12 | c1 << 6 | c2;
                size = 3;
            } else if (lead >= 0xf0 && lead <= 0xf4) {
                int c1 = continuation(b, at + 1);
                if (c1 < 0 || lead == 0xf0 && c1 < 0x10 || lead == 0xf4 && c1 >= 0x10) {
                    throw new Undecodable(codec, at, "invalid continuation byte");
                }
                int c2 = continuation(b, at + 2);
                int c3 = continuation(b, at + 3);
                if (c2 < 0 || c3 < 0) throw new Undecodable(codec, at, "invalid continuation byte");
                code = (lead & 0x07) << 18 | c1 << 12 | c2 << 6 | c3;
                size = 4;
            } else {
                throw new Undecodable(codec, at, "invalid start byte");
            }
            out.appendCodePoint(code);
            at += size;
        }
        return out.toString();
    }

    /** UTF-16 in the given byte order, lone surrogates let through; an odd byte left over is truncated data. */
    static String utf16(byte[] b, int from, boolean bigEndian, String codec) throws Undecodable {
        if ((b.length - from) % 2 != 0) throw new Undecodable(codec, b.length - 1, "truncated data");
        StringBuilder out = new StringBuilder((b.length - from) / 2);
        for (int at = from; at < b.length; at += 2) {
            int high = b[at] & 0xff;
            int low = b[at + 1] & 0xff;
            out.append((char) (bigEndian ? high << 8 | low : low << 8 | high));
        }
        return out.toString();
    }

    /** UTF-32 in the given byte order, surrogates let through, nothing above U+10FFFF. */
    static String utf32(byte[] b, int from, boolean bigEndian, String codec) throws Undecodable {
        if ((b.length - from) % 4 != 0) throw new Undecodable(codec, b.length - (b.length - from) % 4, "truncated data");
        StringBuilder out = new StringBuilder((b.length - from) / 4);
        for (int at = from; at < b.length; at += 4) {
            long code = bigEndian
                ? (b[at] & 0xffL) << 24 | (b[at + 1] & 0xffL) << 16 | (b[at + 2] & 0xffL) << 8 | b[at + 3] & 0xffL
                : (b[at + 3] & 0xffL) << 24 | (b[at + 2] & 0xffL) << 16 | (b[at + 1] & 0xffL) << 8 | b[at] & 0xffL;
            if (code > 0x10ffff) throw new Undecodable(codec, at, "code point not in range(0x110000)");
            if (code >= 0xd800 && code <= 0xdfff) out.append((char) code);
            else out.appendCodePoint((int) code);
        }
        return out.toString();
    }
}
