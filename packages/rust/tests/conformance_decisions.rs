//! decisions.json through the client: every group, every op, against a scripted engine.

mod support;

use support::decisions::replay_decision_case;
use support::fixture;

async fn replay_group(group: &str) {
    let cases: Vec<&serde_json::Value> = fixture("decisions.json")["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| case["group"] == group)
        .collect();
    assert!(!cases.is_empty(), "decisions.json has no {group} cases");
    let mut failures = Vec::new();
    for case in &cases {
        let problems = replay_decision_case(case).await;
        if !problems.is_empty() {
            failures.push(format!(
                "{}: {}\n  {}",
                group,
                case["name"],
                problems.join("\n  ")
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[tokio::test]
async fn the_decide_cases_replay() {
    replay_group("decide").await;
}

#[tokio::test]
async fn the_escalation_cases_replay() {
    replay_group("escalation").await;
}

#[tokio::test]
async fn the_dispatch_cases_replay() {
    replay_group("dispatch").await;
}

#[tokio::test]
async fn the_graph_cases_replay() {
    replay_group("graph").await;
}

#[test]
fn every_group_of_decisions_json_is_replayed() {
    let mut groups: Vec<String> = fixture("decisions.json")["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| case["group"].as_str().unwrap().to_owned())
        .collect();
    groups.sort();
    groups.dedup();
    assert_eq!(groups, ["decide", "dispatch", "escalation", "graph"]);
}
