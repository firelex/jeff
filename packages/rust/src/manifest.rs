//! Adapter manifests: what an adapter is, its context and input, and the functions callers call (port of
//! jeff/manifest.py; every check and message is the Python SDK's).

use std::path::Path;
use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use regex::Regex;

use crate::contract::{QuestionType, SchemaName, interfaces, validate};
use crate::errors::{ErrorType, JeffError, Result};
use crate::json::{Json, JsonMap};
use crate::pyformat::{py_repr, py_sorted, py_str};
use crate::yaml::read_manifest_yaml_file;

pub(crate) const CODE_NAME_PATTERN: &str = "^[a-z][a-z0-9_]*$";
static CODE_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(CODE_NAME_PATTERN).expect("the code-name pattern"));
static NOT_CODE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new("[^a-z0-9]+").expect("the non-code pattern"));

/// The code name a key gets by the rule: itself when it is already one, else lower-cased with every run of other
/// characters turned into `_`, and `_` trimmed from both ends.
pub fn code_name_of(key: &str) -> String {
    if CODE_NAME.is_match(key) {
        return key.to_owned();
    }
    NOT_CODE
        .replace_all(&key.to_lowercase(), "_")
        .trim_matches('_')
        .to_owned()
}

/// A context field: a value a caller binds once and that Jeff sends before the input.
#[derive(Clone, Debug, PartialEq)]
pub struct ContextField {
    /// The key exactly as the adapter was trained with it, and as the prompt shows it.
    pub key: String,
    /// What the field holds, in the manifest's words.
    pub description: String,
    /// Whether a caller must bind it.
    pub required: bool,
    /// The name callers pass it as (see [`code_name_of`]).
    pub code_name: String,
}

/// How the input is sent to the engine.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputForm {
    /// The state is an object: the bound context, then the input under its key.
    Fields,
    /// The state is the input string itself, unwrapped, and there is no context.
    Text,
}

/// The input a caller passes with every call.
#[derive(Clone, Debug, PartialEq)]
pub struct InputField {
    /// With form `Fields`, the key exactly as trained and as the prompt shows it; with form `Text`, only
    /// the input's name.
    pub key: String,
    /// What the input holds, in the manifest's words.
    pub description: String,
    /// The name callers use for the input.
    pub code_name: String,
    /// Whether the input is sent as fields or as plain text.
    pub form: InputForm,
}

/// Where a function's options come from: one of the four label forms a manifest declares.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LabelSource {
    /// The manifest sets every option.
    Fixed,
    /// The caller supplies its own options, sent under keys the manifest names (such as `k1, k2, ...`).
    Caller,
    /// The manifest fixes every allowed option; the caller passes the keys to offer, including the required ones.
    Subset,
    /// The manifest fixes the keys; the caller supplies a description for exactly those keys.
    Describe,
}

/// A function's options as its manifest declares them.
#[derive(Clone, Debug, PartialEq)]
pub struct LabelSpec {
    /// Which of the four label forms this is.
    pub source: LabelSource,
    /// The options the manifest fixes, key to description (or none), in order.
    pub fixed: IndexMap<String, Option<String>>,
    /// A score function's levels, lowest first.
    pub levels: Vec<String>,
    /// Options sent before the caller's own ones, word for word (such as `other`).
    pub fixed_first: IndexMap<String, Option<String>>,
    /// The pattern the caller's options are sent under (such as `k{n}`).
    pub caller_keys: Option<String>,
    /// The most options a caller may supply.
    pub max: Option<u64>,
    /// With subset labels, the keys every subset must include.
    pub required: Vec<String>,
    /// With describe labels, the keys the caller describes.
    pub keys: Vec<String>,
    /// For fixed labels of a choice function, whether the caller may offer a subset of them (none elsewhere).
    pub subset: Option<bool>,
}

/// The local file a calibration's rows were read from instead of its `data` URI.
#[derive(Clone, Debug, PartialEq)]
pub struct DataMirror {
    /// The file's path.
    pub path: String,
    /// The file's SHA-256 digest, in hexadecimal.
    pub sha256: String,
}

/// A function's fitted confidence threshold and the data it was fitted on.
#[derive(Clone, Debug, PartialEq)]
pub struct Calibration {
    /// Where the calibration rows come from (a URI or a path).
    pub data: String,
    /// How many rows the threshold was fitted on.
    pub rows: u64,
    /// The precision the threshold was chosen to reach.
    pub target_precision: f64,
    /// The lowest confidence at which the measured precision reaches the target; a decision below it abstains.
    pub threshold: f64,
    /// The precision measured at the threshold.
    pub precision_at_threshold: f64,
    /// The share of calibration rows that do not abstain at the threshold.
    pub coverage_at_threshold: f64,
    /// The mirror file the rows were read from, when they were not read from `data` itself.
    pub data_mirror: Option<DataMirror>,
    /// Only on an INT4 base's entry (calibration_by_format): which calibration rows were left out, and why.
    pub excluded: Option<String>,
}

