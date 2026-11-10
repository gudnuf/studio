# hzrd149/blossom-server (Deno), the reference Blossom server: BUD-01/02/04/05/06/09/11, HTTP
# range GETs, pubkey-scoped upload rules, streamed uploads, local or S3 storage, SQLite metadata.
#
# Chosen over the crates.io `blossom-server` 0.6.0 (blossom-rs) by spike S4: that one has no
# `HEAD /upload` (BUD-06), answers a `Range` GET with the whole blob, and holds every blob in memory
# under one global lock. See ~/athanor/forge/studio-app/spikes/S4.md.
#
# Upstream's own nix/package.nix, with the one thing it can't do for us: its flake builds for
# x86_64-linux only, and the vendored npm tree (sharp ships per-platform binaries) hashes
# differently per system. `src` and the deno2nix overlay come from the `blossom-server` flake input.
{ pkgs, src, version }:
let
  inherit (pkgs) lib;
  system = pkgs.stdenv.hostPlatform.system;

  # Fixed-output hashes of `deno install` per platform. A new system: build once, copy the
  # "got:" hash here.
  denoDepsHashes = {
    x86_64-linux = "sha256-LaV+xImSqlEx0YZKOEIse0/LHHDdNauLGgR9VtTpzzs="; # upstream's
    aarch64-darwin = "sha256-5T6O0jCSukvNZrVrRoZqI14S3S8160USOM7CbBUmAiE=";
  };

  clientBundle = pkgs.buildDenoBundle {
    pname = "blossom-server-client";
    inherit version src;
    configFile = "src/landing/client/deno.json";
    lockFile = "src/landing/client/deno.lock";
    entrypoint = "src/landing/client/index.tsx";
    bundleName = "client.js";
    bundleFlags = [ "--minify" "--platform=browser" ];
    hash = "sha256-/nxNuxegMsOwKR0RmlKCeoOayUgqozyA4juPPQoZESs=";
  };

  styles = pkgs.runCommand "blossom-server-styles-${version}.css" {
    nativeBuildInputs = [ pkgs.tailwindcss ];
  } ''
    cd ${src}
    tailwindcss -c tailwind.config.js -i src/landing/styles/input.css -o "$out" --minify
  '';
in
pkgs.buildDenoApplication {
  pname = "blossom-server";
  inherit version src;
  entrypoint = "main.ts";
  denoDepsHash = denoDepsHashes.${system} or lib.fakeHash;
  runtimeInputs = [ pkgs.ffmpeg ];
  runFlags = [ "-P" "--vendor" "--node-modules-dir=auto" ];
  postPatch = ''
    cp ${clientBundle}/client.js public/client.js
    cp ${styles} public/styles.css
  '';
  meta = {
    description = "Deno-based Blossom blob storage server (hzrd149)";
    homepage = "https://github.com/hzrd149/blossom-server";
    license = lib.licenses.mit;
    mainProgram = "blossom-server";
  };
}
