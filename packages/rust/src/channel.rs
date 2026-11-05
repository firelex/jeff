//! Remote mode over the server's low-latency channel (contract/socket.md, "The channel"; port of
//! jeff/channel_client.py and the Node SDK's channel.ts): `Jeff::remote(url, token, RemoteOptions { channel: true, .. })`.
//!
//! One WebSocket to `<url>/v1/channel` (ws:// for http://, wss:// for https://), compression off. Opening sends hello
//! and, when there is a token, auth; then every request is one binary message holding one frame with an id, and a
//! reader task hands each reply to the request waiting for that id. A writer task owns the sending half: requests reach
//! it through a queue, so callers never block each other, a caller that stops waiting never leaves half a message on
//! the socket, and closing the channel abandons a write the server stopped taking. Messages sent are at most 50 MiB
//! (64 KiB before auth), as the server allows; messages read at most 4 bytes + 64 MiB.
//!
//! A bound function is a handle on the server: bound once with `bind` (its function, context, labels and policy),
//! each call then a `call` or `call_many` carrying only the input. Dropping it queues its `release`, sent before the
//! channel's next request; queueing only takes a std Mutex, so it is safe from any thread, with or without a runtime.
//! decide, dispatch and graphs use the `decide`, `dispatch` and `graph` messages. The list of served functions is read
//! over HTTP, GET /v1/functions, as without the channel: the channel has no operation for it.
//!
//! Token: a token function is called for the token on the WebSocket handshake (as Authorization: Bearer, for a proxy
//! in front of the server), for the token sent with auth, in each refresh window, and for each GET /v1/functions; not
//! before each request. When the token is a JWT, its exp is read from its payload without verifying the signature.
//! From REFRESH_BEFORE_S seconds before exp, the function is called and `refresh` sent with the token it returns once
//! that token expires later than the channel's current one; while it does not (or the function fails, or the server
//! refuses the refresh), the function is called again every REFRESH_RETRY_S seconds until exp. Each failure is kept:
//! if exp passes without a fresh token, the channel closes with Unauthorised carrying every failure as a note. A
//! string token, or a token that is not a JWT, is never refreshed. The refresh window uses this machine's clock.
//!
//! Timing (as jeff/channel_client.py): timeout_ms bounds opening the WebSocket (TCP, TLS and the upgrade together), the
//! hello, and the closing handshake. A request has three phases, each bounded on its own:
//! - its turn to be written: at most timeout_ms waiting behind earlier messages, else DeadlineExceeded, and it is never
//!   sent;
//! - its upload: as long as it takes on a slow but working link, watched for stalls: when the socket accepts no bytes
//!   of a message for timeout_ms (the server or the network stopped taking data), the channel closes with
//!   BackendUnavailable and every waiting request fails with that error;
//! - the wait for its reply: at most timeout_ms from the moment its message was sent, else DeadlineExceeded.
//!
//! A request whose caller stops waiting before its turn is never sent.
//!
//! Logging: the crate logs no token, but at trace level tungstenite logs the whole handshake request (the
//! Authorization header with the token) and every frame's bytes (the auth and refresh tokens). Keep the `tungstenite`
//! log target below trace in an application that logs.
//!
//! An error for a failure the server sent carries no request id (there is no HTTP response) but the server
//! connection's id as details.channel_id.
//!
//! When the channel closes (a failure with id 0, the socket closing, or close()), every request waiting for a reply
//! fails with its own copy of that error (BackendUnavailable "the channel closed" when the socket closed without one),
//! and every later request fails with the same error. There is no automatic reconnect: connect again with
//! Jeff::remote.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use base64::Engine as _;
use bytes::Bytes;
use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::protocol::frame::CloseFrame;
use tokio_tungstenite::tungstenite::{Error as WsError, Message, error::ProtocolError};
use tokio_util::sync::CancellationToken;

use crate::client::MAX_TIMEOUT_MS;
use crate::contract::{SchemaName, first_problem, is_valid};
use crate::errors::{ErrorType, JeffError, Result, error_from_wire};
use crate::json::{Json, JsonMap};
use crate::protocol::{
    CHANNEL_PATH, HEADER_SIZE, MAX_BODY, MAX_FRAME, PREAUTH_MAX_BODY, check_hello, decode_body,
    encode_frame, hello, validate_message,
};
use crate::pyformat::{format_js_number, py_fixed, py_repr, py_sorted, py_str};
use crate::remote::{RestTransport, TokenSource, Transport, read_token};
use crate::tls::{Connect, Endpoint, ProxyFailure, Route, RouteConnect, Stream};

/// How long before a token's exp the refresher starts asking the token function for a fresh token, and how often it
/// asks again while the token it returns does not expire later (or the function or the refresh fails).
pub const REFRESH_BEFORE_S: u64 = 60;
/// How often, in seconds, a failed token refresh is tried again until the token expires.
pub const REFRESH_RETRY_S: u64 = 5;

/// The keepalive, as the Python SDK's websockets client keeps it: a ping every 20 s, and a pong due within 20 s of each
/// ping being sent, else the channel ends with BackendUnavailable.
const PING_INTERVAL: Duration = Duration::from_secs(20);
const PING_TIMEOUT: Duration = Duration::from_secs(20);

/// The keepalive pinned by `testing::pin_channel_keepalive` (only with the `testing` feature).
#[cfg(feature = "testing")]
static PINNED_KEEPALIVE: Mutex<Option<(Duration, Duration)>> = Mutex::new(None);

#[cfg(feature = "testing")]
pub(crate) fn pin_keepalive(keepalive: Option<(Duration, Duration)>) {
    *PINNED_KEEPALIVE
        .lock()
        .unwrap_or_else(PoisonError::into_inner) = keepalive;
}

/// The ping interval and the pong timeout a channel opened now uses.
fn keepalive() -> (Duration, Duration) {
    #[cfg(feature = "testing")]
    if let Some(pinned) = *PINNED_KEEPALIVE
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
    {
        return pinned;
    }
    (PING_INTERVAL, PING_TIMEOUT)
}

/// Sends a ping every `interval` once the last one was answered; a pong not received within `timeout` of its ping
/// being written ends the channel with BackendUnavailable naming the missing pong.
async fn keepalive_loop(shared: Arc<Shared>, interval: Duration, timeout: Duration) {
    let mut number: u64 = 0;
    loop {
        tokio::select! {
            _ = shared.stop.cancelled() => return,
            _ = tokio::time::sleep(interval) => {}
        }
        number += 1;
        let (sent, written) = oneshot::channel();
        if shared
            .outgoing
            .send(Outgoing::Ping { number, sent })
            .is_err()
        {
            return;
        }
        tokio::select! {
            _ = shared.stop.cancelled() => return,
            done = written => if done.is_err() { return }, // the writer skipped it: the channel has closed
        }
        let mut pongs = shared.pongs.subscribe();
        tokio::select! {
            _ = shared.stop.cancelled() => return,
            answered = pongs.wait_for(|latest| *latest >= number) => if answered.is_err() { return },
            _ = tokio::time::sleep(timeout) => {
                shared.shutdown(backend(format!(
                    "{}: the server sent no pong within {} s of a keepalive ping; the channel closed",
                    shared.ws_url,
                    format_js_number(timeout.as_secs_f64())
                )));
                return;
            }
        }
    }
}

