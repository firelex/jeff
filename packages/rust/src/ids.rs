//! References to adapters and functions: `jeff.<domain>.<name>[.<function>][@<version>]`, or `<alias>[.<function>]`.

use crate::errors::{ErrorType, JeffError, Result};
use crate::pyformat::py_str;

/// A parsed reference to an adapter or one of its functions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FunctionRef {
    /// The adapter id (`jeff.<domain>.<name>`) or alias.
    pub adapter: String,
    /// The function's key, when the reference names one.
    pub key: Option<String>,
    /// The version after `@`, when given; it matches any version it is a prefix of, part by part.
    pub version: Option<String>,
}

fn malformed(reference: &str, how: &str) -> JeffError {
    JeffError::new(
        ErrorType::InvalidReference,
        format!("Malformed reference {}: {how}", py_str(reference)),
    )
}

/// The parts of `reference`. Raises InvalidReference for an empty part, too few or too many parts, or an
/// alias with dots.
pub fn parse_ref(reference: &str) -> Result<FunctionRef> {
    let (text, version) = reference.split_once('@').unwrap_or((reference, ""));
    let parts: Vec<&str> = text.split('.').collect();
    if text.is_empty() || parts.iter().any(|part| part.is_empty()) {
        return Err(malformed(
            reference,
            "use jeff.<domain>.<name>[.<function>] or <alias>[.<function>], each part non-empty",
        ));
    }
    let (adapter, key) = if parts[0] == "jeff" {
        if parts.len() < 3 {
            return Err(malformed(
                reference,
                "use jeff.<domain>.<name>[.<function>]",
            ));
        }
        if parts.len() > 4 {
            return Err(malformed(reference, "too many parts"));
        }
        if parts.len() == 4 {
            (parts[..3].join("."), Some(parts[3].to_owned()))
        } else {
            (text.to_owned(), None)
        }
    } else {
        if parts.len() > 2 {
            return Err(malformed(
                reference,
                "use <alias>[.<function>] (aliases have no dots)",
            ));
        }
        (
            parts[0].to_owned(),
            parts.get(1).map(|key| (*key).to_owned()),
        )
    };
    Ok(FunctionRef {
        adapter,
        key,
        version: if version.is_empty() {
            None
        } else {
            Some(version.to_owned())
        },
    })
}

/// Whether `actual` is `wanted` or starts with it, part by part (`1.3` matches `1.3.0`, not `1.30.0`).
pub fn version_matches(wanted: &str, actual: &str) -> bool {
    let want: Vec<&str> = wanted.split('.').collect();
    let have: Vec<&str> = actual.split('.').take(want.len()).collect();
    have == want
}
