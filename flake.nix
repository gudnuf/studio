{
  description = "studio — raw footage to post drafts, local-first";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  inputs.rust-overlay = { url = "github:oxalica/rust-overlay"; inputs.nixpkgs.follows = "nixpkgs"; };
  # sync infrastructure (nix/): the Blossom server, and nix-darwin to check the darwin modules
  inputs.blossom-server = { url = "github:hzrd149/blossom-server"; inputs.nixpkgs.follows = "nixpkgs"; };
  inputs.nix-darwin = { url = "github:nix-darwin/nix-darwin"; inputs.nixpkgs.follows = "nixpkgs"; };
  outputs = { self, nixpkgs, rust-overlay, ... }@inputs:
    let
      systems = [ "aarch64-darwin" "x86_64-linux" "aarch64-linux" ];
      darwin = [ "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
      forDarwin = f: nixpkgs.lib.genAttrs darwin (s: f nixpkgs.legacyPackages.${s});
      fs = nixpkgs.lib.fileset;

      # Silero VAD v6.2.0 in ggml form, for whisper-cli --vad and whisper-vad-speech-segments
      # (speech edges, the hallucination gate, `studio check`). 885 KB, pinned to a commit of
      # the repo whisper.cpp's models/download-vad-model.sh fetches from. studio.py finds it via
      # STUDIO_VAD_MODEL; without nix it downloads the same file and checks the same sha256.
      vadModelFor = pkgs: pkgs.fetchurl {
        name = "ggml-silero-v6.2.0.bin";
        url = "https://huggingface.co/ggml-org/whisper-vad/resolve/9ffd54a1e1ee413ddf265af9913beaf518d1639b/ggml-silero-v6.2.0.bin";
        sha256 = "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987";
      };

      # The backend the desktop app runs: studio.py + server.py + ui/ + brands/ copied into the
      # store, run by a wrapper whose PATH starts with exact store paths of python3, ffmpeg-full
      # and whisper-cpp. The agent CLI (claude, codex, goose…) is the user's own: the app appends
      # their login-shell PATH.
      backendFor = pkgs:
        let
          tools = [ pkgs.python3 pkgs.ffmpeg-full pkgs.whisper-cpp ];
          src = fs.toSource {
            root = ./.;
            fileset = fs.unions [ ./studio.py ./server.py ./ui ./brands ./README.md ./QUALITY.md ];
          };
        in pkgs.runCommand "studio-backend" { nativeBuildInputs = [ pkgs.makeWrapper ]; } ''
          mkdir -p $out/share/studio $out/bin
          cp -r ${src}/. $out/share/studio/
          makeWrapper ${pkgs.python3}/bin/python3 $out/bin/studio-backend \
            --add-flags "-B $out/share/studio/studio.py" \
            --set-default STUDIO_VAD_MODEL ${vadModelFor pkgs} \
            --prefix PATH : ${pkgs.lib.makeBinPath tools}
          # the CLI, same pinned tools (handy for `nix run .#studio -- ls`)
          ln -s studio-backend $out/bin/studio
        '';

      appFor = pkgs:
        let backend = backendFor pkgs;
        in pkgs.rustPlatform.buildRustPackage {
          pname = "studio-app";
          version = "0.1.0";
          src = fs.toSource {
            root = ./desktop;
            fileset = fs.unions [
              ./desktop/Cargo.toml ./desktop/Cargo.lock ./desktop/build.rs
              ./desktop/tauri.conf.json ./desktop/src ./desktop/icons
              ./desktop/capabilities ./desktop/dist
            ];
          };
          cargoLock.lockFile = ./desktop/Cargo.lock;
          nativeBuildInputs = [ pkgs.cargo-tauri.hook ];
          # baked into the binary (option_env!): the sidecar and the README for Help
          env.STUDIO_BACKEND = "${backend}/bin/studio-backend";
          env.STUDIO_SHARE = "${backend}/share/studio";
          doCheck = false;
          # Ad-hoc sign the whole bundle so Finder opens it without a Gatekeeper wall (the
          # linker already signs the arm64 binary; this seals Info.plist and resources too).
          # Nix's sandbox is off on this Mac, so the system codesign is reachable; when it isn't,
          # `nix run .#install` signs the installed copy instead. postFixup: after the strip,
          # which would break a signature made any earlier.
          postFixup = ''
            if [ -x /usr/bin/codesign ]; then
              /usr/bin/codesign --force --deep --sign - "$out/Applications/Studio.app"
            fi
          '';
          passthru.backend = backend;
          meta.mainProgram = "studio-app";
        };
    in nixpkgs.lib.recursiveUpdate {
      packages = forAll (pkgs: {
        studio-backend = backendFor pkgs;
      }) // forDarwin (pkgs: {
        studio-backend = backendFor pkgs;
        studio-app = appFor pkgs;
        default = appFor pkgs;
      });

      apps = forDarwin (pkgs:
        let
          app = self.packages.${pkgs.stdenv.hostPlatform.system}.studio-app;
          install = pkgs.writeShellApplication {
            name = "studio-install";
            runtimeInputs = [ pkgs.coreutils pkgs.rsync ];
            text = ''
              src="${app}/Applications/Studio.app"
              dest="$HOME/Applications/Studio.app"
              mkdir -p "$HOME/Applications"
              # A GC root, so `nix-collect-garbage` can't delete the backend the copy points at.
              root="''${XDG_STATE_HOME:-$HOME/.local/state}/studio"
              mkdir -p "$root"
              nix-store --realise "${app}" --add-root "$root/app" --indirect >/dev/null
              # a real copy (Spotlight, Launchpad and the Dock all treat it as a normal app)
              rsync -a --delete --chmod=u+w "$src/" "$dest/"
              /usr/bin/codesign --force --deep --sign - "$dest" 2>/dev/null || true
              /usr/bin/xattr -cr "$dest" 2>/dev/null || true
              # tell LaunchServices about it now (icon in Finder/Spotlight right away)
              /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$dest" 2>/dev/null || true
              echo "installed $dest"
              echo "  backend: ${app.backend}"
              echo "  gc root: $root/app"
            '';
          };
        in {
          install = { type = "app"; program = "${install}/bin/studio-install"; };
          studio-app = { type = "app"; program = "${app}/Applications/Studio.app/Contents/MacOS/studio-app"; };
          default = { type = "app"; program = "${app}/Applications/Studio.app/Contents/MacOS/studio-app"; };
        });

      devShells = forAll (pkgs: let rust = import ./nix/rust.nix { inherit pkgs rust-overlay; }; in {
        default = pkgs.mkShell (rust.env // {
          packages = [ pkgs.ffmpeg-full pkgs.whisper-cpp pkgs.python3 ] ++ rust.packages
            ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.cargo-tauri ];
          STUDIO_VAD_MODEL = "${vadModelFor pkgs}";
        });
        # just the Rust toolchain: CI and `test/run_all.sh --only rust`
        rust = pkgs.mkShell (rust.env // { packages = rust.packages; });
      });
    } (import ./nix { inherit self nixpkgs systems; inherit (inputs) nix-darwin blossom-server; });
}
