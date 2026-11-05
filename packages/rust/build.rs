//! Embeds every file of shipped/ (the copy scripts/sync-shipped.sh makes) in the crate, so a program built with
//! jeff-sdk reads the contract and the catalogue without any file at run time.

use std::fs;
use std::path::{Path, PathBuf};

fn collect(root: &Path, dir: &Path, out: &mut Vec<String>) {
    let mut entries: Vec<PathBuf> = fs::read_dir(dir)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", dir.display()))
        .map(|entry| entry.expect("a directory entry of shipped/").path())
        .collect();
    entries.sort();
    for path in entries {
        if path.is_dir() {
            collect(root, &path, out);
        } else {
            let relative = path.strip_prefix(root).expect("a path under shipped/");
            out.push(relative.to_string_lossy().replace('\\', "/"));
        }
    }
}

fn main() {
    let crate_dir =
        PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("cargo sets CARGO_MANIFEST_DIR"));
    let shipped = crate_dir.join("shipped");
    println!("cargo:rerun-if-changed=shipped");
    if !shipped.is_dir() {
        panic!(
            "{} is missing; run packages/rust/scripts/sync-shipped.sh",
            shipped.display()
        );
    }
    let mut files = Vec::new();
    collect(&shipped, &shipped, &mut files);
    let mut code = String::from("pub(crate) static FILES: &[(&str, &str)] = &[\n");
    for name in &files {
        let path = shipped.join(name);
        fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("{} is not UTF-8 text: {error}", path.display()));
        code.push_str(&format!(
            "    ({name:?}, include_str!({:?})),\n",
            path.display().to_string()
        ));
    }
    code.push_str("];\n");
    let out = PathBuf::from(std::env::var("OUT_DIR").expect("cargo sets OUT_DIR"))
        .join("shipped_files.rs");
    fs::write(&out, code).unwrap_or_else(|error| panic!("cannot write {}: {error}", out.display()));
}