/// The channel's WebSocket URL for a server URL: ws:// for http://, wss:// for https://, path /v1/channel.
pub fn channel_url(url: &str) -> Result<String> {
    for (http, ws) in [("http://", "ws://"), ("https://", "wss://")] {
        if let Some(rest) = url.strip_prefix(http) {
            return Ok(format!("{ws}{}{CHANNEL_PATH}", rest.trim_end_matches('/')));
        }
    }
    Err(JeffError::new(
        ErrorType::InvalidRequest,
        format!(
            "The server URL {} must start with http:// or https://",
            py_str(url)
        ),
    ))
}

/// The exp of a JWT (header.payload.signature), read from its payload without verifying the signature; None for a
/// token that is not a JWT (an API key, a console session token). A JWT whose payload cannot be read or has no
/// numeric exp is Unauthorised: the server would refuse it too. No message quotes the token.
pub fn token_expiry(token: &str) -> Result<Option<f64>> {
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        return Ok(None);
    }
    let decoder = base64::engine::GeneralPurpose::new(
        &base64::alphabet::URL_SAFE,
        base64::engine::GeneralPurposeConfig::new()
            .with_decode_padding_mode(base64::engine::DecodePaddingMode::Indifferent),
    );
    let payload: Json = decoder
        .decode(parts[1])
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .ok_or_else(|| {
            JeffError::new(
                ErrorType::Unauthorised,
                "The token is shaped like a JWT, but its payload is not base64-encoded JSON",
            )
        })?;
    let expires = match payload.as_object().and_then(|fields| fields.get("exp")) {
        Some(Json::Number(number)) => number.as_f64(),
        _ => None,
    };
    match expires {
        Some(exp) => Ok(Some(exp)),
        None => {
            let shown = payload
                .as_object()
                .and_then(|fields| fields.get("exp"))
                .map_or_else(|| "None".to_owned(), py_repr);
            Err(JeffError::new(
                ErrorType::Unauthorised,
                format!(
                    "The token is a JWT without a numeric exp (got {shown}); the server needs one"
                ),
            ))
        }
    }
}

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the clock is after 1970")
        .as_secs_f64()
}

fn backend(message: String) -> JeffError {
    JeffError::new(ErrorType::BackendUnavailable, message)
}

fn engine_error(message: String) -> JeffError {
    JeffError::new(ErrorType::EngineError, message)
}

type Reply = std::result::Result<JsonMap, JeffError>;

struct ChannelState {
    pending: HashMap<u64, oneshot::Sender<Reply>>,
    /// Ids of releases sent for dropped bound functions (no one waits for their replies).
    unawaited: HashSet<u64>,
    /// Ids of requests whose caller stopped waiting (DeadlineExceeded, or its future dropped) after their message was
    /// written: their late replies are dropped.
    abandoned: HashSet<u64>,
    /// Ids of messages in the writer's queue not yet started; one taken out of it here is skipped by the writer.
    queued: HashSet<u64>,
    /// Handles of dropped bound functions, released before the next request.
    releases: VecDeque<u64>,
    next_id: u64,
    closed: Option<JeffError>,
    refresh_failures: Vec<String>,
}

/// A message for the writer: its id and frame, and whom to tell when its writing starts and when it is written.
struct Queued {
    id: u64,
    frame: Vec<u8>,
    started: Option<oneshot::Sender<()>>,
    written: Option<oneshot::Sender<()>>,
}

enum Outgoing {
    Message(Queued),
    /// A keepalive ping carrying its number (8 bytes, big-endian); `sent` is told once it has been written.
    Ping {
        number: u64,
        sent: oneshot::Sender<()>,
    },
    /// Start the closing handshake (sent once the channel is closed).
    Close,
}

struct Shared {
    /// The server URL (without trailing slashes).
    url: String,
    ws_url: String,
    timeout: Duration,
    timeout_ms: u64,
    state: Mutex<ChannelState>,
    outgoing: mpsc::UnboundedSender<Outgoing>,
    /// Cancelled when the channel closes: wakes the refresher, tells the writer to abandon a write in progress and
    /// starts the reader's wait (at most timeout_ms) for the end of the closing handshake.
    stop: CancellationToken,
    /// Cancelled by the writer when it abandons a write: no closing handshake is possible, the socket ends at once.
    end_socket: CancellationToken,
    /// Cancelled when the reader has ended: the socket is done.
    socket_done: CancellationToken,
    tasks: Mutex<Vec<JoinHandle<()>>>,
    /// When the socket last accepted bytes of a message (the send-stall watch).
    progress: Arc<Progress>,
    /// The number of the latest keepalive ping the server answered with a pong.
    pongs: watch::Sender<u64>,
}

/// When a stream last accepted bytes, as milliseconds since it was opened.
struct Progress {
    opened: std::time::Instant,
    last_ms: std::sync::atomic::AtomicU64,
}

impl Progress {
    fn new() -> Progress {
        Progress {
            opened: std::time::Instant::now(),
            last_ms: std::sync::atomic::AtomicU64::new(0),
        }
    }

    fn touch(&self) {
        let now = u64::try_from(self.opened.elapsed().as_millis()).unwrap_or(u64::MAX);
        self.last_ms.store(now, std::sync::atomic::Ordering::SeqCst);
    }

    fn last(&self) -> tokio::time::Instant {
        let at = self.last_ms.load(std::sync::atomic::Ordering::SeqCst);
        tokio::time::Instant::from_std(self.opened + Duration::from_millis(at))
    }

    /// Ends once no bytes have been accepted for `timeout`.
    async fn stalled(&self, timeout: Duration) {
        loop {
            let due = self.last() + timeout;
            if tokio::time::Instant::now() >= due {
                return;
            }
            tokio::time::sleep_until(due).await;
        }
    }
}

/// The channel's stream, recording each write that the socket accepts bytes of.
struct Watched {
    inner: Stream,
    progress: Arc<Progress>,
}

impl tokio::io::AsyncRead for Watched {
    fn poll_read(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
        buffer: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::pin::Pin::new(&mut self.inner).poll_read(context, buffer)
    }
}

impl tokio::io::AsyncWrite for Watched {
    fn poll_write(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
        bytes: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        let written = std::pin::Pin::new(&mut self.inner).poll_write(context, bytes);
        if matches!(written, std::task::Poll::Ready(Ok(count)) if count > 0) {
            self.progress.touch();
        }
        written
    }

    fn poll_flush(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::pin::Pin::new(&mut self.inner).poll_flush(context)
    }

    fn poll_shutdown(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::pin::Pin::new(&mut self.inner).poll_shutdown(context)
    }
}

/// A field the socket schema requires of a message decode_body checked; named, never a panic in the reader.
fn field<'a>(message: &'a JsonMap, key: &str, ws_url: &str) -> Result<&'a Json> {
    message.get(key).ok_or_else(|| {
        engine_error(format!(
            "The channel {ws_url} sent a reply without {}: {}",
            py_str(key),
            py_repr(&Json::Object(message.clone()))
        ))
    })
}

