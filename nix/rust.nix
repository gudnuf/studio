# The Rust toolchain for the v2 workspace (crates/), shared by the devshell and CI.
# Pinned stable via rust-overlay (rustup is not used): cargo, rustc, clippy, rustfmt and
# rust-src, plus the three iOS targets on darwin so `cargo build --target aarch64-apple-ios
# {,-sim}` / x86_64-apple-ios link against a real std. 1.95.0 matches ~/athanor-app and
# ~/murmur-rmp, the rmp references this workspace copies from.
#
# The iOS cross builds themselves use the system Xcode clang/SDK (see apps/apple/build-ffi.sh
# once it exists, and ~/athanor-app/apps/ios/build-ffi.sh): the nix cc-wrapper targets macOS.
{ pkgs, rust-overlay }:
let
  rpkgs = pkgs.extend rust-overlay.overlays.default;
  isDarwin = pkgs.stdenv.hostPlatform.isDarwin;
  toolchain = rpkgs.rust-bin.stable."1.95.0".default.override {
    extensions = [ "rust-src" "clippy" "rustfmt" ];
    targets = pkgs.lib.optionals isDarwin [
      "aarch64-apple-ios"
      "aarch64-apple-ios-sim"
      "x86_64-apple-ios"
    ];
  };
in {
  inherit toolchain;
  # cmake + libclang: whisper-rs-sys (crates/stt `--features whisper`) builds whisper.cpp
  # and runs bindgen.
  packages = [ toolchain pkgs.rust-analyzer pkgs.cmake ];
  env = {
    LIBCLANG_PATH = "${pkgs.llvmPackages.libclang.lib}/lib";
    RUST_SRC_PATH = "${toolchain}/lib/rustlib/src/rust/library";
  };
}
