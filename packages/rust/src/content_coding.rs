//! An HTTP answer's Content-Encoding undone as the Python SDK's httpx undoes it: gzip and deflate (what this client
//! asks for, ACCEPT_ENCODING), any number of layers, the last applied first; codings httpx does not know are passed
//! over, as httpx passes them over. Each layer is decoded by zlib (the zlib-rs port), as Python's zlib module decodes
//! it, so a broken body is refused with zlib's own message. What each layer decodes to is held to MAX_BODY, as the body
//! as sent is (http.rs), so a few kilobytes of stacked gzip cannot decode to gigabytes.

use flate2::{Decompress, DecompressError, FlushDecompress, Status};

/// What this client accepts, as httpx's default header says (br and zstd only when their Python packages are
/// installed, which the Python SDK does not require).
pub(crate) const ACCEPT_ENCODING: &str = "gzip, deflate";

/// The longest answer body this client reads, as sent and once each Content-Encoding layer is undone: 64 MiB (the
/// channel's frame limit, the same in every Jeff SDK). httpx has no limit; a Jeff answer is far smaller (a /v1/decide
/// answer of 1,000 calls is a few MiB).
pub(crate) const MAX_BODY: usize = crate::protocol::MAX_FRAME;

/// Why a body could not be decoded.
#[derive(Debug, PartialEq)]
pub(crate) enum Undecoded {
    /// A layer decodes to more than the limit.
    TooLarge,
    /// The bytes are not what their coding says (httpx's DecodingError), with Python's zlib.error message.
    Broken(String),
}

#[derive(Clone, Copy)]
enum Coding {
    Gzip,
    Deflate,
}

/// The body with every Content-Encoding httpx knows undone (`values`: each Content-Encoding header's value, in
/// order), each layer held to `limit` bytes.
pub(crate) fn decoded(
    raw: Vec<u8>,
    values: &[Vec<u8>],
    limit: usize,
) -> Result<Vec<u8>, Undecoded> {
    let mut codings = Vec::new();
    for value in values {
        for coding in value.split(|byte| *byte == b',') {
            match coding.trim_ascii().to_ascii_lowercase().as_slice() {
                b"gzip" => codings.push(Coding::Gzip),
                b"deflate" => codings.push(Coding::Deflate),
                _ => {} // identity, and codings httpx does not know
            }
        }
    }
    let mut body = raw;
    for coding in codings.into_iter().rev() {
        body = match coding {
            // as httpx's GZipDecoder: zlib.decompressobj(zlib.MAX_WBITS | 16), one gzip member
            Coding::Gzip => inflate(&body, Decompress::new_gzip(15), limit)?,
            // as httpx's DeflateDecoder: zlib-wrapped deflate, else raw deflate (whose failure is the one reported)
            Coding::Deflate => match inflate(&body, Decompress::new(true), limit) {
                Err(Undecoded::Broken(_)) => inflate(&body, Decompress::new(false), limit)?,
                decoded => decoded?,
            },
        };
    }
    Ok(body)
}

/// zlib's error as Python's zlib module words it ("Error -3 while decompressing data: incorrect header check").
fn zlib_error(error: &DecompressError) -> Undecoded {
    Undecoded::Broken(match error.message() {
        Some(message) => format!("Error -3 while decompressing data: {message}"),
        None if error.to_string().contains("requires a dictionary") => {
            "Error 2 while decompressing data".into()
        }
        None => "Error -3 while decompressing data: invalid input data".into(),
    })
}

/// Decodes `input` until its stream ends (bytes after it are passed over) or the input is used up (a stream cut short
/// gives what was decoded, as zlib's decompressobj reports no error for one), reading at most `limit` bytes of output.
fn inflate(input: &[u8], mut decoder: Decompress, limit: usize) -> Result<Vec<u8>, Undecoded> {
    let mut output = Vec::new();
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let used = decoder.total_in() as usize;
        let before = decoder.total_out();
        let status = decoder
            .decompress(&input[used..], &mut buffer, FlushDecompress::None)
            .map_err(|error| zlib_error(&error))?;
        let produced = (decoder.total_out() - before) as usize;
        if output.len() + produced > limit {
            return Err(Undecoded::TooLarge);
        }
        output.extend_from_slice(&buffer[..produced]);
        if status == Status::StreamEnd || (produced == 0 && decoder.total_in() as usize == used) {
            return Ok(output);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::Compression;
    use flate2::write::{DeflateEncoder, GzEncoder, ZlibEncoder};
    use std::io::Write;

    fn gzip(data: &[u8]) -> Vec<u8> {
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(data).unwrap();
        encoder.finish().unwrap()
    }

    fn header(value: &str) -> Vec<Vec<u8>> {
        vec![value.as_bytes().to_vec()]
    }

    #[test]
    fn each_layer_is_undone_last_first_and_held_to_the_limit() {
        let data = vec![0u8; 1000];
        let twice = gzip(&gzip(&data));
        assert_eq!(
            decoded(twice.clone(), &header("gzip, gzip"), 1000).unwrap(),
            data
        );
        assert_eq!(
            decoded(twice.clone(), &[b"gzip".to_vec(), b" GZIP ".to_vec()], 1000).unwrap(),
            data
        );
        assert_eq!(
            decoded(twice, &header("gzip, gzip"), 999),
            Err(Undecoded::TooLarge)
        );
        // identity and codings httpx does not know are passed over
        assert_eq!(
            decoded(gzip(b"{}"), &header("identity, gzip, br"), 10).unwrap(),
            b"{}"
        );
        assert_eq!(decoded(b"{}".to_vec(), &header("br"), 10).unwrap(), b"{}");
    }

    #[test]
    fn deflate_is_zlib_wrapped_or_raw_and_an_overlong_one_is_not_retried_as_raw() {
        let data = vec![7u8; 1000];
        let mut zlib = ZlibEncoder::new(Vec::new(), Compression::default());
        zlib.write_all(&data).unwrap();
        let zlib = zlib.finish().unwrap();
        let mut raw = DeflateEncoder::new(Vec::new(), Compression::default());
        raw.write_all(&data).unwrap();
        let raw = raw.finish().unwrap();
        for body in [&zlib, &raw] {
            assert_eq!(
                decoded(body.clone(), &header("deflate"), 1000).unwrap(),
                data
            );
            assert_eq!(
                decoded(body.clone(), &header("deflate"), 999),
                Err(Undecoded::TooLarge)
            );
        }
    }

    #[test]
    fn a_body_that_is_not_its_coding_is_refused_with_zlibs_message_and_a_cut_one_gives_what_was_decoded()
     {
        assert_eq!(
            decoded(b"not gzip".to_vec(), &header("gzip"), 100),
            Err(Undecoded::Broken(
                "Error -3 while decompressing data: incorrect header check".into()
            ))
        );
        let whole = gzip(&[1u8; 5000]);
        let cut = decoded(whole[..whole.len() - 12].to_vec(), &header("gzip"), 10_000).unwrap();
        assert!(cut.len() <= 5000 && cut.iter().all(|byte| *byte == 1));
        // bytes after the gzip member are passed over
        let mut trailing = gzip(b"{}");
        trailing.extend_from_slice(b"junk");
        assert_eq!(decoded(trailing, &header("gzip"), 10).unwrap(), b"{}");
        assert_eq!(decoded(Vec::new(), &header("gzip"), 10).unwrap(), b"");
    }
}
