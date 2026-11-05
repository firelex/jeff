package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** The expected values below were printed by the Python SDK (jeff.decision.make_decision, jeff.errors.LowConfidence). */
class DecisionTest {
    private static final Catalogue CATALOGUE = Catalogue.shipped();

    private static Binding spam() {
        FunctionSpec fn = CATALOGUE.function("spam.is_spam");
        return Binding.bind(CATALOGUE.manifestOf(fn), fn, Map.of("channel", "sms"), null, fn.calibration());
    }

    private static String refusal(Binding binding, double... probabilities) {
        return assertThrows(JeffError.EngineError.class, () -> Decision.make(binding, probabilities, Map.of())).getMessage();
    }

    @Test
    void aTieGoesToTheFirstOptionInOptionOrderAlsoForNumberLikeKeys() {
        Map<String, Object> raw = Values.map(Values.plain(CATALOGUE.adapter("jeff.safety.spam").raw()));
        Map<String, Object> fixed = new LinkedHashMap<>();
        fixed.put("2", "Two");
        fixed.put("1", "One");
        fixed.put("legit", "Fine");
        Values.map(Values.map(Values.map(raw.get("functions")).get("message_type")).get("labels")).put("fixed", fixed);
        Manifest manifest = Manifests.fromMap(raw, "test");
        FunctionSpec fn = manifest.functions().get("message_type");
        Decision decision = Decision.make(Binding.bind(manifest, fn, Map.of("channel", "sms"), null, null),
            new double[] {0.4, 0.4, 0.2}, Map.of());
        assertEquals("2", decision.value());
        assertEquals(List.of("2", "1", "legit"), List.copyOf(decision.probabilities().keySet()));
    }

    @Test
    void probabilitiesAreNormalisedWithPythonsSumAndAScoreValueIsALong() {
        FunctionSpec fn = CATALOGUE.function("triage.urgency");
        Binding binding = Binding.bind(CATALOGUE.manifestOf(fn), fn, Map.of("company", "c", "channel", "e"), null, fn.calibration());
        Decision decision = Decision.make(binding, new double[] {0.2, 0.2, 0.2, 0.2, 0.2}, Map.of());
        assertEquals(0L, decision.value());
        assertInstanceOf(Long.class, decision.value());
        assertEquals(0.2, decision.confidence());
        assertEquals(2.0, decision.score());
        Decision other = Decision.make(binding, new double[] {0.1, 0.2, 0.3, 0.25, 0.15}, Map.of());
        assertEquals(2L, other.value());
        assertEquals(0.3, other.confidence());
        assertEquals(2.15, other.score());
        assertTrue(other.abstained());
    }

    @Test
    void aScoreValueIsALong() {
        FunctionSpec fn = CATALOGUE.function("triage.urgency");
        Binding binding = Binding.bind(CATALOGUE.manifestOf(fn), fn, Map.of("company", "c", "channel", "e"), null, fn.calibration());
        Object value = Decision.make(binding, new double[] {0, 0, 0, 0.9, 0.1}, Map.of()).toJson().get("value");
        assertEquals(3L, value);
    }

    /** contract/socket.md, "The probabilities of a predict": the first broken rule is named (count, then values, then
     * sum), never repaired. */
    @Test
    void aBrokenEngineReplyIsRefusedNamingTheFirstRuleItBreaks() {
        Binding binding = spam();
        assertEquals("jeff.safety.spam.is_spam: the engine returned 1 probabilities for 2 options", refusal(binding, 0.5));
        assertEquals("jeff.safety.spam.is_spam: the engine returned 3 probabilities for 2 options",
            refusal(binding, Double.NaN, 2, 3));
        assertEquals("jeff.safety.spam.is_spam: the engine returned invalid probabilities [0.0, 0.0]; expected their sum to "
            + "be within 0.0001 of 1, got 0.0", refusal(binding, 0, 0));
        assertEquals("jeff.safety.spam.is_spam: the engine returned invalid probabilities [nan, 1.0]; expected each to be a "
            + "finite number from 0 to 1, which [nan] are not", refusal(binding, Double.NaN, 1));
        assertEquals("jeff.safety.spam.is_spam: the engine returned invalid probabilities [inf, -inf]; expected each to be "
            + "a finite number from 0 to 1, which [inf, -inf] are not",
            refusal(binding, Double.POSITIVE_INFINITY, Double.NEGATIVE_INFINITY));
        assertEquals("jeff.safety.spam.is_spam: the engine returned invalid probabilities [1.00005, 0.0]; expected each to "
            + "be a finite number from 0 to 1, which [1.00005] are not", refusal(binding, 1.00005, 0));
        assertEquals("jeff.safety.spam.is_spam: the engine returned invalid probabilities [-0.25, 0.9]; expected each to be "
            + "a finite number from 0 to 1, which [-0.25] are not", refusal(binding, -0.25, 0.9));
        assertEquals("jeff.safety.spam.is_spam: the engine returned invalid probabilities [0.5, 0.50011]; expected their "
            + "sum to be within 0.0001 of 1, got 1.00011", refusal(binding, 0.5, 0.50011));
        Decision withinTolerance = Decision.make(binding, new double[] {0.5, 0.5001}, Map.of());
        assertEquals(true, withinTolerance.value());
        assertEquals(0.5000499950005, withinTolerance.confidence());
    }

