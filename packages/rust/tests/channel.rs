//! Jeff::remote with channel: true: bound functions are handles on the server's /v1/channel (port of the Node SDK's
//! test/channel.test.ts and packages/python/tests/test_channel_client.py, against a fake channel server).

mod support;

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use jeff_sdk::binding::Call;
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::channel::{channel_url, token_expiry};
use jeff_sdk::escalation::{Question, escalate, function_step, llm};
use jeff_sdk::remote::{RemoteOptions, TokenSource};
use jeff_sdk::testing::FakeEngine;
use jeff_sdk::{BoxError, CallOptions, Decision, ErrorType, Jeff, JeffError, Json, JsonMap};
use serde_json::json;
use support::channel_server::{ChannelOptions, INSTALLED, hashed_answers, start_channel_server};

fn context() -> JsonMap {
    json!({"company": "A shop.", "channel": "email"})
        .as_object()
        .unwrap()
        .clone()
}

fn ops(received: &[Json]) -> Vec<String> {
    received
        .iter()
        .map(|message| message["op"].as_str().unwrap().to_owned())
        .collect()
}

fn jwt(exp: u64) -> String {
    let part = |value: Json| URL_SAFE_NO_PAD.encode(value.to_string());
    format!(
        "{}.{}.c2ln",
        part(json!({"alg": "EdDSA"})),
        part(json!({"sub": "alice", "exp": exp}))
    )
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

async fn open(url: &str, token: TokenSource, timeout_ms: u64) -> jeff_sdk::Result<Jeff> {
    Jeff::remote(
        url,
        token,
        RemoteOptions {
            channel: true,
            ..RemoteOptions::new(timeout_ms)
        },
    )
    .await
}

fn secret() -> TokenSource {
    TokenSource::fixed("secret-token")
}

fn bind_options() -> CallOptions {
    CallOptions {
        context: Some(context()),
        ..Default::default()
    }
}

fn decide_call(input: &str) -> Call {
    Call::new("triage.urgency", json!(input)).with_context(context())
}

#[test]
fn the_channel_url_follows_the_server_url_and_a_jwts_exp_is_read_without_verifying_it() {
    assert_eq!(
        channel_url("http://h:1/jeff/").unwrap(),
        "ws://h:1/jeff/v1/channel"
    );
    assert_eq!(channel_url("https://h").unwrap(), "wss://h/v1/channel");
    assert_eq!(
        channel_url("ftp://h").unwrap_err().message(),
        "The server URL 'ftp://h' must start with http:// or https://"
    );
    assert_eq!(token_expiry("an-api-key").unwrap(), None);
    assert_eq!(token_expiry(&jwt(1234)).unwrap(), Some(1234.0));
    assert_eq!(
        token_expiry("a.!!!.c").unwrap_err().message(),
        "The token is shaped like a JWT, but its payload is not base64-encoded JSON"
    );
    let no_exp = format!(
        "a.{}.c",
        URL_SAFE_NO_PAD.encode(json!({"sub": "alice"}).to_string())
    );
    assert_eq!(
        token_expiry(&no_exp).unwrap_err().message(),
        "The token is a JWT without a numeric exp (got None); the server needs one"
    );
}

#[tokio::test]
async fn a_bound_function_sends_one_bind_then_only_call_frames_and_close_releases_its_handle() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    assert_eq!(
        bound.call(json!("Fire")).await.unwrap().value,
        jeff_sdk::Value::Int(2)
    );
    let values: Vec<jeff_sdk::Value> = bound
        .many(vec![json!("a"), json!("b")])
        .await
        .unwrap()
        .into_iter()
        .map(|decision| decision.value)
        .collect();
    assert_eq!(values, [jeff_sdk::Value::Int(2), jeff_sdk::Value::Int(2)]);
    bound.close().await.unwrap();
    assert_eq!(
        bound.call(json!("again")).await.unwrap_err().kind(),
        ErrorType::InvalidRequest
    );
    let received = server.received.lock().unwrap().clone();
    assert_eq!(
        ops(&received),
        ["hello", "auth", "bind", "call", "call_many", "release"]
    );
    assert_eq!(
        received[2],
        json!({"op": "bind", "function": "jeff.support.triage.urgency", "on_low_confidence": "return", "context": context(), "id": 2})
    );
    assert_eq!(*server.headers.lock().unwrap(), ["Bearer secret-token"]);
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn without_a_token_there_is_no_auth_and_no_authorization_header() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, TokenSource::None, 2000).await.unwrap();
    jeff.decide(&[decide_call("x")]).await.unwrap();
    assert_eq!(ops(&server.received.lock().unwrap()), ["hello", "decide"]);
    assert_eq!(*server.headers.lock().unwrap(), [""]);
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_token_the_server_refuses_is_unauthorised_at_connect_and_the_message_never_shows_a_token()
{
    let server = start_channel_server(ChannelOptions {
        token: Some("the-key".into()),
        ..Default::default()
    })
    .await;
    let error = open(&server.url, TokenSource::fixed("the-kez"), 2000)
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::Unauthorised);
    assert!(!error.message().contains("the-kez") && !format!("{error:?}").contains("the-kez"));
    let jeff = open(&server.url, TokenSource::fixed("the-key"), 2000)
        .await
        .unwrap();
    assert_eq!(
        jeff.decide(&[decide_call("x")]).await.unwrap()[0].value,
        jeff_sdk::Value::Int(2)
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn the_bind_carries_the_policy_the_server_runs_and_one_with_an_llm_step_is_bound_as_return() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    jeff.function(
        "triage.urgency",
        CallOptions {
            on_low_confidence: escalate(vec![function_step("triage.urgency")], None).unwrap(),
            ..bind_options()
        },
    )
    .await
    .unwrap();
    let ask = llm(|_: Question| async { Ok::<String, BoxError>("3".into()) });
    jeff.function(
        "triage.urgency",
        CallOptions {
            on_low_confidence: escalate(vec![ask], None).unwrap(),
            ..bind_options()
        },
    )
    .await
    .unwrap();
    let binds: Vec<Json> = server
        .received
        .lock()
        .unwrap()
        .iter()
        .filter(|message| message["op"] == "bind")
        .map(|message| message["on_low_confidence"].clone())
        .collect();
    assert_eq!(
        binds,
        [
            json!({"escalate": ["jeff.support.triage.urgency"]}),
            json!("return")
        ]
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_bind_answered_with_another_adapter_version_is_refused_and_its_handle_released_before_the_next_request()
 {
    let server = start_channel_server(ChannelOptions {
        version: Some("9.9.9".into()),
        ..Default::default()
    })
    .await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    let local = jeff
        .catalogue()
        .adapter("jeff.support.triage")
        .unwrap()
        .version
        .clone();
    let error = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap_err();
    assert_eq!(
        error.message(),
        format!(
            "jeff.support.triage: the server serves version 9.9.9, this client's catalogue has {local}; pass catalogue= with the server's manifests"
        )
    );
    jeff.decide(&[decide_call("x")]).await.unwrap();
    assert_eq!(
        ops(&server.received.lock().unwrap()),
        ["hello", "auth", "bind", "release", "decide"]
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_queued_release_is_sent_before_the_next_request() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    drop(bound); // queues its release, sent before the next request
    jeff.decide(&[decide_call("x")]).await.unwrap();
    assert_eq!(
        ops(&server.received.lock().unwrap())[3..],
        ["release", "decide"]
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn dropping_a_bound_function_or_the_client_in_a_thread_without_a_runtime_never_panics() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    std::thread::spawn(move || drop(bound))
        .join()
        .expect("dropping a bound function outside a runtime does not panic");
    jeff.decide(&[decide_call("x")]).await.unwrap();
    assert_eq!(
        ops(&server.received.lock().unwrap())[3..],
        ["release", "decide"]
    );
    let copy = jeff.clone();
    std::thread::spawn(move || drop(copy))
        .join()
        .expect("dropping a Jeff clone outside a runtime does not panic");
    jeff.close().await;
    std::thread::spawn(move || drop(jeff))
        .join()
        .expect("dropping the last Jeff outside a runtime does not panic");
    server.close();
}

#[tokio::test]
async fn a_failure_the_server_sends_carries_the_connections_id_as_channel_id() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    jeff.channel_transport().unwrap().release(1).await.unwrap();
    let error = bound.call(json!("x")).await.unwrap_err();
    assert_eq!(
        (error.kind(), error.message(), error.request_id()),
        (ErrorType::InvalidRequest, "handle 1 is not bound", None)
    );
    assert_eq!(
        Json::Object(error.details().clone()),
        json!({"channel_id": "0badc0de"})
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_low_confidence_over_the_channel_whose_decision_does_not_match_the_schema_is_a_contract_error()
 {
    let server = start_channel_server(ChannelOptions {
        bad_low_confidence_decision: true,
        ..Default::default()
    })
    .await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    assert_eq!(
        bound.call(json!("x")).await.unwrap_err().kind(),
        ErrorType::ContractError
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_function_token_that_is_a_jwt_is_refreshed_before_its_exp() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let tokens = [jwt(now() + 61), jwt(now() + 3600)];
    let calls = Arc::new(AtomicUsize::new(0));
    // the handshake header and the auth read the first token; GET /v1/functions and the refresh read the second
    let source = {
        let (tokens, calls) = (tokens.clone(), Arc::clone(&calls));
        TokenSource::function(move || {
            let token = if calls.fetch_add(1, Ordering::SeqCst) < 2 {
                tokens[0].clone()
            } else {
                tokens[1].clone()
            };
            async move { Ok::<String, BoxError>(token) }
        })
    };
    let jeff = open(&server.url, source, 2000).await.unwrap();
    tokio::time::sleep(Duration::from_millis(1600)).await;
    let refresh = server
        .received
        .lock()
        .unwrap()
        .iter()
        .find(|message| message["op"] == "refresh")
        .cloned()
        .expect("a refresh was sent");
    assert_eq!(refresh["token"], json!(tokens[1]));
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_string_token_is_never_refreshed_even_a_jwt_inside_the_refresh_window() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, TokenSource::fixed(jwt(now() + 30)), 2000)
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(ops(&server.received.lock().unwrap()), ["hello", "auth"]);
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_token_function_that_never_renews_closes_the_channel_at_exp_with_unauthorised_carrying_every_failure()
 {
    let server = start_channel_server(ChannelOptions::default()).await;
    let fixed = jwt(now() + 2);
    let jeff = open(
        &server.url,
        TokenSource::function(move || {
            let token = fixed.clone();
            async move { Ok::<String, BoxError>(token) }
        }),
        2000,
    )
    .await
    .unwrap();
    tokio::time::sleep(Duration::from_millis(3200)).await;
    let error = jeff.decide(&[decide_call("x")]).await.unwrap_err();
    assert_eq!(error.kind(), ErrorType::Unauthorised);
    assert!(
        error
            .message()
            .starts_with("The channel's token expired at "),
        "{}",
        error.message()
    );
    assert!(error.notes().len() >= 2);
    assert!(
        error
            .notes()
            .iter()
            .all(|note| note.contains("not after the channel's current token"))
    );
    assert!(!ops(&server.received.lock().unwrap()).contains(&"refresh".to_owned()));
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn an_unanswered_request_is_deadline_exceeded_promptly_and_its_late_reply_is_dropped() {
    let server = start_channel_server(ChannelOptions {
        silent: vec!["decide"],
        ..Default::default()
    })
    .await;
    let jeff = open(&server.url, secret(), 300).await.unwrap();
    let started = Instant::now();
    let error = jeff.decide(&[decide_call("x")]).await.unwrap_err();
    let elapsed = started.elapsed().as_millis();
    assert_eq!(
        error.message(),
        format!(
            "The channel {} did not answer 'decide' within 300 ms; the server may still have run the request",
            channel_url(&server.url).unwrap()
        )
    );
    assert!((290..500).contains(&elapsed), "{elapsed} ms");
    let late = server
        .received
        .lock()
        .unwrap()
        .iter()
        .find(|message| message["op"] == "decide")
        .cloned()
        .unwrap();
    server.send(json!({"id": late["id"], "ok": true, "result": {"decisions": []}}));
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    assert_eq!(
        bound.call(json!("Fire")).await.unwrap().value,
        jeff_sdk::Value::Int(2)
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_server_that_never_answers_the_opening_handshake_is_backend_unavailable_promptly() {
    support::no_proxies();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let silent = tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((stream, _)) = listener.accept().await {
            held.push(stream);
        }
    });
    let started = Instant::now();
    let error = open(&url, secret(), 200).await.unwrap_err();
    let elapsed = started.elapsed().as_millis();
    assert_eq!(
        error.message(),
        format!(
            "Cannot reach the Jeff server at {url}: opening {} took more than 200 ms",
            channel_url(&url).unwrap()
        )
    );
    assert!((190..400).contains(&elapsed), "{elapsed} ms");
    silent.abort();
}

#[tokio::test]
async fn a_server_that_sends_no_hello_is_backend_unavailable_promptly() {
    let server = start_channel_server(ChannelOptions {
        silent: vec!["hello"],
        ..Default::default()
    })
    .await;
    let started = Instant::now();
    let error = open(&server.url, secret(), 200).await.unwrap_err();
    assert_eq!(
        error.message(),
        format!(
            "No hello from the channel {} within 200 ms",
            channel_url(&server.url).unwrap()
        )
    );
    assert!(started.elapsed().as_millis() < 400);
    server.close();
}

#[tokio::test]
async fn a_server_refusing_the_websocket_is_an_engine_error_naming_the_http_status() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let elsewhere = format!("{}/elsewhere", server.url);
    let error = open(&elsewhere, secret(), 2000).await.unwrap_err();
    assert_eq!(
        error.message(),
        format!(
            "The Jeff server at {elsewhere} refused to open the channel {}: HTTP status 400",
            channel_url(&elsewhere).unwrap()
        )
    );
    server.close();
}

#[tokio::test]
async fn a_reply_for_an_id_nobody_sent_or_a_failure_with_id_0_ends_the_channel() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    server.send(json!({"id": 999, "ok": true, "result": {}}));
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(
        jeff.function("triage.urgency", bind_options())
            .await
            .unwrap_err()
            .message(),
        format!(
            "The channel {} sent a reply for id 999, which no request is waiting for; the channel is closed",
            channel_url(&server.url).unwrap()
        )
    );
    jeff.close().await;
    let again = open(&server.url, secret(), 2000).await.unwrap();
    server.send(json!({"id": 0, "ok": false, "error": {"type": "Unauthorised", "message": "the channel's token expired", "details": {"channel_id": "0badc0de"}}}));
    tokio::time::sleep(Duration::from_millis(50)).await;
    let error = again.decide(&[decide_call("x")]).await.unwrap_err();
    assert_eq!(
        (error.kind(), error.message()),
        (ErrorType::Unauthorised, "the channel's token expired")
    );
    again.close().await;
    server.close();
}

#[tokio::test]
async fn the_server_going_away_fails_waiting_and_later_requests_with_backend_unavailable() {
    let server = start_channel_server(ChannelOptions {
        silent: vec!["decide"],
        ..Default::default()
    })
    .await;
    let jeff = open(&server.url, secret(), 5000).await.unwrap();
    let waiting = {
        let jeff = jeff.clone();
        tokio::spawn(async move { jeff.decide(&[decide_call("x")]).await })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    server.drop_connections();
    let closed = format!(
        "{}: the channel closed (no close frame received)",
        channel_url(&server.url).unwrap()
    );
    assert_eq!(waiting.await.unwrap().unwrap_err().message(), closed);
    assert_eq!(
        jeff.function("triage.urgency", bind_options())
            .await
            .unwrap_err()
            .kind(),
        ErrorType::BackendUnavailable
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn close_ends_the_channel_and_later_requests_are_invalid_requests() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 2000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    jeff.close().await;
    assert_eq!(
        jeff.decide(&[decide_call("x")])
            .await
            .unwrap_err()
            .message(),
        format!(
            "This remote client of {} is closed; connect again with Jeff.remote",
            server.url
        )
    );
    assert_eq!(
        bound.call(json!("x")).await.unwrap_err().kind(),
        ErrorType::InvalidRequest
    );
    bound.close().await.unwrap(); // nothing to release: the handles ended with the connection
    assert_eq!(
        ops(&server.received.lock().unwrap()),
        ["hello", "auth", "bind"]
    );
    server.close();
}

fn without_timing(decision: &Decision) -> Json {
    let mut json = decision.to_json().unwrap();
    json.as_object_mut().unwrap().shift_remove("timing");
    json
}

#[tokio::test]
async fn concurrent_requests_share_one_channel_and_each_gets_its_own_answer_whatever_order_the_replies_come_in()
 {
    let inputs: Vec<String> = (0..160)
        .map(|index| {
            format!(
                "message {index}{}",
                if index % 3 == 0 { " unsure" } else { "" }
            )
        })
        .collect();
    let local = Jeff::new(
        Arc::new(FakeEngine::new(hashed_answers, INSTALLED)),
        Catalogue::shipped().unwrap(),
    )
    .await
    .unwrap();
    let expected_bound = local
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let mut expected = HashMap::new();
    for input in &inputs {
        expected.insert(
            input.clone(),
            without_timing(&expected_bound.call(json!(input)).await.unwrap()),
        );
    }
    // 160 calls, 2 call_many and 2 decide: every reply is held until all 164 are ready, then sent in reverse order
    let server = start_channel_server(ChannelOptions {
        reorder: Some(164),
        hashed: true,
        ..Default::default()
    })
    .await;
    let jeff = open(&server.url, secret(), 10000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let calls = futures_util::future::join_all(inputs.iter().map(|input| bound.call(json!(input))));
    let many = futures_util::future::join_all(
        [inputs[..5].to_vec(), inputs[5..12].to_vec()]
            .map(|batch| bound.many(batch.into_iter().map(Json::from).collect())),
    );
    let decided = futures_util::future::join_all([12, 13].map(|index| {
        let (jeff, call) = (jeff.clone(), decide_call(&inputs[index]));
        async move { jeff.decide(&[call]).await }
    }));
    let (calls, many, decided) = tokio::join!(calls, many, decided);
    for (decision, input) in calls.into_iter().zip(&inputs) {
        assert_eq!(without_timing(&decision.unwrap()), expected[input]);
    }
    let many: Vec<Decision> = many.into_iter().flat_map(|batch| batch.unwrap()).collect();
    for (decision, input) in many.iter().zip(&inputs[..12]) {
        assert_eq!(without_timing(decision), expected[input]);
    }
    for (decisions, index) in decided.into_iter().zip([12, 13]) {
        assert_eq!(
            without_timing(&decisions.unwrap()[0]),
            expected[&inputs[index]]
        );
    }
    assert_eq!(server.headers.lock().unwrap().len(), 1);
    let ids: Vec<u64> = server
        .received
        .lock()
        .unwrap()
        .iter()
        .filter(|message| message["op"] != "hello")
        .map(|message| message["id"].as_u64().unwrap())
        .collect();
    let unique: std::collections::BTreeSet<&u64> = ids.iter().collect();
    assert_eq!((unique.len(), ids.len()), (ids.len(), 2 + 164));
    jeff.close().await;
    server.close();
}

async fn until(condition: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !condition() {
        assert!(Instant::now() < deadline, "condition not met in time");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

#[tokio::test]
async fn requests_waiting_when_the_channel_drops_each_fail_once_and_so_does_every_later_one() {
    let server = start_channel_server(ChannelOptions {
        gated: vec!["call"],
        ..Default::default()
    })
    .await;
    let jeff = open(&server.url, secret(), 5000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let waiting: Vec<_> = (0..4)
        .map(|index| {
            let bound = bound.clone();
            tokio::spawn(async move { bound.call(json!(format!("pending {index}"))).await })
        })
        .collect();
    until(|| server.waiting() == 4).await;
    server.drop_connections();
    let closed = format!(
        "{}: the channel closed (no close frame received)",
        channel_url(&server.url).unwrap()
    );
    for task in waiting {
        let error: JeffError = task.await.unwrap().unwrap_err();
        assert_eq!(
            (error.kind(), error.message()),
            (ErrorType::BackendUnavailable, closed.as_str())
        );
    }
    assert_eq!(
        bound.call(json!("later")).await.unwrap_err().message(),
        closed
    );
    server.open_gate();
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn the_257th_outstanding_request_is_rate_limited_and_the_channel_stays_open() {
    let server = start_channel_server(ChannelOptions {
        gated: vec!["call"],
        ..Default::default()
    })
    .await;
    let jeff = open(&server.url, secret(), 10000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let tasks: Vec<_> = (0..257)
        .map(|index| {
            let bound = bound.clone();
            tokio::spawn(async move { bound.call(json!(format!("call {index}"))).await })
        })
        .collect();
    until(|| server.waiting() == 256).await;
    until(|| tasks.iter().any(|task| task.is_finished())).await;
    server.open_gate();
    let mut limited = Vec::new();
    let mut values = Vec::new();
    for task in tasks {
        match task.await.unwrap() {
            Ok(decision) => values.push(decision.value),
            Err(error) => limited.push(error),
        }
    }
    assert_eq!(limited.len(), 1);
    assert_eq!(limited[0].kind(), ErrorType::RateLimited);
    assert_eq!(limited[0].retry_after_s().unwrap(), 1.0);
    assert_eq!(limited[0].details()["channel_id"], json!("0badc0de"));
    assert_eq!(values, vec![jeff_sdk::Value::Int(2); 256]);
    assert!(!jeff.channel_transport().unwrap().closed());
    assert_eq!(
        bound.call(json!("after")).await.unwrap().value,
        jeff_sdk::Value::Int(2)
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_request_over_50_mib_is_refused_before_it_is_sent_and_the_channel_stays_open() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 5000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let error = bound
        .call(json!("x".repeat(50 * 1024 * 1024)))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::InvalidRequest);
    assert!(
        error.message().starts_with("A 'call' frame of 52428")
            && error
                .message()
                .ends_with(" bytes is over the channel's limit of 52428800 bytes (50 MiB)"),
        "{}",
        error.message()
    );
    assert_eq!(
        bound.call(json!("Fire")).await.unwrap().value,
        jeff_sdk::Value::Int(2)
    );
    assert_eq!(
        ops(&server.received.lock().unwrap()),
        ["hello", "auth", "bind", "call"]
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_caller_that_stops_waiting_leaves_no_half_message_and_its_late_reply_is_dropped() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 5000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let big = json!("y".repeat(8 * 1024 * 1024));
    // the caller gives up long before its 8 MiB message can have been written
    assert!(
        tokio::time::timeout(Duration::from_millis(1), bound.call(big.clone()))
            .await
            .is_err()
    );
    assert_eq!(
        bound.call(json!("Fire")).await.unwrap().value,
        jeff_sdk::Value::Int(2)
    );
    // the server read the whole message (a half message would not unpack) and its reply was dropped
    let calls: Vec<Json> = server
        .received
        .lock()
        .unwrap()
        .iter()
        .filter(|message| message["op"] == "call")
        .cloned()
        .collect();
    assert_eq!((calls.len(), &calls[0]["input"]), (2, &big));
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(!jeff.channel_transport().unwrap().closed());
    assert_eq!(
        bound.call(json!("Fire")).await.unwrap().value,
        jeff_sdk::Value::Int(2)
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn a_request_whose_caller_gave_up_before_its_turn_to_be_written_is_never_sent() {
    let server = start_channel_server(ChannelOptions::default()).await;
    let jeff = open(&server.url, secret(), 5000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let big = json!("y".repeat(8 * 1024 * 1024));
    // both are queued at once; the small one's caller gives up while the 8 MiB message is still being written
    let (first, second) = tokio::join!(
        bound.call(big.clone()),
        tokio::time::timeout(Duration::from_millis(1), bound.call(json!("given up")))
    );
    assert_eq!(first.unwrap().value, jeff_sdk::Value::Int(2));
    assert!(second.is_err());
    assert_eq!(
        bound.call(json!("Fire")).await.unwrap().value,
        jeff_sdk::Value::Int(2)
    );
    let inputs: Vec<Json> = server
        .received
        .lock()
        .unwrap()
        .iter()
        .filter(|message| message["op"] == "call")
        .map(|message| message["input"].clone())
        .collect();
    assert_eq!(inputs, [big, json!("Fire")]);
    jeff.close().await;
    server.close();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_reply_nested_10000_levels_deep_closes_the_channel_by_name_without_overflowing_a_worker_stack()
 {
    for map in [false, true] {
        let server = start_channel_server(ChannelOptions {
            silent: vec!["decide"],
            ..Default::default()
        })
        .await;
        let jeff = open(&server.url, secret(), 5000).await.unwrap();
        let waiting = {
            let jeff = jeff.clone();
            tokio::spawn(async move { jeff.decide(&[decide_call("x")]).await })
        };
        until(|| ops(&server.received.lock().unwrap()).contains(&"decide".to_owned())).await;
        let opening: &[u8] = if map { &[0x81, 0xa1, b'k'] } else { &[0x91] };
        let mut body = opening.repeat(10_000);
        body.push(0xc0);
        server.send_raw(body);
        let error = waiting.await.unwrap().unwrap_err();
        assert_eq!((error.kind(), error.message()), (ErrorType::EngineError, format!(
            "The channel {} sent a frame that does not match the socket schema: Socket frame is not valid msgpack: the value nests more than 256 arrays and maps inside one another",
            channel_url(&server.url).unwrap()).as_str()));
        jeff.close().await;
        server.close();
    }
}

/// A channel server that answers hello and auth, then never reads again (its socket buffers fill up).
async fn start_deaf_server() -> (String, tokio::task::JoinHandle<()>) {
    use futures_util::{SinkExt, StreamExt};
    use jeff_sdk::msgpack::{pack, unpack};
    use tokio_tungstenite::tungstenite::Message;
    support::no_proxies();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        let reply = |message: Json| {
            let body = pack(&message).unwrap();
            let mut frame = (body.len() as u32).to_be_bytes().to_vec();
            frame.extend(body);
            Message::Binary(frame.into())
        };
        for _ in 0..2 {
            let Some(Ok(Message::Binary(data))) = ws.next().await else {
                return;
            };
            let message = unpack(&data[4..]).unwrap();
            let answer = if message["op"] == "hello" {
                json!({"op": "hello", "version": 2})
            } else {
                json!({"id": message["id"], "ok": true, "result": {}})
            };
            ws.send(reply(answer)).await.unwrap();
        }
        std::future::pending::<()>().await;
        drop(ws);
    });
    (url, task)
}

#[tokio::test]
async fn close_ends_promptly_while_a_write_is_stuck_and_the_waiting_request_fails_by_name() {
    let (url, deaf) = start_deaf_server().await;
    let transport = Arc::new(
        jeff_sdk::ChannelTransport::open(&url, secret(), 5000)
            .await
            .unwrap(),
    );
    let stuck = {
        let transport = Arc::clone(&transport);
        tokio::spawn(async move { transport.call(1, json!("z".repeat(40 * 1024 * 1024))).await })
    };
    tokio::time::sleep(Duration::from_millis(300)).await;
    let started = Instant::now();
    transport.close().await;
    assert!(
        started.elapsed() < Duration::from_millis(1000),
        "close took {:?}",
        started.elapsed()
    );
    let error = stuck.await.unwrap().unwrap_err();
    assert_eq!(
        (error.kind(), error.message()),
        (
            ErrorType::InvalidRequest,
            format!("This remote client of {url} is closed; connect again with Jeff.remote")
                .as_str()
        )
    );
    deaf.abort();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_send_with_no_bytes_accepted_for_the_timeout_ends_the_channel_for_every_waiting_request()
{
    let server = start_channel_server(ChannelOptions::default()).await;
    let relay = support::relay::start_relay(&server.url).await;
    let jeff = open(&relay.url, secret(), 1000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    relay.paused.store(true, Ordering::SeqCst);
    let started = Instant::now();
    let big = {
        let bound = bound.clone();
        tokio::spawn(async move { bound.call(json!("x".repeat(20 * 1024 * 1024))).await })
    };
    tokio::time::sleep(Duration::from_millis(200)).await; // the big message is filling the socket buffers
    let small: Vec<_> = (0..3)
        .map(|_| {
            let bound = bound.clone();
            tokio::spawn(async move { bound.call(json!("hi")).await })
        })
        .collect();
    let stalled = format!(
        "{}: the socket accepted no bytes of a frame for 1000 ms (the server or the network stopped taking data); the channel closed",
        channel_url(&relay.url).unwrap()
    );
    let error = big.await.unwrap().unwrap_err();
    let elapsed = started.elapsed();
    assert_eq!(
        (error.kind(), error.message()),
        (ErrorType::BackendUnavailable, stalled.as_str())
    );
    for task in small {
        let error = task.await.unwrap().unwrap_err();
        assert_eq!(
            (error.kind(), error.message()),
            (ErrorType::BackendUnavailable, stalled.as_str())
        );
    }
    assert!(
        Duration::from_millis(1000) <= elapsed && elapsed < Duration::from_millis(4000),
        "{elapsed:?}"
    );
    let closing = Instant::now();
    jeff.close().await;
    assert!(
        closing.elapsed() < Duration::from_millis(2000),
        "{:?}",
        closing.elapsed()
    );
    relay.close();
    server.close();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_message_on_a_slow_but_steady_link_takes_as_long_as_it_needs_and_its_reply_wait_starts_once_sent()
 {
    let server = start_channel_server(ChannelOptions::default()).await;
    let relay = support::relay::start_relay(&server.url).await;
    let jeff = open(&relay.url, secret(), 1000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    relay.rate.store(2 * 1024 * 1024, Ordering::SeqCst); // 2 MiB/s
    let started = Instant::now();
    assert_eq!(
        bound
            .call(json!("x".repeat(6 * 1024 * 1024)))
            .await
            .unwrap()
            .value,
        jeff_sdk::Value::Int(2)
    );
    assert!(
        started.elapsed() > Duration::from_millis(2500),
        "{:?}",
        started.elapsed()
    ); // several times timeout_ms
    jeff.close().await;
    relay.close();
    server.close();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_reply_arriving_while_a_long_upload_is_in_progress_is_handed_over_at_once() {
    let server = start_channel_server(ChannelOptions {
        gated: vec!["call"],
        ..Default::default()
    })
    .await;
    let relay = support::relay::start_relay(&server.url).await;
    let jeff = open(&relay.url, secret(), 5000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    let first = {
        let bound = bound.clone();
        tokio::spawn(async move { bound.call(json!("Fire")).await })
    };
    until(|| server.waiting() == 1).await;
    relay.rate.store(2 * 1024 * 1024, Ordering::SeqCst); // the 6 MiB message takes about 3 s
    let big = {
        let bound = bound.clone();
        tokio::spawn(async move { bound.call(json!("x".repeat(6 * 1024 * 1024))).await })
    };
    tokio::time::sleep(Duration::from_millis(800)).await;
    let opened = Instant::now();
    server.open_gate();
    assert_eq!(first.await.unwrap().unwrap().value, jeff_sdk::Value::Int(2));
    assert!(
        opened.elapsed() < Duration::from_millis(500),
        "the reply waited {:?}",
        opened.elapsed()
    );
    assert!(!big.is_finished());
    assert_eq!(big.await.unwrap().unwrap().value, jeff_sdk::Value::Int(2));
    jeff.close().await;
    relay.close();
    server.close();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_request_that_cannot_start_sending_within_the_timeout_is_deadline_exceeded_and_never_sent()
 {
    let server = start_channel_server(ChannelOptions::default()).await;
    let relay = support::relay::start_relay(&server.url).await;
    let jeff = open(&relay.url, secret(), 1000).await.unwrap();
    let bound = jeff
        .function("triage.urgency", bind_options())
        .await
        .unwrap();
    relay.rate.store(2 * 1024 * 1024, Ordering::SeqCst); // the 6 MiB message takes about 3 s, steadily
    let big = {
        let bound = bound.clone();
        tokio::spawn(async move { bound.call(json!("x".repeat(6 * 1024 * 1024))).await })
    };
    tokio::time::sleep(Duration::from_millis(300)).await;
    let started = Instant::now();
    let error = bound.call(json!("behind")).await.unwrap_err();
    assert_eq!((error.kind(), error.message()), (ErrorType::DeadlineExceeded, format!(
        "The channel {} could not start sending 'call' within 1000 ms, as earlier messages were still being written; it was not sent",
        channel_url(&relay.url).unwrap()).as_str()));
    assert!(
        (990..1500).contains(&started.elapsed().as_millis()),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(big.await.unwrap().unwrap().value, jeff_sdk::Value::Int(2));
    let inputs: Vec<Json> = server
        .received
        .lock()
        .unwrap()
        .iter()
        .filter(|message| message["op"] == "call")
        .map(|message| message["input"].clone())
        .collect();
    assert_eq!(inputs.len(), 1); // "behind" was never sent
    jeff.close().await;
    relay.close();
    server.close();
}

#[test]
fn closing_or_dropping_a_channel_jeff_leaves_no_task_running() {
    let server_runtime = tokio::runtime::Runtime::new().unwrap();
    let server = server_runtime.block_on(start_channel_server(ChannelOptions::default()));
    for close in [true, false] {
        let client = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        client.block_on(async {
            // a token function's JWT: the refresher task runs too
            let token = jwt(now() + 3600);
            let source = TokenSource::function(move || {
                let token = token.clone();
                async move { Ok::<String, BoxError>(token) }
            });
            let jeff = open(&server.url, source, 2000).await.unwrap();
            let bound = jeff
                .function("triage.urgency", bind_options())
                .await
                .unwrap();
            bound.call(json!("Fire")).await.unwrap();
            assert!(
                tokio::runtime::Handle::current()
                    .metrics()
                    .num_alive_tasks()
                    >= 3
            );
            if close {
                jeff.close().await;
            }
            drop(bound);
            drop(jeff);
            tokio::time::sleep(Duration::from_millis(100)).await;
        });
        assert_eq!(
            client.metrics().num_alive_tasks(),
            0,
            "after {}",
            if close { "close" } else { "drop" }
        );
    }
    server.close();
}
