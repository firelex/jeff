package ai.jeff.sdk;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Stream;
import org.junit.jupiter.params.provider.Arguments;

/** The files of contract/conformance/ (read once) and what every replay does with them. */
final class Fixtures {
    static final String ELAPSED = "<elapsed>";
    private static final Map<String, Map<String, Object>> LOADED = new ConcurrentHashMap<>();
    private static final Map<String, Catalogue> CATALOGUES = new ConcurrentHashMap<>();

    private Fixtures() {}

    static Map<String, Object> load(String file) {
        return LOADED.computeIfAbsent(file, name -> {
            try {
                return Values.map(Json.parse(Files.readString(Shipped.repositoryRoot().resolve("contract/conformance/" + name))));
            } catch (IOException error) {
                throw new UncheckedIOException(error);
            }
        });
    }

    static List<Map<String, Object>> cases(String file) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object item : Values.list(load(file).get("cases"))) out.add(Values.map(item));
        return out;
    }

    /** One JUnit argument set per case: its name (the display name) and the case. */
    static Stream<Arguments> named(List<Map<String, Object>> cases) {
        return cases.stream().map(item -> Arguments.of(item.get("name"), item));
    }

    /** A case's catalogue: the shipped one, or the shipped one with a variant's manifests in place (catalogues.json). */
    static Catalogue catalogueFor(String name) {
        return CATALOGUES.computeIfAbsent(name, key -> {
            if (key.equals("shipped")) return Catalogue.shipped();
            Object variant = Values.map(load("catalogues.json").get("variants")).get(key);
            if (variant == null) throw new IllegalArgumentException("catalogues.json has no variant " + key);
            List<Manifest> manifests = new ArrayList<>();
            for (Object raw : Values.list(variant)) {
                manifests.add(Manifests.fromMap(Values.plain(raw), "conformance/" + key + "/" + Values.map(raw).get("id")));
            }
            return Catalogue.shipped().replacing(manifests);
        });
    }

    /** A thrown error as the fixtures write it; anything but a JeffError fails the test with the error itself. */
    static Map<String, Object> wireOf(Throwable error) {
        if (!(error instanceof JeffError jeff)) throw new AssertionError("expected a JeffError, got " + error, error);
        Map<String, Object> details = new LinkedHashMap<>(jeff.details());
        details.remove("request_id");
        Map<String, Object> wire = new LinkedHashMap<>();
        wire.put("type", jeff.type());
        wire.put("message", jeff.getMessage());
        wire.put("details", details);
        return Values.map(normalised(wire));
    }

    /** The value with timing.ms of every decision an llm or human step made written ELAPSED, as the recorder writes it. */
    static Object normalised(Object value) {
        if (value instanceof List<?> list) {
            List<Object> out = new ArrayList<>();
            for (Object item : list) out.add(normalised(item));
            return out;
        }
        if (!(value instanceof Map<?, ?> map)) return value;
        Map<String, Object> out = new LinkedHashMap<>();
        for (Map.Entry<?, ?> entry : map.entrySet()) out.put((String) entry.getKey(), normalised(entry.getValue()));
        Object decidedBy = out.get("decided_by");
        if (("llm".equals(decidedBy) || "human".equals(decidedBy)) && out.get("timing") instanceof Map<?, ?> timing
            && timing.containsKey("ms")) {
            Map<String, Object> replaced = new LinkedHashMap<>(Values.map(timing));
            replaced.put("ms", ELAPSED);
            out.put("timing", replaced);
        }
        return out;
    }

    static String base64(byte[] bytes) {
        return Base64.getEncoder().encodeToString(bytes);
    }

    static byte[] fromBase64(String text) {
        return Base64.getDecoder().decode(text);
    }

    static Map<String, Object> ordered(Object... keysAndValues) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (int index = 0; index < keysAndValues.length; index += 2) out.put((String) keysAndValues[index], keysAndValues[index + 1]);
        return out;
    }
}
