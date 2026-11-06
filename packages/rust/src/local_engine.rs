//! Local mode with a list of adapters: find the Jeff engine running for this user on this machine, or start one
//! through uv's `uvx` (contract/local-engine.md, which this follows step by step). The pure parts (the platform's
//! extra, the engine folder, finding uvx, resolving the adapters, the command, the log's last lines and every message)
//! are functions of their inputs, so contract/conformance/local_engine.json can test them directly; the environment
//! (HOME, PATH, XDG_CACHE_HOME, the platform, where the printed lines go) is an [`Environment`], the process's own
//! outside tests.

use std::collections::BTreeSet;
use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use crate::catalogue::Catalogue;
use crate::client::{Jeff, StartOptions, check_milliseconds};
use crate::errors::{ErrorType, JeffError, Result};
use crate::socket_engine::SocketEngine;

/// The version of jeff-runtime the engine is started from: this crate's own.
pub const SDK_VERSION: &str = env!("CARGO_PKG_VERSION");

/// How often the start lock and the engine's socket are tried while waiting.
const POLL: Duration = Duration::from_millis(200);

/// A lock file holding no process ID is stale once it is older than this.
const LOCK_WITHOUT_PID_STALE: Duration = Duration::from_secs(10);

/// How many of the log's last lines an error shows.
const TAIL_LINES: usize = 20;

/// What local mode reads from the machine it runs on. [`Environment::from_process`] is the process's own; tests build
/// one by hand.
#[derive(Clone)]
pub struct Environment {
    /// `HOME` (None when not set).
    pub home: Option<String>,
    /// `PATH` (None when not set).
    pub path: Option<String>,
    /// `XDG_CACHE_HOME` (None when not set).
    pub xdg_cache_home: Option<String>,
    /// The operating system as the contract names it: `macos`, `linux`, `windows`, or the language's name for another.
    pub os: String,
    /// The processor as the contract names it: `arm64`, `x86_64`, or the language's name for another.
    pub arch: String,
    /// Whether an NVIDIA GPU was found (only looked for on Linux).
    pub nvidia: bool,
    /// Where the lines local mode prints go (standard error outside tests).
    pub notice: Arc<dyn Fn(&str) + Send + Sync>,
}

impl std::fmt::Debug for Environment {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Environment")
            .field("home", &self.home)
            .field("path", &self.path)
            .field("xdg_cache_home", &self.xdg_cache_home)
            .field("os", &self.os)
            .field("arch", &self.arch)
            .field("nvidia", &self.nvidia)
            .finish_non_exhaustive()
    }
}

fn variable(name: &str) -> Option<String> {
    std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
}

impl Environment {
    /// The process's environment and platform; printed lines go to standard error.
    pub fn from_process() -> Environment {
        let os = os_name(std::env::consts::OS);
        let path = variable("PATH");
        let nvidia = os == "linux"
            && (Path::new("/proc/driver/nvidia/version").exists()
                || find_on_path("nvidia-smi", path.as_deref(), is_executable_file).is_some());
        Environment {
            home: variable("HOME"),
            path,
            xdg_cache_home: variable("XDG_CACHE_HOME"),
            os,
            arch: arch_name(std::env::consts::ARCH),
            nvidia,
            notice: Arc::new(|line: &str| eprintln!("{line}")),
        }
    }
}

/// Rust's name for an operating system (`std::env::consts::OS`) as the contract names it.
pub fn os_name(rust_os: &str) -> String {
    rust_os.to_owned() // Rust already says macos, linux and windows
}

/// Rust's name for a processor (`std::env::consts::ARCH`) as the contract names it: `aarch64` is `arm64`.
pub fn arch_name(rust_arch: &str) -> String {
    match rust_arch {
        "aarch64" => "arm64".to_owned(),
        other => other.to_owned(),
    }
}

fn engine_error(message: String) -> JeffError {
    JeffError::new(ErrorType::EngineError, message)
}

// ---------------------------------------------------------------------------------------------------------------
// The pure parts

