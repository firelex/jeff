package ai.jeff.sdk;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.SequencedMap;

/** JSON-like values: null, Boolean, Long (BigInteger past 64 bits), Double, String, List and Map with String keys. */
final class Values {
    private Values() {}

    /** A value a schema has already checked is a mapping. */
    @SuppressWarnings("unchecked")
    static Map<String, Object> map(Object value) {
        return (Map<String, Object>) value;
    }

    /** A value a schema has already checked is a list. */
    @SuppressWarnings("unchecked")
    static List<Object> list(Object value) {
        return (List<Object>) value;
    }

    /** A value a schema has already checked is a list of strings. */
    static List<String> strings(Object value) {
        List<String> out = new ArrayList<>();
        for (Object item : list(value)) out.add((String) item);
        return Collections.unmodifiableList(out);
    }

    /** An unmodifiable copy that keeps the order and allows null values (Map.copyOf does neither). */
    static <K, V> SequencedMap<K, V> frozenMap(Map<K, V> map) {
        return Collections.unmodifiableSequencedMap(new LinkedHashMap<>(map));
    }

    /** An unmodifiable copy that allows null elements (List.copyOf does not). */
    static <T> List<T> frozenList(List<T> list) {
        return Collections.unmodifiableList(new ArrayList<>(list));
    }

    /** A deep read-only copy of a value plain() has already made JSON-like: every mapping and list in it is an
     * unmodifiable copy (keeping order and nulls), so no holder of the copy can change what another sees. */
    static Object frozen(Object value) {
        return Nesting.run(Nesting.shallow(value), () -> frozenHere(value));
    }

    private static Object frozenHere(Object value) {
        if (value instanceof Map<?, ?> map) {
            LinkedHashMap<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) out.put((String) entry.getKey(), frozenHere(entry.getValue()));
            return Collections.unmodifiableSequencedMap(out);
        }
        if (value instanceof List<?> list) {
            List<Object> out = new ArrayList<>(list.size());
            for (Object item : list) out.add(frozenHere(item));
            return Collections.unmodifiableList(out);
        }
        return value;
    }

    /** A deep copy with every whole number a Long, every mapping a LinkedHashMap and every list an ArrayList. Throws
     * NotAString for a mapping key that is not a string, or NotJson for a value with no JSON-like shape; both carry
     * the offending value so a caller (Json, Yaml) can word its own message around it instead of this generic one. */
    static Object plain(Object value) {
        return Nesting.run(Nesting.shallow(value), () -> plain(value, 0));
    }

    private static Object plain(Object value, int depth) {
        if (value instanceof Integer || value instanceof Short || value instanceof Byte) return ((Number) value).longValue();
        if ((value instanceof Map<?, ?> || value instanceof List<?>) && depth + 1 > Nesting.PYTHON_DEPTH) {
            throw new IllegalArgumentException("The value nests more than " + Nesting.PYTHON_DEPTH
                + " lists and maps inside one another (as deep as Python's json and repr() go)");
        }
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new NotAString(entry.getKey());
                }
                out.put(key, plain(entry.getValue(), depth + 1));
            }
            return out;
        }
        if (value instanceof List<?> list) {
            List<Object> out = new ArrayList<>(list.size());
            for (Object item : list) out.add(plain(item, depth + 1));
            return out;
        }
        if (value == null || value instanceof Boolean || value instanceof Long || value instanceof BigInteger
            || value instanceof Double || value instanceof String || value instanceof byte[]) {
            return value;
        }
        throw new NotJson(value);
    }

    /** A mapping key plain() found that is not a string; carries the key itself, not just its text, so a caller
     * can build its own message (Yaml's names the document and asks the caller to quote it). */
    static final class NotAString extends IllegalArgumentException {
        final Object key;

        NotAString(Object key) {
            super("A mapping key " + PyFormat.repr(key) + " is not a string");
            this.key = key;
        }
    }

    /** A value plain() found with no JSON-like shape; carries the value itself so a caller can build its own
     * message naming the type. */
    static final class NotJson extends IllegalArgumentException {
        final Object value;

        NotJson(Object value) {
            super("A " + value.getClass().getName() + " is not a JSON value (null, Boolean, Long, Integer, Double, String, "
                + "List or Map)");
            this.value = value;
        }
    }
}
