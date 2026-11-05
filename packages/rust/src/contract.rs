//! The contract shipped with the crate: JSON Schemas (draft 2020-12), the interface catalogue and the error
//! catalogue. Schema problems are worded as Python's jsonschema words them (see first_problem), because the Python
//! SDK is the reference.

use std::cmp::Ordering;
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use indexmap::IndexMap;
use jsonschema::error::ValidationErrorKind;
use jsonschema::paths::Location;
use jsonschema::{Registry, RegistryBuilder, ValidationError, Validator};
use serde_json::json;

use crate::errors::{ErrorType, JeffError, Result};
use crate::json::Json;
use crate::pyformat::{py_repr, py_sorted, py_str};
use crate::shipped;
use crate::yaml::read_yaml;

/// The keywords whose problems first_problem and validate word as Python does (or that never fail on their own); the
/// keyword coverage test checks every keyword the shipped schemas use is in this list.
#[cfg(test)]
pub(crate) const WORDED_KEYWORDS: &[&str] = &[
    "$defs",
    "$id",
    "$ref",
    "$schema",
    "additionalProperties",
    "allOf",
    "const",
    "dependentRequired",
    "description",
    "else",
    "enum",
    "exclusiveMinimum",
    "if",
    "items",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "not",
    "oneOf",
    "pattern",
    "properties",
    "propertyNames",
    "required",
    "then",
    "title",
    "type",
    "uniqueItems",
];

macro_rules! schema_names {
    ($($variant:ident => $stem:literal),* $(,)?) => {
        /// A schema of contract/schemas.
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        pub enum SchemaName {
            $(
                #[doc = concat!("contract/schemas/", $stem, ".schema.json.")]
                $variant
            ),*
        }

        impl SchemaName {
            /// Every schema, in the order this crate lists them.
            pub const ALL: [SchemaName; 19] = [$(SchemaName::$variant),*];

            /// The file name without `.schema.json`.
            pub fn stem(self) -> &'static str {
                match self { $(SchemaName::$variant => $stem),* }
            }
        }
    };
}

schema_names!(
    Manifest => "manifest", Decision => "decision", Graph => "graph", GraphResult => "graph_result",
    Socket => "socket", DecideRequest => "decide_request", DecideResponse => "decide_response",
    FunctionsResponse => "functions_response", GraphsResponse => "graphs_response",
    SystemoneRequest => "systemone_request", SystemoneResponse => "systemone_response",
    ModelsResponse => "models_response", ServedRequest => "served_request", AdaptersResponse => "adapters_response",
    HealthResponse => "health_response", LoginRequest => "login_request", LoginResponse => "login_response",
    AccountsKeys => "accounts_keys", AccountsUsage => "accounts_usage",
);

impl SchemaName {
    /// The schema's `$id`.
    pub fn id(self) -> String {
        format!("https://jeff.dev/schemas/{}.schema.json", self.stem())
    }
}

fn contract_error(message: String) -> JeffError {
    JeffError::new(ErrorType::ContractError, message)
}

struct Contract {
    registry: Registry<'static>,
    documents: HashMap<String, Json>,
    validators: Mutex<HashMap<String, Arc<Validator>>>,
}

static CONTRACT: OnceLock<std::result::Result<Contract, JeffError>> = OnceLock::new();

fn load() -> Result<Contract> {
    let mut builder: RegistryBuilder<'static> = Registry::new();
    let mut documents = HashMap::new();
    for name in SchemaName::ALL {
        let path = format!("contract/schemas/{}.schema.json", name.stem());
        let text = shipped::file(&path).ok_or_else(|| {
            contract_error(format!("Schema {path} is missing from the shipped copy; run packages/rust/scripts/sync-shipped.sh"))
        })?;
        let schema: Json = serde_json::from_str(text)
            .map_err(|error| contract_error(format!("Schema {path} is not JSON: {error}")))?;
        if schema.get("$id").and_then(Json::as_str) != Some(name.id().as_str()) {
            return Err(contract_error(format!(
                "Schema {path} does not have the $id {}",
                name.id()
            )));
        }
        builder = builder.add(name.id(), schema.clone()).map_err(|error| {
            contract_error(format!("Schema {path} cannot be registered: {error}"))
        })?;
        documents.insert(name.id(), schema);
    }
    let registry = builder
        .prepare()
        .map_err(|error| contract_error(format!("The schemas cannot be prepared: {error}")))?;
    Ok(Contract {
        registry,
        documents,
        validators: Mutex::new(HashMap::new()),
    })
}

