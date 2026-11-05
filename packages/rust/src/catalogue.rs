//! The catalogue: every known adapter manifest, looked up by id, alias, version or interface (port of
//! jeff/catalogue.py).

use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::sync::{Arc, OnceLock};

use indexmap::IndexMap;

use crate::errors::{ErrorType, JeffError, Result};
use crate::ids::{parse_ref, version_matches};
use crate::json::{Json, JsonMap};
use crate::manifest::{FunctionSpec, Manifest, load_manifest, manifest_from_json};
use crate::pyformat::{py_repr, py_str};
use crate::shipped;
use crate::yaml::{read_manifest_yaml, read_yaml, read_yaml_file};

/// The adapter manifests calls are bound with, by id and alias, and the base models they were trained on
/// (bases.yaml). [`Catalogue::shipped`] is the one this crate carries.
#[derive(Clone, Debug)]
pub struct Catalogue {
    by_id: IndexMap<String, Arc<Manifest>>,
    by_alias: HashMap<String, String>,
    bases: JsonMap,
}

fn unknown(message: String) -> JeffError {
    JeffError::new(ErrorType::UnknownFunction, message)
}

fn manifest_error(message: String) -> JeffError {
    JeffError::new(ErrorType::ManifestError, message)
}

static SHIPPED: OnceLock<std::result::Result<Arc<Catalogue>, JeffError>> = OnceLock::new();

impl Catalogue {
    /// `bases` is bases.yaml as read: base id -> version -> where the weights are. Anything else is refused here
    /// (Python only fails when the malformed entry is looked up).
    pub fn new(manifests: Vec<Arc<Manifest>>, bases: Json) -> Result<Catalogue> {
        let Json::Object(bases) = bases else {
            return Err(manifest_error(format!(
                "bases.yaml is not a mapping of base ids to their versions (got {})",
                py_repr(&bases)
            )));
        };
        for (base_id, versions) in &bases {
            if !versions.is_object() {
                return Err(manifest_error(format!(
                    "bases.yaml: base {base_id} is not a mapping of versions (got {})",
                    py_repr(versions)
                )));
            }
        }
        let mut by_id = IndexMap::new();
        let mut by_alias = HashMap::new();
        for manifest in manifests {
            for name in std::iter::once(&manifest.id).chain(manifest.aliases.iter()) {
                if by_id.contains_key(name) || by_alias.contains_key(name) {
                    return Err(manifest_error(format!(
                        "{}: the name {name} is already used by another adapter",
                        manifest.path
                    )));
                }
            }
            for alias in &manifest.aliases {
                by_alias.insert(alias.clone(), manifest.id.clone());
            }
            by_id.insert(manifest.id.clone(), manifest);
        }
        Ok(Catalogue {
            by_id,
            by_alias,
            bases,
        })
    }

    /// A catalogue folder: bases.yaml and one folder per adapter holding its manifest.yaml, in sorted order.
    pub fn from_dir(path: &Path) -> Result<Catalogue> {
        let bases_file = path.join("bases.yaml");
        if !bases_file.exists() {
            return Err(manifest_error(format!(
                "{} is missing",
                bases_file.display()
            )));
        }
        let unreadable = |error: std::io::Error| {
            manifest_error(format!("{} cannot be read: {error}", path.display()))
        };
        let mut names = Vec::new();
        for entry in std::fs::read_dir(path).map_err(unreadable)? {
            names.push(entry.map_err(unreadable)?.file_name());
        }
        // By name, as Python sorts the paths (one folder level, so by the folder's name).
        names.sort();
        let mut manifests = Vec::new();
        for name in names {
            let manifest = path.join(&name).join("manifest.yaml");
            if manifest.exists() {
                manifests.push(Arc::new(load_manifest(&manifest)?));
            }
        }
        Catalogue::new(manifests, read_yaml_file(&bases_file)?)
    }

    /// The catalogue shipped with the crate (read once).
    pub fn shipped() -> Result<Arc<Catalogue>> {
        SHIPPED
            .get_or_init(|| {
                // Sorted by adapter folder, as from_dir reads a folder: the full paths sort differently when one
                // folder name is a prefix of another ('a.b/' sorts before 'a/').
                let mut folders: Vec<&str> = shipped::paths()
                    .into_iter()
                    .filter_map(|path| {
                        let parts: Vec<&str> = path.split('/').collect();
                        (parts.len() == 3 && parts[0] == "catalogue" && parts[2] == "manifest.yaml")
                            .then_some(parts[1])
                    })
                    .collect();
                folders.sort();
                let mut manifests = Vec::new();
                for folder in folders {
                    let path = format!("catalogue/{folder}/manifest.yaml");
                    let text = shipped::file(&path).ok_or_else(|| {
                        manifest_error(format!("{path} is missing from the shipped copy"))
                    })?;
                    let name = format!("shipped/{path}");
                    manifests.push(Arc::new(manifest_from_json(
                        &read_manifest_yaml(text, &name)?,
                        &name,
                    )?));
                }
                let bases = shipped::file("catalogue/bases.yaml").ok_or_else(|| {
                    manifest_error(
                        "catalogue/bases.yaml is missing from the shipped copy".to_owned(),
                    )
                })?;
                Ok(Arc::new(Catalogue::new(
                    manifests,
                    read_yaml(bases, "shipped/catalogue/bases.yaml")?,
                )?))
            })
            .clone()
    }

