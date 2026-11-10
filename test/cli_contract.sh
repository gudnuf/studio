#!/usr/bin/env bash
# The CLI contract (plan task 0.3): every command line AGENTS.md shows an agent, and at least one
# real invocation of every verb studio.py has, run against a named binary on the seeded library;
# normalized stdout, stderr, exit code + the events each appended, compared with test/golden/cli/.
# `--help` text is not contract (argparse and clap format help differently).
#
#   test/cli_contract.sh             compare $STUDIO_BIN (default ./studio) with the goldens
#   test/cli_contract.sh --update    rewrite the goldens from $STUDIO_BIN
#   test/cli_contract.sh --list      print the command lines (source line -> what runs), run nothing
#
# Command lines come from AGENTS.md: every line of its ```bash blocks and every inline `studio …`
# span. "a | b | c" alternatives become separate lines (b and c take a's verb), [optional] parts
# are included, a|b picks a, and the placeholders (P, ID, MEDIA, CLIP, IN, OUT, REV, NOTE_ID, …)
# are bound to the seeded home (test/seed.py --v2 --with-footage). Each line runs on a fresh
# copy of that home, as the agent (STUDIO_ACTOR=agent, STUDIO_DEVICE=contract); AI steps call the
# fake agent (test/fakebin), never a real one.
#
# Verbs AGENTS.md doesn't show get lines of their own (VERB_LINES below): ingest, the steps, cut,
# review, bundle, import, run, ui (started on a free port, stopped after its first line), plus the
# read-only ls / project / state / agent. `open` is a no-op stub, so nothing opens a window.
#
# Normalized: the home path, the fixture dir, the scratch dir, the ui port, timestamps, render stamps, UUIDs and hex ids/hashes the seeded home
# does not have (numbered in order of appearance); job.progress events are left out (timing).
# render and check measure real media, so their
# lines compare in shape only: digits masked, events reduced to type + data keys.
# A Rust `studio` passes when STUDIO_BIN=path/to/it test/cli_contract.sh exits 0.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
if ! command -v ffmpeg >/dev/null || ! command -v whisper-cli >/dev/null \
   || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
  exec nix develop "$root" --quiet -c bash "$here/cli_contract.sh" "$@" 2> >(grep -v "Git tree .* is dirty" >&2)
