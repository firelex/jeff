//! Every error Jeff raises (contract/errors.yaml): one `ErrorType` per entry, all carried by `JeffError`. `details`
//! holds the error's wire details exactly as the Python SDK sends them (snake_case keys), so an error crosses the
//! engine socket, HTTP and the channel unchanged; the typed getters read them and never invent a missing value.

use std::fmt;
use std::sync::Arc;

use crate::json::{Json, JsonMap};
use crate::pyformat::py_repr;

/// The result of every fallible operation of this crate.
pub type Result<T, E = JeffError> = std::result::Result<T, E>;
/// An error raised by code the caller supplies (an llm or human step, a code node, a token function).
pub type BoxError = Box<dyn std::error::Error + Send + Sync>;

macro_rules! error_types {
    ($($name:ident => $meaning:literal),* $(,)?) => {
        /// The type of a Jeff error: one per entry of contract/errors.yaml, named as there.
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        pub enum ErrorType {
            $(
                #[doc = $meaning]
                $name
            ),*
        }

        impl ErrorType {
            /// Every type, in the order of contract/errors.yaml.
            pub const ALL: [ErrorType; 35] = [$(ErrorType::$name),*];

            /// The meaning contract/errors.yaml gives, as the variant's docs show it.
            #[cfg(test)]
            pub(crate) fn meaning(self) -> &'static str {
                match self { $(ErrorType::$name => $meaning),* }
            }

            /// The name contract/errors.yaml and the wire use.
            pub fn name(self) -> &'static str {
                match self { $(ErrorType::$name => stringify!($name)),* }
            }
        }
    };
}

// Each meaning is contract/errors.yaml's, word for word.
error_types!(
    JeffError => "Base class; never raised directly.",
    ContractError => "A document does not match its contract schema.",
    ManifestError => "An adapter manifest or the routing tree is invalid.",
    InvalidReference => "A function reference is malformed.",
    UnknownFunction => "No function or adapter in the catalogue has this ID, alias or version.",
    NotInstalled => "The function exists but its adapter is not loaded.",
    MissingContext => "Required context fields are missing.",
    UnknownContext => "Context fields the function does not have.",
    MissingLabels => "The function takes its labels from the caller and none were given.",
    InvalidLabels => "Labels are given where they are fixed, or keys collide or are malformed.",
    TooManyLabels => "More labels than the function was trained on.",
    TooManyHandles => "A channel connection already holds the most bound-function handles it may (1024); release one first.",
    InterfaceMismatch => "A function was called through an interface it does not implement.",
    LowConfidence => "The decision abstained and on_low_confidence was 'error'; details carry the decision.",
    EscalationFailed => "An escalation step failed.",
    EscalationBudgetExceeded => "An escalation ran past its budget_ms.",
    InvalidPolicy => "on_low_confidence is not valid for this call.",
    GraphError => "A decision graph is invalid or cannot run.",
    RuntimeMissing => "Local mode needs the jeff-runtime package (Python), or uv to start the engine (Node, Rust, C#, Java).",
    ProprietaryWeights => "A proprietary adapter's weights need a licence or a local folder.",
    CalibrationError => "Calibration cannot reach the target or has no data.",
    EngineError => "The engine failed or broke its contract.",
    InputTooLong => "The request is longer than the token limit; it was refused, never truncated.",
    BackendUnavailable => "The engine backend cannot run on this machine.",
    Busy => "The engine queue is full; retry after the Retry-After seconds.",
    DeadlineExceeded => "The request's deadline passed before it ran.",
    ProtocolMismatch => "The two ends of the engine socket speak different protocol versions.",
    NotReady => "The engine is still starting.",
    Unauthorised => "Missing or invalid credentials.",
    NotLicensed => "The caller's token does not include this proprietary function's licence.",
    RateLimited => "The caller exceeded its rate limit; retry after the Retry-After seconds.",
    UsageLimitReached => "The account has used its input-token allowance for the billing period; calls resume when the next period starts.",
    InvalidRequest => "The request does not match the API schema.",
    Conflict => "The request does not fit the current state of the factory run.",
    FactoryOff => "The factory is off on this server.",
);

