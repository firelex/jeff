package ai.jeff.sdk;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** Python's own text forms and arithmetic, so that every message, reason and number this SDK produces is the one the
 * Python SDK (the reference) produces. contract/conformance pins them. */
final class PyFormat {
    private PyFormat() {}

    /** repr() of a str: single quotes unless the text holds a single quote and no double quote. */
    public static String str(String text) {
        char quote = text.indexOf('\'') >= 0 && text.indexOf('"') < 0 ? '"' : '\'';
        StringBuilder out = new StringBuilder().append(quote);
        text.codePoints().forEach(code -> {
            if (code == quote || code == '\\') out.append('\\').appendCodePoint(code);
            else if (code == '\t') out.append("\\t");
            else if (code == '\n') out.append("\\n");
            else if (code == '\r') out.append("\\r");
            else if (code != ' ' && !printable(code)) {
                if (code <= 0xff) out.append(String.format("\\x%02x", code));
                else if (code <= 0xffff) out.append(String.format("\\u%04x", code));
                else out.append(String.format("\\U%08x", code));
            } else out.appendCodePoint(code);
        });
        return out.append(quote).toString();
    }

    /** Python's str.isprintable() for one character: not in the categories Cc, Cf, Cs, Co, Cn, Zl, Zp or Zs. */
    private static boolean printable(int code) {
        return switch (Character.getType(code)) {
            case Character.CONTROL, Character.FORMAT, Character.SURROGATE, Character.PRIVATE_USE,
                 Character.UNASSIGNED, Character.LINE_SEPARATOR, Character.PARAGRAPH_SEPARATOR,
                 Character.SPACE_SEPARATOR -> false;
            default -> true;
        };
    }

    /** repr() of a float: the shortest digits that read back as the same float (the one nearest the exact value when
     * several are that short), laid out as Python lays them out: scientific below 1e-4 and from 1e16, with an
     * exponent of at least two digits. Double.toString is not used: it keeps two digits where one suffices (it writes
     * Double.MIN_VALUE as 4.9E-324, Python as 5e-324). */
    public static String floatRepr(double value) {
        if (Double.isNaN(value)) return "nan";
        if (Double.isInfinite(value)) return value > 0 ? "inf" : "-inf";
        if (value == 0) return 1 / value < 0 ? "-0.0" : "0.0";
        BigDecimal shortest = shortestDigits(Math.abs(value)).stripTrailingZeros();
        String digits = shortest.unscaledValue().toString();
        int exponent = digits.length() - 1 - shortest.scale();
        String body;
        if (exponent < -4 || exponent >= 16) {
            String shown = digits.length() > 1 ? digits.charAt(0) + "." + digits.substring(1) : digits;
            body = shown + "e" + (exponent < 0 ? "-" : "+") + String.format("%02d", Math.abs(exponent));
        } else if (exponent < 0) {
            body = "0." + "0".repeat(-exponent - 1) + digits;
        } else if (digits.length() <= exponent + 1) {
            body = digits + "0".repeat(exponent + 1 - digits.length()) + ".0";
        } else {
            body = digits.substring(0, exponent + 1) + "." + digits.substring(exponent + 1);
        }
        return value < 0 ? "-" + body : body;
    }

    /** The decimal with the fewest significant digits that parses back to `value` (positive and finite); among those
     * that short, the nearest to the exact binary value, an exact tie going to the even last digit. Seventeen digits
     * always suffice. */
    private static BigDecimal shortestDigits(double value) {
        BigDecimal exact = new BigDecimal(value);
        for (int precision = 1; precision <= 17; precision++) {
            BigDecimal down = exact.round(new MathContext(precision, RoundingMode.FLOOR));
            BigDecimal up = exact.round(new MathContext(precision, RoundingMode.CEILING));
            boolean downReadsBack = Double.parseDouble(down.toString()) == value;
            boolean upReadsBack = Double.parseDouble(up.toString()) == value;
            if (downReadsBack && upReadsBack) return exact.round(new MathContext(precision, RoundingMode.HALF_EVEN));
            if (downReadsBack) return down;
            if (upReadsBack) return up;
        }
        throw new IllegalStateException("no decimal of 17 digits or fewer reads back as the float " + value);
    }

