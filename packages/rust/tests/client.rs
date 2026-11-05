mod support;

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use indexmap::IndexMap;
use jeff_sdk::binding::{Call, Row};
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::engine::{BaseFormat, Engine, EngineResult};
use jeff_sdk::escalation::{Decider, Question, escalate, human, llm, remote};
use jeff_sdk::manifest::manifest_from_json;
use jeff_sdk::routing::{exact_keys_of, refuse_exact_keys};
use jeff_sdk::testing::FakeEngine;
use jeff_sdk::{BoxError, CallOptions, ErrorType, Jeff, JeffError, Json, JsonMap, Result};
use serde_json::json;
use support::{catalogue_for, fixture};

fn ctx(value: Json) -> JsonMap {
    value.as_object().unwrap().clone()
}

fn unsure(_: &str, _: &Row) -> Vec<f64> {
    vec![0.3, 0.2, 0.2, 0.2, 0.1]
}

#[tokio::test]
async fn a_remote_step_asks_another_client_with_the_same_call() {
    let catalogue = Catalogue::shipped().unwrap();
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(unsure, ["jeff.support.triage"])),
        Arc::clone(&catalogue),
    )
    .await
    .unwrap();
    let other = Jeff::new(
        Arc::new(FakeEngine::new(
            |_, _| vec![0.0, 0.0, 0.0, 0.95, 0.05],
            ["jeff.support.triage"],
        )),
        catalogue,
    )
    .await
    .unwrap();
    let decider: Arc<dyn Decider> = Arc::new(other);
    let policy = escalate(vec![remote(decider)], None).unwrap();
    let options = CallOptions {
        context: Some(ctx(json!({"company": "A shop.", "channel": "email"}))),
        labels: None,
        on_low_confidence: policy,
    };
    let decision = jeff
        .function("triage.urgency", options)
        .await
        .unwrap()
        .call(json!("Hm"))
        .await
        .unwrap();
    assert_eq!(decision.value, jeff_sdk::Value::Int(3));
    let by: Vec<&str> = decision
        .escalations
        .iter()
        .map(|record| record.decided_by.as_str())
        .collect();
    assert_eq!(by, ["jeff.support.triage.urgency@1.4.0"]);
}

#[tokio::test]
async fn an_escalation_past_its_budget_is_refused() {
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(unsure, ["jeff.support.triage"])),
        Catalogue::shipped().unwrap(),
    )
    .await
    .unwrap();
    let slow = llm(|_question: Question| async {
        tokio::time::sleep(Duration::from_millis(30)).await;
        Ok::<String, BoxError>("2".into())
    });
    let options = CallOptions {
        context: Some(ctx(json!({"company": "A shop.", "channel": "email"}))),
        labels: None,
        on_low_confidence: escalate(vec![slow], Some(5)).unwrap(),
    };
    let error = jeff
        .function("triage.urgency", options)
        .await
        .unwrap()
        .call(json!("Hm"))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::EscalationBudgetExceeded);
    assert_eq!(
        error.message(),
        "Escalation of jeff.support.triage.urgency took longer than 5 ms"
    );
}

#[tokio::test]
async fn an_llm_step_that_fails_becomes_escalation_failed_naming_it() {
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(unsure, ["jeff.support.triage"])),
        Catalogue::shipped().unwrap(),
    )
    .await
    .unwrap();
    let broken =
        llm(|_question: Question| async { Err::<String, BoxError>("the model is down".into()) });
    let options = CallOptions {
        context: Some(ctx(json!({"company": "A shop.", "channel": "email"}))),
        labels: None,
        on_low_confidence: escalate(vec![broken], None).unwrap(),
    };
    let error = jeff
        .function("triage.urgency", options)
        .await
        .unwrap()
        .call(json!("Hm"))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::EscalationFailed);
    assert_eq!(
        error.message(),
        "Escalation step llm failed: Error: the model is down"
    );
    assert_eq!(error.step().unwrap(), "llm");
}

#[test]
fn a_context_field_given_by_its_exact_key_is_refused_before_routing() {
    let exact = IndexMap::from([(
        "counterparty name".to_owned(),
        "counterparty_name".to_owned(),
    )]);
    let error = refuse_exact_keys(
        &exact,
        "jeff.core.router",
        &ctx(json!({"counterparty name": "Acme"})),
    )
    .unwrap_err();
    assert_eq!(
        error.message(),
        "context is given by code name; pass 'counterparty name' as counterparty_name (fields of \
        adapters reached from router jeff.core.router)"
    );
    assert!(
        exact_keys_of(&Catalogue::shipped().unwrap(), "jeff.safety.spam")
            .unwrap()
            .is_empty()
    );
}

fn asking(answer: &'static str, asked: &Arc<Mutex<Vec<Question>>>) -> jeff_sdk::Step {
    let asked = Arc::clone(asked);
    human(move |question: Question| {
        asked.lock().unwrap().push(question);
        async move { Ok::<String, BoxError>(answer.into()) }
    })
}

fn spaced_context() -> JsonMap {
    ctx(json!({"ten": "x", "deal_id": "D-7", "counterparty_name": "Acme Ltd"}))
}

