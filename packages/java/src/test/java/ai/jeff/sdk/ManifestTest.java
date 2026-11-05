package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

class ManifestTest {
    private static Map<String, Object> spamRaw() {
        return Values.map(Values.plain(Catalogue.shipped().adapter("jeff.safety.spam").raw()));
    }

    @ParameterizedTest
    @CsvSource(delimiter = '|', value = {
        "channel|channel", "counterparty name|counterparty_name", "Deal ID|deal_id", "outreach  log -- v2|outreach_log_v2",
        "_odd_|odd", "clause type?|clause_type", "company_|company_", "a__b|a__b", "x9_|x9_", "'abc\n'|abc"})
    void theCodeNameOfAKeyFollowsThePythonRule(String key, String code) {
        assertEquals(code, Manifests.codeNameOf(key));
    }

    @Test
    void referencesParseAsInPython() {
        assertEquals(new FunctionRef("jeff.safety.spam", "is_spam", "1.3"), FunctionRef.parse("jeff.safety.spam.is_spam@1.3"));
        assertEquals(new FunctionRef("spam", null, null), FunctionRef.parse("spam"));
        assertEquals("Malformed reference 'jeff.x': use jeff.<domain>.<name>[.<function>]",
            assertThrows(JeffError.InvalidReference.class, () -> FunctionRef.parse("jeff.x")).getMessage());
        assertEquals("Malformed reference 'a..b': use jeff.<domain>.<name>[.<function>] or <alias>[.<function>], each part "
            + "non-empty", assertThrows(JeffError.InvalidReference.class, () -> FunctionRef.parse("a..b")).getMessage());
        assertTrue(FunctionRef.versionMatches("1.3", "1.3.0"));
        assertFalse(FunctionRef.versionMatches("1.30", "1.3.0"));
    }

    @Test
    void aNumberLikeFixedLabelKeyKeepsItsDocumentOrder() {
        Map<String, Object> raw = spamRaw();
        Map<String, Object> fixed = new LinkedHashMap<>();
        fixed.put("2", "Two");
        fixed.put("1", "One");
        fixed.put("legit", "Fine");
        Values.map(Values.map(Values.map(raw.get("functions")).get("message_type")).get("labels")).put("fixed", fixed);
        Manifest manifest = Manifests.fromMap(raw, "test");
        assertEquals(List.of("2", "1", "legit"), List.copyOf(manifest.functions().get("message_type").labels().fixed().keySet()));
    }

    @Test
    void aKeyWithNoUsableCodeNameMustDeclareOne() {
        Map<String, Object> raw = spamRaw();
        raw.put("context", List.of(Fixtures.ordered("key", "2nd party", "description", "A field.", "required", true)));
        String message = assertThrows(JeffError.ManifestError.class, () -> Manifests.fromMap(raw, "test")).getMessage();
        assertTrue(message.contains("'2nd party'") && message.contains("declare its code_name"), message);
    }

    @Test
    void theCatalogueResolvesAliasesKeysAndVersions() {
        Catalogue catalogue = Catalogue.shipped();
        assertEquals("jeff.safety.spam.is_spam", catalogue.function("spam.is_spam").id());
        assertThrows(JeffError.UnknownFunction.class, () -> catalogue.function("spam"));
        assertEquals(List.of("jeff.compliance.sanctions.screening"),
            catalogue.implementing("equivalent").stream().map(FunctionSpec::id).toList());
        assertTrue(Manifests.calibrationFor(catalogue.function("spam.is_spam"), null).threshold() > 0);
        String wrongVersion = assertThrows(JeffError.UnknownFunction.class, () -> catalogue.adapter("jeff.safety.spam@9")).getMessage();
        assertTrue(wrongVersion.startsWith("jeff.safety.spam@9 is not in the catalogue; it has "), wrongVersion);
    }

    @Test
    void idPatternsMustEachBeARegularExpression() {
        Map<String, Object> raw = spamRaw();
        Map<String, Object> tests = Values.map(raw.get("tests"));
        tests.put("function_by", "id_pattern");
        tests.put("id_patterns", Fixtures.ordered("is_spam", "[", "message_type", ".*"));
        String message = assertThrows(JeffError.ManifestError.class, () -> Manifests.fromMap(raw, "test")).getMessage();
        assertTrue(message.contains("id_patterns is_spam: '[' is not a regular expression"), message);
    }

