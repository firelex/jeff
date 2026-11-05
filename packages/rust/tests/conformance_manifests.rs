//! contract/conformance/manifests.json: manifests every SDK refuses, read from their YAML text as a manifest file is
//! read (yaml::read_manifest_yaml, then manifest_from_json), each refused with exactly the Python SDK's error.

mod support;

use jeff_sdk::manifest::manifest_from_json;
use jeff_sdk::yaml::{read_manifest_yaml, read_yaml};
use support::fixture;

#[test]
fn each_manifest_is_refused_with_the_python_sdks_error() {
    let cases = fixture("manifests.json")["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 16);
    for case in cases {
        let path = case["path"].as_str().unwrap();
        let error = read_manifest_yaml(case["yaml"].as_str().unwrap(), path)
            .and_then(|raw| manifest_from_json(&raw, path))
            .unwrap_err();
        let expected = &case["expect"]["error"];
        assert_eq!(
            (error.type_name(), error.message()),
            (
                expected["type"].as_str().unwrap(),
                expected["message"].as_str().unwrap()
            ),
            "{}",
            case["name"]
        );
    }
}

#[test]
fn a_manifest_with_a_valid_access_is_read() {
    // access is for a server only: the SDK accepts it and does nothing with it
    let cases = fixture("manifests.json")["cases"].as_array().unwrap();
    let refused = cases
        .iter()
        .find(|case| case["name"] == "an access that is a string other than owner")
        .unwrap();
    let text = refused["yaml"].as_str().unwrap();
    for access in ["access: owner\n", "access: {accounts: [acct1, B-2_x]}\n"] {
        let yaml = text.replace("access: everyone\n", access);
        let raw = read_manifest_yaml(&yaml, "m.yaml").unwrap();
        manifest_from_json(&raw, "m.yaml").unwrap();
    }
}

#[test]
fn a_non_finite_number_outside_a_manifest_keeps_the_yaml_readers_wording() {
    assert_eq!(
        read_yaml("d: [1, .nan]\n", "doc.yaml")
            .unwrap_err()
            .message(),
        "doc.yaml is not valid YAML: line 1: '.nan' is not a finite number (nan), which JSON cannot hold; quote it"
    );
    assert_eq!(
        read_manifest_yaml("changelog:\n- note: -.inf\n", "m.yaml")
            .unwrap_err()
            .message(),
        "m.yaml: /changelog/0/note is -inf, not a finite number, which JSON cannot hold; write a finite number"
    );
    // a non-finite key is not a value: the YAML reader's wording
    assert!(
        read_manifest_yaml(".inf: x\n", "m.yaml")
            .unwrap_err()
            .message()
            .starts_with("m.yaml is not valid YAML: line 1: ")
    );
}
