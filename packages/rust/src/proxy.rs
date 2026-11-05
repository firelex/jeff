//! Proxies from the environment, as the Python SDK's two transports pick them: the HTTP client as httpx does
//! (`trust_env=True`) and the channel as websockets does (`proxy=True`). Both read the variables through Python's
//! `urllib.request.getproxies_environment`, then differ:
//!
//! - httpx: `HTTP_PROXY` for http:// servers, `HTTPS_PROXY` for https:// ones, `ALL_PROXY` for both; a proxy without a
//!   scheme is taken as http://. `NO_PROXY` is a comma-separated list: `*` turns every proxy off; `example.com` bypasses
//!   example.com and its subdomains; `.example.com` only the subdomains; an IPv4 or IPv6 address or `localhost` only
//!   that host; an entry with `://` is a URL pattern (scheme, host, port). The most specific pattern wins (one with a
//!   port first, then the longest host, then the longest scheme). An http:// server is reached by sending each request
//!   to the proxy; an https:// one through a CONNECT tunnel.
//! - websockets: for wss:// `WSS_PROXY`, then `SOCKS_PROXY`, then `HTTPS_PROXY`; for ws:// `WS_PROXY`, `SOCKS_PROXY`,
//!   `HTTPS_PROXY`, then `HTTP_PROXY` (`ALL_PROXY` is not read). `NO_PROXY` is urllib's rule: `*` alone bypasses
//!   everything; otherwise the host (or host:port) equals an entry or ends with "." and an entry (leading dots
//!   ignored). The channel always goes through a CONNECT tunnel.
//!
//! Variable names: a lower-case variable wins over any other spelling of its name, and an empty lower-case one removes
//! the proxy. Under CGI (`REQUEST_METHOD` set) an upper-case `HTTP_PROXY` is ignored (CVE-2016-1000110).
//!
//! Only http:// proxies are supported: an https:// or SOCKS proxy the rules select is refused by name. Python also
//! reads the macOS and Windows system proxy settings when the environment names no proxy; this crate reads only the
//! environment.

use std::collections::HashMap;

use base64::Engine as _;

use crate::errors::{ErrorType, JeffError, Result};
use crate::pyformat::py_str;
use crate::tls::Endpoint;

/// A proxy server to connect through.
#[derive(Clone, PartialEq, Eq)]
pub(crate) struct ProxyServer {
    /// http://host:port, as messages name it (never with credentials).
    pub(crate) url: String,
    pub(crate) host: String,
    pub(crate) port: u16,
    /// "Basic <base64 of user:password>" when the proxy URL carries credentials.
    pub(crate) authorization: Option<String>,
}

impl std::fmt::Debug for ProxyServer {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ProxyServer")
            .field("url", &self.url)
            .field(
                "authorization",
                &self.authorization.as_ref().map(|_| "<not shown>"),
            )
            .finish()
    }
}

fn invalid(message: String) -> JeffError {
    JeffError::new(ErrorType::InvalidRequest, message)
}

/// The environment pinned by `testing::pin_proxy_environment`, read instead of the process's while set (only with the
/// `testing` feature).
#[cfg(feature = "testing")]
static PINNED: std::sync::Mutex<Option<Vec<(String, String)>>> = std::sync::Mutex::new(None);

#[cfg(feature = "testing")]
pub(crate) fn pin(environment: Option<Vec<(String, String)>>) {
    *PINNED
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner) = environment;
}

/// This process's environment, for the rules below (or the pinned one: see `testing::pin_proxy_environment`; in this
/// crate's own unit tests always an empty one, so the machine's proxies never reach them). A proxy variable whose
/// value is not text is an error naming it.
pub(crate) fn process_environment() -> Result<Vec<(String, String)>> {
    if cfg!(test) {
        return Ok(Vec::new());
    }
    #[cfg(feature = "testing")]
    if let Some(pinned) = PINNED
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .as_ref()
    {
        return Ok(pinned.clone());
    }
    let mut environment = Vec::new();
    for (name, value) in std::env::vars_os() {
        let name = name.to_string_lossy().into_owned();
        match value.into_string() {
            Ok(value) => environment.push((name, value)),
            Err(_) if proxy_name(&name).is_some() || name == "REQUEST_METHOD" => {
                return Err(invalid(format!(
                    "The environment variable {name} is not valid UTF-8; set it to a proxy URL or unset it"
                )));
            }
            Err(_) => {} // not a variable these rules read
        }
    }
    Ok(environment)
}

