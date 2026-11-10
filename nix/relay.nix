# The studio's nostr relay as a module pair over nostr-rs-relay (nixpkgs, 0.10.0).
#
#   nixos  -> services.studio-relay, a hardened systemd service (+ nginx/ACME when `domain` is set)
#   darwin -> services.studio-relay, a launchd daemon running as `user`
#
# Both take the same options and write the same config.toml (`configFor` below). The studio is one
# library key talking to one relay: `pubkeyWhitelist` is that key (plus device keys later); empty
# means anyone may publish (fine on localhost, not on a public host).
#
# `nix run .#dev-infra` runs the same server on this Mac without either module; nix/dev-infra.py
# writes the same keys (keep the two in step).
#
# ── # CHIRA SEAM ──────────────────────────────────────────────────────────────────────────────
# A deploy sets only these, from the operator's own infrastructure plans (asked of chira
# 2026-11-09, unanswered at writing): the host that imports this module (hetzner 77.42.27.244 /
# the damsac VPS / the fiduciary laptop `nixos` / nous itself), `domain` (+ `acme`), and
# `pubkeyWhitelist` (the library pubkey from `studio pair`). Everything else keeps its default.
# See docs/sync.md "Deploying".
# ──────────────────────────────────────────────────────────────────────────────────────────────
let
  options = { lib, pkgs, darwin }: {
    enable = lib.mkEnableOption "the studio's nostr relay (nostr-rs-relay)";
    package = lib.mkPackageOption pkgs "nostr-rs-relay" { };
    address = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Bind address. Loopback by default; with `domain` set nginx is the public face and this
        stays loopback. "0.0.0.0" exposes plain ws:// to the local network (home Wi-Fi).
      '';
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 7347;
      description = "Listen port.";
    };
    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/studio-relay";
      description = "Directory for the relay's SQLite database (nostr.db).";
    };
    maxEventBytes = lib.mkOption {
      type = lib.types.ints.positive;
      default = 131072;
      description = ''
        Largest EVENT accepted, in bytes (the WebSocket message/frame limits follow it). One NIP-44
        v2 payload tops out at ~87.5 KB of base64, so the 128 KiB default holds any single
        encrypted log line or snapshot pointer (S4).
      '';
    };
    pubkeyWhitelist = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "35d26e4690cbe1a898af61cc3515661eb5fa763b57bd0b42e45099c8b32fd50f" ];
      description = "Hex pubkeys allowed to publish. Empty = anyone.";
    };
    domain = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "relay.example.org";
      description = ''
        Public name. Sets the advertised relay_url (wss://DOMAIN/) and, on NixOS, an nginx virtual
        host proxying to the relay. # CHIRA SEAM
      '';
    };
    acme = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        NixOS only: Let's Encrypt for `domain` (the host must set security.acme.acceptTerms and
        security.acme.defaults.email). # CHIRA SEAM
      '';
    };
    extraSettings = lib.mkOption {
      type = (pkgs.formats.toml { }).type;
      default = { };
      description = "Merged over the generated config.toml (nostr-rs-relay's own keys).";
    };
  } // lib.optionalAttrs darwin {
    user = lib.mkOption {
      type = lib.types.str;
      default = "root";
      example = "claude";
      description = "Darwin: the user the launchd daemon runs as and who owns `dataDir`.";
    };
  };

  # The config both modules (and, by hand, nix/dev-infra.py) write.
  configFor = { lib, cfg }: lib.recursiveUpdate {
    info = {
      relay_url = if cfg.domain != null then "wss://${cfg.domain}/" else "ws://${cfg.address}:${toString cfg.port}/";
      name = "studio";
      description = "The studio's library relay.";
    };
    database.data_directory = cfg.dataDir;
    network = { address = cfg.address; port = cfg.port; }
      // lib.optionalAttrs (cfg.domain != null) { remote_ip_header = "x-forwarded-for"; };
    options.reject_future_seconds = 1800;
    limits = {
      max_event_bytes = cfg.maxEventBytes;
      # the EVENT envelope (["EVENT", {...}]) is a few bytes bigger than the event itself
      max_ws_message_bytes = cfg.maxEventBytes + 4096;
      max_ws_frame_bytes = cfg.maxEventBytes + 4096;
      limit_scrapers = false;
    };
    authorization = lib.optionalAttrs (cfg.pubkeyWhitelist != [ ]) {
      pubkey_whitelist = cfg.pubkeyWhitelist;
    };
  } cfg.extraSettings;

  configFile = { lib, pkgs, cfg }: (pkgs.formats.toml { }).generate "studio-relay.toml" (configFor { inherit lib cfg; });

  assertions = cfg: [
    { assertion = cfg.acme -> cfg.domain != null; message = "services.studio-relay.acme needs services.studio-relay.domain"; }
  ];