impl ErrorType {
    /// The type named `name` on the wire, if there is one.
    pub fn from_name(name: &str) -> Option<ErrorType> {
        ErrorType::ALL.into_iter().find(|kind| kind.name() == name)
    }
}

/// `JeffError`'s payload, boxed so that `JeffError` itself is two machine words: a fallible function can then
/// return `Result<T, JeffError>` without tripping `clippy::result_large_err` (the payload below is well over its
/// 128-byte default threshold on its own), and that holds for every `Result<T>` this crate's growing error type
/// will ever be used in, not just the functions below.
#[derive(Clone)]
struct JeffErrorPayload {
    message: String,
    details: JsonMap,
    request_id: Option<String>,
    notes: Vec<String>,
    source: Option<Arc<dyn std::error::Error + Send + Sync>>,
}

/// A Jeff error: its type, the exact message the Python SDK gives, its wire details, the request id of the server
/// answer that carried it (remote mode), and notes about further failures met while handling it.
#[derive(Clone)]
pub struct JeffError {
    kind: ErrorType,
    payload: Box<JeffErrorPayload>,
}

impl JeffError {
    /// An error of type `kind` with `message` and no details.
    pub fn new(kind: ErrorType, message: impl Into<String>) -> JeffError {
        JeffError::with_details(kind, message, JsonMap::new())
    }

    /// An error of type `kind` with `message` and the structured `details` (such as `function_id` or
    /// `retry_after_s`) its type carries.
    pub fn with_details(
        kind: ErrorType,
        message: impl Into<String>,
        details: JsonMap,
    ) -> JeffError {
        JeffError {
            kind,
            payload: Box::new(JeffErrorPayload {
                message: message.into(),
                details,
                request_id: None,
                notes: Vec::new(),
                source: None,
            }),
        }
    }

    /// The same error, recording the error that caused it (shown by `std::error::Error::source`).
    pub fn with_source(mut self, source: BoxError) -> JeffError {
        self.payload.source = Some(Arc::from(source));
        self
    }

    /// The error's type.
    pub fn kind(&self) -> ErrorType {
        self.kind
    }

    /// The name of the error's type, as contract/errors.yaml and the wire write it (`NotInstalled`).
    pub fn type_name(&self) -> &'static str {
        self.kind.name()
    }

    /// The message, word for word the Python SDK's for the same failure.
    pub fn message(&self) -> &str {
        &self.payload.message
    }

    /// The structured details (empty when the type carries none); the typed getters below read them.
    pub fn details(&self) -> &JsonMap {
        &self.payload.details
    }

    /// The x-request-id of the Jeff server answer that carried this error, when there was one.
    pub fn request_id(&self) -> Option<&str> {
        self.payload.request_id.as_deref()
    }

    /// Further failures noticed while handling this one (Python's `add_note`); not part of the message.
    pub fn notes(&self) -> &[String] {
        &self.payload.notes
    }

    /// "Type: message", as the Python SDK writes an error inside another error's message.
    pub fn described(&self) -> String {
        format!("{}: {}", self.type_name(), self.payload.message)
    }

    pub(crate) fn set_request_id(&mut self, request_id: Option<String>) {
        self.payload.request_id = request_id;
    }

    pub(crate) fn add_note(&mut self, note: String) {
        self.payload.notes.push(note);
    }

    fn detail(&self, key: &str) -> &Json {
        self.payload.details.get(key).unwrap_or(&Json::Null)
    }

    fn missing(&self, key: &str, what: &str) -> JeffError {
        JeffError::new(
            ErrorType::ContractError,
            format!(
                "{}.details is missing its '{key}' {what} (got {})",
                self.type_name(),
                py_repr(self.detail(key))
            ),
        )
    }

    fn string_detail(&self, key: &str) -> Result<String> {
        self.detail(key)
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| self.missing(key, "string"))
    }

    /// NotInstalled, MissingContext, MissingLabels: the function the error is about.
    pub fn function_id(&self) -> Result<String> {
        self.string_detail("function_id")
    }

    /// MissingContext: the context fields that are missing.
    pub fn fields(&self) -> Result<Vec<String>> {
        let items = self
            .detail("fields")
            .as_array()
            .ok_or_else(|| self.missing("fields", "string array"))?;
        items
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| self.missing("fields", "string array"))
            })
            .collect()
    }

    /// Busy, RateLimited: how many seconds to wait before trying again.
    pub fn retry_after_s(&self) -> Result<f64> {
        self.detail("retry_after_s")
            .as_f64()
            .ok_or_else(|| self.missing("retry_after_s", "number"))
    }

    /// UsageLimitReached: the id on the hub of the account that has used its allowance.
    pub fn account(&self) -> Result<String> {
        self.string_detail("account")
    }

    /// UsageLimitReached: the input tokens the account has used in the current billing period.
    pub fn used(&self) -> Result<i64> {
        self.detail("used")
            .as_i64()
            .ok_or_else(|| self.missing("used", "whole number"))
    }

    /// UsageLimitReached: the input tokens the account may use in a billing period.
    pub fn allowance(&self) -> Result<i64> {
        self.detail("allowance")
            .as_i64()
            .ok_or_else(|| self.missing("allowance", "whole number"))
    }

    /// UsageLimitReached: when the current billing period ends and calls resume (ISO 8601, UTC).
    pub fn period_end(&self) -> Result<String> {
        self.string_detail("period_end")
    }

    /// EscalationFailed: the step that failed.
    pub fn step(&self) -> Result<String> {
        self.string_detail("step")
    }

    /// ProprietaryWeights: the adapter whose weights are proprietary.
    pub fn adapter_id(&self) -> Result<String> {
        self.string_detail("adapter_id")
    }
}

