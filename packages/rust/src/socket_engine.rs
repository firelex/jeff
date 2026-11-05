//! An Engine over the engine socket (contract/socket.md; port of jeff/socket_engine.py). See Task 13 of the Rust SDK
//! plan for how replies, failures and reconnects are ordered. It never starts an engine.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use tokio::io::AsyncWriteExt;
use tokio::net::UnixStream;
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::binding::Row;
use crate::engine::{BaseFormat, Engine, EngineResult};
use crate::errors::{ErrorType, JeffError, Result, error_from_wire};
use crate::escalation::BoxFuture;
use crate::json::{Json, JsonMap};
use crate::protocol::{FrameFailure, FrameReader, check_hello, decode_body, encode_frame, hello};
use crate::pyformat::{py_fixed, py_float, py_repr};

/// The fewest milliseconds between two reconnect attempts. A call made sooner after an attempt fails at once, naming
/// that attempt, so that while the engine is down every caller learns so immediately and its socket is not hammered.
pub const RECONNECT_INTERVAL_MS: u64 = 1000;

/// Replies to these ops carry the engine's whole installed list beside its version.
const CARRY_ADAPTERS: [&str; 3] = ["installed", "serve", "reload"];

/// The command that starts an engine listening at `path`.
pub(crate) fn start_command(path: &str) -> String {
    format!(
        "jeff runtime serve --socket {path} --adapter <adapter id> --batch-window-ms <milliseconds> --max-batch-rows \
         <rows> --queue-limit-rows <rows> --prepare-workers <threads>"
    )
}

type Reply = std::result::Result<JsonMap, JeffError>;

/// One open connection. Its writer task owns the write half and writes each whole frame it is handed, in order, so a
/// caller whose future is dropped never leaves half a frame on the socket; its reader task reads every reply.
struct Connection {
    generation: u64,
    frames: mpsc::UnboundedSender<Vec<u8>>,
    reader: JoinHandle<()>,
    writer: JoinHandle<()>,
}

impl Connection {
    /// Ends both tasks: the socket closes, and a write blocked on an engine that stopped reading is abandoned.
    fn stop(&self) {
        self.reader.abort();
        self.writer.abort();
    }
}

/// A message read from the engine, decoded outside the state lock, or why the connection ended.
enum Incoming {
    Message(JsonMap),
    Failure(JeffError),
}

type Deliveries = Vec<(oneshot::Sender<Reply>, JsonMap)>;

/// Removes a call's pending entry when the call stops waiting for any reason (its reply came, it timed out, or its
/// future was dropped), so an id the engine never answers is not kept.
struct Waiting<'a> {
    shared: &'a Shared,
    id: u64,
}

impl Drop for Waiting<'_> {
    fn drop(&mut self) {
        if let Ok(mut state) = self.shared.state.lock() {
            state.pending.remove(&self.id); // remove: not an ordered map
        }
    }
}

struct State {
    connection: Option<Connection>,
    closed: Option<JeffError>,
    closed_by_caller: bool,
    pending: HashMap<u64, oneshot::Sender<Reply>>,
    next_id: u64,
    generation: u64,
    snapshot: (i64, BTreeSet<String>),
    last_attempt: Option<(Instant, Option<JeffError>)>,
    format: BaseFormat,
    served: (String, String),
}

struct Shared {
    path: String,
    timeout: Duration,
    timeout_ms: u64,
    state: Mutex<State>,
    reconnecting: tokio::sync::Mutex<()>,
    reconnects: AtomicU64,
}

enum OpenFailure {
    /// Nothing listens at the path: the socket's own error.
    Dial(std::io::Error),
    Jeff(JeffError),
}

impl From<JeffError> for OpenFailure {
    fn from(error: JeffError) -> OpenFailure {
        OpenFailure::Jeff(error)
    }
}

fn engine_error(message: String) -> JeffError {
    JeffError::new(ErrorType::EngineError, message)
}

