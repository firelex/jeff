package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;

/** The golden comparison and the served-adapter check; the expected values were printed by the Python SDK
 * (jeff.evaluate.golden_problems and same, jeff.engine.serves). */
class GoldenTest {
    @Test
    void servesAsksACachedListOnceMoreWithRefresh() {
        List<Boolean> asked = new ArrayList<>();
        Engine engine = new Engine() {
            public BaseFormat baseFormat() { return BaseFormat.bf16(); }
            public Set<String> installed(boolean refresh) { asked.add(refresh); return refresh ? Set.of("a") : Set.of(); }
            public EngineResult predict(String adapterId, List<Row> rows) { throw new AssertionError("not called"); }
        };
        assertTrue(Jeff.serves(engine, "a"));
        assertEquals(List.of(false, true), asked);
        assertFalse(Jeff.serves(engine, "b"));
    }

    @Test
    void goldenProblemsWordEachDifferenceAsPythonDoes() {
        Decision decision = new Decision(true, 0.6, false, true, "i", "f", "1", new LinkedHashMap<>(), null, List.of(), "x",
            List.of(), new LinkedHashMap<>(), List.of());
        Map<String, Object> call = Map.of("function", "f", "input", "x");
        assertEquals(List.of("value True, expected 1", "confidence 0.6, expected 0.9 +/- 0.05",
            "abstained False, expected True (the recorded confidence is more than 0.05 from the threshold 0.7)"),
            Golden.goldenProblems(Map.of("name", "n", "call", call, "expected", Map.of("value", 1L, "abstained", true,
                "confidence", 0.9), "tolerance", 0.05), decision, 0.7));
        assertEquals(List.of("abstained False, expected True (the recorded confidence is more than 0.05 from the threshold None)"),
            Golden.goldenProblems(Map.of("name", "n", "call", call, "expected", Map.of("value", true, "abstained", true,
                "confidence", 0.62), "tolerance", 0.05), decision, null));
        assertEquals(List.of(), Golden.goldenProblems(Map.of("name", "n", "call", call, "expected", Map.of("value", true,
            "abstained", true, "confidence", 0.62), "tolerance", 0.05), decision, 0.65));
        // Whole numbers in a case print as Python ints, and an abstained of 0 equals False in Python (no problem).
        assertEquals(List.of("confidence 0.6, expected 1 +/- 0",
            "abstained False, expected 1 (the recorded confidence is more than 0 from the threshold None)"),
            Golden.goldenProblems(Map.of("name", "n", "call", call, "expected", Map.of("value", true, "abstained", 1L,
                "confidence", 1L), "tolerance", 0L), decision, null));
        assertEquals(List.of(), Golden.goldenProblems(Map.of("name", "n", "call", call, "expected", Map.of("value", true,
            "abstained", 0L, "confidence", 0.6), "tolerance", 0L), decision, null));
        Decision answered = decision.answeredBy("human", "a", Map.of());
        assertEquals(List.of("no confidence"), Golden.goldenProblems(Map.of("name", "n", "call", call, "expected",
            Map.of("value", "a", "abstained", false, "confidence", 0.5), "tolerance", 0.1), answered, 0.5));
    }

    @Test
    void sameIsEqualAndOfTheSamePythonType() {
        assertTrue(Golden.same(1L, 1L));
        assertFalse(Golden.same(true, 1L));
        assertFalse(Golden.same(1L, 1.0));
        assertTrue(Golden.same("a", "a"));
        assertTrue(Golden.same(null, null));
        assertFalse(Golden.same(null, false));
    }
}
