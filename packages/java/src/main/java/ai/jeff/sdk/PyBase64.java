package ai.jeff.sdk;

import java.io.ByteArrayOutputStream;

/** Python 3.13's base64.urlsafe_b64decode(text + padding) as the Python SDK reads a JWT's payload with it
 * (binascii.a2b_base64, not strict): '-' and '_' read as '+' and '/', every other character outside the alphabet and
 * '=' skipped, the bits left over after the last whole byte ignored. */
final class PyBase64 {
    private PyBase64() {}

    /** The decoded bytes; null where Python raises (a non-ASCII string, one data character more than a multiple of
     * four, or a last group too short for its padding). */
    static byte[] urlSafeDecode(String text) {
        for (int index = 0; index < text.length(); index++) if (text.charAt(index) > 0x7f) return null;
        String padded = text + "=".repeat((4 - text.length() % 4) % 4);
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        int quad = 0;
        int pads = 0;
        int bits = 0;
        int accumulated = 0;
        for (int index = 0; index < padded.length(); index++) {
            char character = padded.charAt(index);
            if (character == '=') {
                if (quad >= 2) pads++;
                continue;
            }
            int value;
            if (character >= 'A' && character <= 'Z') value = character - 'A';
            else if (character >= 'a' && character <= 'z') value = character - 'a' + 26;
            else if (character >= '0' && character <= '9') value = character - '0' + 52;
            else if (character == '+' || character == '-') value = 62;
            else if (character == '/' || character == '_') value = 63;
            else continue;
            pads = 0;
            quad = (quad + 1) % 4;
            accumulated = (accumulated << 6 | value) & 0xffffff;
            bits += 6;
            if (bits >= 8) {
                bits -= 8;
                output.write(accumulated >> bits);
            }
        }
        if (quad == 1 || (quad != 0 && quad + pads < 4)) return null;
        return output.toByteArray();
    }
}
