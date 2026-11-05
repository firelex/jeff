package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class YamlTest {
    private static Object value(String text) {
        return ((Map<?, ?>) Yaml.parse("d: " + text + "\n", "test.yaml")).get("d");
    }

    private static String refusal(String text) {
        return assertThrows(JeffError.ManifestError.class, () -> Yaml.parse("d: " + text + "\n", "test.yaml")).getMessage();
    }

    @Test
    void anUnquotedDateIsItsOwnTextNotADate() {
        assertEquals("2026-10-03", value("2026-10-03"));
        assertEquals("2026-10-03", value("'2026-10-03'"));
        assertEquals("0099-01-01", value("0099-01-01"));
    }

    @Test
    void aDatetimeOrADayNotOnTheCalendarIsRefused() {
        String timePart = refusal("2026-10-03T10:11:12Z");
        assertTrue(timePart.startsWith("test.yaml is not valid YAML: "), timePart);
        assertTrue(timePart.contains("'2026-10-03T10:11:12Z' is a YAML timestamp with a time part"), timePart);
        assertTrue(refusal("2026-02-30").contains("'2026-02-30' is not a real calendar date"));
        assertTrue(refusal("0000-01-01").contains("'0000-01-01' is not a real calendar date"));
    }

    @Test
    void scalarsResolveAsPyYamlResolvesThem() {
        assertEquals(true, value("yes"));
        assertEquals(false, value("Off"));
        assertEquals("1e5", value("1e5"));
        assertEquals(100000.0, value("1.0e+5"));
        assertEquals(1.5, value("1.5"));
        assertEquals(15L, value("017"));
        assertEquals(31L, value("0x1F"));
        assertEquals(1000L, value("1_000"));
        assertEquals(80L, value("1:20"));
        assertEquals("0o17", value("0o17"));
        assertEquals(Double.POSITIVE_INFINITY, value(".inf"));
        assertNull(value("~"));
        assertNull(value(""));
        assertInstanceOf(Long.class, value("3"));
    }

    @Test
    void aRepeatedKeyKeepsItsFirstPlaceAndItsLastValue() {
        Map<?, ?> parsed = (Map<?, ?>) Yaml.parse("a: 1\nb: 2\na: 3\n", "test.yaml");
        assertEquals(List.of("a", "b"), List.copyOf(parsed.keySet()));
        assertEquals(3L, parsed.get("a"));
    }

    @Test
    void mappingsKeepDocumentOrderEvenForNumberLikeQuotedKeys() {
        Map<?, ?> parsed = (Map<?, ?>) Yaml.parse("fixed:\n  '2': Two\n  '1': One\n  legit: Fine\n", "test.yaml");
        assertEquals(List.of("2", "1", "legit"), List.copyOf(((Map<?, ?>) parsed.get("fixed")).keySet()));
    }

    @Test
    void aKeyThatIsNotAStringIsRefused() {
        JeffError.ManifestError error = assertThrows(JeffError.ManifestError.class,
            () -> Yaml.parse("nodes:\n  10: {code: ten}\n", "graph.yaml"));
        assertEquals("graph.yaml: the mapping key 10 is not a string; quote it", error.getMessage());
    }

    @Test
    void anEqualsSignAloneIsRefusedAsPyYamlRefusesIt() {
        assertTrue(refusal("=").contains("could not determine a constructor for the tag tag:yaml.org,2002:value"));
    }

    /** A flow sequence with `levels` opening brackets around a scalar: `levels` levels of list nesting, as the
     * document root (no surrounding mapping, so the depth counted is exactly `levels`). */
    private static String nested(int levels) {
        return "[".repeat(levels).concat("0").concat("]".repeat(levels));
    }

    @Test
    void nestingOfExactly512LevelsIsRead() {
        Object cursor = Yaml.parse(nested(512), "doc.yaml");
        for (int depth = 0; depth < 512; depth++) {
            cursor = ((List<?>) cursor).get(0);
        }
        assertEquals(0L, cursor);
    }

    @Test
    void nestingOfMoreThan512LevelsIsRefusedByNameInsteadOfOverflowingTheStack() {
        JeffError.ManifestError error = assertThrows(JeffError.ManifestError.class, () -> Yaml.parse(nested(513), "doc.yaml"));
        assertTrue(error.getMessage().contains("512"), error.getMessage());
    }

    @Test
    void readParsesAnActualFileTheSameWayAsParse(@TempDir Path dir) throws IOException {
        Path file = dir.resolve("manifest.yaml");
        Files.writeString(file, "d: yes\n");
        assertEquals(Map.of("d", true), Yaml.read(file));
    }

    @Test
    void readOnAMissingFileNamesThePath(@TempDir Path dir) {
        Path file = dir.resolve("missing.yaml");
        UncheckedIOException error = assertThrows(UncheckedIOException.class, () -> Yaml.read(file));
        assertTrue(error.getMessage().contains(file.toString()), error.getMessage());
    }

    @Test
    void aMergeKeyMergesTheAnchoredMappingAndAnOverrideWins() {
        Map<?, ?> parsed = (Map<?, ?>) Yaml.parse("base: &b\n  x: 1\n  y: 2\nchild:\n  <<: *b\n  y: 3\n", "test.yaml");
        assertEquals(Map.of("x", 1L, "y", 3L), parsed.get("child"));
    }

    /** The final review's files: each level names the previous one two (or three) times, so a few hundred bytes stand
     * for millions of values once every alias is copied out. */
    private static String aliasLevels(int levels, int width) {
        StringBuilder text = new StringBuilder("a0: &a0 [" + String.join(", ", java.util.Collections.nCopies(width, "x")) + "]\n");
        for (int i = 1; i <= levels; i++) {
            text.append("a").append(i).append(": &a").append(i).append(" [")
                .append(String.join(", ", java.util.Collections.nCopies(width, "*a" + (i - 1)))).append("]\n");
        }
        return text.toString();
    }

    @Test
    void aliasesThatExpandPastAMillionValuesAreRefusedByName() {
        String two = aliasLevels(24, 2);
        String three = aliasLevels(15, 3);
        assertEquals(529, two.length());
        assertEquals(405, three.length());
        for (String text : List.of(two, three)) {
            assertEquals("bomb.yaml: the aliases expand to more than 1,000,000 values; write the repeated parts out or use fewer aliases",
                assertThrows(JeffError.ManifestError.class, () -> Yaml.parse(text, "bomb.yaml")).getMessage());
        }
    }

    @Test
    void aManifestWhoseAliasesExpandTooFarIsRefusedBeforeItsSchemaCheck(@TempDir Path dir) throws IOException {
        Path file = dir.resolve("manifest.yaml");
        Files.writeString(file, aliasLevels(24, 2));
        assertEquals(file + ": the aliases expand to more than 1,000,000 values; write the repeated parts out or use fewer aliases",
            assertThrows(JeffError.ManifestError.class, () -> Manifests.load(file)).getMessage());
    }

    @Test
    void theLimitCountsTheValuesTheAliasesAdd() {
        // a list of 999 items is 1,000 values; 1,000 aliases to it add exactly 1,000,000, one more is past the limit
        String base = "base: &b [" + String.join(", ", java.util.Collections.nCopies(999, "0")) + "]\n";
        String atLimit = base + "copies: [" + String.join(", ", java.util.Collections.nCopies(1000, "*b")) + "]\n";
        String pastLimit = base + "copies: [" + String.join(", ", java.util.Collections.nCopies(1001, "*b")) + "]\n";
        assertEquals(1000, ((List<?>) ((Map<?, ?>) Yaml.parse(atLimit, "at.yaml")).get("copies")).size());
        assertEquals("past.yaml: the aliases expand to more than 1,000,000 values; write the repeated parts out or use fewer aliases",
            assertThrows(JeffError.ManifestError.class, () -> Yaml.parse(pastLimit, "past.yaml")).getMessage());
    }

    /** Mapping keys are not values, as the Rust and C# SDKs count: a mapping of 999 keys is 1,000 values, so 1,000
     * aliases to it add exactly 1,000,000 (counting its keys too would make it 1,999 and refuse it at the 501st). */
    @Test
    void mappingKeysAreNotCountedAsValues() {
        StringBuilder base = new StringBuilder("base: &b {");
        for (int i = 0; i < 999; i++) base.append(i == 0 ? "" : ", ").append("k").append(i).append(": 0");
        base.append("}\n");
        String atLimit = base + "copies: [" + String.join(", ", java.util.Collections.nCopies(1000, "*b")) + "]\n";
        String pastLimit = base + "copies: [" + String.join(", ", java.util.Collections.nCopies(1001, "*b")) + "]\n";
        assertEquals(1000, ((List<?>) ((Map<?, ?>) Yaml.parse(atLimit, "at.yaml")).get("copies")).size());
        assertEquals("past.yaml: the aliases expand to more than 1,000,000 values; write the repeated parts out or use fewer aliases",
            assertThrows(JeffError.ManifestError.class, () -> Yaml.parse(pastLimit, "past.yaml")).getMessage());
    }

    /** Rust's figure (packages/rust/src/yaml.rs's test): 5 levels of 10 aliases add 123,440 values. With 876 aliases to
     * a list of 1,000 values and one to a list of 560, the document adds exactly 1,000,000 and is read; one alias more,
     * to a single value, is past the limit. */
    @Test
    void theLimitAgreesWithTheRustCountAtExactlyAMillion() {
        String levels = aliasLevels(4, 10);
        String fill = "f: &f [" + String.join(", ", java.util.Collections.nCopies(999, "0")) + "]\n"
            + "s: &s [" + String.join(", ", java.util.Collections.nCopies(559, "0")) + "]\n"
            + "one: &one 1\n"
            + "copies: [" + String.join(", ", java.util.Collections.nCopies(876, "*f")) + ", *s]\n";
        String atLimit = levels + fill;
        Map<?, ?> read = (Map<?, ?>) Yaml.parse(atLimit, "at.yaml");
        assertEquals(877, ((List<?>) read.get("copies")).size());
        String pastLimit = atLimit + "more: *one\n";
        assertEquals("past.yaml: the aliases expand to more than 1,000,000 values; write the repeated parts out or use fewer aliases",
            assertThrows(JeffError.ManifestError.class, () -> Yaml.parse(pastLimit, "past.yaml")).getMessage());
        // the levels alone are 123,440: with 877 lists of 1,000 the count passes the limit by 440
        String overByLevels = levels + "f: &f [" + String.join(", ", java.util.Collections.nCopies(999, "0")) + "]\n"
            + "copies: [" + String.join(", ", java.util.Collections.nCopies(877, "*f")) + "]\n";
        assertThrows(JeffError.ManifestError.class, () -> Yaml.parse(overByLevels, "over.yaml"));
    }

    /** SnakeYAML's own guard refused more than 50 aliases to collections; PyYAML reads this file (61 keys, 60 of them
     * aliases of one small mapping: uv run --with pyyaml python -c "import yaml; print(len(yaml.safe_load(open(f))))"
     * prints 61). */
    @Test
    void manyAliasesOfASmallMappingAreReadAsPyYamlReadsThem() {
        StringBuilder text = new StringBuilder("base: &b {x: 1}\n");
        for (int i = 0; i < 60; i++) text.append("k").append(i).append(": *b\n");
        Map<?, ?> read = (Map<?, ?>) Yaml.parse(text.toString(), "many.yaml");
        assertEquals(61, read.size());
        assertEquals(Map.of("x", 1L), read.get("k59"));
    }

    @Test
    void anAliasInsideTheCollectionItNamesIsRefusedByName() {
        assertEquals("loop.yaml: an alias at line 1 names a collection it is inside, which has no JSON form; write the value out",
            assertThrows(JeffError.ManifestError.class, () -> Yaml.parse("a: &a [1, *a]\n", "loop.yaml")).getMessage());
    }
}
