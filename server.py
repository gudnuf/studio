"""The desktop studio's backend: a loopback-only HTTP server over the same library and event
log the CLI uses. Every change the UI makes is an event, so Claude (via the CLI) and the
operator (via the UI) always see one state. Stdlib only."""
import atexit, json, mimetypes, os, re, shutil, signal, socket, subprocess, sys, tempfile, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import studio as S

UI = S.HERE / "ui"
_job_n = [0]
_job_lock = threading.Lock()
_save_lock = threading.Lock()   # timeline compare-and-save
_waiting = {}             # submitted jobs that haven't got a lane yet: job -> {kind, ...info}
_home_lock = None         # held while serving: marks the server that owns crash recovery
_port = None              # the port this server listens on


# ---------- background jobs: one thread per job, hardware lanes do the limiting ---------
# A job waits only for the lane of the step it's on (S.LANES: media / whisper / ai), so a
# 10-minute transcription can't hold up the proxy for a file dropped in after it.

def submit(kind, fn, lane=None, **info):
    """Run fn in the background. lane=None means fn takes lanes itself (via run_pipeline's
    on_lane, passed as fn's `started` callback); otherwise the whole job runs in that lane."""
    with _job_lock:
        _job_n[0] += 1
        job = f"{int(time.time())}-{_job_n[0]}"
        _waiting[job] = {"job": job, "kind": kind, **info}
    S.emit("job.queued", None, job=job, kind=kind, **info)
    threading.Thread(target=run_job, args=(job, kind, fn, lane, info), daemon=True).start()
    return job

def run_job(job, kind, fn, lane, info):
    S._ctx.job = job
    started = threading.Event()
    def mark_started(*_):
        with _job_lock:
            if started.is_set():
                return
            started.set()
            _waiting.pop(job, None)
        S.emit("job.started", None, job=job, kind=kind, **info)
    try:
        if lane:
            with S.LANES[lane]:
                mark_started()
                result = fn(mark_started) or {}
        else:
            result = fn(mark_started) or {}
        mark_started()
        S.emit("job.done", None, job=job, kind=kind, **({"result": result} if isinstance(result, dict) else {}))
    except Exception as e:
        traceback.print_exc()
        mark_started()
        S.emit("job.failed", None, job=job, kind=kind, error=str(e)[:1500])

def steps_job(ids, steps, force=False):
    def fn(started):
        S.run_pipeline(ids, steps, force=force, quiet=True, on_lane=started)
    return fn


# ---------- /api/state, cached ------------------------------------------------
# Rebuilt only when the log grows, a moments/drafts/keyframes file changes, or the queue
# moves; otherwise it's the same bytes as last time.

_state_cache = {"key": None, "body": None, "mids": ()}
_state_lock = threading.Lock()
_SIDE = ("moments.json", "drafts.json", "keyframes/index.json")

def _side_sig(mids):
    sig = []
    for mid in mids:
        d = S.mdir(mid)
        for name in _SIDE:
            try:
                st = os.stat(d / name)
                sig.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                sig.append(None)
    return tuple(sig)

def state_body():
    with _state_lock:
        c = _state_cache
        lsig, queued = S.log_sig(), len(_waiting)
        qjobs = list(_waiting.values())
        if c["key"] and c["key"][0] == lsig:
            key = (lsig, queued, _side_sig(c["mids"]))
            if key == c["key"]:
                return c["body"]
        st = S.state()
        for mid, it in st["media"].items():
            d = S.mdir(mid)
            it["moments"] = S.load_json_cached(d / "moments.json", [])
            it["drafts"] = S.load_json_cached(d / "drafts.json", {})
            it["keyframes"] = S.load_json_cached(d / "keyframes" / "index.json", [])
            if it.get("path"):    # play the original until the proxy exists (Chrome does HEVC)
                it["source"] = f"media/{mid}/{Path(it['path']).name}"
            it.pop("path", None)
        st["jobs"] = {k: v for k, v in st["jobs"].items() if v["status"] == "running"}
        st["queued"] = queued
        st["queued_jobs"] = qjobs
        st["aspects"] = list(S.ASPECTS)
        st["brands"] = sorted(p.stem for p in (S.HERE / "brands").glob("*.md"))
        st["hw"] = {"videotoolbox": S.hw()["enc"], "render_jobs": S.render_pool_size()}
        mids = tuple(st["media"])
        c.update(key=(lsig, queued, _side_sig(mids)), body=json.dumps(st).encode(), mids=mids)
        return c["body"]


