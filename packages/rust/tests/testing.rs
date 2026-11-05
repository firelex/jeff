use indexmap::IndexMap;
use jeff_sdk::binding::Call;
use jeff_sdk::binding::{Criteria, Row, RowQuestion, RowState};
use jeff_sdk::contract::QuestionType;
use jeff_sdk::decision::{Decision, Value};
use jeff_sdk::engine::{BaseFormat, Engine};
use jeff_sdk::testing::{FakeEngine, GoldenCase, fake_tokens, golden_problems};
use jeff_sdk::{ErrorType, JsonMap};
use serde_json::json;

fn row() -> Row {
    Row {
        state: RowState::Fields(
            json!({"channel": "sms", "message": "Win  a prize\tnow\n café x"})
                .as_object()
                .unwrap()
                .clone(),
        ),
        question: RowQuestion {
            question_type: QuestionType::Noul,
            instructions: "Is this spam?".into(),
            criteria: Some(Criteria::Described(IndexMap::from([
                ("false".into(), Some("Not spam.".into())),
                ("true".into(), Some("Spam.".into())),
            ]))),
        },
    }
}

#[tokio::test]
async fn the_fake_engine_answers_from_the_function_records_its_calls_and_refuses_an_adapter_it_does_not_serve()
 {
    let engine = FakeEngine::new(|_, _| vec![0.25, 0.75], ["jeff.safety.spam"]);
    let result = engine.predict("jeff.safety.spam", &[row()]).await.unwrap();
    assert_eq!(result.probabilities, vec![vec![0.25, 0.75]]);
    assert_eq!(
        serde_json::Value::Object(result.timing),
        json!({"path": "fake", "model_ms": 0.0, "format": "bf16"})
    );
    assert_eq!(result.input_tokens, Some(vec![22])); // printed by the Python SDK's jeff.testing.fake_tokens
    assert_eq!(
        engine.calls(),
        vec![("jeff.safety.spam".to_owned(), vec![row()])]
    );
    let refused = engine
        .predict("jeff.support.triage", &[row()])
        .await
        .unwrap_err();
    assert_eq!(
        refused.message(),
        "jeff.support.triage is not installed: FakeEngine has not loaded it. Install it with: jeff pull jeff.support.triage"
    );
    assert_eq!(fake_tokens(&row()), 22);
}

#[test]
fn the_fake_engine_refuses_a_malformed_base_format() {
    let error = FakeEngine::new(|_, _| vec![], Vec::<String>::new())
        .with_base_format(BaseFormat {
            format: "int4-g64".into(),
            format_id: None,
        })
        .err()
        .unwrap();
    assert_eq!(error.kind(), ErrorType::EngineError);
}

#[test]
fn golden_problems_words_each_difference_as_python_does() {
    let decision = Decision {
        value: Value::Bool(true),
        confidence: Some(0.6),
        abstained: false,
        calibrated: true,
        interface: "i".into(),
        function: "f".into(),
        version: "1".into(),
        probabilities: IndexMap::new(),
        score: None,
        route: vec![],
        decided_by: "x".into(),
        escalations: vec![],
        timing: JsonMap::new(),
        evidence_refs: vec![],
    };
    let case = |value: serde_json::Value, abstained: bool, confidence: f64| GoldenCase {
        name: "n".into(),
        call: Call::new("f", json!("x")),
        expected_value: value,
        expected_abstained: abstained,
        expected_confidence: confidence,
        tolerance: 0.05,
    };
    assert_eq!(
        golden_problems(&case(json!(1), true, 0.9), &decision, Some(0.7)),
        [
            "value True, expected 1",
            "confidence 0.6, expected 0.9 +/- 0.05",
            "abstained False, expected True (the recorded confidence is more than 0.05 from the threshold 0.7)",
        ]
    );
    assert_eq!(
        golden_problems(&case(json!(true), true, 0.62), &decision, None),
        [
            "abstained False, expected True (the recorded confidence is more than 0.05 from the threshold None)"
        ]
    );
    assert!(golden_problems(&case(json!(true), true, 0.62), &decision, Some(0.65)).is_empty());
}