    /** confidence < threshold abstains, as Python compares them: exactly at the threshold does not. */
    @Test
    void aConfidenceExactlyAtTheThresholdDoesNotAbstainAndOneBelowDoes() {
        Binding binding = spam();
        double threshold = binding.calibration().threshold();
        assertEquals(0.515807, threshold);
        for (double confidence : new double[] {threshold, Math.nextUp(threshold), Math.nextDown(threshold)}) {
            Decision decision = Decision.make(binding, new double[] {1 - confidence, confidence}, Map.of());
            assertEquals(confidence, decision.confidence());
            assertEquals(confidence < threshold, decision.abstained(), PyFormat.floatRepr(confidence));
            assertTrue(decision.calibrated());
        }
        FunctionSpec fn = binding.function();
        Decision uncalibrated = Decision.make(Binding.bind(binding.manifest(), fn, Map.of("channel", "sms"), null, null),
            new double[] {0.49, 0.51}, Map.of());
        assertFalse(uncalibrated.abstained());
        assertFalse(uncalibrated.calibrated());
    }

    @Test
    void aDecisionSurvivesItsJsonAndLowConfidenceCarriesIt() {
        Decision decision = Decision.make(spam(), new double[] {0.47, 0.53}, Map.of("path", "fake"));
        assertEquals(decision.toJson(), Decision.fromJson(decision.toJson()).toJson());
        assertEquals(decision, Decision.fromJson(decision.toJson()));
        JeffError.LowConfidence error = JeffError.lowConfidence(decision);
        assertEquals("jeff.safety.spam.is_spam abstained (confidence 0.53); on_low_confidence='error'", error.getMessage());
        assertEquals(decision.toJson(), error.decision().toJson());
        assertEquals("LowConfidence.details is missing its 'decision' object (got None)",
            assertThrows(JeffError.ContractError.class, () -> new JeffError.LowConfidence("x").decision()).getMessage());
        Decision answered = decision.answeredBy("human", true, Map.of("path", "human"));
        assertEquals("jeff.safety.spam.is_spam abstained (confidence None); on_low_confidence='error'",
            JeffError.lowConfidence(answered).getMessage());
    }

    /** Python's json.dumps writes NaN and infinities as the bare words NaN, Infinity and -Infinity. */
    @Test
    void aDecisionsJsonWritesNanAndInfinityAsPythonsJsonDumps() {
        Map<String, Object> timing = new LinkedHashMap<>();
        timing.put("x", Double.NaN);
        timing.put("y", Double.POSITIVE_INFINITY);
        Decision decision = Decision.make(spam(), new double[] {0.47, 0.53}, timing);
        assertEquals("{\"x\":NaN,\"y\":Infinity}", Json.write(decision.toJson().get("timing")));
    }

    @Test
    void decisionsShareNoMutableState() {
        Map<String, Object> inner = new LinkedHashMap<>();
        inner.put("ms", 1.5);
        Map<String, Object> timing = new LinkedHashMap<>();
        timing.put("steps", inner);
        List<String> refs = new ArrayList<>(List.of("a"));
        Decision decision = Decision.make(spam(), new double[] {0.47, 0.53}, timing).withRoute(List.of());
        inner.put("ms", 9.0);
        timing.put("later", 1L);
        refs.add("b");
        assertEquals(Map.of("steps", Map.of("ms", 1.5)), decision.timing());
        assertThrows(UnsupportedOperationException.class, () -> Values.map(decision.timing().get("steps")).put("ms", 2.0));
        assertThrows(UnsupportedOperationException.class, () -> decision.probabilities().put("true", 1.0));
        Map<String, Object> json = decision.toJson();
        Values.map(Values.map(json.get("timing")).get("steps")).put("ms", 3.0);
        Values.map(json.get("probabilities")).put("true", 1.0);
        assertEquals(Map.of("steps", Map.of("ms", 1.5)), decision.timing());
        assertEquals(0.53, decision.probabilities().get("true"));
        assertNotSame(decision.toJson().get("timing"), decision.toJson().get("timing"));
    }
}
