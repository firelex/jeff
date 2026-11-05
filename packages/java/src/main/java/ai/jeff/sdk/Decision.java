package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.SequencedMap;

/** The Decision every function returns (port of jeff/decision.py). {@link #toJson()} is
 * contract/schemas/decision.schema.json's form. A Decision holds no mutable state: its maps and lists are read-only
 * copies (timing deeply so), and {@link #toJson()} hands back fresh mutable copies.
 *
 * @param value the answer: a String (a choice), a Boolean (a yes-or-no question) or a Long (a score)
 * @param confidence the winning option's probability, or null when an llm or human step answered
 * @param abstained whether the decision is unsure: its confidence is below the calibrated threshold, or it chose an
 *     abstain label
 * @param calibrated whether the function has a calibrated threshold on the engine's base
 * @param interfaceName the interface the function implements
 * @param function the function's id
 * @param version the adapter's version
 * @param probabilities each option's probability, keyed by option label in option order
 * @param score a score function's expected level, or null for other functions
 * @param route the routers a dispatch went through, in order
 * @param decidedBy who decided: the function's id and version, or the escalation step that answered
 * @param escalations the earlier attempts an escalation made before this decision, in order
 * @param timing the engine's (or server's) timing fields
 * @param evidenceRefs references to the evidence the decision rests on (empty unless a function gives them) */
