//! The Decision every function returns (port of jeff/decision.py).

use indexmap::IndexMap;

use crate::binding::{Binding, option_keys};
use crate::contract::QuestionType;
use crate::errors::{ErrorType, JeffError, Result};
use crate::json::{Json, JsonMap};
use crate::pyformat::{
    py_float, py_json_dumps, py_json_float, py_json_string, py_repr, py_str, py_sum,
};

/// A decision's value: an option key (choice), true or false (noul), or a level index (score). It is not a JSON value
/// (that is [`Json`], serde_json's `Value`); [`Value::to_json`] converts it to one.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    /// An option key: the answer of a choice function.
    Str(String),
    /// The answer of a noul (yes or no) function: true when the probability of true is at least 0.5.
    Bool(bool),
    /// The 0-based index of the most likely level: the answer of a score function.
    Int(i64),
}

impl Value {
    /// The value as JSON: a string, a boolean or a whole number.
    pub fn to_json(&self) -> Json {
        match self {
            Value::Str(text) => Json::from(text.as_str()),
            Value::Bool(flag) => Json::Bool(*flag),
            Value::Int(level) => Json::from(*level),
        }
    }

    /// The value a JSON string, boolean or whole number holds. Raises ContractError for any other JSON.
    pub fn from_json(value: &Json) -> Result<Value> {
        match value {
            Json::String(text) => Ok(Value::Str(text.clone())),
            Json::Bool(flag) => Ok(Value::Bool(*flag)),
            Json::Number(number) if number.is_i64() => {
                Ok(Value::Int(number.as_i64().expect("an i64")))
            }
            other => Err(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "A decision value is a string, a boolean or a whole number; got {}",
                    py_repr(other)
                ),
            )),
        }
    }
}

/// One hop of routing: the router asked and the target it chose.
#[derive(Clone, Debug, PartialEq)]
pub struct RouteStep {
    /// The id of the router that was asked.
    pub router: String,
    /// The id of the router or function it chose.
    pub chose: String,
    /// The router's confidence in that choice.
    pub confidence: f64,
}

/// An earlier answer to the same call that an escalation went past.
#[derive(Clone, Debug, PartialEq)]
pub struct EscalationRecord {
    /// Who gave that answer (`<function id>@<version>` for Jeff, or the step's name).
    pub decided_by: String,
    /// The answer it gave.
    pub value: Value,
    /// Its confidence; none for an answer from an `llm` or `human` step.
    pub confidence: Option<f64>,
    /// Why the escalation went past it (for example the confidence below the threshold).
    pub reason: String,
}

/// A decision (contract/schemas/decision.schema.json). `probabilities` iterates in option order.
#[derive(Clone, Debug, PartialEq)]
pub struct Decision {
    /// The answer: an option key, true or false, or a level index. It holds the top answer even when
    /// the decision abstained.
    pub value: Value,
    /// The calibrated probability of `value`; none when an `llm` or `human` step answered.
    pub confidence: Option<f64>,
    /// True when `confidence` is below the function's threshold, or `value` is one of its abstain labels.
    pub abstained: bool,
    /// Whether the function has a calibrated threshold for the base that answered.
    pub calibrated: bool,
    /// The interface of the function that answered (classify, route, rate and so on).
    pub interface: String,
    /// The full id of the function that answered.
    pub function: String,
    /// The version of that function's adapter.
    pub version: String,
    /// The probability of each option, in option order; empty when an `llm` or `human` step answered.
    pub probabilities: IndexMap<String, f64>,
    /// For a score function, the expected level as a number; none for any other function.
    pub score: Option<f64>,
    /// The routing hops that led to the function, first hop first; empty for a direct call.
    pub route: Vec<RouteStep>,
    /// Who answered: `<function id>@<version>` for Jeff, or the name of the escalation step.
    pub decided_by: String,
    /// Every earlier answer the escalation went past, in order.
    pub escalations: Vec<EscalationRecord>,
    /// How the answer was reached and how long it took (for example `path` and `model_ms`).
    pub timing: JsonMap,
    /// Reserved: always empty until a function can point at spans of its input.
    pub evidence_refs: Vec<String>,
}

