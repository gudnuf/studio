//! The studio server behind the window: attach to the one already serving this library
//! ($STUDIO_HOME/.server.json, confirmed by GET /api/health), or start our own from the
//! nix-built wrapper. Only a server we started is ever stopped.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::Value;

pub struct Backend {
    pub home: PathBuf,
    pub port: u16,
    pub attached: bool,
    pub child: Option<Child>,
    pub health: Value,
}

impl Backend {
    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}/", self.port)
    }

    /// Stop the server only if we started it: close its stdin (it exits on EOF and takes its
    /// ffmpeg/whisper/agent children with it), then TERM the process group, then KILL.
    pub fn stop(&mut self) {
        let Some(mut child) = self.child.take() else { return };
        let pid = child.id();
        drop(child.stdin.take());
        if wait_exit(&mut child, Duration::from_secs(3)) {
            return;
        }
        let _ = Command::new("/bin/kill").args(["-TERM", &format!("-{pid}")]).status();
        if wait_exit(&mut child, Duration::from_secs(2)) {
            return;
        }
        let _ = Command::new("/bin/kill").args(["-KILL", &format!("-{pid}")]).status();
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn wait_exit(child: &mut Child, max: Duration) -> bool {
    let end = Instant::now() + max;
    while Instant::now() < end {
        if let Ok(Some(_)) = child.try_wait() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    false
}

/// GET /api/health on loopback, with short timeouts. None if nothing (or not studio) answers.
pub fn health(port: u16) -> Option<Value> {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_millis(400)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    s.write_all(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").ok()?;
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    let (head, body) = text.split_once("\r\n\r\n")?;
    if !head.starts_with("HTTP/1.") || !head.contains(" 200 ") {
        return None;
    }
    let v: Value = serde_json::from_str(body).ok()?;
    (v.get("app").and_then(Value::as_str) == Some("studio")).then_some(v)
}

fn same_home(a: &Path, b: &str) -> bool {
    let ca = fs::canonicalize(a).unwrap_or_else(|_| a.to_path_buf());
    let cb = fs::canonicalize(b).unwrap_or_else(|_| PathBuf::from(b));
    ca == cb
}

/// The server already serving `home`, if there is one and it answers for that library.
pub fn find_running(home: &Path) -> Option<(u16, Value)> {
    let info: Value = serde_json::from_str(&fs::read_to_string(home.join(".server.json")).ok()?).ok()?;
    let port = u16::try_from(info.get("port")?.as_u64()?).ok()?;
    let h = health(port)?;
    let ok = h.get("home").and_then(Value::as_str).is_some_and(|hh| same_home(home, hh))
        && h.get("pid") == info.get("pid");
    ok.then_some((port, h))
}

/// The ports to try, from $STUDIO_APP_PORTS ("7800-7899") or 7317-7399. A stable port keeps
/// the UI's per-origin settings (zoom, mute) across launches.
fn port_range() -> (u16, u16) {
    let spec = std::env::var("STUDIO_APP_PORTS").unwrap_or_else(|_| "7317-7399".into());
    let mut it = spec.split('-').map(|x| x.trim().parse::<u16>().ok());
    match (it.next().flatten(), it.next().flatten()) {
        (Some(a), Some(b)) if a <= b => (a, b),
        (Some(a), None) => (a, a),
        _ => (7317, 7399),
    }
}

fn free_port(skip: &[u16]) -> Option<u16> {
    let (a, b) = port_range();
    (a..=b).find(|p| !skip.contains(p) && TcpListener::bind(("127.0.0.1", *p)).is_ok())
}

/// How to run the backend: $STUDIO_BACKEND (runtime override), else the wrapper the nix build
/// baked in, else (cargo tauri dev) python3 on PATH with the source tree's studio.py.
fn backend_command() -> (String, Vec<String>) {
    if let Ok(b) = std::env::var("STUDIO_BACKEND") {
        if !b.is_empty() {
            return (b, vec![]);
        }
    }
    if let Some(b) = option_env!("STUDIO_BACKEND") {
        return (b.to_string(), vec![]);
    }
    let studio = Path::new(env!("CARGO_MANIFEST_DIR")).join("../studio.py");
    ("python3".into(), vec!["-B".into(), studio.to_string_lossy().into_owned()])
}

/// The user's login-shell PATH. A Dock launch gets launchd's bare PATH
/// (/usr/bin:/bin:/usr/sbin:/sbin), which has no agent CLI (`claude`, `codex`, `goose`…); the
/// shell knows where it is.
pub fn login_path() -> Option<String> {
    let shell = std::env::var("SHELL").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/bin/zsh".into());
    let mut child = Command::new(&shell)
        .args(["-l", "-c", "printf '\\n__STUDIO_PATH__%s\\n' \"$PATH\""])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    if !wait_exit(&mut child, Duration::from_secs(8)) {
        let _ = child.kill();
        let _ = child.wait();
        return None;
    }
    let mut out = String::new();
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    out.lines().find_map(|l| l.strip_prefix("__STUDIO_PATH__")).map(str::to_string)
}

/// Our own PATH first (so a test can put a fake agent in front), then the login shell's.
/// The wrapper then puts the pinned python3/ffmpeg/whisper-cli in front of all of it.
pub fn merged_path(login: Option<&str>) -> String {
    let own = std::env::var("PATH").unwrap_or_default();
    let mut out: Vec<&str> = Vec::new();
    for part in own.split(':').chain(login.unwrap_or("").split(':')) {
        if !part.is_empty() && !out.contains(&part) {
            out.push(part);
        }
    }
    for d in ["/usr/bin", "/bin", "/usr/sbin", "/sbin"] {
        if !out.contains(&d) {
            out.push(d);
        }
    }
    out.join(":")
}

pub fn which(path: &str, tool: &str) -> Option<PathBuf> {
    path.split(':').map(|d| Path::new(d).join(tool)).find(|p| {
        fs::metadata(p).map(|m| m.is_file() && std::os::unix::fs::PermissionsExt::mode(&m.permissions()) & 0o111 != 0).unwrap_or(false)
    })
}

/// The coding agents studio.py has built in, in the order it picks the first one found.
pub const AGENTS: [&str; 3] = ["claude", "codex", "goose"];

/// The agent CLI that "Find moments" will run, as studio.py chooses it: STUDIO_AGENT_CMD's
/// program, else STUDIO_AGENT, else the first of AGENTS on `path`. (name, where it is)
pub fn find_agent(path: &str) -> (String, Option<PathBuf>) {
    let var = |k: &str| std::env::var(k).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
    let name = var("STUDIO_AGENT_CMD").and_then(|c| c.split_whitespace().next().map(str::to_string))
        .or_else(|| var("STUDIO_AGENT").map(|a| a.to_lowercase()))
        .or_else(|| AGENTS.iter().find(|a| which(path, a).is_some()).map(|a| a.to_string()))
        .unwrap_or_else(|| AGENTS[0].to_string());
    let found = if name.contains('/') { Some(PathBuf::from(&name)).filter(|p| p.is_file()) } else { which(path, &name) };
    (name, found)
}

pub fn log_dir() -> PathBuf {
    let home = std::env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("/tmp"));
    let d = home.join("Library/Logs/Studio");
    let _ = fs::create_dir_all(&d);
    d
}

pub fn log(msg: &str) {
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(log_dir().join("app.log")) {
        let _ = writeln!(f, "[{}] {msg}", chrono_now());
    }
    eprintln!("studio-app: {msg}");
}

fn chrono_now() -> String {
    let out = Command::new("/bin/date").arg("+%Y-%m-%d %H:%M:%S").output();
    out.map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default()
}

fn tail(path: &Path, n: usize) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

/// Attach to the server for `home`, or start one. `path` is the PATH the server gets.
pub fn attach_or_start(home: &Path, path: &str) -> Result<Backend, (String, String)> {
    if let Some((port, h)) = find_running(home) {
        log(&format!("attached to the running server on port {port} for {}", home.display()));
        return Ok(Backend { home: home.to_path_buf(), port, attached: true, child: None, health: h });
    }
    fs::create_dir_all(home).map_err(|e| (format!("Can't use the library folder {}", home.display()), e.to_string()))?;
    let (prog, pre) = backend_command();
    let server_log = log_dir().join("server.log");
    let mut tried = Vec::new();
    for _attempt in 0..4 {
        let Some(port) = free_port(&tried) else {
            let (a, b) = port_range();
            return Err(("No free port for the studio".into(), format!("ports {a}-{b} are all in use")));
        };
        tried.push(port);
        let out = OpenOptions::new().create(true).append(true).open(&server_log)
            .map_err(|e| ("Can't write the server log".to_string(), e.to_string()))?;
        let err = out.try_clone().map_err(|e| ("Can't write the server log".to_string(), e.to_string()))?;
        log(&format!("starting {prog} {} ui --no-open --port {port} (library {})", pre.join(" "), home.display()));
        let mut cmd = Command::new(&prog);
        cmd.args(&pre)
            .args(["ui", "--no-open", "--port", &port.to_string()])
            .env("STUDIO_HOME", home)
            .env("STUDIO_PARENT_PID", std::process::id().to_string())
            .env("PATH", path)
            // the app's server acts for the operator, never as an agent (see studio.py actor_now)
            .env_remove("CLAUDECODE")
            .env_remove("CODEX_THREAD_ID")
            .env_remove("CODEX_SANDBOX")
            .env_remove("GOOSE_TERMINAL")
            .env_remove("STUDIO_ACTOR")
            .stdin(Stdio::piped())
            .stdout(Stdio::from(out))
            .stderr(Stdio::from(err))
            .process_group(0);
        let mut child = cmd.spawn().map_err(|e| (format!("Couldn't start the studio ({prog})"), e.to_string()))?;
        let end = Instant::now() + Duration::from_secs(60);
        loop {
            if let Some(h) = health(port) {
                let pid_ok = h.get("pid").and_then(Value::as_u64) == Some(u64::from(child.id()));
                if pid_ok {
                    log(&format!("server up on port {port} (pid {})", child.id()));
                    return Ok(Backend { home: home.to_path_buf(), port, attached: false, child: Some(child), health: h });
                }
            }
            if let Ok(Some(status)) = child.try_wait() {
                log(&format!("server exited early ({status}) on port {port}"));
                // someone else took the port between our check and its bind: try the next one
                if tail(&server_log, 5).contains("Address already in use") {
                    break;
                }
                return Err(("The studio stopped while starting".into(), tail(&server_log, 25)));
            }
            if Instant::now() > end {
                let mut b = Backend { home: home.to_path_buf(), port, attached: false, child: Some(child), health: Value::Null };
                b.stop();
                return Err(("The studio didn't answer within a minute".into(), tail(&server_log, 25)));
            }
            std::thread::sleep(Duration::from_millis(80));
        }
    }
    Err(("Couldn't start the studio".into(), tail(&server_log, 25)))
}

/// The library folder: --home DIR, else $STUDIO_HOME, else the saved setting, else ~/Studio.
pub fn resolve_home(saved: Option<PathBuf>) -> PathBuf {
    let args: Vec<String> = std::env::args().collect();
    if let Some(i) = args.iter().position(|a| a == "--home") {
        if let Some(p) = args.get(i + 1) {
            return PathBuf::from(p);
        }
    }
    if let Some(a) = args.iter().find_map(|a| a.strip_prefix("--home=")) {
        return PathBuf::from(a);
    }
    if let Ok(h) = std::env::var("STUDIO_HOME") {
        if !h.is_empty() {
            return PathBuf::from(h);
        }
    }
    if let Some(p) = saved {
        return p;
    }
    let home = std::env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("/tmp"));
    home.join("Studio")
}

pub fn settings_file(config_dir: &Path) -> PathBuf {
    config_dir.join("settings.json")
}

pub fn load_saved_home(config_dir: &Path) -> Option<PathBuf> {
    let v: Value = serde_json::from_str(&fs::read_to_string(settings_file(config_dir)).ok()?).ok()?;
    v.get("home").and_then(Value::as_str).map(PathBuf::from)
}

pub fn save_home(config_dir: &Path, home: &Path) -> std::io::Result<()> {
    fs::create_dir_all(config_dir)?;
    let mut f = File::create(settings_file(config_dir))?;
    f.write_all(serde_json::json!({ "home": home }).to_string().as_bytes())
}
