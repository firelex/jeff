//! Binding a function to its context and labels, and building the row the model reads (port of jeff/binding.py).
//! Context and labels come before the input, so one binding gives one fixed prompt prefix the engine can reuse. Every
//! map keeps its order, so keys that look like numbers stay where the manifest or the caller put them.

use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use regex::Regex;

use crate::contract::QuestionType;
use crate::errors::{ErrorType, JeffError, Result, missing_context, missing_labels};
use crate::json::{Json, JsonMap};
use crate::manifest::{Calibration, FunctionSpec, InputField, InputForm, LabelSource, Manifest};
use crate::pyformat::{PY_SPACE, py_repr, py_sorted, py_str, py_type_name};

/// Context values by code name.
pub type Context = JsonMap;

/// Labels a caller gives: a list of keys (subset labels, fixed labels with subset) or key -> description (caller and
/// describe labels).
#[derive(Clone, Debug, PartialEq)]
pub enum Labels {
    /// A list of keys: the options to offer (subset labels, or fixed labels a caller may offer a subset of).
    Keys(Vec<String>),
    /// Key to description (or none), in order: the caller's own options (caller labels), or the
    /// descriptions of the manifest's keys (describe labels).
    Described(IndexMap<String, Option<String>>),
}

impl Labels {
    /// The labels a JSON list of keys or object of key to description (a string or null) gives. Raises
    /// InvalidLabels for any other JSON.
    pub fn from_json(value: &Json) -> Result<Labels> {
        let refused = || {
            JeffError::new(
                ErrorType::InvalidLabels,
                format!(
                    "labels= must be a list of keys or an object of key -> description (a string or null); got {}",
                    py_repr(value)
                ),
            )
        };
        match value {
            Json::Array(items) => items
                .iter()
                .map(|item| item.as_str().map(str::to_owned))
                .collect::<Option<Vec<_>>>()
                .map(Labels::Keys)
                .ok_or_else(refused),
            Json::Object(map) => map
                .iter()
                .map(|(key, description)| match description {
                    Json::Null => Some((key.clone(), None)),
                    Json::String(text) => Some((key.clone(), Some(text.clone()))),
                    _ => None,
                })
                .collect::<Option<IndexMap<_, _>>>()
                .map(Labels::Described)
                .ok_or_else(refused),
            _ => Err(refused()),
        }
    }

    /// The labels as JSON: a list of keys, or an object of key to description (or null).
    pub fn to_json(&self) -> Json {
        match self {
            Labels::Keys(keys) => Json::from(keys.clone()),
            Labels::Described(map) => Json::Object(
                map.iter()
                    .map(|(key, description)| (key.clone(), Json::from(description.clone())))
                    .collect(),
            ),
        }
    }
}

/// One call of a function: its reference, input, and (when the function takes them) context and labels.
#[derive(Clone, Debug, PartialEq)]
pub struct Call {
    /// The function's reference (id or alias, with an optional key and `@version`).
    pub function: String,
    /// The input of this call.
    pub input: Json,
    /// Context values by code name, when the function takes context.
    pub context: Option<Context>,
    /// The caller's labels, when the function takes them from the caller.
    pub labels: Option<Labels>,
}

impl Call {
    /// A call of the function `function` with `input`, and no context or labels.
    pub fn new(function: impl Into<String>, input: Json) -> Call {
        Call {
            function: function.into(),
            input,
            context: None,
            labels: None,
        }
    }

    /// The same call with `context` (values by code name).
    pub fn with_context(mut self, context: Context) -> Call {
        self.context = Some(context);
        self
    }

    /// The same call with `labels`.
    pub fn with_labels(mut self, labels: Labels) -> Call {
        self.labels = Some(labels);
        self
    }

