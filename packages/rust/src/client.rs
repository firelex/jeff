//! The Jeff client: bind functions, decide in batches grouped by adapter, the interface shorthands, dispatch (port of
//! jeff/client.py). Every method that reaches an engine is async. A Jeff is a cheap handle: clone it to share it.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures_util::future::join_all;
use indexmap::IndexMap;

use crate::binding::{Binding, Call, Context, Labels, Row, bind, build_row};
use crate::catalogue::Catalogue;
use crate::decision::{Decision, make_decision};
use crate::engine::{BaseFormat, Engine, format_id_of, serves};
use crate::errors::{ErrorType, JeffError, Result, not_installed};
use crate::escalation::{Decider, Policy, apply_policy, check_policy};
use crate::json::Json;
use crate::manifest::{Calibration, FunctionSpec, calibration_for};
use crate::routing::{check_routers, exact_keys_of};

/// How a bound function's calls are made.
#[derive(Clone, Debug, Default)]
pub struct CallOptions {
    /// Context values by code name, when the function takes context.
    pub context: Option<Context>,
    /// The caller's labels, when the function takes them from the caller.
    pub labels: Option<Labels>,
    /// What happens when the decision abstains (`Policy::Return` when not set).
    pub on_low_confidence: Policy,
}

/// How an interface shorthand (classify, route, rate, ...) is called.
#[derive(Clone, Debug, Default)]
pub struct ShorthandOptions {
    /// The function to call; without it, the call is routed (dispatch) to a function implementing the interface.
    pub function: Option<String>,
    /// Context values by code name, when the function takes context.
    pub context: Option<Context>,
    /// The caller's labels, when the function takes them from the caller.
    pub labels: Option<Labels>,
    /// What happens when the decision abstains (`Policy::Return` when not set).
    pub on_low_confidence: Policy,
}

/// How a call is routed: the interface the answer must come through, and where routing starts.
#[derive(Clone, Debug)]
pub struct DispatchOptions {
    /// The interface the answering function must implement.
    pub interface: String,
    /// The router to start from; `jeff.core.router` when not set.
    pub router: Option<String>,
    /// Context values by code name, passed to the function routing reaches.
    pub context: Option<Context>,
    /// The caller's labels, passed to the function routing reaches.
    pub labels: Option<Labels>,
    /// What happens when the decision abstains.
    pub on_low_confidence: Policy,
}

impl DispatchOptions {
    /// Routing to `interface` from the default router, with no context or labels and `Policy::Return`.
    pub fn new(interface: impl Into<String>) -> DispatchOptions {
        DispatchOptions {
            interface: interface.into(),
            router: None,
            context: None,
            labels: None,
            on_low_confidence: Policy::Return,
        }
    }
}

/// The longest timeout Tokio's timers keep (about 2.2 years).
pub const MAX_TIMEOUT_MS: u64 = 68_719_476_734;

/// Refuses a timeout of 0 or one longer than Tokio's timers keep.
pub(crate) fn check_timeout_ms(value: u64) -> Result<()> {
    if value == 0 {
        return Err(JeffError::new(
            ErrorType::InvalidRequest,
            "timeout_ms must be a whole number of milliseconds above 0; got 0",
        ));
    }
    if value > MAX_TIMEOUT_MS {
        return Err(JeffError::new(
            ErrorType::InvalidRequest,
            format!(
                "timeout_ms must be at most {MAX_TIMEOUT_MS} milliseconds (about 2.2 years), the longest timer Tokio keeps; got {value}"
            ),
        ));
    }
    Ok(())
}

/// How `Jeff::local` reaches the engine on this machine.
#[derive(Clone, Debug)]
pub struct LocalOptions {
    /// How long to wait for the engine's hello and for each reply, in milliseconds.
    pub timeout_ms: u64,
    /// The manifests calls are bound with; the catalogue shipped with the crate when None.
    pub catalogue: Option<Arc<Catalogue>>,
}

