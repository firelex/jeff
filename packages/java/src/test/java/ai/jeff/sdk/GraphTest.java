package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import ai.jeff.sdk.testing.FakeEngine;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import org.junit.jupiter.api.Test;

class GraphTest {
    private static Map<String, Object> doc() {
        Map<String, Object> kind = Fixtures.ordered("call", "spam.message_type", "context",
            Fixtures.ordered("channel", Fixtures.ordered("param", "channel")));
        Map<String, Object> flag = Fixtures.ordered("code", "flag", "when", Fixtures.ordered("node", "kind", "in", List.of("spam", true)));
        return Fixtures.ordered("graph", "jeff.graphs.t", "version", "1.0.0", "input", "message", "params", List.of("channel"),
            "nodes", Fixtures.ordered("kind", kind, "flag", flag), "outputs", List.of("kind", "flag"));
    }

    private static Map<String, Object> codeOnly(String... names) {
        Map<String, Object> nodes = new LinkedHashMap<>();
        for (String name : names) nodes.put(name, Fixtures.ordered("code", "c" + name));
        return Fixtures.ordered("graph", "jeff.graphs.t", "version", "1.0.0", "input", "message", "params", List.of(),
            "nodes", nodes, "outputs", List.of(names[0]));
    }

    @Test
    void aGraphDocumentReadsBackTheSameAfterToMap() {
        Graph graph = Graphs.fromMap(doc(), "t");
        assertEquals(List.of("kind", "flag"), graph.order());
        assertEquals(doc(), Graphs.toMap(graph));
    }

    @Test
    void aNodeNamingAnUnknownNodeOrParamIsRefused() {
        Map<String, Object> bad = doc();
        Values.map(Values.map(bad.get("nodes")).get("flag")).put("after", List.of("ghost"));
        assertEquals("t: node flag refers to unknown node(s) ghost",
            assertThrows(JeffError.GraphError.class, () -> Graphs.fromMap(bad, "t")).getMessage());
        Map<String, Object> undeclared = doc();
        undeclared.put("params", List.of());
        assertEquals("t: node kind uses undeclared param(s) channel",
            assertThrows(JeffError.GraphError.class, () -> Graphs.fromMap(undeclared, "t")).getMessage());
    }

    @Test
    void numberLikeNodeNamesKeepTheirOrderAndAreWrittenBack() {
        Graph graph = Graphs.fromMap(codeOnly("b", "10", "2"), "t");
        assertEquals(List.of("b", "10", "2"), List.copyOf(graph.nodes().keySet()));
        assertEquals(List.of("b", "10", "2"), graph.order());
        assertEquals(List.of("b", "10", "2"), List.copyOf(Values.map(Graphs.toMap(graph).get("nodes")).keySet()));
    }

    @Test
    void aGraphWhoseOrderIsNotTheOrderItsDependenciesGiveCannotBeWritten() {
        Graph graph = Graphs.fromMap(doc(), "t");
        Graph reordered = new Graph(graph.id(), graph.version(), graph.input(), graph.params(), graph.nodes(), graph.outputs(),
            List.of("flag", "kind"));
        assertEquals("jeff.graphs.t cannot be written as a graph document that reads back the same: its order flag, kind is "
            + "not the order its dependencies give (kind, flag)",
            assertThrows(JeffError.GraphError.class, () -> Graphs.toMap(reordered)).getMessage());
    }

    @Test
    void aNodeWithAPolicyTheDocumentCannotHoldIsRefusedByToMap() {
        Graph graph = Graphs.fromMap(doc(), "t");
        GraphNode kind = graph.nodes().get("kind");
        Map<String, GraphNode> nodes = new LinkedHashMap<>(graph.nodes());
        nodes.put("kind", new GraphNode(kind.name(), kind.call(), kind.code(), kind.after(), kind.context(), kind.labels(),
            kind.when(), "escalate"));
        Graph changed = new Graph(graph.id(), graph.version(), graph.input(), graph.params(), Values.frozenMap(nodes),
            graph.outputs(), graph.order());
        assertEquals("jeff.graphs.t: node kind has on_low_confidence 'escalate'; a graph document holds only 'return' or "
            + "'error' (pass other policies in policies=)", assertThrows(JeffError.GraphError.class, () -> Graphs.toMap(changed)).getMessage());
    }

