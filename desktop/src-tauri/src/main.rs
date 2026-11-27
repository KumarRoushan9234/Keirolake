#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! KeiroLake desktop shell.
//!
//! Starts two local services and supervises them for the life of the window:
//!   * Qdrant (vector store) when its binary ships next to the app, otherwise the
//!     engine uses Qdrant's embedded mode;
//!   * the KeiroLake engine (Python, frozen) on a random 127.0.0.1 port with a
//!     per-launch token, so other programs and websites can't use its API.
//! If either stops (a crash, or Help → Restart engine) it is started again on the
//! same port and token, so the open window keeps working. The UI asks for the
//! engine's address and token with `engine_info`.
//!
//! The app lives in the system tray: closing the window hides it and the engine keeps running
//! (scheduled refreshes, running jobs, the data API). Only Quit stops everything. It starts at login,
//! hidden in the tray, and a second launch just brings the running window forward.

mod mcp_bridge;

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::ManagerExt;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const MAX_RESTARTS: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(600);

/// Everything needed to (re)start the services with the same address and token.
#[derive(Clone)]
struct Launch {
    data_dir: PathBuf,
    port: u16,
    token: String,
    qdrant: Option<(u16, u16)>, // http, grpc
}

#[derive(Default)]
struct EngineState {
    url: String,
    token: String,
    ready: bool,
    error: Option<String>,
    launch: Option<Launch>,
    engine: Option<Child>,
    qdrant: Option<Child>,
    restarts: Vec<Instant>,
    stopping: bool,
    /// (nonce, knowledge base) of the page capture the user just asked for.
    pending_capture: Option<(String, String)>,
}

struct Shared(Mutex<EngineState>);

#[derive(Serialize)]
struct EngineInfo {
    url: String,
    token: String,
    ready: bool,
    error: Option<String>,
    version: String,
}

#[derive(Serialize, Clone)]
struct CapturedPage {
    url: String,
    title: String,
    html: String,
    kb: String,
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0").and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(8765)
}

fn exe_name(base: &str) -> String {
    if cfg!(windows) { format!("{base}.exe") } else { base.to_string() }
}

/// Sidecars are installed next to the app executable.
fn sidecar(base: &str) -> Option<PathBuf> {
    let dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let path = dir.join(exe_name(base));
    path.exists().then_some(path)
}

fn hide_window(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

fn log_file(data_dir: &Path, name: &str) -> Stdio {
    let dir = data_dir.join("logs");
    let _ = std::fs::create_dir_all(&dir);
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join(name))
        .map(Stdio::from)
        .unwrap_or_else(|_| Stdio::null())
}

/// Plain HTTP GET against a local service; true on a 200 response.
fn http_ok(port: u16, path: &str, token: &str) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-KeiroLake-Token: {token}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 16];
    matches!(stream.read(&mut head), Ok(n) if n >= 12 && head[9..12] == *b"200")
}

fn spawn_qdrant(l: &Launch) -> Option<Child> {
    let (http, grpc) = l.qdrant?;
    let bin = sidecar("qdrant")?;
    let mut cmd = Command::new(bin);
    cmd.current_dir(&l.data_dir)
        .env("QDRANT__SERVICE__HOST", "127.0.0.1")
        .env("QDRANT__SERVICE__HTTP_PORT", http.to_string())
        .env("QDRANT__SERVICE__GRPC_PORT", grpc.to_string())
        .env("QDRANT__STORAGE__STORAGE_PATH", l.data_dir.join("qdrant-storage"))
        .env("QDRANT__STORAGE__SNAPSHOTS_PATH", l.data_dir.join("qdrant-snapshots"))
        .env("QDRANT__TELEMETRY_DISABLED", "true")
        .stdin(Stdio::null())
        .stdout(log_file(&l.data_dir, "qdrant.log"))
        .stderr(log_file(&l.data_dir, "qdrant.log"));
    hide_window(&mut cmd).spawn().ok()
}