    /// A call as JSON writes it: `{"function", "input", "context"?, "labels"?}` (null context or labels are none).
    pub fn from_json(value: &Json) -> Result<Call> {
        let invalid = |what: &str| {
            JeffError::new(
                ErrorType::InvalidRequest,
                format!("A call needs {what}; got {}", py_repr(value)),
            )
        };
        let function = value
            .get("function")
            .and_then(Json::as_str)
            .ok_or_else(|| invalid("a 'function' string"))?;
        let input = value
            .get("input")
            .ok_or_else(|| invalid("an 'input'"))?
            .clone();
        let context = match value.get("context") {
            None | Some(Json::Null) => None,
            Some(Json::Object(map)) => Some(map.clone()),
            Some(_) => return Err(invalid("'context' as an object of code name -> value")),
        };
        let labels = match value.get("labels") {
            None | Some(Json::Null) => None,
            Some(labels) => Some(Labels::from_json(labels)?),
        };
        Ok(Call {
            function: function.to_owned(),
            input,
            context,
            labels,
        })
    }
}

/// What the model reads: the bound context then the input, under their exact keys (form fields), or the plain input
/// string (form text).
#[derive(Clone, Debug, PartialEq)]
pub enum RowState {
    /// The bound context, then the input under its key (form fields).
    Fields(JsonMap),
    /// The input string itself (form text).
    Text(String),
}

/// The options of a row: each key with its description, or a score function's levels.
#[derive(Clone, Debug, PartialEq)]
pub enum Criteria {
    /// Each option key with its description (or none), in option order.
    Described(IndexMap<String, Option<String>>),
    /// A score function's levels, lowest first.
    Levels(Vec<String>),
}

/// The question part of a row: what the model is asked and over which options.
#[derive(Clone, Debug, PartialEq)]
pub struct RowQuestion {
    /// The question type: choice, noul or score.
    pub question_type: QuestionType,
    /// The function's instructions, word for word.
    pub instructions: String,
    /// The options; none for a noul function whose manifest describes neither answer.
    pub criteria: Option<Criteria>,
}

/// One engine row (contract/socket.md, predict).
#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    /// What the model reads.
    pub state: RowState,
    /// What the model is asked.
    pub question: RowQuestion,
}

impl Row {
    /// `{"state", "question": {"type", "instructions", "criteria"?}}`, in that order, as the Python SDK packs it.
    pub fn to_json(&self) -> Json {
        let state = match &self.state {
            RowState::Fields(map) => Json::Object(map.clone()),
            RowState::Text(text) => Json::from(text.as_str()),
        };
        let mut question = JsonMap::new();
        question.insert(
            "type".into(),
            Json::from(self.question.question_type.as_str()),
        );
        question.insert(
            "instructions".into(),
            Json::from(self.question.instructions.as_str()),
        );
        match &self.question.criteria {
            Some(Criteria::Described(map)) => {
                question.insert("criteria".into(), Labels::Described(map.clone()).to_json());
            }
            Some(Criteria::Levels(levels)) => {
                question.insert("criteria".into(), Json::from(levels.clone()));
            }
            None => {}
        }
        let mut row = JsonMap::new();
        row.insert("state".into(), state);
        row.insert("question".into(), Json::Object(question));
        Json::Object(row)
    }

