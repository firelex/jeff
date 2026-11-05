//! Streams to a Jeff server: plain TCP for http:// and ws://, TLS (rustls with the webpki roots, ALPN http/1.1) for
//! https:// and wss://. The HTTP client and the channel share them.

use std::sync::{Arc, OnceLock};

use tokio::net::TcpStream;

use crate::errors::{ErrorType, JeffError, Result};
use crate::escalation::BoxFuture;
use crate::proxy::ProxyServer;
use crate::pyformat::py_str;

pub(crate) type Stream =
    tokio_util::either::Either<TcpStream, tokio_rustls::client::TlsStream<TcpStream>>;

/// A server URL taken apart.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Endpoint {
    pub(crate) tls: bool,
    pub(crate) host: String,
    pub(crate) port: u16,
    /// host[:port], for the Host header.
    pub(crate) authority: String,
    /// The URL's path without its trailing slashes ("" for none), put before every request path.
    pub(crate) base_path: String,
    /// The URL without its trailing slashes, as messages name the server.
    pub(crate) url: String,
}

impl Endpoint {
    pub(crate) fn parse(url: &str) -> Result<Endpoint> {
        let refused = || {
            JeffError::new(
                ErrorType::InvalidRequest,
                format!(
                    "The server URL {} must start with http:// or https://",
                    py_str(url)
                ),
            )
        };
        let tls = if url.starts_with("https://") {
            true
        } else if url.starts_with("http://") {
            false
        } else {
            return Err(refused());
        };
        let trimmed = url.trim_end_matches('/');
        let uri: http::Uri = trimmed.parse().map_err(|error| {
            JeffError::new(
                ErrorType::InvalidRequest,
                format!("The server URL {} is not a URL: {error}", py_str(url)),
            )
        })?;
        let invalid = |problem: &str| {
            JeffError::new(
                ErrorType::InvalidRequest,
                format!("The server URL {} {problem}", py_str(url)),
            )
        };
        // "http://" trimmed is "http:", which parses as a bare authority with no scheme.
        if uri.scheme_str() != Some(if tls { "https" } else { "http" }) {
            return Err(invalid("has no host"));
        }
        let authority = uri.authority().ok_or_else(|| invalid("has no host"))?;
        if authority.as_str().contains('@') {
            return Err(invalid(
                "must not carry a user name or password; pass the token instead",
            ));
        }
        if uri.query().is_some() {
            return Err(invalid("must not have a query (the part after '?')"));
        }
        let host = authority
            .host()
            .trim_start_matches('[')
            .trim_end_matches(']')
            .to_owned();
        if host.is_empty() {
            return Err(invalid("has no host"));
        }
        // The text after the host: "" or ":" mean the scheme's default port (RFC 3986), anything else must be one.
        let port = match authority.as_str()[authority.host().len()..].strip_prefix(':') {
            None | Some("") => {
                if tls {
                    443
                } else {
                    80
                }
            }
            Some(written) => written
                .parse::<u16>()
                .map_err(|_| invalid(&format!("has an invalid port {}", py_str(written))))?,
        };
        let base_path = uri.path().trim_end_matches('/').to_owned();
        Ok(Endpoint {
            tls,
            host,
            port,
            authority: authority.as_str().to_owned(),
            base_path,
            url: trimmed.to_owned(),
        })
    }
}

/// How the clients open a stream (tests substitute a slow or silent one).
pub(crate) trait Connect: Send + Sync {
    fn connect(&self, endpoint: &Endpoint) -> BoxFuture<'_, std::io::Result<Stream>>;
}

fn tls_config() -> Arc<rustls::ClientConfig> {
    static CONFIG: OnceLock<Arc<rustls::ClientConfig>> = OnceLock::new();
    Arc::clone(CONFIG.get_or_init(|| {
        let mut roots = rustls::RootCertStore::empty();
        roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        let mut config = rustls::ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .expect("ring supports the default protocol versions")
        .with_root_certificates(roots)
        .with_no_client_auth();
        config.alpn_protocols = vec![b"http/1.1".to_vec()];
        Arc::new(config)
    }))
}

/// A TCP connection to host:port, without Nagle's delay.
async fn tcp(host: &str, port: u16) -> std::io::Result<TcpStream> {
    let tcp = TcpStream::connect((host, port)).await?;
    tcp.set_nodelay(true)?;
    Ok(tcp)
}

/// TLS over `tcp` to `endpoint` when its URL is https:// (or wss://); `tcp` itself otherwise.
async fn secured(tcp: TcpStream, endpoint: &Endpoint) -> std::io::Result<Stream> {
    if !endpoint.tls {
        return Ok(Stream::Left(tcp));
    }
    let name = rustls::pki_types::ServerName::try_from(endpoint.host.clone())
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
    Ok(Stream::Right(
        tokio_rustls::TlsConnector::from(tls_config())
            .connect(name, tcp)
            .await?,
    ))
}

