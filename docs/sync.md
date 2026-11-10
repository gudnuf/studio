# Sync infrastructure: one relay, one Blossom server

The studio syncs a library between devices (the Mac, the phone, workers) over **nostr** (one
relay carries the encrypted event log and the snapshot pointer) and stores media on **Blossom**
(content-addressed blobs, uploaded with a nostr-signed auth event). The event shapes are in
`docs/events.md`; this page is the servers.

Nothing is hardcoded to a host. Clients read the URLs from `$STUDIO_HOME/studio.toml`:

```toml
[sync]
relays  = ["ws://127.0.0.1:7347"]
blossom = ["http://127.0.0.1:7348"]
```

and tests override them with `STUDIO_RELAY` / `STUDIO_BLOSSOM`.

## The servers

| | Server | Package | Default port |
|---|---|---|---|
| relay | nostr-rs-relay 0.10.0 | `pkgs.nostr-rs-relay` (nixpkgs) | 7347 |
| Blossom | hzrd149/blossom-server (Deno) | `packages.blossom-server` (`nix/blossom-server.nix`, the `blossom-server` flake input) | 7348 |

The Blossom server is hzrd149's, not the crates.io `blossom-server` the plan named first: spike S4
found that one lacks `HEAD /upload` (BUD-06) and range GETs, buffers whole blobs in memory, and
accepts only its own auth-tag dialect (see `~/athanor/forge/studio-app/spikes/S4.md`).

Limits (S4): one event up to 128 KiB (`maxEventBytes`); a single NIP-44 v2 payload is at most
87,472 bytes of base64, so every encrypted log line or pointer fits. Uploads up to 128 MiB
(`maxUpload`); clients chunk at 64 MiB. A relay `REQ` returns everything that matches when no
`limit` is given (10,000 events in ~3 s locally), and paging with `until` is exact.

## Development: `nix run .#dev-infra`

Runs both servers on this machine under process-compose (`nix/process-compose.yaml`), configured
by `nix/dev-infra.py`:

```sh
nix run .#dev-infra                  # TUI; q stops both
nix run .#dev-infra -- --lan         # bind 0.0.0.0 so a phone on the same Wi-Fi reaches them
nix run .#dev-infra -- --print       # what would run; writes nothing
nix run .#dev-infra -- -t=false      # no TUI (logs to stdout); other flags go to process-compose
nix run .#infra-smoke                # check a running pair: event round trip, signed upload, ranges
```

- **Ports** come from the URLs clients use: `STUDIO_RELAY` / `STUDIO_BLOSSOM`, else
  `studio.toml [sync]`, else 7347 / 7348. A non-local URL there (a deployed relay) is skipped with
  a note: dev-infra only runs local servers.
- **Bind** is `127.0.0.1` unless `--lan`, `STUDIO_INFRA_LAN=1` or `[infra] lan = true`. LAN mode is
  plain `ws://` / `http://` (no TLS); the phone uses `ws://<mac>.local:7347`. The macOS firewall may
  ask once.
- **State** goes to `$STUDIO_INFRA_DIR`, else `[infra] state_dir`, else
  `~/.local/state/studio/infra/` (`relay/nostr.db`, `blossom/blobs/`, logs). Never inside
  `~/Studio`: dev-infra refuses. A test home's own dir is fine.
- **Who may write:** `[infra] allowed_pubkeys = ["<hex>"]` restricts both servers to those keys
  (the library key from `studio pair`, once 0.6 lands). Empty = any key may publish/upload; uploads
  always need a signed kind-24242 event either way.
- `[infra] max_event_bytes` / `max_upload` override the limits.

`infra-smoke` signs with a throwaway key; with an allowlist set, pass one of the allowed keys as
`STUDIO_SMOKE_SEC`.

## Deploying

`nix/relay.nix` and `nix/blossom.nix` are module pairs with the same options on both platforms:

| Flake output | Platform | Runs as |
|---|---|---|
| `nixosModules.studio-relay`, `.studio-blossom`, `.studio-infra` (both) | NixOS | hardened systemd services, nginx + ACME when `domain` is set |
| `darwinModules.studio-relay`, `.studio-blossom`, `.studio-infra` (both) | nix-darwin | launchd daemons as `user` |

Relay options: `enable, package, address, port, dataDir, maxEventBytes, pubkeyWhitelist, domain,
acme, extraSettings` (+ `user` on darwin). Blossom options: `enable, package, address, port, dataDir,
maxUpload, allowedPubkeys, storage = { local = { }; } | { s3 = { endpoint, bucket, region,
publicUrl }; }, environmentFile, domain, acme, extraSettings` (+ `user` on darwin).

`nix flake check` evaluates both pairs on a dummy NixOS host (domain + ACME + S3) and a dummy darwin
host. They were also evaluated into the real `nous` darwin configuration from nix-config (via
`extendModules`, without touching nix-config).

### # CHIRA SEAM

The operator's own infrastructure plans (asked of chira 2026-11-09; see
`~/athanor/CLAUDE-HANDOFF.md`) decide only these values; nothing else changes:

1. **The host**: hetzner (77.42.27.244), the damsac VPS, the always-on fiduciary laptop
   (tailscale `nixos`), or nous itself.
2. **The domain** (`domain`, and `acme = true` on NixOS) for each server.
3. **Where blobs live**: local disk (`dataDir`, e.g. the fiduciary laptop's spare drive) or an
   S3-compatible bucket (`storage.s3` + `environmentFile` holding `S3_ACCESS_KEY` /
   `S3_SECRET_KEY`).
4. **Who may write**: the library pubkey in `pubkeyWhitelist` / `allowedPubkeys`.

Then the studio side is one edit to `studio.toml`:

```toml
[sync]
relays  = ["wss://relay.example.org"]
blossom = ["https://blossom.example.org"]
```

### Importing from nix-config

```nix
# flake.nix inputs
studio = { url = "github:gudnuf/studio"; inputs.nixpkgs.follows = "nixpkgs"; };

# a NixOS host (hetzner, the fiduciary laptop): add to its modules
inputs.studio.nixosModules.studio-infra
{
  security.acme = { acceptTerms = true; defaults.email = "…"; };
  services.studio-relay = {
    enable = true;
    domain = "relay.example.org";            # CHIRA SEAM
    acme = true;
    pubkeyWhitelist = [ "<library pubkey hex>" ];
  };
  services.studio-blossom = {
    enable = true;
    domain = "blossom.example.org";          # CHIRA SEAM
    acme = true;
    allowedPubkeys = [ "<library pubkey hex>" ];
    # storage.s3 = { endpoint = "…"; bucket = "…"; };  environmentFile = "/run/secrets/…";
  };
}

# nous (nix-darwin), always-on at home instead of `nix run .#dev-infra`
inputs.studio.darwinModules.studio-infra
{
  services.studio-relay = { enable = true; user = "claude"; dataDir = "/Users/claude/.local/state/studio/relay"; };
  services.studio-blossom = { enable = true; user = "claude"; dataDir = "/Users/claude/.local/state/studio/blossom"; };
}
```

On darwin there is no ACME: put TLS in front with `tailscale serve` or a proxy if the phone needs
`wss://` away from home. The `blossom-server` package has pinned dependency hashes for
`x86_64-linux` and `aarch64-darwin`; on `aarch64-linux` the first build prints the hash to add to
`nix/blossom-server.nix`.