    /// The row a `{"state", "question"}` object describes. Raises InvalidRequest for a missing or malformed
    /// part.
    pub fn from_json(value: &Json) -> Result<Row> {
        let invalid = |what: &str| {
            JeffError::new(
                ErrorType::InvalidRequest,
                format!("A row needs {what}; got {}", py_repr(value)),
            )
        };
        let state = match value.get("state") {
            Some(Json::Object(map)) => RowState::Fields(map.clone()),
            Some(Json::String(text)) => RowState::Text(text.clone()),
            _ => return Err(invalid("a 'state' object or string")),
        };
        let question = value
            .get("question")
            .ok_or_else(|| invalid("a 'question'"))?;
        let question_type = question
            .get("type")
            .and_then(Json::as_str)
            .and_then(QuestionType::parse)
            .ok_or_else(|| invalid("a question 'type' of choice, noul or score"))?;
        let instructions = question
            .get("instructions")
            .and_then(Json::as_str)
            .ok_or_else(|| invalid("question 'instructions'"))?;
        let criteria = match question.get("criteria") {
            None => None,
            Some(Json::Array(levels)) => Some(Criteria::Levels(
                levels
                    .iter()
                    .map(|level| level.as_str().map(str::to_owned))
                    .collect::<Option<Vec<_>>>()
                    .ok_or_else(|| invalid("criteria levels that are strings"))?,
            )),
            Some(described @ Json::Object(_)) => match Labels::from_json(described)
                .map_err(|_| invalid("criteria descriptions that are strings or null"))?
            {
                Labels::Described(map) => Some(Criteria::Described(map)),
                Labels::Keys(_) => unreachable!("an object reads as described labels"),
            },
            Some(_) => return Err(invalid("'criteria' as an object or a list")),
        };
        Ok(Row {
            state,
            question: RowQuestion {
                question_type,
                instructions: instructions.to_owned(),
                criteria,
            },
        })
    }
}

/// A function bound to its context and labels, with the calibration it decides with on the engine's base format.
#[derive(Clone, Debug, PartialEq)]
pub struct Binding {
    /// The function that is bound.
    pub function: Arc<FunctionSpec>,
    /// The manifest of its adapter.
    pub manifest: Arc<Manifest>,
    /// The caller's context keyed by each field's exact key, in the function's field order.
    pub context: JsonMap,
    /// A choice function's options on the wire, in option order; None for noul and score functions.
    pub criteria: Option<IndexMap<String, Option<String>>>,
    /// Wire option key -> the caller's key (they differ for caller labels with caller_keys).
    pub wire_to_caller: IndexMap<String, String>,
    /// The calibration the function decides with on the engine's base format; none when it is uncalibrated
    /// there.
    pub calibration: Option<Calibration>,
}

// Python's re: \s and \d match Unicode whitespace and decimal digits in a str pattern.
static NUMBER_LIKE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"^[{PY_SPACE}]*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?[{PY_SPACE}]*$"
    ))
    .expect("the number-like pattern")
});

fn invalid_labels(message: String) -> JeffError {
    JeffError::new(ErrorType::InvalidLabels, message)
}

fn context_of(function: &FunctionSpec, given: Option<&Context>) -> Result<JsonMap> {
    let empty = JsonMap::new();
    let values = given.unwrap_or(&empty);
    let names: Vec<String> = values.keys().cloned().collect();
    if function.input.form == InputForm::Text && !names.is_empty() {
        return Err(JeffError::new(
            ErrorType::UnknownContext,
            format!(
                "{} takes plain text only (its input {} is the whole state the adapter sees), so it has no context fields; do \
             not pass context (got {})",
                function.id,
                function.input.code_name,
                py_sorted(names).join(", ")
            ),
        ));
    }
    let known: Vec<&str> = function
        .context
        .iter()
        .map(|field| field.code_name.as_str())
        .collect();
    let unknown = py_sorted(
        names
            .into_iter()
            .filter(|name| !known.contains(&name.as_str())),
    );
    if !unknown.is_empty() {
        let exact: Vec<String> = unknown
            .iter()
            .filter_map(|key| {
                function
                    .context
                    .iter()
                    .find(|field| &field.key == key)
                    .map(|field| format!("{} as {}", py_str(key), field.code_name))
            })
            .collect();
        let fields = if known.is_empty() {
            "none".to_owned()
        } else {
            known.join(", ")
        };
        let hint = if exact.is_empty() {
            String::new()
        } else {
            format!(
                "; context is given by code name, so pass {}",
                exact.join(", ")
            )
        };
        return Err(JeffError::new(
            ErrorType::UnknownContext,
            format!(
                "{} has no context field(s) {}; its fields are {fields}{hint}",
                function.id,
                unknown.join(", ")
            ),
        ));
    }
    let missing: Vec<String> = function
        .context
        .iter()
        .filter(|field| field.required && !values.contains_key(&field.code_name))
        .map(|field| field.code_name.clone())
        .collect();
    if !missing.is_empty() {
        return Err(missing_context(&function.id, &missing));
    }
    let mut bound = JsonMap::new();
    for field in &function.context {
        if let Some(value) = values.get(&field.code_name) {
            bound.insert(field.key.clone(), value.clone());
        }
    }
    Ok(bound)
}

