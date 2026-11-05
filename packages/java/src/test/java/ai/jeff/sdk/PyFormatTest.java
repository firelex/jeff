package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class PyFormatTest {
    @Test
    void reprOfAStrPicksPythonsQuotesAndEscapes() {
        assertEquals("'plain'", PyFormat.str("plain"));
        assertEquals("\"it's\"", PyFormat.str("it's"));
        assertEquals("'it\\'s \"x\"'", PyFormat.str("it's \"x\""));
        assertEquals("'a\\nb\\tc\\\\'", PyFormat.str("a\nb\tc\\"));
        assertEquals("'\\x07\\xa0\\u2028'", PyFormat.str("\u0007\u00a0\u2028"));
        assertEquals("'café 💶'", PyFormat.str("café 💶"));
        assertEquals("'\\ud800'", PyFormat.str("\ud800"));
    }

    @Test
    void reprOfAFloatIsPythonsShortestFormNotJavas() {
        assertEquals("1.0", PyFormat.floatRepr(1));
        assertEquals("0.1", PyFormat.floatRepr(0.1));
        assertEquals("0.771902", PyFormat.floatRepr(0.771902));
        assertEquals("1e-05", PyFormat.floatRepr(1e-5));
        assertEquals("0.0001", PyFormat.floatRepr(0.0001));
        assertEquals("1e+16", PyFormat.floatRepr(1e16));
        assertEquals("1234567890123456.0", PyFormat.floatRepr(1234567890123456.0));
        assertEquals("123456789.5", PyFormat.floatRepr(123456789.5));
        assertEquals("100.0", PyFormat.floatRepr(100));
        assertEquals("1e+22", PyFormat.floatRepr(1e22));
        assertEquals("-2.5e-07", PyFormat.floatRepr(-2.5e-7));
        assertEquals("5e-324", PyFormat.floatRepr(Double.MIN_VALUE));
        assertEquals("0.30000000000000004", PyFormat.floatRepr(0.1 + 0.2));
        assertEquals("0.0", PyFormat.floatRepr(0));
        assertEquals("-0.0", PyFormat.floatRepr(-0.0));
        assertEquals("nan", PyFormat.floatRepr(Double.NaN));
        assertEquals("inf", PyFormat.floatRepr(Double.POSITIVE_INFINITY));
        assertEquals("-inf", PyFormat.floatRepr(Double.NEGATIVE_INFINITY));
        // Checked against `python3 -c 'print(repr(x))'` (CPython 3.13); Java's Double.toString differs on these.
        assertEquals("1000000000000000.0", PyFormat.floatRepr(1e15));
        assertEquals("1e+21", PyFormat.floatRepr(1e21));
        assertEquals("1e+23", PyFormat.floatRepr(1e23));
        assertEquals("2e-07", PyFormat.floatRepr(2e-7));
        assertEquals("1.7976931348623157e+308", PyFormat.floatRepr(Double.MAX_VALUE));
        assertEquals("1.2345678901234568e+17", PyFormat.floatRepr(123456789012345678.0));
        assertEquals("9007199254740992.0", PyFormat.floatRepr(9007199254740993.0));
    }

    @Test
    void fixedDigitsRoundExactHalvesToEvenAsPythonsFormatDoes() {
        assertEquals("0.312", PyFormat.fixed(0.3125, 3));
        assertEquals("0.438", PyFormat.fixed(0.4375, 3));
        assertEquals("0.062", PyFormat.fixed(0.0625, 3));
        assertEquals("0.689", PyFormat.fixed(0.689444, 3));
        assertEquals("0.100", PyFormat.fixed(0.1, 3));
        assertEquals("1.25", PyFormat.fixed(1.25, 2));
        assertEquals("-0.000", PyFormat.fixed(-0.0001, 3));
        assertEquals("-0.000", PyFormat.fixed(-0.0, 3));
        assertEquals("2", PyFormat.fixed(2.5, 0));
    }

    @Test
    void fixedDigitsReturnsNanAndInfinityAsFloatRepr() {
        assertEquals("nan", PyFormat.fixed(Double.NaN, 3));
        assertEquals("inf", PyFormat.fixed(Double.POSITIVE_INFINITY, 3));
        assertEquals("-inf", PyFormat.fixed(Double.NEGATIVE_INFINITY, 3));
    }

    @Test
    void fixedDigitsRefuseNegativeCount() {
        assertThrows(IllegalArgumentException.class, () -> PyFormat.fixed(1.0, -1));
    }

    @Test
    void reprOfContainersNumbersAndNone() {
        assertEquals("['a', 1, 2.5, True, None]", PyFormat.repr(Arrays.asList("a", 1L, 2.5, true, null)));
        Map<String, Object> format = new LinkedHashMap<>();
        format.put("format", "bf16");
        format.put("format_id", "x");
        assertEquals("{'format': 'bf16', 'format_id': 'x'}", PyFormat.repr(format));
        assertEquals("1.0", PyFormat.repr(1.0));
        assertEquals("1", PyFormat.repr(1L));
        assertEquals("1", PyFormat.repr(1));
        assertEquals("18446744073709551615", PyFormat.repr(new BigInteger("18446744073709551615")));
        assertEquals("[]", PyFormat.repr(new ArrayList<>()));
    }

    @Test
    void strOfAValueIsPythonsStr() {
        assertEquals("text", PyFormat.toStr("text"));
        assertEquals("5", PyFormat.toStr(5L));
        assertEquals("None", PyFormat.toStr(null));
        assertEquals("True", PyFormat.toStr(true));
        assertEquals("['a']", PyFormat.toStr(List.of("a")));
    }

    @Test
    void sumUsesPythonsCompensatedSummation() {
        double[] tenths = new double[10];
        Arrays.fill(tenths, 0.1);
        double naive = 0;
        for (double value : tenths) naive += value;
        assertEquals(0.9999999999999999, naive);
        assertEquals(1.0, PyFormat.sum(tenths));
        assertEquals(1.0, PyFormat.sum(1e100, 1.0, -1e100));
        assertEquals(0.0, PyFormat.sum());
    }

    @Test
    void sortedComparesCodePointsNotUtf16Units() {
        assertEquals(List.of("a", "b", "\uffff", "💶"), PyFormat.sorted(List.of("b", "a", "💶", "\uffff")));
    }

    @Test
    void typeNamesFollowPython() {
        assertEquals(List.of("NoneType", "bool", "int", "int", "float", "str", "list", "dict"),
            Arrays.asList(null, true, 1L, 1, 1.5, "s", List.of(), Map.of()).stream().map(PyFormat::typeName).toList());
    }

    @Test
    void typeNameRefusesUnsupportedTypes() {
        assertThrows(IllegalArgumentException.class, () -> PyFormat.typeName(new Object()));
    }

    @Test
    void equalityFollowsPython() {
        assertTrue(PyFormat.pyEquals(true, 1L));
        assertTrue(PyFormat.pyEquals(false, 0L));
        assertTrue(PyFormat.pyEquals(1L, 1.0));
        assertTrue(PyFormat.pyEquals(1, 1L));
        assertFalse(PyFormat.pyEquals(1L, "1"));
        assertTrue(PyFormat.pyEquals("a", "a"));
        assertTrue(PyFormat.pyEquals(null, null));
        assertFalse(PyFormat.pyEquals(Double.NaN, Double.NaN));
        assertFalse(PyFormat.pyEquals(new BigInteger("9007199254740993"), 9007199254740992.0));
        assertTrue(PyFormat.pyEquals(List.of(1L, true), List.of(1.0, 1L)));
        assertTrue(PyFormat.pyEquals(Map.of("a", 1L), Map.of("a", true)));
    }

    @Test
    void bytesAreWrittenAsPythonWritesBytes() {
        assertEquals("b\"a'\\x00\\n\\\\\\x7f\\xff\"", PyFormat.repr(new byte[] {'a', '\'', 0, '\n', '\\', 0x7f, (byte) 0xff}));
        assertEquals("b'q\"\\''", PyFormat.repr(new byte[] {'q', '"', '\''}));
        assertEquals("[b'']", PyFormat.repr(List.of(new byte[0])));
        assertEquals("bytes", PyFormat.typeName(new byte[0]));
    }
}