impl LocalOptions {
    /// Options with `timeout_ms` and the shipped catalogue.
    pub fn new(timeout_ms: u64) -> LocalOptions {
        LocalOptions {
            timeout_ms,
            catalogue: None,
        }
    }
}

/// How a Jeff reaches its decisions: through its engine (local), or through a Jeff server (remote), whose listing is
/// then its engine and which answers every decision.
pub(crate) enum Backend {
    Local,
    Remote(Arc<crate::remote::RemoteSide>),
}

struct Inner {
    engine: Arc<dyn Engine>,
    catalogue: Arc<Catalogue>,
    base_format: BaseFormat,
    format_id: Option<String>,
    /// Router id -> the exact keys its tree refuses (routing::exact_keys_of).
    exact_keys: Mutex<HashMap<String, Arc<IndexMap<String, String>>>>,
    /// Function id -> the calibration it decides with; a refusal is not kept, and is raised again next time.
    calibrations: Mutex<HashMap<String, Option<Calibration>>>,
    backend: Backend,
}

/// The client: binds and calls Jeff's decision functions, routes, escalates and runs graphs, against an
/// engine on this machine ([`Jeff::local`]), a Jeff server ([`Jeff::remote`]) or any [`Engine`] ([`Jeff::new`]).
/// Clones share one connection.
#[derive(Clone)]
pub struct Jeff {
    inner: Arc<Inner>,
}

impl std::fmt::Debug for Jeff {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut shown = formatter.debug_struct("Jeff");
        if let Some(side) = self.remote_side() {
            shown.field("server", &side.url);
        }
        shown
            .field("base_format", &self.inner.base_format)
            .finish_non_exhaustive()
    }
}

impl Jeff {
    /// A Jeff over any engine (a SocketEngine, a FakeEngine, your own), binding calls with `catalogue`. Every
    /// installed router is checked here (its targets resolve, are installed and form no loop).
    pub async fn new(engine: Arc<dyn Engine>, catalogue: Arc<Catalogue>) -> Result<Jeff> {
        Jeff::with_backend(engine, catalogue, Backend::Local).await
    }

    /// A Jeff over the engine already running on this machine at `socket_path` (start it with `jeff runtime serve`).
    /// It never starts an engine and never falls back to a server: no engine there is an EngineError naming the path
    /// and the command that starts one.
    pub async fn local(
        socket_path: impl AsRef<std::path::Path>,
        options: LocalOptions,
    ) -> Result<Jeff> {
        check_timeout_ms(options.timeout_ms)?;
        let engine = Arc::new(
            crate::socket_engine::SocketEngine::connect(socket_path, options.timeout_ms).await?,
        );
        let catalogue = match options.catalogue {
            Some(catalogue) => catalogue,
            None => Catalogue::shipped()?,
        };
        match Jeff::new(engine.clone(), catalogue).await {
            Ok(jeff) => Ok(jeff),
            Err(error) => {
                engine.close().await;
                Err(error)
            }
        }
    }

    pub(crate) async fn with_backend(
        engine: Arc<dyn Engine>,
        catalogue: Arc<Catalogue>,
        backend: Backend,
    ) -> Result<Jeff> {
        let base_format = engine.base_format();
        let format_id = format_id_of(&base_format)?;
        let exact = check_routers(&catalogue, &engine.installed(false).await?)?;
        Ok(Jeff {
            inner: Arc::new(Inner {
                engine,
                catalogue,
                base_format,
                format_id,
                exact_keys: Mutex::new(exact),
                calibrations: Mutex::new(HashMap::new()),
                backend,
            }),
        })
    }