/// The error a failure reply describes. The engine puts its adapters_version in every failure's details; that is
/// protocol information, not an attribute of the error, so it is left out.
fn rebuild(body: &Json) -> JeffError {
    let mut details = body
        .get("details")
        .and_then(Json::as_object)
        .cloned()
        .unwrap_or_default();
    details.shift_remove("adapters_version");
    error_from_wire(
        body.get("type")
            .and_then(Json::as_str)
            .expect("the failure-reply schema requires type"),
        body.get("message")
            .and_then(Json::as_str)
            .expect("the failure-reply schema requires message"),
        Some(&Json::Object(details)),
    )
}

impl Shared {
    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().expect("the socket engine state lock")
    }

    /// Ends the connection of `generation`, if it is still the current one: the first error wins and every waiting
    /// call fails with its own copy of it.
    fn fail_locked(&self, state: &mut State, generation: u64, error: JeffError) {
        let Some(connection) = state
            .connection
            .take_if(|connection| connection.generation == generation)
        else {
            return;
        };
        connection.stop();
        if state.closed.is_none() {
            state.closed = Some(error);
        }
        let closed = state.closed.clone().expect("set above");
        for (_, waiting) in state.pending.drain() {
            // a caller that already gave up (its timeout fired) has dropped its receiver
            let _ = waiting.send(Err(closed.clone()));
        }
    }

    fn fail(&self, generation: u64, error: JeffError) {
        let mut state = self.lock();
        self.fail_locked(&mut state, generation, error);
    }

    /// What one frame says. Decoding happens before the state lock is taken.
    fn incoming(&self, frame: Frame) -> Incoming {
        let failed = |what: String| {
            Incoming::Failure(engine_error(format!(
                "The engine socket to {} failed: {what}",
                self.path
            )))
        };
        match frame {
            Ok(body) => match decode_body(&body) {
                Ok(message) => Incoming::Message(message),
                Err(error) => failed(error.described()),
            },
            Err(FrameFailure::Closed) => Incoming::Failure(engine_error(format!(
                "The engine at {} closed the connection",
                self.path
            ))),
            Err(FrameFailure::Io(error)) => failed(error.to_string()),
            Err(FrameFailure::Oversized(error)) => failed(error.described()),
        }
    }

    /// Decodes frames up to the first that ends the connection.
    fn decode_all(&self, frames: Vec<Frame>) -> Vec<Incoming> {
        let mut decoded = Vec::with_capacity(frames.len());
        for frame in frames {
            let incoming = self.incoming(frame);
            let ends = matches!(incoming, Incoming::Failure(_));
            decoded.push(incoming);
            if ends {
                break;
            }
        }
        decoded
    }

    /// Handles every message that arrived together, in one pass under the lock, collecting the replies to hand to
    /// their callers once the lock is released. True when the connection ended.
    fn handle_locked(
        &self,
        state: &mut State,
        generation: u64,
        messages: Vec<Incoming>,
        deliveries: &mut Deliveries,
    ) -> bool {
        for incoming in messages {
            let failure = match incoming {
                Incoming::Failure(error) => error,
                Incoming::Message(message) => {
                    let id = message.get("id").and_then(Json::as_u64);
                    if id == Some(0) && message.get("ok") == Some(&Json::Bool(false)) {
                        // Not a reply to any call (this side's ids start at 1): a connection-level failure.
                        rebuild(message.get("error").unwrap_or(&Json::Null))
                    } else {
                        let waiting = id.and_then(|id| state.pending.remove(&id)); // remove: not an ordered map
                        if let Some(waiting) = waiting {
                            deliveries.push((waiting, message));
                        }
                        continue;
                    }
                }
            };
            self.fail_locked(state, generation, failure);
            return true;
        }
        false
    }

    /// Handles every frame that arrived together (see handle_locked). True when the connection ended.
    fn handle_frames(&self, generation: u64, frames: Vec<Frame>) -> bool {
        let messages = self.decode_all(frames);
        let mut deliveries = Vec::new();
        let ended = {
            let mut state = self.lock();
            self.handle_locked(&mut state, generation, messages, &mut deliveries)
        };
        deliver(deliveries);
        ended
    }

    fn dead(&self, state: &State) -> JeffError {
        let cause = state
            .closed
            .as_ref()
            .expect("a connection closed for good records why");
        engine_error(format!(
            "The engine at {} closed the connection after: {}",
            self.path,
            cause.described()
        ))
    }

    fn too_soon(&self, state: &State, now: Instant) -> JeffError {
        let (at, failure) = state.last_attempt.as_ref().expect("an attempt was made");
        let ago = py_fixed(now.duration_since(*at).as_secs_f64(), 2);
        let interval = py_float(RECONNECT_INTERVAL_MS as f64 / 1000.0);
        match failure {
            Some(failure) => engine_error(format!(
                "No engine is listening at {}: the last reconnect attempt, {ago}s ago, failed: {}. The next attempt may be made {interval}s after it",
                self.path,
                failure.message()
            )),
            None => engine_error(format!(
                "The engine at {} closed the connection {ago}s ago, just after this side reconnected: {}. The next attempt may be made {interval}s after the last",
                self.path,
                state
                    .closed
                    .as_ref()
                    .expect("a reconnect is attempted only after the connection closed")
                    .described()
            )),
        }
    }
}