fn contract() -> Result<&'static Contract> {
    CONTRACT.get_or_init(load).as_ref().map_err(Clone::clone)
}

impl Contract {
    fn reference(&self, name: SchemaName, definition: Option<&str>) -> Result<String> {
        let Some(definition) = definition else {
            return Ok(name.id());
        };
        let defined = self.documents[&name.id()]
            .get("$defs")
            .and_then(|defs| defs.get(definition))
            .is_some();
        if !defined {
            return Err(contract_error(format!(
                "The {} schema has no definition {}",
                name.stem(),
                py_str(definition)
            )));
        }
        Ok(format!("{}#/$defs/{definition}", name.id()))
    }

    fn validator(&'static self, reference: &str) -> Result<Arc<Validator>> {
        if let Some(found) = self
            .validators
            .lock()
            .expect("the validator cache lock")
            .get(reference)
        {
            return Ok(Arc::clone(found));
        }
        let built = jsonschema::options()
            .with_registry(&self.registry)
            .build(&json!({"$ref": reference}))
            .map_err(|error| {
                contract_error(format!(
                    "The schema {reference} cannot be compiled: {error}"
                ))
            })?;
        let built = Arc::new(built);
        self.validators
            .lock()
            .expect("the validator cache lock")
            .insert(reference.to_owned(), Arc::clone(&built));
        Ok(built)
    }

    /// The schema node an error's evaluation path reaches, with the document and JSON pointer it sits at. The path
    /// starts at the wrapper `{"$ref": root}` every validator here is built from.
    fn node_at(&self, root: &str, path: &Location) -> Result<(String, String, &Json)> {
        let (document, pointer, node, _) = self.walk(root, path)?;
        Ok((document, pointer, node))
    }

    /// Where an error comes in Python's jsonschema, which checks a schema's keywords (and a `properties` map's
    /// properties) in the order the schema writes them: at each step of the error's evaluation path, the step's
    /// place among its schema node's keys, or its index in a list of schemas.
    fn python_order(&self, root: &str, error: &ValidationError<'_>) -> Result<Vec<usize>> {
        Ok(self.walk(root, error.evaluation_path())?.3)
    }

    fn walk(&self, root: &str, path: &Location) -> Result<(String, String, &Json, Vec<usize>)> {
        let mut document = String::new();
        let mut pointer = String::new();
        let mut node: Option<&Json> = None;
        let mut ranks = Vec::new();
        for segment in path.iter() {
            let segment = segment.to_string();
            ranks.push(match node {
                None => 0,
                Some(Json::Object(map)) => {
                    map.keys().position(|key| *key == segment).ok_or_else(|| {
                        contract_error(format!(
                            "first_problem: {document}#{pointer} has no {}",
                            py_str(&segment)
                        ))
                    })?
                }
                Some(Json::Array(_)) => segment.parse::<usize>().map_err(|_| {
                    contract_error(format!(
                        "first_problem: {document}#{pointer} is a list, which has no {}",
                        py_str(&segment)
                    ))
                })?,
                Some(_) => {
                    return Err(contract_error(format!(
                        "first_problem: {document}#{pointer} is not a schema node with {}",
                        py_str(&segment)
                    )));
                }
            });
            if segment == "$ref" {
                let target = match node {
                    None => root.to_owned(),
                    Some(current) => current
                        .get("$ref")
                        .and_then(Json::as_str)
                        .map(str::to_owned)
                        .ok_or_else(|| {
                            contract_error(format!(
                                "first_problem: {document}#{pointer} has no $ref to follow"
                            ))
                        })?,
                };
                let (id, fragment) = target.split_once('#').unwrap_or((target.as_str(), ""));
                if !id.is_empty() {
                    document = id.to_owned();
                }
                pointer = fragment.to_owned();
                let doc = self.documents.get(&document).ok_or_else(|| {
                    contract_error(format!(
                        "first_problem: no shipped schema has $id {document}"
                    ))
                })?;
                node = Some(doc.pointer(&pointer).ok_or_else(|| {
                    contract_error(format!(
                        "first_problem: {document}#{pointer} does not exist"
                    ))
                })?);
            } else {
                let current = node.ok_or_else(|| {
                    contract_error(format!(
                        "first_problem: the evaluation path {path} does not start at a $ref"
                    ))
                })?;
                let next = match current {
                    Json::Array(items) => segment
                        .parse::<usize>()
                        .ok()
                        .and_then(|index| items.get(index)),
                    Json::Object(map) => map.get(&segment),
                    _ => None,
                };
                node = Some(next.ok_or_else(|| {
                    contract_error(format!(
                        "first_problem: {document}#{pointer} has no {}",
                        py_str(&segment)
                    ))
                })?);
                pointer.push('/');
                pointer.push_str(&segment.replace('~', "~0").replace('/', "~1"));
            }
        }
        let node =
            node.ok_or_else(|| contract_error("first_problem: an empty evaluation path".into()))?;
        Ok((document, pointer, node, ranks))
    }

