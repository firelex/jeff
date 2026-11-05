//! remote.json: the exact HTTP requests the Python SDK sends to a Jeff server, and what it makes of the recorded
//! answers. Each recorded answer is served only after the request it answers is the recorded one.

mod support;

use std::sync::{Arc, Mutex};

use jeff_sdk::remote::{RemoteOptions, TokenSource};
use jeff_sdk::{Jeff, JeffError, Json};
use serde_json::json;
use support::decisions::{Recorder, run_op};
use support::http_server::{Reply, Request, serve};
use support::{catalogue_for, fixture, normalised, wire_of};

fn seen(request: &Request) -> Json {
    let body = if request.body.is_empty() {
        Json::Null
    } else {
        serde_json::from_slice(&request.body).unwrap()
    };
    json!({"method": request.method, "path": request.path,
        "authorization": request.header("authorization"), "body": body})
}

fn with_id(error: &JeffError) -> Json {
    let mut wire = wire_of(error);
    wire["request_id"] = json!(error.request_id());
    normalised(&wire)
}

#[tokio::test]
async fn every_remote_case_sends_the_recorded_requests_and_reads_the_recorded_answers() {
    let mut failures = Vec::new();
    for case in fixture("remote.json")["cases"].as_array().unwrap() {
        let exchanges = case["exchanges"].as_array().unwrap().clone();
        let sent: Arc<Mutex<Vec<Json>>> = Default::default();
        let next = Arc::new(Mutex::new(0usize));
        let server = {
            let (sent, exchanges, next) = (Arc::clone(&sent), exchanges.clone(), Arc::clone(&next));
            serve(move |request| {
                let got = seen(request);
                sent.lock().unwrap().push(got.clone());
                let mut index = next.lock().unwrap();
                let Some(exchange) = exchanges.get(*index) else {
                    return Reply::Full {
                        status: 599,
                        headers: vec![],
                        body: "no recorded answer left".into(),
                    };
                };
                *index += 1;
                if got != exchange["request"] {
                    return Reply::Full {
                        status: 599,
                        headers: vec![],
                        body: "this request is not the recorded one".into(),
                    };
                }
                let response = &exchange["response"];
                Reply::Full {
                    status: response["status"].as_u64().unwrap() as u16,
                    headers: response["headers"]
                        .as_object()
                        .unwrap()
                        .iter()
                        .map(|(name, value)| (name.clone(), value.as_str().unwrap().to_owned()))
                        .collect(),
                    body: response["body"].as_str().unwrap().to_owned(),
                }
            })
            .await
        };
        let token = match case["token"].as_str() {
            Some(token) => TokenSource::fixed(token),
            None => TokenSource::None,
        };
        let options = RemoteOptions {
            catalogue: Some(catalogue_for(case["catalogue"].as_str().unwrap())),
            ..RemoteOptions::new(10000)
        };
        let recorder = Recorder::new(&case["questions"]);
        let mut results = Vec::new();
        let connect = match Jeff::remote(&server.url, token, options).await {
            Err(error) => json!({"error": with_id(&error)}),
            Ok(jeff) => {
                for action in case["actions"].as_array().unwrap() {
                    results.push(
                        match run_op(
                            &jeff,
                            action["op"].as_str().unwrap(),
                            &action["args"],
                            &recorder,
                        )
                        .await
                        {
                            Ok(result) => json!({"result": normalised(&result)}),
                            Err(error) => json!({"error": with_id(&error)}),
                        },
                    );
                }
                jeff.close().await;
                json!({"ok": true})
            }
        };
        server.close();
        let requests: Vec<Json> = exchanges
            .iter()
            .map(|exchange| exchange["request"].clone())
            .collect();
        let questions = Json::Array(recorder.questions.lock().unwrap().clone());
        let problems: Vec<String> = [
            (Json::Array(sent.lock().unwrap().clone()) != Json::Array(requests.clone())).then(
                || {
                    format!(
                        "requests {:?}\n  recorded {requests:?}",
                        sent.lock().unwrap()
                    )
                },
            ),
            (connect != case["connect"])
                .then(|| format!("connect {connect}, recorded {}", case["connect"])),
            (Json::Array(results.clone()) != case["results"]).then(|| {
                format!(
                    "results {}\n  recorded {}",
                    Json::Array(results.clone()),
                    case["results"]
                )
            }),
            (questions != case["questions"])
                .then(|| format!("questions {questions}, recorded {}", case["questions"])),
        ]
        .into_iter()
        .flatten()
        .collect();
        if !problems.is_empty() {
            failures.push(format!("{}\n  {}", case["name"], problems.join("\n  ")));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
