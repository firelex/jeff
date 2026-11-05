//! HTTP/1.1 requests to a Jeff server with precise per-phase timeouts, as httpx's Timeout(timeout_s) has them (see
//! Task 14 of the Rust SDK plan).
//!
//! Idle connections are kept for at most KEEPALIVE_EXPIRY and at most MAX_IDLE of them, and at most MAX_CONNECTIONS
//! are open at once, as httpx's default Limits (keepalive_expiry=5.0, max_keepalive_connections=20,
//! max_connections=100) has them: a request that finds every connection busy waits for one to come free, in arrival
//! order (the oldest waiting request is handed the next connection put back or the next free place, as httpcore's
//! queue does), and gives up with NoAnswer::Deadline after the timeout, as httpx's pool timeout (the same timeout_s)
//! raises PoolTimeout. Unlike
//! httpx, the request is sent and the answer's head awaited under one timer (httpx times each write and read), and
//! hyper's head limits apply: at most 100 headers and about 400 KiB (h11 has no header count limit and httpcore stops
//! at 100 KiB).

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::client::conn::http1::{SendRequest, handshake};
use hyper_util::rt::TokioIo;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use crate::errors::Result;
use crate::tls::{Connect, Endpoint, ProxyFailure, Route, RouteConnect, Stream};

pub(crate) const REQUEST_ID_HEADER: &str = "x-request-id";
/// How long a connection may sit idle in the pool and still be reused (httpx's default keepalive_expiry): one that a
/// NAT or load balancer dropped without telling either side is not written into.
pub(crate) const KEEPALIVE_EXPIRY: Duration = Duration::from_secs(5);
/// How many idle connections the pool keeps (httpx's default max_keepalive_connections).
pub(crate) const MAX_IDLE: usize = 20;
/// How many connections may be open at once, idle or busy (httpx's default max_connections).
pub(crate) const MAX_CONNECTIONS: usize = 100;

/// The pool: idle connections, how many connections are open (idle or busy), and the requests waiting for one, oldest
/// first. One lock guards it all, so a connection put back or a place freed goes to the oldest waiter, never to a
/// request that came later.
#[derive(Default)]
struct State {
    /// Idle connections, each with the moment it went idle; the most recent last.
    idle: Vec<(Instant, Connection)>,
    open: usize,
    waiters: VecDeque<oneshot::Sender<Free>>,
    closed: bool,
}

#[derive(Default)]
struct Pool {
    state: Mutex<State>,
}

impl Pool {
    /// Hands a connection put back, or a freed place, to the oldest request still waiting. With none waiting, the
    /// connection goes idle (the one idle longest is closed beyond MAX_IDLE, as httpcore does) and the place is given
    /// up. Nothing that holds a place is dropped under the lock: its drop gives the place back, which takes the lock.
    fn give(pool: &Arc<Pool>, mut free: Free) {
        let closed_now = {
            let mut state = pool.state.lock().expect("the pool lock");
            while let Some(waiter) = state.waiters.pop_front() {
                match waiter.send(free) {
                    Ok(()) => return,
                    // That request stopped waiting (its timeout, or its caller gave up): the next one gets it.
                    Err(back) => free = back,
                }
            }
            match free {
                Free::Idle(connection) if !state.closed => {
                    state.idle.push((Instant::now(), connection));
                    (state.idle.len() > MAX_IDLE).then(|| state.idle.remove(0).1) // remove: not an ordered map (a Vec)
                }
                Free::Idle(connection) => Some(connection),
                Free::Slot(mut slot) => {
                    state.open -= 1;
                    slot.held = false;
                    None
                }
            }
        };
        drop(closed_now);
    }
}

/// One open connection's place among max_connections, given back (to the oldest waiter first) when it is dropped.
struct Slot {
    pool: Arc<Pool>,
    held: bool,
}

impl Drop for Slot {
    fn drop(&mut self) {
        if self.held {
            self.held = false;
            let place = Slot {
                pool: Arc::clone(&self.pool),
                held: true,
            };
            Pool::give(&self.pool, Free::Slot(place));
        }
    }
}

/// A connection's sender with the place it holds.
struct Connection {
    sender: SendRequest<Full<Bytes>>,
    slot: Slot,
}

impl Connection {
    fn usable(&self) -> bool {
        !self.sender.is_closed() && self.sender.is_ready()
    }
}

/// What a request waiting for a connection is handed: a connection put back, or a place to open a new one in.
enum Free {
    Idle(Connection),
    Slot(Slot),
}

/// What a request found when it looked: a connection to use, a place to open one in, or its place in the queue.
enum Found {
    Ready(Connection),
    Open(Slot),
    Wait(oneshot::Receiver<Free>),
    Closed,
}

/// A server's answer, whatever its status.
pub(crate) struct HttpAnswer {
    pub(crate) status: u16,
    pub(crate) request_id: Option<String>,
    /// Each Content-Encoding header's value, in order (content_coding::decoded undoes them).
    pub(crate) content_encodings: Vec<Vec<u8>>,
    pub(crate) body: Vec<u8>,
}