/// The jeff-runtime extra for this machine (`mac` or `cuda`), or BackendUnavailable (contract: "Platforms").
pub fn choose_extra(os: &str, arch: &str, nvidia: bool) -> Result<&'static str> {
    let why = match (os, arch) {
        ("macos", "arm64") => return Ok("mac"),
        ("linux", _) if nvidia => return Ok("cuda"),
        ("macos", "x86_64") => "this is an Intel Mac (or a program running under Rosetta), so local mode cannot start it \
             here. Use a Jeff server instead (remote mode)"
            .to_owned(),
        ("macos", other) => format!(
            "this Mac's processor is {other}, so local mode cannot start it here. Use a Jeff server instead (remote mode)"
        ),
        ("linux", _) => "this Linux machine has no NVIDIA GPU (no /proc/driver/nvidia/version, and no nvidia-smi on \
             PATH), so local mode cannot start it here. Use a Jeff server instead (remote mode)"
            .to_owned(),
        ("windows", _) => "the engine cannot run on Windows itself, so local mode cannot start it here. Run your program \
             in WSL 2 on a machine with an NVIDIA GPU, or use a Jeff server instead (remote mode)"
            .to_owned(),
        (other, _) => format!(
            "this machine runs {other}, so local mode cannot start it here. Use a Jeff server instead (remote mode)"
        ),
    };
    Err(JeffError::new(
        ErrorType::BackendUnavailable,
        format!(
            "The Jeff engine runs on macOS with Apple silicon, or on Linux with an NVIDIA GPU; {why}"
        ),
    ))
}

/// The error for a HOME that is not set or empty.
pub fn home_missing_error() -> JeffError {
    engine_error(
        "HOME is not set, so local mode cannot find the folder for the Jeff engine (the user's cache folder)"
            .to_owned(),
    )
}

/// HOME, or the error saying it is not set (an empty HOME counts as not set).
pub fn home_of(home: Option<&str>) -> Result<&str> {
    match home {
        Some(home) if !home.is_empty() => Ok(home),
        _ => Err(home_missing_error()),
    }
}

/// The engine folder on `os` (contract: "The engine folder"); it is not created here.
pub fn engine_folder(
    os: &str,
    home: Option<&str>,
    xdg_cache_home: Option<&str>,
) -> Result<PathBuf> {
    let home = Path::new(home_of(home)?);
    if os == "macos" {
        return Ok(home.join("Library/Caches/jeff/engine"));
    }
    match xdg_cache_home {
        Some(cache) if Path::new(cache).is_absolute() => Ok(Path::new(cache).join("jeff/engine")),
        _ => Ok(home.join(".cache/jeff/engine")),
    }
}