# ---------- http ------------------------------------------------------------

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the terminal quiet; the event log is the record
        pass

    def send_json(self, obj, code=200, headers=None):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def drain_body(self):
        """Discard a request body nobody read, so its bytes can't be parsed as the next
        request on this keep-alive connection."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        while n > 0:
            chunk = self.rfile.read(min(1 << 16, n))
            if not chunk:
                break
            n -= len(chunk)

    def send_file(self, path, cache=None):
        """Range-capable static files. Media under /files is content-stable per media id, so
        it caches (and revalidates by ETag); a ?v=<anything> URL is treated as immutable."""
        try:
            st = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return self.send_error(404)
        if not path.is_file():
            return self.send_error(404)
        size = st.st_size
        etag = f'"{st.st_mtime_ns:x}-{size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache or "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
        self.send_header("Cache-Control", cache or "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.flush()
            with open(path, "rb") as f:
                try:   # zero-copy from the page cache straight into the socket
                    self.connection.sendfile(f, start, end - start + 1)
                except (AttributeError, OSError, ValueError):
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(4 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def safe(self, root, rel):
        p = (root / unquote(rel)).resolve()
        return p if p.is_relative_to(root.resolve()) else None

    # -- GET --
    def do_HEAD(self):
        return self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/":
            return self.send_file(UI / "index.html")
        if path.startswith("/ui/"):
            p = self.safe(UI, path[4:])
            return self.send_file(p) if p else self.send_error(403)
        if path.startswith("/files/"):
            p = self.safe(S.HOME, path[7:])
            if not p:
                return self.send_error(403)
            rel = path[7:]
            if "v=" in u.query:
                cache = "private, max-age=31536000, immutable"
            elif rel.startswith("media/") and ("/keyframes/" in rel or rel.endswith("/proxy.mp4")
                                               or "/source." in rel):
                cache = "private, max-age=3600"     # regenerated only on --force; ETag catches it
            else:
                cache = "no-cache"
            return self.send_file(p, cache)
        if path == "/api/state":
            return self.send_json(state_body())
        if path == "/api/health":
            return self.send_json(health())
        if path.startswith("/api/transcript/"):
            mid = path.rsplit("/", 1)[1]
            return self.send_json(S.load_json_cached(S.mdir(mid) / "transcript.json",
                                                     {"segments": [], "words": []}))
        if path == "/api/activity":
            return self.send_json(S.tail_events(120, skip=("job.queued", "job.progress")))
        if path == "/api/stream":
            return self.stream()
        self.send_error(404)

    def stream(self):
        """Server-sent events: every new log line, so CLI edits show up in the UI live.
        In-process emits wake the stream at once; other writers (the CLI) are seen within
        100ms by polling the log size."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        log = S.log_path()
        pos = log.stat().st_size if log.exists() else 0
        last_beat = time.time()
        try:
            while True:
                try:
                    size = log.stat().st_size
                except FileNotFoundError:
                    size = 0
                if size < pos:
                    pos = 0                   # log was replaced
                if size > pos:
                    with open(log, "rb") as f:
                        f.seek(pos)
                        chunk = f.read(size - pos)
                    end = chunk.rfind(b"\n") + 1       # never send half a line
                    pos += end
                    out = b"".join(b"data: " + line + b"\n\n"
                                   for line in chunk[:end].splitlines() if line.strip())
                    if out:
                        self.wfile.write(out)
                        self.wfile.flush()
                        last_beat = time.time()
                elif time.time() - last_beat > 15:
                    self.wfile.write(b": beat\n\n"); self.wfile.flush()
                    last_beat = time.time()
                with S._emit_cv:
                    S._emit_cv.wait(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # -- POST / PUT --
    def do_PUT(self):
        self._body_read = False
        m = re.match(r"^/api/projects/([^/]+)/timeline$", urlparse(self.path).path)
        if not m:
            self.drain_body()
            return self.send_error(404)
        slug = m.group(1)
        body = self.body_json()
        base = parse_qs(urlparse(self.path).query).get("base", [None])[0]
        with _save_lock:
            # ?base=<rev the client edited from>: refuse a save that would silently overwrite
            # an edit (usually Claude's, via the CLI) the client hasn't seen. No base means 0
            # (a client that never saw a save): it can only write a story nobody has saved yet.
            # The check runs inside timeline_save's file lock, so a CLI save can't slip between.
            try:
                tl = S.timeline_save(slug, body, actor="operator",
                                     base=int(base) if base is not None and base.isdigit() else 0)
            except S.StaleRev:
                pj = S.state()["projects"].get(slug) or {}
                return self.send_json({"error": "stale", "conflict": True, "rev": pj.get("rev", 0),
                                       "actor": pj.get("rev_actor"),
                                       "timeline": pj.get("timeline")}, 409)
            rev = (S.state()["projects"].get(slug) or {}).get("rev", 0)
        self.send_json(tl, headers={"X-Timeline-Rev": str(rev)})

    def do_POST(self):
        self._body_read = False
        try:
            return self._post()
        finally:
            self.drain_body()

    def _post(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            if path == "/api/projects":
                b = self.body_json()
                return self.send_json({"slug": S.project_new(b["name"], b.get("brand", "jefe"))})
            if path == "/api/upload":
                return self.upload(q)
            if path == "/api/ingest-paths":
                # JSON only: a cross-site form can't send this content type without a CORS
                # preflight, which this server never answers, so no web page can make the
                # studio read files off this Mac
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self.send_json({"error": "send JSON"}, 415)
                b = self.body_json()
                return self.send_json(ingest_paths(b.get("paths") or [], b.get("project") or S.DEFAULT_PROJECT,
                                                   b.get("brand") or "jefe"))
            if path in ("/api/bundle", "/api/import"):
                # JSON only, like ingest-paths: these read and write folders on this Mac
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self.send_json({"error": "send JSON"}, 415)
                b = self.body_json()
                if path == "/api/bundle":
                    return self.send_json(share_job(b.get("projects") or [], b.get("out") or "",
                                                    bool(b.get("zip"))))
                return self.send_json(import_job(b.get("path") or ""))
            if path == "/api/run":
                b = self.body_json()
                steps = [s for s in b["steps"] if s in S.STEPS]
                job = submit("steps", steps_job(b["ids"], steps, b.get("force", False)),
                             ids=b["ids"], steps=steps)
                return self.send_json({"job": job})
            if (m := re.match(r"^/api/projects/([^/]+)/render$", path)):
                slug = m.group(1)
                job = submit("render", lambda _started: {"file": str(S.render(slug, actor="operator")
                                                                      .relative_to(S.HOME))},
                             lane="export", project=slug)
                return self.send_json({"job": job})
            if path == "/api/notes":
                b = self.body_json()
                ev = S.emit("note", None, text=b["text"].strip(), project=b.get("project"),
                            focus=b.get("focus"), actor="operator", id=S.clip_id())
                return self.send_json(ev)
            if (m := re.match(r"^/api/drafts/([0-9a-f]{6,64})$", path)):
                # the operator's rewrite of a post draft: into the log, so Claude can read it
                b = self.body_json()
                mid = m.group(1)
                if mid not in S.library():
                    return self.send_json({"error": f"no media {mid}"}, 404)
                ev = S.emit("draft.edited", mid, key=str(b["key"]), field=str(b["field"]),
                            text=b.get("text"), n=b.get("n"), actor="operator")
                return self.send_json(ev)
            if (m := re.match(r"^/api/words/([0-9a-f]{6,64})/fix$", path)):
                # the operator corrected a misheard word (double-click in the Words tab)
                b = self.body_json()
                mid, right = m.group(1), str(b.get("right") or "").strip()
                wrong = str(b.get("wrong") or "").strip()
                if mid not in S.library():
                    return self.send_json({"error": f"no media {mid}"}, 404)
                if not right or (b.get("all") and not wrong):
                    return self.send_json({"error": "say what the word should be"}, 400)
                files = changed = 0
                if b.get("all"):           # fix everywhere: every transcript in the library
                    for x in S.library():
                        n = S.fix_transcript(x, wrong, right, actor="operator")
                        changed += n; files += bool(n)
                else:
                    idx = b.get("index")
                    n = S.fix_transcript(mid, wrong, right, index=int(idx) if idx is not None else None,
                                         actor="operator")
                    changed, files = n, int(bool(n))
                return self.send_json({"changed": changed, "files": files})
            if path == "/api/focus":
                (S.HOME / "ui-focus.json").write_text(json.dumps(self.body_json(), indent=1))
                return self.send_json({"ok": True})
            if path == "/api/move":
                b = self.body_json()
                S.emit("media.moved", b["media"], project=b["project"])
                return self.send_json({"ok": True})
            if path == "/api/reveal":
                b = self.body_json()
                p = self.safe(S.HOME, b["path"])
                if p and sys.platform == "darwin":
                    subprocess.run(["open", "-R", str(p)])
                return self.send_json({"ok": bool(p)})
        except Exception as e:
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 400)
        self.send_error(404)

    def upload(self, q):
        name = Path(unquote(q.get("name", ["upload.bin"])[0])).name
        project = q.get("project", [S.DEFAULT_PROJECT])[0]
        brand = q.get("brand", ["jefe"])[0]
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        up = upload_path(name)
        left = n
        try:
            self.connection.settimeout(UPLOAD_STALL_SECS)   # a client that stops sending
            with open(up, "wb") as f:
                while left > 0:
                    try:
                        chunk = self.rfile.read(min(1 << 20, left))
                    except (OSError, socket.timeout):
                        chunk = b""
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
        except OSError as e:           # disk full, permissions: nothing half-written stays
            up.unlink(missing_ok=True)
            self.close_connection = True
            return self.send_json({"error": f"couldn't save the upload: {e}"}, 500)
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
        if left > 0 or n == 0:
            # cut off part way (or nothing sent): never ingest a partial file as footage
            up.unlink(missing_ok=True)
            self.close_connection = True
            try:
                return self.send_json({"error": f"upload incomplete: got {n - left} of {n} bytes "
                                                f"of {name}; nothing was added"}, 400)
            except OSError:
                return
        def fn(started):
            started()                          # hashing + probing starts right away
            problems = []
            try:
                ids = S.ingest([up], brand, project, names={str(up): name}, settle=0,
                               problems=problems)
            finally:
                up.unlink(missing_ok=True)
            return prepare(ids, name, problems)
        job = submit("upload", fn, name=name, project=project)
        self.send_json({"job": job})


def prepare(ids, name, problems):
    """After ingest: the local steps for what came in. Shared by uploads and path adds."""
    if not ids and problems:           # unreadable footage: say so (toast), don't hide it
        raise RuntimeError(f"couldn't add {name}: {problems[0][1]}")
    job = getattr(S._ctx, "job", None)
    todo = [m for m in ids if not all(s in S.library()[m]["steps"] for s in S.LOCAL_STEPS)]
    if todo:   # until a step gets its lane this file is only waiting: say so
        S.emit("job.progress", None, job=job, step="waiting", media_id=todo[0])
    def on_lane(step, mid):
        S.emit("job.progress", None, job=job, step=step, pct=0, media_id=mid)
    # proxy first (playable in seconds), keyframes right after it, transcription in
    # parallel on the GPU lane
    S.run_pipeline(ids, S.LOCAL_STEPS, quiet=True, on_lane=on_lane)
    return {"ids": ids}


# ---------- adding files where they sit (the desktop app's drag and drop) ----------------
# No HTTP upload: the server reads the operator's file in place and clones it into the
# library (APFS: instant, no extra space). The original is never moved or changed.

MEDIA_EXT = S.VIDEO_EXT | S.AUDIO_EXT
PACKAGES = {".app", ".photoslibrary", ".fcpbundle", ".imovielibrary", ".tvlibrary", ".bundle"}
MAX_PATHS = 1000
SETTLE_WAIT_SECS = float(os.environ.get("STUDIO_SETTLE_WAIT_SECS", 900))

def expand_paths(paths):
    """Dropped paths -> (media files in drop order, [(name, reason)] skipped). A folder gives
    the media inside it (and its subfolders, by name); hidden files and app/library packages
    are left alone."""
    files, skipped, seen = [], [], set()
    def add(f):
        k = os.path.realpath(f)
        if k in seen:
            return
        seen.add(k)
        if f.suffix.lower() in MEDIA_EXT:
            files.append(f)
        else:
            skipped.append((f.name, "not a video or audio file"))
    for raw in paths:
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            skipped.append((str(raw), "not a full path"))
            continue
        if p.is_dir() and p.suffix.lower() not in PACKAGES:
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                                 and Path(d).suffix.lower() not in PACKAGES)
                for n in sorted(names):
                    if not n.startswith("."):
                        add(Path(root) / n)
        elif p.is_file():
            add(p)
        else:
            skipped.append((p.name or str(p), "it's not there any more" if not p.exists()
                            else "not a file or folder"))
    if len(files) > MAX_PATHS:
        skipped.append((f"{len(files) - MAX_PATHS} more files", f"only {MAX_PATHS} at a time"))
        files = files[:MAX_PATHS]
    return files, skipped