/// One function of an adapter, as its manifest declares it.
#[derive(Clone, Debug, PartialEq)]
pub struct FunctionSpec {
    /// The id of the adapter it belongs to.
    pub adapter_id: String,
    /// Its key in the manifest's `functions`.
    pub key: String,
    /// Its full id, `<adapter_id>.<key>`.
    pub id: String,
    /// The interface it implements (classify, route, rate and so on).
    pub interface: String,
    /// Its question type: choice, noul (yes or no) or score.
    pub question_type: QuestionType,
    /// The instructions the model reads, word for word.
    pub instructions: String,
    /// Its options.
    pub labels: LabelSpec,
    /// The options that mean "unclear" (a decision on one of them abstains).
    pub abstain_labels: Vec<String>,
    /// Its calibration on the bfloat16 base, when it has one.
    pub calibration: Option<Calibration>,
    /// The context fields it takes: its own when its entry declares them, else the adapter's.
    pub context: Vec<ContextField>,
    /// The input it takes: its own when its entry declares one, else the adapter's.
    pub input: InputField,
    /// Its calibration on each INT4 base, by the base's format_id.
    pub calibration_by_format: IndexMap<String, Calibration>,
}

/// An adapter manifest: what the adapter is, its context and input, and its functions.
#[derive(Clone, Debug, PartialEq)]
pub struct Manifest {
    /// The adapter id (`jeff.<domain>.<name>`).
    pub id: String,
    /// The adapter's version.
    pub version: String,
    /// Short names callers may use for the adapter.
    pub aliases: Vec<String>,
    /// `open` or `proprietary`.
    pub licence_kind: String,
    /// Where the adapter's weights are (the manifest's `weights` block).
    pub weights: Json,
    /// The id of the base model the adapter was trained on.
    pub base_id: String,
    /// The version of that base model.
    pub base_version: String,
    /// The most options any of its functions may offer in one call.
    pub max_options: u64,
    /// The adapter's context fields, in prompt order.
    pub context: Vec<ContextField>,
    /// The adapter's input.
    pub input: InputField,
    /// Its functions by key, in the manifest's order.
    pub functions: IndexMap<String, Arc<FunctionSpec>>,
    /// For a router, the adapters it routes to; empty for any other adapter.
    pub routes_to: Vec<String>,
    /// Where its test and calibration data live (the manifest's `tests` block).
    pub tests: JsonMap,
    /// The manifest document as read.
    pub raw: Json,
    /// The path the manifest was read from.
    pub path: String,
    /// Whether the adapter is a router (it has `routes_to`).
    pub is_router: bool,
}

fn manifest_error(message: String) -> JeffError {
    JeffError::new(ErrorType::ManifestError, message)
}

fn list_repr(items: &[String]) -> String {
    py_repr(&Json::from(items.to_vec()))
}

// The document was validated against manifest.schema.json first, so a field of the wrong type here is a contract bug;
// these readers still name it rather than invent a value.
fn shape(where_: &str, key: &str, what: &str, found: Option<&Json>) -> JeffError {
    JeffError::new(
        ErrorType::ContractError,
        format!(
            "{where_}: '{key}' is not {what} (got {})",
            py_repr(found.unwrap_or(&Json::Null))
        ),
    )
}

fn object<'a>(value: &'a Json, where_: &str, key: &str) -> Result<&'a JsonMap> {
    value
        .get(key)
        .and_then(Json::as_object)
        .ok_or_else(|| shape(where_, key, "an object", value.get(key)))
}

fn text(value: &Json, where_: &str, key: &str) -> Result<String> {
    value
        .get(key)
        .and_then(Json::as_str)
        .map(str::to_owned)
        .ok_or_else(|| shape(where_, key, "a string", value.get(key)))
}

fn texts(value: &Json, where_: &str, key: &str) -> Result<Vec<String>> {
    let list = value
        .get(key)
        .and_then(Json::as_array)
        .ok_or_else(|| shape(where_, key, "a list of strings", value.get(key)))?;
    list.iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| shape(where_, key, "a list of strings", value.get(key)))
        })
        .collect()
}

/// An optional list of strings: absent or null is empty, as Python's `raw.get(key) or ()`.
fn optional_texts(value: &Json, where_: &str, key: &str) -> Result<Vec<String>> {
    match value.get(key) {
        None | Some(Json::Null) => Ok(Vec::new()),
        Some(_) => texts(value, where_, key),
    }
}

