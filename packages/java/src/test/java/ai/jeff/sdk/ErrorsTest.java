package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class ErrorsTest {
    @Test
    void everyTypeNameBuildsItsOwnClassWhoseTypeIsItsName() {
        assertEquals(35, JeffError.typeNames().size());
        for (String name : JeffError.typeNames()) {
            JeffError error = JeffError.fromWire(Map.of("type", name, "message", "m"));
            assertEquals(name, error.type());
            assertEquals(name, error.getClass().getSimpleName());
            assertEquals(name + ": m", error.described());
        }
    }

    @Test
    void theTypedConstructorsWriteThePythonMessages() {
        JeffError.NotInstalled error = JeffError.notInstalled("jeff.support.triage.urgency", "it is an escalation step");
        assertEquals("jeff.support.triage.urgency is not installed: it is an escalation step. Install it with: jeff pull "
            + "jeff.support.triage", error.getMessage());
        assertEquals(Map.of("function_id", "jeff.support.triage.urgency"), error.details());
        assertEquals("jeff.core.router is not installed. Install it with: jeff pull jeff.core.router",
            JeffError.notInstalled("jeff.core.router").getMessage());
        JeffError.MissingContext missing = JeffError.missingContext("spam.x", List.of("channel", "region"));
        assertEquals("spam.x needs context field(s) channel, region; pass them in context=", missing.getMessage());
        assertEquals(List.of("channel", "region"), missing.fields());
        assertEquals("spam.x takes its labels from the caller; pass labels=", JeffError.missingLabels("spam.x").getMessage());
        assertEquals("Escalation step llm failed: why", JeffError.escalationFailed("llm", "why").getMessage());
    }

    @Test
    void anErrorOnTheWireBecomesTheSameClassWithTheSameMessageAndDetails() {
        JeffError error = JeffError.fromWire(Map.of("type", "Busy", "message", "full", "details", Map.of("retry_after_s", 2.5)));
        JeffError.Busy busy = assertInstanceOf(JeffError.Busy.class, error);
        assertEquals(2.5, busy.retryAfterS());
        assertEquals(Map.of("type", "Busy", "message", "full", "details", Map.of("retry_after_s", 2.5)), JeffError.toWire(error));
        assertInstanceOf(JeffError.NotInstalled.class,
            JeffError.fromWire(JeffError.toWire(new JeffError.NotInstalled("x", Map.of("function_id", "x")))));
    }

    @Test
    void anUnknownTypeOnTheWireIsAnEngineErrorNamingIt() {
        JeffError error = JeffError.fromWire(Map.of("type", "Weird", "message", "Something odd"));
        assertInstanceOf(JeffError.EngineError.class, error);
        assertEquals("unknown error type 'Weird' from the engine: Something odd", error.getMessage());
        assertEquals(Map.of(), error.details());
    }

    @Test
    void aTypedGetterThrowsNamingTheMissingField() {
        JeffError.NotInstalled broken = new JeffError.NotInstalled("boom");
        JeffError.ContractError error = assertThrows(JeffError.ContractError.class, broken::functionId);
        assertEquals("NotInstalled.details is missing its 'function_id' string (got None)", error.getMessage());
        JeffError.Busy busy = new JeffError.Busy("full", Map.of("retry_after_s", "soon"));
        assertEquals("Busy.details is missing its 'retry_after_s' number (got 'soon')",
            assertThrows(JeffError.ContractError.class, busy::retryAfterS).getMessage());
    }

    @Test
    void usageLimitReachedReadsItsFourDetails() {
        Map<String, Object> details = new LinkedHashMap<>();
        details.put("account", "u_1");
        details.put("used", 50_000_123L);
        details.put("allowance", 50_000_000L);
        details.put("period_end", "2026-12-03T10:00:00Z");
        JeffError error = JeffError.fromWire(Map.of("type", "UsageLimitReached", "message", "This account has used its "
            + "50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; calls resume then", "details", details));
        JeffError.UsageLimitReached limit = assertInstanceOf(JeffError.UsageLimitReached.class, error);
        assertEquals(List.of("u_1", 50_000_123L, 50_000_000L, "2026-12-03T10:00:00Z"),
            List.of(limit.account(), limit.used(), limit.allowance(), limit.periodEnd()));
        assertEquals("UsageLimitReached.details is missing its 'period_end' string (got None)",
            assertThrows(JeffError.ContractError.class, new JeffError.UsageLimitReached("m")::periodEnd).getMessage());
        assertEquals("UsageLimitReached.details is missing its 'used' whole number (got 1.5)",
            assertThrows(JeffError.ContractError.class,
                new JeffError.UsageLimitReached("m", Map.of("used", 1.5))::used).getMessage());
    }

    @Test
    void missingContextDetailsKeepPythonsKeyOrder() {
        // Map.of with two or more entries does not keep insertion order (confirmed by running a trivial program
        // that wraps Map.of("function_id", "x", "fields", List.of()) in a LinkedHashMap and printing its key set
        // across repeated JVM launches: the order varies run to run). missingContext is the only error constructor
        // in this file that builds a details map with two or more keys, so it is the only one this test needs to
        // pin. Python's order comes from packages/python/src/jeff/wire.py's error_to_wire, which walks
        // vars(error).items() in the order the exception's __init__ sets its attributes; for MissingContext that is
        // "self.function_id, self.fields = function_id, fields" (function_id, then fields). Confirmed directly:
        //   $ cd /Users/mattsinalco/mathias/apps/jeff-dev && uv run python -c "
        //   import jeff.errors as errors
        //   from jeff.wire import error_to_wire
        //   print(list(error_to_wire(errors.MissingContext('spam.x', ['channel', 'region']))['details'].keys()))"
        //   ['function_id', 'fields']
        JeffError.MissingContext missing = JeffError.missingContext("spam.x", List.of("channel", "region"));
        assertEquals(List.of("function_id", "fields"), List.copyOf(missing.details().keySet()));
    }

    @Test
    void fromWireRequiresTypeAndMessage() {
        JeffError.ContractError missingType = assertThrows(JeffError.ContractError.class,
            () -> JeffError.fromWire(Map.of("message", "m")));
        assertEquals("error wire body is missing its 'type' key", missingType.getMessage());
        JeffError.ContractError missingMessage = assertThrows(JeffError.ContractError.class,
            () -> JeffError.fromWire(Map.of("type", "Busy")));
        assertEquals("error wire body is missing its 'message' key", missingMessage.getMessage());
    }

    @Test
    void detailsKeepTheirOrderAndAllowNull() {
        Map<String, Object> details = new LinkedHashMap<>();
        details.put("b", null);
        details.put("a", 1L);
        JeffError error = new JeffError.EngineError("m", details);
        assertEquals(List.of("b", "a"), List.copyOf(error.details().keySet()));
        assertThrows(UnsupportedOperationException.class, () -> error.details().put("c", 2L));
    }

    @Test
    void copyGivesEachWaitingCallerItsOwnInstanceWithTheSameNotesAndRequestId() {
        JeffError.NotInstalled original = JeffError.notInstalled("jeff.core.router");
        original.setRequestId("req-1");
        original.addNote("also tried the other router");
        JeffError copy = JeffError.copy(original);
        assertNotSame(original, copy);
        assertInstanceOf(JeffError.NotInstalled.class, copy);
        assertEquals(original.getMessage(), copy.getMessage());
        assertEquals(original.details(), copy.details());
        assertEquals("req-1", copy.requestId());
        assertEquals(List.of("also tried the other router"), copy.notes());
        copy.addNote("only on the copy");
        assertEquals(List.of("also tried the other router"), original.notes());
    }

    @Test
    void describeWritesAnyExceptionAsPythonDoes() {
        assertEquals("IllegalStateException: broken", JeffError.describe(new IllegalStateException("broken")));
        assertEquals("IllegalStateException: ", JeffError.describe(new IllegalStateException()));
        assertEquals("EngineError: gone", JeffError.describe(new JeffError.EngineError("gone")));
    }
}
