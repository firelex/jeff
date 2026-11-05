//! Remote mode: Jeff's surface over a Jeff HTTP server (port of jeff/remote.py).
//!
//! A remote Jeff keeps a local catalogue, so binding, labels, policies and escalation questions are worked out exactly
//! as in local mode, and replaces every place where Jeff would call an engine with one request to the server.
//! Connecting reads GET /v1/functions once and refuses a server that serves another version of an adapter the local
//! catalogue has (labels would otherwise be mapped by the wrong manifest). A policy the server can run ("return",
//! "error", or escalation through function steps only) is sent with the request; one with an llm, human or remote step
//! is run here, on the decision the server returns.

use std::collections::{BTreeSet, HashMap};
use std::future::Future;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use indexmap::IndexMap;

use crate::binding::{Binding, Call, Row};
use crate::catalogue::Catalogue;
use crate::client::{Backend, DispatchOptions, Jeff, ShorthandOptions};
use crate::content_coding::{ACCEPT_ENCODING, MAX_BODY, Undecoded, decoded};
use crate::contract::{SchemaName, first_problem, is_valid};
use crate::decision::{Decision, low_confidence};
use crate::engine::{BaseFormat, Engine, EngineResult};
use crate::errors::{BoxError, ErrorType, JeffError, Result, error_from_wire, not_installed};
use crate::escalation::{BoxFuture, Policy, Step, check_policy};
use crate::graph::{
    CodeFunction, GraphResult, GraphSource, check_graph, graph_to_json, run_checked,
};
use crate::http::{HttpAnswer, HttpClient, NoAnswer};
use crate::json::{Json, JsonMap};
use crate::manifest::{Calibration, FunctionSpec, calibration_from_wire, resolve_calibration};
use crate::pyformat::{py_repr, py_sorted};
use crate::routing::{DEFAULT_ROUTER, leaf_call, start_router};

/// An async function returning the current bearer token, or an error that is reported as Unauthorised.
pub type TokenFunction =
    Arc<dyn Fn() -> BoxFuture<'static, std::result::Result<String, BoxError>> + Send + Sync>;

/// The bearer token: a string, a function returning the current token (called before every HTTP request, so it can
/// refresh), or None for a server with no credentials configured. Its Debug form never shows a token.
#[derive(Clone)]
pub enum TokenSource {
    /// No token: only for a server with no credentials configured.
    None,
    /// One token for every request.
    Fixed(String),
    /// A function asked for the current token before every HTTP request (on the channel, at connect and before
    /// the token expires).
    Function(TokenFunction),
}

impl TokenSource {
    /// A fixed token.
    pub fn fixed(token: impl Into<String>) -> TokenSource {
        TokenSource::Fixed(token.into())
    }

    /// A token read from the async function `function` each time one is needed.
    pub fn function<F, Fut>(function: F) -> TokenSource
    where
        F: Fn() -> Fut + Send + Sync + 'static,
        Fut: Future<Output = std::result::Result<String, BoxError>> + Send + 'static,
    {
        TokenSource::Function(Arc::new(
            move || -> BoxFuture<'static, std::result::Result<String, BoxError>> {
                Box::pin(function())
            },
        ))
    }
}

impl std::fmt::Debug for TokenSource {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            TokenSource::None => "TokenSource::None",
            TokenSource::Fixed(_) => "TokenSource::Fixed(<token not shown>)",
            TokenSource::Function(_) => "TokenSource::Function(<token function>)",
        })
    }
}

fn unauthorised(message: String) -> JeffError {
    JeffError::new(ErrorType::Unauthorised, message)
}

/// The current token: the string, or what the function returns now (called once per use); None when there is no
/// token. Unauthorised when it is not a non-empty string an HTTP header can carry. No message shows the token.
pub(crate) async fn read_token(source: &TokenSource, url: &str) -> Result<Option<String>> {
    let (token, what) = match source {
        TokenSource::None => return Ok(None),
        TokenSource::Fixed(token) => (token.clone(), "got"),
        TokenSource::Function(function) => {
            let token = function().await.map_err(|error| {
                unauthorised(format!("The token function for {url} failed: {error}"))
                    .with_source(error)
            })?;
            (token, "the token function returned")
        }
    };
    if token.is_empty() {
        return Err(unauthorised(format!(
            "The token for {url} must be a non-empty string; {what} an empty string"
        )));
    }
    if !token.bytes().all(|byte| (0x20..=0x7e).contains(&byte)) {
        return Err(unauthorised(format!(
            "The token for {url} must be printable ASCII, as an HTTP header carries it; {what} a string with other characters (not shown)"
        )));
    }
    Ok(Some(token))
}

