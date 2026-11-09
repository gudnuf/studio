//! A test hook, off unless STUDIO_APP_DEVHOOK=<dir> is set in the app's environment (a web
//! page can't set that). It lets a script drive the real app with nothing but files, which
//! works even when the screen is locked and no mouse or screenshot is possible:
//!
//!   <dir>/in/NAME.js    run in the page (an async function body); the result (or the error)
//!                       lands in <dir>/out/NAME.json
//!   <dir>/in/NAME.snap  a snapshot of what the webview shows → <dir>/out/NAME.png
//!   <dir>/in/NAME.drop  one absolute path per line: dropped onto the window through the real
//!                       NSDraggingDestination entry points (draggingEntered: +
//!                       performDragOperation:) with a pasteboard of file URLs, so the native
//!                       route (dragdrop.rs → wry → Tauri → the UI → /api/ingest-paths) runs
//!                       exactly as for a drag from Finder → <dir>/out/NAME.json {op, accepted}
//!
//! Handled files move to <dir>/done/.

use std::ffi::CString;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicPtr, Ordering};
use std::time::Duration;

use block2::RcBlock;
use objc2::encode::{Encode, Encoding};
use objc2::runtime::{AnyClass, AnyObject, Bool, ClassBuilder, Sel};
use objc2::{class, msg_send, sel};
use tauri::{AppHandle, Listener, Manager};

pub fn start(app: &AppHandle) {
    let Ok(dir) = std::env::var("STUDIO_APP_DEVHOOK") else { return };
    let dir = PathBuf::from(dir);
    for d in ["in", "out", "done"] {
        let _ = fs::create_dir_all(dir.join(d));
    }
    crate::backend::log(&format!("devhook: watching {}", dir.display()));
    let out = dir.join("out");
    app.listen_any("studio://devhook", move |ev| {
        let v: serde_json::Value = serde_json::from_str(ev.payload()).unwrap_or_default();
        let name = v.get("name").and_then(|n| n.as_str()).unwrap_or("result").to_string();
        let _ = fs::write(out.join(format!("{name}.json")), v.to_string());
    });
    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(200));
        let Ok(rd) = fs::read_dir(dir.join("in")) else { continue };
        let mut files: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
        files.sort();
        for f in files {
            handle(&app, &dir, &f);
            if let Some(n) = f.file_name() {
                let _ = fs::rename(&f, dir.join("done").join(n));
            }
        }
    });
}

fn handle(app: &AppHandle, dir: &Path, f: &Path) {
    let Some(w) = app.get_webview_window("main") else { return };
    let name = f.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let body = fs::read_to_string(f).unwrap_or_default();
    match f.extension().and_then(|e| e.to_str()) {
        Some("js") => {
            let n = serde_json::to_string(&name).unwrap();
            let js = format!(
                "(async () => {{ try {{ const v = await (async () => {{ {body}\n }})(); return {{ok: true, value: v === undefined ? null : v}}; }} \
                 catch (e) {{ return {{ok: false, error: String((e && e.stack) || e)}}; }} }})()\
                 .then(r => window.__TAURI__.event.emit('studio://devhook', Object.assign({{name: {n}}}, r)))"
            );
            let _ = w.eval(js);
        }
        Some("snap") => {
            let png = dir.join("out").join(format!("{name}.png"));
            let _ = w.with_webview(move |pw| unsafe { snapshot(pw.inner() as *mut AnyObject, &png) });
        }
        Some("drop") => {
            let paths: Vec<String> = body.lines().map(str::trim).filter(|l| !l.is_empty()).map(String::from).collect();
            let out = dir.join("out").join(format!("{name}.json"));
            let _ = w.with_webview(move |pw| unsafe {
                let (op, ok) = native_drop(pw.inner() as *mut AnyObject, &paths);
                let _ = fs::write(out, serde_json::json!({"op": op, "accepted": ok, "paths": paths}).to_string());
            });
        }
        _ => {}
    }
}