def wait_settled(f, job):
    """A file still being written (AirDrop, a copy from a card) is waited for, not refused:
    it counts as settled once it hasn't changed for STUDIO_SETTLE_SECS."""
    end = time.time() + SETTLE_WAIT_SECS
    said = False
    while True:
        st = f.stat()
        if time.time() - st.st_mtime >= S.SETTLE_SECS:
            return
        if time.time() > end:
            raise RuntimeError(f"couldn't add {f.name}: it's still being copied")
        if not said:
            S.emit("job.progress", None, job=job, step="waiting", name=f.name)
            said = True
        time.sleep(0.5)

def ingest_paths(paths, project, brand):
    """POST /api/ingest-paths: one job per media file (the first dropped is the first ready),
    each with the same safety rules as `studio ingest` (settled, hashed, probed in place,
    deduped by hash) but never moving the original."""
    files, skipped = expand_paths(paths if isinstance(paths, list) else [paths])
    S.ensure_project(project)
    jobs = []
    for f in files:
        def fn(started, f=f):
            started()
            wait_settled(f, getattr(S._ctx, "job", None))
            problems = []
            ids = S.ingest([f], brand, project, names={str(f): f.name}, settle=S.SETTLE_SECS,
                           problems=problems, move=False)
            return prepare(ids, f.name, problems)
        jobs.append(submit("upload", fn, name=f.name, project=project, via="path"))
    return {"jobs": jobs, "files": [f.name for f in files],
            "skipped": [{"name": n, "reason": r} for n, r in skipped]}


