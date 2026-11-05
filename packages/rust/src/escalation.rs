//! What happens when a decision abstains: return it, raise, or escalate through explicit steps (port of
//! jeff/escalation.py). An escalated decision says who answered and lists every earlier attempt; a failing step
//! raises EscalationFailed.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;
use indexmap::IndexMap;

use crate::binding::{Call, RowState, build_row, option_keys};
use crate::client::Jeff;
use crate::contract::QuestionType;
use crate::decision::{Decision, EscalationRecord, Value, low_confidence};
use crate::errors::{BoxError, ErrorType, JeffError, Result, escalation_failed, not_installed};
use crate::json::{Json, JsonMap};
use crate::pyformat::{py_fixed, py_float, py_repr, py_str};

/// A boxed future that can be sent between threads.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;
/// An llm or human step: your own function, shown a Question, answering with one of its option keys.
pub type Ask = Arc<
    dyn Fn(Question) -> BoxFuture<'static, std::result::Result<String, BoxError>> + Send + Sync,
>;

/// Something that decides calls: another Jeff client, for a `remote` step.
#[async_trait]
pub trait Decider: Send + Sync {
    /// The decisions for `calls`, in order.
    async fn decide(&self, calls: &[Call]) -> Result<Vec<Decision>>;
}

/// One step of an escalation, tried in turn while the decision is unsure.
#[derive(Clone)]
pub enum Step {
    /// Another function implementing the same interface.
    Function(String),
    /// Another client (a Jeff server, say), asked the same call.
    Remote(Arc<dyn Decider>),
    /// Your own function asking a language model.
    Llm(Ask),
    /// Your own function asking a person.
    Human(Ask),
}

impl Step {
    /// The step's name, as decisions and error messages write it: `function`, `remote`, `llm` or `human`.
    pub fn name(&self) -> &'static str {
        match self {
            Step::Function(_) => "function",
            Step::Remote(_) => "remote",
            Step::Llm(_) => "llm",
            Step::Human(_) => "human",
        }
    }
}

impl std::fmt::Debug for Step {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Step::Function(reference) => write!(formatter, "function_step({})", py_str(reference)),
            other => write!(formatter, "{}(...)", other.name()),
        }
    }
}

/// An escalation policy: its steps and its time budget.
#[derive(Clone, Debug)]
pub struct Escalate {
    /// The steps, tried in order until one answers with confidence.
    pub steps: Vec<Step>,
    /// The longest the whole escalation may take; None for no limit.
    pub budget_ms: Option<u64>,
}

/// What happens when a decision abstains.
#[derive(Clone, Debug, Default)]
pub enum Policy {
    /// Return the unsure decision (the default).
    #[default]
    Return,
    /// Raise LowConfidence carrying the decision.
    Error,
    /// Try the escalation's steps in turn.
    Escalate(Escalate),
}

impl Policy {
    /// "return" or "error"; any other name is refused, as the Python SDK refuses it.
    pub fn named(name: &str) -> Result<Policy> {
        match name {
            "return" => Ok(Policy::Return),
            "error" => Ok(Policy::Error),
            other => Err(JeffError::new(
                ErrorType::InvalidPolicy,
                format!(
                    "Unknown on_low_confidence policy {}; use 'return', 'error' or escalate(...)",
                    py_str(other)
                ),
            )),
        }
    }
}

/// The policy that tries `steps` in turn within `budget_ms` (no limit when None). Raises InvalidPolicy
/// when `steps` is empty.
pub fn escalate(steps: Vec<Step>, budget_ms: Option<u64>) -> Result<Policy> {
    if steps.is_empty() {
        return Err(JeffError::new(
            ErrorType::InvalidPolicy,
            "escalate() needs at least one step",
        ));
    }
    Ok(Policy::Escalate(Escalate { steps, budget_ms }))
}

/// A step that asks the function `reference`, which must implement the same interface.
pub fn function_step(reference: &str) -> Step {
    Step::Function(reference.to_owned())
}