    /// `errors` in the order Python's jsonschema yields them (a stable sort: errors the order cannot tell apart keep
    /// the validator's order).
    fn in_python_order<'a, 'b>(
        &self,
        root: &str,
        errors: impl IntoIterator<Item = &'a ValidationError<'b>>,
    ) -> Result<Vec<&'a ValidationError<'b>>>
    where
        'b: 'a,
    {
        let mut keyed = errors
            .into_iter()
            .map(|error| Ok((self.python_order(root, error)?, error)))
            .collect::<Result<Vec<_>>>()?;
        keyed.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(keyed.into_iter().map(|(_, error)| error).collect())
    }

    /// The message Python's jsonschema gives for this error (jsonschema/_keywords.py).
    fn python_message(&'static self, root: &str, error: &ValidationError<'_>) -> Result<String> {
        let instance = py_repr(error.instance());
        let pick = |limit: u64, at: u64, when: &str, otherwise: &str| -> String {
            format!("{instance} {}", if limit == at { when } else { otherwise })
        };
        Ok(match error.kind() {
            ValidationErrorKind::AdditionalProperties { unexpected } => {
                let extras = py_sorted(unexpected.iter().cloned());
                let joined = extras
                    .iter()
                    .map(|extra| py_str(extra))
                    .collect::<Vec<_>>()
                    .join(", ");
                format!(
                    "Additional properties are not allowed ({joined} {} unexpected)",
                    if extras.len() == 1 { "was" } else { "were" }
                )
            }
            ValidationErrorKind::Constant { expected_value } => {
                format!("{} was expected", py_repr(expected_value))
            }
            ValidationErrorKind::Enum { options } => {
                format!("{instance} is not one of {}", py_repr(options))
            }
            ValidationErrorKind::ExclusiveMinimum { limit } => {
                format!(
                    "{instance} is less than or equal to the minimum of {}",
                    py_repr(limit)
                )
            }
            ValidationErrorKind::ExclusiveMaximum { limit } => {
                format!(
                    "{instance} is greater than or equal to the maximum of {}",
                    py_repr(limit)
                )
            }
            ValidationErrorKind::Minimum { limit } => {
                format!("{instance} is less than the minimum of {}", py_repr(limit))
            }
            ValidationErrorKind::Maximum { limit } => format!(
                "{instance} is greater than the maximum of {}",
                py_repr(limit)
            ),
            ValidationErrorKind::MinItems { limit } => {
                pick(*limit, 1, "should be non-empty", "is too short")
            }
            ValidationErrorKind::MaxItems { limit } => {
                pick(*limit, 0, "is expected to be empty", "is too long")
            }
            ValidationErrorKind::MinLength { limit } => {
                pick(*limit, 1, "should be non-empty", "is too short")
            }
            ValidationErrorKind::MaxLength { limit } => {
                pick(*limit, 0, "is expected to be empty", "is too long")
            }
            ValidationErrorKind::MinProperties { limit } => pick(
                *limit,
                1,
                "should be non-empty",
                "does not have enough properties",
            ),
            ValidationErrorKind::MaxProperties { limit } => pick(
                *limit,
                0,
                "is expected to be empty",
                "has too many properties",
            ),
            ValidationErrorKind::Not { schema } => {
                format!("{instance} should not be valid under {}", py_repr(schema))
            }
            ValidationErrorKind::OneOfNotValid { .. } => {
                format!("{instance} is not valid under any of the given schemas")
            }
            ValidationErrorKind::OneOfMultipleValid { .. } => {
                let (document, pointer, node) = self.node_at(root, error.evaluation_path())?;
                let alternatives = node.as_array().ok_or_else(|| {
                    contract_error(format!(
                        "first_problem: {document}#{pointer} is not a oneOf list"
                    ))
                })?;
                let mut valid = Vec::new();
                for index in 0..alternatives.len() {
                    if self
                        .validator(&format!("{document}#{pointer}/{index}"))?
                        .is_valid(error.instance())
                    {
                        valid.push(index);
                    }
                }
                // Python lists the alternatives valid after the first, then the first.
                let mut order: Vec<usize> = valid.iter().skip(1).copied().collect();
                order.extend(valid.first().copied());
                let reprs = order
                    .iter()
                    .map(|index| py_repr(&alternatives[*index]))
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("{instance} is valid under each of {reprs}")
            }
            ValidationErrorKind::Pattern { pattern } => {
                format!("{instance} does not match {}", py_str(pattern))
            }
            ValidationErrorKind::Required { property } => {
                let path = error.evaluation_path();
                if path
                    .iter()
                    .last()
                    .map(|segment| segment.to_string())
                    .as_deref()
                    == Some("dependentRequired")
                {
                    let (_, _, node) = self.node_at(root, path)?;
                    let dependent = node.as_object().and_then(|rules| {
                        rules.iter().find(|(key, needed)| {
                            error.instance().get(key.as_str()).is_some()
                                && needed
                                    .as_array()
                                    .is_some_and(|list| list.contains(property))
                        })
                    });
                    let (key, _) = dependent.ok_or_else(|| {
                        contract_error(format!(
                            "first_problem: no dependentRequired rule needs {}",
                            py_repr(property)
                        ))
                    })?;
                    format!("{} is a dependency of {}", py_repr(property), py_str(key))
                } else {
                    format!("{} is a required property", py_repr(property))
                }
            }
            ValidationErrorKind::Type { .. } => {
                let (document, pointer, node) = self.node_at(root, error.evaluation_path())?;
                let names: Vec<String> = match node {
                    Json::String(name) => vec![py_str(name)],
                    Json::Array(names) => {
                        names.iter().filter_map(Json::as_str).map(py_str).collect()
                    }
                    _ => {
                        return Err(contract_error(format!(
                            "first_problem: {document}#{pointer} is not a type"
                        )));
                    }
                };
                format!("{instance} is not of type {}", names.join(", "))
            }
            ValidationErrorKind::UniqueItems => format!("{instance} has non-unique elements"),
            ValidationErrorKind::FalseSchema => format!("False schema does not allow {instance}"),
            ValidationErrorKind::PropertyNames { error: inner } => {
                self.python_message(root, inner)?
            }
            other => {
                return Err(contract_error(format!(
                    "first_problem has no Python wording for the {} problem at {}",
                    other.keyword(),
                    error.instance_path()
                )));
            }
        })
    }
}

