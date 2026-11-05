package ai.jeff.sdk;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.SequencedMap;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;

/** The contract shipped with the package: JSON Schemas, the interface catalogue and the error catalogue.
 *
 * <p>Documents are checked by a JSON Schema 2020-12 checker written here for the keywords the contract's schemas use,
 * which works the way Python's jsonschema (4.26, Draft202012Validator, no format checker) does: the same keywords in
 * the same order, the same error tree (a failed oneOf keeps each alternative's errors apart as its context), and the
 * same message for every keyword, so validate() and firstProblem() give the Python SDK's text word for word. A schema
 * using any other keyword is refused when it is loaded, never skipped. */
final class Contract {
    public static final List<String> SCHEMAS = List.of("manifest", "decision", "graph", "graph_result", "socket",
        "decide_request", "decide_response", "functions_response", "graphs_response", "systemone_request",
        "systemone_response", "models_response", "served_request", "adapters_response", "health_response",
        "login_request", "login_response", "accounts_keys", "accounts_usage");

    public record InterfaceSpec(String name, String questionType, List<String> values, String description) {}

    public record ErrorSpec(String name, Long status, String meaning) {}

    /** How many schema checks may sit inside one another. Python's jsonschema recurses once per check and stops with
     * a RecursionError at about this depth (about 170 nested graph conditions, three checks each); this SDK stops
     * with a ContractError naming the place instead of overflowing the stack. */
    static final int MAX_NESTED_CHECKS = 500;

    private static final Map<String, Map<String, Object>> DOCUMENTS = new ConcurrentHashMap<>();
    private static final Map<String, Pattern> PATTERNS = new ConcurrentHashMap<>();
    private static volatile SequencedMap<String, InterfaceSpec> interfaceSpecs;
    private static volatile SequencedMap<String, ErrorSpec> errorSpecs;

    private Contract() {}

    // ---- loading

    public static Map<String, Object> loadSchema(String name) {
        if (!SCHEMAS.contains(name)) {
            throw new JeffError.ContractError("There is no schema " + PyFormat.str(name) + "; the schemas are "
                + String.join(", ", SCHEMAS));
        }
        return Collections.unmodifiableMap(DOCUMENTS.computeIfAbsent(name, key -> {
            String where = Shipped.ROOT + "contract/schemas/" + key + ".schema.json";
            Map<String, Object> schema = Values.map(Json.parse(Shipped.text("contract/schemas/" + key + ".schema.json")));
            checkKeywords(schema, where, "");
            return schema;
        }));
    }

    private static String idOf(String name) {
        return (String) loadSchema(name).get("$id");
    }

    private static Map<String, Object> schemaWithId(String schemaId) {
        for (String name : SCHEMAS) if (idOf(name).equals(schemaId)) return loadSchema(name);
        throw new JeffError.ContractError("No shipped schema has $id " + PyFormat.str(schemaId));
    }

    /** Keywords that check nothing (a format is an annotation, as in Python with no format checker). */
    private static final Set<String> ANNOTATIONS = Set.of("$schema", "$id", "$defs", "$comment", "title", "description",
        "default", "examples", "format");
    /** Keywords applied only through their schema's "if", never on their own. */
    private static final Set<String> APPLIED_BY_IF = Set.of("then", "else");
    private static final Set<String> CHECKED = Set.of("$ref", "type", "enum", "const", "properties", "required",
        "additionalProperties", "propertyNames", "minProperties", "maxProperties", "dependentRequired", "items",
        "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "minimum", "maximum",
        "exclusiveMinimum", "oneOf", "allOf", "not", "if");
    private static final Set<String> TYPES = Set.of("object", "array", "string", "number", "integer", "boolean", "null");

    /** Refuses a schema that uses a keyword this checker does not implement, a type it does not know or a pattern it
     * cannot read as Python's re reads it (jsonschema would skip the keyword; skipping it here would accept documents
     * the contract means to refuse). */
    private static void checkKeywords(Object schema, String where, String pointer) {
        if (schema instanceof Boolean) return;
        if (!(schema instanceof Map<?, ?> node)) {
            throw new JeffError.ContractError(where + "#" + pointer + " is not a schema (an object or a boolean)");
        }
        for (Map.Entry<?, ?> entry : node.entrySet()) {
            String key = (String) entry.getKey();
            Object value = entry.getValue();
            String at = pointer + "/" + key;
            if (!ANNOTATIONS.contains(key) && !APPLIED_BY_IF.contains(key) && !CHECKED.contains(key)) {
                throw new JeffError.ContractError(where + "#" + pointer + " uses the keyword " + PyFormat.str(key)
                    + ", which this SDK's schema checker does not implement");
            }
            switch (key) {
                case "$defs", "properties" -> {
                    for (Map.Entry<String, Object> sub : Values.map(value).entrySet()) {
                        checkKeywords(sub.getValue(), where, at + "/" + sub.getKey());
                    }
                }
                case "additionalProperties", "propertyNames", "items", "not", "if", "then", "else" ->
                    checkKeywords(value, where, at);
                case "oneOf", "allOf" -> {
                    List<Object> alternatives = Values.list(value);
                    for (int index = 0; index < alternatives.size(); index++) {
                        checkKeywords(alternatives.get(index), where, at + "/" + index);
                    }
                }
                case "type" -> {
                    for (Object type : value instanceof List<?> list ? list : List.of(value)) {
                        if (!TYPES.contains(type)) {
                            throw new JeffError.ContractError(where + "#" + at + " names the unknown type " + PyFormat.repr(type));
                        }
                    }
                }
                case "pattern" -> pattern((String) value, where + "#" + at);
                case "$id" -> {
                    if (!pointer.isEmpty()) {
                        throw new JeffError.ContractError(where + "#" + pointer + " has its own $id; this SDK's schema "
                            + "checker resolves references against each file's top-level $id only");
                    }
                }
                default -> { }
            }
        }
    }

