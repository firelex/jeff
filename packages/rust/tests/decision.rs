use std::sync::Arc;

use jeff_sdk::binding::bind;
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::decision::{
    Decision, EscalationRecord, RouteStep, Value, low_confidence, make_decision,
};
use jeff_sdk::manifest::manifest_from_json;
use jeff_sdk::{ErrorType, JsonMap};
use serde_json::json;

fn ctx(value: serde_json::Value) -> JsonMap {
    value.as_object().unwrap().clone()
}

#[test]
fn a_tie_goes_to_the_first_option_in_option_order_and_probabilities_keep_that_order() {
    let mut raw = Catalogue::shipped()
        .unwrap()
        .adapter("jeff.safety.spam")
        .unwrap()
        .raw
        .clone();
    raw["functions"]["message_type"]["labels"]["fixed"] =
        json!({"2": "Two", "1": "One", "legit": "Fine"});
    let manifest = Arc::new(manifest_from_json(&raw, "test").unwrap());
    let function = Arc::clone(&manifest.functions["message_type"]);
    let binding = bind(
        &manifest,
        &function,
        Some(&ctx(json!({"channel": "sms"}))),
        None,
        None,
    )
    .unwrap();
    let decision = make_decision(&binding, &[0.4, 0.4, 0.2], &JsonMap::new()).unwrap();
    assert_eq!(decision.value, Value::Str("2".into()));
    assert_eq!(
        decision.probabilities.keys().collect::<Vec<_>>(),
        ["2", "1", "legit"]
    );
    let written = decision.to_json().unwrap();
    assert_eq!(
        written["probabilities"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        ["2", "1", "legit"]
    );
}

#[test]
fn probabilities_are_normalised_with_pythons_sum() {
    let catalogue = Catalogue::shipped().unwrap();
    let function = catalogue.function("triage.urgency").unwrap();
    let binding = bind(
        &catalogue.manifest_of(&function).unwrap(),
        &function,
        Some(&ctx(json!({"company": "c", "channel": "e"}))),
        None,
        function.calibration.clone(),
    )
    .unwrap();
    let decision = make_decision(&binding, &[0.2; 5], &JsonMap::new()).unwrap();
    assert_eq!(decision.value, Value::Int(0));
    assert_eq!(decision.confidence, Some(0.2));
    assert!((decision.score.unwrap() - 2.0).abs() < 1e-12);
}

#[test]
fn an_engine_answer_of_the_wrong_length_or_with_no_mass_is_refused() {
    let catalogue = Catalogue::shipped().unwrap();
    let function = catalogue.function("spam.is_spam").unwrap();
    let binding = bind(
        &catalogue.manifest_of(&function).unwrap(),
        &function,
        Some(&ctx(json!({"channel": "sms"}))),
        None,
        function.calibration.clone(),
    )
    .unwrap();
    let short = make_decision(&binding, &[0.5], &JsonMap::new()).unwrap_err();
    assert_eq!(
        (short.kind(), short.message()),
        (
            ErrorType::EngineError,
            "jeff.safety.spam.is_spam: the engine returned 1 probabilities for 2 options"
        )
    );
    assert_eq!(
        make_decision(&binding, &[0.0, 0.0], &JsonMap::new())
            .unwrap_err()
            .message(),
        "jeff.safety.spam.is_spam: the engine returned invalid probabilities [0.0, 0.0]; expected their sum to be \
         within 0.0001 of 1, got 0.0"
    );
}

#[test]
fn a_decision_survives_its_json_and_low_confidence_carries_it() {
    let catalogue = Catalogue::shipped().unwrap();
    let function = catalogue.function("spam.is_spam").unwrap();
    let binding = bind(
        &catalogue.manifest_of(&function).unwrap(),
        &function,
        Some(&ctx(json!({"channel": "sms"}))),
        None,
        function.calibration.clone(),
    )
    .unwrap();
    let decision = make_decision(&binding, &[0.47, 0.53], &ctx(json!({"path": "fake"}))).unwrap();
    assert_eq!(
        Decision::from_json(&decision.to_json().unwrap()).unwrap(),
        decision
    );
    let error = low_confidence(&decision).unwrap();
    assert_eq!(error.kind(), ErrorType::LowConfidence);
    assert_eq!(
        error.message(),
        "jeff.safety.spam.is_spam abstained (confidence 0.53); on_low_confidence='error'"
    );
    assert_eq!(error.decision().unwrap(), decision);
    assert_eq!(
        Decision::from_json(&json!({"value": 1}))
            .unwrap_err()
            .kind(),
        ErrorType::ContractError
    );
}

fn spam_binding() -> jeff_sdk::binding::Binding {
    let catalogue = Catalogue::shipped().unwrap();
    let function = catalogue.function("spam.is_spam").unwrap();
    bind(
        &catalogue.manifest_of(&function).unwrap(),
        &function,
        Some(&ctx(json!({"channel": "sms"}))),
        None,
        function.calibration.clone(),
    )
    .unwrap()
}

#[test]
fn a_confidence_exactly_at_the_threshold_decides_and_one_just_below_abstains() {
    // Values printed by the Python SDK: confidence 0.53 abstains only when the threshold is above it.
    let mut binding = spam_binding();
    let threshold = |binding: &mut jeff_sdk::binding::Binding, value: f64| {
        binding.calibration.as_mut().unwrap().threshold = value;
    };
    threshold(&mut binding, 0.53);
    let at = make_decision(&binding, &[0.47, 0.53], &JsonMap::new()).unwrap();
    assert_eq!(
        (at.confidence, at.abstained, at.calibrated),
        (Some(0.53), false, true)
    );
    threshold(&mut binding, 0.5300000000000001);
    assert!(
        make_decision(&binding, &[0.47, 0.53], &JsonMap::new())
            .unwrap()
            .abstained
    );
    binding.calibration = None;
    let uncalibrated = make_decision(&binding, &[0.47, 0.53], &JsonMap::new()).unwrap();
    assert_eq!(
        (uncalibrated.abstained, uncalibrated.calibrated),
        (false, false)
    );
}

#[test]
fn a_nan_or_infinite_probability_is_refused_by_name_never_clamped() {
    let binding = spam_binding();
    for (answer, shown, offending) in [
        ([f64::NAN, 0.5], "[nan, 0.5]", "[nan]"),
        ([f64::INFINITY, 0.5], "[inf, 0.5]", "[inf]"),
        ([-0.25, 0.9], "[-0.25, 0.9]", "[-0.25]"),
    ] {
        let error = make_decision(&binding, &answer, &JsonMap::new()).unwrap_err();
        assert_eq!(
            (error.kind(), error.message().to_owned()),
            (
                ErrorType::EngineError,
                format!(
                    "jeff.safety.spam.is_spam: the engine returned invalid probabilities {shown}; expected each to \
                     be a finite number from 0 to 1, which {offending} are not"
                )
            )
        );
    }
}

/// The text printed by the Python SDK, `json.dumps(decision.to_json())`, for `hand_built(0.5, 0.25, None, 0.75, 0.5)`.
const PYTHON_TEXT: &str = r#"{"value": 1, "confidence": 0.5, "abstained": true, "calibrated": true, "interface": "rate", "function": "f.g", "version": "1", "probabilities": {"a": 0.25, "b\u00e9\ud83d\ude00\"\u007f": 0.75}, "score": null, "route": [{"router": "r", "chose": "c", "confidence": 0.75}], "decided_by": "f.g@1", "escalations": [{"decided_by": "y", "value": "k", "confidence": 0.5, "reason": "z\n"}], "timing": {"path": "fake", "model_ms": 0.0, "n": 3, "x": 1e-07}, "evidence_refs": []}"#;

fn hand_built(
    confidence: f64,
    p: f64,
    score: Option<f64>,
    route: f64,
    escalation: f64,
) -> Decision {
    Decision {
        value: Value::Int(1),
        confidence: Some(confidence),
        abstained: true,
        calibrated: true,
        interface: "rate".into(),
        function: "f.g".into(),
        version: "1".into(),
        probabilities: [
            ("a".to_owned(), p),
            ("b\u{e9}\u{1F600}\"\u{7f}".to_owned(), 0.75),
        ]
        .into_iter()
        .collect(),
        score,
        route: vec![RouteStep {
            router: "r".into(),
            chose: "c".into(),
            confidence: route,
        }],
        decided_by: "f.g@1".into(),
        escalations: vec![EscalationRecord {
            decided_by: "y".into(),
            value: Value::Str("k".into()),
            confidence: Some(escalation),
            reason: "z\n".into(),
        }],
        timing: ctx(json!({"path": "fake", "model_ms": 0.0, "n": 3, "x": 1e-7})),
        evidence_refs: vec![],
    }
}

#[test]
fn a_decision_writes_its_text_as_python_does_and_names_a_number_json_cannot_hold() {
    assert_eq!(
        hand_built(0.5, 0.25, None, 0.75, 0.5).to_json_text(),
        PYTHON_TEXT
    );
    for (number, text) in [
        (f64::NAN, "NaN"),
        (f64::INFINITY, "Infinity"),
        (f64::NEG_INFINITY, "-Infinity"),
    ] {
        let fields = [
            (
                hand_built(number, 0.25, None, 0.75, 0.5),
                "confidence",
                r#""confidence": 0.5, "abstained""#,
            ),
            (
                hand_built(0.5, number, None, 0.75, 0.5),
                "probabilities['a']",
                r#"{"a": 0.25,"#,
            ),
            (
                hand_built(0.5, 0.25, Some(number), 0.75, 0.5),
                "score",
                r#""score": null"#,
            ),
            (
                hand_built(0.5, 0.25, None, number, 0.5),
                "route[0].confidence",
                r#""confidence": 0.75}]"#,
            ),
            (
                hand_built(0.5, 0.25, None, 0.75, number),
                "escalations[0].confidence",
                r#""confidence": 0.5, "reason""#,
            ),
        ];
        for (decision, path, written) in fields {
            // Python writes the number as NaN, Infinity or -Infinity (json.dumps with its default allow_nan=True).
            let python = written.replacen(
                if path == "score" {
                    "null"
                } else if path == "probabilities['a']" {
                    "0.25"
                } else if path == "route[0].confidence" {
                    "0.75"
                } else {
                    "0.5"
                },
                text,
                1,
            );
            assert_eq!(
                decision.to_json_text(),
                PYTHON_TEXT.replacen(written, &python, 1),
                "{path} {text}"
            );
            let refused = decision.to_json().unwrap_err();
            let shown = jeff_sdk::pyformat::py_float(number);
            assert_eq!(
                (refused.kind(), refused.message().to_owned()),
                (
                    ErrorType::ContractError,
                    format!(
                        "The decision's {path} is {shown}, which JSON cannot hold; Decision::to_json_text writes it as \
                 Python's json.dumps does ({text})"
                    )
                )
            );
            assert_eq!(
                low_confidence(&decision).unwrap_err().message(),
                refused.message()
            );
        }
    }
}
