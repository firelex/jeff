//! A fake Jeff server for the channel tests: GET /v1/functions answers the listing recorded in remote.json, and a
//! WebSocket at /v1/channel answers hello, auth, refresh, bind, call, call_many, decide and release over a local Jeff
//! with a fake engine (as jeff-server's channel does).

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use jeff_sdk::binding::{Call, Criteria, Row, RowState};
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::contract::QuestionType;
use jeff_sdk::msgpack::{pack, unpack};
use jeff_sdk::testing::FakeEngine;
use jeff_sdk::{Jeff, Json};
use serde_json::json;
use tokio::io::AsyncWriteExt;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};

use super::http_server::listing;

/// The most requests a connection may have outstanding (contract/socket.md, "Limits").
const MAX_OUTSTANDING: usize = 256;
pub const INSTALLED: [&str; 2] = ["jeff.support.triage", "jeff.safety.spam"];
/// Below triage.urgency's calibration threshold 0.6.
const UNSURE: [f64; 5] = [0.0, 0.5, 0.5, 0.0, 0.0];

/// The fake engine's answers: a score function's level 2, confidently, unless the input contains "unsure"; a choice's
/// first criterion; every other type's first option.
pub fn answers(_adapter: &str, row: &Row) -> Vec<f64> {
    if row.question.question_type == QuestionType::Score {
        let text = match &row.state {
            RowState::Fields(map) => Json::Object(map.clone()).to_string(),
            RowState::Text(text) => text.clone(),
        };
        return if text.contains("unsure") {
            UNSURE.to_vec()
        } else {
            vec![0.0, 0.0, 0.9, 0.1, 0.0]
        };
    }
    let count = match &row.question.criteria {
        None => 2,
        Some(Criteria::Described(map)) => map.len(),
        Some(Criteria::Levels(levels)) => levels.len(),
    };
    let mut probabilities = vec![0.0; count];
    probabilities[0] = 1.0;
    probabilities
}

/// As `answers`, but a score function's probabilities come from a hash of the row's state, so every input gets its own
/// answer and a reply handed to the wrong request shows.
pub fn hashed_answers(adapter: &str, row: &Row) -> Vec<f64> {
    if row.question.question_type != QuestionType::Score {
        return answers(adapter, row);
    }
    let text = match &row.state {
        RowState::Fields(map) => Json::Object(map.clone()).to_string(),
        RowState::Text(text) => text.clone(),
    };
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in text.bytes() {
        hash = (hash ^ u64::from(byte)).wrapping_mul(0x0100_0000_01b3);
    }
    let weights: Vec<f64> = (0..5)
        .map(|index| 1.0 + ((hash >> (index * 12)) & 0xfff) as f64)
        .collect();
    let total: f64 = weights.iter().sum();
    weights.iter().map(|weight| weight / total).collect()
}

#[derive(Clone, Default)]
pub struct ChannelOptions {
    /// What bind reports for every adapter (the catalogue's version when None).
    pub version: Option<String>,
    /// Ops that get no answer.
    pub silent: Vec<&'static str>,
    /// The only token auth and refresh accept (any token when None).
    pub token: Option<String>,
    /// Ops whose requests wait until open_gate().
    pub gated: Vec<&'static str>,
    /// The replies to call, call_many and decide are held until that many are ready, then sent in reverse order.
    pub reorder: Option<usize>,
    /// Every call, call_many and decide fails with a LowConfidence whose decision does not match the decision schema.
    pub bad_low_confidence_decision: bool,
    /// The fake engine answers with hashed_answers instead of answers.
    pub hashed: bool,
}

pub struct ChannelServer {
    pub url: String,
    pub received: Arc<Mutex<Vec<Json>>>,
    pub headers: Arc<Mutex<Vec<String>>>,
    writers: Arc<Mutex<Vec<mpsc::UnboundedSender<Vec<u8>>>>>,
    gate: tokio::sync::watch::Sender<bool>,
    waiting: Arc<AtomicUsize>,
    tasks: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

fn frame(message: &Json) -> Vec<u8> {
    let body = pack(message).unwrap();
    let mut out = (body.len() as u32).to_be_bytes().to_vec();
    out.extend(body);
    out
}

impl ChannelServer {
    /// Sends `message` as one frame on every open connection.
    pub fn send(&self, message: Json) {
        for writer in self.writers.lock().unwrap().iter() {
            let _ = writer.send(frame(&message));
        }
    }