    /// A Jeff over the Jeff server at `url` (our API or your own). `token` is sent as a bearer token (a function is
    /// called before every request; over the channel, for the handshake, auth, each refresh window and each GET
    /// /v1/functions). Connecting reads GET /v1/functions once and refuses a server serving another version of an
    /// adapter the catalogue has. With `options.channel`, every operation goes over the server's WebSocket channel
    /// and bound functions are handles on the server.
    pub async fn remote(
        url: &str,
        token: crate::remote::TokenSource,
        options: crate::remote::RemoteOptions,
    ) -> Result<Jeff> {
        check_timeout_ms(options.timeout_ms)?;
        let catalogue = match options.catalogue {
            Some(catalogue) => catalogue,
            None => Catalogue::shipped()?,
        };
        if options.channel {
            let transport = Arc::new(
                crate::channel::ChannelTransport::open(url, token, options.timeout_ms).await?,
            );
            let base = transport.url().to_owned();
            return match crate::remote::connect(
                transport.clone(),
                catalogue,
                base,
                Some(Arc::clone(&transport)),
            )
            .await
            {
                Ok(jeff) => Ok(jeff),
                Err(error) => {
                    transport.close().await;
                    Err(error)
                }
            };
        }
        let transport = Arc::new(crate::remote::RestTransport::new(
            url,
            token,
            options.timeout_ms,
        )?);
        let base = transport.url().to_owned();
        match crate::remote::connect(transport.clone(), catalogue, base, None).await {
            Ok(jeff) => Ok(jeff),
            Err(error) => {
                crate::remote::Transport::close(transport.as_ref()).await;
                Err(error)
            }
        }
    }

    fn remote_side(&self) -> Option<&Arc<crate::remote::RemoteSide>> {
        match &self.inner.backend {
            Backend::Remote(side) => Some(side),
            Backend::Local => None,
        }
    }

    /// The channel a `Jeff::remote(..., RemoteOptions { channel: true, .. })` talks over, for its lower-level handle
    /// operations; None for any other Jeff.
    pub fn channel_transport(&self) -> Option<&Arc<crate::channel::ChannelTransport>> {
        self.remote_side().and_then(|side| side.channel.as_ref())
    }

    /// The catalogue calls are bound with.
    pub fn catalogue(&self) -> &Arc<Catalogue> {
        &self.inner.catalogue
    }

    /// The engine decisions come from (for a remote Jeff, the server's listing).
    pub fn engine(&self) -> &Arc<dyn Engine> {
        &self.inner.engine
    }

    /// The engine's base format, read once when this Jeff was made.
    pub fn base_format(&self) -> &BaseFormat {
        &self.inner.base_format
    }

    /// The engine's INT4 format_id; none on a bfloat16 base.
    pub fn format_id(&self) -> Option<&str> {
        self.inner.format_id.as_deref()
    }

    /// The calibration `function` decides with on this Jeff's engine, resolved once per function and kept.
    pub fn calibration_of(&self, function: &FunctionSpec) -> Result<Option<Calibration>> {
        if let Some(found) = self
            .inner
            .calibrations
            .lock()
            .expect("the calibration cache lock")
            .get(&function.id)
        {
            return Ok(found.clone());
        }
        let resolved = self.resolve_calibration_of(function)?;
        self.inner
            .calibrations
            .lock()
            .expect("the calibration cache lock")
            .insert(function.id.clone(), resolved.clone());
        Ok(resolved)
    }

    /// The calibration `function` decides with, not kept: the manifest's, or for a remote Jeff the one the server
    /// lists (NotInstalled for a function it does not list).
    fn resolve_calibration_of(&self, function: &FunctionSpec) -> Result<Option<Calibration>> {
        match self.remote_side() {
            Some(side) => side.served.calibration_of(function, self.format_id()),
            None => calibration_for(function, self.format_id()),
        }
    }