    /// The same bases, holding `manifests` too, each in place of this catalogue's manifest of the same id (an adapter
    /// folder an engine serves instead of the catalogue's download). A name two adapters would then share is refused.
    pub fn replacing(&self, manifests: Vec<Arc<Manifest>>) -> Result<Catalogue> {
        let mut merged = self.by_id.clone();
        for manifest in manifests {
            merged.insert(manifest.id.clone(), manifest);
        }
        Catalogue::new(
            merged.into_values().collect(),
            Json::Object(self.bases.clone()),
        )
    }

    /// Every adapter id and alias in the catalogue.
    pub fn names(&self) -> BTreeSet<String> {
        self.by_id
            .keys()
            .chain(self.by_alias.keys())
            .cloned()
            .collect()
    }

    /// Every manifest, in catalogue order.
    pub fn manifests(&self) -> Vec<Arc<Manifest>> {
        self.by_id.values().cloned().collect()
    }

    /// The manifest an adapter reference (id or alias, with an optional `@version`) names. Raises
    /// InvalidReference for a malformed reference and UnknownFunction for an adapter or version the catalogue lacks.
    pub fn adapter(&self, reference: &str) -> Result<Arc<Manifest>> {
        let parsed = parse_ref(reference)?;
        let name = self
            .by_alias
            .get(&parsed.adapter)
            .unwrap_or(&parsed.adapter);
        let manifest = self.by_id.get(name).ok_or_else(|| {
            unknown(format!(
                "No adapter {} in the catalogue",
                py_str(&parsed.adapter)
            ))
        })?;
        if let Some(version) = &parsed.version
            && !version_matches(version, &manifest.version)
        {
            return Err(unknown(format!(
                "{}@{version} is not in the catalogue; it has {}",
                manifest.id, manifest.version
            )));
        }
        Ok(Arc::clone(manifest))
    }

    /// The function a reference names; an adapter with one function may be named without its key. Raises
    /// InvalidReference for a malformed reference and UnknownFunction for an unknown adapter, version or function, or
    /// an adapter with several functions named without a key.
    pub fn function(&self, reference: &str) -> Result<Arc<FunctionSpec>> {
        let parsed = parse_ref(reference)?;
        let manifest = self.adapter(reference)?;
        let keys: Vec<&str> = manifest.functions.keys().map(String::as_str).collect();
        match &parsed.key {
            None => match keys.as_slice() {
                [_] => Ok(Arc::clone(&manifest.functions[0])),
                [] => Err(unknown(format!("{} has no functions", manifest.id))),
                [first, ..] => Err(unknown(format!(
                    "{} has {} functions ({}); name one, e.g. {}.{first}",
                    manifest.id,
                    keys.len(),
                    keys.join(", "),
                    manifest.id
                ))),
            },
            Some(key) => manifest.functions.get(key).cloned().ok_or_else(|| {
                unknown(format!(
                    "{} has no function {}; its functions are {}",
                    manifest.id,
                    py_str(key),
                    keys.join(", ")
                ))
            }),
        }
    }

    /// The manifest of the adapter `function` belongs to. Raises UnknownFunction when that adapter is not in
    /// this catalogue.
    pub fn manifest_of(&self, function: &FunctionSpec) -> Result<Arc<Manifest>> {
        self.by_id
            .get(&function.adapter_id)
            .cloned()
            .ok_or_else(|| {
                unknown(format!(
                    "No adapter {} in the catalogue",
                    py_str(&function.adapter_id)
                ))
            })
    }

    /// Every function implementing `interface`, routers left out, in catalogue order.
    pub fn implementing(&self, interface: &str) -> Vec<Arc<FunctionSpec>> {
        self.by_id
            .values()
            .filter(|manifest| !manifest.is_router)
            .flat_map(|manifest| {
                manifest
                    .functions
                    .values()
                    .filter(|function| function.interface == interface)
                    .cloned()
            })
            .collect()
    }

    /// Every (base id, version) bases.yaml lists.
    pub fn base_versions(&self) -> Vec<(String, String)> {
        self.bases
            .iter()
            .flat_map(|(id, versions)| {
                versions.as_object().into_iter().flat_map(move |map| {
                    map.keys().map(move |version| (id.clone(), version.clone()))
                })
            })
            .collect()
    }

    /// The bases.yaml entry for a base model's id and version. Raises ManifestError when bases.yaml has no such
    /// entry or it is not a mapping.
    pub fn base(&self, base_id: &str, version: &str) -> Result<JsonMap> {
        let found = self
            .bases
            .get(base_id)
            .and_then(|versions| versions.get(version));
        match found {
            Some(Json::Object(entry)) => Ok(entry.clone()),
            None => Err(manifest_error(format!(
                "Base {base_id} {version} is not in bases.yaml (known: {})",
                py_repr(&Json::Object(self.bases.clone()))
            ))),
            Some(other) => Err(manifest_error(format!(
                "bases.yaml: base {base_id} {version} is not a mapping (got {})",
                py_repr(other)
            ))),
        }
    }
}
