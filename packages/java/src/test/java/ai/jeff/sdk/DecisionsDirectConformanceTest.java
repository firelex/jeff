package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/** The decide cases of one call and one predict, replayed without the client: bind, build the row, make the decision
 * from the probabilities the engine answered. Task 10 replays every case through the client. */
class DecisionsDirectConformanceTest {
    static List<Map<String, Object>> direct() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map<String, Object> item : Fixtures.cases("decisions.json")) {
            Map<String, Object> args = Values.map(item.get("args"));
            if (item.get("op").equals("decide") && Values.list(args.get("calls")).size() == 1
                && Values.list(item.get("predicts")).size() == 1) {
                out.add(item);
            }
        }
        return out;
    }

    static Stream<Arguments> cases() {
        return Fixtures.named(direct());
    }

    /** A probability as decisions.json writes it: a number, or (JSON having none) "NaN", "Infinity" or "-Infinity"
     * for one that is not finite (contract/socket.md, "The probabilities of a predict"). */
    static double probability(Object value) {
        if (value instanceof Double number) return number;
        if ("NaN".equals(value)) return Double.NaN;
        if ("Infinity".equals(value)) return Double.POSITIVE_INFINITY;
        if ("-Infinity".equals(value)) return Double.NEGATIVE_INFINITY;
        throw new AssertionError("decisions.json has a probability that is not a float: " + PyFormat.repr(value));
    }

    @Test
    void thereAreDirectCasesIncludingEveryRefusalOfABrokenReply() {
        assertTrue(direct().size() > 150, direct().size() + " direct cases");
        long refusals = direct().stream().filter(item -> String.valueOf(item.get("expect")).contains("the engine returned")).count();
        assertTrue(refusals >= 50, refusals + " refusals of a broken engine reply");
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    void theDecisionIsThePythonSdksDecision(String name, Map<String, Object> item) {
        Catalogue catalogue = Fixtures.catalogueFor((String) item.get("catalogue"));
        Map<String, Object> call = Values.map(Values.list(Values.map(item.get("args")).get("calls")).get(0));
        Map<String, Object> predict = Values.map(Values.list(item.get("predicts")).get(0));
        Map<String, Object> got = new LinkedHashMap<>();
        try {
            FunctionSpec fn = catalogue.function((String) call.get("function"));
            Binding binding = Binding.bind(catalogue.manifestOf(fn), fn, Values.map(call.get("context")), call.get("labels"),
                Manifests.calibrationFor(fn, BaseFormat.formatIdOf(BaseFormat.fromWire(Values.map(item.get("base_format"))))));
            Row row = Binding.buildRow(binding, call.get("input"));
            assertEquals(predict.get("rows_msgpack"), Fixtures.base64(MsgPack.encode(List.of(row.toWire()))));
            List<Object> probabilities = Values.list(Values.list(predict.get("probabilities")).get(0));
            double[] answer = probabilities.stream().mapToDouble(DecisionsDirectConformanceTest::probability).toArray();
            got.put("result", List.of(Decision.make(binding, answer, Values.map(predict.get("timing"))).toJson()));
        } catch (RuntimeException error) {
            got.put("error", Fixtures.wireOf(error));
        }
        assertEquals(item.get("expect"), got);
    }
}
