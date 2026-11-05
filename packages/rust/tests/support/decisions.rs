//! Replaying decisions.json: a scripted engine that must be asked exactly the recorded predicts, llm and human steps
//! that record the questions they are shown, and one runner for every op of the fixtures.

use std::collections::{BTreeSet, VecDeque};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use jeff_sdk::binding::{Call, Labels, Row};
use jeff_sdk::engine::{BaseFormat, Engine, EngineResult};
use jeff_sdk::escalation::{Policy, Question, Step, escalate, function_step, human, llm};
use jeff_sdk::msgpack::pack;
use jeff_sdk::{
    BoxError, CallOptions, DispatchOptions, Jeff, Json, JsonMap, Result, ShorthandOptions,
};
use serde_json::json;

use super::{b64, catalogue_for, engine_probabilities, normalised, wire_of};

/// The n-th predict answers the n-th recorded predict, and must be asked for exactly its adapter and rows (compared
/// as MessagePack bytes).
pub struct ScriptedEngine {
    served: BTreeSet<String>,
    base_format: BaseFormat,
    script: Mutex<VecDeque<Json>>,
    pub mismatches: Mutex<Vec<String>>,
}

impl ScriptedEngine {
    pub fn new(installed: &Json, base_format: &Json, predicts: &Json) -> ScriptedEngine {
        ScriptedEngine {
            served: installed
                .as_array()
                .unwrap()
                .iter()
                .map(|id| id.as_str().unwrap().to_owned())
                .collect(),
            base_format: BaseFormat::from_json(base_format).unwrap(),
            script: Mutex::new(predicts.as_array().unwrap().iter().cloned().collect()),
            mismatches: Mutex::new(Vec::new()),
        }
    }

    pub fn remaining(&self) -> usize {
        self.script.lock().unwrap().len()
    }
}

#[async_trait]
impl Engine for ScriptedEngine {
    fn base_format(&self) -> BaseFormat {
        self.base_format.clone()
    }

    async fn installed(&self, _refresh: bool) -> Result<BTreeSet<String>> {
        Ok(self.served.clone())
    }

    async fn predict(&self, adapter_id: &str, rows: &[Row]) -> Result<EngineResult> {
        let next = self.script.lock().unwrap().pop_front().unwrap_or_else(|| {
            panic!("the script has no answer left for a predict of {adapter_id}")
        });
        let mut mismatches = self.mismatches.lock().unwrap();
        if next["adapter"] != adapter_id {
            mismatches.push(format!("asked {adapter_id}, recorded {}", next["adapter"]));
        }
        let packed = b64(&pack(&Json::Array(rows.iter().map(Row::to_json).collect())).unwrap());
        if packed != next["rows_msgpack"].as_str().unwrap() {
            mismatches.push(format!("rows of {adapter_id} differ from the recording"));
        }
        Ok(EngineResult {
            probabilities: engine_probabilities(&next),
            timing: next["timing"].as_object().unwrap().clone(),
            input_tokens: None,
        })
    }
}

/// What the llm and human steps were shown, checked against the case's recorded questions as they come.
pub struct Recorder {
    recorded: Vec<Json>,
    pub questions: Mutex<Vec<Json>>,
    pub order_problems: Mutex<Vec<String>>,
}

impl Recorder {
    pub fn new(recorded: &Json) -> Arc<Recorder> {
        Arc::new(Recorder {
            recorded: recorded.as_array().cloned().unwrap_or_default(),
            questions: Mutex::new(Vec::new()),
            order_problems: Mutex::new(Vec::new()),
        })
    }