/// The error a failure reply carries. As RestTransport checks for HTTP: a decision the error carries (LowConfidence)
/// must match contract/schemas/decision first, or it is a ContractError naming the problem.
fn wire_error(reply: &JsonMap, ws_url: &str) -> JeffError {
    match checked_wire_error(reply, ws_url) {
        Ok(made) | Err(made) => made,
    }
}

fn checked_wire_error(reply: &JsonMap, ws_url: &str) -> Result<JeffError> {
    let wire = field(reply, "error", ws_url)?;
    let text = |key: &str| {
        wire.get(key).and_then(Json::as_str).ok_or_else(|| {
            engine_error(format!(
                "The channel {ws_url} sent a failure whose error has no string {}: {}",
                py_str(key),
                py_repr(wire)
            ))
        })
    };
    let made = error_from_wire(text("type")?, text("message")?, wire.get("details"));
    if let Some(decision) = made.details().get("decision")
        && !is_valid(SchemaName::Decision, decision, None)?
    {
        return Ok(JeffError::new(
            ErrorType::ContractError,
            format!(
                "The {} error the channel {ws_url} sent carries a decision that does not match contract/schemas/decision: {}",
                made.type_name(),
                first_problem(SchemaName::Decision, decision, None)?
            ),
        ));
    }
    Ok(made)
}

/// How a WebSocket failure ended the connection. Python's websockets reports every connection lost without a close
/// frame (a reset, an end of stream, a socket error) as "no close frame received".
fn how_closed(error: &WsError) -> String {
    match error {
        WsError::Protocol(ProtocolError::ResetWithoutClosingHandshake)
        | WsError::ConnectionClosed
        | WsError::AlreadyClosed
        | WsError::Io(_) => "no close frame received".into(),
        other => other.to_string(),
    }
}

fn close_code(frame: Option<&CloseFrame>) -> String {
    match frame {
        Some(frame) if frame.reason.is_empty() => {
            format!("WebSocket close code {}", u16::from(frame.code))
        }
        Some(frame) => format!(
            "WebSocket close code {}: {}",
            u16::from(frame.code),
            frame.reason
        ),
        None => "WebSocket close code 1005".to_owned(),
    }
}

/// The checked message one WebSocket message carries (exactly one frame).
fn read_message(data: &[u8], ws_url: &str) -> Result<JsonMap> {
    if data.len() < HEADER_SIZE {
        return Err(engine_error(format!(
            "The channel {ws_url} sent a message of {} bytes, shorter than a frame header",
            data.len()
        )));
    }
    let header: [u8; HEADER_SIZE] = data[..HEADER_SIZE]
        .try_into()
        .expect("the slice is HEADER_SIZE bytes long");
    let length = u32::from_be_bytes(header) as usize;
    if length != data.len() - HEADER_SIZE {
        return Err(engine_error(format!(
            "The channel {ws_url} sent a message that is not exactly one frame: its header announces {length} bytes and the message carries {}",
            data.len() - HEADER_SIZE
        )));
    }
    decode_body(&data[HEADER_SIZE..]).map_err(|error| {
        if error.kind() == ErrorType::InvalidRequest {
            engine_error(format!(
                "The channel {ws_url} sent a frame that does not match the socket schema: {}",
                error.message()
            ))
        } else {
            error
        }
    })
}

fn text_message(ws_url: &str) -> JeffError {
    engine_error(format!(
        "The channel {ws_url} sent a text message; it carries binary frames only"
    ))
}

fn op_message(op: &str, fields: Vec<(&str, Json)>) -> JsonMap {
    let mut message = JsonMap::new();
    message.insert("op".into(), Json::from(op));
    for (key, value) in fields {
        message.insert(key.into(), value);
    }
    message
}

/// The op of a request this module built (every one names its op).
fn op_of(message: &JsonMap) -> &Json {
    message
        .get("op")
        .expect("every request this module builds names its op")
}

/// Cleans up after a caller that stops waiting without a reply (a timeout fired, or its future was dropped): a message
/// not yet started is taken out of the queue (never sent); one already written has its id moved to abandoned, so the
/// late reply is dropped instead of ending the channel.
struct Waiting<'a> {
    shared: &'a Shared,
    id: u64,
}

