//! Routing: walk a tree of router functions to the function that should answer (port of jeff/routing.py). A router's
//! labels are subset labels over its routes_to: at each level dispatch offers only the targets that implement the
//! requested interface, or lead to one that does.

use std::collections::{BTreeSet, HashMap, VecDeque};
use std::sync::Arc;

use indexmap::IndexMap;

use crate::binding::{Call, Context, Labels};
use crate::catalogue::Catalogue;
use crate::client::{DispatchOptions, Jeff};
use crate::decision::{Decision, RouteStep, Value, low_confidence};
use crate::engine::serves;
use crate::errors::{ErrorType, JeffError, Result, not_installed};
use crate::escalation::{Policy, check_policy};
use crate::json::Json;
use crate::manifest::{FunctionSpec, LabelSource};
use crate::pyformat::py_str;

/// The most routers one call may pass through; going deeper is a ManifestError.
pub const MAX_DEPTH: usize = 8;
/// The router a dispatch starts from when it names none.
pub const DEFAULT_ROUTER: &str = "jeff.core.router";

/// One of a router's targets, resolved: another router (its adapter id) or a function (its id).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Target {
    /// Another router, by its adapter id.
    Router(String),
    /// A function, by its full id.
    Function(String),
}

fn manifest_error(message: String) -> JeffError {
    JeffError::new(ErrorType::ManifestError, message)
}

/// What one of the router `router_id`'s targets is: a router or a function. Raises ManifestError naming the
/// router when the target is not in `catalogue`.
pub fn resolve_target(catalogue: &Catalogue, router_id: &str, target: &str) -> Result<Target> {
    let resolved = (|| {
        let manifest = catalogue.adapter(target)?;
        if manifest.is_router {
            return Ok(Target::Router(manifest.id.clone()));
        }
        Ok(Target::Function(catalogue.function(target)?.id.clone()))
    })();
    resolved.map_err(|error: JeffError| {
        if error.kind() == ErrorType::UnknownFunction {
            manifest_error(format!(
                "Router {router_id} routes to {target}: {}",
                error.message()
            ))
            .with_source(Box::new(error))
        } else {
            error
        }
    })
}

/// Every interface a router's targets implement or lead to, through nested routers. A loop is a ManifestError.
pub fn interfaces_reached(
    catalogue: &Catalogue,
    router_id: &str,
    path: &[String],
) -> Result<BTreeSet<String>> {
    let mut next = path.to_vec();
    next.push(router_id.to_owned());
    if path.iter().any(|seen| seen == router_id) {
        return Err(manifest_error(format!(
            "The routing tree has a loop: {}",
            next.join(" -> ")
        )));
    }
    let mut reached = BTreeSet::new();
    for target in &catalogue.adapter(router_id)?.routes_to {
        reached.extend(target_interfaces(catalogue, router_id, target, &next)?);
    }
    Ok(reached)
}

fn target_interfaces(
    catalogue: &Catalogue,
    router_id: &str,
    target: &str,
    path: &[String],
) -> Result<BTreeSet<String>> {
    match resolve_target(catalogue, router_id, target)? {
        Target::Router(id) => interfaces_reached(catalogue, &id, path),
        Target::Function(id) => Ok([catalogue.function(&id)?.interface.clone()].into()),
    }
}

/// The adapter a resolved target is served by: the router itself, or the function's adapter.
fn adapter_of(catalogue: &Catalogue, target: Target) -> Result<String> {
    match target {
        Target::Router(id) => Ok(id),
        Target::Function(id) => Ok(catalogue.function(&id)?.adapter_id.clone()),
    }
}

