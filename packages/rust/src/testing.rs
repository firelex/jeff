//! Test helpers for code that uses the crate: an engine whose answers come from a function you supply, and the
//! golden comparison (port of jeff.testing.FakeEngine and jeff.evaluate.same and golden_problems).

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;

use crate::binding::{Call, Row};
use crate::decision::Decision;
use crate::engine::{BaseFormat, Engine, EngineResult, format_id_of};
use crate::errors::{ErrorType, JeffError, Result, not_installed};
use crate::json::{Json, JsonMap};
use crate::pyformat::{py_float, py_json_dumps, py_repr};

type Answers = Arc<dyn Fn(&str, &Row) -> Vec<f64> + Send + Sync>;

/// An engine that answers each row with `answers(adapter_id, row)`, serves the adapters it was given, and records
/// every predict it was asked.
pub struct FakeEngine {
    answers: Answers,
    served: BTreeSet<String>,
    base_format: BaseFormat,
    calls: Mutex<Vec<(String, Vec<Row>)>>,
}

impl FakeEngine {
    /// An engine on a bfloat16 base that serves the adapters `installed` and answers each row with
    /// `answers(adapter_id, row)`. A predict for any other adapter raises NotInstalled.
    pub fn new(
        answers: impl Fn(&str, &Row) -> Vec<f64> + Send + Sync + 'static,
        installed: impl IntoIterator<Item = impl Into<String>>,
    ) -> FakeEngine {
        FakeEngine {
            answers: Arc::new(answers),
            served: installed.into_iter().map(Into::into).collect(),
            base_format: BaseFormat::bf16(),
            calls: Mutex::new(Vec::new()),
        }
    }

    /// The same engine reporting another base format (an INT4 one, say); a malformed format is refused here, as a
    /// real engine's base folder would be.
    pub fn with_base_format(mut self, base_format: BaseFormat) -> Result<FakeEngine> {
        format_id_of(&base_format)?;
        self.base_format = base_format;
        Ok(self)
    }

    /// Every predict asked so far, in order: the adapter id and the rows.
    pub fn calls(&self) -> Vec<(String, Vec<Row>)> {
        self.calls.lock().expect("the calls lock").clone()
    }
}

#[async_trait]
impl Engine for FakeEngine {
    fn base_format(&self) -> BaseFormat {
        self.base_format.clone()
    }

    async fn installed(&self, _refresh: bool) -> Result<BTreeSet<String>> {
        Ok(self.served.clone())
    }

    async fn predict(&self, adapter_id: &str, rows: &[Row]) -> Result<EngineResult> {
        if !self.served.contains(adapter_id) {
            return Err(not_installed(adapter_id, "FakeEngine has not loaded it"));
        }
        self.calls
            .lock()
            .expect("the calls lock")
            .push((adapter_id.to_owned(), rows.to_vec()));
        let mut timing = JsonMap::new();
        timing.insert("path".into(), Json::from("fake"));
        timing.insert("model_ms".into(), Json::from(0.0));
        if let Json::Object(format) = self.base_format.to_json() {
            timing.extend(format);
        }
        Ok(EngineResult {
            probabilities: rows
                .iter()
                .map(|row| (self.answers)(adapter_id, row))
                .collect(),
            timing,
            input_tokens: Some(rows.iter().map(fake_tokens).collect()),
        })
    }
}

/// FakeEngine's stand-in for a row's prompt length, as Python's fake_tokens: one token per space-separated word of the
/// row's JSON as `json.dumps` writes it.
pub fn fake_tokens(row: &Row) -> u64 {
    py_json_dumps(&row.to_json())
        .split(' ')
        .filter(|word| !word.is_empty())
        .count() as u64
}

/// A golden case (contract/golden/*.json).
#[derive(Clone, Debug, PartialEq)]
pub struct GoldenCase {
    /// The case's name.
    pub name: String,
    /// The call to make.
    pub call: Call,
    /// The value the decision must have.
    pub expected_value: Json,
    /// Whether the decision must abstain.
    pub expected_abstained: bool,
    /// The confidence the decision must have, within `tolerance`.
    pub expected_confidence: f64,
    /// How far the confidence may be from `expected_confidence`.
    pub tolerance: f64,
}