/// An optional mapping of key -> description (a string or null): absent or null is empty.
fn described(value: &Json, where_: &str, key: &str) -> Result<IndexMap<String, Option<String>>> {
    let map = match value.get(key) {
        None | Some(Json::Null) => return Ok(IndexMap::new()),
        Some(Json::Object(map)) => map,
        found => return Err(shape(where_, key, "an object of descriptions", found)),
    };
    map.iter()
        .map(|(name, description)| match description {
            Json::Null => Ok((name.clone(), None)),
            Json::String(text) => Ok((name.clone(), Some(text.clone()))),
            _ => Err(shape(
                where_,
                key,
                "an object of descriptions",
                value.get(key),
            )),
        })
        .collect()
}

fn number(value: &Json, where_: &str, key: &str) -> Result<f64> {
    value
        .get(key)
        .and_then(Json::as_f64)
        .ok_or_else(|| shape(where_, key, "a number", value.get(key)))
}

fn whole(value: &Json, where_: &str, key: &str) -> Result<u64> {
    value
        .get(key)
        .and_then(Json::as_u64)
        .ok_or_else(|| shape(where_, key, "a whole number", value.get(key)))
}

/// A calibration block as manifests and GET /v1/functions write it (snake_case).
pub fn calibration_from_wire(block: &Json) -> Result<Calibration> {
    let where_ = "a calibration block";
    let data_mirror = match block.get("data_mirror") {
        None | Some(Json::Null) => None,
        Some(mirror) => Some(DataMirror {
            path: text(mirror, where_, "path")?,
            sha256: text(mirror, where_, "sha256")?,
        }),
    };
    let excluded = match block.get("excluded") {
        None => None,
        Some(_) => Some(text(block, where_, "excluded")?),
    };
    Ok(Calibration {
        data: text(block, where_, "data")?,
        rows: whole(block, where_, "rows")?,
        target_precision: number(block, where_, "target_precision")?,
        threshold: number(block, where_, "threshold")?,
        precision_at_threshold: number(block, where_, "precision_at_threshold")?,
        coverage_at_threshold: number(block, where_, "coverage_at_threshold")?,
        data_mirror,
        excluded,
    })
}

fn labels_of(raw: &Json, where_: &str) -> Result<LabelSpec> {
    let source = match text(raw, where_, "source")?.as_str() {
        "fixed" => LabelSource::Fixed,
        "caller" => LabelSource::Caller,
        "subset" => LabelSource::Subset,
        "describe" => LabelSource::Describe,
        other => {
            return Err(shape(
                where_,
                "source",
                "a label source",
                Some(&Json::from(other)),
            ));
        }
    };
    let caller_keys = match raw.get("caller_keys") {
        None | Some(Json::Null) => None,
        Some(_) => Some(text(raw, where_, "caller_keys")?),
    };
    let max = match raw.get("max") {
        None | Some(Json::Null) => None,
        Some(_) => Some(whole(raw, where_, "max")?),
    };
    let subset = match raw.get("subset") {
        None | Some(Json::Null) => None,
        Some(flag) => Some(
            flag.as_bool()
                .ok_or_else(|| shape(where_, "subset", "a boolean", Some(flag)))?,
        ),
    };
    Ok(LabelSpec {
        source,
        fixed: described(raw, where_, "fixed")?,
        levels: optional_texts(raw, where_, "levels")?,
        fixed_first: described(raw, where_, "fixed_first")?,
        caller_keys,
        max,
        required: optional_texts(raw, where_, "required")?,
        keys: optional_texts(raw, where_, "keys")?,
        subset,
    })
}

fn code_name_for(where_: &str, field: &Json) -> Result<String> {
    if field.get("code_name").is_some() {
        return text(field, where_, "code_name");
    }
    let key = text(field, where_, "key")?;
    let code = code_name_of(&key);
    if !CODE_NAME.is_match(&code) {
        return Err(manifest_error(format!(
            "{where_}: field {} gives no code name by the rule (lower-case, other characters to '_': {}, which is not of \
             the form {CODE_NAME_PATTERN}); declare its code_name",
            py_str(&key),
            py_str(&code)
        )));
    }
    Ok(code)
}

