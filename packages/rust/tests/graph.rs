//! Decision graphs: reading and writing graph documents, the checks before anything runs, conditions, code nodes, and
//! deep input refused by name. Every expected message was checked against the Python SDK unless a test says otherwise.

use std::sync::Arc;

use indexmap::IndexMap;
use jeff_sdk::binding::Row;
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::graph::{
    CodeCall, CodeFunction, Graph, GraphNode, GraphResult, MAX_GRAPH_DEPTH, NodeResult,
    code_function, graph_from_json, graph_to_json, load_graph,
};
use jeff_sdk::testing::FakeEngine;
use jeff_sdk::{BoxError, ErrorType, Jeff, Json, JsonMap};
use serde_json::json;

fn doc() -> Json {
    json!({"graph": "jeff.graphs.t", "version": "1.0.0", "input": "message", "params": ["channel"],
        "nodes": {"kind": {"call": "spam.message_type", "context": {"channel": {"param": "channel"}}},
            "flag": {"code": "flag", "when": {"node": "kind", "in": ["spam", true]}}},
        "outputs": ["kind", "flag"]})
}

#[test]
fn a_graph_document_reads_back_the_same_after_graph_to_json() {
    let graph = graph_from_json(&doc(), "t").unwrap();
    assert_eq!(graph.order, ["kind", "flag"]);
    assert_eq!(graph_to_json(&graph).unwrap(), doc());
}

#[test]
fn a_node_naming_an_unknown_node_or_param_is_refused() {
    let mut bad = doc();
    bad["nodes"]["flag"]["after"] = json!(["ghost"]);
    let error = graph_from_json(&bad, "t").unwrap_err();
    assert_eq!(
        (error.kind(), error.message()),
        (
            ErrorType::GraphError,
            "t: node flag refers to unknown node(s) ghost"
        )
    );
    let mut undeclared = doc();
    undeclared["params"] = json!([]);
    assert_eq!(
        graph_from_json(&undeclared, "t").unwrap_err().message(),
        "t: node kind uses undeclared param(s) channel"
    );
}

fn number_like(names: &[&str]) -> Json {
    let nodes: JsonMap = names
        .iter()
        .map(|name| ((*name).to_owned(), json!({"code": format!("c{name}")})))
        .collect();
    json!({"graph": "jeff.graphs.t", "version": "1.0.0", "input": "message", "params": [], "nodes": nodes, "outputs": [names[0]]})
}