/// The wire form of a policy the server runs (contract/schemas/decide_request.schema.json, $defs/policy).
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum WirePolicy {
    Return,
    Error,
    Escalate {
        refs: Vec<String>,
        budget_ms: Option<u64>,
    },
}

impl WirePolicy {
    pub fn to_json(&self) -> Json {
        match self {
            WirePolicy::Return => Json::from("return"),
            WirePolicy::Error => Json::from("error"),
            WirePolicy::Escalate { refs, budget_ms } => {
                let mut map = JsonMap::new();
                map.insert("escalate".into(), Json::from(refs.clone()));
                if let Some(budget) = budget_ms {
                    map.insert("budget_ms".into(), Json::from(*budget));
                }
                Json::Object(map)
            }
        }
    }
}

/// The wire form of a policy the server can run; None when the policy has an llm, human or remote step, which only
/// this process can run.
pub(crate) fn server_policy(policy: &Policy) -> Option<WirePolicy> {
    match policy {
        Policy::Return => Some(WirePolicy::Return),
        Policy::Error => Some(WirePolicy::Error),
        Policy::Escalate(escalation) => {
            let mut refs = Vec::new();
            for step in &escalation.steps {
                match step {
                    Step::Function(reference) => refs.push(reference.clone()),
                    Step::Remote(_) | Step::Llm(_) | Step::Human(_) => return None,
                }
            }
            Some(WirePolicy::Escalate {
                refs,
                budget_ms: escalation.budget_ms,
            })
        }
    }
}

/// The most calls one request carries (contract/schemas/decide_request); more are refused, never split.
pub const MAX_CALLS: usize = 256;
/// Characters of a non-Jeff error body quoted in the EngineError.
const BODY_EXCERPT: usize = 300;

/// A ContractError for an answer whose shape is not the contract's: for a Transport that does not check the answer
/// against its schema, as RestTransport does.
fn listing_error(url: &str, request: &str, what: &str) -> JeffError {
    JeffError::new(
        ErrorType::ContractError,
        format!("The answer of {url} to {request} has {what}"),
    )
}

fn check_count(url: &str, decisions: usize, calls: usize) -> Result<()> {
    if decisions != calls {
        return Err(JeffError::new(
            ErrorType::ContractError,
            format!("{url} answered {decisions} decisions for {calls} calls"),
        ));
    }
    Ok(())
}

pub(crate) fn check_batch(count: usize, what: &str) -> Result<()> {
    if count > MAX_CALLS {
        return Err(JeffError::new(
            ErrorType::InvalidRequest,
            format!(
                "{count} {what} in one request: a Jeff server answers at most {MAX_CALLS} per request; send them in batches of at most {MAX_CALLS}"
            ),
        ));
    }
    Ok(())
}

/// The error for a server serving another version of an adapter than the local catalogue has: labels would be mapped
/// by the wrong manifest.
pub(crate) fn version_mismatch(adapter_id: &str, served: &str, local: &str) -> JeffError {
    JeffError::new(
        ErrorType::ContractError,
        format!(
            "{adapter_id}: the server serves version {served}, this client's catalogue has {local}; pass catalogue= with the server's manifests"
        ),
    )
}

/// How a remote Jeff reaches the server: one round trip per method. Bodies and answers are the JSON shapes of
/// contract/schemas/decide_request and decide_response.
#[async_trait]
pub trait Transport: Send + Sync {
    /// The decisions for `calls`, in order, with `policy` applied by the server.
    async fn decide_calls(&self, calls: Vec<Json>, policy: Json) -> Result<Vec<Json>>;
    /// The routed decision for a {dispatch, on_low_confidence} body.
    async fn dispatch(&self, body: JsonMap) -> Result<Json>;
    /// The graph result for a {graph, input, params, ...} body.
    async fn graph(&self, body: JsonMap) -> Result<Json>;
    /// The server's GET /v1/functions answer (contract/schemas/functions_response).
    async fn functions(&self) -> Result<JsonMap>;
    /// Ends the transport's connections; later requests fail with InvalidRequest.
    async fn close(&self);
}

fn excerpt(body: &[u8]) -> String {
    String::from_utf8_lossy(body)
        .chars()
        .take(BODY_EXCERPT)
        .collect()
}

/// The Transport over HTTP. Every non-2xx answer is the JeffError its body names, with the answer's x-request-id.
pub struct RestTransport {
    url: String,
    token: TokenSource,
    timeout_ms: u64,
    client: HttpClient,
}

impl std::fmt::Debug for RestTransport {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RestTransport")
            .field("url", &self.url)
            .field("token", &self.token)
            .finish_non_exhaustive()
    }
}

