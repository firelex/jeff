//! socket.json: byte-exact conversations with the engine. The scripted engine plays the case's steps on the first
//! connection and records every frame it reads.

mod support;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use jeff_sdk::binding::Row;
use jeff_sdk::pyformat::py_sorted;
use jeff_sdk::socket_engine::SocketEngine;
use jeff_sdk::{Engine, JeffError, Json};
use serde_json::json;
use support::engine_server::socket_directory;
use support::{b64, fixture, from_b64};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixListener;

struct Scripted {
    path: String,
    received: Arc<Mutex<Vec<String>>>,
    done: tokio::task::JoinHandle<()>,
    _directory: tempfile::TempDir,
}

async fn scripted(steps: Vec<Json>) -> Scripted {
    let directory = socket_directory();
    let path = directory.path().join("e.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let received: Arc<Mutex<Vec<String>>> = Default::default();
    let record = Arc::clone(&received);
    let done = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        for step in steps {
            if step.get("client").is_some() {
                let read = tokio::time::timeout(Duration::from_secs(5), async {
                    let mut header = [0u8; 4];
                    stream.read_exact(&mut header).await.ok()?;
                    let mut body = vec![0u8; u32::from_be_bytes(header) as usize];
                    stream.read_exact(&mut body).await.ok()?;
                    let mut frame = header.to_vec();
                    frame.extend(body);
                    Some(frame)
                })
                .await;
                match read {
                    Ok(Some(frame)) => record.lock().unwrap().push(b64(&frame)),
                    Ok(None) => {
                        record.lock().unwrap().push("null".into());
                        break;
                    }
                    Err(_) => {
                        record.lock().unwrap().push("<timeout>".into());
                        break;
                    }
                }
            } else if let Some(bytes) = step.get("engine") {
                if stream
                    .write_all(&from_b64(bytes.as_str().unwrap()))
                    .await
                    .is_err()
                {
                    break;
                }
            } else {
                break; // {"close": true}: the stream is dropped below
            }
        }
    });
    Scripted {
        path: path.display().to_string(),
        received,
        done,
        _directory: directory,
    }
}

fn row_of(action: &Json) -> Vec<Row> {
    if let Some(rows) = action.get("rows") {
        return rows
            .as_array()
            .unwrap()
            .iter()
            .map(|row| Row::from_json(row).unwrap())
            .collect();
    }
    let generated = &action["rows_generated"];
    let state = "x".repeat(generated["state_length"].as_u64().unwrap() as usize);
    vec![Row::from_json(&json!({"state": state, "question": generated["question"]})).unwrap()]
}

fn failed(error: &JeffError, path: &str) -> Json {
    json!({"error": {"type": error.type_name(), "message": error.message().replace(path, "{socket}")}})
}

async fn act(engine: Arc<SocketEngine>, action: Json, path: String) -> Json {
    let outcome = match action["op"].as_str().unwrap() {
        "predict" => engine.predict_with_deadline(action["adapter"].as_str().unwrap(), &row_of(&action), action.get("deadline_ms").and_then(Json::as_u64)).await
            .map(|result| json!({"probabilities": result.probabilities, "timing": result.timing, "input_tokens": result.input_tokens})),
        "installed" => engine.installed(true).await.map(|installed| json!({"installed": py_sorted(installed)})),
        "health" => engine.health().await.map(Json::Object),
        other => panic!("unknown socket action {other}"),
    };
    outcome
        .map(|result| json!({"result": result}))
        .unwrap_or_else(|error| failed(&error, &path))
}

#[tokio::test]
async fn every_socket_conversation_is_the_recorded_one() {
    let mut failures = Vec::new();
    for case in fixture("socket.json")["cases"].as_array().unwrap() {
        let steps = case["steps"].as_array().unwrap().clone();
        let server = scripted(steps.clone()).await;
        let actions = case["actions"].as_array().unwrap();
        let mut results = Vec::new();
        let engine = match SocketEngine::connect(&server.path, 5000).await {
            Ok(engine) => {
                let (id, version) = engine.base();
                results.push(json!({"result": {"base_format": engine.base_format().to_json(), "base": [id, version],
                    "installed": py_sorted(engine.installed(false).await.unwrap())}}));
                Some(Arc::new(engine))
            }
            Err(error) => {
                results.push(failed(&error, &server.path));
                None
            }
        };
        if let Some(engine) = &engine {
            let mut index = 1;
            while index < actions.len() {
                let mut group = vec![index];
                while actions[*group.last().unwrap()]["concurrent"] == true
                    && actions
                        .get(group.last().unwrap() + 1)
                        .is_some_and(|next| next["concurrent"] == true)
                {
                    group.push(group.last().unwrap() + 1);
                }
                let mut running = Vec::new();
                for member in &group {
                    let before = server.received.lock().unwrap().len();
                    running.push(tokio::spawn(act(
                        Arc::clone(engine),
                        actions[*member].clone(),
                        server.path.clone(),
                    )));
                    if group.len() > 1 {
                        // start the next one only after the engine has read this one's frame
                        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
                        while server.received.lock().unwrap().len() == before
                            && tokio::time::Instant::now() < deadline
                        {
                            tokio::time::sleep(Duration::from_millis(2)).await;
                        }
                    }
                }
                for task in running {
                    results.push(task.await.unwrap());
                }
                index = group.last().unwrap() + 1;
            }
            engine.close().await;
        }
        let _ = tokio::time::timeout(Duration::from_secs(5), server.done).await;
        let expected_frames: Vec<String> = steps
            .iter()
            .filter_map(|step| {
                step.get("client")
                    .map(|frame| frame.as_str().unwrap().to_owned())
            })
            .collect();
        let received = server.received.lock().unwrap().clone();
        if Json::Array(results.clone()) != case["results"] || received != expected_frames {
            failures.push(format!("{}\n  results  {}\n  expected {}\n  frames   {received:?}\n  expected {expected_frames:?}",
                case["name"], Json::Array(results), case["results"]));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
