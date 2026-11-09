//! Studio for macOS: a native window around the studio's own UI and server.
//!
//! The window shows a local splash, finds or starts the studio server for the library
//! (backend.rs), then navigates to it. Everything the UI does still goes through that server,
//! so the browser path (`./studio ui`) and this app behave the same. What the app adds is
//! native: menus, the file dialog, drops of real file paths (no upload), notifications,
//! window memory and a single instance.

mod backend;
#[cfg(target_os = "macos")]
mod devhook;
#[cfg(target_os = "macos")]
mod dragdrop;

use std::path::PathBuf;
use std::sync::Mutex;

use serde_json::json;
use tauri::menu::{AboutMetadataBuilder, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{
    AppHandle, DragDropEvent, Emitter, Manager, RunEvent, UserAttentionType, WebviewUrl,
    WebviewWindow, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;

use backend::{log, Backend};

const MEDIA_EXT: &[&str] = &["mov", "mp4", "m4v", "mkv", "webm", "avi", "m4a", "mp3", "wav", "aac", "caf", "ogg", "flac"];

#[derive(Default)]
struct AppState {
    backend: Mutex<Option<Backend>>,
    path: Mutex<String>,
    starting: Mutex<bool>,
}

fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window("main")
}

fn splash(app: &AppHandle, call: &str, args: &[&str]) {
    if let Some(w) = main_window(app) {
        let a: Vec<String> = args.iter().map(|s| serde_json::to_string(s).unwrap()).collect();
        let _ = w.eval(format!("window.studioSplash && studioSplash.{call}({})", a.join(",")));
    }
}

/// Find or start the server for `home`, then show it. Runs off the main thread.
fn open_library(app: AppHandle, home: PathBuf) {
    {
        let state = app.state::<AppState>();
        let mut s = state.starting.lock().unwrap();
        if *s {
            return;
        }
        *s = true;
    }
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        // the old library's server goes first (only if we started it)
        if let Some(mut old) = state.backend.lock().unwrap().take() {
            old.stop();
        }
        let path = {
            let mut p = state.path.lock().unwrap();
            if p.is_empty() {
                splash(&app, "status", &["Opening your studio…", "Finding your tools…"]);
                let login = backend::login_path();
                if login.is_none() {
                    log("couldn't read the login shell's PATH; the agent CLI may not be found");
                }
                *p = backend::merged_path(login.as_deref());
                match backend::find_agent(&p) {
                    (a, Some(c)) => log(&format!("agent {a}: {}", c.display())),
                    (a, None) => log(&format!("agent {a}: not found on the login PATH (Find moments won't work)")),
                }
            }
            p.clone()
        };
        splash(&app, "status", &["Opening your studio…", &format!("Library: {}", home.display())]);
        match backend::attach_or_start(&home, &path) {
            Ok(b) => {
                let url = b.url();
                let title = format!("Studio — {}", home.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
                *state.backend.lock().unwrap() = Some(b);
                if let Some(w) = main_window(&app) {
                    let _ = w.set_title(&title);
                    if let Ok(u) = url.parse() {
                        let _ = w.navigate(u);
                    }
                }
            }
            Err((title, detail)) => {
                log(&format!("{title}: {detail}"));
                splash(&app, "error", &[&title, &format!("Library: {}", home.display()), &detail]);
            }
        }
        *state.starting.lock().unwrap() = false;
    });
}

fn config_dir(app: &AppHandle) -> PathBuf {
    app.path().app_config_dir().unwrap_or_else(|_| PathBuf::from("/tmp/studio-app"))
}

fn current_home(app: &AppHandle) -> Option<PathBuf> {
    app.state::<AppState>().backend.lock().unwrap().as_ref().map(|b| b.home.clone())
}

/// Studio ▸ Settings…: pick the library folder; remembered for the next launch.
fn choose_library(app: AppHandle) {
    std::thread::spawn(move || {
        let mut d = app.dialog().file().set_title("Choose the Studio library folder");
        if let Some(h) = current_home(&app) {
            d = d.set_directory(h);
        }
        let Some(picked) = d.blocking_pick_folder() else { return };
        let Ok(home) = picked.into_path() else { return };
        if let Err(e) = backend::save_home(&config_dir(&app), &home) {
            log(&format!("couldn't save settings: {e}"));
        }
        log(&format!("library → {}", home.display()));
        if let Some(w) = main_window(&app) {
            let _ = w.navigate("tauri://localhost/index.html".parse().unwrap());
        }
        open_library(app, home);
    });
}