fn check_code_names(where_: &str, fields: &[(String, String)]) -> Result<()> {
    let mut by_code: IndexMap<&str, Vec<&str>> = IndexMap::new();
    for (key, code) in fields {
        by_code.entry(code.as_str()).or_default().push(key.as_str());
    }
    let shared: Vec<String> = by_code
        .iter()
        .filter(|(_, keys)| keys.len() > 1)
        .map(|(code, keys)| {
            format!(
                "{} -> {code}",
                keys.iter()
                    .map(|key| py_str(key))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })
        .collect();
    if !shared.is_empty() {
        return Err(manifest_error(format!(
            "{where_}: fields share a code name, which callers could not tell apart: {}; declare a distinct code_name for each",
            shared.join("; ")
        )));
    }
    Ok(())
}

fn request_fields(
    where_: &str,
    raw_context: &Json,
    raw_input: &Json,
) -> Result<(Vec<ContextField>, InputField)> {
    let items = raw_context
        .as_array()
        .ok_or_else(|| shape(where_, "context", "a list", Some(raw_context)))?;
    let mut context = Vec::new();
    for item in items {
        let required = item
            .get("required")
            .and_then(Json::as_bool)
            .ok_or_else(|| shape(where_, "required", "a boolean", item.get("required")))?;
        context.push(ContextField {
            key: text(item, where_, "key")?,
            description: text(item, where_, "description")?,
            required,
            code_name: code_name_for(where_, item)?,
        });
    }
    let input_key = text(raw_input, where_, "key")?;
    let keys: Vec<String> = context.iter().map(|field| field.key.clone()).collect();
    let unique: std::collections::HashSet<&String> = keys.iter().collect();
    if unique.len() != keys.len() || keys.contains(&input_key) {
        return Err(manifest_error(format!(
            "{where_}: context keys must be unique and differ from the input key ({}, input {input_key})",
            list_repr(&keys)
        )));
    }
    let form = match text(raw_input, where_, "form")?.as_str() {
        "fields" => InputForm::Fields,
        "text" => InputForm::Text,
        other => {
            return Err(shape(
                where_,
                "form",
                "fields or text",
                Some(&Json::from(other)),
            ));
        }
    };
    if form == InputForm::Text && !context.is_empty() {
        return Err(manifest_error(format!(
            "{where_}: input {} has form text (the state is the plain input string), so there can be no context fields, \
             but it has {}",
            py_str(&input_key),
            list_repr(&keys)
        )));
    }
    let input = InputField {
        key: input_key,
        description: text(raw_input, where_, "description")?,
        code_name: code_name_for(where_, raw_input)?,
        form,
    };
    let mut pairs: Vec<(String, String)> = context
        .iter()
        .map(|field| (field.key.clone(), field.code_name.clone()))
        .collect();
    pairs.push((input.key.clone(), input.code_name.clone()));
    check_code_names(where_, &pairs)?;
    Ok((context, input))
}

fn check_function(spec: &FunctionSpec, max_options: u64, where_: &str) -> Result<()> {
    let known = interfaces()?;
    let name = format!("{where_}: function {}", spec.key);
    let Some(interface) = known.get(&spec.interface) else {
        let names: Vec<&str> = known.keys().map(String::as_str).collect();
        return Err(manifest_error(format!(
            "{name} names interface {}, which is not in the interface catalogue ({})",
            py_str(&spec.interface),
            names.join(", ")
        )));
    };
    if spec.question_type != interface.question_type {
        return Err(manifest_error(format!(
            "{name} has type {} but interface {} compiles to {}",
            spec.question_type.as_str(),
            spec.interface,
            interface.question_type.as_str()
        )));
    }
    let labels = &spec.labels;
    if spec.question_type != QuestionType::Choice
        && (!labels.required.is_empty() || !labels.keys.is_empty())
    {
        return Err(manifest_error(format!(
            "{name}: only choice functions have 'required' or 'keys'"
        )));
    }
    match spec.question_type {
        QuestionType::Choice => {
            if !labels.levels.is_empty() {
                return Err(manifest_error(format!(
                    "{name}: choice functions have no levels"
                )));
            }
            if labels.source == LabelSource::Fixed && labels.fixed.is_empty() {
                return Err(manifest_error(format!(
                    "{name}: fixed labels need a non-empty 'fixed'"
                )));
            }
            if labels.source == LabelSource::Caller && labels.max.is_none() {
                return Err(manifest_error(format!(
                    "{name}: caller labels need 'max', the most labels the adapter was trained on"
                )));
            }
            if !labels.required.is_empty() && labels.source != LabelSource::Subset {
                return Err(manifest_error(format!(
                    "{name}: only subset labels have 'required'"
                )));
            }
            if !labels.keys.is_empty() && labels.source != LabelSource::Describe {
                return Err(manifest_error(format!(
                    "{name}: only describe labels have 'keys'"
                )));
            }
            if labels.source == LabelSource::Subset {
                if labels.fixed.is_empty()
                    || !labels.fixed_first.is_empty()
                    || labels
                        .caller_keys
                        .as_deref()
                        .is_some_and(|keys| !keys.is_empty())
                    || labels.max.is_some()
                {
                    return Err(manifest_error(format!(
                        "{name}: subset labels need a non-empty 'fixed' (every allowed label) and no fixed_first, caller_keys or max"
                    )));
                }
                let stray: Vec<String> = labels
                    .required
                    .iter()
                    .filter(|key| !labels.fixed.contains_key(*key))
                    .cloned()
                    .collect();
                if !stray.is_empty() {
                    let fixed: Vec<String> = labels.fixed.keys().cloned().collect();
                    return Err(manifest_error(format!(
                        "{name}: required labels {} are not in 'fixed' ({})",
                        list_repr(&stray),
                        list_repr(&fixed)
                    )));
                }
            }
            if labels.source == LabelSource::Describe {
                if labels.keys.is_empty()
                    || !labels.fixed.is_empty()
                    || !labels.fixed_first.is_empty()
                    || labels
                        .caller_keys
                        .as_deref()
                        .is_some_and(|keys| !keys.is_empty())
                    || labels.max.is_some()
                {
                    return Err(manifest_error(format!(
                        "{name}: describe labels need non-empty 'keys' and no fixed, fixed_first, caller_keys or max"
                    )));
                }
                let unique: std::collections::HashSet<&String> = labels.keys.iter().collect();
                if unique.len() != labels.keys.len() {
                    return Err(manifest_error(format!(
                        "{name}: describe keys {} repeat a key",
                        list_repr(&labels.keys)
                    )));
                }
            }
            let count = labels.fixed.len() as u64
                + labels.fixed_first.len() as u64
                + labels.max.unwrap_or(0)
                + labels.keys.len() as u64;
            if count > max_options {
                return Err(manifest_error(format!(
                    "{name} allows {count} options, more than the base's max_options {max_options}"
                )));
            }
            let fixed: Vec<String> = labels.fixed.keys().cloned().collect();
            if let Some(values) = &interface.values
                && &fixed != values
            {
                return Err(manifest_error(format!(
                    "{name}: interface {} requires labels {}, got {}",
                    spec.interface,
                    list_repr(values),
                    list_repr(&fixed)
                )));
            }
        }
        QuestionType::Score => {
            if labels.levels.len() < 2
                || !labels.fixed.is_empty()
                || labels.source != LabelSource::Fixed
            {
                return Err(manifest_error(format!(
                    "{name}: score functions need fixed 'levels' (2 to 10) and nothing else"
                )));
            }
        }
        QuestionType::Noul => {
            if !labels.levels.is_empty()
                || labels
                    .fixed
                    .keys()
                    .any(|key| key != "true" && key != "false")
                || labels.source != LabelSource::Fixed
            {
                return Err(manifest_error(format!(
                    "{name}: noul functions may only describe 'true' and 'false' in 'fixed'"
                )));
            }
        }
    }
    let known_labels: std::collections::BTreeSet<String> = labels
        .fixed
        .keys()
        .chain(labels.fixed_first.keys())
        .chain(labels.keys.iter())
        .cloned()
        .collect();
    let unknown: Vec<String> = spec
        .abstain_labels
        .iter()
        .filter(|label| !known_labels.contains(*label))
        .cloned()
        .collect();
    if !unknown.is_empty() {
        let sorted = py_sorted(known_labels);
        return Err(manifest_error(format!(
            "{name}: abstain_labels {} are not among its labels {}",
            list_repr(&unknown),
            list_repr(&sorted)
        )));
    }
    Ok(())
}

/// tests.function_by as its list of criteria (a single value is the list holding only it).
fn criteria(where_: &str, function_by: &Json) -> Result<Vec<String>> {
    let not_criteria = || {
        shape(
            where_,
            "function_by",
            "a criterion or a list of criteria",
            Some(function_by),
        )
    };
    match function_by {
        Json::String(one) => Ok(vec![one.clone()]),
        Json::Array(many) => many
            .iter()
            .map(|item| item.as_str().map(str::to_owned).ok_or_else(not_criteria))
            .collect(),
        _ => Err(not_criteria()),
    }
}

fn check_tests(
    where_: &str,
    tests: &JsonMap,
    functions: &IndexMap<String, Arc<FunctionSpec>>,
) -> Result<()> {
    let function_by = tests
        .get("function_by")
        .ok_or_else(|| shape(where_, "function_by", "given", None))?;
    let by = criteria(where_, function_by)?;
    let keys: Vec<String> = functions.keys().cloned().collect();
    if function_by.as_str() == Some("question_type") {
        let types: Vec<String> = functions
            .values()
            .map(|spec| spec.question_type.as_str().to_owned())
            .collect();
        let unique: std::collections::HashSet<&String> = types.iter().collect();
        if unique.len() != types.len() {
            return Err(manifest_error(format!(
                "{where_}: tests.function_by question_type needs one function per question type; the functions have types {}",
                list_repr(&types)
            )));
        }
    }
    if let Some(kinds) = tests.get("kinds") {
        if !by.iter().any(|criterion| criterion == "source_kind") {
            return Err(manifest_error(format!(
                "{where_}: tests.kinds maps source.kind values to functions, so tests.function_by must use source_kind (it is {})",
                py_repr(function_by)
            )));
        }
        let kinds = kinds
            .as_object()
            .ok_or_else(|| shape(where_, "kinds", "an object", Some(kinds)))?;
        let mut named = Vec::new();
        let mut several = Vec::new();
        for (kind, value) in kinds {
            match value {
                Json::String(one) => named.push(one.clone()),
                Json::Array(many) => {
                    several.push(kind.clone());
                    named.extend(many.iter().filter_map(Json::as_str).map(str::to_owned));
                }
                _ => {
                    return Err(shape(
                        where_,
                        "kinds",
                        "an object of function keys",
                        Some(value),
                    ));
                }
            }
        }
        let stray = py_sorted(
            named
                .into_iter()
                .filter(|key| !functions.contains_key(key))
                .collect::<std::collections::BTreeSet<_>>(),
        );
        if !stray.is_empty() {
            return Err(manifest_error(format!(
                "{where_}: tests.kinds names {}, which are not its functions ({})",
                list_repr(&stray),
                list_repr(&keys)
            )));
        }
        if !several.is_empty() && by.len() == 1 && by[0] == "source_kind" {
            return Err(manifest_error(format!(
                "{where_}: tests.kinds maps {} to several functions each, but tests.function_by is source_kind alone, so no \
                 other criterion can leave one of them: every row of those kinds would match several functions",
                list_repr(&several)
            )));
        }
    }
    let uses = by.iter().any(|criterion| criterion == "id_pattern");
    let Some(patterns) = tests.get("id_patterns") else {
        if uses {
            return Err(manifest_error(format!(
                "{where_}: tests.function_by uses id_pattern, so tests.id_patterns must give every function's row id pattern ({})",
                keys.join(", ")
            )));
        }
        return Ok(());
    };
    if !uses {
        return Err(manifest_error(format!(
            "{where_}: tests.id_patterns is given, but tests.function_by does not use id_pattern (it is {})",
            py_repr(function_by)
        )));
    }
    let patterns = patterns
        .as_object()
        .ok_or_else(|| shape(where_, "id_patterns", "an object", Some(patterns)))?;
    let named: Vec<String> = patterns.keys().cloned().collect();
    if named != keys {
        return Err(manifest_error(format!(
            "{where_}: tests.id_patterns must name every function, in the functions' order ({}); it names {}",
            list_repr(&keys),
            list_repr(&named)
        )));
    }
    // The patterns only map test rows, which this crate never does, so a pattern is only checked for being a regular
    // expression; fancy-regex reads Python's syntax more closely than the regex crate (lookarounds, backreferences).
    for (key, pattern) in patterns {
        let pattern = pattern
            .as_str()
            .ok_or_else(|| shape(where_, key, "a pattern string", Some(pattern)))?;
        if let Err(error) = fancy_regex::Regex::new(pattern) {
            return Err(manifest_error(format!(
                "{where_}: tests.id_patterns {key}: {} is not a regular expression ({error})",
                py_str(pattern)
            )));
        }
    }
    Ok(())
}

static VERSION_PART: LazyLock<Regex> =
    LazyLock::new(|| Regex::new("^v[0-9]+$").expect("the version-part pattern"));

/// The id's prefix (jeff.<domain>), its name's hyphen-separated segments, and the indexes of those that are a version
/// part (v and digits, the whole segment). None when the id has no dot (it is then no adapter id).
fn version_parts(adapter_id: &str) -> Option<(&str, Vec<&str>, Vec<usize>)> {
    let (prefix, name) = adapter_id.rsplit_once('.')?;
    let segments: Vec<&str> = name.split('-').collect();
    let versions = segments
        .iter()
        .enumerate()
        .filter(|(_, segment)| VERSION_PART.is_match(segment))
        .map(|(index, _)| index)
        .collect();
    Some((prefix, segments, versions))
}

/// The adapter id without its version part (contract/schemas/manifest.schema.json, aliases): the one hyphen-separated
/// segment of its name (the part after the second dot) that is v and digits, removed with one hyphen, as
/// jeff.routing.router-open-v14 -> jeff.routing.router-open. None when the name has no such segment, more than one,
/// or nothing besides it, or when `adapter_id` has no dot (it is then no adapter id).
pub fn unversioned_id(adapter_id: &str) -> Option<String> {
    let (prefix, mut segments, versions) = version_parts(adapter_id)?;
    if versions.len() != 1 || segments.len() == 1 {
        return None;
    }
    segments.remove(versions[0]); // remove: not an ordered map (a Vec of the name's segments)
    Some(format!("{prefix}.{}", segments.join("-")))
}

/// No alias is listed twice, and an alias of the id form (the schema allows it as jeff.<domain>.<name>) must be the
/// adapter's unversioned id.
fn check_aliases(where_: &str, adapter_id: &str, aliases: &[String]) -> Result<()> {
    let mut seen = std::collections::HashSet::new();
    for alias in aliases {
        if !seen.insert(alias.as_str()) {
            return Err(manifest_error(format!(
                "{where_}: the alias {alias} is listed twice"
            )));
        }
        if !alias.starts_with("jeff.") {
            continue;
        }
        let form = format!(
            "{where_}: the alias {alias} has the form of an adapter ID, which only the adapter's ID without its \
             version part may have, and "
        );
        let Some((_, segments, versions)) = version_parts(adapter_id) else {
            return Err(manifest_error(format!(
                "{form}{adapter_id} has no version part (one segment v<digits> in its name besides others, as in \
                 jeff.routing.router-open-v14)"
            )));
        };
        if versions.is_empty() {
            return Err(manifest_error(format!(
                "{form}{adapter_id} has no version part (one segment v<digits> in its name besides others, as in \
                 jeff.routing.router-open-v14)"
            )));
        }
        if versions.len() > 1 {
            let parts: Vec<&str> = versions.iter().map(|index| segments[*index]).collect();
            return Err(manifest_error(format!(
                "{form}{adapter_id} has more than one version part ({}), so it has no ID without one",
                parts.join(", ")
            )));
        }
        if segments.len() == 1 {
            return Err(manifest_error(format!(
                "{form}the name of {adapter_id} is only a version part, so it has no ID without one"
            )));
        }
        let expected = unversioned_id(adapter_id).expect("one version part and other segments");
        if *alias != expected {
            return Err(manifest_error(format!(
                "{where_}: the alias {alias} has the form of an adapter ID, so it must be {expected}, the ID \
                 {adapter_id} without its version part"
            )));
        }
    }
    Ok(())
}

/// A manifest document (as `read_yaml` reads it, or JSON) checked against manifest.schema.json and every rule of the
/// Python SDK. `path` names the manifest in every error.
pub fn manifest_from_json(raw: &Json, path: &str) -> Result<Manifest> {
    let where_ = path;
    validate(SchemaName::Manifest, raw, where_, None)?;
    let adapter_id = text(raw, where_, "id")?;
    let aliases = texts(raw, where_, "aliases")?;
    check_aliases(where_, &adapter_id, &aliases)?;
    let (context, input) = request_fields(
        where_,
        raw.get("context").unwrap_or(&Json::Null),
        raw.get("input").unwrap_or(&Json::Null),
    )?;
    let base = raw.get("base").cloned().unwrap_or(Json::Null);
    let max_options = whole(&base, where_, "max_options")?;
    let mut functions = IndexMap::new();
    for (key, body) in object(raw, where_, "functions")? {
        let calibration = match body.get("calibration") {
            None | Some(Json::Null) => None,
            Some(block) => Some(calibration_from_wire(block)?),
        };
        let mut by_format = IndexMap::new();
        if let Some(Json::Object(entries)) = body.get("calibration_by_format") {
            for (format_id, entry) in entries {
                by_format.insert(format_id.clone(), calibration_from_wire(entry)?);
            }
        }
        if !by_format.is_empty() && calibration.is_none() {
            let formats: Vec<&str> = by_format.keys().map(String::as_str).collect();
            return Err(manifest_error(format!(
                "{where_}: function {key} has calibration_by_format entries ({}) but calibration null; a function \
                 uncalibrated on the bfloat16 base stays uncalibrated in every format",
                formats.join(", ")
            )));
        }
        let (own_context, own_input) = if body.get("input").is_some() {
            request_fields(
                &format!("{where_}: function {key}"),
                body.get("context").unwrap_or(&Json::Null),
                &body["input"],
            )?
        } else {
            (context.clone(), input.clone())
        };
        let kind = text(body, where_, "type")?;
        let question_type = QuestionType::parse(&kind)
            .ok_or_else(|| shape(where_, "type", "a question type", body.get("type")))?;
        let labels = body.get("labels").cloned().unwrap_or(Json::Null);
        let spec = FunctionSpec {
            adapter_id: adapter_id.clone(),
            key: key.clone(),
            id: format!("{adapter_id}.{key}"),
            interface: text(body, where_, "interface")?,
            question_type,
            instructions: text(body, where_, "instructions")?,
            labels: labels_of(&labels, where_)?,
            abstain_labels: texts(body, where_, "abstain_labels")?,
            calibration,
            context: own_context,
            input: own_input,
            calibration_by_format: by_format,
        };
        check_function(&spec, max_options, where_)?;
        functions.insert(key.clone(), Arc::new(spec));
    }
    let tests = object(raw, where_, "tests")?.clone();
    check_tests(where_, &tests, &functions)?;
    let routes_to = texts(raw, where_, "routes_to")?;
    if !routes_to.is_empty() {
        let only: Vec<&Arc<FunctionSpec>> = functions.values().collect();
        let fits = only.len() == 1 && {
            let first = only[0];
            let fixed: Vec<String> = first.labels.fixed.keys().cloned().collect();
            first.interface == "route"
                && first.labels.source == LabelSource::Subset
                && fixed == routes_to
        };
        if !fits {
            return Err(manifest_error(format!(
                "{where_}: a router has exactly one route function with subset labels whose keys equal routes_to {} \
                 (dispatch offers the targets that lead to the requested interface)",
                list_repr(&routes_to)
            )));
        }
        if !only[0].labels.required.is_empty() {
            return Err(manifest_error(format!(
                "{where_}: a router's labels have no required keys; dispatch offers only the targets that lead to the \
                 requested interface"
            )));
        }
    }
    let licence = raw.get("licence").cloned().unwrap_or(Json::Null);
    Ok(Manifest {
        id: adapter_id,
        version: text(raw, where_, "version")?,
        aliases,
        licence_kind: text(&licence, where_, "kind")?,
        weights: raw.get("weights").cloned().unwrap_or(Json::Null),
        base_id: text(&base, where_, "id")?,
        base_version: text(&base, where_, "version")?,
        max_options,
        context,
        input,
        is_router: !routes_to.is_empty(),
        functions,
        routes_to,
        tests,
        raw: raw.clone(),
        path: path.to_owned(),
    })
}

/// The manifest in the YAML file at `path`, checked as the Python SDK checks it. Raises ManifestError
/// for a file that cannot be read, is not valid YAML or breaks a manifest rule.
pub fn load_manifest(path: &Path) -> Result<Manifest> {
    manifest_from_json(&read_manifest_yaml_file(path)?, &path.display().to_string())
}

/// The calibration a function decides with on a base of `format_id` (None: bfloat16): `calibration` on a
/// bfloat16 base or for an uncalibrated function, else its entry in `by_format` for exactly that format_id. Raises
/// ManifestError naming the function, the format and the `jeff calibrate` command when a calibrated function has no
/// entry for that INT4 base.
pub fn resolve_calibration(
    function_id: &str,
    calibration: Option<&Calibration>,
    by_format: &IndexMap<String, Calibration>,
    format_id: Option<&str>,
) -> Result<Option<Calibration>> {
    let (Some(format_id), Some(_)) = (format_id, calibration) else {
        return Ok(calibration.cloned());
    };
    match by_format.get(format_id) {
        Some(entry) => Ok(Some(entry.clone())),
        None => {
            let formats: Vec<&str> = by_format.keys().map(String::as_str).collect();
            let calibrated_on = if formats.is_empty() {
                "no INT4 base".to_owned()
            } else {
                formats.join(", ")
            };
            Err(manifest_error(format!(
                "{function_id} has no calibration for the INT4 base this engine serves ({format_id}); it is calibrated on \
                 the bfloat16 base and on {calibrated_on}. Fit its threshold on this base with `jeff calibrate \
                 {function_id} --base <the INT4 base folder>` (for an adapter served from an adapter folder, `jeff \
                 calibrate <that folder> --base <the INT4 base folder>`, which writes into the folder's manifest.yaml)"
            )))
        }
    }
}

/// The calibration `function` decides with on a base of `format_id` (None for bfloat16).
pub fn calibration_for(
    function: &FunctionSpec,
    format_id: Option<&str>,
) -> Result<Option<Calibration>> {
    resolve_calibration(
        &function.id,
        function.calibration.as_ref(),
        &function.calibration_by_format,
        format_id,
    )
}
