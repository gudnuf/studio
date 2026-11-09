fn main() {
    // rebuild when the nix build bakes in a different backend
    println!("cargo:rerun-if-env-changed=STUDIO_BACKEND");
    println!("cargo:rerun-if-env-changed=STUDIO_SHARE");
    // The UI is served by the studio server (a remote origin as far as Tauri is concerned), so
    // the app's own commands need an ACL manifest: capabilities/default.json grants them to
    // http://127.0.0.1:* and nothing else.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["pick_footage", "pick_folder", "notify", "app_info"]),
    ))
    .expect("tauri-build failed");
}
