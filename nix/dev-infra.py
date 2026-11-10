"""`nix run .#dev-infra`: the studio's relay + Blossom server on this machine, for development.

Resolves where the two servers listen and keep their data, writes their configs, then hands over
to process-compose (nix/process-compose.yaml). Nothing is written under the library.

  nix run .#dev-infra                      # TUI; q to stop both
  nix run .#dev-infra -- --lan             # bind 0.0.0.0 so the phone on the same Wi-Fi reaches them
  nix run .#dev-infra -- --print           # show what would run, write nothing
  nix run .#dev-infra -- -t=false          # anything else goes to process-compose (here: no TUI)

Where the values come from, first match wins:
  relay URL    STUDIO_RELAY, studio.toml [sync] relays[0], ws://127.0.0.1:7347
  Blossom URL  STUDIO_BLOSSOM, studio.toml [sync] blossom[0], http://127.0.0.1:7348
  LAN          --lan, STUDIO_INFRA_LAN=1, studio.toml [infra] lan = true
  state dir    STUDIO_INFRA_DIR, studio.toml [infra] state_dir, $XDG_STATE_HOME/studio/infra
               (anywhere but ~/Studio: a test home's own dir is fine)
  limits/keys  studio.toml [infra] max_event_bytes, max_upload, allowed_pubkeys
studio.toml is $STUDIO_HOME/studio.toml (STUDIO_HOME defaults to ~/Studio, read only). A URL that
is not on this machine (a deployed relay) is ignored with a note: dev-infra only runs local servers.

The configs written mirror nix/relay.nix and nix/blossom.nix `configFor` (keep them in step).
"""
import json
import os
import socket
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_RELAY = "ws://127.0.0.1:7347"
DEFAULT_BLOSSOM = "http://127.0.0.1:7348"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def die(msg):
    print(f"dev-infra: {msg}", file=sys.stderr)
    sys.exit(1)


def is_local(host):
    host = (host or "").lower()
    me = socket.gethostname().lower()
    return (host in LOCAL_HOSTS or host == me or host == me.split(".")[0]
            or host == me.split(".")[0] + ".local")


def pick_url(env, listed, default, what):
    """The URL the servers should answer at, and where it came from."""
    if isinstance(listed, str):
        listed = [listed]
    for value, source in ((os.environ.get(env), env), ((listed or [None])[0], f"studio.toml [sync] {what}")):
        if not value:
            continue
        u = urlparse(value)
        if u.port and is_local(u.hostname):
            return value, source
        print(f"dev-infra: {source} = {value} is not a local URL with a port; using {default}", file=sys.stderr)
    return default, "default"


def inside(child, parent):
    c, p = str(child.resolve()).lower(), str(parent.resolve()).lower()   # APFS is case-insensitive
    return c == p or c.startswith(p.rstrip("/") + "/")


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def write_toml(path, doc):
    lines = []
    for section, values in doc.items():
        if not values:
            continue
        lines.append(f"[{section}]")
        lines += [f"{k} = {toml_value(v)}" for k, v in values.items()]
        lines.append("")
    path.write_text("\n".join(lines))


