//! YAML as PyYAML's `yaml.safe_load` reads it (YAML 1.1), into JSON values whose objects keep document order. See
//! Task 5 of the Rust SDK plan for what is refused and why.

use std::collections::HashMap;
use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;
use saphyr_parser::{Event, Parser, ScalarStyle};

use crate::errors::{ErrorType, JeffError, Result};
use crate::json::{Json, JsonMap};
use crate::pyformat::{py_float, py_repr, py_str};

fn pattern(text: &str) -> Regex {
    Regex::new(text).expect("a PyYAML resolver pattern")
}

// PyYAML's implicit resolvers (yaml/resolver.py), verbose flags removed.
static BOOL: LazyLock<Regex> = LazyLock::new(|| {
    pattern(r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$")
});
static FLOAT: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?",
        r"|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?",
        r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*",
        r"|[-+]?\.(?:inf|Inf|INF)",
        r"|\.(?:nan|NaN|NAN))$"
    ))
});
static INT: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"^(?:[-+]?0b[0-1_]+",
        r"|[-+]?0[0-7_]+",
        r"|[-+]?(?:0|[1-9][0-9_]*)",
        r"|[-+]?0x[0-9a-fA-F_]+",
        r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$"
    ))
});
static MERGE: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(?:<<)$"));
static NULL: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(?:~|null|Null|NULL|)$"));
static TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
        r"|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?",
        r"(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?",
        r"(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$"
    ))
});
static VALUE: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(?:=)$"));
static DATE: LazyLock<Regex> = LazyLock::new(|| pattern(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$"));

fn sexagesimal(text: &str) -> f64 {
    text.split(':').fold(0.0, |total, part| {
        total * 60.0 + part.parse::<f64>().expect("digits the resolver matched")
    })
}

fn integer(text: &str) -> std::result::Result<Json, String> {
    let cleaned = text.replace('_', "");
    let (negative, digits) = match cleaned.strip_prefix('-') {
        Some(rest) => (true, rest.to_owned()),
        None => (
            false,
            cleaned.strip_prefix('+').unwrap_or(&cleaned).to_owned(),
        ),
    };
    let too_big = || {
        format!(
            "{} is an integer beyond 64 bits, which JSON numbers here cannot hold; quote it",
            py_str(text)
        )
    };
    let magnitude: u128 = if digits == "0" {
        0
    } else if let Some(binary) = digits.strip_prefix("0b") {
        u128::from_str_radix(binary, 2).map_err(|_| too_big())?
    } else if let Some(hex) = digits.strip_prefix("0x") {
        u128::from_str_radix(hex, 16).map_err(|_| too_big())?
    } else if let Some(octal) = digits.strip_prefix('0') {
        u128::from_str_radix(octal, 8).map_err(|_| too_big())?
    } else if digits.contains(':') {
        digits.split(':').try_fold(0_u128, |total, part| {
            total
                .checked_mul(60)
                .and_then(|t| t.checked_add(part.parse::<u128>().ok()?))
                .ok_or_else(too_big)
        })?
    } else {
        digits.parse::<u128>().map_err(|_| too_big())?
    };
    if negative {
        let value = i64::try_from(magnitude).map(|m| -m).or_else(|_| {
            if magnitude == 1_u128 << 63 {
                Ok(i64::MIN)
            } else {
                Err(too_big())
            }
        })?;
        Ok(Json::from(value))
    } else {
        u64::try_from(magnitude)
            .map(Json::from)
            .map_err(|_| too_big())
    }
}

fn floating(text: &str) -> std::result::Result<Json, String> {
    let cleaned = text.replace('_', "").to_lowercase();
    let (sign, body) = match cleaned.strip_prefix('-') {
        Some(rest) => (-1.0, rest.to_owned()),
        None => (
            1.0,
            cleaned.strip_prefix('+').unwrap_or(&cleaned).to_owned(),
        ),
    };
    let value = if body == ".inf" {
        f64::INFINITY
    } else if body == ".nan" {
        f64::NAN
    } else if body.contains(':') {
        sign * sexagesimal(&body)
    } else {
        sign * body
            .parse::<f64>()
            .map_err(|error| format!("{} is not a float: {error}", py_str(text)))?
    };
    serde_json::Number::from_f64(value)
        .map(Json::Number)
        .ok_or_else(|| {
            format!(
                "{} is not a finite number ({}), which JSON cannot hold; quote it",
                py_str(text),
                py_float(value)
            )
        })
}

fn real_date(year: u32, month: u32, day: u32) -> bool {
    let leap = (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400);
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return false,
    };
    (1..=9999).contains(&year) && (1..=days).contains(&day)
}

fn timestamp(text: &str) -> std::result::Result<Json, String> {
    let Some(parts) = DATE.captures(text) else {
        return Err(format!(
            "{} is a YAML timestamp with a time part; this SDK only reads a manifest date as a bare YYYY-MM-DD, so quote \
             it to keep it a plain string",
            py_str(text)
        ));
    };
    let number = |index: usize| {
        parts[index]
            .parse::<u32>()
            .expect("digits the pattern matched")
    };
    if !real_date(number(1), number(2), number(3)) {
        return Err(format!(
            "{} is not a real calendar date; quote it to keep it a plain string",
            py_str(text)
        ));
    }
    Ok(Json::String(text.to_owned()))
}

/// A plain scalar resolved as PyYAML's SafeLoader resolves it.
fn resolve(text: &str) -> std::result::Result<Json, String> {
    if BOOL.is_match(text) {
        return Ok(Json::Bool(matches!(
            text.to_lowercase().as_str(),
            "yes" | "true" | "on"
        )));
    }
    if FLOAT.is_match(text) {
        return floating(text);
    }
    if INT.is_match(text) {
        return integer(text);
    }
    if MERGE.is_match(text) {
        return Err("merge keys (<<) are not supported; write the mapping out in full".into());
    }
    if NULL.is_match(text) {
        return Ok(Json::Null);
    }
    if TIMESTAMP.is_match(text) {
        return timestamp(text);
    }
    if VALUE.is_match(text) {
        return Err("the value key (=) is not supported; quote it".into());
    }
    Ok(Json::String(text.to_owned()))
}

enum Open {
    Sequence {
        items: Vec<Json>,
        anchor: usize,
    },
    Mapping {
        map: JsonMap,
        key: Option<String>,
        anchor: usize,
    },
}

fn add(
    stack: &mut [Open],
    documents: &mut Vec<Json>,
    value: Json,
    line: usize,
) -> std::result::Result<(), String> {
    match stack.last_mut() {
        None => documents.push(value),
        Some(Open::Sequence { items, .. }) => items.push(value),
        Some(Open::Mapping { map, key, .. }) => match key.take() {
            None => match value {
                Json::String(text) => *key = Some(text),
                other => {
                    return Err(format!(
                        "line {line}: a mapping key that is not a string ({}); quote it",
                        py_repr(&other)
                    ));
                }
            },
            // A repeated key keeps its first place and takes the last value, as PyYAML's dict does.
            Some(name) => {
                map.insert(name, value);
            }
        },
    }
    Ok(())
}

fn refuse_tag(tag: Option<&saphyr_parser::Tag>, line: usize) -> std::result::Result<(), String> {
    match tag {
        None => Ok(()),
        Some(tag) => Err(format!(
            "line {line}: the tag {}{} is not supported; write the value without a tag",
            tag.handle, tag.suffix
        )),
    }
}

/// The most values that aliases may add to one document. Each alias is expanded by copying its anchored value (PyYAML
/// shares one object instead), so a few hundred bytes of nested aliases could otherwise build gigabytes.
const ALIAS_EXPANSION_LIMIT: usize = 1_000_000;

/// The number of values in `value`, itself included (counted without recursion, whatever the depth).
fn count_values(value: &Json) -> usize {
    let mut count = 0;
    let mut pending = vec![value];
    while let Some(next) = pending.pop() {
        count += 1;
        match next {
            Json::Array(items) => pending.extend(items),
            Json::Object(map) => pending.extend(map.values()),
            _ => {}
        }
    }
    count
}

/// An anchored value and the number of values an alias to it adds.
struct Anchored {
    value: Json,
    size: usize,
}

fn remember(anchors: &mut HashMap<usize, Anchored>, anchor: usize, value: &Json) {
    if anchor > 0 {
        anchors.insert(
            anchor,
            Anchored {
                value: value.clone(),
                size: count_values(value),
            },
        );
    }
}

/// A plain scalar that is a non-finite float (.nan, .inf, -.inf) in a value's place, found while reading: where it is
/// in the document (a JSON pointer, as the Python SDK names it) and its value.
struct NonFinite {
    pointer: String,
    value: f64,
}

/// The JSON pointer of the value about to be added: each open sequence's next index, each open mapping's pending key;
/// None while a mapping key is being read (a key is not a value).
fn pointer_of(stack: &[Open]) -> Option<String> {
    let mut pointer = String::new();
    for open in stack {
        match open {
            Open::Sequence { items, .. } => pointer.push_str(&format!("/{}", items.len())),
            Open::Mapping { key: Some(key), .. } => pointer.push_str(&format!("/{key}")),
            Open::Mapping { key: None, .. } => return None,
        }
    }
    Some(if pointer.is_empty() {
        "/".to_owned()
    } else {
        pointer
    })
}

/// The non-finite float a plain scalar resolves to, if it is one (PyYAML's .inf, -.inf, +.inf and .nan in any case).
fn non_finite(text: &str) -> Option<f64> {
    if !FLOAT.is_match(text) {
        return None;
    }
    let cleaned = text.replace('_', "").to_lowercase();
    match cleaned.as_str() {
        ".inf" | "+.inf" => Some(f64::INFINITY),
        "-.inf" => Some(f64::NEG_INFINITY),
        ".nan" => Some(f64::NAN),
        _ => None,
    }
}

fn parse(text: &str, found: &mut Option<NonFinite>) -> std::result::Result<Json, String> {
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut stack: Vec<Open> = Vec::new();
    let mut anchors: HashMap<usize, Anchored> = HashMap::new();
    let mut expanded: usize = 0;
    let mut documents: Vec<Json> = Vec::new();
    for event in Parser::new_from_str(text) {
        let (event, span) = event.map_err(|error| error.to_string())?;
        let line = span.start.line();
        match event {
            Event::Scalar(value, style, anchor, tag) => {
                refuse_tag(tag.as_deref(), line)?;
                let resolved = if style == ScalarStyle::Plain {
                    if let (Some(number), Some(pointer)) = (non_finite(&value), pointer_of(&stack))
                    {
                        *found = Some(NonFinite {
                            pointer,
                            value: number,
                        });
                    }
                    resolve(&value).map_err(|reason| format!("line {line}: {reason}"))?
                } else {
                    Json::String(value.into_owned())
                };
                remember(&mut anchors, anchor, &resolved);
                add(&mut stack, &mut documents, resolved, line)?;
            }
            Event::SequenceStart(anchor, tag) => {
                refuse_tag(tag.as_deref(), line)?;
                stack.push(Open::Sequence {
                    items: Vec::new(),
                    anchor,
                });
            }
            Event::MappingStart(anchor, tag) => {
                refuse_tag(tag.as_deref(), line)?;
                stack.push(Open::Mapping {
                    map: JsonMap::new(),
                    key: None,
                    anchor,
                });
            }
            Event::SequenceEnd | Event::MappingEnd => {
                let (value, anchor) = match stack.pop() {
                    Some(Open::Sequence { items, anchor }) => (Json::Array(items), anchor),
                    Some(Open::Mapping { map, anchor, .. }) => (Json::Object(map), anchor),
                    None => {
                        return Err(format!(
                            "line {line}: a collection ended that was never opened"
                        ));
                    }
                };
                remember(&mut anchors, anchor, &value);
                add(&mut stack, &mut documents, value, line)?;
            }
            Event::Alias(anchor) => {
                let anchored = anchors.get(&anchor).ok_or_else(|| {
                    format!("line {line}: an alias to an anchor that is not defined before it")
                })?;
                expanded = expanded.saturating_add(anchored.size);
                if expanded > ALIAS_EXPANSION_LIMIT {
                    return Err(format!(
                        "line {line}: the aliases expand to more than {ALIAS_EXPANSION_LIMIT} values; write the \
                         repeated parts out or use fewer aliases"
                    ));
                }
                let value = anchored.value.clone();
                add(&mut stack, &mut documents, value, line)?;
            }
            Event::Nothing
            | Event::StreamStart
            | Event::StreamEnd
            | Event::DocumentStart(_)
            | Event::DocumentEnd => {}
        }
    }
    match documents.len() {
        0 => Ok(Json::Null),
        1 => Ok(documents.pop().expect("one document")),
        count => Err(format!(
            "expected a single document in the stream, found {count}"
        )),
    }
}

/// `text` (the contents of the file `path`) as PyYAML's safe_load reads it. A non-finite number (.nan, .inf), which
/// JSON cannot hold, is refused by its line ("'.nan' is not a finite number (nan), which JSON cannot hold; quote
/// it").
pub fn read_yaml(text: &str, path: &str) -> Result<Json> {
    parse(text, &mut None).map_err(|reason| {
        JeffError::new(
            ErrorType::ManifestError,
            format!("{path} is not valid YAML: {reason}"),
        )
    })
}

/// A manifest's YAML (`text`, the contents of the file `path`), as read_yaml reads it, except that a non-finite
/// number in a value's place is refused as the Python SDK refuses it in a manifest (contract/conformance/
/// manifests.json), by its place and value: "<path>: /functions/is_spam/calibration/threshold is nan, not a finite
/// number, which JSON cannot hold; write a finite number". A manifest's number is never text to quote: a threshold of
/// nan would turn calibration off without a word.
pub fn read_manifest_yaml(text: &str, path: &str) -> Result<Json> {
    let mut found = None;
    parse(text, &mut found).map_err(|reason| match found {
        Some(NonFinite { pointer, value }) => JeffError::new(
            ErrorType::ManifestError,
            format!(
                "{path}: {pointer} is {}, not a finite number, which JSON cannot hold; write a finite number",
                py_float(value)
            ),
        ),
        None => JeffError::new(
            ErrorType::ManifestError,
            format!("{path} is not valid YAML: {reason}"),
        ),
    })
}

/// The manifest file at `path`, read as read_manifest_yaml reads it.
pub fn read_manifest_yaml_file(path: &Path) -> Result<Json> {
    let text = std::fs::read_to_string(path).map_err(|error| {
        JeffError::new(
            ErrorType::ManifestError,
            format!("{} cannot be read: {error}", path.display()),
        )
    })?;
    read_manifest_yaml(&text, &path.display().to_string())
}

/// The YAML file at `path` as PyYAML's safe_load reads it.
pub fn read_yaml_file(path: &Path) -> Result<Json> {
    let text = std::fs::read_to_string(path).map_err(|error| {
        JeffError::new(
            ErrorType::ManifestError,
            format!("{} cannot be read: {error}", path.display()),
        )
    })?;
    read_yaml(&text, &path.display().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn one(text: &str) -> Json {
        read_yaml(&format!("d: {text}\n"), "doc.yaml").unwrap()["d"].clone()
    }

    fn refused(text: &str) -> String {
        read_yaml(&format!("d: {text}\n"), "doc.yaml")
            .unwrap_err()
            .message()
            .to_owned()
    }

    #[test]
    fn plain_scalars_resolve_as_pyyaml_resolves_them() {
        assert_eq!(one("yes"), json!(true));
        assert_eq!(one("Off"), json!(false));
        assert_eq!(one("~"), json!(null));
        assert_eq!(one(""), json!(null));
        assert_eq!(one("012"), json!(10));
        assert_eq!(one("0x1F"), json!(31));
        assert_eq!(one("-0b101"), json!(-5));
        assert_eq!(one("1_000"), json!(1000));
        assert_eq!(one("190:20:30"), json!(685230));
        assert_eq!(one("0.6"), json!(0.6));
        assert_eq!(one("1.0e+5"), json!(100000.0));
        assert_eq!(one("1."), json!(1.0));
        assert_eq!(one("1e-05"), json!("1e-05"));
        assert_eq!(one("'yes'"), json!("yes"));
        assert_eq!(one("\"012\""), json!("012"));
        assert_eq!(one("13.380 and more"), json!("13.380 and more"));
    }

    #[test]
    fn a_bare_date_is_its_own_text_and_anything_else_dated_is_refused() {
        assert_eq!(one("2026-10-03"), json!("2026-10-03"));
        assert_eq!(one("'2026-10-03'"), json!("2026-10-03"));
        assert_eq!(one("0099-01-01"), json!("0099-01-01"));
        assert!(refused("2026-10-03T10:11:12Z").contains("is a YAML timestamp with a time part"));
        assert_eq!(
            refused("2026-02-30"),
            "doc.yaml is not valid YAML: line 1: '2026-02-30' is not a real calendar date; quote it to keep it a plain string"
        );
        assert!(refused("0000-01-01").contains("not a real calendar date"));
        assert_eq!(one("2024-02-29"), json!("2024-02-29"));
        assert!(refused("2023-02-29").contains("not a real calendar date"));
    }

    #[test]
    fn a_repeated_key_keeps_its_first_place_and_its_last_value() {
        let value = read_yaml("a: 1\nb: 2\na: 3\n", "doc.yaml").unwrap();
        assert_eq!(value, json!({"a": 3, "b": 2}));
        assert_eq!(
            value.as_object().unwrap().keys().collect::<Vec<_>>(),
            ["a", "b"]
        );
    }

    #[test]
    fn number_like_keys_keep_their_document_order() {
        let value = read_yaml(
            "fixed:\n  '2': Two\n  '1': One\n  legit: Fine\n",
            "doc.yaml",
        )
        .unwrap();
        assert_eq!(
            value["fixed"]
                .as_object()
                .unwrap()
                .keys()
                .collect::<Vec<_>>(),
            ["2", "1", "legit"]
        );
    }

    #[test]
    fn anchors_and_aliases_are_read_and_what_json_cannot_hold_is_refused() {
        assert_eq!(
            read_yaml("a: &x [1, 2]\nb: *x\n", "doc.yaml").unwrap(),
            json!({"a": [1, 2], "b": [1, 2]})
        );
        assert!(refused(".inf").contains("not a finite number"));
        assert!(refused("99999999999999999999").contains("beyond 64 bits"));
        assert!(
            read_yaml("<<: {a: 1}\n", "doc.yaml")
                .unwrap_err()
                .message()
                .contains("merge keys")
        );
        assert!(
            read_yaml("a: !!str 1\n", "doc.yaml")
                .unwrap_err()
                .message()
                .contains("is not supported; write the value without a tag")
        );
        assert!(
            read_yaml("1: one\n", "doc.yaml")
                .unwrap_err()
                .message()
                .contains("a mapping key that is not a string (1)")
        );
        assert!(
            read_yaml("a: 1\n---\nb: 2\n", "doc.yaml")
                .unwrap_err()
                .message()
                .contains("single document")
        );
        assert!(
            read_yaml("a: [1\n", "doc.yaml")
                .unwrap_err()
                .message()
                .starts_with("doc.yaml is not valid YAML: ")
        );
        assert_eq!(read_yaml("", "doc.yaml").unwrap(), json!(null));
    }

    #[test]
    fn aliases_that_expand_past_the_limit_are_refused_by_name_before_memory_runs_out() {
        // 7 levels of 10 aliases: about 450 bytes that would expand to 11 million values. PyYAML's safe_load shares
        // the aliased objects and loads it in about 32 MB; copying them, this reader must stop at the limit instead.
        let mut text = String::from("a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n");
        for level in 1..8 {
            let aliases = vec![format!("*a{}", level - 1); 10].join(", ");
            text.push_str(&format!("a{level}: &a{level} [{aliases}]\n"));
        }
        let error = read_yaml(&text, "laughs.yaml").unwrap_err();
        assert_eq!(error.kind(), ErrorType::ManifestError);
        assert!(
            error
                .message()
                .starts_with("laughs.yaml is not valid YAML: line ")
        );
        assert!(
            error
                .message()
                .contains("the aliases expand to more than 1000000 values")
        );
        // Below the limit (5 levels, 123,440 values from aliases) the same shape is read in full.
        let small: String = text
            .lines()
            .take(5)
            .map(|line| format!("{line}\n"))
            .collect();
        let read = read_yaml(&small, "small.yaml").unwrap();
        assert_eq!(read["a4"][9][9][9][9][9], json!("x"));
    }

    #[test]
    fn windows_line_endings_and_a_byte_order_mark_read_as_pyyaml_reads_them() {
        let plain = "a: 1\nb:\n  - x\n  - 'y z'\nc: >\n  folded\n  text\n";
        let windows = format!("\u{feff}{}", plain.replace('\n', "\r\n"));
        assert_eq!(
            read_yaml(&windows, "doc.yaml").unwrap(),
            read_yaml(plain, "doc.yaml").unwrap()
        );
        assert_eq!(
            read_yaml(plain, "doc.yaml").unwrap()["c"],
            json!("folded text\n")
        );
    }

    #[test]
    fn every_shipped_manifest_and_bases_read_exactly_as_the_python_sdk_read_them() {
        let path = crate::shipped::repository_root().join("contract/conformance/catalogues.json");
        let file: Json = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let manifests = file["shipped"]["manifests"].as_object().unwrap();
        // the fixture covers every manifest the crate ships, no more and no fewer
        let shipped: std::collections::BTreeSet<&str> = crate::shipped::paths()
            .into_iter()
            .filter_map(|path| {
                path.strip_prefix("catalogue/")?
                    .strip_suffix("/manifest.yaml")
            })
            .collect();
        let recorded: std::collections::BTreeSet<&str> =
            manifests.keys().map(String::as_str).collect();
        assert_eq!(recorded, shipped);
        for (id, expected) in manifests {
            let name = format!("catalogue/{id}/manifest.yaml");
            let read = read_yaml(crate::shipped::file(&name).unwrap(), &name).unwrap();
            assert_eq!(&read, expected, "{id}");
        }
        let bases = read_yaml(
            crate::shipped::file("catalogue/bases.yaml").unwrap(),
            "bases.yaml",
        )
        .unwrap();
        assert_eq!(bases, file["shipped"]["bases"]);
    }
}