# ---------- sharing: "Share project…" and "Import…" (studio bundle / studio import) -------

def _abs_folder(p):
    p = Path(str(p)).expanduser()
    if not str(p).strip() or not p.is_absolute():
        raise ValueError("give the full path of a folder (it starts with / or ~)")
    return p

def share_job(projects, out, zip_it):
    """POST /api/bundle {projects, out, zip}: write a bundle in the background. Checks what
    it can up front, so a bad folder or project is an error now, not a failed job later."""
    st = S.state()
    projects = [p for p in projects if isinstance(p, str)]
    if not projects:
        raise ValueError("choose at least one project to share")
    bad = [p for p in projects if p not in st["projects"]]
    if bad:
        raise ValueError(f"no project {', '.join(bad)}")
    out = _abs_folder(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} already exists; pick a new folder name")
    def fn(started):
        started()
        return S.bundle(out, projects, zip_it=zip_it, log=lambda m: None)
    return {"job": submit("share", fn, projects=projects, out=str(out)), "out": str(out)}

def import_job(path):
    """POST /api/import {path}: merge a bundle (checked first), then prepare previews in the
    same job, so the footer shows one task from start to finish."""
    b = S.find_bundle(_abs_folder(path))           # not a bundle: say so now
    def fn(started):
        started()
        r = S.import_bundle(b, log=lambda m: None, prepare=False, actor="operator")
        todo = [m for m in r["media"] if any(s not in S.library()[m]["steps"] for s in S.LOCAL_STEPS)]
        if todo:
            S.run_pipeline(todo, S.LOCAL_STEPS, quiet=True, on_lane=lambda *_: None)
        return {k: r[k] for k in ("media", "already", "projects", "renamed", "events")}
    return {"job": submit("import", fn, bundle=b.name), "bundle": str(b)}