    /// The exact keys a router's tree refuses: worked out when the Jeff was built for every installed router, and at
    /// the first dispatch for a router installed later.
    pub(crate) fn exact_keys_for(&self, router_id: &str) -> Result<Arc<IndexMap<String, String>>> {
        if let Some(found) = self
            .inner
            .exact_keys
            .lock()
            .expect("the exact-keys cache lock")
            .get(router_id)
        {
            return Ok(Arc::clone(found));
        }
        let made = Arc::new(exact_keys_of(self.catalogue(), router_id)?);
        self.inner
            .exact_keys
            .lock()
            .expect("the exact-keys cache lock")
            .insert(router_id.to_owned(), Arc::clone(&made));
        Ok(made)
    }

    /// The one place a call is bound: its function resolved, its adapter checked as served, its calibration chosen.
    pub(crate) async fn bind_call(&self, call: &Call) -> Result<Binding> {
        let function = self.catalogue().function(&call.function)?;
        let manifest = self.catalogue().manifest_of(&function)?;
        if !serves(self.engine().as_ref(), &manifest.id).await? {
            return Err(not_installed(&function.id, ""));
        }
        let calibration = self.calibration_of(&function)?;
        bind(
            &manifest,
            &function,
            call.context.as_ref(),
            call.labels.as_ref(),
            calibration,
        )
    }

    /// The decisions for `calls`, in order, with one engine request per adapter. No escalation policy is
    /// applied. Raises InvalidReference or UnknownFunction for a bad reference, NotInstalled for an adapter the engine
    /// does not serve, the context and label errors of binding, and the engine's or server's errors (with a remote
    /// Jeff, more than 256 calls is InvalidRequest).
    pub async fn decide(&self, calls: &[Call]) -> Result<Vec<Decision>> {
        let mut bound = Vec::with_capacity(calls.len());
        for call in calls {
            bound.push((call.clone(), self.bind_call(call).await?));
        }
        self.decide_bound(&bound).await
    }

    /// The decision for one call.
    pub(crate) async fn decide_one(&self, call: &Call) -> Result<Decision> {
        Ok(self
            .decide(std::slice::from_ref(call))
            .await?
            .into_iter()
            .next()
            .expect("decide answers one decision per call"))
    }

    /// Bound calls, one predict per adapter (in the order the adapters are first met), sent together so the engine
    /// can batch them; decisions in call order.
    pub(crate) async fn decide_bound(&self, bound: &[(Call, Binding)]) -> Result<Vec<Decision>> {
        match self.remote_side() {
            Some(side) => {
                crate::remote::post(side, bound, &crate::remote::WirePolicy::Return).await
            }
            None => self.decide_locally(bound).await,
        }
    }

    /// A failure is reported as Python, which handles one adapter after another, meets it first: each earlier
    /// adapter's predict and decisions come before a later adapter's rows are built.
    pub(crate) async fn decide_locally(&self, bound: &[(Call, Binding)]) -> Result<Vec<Decision>> {
        let mut groups: IndexMap<String, Vec<usize>> = IndexMap::new();
        for (index, (_, binding)) in bound.iter().enumerate() {
            groups
                .entry(binding.manifest.id.clone())
                .or_default()
                .push(index);
        }
        let mut requests: Vec<(String, Vec<Row>)> = Vec::new();
        let mut build_failure = None;
        for (adapter_id, indices) in &groups {
            let rows = indices
                .iter()
                .map(|&index| build_row(&bound[index].1, &bound[index].0.input))
                .collect::<Result<Vec<_>>>();
            match rows {
                Ok(rows) => requests.push((adapter_id.clone(), rows)),
                Err(error) => {
                    build_failure = Some(error);
                    break;
                }
            }
        }
        let engine = Arc::clone(self.engine());
        let outcomes = join_all(
            requests
                .iter()
                .map(|(adapter_id, rows)| engine.predict(adapter_id, rows)),
        )
        .await;
        let mut results: Vec<Option<Decision>> = vec![None; bound.len()];
        for ((adapter_id, indices), outcome) in groups.iter().zip(outcomes) {
            let output = outcome?;
            if output.probabilities.len() != indices.len() {
                return Err(JeffError::new(
                    ErrorType::EngineError,
                    format!(
                        "The engine answered {} of {} rows for {adapter_id}",
                        output.probabilities.len(),
                        indices.len()
                    ),
                ));
            }
            for (row, &index) in indices.iter().enumerate() {
                results[index] = Some(make_decision(
                    &bound[index].1,
                    &output.probabilities[row],
                    &output.timing,
                )?);
            }
        }
        if let Some(error) = build_failure {
            return Err(error);
        }
        Ok(results
            .into_iter()
            .map(|decision| decision.expect("every call was decided"))
            .collect())
    }