impl fmt::Display for JeffError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.payload.message)
    }
}

impl fmt::Debug for JeffError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("JeffError")
            .field("type", &self.kind)
            .field("message", &self.payload.message)
            .field("details", &self.payload.details)
            .field("request_id", &self.payload.request_id)
            .field("notes", &self.payload.notes)
            .finish()
    }
}

impl std::error::Error for JeffError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.payload
            .source
            .as_deref()
            .map(|source| source as &(dyn std::error::Error + 'static))
    }
}

fn details_of(pairs: &[(&str, Json)]) -> JsonMap {
    pairs
        .iter()
        .map(|(key, value)| ((*key).to_owned(), value.clone()))
        .collect()
}

/// NotInstalled for `function_id`, naming the adapter to pull; `why` (empty for none) says why it was needed.
pub(crate) fn not_installed(function_id: &str, why: &str) -> JeffError {
    let dots = function_id.matches('.').count();
    let adapter = if dots >= 3 {
        &function_id[..function_id.rfind('.').expect("dots were counted")]
    } else {
        function_id
    };
    let reason = if why.is_empty() {
        String::new()
    } else {
        format!(": {why}")
    };
    JeffError::with_details(
        ErrorType::NotInstalled,
        format!("{function_id} is not installed{reason}. Install it with: jeff pull {adapter}"),
        details_of(&[("function_id", Json::from(function_id))]),
    )
}

pub(crate) fn missing_context(function_id: &str, fields: &[String]) -> JeffError {
    JeffError::with_details(
        ErrorType::MissingContext,
        format!(
            "{function_id} needs context field(s) {}; pass them in context=",
            fields.join(", ")
        ),
        details_of(&[
            ("function_id", Json::from(function_id)),
            ("fields", Json::from(fields.to_vec())),
        ]),
    )
}

pub(crate) fn missing_labels(function_id: &str) -> JeffError {
    JeffError::with_details(
        ErrorType::MissingLabels,
        format!("{function_id} takes its labels from the caller; pass labels="),
        details_of(&[("function_id", Json::from(function_id))]),
    )
}

pub(crate) fn escalation_failed(step: &str, reason: &str) -> JeffError {
    JeffError::with_details(
        ErrorType::EscalationFailed,
        format!("Escalation step {step} failed: {reason}"),
        details_of(&[("step", Json::from(step))]),
    )
}

