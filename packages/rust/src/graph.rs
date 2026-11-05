//! Decision graphs: functions wired together with declarative conditions, run in batched stages (port of
//! jeff/graph.py). Node order is the document's, whatever the node names look like.
//!
//! Nothing here walks a graph by recursion deeper than [`MAX_GRAPH_DEPTH`]: a deeper document or condition is a
//! GraphError naming the limit, and the dependency order is found without recursion, so no graph overflows the stack.

use std::collections::{BTreeSet, HashMap};
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use indexmap::IndexMap;

use crate::binding::{Binding, Call, Labels};
use crate::client::Jeff;
use crate::contract::{QuestionType, SchemaName, validate};
use crate::decision::Decision;
use crate::errors::{BoxError, ErrorType, JeffError, Result, not_installed};
use crate::escalation::{BoxFuture, Policy, check_policy};
use crate::json::{Json, JsonMap};
use crate::manifest::{FunctionSpec, LabelSource};
use crate::pyformat::{py_equals, py_repr, py_sorted, py_str};
use crate::yaml::read_yaml_file;

/// How many objects and lists a graph document, or one node's condition, may hold inside one another. Python reads a
/// graph document up to 162 nested `not` conditions (166 levels) or 122 nested `all` ones (248 levels) and then stops
/// with a RecursionError; this SDK reads every document Python reads and refuses a deeper one by name.
pub const MAX_GRAPH_DEPTH: usize = 256;

/// One node of a graph: a function call or a code function, when it runs and how it is bound.
#[derive(Clone, Debug, PartialEq)]
pub struct GraphNode {
    /// The node's name in the document.
    pub name: String,
    /// The reference of the function the node calls; none for a code node.
    pub call: Option<String>,
    /// The name of the code function the node runs; none for a call node.
    pub code: Option<String>,
    /// Nodes this one runs after, besides those its condition reads.
    pub after: Vec<String>,
    /// Context code name -> the param it takes its value from, in the document's order.
    pub context: IndexMap<String, String>,
    /// The param the called function's labels come from, when it takes labels.
    pub labels: Option<String>,
    /// A condition as graph.schema.json writes it: {all}, {any}, {not}, {node, is}, {node, in}, {node, abstained} or
    /// {node, confidence_below}.
    pub when: Option<Json>,
    /// `return` or `error`: what happens when the node's decision abstains.
    pub on_low_confidence: String,
}

/// A decision graph (contract/schemas/graph.schema.json), checked and with its dependency order found.
#[derive(Clone, Debug, PartialEq)]
pub struct Graph {
    /// The graph's id (its `graph` field).
    pub id: String,
    /// The graph's version.
    pub version: String,
    /// The name of the changing input every node reads.
    pub input: String,
    /// The names of the fixed values the caller supplies when running the graph.
    pub params: Vec<String>,
    /// The nodes by name, in the document's order.
    pub nodes: IndexMap<String, GraphNode>,
    /// The nodes whose results are the graph's outputs.
    pub outputs: Vec<String>,
    /// The node names in dependency order (every node after the ones it depends on).
    pub order: Vec<String>,
}

// The plan's interface holds a Decision itself, so a result can be matched and moved without a Box; a graph holds a
// few results, so the larger size costs nothing that matters.
/// One node's result.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, PartialEq)]
pub enum NodeResult {
    /// The decision of a call node.
    Decision(Decision),
    /// A node that did not run, and why.
    Skipped(String),
    /// What a code node returned.
    Code(Json),
}

impl NodeResult {
    /// `{"decision"}`, `{"skipped": true, "reason"}` or `{"code"}`, as graph_result.schema.json writes a node result.
    pub fn to_json(&self) -> Result<Json> {
        let mut map = JsonMap::new();
        match self {
            NodeResult::Decision(decision) => {
                map.insert("decision".into(), decision.to_json()?);
            }
            NodeResult::Skipped(reason) => {
                map.insert("skipped".into(), Json::Bool(true));
                map.insert("reason".into(), Json::from(reason.as_str()));
            }
            NodeResult::Code(value) => {
                map.insert("code".into(), value.clone());
            }
        }
        Ok(Json::Object(map))
    }