/// Whether `path` is a file (following links) that someone may execute.
pub fn is_executable_file(path: &Path) -> bool {
    std::fs::metadata(path)
        .map(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
        .unwrap_or(false) // a path that cannot be read is not an executable file
}

/// The first executable `name` in the folders of `path` (split at `:`, empty entries skipped, never the current
/// folder).
pub fn find_on_path(
    name: &str,
    path: Option<&str>,
    is_executable: impl Fn(&Path) -> bool,
) -> Option<PathBuf> {
    path.unwrap_or("")
        .split(':')
        .filter(|folder| folder.starts_with('/')) // empty and relative entries: never the current folder
        .map(|folder| Path::new(folder).join(name))
        .find(|candidate| is_executable(candidate))
}

/// Where uvx is (contract: "Finding uv"), or RuntimeMissing.
pub fn find_uvx(
    path: Option<&str>,
    home: &str,
    is_executable: impl Fn(&Path) -> bool,
) -> Result<PathBuf> {
    if let Some(found) = find_on_path("uvx", path, &is_executable) {
        return Ok(found);
    }
    let installer = Path::new(home).join(".local/bin/uvx");
    let cargo = Path::new(home).join(".cargo/bin/uvx");
    for candidate in [&installer, &cargo] {
        if is_executable(candidate) {
            return Ok(candidate.clone());
        }
    }
    Err(JeffError::new(
        ErrorType::RuntimeMissing,
        format!(
            "Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at {} or {}. Install uv once \
             with: curl -LsSf https://astral.sh/uv/install.sh | sh (see https://docs.astral.sh/uv/). Or start an \
             engine yourself (jeff runtime serve) and give its socket instead of the adapters",
            installer.display(),
            cargo.display()
        ),
    ))
}

/// The error for adapters trained on different bases (`bases` as `<id>@<version>`, in any order).
pub fn different_bases_error(bases: &[String]) -> JeffError {
    let mut sorted = bases.to_vec();
    sorted.sort();
    JeffError::new(
        ErrorType::ManifestError,
        format!(
            "The adapters are trained on different bases ({}); load one base at a time",
            sorted.join(", ")
        ),
    )
}

/// The error for an adapter whose weights are not open.
pub fn proprietary_error(adapter_id: &str) -> JeffError {
    JeffError::new(
        ErrorType::ProprietaryWeights,
        format!(
            "{adapter_id} is proprietary: its weights download only through JeffHub with a licence, so local mode cannot \
             start an engine for it. Start an engine yourself (jeff runtime serve --path {adapter_id}=<folder>) and \
             give its socket instead of the adapters"
        ),
    )
}

/// The adapter references resolved in `catalogue` (contract step 2): the adapter IDs, each once, in the order first
/// named, and their base as `<id>@<version>`.
pub fn resolve_adapters(
    catalogue: &Catalogue,
    references: &[String],
) -> Result<(Vec<String>, String)> {
    if references.is_empty() {
        return Err(JeffError::new(
            ErrorType::ManifestError,
            "local mode needs at least one adapter (none were given)",
        ));
    }
    let mut manifests = Vec::new();
    for reference in references {
        let manifest = catalogue.adapter(reference)?;
        if !manifests
            .iter()
            .any(|known: &Arc<crate::manifest::Manifest>| known.id == manifest.id)
        {
            manifests.push(manifest);
        }
    }
    let bases: BTreeSet<String> = manifests
        .iter()
        .map(|manifest| format!("{}@{}", manifest.base_id, manifest.base_version))
        .collect();
    if bases.len() > 1 {
        return Err(different_bases_error(
            &bases.into_iter().collect::<Vec<_>>(),
        ));
    }
    for manifest in &manifests {
        if manifest.weights.get("open").is_none() {
            return Err(proprietary_error(&manifest.id));
        }
    }
    let base = bases.into_iter().next().expect("at least one adapter");
    Ok((
        manifests
            .iter()
            .map(|manifest| manifest.id.clone())
            .collect(),
        base,
    ))
}

/// The argument list that starts the engine (contract: "Starting").
pub fn engine_command(
    uvx: &Path,
    extra: &str,
    version: &str,
    folder: &Path,
    adapter_ids: &[String],
    base: &str,
) -> Vec<String> {
    let mut argv: Vec<String> = vec![
        uvx.display().to_string(),
        "--from".into(),
        format!("jeff-runtime[{extra}]=={version}"),
        "jeff".into(),
        "runtime".into(),
        "serve".into(),
        "--socket".into(),
        folder.join("engine.sock").display().to_string(),
        "--base-version".into(),
        base.to_owned(),
    ];
    for id in adapter_ids {
        argv.push("--adapter".into());
        argv.push(id.clone());
    }
    for (option, value) in [
        ("--batch-window-ms", "0.1"),
        ("--max-batch-rows", "64"),
        ("--queue-limit-rows", "4096"),
        ("--prepare-workers", "2"),
    ] {
        argv.push(option.into());
        argv.push(value.into());
    }
    if extra == "cuda" {
        argv.push("--max-adapters".into());
        argv.push(adapter_ids.len().max(8).to_string());
        argv.push("--prefix-pool-mb".into());
        argv.push("7345".into());
    }
    argv.push("--exit-when-unused-s".into());
    argv.push("300".into());
    argv
}

/// The log's last lines (contract: "Starting", after the two errors).
pub fn log_tail(log: &str) -> String {
    let mut pieces: Vec<&str> = log.split('\n').collect();
    if pieces.last() == Some(&"") {
        pieces.pop();
    }
    let start = pieces.len().saturating_sub(TAIL_LINES);
    pieces[start..].join("\n")
}

/// `Its log is <path>; its last lines:` and the lines, or `Its log, <path>, is empty`.
fn log_ending(log_path: &Path, log: &str) -> String {
    if log.is_empty() {
        format!("Its log, {}, is empty", log_path.display())
    } else {
        format!(
            "Its log is {}; its last lines:\n{}",
            log_path.display(),
            log_tail(log)
        )
    }
}

/// The line printed before starting the engine.
pub fn starting_line(adapter_ids: &[String], log_path: &Path) -> String {
    format!(
        "Jeff: starting the Jeff engine for {}; the first time, this downloads the engine and the base model (about 2 \
         GB, once). Its log: {}",
        adapter_ids.join(", "),
        log_path.display()
    )
}

/// The line printed once while waiting for another program's start.
pub fn waiting_line(log_path: &Path) -> String {
    format!(
        "Jeff: waiting for the Jeff engine that another program is starting. Its log: {}",
        log_path.display()
    )
}

/// The error for an engine that exited with `status` before it answered.
pub fn exited_error(status: i32, log_path: &Path, log: &str) -> JeffError {
    engine_error(format!(
        "The Jeff engine exited while starting, with status {status}. {}",
        log_ending(log_path, log)
    ))
}

/// The error for an engine that did not answer within the start timeout (it has been sent SIGTERM).
pub fn start_timeout_error(
    socket_path: &Path,
    start_timeout_ms: u64,
    log_path: &Path,
    log: &str,
) -> JeffError {
    engine_error(format!(
        "The Jeff engine did not answer on {} within {start_timeout_ms} ms, so it was stopped. {}",
        socket_path.display(),
        log_ending(log_path, log)
    ))
}

/// The error for a start lock another program held past the start timeout.
pub fn lock_timeout_error(
    start_timeout_ms: u64,
    lock_path: &Path,
    pid: &str,
    log_path: &Path,
) -> JeffError {
    engine_error(format!(
        "Another program has been starting the Jeff engine for longer than {start_timeout_ms} ms (the lock {}, held by \
         process {pid}). Its log is {}",
        lock_path.display(),
        log_path.display()
    ))
}

/// The error for a running engine that refused to serve the `missing` adapters (`refusal` is the engine's error).
pub fn not_loadable_error(
    socket_path: &Path,
    missing: &[String],
    refusal: &JeffError,
) -> JeffError {
    JeffError::new(
        ErrorType::NotInstalled,
        format!(
            "The Jeff engine already running at {} does not serve {}, and refused to load them: {}: {}. Start an \
             engine yourself with every adapter you need (jeff runtime serve) and give its socket instead of the \
             adapters; the shared engine stops by itself 5 minutes after the last program using it closes",
            socket_path.display(),
            missing.join(", "),
            refusal.type_name(),
            refusal.message()
        ),
    )
}

/// The status a shell reports for a process: its exit code, or 128 + the signal that ended it.
pub fn exit_status_number(status: ExitStatus) -> i32 {
    match (status.code(), status.signal()) {
        (Some(code), _) => code,
        (None, Some(signal)) => 128 + signal,
        (None, None) => unreachable!("on Unix a process ends with an exit code or by a signal"),
    }
}

// ---------------------------------------------------------------------------------------------------------------
// The steps

/// Local mode with a list of adapters, in `environment` (contract/local-engine.md, "The steps, in order").
pub async fn local_adapters_in(
    references: &[String],
    options: StartOptions,
    environment: &Environment,
) -> Result<Jeff> {
    let called = Instant::now();
    // 1. the options
    check_milliseconds("timeout_ms", options.timeout_ms)?;
    check_milliseconds("start_timeout_ms", options.start_timeout_ms)?;
    let deadline = called + Duration::from_millis(options.start_timeout_ms);
    // 2. the adapters
    let catalogue = Catalogue::shipped()?;
    let (adapter_ids, base) = resolve_adapters(&catalogue, references)?;
    // 3. the platform
    let extra = choose_extra(&environment.os, &environment.arch, environment.nvidia)?;
    // 4. the folder
    let folder = engine_folder(
        &environment.os,
        environment.home.as_deref(),
        environment.xdg_cache_home.as_deref(),
    )?;
    make_folder(&folder)?;
    let socket = folder.join("engine.sock");
    // 5. a running engine
    let engine = match SocketEngine::connect_if_listening(&socket, options.timeout_ms).await? {
        Some(engine) => engine,
        None => {
            // 6. uvx
            let home = home_of(environment.home.as_deref())?;
            let uvx = find_uvx(environment.path.as_deref(), home, is_executable_file)?;
            // 7. the lock, then a second look
            let lock =
                StartLock::take(&folder, deadline, options.start_timeout_ms, environment).await?;
            let started = async {
                match SocketEngine::connect_if_listening(&socket, options.timeout_ms).await? {
                    Some(engine) => Ok(engine),
                    // 8. start it
                    None => {
                        let argv =
                            engine_command(&uvx, extra, SDK_VERSION, &folder, &adapter_ids, &base);
                        start_engine(
                            &argv,
                            &folder,
                            &adapter_ids,
                            deadline,
                            &options,
                            environment,
                        )
                        .await
                    }
                }
            }
            .await;
            // the lock is released whether the start succeeded or not
            match (started, lock.release()) {
                (Ok(engine), Ok(())) => engine,
                (Ok(engine), Err(error)) => {
                    engine.close().await;
                    return Err(error);
                }
                (Err(mut error), Err(release)) => {
                    error.add_note(format!("Also: {}", release.message()));
                    return Err(error);
                }
                (Err(error), Ok(())) => return Err(error),
            }
        }
    };
    let engine = Arc::new(engine);
    // 9. every adapter served
    if let Err(error) = serve_every_adapter(&engine, &socket, &adapter_ids).await {
        engine.close().await;
        return Err(error);
    }
    // 10. the client
    Jeff::over_socket_engine(engine, catalogue).await
}

fn make_folder(folder: &Path) -> Result<()> {
    let failed = |error: std::io::Error| {
        engine_error(format!(
            "Local mode cannot create the folder for the Jeff engine, {}: {error}",
            folder.display()
        ))
    };
    if let Some(parent) = folder.parent() {
        std::fs::create_dir_all(parent).map_err(failed)?;
    }
    match std::fs::DirBuilder::new().mode(0o700).create(folder) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::AlreadyExists && folder.is_dir() => Ok(()),
        Err(error) => Err(failed(error)),
    }
}