/// Why a request got no answer.
#[derive(Debug)]
pub(crate) enum NoAnswer {
    /// The client was closed.
    Closed,
    /// No connection within the timeout.
    ConnectTimeout,
    /// The connection could not be opened (refused, unknown host, TLS failure).
    Connect(String),
    /// No headers, or no further body, within the timeout once the request was sent.
    Deadline,
    /// The connection failed once it was open (reset, not HTTP).
    Broken(String),
    /// The body as sent is longer than content_coding::MAX_BODY; the connection is dropped.
    TooLarge,
}

pub(crate) struct HttpClient {
    endpoint: Endpoint,
    timeout: Duration,
    pool: Arc<Pool>,
    keepalive_expiry: Duration,
    max_connections: usize,
    connections: Mutex<Vec<JoinHandle<()>>>,
    closed: CancellationToken,
    connector: Arc<dyn Connect>,
    /// The proxy every request is sent to, naming the whole URL (an http:// server behind HTTP_PROXY or ALL_PROXY).
    forward: Option<crate::proxy::ProxyServer>,
}

fn described(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        text.push_str(&format!(": {cause}"));
        source = cause.source();
    }
    text
}

impl HttpClient {
    /// A client for the server at `url`, through the proxy the environment names for it, as httpx (trust_env=True)
    /// picks it (crate::proxy).
    pub(crate) fn new(url: &str, timeout_ms: u64) -> Result<HttpClient> {
        let endpoint = Endpoint::parse(url)?;
        let proxy = crate::proxy::httpx_proxy(&crate::proxy::process_environment()?, &endpoint)?;
        let route = match proxy {
            None => Route::Direct,
            Some(proxy) if !endpoint.tls => Route::Forward(proxy),
            Some(proxy) => {
                // as httpcore: CONNECT host:port, with that as Host
                let target = format!("{}:{}", endpoint.host, endpoint.port);
                Route::Tunnel {
                    proxy,
                    headers: vec![
                        ("Host".into(), target.clone()),
                        ("Accept".into(), "*/*".into()),
                    ],
                    target,
                }
            }
        };
        let forward = match &route {
            Route::Forward(proxy) => Some(proxy.clone()),
            _ => None,
        };
        let mut client =
            HttpClient::with_connector(url, timeout_ms, Arc::new(RouteConnect(route)))?;
        client.forward = forward;
        Ok(client)
    }

    pub(crate) fn with_connector(
        url: &str,
        timeout_ms: u64,
        connector: Arc<dyn Connect>,
    ) -> Result<HttpClient> {
        Ok(HttpClient {
            endpoint: Endpoint::parse(url)?,
            timeout: Duration::from_millis(timeout_ms),
            pool: Arc::new(Pool::default()),
            keepalive_expiry: KEEPALIVE_EXPIRY,
            max_connections: MAX_CONNECTIONS,
            connections: Mutex::new(Vec::new()),
            closed: CancellationToken::new(),
            connector,
            forward: None,
        })
    }

    pub(crate) fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    /// Whether close() was called.
    pub(crate) fn is_closed(&self) -> bool {
        self.closed.is_cancelled()
    }

    async fn open(&self, slot: Slot) -> std::result::Result<Connection, NoAnswer> {
        let stream: Stream = match tokio::time::timeout(
            self.timeout,
            self.connector.connect(&self.endpoint),
        )
        .await
        {
            Err(_) => return Err(NoAnswer::ConnectTimeout),
            // as httpx: a proxy that refuses the tunnel is a ProxyError, one that does not answer HTTP a
            // RemoteProtocolError, both "the connection failed"
            Ok(Err(error)) => {
                return Err(match ProxyFailure::of(&error) {
                    Some(failure @ ProxyFailure::Refused { .. }) => {
                        NoAnswer::Broken(format!("ProxyError: {failure}"))
                    }
                    Some(failure) => NoAnswer::Broken(format!("RemoteProtocolError: {failure}")),
                    None => NoAnswer::Connect(described(&error)),
                });
            }
            Ok(Ok(stream)) => stream,
        };
        let (sender, connection) = handshake(TokioIo::new(stream))
            .await
            .map_err(|error| NoAnswer::Connect(described(&error)))?;
        // The connection's own failure reaches the request through send_request; the task only drives it.
        let task = tokio::spawn(async move {
            let _ = connection.await;
        });
        let mut connections = self.connections.lock().expect("the connections lock");
        connections.retain(|task| !task.is_finished());
        connections.push(task);
        Ok(Connection { sender, slot })
    }

    /// Looks once, under the pool lock. With no request waiting: the most recently used idle connection that can take
    /// a request, else a new place if fewer than max_connections are open, else the place of an idle connection that
    /// cannot be used. Otherwise (or with requests already waiting) a place at the back of the queue. Returns, too,
    /// the idle connections found expired or unusable, to be dropped once the lock is released.
    fn look(&self) -> (Found, Vec<Connection>) {
        let mut state = self.pool.state.lock().expect("the pool lock");
        let mut stale = Vec::new();
        if state.closed {
            return (Found::Closed, stale);
        }
        if state.waiters.is_empty() {
            // Expired connections are closed, as httpx does before it picks one.
            let idle = std::mem::take(&mut state.idle);
            for (since, connection) in idle {
                if since.elapsed() <= self.keepalive_expiry {
                    state.idle.push((since, connection));
                } else {
                    stale.push(connection);
                }
            }
            while let Some((_, connection)) = state.idle.pop() {
                if connection.usable() {
                    return (Found::Ready(connection), stale);
                }
                stale.push(connection);
            }
            if state.open < self.max_connections {
                state.open += 1;
                let slot = Slot {
                    pool: Arc::clone(&self.pool),
                    held: true,
                };
                return (Found::Open(slot), stale);
            }
            if let Some(connection) = stale.pop() {
                return (Found::Open(connection.slot), stale);
            }
        }
        let (waiter, handed) = oneshot::channel();
        state.waiters.push_back(waiter);
        (Found::Wait(handed), stale)
    }