/// For NAME_PROXY (in any case), the lower-cased NAME.
fn proxy_name(name: &str) -> Option<String> {
    let characters: Vec<char> = name.chars().collect();
    let count = characters.len();
    if count > 5 && characters[count - 6] == '_' {
        let suffix: String = characters[count - 5..].iter().collect();
        if suffix.to_lowercase() == "proxy" {
            return Some(
                characters[..count - 6]
                    .iter()
                    .collect::<String>()
                    .to_lowercase(),
            );
        }
    }
    None
}

/// urllib.request.getproxies_environment: scheme (or "no") -> value.
fn environment_proxies(environment: &[(String, String)]) -> HashMap<String, String> {
    let mut proxies = HashMap::new();
    let mut found = Vec::new();
    for (name, value) in environment {
        if let Some(key) = proxy_name(name) {
            if !value.is_empty() {
                proxies.insert(key.clone(), value.clone());
            }
            found.push((name, value, key));
        }
    }
    if environment.iter().any(|(name, _)| name == "REQUEST_METHOD") {
        proxies.remove("http"); // remove: not an ordered map
    }
    for (name, value, key) in found {
        if name.ends_with("_proxy") {
            if value.is_empty() {
                proxies.remove(&key); // remove: not an ordered map
            } else {
                proxies.insert(key, value.clone());
            }
        }
    }
    proxies
}

fn percent_decoded(text: &str, what: &str) -> Result<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).ok();
            if let Some(byte) = hex.and_then(|hex| u8::from_str_radix(hex, 16).ok()) {
                out.push(byte);
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8(out).map_err(|_| {
        invalid(format!(
            "The proxy's {what} is not valid UTF-8 once decoded"
        ))
    })
}

/// scheme, user info, host (lower case, IPv6 without brackets), port text and the rest of a URL-like text.
struct Parts {
    scheme: String,
    userinfo: Option<String>,
    host: String,
    port: Option<String>,
    rest: String,
}

fn parts(text: &str) -> Option<Parts> {
    let (scheme, after) = text.split_once("://")?;
    let end = after.find(['/', '?', '#']).unwrap_or(after.len());
    let (authority, rest) = after.split_at(end);
    let (userinfo, hostport) = match authority.rfind('@') {
        Some(at) => (Some(authority[..at].to_owned()), &authority[at + 1..]),
        None => (None, authority),
    };
    let (host, port) = if let Some(bracketed) = hostport.strip_prefix('[') {
        let (host, after) = bracketed.split_once(']')?;
        (host, after.strip_prefix(':'))
    } else {
        match hostport.rsplit_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (hostport, None),
        }
    };
    Some(Parts {
        scheme: scheme.to_lowercase(),
        userinfo,
        host: host.to_lowercase(),
        port: port.map(str::to_owned),
        rest: rest.to_owned(),
    })
}