/// A decision's JSON before it is written: floats stay f64 so that a non-finite one can be written as Python writes it
/// (to_json_text) or refused by name (to_json).
enum Out {
    Json(Json),
    Float(f64),
    Map(Vec<(&'static str, Out)>),
    Floats(Vec<(String, f64)>),
    List(Vec<Out>),
}

fn optional_float(value: Option<f64>) -> Out {
    value.map_or(Out::Json(Json::Null), Out::Float)
}

fn text(value: &str) -> Out {
    Out::Json(Json::from(value))
}

impl Out {
    fn to_json(&self, path: &str) -> Result<Json> {
        let float = |value: f64, path: &str| {
            serde_json::Number::from_f64(value).map(Json::Number).ok_or_else(|| {
                JeffError::new(
                    ErrorType::ContractError,
                    format!(
                        "The decision's {path} is {}, which JSON cannot hold; Decision::to_json_text writes it as \
                         Python's json.dumps does ({})",
                        py_float(value),
                        py_json_float(value)
                    ),
                )
            })
        };
        let join = |key: &str| {
            if path.is_empty() {
                key.to_owned()
            } else {
                format!("{path}.{key}")
            }
        };
        Ok(match self {
            Out::Json(value) => value.clone(),
            Out::Float(value) => float(*value, path)?,
            Out::Map(fields) => Json::Object(
                fields
                    .iter()
                    .map(|(key, item)| Ok(((*key).to_owned(), item.to_json(&join(key))?)))
                    .collect::<Result<JsonMap>>()?,
            ),
            Out::Floats(fields) => Json::Object(
                fields
                    .iter()
                    .map(|(key, value)| {
                        Ok((
                            key.clone(),
                            float(*value, &format!("{path}[{}]", py_str(key)))?,
                        ))
                    })
                    .collect::<Result<JsonMap>>()?,
            ),
            Out::List(items) => Json::Array(
                items
                    .iter()
                    .enumerate()
                    .map(|(index, item)| item.to_json(&format!("{path}[{index}]")))
                    .collect::<Result<Vec<_>>>()?,
            ),
        })
    }

    fn to_text(&self) -> String {
        match self {
            Out::Json(value) => py_json_dumps(value),
            Out::Float(value) => py_json_float(*value),
            Out::Map(fields) => format!(
                "{{{}}}",
                fields
                    .iter()
                    .map(|(key, item)| format!("{}: {}", py_json_string(key), item.to_text()))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Out::Floats(fields) => format!(
                "{{{}}}",
                fields
                    .iter()
                    .map(|(key, value)| format!(
                        "{}: {}",
                        py_json_string(key),
                        py_json_float(*value)
                    ))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Out::List(items) => format!(
                "[{}]",
                items
                    .iter()
                    .map(Out::to_text)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }
}

fn shape(key: &str, what: &str, found: Option<&Json>) -> JeffError {
    JeffError::new(
        ErrorType::ContractError,
        format!(
            "A decision's '{key}' is not {what} (got {})",
            py_repr(found.unwrap_or(&Json::Null))
        ),
    )
}

fn text_at(map: &Json, key: &str) -> Result<String> {
    map.get(key)
        .and_then(Json::as_str)
        .map(str::to_owned)
        .ok_or_else(|| shape(key, "a string", map.get(key)))
}

fn optional_number_at(map: &Json, key: &str) -> Result<Option<f64>> {
    match map.get(key) {
        Some(Json::Null) => Ok(None),
        Some(Json::Number(number)) => Ok(number.as_f64()),
        other => Err(shape(key, "a number or null", other)),
    }
}

fn list_at<'a>(map: &'a Json, key: &str) -> Result<&'a Vec<Json>> {
    map.get(key)
        .and_then(Json::as_array)
        .ok_or_else(|| shape(key, "a list", map.get(key)))
}

impl Decision {
    /// The decision's fields in schema order (snake_case).
    fn out(&self) -> Out {
        Out::Map(vec![
            ("value", Out::Json(self.value.to_json())),
            ("confidence", optional_float(self.confidence)),
            ("abstained", Out::Json(Json::Bool(self.abstained))),
            ("calibrated", Out::Json(Json::Bool(self.calibrated))),
            ("interface", text(&self.interface)),
            ("function", text(&self.function)),
            ("version", text(&self.version)),
            (
                "probabilities",
                Out::Floats(
                    self.probabilities
                        .iter()
                        .map(|(key, p)| (key.clone(), *p))
                        .collect(),
                ),
            ),
            ("score", optional_float(self.score)),
            (
                "route",
                Out::List(
                    self.route
                        .iter()
                        .map(|step| {
                            Out::Map(vec![
                                ("router", text(&step.router)),
                                ("chose", text(&step.chose)),
                                ("confidence", Out::Float(step.confidence)),
                            ])
                        })
                        .collect(),
                ),
            ),
            ("decided_by", text(&self.decided_by)),
            (
                "escalations",
                Out::List(
                    self.escalations
                        .iter()
                        .map(|record| {
                            Out::Map(vec![
                                ("decided_by", text(&record.decided_by)),
                                ("value", Out::Json(record.value.to_json())),
                                ("confidence", optional_float(record.confidence)),
                                ("reason", text(&record.reason)),
                            ])
                        })
                        .collect(),
                ),
            ),
            ("timing", Out::Json(Json::Object(self.timing.clone()))),
            (
                "evidence_refs",
                Out::Json(Json::from(self.evidence_refs.clone())),
            ),
        ])
    }

    /// The decision as the contract writes it (snake_case, schema order). A NaN or infinite number (only a decision
    /// built by hand can hold one) is a ContractError naming the field: JSON has no value for it.
    pub fn to_json(&self) -> Result<Json> {
        self.out().to_json("")
    }

    /// The decision's JSON text exactly as the Python SDK writes it, `json.dumps(decision.to_json())`: separators
    /// ", " and ": ", non-ASCII escaped, floats as Python prints them, and NaN, Infinity and -Infinity for the numbers
    /// JSON has no text for.
    pub fn to_json_text(&self) -> String {
        self.out().to_text()
    }

    /// A decision from its JSON. Callers check the JSON against the decision schema first; this still names any field
    /// of the wrong shape.
    pub fn from_json(value: &Json) -> Result<Decision> {
        let probabilities = value
            .get("probabilities")
            .and_then(Json::as_object)
            .ok_or_else(|| shape("probabilities", "an object", value.get("probabilities")))?
            .iter()
            .map(|(key, p)| {
                p.as_f64()
                    .map(|p| (key.clone(), p))
                    .ok_or_else(|| shape("probabilities", "an object of numbers", Some(p)))
            })
            .collect::<Result<IndexMap<_, _>>>()?;
        let route = list_at(value, "route")?
            .iter()
            .map(|step| {
                Ok(RouteStep {
                    router: text_at(step, "router")?,
                    chose: text_at(step, "chose")?,
                    confidence: step
                        .get("confidence")
                        .and_then(Json::as_f64)
                        .ok_or_else(|| shape("confidence", "a number", step.get("confidence")))?,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let escalations = list_at(value, "escalations")?
            .iter()
            .map(|record| {
                Ok(EscalationRecord {
                    decided_by: text_at(record, "decided_by")?,
                    value: Value::from_json(record.get("value").unwrap_or(&Json::Null))?,
                    confidence: optional_number_at(record, "confidence")?,
                    reason: text_at(record, "reason")?,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let flag = |key: &str| {
            value
                .get(key)
                .and_then(Json::as_bool)
                .ok_or_else(|| shape(key, "a boolean", value.get(key)))
        };
        Ok(Decision {
            value: Value::from_json(value.get("value").unwrap_or(&Json::Null))?,
            confidence: optional_number_at(value, "confidence")?,
            abstained: flag("abstained")?,
            calibrated: flag("calibrated")?,
            interface: text_at(value, "interface")?,
            function: text_at(value, "function")?,
            version: text_at(value, "version")?,
            probabilities,
            score: optional_number_at(value, "score")?,
            route,
            decided_by: text_at(value, "decided_by")?,
            escalations,
            timing: value
                .get("timing")
                .and_then(Json::as_object)
                .cloned()
                .ok_or_else(|| shape("timing", "an object", value.get("timing")))?,
            evidence_refs: list_at(value, "evidence_refs")?
                .iter()
                .map(|item| {
                    item.as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| shape("evidence_refs", "a list of strings", Some(item)))
                })
                .collect::<Result<Vec<_>>>()?,
        })
    }
}

/// How far from 1 the sum of an engine reply's probabilities may be (contract/socket.md, "The probabilities of a
/// predict"). The engine's softmax runs in float32 over at most 255 options, so a sum is off by at most about
/// 255 * 2**-24 (1.5e-5); the engine's own tests hold it to 1e-4. SUM_TOLERANCE in jeff/decision.py.
pub const SUM_TOLERANCE: f64 = 1e-4;

/// The decision for one row: probabilities normalised with Python's sum, the winner the first best option in option
/// order, abstained below the calibration threshold or on an abstain label. A reply that breaks the engine's
/// contract is refused with EngineError, never repaired: the wrong number of probabilities, any that is not a
/// finite number from 0 to 1, or a sum more than SUM_TOLERANCE from 1 (the first rule broken is the one named).
pub fn make_decision(
    binding: &Binding,
    probabilities: &[f64],
    timing: &JsonMap,
) -> Result<Decision> {
    let function = &binding.function;
    let keys = option_keys(binding);
    if probabilities.len() != keys.len() {
        return Err(JeffError::new(
            ErrorType::EngineError,
            format!(
                "{}: the engine returned {} probabilities for {} options",
                function.id,
                probabilities.len(),
                keys.len()
            ),
        ));
    }
    let shown = format!(
        "[{}]",
        probabilities
            .iter()
            .map(|p| py_float(*p))
            .collect::<Vec<_>>()
            .join(", ")
    );
    let offending: Vec<f64> = probabilities
        .iter()
        .copied()
        .filter(|p| !(p.is_finite() && *p >= 0.0 && *p <= 1.0))
        .collect();
    if !offending.is_empty() {
        let offending_shown: Vec<String> = offending.iter().map(|p| py_float(*p)).collect();
        return Err(JeffError::new(
            ErrorType::EngineError,
            format!(
                "{}: the engine returned invalid probabilities {shown}; expected each to be a finite number from 0 \
                 to 1, which [{}] are not",
                function.id,
                offending_shown.join(", ")
            ),
        ));
    }
    let total = py_sum(probabilities);
    if (total - 1.0).abs() > SUM_TOLERANCE {
        return Err(JeffError::new(
            ErrorType::EngineError,
            format!(
                "{}: the engine returned invalid probabilities {shown}; expected their sum to be within {} of 1, \
                 got {}",
                function.id,
                py_float(SUM_TOLERANCE),
                py_float(total)
            ),
        ));
    }
    let normalised: Vec<f64> = probabilities.iter().map(|p| p / total).collect();
    let mut caller = IndexMap::new();
    for (key, p) in keys.iter().zip(&normalised) {
        let name = binding.wire_to_caller.get(key).ok_or_else(|| {
            JeffError::new(
                ErrorType::EngineError,
                format!(
                    "{}: the binding has no caller key for option {key}",
                    function.id
                ),
            )
        })?;
        caller.insert(name.clone(), *p);
    }
    let (value, confidence, score) = match function.question_type {
        QuestionType::Noul => {
            let option = |key: &str| {
                caller.get(key).copied().ok_or_else(|| {
                    JeffError::new(
                        ErrorType::EngineError,
                        format!(
                            "{}: a yes-or-no function has no '{key}' option",
                            function.id
                        ),
                    )
                })
            };
            let yes = option("true")?;
            let value = yes >= 0.5;
            (
                Value::Bool(value),
                if value { yes } else { option("false")? },
                None,
            )
        }
        QuestionType::Score => {
            let mut best = 0;
            for (index, p) in normalised.iter().enumerate() {
                if *p > normalised[best] {
                    best = index;
                }
            }
            let weighted: Vec<f64> = normalised
                .iter()
                .enumerate()
                .map(|(index, p)| index as f64 * p)
                .collect();
            (
                Value::Int(best as i64),
                normalised[best],
                Some(py_sum(&weighted)),
            )
        }
        QuestionType::Choice => {
            let mut best: Option<(&String, f64)> = None;
            for (key, p) in &caller {
                if best.is_none_or(|(_, top)| *p > top) {
                    best = Some((key, *p));
                }
            }
            let (key, p) = best.expect("a choice function has at least one option");
            (Value::Str(key.clone()), p, None)
        }
    };
    let below = binding
        .calibration
        .as_ref()
        .is_some_and(|calibration| confidence < calibration.threshold);
    let abstain_label = matches!(&value, Value::Str(key) if function.abstain_labels.contains(key));
    Ok(Decision {
        value,
        confidence: Some(confidence),
        abstained: below || abstain_label,
        calibrated: binding.calibration.is_some(),
        interface: function.interface.clone(),
        function: function.id.clone(),
        version: binding.manifest.version.clone(),
        probabilities: caller,
        score,
        route: Vec::new(),
        decided_by: format!("{}@{}", function.id, binding.manifest.version),
        escalations: Vec::new(),
        timing: timing.clone(),
        evidence_refs: Vec::new(),
    })
}

/// LowConfidence for a decision that abstained under on_low_confidence='error', carrying the decision; a ContractError
/// when the decision holds a number JSON cannot (Decision::to_json).
pub fn low_confidence(decision: &Decision) -> Result<JeffError> {
    let confidence = decision
        .confidence
        .map_or_else(|| "None".to_owned(), py_float);
    let mut details = JsonMap::new();
    details.insert("decision".into(), decision.to_json()?);
    Ok(JeffError::with_details(
        ErrorType::LowConfidence,
        format!(
            "{} abstained (confidence {confidence}); on_low_confidence='error'",
            decision.function
        ),
        details,
    ))
}

impl JeffError {
    /// LowConfidence: the decision that abstained, as its wire details carry it.
    pub fn decision(&self) -> Result<Decision> {
        match self.details().get("decision") {
            Some(data @ Json::Object(_)) => Decision::from_json(data),
            other => Err(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "{}.details is missing its 'decision' object (got {})",
                    self.type_name(),
                    py_repr(other.unwrap_or(&Json::Null))
                ),
            )),
        }
    }
}