    /// The node result a `{"decision"}`, `{"skipped", "reason"}` or `{"code"}` object holds. Raises GraphError
    /// for any other object, and ContractError for a malformed decision.
    pub fn from_json(data: &Json) -> Result<NodeResult> {
        if let Some(decision) = data.get("decision") {
            return Ok(NodeResult::Decision(Decision::from_json(decision)?));
        }
        if data.get("skipped").is_some() {
            return match data.get("reason") {
                Some(Json::String(reason)) => Ok(NodeResult::Skipped(reason.clone())),
                other => Err(graph_error(format!(
                    "A skipped graph result node needs its 'reason' string (got {})",
                    py_repr(other.unwrap_or(&Json::Null))
                ))),
            };
        }
        if let Some(value) = data.get("code") {
            return Ok(NodeResult::Code(value.clone()));
        }
        let keys: Vec<String> = match data.as_object() {
            Some(map) => py_sorted(map.keys().cloned()),
            None => Vec::new(),
        };
        Err(graph_error(format!(
            "A graph result node must hold 'decision', 'skipped' or 'code'; got keys {}",
            py_repr(&Json::from(keys))
        )))
    }
}

/// The result of running a graph (contract/schemas/graph_result.schema.json).
#[derive(Clone, Debug, PartialEq)]
pub struct GraphResult {
    /// Every node's result, in the order the nodes got them.
    pub nodes: IndexMap<String, NodeResult>,
    /// The results of the graph's output nodes, in the order of `outputs`.
    pub outputs: IndexMap<String, NodeResult>,
    /// How many batched stages the run took.
    pub stages: u64,
}

fn results_json(results: &IndexMap<String, NodeResult>) -> Result<Json> {
    let mut map = JsonMap::new();
    for (name, result) in results {
        map.insert(name.clone(), result.to_json()?);
    }
    Ok(Json::Object(map))
}

fn results_from(data: Option<&Json>, key: &str) -> Result<IndexMap<String, NodeResult>> {
    let map = data
        .and_then(Json::as_object)
        .ok_or_else(|| graph_error(format!("A graph result needs its '{key}' object")))?;
    map.iter()
        .map(|(name, item)| Ok((name.clone(), NodeResult::from_json(item)?)))
        .collect()
}

impl GraphResult {
    /// `{"nodes", "outputs", "stages"}`, as graph_result.schema.json writes it. Raises ContractError for a
    /// decision holding a NaN or infinite number.
    pub fn to_json(&self) -> Result<Json> {
        let mut map = JsonMap::new();
        map.insert("nodes".into(), results_json(&self.nodes)?);
        map.insert("outputs".into(), results_json(&self.outputs)?);
        map.insert("stages".into(), Json::from(self.stages));
        Ok(Json::Object(map))
    }

    /// The graph result a `{"nodes", "outputs", "stages"}` object holds. Raises GraphError for a missing or
    /// malformed part, and ContractError for a malformed decision.
    pub fn from_json(data: &Json) -> Result<GraphResult> {
        Ok(GraphResult {
            nodes: results_from(data.get("nodes"), "nodes")?,
            outputs: results_from(data.get("outputs"), "outputs")?,
            stages: data
                .get("stages")
                .and_then(Json::as_u64)
                .ok_or_else(|| graph_error("A graph result needs its 'stages' count".into()))?,
        })
    }
}

/// What a code node's function is given: the graph's input, its params, and every node result so far (in the order
/// the nodes got them).
#[derive(Clone, Debug)]
pub struct CodeCall {
    /// The graph's input.
    pub input: Json,
    /// The params the graph was run with.
    pub params: JsonMap,
    /// Every node result so far, in the order the nodes got them.
    pub nodes: IndexMap<String, NodeResult>,
}

/// A code node's async function: given a [`CodeCall`], it returns the node's JSON result or an error.
pub type CodeFunction =
    Arc<dyn Fn(CodeCall) -> BoxFuture<'static, std::result::Result<Json, BoxError>> + Send + Sync>;

/// A code node's function from an async closure.
pub fn code_function<F, Fut>(function: F) -> CodeFunction
where
    F: Fn(CodeCall) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = std::result::Result<Json, BoxError>> + Send + 'static,
{
    Arc::new(
        move |call| -> BoxFuture<'static, std::result::Result<Json, BoxError>> {
            Box::pin(function(call))
        },
    )
}

/// A graph to run: a Graph, a graph file (YAML), or a graph document.
#[derive(Clone, Debug)]
pub enum GraphSource {
    /// A graph already read.
    Graph(Graph),
    /// A graph file (YAML), read when the graph runs.
    Path(PathBuf),
    /// A graph document, checked when the graph runs.
    Document(Json),
}

impl From<Graph> for GraphSource {
    fn from(graph: Graph) -> GraphSource {
        GraphSource::Graph(graph)
    }
}

impl From<PathBuf> for GraphSource {
    fn from(path: PathBuf) -> GraphSource {
        GraphSource::Path(path)
    }
}

impl From<&Path> for GraphSource {
    fn from(path: &Path) -> GraphSource {
        GraphSource::Path(path.to_path_buf())
    }
}