impl RestTransport {
    /// A transport to the server at `url` with `token`, each phase of a request bounded by `timeout_ms`. Raises
    /// InvalidRequest for a URL that does not start with http:// or https:// and for a timeout of 0 or beyond
    /// [`MAX_TIMEOUT_MS`](crate::client::MAX_TIMEOUT_MS).
    pub fn new(url: &str, token: TokenSource, timeout_ms: u64) -> Result<RestTransport> {
        crate::client::check_timeout_ms(timeout_ms)?;
        let client = HttpClient::new(url, timeout_ms)?;
        Ok(RestTransport {
            url: client.endpoint().url.clone(),
            token,
            timeout_ms,
            client,
        })
    }

    /// The server URL (without trailing slashes), as messages name it.
    pub fn url(&self) -> &str {
        &self.url
    }

    fn no_answer(&self, failure: NoAnswer, method: &str, path: &str) -> JeffError {
        let url = &self.url;
        match failure {
            NoAnswer::Closed => JeffError::new(
                ErrorType::InvalidRequest,
                format!("This remote client of {url} is closed; connect again with Jeff.remote"),
            ),
            NoAnswer::ConnectTimeout => JeffError::new(
                ErrorType::BackendUnavailable,
                format!(
                    "Cannot reach the Jeff server at {url}: no connection within {} ms",
                    self.timeout_ms
                ),
            ),
            NoAnswer::Connect(reason) => JeffError::new(
                ErrorType::BackendUnavailable,
                format!("Cannot reach the Jeff server at {url}: {reason}"),
            ),
            NoAnswer::Deadline => JeffError::new(
                ErrorType::DeadlineExceeded,
                format!(
                    "The Jeff server at {url} did not answer {method} {path} within {} ms; the server may still have run the request",
                    self.timeout_ms
                ),
            ),
            NoAnswer::Broken(reason) => JeffError::new(
                ErrorType::BackendUnavailable,
                format!(
                    "The connection to the Jeff server at {url} failed during {method} {path}: {reason}"
                ),
            ),
            NoAnswer::TooLarge => self.too_large(method, path, false),
        }
    }

    /// An answer body past content_coding::MAX_BODY, as sent or once a Content-Encoding layer is undone (`decoded`):
    /// the client is closed (a server that sends one is not trusted with later requests, as the channel ends on a
    /// message past its limit), and the error names the limit.
    fn too_large(&self, method: &str, path: &str, decoded: bool) -> JeffError {
        self.client.close();
        let what = if decoded {
            "a body that decodes (Content-Encoding) to more than"
        } else {
            "a body of more than"
        };
        JeffError::new(
            ErrorType::BackendUnavailable,
            format!(
                "The Jeff server at {} answered {method} {path} with {what} {} bytes, the most this client reads; the \
                 client is closed, connect again with Jeff.remote",
                self.url, MAX_BODY
            ),
        )
    }