    @Test
    void aCodeFunctionReturningNullStoresItAsPythonStoresNone() {
        Jeff jeff = Jeff.create(new FakeEngine((adapter, row) -> new double[0], List.of()), Catalogue.shipped());
        GraphResult result = jeff.runGraph(Graphs.fromMap(codeOnly("value"), "t"), "hi", Map.of(),
            Map.of("cvalue", call -> null), Map.of());
        assertEquals(new NodeResult.Code(null), result.outputs().get("value"));
        assertNull(((NodeResult.Code) result.nodes().get("value")).value());
    }

    @Test
    void aCodeFunctionReturningSomethingThatIsNotJsonIsAGraphError() {
        Jeff jeff = Jeff.create(new FakeEngine((adapter, row) -> new double[0], List.of()), Catalogue.shipped());
        String message = assertThrows(JeffError.GraphError.class, () -> jeff.runGraph(Graphs.fromMap(codeOnly("value"), "t"),
            "hi", Map.of(), Map.of("cvalue", call -> new Object()), Map.of())).getMessage();
        assertTrue(message.startsWith("jeff.graphs.t: code function cvalue (node value) returned a java.lang.Object, which is "
            + "not a JSON value"), message);
    }

    @Test
    void aCodeFunctionsIntegerIsStoredAsALong() {
        Jeff jeff = Jeff.create(new FakeEngine((adapter, row) -> new double[0], List.of()), Catalogue.shipped());
        GraphResult result = jeff.runGraph(Graphs.fromMap(codeOnly("value"), "t"), "hi", Map.of(),
            Map.of("cvalue", call -> 3), Map.of());
        assertEquals(Map.of("code", 3L), result.outputs().get("value").toJson());
    }

    // ---- beyond the brief: each expected message below is the Python SDK's, from a run of the same graph

    private static Map<String, Object> withWhen(Object when) {
        Map<String, Object> nodes = Fixtures.ordered("a", Fixtures.ordered("code", "f"), "b", Fixtures.ordered("code", "g", "when", when));
        return Fixtures.ordered("graph", "jeff.graphs.t", "version", "1.0.0", "input", "message", "params", List.of(),
            "nodes", nodes, "outputs", List.of("b"));
    }

    private static Map<String, Object> nots(int depth) {
        Map<String, Object> condition = Fixtures.ordered("node", "a", "is", 1L);
        for (int index = 0; index < depth; index++) condition = Fixtures.ordered("not", condition);
        return condition;
    }

    /** The work's result or failure, on a thread with a 256 KB stack. */
    private static Object onSmallStack(Supplier<Object> work) throws InterruptedException {
        AtomicReference<Object> outcome = new AtomicReference<>();
        Thread thread = Thread.ofPlatform().stackSize(256 * 1024).unstarted(() -> {
            try {
                outcome.set(work.get());
            } catch (Throwable thrown) {
                outcome.set(thrown);
            }
        });
        thread.start();
        thread.join();
        return outcome.get();
    }

    private static Jeff codeJeff() {
        return Jeff.create(new FakeEngine((adapter, row) -> new double[0], List.of()), Catalogue.shipped());
    }