    /// An idle connection, or a new one; with max_connections open and busy, waits (at most the timeout) in arrival
    /// order for one to come free, as a request waits in httpx's pool.
    async fn connection(&self) -> std::result::Result<Connection, NoAnswer> {
        let (found, stale) = self.look();
        drop(stale);
        let free = match found {
            Found::Closed => return Err(NoAnswer::Closed),
            Found::Ready(connection) => return Ok(connection),
            Found::Open(slot) => Free::Slot(slot),
            Found::Wait(handed) => match tokio::time::timeout(self.timeout, handed).await {
                Err(_) => return Err(NoAnswer::Deadline),
                // The queue was emptied by close().
                Ok(Err(_)) => return Err(NoAnswer::Closed),
                Ok(Ok(free)) => free,
            },
        };
        match free {
            Free::Idle(connection) if connection.usable() => Ok(connection),
            // A connection put back that can no longer be used: its place is used for a new one.
            Free::Idle(connection) => self.open(connection.slot).await,
            Free::Slot(slot) => self.open(slot).await,
        }
    }

    async fn exchange(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, String)],
        body: Option<Vec<u8>>,
    ) -> std::result::Result<HttpAnswer, NoAnswer> {
        let target = match &self.forward {
            // through a proxy, the request names the whole URL (absolute form)
            Some(_) => format!(
                "http://{}{}{path}",
                self.endpoint.authority, self.endpoint.base_path
            ),
            None => format!("{}{path}", self.endpoint.base_path),
        };
        let mut request = http::Request::builder()
            .method(method)
            .uri(target)
            .header("host", &self.endpoint.authority);
        if let Some(authorization) = self
            .forward
            .as_ref()
            .and_then(|proxy| proxy.authorization.as_ref())
        {
            request = request.header("proxy-authorization", authorization);
        }
        for (name, value) in headers {
            request = request.header(*name, value);
        }
        let request = request
            .body(Full::new(Bytes::from(body.unwrap_or_default())))
            .map_err(|error| NoAnswer::Broken(described(&error)))?;
        let mut connection = self.connection().await?;
        let response =
            match tokio::time::timeout(self.timeout, connection.sender.send_request(request)).await
            {
                Err(_) => return Err(NoAnswer::Deadline),
                Ok(Err(error)) => return Err(NoAnswer::Broken(described(&error))),
                Ok(Ok(response)) => response,
            };
        let status = response.status().as_u16();
        let request_id = response
            .headers()
            .get(REQUEST_ID_HEADER)
            // Each byte as one character (Latin-1), as httpx decodes a header that is not ASCII.
            .map(|value| {
                value
                    .as_bytes()
                    .iter()
                    .map(|&byte| char::from(byte))
                    .collect()
            });
        // hyper closes the connection itself after `connection: close` (and is_closed then says so); checking here too
        // keeps such a connection out of the pool at once. An answer framed by both content-length and chunked
        // transfer-encoding must not be followed by another on the same connection (RFC 9112, section 6.3).
        let headers = response.headers();
        let asks_to_close = headers.get_all("connection").iter().any(|value| {
            value
                .as_bytes()
                .split(|byte| *byte == b',')
                .any(|token| token.trim_ascii().eq_ignore_ascii_case(b"close"))
        });
        let framed_twice =
            headers.contains_key("content-length") && headers.contains_key("transfer-encoding");
        let keep_alive = !asks_to_close && !framed_twice;
        let content_encodings = headers
            .get_all("content-encoding")
            .iter()
            .map(|value| value.as_bytes().to_vec())
            .collect();
        let mut incoming = response.into_body();
        let mut collected = Vec::new();
        loop {
            match tokio::time::timeout(self.timeout, incoming.frame()).await {
                Err(_) => return Err(NoAnswer::Deadline),
                Ok(None) => break,
                Ok(Some(Err(error))) => return Err(NoAnswer::Broken(described(&error))),
                Ok(Some(Ok(frame))) => {
                    if let Some(data) = frame.data_ref() {
                        // checked before the frame is kept, so no body past the limit is ever held (whatever its
                        // framing: Content-Length, chunked, or none, ended by the connection)
                        if collected.len() + data.len() > crate::content_coding::MAX_BODY {
                            return Err(NoAnswer::TooLarge);
                        }
                        collected.extend_from_slice(data);
                    }
                }
            }
        }
        if keep_alive {
            // To the oldest waiting request, else idle; closed under the pool lock once close() has run.
            Pool::give(&self.pool, Free::Idle(connection));
        }
        Ok(HttpAnswer {
            status,
            request_id,
            content_encodings,
            body: collected,
        })
    }

    /// One request and its whole answer. `path` starts with `/` and is put after the URL's own path.
    pub(crate) async fn request(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, String)],
        body: Option<Vec<u8>>,
    ) -> std::result::Result<HttpAnswer, NoAnswer> {
        if self.closed.is_cancelled() {
            return Err(NoAnswer::Closed);
        }
        tokio::select! {
            biased;
            _ = self.closed.cancelled() => Err(NoAnswer::Closed),
            outcome = self.exchange(method, path, headers, body) => outcome,
        }
    }

    /// Ends every request in flight (each gets NoAnswer::Closed) and every connection; later requests are refused.
    pub(crate) fn close(&self) {
        self.closed.cancel();
        let (idle, waiters) = {
            let mut state = self.pool.state.lock().expect("the pool lock");
            state.closed = true;
            (
                std::mem::take(&mut state.idle),
                std::mem::take(&mut state.waiters),
            )
        };
        drop((idle, waiters));
        for task in self
            .connections
            .lock()
            .expect("the connections lock")
            .drain(..)
        {
            task.abort();
        }
    }
}

