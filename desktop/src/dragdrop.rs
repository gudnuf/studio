//! Drags into the window, routed by what they carry.
//!
//! Tauri's native drop handler (wry's WryWebView) claims every drag that enters the webview,
//! so the page never sees one. That's what we want for files from Finder (Tauri hands us their
//! real paths, so nothing is uploaded), but it also swallows the UI's own HTML5 drags (a moment
//! or a clip of footage onto the Story). So the four NSDraggingDestination methods of the
//! webview's class are replaced with ones that look at the pasteboard: a drag that carries
//! file URLs goes to wry/Tauri as before, anything else goes straight to WKWebView's own
//! implementation, i.e. to the page's dragenter/dragover/drop.

use std::ffi::{c_char, CStr};
use std::sync::OnceLock;

use objc2::ffi;
use objc2::runtime::{AnyClass, AnyObject, Bool, Imp, Sel};
use objc2::{msg_send, sel};

type OpFn = unsafe extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject) -> usize;
type BoolFn = unsafe extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject) -> Bool;
type VoidFn = unsafe extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject);

struct Imps {
    wry_entered: OpFn,
    wry_updated: OpFn,
    wry_perform: BoolFn,
    wry_exited: VoidFn,
    wk_entered: OpFn,
    wk_updated: OpFn,
    wk_perform: BoolFn,
    wk_exited: VoidFn,
}
// IMPs are plain function pointers
unsafe impl Send for Imps {}
unsafe impl Sync for Imps {}

static IMPS: OnceLock<Imps> = OnceLock::new();

/// Does this drag carry files (Finder, the desktop, Photos' "export unmodified original")?
unsafe fn carries_files(info: *mut AnyObject) -> bool {
    if info.is_null() {
        return false;
    }
    let pb: *mut AnyObject = msg_send![info, draggingPasteboard];
    if pb.is_null() {
        return false;
    }
    let types: *mut AnyObject = msg_send![pb, types];
    if types.is_null() {
        return false;
    }
    let n: usize = msg_send![types, count];
    for i in 0..n {
        let t: *mut AnyObject = msg_send![types, objectAtIndex: i];
        if t.is_null() {
            continue;
        }
        let s: *const c_char = msg_send![t, UTF8String];
        if s.is_null() {
            continue;
        }
        let s = CStr::from_ptr(s).to_bytes();
        if s == b"public.file-url" || s == b"NSFilenamesPboardType" {
            return true;
        }
    }
    false
}

unsafe extern "C-unwind" fn entered(this: *mut AnyObject, sel: Sel, info: *mut AnyObject) -> usize {
    let imps = IMPS.get().unwrap();
    if carries_files(info) { (imps.wry_entered)(this, sel, info) } else { (imps.wk_entered)(this, sel, info) }
}
unsafe extern "C-unwind" fn updated(this: *mut AnyObject, sel: Sel, info: *mut AnyObject) -> usize {
    let imps = IMPS.get().unwrap();
    if carries_files(info) { (imps.wry_updated)(this, sel, info) } else { (imps.wk_updated)(this, sel, info) }
}
unsafe extern "C-unwind" fn perform(this: *mut AnyObject, sel: Sel, info: *mut AnyObject) -> Bool {
    let imps = IMPS.get().unwrap();
    if carries_files(info) { (imps.wry_perform)(this, sel, info) } else { (imps.wk_perform)(this, sel, info) }
}
unsafe extern "C-unwind" fn exited(this: *mut AnyObject, sel: Sel, info: *mut AnyObject) {
    let imps = IMPS.get().unwrap();
    if carries_files(info) { (imps.wry_exited)(this, sel, info) } else { (imps.wk_exited)(this, sel, info) }
}

unsafe fn imp_of(cls: &AnyClass, s: Sel) -> Option<(Imp, *const c_char)> {
    let m = ffi::class_getInstanceMethod(cls, s);
    if m.is_null() {
        return None;
    }
    Some((ffi::method_getImplementation(m)?, ffi::method_getTypeEncoding(m)))
}

/// Install the routing on the class of `webview` (the WKWebView pointer Tauri gives us).
/// Returns false (and changes nothing) if the classes don't look the way we expect.
pub fn install(webview: *mut std::ffi::c_void) -> bool {
    if IMPS.get().is_some() {
        return true;
    }
    unsafe {
        let obj = webview as *mut AnyObject;
        if obj.is_null() {
            return false;
        }
        let cls: &AnyClass = (*obj).class();
        let Some(wk) = AnyClass::get(c"WKWebView") else { return false };
        if std::ptr::eq(cls, wk) {
            return false; // not wry's subclass: nothing to route
        }
        let sels = [sel!(draggingEntered:), sel!(draggingUpdated:), sel!(performDragOperation:), sel!(draggingExited:)];
        let mut wry = Vec::new();
        let mut wkv = Vec::new();
        for s in sels {
            let (Some(a), Some(b)) = (imp_of(cls, s), imp_of(wk, s)) else { return false };
            if a.0 as usize == b.0 as usize {
                return false; // wry didn't override it: nothing to route
            }
            wry.push(a);
            wkv.push(b.0);
        }
        let imps = Imps {
            wry_entered: std::mem::transmute::<Imp, OpFn>(wry[0].0),
            wry_updated: std::mem::transmute::<Imp, OpFn>(wry[1].0),
            wry_perform: std::mem::transmute::<Imp, BoolFn>(wry[2].0),
            wry_exited: std::mem::transmute::<Imp, VoidFn>(wry[3].0),
            wk_entered: std::mem::transmute::<Imp, OpFn>(wkv[0]),
            wk_updated: std::mem::transmute::<Imp, OpFn>(wkv[1]),
            wk_perform: std::mem::transmute::<Imp, BoolFn>(wkv[2]),
            wk_exited: std::mem::transmute::<Imp, VoidFn>(wkv[3]),
        };
        if IMPS.set(imps).is_err() {
            return true;
        }
        let cls_mut = cls as *const AnyClass as *mut AnyClass;
        let news: [Imp; 4] = [
            std::mem::transmute::<OpFn, Imp>(entered as OpFn),
            std::mem::transmute::<OpFn, Imp>(updated as OpFn),
            std::mem::transmute::<BoolFn, Imp>(perform as BoolFn),
            std::mem::transmute::<VoidFn, Imp>(exited as VoidFn),
        ];
        for (i, s) in sels.into_iter().enumerate() {
            ffi::class_replaceMethod(cls_mut, s, news[i], wry[i].1);
        }
        true
    }
}
