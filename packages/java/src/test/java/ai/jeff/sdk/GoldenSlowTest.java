package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.IOException;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;

/** contract/golden/step1.json against a real engine: a {@code jeff runtime serve} already running, whose socket
 * JEFF_TEST_SOCKET names. It needs the weights of the base the shipped manifests name (v1.4 is only on the RTX), so
 * without JEFF_TEST_SOCKET the test is skipped, saying so. With it, every golden case whose adapter that engine serves
 * is decided in one batch per adapter and compared as Python's golden_problems compares it; the cases of adapters it
 * does not serve are listed on the output, and an engine serving none of them fails the test. */
class GoldenSlowTest {
    static final String SOCKET_VARIABLE = "JEFF_TEST_SOCKET";

    @Test
    void theGoldenCasesReproduceAgainstARealEngine() throws IOException {
        String socket = System.getenv(SOCKET_VARIABLE);
        assumeTrue(socket != null && !socket.isEmpty(), "Skipped: the golden cases need a running engine with real weights; set "
            + SOCKET_VARIABLE + " to the socket of a running `jeff runtime serve` to run them");
        assertEquals(List.of(), problems(socket));
    }

    /** The check itself, against a fake engine that answers every row with even odds: it decides the served adapter's
     * cases and reports how each differs, so a real engine that does not reproduce them fails the test above. */
    @Test
    void theCheckReportsEveryCaseAnEngineDoesNotReproduce() throws IOException {
        try (EngineServer server = EngineServer.start((adapter, row) -> evenOdds(row), List.of("jeff.safety.spam"))) {
            assertEquals(List.of(
                "jeff.safety.spam.is_spam (example.json): confidence 0.5, expected 0.9992507953683402 +/- 0.03",
                "jeff.safety.spam.is_spam (example.json): abstained True, expected False (the recorded confidence is more "
                    + "than 0.03 from the threshold 0.515807)",
                "jeff.safety.spam.message_type (example.json): value 'legitimate', expected 'phishing'",
                "jeff.safety.spam.message_type (example.json): confidence 0.3333333333333333, expected 0.9678571120548944 "
                    + "+/- 0.03",
                "jeff.safety.spam.message_type (example.json): abstained True, expected False (the recorded confidence is "
                    + "more than 0.03 from the threshold 0.425658)"), problems(server.path().toString()));
        }
    }

    /** The same probability for every option of the row's question (its criteria: a map of options or a list of
     * levels), the row as the engine socket carries it. */
    private static double[] evenOdds(Map<String, Object> row) {
        Object criteria = Values.map(row.get("question")).get("criteria");
        int options = criteria instanceof Map<?, ?> map ? map.size() : ((List<?>) criteria).size();
        double[] probabilities = new double[options];
        java.util.Arrays.fill(probabilities, 1.0 / options);
        return probabilities;
    }

    /** Every golden case whose adapter the engine at {@code socket} serves, decided in one batch per adapter: each
     * difference from its case as "case name: problem". */
    static List<String> problems(String socket) throws IOException {
        Map<String, Object> golden = Values.map(Json.parse(Files.readString(Shipped.repositoryRoot().resolve("contract/golden/step1.json"))));
        List<String> found = new ArrayList<>();
        try (Jeff jeff = Jeff.local(socket, LocalOptions.timeoutMs(600000))) {
            Set<String> served = jeff.engine().installed(true);
            Map<String, List<Map<String, Object>>> byAdapter = new LinkedHashMap<>();
            List<String> notServed = new ArrayList<>();
            for (Object item : Values.list(golden.get("cases"))) {
                Map<String, Object> goldenCase = Values.map(item);
                String function = (String) Values.map(goldenCase.get("call")).get("function");
                String adapter = jeff.catalogue().function(function).adapterId();
                if (served.contains(adapter)) byAdapter.computeIfAbsent(adapter, key -> new ArrayList<>()).add(goldenCase);
                else if (!notServed.contains(adapter)) notServed.add(adapter);
            }
            System.out.println("Golden cases checked for " + String.join(", ", byAdapter.keySet()) + "; not served by the engine at "
                + socket + ", so not checked: " + (notServed.isEmpty() ? "none" : String.join(", ", notServed)));
            if (byAdapter.isEmpty()) {
                throw new AssertionError("The engine at " + socket + " serves none of the adapters contract/golden/step1.json has "
                    + "cases for (" + String.join(", ", notServed) + ")");
            }
            for (List<Map<String, Object>> cases : byAdapter.values()) {
                List<Call> calls = new ArrayList<>();
                for (Map<String, Object> goldenCase : cases) {
                    Map<String, Object> call = Values.map(goldenCase.get("call"));
                    Map<String, Object> context = call.get("context") == null ? null : Values.map(call.get("context"));
                    calls.add(new Call((String) call.get("function"), call.get("input"), context, call.get("labels")));
                }
                List<Decision> decisions = jeff.decide(calls);
                for (int index = 0; index < cases.size(); index++) {
                    Calibration calibration = jeff.calibrationOf(jeff.catalogue().function(calls.get(index).function()));
                    for (String problem : Golden.goldenProblems(cases.get(index), decisions.get(index),
                        calibration == null ? null : calibration.threshold())) {
                        found.add(cases.get(index).get("name") + ": " + problem);
                    }
                }
            }
        }
        return found;
    }
}
