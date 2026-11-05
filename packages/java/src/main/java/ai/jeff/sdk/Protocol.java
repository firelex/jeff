package ai.jeff.sdk;

import java.io.EOFException;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.ReadableByteChannel;
import java.nio.channels.SelectableChannel;
import java.util.LinkedHashMap;
import java.util.Map;

/** The engine socket protocol (contract/socket.md): each frame is a 4-byte big-endian length, then a MessagePack map.
 * Replies carry the request id and may arrive in any order (port of jeff/protocol.py). */
final class Protocol {
    /** 2: the base served as adapter "base", and input_tokens in predict results. */
    public static final int PROTOCOL_VERSION = 2;
    public static final int MAX_FRAME = 64 * 1024 * 1024;
    public static final int HEADER_SIZE = 4;
    /** The server's low-latency channel (contract/socket.md, "The channel"): its path, the largest frame body it
     * reads (the HTTP API's body limit, 50 MiB) and the largest before the connection is authenticated. */
    public static final String CHANNEL_PATH = "/v1/channel";
    public static final int MAX_BODY = 50 * 1024 * 1024;
    public static final int PREAUTH_MAX_BODY = 64 * 1024;

    /** The socket schema's definition for each request op (contract/schemas/socket.schema.json). */
    private static final Map<String, String> OP_DEFINITIONS = Map.ofEntries(
        Map.entry("installed", "simple"), Map.entry("reload", "simple"), Map.entry("stats", "simple"),
        Map.entry("health", "simple"), Map.entry("predict", "predict"), Map.entry("serve", "serve"),
        Map.entry("auth", "auth"), Map.entry("refresh", "auth"), Map.entry("bind", "bind"), Map.entry("call", "call"),
        Map.entry("call_many", "call_many"), Map.entry("release", "release"), Map.entry("decide", "decide"),
        Map.entry("dispatch", "dispatch"), Map.entry("graph", "graph"));

    private Protocol() {}

    /** The one $defs entry of the socket schema a message can match, read from its tag (its op, or for a reply its
     * ok): each alternative of the schema's top-level oneOf is one entry that fixes its tag and allows no other
     * fields, so checking that entry accepts exactly what the whole schema accepts. Null when the tag names none,
     * and the whole schema is then checked. */
    static String socketDefinition(Object message) {
        if (!(message instanceof Map<?, ?> record)) return null;
        if (record.containsKey("op")) {
            if (!(record.get("op") instanceof String op)) return null;
            if (op.equals("hello")) return record.containsKey("adapters_version") ? "engine_hello" : "hello";
            return OP_DEFINITIONS.get(op);
        }
        if (Boolean.TRUE.equals(record.get("ok"))) return "ok_reply";
        if (Boolean.FALSE.equals(record.get("ok"))) return "failure_reply";
        return null;
    }

    /** The message if it matches the socket schema (or, with `definition`, that one of its $defs); InvalidRequest,
     * with every problem listed, if not. */
    static Map<String, Object> validateMessage(Object message, String where, String definition) {
        String shape = definition != null ? definition : socketDefinition(message);
        try {
            Contract.validate("socket", message, where, shape);
        } catch (JeffError.ContractError error) {
            throw new JeffError.InvalidRequest(error.getMessage());
        }
        return Values.map(message);
    }

    /** One frame: the message checked against the socket schema, packed (maps in their order), behind its 4-byte
     * length. InvalidRequest for a message that does not match, cannot be packed, or packs larger than 64 MiB. */
    public static byte[] encodeFrame(Map<String, Object> message, String definition) {
        validateMessage(message, "outgoing socket message", definition);
        byte[] frame;
        try {
            frame = MsgPack.encode(message, HEADER_SIZE);
        } catch (IllegalArgumentException error) {
            throw new JeffError.InvalidRequest("The outgoing socket message cannot be packed with msgpack: " + JeffError.describe(error));
        }
        int length = frame.length - HEADER_SIZE;
        if (length > MAX_FRAME) throw new JeffError.InvalidRequest("Socket message of " + length + " bytes is over the 64 MiB limit");
        frame[0] = (byte) (length >>> 24);
        frame[1] = (byte) (length >>> 16);
        frame[2] = (byte) (length >>> 8);
        frame[3] = (byte) length;
        return frame;
    }

    /** The body length a frame header announces (an unsigned 32-bit number); InvalidRequest over 64 MiB. */
    public static int frameLength(byte[] header) {
        if (header.length != HEADER_SIZE) {
            throw new IllegalArgumentException("A frame header is " + HEADER_SIZE + " bytes, not " + header.length);
        }
        long length = ((header[0] & 0xffL) << 24) | ((header[1] & 0xffL) << 16) | ((header[2] & 0xffL) << 8) | (header[3] & 0xffL);
        if (length > MAX_FRAME) throw new JeffError.InvalidRequest("Socket frame of " + length + " bytes is over the 64 MiB limit");
        return (int) length;
    }

    /** A frame body unpacked and checked against the socket schema; InvalidRequest when it is not MessagePack or
     * does not match. */
    public static Map<String, Object> decodeBody(byte[] body) {
        Object message;
        try {
            message = MsgPack.decode(body);
        } catch (IllegalArgumentException error) {
            throw new JeffError.InvalidRequest("Socket frame is not valid msgpack: " + error.getMessage());
        }
        return validateMessage(message, "incoming socket message", null);
    }

    /** Reads until `buffer` is full; false when the stream ended first (its position says how much was read). */
    private static boolean fill(ReadableByteChannel channel, ByteBuffer buffer) throws IOException {
        while (buffer.hasRemaining()) {
            if (channel.read(buffer) < 0) return false;
        }
        return true;
    }

    /** The next frame's body, not yet unpacked or checked: its header, then exactly the bytes it announces into one
     * array allocated once, after the length is checked against the 64 MiB limit. Null when the stream ended cleanly
     * between frames; EOFException when it ended in the middle of one; InvalidRequest for a header over 64 MiB. The
     * channel must be blocking. */
    public static byte[] readFrameBody(ReadableByteChannel channel) throws IOException {
        if (channel instanceof SelectableChannel selectable && !selectable.isBlocking()) {
            throw new IllegalArgumentException("readFrameBody needs a blocking channel; this one is in non-blocking mode");
        }
        ByteBuffer header = ByteBuffer.allocate(HEADER_SIZE);
        if (!fill(channel, header)) {
            if (header.position() == 0) return null;
            throw new EOFException("Socket closed in the middle of a frame header");
        }
        int length = frameLength(header.array());
        ByteBuffer body = ByteBuffer.allocate(length);
        if (!fill(channel, body)) {
            throw new EOFException("Socket closed in the middle of a frame body (got " + body.position() + " of " + length + " bytes)");
        }
        return body.array();
    }

    /** This side's hello: the protocol version it speaks. */
    public static Map<String, Object> hello() {
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("op", "hello");
        message.put("version", (long) PROTOCOL_VERSION);
        return message;
    }

    /** ProtocolMismatch unless `message` is a hello of this side's protocol version (compared as Python's ==). */
    public static void checkHello(Map<String, Object> message) {
        if (!"hello".equals(message.get("op"))) {
            throw new JeffError.ProtocolMismatch("Expected a hello, got " + PyFormat.repr(message.get("op")));
        }
        if (!PyFormat.pyEquals(message.get("version"), (long) PROTOCOL_VERSION)) {
            // Python writes the version with str(), not repr()
            throw new JeffError.ProtocolMismatch("This side speaks protocol " + PROTOCOL_VERSION + ", the other side "
                + PyFormat.toStr(message.get("version")) + "; upgrade the older one");
        }
    }
}