/// Why a proxy did not open a tunnel: it answered CONNECT with another status, or not with HTTP.
#[derive(Debug)]
pub(crate) enum ProxyFailure {
    Refused { status: u16, reason: String },
    Unreadable(String),
}

impl std::fmt::Display for ProxyFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProxyFailure::Refused { status, reason } => write!(formatter, "{status} {reason}"),
            ProxyFailure::Unreadable(problem) => formatter.write_str(problem),
        }
    }
}

impl std::error::Error for ProxyFailure {}

impl ProxyFailure {
    /// The ProxyFailure an io error carries, if any.
    pub(crate) fn of(error: &std::io::Error) -> Option<&ProxyFailure> {
        error
            .get_ref()
            .and_then(|inner| inner.downcast_ref::<ProxyFailure>())
    }
}

/// The largest CONNECT answer head read from a proxy.
const MAX_PROXY_HEAD: usize = 64 * 1024;

/// A TCP connection to `proxy` turned into a tunnel to `target` (host:port) with CONNECT, sending `headers` and the
/// proxy's credentials. The answer's head is read byte by byte, so nothing past it is taken from the tunnel.
async fn tunnel(
    proxy: &ProxyServer,
    target: &str,
    headers: &[(String, String)],
) -> std::io::Result<TcpStream> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut stream = tcp(&proxy.host, proxy.port).await?;
    let mut request = format!("CONNECT {target} HTTP/1.1\r\n");
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    if let Some(authorization) = &proxy.authorization {
        request.push_str(&format!("Proxy-Authorization: {authorization}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes()).await?;
    let mut head = Vec::new();
    while !head.ends_with(b"\r\n\r\n") {
        if head.len() >= MAX_PROXY_HEAD {
            return Err(std::io::Error::other(ProxyFailure::Unreadable(format!(
                "the proxy {} answered CONNECT with a head over {MAX_PROXY_HEAD} bytes",
                proxy.url
            ))));
        }
        let mut byte = [0u8; 1];
        if stream.read(&mut byte).await? == 0 {
            return Err(std::io::Error::other(ProxyFailure::Unreadable(format!(
                "the proxy {} closed the connection before answering CONNECT",
                proxy.url
            ))));
        }
        head.push(byte[0]);
    }
    let text = String::from_utf8_lossy(&head);
    let line = text.lines().next().unwrap_or("");
    let mut fields = line.splitn(3, ' ');
    let version = fields.next().unwrap_or("");
    let status = fields.next().and_then(|code| code.parse::<u16>().ok());
    let reason = fields.next().unwrap_or("").to_owned();
    match status {
        Some(status) if version.starts_with("HTTP/1.") && (200..300).contains(&status) => {
            Ok(stream)
        }
        Some(status) if version.starts_with("HTTP/1.") => {
            Err(std::io::Error::other(ProxyFailure::Refused {
                status,
                reason,
            }))
        }
        _ => Err(std::io::Error::other(ProxyFailure::Unreadable(format!(
            "the proxy {} answered CONNECT with {}, not an HTTP status line",
            proxy.url,
            crate::pyformat::py_str(line)
        )))),
    }
}

/// How a client reaches its server: directly, by sending each request to a proxy (an http:// server through httpx's
/// rules), or through a CONNECT tunnel (an https:// server through httpx's rules, and the channel always).
#[derive(Clone, Debug)]
pub(crate) enum Route {
    Direct,
    Forward(ProxyServer),
    Tunnel {
        proxy: ProxyServer,
        /// host:port, as the CONNECT line names the server.
        target: String,
        /// The CONNECT request's headers besides Proxy-Authorization.
        headers: Vec<(String, String)>,
    },
}

/// Opens streams along a Route.
pub(crate) struct RouteConnect(pub(crate) Route);

impl Connect for RouteConnect {
    fn connect(&self, endpoint: &Endpoint) -> BoxFuture<'_, std::io::Result<Stream>> {
        let endpoint = endpoint.clone();
        Box::pin(async move {
            match &self.0 {
                Route::Direct => {
                    secured(tcp(&endpoint.host, endpoint.port).await?, &endpoint).await
                }
                Route::Forward(proxy) => Ok(Stream::Left(tcp(&proxy.host, proxy.port).await?)),
                Route::Tunnel {
                    proxy,
                    target,
                    headers,
                } => secured(tunnel(proxy, target, headers).await?, &endpoint).await,
            }
        })
    }
}