    fn ask(&self, step: &str, question: &Question) {
        let mut questions = self.questions.lock().unwrap();
        let index = questions.len();
        if let Some(recorded) = self.recorded.get(index) {
            let expected = &recorded["question"];
            let options: Vec<&str> = question
                .option_entries()
                .iter()
                .map(|(key, _)| *key)
                .collect();
            let expected_options: Vec<&str> = expected["options"]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect();
            if options != expected_options {
                self.order_problems.lock().unwrap().push(format!(
                    "question {index}: options {options:?}, recorded {expected_options:?}"
                ));
            }
            let state: Option<Vec<&str>> = question
                .state_entries()
                .map(|entries| entries.iter().map(|(key, _)| *key).collect());
            let expected_state: Option<Vec<&str>> = expected["state"]
                .as_object()
                .map(|map| map.keys().map(String::as_str).collect());
            if state != expected_state {
                self.order_problems.lock().unwrap().push(format!(
                    "question {index}: state {state:?}, recorded {expected_state:?}"
                ));
            }
        }
        questions.push(json!({"step": step, "question": question.to_json()}));
    }
}

fn asker(step: &'static str, answer: String, recorder: &Arc<Recorder>) -> Step {
    let recorder = Arc::clone(recorder);
    let ask = move |question: Question| {
        recorder.ask(step, &question);
        let answer = answer.clone();
        async move { Ok::<String, BoxError>(answer) }
    };
    if step == "llm" { llm(ask) } else { human(ask) }
}

/// A fixture policy as a Policy; any other string is refused by Policy::named, as Jeff refuses it.
pub fn policy_from(spec: &Json, recorder: &Arc<Recorder>) -> Result<Policy> {
    if let Some(name) = spec.as_str() {
        return Policy::named(name);
    }
    let steps = spec["escalate"]
        .as_array()
        .unwrap()
        .iter()
        .map(|step| {
            if let Some(reference) = step.get("function") {
                function_step(reference.as_str().unwrap())
            } else if let Some(answer) = step.get("llm") {
                asker("llm", answer.as_str().unwrap().to_owned(), recorder)
            } else if let Some(answer) = step.get("human") {
                asker("human", answer.as_str().unwrap().to_owned(), recorder)
            } else {
                panic!("unknown escalation step {step}")
            }
        })
        .collect();
    escalate(
        steps,
        spec.get("budget_ms")
            .map(|budget| budget.as_u64().expect("a whole number of milliseconds")),
    )
}

fn optional_context(args: &Json) -> Option<JsonMap> {
    args.get("context").and_then(Json::as_object).cloned()
}

fn optional_labels(args: &Json) -> Result<Option<Labels>> {
    match args.get("labels") {
        None | Some(Json::Null) => Ok(None),
        Some(labels) => Labels::from_json(labels).map(Some),
    }
}

fn decisions_json(decisions: Vec<jeff_sdk::Decision>) -> Result<Json> {
    Ok(Json::Array(
        decisions
            .iter()
            .map(jeff_sdk::Decision::to_json)
            .collect::<Result<Vec<_>>>()?,
    ))
}