impl Drop for Waiting<'_> {
    fn drop(&mut self) {
        let mut state = self.shared.lock();
        let unsent = state.queued.remove(&self.id); // remove: not an ordered map
        let was_waiting = state.pending.remove(&self.id).is_some(); // remove: not an ordered map
        if was_waiting && !unsent && state.closed.is_none() {
            state.abandoned.insert(self.id);
        }
    }
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, ChannelState> {
        // Nothing panics while holding this lock; a poisoned lock still holds consistent state.
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The error the channel closed with; for a writer that has stopped without one (which closing always records
    /// first), an EngineError naming it.
    fn closed_error(&self) -> JeffError {
        self.lock().closed.clone().unwrap_or_else(|| {
            engine_error(format!(
                "The channel {} failed: its writer task has stopped",
                self.ws_url
            ))
        })
    }

    /// Ends the channel once: the first error wins; every waiting request fails with its own copy of it.
    fn shutdown(&self, mut error: JeffError) {
        let waiting: Vec<oneshot::Sender<Reply>> = {
            let mut state = self.lock();
            if state.closed.is_none() {
                for note in std::mem::take(&mut state.refresh_failures) {
                    error.add_note(note);
                }
                state.closed = Some(error);
            }
            state.unawaited.clear();
            state.abandoned.clear();
            state.queued.clear();
            state.releases.clear();
            state.pending.drain().map(|(_, sender)| sender).collect()
        };
        let closed = self.closed_error();
        for sender in waiting {
            // A send fails only for a caller that stopped waiting in the meantime: nobody to tell.
            let _ = sender.send(Err(closed.clone()));
        }
        self.stop.cancel();
        // The closing handshake: the writer sends a close frame (unless it is gone: the socket has ended already).
        let _ = self.outgoing.send(Outgoing::Close);
    }

    fn on_message(&self, data: &[u8]) {
        let message = match read_message(data, &self.ws_url) {
            Ok(message) => message,
            Err(error) => return self.shutdown(error),
        };
        let Some(ok) = message.get("ok").and_then(Json::as_bool) else {
            return self.shutdown(engine_error(format!(
                "The channel {} sent a message that is not a reply (op {}); the channel is closed",
                self.ws_url,
                py_repr(message.get("op").unwrap_or(&Json::Null))
            )));
        };
        let id = match field(&message, "id", &self.ws_url).map(Json::as_u64) {
            Ok(Some(id)) => id,
            Ok(None) => {
                return self.shutdown(engine_error(format!(
                    "The channel {} sent a reply whose id is not a whole number: {}",
                    self.ws_url,
                    py_repr(&message["id"])
                )));
            }
            Err(error) => return self.shutdown(error),
        };
        if id == 0 && !ok {
            // A failure of the whole connection (the token expired, a frame the server could not read): the server
            // closes the channel after it.
            return self.shutdown(wire_error(&message, &self.ws_url));
        }
        let (waiting, unawaited, abandoned) = {
            let mut state = self.lock();
            let waiting = state.pending.remove(&id); // remove: not an ordered map
            let unawaited = state.unawaited.remove(&id); // remove: not an ordered map
            let abandoned = state.abandoned.remove(&id); // remove: not an ordered map
            (waiting, unawaited, abandoned)
        };
        if let Some(waiting) = waiting {
            // A send fails only for a caller that stopped waiting in the meantime: its Waiting guard has run.
            let _ = waiting.send(Ok(message));
        } else if unawaited {
            if !ok {
                log::error!(
                    "Releasing the handle of a dropped bound function on {} failed: {}",
                    self.ws_url,
                    py_repr(message.get("error").unwrap_or(&Json::Null))
                );
            }
        } else if !abandoned {
            // A reply to an id this side never sent, or one already answered: the two sides disagree about what is
            // outstanding, so no later reply can be trusted to be matched right.
            self.shutdown(engine_error(format!(
                "The channel {} sent a reply for id {id}, which no request is waiting for; the channel is closed",
                self.ws_url
            )));
        }
        // An abandoned id is a late reply to a request whose caller stopped waiting: dropped.
    }

    /// The request with its id, as one frame; InvalidRequest when it is over `limit` bytes.
    fn register(&self, message: &JsonMap, limit: usize) -> Result<(u64, Vec<u8>)> {
        let id = {
            let mut state = self.lock();
            state.next_id += 1;
            state.next_id
        };
        let mut with_id = message.clone();
        with_id.insert("id".into(), Json::from(id));
        let frame = encode_frame(&with_id, None)?;
        let size = frame.len() - HEADER_SIZE;
        if size > limit {
            let suffix = if limit == PREAUTH_MAX_BODY {
                " before auth"
            } else {
                " (50 MiB)"
            };
            return Err(JeffError::new(
                ErrorType::InvalidRequest,
                format!(
                    "A {} frame of {size} bytes is over the channel's limit of {limit} bytes{suffix}",
                    py_repr(op_of(message))
                ),
            ));
        }
        Ok((id, frame))
    }

    /// Sends `release` for each handle queued by release_later, without waiting for the replies (the reader logs a
    /// failure).
    fn send_releases(&self) -> Result<()> {
        loop {
            let handle = {
                let mut state = self.lock();
                if state.closed.is_some() {
                    return Ok(()); // the handles ended with the connection
                }
                match state.releases.pop_front() {
                    Some(handle) => handle,
                    None => return Ok(()),
                }
            };
            let message = op_message("release", vec![("handle", Json::from(handle))]);
            let (id, frame) = self.register(&message, MAX_BODY)?;
            {
                let mut state = self.lock();
                if state.closed.is_some() {
                    return Ok(()); // the handles ended with the connection
                }
                state.unawaited.insert(id);
                state.queued.insert(id);
            }
            let queued = Queued {
                id,
                frame,
                started: None,
                written: None,
            };
            if self.outgoing.send(Outgoing::Message(queued)).is_err() {
                self.lock().unawaited.remove(&id); // remove: not an ordered map
                return Err(self.closed_error());
            }
        }
    }

    /// Sends one request and waits for its reply (at most timeout_ms); its result, or the error its failure names.
    async fn request(&self, message: JsonMap, limit: usize) -> Result<JsonMap> {
        self.send_releases()?;
        let (id, frame) = self.register(&message, limit)?;
        let receiver = {
            let mut state = self.lock();
            if let Some(closed) = &state.closed {
                return Err(closed.clone());
            }
            let (sender, receiver) = oneshot::channel();
            state.pending.insert(id, sender);
            state.queued.insert(id);
            receiver
        };
        let _waiting = Waiting { shared: self, id };
        let (started, started_here) = oneshot::channel();
        let (written, written_here) = oneshot::channel();
        let queued = Queued {
            id,
            frame,
            started: Some(started),
            written: Some(written),
        };
        if self.outgoing.send(Outgoing::Message(queued)).is_err() {
            return Err(self.closed_error());
        }
        // Its turn to be written, at most timeout_ms; a writer that drops it unstarted has seen the channel close.
        match tokio::time::timeout(self.timeout, started_here).await {
            Ok(Ok(())) => {}
            Ok(Err(_)) => return Err(self.closed_error()),
            Err(_) => {
                let unstarted = self.lock().queued.remove(&id); // remove: not an ordered map
                if unstarted {
                    return Err(JeffError::new(
                        ErrorType::DeadlineExceeded,
                        format!(
                            "The channel {} could not start sending {} within {} ms, as earlier messages were still being written; it was not sent",
                            self.ws_url,
                            py_repr(op_of(&message)),
                            self.timeout_ms
                        ),
                    ));
                }
                // its writing started just as the timer fired: on to the upload
            }
        }
        // Its upload: as long as it takes; the writer's stall watch closes the channel if it stops.
        if written_here.await.is_err() {
            return Err(self.closed_error());
        }
        let reply = match tokio::time::timeout(self.timeout, receiver).await {
            Err(_) => {
                return Err(JeffError::new(
                    ErrorType::DeadlineExceeded,
                    format!(
                        "The channel {} did not answer {} within {} ms; the server may still have run the request",
                        self.ws_url,
                        py_repr(op_of(&message)),
                        self.timeout_ms
                    ),
                ));
            }
            // The sender went without an answer: only the channel closing drops a waiting request.
            Ok(Err(_)) => return Err(self.closed_error()),
            Ok(Ok(reply)) => reply?,
        };
        if reply.get("ok") != Some(&Json::Bool(true)) {
            return Err(wire_error(&reply, &self.ws_url));
        }
        match reply.get("result") {
            Some(Json::Object(result)) => Ok(result.clone()),
            _ => Err(engine_error(format!(
                "The channel {} answered {} with a reply whose result is not an object: {}",
                self.ws_url,
                py_repr(op_of(&message)),
                py_repr(&Json::Object(reply))
            ))),
        }
    }

    /// A request answered as /v1/decide answers: its result checked against decide_response, holding `key`.
    async fn decide_answer(&self, message: JsonMap, key: &str) -> Result<Json> {
        let op = op_of(&message).clone();
        let document = Json::Object(self.request(message, MAX_BODY).await?);
        let valid = is_valid(SchemaName::DecideResponse, &document, None)?;
        let found = document.get(key);
        let Some(found) = found.filter(|_| valid) else {
            let problem = if valid {
                let keys = document
                    .as_object()
                    .into_iter()
                    .flat_map(|result| result.keys().cloned());
                py_repr(&Json::from(py_sorted(keys)))
            } else {
                first_problem(SchemaName::DecideResponse, &document, None)?
            };
            return Err(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "The answer of {} to {} does not match contract/schemas/decide_response with {}: {problem}",
                    self.ws_url,
                    py_repr(&op),
                    py_repr(&Json::from(key))
                ),
            ));
        };
        Ok(found.clone())
    }

    /// A list decide_response holds under `key` (the schema makes it one).
    async fn decide_list(
        &self,
        message: JsonMap,
        key: &str,
        count: usize,
        what: &str,
    ) -> Result<Vec<Json>> {
        let Json::Array(decisions) = self.decide_answer(message, key).await? else {
            return Err(JeffError::new(
                ErrorType::ContractError,
                format!("{} answered {key} that is not a list", self.ws_url),
            ));
        };
        if decisions.len() != count {
            return Err(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "{} answered {} decisions for {count} {what}",
                    self.ws_url,
                    decisions.len()
                ),
            ));
        }
        Ok(decisions)
    }

    /// Waits `seconds`; true when the channel closed meanwhile.
    async fn wait(&self, seconds: f64) -> bool {
        if self.stop.is_cancelled() {
            return true;
        }
        let duration = Duration::from_millis((seconds.max(0.0) * 1000.0).ceil() as u64);
        tokio::select! {
            _ = self.stop.cancelled() => true,
            _ = tokio::time::sleep(duration) => false,
        }
    }
}