    /// Binds once, now: context, labels and the policy are checked here and the binding is reused by every call.
    pub async fn function(&self, reference: &str, options: CallOptions) -> Result<BoundFunction> {
        let template = Call {
            function: reference.to_owned(),
            input: Json::Null,
            context: options.context,
            labels: options.labels,
        };
        let binding = self.bind_call(&template).await?;
        check_policy(
            self,
            &binding.function.interface,
            &options.on_low_confidence,
        )
        .await?;
        if let Some(transport) = self.channel_transport() {
            let wire = crate::remote::wire_policy(self, &options.on_low_confidence)?;
            let policy = wire.as_ref().map_or_else(
                || crate::remote::WirePolicy::Return.to_json(),
                |wire| wire.to_json(),
            );
            let (handle, version) = transport
                .bind(
                    &binding.function.id,
                    template.context.as_ref(),
                    template.labels.as_ref().map(Labels::to_json),
                    policy,
                )
                .await?;
            if version != binding.manifest.version {
                // The adapter changed on the server since connecting: the handle is released before the next request.
                transport.release_later(handle);
                return Err(crate::remote::version_mismatch(
                    &binding.manifest.id,
                    &version,
                    &binding.manifest.version,
                ));
            }
            let kind = BoundKind::Channel(ChannelHandle {
                transport: Arc::clone(transport),
                handle,
                wire,
                released: AtomicBool::new(false),
            });
            return Ok(BoundFunction::from_parts(
                self.clone(),
                template,
                options.on_low_confidence,
                binding,
                kind,
            ));
        }
        let kind = if self.remote_side().is_some() {
            BoundKind::Remote
        } else {
            BoundKind::Local
        };
        Ok(BoundFunction::from_parts(
            self.clone(),
            template,
            options.on_low_confidence,
            binding,
            kind,
        ))
    }

    /// What `policy` makes of `decision`, the answer to `call`: the decision itself, LowConfidence, or the
    /// escalated decision.
    pub(crate) async fn apply_policy(
        &self,
        call: &Call,
        decision: Decision,
        policy: &Policy,
    ) -> Result<Decision> {
        apply_policy(self, call, decision, policy).await
    }

