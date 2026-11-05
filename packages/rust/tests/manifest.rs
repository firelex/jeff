mod support;

use std::sync::Arc;

use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::ids::{parse_ref, version_matches};
use jeff_sdk::manifest::{calibration_for, code_name_of, manifest_from_json, unversioned_id};
use jeff_sdk::{ErrorType, Json};
use serde_json::json;

fn spam_raw() -> Json {
    Catalogue::shipped()
        .unwrap()
        .adapter("jeff.safety.spam")
        .unwrap()
        .raw
        .clone()
}

#[test]
fn code_names_follow_the_rule() {
    for (key, code) in [
        ("channel", "channel"),
        ("counterparty name", "counterparty_name"),
        ("Deal ID", "deal_id"),
        ("outreach  log -- v2", "outreach_log_v2"),
        ("_odd_", "odd"),
        ("clause type?", "clause_type"),
        ("company_", "company_"),
        ("a__b", "a__b"),
        ("x9_", "x9_"),
    ] {
        assert_eq!(code_name_of(key), code, "{key}");
    }
}

#[test]
fn the_unversioned_id_removes_the_one_version_segment_as_in_python() {
    let cases = [
        (
            "jeff.example.router-v14-fast",
            Some("jeff.example.router-fast"),
        ),
        (
            "jeff.routing.router-open-v14",
            Some("jeff.routing.router-open"),
        ),
        ("jeff.v2.router-v3", Some("jeff.v2.router")),
        ("jeff.support.triage", None),
        ("jeff.routing.v14", None),
        ("jeff.routing.router-v14-v15", None),
        ("jeff.routing.router-v", None),
        ("jeff.routing.router-v1x", None),
        ("jeff.routing.router-14", None),
        ("jeff.routing.router-open-v14\n", None),
    ];
    for (adapter_id, unversioned) in cases {
        assert_eq!(
            unversioned_id(adapter_id).as_deref(),
            unversioned,
            "{adapter_id}"
        );
    }
}

#[test]
fn references_parse_as_in_python() {
    let parsed = parse_ref("jeff.safety.spam.is_spam@1.3").unwrap();
    assert_eq!(
        (
            parsed.adapter.as_str(),
            parsed.key.as_deref(),
            parsed.version.as_deref()
        ),
        ("jeff.safety.spam", Some("is_spam"), Some("1.3"))
    );
    let alias = parse_ref("spam").unwrap();
    assert_eq!(
        (alias.adapter.as_str(), alias.key, alias.version),
        ("spam", None, None)
    );
    let error = parse_ref("jeff.x").unwrap_err();
    assert_eq!(error.kind(), ErrorType::InvalidReference);
    assert_eq!(
        error.message(),
        "Malformed reference 'jeff.x': use jeff.<domain>.<name>[.<function>]"
    );
    assert!(version_matches("1.3", "1.3.0"));
    assert!(!version_matches("1.30", "1.3.0"));
}

#[test]
fn a_number_like_fixed_label_key_keeps_its_yaml_order() {
    let mut raw = spam_raw();
    raw["functions"]["message_type"]["labels"]["fixed"] =
        json!({"2": "Two", "1": "One", "legit": "Fine"});
    let manifest = manifest_from_json(&raw, "test").unwrap();
    let keys: Vec<&String> = manifest.functions["message_type"]
        .labels
        .fixed
        .keys()
        .collect();
    assert_eq!(keys, ["2", "1", "legit"]);
}

#[test]
fn a_key_with_no_usable_code_name_must_declare_one() {
    let mut raw = spam_raw();
    raw["context"] = json!([{"key": "2nd party", "description": "A field.", "required": true}]);
    let error = manifest_from_json(&raw, "test").unwrap_err();
    assert_eq!(error.kind(), ErrorType::ManifestError);
    assert!(
        error.message().contains("'2nd party'")
            && error.message().contains("declare its code_name"),
        "{}",
        error.message()
    );
}

#[test]
fn the_catalogue_resolves_aliases_keys_and_versions() {
    let catalogue = Catalogue::shipped().unwrap();
    assert_eq!(
        catalogue.function("spam.is_spam").unwrap().id,
        "jeff.safety.spam.is_spam"
    );
    assert_eq!(
        catalogue.function("spam").unwrap_err().kind(),
        ErrorType::UnknownFunction
    );
    let equivalent: Vec<String> = catalogue
        .implementing("equivalent")
        .iter()
        .map(|function| function.id.clone())
        .collect();
    assert_eq!(equivalent, ["jeff.compliance.sanctions.screening"]);
    let spam = catalogue.function("spam.is_spam").unwrap();
    assert!(calibration_for(&spam, None).unwrap().unwrap().threshold > 0.0);
    assert_eq!(
        catalogue.function("spam.is_spam@2").unwrap_err().message(),
        "jeff.safety.spam@2 is not in the catalogue; it has 1.4.0"
    );
}

#[test]
fn id_patterns_must_each_be_a_regular_expression() {
    let mut raw = spam_raw();
    raw["tests"]["function_by"] = json!("id_pattern");
    raw["tests"]["id_patterns"] = json!({"is_spam": "[", "message_type": ".*"});
    let error = manifest_from_json(&raw, "test").unwrap_err();
    assert_eq!(error.kind(), ErrorType::ManifestError);
    assert!(
        error
            .message()
            .contains("id_patterns is_spam: '[' is not a regular expression"),
        "{}",
        error.message()
    );
}

#[test]
fn a_base_is_looked_up_by_id_and_version() {
    let catalogue = Catalogue::shipped().unwrap();
    assert_eq!(
        catalogue.base("jeff-base", "v1.4").unwrap()["repo"],
        json!("mstrasser/jeff-base")
    );
    assert_eq!(
        catalogue.base("jeff-base", "v9").unwrap_err().kind(),
        ErrorType::ManifestError
    );
}

#[test]
fn replacing_keeps_each_adapter_in_its_place() {
    let shipped = Catalogue::shipped().unwrap();
    let before: Vec<String> = shipped
        .manifests()
        .iter()
        .map(|manifest| manifest.id.clone())
        .collect();
    let spam = Arc::new(manifest_from_json(&spam_raw(), "again").unwrap());
    let replaced = shipped.replacing(vec![spam]).unwrap();
    let after: Vec<String> = replaced
        .manifests()
        .iter()
        .map(|manifest| manifest.id.clone())
        .collect();
    assert_eq!(after, before);
    assert_eq!(replaced.adapter("jeff.safety.spam").unwrap().path, "again");
}

#[test]
fn a_catalogue_folder_reads_as_the_shipped_copy() {
    let folder = jeff_sdk::shipped::repository_root().join("catalogue");
    let read = Catalogue::from_dir(&folder).unwrap();
    let shipped = Catalogue::shipped().unwrap();
    let raws = |catalogue: &Catalogue| -> Vec<Json> {
        catalogue
            .manifests()
            .iter()
            .map(|manifest| manifest.raw.clone())
            .collect()
    };
    assert_eq!(raws(&read), raws(&shipped));
    assert_eq!(read.base_versions(), shipped.base_versions());
    let empty = tempfile::tempdir().unwrap();
    let error = Catalogue::from_dir(empty.path()).unwrap_err();
    assert_eq!(error.kind(), ErrorType::ManifestError);
    assert_eq!(
        error.message(),
        format!("{} is missing", empty.path().join("bases.yaml").display())
    );
}