/// A step that asks `client` (another Jeff, such as one on a Jeff server) the same call.
pub fn remote(client: Arc<dyn Decider>) -> Step {
    Step::Remote(client)
}

fn ask_of<F, Fut>(ask: F) -> Ask
where
    F: Fn(Question) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = std::result::Result<String, BoxError>> + Send + 'static,
{
    Arc::new(
        move |question| -> BoxFuture<'static, std::result::Result<String, BoxError>> {
            Box::pin(ask(question))
        },
    )
}

/// A step that shows your async function `ask` the [`Question`] for a language model to answer with one of
/// its option keys.
pub fn llm<F, Fut>(ask: F) -> Step
where
    F: Fn(Question) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = std::result::Result<String, BoxError>> + Send + 'static,
{
    Step::Llm(ask_of(ask))
}

/// A step that shows your async function `ask` the [`Question`] for a person to answer with one of its
/// option keys.
pub fn human<F, Fut>(ask: F) -> Step
where
    F: Fn(Question) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = std::result::Result<String, BoxError>> + Send + 'static,
{
    Step::Human(ask_of(ask))
}

/// The state an llm or human step is shown: the named fields the model read, in its order, or the plain input.
#[derive(Clone, Debug, PartialEq)]
pub enum QuestionState {
    /// The context and the input under their exact keys (an input of form fields).
    Fields(JsonMap),
    /// The plain input string (an input of form text).
    Text(String),
}

/// What an llm or human step is shown. `state` and `options` keep the order the model was trained on and reads them;
/// build any prompt from them in that order.
#[derive(Clone, Debug, PartialEq)]
pub struct Question {
    /// The full id of the function whose decision was unsure.
    pub function: String,
    /// The function's instructions, word for word.
    pub instructions: String,
    /// What the model read.
    pub state: QuestionState,
    /// Each key the step may answer with, and its description, in option order.
    pub options: IndexMap<String, Option<String>>,
}

impl Question {
    /// The state's fields in order; None when the state is a plain string.
    pub fn state_entries(&self) -> Option<Vec<(&str, &Json)>> {
        match &self.state {
            QuestionState::Fields(map) => Some(
                map.iter()
                    .map(|(key, value)| (key.as_str(), value))
                    .collect(),
            ),
            QuestionState::Text(_) => None,
        }
    }

    /// Each option key with its description (or none), in option order.
    pub fn option_entries(&self) -> Vec<(&str, Option<&str>)> {
        self.options
            .iter()
            .map(|(key, description)| (key.as_str(), description.as_deref()))
            .collect()
    }

    /// `{"function", "instructions", "state", "options"}`, as the fixtures record a question.
    pub fn to_json(&self) -> Json {
        let mut map = JsonMap::new();
        map.insert("function".into(), Json::from(self.function.as_str()));
        map.insert(
            "instructions".into(),
            Json::from(self.instructions.as_str()),
        );
        map.insert(
            "state".into(),
            match &self.state {
                QuestionState::Fields(fields) => Json::Object(fields.clone()),
                QuestionState::Text(text) => Json::from(text.as_str()),
            },
        );
        map.insert(
            "options".into(),
            Json::Object(
                self.options
                    .iter()
                    .map(|(key, description)| (key.clone(), Json::from(description.clone())))
                    .collect(),
            ),
        );
        Json::Object(map)
    }
}

fn interface_mismatch(step_id: &str, implements: &str, interface: &str) -> JeffError {
    JeffError::new(
        ErrorType::InterfaceMismatch,
        format!("Escalation step {step_id} implements {implements}, not {interface}"),
    )
}

