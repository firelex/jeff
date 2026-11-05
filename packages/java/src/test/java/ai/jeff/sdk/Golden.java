package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** The golden comparison (port of jeff.evaluate.same and golden_problems) for contract/golden/*.json cases: a helper of
 * this package's tests (GoldenSlowTest), not of the SDK, since the golden cases are not shipped with it. */
final class Golden {
    private Golden() {}

    /** Equal and of the same Python type, so that true does not match 1 (Python's type(value) is type(expected)
     * and value == expected). */
    static boolean same(Object value, Object expected) {
        return PyFormat.typeName(value).equals(PyFormat.typeName(expected)) && PyFormat.pyEquals(value, expected);
    }

    private static double number(Object value, String what) {
        if (value instanceof Number number && !(value instanceof Boolean)) return number.doubleValue();
        throw new IllegalArgumentException("The golden case's " + what + " is not a number (got " + PyFormat.repr(value) + ")");
    }

    /** How a decision differs from a golden case; empty when it reproduces it. `value` must be the same, `confidence`
     * within the case's tolerance, and `abstained` must match except when the recorded confidence is within the
     * tolerance of the threshold. Numbers are written as Python's str() writes them (an int case value as 1, a
     * float as 1.0). */
    static List<String> goldenProblems(Map<String, ?> goldenCase, Decision decision, Double threshold) {
        if (!(goldenCase.get("expected") instanceof Map<?, ?> expected)) {
            throw new IllegalArgumentException("The golden case has no 'expected' object (got "
                + PyFormat.repr(goldenCase.get("expected")) + ")");
        }
        Object toleranceGiven = goldenCase.get("tolerance");
        Object confidenceGiven = expected.get("confidence");
        double tolerance = number(toleranceGiven, "tolerance");
        double expectedConfidence = number(confidenceGiven, "expected confidence");
        List<String> problems = new ArrayList<>();
        if (!same(decision.value(), expected.get("value"))) {
            problems.add("value " + PyFormat.repr(decision.value()) + ", expected " + PyFormat.repr(expected.get("value")));
        }
        if (decision.confidence() == null) problems.add("no confidence");
        else if (Math.abs(decision.confidence() - expectedConfidence) > tolerance) {
            problems.add("confidence " + PyFormat.floatRepr(decision.confidence()) + ", expected "
                + PyFormat.toStr(confidenceGiven) + " +/- " + PyFormat.toStr(toleranceGiven));
        }
        boolean nearThreshold = threshold != null && Math.abs(expectedConfidence - threshold) <= tolerance;
        if (!PyFormat.pyEquals(decision.abstained(), expected.get("abstained")) && !nearThreshold) {
            problems.add("abstained " + PyFormat.repr(decision.abstained()) + ", expected " + PyFormat.toStr(expected.get("abstained"))
                + " (the recorded confidence is more than " + PyFormat.toStr(toleranceGiven) + " from the threshold "
                + (threshold == null ? "None" : PyFormat.floatRepr(threshold)) + ")");
        }
        return problems;
    }
}
