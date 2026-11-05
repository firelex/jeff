mod support;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use indexmap::IndexMap;
use jeff_sdk::binding::Call;
use jeff_sdk::escalation::{Question, escalate, llm};
use jeff_sdk::remote::{RemoteOptions, RestTransport, TokenSource, Transport};
use jeff_sdk::{BoxError, ErrorType, Jeff, Json, JsonMap, Policy, ShorthandOptions};
use serde_json::json;
use support::fixture;
use support::http_server::{Reply, json_reply, listing, serve};

fn context() -> JsonMap {
    json!({"company": "A furniture shop.", "channel": "email"})
        .as_object()
        .unwrap()
        .clone()
}

fn recorded_decision() -> Json {
    let body = fixture("remote.json")["cases"][1]["exchanges"][1]["response"]["body"]
        .as_str()
        .unwrap();
    serde_json::from_str::<Json>(body).unwrap()["decisions"][0].clone()
}

fn listing_or(
    post: impl Fn(&support::http_server::Request) -> Reply + Send + Sync + 'static,
) -> impl Fn(&support::http_server::Request) -> Reply + Send + Sync + 'static {
    move |request| {
        if request.method == "GET" {
            json_reply(200, &listing(), "rid-1")
        } else {
            post(request)
        }
    }
}

fn urgency(policy: Policy) -> ShorthandOptions {
    ShorthandOptions {
        function: Some("jeff.support.triage.urgency".into()),
        context: Some(context()),
        labels: None,
        on_low_confidence: policy,
    }
}

#[tokio::test]
async fn a_server_that_cannot_be_reached_is_backend_unavailable() {
    let error = Jeff::remote(
        "http://127.0.0.1:9",
        TokenSource::None,
        RemoteOptions::new(2000),
    )
    .await
    .unwrap_err();
    assert_eq!(error.kind(), ErrorType::BackendUnavailable);
    assert!(
        error
            .message()
            .starts_with("Cannot reach the Jeff server at http://127.0.0.1:9: ")
    );
}

#[tokio::test]
async fn a_server_that_does_not_send_headers_in_time_is_deadline_exceeded_promptly() {
    let server = serve(|_| Reply::Silent).await;
    let started = Instant::now();
    let error = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(200))
        .await
        .unwrap_err();
    let elapsed = started.elapsed().as_millis();
    assert_eq!(
        (error.kind(), error.message().to_owned()),
        (
            ErrorType::DeadlineExceeded,
            format!(
                "The Jeff server at {} did not answer GET /v1/functions within 200 ms; the server may still have run the request",
                server.url
            )
        )
    );
    assert!((190..400).contains(&elapsed), "{elapsed} ms");
    server.close();
}

#[tokio::test]
async fn an_answer_whose_body_stops_arriving_is_deadline_exceeded_promptly() {
    let server = serve(|_| Reply::Partial {
        status: 200,
        body_start: "{\"base_format\":".into(),
        length: 100,
    })
    .await;
    let started = Instant::now();
    let error = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(300))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::DeadlineExceeded);
    assert!(started.elapsed().as_millis() < 500);
    server.close();
}

#[tokio::test]
async fn an_answer_that_is_not_a_jeff_error_is_an_engine_error_quoting_it_and_a_redirect_is_not_followed()
 {
    let server = serve(|_| Reply::Full {
        status: 502,
        headers: vec![],
        body: "Bad gateway".into(),
    })
    .await;
    let error = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap_err();
    assert_eq!(
        error.message(),
        format!(
            "The Jeff server at {} answered GET /v1/functions with status 502 and a body that is not a Jeff error: Bad gateway",
            server.url
        )
    );
    server.close();
    let paths = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::clone(&paths);
    let moved = serve(move |request| {
        seen.lock().unwrap().push(request.path.clone());
        Reply::Full {
            status: 302,
            headers: vec![("location".into(), "/never".into())],
            body: "moved".into(),
        }
    })
    .await;
    let error = Jeff::remote(&moved.url, TokenSource::None, RemoteOptions::new(200))
        .await
        .unwrap_err();
    assert_eq!(
        error.message(),
        format!(
            "The Jeff server at {} answered GET /v1/functions with status 302 and a body that is not a Jeff error: moved",
            moved.url
        )
    );
    assert_eq!(*paths.lock().unwrap(), ["/v1/functions"]);
    moved.close();
}

#[tokio::test]
async fn an_answer_that_is_not_http_is_a_broken_connection() {
    let server = serve(|_| Reply::Raw(b"this is not HTTP\r\n\r\n".to_vec())).await;
    let error = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::BackendUnavailable);
    assert!(
        error.message().starts_with(&format!(
            "The connection to the Jeff server at {} failed during GET /v1/functions: ",
            server.url
        )),
        "{}",
        error.message()
    );
    server.close();
}