    /// One request; the answer's JSON object, checked against `schema`. Every error raised after the server answered
    /// carries the answer's x-request-id.
    async fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&Json>,
        schema: SchemaName,
    ) -> Result<JsonMap> {
        if self.client.is_closed() {
            return Err(self.no_answer(NoAnswer::Closed, method, path));
        }
        let mut headers: Vec<(&str, String)> = vec![("accept-encoding", ACCEPT_ENCODING.into())];
        if let Some(token) = read_token(&self.token, &self.url).await? {
            headers.push(("authorization", format!("Bearer {token}")));
        }
        if body.is_some() {
            headers.push(("content-type", "application/json".into()));
        }
        let payload = body.map(|body| serde_json::to_vec(body).expect("JSON values serialise"));
        let mut answer = self
            .client
            .request(method, path, &headers, payload)
            .await
            .map_err(|failure| self.no_answer(failure, method, path))?;
        answer.body = match decoded(
            std::mem::take(&mut answer.body),
            &answer.content_encodings,
            MAX_BODY,
        ) {
            Ok(body) => body,
            Err(Undecoded::TooLarge) => return Err(self.too_large(method, path, true)),
            // httpx decodes while reading, so this is jeff/remote.py's RequestError branch, before any answer
            Err(Undecoded::Broken(reason)) => {
                return Err(JeffError::new(
                    ErrorType::EngineError,
                    format!(
                        "The answer of the Jeff server at {} to {method} {path} could not be read: DecodingError: \
                         {reason}",
                        self.url
                    ),
                ));
            }
        };
        self.answer(method, path, &answer, schema)
            .map_err(|mut error| {
                error.set_request_id(answer.request_id.clone());
                error
            })
    }

    fn answer(
        &self,
        method: &str,
        path: &str,
        answer: &HttpAnswer,
        schema: SchemaName,
    ) -> Result<JsonMap> {
        let url = &self.url;
        let status = answer.status;
        if !(200..=299).contains(&status) {
            return Err(self.error(method, path, answer)?);
        }
        let parsed: Json = serde_json::from_slice(&answer.body).map_err(|_| JeffError::new(ErrorType::EngineError, format!(
            "The Jeff server at {url} answered {method} {path} with status {status} and a body that is not JSON: {}", excerpt(&answer.body)
        )))?;
        let Json::Object(map) = &parsed else {
            return Err(JeffError::new(
                ErrorType::EngineError,
                format!(
                    "The Jeff server at {url} answered {method} {path} with JSON that is not an object: {}",
                    excerpt(&answer.body)
                ),
            ));
        };
        if !is_valid(schema, &parsed, None)? {
            return Err(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "The answer of {url} to {method} {path} does not match contract/schemas/{}: {}",
                    schema.stem(),
                    first_problem(schema, &parsed, None)?
                ),
            ));
        }
        Ok(map.clone())
    }

    /// The JeffError a non-2xx answer names; a decision it carries (LowConfidence) is checked against the decision
    /// schema first, as the Python SDK builds it at once.
    fn error(&self, method: &str, path: &str, answer: &HttpAnswer) -> Result<JeffError> {
        let url = &self.url;
        let body: Option<Json> = serde_json::from_slice(&answer.body).ok();
        let wire = body
            .as_ref()
            .and_then(|body| body.get("error"))
            .filter(|error| {
                error.get("type").is_some_and(Json::is_string)
                    && error.get("message").is_some_and(Json::is_string)
            });
        let Some(wire) = wire else {
            return Ok(JeffError::new(
                ErrorType::EngineError,
                format!(
                    "The Jeff server at {url} answered {method} {path} with status {} and a body that is not a Jeff error: {}",
                    answer.status,
                    excerpt(&answer.body)
                ),
            ));
        };
        let made = error_from_wire(
            wire["type"].as_str().expect("checked"),
            wire["message"].as_str().expect("checked"),
            wire.get("details"),
        );
        if let Some(decision) = made.details().get("decision")
            && !is_valid(SchemaName::Decision, decision, None)?
        {
            return Ok(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "The {} error the Jeff server at {url} answered {method} {path} with carries a decision that does not match contract/schemas/decision: {}",
                    made.type_name(),
                    first_problem(SchemaName::Decision, decision, None)?
                ),
            ));
        }
        Ok(made)
    }

    async fn decide_answer(&self, body: Json, key: &str) -> Result<Json> {
        let answer = self
            .request(
                "POST",
                "/v1/decide",
                Some(&body),
                SchemaName::DecideResponse,
            )
            .await?;
        answer.get(key).cloned().ok_or_else(|| {
            let keys = py_sorted(answer.keys().cloned());
            JeffError::new(
                ErrorType::ContractError,
                format!(
                    "The answer of {} to POST /v1/decide has {}, not {}",
                    self.url,
                    py_repr(&Json::from(keys)),
                    py_repr(&Json::from(key))
                ),
            )
        })
    }
}

#[async_trait]
impl Transport for RestTransport {
    async fn decide_calls(&self, calls: Vec<Json>, policy: Json) -> Result<Vec<Json>> {
        let count = calls.len();
        let mut body = JsonMap::new();
        body.insert("calls".into(), Json::Array(calls));
        body.insert("on_low_confidence".into(), policy);
        let decisions = self.decide_answer(Json::Object(body), "decisions").await?;
        let Json::Array(decisions) = decisions else {
            return Err(listing_error(
                &self.url,
                "POST /v1/decide",
                "a 'decisions' that is not a list",
            ));
        };
        check_count(&self.url, decisions.len(), count)?;
        Ok(decisions)
    }

    async fn dispatch(&self, body: JsonMap) -> Result<Json> {
        self.decide_answer(Json::Object(body), "decision").await
    }

    async fn graph(&self, body: JsonMap) -> Result<Json> {
        self.decide_answer(Json::Object(body), "graph_result").await
    }

    async fn functions(&self) -> Result<JsonMap> {
        self.request("GET", "/v1/functions", None, SchemaName::FunctionsResponse)
            .await
    }

    /// Closes the connections; a request still in flight fails with the closed error.
    async fn close(&self) {
        self.client.close();
    }
}

type ServedCalibrations = HashMap<String, (Option<Calibration>, IndexMap<String, Calibration>)>;

fn base_format_of(answer: &JsonMap, url: &str) -> Result<BaseFormat> {
    match answer.get("base_format") {
        Some(format) => BaseFormat::from_json(format),
        None => Err(listing_error(url, "GET /v1/functions", "no 'base_format'")),
    }
}

