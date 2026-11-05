//! The decide cases of one call and one predict, replayed without the client: bind, build the row (its MessagePack
//! must be the recorded one), make the decision from the probabilities the engine answered. Task 10 replays every
//! case through the client.

mod support;

use jeff_sdk::binding::{Call, bind, build_row};
use jeff_sdk::decision::make_decision;
use jeff_sdk::engine::{BaseFormat, format_id_of};
use jeff_sdk::manifest::calibration_for;
use jeff_sdk::msgpack::pack;
use jeff_sdk::{Json, Result};
use serde_json::json;
use support::{b64, catalogue_for, engine_probabilities, fixture, wire_of};

fn replay(case: &Json) -> Result<Json> {
    let catalogue = catalogue_for(case["catalogue"].as_str().unwrap());
    let call = Call::from_json(&case["args"]["calls"][0])?;
    let predict = &case["predicts"][0];
    let function = catalogue.function(&call.function)?;
    let format_id = format_id_of(&BaseFormat::from_json(&case["base_format"])?)?;
    let binding = bind(
        &catalogue.manifest_of(&function)?,
        &function,
        call.context.as_ref(),
        call.labels.as_ref(),
        calibration_for(&function, format_id.as_deref())?,
    )?;
    let row = build_row(&binding, &call.input)?;
    assert_eq!(
        b64(&pack(&Json::Array(vec![row.to_json()])).unwrap()),
        predict["rows_msgpack"].as_str().unwrap(),
        "{}",
        case["name"]
    );
    let probabilities = engine_probabilities(predict).swap_remove(0);
    let decision = make_decision(
        &binding,
        &probabilities,
        predict["timing"].as_object().unwrap(),
    )?;
    Ok(json!({"result": [decision.to_json()?]}))
}

#[test]
fn every_single_call_decide_case_decides_as_the_python_sdk_decides() {
    let cases: Vec<&Json> = fixture("decisions.json")["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| {
            case["op"] == "decide"
                && case["args"]["calls"].as_array().unwrap().len() == 1
                && case["predicts"].as_array().unwrap().len() == 1
        })
        .collect();
    assert!(cases.len() > 150);
    let mut failures = Vec::new();
    for case in &cases {
        let got = replay(case).unwrap_or_else(|error| json!({"error": wire_of(&error)}));
        if got != case["expect"] {
            failures.push(format!(
                "{}\n  got      {got}\n  expected {}",
                case["name"], case["expect"]
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