    @Test
    void baseLooksUpAVersionAndRefusesOneBasesYamlDoesNotHave() {
        Catalogue catalogue = Catalogue.shipped();
        assertEquals("mstrasser/jeff-base", catalogue.base("jeff-base", "v1.4").get("repo"));
        assertThrows(JeffError.ManifestError.class, () -> catalogue.base("jeff-base", "v9"));
        assertThrows(JeffError.ManifestError.class, () -> catalogue.base("no-such-base", "v1.3"));
    }

    @Test
    void anIntFourBaseWithoutItsCalibrationIsRefusedNamingTheCommand() {
        FunctionSpec fn = Catalogue.shipped().function("spam.message_type");
        String message = assertThrows(JeffError.ManifestError.class,
            () -> Manifests.calibrationFor(fn, "jeff-base@v1.3+int4-g64-gptq.c0ffee00")).getMessage();
        assertTrue(message.startsWith("jeff.safety.spam.message_type has no calibration for the INT4 base this engine "
            + "serves (jeff-base@v1.3+int4-g64-gptq.c0ffee00); it is calibrated on the bfloat16 base and on no INT4 base."), message);
    }

    @Test
    void idPatternsAreCheckedWithPythonsRulesNotJavas() {
        Map<String, Object> raw = spamRaw();
        Map<String, Object> tests = Values.map(raw.get("tests"));
        tests.put("function_by", "id_pattern");
        // Python accepts both (Java's Pattern refuses a named group written (?P<n>...) and a{,3})
        tests.put("id_patterns", Fixtures.ordered("is_spam", "(?P<n>x)a{,3}", "message_type", ".*"));
        assertEquals("jeff.safety.spam", Manifests.fromMap(raw, "test").id());
        // Python refuses it (Java's Pattern accepts \p{L})
        tests.put("id_patterns", Fixtures.ordered("is_spam", "\\p{L}", "message_type", ".*"));
        assertEquals("test: tests.id_patterns is_spam: '\\\\p{L}' is not a regular expression (bad escape \\p at position 0)",
            assertThrows(JeffError.ManifestError.class, () -> Manifests.fromMap(raw, "test")).getMessage());
        tests.put("id_patterns", Fixtures.ordered("is_spam", "(?<=a)b", "message_type", ".*"));
        assertEquals("test: tests.id_patterns is_spam: '(?<=a)b' uses a look-behind (?<=...), which the Java SDK does not "
            + "check as Python's re module would; write the pattern without it",
            assertThrows(JeffError.ManifestError.class, () -> Manifests.fromMap(raw, "test")).getMessage());
    }

    @Test
    void aMissingOrMalformedFunctionByIsRefused() {
        Map<String, Object> raw = spamRaw();
        Values.map(raw.get("tests")).remove("function_by");
        String missing = assertThrows(JeffError.ContractError.class, () -> Manifests.fromMap(raw, "test")).getMessage();
        assertTrue(missing.contains("'function_by' is a required property"), missing);
        Values.map(raw.get("tests")).put("function_by", List.of("only"));
        String malformed = assertThrows(JeffError.ContractError.class, () -> Manifests.fromMap(raw, "test")).getMessage();
        assertTrue(malformed.contains("at /tests/function_by: ['only'] is not valid under any of the given schemas"), malformed);
    }

    @Test
    void anAdapterWithNoFunctionsIsNamedNotCrashedOn() {
        Manifest spam = Catalogue.shipped().adapter("jeff.safety.spam");
        Manifest empty = new Manifest("jeff.test.empty", "1.0.0", List.of(), spam.licenceKind(), spam.weights(),
            spam.baseId(), spam.baseVersion(), spam.maxOptions(), spam.context(), spam.input(),
            Values.frozenMap(new LinkedHashMap<String, FunctionSpec>()), List.of(), spam.tests(), spam.raw(), "test", false);
        Catalogue catalogue = new Catalogue(List.of(empty), Map.of());
        assertEquals("jeff.test.empty has no functions",
            assertThrows(JeffError.UnknownFunction.class, () -> catalogue.function("jeff.test.empty")).getMessage());
    }

