"""Shared helpers for the studio test + bench harness (stdlib only).

Everything here talks to studio through its documented contract: the CLI (studio.py), the
HTTP API (server.py) and the event log. Nothing writes to ~/Studio: every home is a temp dir
under $STUDIO_TEST_TMP (or the system temp dir).
"""
import http.client, json, os, shutil, signal, socket, subprocess, sys, tempfile, time
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parent
STUDIO_PY = ROOT / "studio.py"
FIXTURE_FILES = ["phone-talking.mov", "screen-recording.mp4", "site-walk.mov",
                 "voice-memo.m4a", "junk.txt"]
REAL_HOME = Path.home() / "Studio"
# made by fixtures.sh too, but not ingested by the CLI suite's shared home
QUALITY_FIXTURES = ["bursts.mov", "bursts.json", "broll-roomtone.mov"]


def have_tools(*tools):
    return all(shutil.which(t) for t in tools)


def tmp_root():
    base = os.environ.get("STUDIO_TEST_TMP")
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
    return base


def new_home(label="home"):
    home = Path(tempfile.mkdtemp(prefix=f"studio-{label}-", dir=tmp_root()))
    assert home.resolve() != REAL_HOME.resolve()
    for d in ("inbox", "media", "projects", "uploads"):
        (home / d).mkdir(parents=True, exist_ok=True)
    return home


def fixtures():
    """Path to the fixture dir, generating (cached) via fixtures.sh if needed."""
    d = os.environ.get("STUDIO_FIXTURES")
    if d and all((Path(d) / f).exists() for f in FIXTURE_FILES + QUALITY_FIXTURES):
        return Path(d)
    cmd = ["bash", str(TEST_DIR / "fixtures.sh")] + ([d] if d else [])
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"fixtures.sh failed:\n{out.stderr[-2000:]}")
    path = Path(out.stdout.strip().splitlines()[-1])
    os.environ["STUDIO_FIXTURES"] = str(path)
    return path


# What says "an agent is driving this" (studio.py's AGENT_MARKERS, STUDIO_ACTOR) and which
# agent the AI steps call: none of it leaks in from the shell the tests run in.
AGENT_ENV = ("CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX", "GOOSE_TERMINAL", "STUDIO_ACTOR",
             "STUDIO_AGENT", "STUDIO_AGENT_CMD", "STUDIO_AGENT_MODEL", "STUDIO_MOMENTS_MODEL",
             "STUDIO_CLAUDE_MODEL")


def env_for(home, as_agent=False, **extra):
    env = {k: v for k, v in os.environ.items() if k not in AGENT_ENV}
    env["STUDIO_HOME"] = str(home)
    if as_agent:
        env["STUDIO_ACTOR"] = "agent"
    env.update(extra)
    return env


def cli(home, *args, check=True, as_agent=False, input=None, timeout=600, env=None):
    r = subprocess.run([sys.executable, str(STUDIO_PY), *map(str, args)], env=env or env_for(home, as_agent),
                       capture_output=True, text=True, input=input, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"studio {' '.join(map(str, args))} failed ({r.returncode}):\n"
                             f"{r.stdout[-1500:]}\n{r.stderr[-2500:]}")
    return r


def events(home):
    p = Path(home) / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def import_studio(home):
    """Import studio.py pointed at `home`. HOME is read at import time, so set both."""
    os.environ["STUDIO_HOME"] = str(home)
    sys.path.insert(0, str(ROOT))
    import studio as S  # noqa
    S.HOME = Path(home)
    return S


def fake_moments(home, mid, duration, n=3, title="Moment"):
    """Write a hand-made moments.json and emit step.moments (no agent call)."""
    S = import_studio(home)
    span = duration / n
    ms = [{"n": i + 1, "start": round(i * span + 0.5, 2), "end": round((i + 1) * span - 0.5, 2),
           "title": f"{title} {i + 1}", "hook": f"Hook line {i + 1}", "pillar": "time back",
           "why": "fixture", "angle": "past-self", "kind": "talking"} for i in range(n)]
    d = Path(home) / "media" / mid
    (d / "moments.json").write_text(json.dumps(ms, indent=1))
    S.emit("step.moments", mid, moments=len(ms), fixture=True)
    return ms