/// The engine a remote Jeff holds: the adapters the server serves and the base format its engines serve, read from
/// GET /v1/functions when it is built and again on installed(true). It never predicts.
pub struct ServedFunctions {
    transport: Arc<dyn Transport>,
    catalogue: Arc<Catalogue>,
    url: String,
    base_format: BaseFormat,
    listing: Mutex<(BTreeSet<String>, ServedCalibrations)>,
}

impl ServedFunctions {
    /// The listing of the server `transport` reaches, read once with GET /v1/functions. Raises the server's
    /// error for a failed request, and ContractError for an answer that breaks the functions_response schema.
    pub async fn read(
        transport: Arc<dyn Transport>,
        catalogue: Arc<Catalogue>,
        url: String,
    ) -> Result<ServedFunctions> {
        let answer = transport.functions().await?;
        let base_format = base_format_of(&answer, &url)?;
        let served = ServedFunctions {
            transport,
            catalogue,
            url,
            base_format,
            listing: Mutex::new(Default::default()),
        };
        let listing = served.read_listing(&answer)?;
        *served.listing.lock().expect("the listing lock") = listing;
        Ok(served)
    }

    fn read_listing(&self, answer: &JsonMap) -> Result<(BTreeSet<String>, ServedCalibrations)> {
        let format = base_format_of(answer, &self.url)?;
        if format != self.base_format {
            return Err(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "The server at {} now serves a base of format {}, not {} as when this client connected; the functions bound here use that format's calibration. Connect again",
                    self.url,
                    py_repr(&format.to_json()),
                    py_repr(&self.base_format.to_json())
                ),
            ));
        }
        let mut served: IndexMap<String, String> = IndexMap::new();
        let mut calibrations = ServedCalibrations::new();
        let bad = |what: &str| listing_error(&self.url, "GET /v1/functions", what);
        let Some(Json::Array(functions)) = answer.get("functions") else {
            return Err(bad("no 'functions' list"));
        };
        for listed in functions {
            let Some(id) = listed.get("id").and_then(Json::as_str) else {
                return Err(bad("a function without a string 'id'"));
            };
            // As Python's id.rsplit(".", 1)[0]: the id up to its last dot (the whole id when it has none).
            let adapter = id.rfind('.').map_or(id, |dot| &id[..dot]);
            let Some(version) = listed.get("adapter_version").and_then(Json::as_str) else {
                return Err(bad(&format!(
                    "function {id} without a string 'adapter_version'"
                )));
            };
            served.insert(adapter.to_owned(), version.to_owned());
            let block = match listed.get("calibration") {
                Some(Json::Null) => None,
                Some(block) => Some(calibration_from_wire(block)?),
                None => return Err(bad(&format!("function {id} without 'calibration'"))),
            };
            let mut by_format = IndexMap::new();
            // calibration_by_format is optional in functions_response: a function listed without it has no INT4 entries
            match listed.get("calibration_by_format") {
                None => {}
                Some(Json::Object(entries)) => {
                    for (format_id, entry) in entries {
                        by_format.insert(format_id.clone(), calibration_from_wire(entry)?);
                    }
                }
                Some(_) => {
                    return Err(bad(&format!(
                        "function {id} with a 'calibration_by_format' that is not an object"
                    )));
                }
            }
            calibrations.insert(id.to_owned(), (block, by_format));
        }
        let local: HashMap<String, String> = self
            .catalogue
            .manifests()
            .iter()
            .map(|manifest| (manifest.id.clone(), manifest.version.clone()))
            .collect();
        for (adapter, version) in &served {
            if let Some(mine) = local.get(adapter)
                && mine != version
            {
                return Err(version_mismatch(adapter, version, mine));
            }
        }
        Ok((served.into_keys().collect(), calibrations))
    }

    /// The calibration the server decides `function` with; NotInstalled for a function it does not list.
    pub(crate) fn calibration_of(
        &self,
        function: &FunctionSpec,
        format_id: Option<&str>,
    ) -> Result<Option<Calibration>> {
        let listing = self.listing.lock().expect("the listing lock");
        let (block, by_format) = listing.1.get(&function.id).ok_or_else(|| {
            not_installed(
                &function.id,
                &format!("the server at {} does not list it", self.url),
            )
        })?;
        resolve_calibration(&function.id, block.as_ref(), by_format, format_id)
    }
}

#[async_trait]
impl Engine for ServedFunctions {
    fn base_format(&self) -> BaseFormat {
        self.base_format.clone()
    }

    async fn installed(&self, refresh: bool) -> Result<BTreeSet<String>> {
        if refresh {
            let answer = self.transport.functions().await?;
            let listing = self.read_listing(&answer)?;
            *self.listing.lock().expect("the listing lock") = listing;
        }
        Ok(self.listing.lock().expect("the listing lock").0.clone())
    }