/// Writes each queued message whole and in order. Once the channel has closed it skips what is left, abandons a write
/// in progress (ending the socket at once: no close frame can follow half a message) and otherwise sends the close
/// frame, waiting at most timeout_ms for it to be taken.
async fn write_loop(
    shared: Arc<Shared>,
    mut sink: SplitSink<WebSocketStream<Watched>, Message>,
    mut queue: mpsc::UnboundedReceiver<Outgoing>,
) {
    while let Some(item) = queue.recv().await {
        match item {
            Outgoing::Message(queued) => {
                let Queued {
                    id,
                    frame,
                    started,
                    written,
                } = queued;
                if shared.stop.is_cancelled() {
                    continue; // its request has already failed with the channel's error
                }
                // its caller stopped waiting before its turn came: never sent, so no late reply will come
                let taken = shared.lock().queued.remove(&id); // remove: not an ordered map
                if !taken {
                    continue;
                }
                if let Some(started) = started {
                    let _ = started.send(()); // a caller that gave up meanwhile has stopped listening
                }
                shared.progress.touch();
                tokio::select! {
                    sent = sink.send(Message::Binary(Bytes::from(frame))) => match sent {
                        Ok(()) => {
                            if let Some(written) = written {
                                let _ = written.send(()); // a caller that gave up meanwhile has stopped listening
                            }
                        }
                        Err(error) => {
                            shared.shutdown(backend(format!("{}: the channel closed ({})", shared.ws_url, how_closed(&error))));
                        }
                    },
                    _ = shared.progress.stalled(shared.timeout) => {
                        shared.shutdown(backend(format!(
                            "{}: the socket accepted no bytes of a frame for {} ms (the server or the network stopped taking data); the channel closed",
                            shared.ws_url, shared.timeout_ms
                        )));
                        shared.end_socket.cancel();
                        return;
                    }
                    _ = shared.stop.cancelled() => {
                        shared.end_socket.cancel();
                        return;
                    }
                }
            }
            Outgoing::Ping { number, sent } => {
                if shared.stop.is_cancelled() {
                    continue;
                }
                shared.progress.touch();
                let ping = Message::Ping(Bytes::from(number.to_be_bytes().to_vec()));
                tokio::select! {
                    done = sink.send(ping) => match done {
                        Ok(()) => {
                            let _ = sent.send(()); // the keepalive task may have ended meanwhile
                        }
                        Err(error) => {
                            shared.shutdown(backend(format!("{}: the channel closed ({})", shared.ws_url, how_closed(&error))));
                        }
                    },
                    _ = shared.progress.stalled(shared.timeout) => {
                        shared.shutdown(backend(format!(
                            "{}: the socket accepted no bytes of a frame for {} ms (the server or the network stopped taking data); the channel closed",
                            shared.ws_url, shared.timeout_ms
                        )));
                        shared.end_socket.cancel();
                        return;
                    }
                    _ = shared.stop.cancelled() => {
                        shared.end_socket.cancel();
                        return;
                    }
                }
            }
            Outgoing::Close => {
                // Sends the close frame; a server already gone fails it at once, one that takes nothing is not
                // waited for beyond timeout_ms.
                if !matches!(
                    tokio::time::timeout(shared.timeout, sink.close()).await,
                    Ok(Ok(()))
                ) {
                    shared.end_socket.cancel();
                }
                return;
            }
        }
    }
}

/// Hands each reply to its request. Once the channel has closed it reads on for at most timeout_ms (the server's close
/// frame, ending the closing handshake), then ends; the socket closes when both tasks have ended.
async fn read_loop(shared: Arc<Shared>, mut source: SplitStream<WebSocketStream<Watched>>) {
    let ws_url = shared.ws_url.clone();
    let mut deadline: Option<tokio::time::Instant> = None;
    loop {
        let next = match deadline {
            None => tokio::select! {
                next = source.next() => next,
                _ = shared.stop.cancelled() => {
                    deadline = Some(tokio::time::Instant::now() + shared.timeout);
                    continue;
                }
            },
            Some(deadline) => tokio::select! {
                next = source.next() => next,
                _ = tokio::time::sleep_until(deadline) => break,
                _ = shared.end_socket.cancelled() => break,
            },
        };
        match next {
            Some(Ok(Message::Binary(data))) => shared.on_message(&data),
            Some(Ok(Message::Text(_))) => shared.shutdown(text_message(&ws_url)),
            Some(Ok(Message::Close(frame))) => shared.shutdown(backend(format!(
                "{ws_url}: the channel closed ({})",
                close_code(frame.as_ref())
            ))),
            Some(Ok(Message::Pong(payload))) => {
                // the answer to a keepalive ping; a pong with any other payload is unsolicited and ignored
                if let Ok(number) = <[u8; 8]>::try_from(payload.as_ref()) {
                    let number = u64::from_be_bytes(number);
                    shared.pongs.send_if_modified(|latest| {
                        let newer = number > *latest;
                        if newer {
                            *latest = number;
                        }
                        newer
                    });
                }
            }
            Some(Ok(_)) => {} // pings (tungstenite answers them) and raw frames carry no reply
            Some(Err(error)) => {
                shared.shutdown(backend(format!(
                    "{ws_url}: the channel closed ({})",
                    how_closed(&error)
                )));
                break;
            }
            None => {
                shared.shutdown(backend(format!(
                    "{ws_url}: the channel closed (no close frame received)"
                )));
                break;
            }
        }
    }
    shared.socket_done.cancel();
}

/// Sends hello and reads the server's, at most timeout_ms.
async fn handshake(
    ws: &mut WebSocketStream<Watched>,
    ws_url: &str,
    timeout: Duration,
    timeout_ms: u64,
) -> Result<()> {
    let during = |how: String| {
        backend(format!(
            "{ws_url}: the channel closed during the hello ({how})"
        ))
    };
    let frame = encode_frame(&hello(None), Some("hello"))?;
    let exchange = async {
        ws.send(Message::Binary(Bytes::from(frame)))
            .await
            .map_err(|error| during(how_closed(&error)))?;
        loop {
            match ws.next().await {
                Some(Ok(Message::Binary(data))) => return Ok(data),
                Some(Ok(Message::Text(_))) => return Err(text_message(ws_url)),
                Some(Ok(Message::Close(frame))) => return Err(during(close_code(frame.as_ref()))),
                Some(Ok(_)) => {} // ping, pong and raw frames carry no hello
                Some(Err(error)) => return Err(during(how_closed(&error))),
                None => return Err(during("no close frame received".into())),
            }
        }
    };
    let data = tokio::time::timeout(timeout, exchange)
        .await
        .map_err(|_| {
            backend(format!(
                "No hello from the channel {ws_url} within {timeout_ms} ms"
            ))
        })??;
    let message = read_message(&data, ws_url)?;
    if message.get("ok") == Some(&Json::Bool(false)) {
        return Err(wire_error(&message, ws_url));
    }
    check_hello(&message)?;
    validate_message(
        &Json::Object(message),
        &format!("the hello of {ws_url}"),
        Some("hello"),
    )?;
    Ok(())
}

