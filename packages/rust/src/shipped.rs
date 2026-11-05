//! What the crate carries from the repository: contract/schemas, contract/interfaces.yaml, contract/errors.yaml and
//! catalogue/, copied into packages/rust/shipped/ by scripts/sync-shipped.sh and embedded by build.rs.

#[cfg(any(test, feature = "testing"))]
use std::path::{Path, PathBuf};

include!(concat!(env!("OUT_DIR"), "/shipped_files.rs"));

/// The repository paths the shipped copy holds.
pub const SHIPPED_ITEMS: [&str; 4] = [
    "contract/schemas",
    "contract/interfaces.yaml",
    "contract/errors.yaml",
    "catalogue",
];

/// The text of a shipped file, by its path inside the copy (`catalogue/jeff.safety.spam/manifest.yaml`); None when
/// the copy has no such file.
pub fn file(path: &str) -> Option<&'static str> {
    FILES
        .iter()
        .find(|(name, _)| *name == path)
        .map(|(_, text)| *text)
}

/// Every shipped path, sorted.
pub fn paths() -> Vec<&'static str> {
    FILES.iter().map(|(name, _)| *name).collect()
}

/// The repository root (two levels above this crate, as a path on the machine that built it), for the tests and the
/// sync check; only built for this crate's tests or with the `testing` feature.
#[cfg(any(test, feature = "testing"))]
pub fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}