    @Test
    void theCachedShippedCatalogueCannotBeChangedThroughAnyView() {
        Catalogue catalogue = Catalogue.shipped();
        Manifest spam = catalogue.adapter("jeff.safety.spam");
        assertThrows(UnsupportedOperationException.class, () -> spam.raw().put("id", "x"));
        assertThrows(UnsupportedOperationException.class, () -> Values.map(spam.raw().get("tests")).put("function_by", "only"));
        assertThrows(UnsupportedOperationException.class, () -> Values.list(spam.raw().get("aliases")).add("x"));
        assertThrows(UnsupportedOperationException.class, () -> spam.tests().clear());
        assertThrows(UnsupportedOperationException.class, () -> spam.weights().clear());
        assertThrows(UnsupportedOperationException.class, () -> spam.functions().clear());
        assertThrows(UnsupportedOperationException.class, () -> spam.functions().get("is_spam").labels().fixed().clear());
        assertThrows(UnsupportedOperationException.class, () -> catalogue.base("jeff-base", "v1.4").put("repo", "x"));
        assertThrows(UnsupportedOperationException.class, () -> catalogue.manifests().clear());
        assertEquals("jeff.safety.spam", Catalogue.shipped().adapter("spam").raw().get("id"));
    }

    @Test
    void aCalibrationBlockMissingAKeyIsRefusedByName() {
        Map<String, Object> block = Values.map(Values.plain(Catalogue.shipped().adapter("jeff.safety.spam").raw()));
        Map<String, Object> calibration = Values.map(Values.map(Values.map(block.get("functions")).get("is_spam")).get("calibration"));
        calibration.remove("threshold");
        assertEquals("calibration block has no 'threshold'",
            assertThrows(JeffError.ContractError.class, () -> Calibration.fromWire(calibration)).getMessage());
    }

    private static void adapterFolder(Path folder, String adapterId) throws IOException {
        Files.createDirectories(folder);
        Files.writeString(folder.resolve("manifest.yaml"), Shipped.text("catalogue/" + adapterId + "/manifest.yaml"));
    }

    @Test
    void aFolderIsReadAsPythonGlobsItAndAnUnreadableFolderIsAnError(@TempDir Path root) throws IOException {
        Path catalogue = root.resolve("catalogue");
        Files.createDirectories(catalogue);
        Files.writeString(catalogue.resolve("bases.yaml"), Shipped.text("catalogue/bases.yaml"));
        adapterFolder(catalogue.resolve("b"), "jeff.safety.spam");
        adapterFolder(catalogue.resolve(".hidden"), "jeff.safety.guard");
        adapterFolder(root.resolve("elsewhere"), "jeff.compliance.sanctions");
        Files.createSymbolicLink(catalogue.resolve("linked"), root.resolve("elsewhere"));
        Files.createSymbolicLink(catalogue.resolve("dangling"), root.resolve("nowhere"));
        Files.createDirectories(catalogue.resolve("empty"));
        Files.writeString(catalogue.resolve("notes.txt"), "not an adapter");
        assertEquals(List.of("jeff.safety.guard", "jeff.safety.spam", "jeff.compliance.sanctions"),
            Catalogue.fromDir(catalogue).manifests().stream().map(Manifest::id).toList());

        Path locked = catalogue.resolve("locked");
        Files.createDirectories(locked);
        Files.setPosixFilePermissions(locked, PosixFilePermissions.fromString("---------"));
        try {
            String message = assertThrows(JeffError.ManifestError.class, () -> Catalogue.fromDir(catalogue)).getMessage();
            assertTrue(message.startsWith(locked + " cannot be read, so whether it holds an adapter's manifest.yaml is unknown"),
                message);
        } finally {
            Files.setPosixFilePermissions(locked, PosixFilePermissions.fromString("rwx------"));
        }

        Files.createDirectories(catalogue.resolve("a").resolve("manifest.yaml"));
        // as in Python, a manifest.yaml that is a folder is listed, and reading it fails
        String unreadable = assertThrows(UncheckedIOException.class, () -> Catalogue.fromDir(catalogue)).getMessage();
        assertEquals("Reading " + catalogue.resolve("a").resolve("manifest.yaml") + " failed", unreadable);
    }

    @Test
    void theUnversionedIdRemovesTheOneVersionSegmentAsInPython() {
        String[][] cases = {
            {"jeff.example.router-v14-fast", "jeff.example.router-fast"},
            {"jeff.routing.router-open-v14", "jeff.routing.router-open"},
            {"jeff.v2.router-v3", "jeff.v2.router"},
            {"jeff.support.triage", null}, {"jeff.routing.v14", null}, {"jeff.routing.router-v14-v15", null},
            {"jeff.routing.router-v", null}, {"jeff.routing.router-v1x", null}, {"jeff.routing.router-14", null},
            {"jeff.routing.router-open-v14\n", null},
        };
        for (String[] item : cases) assertEquals(item[1], Manifests.unversionedId(item[0]), item[0]);
    }
}
