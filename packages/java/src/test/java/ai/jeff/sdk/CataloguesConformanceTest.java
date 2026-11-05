package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/** catalogues.json: the shipped manifests read as the Python SDK reads them, and every variant loads. */
class CataloguesConformanceTest {
    @Test
    void theShippedManifestsReadAsThePythonSdkReadsThem() {
        Map<String, Object> shipped = Values.map(Fixtures.load("catalogues.json").get("shipped"));
        Map<String, Object> manifests = Values.map(shipped.get("manifests"));
        Catalogue catalogue = Fixtures.catalogueFor("shipped");
        assertEquals(PyFormat.sorted(manifests.keySet()), PyFormat.sorted(catalogue.manifests().stream().map(Manifest::id).toList()));
        for (Manifest manifest : catalogue.manifests()) assertEquals(manifests.get(manifest.id()), manifest.raw(), manifest.id());
        assertEquals(shipped.get("bases"), Yaml.parse(Shipped.text("catalogue/bases.yaml"), "bases.yaml"));
    }

    static Stream<Arguments> variants() {
        return Values.map(Fixtures.load("catalogues.json").get("variants")).entrySet().stream()
            .map(entry -> Arguments.of(entry.getKey(), entry.getValue()));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("variants")
    void aVariantLoads(String name, List<Object> raws) {
        Catalogue catalogue = Fixtures.catalogueFor(name);
        for (Object raw : raws) assertEquals(raw, catalogue.adapter((String) Values.map(raw).get("id")).raw());
    }
}
