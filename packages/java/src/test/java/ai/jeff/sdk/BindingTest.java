package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class BindingTest {
    private static final Catalogue CATALOGUE = Catalogue.shipped();

    private static Binding bindIntent(Object labels) {
        FunctionSpec fn = CATALOGUE.function("support-intents.intent");
        return Binding.bind(CATALOGUE.manifestOf(fn), fn, Map.of("service", "A shop"), labels, null);
    }

    @Test
    void callerLabelsWithoutOrderAreRefused() {
        Map<String, Object> unordered = new HashMap<>();
        unordered.put("refund", "Wants money back");
        unordered.put("track", "Asks where the order is");
        JeffError.InvalidLabels error = assertThrows(JeffError.InvalidLabels.class, () -> bindIntent(unordered));
        assertEquals("jeff.support.support-intents.intent: pass labels= as a LinkedHashMap (or another map that keeps its "
            + "order): the caller's labels become the options in their order, and a java.util.HashMap keeps none",
            error.getMessage());
        assertEquals(List.of("refund"), List.copyOf(bindIntent(Map.of("refund", "Wants money back")).criteria().keySet()));
        Map<String, Object> ordered = new LinkedHashMap<>();
        ordered.put("track", "Asks where the order is");
        ordered.put("refund", "Wants money back");
        assertEquals(List.of("track", "refund"), List.copyOf(bindIntent(ordered).criteria().keySet()));
    }

    @Test
    void aLabelKeyThatIsNotAStringIsRefused() {
        Map<Object, Object> labels = new LinkedHashMap<>();
        labels.put(1L, "One");
        assertEquals("jeff.support.support-intents.intent: label key 1 is not a string",
            assertThrows(JeffError.InvalidLabels.class, () -> bindIntent(labels)).getMessage());
    }

    /** Each key judged as Python's NUMBER_LIKE judges it (checked with `uv run python` against jeff.binding): \d is
     * any Unicode decimal digit, \s any character str.isspace() accepts, and `$` also matches before a final \n. */
    @Test
    void aCallerLabelKeyIsNumberLikeExactlyWhenPythonsPatternSaysSo() {
        FunctionSpec fn = CATALOGUE.function("triage.route");
        Map<String, Object> context = new LinkedHashMap<>();
        context.put("company", "A furniture shop.");
        context.put("channel", "email");
        List<String> refused = List.of("12", " 1.5e3 ", "1.", ".5", "+.5", "-3", "٢٣", "12\n", "12\n\n",
            " 12　", "\u001c7", "12\r", "１２", "𝟎", "1e+5", "12 ");
        List<String> accepted = List.of("1e", "e5", "1_000", "0x1F", "inf", "nan", "1.2.3", " ", "", "12a");
        for (String key : refused) {
            JeffError.InvalidLabels error = assertThrows(JeffError.InvalidLabels.class,
                () -> Binding.bind(CATALOGUE.manifestOf(fn), fn, context, Map.of(key, "D"), null), key);
            assertEquals("jeff.support.triage.route: label key " + PyFormat.str(key) + " looks like a number; use a word",
                error.getMessage());
        }
        for (String key : accepted) {
            Binding binding = Binding.bind(CATALOGUE.manifestOf(fn), fn, context, Map.of(key, "D"), null);
            assertEquals(key, binding.wireToCaller().get("k1"));
        }
    }

    @Test
    void aRowKeepsTheManifestsFieldOrderWhateverOrderTheContextWasGivenIn() {
        FunctionSpec fn = CATALOGUE.function("triage.urgency");
        Map<String, Object> context = new HashMap<>();
        context.put("channel", "email");
        context.put("company", "A shop.");
        Row row = Binding.buildRow(Binding.bind(CATALOGUE.manifestOf(fn), fn, context, null, fn.calibration()), "Fire");
        List<String> manifestOrder = fn.context().stream().map(ContextField::key).toList();
        List<String> expected = new java.util.ArrayList<>(manifestOrder);
        expected.add(fn.input().key());
        assertEquals(expected, List.copyOf(((Map<?, ?>) row.state()).keySet()));
    }
}
