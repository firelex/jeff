//! Python's own text forms and arithmetic, so that every message, reason and number this crate produces is the one
//! the Python SDK (the reference) produces. contract/conformance pins them.

use unicode_general_category::{GeneralCategory, get_general_category};

use crate::json::Json;

/// The characters Python's `\s` matches in a str pattern (`str.isspace`), as the body of a regex character class.
pub const PY_SPACE: &str = r"\t\n\x0B\x0C\r\x1C-\x20\x{85}\x{A0}\x{1680}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}";

/// Python's `str.isprintable` for one character (Unicode 16 tables; CPython 3.13 uses 15.1, which differs only for
/// characters assigned in 16).
fn printable(character: char) -> bool {
    character == ' '
        || !matches!(
            get_general_category(character),
            GeneralCategory::Control
                | GeneralCategory::Format
                | GeneralCategory::Surrogate
                | GeneralCategory::PrivateUse
                | GeneralCategory::Unassigned
                | GeneralCategory::LineSeparator
                | GeneralCategory::ParagraphSeparator
                | GeneralCategory::SpaceSeparator
        )
}

/// `repr()` of a str: single quotes unless the text holds a single quote and no double quote.
pub fn py_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        let code = character as u32;
        if character == quote || character == '\\' {
            out.push('\\');
            out.push(character);
        } else if character == '\t' {
            out.push_str("\\t");
        } else if character == '\n' {
            out.push_str("\\n");
        } else if character == '\r' {
            out.push_str("\\r");
        } else if !printable(character) {
            if code <= 0xff {
                out.push_str(&format!("\\x{code:02x}"));
            } else if code <= 0xffff {
                out.push_str(&format!("\\u{code:04x}"));
            } else {
                out.push_str(&format!("\\U{code:08x}"));
            }
        } else {
            out.push(character);
        }
    }
    out.push(quote);
    out
}

/// `repr()` of a float: the shortest digits that read back the same, in scientific form below 1e-4 and from 1e16.
pub fn py_float(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value > 0.0 {
            "inf".into()
        } else {
            "-inf".into()
        };
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    // Rust's `{:e}` writes the shortest round-tripping digits, e.g. "7.71902e-1", "1e16", but when two candidates
    // of that length are equally near the value it takes the upper one, where Python takes the even one
    // (repr(28708591244768.3125) is '28708591244768.312'). So take only the digit count from it, and the digits from
    // the exact-precision form, which rounds an exact half to even.
    let shortest = format!("{:e}", value.abs());
    let (shortest_mantissa, _) = shortest
        .split_once('e')
        .expect("LowerExp always writes an exponent");
    let count = shortest_mantissa
        .chars()
        .filter(|character| *character != '.')
        .count();
    let scientific = format!("{:.precision$e}", value.abs(), precision = count - 1);
    let (mantissa, exponent) = scientific
        .split_once('e')
        .expect("LowerExp always writes an exponent");
    let exponent: i32 = exponent
        .parse()
        .expect("LowerExp writes a decimal exponent");
    let digits: String = mantissa
        .chars()
        .filter(|character| *character != '.')
        .collect();
    let body = if !(-4..16).contains(&exponent) {
        let shown = if digits.len() > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits.clone()
        };
        format!(
            "{shown}e{}{:02}",
            if exponent < 0 { '-' } else { '+' },
            exponent.abs()
        )
    } else if exponent < 0 {
        format!("0.{}{digits}", "0".repeat((-exponent - 1) as usize))
    } else if digits.len() <= exponent as usize + 1 {
        format!(
            "{digits}{}.0",
            "0".repeat(exponent as usize + 1 - digits.len())
        )
    } else {
        format!(
            "{}.{}",
            &digits[..exponent as usize + 1],
            &digits[exponent as usize + 1..]
        )
    };
    if value < 0.0 {
        format!("-{body}")
    } else {
        body
    }
}