    /** A pattern compiled to match as Python's re.search matches it: `$` and `.` know only \n as a line end
     * (UNIX_LINES) and \d is any Unicode decimal digit (UNICODE_CHARACTER_CLASS). Syntax the two engines read
     * differently even so is refused: letter escapes other than \d and \\uXXXX (\w, \s and \S outside the pair
     * [\s\S], \b, \Z...), a `[` or `&&` inside a character class (Java's class union and intersection, plain
     * characters to Python) and named groups (`(?<name>` in Java, `(?P<name>` in Python). */
    private static void pattern(String source, String where) {
        PATTERNS.computeIfAbsent(source, text -> {
            String checked = text.replace("[\\s\\S]", "");
            boolean inClass = false;
            for (int index = 0; index < checked.length(); index++) {
                char character = checked.charAt(index);
                if (character == '\\') {
                    if (index + 1 == checked.length()) refusePattern(text, where, "ends with a backslash");
                    char escaped = checked.charAt(++index);
                    if (escaped == 'u' || escaped == 'd' || !Character.isLetterOrDigit(escaped)) continue;
                    refusePattern(text, where, "uses \\" + escaped);
                } else if (inClass) {
                    if (character == '[') refusePattern(text, where, "has a [ inside a character class");
                    if (character == '&' && index + 1 < checked.length() && checked.charAt(index + 1) == '&') {
                        refusePattern(text, where, "has && inside a character class");
                    }
                    if (character == ']') inClass = false;
                } else if (character == '[') {
                    inClass = true;
                    if (index + 1 < checked.length() && checked.charAt(index + 1) == '^') index++;
                    if (index + 1 < checked.length() && checked.charAt(index + 1) == ']') index++;
                } else if (character == '(' && checked.startsWith("(?<", index) && index + 3 < checked.length()
                    && checked.charAt(index + 3) != '=' && checked.charAt(index + 3) != '!') {
                    refusePattern(text, where, "has a named group");
                } else if (character == '(' && checked.startsWith("(?P", index)) {
                    refusePattern(text, where, "has a Python named group");
                }
            }
            return Pattern.compile(text, Pattern.UNIX_LINES | Pattern.UNICODE_CHARACTER_CLASS);
        });
    }

    private static void refusePattern(String text, String where, String what) {
        throw new JeffError.ContractError(where + ": the pattern " + PyFormat.str(text) + " " + what
            + ", which Java's regular expressions do not read exactly as Python's re does");
    }

    /** The pattern compiled when its schema was loaded. */
    private static Pattern compiledPattern(String source) {
        Pattern found = PATTERNS.get(source);
        if (found == null) throw new IllegalStateException("The pattern " + PyFormat.str(source) + " was not checked when its schema was loaded");
        return found;
    }

    /** For tests: the load-time check of a schema (keywords, types, nested $id, patterns). */
    static void checkSchema(Map<String, Object> schema, String where) {
        checkKeywords(schema, where, "");
    }

    /** For tests: the messages of every error of a document against a schema that has no $ref, in jsonschema's order. */
    static List<String> messages(Map<String, Object> schema, Object document) {
        checkKeywords(schema, "a test schema", "");
        List<String> out = new ArrayList<>();
        for (Problem problem : new Checker("test").descend(document, schema, "", List.of())) out.add(problem.message());
        return out;
    }

    private static Map<String, Object> definitionNode(String name, String definition) {
        Map<String, Object> schema = loadSchema(name);
        if (definition == null) return schema;
        if (!(schema.get("$defs") instanceof Map<?, ?> definitions) || !definitions.containsKey(definition)) {
            throw new JeffError.ContractError("The " + name + " schema has no definition " + PyFormat.str(definition));
        }
        return Values.map(definitions.get(definition));
    }

    // ---- the checker

    /** One error as jsonschema reports it: the keyword that failed (null for a false schema), its message, the
     * absolute path to the value (an Integer for a list index, a String for a key), that value, the keyword's value
     * in the schema, and for a failed oneOf the errors of each alternative (context), each tagged with the index of
     * its alternative (its relative_schema_path[0]). */
    private record Problem(String keyword, String message, List<Object> path, Object instance, Object keywordValue,
                           List<Problem> context, int alternative) {
        Problem withAlternative(int index) {
            return new Problem(keyword, message, path, instance, keywordValue, context, index);
        }
    }

