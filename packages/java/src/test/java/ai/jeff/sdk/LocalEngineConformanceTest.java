package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.function.Executable;

/** local_engine.json: every case of the engine start's pure parts (contract/local-engine.md). */
class LocalEngineConformanceTest {
    private static List<Map<String, Object>> section(String name) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object item : Values.list(Fixtures.load("local_engine.json").get(name))) out.add(Values.map(item));
        if (out.isEmpty()) throw new AssertionError("local_engine.json has no " + name);
        return out;
    }

    private static void assertRaises(Map<String, Object> expected, Executable action, String what) {
        JeffError error = assertThrows(JeffError.class, action, what);
        assertEquals(expected.get("type"), error.type(), what);
        assertEquals(expected.get("message"), error.getMessage(), what);
    }

    private static void assertError(Map<String, Object> expected, JeffError error, String what) {
        assertEquals(expected.get("type"), error.type(), what);
        assertEquals(expected.get("message"), error.getMessage(), what);
    }

    @Test
    void platforms() {
        for (Map<String, Object> item : section("platforms")) {
            String os = (String) item.get("os");
            String arch = (String) item.get("arch");
            boolean nvidia = (Boolean) item.get("nvidia");
            String what = item.toString();
            if (item.containsKey("error")) {
                assertRaises(Values.map(item.get("error")), () -> LocalEngine.extra(os, arch, () -> nvidia), what);
            } else {
                assertEquals(item.get("extra"), LocalEngine.extra(os, arch, () -> nvidia), what);
            }
        }
    }

    @Test
    void javaNamesOfSystemsAndProcessors() {
        assertEquals("macos", LocalEngine.osName("Mac OS X"));
        assertEquals("linux", LocalEngine.osName("Linux"));
        assertEquals("windows", LocalEngine.osName("Windows 11"));
        assertEquals("FreeBSD", LocalEngine.osName("FreeBSD"));
        assertEquals("arm64", LocalEngine.archName("aarch64"));
        assertEquals("x86_64", LocalEngine.archName("amd64"));
        assertEquals("x86_64", LocalEngine.archName("x86_64"));
        assertEquals("riscv64", LocalEngine.archName("riscv64"));
    }

    @Test
    void folders() {
        for (Map<String, Object> item : section("folders")) {
            String os = (String) item.get("os");
            String home = (String) item.get("home");
            String xdg = (String) item.get("xdg_cache_home");
            String what = item.toString();
            if (item.containsKey("error")) {
                assertRaises(Values.map(item.get("error")), () -> LocalEngine.engineFolder(os, home, xdg), what);
            } else {
                assertEquals(item.get("folder"), LocalEngine.engineFolder(os, home, xdg).toString(), what);
            }
        }
    }

    @Test
    void uvx() {
        for (Map<String, Object> item : section("uvx")) {
            List<String> executables = Values.strings(item.get("executables"));
            String path = (String) item.get("path");
            String home = (String) item.get("home");
            String what = (String) item.get("name");
            if (item.containsKey("error")) {
                assertRaises(Values.map(item.get("error")), () -> LocalEngine.findUvx(path, home, executables::contains), what);
            } else {
                assertEquals(item.get("found"), LocalEngine.findUvx(path, home, executables::contains), what);
            }
        }
    }

    @Test
    void shellStarts() {
        for (Map<String, Object> item : section("shell_starts")) {
            assertEquals(item.get("argv"), LocalEngine.shellStart((String) item.get("os"), (String) item.get("setsid"),
                Values.strings(item.get("uvx_argv"))), (String) item.get("os"));
        }
    }

    @Test
    void setsid() {
        for (Map<String, Object> item : section("setsid")) {
            List<String> executables = Values.strings(item.get("executables"));
            String path = (String) item.get("path");
            String what = (String) item.get("name");
            if (item.containsKey("error")) {
                assertRaises(Values.map(item.get("error")), () -> LocalEngine.findSetsid(path, executables::contains), what);
            } else {
                assertEquals(item.get("found"), LocalEngine.findSetsid(path, executables::contains), what);
            }
        }
    }

    @Test
    void adapters() {
        for (Map<String, Object> item : section("adapters")) {
            List<String> references = Values.strings(item.get("adapters"));
            String what = references.toString();
            if (item.containsKey("error")) {
                assertRaises(Values.map(item.get("error")), () -> LocalEngine.resolve(Catalogue.shipped(), references), what);
            } else {
                LocalEngine.Resolved resolved = LocalEngine.resolve(Catalogue.shipped(), references);
                assertEquals(item.get("adapter_ids"), resolved.adapterIds(), what);
                assertEquals(item.get("base"), resolved.base(), what);
            }
        }
    }

    @Test
    void commands() {
        for (Map<String, Object> item : section("commands")) {
            LocalEngine.Resolved resolved = LocalEngine.resolve(Catalogue.shipped(), Values.strings(item.get("adapters")));
            List<String> argv = LocalEngine.command((String) item.get("uvx"), (String) item.get("extra"), (String) item.get("version"),
                Path.of((String) item.get("folder")), resolved.adapterIds(), resolved.base());
            assertEquals(item.get("argv"), argv, (String) item.get("name"));
        }
    }

    @Test
    void theCommandPinsThisPackagesOwnVersion() {
        assertEquals("1.4.1", LocalEngine.sdkVersion());
    }

    @Test
    void logTails() {
        for (Map<String, Object> item : section("log_tails")) {
            assertEquals(item.get("tail"), LocalEngine.logTail((String) item.get("log")), (String) item.get("name"));
        }
    }

    @Test
    void messages() {
        for (Map<String, Object> item : section("messages")) {
            String kind = (String) item.get("kind");
            String what = item.toString();
            Path logPath = item.containsKey("log_path") ? Path.of((String) item.get("log_path")) : null;
            Path socketPath = item.containsKey("socket_path") ? Path.of((String) item.get("socket_path")) : null;
            switch (kind) {
                case "starting" -> assertEquals(item.get("text"), LocalEngine.startingLine(Values.strings(item.get("adapter_ids")), logPath), what);
                case "waiting" -> assertEquals(item.get("text"), LocalEngine.waitingLine(logPath), what);
                case "exited" -> assertError(Values.map(item.get("error")),
                    LocalEngine.exited(((Number) item.get("status")).intValue(), logPath, (String) item.get("log")), what);
                case "start_timeout" -> assertError(Values.map(item.get("error")), LocalEngine.startTimeout(socketPath,
                    ((Number) item.get("start_timeout_ms")).longValue(), logPath, (String) item.get("log")), what);
                case "lock_timeout" -> assertError(Values.map(item.get("error")), LocalEngine.lockTimeout(
                    ((Number) item.get("start_timeout_ms")).longValue(), Path.of((String) item.get("lock_path")),
                    item.get("pid").toString(), logPath), what);
                case "not_loadable" -> {
                    Map<String, Object> engineError = Values.map(item.get("engine_error"));
                    assertError(Values.map(item.get("error")), LocalEngine.notLoadable(socketPath, Values.strings(item.get("missing")),
                        (String) engineError.get("type"), (String) engineError.get("message")), what);
                }
                case "proprietary" -> assertError(Values.map(item.get("error")), LocalEngine.proprietary((String) item.get("adapter_id")), what);
                case "not_own_group" -> assertError(Values.map(item.get("error")),
                    LocalEngine.notOwnGroup(logPath, (String) item.get("log")), what);
                case "home_missing" -> assertError(Values.map(item.get("error")), LocalEngine.homeMissing(), what);
                case "different_bases" -> assertError(Values.map(item.get("error")),
                    LocalEngine.differentBases(Values.strings(item.get("bases"))), what);
                default -> throw new AssertionError("local_engine.json has a message of an unknown kind: " + kind);
            }
        }
    }
}
