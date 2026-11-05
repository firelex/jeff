//! The engine socket protocol (contract/socket.md): each frame is a 4-byte big-endian length, then a MessagePack
//! map. Replies carry the request id and may arrive in any order (port of jeff/protocol.py).

use tokio::io::{AsyncRead, AsyncReadExt};

use crate::contract::{SchemaName, validate};
use crate::errors::{ErrorType, JeffError, Result};
use crate::json::{Json, JsonMap};
use crate::msgpack::{pack, unpack};
use crate::pyformat::{py_equals, py_repr};

/// 2: the base served as adapter "base", and input_tokens in predict results.
pub const PROTOCOL_VERSION: u64 = 2;
/// The largest frame body the engine socket carries: 64 MiB.
pub const MAX_FRAME: usize = 64 * 1024 * 1024;
/// The size of a frame's header: the body's length as 4 bytes, big-endian.
pub const HEADER_SIZE: usize = 4;
/// The server's low-latency channel (contract/socket.md, "The channel"): its path, the largest frame body it reads
/// (the HTTP API's body limit, 50 MiB) and the largest before the connection is authenticated.
pub const CHANNEL_PATH: &str = "/v1/channel";
/// The largest frame body the channel reads once authenticated: 50 MiB.
pub const MAX_BODY: usize = 50 * 1024 * 1024;
/// The largest frame body the channel reads before the connection is authenticated: 64 KiB.
pub const PREAUTH_MAX_BODY: usize = 64 * 1024;

/// How much room the reader makes for the next read when it is not in the middle of a frame.
const READ_SIZE: usize = 64 * 1024;

/// The socket schema's definition for each request op (contract/schemas/socket.schema.json).
fn op_definition(op: &str) -> Option<&'static str> {
    Some(match op {
        "installed" | "reload" | "stats" | "health" => "simple",
        "predict" => "predict",
        "serve" => "serve",
        "auth" | "refresh" => "auth",
        "bind" => "bind",
        "call" => "call",
        "call_many" => "call_many",
        "release" => "release",
        "decide" => "decide",
        "dispatch" => "dispatch",
        "graph" => "graph",
        _ => return None,
    })
}

/// The one $defs entry of the socket schema a message can match, read from its tag (its op, or for a reply its ok);
/// None when the tag names no entry, and the whole schema is then checked.
pub fn socket_definition(message: &Json) -> Option<&'static str> {
    let record = message.as_object()?;
    if let Some(op) = record.get("op") {
        let op = op.as_str()?;
        if op == "hello" {
            return Some(if record.contains_key("adapters_version") {
                "engine_hello"
            } else {
                "hello"
            });
        }
        return op_definition(op);
    }
    match record.get("ok") {
        Some(Json::Bool(true)) => Some("ok_reply"),
        Some(Json::Bool(false)) => Some("failure_reply"),
        _ => None,
    }
}

/// The message as an object if it matches the socket schema (or `definition` of it); InvalidRequest listing every
/// problem if not.
pub fn validate_message(message: &Json, where_: &str, definition: Option<&str>) -> Result<JsonMap> {
    let shape = definition.or_else(|| socket_definition(message));
    validate(SchemaName::Socket, message, where_, shape).map_err(|error| {
        if error.kind() == ErrorType::ContractError {
            JeffError::new(ErrorType::InvalidRequest, error.message())
        } else {
            error
        }
    })?;
    message.as_object().cloned().ok_or_else(|| {
        JeffError::new(
            ErrorType::InvalidRequest,
            format!("{where_} is not an object"),
        )
    })
}

/// One frame: the message checked against the socket schema, packed (keys in insertion order), behind its 4-byte
/// length. InvalidRequest for a message that does not match, cannot be packed, or packs larger than 64 MiB.
pub fn encode_frame(message: &JsonMap, definition: Option<&str>) -> Result<Vec<u8>> {
    let message = Json::Object(message.clone());
    validate_message(&message, "outgoing socket message", definition)?;
    let payload = pack(&message).map_err(|reason| {
        JeffError::new(
            ErrorType::InvalidRequest,
            format!("The outgoing socket message cannot be packed with msgpack: {reason}"),
        )
    })?;
    if payload.len() > MAX_FRAME {
        return Err(JeffError::new(
            ErrorType::InvalidRequest,
            format!(
                "Socket message of {} bytes is over the 64 MiB limit",
                payload.len()
            ),
        ));
    }
    let length = u32::try_from(payload.len()).expect("a payload of at most 64 MiB fits in 32 bits");
    let mut frame = Vec::with_capacity(HEADER_SIZE + payload.len());
    frame.extend_from_slice(&length.to_be_bytes());
    frame.extend_from_slice(&payload);
    Ok(frame)
}