/// Whether Python reads the value as false (`not value`): null, false, a zero, "", [] or {}.
fn python_falsy(value: &Json) -> bool {
    match value {
        Json::Null => true,
        Json::Bool(flag) => !flag,
        Json::Number(number) => number.as_f64() == Some(0.0),
        Json::String(text) => text.is_empty(),
        Json::Array(items) => items.is_empty(),
        Json::Object(map) => map.is_empty(),
    }
}

/// The error a wire body names. An unknown type is an EngineError naming it. Details that are absent or that Python
/// reads as false (null, false, 0, "", []) are none, as the Python SDK's `dict(body.get("details") or {})` reads them;
/// any other details that are not an object break the contract (details is an object) and are a ContractError naming
/// them, where Python crashes or misreads them.
pub fn error_from_wire(type_name: &str, message: &str, details: Option<&Json>) -> JeffError {
    let Some(kind) = ErrorType::from_name(type_name) else {
        return JeffError::new(
            ErrorType::EngineError,
            format!(
                "unknown error type {} from the engine: {message}",
                crate::pyformat::py_str(type_name)
            ),
        );
    };
    let details = match details {
        Some(Json::Object(map)) => map.clone(),
        None => JsonMap::new(),
        Some(falsy) if python_falsy(falsy) => JsonMap::new(),
        Some(other) => {
            return JeffError::new(
                ErrorType::ContractError,
                format!(
                    "The {type_name} error received has 'details' that are not an object: {}; an error's details \
                     must be an object (its message was: {message})",
                    crate::pyformat::py_repr(other)
                ),
            );
        }
    };
    JeffError::with_details(kind, message, details)
}