fi
export STUDIO_BIN=${STUDIO_BIN:-$root/studio}
[[ $STUDIO_BIN == */* && $STUDIO_BIN != /* ]] && STUDIO_BIN="$PWD/$STUDIO_BIN"   # runs happen elsewhere
exec python3 - "$root" "$@" <<'PY'
import hashlib, json, os, re, shlex, shutil, subprocess, sys, tempfile, time
from pathlib import Path

root, args = Path(sys.argv[1]), sys.argv[2:]
update, listing = "--update" in args, "--list" in args
golden = root / "test" / "golden" / "cli"
binary = os.environ["STUDIO_BIN"]
# measure real media (or run whisper): compared in shape only
HEAVY = {"render", "check", "ingest", "run", "proxy", "speech", "transcribe", "keyframes", "cut"}
PARALLEL = {"run", "ui"}               # steps run in lanes at once: line and event order is not contract
UI_EVENTS = {"ui.started", "job.queued"}   # ui: what start-up writes, not the resumed work it races
SETUP = {"check": [["render", "sparks-opener"]],          # run first, their events not counted
         "import": [["bundle", "incoming", "-p", "paperwork-reel"]]}

tmp = Path(tempfile.mkdtemp(prefix="studio-contract-")).resolve()
seed = tmp / "seed"
fixtures = Path(subprocess.run(["bash", str(root / "test" / "fixtures.sh")], capture_output=True, text=True,
                               check=True).stdout.strip().splitlines()[-1]).resolve()
(tmp / "bin").mkdir()
(tmp / "bin" / "open").write_text("#!/bin/sh\nexit 0\n")        # review/run open a window on macOS
(tmp / "bin" / "open").chmod(0o755)
subprocess.run([sys.executable, str(root / "test" / "seed.py"), "--v2", "--with-footage", str(seed)],
               check=True, stdout=subprocess.DEVNULL)
log0 = (seed / "events.jsonl").read_text()
evs0 = [json.loads(l) for l in log0.splitlines() if l.strip()]
known = set(re.findall(r"\b[0-9a-f]{8,64}\b", log0))

# ---- what the placeholders mean in the seeded home -----------------------------------------
st = {"media": {}, "notes": [], "exports": [], "round": None}
for e in evs0:
    d = e["data"]
    if e["type"] == "ingested":
        st["media"][d["name"]] = e["media"]
    elif e["type"] == "note":
        st["notes"].append(d)
    elif e["type"] == "render.done" and d["project"] == "sparks-opener":
        st["exports"].append(d["sha256"])
    elif e["type"] == "blind.round":
        st["round"] = d["round"]
acked = {i for e in evs0 if e["type"] == "note.ack" for i in e["data"]["ids"]}
talk = st["media"]["talking.mov"]
open_note = [n for n in st["notes"] if n["actor"] == "operator" and n["id"] not in acked][-1]["id"]
s1, s2 = (x[:12] for x in st["exports"][:2])
BIND = {"P": "sparks-opener", "ID": talk, "MEDIA": talk, "CLIP": "a1000002", "IN": "12.5",
        "OUT": "18.0", "AT": "2.0", "TO": "1", "T": "0.5", "N": "1", "REV": "2", "NOTE_ID": open_note,
        "RULE": "pauses", "WRONG": "typing", "RIGHT": "writing", "TEXT": "the turn",
        "R": st["round"], "ROUND": st["round"], "CHOSEN": s2,
        "A,B,C": f"{s1},{s2}", "…": "the held beat lands the line"}

# ---- the command lines ---------------------------------------------------------------------
def agents_lines():
    text = (root / "AGENTS.md").read_text()
    out, fence = [], False
    for raw in text.splitlines():
        if raw.startswith("```"):
            fence = not fence
            continue
        if fence:
            line = re.sub(r"\s+#\s.*$", "", raw).strip()
            if line.startswith("studio "):
                out.append(line)
        else:
            out += [m for m in re.findall(r"`(studio [^`]+)`", raw)]
    return out

def expand(line):
    """AGENTS.md notation -> concrete argv lists (without the leading `studio`)."""
    parts = [p.strip() for p in line.split(" | ")]
    verb = parts[0].split()[:2]
    rows = []
    for p in parts:
        if not p.startswith("studio "):
            p = " ".join(verb + [p])
        p = re.sub(r"\[([^\]]*)\]", r"\1", p)                    # [optional] -> included
        p = re.sub(r"(?<![\w,])([^\s|\"',]+)(\|[^\s|\"']+)+", r"\1", p)   # a|b -> a
        toks = shlex.split(p)[1:]
        if not toks or toks == ["…"]:
            continue
        argv = []
        for t in toks:
            if t in BIND:
                argv.append(BIND[t])
            elif re.fullmatch(r"[A-Z_]+@[A-Z]+", t):
                a, b = t.split("@")
                argv.append(f"{BIND[a]}@{BIND[b]}")
            else:
                argv.append(t)
        rows.append(argv)
    return rows

# the verbs: the reference implementation's (studio.py main()), whatever binary is under test
usage = subprocess.run([sys.executable, str(root / "studio.py"), "--help"], capture_output=True, text=True,
                       env={**os.environ, "STUDIO_HOME": str(seed), "COLUMNS": "200", "NO_COLOR": "1"}).stdout
verbs = re.search(r"\{([a-z0-9_,-]+)\}", usage).group(1).split(",")
broll = st["media"]["broll.mov"]
VERB_LINES = [
    ["ls"], ["project", "ls"], ["project", "new", "Third reel"], ["state", "--json"], ["agent"],
    ["ingest", "-p", "sparks-opener", "$FIXTURES/screen-recording.mp4"],
    ["proxy", talk, "--force"], ["speech", talk, "--force"], ["transcribe", talk, "--force"],
    ["keyframes", broll], ["cut", talk], ["review"],
    ["bundle", "shared", "-p", "paperwork-reel"], ["import", "incoming"],
    ["run", "--no-open", "-p", "paperwork-reel", "$FIXTURES/screen-recording.mp4"],
    ["ui", "--port", "PORT", "--no-open"],
]

cases, seen = [], set()
for src in agents_lines():
    for argv in expand(src):
        key = shlex.join(argv)
        if key not in seen:
            seen.add(key); cases.append((src, argv))
for argv in VERB_LINES:
    cases.append((f"(verb line) studio {shlex.join(argv)}", argv))

missing = [v for v in verbs if not any(a and a[0] == v for _, a in cases)]
if missing:
    sys.exit(f"cli_contract: verbs with no line: {missing}")

if listing:
    for src, argv in cases:
        print(f"{src}\n    -> studio {shlex.join(argv)}")
    sys.exit(0)

# ---- run and normalize ---------------------------------------------------------------------
def name_of(argv):
    words = [a for a in argv if re.fullmatch(r"[a-z][a-z-]*", a)][:3]
    return "-".join(words or ["studio"]) + "-" + hashlib.sha1(shlex.join(argv).encode()).hexdigest()[:8]

port = None

def free_port():
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]

def run_ui(argv, env):
    """The server never exits: start it, keep its output up to the line naming its URL, stop it."""
    import select, signal, time
    p = subprocess.Popen([binary, *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=env, cwd=tmp, start_new_session=True)
    out, t0 = "", time.time()
    while time.time() - t0 < 120:
        if select.select([p.stdout], [], [], 1)[0]:
            line = p.stdout.readline()
            if not line:
                break
            out += line
            if "studio ui" in line:
                break
    time.sleep(1)                       # let it log ui.started
    os.killpg(p.pid, signal.SIGTERM)
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL); p.wait()
    err = p.stderr.read()
    return subprocess.CompletedProcess(argv, 0 if "studio ui" in out else 1, out, err)

def normalize(text, home, fresh):
    text = (text.replace(str(home), "$STUDIO_HOME").replace(str(fixtures), "$FIXTURES")
            .replace(str(tmp), "$TMP").replace(str(root), "$STUDIO_SRC"))
    if port:
        text = re.sub(rf"\b{port}\b", "PORT", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[+-]\d\d:\d\d|Z)?", "<ts>", text)
    text = re.sub(r"\d{8}-\d{6}(?:_\d+)?", "<stamp>", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", text)
    def hexid(m):
        h = m.group(0)
        if h in known or any(k.startswith(h) for k in known) or not re.search(r"\d", h):
            return h
        fresh.setdefault(h, f"<new{len(fresh) + 1}>")
        return fresh[h]
    return re.sub(r"\b[0-9a-f]{8,64}\b", hexid, text)

def run(argv):
    home = tmp / "home"
    if home.exists():
        shutil.rmtree(home)
    shutil.copytree(seed, home, symlinks=True)
    env = {k: v for k, v in os.environ.items() if k not in (
        "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX", "GOOSE_TERMINAL", "STUDIO_AGENT_CMD",
        "STUDIO_AGENT_MODEL", "STUDIO_MOMENTS_MODEL", "STUDIO_CLAUDE_MODEL")}
    env.update(STUDIO_HOME=str(home), STUDIO_ACTOR="agent", STUDIO_DEVICE="contract",
               STUDIO_AGENT="claude", FAKE_AGENT_DELAY="0", COLUMNS="100", NO_COLOR="1",
               PYTHON_COLORS="0", PATH=f"{tmp / 'bin'}:{root / 'test' / 'fakebin'}:{env['PATH']}")
    for d in ("shared", "incoming"):
        shutil.rmtree(tmp / d, ignore_errors=True)
    for pre in SETUP.get(argv[0] if argv else None, []):
        subprocess.run([binary, *pre], capture_output=True, env=env, timeout=600, cwd=tmp, check=True)
    base = len((home / "events.jsonl").read_text())
    global port
    port = None
    if argv[:1] == ["ui"]:
        port = free_port()
        r = run_ui([str(port) if a == "PORT" else a for a in argv], env)
    else:
        real = [a.replace("$FIXTURES", str(fixtures)) for a in argv]
        r = subprocess.run([binary, *real], capture_output=True, text=True, env=env, timeout=900, cwd=tmp)
    log = (home / "events.jsonl").read_text()
    new = [e for e in (json.loads(l) for l in log[base:].splitlines() if l.strip())
           if e["type"] != "job.progress"]     # timing-dependent and local-only: not contract
    heavy = bool(argv) and argv[0] in HEAVY
    fresh = {}
    out = normalize(r.stdout, home, fresh)
    lines = []
    if argv[:1] == ["ui"]:
        new = [e for e in new if e["type"] in UI_EVENTS]
    for e in new:
        if heavy:
            e = {"type": e["type"], "media": e["media"], "data": sorted(e["data"])}
        else:
            if e["type"] == "blind.round":        # the shuffle is random on purpose
                e["data"]["labels"] = {k: "<export>" for k in sorted(e["data"]["labels"])}
            if e.get("device") == "contract":
                del e["device"]
        lines.append(normalize(json.dumps(e, sort_keys=True, ensure_ascii=False), home, fresh))
    err = normalize("".join(l for l in r.stderr.splitlines(True) if not re.match(r"warning: Git tree .* is dirty", l)),
                    home, fresh)
    if heavy:      # numbers standing alone (not inside an id or a name like mp4) are measurements
        num = r"(?<![0-9A-Za-z])\d+(?:\.\d+)?(?![0-9A-Za-z])"
        out, err = re.sub(num, "#", out), re.sub(num, "#", err)
    if argv[:1] and argv[0] in PARALLEL:
        out = "".join(sorted(out.splitlines(True)))
        lines = sorted(lines)
    nl = lambda t: t + ("" if t.endswith("\n") or not t else "\n")
    return (f"$ studio {shlex.join(argv)}\nexit {r.returncode}\n--- stdout\n{nl(out)}--- stderr\n{nl(err)}"
            + "--- events\n" + "".join(l + "\n" for l in lines))

golden.mkdir(parents=True, exist_ok=True)
index, results, failed = [], {}, []
for src, argv in cases:
    name = name_of(argv) + ".txt"
    t0 = time.time()
    results[name] = run(argv)
    if os.environ.get("CLI_CONTRACT_VERBOSE"):
        print(f"  {time.time() - t0:6.1f}s  studio {shlex.join(argv)}", file=sys.stderr, flush=True)
    index.append(f"{name}\t{src}")
    if not update:
        want = (golden / name).read_text() if (golden / name).exists() else None
        if want != results[name]:
            failed.append(name)
            import difflib
            sys.stdout.writelines(difflib.unified_diff((want or "").splitlines(True), results[name].splitlines(True),
                                                       f"golden/{name}", f"{binary}"))
idx = "".join(l + "\n" for l in index)
if update:
    for f in golden.glob("*.txt"):
        if f.name not in results:
            f.unlink()
    for n, t in results.items():
        (golden / n).write_text(t)
    (golden / "INDEX").write_text(idx)
    print(f"cli_contract: wrote {len(results)} goldens to {golden}")
else:
    stale = sorted(f.name for f in golden.glob("*.txt") if f.name not in results)
    if (golden / "INDEX").read_text() != idx:
        failed.append("INDEX (the command lines changed: --update after checking the diff)")
    if stale:
        failed.append(f"stale goldens {stale}")
    print(f"cli_contract: {len(results) - len([f for f in failed if f.endswith('.txt')])}/{len(results)} match"
          + (f"; FAILED: {failed}" if failed else ""))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if failed else 0)
PY