/// The Transport over the channel, plus the handle operations bind, call, call_many and release. GET /v1/functions
/// goes over HTTP (a RestTransport with the same URL, token and timeout).
pub struct ChannelTransport {
    shared: Arc<Shared>,
    rest: RestTransport,
}

impl std::fmt::Debug for ChannelTransport {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ChannelTransport")
            .field("ws_url", &self.shared.ws_url)
            .finish_non_exhaustive()
    }
}

impl ChannelTransport {
    /// Opens the channel: the WebSocket, hello, and auth when there is a token; a function's JWT is then refreshed.
    pub async fn open(url: &str, token: TokenSource, timeout_ms: u64) -> Result<ChannelTransport> {
        let ws_url = channel_url(url)?;
        let rest = RestTransport::new(url, token.clone(), timeout_ms)?; // also checks the URL and the timeout
        let base = rest.url().to_owned();
        let endpoint = Endpoint::parse(&base)?;
        let timeout = Duration::from_millis(timeout_ms);
        // The token also goes on the handshake as Authorization: Bearer. The Jeff server ignores it there (it checks
        // the auth message sent after hello), but a proxy in front of the server may open the WebSocket only for a
        // request that carries credentials.
        let header = read_token(&token, &base).await?;
        let cannot_reach = |reason: String| {
            backend(format!(
                "Cannot reach the Jeff server at {base} ({ws_url}): {reason}"
            ))
        };
        // as websockets (proxy=True): the environment's proxy for this server, always through a CONNECT tunnel
        let route =
            match crate::proxy::websockets_proxy(&crate::proxy::process_environment()?, &endpoint)?
            {
                None => Route::Direct,
                Some(proxy) => {
                    let host = if endpoint.host.contains(':') {
                        format!("[{}]", endpoint.host)
                    } else {
                        endpoint.host.clone()
                    };
                    let default_port = if endpoint.tls { 443 } else { 80 };
                    let host_header = if endpoint.port == default_port {
                        host.clone()
                    } else {
                        format!("{host}:{}", endpoint.port)
                    };
                    Route::Tunnel {
                        proxy,
                        target: format!("{host}:{}", endpoint.port),
                        headers: vec![("Host".into(), host_header)],
                    }
                }
            };
        let progress = Arc::new(Progress::new());
        let opening = async {
            let stream = Watched {
                inner: RouteConnect(route)
                    .connect(&endpoint)
                    .await
                    .map_err(|error| match ProxyFailure::of(&error) {
                        Some(ProxyFailure::Refused { status, .. }) => cannot_reach(format!(
                            "InvalidProxyStatus: proxy rejected connection: HTTP {status}"
                        )),
                        Some(ProxyFailure::Unreadable(_)) => cannot_reach(
                            "InvalidProxyMessage: did not receive a valid HTTP response from proxy"
                                .into(),
                        ),
                        None => cannot_reach(error.to_string()),
                    })?,
                progress: Arc::clone(&progress),
            };
            let mut request = ws_url
                .as_str()
                .into_client_request()
                .map_err(|error| cannot_reach(error.to_string()))?;
            if let Some(token) = &header {
                let value =
                    http::HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| {
                        JeffError::new(
                            ErrorType::Unauthorised,
                            format!(
                                "The token for {base} cannot be sent as an HTTP header (not shown)"
                            ),
                        )
                    })?;
                request.headers_mut().insert("authorization", value);
            }
            let config = WebSocketConfig::default()
                .max_message_size(Some(HEADER_SIZE + MAX_FRAME))
                .max_frame_size(Some(HEADER_SIZE + MAX_FRAME));
            match tokio_tungstenite::client_async_with_config(request, stream, Some(config)).await {
                Ok((ws, _response)) => Ok(ws),
                Err(WsError::Http(response)) => Err(engine_error(format!(
                    "The Jeff server at {base} refused to open the channel {ws_url}: HTTP status {}",
                    response.status().as_u16()
                ))),
                Err(error) => Err(cannot_reach(format!(
                    "the connection closed before the channel opened ({})",
                    how_closed(&error)
                ))),
            }
        };
        let mut ws = tokio::time::timeout(timeout, opening).await.map_err(|_| {
            backend(format!(
                "Cannot reach the Jeff server at {base}: opening {ws_url} took more than {timeout_ms} ms"
            ))
        })??;
        if let Err(error) = handshake(&mut ws, &ws_url, timeout, timeout_ms).await {
            // The closing handshake, at most timeout_ms; the hello's error is what the caller needs.
            let _ = tokio::time::timeout(timeout, ws.close(None)).await;
            return Err(error);
        }
        let (sink, source) = ws.split();
        let (outgoing, queue) = mpsc::unbounded_channel();
        let shared = Arc::new(Shared {
            url: base.clone(),
            ws_url,
            timeout,
            timeout_ms,
            state: Mutex::new(ChannelState {
                pending: HashMap::new(),
                unawaited: HashSet::new(),
                abandoned: HashSet::new(),
                queued: HashSet::new(),
                releases: VecDeque::new(),
                next_id: 0,
                closed: None,
                refresh_failures: Vec::new(),
            }),
            outgoing,
            stop: CancellationToken::new(),
            end_socket: CancellationToken::new(),
            socket_done: CancellationToken::new(),
            tasks: Mutex::new(Vec::new()),
            progress,
            pongs: watch::Sender::new(0),
        });
        shared.add_task(tokio::spawn(write_loop(Arc::clone(&shared), sink, queue)));
        shared.add_task(tokio::spawn(read_loop(Arc::clone(&shared), source)));
        let (interval, pong_timeout) = keepalive();
        shared.add_task(tokio::spawn(keepalive_loop(
            Arc::clone(&shared),
            interval,
            pong_timeout,
        )));
        let transport = ChannelTransport {
            shared: Arc::clone(&shared),
            rest,
        };
        match transport.authenticate(&token).await {
            Ok(Some(expires)) => {
                shared.add_task(tokio::spawn(refresh_loop(
                    Arc::clone(&shared),
                    token,
                    expires,
                )));
                Ok(transport)
            }
            Ok(None) => Ok(transport),
            Err(error) => {
                transport.close().await;
                Err(error)
            }
        }
    }

    /// Sends auth when there is a token; the token's exp when it is a function's JWT (to refresh), else None.
    async fn authenticate(&self, token: &TokenSource) -> Result<Option<f64>> {
        let Some(text) = read_token(token, &self.shared.url).await? else {
            return Ok(None);
        };
        let auth = op_message("auth", vec![("token", Json::from(text.as_str()))]);
        self.shared.request(auth, PREAUTH_MAX_BODY).await?;
        // only a token function can give a fresher token; a string token is never refreshed
        match token {
            TokenSource::Function(_) => token_expiry(&text),
            _ => Ok(None),
        }
    }

    /// The server URL (without trailing slashes), as messages name it.
    pub fn url(&self) -> &str {
        &self.shared.url
    }

    /// The channel's WebSocket URL (`ws://` or `wss://`, path `/v1/channel`).
    pub fn ws_url(&self) -> &str {
        &self.shared.ws_url
    }

    /// Binds `function` (its full id) with this context, labels (wire form) and policy (wire form); the handle's
    /// number and the version of the function's adapter the server serves.
    pub async fn bind(
        &self,
        function: &str,
        context: Option<&JsonMap>,
        labels: Option<Json>,
        policy: Json,
    ) -> Result<(u64, String)> {
        let mut message = op_message(
            "bind",
            vec![
                ("function", Json::from(function)),
                ("on_low_confidence", policy),
            ],
        );
        if let Some(context) = context {
            message.insert("context".into(), Json::Object(context.clone()));
        }
        if let Some(labels) = labels {
            message.insert("labels".into(), labels);
        }
        let result = self.shared.request(message, MAX_BODY).await?;
        let handle = match result.get("handle") {
            Some(Json::Number(number)) => number.as_u64().filter(|handle| *handle >= 1),
            _ => None,
        };
        let version = result.get("version").and_then(Json::as_str);
        match (handle, version) {
            (Some(handle), Some(version))
                if result.get("function") == Some(&Json::from(function)) =>
            {
                Ok((handle, version.to_owned()))
            }
            _ => Err(JeffError::new(
                ErrorType::ContractError,
                format!(
                    "{} answered bind {} with {}; expected a handle (a whole number from 1) for {} and its adapter's version",
                    self.shared.ws_url,
                    py_repr(&Json::from(function)),
                    py_repr(&Json::Object(result.clone())),
                    py_repr(&Json::from(function))
                ),
            )),
        }
    }

    /// The decision (contract JSON) of the bound function `handle` for `input`. Raises the error the server answers
    /// with, DeadlineExceeded when no reply comes within the timeout, BackendUnavailable when the channel ended,
    /// ContractError for a malformed answer and InvalidRequest once closed.
    pub async fn call(&self, handle: u64, input: Json) -> Result<Json> {
        let message = op_message(
            "call",
            vec![("handle", Json::from(handle)), ("input", input)],
        );
        self.shared.decide_answer(message, "decision").await
    }

    /// The decisions of the bound function `handle` for each of `inputs`, in order. Raises as
    /// [`call`](ChannelTransport::call) does, and ContractError when the count of decisions differs from the inputs'.
    pub async fn call_many(&self, handle: u64, inputs: Vec<Json>) -> Result<Vec<Json>> {
        let count = inputs.len();
        let message = op_message(
            "call_many",
            vec![
                ("handle", Json::from(handle)),
                ("inputs", Json::Array(inputs)),
            ],
        );
        self.shared
            .decide_list(message, "decisions", count, "inputs")
            .await
    }

    /// Releases the bound function `handle` on the server and waits for the reply. Raises as
    /// [`call`](ChannelTransport::call) does.
    pub async fn release(&self, handle: u64) -> Result<()> {
        let message = op_message("release", vec![("handle", Json::from(handle))]);
        self.shared.request(message, MAX_BODY).await.map(|_| ())
    }

    /// Queues a release, sent before the next request: safe from Drop on any thread, with or without a runtime (it
    /// only takes a std Mutex and sends nothing). Nothing to do once the channel is closed: its handles ended with it.
    pub(crate) fn release_later(&self, handle: u64) {
        let mut state = self.shared.lock();
        if state.closed.is_none() {
            state.releases.push_back(handle);
        }
    }

    /// Whether the channel has ended, closed by its caller or by a failure.
    pub fn closed(&self) -> bool {
        self.shared.lock().closed.is_some()
    }

    /// Ends the channel (with a closing handshake of at most timeout_ms) and the HTTP connections. Every request still
    /// waiting fails with InvalidRequest "This remote client of `<url>` is closed"; calling it again does nothing more.
    pub async fn close(&self) {
        self.shared.shutdown(JeffError::new(
            ErrorType::InvalidRequest,
            format!(
                "This remote client of {} is closed; connect again with Jeff.remote",
                self.shared.url
            ),
        ));
        Transport::close(&self.rest).await;
        // A closing handshake that has not finished within timeout_ms is cut short: the tasks are ended below.
        let _ =
            tokio::time::timeout(self.shared.timeout, self.shared.socket_done.cancelled()).await;
        self.shared.abort_tasks();
    }
}