/// Every installed router's targets resolve, are installed and form no loop; returns each one's exact keys.
pub fn check_routers(
    catalogue: &Catalogue,
    installed: &BTreeSet<String>,
) -> Result<HashMap<String, Arc<IndexMap<String, String>>>> {
    let mut exact = HashMap::new();
    for manifest in catalogue.manifests() {
        if !manifest.is_router || !installed.contains(&manifest.id) {
            continue;
        }
        for target in &manifest.routes_to {
            let adapter = adapter_of(catalogue, resolve_target(catalogue, &manifest.id, target)?)?;
            if !installed.contains(&adapter) {
                return Err(not_installed(
                    target,
                    &format!("router {} routes to it", manifest.id),
                ));
            }
        }
        interfaces_reached(catalogue, &manifest.id, &[])?;
        exact.insert(
            manifest.id.clone(),
            Arc::new(exact_keys_of(catalogue, &manifest.id)?),
        );
    }
    Ok(exact)
}

/// The router and every adapter its tree reaches, each once, in the order first met.
fn reachable_adapters(catalogue: &Catalogue, router_id: &str) -> Result<Vec<String>> {
    let mut seen: Vec<String> = Vec::new();
    let mut pending = VecDeque::from([router_id.to_owned()]);
    while let Some(adapter_id) = pending.pop_front() {
        if seen.contains(&adapter_id) {
            continue;
        }
        seen.push(adapter_id.clone());
        let manifest = catalogue.adapter(&adapter_id)?;
        for target in &manifest.routes_to {
            pending.push_back(adapter_of(
                catalogue,
                resolve_target(catalogue, &manifest.id, target)?,
            )?);
        }
    }
    Ok(seen)
}

/// Exact key -> code name of every context field of every function reachable from the router whose key differs from
/// its code name (the first one met wins).
pub fn exact_keys_of(catalogue: &Catalogue, router_id: &str) -> Result<IndexMap<String, String>> {
    let mut exact = IndexMap::new();
    for adapter_id in reachable_adapters(catalogue, router_id)? {
        for function in catalogue.adapter(&adapter_id)?.functions.values() {
            for field in &function.context {
                if field.key != field.code_name && !exact.contains_key(&field.key) {
                    exact.insert(field.key.clone(), field.code_name.clone());
                }
            }
        }
    }
    Ok(exact)
}

/// Dispatch passes each function only its fields, by code name; a field given by its exact key would be dropped, so
/// it is refused up front, naming the code name.
pub fn refuse_exact_keys(
    exact: &IndexMap<String, String>,
    router_id: &str,
    context: &Context,
) -> Result<()> {
    let misnamed: Vec<String> = context
        .keys()
        .filter_map(|key| {
            exact
                .get(key)
                .map(|code| format!("{} as {code}", py_str(key)))
        })
        .collect();
    if !misnamed.is_empty() {
        return Err(JeffError::new(
            ErrorType::UnknownContext,
            format!(
                "context is given by code name; pass {} (fields of adapters reached from router {router_id})",
                misnamed.join(", ")
            ),
        ));
    }
    Ok(())
}

pub(crate) async fn start_router(jeff: &Jeff, router: &str) -> Result<String> {
    let manifest = jeff.catalogue().adapter(router).map_err(|error| {
        if error.kind() == ErrorType::UnknownFunction {
            JeffError::new(
                ErrorType::UnknownFunction,
                format!(
                    "There is no router {router} in the catalogue, so this call cannot be routed; name the function \
                     to call with function="
                ),
            )
            .with_source(Box::new(error))
        } else {
            error
        }
    })?;
    if !serves(jeff.engine().as_ref(), &manifest.id).await? {
        return Err(not_installed(
            &manifest.id,
            "there is no router installed to route this call; install it, or name the function to call with \
             function=",
        ));
    }
    Ok(manifest.id.clone())
}

/// The caller's context fields that `function` has (its own, or its adapter's), by code name, in its field order.
fn fields_of(function: &FunctionSpec, given: &Context) -> Context {
    function
        .context
        .iter()
        .filter_map(|field| {
            given
                .get(&field.code_name)
                .map(|value| (field.code_name.clone(), value.clone()))
        })
        .collect()
}

