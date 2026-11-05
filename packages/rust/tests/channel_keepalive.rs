//! The channel's WebSocket keepalive and frame rules, against a server written frame by frame: a ping every interval
//! and a missing pong ending the channel (as the Python SDK's websockets client does, 20 s and 20 s, shortened here
//! with testing::pin_channel_keepalive), a ping between the fragments of a message, and close codes RFC 6455 does not
//! allow on the wire. This file is its own test binary, so the pinned keepalive reaches no other test.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use jeff_sdk::msgpack::{pack, unpack};
use jeff_sdk::testing::{pin_channel_keepalive, pin_proxy_environment};
use jeff_sdk::{ChannelTransport, ErrorType, Json, TokenSource};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::handshake::derive_accept_key;

const BINARY: u8 = 0x2;
const CONTINUATION: u8 = 0x0;
const CLOSE: u8 = 0x8;
const PING: u8 = 0x9;
const PONG: u8 = 0xa;

/// What the server does besides answering hello.
#[derive(Clone, Copy, PartialEq)]
enum Behaviour {
    /// Answers every ping with a pong carrying its payload.
    AnswersPings,
    /// Never answers a ping.
    IgnoresPings,
    /// Answers `release` with its reply split in two fragments, a ping of its own between them.
    PingBetweenFragments,
    /// Answers `release` with a close frame of this code.
    Closes(u16),
}

#[derive(Default)]
struct Seen {
    /// The opcode and payload of every frame the client sent, in order.
    frames: Vec<(u8, Vec<u8>)>,
}

fn frame(opcode: u8, fin: bool, payload: &[u8]) -> Vec<u8> {
    let mut out = vec![if fin { 0x80 } else { 0 } | opcode];
    match payload.len() {
        len if len < 126 => out.push(len as u8),
        len if len <= 0xffff => {
            out.push(126);
            out.extend((len as u16).to_be_bytes());
        }
        len => {
            out.push(127);
            out.extend((len as u64).to_be_bytes());
        }
    }
    out.extend(payload);
    out
}

fn message(value: &Json) -> Vec<u8> {
    let body = pack(value).unwrap();
    let mut out = (body.len() as u32).to_be_bytes().to_vec();
    out.extend(body);
    out
}

/// One client frame (always masked): its opcode and unmasked payload; None once the client has gone.
async fn read_frame(stream: &mut TcpStream) -> Option<(u8, Vec<u8>)> {
    let mut head = [0u8; 2];
    stream.read_exact(&mut head).await.ok()?;
    let opcode = head[0] & 0x0f;
    let len = match head[1] & 0x7f {
        126 => {
            let mut ext = [0u8; 2];
            stream.read_exact(&mut ext).await.ok()?;
            u64::from(u16::from_be_bytes(ext))
        }
        127 => {
            let mut ext = [0u8; 8];
            stream.read_exact(&mut ext).await.ok()?;
            u64::from_be_bytes(ext)
        }
        len => u64::from(len),
    };
    let mut mask = [0u8; 4];
    stream.read_exact(&mut mask).await.ok()?;
    let mut payload = vec![0u8; len as usize];
    stream.read_exact(&mut payload).await.ok()?;
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte ^= mask[index % 4];
    }
    Some((opcode, payload))
}

/// A server for one connection: the WebSocket upgrade, hello, then `behaviour`. Returns its URL and what it saw.
async fn serve(behaviour: Behaviour) -> (String, Arc<Mutex<Seen>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Seen::default()));
    let record = Arc::clone(&seen);
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            let mut byte = [0u8; 1];
            stream.read_exact(&mut byte).await.unwrap();
            request.push(byte[0]);
        }
        let text = String::from_utf8(request).unwrap();
        let key = text
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("sec-websocket-key")
                    .then(|| value.trim().to_owned())
            })
            .unwrap();
        let reply = format!(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {}\r\n\r\n",
            derive_accept_key(key.as_bytes())
        );
        stream.write_all(reply.as_bytes()).await.unwrap();
        while let Some((opcode, payload)) = read_frame(&mut stream).await {
            record
                .lock()
                .unwrap()
                .frames
                .push((opcode, payload.clone()));
            match opcode {
                PING if behaviour != Behaviour::IgnoresPings => {
                    stream
                        .write_all(&frame(PONG, true, &payload))
                        .await
                        .unwrap();
                }
                BINARY => {
                    let request = unpack(&payload[4..]).unwrap();
                    let id = request["id"].clone();
                    match request["op"].as_str().unwrap() {
                        "hello" => {
                            let hello = message(&json!({"op": "hello", "version": 2}));
                            stream
                                .write_all(&frame(BINARY, true, &hello))
                                .await
                                .unwrap();
                        }
                        "release" => match behaviour {
                            Behaviour::PingBetweenFragments => {
                                let reply = message(&json!({"id": id, "ok": true, "result": {}}));
                                let (first, second) = reply.split_at(reply.len() / 2);
                                let mut out = frame(BINARY, false, first);
                                out.extend(frame(PING, true, b"between"));
                                out.extend(frame(CONTINUATION, true, second));
                                stream.write_all(&out).await.unwrap();
                            }
                            Behaviour::Closes(code) => {
                                let mut payload = code.to_be_bytes().to_vec();
                                payload.extend(b"bye");
                                stream
                                    .write_all(&frame(CLOSE, true, &payload))
                                    .await
                                    .unwrap();
                            }
                            _ => {} // never answered
                        },
                        other => panic!("unexpected op {other}"),
                    }
                }
                _ => {}
            }
        }
    });
    (url, seen)
}

