fn main() {
    // Re-embed the web UI whenever it is rebuilt (cargo doesn't watch frontendDist on its own).
    println!("cargo:rerun-if-changed=../../frontend/dist");
    // `web_captured` is the only command a remote page (the live web window) may call;
    // listing it here generates the `allow-web-captured` permission used in capabilities/web.json.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["engine_info", "open_web", "capture_web", "web_captured", "pick_folder", "open_data_folder", "open_in_browser", "autostart_get", "autostart_set", "window_theme", "update_check", "update_install", "relaunch"]),
        ),
    )
    .expect("failed to run tauri build script");
}
