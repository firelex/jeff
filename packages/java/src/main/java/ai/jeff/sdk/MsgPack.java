package ai.jeff.sdk;

import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.SequencedMap;

/** MessagePack exactly as msgpack-python writes it (msgpack.packb(value, use_bin_type=True)) and reads it
 * (msgpack.unpackb(raw=False, strict_map_key=True)): see Task 4 of the Java SDK plan for why no library is used. A
 * map is written in its iteration order, so it must keep one (a LinkedHashMap, a TreeMap, or any map of fewer than
 * two entries). */
final class MsgPack {
    private MsgPack() {}

    static byte[] encode(Object value) {
        return encode(value, 0);
    }

    /** The packed bytes behind `room` zero bytes (a frame's header is written into them afterwards). */
    static byte[] encode(Object value, int room) {
        return Nesting.run(Nesting.shallow(value), () -> {
            Out out = new Out(room);
            write(value, out, 0);
            return out.toArray();
        });
    }

    /** msgpack-python packs 1025 lists and maps inside one another and refuses one more ("recursion limit
     * exceeded."); it unpacks 1024 and refuses one more (StackError). Measured with msgpack 1.x on CPython 3.13. */
    static final int PACK_DEPTH = 1025;
    static final int UNPACK_DEPTH = 1024;

    private static final class Out {
        private byte[] bytes;
        private int size;

        Out(int room) {
            bytes = new byte[Math.max(256, room * 2)];
            size = room;
        }

        void ensure(int more) {
            long needed = (long) size + more;
            if (needed > Integer.MAX_VALUE - 8) throw new IllegalArgumentException("Cannot pack more than 2 GiB");
            if (needed > bytes.length) {
                bytes = Arrays.copyOf(bytes, (int) Math.min(Integer.MAX_VALUE - 8, Math.max(needed, 2L * bytes.length)));
            }
        }

        void put(int value) {
            ensure(1);
            bytes[size++] = (byte) value;
        }

        void put(byte[] data) {
            ensure(data.length);
            System.arraycopy(data, 0, bytes, size, data.length);
            size += data.length;
        }

        void putShort(int value) {
            put(value >>> 8);
            put(value);
        }

        void putInt(long value) {
            put((int) (value >>> 24));
            put((int) (value >>> 16));
            put((int) (value >>> 8));
            put((int) value);
        }

        void putLong(long value) {
            for (int shift = 56; shift >= 0; shift -= 8) put((int) (value >>> shift));
        }

        byte[] toArray() {
            return size == bytes.length ? bytes : Arrays.copyOf(bytes, size);
        }
    }

    private static void write(Object value, Out out, int depth) {
        if ((value instanceof Map<?, ?> || value instanceof List<?>) && depth + 1 > PACK_DEPTH) {
            throw new IllegalArgumentException("Cannot pack a value that nests more than " + PACK_DEPTH
                + " lists and maps inside one another (msgpack-python refuses it too: recursion limit exceeded)");
        }
        switch (value) {
            case null -> out.put(0xc0);
            case Boolean flag -> out.put(flag ? 0xc3 : 0xc2);
            case Long number -> writeLong(number, out);
            case Integer number -> writeLong(number, out);
            case Short number -> writeLong(number, out);
            case Byte number -> writeLong(number, out);
            case BigInteger number -> writeBig(number, out);
            case Double number -> {
                out.put(0xcb);
                out.putLong(Double.doubleToRawLongBits(number));
            }
            case String text -> writeString(text, out);
            case byte[] data -> {
                header(data.length, -1, 0, 0xc4, 0xc5, 0xc6, out);
                out.put(data);
            }
            case Map<?, ?> map -> writeMap(map, out, depth);
            case List<?> list -> {
                header(list.size(), 0x90, 16, -1, 0xdc, 0xdd, out);
                for (Object item : list) write(item, out, depth + 1);
            }
            default -> throw new IllegalArgumentException("Cannot pack a " + value.getClass().getName() + ": MessagePack "
                + "messages hold JSON values only (null, Boolean, Long, Integer, Double, String, byte[], List, Map with "
                + "String keys)");
        }
    }