in
{
  inherit configFor;

  nixos = { config, lib, pkgs, ... }:
    let cfg = config.services.studio-relay;
    in {
      options.services.studio-relay = options { inherit lib pkgs; darwin = false; };

      config = lib.mkIf cfg.enable {
        assertions = assertions cfg;

        users.users.studio-relay = { isSystemUser = true; group = "studio-relay"; home = cfg.dataDir; };
        users.groups.studio-relay = { };
        systemd.tmpfiles.rules = [ "d ${cfg.dataDir} 0750 studio-relay studio-relay - -" ];

        systemd.services.studio-relay = {
          description = "studio nostr relay (nostr-rs-relay)";
          wantedBy = [ "multi-user.target" ];
          after = [ "network.target" ];
          serviceConfig = {
            ExecStart = "${lib.getExe' cfg.package "nostr-rs-relay"} --config ${configFile { inherit lib pkgs cfg; }}";
            User = "studio-relay";
            Group = "studio-relay";
            Restart = "on-failure";
            ReadWritePaths = [ cfg.dataDir ];
            PrivateTmp = true;
            PrivateDevices = true;
            ProtectSystem = "strict";
            ProtectHome = true;
            NoNewPrivileges = true;
            ProtectKernelTunables = true;
            ProtectKernelModules = true;
            ProtectControlGroups = true;
            LockPersonality = true;
            RestrictSUIDSGID = true;
            CapabilityBoundingSet = "";
            SystemCallFilter = [ "@system-service" ];
          };
        };

        services.nginx = lib.mkIf (cfg.domain != null) {
          enable = true;
          recommendedProxySettings = true;
          virtualHosts.${cfg.domain} = {
            enableACME = cfg.acme;
            forceSSL = cfg.acme;
            locations."/" = {
              proxyPass = "http://${if cfg.address == "0.0.0.0" then "127.0.0.1" else cfg.address}:${toString cfg.port}";
              proxyWebsockets = true;
            };
          };
        };
        networking.firewall.allowedTCPPorts = lib.mkIf (cfg.domain != null) [ 80 443 ];
      };
    };

  darwin = { config, lib, pkgs, ... }:
    let cfg = config.services.studio-relay;
    in {
      options.services.studio-relay = options { inherit lib pkgs; darwin = true; };

      config = lib.mkIf cfg.enable {
        assertions = assertions cfg ++ [
          { assertion = !cfg.acme; message = "services.studio-relay.acme is NixOS-only; on darwin put a reverse proxy with its own TLS (Caddy, nginx) in front, see docs/sync.md"; }
        ];

        system.activationScripts.postActivation.text = ''
          mkdir -p ${lib.escapeShellArg cfg.dataDir}
          chown ${cfg.user} ${lib.escapeShellArg cfg.dataDir}
        '';

        launchd.daemons.studio-relay.serviceConfig = {
          Label = "org.studio.relay";
          ProgramArguments = [ (lib.getExe' cfg.package "nostr-rs-relay") "--config" "${configFile { inherit lib pkgs cfg; }}" ];
          UserName = cfg.user;
          WorkingDirectory = cfg.dataDir;
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "${cfg.dataDir}/relay.log";
          StandardErrorPath = "${cfg.dataDir}/relay.log";
        };
      };
    };
}