type CriteriaPair = (IndexMap<String, Option<String>>, IndexMap<String, String>);

fn identity<'a>(keys: impl IntoIterator<Item = &'a String>) -> IndexMap<String, String> {
    keys.into_iter()
        .map(|key| (key.clone(), key.clone()))
        .collect()
}

fn repeated(keys: &[String]) -> Vec<String> {
    let twice: std::collections::BTreeSet<String> = keys
        .iter()
        .enumerate()
        .filter(|(index, key)| keys.iter().position(|other| other == *key) != Some(*index))
        .map(|(_, key)| key.clone())
        .collect();
    py_sorted(twice)
}

fn fixed_keys(function: &FunctionSpec) -> String {
    function
        .labels
        .fixed
        .keys()
        .cloned()
        .collect::<Vec<_>>()
        .join(", ")
}

fn subset_criteria(function: &FunctionSpec, labels: &Labels) -> Result<CriteriaPair> {
    let spec = &function.labels;
    let fixed = fixed_keys(function);
    let Labels::Keys(keys) = labels else {
        return Err(invalid_labels(format!(
            "{} offers a choice of its own labels; pass labels= as a list of keys from {fixed}",
            function.id
        )));
    };
    if keys.is_empty() {
        return Err(missing_labels(&function.id));
    }
    let twice = repeated(keys);
    if !twice.is_empty() {
        return Err(invalid_labels(format!(
            "{}: label(s) {} given more than once",
            function.id,
            twice.join(", ")
        )));
    }
    let unknown: Vec<&str> = keys
        .iter()
        .filter(|key| !spec.fixed.contains_key(*key))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        return Err(invalid_labels(format!(
            "{}: label(s) {} are not among its labels {fixed}",
            function.id,
            unknown.join(", ")
        )));
    }
    let missing: Vec<&str> = spec
        .required
        .iter()
        .filter(|key| !keys.contains(key))
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(invalid_labels(format!(
            "{}: label(s) {} must always be offered",
            function.id,
            missing.join(", ")
        )));
    }
    let criteria = keys
        .iter()
        .map(|key| (key.clone(), spec.fixed[key].clone()))
        .collect();
    Ok((criteria, identity(keys)))
}

/// Fixed labels with subset true: the caller's keys in the manifest's order, each with its fixed description.
fn fixed_subset_criteria(function: &FunctionSpec, labels: &Labels) -> Result<CriteriaPair> {
    let spec = &function.labels;
    let fixed = fixed_keys(function);
    let Labels::Keys(keys) = labels else {
        return Err(invalid_labels(format!(
            "{} has fixed labels and may offer some of them; pass labels= as a list of keys from {fixed}, or no labels= to offer them all",
            function.id
        )));
    };
    if keys.is_empty() {
        return Err(invalid_labels(format!(
            "{}: labels= is an empty list; pass at least one key from {fixed}, or no labels= to offer them all",
            function.id
        )));
    }
    let twice = repeated(keys);
    if !twice.is_empty() {
        return Err(invalid_labels(format!(
            "{}: label(s) {} given more than once",
            function.id,
            twice.join(", ")
        )));
    }
    let unknown: Vec<&str> = keys
        .iter()
        .filter(|key| !spec.fixed.contains_key(*key))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        return Err(invalid_labels(format!(
            "{}: label(s) {} are not among its labels {fixed}",
            function.id,
            unknown.join(", ")
        )));
    }
    let criteria: IndexMap<String, Option<String>> = spec
        .fixed
        .iter()
        .filter(|(key, _)| keys.contains(key))
        .map(|(key, description)| (key.clone(), description.clone()))
        .collect();
    let wire = identity(criteria.keys());
    Ok((criteria, wire))
}

