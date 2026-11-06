//! Local mode with a list of adapters (contract/local-engine.md), against a fake `uvx`: a shell script this test
//! writes into a temporary folder it gives as the PATH. The script records its arguments and process ID, then plays
//! the engine: it sleeps (the test starts its stub engine on the recorded socket), exits, or hangs. HOME is a short
//! temporary folder (a Unix socket path is limited to about 104 bytes on macOS). No real uv or engine is ever run.

mod support;

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use jeff_sdk::binding::Call;
use jeff_sdk::catalogue::Catalogue;
use jeff_sdk::local_engine::{
    Environment, SDK_VERSION, exited_error, local_adapters_in, lock_timeout_error,
    resolve_adapters, start_timeout_error, starting_line, waiting_line,
};
use jeff_sdk::manifest::manifest_from_json;
use jeff_sdk::{ErrorType, Jeff, JeffError, Json, LocalOptions, StartOptions, Value};
use serde_json::json;
use support::engine_server::{EngineServer, EngineServerOptions, answering, map, start};

/// The fake engine stays up: `exec` keeps the recorded process ID.
const SERVE: &str = "exec sleep 60";
/// The fake engine writes a few lines and exits with status 3.
const EXIT: &str = "echo 'warning: something'\necho 'ManifestError: bad' >&2\nexit 3";
/// The fake engine is ended by a signal (SIGTERM, status 143).
const KILLED: &str = "echo 'about to stop'\nkill -TERM $$\nsleep 5";
/// The fake engine never listens: it writes one line, starts a second process in its group and sleeps.
const HANG: &str = "echo 'Fetching 9%'\nsleep 60 &\necho $! > \"$RECORD/child\"\nexec sleep 60";

struct Sandbox {
    home: tempfile::TempDir,
    bin: PathBuf,
    record: PathBuf,
    notices: Arc<Mutex<Vec<String>>>,
}

impl Sandbox {
    fn new(engine: &str) -> Sandbox {
        let home = tempfile::Builder::new()
            .prefix("jl")
            .tempdir_in("/tmp")
            .unwrap();
        let bin = home.path().join("bin");
        let record = home.path().join("record");
        std::fs::create_dir_all(&bin).unwrap();
        std::fs::create_dir_all(&record).unwrap();
        let script = format!(
            "#!/bin/sh\nRECORD='{}'\necho $$ >> \"$RECORD/pids\"\nps -o pgid= -p $$ | tr -d ' ' > \"$RECORD/pgid\"\nprintf '%s\\n' \"$0\" \"$@\" > \"$RECORD/args\"\necho run >> \"$RECORD/runs\"\n{engine}\n",
            record.display()
        );
        let uvx = bin.join("uvx");
        std::fs::write(&uvx, script).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&uvx, std::fs::Permissions::from_mode(0o755)).unwrap();
        Sandbox {
            home,
            bin,
            record,
            notices: Arc::default(),
        }
    }

    fn environment(&self) -> Environment {
        let notices = Arc::clone(&self.notices);
        Environment {
            home: Some(self.home.path().display().to_string()),
            path: Some(self.bin.display().to_string()),
            xdg_cache_home: None,
            os: "macos".into(),
            arch: "arm64".into(),
            nvidia: false,
            notice: Arc::new(move |line: &str| notices.lock().unwrap().push(line.to_owned())),
        }
    }

    fn folder(&self) -> PathBuf {
        self.home.path().join("Library/Caches/jeff/engine")
    }

    fn socket(&self) -> PathBuf {
        self.folder().join("engine.sock")
    }

    fn uvx(&self) -> PathBuf {
        self.bin.join("uvx")
    }

    fn runs(&self) -> usize {
        std::fs::read_to_string(self.record.join("runs"))
            .map(|text| text.lines().count())
            .unwrap_or(0)
    }

    fn recorded(&self, name: &str) -> Vec<String> {
        std::fs::read_to_string(self.record.join(name))
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    fn notices(&self) -> Vec<String> {
        self.notices.lock().unwrap().clone()
    }

    /// Stops every fake engine process the test started (each is the leader of its own process group).
    fn stop_engines(&self) {
        if let Ok(pids) = std::fs::read_to_string(self.record.join("pids")) {
            for pid in pids.lines() {
                let pid: i32 = pid.parse().unwrap();
                unsafe { libc::kill(-pid, libc::SIGTERM) };
            }
        }
    }
}