    private static List<Object> child(List<Object> path, Object segment) {
        List<Object> out = new ArrayList<>(path.size() + 1);
        out.addAll(path);
        out.add(segment);
        return out;
    }

    private static String joined(List<Object> path) {
        List<String> parts = new ArrayList<>();
        for (Object segment : path) parts.add(segment.toString());
        return String.join("/", parts);
    }

    /** The errors of one document against one schema, collected the way jsonschema's iter_errors yields them. */
    private static final class Checker {
        private final String schemaName;
        private int depth;

        Checker(String schemaName) {
            this.schemaName = schemaName;
        }

        List<Problem> descend(Object instance, Object schema, String base, List<Object> path) {
            if (Boolean.TRUE.equals(schema)) return List.of();
            List<Problem> out = new ArrayList<>();
            if (Boolean.FALSE.equals(schema)) {
                out.add(new Problem(null, "False schema does not allow " + PyFormat.repr(instance), path, instance, null,
                    List.of(), -1));
                return out;
            }
            if (++depth > MAX_NESTED_CHECKS) {
                throw new JeffError.ContractError("The document is nested too deeply to check against the " + schemaName
                    + " schema: more than " + MAX_NESTED_CHECKS + " schema checks inside one another (at /"
                    + joined(path) + ")");
            }
            try {
                Map<String, Object> node = Values.map(schema);
                for (Map.Entry<String, Object> entry : node.entrySet()) {
                    keyword(entry.getKey(), entry.getValue(), instance, node, base, path, out);
                }
                return out;
            } finally {
                depth--;
            }
        }

        boolean valid(Object instance, Object schema, String base, List<Object> path) {
            return descend(instance, schema, base, path).isEmpty();
        }