fn path_of(error: &ValidationError<'_>) -> Vec<String> {
    error
        .instance_path()
        .iter()
        .map(|segment| segment.to_string())
        .collect()
}

/// Python's `min(problems, key=lambda error: list(map(str, error.absolute_path)))`: the first of the smallest.
fn closest<'a, 'b>(problems: &[&'a ValidationError<'b>]) -> Option<&'a ValidationError<'b>> {
    let mut best: Option<&ValidationError<'b>> = None;
    for &problem in problems {
        if best.is_none_or(|current| path_of(problem) < path_of(current)) {
            best = Some(problem);
        }
    }
    best
}

/// Python's `validate` sort: by absolute path, array indices compared as numbers.
fn compare_paths(a: &[String], b: &[String]) -> Ordering {
    for (x, y) in a.iter().zip(b) {
        let order = match (x.parse::<u64>(), y.parse::<u64>()) {
            (Ok(p), Ok(q)) => p.cmp(&q),
            _ => x.cmp(y),
        };
        if order != Ordering::Equal {
            return order;
        }
    }
    a.len().cmp(&b.len())
}

/// Whether `document` matches the schema `name` (or its `$defs` entry `definition`). Raises ContractError only
/// when the shipped schema itself cannot be read or the definition does not exist.
pub fn is_valid(name: SchemaName, document: &Json, definition: Option<&str>) -> Result<bool> {
    let contract = contract()?;
    Ok(contract
        .validator(&contract.reference(name, definition)?)?
        .is_valid(document))
}