type Frame = std::result::Result<Vec<u8>, FrameFailure>;

/// Moves every frame already wholly in the reader's buffer into `frames`, stopping at a failure (kept as the last).
fn take_buffered(reader: &mut FrameReader<OwnedReadHalf>, frames: &mut Vec<Frame>) {
    loop {
        match reader.buffered() {
            Ok(Some(body)) => frames.push(Ok(body)),
            Ok(None) => return,
            Err(failure) => {
                frames.push(Err(failure));
                return;
            }
        }
    }
}

async fn read_loop(shared: Arc<Shared>, generation: u64, mut reader: FrameReader<OwnedReadHalf>) {
    loop {
        let first = reader.next().await;
        let more = first.is_ok();
        let mut frames = vec![first];
        if more {
            take_buffered(&mut reader, &mut frames);
        }
        if shared.handle_frames(generation, frames) {
            return;
        }
    }
}

fn deliver(deliveries: Deliveries) {
    for (waiting, message) in deliveries {
        let _ = waiting.send(Ok(message)); // a caller whose timeout fired has stopped waiting
    }
}

/// Writes each frame whole, in the order handed over. A failed write ends the connection for every call on it.
async fn write_loop(
    shared: Arc<Shared>,
    generation: u64,
    mut write: OwnedWriteHalf,
    mut frames: mpsc::UnboundedReceiver<Vec<u8>>,
) {
    while let Some(frame) = frames.recv().await {
        if let Err(error) = write.write_all(&frame).await {
            shared.fail(
                generation,
                engine_error(format!(
                    "The engine socket to {} failed: {error}",
                    shared.path
                )),
            );
            return;
        }
    }
}

/// An Engine over the engine socket at a path: one connection, requests multiplexed by id.
pub struct SocketEngine {
    shared: Arc<Shared>,
}

impl std::fmt::Debug for SocketEngine {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SocketEngine")
            .field("path", &self.shared.path)
            .finish_non_exhaustive()
    }
}