#[tokio::test]
async fn a_questions_state_entries_keep_the_order_the_model_reads() {
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(|_, _| vec![0.5, 0.5], ["jeff.safety.spam"])),
        catalogue_for("spaced"),
    )
    .await
    .unwrap();
    let asked = Arc::new(Mutex::new(Vec::new()));
    let options = CallOptions {
        context: Some(spaced_context()),
        labels: None,
        on_low_confidence: escalate(vec![asking("false", &asked)], None).unwrap(),
    };
    let decision = jeff
        .function("spam.is_spam", options)
        .await
        .unwrap()
        .call(json!("Please sign"))
        .await
        .unwrap();
    assert_eq!(decision.decided_by, "human");
    let keys: Vec<String> = asked.lock().unwrap()[0]
        .state_entries()
        .unwrap()
        .iter()
        .map(|(key, _)| (*key).to_owned())
        .collect();
    assert_eq!(keys, ["counterparty name", "Deal ID", "10", "message"]);
}

#[tokio::test]
async fn a_questions_option_entries_keep_option_order() {
    let mut raw = fixture("catalogues.json")["variants"]["spaced"][0].clone();
    raw["functions"]["message_type"]["labels"]["fixed"] =
        json!({"other": "None of these", "10": "Ten"});
    raw["functions"]["message_type"]["abstain_labels"] = json!(["other"]);
    let manifest =
        Arc::new(manifest_from_json(&raw, "test/number-like-labels/jeff.safety.spam").unwrap());
    let catalogue = Arc::new(
        Catalogue::shipped()
            .unwrap()
            .replacing(vec![manifest])
            .unwrap(),
    );
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(|_, _| vec![0.9, 0.1], ["jeff.safety.spam"])),
        catalogue,
    )
    .await
    .unwrap();
    let asked = Arc::new(Mutex::new(Vec::new()));
    let options = CallOptions {
        context: Some(spaced_context()),
        labels: None,
        on_low_confidence: escalate(vec![asking("10", &asked)], None).unwrap(),
    };
    let decision = jeff
        .function("spam.message_type", options)
        .await
        .unwrap()
        .call(json!("Please sign"))
        .await
        .unwrap();
    assert_eq!(decision.value, jeff_sdk::Value::Str("10".into()));
    assert_eq!(
        asked.lock().unwrap()[0].option_entries(),
        [("other", Some("None of these")), ("10", Some("Ten"))]
    );
}

struct Flaky {
    down: Mutex<bool>,
}

#[async_trait]
impl Engine for Flaky {
    fn base_format(&self) -> BaseFormat {
        BaseFormat::bf16()
    }
    async fn installed(&self, _refresh: bool) -> Result<BTreeSet<String>> {
        Ok([
            "jeff.support.triage".to_owned(),
            "jeff.safety.spam".to_owned(),
        ]
        .into())
    }
    async fn predict(&self, adapter_id: &str, rows: &[Row]) -> Result<EngineResult> {
        if *self.down.lock().unwrap() {
            return Err(JeffError::new(
                ErrorType::EngineError,
                format!("{adapter_id} is down"),
            ));
        }
        Ok(EngineResult {
            probabilities: rows.iter().map(|_| vec![0.5, 0.5, 0.0, 0.0, 0.0]).collect(),
            timing: JsonMap::new(),
            input_tokens: None,
        })
    }
}

#[tokio::test]
async fn a_predict_that_fails_is_reported_before_a_later_adapters_rows_fail_to_build() {
    // the "text" catalogue's spam functions take plain text, so a number as input fails when its row is built
    let engine = Arc::new(Flaky {
        down: Mutex::new(true),
    });
    let jeff = Jeff::new(engine.clone(), catalogue_for("text"))
        .await
        .unwrap();
    let urgency = Call::new("triage.urgency", json!("Hm"))
        .with_context(ctx(json!({"company": "A shop.", "channel": "email"})));
    let spam = Call::new("spam.is_spam", json!(5));
    let calls = [urgency, spam];
    assert_eq!(
        jeff.decide(&calls).await.unwrap_err().message(),
        "jeff.support.triage is down"
    );
    *engine.down.lock().unwrap() = false;
    assert_eq!(
        jeff.decide(&calls).await.unwrap_err().kind(),
        ErrorType::InvalidRequest
    );
}

#[tokio::test]
async fn an_unknown_policy_name_is_refused_with_pythons_words() {
    let error = jeff_sdk::Policy::named("maybe").unwrap_err();
    assert_eq!(error.kind(), ErrorType::InvalidPolicy);
    assert_eq!(
        error.message(),
        "Unknown on_low_confidence policy 'maybe'; use 'return', 'error' or escalate(...)"
    );
    assert_eq!(
        escalate(vec![], None).unwrap_err().message(),
        "escalate() needs at least one step"
    );
}

#[tokio::test]
async fn an_ask_failing_with_a_jeff_error_is_described_by_its_type_as_python_does() {
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(unsure, ["jeff.support.triage"])),
        Catalogue::shipped().unwrap(),
    )
    .await
    .unwrap();
    let refusing = human(|_question: Question| async {
        Err::<String, BoxError>(Box::new(JeffError::new(
            ErrorType::NotInstalled,
            "jeff.x.y.z is not installed: test. Install it with: jeff pull jeff.x.y",
        )))
    });
    let options = CallOptions {
        context: Some(ctx(json!({"company": "A shop.", "channel": "email"}))),
        labels: None,
        on_low_confidence: escalate(vec![refusing], None).unwrap(),
    };
    let error = jeff
        .function("triage.urgency", options)
        .await
        .unwrap()
        .call(json!("Hm"))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::EscalationFailed);
    assert_eq!(
        error.message(),
        "Escalation step human failed: NotInstalled: jeff.x.y.z is not installed: test. Install it with: jeff pull \
         jeff.x.y"
    );
}