fn describe_criteria(function: &FunctionSpec, labels: &Labels) -> Result<CriteriaPair> {
    let spec = &function.labels;
    let Labels::Described(given) = labels else {
        return Err(invalid_labels(format!(
            "{}: pass labels= as an object with exactly the keys {}, each with its description",
            function.id,
            spec.keys.join(", ")
        )));
    };
    let extra: Vec<&str> = given
        .keys()
        .filter(|key| !spec.keys.contains(key))
        .map(String::as_str)
        .collect();
    let missing: Vec<&str> = spec
        .keys
        .iter()
        .filter(|key| !given.contains_key(*key))
        .map(String::as_str)
        .collect();
    if !extra.is_empty() || !missing.is_empty() {
        let mut message = format!(
            "{} takes exactly the labels {}",
            function.id,
            spec.keys.join(", ")
        );
        if !extra.is_empty() {
            message.push_str(&format!("; unknown: {}", extra.join(", ")));
        }
        if !missing.is_empty() {
            message.push_str(&format!("; missing: {}", missing.join(", ")));
        }
        return Err(invalid_labels(message));
    }
    let criteria = spec
        .keys
        .iter()
        .map(|key| (key.clone(), given[key].clone()))
        .collect();
    Ok((criteria, identity(&spec.keys)))
}

fn choice_criteria(function: &FunctionSpec, labels: Option<&Labels>) -> Result<CriteriaPair> {
    let spec = &function.labels;
    if spec.source == LabelSource::Fixed {
        let Some(labels) = labels else {
            return Ok((spec.fixed.clone(), identity(spec.fixed.keys())));
        };
        if spec.subset != Some(true) {
            return Err(invalid_labels(format!(
                "{} has fixed labels ({}); do not pass labels=",
                function.id,
                fixed_keys(function)
            )));
        }
        return fixed_subset_criteria(function, labels);
    }
    let Some(labels) = labels else {
        return Err(missing_labels(&function.id));
    };
    match spec.source {
        LabelSource::Subset => return subset_criteria(function, labels),
        LabelSource::Describe => return describe_criteria(function, labels),
        LabelSource::Caller | LabelSource::Fixed => {}
    }
    let Labels::Described(caller) = labels else {
        return Err(invalid_labels(format!(
            "{}: pass labels= as an object of key -> description",
            function.id
        )));
    };
    if caller.is_empty() {
        return Err(missing_labels(&function.id));
    }
    if let Some(max) = spec.max
        && caller.len() as u64 > max
    {
        return Err(JeffError::new(
            ErrorType::TooManyLabels,
            format!(
                "{} got {} labels; it was trained on at most {max}",
                function.id,
                caller.len()
            ),
        ));
    }
    for key in caller.keys() {
        if spec.fixed_first.contains_key(key) {
            let reserved: Vec<&str> = spec.fixed_first.keys().map(String::as_str).collect();
            return Err(invalid_labels(format!(
                "{}: label {} is reserved (always listed first: {}); rename yours",
                function.id,
                py_str(key),
                reserved.join(", ")
            )));
        }
        if NUMBER_LIKE.is_match(key) {
            return Err(invalid_labels(format!(
                "{}: label key {} looks like a number; use a word",
                function.id,
                py_str(key)
            )));
        }
    }
    let mut criteria = spec.fixed_first.clone();
    let mut wire_to_caller = identity(spec.fixed_first.keys());
    for (index, (key, description)) in caller.iter().enumerate() {
        let wire = match &spec.caller_keys {
            Some(pattern) => pattern.replacen("{n}", &(index + 1).to_string(), 1),
            None => key.clone(),
        };
        if criteria.contains_key(&wire) {
            return Err(invalid_labels(format!(
                "{}: label {} collides with option {}",
                function.id,
                py_str(key),
                py_str(&wire)
            )));
        }
        criteria.insert(wire.clone(), description.clone());
        wire_to_caller.insert(wire, key.clone());
    }
    Ok((criteria, wire_to_caller))
}