/// The proxy at `url` (named `source` in messages): http:// only, with its credentials as a Basic authorization.
/// websockets refuses a user name without a password; httpx sends it with an empty one.
fn http_proxy(url: &str, source: &str, websockets: bool) -> Result<ProxyServer> {
    let refused =
        |problem: &str| invalid(format!("The proxy {} ({source}) {problem}", py_str(url)));
    let Some(parts) = parts(url) else {
        return Err(refused("is not a URL"));
    };
    match parts.scheme.as_str() {
        "http" => {}
        "https" | "socks5" | "socks5h" | "socks4" | "socks4a" => {
            return Err(refused(&format!(
                "is a {}:// proxy; this SDK connects only through http:// proxies",
                parts.scheme
            )));
        }
        other => {
            return Err(refused(&format!(
                "has the scheme {}, which is not a proxy's",
                py_str(other)
            )));
        }
    }
    if parts.host.is_empty() {
        return Err(refused("has no host"));
    }
    let port = match parts.port.as_deref() {
        None | Some("") => 80,
        Some(written) => written
            .parse::<u16>()
            .map_err(|_| refused(&format!("has an invalid port {}", py_str(written))))?,
    };
    let authorization = match parts.userinfo {
        None => None,
        Some(userinfo) => {
            let (user, password) = match userinfo.split_once(':') {
                Some(pair) => pair,
                None if websockets => return Err(refused("has a user name without a password")),
                None => (userinfo.as_str(), ""),
            };
            let credentials = format!(
                "{}:{}",
                percent_decoded(user, "user name")?,
                percent_decoded(password, "password")?
            );
            Some(format!(
                "Basic {}",
                base64::engine::general_purpose::STANDARD.encode(credentials)
            ))
        }
    };
    let shown_host = if parts.host.contains(':') {
        format!("[{}]", parts.host)
    } else {
        parts.host.clone()
    };
    Ok(ProxyServer {
        url: format!("http://{shown_host}:{port}"),
        host: parts.host,
        port,
        authorization,
    })
}

/// One of httpx's mount patterns (httpx._utils.URLPattern).
struct Pattern {
    scheme: String,
    host: String,
    port: Option<u16>,
}

impl Pattern {
    fn parse(text: &str) -> Result<Pattern> {
        let parts = parts(text).ok_or_else(|| {
            invalid(format!(
                "The NO_PROXY entry {} is not a host or URL",
                py_str(text)
            ))
        })?;
        let port = match parts.port.as_deref() {
            None | Some("") => None,
            Some(written) => Some(written.parse::<u16>().map_err(|_| {
                invalid(format!(
                    "The NO_PROXY entry {} has an invalid port {}",
                    py_str(text),
                    py_str(written)
                ))
            })?),
        };
        Ok(Pattern {
            scheme: if parts.scheme == "all" {
                String::new()
            } else {
                parts.scheme
            },
            host: if parts.host == "*" {
                String::new()
            } else {
                parts.host
            },
            port,
        })
    }

    fn host_matches(&self, host: &str) -> bool {
        if let Some(domain) = self.host.strip_prefix("*.") {
            host.len() > domain.len() + 1 && host.ends_with(&format!(".{domain}"))
        } else if let Some(domain) = self.host.strip_prefix('*') {
            host == domain
                || (host.len() > domain.len() + 1 && host.ends_with(&format!(".{domain}")))
        } else {
            host == self.host
        }
    }

    fn matches(&self, scheme: &str, host: &str, port: Option<u16>) -> bool {
        (self.scheme.is_empty() || self.scheme == scheme)
            && (self.host.is_empty() || self.host_matches(host))
            && (self.port.is_none() || self.port == port)
    }

    /// Most specific first: a port, then a longer host, then a longer scheme.
    fn priority(&self) -> (u8, isize, isize) {
        (
            u8::from(self.port.is_none()),
            -(self.host.len() as isize),
            -(self.scheme.len() as isize),
        )
    }
}