/// The body length a frame header announces; InvalidRequest over 64 MiB.
pub fn frame_length(header: [u8; 4]) -> Result<usize> {
    let length = u32::from_be_bytes(header) as usize;
    if length > MAX_FRAME {
        return Err(JeffError::new(
            ErrorType::InvalidRequest,
            format!("Socket frame of {length} bytes is over the 64 MiB limit"),
        ));
    }
    Ok(length)
}

/// A frame body unpacked and checked against the socket schema.
pub fn decode_body(body: &[u8]) -> Result<JsonMap> {
    let message = unpack(body).map_err(|reason| {
        JeffError::new(
            ErrorType::InvalidRequest,
            format!("Socket frame is not valid msgpack: {reason}"),
        )
    })?;
    validate_message(&message, "incoming socket message", None)
}

/// The hello each side sends first; the engine's carries its adapters_version.
pub fn hello(adapters_version: Option<u64>) -> JsonMap {
    let mut message = JsonMap::new();
    message.insert("op".into(), Json::from("hello"));
    message.insert("version".into(), Json::from(PROTOCOL_VERSION));
    if let Some(version) = adapters_version {
        message.insert("adapters_version".into(), Json::from(version));
    }
    message
}

/// ProtocolMismatch unless the message is a hello speaking this side's protocol version (compared as Python's `==`
/// compares, so a version of 2.0 is accepted).
pub fn check_hello(message: &JsonMap) -> Result<()> {
    let op = message.get("op").unwrap_or(&Json::Null);
    if *op != "hello" {
        return Err(JeffError::new(
            ErrorType::ProtocolMismatch,
            format!("Expected a hello, got {}", py_repr(op)),
        ));
    }
    let version = message.get("version").unwrap_or(&Json::Null);
    if !py_equals(version, &Json::from(PROTOCOL_VERSION)) {
        // Python formats the version with str(), not repr()
        let shown = match version {
            Json::String(text) => text.clone(),
            other => py_repr(other),
        };
        return Err(JeffError::new(
            ErrorType::ProtocolMismatch,
            format!(
                "This side speaks protocol {PROTOCOL_VERSION}, the other side {shown}; upgrade the older one"
            ),
        ));
    }
    Ok(())
}

/// Why no frame could be read.
#[derive(Debug)]
pub enum FrameFailure {
    /// The other side closed the stream.
    Closed,
    /// Reading the stream failed.
    Io(std::io::Error),
    /// A header announced more than 64 MiB.
    Oversized(JeffError),
}

/// Splits a byte stream into frame bodies, reading each frame into one buffer reserved once for its whole length.
/// The length a header announces is checked against the 64 MiB limit before any room is made for it.
pub struct FrameReader<R> {
    reader: R,
    buffer: Vec<u8>,
    /// Where the unread bytes of `buffer` start.
    start: usize,
    /// How many times the buffer had to grow (tests check reading stays linear).
    pub(crate) growths: usize,
}

impl<R: AsyncRead + Unpin> FrameReader<R> {
    /// A frame reader over `reader`, with nothing read yet.
    pub fn new(reader: R) -> FrameReader<R> {
        FrameReader {
            reader,
            buffer: Vec::with_capacity(READ_SIZE),
            start: 0,
            growths: 0,
        }
    }

    fn available(&self) -> usize {
        self.buffer.len() - self.start
    }

    /// The length the next frame's header announces, once its header is here.
    fn announced(&self) -> std::result::Result<Option<usize>, FrameFailure> {
        if self.available() < HEADER_SIZE {
            return Ok(None);
        }
        let mut header = [0_u8; HEADER_SIZE];
        header.copy_from_slice(&self.buffer[self.start..self.start + HEADER_SIZE]);
        frame_length(header)
            .map(Some)
            .map_err(FrameFailure::Oversized)
    }