impl SocketEngine {
    /// Connects to the engine at `path`, exchanges hellos, and reads its installed list and base once.
    pub async fn connect(path: impl AsRef<Path>, timeout_ms: u64) -> Result<SocketEngine> {
        crate::client::check_timeout_ms(timeout_ms)?;
        let path = path.as_ref().display().to_string();
        let engine = SocketEngine {
            shared: Arc::new(Shared {
                path: path.clone(),
                timeout: Duration::from_millis(timeout_ms),
                timeout_ms,
                state: Mutex::new(State {
                    connection: None,
                    closed: None,
                    closed_by_caller: false,
                    pending: HashMap::new(),
                    next_id: 1,
                    generation: 0,
                    snapshot: (-1, BTreeSet::new()),
                    last_attempt: None,
                    format: BaseFormat {
                        format: String::new(),
                        format_id: None,
                    },
                    served: (String::new(), String::new()),
                }),
                reconnecting: tokio::sync::Mutex::new(()),
                reconnects: AtomicU64::new(0),
            }),
        };
        match engine.open().await {
            Ok(()) => {}
            Err(OpenFailure::Dial(error)) => {
                return Err(engine_error(format!(
                    "No engine is listening at {path}: {error}. Start one with: {}",
                    start_command(&path)
                )));
            }
            Err(OpenFailure::Jeff(error)) => return Err(error),
        }
        // no reconnect while connecting: a connection that closes now fails the connect, naming why
        let read = async {
            let (version, installed) = engine.installed_now(None, false).await?;
            engine.merge(version, installed);
            let (format, served) = engine.base_now(false).await?;
            let mut state = engine.shared.lock();
            state.format = format;
            state.served = served;
            Ok::<(), JeffError>(())
        };
        if let Err(error) = read.await {
            engine.close().await;
            return Err(error);
        }
        Ok(engine)
    }

    /// The path of the engine's socket.
    pub fn path(&self) -> &str {
        &self.shared.path
    }

    /// The base the engine serves as adapter manifests name it (id, version), read once at connect.
    pub fn base(&self) -> (String, String) {
        self.shared.lock().served.clone()
    }

    /// The engine's adapters_version as of the last reply read from it (-1 from a reconnect until its first reply).
    pub fn adapters_version(&self) -> i64 {
        self.shared.lock().snapshot.0
    }

    /// How many times a call has reconnected to the engine after its connection closed.
    pub fn reconnects(&self) -> u64 {
        self.shared.reconnects.load(Ordering::SeqCst)
    }

    /// Opens a connection, exchanges hellos (bounded by the timeout) and makes it the connection every call uses. A
    /// failure leaves the current state as it was. Request ids are not restarted; the cached adapters_version is set
    /// below every real one (the list is kept), since a restarted engine counts from 0 again.
    async fn open(&self) -> std::result::Result<(), OpenFailure> {
        let shared = &self.shared;
        let stream = UnixStream::connect(&shared.path)
            .await
            .map_err(OpenFailure::Dial)?;
        let (read, mut write) = stream.into_split();
        let mut reader = FrameReader::new(read);
        let during = |reason: String| {
            engine_error(format!(
                "The engine at {} closed the connection during the handshake{reason}",
                shared.path
            ))
        };
        write
            .write_all(&encode_frame(&hello(None), None)?)
            .await
            .map_err(|error| during(format!(": {error}")))?;
        let first = tokio::time::timeout(shared.timeout, reader.next())
            .await
            .map_err(|_| {
                engine_error(format!(
                    "No hello from the engine at {} within {} ms",
                    shared.path, shared.timeout_ms
                ))
            })?;
        let body = match first {
            Ok(body) => body,
            Err(FrameFailure::Closed) => return Err(during(String::new()).into()),
            Err(FrameFailure::Io(error)) => return Err(during(format!(": {error}")).into()),
            Err(FrameFailure::Oversized(error)) => return Err(error.into()),
        };
        let message = decode_body(&body)?;
        if message.get("ok") == Some(&Json::Bool(false)) {
            return Err(rebuild(message.get("error").unwrap_or(&Json::Null)).into());
        }
        check_hello(&message)?;
        // frames that came in the same bytes as the hello, handled before any call can use the connection
        let mut early = Vec::new();
        take_buffered(&mut reader, &mut early);
        let early = shared.decode_all(early);
        let mut deliveries = Vec::new();
        {
            let mut state = shared.lock();
            if state.closed_by_caller {
                return Err(shared.dead(&state).into());
            }
            state.generation += 1;
            let generation = state.generation;
            // Both tasks exist before the connection is published, so failing it always stops them. Neither can
            // touch the state before this lock is released, by which time the early frames have been handled.
            let (frames, queued) = mpsc::unbounded_channel();
            state.connection = Some(Connection {
                generation,
                frames,
                reader: tokio::spawn(read_loop(Arc::clone(shared), generation, reader)),
                writer: tokio::spawn(write_loop(Arc::clone(shared), generation, write, queued)),
            });
            state.closed = None;
            state.snapshot.0 = -1;
            // an early failure ends the connection here; the next call reports why
            shared.handle_locked(&mut state, generation, early, &mut deliveries);
        }
        deliver(deliveries);
        Ok(())
    }

