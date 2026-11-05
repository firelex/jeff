//! MessagePack as the Python SDK packs it (`msgpack.packb(value, use_bin_type=True)`) and reads it
//! (`msgpack.unpackb(data, raw=False)`): maps in insertion order, integers in their smallest encoding, every float
//! a float64, text as str. Rows and frames must equal contract/conformance's bytes exactly.

use crate::json::{Json, JsonMap};
use crate::pyformat::py_float;

const VEC: &str = "writing MessagePack into a Vec cannot fail";

fn length(len: usize, what: &str) -> Result<u32, String> {
    u32::try_from(len).map_err(|_| format!("Too large {what} to pack: {len}"))
}

fn pack_into(value: &Json, out: &mut Vec<u8>) -> Result<(), String> {
    match value {
        Json::Null => rmp::encode::write_nil(out).expect(VEC),
        Json::Bool(flag) => rmp::encode::write_bool(out, *flag).expect(VEC),
        Json::Number(number) => {
            if let Some(int) = number.as_i64() {
                rmp::encode::write_sint(out, int).expect(VEC);
            } else if let Some(int) = number.as_u64() {
                rmp::encode::write_uint(out, int).expect(VEC);
            } else {
                let float = number
                    .as_f64()
                    .expect("a JSON number is an integer or a float");
                rmp::encode::write_f64(out, float).expect(VEC);
            }
        }
        Json::String(text) => {
            length(text.len(), "str")?;
            rmp::encode::write_str(out, text).expect(VEC);
        }
        Json::Array(items) => {
            rmp::encode::write_array_len(out, length(items.len(), "array")?).expect(VEC);
            for item in items {
                pack_into(item, out)?;
            }
        }
        Json::Object(map) => {
            rmp::encode::write_map_len(out, length(map.len(), "map")?).expect(VEC);
            for (key, item) in map {
                length(key.len(), "str")?;
                rmp::encode::write_str(out, key).expect(VEC);
                pack_into(item, out)?;
            }
        }
    }
    Ok(())
}

/// The value packed as MessagePack bytes.
pub fn pack(value: &Json) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    pack_into(value, &mut out)?;
    Ok(out)
}

fn float(value: f64) -> Result<Json, String> {
    serde_json::Number::from_f64(value)
        .map(Json::Number)
        .ok_or_else(|| format!("the number {}, which JSON cannot hold", py_float(value)))
}

fn to_json(value: rmpv::Value) -> Result<Json, String> {
    Ok(match value {
        rmpv::Value::Nil => Json::Null,
        rmpv::Value::Boolean(flag) => Json::Bool(flag),
        rmpv::Value::Integer(int) => match (int.as_i64(), int.as_u64()) {
            (Some(signed), _) => Json::from(signed),
            (None, Some(unsigned)) => Json::from(unsigned),
            (None, None) => return Err("an integer outside the 64-bit range".into()),
        },
        rmpv::Value::F32(value) => float(f64::from(value))?,
        rmpv::Value::F64(value) => float(value)?,
        rmpv::Value::String(text) => {
            Json::String(text.into_str().ok_or("a str that is not valid UTF-8")?)
        }
        rmpv::Value::Binary(bytes) => {
            return Err(format!(
                "binary data ({} bytes), which JSON cannot hold",
                bytes.len()
            ));
        }
        rmpv::Value::Array(items) => {
            Json::Array(items.into_iter().map(to_json).collect::<Result<_, _>>()?)
        }
        rmpv::Value::Map(pairs) => {
            let mut map = JsonMap::new();
            for (key, item) in pairs {
                let rmpv::Value::String(key) = key else {
                    return Err(format!("a map key that is not a string ({key})"));
                };
                let key = key.into_str().ok_or("a map key that is not valid UTF-8")?;
                map.insert(key, to_json(item)?);
            }
            Json::Object(map)
        }
        rmpv::Value::Ext(kind, _) => {
            return Err(format!(
                "an extension type {kind} value, which JSON cannot hold"
            ));
        }
    })
}