impl GoldenCase {
    /// The golden case a contract/golden entry describes. Raises ContractError for a missing or malformed field,
    /// and InvalidRequest for a malformed call.
    pub fn from_json(value: &Json) -> Result<GoldenCase> {
        let invalid = |what: &str| {
            JeffError::new(
                ErrorType::ContractError,
                format!("A golden case needs {what}; got {}", py_repr(value)),
            )
        };
        let expected = value.get("expected").ok_or_else(|| invalid("'expected'"))?;
        Ok(GoldenCase {
            name: value
                .get("name")
                .and_then(Json::as_str)
                .ok_or_else(|| invalid("a 'name'"))?
                .to_owned(),
            call: Call::from_json(value.get("call").ok_or_else(|| invalid("a 'call'"))?)?,
            expected_value: expected
                .get("value")
                .cloned()
                .ok_or_else(|| invalid("expected.value"))?,
            expected_abstained: expected
                .get("abstained")
                .and_then(Json::as_bool)
                .ok_or_else(|| invalid("expected.abstained"))?,
            expected_confidence: expected
                .get("confidence")
                .and_then(Json::as_f64)
                .ok_or_else(|| invalid("expected.confidence"))?,
            tolerance: value
                .get("tolerance")
                .and_then(Json::as_f64)
                .ok_or_else(|| invalid("a 'tolerance'"))?,
        })
    }
}

/// Equal and of the same JSON type, so that true does not match 1 (serde_json's `==` already tells them apart).
pub fn same(value: &Json, expected: &Json) -> bool {
    value == expected
}

/// How a decision differs from a golden case; empty when it reproduces it.
pub fn golden_problems(
    case: &GoldenCase,
    decision: &Decision,
    threshold: Option<f64>,
) -> Vec<String> {
    let mut problems = Vec::new();
    let value = decision.value.to_json();
    if !same(&value, &case.expected_value) {
        problems.push(format!(
            "value {}, expected {}",
            py_repr(&value),
            py_repr(&case.expected_value)
        ));
    }
    match decision.confidence {
        None => problems.push("no confidence".into()),
        Some(confidence) if (confidence - case.expected_confidence).abs() > case.tolerance => {
            problems.push(format!(
                "confidence {}, expected {} +/- {}",
                py_float(confidence),
                py_float(case.expected_confidence),
                py_float(case.tolerance)
            ))
        }
        Some(_) => {}
    }
    let near = threshold
        .is_some_and(|threshold| (case.expected_confidence - threshold).abs() <= case.tolerance);
    if decision.abstained != case.expected_abstained && !near {
        problems.push(format!(
            "abstained {}, expected {} (the recorded confidence is more than {} from the threshold {})",
            py_repr(&Json::Bool(decision.abstained)), py_repr(&Json::Bool(case.expected_abstained)), py_float(case.tolerance),
            threshold.map_or_else(|| "None".to_owned(), py_float)
        ));
    }
    problems
}

/// Makes every HTTP client and channel opened afterwards, in this process, pick its proxy from `environment` (name and
/// value pairs, as the process environment would hold them) instead of the process's own variables; None reads the
/// process's again. For tests that must not depend on the machine's HTTPS_PROXY, HTTP_PROXY, ALL_PROXY, WS_PROXY or
/// NO_PROXY: `pin_proxy_environment(Some(Vec::new()))` connects directly whatever is set.
///
/// Only with the `testing` feature: it changes how every connection in the process picks its proxy, so a normal build
/// cannot call it by mistake.
#[cfg(feature = "testing")]
pub fn pin_proxy_environment(environment: Option<Vec<(String, String)>>) {
    crate::proxy::pin(environment);
}

/// Makes every channel opened afterwards, in this process, send its keepalive ping every `interval_ms` and end when a
/// pong does not come within `timeout_ms` of a ping, instead of Python's 20 s and 20 s; None restores those. For tests
/// of the keepalive that cannot wait 40 s.
///
/// Only with the `testing` feature: it changes every channel in the process, so a normal build cannot call it by
/// mistake.
#[cfg(feature = "testing")]
pub fn pin_channel_keepalive(keepalive: Option<(u64, u64)>) {
    crate::channel::pin_keepalive(keepalive.map(|(interval_ms, timeout_ms)| {
        (
            std::time::Duration::from_millis(interval_ms),
            std::time::Duration::from_millis(timeout_ms),
        )
    }));
}