unsafe fn nsstring(s: &str) -> *mut AnyObject {
    let c = CString::new(s).unwrap_or_default();
    msg_send![class!(NSString), stringWithUTF8String: c.as_ptr()]
}

/// WKWebView takeSnapshotWithConfiguration: → PNG on disk.
unsafe fn snapshot(wv: *mut AnyObject, png: &Path) {
    let path = png.to_string_lossy().into_owned();
    let block = RcBlock::new(move |img: *mut AnyObject, _err: *mut AnyObject| {
        if img.is_null() {
            crate::backend::log("devhook: snapshot failed");
            return;
        }
        let tiff: *mut AnyObject = msg_send![img, TIFFRepresentation];
        let rep: *mut AnyObject = msg_send![class!(NSBitmapImageRep), imageRepWithData: tiff];
        let props: *mut AnyObject = msg_send![class!(NSDictionary), dictionary];
        // NSBitmapImageFileTypePNG = 4
        let data: *mut AnyObject = msg_send![rep, representationUsingType: 4usize, properties: props];
        let _: Bool = msg_send![data, writeToFile: nsstring(&path), atomically: Bool::YES];
    });
    let cfg: *mut AnyObject = std::ptr::null_mut();
    let _: () = msg_send![wv, takeSnapshotWithConfiguration: cfg, completionHandler: &*block];
}

/* ---------- a drop with real file URLs on a real pasteboard ---------- */

#[repr(C)]
#[derive(Clone, Copy)]
struct Pt {
    x: f64,
    y: f64,
}
unsafe impl Encode for Pt {
    const ENCODING: Encoding = Encoding::Struct("CGPoint", &[f64::ENCODING, f64::ENCODING]);
}

static PASTEBOARD: AtomicPtr<AnyObject> = AtomicPtr::new(std::ptr::null_mut());

extern "C-unwind" fn info_pasteboard(_this: &AnyObject, _sel: Sel) -> *mut AnyObject {
    PASTEBOARD.load(Ordering::SeqCst)
}
extern "C-unwind" fn info_location(_this: &AnyObject, _sel: Sel) -> Pt {
    Pt { x: 400.0, y: 400.0 }
}
extern "C-unwind" fn info_mask(_this: &AnyObject, _sel: Sel) -> usize {
    1 // NSDragOperationCopy
}

fn info_class() -> &'static AnyClass {
    if let Some(c) = AnyClass::get(c"StudioDevhookDraggingInfo") {
        return c;
    }
    let mut b = ClassBuilder::new(c"StudioDevhookDraggingInfo", class!(NSObject)).expect("class");
    unsafe {
        b.add_method(sel!(draggingPasteboard), info_pasteboard as extern "C-unwind" fn(_, _) -> _);
        b.add_method(sel!(draggingLocation), info_location as extern "C-unwind" fn(_, _) -> _);
        b.add_method(sel!(draggingSourceOperationMask), info_mask as extern "C-unwind" fn(_, _) -> _);
    }
    b.register()
}

unsafe fn native_drop(wv: *mut AnyObject, paths: &[String]) -> (usize, bool) {
    let pb: *mut AnyObject = msg_send![class!(NSPasteboard), pasteboardWithUniqueName];
    let _: isize = msg_send![pb, clearContents];
    let arr: *mut AnyObject = msg_send![class!(NSMutableArray), array];
    for p in paths {
        let url: *mut AnyObject = msg_send![class!(NSURL), fileURLWithPath: nsstring(p)];
        let _: () = msg_send![arr, addObject: url];
    }
    let _: Bool = msg_send![pb, writeObjects: arr];
    PASTEBOARD.store(pb, Ordering::SeqCst);
    let info: *mut AnyObject = msg_send![info_class(), new];
    let op: usize = msg_send![wv, draggingEntered: info];
    let _: usize = msg_send![wv, draggingUpdated: info];
    let ok: Bool = msg_send![wv, performDragOperation: info];
    let _: () = msg_send![pb, releaseGlobally];
    PASTEBOARD.store(std::ptr::null_mut(), Ordering::SeqCst);
    (op, ok.as_bool())
}