    /// Sends `body` behind its 4-byte length as one message on every open connection, whatever it holds.
    pub fn send_raw(&self, body: Vec<u8>) {
        let mut frame = (body.len() as u32).to_be_bytes().to_vec();
        frame.extend(body);
        for writer in self.writers.lock().unwrap().iter() {
            let _ = writer.send(frame.clone());
        }
    }

    pub fn open_gate(&self) {
        self.gate.send_replace(true);
    }

    pub fn waiting(&self) -> usize {
        self.waiting.load(Ordering::SeqCst)
    }

    /// Ends every connection at once, without a close frame.
    pub fn drop_connections(&self) {
        for task in self.tasks.lock().unwrap().iter().skip(1) {
            task.abort();
        }
        self.writers.lock().unwrap().clear();
    }

    pub fn close(self) {
        for task in self.tasks.lock().unwrap().drain(..) {
            task.abort();
        }
    }
}

struct Handler {
    options: ChannelOptions,
    jeff: Jeff,
    catalogue: Arc<Catalogue>,
    handles: Mutex<HashMap<u64, Json>>,
    next_handle: AtomicU64,
    held: Mutex<Vec<Vec<u8>>>,
    gate: tokio::sync::watch::Receiver<bool>,
    waiting: Arc<AtomicUsize>,
}

fn fail(id: &Json, kind: &str, message: &str, mut details: serde_json::Map<String, Json>) -> Json {
    details.insert("channel_id".into(), json!("0badc0de"));
    json!({"id": id, "ok": false, "error": {"type": kind, "message": message, "details": details}})
}

impl Handler {
    async fn answer(&self, message: &Json) -> Option<Json> {
        let id = message.get("id").cloned().unwrap_or(Json::Null);
        let op = message["op"].as_str().unwrap();
        if self.options.gated.contains(&op) {
            self.waiting.fetch_add(1, Ordering::SeqCst);
            let mut gate = self.gate.clone();
            let _ = gate.wait_for(|open| *open).await;
            self.waiting.fetch_sub(1, Ordering::SeqCst);
        }
        if self.options.bad_low_confidence_decision && ["call", "call_many", "decide"].contains(&op)
        {
            return Some(fail(
                &id,
                "LowConfidence",
                "abstained",
                json!({"decision": {"value": 1}})
                    .as_object()
                    .unwrap()
                    .clone(),
            ));
        }
        let ok = |result: Json| json!({"id": id, "ok": true, "result": result});
        Some(match op {
            "hello" => json!({"op": "hello", "version": 2}),
            "auth" | "refresh" => match &self.options.token {
                Some(token) if message["token"] != *token => fail(
                    &id,
                    "Unauthorised",
                    "the token is not valid",
                    Default::default(),
                ),
                _ => ok(json!({})),
            },
            "bind" => {
                let handle = self.next_handle.fetch_add(1, Ordering::SeqCst);
                self.handles.lock().unwrap().insert(handle, message.clone());
                let function = self
                    .catalogue
                    .function(message["function"].as_str().unwrap())
                    .unwrap();
                let version = self.options.version.clone().unwrap_or_else(|| {
                    self.catalogue
                        .adapter(&function.adapter_id)
                        .unwrap()
                        .version
                        .clone()
                });
                ok(json!({"handle": handle, "function": message["function"], "version": version}))
            }
            "call" | "call_many" => {
                let handle = message["handle"].as_u64().unwrap();
                let Some(bound) = self.handles.lock().unwrap().get(&handle).cloned() else {
                    return Some(fail(
                        &id,
                        "InvalidRequest",
                        &format!("handle {handle} is not bound"),
                        Default::default(),
                    ));
                };
                let inputs: Vec<Json> = if op == "call" {
                    vec![message["input"].clone()]
                } else {
                    message["inputs"].as_array().unwrap().clone()
                };
                let calls: Vec<Call> = inputs
                    .into_iter()
                    .map(|input| {
                        let mut call = bound.clone();
                        call["input"] = input;
                        Call::from_json(&call).unwrap()
                    })
                    .collect();
                let decisions = self.jeff.decide(&calls).await.unwrap();
                if op == "call" {
                    ok(json!({"decision": decisions[0].to_json().unwrap()}))
                } else {
                    ok(
                        json!({"decisions": decisions.iter().map(|decision| decision.to_json().unwrap()).collect::<Vec<_>>()}),
                    )
                }
            }
            "decide" => {
                let calls: Vec<Call> = message["calls"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|call| Call::from_json(call).unwrap())
                    .collect();
                let decisions = self.jeff.decide(&calls).await.unwrap();
                ok(
                    json!({"decisions": decisions.iter().map(|decision| decision.to_json().unwrap()).collect::<Vec<_>>()}),
                )
            }
            "release" => {
                let handle = message["handle"].as_u64().unwrap();
                if self.handles.lock().unwrap().remove(&handle).is_none() {
                    // remove: not an ordered map
                    fail(
                        &id,
                        "InvalidRequest",
                        &format!("handle {handle} is not bound"),
                        Default::default(),
                    )
                } else {
                    ok(json!({}))
                }
            }
            _ => return None,
        })
    }
}

pub async fn start_channel_server(options: ChannelOptions) -> ChannelServer {
    super::no_proxies();
    let catalogue = Catalogue::shipped().unwrap();
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(
            if options.hashed {
                hashed_answers
            } else {
                answers
            },
            INSTALLED,
        )),
        Arc::clone(&catalogue),
    )
    .await
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let received: Arc<Mutex<Vec<Json>>> = Default::default();
    let headers: Arc<Mutex<Vec<String>>> = Default::default();
    let writers: Arc<Mutex<Vec<mpsc::UnboundedSender<Vec<u8>>>>> = Default::default();
    let (gate, gate_receiver) = tokio::sync::watch::channel(false);
    let waiting = Arc::new(AtomicUsize::new(0));
    let tasks: Arc<Mutex<Vec<JoinHandle<()>>>> = Default::default();
    let handler = Arc::new(Handler {
        options: options.clone(),
        jeff,
        catalogue,
        handles: Mutex::new(HashMap::new()),
        next_handle: AtomicU64::new(1),
        held: Mutex::new(Vec::new()),
        gate: gate_receiver,
        waiting: Arc::clone(&waiting),
    });
    let accept = {
        let (received, headers, writers, tasks_list) = (
            Arc::clone(&received),
            Arc::clone(&headers),
            Arc::clone(&writers),
            Arc::clone(&tasks),
        );
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                let (received, headers, writers, handler) = (
                    Arc::clone(&received),
                    Arc::clone(&headers),
                    Arc::clone(&writers),
                    Arc::clone(&handler),
                );
                let writer_tasks = Arc::clone(&tasks_list);
                let task = tokio::spawn(async move {
                    let mut peeked = vec![0u8; 8192];
                    let head = loop {
                        let count = stream.peek(&mut peeked).await.unwrap_or(0);
                        if count == 0 {
                            return;
                        }
                        let text = String::from_utf8_lossy(&peeked[..count]).to_string();
                        if text.contains("\r\n\r\n") {
                            break text;
                        }
                        tokio::time::sleep(std::time::Duration::from_millis(1)).await;
                    };
                    let path = head.split(' ').nth(1).unwrap_or("").to_owned();
                    let upgrade = head.to_ascii_lowercase().contains("upgrade: websocket");
                    if !upgrade || path != "/v1/channel" {
                        let mut skip = vec![0u8; head.find("\r\n\r\n").unwrap() + 4];
                        let _ = tokio::io::AsyncReadExt::read_exact(&mut stream, &mut skip).await;
                        let reply = if path == "/v1/functions" && !upgrade {
                            let body = listing();
                            format!(
                                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\ncontent-length: {}\r\n\r\n{body}",
                                body.len()
                            )
                        } else if upgrade {
                            "HTTP/1.1 400 Bad Request\r\nconnection: close\r\ncontent-length: 0\r\n\r\n".to_owned()
                        } else {
                            "HTTP/1.1 404 Not Found\r\nconnection: close\r\ncontent-length: 0\r\n\r\n".to_owned()
                        };
                        let _ = stream.write_all(reply.as_bytes()).await;
                        return;
                    }
                    let seen = Arc::clone(&headers);
                    // tungstenite fixes the callback's signature, large error response included
                    #[allow(clippy::result_large_err)]
                    let callback = move |request: &Request, response: Response| {
                        let value = request
                            .headers()
                            .get("authorization")
                            .and_then(|value| value.to_str().ok())
                            .unwrap_or("")
                            .to_owned();
                        seen.lock().unwrap().push(value);
                        Ok(response)
                    };
                    // as jeff-server: a message (one frame) of up to 4 bytes + 50 MiB
                    let config =
                        tokio_tungstenite::tungstenite::protocol::WebSocketConfig::default()
                            .max_message_size(Some(4 + 50 * 1024 * 1024))
                            .max_frame_size(Some(4 + 50 * 1024 * 1024));
                    let Ok(ws) = tokio_tungstenite::accept_hdr_async_with_config(
                        stream,
                        callback,
                        Some(config),
                    )
                    .await
                    else {
                        return;
                    };
                    let (mut sink, mut source) = ws.split();
                    let (outgoing, mut queue) = mpsc::unbounded_channel::<Vec<u8>>();
                    writers.lock().unwrap().push(outgoing.clone());
                    let writer = tokio::spawn(async move {
                        while let Some(bytes) = queue.recv().await {
                            if sink
                                .send(Message::Binary(Bytes::from(bytes)))
                                .await
                                .is_err()
                            {
                                return;
                            }
                        }
                    });
                    // drop_connections aborts the writer too: the handlers of gated requests hold its queue open
                    let end_writer = writer.abort_handle();
                    writer_tasks.lock().unwrap().push(writer);
                    let outstanding = Arc::new(AtomicUsize::new(0));
                    while let Some(Ok(message)) = source.next().await {
                        let data = match message {
                            Message::Binary(data) => data,
                            // the client's keepalive (tungstenite answers each ping itself)
                            Message::Ping(_) | Message::Pong(_) => continue,
                            _ => break,
                        };
                        let message = unpack(&data[4..]).unwrap();
                        received.lock().unwrap().push(message.clone());
                        let op = message["op"].as_str().unwrap().to_owned();
                        if handler.options.silent.contains(&op.as_str()) {
                            continue;
                        }
                        if op != "hello" && outstanding.load(Ordering::SeqCst) >= MAX_OUTSTANDING {
                            let limited = fail(
                                &message["id"],
                                "RateLimited",
                                &format!(
                                    "{MAX_OUTSTANDING} requests are outstanding on this connection"
                                ),
                                json!({"retry_after_s": 1}).as_object().unwrap().clone(),
                            );
                            let _ = outgoing.send(frame(&limited));
                            continue;
                        }
                        if op != "hello" {
                            outstanding.fetch_add(1, Ordering::SeqCst);
                        }
                        let (handler, outgoing, outstanding) = (
                            Arc::clone(&handler),
                            outgoing.clone(),
                            Arc::clone(&outstanding),
                        );
                        tokio::spawn(async move {
                            let Some(reply) = handler.answer(&message).await else {
                                return;
                            };
                            if op != "hello" {
                                outstanding.fetch_sub(1, Ordering::SeqCst);
                            }
                            let bytes = frame(&reply);
                            match handler.options.reorder {
                                Some(count)
                                    if ["call", "call_many", "decide"].contains(&op.as_str()) =>
                                {
                                    let ready: Vec<Vec<u8>> = {
                                        let mut held = handler.held.lock().unwrap();
                                        held.push(bytes);
                                        if held.len() == count {
                                            held.drain(..).rev().collect()
                                        } else {
                                            Vec::new()
                                        }
                                    };
                                    for item in ready {
                                        let _ = outgoing.send(item);
                                    }
                                }
                                _ => {
                                    let _ = outgoing.send(bytes);
                                }
                            }
                        });
                    }
                    end_writer.abort();
                });
                tasks_list.lock().unwrap().push(task);
            }
        })
    };
    tasks.lock().unwrap().insert(0, accept);
    ChannelServer {
        url,
        received,
        headers,
        writers,
        gate,
        waiting,
        tasks,
    }
}
