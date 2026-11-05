package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.EOFException;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.ReadableByteChannel;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class ProtocolTest {
    /** A channel that hands out `chunk` bytes per read, then the end of the stream. */
    private static ReadableByteChannel chunked(byte[] data, int chunk) {
        return new ReadableByteChannel() {
            private int position;

            public int read(ByteBuffer target) {
                if (position == data.length) return -1;
                int count = Math.min(Math.min(chunk, data.length - position), target.remaining());
                target.put(data, position, count);
                position += count;
                return count;
            }

            public boolean isOpen() {
                return true;
            }

            public void close() {}
        };
    }

    private static byte[] concat(byte[]... parts) {
        int size = 0;
        for (byte[] part : parts) size += part.length;
        byte[] out = new byte[size];
        int at = 0;
        for (byte[] part : parts) {
            System.arraycopy(part, 0, out, at, part.length);
            at += part.length;
        }
        return out;
    }

    private static final Map<String, Object> INSTALLED = Fixtures.ordered("op", "installed", "id", 1L);
    private static final Map<String, Object> HEALTH = Fixtures.ordered("op", "health", "id", 2L);

    @Test
    void aHeaderSplitByteByByteGivesTheFrameOnceItIsWhole() throws IOException {
        ReadableByteChannel channel = chunked(Protocol.encodeFrame(INSTALLED, null), 1);
        assertEquals(INSTALLED, Protocol.decodeBody(Protocol.readFrameBody(channel)));
        assertNull(Protocol.readFrameBody(channel));
    }

    @Test
    void twoFramesInOneReadAreBothRead() throws IOException {
        ReadableByteChannel channel = chunked(concat(Protocol.encodeFrame(INSTALLED, null), Protocol.encodeFrame(HEALTH, null)), 1 << 20);
        assertEquals(INSTALLED, Protocol.decodeBody(Protocol.readFrameBody(channel)));
        assertEquals(HEALTH, Protocol.decodeBody(Protocol.readFrameBody(channel)));
    }

    @Test
    void anEightMebibyteBodyIn64KibChunksIsRead() throws IOException {
        Map<String, Object> row = Fixtures.ordered("state", "x".repeat(8 * 1024 * 1024), "question",
            Fixtures.ordered("type", "noul", "instructions", "?"));
        Map<String, Object> message = Fixtures.ordered("op", "predict", "adapter", "jeff.safety.spam", "rows", List.of(row), "id", 3L);
        byte[] frame = Protocol.encodeFrame(message, null);
        byte[] body = Protocol.readFrameBody(chunked(frame, 64 * 1024));
        assertArrayEquals(Arrays.copyOfRange(frame, 4, frame.length), body);
        assertEquals(message, Protocol.decodeBody(body));
    }

    @Test
    void aHeaderOverSixtyFourMebibytesIsRefusedAsSoonAsItIsRead() {
        byte[] header = {0x04, 0x00, 0x00, 0x01};
        assertEquals("Socket frame of 67108865 bytes is over the 64 MiB limit",
            assertThrows(JeffError.InvalidRequest.class, () -> Protocol.readFrameBody(chunked(header, 4))).getMessage());
    }

    @Test
    void theLargestHeaderAnUnsignedIntCanHoldIsRefusedWithoutOverflow() {
        byte[] header = {(byte) 0xff, (byte) 0xff, (byte) 0xff, (byte) 0xff};
        assertEquals("Socket frame of 4294967295 bytes is over the 64 MiB limit",
            assertThrows(JeffError.InvalidRequest.class, () -> Protocol.frameLength(header)).getMessage());
    }

    @Test
    void aHeaderOfExactlySixtyFourMebibytesIsAccepted() {
        assertEquals(Protocol.MAX_FRAME, Protocol.frameLength(new byte[] {0x04, 0x00, 0x00, 0x00}));
    }

    @Test
    void aHeaderOfTheWrongSizeIsRefused() {
        assertThrows(IllegalArgumentException.class, () -> Protocol.frameLength(new byte[] {0x00, 0x01}));
    }

    @Test
    void aStreamThatEndsInTheMiddleOfAFrameIsAnEofException() {
        byte[] frame = Protocol.encodeFrame(INSTALLED, null);
        EOFException error = assertThrows(EOFException.class,
            () -> Protocol.readFrameBody(chunked(Arrays.copyOf(frame, frame.length - 1), 3)));
        assertEquals("Socket closed in the middle of a frame body (got 17 of 18 bytes)", error.getMessage());
    }

    @Test
    void aStreamThatEndsInTheMiddleOfAHeaderIsAnEofException() {
        EOFException error = assertThrows(EOFException.class, () -> Protocol.readFrameBody(chunked(new byte[] {0, 0}, 1)));
        assertEquals("Socket closed in the middle of a frame header", error.getMessage());
    }

    @Test
    void anOutgoingMessageThatDoesNotMatchTheSchemaIsRefused() {
        JeffError.InvalidRequest error = assertThrows(JeffError.InvalidRequest.class,
            () -> Protocol.encodeFrame(Fixtures.ordered("op", "predict", "id", 1L, "adapter", "a"), null));
        assertTrue(error.getMessage().startsWith("outgoing socket message does not match predict in the socket schema:"), error.getMessage());
    }

    @Test
    void anIncomingMessageThatDoesNotMatchTheSchemaListsEveryProblem() {
        // Python: protocol.decode(b'\x81\xa2op\xa7predict')
        JeffError.InvalidRequest error = assertThrows(JeffError.InvalidRequest.class,
            () -> Protocol.decodeBody(MsgPack.encode(Fixtures.ordered("op", "predict"))));
        assertEquals("incoming socket message does not match predict in the socket schema:\n"
            + "  at /: 'id' is a required property\n"
            + "  at /: 'adapter' is a required property\n"
            + "  at /: 'rows' is a required property", error.getMessage());
    }

    @Test
    void aBodyThatIsNotMessagePackIsAnInvalidRequest() {
        for (byte[] body : List.of(new byte[] {(byte) 0xc1}, new byte[] {(byte) 0x92, 0x01}, new byte[] {0x01, 0x02},
            new byte[] {(byte) 0x81, 0x01, 0x02}, new byte[] {(byte) 0xdd, 0x7f, (byte) 0xff, (byte) 0xff, (byte) 0xff},
            new byte[0])) {
            JeffError.InvalidRequest error = assertThrows(JeffError.InvalidRequest.class, () -> Protocol.decodeBody(body));
            assertTrue(error.getMessage().startsWith("Socket frame is not valid msgpack: "), error.getMessage());
        }
    }

    @Test
    void aMessageMsgPackCannotPackIsAnInvalidRequest() {
        // A message the schema accepts but the packer refuses: a map of several entries that keeps no order.
        Map<String, Object> unordered = new java.util.HashMap<>(Map.of("op", "health", "id", 1L));
        assertTrue(assertThrows(JeffError.InvalidRequest.class, () -> Protocol.encodeFrame(unordered, null)).getMessage()
            .startsWith("The outgoing socket message cannot be packed with msgpack: IllegalArgumentException: Cannot pack a java.util.HashMap"));
    }

    @Test
    void aMessageIsCheckedAgainstTheDefinitionItsTagNames() {
        assertEquals("simple", Protocol.socketDefinition(INSTALLED));
        assertEquals("auth", Protocol.socketDefinition(Fixtures.ordered("op", "refresh")));
        assertEquals("hello", Protocol.socketDefinition(Protocol.hello()));
        assertEquals("engine_hello", Protocol.socketDefinition(Fixtures.ordered("op", "hello", "adapters_version", 1L)));
        assertEquals("ok_reply", Protocol.socketDefinition(Fixtures.ordered("id", 1L, "ok", true)));
        assertEquals("failure_reply", Protocol.socketDefinition(Fixtures.ordered("id", 1L, "ok", false)));
        assertNull(Protocol.socketDefinition(Fixtures.ordered("op", "nonsense")));
        assertNull(Protocol.socketDefinition(Fixtures.ordered("op", 1L)));
        assertNull(Protocol.socketDefinition(Fixtures.ordered("ok", 1L)));
        assertNull(Protocol.socketDefinition(List.of()));
        assertTrue(assertThrows(JeffError.InvalidRequest.class,
            () -> Protocol.validateMessage(Fixtures.ordered("op", "nonsense"), "incoming socket message", null)).getMessage()
            .startsWith("incoming socket message does not match the socket schema:"));
    }

    @Test
    void theHelloIsTheProtocolVersionAndMatchesTheSchema() {
        assertArrayEquals(Fixtures.fromBase64("AAAAE4Kib3ClaGVsbG+ndmVyc2lvbgI="), Protocol.encodeFrame(Protocol.hello(), null));
    }

    @Test
    void aHelloFromAnotherProtocolIsRefusedNamingBothVersions() {
        assertEquals("This side speaks protocol 2, the other side 1; upgrade the older one",
            assertThrows(JeffError.ProtocolMismatch.class,
                () -> Protocol.checkHello(Fixtures.ordered("op", "hello", "version", 1L))).getMessage());
        assertEquals("Expected a hello, got 'predict'", assertThrows(JeffError.ProtocolMismatch.class,
            () -> Protocol.checkHello(Fixtures.ordered("op", "predict"))).getMessage());
        Protocol.checkHello(Fixtures.ordered("op", "hello", "version", 2L, "adapters_version", 1L));
    }

    @Test
    void aHelloIsComparedAndWrittenAsPythonDoes() {
        // Python: check_hello({"op": "hello", "version": v}) for v in None, "2", 2.0, True; and a message without op
        assertEquals("This side speaks protocol 2, the other side None; upgrade the older one",
            assertThrows(JeffError.ProtocolMismatch.class, () -> Protocol.checkHello(Fixtures.ordered("op", "hello", "version", null))).getMessage());
        assertEquals("This side speaks protocol 2, the other side 2; upgrade the older one",
            assertThrows(JeffError.ProtocolMismatch.class, () -> Protocol.checkHello(Fixtures.ordered("op", "hello", "version", "2"))).getMessage());
        Protocol.checkHello(Fixtures.ordered("op", "hello", "version", 2.0));
        assertEquals("This side speaks protocol 2, the other side True; upgrade the older one",
            assertThrows(JeffError.ProtocolMismatch.class, () -> Protocol.checkHello(Fixtures.ordered("op", "hello", "version", true))).getMessage());
        assertEquals("Expected a hello, got None",
            assertThrows(JeffError.ProtocolMismatch.class, () -> Protocol.checkHello(Fixtures.ordered("version", 2L))).getMessage());
    }
}