fn readme() -> PathBuf {
    match option_env!("STUDIO_SHARE") {
        Some(s) => PathBuf::from(s).join("README.md"),
        None => PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../README.md"),
    }
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let about = AboutMetadataBuilder::new()
        .name(Some("Studio"))
        .version(Some(env!("CARGO_PKG_VERSION")))
        .comments(Some("Raw footage in; a finished vertical video and post drafts out. Everything runs on this Mac."))
        .build();
    let item = |id: &str, text: &str, accel: Option<&str>| MenuItem::with_id(app, id, text, true, accel);
    let sep = || PredefinedMenuItem::separator(app);

    let studio = Submenu::with_items(app, "Studio", true, &[
        &PredefinedMenuItem::about(app, Some("About Studio"), Some(about))?,
        &sep()?,
        &item("settings", "Settings… (Library Folder)", Some("CmdOrCtrl+,"))?,
        &sep()?,
        &PredefinedMenuItem::services(app, None)?,
        &sep()?,
        &PredefinedMenuItem::hide(app, Some("Hide Studio"))?,
        &PredefinedMenuItem::hide_others(app, None)?,
        &PredefinedMenuItem::show_all(app, None)?,
        &sep()?,
        &PredefinedMenuItem::quit(app, Some("Quit Studio"))?,
    ])?;
    let file = Submenu::with_items(app, "File", true, &[
        &item("new-project", "New Project…", Some("CmdOrCtrl+N"))?,
        &item("add-footage", "Add Footage…", Some("CmdOrCtrl+O"))?,
        &item("import", "Import…", Some("CmdOrCtrl+Shift+O"))?,
        &item("share", "Share Project…", Some("CmdOrCtrl+Shift+S"))?,
        &sep()?,
        &item("export", "Export", Some("CmdOrCtrl+E"))?,
        &sep()?,
        &item("show-library", "Show Library in Finder", None)?,
        &sep()?,
        &PredefinedMenuItem::close_window(app, None)?,
    ])?;
    // Undo/Redo are ours, not the predefined ones: the UI decides whether ⌘Z undoes text (in
    // a text field) or the story. Cut/Copy/Paste/Select All are the native ones, which go to
    // whatever has focus in the page.
    let edit = Submenu::with_items(app, "Edit", true, &[
        &item("undo", "Undo", Some("CmdOrCtrl+Z"))?,
        &item("redo", "Redo", Some("CmdOrCtrl+Shift+Z"))?,
        &sep()?,
        &PredefinedMenuItem::cut(app, None)?,
        &PredefinedMenuItem::copy(app, None)?,
        &PredefinedMenuItem::paste(app, None)?,
        &PredefinedMenuItem::select_all(app, None)?,
    ])?;
    let view = Submenu::with_items(app, "View", true, &[
        &item("zoom-fit", "Zoom Story to Fit", None)?,
        &item("toggle-captions", "Toggle Captions", None)?,
        &sep()?,
        &PredefinedMenuItem::fullscreen(app, None)?,
    ])?;
    let window = Submenu::with_items(app, "Window", true, &[
        &PredefinedMenuItem::minimize(app, None)?,
        &PredefinedMenuItem::maximize(app, None)?,
        &sep()?,
        &PredefinedMenuItem::bring_all_to_front(app, None)?,
    ])?;
    let help = Submenu::with_items(app, "Help", true, &[
        &item("help-readme", "Studio Help (README)", None)?,
        &item("open-logs", "Show Logs in Finder", None)?,
    ])?;
    Menu::with_items(app, &[&studio, &file, &edit, &view, &window, &help])
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "settings" => choose_library(app.clone()),
        "show-library" => {
            if let Some(h) = current_home(app) {
                let _ = std::process::Command::new("/usr/bin/open").arg(h).spawn();
            }
        }
        "help-readme" => {
            let _ = std::process::Command::new("/usr/bin/open").arg("-t").arg(readme()).spawn();
        }
        "open-logs" => {
            let _ = std::process::Command::new("/usr/bin/open").arg(backend::log_dir()).spawn();
        }
        // everything else is the UI's: it gets the menu id as an event
        other => {
            if let Some(w) = main_window(app) {
                let _ = w.emit("studio://menu", other);
            }
        }
    }
}

/* ---------- commands the UI calls (granted to http://127.0.0.1:* in capabilities/) ---------- */

/// File ▸ Add Footage… and the "+ Add footage" button: a native picker. The UI sends the paths
/// to POST /api/ingest-paths, like a drop.
#[tauri::command]
async fn pick_footage(app: AppHandle) -> Vec<String> {
    let files = app
        .dialog()
        .file()
        .set_title("Add footage")
        .add_filter("Video and audio", MEDIA_EXT)
        .blocking_pick_files()
        .unwrap_or_default();
    files.into_iter().filter_map(|f| f.into_path().ok()).map(|p| p.to_string_lossy().into_owned()).collect()
}

