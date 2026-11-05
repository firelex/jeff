//! Jeff::remote through the proxy the environment names, as the Python SDK's httpx (trust_env=True) and websockets
//! (proxy=True) pick it, against a local proxy. These tests set process environment variables, so they live in their
//! own test binary and run one at a time.

mod support;

use base64::Engine as _;
use jeff_sdk::binding::Call;
use jeff_sdk::remote::{RemoteOptions, TokenSource};
use jeff_sdk::{ErrorType, Jeff, JsonMap};
use serde_json::json;
use support::channel_server::{ChannelOptions, start_channel_server};
use support::proxy_server::start_proxy;

static ENVIRONMENT: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const NAMES: [&str; 10] = [
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "WS_PROXY",
    "ws_proxy",
];

/// Sets exactly these proxy variables for the rest of the test (the others unset).
fn set_environment(pairs: &[(&str, &str)]) {
    for name in NAMES {
        // SAFETY: the tests of this binary run one at a time (ENVIRONMENT), and nothing else in it reads the
        // environment from another thread while a variable is set.
        unsafe { std::env::remove_var(name) };
    }
    // the servers' helpers pin an empty proxy environment; these tests read the real one they set
    jeff_sdk::testing::pin_proxy_environment(None);
    for (name, value) in pairs {
        // SAFETY: as above.
        unsafe { std::env::set_var(name, value) };
    }
}

fn context() -> JsonMap {
    json!({"company": "A shop.", "channel": "email"})
        .as_object()
        .unwrap()
        .clone()
}

async fn remote(url: &str, channel: bool) -> jeff_sdk::Result<Jeff> {
    Jeff::remote(
        url,
        TokenSource::fixed("secret-token"),
        RemoteOptions {
            channel,
            ..RemoteOptions::new(3000)
        },
    )
    .await
}

fn basic(credentials: &str) -> String {
    format!(
        "Basic {}",
        base64::engine::general_purpose::STANDARD.encode(credentials)
    )
}

#[tokio::test]
async fn proxies_from_the_environment_are_used_and_no_proxy_bypasses_them() {
    let _one_at_a_time = ENVIRONMENT.lock().await;
    let server = start_channel_server(ChannelOptions::default()).await;
    let address = server.url.trim_start_matches("http://").to_owned();

    // HTTP to an http:// server: each request goes to the proxy naming the whole URL, with the proxy's credentials
    let proxy = start_proxy(None).await;
    let with_credentials = proxy.url.replace("http://", "http://alice:s3cret@");
    set_environment(&[("HTTP_PROXY", &with_credentials)]);
    let jeff = remote(&server.url, false).await.unwrap();
    assert_eq!(
        *proxy.seen.lock().unwrap(),
        [(
            format!("GET http://{address}/v1/functions HTTP/1.1"),
            basic("alice:s3cret")
        )]
    );
    jeff.close().await;

    // The channel: a CONNECT tunnel through the proxy (websockets reads HTTP_PROXY for ws:// last, after HTTPS_PROXY)
    let jeff = remote(&server.url, true).await.unwrap();
    jeff.decide(&[Call::new("triage.urgency", json!("Fire")).with_context(context())])
        .await
        .unwrap();
    assert!(
        proxy
            .lines()
            .contains(&format!("CONNECT {address} HTTP/1.1")),
        "{:?}",
        proxy.lines()
    );
    let host = address.clone();
    assert_eq!(
        proxy.connect_headers.lock().unwrap()[0],
        [("host".to_owned(), host)]
    );
    jeff.close().await;
    proxy.close();

    // a lower-case variable wins over its upper-case spelling
    let lower = start_proxy(None).await;
    let upper = start_proxy(None).await;
    set_environment(&[("HTTP_PROXY", &upper.url), ("http_proxy", &lower.url)]);
    remote(&server.url, false).await.unwrap().close().await;
    assert_eq!((lower.lines().len(), upper.lines().len()), (1, 0));

    // NO_PROXY: the server's host bypasses the proxy, for HTTP and for the channel
    set_environment(&[
        ("HTTP_PROXY", &lower.url),
        ("NO_PROXY", "localhost, 127.0.0.1"),
    ]);
    remote(&server.url, false).await.unwrap().close().await;
    remote(&server.url, true).await.unwrap().close().await;
    assert_eq!(lower.lines().len(), 1);
    set_environment(&[("HTTP_PROXY", &lower.url), ("no_proxy", "*")]);
    remote(&server.url, true).await.unwrap().close().await;
    assert_eq!(lower.lines().len(), 1);
    lower.close();
    upper.close();
    set_environment(&[]);
    server.close();
}

#[tokio::test]
async fn a_proxy_refusing_the_tunnel_is_named_as_python_names_it() {
    let _one_at_a_time = ENVIRONMENT.lock().await;
    let server = start_channel_server(ChannelOptions::default()).await;
    let port = server.url.rsplit(':').next().unwrap().to_owned();
    let proxy = start_proxy(Some(basic("alice:s3cret"))).await;
    set_environment(&[("HTTPS_PROXY", &proxy.url), ("NO_PROXY", "")]);

    // the channel to an http:// server reads HTTPS_PROXY too; without credentials the proxy answers 407
    let error = remote(&server.url, true).await.unwrap_err();
    assert_eq!((error.kind(), error.message()), (ErrorType::BackendUnavailable, format!(
        "Cannot reach the Jeff server at {} (ws://127.0.0.1:{port}/v1/channel): InvalidProxyStatus: proxy rejected connection: HTTP 407",
        server.url).as_str()));

    // HTTP to an https:// server: httpx tunnels with CONNECT host:port, Host and Accept
    let https = format!("https://127.0.0.1:{port}");
    let error = remote(&https, false).await.unwrap_err();
    assert_eq!((error.kind(), error.message()), (ErrorType::BackendUnavailable, format!(
        "The connection to the Jeff server at {https} failed during GET /v1/functions: ProxyError: 407 Proxy Authentication Required"
    ).as_str()));
    assert_eq!(
        proxy.lines().last().unwrap(),
        &format!("CONNECT 127.0.0.1:{port} HTTP/1.1")
    );

    // with the credentials the tunnel opens and TLS starts through it (the test server speaks plain HTTP, so TLS fails)
    let with_credentials = proxy.url.replace("http://", "http://alice:s3cret@");
    set_environment(&[("HTTPS_PROXY", &with_credentials)]);
    let error = remote(&https, false).await.unwrap_err();
    assert_eq!(error.kind(), ErrorType::BackendUnavailable);
    assert!(
        error
            .message()
            .starts_with(&format!("Cannot reach the Jeff server at {https}: ")),
        "{}",
        error.message()
    );
    assert_eq!(
        proxy.connect_headers.lock().unwrap().last().unwrap(),
        &[
            ("host".to_owned(), format!("127.0.0.1:{port}")),
            ("accept".to_owned(), "*/*".to_owned())
        ]
    );
    assert_eq!(
        proxy.seen.lock().unwrap().last().unwrap().1,
        basic("alice:s3cret")
    );

    // an https:// proxy is refused by name when it is picked
    set_environment(&[("HTTPS_PROXY", "https://tls-proxy:3128")]);
    let error = remote(&https, false).await.unwrap_err();
    assert_eq!(
        error.message(),
        "The proxy 'https://tls-proxy:3128' (HTTPS_PROXY) is a https:// proxy; this SDK connects only through http:// proxies"
    );
    set_environment(&[]);
    proxy.close();
    server.close();
}