        private void keyword(String key, Object value, Object instance, Map<String, Object> schema, String base,
                             List<Object> path, List<Problem> out) {
            switch (key) {
                case "$ref" -> {
                    String reference = (String) value;
                    int hash = reference.indexOf('#');
                    String id = hash < 0 ? reference : reference.substring(0, hash);
                    String target = id.isEmpty() ? base : id;
                    out.addAll(descend(instance, nodeAt(target, hash < 0 ? "" : reference.substring(hash + 1)), target, path));
                }
                case "type" -> {
                    List<?> types = value instanceof List<?> list ? list : List.of(value);
                    for (Object type : types) if (isType(instance, (String) type)) return;
                    List<String> reprs = new ArrayList<>();
                    for (Object type : types) reprs.add(PyFormat.repr(type));
                    fail(out, key, PyFormat.repr(instance) + " is not of type " + String.join(", ", reprs), path, instance, value);
                }
                case "enum" -> {
                    for (Object each : Values.list(value)) if (equal(each, instance)) return;
                    fail(out, key, PyFormat.repr(instance) + " is not one of " + PyFormat.repr(value), path, instance, value);
                }
                case "const" -> {
                    if (!equal(instance, value)) fail(out, key, PyFormat.repr(value) + " was expected", path, instance, value);
                }
                case "properties" -> {
                    if (!(instance instanceof Map<?, ?> map)) return;
                    for (Map.Entry<String, Object> property : Values.map(value).entrySet()) {
                        if (map.containsKey(property.getKey())) {
                            out.addAll(descend(map.get(property.getKey()), property.getValue(), base,
                                child(path, property.getKey())));
                        }
                    }
                }
                case "required" -> {
                    if (!(instance instanceof Map<?, ?> map)) return;
                    for (Object property : Values.list(value)) {
                        if (!map.containsKey(property)) {
                            fail(out, key, PyFormat.repr(property) + " is a required property", path, instance, value);
                        }
                    }
                }
                case "additionalProperties" -> {
                    if (!(instance instanceof Map<?, ?> map)) return;
                    Map<?, ?> properties = schema.get("properties") instanceof Map<?, ?> known ? known : Map.of();
                    List<String> extras = new ArrayList<>();
                    for (Object property : map.keySet()) if (!properties.containsKey(property)) extras.add((String) property);
                    if (value instanceof Map<?, ?>) {
                        // jsonschema walks a set of the extras here, in an order that changes from one Python process
                        // to the next; this walks them in the document's order.
                        for (String extra : extras) out.addAll(descend(map.get(extra), value, base, child(path, extra)));
                    } else if (Boolean.FALSE.equals(value) && !extras.isEmpty()) {
                        List<String> sorted = PyFormat.sorted(extras);
                        List<String> reprs = new ArrayList<>();
                        for (String extra : sorted) reprs.add(PyFormat.str(extra));
                        fail(out, key, "Additional properties are not allowed (" + String.join(", ", reprs)
                            + (sorted.size() == 1 ? " was" : " were") + " unexpected)", path, instance, value);
                    }
                }
                case "propertyNames" -> {
                    if (!(instance instanceof Map<?, ?> map)) return;
                    for (Object property : map.keySet()) out.addAll(descend(property, value, base, path));
                }
                case "minProperties" -> {
                    if (instance instanceof Map<?, ?> map) size(out, key, false, map.size(), "does not have enough properties", path, instance, value);
                }
                case "maxProperties" -> {
                    if (instance instanceof Map<?, ?> map) size(out, key, true, map.size(), "has too many properties", path, instance, value);
                }
                case "dependentRequired" -> {
                    if (!(instance instanceof Map<?, ?> map)) return;
                    for (Map.Entry<String, Object> dependency : Values.map(value).entrySet()) {
                        if (!map.containsKey(dependency.getKey())) continue;
                        for (Object each : Values.list(dependency.getValue())) {
                            if (!map.containsKey(each)) {
                                fail(out, key, PyFormat.repr(each) + " is a dependency of " + PyFormat.str(dependency.getKey()),
                                    path, instance, value);
                            }
                        }
                    }
                }
                case "items" -> {
                    if (!(instance instanceof List<?> list) || list.isEmpty()) return;
                    if (Boolean.FALSE.equals(value)) {
                        Object rest = list.size() == 1 ? list.get(0) : list;
                        fail(out, key, "Expected at most 0 items but found " + list.size() + " extra: " + PyFormat.repr(rest),
                            path, instance, value);
                        return;
                    }
                    for (int index = 0; index < list.size(); index++) {
                        out.addAll(descend(list.get(index), value, base, child(path, index)));
                    }
                }
                case "minItems" -> {
                    if (instance instanceof List<?> list) size(out, key, false, list.size(), "is too short", path, instance, value);
                }
                case "maxItems" -> {
                    if (instance instanceof List<?> list) size(out, key, true, list.size(), "is too long", path, instance, value);
                }
                case "uniqueItems" -> {
                    if (Boolean.TRUE.equals(value) && instance instanceof List<?> list && !unique(list)) {
                        fail(out, key, PyFormat.repr(instance) + " has non-unique elements", path, instance, value);
                    }
                }
                case "minLength" -> {
                    if (instance instanceof String text) {
                        size(out, key, false, text.codePointCount(0, text.length()), "is too short", path, instance, value);
                    }
                }
                case "maxLength" -> {
                    if (instance instanceof String text) {
                        size(out, key, true, text.codePointCount(0, text.length()), "is too long", path, instance, value);
                    }
                }
                case "pattern" -> {
                    if (instance instanceof String text && !compiledPattern((String) value).matcher(text).find()) {
                        fail(out, key, PyFormat.repr(instance) + " does not match " + PyFormat.repr(value), path, instance, value);
                    }
                }
                case "minimum" -> {
                    if (isType(instance, "number") && less(instance, value)) {
                        fail(out, key, PyFormat.repr(instance) + " is less than the minimum of " + PyFormat.repr(value),
                            path, instance, value);
                    }
                }
                case "maximum" -> {
                    if (isType(instance, "number") && greater(instance, value)) {
                        fail(out, key, PyFormat.repr(instance) + " is greater than the maximum of " + PyFormat.repr(value),
                            path, instance, value);
                    }
                }
                case "exclusiveMinimum" -> {
                    if (isType(instance, "number") && !greater(instance, value) && compare(instance, value) != null) {
                        fail(out, key, PyFormat.repr(instance) + " is less than or equal to the minimum of "
                            + PyFormat.repr(value), path, instance, value);
                    }
                }
                case "oneOf" -> oneOf(Values.list(value), instance, base, path, out);
                case "allOf" -> {
                    for (Object alternative : Values.list(value)) out.addAll(descend(instance, alternative, base, path));
                }
                case "not" -> {
                    if (valid(instance, value, base, path)) {
                        fail(out, key, PyFormat.repr(instance) + " should not be valid under " + PyFormat.repr(value),
                            path, instance, value);
                    }
                }
                case "if" -> {
                    if (valid(instance, value, base, path)) {
                        if (schema.containsKey("then")) out.addAll(descend(instance, schema.get("then"), base, path));
                    } else if (schema.containsKey("else")) {
                        out.addAll(descend(instance, schema.get("else"), base, path));
                    }
                }
                default -> {
                    // checkKeywords refused every other keyword when the schema was loaded
                    if (!ANNOTATIONS.contains(key) && !APPLIED_BY_IF.contains(key)) {
                        throw new IllegalStateException("The " + schemaName + " schema's keyword " + PyFormat.str(key)
                            + " was not checked when the schema was loaded");
                    }
                }
            }
        }