def main(argv):
    lan = "--lan" in argv
    show_only = "--print" in argv
    passthrough = [a for a in argv if a not in ("--lan", "--print")]

    home = Path(os.environ.get("STUDIO_HOME") or Path.home() / "Studio").expanduser()
    library = Path.home() / "Studio"
    toml_path = home / "studio.toml"
    conf = {}
    if toml_path.is_file():
        with toml_path.open("rb") as f:
            conf = tomllib.load(f)
    sync, infra = conf.get("sync", {}), conf.get("infra", {})

    relay_url, relay_src = pick_url("STUDIO_RELAY", sync.get("relays"), DEFAULT_RELAY, "relays")
    blossom_url, blossom_src = pick_url("STUDIO_BLOSSOM", sync.get("blossom"), DEFAULT_BLOSSOM, "blossom")
    relay_port, blossom_port = urlparse(relay_url).port, urlparse(blossom_url).port
    if relay_port == blossom_port:
        die(f"relay and Blossom both on port {relay_port}")

    lan = lan or os.environ.get("STUDIO_INFRA_LAN") == "1" or infra.get("lan") is True
    address = "0.0.0.0" if lan else "127.0.0.1"

    state = Path(os.environ.get("STUDIO_INFRA_DIR") or infra.get("state_dir")
                 or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "studio/infra").expanduser()
    if inside(state, library):
        die(f"state dir {state} is inside {library}; infra data never goes there")

    max_event = int(infra.get("max_event_bytes", 131072))
    max_upload = int(infra.get("max_upload", 134217728))
    pubkeys = [str(p) for p in infra.get("allowed_pubkeys", [])]

    lan_host = socket.gethostname().split(".")[0] + ".local"
    print(f"relay    ws://{address}:{relay_port}   ({relay_src})")
    print(f"blossom  http://{address}:{blossom_port}   ({blossom_src})")
    if lan:
        print(f"LAN      ws://{lan_host}:{relay_port}  http://{lan_host}:{blossom_port}  (plain, no TLS)")
    print(f"state    {state}")
    print(f"limits   event {max_event} B, upload {max_upload} B, "
          f"{'pubkeys ' + ','.join(p[:8] for p in pubkeys) if pubkeys else 'any signing key'}")
    print(f"studio.toml:\n  [sync]\n  relays = [\"ws://127.0.0.1:{relay_port}\"]\n"
          f"  blossom = [\"http://127.0.0.1:{blossom_port}\"]")
    if show_only:
        return 0

    relay_dir, blossom_dir = state / "relay", state / "blossom"
    for d in (relay_dir, blossom_dir):
        d.mkdir(parents=True, exist_ok=True)

    # mirrors nix/relay.nix configFor
    write_toml(relay_dir / "config.toml", {
        "info": {"relay_url": f"ws://{address}:{relay_port}/", "name": "studio",
                 "description": "The studio's library relay (dev)."},
        "database": {"data_directory": str(relay_dir)},
        "network": {"address": address, "port": relay_port},
        "options": {"reject_future_seconds": 1800},
        "limits": {"max_event_bytes": max_event, "max_ws_message_bytes": max_event + 4096,
                   "max_ws_frame_bytes": max_event + 4096, "limit_scrapers": False},
        "authorization": {"pubkey_whitelist": pubkeys} if pubkeys else {},
    })

    # mirrors nix/blossom.nix configFor; JSON is YAML
    (blossom_dir / "config.yml").write_text(json.dumps({
        "host": address,
        "port": blossom_port,
        "database": {"path": str(blossom_dir / "sqlite.db")},
        "storage": {
            "backend": "local", "local": {"dir": str(blossom_dir / "blobs")},
            "removeWhenNoOwners": False,
            "rules": [{"type": "*", "expiration": "100 years", **({"pubkeys": pubkeys} if pubkeys else {})}],
        },
        "upload": {"enabled": True, "requireAuth": True, "maxSize": max_upload,
                   "requirePubkeyInRule": bool(pubkeys)},
        "mirror": {"enabled": False},
        "delete": {"requireAuth": True},
        "list": {"enabled": False},
        "media": {"enabled": False, "tmpDir": str(blossom_dir / "media-tmp")},
        "report": {"enabled": False},
        "landing": {"enabled": False},
    }, indent=2) + "\n")

    env = dict(os.environ,
               STUDIO_INFRA_STATE=str(state),
               STUDIO_INFRA_RELAY_PORT=str(relay_port),
               STUDIO_INFRA_BLOSSOM_PORT=str(blossom_port))
    pc = os.environ["STUDIO_INFRA_PROCESS_COMPOSE"]
    sock = state / "process-compose.sock"
    if len(str(sock)) > 100:   # sun_path is 104 bytes on macOS, 108 on Linux
        sock = Path(f"/tmp/studio-infra-{os.getuid()}.sock")
    # control socket, not a TCP port (process-compose's default :8080 is often taken)
    args = [pc, "up", "-f", os.environ["STUDIO_INFRA_PC_CONFIG"],
            "--use-uds", "--unix-socket", str(sock), "-L", str(state / "process-compose.run.log")]
    if not sys.stdout.isatty():
        args.append("--tui=false")
    sys.stdout.flush()
    os.execve(pc, args + passthrough, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