def fake_drafts(home, mid, ms):
    S = import_studio(home)
    dr = {"posts": [{"n": m["n"], "short": f"{m['hook']}\nline two", "x": f"X post {m['n']}",
                     "linkedin": f"LinkedIn {m['n']}\nmore", "title": m["title"]} for m in ms],
          "newsletter": "Newsletter paragraph.", "ideas": ["idea a", "idea b", "idea c"]}
    (Path(home) / "media" / mid / "drafts.json").write_text(json.dumps(dr, indent=1))
    S.emit("step.draft", mid, posts=len(ms), fixture=True)
    return dr


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", str(path)], capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"duration": float(d["format"]["duration"]),
            "width": v and int(v["width"]), "height": v and int(v["height"]),
            "vcodec": v and v["codec_name"], "acodec": a and a["codec_name"],
            "has_audio": a is not None, "has_video": v is not None}


# ---------- server ----------------------------------------------------------

def port_range():
    """Ports tests may use: $STUDIO_TEST_PORTS ("7700-7799"), default 7400-7499."""
    lo, _, hi = os.environ.get("STUDIO_TEST_PORTS", "7400-7499").partition("-")
    return int(lo), int(hi or lo)


_handed_out = set()


def free_port(lo=None, hi=None):
    d_lo, d_hi = port_range()
    lo, hi = lo or d_lo, hi or d_hi
    for p in range(lo, hi + 1):
        if p in _handed_out:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                continue
        _handed_out.add(p)
        return p
    raise RuntimeError(f"no free port in {lo}-{hi}")


class Server:
    """server.serve(port, open_window=False) in a subprocess with its own STUDIO_HOME."""

    def __init__(self, home, port=None):
        self.home, self.port = Path(home), port or free_port()
        code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import server; "
                f"server.serve({self.port}, open_window=False)")
        self.log = open(self.home / "server.stderr", "w")
        self.proc = subprocess.Popen([sys.executable, "-c", code], env=env_for(home),
                                     stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server died:\n{(self.home / 'server.stderr').read_text()}")
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("server did not come up")

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()

    # -- http --
    def request(self, method, path, body=None, headers=None, timeout=30):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def json(self, method, path, body=None, timeout=30):
        st, _, data = self.request(method, path, body, timeout=timeout)
        if st >= 400:
            raise AssertionError(f"{method} {path} -> {st}: {data[:400]!r}")
        return json.loads(data)

    def state(self):
        return self.json("GET", "/api/state")

    def raw_get(self, raw_path, timeout=10, host=None):
        """Send the request line verbatim (no client-side path normalisation). `host` is the
        Host header (default 127.0.0.1:PORT; "" sends none)."""
        host = f"127.0.0.1:{self.port}" if host is None else host
        hdr = f"Host: {host}\r\n" if host else ""
        s = socket.create_connection(("127.0.0.1", self.port), timeout)
        s.sendall(f"GET {raw_path} HTTP/1.1\r\n{hdr}Connection: close\r\n\r\n".encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        return int(head.split()[1]), body

    def upload(self, path, project="inbox", brand="jefe", name=None):
        from urllib.parse import quote
        name = name or Path(path).name
        data = Path(path).read_bytes()
        return self.json("POST", f"/api/upload?project={quote(project)}&name={quote(name)}"
                                 f"&brand={quote(brand)}", data)

    def wait(self, pred, timeout=240, every=0.1, what="condition"):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.state()
            r = pred(last)
            if r:
                return r
            time.sleep(every)
        raise AssertionError(f"timed out after {timeout}s waiting for {what}; "
                             f"last state jobs={last and last.get('jobs')} queued={last and last.get('queued')}")


class SSE:
    """Minimal SSE reader on a raw socket; collects (arrival_time, event_dict)."""

    def __init__(self, port):
        import threading
        self.events, self.sock = [], socket.create_connection(("127.0.0.1", port), 5)
        self.sock.sendall(f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          "Accept: text/event-stream\r\n\r\n".encode())
        self.ready = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        self.ready.wait(5)

    def _run(self):
        buf = b""
        header_done = False
        self.sock.settimeout(None)
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return
                now = time.time()
                buf += chunk
                if not header_done:
                    if b"\r\n\r\n" not in buf:
                        continue
                    head, buf = buf.split(b"\r\n\r\n", 1)
                    self.status = int(head.split()[1])
                    header_done = True
                    self.ready.set()
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    for line in block.decode(errors="replace").splitlines():
                        if line.startswith("data:"):
                            try:
                                self.events.append((now, json.loads(line[5:].strip())))
                            except ValueError:
                                pass
        except OSError:
            return

    def wait_for(self, pred, since=0, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for t, ev in self.events[since:]:
                if pred(ev):
                    return t, ev
            time.sleep(0.005)
        return None, None

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
