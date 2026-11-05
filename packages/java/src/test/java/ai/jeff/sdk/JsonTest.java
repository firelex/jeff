package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.math.BigInteger;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class JsonTest {
    @Test
    void wholeNumbersAreLongsDecimalsDoublesAndObjectsKeepTheirOrder() {
        Map<?, ?> parsed = assertInstanceOf(Map.class, Json.parse("{\"b\": 1, \"10\": 1.0, \"a\": [2, 2.5, null]}"));
        assertEquals(List.of("b", "10", "a"), List.copyOf(parsed.keySet()));
        assertEquals(1L, parsed.get("b"));
        assertEquals(1.0, parsed.get("10"));
        assertEquals(Arrays.asList(2L, 2.5, null), parsed.get("a"));
        assertInstanceOf(LinkedHashMap.class, parsed);
    }

    @Test
    void pythonsNanAndInfinityAreReadAndWritten() {
        assertEquals(List.of(Double.NaN, Double.POSITIVE_INFINITY), Json.parse("[NaN, Infinity]"));
        assertEquals("[NaN]", Json.write(List.of(Double.NaN)));
    }

    /** Checked against CPython 3.13: json.loads("+Infinity") raises "Expecting value" (unlike "Infinity" and
     * "-Infinity", which it accepts). */
    @Test
    void aLeadingPlusBeforeInfinityIsRefusedEvenThoughInfinityAloneIsAccepted() {
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class, () -> Json.parse("+Infinity"));
        assertTrue(error.getMessage().startsWith("Not JSON: "), error.getMessage());
    }

    @Test
    void textThatIsNotJsonIsAnIllegalArgumentNamingTheProblem() {
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class, () -> Json.parse("{oops"));
        assertTrue(error.getMessage().startsWith("Not JSON: "), error.getMessage());
    }

    /** Checked against CPython 3.13 (`uv run python3 -c "import json; print(json.dumps(2**63));
     * print(json.dumps(-2**63-1)); print(json.dumps(2**64));
     * print(json.dumps(123456789012345678901234567890))"`): json.loads/json.dumps raise no error for an integer of
     * any size and reproduce it digit for digit; Values.java's value model promises the same split (Long within the
     * signed 64-bit range, BigInteger past it). */
    @Test
    void wholeNumbersPastSixtyFourBitsParseAsBigIntegerAndRoundTripThroughWrite() {
        assertLongRoundTrip("9223372036854775807"); // 2^63 - 1: still fits a long
        assertBigIntegerRoundTrip("9223372036854775808"); // 2^63
        assertBigIntegerRoundTrip("-9223372036854775809"); // -2^63 - 1
        assertBigIntegerRoundTrip("18446744073709551616"); // 2^64
        assertBigIntegerRoundTrip("123456789012345678901234567890"); // a 30-digit number
    }

    private static void assertLongRoundTrip(String text) {
        Object parsed = Json.parse(text);
        assertInstanceOf(Long.class, parsed, text);
        assertEquals(text, Json.write(parsed), text);
    }

    private static void assertBigIntegerRoundTrip(String text) {
        Object parsed = Json.parse(text);
        assertInstanceOf(BigInteger.class, parsed, text);
        assertEquals(text, Json.write(parsed), text);
    }

    /** Checked against CPython 3.13 (`uv run python3 -c "import json; print(json.dumps(1e7));
     * print(json.dumps(0.0001)); print(json.dumps(1e-5)); print(json.dumps(1234567890123.0));
     * print(json.dumps(5e-324))"`): Python's json.dumps formats a float with float.__repr__'s digits (the same ones
     * PyFormat.floatRepr produces), which disagrees with Jackson's default Double.toString-shaped output outside
     * roughly [1e-3, 1e7). */
    @Test
    void finiteDoublesOutsideJacksonsDefaultRangeWritePythonsDigits() {
        assertEquals("10000000.0", Json.write(1e7));
        assertEquals("0.0001", Json.write(0.0001));
        assertEquals("1e-05", Json.write(1e-5));
        assertEquals("1234567890123.0", Json.write(1234567890123.0));
        assertEquals("5e-324", Json.write(Double.MIN_VALUE));
    }

    @Test
    void plainCopiesWithLongsAndOrderedMaps() {
        Map<String, Object> given = new LinkedHashMap<>();
        given.put("n", 3);
        given.put("list", List.of(4, "x"));
        Object copy = Values.plain(given);
        assertEquals(Map.of("n", 3L, "list", List.of(4L, "x")), copy);
        assertInstanceOf(LinkedHashMap.class, copy);
    }
}
