package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class DecisionsCoverageTest {
    @Test
    void everyGroupOfDecisionsJsonIsReplayed() {
        List<String> groups = Fixtures.cases("decisions.json").stream().map(item -> (String) item.get("group")).distinct().toList();
        assertEquals(List.of("decide", "dispatch", "escalation", "graph"), PyFormat.sorted(groups));
        assertEquals(555, Fixtures.cases("decisions.json").size());
        for (Map<String, Object> item : Fixtures.cases("decisions.json")) {
            assertEquals(true, List.of("decide", "function", "shorthand", "dispatch", "graph").contains(item.get("op")), (String) item.get("name"));
        }
    }
}
