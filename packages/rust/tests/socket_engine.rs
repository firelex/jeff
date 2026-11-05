mod support;

use std::sync::Arc;
use std::time::Duration;

use jeff_sdk::binding::{Call, Row};
use jeff_sdk::protocol::FrameReader;
use jeff_sdk::socket_engine::SocketEngine;
use jeff_sdk::{Engine, ErrorType, Jeff, Json, LocalOptions, Value};
use serde_json::json;
use support::engine_server::{EngineServerOptions, answering, frame, map, socket_directory, start};
use tokio::io::AsyncWriteExt;

fn level_of(_adapter: &str, row: &Json) -> Json {
    let message = row["state"]["message"].as_str().unwrap();
    let level: usize = message[message.len() - 1..].parse().unwrap();
    Json::from(
        (0..5)
            .map(|index| if index == level { 1.0 } else { 0.0 })
            .collect::<Vec<f64>>(),
    )
}

fn context() -> jeff_sdk::JsonMap {
    map(json!({"company": "A shop.", "channel": "email"}))
}

fn noul_row() -> Row {
    Row::from_json(&json!({"state": "x", "question": {"type": "noul", "instructions": "?"}}))
        .unwrap()
}

#[tokio::test]
async fn jeff_local_decides_over_the_engine_socket() {
    let server = start(answering(level_of, &["jeff.support.triage"])).await;
    let jeff = Jeff::local(&server.path, LocalOptions::new(5000))
        .await
        .unwrap();
    let decisions = jeff
        .decide(&[Call::new("triage.urgency", json!("level 3")).with_context(context())])
        .await
        .unwrap();
    assert_eq!(decisions[0].value, Value::Int(3));
    jeff.close().await;
    server.close();
}