/// Checks a policy before anything is decided: every function step is in the catalogue, installed, and implements
/// the interface the call is answered through.
pub(crate) async fn check_policy(jeff: &Jeff, interface: &str, policy: &Policy) -> Result<()> {
    let Policy::Escalate(escalation) = policy else {
        return Ok(());
    };
    for step in &escalation.steps {
        let Step::Function(reference) = step else {
            continue;
        };
        let spec = jeff.catalogue().function(reference).map_err(|error| {
            if error.kind() == ErrorType::UnknownFunction {
                JeffError::new(
                    ErrorType::InvalidPolicy,
                    format!(
                        "Escalation step function_step({}): {}",
                        py_str(reference),
                        error.message()
                    ),
                )
                .with_source(Box::new(error))
            } else {
                error
            }
        })?;
        if !jeff
            .engine()
            .installed(false)
            .await?
            .contains(&spec.adapter_id)
        {
            return Err(not_installed(&spec.id, "it is an escalation step"));
        }
        if spec.interface != interface {
            return Err(interface_mismatch(&spec.id, &spec.interface, interface));
        }
    }
    Ok(())
}

fn reason(decision: &Decision, threshold: Option<f64>) -> String {
    match (threshold, decision.confidence) {
        (Some(threshold), Some(confidence)) if confidence < threshold => format!(
            "abstained (confidence {} < threshold {})",
            py_fixed(confidence, 3),
            py_float(threshold)
        ),
        _ => "abstained (abstain label)".into(),
    }
}

fn record(jeff: &Jeff, decision: &Decision) -> Result<EscalationRecord> {
    let calibration = jeff.calibration_of(&*jeff.catalogue().function(&decision.function)?)?;
    Ok(EscalationRecord {
        decided_by: decision.decided_by.clone(),
        value: decision.value.clone(),
        confidence: decision.confidence,
        reason: reason(
            decision,
            calibration.map(|calibration| calibration.threshold),
        ),
    })
}

/// The question an llm or human step is shown, and its option keys in option order.
async fn question_of(jeff: &Jeff, call: &Call) -> Result<(Question, Vec<String>)> {
    let binding = jeff.bind_call(call).await?;
    let function = &binding.function;
    let row = build_row(&binding, &call.input)?;
    let mut options = IndexMap::new();
    match function.question_type {
        QuestionType::Choice => {
            let criteria = binding
                .criteria
                .as_ref()
                .expect("a choice function's binding always has criteria");
            for key in option_keys(&binding) {
                options.insert(binding.wire_to_caller[&key].clone(), criteria[&key].clone());
            }
        }
        QuestionType::Score => {
            for (index, level) in function.labels.levels.iter().enumerate() {
                options.insert(index.to_string(), Some(level.clone()));
            }
        }
        QuestionType::Noul => {
            // The wording the engine shows the model when a noul function does not describe its answers
            // (jeff_runtime.engine.model.options): what the model was shown, not a substitute for a missing value.
            let described = &function.labels.fixed;
            for (key, shown) in [("true", "Yes / true"), ("false", "No / false")] {
                let description = match described.get(key) {
                    Some(description) => description.clone(),
                    None => Some(shown.to_owned()),
                };
                options.insert(key.to_owned(), description);
            }
        }
    }
    let state = match row.state {
        RowState::Fields(map) => QuestionState::Fields(map),
        RowState::Text(text) => QuestionState::Text(text),
    };
    let keys = options.keys().cloned().collect();
    Ok((
        Question {
            function: function.id.clone(),
            instructions: function.instructions.clone(),
            state,
            options,
        },
        keys,
    ))
}

fn converted(question_type: QuestionType, answer: &str) -> Value {
    match question_type {
        QuestionType::Noul => Value::Bool(answer == "true"),
        QuestionType::Score => {
            Value::Int(answer.parse().expect("a score option key is a level index"))
        }
        QuestionType::Choice => Value::Str(answer.to_owned()),
    }
}

/// How a step failed: with a Jeff error (whose type names the failure), or with the caller's own error.
enum StepFailure {
    Jeff(JeffError),
    Caller(BoxError),
}

impl From<JeffError> for StepFailure {
    fn from(error: JeffError) -> StepFailure {
        StepFailure::Jeff(error)
    }
}

/// The one decision a decider answered for one call; a decider answering none is a contract breach.
fn the_one(decisions: Vec<Decision>, asked: &str) -> Result<Decision> {
    let answered = decisions.len();
    decisions.into_iter().next().ok_or_else(|| {
        JeffError::new(
            ErrorType::ContractError,
            format!("{asked} answered {answered} decisions for 1 call"),
        )
    })
}