impl Drop for HttpClient {
    fn drop(&mut self) {
        self.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::escalation::BoxFuture;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Instant;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    /// A server that reads each request head and answers it with `answer(path)`: None never answers.
    async fn server(
        answer: fn(&str) -> Option<Vec<u8>>,
    ) -> (String, Arc<AtomicUsize>, Arc<std::sync::Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let accepted = Arc::new(AtomicUsize::new(0));
        let paths: Arc<std::sync::Mutex<Vec<String>>> = Default::default();
        let (count, seen) = (Arc::clone(&accepted), Arc::clone(&paths));
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                count.fetch_add(1, Ordering::SeqCst);
                let seen = Arc::clone(&seen);
                tokio::spawn(async move {
                    let mut buffer = Vec::new();
                    loop {
                        let mut chunk = [0u8; 4096];
                        let Ok(read) = stream.read(&mut chunk).await else {
                            return;
                        };
                        if read == 0 {
                            return;
                        }
                        buffer.extend_from_slice(&chunk[..read]);
                        while let Some(end) =
                            buffer.windows(4).position(|window| window == b"\r\n\r\n")
                        {
                            let head = String::from_utf8_lossy(&buffer[..end]).to_string();
                            buffer.drain(..end + 4);
                            let path = head.split(' ').nth(1).unwrap_or("").to_owned();
                            seen.lock().unwrap().push(path.clone());
                            match answer(&path) {
                                Some(bytes) => {
                                    if stream.write_all(&bytes).await.is_err() {
                                        return;
                                    }
                                }
                                None => std::future::pending::<()>().await,
                            }
                        }
                    }
                });
            }
        });
        (url, accepted, paths)
    }

    fn ok(_: &str) -> Option<Vec<u8>> {
        Some(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nx-request-id: rid-1\r\n\r\n{}".to_vec())
    }

    struct Delayed(Option<Duration>);

    impl Connect for Delayed {
        fn connect(&self, endpoint: &Endpoint) -> BoxFuture<'_, std::io::Result<Stream>> {
            let address = format!("{}:{}", endpoint.host, endpoint.port);
            let delay = self.0;
            Box::pin(async move {
                match delay {
                    None => std::future::pending().await,
                    Some(delay) => {
                        tokio::time::sleep(delay).await;
                        Ok(Stream::Left(tokio::net::TcpStream::connect(address).await?))
                    }
                }
            })
        }
    }

    #[test]
    fn a_server_url_must_be_http_or_https_and_keeps_its_base_path() {
        assert_eq!(
            Endpoint::parse("ftp://x").unwrap_err().message(),
            "The server URL 'ftp://x' must start with http:// or https://"
        );
        let endpoint = Endpoint::parse("http://h:1/jeff/").unwrap();
        assert_eq!(
            (
                endpoint.host.as_str(),
                endpoint.port,
                endpoint.base_path.as_str(),
                endpoint.url.as_str()
            ),
            ("h", 1, "/jeff", "http://h:1/jeff")
        );
        let secure = Endpoint::parse("https://api.example.com").unwrap();
        assert_eq!(
            (secure.tls, secure.port, secure.base_path.as_str()),
            (true, 443, "")
        );
    }

    #[tokio::test]
    async fn an_answer_comes_back_with_its_status_body_and_request_id_and_the_connection_is_reused()
    {
        let (url, accepted, _) = server(ok).await;
        let client = HttpClient::new(&url, 2000).unwrap();
        for _ in 0..2 {
            let answer = client
                .request("GET", "/v1/functions", &[], None)
                .await
                .unwrap_or_else(|_| panic!("an answer"));
            assert_eq!(
                (
                    answer.status,
                    answer.request_id.as_deref(),
                    answer.body.as_slice()
                ),
                (200, Some("rid-1"), &b"{}"[..])
            );
        }
        assert_eq!(accepted.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn connecting_is_timed_on_its_own_and_promptly() {
        let client =
            HttpClient::with_connector("http://127.0.0.1:9", 100, Arc::new(Delayed(None))).unwrap();
        let started = Instant::now();
        assert!(matches!(
            client.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::ConnectTimeout)
        ));
        let elapsed = started.elapsed().as_millis();
        assert!((90..250).contains(&elapsed), "{elapsed} ms");
    }

    #[tokio::test]
    async fn a_slow_connect_followed_by_a_fast_answer_never_adds_up_to_a_timeout() {
        let (url, _, _) = server(ok).await;
        let client = HttpClient::with_connector(
            &url,
            200,
            Arc::new(Delayed(Some(Duration::from_millis(150)))),
        )
        .unwrap();
        assert!(
            client
                .request("GET", "/v1/functions", &[], None)
                .await
                .is_ok()
        );
    }

    #[tokio::test]
    async fn a_server_that_sends_no_headers_in_time_misses_the_deadline_promptly() {
        let (url, _, _) = server(|_| None).await;
        let client = HttpClient::new(&url, 200).unwrap();
        let started = Instant::now();
        assert!(matches!(
            client.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::Deadline)
        ));
        let elapsed = started.elapsed().as_millis();
        assert!((190..400).contains(&elapsed), "{elapsed} ms");
    }

    #[tokio::test]
    async fn an_answer_whose_body_stops_arriving_misses_the_deadline_promptly() {
        let (url, _, _) = server(|_| {
            Some(b"HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n{\"base_format\":".to_vec())
        })
        .await;
        let client = HttpClient::new(&url, 300).unwrap();
        let started = Instant::now();
        assert!(matches!(
            client.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::Deadline)
        ));
        assert!(started.elapsed().as_millis() < 500);
    }

    #[tokio::test]
    async fn a_redirect_is_an_answer_like_any_other_and_is_never_followed() {
        let (url, _, paths) = server(|_| {
            Some(
                b"HTTP/1.1 302 Found\r\nlocation: /never\r\ncontent-length: 5\r\n\r\nmoved"
                    .to_vec(),
            )
        })
        .await;
        let client = HttpClient::new(&url, 200).unwrap();
        let answer = client
            .request("GET", "/v1/functions", &[], None)
            .await
            .unwrap_or_else(|_| panic!("an answer"));
        assert_eq!(
            (answer.status, answer.body.as_slice()),
            (302, &b"moved"[..])
        );
        assert_eq!(*paths.lock().unwrap(), ["/v1/functions"]);
    }

    #[tokio::test]
    async fn an_answer_that_is_not_http_is_a_broken_connection_and_an_unreachable_server_cannot_be_reached()
     {
        let (url, _, _) = server(|_| Some(b"this is not HTTP\r\n\r\n".to_vec())).await;
        let client = HttpClient::new(&url, 2000).unwrap();
        assert!(matches!(
            client.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::Broken(_))
        ));
        let nowhere = HttpClient::new("http://127.0.0.1:9", 2000).unwrap();
        assert!(matches!(
            nowhere.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::Connect(_))
        ));
    }

    #[tokio::test]
    async fn closing_ends_a_request_in_flight_at_once() {
        let (url, _, _) = server(|_| None).await;
        let client = Arc::new(HttpClient::new(&url, 5000).unwrap());
        let waiting = {
            let client = Arc::clone(&client);
            tokio::spawn(async move {
                client
                    .request("GET", "/v1/functions", &[], None)
                    .await
                    .map(|_| ())
            })
        };
        tokio::time::sleep(Duration::from_millis(50)).await;
        let started = Instant::now();
        client.close();
        assert!(matches!(waiting.await.unwrap(), Err(NoAnswer::Closed)));
        assert!(started.elapsed().as_millis() < 100);
        assert!(matches!(
            client.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::Closed)
        ));
    }

    /// A server that runs `conversation` on each connection it accepts, after reading the first request head.
    async fn scripted<F, Fut>(conversation: F) -> (String, Arc<AtomicUsize>)
    where
        F: Fn(tokio::net::TcpStream) -> Fut + Send + Sync + 'static,
        Fut: std::future::Future<Output = ()> + Send + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let accepted = Arc::new(AtomicUsize::new(0));
        let count = Arc::clone(&accepted);
        let conversation = Arc::new(conversation);
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                count.fetch_add(1, Ordering::SeqCst);
                let conversation = Arc::clone(&conversation);
                tokio::spawn(async move {
                    let mut buffer = Vec::new();
                    while !buffer.windows(4).any(|window| window == b"\r\n\r\n") {
                        let mut chunk = [0u8; 4096];
                        match stream.read(&mut chunk).await {
                            Ok(0) | Err(_) => return,
                            Ok(read) => buffer.extend_from_slice(&chunk[..read]),
                        }
                    }
                    conversation(stream).await;
                });
            }
        });
        (url, accepted)
    }

    #[tokio::test]
    async fn a_server_that_closes_without_answering_is_a_broken_connection() {
        let (url, _) = scripted(|stream| async move { drop(stream) }).await;
        let client = HttpClient::new(&url, 2000).unwrap();
        assert!(matches!(
            client.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::Broken(_))
        ));
    }

    #[tokio::test]
    async fn an_answer_whose_headers_are_too_large_is_a_broken_connection() {
        let (url, _) = scripted(|mut stream| async move {
            let mut head = b"HTTP/1.1 200 OK\r\nx-huge: ".to_vec();
            head.extend(std::iter::repeat_n(b'a', 1 << 20));
            head.extend_from_slice(b"\r\ncontent-length: 0\r\n\r\n");
            let _ = stream.write_all(&head).await;
            std::future::pending::<()>().await;
        })
        .await;
        let client = HttpClient::new(&url, 2000).unwrap();
        let started = Instant::now();
        match client.request("GET", "/v1/functions", &[], None).await {
            Err(NoAnswer::Broken(text)) => assert!(text.contains("too large"), "{text}"),
            _ => panic!("a broken connection"),
        }
        assert!(started.elapsed().as_millis() < 1000);
    }

    #[tokio::test]
    async fn a_chunked_body_whose_chunks_each_come_in_time_is_read_whole_however_long_it_takes() {
        let (url, _) = scripted(|mut stream| async move {
            let _ = stream
                .write_all(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n")
                .await;
            for part in ["{\"a\"", ":1", "}"] {
                tokio::time::sleep(Duration::from_millis(120)).await;
                let _ = stream
                    .write_all(format!("{:x}\r\n{part}\r\n", part.len()).as_bytes())
                    .await;
            }
            let _ = stream.write_all(b"0\r\n\r\n").await;
            std::future::pending::<()>().await;
        })
        .await;
        let client = HttpClient::new(&url, 200).unwrap();
        let started = Instant::now();
        let answer = client
            .request("GET", "/v1/functions", &[], None)
            .await
            .unwrap_or_else(|_| panic!("an answer"));
        assert_eq!(
            (answer.status, answer.request_id, answer.body.as_slice()),
            (200, None, &b"{\"a\":1}"[..])
        );
        assert!(started.elapsed().as_millis() >= 300);
    }

    #[tokio::test]
    async fn an_answer_that_asks_to_close_the_connection_is_not_reused() {
        let (url, accepted, _) = server(|_| {
            Some(b"HTTP/1.1 200 OK\r\nconnection: close\r\ncontent-length: 2\r\n\r\n{}".to_vec())
        })
        .await;
        let client = HttpClient::new(&url, 2000).unwrap();
        for _ in 0..2 {
            assert!(
                client
                    .request("GET", "/v1/functions", &[], None)
                    .await
                    .is_ok()
            );
        }
        assert_eq!(accepted.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn a_pooled_connection_the_server_has_closed_is_dropped_and_a_new_one_opened() {
        let (url, accepted) = scripted(|mut stream| async move {
            let _ = stream
                .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}")
                .await;
            drop(stream);
        })
        .await;
        let client = HttpClient::new(&url, 2000).unwrap();
        assert!(
            client
                .request("GET", "/v1/functions", &[], None)
                .await
                .is_ok()
        );
        // Wait until the pooled connection has seen the server close it, rather than for a guessed time.
        let waited = Instant::now();
        while !client
            .pool
            .state
            .lock()
            .unwrap()
            .idle
            .iter()
            .all(|(_, connection)| connection.sender.is_closed())
        {
            assert!(
                waited.elapsed() < Duration::from_secs(2),
                "the pooled connection never saw the close"
            );
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        assert!(
            client
                .request("GET", "/v1/functions", &[], None)
                .await
                .is_ok()
        );
        assert_eq!(accepted.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn every_request_path_goes_after_the_server_urls_own_path() {
        let (url, _, paths) = server(ok).await;
        let client = HttpClient::new(&format!("{url}/jeff/"), 2000).unwrap();
        assert!(
            client
                .request("GET", "/v1/functions", &[], None)
                .await
                .is_ok()
        );
        assert_eq!(*paths.lock().unwrap(), ["/jeff/v1/functions"]);
    }

    #[test]
    fn a_server_url_with_a_bad_port_no_host_user_info_or_a_query_is_refused() {
        let refusal = |url: &str| Endpoint::parse(url).unwrap_err().message().to_owned();
        assert_eq!(
            refusal("http://h:99999"),
            "The server URL 'http://h:99999' has an invalid port '99999'"
        );
        assert_eq!(
            refusal("http://h:65536/jeff"),
            "The server URL 'http://h:65536/jeff' has an invalid port '65536'"
        );
        assert!(
            refusal("http://h:abc").starts_with("The server URL 'http://h:abc' "),
            "{}",
            refusal("http://h:abc")
        );
        assert_eq!(refusal("http://"), "The server URL 'http://' has no host");
        assert_eq!(
            refusal("http://user:pw@h:1"),
            "The server URL 'http://user:pw@h:1' must not carry a user name or password; pass the token instead"
        );
        assert_eq!(
            refusal("http://h/x?q=1"),
            "The server URL 'http://h/x?q=1' must not have a query (the part after '?')"
        );
        let empty_port = Endpoint::parse("http://h:/").unwrap();
        assert_eq!((empty_port.host.as_str(), empty_port.port), ("h", 80));
        let ipv6 = Endpoint::parse("https://[::1]:8443/jeff").unwrap();
        assert_eq!(
            (ipv6.host.as_str(), ipv6.port, ipv6.authority.as_str()),
            ("::1", 8443, "[::1]:8443")
        );
    }

    #[tokio::test]
    async fn a_connection_idle_longer_than_the_keepalive_expiry_is_not_reused() {
        assert_eq!(KEEPALIVE_EXPIRY, Duration::from_secs(5));
        let (url, accepted, _) = server(ok).await;
        let mut client = HttpClient::new(&url, 2000).unwrap();
        client.keepalive_expiry = Duration::from_millis(100);
        for pause in [0, 30, 150] {
            tokio::time::sleep(Duration::from_millis(pause)).await;
            assert!(
                client
                    .request("GET", "/v1/functions", &[], None)
                    .await
                    .is_ok()
            );
        }
        // The second request reused the first connection (idle 30 ms); the third found it expired (idle 150 ms).
        assert_eq!(accepted.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn an_answer_framed_by_both_a_length_and_chunks_is_read_and_its_connection_not_reused() {
        let (url, accepted, _) = server(|_| {
            Some(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\ntransfer-encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n".to_vec())
        })
        .await;
        let client = HttpClient::new(&url, 2000).unwrap();
        for _ in 0..2 {
            let answer = client
                .request("GET", "/v1/functions", &[], None)
                .await
                .unwrap_or_else(|_| panic!("an answer"));
            assert_eq!(answer.body.as_slice(), &b"{}"[..]);
        }
        assert_eq!(accepted.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn an_answer_whose_connection_header_lists_close_among_others_is_not_reused() {
        let (url, accepted, _) = server(|_| {
            Some(
                b"HTTP/1.1 200 OK\r\nconnection: keep-alive, Close\r\ncontent-length: 2\r\n\r\n{}"
                    .to_vec(),
            )
        })
        .await;
        let client = HttpClient::new(&url, 2000).unwrap();
        for _ in 0..2 {
            assert!(
                client
                    .request("GET", "/v1/functions", &[], None)
                    .await
                    .is_ok()
            );
        }
        assert_eq!(accepted.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn a_request_that_cannot_be_built_fails_before_any_connection_is_opened() {
        let client =
            HttpClient::with_connector("http://127.0.0.1:9", 2000, Arc::new(Delayed(None)))
                .unwrap();
        let started = Instant::now();
        let outcome = client
            .request(
                "GET",
                "/v1/functions",
                &[("x-bad", "a\nb".to_owned())],
                None,
            )
            .await;
        assert!(
            matches!(outcome, Err(NoAnswer::Broken(_))),
            "{:?}",
            outcome.map(|_| ())
        );
        assert!(started.elapsed().as_millis() < 100);
    }

    /// A server whose every connection answers each request `delay` after reading it; counts the connections open at
    /// once (the most seen) and in all.
    async fn slow_server(delay: Duration) -> (String, Arc<AtomicUsize>, Arc<AtomicUsize>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let (open, most) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)));
        let (open_now, most_seen) = (Arc::clone(&open), Arc::clone(&most));
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                let (open, most) = (Arc::clone(&open_now), Arc::clone(&most_seen));
                most.fetch_max(open.fetch_add(1, Ordering::SeqCst) + 1, Ordering::SeqCst);
                tokio::spawn(async move {
                    let mut buffer = Vec::new();
                    loop {
                        let mut chunk = [0u8; 4096];
                        match stream.read(&mut chunk).await {
                            Ok(0) | Err(_) => break,
                            Ok(read) => buffer.extend_from_slice(&chunk[..read]),
                        }
                        while let Some(end) =
                            buffer.windows(4).position(|window| window == b"\r\n\r\n")
                        {
                            buffer.drain(..end + 4);
                            tokio::time::sleep(delay).await;
                            let _ = stream
                                .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}")
                                .await;
                        }
                    }
                    open.fetch_sub(1, Ordering::SeqCst);
                });
            }
        });
        (url, open, most)
    }

    #[tokio::test]
    async fn at_most_max_connections_are_open_and_a_request_waits_for_one_to_come_free() {
        assert_eq!(MAX_CONNECTIONS, 100);
        let (url, _, most) = slow_server(Duration::from_millis(100)).await;
        let mut client = HttpClient::new(&url, 2000).unwrap();
        client.max_connections = 2;
        let client = Arc::new(client);
        let started = Instant::now();
        let requests = (0..5).map(|_| {
            let client = Arc::clone(&client);
            tokio::spawn(async move {
                client
                    .request("GET", "/v1/functions", &[], None)
                    .await
                    .is_ok()
            })
        });
        let outcomes = futures_util::future::join_all(requests).await;
        assert!(outcomes.into_iter().all(|outcome| outcome.unwrap()));
        // Five requests of 100 ms over two connections take three rounds; never more than two were open.
        let elapsed = started.elapsed().as_millis();
        assert!((290..600).contains(&elapsed), "{elapsed} ms");
        assert_eq!(most.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn a_request_that_waits_longer_than_the_timeout_for_a_connection_misses_the_deadline() {
        // The one connection stays busy: its answer's body keeps coming, a byte every 50 ms, without end.
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut chunk = [0u8; 4096];
                    let _ = stream.read(&mut chunk).await;
                    let _ = stream
                        .write_all(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n")
                        .await;
                    while stream.write_all(b"1\r\nx\r\n").await.is_ok() {
                        tokio::time::sleep(Duration::from_millis(50)).await;
                    }
                });
            }
        });
        let mut client = HttpClient::new(&url, 200).unwrap();
        client.max_connections = 1;
        let client = Arc::new(client);
        let busy = {
            let client = Arc::clone(&client);
            tokio::spawn(async move { client.request("GET", "/v1/functions", &[], None).await })
        };
        tokio::time::sleep(Duration::from_millis(50)).await;
        let started = Instant::now();
        assert!(matches!(
            client.request("GET", "/v1/functions", &[], None).await,
            Err(NoAnswer::Deadline)
        ));
        let elapsed = started.elapsed().as_millis();
        assert!((190..350).contains(&elapsed), "{elapsed} ms");
        assert!(!busy.is_finished());
        client.close();
        assert!(matches!(busy.await.unwrap(), Err(NoAnswer::Closed)));
    }

    #[tokio::test]
    async fn a_connection_that_failed_frees_its_place_for_the_next_request() {
        let (url, accepted, _) = server(|path| {
            (path == "/ok").then(|| b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}".to_vec())
        })
        .await;
        let mut client = HttpClient::new(&url, 100).unwrap();
        client.max_connections = 1;
        assert!(matches!(
            client.request("GET", "/silent", &[], None).await,
            Err(NoAnswer::Deadline)
        ));
        assert!(client.request("GET", "/ok", &[], None).await.is_ok());
        assert_eq!(accepted.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn closing_ends_a_request_waiting_for_a_connection() {
        let (url, _, _) = server(|_| None).await;
        let mut client = HttpClient::new(&url, 5000).unwrap();
        client.max_connections = 1;
        let client = Arc::new(client);
        let mut pending = Vec::new();
        for _ in 0..2 {
            let client = Arc::clone(&client);
            pending.push(tokio::spawn(async move {
                client.request("GET", "/v1/functions", &[], None).await
            }));
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
        client.close();
        assert!(client.is_closed());
        for request in pending {
            assert!(matches!(request.await.unwrap(), Err(NoAnswer::Closed)));
        }
    }

    /// Workers each sending requests back to back for `run`, over a client capped at `cap` connections to a server
    /// answering each request after `delay`: the requests that missed the deadline, and each worker's successes.
    async fn closed_loop(
        cap: usize,
        workers: usize,
        delay: Duration,
        timeout_ms: u64,
        run: Duration,
    ) -> (usize, Vec<usize>) {
        let (url, _, most) = slow_server(delay).await;
        let mut client = HttpClient::new(&url, timeout_ms).unwrap();
        client.max_connections = cap;
        let client = Arc::new(client);
        let missed = Arc::new(AtomicUsize::new(0));
        let until = Instant::now() + run;
        let tasks = (0..workers).map(|_| {
            let (client, missed) = (Arc::clone(&client), Arc::clone(&missed));
            tokio::spawn(async move {
                let mut done = 0;
                while Instant::now() < until {
                    match client.request("GET", "/v1/functions", &[], None).await {
                        Ok(_) => done += 1,
                        Err(NoAnswer::Deadline) => {
                            missed.fetch_add(1, Ordering::SeqCst);
                        }
                        Err(other) => panic!("{other:?}"),
                    }
                }
                done
            })
        });
        let done = futures_util::future::join_all(tasks)
            .await
            .into_iter()
            .map(Result::unwrap)
            .collect();
        assert!(most.load(Ordering::SeqCst) <= cap);
        (missed.load(Ordering::SeqCst), done)
    }

    #[tokio::test]
    async fn waiting_requests_are_served_in_arrival_order_so_none_starves_on_one_thread() {
        // Three times as many workers as connections: each waits about two answers' time for a connection, well
        // within the timeout, when connections go to the requests in the order they came.
        let (missed, done) = closed_loop(
            10,
            30,
            Duration::from_millis(20),
            150,
            Duration::from_millis(800),
        )
        .await;
        assert_eq!(missed, 0, "{done:?}");
        assert!(done.iter().all(|&count| count > 0), "{done:?}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn waiting_requests_are_served_in_arrival_order_so_none_starves_on_many_threads() {
        let (missed, done) = closed_loop(
            10,
            30,
            Duration::from_millis(20),
            150,
            Duration::from_millis(800),
        )
        .await;
        assert_eq!(missed, 0, "{done:?}");
        assert!(done.iter().all(|&count| count > 0), "{done:?}");
    }

    #[tokio::test]
    async fn a_request_id_that_is_not_ascii_is_read_byte_by_byte_as_httpx_reads_it() {
        let (url, _, _) = server(|_| {
            Some(
                b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nx-request-id: rid-\xe9\xff\r\n\r\n{}"
                    .to_vec(),
            )
        })
        .await;
        let client = HttpClient::new(&url, 2000).unwrap();
        let answer = client
            .request("GET", "/v1/functions", &[], None)
            .await
            .unwrap_or_else(|_| panic!("an answer"));
        assert_eq!(answer.request_id.as_deref(), Some("rid-\u{e9}\u{ff}"));
    }
}
