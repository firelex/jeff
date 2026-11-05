package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import ai.jeff.sdk.testing.FakeEngine;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;

class ClientTest {
    private static final Map<String, Object> CTX = Fixtures.ordered("company", "A shop.", "channel", "email");
    private static final Map<String, Object> SPACED = Fixtures.ordered("ten", "x", "deal_id", "D-7", "counterparty_name", "Acme Ltd");

    private static double[] unsure(String adapter, Row row) {
        return new double[] {0.3, 0.2, 0.2, 0.2, 0.1};
    }

    private static void pause(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("interrupted", error);
        }
    }

    private static Step asking(String answer, List<Question> asked) {
        return Step.human(question -> {
            asked.add(question);
            return answer;
        });
    }

    @Test
    void aRemoteStepAsksAnotherClientWithTheSameCall() {
        Catalogue catalogue = Catalogue.shipped();
        Jeff jeff = Jeff.create(new FakeEngine(ClientTest::unsure, List.of("jeff.support.triage")), catalogue);
        Jeff other = Jeff.create(new FakeEngine((adapter, row) -> new double[] {0, 0, 0, 0.95, 0.05},
            List.of("jeff.support.triage")), catalogue);
        BoundFunction bound = jeff.function("triage.urgency",
            CallOptions.none().context(CTX).onLowConfidence(Policy.escalate(List.of(Step.remote(other)))));
        Decision decision = bound.call("Hm");
        assertEquals(3L, decision.value());
        assertEquals(List.of("jeff.support.triage.urgency@1.4.0"),
            decision.escalations().stream().map(Decision.EscalationRecord::decidedBy).toList());
    }

    @Test
    void anEscalationPastItsBudgetIsRefused() {
        Jeff jeff = Jeff.create(new FakeEngine(ClientTest::unsure, List.of("jeff.support.triage")), Catalogue.shipped());
        Step slow = Step.llm(question -> {
            pause(30);
            return "2";
        });
        BoundFunction bound = jeff.function("triage.urgency",
            CallOptions.none().context(CTX).onLowConfidence(Policy.escalate(List.of(slow), 5L)));
        assertEquals("Escalation of jeff.support.triage.urgency took longer than 5 ms",
            assertThrows(JeffError.EscalationBudgetExceeded.class, () -> bound.call("Hm")).getMessage());
    }

    @Test
    void aContextFieldGivenByItsExactKeyIsRefusedBeforeRouting() {
        assertEquals("context is given by code name; pass 'counterparty name' as counterparty_name (fields of adapters reached "
            + "from router jeff.core.router)", assertThrows(JeffError.UnknownContext.class, () -> Routing.refuseExactKeys(
                Map.of("counterparty name", "counterparty_name"), "jeff.core.router", Map.of("counterparty name", "Acme"))).getMessage());
        assertTrue(Routing.exactKeysOf(Catalogue.shipped(), "jeff.safety.spam").isEmpty());
    }

    @Test
    void aQuestionsStateKeepsTheOrderTheModelReadsNumberLikeKeysIncluded() {
        Jeff jeff = Jeff.create(new FakeEngine((adapter, row) -> new double[] {0.5, 0.5}, List.of("jeff.safety.spam")),
            Fixtures.catalogueFor("spaced"));
        List<Question> asked = new ArrayList<>();
        BoundFunction bound = jeff.function("spam.is_spam",
            CallOptions.none().context(SPACED).onLowConfidence(Policy.escalate(List.of(asking("false", asked)))));
        assertEquals("human", bound.call("Please sign").decidedBy());
        List<String> order = List.of("counterparty name", "Deal ID", "10", "message");
        assertEquals(order, asked.get(0).stateEntries().stream().map(Map.Entry::getKey).toList());
        assertEquals(order, List.copyOf(((Map<?, ?>) asked.get(0).state()).keySet()));
    }

    @Test
    void aQuestionsOptionsKeepOptionOrderNumberLikeKeysIncluded() {
        Map<String, Object> raw = Values.map(Values.plain(Values.list(
            Values.map(Fixtures.load("catalogues.json").get("variants")).get("spaced")).get(0)));
        Map<String, Object> messageType = Values.map(Values.map(raw.get("functions")).get("message_type"));
        Values.map(messageType.get("labels")).put("fixed", Fixtures.ordered("other", "None of these", "10", "Ten"));
        messageType.put("abstain_labels", List.of("other"));
        Catalogue catalogue = Catalogue.shipped().replacing(List.of(Manifests.fromMap(raw, "test/number-like-labels/jeff.safety.spam")));
        Jeff jeff = Jeff.create(new FakeEngine((adapter, row) -> new double[] {0.9, 0.1}, List.of("jeff.safety.spam")), catalogue);
        List<Question> asked = new ArrayList<>();
        BoundFunction bound = jeff.function("spam.message_type",
            CallOptions.none().context(SPACED).onLowConfidence(Policy.escalate(List.of(asking("10", asked)))));
        assertEquals("10", bound.call("Please sign").value());
        assertEquals(List.of("other", "10"), asked.get(0).optionEntries().stream().map(Map.Entry::getKey).toList());
        assertEquals(List.of("None of these", "Ten"), asked.get(0).optionEntries().stream().map(Map.Entry::getValue).toList());
        assertEquals(List.of("other", "10"), List.copyOf(asked.get(0).options().keySet()));
    }

    @Test
    void anExplicitNullPolicyIsRefusedAndAnExplicitNullRouterIsNotReplacedByTheDefault() {
        Jeff jeff = Jeff.create(new FakeEngine(ClientTest::unsure, List.of("jeff.support.triage")), Catalogue.shipped());
        assertEquals("Unknown on_low_confidence policy None; use 'return', 'error' or escalate(...)",
            assertThrows(JeffError.InvalidPolicy.class,
                () -> jeff.function("triage.urgency", CallOptions.none().context(CTX).onLowConfidence(null))).getMessage());
        assertThrows(JeffError.InvalidPolicy.class, () -> jeff.rate("Hm", CallOptions.none().context(CTX).onLowConfidence(null)));
        assertThrows(NullPointerException.class, () -> jeff.dispatch("Hm", "rate", null, CallOptions.none()));
        assertThrows(NullPointerException.class, () -> jeff.rate("Hm", (String) null, CallOptions.none()));
    }

    @Test
    void aPredictThatFailsIsReportedBeforeALaterAdaptersRowsFailToBuild() {
        boolean[] down = {true};
        Engine engine = new Engine() {
            public BaseFormat baseFormat() { return BaseFormat.bf16(); }
            public Set<String> installed(boolean refresh) { return Set.of("jeff.support.triage", "jeff.safety.spam"); }
            public EngineResult predict(String adapterId, List<Row> rows) {
                if (down[0]) throw new JeffError.EngineError(adapterId + " is down");
                List<double[]> answers = new ArrayList<>();
                for (Row row : rows) answers.add(new double[] {0.5, 0.5, 0, 0, 0});
                return new EngineResult(answers, new LinkedHashMap<>(), null);
            }
        };
        Jeff jeff = Jeff.create(engine, Fixtures.catalogueFor("text"));
        Call urgency = new Call("triage.urgency", "Hm", CTX, null);
        Call spam = new Call("spam.is_spam", 5L, null, null);
        List<Jeff.Bound> bound = List.of(new Jeff.Bound(urgency, jeff.bindCall(urgency)),
            new Jeff.Bound(spam, jeff.bindCall(new Call("spam.is_spam", "x", null, null))));
        assertEquals("jeff.support.triage is down", assertThrows(JeffError.EngineError.class, () -> jeff.decideBound(bound)).getMessage());
        down[0] = false;
        assertThrows(JeffError.InvalidRequest.class, () -> jeff.decideBound(bound));
    }

    @Test
    void aNullInputIsCalledLikeAnyOtherValue() {
        FakeEngine engine = new FakeEngine((adapter, row) -> new double[] {0.5, 0.5}, List.of("jeff.safety.spam"));
        Jeff jeff = Jeff.create(engine, Catalogue.shipped());
        BoundFunction bound = jeff.function("spam.is_spam", CallOptions.none().context(Map.of("channel", "sms")));
        assertEquals(2, bound.many(Arrays.asList("hello", null)).size());
        assertNull(((Map<?, ?>) engine.calls().get(0).getValue().get(1).state()).get("message"));
        assertTrue(((Map<?, ?>) engine.calls().get(0).getValue().get(1).state()).containsKey("message"));
    }

    @Test
    void theAsyncVariantsGiveTheSameDecisionsAndThrowJeffErrorsAsThemselves() {
        Jeff jeff = Jeff.create(new FakeEngine(ClientTest::unsure, List.of("jeff.support.triage")), Catalogue.shipped());
        List<Call> calls = List.of(new Call("triage.urgency", "Hm", CTX, null));
        assertEquals(jeff.decide(calls).get(0).toJson().get("value"), Async.join(jeff.decideAsync(calls)).get(0).toJson().get("value"));
        assertThrows(JeffError.NotInstalled.class,
            () -> Async.join(jeff.decideAsync(List.of(new Call("spam.is_spam", "x", Map.of("channel", "sms"), null)))));
        BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
        assertEquals(0L, Async.join(bound.callAsync("Hm")).value());
    }
}
