//! A small HTTP/1.1 server for the remote tests: each request (keep-alive included) goes to the handler, whose Reply
//! says what to write back.

use std::sync::{Arc, Mutex};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

use super::fixture;

#[derive(Clone, Debug)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

pub enum Reply {
    Full {
        status: u16,
        headers: Vec<(String, String)>,
        body: String,
    },
    /// Headers announcing `length` bytes, then `body_start`, then nothing more.
    Partial {
        status: u16,
        body_start: String,
        length: usize,
    },
    /// Never answer.
    Silent,
    /// Write these bytes and close.
    Raw(Vec<u8>),
}

pub struct HttpServer {
    pub url: String,
    accept: JoinHandle<()>,
    connections: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

impl HttpServer {
    pub fn close(self) {
        self.accept.abort();
        for connection in self.connections.lock().unwrap().drain(..) {
            connection.abort();
        }
    }
}

pub fn json_reply(status: u16, body: &str, request_id: &str) -> Reply {
    Reply::Full {
        status,
        headers: vec![
            ("content-type".into(), "application/json".into()),
            ("x-request-id".into(), request_id.into()),
        ],
        body: body.into(),
    }
}

/// The GET /v1/functions answer recorded in remote.json (the shipped catalogue's functions, base bf16).
pub fn listing() -> String {
    fixture("remote.json")["cases"][0]["exchanges"][0]["response"]["body"]
        .as_str()
        .unwrap()
        .to_owned()
}

fn parse(head: &str) -> (String, String, Vec<(String, String)>) {
    let mut lines = head.split("\r\n");
    let mut first = lines.next().unwrap().split(' ');
    let method = first.next().unwrap().to_owned();
    let path = first.next().unwrap().split('?').next().unwrap().to_owned();
    let headers = lines
        .filter_map(|line| {
            line.split_once(':')
                .map(|(name, value)| (name.trim().to_owned(), value.trim().to_owned()))
        })
        .collect();
    (method, path, headers)
}

pub async fn serve(handler: impl Fn(&Request) -> Reply + Send + Sync + 'static) -> HttpServer {
    super::no_proxies();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let handler = Arc::new(handler);
    let connections: Arc<Mutex<Vec<JoinHandle<()>>>> = Default::default();
    let accept = {
        let connections = Arc::clone(&connections);
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                let handler = Arc::clone(&handler);
                connections.lock().unwrap().push(tokio::spawn(async move {
                    let mut buffer: Vec<u8> = Vec::new();
                    loop {
                        let end = loop {
                            if let Some(end) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
                                break end;
                            }
                            let mut chunk = [0u8; 8192];
                            match stream.read(&mut chunk).await {
                                Ok(0) | Err(_) => return,
                                Ok(read) => buffer.extend_from_slice(&chunk[..read]),
                            }
                        };
                        let (method, path, headers) = parse(&String::from_utf8_lossy(&buffer[..end]));
                        buffer.drain(..end + 4);
                        let length: usize = headers.iter().find(|(key, _)| key.eq_ignore_ascii_case("content-length"))
                            .map(|(_, value)| value.parse().unwrap()).unwrap_or(0);
                        while buffer.len() < length {
                            let mut chunk = [0u8; 8192];
                            match stream.read(&mut chunk).await {
                                Ok(0) | Err(_) => return,
                                Ok(read) => buffer.extend_from_slice(&chunk[..read]),
                            }
                        }
                        let body: Vec<u8> = buffer.drain(..length).collect();
                        let request = Request { method, path, headers, body };
                        let bytes = match handler(&request) {
                            Reply::Full { status, headers, body } => {
                                let mut text = format!("HTTP/1.1 {status} Status\r\ncontent-length: {}\r\n", body.len());
                                for (name, value) in headers.iter().filter(|(name, _)| !name.eq_ignore_ascii_case("content-length")) {
                                    text.push_str(&format!("{name}: {value}\r\n"));
                                }
                                text.push_str("\r\n");
                                text.push_str(&body);
                                text.into_bytes()
                            }
                            Reply::Partial { status, body_start, length } => {
                                let text = format!("HTTP/1.1 {status} Status\r\ncontent-length: {length}\r\ncontent-type: application/json\r\n\r\n{body_start}");
                                let _ = stream.write_all(text.as_bytes()).await;
                                std::future::pending::<()>().await;
                                return;
                            }
                            Reply::Silent => {
                                std::future::pending::<()>().await;
                                return;
                            }
                            Reply::Raw(bytes) => {
                                let _ = stream.write_all(&bytes).await;
                                return;
                            }
                        };
                        if stream.write_all(&bytes).await.is_err() {
                            return;
                        }
                    }
                }));
            }
        })
    };
    HttpServer {
        url,
        accept,
        connections,
    }
}