async fn open(url: &str) -> ChannelTransport {
    pin_proxy_environment(Some(Vec::new()));
    pin_channel_keepalive(Some((100, 300)));
    ChannelTransport::open(url, TokenSource::None, 2000)
        .await
        .unwrap()
}

fn opcodes(seen: &Mutex<Seen>, opcode: u8) -> Vec<Vec<u8>> {
    let seen = seen.lock().unwrap();
    seen.frames
        .iter()
        .filter(|(code, _)| *code == opcode)
        .map(|(_, payload)| payload.clone())
        .collect()
}

#[tokio::test]
async fn a_server_that_answers_pings_keeps_the_channel_open() {
    let (url, seen) = serve(Behaviour::AnswersPings).await;
    let channel = open(&url).await;
    tokio::time::sleep(Duration::from_millis(900)).await;
    assert!(!channel.closed());
    let pings = opcodes(&seen, PING);
    assert!(
        pings.len() >= 3,
        "{} pings in 900 ms at one per 100 ms",
        pings.len()
    );
    assert_eq!(pings[0], 1u64.to_be_bytes());
    assert_eq!(pings[1], 2u64.to_be_bytes());
    channel.close().await;
}

#[tokio::test]
async fn no_pong_within_the_timeout_ends_the_channel_with_backend_unavailable() {
    let (url, seen) = serve(Behaviour::IgnoresPings).await;
    let channel = Arc::new(open(&url).await);
    let waiting = tokio::spawn({
        let channel = Arc::clone(&channel);
        async move { channel.release(1).await }
    });
    let error = tokio::time::timeout(Duration::from_secs(5), waiting)
        .await
        .expect("the missing pong ends the channel well within 5 s")
        .unwrap()
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::BackendUnavailable);
    assert_eq!(
        error.message(),
        format!(
            "{}/v1/channel: the server sent no pong within 0.3 s of a keepalive ping; the channel closed",
            url.replace("http://", "ws://")
        )
    );
    assert!(channel.closed());
    assert_eq!(
        opcodes(&seen, PING).len(),
        1,
        "no second ping while the first is unanswered"
    );
}

#[tokio::test]
async fn a_ping_between_the_fragments_of_a_message_is_answered_and_the_message_kept() {
    let (url, seen) = serve(Behaviour::PingBetweenFragments).await;
    let channel = open(&url).await;
    channel.release(1).await.unwrap();
    // the client answers the server's ping, with its payload, whatever its own keepalive sends meanwhile
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(opcodes(&seen, PONG).contains(&b"between".to_vec()));
    assert!(!channel.closed());
    channel.close().await;
}

#[tokio::test]
async fn a_close_code_rfc_6455_allows_is_reported_with_its_reason() {
    let (url, _seen) = serve(Behaviour::Closes(4000)).await;
    let channel = open(&url).await;
    let error = channel.release(1).await.unwrap_err();
    assert_eq!(error.kind(), ErrorType::BackendUnavailable);
    assert!(
        error
            .message()
            .ends_with("the channel closed (WebSocket close code 4000: bye)"),
        "{}",
        error.message()
    );
}

#[tokio::test]
async fn a_close_code_rfc_6455_forbids_on_the_wire_is_refused_as_a_protocol_error() {
    // 999 and below are never used; 1004, 1005, 1006 and 1015 are reserved and must not be sent; 1016-2999 are
    // reserved for the protocol's future versions
    for code in [999u16, 1004, 1005, 1006, 1015, 1016, 2999] {
        let (url, seen) = serve(Behaviour::Closes(code)).await;
        let channel = open(&url).await;
        let error = channel.release(1).await.unwrap_err();
        assert_eq!(
            error.kind(),
            ErrorType::BackendUnavailable,
            "close code {code}"
        );
        // tungstenite puts its own frame (1002, "Protocol violation") in place of the forbidden one, as the close
        // it reports and the one it answers with
        assert!(
            error
                .message()
                .ends_with("the channel closed (WebSocket close code 1002: Protocol violation)"),
            "close code {code}: {}",
            error.message()
        );
        assert!(channel.closed());
        tokio::time::sleep(Duration::from_millis(50)).await;
        let closes = opcodes(&seen, CLOSE);
        assert_eq!(closes.len(), 1, "close code {code}");
        assert_eq!(closes[0][..2], 1002u16.to_be_bytes(), "close code {code}");
    }
}