pub async fn run_op(jeff: &Jeff, op: &str, args: &Json, recorder: &Arc<Recorder>) -> Result<Json> {
    let policy = policy_from(args.get("policy").unwrap_or(&json!("return")), recorder)?;
    match op {
        "decide" => {
            let calls = args["calls"]
                .as_array()
                .unwrap()
                .iter()
                .map(Call::from_json)
                .collect::<Result<Vec<_>>>()?;
            decisions_json(jeff.decide(&calls).await?)
        }
        "function" => {
            let options = CallOptions {
                context: optional_context(args),
                labels: optional_labels(args)?,
                on_low_confidence: policy,
            };
            let bound = jeff
                .function(args["ref"].as_str().unwrap(), options)
                .await?;
            decisions_json(
                bound
                    .many(args["inputs"].as_array().unwrap().clone())
                    .await?,
            )
        }
        "shorthand" => {
            let options = ShorthandOptions {
                function: args
                    .get("function")
                    .and_then(Json::as_str)
                    .map(str::to_owned),
                context: optional_context(args),
                labels: optional_labels(args)?,
                on_low_confidence: policy,
            };
            let input = args["input"].clone();
            let decision = match args["interface"].as_str().unwrap() {
                "classify" => jeff.classify(input, options).await?,
                "route" => jeff.route(input, options).await?,
                "rate" => jeff.rate(input, options).await?,
                "detect" => jeff.detect(input, options).await?,
                "is_supported" => jeff.is_supported(input, options).await?,
                "equivalent" => jeff.equivalent(input, options).await?,
                "requires_escalation" => jeff.requires_escalation(input, options).await?,
                other => panic!("no shorthand for interface {other}"),
            };
            decision.to_json()
        }
        "dispatch" => {
            let options = DispatchOptions {
                interface: args["interface"].as_str().unwrap().to_owned(),
                router: args.get("router").and_then(Json::as_str).map(str::to_owned),
                context: optional_context(args),
                labels: optional_labels(args)?,
                on_low_confidence: policy,
            };
            jeff.dispatch(args["input"].clone(), options)
                .await?
                .to_json()
        }
        "graph" => {
            let code_calls = Arc::new(Mutex::new(Vec::<Json>::new()));
            let mut code = indexmap::IndexMap::new();
            for (name, value) in args
                .get("code")
                .and_then(Json::as_object)
                .cloned()
                .unwrap_or_default()
            {
                let calls = Arc::clone(&code_calls);
                let recorded_name = name.clone();
                code.insert(
                    name,
                    jeff_sdk::graph::code_function(move |call: jeff_sdk::graph::CodeCall| {
                        let nodes: JsonMap = call
                            .nodes
                            .iter()
                            .map(|(node, result)| (node.clone(), result.to_json().unwrap()))
                            .collect();
                        calls.lock().unwrap().push(json!({"code": recorded_name, "input": call.input, "params": call.params, "nodes": nodes}));
                        let value = value.clone();
                        async move { Ok::<Json, BoxError>(value) }
                    }),
                );
            }
            let mut policies = indexmap::IndexMap::new();
            for (node, spec) in args
                .get("policies")
                .and_then(Json::as_object)
                .cloned()
                .unwrap_or_default()
            {
                policies.insert(node, policy_from(&spec, recorder)?);
            }
            let graph = jeff_sdk::graph::graph_from_json(&args["graph"], "the conformance graph")?;
            let params = args["params"].as_object().unwrap().clone();
            let result = jeff
                .run_graph(graph, args["input"].clone(), params, code, policies)
                .await?;
            let calls = Json::Array(code_calls.lock().unwrap().clone());
            Ok(json!({"graph_result": result.to_json()?, "code_calls": calls}))
        }
        other => panic!("run_op does not know op {other}"),
    }
}

/// Replays one case of decisions.json; the problems found (empty when the case reproduces).
pub async fn replay_decision_case(case: &Json) -> Vec<String> {
    let engine = Arc::new(ScriptedEngine::new(
        &case["installed"],
        &case["base_format"],
        &case["predicts"],
    ));
    let recorder = Recorder::new(&case["questions"]);
    let as_engine: Arc<dyn Engine> = engine.clone();
    let got = match Jeff::new(
        as_engine,
        catalogue_for(case["catalogue"].as_str().unwrap()),
    )
    .await
    {
        Err(error) => json!({"error": wire_of(&error)}),
        Ok(jeff) => match run_op(
            &jeff,
            case["op"].as_str().unwrap(),
            &case["args"],
            &recorder,
        )
        .await
        {
            Ok(result) => json!({"result": normalised(&result)}),
            Err(error) => json!({"error": wire_of(&error)}),
        },
    };
    let mut problems = Vec::new();
    if got != case["expect"] {
        problems.push(format!("got      {got}\n  expected {}", case["expect"]));
    }
    problems.extend(engine.mismatches.lock().unwrap().iter().cloned());
    if engine.remaining() != 0 {
        problems.push(format!(
            "{} recorded predicts were never asked",
            engine.remaining()
        ));
    }
    let questions = Json::Array(recorder.questions.lock().unwrap().clone());
    if questions != *case.get("questions").unwrap_or(&json!([])) {
        problems.push(format!(
            "questions {questions}, recorded {}",
            case["questions"]
        ));
    }
    problems.extend(recorder.order_problems.lock().unwrap().iter().cloned());
    problems
}
