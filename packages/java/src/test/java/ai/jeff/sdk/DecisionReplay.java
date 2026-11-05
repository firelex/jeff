package ai.jeff.sdk;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** The fixtures' scripted engine and the runner of decisions.json's (and remote.json's) ops. */
final class DecisionReplay {
    private DecisionReplay() {}

    /** The n-th predict answers the n-th recorded predict, and must be asked for exactly its adapter and rows
     * (compared as MessagePack bytes). */
    static final class ScriptedEngine implements Engine {
        private final Set<String> served;
        private final BaseFormat baseFormat;
        private final Deque<Map<String, Object>> script = new ArrayDeque<>();
        private final List<String> mismatches = new ArrayList<>();

        ScriptedEngine(Collection<String> installed, BaseFormat baseFormat, List<Object> predicts) {
            this.served = Set.copyOf(new LinkedHashSet<>(installed));
            this.baseFormat = baseFormat;
            for (Object predict : predicts) script.add(Values.map(predict));
        }

        @Override
        public BaseFormat baseFormat() {
            return baseFormat;
        }

        @Override
        public Set<String> installed(boolean refresh) {
            return served;
        }

        @Override
        public EngineResult predict(String adapterId, List<Row> rows) {
            Map<String, Object> next = script.pollFirst();
            if (next == null) throw new AssertionError("The script has no answer left for a predict of " + adapterId);
            if (!next.get("adapter").equals(adapterId)) mismatches.add("asked " + adapterId + ", recorded " + next.get("adapter"));
            List<Object> wire = new ArrayList<>();
            for (Row row : rows) wire.add(row.toWire());
            if (!Fixtures.base64(MsgPack.encode(wire)).equals(next.get("rows_msgpack"))) {
                mismatches.add("rows of " + adapterId + " differ from the recording");
            }
            List<double[]> probabilities = new ArrayList<>();
            for (Object row : Values.list(next.get("probabilities"))) {
                probabilities.add(Values.list(row).stream().mapToDouble(DecisionsDirectConformanceTest::probability).toArray());
            }
            return new EngineResult(probabilities, new LinkedHashMap<>(Values.map(next.get("timing"))), null);
        }

        List<String> mismatches() {
            return mismatches;
        }

        int remaining() {
            return script.size();
        }
    }

    private static Ask asker(String step, String answer, List<Object> questions, List<Question> asked) {
        return question -> {
            asked.add(question);
            Map<String, Object> recorded = new LinkedHashMap<>();
            recorded.put("step", step);
            recorded.put("question", question.toJson());
            questions.add(recorded);
            return answer;
        };
    }

    /** A fixture policy as a Policy; any other string is passed on, for Jeff to refuse. */
    static Policy policyFrom(Object spec, List<Object> questions, List<Question> asked) {
        if (spec instanceof String name) return Policy.named(name);
        Map<String, Object> escalate = Values.map(spec);
        List<Step> steps = new ArrayList<>();
        for (Object item : Values.list(escalate.get("escalate"))) {
            Map<String, Object> step = Values.map(item);
            if (step.containsKey("function")) steps.add(Step.function((String) step.get("function")));
            else if (step.containsKey("llm")) steps.add(Step.llm(asker("llm", (String) step.get("llm"), questions, asked)));
            else if (step.containsKey("human")) steps.add(Step.human(asker("human", (String) step.get("human"), questions, asked)));
            else throw new AssertionError("Unknown escalation step " + step);
        }
        return escalate.containsKey("budget_ms") ? Policy.escalate(steps, (Number) escalate.get("budget_ms")) : Policy.escalate(steps);
    }

    private static CallOptions options(Map<String, Object> args, Policy policy) {
        return CallOptions.none().context(Values.map(args.get("context"))).labels(args.get("labels")).onLowConfidence(policy);
    }

    private static List<Object> json(List<Decision> decisions) {
        List<Object> out = new ArrayList<>();
        for (Decision decision : decisions) out.add(decision.toJson());
        return out;
    }

    private static Decision shorthand(Jeff jeff, String iface, Object input, String function, CallOptions options) {
        boolean routed = function == null;
        return switch (iface) {
            case "classify" -> routed ? jeff.classify(input, options) : jeff.classify(input, function, options);
            case "route" -> routed ? jeff.route(input, options) : jeff.route(input, function, options);
            case "rate" -> routed ? jeff.rate(input, options) : jeff.rate(input, function, options);
            case "detect" -> routed ? jeff.detect(input, options) : jeff.detect(input, function, options);
            case "is_supported" -> routed ? jeff.isSupported(input, options) : jeff.isSupported(input, function, options);
            case "equivalent" -> routed ? jeff.equivalent(input, options) : jeff.equivalent(input, function, options);
            case "requires_escalation" -> routed ? jeff.requiresEscalation(input, options)
                : jeff.requiresEscalation(input, function, options);
            default -> throw new AssertionError("Unknown interface " + iface);
        };
    }

    static Object runOp(Jeff jeff, String op, Map<String, Object> args, List<Object> questions, List<Question> asked) {
        Policy policy = policyFrom(args.containsKey("policy") ? args.get("policy") : "return", questions, asked);
        switch (op) {
            case "decide" -> {
                List<Call> calls = new ArrayList<>();
                for (Object item : Values.list(args.get("calls"))) {
                    Map<String, Object> call = Values.map(item);
                    calls.add(new Call((String) call.get("function"), call.get("input"), Values.map(call.get("context")), call.get("labels")));
                }
                return json(jeff.decide(calls));
            }
            case "function" -> {
                BoundFunction bound = jeff.function((String) args.get("ref"), options(args, policy));
                return json(bound.many(Values.list(args.get("inputs"))));
            }
            case "shorthand" -> {
                return shorthand(jeff, (String) args.get("interface"), args.get("input"), (String) args.get("function"),
                    options(args, policy)).toJson();
            }
            case "dispatch" -> {
                String router = args.containsKey("router") ? (String) args.get("router") : Jeff.DEFAULT_ROUTER;
                return jeff.dispatch(args.get("input"), (String) args.get("interface"), router, options(args, policy)).toJson();
            }
            case "graph" -> {
                return graphOp(jeff, args, questions, asked);
            }
            default -> throw new AssertionError("runOp does not know op " + op);
        }
    }

    /** Runs a graph op; each code function records its call (with every node result in graph-result form) and
     * returns the JSON the fixture gives it. */
    private static Object graphOp(Jeff jeff, Map<String, Object> args, List<Object> questions, List<Question> asked) {
        List<Object> codeCalls = new ArrayList<>();
        Map<String, CodeFunction> code = new LinkedHashMap<>();
        Map<String, Object> given = args.containsKey("code") ? Values.map(args.get("code")) : new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : given.entrySet()) {
            String name = entry.getKey();
            Object value = entry.getValue();
            code.put(name, call -> {
                Map<String, Object> nodes = new LinkedHashMap<>();
                for (Map.Entry<String, NodeResult> result : call.nodes().entrySet()) nodes.put(result.getKey(), result.getValue().toJson());
                Map<String, Object> recorded = new LinkedHashMap<>();
                recorded.put("code", name);
                recorded.put("input", call.input());
                recorded.put("params", new LinkedHashMap<>(call.params()));
                recorded.put("nodes", nodes);
                codeCalls.add(recorded);
                return Values.plain(value);
            });
        }
        Map<String, Policy> policies = new LinkedHashMap<>();
        if (args.containsKey("policies")) {
            for (Map.Entry<String, Object> entry : Values.map(args.get("policies")).entrySet()) {
                policies.put(entry.getKey(), policyFrom(entry.getValue(), questions, asked));
            }
        }
        Graph graph = Graphs.fromMap(args.get("graph"), "the conformance graph");
        GraphResult result = jeff.runGraph(graph, args.get("input"), Values.map(args.get("params")), code, policies);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("graph_result", result.toJson());
        out.put("code_calls", codeCalls);
        return out;
    }

    /** How the entries of the questions asked differ in order from the recorded questions (Jackson keeps the file's
     * order); empty when they match. */
    static List<String> entryOrderProblems(List<Question> asked, List<Object> recorded) {
        List<String> problems = new ArrayList<>();
        for (int index = 0; index < Math.min(asked.size(), recorded.size()); index++) {
            Map<String, Object> expected = Values.map(Values.map(recorded.get(index)).get("question"));
            Question question = asked.get(index);
            List<String> options = question.optionEntries().stream().map(Map.Entry::getKey).toList();
            List<String> expectedOptions = List.copyOf(Values.map(expected.get("options")).keySet());
            if (!options.equals(expectedOptions)) problems.add("question " + index + ": options " + options + ", recorded " + expectedOptions);
            List<String> state = question.stateEntries() == null ? null : question.stateEntries().stream().map(Map.Entry::getKey).toList();
            List<String> expectedState = expected.get("state") instanceof Map<?, ?> fields ? List.copyOf(Values.map(fields).keySet()) : null;
            if (!java.util.Objects.equals(state, expectedState)) {
                problems.add("question " + index + ": state fields " + state + ", recorded " + expectedState);
            }
        }
        return problems;
    }

    record Replayed(Map<String, Object> got, ScriptedEngine engine, List<Object> questions, List<String> entryOrder) {}

    static Replayed replay(Map<String, Object> item) {
        ScriptedEngine engine = new ScriptedEngine(Values.strings(item.get("installed")),
            BaseFormat.fromWire(Values.map(item.get("base_format"))), Values.list(item.get("predicts")));
        List<Object> questions = new ArrayList<>();
        List<Question> asked = new ArrayList<>();
        Map<String, Object> got = new LinkedHashMap<>();
        try {
            Jeff jeff = Jeff.create(engine, Fixtures.catalogueFor((String) item.get("catalogue")));
            got.put("result", Fixtures.normalised(runOp(jeff, (String) item.get("op"), Values.map(item.get("args")), questions, asked)));
        } catch (RuntimeException error) {
            got.put("error", Fixtures.wireOf(error));
        }
        return new Replayed(got, engine, questions, entryOrderProblems(asked, Values.list(item.get("questions"))));
    }
}