#[tokio::test(flavor = "current_thread")]
async fn jeff_local_works_on_a_current_thread_runtime() {
    let server = start(answering(level_of, &["jeff.support.triage"])).await;
    let jeff = Jeff::local(&server.path, LocalOptions::new(5000))
        .await
        .unwrap();
    let urgency = jeff
        .function(
            "triage.urgency",
            jeff_sdk::CallOptions {
                context: Some(context()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(
        urgency.call(json!("level 2")).await.unwrap().value,
        Value::Int(2)
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn concurrent_callers_share_one_connection_and_each_gets_its_own_answer() {
    let server = start(answering(level_of, &["jeff.support.triage"])).await;
    let jeff = Jeff::local(&server.path, LocalOptions::new(5000))
        .await
        .unwrap();
    let inputs: Vec<String> = (0..40).map(|index| format!("case {}", index % 5)).collect();
    let tasks: Vec<_> = inputs
        .iter()
        .map(|input| {
            let (jeff, input) = (jeff.clone(), input.clone());
            tokio::spawn(async move {
                jeff.decide(&[Call::new("triage.urgency", json!(input)).with_context(context())])
                    .await
                    .unwrap()[0]
                    .value
                    .clone()
            })
        })
        .collect();
    for (task, input) in tasks.into_iter().zip(&inputs) {
        assert_eq!(
            task.await.unwrap(),
            Value::Int(input[input.len() - 1..].parse().unwrap())
        );
    }
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn an_engine_answering_integer_probabilities_decides_as_with_floats() {
    let server = start(answering(|_, _| json!([1, 0]), &["jeff.safety.spam"])).await;
    let engine = SocketEngine::connect(&server.path, 5000).await.unwrap();
    assert_eq!(
        engine
            .predict("jeff.safety.spam", &[noul_row()])
            .await
            .unwrap()
            .probabilities,
        vec![vec![1.0, 0.0]]
    );
    engine.close().await;
    let jeff = Jeff::local(&server.path, LocalOptions::new(5000))
        .await
        .unwrap();
    let decision = &jeff
        .decide(&[
            Call::new("spam.is_spam", json!("Hi")).with_context(map(json!({"channel": "sms"})))
        ])
        .await
        .unwrap()[0];
    assert_eq!(
        (decision.value.clone(), decision.confidence),
        (Value::Bool(false), Some(1.0))
    );
    jeff.close().await;
    server.close();
}

#[tokio::test]
async fn no_engine_on_the_socket_names_the_path_and_the_command_that_starts_one() {
    let error = Jeff::local("/tmp/jeff-no-such-engine.sock", LocalOptions::new(1000))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::EngineError);
    assert!(
        error
            .message()
            .starts_with("No engine is listening at /tmp/jeff-no-such-engine.sock: "),
        "{}",
        error.message()
    );
    assert!(error.message().ends_with("Start one with: jeff runtime serve --socket /tmp/jeff-no-such-engine.sock --adapter \
        <adapter id> --batch-window-ms <milliseconds> --max-batch-rows <rows> --queue-limit-rows <rows> --prepare-workers <threads>"));
    assert_eq!(
        Jeff::local("/tmp/x.sock", LocalOptions::new(0))
            .await
            .unwrap_err()
            .kind(),
        ErrorType::InvalidRequest
    );
}

#[tokio::test]
async fn a_call_without_a_reply_within_the_timeout_fails_promptly_naming_the_engine() {
    let server = start(answering(|_, _| json!([1.0, 0.0]), &["jeff.safety.spam"])).await;
    let engine = SocketEngine::connect(&server.path, 200).await.unwrap();
    server.stop_answering();
    let started = std::time::Instant::now();
    let error = engine
        .predict("jeff.safety.spam", &[noul_row()])
        .await
        .unwrap_err();
    let elapsed = started.elapsed().as_millis();
    assert_eq!(
        error.message(),
        format!(
            "No reply from the engine at {} within 200 ms",
            server.path.display()
        )
    );
    assert!((190..400).contains(&elapsed), "{elapsed} ms");
    engine.close().await;
    server.close();
}

#[tokio::test]
async fn after_the_engine_restarts_the_next_call_reconnects_once_and_a_call_right_after_a_failed_attempt_fails_at_once()
 {
    let directory = socket_directory();
    let path = directory.path().join("e.sock");
    let options = || EngineServerOptions {
        path: Some(path.clone()),
        ..answering(|_, _| json!([0.2, 0.8]), &["jeff.safety.spam"])
    };
    let first = start(options()).await;
    let engine = SocketEngine::connect(&path, 2000).await.unwrap();
    first.close();
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        engine
            .predict("jeff.safety.spam", &[noul_row()])
            .await
            .unwrap_err()
            .message()
            .ends_with("(reconnect attempt)")
    );
    assert!(
        engine
            .predict("jeff.safety.spam", &[noul_row()])
            .await
            .unwrap_err()
            .message()
            .contains("the last reconnect attempt")
    );
    tokio::time::sleep(Duration::from_millis(1100)).await;
    let second = start(options()).await;
    assert_eq!(
        engine
            .predict("jeff.safety.spam", &[noul_row()])
            .await
            .unwrap()
            .probabilities,
        vec![vec![0.2, 0.8]]
    );
    assert_eq!(engine.reconnects(), 1);
    engine.close().await;
    second.close();
}

#[tokio::test]
async fn a_closed_engine_refuses_later_calls() {
    let server = start(answering(|_, _| json!([1.0, 0.0]), &[])).await;
    let engine = SocketEngine::connect(&server.path, 1000).await.unwrap();
    engine.close().await;
    assert!(
        engine
            .health()
            .await
            .unwrap_err()
            .message()
            .contains("was closed by its caller")
    );
    server.close();
}

/// An engine that writes `on_hello` after reading the hello and `on_installed(id)` after reading an installed
/// request; it counts connections and records each op it reads.
async fn raw_engine(
    on_hello: Vec<u8>,
    on_installed: Option<fn(u64) -> Vec<u8>>,
) -> (
    tempfile::TempDir,
    std::path::PathBuf,
    Arc<std::sync::Mutex<Vec<String>>>,
    Arc<std::sync::atomic::AtomicUsize>,
) {
    let directory = socket_directory();
    let path = directory.path().join("e.sock");
    let listener = tokio::net::UnixListener::bind(&path).unwrap();
    let ops: Arc<std::sync::Mutex<Vec<String>>> = Default::default();
    let connections = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let (record, count) = (Arc::clone(&ops), Arc::clone(&connections));
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            count.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let (read, mut write) = stream.into_split();
            let (record, on_hello) = (Arc::clone(&record), on_hello.clone());
            tokio::spawn(async move {
                let mut reader = FrameReader::new(read);
                while let Ok(body) = reader.next().await {
                    let message = jeff_sdk::msgpack::unpack(&body).unwrap();
                    let op = message["op"].as_str().unwrap().to_owned();
                    record.lock().unwrap().push(op.clone());
                    let reply = match (op.as_str(), on_installed) {
                        ("hello", _) => on_hello.clone(),
                        ("installed", Some(make)) => make(message["id"].as_u64().unwrap()),
                        _ => continue,
                    };
                    if write.write_all(&reply).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    (directory, path, ops, connections)
}

fn engine_hello() -> Vec<u8> {
    frame(&json!({"op": "hello", "version": 2, "adapters_version": 1}))
}

fn id_zero_failure() -> Vec<u8> {
    frame(
        &json!({"id": 0, "ok": false, "error": {"type": "InvalidRequest", "message": "Frame could not be read", "details": {"adapters_version": 1}}}),
    )
}

#[tokio::test]
async fn a_failure_with_id_0_in_the_same_bytes_as_the_hello_fails_the_connect_without_a_reconnect()
{
    let mut bytes = engine_hello();
    bytes.extend(id_zero_failure());
    let (_directory, path, _ops, connections) = raw_engine(bytes, None).await;
    let error = SocketEngine::connect(&path, 2000).await.unwrap_err();
    assert_eq!(
        error.message(),
        format!(
            "The engine at {} closed the connection after: InvalidRequest: Frame could not be read",
            path.display()
        )
    );
    assert_eq!(connections.load(std::sync::atomic::Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_connection_that_closes_while_connecting_fails_the_connect_at_once_without_a_reconnect() {
    fn installed_then_failure(id: u64) -> Vec<u8> {
        let mut bytes = frame(
            &json!({"id": id, "ok": true, "result": {"adapters": ["base"], "adapters_version": 1}}),
        );
        bytes.extend(id_zero_failure());
        bytes
    }
    let (_directory, path, ops, connections) =
        raw_engine(engine_hello(), Some(installed_then_failure)).await;
    let error = SocketEngine::connect(&path, 2000).await.unwrap_err();
    assert_eq!(
        error.message(),
        format!(
            "The engine at {} closed the connection after: InvalidRequest: Frame could not be read",
            path.display()
        )
    );
    assert_eq!(*ops.lock().unwrap(), ["hello", "installed"]);
    assert_eq!(connections.load(std::sync::atomic::Ordering::SeqCst), 1);
}

#[tokio::test]
async fn connecting_and_closing_can_run_on_a_spawned_task() {
    let server = start(answering(level_of, &["jeff.support.triage"])).await;
    let path = server.path.clone();
    let jeff = tokio::spawn(async move { Jeff::local(path, LocalOptions::new(5000)).await })
        .await
        .unwrap()
        .unwrap();
    let closing = jeff.clone();
    tokio::spawn(async move { closing.close().await })
        .await
        .unwrap();
    server.close();
}

#[tokio::test]
async fn the_engine_closing_fails_every_call_waiting_on_it_at_once() {
    let server = start(answering(|_, _| json!([1.0, 0.0]), &["jeff.safety.spam"])).await;
    let engine = Arc::new(SocketEngine::connect(&server.path, 5000).await.unwrap());
    server.stop_answering();
    let waiting: Vec<_> = (0..5)
        .map(|_| {
            let engine = Arc::clone(&engine);
            tokio::spawn(async move { engine.predict("jeff.safety.spam", &[noul_row()]).await })
        })
        .collect();
    tokio::time::sleep(Duration::from_millis(100)).await;
    let path = server.path.display().to_string();
    let started = std::time::Instant::now();
    server.close();
    for call in waiting {
        let error = call.await.unwrap().unwrap_err();
        assert_eq!(
            error.message(),
            format!("The engine at {path} closed the connection")
        );
    }
    assert!(started.elapsed() < Duration::from_secs(1));
    engine.close().await;
}

/// An engine that answers the connect's hello, installed and health. After those, it stops reading for good when
/// `stop_reading` is set; otherwise it keeps answering, reading any body over 1 MiB slowly (64 KiB every 5 ms).
async fn stubborn_engine(stop_reading: bool) -> (tempfile::TempDir, std::path::PathBuf) {
    use tokio::io::AsyncReadExt;
    let directory = socket_directory();
    let path = directory.path().join("e.sock");
    let listener = tokio::net::UnixListener::bind(&path).unwrap();
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut read = 0;
        loop {
            if stop_reading && read == 3 {
                tokio::time::sleep(Duration::from_secs(3600)).await;
            }
            let mut header = [0u8; 4];
            if stream.read_exact(&mut header).await.is_err() {
                return;
            }
            let length = u32::from_be_bytes(header) as usize;
            let mut body = vec![0u8; length];
            let mut at = 0;
            while at < length {
                let end = if length > 1 << 20 {
                    (at + (64 << 10)).min(length)
                } else {
                    length
                };
                if stream.read_exact(&mut body[at..end]).await.is_err() {
                    return;
                }
                at = end;
                if at < length {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            }
            read += 1;
            let message = jeff_sdk::msgpack::unpack(&body).unwrap();
            let id = message.get("id").cloned().unwrap_or(Json::Null);
            let reply = match message["op"].as_str().unwrap() {
                "hello" => json!({"op": "hello", "version": 2, "adapters_version": 1}),
                "installed" => {
                    json!({"id": id, "ok": true, "result": {"adapters": ["base", "jeff.safety.spam"], "adapters_version": 1}})
                }
                "health" => {
                    json!({"id": id, "ok": true, "result": {"status": "ready", "adapters_version": 1,
                    "base": {"id": "jeff-base", "version": "v1.3", "format": "bf16"}}})
                }
                "predict" => json!({"id": id, "ok": true, "result": {"probabilities": [[1.0, 0.0]],
                    "timing": {"path": "fake"}, "adapters_version": 1}}),
                other => panic!("unexpected op {other}"),
            };
            if stream.write_all(&frame(&reply)).await.is_err() {
                return;
            }
        }
    });
    (directory, path)
}

fn big_row(bytes: usize) -> Row {
    Row::from_json(
        &json!({"state": "x".repeat(bytes), "question": {"type": "noul", "instructions": "?"}}),
    )
    .unwrap()
}

#[tokio::test]
async fn close_fails_a_call_whose_write_is_stuck_on_an_engine_that_stopped_reading_and_every_call_behind_it()
 {
    let (_directory, path) = stubborn_engine(true).await;
    let engine = Arc::new(SocketEngine::connect(&path, 30_000).await.unwrap());
    let calls: Vec<_> = [8 << 20, 10]
        .into_iter()
        .map(|bytes| {
            let engine = Arc::clone(&engine);
            tokio::spawn(async move { engine.predict("jeff.safety.spam", &[big_row(bytes)]).await })
        })
        .collect();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(
        calls.iter().all(|call| !call.is_finished()),
        "the engine is not reading, so nothing can be answered"
    );
    let started = std::time::Instant::now();
    engine.close().await;
    for call in calls {
        let error = tokio::time::timeout(Duration::from_secs(1), call)
            .await
            .expect("close unblocks the call")
            .unwrap()
            .unwrap_err();
        assert_eq!(
            error.message(),
            format!(
                "The SocketEngine for {} was closed by its caller",
                path.display()
            )
        );
    }
    assert!(started.elapsed() < Duration::from_secs(1));
}

#[tokio::test]
async fn a_caller_cancelled_while_its_large_frame_is_being_written_leaves_the_connection_whole() {
    let (_directory, path) = stubborn_engine(false).await;
    let engine = SocketEngine::connect(&path, 5000).await.unwrap();
    // about 64 reads of 64 KiB, 5 ms apart: still being written when the caller gives up
    let cancelled = tokio::time::timeout(
        Duration::from_millis(50),
        engine.predict("jeff.safety.spam", &[big_row(4 << 20)]),
    )
    .await;
    assert!(cancelled.is_err(), "the caller's own timeout fired first");
    let next = engine
        .predict("jeff.safety.spam", &[noul_row()])
        .await
        .unwrap();
    assert_eq!(next.probabilities, vec![vec![1.0, 0.0]]);
    engine.close().await;
}
