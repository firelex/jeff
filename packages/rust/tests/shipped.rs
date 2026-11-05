//! The shipped copy is the repository's contract and catalogue, byte for byte, and build.rs embedded all of it.

use std::fs;
use std::path::{Path, PathBuf};

use jeff_sdk::shipped::{self, SHIPPED_ITEMS};

fn files(path: &Path) -> Vec<PathBuf> {
    if path.is_file() {
        return vec![path.to_path_buf()];
    }
    let mut entries: Vec<PathBuf> = fs::read_dir(path)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    entries.sort();
    entries.iter().flat_map(|entry| files(entry)).collect()
}

fn relative(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap()
        .to_string_lossy()
        .replace('\\', "/")
}

#[test]
fn the_shipped_copy_matches_the_repository() {
    let root = shipped::repository_root();
    let copy = Path::new(env!("CARGO_MANIFEST_DIR")).join("shipped");
    for item in SHIPPED_ITEMS {
        let source = root.join(item);
        let target = copy.join(item);
        let source_files: Vec<String> = files(&source)
            .iter()
            .map(|path| relative(&source, path))
            .collect();
        let target_files: Vec<String> = files(&target)
            .iter()
            .map(|path| relative(&target, path))
            .collect();
        assert_eq!(
            target_files, source_files,
            "{item}: run packages/rust/scripts/sync-shipped.sh"
        );
        for name in &source_files {
            // `source`/`target` are themselves a file for a single-file item (e.g. `contract/errors.yaml`), where
            // `name` is "" (relative(root, root)); `source.join("")` would append a trailing slash and make
            // fs::read fail with "Not a directory", so join only when there is a name to join.
            let source_path = if name.is_empty() {
                source.clone()
            } else {
                source.join(name)
            };
            let target_path = if name.is_empty() {
                target.clone()
            } else {
                target.join(name)
            };
            let a = fs::read(&source_path).unwrap();
            let b = fs::read(&target_path).unwrap();
            assert!(
                a == b,
                "{item}/{name} differs: run packages/rust/scripts/sync-shipped.sh"
            );
        }
    }
}

#[test]
fn every_shipped_file_is_embedded() {
    let copy = Path::new(env!("CARGO_MANIFEST_DIR")).join("shipped");
    let on_disk: Vec<String> = files(&copy)
        .iter()
        .map(|path| relative(&copy, path))
        .collect();
    assert_eq!(shipped::paths(), on_disk);
    for name in &on_disk {
        assert_eq!(
            shipped::file(name).unwrap(),
            fs::read_to_string(copy.join(name)).unwrap(),
            "{name}"
        );
    }
    assert!(shipped::file("catalogue/no-such/manifest.yaml").is_none());
}
