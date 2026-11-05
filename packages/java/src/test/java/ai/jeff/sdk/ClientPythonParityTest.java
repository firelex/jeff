package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import ai.jeff.sdk.testing.FakeEngine;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;

/** The client against what the Python SDK does in the same situation (each expected message was produced by the
 * Python SDK, `uv run python`, on the same catalogue and engine answers), beyond what decisions.json records. */
class ClientPythonParityTest {
    private static final Map<String, Object> CTX = Fixtures.ordered("company", "A shop.", "channel", "email");
    private static final Map<String, Object> SMS = Fixtures.ordered("channel", "sms");
    private static final Set<String> ROUTED = Set.of("jeff.core.router", "jeff.support.router", "jeff.support.triage",
        "jeff.safety.spam");

    private static double[] unsure(String adapter, Row row) {
        return new double[] {0.3, 0.2, 0.2, 0.2, 0.1};
    }

    private static Jeff triage() {
        return Jeff.create(new FakeEngine(ClientPythonParityTest::unsure, List.of("jeff.support.triage", "jeff.safety.spam")),
            Catalogue.shipped());
    }

    private static Decision urgency(Jeff jeff, Policy policy) {
        return jeff.function("triage.urgency", CallOptions.none().context(CTX).onLowConfidence(policy)).call("Hm");
    }

    private static String failed(Jeff jeff, Policy policy) {
        return assertThrows(JeffError.EscalationFailed.class, () -> urgency(jeff, policy)).getMessage();
    }