#[test]
fn number_like_node_names_keep_their_order_and_write_back() {
    let graph = graph_from_json(&number_like(&["b", "10", "2"]), "t").unwrap();
    assert_eq!(graph.nodes.keys().collect::<Vec<_>>(), ["b", "10", "2"]);
    assert_eq!(graph.order, ["b", "10", "2"]);
    let written = graph_to_json(&graph).unwrap();
    assert_eq!(
        written["nodes"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        ["b", "10", "2"]
    );
    assert_eq!(graph_from_json(&written, "t").unwrap(), graph);
}

#[test]
fn a_graph_whose_order_is_not_its_dependencies_order_cannot_be_written() {
    let graph = graph_from_json(&doc(), "t").unwrap();
    let reordered = Graph {
        order: vec!["flag".into(), "kind".into()],
        ..graph
    };
    assert_eq!(
        graph_to_json(&reordered).unwrap_err().message(),
        "jeff.graphs.t cannot be written as a graph document \
        that reads back the same: its order flag, kind is not the order its dependencies give (kind, flag)"
    );
}

#[test]
fn a_node_with_a_policy_the_document_cannot_hold_is_refused_by_graph_to_json() {
    let mut graph = graph_from_json(&doc(), "t").unwrap();
    graph.nodes.get_mut("kind").unwrap().on_low_confidence = "escalate".into();
    assert_eq!(
        graph_to_json(&graph).unwrap_err().message(),
        "jeff.graphs.t: node kind has on_low_confidence 'escalate'; a \
        graph document holds only 'return' or 'error' (pass other policies in policies=)"
    );
}

fn code_only() -> Json {
    json!({"graph": "jeff.graphs.code", "version": "1.0.0", "input": "message", "params": [],
        "nodes": {"value": {"code": "make_value"}}, "outputs": ["value"]})
}

async fn no_engine() -> Jeff {
    Jeff::new(
        Arc::new(FakeEngine::new(|_, _| vec![], Vec::<String>::new())),
        Catalogue::shipped().unwrap(),
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn a_code_function_returning_null_stores_it_and_one_that_fails_is_a_graph_error() {
    let jeff = no_engine().await;
    let null = IndexMap::from([(
        "make_value".to_owned(),
        code_function(|_call: CodeCall| async { Ok::<Json, BoxError>(Json::Null) }),
    )]);
    let result = jeff
        .run_graph(
            code_only(),
            json!("hi"),
            JsonMap::new(),
            null,
            IndexMap::new(),
        )
        .await
        .unwrap();
    assert_eq!(
        result.outputs["value"].to_json().unwrap(),
        json!({"code": null})
    );
    let failing = IndexMap::from([(
        "make_value".to_owned(),
        code_function(|_call: CodeCall| async { Err::<Json, BoxError>("no ticket system".into()) }),
    )]);
    let error = jeff
        .run_graph(
            code_only(),
            json!("hi"),
            JsonMap::new(),
            failing,
            IndexMap::new(),
        )
        .await
        .unwrap_err();
    assert_eq!(
        (error.kind(), error.message()),
        (
            ErrorType::GraphError,
            "jeff.graphs.code: code function make_value (node value) failed: no ticket system"
        )
    );
    assert_eq!(
        std::error::Error::source(&error).unwrap().to_string(),
        "no ticket system"
    );
}

// ---- beyond the brief: cycles, conditions, hand-built graphs and deep input, each checked against Python

fn graph_of(nodes: Json, outputs: Json, params: Json) -> Json {
    json!({"graph": "jeff.graphs.t", "version": "1.0.0", "input": "m", "params": params, "nodes": nodes,
        "outputs": outputs})
}

fn refusal(raw: &Json) -> String {
    let error = graph_from_json(raw, "w").unwrap_err();
    assert_eq!(error.kind(), ErrorType::GraphError, "{}", error.message());
    error.message().to_owned()
}

#[test]
fn cycles_are_refused_naming_the_path_that_closes_them() {
    assert_eq!(
        refusal(&graph_of(
            json!({"a": {"code": "a", "after": ["a"]}}),
            json!(["a"]),
            json!([])
        )),
        "w: the nodes form a cycle: a -> a"
    );
    assert_eq!(
        refusal(&graph_of(
            json!({"a": {"code": "a", "when": {"node": "c", "is": 1}}, "b": {"code": "b", "after": ["a"]},
            "c": {"code": "c", "after": ["b"]}}),
            json!(["a"]),
            json!([])
        )),
        "w: the nodes form a cycle: a -> c -> b -> a"
    );
    assert_eq!(
        refusal(&graph_of(
            json!({"x": {"code": "a", "after": ["z", "y"]}, "y": {"code": "b", "after": ["x"]}, "z": {"code": "c"}}),
            json!(["x"]),
            json!([])
        )),
        "w: the nodes form a cycle: x -> y -> x"
    );
}

#[test]
fn unknown_nodes_params_and_outputs_are_listed_in_pythons_order() {
    assert_eq!(
        refusal(&graph_of(
            json!({"a": {"code": "a", "after": ["q", "b10", "b2"],
            "when": {"all": [{"node": "p", "is": 1}, {"not": {"node": "a1", "abstained": true}}]}}}),
            json!(["a"]),
            json!([])
        )),
        "w: node a refers to unknown node(s) a1, b10, b2, p, q"
    );
    assert_eq!(
        refusal(&graph_of(
            json!({"a": {"call": "x", "labels": {"param": "L"}, "context": {"c": {"param": "z"}}}}),
            json!(["a"]),
            json!([])
        )),
        "w: node a uses undeclared param(s) L, z"
    );
    assert_eq!(
        refusal(&graph_of(
            json!({"a": {"code": "a"}}),
            json!(["q", "b", "a"]),
            json!([])
        )),
        "w: outputs name unknown node(s) b, q"
    );
}

#[test]
fn a_document_the_schema_refuses_is_a_graph_error_with_the_schema_message() {
    assert_eq!(
        refusal(&graph_of(
            json!({"a": {"code": "a", "on_low_confidence": "escalate"}}),
            json!(["a"]),
            json!([])
        )),
        "w does not match the graph schema:\n  at /nodes/a/on_low_confidence: 'escalate' is not one of ['return', 'error']"
    );
    assert_eq!(
        refusal(&graph_of(
            json!({"a": {"code": "a", "call": "x"}}),
            json!(["a"]),
            json!([])
        )),
        "w does not match the graph schema:\n  at /nodes/a: {'code': 'a', 'call': 'x'} is valid under each of \
         {'required': ['code']}, {'required': ['call']}"
    );
    assert_eq!(
        refusal(&json!([1])),
        "w does not match the graph schema:\n  at /: [1] is not of type 'object'"
    );
}

#[test]
fn number_like_dependencies_order_nodes_as_python_sorts_them() {
    let raw = graph_of(
        json!({"b": {"code": "cb", "after": ["2", "10"]}, "10": {"code": "c10"}, "2": {"code": "c2"}}),
        json!(["b"]),
        json!([]),
    );
    let graph = graph_from_json(&raw, "w").unwrap();
    assert_eq!(graph.nodes.keys().collect::<Vec<_>>(), ["b", "10", "2"]);
    assert_eq!(graph.order, ["10", "2", "b"]);
}

#[test]
fn a_graph_file_reads_as_its_document_and_a_key_that_is_not_a_string_is_refused() {
    let folder = std::env::temp_dir().join(format!("jeff-graph-test-{}", std::process::id()));
    std::fs::create_dir_all(&folder).unwrap();
    let quoted = folder.join("quoted.yaml");
    std::fs::write(&quoted, "graph: jeff.graphs.t\nversion: 1.0.0\ninput: m\nparams: []\nnodes:\n  b: {code: cb, after: ['10']}\n  '10': {code: c10}\noutputs: [b]\n").unwrap();
    let graph = load_graph(&quoted).unwrap();
    assert_eq!(graph.nodes.keys().collect::<Vec<_>>(), ["b", "10"]);
    assert_eq!(graph.order, ["10", "b"]);
    // PyYAML reads `10:` as the int 10, a node name Python keeps as an int; this SDK refuses it by name (Task 5)
    let bare = folder.join("bare.yaml");
    std::fs::write(&bare, "graph: jeff.graphs.t\nversion: 1.0.0\ninput: m\nparams: []\nnodes:\n  b: {code: cb}\n  10: {code: c10}\noutputs: [b]\n").unwrap();
    let error = load_graph(&bare).unwrap_err();
    assert_eq!(error.kind(), ErrorType::ManifestError);
    assert!(
        error
            .message()
            .contains("a mapping key that is not a string (10); quote it"),
        "{}",
        error.message()
    );
    std::fs::remove_dir_all(&folder).unwrap();
}

#[test]
fn graph_to_json_names_a_node_it_cannot_write() {
    let graph = graph_from_json(&doc(), "t").unwrap();
    let mut renamed = graph.clone();
    let flag = renamed.nodes.shift_remove("flag").unwrap();
    renamed.nodes.insert("x".into(), flag);
    assert_eq!(
        graph_to_json(&renamed).unwrap_err().message(),
        "jeff.graphs.t: the node under 'x' is named flag; a graph document names each node by its key only"
    );
    let mut both = graph.clone();
    both.nodes.get_mut("kind").unwrap().code = Some("z".into());
    assert_eq!(
        graph_to_json(&both).unwrap_err().message(),
        "jeff.graphs.t: node kind has both call and code; a node is a model call or a code function"
    );
    let mut neither = graph.clone();
    neither.nodes.get_mut("kind").unwrap().call = None;
    assert_eq!(
        graph_to_json(&neither).unwrap_err().message(),
        "jeff.graphs.t: node kind has neither call nor code"
    );
    let bad_id = Graph {
        id: "BAD".into(),
        ..graph
    };
    assert_eq!(
        graph_to_json(&bad_id).unwrap_err().message(),
        "graph BAD does not match the graph schema:\n  at /graph: 'BAD' does not match \
         '^jeff(\\\\.[a-z0-9][a-z0-9_-]*){2}$|^[a-z][a-z0-9_-]*$'"
    );
}

#[test]
fn a_graph_result_round_trips_and_a_malformed_one_is_named() {
    let result = GraphResult {
        nodes: IndexMap::from([
            ("a".to_owned(), NodeResult::Code(json!({"x": 1}))),
            (
                "b".to_owned(),
                NodeResult::Skipped("condition not met".into()),
            ),
        ]),
        outputs: IndexMap::from([("a".to_owned(), NodeResult::Code(json!({"x": 1})))]),
        stages: 2,
    };
    assert_eq!(
        GraphResult::from_json(&result.to_json().unwrap()).unwrap(),
        result
    );
    let bad = json!({"nodes": {"a": {"x": 1, "b": 2}}, "outputs": {}, "stages": 1});
    assert_eq!(
        GraphResult::from_json(&bad).unwrap_err().message(),
        "A graph result node must hold 'decision', 'skipped' or 'code'; got keys ['b', 'x']"
    );
}

/// The urgency function answers level 1 with 0.9; every choice question answers its first option.
async fn engine_jeff() -> Jeff {
    let answers = |_: &str, row: &Row| -> Vec<f64> {
        let question = row.to_json()["question"].clone();
        if question["type"] == "score" {
            return vec![0.0, 0.9, 0.1, 0.0, 0.0];
        }
        let count = question["criteria"].as_object().unwrap().len();
        (0..count)
            .map(|index| if index == 0 { 1.0 } else { 0.0 })
            .collect()
    };
    Jeff::new(
        Arc::new(FakeEngine::new(
            answers,
            ["jeff.safety.spam", "jeff.support.triage"],
        )),
        Catalogue::shipped().unwrap(),
    )
    .await
    .unwrap()
}

fn returning(value: Json) -> CodeFunction {
    code_function(move |_call: CodeCall| {
        let value = value.clone();
        async move { Ok::<Json, BoxError>(value) }
    })
}

fn code_map(pairs: &[(&str, Json)]) -> IndexMap<String, CodeFunction> {
    pairs
        .iter()
        .map(|(name, value)| ((*name).to_owned(), returning(value.clone())))
        .collect()
}

fn values(result: &GraphResult) -> Json {
    Json::Object(
        result
            .nodes
            .iter()
            .map(|(name, result)| {
                let value = match result {
                    NodeResult::Decision(decision) => decision.value.to_json(),
                    NodeResult::Skipped(reason) => json!({"skipped": reason}),
                    NodeResult::Code(value) => value.clone(),
                };
                (name.clone(), value)
            })
            .collect(),
    )
}

async fn run(
    jeff: &Jeff,
    raw: Json,
    params: Json,
    code: &[(&str, Json)],
) -> jeff_sdk::Result<Json> {
    let result = jeff
        .run_graph(
            raw,
            json!("x"),
            params.as_object().unwrap().clone(),
            code_map(code),
            IndexMap::new(),
        )
        .await?;
    Ok(json!({"values": values(&result), "stages": result.stages}))
}

fn two_code_nodes(condition: Json) -> Json {
    graph_of(
        json!({"a": {"code": "a"}, "b": {"code": "b", "when": condition}}),
        json!(["b"]),
        json!([]),
    )
}

#[tokio::test]
async fn is_and_in_compare_a_code_value_as_python_compares() {
    let jeff = no_engine().await;
    let cases = [
        (json!(1), json!({"node": "a", "is": true}), true),
        (json!("1"), json!({"node": "a", "is": 1}), false),
        (json!(true), json!({"node": "a", "is": 1}), true),
        (json!(1.0), json!({"node": "a", "is": 1}), true),
        (json!(0), json!({"node": "a", "is": false}), true),
        (json!([1]), json!({"node": "a", "in": [1]}), false),
        (json!(null), json!({"node": "a", "in": ["x"]}), false),
        (json!(2.0), json!({"node": "a", "in": [2, "a"]}), true),
        (json!({"a": 1}), json!({"node": "a", "is": "a"}), false),
    ];
    for (value, condition, ran) in cases {
        let got = run(
            &jeff,
            two_code_nodes(condition.clone()),
            json!({}),
            &[("a", value.clone()), ("b", json!("ran"))],
        )
        .await
        .unwrap();
        let expected = if ran {
            json!("ran")
        } else {
            json!({"skipped": "condition not met"})
        };
        assert_eq!(got["values"]["b"], expected, "{value} {condition}");
        assert_eq!(got["stages"], 2);
    }
}

#[tokio::test]
async fn a_condition_needing_a_decision_on_a_code_node_is_refused() {
    let jeff = no_engine().await;
    for condition in [
        json!({"node": "a", "abstained": false}),
        json!({"node": "a", "confidence_below": 0.5}),
    ] {
        let error = run(
            &jeff,
            two_code_nodes(condition),
            json!({}),
            &[("a", json!(1)), ("b", json!(2))],
        )
        .await
        .unwrap_err();
        assert_eq!(
            (error.kind(), error.message()),
            (
                ErrorType::GraphError,
                "Condition on a needs a model decision; a is a code node"
            )
        );
    }
}

#[tokio::test]
async fn code_nodes_see_every_result_so_far_in_the_order_the_nodes_got_them() {
    let jeff = no_engine().await;
    let seen = code_function(|call: CodeCall| async move {
        Ok::<Json, BoxError>(Json::from(call.nodes.keys().cloned().collect::<Vec<_>>()))
    });
    let raw = graph_of(
        json!({"b": {"code": "f", "after": ["2"]}, "10": {"code": "f"}, "2": {"code": "f", "after": ["10"]}}),
        json!(["b"]),
        json!([]),
    );
    let result = jeff
        .run_graph(
            raw,
            json!("x"),
            JsonMap::new(),
            IndexMap::from([("f".to_owned(), seen)]),
            IndexMap::new(),
        )
        .await
        .unwrap();
    assert_eq!(
        values(&result),
        json!({"10": [], "2": ["10"], "b": ["10", "2"]})
    );
    assert_eq!(result.stages, 3);
}

const URGENCY: &str = "jeff.support.triage.urgency";

fn urgency_node() -> Json {
    json!({"call": URGENCY, "context": {"company": {"param": "company"}, "channel": {"param": "channel"}}})
}

fn shop() -> Json {
    json!({"channel": "email", "company": "A shop"})
}

#[tokio::test]
async fn model_nodes_are_decided_and_their_conditions_read() {
    let jeff = engine_jeff().await;
    let params = json!(["channel", "company"]);
    for (condition, ran) in [
        (json!({"node": "u", "confidence_below": 0.95}), true),
        (json!({"node": "u", "is": 1}), true),
        (json!({"node": "u", "abstained": true}), false),
    ] {
        let raw = graph_of(
            json!({"u": urgency_node(), "b": {"code": "b", "when": condition}}),
            json!(["u"]),
            params.clone(),
        );
        let got = run(&jeff, raw, shop(), &[("b", json!("ran"))])
            .await
            .unwrap();
        assert_eq!(got["values"]["u"], json!(1));
        let expected = if ran {
            json!("ran")
        } else {
            json!({"skipped": "condition not met"})
        };
        assert_eq!(got["values"]["b"], expected);
    }
}

#[tokio::test]
async fn the_checks_before_anything_runs_name_what_is_wrong() {
    let jeff = engine_jeff().await;
    let message = |result: jeff_sdk::Result<Json>| {
        let error = result.unwrap_err();
        (error.kind(), error.message().to_owned())
    };
    let code_nodes = graph_of(
        json!({"b": {"code": "a"}, "10": {"code": "c"}}),
        json!(["b"]),
        json!([]),
    );
    let error = jeff
        .run_graph(
            code_nodes,
            json!("x"),
            JsonMap::new(),
            code_map(&[("a", json!(1)), ("c", json!(1))]),
            IndexMap::from([
                ("zz".to_owned(), jeff_sdk::Policy::Error),
                ("10x".to_owned(), jeff_sdk::Policy::Error),
            ]),
        )
        .await
        .unwrap_err();
    assert_eq!(
        error.message(),
        "jeff.graphs.t: policies= names node(s) 10x, zz, which the graph does not have (its nodes: b, 10)"
    );
    let params = json!(["channel", "company"]);
    let urgency = graph_of(json!({"u": urgency_node()}), json!(["u"]), params.clone());
    assert_eq!(
        message(run(&jeff, urgency, shop(), &[("q", json!(1))]).await),
        (
            ErrorType::GraphError,
            "jeff.graphs.t: code= names function(s) q, which no node uses (code nodes use: none)"
                .into()
        )
    );
    let unknown = graph_of(
        json!({"u": {"call": "jeff.support.triage.nope"}}),
        json!(["u"]),
        json!([]),
    );
    assert_eq!(
        message(run(&jeff, unknown, json!({}), &[]).await),
        (
            ErrorType::GraphError,
            "jeff.graphs.t: node u calls jeff.support.triage.nope: jeff.support.triage has no function 'nope'; its \
             functions are route, urgency, sentiment, needs_human"
                .into()
        )
    );
    let colour = graph_of(
        json!({"u": {"call": URGENCY, "context": {"colour": {"param": "channel"}}}}),
        json!(["u"]),
        json!(["channel"]),
    );
    assert_eq!(
        message(run(&jeff, colour, json!({"channel": "e"}), &[]).await),
        (
            ErrorType::GraphError,
            "jeff.graphs.t: node u (jeff.support.triage.urgency): jeff.support.triage.urgency has no context \
             field(s) colour; its fields are company, channel"
                .into()
        )
    );
    let mut labelled = urgency_node();
    labelled["labels"] = json!({"param": "channel"});
    assert_eq!(
        message(
            run(
                &jeff,
                graph_of(json!({"u": labelled}), json!(["u"]), params.clone()),
                shop(),
                &[]
            )
            .await
        ),
        (
            ErrorType::GraphError,
            "jeff.graphs.t: node u (jeff.support.triage.urgency): jeff.support.triage.urgency is a score \
             function; it takes no labels="
                .into()
        )
    );
}

fn route_graph() -> Json {
    graph_of(
        json!({"r": {"call": "jeff.support.triage.route", "context": {"company": {"param": "company"},
            "channel": {"param": "channel"}}, "labels": {"param": "teams"}}}),
        json!(["r"]),
        json!(["channel", "company", "teams"]),
    )
}

#[tokio::test]
async fn a_labels_param_binds_as_python_binds_it() {
    let jeff = engine_jeff().await;
    let route = "jeff.graphs.t: node r (jeff.support.triage.route): jeff.support.triage.route";
    for (teams, expected) in [
        (
            json!(["a", "b"]),
            format!("{route}: pass labels= as an object of key -> description"),
        ),
        (
            json!({}),
            format!("{route} takes its labels from the caller; pass labels="),
        ),
        (
            json!(null),
            format!("{route} takes its labels from the caller; pass labels="),
        ),
    ] {
        let mut params = shop();
        params["teams"] = teams;
        let error = run(&jeff, route_graph(), params, &[]).await.unwrap_err();
        assert_eq!(
            (error.kind(), error.message()),
            (ErrorType::GraphError, expected.as_str())
        );
    }
    for teams in [json!(5), json!("abc"), json!(["a", 3]), json!(true)] {
        let mut params = shop();
        params["teams"] = teams.clone();
        let error = run(&jeff, route_graph(), params, &[]).await.unwrap_err();
        assert_eq!(
            error.message(),
            format!("{route}: pass labels= as an object of key -> description"),
            "{teams}"
        );
    }
    // Python takes a description that is not a string (and sends it to the engine); Labels cannot hold it, so this
    // SDK refuses it by name (an open concern of Task 11)
    let mut params = shop();
    params["teams"] = json!({"a": 1});
    let error = run(&jeff, route_graph(), params, &[]).await.unwrap_err();
    assert_eq!(
        error.message(),
        "jeff.graphs.t: node r (jeff.support.triage.route): labels= must be a list of keys or an object of key -> \
         description (a string or null); got {'a': 1}"
    );
}

fn labelled_graph(function: &str, fields: &[&str]) -> Json {
    let context: JsonMap = fields
        .iter()
        .map(|field| ((*field).to_owned(), json!({"param": format!("p_{field}")})))
        .collect();
    let mut params: Vec<String> = fields.iter().map(|field| format!("p_{field}")).collect();
    params.push("L".into());
    graph_of(
        json!({"n": {"call": function, "context": context, "labels": {"param": "L"}}}),
        json!(["n"]),
        json!(params),
    )
}

#[tokio::test]
async fn a_labels_param_of_the_wrong_kind_is_refused_in_the_functions_words() {
    let jeff = Jeff::new(
        Arc::new(FakeEngine::new(
            |_, _| vec![1.0, 0.0, 0.0],
            ["jeff.security.soc", "jeff.finance.trading-desk"],
        )),
        Catalogue::shipped().unwrap(),
    )
    .await
    .unwrap();
    let soc = "jeff.graphs.t: node n (jeff.security.soc.action): jeff.security.soc.action";
    // v1.4: soc's action has fixed labels the caller may offer a subset of (the Python SDK's messages for each case)
    let list = format!(
        "{soc} has fixed labels and may offer some of them; pass labels= as a list of keys from block, isolate, \
         escalate, investigate, ignore, or no labels= to offer them all"
    );
    let desk =
        "jeff.graphs.t: node n (jeff.finance.trading-desk.route): jeff.finance.trading-desk.route";
    let object = format!(
        "{desk}: pass labels= as an object with exactly the keys venue_a, venue_b, venue_c, each with its description"
    );
    let exactly = format!(
        "{desk} takes exactly the labels venue_a, venue_b, venue_c; unknown: a; missing: venue_a, venue_b, venue_c"
    );
    let cases = [
        (
            "jeff.security.soc.action",
            ["organisation", "playbook"],
            json!(5),
            list.clone(),
        ),
        (
            "jeff.security.soc.action",
            ["organisation", "playbook"],
            json!(["a", 3]),
            list.clone(),
        ),
        (
            "jeff.security.soc.action",
            ["organisation", "playbook"],
            json!({"a": 1}),
            list.clone(),
        ),
        (
            "jeff.security.soc.action",
            ["organisation", "playbook"],
            json!({}),
            list.clone(),
        ),
        (
            "jeff.security.soc.action",
            ["organisation", "playbook"],
            json!([]),
            format!(
                "{soc}: labels= is an empty list; pass at least one key from block, isolate, escalate, investigate, \
                 ignore, or no labels= to offer them all"
            ),
        ),
        (
            "jeff.security.soc.action",
            ["organisation", "playbook"],
            json!(["nope"]),
            format!(
                "{soc}: label(s) nope are not among its labels block, isolate, escalate, investigate, ignore"
            ),
        ),
        (
            "jeff.finance.trading-desk.route",
            ["desk", "market_context"],
            json!("abc"),
            object.clone(),
        ),
        (
            "jeff.finance.trading-desk.route",
            ["desk", "market_context"],
            json!(true),
            object.clone(),
        ),
        (
            "jeff.finance.trading-desk.route",
            ["desk", "market_context"],
            json!([]),
            object.clone(),
        ),
        (
            "jeff.finance.trading-desk.route",
            ["desk", "market_context"],
            json!({"a": 1}),
            exactly.clone(),
        ),
        (
            "jeff.finance.trading-desk.route",
            ["desk", "market_context"],
            json!({"a": "x"}),
            exactly.clone(),
        ),
    ];
    for (function, fields, labels, expected) in cases {
        let mut params: JsonMap = fields
            .iter()
            .map(|field| (format!("p_{field}"), json!("x")))
            .collect();
        params.insert("L".into(), labels.clone());
        let error = jeff
            .run_graph(
                labelled_graph(function, &fields),
                json!("in"),
                params,
                IndexMap::new(),
                IndexMap::new(),
            )
            .await
            .unwrap_err();
        assert_eq!(
            (error.kind(), error.message()),
            (ErrorType::GraphError, expected.as_str()),
            "{function} {labels}"
        );
    }
}

fn node(name: &str, call: Option<&str>, code: Option<&str>) -> GraphNode {
    GraphNode {
        name: name.into(),
        call: call.map(str::to_owned),
        code: code.map(str::to_owned),
        after: vec![],
        context: IndexMap::new(),
        labels: None,
        when: None,
        on_low_confidence: "return".into(),
    }
}

fn hand_built(nodes: Vec<GraphNode>, outputs: &[&str], order: &[&str]) -> Graph {
    Graph {
        id: "jeff.graphs.h".into(),
        version: "1.0.0".into(),
        input: "m".into(),
        params: vec![],
        nodes: nodes
            .into_iter()
            .map(|node| (node.name.clone(), node))
            .collect(),
        outputs: outputs.iter().map(|name| (*name).to_owned()).collect(),
        order: order.iter().map(|name| (*name).to_owned()).collect(),
    }
}

async fn run_built(jeff: &Jeff, graph: Graph) -> jeff_sdk::Result<GraphResult> {
    jeff.run_graph(
        graph,
        json!("x"),
        JsonMap::new(),
        code_map(&[("f", json!(1))]),
        IndexMap::new(),
    )
    .await
}

#[tokio::test]
async fn a_hand_built_graph_is_checked_where_python_checks_it() {
    let jeff = no_engine().await;
    let error = jeff
        .run_graph(
            hand_built(vec![node("u", None, None)], &["u"], &["u"]),
            json!("x"),
            JsonMap::new(),
            IndexMap::new(),
            IndexMap::new(),
        )
        .await
        .unwrap_err();
    assert_eq!(
        error.message(),
        "jeff.graphs.h: node u has neither call nor code"
    );
    let unordered = hand_built(
        vec![node("a", None, Some("f")), node("b", None, Some("f"))],
        &["a"],
        &["a"],
    );
    assert_eq!(
        run_built(&jeff, unordered).await.unwrap_err().message(),
        "jeff.graphs.h makes no progress: node(s) b wait on each other or on nodes the graph does not have"
    );
    let mut waiting = node("b", None, Some("f"));
    waiting.when = Some(json!({"node": "ghost", "is": 1}));
    let ghost = hand_built(
        vec![node("a", None, Some("f")), waiting],
        &["a"],
        &["a", "b"],
    );
    assert_eq!(
        run_built(&jeff, ghost).await.unwrap_err().message(),
        "jeff.graphs.h makes no progress: node(s) b wait on each other or on nodes the graph does not have"
    );
    let mut escalating = node("u", Some(URGENCY), None);
    escalating.on_low_confidence = "escalate".into();
    let jeff_with_engine = engine_jeff().await;
    let mut graph = hand_built(vec![escalating], &["u"], &["u"]);
    graph.params = vec!["channel".into(), "company".into()];
    graph.nodes.get_mut("u").unwrap().context = IndexMap::from([
        ("company".to_owned(), "company".to_owned()),
        ("channel".to_owned(), "channel".to_owned()),
    ]);
    let error = jeff_with_engine
        .run_graph(
            graph,
            json!("x"),
            shop().as_object().unwrap().clone(),
            IndexMap::new(),
            IndexMap::new(),
        )
        .await
        .unwrap_err();
    assert_eq!(
        (error.kind(), error.message()),
        (
            ErrorType::InvalidPolicy,
            "Unknown on_low_confidence policy 'escalate'; use 'return', 'error' or escalate(...)"
        )
    );
}

/// Where Python stops with a KeyError or a TypeError on a Graph built by hand, this SDK names what is wrong.
#[tokio::test]
async fn a_hand_built_graph_python_crashes_on_is_a_named_graph_error() {
    let jeff = no_engine().await;
    let order_ghost = hand_built(vec![node("a", None, Some("f"))], &["a"], &["a", "ghost"]);
    assert_eq!(
        run_built(&jeff, order_ghost).await.unwrap_err().message(),
        "jeff.graphs.h: its order names node ghost, which the graph does not have"
    );
    let output_ghost = hand_built(vec![node("a", None, Some("f"))], &["ghost"], &["a"]);
    assert_eq!(
        run_built(&jeff, output_ghost).await.unwrap_err().message(),
        "jeff.graphs.h: outputs name unknown node(s) ghost"
    );
    for (condition, part) in [
        (json!({"bogus": 1}), "{'bogus': 1}"),
        (
            json!({"all": {"node": "a", "is": 1}}),
            "{'all': {'node': 'a', 'is': 1}}",
        ),
        (
            json!({"node": "a", "in": "x1"}),
            "{'node': 'a', 'in': 'x1'}",
        ),
    ] {
        let mut reading = node("b", None, Some("f"));
        reading.when = Some(condition);
        let graph = hand_built(
            vec![node("a", None, Some("f")), reading],
            &["a"],
            &["a", "b"],
        );
        let error = run_built(&jeff, graph).await.unwrap_err();
        assert_eq!(
            (error.kind(), error.message().to_owned()),
            (
                ErrorType::GraphError,
                format!(
                    "jeff.graphs.h: the condition of node b has a part graph.schema.json does not allow: {part}"
                )
            )
        );
    }
    let mut missing_param = node("u", Some(URGENCY), None);
    missing_param.context = IndexMap::from([("company".to_owned(), "zz".to_owned())]);
    let jeff_with_engine = engine_jeff().await;
    let error = jeff_with_engine
        .run_graph(
            hand_built(vec![missing_param], &["u"], &["u"]),
            json!("x"),
            JsonMap::new(),
            IndexMap::new(),
            IndexMap::new(),
        )
        .await
        .unwrap_err();
    assert_eq!(
        error.message(),
        "jeff.graphs.h: node u uses param(s) zz that were not given"
    );
}

fn nested_not(levels: usize) -> Json {
    let mut condition = json!({"node": "a", "is": true});
    for _ in 0..levels {
        condition = json!({ "not": condition });
    }
    condition
}

fn nested_all(levels: usize) -> Json {
    let mut condition = json!({"node": "a", "is": true});
    for _ in 0..levels {
        condition = json!({ "all": [condition] });
    }
    condition
}

#[tokio::test]
async fn every_depth_python_reads_runs_and_a_deeper_document_is_refused_by_name() {
    let jeff = no_engine().await;
    // Python's deepest graph documents: 162 nested `not` and 122 nested `all` conditions
    for condition in [nested_not(162), nested_all(122), nested_not(252)] {
        let raw = two_code_nodes(condition);
        let got = run(
            &jeff,
            raw,
            json!({}),
            &[("a", json!(true)), ("b", json!("ran"))],
        )
        .await
        .unwrap();
        assert_eq!(got["stages"], 2);
    }
    for condition in [nested_not(253), nested_not(1_000), nested_all(500)] {
        let error = graph_from_json(&two_code_nodes(condition), "w").unwrap_err();
        assert_eq!(
            (error.kind(), error.message().to_owned()),
            (
                ErrorType::GraphError,
                format!(
                    "w is nested more than {MAX_GRAPH_DEPTH} levels deep (objects and lists inside one another); \
                     a graph document is never that deep"
                )
            )
        );
    }
}

#[tokio::test]
async fn a_hand_built_condition_too_deep_is_refused_before_anything_runs() {
    let jeff = no_engine().await;
    let mut deep = node("b", None, Some("f"));
    deep.when = Some(nested_not(1_000));
    let graph = hand_built(vec![node("a", None, Some("f")), deep], &["a"], &["a", "b"]);
    assert_eq!(
        dependencies_of(&graph, "b"),
        ["a"],
        "dependencies reads a deep condition without recursion"
    );
    let message = format!(
        "jeff.graphs.h: the condition of node b is nested more than {MAX_GRAPH_DEPTH} levels deep (objects and \
         lists inside one another)"
    );
    assert_eq!(graph_to_json(&graph).unwrap_err().message(), message);
    assert_eq!(
        run_built(&jeff, graph).await.unwrap_err().message(),
        message
    );
}

fn dependencies_of(graph: &Graph, name: &str) -> Vec<String> {
    jeff_sdk::graph::dependencies(&graph.nodes[name])
        .into_iter()
        .collect()
}

#[test]
fn a_long_chain_of_nodes_is_ordered_without_recursion() {
    let count = 50_000;
    let nodes: JsonMap = (0..count)
        .map(|index| {
            let body = if index + 1 < count {
                json!({"code": "f", "after": [format!("n{}", index + 1)]})
            } else {
                json!({"code": "f"})
            };
            (format!("n{index}"), body)
        })
        .collect();
    let graph = graph_from_json(
        &graph_of(Json::Object(nodes), json!(["n0"]), json!([])),
        "w",
    )
    .unwrap();
    assert_eq!(graph.order.first().unwrap(), &format!("n{}", count - 1));
    assert_eq!(graph.order.last().unwrap(), "n0");
}

#[test]
fn a_condition_no_alternative_matches_lists_each_alternatives_problems_in_pythons_order() {
    let raw = graph_of(
        json!({"a": {"code": "a", "when": {"bogus": 1}}}),
        json!(["a"]),
        json!([]),
    );
    let unexpected = "    - Additional properties are not allowed ('bogus' was unexpected)";
    let lines = [
        "w does not match the graph schema:".to_owned(),
        "  at /nodes/a/when: {'bogus': 1} is not valid under any of the given schemas".to_owned(),
        format!("{unexpected}\n    - 'all' is a required property"),
        format!("{unexpected}\n    - 'any' is a required property"),
        format!("{unexpected}\n    - 'not' is a required property"),
        format!(
            "{unexpected}\n    - 'node' is a required property\n    - 'is' is a required property"
        ),
        format!(
            "{unexpected}\n    - 'node' is a required property\n    - 'in' is a required property"
        ),
        format!(
            "{unexpected}\n    - 'node' is a required property\n    - 'abstained' is a required property"
        ),
        format!(
            "{unexpected}\n    - 'node' is a required property\n    - 'confidence_below' is a required property"
        ),
    ];
    assert_eq!(refusal(&raw), lines.join("\n"));
}

#[test]
fn the_first_problem_of_a_graph_document_is_the_one_python_names() {
    let cases = [
        (
            json!({"nodes": {"a": {"code": "a", "when": {"bogus": 1}}}}),
            "Additional properties are not allowed ('bogus' was unexpected) (at /nodes/a/when)",
        ),
        (
            json!({"nodes": {"a": {"code": "a", "when": {"node": "a", "is": 1.5}}}}),
            "1.5 is not of type 'string', 'boolean', 'integer' (at /nodes/a/when/is)",
        ),
        (
            json!({"nodes": {"a": {"code": "a", "when": {"node": "a"}}}}),
            "'is' is a required property (at /nodes/a/when)",
        ),
        (
            json!({"nodes": {"a": {"code": "a", "when": {"all": []}}}}),
            "[] should be non-empty (at /nodes/a/when/all)",
        ),
        (
            json!({"nodes": {"a": {"code": "a", "when": {"all": [{"node": 1, "is": 2}]}}}}),
            "1 is not of type 'string' (at /nodes/a/when/all/0/node)",
        ),
        (
            json!({"nodes": {"a": {"bogus": 1}}}),
            "Additional properties are not allowed ('bogus' was unexpected) (at /nodes/a)",
        ),
        (
            json!({"nodes": {"a": {"code": 1, "after": "x", "context": {"c": {"p": 1}}}}}),
            "'x' is not of type 'array' (at /nodes/a/after)",
        ),
        (
            json!({"nodes": {"a": {"code": "a", "when": {"node": "a", "in": []}}}}),
            "[] should be non-empty (at /nodes/a/when/in)",
        ),
        (
            json!({"nodes": {"a": {"code": "a", "when": {"not": {"node": "a", "abstained": 1}}}}}),
            "1 is not of type 'boolean' (at /nodes/a/when/not/abstained)",
        ),
        (
            json!({"nodes": {"a": {"code": "a", "labels": {"param": 1, "x": 2}}}}),
            "Additional properties are not allowed ('x' was unexpected) (at /nodes/a/labels)",
        ),
    ];
    for (fields, expected) in cases {
        let mut raw = graph_of(json!({}), json!(["a"]), json!([]));
        for (key, value) in fields.as_object().unwrap() {
            raw[key] = value.clone();
        }
        assert_eq!(
            jeff_sdk::contract::first_problem(jeff_sdk::contract::SchemaName::Graph, &raw, None)
                .unwrap(),
            expected,
            "{fields}"
        );
    }
}
