# The studio's sync infrastructure, as flake outputs (merged into flake.nix's own):
#
#   packages.<system>.{blossom-server, dev-infra, infra-smoke}
#   apps.<system>.{dev-infra, infra-smoke}       nix run .#dev-infra / nix run .#infra-smoke
#   nixosModules.{studio-relay, studio-blossom, studio-infra}    (studio-infra = both)
#   darwinModules.{studio-relay, studio-blossom, studio-infra}
#   checks.<system>.infra-modules                both pairs evaluated on a dummy NixOS + darwin host
#
# Importing from another flake (nix-config; see docs/sync.md "Deploying"):
#   inputs.studio.url = "github:gudnuf/studio";
#   modules = [ inputs.studio.darwinModules.studio-infra { services.studio-relay.enable = true; } ];
{ self, nixpkgs, nix-darwin, blossom-server, systems }:
let
  lib = nixpkgs.lib;
  forAll = f: lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});

  blossomVersion = (builtins.fromJSON (builtins.readFile "${blossom-server}/deno.json")).version;
  blossomServerFor = pkgs: import ./blossom-server.nix {
    pkgs = pkgs.extend blossom-server.inputs.deno2nix.overlays.default;
    src = blossom-server.outPath;
    version = blossomVersion;
  };

  relay = import ./relay.nix;
  blossom = import ./blossom.nix { inherit blossomServerFor; };

  # nak's FUSE subpackage doesn't build on darwin (macfuse stubs); the CLI is all we use
  nakFor = pkgs: pkgs.nak.overrideAttrs (_: { subPackages = [ "." ]; });

  devInfraFor = pkgs: pkgs.writeShellApplication {
    name = "dev-infra";
    text = ''
      export STUDIO_INFRA_RELAY_BIN=${lib.getExe' pkgs.nostr-rs-relay "nostr-rs-relay"}
      export STUDIO_INFRA_BLOSSOM_BIN=${lib.getExe (blossomServerFor pkgs)}
      export STUDIO_INFRA_PROCESS_COMPOSE=${lib.getExe pkgs.process-compose}
      export STUDIO_INFRA_PC_CONFIG=${./process-compose.yaml}
      exec ${lib.getExe pkgs.python3} -I ${./dev-infra.py} "$@"
    '';
  };

  smokeFor = pkgs: pkgs.writeShellApplication {
    name = "infra-smoke";
    runtimeInputs = [ (nakFor pkgs) pkgs.curl pkgs.jq pkgs.coreutils pkgs.diffutils ];
    text = builtins.readFile ./infra-smoke.sh;
  };

  # A host that turns on every option a deploy would touch, to keep both module pairs evaluating.
  exampleSettings = {
    services.studio-relay = {
      enable = true;
      maxEventBytes = 131072;
      pubkeyWhitelist = [ "35d26e4690cbe1a898af61cc3515661eb5fa763b57bd0b42e45099c8b32fd50f" ];
    };
    services.studio-blossom = {
      enable = true;
      allowedPubkeys = [ "35d26e4690cbe1a898af61cc3515661eb5fa763b57bd0b42e45099c8b32fd50f" ];
    };
  };
  dummyNixos = lib.nixosSystem {
    modules = [
      self.nixosModules.studio-infra
      exampleSettings
      {
        nixpkgs.hostPlatform = "x86_64-linux";
        boot.loader.grub.enable = false;
        fileSystems."/" = { device = "/dev/null"; fsType = "ext4"; };
        system.stateVersion = "26.05";
        security.acme = { acceptTerms = true; defaults.email = "ops@example.org"; };
        services.studio-relay = { domain = "relay.example.org"; acme = true; };
        services.studio-blossom = {
          domain = "blossom.example.org";
          acme = true;
          storage.s3 = { endpoint = "https://s3.example.org"; bucket = "studio"; };
          environmentFile = "/run/secrets/studio-blossom.env";
        };
      }
    ];
  };
  dummyDarwin = nix-darwin.lib.darwinSystem {
    modules = [
      self.darwinModules.studio-infra
      exampleSettings
      {
        nixpkgs.hostPlatform = "aarch64-darwin";
        system.stateVersion = 6;
        system.primaryUser = "claude";
        nix.enable = false;
        services.studio-relay = { user = "claude"; dataDir = "/Users/claude/.local/state/studio/relay"; };
        services.studio-blossom = { user = "claude"; dataDir = "/Users/claude/.local/state/studio/blossom"; };
      }
    ];
  };
  drv = x: builtins.unsafeDiscardStringContext x.drvPath;
in
{
  packages = forAll (pkgs: {
    blossom-server = blossomServerFor pkgs;
    dev-infra = devInfraFor pkgs;
    infra-smoke = smokeFor pkgs;
  });

  apps = forAll (pkgs: {
    dev-infra = { type = "app"; program = lib.getExe (devInfraFor pkgs); meta.description = "Local relay + Blossom server for development"; };
    infra-smoke = { type = "app"; program = lib.getExe (smokeFor pkgs); meta.description = "Check a running relay + Blossom server"; };
  });

  nixosModules = {
    studio-relay = relay.nixos;
    studio-blossom = blossom.nixos;
    studio-infra = { imports = [ relay.nixos blossom.nixos ]; };
  };
  darwinModules = {
    studio-relay = relay.darwin;
    studio-blossom = blossom.darwin;
    studio-infra = { imports = [ relay.darwin blossom.darwin ]; };
  };

  # Evaluation only: forcing the two systems' derivation paths proves the modules type-check and
  # produce a system, without building Linux closures on the Mac.
  checks = forAll (pkgs: {
    infra-modules = pkgs.runCommand "studio-infra-modules" { } ''
      echo nixos ${drv dummyNixos.config.system.build.toplevel} > $out
      echo darwin ${drv dummyDarwin.system} >> $out
    '';
  });
}
