//! `keirolake-desktop mcp`: a local MCP server over stdio for AI apps that start programs, such as Claude Desktop.
//!
//! It reads one JSON-RPC message per line on stdin, passes each to the running engine's /mcp endpoint with the API key
//! from KEIROLAKE_KEY, and writes the answer as one line on stdout. If KeiroLake isn't running, it starts it (in the
//! tray) and waits. The engine's address comes from `<data dir>/engine-address.json`, which the app writes at start.

use std::io::{BufRead, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

const IDENTIFIER: &str = "app.keirolake.desktop";
const ADDRESS_FILE: &str = "engine-address.json";

pub fn data_dir() -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("KEIROLAKE_DATA_DIR") {
        if !dir.is_empty() {
            return Some(PathBuf::from(dir));
        }
    }
    #[cfg(windows)]
    let base = std::env::var_os("APPDATA").map(PathBuf::from);
    #[cfg(target_os = "macos")]
    let base = std::env::var_os("HOME").map(|h| PathBuf::from(h).join("Library/Application Support"));
    #[cfg(all(unix, not(target_os = "macos")))]
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")));
    base.map(|b| b.join(IDENTIFIER))
}

/// Called by the app once the engine answers, so the bridge can find it.
pub fn write_address(data_dir: &std::path::Path, port: u16) {
    let _ = std::fs::write(data_dir.join(ADDRESS_FILE), format!("{{\"port\": {port}}}"));
}

fn engine_port() -> Option<u16> {
    let text = std::fs::read_to_string(data_dir()?.join(ADDRESS_FILE)).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let port = value.get("port")?.as_u64()?;
    u16::try_from(port).ok()
}

/// POST one message to the engine; Ok(None) for a notification (HTTP 202, no body).
fn post(port: u16, key: &str, body: &str) -> Result<Option<String>, String> {
    let mut stream = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_secs(2)).map_err(|e| e.to_string())?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(600)));  // building a table can take minutes
    let req = format!(
        "POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {key}\r\nContent-Type: application/json\r\n\
         Accept: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&raw);
    let (head, payload) = text.split_once("\r\n\r\n").ok_or("The engine sent an incomplete answer")?;
    let status = head.split_whitespace().nth(1).unwrap_or("");
    if status == "202" {
        return Ok(None);
    }
    let payload = if head.to_ascii_lowercase().contains("transfer-encoding: chunked") { dechunk(payload) } else { payload.to_string() };
    if status == "401" {
        return Err("KeiroLake refused the API key in KEIROLAKE_KEY (wrong, revoked or expired). Make a new one in KeiroLake → Connect.".into());
    }
    // Re-encode on one line: stdio MCP is one message per line.
    serde_json::from_str::<serde_json::Value>(&payload)
        .map(|v| Some(v.to_string()))
        .map_err(|_| format!("The engine answered {status} with something that isn't JSON"))
}

fn dechunk(body: &str) -> String {
    let mut out = String::new();
    let mut rest = body;
    while let Some((size, after)) = rest.split_once("\r\n") {
        let Ok(n) = usize::from_str_radix(size.trim(), 16) else { break };
        if n == 0 || after.len() < n {
            break;
        }
        out.push_str(&after[..n]);
        rest = after[n..].trim_start_matches("\r\n");
    }
    out
}

/// Find the engine, starting the app in the tray if it isn't running yet.
fn connect() -> Result<u16, String> {
    let reachable = |p: u16| TcpStream::connect_timeout(&([127, 0, 0, 1], p).into(), Duration::from_millis(500)).is_ok();
    if let Some(p) = engine_port().filter(|p| reachable(*p)) {
        return Ok(p);
    }
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let mut cmd = Command::new(exe);
    cmd.arg("--hidden");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0000_0008);  // DETACHED_PROCESS: the app outlives this bridge
    }
    let _ = cmd.spawn();
    let started = Instant::now();
    while started.elapsed() < Duration::from_secs(120) {
        std::thread::sleep(Duration::from_secs(1));
        if let Some(p) = engine_port().filter(|p| reachable(*p)) {
            return Ok(p);
        }
    }
    Err("KeiroLake didn't start within 2 minutes. Open it once, then try again.".into())
}

fn error_line(id: &serde_json::Value, message: &str) -> String {
    serde_json::json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32000, "message": message}}).to_string()
}

pub fn run() -> i32 {
    let key = std::env::var("KEIROLAKE_KEY").unwrap_or_default();
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    let mut port: Option<u16> = None;
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let id = serde_json::from_str::<serde_json::Value>(&line).ok().and_then(|v| v.get("id").cloned()).unwrap_or(serde_json::Value::Null);
        let answer = if key.is_empty() {
            Err("Set KEIROLAKE_KEY to an API key made in KeiroLake → Connect.".to_string())
        } else {
            let p = match port {
                Some(p) => Ok(p),
                None => connect(),
            };
            p.and_then(|p| {
                port = Some(p);
                // One retry after reconnecting: the app may have restarted (an update) since the last message.
                post(p, &key, &line).or_else(|_| connect().and_then(|p2| { port = Some(p2); post(p2, &key, &line) }))
            })
        };
        let out = match answer {
            Ok(Some(text)) => text,
            Ok(None) => continue,
            Err(e) if id.is_null() => { eprintln!("{e}"); continue }
            Err(e) => error_line(&id, &e),
        };
        if writeln!(stdout, "{out}").and_then(|_| stdout.flush()).is_err() {
            break;
        }
    }
    0
}