/// The most arrays and maps one value may hold inside one another. A deeper value is refused by name, measured without
/// recursion before anything is decoded, so no input can overflow the stack of the thread reading it.
///
/// Why 256: every walk of a decoded value (rmpv's decoder, the JSON conversion, the schema check, Python-style repr,
/// drop) recurses once per level. Measured in a debug build on a 2 MiB thread (a Tokio worker's stack and a test
/// thread's), rmpv plus the JSON conversion overflow at about 350 levels (348 one-key maps, 364 arrays); 256 keeps a
/// margin for the caller's own frames. msgpack-python reads up to 1024 levels (1025 raise StackError), but no message
/// of the contract nests anywhere near 256.
pub const MAX_DEPTH: usize = 256;

fn take<'a>(bytes: &mut &'a [u8], count: usize) -> Option<&'a [u8]> {
    if bytes.len() < count {
        return None;
    }
    let (head, rest) = bytes.split_at(count);
    *bytes = rest;
    Some(head)
}

fn take_len(bytes: &mut &[u8], width: usize) -> Option<usize> {
    let head = take(bytes, width)?;
    Some(
        head.iter()
            .fold(0usize, |length, byte| (length << 8) | usize::from(*byte)),
    )
}

/// What rmpv's decoder would not refuse itself, found without recursion: a value nesting more than MAX_DEPTH arrays
/// and maps, and the byte 0xc1, which MessagePack never uses (rmpv reads it as nil; msgpack-python refuses it). Bytes
/// cut short end the scan without an error: the decoder names them.
fn check_shape(mut bytes: &[u8]) -> Result<(), String> {
    use rmp::Marker;
    // How many more values each open array or map holds.
    let mut open: Vec<usize> = Vec::new();
    let mut values = 1usize;
    loop {
        // the next value belongs to the innermost container that still holds one (or is the top value)
        while let Some(remaining) = open.last_mut() {
            if *remaining > 0 {
                *remaining -= 1;
                break;
            }
            open.pop();
        }
        if open.is_empty() {
            if values == 0 {
                return Ok(());
            }
            values = 0;
        }
        let Some(&first) = bytes.first() else {
            return Ok(());
        };
        bytes = &bytes[1..];
        let skip = match Marker::from_u8(first) {
            Marker::FixPos(_) | Marker::FixNeg(_) | Marker::Null | Marker::True | Marker::False => {
                0
            }
            Marker::U8 | Marker::I8 => 1,
            Marker::U16 | Marker::I16 => 2,
            Marker::U32 | Marker::I32 | Marker::F32 => 4,
            Marker::U64 | Marker::I64 | Marker::F64 => 8,
            Marker::FixStr(length) => usize::from(length),
            Marker::Str8 | Marker::Bin8 => match take_len(&mut bytes, 1) {
                Some(length) => length,
                None => return Ok(()),
            },
            Marker::Str16 | Marker::Bin16 => match take_len(&mut bytes, 2) {
                Some(length) => length,
                None => return Ok(()),
            },
            Marker::Str32 | Marker::Bin32 => match take_len(&mut bytes, 4) {
                Some(length) => length,
                None => return Ok(()),
            },
            Marker::FixExt1 => 2,
            Marker::FixExt2 => 3,
            Marker::FixExt4 => 5,
            Marker::FixExt8 => 9,
            Marker::FixExt16 => 17,
            Marker::Ext8 => match take_len(&mut bytes, 1) {
                Some(length) => length + 1,
                None => return Ok(()),
            },
            Marker::Ext16 => match take_len(&mut bytes, 2) {
                Some(length) => length + 1,
                None => return Ok(()),
            },
            Marker::Ext32 => match take_len(&mut bytes, 4) {
                Some(length) => length + 1,
                None => return Ok(()),
            },
            Marker::FixArray(_)
            | Marker::Array16
            | Marker::Array32
            | Marker::FixMap(_)
            | Marker::Map16
            | Marker::Map32 => {
                let items = match Marker::from_u8(first) {
                    Marker::FixArray(count) => Some(usize::from(count)),
                    Marker::FixMap(count) => Some(2 * usize::from(count)),
                    Marker::Array16 => take_len(&mut bytes, 2),
                    Marker::Array32 => take_len(&mut bytes, 4),
                    Marker::Map16 => take_len(&mut bytes, 2).map(|count| 2 * count),
                    _ => take_len(&mut bytes, 4).map(|count| 2 * count),
                };
                let Some(items) = items else { return Ok(()) };
                open.push(items);
                if open.len() > MAX_DEPTH {
                    return Err(format!(
                        "the value nests more than {MAX_DEPTH} arrays and maps inside one another"
                    ));
                }
                continue;
            }
            Marker::Reserved => {
                return Err("the byte 0xc1, which MessagePack never uses".to_owned());
            }
        };
        if take(&mut bytes, skip).is_none() {
            return Ok(());
        }
    }
}