    /// Called by a call that found the connection closed: one attempt at a time makes one fresh connection (connect,
    /// hello, installed, health); a call that waited meanwhile then finds the connection open and uses it, or fails
    /// naming that attempt. At most one attempt per RECONNECT_INTERVAL_MS; never after close().
    async fn reconnect(&self) -> Result<()> {
        let _one_at_a_time = self.shared.reconnecting.lock().await;
        {
            let mut state = self.shared.lock();
            if state.closed_by_caller {
                return Err(self.shared.dead(&state));
            }
            if state.closed.is_none() && state.connection.is_some() {
                return Ok(());
            }
            let now = Instant::now();
            if let Some((at, _)) = &state.last_attempt
                && now.duration_since(*at) < Duration::from_millis(RECONNECT_INTERVAL_MS)
            {
                return Err(self.shared.too_soon(&state, now));
            }
            state.last_attempt = Some((now, None));
        }
        match self.attempt().await {
            Ok(()) => {
                self.shared.reconnects.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
            Err(error) => {
                let mut state = self.shared.lock();
                if let Some((_, failure)) = state.last_attempt.as_mut() {
                    *failure = Some(error.clone());
                }
                if let Some(generation) = state
                    .connection
                    .as_ref()
                    .map(|connection| connection.generation)
                {
                    self.shared
                        .fail_locked(&mut state, generation, error.clone());
                }
                Err(error)
            }
        }
    }

    async fn attempt(&self) -> Result<()> {
        let wrap = |cause: String| {
            engine_error(format!(
                "No engine is listening at {}: {cause} (reconnect attempt)",
                self.shared.path
            ))
        };
        match self.open().await {
            Ok(()) => {}
            Err(OpenFailure::Dial(error)) => return Err(wrap(error.to_string())),
            Err(OpenFailure::Jeff(error)) => return Err(wrap(error.message().to_owned())),
        }
        let (version, installed) = self
            .installed_now(None, false)
            .await
            .map_err(|error| wrap(error.message().to_owned()))?;
        let (format, served) = self
            .base_now(false)
            .await
            .map_err(|error| wrap(error.message().to_owned()))?;
        let (known_format, known_served) = {
            let state = self.shared.lock();
            (state.format.clone(), state.served.clone())
        };
        if format != known_format || served != known_served {
            // Functions bound through this engine chose their calibration for the format read at connect; the
            // restarted engine serves other weights, so nothing more is sent to it from this process.
            return Err(engine_error(format!(
                "The engine at {} restarted serving a base of format {} ({}@{}), not {} ({}@{}) as when this process \
                 connected; the functions bound here use that format's calibration, and the adapters listed are the ones \
                 trained for that base. Restart this process to serve the new base",
                self.shared.path,
                py_repr(&format.to_json()),
                served.0,
                served.1,
                py_repr(&known_format.to_json()),
                known_served.0,
                known_served.1
            )));
        }
        self.merge(version, installed);
        Ok(())
    }

    fn version_of(&self, body: &JsonMap, op: &str) -> Result<i64> {
        body.get("adapters_version").and_then(Json::as_i64).ok_or_else(|| engine_error(format!(
            "The engine at {} answered {} without an adapters_version; every reply must carry one (contract/socket.md)",
            self.shared.path, py_repr(&Json::from(op))
        )))
    }

    /// Sends one request and waits for its reply. Every reply carries the engine's adapters_version: a reply to
    /// installed/serve/reload sets the cached list from its own; any other reply with another version makes this side
    /// ask for the list (except a failed `installed`). `reconnect` false is a call made while connecting or by a
    /// reconnect attempt itself: a connection already closed again fails at once.
    fn call(
        &self,
        message: JsonMap,
        timeout: Option<Duration>,
        reconnect: bool,
    ) -> BoxFuture<'_, Result<JsonMap>> {
        Box::pin(async move {
            let shared = &self.shared;
            let timeout = timeout.unwrap_or(shared.timeout);
            let op = message
                .get("op")
                .and_then(Json::as_str)
                .expect("every request this client builds names its op")
                .to_owned();
            loop {
                let id = {
                    let mut state = shared.lock();
                    let id = state.next_id;
                    state.next_id += 1;
                    id
                };
                // encoded before anything is registered: an oversized or invalid message leaves nothing behind
                let mut with_id = message.clone();
                with_id.insert("id".into(), Json::from(id));
                let frame = encode_frame(&with_id, None)?;
                let registered = {
                    let mut state = shared.lock();
                    let open = state.closed.is_none() && state.connection.is_some();
                    match state.connection.as_ref() {
                        Some(connection) if open => {
                            // Handed over whole: from here the frame is written completely or the connection fails.
                            if connection.frames.send(frame).is_err() {
                                return Err(engine_error(format!(
                                    "The engine socket to {} failed: its writer task has stopped",
                                    shared.path
                                )));
                            }
                            let (sender, receiver) = oneshot::channel();
                            state.pending.insert(id, sender);
                            Some(receiver)
                        }
                        _ if !reconnect => {
                            return Err(
                                if state.closed_by_caller || state.last_attempt.is_none() {
                                    shared.dead(&state)
                                } else {
                                    shared.too_soon(&state, Instant::now())
                                },
                            );
                        }
                        _ => None,
                    }
                };
                let Some(receiver) = registered else {
                    self.reconnect().await?;
                    continue;
                };
                let _waiting = Waiting { shared, id };
                let reply = match tokio::time::timeout(timeout, receiver).await {
                    Err(_) => {
                        return Err(engine_error(format!(
                            "No reply from the engine at {} within {} ms",
                            shared.path,
                            timeout.as_millis()
                        )));
                    }
                    // Every pending sender is used before it is dropped (a reply, or the connection's failure), so
                    // this means the engine itself is being dropped.
                    Ok(Err(_)) => {
                        return Err(engine_error(format!(
                            "The SocketEngine for {} was dropped while this call waited for its reply",
                            shared.path
                        )));
                    }
                    Ok(Ok(reply)) => reply?,
                };
                if reply.get("ok") != Some(&Json::Bool(true)) {
                    // The engine's own error always wins; a failure reading its version or refreshing the list is a note on it.
                    let failure = reply.get("error").cloned().unwrap_or(Json::Null);
                    let mut original = rebuild(&failure);
                    let details = failure
                        .get("details")
                        .and_then(Json::as_object)
                        .cloned()
                        .unwrap_or_default();
                    match self.version_of(&details, &op) {
                        Err(error) => original.add_note(format!(
                            "Also failed to read adapters_version from this failure reply: {}",
                            error.described()
                        )),
                        Ok(version) if op != "installed" => {
                            if let Err(error) = self.follow(version, Some(timeout)).await {
                                original.add_note(format!(
                                    "Also failed to refresh the adapter list after this error: {}",
                                    error.described()
                                ));
                            }
                        }
                        Ok(_) => {}
                    }
                    return Err(original);
                }
                let result = reply
                    .get("result")
                    .and_then(Json::as_object)
                    .cloned()
                    .ok_or_else(|| {
                        engine_error(format!(
                            "The engine at {} answered {op} without a result",
                            shared.path
                        ))
                    })?;
                let version = self.version_of(&result, &op)?;
                if CARRY_ADAPTERS.contains(&op.as_str()) {
                    self.merge(version, adapters_of(&result, &shared.path)?);
                } else {
                    self.follow(version, Some(timeout)).await?;
                }
                return Ok(result);
            }
        })
    }

