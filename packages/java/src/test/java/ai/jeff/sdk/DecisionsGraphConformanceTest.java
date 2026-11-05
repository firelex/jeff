package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/** decisions.json's graph group, replayed through Jeff.runGraph over the scripted engine. */
class DecisionsGraphConformanceTest {
    static Stream<Arguments> cases() {
        return Fixtures.named(Fixtures.cases("decisions.json").stream().filter(item -> item.get("group").equals("graph")).toList());
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    void theGraphRunsAsInThePythonSdk(String name, Map<String, Object> item) {
        DecisionReplay.Replayed replayed = DecisionReplay.replay(item);
        assertEquals(item.get("expect"), replayed.got());
        assertEquals(List.of(), replayed.engine().mismatches());
        assertEquals(0, replayed.engine().remaining());
        assertEquals(item.get("questions"), replayed.questions());
        assertEquals(List.of(), replayed.entryOrder());
    }
}
