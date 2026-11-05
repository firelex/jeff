//! A local HTTP proxy for the proxy tests: CONNECT tunnels (as httpx uses for https:// servers and websockets for the
//! channel), and requests naming the whole URL (as httpx sends them for http:// servers), which it forwards in origin
//! form. It records each request's first line and its Proxy-Authorization, and can require credentials.

use std::sync::{Arc, Mutex};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;

/// Headers, lower-cased names, as each CONNECT carried them.
pub type Headers = Vec<(String, String)>;

pub struct ProxyServer {
    /// http://127.0.0.1:<port>
    pub url: String,
    /// Each request: its first line, and its Proxy-Authorization ("" without one).
    pub seen: Arc<Mutex<Vec<(String, String)>>>,
    /// Each CONNECT's other headers, lower-cased names.
    pub connect_headers: Arc<Mutex<Vec<Headers>>>,
    task: JoinHandle<()>,
}

impl ProxyServer {
    pub fn lines(&self) -> Vec<String> {
        self.seen
            .lock()
            .unwrap()
            .iter()
            .map(|(line, _)| line.clone())
            .collect()
    }

    pub fn close(self) {
        self.task.abort();
    }
}

async fn read_head(stream: &mut TcpStream) -> Option<Vec<u8>> {
    let mut head = Vec::new();
    while !head.ends_with(b"\r\n\r\n") {
        let mut byte = [0u8; 1];
        if stream.read(&mut byte).await.ok()? == 0 {
            return None;
        }
        head.push(byte[0]);
    }
    Some(head)
}

/// A proxy; with `required`, only requests carrying that Proxy-Authorization are let through (others get 407).
pub async fn start_proxy(required: Option<String>) -> ProxyServer {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen: Arc<Mutex<Vec<(String, String)>>> = Default::default();
    let connect_headers: Arc<Mutex<Vec<Headers>>> = Default::default();
    let task = {
        let (seen, connect_headers) = (Arc::clone(&seen), Arc::clone(&connect_headers));
        tokio::spawn(async move {
            let mut links = Vec::new();
            while let Ok((mut client, _)) = listener.accept().await {
                let (seen, connect_headers, required) = (
                    Arc::clone(&seen),
                    Arc::clone(&connect_headers),
                    required.clone(),
                );
                links.push(tokio::spawn(async move {
                    let Some(head) = read_head(&mut client).await else { return };
                    let text = String::from_utf8_lossy(&head).to_string();
                    let mut lines = text.split("\r\n");
                    let first = lines.next().unwrap().to_owned();
                    let headers: Vec<(String, String)> = lines
                        .filter_map(|line| line.split_once(": "))
                        .map(|(name, value)| (name.to_ascii_lowercase(), value.to_owned()))
                        .collect();
                    let authorization = headers.iter().find(|(name, _)| name == "proxy-authorization").map_or(String::new(), |(_, value)| value.clone());
                    seen.lock().unwrap().push((first.clone(), authorization.clone()));
                    if required.as_ref().is_some_and(|required| *required != authorization) {
                        let _ = client.write_all(b"HTTP/1.1 407 Proxy Authentication Required\r\ncontent-length: 0\r\nconnection: close\r\n\r\n").await;
                        return;
                    }
                    let mut parts = first.split(' ');
                    let method = parts.next().unwrap().to_owned();
                    let target = parts.next().unwrap().to_owned();
                    let mut server = if method == "CONNECT" {
                        connect_headers.lock().unwrap().push(headers.into_iter().filter(|(name, _)| name != "proxy-authorization").collect());
                        let Ok(server) = TcpStream::connect(target.as_str()).await else {
                            let _ = client.write_all(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n").await;
                            return;
                        };
                        client.write_all(b"HTTP/1.1 200 Connection established\r\n\r\n").await.unwrap();
                        server
                    } else {
                        // GET http://host:port/path HTTP/1.1: forwarded in origin form, without Proxy-Authorization
                        let rest = target.strip_prefix("http://").unwrap();
                        let (authority, path) = rest.split_at(rest.find('/').unwrap_or(rest.len()));
                        let mut server = TcpStream::connect(authority).await.unwrap();
                        let mut forwarded = text.replacen(&target, if path.is_empty() { "/" } else { path }, 1);
                        if let Some(start) = forwarded.to_ascii_lowercase().find("proxy-authorization:") {
                            let end = forwarded[start..].find("\r\n").unwrap() + start + 2;
                            forwarded.replace_range(start..end, "");
                        }
                        server.write_all(forwarded.as_bytes()).await.unwrap();
                        server
                    };
                    let _ = tokio::io::copy_bidirectional(&mut client, &mut server).await;
                }));
            }
            for link in links {
                link.abort();
            }
        })
    };
    ProxyServer {
        url,
        seen,
        connect_headers,
        task,
    }
}