    async fn follow(&self, version: i64, timeout: Option<Duration>) -> Result<()> {
        if version != self.adapters_version() {
            let (version, installed) = self.installed_now(timeout, true).await?;
            self.merge(version, installed);
        }
        Ok(())
    }

    fn op(name: &str) -> JsonMap {
        JsonMap::from_iter([("op".to_owned(), Json::from(name))])
    }

    /// The engine's current (version, installed) pair; every caller routes it through merge.
    async fn installed_now(
        &self,
        timeout: Option<Duration>,
        reconnect: bool,
    ) -> Result<(i64, BTreeSet<String>)> {
        let result = self
            .call(SocketEngine::op("installed"), timeout, reconnect)
            .await?;
        let version = result
            .get("adapters_version")
            .and_then(Json::as_i64)
            .expect("call checked adapters_version");
        Ok((version, adapters_of(&result, &self.shared.path)?))
    }

    /// The one place the cached pair is written. Replies may arrive out of order, so an older version never replaces
    /// a newer one.
    fn merge(&self, version: i64, installed: BTreeSet<String>) {
        let mut state = self.shared.lock();
        if version >= state.snapshot.0 {
            state.snapshot = (version, installed);
        }
    }

    /// From the engine's health: its base format and the base it serves.
    async fn base_now(&self, reconnect: bool) -> Result<(BaseFormat, (String, String))> {
        let health = self
            .call(SocketEngine::op("health"), None, reconnect)
            .await?;
        let base = health.get("base").unwrap_or(&Json::Null);
        if base.get("format").and_then(Json::as_str).is_none() {
            return Err(engine_error(format!(
                "The engine at {} answered 'health' without base.format (got base {}); every engine reports its base's weight format (contract/socket.md)",
                self.shared.path,
                py_repr(base)
            )));
        }
        let (Some(id), Some(version)) = (
            base.get("id").and_then(Json::as_str),
            base.get("version").and_then(Json::as_str),
        ) else {
            return Err(engine_error(format!(
                "The engine at {} answered 'health' without base.id and base.version (got base {}); every engine reports the base it serves as adapter manifests name it (contract/socket.md)",
                self.shared.path,
                py_repr(base)
            )));
        };
        Ok((
            BaseFormat::from_json(base)?,
            (id.to_owned(), version.to_owned()),
        ))
    }