    /** format(value, f".{digits}f"): the exact binary value rounded to `digits` decimals, an exact half to even (as
     * Python does; String.format rounds it up), and a negative value that rounds to zero keeps its sign (-0.000).
     * NaN and infinity return their repr() ('nan', 'inf', '-inf'), as Python does. */
    public static String fixed(double value, int digits) {
        if (!Double.isFinite(value)) {
            return floatRepr(value);
        }
        if (digits < 0) throw new IllegalArgumentException("PyFormat.fixed was given " + digits + " digits; it needs 0 or more");
        String body = new BigDecimal(value).abs().setScale(digits, RoundingMode.HALF_EVEN).toPlainString();
        return Math.copySign(1.0, value) < 0 ? "-" + body : body;
    }

    private static boolean wholeNumber(Object value) {
        return value instanceof Long || value instanceof Integer || value instanceof Short || value instanceof Byte
            || value instanceof BigInteger;
    }

    /** repr() of a JSON-like value. A Long, Integer or BigInteger is a Python int; a Double is a Python float; a
     * byte[] is Python bytes. A value nested more than Nesting.PYTHON_DEPTH lists and maps deep is refused by name,
     * where Python's repr() raises RecursionError; a deep value is written on a large-stack thread (see Nesting). */
    public static String repr(Object value) {
        return Nesting.run(Nesting.shallow(value), () -> {
            StringBuilder out = new StringBuilder();
            reprInto(value, out, 0);
            return out.toString();
        });
    }