    async fn predict(&self, adapter_id: &str, rows: &[Row]) -> Result<EngineResult> {
        Err(JeffError::new(
            ErrorType::EngineError,
            format!(
                "A remote Jeff sends every decision to the server at {}; nothing may call its engine's predict (asked for {} row(s) of {adapter_id})",
                self.url,
                rows.len()
            ),
        ))
    }
}

/// What a remote Jeff holds besides its ServedFunctions engine.
pub(crate) struct RemoteSide {
    pub(crate) transport: Arc<dyn Transport>,
    pub(crate) served: Arc<ServedFunctions>,
    pub(crate) url: String,
    /// The channel this Jeff talks over (RemoteOptions::channel), whose handles its bound functions are.
    pub(crate) channel: Option<Arc<crate::channel::ChannelTransport>>,
}

/// How [`Jeff::remote`](crate::Jeff::remote) reaches a Jeff server.
#[derive(Clone, Debug)]
pub struct RemoteOptions {
    /// How long each phase of a request may take, in milliseconds: connecting, the answer's headers (sending the
    /// request included), and each wait for more of the answer's body.
    pub timeout_ms: u64,
    /// The manifests calls are bound with; the catalogue shipped with the crate when None.
    pub catalogue: Option<Arc<Catalogue>>,
    /// Use the server's WebSocket channel (lower latency; bound functions become handles on the server) instead of
    /// one HTTP request per operation. timeout_ms then bounds opening the channel, its hello and the closing handshake;
    /// for each request, its wait for a turn to be written, any stall of its upload (no bytes accepted for timeout_ms
    /// closes the channel) and, once it is sent, the wait for its reply.
    pub channel: bool,
}

impl RemoteOptions {
    /// Options with `timeout_ms`, the shipped catalogue and HTTP (no channel).
    pub fn new(timeout_ms: u64) -> RemoteOptions {
        RemoteOptions {
            timeout_ms,
            catalogue: None,
            channel: false,
        }
    }
}

/// A Jeff over `transport`: reads GET /v1/functions once.
pub(crate) async fn connect(
    transport: Arc<dyn Transport>,
    catalogue: Arc<Catalogue>,
    url: String,
    channel: Option<Arc<crate::channel::ChannelTransport>>,
) -> Result<Jeff> {
    let served = Arc::new(
        ServedFunctions::read(Arc::clone(&transport), Arc::clone(&catalogue), url.clone()).await?,
    );
    let side = Arc::new(RemoteSide {
        transport,
        served: Arc::clone(&served),
        url,
        channel,
    });
    Jeff::with_backend(served, catalogue, Backend::Remote(side)).await
}

/// server_policy with every function step named by the full id the local catalogue resolves, so the server runs
/// exactly the steps this client checked.
pub(crate) fn wire_policy(jeff: &Jeff, policy: &Policy) -> Result<Option<WirePolicy>> {
    match server_policy(policy) {
        Some(WirePolicy::Escalate { refs, budget_ms }) => {
            let refs = refs
                .iter()
                .map(|reference| Ok(jeff.catalogue().function(reference)?.id.clone()))
                .collect::<Result<Vec<_>>>()?;
            Ok(Some(WirePolicy::Escalate { refs, budget_ms }))
        }
        other => Ok(other),
    }
}

/// A call as the server takes it, naming the function by the id the local catalogue resolved.
fn call_to_wire(call: &Call, binding: &Binding) -> Json {
    let mut wire = JsonMap::new();
    wire.insert("function".into(), Json::from(binding.function.id.as_str()));
    wire.insert("input".into(), call.input.clone());
    if let Some(context) = &call.context {
        wire.insert("context".into(), Json::Object(context.clone()));
    }
    if let Some(labels) = &call.labels {
        wire.insert("labels".into(), labels.to_json());
    }
    Json::Object(wire)
}

/// A decision the server answered, checked against contract/schemas/decision before it is built.
pub(crate) fn server_decision(data: &Json, url: &str) -> Result<Decision> {
    if !is_valid(SchemaName::Decision, data, None)? {
        return Err(JeffError::new(
            ErrorType::ContractError,
            format!(
                "A decision from {url} does not match contract/schemas/decision: {}",
                first_problem(SchemaName::Decision, data, None)?
            ),
        ));
    }
    Decision::from_json(data)
}

fn server_graph_result(data: &Json, url: &str) -> Result<GraphResult> {
    if !is_valid(SchemaName::GraphResult, data, None)? {
        return Err(JeffError::new(
            ErrorType::ContractError,
            format!(
                "A graph result from {url} does not match contract/schemas/graph_result: {}",
                first_problem(SchemaName::GraphResult, data, None)?
            ),
        ));
    }
    GraphResult::from_json(data)
}