    @Test
    void aConditionTheSchemaRefusesListsEveryAlternativeInPythonsOrder() {
        String message = assertThrows(JeffError.GraphError.class,
            () -> Graphs.fromMap(withWhen(Fixtures.ordered("node", "a")), "t")).getMessage();
        assertEquals("t does not match the graph schema:\n"
            + "  at /nodes/b/when: {'node': 'a'} is not valid under any of the given schemas\n"
            + "    - Additional properties are not allowed ('node' was unexpected)\n"
            + "    - 'all' is a required property\n"
            + "    - Additional properties are not allowed ('node' was unexpected)\n"
            + "    - 'any' is a required property\n"
            + "    - Additional properties are not allowed ('node' was unexpected)\n"
            + "    - 'not' is a required property\n"
            + "    - 'is' is a required property\n"
            + "    - 'in' is a required property\n"
            + "    - 'abstained' is a required property\n"
            + "    - 'confidence_below' is a required property", message);
    }

    @Test
    void aCycleIsNamedAlongItsPathWhateverTheNodesAreCalled() {
        Map<String, Object> nodes = Fixtures.ordered("b", Fixtures.ordered("code", "f", "after", List.of("10")),
            "10", Fixtures.ordered("code", "g", "after", List.of("2")), "2", Fixtures.ordered("code", "h", "after", List.of("b")));
        Map<String, Object> doc = Fixtures.ordered("graph", "jeff.graphs.t", "version", "1.0.0", "input", "message",
            "params", List.of(), "nodes", nodes, "outputs", List.of("b"));
        assertEquals("t: the nodes form a cycle: b -> 10 -> 2 -> b",
            assertThrows(JeffError.GraphError.class, () -> Graphs.fromMap(doc, "t")).getMessage());
    }

    @Test
    void numberLikeNamesAreOrderedByTheirDependenciesSortedAsStrings() {
        Map<String, Object> nodes = Fixtures.ordered("b", Fixtures.ordered("code", "f", "after", List.of("10", "2")),
            "10", Fixtures.ordered("code", "g"), "2", Fixtures.ordered("code", "h", "after", List.of("1a")), "1a", Fixtures.ordered("code", "i"));
        Map<String, Object> doc = Fixtures.ordered("graph", "jeff.graphs.t", "version", "1.0.0", "input", "message",
            "params", List.of(), "nodes", nodes, "outputs", List.of("b", "10"));
        Graph graph = Graphs.fromMap(doc, "t");
        assertEquals(List.of("10", "1a", "2", "b"), graph.order());
        assertEquals(List.of("b", "10", "2", "1a"), List.copyOf(graph.nodes().keySet()));
        assertEquals(doc, Graphs.toMap(graph));
    }

    @Test
    void aChainLongerThanPythonCanRecurseIsOrderedOnASmallStack() throws InterruptedException {
        Map<String, Object> nodes = new LinkedHashMap<>();
        for (int index = 0; index < 20_000; index++) {
            nodes.put("n" + index, index == 0 ? Fixtures.ordered("code", "f") : Fixtures.ordered("code", "f", "after", List.of("n" + (index - 1))));
        }
        Map<String, Object> doc = Fixtures.ordered("graph", "jeff.graphs.t", "version", "1.0.0", "input", "message",
            "params", List.of(), "nodes", nodes, "outputs", List.of("n19999"));
        Object outcome = onSmallStack(() -> Graphs.fromMap(doc, "t"));
        Graph graph = assertInstanceOf(Graph.class, outcome);
        assertEquals("n0", graph.order().get(0));
        assertEquals("n19999", graph.order().get(19_999));
        // the same chain closed into a cycle is named in full
        Values.map(nodes.get("n0")).put("after", List.of("n19999"));
        Object cycle = onSmallStack(() -> Graphs.fromMap(doc, "t"));
        assertTrue(assertInstanceOf(JeffError.GraphError.class, cycle).getMessage().startsWith("t: the nodes form a cycle: n0 -> n19999 -> n19998"));
    }

