package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;

/** contract/conformance/manifests.json: manifests every SDK refuses, read from their YAML text as a manifest file is
 * read (Yaml.parse, then Manifests.fromMap), each refused with the Python SDK's error. */
class ManifestsConformanceTest {
    static Stream<Arguments> cases() {
        return Fixtures.named(Fixtures.cases("manifests.json"));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    void eachManifestIsRefusedWithThePythonSdksError(String name, Map<String, Object> item) {
        String path = (String) item.get("path");
        Map<String, Object> expected = Values.map(Values.map(item.get("expect")).get("error"));
        JeffError error = assertThrows(JeffError.class, () -> Manifests.fromMap(Yaml.parse((String) item.get("yaml"), path), path));
        assertEquals(List.of(expected.get("type"), expected.get("message")), List.of(error.type(), error.getMessage()));
    }

    @Test
    void theFixtureHasTheNonFiniteCasesTheRefusedAliasesAndTheRefusedAccesses() {
        assertEquals(16, Fixtures.cases("manifests.json").size());
    }

    /** access is for a server only: the SDK accepts it and does nothing with it. */
    @ParameterizedTest(name = "{0}")
    @ValueSource(strings = {"access: owner\n", "access: {accounts: [acct1, B-2_x]}\n"})
    void aManifestWithAValidAccessIsRead(String access) {
        Map<String, Object> refused = Fixtures.cases("manifests.json").stream()
            .filter(item -> "an access that is a string other than owner".equals(item.get("name"))).findFirst().orElseThrow();
        String yaml = ((String) refused.get("yaml")).replace("access: everyone\n", access);
        assertEquals("jeff.safety.spam", Manifests.fromMap(Yaml.parse(yaml, "m.yaml"), "m.yaml").id());
    }

    @Test
    void aNumberThatIsNotFiniteIsRefusedWhereverItIs() {
        Map<String, Object> manifest = Values.map(Values.plain(Map.of("changelog", List.of(Map.of("note", Double.NEGATIVE_INFINITY)))));
        assertEquals("x: /changelog/0/note is -inf, not a finite number, which JSON cannot hold; write a finite number",
            assertThrows(JeffError.ManifestError.class, () -> Manifests.fromMap(manifest, "x")).getMessage());
    }
}
