package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.math.BigInteger;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class MsgPackTest {
    private static byte[] bytes(int... values) {
        byte[] out = new byte[values.length];
        for (int index = 0; index < values.length; index++) out[index] = (byte) values[index];
        return out;
    }

    @Test
    void aLinkedHashMapKeepsItsInsertionOrderNumberLikeKeysIncluded() {
        Map<String, Object> map = new LinkedHashMap<>();
        map.put("b", 1L);
        map.put("10", 2L);
        assertArrayEquals(bytes(0x82, 0xa1, 0x62, 0x01, 0xa2, 0x31, 0x30, 0x02), MsgPack.encode(map));
    }

    @Test
    void wholeNumbersTakeTheSmallestEncodingAsMsgpackPythonWritesThem() {
        assertArrayEquals(bytes(0x7f), MsgPack.encode(127L));
        assertArrayEquals(bytes(0xcc, 0x80), MsgPack.encode(128L));
        assertArrayEquals(bytes(0xcc, 0xff), MsgPack.encode(255L));
        assertArrayEquals(bytes(0xcd, 0x01, 0x00), MsgPack.encode(256L));
        assertArrayEquals(bytes(0xce, 0x00, 0x01, 0x00, 0x00), MsgPack.encode(65536L));
        assertArrayEquals(bytes(0xcf, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00), MsgPack.encode(4294967296L));
        assertArrayEquals(bytes(0xff), MsgPack.encode(-1L));
        assertArrayEquals(bytes(0xe0), MsgPack.encode(-32L));
        assertArrayEquals(bytes(0xd0, 0xdf), MsgPack.encode(-33L));
        assertArrayEquals(bytes(0xd1, 0xff, 0x7f), MsgPack.encode(-129L));
        assertArrayEquals(bytes(0xd2, 0xff, 0xff, 0x7f, 0xff), MsgPack.encode(-32769L));
        assertArrayEquals(bytes(0xcf, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff),
            MsgPack.encode(new BigInteger("18446744073709551615")));
    }

    @Test
    void integerAndLongPackTheSame() {
        assertArrayEquals(MsgPack.encode(300L), MsgPack.encode(300));
        assertArrayEquals(MsgPack.encode(List.of(1L, -40000L)), MsgPack.encode(List.of(1, -40000)));
    }

    @Test
    void floatsAreFloat64AndStringsUseStr8FromThirtyTwoBytes() {
        assertArrayEquals(bytes(0xcb, 0x3f, 0xf0, 0, 0, 0, 0, 0, 0), MsgPack.encode(1.0));
        assertEquals(0xbf, MsgPack.encode("x".repeat(31))[0] & 0xff);
        assertArrayEquals(bytes(0xd9, 0x20), Arrays.copyOf(MsgPack.encode("x".repeat(32)), 2));
        assertArrayEquals(bytes(0xda, 0x01, 0x00), Arrays.copyOf(MsgPack.encode("x".repeat(256)), 3));
        assertArrayEquals(bytes(0xdb, 0x00, 0x01, 0x00, 0x00), Arrays.copyOf(MsgPack.encode("x".repeat(65536)), 5));
        assertArrayEquals(bytes(0xa2, 0xc3, 0xa9), MsgPack.encode("é"));
        assertArrayEquals(bytes(0xc0, 0xc3, 0xc2), Arrays.copyOfRange(MsgPack.encode(Arrays.asList(null, true, false)), 1, 4));
    }

    @Test
    void mapAndArraySizesPastTheFixFormatsUseTheirSixteenBitHeaders() {
        Map<String, Object> big = new LinkedHashMap<>();
        for (int index = 0; index < 20; index++) big.put("k" + index, index * 1.5);
        assertArrayEquals(bytes(0xde, 0x00, 0x14), Arrays.copyOf(MsgPack.encode(big), 3));
        List<Object> list = new ArrayList<>();
        for (int index = 0; index < 17; index++) list.add((long) index);
        assertArrayEquals(bytes(0xdc, 0x00, 0x11), Arrays.copyOf(MsgPack.encode(list), 3));
    }

    @Test
    void aMapWithoutOrderIsRefused() {
        Map<String, Object> unordered = new HashMap<>();
        unordered.put("a", 1L);
        unordered.put("b", 2L);
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class, () -> MsgPack.encode(unordered));
        assertEquals("Cannot pack a java.util.HashMap of 2 entries: it keeps no key order, and the engine reads keys in "
            + "order; use a LinkedHashMap", error.getMessage());
        assertArrayEquals(bytes(0x81, 0xa1, 0x61, 0x01), MsgPack.encode(Map.of("a", 1L)));
    }

    @Test
    void aFloatIsRefused() {
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class, () -> MsgPack.encode(List.of(0.5f)));
        assertTrue(error.getMessage().startsWith("Cannot pack a java.lang.Float: "), error.getMessage());
    }

    @Test
    void aLoneSurrogateIsRefused() {
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class, () -> MsgPack.encode("a\ud800b"));
        assertTrue(error.getMessage().startsWith("Cannot pack a string holding a lone surrogate"), error.getMessage());
    }

    @Test
    void decodingGivesLongsDoublesLinkedHashMapsAndArrayLists() {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("z", Arrays.asList(1L, -2L, 2.5, "s", null, true, new BigInteger("18446744073709551615")));
        value.put("a", new LinkedHashMap<>(Map.of("n", 300L)));
        Object decoded = MsgPack.decode(MsgPack.encode(value));
        assertEquals(value, decoded);
        assertEquals(List.of("z", "a"), List.copyOf(((Map<?, ?>) decoded).keySet()));
        assertEquals(0.5, MsgPack.decode(bytes(0xca, 0x3f, 0x00, 0x00, 0x00)));
    }

    @Test
    void decodingRefusesTrailingBytesTruncationAndKeysThatAreNotStrings() {
        assertEquals("1 extra bytes after the packed value",
            assertThrows(IllegalArgumentException.class, () -> MsgPack.decode(bytes(0x01, 0x02))).getMessage());
        assertEquals("the packed value ends early, at byte 2",
            assertThrows(IllegalArgumentException.class, () -> MsgPack.decode(bytes(0xcd, 0x01))).getMessage());
        assertEquals("a map key that is not a string: 1",
            assertThrows(IllegalArgumentException.class, () -> MsgPack.decode(bytes(0x81, 0x01, 0x02))).getMessage());
    }

    @Test
    void everyClientFrameMsgpackPythonPackedInSocketJsonRepacksToTheSameBytes() throws Exception {
        Map<?, ?> file = (Map<?, ?>) Json.parse(Files.readString(
            Shipped.repositoryRoot().resolve("contract/conformance/socket.json")));
        int checked = 0;
        for (Object item : (List<?>) file.get("cases")) {
            for (Object step : (List<?>) ((Map<?, ?>) item).get("steps")) {
                Object client = ((Map<?, ?>) step).get("client");
                if (client == null) continue;
                byte[] frame = Base64.getDecoder().decode((String) client);
                byte[] body = Arrays.copyOfRange(frame, 4, frame.length);
                assertArrayEquals(body, MsgPack.encode(MsgPack.decode(body)));
                checked += 1;
            }
        }
        assertTrue(checked > 20, "only " + checked + " client frames found");
    }

    /** msgpack-python refuses a whole number outside [-2^63, 2^64 - 1] with OverflowError("Integer value out of
     * range"), and Python's request path (jeff/protocol.py encode) shows that as "OverflowError: Integer value out of
     * range" inside the InvalidRequest a caller sees; a row holding one must read the same here. */
    @Test
    void aWholeNumberPastSixtyFourBitsIsRefusedInMsgpackPythonsWords() {
        for (BigInteger value : List.of(BigInteger.TWO.pow(64), BigInteger.TWO.pow(63).negate().subtract(BigInteger.ONE),
            BigInteger.TWO.pow(100))) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("state", new LinkedHashMap<>(Map.of("message", value)));
            IllegalArgumentException error = assertThrows(IllegalArgumentException.class, () -> MsgPack.encode(List.of(row)));
            assertEquals("OverflowError: Integer value out of range", JeffError.describe(error));
        }
        assertArrayEquals(bytes(0xd3, 0x80, 0, 0, 0, 0, 0, 0, 0), MsgPack.encode(BigInteger.TWO.pow(63).negate()));
    }
}