    private static void pause(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("interrupted", error);
        }
    }

    private static Jeff routed() {
        return Jeff.create(new FakeEngine((adapter, row) -> switch (adapter) {
            case "jeff.support.router" -> new double[] {1.0};
            case "jeff.core.router" -> new double[] {0.9, 0.1};
            default -> unsure(adapter, row);
        }, ROUTED), Fixtures.catalogueFor("routed"));
    }

    @Test
    void aNamedShorthandOfAnotherInterfaceIsRefused() {
        Jeff jeff = triage();
        assertEquals("jeff.safety.spam.is_spam implements detect, not rate; call jeff.detect",
            assertThrows(JeffError.InterfaceMismatch.class, () -> jeff.rate("Hm", "spam.is_spam", CallOptions.none().context(SMS)))
                .getMessage());
        assertEquals("jeff.support.triage.urgency implements rate, not classify; call jeff.rate",
            assertThrows(JeffError.InterfaceMismatch.class, () -> jeff.classify("Hm", "triage.urgency", CallOptions.none().context(CTX)))
                .getMessage());
        assertEquals("Escalation step jeff.safety.spam.is_spam implements detect, not rate",
            assertThrows(JeffError.InterfaceMismatch.class, () -> jeff.rate("Hm", "triage.urgency",
                CallOptions.none().context(CTX).onLowConfidence(Policy.escalate(List.of(Step.function("spam.is_spam"))))))
                .getMessage());
    }

    @Test
    void policyErrorRaisesLowConfidenceAndAnUnknownPolicyIsRefused() {
        Jeff jeff = triage();
        JeffError.LowConfidence error = assertThrows(JeffError.LowConfidence.class, () -> urgency(jeff, Policy.ERROR));
        assertEquals("jeff.support.triage.urgency abstained (confidence 0.3); on_low_confidence='error'", error.getMessage());
        assertEquals(0L, error.decision().value());
        assertEquals("Unknown on_low_confidence policy 'retry'; use 'return', 'error' or escalate(...)",
            assertThrows(JeffError.InvalidPolicy.class, () -> urgency(jeff, Policy.named("retry"))).getMessage());
    }

    @Test
    void aFunctionStepThatAbstainsTooReturnsItsDecisionWithTheEarlierAttempt() {
        Map<String, Object> json = urgency(triage(), Policy.escalate(List.of(Step.function("triage.sentiment")))).toJson();
        assertEquals("jeff.support.triage.sentiment@1.4.0", json.get("decided_by"));
        assertEquals(true, json.get("abstained"));
        assertEquals(List.of(Fixtures.ordered("decided_by", "jeff.support.triage.urgency@1.4.0", "value", 0L, "confidence", 0.3,
            "reason", "abstained (confidence 0.300 < threshold 0.787201)")), json.get("escalations"));
    }

    @Test
    void aRemoteStepsDecisionIsReturnedWithTheEarlierAttempt() {
        Jeff other = Jeff.create(new FakeEngine((adapter, row) -> new double[] {0, 0, 0, 0.95, 0.05},
            List.of("jeff.support.triage")), Catalogue.shipped());
        Map<String, Object> json = urgency(triage(), Policy.escalate(List.of(Step.remote(other)))).toJson();
        assertEquals(3L, json.get("value"));
        assertEquals(0.95, json.get("confidence"));
        assertEquals("jeff.support.triage.urgency@1.4.0", json.get("decided_by"));
        assertEquals(1, ((List<?>) json.get("escalations")).size());
    }

    @Test
    void anAnswerThatIsNotAnOptionFailsTheEscalationForEveryKindOfFunction() {
        Jeff jeff = triage();
        assertEquals("Escalation step llm failed: answered None, which is not one of ['0', '1', '2', '3', '4']",
            failed(jeff, Policy.escalate(List.of(Step.llm(question -> null)))));
        Jeff spam = Jeff.create(new FakeEngine((adapter, row) -> new double[] {0.5, 0.5}, List.of("jeff.safety.spam")),
            Catalogue.shipped());
        assertEquals("Escalation step human failed: answered 'yes', which is not one of ['true', 'false']",
            assertThrows(JeffError.EscalationFailed.class, () -> spam.function("spam.is_spam",
                CallOptions.none().context(SMS).onLowConfidence(Policy.escalate(List.of(Step.human(question -> "yes"))))).call("Hm"))
                .getMessage());
        Jeff route = Jeff.create(new FakeEngine((adapter, row) -> new double[] {0.34, 0.33, 0.33}, List.of("jeff.support.triage")),
            Catalogue.shipped());
        assertEquals("Escalation step llm failed: answered 'billing', which is not one of ['other', 'refunds', 'deliveries']",
            assertThrows(JeffError.EscalationFailed.class, () -> route.function("triage.route", CallOptions.none().context(CTX)
                .labels(Fixtures.ordered("refunds", "Money back", "deliveries", "Where"))
                .onLowConfidence(Policy.escalate(List.of(Step.llm(question -> "billing"))))).call("Hm")).getMessage());
    }

    @Test
    void aThrowingStepIsAnEscalationFailedNamingItsJavaTypeWithTheCauseKept() {
        Jeff jeff = triage();
        IllegalArgumentException thrown = new IllegalArgumentException("no one is here");
        JeffError.EscalationFailed error = assertThrows(JeffError.EscalationFailed.class,
            () -> urgency(jeff, Policy.escalate(List.of(Step.human(question -> { throw thrown; })))));
        // Python: "Escalation step human failed: ValueError: no one is here" (the closest Java type is named instead)
        assertEquals("Escalation step human failed: IllegalArgumentException: no one is here", error.getMessage());
        assertEquals("human", error.step());
        assertSame(thrown, error.getCause());
        Decider down = calls -> { throw new JeffError.EngineError("remote is down"); };
        assertEquals("Escalation step remote failed: EngineError: remote is down",
            failed(jeff, Policy.escalate(List.of(Step.remote(down)))));
        Decider late = calls -> { throw new JeffError.DeadlineExceeded("the step took too long"); };
        assertEquals("Escalation step remote failed: DeadlineExceeded: the step took too long",
            failed(jeff, Policy.escalate(List.of(Step.remote(late)))));
    }

    @Test
    void aRemoteStepAnsweringAnythingButOneDecisionIsANamedError() {
        Jeff jeff = triage();
        // Python fails with "IndexError: list index out of range" for none and takes the first of two
        assertEquals("Escalation step remote failed: ContractError: The remote step's client answered 0 decisions for 1 call",
            failed(jeff, Policy.escalate(List.of(Step.remote(calls -> List.of())))));
        Decision one = jeff.decide(List.of(new Call("triage.urgency", "Hm", CTX, null))).get(0);
        assertEquals("Escalation step remote failed: ContractError: The remote step's client answered 2 decisions for 1 call",
            failed(jeff, Policy.escalate(List.of(Step.remote(calls -> List.of(one, one))))));
        assertEquals("Escalation step remote failed: ContractError: The remote step's client answered null for 1 call",
            failed(jeff, Policy.escalate(List.of(Step.remote(calls -> null)))));
    }

    @Test
    void theBudgetIsCheckedAfterEachStepSoASlowFirstStepStopsTheChain() {
        Jeff jeff = triage();
        boolean[] askedLlm = {false};
        Decider slowAbstaining = calls -> {
            pause(30);
            return jeff.decide(calls);
        };
        assertEquals("Escalation of jeff.support.triage.urgency took longer than 5 ms",
            assertThrows(JeffError.EscalationBudgetExceeded.class, () -> urgency(jeff, Policy.escalate(
                List.of(Step.remote(slowAbstaining), Step.llm(question -> { askedLlm[0] = true; return "1"; })), 5L))).getMessage());
        assertFalse(askedLlm[0]);
        assertEquals("Escalation of jeff.support.triage.urgency took longer than 2.5 ms",
            assertThrows(JeffError.EscalationBudgetExceeded.class, () -> urgency(jeff, Policy.escalate(List.of(
                Step.function("triage.sentiment"), Step.llm(question -> { pause(30); return "1"; })), 2.5))).getMessage());
    }

    @Test
    void theCallersOwnCancellationPassesThroughUnwrapped() {
        Jeff jeff = triage();
        CancellationException cancelled = new CancellationException("the caller gave up");
        try {
            assertSame(cancelled, assertThrows(CancellationException.class, () -> urgency(jeff, Policy.escalate(List.of(
                Step.human(question -> {
                    Thread.currentThread().interrupt();
                    throw cancelled;
                }))))));
        } finally {
            assertTrue(Thread.interrupted());
        }
        // the same exception from a step whose caller did not cancel is that step failing
        assertEquals("Escalation step human failed: CancellationException: the caller gave up",
            failed(jeff, Policy.escalate(List.of(Step.human(question -> { throw cancelled; })))));
    }

    @Test
    void dispatchRefusesWhatNoRouterLeadsToAndWhatIsNotARouter() {
        Jeff jeff = routed();
        assertEquals("Router jeff.core.router has no target that implements or leads to equivalent",
            assertThrows(JeffError.ManifestError.class, () -> jeff.equivalent("x", CallOptions.none().context(CTX))).getMessage());
        assertEquals("jeff.safety.spam is not a router", assertThrows(JeffError.ManifestError.class,
            () -> jeff.dispatch("x", "rate", "jeff.safety.spam", CallOptions.none())).getMessage());
        assertEquals("Malformed reference 'jeff.nope': use jeff.<domain>.<name>[.<function>]",
            assertThrows(JeffError.InvalidReference.class, () -> jeff.dispatch("x", "rate", "jeff.nope", CallOptions.none()))
                .getMessage());
        assertEquals("jeff.support.triage.urgency abstained (confidence 0.3); on_low_confidence='error'",
            assertThrows(JeffError.LowConfidence.class, () -> jeff.dispatch("Hm", "rate", CallOptions.none().context(CTX)
                .onLowConfidence(Policy.ERROR))).getMessage());
        Jeff unrouted = Jeff.create(new FakeEngine(ClientPythonParityTest::unsure, List.of("jeff.support.triage", "jeff.safety.spam")),
            Fixtures.catalogueFor("routed"));
        assertEquals("jeff.core.router is not installed: there is no router installed to route this call; install it, or name "
            + "the function to call with function=. Install it with: jeff pull jeff.core.router",
            assertThrows(JeffError.NotInstalled.class, () -> unrouted.dispatch("x", "rate", CallOptions.none().context(CTX))).getMessage());
    }

    @SuppressWarnings("unchecked")
    private static Catalogue routedWith(String target, String description) {
        List<Manifest> manifests = new ArrayList<>();
        for (Object item : Values.list(Values.map(Fixtures.load("catalogues.json").get("variants")).get("routed"))) {
            Map<String, Object> raw = Values.map(Values.plain(item));
            if (raw.get("id").equals("jeff.core.router")) {
                ((List<Object>) raw.get("routes_to")).add(target);
                Map<String, Object> domain = Values.map(Values.map(raw.get("functions")).get("domain"));
                Values.map(Values.map(domain.get("labels")).get("fixed")).put(target, description);
            }
            manifests.add(Manifests.fromMap(raw, "conformance/x/" + raw.get("id")));
        }
        return Catalogue.shipped().replacing(manifests);
    }

    @Test
    void aRouterRoutingToTheBaseOrToAWholeAdapterIsRefusedWhenTheClientIsMade() {
        Set<String> withBase = new java.util.HashSet<>(ROUTED);
        withBase.add("base");
        FakeEngine engine = new FakeEngine((adapter, row) -> new double[] {0, 0, 0, 1.0}, withBase);
        assertEquals("Router jeff.core.router routes to base: No adapter 'base' in the catalogue",
            assertThrows(JeffError.ManifestError.class, () -> Jeff.create(engine, routedWith("base", "The base model"))).getMessage());
        assertEquals("Router jeff.core.router routes to jeff.safety.spam: jeff.safety.spam has 2 functions (is_spam, message_type); "
            + "name one, e.g. jeff.safety.spam.is_spam", assertThrows(JeffError.ManifestError.class,
                () -> Jeff.create(engine, routedWith("jeff.safety.spam", "Spam"))).getMessage());
    }

    /** An engine whose predictAsync runs each predict on its own virtual thread, as the engine socket keeps several
     * in flight; `behaviour` answers or throws per adapter. */
    private static final class ConcurrentEngine implements Engine {
        interface Behaviour {
            EngineResult predict(String adapterId, List<Row> rows);
        }

        private final Behaviour behaviour;
        private final Set<String> syncFailures;
        final List<CompletableFuture<EngineResult>> started = new java.util.concurrent.CopyOnWriteArrayList<>();

        ConcurrentEngine(Behaviour behaviour, Set<String> syncFailures) {
            this.behaviour = behaviour;
            this.syncFailures = syncFailures;
        }

        public BaseFormat baseFormat() { return BaseFormat.bf16(); }
        public Set<String> installed(boolean refresh) { return Set.of("jeff.support.triage", "jeff.safety.spam"); }
        public EngineResult predict(String adapterId, List<Row> rows) { return behaviour.predict(adapterId, rows); }

        @Override
        public CompletableFuture<EngineResult> predictAsync(String adapterId, List<Row> rows) {
            if (syncFailures.contains(adapterId)) throw new JeffError.BackendUnavailable(adapterId + " refused at once");
            CompletableFuture<EngineResult> future = Async.run(() -> predict(adapterId, rows));
            started.add(future);
            return future;
        }
    }

    private static EngineResult answer(List<Row> rows, double... probabilities) {
        List<double[]> answers = new ArrayList<>();
        for (Row row : rows) answers.add(probabilities);
        return new EngineResult(answers, new LinkedHashMap<>(), null);
    }

    private static final List<Call> TWO_ADAPTERS = List.of(new Call("triage.urgency", "Hm", CTX, null),
        new Call("spam.is_spam", "x", SMS, null));

    @Test
    void predictsForSeveralAdaptersRunTogether() {
        CountDownLatch both = new CountDownLatch(2);
        ConcurrentEngine engine = new ConcurrentEngine((adapter, rows) -> {
            both.countDown();
            try {
                if (!both.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("the other predict never started");
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(error);
            }
            return adapter.equals("jeff.safety.spam") ? answer(rows, 0.1, 0.9) : answer(rows, 0, 0, 0.9, 0.1, 0);
        }, Set.of());
        List<Decision> decisions = Jeff.create(engine, Catalogue.shipped()).decide(TWO_ADAPTERS);
        assertEquals(List.of(2L, true), decisions.stream().map(Decision::value).toList());
    }

    @Test
    void aFailureIsReportedInAdapterOrderWithNoPredictLeftRunning() {
        AtomicBoolean firstFinished = new AtomicBoolean();
        // the first adapter fails late, the second at once: Python meets the first adapter's failure first
        ConcurrentEngine lateFirst = new ConcurrentEngine((adapter, rows) -> {
            if (adapter.equals("jeff.support.triage")) {
                pause(50);
                firstFinished.set(true);
                throw new JeffError.EngineError("triage failed late");
            }
            throw new JeffError.EngineError("spam failed at once");
        }, Set.of());
        assertEquals("triage failed late", assertThrows(JeffError.EngineError.class,
            () -> Jeff.create(lateFirst, Catalogue.shipped()).decide(TWO_ADAPTERS)).getMessage());
        assertTrue(firstFinished.get());

        // the second adapter's engine throws instead of failing its future: the same failure, once the first predict
        // (which succeeds, slowly) has finished
        AtomicBoolean slowFinished = new AtomicBoolean();
        ConcurrentEngine syncThrow = new ConcurrentEngine((adapter, rows) -> {
            pause(50);
            slowFinished.set(true);
            return answer(rows, 0, 0, 0.9, 0.1, 0);
        }, Set.of("jeff.safety.spam"));
        assertEquals("jeff.safety.spam refused at once", assertThrows(JeffError.BackendUnavailable.class,
            () -> Jeff.create(syncThrow, Catalogue.shipped()).decide(TWO_ADAPTERS)).getMessage());
        assertTrue(slowFinished.get());
        assertTrue(syncThrow.started.stream().allMatch(CompletableFuture::isDone));

        // the first adapter's predict fails while the second's throws at once: the first is reported
        ConcurrentEngine bothFail = new ConcurrentEngine((adapter, rows) -> {
            pause(20);
            throw new JeffError.EngineError("triage failed");
        }, Set.of("jeff.safety.spam"));
        assertEquals("triage failed", assertThrows(JeffError.EngineError.class,
            () -> Jeff.create(bothFail, Catalogue.shipped()).decide(TWO_ADAPTERS)).getMessage());
    }

    @Test
    void anAskStepsTimingIsRoundedAsPythonsRoundToThreeDecimals() {
        // Python: round(n / 1e6, 3) for each n
        long[] nanos = {5500, 6500, 1500, 2500, 7500, 12345500, 999500, 3000500};
        double[] python = {0.005, 0.006, 0.002, 0.003, 0.007, 12.345, 1.0, 3.001};
        for (int index = 0; index < nanos.length; index++) {
            assertEquals(python[index], Escalation.askMs(nanos[index]), "for " + nanos[index] + " ns");
        }
    }

    @Test
    void aRefusalKeepsTheErrorItCameFromAsPythonChainsIt() {
        Jeff jeff = triage();
        JeffError.InvalidPolicy policy = assertThrows(JeffError.InvalidPolicy.class,
            () -> urgency(jeff, Policy.escalate(List.of(Step.function("jeff.support.triage.nope")))));
        assertTrue(policy.getCause() instanceof JeffError.UnknownFunction);
        Set<String> withBase = new java.util.HashSet<>(ROUTED);
        withBase.add("base");
        JeffError.ManifestError routes = assertThrows(JeffError.ManifestError.class, () -> Jeff.create(
            new FakeEngine((adapter, row) -> new double[] {1.0}, withBase), routedWith("base", "The base model")));
        assertTrue(routes.getCause() instanceof JeffError.UnknownFunction);
        JeffError.UnknownFunction noRouter = assertThrows(JeffError.UnknownFunction.class,
            () -> jeff.dispatch("x", "rate", CallOptions.none().context(CTX)));
        assertTrue(noRouter.getCause() instanceof JeffError.UnknownFunction);
    }

    @Test
    void anAsyncTwinGivenABadListFailsInItsFutureNotOnTheCallersThread() {
        Jeff jeff = triage();
        CompletableFuture<List<Decision>> nullCall = jeff.decideAsync(java.util.Arrays.asList((Call) null));
        assertThrows(NullPointerException.class, () -> Async.join(nullCall));
        CompletableFuture<List<Decision>> nullList = jeff.decideAsync(null);
        assertThrows(NullPointerException.class, () -> Async.join(nullList));
        CompletableFuture<List<Decision>> nullInputs = jeff.function("triage.urgency", CallOptions.none().context(CTX)).manyAsync(null);
        assertThrows(NullPointerException.class, () -> Async.join(nullInputs));
    }

    @Test
    void anInterruptedCallerStopsWaitingForThePredictsAndKeepsItsInterrupt() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        ConcurrentEngine hanging = new ConcurrentEngine((adapter, rows) -> {
            try {
                release.await();
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(error);
            }
            return adapter.equals("jeff.safety.spam") ? answer(rows, 0.1, 0.9) : answer(rows, 0, 0, 0.9, 0.1, 0);
        }, Set.of());
        Jeff jeff = Jeff.create(hanging, Catalogue.shipped());
        CompletableFuture<Object[]> outcome = new CompletableFuture<>();
        Thread caller = Thread.ofVirtual().start(() -> {
            try {
                jeff.decide(TWO_ADAPTERS);
                outcome.complete(new Object[] {null, Thread.currentThread().isInterrupted()});
            } catch (RuntimeException error) {
                outcome.complete(new Object[] {error, Thread.currentThread().isInterrupted()});
            }
        });
        while (hanging.started.size() < 2) Thread.onSpinWait();
        caller.interrupt();
        Object[] got = outcome.get(5, TimeUnit.SECONDS);
        release.countDown();
        CancellationException cancelled = (CancellationException) got[0];
        assertEquals("Interrupted while waiting for the engine's predicts of jeff.support.triage, jeff.safety.spam; the "
            + "predicts still running were not waited for", cancelled.getMessage());
        assertTrue(cancelled.getCause() instanceof InterruptedException);
        assertEquals(true, got[1]);
    }
}