        /** jsonschema's oneOf: the alternatives in order until one matches (each failed one's errors kept, tagged
         * with its index); none matching is one error carrying them all; then any later alternative that also matches
         * is a second error naming every matching alternative, the later ones first. */
        private void oneOf(List<Object> alternatives, Object instance, String base, List<Object> path, List<Problem> out) {
            List<Problem> failures = new ArrayList<>();
            int firstValid = -1;
            for (int index = 0; index < alternatives.size(); index++) {
                List<Problem> problems = descend(instance, alternatives.get(index), base, path);
                if (problems.isEmpty()) {
                    firstValid = index;
                    break;
                }
                for (Problem problem : problems) failures.add(problem.withAlternative(index));
            }
            if (firstValid < 0) {
                out.add(new Problem("oneOf", PyFormat.repr(instance) + " is not valid under any of the given schemas",
                    path, instance, alternatives, failures, -1));
                return;
            }
            List<Object> moreValid = new ArrayList<>();
            for (int index = firstValid + 1; index < alternatives.size(); index++) {
                if (valid(instance, alternatives.get(index), base, path)) moreValid.add(alternatives.get(index));
            }
            if (moreValid.isEmpty()) return;
            moreValid.add(alternatives.get(firstValid));
            List<String> reprs = new ArrayList<>();
            for (Object alternative : moreValid) reprs.add(PyFormat.repr(alternative));
            out.add(new Problem("oneOf", PyFormat.repr(instance) + " is valid under each of " + String.join(", ", reprs),
                path, instance, alternatives, List.of(), -1));
        }

        /** jsonschema's min/max length, items and properties: below a minimum of 1 "should be non-empty", above a
         * maximum of 0 "is expected to be empty", otherwise the keyword's own words. */
        private static void size(List<Problem> out, String keyword, boolean maximum, int size, String words,
                                 List<Object> path, Object instance, Object limit) {
            if (maximum ? !greater(size, limit) : !less(size, limit)) return;
            String tail = maximum ? (same(0L, limit) ? "is expected to be empty" : words)
                : (same(1L, limit) ? "should be non-empty" : words);
            fail(out, keyword, PyFormat.repr(instance) + " " + tail, path, instance, limit);
        }

        private static void fail(List<Problem> out, String keyword, String message, List<Object> path, Object instance,
                                 Object keywordValue) {
            out.add(new Problem(keyword, message, path, instance, keywordValue, List.of(), -1));
        }
    }

    /** The schema node a $ref names: a JSON Pointer into the shipped schema with that $id. */
    private static Object nodeAt(String schemaId, String pointer) {
        Object node = schemaWithId(schemaId);
        if (pointer.isEmpty()) return node;
        if (!pointer.startsWith("/")) {
            throw new JeffError.ContractError("The reference " + schemaId + "#" + pointer + " is not a JSON Pointer");
        }
        for (String raw : pointer.substring(1).split("/", -1)) {
            String segment = raw.replace("~1", "/").replace("~0", "~");
            Object next = node instanceof List<?> list ? list.get(Integer.parseInt(segment))
                : node instanceof Map<?, ?> map ? map.get(segment) : null;
            if (next == null) throw new JeffError.ContractError("The reference " + schemaId + "#" + pointer + " names nothing");
            node = next;
        }
        return node;
    }

    // ---- Python's types, equality and comparison, as jsonschema applies them

    private static boolean wholeNumber(Object value) {
        return value instanceof Long || value instanceof Integer || value instanceof Short || value instanceof Byte
            || value instanceof BigInteger;
    }

    /** jsonschema's 2020-12 type checker: a bool is not a number, and a float with no fractional part is an integer. */
    private static boolean isType(Object instance, String type) {
        return switch (type) {
            case "object" -> instance instanceof Map<?, ?>;
            case "array" -> instance instanceof List<?>;
            case "string" -> instance instanceof String;
            case "boolean" -> instance instanceof Boolean;
            case "null" -> instance == null;
            case "number" -> wholeNumber(instance) || instance instanceof Double;
            case "integer" -> wholeNumber(instance)
                || instance instanceof Double number && Double.isFinite(number) && number == Math.rint(number);
            default -> throw new JeffError.ContractError("The schema type " + PyFormat.str(type) + " is unknown");
        };
    }

    /** Python's exact comparison of two numbers (an int and a float are compared without rounding), or null when
     * either is NaN, which Python never orders. */
    private static Integer compare(Object a, Object b) {
        if (a instanceof Double x && x.isNaN() || b instanceof Double y && y.isNaN()) return null;
        if (a instanceof Double x && x.isInfinite()) {
            return b instanceof Double y && y.isInfinite() ? Double.compare(x, y) : x > 0 ? 1 : -1;
        }
        if (b instanceof Double y && y.isInfinite()) return y > 0 ? -1 : 1;
        return decimal(a).compareTo(decimal(b));
    }

    private static boolean less(Object a, Object b) {
        Integer order = compare(a, b);
        return order != null && order < 0;
    }

    private static boolean greater(Object a, Object b) {
        Integer order = compare(a, b);
        return order != null && order > 0;
    }

    private static boolean same(Object a, Object b) {
        Integer order = compare(a, b);
        return order != null && order == 0;
    }