/// Checks `document` against the schema `name` (or its `$defs` entry `definition`). Raises ContractError naming
/// `where_`, the schema and every problem, worded as the Python SDK words it, when it does not match.
pub fn validate(
    name: SchemaName,
    document: &Json,
    where_: &str,
    definition: Option<&str>,
) -> Result<()> {
    let contract = contract()?;
    let root = contract.reference(name, definition)?;
    let validator = contract.validator(&root)?;
    let found: Vec<ValidationError<'_>> = validator.iter_errors(document).collect();
    if found.is_empty() {
        return Ok(());
    }
    // Python sorts the errors iter_errors yields by path; a stable sort keeps their schema order within a path.
    let mut problems = contract.in_python_order(&root, &found)?;
    problems.sort_by(|a, b| compare_paths(&path_of(a), &path_of(b)));
    let mut lines = Vec::new();
    for problem in problems {
        lines.push(format!(
            "  at /{}: {}",
            path_of(problem).join("/"),
            contract.python_message(&root, problem)?
        ));
        if let ValidationErrorKind::OneOfNotValid { context }
        | ValidationErrorKind::AnyOf { context } = problem.kind()
        {
            for sub in contract.in_python_order(&root, context.iter().flatten())? {
                lines.push(format!("    - {}", contract.python_message(&root, sub)?));
            }
        }
    }
    let shape = match definition {
        None => format!("the {} schema", name.stem()),
        Some(definition) => format!("{definition} in the {} schema", name.stem()),
    };
    Err(contract_error(format!(
        "{where_} does not match {shape}:\n{}",
        lines.join("\n")
    )))
}

/// One short line naming the first thing wrong with a document that does not match the schema (Python's
/// `jeff.contract.first_problem`; see Task 6 of the plan).
pub fn first_problem(
    name: SchemaName,
    document: &Json,
    definition: Option<&str>,
) -> Result<String> {
    let contract = contract()?;
    let root = contract.reference(name, definition)?;
    let problems: Vec<ValidationError<'static>> = contract
        .validator(&root)?
        .iter_errors(document)
        .map(ValidationError::to_owned)
        .collect();
    let problems: Vec<&ValidationError<'static>> = contract.in_python_order(&root, &problems)?;
    let mut problem = closest(&problems).ok_or_else(|| {
        contract_error(format!(
            "first_problem was asked about a document that matches the {} schema",
            name.stem()
        ))
    })?;
    while let ValidationErrorKind::OneOfNotValid { context } = problem.kind() {
        let mut best: Option<(&Vec<ValidationError<'static>>, usize)> = None;
        for alternative in context.iter().filter(|errors| !errors.is_empty()) {
            let depth = alternative
                .iter()
                .map(|error| path_of(error).len())
                .min()
                .expect("a non-empty alternative");
            let better = match best {
                None => true,
                Some((current, current_depth)) => {
                    depth > current_depth
                        || (depth == current_depth && alternative.len() < current.len())
                }
            };
            if better {
                best = Some((alternative, depth));
            }
        }
        let Some((alternative, _)) = best else { break };
        problem = closest(&contract.in_python_order(&root, alternative)?)
            .expect("a non-empty alternative");
    }
    let message = match problem.kind() {
        ValidationErrorKind::MaxItems { limit } if problem.instance().is_array() => {
            format!(
                "{} items, at most {limit} are allowed",
                problem.instance().as_array().expect("an array").len()
            )
        }
        ValidationErrorKind::MaxProperties { limit } if problem.instance().is_object() => {
            format!(
                "{} properties, at most {limit} are allowed",
                problem.instance().as_object().expect("an object").len()
            )
        }
        _ => {
            let message = contract.python_message(&root, problem)?;
            if message.chars().count() > 200 {
                format!("{}...", message.chars().take(200).collect::<String>())
            } else {
                message
            }
        }
    };
    let at = path_of(problem).join("/");
    Ok(if at.is_empty() {
        message
    } else {
        format!("{message} (at /{at})")
    })
}

/// The question type a function compiles to on the server.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum QuestionType {
    /// One of a set of option keys.
    Choice,
    /// Yes or no (true or false).
    Noul,
    /// One level of an ordered scale.
    Score,
}

