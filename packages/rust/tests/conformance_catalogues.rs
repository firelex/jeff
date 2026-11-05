mod support;

use jeff_sdk::catalogue::Catalogue;
use support::{catalogue_for, fixture};

#[test]
fn the_shipped_manifests_read_as_the_python_sdk_reads_them() {
    let file = fixture("catalogues.json");
    let shipped = Catalogue::shipped().unwrap();
    let mut ids: Vec<String> = shipped
        .manifests()
        .iter()
        .map(|manifest| manifest.id.clone())
        .collect();
    ids.sort();
    let mut expected: Vec<String> = file["shipped"]["manifests"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    expected.sort();
    assert_eq!(ids, expected);
    for manifest in shipped.manifests() {
        assert_eq!(
            manifest.raw, file["shipped"]["manifests"][&manifest.id],
            "{}",
            manifest.id
        );
    }
}

#[test]
fn every_variant_loads() {
    let file = fixture("catalogues.json");
    for (name, manifests) in file["variants"].as_object().unwrap() {
        let catalogue = catalogue_for(name);
        for raw in manifests.as_array().unwrap() {
            assert_eq!(
                catalogue.adapter(raw["id"].as_str().unwrap()).unwrap().raw,
                *raw,
                "{name}"
            );
        }
    }
}
