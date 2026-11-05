//! A TCP relay in front of a test server: everything the client sends goes through it at `rate` bytes a second (no
//! limit at 0), or not at all while `paused`; what the server sends comes back at once. It stands for a slow or
//! stalled link.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;

pub struct Relay {
    /// http://127.0.0.1:<port> of the relay.
    pub url: String,
    pub paused: Arc<AtomicBool>,
    pub rate: Arc<AtomicUsize>,
    task: JoinHandle<()>,
}

impl Relay {
    pub fn close(self) {
        self.task.abort();
    }
}

/// A relay to the server at `target` (an http://host:port URL).
pub async fn start_relay(target: &str) -> Relay {
    let address = target.trim_start_matches("http://").to_owned();
    // Small socket buffers, so that what the client has written is what has gone through the relay, near enough.
    let socket = tokio::net::TcpSocket::new_v4().unwrap();
    socket.set_recv_buffer_size(64 * 1024).unwrap();
    socket.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let listener: TcpListener = socket.listen(64).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let paused = Arc::new(AtomicBool::new(false));
    let rate = Arc::new(AtomicUsize::new(0));
    let task = {
        let (paused, rate) = (Arc::clone(&paused), Arc::clone(&rate));
        tokio::spawn(async move {
            let mut links = Vec::new();
            while let Ok((client, _)) = listener.accept().await {
                let server = TcpStream::connect(&address).await.unwrap();
                let (mut client_read, mut client_write) = client.into_split();
                let (mut server_read, mut server_write) = server.into_split();
                let (paused, rate) = (Arc::clone(&paused), Arc::clone(&rate));
                links.push(tokio::spawn(async move {
                    let mut buffer = vec![0u8; 64 * 1024];
                    loop {
                        while paused.load(Ordering::SeqCst) {
                            tokio::time::sleep(Duration::from_millis(10)).await;
                        }
                        let limit = rate.load(Ordering::SeqCst);
                        let chunk = if limit == 0 {
                            buffer.len()
                        } else {
                            (limit / 50).clamp(1, buffer.len())
                        };
                        let count = match client_read.read(&mut buffer[..chunk]).await {
                            Ok(0) | Err(_) => return,
                            Ok(count) => count,
                        };
                        if server_write.write_all(&buffer[..count]).await.is_err() {
                            return;
                        }
                        if limit > 0 {
                            tokio::time::sleep(Duration::from_secs_f64(
                                count as f64 / limit as f64,
                            ))
                            .await;
                        }
                    }
                }));
                links.push(tokio::spawn(async move {
                    let _ = tokio::io::copy(&mut server_read, &mut client_write).await;
                }));
            }
            for link in links {
                link.abort();
            }
        })
    };
    Relay {
        url,
        paused,
        rate,
        task,
    }
}