/// `{"type", "message", "details"}`, as the wire and the fixtures write an error.
pub fn error_to_wire(error: &JeffError) -> JsonMap {
    details_of(&[
        ("type", Json::from(error.type_name())),
        ("message", Json::from(error.message())),
        ("details", Json::Object(error.details().clone())),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn every_type_has_its_contract_name() {
        assert_eq!(ErrorType::ALL.len(), 35);
        for kind in ErrorType::ALL {
            assert_eq!(ErrorType::from_name(kind.name()), Some(kind));
            assert_eq!(format!("{kind:?}"), kind.name());
        }
        assert_eq!(ErrorType::from_name("Weird"), None);
    }

    #[test]
    fn the_typed_constructors_write_the_python_messages() {
        let error = not_installed("jeff.support.triage.urgency", "it is an escalation step");
        assert_eq!(
            error.message(),
            "jeff.support.triage.urgency is not installed: it is an escalation step. Install it with: jeff pull jeff.support.triage"
        );
        assert_eq!(
            error.details(),
            json!({"function_id": "jeff.support.triage.urgency"})
                .as_object()
                .unwrap()
        );
        assert_eq!(
            not_installed("jeff.core.router", "").message(),
            "jeff.core.router is not installed. Install it with: jeff pull jeff.core.router"
        );
        let missing = missing_context("spam.x", &["channel".into(), "region".into()]);
        assert_eq!(missing.kind(), ErrorType::MissingContext);
        assert_eq!(
            missing.message(),
            "spam.x needs context field(s) channel, region; pass them in context="
        );
        assert_eq!(missing.fields().unwrap(), ["channel", "region"]);
        assert_eq!(
            missing_labels("f").message(),
            "f takes its labels from the caller; pass labels="
        );
        assert_eq!(
            escalation_failed("llm", "answered 'x'").message(),
            "Escalation step llm failed: answered 'x'"
        );
    }

    #[test]
    fn an_error_on_the_wire_becomes_the_same_type_with_the_same_message_and_details() {
        let error = error_from_wire("Busy", "full", Some(&json!({"retry_after_s": 2.5})));
        assert_eq!(error.kind(), ErrorType::Busy);
        assert_eq!(error.retry_after_s().unwrap(), 2.5);
        assert_eq!(
            Json::Object(error_to_wire(&error)),
            json!({"type": "Busy", "message": "full", "details": {"retry_after_s": 2.5}})
        );
        let unknown = error_from_wire("Weird", "Something odd", None);
        assert_eq!(unknown.kind(), ErrorType::EngineError);
        assert_eq!(
            unknown.message(),
            "unknown error type 'Weird' from the engine: Something odd"
        );
        assert!(unknown.details().is_empty());
    }

    #[test]
    fn details_python_reads_as_false_are_none_and_other_details_that_are_not_an_object_are_refused()
    {
        for falsy in [
            json!(null),
            json!(false),
            json!(0),
            json!(0.0),
            json!(""),
            json!([]),
        ] {
            let error = error_from_wire("InvalidRequest", "bad", Some(&falsy));
            assert_eq!(
                (error.kind(), error.message()),
                (ErrorType::InvalidRequest, "bad"),
                "{falsy}"
            );
            assert!(error.details().is_empty());
        }
        for (other, shown) in [
            (json!("garbage"), "'garbage'"),
            (json!(5), "5"),
            (json!(true), "True"),
            (json!([["decision", 1]]), "[['decision', 1]]"),
        ] {
            let error = error_from_wire("LowConfidence", "abstained", Some(&other));
            assert_eq!(error.kind(), ErrorType::ContractError);
            assert_eq!(
                error.message(),
                format!(
                    "The LowConfidence error received has 'details' that are not an object: {shown}; an error's \
                     details must be an object (its message was: abstained)"
                )
            );
        }
    }

    #[test]
    fn a_typed_getter_names_the_missing_field_instead_of_inventing_a_value() {
        let broken = JeffError::new(ErrorType::NotInstalled, "boom");
        let problem = broken.function_id().unwrap_err();
        assert_eq!(problem.kind(), ErrorType::ContractError);
        assert_eq!(
            problem.message(),
            "NotInstalled.details is missing its 'function_id' string (got None)"
        );
        let wrong = JeffError::with_details(
            ErrorType::Busy,
            "b",
            json!({"retry_after_s": "soon"})
                .as_object()
                .unwrap()
                .clone(),
        );
        assert_eq!(
            wrong.retry_after_s().unwrap_err().message(),
            "Busy.details is missing its 'retry_after_s' number (got 'soon')"
        );
    }

    #[test]
    fn usage_limit_reached_reads_its_four_details() {
        let error = error_from_wire(
            "UsageLimitReached",
            "This account has used its 50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; calls resume then",
            Some(
                &json!({"account": "u_1", "used": 50_000_123, "allowance": 50_000_000, "period_end": "2026-12-03T10:00:00Z"}),
            ),
        );
        assert_eq!(error.kind(), ErrorType::UsageLimitReached);
        assert_eq!(
            (
                error.account().unwrap(),
                error.used().unwrap(),
                error.allowance().unwrap(),
                error.period_end().unwrap()
            ),
            (
                "u_1".to_owned(),
                50_000_123,
                50_000_000,
                "2026-12-03T10:00:00Z".to_owned()
            )
        );
        let broken = JeffError::new(ErrorType::UsageLimitReached, "m");
        assert_eq!(
            broken.period_end().unwrap_err().message(),
            "UsageLimitReached.details is missing its 'period_end' string (got None)"
        );
    }

    #[test]
    fn a_clone_carries_the_notes_and_request_id_and_display_is_the_message() {
        let mut original = not_installed("jeff.core.router", "");
        original.set_request_id(Some("req-1".into()));
        original.add_note("also tried the other router".into());
        let mut copy = original.clone();
        assert_eq!(copy.request_id(), Some("req-1"));
        assert_eq!(copy.notes(), ["also tried the other router"]);
        copy.add_note("only on the copy".into());
        assert_eq!(original.notes().len(), 1);
        assert_eq!(original.to_string(), original.message());
        assert_eq!(
            original.described(),
            format!("NotInstalled: {}", original.message())
        );
    }

    #[test]
    fn a_source_is_kept_for_the_error_chain() {
        let io = std::io::Error::other("disk gone");
        let error = JeffError::new(ErrorType::EngineError, "failed").with_source(Box::new(io));
        assert_eq!(
            std::error::Error::source(&error).unwrap().to_string(),
            "disk gone"
        );
    }
}