fn options(start_timeout_ms: u64) -> StartOptions {
    StartOptions {
        timeout_ms: 5000,
        start_timeout_ms,
    }
}

fn references(adapters: &[&str]) -> Vec<String> {
    adapters
        .iter()
        .map(|adapter| (*adapter).to_owned())
        .collect()
}

fn level_of(_adapter: &str, row: &Json) -> Json {
    let message = row["state"]["message"].as_str().unwrap();
    let level: usize = message[message.len() - 1..].parse().unwrap();
    Json::from(
        (0..5)
            .map(|index| if index == level { 1.0 } else { 0.0 })
            .collect::<Vec<f64>>(),
    )
}

fn engine_options(socket: &Path, installed: &[&str]) -> EngineServerOptions {
    EngineServerOptions {
        path: Some(socket.to_owned()),
        ..answering(level_of, installed)
    }
}

/// Plays the engine the fake uvx stands for: once it has run, starts the stub engine on the `--socket` it was given.
fn serve_when_started(
    sandbox: &Sandbox,
    installed: &'static [&'static str],
) -> tokio::task::JoinHandle<EngineServer> {
    let record = sandbox.record.clone();
    tokio::spawn(async move {
        let runs = record.join("runs");
        let began = Instant::now();
        while !runs.exists() {
            assert!(
                began.elapsed() < Duration::from_secs(10),
                "the fake uvx never ran"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let args: Vec<String> = std::fs::read_to_string(record.join("args"))
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect();
        let at = args.iter().position(|arg| arg == "--socket").unwrap();
        start(engine_options(Path::new(&args[at + 1]), installed)).await
    })
}

fn process_running(pid: i32) -> bool {
    unsafe { libc::kill(pid, 0) == 0 }
}

async fn ended_within(pid: i32, limit: Duration) -> bool {
    let began = Instant::now();
    while process_running(pid) {
        if began.elapsed() > limit {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    true
}

fn assert_error(error: &JeffError, expected: &JeffError) {
    assert_eq!(error.type_name(), expected.type_name());
    assert_eq!(error.message(), expected.message());
}

async fn urgency_of(jeff: &Jeff, message: &str) -> Value {
    let context = map(json!({"company": "A shop.", "channel": "email"}));
    jeff.decide(&[Call::new("triage.urgency", json!(message)).with_context(context)])
        .await
        .unwrap()[0]
        .value
        .clone()
}

#[tokio::test]
async fn starts_the_engine_then_every_later_client_reuses_it() {
    let sandbox = Sandbox::new(SERVE);
    let environment = sandbox.environment();
    let server = serve_when_started(&sandbox, &["jeff.support.triage"]);
    let jeff = local_adapters_in(&references(&["triage"]), options(20_000), &environment)
        .await
        .unwrap();
    let server = server.await.unwrap();
    // the engine leads a process group of its own, not this program's
    let pid = std::fs::read_to_string(sandbox.record.join("pids")).unwrap();
    let pgid = std::fs::read_to_string(sandbox.record.join("pgid")).unwrap();
    assert_eq!(pgid.trim(), pid.lines().next().unwrap().trim());
    let folder = sandbox.folder();
    let mut expected = vec![
        sandbox.uvx().display().to_string(),
        "--from".into(),
        format!("jeff-runtime[mac]=={SDK_VERSION}"),
    ];
    expected.extend(
        [
            "jeff",
            "runtime",
            "serve",
            "--socket",
            &sandbox.socket().display().to_string(),
            "--base-version",
            "jeff-base@v1.4",
            "--adapter",
            "jeff.support.triage",
            "--batch-window-ms",
            "0.1",
            "--max-batch-rows",
            "64",
            "--queue-limit-rows",
            "4096",
            "--prepare-workers",
            "2",
            "--exit-when-unused-s",
            "300",
        ]
        .map(str::to_owned),
    );
    assert_eq!(SDK_VERSION, "1.4.1");
    assert_eq!(sandbox.recorded("args"), expected);
    assert_eq!(
        sandbox.notices(),
        vec![starting_line(
            &references(&["jeff.support.triage"]),
            &folder.join("engine.log")
        )]
    );
    assert!(!folder.join("engine.lock").exists());
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        std::fs::metadata(&folder).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(urgency_of(&jeff, "level 3").await, Value::Int(3));

    // a second client, while the first is open, and another after it closed: both use the running engine
    let second = local_adapters_in(
        &references(&["jeff.support.triage"]),
        options(20_000),
        &environment,
    )
    .await
    .unwrap();
    assert_eq!(urgency_of(&second, "level 1").await, Value::Int(1));
    jeff.close().await;
    second.close().await;
    let third = local_adapters_in(&references(&["triage"]), options(20_000), &environment)
        .await
        .unwrap();
    assert_eq!(urgency_of(&third, "level 4").await, Value::Int(4));
    third.close().await;
    assert_eq!(sandbox.runs(), 1);
    assert_eq!(sandbox.notices().len(), 1);
    server.close();
    sandbox.stop_engines();
}

#[tokio::test]
async fn a_running_engine_lacking_an_adapter_is_asked_to_serve_it_and_its_refusal_is_not_installed()
{
    let sandbox = Sandbox::new(SERVE);
    std::fs::create_dir_all(sandbox.folder()).unwrap();
    let refusal = json!({"type": "UnknownFunction", "message": "not available to serve from this engine: jeff.safety.spam"});
    let server = start(EngineServerOptions {
        serve_refusal: Some(refusal),
        ..engine_options(&sandbox.socket(), &["jeff.support.triage"])
    })
    .await;
    let error = local_adapters_in(
        &references(&["triage", "spam"]),
        options(20_000),
        &sandbox.environment(),
    )
    .await
    .unwrap_err();
    assert_eq!(error.kind(), ErrorType::NotInstalled);
    assert_eq!(
        error.message(),
        format!(
            "The Jeff engine already running at {} does not serve jeff.safety.spam, and refused to load them: \
             UnknownFunction: not available to serve from this engine: jeff.safety.spam. Start an engine yourself with \
             every adapter you need (jeff runtime serve) and give its socket instead of the adapters; the shared engine \
             stops by itself 5 minutes after the last program using it closes",
            sandbox.socket().display()
        )
    );
    let serves: Vec<Json> = server
        .received
        .lock()
        .unwrap()
        .iter()
        .filter(|message| message["op"] == "serve")
        .cloned()
        .collect();
    assert_eq!(serves.len(), 1);
    assert_eq!(
        serves[0]["adapters"],
        json!(["jeff.support.triage", "jeff.safety.spam"])
    );
    assert_eq!(sandbox.runs(), 0);
    assert!(sandbox.notices().is_empty());
    server.close();
}

#[tokio::test]
async fn a_running_engine_that_accepts_the_serve_is_used() {
    let sandbox = Sandbox::new(SERVE);
    std::fs::create_dir_all(sandbox.folder()).unwrap();
    let server = start(engine_options(&sandbox.socket(), &["jeff.support.triage"])).await;
    let jeff = local_adapters_in(
        &references(&["spam", "triage"]),
        options(20_000),
        &sandbox.environment(),
    )
    .await
    .unwrap();
    let serves: Vec<Json> = server
        .received
        .lock()
        .unwrap()
        .iter()
        .filter(|message| message["op"] == "serve")
        .cloned()
        .collect();
    assert_eq!(serves.len(), 1);
    assert_eq!(
        serves[0]["adapters"],
        json!(["jeff.support.triage", "jeff.safety.spam"])
    );
    assert_eq!(urgency_of(&jeff, "level 2").await, Value::Int(2));
    jeff.close().await;
    assert_eq!(sandbox.runs(), 0);
    server.close();
}

#[tokio::test]
async fn without_uvx_it_is_runtime_missing() {
    let sandbox = Sandbox::new(SERVE);
    let mut environment = sandbox.environment();
    environment.path = Some(sandbox.record.display().to_string()); // a folder with no uvx
    let error = local_adapters_in(&references(&["guard"]), options(20_000), &environment)
        .await
        .unwrap_err();
    let home = sandbox.home.path().display();
    assert_eq!(error.kind(), ErrorType::RuntimeMissing);
    assert_eq!(
        error.message(),
        format!(
            "Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at {home}/.local/bin/uvx or \
             {home}/.cargo/bin/uvx. Install uv once with: curl -LsSf https://astral.sh/uv/install.sh | sh (see \
             https://docs.astral.sh/uv/). Or start an engine yourself (jeff runtime serve) and give its socket \
             instead of the adapters"
        )
    );
    assert_eq!(sandbox.runs(), 0);
}

#[tokio::test]
async fn an_unsupported_machine_is_refused_before_anything_is_looked_for_or_run() {
    let sandbox = Sandbox::new(SERVE);
    let mut environment = sandbox.environment();
    environment.os = "linux".into();
    environment.arch = "x86_64".into();
    let error = local_adapters_in(&references(&["guard"]), options(20_000), &environment)
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::BackendUnavailable);
    assert!(
        error
            .message()
            .contains("this Linux machine has no NVIDIA GPU")
    );
    assert_eq!(sandbox.runs(), 0);
    assert!(!sandbox.home.path().join(".cache").exists());
}

#[tokio::test]
async fn the_options_are_checked_first() {
    let sandbox = Sandbox::new(SERVE);
    let error = local_adapters_in(&references(&["nope"]), options(0), &sandbox.environment())
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::InvalidRequest);
    assert_eq!(
        error.message(),
        "start_timeout_ms must be a whole number of milliseconds above 0; got 0"
    );
    let empty: [&str; 0] = [];
    let error = Jeff::local_adapters(empty, StartOptions::default())
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::ManifestError);
}

#[tokio::test]
async fn the_socket_form_never_starts_an_engine() {
    let sandbox = Sandbox::new(SERVE);
    let error = Jeff::local(sandbox.socket(), LocalOptions::new(2000))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorType::EngineError);
    assert!(error.message().starts_with(&format!(
        "No engine is listening at {}",
        sandbox.socket().display()
    )));
    assert_eq!(sandbox.runs(), 0);
    assert!(!sandbox.home.path().join("Library").exists());
}