    private static BigDecimal decimal(Object value) {
        if (value instanceof Double number) return new BigDecimal(number);
        if (value instanceof BigInteger big) return new BigDecimal(big);
        return BigDecimal.valueOf(((Number) value).longValue());
    }

    private static boolean isNaN(Object value) {
        return value instanceof Double number && number.isNaN();
    }

    /** jsonschema's equal(): Python's == except that True and False are never equal to 1 and 0, at any depth. It
     * starts with `one is two`, and Python's json module reads every NaN as one shared float object, so a NaN equals a
     * NaN here (as in every document Python reads from JSON; msgpack-python makes a new float for each NaN). */
    private static boolean equal(Object a, Object b) {
        if (isNaN(a) && isNaN(b)) return true;
        if (a instanceof String || b instanceof String) return a instanceof String && a.equals(b);
        if (a instanceof List<?> x && b instanceof List<?> y) {
            if (x.size() != y.size()) return false;
            for (int index = 0; index < x.size(); index++) if (!equal(x.get(index), y.get(index))) return false;
            return true;
        }
        if (a instanceof Map<?, ?> x && b instanceof Map<?, ?> y) {
            if (x.size() != y.size()) return false;
            for (Map.Entry<?, ?> entry : x.entrySet()) {
                if (!y.containsKey(entry.getKey()) || !equal(entry.getValue(), y.get(entry.getKey()))) return false;
            }
            return true;
        }
        if (a instanceof Boolean || b instanceof Boolean) return a instanceof Boolean && a.equals(b);
        if (a == null || b == null) return a == b;
        return isType(a, "number") && isType(b, "number") && same(a, b);
    }

    /** Python's TypeError from {@code <} between values it cannot order (jsonschema's uniq() then compares every pair). */
    private static final class Unorderable extends RuntimeException {
        Unorderable() {
            super(null, null, false, false);
        }
    }

    /** What jsonschema's unbool() makes of True and False at the top of the list: an object that orders with nothing. */
    private record Unbooled(boolean value) {}

    /** jsonschema's uniq(): sort the items (True and False replaced by unorderable stand-ins) with CPython's own sort
     * and compare neighbours with equal(); when Python cannot order them, compare every pair instead. The sort
     * matters: a NaN, which orders with nothing, can keep equal items apart ([1, NaN, 1] is unique to Python). */
    private static boolean unique(List<?> items) {
        List<Object> keys = new ArrayList<>(items.size());
        for (Object item : items) keys.add(item instanceof Boolean flag ? new Unbooled(flag) : item);
        try {
            PySort.sort(keys);
        } catch (Unorderable error) {
            for (int index = 0; index < items.size(); index++) {
                for (int other = 0; other < index; other++) if (equal(items.get(other), items.get(index))) return false;
            }
            return true;
        }
        for (int index = 1; index < keys.size(); index++) if (equal(keys.get(index - 1), keys.get(index))) return false;
        return true;
    }

    /** list.sort() as CPython 3.13 does it, with Python's `<`, for the lists uniqueItems sees. Below 64 items that is
     * count_run and binary insertion sort, ported from Objects/listobject.c, which is exact even when `<` is not a
     * total order (NaN). From 64 items a stable sort gives CPython's order whenever `<` is a total order; with a NaN
     * it may not, so that case is refused by name. */
    private static final class PySort {
        private static void sort(List<Object> keys) {
            int n = keys.size();
            if (n < 2) return;
            if (n >= 64) {
                for (Object key : keys) {
                    if (holdsNaN(key)) {
                        throw new JeffError.ContractError("uniqueItems over " + n + " items holding a NaN: this SDK does "
                            + "not reproduce the order Python's sort leaves them in");
                    }
                }
                keys.sort((a, b) -> less(a, b) ? -1 : less(b, a) ? 1 : 0);
                return;
            }
            int run = countRun(keys, n);
            binarySort(keys, n, run);
        }

        private static boolean holdsNaN(Object value) {
            if (isNaN(value)) return true;
            if (value instanceof List<?> list) for (Object item : list) if (holdsNaN(item)) return true;
            return false;
        }

        private static void reverse(List<Object> keys, int from, int to) {
            Collections.reverse(keys.subList(from, to));
        }

        private static int countRun(List<Object> lo, int remaining) {
            int n;
            for (n = 1; n < remaining; n++) if (less(lo.get(n), lo.get(n - 1))) break;
            if (n == remaining) return n;
            if (n > 1) {
                if (less(lo.get(0), lo.get(n - 1))) return n;
                reverse(lo, 0, n);
            }
            n++;
            int equalRun = 0;
            for (; n < remaining; n++) {
                if (less(lo.get(n), lo.get(n - 1))) {
                    if (equalRun > 0) {
                        reverse(lo, n - equalRun - 1, n);
                        equalRun = 0;
                    }
                } else if (less(lo.get(n - 1), lo.get(n))) {
                    break;
                } else {
                    equalRun++;
                }
            }
            if (equalRun > 0) reverse(lo, n - equalRun - 1, n);
            reverse(lo, 0, n);
            for (; n < remaining; n++) if (less(lo.get(n), lo.get(n - 1))) break;
            return n;
        }

