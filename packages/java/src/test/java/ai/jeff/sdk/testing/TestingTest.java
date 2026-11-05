package ai.jeff.sdk.testing;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;

import ai.jeff.sdk.BaseFormat;
import ai.jeff.sdk.EngineResult;
import ai.jeff.sdk.JeffError;
import ai.jeff.sdk.Row;
import ai.jeff.sdk.RowQuestion;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** The expected values below were printed by the Python SDK (jeff.testing.FakeEngine and fake_tokens). */
class TestingTest {
    private static Row row() {
        Map<String, Object> state = new LinkedHashMap<>();
        state.put("channel", "sms");
        state.put("message", "Win  a prize\tnow\n café x");
        Map<String, Object> criteria = new LinkedHashMap<>();
        criteria.put("false", "Not spam.");
        criteria.put("true", "Spam.");
        return new Row(state, new RowQuestion("noul", "Is this spam?", criteria));
    }

    @Test
    void fakeEngineAnswersFromTheFunctionRecordsItsCallsAndRefusesAnAdapterItDoesNotServe() {
        FakeEngine engine = new FakeEngine((adapter, row) -> new double[] {0.25, 0.75}, List.of("jeff.safety.spam"));
        EngineResult result = engine.predict("jeff.safety.spam", List.of(row()));
        assertArrayEquals(new double[] {0.25, 0.75}, result.probabilities().get(0));
        assertEquals(Map.of("path", "fake", "model_ms", 0.0, "format", "bf16"), result.timing());
        assertEquals(List.of("path", "model_ms", "format"), List.copyOf(result.timing().keySet()));
        assertInstanceOf(Double.class, result.timing().get("model_ms"));
        // json.dumps(row).split() in Python: 22 words ("Win  a" is two words, "\t" and "\n" are escaped inside one).
        assertEquals(List.of(22L), result.inputTokens());
        assertEquals(22, FakeEngine.fakeTokens(row()));
        assertEquals("jeff.safety.spam", engine.calls().get(0).getKey());
        assertEquals(List.of(row()), engine.calls().get(0).getValue());
        assertEquals("jeff.support.triage is not installed: FakeEngine has not loaded it. Install it with: jeff pull "
            + "jeff.support.triage",
            assertThrows(JeffError.NotInstalled.class, () -> engine.predict("jeff.support.triage", List.of(row()))).getMessage());
        assertEquals(1, engine.calls().size());
    }

    @Test
    void anInt4FakeEngineReportsItsFormatInItsTiming() {
        BaseFormat int4 = new BaseFormat("int4-g64", "jeff-base@v1.3+int4-g64-gptq.0123abcd");
        FakeEngine engine = new FakeEngine((adapter, row) -> new double[] {0.5, 0.5}, List.of("a"), int4);
        assertEquals(int4, engine.baseFormat());
        assertEquals(List.of("path", "model_ms", "format", "format_id"),
            List.copyOf(engine.predict("a", List.of(row())).timing().keySet()));
    }

    @Test
    void fakeEngineRefusesAMalformedBaseFormat() {
        assertThrows(JeffError.EngineError.class,
            () -> new FakeEngine((adapter, row) -> new double[0], List.of(), new BaseFormat("int4-g64", null)));
    }

    @Test
    void anEngineResultSharesNoArrayWithItsMaker() {
        double[] given = {0.25, 0.75};
        EngineResult result = new EngineResult(List.of(given), new LinkedHashMap<>(), null);
        given[0] = 1;
        result.probabilities().get(0)[1] = 1;
        assertArrayEquals(new double[] {0.25, 0.75}, result.probabilities().get(0));
    }
}