impl From<Json> for GraphSource {
    fn from(document: Json) -> GraphSource {
        GraphSource::Document(document)
    }
}

impl GraphSource {
    pub(crate) fn into_graph(self) -> Result<Graph> {
        match self {
            GraphSource::Graph(graph) => Ok(graph),
            GraphSource::Path(path) => load_graph(&path),
            GraphSource::Document(document) => {
                graph_from_json(&document, "the graph given to run_graph")
            }
        }
    }
}

fn graph_error(message: String) -> JeffError {
    JeffError::new(ErrorType::GraphError, message)
}

/// True when `value` holds more than MAX_GRAPH_DEPTH objects and lists inside one another (`value` itself counts as
/// one when it is an object or a list); measured without recursion, stopping at the first level too deep.
fn too_deep(value: &Json) -> bool {
    let mut pending: Vec<(&Json, usize)> = vec![(value, 1)];
    while let Some((current, depth)) = pending.pop() {
        let children: Box<dyn Iterator<Item = &Json>> = match current {
            Json::Array(items) => Box::new(items.iter()),
            Json::Object(map) => Box::new(map.values()),
            _ => continue,
        };
        if depth > MAX_GRAPH_DEPTH {
            return true;
        }
        pending.extend(children.map(|child| (child, depth + 1)));
    }
    false
}

fn condition_too_deep(graph_id: &str, graph: &Graph) -> Result<()> {
    for (name, node) in &graph.nodes {
        if node.when.as_ref().is_some_and(too_deep) {
            return Err(graph_error(format!(
                "{graph_id}: the condition of node {name} is nested more than {MAX_GRAPH_DEPTH} levels deep \
                 (objects and lists inside one another)"
            )));
        }
    }
    Ok(())
}

/// The nodes a condition reads, found without recursion. Each part is read as `holds` reads it (`all` before `any`
/// before `not` before `node`); a part `holds` cannot read adds nothing here and is refused by name when it runs.
fn condition_nodes(condition: &Json, into: &mut BTreeSet<String>) {
    let mut pending = vec![condition];
    while let Some(current) = pending.pop() {
        if let Some(items) = current.get("all").or_else(|| current.get("any")) {
            if let Some(items) = items.as_array() {
                pending.extend(items.iter());
            }
        } else if let Some(inner) = current.get("not") {
            pending.push(inner);
        } else if let Some(Json::String(node)) = current.get("node") {
            into.insert(node.clone());
        }
    }
}

/// The nodes a node waits for: those its condition reads and those it runs after.
pub fn dependencies(node: &GraphNode) -> BTreeSet<String> {
    let mut found: BTreeSet<String> = node.after.iter().cloned().collect();
    if let Some(when) = &node.when {
        condition_nodes(when, &mut found);
    }
    found
}