        private static void binarySort(List<Object> a, int n, int ok) {
            for (; ok < n; ok++) {
                int left = 0;
                int right = ok;
                Object pivot = a.get(ok);
                do {
                    int middle = (left + right) >> 1;
                    if (less(pivot, a.get(middle))) right = middle;
                    else left = middle + 1;
                } while (left < right);
                a.remove(ok);
                a.add(left, pivot);
            }
        }

        /** Python's `<` between two JSON-like values, or Unorderable where Python raises TypeError. */
        private static boolean less(Object a, Object b) {
            if (a instanceof Unbooled || b instanceof Unbooled) throw new Unorderable();
            if (pyNumber(a) && pyNumber(b)) return Contract.less(number(a), number(b));
            if (a instanceof String x && b instanceof String y) return PyFormat.compareCodePoints(x, y) < 0;
            if (a instanceof List<?> x && b instanceof List<?> y) {
                for (int index = 0; index < Math.min(x.size(), y.size()); index++) {
                    if (!pyEquals(x.get(index), y.get(index))) return less(x.get(index), y.get(index));
                }
                return x.size() < y.size();
            }
            throw new Unorderable();
        }

        /** Inside a list a bool is Python's int 0 or 1. */
        private static boolean pyNumber(Object value) {
            return value instanceof Boolean || isType(value, "number");
        }

        private static Object number(Object value) {
            return value instanceof Boolean flag ? (flag ? 1L : 0L) : value;
        }

        /** Python's == as list comparison uses it: identity first (one shared NaN, see equal()), then ==. */
        private static boolean pyEquals(Object a, Object b) {
            if (isNaN(a) && isNaN(b)) return true;
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
            return PyFormat.pyEquals(a, b);
        }
    }

    // ---- validate, isValid, firstProblem

    /** Every error of the document; a deep document is checked on a large-stack thread (see Nesting), so the
     * MAX_NESTED_CHECKS error is reached on any caller's thread. */
    private static List<Problem> problems(String name, Object document, String definition) {
        Map<String, Object> node = definitionNode(name, definition);
        String base = idOf(name);
        return Nesting.run(Nesting.shallow(document), () -> new Checker(name).descend(document, node, base, List.of()));
    }

    /** Two error paths compared element by element, a strict prefix first, each segment by `segments`. */
    private static int comparePaths(List<Object> a, List<Object> b, Comparator<Object> segments) {
        for (int index = 0; index < Math.min(a.size(), b.size()); index++) {
            int order = segments.compare(a.get(index), b.get(index));
            if (order != 0) return order;
        }
        return Integer.compare(a.size(), b.size());
    }

    /** Python's sort key list(error.absolute_path): an int index by value, a key by code point. */
    private static int compareTyped(List<Object> a, List<Object> b) {
        return comparePaths(a, b, (x, y) -> {
            if (x instanceof Integer i && y instanceof Integer j) return Integer.compare(i, j);
            if (x instanceof String i && y instanceof String j) return PyFormat.compareCodePoints(i, j);
            throw new IllegalStateException("Two error paths hold an index and a key at the same place: " + a + ", " + b);
        });
    }

    /** Python's key list(map(str, error.absolute_path)): every segment as text, so index 10 sorts before index 2. */
    private static int compareAsText(List<Object> a, List<Object> b) {
        return comparePaths(a, b, (x, y) -> PyFormat.compareCodePoints(x.toString(), y.toString()));
    }

    public static void validate(String name, Object document, String where) {
        validate(name, document, where, null);
    }

    /** ContractError listing every problem when `document` does not match schema `name` (or, with `definition`, the
     * one of its $defs the document says it is), as jsonschema words each, sorted by place; a failed oneOf is
     * followed by why each alternative failed. */
    public static void validate(String name, Object document, String where, String definition) {
        List<Problem> problems = new ArrayList<>(problems(name, document, definition));
        if (problems.isEmpty()) return;
        problems.sort((a, b) -> compareTyped(a.path(), b.path()));
        List<String> lines = new ArrayList<>();
        for (Problem problem : problems) {
            lines.add("  at /" + joined(problem.path()) + ": " + problem.message());
            for (Problem sub : problem.context()) lines.add("    - " + sub.message());
        }
        String shape = definition == null ? "the " + name + " schema" : definition + " in the " + name + " schema";
        throw new JeffError.ContractError(where + " does not match " + shape + ":\n" + String.join("\n", lines));
    }

    public static boolean isValid(String name, Object document) {
        return isValid(name, document, null);
    }

    public static boolean isValid(String name, Object document, String definition) {
        return problems(name, document, definition).isEmpty();
    }

    public static String firstProblem(String name, Object document) {
        return firstProblem(name, document, null);
    }

    private static Problem closest(List<Problem> problems) {
        Problem chosen = problems.get(0);
        for (Problem problem : problems.subList(1, problems.size())) {
            if (compareAsText(problem.path(), chosen.path()) < 0) chosen = problem;
        }
        return chosen;
    }