fn fixed_option_keys(function: &FunctionSpec) -> Vec<String> {
    match function.question_type {
        QuestionType::Noul => vec!["false".into(), "true".into()],
        QuestionType::Score => (0..function.labels.levels.len())
            .map(|index| index.to_string())
            .collect(),
        QuestionType::Choice => {
            unreachable!("a choice function takes its option keys from a binding")
        }
    }
}

/// `function` (of `manifest`) bound to the caller's context and labels. `calibration`: the block the function decides
/// with on the engine's base format (`calibration_for`), resolved before binding, or None when it is uncalibrated
/// there. Raises MissingContext or UnknownContext for context fields missing or not the function's, and MissingLabels,
/// InvalidLabels or TooManyLabels for labels the function's label form does not allow.
pub fn bind(
    manifest: &Arc<Manifest>,
    function: &Arc<FunctionSpec>,
    context: Option<&Context>,
    labels: Option<&Labels>,
    calibration: Option<Calibration>,
) -> Result<Binding> {
    let bound_context = context_of(function, context)?;
    if function.question_type == QuestionType::Choice {
        let (criteria, wire_to_caller) = choice_criteria(function, labels)?;
        return Ok(Binding {
            function: Arc::clone(function),
            manifest: Arc::clone(manifest),
            context: bound_context,
            criteria: Some(criteria),
            wire_to_caller,
            calibration,
        });
    }
    if labels.is_some() {
        return Err(invalid_labels(format!(
            "{} is a {} function; it takes no labels=",
            function.id,
            function.question_type.as_str()
        )));
    }
    let keys = fixed_option_keys(function);
    Ok(Binding {
        function: Arc::clone(function),
        manifest: Arc::clone(manifest),
        context: bound_context,
        criteria: None,
        wire_to_caller: identity(&keys),
        calibration,
    })
}

/// The binding's option keys on the wire, in option order.
pub fn option_keys(binding: &Binding) -> Vec<String> {
    match &binding.criteria {
        Some(criteria) => criteria.keys().cloned().collect(),
        None => fixed_option_keys(&binding.function),
    }
}

pub(crate) fn request_state(
    function_id: &str,
    spec: &InputField,
    context: &JsonMap,
    input: &Json,
) -> Result<RowState> {
    match spec.form {
        InputForm::Fields => {
            let mut state = context.clone();
            state.insert(spec.key.clone(), input.clone());
            Ok(RowState::Fields(state))
        }
        InputForm::Text => match input {
            Json::String(text) => Ok(RowState::Text(text.clone())),
            other => Err(JeffError::new(
                ErrorType::InvalidRequest,
                format!(
                    "{function_id} takes plain text: its input {} must be a string, got {}",
                    spec.code_name,
                    py_type_name(other)
                ),
            )),
        },
    }
}

/// The engine row for one call of a bound function with `input`. Raises InvalidRequest when a function
/// that takes plain text is given an input that is not a string.
pub fn build_row(binding: &Binding, input: &Json) -> Result<Row> {
    let function = &binding.function;
    let state = request_state(&function.id, &function.input, &binding.context, input)?;
    let criteria = match function.question_type {
        QuestionType::Choice => Some(Criteria::Described(
            binding
                .criteria
                .clone()
                .expect("a choice function's binding always has criteria"),
        )),
        QuestionType::Score => Some(Criteria::Levels(function.labels.levels.clone())),
        QuestionType::Noul if !function.labels.fixed.is_empty() => {
            Some(Criteria::Described(function.labels.fixed.clone()))
        }
        QuestionType::Noul => None,
    };
    Ok(Row {
        state,
        question: RowQuestion {
            question_type: function.question_type,
            instructions: function.instructions.clone(),
            criteria,
        },
    })
}