async fn run_step(
    jeff: &Jeff,
    call: &Call,
    original: &Decision,
    step: &Step,
) -> std::result::Result<Decision, StepFailure> {
    match step {
        Step::Function(reference) => {
            let spec = jeff.catalogue().function(reference)?;
            if spec.interface != original.interface {
                return Err(
                    interface_mismatch(&spec.id, &spec.interface, &original.interface).into(),
                );
            }
            let asked = Call {
                function: reference.clone(),
                ..call.clone()
            };
            Ok(the_one(jeff.decide(&[asked]).await?, "Jeff")?)
        }
        Step::Remote(client) => Ok(the_one(
            client.decide(std::slice::from_ref(call)).await?,
            "The remote step's client",
        )?),
        Step::Llm(ask) | Step::Human(ask) => {
            let (question, keys) = question_of(jeff, call).await?;
            let started = Instant::now();
            let answer =
                ask(question)
                    .await
                    .map_err(|error| match error.downcast::<JeffError>() {
                        Ok(jeff_error) => StepFailure::Jeff(*jeff_error),
                        Err(other) => StepFailure::Caller(other),
                    })?;
            let elapsed = (started.elapsed().as_secs_f64() * 1000.0 * 1000.0).round() / 1000.0;
            if !keys.contains(&answer) {
                return Err(escalation_failed(
                    step.name(),
                    &format!(
                        "answered {}, which is not one of {}",
                        py_str(&answer),
                        py_repr(&Json::from(keys))
                    ),
                )
                .into());
            }
            let function = jeff.catalogue().function(&call.function)?;
            let mut timing = JsonMap::new();
            timing.insert("path".into(), Json::from(step.name()));
            timing.insert("ms".into(), Json::from(elapsed));
            Ok(Decision {
                value: converted(function.question_type, &answer),
                confidence: None,
                abstained: false,
                calibrated: false,
                probabilities: IndexMap::new(),
                score: None,
                decided_by: step.name().to_owned(),
                escalations: Vec::new(),
                timing,
                ..original.clone()
            })
        }
    }
}

pub(crate) async fn apply_policy(
    jeff: &Jeff,
    call: &Call,
    decision: Decision,
    policy: &Policy,
) -> Result<Decision> {
    if !decision.abstained {
        return Ok(decision);
    }
    let escalation = match policy {
        Policy::Return => return Ok(decision),
        Policy::Error => return Err(low_confidence(&decision)?),
        Policy::Escalate(escalation) => escalation,
    };
    let started = Instant::now();
    let mut attempts = vec![record(jeff, &decision)?];
    let mut latest = decision.clone();
    for step in &escalation.steps {
        latest = match run_step(jeff, call, &decision, step).await {
            Ok(answered) => answered,
            Err(StepFailure::Jeff(error)) if error.kind() == ErrorType::EscalationFailed => {
                return Err(error);
            }
            Err(StepFailure::Jeff(error)) => {
                let reason = error.described();
                return Err(escalation_failed(step.name(), &reason).with_source(Box::new(error)));
            }
            Err(StepFailure::Caller(error)) => {
                let reason = format!("Error: {error}");
                return Err(escalation_failed(step.name(), &reason).with_source(error));
            }
        };
        if let Some(budget) = escalation.budget_ms
            && started.elapsed().as_secs_f64() * 1000.0 > budget as f64
        {
            return Err(JeffError::new(
                ErrorType::EscalationBudgetExceeded,
                format!(
                    "Escalation of {} took longer than {budget} ms",
                    decision.function
                ),
            ));
        }
        if !latest.abstained {
            return Ok(Decision {
                route: decision.route.clone(),
                escalations: attempts,
                ..latest
            });
        }
        attempts.push(record(jeff, &latest)?);
    }
    // every step abstained: the last attempt is returned, every earlier one recorded
    attempts.pop();
    Ok(Decision {
        route: decision.route.clone(),
        escalations: attempts,
        ..latest
    })
}