/// Whether process `pid` is running (signal 0 checks without sending anything; EPERM means it runs as another user).
fn process_running(pid: i32) -> bool {
    // SAFETY: kill with signal 0 sends nothing; it only reports whether the process exists.
    let answer = unsafe { libc::kill(pid, 0) };
    answer == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

/// The start lock (contract: "The start lock"), released by [`StartLock::release`]. Dropped unreleased (the caller
/// stopped waiting for local mode midway), it is released then too, so other programs do not wait for a start that
/// nobody is making.
struct StartLock {
    path: PathBuf,
    pid: u32,
    released: bool,
}

impl StartLock {
    async fn take(
        folder: &Path,
        deadline: Instant,
        start_timeout_ms: u64,
        environment: &Environment,
    ) -> Result<StartLock> {
        let path = folder.join("engine.lock");
        let log_path = folder.join("engine.log");
        let pid = std::process::id();
        let failed = |what: &str, error: std::io::Error| {
            engine_error(format!(
                "Local mode cannot {what} the start lock {}: {error}",
                path.display()
            ))
        };
        let mut waiting_printed = false;
        loop {
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(mut file) => {
                    file.write_all(format!("{pid}\n").as_bytes())
                        .map_err(|error| failed("write", error))?;
                    return Ok(StartLock {
                        path,
                        pid,
                        released: false,
                    });
                }
                Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
                Err(error) => return Err(failed("create", error)),
            }
            let mut text = String::new();
            let modified = match File::open(&path) {
                Ok(mut file) => {
                    file.read_to_string(&mut text)
                        .map_err(|error| failed("read", error))?;
                    file.metadata()
                        .and_then(|metadata| metadata.modified())
                        .map_err(|error| failed("read", error))?
                }
                // released between the two looks: try again at once
                Err(error) if error.kind() == ErrorKind::NotFound => continue,
                Err(error) => return Err(failed("read", error)),
            };
            let holder = text.trim().parse::<i32>().ok().filter(|holder| *holder > 0);
            let stale = match holder {
                Some(holder) => !process_running(holder),
                None => SystemTime::now()
                    .duration_since(modified)
                    .is_ok_and(|age| age > LOCK_WITHOUT_PID_STALE),
            };
            if stale {
                match std::fs::remove_file(&path) {
                    Ok(()) => {}
                    Err(error) if error.kind() == ErrorKind::NotFound => {} // another program removed it first
                    Err(error) => return Err(failed("remove the stale", error)),
                }
                continue;
            }
            if !waiting_printed {
                (environment.notice)(&waiting_line(&log_path));
                waiting_printed = true;
            }
            let now = Instant::now();
            if now >= deadline {
                let holder = match holder {
                    Some(holder) => holder.to_string(),
                    None => "(none written yet)".to_owned(),
                };
                return Err(lock_timeout_error(
                    start_timeout_ms,
                    &path,
                    &holder,
                    &log_path,
                ));
            }
            tokio::time::sleep(POLL.min(deadline - now)).await;
        }
    }
}