/// One MessagePack value read from `bytes`, which must hold exactly that value. A value nesting more than MAX_DEPTH
/// arrays and maps, or holding the unused byte 0xc1, is refused by name before it is decoded.
pub fn unpack(bytes: &[u8]) -> Result<Json, String> {
    check_shape(bytes)?;
    let mut rest = bytes;
    let value = rmpv::decode::read_value(&mut rest).map_err(|error| error.to_string())?;
    if !rest.is_empty() {
        return Err(format!(
            "{} bytes of extra data after the value",
            rest.len()
        ));
    }
    to_json(value)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// Runs `work` on a thread with a 2 MiB stack, a Tokio worker's (in a debug build when run by `cargo test`).
    pub(crate) fn on_small_stack<T: Send + 'static>(
        work: impl FnOnce() -> T + Send + 'static,
    ) -> T {
        std::thread::Builder::new()
            .stack_size(2 * 1024 * 1024)
            .spawn(work)
            .unwrap()
            .join()
            .expect("the work does not panic or overflow the stack")
    }

    /// `depth` arrays (or one-key maps) inside one another around a nil.
    pub(crate) fn nested(depth: usize, map: bool) -> Vec<u8> {
        let opening: &[u8] = if map { &[0x81, 0xa1, b'k'] } else { &[0x91] };
        let mut bytes = opening.repeat(depth);
        bytes.push(0xc0);
        bytes
    }

    #[test]
    fn values_nesting_up_to_256_levels_are_read_and_deeper_ones_refused_by_name_on_a_small_stack() {
        for map in [false, true] {
            let read = on_small_stack(move || unpack(&nested(MAX_DEPTH, map)).map(|_| ()));
            assert_eq!(read, Ok(()));
            for depth in [MAX_DEPTH + 1, 1025, 10_000, 100_000] {
                let refused = on_small_stack(move || unpack(&nested(depth, map)));
                assert_eq!(
                    refused,
                    Err(
                        "the value nests more than 256 arrays and maps inside one another"
                            .to_owned()
                    )
                );
            }
        }
        // the scan follows lengths: a long flat array, strings, binaries and ext values hide no nesting
        let mut flat = vec![0xdc, 0x01, 0x00];
        for _ in 0..128 {
            flat.extend([0xc4, 0x02, 0x91, 0x91]); // a bin8 whose bytes look like arrays
            flat.extend([0xc7, 0x01, 0x05, 0x91]); // an ext8 likewise
        }
        assert_eq!(check_shape(&flat), Ok(()));
        assert!(check_shape(&nested(MAX_DEPTH + 1, false)).is_err());
        let mut wide = vec![0x92];
        wide.extend(nested(MAX_DEPTH - 1, false));
        wide.extend(nested(MAX_DEPTH - 1, true));
        assert!(on_small_stack(move || unpack(&wide).map(|_| ())).is_ok());
        // truncated or reserved bytes end the scan; the decoder names them
        assert!(
            unpack(&[0x91, 0x91]).is_err(),
            "{:?}",
            unpack(&[0x91, 0x91])
        );
        for unused in [&[0xc1][..], &[0x91, 0xc1], &[0x81, 0xa1, b'k', 0xc1]] {
            assert_eq!(
                unpack(unused),
                Err("the byte 0xc1, which MessagePack never uses".to_owned())
            );
        }
    }
    use base64::Engine as _;
    use base64::engine::general_purpose::STANDARD;
    use serde_json::json;

    fn fixture(name: &str) -> Json {
        let path = crate::shipped::repository_root()
            .join("contract/conformance")
            .join(name);
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap()
    }

    #[test]
    fn a_map_keeps_its_insertion_order_number_like_keys_included() {
        let mut map = JsonMap::new();
        map.insert("b".into(), json!(1));
        map.insert("10".into(), json!(2));
        assert_eq!(
            pack(&Json::Object(map)).unwrap(),
            [0x82, 0xa1, 0x62, 0x01, 0xa2, 0x31, 0x30, 0x02]
        );
    }

    #[test]
    fn integers_take_their_smallest_encoding_and_floats_are_always_float64() {
        assert_eq!(pack(&json!(5)).unwrap(), [0x05]);
        assert_eq!(pack(&json!(-1)).unwrap(), [0xff]);
        assert_eq!(pack(&json!(-33)).unwrap(), [0xd0, 0xdf]);
        assert_eq!(pack(&json!(200)).unwrap(), [0xcc, 0xc8]);
        assert_eq!(pack(&json!(70000)).unwrap(), [0xce, 0x00, 0x01, 0x11, 0x70]);
        assert_eq!(
            pack(&json!(u64::MAX)).unwrap(),
            [0xcf, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff]
        );
        assert_eq!(
            pack(&json!(1.0)).unwrap(),
            [0xcb, 0x3f, 0xf0, 0, 0, 0, 0, 0, 0]
        );
        assert_eq!(pack(&json!(null)).unwrap(), [0xc0]);
        assert_eq!(pack(&json!(true)).unwrap(), [0xc3]);
    }

    #[test]
    fn string_lengths_count_bytes_not_characters() {
        // 16 characters of two bytes each: 32 bytes, one past the fixstr limit of 31
        let packed = pack(&json!("é".repeat(16))).unwrap();
        assert_eq!(&packed[..2], [0xd9, 32]);
        assert_eq!(pack(&json!("é".repeat(15))).unwrap()[0], 0xa0 | 30);
        let long = pack(&json!("x".repeat(70000))).unwrap();
        assert_eq!(&long[..5], [0xdb, 0x00, 0x01, 0x11, 0x70]);
    }

    #[test]
    fn every_row_of_rows_json_packs_to_the_recorded_bytes() {
        let file = fixture("rows.json");
        let mut checked = 0;
        for case in file["cases"].as_array().unwrap() {
            let Some(expected) = case["expect"]["row_msgpack"].as_str() else {
                continue;
            };
            let packed = pack(&case["expect"]["row"]).unwrap();
            assert_eq!(STANDARD.encode(packed), expected, "{}", case["name"]);
            checked += 1;
        }
        // rows.json has exactly 40 cases that carry expect.row_msgpack (the rest are error cases with no row).
        assert!(checked >= 40);
    }

    #[test]
    fn every_frame_of_socket_json_unpacks_and_packs_back_to_the_same_bytes() {
        let file = fixture("socket.json");
        let mut checked = 0;
        for case in file["cases"].as_array().unwrap() {
            for step in case["steps"].as_array().unwrap() {
                let Some(text) = step
                    .get("client")
                    .or_else(|| step.get("engine"))
                    .and_then(Json::as_str)
                else {
                    continue;
                };
                let bytes = STANDARD.decode(text).unwrap();
                let mut at = 0;
                while at + 4 <= bytes.len() {
                    let length = u32::from_be_bytes(bytes[at..at + 4].try_into().unwrap()) as usize;
                    if length > 64 * 1024 * 1024 || at + 4 + length > bytes.len() {
                        break; // the oversized header case: no body follows
                    }
                    let body = &bytes[at + 4..at + 4 + length];
                    assert_eq!(
                        pack(&unpack(body).unwrap()).unwrap(),
                        body,
                        "{}",
                        case["name"]
                    );
                    at += 4 + length;
                    checked += 1;
                }
            }
        }
        assert!(checked > 30);
    }

    #[test]
    fn unpack_refuses_what_json_cannot_hold_and_trailing_bytes() {
        assert_eq!(unpack(&[0x81, 0xa1, 0x61, 0x01]).unwrap(), json!({"a": 1}));
        assert_eq!(unpack(&[0xca, 0x3f, 0x80, 0, 0]).unwrap(), json!(1.0));
        assert!(unpack(&[0x01, 0x02]).unwrap_err().contains("extra data"));
        assert!(unpack(&[0xc4, 0x01, 0x00]).unwrap_err().contains("binary"));
        assert!(
            unpack(&[0x81, 0x01, 0x02])
                .unwrap_err()
                .contains("not a string")
        );
        assert!(
            unpack(&[0xcb, 0x7f, 0xf8, 0, 0, 0, 0, 0, 0])
                .unwrap_err()
                .contains("nan")
        );
        assert!(unpack(&[0xa2, 0xff, 0xfe]).unwrap_err().contains("UTF-8"));
        assert!(unpack(&[]).is_err());
    }
}
