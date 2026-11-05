package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/** rows.json: the exact engine row the Python SDK builds for each call (bytes included), or its exact error. */
class RowsConformanceTest {
    static Stream<Arguments> cases() {
        return Fixtures.named(Fixtures.cases("rows.json"));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    void theRowIsThePythonSdksRow(String name, Map<String, Object> item) {
        Catalogue catalogue = Fixtures.catalogueFor((String) item.get("catalogue"));
        Map<String, Object> call = Values.map(item.get("call"));
        Map<String, Object> got = new LinkedHashMap<>();
        try {
            FunctionSpec fn = catalogue.function((String) call.get("function"));
            Manifest manifest = catalogue.manifestOf(fn);
            Calibration calibration = Manifests.calibrationFor(fn,
                BaseFormat.formatIdOf(BaseFormat.fromWire(Values.map(item.get("base_format")))));
            Row row = Binding.buildRow(Binding.bind(manifest, fn, Values.map(call.get("context")), call.get("labels"), calibration),
                call.get("input"));
            got.put("adapter", manifest.id());
            got.put("row", row.toWire());
            got.put("row_msgpack", Fixtures.base64(MsgPack.encode(row.toWire())));
        } catch (RuntimeException error) {
            got.put("error", Fixtures.wireOf(error));
        }
        assertEquals(item.get("expect"), got);
    }
}