    /** A size header: fix + size below fixLimit (when fix >= 0), else the 8-bit form (when code8 >= 0), the 16-bit or
     * the 32-bit one. */
    private static void header(int size, int fix, int fixLimit, int code8, int code16, int code32, Out out) {
        if (fix >= 0 && size < fixLimit) out.put(fix | size);
        else if (code8 >= 0 && size <= 0xff) {
            out.put(code8);
            out.put(size);
        } else if (size <= 0xffff) {
            out.put(code16);
            out.putShort(size);
        } else {
            out.put(code32);
            out.putInt(size);
        }
    }

    private static void writeLong(long value, Out out) {
        if (value >= 0) {
            if (value < 0x80) out.put((int) value);
            else if (value <= 0xff) {
                out.put(0xcc);
                out.put((int) value);
            } else if (value <= 0xffff) {
                out.put(0xcd);
                out.putShort((int) value);
            } else if (value <= 0xffffffffL) {
                out.put(0xce);
                out.putInt(value);
            } else {
                out.put(0xcf);
                out.putLong(value);
            }
        } else if (value >= -32) {
            out.put((int) value);
        } else if (value >= -128) {
            out.put(0xd0);
            out.put((int) value);
        } else if (value >= -32768) {
            out.put(0xd1);
            out.putShort((int) value);
        } else if (value >= Integer.MIN_VALUE) {
            out.put(0xd2);
            out.putInt(value);
        } else {
            out.put(0xd3);
            out.putLong(value);
        }
    }

    private static void writeBig(BigInteger value, Out out) {
        if (value.bitLength() <= 63) writeLong(value.longValue(), out);
        else if (value.signum() > 0 && value.bitLength() == 64) {
            out.put(0xcf);
            out.putLong(value.longValue());
        } else {
            throw new OverflowError();
        }
    }

    /** A whole number outside [-2^63, 2^64 - 1], refused as msgpack-python refuses it: OverflowError("Integer value
     * out of range"). The class is named after Python's exception, so JeffError.describe(error) reads
     * "OverflowError: Integer value out of range", the text Python's request path embeds in the InvalidRequest a
     * caller sees (jeff/protocol.py's encode: "... cannot be packed with msgpack: OverflowError: Integer value out
     * of range"). */
    static final class OverflowError extends IllegalArgumentException {
        OverflowError() {
            super("Integer value out of range");
        }
    }

    private static void writeString(String text, Out out) {
        byte[] data = utf8(text);
        header(data.length, 0xa0, 32, 0xd9, 0xda, 0xdb, out);
        out.put(data);
    }