public record Decision(Object value, Double confidence, boolean abstained, boolean calibrated, String interfaceName,
                       String function, String version, SequencedMap<String, Double> probabilities, Double score,
                       List<RouteStep> route, String decidedBy, List<EscalationRecord> escalations,
                       SequencedMap<String, Object> timing, List<String> evidenceRefs) {
    /** One router a dispatch went through.
     *
     * @param router the router's function id
     * @param chose the target it chose
     * @param confidence its confidence in that choice */
    public record RouteStep(String router, String chose, double confidence) {}

    /** An earlier attempt of an escalation.
     *
     * @param decidedBy who decided it
     * @param value its answer
     * @param confidence its confidence, or null
     * @param reason why the escalation went on (for example {@code abstained (confidence 0.300 < threshold 0.787)}) */
    public record EscalationRecord(String decidedBy, Object value, Double confidence, String reason) {}

    /** How far from 1 the sum of an engine reply's probabilities may be (contract/socket.md, "The probabilities of a
     * predict"): a float32 softmax over at most 255 options is off by at most about 1.5e-5. */
    static final double SUM_TOLERANCE = 1e-4;

    /** Copies every map and list, read-only.
     *
     * @param value the answer
     * @param confidence the confidence, or null
     * @param abstained whether it abstained
     * @param calibrated whether it is calibrated
     * @param interfaceName the interface
     * @param function the function's id
     * @param version the adapter's version
     * @param probabilities the probabilities by option
     * @param score the score, or null
     * @param route the route
     * @param decidedBy who decided
     * @param escalations the earlier attempts
     * @param timing the timing fields
     * @param evidenceRefs the evidence references
     * @throws NullPointerException when a map or list is null */
    public Decision {
        probabilities = Values.frozenMap(probabilities);
        route = List.copyOf(route);
        escalations = List.copyOf(escalations);
        timing = frozenTiming(timing);
        evidenceRefs = List.copyOf(evidenceRefs);
    }

    @SuppressWarnings("unchecked")
    private static SequencedMap<String, Object> frozenTiming(Map<String, ?> timing) {
        return (SequencedMap<String, Object>) Values.frozen(Values.plain(Objects.requireNonNull(timing, "timing")));
    }

    Decision withRoute(List<RouteStep> newRoute) {
        return new Decision(value, confidence, abstained, calibrated, interfaceName, function, version, probabilities, score,
            newRoute, decidedBy, escalations, timing, evidenceRefs);
    }

    Decision withRouteAndEscalations(List<RouteStep> newRoute, List<EscalationRecord> newEscalations) {
        return new Decision(value, confidence, abstained, calibrated, interfaceName, function, version, probabilities, score,
            newRoute, decidedBy, newEscalations, timing, evidenceRefs);
    }

    /** This decision answered by an llm or human step instead (Python's escalation replace()): no confidence,
     * probabilities, score or escalations, not abstained or calibrated, decided by the step, with the step's timing. */
    Decision answeredBy(String step, Object answer, Map<String, ?> stepTiming) {
        return new Decision(answer, null, false, false, interfaceName, function, version, new LinkedHashMap<>(), null, route,
            step, List.of(), new LinkedHashMap<String, Object>(stepTiming), evidenceRefs);
    }

    /** The decision's JSON, as Python's to_json (dataclasses.asdict) builds it.
     *
     * @return a fresh, mutable copy each call */
    public Map<String, Object> toJson() {
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("value", value);
        json.put("confidence", confidence);
        json.put("abstained", abstained);
        json.put("calibrated", calibrated);
        json.put("interface", interfaceName);
        json.put("function", function);
        json.put("version", version);
        json.put("probabilities", new LinkedHashMap<>(probabilities));
        json.put("score", score);
        List<Object> steps = new ArrayList<>();
        for (RouteStep step : route) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("router", step.router());
            item.put("chose", step.chose());
            item.put("confidence", step.confidence());
            steps.add(item);
        }
        json.put("route", steps);
        json.put("decided_by", decidedBy);
        List<Object> records = new ArrayList<>();
        for (EscalationRecord record : escalations) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("decided_by", record.decidedBy());
            item.put("value", record.value());
            item.put("confidence", record.confidence());
            item.put("reason", record.reason());
            records.add(item);
        }
        json.put("escalations", records);
        json.put("timing", Values.plain(timing));
        json.put("evidence_refs", new ArrayList<>(evidenceRefs));
        return json;
    }

    private static Double number(Object value) {
        return value == null ? null : ((Number) value).doubleValue();
    }

    /** A decision from its JSON (a server's answer, an error's details), as Python's from_json.
     *
     * @param data the JSON, already checked against contract/schemas/decision.schema.json
     * @return the decision */
    public static Decision fromJson(Map<String, Object> data) {
        SequencedMap<String, Double> probabilities = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : Values.map(data.get("probabilities")).entrySet()) {
            probabilities.put(entry.getKey(), number(entry.getValue()));
        }
        List<RouteStep> route = new ArrayList<>();
        for (Object item : Values.list(data.get("route"))) {
            Map<String, Object> step = Values.map(item);
            route.add(new RouteStep((String) step.get("router"), (String) step.get("chose"), number(step.get("confidence"))));
        }
        List<EscalationRecord> escalations = new ArrayList<>();
        for (Object item : Values.list(data.get("escalations"))) {
            Map<String, Object> record = Values.map(item);
            escalations.add(new EscalationRecord((String) record.get("decided_by"), Values.plain(record.get("value")),
                number(record.get("confidence")), (String) record.get("reason")));
        }
        return new Decision(Values.plain(data.get("value")), number(data.get("confidence")), (Boolean) data.get("abstained"),
            (Boolean) data.get("calibrated"), (String) data.get("interface"), (String) data.get("function"),
            (String) data.get("version"), probabilities, number(data.get("score")), route, (String) data.get("decided_by"),
            escalations, new LinkedHashMap<>(Values.map(data.get("timing"))), Values.strings(data.get("evidence_refs")));
    }

    private static String listRepr(List<Double> values) {
        List<String> shown = new ArrayList<>();
        for (double value : values) shown.add(PyFormat.floatRepr(value));
        return "[" + String.join(", ", shown) + "]";
    }

    /** The engine's probabilities for one row, checked as contract/socket.md ("The probabilities of a predict") says
     * and Python's make_decision checks them, the first broken rule named: one per option; each a finite number from
     * 0 to 1; their sum (Python's sum) within SUM_TOLERANCE of 1. A broken reply is refused with EngineError, never
     * repaired. Returns their sum. */
    private static double checkedSum(String functionId, double[] probabilities, int options) {
        if (probabilities.length != options) {
            throw new JeffError.EngineError(functionId + ": the engine returned " + probabilities.length + " probabilities for "
                + options + " options");
        }
        List<Double> given = new ArrayList<>();
        List<Double> offending = new ArrayList<>();
        for (double probability : probabilities) {
            given.add(probability);
            if (!(Double.isFinite(probability) && 0 <= probability && probability <= 1)) offending.add(probability);
        }
        if (!offending.isEmpty()) {
            throw new JeffError.EngineError(functionId + ": the engine returned invalid probabilities " + listRepr(given)
                + "; expected each to be a finite number from 0 to 1, which " + listRepr(offending) + " are not");
        }
        double total = PyFormat.sum(probabilities);
        if (Math.abs(total - 1) > SUM_TOLERANCE) {
            throw new JeffError.EngineError(functionId + ": the engine returned invalid probabilities " + listRepr(given)
                + "; expected their sum to be within " + PyFormat.floatRepr(SUM_TOLERANCE) + " of 1, got "
                + PyFormat.floatRepr(total));
        }
        return total;
    }

    /** The decision for one row from the engine's probabilities, one per option in option order: a reply that breaks
     * the engine's contract is refused (checkedSum); a valid one is divided by its sum. The winner is the first best
     * option in option order; it abstained below the calibration threshold or on an abstain label. */
    static Decision make(Binding binding, double[] probabilities, Map<String, ?> timing) {
        FunctionSpec fn = binding.function();
        Manifest manifest = binding.manifest();
        List<String> keys = Binding.optionKeys(binding);
        double total = checkedSum(fn.id(), probabilities, keys.size());
        double[] normalised = new double[probabilities.length];
        for (int index = 0; index < probabilities.length; index++) normalised[index] = probabilities[index] / total;
        SequencedMap<String, Double> caller = new LinkedHashMap<>();
        for (int index = 0; index < keys.size(); index++) {
            String name = binding.wireToCaller().get(keys.get(index));
            if (name == null) throw new IllegalStateException(fn.id() + ": the binding has no caller key for option " + keys.get(index));
            caller.put(name, normalised[index]);
        }
        Object value;
        double confidence;
        Double score = null;
        if (fn.type().equals("noul")) {
            double yes = caller.get("true");
            boolean answer = yes >= 0.5;
            value = answer;
            confidence = answer ? yes : caller.get("false");
        } else if (fn.type().equals("score")) {
            int best = 0;
            for (int index = 0; index < normalised.length; index++) if (normalised[index] > normalised[best]) best = index;
            value = (long) best;
            confidence = normalised[best];
            double[] weighted = new double[normalised.length];
            for (int index = 0; index < normalised.length; index++) weighted[index] = index * normalised[index];
            score = PyFormat.sum(weighted);
        } else {
            String bestKey = null;
            double bestP = 0;
            for (Map.Entry<String, Double> entry : caller.entrySet()) {
                if (bestKey == null || entry.getValue() > bestP) {
                    bestKey = entry.getKey();
                    bestP = entry.getValue();
                }
            }
            value = bestKey;
            confidence = bestP;
        }
        Calibration calibration = binding.calibration();
        boolean abstained = (calibration != null && confidence < calibration.threshold())
            || (value instanceof String label && fn.abstainLabels().contains(label));
        return new Decision(value, confidence, abstained, calibration != null, fn.interfaceName(), fn.id(), manifest.version(),
            caller, score, List.of(), fn.id() + "@" + manifest.version(), List.of(), new LinkedHashMap<String, Object>(timing),
            List.of());
    }
}
