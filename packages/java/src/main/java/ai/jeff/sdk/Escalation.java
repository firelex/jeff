package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.SequencedMap;

/** Policies applied to an abstained decision (port of jeff/escalation.py). An escalated decision says who answered
 * and lists every earlier attempt; a failing step raises EscalationFailed. */
final class Escalation {
    private Escalation() {}

    static JeffError.InvalidPolicy unknownPolicy(Policy policy) {
        String shown = policy == null ? "None" : policy instanceof Policy.Named named
            ? (named.name() == null ? "None" : PyFormat.str(named.name())) : "escalate(...)";
        return new JeffError.InvalidPolicy("Unknown on_low_confidence policy " + shown + "; use 'return', 'error' or escalate(...)");
    }

    static JeffError.InvalidPolicy notAStep(Step step) {
        return new JeffError.InvalidPolicy("escalate() got " + (step == null ? "None" : step.toString()) + ", which is not a "
            + "step; use Step.function(...), Step.remote(...), Step.llm(...) or Step.human(...)");
    }

    private static boolean plain(Policy policy) {
        return Policy.RETURN.equals(policy) || Policy.ERROR.equals(policy);
    }

    /** Checks a policy before anything is decided: every function step is in the catalogue, installed, and
     * implements the interface the call is answered through. */
    static void checkPolicy(Jeff jeff, String iface, Policy policy) {
        if (plain(policy)) return;
        if (!(policy instanceof Escalate escalate)) throw unknownPolicy(policy);
        for (Step step : escalate.steps()) {
            if (step instanceof Step.RemoteStep || step instanceof Step.AskStep) continue;
            if (!(step instanceof Step.FunctionStep function)) throw notAStep(step);
            FunctionSpec spec;
            try {
                spec = jeff.catalogue().function(function.ref());
            } catch (JeffError.UnknownFunction error) {
                JeffError.InvalidPolicy refused = new JeffError.InvalidPolicy("Escalation step function_step("
                    + PyFormat.str(function.ref()) + "): " + error.getMessage());
                refused.initCause(error);
                throw refused;
            }
            if (!jeff.engine().installed().contains(spec.adapterId())) throw JeffError.notInstalled(spec.id(), "it is an escalation step");
            if (!spec.interfaceName().equals(iface)) {
                throw new JeffError.InterfaceMismatch("Escalation step " + spec.id() + " implements " + spec.interfaceName()
                    + ", not " + iface);
            }
        }
    }

    private static String reason(Decision decision, Double threshold) {
        if (threshold != null && decision.confidence() != null && decision.confidence() < threshold) {
            return "abstained (confidence " + PyFormat.fixed(decision.confidence(), 3) + " < threshold "
                + PyFormat.floatRepr(threshold) + ")";
        }
        return "abstained (abstain label)";
    }

    private static Decision.EscalationRecord record(Jeff jeff, Decision decision) {
        Calibration calibration = jeff.calibrationOf(jeff.catalogue().function(decision.function()));
        return new Decision.EscalationRecord(decision.decidedBy(), decision.value(), decision.confidence(),
            reason(decision, calibration == null ? null : calibration.threshold()));
    }

    private record Asked(Question question, List<String> keys) {}

    private static Asked questionOf(Jeff jeff, Call call) {
        Binding binding = jeff.bindCall(call);
        FunctionSpec fn = binding.function();
        Row row = Binding.buildRow(binding, call.input());
        SequencedMap<String, Object> options = new LinkedHashMap<>();
        if (fn.type().equals("choice")) {
            for (String key : Binding.optionKeys(binding)) {
                String caller = binding.wireToCaller().get(key);
                if (caller == null) throw new IllegalStateException("the binding of " + fn.id() + " has no caller key for option " + key);
                if (!binding.criteria().containsKey(key)) {
                    throw new IllegalStateException("the binding of " + fn.id() + " has no description for option " + key);
                }
                options.put(caller, binding.criteria().get(key));
            }
        } else if (fn.type().equals("score")) {
            List<String> levels = fn.labels().levels();
            for (int index = 0; index < levels.size(); index++) options.put(String.valueOf(index), levels.get(index));
        } else {
            // the wording the engine shows the model when a noul function does not describe its answers
            SequencedMap<String, String> described = fn.labels().fixed();
            options.put("true", described.containsKey("true") ? described.get("true") : "Yes / true");
            options.put("false", described.containsKey("false") ? described.get("false") : "No / false");
        }
        return new Asked(new Question(fn.id(), fn.instructions(), row.state(), Values.frozenMap(options)), List.copyOf(options.keySet()));
    }

