//! local_engine.json: local mode's engine start (contract/local-engine.md), its pure parts: the platform's extra, the
//! engine folder, finding uvx, resolving the adapters, the command, the log's last lines and every message.

mod support;

use std::path::{Path, PathBuf};

use jeff_sdk::local_engine::{
    choose_extra, different_bases_error, engine_command, engine_folder, exited_error, find_uvx,
    home_missing_error, lock_timeout_error, log_tail, not_loadable_error, proprietary_error,
    resolve_adapters, start_timeout_error, starting_line, waiting_line,
};
use jeff_sdk::{Catalogue, ErrorType, JeffError, Json};
use support::fixture;

fn cases(section: &str) -> &'static Vec<Json> {
    fixture("local_engine.json")[section]
        .as_array()
        .unwrap_or_else(|| panic!("local_engine.json has no {section}"))
}

fn text(value: &Json) -> &str {
    value.as_str().unwrap()
}

fn texts(value: &Json) -> Vec<String> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|item| text(item).to_owned())
        .collect()
}

fn optional(value: &Json) -> Option<&str> {
    value.as_str()
}

fn assert_error(case: &Json, error: &JeffError) {
    let expected = &case["error"];
    assert_eq!(error.type_name(), text(&expected["type"]), "{case}");
    assert_eq!(error.message(), text(&expected["message"]), "{case}");
}

#[test]
fn platforms() {
    for case in cases("platforms") {
        let chosen = choose_extra(
            text(&case["os"]),
            text(&case["arch"]),
            case["nvidia"].as_bool().unwrap(),
        );
        match case.get("extra") {
            Some(extra) => assert_eq!(chosen.unwrap(), text(extra), "{case}"),
            None => assert_error(case, &chosen.unwrap_err()),
        }
    }
}

#[test]
fn folders() {
    for case in cases("folders") {
        let folder = engine_folder(
            text(&case["os"]),
            optional(&case["home"]),
            optional(&case["xdg_cache_home"]),
        );
        match case.get("folder") {
            Some(expected) => assert_eq!(folder.unwrap(), PathBuf::from(text(expected)), "{case}"),
            None => assert_error(case, &folder.unwrap_err()),
        }
    }
}

#[test]
fn uvx() {
    for case in cases("uvx") {
        let executables: Vec<PathBuf> = texts(&case["executables"])
            .iter()
            .map(PathBuf::from)
            .collect();
        let found = find_uvx(
            Some(text(&case["path"])),
            text(&case["home"]),
            |path: &Path| executables.iter().any(|executable| executable == path),
        );
        match case.get("found") {
            Some(expected) => assert_eq!(found.unwrap(), PathBuf::from(text(expected)), "{case}"),
            None => assert_error(case, &found.unwrap_err()),
        }
    }
}

#[test]
fn adapters() {
    let catalogue = Catalogue::shipped().unwrap();
    for case in cases("adapters") {
        let resolved = resolve_adapters(&catalogue, &texts(&case["adapters"]));
        match case.get("adapter_ids") {
            Some(ids) => {
                let (adapter_ids, base) = resolved.unwrap();
                assert_eq!(adapter_ids, texts(ids), "{case}");
                assert_eq!(base, text(&case["base"]), "{case}");
            }
            None => assert_error(case, &resolved.unwrap_err()),
        }
    }
}

#[test]
fn commands() {
    let catalogue = Catalogue::shipped().unwrap();
    for case in cases("commands") {
        let (adapter_ids, base) = resolve_adapters(&catalogue, &texts(&case["adapters"])).unwrap();
        let argv = engine_command(
            Path::new(text(&case["uvx"])),
            text(&case["extra"]),
            text(&case["version"]),
            Path::new(text(&case["folder"])),
            &adapter_ids,
            &base,
        );
        assert_eq!(argv, texts(&case["argv"]), "{}", case["name"]);
    }
}

#[test]
fn log_tails() {
    for case in cases("log_tails") {
        assert_eq!(
            log_tail(text(&case["log"])),
            text(&case["tail"]),
            "{}",
            case["name"]
        );
    }
}

#[test]
fn messages() {
    for case in cases("messages") {
        let path = |key: &str| PathBuf::from(text(&case[key]));
        match text(&case["kind"]) {
            "starting" => assert_eq!(
                starting_line(&texts(&case["adapter_ids"]), &path("log_path")),
                text(&case["text"])
            ),
            "waiting" => assert_eq!(waiting_line(&path("log_path")), text(&case["text"])),
            "exited" => assert_error(
                case,
                &exited_error(
                    case["status"].as_i64().unwrap() as i32,
                    &path("log_path"),
                    text(&case["log"]),
                ),
            ),
            "start_timeout" => assert_error(
                case,
                &start_timeout_error(
                    &path("socket_path"),
                    case["start_timeout_ms"].as_u64().unwrap(),
                    &path("log_path"),
                    text(&case["log"]),
                ),
            ),
            "lock_timeout" => assert_error(
                case,
                &lock_timeout_error(
                    case["start_timeout_ms"].as_u64().unwrap(),
                    &path("lock_path"),
                    &case["pid"].as_u64().unwrap().to_string(),
                    &path("log_path"),
                ),
            ),
            "not_loadable" => {
                let refusal = &case["engine_error"];
                let refusal = JeffError::new(
                    ErrorType::from_name(text(&refusal["type"])).unwrap(),
                    text(&refusal["message"]),
                );
                assert_error(
                    case,
                    &not_loadable_error(&path("socket_path"), &texts(&case["missing"]), &refusal),
                );
            }
            "proprietary" => assert_error(case, &proprietary_error(text(&case["adapter_id"]))),
            "home_missing" => assert_error(case, &home_missing_error()),
            "different_bases" => assert_error(case, &different_bases_error(&texts(&case["bases"]))),
            // Only Java and C# start the engine through a shell script, whose group check gives status 125; Rust
            // starts it in its own group with process_group(0) (the local_engine tests check the group).
            "not_own_group" => {}
            other => panic!("local_engine.json: a message of kind {other} this test does not know"),
        }
    }
}