/// True once Qdrant answers; false if it exits or doesn't answer in time.
fn wait_qdrant(port: u16, child: &mut Option<Child>) -> bool {
    let started = Instant::now();
    while started.elapsed() < Duration::from_secs(30) {
        if http_ok(port, "/healthz", "") {
            return true;
        }
        if child.as_mut().map_or(true, |c| c.try_wait().ok().flatten().is_some()) {
            return false; // it exited (for example a library the system lacks)
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

/// The frozen engine in a release build; the backend's virtualenv in development.
fn engine_command() -> Result<Command, String> {
    if let Some(bin) = sidecar("keirolake-engine") {
        return Ok(Command::new(bin));
    }
    let backend = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../backend");
    let python = if cfg!(windows) { backend.join(".venv/Scripts/python.exe") } else { backend.join(".venv/bin/python") };
    if !python.exists() {
        return Err(format!("Engine not found. Build it with desktop/scripts/build_sidecars.py, or create {}", python.display()));
    }
    let mut cmd = Command::new(python);
    cmd.current_dir(&backend).arg("engine_main.py");
    Ok(cmd)
}

fn spawn_engine(l: &Launch) -> Result<Child, String> {
    let mut cmd = engine_command()?;
    cmd.env("KEIROLAKE_TOKEN", &l.token)
        .env("KEIROLAKE_PORT", l.port.to_string())
        .env("KEIROLAKE_SUPERVISED", "1")
        .env("KEIROLAKE_PARENT_PID", std::process::id().to_string())
        .env("KEIROLAKE_APP_EXE", std::env::current_exe().unwrap_or_default())
        .env("KEIROLAKE_APP_VERSION", env!("CARGO_PKG_VERSION"))
        .env("DATA_DIR", &l.data_dir)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(log_file(&l.data_dir, "engine.log"))
        .stderr(log_file(&l.data_dir, "engine.log"));
    match l.qdrant {
        Some((http, _)) => cmd.env("QDRANT_URL", format!("http://127.0.0.1:{http}")).env_remove("QDRANT_PATH"),
        None => cmd.env("QDRANT_PATH", l.data_dir.join("qdrant")),
    };
    #[cfg(unix)]
    {
        // Its own process group, so stopping it also stops the packaged engine's inner process.
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    hide_window(&mut cmd).spawn().map_err(|e| format!("Couldn't start the engine: {e}"))
}

/// Stop a sidecar and everything it started. The packaged engine is a launcher plus the real engine; killing only the
/// launcher left the engine running with its ports held, so the next start (after an update) failed.
fn kill_tree(mut child: Child) {
    #[cfg(windows)]
    {
        let _ = hide_window(Command::new("taskkill").args(["/PID", &child.id().to_string(), "/T", "/F"]))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    #[cfg(unix)]
    {
        let _ = Command::new("kill").args(["-KILL", "--", &format!("-{}", child.id())]).stderr(Stdio::null()).status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn wait_engine(app: &tauri::AppHandle, l: &Launch) -> Result<(), String> {
    let started = Instant::now();
    let state = app.state::<Shared>();
    while started.elapsed() < Duration::from_secs(120) {
        if http_ok(l.port, "/api/net", &l.token) {
            mcp_bridge::write_address(&l.data_dir, l.port);
            state.0.lock().unwrap().ready = true;
            return Ok(());
        }
        let exited = state.0.lock().unwrap().engine.as_mut().and_then(|c| c.try_wait().ok().flatten());
        if let Some(code) = exited {
            return Err(format!("The engine stopped ({code}); see {}", l.data_dir.join("logs/engine.log").display()));
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Err(format!("The engine did not answer within 2 minutes; see {}", l.data_dir.join("logs/engine.log").display()))
}

fn boot(app: &tauri::AppHandle) -> Result<(), String> {
    let data_dir = match std::env::var("KEIROLAKE_DATA_DIR") {
        Ok(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => app.path().app_data_dir().map_err(|e| e.to_string())?,
    };
    std::fs::create_dir_all(&data_dir).map_err(|e| format!("Can't create {}: {e}", data_dir.display()))?;
    let launch = Launch {
        data_dir,
        port: free_port(),
        token: uuid::Uuid::new_v4().simple().to_string(),
        qdrant: sidecar("qdrant").map(|_| (free_port(), free_port())),
    };
    let mut launch = launch;
    let state = app.state::<Shared>();
    if let Some((http, _)) = launch.qdrant {
        let mut q = spawn_qdrant(&launch);
        if wait_qdrant(http, &mut q) {
            state.0.lock().unwrap().qdrant = q;
        } else {
            // The Qdrant server can't run here: use the engine's embedded vector store instead of failing.
            if let Some(mut child) = q {
                let _ = child.kill();
            }
            launch.qdrant = None;
        }
    }
    let engine = spawn_engine(&launch)?;
    {
        let mut s = state.0.lock().unwrap();
        s.engine = Some(engine);
        s.url = format!("http://127.0.0.1:{}", launch.port);
        s.token = launch.token.clone();
        s.launch = Some(launch.clone());
    }
    wait_engine(app, &launch)
}

/// Restart either service when it stops, a few times at most so a crash loop can't spin forever.
fn supervise(app: tauri::AppHandle) {
    loop {
        std::thread::sleep(Duration::from_secs(2));
        let state = app.state::<Shared>();
        let (launch, engine_down, qdrant_down) = {
            let mut s = state.0.lock().unwrap();
            if s.stopping {
                return;
            }
            let Some(launch) = s.launch.clone() else { continue };
            let engine_down = s.engine.as_mut().map_or(false, |c| c.try_wait().ok().flatten().is_some());
            let qdrant_down = s.qdrant.as_mut().map_or(false, |c| c.try_wait().ok().flatten().is_some());
            (launch, engine_down, qdrant_down)
        };
        if !engine_down && !qdrant_down {
            continue;
        }
        {
            let mut s = state.0.lock().unwrap();
            s.restarts.retain(|t| t.elapsed() < RESTART_WINDOW);
            if s.restarts.len() >= MAX_RESTARTS {
                s.ready = false;
                s.error = Some(format!(
                    "The engine stopped {MAX_RESTARTS} times in 10 minutes. Close and reopen KeiroLake; logs are in {}",
                    launch.data_dir.join("logs").display()
                ));
                return;
            }
            s.restarts.push(Instant::now());
            s.ready = false;
        }
        if qdrant_down {
            let mut q = spawn_qdrant(&launch);
            if let Some((http, _)) = launch.qdrant {
                wait_qdrant(http, &mut q);
            }
            state.0.lock().unwrap().qdrant = q;
        }
        if engine_down {
            match spawn_engine(&launch) {
                Ok(child) => state.0.lock().unwrap().engine = Some(child),
                Err(e) => {
                    state.0.lock().unwrap().error = Some(e);
                    continue;
                }
            }
        }
        if let Err(e) = wait_engine(&app, &launch) {
            state.0.lock().unwrap().error = Some(e);
        } else {
            let _ = app.emit_to("main", "engine-restarted", ());
        }
    }
}

#[tauri::command]
fn engine_info(state: tauri::State<Shared>) -> EngineInfo {
    let s = state.0.lock().unwrap();
    EngineInfo { url: s.url.clone(), token: s.token.clone(), ready: s.ready, error: s.error.clone(), version: env!("CARGO_PKG_VERSION").into() }
}

/// Open a live web page in its own window (scripts run, so JavaScript-heavy sites render).
#[tauri::command]
fn open_web(app: tauri::AppHandle, url: String) -> Result<(), String> {
    let parsed: tauri::Url = url.parse().map_err(|_| "Not a valid address".to_string())?;
    if parsed.scheme() != "https" && parsed.scheme() != "http" {
        return Err("Only http and https pages can be opened".into());
    }
    if let Some(win) = app.get_webview_window("web") {
        win.navigate(parsed).map_err(|e| e.to_string())?;
        let _ = win.set_focus();
        return Ok(());
    }
    WebviewWindowBuilder::new(&app, "web", WebviewUrl::External(parsed))
        .title("KeiroLake · Web")
        .inner_size(1100.0, 800.0)
        .build()
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// Ask the live window to hand back its rendered HTML. Only a page carrying this nonce is accepted.
#[tauri::command]
fn capture_web(app: tauri::AppHandle, state: tauri::State<Shared>, kb: String) -> Result<(), String> {
    let win = app.get_webview_window("web").ok_or("Open a live page first")?;
    let nonce = uuid::Uuid::new_v4().simple().to_string();
    state.0.lock().unwrap().pending_capture = Some((nonce.clone(), kb));
    let js = format!(
        "(function(){{try{{window.__TAURI_INTERNALS__.invoke('web_captured',{{nonce:'{nonce}',url:location.href,title:document.title,html:document.documentElement.outerHTML}});}}catch(e){{}}}})();"
    );
    win.eval(&js).map_err(|e| e.to_string())
}

#[tauri::command]
fn web_captured(app: tauri::AppHandle, state: tauri::State<Shared>, nonce: String, url: String, title: String, html: String) -> Result<(), String> {
    let kb = {
        let mut s = state.0.lock().unwrap();
        match s.pending_capture.take() {
            Some((expected, kb)) if expected == nonce => kb,
            other => {
                s.pending_capture = other;
                return Err("Unexpected capture".into());
            }
        }
    };
    if html.len() > 8 * 1024 * 1024 {
        return Err("Page is too large to save".into());
    }
    app.emit_to("main", "page-captured", CapturedPage { url, title, html, kb }).map_err(|e| e.to_string())
}

#[tauri::command]
async fn pick_folder(app: tauri::AppHandle) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    app.dialog().file().blocking_pick_folder().map(|p| p.to_string())
}

/// Show the data or logs folder in the system file manager.
#[tauri::command]
fn open_data_folder(state: tauri::State<Shared>, sub: Option<String>) -> Result<(), String> {
    let s = state.0.lock().unwrap();
    let base = s.launch.as_ref().map(|l| l.data_dir.clone()).ok_or("The engine hasn't started yet")?;
    let path = match sub.as_deref() {
        Some("logs") => base.join("logs"),
        Some("backups") => base.join("backups"),
        Some("lake") => base.join("lake"),
        _ => base,
    };
    let _ = std::fs::create_dir_all(&path);
    let opener = if cfg!(windows) { "explorer" } else { "xdg-open" };
    Command::new(opener).arg(&path).spawn().map(|_| ()).map_err(|e| e.to_string())
}

/// POST to the engine with the launch token and return the response body.
fn engine_post(port: u16, token: &str, path: &str) -> Option<String> {
    let mut stream = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_secs(2)).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(20))).ok()?;
    let req = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-KeiroLake-Token: {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut raw = String::new();
    stream.read_to_string(&mut raw).ok()?;
    let (head, body) = raw.split_once("\r\n\r\n")?;
    head.starts_with("HTTP/1.1 200").then(|| body.to_string())
}

/// Open the app in the default browser, signed in with a one-time code (no password on this PC).
#[tauri::command]
fn open_in_browser(state: tauri::State<Shared>) -> Result<(), String> {
    let (port, token) = {
        let s = state.0.lock().unwrap();
        let l = s.launch.as_ref().ok_or("The engine hasn't started yet")?;
        (l.port, l.token.clone())
    };
    let body = engine_post(port, &token, "/api/auth/open-link").ok_or("The browser link couldn't be made")?;
    // The body may be chunked; take the JSON object out of it.
    let json = body.find('{').and_then(|a| body.rfind('}').map(|b| &body[a..=b])).ok_or("Unexpected reply from the engine")?;
    let url = serde_json::from_str::<serde_json::Value>(json)
        .ok()
        .and_then(|v| v.get("url").and_then(|u| u.as_str().map(String::from)))
        .ok_or("The engine returned no link")?;
    let opener = if cfg!(windows) { "explorer" } else { "xdg-open" };
    Command::new(opener).arg(&url).spawn().map(|_| ()).map_err(|e| e.to_string())
}

#[tauri::command]
fn autostart_get(app: tauri::AppHandle) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false)
}

#[tauri::command]
fn autostart_set(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let launcher = app.autolaunch();
    (if enabled { launcher.enable() } else { launcher.disable() }).map_err(|e| e.to_string())
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open KeiroLake", true, None::<&str>)?;
    let browser = MenuItem::with_id(app, "browser", "Open in browser", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &browser, &quit])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("KeiroLake")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main(app),
            "browser" => {
                let _ = open_in_browser(app.state::<Shared>());
            }
            "quit" => {
                stop_children(app);
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

// ---------- updates: signed releases from GitHub, no server of our own ----------

#[derive(serde::Serialize)]
struct UpdateInfo {
    current: String,
    available: bool,
    version: Option<String>,
    notes: Option<String>,
    date: Option<String>,
    /// How this copy was installed: "installer" (Windows), "appimage", or "package" (.deb/.rpm, updated by the system).
    install: String,
}

fn install_kind() -> &'static str {
    if cfg!(windows) {
        "installer"
    } else if std::env::var_os("APPIMAGE").is_some() {
        "appimage"
    } else {
        "package"
    }
}

/// Ask GitHub Releases whether a newer signed version exists.
#[tauri::command]
async fn update_check(app: tauri::AppHandle) -> Result<UpdateInfo, String> {
    use tauri_plugin_updater::UpdaterExt;
    let current = app.package_info().version.to_string();
    let found = app.updater().map_err(|e| e.to_string())?.check().await.map_err(|e| e.to_string())?;
    Ok(match found {
        Some(u) => UpdateInfo {
            current,
            available: true,
            version: Some(u.version.clone()),
            notes: u.body.clone(),
            date: u.date.map(|d| d.to_string()),
            install: install_kind().into(),
        },
        None => UpdateInfo { current, available: false, version: None, notes: None, date: None, install: install_kind().into() },
    })
}

/// Download the update (progress goes to the page as "update-progress"), check its signature, stop the engine so its
/// files can be replaced, install, and start again.
#[tauri::command]
async fn update_install(app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_updater::UpdaterExt;
    if install_kind() == "package" {
        return Err("This copy was installed from a .deb or .rpm package. Download the new package from the Releases page.".into());
    }
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "KeiroLake is up to date".to_string())?;
    let progress = app.clone();
    let mut received: u64 = 0;
    let bytes = update
        .download(
            move |chunk, total| {
                received += chunk as u64;
                let _ = progress.emit("update-progress", (received, total));
            },
            || {},
        )
        .await
        .map_err(|e| e.to_string())?;
    stop_children(&app); // the installer replaces the engine and Qdrant, which can't be in use
    update.install(bytes).map_err(|e| e.to_string())?;
    app.restart();
}

/// Start again (after a .deb/.rpm update installed a new version): stop the engine and Qdrant, then relaunch.
#[tauri::command]
fn relaunch(app: tauri::AppHandle) {
    stop_children(&app);
    app.restart();
}

fn stop_children(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Shared>() {
        let mut s = state.0.lock().unwrap();
        s.stopping = true;
        for child in [s.engine.take(), s.qdrant.take()].into_iter().flatten() {
            kill_tree(child);
        }
    }
}

/// Windows paints the title bar and a 1 px border in the accent colour (red on some systems). KeiroLake paints them in its
/// own colours instead, matching the light or dark theme, and asks for no coloured border.
#[cfg(windows)]
fn paint_frame(win: &tauri::WebviewWindow, dark: bool) {
    #[link(name = "dwmapi")]
    extern "system" {
        fn DwmSetWindowAttribute(hwnd: isize, attribute: u32, value: *const core::ffi::c_void, size: u32) -> i32;
    }
    const DWMWA_USE_IMMERSIVE_DARK_MODE: u32 = 20;
    const DWMWA_BORDER_COLOR: u32 = 34;
    const DWMWA_CAPTION_COLOR: u32 = 35;
    const DWMWA_TEXT_COLOR: u32 = 36;
    const DWMWA_COLOR_NONE: u32 = 0xFFFF_FFFE;
    // COLORREF is 0x00BBGGRR. Light: white bar, near-black text. Dark: the app's #161b22 panel, #e6edf3 text.
    let (caption, text): (u32, u32) = if dark { (0x00221B16, 0x00F3EDE6) } else { (0x00FFFFFF, 0x00271811) };
    let immersive: u32 = dark as u32;
    if let Ok(hwnd) = win.hwnd() {
        let h = hwnd.0 as isize;
        let set = |attr: u32, value: &u32| unsafe {
            DwmSetWindowAttribute(h, attr, value as *const u32 as *const core::ffi::c_void, 4);
        };
        // Windows versions without these attributes ignore them and keep their usual frame.
        set(DWMWA_USE_IMMERSIVE_DARK_MODE, &immersive);
        set(DWMWA_CAPTION_COLOR, &caption);
        set(DWMWA_TEXT_COLOR, &text);
        set(DWMWA_BORDER_COLOR, &DWMWA_COLOR_NONE);
    }
}

/// The app tells the shell which theme is showing, so the window frame matches it.
#[tauri::command]
fn window_theme(app: tauri::AppHandle, dark: bool) {
    #[cfg(windows)]
    if let Some(win) = app.get_webview_window("main") {
        paint_frame(&win, dark);
    }
    #[cfg(not(windows))]
    let _ = (app, dark);
}

fn main() {
    // `keirolake-desktop mcp`: a stdio MCP server for Claude Desktop and similar apps, without opening a window here.
    if std::env::args().nth(1).as_deref() == Some("mcp") {
        std::process::exit(mcp_bridge::run());
    }
    #[cfg(target_os = "linux")]
    std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, Some(vec!["--hidden"])))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Shared(Mutex::new(EngineState::default())))
        .invoke_handler(tauri::generate_handler![engine_info, open_web, capture_web, web_captured, pick_folder, open_data_folder, open_in_browser, autostart_get, autostart_set, window_theme, update_check, update_install, relaunch])
        .on_window_event(|window, event| {
            // Closing the window hides it; the engine keeps running in the tray. Quit is in the tray menu.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .setup(|app| {
            setup_tray(app)?;
            #[cfg(windows)]
            if let Some(win) = app.get_webview_window("main") {
                paint_frame(&win, false);  // light until the page says otherwise
            }
            if std::env::args().any(|a| a == "--hidden") {
                if let Some(win) = app.get_webview_window("main") {
                    let _ = win.hide();
                }
            }
            // First run: start at login (hidden in the tray). After that the user's choice in Settings is kept.
            if let Ok(dir) = app.path().app_data_dir() {
                let marker = dir.join(".autostart-configured");
                if !marker.exists() {
                    let _ = std::fs::create_dir_all(&dir);
                    let _ = app.autolaunch().enable();
                    let _ = std::fs::write(marker, b"1");
                }
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                match boot(&handle) {
                    Ok(()) => supervise(handle),
                    Err(err) => handle.state::<Shared>().0.lock().unwrap().error = Some(err),
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("could not start KeiroLake")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_children(app);
            }
        });
}