    /** How long an ask step took, in milliseconds rounded to 3 decimals as Python's round(x, 3) rounds: the exact
     * value of the double, an exact half to even. */
    static double askMs(long nanos) {
        return Double.parseDouble(PyFormat.fixed(nanos / 1e6, 3));
    }

    private static Object converted(String type, String answer) {
        if (type.equals("noul")) return answer.equals("true");
        if (type.equals("score")) return Long.parseLong(answer);
        return answer;
    }

    private static Decision runStep(Jeff jeff, Call call, Decision original, Step step) {
        if (step instanceof Step.FunctionStep function) {
            FunctionSpec spec = jeff.catalogue().function(function.ref());
            if (!spec.interfaceName().equals(original.interfaceName())) {
                throw new JeffError.InterfaceMismatch("Escalation step " + spec.id() + " implements " + spec.interfaceName()
                    + ", not " + original.interfaceName());
            }
            return jeff.decide(List.of(new Call(function.ref(), call.input(), call.context(), call.labels()))).get(0);
        }
        if (step instanceof Step.RemoteStep remote) {
            List<Decision> answered = remote.client().decide(List.of(call));
            // Python takes the first decision (IndexError when there is none); one call has exactly one decision
            if (answered == null || answered.size() != 1) {
                throw new JeffError.ContractError("The remote step's client answered "
                    + (answered == null ? "null" : answered.size() + " decisions") + " for 1 call");
            }
            return answered.get(0);
        }
        Step.AskStep ask = (Step.AskStep) step;
        Asked asked = questionOf(jeff, call);
        long started = System.nanoTime();
        String answer = ask.ask().answer(asked.question());
        double elapsed = askMs(System.nanoTime() - started);
        if (answer == null || !asked.keys().contains(answer)) {
            throw JeffError.escalationFailed(ask.name(), "answered " + PyFormat.repr(answer) + ", which is not one of "
                + PyFormat.repr(asked.keys()));
        }
        Map<String, Object> timing = new LinkedHashMap<>();
        timing.put("path", ask.name());
        timing.put("ms", elapsed);
        FunctionSpec fn = jeff.catalogue().function(call.function());
        return original.answeredBy(ask.name(), converted(fn.type(), answer), timing);
    }

    static Decision applyPolicy(Jeff jeff, Call call, Decision decision, Policy policy) {
        if (!decision.abstained() || Policy.RETURN.equals(policy)) return decision;
        if (Policy.ERROR.equals(policy)) throw JeffError.lowConfidence(decision);
        if (!(policy instanceof Escalate escalate)) throw unknownPolicy(policy);
        long started = System.nanoTime();
        List<Decision.EscalationRecord> attempts = new ArrayList<>(List.of(record(jeff, decision)));
        Decision latest = decision;
        for (Step step : escalate.steps()) {
            try {
                latest = runStep(jeff, call, decision, step);
            } catch (JeffError.EscalationFailed error) {
                throw error;
            } catch (Exception error) {
                // The caller's own cancellation (this thread interrupted) is not a failing step: it passes through
                // unwrapped, as Python's CancelledError does. A step's own timeout is a failing step.
                if (Thread.currentThread().isInterrupted()) throw error;
                JeffError.EscalationFailed failed = JeffError.escalationFailed(step.name(), JeffError.describe(error));
                failed.initCause(error);
                throw failed;
            }
            if (escalate.budgetMs() != null && (System.nanoTime() - started) / 1e6 > escalate.budgetMs().doubleValue()) {
                throw new JeffError.EscalationBudgetExceeded("Escalation of " + decision.function() + " took longer than "
                    + PyFormat.repr(escalate.budgetMs()) + " ms");
            }
            if (!latest.abstained()) return latest.withRouteAndEscalations(decision.route(), attempts);
            attempts.add(record(jeff, latest));
        }
        return latest.withRouteAndEscalations(decision.route(), attempts.subList(0, attempts.size() - 1));
    }
}
