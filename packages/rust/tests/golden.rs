//! contract/golden/step1.json against a real engine: `jeff runtime serve` running on this machine (it needs the
//! weights; run on the Mac or the RTX). Run it with
//!   JEFF_ENGINE_SOCKET=/tmp/jeff.sock JEFF_GOLDEN_ADAPTERS=jeff.safety.spam,jeff.support.triage \
//!     cargo test --test golden -- --ignored
//! JEFF_ENGINE_SOCKET is the engine's socket; JEFF_GOLDEN_ADAPTERS the comma-separated adapter ids it serves whose
//! golden cases to check. Both are required: a missing one fails the run.

use jeff_sdk::testing::{GoldenCase, golden_problems};
use jeff_sdk::{Jeff, Json, LocalOptions, shipped};

#[tokio::test]
#[ignore = "needs a running engine with the weights: see the comment at the top of this file"]
async fn golden_cases_against_a_real_engine() {
    let socket = std::env::var("JEFF_ENGINE_SOCKET")
        .expect("JEFF_ENGINE_SOCKET (the socket of a running `jeff runtime serve`) is required");
    let adapters = std::env::var("JEFF_GOLDEN_ADAPTERS")
        .expect("JEFF_GOLDEN_ADAPTERS (comma-separated adapter ids that engine serves, whose golden cases to check) is required");
    let path = shipped::repository_root().join("contract/golden/step1.json");
    let golden: Json = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let cases: Vec<GoldenCase> = golden["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| GoldenCase::from_json(case).unwrap())
        .collect();
    let jeff = Jeff::local(&socket, LocalOptions::new(600_000))
        .await
        .unwrap();
    let mut problems = Vec::new();
    let mut checked = 0;
    for adapter in adapters.split(',').map(str::trim) {
        let mine: Vec<&GoldenCase> = cases
            .iter()
            .filter(|case| {
                jeff.catalogue()
                    .function(&case.call.function)
                    .unwrap()
                    .adapter_id
                    == adapter
            })
            .collect();
        assert!(
            !mine.is_empty(),
            "contract/golden/step1.json has no case for {adapter}"
        );
        let calls: Vec<_> = mine.iter().map(|case| case.call.clone()).collect();
        let decisions = jeff.decide(&calls).await.unwrap();
        for (case, decision) in mine.iter().zip(&decisions) {
            let function = jeff.catalogue().function(&case.call.function).unwrap();
            let threshold = jeff
                .calibration_of(&function)
                .unwrap()
                .map(|calibration| calibration.threshold);
            for problem in golden_problems(case, decision, threshold) {
                problems.push(format!("{}: {problem}", case.name));
            }
            checked += 1;
        }
    }
    jeff.close().await;
    assert!(problems.is_empty(), "{}", problems.join("\n"));
    eprintln!("{checked} golden cases match");
}