/// The call dispatch makes to the chosen function: the caller's context fields it has, and the caller's labels
/// unless its labels are fixed with no caller subset.
pub fn leaf_call(
    leaf: &FunctionSpec,
    input: &Json,
    context: Option<&Context>,
    labels: Option<&Labels>,
) -> Call {
    let leaf_context = match context {
        Some(given) => fields_of(leaf, given),
        None => Context::new(),
    };
    let takes_labels = leaf.labels.source != LabelSource::Fixed || leaf.labels.subset == Some(true);
    Call {
        function: leaf.id.clone(),
        input: input.clone(),
        context: Some(leaf_context),
        labels: if takes_labels { labels.cloned() } else { None },
    }
}

/// Python's `str()` of a decision value.
fn value_text(value: &Value) -> String {
    match value {
        Value::Str(text) => text.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Int(level) => level.to_string(),
    }
}

pub(crate) async fn dispatch(
    jeff: &Jeff,
    input: Json,
    options: DispatchOptions,
) -> Result<Decision> {
    let policy = &options.on_low_confidence;
    let interface = options.interface.as_str();
    let router = options.router.as_deref().unwrap_or(DEFAULT_ROUTER);
    check_policy(jeff, interface, policy).await?;
    let given = match &options.context {
        Some(context) => context.clone(),
        None => Context::new(),
    };
    let mut route: Vec<RouteStep> = Vec::new();
    let mut current = start_router(jeff, router).await?;
    if !given.is_empty() {
        refuse_exact_keys(&*jeff.exact_keys_for(&current)?, &current, &given)?;
    }
    for _ in 0..MAX_DEPTH {
        let manifest = jeff.catalogue().adapter(&current)?;
        if !serves(jeff.engine().as_ref(), &manifest.id).await? {
            let chooser = route
                .last()
                .expect("the first router was checked as served, so another router chose this one");
            return Err(not_installed(
                &manifest.id,
                &format!("router {} chose it", chooser.router),
            ));
        }
        if !manifest.is_router {
            return Err(manifest_error(format!("{} is not a router", manifest.id)));
        }
        let mut offered = Vec::new();
        for target in &manifest.routes_to {
            let reached = target_interfaces(
                jeff.catalogue(),
                &manifest.id,
                target,
                std::slice::from_ref(&manifest.id),
            )?;
            if reached.contains(interface) {
                offered.push(target.clone());
            }
        }
        if offered.is_empty() {
            return Err(manifest_error(format!(
                "Router {} has no target that implements or leads to {interface}",
                manifest.id
            )));
        }
        let function = Arc::clone(
            manifest
                .functions
                .first()
                .ok_or_else(|| manifest_error(format!("Router {} has no functions", manifest.id)))?
                .1,
        );
        let router_context = fields_of(&function, &given);
        let call = Call {
            function: function.id.clone(),
            input: input.clone(),
            context: if router_context.is_empty() {
                None
            } else {
                Some(router_context)
            },
            labels: Some(Labels::Keys(offered)),
        };
        let decision = jeff.decide_one(&call).await?;
        let confidence = decision.confidence.ok_or_else(|| {
            JeffError::new(
                ErrorType::EngineError,
                format!(
                    "Router {} returned a decision without a confidence",
                    function.id
                ),
            )
        })?;
        route.push(RouteStep {
            router: function.id.clone(),
            chose: value_text(&decision.value),
            confidence,
        });
        if decision.abstained {
            let with_route = Decision { route, ..decision };
            return match policy {
                Policy::Return => Ok(with_route),
                Policy::Error | Policy::Escalate(_) => Err(low_confidence(&with_route)?),
            };
        }
        match resolve_target(jeff.catalogue(), &manifest.id, &value_text(&decision.value))? {
            Target::Router(id) => current = id,
            Target::Function(id) => {
                let leaf = jeff.catalogue().function(&id)?;
                let call = leaf_call(&leaf, &input, Some(&given), options.labels.as_ref());
                let answered = jeff.decide_one(&call).await?;
                return jeff
                    .apply_policy(&call, Decision { route, ..answered }, policy)
                    .await;
            }
        }
    }
    Err(manifest_error(format!(
        "Routing from {router} went deeper than {MAX_DEPTH} levels"
    )))
}