impl Shared {
    fn add_task(&self, task: JoinHandle<()>) {
        self.tasks
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(task);
    }

    fn abort_tasks(&self) {
        let tasks = std::mem::take(&mut *self.tasks.lock().unwrap_or_else(PoisonError::into_inner));
        for task in tasks {
            task.abort();
        }
    }
}

impl Drop for ChannelTransport {
    fn drop(&mut self) {
        // No runtime is needed: cancelling and aborting only set flags; the socket closes as the tasks end.
        self.shared.stop.cancel();
        self.shared.abort_tasks();
    }
}

/// One refresh attempt: the function's token, sent with `refresh` when it expires after `expires`; its exp.
async fn refresh_once(shared: &Shared, token: &TokenSource, expires: f64) -> Result<f64> {
    let fresh = read_token(token, &shared.url).await?.ok_or_else(|| {
        JeffError::new(
            ErrorType::Unauthorised,
            "the channel's token source gave no token to refresh with",
        )
    })?;
    match token_expiry(&fresh)? {
        Some(renewed) if renewed > expires => {
            let message = op_message("refresh", vec![("token", Json::from(fresh.as_str()))]);
            shared.request(message, MAX_BODY).await?;
            Ok(renewed)
        }
        other => Err(JeffError::new(
            ErrorType::Unauthorised,
            format!(
                "the token function returned a token expiring at {}, not after the channel's current token (exp {})",
                other.map_or_else(|| "None".to_owned(), format_js_number),
                format_js_number(expires)
            ),
        )),
    }
}

/// From REFRESH_BEFORE_S seconds before each token's exp (and at least REFRESH_RETRY_S after the last successful
/// refresh, so a function minting tokens shorter-lived than REFRESH_BEFORE_S is not called in a tight loop), asks the
/// token function for a token expiring later and sends `refresh` with it; each failure is kept (and logged) and the
/// function asked again every REFRESH_RETRY_S seconds. If exp passes first, the channel closes with Unauthorised
/// carrying every failure as a note.
async fn refresh_loop(shared: Arc<Shared>, token: TokenSource, first: f64) {
    let longest_wait_s = MAX_TIMEOUT_MS as f64 / 1000.0;
    let mut expires = first;
    let mut earliest = 0.0_f64; // no refresh before this time: REFRESH_RETRY_S after the last successful one
    loop {
        let now = unix_now();
        let wait = 0.0_f64
            .max(expires - REFRESH_BEFORE_S as f64 - now)
            .max(earliest - now);
        if shared.wait(wait.min(longest_wait_s)).await {
            return;
        }
        if wait > longest_wait_s {
            continue; // longer than one Tokio timer: wait again
        }
        let renewed = loop {
            match refresh_once(&shared, &token, expires).await {
                Ok(renewed) => break renewed,
                Err(error) => {
                    if shared.stop.is_cancelled() {
                        return;
                    }
                    let failure = format!(
                        "Refreshing the channel's token (exp {}) at {} failed: {}",
                        format_js_number(expires),
                        py_fixed(unix_now(), 1),
                        error.described()
                    );
                    log::warn!("{}: {failure}", shared.ws_url);
                    let count = {
                        let mut state = shared.lock();
                        state.refresh_failures.push(failure);
                        state.refresh_failures.len()
                    };
                    let remaining = expires - unix_now();
                    if remaining <= 0.0 {
                        shared.shutdown(JeffError::new(
                            ErrorType::Unauthorised,
                            format!(
                                "The channel's token expired at {} before the token function returned one expiring later ({count} attempts failed; see the notes); the channel is closed",
                                format_js_number(expires)
                            ),
                        ));
                        return;
                    }
                    if shared.wait((REFRESH_RETRY_S as f64).min(remaining)).await {
                        return;
                    }
                }
            }
        };
        shared.lock().refresh_failures.clear();
        expires = renewed;
        earliest = unix_now() + REFRESH_RETRY_S as f64;
    }
}