#[tokio::test]
async fn an_engine_that_does_not_answer_within_the_start_timeout_is_stopped() {
    let sandbox = Sandbox::new(HANG);
    let began = Instant::now();
    let error = local_adapters_in(
        &references(&["guard"]),
        options(4000),
        &sandbox.environment(),
    )
    .await
    .unwrap_err();
    assert!(began.elapsed() >= Duration::from_millis(4000));
    let folder = sandbox.folder();
    assert_error(
        &error,
        &start_timeout_error(
            &sandbox.socket(),
            4000,
            &folder.join("engine.log"),
            "Fetching 9%\n",
        ),
    );
    // SIGTERM went to the whole process group: the engine and the process it started
    let engine: i32 = sandbox.recorded("pids")[0].parse().unwrap();
    let child: i32 = sandbox.recorded("child")[0].parse().unwrap();
    assert!(ended_within(engine, Duration::from_secs(5)).await);
    assert!(ended_within(child, Duration::from_secs(5)).await);
    assert!(!folder.join("engine.lock").exists());
}

#[tokio::test]
async fn an_engine_that_exits_while_starting_is_an_engine_error_with_its_status_and_log() {
    let sandbox = Sandbox::new(EXIT);
    let error = local_adapters_in(
        &references(&["guard"]),
        options(20_000),
        &sandbox.environment(),
    )
    .await
    .unwrap_err();
    let log = sandbox.folder().join("engine.log");
    assert_error(
        &error,
        &exited_error(3, &log, "warning: something\nManifestError: bad\n"),
    );
    assert!(!sandbox.folder().join("engine.lock").exists());

    let killed = Sandbox::new(KILLED);
    let error = local_adapters_in(
        &references(&["guard"]),
        options(20_000),
        &killed.environment(),
    )
    .await
    .unwrap_err();
    assert_error(
        &error,
        &exited_error(143, &killed.folder().join("engine.log"), "about to stop\n"),
    );
}