    /// The engine's (adapters_version, installed list) pair from one reply, which also updates the cache.
    pub async fn installed_version(&self) -> Result<(i64, BTreeSet<String>)> {
        let (version, installed) = self.installed_now(None, true).await?;
        self.merge(version, installed.clone());
        Ok((version, installed))
    }

    /// The engine's answer to `health`: whether it is up and able to serve. Raises EngineError when the
    /// engine cannot be reached or does not answer within the timeout.
    pub async fn health(&self) -> Result<JsonMap> {
        self.call(SocketEngine::op("health"), None, true).await
    }

    /// The engine's running counters (requests served, timing, queue depth and so on). Raises EngineError as
    /// [`health`](SocketEngine::health) does.
    pub async fn stats(&self) -> Result<JsonMap> {
        self.call(SocketEngine::op("stats"), None, true).await
    }

    /// As [`Engine::predict`], asking the engine to spend at most `deadline_ms` on it when given. Raises the
    /// error the engine answers with (such as NotInstalled, Busy or DeadlineExceeded), and EngineError for an engine
    /// that cannot be reached, does not answer in time or answers with a malformed result.
    pub async fn predict_with_deadline(
        &self,
        adapter_id: &str,
        rows: &[Row],
        deadline_ms: Option<u64>,
    ) -> Result<EngineResult> {
        let mut message = SocketEngine::op("predict");
        message.insert("adapter".into(), Json::from(adapter_id));
        message.insert(
            "rows".into(),
            Json::Array(rows.iter().map(Row::to_json).collect()),
        );
        if let Some(deadline) = deadline_ms {
            message.insert("deadline_ms".into(), Json::from(deadline));
        }
        let result = self.call(message, None, true).await?;
        let shape = |what: &str| {
            engine_error(format!(
                "The engine at {} answered predict with {what}",
                self.shared.path
            ))
        };
        let probabilities = result
            .get("probabilities")
            .and_then(Json::as_array)
            .ok_or_else(|| shape("no probabilities"))?
            .iter()
            .map(|row| {
                row.as_array()
                    .and_then(|values| {
                        values
                            .iter()
                            .map(Json::as_f64)
                            .collect::<Option<Vec<f64>>>()
                    })
                    .ok_or_else(|| shape("probabilities that are not numbers"))
            })
            .collect::<Result<Vec<_>>>()?;
        // optional in the reply: an engine that does not count tokens leaves it out
        let input_tokens = match result.get("input_tokens") {
            None => None,
            Some(tokens) => Some(
                tokens
                    .as_array()
                    .and_then(|values| {
                        values
                            .iter()
                            .map(Json::as_u64)
                            .collect::<Option<Vec<u64>>>()
                    })
                    .ok_or_else(|| shape("input_tokens that are not whole numbers"))?,
            ),
        };
        Ok(EngineResult {
            probabilities,
            timing: result
                .get("timing")
                .and_then(Json::as_object)
                .cloned()
                .ok_or_else(|| shape("no timing"))?,
            input_tokens,
        })
    }

