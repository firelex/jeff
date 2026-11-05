//! rows.json: the exact engine row the Python SDK builds for each call, or the exact error it raises.

mod support;

use jeff_sdk::binding::{Call, bind, build_row};
use jeff_sdk::engine::{BaseFormat, format_id_of};
use jeff_sdk::manifest::calibration_for;
use jeff_sdk::msgpack::pack;
use jeff_sdk::{Json, Result};
use serde_json::json;
use support::{b64, catalogue_for, fixture, wire_of};

fn row_of(case: &Json) -> Result<Json> {
    let catalogue = catalogue_for(case["catalogue"].as_str().unwrap());
    let call = Call::from_json(&case["call"])?;
    let function = catalogue.function(&call.function)?;
    let manifest = catalogue.manifest_of(&function)?;
    let format_id = format_id_of(&BaseFormat::from_json(&case["base_format"])?)?;
    let calibration = calibration_for(&function, format_id.as_deref())?;
    let row = build_row(
        &bind(
            &manifest,
            &function,
            call.context.as_ref(),
            call.labels.as_ref(),
            calibration,
        )?,
        &call.input,
    )?;
    let row = row.to_json();
    Ok(json!({"adapter": manifest.id, "row": row, "row_msgpack": b64(&pack(&row).unwrap())}))
}

#[test]
fn every_rows_case_is_built_as_the_python_sdk_builds_it() {
    let mut failures = Vec::new();
    let cases = fixture("rows.json")["cases"].as_array().unwrap();
    for case in cases {
        let got = match row_of(case) {
            Ok(row) => row,
            Err(error) => json!({"error": wire_of(&error)}),
        };
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
