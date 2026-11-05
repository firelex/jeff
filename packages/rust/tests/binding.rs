use std::sync::Arc;

use jeff_sdk::ErrorType;
use jeff_sdk::binding::{Call, Criteria, Labels, Row, RowState, bind, build_row, option_keys};
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::engine::{BaseFormat, format_id_of};
use jeff_sdk::manifest::manifest_from_json;
use jeff_sdk::msgpack::pack;
use serde_json::json;

#[test]
fn number_like_fixed_labels_reach_the_row_in_manifest_order() {
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
    let context = json!({"channel": "sms"}).as_object().unwrap().clone();
    let binding = bind(&manifest, &function, Some(&context), None, None).unwrap();
    assert_eq!(option_keys(&binding), ["2", "1", "legit"]);
    let row = build_row(&binding, &json!("hi")).unwrap();
    let Some(Criteria::Described(criteria)) = &row.question.criteria else {
        panic!("described criteria")
    };
    assert_eq!(criteria.keys().collect::<Vec<_>>(), ["2", "1", "legit"]);
    let packed = pack(&row.to_json()).unwrap();
    let two = packed
        .windows(2)
        .position(|pair| pair == [0xa1, b'2'])
        .unwrap();
    let one = packed
        .windows(2)
        .position(|pair| pair == [0xa1, b'1'])
        .unwrap();
    assert!(two < one);
}

#[test]
fn labels_and_calls_read_from_json() {
    assert_eq!(
        Labels::from_json(&json!(["a", "b"])).unwrap(),
        Labels::Keys(vec!["a".into(), "b".into()])
    );
    let described = Labels::from_json(&json!({"x": "An x.", "y": null})).unwrap();
    assert_eq!(described.to_json(), json!({"x": "An x.", "y": null}));
    assert_eq!(
        Labels::from_json(&json!(5)).unwrap_err().kind(),
        ErrorType::InvalidLabels
    );
    let call = Call::from_json(&json!({"function": "spam.is_spam", "input": "Hi", "context": {"channel": "sms"}, "labels": null})).unwrap();
    assert_eq!(call.function, "spam.is_spam");
    assert!(call.labels.is_none());
    assert_eq!(
        Call::from_json(&json!({"function": "f"}))
            .unwrap_err()
            .kind(),
        ErrorType::InvalidRequest
    );
}

#[test]
fn a_row_survives_its_json() {
    let row = Row::from_json(&json!({"state": {"channel": "sms", "message": "WIN"}, "question": {"type": "noul", "instructions": "Spam?"}})).unwrap();
    assert!(matches!(row.state, RowState::Fields(_)));
    assert_eq!(
        row.to_json(),
        json!({"state": {"channel": "sms", "message": "WIN"}, "question": {"type": "noul", "instructions": "Spam?"}})
    );
    assert_eq!(
        Row::from_json(&json!({"state": 5, "question": {}}))
            .unwrap_err()
            .kind(),
        ErrorType::InvalidRequest
    );
}

#[test]
fn a_base_format_names_its_weights() {
    assert_eq!(format_id_of(&BaseFormat::bf16()).unwrap(), None);
    let int4 =
        BaseFormat::from_json(&json!({"format": "int4-g64", "format_id": "jeff-base@v1.3+int4"}))
            .unwrap();
    assert_eq!(
        format_id_of(&int4).unwrap().as_deref(),
        Some("jeff-base@v1.3+int4")
    );
    let error = format_id_of(&BaseFormat {
        format: "int4-g64".into(),
        format_id: None,
    })
    .unwrap_err();
    assert_eq!(
        error.message(),
        "The engine's base format {'format': 'int4-g64'} names no format_id; every base format other \
        than bf16 is one exact set of weights, named by its format_id"
    );
}