    /// A frame that is already wholly in the buffer, without reading the stream.
    pub fn buffered(&mut self) -> std::result::Result<Option<Vec<u8>>, FrameFailure> {
        let Some(length) = self.announced()? else {
            return Ok(None);
        };
        if self.available() < HEADER_SIZE + length {
            return Ok(None);
        }
        let body_start = self.start + HEADER_SIZE;
        let body = self.buffer[body_start..body_start + length].to_vec();
        self.start = body_start + length;
        if self.start == self.buffer.len() {
            self.buffer.clear();
            self.start = 0;
        }
        Ok(Some(body))
    }

    /// The next frame's body, reading the stream as needed.
    pub async fn next(&mut self) -> std::result::Result<Vec<u8>, FrameFailure> {
        loop {
            if let Some(body) = self.buffered()? {
                return Ok(body);
            }
            if self.start > 0 {
                // Only the start of one unfinished frame is left: move it to the front, once per frame.
                self.buffer.drain(..self.start);
                self.start = 0;
            }
            let wanted = match self.announced()? {
                Some(length) => HEADER_SIZE + length - self.available(),
                None => READ_SIZE,
            };
            let spare = self.buffer.capacity() - self.buffer.len();
            if spare < wanted {
                self.buffer.reserve_exact(wanted);
                self.growths += 1;
            }
            let read = self
                .reader
                .read_buf(&mut self.buffer)
                .await
                .map_err(FrameFailure::Io)?;
            if read == 0 {
                return Err(FrameFailure::Closed);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::pin::Pin;
    use std::task::{Context, Poll};
    use tokio::io::ReadBuf;

    #[test]
    fn a_frame_nested_too_deep_is_refused_by_name_and_one_at_the_limit_is_checked_on_a_small_stack()
    {
        use crate::msgpack::MAX_DEPTH;
        use crate::msgpack::tests::{nested, on_small_stack};
        for map in [false, true] {
            for depth in [MAX_DEPTH + 1, 10_000, 100_000] {
                let message = on_small_stack(move || {
                    decode_body(&nested(depth, map))
                        .unwrap_err()
                        .message()
                        .to_owned()
                });
                assert_eq!(
                    message,
                    "Socket frame is not valid msgpack: the value nests more than 256 arrays and maps inside one another"
                );
            }
            // at the limit, inside a reply: decoded, checked against the schema and its problem written out
            let checked = on_small_stack(move || {
                let mut frame = vec![0x83, 0xa2, b'i', b'd', 0x01, 0xa2, b'o', b'k', 0xc3, 0xa6];
                frame.extend(b"result");
                frame.extend(nested(MAX_DEPTH - 1, map));
                decode_body(&frame)
                    .map(|_| ())
                    .map_err(|error| error.message().to_owned())
            });
            if map {
                assert_eq!(checked, Ok(())); // a result is any object
            } else {
                assert!(checked.as_ref().is_err_and(|message| {
                    message.contains("does not match ok_reply in the socket schema")
                }));
            }
        }
    }

    /// Hands out `data` at most `chunk` bytes per read.
    struct Trickle {
        data: Vec<u8>,
        at: usize,
        chunk: usize,
    }

    impl AsyncRead for Trickle {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
            buf: &mut ReadBuf<'_>,
        ) -> Poll<std::io::Result<()>> {
            let end = (self.at + self.chunk)
                .min(self.data.len())
                .min(self.at + buf.remaining());
            let (at, data) = (self.at, std::mem::take(&mut self.data));
            buf.put_slice(&data[at..end]);
            self.data = data;
            self.at = end;
            Poll::Ready(Ok(()))
        }
    }

    fn map(value: Json) -> JsonMap {
        value.as_object().unwrap().clone()
    }

    #[tokio::test]
    async fn a_header_split_byte_by_byte_gives_the_frame_once_it_is_whole() {
        let frame = encode_frame(&map(json!({"op": "installed", "id": 1})), None).unwrap();
        let mut reader = FrameReader::new(Trickle {
            data: frame,
            at: 0,
            chunk: 1,
        });
        let body = reader.next().await.unwrap();
        assert_eq!(
            Json::Object(decode_body(&body).unwrap()),
            json!({"op": "installed", "id": 1})
        );
        assert!(matches!(reader.next().await, Err(FrameFailure::Closed)));
    }

    #[tokio::test]
    async fn two_frames_in_one_read_are_both_available_without_another_read() {
        let mut data = encode_frame(&map(json!({"op": "installed", "id": 1})), None).unwrap();
        data.extend(encode_frame(&map(json!({"op": "health", "id": 2})), None).unwrap());
        let mut reader = FrameReader::new(Trickle {
            data,
            at: 0,
            chunk: 1 << 20,
        });
        reader.next().await.unwrap();
        let second = reader
            .buffered()
            .unwrap()
            .expect("the second frame came in the same read");
        assert_eq!(
            Json::Object(decode_body(&second).unwrap()),
            json!({"op": "health", "id": 2})
        );
        assert!(reader.buffered().unwrap().is_none());
    }

    #[tokio::test]
    async fn an_8_mib_body_in_64_kib_reads_is_reserved_once_not_regrown_per_read() {
        let rows = json!([{"state": "x".repeat(8 * 1024 * 1024), "question": {"type": "noul", "instructions": "?"}}]);
        let message =
            map(json!({"op": "predict", "adapter": "jeff.safety.spam", "rows": rows, "id": 3}));
        let frame = encode_frame(&message, None).unwrap();
        let mut reader = FrameReader::new(Trickle {
            data: frame,
            at: 0,
            chunk: 64 * 1024,
        });
        let body = reader.next().await.unwrap();
        assert!(
            reader.growths <= 2,
            "the buffer grew {} times",
            reader.growths
        );
        assert_eq!(decode_body(&body).unwrap(), message);
    }

    #[tokio::test]
    async fn a_frame_before_an_oversized_header_in_the_same_read_is_returned_then_the_header_is_refused()
     {
        let mut data = encode_frame(&map(json!({"op": "installed", "id": 1})), None).unwrap();
        data.extend((64_u32 * 1024 * 1024 + 1).to_be_bytes());
        let mut reader = FrameReader::new(Trickle {
            data,
            at: 0,
            chunk: 1 << 20,
        });
        reader.next().await.unwrap();
        let Err(FrameFailure::Oversized(error)) = reader.buffered() else {
            panic!("the oversized header is refused")
        };
        assert_eq!(
            (error.kind(), error.message()),
            (
                ErrorType::InvalidRequest,
                "Socket frame of 67108865 bytes is over the 64 MiB limit"
            )
        );
    }

    #[test]
    fn outgoing_messages_are_checked_against_the_socket_schema_and_the_size_limit() {
        let wrong = encode_frame(
            &map(json!({"op": "predict", "id": 1, "adapter": "a"})),
            None,
        )
        .unwrap_err();
        assert_eq!(wrong.kind(), ErrorType::InvalidRequest);
        assert!(
            wrong
                .message()
                .starts_with("outgoing socket message does not match predict in the socket schema")
        );
        let huge = map(
            json!({"op": "predict", "adapter": "a", "rows": [{"state": "x".repeat(64 * 1024 * 1024), "question": {"type": "noul", "instructions": "?"}}], "id": 1}),
        );
        let refused = encode_frame(&huge, None).unwrap_err();
        assert_eq!(
            refused.message(),
            "Socket message of 67108943 bytes is over the 64 MiB limit"
        ); // msgpack-python packs it to 67108943 bytes
    }

    #[test]
    fn the_hello_is_checked_as_python_checks_it() {
        assert_eq!(
            Json::Object(hello(None)),
            json!({"op": "hello", "version": 2})
        );
        assert!(
            check_hello(&map(
                json!({"op": "hello", "version": 2, "adapters_version": 1})
            ))
            .is_ok()
        );
        assert_eq!(
            check_hello(&map(json!({"op": "installed"})))
                .unwrap_err()
                .message(),
            "Expected a hello, got 'installed'"
        );
        let old = check_hello(&map(json!({"op": "hello", "version": 1}))).unwrap_err();
        assert_eq!(
            (old.kind(), old.message()),
            (
                ErrorType::ProtocolMismatch,
                "This side speaks protocol 2, the other side 1; upgrade the older one"
            )
        );
        assert_eq!(
            check_hello(&map(json!({"op": "hello", "version": "x"})))
                .unwrap_err()
                .message(),
            "This side speaks protocol 2, the other side x; upgrade the older one"
        );
    }

    #[test]
    fn the_definition_a_message_names_is_read_from_its_tag() {
        assert_eq!(
            socket_definition(&json!({"op": "hello", "version": 2})),
            Some("hello")
        );
        assert_eq!(
            socket_definition(&json!({"op": "hello", "version": 2, "adapters_version": 1})),
            Some("engine_hello")
        );
        assert_eq!(
            socket_definition(&json!({"op": "call_many"})),
            Some("call_many")
        );
        assert_eq!(
            socket_definition(&json!({"ok": false})),
            Some("failure_reply")
        );
        assert_eq!(socket_definition(&json!({"op": "nope"})), None);
        assert_eq!(
            decode_body(&[0xc1]).unwrap_err().kind(),
            ErrorType::InvalidRequest
        );
    }

    #[test]
    fn a_version_python_finds_equal_to_2_is_accepted_and_others_are_shown_as_python_str_shows_them()
    {
        assert!(check_hello(&map(json!({"op": "hello", "version": 2.0}))).is_ok());
        for (version, shown) in [
            (json!(true), "True"),
            (json!(null), "None"),
            (json!(1.5), "1.5"),
            (json!([1]), "[1]"),
        ] {
            let refused =
                check_hello(&map(json!({"op": "hello", "version": version}))).unwrap_err();
            assert_eq!(
                refused.message(),
                format!(
                    "This side speaks protocol 2, the other side {shown}; upgrade the older one"
                )
            );
        }
        assert_eq!(
            check_hello(&JsonMap::new()).unwrap_err().message(),
            "Expected a hello, got None"
        );
    }

    /// Every frame of contract/conformance/socket.json, read in 7-byte pieces: each frame the Python client wrote
    /// decodes and encodes back to the same bytes, each frame the scripted engine wrote decodes, and the one bare
    /// header over 64 MiB is refused.
    #[tokio::test]
    async fn the_frames_of_the_socket_fixture_read_and_encode_as_python_wrote_them() {
        use base64::Engine as _;
        use base64::engine::general_purpose::STANDARD;
        let path = crate::shipped::repository_root().join("contract/conformance/socket.json");
        let fixture: Json = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let (mut frames, mut refused) = (0, 0);
        for case in fixture["cases"].as_array().unwrap() {
            for step in case["steps"].as_array().unwrap() {
                let (side, bytes) = match (step.get("client"), step.get("engine")) {
                    (Some(client), None) => {
                        ("client", STANDARD.decode(client.as_str().unwrap()).unwrap())
                    }
                    (None, Some(engine)) => {
                        ("engine", STANDARD.decode(engine.as_str().unwrap()).unwrap())
                    }
                    _ => continue,
                };
                let mut reader = FrameReader::new(Trickle {
                    data: bytes.clone(),
                    at: 0,
                    chunk: 7,
                });
                match reader.next().await {
                    Ok(body) => {
                        let message = decode_body(&body).unwrap_or_else(|error| {
                            panic!("{}: {}", case["name"], error.message())
                        });
                        if side == "client" {
                            assert_eq!(
                                encode_frame(&message, None).unwrap(),
                                bytes,
                                "{}",
                                case["name"]
                            );
                        }
                        assert!(
                            matches!(reader.next().await, Err(FrameFailure::Closed)),
                            "{}: one frame per step",
                            case["name"]
                        );
                        frames += 1;
                    }
                    Err(FrameFailure::Oversized(error)) => {
                        assert_eq!((side, bytes.len()), ("engine", HEADER_SIZE));
                        assert_eq!(
                            error.message(),
                            "Socket frame of 67108865 bytes is over the 64 MiB limit"
                        );
                        refused += 1;
                    }
                    Err(other) => panic!("{}: {other:?}", case["name"]),
                }
            }
        }
        assert!(frames > 0);
        assert_eq!(refused, 1);
    }
}