/// The proxy httpx (trust_env=True) sends requests to `target` through; None to connect directly.
pub(crate) fn httpx_proxy(
    environment: &[(String, String)],
    target: &Endpoint,
) -> Result<Option<ProxyServer>> {
    let proxies = environment_proxies(environment);
    // the mounts, in the order a dict built by httpx's get_environment_proxies keeps them
    let mut mounts: Vec<(String, Option<(String, String)>)> = Vec::new();
    let mut mount = |key: String, proxy: Option<(String, String)>| match mounts
        .iter_mut()
        .find(|(known, _)| *known == key)
    {
        Some(entry) => entry.1 = proxy,
        None => mounts.push((key, proxy)),
    };
    for scheme in ["http", "https", "all"] {
        if let Some(value) = proxies.get(scheme) {
            let url = if value.contains("://") {
                value.clone()
            } else {
                format!("http://{value}")
            };
            mount(
                format!("{scheme}://"),
                Some((url, format!("{}_PROXY", scheme.to_uppercase()))),
            );
        }
    }
    let no_proxy = proxies.get("no").map_or("", String::as_str);
    for entry in no_proxy.split(',').map(str::trim) {
        if entry == "*" {
            return Ok(None);
        }
        if entry.is_empty() {
            continue;
        }
        let address = entry.split('/').next().unwrap_or(entry);
        let key = if entry.contains("://") {
            entry.to_owned()
        } else if address.parse::<std::net::Ipv4Addr>().is_ok() {
            format!("all://{entry}")
        } else if address.parse::<std::net::Ipv6Addr>().is_ok() {
            format!("all://[{entry}]")
        } else if entry.to_lowercase() == "localhost" {
            format!("all://{entry}")
        } else {
            format!("all://*{entry}")
        };
        mount(key, None);
    }
    // httpx builds every mounted proxy when the client is made: one it cannot use fails whichever server is asked for
    let mut patterns = mounts
        .into_iter()
        .map(|(key, proxy)| {
            let proxy = match proxy {
                // an https:// proxy is one httpx can use: refused only for the servers it is picked for
                Some((url, source)) if url.to_lowercase().starts_with("https://") => {
                    Some(http_proxy(&url, &source, false))
                }
                Some((url, source)) => Some(Ok(http_proxy(&url, &source, false)?)),
                None => None,
            };
            Ok((Pattern::parse(&key)?, proxy))
        })
        .collect::<Result<Vec<_>>>()?;
    patterns.sort_by_key(|(pattern, _)| pattern.priority()); // stable, as Python's sorted
    let scheme = if target.tls { "https" } else { "http" };
    let default_port = if target.tls { 443 } else { 80 };
    let port = (target.port != default_port).then_some(target.port);
    let host = target.host.to_lowercase();
    for (pattern, proxy) in patterns {
        if pattern.matches(scheme, &host, port) {
            return proxy.transpose();
        }
    }
    Ok(None)
}

/// urllib.request.proxy_bypass_environment for "host:port".
fn bypassed(proxies: &HashMap<String, String>, host_and_port: &str) -> bool {
    let Some(no_proxy) = proxies.get("no") else {
        return false;
    };
    if no_proxy == "*" {
        return true;
    }
    let host = host_and_port.to_lowercase();
    // urllib's _splitport: "(.*):([0-9]*)"
    let host_only = match host.rsplit_once(':') {
        Some((before, port)) if port.bytes().all(|byte| byte.is_ascii_digit()) => before.to_owned(),
        _ => host.clone(),
    };
    for name in no_proxy.split(',').map(str::trim) {
        if name.is_empty() {
            continue;
        }
        let name = name.trim_start_matches('.').to_lowercase();
        if host_only == name || host == name {
            return true;
        }
        let dotted = format!(".{name}");
        if host_only.ends_with(&dotted) || host.ends_with(&dotted) {
            return true;
        }
    }
    false
}