impl QuestionType {
    /// The type's name: `choice`, `noul` or `score`.
    pub fn as_str(self) -> &'static str {
        match self {
            QuestionType::Choice => "choice",
            QuestionType::Noul => "noul",
            QuestionType::Score => "score",
        }
    }

    /// The type named `text`; none for any other text.
    pub fn parse(text: &str) -> Option<QuestionType> {
        match text {
            "choice" => Some(QuestionType::Choice),
            "noul" => Some(QuestionType::Noul),
            "score" => Some(QuestionType::Score),
            _ => None,
        }
    }
}

/// An interface of contract/interfaces.yaml: what a function implementing it takes and returns.
#[derive(Clone, Debug, PartialEq)]
pub struct InterfaceSpec {
    /// The interface's name (classify, route, rate and so on).
    pub name: String,
    /// The question type its functions compile to.
    pub question_type: QuestionType,
    /// The exact label keys, in order, every implementing function must have; None for an open set.
    pub values: Option<Vec<String>>,
    /// What the interface decides, in the contract's words.
    pub description: String,
}

fn shipped_yaml(path: &str) -> Result<Json> {
    let text = shipped::file(path).ok_or_else(|| {
        contract_error(format!(
            "{path} is missing from the shipped copy; run packages/rust/scripts/sync-shipped.sh"
        ))
    })?;
    read_yaml(text, path)
}

fn required_text(spec: &Json, key: &str, context: &str) -> Result<String> {
    spec.get(key)
        .and_then(Json::as_str)
        .map(str::to_owned)
        .ok_or_else(|| {
            contract_error(format!(
                "{context} is missing its '{key}' string (got {})",
                py_repr(spec.get(key).unwrap_or(&Json::Null))
            ))
        })
}

static INTERFACES: OnceLock<std::result::Result<IndexMap<String, InterfaceSpec>, JeffError>> =
    OnceLock::new();

/// Every interface of the shipped contract/interfaces.yaml, by name, in the file's order. Raises
/// ContractError when the shipped file is missing or malformed.
pub fn interfaces() -> Result<&'static IndexMap<String, InterfaceSpec>> {
    INTERFACES
        .get_or_init(|| {
            let path = "contract/interfaces.yaml";
            let raw = shipped_yaml(path)?;
            let entries = raw.as_object().ok_or_else(|| contract_error(format!("{path} is not a mapping")))?;
            let mut found = IndexMap::new();
            for (name, spec) in entries {
                let kind = spec.get("question_type").unwrap_or(&Json::Null);
                let question_type = kind.as_str().and_then(QuestionType::parse).ok_or_else(|| {
                    contract_error(format!("{path}: interface {name} has an unknown question_type {}", py_repr(kind)))
                })?;
                let values = match spec.get("values") {
                    None => None,
                    Some(list) => Some(
                        list.as_array()
                            .and_then(|items| items.iter().map(|item| item.as_str().map(str::to_owned)).collect::<Option<Vec<_>>>())
                            .ok_or_else(|| contract_error(format!("{path}: interface {name} has values that are not a list of strings")))?,
                    ),
                };
                let description = required_text(spec, "description", &format!("{path}: interface {name}"))?;
                found.insert(name.clone(), InterfaceSpec { name: name.clone(), question_type, values, description });
            }
            Ok(found)
        })
        .as_ref()
        .map_err(Clone::clone)
}

/// An entry of contract/errors.yaml.
#[derive(Clone, Debug, PartialEq)]
pub struct ErrorSpec {
    /// The error type's name.
    pub name: String,
    /// The HTTP status a server answers with; None for an error only a local process raises.
    pub status: Option<u16>,
    /// When the error is raised, in the contract's words.
    pub meaning: String,
}

static ERRORS: OnceLock<std::result::Result<IndexMap<String, ErrorSpec>, JeffError>> =
    OnceLock::new();