    @Test
    void aDeepDocumentIsAGraphErrorOnASmallStackNeverAStackOverflow() throws InterruptedException {
        // deeper than the schema checker goes: its own named error, as a GraphError
        Object checked = onSmallStack(() -> Graphs.fromMap(withWhen(nots(400)), "t"));
        assertTrue(assertInstanceOf(JeffError.GraphError.class, checked).getMessage()
            .startsWith("The document is nested too deeply to check against the graph schema"));
        // deeper than the document can be copied (Python's json depth): the copy's error, as a GraphError with its cause
        Object copied = onSmallStack(() -> Graphs.fromMap(withWhen(nots(10_000)), "t"));
        JeffError.GraphError error = assertInstanceOf(JeffError.GraphError.class, copied);
        assertTrue(error.getMessage().startsWith("t: The value nests more than 9998 lists and maps"), error.getMessage());
        assertInstanceOf(IllegalArgumentException.class, error.getCause());
        // a hand-built node's condition that cannot be copied
        Object built = onSmallStack(() -> new GraphNode("b", null, "g", List.of(), new LinkedHashMap<>(), null, nots(10_000), "return"));
        assertTrue(assertInstanceOf(JeffError.GraphError.class, built).getMessage().startsWith("The condition of node b is not a condition"));
    }

    @Test
    void aDeepHandBuiltConditionIsReadOnASmallStack() throws InterruptedException {
        Graph graph = Graphs.fromMap(withWhen(nots(2)), "t");
        GraphNode b = graph.nodes().get("b");
        Map<String, GraphNode> nodes = new LinkedHashMap<>(graph.nodes());
        nodes.put("b", new GraphNode("b", null, "g", List.of(), b.context(), null, nots(5_000), "return"));
        Graph deep = new Graph(graph.id(), graph.version(), graph.input(), graph.params(), Values.frozenMap(nodes), graph.outputs(), graph.order());
        Object outcome = onSmallStack(() -> codeJeff().runGraph(deep, "hi", Map.of(), Map.of("f", call -> 1, "g", call -> 2), Map.of()));
        assertEquals(new NodeResult.Code(2L), assertInstanceOf(GraphResult.class, outcome).outputs().get("b"));
        // a document cannot hold it: the schema checker's named error, as a GraphError
        Object written = onSmallStack(() -> Graphs.toMap(deep));
        assertTrue(assertInstanceOf(JeffError.GraphError.class, written).getMessage()
            .startsWith("The document is nested too deeply to check against the graph schema"));
    }

    @Test
    void aFailingCodeFunctionIsAGraphErrorWithTheFailureAsItsCause() {
        IllegalStateException failure = new IllegalStateException("no ticket system");
        JeffError.GraphError error = assertThrows(JeffError.GraphError.class, () -> codeJeff().runGraph(
            Graphs.fromMap(codeOnly("value"), "t"), "hi", Map.of(), Map.of("cvalue", call -> { throw failure; }), Map.of()));
        assertEquals("jeff.graphs.t: code function cvalue (node value) failed: no ticket system", error.getMessage());
        assertEquals(failure, error.getCause());
    }

    @Test
    void aHandBuiltConditionNamingNoNodeIsAGraphError() {
        Graph graph = Graphs.fromMap(withWhen(nots(0)), "t");
        GraphNode bad = new GraphNode("b", null, "g", List.of(), new LinkedHashMap<>(), null,
            Fixtures.ordered("any", List.of(Fixtures.ordered("is", 1L))), "return");
        assertEquals("The condition of node b has a part graph.schema.json does not allow: {'is': 1}",
            assertThrows(JeffError.GraphError.class, () -> Graphs.dependencies(bad)).getMessage());
        GraphNode string = new GraphNode("b", null, "g", List.of(), new LinkedHashMap<>(), null, Fixtures.ordered("not", "x"), "return");
        assertEquals("The condition of node b has a part graph.schema.json does not allow: 'x'",
            assertThrows(JeffError.GraphError.class, () -> Graphs.dependencies(string)).getMessage());
        // read at run time, the condition names the graph too
        Map<String, GraphNode> nodes = new LinkedHashMap<>(graph.nodes());
        nodes.put("b", new GraphNode("b", null, "g", List.of("a"), new LinkedHashMap<>(), null,
            Fixtures.ordered("node", "a", "in", "xyz"), "return"));
        Graph changed = new Graph(graph.id(), graph.version(), graph.input(), graph.params(), Values.frozenMap(nodes), graph.outputs(), graph.order());
        assertEquals("jeff.graphs.t: the condition of node b has a part graph.schema.json does not allow: {'node': 'a', 'in': 'xyz'}",
            assertThrows(JeffError.GraphError.class, () -> codeJeff().runGraph(changed, "hi", Map.of(),
                Map.of("f", call -> "y", "g", call -> 1), Map.of())).getMessage());
    }