/// `format(value, f".{digits}f")`. Rust's `{:.N}` rounds the exact binary value correctly (and keeps the sign of a
/// negative zero, as Python does: `-0.000`); only an exact half at the last digit needs care, where Python rounds to
/// even. An exact half happens exactly when value * 2**(digits + 1) is an odd integer.
///
/// NaN and the infinities print as Python prints them: `nan`, `inf`, `-inf`.
///
/// Panics when the value is an exact half at the last digit and the digit count is too large for 128-bit integer
/// arithmetic (about 30 digits and more); no caller formats more than a few digits.
pub fn py_fixed(value: f64, digits: u32) -> String {
    if !value.is_finite() {
        return py_float(value);
    }
    let scaled = value * 2f64.powi(digits as i32 + 1);
    if scaled.is_finite()
        && scaled.fract() == 0.0
        && scaled.abs() < 9_007_199_254_740_992.0
        && (scaled as i64) % 2 != 0
    {
        // twice = value * 10**digits * 2, an odd integer
        let twice = 5_i128
            .checked_pow(digits)
            .and_then(|power| power.checked_mul(scaled as i128))
            .unwrap_or_else(|| {
                panic!(
                    "py_fixed cannot round an exact half of {} to {digits} digits: the arithmetic overflows 128-bit integers",
                    py_float(value)
                )
            });
        let negative = twice < 0;
        let mut lower = (twice.abs() - 1) / 2;
        if lower % 2 == 1 {
            lower += 1;
        }
        let text = format!("{lower:0width$}", width = digits as usize + 1);
        let body = if digits == 0 {
            text
        } else {
            let split = text.len() - digits as usize;
            format!("{}.{}", &text[..split], &text[split..])
        };
        return if negative { format!("-{body}") } else { body };
    }
    format!("{value:.prec$}", prec = digits as usize)
}

/// How JavaScript (and Python's `str` of an int-valued number) prints a number: `1234` for a whole number, else the
/// float's repr.
pub fn format_js_number(value: f64) -> String {
    if value.is_finite() && value.fract() == 0.0 && value.abs() < 1e16 {
        format!("{}", value as i64)
    } else {
        py_float(value)
    }
}

