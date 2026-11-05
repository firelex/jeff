//! serde_json::Map::remove (with preserve_order) is swap_remove: it moves the map's last key into the removed key's
//! place, which would reorder a row, a criteria map or a graph. No source line calls `.remove(` unless it says why
//! it is safe with the marker comment `// remove: not an ordered map`.

use std::fs;
use std::path::Path;

fn sources(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            sources(&path, out);
        } else if path.extension().is_some_and(|ext| ext == "rs") {
            out.push(path);
        }
    }
}

#[test]
fn no_unmarked_remove_in_the_sources() {
    let mut files = Vec::new();
    sources(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut files,
    );
    let mut found = Vec::new();
    for file in files {
        for (number, line) in fs::read_to_string(&file).unwrap().lines().enumerate() {
            if line.contains(".remove(") && !line.contains("// remove: not an ordered map") {
                found.push(format!(
                    "{}:{}: {}",
                    file.display(),
                    number + 1,
                    line.trim()
                ));
            }
        }
    }
    assert!(
        found.is_empty(),
        "use shift_remove on ordered maps, or mark the line:\n{}",
        found.join("\n")
    );
}