#[tokio::test]
async fn the_url_the_token_and_the_timeout_are_checked_and_a_token_is_never_shown() {
    assert_eq!(
        RestTransport::new("ftp://x", TokenSource::None, 1000)
            .err()
            .unwrap()
            .message(),
        "The server URL 'ftp://x' must start with http:// or https://"
    );
    assert_eq!(
        Jeff::remote("http://x", TokenSource::None, RemoteOptions::new(0))
            .await
            .unwrap_err()
            .kind(),
        ErrorType::InvalidRequest
    );
    let secret = TokenSource::function(|| async { Ok::<String, BoxError>("s3cret\nvalue".into()) });
    assert!(!format!("{:?} {:?}", TokenSource::fixed("s3cret-token"), secret).contains("s3cret"));
}

#[tokio::test]
async fn the_token_function_is_called_before_every_request_and_sent_as_a_bearer_token() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let record = Arc::clone(&seen);
    let server = serve(move |request| {
        record
            .lock()
            .unwrap()
            .push(request.header("authorization").map(str::to_owned));
        json_reply(200, &listing(), "rid-1")
    })
    .await;
    let count = Arc::new(AtomicUsize::new(0));
    let token = TokenSource::function(move || {
        let count = Arc::clone(&count);
        async move {
            Ok::<String, BoxError>(format!(
                "token-{}",
                count.fetch_add(1, Ordering::SeqCst) + 1
            ))
        }
    });
    let jeff = Jeff::remote(&server.url, token, RemoteOptions::new(2000))
        .await
        .unwrap();
    jeff.engine().installed(true).await.unwrap();
    assert_eq!(
        *seen.lock().unwrap(),
        [
            Some("Bearer token-1".to_owned()),
            Some("Bearer token-2".to_owned())
        ]
    );
    assert!(!format!("{jeff:?}").contains("token-"));
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_wire_error_becomes_its_jeff_error_with_the_answers_request_id() {
    let server = serve(listing_or(|_| json_reply(401, r#"{"error": {"type": "Unauthorised", "message": "The token has expired", "details": {}}}"#, "rid-42"))).await;
    let jeff = Jeff::remote(
        &server.url,
        TokenSource::fixed("t"),
        RemoteOptions::new(2000),
    )
    .await
    .unwrap();
    let call = Call::new("spam.is_spam", json!("Hi"))
        .with_context(json!({"channel": "sms"}).as_object().unwrap().clone());
    let error = jeff.decide(&[call]).await.unwrap_err();
    assert_eq!(
        (error.kind(), error.message(), error.request_id()),
        (
            ErrorType::Unauthorised,
            "The token has expired",
            Some("rid-42")
        )
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_402_usage_limit_reached_answer_is_raised_as_usage_limit_reached_with_its_details() {
    let server = serve(listing_or(|_| json_reply(402, r#"{"error": {"type": "UsageLimitReached", "message": "This account has used its 50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; calls resume then", "details": {"account": "u_1", "used": 50000123, "allowance": 50000000, "period_end": "2026-12-03T10:00:00Z"}}}"#, "rid-402"))).await;
    let jeff = Jeff::remote(
        &server.url,
        TokenSource::fixed("t"),
        RemoteOptions::new(2000),
    )
    .await
    .unwrap();
    let call = Call::new("spam.is_spam", json!("Hi"))
        .with_context(json!({"channel": "sms"}).as_object().unwrap().clone());
    let error = jeff.decide(&[call]).await.unwrap_err();
    assert_eq!(
        (error.kind(), error.request_id()),
        (ErrorType::UsageLimitReached, Some("rid-402"))
    );
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
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_shorthand_naming_a_function_of_another_interface_is_refused_before_any_request() {
    let posts = Arc::new(AtomicUsize::new(0));
    let counted = Arc::clone(&posts);
    let server = serve(listing_or(move |_| {
        counted.fetch_add(1, Ordering::SeqCst);
        json_reply(500, "unexpected", "rid-1")
    }))
    .await;
    let jeff = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap();
    let error = jeff
        .classify(json!("x"), urgency(Policy::Return))
        .await
        .unwrap_err();
    assert!(error.message().contains("implements rate, not classify"));
    assert_eq!(posts.load(Ordering::SeqCst), 0);
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_server_decision_or_a_low_confidence_decision_that_does_not_match_the_schema_is_a_contract_error()
 {
    let server = serve(listing_or(|_| {
        let mut decision = recorded_decision();
        decision["abstained"] = json!("no");
        json_reply(200, &json!({"decisions": [decision]}).to_string(), "rid-1")
    }))
    .await;
    let jeff = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap();
    assert_eq!(
        jeff.rate(json!("x"), urgency(Policy::Return))
            .await
            .unwrap_err()
            .kind(),
        ErrorType::ContractError
    );
    jeff.close().await;
    server.close();
    let low = serve(listing_or(|_| json_reply(409, r#"{"error": {"type": "LowConfidence", "message": "abstained", "details": {"decision": {"value": 1}}}}"#, "rid-1"))).await;
    let jeff = Jeff::remote(&low.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap();
    let error = jeff
        .rate(json!("x"), urgency(Policy::Error))
        .await
        .unwrap_err();
    assert_eq!(
        (error.kind(), error.request_id()),
        (ErrorType::ContractError, Some("rid-1"))
    );
    jeff.close().await;
    low.close();
}

#[tokio::test]
async fn closing_aborts_a_request_in_flight_and_a_closed_client_refuses_requests() {
    let server = serve(|_| Reply::Silent).await;
    let transport = Arc::new(RestTransport::new(&server.url, TokenSource::None, 5000).unwrap());
    let pending = {
        let transport = Arc::clone(&transport);
        tokio::spawn(async move { transport.functions().await })
    };
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    transport.close().await;
    let closed = format!(
        "This remote client of {} is closed; connect again with Jeff.remote",
        server.url
    );
    assert_eq!(pending.await.unwrap().unwrap_err().message(), closed);
    assert_eq!(transport.functions().await.unwrap_err().message(), closed);
    server.close();
}

#[tokio::test]
async fn a_registered_graph_with_an_llm_node_policy_is_refused_before_any_request() {
    let server = serve(|_| json_reply(200, &listing(), "rid-1")).await;
    let jeff = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap();
    let policies = IndexMap::from([(
        "n".to_owned(),
        escalate(
            vec![llm(|_: Question| async {
                Ok::<String, BoxError>("1".into())
            })],
            None,
        )
        .unwrap(),
    )]);
    let error = jeff
        .run_server_graph("g", json!("x"), JsonMap::new(), policies)
        .await
        .unwrap_err();
    assert!(
        error
            .message()
            .starts_with("Node n: a graph registered on the server runs there")
    );
    jeff.close().await;
    server.close();
}

#[tokio::test(flavor = "current_thread")]
async fn remote_mode_works_on_a_current_thread_runtime() {
    let server = serve(listing_or(|_| {
        json_reply(
            200,
            &json!({"decisions": [recorded_decision()]}).to_string(),
            "rid-1",
        )
    }))
    .await;
    let jeff = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap();
    let call = Call::new("triage.urgency", json!("x")).with_context(context());
    assert_eq!(
        jeff.decide(&[call]).await.unwrap()[0].function,
        "jeff.support.triage.urgency"
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn error_details_that_are_not_an_object_are_a_contract_error_and_null_details_are_none() {
    let server = serve(listing_or(|request| {
        let details = if request.body.windows(4).any(|window| window == b"\"Hi\"") { "\"garbage\"" } else { "null" };
        json_reply(400, &format!(r#"{{"error": {{"type": "InvalidRequest", "message": "bad", "details": {details}}}}}"#), "rid-7")
    }))
    .await;
    let jeff = Jeff::remote(&server.url, TokenSource::None, RemoteOptions::new(2000))
        .await
        .unwrap();
    let call = |input: &str| {
        Call::new("spam.is_spam", json!(input))
            .with_context(json!({"channel": "sms"}).as_object().unwrap().clone())
    };
    let error = jeff.decide(&[call("Hi")]).await.unwrap_err();
    assert_eq!(
        (error.kind(), error.request_id()),
        (ErrorType::ContractError, Some("rid-7"))
    );
    assert!(
        error.message().starts_with(
            "The InvalidRequest error received has 'details' that are not an object: 'garbage'"
        ),
        "{}",
        error.message()
    );
    let error = jeff.decide(&[call("Yo")]).await.unwrap_err();
    assert_eq!(
        (error.kind(), error.message(), error.request_id()),
        (ErrorType::InvalidRequest, "bad", Some("rid-7"))
    );
    assert!(error.details().is_empty());
    jeff.close().await;
    server.close();
}

// ---- the answer-size limit: 64 MiB as sent and for each decoded Content-Encoding layer

const LIMIT: usize = 64 * 1024 * 1024;

fn gzip(data: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    encoder.write_all(data).unwrap();
    encoder.finish().unwrap()
}

/// An answer with these headers and body, written whole; the server then closes the connection.
fn raw_answer(headers: &str, body: &[u8]) -> Vec<u8> {
    let mut bytes =
        format!("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n{headers}\r\n").into_bytes();
    bytes.extend_from_slice(body);
    bytes
}

/// What a closed transport answers every later request with.
async fn assert_closed(transport: &RestTransport, url: &str) {
    assert_eq!(
        transport.functions().await.unwrap_err().message(),
        format!("This remote client of {url} is closed; connect again with Jeff.remote")
    );
}

#[tokio::test]
async fn an_answer_whose_content_encoding_decodes_past_the_limit_is_refused_by_name_and_closes_the_client()
 {
    let once = gzip(&vec![0u8; LIMIT + 1]);
    let twice = gzip(&once);
    assert!(once.len() < 100_000 && twice.len() < 10_000);
    for (coding, body) in [("gzip", once), ("gzip, gzip", twice)] {
        let answer = raw_answer(
            &format!(
                "content-encoding: {coding}\r\ncontent-length: {}\r\n",
                body.len()
            ),
            &body,
        );
        let server = serve(move |_| Reply::Raw(answer.clone())).await;
        let transport = RestTransport::new(&server.url, TokenSource::None, 10_000).unwrap();
        let error = transport.functions().await.unwrap_err();
        assert_eq!(
            (error.kind(), error.message().to_owned()),
            (
                ErrorType::BackendUnavailable,
                format!(
                    "The Jeff server at {} answered GET /v1/functions with a body that decodes (Content-Encoding) to \
                     more than 67108864 bytes, the most this client reads; the client is closed, connect again with \
                     Jeff.remote",
                    server.url
                )
            ),
            "{coding}"
        );
        assert_closed(&transport, &server.url).await;
        server.close();
    }
}

#[tokio::test]
async fn a_body_with_no_length_ended_by_the_connection_is_held_to_the_limit() {
    let answer = raw_answer("connection: close\r\n", &vec![b' '; LIMIT + 1024 * 1024]);
    let server = serve(move |_| Reply::Raw(answer.clone())).await;
    let transport = RestTransport::new(&server.url, TokenSource::None, 10_000).unwrap();
    let error = transport.functions().await.unwrap_err();
    assert_eq!(
        (error.kind(), error.message().to_owned()),
        (
            ErrorType::BackendUnavailable,
            format!(
                "The Jeff server at {} answered GET /v1/functions with a body of more than 67108864 bytes, the most \
                 this client reads; the client is closed, connect again with Jeff.remote",
                server.url
            )
        )
    );
    assert_closed(&transport, &server.url).await;
    server.close();
}

#[tokio::test]
async fn requests_ask_for_gzip_and_deflate_as_httpx_does_and_a_gzipped_answer_is_read() {
    let seen: Arc<Mutex<Vec<Option<String>>>> = Default::default();
    let record = Arc::clone(&seen);
    let body = gzip(listing().as_bytes());
    let server = serve(move |request| {
        record
            .lock()
            .unwrap()
            .push(request.header("accept-encoding").map(str::to_owned));
        Reply::Raw(raw_answer(
            &format!(
                "content-encoding: gzip\r\ncontent-length: {}\r\n",
                body.len()
            ),
            &body,
        ))
    })
    .await;
    let transport = RestTransport::new(&server.url, TokenSource::None, 2000).unwrap();
    let listed = transport.functions().await.unwrap();
    assert_eq!(listed["base_format"], json!({"format": "bf16"}));
    assert_eq!(
        *seen.lock().unwrap(),
        vec![Some("gzip, deflate".to_owned())]
    );
    server.close();
}

#[tokio::test]
async fn a_body_that_cannot_be_decoded_is_an_engine_error_without_a_request_id() {
    let answer = raw_answer(
        "content-encoding: gzip\r\nx-request-id: r\r\ncontent-length: 8\r\n",
        b"not gzip",
    );
    let server = serve(move |_| Reply::Raw(answer.clone())).await;
    let transport = RestTransport::new(&server.url, TokenSource::None, 2000).unwrap();
    let error = transport.functions().await.unwrap_err();
    assert_eq!(
        (
            error.kind(),
            error.message().to_owned(),
            error.request_id().map(str::to_owned)
        ),
        (
            ErrorType::EngineError,
            format!(
                "The answer of the Jeff server at {} to GET /v1/functions could not be read: DecodingError: Error -3 \
                 while decompressing data: incorrect header check",
                server.url
            ),
            None
        )
    );
    server.close();
}