#[async_trait]
impl Transport for ChannelTransport {
    async fn decide_calls(&self, calls: Vec<Json>, policy: Json) -> Result<Vec<Json>> {
        let count = calls.len();
        let message = op_message(
            "decide",
            vec![("calls", Json::Array(calls)), ("on_low_confidence", policy)],
        );
        self.shared
            .decide_list(message, "decisions", count, "calls")
            .await
    }

    async fn dispatch(&self, body: JsonMap) -> Result<Json> {
        let mut message = op_message("dispatch", Vec::new());
        message.extend(body);
        self.shared.decide_answer(message, "decision").await
    }

    async fn graph(&self, body: JsonMap) -> Result<Json> {
        let mut message = op_message("graph", Vec::new());
        message.extend(body);
        self.shared.decide_answer(message, "graph_result").await
    }

    async fn functions(&self) -> Result<JsonMap> {
        self.rest.functions().await
    }

    async fn close(&self) {
        ChannelTransport::close(self).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_keepalive_is_pythons_a_ping_every_20_s_and_a_pong_due_within_20_s() {
        // websockets' defaults, which the Python SDK's channel client keeps (ping_interval=20, ping_timeout=20)
        assert_eq!(PING_INTERVAL, Duration::from_secs(20));
        assert_eq!(PING_TIMEOUT, Duration::from_secs(20));
    }

    #[test]
    fn a_jwt_whose_exp_is_not_a_number_names_what_it_got() {
        use base64::engine::general_purpose::URL_SAFE_NO_PAD;
        let token = |payload: &str| format!("a.{}.c", URL_SAFE_NO_PAD.encode(payload));
        let message = |payload: &str| {
            token_expiry(&token(payload))
                .unwrap_err()
                .message()
                .to_owned()
        };
        assert_eq!(
            message(r#"{"exp": "soon"}"#),
            "The token is a JWT without a numeric exp (got 'soon'); the server needs one"
        );
        assert_eq!(
            message(r#"{"exp": true}"#),
            "The token is a JWT without a numeric exp (got True); the server needs one"
        );
        assert_eq!(
            message("[1]"),
            "The token is a JWT without a numeric exp (got None); the server needs one"
        );
        assert_eq!(
            token_expiry(&token(r#"{"exp": 12.5}"#)).unwrap(),
            Some(12.5)
        );
        // padded or not, as Python's urlsafe_b64decode after padding
        let padded = format!(
            "a.{}.c",
            base64::engine::general_purpose::URL_SAFE.encode(r#"{"exp": 7}"#)
        );
        assert_eq!(token_expiry(&padded).unwrap(), Some(7.0));
    }

    #[test]
    fn a_message_that_is_not_exactly_one_frame_is_an_engine_error() {
        let ws = "ws://h/v1/channel";
        assert_eq!(
            read_message(&[0, 0], ws).unwrap_err().message(),
            "The channel ws://h/v1/channel sent a message of 2 bytes, shorter than a frame header"
        );
        assert_eq!(
            read_message(&[0, 0, 0, 5, 1], ws).unwrap_err().message(),
            "The channel ws://h/v1/channel sent a message that is not exactly one frame: its header announces 5 bytes and the message carries 1"
        );
        let error = read_message(&[0, 0, 0, 1, 0xc1], ws).unwrap_err();
        assert_eq!(error.kind(), ErrorType::EngineError);
        assert!(error.message().starts_with(
            "The channel ws://h/v1/channel sent a frame that does not match the socket schema: "
        ));
    }

    fn unopened() -> Shared {
        let (outgoing, _queue) = mpsc::unbounded_channel();
        Shared {
            url: "http://h".into(),
            ws_url: "ws://h/v1/channel".into(),
            timeout: Duration::from_millis(100),
            timeout_ms: 100,
            state: Mutex::new(ChannelState {
                pending: HashMap::new(),
                unawaited: HashSet::new(),
                abandoned: HashSet::new(),
                queued: HashSet::new(),
                releases: VecDeque::new(),
                next_id: 0,
                closed: None,
                refresh_failures: Vec::new(),
            }),
            outgoing,
            stop: CancellationToken::new(),
            end_socket: CancellationToken::new(),
            socket_done: CancellationToken::new(),
            tasks: Mutex::new(Vec::new()),
            progress: Arc::new(Progress::new()),
            pongs: watch::Sender::new(0),
        }
    }

    #[test]
    fn a_frame_is_held_to_64_kib_before_auth_and_50_mib_after_as_the_server_allows() {
        let shared = unopened();
        let auth = |size: usize| op_message("auth", vec![("token", Json::from("t".repeat(size)))]);
        let (id, frame) = shared.register(&auth(65_000), PREAUTH_MAX_BODY).unwrap();
        assert_eq!(
            (id, frame.len() - HEADER_SIZE <= PREAUTH_MAX_BODY),
            (1, true)
        );
        let error = shared
            .register(&auth(PREAUTH_MAX_BODY), PREAUTH_MAX_BODY)
            .unwrap_err();
        assert_eq!(error.kind(), ErrorType::InvalidRequest);
        assert_eq!(
            error.message(),
            "A 'auth' frame of 65560 bytes is over the channel's limit of 65536 bytes before auth"
        );
        let call = op_message(
            "call",
            vec![
                ("handle", Json::from(1)),
                ("input", Json::from("x".repeat(MAX_BODY))),
            ],
        );
        assert!(
            shared
                .register(&call, MAX_BODY)
                .unwrap_err()
                .message()
                .ends_with("bytes is over the channel's limit of 52428800 bytes (50 MiB)")
        );
    }

    #[test]
    fn a_failure_whose_error_lacks_its_type_is_a_named_engine_error() {
        let reply: JsonMap =
            serde_json::from_str(r#"{"id": 3, "ok": false, "error": {"message": "m"}}"#).unwrap();
        let error = wire_error(&reply, "ws://h/v1/channel");
        assert_eq!(error.kind(), ErrorType::EngineError);
        assert_eq!(
            error.message(),
            "The channel ws://h/v1/channel sent a failure whose error has no string 'type': {'message': 'm'}"
        );
    }
}