    private static void reprInto(Object value, StringBuilder out, int depth) {
        if (value instanceof List<?> || value instanceof Map<?, ?>) {
            if (depth + 1 > Nesting.PYTHON_DEPTH) {
                throw new IllegalArgumentException("PyFormat.repr: the value nests more than " + Nesting.PYTHON_DEPTH
                    + " lists and maps inside one another (Python's repr() raises RecursionError there)");
            }
        }
        if (value == null) out.append("None");
        else if (value instanceof Boolean flag) out.append(flag ? "True" : "False");
        else if (value instanceof Double number) out.append(floatRepr(number));
        else if (wholeNumber(value)) out.append(value);
        else if (value instanceof String text) out.append(str(text));
        else if (value instanceof byte[] data) out.append(bytes(data));
        else if (value instanceof List<?> list) {
            out.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) out.append(", ");
                first = false;
                reprInto(item, out, depth + 1);
            }
            out.append(']');
        } else if (value instanceof Map<?, ?> map) {
            out.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!first) out.append(", ");
                first = false;
                reprInto(entry.getKey(), out, depth + 1);
                out.append(": ");
                reprInto(entry.getValue(), out, depth + 1);
            }
            out.append('}');
        } else {
            throw new IllegalArgumentException("PyFormat.repr has no Python form for a " + value.getClass().getName()
                + "; it writes JSON-like values only");
        }
    }

    /** repr() of bytes: b'...' (double quotes when the bytes hold a single quote and no double quote), printable ASCII
     * as itself, \t \n \r and \\ escaped, anything else as \xhh. */
    private static String bytes(byte[] data) {
        boolean single = false;
        boolean dbl = false;
        for (byte item : data) {
            if (item == '\'') single = true;
            if (item == '"') dbl = true;
        }
        char quote = single && !dbl ? '"' : '\'';
        StringBuilder out = new StringBuilder("b").append(quote);
        for (byte item : data) {
            int code = item & 0xff;
            if (code == quote || code == '\\') out.append('\\').append((char) code);
            else if (code == '\t') out.append("\\t");
            else if (code == '\n') out.append("\\n");
            else if (code == '\r') out.append("\\r");
            else if (code < 0x20 || code >= 0x7f) out.append(String.format("\\x%02x", code));
            else out.append((char) code);
        }
        return out.append(quote).toString();
    }

    /** str() of a JSON-like value: a string as itself, anything else as its repr(). */
    public static String toStr(Object value) {
        return value instanceof String text ? text : repr(value);
    }

    /** sum() of a list of Python floats, as CPython 3.12 and later computes it (Neumaier's compensated summation). Only
     * a list of floats: CPython does not compensate an int that follows a float, so a mixed list can differ. */
    public static double sum(double... values) {
        double total = 0;
        double compensation = 0;
        for (double value : values) {
            double next = total + value;
            if (Math.abs(total) >= Math.abs(value)) compensation += (total - next) + value;
            else compensation += (value - next) + total;
            total = next;
        }
        if (compensation != 0 && Double.isFinite(compensation)) total += compensation;
        return total;
    }

    /** Python's ordering of str: by code point (String.compareTo compares UTF-16 code units). */
    public static int compareCodePoints(String a, String b) {
        int[] x = a.codePoints().toArray();
        int[] y = b.codePoints().toArray();
        for (int index = 0; index < Math.min(x.length, y.length); index++) {
            if (x[index] != y[index]) return Integer.compare(x[index], y[index]);
        }
        return Integer.compare(x.length, y.length);
    }

    public static List<String> sorted(Collection<String> values) {
        List<String> out = new ArrayList<>(values);
        out.sort(PyFormat::compareCodePoints);
        return out;
    }

    /** type(value).__name__ of the Python value this Java value stands for; raises if the type is unsupported. */
    public static String typeName(Object value) {
        if (value == null) return "NoneType";
        if (value instanceof Boolean) return "bool";
        if (wholeNumber(value)) return "int";
        if (value instanceof Double || value instanceof Float) return "float";
        if (value instanceof String) return "str";
        if (value instanceof byte[]) return "bytes";
        if (value instanceof List<?>) return "list";
        if (value instanceof Map<?, ?>) return "dict";
        throw new IllegalArgumentException("PyFormat.typeName has no Python form for a " + value.getClass().getName()
            + "; it converts JSON-like types only");
    }

    private static boolean numeric(Object value) {
        return value instanceof Boolean || value instanceof Double || wholeNumber(value);
    }

    private static BigDecimal decimal(Object value) {
        if (value instanceof Boolean flag) return flag ? BigDecimal.ONE : BigDecimal.ZERO;
        if (value instanceof Double number) return new BigDecimal(number);
        if (value instanceof BigInteger big) return new BigDecimal(big);
        return BigDecimal.valueOf(((Number) value).longValue());
    }

    /** Python's == between JSON-like values: True == 1, 1 == 1.0 (compared exactly), lists in order, dicts by
     * content whatever their order. */
    public static boolean pyEquals(Object a, Object b) {
        if (numeric(a) && numeric(b)) {
            boolean special = a instanceof Double x && !Double.isFinite(x) || b instanceof Double y && !Double.isFinite(y);
            if (special) return a instanceof Double x && b instanceof Double y && !x.isNaN() && x.equals(y);
            return decimal(a).compareTo(decimal(b)) == 0;
        }
        if (a instanceof List<?> x && b instanceof List<?> y) {
            if (x.size() != y.size()) return false;
            for (int index = 0; index < x.size(); index++) if (!pyEquals(x.get(index), y.get(index))) return false;
            return true;
        }
        if (a instanceof Map<?, ?> x && b instanceof Map<?, ?> y) {
            if (x.size() != y.size()) return false;
            for (Map.Entry<?, ?> entry : x.entrySet()) {
                if (!y.containsKey(entry.getKey()) || !pyEquals(entry.getValue(), y.get(entry.getKey()))) return false;
            }
            return true;
        }
        return Objects.equals(a, b);
    }
}