# ---------- health, the library's lockfile, and exiting with the desktop app --------------

def health():
    """What the desktop app checks before attaching to a running server, and what the UI
    reads to say plainly when a tool or the speech model is missing."""
    tools = {t: shutil.which(t) for t in ("ffmpeg", "ffprobe", "whisper-cli", "claude")}
    return {"ok": True, "app": "studio", "pid": os.getpid(), "port": _port,
            "home": str(S.HOME.resolve()), "owner": _home_lock is not None,
            "tools": tools, "model": {"path": str(S.MODEL), "exists": S.MODEL.exists()}}

def lock_path():
    return S.HOME / ".server.json"

def write_lock(port):
    """$STUDIO_HOME/.server.json: which server owns this library, so the desktop app attaches
    to it instead of starting a second one. Only the owner (the crash-recovery lock holder)
    writes it; readers confirm it with GET /api/health, so a stale file is harmless."""
    info = {"pid": os.getpid(), "port": port, "url": f"http://127.0.0.1:{port}/",
            "home": str(S.HOME.resolve()), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = lock_path().with_name(f".server.json.{os.getpid()}")
    tmp.write_text(json.dumps(info))
    os.replace(tmp, lock_path())
    atexit.register(remove_lock)

def remove_lock():
    try:
        if json.loads(lock_path().read_text()).get("pid") == os.getpid():
            lock_path().unlink()
    except (OSError, ValueError):
        pass

def _quit(*_):
    """Leave nothing behind: the lockfile goes, and (when the app started us as a process
    group) the ffmpeg/whisper/claude children go with us."""
    remove_lock()
    if os.environ.get("STUDIO_PARENT_PID") and os.getpgid(0) == os.getpid():
        # (runs on a watcher thread, where signal.signal() isn't allowed; the TERM this sends
        # us too only re-enters _quit on the main thread, which exits the same way)
        try:
            os.killpg(os.getpid(), signal.SIGTERM)
        except OSError:
            pass
    os._exit(0)

def watch_parent():
    """Started by the desktop app (STUDIO_PARENT_PID set): exit when the app goes away, even
    if it crashed. Two signals: stdin (a pipe from the app) reaching EOF, and the parent pid
    changing."""
    ppid = int(os.environ["STUDIO_PARENT_PID"])
    def stdin_eof():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        _quit()
    def parent_gone():
        while True:
            time.sleep(1)
            if os.getppid() != ppid:
                _quit()
    if not sys.stdin.isatty():
        threading.Thread(target=stdin_eof, daemon=True).start()
    threading.Thread(target=parent_gone, daemon=True).start()


UPLOAD_STALL_SECS = float(os.environ.get("STUDIO_UPLOAD_STALL_SECS", 120))

def upload_path(name):
    """A fresh, unique file in uploads/ (two uploads of the same name at the same moment get
    two files), keeping the extension so ingest knows what it is."""
    d = S.HOME / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    suffix = Path(name).suffix.lower()[:12]
    fd, path = tempfile.mkstemp(dir=d, prefix="up-", suffix=suffix)
    os.close(fd)
    return Path(path)

def heal(log=print):
    """At start: what a crash left behind. Upload temp files (no upload outlives its server),
    half-finished ingests (S.recover), and files whose local steps never finished: those
    steps are queued again. Returns the job id of that resubmission, if any."""
    up = S.HOME / "uploads"
    for f in (up.iterdir() if up.is_dir() else []):
        if f.is_file():
            f.unlink(missing_ok=True)
    S.recover(log=log)
    lib = S.library()
    todo = [m for m, it in lib.items() if any(s not in it["steps"] for s in S.LOCAL_STEPS)
            and Path(it.get("path", "")).exists()]
    if not todo:
        return None
    log(f"resuming local steps for {len(todo)} file(s): {' '.join(todo)}")
    return submit("steps", steps_job(todo, S.LOCAL_STEPS), ids=todo, steps=list(S.LOCAL_STEPS),
                  resumed=True)

def open_app_window(url):
    chrome = Path("/Applications/Google Chrome.app")
    if sys.platform == "darwin" and chrome.exists():
        profile = Path.home() / ".local/share/studio/window"
        subprocess.Popen(["open", "-na", str(chrome), "--args", f"--app={url}",
                          f"--user-data-dir={profile}", "--window-size=1500,960"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])

def serve(port=7317, open_window=True):
    for d in ("inbox", "media", "projects", "uploads"):
        (S.HOME / d).mkdir(parents=True, exist_ok=True)
    # Jobs that were running when the last server died will never finish; say so in the log.
    for job, j in S.state()["jobs"].items():
        if j["status"] == "running":
            S.emit("job.failed", None, job=job, kind=j.get("kind"), error="server restarted")
    threading.Thread(target=S.hw, daemon=True).start()   # probe VideoToolbox off the request path
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    global _port
    _port = port
    # only the first server on this library cleans up after a crash (a second one, on another
    # port, must not delete the first one's in-flight uploads)
    import fcntl
    global _home_lock
    _home_lock = open(S.HOME / ".server.lock", "a")
    try:
        fcntl.flock(_home_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_lock(port)
        heal(log=lambda m: print(m, flush=True))
    except BlockingIOError:
        _home_lock.close()
        _home_lock = None
        print("another studio server is using this library; skipping crash recovery", flush=True)
    signal.signal(signal.SIGTERM, _quit)
    if os.environ.get("STUDIO_PARENT_PID"):
        watch_parent()
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"studio ui → {url}   (library {S.HOME})", flush=True)
    S.emit("ui.started", None, url=url)
    if open_window:
        open_app_window(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        remove_lock()
