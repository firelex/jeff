//! A fake engine on a Unix socket: answers hello, installed, health, predict (with `answers`) and serve (refused
//! with `serve_refusal` when set), like `jeff runtime serve` over a fake model. Every request it reads is recorded.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use jeff_sdk::msgpack::{pack, unpack};
use jeff_sdk::{Json, JsonMap};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixListener;
use tokio::task::JoinHandle;

pub type AnswerFn = Arc<dyn Fn(&str, &Json) -> Json + Send + Sync>;

pub struct EngineServerOptions {
    pub answers: AnswerFn,
    pub installed: Vec<String>,
    /// Listen here (the caller owns the directory); a temporary directory of the server's own when None.
    pub path: Option<PathBuf>,
    pub predict_delay_ms: Option<u64>,
    /// The error (`{"type", "message"}`) a `serve` request is refused with; when None, a `serve` is accepted.
    pub serve_refusal: Option<Json>,
}

pub struct EngineServer {
    pub path: PathBuf,
    answering: Arc<AtomicBool>,
    accept: JoinHandle<()>,
    connections: Arc<std::sync::Mutex<Vec<JoinHandle<()>>>>,
    /// Every request read, on every connection, in the order read.
    pub received: Arc<std::sync::Mutex<Vec<Json>>>,
    _directory: Option<tempfile::TempDir>,
}

pub fn socket_directory() -> tempfile::TempDir {
    tempfile::Builder::new().prefix("jeffrs").tempdir().unwrap()
}

pub fn frame(message: &Json) -> Vec<u8> {
    let body = pack(message).unwrap();
    let mut out = (body.len() as u32).to_be_bytes().to_vec();
    out.extend(body);
    out
}

pub async fn start(options: EngineServerOptions) -> EngineServer {
    let directory = if options.path.is_none() {
        Some(socket_directory())
    } else {
        None
    };
    let path = options
        .path
        .clone()
        .unwrap_or_else(|| directory.as_ref().unwrap().path().join("e.sock"));
    let listener = UnixListener::bind(&path).unwrap();
    let answering = Arc::new(AtomicBool::new(true));
    let connections: Arc<std::sync::Mutex<Vec<JoinHandle<()>>>> = Default::default();
    let received: Arc<std::sync::Mutex<Vec<Json>>> = Default::default();
    let options = Arc::new(options);
    let accept = {
        let (answering, connections) = (Arc::clone(&answering), Arc::clone(&connections));
        let received = Arc::clone(&received);
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let (options, answering) = (Arc::clone(&options), Arc::clone(&answering));
                let received = Arc::clone(&received);
                connections.lock().unwrap().push(tokio::spawn(async move {
                    let (mut read, write) = stream.into_split();
                    let write = Arc::new(tokio::sync::Mutex::new(write));
                    loop {
                        let mut header = [0u8; 4];
                        if read.read_exact(&mut header).await.is_err() {
                            return;
                        }
                        let mut body = vec![0u8; u32::from_be_bytes(header) as usize];
                        if read.read_exact(&mut body).await.is_err() {
                            return;
                        }
                        let message = unpack(&body).unwrap();
                        received.lock().unwrap().push(message.clone());
                        let id = message.get("id").cloned().unwrap_or(Json::Null);
                        let installed: Vec<Json> = options.installed.iter().map(|id| json!(id)).collect();
                        let reply = match message["op"].as_str().unwrap() {
                            "hello" => json!({"op": "hello", "version": 2, "adapters_version": 1}),
                            "installed" => {
                                let mut adapters = vec![json!("base")];
                                adapters.extend(installed);
                                json!({"id": id, "ok": true, "result": {"adapters": adapters, "adapters_version": 1}})
                            }
                            "health" => json!({"id": id, "ok": true, "result": {"status": "ready", "backend": "fake",
                                "adapters": installed, "adapters_version": 1,
                                "base": {"id": "jeff-base", "version": "v1.3", "checkpoint": "fake", "max_options": 252, "format": "bf16"}}}),
                            "serve" => match &options.serve_refusal {
                                Some(error) => json!({"id": id, "ok": false, "error": {"type": error["type"],
                                    "message": error["message"], "details": {"adapters_version": 1}}}),
                                None => {
                                    let mut adapters = vec![json!("base")];
                                    adapters.extend(message["adapters"].as_array().unwrap().iter().cloned());
                                    json!({"id": id, "ok": true, "result": {"adapters": adapters, "adapters_version": 2}})
                                }
                            },
                            "predict" if answering.load(Ordering::SeqCst) => {
                                let adapter = message["adapter"].as_str().unwrap().to_owned();
                                let probabilities: Vec<Json> = message["rows"].as_array().unwrap().iter()
                                    .map(|row| (options.answers)(&adapter, row)).collect();
                                json!({"id": id, "ok": true, "result": {"probabilities": probabilities,
                                    "timing": {"path": "fake", "model_ms": 0.5, "format": "bf16"}, "adapters_version": 1}})
                            }
                            _ => continue,
                        };
                        let bytes = frame(&reply);
                        let write = Arc::clone(&write);
                        match options.predict_delay_ms.filter(|_| message["op"] == "predict") {
                            None => { let _ = write.lock().await.write_all(&bytes).await; }
                            Some(delay) => {
                                tokio::spawn(async move {
                                    tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
                                    let _ = write.lock().await.write_all(&bytes).await;
                                });
                            }
                        }
                    }
                }));
            }
        })
    };
    EngineServer {
        path,
        answering,
        accept,
        connections,
        received,
        _directory: directory,
    }
}

impl EngineServer {
    pub fn stop_answering(&self) {
        self.answering.store(false, Ordering::SeqCst);
    }

    /// Ends every connection and stops listening (the socket file is removed).
    pub fn close(self) {
        self.accept.abort();
        for connection in self.connections.lock().unwrap().drain(..) {
            connection.abort();
        }
        let _ = std::fs::remove_file(&self.path);
    }
}

pub fn answering(
    answers: impl Fn(&str, &Json) -> Json + Send + Sync + 'static,
    installed: &[&str],
) -> EngineServerOptions {
    EngineServerOptions {
        answers: Arc::new(answers),
        installed: installed.iter().map(|id| (*id).to_owned()).collect(),
        path: None,
        predict_delay_ms: None,
        serve_refusal: None,
    }
}

pub fn map(value: Json) -> JsonMap {
    value.as_object().unwrap().clone()
}