    @Test
    void aHandBuiltGraphThatPythonStopsOnWithAKeyErrorIsANamedGraphError() {
        Graph graph = Graphs.fromMap(codeOnly("a", "b"), "t");
        Map<String, CodeFunction> code = Map.of("ca", call -> 1, "cb", call -> 2);
        Graph ghostOrder = new Graph(graph.id(), graph.version(), graph.input(), graph.params(), graph.nodes(), graph.outputs(),
            List.of("a", "ghost", "b"));
        assertEquals("jeff.graphs.t: its order names node ghost, which the graph does not have",
            assertThrows(JeffError.GraphError.class, () -> codeJeff().runGraph(ghostOrder, "hi", Map.of(), code, Map.of())).getMessage());
        Graph ghostOutputs = new Graph(graph.id(), graph.version(), graph.input(), graph.params(), graph.nodes(), List.of("b", "ghost", "a"),
            graph.order());
        assertEquals("jeff.graphs.t: outputs name unknown node(s) ghost",
            assertThrows(JeffError.GraphError.class, () -> codeJeff().runGraph(ghostOutputs, "hi", Map.of(), code, Map.of())).getMessage());
        Graph model = Graphs.fromMap(doc(), "t");
        GraphNode kind = model.nodes().get("kind");
        Map<String, GraphNode> nodes = new LinkedHashMap<>(model.nodes());
        nodes.put("kind", new GraphNode("kind", kind.call(), null, kind.after(), new LinkedHashMap<>(Map.of("channel", "nochannel")), null, null, "return"));
        Graph notGiven = new Graph(model.id(), model.version(), model.input(), model.params(), Values.frozenMap(nodes), model.outputs(), model.order());
        Jeff jeff = Jeff.create(new FakeEngine((adapter, row) -> new double[] {1, 0, 0}, List.of("jeff.safety.spam")), Catalogue.shipped());
        assertEquals("jeff.graphs.t: node kind uses param(s) nochannel that were not given",
            assertThrows(JeffError.GraphError.class, () -> jeff.runGraph(notGiven, "hi", Map.of("channel", "sms"),
                Map.of("flag", call -> 1), Map.of())).getMessage());
    }

    @Test
    void aGraphResultReadsBackFromItsJson() {
        Jeff jeff = Jeff.create(new FakeEngine((adapter, row) -> new double[] {1, 0, 0}, List.of("jeff.safety.spam")), Catalogue.shipped());
        GraphResult result = jeff.runGraph(Graphs.fromMap(doc(), "t"), "hi", Map.of("channel", "sms"), Map.of("flag", call -> List.of(1, "x")), Map.of());
        assertEquals("legitimate", ((NodeResult.Decided) result.outputs().get("kind")).decision().value());
        assertEquals(new NodeResult.Skipped("condition not met"), result.outputs().get("flag"));
        assertEquals(result.toJson(), GraphResult.fromJson(result.toJson()).toJson());
        assertEquals(result.toJson(), Async.join(jeff.runGraphAsync(Graphs.fromMap(doc(), "t"), "hi", Map.of("channel", "sms"),
            Map.of("flag", call -> List.of(1, "x")), Map.of())).toJson());
        assertEquals("A graph result node must hold 'decision', 'skipped' or 'code'; got keys ['a', 'b']",
            assertThrows(JeffError.GraphError.class, () -> NodeResult.fromJson(Fixtures.ordered("b", 1L, "a", 2L))).getMessage());
    }
}