/// A process that has ended (its ID, once reaped, belongs to no running process).
fn dead_pid() -> u32 {
    let mut child = std::process::Command::new("/usr/bin/true").spawn().unwrap();
    let pid = child.id();
    child.wait().unwrap();
    pid
}

#[tokio::test]
async fn a_stale_lock_is_taken_over() {
    let sandbox = Sandbox::new(SERVE);
    std::fs::create_dir_all(sandbox.folder()).unwrap();
    let lock = sandbox.folder().join("engine.lock");
    std::fs::write(&lock, format!("{}\n", dead_pid())).unwrap();
    let server = serve_when_started(&sandbox, &["jeff.support.triage"]);
    let jeff = local_adapters_in(
        &references(&["triage"]),
        options(20_000),
        &sandbox.environment(),
    )
    .await
    .unwrap();
    let server = server.await.unwrap();
    assert_eq!(sandbox.runs(), 1);
    assert_eq!(sandbox.notices().len(), 1); // the starting line, no waiting line
    assert!(!lock.exists());
    jeff.close().await;
    server.close();
    sandbox.stop_engines();
}

struct Holder(std::process::Child);

impl Drop for Holder {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn live_holder() -> Holder {
    Holder(
        std::process::Command::new("/bin/sleep")
            .arg("60")
            .spawn()
            .unwrap(),
    )
}

#[tokio::test]
async fn a_live_lock_is_waited_for_and_the_engine_its_holder_started_is_used() {
    let sandbox = Sandbox::new(SERVE);
    let folder = sandbox.folder();
    std::fs::create_dir_all(&folder).unwrap();
    let holder = live_holder();
    let lock = folder.join("engine.lock");
    std::fs::write(&lock, format!("{}\n", holder.0.id())).unwrap();
    let (socket, released) = (sandbox.socket(), lock.clone());
    let other_program = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(700)).await;
        let server = start(engine_options(&socket, &["jeff.support.triage"])).await;
        std::fs::remove_file(&released).unwrap();
        server
    });
    let jeff = local_adapters_in(
        &references(&["triage"]),
        options(20_000),
        &sandbox.environment(),
    )
    .await
    .unwrap();
    assert_eq!(
        sandbox.notices(),
        vec![waiting_line(&folder.join("engine.log"))]
    );
    assert_eq!(sandbox.runs(), 0);
    assert_eq!(urgency_of(&jeff, "level 0").await, Value::Int(0));
    jeff.close().await;
    other_program.await.unwrap().close();
}