    private static byte[] utf8(String text) {
        boolean surrogates = false;
        for (int index = 0; index < text.length() && !surrogates; index++) surrogates = Character.isSurrogate(text.charAt(index));
        if (!surrogates) return text.getBytes(StandardCharsets.UTF_8);
        try {
            ByteBuffer encoded = StandardCharsets.UTF_8.newEncoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).encode(CharBuffer.wrap(text));
            byte[] data = new byte[encoded.remaining()];
            encoded.get(data);
            return data;
        } catch (CharacterCodingException error) {
            throw new IllegalArgumentException("Cannot pack a string holding a lone surrogate (" + error.getMessage()
                + "): it has no UTF-8 form", error);
        }
    }

    private static void writeMap(Map<?, ?> map, Out out, int depth) {
        if (map.size() > 1 && !(map instanceof SequencedMap<?, ?>)) {
            throw new IllegalArgumentException("Cannot pack a " + map.getClass().getName() + " of " + map.size()
                + " entries: it keeps no key order, and the engine reads keys in order; use a LinkedHashMap");
        }
        header(map.size(), 0x80, 16, -1, 0xde, 0xdf, out);
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (!(entry.getKey() instanceof String key)) {
                throw new IllegalArgumentException("Cannot pack a map key that is not a string: " + entry.getKey());
            }
            writeString(key, out);
            write(entry.getValue(), out, depth + 1);
        }
    }

    static Object decode(byte[] bytes) {
        return Nesting.run(Nesting.shallowMsgPack(bytes), () -> {
            In in = new In(bytes);
            Object value = in.read(0);
            if (in.position != bytes.length) {
                throw new IllegalArgumentException((bytes.length - in.position) + " extra bytes after the packed value");
            }
            return value;
        });
    }

    private static final class In {
        private final byte[] bytes;
        private int position;

        In(byte[] bytes) {
            this.bytes = bytes;
        }

        private void need(long count) {
            if (bytes.length - position < count) throw new IllegalArgumentException("the packed value ends early, at byte " + position);
        }

        private int u8() {
            need(1);
            return bytes[position++] & 0xff;
        }

        private int u16() {
            return (u8() << 8) | u8();
        }

        private long u32() {
            return ((long) u16() << 16) | u16();
        }

        private long u64() {
            return (u32() << 32) | u32();
        }

        private int length(long size) {
            if (size > Integer.MAX_VALUE) throw new IllegalArgumentException("a packed length of " + size + " is too large");
            return (int) size;
        }

        Object read(int depth) {
            int code = u8();
            boolean container = (code & 0xf0) == 0x90 || (code & 0xf0) == 0x80 || code == 0xdc || code == 0xdd
                || code == 0xde || code == 0xdf;
            if (container && depth + 1 > UNPACK_DEPTH) {
                throw new IllegalArgumentException("the packed value nests more than " + UNPACK_DEPTH
                    + " arrays and maps inside one another, at byte " + (position - 1) + " (msgpack-python refuses it too)");
            }
            if (code <= 0x7f) return (long) code;
            if (code >= 0xe0) return (long) (byte) code;
            if ((code & 0xe0) == 0xa0) return string(code & 0x1f);
            if ((code & 0xf0) == 0x90) return array(code & 0x0f, depth);
            if ((code & 0xf0) == 0x80) return map(code & 0x0f, depth);
            return switch (code) {
                case 0xc0 -> null;
                case 0xc2 -> false;
                case 0xc3 -> true;
                case 0xc4 -> binary(u8());
                case 0xc5 -> binary(u16());
                case 0xc6 -> binary(length(u32()));
                case 0xca -> (double) Float.intBitsToFloat((int) u32());
                case 0xcb -> Double.longBitsToDouble(u64());
                case 0xcc -> (long) u8();
                case 0xcd -> (long) u16();
                case 0xce -> u32();
                case 0xcf -> {
                    long value = u64();
                    yield value >= 0 ? (Object) value : new BigInteger(Long.toUnsignedString(value));
                }
                case 0xd0 -> (long) (byte) u8();
                case 0xd1 -> (long) (short) u16();
                case 0xd2 -> (long) (int) u32();
                case 0xd3 -> u64();
                case 0xd9 -> string(u8());
                case 0xda -> string(u16());
                case 0xdb -> string(length(u32()));
                case 0xdc -> array(u16(), depth);
                case 0xdd -> array(length(u32()), depth);
                case 0xde -> map(u16(), depth);
                case 0xdf -> map(length(u32()), depth);
                default -> throw new IllegalArgumentException(String.format("byte 0x%02x at %d is not a MessagePack type "
                    + "the Jeff protocol uses (extension types and 0xc1 are not)", code, position - 1));
            };
        }

        private byte[] binary(int size) {
            need(size);
            byte[] out = Arrays.copyOfRange(bytes, position, position + size);
            position += size;
            return out;
        }

        private String string(int size) {
            need(size);
            try {
                String text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes, position, size)).toString();
                position += size;
                return text;
            } catch (CharacterCodingException error) {
                throw new IllegalArgumentException("a packed string at byte " + position + " is not valid UTF-8", error);
            }
        }

        private List<Object> array(int size, int depth) {
            need(size);
            List<Object> out = new ArrayList<>(size);
            for (int index = 0; index < size; index++) out.add(read(depth + 1));
            return out;
        }

        private Map<String, Object> map(int size, int depth) {
            need(2L * size);
            Map<String, Object> out = new LinkedHashMap<>();
            for (int index = 0; index < size; index++) {
                Object key = read(depth + 1);
                if (!(key instanceof String text)) throw new IllegalArgumentException("a map key that is not a string: " + key);
                out.put(text, read(depth + 1));
            }
            return out;
        }
    }
}