fn strings(value: Option<&Json>) -> Vec<String> {
    value
        .and_then(Json::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Json::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn param_of(value: &Json) -> Option<String> {
    value.get("param").and_then(Json::as_str).map(str::to_owned)
}

/// The nodes in dependency order: a depth-first walk from each node in document order, visiting its dependencies in
/// Python's sorted order, as jeff/graph.py's `visit` does, but with its own stack instead of recursion. A cycle is a
/// GraphError naming the path that closes it.
fn dependency_order(nodes: &IndexMap<String, GraphNode>, where_: &str) -> Result<Vec<String>> {
    #[derive(PartialEq)]
    enum State {
        Active,
        Done,
    }
    let mut state: HashMap<&str, State> = HashMap::new();
    let mut order = Vec::new();
    for root in nodes.keys() {
        if state.contains_key(root.as_str()) {
            continue;
        }
        state.insert(root, State::Active);
        // (node, its dependencies sorted, how many of them were visited)
        let mut stack: Vec<(&str, Vec<String>, usize)> =
            vec![(root, py_sorted(dependencies(&nodes[root])), 0)];
        while let Some((name, dependencies_of, visited)) = stack.last_mut() {
            let Some(next) = dependencies_of.get(*visited).cloned() else {
                let name = *name;
                stack.pop();
                state.insert(name, State::Done);
                order.push(name.to_owned());
                continue;
            };
            *visited += 1;
            let (key, node) = nodes
                .get_key_value(&next)
                .expect("every dependency was checked to be a node");
            match state.get(key.as_str()) {
                Some(State::Done) => {}
                Some(State::Active) => {
                    let mut path: Vec<&str> = stack.iter().map(|(name, _, _)| *name).collect();
                    path.push(key);
                    return Err(graph_error(format!(
                        "{where_}: the nodes form a cycle: {}",
                        path.join(" -> ")
                    )));
                }
                None => {
                    state.insert(key, State::Active);
                    stack.push((key, py_sorted(dependencies(node)), 0));
                }
            }
        }
    }
    Ok(order)
}

/// A graph document (graph.schema.json) as a Graph.
pub fn graph_from_json(raw: &Json, where_: &str) -> Result<Graph> {
    if too_deep(raw) {
        return Err(graph_error(format!(
            "{where_} is nested more than {MAX_GRAPH_DEPTH} levels deep (objects and lists inside one another); \
             a graph document is never that deep"
        )));
    }
    validate(SchemaName::Graph, raw, where_, None).map_err(|error| {
        if error.kind() == ErrorType::ContractError {
            graph_error(error.message().to_owned())
        } else {
            error
        }
    })?;
    // The schema guarantees the shapes read below (fields, types and nesting).
    let params = strings(raw.get("params"));
    let raw_nodes = raw["nodes"].as_object().expect("the schema requires nodes");
    let mut nodes = IndexMap::new();
    for (name, body) in raw_nodes {
        let context: IndexMap<String, String> = body
            .get("context")
            .and_then(Json::as_object)
            .map(|map| {
                map.iter()
                    .filter_map(|(field, reference)| {
                        param_of(reference).map(|param| (field.clone(), param))
                    })
                    .collect()
            })
            .unwrap_or_default();
        let node = GraphNode {
            name: name.clone(),
            call: body.get("call").and_then(Json::as_str).map(str::to_owned),
            code: body.get("code").and_then(Json::as_str).map(str::to_owned),
            after: strings(body.get("after")),
            labels: body.get("labels").and_then(param_of),
            when: body.get("when").cloned(),
            // the schema's optional field, whose meaning when absent is 'return' (graph.py's own reading)
            on_low_confidence: body
                .get("on_low_confidence")
                .and_then(Json::as_str)
                .unwrap_or("return")
                .to_owned(),
            context,
        };
        let unknown = py_sorted(
            dependencies(&node)
                .into_iter()
                .filter(|other| !raw_nodes.contains_key(other)),
        );
        if !unknown.is_empty() {
            return Err(graph_error(format!(
                "{where_}: node {name} refers to unknown node(s) {}",
                unknown.join(", ")
            )));
        }
        let used: BTreeSet<String> = node
            .context
            .values()
            .cloned()
            .chain(node.labels.clone())
            .collect();
        let undeclared = py_sorted(used.into_iter().filter(|param| !params.contains(param)));
        if !undeclared.is_empty() {
            return Err(graph_error(format!(
                "{where_}: node {name} uses undeclared param(s) {}",
                undeclared.join(", ")
            )));
        }
        nodes.insert(name.clone(), node);
    }
    let outputs = strings(raw.get("outputs"));
    let missing: BTreeSet<String> = outputs
        .iter()
        .filter(|name| !nodes.contains_key(*name))
        .cloned()
        .collect();
    if !missing.is_empty() {
        return Err(graph_error(format!(
            "{where_}: outputs name unknown node(s) {}",
            py_sorted(missing).join(", ")
        )));
    }
    let order = dependency_order(&nodes, where_)?;
    Ok(Graph {
        id: raw["graph"]
            .as_str()
            .expect("the schema requires graph")
            .to_owned(),
        version: raw["version"]
            .as_str()
            .expect("the schema requires version")
            .to_owned(),
        input: raw["input"]
            .as_str()
            .expect("the schema requires input")
            .to_owned(),
        params,
        nodes,
        outputs,
        order,
    })
}

fn param_reference(param: &str) -> Json {
    Json::Object(JsonMap::from_iter([(
        "param".to_owned(),
        Json::from(param),
    )]))
}

fn node_to_json(graph_id: &str, key: &str, node: &GraphNode) -> Result<Json> {
    if node.name != key {
        return Err(graph_error(format!(
            "{graph_id}: the node under {} is named {}; a graph document names each node by its key only",
            py_str(key),
            node.name
        )));
    }
    let mut body = JsonMap::new();
    match (&node.call, &node.code) {
        (Some(_), Some(_)) => {
            return Err(graph_error(format!(
                "{graph_id}: node {key} has both call and code; a node is a model call or a code function"
            )));
        }
        (None, None) => {
            return Err(graph_error(format!(
                "{graph_id}: node {key} has neither call nor code"
            )));
        }
        (Some(call), None) => {
            body.insert("call".into(), Json::from(call.as_str()));
        }
        (None, Some(code)) => {
            body.insert("code".into(), Json::from(code.as_str()));
        }
    }
    if node.on_low_confidence != "return" && node.on_low_confidence != "error" {
        return Err(graph_error(format!(
            "{graph_id}: node {key} has on_low_confidence {}; a graph document holds only 'return' or 'error' \
             (pass other policies in policies=)",
            py_str(&node.on_low_confidence)
        )));
    }
    if !node.after.is_empty() {
        body.insert("after".into(), Json::from(node.after.clone()));
    }
    if !node.context.is_empty() {
        let context: JsonMap = node
            .context
            .iter()
            .map(|(field, param)| (field.clone(), param_reference(param)))
            .collect();
        body.insert("context".into(), Json::Object(context));
    }
    if let Some(param) = &node.labels {
        body.insert("labels".into(), param_reference(param));
    }
    if let Some(when) = &node.when {
        body.insert("when".into(), when.clone());
    }
    if node.on_low_confidence != "return" {
        body.insert(
            "on_low_confidence".into(),
            Json::from(node.on_low_confidence.as_str()),
        );
    }
    Ok(Json::Object(body))
}

/// Python's `==` between two nodes (a condition's `true` equals `1`, as Python compares).
fn same_node(a: &GraphNode, b: &GraphNode) -> bool {
    let when = match (&a.when, &b.when) {
        (None, None) => true,
        (Some(x), Some(y)) => py_equals(x, y),
        _ => false,
    };
    a.name == b.name
        && a.call == b.call
        && a.code == b.code
        && a.after == b.after
        && a.context == b.context
        && a.labels == b.labels
        && when
        && a.on_low_confidence == b.on_low_confidence
}

/// The graph as the document graph_from_json reads: graph_from_json(graph_to_json(graph)) is the same graph. A
/// Graph the document cannot hold exactly (a node with both or neither of call and code, a policy other than
/// 'return' or 'error', a node keyed by another name, an order other than the one its dependencies give) is a
/// GraphError naming what cannot be written.
pub fn graph_to_json(graph: &Graph) -> Result<Json> {
    condition_too_deep(&graph.id, graph)?;
    let mut nodes = JsonMap::new();
    for (key, node) in &graph.nodes {
        nodes.insert(key.clone(), node_to_json(&graph.id, key, node)?);
    }
    let mut raw = JsonMap::new();
    raw.insert("graph".into(), Json::from(graph.id.as_str()));
    raw.insert("version".into(), Json::from(graph.version.as_str()));
    raw.insert("input".into(), Json::from(graph.input.as_str()));
    raw.insert("params".into(), Json::from(graph.params.clone()));
    raw.insert("nodes".into(), Json::Object(nodes));
    raw.insert("outputs".into(), Json::from(graph.outputs.clone()));
    let raw = Json::Object(raw);
    let rebuilt = graph_from_json(&raw, &format!("graph {}", graph.id))?;
    let nodes_same = rebuilt.nodes.len() == graph.nodes.len()
        && graph.nodes.iter().all(|(key, node)| {
            rebuilt
                .nodes
                .get(key)
                .is_some_and(|other| same_node(node, other))
        });
    let differing: Vec<&str> = [
        ("id", rebuilt.id == graph.id),
        ("version", rebuilt.version == graph.version),
        ("input", rebuilt.input == graph.input),
        ("params", rebuilt.params == graph.params),
        ("nodes", nodes_same),
        ("outputs", rebuilt.outputs == graph.outputs),
        ("order", rebuilt.order == graph.order),
    ]
    .into_iter()
    .filter(|(_, same)| !same)
    .map(|(field, _)| field)
    .collect();
    if !differing.is_empty() {
        let detail = if differing == ["order"] {
            format!(
                "its order {} is not the order its dependencies give ({})",
                graph.order.join(", "),
                rebuilt.order.join(", ")
            )
        } else {
            format!("field(s) {} differ", differing.join(", "))
        };
        return Err(graph_error(format!(
            "{} cannot be written as a graph document that reads back the same: {detail}",
            graph.id
        )));
    }
    Ok(raw)
}

/// The graph in the YAML file at `path`. Raises ManifestError for a file that cannot be read or is not valid
/// YAML, and GraphError for a document that breaks graph.schema.json or a graph that is invalid (an unknown node, a
/// cycle, a document nested too deep).
pub fn load_graph(path: &Path) -> Result<Graph> {
    graph_from_json(&read_yaml_file(path)?, &path.display().to_string())
}

/// A node result's value for `is` and `in`: a decision's value or what a code node returned (None for a skipped
/// node, which equals nothing, as Python's comparison of a Skipped object).
fn value_of(result: &NodeResult) -> Option<Json> {
    match result {
        NodeResult::Decision(decision) => Some(decision.value.to_json()),
        NodeResult::Code(value) => Some(value.clone()),
        NodeResult::Skipped(_) => None,
    }
}

/// Where a condition is read: the graph and the node it belongs to, for errors that name them.
struct ConditionOf<'a> {
    graph_id: &'a str,
    node: &'a str,
}

impl ConditionOf<'_> {
    fn unreadable(&self, part: &Json) -> JeffError {
        graph_error(format!(
            "{}: the condition of node {} has a part graph.schema.json does not allow: {}",
            self.graph_id,
            self.node,
            py_repr(part)
        ))
    }
}