/// `repr()` of a JSON value as Python holds it: integers print as ints, floats as `py_float`.
pub fn py_repr(value: &Json) -> String {
    match value {
        Json::Null => "None".into(),
        Json::Bool(true) => "True".into(),
        Json::Bool(false) => "False".into(),
        Json::Number(number) => match number.as_f64() {
            Some(float) if number.is_f64() => py_float(float),
            _ => number.to_string(),
        },
        Json::String(text) => py_str(text),
        Json::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Json::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(key, item)| format!("{}: {}", py_str(key), py_repr(item)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// A float as Python's `json.dumps` writes it (its default `allow_nan=True`): `float.__repr__`, and `NaN`, `Infinity`
/// and `-Infinity` for the numbers JSON has no text for.
pub fn py_json_float(value: f64) -> String {
    if value.is_nan() {
        "NaN".into()
    } else if value == f64::INFINITY {
        "Infinity".into()
    } else if value == f64::NEG_INFINITY {
        "-Infinity".into()
    } else {
        py_float(value)
    }
}

/// A str as Python's `json.dumps` writes it (its default `ensure_ascii=True`): `\"`, `\\`, `\n`, `\r`, `\t`, `\b`
/// and `\f` by name, every other character outside space to `~` as `\uXXXX` (lowercase hex, a surrogate pair above
/// U+FFFF).
pub fn py_json_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            other => {
                let mut units = [0_u16; 2];
                for unit in other.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
    out
}

/// Python's `json.dumps` of a value built from JSON, with its defaults (separators ", " and ": ", `ensure_ascii`):
/// ints as ints, floats as `float.__repr__`.
pub fn py_json_dumps(value: &Json) -> String {
    match value {
        Json::Null => "null".into(),
        Json::Bool(flag) => flag.to_string(),
        Json::Number(number) => match number.as_f64() {
            Some(float) if number.is_f64() => py_json_float(float),
            _ => number.to_string(),
        },
        Json::String(text) => py_json_string(text),
        Json::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(py_json_dumps)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Json::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(key, item)| format!("{}: {}", py_json_string(key), py_json_dumps(item)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// `sum()` of a list of Python floats as CPython 3.12 and later computes it: Neumaier's compensated summation, the
/// compensation added at the end only when it is non-zero and finite (so an infinite sum stays infinite).
///
/// Only a list of floats: CPython compensates only while every item is a float, and an int among them (as in
/// `sum([1e100, 1, -1e100])`, which gives `0.0`) ends the compensation for the rest of the list. Every caller in this
/// crate sums floats (probabilities, a score's weighted sum), so this function takes `f64`s only.
pub fn py_sum(values: &[f64]) -> f64 {
    let mut total = 0.0_f64;
    let mut compensation = 0.0_f64;
    for &value in values {
        let next = total + value;
        if total.abs() >= value.abs() {
            compensation += (total - next) + value;
        } else {
            compensation += (value - next) + total;
        }
        total = next;
    }
    if compensation != 0.0 && compensation.is_finite() {
        total += compensation;
    }
    total
}

/// `sorted()` of strs: by code point, which is UTF-8 byte order, which is Rust's `String` order.
pub fn py_sorted<I: IntoIterator<Item = String>>(values: I) -> Vec<String> {
    let mut sorted: Vec<String> = values.into_iter().collect();
    sorted.sort();
    sorted
}

/// `type(value).__name__` of the Python value JSON gives.
pub fn py_type_name(value: &Json) -> &'static str {
    match value {
        Json::Null => "NoneType",
        Json::Bool(_) => "bool",
        Json::Number(number) if number.is_f64() => "float",
        Json::Number(_) => "int",
        Json::String(_) => "str",
        Json::Array(_) => "list",
        Json::Object(_) => "dict",
    }
}

enum PyNumber {
    Int(i128),
    Float(f64),
}

fn as_py_number(value: &Json) -> Option<PyNumber> {
    match value {
        Json::Bool(flag) => Some(PyNumber::Int(i128::from(*flag))),
        Json::Number(number) => {
            if let Some(int) = number.as_i64() {
                Some(PyNumber::Int(i128::from(int)))
            } else if let Some(int) = number.as_u64() {
                Some(PyNumber::Int(i128::from(int)))
            } else {
                number.as_f64().map(PyNumber::Float)
            }
        }
        _ => None,
    }
}

fn numbers_equal(a: &PyNumber, b: &PyNumber) -> bool {
    match (a, b) {
        (PyNumber::Int(x), PyNumber::Int(y)) => x == y,
        (PyNumber::Float(x), PyNumber::Float(y)) => x == y,
        (PyNumber::Int(int), PyNumber::Float(float))
        | (PyNumber::Float(float), PyNumber::Int(int)) => {
            // Python compares an int and a float exactly: the float must be a whole number equal to the int.
            float.is_finite()
                && float.fract() == 0.0
                && float.abs() < 1.7e38
                && (*float as i128) == *int
        }
    }
}

/// Python's `==` between two values built from JSON: bools are the ints 0 and 1, ints and floats compare exactly,
/// lists in order, dicts whatever their key order.
pub fn py_equals(a: &Json, b: &Json) -> bool {
    if let (Some(x), Some(y)) = (as_py_number(a), as_py_number(b)) {
        return numbers_equal(&x, &y);
    }
    match (a, b) {
        (Json::Null, Json::Null) => true,
        (Json::String(x), Json::String(y)) => x == y,
        (Json::Array(x), Json::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_equals(p, q))
        }
        (Json::Object(x), Json::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(key, item)| y.get(key).is_some_and(|other| py_equals(item, other)))
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn repr_of_a_str_picks_pythons_quotes_and_escapes() {
        assert_eq!(py_str("plain"), "'plain'");
        assert_eq!(py_str("it's"), "\"it's\"");
        assert_eq!(py_str("it's \"x\""), "'it\\'s \"x\"'");
        assert_eq!(py_str("a\nb\tc\\"), "'a\\nb\\tc\\\\'");
        assert_eq!(py_str("\u{7}\u{a0}\u{2028}"), "'\\x07\\xa0\\u2028'");
        assert_eq!(py_str("café 💶"), "'café 💶'");
        assert_eq!(py_str("\u{e000}\u{10ffff}"), "'\\ue000\\U0010ffff'");
    }

    #[test]
    fn repr_of_a_float_is_pythons_shortest_form() {
        assert_eq!(py_float(1.0), "1.0");
        assert_eq!(py_float(0.1), "0.1");
        assert_eq!(py_float(0.771902), "0.771902");
        assert_eq!(py_float(1e-5), "1e-05");
        assert_eq!(py_float(0.0001), "0.0001");
        assert_eq!(py_float(1e16), "1e+16");
        assert_eq!(py_float(9999999999999998.0), "9999999999999998.0");
        assert_eq!(py_float(123456789.5), "123456789.5");
        assert_eq!(py_float(-2.5e-7), "-2.5e-07");
        assert_eq!(py_float(0.0), "0.0");
        assert_eq!(py_float(-0.0), "-0.0");
        assert_eq!(py_float(1.5e300), "1.5e+300");
        assert_eq!(py_float(f64::NAN), "nan");
        assert_eq!(py_float(f64::NEG_INFINITY), "-inf");
        assert_eq!(py_float(0.06176575000000001), "0.06176575000000001");
    }

    #[test]
    fn fixed_digits_round_exact_halves_to_even_as_pythons_format_does() {
        assert_eq!(py_fixed(0.3125, 3), "0.312");
        assert_eq!(py_fixed(0.4375, 3), "0.438");
        assert_eq!(py_fixed(0.0625, 3), "0.062");
        assert_eq!(py_fixed(0.689444, 3), "0.689");
        assert_eq!(py_fixed(0.1, 3), "0.100");
        assert_eq!(py_fixed(2.5, 0), "2");
        assert_eq!(py_fixed(-0.3125, 3), "-0.312");
        assert_eq!(py_fixed(1.25, 2), "1.25");
        assert_eq!(py_fixed(0.04, 2), "0.04");
        assert_eq!(py_fixed(1730000000.25, 1), "1730000000.2");
    }

    #[test]
    fn repr_of_json_values_tells_ints_from_floats() {
        assert_eq!(
            py_repr(&json!(["a", 1, 2.5, true, null])),
            "['a', 1, 2.5, True, None]"
        );
        assert_eq!(py_repr(&json!(1.0)), "1.0");
        assert_eq!(
            py_repr(&json!({"format": "bf16", "format_id": "x"})),
            "{'format': 'bf16', 'format_id': 'x'}"
        );
        assert_eq!(py_repr(&json!({"10": 1, "b": 2})), "{'10': 1, 'b': 2}");
    }

    #[test]
    fn sum_uses_pythons_compensated_summation() {
        let tenths = [0.1; 10];
        assert_eq!(tenths.iter().sum::<f64>(), 0.9999999999999999);
        assert_eq!(py_sum(&tenths), 1.0);
        assert_eq!(py_sum(&[1e100, 1.0, -1e100]), 1.0);
        assert_eq!(py_sum(&[]), 0.0);
        assert_eq!(py_sum(&[f64::INFINITY, 1.0]), f64::INFINITY);
    }

    #[test]
    fn sorted_compares_code_points() {
        let sorted = py_sorted(["b", "a", "\u{1f4b6}", "\u{ffff}"].map(String::from));
        assert_eq!(sorted, ["a", "b", "\u{ffff}", "\u{1f4b6}"]);
    }

    #[test]
    fn type_names_and_equality_follow_python() {
        let names: Vec<&str> = [
            json!(null),
            json!(true),
            json!(1),
            json!(1.5),
            json!("s"),
            json!([]),
            json!({}),
        ]
        .iter()
        .map(py_type_name)
        .collect();
        assert_eq!(
            names,
            ["NoneType", "bool", "int", "float", "str", "list", "dict"]
        );
        assert!(py_equals(&json!(true), &json!(1)));
        assert!(py_equals(&json!(false), &json!(0)));
        assert!(py_equals(&json!(1), &json!(1.0)));
        assert!(!py_equals(&json!(1), &json!("1")));
        assert!(py_equals(&json!("a"), &json!("a")));
        assert!(!py_equals(
            &json!(9007199254740993_i64),
            &json!(9007199254740992.0)
        ));
        assert!(py_equals(&json!({"a": [1, true]}), &json!({"a": [1.0, 1]})));
        assert!(!py_equals(&json!({"a": 1}), &json!({"a": 1, "b": 2})));
    }

    #[test]
    fn whole_number_floats_print_as_javascript_prints_them() {
        assert_eq!(format_js_number(1234.0), "1234");
        assert_eq!(format_js_number(1234.5), "1234.5");
    }

    #[test]
    fn float_repr_matches_python_3_13_at_the_edges() {
        // Each expected text is `repr()` in CPython 3.13.14.
        assert_eq!(py_float(1e21), "1e+21");
        assert_eq!(py_float(5e-324), "5e-324");
        assert_eq!(py_float(0.1 + 0.2), "0.30000000000000004");
    }

    #[test]
    fn fixed_digits_keep_the_sign_of_zero_as_python_does() {
        // format(-0.0, ".3f") == "-0.000", format(-0.0001, ".3f") == "-0.000", format(-0.5, ".0f") == "-0"
        assert_eq!(py_fixed(-0.0, 3), "-0.000");
        assert_eq!(py_fixed(-0.0001, 3), "-0.000");
        assert_eq!(py_fixed(-0.5, 0), "-0");
        assert_eq!(py_fixed(-0.0625, 3), "-0.062");
        assert_eq!(py_fixed(1e22, 2), "10000000000000000000000.00");
    }

    #[test]
    fn fixed_digits_print_nan_and_infinities_as_python_does() {
        // format(float("nan"), ".3f") == "nan", and "inf", "-inf" for the infinities.
        assert_eq!(py_fixed(f64::NAN, 3), "nan");
        assert_eq!(py_fixed(f64::INFINITY, 3), "inf");
        assert_eq!(py_fixed(f64::NEG_INFINITY, 3), "-inf");
    }

    #[test]
    #[should_panic(
        expected = "py_fixed cannot round an exact half of 4.336808689942018e-19 to 60 digits"
    )]
    fn fixed_digits_refuse_a_digit_count_too_large_for_exact_half_rounding() {
        py_fixed(2f64.powi(-61), 60);
    }

    #[test]
    // The literals are the exact binary values, written in full on purpose.
    #[allow(clippy::excessive_precision)]
    fn float_repr_breaks_a_tie_between_two_shortest_forms_to_even() {
        // repr() in CPython 3.13.14.
        assert_eq!(py_float(28708591244768.3125), "28708591244768.312");
        assert_eq!(py_float(1866365865376046.25), "1866365865376046.2");
        assert_eq!(py_float(704473730295756.25), "704473730295756.2");
    }

    #[test]
    fn float_repr_matches_python_on_quarters_between_1e15_and_2_to_the_51() {
        // 300 random multiples of 0.25 in [1e15, 2**51) and their repr() in CPython 3.13.14
        // (random.seed(7); random.randrange(4 * 10**15, 2**53) / 4).
        let cases: [(f64, &str); 300] = [
            (1004298017190426.8, "1004298017190426.8"),
            (1008157695337463.0, "1008157695337463.0"),
            (1026000071761944.8, "1026000071761944.8"),
            (1027163771206266.5, "1027163771206266.5"),
            (1032099651220127.8, "1032099651220127.8"),
            (1036208512063111.0, "1036208512063111.0"),
            (1037901946448881.5, "1037901946448881.5"),
            (1041508727909960.0, "1041508727909960.0"),
            (1042425287267865.0, "1042425287267865.0"),
            (1048181083769375.5, "1048181083769375.5"),
            (1062034496546189.0, "1062034496546189.0"),
            (1062747927863248.0, "1062747927863248.0"),
            (1063036387866559.0, "1063036387866559.0"),
            (1077567371838189.0, "1077567371838189.0"),
            (1083202375375422.8, "1083202375375422.8"),
            (1094264901929149.8, "1094264901929149.8"),
            (1095023227535972.0, "1095023227535972.0"),
            (1097289737943066.0, "1097289737943066.0"),
            (1101865127770219.5, "1101865127770219.5"),
            (1105266176985547.8, "1105266176985547.8"),
            (1108731017259284.0, "1108731017259284.0"),
            (1111665280661125.0, "1111665280661125.0"),
            (1114347661519120.5, "1114347661519120.5"),
            (1124751795364235.2, "1124751795364235.2"),
            (1127953046070359.8, "1127953046070359.8"),
            (1133104713552521.5, "1133104713552521.5"),
            (1137144888951518.8, "1137144888951518.8"),
            (1139060901137399.8, "1139060901137399.8"),
            (1139301839389546.2, "1139301839389546.2"),
            (1142694825735227.5, "1142694825735227.5"),
            (1146366756124742.5, "1146366756124742.5"),
            (1154840503318490.5, "1154840503318490.5"),
            (1157877411871245.0, "1157877411871245.0"),
            (1158336144232062.5, "1158336144232062.5"),
            (1164834381082446.0, "1164834381082446.0"),
            (1168012904415424.0, "1168012904415424.0"),
            (1172182510487342.5, "1172182510487342.5"),
            (1172803051216628.2, "1172803051216628.2"),
            (1174785485343860.0, "1174785485343860.0"),
            (1181355385598401.2, "1181355385598401.2"),
            (1184320931088126.8, "1184320931088126.8"),
            (1186864525422639.2, "1186864525422639.2"),
            (1188904882606595.0, "1188904882606595.0"),
            (1189288984917372.2, "1189288984917372.2"),
            (1190238310160924.0, "1190238310160924.0"),
            (1191221550568059.2, "1191221550568059.2"),
            (1191609475389769.8, "1191609475389769.8"),
            (1193327191923891.2, "1193327191923891.2"),
            (1193535561585718.0, "1193535561585718.0"),
            (1195342985852478.2, "1195342985852478.2"),
            (1199433192646699.5, "1199433192646699.5"),
            (1203344777238499.8, "1203344777238499.8"),
            (1204578961300490.8, "1204578961300490.8"),
            (1204937439000484.5, "1204937439000484.5"),
            (1207035178202815.0, "1207035178202815.0"),
            (1207601881233684.5, "1207601881233684.5"),
            (1210760315832383.5, "1210760315832383.5"),
            (1211952290092000.5, "1211952290092000.5"),
            (1211962580165642.8, "1211962580165642.8"),
            (1219393771789440.8, "1219393771789440.8"),
            (1220102870743510.5, "1220102870743510.5"),
            (1224097082675945.2, "1224097082675945.2"),
            (1230037611351552.0, "1230037611351552.0"),
            (1230537794774551.2, "1230537794774551.2"),
            (1231422125425937.2, "1231422125425937.2"),
            (1232060498281826.8, "1232060498281826.8"),
            (1247536319409385.2, "1247536319409385.2"),
            (1254115030325029.8, "1254115030325029.8"),
            (1259752237770154.8, "1259752237770154.8"),
            (1269999533394190.2, "1269999533394190.2"),
            (1270974560342283.5, "1270974560342283.5"),
            (1273994070078593.0, "1273994070078593.0"),
            (1275507961397755.5, "1275507961397755.5"),
            (1282581954170480.0, "1282581954170480.0"),
            (1294452759036973.2, "1294452759036973.2"),
            (1299883054572978.8, "1299883054572978.8"),
            (1300482964108586.0, "1300482964108586.0"),
            (1308330208255062.2, "1308330208255062.2"),
            (1309070015780168.5, "1309070015780168.5"),
            (1313564753592898.8, "1313564753592898.8"),
            (1314618848467118.5, "1314618848467118.5"),
            (1318761920346759.8, "1318761920346759.8"),
            (1323955520865544.2, "1323955520865544.2"),
            (1324523526136423.5, "1324523526136423.5"),
            (1325953274937837.0, "1325953274937837.0"),
            (1328033500336598.2, "1328033500336598.2"),
            (1337325442046929.5, "1337325442046929.5"),
            (1340693101258428.0, "1340693101258428.0"),
            (1341904663866261.5, "1341904663866261.5"),
            (1349597185137554.2, "1349597185137554.2"),
            (1351083887924132.5, "1351083887924132.5"),
            (1357712498203120.5, "1357712498203120.5"),
            (1366582424356822.5, "1366582424356822.5"),
            (1371461432968762.0, "1371461432968762.0"),
            (1374623240207993.8, "1374623240207993.8"),
            (1404799373122825.8, "1404799373122825.8"),
            (1407903835614170.2, "1407903835614170.2"),
            (1408817298511887.8, "1408817298511887.8"),
            (1410599506064151.8, "1410599506064151.8"),
            (1411984383329624.2, "1411984383329624.2"),
            (1412854470283107.2, "1412854470283107.2"),
            (1417375751482710.2, "1417375751482710.2"),
            (1423048522233169.8, "1423048522233169.8"),
            (1429214402338378.0, "1429214402338378.0"),
            (1430783339123902.2, "1430783339123902.2"),
            (1436048980496404.5, "1436048980496404.5"),
            (1438663125836510.2, "1438663125836510.2"),
            (1439429756975202.5, "1439429756975202.5"),
            (1446509785220951.0, "1446509785220951.0"),
            (1447262156054373.8, "1447262156054373.8"),
            (1448828109114660.5, "1448828109114660.5"),
            (1449015758405871.8, "1449015758405871.8"),
            (1450175093106265.2, "1450175093106265.2"),
            (1456188816686260.5, "1456188816686260.5"),
            (1462103072368252.8, "1462103072368252.8"),
            (1463581331824272.5, "1463581331824272.5"),
            (1468268338188237.8, "1468268338188237.8"),
            (1478934029173435.2, "1478934029173435.2"),
            (1479137158270914.5, "1479137158270914.5"),
            (1483457096078269.2, "1483457096078269.2"),
            (1484544990148078.8, "1484544990148078.8"),
            (1490202540589839.8, "1490202540589839.8"),
            (1490589750534679.5, "1490589750534679.5"),
            (1491359354349760.0, "1491359354349760.0"),
            (1497832855008947.0, "1497832855008947.0"),
            (1500818802144052.5, "1500818802144052.5"),
            (1501685221289541.5, "1501685221289541.5"),
            (1503755460494853.2, "1503755460494853.2"),
            (1514658187643476.0, "1514658187643476.0"),
            (1516937949924936.2, "1516937949924936.2"),
            (1519571004082357.8, "1519571004082357.8"),
            (1519606171826135.2, "1519606171826135.2"),
            (1528690358183919.0, "1528690358183919.0"),
            (1539053620906660.5, "1539053620906660.5"),
            (1541924016030435.8, "1541924016030435.8"),
            (1550699290067064.8, "1550699290067064.8"),
            (1553221288524404.0, "1553221288524404.0"),
            (1557671375801843.5, "1557671375801843.5"),
            (1558849549196147.0, "1558849549196147.0"),
            (1559406927274475.2, "1559406927274475.2"),
            (1569947682410849.0, "1569947682410849.0"),
            (1586710573244931.5, "1586710573244931.5"),
            (1589541732403949.2, "1589541732403949.2"),
            (1589735690317463.0, "1589735690317463.0"),
            (1594008461056283.8, "1594008461056283.8"),
            (1595489770933037.5, "1595489770933037.5"),
            (1605561475460798.5, "1605561475460798.5"),
            (1605816043668745.0, "1605816043668745.0"),
            (1607849482135477.5, "1607849482135477.5"),
            (1609161845061168.0, "1609161845061168.0"),
            (1612310150218057.8, "1612310150218057.8"),
            (1624166866105451.2, "1624166866105451.2"),
            (1625637738546692.2, "1625637738546692.2"),
            (1628092232044460.5, "1628092232044460.5"),
            (1628392466169776.8, "1628392466169776.8"),
            (1634851209303857.2, "1634851209303857.2"),
            (1639908411657702.0, "1639908411657702.0"),
            (1642987223608250.2, "1642987223608250.2"),
            (1646984943906621.8, "1646984943906621.8"),
            (1647239511489200.2, "1647239511489200.2"),
            (1652095239670532.0, "1652095239670532.0"),
            (1665303988988884.0, "1665303988988884.0"),
            (1671238457166729.2, "1671238457166729.2"),
            (1674742607579983.8, "1674742607579983.8"),
            (1676101483617195.8, "1676101483617195.8"),
            (1681799933534643.2, "1681799933534643.2"),
            (1693065457961709.2, "1693065457961709.2"),
            (1697173803351757.5, "1697173803351757.5"),
            (1701829008622387.2, "1701829008622387.2"),
            (1702228746151330.8, "1702228746151330.8"),
            (1711523010587052.8, "1711523010587052.8"),
            (1717450198839108.2, "1717450198839108.2"),
            (1728533341257740.8, "1728533341257740.8"),
            (1733216891612802.5, "1733216891612802.5"),
            (1734063153917891.2, "1734063153917891.2"),
            (1736448056894387.5, "1736448056894387.5"),
            (1740676732840218.0, "1740676732840218.0"),
            (1747086097115542.2, "1747086097115542.2"),
            (1755101422682970.2, "1755101422682970.2"),
            (1760516512765303.2, "1760516512765303.2"),
            (1761061783319893.2, "1761061783319893.2"),
            (1765887495041139.0, "1765887495041139.0"),
            (1770241994227519.8, "1770241994227519.8"),
            (1773438650335409.8, "1773438650335409.8"),
            (1774634560185454.2, "1774634560185454.2"),
            (1775252287654658.0, "1775252287654658.0"),
            (1781386265605319.5, "1781386265605319.5"),
            (1787048449180667.0, "1787048449180667.0"),
            (1788532046051363.8, "1788532046051363.8"),
            (1800434223344228.5, "1800434223344228.5"),
            (1800955745489169.5, "1800955745489169.5"),
            (1803024876803237.5, "1803024876803237.5"),
            (1814211395549452.0, "1814211395549452.0"),
            (1814593728247838.0, "1814593728247838.0"),
            (1819972890229110.2, "1819972890229110.2"),
            (1824052528166944.2, "1824052528166944.2"),
            (1824572152751243.8, "1824572152751243.8"),
            (1847193695905799.0, "1847193695905799.0"),
            (1848068248794007.2, "1848068248794007.2"),
            (1856678347480449.2, "1856678347480449.2"),
            (1857669365094874.2, "1857669365094874.2"),
            (1865465994330449.8, "1865465994330449.8"),
            (1871104481701413.2, "1871104481701413.2"),
            (1874905142413794.2, "1874905142413794.2"),
            (1883525369243508.0, "1883525369243508.0"),
            (1887160973208544.2, "1887160973208544.2"),
            (1887479184449153.0, "1887479184449153.0"),
            (1889062687212365.8, "1889062687212365.8"),
            (1895985204653024.8, "1895985204653024.8"),
            (1896372980890728.8, "1896372980890728.8"),
            (1901717253698370.8, "1901717253698370.8"),
            (1902261838418213.8, "1902261838418213.8"),
            (1904422884342912.5, "1904422884342912.5"),
            (1909298817281118.8, "1909298817281118.8"),
            (1914113199672533.0, "1914113199672533.0"),
            (1938183139708805.0, "1938183139708805.0"),
            (1940712248305420.2, "1940712248305420.2"),
            (1941639833016790.5, "1941639833016790.5"),
            (1943817226777957.5, "1943817226777957.5"),
            (1948629790790707.2, "1948629790790707.2"),
            (1949580213201888.5, "1949580213201888.5"),
            (1964532875206686.2, "1964532875206686.2"),
            (1966573399881124.2, "1966573399881124.2"),
            (1969449086588086.2, "1969449086588086.2"),
            (1969941328832108.0, "1969941328832108.0"),
            (1977111693977183.8, "1977111693977183.8"),
            (1992203167785026.5, "1992203167785026.5"),
            (2003494221441641.2, "2003494221441641.2"),
            (2003597803206630.2, "2003597803206630.2"),
            (2006680134463541.2, "2006680134463541.2"),
            (2007744110988916.2, "2007744110988916.2"),
            (2010684971137864.0, "2010684971137864.0"),
            (2016431751600176.8, "2016431751600176.8"),
            (2018209515758292.5, "2018209515758292.5"),
            (2028269989469849.2, "2028269989469849.2"),
            (2028956056079693.0, "2028956056079693.0"),
            (2032835002896174.2, "2032835002896174.2"),
            (2036594625802654.2, "2036594625802654.2"),
            (2042945186032383.5, "2042945186032383.5"),
            (2047883871230146.2, "2047883871230146.2"),
            (2048441582714043.8, "2048441582714043.8"),
            (2049108538001735.5, "2049108538001735.5"),
            (2053248352698615.8, "2053248352698615.8"),
            (2058526072185951.0, "2058526072185951.0"),
            (2063390179098190.8, "2063390179098190.8"),
            (2064977874786131.5, "2064977874786131.5"),
            (2067034949945914.5, "2067034949945914.5"),
            (2067693827322922.0, "2067693827322922.0"),
            (2068157271627045.5, "2068157271627045.5"),
            (2077760050185958.5, "2077760050185958.5"),
            (2081753512101237.8, "2081753512101237.8"),
            (2084292522334731.0, "2084292522334731.0"),
            (2086314280405931.2, "2086314280405931.2"),
            (2086464113531920.2, "2086464113531920.2"),
            (2086838472471556.8, "2086838472471556.8"),
            (2093919645490872.8, "2093919645490872.8"),
            (2099031502977929.5, "2099031502977929.5"),
            (2102513461709655.5, "2102513461709655.5"),
            (2109646158921262.0, "2109646158921262.0"),
            (2111697649467230.0, "2111697649467230.0"),
            (2112819567545638.0, "2112819567545638.0"),
            (2113767236903059.0, "2113767236903059.0"),
            (2114903207006664.2, "2114903207006664.2"),
            (2117825589472631.8, "2117825589472631.8"),
            (2118034609922220.0, "2118034609922220.0"),
            (2118412269561843.2, "2118412269561843.2"),
            (2128515856496436.2, "2128515856496436.2"),
            (2131973827102984.0, "2131973827102984.0"),
            (2138377658950926.8, "2138377658950926.8"),
            (2138632050788861.5, "2138632050788861.5"),
            (2139282885928002.2, "2139282885928002.2"),
            (2140804323718184.8, "2140804323718184.8"),
            (2143355167221152.8, "2143355167221152.8"),
            (2144265672702912.8, "2144265672702912.8"),
            (2149649490616774.2, "2149649490616774.2"),
            (2152769349018564.0, "2152769349018564.0"),
            (2155131338233191.0, "2155131338233191.0"),
            (2157953451200034.2, "2157953451200034.2"),
            (2159187964081336.8, "2159187964081336.8"),
            (2162681106367131.5, "2162681106367131.5"),
            (2163642902937490.0, "2163642902937490.0"),
            (2165157337613033.2, "2165157337613033.2"),
            (2167326220524674.0, "2167326220524674.0"),
            (2178163967163486.8, "2178163967163486.8"),
            (2184424912877176.0, "2184424912877176.0"),
            (2195089165328405.0, "2195089165328405.0"),
            (2195898537588060.2, "2195898537588060.2"),
            (2196705262606374.0, "2196705262606374.0"),
            (2197328091032661.0, "2197328091032661.0"),
            (2203791736079611.8, "2203791736079611.8"),
            (2208312973442711.5, "2208312973442711.5"),
            (2208356998894367.8, "2208356998894367.8"),
            (2217516009126313.0, "2217516009126313.0"),
            (2219500700991081.0, "2219500700991081.0"),
            (2225773207350988.2, "2225773207350988.2"),
            (2229271711226645.8, "2229271711226645.8"),
            (2234629740543751.5, "2234629740543751.5"),
            (2236410374931967.2, "2236410374931967.2"),
            (2238985175958914.2, "2238985175958914.2"),
            (2240847287729654.2, "2240847287729654.2"),
        ];
        for (value, expected) in cases {
            assert_eq!(py_float(value), expected, "repr of {value:?}");
        }
    }

    #[test]
    fn space_class_matches_the_29_characters_python_calls_space() {
        // [hex(c) for c in range(0x110000) if chr(c).isspace()] in CPython 3.13.14.
        let python: [u32; 29] = [
            0x9, 0xa, 0xb, 0xc, 0xd, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000,
            0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028,
            0x2029, 0x202f, 0x205f, 0x3000,
        ];
        let class = regex::Regex::new(&format!("^[{PY_SPACE}]$"))
            .expect("PY_SPACE is a valid character class");
        let matched: Vec<u32> = (0..=0x10ffff_u32)
            .filter_map(char::from_u32)
            .filter(|character| class.is_match(character.encode_utf8(&mut [0; 4])))
            .map(|character| character as u32)
            .collect();
        assert_eq!(matched, python);
    }

    #[test]
    fn sum_is_the_sum_of_a_list_of_python_floats() {
        // sum([1e100, 1.0, -1e100]) == 1.0 in Python 3.13; sum([1e100, 1, -1e100]) (an int in the middle) is 0.0 there,
        // which py_sum does not model: it takes floats only.
        assert_eq!(py_sum(&[1e100, 1.0, -1e100]), 1.0);
        assert_eq!(py_sum(&[0.1, 0.2]), 0.30000000000000004);
    }
}