/// One request for these calls; none for no calls (as local mode, which answers [] without the engine).
pub(crate) async fn post(
    side: &RemoteSide,
    bound: &[(Call, Binding)],
    policy: &WirePolicy,
) -> Result<Vec<Decision>> {
    if bound.is_empty() {
        return Ok(Vec::new());
    }
    check_batch(bound.len(), "calls")?;
    let calls = bound
        .iter()
        .map(|(call, binding)| call_to_wire(call, binding))
        .collect();
    let answered = side.transport.decide_calls(calls, policy.to_json()).await?;
    // RestTransport checks the count itself, as Python's does; checked here again for every other Transport.
    check_count(&side.url, answered.len(), bound.len())?;
    answered
        .iter()
        .map(|item| server_decision(item, &side.url))
        .collect()
}

/// Already-bound calls in one request: with the policy when the server can run it, else with "return" and the policy
/// then run here on each decision.
pub(crate) async fn decide_policy(
    jeff: &Jeff,
    side: &RemoteSide,
    bound: &[(Call, Binding)],
    policy: &Policy,
) -> Result<Vec<Decision>> {
    if let Some(wire) = wire_policy(jeff, policy)? {
        return post(side, bound, &wire).await;
    }
    let decisions = post(side, bound, &WirePolicy::Return).await?;
    let mut out = Vec::with_capacity(decisions.len());
    for ((call, _), decision) in bound.iter().zip(decisions) {
        out.push(jeff.apply_policy(call, decision, policy).await?);
    }
    Ok(out)
}

pub(crate) async fn interface_call(
    jeff: &Jeff,
    side: &RemoteSide,
    interface: &str,
    input: Json,
    options: ShorthandOptions,
) -> Result<Decision> {
    let Some(reference) = options.function.clone() else {
        return jeff
            .dispatch(
                input,
                DispatchOptions {
                    interface: interface.to_owned(),
                    router: None,
                    context: options.context,
                    labels: options.labels,
                    on_low_confidence: options.on_low_confidence,
                },
            )
            .await;
    };
    let spec = jeff.catalogue().function(&reference)?;
    if spec.interface != interface {
        return Err(JeffError::new(
            ErrorType::InterfaceMismatch,
            format!(
                "{} implements {}, not {interface}; call jeff.{}",
                spec.id, spec.interface, spec.interface
            ),
        ));
    }
    check_policy(jeff, interface, &options.on_low_confidence).await?;
    let call = Call {
        function: reference,
        input,
        context: options.context,
        labels: options.labels,
    };
    let binding = jeff.bind_call(&call).await?;
    let decisions =
        decide_policy(jeff, side, &[(call, binding)], &options.on_low_confidence).await?;
    Ok(decisions
        .into_iter()
        .next()
        .expect("post answers one decision per call (check_count)"))
}

pub(crate) async fn dispatch(
    jeff: &Jeff,
    side: &RemoteSide,
    input: Json,
    options: DispatchOptions,
) -> Result<Decision> {
    let policy = &options.on_low_confidence;
    check_policy(jeff, &options.interface, policy).await?;
    let router_id = start_router(jeff, options.router.as_deref().unwrap_or(DEFAULT_ROUTER)).await?;
    let wire = wire_policy(jeff, policy)?;
    let mut request = JsonMap::new();
    request.insert("input".into(), input.clone());
    request.insert("interface".into(), Json::from(options.interface.as_str()));
    request.insert("router".into(), Json::from(router_id));
    if let Some(context) = &options.context {
        request.insert("context".into(), Json::Object(context.clone()));
    }
    if let Some(labels) = &options.labels {
        request.insert("labels".into(), labels.to_json());
    }
    let mut body = JsonMap::new();
    body.insert("dispatch".into(), Json::Object(request));
    body.insert(
        "on_low_confidence".into(),
        wire.as_ref().unwrap_or(&WirePolicy::Return).to_json(),
    );
    let decision = server_decision(&side.transport.dispatch(body).await?, &side.url)?;
    if wire.is_some() || !decision.abstained {
        return Ok(decision);
    }
    // The policy has a step only this process can run. As in local mode, a router that abstains raises; an abstained
    // leaf is escalated with the call dispatch made to it.
    let function = jeff.catalogue().function(&decision.function)?;
    if jeff.catalogue().manifest_of(&function)?.is_router {
        return Err(low_confidence(&decision)?);
    }
    let call = leaf_call(
        &function,
        &input,
        options.context.as_ref(),
        options.labels.as_ref(),
    );
    jeff.apply_policy(&call, decision, policy).await
}