    async fn interface_call(
        &self,
        interface: &str,
        input: Json,
        options: ShorthandOptions,
    ) -> Result<Decision> {
        if let Some(side) = self.remote_side() {
            return crate::remote::interface_call(self, side, interface, input, options).await;
        }
        let Some(reference) = options.function else {
            return self
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
        let spec = self.catalogue().function(&reference)?;
        if spec.interface != interface {
            return Err(JeffError::new(
                ErrorType::InterfaceMismatch,
                format!(
                    "{} implements {}, not {interface}; call jeff.{}",
                    spec.id, spec.interface, spec.interface
                ),
            ));
        }
        check_policy(self, interface, &options.on_low_confidence).await?;
        let call = Call {
            function: reference,
            input,
            context: options.context,
            labels: options.labels,
        };
        let decision = self.decide_one(&call).await?;
        self.apply_policy(&call, decision, &options.on_low_confidence)
            .await
    }

    /// Routes the call through the routers to a function implementing `options.interface`.
    pub async fn dispatch(&self, input: Json, options: DispatchOptions) -> Result<Decision> {
        match self.remote_side() {
            Some(side) => crate::remote::dispatch(self, side, input, options).await,
            None => crate::routing::dispatch(self, input, options).await,
        }
    }

    /// Calls a function implementing the `classify` interface (choose one label from a defined set) with `input`: the
    /// function `options.function` names, or, without one, the function the routers choose. Raises InterfaceMismatch
    /// when the named function implements another interface, and the errors [`decide`](Jeff::decide) raises.
    pub async fn classify(&self, input: Json, options: ShorthandOptions) -> Result<Decision> {
        self.interface_call("classify", input, options).await
    }

    /// Calls a function implementing the `route` interface (choose which candidate should handle the input) with
    /// `input`: the function `options.function` names, or, without one, the function the routers choose. Raises
    /// InterfaceMismatch when the named function implements another interface, and the errors [`decide`](Jeff::decide)
    /// raises.
    pub async fn route(&self, input: Json, options: ShorthandOptions) -> Result<Decision> {
        self.interface_call("route", input, options).await
    }

    /// Calls a function implementing the `rate` interface (place the input on an ordered scale of levels) with `input`:
    /// the function `options.function` names, or, without one, the function the routers choose. Raises
    /// InterfaceMismatch when the named function implements another interface, and the errors [`decide`](Jeff::decide)
    /// raises.
    pub async fn rate(&self, input: Json, options: ShorthandOptions) -> Result<Decision> {
        self.interface_call("rate", input, options).await
    }

    /// Calls a function implementing the `detect` interface (whether the input is an instance of what the function
    /// detects) with `input`: the function `options.function` names, or, without one, the function the routers choose.
    /// Raises InterfaceMismatch when the named function implements another interface, and the errors
    /// [`decide`](Jeff::decide) raises.
    pub async fn detect(&self, input: Json, options: ShorthandOptions) -> Result<Decision> {
        self.interface_call("detect", input, options).await
    }

    /// Calls a function implementing the `is_supported` interface (whether an answer is supported by its sources) with
    /// `input`: the function `options.function` names, or, without one, the function the routers choose. Raises
    /// InterfaceMismatch when the named function implements another interface, and the errors [`decide`](Jeff::decide)
    /// raises.
    pub async fn is_supported(&self, input: Json, options: ShorthandOptions) -> Result<Decision> {
        self.interface_call("is_supported", input, options).await
    }

    /// Calls a function implementing the `equivalent` interface (whether two records describe the same entity) with
    /// `input`: the function `options.function` names, or, without one, the function the routers choose. Raises
    /// InterfaceMismatch when the named function implements another interface, and the errors [`decide`](Jeff::decide)
    /// raises.
    pub async fn equivalent(&self, input: Json, options: ShorthandOptions) -> Result<Decision> {
        self.interface_call("equivalent", input, options).await
    }

    /// Calls a function implementing the `requires_escalation` interface (whether a person must act on the input now)
    /// with `input`: the function `options.function` names, or, without one, the function the routers choose. Raises
    /// InterfaceMismatch when the named function implements another interface, and the errors [`decide`](Jeff::decide)
    /// raises.
    pub async fn requires_escalation(
        &self,
        input: Json,
        options: ShorthandOptions,
    ) -> Result<Decision> {
        self.interface_call("requires_escalation", input, options)
            .await
    }

    /// Runs a graph: a Graph, a graph file's path, or a graph document. `code`: the code nodes' functions by name;
    /// `policies`: a policy per node, in place of the document's 'return' or 'error'.
    pub async fn run_graph(
        &self,
        graph: impl Into<crate::graph::GraphSource>,
        input: Json,
        params: crate::json::JsonMap,
        code: IndexMap<String, crate::graph::CodeFunction>,
        policies: IndexMap<String, Policy>,
    ) -> Result<crate::graph::GraphResult> {
        if let Some(side) = self.remote_side() {
            return crate::remote::run_graph(
                self,
                side,
                graph.into(),
                input,
                params,
                code,
                policies,
            )
            .await;
        }
        let graph = graph.into().into_graph()?;
        let model =
            crate::graph::check_graph(self, &graph, &input, &params, &code, &policies).await?;
        crate::graph::run_checked(self, &graph, &input, &params, &code, &model).await
    }

    /// A graph registered on the server, by id, in one request (remote mode only). Every node policy must be one the
    /// server can run: the graph runs entirely there.
    pub async fn run_server_graph(
        &self,
        graph_id: &str,
        input: Json,
        params: crate::json::JsonMap,
        policies: IndexMap<String, Policy>,
    ) -> Result<crate::graph::GraphResult> {
        let Some(side) = self.remote_side() else {
            return Err(JeffError::new(
                ErrorType::InvalidRequest,
                "run_server_graph runs a graph registered on a Jeff server; this Jeff talks to a local engine, so pass the graph to run_graph",
            ));
        };
        crate::remote::run_server_graph(self, side, graph_id, input, params, policies).await
    }

    /// Ends the engine's connection (a remote Jeff's connections to the server); later calls fail.
    pub async fn close(&self) {
        match self.remote_side() {
            Some(side) => side.transport.close().await,
            None => self.engine().close().await,
        }
    }
}

#[async_trait]
impl Decider for Jeff {
    async fn decide(&self, calls: &[Call]) -> Result<Vec<Decision>> {
        Jeff::decide(self, calls).await
    }
}

/// How a bound function reaches its decisions: through the Jeff's engine, one request to a Jeff server for all of a
/// many(), or a handle on the server's channel.
pub(crate) enum BoundKind {
    Local,
    Remote,
    Channel(ChannelHandle),
}

/// A handle bound on the server's channel; released before the channel's next request when the last clone of its
/// bound function is dropped without close().
pub(crate) struct ChannelHandle {
    transport: Arc<crate::channel::ChannelTransport>,
    handle: u64,
    /// The policy the server runs with the handle; None when the policy has a step only this process can run (the
    /// handle is then bound with "return" and the policy run here on each decision).
    wire: Option<crate::remote::WirePolicy>,
    released: AtomicBool,
}

impl Drop for ChannelHandle {
    fn drop(&mut self) {
        if !self.released.load(Ordering::SeqCst) {
            self.transport.release_later(self.handle);
        }
    }
}

pub(crate) struct BoundInner {
    pub(crate) jeff: Jeff,
    pub(crate) template: Call,
    pub(crate) policy: Policy,
    pub(crate) binding: Binding,
    pub(crate) kind: BoundKind,
}

/// A function bound once to its context, labels and policy; each call sends only the input. Clone it to share it.
#[derive(Clone)]
pub struct BoundFunction {
    pub(crate) inner: Arc<BoundInner>,
}

impl std::fmt::Debug for BoundFunction {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("BoundFunction")
            .field("function", &self.inner.binding.function.id)
            .finish_non_exhaustive()
    }
}