/// Every entry of the shipped contract/errors.yaml, by name, in the file's order. Raises ContractError
/// when the shipped file is missing or malformed.
pub fn error_catalogue() -> Result<&'static IndexMap<String, ErrorSpec>> {
    ERRORS
        .get_or_init(|| {
            let path = "contract/errors.yaml";
            let raw = shipped_yaml(path)?;
            let entries = raw
                .as_object()
                .ok_or_else(|| contract_error(format!("{path} is not a mapping")))?;
            let mut found = IndexMap::new();
            for (name, spec) in entries {
                let status = match spec.get("status") {
                    Some(Json::Null) => None,
                    Some(value) => match value.as_u64() {
                        Some(code @ 400..=599) => Some(code as u16),
                        _ => {
                            return Err(contract_error(format!(
                                "{path}: {name} has status {}; use an HTTP error status or null",
                                py_repr(value)
                            )));
                        }
                    },
                    None => {
                        return Err(contract_error(format!(
                            "{path}: {name} has no status; use an HTTP error status or null"
                        )));
                    }
                };
                let meaning = required_text(spec, "meaning", &format!("{path}: {name}"))?;
                found.insert(
                    name.clone(),
                    ErrorSpec {
                        name: name.clone(),
                        status,
                        meaning,
                    },
                );
            }
            Ok(found)
        })
        .as_ref()
        .map_err(Clone::clone)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn decision() -> Json {
        json!({"value": 2, "confidence": 0.5, "abstained": "no", "calibrated": true, "interface": "rate", "function": "f",
            "version": "1", "probabilities": {}, "score": null, "route": [], "decided_by": "x", "escalations": [],
            "timing": {}, "evidence_refs": []})
    }

    #[test]
    fn every_schema_file_is_a_schema_name_with_its_id() {
        let directory = crate::shipped::repository_root().join("contract/schemas");
        let mut files: Vec<String> = std::fs::read_dir(directory)
            .unwrap()
            .map(|entry| {
                entry
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .replace(".schema.json", "")
            })
            .collect();
        files.sort();
        let mut names: Vec<&str> = SchemaName::ALL.iter().map(|name| name.stem()).collect();
        names.sort();
        assert_eq!(names, files);
        assert_eq!(
            SchemaName::GraphResult.id(),
            "https://jeff.dev/schemas/graph_result.schema.json"
        );
        for name in SchemaName::ALL {
            assert!(is_valid(name, &json!(null), None).is_ok());
        }
    }

    #[test]
    fn validate_names_where_the_document_is_wrong_in_pythons_words() {
        let error = validate(
            SchemaName::Decision,
            &json!({"value": 1}),
            "a decision",
            None,
        )
        .unwrap_err();
        assert_eq!(error.kind(), ErrorType::ContractError);
        assert!(error.message().starts_with("a decision does not match the decision schema:\n  at /: 'confidence' is a required property"));
        let shaped = validate(
            SchemaName::Socket,
            &json!({"op": "predict", "id": 1, "adapter": "a", "rows": []}),
            "m",
            Some("predict"),
        );
        assert_eq!(
            shaped.unwrap_err().message(),
            "m does not match predict in the socket schema:\n  at /rows: [] should be non-empty"
        );
        assert!(
            validate(SchemaName::Socket, &json!({"op": "x"}), "m", Some("nope"))
                .unwrap_err()
                .message()
                .contains("The socket schema has no definition 'nope'")
        );
    }

    #[test]
    fn one_definition_of_a_schema_can_be_checked_across_schemas() {
        assert!(
            is_valid(
                SchemaName::Socket,
                &json!({"op": "predict", "id": 1, "adapter": "a", "rows": [{}]}),
                Some("predict")
            )
            .unwrap()
        );
        assert!(
            !is_valid(
                SchemaName::Socket,
                &json!({"op": "predict", "id": 1, "adapter": "a", "rows": []}),
                Some("predict")
            )
            .unwrap()
        );
        assert!(is_valid(SchemaName::DecideResponse, &json!({"decisions": []}), None).unwrap());
    }

    #[test]
    fn first_problem_gives_the_line_the_python_sdk_gives() {
        let cases: Vec<(SchemaName, Option<&str>, Json, &str)> = vec![
            (
                SchemaName::Socket,
                None,
                json!({"op": "predict", "id": 4, "adapter": "a"}),
                "'rows' is a required property",
            ),
            (
                SchemaName::Socket,
                None,
                json!({"op": "stats", "id": 4, "extra": 1}),
                "Additional properties are not allowed ('extra' was unexpected)",
            ),
            (
                SchemaName::Socket,
                None,
                json!({"op": "nope", "id": 3}),
                "'nope' is not one of ['installed', 'reload', 'stats', 'health'] (at /op)",
            ),
            (
                SchemaName::DecideRequest,
                None,
                json!({"calls": [{"function": "f", "input": "x"}], "on_low_confidence": "maybe"}),
                "'maybe' is not one of ['return', 'error'] (at /on_low_confidence)",
            ),
            (
                SchemaName::DecideRequest,
                None,
                json!({"calls": [{"function": "f", "input": "x", "labels": 5},
                {"function": "f", "input": "x", "labels": 5}, {"function": "f", "input": "x", "labels": 5}]}),
                "5 is not of type 'object' (at /calls/0/labels)",
            ),
            (
                SchemaName::Socket,
                Some("predict"),
                json!({"op": "predict", "id": 1, "adapter": "a", "rows": []}),
                "[] should be non-empty (at /rows)",
            ),
            (
                SchemaName::Decision,
                None,
                json!({"value": 1}),
                "'confidence' is a required property",
            ),
            (
                SchemaName::DecideRequest,
                None,
                json!({"calls": vec![json!({"function": "f", "input": "x"}); 257]}),
                "257 items, at most 256 are allowed (at /calls)",
            ),
            (
                SchemaName::Decision,
                None,
                decision(),
                "'no' is not of type 'boolean' (at /abstained)",
            ),
        ];
        for (name, definition, document, expected) in cases {
            assert_eq!(
                first_problem(name, &document, definition).unwrap(),
                expected,
                "{document}"
            );
        }
        let mut nested = decision();
        nested["abstained"] = json!(true);
        nested["confidence"] = json!("x");
        assert_eq!(
            first_problem(
                SchemaName::DecideResponse,
                &json!({"decisions": [nested]}),
                None
            )
            .unwrap(),
            "'x' is not of type 'number', 'null' (at /decisions/0/confidence)"
        );
        let valid = json!({"decisions": []});
        assert!(
            first_problem(SchemaName::DecideResponse, &valid, None)
                .unwrap_err()
                .message()
                .contains("matches")
        );
    }

    #[test]
    fn additional_properties_are_listed_sorted_as_python_lists_them() {
        let problem = first_problem(
            SchemaName::Socket,
            &json!({"op": "stats", "id": 4, "zz": 1, "aa": 2}),
            Some("simple"),
        )
        .unwrap();
        assert_eq!(
            problem,
            "Additional properties are not allowed ('aa', 'zz' were unexpected)"
        );
    }

    #[test]
    fn every_keyword_the_shipped_schemas_use_has_a_python_wording() {
        fn walk(node: &Json, skip_children: bool, found: &mut std::collections::BTreeSet<String>) {
            match node {
                Json::Object(map) => {
                    for (key, child) in map {
                        if !skip_children {
                            found.insert(key.clone());
                        }
                        let names = matches!(
                            key.as_str(),
                            "properties" | "$defs" | "dependentRequired" | "patternProperties"
                        );
                        walk(child, names && !skip_children, found);
                    }
                }
                Json::Array(items) => items.iter().for_each(|item| walk(item, false, found)),
                _ => {}
            }
        }
        let mut found = std::collections::BTreeSet::new();
        for name in SchemaName::ALL {
            let text =
                crate::shipped::file(&format!("contract/schemas/{}.schema.json", name.stem()))
                    .unwrap();
            walk(&serde_json::from_str(text).unwrap(), false, &mut found);
        }
        let unknown: Vec<&String> = found
            .iter()
            .filter(|keyword| !WORDED_KEYWORDS.contains(&keyword.as_str()))
            .collect();
        assert!(
            unknown.is_empty(),
            "keywords with no Python wording in contract.rs: {unknown:?}"
        );
    }

    #[test]
    fn the_interfaces_have_their_question_types_and_values() {
        let found = interfaces().unwrap();
        assert_eq!(found["detect"].question_type, QuestionType::Noul);
        assert_eq!(
            found["equivalent"].values.as_deref().unwrap(),
            ["match", "possible_match", "no_match"]
        );
        assert!(found["classify"].values.is_none());
    }

    #[test]
    fn there_is_one_error_type_per_entry_of_errors_yaml_in_its_order() {
        let names: Vec<&str> = error_catalogue()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        let types: Vec<&str> = ErrorType::ALL.iter().map(|kind| kind.name()).collect();
        assert_eq!(names, types);
        for kind in ErrorType::ALL {
            assert_eq!(
                kind.meaning(),
                error_catalogue().unwrap()[kind.name()].meaning
            );
        }
        assert_eq!(error_catalogue().unwrap()["NotInstalled"].status, Some(424));
        assert_eq!(error_catalogue().unwrap()["RuntimeMissing"].status, None);
        assert_eq!(
            error_catalogue().unwrap()["UsageLimitReached"].status,
            Some(402)
        );
    }
}