/// The proxy websockets (proxy=True) opens the channel to `target` through; None to connect directly.
pub(crate) fn websockets_proxy(
    environment: &[(String, String)],
    target: &Endpoint,
) -> Result<Option<ProxyServer>> {
    let proxies = environment_proxies(environment);
    if bypassed(&proxies, &format!("{}:{}", target.host, target.port)) {
        return Ok(None);
    }
    let schemes: &[&str] = if target.tls {
        &["wss", "socks", "https"]
    } else {
        &["ws", "socks", "https", "http"]
    };
    for scheme in schemes {
        if let Some(value) = proxies.get(*scheme) {
            let value = match value.strip_prefix("http://") {
                Some(rest) if *scheme == "socks" => format!("socks5h://{rest}"),
                _ => value.clone(),
            };
            let source = format!("{}_PROXY", scheme.to_uppercase());
            if let Some(parts) = parts(&value)
                && !matches!(parts.rest.as_str(), "" | "/")
            {
                return Err(invalid(format!(
                    "The proxy {} ({source}) has a path, query or fragment, which a proxy cannot use",
                    py_str(&value)
                )));
            }
            return Ok(Some(http_proxy(&value, &source, true)?));
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
            .collect()
    }

    #[test]
    fn a_lower_case_variable_wins_and_an_empty_one_removes_the_proxy_and_cgi_ignores_upper_case_http_proxy()
     {
        let proxies = environment_proxies(&env(&[
            ("HTTPS_PROXY", "http://upper:1"),
            ("https_proxy", "http://lower:2"),
        ]));
        assert_eq!(proxies["https"], "http://lower:2");
        let proxies = environment_proxies(&env(&[
            ("https_proxy", "http://lower:2"),
            ("HTTPS_PROXY", "http://upper:1"),
        ]));
        assert_eq!(proxies["https"], "http://lower:2");
        let proxies = environment_proxies(&env(&[
            ("HTTPS_PROXY", "http://upper:1"),
            ("https_proxy", ""),
        ]));
        assert!(!proxies.contains_key("https"));
        let proxies = environment_proxies(&env(&[
            ("HTTP_PROXY", "http://upper:1"),
            ("REQUEST_METHOD", "GET"),
        ]));
        assert!(!proxies.contains_key("http"));
        let proxies = environment_proxies(&env(&[
            ("http_proxy", "http://lower:1"),
            ("REQUEST_METHOD", "GET"),
        ]));
        assert_eq!(proxies["http"], "http://lower:1");
    }

    #[test]
    fn credentials_become_a_basic_authorization_and_never_show() {
        let proxy = http_proxy(
            "http://al%20ice:s3cr%3At@Proxy.Example:3128/",
            "HTTP_PROXY",
            false,
        )
        .unwrap();
        assert_eq!(proxy.url, "http://proxy.example:3128");
        assert_eq!(
            proxy.authorization.as_deref(),
            Some(
                format!(
                    "Basic {}",
                    base64::engine::general_purpose::STANDARD.encode("al ice:s3cr:t")
                )
                .as_str()
            )
        );
        assert!(!format!("{proxy:?}").contains("s3cr"));
        let refused = http_proxy("socks5://h:1080", "ALL_PROXY", false).unwrap_err();
        assert_eq!(
            refused.message(),
            "The proxy 'socks5://h:1080' (ALL_PROXY) is a socks5:// proxy; this SDK connects only through http:// proxies"
        );
    }

    /// Cases recorded from httpx 0.28.1 and websockets 17.3 with packages/rust/tests/data/proxy_cases.py.
    #[test]
    fn every_recorded_case_picks_the_proxy_python_picks() {
        let path =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data/proxy_cases.json");
        let cases: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let mut checked = 0;
        for case in cases.as_array().unwrap() {
            let environment: Vec<(String, String)> = case["env"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(name, value)| (name.clone(), value.as_str().unwrap().to_owned()))
                .collect();
            let target = Endpoint::parse(case["url"].as_str().unwrap()).unwrap();
            for (kind, picked) in [
                ("httpx", httpx_proxy(&environment, &target)),
                ("websockets", websockets_proxy(&environment, &target)),
            ] {
                let expected = &case[kind];
                let got = match picked {
                    Ok(None) => serde_json::Value::Null,
                    Ok(Some(proxy)) => {
                        serde_json::json!({"url": proxy.url, "authorization": proxy.authorization})
                    }
                    Err(_) => serde_json::json!("error"),
                };
                assert_eq!(
                    &got, expected,
                    "{kind} for {} with {:?}",
                    case["url"], case["env"]
                );
                checked += 1;
            }
        }
        assert!(checked > 100, "{checked}");
    }
}