impl BoundFunction {
    pub(crate) fn from_parts(
        jeff: Jeff,
        template: Call,
        policy: Policy,
        binding: Binding,
        kind: BoundKind,
    ) -> BoundFunction {
        BoundFunction {
            inner: Arc::new(BoundInner {
                jeff,
                template,
                policy,
                binding,
                kind,
            }),
        }
    }

    /// The binding every call of this function reuses.
    pub fn binding(&self) -> &Binding {
        &self.inner.binding
    }

    pub(crate) fn call_of(&self, input: Json) -> Call {
        Call {
            input,
            ..self.inner.template.clone()
        }
    }

    /// The live channel handle, or InvalidRequest once close() has released it.
    fn live(&self, handle: &'_ ChannelHandle) -> Result<u64> {
        if handle.released.load(Ordering::SeqCst) {
            return Err(JeffError::new(
                ErrorType::InvalidRequest,
                format!(
                    "This bound function of {} is closed; bind it again with jeff.function",
                    self.inner.binding.function.id
                ),
            ));
        }
        Ok(handle.handle)
    }

    /// The decision for `input`, with the bound policy applied. Raises the errors
    /// [`Jeff::decide`] raises, LowConfidence or the escalation errors when the policy says so, and InvalidRequest once
    /// closed.
    pub async fn call(&self, input: Json) -> Result<Decision> {
        if let BoundKind::Channel(handle) = &self.inner.kind {
            let number = self.live(handle)?;
            let decision =
                Decision::from_json(&handle.transport.call(number, input.clone()).await?)?;
            if handle.wire.is_some() {
                return Ok(decision);
            }
            return self
                .inner
                .jeff
                .apply_policy(&self.call_of(input), decision, &self.inner.policy)
                .await;
        }
        Ok(self
            .many(vec![input])
            .await?
            .into_iter()
            .next()
            .expect("many answers one decision per input"))
    }