/// A graph with no code nodes, whose node policies the server can run, is one request carrying the graph inline (each
/// node's function by the id the local catalogue resolves); any other graph runs here stage by stage. Everything local
/// mode checks before the first decision is checked here first, without a request.
pub(crate) async fn run_graph(
    jeff: &Jeff,
    side: &RemoteSide,
    graph: GraphSource,
    input: Json,
    params: JsonMap,
    code: IndexMap<String, CodeFunction>,
    policies: IndexMap<String, Policy>,
) -> Result<GraphResult> {
    let graph = graph.into_graph()?;
    let model = check_graph(jeff, &graph, &input, &params, &code, &policies).await?;
    let mut wire = IndexMap::new();
    for (name, policy) in &policies {
        wire.insert(name.clone(), wire_policy(jeff, policy)?);
    }
    let has_code = graph.nodes.values().any(|node| node.code.is_some());
    if has_code || wire.values().any(Option::is_none) {
        return run_checked(jeff, &graph, &input, &params, &code, &model).await;
    }
    let mut inline = graph_to_json(&graph)?;
    for (name, (_, binding, _)) in &model {
        inline["nodes"][name]["call"] = Json::from(binding.function.id.as_str());
    }
    let mut body = JsonMap::new();
    body.insert(
        "graph".into(),
        Json::Object(JsonMap::from_iter([("inline".to_owned(), inline)])),
    );
    body.insert("input".into(), input);
    body.insert("params".into(), Json::Object(params));
    if !wire.is_empty() {
        let by_node: JsonMap = wire
            .into_iter()
            .map(|(name, policy)| (name, policy.expect("checked above").to_json()))
            .collect();
        body.insert("on_low_confidence_by_node".into(), Json::Object(by_node));
    }
    server_graph_result(&side.transport.graph(body).await?, &side.url)
}

/// A graph registered on the server, by id, in one request; every node policy must be one the server can run.
pub(crate) async fn run_server_graph(
    jeff: &Jeff,
    side: &RemoteSide,
    graph_id: &str,
    input: Json,
    params: JsonMap,
    policies: IndexMap<String, Policy>,
) -> Result<GraphResult> {
    let mut by_node = JsonMap::new();
    for (name, policy) in &policies {
        let Some(wire) = wire_policy(jeff, policy)? else {
            return Err(JeffError::new(
                ErrorType::InvalidPolicy,
                format!(
                    "Node {name}: a graph registered on the server runs there, and the server cannot call llm, human or remote \
                 escalation steps; use 'return', 'error' or escalate([function_step(...), ...]) for this node"
                ),
            ));
        };
        by_node.insert(name.clone(), wire.to_json());
    }
    let mut body = JsonMap::new();
    body.insert(
        "graph".into(),
        Json::Object(JsonMap::from_iter([(
            "id".to_owned(),
            Json::from(graph_id),
        )])),
    );
    body.insert("input".into(), input);
    body.insert("params".into(), Json::Object(params));
    if !by_node.is_empty() {
        body.insert("on_low_confidence_by_node".into(), Json::Object(by_node));
    }
    server_graph_result(&side.transport.graph(body).await?, &side.url)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::errors::BoxError;
    use crate::escalation::{Question, escalate, function_step, llm};

    #[tokio::test]
    async fn an_empty_or_unusable_token_is_refused_without_showing_it() {
        let empty = TokenSource::function(|| async { Ok::<String, BoxError>(String::new()) });
        assert_eq!(
            read_token(&empty, "http://h").await.unwrap_err().message(),
            "The token for http://h must be a non-empty string; the token function returned an empty string"
        );
        let secret =
            TokenSource::function(|| async { Ok::<String, BoxError>("s3cret\nvalue".into()) });
        let error = read_token(&secret, "http://h").await.unwrap_err();
        assert_eq!(error.kind(), ErrorType::Unauthorised);
        assert!(!error.message().contains("s3cret"));
    }

    #[test]
    fn policies_the_server_runs_are_sent_and_llm_steps_run_here() {
        assert_eq!(server_policy(&Policy::Return), Some(WirePolicy::Return));
        let functions = escalate(vec![function_step("a.b.c")], Some(50)).unwrap();
        assert_eq!(
            server_policy(&functions).unwrap().to_json(),
            json!({"escalate": ["a.b.c"], "budget_ms": 50})
        );
        let asks = escalate(
            vec![
                function_step("a.b.c"),
                llm(|_: Question| async { Ok::<String, BoxError>("x".into()) }),
            ],
            None,
        )
        .unwrap();
        assert_eq!(server_policy(&asks), None);
        assert!(check_batch(MAX_CALLS, "calls").is_ok());
        assert_eq!(
            check_batch(MAX_CALLS + 1, "calls").unwrap_err().message(),
            "257 calls in one request: a Jeff server answers at most 256 per request; send them in batches of at most 256"
        );
    }
}
