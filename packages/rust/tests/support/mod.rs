//! What every integration test shares: the conformance fixtures, the catalogues they name, and errors and decisions
//! written as the fixtures write them.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::manifest::manifest_from_json;
use jeff_sdk::{JeffError, Json, JsonMap, shipped};

pub const ELAPSED: &str = "<elapsed>";

/// A file of contract/conformance/, read once (objects keep the file's key order).
pub fn fixture(name: &str) -> &'static Json {
    static FILES: OnceLock<Mutex<HashMap<String, &'static Json>>> = OnceLock::new();
    let mut files = FILES.get_or_init(Default::default).lock().unwrap();
    files.entry(name.to_owned()).or_insert_with(|| {
        let path = shipped::repository_root()
            .join("contract/conformance")
            .join(name);
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("{}: {error}", path.display()));
        Box::leak(Box::new(serde_json::from_str(&text).unwrap()))
    })
}

/// A case's catalogue: the shipped one, or the shipped one with a variant's manifests in place (catalogues.json).
pub fn catalogue_for(name: &str) -> Arc<Catalogue> {
    static MADE: OnceLock<Mutex<HashMap<String, Arc<Catalogue>>>> = OnceLock::new();
    let mut made = MADE.get_or_init(Default::default).lock().unwrap();
    if let Some(found) = made.get(name) {
        return Arc::clone(found);
    }
    let shipped = Catalogue::shipped().unwrap();
    let catalogue = if name == "shipped" {
        shipped
    } else {
        let variant = fixture("catalogues.json")["variants"][name]
            .as_array()
            .unwrap_or_else(|| panic!("catalogues.json has no variant {name}"));
        let manifests = variant
            .iter()
            .map(|raw| {
                Arc::new(
                    manifest_from_json(
                        raw,
                        &format!("conformance/{name}/{}", raw["id"].as_str().unwrap()),
                    )
                    .unwrap(),
                )
            })
            .collect();
        Arc::new(shipped.replacing(manifests).unwrap())
    };
    made.insert(name.to_owned(), Arc::clone(&catalogue));
    catalogue
}

/// An error as the fixtures write it: `{"type", "message", "details"}`, details without request_id.
pub fn wire_of(error: &JeffError) -> Json {
    let mut details = error.details().clone();
    details.shift_remove("request_id");
    let mut wire = JsonMap::new();
    wire.insert("type".into(), Json::from(error.type_name()));
    wire.insert("message".into(), Json::from(error.message()));
    wire.insert("details".into(), Json::Object(details));
    normalised(&Json::Object(wire))
}

/// The value with timing.ms of every decision an llm or human step made written ELAPSED, as the recorder writes it.
pub fn normalised(value: &Json) -> Json {
    match value {
        Json::Array(items) => Json::Array(items.iter().map(normalised).collect()),
        Json::Object(map) => {
            let mut out: JsonMap = map
                .iter()
                .map(|(key, item)| (key.clone(), normalised(item)))
                .collect();
            let by_step = matches!(
                out.get("decided_by").and_then(Json::as_str),
                Some("llm" | "human")
            );
            if by_step
                && let Some(Json::Object(timing)) = out.get_mut("timing")
                && timing.contains_key("ms")
            {
                timing.insert("ms".into(), Json::from(ELAPSED));
            }
            Json::Object(out)
        }
        other => other.clone(),
    }
}

/// A predict's recorded probabilities as the engine sent them: JSON has no NaN or infinities, so decisions.json
/// writes those as the strings "NaN", "Infinity" and "-Infinity" (contract/socket.md, "The probabilities of a
/// predict"); this reads them back as numbers. Any other string is an error in the fixture.
pub fn engine_probabilities(predict: &Json) -> Vec<Vec<f64>> {
    predict["probabilities"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            row.as_array()
                .unwrap()
                .iter()
                .map(|value| match value {
                    Json::Number(_) => value.as_f64().unwrap(),
                    Json::String(text) => match text.as_str() {
                        "NaN" => f64::NAN,
                        "Infinity" => f64::INFINITY,
                        "-Infinity" => f64::NEG_INFINITY,
                        other => {
                            panic!("decisions.json: a probability \"{other}\" is not a number")
                        }
                    },
                    other => panic!("decisions.json: a probability {other} is not a number"),
                })
                .collect()
        })
        .collect()
}

pub fn b64(bytes: &[u8]) -> String {
    STANDARD.encode(bytes)
}

pub fn from_b64(text: &str) -> Vec<u8> {
    STANDARD.decode(text).unwrap()
}
pub mod channel_server;
pub mod decisions;
pub mod engine_server;
pub mod http_server;
pub mod proxy_server;
pub mod relay;

/// Every connection this test binary opens connects directly, whatever proxy variables the machine has set (only
/// tests/proxy.rs uses proxies, and sets its own).
pub fn no_proxies() {
    jeff_sdk::testing::pin_proxy_environment(Some(Vec::new()));
}