#[tokio::test]
async fn a_lock_held_past_the_start_timeout_is_an_engine_error() {
    let sandbox = Sandbox::new(SERVE);
    let folder = sandbox.folder();
    std::fs::create_dir_all(&folder).unwrap();
    let holder = live_holder();
    let lock = folder.join("engine.lock");
    std::fs::write(&lock, format!("{}\n", holder.0.id())).unwrap();
    let error = local_adapters_in(
        &references(&["guard"]),
        options(600),
        &sandbox.environment(),
    )
    .await
    .unwrap_err();
    assert_error(
        &error,
        &lock_timeout_error(
            600,
            &lock,
            &holder.0.id().to_string(),
            &folder.join("engine.log"),
        ),
    );
    assert_eq!(
        sandbox.notices(),
        vec![waiting_line(&folder.join("engine.log"))]
    );
    assert_eq!(sandbox.runs(), 0);
    assert!(lock.exists()); // not this program's to remove
}

#[test]
fn a_proprietary_adapter_is_refused() {
    let shipped = Catalogue::shipped().unwrap();
    let mut raw = jeff_sdk::yaml::read_manifest_yaml_file(
        &jeff_sdk::shipped::repository_root().join("catalogue/jeff.safety.guard/manifest.yaml"),
    )
    .unwrap();
    raw["weights"] = json!({"proprietary": {"repo": "teamsuzie/jeff-adapters", "path": "guard"}});
    let manifest = Arc::new(manifest_from_json(&raw, "proprietary guard").unwrap());
    let catalogue = shipped.replacing(vec![manifest]).unwrap();
    let error = resolve_adapters(&catalogue, &references(&["triage", "guard"])).unwrap_err();
    assert_eq!(error.kind(), ErrorType::ProprietaryWeights);
    assert!(
        error
            .message()
            .starts_with("jeff.safety.guard is proprietary")
    );
}