impl StartLock {
    /// Deletes the lock file if it still holds this program's process ID.
    fn release(mut self) -> Result<()> {
        self.released = true;
        let failed = |error: std::io::Error| {
            engine_error(format!(
                "Local mode cannot release the start lock {}: {error}",
                self.path.display()
            ))
        };
        let text = match std::fs::read_to_string(&self.path) {
            Ok(text) => text,
            Err(error) if error.kind() == ErrorKind::NotFound => return Ok(()), // no longer there to release
            Err(error) => return Err(failed(error)),
        };
        if text.trim() != self.pid.to_string() {
            return Ok(()); // taken over by another program: no longer this program's to release
        }
        match std::fs::remove_file(&self.path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
            Err(error) => Err(failed(error)),
        }
    }
}

impl Drop for StartLock {
    fn drop(&mut self) {
        if !self.released
            && let Ok(text) = std::fs::read_to_string(&self.path)
            && text.trim() == self.pid.to_string()
        {
            // Nobody is left to report a failure to: the caller stopped waiting. A lock left behind is waited for
            // while this process runs, and found stale after it ends.
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

fn read_log(log_path: &Path) -> Result<String> {
    std::fs::read(log_path)
        .map(|bytes| String::from_utf8_lossy(&bytes).into_owned())
        .map_err(|error| {
            engine_error(format!(
                "Local mode cannot read the Jeff engine's log {}: {error}",
                log_path.display()
            ))
        })
}

/// Collects the engine's exit status once it ends, so it does not linger as a zombie; nothing waits for it.
fn reap(mut child: Child) {
    std::thread::spawn(move || {
        let _ = child.wait(); // only reaps; its status is of no use any more
    });
}

/// Starts the engine and waits for it to answer (contract: "Starting").
async fn start_engine(
    argv: &[String],
    folder: &Path,
    adapter_ids: &[String],
    deadline: Instant,
    options: &StartOptions,
    environment: &Environment,
) -> Result<SocketEngine> {
    let log_path = folder.join("engine.log");
    let socket = folder.join("engine.sock");
    (environment.notice)(&starting_line(adapter_ids, &log_path));
    let log = File::create(&log_path).map_err(|error| {
        engine_error(format!(
            "Local mode cannot empty the Jeff engine's log {}: {error}",
            log_path.display()
        ))
    })?;
    let log_for_errors = log.try_clone().map_err(|error| {
        engine_error(format!(
            "Local mode cannot open the Jeff engine's log {} twice: {error}",
            log_path.display()
        ))
    })?;
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .current_dir(folder)
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log_for_errors)
        .process_group(0)
        .spawn()
        .map_err(|error| {
            engine_error(format!(
                "Local mode cannot run {} to start the Jeff engine: {error}",
                argv[0]
            ))
        })?;
    loop {
        match SocketEngine::connect_if_listening(&socket, options.timeout_ms).await {
            Ok(Some(engine)) => {
                reap(child);
                return Ok(engine);
            }
            Ok(None) => {}
            Err(error) => {
                reap(child);
                return Err(error);
            }
        }
        let exited = child.try_wait().map_err(|error| {
            engine_error(format!(
                "Local mode cannot tell whether the Jeff engine it started (process {}) is still running: {error}",
                child.id()
            ))
        })?;
        if let Some(status) = exited {
            return Err(exited_error(
                exit_status_number(status),
                &log_path,
                &read_log(&log_path)?,
            ));
        }
        let now = Instant::now();
        if now >= deadline {
            let group = child.id() as i32;
            // SAFETY: a negative pid sends the signal to that process group, the one this program started.
            if unsafe { libc::kill(-group, libc::SIGTERM) } != 0 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(engine_error(format!(
                        "The Jeff engine did not answer on {} within {} ms, and local mode could not stop it \
                         (SIGTERM to process group {group}): {error}",
                        socket.display(),
                        options.start_timeout_ms
                    )));
                }
                // ESRCH: the whole group had already ended
            }
            reap(child);
            return Err(start_timeout_error(
                &socket,
                options.start_timeout_ms,
                &log_path,
                &read_log(&log_path)?,
            ));
        }
        tokio::time::sleep(POLL.min(deadline - now)).await;
    }
}

/// Step 9: one `serve` request when the engine lacks some of the adapters (contract: "Sharing").
async fn serve_every_adapter(
    engine: &SocketEngine,
    socket: &Path,
    adapter_ids: &[String],
) -> Result<()> {
    let installed = engine.installed_in_order().await?;
    let missing: Vec<String> = adapter_ids
        .iter()
        .filter(|id| !installed.contains(id))
        .cloned()
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    let mut wanted: Vec<String> = installed.into_iter().filter(|id| id != "base").collect();
    wanted.extend(missing.iter().cloned());
    match engine.serve_or_refusal(&wanted).await? {
        Ok(_) => Ok(()),
        Err(refusal) => Err(not_loadable_error(socket, &missing, &refusal)),
    }
}