    /// Final: later calls fail, and there is no reconnect.
    pub async fn close(&self) {
        let mut state = self.shared.lock();
        state.closed_by_caller = true;
        let error = engine_error(format!(
            "The SocketEngine for {} was closed by its caller",
            self.shared.path
        ));
        match state
            .connection
            .as_ref()
            .map(|connection| connection.generation)
        {
            Some(generation) => self.shared.fail_locked(&mut state, generation, error),
            None if state.closed.is_none() => state.closed = Some(error),
            None => {}
        }
    }
}

fn adapters_of(result: &JsonMap, path: &str) -> Result<BTreeSet<String>> {
    result
        .get("adapters")
        .and_then(Json::as_array)
        .and_then(|items| {
            items
                .iter()
                .map(|item| item.as_str().map(str::to_owned))
                .collect::<Option<BTreeSet<_>>>()
        })
        .ok_or_else(|| {
            engine_error(format!(
                "The engine at {path} answered without its list of adapters"
            ))
        })
}

impl Drop for SocketEngine {
    fn drop(&mut self) {
        // No runtime is needed: aborting the connection's two tasks closes the socket.
        if let Ok(mut state) = self.shared.state.lock() {
            state.closed_by_caller = true;
            if let Some(connection) = state.connection.take() {
                connection.stop();
            }
        }
    }
}

#[async_trait]
impl Engine for SocketEngine {
    fn base_format(&self) -> BaseFormat {
        self.shared.lock().format.clone()
    }

    async fn installed(&self, refresh: bool) -> Result<BTreeSet<String>> {
        if refresh {
            let (version, installed) = self.installed_now(None, true).await?;
            self.merge(version, installed);
        }
        Ok(self.shared.lock().snapshot.1.clone())
    }

    async fn predict(&self, adapter_id: &str, rows: &[Row]) -> Result<EngineResult> {
        self.predict_with_deadline(adapter_id, rows, None).await
    }

    async fn close(&self) {
        SocketEngine::close(self).await;
    }
}