/// Share Project… (where to save the bundle) and Import… (the shared folder): a native folder
/// picker. The UI sends the path to POST /api/bundle or /api/import.
#[tauri::command]
async fn pick_folder(app: AppHandle, title: Option<String>) -> Option<String> {
    let mut d = app.dialog().file().set_title(title.unwrap_or_else(|| "Choose a folder".into()));
    if let Some(home) = std::env::var_os("HOME") {
        d = d.set_directory(PathBuf::from(home).join("Desktop"));
    }
    let picked = d.blocking_pick_folder()?;
    picked.into_path().ok().map(|p| p.to_string_lossy().into_owned())
}

/// A notification, but only when the window is in the background (in front, the UI's own
/// toast says it). Returns whether one was shown.
#[tauri::command]
fn notify(app: AppHandle, title: String, body: String) -> bool {
    let Some(w) = main_window(&app) else { return false };
    if w.is_focused().unwrap_or(false) && !w.is_minimized().unwrap_or(false) {
        return false;
    }
    let shown = app.notification().builder().title(title).body(body).show().is_ok();
    let _ = w.request_user_attention(Some(UserAttentionType::Informational));
    shown
}

#[tauri::command]
fn app_info(app: AppHandle) -> serde_json::Value {
    let st = app.state::<AppState>();
    let b = st.backend.lock().unwrap();
    let path = st.path.lock().unwrap().clone();
    let (agent, agent_path) = backend::find_agent(&path);
    json!({
        "version": env!("CARGO_PKG_VERSION"),
        "home": b.as_ref().map(|b| b.home.clone()),
        "url": b.as_ref().map(|b| b.url()),
        "attached": b.as_ref().map(|b| b.attached),
        "server_pid": b.as_ref().and_then(|b| b.child.as_ref().map(|c| c.id())),
        "server": b.as_ref().map(|b| b.health.clone()),
        "agent": {"name": agent, "path": agent_path},
        "logs": backend::log_dir(),
    })
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            log(&format!("second launch ({argv:?}): showing the open window"));
            if let Some(w) = main_window(app) {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .manage(AppState::default())
        .menu(build_menu)
        .on_menu_event(|app, e| on_menu(app, e.id().as_ref()))
        .invoke_handler(tauri::generate_handler![pick_footage, pick_folder, notify, app_info])
        .setup(|app| {
            let handle = app.handle().clone();
            let w = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Studio")
                .inner_size(1500.0, 960.0)
                .min_inner_size(1024.0, 660.0)
                .background_color(tauri::window::Color(17, 17, 19, 255))
                .on_navigation(|url| {
                    // the studio and the splash stay in the window; any other link opens in the
                    // browser instead of replacing the studio
                    let local = url.scheme() == "tauri" || url.host_str() == Some("127.0.0.1") || url.host_str() == Some("localhost");
                    if !local && (url.scheme() == "http" || url.scheme() == "https") {
                        let _ = std::process::Command::new("/usr/bin/open").arg(url.as_str()).spawn();
                    }
                    local
                })
                .build()?;
            #[cfg(target_os = "macos")]
            {
                let _ = w.with_webview(|pw| {
                    if !dragdrop::install(pw.inner()) {
                        log("drag routing not installed: internal drags may not reach the page");
                    }
                });
            }
            #[cfg(not(target_os = "macos"))]
            let _ = w;
            let saved = backend::load_saved_home(&config_dir(&handle));
            let home = backend::resolve_home(saved);
            log(&format!("launch: library {}", home.display()));
            #[cfg(target_os = "macos")]
            devhook::start(&handle);
            open_library(handle, home);
            Ok(())
        })
        .on_window_event(|win, ev| {
            if let WindowEvent::DragDrop(d) = ev {
                // file drags (dragdrop.rs routes only those here): the UI shows its drop zone
                // and sends the paths to POST /api/ingest-paths
                let payload = match d {
                    DragDropEvent::Enter { paths, .. } => json!({"type": "enter", "paths": paths}),
                    DragDropEvent::Over { .. } => return,
                    DragDropEvent::Drop { paths, .. } => json!({"type": "drop", "paths": paths}),
                    DragDropEvent::Leave => json!({"type": "leave"}),
                    _ => return,
                };
                if let DragDropEvent::Drop { paths, .. } = d {
                    log(&format!("drop: {} path(s)", paths.len()));
                }
                let _ = win.emit("studio://drag", payload);
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Studio");

    app.run(|app, ev| {
        if let RunEvent::Exit = ev {
            if let Some(mut b) = app.state::<AppState>().backend.lock().unwrap().take() {
                if !b.attached {
                    log(&format!("quitting: stopping the server we started (port {})", b.port));
                }
                b.stop();
            }
        }
    });
}