    /** One short line naming the first thing wrong with a document that does not match schema `name` (Python's
     * first_problem): the error closest to the root; where that is a failed oneOf, the alternative the document came
     * closest to matching (its errors lie deepest, then it has the fewest, then it is declared first) and that
     * alternative's error closest to the root, repeatedly. A value with too many items or properties is reported by
     * its count; a longer message is cut at 200 characters. */
    public static String firstProblem(String name, Object document, String definition) {
        List<Problem> problems = problems(name, document, definition);
        if (problems.isEmpty()) {
            throw new JeffError.ContractError("first_problem was asked about a document that matches the " + name + " schema");
        }
        Problem problem = closest(problems);
        while ("oneOf".equals(problem.keyword()) && !problem.context().isEmpty()) {
            Map<Integer, List<Problem>> byAlternative = new LinkedHashMap<>();
            for (Problem sub : problem.context()) {
                byAlternative.computeIfAbsent(sub.alternative(), key -> new ArrayList<>()).add(sub);
            }
            List<Problem> best = null;
            int bestDepth = -1;
            for (List<Problem> group : byAlternative.values()) {
                int depth = Integer.MAX_VALUE;
                for (Problem sub : group) depth = Math.min(depth, sub.path().size());
                if (best == null || depth > bestDepth || depth == bestDepth && group.size() < best.size()) {
                    best = group;
                    bestDepth = depth;
                }
            }
            problem = closest(best);
        }
        String message;
        if (("maxItems".equals(problem.keyword()) || "maxProperties".equals(problem.keyword()))
            && (problem.instance() instanceof List<?> || problem.instance() instanceof Map<?, ?>)) {
            int count = problem.instance() instanceof List<?> list ? list.size() : ((Map<?, ?>) problem.instance()).size();
            String kind = "maxItems".equals(problem.keyword()) ? "items" : "properties";
            message = count + " " + kind + ", at most " + PyFormat.repr(problem.keywordValue()) + " are allowed";
        } else {
            String text = problem.message();
            message = text.codePointCount(0, text.length()) <= 200 ? text
                : text.substring(0, text.offsetByCodePoints(0, 200)) + "...";
        }
        String at = joined(problem.path());
        return at.isEmpty() ? message : message + " (at /" + at + ")";
    }

    // ---- the interface and error catalogues

    private static boolean questionType(Object value) {
        return "choice".equals(value) || "noul".equals(value) || "score".equals(value);
    }

    private static String requiredString(Map<String, Object> spec, String key, String where) {
        if (spec.get(key) instanceof String text) return text;
        throw new JeffError.ContractError(where + " is missing its '" + key + "' string (got " + PyFormat.repr(spec.get(key)) + ")");
    }

    public static SequencedMap<String, InterfaceSpec> interfaces() {
        SequencedMap<String, InterfaceSpec> known = interfaceSpecs;
        if (known != null) return known;
        String where = Shipped.ROOT + "contract/interfaces.yaml";
        Map<String, InterfaceSpec> found = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : Values.map(Yaml.parse(Shipped.text("contract/interfaces.yaml"), where)).entrySet()) {
            Map<String, Object> spec = Values.map(entry.getValue());
            Object type = spec.get("question_type");
            if (!questionType(type)) {
                throw new JeffError.ContractError(where + ": interface " + entry.getKey() + " has an unknown question_type "
                    + PyFormat.repr(type));
            }
            List<String> values = spec.containsKey("values") ? Values.strings(spec.get("values")) : null;
            found.put(entry.getKey(), new InterfaceSpec(entry.getKey(), (String) type, values,
                requiredString(spec, "description", where + ": interface " + entry.getKey())));
        }
        interfaceSpecs = Values.frozenMap(found);
        return interfaceSpecs;
    }

    public static SequencedMap<String, ErrorSpec> errorCatalogue() {
        SequencedMap<String, ErrorSpec> known = errorSpecs;
        if (known != null) return known;
        String where = Shipped.ROOT + "contract/errors.yaml";
        Map<String, ErrorSpec> found = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : Values.map(Yaml.parse(Shipped.text("contract/errors.yaml"), where)).entrySet()) {
            Map<String, Object> spec = Values.map(entry.getValue());
            if (!spec.containsKey("status")) {
                throw new JeffError.ContractError(where + ": " + entry.getKey() + " has no status; use an HTTP error status or null");
            }
            Object status = spec.get("status");
            if (status != null && !(status instanceof Long code && code >= 400 && code <= 599)) {
                throw new JeffError.ContractError(where + ": " + entry.getKey() + " has status " + PyFormat.repr(status)
                    + "; use an HTTP error status or null");
            }
            found.put(entry.getKey(), new ErrorSpec(entry.getKey(), (Long) status,
                requiredString(spec, "meaning", where + ": " + entry.getKey())));
        }
        errorSpecs = Values.frozenMap(found);
        return errorSpecs;
    }
}
