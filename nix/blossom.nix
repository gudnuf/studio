# The studio's Blossom server as a module pair over hzrd149/blossom-server (nix/blossom-server.nix).
#
#   nixos  -> services.studio-blossom, a hardened systemd service (+ nginx/ACME when `domain` is set)
#   darwin -> services.studio-blossom, a launchd daemon running as `user`
#
# Both take the same options and write the same config.yml (`configFor` below). Uploads always need
# a BUD-11 auth event (kind 24242) signed by a nostr key; `allowedPubkeys` narrows that to the
# library key (empty = any key). Blobs never expire on their own; nothing here deletes them except
# an authorised DELETE.
#
# `nix run .#dev-infra` runs the same server on this Mac without either module; nix/dev-infra.py
# writes the same keys (keep the two in step).
#
# ── # CHIRA SEAM ──────────────────────────────────────────────────────────────────────────────
# A deploy sets only these, from the operator's own infrastructure plans (asked of chira
# 2026-11-09, unanswered at writing): the host that imports this module (hetzner 77.42.27.244 /
# the damsac VPS / the fiduciary laptop `nixos` / nous itself), `domain` (+ `acme`), `storage`
# (local disk, e.g. the fiduciary laptop's spare 3.6 TB drive as `dataDir`, or an S3 bucket +
# `environmentFile`), and `allowedPubkeys` (the library pubkey from `studio pair`).
# Everything else keeps its default. See docs/sync.md "Deploying".
# ──────────────────────────────────────────────────────────────────────────────────────────────
{ blossomServerFor }:
let
  options = { lib, pkgs, config, darwin }: {
    enable = lib.mkEnableOption "the studio's Blossom blob server (hzrd149/blossom-server)";
    package = lib.mkOption {
      type = lib.types.package;
      default = blossomServerFor pkgs;
      defaultText = lib.literalExpression "studio.packages.\${system}.blossom-server";
      description = ''
        The Blossom server; takes hzrd149/blossom-server's config.yml as its first argument. Defaults
        to the studio flake's pin, built with the studio flake's own nixpkgs (its deno dependency
        hashes are only valid there), whatever the host's nixpkgs has.
      '';
    };
    address = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Bind address. Loopback by default; with `domain` set nginx is the public face and this
        stays loopback. "0.0.0.0" exposes plain http:// to the local network (home Wi-Fi).
      '';
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 7348;
      description = "Listen port.";
    };
    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/studio-blossom";
      description = "SQLite metadata, local blobs (`blobs/`) and upload scratch live here.";
    };
    maxUpload = lib.mkOption {
      type = lib.types.ints.positive;
      default = 134217728;
      description = ''
        Largest single upload in bytes. The studio's client uploads in 64 MiB chunks (S4), so the
        128 MiB default leaves headroom and still refuses a client that forgot to chunk.
      '';
    };
    allowedPubkeys = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Hex pubkeys allowed to upload (a signed auth event is required either way). Empty = any key.";
    };
    storage = lib.mkOption {
      type = lib.types.attrTag {
        local = lib.mkOption {
          description = "Blobs under `dataDir`/blobs.";
          type = lib.types.submodule { };
        };
        s3 = lib.mkOption {
          description = ''
            Blobs in an S3-compatible bucket. Keys come from `environmentFile` as S3_ACCESS_KEY and
            S3_SECRET_KEY (never in the store). # CHIRA SEAM
          '';
          type = lib.types.submodule {
            options = {
              endpoint = lib.mkOption { type = lib.types.str; example = "https://ACCOUNT.r2.cloudflarestorage.com"; };
              bucket = lib.mkOption { type = lib.types.str; };
              region = lib.mkOption { type = lib.types.str; default = "auto"; };
              publicUrl = lib.mkOption {
                type = lib.types.nullOr lib.types.str;
                default = null;
                description = "If set, GET redirects here instead of proxying the bytes.";
              };
            };
          };
        };
      };
      default = { local = { }; };
      example = lib.literalExpression ''{ s3 = { endpoint = "https://…"; bucket = "studio-blobs"; }; }'';
      description = "Where blobs live: `{ local = { }; }` or `{ s3 = { … }; }`.";
    };
    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/secrets/studio-blossom.env";
      description = "KEY=value lines (S3_ACCESS_KEY, S3_SECRET_KEY) read at start; outside the store.";
    };
    domain = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "blossom.example.org";
      description = ''
        Public name. Sets the blob URLs (https://) and the BUD-11 `server` tag check and, on NixOS,
        an nginx virtual host proxying to the server; NixOS requires `acme` with it. On darwin a
        reverse proxy with TLS must front it. # CHIRA SEAM
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
      type = (pkgs.formats.yaml { }).type;
      default = { };
      description = "Merged over the generated config.yml (blossom-server's own keys).";
    };
  } // lib.optionalAttrs darwin {
    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = config.system.primaryUser or null;
      defaultText = lib.literalExpression "config.system.primaryUser";
      example = "claude";
      description = "Darwin: the user the launchd daemon runs as and who owns `dataDir`. Never root.";
    };
  };

  # The config both modules (and, by hand, nix/dev-infra.py) write.
  configFor = { lib, cfg }: lib.recursiveUpdate ({
    host = cfg.address;
    port = cfg.port;
    database.path = "${cfg.dataDir}/sqlite.db";
    storage = {
      removeWhenNoOwners = false;
      # Rules double as the upload allowlist: an upload no rule matches is refused (415, despite
      # upstream's "[] accepts any type" comment; S4). So: one catch-all rule, scoped to the
      # allowed pubkeys when there are any. "100 years" unaccessed = never pruned, in practice.
      rules = [ ({ type = "*"; expiration = "100 years"; }
        // lib.optionalAttrs (cfg.allowedPubkeys != [ ]) { pubkeys = cfg.allowedPubkeys; }) ];
    } // (if cfg.storage ? s3 then {
      backend = "s3";
      s3 = {
        inherit (cfg.storage.s3) endpoint bucket region;
        accessKey = "\${S3_ACCESS_KEY}";
        secretKey = "\${S3_SECRET_KEY}";
        tmpDir = "${cfg.dataDir}/s3-tmp";
      } // lib.optionalAttrs (cfg.storage.s3.publicUrl != null) { publicURL = cfg.storage.s3.publicUrl; };
    } else {
      backend = "local";
      local.dir = "${cfg.dataDir}/blobs";
    });
    upload = {
      enabled = true;
      requireAuth = true;
      maxSize = cfg.maxUpload;
      requirePubkeyInRule = cfg.allowedPubkeys != [ ];
    };
    # the studio needs PUT/GET/HEAD/DELETE only; mirror is an outbound-fetch surface
    mirror.enabled = false;
    delete.requireAuth = true;
    list.enabled = false;
    media = { enabled = false; tmpDir = "${cfg.dataDir}/media-tmp"; };
    report.enabled = false;
    landing.enabled = false;
  } // lib.optionalAttrs (cfg.domain != null) { publicDomain = cfg.domain; }) cfg.extraSettings;

  configFile = { lib, pkgs, cfg }: (pkgs.formats.yaml { }).generate "studio-blossom.yml" (configFor { inherit lib cfg; });

  assertions = cfg: [
    { assertion = cfg.acme -> cfg.domain != null; message = "services.studio-blossom.acme needs services.studio-blossom.domain"; }
    { assertion = cfg.storage ? s3 -> cfg.environmentFile != null; message = "services.studio-blossom.storage.s3 needs environmentFile (S3_ACCESS_KEY, S3_SECRET_KEY)"; }
  ];
in
{
  inherit configFor;

  nixos = { config, lib, pkgs, ... }:
    let cfg = config.services.studio-blossom;
    in {
      options.services.studio-blossom = options { inherit lib pkgs config; darwin = false; };

      config = lib.mkIf cfg.enable {
        assertions = assertions cfg ++ [
          { assertion = cfg.domain != null -> cfg.acme; message = "services.studio-blossom.domain needs acme = true on NixOS (blob URLs are https://)"; }
        ];

        users.users.studio-blossom = { isSystemUser = true; group = "studio-blossom"; home = cfg.dataDir; };
        users.groups.studio-blossom = { };
        systemd.tmpfiles.rules = [ "d ${cfg.dataDir} 0750 studio-blossom studio-blossom - -" ];

        systemd.services.studio-blossom = {
          description = "studio Blossom server (hzrd149/blossom-server)";
          wantedBy = [ "multi-user.target" ];
          wants = [ "network-online.target" ];
          after = [ "network-online.target" ];
          environment = {
            DENO_DIR = "/var/cache/studio-blossom/deno";
            BLOSSOM_REQUIRE_CONFIG = "1";
          };
          serviceConfig = {
            ExecStart = "${lib.getExe cfg.package} ${configFile { inherit lib pkgs cfg; }}";
            User = "studio-blossom";
            Group = "studio-blossom";
            WorkingDirectory = cfg.dataDir;
            CacheDirectory = "studio-blossom";
            Restart = "on-failure";
            RestartSec = 5;
            UMask = "0077";
            ReadWritePaths = [ cfg.dataDir ];
            NoNewPrivileges = true;
            PrivateDevices = true;
            PrivateTmp = true;
            ProtectControlGroups = true;
            ProtectHome = true;
            ProtectKernelModules = true;
            ProtectKernelTunables = true;
            ProtectSystem = "strict";
            RestrictSUIDSGID = true;
            # as the relay's, minus MemoryDenyWriteExecute (deno JITs)
            CapabilityBoundingSet = "";
            LockPersonality = true;
            SystemCallFilter = [ "@system-service" ];
            SystemCallArchitectures = "native";
            RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
            RestrictNamespaces = true;
            RestrictRealtime = true;
            ProtectClock = true;
            ProtectHostname = true;
            ProtectProc = "invisible";
          } // lib.optionalAttrs (cfg.environmentFile != null) { EnvironmentFile = cfg.environmentFile; };
        };

        services.nginx = lib.mkIf (cfg.domain != null) {
          enable = true;
          recommendedProxySettings = true;
          virtualHosts.${cfg.domain} = {
            enableACME = cfg.acme;
            forceSSL = cfg.acme;
            locations."/" = {
              proxyPass = "http://${if cfg.address == "0.0.0.0" then "127.0.0.1" else cfg.address}:${toString cfg.port}";
              extraConfig = ''
                client_max_body_size ${toString cfg.maxUpload};
                proxy_request_buffering off;
                proxy_buffering off;
                # a 128 MiB body to S3 on a slow uplink: wait for the descriptor
                proxy_read_timeout 600s;
                proxy_send_timeout 600s;
              '';
            };
          };
        };
        networking.firewall.allowedTCPPorts = lib.mkIf (cfg.domain != null) [ 80 443 ];
      };
    };

  darwin = { config, lib, pkgs, ... }:
    let
      cfg = config.services.studio-blossom;
      start = pkgs.writeShellScript "studio-blossom-start" ''
        set -eu
        ${lib.optionalString (cfg.environmentFile != null) ''
          set -a; . ${lib.escapeShellArg cfg.environmentFile}; set +a
        ''}
        exec ${lib.getExe cfg.package} ${configFile { inherit lib pkgs cfg; }}
      '';
    in {
      options.services.studio-blossom = options { inherit lib pkgs config; darwin = true; };

      config = lib.mkIf cfg.enable {
        assertions = assertions cfg ++ [
          { assertion = cfg.user != null && cfg.user != "root"; message = "services.studio-blossom.user: set a non-root user (no system.primaryUser to default to)"; }
          { assertion = !cfg.acme; message = "services.studio-blossom.acme is NixOS-only; on darwin put a reverse proxy with its own TLS (Caddy, nginx) in front, see docs/sync.md"; }
        ];

        system.activationScripts.postActivation.text = ''
          mkdir -p ${lib.escapeShellArg cfg.dataDir}
          chown ${cfg.user} ${lib.escapeShellArg cfg.dataDir}
        '';

        launchd.daemons.studio-blossom.serviceConfig = {
          Label = "org.studio.blossom";
          ProgramArguments = [ "${start}" ];
          UserName = cfg.user;
          WorkingDirectory = cfg.dataDir;
          EnvironmentVariables = {
            DENO_DIR = "${cfg.dataDir}/deno";
            BLOSSOM_REQUIRE_CONFIG = "1";
          };
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "${cfg.dataDir}/blossom.log";
          StandardErrorPath = "${cfg.dataDir}/blossom.log";
        };
      };
    };
}