/// Whether `condition` holds for the results so far. check_graph has refused a condition nested more than
/// MAX_GRAPH_DEPTH levels deep, so this recursion is bounded.
fn holds(
    condition: &Json,
    results: &IndexMap<String, NodeResult>,
    of: &ConditionOf<'_>,
) -> Result<bool> {
    let Some(parts) = condition.as_object() else {
        return Err(of.unreadable(condition));
    };
    if let Some(items) = parts.get("all") {
        let items = items.as_array().ok_or_else(|| of.unreadable(condition))?;
        for item in items {
            if !holds(item, results, of)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if let Some(items) = parts.get("any") {
        let items = items.as_array().ok_or_else(|| of.unreadable(condition))?;
        for item in items {
            if holds(item, results, of)? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    if let Some(inner) = parts.get("not") {
        return Ok(!holds(inner, results, of)?);
    }
    let Some(Json::String(name)) = parts.get("node") else {
        return Err(of.unreadable(condition));
    };
    let result = results.get(name).ok_or_else(|| {
        graph_error(format!(
            "{}: the condition of node {} reads node {name}, which has no result yet",
            of.graph_id, of.node
        ))
    })?;
    if let Some(expected) = parts.get("is") {
        return Ok(value_of(result).is_some_and(|value| py_equals(&value, expected)));
    }
    if let Some(options) = parts.get("in") {
        let options = options.as_array().ok_or_else(|| of.unreadable(condition))?;
        return Ok(value_of(result)
            .is_some_and(|value| options.iter().any(|option| py_equals(&value, option))));
    }
    let NodeResult::Decision(decision) = result else {
        return Err(graph_error(format!(
            "Condition on {name} needs a model decision; {name} is a code node"
        )));
    };
    if let Some(abstained) = parts.get("abstained") {
        // Python: `result.abstained is condition["abstained"]`, true only for the same bool
        return Ok(Json::Bool(decision.abstained) == *abstained);
    }
    // Python: `result.confidence is not None and result.confidence < condition["confidence_below"]`
    let Some(confidence) = decision.confidence else {
        return Ok(false);
    };
    let below = match parts.get("confidence_below") {
        Some(Json::Number(number)) => number.as_f64().expect("a JSON number reads as a float"),
        Some(Json::Bool(flag)) => f64::from(u8::from(*flag)),
        _ => return Err(of.unreadable(condition)),
    };
    Ok(confidence < below)
}

/// Each model node's call, binding and policy, as check_graph resolved them.
pub(crate) type ModelNodes = IndexMap<String, (Call, Binding, Policy)>;

/// A param's value as the labels of a call to `function`, read as Python's binding reads `params[node.labels]`, with
/// the refusal to raise if the binding takes them. Null is no labels; a list of key strings, or an object of
/// descriptions that are strings or null, are those labels. Python hands any other value to the binding, which
/// refuses it with the words it has for labels of the wrong kind: such a value is bound here as the kind of labels
/// `function` does not take (an empty list where it takes an object, an empty object where it takes a list), so the
/// binding refuses it with those words. An object with a description that is neither a string nor null, for a
/// function that takes an object, is bound with its keys alone, so the binding refuses its keys as Python's does;
/// keys Python would take, Labels cannot hold with such descriptions, so Labels::from_json's refusal follows.
fn labels_of(value: &Json, function: &FunctionSpec) -> (Option<Labels>, Option<JeffError>) {
    let takes_object = function.question_type == QuestionType::Choice
        && matches!(
            function.labels.source,
            LabelSource::Caller | LabelSource::Describe
        );
    match value {
        Json::Null => (None, None),
        Json::Array(items) if items.iter().all(Json::is_string) => (
            Some(Labels::Keys(
                items
                    .iter()
                    .filter_map(Json::as_str)
                    .map(str::to_owned)
                    .collect(),
            )),
            None,
        ),
        Json::Object(map) if map.values().all(|text| text.is_null() || text.is_string()) => (
            Some(Labels::Described(
                map.iter()
                    .map(|(key, text)| (key.clone(), text.as_str().map(str::to_owned)))
                    .collect(),
            )),
            None,
        ),
        Json::Object(map) if takes_object => (
            Some(Labels::Described(
                map.keys().map(|key| (key.clone(), None)).collect(),
            )),
            Some(
                Labels::from_json(value)
                    .expect_err("an object with a description that is not a string or null"),
            ),
        ),
        _ if takes_object => (Some(Labels::Keys(Vec::new())), None),
        _ => (Some(Labels::Described(IndexMap::new())), None),
    }
}

/// Everything that can be checked before the first decision: params, code and policies name real things, and every
/// model node resolves in the catalogue, is installed, binds its context and labels, and has a valid policy.
pub(crate) async fn check_graph(
    jeff: &Jeff,
    graph: &Graph,
    input: &Json,
    params: &JsonMap,
    code: &IndexMap<String, CodeFunction>,
    policies: &IndexMap<String, Policy>,
) -> Result<ModelNodes> {
    condition_too_deep(&graph.id, graph)?;
    let missing: Vec<&str> = graph
        .params
        .iter()
        .filter(|name| !params.contains_key(*name))
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(graph_error(format!(
            "{} needs param(s) {}",
            graph.id,
            missing.join(", ")
        )));
    }
    let unknown_policies = py_sorted(
        policies
            .keys()
            .filter(|name| !graph.nodes.contains_key(*name))
            .cloned(),
    );
    if !unknown_policies.is_empty() {
        let names: Vec<&str> = graph.nodes.keys().map(String::as_str).collect();
        return Err(graph_error(format!(
            "{}: policies= names node(s) {}, which the graph does not have (its nodes: {})",
            graph.id,
            unknown_policies.join(", "),
            names.join(", ")
        )));
    }
    let used: BTreeSet<String> = graph
        .nodes
        .values()
        .filter_map(|node| node.code.clone())
        .collect();
    let unknown_code = py_sorted(code.keys().filter(|name| !used.contains(*name)).cloned());
    if !unknown_code.is_empty() {
        let uses = if used.is_empty() {
            "none".to_owned()
        } else {
            py_sorted(used.iter().cloned()).join(", ")
        };
        return Err(graph_error(format!(
            "{}: code= names function(s) {}, which no node uses (code nodes use: {uses})",
            graph.id,
            unknown_code.join(", ")
        )));
    }
    let missing_code = py_sorted(
        used.iter()
            .filter(|name| !code.contains_key(*name))
            .cloned(),
    );
    if !missing_code.is_empty() {
        return Err(graph_error(format!(
            "{} needs code function(s) {}; pass them in code=",
            graph.id,
            missing_code.join(", ")
        )));
    }
    let mut model = ModelNodes::new();
    for name in &graph.order {
        let node = graph.nodes.get(name).ok_or_else(|| {
            graph_error(format!(
                "{}: its order names node {name}, which the graph does not have",
                graph.id
            ))
        })?;
        let Some(reference) = &node.call else {
            if node.code.is_none() {
                return Err(graph_error(format!(
                    "{}: node {name} has neither call nor code",
                    graph.id
                )));
            }
            continue;
        };
        let function = jeff.catalogue().function(reference).map_err(|error| {
            if error.kind() == ErrorType::UnknownFunction {
                graph_error(format!(
                    "{}: node {name} calls {reference}: {}",
                    graph.id,
                    error.message()
                ))
            } else {
                error
            }
        })?;
        if !jeff
            .engine()
            .installed(false)
            .await?
            .contains(&function.adapter_id)
        {
            return Err(not_installed(
                &function.id,
                &format!("node {name} of graph {} calls it", graph.id),
            ));
        }
        // Python: `params[node.labels] if node.labels else None` (an empty param name gives no labels).
        let labels_param = node.labels.as_deref().filter(|param| !param.is_empty());
        let not_given = py_sorted(
            node.context
                .values()
                .map(String::as_str)
                .chain(labels_param)
                .filter(|param| !params.contains_key(*param))
                .map(str::to_owned)
                .collect::<BTreeSet<_>>(),
        );
        if !not_given.is_empty() {
            return Err(graph_error(format!(
                "{}: node {name} uses param(s) {} that were not given",
                graph.id,
                not_given.join(", ")
            )));
        }
        let context: JsonMap = node
            .context
            .iter()
            .map(|(field, param)| (field.clone(), params[param.as_str()].clone()))
            .collect();
        let bound = async {
            let (labels, refusal) = match labels_param {
                Some(param) => labels_of(&params[param], &function),
                None => (None, None),
            };
            let call = Call {
                function: reference.clone(),
                input: input.clone(),
                context: if node.context.is_empty() {
                    None
                } else {
                    Some(context)
                },
                labels,
            };
            let binding = jeff.bind_call(&call).await?;
            if let Some(refusal) = refusal {
                return Err(refusal);
            }
            Ok::<(Call, Binding), JeffError>((call, binding))
        };
        let (call, binding) = bound.await.map_err(|error| match error.kind() {
            // a refused licence is the caller's, not a problem with the graph
            ErrorType::NotLicensed => error,
            _ => graph_error(format!(
                "{}: node {name} ({}): {}",
                graph.id,
                function.id,
                error.message()
            )),
        })?;
        let policy = match policies.get(name) {
            Some(policy) => policy.clone(),
            None => Policy::named(&node.on_low_confidence)?,
        };
        check_policy(jeff, &function.interface, &policy).await?;
        model.insert(name.clone(), (call, binding, policy));
    }
    Ok(model)
}

/// Runs a graph whose model nodes check_graph resolved: each stage is every node whose dependencies have results, its
/// model nodes decided in one batch, then its code nodes called with every result so far.
pub(crate) async fn run_checked(
    jeff: &Jeff,
    graph: &Graph,
    input: &Json,
    params: &JsonMap,
    code: &IndexMap<String, CodeFunction>,
    model: &ModelNodes,
) -> Result<GraphResult> {
    let mut results: IndexMap<String, NodeResult> = IndexMap::new();
    let mut stages = 0;
    while results.len() < graph.nodes.len() {
        // check_graph found every name of the order among the nodes
        let ready: Vec<&String> = graph
            .order
            .iter()
            .filter(|name| {
                !results.contains_key(*name)
                    && dependencies(&graph.nodes[name.as_str()])
                        .iter()
                        .all(|dependency| results.contains_key(dependency))
            })
            .collect();
        if ready.is_empty() {
            let waiting = py_sorted(
                graph
                    .nodes
                    .keys()
                    .filter(|name| !results.contains_key(*name))
                    .cloned(),
            );
            return Err(graph_error(format!(
                "{} makes no progress: node(s) {} wait on each other or on nodes the graph does not have",
                graph.id,
                waiting.join(", ")
            )));
        }
        stages += 1;
        let mut batch: Vec<&String> = Vec::new();
        let mut coded: Vec<(&String, &String)> = Vec::new();
        for name in ready {
            let node = &graph.nodes[name.as_str()];
            let skipped = py_sorted(dependencies(node).into_iter().filter(|dependency| {
                matches!(results.get(dependency), Some(NodeResult::Skipped(_)))
            }));
            let of = ConditionOf {
                graph_id: &graph.id,
                node: name,
            };
            if let Some(first) = skipped.first() {
                results.insert(
                    name.clone(),
                    NodeResult::Skipped(format!("depends on skipped node {first}")),
                );
            } else if let Some(when) = &node.when
                && !holds(when, &results, &of)?
            {
                results.insert(
                    name.clone(),
                    NodeResult::Skipped("condition not met".into()),
                );
            } else if model.contains_key(name) {
                batch.push(name);
            } else if let Some(code_name) = &node.code {
                coded.push((name, code_name));
            } else {
                return Err(graph_error(format!(
                    "{}: node {name} has neither call nor code",
                    graph.id
                )));
            }
        }
        if !batch.is_empty() {
            let bound: Vec<(Call, Binding)> = batch
                .iter()
                .map(|name| (model[*name].0.clone(), model[*name].1.clone()))
                .collect();
            let decisions = jeff.decide_bound(&bound).await?;
            for (name, decision) in batch.iter().zip(decisions) {
                let (call, _, policy) = &model[*name];
                results.insert(
                    (*name).clone(),
                    NodeResult::Decision(jeff.apply_policy(call, decision, policy).await?),
                );
            }
        }
        for (name, code_name) in coded {
            let function = &code[code_name];
            let call = CodeCall {
                input: input.clone(),
                params: params.clone(),
                nodes: results.clone(),
            };
            let value = function(call).await.map_err(|error| {
                graph_error(format!(
                    "{}: code function {code_name} (node {name}) failed: {error}",
                    graph.id
                ))
                .with_source(error)
            })?;
            results.insert(name.clone(), NodeResult::Code(value));
        }
    }
    // Python reads each output's result here (a KeyError for a Graph built by hand whose outputs name no node)
    let unknown_outputs = py_sorted(
        graph
            .outputs
            .iter()
            .filter(|name| !results.contains_key(*name))
            .cloned()
            .collect::<BTreeSet<_>>(),
    );
    if !unknown_outputs.is_empty() {
        return Err(graph_error(format!(
            "{}: outputs name unknown node(s) {}",
            graph.id,
            unknown_outputs.join(", ")
        )));
    }
    let outputs = graph
        .outputs
        .iter()
        .map(|name| (name.clone(), results[name.as_str()].clone()))
        .collect();
    Ok(GraphResult {
        nodes: results,
        outputs,
        stages,
    })
}