    /// The decisions for each of `inputs`, in order, with the bound policy applied to each. Raises as
    /// [`call`](BoundFunction::call) does; with a remote Jeff, more than 256 inputs is InvalidRequest.
    pub async fn many(&self, inputs: Vec<Json>) -> Result<Vec<Decision>> {
        let inner = &self.inner;
        if let BoundKind::Channel(handle) = &inner.kind {
            let number = self.live(handle)?;
            if inputs.is_empty() {
                return Ok(Vec::new());
            }
            crate::remote::check_batch(inputs.len(), "inputs")?;
            let answered = handle.transport.call_many(number, inputs.clone()).await?;
            let decisions = answered
                .iter()
                .map(Decision::from_json)
                .collect::<Result<Vec<_>>>()?;
            if handle.wire.is_some() {
                return Ok(decisions);
            }
            let mut out = Vec::with_capacity(decisions.len());
            for (input, decision) in inputs.into_iter().zip(decisions) {
                out.push(
                    inner
                        .jeff
                        .apply_policy(&self.call_of(input), decision, &inner.policy)
                        .await?,
                );
            }
            return Ok(out);
        }
        let calls: Vec<(Call, Binding)> = inputs
            .into_iter()
            .map(|input| (self.call_of(input), inner.binding.clone()))
            .collect();
        if let (BoundKind::Remote, Some(side)) = (&inner.kind, inner.jeff.remote_side()) {
            return crate::remote::decide_policy(&inner.jeff, side, &calls, &inner.policy).await;
        }
        let decisions = inner.jeff.decide_bound(&calls).await?;
        let mut out = Vec::with_capacity(decisions.len());
        for ((call, _), decision) in calls.iter().zip(decisions) {
            out.push(
                inner
                    .jeff
                    .apply_policy(call, decision, &inner.policy)
                    .await?,
            );
        }
        Ok(out)
    }

    /// Releases what this bound function holds on a server: its channel handle (waiting for the server's reply);
    /// nothing for a local one or one over HTTP. Nothing to release once the channel is closed: its handles ended with
    /// it. Calling it again does nothing more; later calls fail with InvalidRequest.
    pub async fn close(&self) -> Result<()> {
        let BoundKind::Channel(handle) = &self.inner.kind else {
            return Ok(());
        };
        if handle.released.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        if !handle.transport.closed() {
            handle.transport.release(handle.handle).await?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_timeout_beyond_the_longest_tokio_timer_is_refused() {
        assert!(check_timeout_ms(MAX_TIMEOUT_MS).is_ok());
        assert_eq!(
            check_timeout_ms(MAX_TIMEOUT_MS + 1).unwrap_err().message(),
            "timeout_ms must be at most 68719476734 \
            milliseconds (about 2.2 years), the longest timer Tokio keeps; got 68719476735"
        );
        assert_eq!(
            check_timeout_ms(0).unwrap_err().message(),
            "timeout_ms must be a whole number of milliseconds above 0; got 0"
        );
    }
}
