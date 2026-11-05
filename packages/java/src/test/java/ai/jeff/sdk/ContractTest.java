package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

class ContractTest {
    private static Map<String, Object> map(Object... pairs) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (int index = 0; index < pairs.length; index += 2) out.put((String) pairs[index], pairs[index + 1]);
        return out;
    }

    @Test
    void everySchemaFileIsListedAndHasItsId() throws IOException {
        List<String> files = new ArrayList<>();
        try (Stream<Path> paths = Files.list(Shipped.repositoryRoot().resolve("contract/schemas"))) {
            paths.forEach(path -> files.add(path.getFileName().toString().replace(".schema.json", "")));
        }
        assertEquals(PyFormat.sorted(files), PyFormat.sorted(Contract.SCHEMAS));
        for (String name : Contract.SCHEMAS) {
            assertEquals("https://jeff.dev/schemas/" + name + ".schema.json", Contract.loadSchema(name).get("$id"));
        }
    }

    @Test
    void validateNamesWhereTheDocumentIsWrong() {
        JeffError.ContractError error = assertThrows(JeffError.ContractError.class,
            () -> Contract.validate("decision", map("value", 1L), "a decision"));
        assertTrue(error.getMessage().startsWith("a decision does not match the decision schema:\n  at /: "), error.getMessage());
    }

    @Test
    void oneDefinitionOfASchemaCanBeChecked() {
        assertTrue(Contract.isValid("socket", map("op", "predict", "id", 1L, "adapter", "a", "rows", List.of(map())), "predict"));
        assertFalse(Contract.isValid("socket", map("op", "predict", "id", 1L, "adapter", "a", "rows", List.of()), "predict"));
        assertTrue(Contract.isValid("decide_response", map("decisions", List.of())));
        assertEquals("'rows' is a required property",
            Contract.firstProblem("socket", map("op", "predict", "id", 1L, "adapter", "a"), "predict"));
        assertEquals("The socket schema has no definition 'nope'", assertThrows(JeffError.ContractError.class,
            () -> Contract.isValid("socket", map(), "nope")).getMessage());
    }

    @Test
    void aWholeNumberWrittenAsAFloatIsAnIntegerAsInPython() {
        assertTrue(Contract.isValid("socket", map("op", "installed", "id", 1.0), "simple"));
    }

    @Test
    void theInterfacesHaveTheirQuestionTypesAndValues() {
        Map<String, Contract.InterfaceSpec> found = Contract.interfaces();
        assertEquals("noul", found.get("detect").questionType());
        assertEquals(List.of("match", "possible_match", "no_match"), found.get("equivalent").values());
        assertNull(found.get("classify").values());
    }

    @Test
    void thereIsOneErrorClassPerEntryOfErrorsYaml() {
        assertEquals(PyFormat.sorted(Contract.errorCatalogue().keySet()), PyFormat.sorted(JeffError.typeNames()));
        assertEquals(404L, Contract.errorCatalogue().get("UnknownFunction").status());
        assertEquals(402L, Contract.errorCatalogue().get("UsageLimitReached").status());
    }

    static Stream<Arguments> socketMessages() {
        return Stream.of(
            Arguments.of(map("op", "predict", "id", 4L, "adapter", "a"), "rows"),
            Arguments.of(map("op", "stats", "id", 4L, "extra", 1L), "'extra' was unexpected"),
            Arguments.of(map("op", "nope", "id", 3L), "nope"));
    }

    @ParameterizedTest
    @MethodSource("socketMessages")
    void firstProblemNamesTheFieldActuallyWrongNotAnUnrelatedAlternative(Map<String, Object> message, String named) {
        String problem = Contract.firstProblem("socket", message);
        assertTrue(problem.contains(named), problem);
        assertFalse(problem.contains("\n"), problem);
        assertTrue(problem.length() < 300, problem);
    }

    @Test
    void firstProblemFollowsANestedOneOfToTheAlternativeThatMatchesClosest() {
        String problem = Contract.firstProblem("decide_request",
            map("calls", List.of(map("function", "f", "input", "x")), "on_low_confidence", "maybe"));
        assertTrue(problem.contains("on_low_confidence"), problem);
        assertTrue(problem.contains("maybe"), problem);
    }

    @Test
    void firstProblemPrefersTheAlternativeTheDocumentMatchedFurthestInto() {
        Map<String, Object> call = map("function", "f", "input", "x", "labels", 5L);
        String problem = Contract.firstProblem("decide_request", map("calls", List.of(call, call, call)));
        assertTrue(problem.contains("labels"), problem);
        assertFalse(problem.contains("dispatch"), problem);
    }

    @Test
    void firstProblemRefusesADocumentThatMatches() {
        assertEquals("first_problem was asked about a document that matches the decide_response schema",
            assertThrows(JeffError.ContractError.class,
                () -> Contract.firstProblem("decide_response", map("decisions", List.of()))).getMessage());
    }

    // ---- the same documents as the Python SDK, with what it says about each (src/test/resources/contract-cases.json,
    // made by running jeff.contract's validate, first_problem and is_valid on every document)

    static Stream<Arguments> pythonCases() {
        String text;
        try (InputStream in = ContractTest.class.getClassLoader().getResourceAsStream("contract-cases.json")) {
            if (in == null) throw new IllegalStateException("contract-cases.json is missing from the test resources");
            text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
        List<Arguments> out = new ArrayList<>();
        for (Object item : Values.list(Values.map(Json.parse(text)).get("cases"))) {
            Map<String, Object> entry = Values.map(item);
            out.add(Arguments.of(entry.get("label"), entry));
        }
        return out.stream();
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("pythonCases")
    void everyMessageIsWhatThePythonSdkSays(String label, Map<String, Object> entry) {
        String schema = (String) entry.get("schema");
        String definition = (String) entry.get("definition");
        Object document = entry.get("document");
        assertEquals(entry.get("valid"), Contract.isValid(schema, document, definition), label);
        String expected = (String) entry.get("validate");
        if (expected == null) {
            Contract.validate(schema, document, "the document", definition);
        } else {
            assertEquals(expected, assertThrows(JeffError.ContractError.class,
                () -> Contract.validate(schema, document, "the document", definition)).getMessage(), label);
        }
        if (entry.containsKey("first_problem")) {
            assertEquals(entry.get("first_problem"), Contract.firstProblem(schema, document, definition), label);
        } else {
            assertEquals(entry.get("first_problem_error"), assertThrows(JeffError.ContractError.class,
                () -> Contract.firstProblem(schema, document, definition)).getMessage(), label);
        }
    }

    private static Map<String, Object> graphWhen(Object condition) {
        return map("graph", "g", "version", "1.0.0", "input", "x", "params", List.of(),
            "nodes", map("a", map("call", "f", "when", condition)), "outputs", List.of("a"));
    }

    private static Object nestedNot(int levels, Object leaf) {
        Object condition = leaf;
        for (int level = 0; level < levels; level++) condition = map("not", condition);
        return condition;
    }

    @Test
    void deepNestingIsANamedErrorNotAStackOverflow() {
        // Python checks 150 nested conditions and stops with a RecursionError before 200; this SDK checks as deep as
        // Python does and refuses a much deeper document by name.
        assertTrue(Contract.isValid("graph", graphWhen(nestedNot(150, map("node", "a", "is", true)))));
        assertEquals("1.5 is not of type 'string', 'boolean', 'integer' (at /nodes/a/when" + "/not".repeat(150) + "/is)",
            Contract.firstProblem("graph", graphWhen(nestedNot(150, map("node", "a", "is", 1.5)))));
        Map<String, Object> deep = graphWhen(nestedNot(5000, map("node", "a", "is", true)));
        JeffError.ContractError error = assertThrows(JeffError.ContractError.class, () -> Contract.isValid("graph", deep));
        assertTrue(error.getMessage().startsWith("The document is nested too deeply to check against the graph schema: "
            + "more than 500 schema checks inside one another (at /nodes/a/when/not/not/"), error.getMessage());
        assertThrows(JeffError.ContractError.class, () -> Contract.validate("graph", deep, "a graph"));
        assertThrows(JeffError.ContractError.class, () -> Contract.firstProblem("graph", deep));
    }

    @Test
    void extraPropertiesUnderASchemaAreCheckedInTheDocumentsOrder() {
        // jsonschema walks these in an order that changes from one Python process to the next; this SDK keeps the
        // document's order, so the two lines below always come b first, then a.
        String message = assertThrows(JeffError.ContractError.class, () -> Contract.validate("decide_request",
            map("graph", map("id", "g"), "input", 1L, "params", map(),
                "on_low_confidence_by_node", map("b", "maybe", "a", "nope")), "a request")).getMessage();
        assertTrue(message.endsWith("    - 'maybe' is not valid under any of the given schemas\n"
            + "    - 'nope' is not valid under any of the given schemas"), message);
    }

    @Test
    void patternsReadAsPythonsReReadsThem() {
        Map<String, Object> graph = graphWhen(map("node", "a", "is", true));
        for (String version : List.of("1.2.3", "1.2.3\n", "\u0661.\u0662.\u0663")) {
            graph.put("version", version);
            assertTrue(Contract.isValid("graph", graph), PyFormat.repr(version));
        }
        for (String version : List.of("1.2.3\r", "1.2.3\u2028", "1.2.3\u0085", "1.2.3\n\n")) {
            graph.put("version", version);
            assertFalse(Contract.isValid("graph", graph), PyFormat.repr(version));
        }
    }

    // ---- deep input on a small thread stack: the answer or the named error, never a StackOverflowError

    static final long SMALL_STACK = 256 * 1024;

    /** The work's result on a new thread with a 256 KB stack, or what it threw there. */
    static Object onSmallStack(java.util.function.Supplier<Object> work) throws InterruptedException {
        Object[] outcome = new Object[1];
        Thread thread = new Thread(null, () -> {
            try {
                outcome[0] = work.get();
            } catch (Throwable thrown) {
                outcome[0] = thrown;
            }
        }, "small-stack", SMALL_STACK);
        thread.start();
        thread.join();
        if (outcome[0] instanceof StackOverflowError overflow) throw new AssertionError("overflowed the stack", overflow);
        return outcome[0];
    }

    static Object nestedLists(int levels) {
        Object value = new ArrayList<>();
        for (int level = 1; level < levels; level++) value = new ArrayList<>(List.of(value));
        return value;
    }

    @Test
    void deepDocumentsOnASmallStackGiveTheAnswerOrTheNamedError() throws InterruptedException {
        for (int levels : List.of(11, 12, 40, 100, 160)) {
            Object answer = onSmallStack(() -> Contract.firstProblem("graph", graphWhen(nestedNot(levels, map("node", "a", "is", 1.5)))));
            assertEquals("1.5 is not of type 'string', 'boolean', 'integer' (at /nodes/a/when" + "/not".repeat(levels) + "/is)", answer);
        }
        Object deep = onSmallStack(() -> Contract.isValid("graph", graphWhen(nestedNot(5000, map("node", "a", "is", true)))));
        assertTrue(deep instanceof JeffError.ContractError error && error.getMessage().startsWith(
            "The document is nested too deeply to check against the graph schema"), String.valueOf(deep));
        // a shallow schema with a deep value quoted in its message, as Python answers it (repr goes 9998 levels deep)
        Object quoted = onSmallStack(() -> Contract.firstProblem("decision", nestedLists(3000)));
        assertEquals("[".repeat(200) + "...", quoted);
        Object listed = onSmallStack(() -> assertThrows(JeffError.ContractError.class,
            () -> Contract.validate("decision", nestedLists(3000), "a decision")).getMessage());
        assertTrue(String.valueOf(listed).endsWith("]".repeat(3000) + " is not of type 'object'"));
        assertEquals(false, onSmallStack(() -> Contract.isValid("decision", nestedLists(3000))));
    }

    @Test
    void everyRecursiveWalkIsSafeOnASmallStack() throws InterruptedException {
        Object deep = nestedLists(Nesting.PYTHON_DEPTH);
        Object tooDeep = nestedLists(Nesting.PYTHON_DEPTH + 1);
        String text = "[".repeat(Nesting.PYTHON_DEPTH) + "]".repeat(Nesting.PYTHON_DEPTH);
        assertEquals(text.replace("][", "], ["), onSmallStack(() -> PyFormat.repr(deep)));
        // compared as text: ArrayList.equals would recurse 9998 levels on this test's own thread
        assertEquals(text, Json.write(onSmallStack(() -> Values.plain(deep))));
        assertEquals(text, onSmallStack(() -> Json.write(deep)));
        assertEquals(text, Json.write(onSmallStack(() -> Json.parse(text))));
        assertNamed(onSmallStack(() -> PyFormat.repr(tooDeep)), "PyFormat.repr: the value nests more than 9998");
        assertNamed(onSmallStack(() -> Values.plain(tooDeep)), "The value nests more than 9998");
        assertNamed(onSmallStack(() -> Json.write(tooDeep)), "Cannot write as JSON:");
        assertNamed(onSmallStack(() -> Json.parse("[" + text + "]")), "Not JSON:");
        Object packable = nestedLists(MsgPack.PACK_DEPTH);
        byte[] packed = (byte[]) onSmallStack(() -> MsgPack.encode(packable));
        assertNamed(onSmallStack(() -> MsgPack.encode(nestedLists(MsgPack.PACK_DEPTH + 1))), "Cannot pack a value that nests more than 1025");
        assertNamed(onSmallStack(() -> MsgPack.decode(packed)), "the packed value nests more than 1024");
        byte[] unpackable = java.util.Arrays.copyOfRange(packed, 1, packed.length);
        assertEquals(Json.write(nestedLists(MsgPack.UNPACK_DEPTH)), Json.write(onSmallStack(() -> MsgPack.decode(unpackable))));
        String yaml = "a:\n" + "  ".repeat(1) + "b: [" + "[".repeat(400) + "]".repeat(400) + "]\n";
        assertTrue(onSmallStack(() -> Yaml.parse(yaml, "a test")) instanceof Map<?, ?>);
        assertNamed(onSmallStack(() -> Yaml.parse("[".repeat(5000) + "]".repeat(5000), "a test")), "a test is not valid YAML");
    }

    private static void assertNamed(Object outcome, String start) {
        assertTrue(outcome instanceof RuntimeException error && error.getMessage().startsWith(start),
            outcome instanceof Throwable thrown ? thrown.toString() : String.valueOf(outcome));
    }

    // ---- branches no shipped schema uses, and schemas the checker refuses (messages from jsonschema 4.26)

    @Test
    void falseSchemasAndItemsFalseWordedAsJsonschemaWordsThem() {
        assertEquals(List.of("False schema does not allow 3"), Contract.messages(map("properties", map("a", false)), map("a", 3L)));
        assertEquals(List.of("Expected at most 0 items but found 1 extra: 7"), Contract.messages(map("items", false), List.of(7L)));
        assertEquals(List.of("Expected at most 0 items but found 2 extra: [7, 'x']"),
            Contract.messages(map("items", false), List.of(7L, "x")));
        assertEquals(List.of(), Contract.messages(map("items", false), List.of()));
    }

    @Test
    void schemasTheCheckerCannotReadAsPythonDoesAreRefusedWhenLoaded() {
        Map<String, String> refusals = new LinkedHashMap<>();
        refusals.put("uses the keyword 'prefixItems'", "{\"prefixItems\": []}");
        refusals.put("names the unknown type 'float'", "{\"type\": \"float\"}");
        refusals.put("has its own $id", "{\"properties\": {\"a\": {\"$id\": \"x\"}}}");
        refusals.put("uses \\w", "{\"pattern\": \"^\\\\w+$\"}");
        refusals.put("uses \\s", "{\"pattern\": \"\\\\s\"}");
        refusals.put("has && inside a character class", "{\"pattern\": \"[a-z&&[^b]]\"}");
        refusals.put("has a [ inside a character class", "{\"pattern\": \"[[a]\"}");
        refusals.put("has a named group", "{\"pattern\": \"(?<name>a)\"}");
        refusals.put("has a Python named group", "{\"pattern\": \"(?P<name>a)\"}");
        refusals.put("ends with a backslash", "{\"pattern\": \"a\\\\\"}");
        for (Map.Entry<String, String> refusal : refusals.entrySet()) {
            Map<String, Object> schema = Values.map(Json.parse(refusal.getValue()));
            String message = assertThrows(JeffError.ContractError.class, () -> Contract.checkSchema(schema, "a test schema"))
                .getMessage();
            assertTrue(message.contains(refusal.getKey()), message);
        }
        Contract.checkSchema(Values.map(Json.parse("{\"pattern\": \"(?<=a)b(?<!c)[\\\\s\\\\S]\\\\d\"}")), "a test schema");
    }

    @Test
    void uniqueItemsOverManyItemsHoldingNaNIsRefusedByName() {
        List<Object> items = new ArrayList<>();
        for (int index = 0; index < 70; index++) items.add(index == 3 ? Double.NaN : (Object) (long) index);
        assertEquals("uniqueItems over 70 items holding a NaN: this SDK does not reproduce the order Python's sort leaves them in",
            assertThrows(JeffError.ContractError.class, () -> Contract.messages(map("uniqueItems", true), items)).getMessage());
    }

    @Test
    void theCataloguesAreReadOnce() {
        assertTrue(Contract.errorCatalogue() == Contract.errorCatalogue());
        assertTrue(Contract.interfaces() == Contract.interfaces());
    }
}
