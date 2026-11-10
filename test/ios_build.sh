#!/usr/bin/env bash
# Cross-builds the Rust workspace for the Apple targets, the way apps/apple/build-ffi.sh will:
# cargo/rustc from the nix toolchain (nix/rust.nix), but C compiler, archiver, linker and SDK
# from the system Xcode (the nix cc-wrapper targets macOS and links the MacOSX .tbd stubs).
# Rules copied from ~/athanor-app/apps/ios/build-ffi.sh: run from a LOGIN shell, never inside
# `nix develop` (a nix DEVELOPER_DIR has no iOS SDKs); /usr/bin first so the real xcrun wins;
# leave NIX_* alone (host build scripts need them).
#
#   test/ios_build.sh [--whisper] [TARGET...]
#     TARGET   default: aarch64-apple-darwin aarch64-apple-ios aarch64-apple-ios-sim
#     --whisper  also build crates/stt with the native whisper.cpp backend (cmake, Metal)
set -euo pipefail

if [ -n "${IN_NIX_SHELL:-}" ] || case "${DEVELOPER_DIR:-}" in /nix/store/*) true ;; *) false ;; esac; then
  echo "!! Run this from a login shell, NOT inside 'nix develop' (nix DEVELOPER_DIR: ${DEVELOPER_DIR:-<in-nix-shell>})." >&2
  exit 1
fi

cd "$(dirname "$0")/.."
whisper=0
targets=()
for a in "$@"; do
  case $a in
    --whisper) whisper=1 ;;
    -*) echo "unknown arg $a" >&2; exit 2 ;;
    *) targets+=("$a") ;;
  esac
done
[ ${#targets[@]} -gt 0 ] || targets=(aarch64-apple-darwin aarch64-apple-ios aarch64-apple-ios-sim)

if [ -z "${DEVELOPER_DIR:-}" ]; then
  DEVELOPER_DIR="$(xcode-select -p)"
fi
export DEVELOPER_DIR

for t in "${targets[@]}"; do
  case $t in
    aarch64-apple-darwin) sdk=macosx ;;
    aarch64-apple-ios) sdk=iphoneos ;;
    aarch64-apple-ios-sim|x86_64-apple-ios) sdk=iphonesimulator ;;
    *) echo "unsupported target $t" >&2; exit 2 ;;
  esac
  envprefix=$(echo "$t" | tr '-' '_')
  ENVUP=$(echo "$envprefix" | tr '[:lower:]' '[:upper:]')
  echo "==> $t (sdk $sdk)"
  cmds="cargo build --workspace --target $t"
  [ $whisper = 1 ] && cmds="$cmds && cargo build -p stt --features whisper --target $t"
  nix develop --quiet -c bash -c '
    set -euo pipefail
    export PATH="/usr/bin:$PATH"
    export DEVELOPER_DIR="'"$DEVELOPER_DIR"'"
    SDKROOT=$(/usr/bin/xcrun --sdk '"$sdk"' --show-sdk-path)
    export SDKROOT
    export CC_'"$envprefix"'=/usr/bin/clang CXX_'"$envprefix"'=/usr/bin/clang++ AR_'"$envprefix"'=/usr/bin/ar
    export CARGO_TARGET_'"$ENVUP"'_LINKER=/usr/bin/clang
    '"$cmds"'
  ' 2> >(grep -v "Git tree .* is dirty" >&2)
done
echo "==> ok: ${targets[*]}"
