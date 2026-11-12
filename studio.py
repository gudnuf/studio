#!/usr/bin/env python3
"""studio — raw footage in, post drafts and edits out. Local-first; the event log is the truth.

  studio ui                                start the desktop studio (http://127.0.0.1:7317)
  studio run   [-p project] [--brand jefe] [files...]   ingest inbox (or files), run every step
  studio ingest [-p project] [files...]    move footage from inbox/ into the library
  studio proxy|speech|transcribe|keyframes|moments|cut|draft ID... | --all [--force]   one step
                                           (moments and draft call your agent: see below)
  studio media show ID                     one file: steps, moments (n, times, title, hook)
  studio media move ID PROJECT             file it under another project (ingest never moves
                                           footage out of a project, only out of the inbox)
  studio words ID [--from T --to T] [--grep WORD]   transcript words with timings
  studio words ID --fix WRONG RIGHT | --fix WRONG RIGHT --all   fix a misheard word
  studio posts ID [--json]                 post drafts, with the operator's rewrites
  studio project new NAME | ls             projects group media and own a timeline
  studio timeline show [--json] PROJECT    the story, with its rev
  studio timeline add|clear|set|trim|move|split|rm|label|fit|snap PROJECT ... [--base REV]
                                           edit it; CLIP = 1-based number (as the UI shows:
                                           "Clip 3 of 4" is 3) or id (ids are stable)
  studio timeline fit PROJECT CLIP fill|fit|auto   crop to fill, or show the whole frame
  studio timeline snap PROJECT CLIP|all    put spoken clips on speech edges (add/trim/set take
                                           --snap; add MEDIA@N snaps unless --no-snap)
  studio focus [--brief]                   what the operator has selected in the UI
  studio render PROJECT                    export the timeline through ffmpeg (loudness -14 LUFS)
  studio check PROJECT [EXPORT] [--json]   measure the export against QUALITY.md, write a review
                                           sheet next to it; exits 1 if a hard rule fails
  studio ready PROJECT [EXPORT]            is it checked? exit 0 only if a check of these exact
                                           bytes passes (or every failure has an exception)
  studio exception grant P RULE [CLIP] --why TEXT | revoke P ID | ls P
                                           bend a hard rule on purpose, in words; listed by check
                                           (--onscreen TEXT / --card: declare a hook in the picture
                                           or a designed card; bound to the clip as it is now)
  studio choose P CHOSEN|none --from A,B,C | choose P LABEL --round R
                                           log a pick: what was shown, what was chosen
  studio ab PROJECT [EXPORT...]            a blind A/B of versions (shuffled, mapping logged)
  studio notes [-p P] [--unread] [--json] [--ack ID..]   notes the operator left, with
                                           what they had selected when they wrote each
  studio say "TEXT" [-p P] [--re NOTE_ID]  reply to the operator (shows in the UI)
  studio review                            write review.html and open it
  studio bundle OUT_DIR -p PROJECT [-p ...] | --all [--zip]   share projects with a teammate:
                                           the original footage + everything done to it, in
                                           one folder (and OUT_DIR.zip, stored, with --zip)
  studio import BUNDLE_DIR [--no-previews]   merge a shared bundle into this library (checks
                                           every file first; importing twice changes nothing)
  studio ls [-p project]                   library status
  studio log [-n N]                        tail the event log
  studio agent                             which agent moments/draft call, and how

Home is $STUDIO_HOME (default ~/Studio). Needs ffmpeg and whisper-cli on PATH (`nix develop`
in this directory provides them) and, for moments and draft, a coding agent's CLI:
STUDIO_AGENT=claude|codex|goose (default: the first of those on PATH), or STUDIO_AGENT_CMD
for any other command that reads a prompt on stdin and prints the reply.
"""
import argparse, bisect, copy, ctypes, datetime, errno, hashlib, html, itertools, json, math, os, re, shlex, shutil, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path(os.environ.get("STUDIO_HOME", Path.home() / "Studio"))
HERE = Path(__file__).resolve().parent
MODEL = Path(os.environ.get("STUDIO_WHISPER_MODEL",
             Path.home() / ".local/share/studio/models/ggml-large-v3-turbo.bin"))
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mkv", ".webm", ".avi"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".ogg", ".flac"}
LOCAL_STEPS = ["proxy", "speech", "transcribe", "keyframes"]   # cheap, on-device, at ingest
AI_STEPS = ["moments", "cut", "draft"]                    # call the agent, run on request
STEPS = LOCAL_STEPS + AI_STEPS
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
DEFAULT_PROJECT = "inbox"


# ---------- event log -------------------------------------------------------

_log_lock = threading.Lock()
_emit_cv = threading.Condition()   # in-process listeners (the SSE stream) wake on every emit
_ctx = threading.local()           # .job: the server job this thread works for (progress events)

def log_path():
    return HOME / "events.jsonl"

def uuid7():
    """A UUIDv7 (RFC 9562), monotonic within a process: Python 3.14's uuid.uuid7 (method 3, a
    sub-millisecond counter): needs 3.14, the flake's python3. docs/events.md §2 relies on the order."""
    import uuid
    return str(uuid.uuid7())

def device_name():
    """Which device wrote an event (docs/events.md): STUDIO_DEVICE, else the short hostname."""
    import socket
    return os.environ.get("STUDIO_DEVICE") or socket.gethostname().split(".")[0] or "unknown"

def emit(type_, media=None, **data):
    ev = {"id": uuid7(), "device": device_name(),
          "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
          "type": type_, "media": media, "data": data}
    append_events([ev])
    return ev

def append_events(evs):
    """Append whole events (keeping their own ts) in one locked write: emit() for one new
    event, `studio import` for a bundle's history."""
    if not evs:
        return
    line = "".join(json.dumps(ev) + "\n" for ev in evs).encode()
    import fcntl
    with _log_lock:
        # One locked O_APPEND write per event, so CLI and server writers never interleave a
        # line. If an earlier writer died mid-line (no trailing newline), start on a fresh line,
        # or that fragment would swallow this event when the log is read back.
        fd = os.open(log_path(), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            view = memoryview(line)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError(f"event log write stopped short ({len(view)} bytes left)")
                view = view[n:]
        finally:
            os.close(fd)               # closing drops the flock
    with _emit_cv:
        _emit_cv.notify_all()

def _bad_line(line, where):
    print(f"warning: skipping an unreadable line in {log_path()} ({where}): "
          f"{line[:120]!r}", file=sys.stderr)

def events():
    if not log_path().exists():
        return []
    out = []
    for i, l in enumerate(log_path().read_text(errors="replace").splitlines()):
        if l.strip():
            try:
                out.append(json.loads(l))
            except ValueError:
                _bad_line(l, f"line {i + 1}")
    return out

def tail_events(n, skip=()):
    """The last n events (minus types in skip) without reading the whole log."""
    p = log_path()
    if not p.exists():
        return []
    size, block, data = p.stat().st_size, 1 << 18, b""
    with open(p, "rb") as f:
        while True:
            start = max(0, size - block)
            f.seek(start)
            data = f.read(size - start)
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]          # first line is probably partial
            evs = []
            for l in lines:
                if l.strip():
                    try:
                        ev = json.loads(l)
                    except ValueError:
                        continue
                    if ev.get("type") not in skip:
                        evs.append(ev)
            if len(evs) >= n or start == 0:
                return evs[-n:]
            block *= 4

def _new_acc():
    return {"media": {}, "projects": {}, "notes": [], "jobs": {}, "progress": {}}

def _apply(acc, ev):
    lib, projects, notes, jobs = acc["media"], acc["projects"], acc["notes"], acc["jobs"]
    m, d, t = ev["media"], ev["data"], ev["type"]
    if t == "project.created":
        # rev 0 from the start: a never-saved project still has a revision, so the UI's
        # first save carries ?base=0 and can't silently overwrite an edit made meanwhile
        projects[d["slug"]] = {"slug": d["slug"], "name": d["name"], "created": ev["ts"],
                               "brand": d.get("brand", "jefe"),
                               "timeline": empty_timeline(), "rev": 0}
    elif t == "timeline.saved" and d["project"] in projects:
        pj = projects[d["project"]]
        pj["timeline"] = d["timeline"]
        pj["rev"] = pj.get("rev", 0) + 1          # saves so far: the UI's stale-write guard
        pj["rev_actor"] = d.get("actor")
    elif t == "ingested":
        lib[m] = {**d, "id": m, "steps": {}, "project": d.get("project", DEFAULT_PROJECT)}
        # The log records where the source landed, but a library must survive being moved or
        # handed to a teammate: always resolve the source inside *this* library's media dir.
        lib[m]["path"] = str(HOME / "media" / m / Path(d.get("path", "source")).name)
    elif t == "media.moved" and m in lib:
        lib[m]["project"] = d["project"]
    elif t == "job.progress":
        if d.get("job") in jobs:
            j = jobs[d["job"]]
            j["progress"] = {k: d[k] for k in ("step", "pct") if k in d} | {"media": m or d.get("media_id")}
            if d.get("step") and d.get("step") != "waiting":   # per-step %, so a job's
                parts = j.setdefault("parts", {})              # overall % can only rise
                parts[d["step"]] = max(parts.get(d["step"], 0), d.get("pct") or 0)
            if m or d.get("media_id"):
                j["media_id"] = m or d.get("media_id")
        if m:
            acc["progress"][m] = {"step": d.get("step"), "pct": d.get("pct"), "ts": ev["ts"]}
    elif t.startswith("step.") and t != "step.failed" and m in lib:
        lib[m]["steps"][t[5:]] = d
        lib[m].pop("error", None)
        if t == "step.moments":           # new moments: old post edits belong to old moments
            lib[m].pop("draft_edits", None)
        if acc["progress"].get(m, {}).get("step") == t[5:]:
            acc["progress"].pop(m)
    elif t == "step.failed" and m in lib:
        lib[m]["error"] = {"step": d["step"], "error": d["error"][:400]}
        if acc["progress"].get(m, {}).get("step") == d["step"]:
            acc["progress"].pop(m)
    elif t == "brand.set" and m in lib:
        lib[m]["brand"] = d["brand"]
    elif t == "transcript.edited" and m in lib and "transcribe" in lib[m]["steps"]:
        # a word fix rewrote transcript.json: a new receipt, so every client refetches it
        tr = lib[m]["steps"]["transcribe"]
        lib[m]["steps"]["transcribe"] = {**tr, "sha256": d.get("sha256"),
                                         "edits": tr.get("edits", 0) + 1}
    elif t == "note":
        notes.append({"ts": ev["ts"], **d, "id": d.get("id") or ev["ts"]})
    elif t == "note.ack":
        ids = set(d.get("ids") or [])
        for n in notes:
            if n["id"] in ids and not n.get("acked"):
                n["acked"] = ev["ts"]
    elif t == "draft.edited" and m in lib:
        eds = lib[m].setdefault("draft_edits", {})
        k, f = str(d.get("key")), d.get("field")
        if d.get("text") is None:
            eds.get(k, {}).pop(f, None)
            if not eds.get(k):
                eds.pop(k, None)
        else:
            eds.setdefault(k, {})[f] = d["text"]
    elif t == "job.started":
        jobs[d["job"]] = {**d, "status": "running", "ts": ev["ts"]}
    elif t in ("job.done", "job.failed") and d["job"] in jobs:
        jobs[d["job"]].update(status=t[4:], **{k: v for k, v in d.items() if k != "job"})
        jobs[d["job"]].pop("progress", None)
        if len(jobs) > 400:               # finished jobs are history; the log keeps them
            for k in [k for k, j in jobs.items() if j["status"] != "running"][:200]:
                del jobs[k]
    elif t == "render.done" and d["project"] in projects:
        projects[d["project"]].setdefault("exports", []).append({**d, "ts": ev["ts"]})
    elif t == "check.done" and d.get("project") in projects and d.get("sha256"):
        # a check record binds to the exact bytes it measured (see export_verdict)
        chk = projects[d["project"]].setdefault("checks", {})
        chk[d["sha256"]] = {k: d[k] for k in ("sha256", "file", "ok", "failed", "failures", "actor")
                            if k in d} | {"ts": ev["ts"]}
    elif t == "exception.granted" and d.get("project") in projects:
        projects[d["project"]].setdefault("exceptions", {})[d["id"]] = {**d, "ts": ev["ts"]}
    elif t == "exception.revoked" and d.get("project") in projects:
        x = projects[d["project"]].get("exceptions", {}).get(d.get("id"))
        if x and not x.get("revoked"):
            x["revoked"] = {"ts": ev["ts"], "actor": d.get("actor"), "why": d.get("why")}
    elif t == "blind.round" and d.get("project") in projects:
        projects[d["project"]].setdefault("rounds", {})[d["round"]] = {**d, "ts": ev["ts"]}
    elif t == "project.shelved" and d.get("project") in projects:
        projects[d["project"]]["shelved"] = {"ts": ev["ts"], "why": d.get("why"), "actor": d.get("actor")}

def _finish(acc):
    st = copy.deepcopy(acc)
    for pj in st["projects"].values():
        for ex in pj.get("exports", []):
            ex["verdict"] = export_verdict(pj, ex)
        if pj.get("exports"):
            pj["ready"] = pj["exports"][-1]["verdict"]["state"] == "ready"
    for it in st["media"].values():
        if it["project"] not in st["projects"]:
            st["projects"].setdefault(it["project"], {"slug": it["project"], "name": it["project"],
                                                      "created": None, "brand": "jefe",
                                                      "timeline": empty_timeline(), "rev": 0})
    return st

_fold = {"file": None, "pos": 0, "acc": None, "lock": threading.Lock()}

def log_sig():
    """Cheap identity of the log's current contents: (inode, size, mtime)."""
    try:
        s = os.stat(log_path())
        return (s.st_ino, s.st_size, s.st_mtime_ns)
    except FileNotFoundError:
        return None

def state():
    """Fold the log into current state. Nothing else is authoritative.
    The fold is incremental: only bytes appended since the last call are parsed, so this
    stays fast with any number of events. Callers get their own copy to mutate."""
    p = log_path()
    with _fold["lock"]:
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return _finish(_new_acc())
        ident = (str(p), st.st_dev, st.st_ino)
        if _fold["acc"] is None or _fold["file"] != ident or st.st_size < _fold["pos"]:
            _fold.update(file=ident, pos=0, acc=_new_acc())
        if st.st_size > _fold["pos"]:
            with open(p, "rb") as f:
                f.seek(_fold["pos"])
                chunk = f.read(st.st_size - _fold["pos"])
            end = chunk.rfind(b"\n") + 1          # a writer may be mid-line; leave it for later
            for line in chunk[:end].splitlines():
                if line.strip():
                    try:
                        ev = json.loads(line)
                        ev["type"], ev["data"]       # a line that parses but isn't an event
                    except (ValueError, TypeError, KeyError):
                        _bad_line(line.decode(errors="replace"), f"byte {_fold['pos']}")
                        continue
                    try:
                        _apply(_fold["acc"], ev)
                    except (KeyError, TypeError, AttributeError) as e:
                        _bad_line(line.decode(errors="replace"), f"byte {_fold['pos']}: {e!r}")
            _fold["pos"] += end
        return _finish(_fold["acc"])

def library():
    return state()["media"]

def receipt(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

_sha_cache = {}

def bytes_sha(path):
    """receipt(path), remembered per (path, size, mtime): what's on disk right now, cheaply."""
    try:
        s = os.stat(path)
    except FileNotFoundError:
        return None
    key = (str(path), s.st_size, s.st_mtime_ns)
    if key not in _sha_cache:
        if len(_sha_cache) > 256:
            _sha_cache.clear()
        _sha_cache[key] = receipt(path)
    return _sha_cache[key]

def project_brand(st, slug):
    return (st["projects"].get(slug) or {}).get("brand") or "jefe"


# ---------- ready means checked; exceptions to the bar ---------------------------------------
# An export is "ready" only when a check.done exists for its exact bytes (sha256) and every
# failure it recorded is covered by an exception that is still granted. A re-render is new
# bytes, so it starts unchecked. Exceptions are events (exception.granted / .revoked): who,
# which rule, which clip (or the whole reel), and why in words. The agent may grant one when it
# can justify it; the operator tunes by revoking, and every revocation stays in the log.

# The rules an exception may bend, and the kind of case each is for. The rest (a cut inside a
# word, loudness, captions against the audio, a filler ending) are defects, never choices.
EXCEPTABLE = {
    "pauses": "a held silence the picture carries (a payoff, a reveal)",
    "shortest": "a short punch that lands (\"Perfect.\" in 0.8s)",
    "first word": "a silent cold open on action (<= 1.5s, natural sound)",
    "picture": "a held or dark frame that is meant",
    "length": "a reel outside 15-35s whose every beat earns it",
    "jump": "a jump cut that is meant (time skipped on purpose, in plain sight)",
    "held": "a frame held on purpose (a freeze on the payoff)",
    "cover short": "a cover piece that stops early, the face coming back where it works",
    "blip": "a shot under 1s that is meant (a flash cut the reel is built on, a deliberate punch)",
}

def rule_id(name):
    return re.sub(r"[-_\s]+", " ", str(name or "")).strip().lower()

# What an exception was granted on: the clip as it was (its media, in/out and every effect; not
# its label), or the whole story for one without a clip. If that changes, the exception lapses
# and must be granted again: it never waives a failure nobody looked at.
_FP_SKIP = ("id", "label", "moment")

def clip_fp(c):
    return hashlib.sha256(json.dumps({k: v for k, v in c.items() if k not in _FP_SKIP},
                                     sort_keys=True).encode()).hexdigest()[:16]

def reel_fp(tl):
    return hashlib.sha256(json.dumps([tl.get("aspect"), bool(tl.get("captions", True)), tl.get("voice"),
                                      [clip_fp(c) for c in tl.get("clips", [])]]).encode()).hexdigest()[:16]

def exception_scope(x):
    """'on-screen text', 'designed card' or None: what an exception declares about the picture."""
    return {"onscreen": "on-screen text", "card": "designed card"}.get(x.get("kind"))

def covering(f, exceptions, lapsed=None):
    """The granted (not revoked) exception that covers failure f {rule, clip, fp, reel}, or None.
    One with a clip covers that clip as it was when granted; one without covers the rule across
    the whole reel as it was. Exceptions that match but were granted on a different state are
    added to `lapsed` (a list) if given."""
    for x in (exceptions or {}).values():
        if x.get("revoked") or f.get("rule") not in EXCEPTABLE or x.get("rule") != f["rule"]:
            continue
        if x.get("clip") and x["clip"] != f.get("clip"):
            continue
        if x.get("fp") == (f.get("fp") if x.get("clip") else f.get("reel")):
            return x
        if lapsed is not None:
            lapsed.append(x)
    return None

def export_verdict(pj, ex, sha=None):
    """Is this export ready? {state: ready|failed|unchecked, ...}. sha: the bytes on disk now
    (bytes_sha); default the render's own receipt. Only a check of these very bytes counts."""
    sha = sha or ex.get("sha256")
    if sha and ex.get("sha256") and sha != ex["sha256"]:
        return {"state": "unchecked", "why": "the file changed since it was rendered"}
    chk = (pj.get("checks") or {}).get(sha) if sha else None
    if not chk:
        return {"state": "unchecked", "why": "not checked (studio check)"}
    open_, excepted = [], []
    for f in chk.get("failures") or []:
        x = covering(f, pj.get("exceptions"))
        if x:
            excepted.append({**f, "exception": x["id"], "by": x.get("actor"), "why": x.get("why"),
                             **({"declares": exception_scope(x)} if exception_scope(x) else {}),
                             **({"text": x["text"]} if x.get("text") else {})})
        else:
            open_.append(f)
    return {"state": "failed" if open_ else "ready", "checked": chk["ts"], "by": chk.get("actor"),
            "failed": sorted({f["rule"] for f in open_}), "open": open_, "excepted": excepted}

def grant_exception(slug, rule, clip=None, why="", actor="cli", onscreen=None, card=False):
    """onscreen TEXT: the clip shows this text (a drawn or overlaid hook), so the first-word rule
    is met by the picture; card: the clip is a designed card, so a still or dark picture is meant.
    Both are declarations nothing measures, so they are exceptions like any other: who, why,
    revocable, listed by check."""
    st = state()
    pj = st["projects"].get(slug)
    if not pj:
        raise RuntimeError(f"no project {slug}")
    rule = rule_id(rule)
    if rule not in EXCEPTABLE:
        raise RuntimeError(f"rule {rule!r} can't be excepted; these can: " +
                           "; ".join(f"{k} ({v})" for k, v in EXCEPTABLE.items()))
    if len(why.strip()) < 8:
        raise RuntimeError("say why in words (--why): what the picture or the line does that the rule misses")
    kind = "onscreen" if onscreen is not None else "card" if card else None
    if kind and not clip:
        raise RuntimeError(f"--{kind} declares something about one clip: name the CLIP")
    if kind == "onscreen" and (rule != "first word" or not onscreen.strip()):
        raise RuntimeError('--onscreen TEXT goes with the "first word" rule: the text that is the hook')
    if kind == "card" and rule != "picture":
        raise RuntimeError("--card goes with the picture rule: a still or dark card that is meant")
    tl = normalize_timeline(pj["timeline"], st["media"])
    c = next((c for c in tl["clips"] if c["id"] == clip), None) if clip else None
    if clip and not c:
        raise RuntimeError(f"no clip {clip} in {slug}")
    ev = emit("exception.granted", None, id=clip_id(), project=slug, brand=pj.get("brand", "jefe"),
              rule=rule, clip=clip, fp=clip_fp(c) if c else reel_fp(tl),
              **({"label": c["label"]} if c and c.get("label") else {}),
              **({"kind": kind} if kind else {}), **({"text": onscreen.strip()} if kind == "onscreen" else {}),
              why=why.strip(), actor=actor)
    return ev["data"]

def exception_state(x, tl):
    """granted | lapsed (what it was granted on changed) | revoked | invalid (a rule that can't
    bend: a hand-written event, ignored)."""
    if x.get("rule") not in EXCEPTABLE:
        return "invalid"
    if x.get("revoked"):
        return "revoked"
    c = next((c for c in tl["clips"] if c["id"] == x["clip"]), None) if x.get("clip") else None
    now = (clip_fp(c) if c else None) if x.get("clip") else reel_fp(tl)
    return "granted" if now and now == x.get("fp") else "lapsed"

def revoke_exception(slug, xid, why="", actor="cli"):
    st = state()
    exs = (st["projects"].get(slug) or {}).get("exceptions") or {}
    hits = [k for k in exs if k.startswith(xid)] if len(xid) >= 4 else []
    if len(hits) != 1:
        raise RuntimeError(f"no exception {xid} in {slug}" if not hits else f"exception id {xid} is ambiguous")
    x = exs[hits[0]]
    if x.get("revoked"):
        raise RuntimeError(f"exception {x['id']} was already revoked")
    # self-contained, so the revocations read as tuning data on their own
    emit("exception.revoked", None, id=x["id"], project=slug, brand=x.get("brand"), rule=x["rule"],
         clip=x.get("clip"), granted_by=x.get("actor"), granted_why=x.get("why"),
         granted_ts=x.get("ts"), why=(why or "").strip(), actor=actor)
    return x


# ---------- choice: every pick is a labelled preference ---------------------------------------
# A `choice` event says what was shown (option ids, in the order shown), what was chosen (or
# none), whether the labels were hidden, and where each option came from (provider, model,
# prompt version; for a version, its bytes and the story it was rendered from). It's the
# training set later; it's cheap now and impossible to reconstruct after the fact.

ACTIONS = ("pick", "keep", "rewrite", "reject")

def prompt_version(template):
    return hashlib.sha256(template.encode()).hexdigest()[:12]

def option_provenance(st, slug, opt):
    """(kind, provenance) for one option id, where it's known: an export of the project (sha256
    prefix, file name or path), a moment MEDIA@N, a post draft MEDIA@N:field. Else (None, {})."""
    pj = st["projects"].get(slug) or {}
    o = str(opt)
    for ex in reversed(pj.get("exports") or []):
        if (len(o) >= 6 and ex.get("sha256", "").startswith(o)) or o in (ex["file"], Path(ex["file"]).name):
            tl = ex.get("timeline") or {}
            return "version", {"id": ex["sha256"], "file": ex["file"], "rendered": ex["ts"],
                               "by": ex.get("actor"), "duration": ex.get("duration"),
                               "clips": [c.get("id") for c in tl.get("clips", [])],
                               "timeline_sha": hashlib.sha256(json.dumps(tl, sort_keys=True).encode()).hexdigest()[:12],
                               "verdict": ex["verdict"]["state"]}
    m = re.fullmatch(r"([0-9a-f]{4,64})@(\d+)(?::(\w+))?", o)
    if m:
        hits = [x for x in st["media"] if x.startswith(m.group(1))]
        if len(hits) == 1:
            it, step = st["media"][hits[0]], "draft" if m.group(3) else "moments"
            s = it["steps"].get(step) or {}
            return ("draft" if m.group(3) else "moment"), {
                "id": f"{hits[0]}@{m.group(2)}" + (f":{m.group(3)}" if m.group(3) else ""),
                "media": hits[0], "n": int(m.group(2)), **({"field": m.group(3)} if m.group(3) else {}),
                "step_sha256": s.get("sha256"), **(s.get("provenance") or {})}
    return None, {}

def record_choice(slug, shown, chosen, kind=None, action="pick", blind=False, labels=None,
                  round_=None, note=None, text=None, actor="cli", options=None):
    """Log one choice. shown: option ids in the order shown; chosen: one of them, or None (none
    of these). Provenance is filled in for every option the studio knows; `options` adds what a
    caller knows itself (e.g. the UI's view of a draft). Returns the event's data."""
    st = state()
    if slug not in st["projects"]:
        raise RuntimeError(f"no project {slug}")
    if action not in ACTIONS:
        raise RuntimeError(f"action is one of {', '.join(ACTIONS)}")
    if not shown:
        raise RuntimeError("a choice needs the options that were shown")
    ids, prov, kinds = [], {}, set()
    for o in shown:
        k, p = option_provenance(st, slug, o)
        oid = p.get("id", str(o))
        ids.append(oid)
        caller = (options or {}).get(str(o))    # what the caller saw (e.g. a draft's text);
        prov[oid] = {**(caller if isinstance(caller, dict) else {}), **p}   # the studio's facts win
        prov[oid].pop("id", None)
        kinds.add(k)
    if len(set(ids)) != len(ids):
        raise RuntimeError("an option is shown twice")
    pick = None
    if chosen is not None:
        k, p = option_provenance(st, slug, chosen)
        pick = p.get("id", str(chosen))
        if pick not in ids:
            raise RuntimeError(f"{chosen} is not one of the options shown")
    kind = kind or (kinds.pop() if len(kinds) == 1 and None not in kinds else "other")
    ev = emit("choice", None, id=clip_id(), project=slug, brand=project_brand(st, slug), kind=kind,
              action=action, shown=ids, chosen=pick, position=ids.index(pick) if pick else None,
              blind=bool(blind), **({"labels": labels} if labels else {}),
              **({"round": round_} if round_ else {}), **({"note": note} if note else {}),
              **({"text": text} if text is not None else {}),
              options={k: v for k, v in prov.items() if v}, actor=actor)
    return ev["data"]

BLIND_LABELS = "ABCDEF"

def blind_round(slug, exports=None, actor="cli", rng=None):
    """A blind A/B of versions: the exports (default: the two latest) are cloned to
    projects/P/blind/ROUND/A.mp4, B.mp4, … in a random order, and blind.round logs which is
    which. `studio choose P A --round ROUND` turns the pick back into the real version."""
    import random
    st = state()
    pj = st["projects"].get(slug)
    if not pj:
        raise RuntimeError(f"no project {slug}")
    exs = pj.get("exports") or []
    if exports:
        picked = []
        for x in exports:
            k, p = option_provenance(st, slug, x)
            if k != "version":
                raise RuntimeError(f"{x} is not an export of {slug} (sha256 prefix or file name)")
            picked.append(next(e for e in exs if e["sha256"] == p["id"]))
    else:
        picked = list(reversed(exs))[:2]
    picked = list({e["sha256"]: e for e in picked}.values())
    if not 2 <= len(picked) <= len(BLIND_LABELS):
        raise RuntimeError(f"a blind round needs 2-{len(BLIND_LABELS)} different versions "
                           f"({slug} has {len(exs)} export{'s' if len(exs) != 1 else ''})")
    (rng or random.SystemRandom()).shuffle(picked)
    rid = clip_id()
    d = pdir(slug) / "blind" / rid
    d.mkdir(parents=True)
    labels, now = {}, time.time()
    for lab, e in zip(BLIND_LABELS, picked):
        # a real copy (a clone keeps the export's dates, which say which is newer), then every
        # copy gets the same dates; they're made in label order, so creation order says nothing
        out = d / f"{lab}{Path(e['file']).suffix}"
        shutil.copyfile(HOME / e["file"], out)
        os.utime(out, (now, now))
        labels[lab] = e["sha256"]
    emit("blind.round", None, round=rid, project=slug, brand=pj.get("brand", "jefe"), kind="version",
         labels=labels, dir=str(d.relative_to(HOME)), actor=actor)
    return rid, d, labels

def choose_blind(slug, rid, label, note=None, actor="cli"):
    st = state()
    rnd = ((st["projects"].get(slug) or {}).get("rounds") or {}).get(rid)
    if not rnd:
        raise RuntimeError(f"no blind round {rid} in {slug} (studio ab {slug})")
    labels = rnd["labels"]
    if label is not None and label.upper() not in labels:
        raise RuntimeError(f"round {rid} shows {', '.join(labels)}")
    order = sorted(labels)                       # shown as A, B, ...: the shuffle is in the mapping
    return record_choice(slug, [labels[x] for x in order],
                         labels[label.upper()] if label is not None else None, kind="version",
                         blind=True, labels=labels, round_=rid, note=note, actor=actor)


# ---------- helpers ---------------------------------------------------------

def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({r.returncode}):\n{r.stderr[-2000:]}")
    return r.stdout

def need(tool):
    if not shutil.which(tool):
        raise RuntimeError(f"`{tool}` not on PATH — run inside `nix develop {HERE}`")

def mdir(mid):
    return HOME / "media" / mid

def pdir(slug):
    return HOME / "projects" / slug

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "project"

def probe(path):
    out = json.loads(sh(["ffprobe", "-v", "error", "-print_format", "json",
                         "-show_format", "-show_streams", str(path)]))
    v = next((s for s in out["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in out["streams"] if s["codec_type"] == "audio"), None)
    meta = {"duration": float(out["format"].get("duration", 0)),
            "has_audio": a is not None, "has_video": v is not None}
    if v:
        w, h = int(v["width"]), int(v["height"])
        rot = 0
        for sd in v.get("side_data_list", []):
            rot = int(sd.get("rotation", rot) or rot)
        rot = rot or int(v.get("tags", {}).get("rotate", 0) or 0)
        if abs(rot) % 180 == 90:
            w, h = h, w
        meta.update(width=w, height=h)
    return meta

def fmt_t(t):
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{t % 60:04.1f}"

# ---------- the agent ---------------------------------------------------------
# moments and draft hand one prompt to a coding agent's non-interactive mode, run in the
# media's folder (so it can open keyframes/ and transcript.txt), and parse the JSON block out
# of its reply. Any agent will do:
#   STUDIO_AGENT_CMD      any command: the prompt on stdin, the reply on stdout (the model,
#                         if one is set, in $STUDIO_AGENT_MODEL). Wins over STUDIO_AGENT.
#   STUDIO_AGENT          claude | codex | goose (built in, see agent_cmd); unset = the first
#                         of those on PATH
#   STUDIO_AGENT_MODEL    model for every call; unset = the agent's own default
#   STUDIO_MOMENTS_MODEL  model for picking moments; unset = MOMENTS_MODEL below for that
#                         agent, "" = no override (STUDIO_AGENT_MODEL or the agent's default)
# Drafts are rewritten by the operator anyway and stay on the default model.

AGENTS = ("claude", "codex", "goose")
# With Claude Code, moment picking runs on Opus (operator's call, 2026-11-08).
MOMENTS_MODEL = {"claude": "claude-opus-5-5"}

def agent_name():
    """Which agent the AI steps call: "custom" (STUDIO_AGENT_CMD) or a built-in."""
    if os.environ.get("STUDIO_AGENT_CMD", "").strip():
        return "custom"
    a = os.environ.get("STUDIO_AGENT", "").strip().lower()
    if a and a not in AGENTS:
        raise RuntimeError(f"STUDIO_AGENT={a}: the built-in agents are {', '.join(AGENTS)}; "
                           "for any other, set STUDIO_AGENT_CMD to a command that reads the "
                           "prompt on stdin and prints the reply")
    return a or next((x for x in AGENTS if shutil.which(x)), AGENTS[0])

def agent_model(name, model=None):
    """model, else STUDIO_AGENT_MODEL (STUDIO_CLAUDE_MODEL, its old name, for claude), else
    None: the agent's own default."""
    return model or os.environ.get("STUDIO_AGENT_MODEL") or \
        (os.environ.get("STUDIO_CLAUDE_MODEL") if name == "claude" else None) or None

def moments_model(name):
    return os.environ.get("STUDIO_MOMENTS_MODEL", MOMENTS_MODEL.get(name))

def agent_cmd(name, model=None, allow_read=False, out=None):
    """The command line for one call. The prompt always goes on stdin (a transcript can be
    longer than an argument may be). out: where codex writes its last message."""
    if name == "custom":
        return shlex.split(os.environ["STUDIO_AGENT_CMD"])
    if name == "claude":   # Claude Code: JSON {"result", "is_error"} on stdout
        return ["claude", "-p", "--output-format", "json"] + (["--model", model] if model else []) \
            + (["--allowedTools", "Read"] if allow_read else [])
    if name == "codex":    # Codex: `exec` is the non-interactive entry; "-" = prompt on stdin
        return ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                "--color", "never"] + (["-m", model] if model else []) + ["-o", str(out), "-"]
    if name == "goose":    # Goose: the session's messages as JSON on stdout
        return ["goose", "run", "--no-session", "--quiet", "--output-format", "json"] \
            + (["--model", model] if model else []) + ["-i", "-"]
    raise RuntimeError(f"no agent {name}")

def agent_info():
    """For `studio agent` and the UI's health check: which agent, where it is, which models."""
    try:
        name = agent_name()
    except RuntimeError as e:
        return {"name": None, "path": None, "error": str(e)}
    exe = (agent_cmd(name) or [""])[0] if name == "custom" else name
    return {"name": name, "path": shutil.which(exe) if exe else None, "command": exe,
            "model": agent_model(name), "moments_model": agent_model(name, moments_model(name))}

def agent(prompt, cwd, allow_read=False, model=None):
    """One prompt to the agent; its reply, as text."""
    name = agent_name()
    model = agent_model(name, model)
    with tempfile.TemporaryDirectory(prefix="studio-agent-") as t:
        out = Path(t) / "reply.txt"
        cmd = agent_cmd(name, model, allow_read, out)
        if not cmd or not shutil.which(cmd[0]):
            raise RuntimeError(f"`{cmd[0] if cmd else ''}` not on PATH: install it, or point "
                               "STUDIO_AGENT / STUDIO_AGENT_CMD at an agent you have (`studio agent`)")
        env = {**os.environ, **({"STUDIO_AGENT_MODEL": model} if model else {})}
        text = sh(cmd, cwd=cwd, input=prompt, env=env)
        if name == "claude":
            res = json.loads(text)
            if res.get("is_error"):
                raise RuntimeError(f"claude: {res.get('result')}")
            return res["result"]
        if name == "codex":
            text = out.read_text() if out.exists() else ""
        elif name == "goose":
            said = [m for m in json.loads(text).get("messages", []) if m.get("role") == "assistant"]
            text = "".join(c.get("text", "") for c in (said[-1]["content"] if said else [])
                           if c.get("type") == "text")
        if not text.strip():
            raise RuntimeError(f"{name}: no reply")
        return text

def step_provenance(task, template, model, item):
    """Where an AI step's output came from, for the choice log: the agent, the model (None =
    the agent's own default), the prompt template's version, and the bar and brand it read."""
    name = agent_name()
    q = HERE / "QUALITY.md"
    return {"provider": name, "model": agent_model(name, model), "task": task,
            "prompt": prompt_version(template),
            "quality": prompt_version(q.read_text()) if q.exists() else None,
            "brand": item.get("brand") or "jefe", "brand_file": prompt_version(brand_text(item))}

def parse_json_block(text):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = m.group(1) if m else text
    start = min([i for i in (body.find("["), body.find("{")) if i >= 0], default=0)
    return json.loads(body[start:])

def brand_text(item):
    name = item.get("brand") or "jefe"
    p = HERE / "brands" / f"{name}.md"
    return p.read_text() if p.exists() else f"(no brand file for {name})"

def quality_text():
    p = HERE / "QUALITY.md"
    return p.read_text().strip() if p.exists() else "(no QUALITY.md)"

def brand_words(name):
    """(vocabulary, fixes) from brands/<name>.md. `## Vocabulary`: terms, comma-separated
    or one per line. `## Fix words`: one `- heard → meant` per line (-> also works).
    Whisper gets the vocabulary as its prompt; the fixes are applied to what it wrote."""
    p = HERE / "brands" / f"{name or 'jefe'}.md"
    vocab, fixes, sec = [], [], None
    if not p.exists():
        return vocab, fixes
    text = re.sub(r"<!--.*?-->", "", p.read_text(), flags=re.S)     # comments are for humans
    for line in text.splitlines():
        if line.startswith("#"):
            sec = line.lstrip("#").strip().lower()
            continue
        txt = re.sub(r"^\s*[-*+]\s*", "", line).strip()
        if not txt or txt.startswith(">"):
            continue
        if sec == "vocabulary":
            vocab += [x.strip().strip('"') for x in txt.split(",") if x.strip().strip('"')]
        elif sec == "fix words":
            m = re.match(r'"?(.+?)"?\s*(?:→|->|=>)\s*"?(.+?)"?\s*$', txt)
            if m and m.group(1).strip() and m.group(2).strip():
                fixes.append((m.group(1).strip(), m.group(2).strip()))
    return vocab, fixes

def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default

_json_cache, _json_lock = {}, threading.Lock()

def load_json_cached(path, default):
    """load_json, but re-read only when the file's mtime/size change. Don't mutate the result."""
    try:
        s = os.stat(path)
    except FileNotFoundError:
        return default
    key = (s.st_ino, s.st_mtime_ns, s.st_size)
    with _json_lock:
        hit = _json_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        val = json.loads(Path(path).read_text())
    except ValueError:
        return default
    with _json_lock:
        _json_cache[str(path)] = (key, val)
    return val


# ---------- hardware --------------------------------------------------------
# Apple silicon: VideoToolbox for decode/scale/encode (the media engines, not the CPU), Metal
# for whisper. Detected once per process; STUDIO_HW=0 forces the portable software path.

def _sysctl(name, default):
    try:
        return int(subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                                  timeout=2).stdout.strip())
    except Exception:
        return default

CPUS = os.cpu_count() or 4
PCORES = _sysctl("hw.perflevel0.physicalcpu", CPUS) if sys.platform == "darwin" else CPUS
WHISPER_THREADS = int(os.environ.get("STUDIO_WHISPER_THREADS", min(8, PCORES)))
WHISPER_BEAM = int(os.environ.get("STUDIO_WHISPER_BEAM", 1))   # 1 = greedy (+temperature fallback)
RENDER_JOBS = int(os.environ.get("STUDIO_RENDER_JOBS", 0)) or None   # None = pick from encoder
PROXY_CHUNKS = int(os.environ.get("STUDIO_PROXY_CHUNKS", 4))
# VideoToolbox quality scale (0-100). 55 at 720p ~ x264 crf 26; 65 at 1080x1920 ~ crf 20.
PROXY_Q = os.environ.get("STUDIO_PROXY_Q", "55")
EXPORT_Q = os.environ.get("STUDIO_EXPORT_Q", "65")

_hw, _hw_lock = {}, threading.Lock()

def hw():
    """{'enc': bool, 'dec': bool} — can this ffmpeg use VideoToolbox here? Probed once."""
    with _hw_lock:
        if _hw:
            return _hw
        res = {"enc": False, "dec": False}
        if os.environ.get("STUDIO_HW", "1") != "0" and shutil.which("ffmpeg"):
            try:
                res["enc"] = subprocess.run(
                    ["ffmpeg", "-v", "error", "-hide_banner", "-f", "lavfi", "-i",
                     "testsrc2=size=256x256:rate=30:duration=0.2", "-c:v", "h264_videotoolbox",
                     "-q:v", PROXY_Q, "-allow_sw", "1", "-f", "null", "-"],
                    capture_output=True, timeout=20).returncode == 0
            except Exception:
                pass
        # decode is tried per file (VideoToolbox can't do every codec); a failure retries in
        # software
        res["dec"] = res["enc"] and os.environ.get("STUDIO_HWDEC", "1") != "0"
        _hw.update(res)
        return _hw

# Tag every output SDR BT.709 explicitly: the colour description lives in the SPS, so parts
# decoded by different paths (or from differently tagged sources) still concat losslessly.
BT709 = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-color_range", "tv"]

# ...and on the frames themselves: VideoToolbox writes the frame's colour tags, not the
# codec context's, so untagged software frames would otherwise produce a different SPS.
SETPARAMS = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"

def venc(kind, gop):
    """Video encoder args. Every segment of one render uses the identical set, so the parts
    share SPS/PPS and concat without re-encoding."""
    if hw()["enc"]:
        q = PROXY_Q if kind == "proxy" else EXPORT_Q
        return ["-c:v", "h264_videotoolbox", "-q:v", q, "-allow_sw", "1", "-profile:v", "high",
                "-level:v", "4.2" if kind == "export" else "4.1", "-g", str(gop), *BT709]
    crf = "26" if kind == "proxy" else "20"
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-g", str(gop),
            "-pix_fmt", "yuv420p", "-threads", str(max(2, CPUS // 2)), *BT709]

def render_pool_size():
    if RENDER_JOBS:
        return RENDER_JOBS
    # measured on M4 Max, 7-segment 93s export: 1 -> 16.4s, 2 -> 9.5s, 3 -> 8.2s, 4 -> 7.6s,
    # 6 -> 7.5s. The VideoToolbox encoder saturates around 4 concurrent sessions.
    return 4 if hw()["enc"] else 2

_vinfo = {}

def vinfo(path):
    """{'theta': display rotation 0/90/180/270 (ffmpeg autorotate sense), 'tenbit': bool,
    'codec': str, 'fps': float}."""
    path = str(path)
    if path not in _vinfo:
        info = {"theta": 0, "tenbit": False, "codec": None, "fps": 30.0, "wide": False}
        try:
            out = json.loads(sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-print_format",
                                 "json", "-show_streams", path]))
            v = out["streams"][0]
            r = 0.0
            for sd in v.get("side_data_list", []):
                r = float(sd.get("rotation", r) or r)
            r = r or float(v.get("tags", {}).get("rotate", 0) or 0)
            info["theta"] = int(-round(r) % 360)          # fftools: theta = -display_rotation
            info["tenbit"] = any(x in v.get("pix_fmt", "") for x in ("10", "12", "p016"))
            info["codec"] = v.get("codec_name")
            info["wide"] = (v.get("color_transfer") in ("arib-std-b67", "smpte2084")
                            or v.get("color_primaries") == "bt2020")
            num, _, den = (v.get("avg_frame_rate") or "30/1").partition("/")
            info["fps"] = float(num) / float(den or 1) if float(den or 1) else 30.0
        except Exception:
            pass
        _vinfo[path] = info
    return _vinfo[path]

def vt_transpose(theta):
    return {90: "transpose_vt=dir=clock", 270: "transpose_vt=dir=cclock",
            180: "transpose_vt=dir=reversal"}.get(theta)

def even(x):
    return max(2, int(round(x / 2)) * 2)


# ---------- progress --------------------------------------------------------

class Progress:
    """Throttled job.progress events (~2/s, only on visible change). Thread-safe; parts let
    parallel ffmpeg runs (chunks, segments) add up to one percentage."""
    def __init__(self, step, media=None, weights=None, job=None, **extra):
        self.step, self.media, self.extra = step, media, extra
        self.job = job if job is not None else getattr(_ctx, "job", None)
        self.weights = weights or {None: 1.0}
        self.done = {k: 0.0 for k in self.weights}
        self.last_t, self.last_pct, self.lock = 0.0, -1.0, threading.Lock()

    def part(self, key):
        return lambda frac: self.update(key, frac)

    def update(self, key, frac):
        with self.lock:
            self.done[key] = max(self.done.get(key, 0.0), min(1.0, max(0.0, frac)))
            tot = sum(self.weights.values()) or 1.0
            pct = round(100 * sum(self.done[k] * w for k, w in self.weights.items()) / tot, 1)
            now = time.monotonic()
            if pct >= 100 or now - self.last_t < 0.5 or pct - self.last_pct < 1:
                return
            self.last_t, self.last_pct = now, pct
        emit("job.progress", self.media, job=self.job, step=self.step, pct=pct, **self.extra)

def ff(cmd, dur=None, on_frac=None, cwd=None):
    """Run ffmpeg. With on_frac and a duration, stream -progress and report 0..1."""
    if not on_frac or not dur:
        return sh(cmd, cwd=cwd)
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    with tempfile.TemporaryFile() as err, \
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True, cwd=cwd) as p:
        for line in p.stdout:
            if line.startswith("out_time_us="):
                v = line[12:].strip()
                if v.isdigit():
                    on_frac(int(v) / 1e6 / dur)
        rc = p.wait()
        if rc != 0:
            err.seek(0)
            raise RuntimeError(f"{cmd[0]} failed ({rc}):\n{err.read().decode(errors='replace')[-2000:]}")

def spawn_ctx(fn):
    """Wrap fn so it runs with the caller's job context (for pool / thread workers)."""
    job = getattr(_ctx, "job", None)
    def run(*a, **kw):
        _ctx.job = job
        return fn(*a, **kw)
    return run


# ---------- projects --------------------------------------------------------

def empty_timeline():
    return {"aspect": "9:16", "captions": True, "clips": []}

def project_new(name, brand="jefe"):
    projects = state()["projects"]
    slug, n = slugify(name), 2
    base = slug
    while slug in projects:
        slug, n = f"{base}-{n}", n + 1
    pdir(slug).mkdir(parents=True, exist_ok=True)
    emit("project.created", None, slug=slug, name=name, brand=brand)
    return slug

def ensure_project(slug):
    p = state()["projects"].get(slug)
    if not p or p["created"] is None:
        pdir(slug).mkdir(parents=True, exist_ok=True)
        emit("project.created", None, slug=slug, name=slug, brand="jefe")
    return slug

def clip_id():
    return hashlib.sha1(os.urandom(8)).hexdigest()[:8]

def normalize_timeline(tl, lib, problems=None):
    """Clamp every clip to its media and drop anything that points nowhere. If `problems` is
    a list, a line is appended for every clip that was dropped ("dropped") or clamped."""
    out = {"aspect": tl.get("aspect", "9:16") if tl.get("aspect") in ASPECTS else "9:16",
           "captions": bool(tl.get("captions", True)), "clips": []}
    say = (lambda kind, msg: problems.append((kind, msg))) if problems is not None else (lambda *_: None)
    for i, c in enumerate(tl.get("clips", [])):
        it = lib.get(c.get("media"))
        tag = f"clip {i + 1} {c.get('id') or ''}".rstrip()
        if not it:
            say("dropped", f"dropped {tag}: no media {c.get('media')}")
            continue
        if not it.get("has_video"):
            say("dropped", f"dropped {tag}: {it.get('name', c['media'])} is audio-only; it can't go on the Story")
            continue
        try:
            ra, rb = float(c.get("in", 0)), float(c.get("out", it["duration"]))
        except (TypeError, ValueError):
            say("dropped", f"dropped {tag}: in/out are not numbers")
            continue
        a = max(0.0, ra)
        b = min(it["duration"], rb)
        if b - a < 0.1:
            say("dropped", f"dropped {tag}: zero length ({ra:g}-{rb:g}s of a {it['duration']:.1f}s file)")
            continue
        if abs(a - ra) > 1e-3 or abs(b - rb) > 1e-3:
            say("clamped", f"warning: {tag} clamped to {a:.2f}-{b:.2f}s (media is {it['duration']:.2f}s long)")
        extra = {}
        if c.get("label"):
            extra["label"] = c["label"]
        try:      # which moment the clip was added from (Copy post, "In story")
            if c.get("moment") is not None and int(c["moment"]) > 0:
                extra["moment"] = int(c["moment"])
        except (TypeError, ValueError):
            pass
        if c.get("fit") in FITS:       # absent = the default for that footage (fit_default)
            extra["fit"] = c["fit"]
        if c.get("voice") in VOICES:   # absent = the story's setting (else natural if spoken)
            extra["voice"] = c["voice"]
        try:                           # absent = 1 (as said)
            pace = min(PACE_RANGE[1], max(PACE_RANGE[0], float(c.get("pace", 1))))
        except (TypeError, ValueError):
            pace = 1.0
        if pace != 1.0:
            extra["pace"] = round(pace, 3)
        cover = normalize_cover(c.get("cover"), lib, tag, say,
                                {"in": round(a, 3), "out": round(b, 3), "pace": pace})
        if cover:
            extra["cover"] = cover
        out["clips"].append({"id": c.get("id") or clip_id(), "media": c["media"],
                             "in": round(a, 3), "out": round(b, 3), **extra})
    if tl.get("voice") in VOICES and tl["voice"] != "off":
        out["voice"] = tl["voice"]
    return out

def normalize_cover(pieces, lib, tag, say, clip):
    """A clip's cover pieces ({media, in, out, at, fit?, to_end?}: see "cover" below), laid in
    stored order (a later piece cuts what it lands on, so pieces never overlap) and sorted by
    `at`. A piece that points nowhere, is audio-only, isn't finite numbers, is laid over fewer
    than COVER_FRAMES frames (cover_frames: the same count `timeline cover` refuses on) or
    starts past the end of its footage is dropped with a warning (the clip keeps its own
    picture there). A piece laid outside the clip's in-out is kept: it shows nothing until a
    trim brings its words back. Nothing is clamped: cover_plan says what shows, and `check`
    fails `cover short`. Normalizing twice changes nothing. Problems are kind "cover": they
    never block a save."""
    lay = {"in": clip["in"], "out": clip["out"], "pace": clip.get("pace", 1), "cover": []}
    for k, p in enumerate(pieces if isinstance(pieces, list) else []):
        p = p if isinstance(p, dict) else {}
        it = lib.get(p.get("media"))
        what = f"{tag} cover {k + 1}"
        if not it or not it.get("has_video"):
            say("cover", f"warning: {what} dropped: no footage {p.get('media')}")
            continue
        try:
            pin, pout, at = float(p.get("in", 0)), float(p.get("out", 0)), float(p.get("at", 0))
        except (TypeError, ValueError):
            say("cover", f"warning: {what} dropped: in/out/at are not numbers")
            continue
        if not all(map(math.isfinite, (pin, pout, at))):
            say("cover", f"warning: {what} dropped: in/out/at are not finite numbers")
            continue
        if pin < 0:                    # the part before the footage starts never shows
            say("cover", f"warning: {what}: starts {-pin:.2f}s before its footage does; it starts there")
            b = at + pout - pin
            at, pin = at - pin * clip_pace(clip), 0.0
            pout = b - at
        piece = {"media": p["media"], "in": cover_r3(pin), "out": cover_r3(pout), "at": cover_r3(at)}
        if p.get("fit") in FITS:
            piece["fit"] = p["fit"]
        if p.get("to_end") is True:
            piece["to_end"] = True
        else:
            piece["out"] = cover_r3(max(pout, pin))
        bad = cover_bad(piece, clip, it)
        if bad:
            say("cover", f"warning: {what} dropped: {bad}")
            continue
        lay["cover"] = cover_add(lay, piece)
    out = []                           # what laying one on another left must hold too
    for p in lay["cover"]:
        bad = cover_bad(p, clip, lib.get(p["media"]))
        if bad:
            say("cover", f"warning: {tag}: a remainder of a cover piece dropped: {bad}")
        else:
            out.append(p)
    return sorted(out, key=lambda p: p["at"])

def cover_frames(p, clip):
    """How many export frames piece p is laid over, counted as cover_plan counts them but
    wherever the clip's ends are (a `to_end` piece: to the clip's last frame)."""
    a, pace = clip["in"], clip_pace(clip)
    f0 = round((p["at"] - a) * 30 / pace)
    fe = clip_frames(clip) if p.get("to_end") else round((p["at"] - a + p["out"] - p["in"]) * 30 / pace)
    return fe - f0

def cover_bad(p, clip, it):
    """Why a stored piece can't be kept (None = it can): the one rule `timeline cover` and every
    save use, so a piece laid is never dropped at its save."""
    if not p.get("to_end") and cover_frames(p, clip) < COVER_FRAMES:
        return f"it is laid over {max(0, cover_frames(p, clip))} frames (under {COVER_FRAMES})"
    if p["in"] > float(it["duration"]) - COVER_MIN:
        return f"it starts at {p['in']:.2f}s of a {float(it['duration']):.2f}s file"
    return None

FITS = ("fill", "fit")

def fit_default(item, aspect):
    """How a clip fills the frame when nobody chose: 'fill' (crop to cover; camera footage) or
    'fit' (the whole frame over a blurred copy of itself). A silent portrait source whose
    shape is far from the target (a screen recording) gets 'fit', so its top bar and edges
    aren't cropped away. ui/formats.js fitDefault() is the same rule."""
    w, h = item.get("width") or 0, item.get("height") or 0
    if not item.get("has_video") or not w or not h or aspect not in ASPECTS:
        return "fill"
    tw, th = ASPECTS[aspect]
    far = abs(math.log((w / h) / (tw / th))) > math.log(1.15)
    return "fit" if (not item.get("has_audio") and h > w and far) else "fill"

def clip_fit(clip, item, aspect):
    return clip.get("fit") if clip.get("fit") in FITS else fit_default(item, aspect)

# ---------- cover: b-roll over a clip's sound ------------------------------------------
# A clip's sound is always its own media's in-out. Its picture can be covered by pieces of
# other footage: cover = [{media, in, out, at, fit?, to_end?}] (the stored shape is the one
# the log has always had). A piece is laid over the clip's own SOURCE seconds, its words:
# from `at` for `out - in` source seconds (to the clip's out point instead when `to_end`).
# Its footage plays at normal speed from `in`, so source second x shows footage
# in + (x - at) / pace. Because the anchor is the words, a trim, snap or pace never moves a
# piece off them; a split or a cut gives each side the part laid over its own words.
#
# The CLI and `timeline show` speak REEL time (seconds into the clip as it plays, after its
# pace) and convert at the boundary. What a story shows is computed once, in whole export
# frames, by cover_plan: render, the cut list, `check`, `timeline show` and the edit warnings
# all read it, so what check measures is what renders. A piece stops where its footage runs
# out (never a cloned frame), and `check` fails `cover short` when that is before the end it
# was laid to. A piece under COVER_FRAMES is never drawn, nor a sliver of the clip's own picture
# under 0.1 s of its source between pieces (it goes to a neighbour whose footage lasts).

COVER_FRAMES = 3     # the shortest stretch of picture a plan draws: 3 frames = 0.1 s
COVER_MIN = COVER_FRAMES / 30

def cover_r3(x):
    """3 decimals, rounding half up: the same in Python and in ui/ops.js (round() is half-even)."""
    return math.floor(x * 1000 + 0.5) / 1000

def reel_frame(r):
    """The export frame (of the clip, from 0) at reel second r, as frames_of rounds."""
    return math.floor(r * 30 + 0.5 + 1e-6)

def cover_to(p, clip):
    """The source second piece p is laid to: the clip's out point for a `to_end` piece."""
    return clip["out"] if p.get("to_end") else p["at"] + p["out"] - p["in"]

def cover_footage(p, clip, x):
    """The second of p's footage at source second x of the clip."""
    return p["in"] + (x - p["at"]) / clip_pace(clip)

def cover_plan(clip, lib):
    """What the clip shows, frame by frame: (spans, pieces). spans = [(f0, f1, piece or None,
    t0)], end to end over the clip's clip_frames(clip) frames: None = the clip's own picture
    from source second t0; a piece = its footage from second t0, one footage frame per export
    frame. pieces = one dict per stored piece (in stored order): {n, piece, laid: (f0, fe) the
    frames it was laid over inside the clip, shown: (f0, f1) or None, short: None | 'footage' |
    'nothing'}. A piece is cut where its footage runs out (whole frames from its start, so
    never a cloned frame); a sliver of the clip's own picture under COVER_FRAMES between or
    after pieces goes to the piece before it while its footage lasts, else to the piece after
    it if it has footage before what it showed (never before its IN), else stays; one before the
    first piece always goes to it (b-roll on the clip's first frame, its footage from IN at the
    earliest, a frame or two sooner than laid)."""
    n, pace, a = clip_frames(clip), clip_pace(clip), clip["in"]
    # the frame a source offset lands on, rounded exactly as renders always have (so a stored
    # story draws the frames it always drew)
    fr = lambda t: min(n, max(0, round(t * 30 / pace)))
    info, live = [], []
    for k, p in enumerate(clip.get("cover") or [], 1):
        f0 = fr(p["at"] - a)
        fe = n if p.get("to_end") else fr(p["at"] - a + p["out"] - p["in"])
        it = (lib or {}).get(p["media"]) or {}
        dur = float(it.get("duration") or 0)
        x = {"n": k, "piece": p, "laid": (f0, fe), "shown": None, "short": None, "dur": dur}
        info.append(x)
        if fe - f0 < COVER_FRAMES:          # laid over (almost) nothing of this clip
            continue
        x["t0"] = max(p["in"], cover_footage(p, clip, a + f0 * pace / 30))
        x["avail"] = math.floor((dur - x["t0"]) * 30 + 1e-6)   # whole footage frames from t0
        x["f0"], x["f1"] = f0, min(fe, f0 + max(0, x["avail"]))
        live.append(x)
    live.sort(key=lambda x: x["f0"])
    for prev, x in zip(live, live[1:]):     # never overlap: a later one starts where one ends
        x["f0"] = max(x["f0"], prev["f1"])
    pcs = [x for x in live if x["f1"] - x["f0"] >= COVER_FRAMES]
    def back(y, f):                         # y starts at frame f, on footage before what it showed:
        t0 = y["t0"] - (y["f0"] - f) / 30   # only if that footage is past its IN (every later
        if t0 >= y["piece"]["in"] - 1e-9:   # frame keeps its footage)
            y["f0"], y["t0"], y["avail"] = f, t0, y["avail"] + (y["f0"] - f)
    # a sliver is under 0.1 s of the clip's source: on the reel, under 3 / pace frames (the
    # last one measured to the clip's true end, not its rounded last frame), as renders always had
    sliver = lambda g: 0 < g < COVER_FRAMES / pace - 1e-9
    for i, x in enumerate(pcs):             # slivers of own picture: to a neighbouring piece
        last = i + 1 == len(pcs)
        gap_end = n if last else pcs[i + 1]["f0"]
        g = (clip_len(clip) * 30 if last else gap_end) - x["f1"]
        if sliver(g) and gap_end - x["f0"] <= x["avail"]:
            x["f1"] = gap_end
        elif sliver(g) and not last:
            back(pcs[i + 1], x["f1"])
    if pcs and sliver(pcs[0]["f0"]):        # before the first one: the b-roll starts on the clip's
        y = pcs[0]                          # first frame (as renders always did), from footage
        t0 = max(y["piece"]["in"], y["t0"] - y["f0"] / 30)   # before what it showed, never before IN
        y["avail"] = math.floor((y["dur"] - t0) * 30 + 1e-6)
        y["f0"], y["t0"], y["f1"] = 0, t0, min(y["f1"], y["avail"])
    spans, t = [], 0
    for x in pcs:
        if x["f0"] > t:
            spans.append((t, x["f0"], None, a + t * pace / 30))
        spans.append((x["f0"], x["f1"], x["piece"], x["t0"]))
        t = x["f1"]
        x["shown"] = (x["f0"], x["f1"])
    if t < n or not spans:
        spans.append((t, n, None, a + t * pace / 30))
    for x in info:
        f0, fe = x["laid"]
        if fe - f0 < COVER_FRAMES:
            continue                       # nothing of it is in this clip: no promise to keep
        if not x["shown"]:
            x["short"] = "nothing"
        elif x["shown"][1] < fe:
            x["short"] = "footage" if x["f0"] + x["avail"] < fe else "clip"
    return spans, info

def cover_short_text(x, name):
    """Why a piece stops before the end it was laid to."""
    f0, fe = x["laid"]
    if x["short"] == "nothing":
        return f"shows nothing of the {f0 / 30:.2f}–{fe / 30:.2f}s it was laid over"
    s1 = x["shown"][1]
    if x["short"] == "footage":
        return (f"stops at {s1 / 30:.2f}s, laid to {fe / 30:.2f}s: its footage ends "
                f"({name} is {x['dur']:.2f}s long)")
    return f"stops at {s1 / 30:.2f}s, laid to {fe / 30:.2f}s: another piece takes over"

def cover_short(clip, lib):
    """[(n, piece, text)] for the clip's pieces that stop before the end they were laid to."""
    return [(x["n"], x["piece"], cover_short_text(x, ((lib or {}).get(x["piece"]["media"]) or {})
                                                   .get("name", x["piece"]["media"])))
            for x in cover_plan(clip, lib)[1] if x["short"]]

def cover_add(clip, piece):
    """clip's cover with piece laid on; what it lands on is cut away (source clock)."""
    pace = clip_pace(clip)
    end = lambda p: math.inf if p.get("to_end") else p["at"] + p["out"] - p["in"]
    a0, a1 = piece["at"], end(piece)
    out = []
    for p in clip.get("cover") or []:
        p0, p1 = p["at"], end(p)
        if p1 <= a0 + 1e-3 or p0 >= a1 - 1e-3:
            out.append(p)
            continue
        if p0 < a0 - 1e-3:
            q = {k: v for k, v in p.items() if k != "to_end"}
            q["out"] = cover_r3(p["in"] + a0 - p0)
            if cover_frames(q, clip) >= COVER_FRAMES:      # a sliver left over is the new piece's
                out.append(q)
        if p1 > a1 + 1e-3:
            fin = cover_r3(p["in"] + (a1 - p0) / pace)
            q = {**p, "in": fin, "at": cover_r3(a1),
                 "out": cover_r3(fin + ((clip["out"] - a1) if p.get("to_end") else p1 - a1))}
            if p.get("to_end") or cover_frames(q, clip) >= COVER_FRAMES:
                out.append(q)
    return sorted(out + [piece], key=lambda p: p["at"])

def cover_cut(cover, s1, s2, pace, cin, lib=None):
    """A clip's cover cut at source seconds s1..s2 (a split: s1 == s2; a tighten or "Cut this
    out": the stretch between is removed). Returns (left side's, right side's, notes). The
    left keeps what is laid before s1; the right what is laid after s2, its pieces starting at
    s2 at the latest. Each piece stays laid over the same words. A piece that spans the cut
    plays its footage on unbroken (the right side picks up where the left stopped: a b-roll
    that skipped ahead would be a jump cut); one that starts inside the removed stretch starts
    at s2 from its own `in`; one laid entirely inside it is dropped (notes say so), and so is a
    right side whose footage ran out before the cut. cin: the clip's in point (the left side
    is cin..s1): the footage a spanning piece reached is counted in the left side's whole
    frames, so the right side picks up on the next footage frame. ui/ops.js coverCut is the same."""
    left, right, notes = [], [], []
    n_left = frames_of((s1 - cin) / pace)    # rounding as cover_plan and clip_frames do (ui/ops.js too)
    for n, p in enumerate(cover or [], 1):
        a, b = p["at"], (math.inf if p.get("to_end") else p["at"] + p["out"] - p["in"])
        if a < s1:
            q = dict(p)
            if b > s1 and not p.get("to_end"):
                q["out"] = cover_r3(p["in"] + s1 - a)
            left.append(q)
        if b > s2:
            if a >= s2:
                right.append(dict(p))
                continue
            if a < s1:                         # the footage reached at the left side's last frame
                r0 = (a - cin) / pace
                f0 = max(0, round((a - cin) * 30 / pace))
                fin = max(p["in"], p["in"] + f0 / 30 - r0) + (n_left - f0) / 30
            else:
                fin = p["in"]
            q = {**p, "in": cover_r3(fin), "at": cover_r3(s2)}
            if not p.get("to_end"):
                q["out"] = cover_r3(fin + b - s2)
            dur = float(((lib or {}).get(p["media"]) or {}).get("duration") or math.inf)
            if fin > dur - COVER_MIN:
                notes.append(f"cover {n}: its footage had run out before the cut; nothing of it is left after it")
            else:
                right.append(q)
        elif a >= s1 and s2 > s1:
            notes.append(f"cover {n} removed: it was laid inside the stretch that was cut out")
    return left, right, notes

def cover_line(x, clip, lib, at=None):
    """One piece as `timeline show` and `cover` say it: where it shows in the clip, as it plays
    (and in the reel, from at = the clip's start in the export), and which footage."""
    p = x["piece"]
    it = (lib or {}).get(p["media"]) or {}
    name = it.get("name", p["media"])
    f0, fe = x["laid"]
    if fe - f0 < COVER_FRAMES:
        return f"{name}: laid outside the clip, shows nothing"
    s0, s1 = x["shown"] or (f0, f0)
    out = f"{s0 / 30:.2f}–{s1 / 30:.2f}s of the clip" + (", to its end" if p.get("to_end") else "")
    if at is not None:
        out += f" ({at + s0 / 30:.2f}–{at + s1 / 30:.2f}s in the reel)"
    t0 = x.get("t0", p["in"])
    out += f" shows {name} {t0:.2f}–{t0 + (s1 - s0) / 30:.2f}s" + (f"  [{p['fit']}]" if p.get("fit") else "")
    if x["short"]:
        out += f"  SHORT: {cover_short_text(x, name)}"
    return out

# ---------- voice: an optional treatment of a clip's sound, before loudnorm ------------------
# "natural": rumble off, gentle denoise, a little less mud and a little more presence, soft
# de-ess, light compression. Tune it here; a clip's voice (or the story's) picks it by name.
VOICE_CHAINS = {
    "natural": "highpass=f=85,afftdn=nr=10:nf=-42:tn=1,equalizer=f=220:t=q:w=1.2:g=-2,"
               "equalizer=f=3200:t=q:w=1.4:g=2.5,deesser=i=0.35,"
               "acompressor=threshold=-22dB:ratio=2.5:attack=8:release=120:makeup=2",
}
VOICES = ("off", *VOICE_CHAINS)
VOICE_DEFAULT = "natural"      # what a spoken clip gets when nobody chose (operator-approved)

def clip_voice(clip, tl, spoken=False):
    """The voice treatment a clip renders with: its own setting, else the story's, else
    VOICE_DEFAULT for a clip with speech in it and off for one without."""
    v = clip.get("voice") if clip.get("voice") in VOICES else tl.get("voice")
    v = v if v in VOICES else (VOICE_DEFAULT if spoken else "off")
    return v if v in VOICE_CHAINS else "off"

def clip_spoken(clip, regions=None):
    """Does the clip have speech in it (its media's speech regions, cached ones by default)?"""
    regs = regions if regions is not None else (speech_regions(clip["media"]) or [])
    return sum(max(0.0, min(clip["out"], r[1]) - max(clip["in"], r[0])) for r in regs) >= SNAP_EDGE

# ---------- pace: a clip played a little faster, pitch kept ------------------------------
# atempo on the sound, setpts on the clip's own picture. Above 1.2 speech stops sounding
# natural. Everything measured on the export (captions, snapping's lead and tail, check, cut
# times) runs on the sped-up clock: a clip lasts (out - in) / pace there. Cover pieces play
# at normal speed over the same words.
PACE_RANGE = (1.0, 1.2)

def clip_pace(clip):
    try:
        return min(PACE_RANGE[1], max(PACE_RANGE[0], float(clip.get("pace", 1))))
    except (TypeError, ValueError):
        return 1.0

def clip_len(clip):
    """How long the clip lasts in the export, before it's rounded to whole frames."""
    return (clip["out"] - clip["in"]) / clip_pace(clip)

FPS = 30             # every export runs at 30 fps, and every clip is a whole number of its frames
SR = 48000           # and its sound at 48 kHz: one frame is exactly SR / FPS = 1600 samples

def frames_of(secs):
    return max(1, int(round(secs * FPS + 1e-6)))

def clip_frames(clip):
    """How many export frames the clip lasts: its picture and its sound, exactly."""
    return frames_of(clip_len(clip))

# Spoken cuts sit on speech edges (QUALITY.md): a lead-in of 0.05-0.15 s before the first
# word, >= 0.25 s of tail after the last one's decay, never inside speech. Lead + tail stay
# under the 0.35 s pause limit, so two snapped clips butt together without a dead gap.
SNAP_LEAD, SNAP_TAIL = 0.07, 0.26
SNAP_EDGE = 0.12      # speech overlapping a clip by less than this at an edge is the end of
                      # the phrase before (or the start of the one after): left out, not pulled in
SNAP_REACH = 1.5      # the farthest a cut travels to reach a speech edge; past that (a long run
                      # of speech with no pause) it stays near where it was asked, at the
                      # quietest moment there, and `studio check` flags it

def snap_clip(a, b, regions, dur=None, words=None, quiet=None, pace=1.0):
    """(a, b) moved onto speech edges: the in point to SNAP_LEAD before the first phrase the
    clip speaks (back to that phrase's start if it was cut into), the out point to SNAP_TAIL
    after the last one (on to its end if it was cut into), never into the speech either side.
    Leading and trailing dead air goes. A clip with no speech in it is left alone (a silent
    shot, a held reveal). words (the transcript's): the phrase holding the last word that
    starts inside the clip stays in, even when whisper's early word end put the out point
    before that phrase began. quiet(t, lo, hi): the quietest moment in lo-hi, for a cut deep
    in a phrase (see SNAP_REACH). pace: the clip plays this much faster, so its lead and tail
    are that much longer in the source (they're measured on the export). Returns (a, b,
    reason or None)."""
    regions = regions or []
    lead, tail = SNAP_LEAD * pace, SNAP_TAIL * pace
    mine = [r for r in regions if r[1] > a and r[0] < b]
    keep_last = False
    said = [w["start"] for w in words or [] if a <= w["start"] < b]
    if said:
        r = next((r for r in regions if r[1] > said[-1]), None)
        if r and r[0] - said[-1] < 0.5 and r not in mine:
            mine.append(r)
        keep_last = bool(r) and r[0] - said[-1] < 0.5
    if len(mine) > 1 and mine[0][1] - a < SNAP_EDGE:
        mine = mine[1:]                    # just the tail of the phrase before
    if len(mine) > 1 and not keep_last and b - mine[-1][0] < SNAP_EDGE:
        mine = mine[:-1]                   # just the onset of the phrase after
    if not mine or (len(mine) == 1 and not keep_last
                    and min(b, mine[0][1]) - max(a, mine[0][0]) < SNAP_EDGE):
        return a, b, None
    first, last = mine[0], mine[-1]
    before = max((r[1] for r in regions if r[1] <= first[0]), default=0.0)
    after = min((r[0] for r in regions if r[0] >= last[1]), default=dur if dur else last[1] + 9e9)
    na = max(0.0, first[0] - lead)
    if na < before:                        # no room for a full lead-in: halfway into the gap
        na = (before + first[0]) / 2
    nb = last[1] + tail
    if nb > after:
        nb = (last[1] + after) / 2
    if dur:
        nb = min(nb, dur)
    deep = []
    if first[0] < a - SNAP_REACH:
        na = quiet(a, a - 0.4, a + 0.4) if quiet else a
        deep.append("in")
    if last[1] - max(b, last[0]) > SNAP_REACH:
        nb = quiet(b, b - 0.2, b + 0.6) if quiet else b
        deep.append("out")
    na, nb = round(na, 3), round(nb, 3)
    if abs(na - a) < 1e-3 and abs(nb - b) < 1e-3:
        return a, b, None
    why = [f"{x} is deep in a run of speech: at its quietest moment nearby" for x in deep]
    if first[0] < a - 1e-3 and "in" not in deep:
        why.append("in was inside speech")
    if last[1] > b + 1e-3 and "out" not in deep:
        why.append("out was inside speech")
    if not why:
        why.append("to the speech edges")
    return na, nb, ", ".join(why)

PAUSE_CUT_SKIP = 3.0  # the most silence a pause cut takes out of one take; more is a new beat

def pause_cut(c, nxt, regions):
    """Is c -> nxt a pause cut: the same take going on a little later, skipping only silence
    (what `timeline tighten` makes)? Its two edges are one pause, not a beat's end and start."""
    return bool(nxt) and nxt["media"] == c["media"] and 0 <= nxt["in"] - c["out"] <= PAUSE_CUT_SKIP \
        and not any(r[1] > c["out"] + 0.01 and r[0] < nxt["in"] - 0.01 for r in regions or [])

def snap_media(mid, a, b, pace=1.0):
    """snap_clip() with this media's speech regions (made now if it has none yet) and words."""
    it = library().get(mid) or {}
    words = load_json_cached(mdir(mid) / "transcript.json", {}).get("words")
    return snap_clip(a, b, ensure_speech(mid), it.get("duration"), words,
                     lambda t, lo, hi: quietest(it, t, lo, hi), pace)

def quietest(item, t, lo, hi):
    """The middle of the quietest 30 ms of item's audio between lo and hi (t if unreadable)."""
    import array
    lo = max(0.0, lo)
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}",
                        "-i", item.get("path", ""), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                       capture_output=True)
    x = array.array("h", r.stdout[:len(r.stdout) // 2 * 2])
    win, hop = 480, 160
    if r.returncode != 0 or len(x) < win:
        return t
    best = min(range(0, len(x) - win + 1, hop), key=lambda i: sum(v * v for v in x[i:i + win:4]))
    return round(lo + (best + win / 2) / 16000, 3)

def snap_note(c, na, nb, why):
    return f"snapped {c.get('id') or ''} {c['in']:.2f}-{c['out']:.2f} -> {na:.2f}-{nb:.2f} ({why})".replace("  ", " ")

class StaleRev(RuntimeError):
    def __init__(self, rev, actor):
        super().__init__(f"story changed since your rev (now rev {rev}, by {actor or '?'})")
        self.rev, self.actor = rev, actor

def timeline_save(slug, tl, actor="cli", base=None, problems=None):
    """Normalize and save. With base (the rev the caller edited from), refuse with StaleRev
    if anyone saved since. A file lock makes check-and-save atomic across the server and
    any number of CLI processes."""
    import fcntl
    pdir(slug).mkdir(parents=True, exist_ok=True)
    with open(HOME / ".timeline.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            st = state()
            if base is not None:
                pj = st["projects"].get(slug) or {}
                if int(base) != pj.get("rev", 0):
                    raise StaleRev(pj.get("rev", 0), pj.get("rev_actor"))
            tl = normalize_timeline(tl, st["media"], problems)
            (pdir(slug) / "timeline.json").write_text(json.dumps(tl, indent=1))
            emit("timeline.saved", None, project=slug, timeline=tl, actor=actor)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
    return tl


# ---------- steps -----------------------------------------------------------

SETTLE_SECS = float(os.environ.get("STUDIO_SETTLE_SECS", 3))
SETTLE_WAIT_SECS = float(os.environ.get("STUDIO_SETTLE_WAIT_SECS", 900))        # the app's drag and drop
CLI_SETTLE_WAIT_SECS = float(os.environ.get("STUDIO_CLI_SETTLE_WAIT_SECS", 60))  # `studio ingest FILE`

def unsettled(st, settle):
    """Was this file written in the last `settle` seconds? A modification time more than a
    second in the future (a camera's or phone's clock, `cp -p` from a device) is a finished
    file, not one being written."""
    age = time.time() - st.st_mtime
    return -1.0 <= age < settle

def wait_settled(f, settle=None, on_wait=None, limit=None):
    """A file still being written (AirDrop, a copy from a card, a render that just finished)
    is waited for, not refused: it counts as settled once it hasn't changed for `settle`
    seconds (STUDIO_SETTLE_SECS). on_wait() is called once if it has to wait. Gives up (Skip)
    after `limit` seconds (default STUDIO_SETTLE_WAIT_SECS)."""
    settle = SETTLE_SECS if settle is None else settle
    limit = SETTLE_WAIT_SECS if limit is None else limit
    end, said = time.time() + limit, False
    while True:
        try:
            st = f.stat()
        except FileNotFoundError:
            raise Skip("it's gone")
        if not unsettled(st, settle):
            return
        if time.time() > end:
            raise Skip(f"it was still being written after {limit:g}s; run ingest again when it's "
                       f"done (STUDIO_CLI_SETTLE_WAIT_SECS waits longer)")
        if not said and on_wait:
            on_wait()
        said = True
        time.sleep(0.25)

class Skip(Exception):
    """A file ingest leaves where it is, with the reason printed."""

def probe_ok(path):
    """probe(), but refuse what can't be used: unreadable, no streams, or 0 seconds long."""
    try:
        meta = probe(path)
    except (RuntimeError, ValueError, KeyError) as e:
        last = [l.replace(f"{path}: ", "").strip() for l in str(e).splitlines() if l.strip()]
        raise Skip(f"can't read it: {last[-1][:160] if last else 'ffprobe failed'}")
    if not meta["has_video"] and not meta["has_audio"]:
        raise Skip("no video or sound in it")
    if not meta["duration"] or meta["duration"] < 0.1:
        raise Skip("it's 0 seconds long")
    if not tail_decodes(path, meta["duration"]):
        # the index says N seconds but the end isn't there: a cut-off copy or upload
        # (faststart files put the index first, so they probe as complete)
        raise Skip("the end of the file is missing (an interrupted copy?)")
    return meta

def tail_decodes(path, dur):
    """Decode the last second. A truncated file whose index survived probes fine but decodes
    nothing here. ~0.05 s for 1080p, ~0.5 s for 4K HDR."""
    a = max(0.0, dur - 1.5)
    try:
        out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{a:.3f}", "-i", str(path),
                              "-t", "1", "-progress", "pipe:1", "-nostats", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=60).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False
    got = [l.split("=", 1)[1] for l in out.splitlines() if l.startswith("out_time_us=")]
    us = int(got[-1]) if got and got[-1].strip().isdigit() else 0
    return us >= min(200_000, int((dur - a) * 0.5e6))

def ingest(paths, brand="jefe", project=DEFAULT_PROJECT, names=None, settle=None, problems=None,
           move=True):
    """Bring footage into the library. Per file: wait until it has stopped changing (a file
    named in `paths` is waited for; one found in inbox/ that's still being written is left for
    the next run), hash it, probe it where it is, and only then move it (inbox/, uploads/) or
    copy it (anywhere else) into media/ID/. Bytes already in the library are that media (by
    sha256): it stays in its project unless that's the inbox (then it's sorted into `project`).
    A file that can't be used is reported and left where it was; one bad file never stops
    the batch. `problems`, if a list, collects (name, reason) for every media file skipped.
    move=False never moves a file, even out of inbox/ (the desktop app's drag and drop: the
    operator's file stays exactly where it was). Copies are APFS clones when they can be."""
    need("ffprobe")
    ensure_project(project)
    settle = SETTLE_SECS if settle is None else settle
    inbox = HOME / "inbox"
    files = [Path(p) for p in paths] if paths else sorted(
        p for p in inbox.iterdir() if p.is_file() and not p.name.startswith("."))
    known = {v.get("sha256"): k for k, v in library().items()}
    ids = []
    for f in files:
        display = (names or {}).get(str(f), f.name)
        if f.suffix.lower() not in VIDEO_EXT | AUDIO_EXT:
            print(f"  skip {display} (not media)")
            continue
        try:
            mid = _ingest_one(f, display, brand, project, known, settle, inbox, move, wait=bool(paths))
        except Exception as e:  # noqa: BLE001 — one bad file must not sink the batch
            reason = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
            if not isinstance(e, Skip):
                reason = f"{type(e).__name__}: {reason}"
            print(f"  skip {display} ({reason})", flush=True)
            if problems is not None:
                problems.append((display, reason))
            continue
        ids.append(mid)
    return ids

def _ingest_one(f, display, brand, project, known, settle, inbox, move_ok=True, wait=False):
    try:
        st0 = f.stat()
    except FileNotFoundError:
        raise Skip("it's gone")
    if settle and unsettled(st0, settle):
        if not wait:
            raise Skip("still copying; run ingest again in a moment")
        wait_settled(f, settle, lambda: print(f"  wait {display} (still being written)", flush=True),
                     CLI_SETTLE_WAIT_SECS)
        st0 = f.stat()
    if st0.st_size == 0:
        raise Skip("the file is empty")
    digest = receipt(f)
    st1 = f.stat()
    if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns):
        raise Skip("still copying (it changed while being read); run ingest again in a moment")
    if digest in known:
        # the same bytes are the same media. It stays where it is: moving it would take it out
        # of another project (`studio media move` does that, on purpose). Unsorted footage
        # (the inbox) is sorted into the project it's ingested for.
        mid = known[digest]
        where = library()[mid]["project"]
        if where == DEFAULT_PROJECT and project != DEFAULT_PROJECT:
            emit("media.moved", mid, project=project)
            print(f"  have {display} → {mid}  (moved from {where} to {project})")
        elif where != project:
            print(f"  have {display} → {mid}  (byte for byte the same as {library()[mid].get('name')!r} "
                  f"in project {where}; any story can use it)")
        else:
            print(f"  have {display} → {mid}")
        return mid
    mid = digest[:12]
    meta = probe_ok(f)                     # in place: nothing has moved yet
    d = mdir(mid)
    d.mkdir(parents=True, exist_ok=True)
    move = move_ok and f.parent.resolve() in (inbox.resolve(), (HOME / "uploads").resolve())
    # until the ingested event is written this dir is provisional: recover() at server start
    # puts a moved source back in inbox/ if the process died in between
    marker = d / ".ingesting"
    marker.write_text(json.dumps({"pid": os.getpid(), "from": str(f), "name": display, "moved": move}))
    dest = d / f"source{f.suffix.lower()}"
    if move:
        shutil.move(str(f), dest)
    else:
        tmp = d / f".source.{os.getpid()}{f.suffix.lower()}"
        clone_or_copy(f, tmp)
        os.replace(tmp, dest)
    emit("ingested", mid, name=display, sha256=digest, path=str(dest), project=project, **meta)
    emit("brand.set", mid, brand=brand)
    marker.unlink(missing_ok=True)
    known[digest] = mid
    print(f"  new  {display} → {mid}  ({fmt_t(meta['duration'])}, "
          f"{'audio' if meta['has_audio'] else 'silent'})")
    return mid

_clonefile = None

def clone_or_copy(src, dst):
    """Copy src to dst (which must not exist). On APFS this is a clone (clonefile(2), what
    `cp -c` does): instant, and it shares the blocks until either side changes, so a 4 GB
    drop takes no extra space. Anywhere else (another volume, a network share, Linux) it's
    a normal copy. src is only ever read. Returns "clone" or "copy"."""
    global _clonefile
    if sys.platform == "darwin":
        if _clonefile is None:
            try:
                fn = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).clonefile
                fn.argtypes, fn.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
                _clonefile = fn
            except (OSError, AttributeError):
                _clonefile = False
        if _clonefile:
            if _clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
                return "clone"
            err = ctypes.get_errno()
            if err in (errno.ENOENT, errno.EACCES, errno.EPERM) and not Path(src).exists():
                raise FileNotFoundError(err, os.strerror(err), str(src))
            Path(dst).unlink(missing_ok=True)   # EXDEV/ENOTSUP/...: not clonable here
    shutil.copy2(src, dst)
    return "copy"

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True

def recover(log=print):
    """Clean up after an ingest that died half way: a media/ID/ dir that still has its
    .ingesting marker, no ingested event, and no live process behind it. A source that was
    moved out of inbox/ or uploads/ goes back into inbox/ (never deleted); a copy of a file
    that lives elsewhere is just removed. Dirs without the marker are never touched."""
    lib = library()
    root = HOME / "media"
    if not root.is_dir():
        return []
    fixed = []
    for d in sorted(root.iterdir()):
        mk = d / ".ingesting"
        if not d.is_dir() or d.name in lib or not mk.exists():
            continue
        try:
            info = json.loads(mk.read_text())
        except ValueError:
            info = {}
        if _pid_alive(info.get("pid")) and info.get("pid") != os.getpid():
            continue                       # that ingest is still running
        for src in d.glob("source.*"):
            if info.get("moved"):
                (HOME / "inbox").mkdir(exist_ok=True)
                name = Path(info.get("name") or src.name).name
                back = HOME / "inbox" / name
                k = 2
                while back.exists():
                    back = HOME / "inbox" / f"{Path(name).stem}-{k}{Path(name).suffix}"
                    k += 1
                shutil.move(str(src), back)
                log(f"recovered {name} from an unfinished ingest → {back}")
        shutil.rmtree(d, ignore_errors=True)
        fixed.append(d.name)
    return fixed

VT_SDR = "color_matrix=bt709:color_primaries=bt709:color_transfer=bt709"

def vt_scale(w, h, vi):
    """scale_vt, plus an HDR (HLG/PQ, BT.2020) -> SDR BT.709 conversion when the source needs
    one. SDR sources are left alone: asking VT to 'convert' untagged video shifts its colours."""
    conv = f":{VT_SDR}" if vi["wide"] or vi["tenbit"] else ""
    return f"scale_vt=w={w}:h={h}{conv}"

def _dec(use_hw):
    """Decoder args. Hardware frames stay on the GPU until a CPU filter needs them; rotation is
    then applied explicitly (transpose_vt) instead of by autorotate."""
    # -display_rotation 0 drops the source's rotation matrix too, or it would be copied to the
    # output and players would rotate the (already upright) picture a second time
    return (["-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld",
             "-noautorotate", "-display_rotation", "0"] if use_hw else [])

# Measured on M4 Max (see README): for proxies, VideoToolbox decode wins on 4K and on 10-bit
# HDR (where it also does the HDR->SDR conversion); at <=1440p the multithreaded software
# decoder is faster. Renders always use the VT path (it keeps scaling off the CPU, and with
# 4 segments in flight it beat software decode by ~45%).
def prefer_hw_decode(item):
    vi = vinfo(item["path"])
    if os.environ.get("STUDIO_HWDEC") == "force":
        return hw()["dec"]
    return hw()["dec"] and (vi["tenbit"] or item.get("width", 0) * item.get("height", 0) > 2560 * 1600)

def _with_hw_fallback(build, run, use_hw=True):
    """run(build(True)) on VideoToolbox, falling back to the software graph if the hardware
    path fails for this file (unsupported codec, odd pixel format)."""
    if hw()["dec"] and use_hw:
        try:
            return run(build(True))
        except RuntimeError as e:
            print(f"    videotoolbox decode failed, using software: {str(e).splitlines()[-1][:160]}",
                  file=sys.stderr)
    return run(build(False))

def keyframe_times(path):
    """Seconds (from file start) of every video keyframe — packet flags only, no decoding."""
    out = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
              "packet=pts_time,flags:format=start_time", "-of", "csv=p=0", str(path)])
    start, ts = 0.0, []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[-1] and parts[0] not in ("", "N/A"):
            ts.append(float(parts[0]))
        elif len(parts) == 1 and parts[0] not in ("", "N/A"):
            start = float(parts[0])
    return sorted(t - start for t in ts)

def chunk_plan(item, n):
    """Split points on source keyframes, so each chunk decodes from its own I-frame and the
    chunks join with no gap or overlap."""
    if n <= 1:
        return [(0.0, None)]
    try:
        kfs = keyframe_times(item["path"])
    except RuntimeError:
        return [(0.0, None)]
    dur, cuts = item["duration"], []
    for k in range(1, n):
        target = dur * k / n
        best = min(kfs, key=lambda t: abs(t - target), default=None)
        if best is not None and 1.0 < best < dur - 1.0 and all(abs(best - c) > 1.0 for c in cuts):
            cuts.append(best)
    edges = [0.0] + sorted(cuts)
    return [(a, edges[i + 1] if i + 1 < len(edges) else None) for i, a in enumerate(edges)]

def proxy(item):
    """720p H.264 for smooth playback and scrubbing in the editor; renders use the source.
    Long files are transcoded as keyframe-aligned chunks in parallel (decode is the
    bottleneck and one ffmpeg can't keep the media engines busy), then joined losslessly."""
    if not item["has_video"]:
        return {"skipped": "no video"}
    need("ffmpeg")
    d = mdir(item["id"])
    out = d / "proxy.mp4"
    src, dur = item["path"], item["duration"]
    if item["width"] >= item["height"]:
        tw, th = even(720 * item["width"] / item["height"]), 720
    else:
        tw, th = 720, even(720 * item["height"] / item["width"])
    vi = vinfo(src)
    use_hw = prefer_hw_decode(item)
    # 60fps screen recordings scrub just as well at 30, for half the encode work
    fps = ",fps=30" if vi["fps"] > 31 else ""
    # chunk when there's enough to split: ~1 chunk per 10s of 1080p-equivalent pixels
    heavy = dur * item["width"] * item["height"] / (1920 * 1080)
    n = max(1, min(PROXY_CHUNKS, int(heavy // 10)))
    if _exporting[0]:          # an export is running: leave the media engine to it
        n = 1
    plan = chunk_plan(item, n)
    prog = Progress("proxy", item["id"], weights={i: ((b or dur) - a) for i, (a, b) in enumerate(plan)})
    audio = ["-c:a", "aac", "-b:a", "128k"] if item["has_audio"] else ["-an"]

    def build(use_hw, a=0.0, b=None, path=None, with_audio=True):
        rng = (["-ss", f"{a:.6f}"] if a else []) + (["-t", f"{b - a:.6f}"] if b is not None else [])
        if use_hw:
            vf = ",".join(x for x in (vt_transpose(vi["theta"]),
                                      vt_scale(tw, th, vi)) if x) + fps + "," + SETPARAMS
        else:
            vf = f"scale={tw}:{th}{fps},{SETPARAMS},format={'nv12' if hw()['enc'] else 'yuv420p'}"
        return ["ffmpeg", "-y", "-v", "error", *_dec(use_hw), *rng, "-i", src, "-vf", vf,
                *venc("proxy", 15), *(audio if with_audio else ["-an"]),
                "-movflags", "+faststart", str(path or out)]

    if len(plan) == 1:
        _with_hw_fallback(build, lambda cmd: ff(cmd, dur, prog.part(0)), use_hw)
    else:
        work = d / "proxy.parts"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        try:
            def chunk(i):
                a, b = plan[i]
                part = work / f"p{i:02d}.mp4"
                _with_hw_fallback(lambda h: build(h, a, b, part, with_audio=False),
                                  lambda cmd: ff(cmd, (b or dur) - a, prog.part(i)), use_hw)
                return part
            def aud():
                if item["has_audio"]:
                    sh(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", *audio, str(work / "a.m4a")])
            with ThreadPoolExecutor(len(plan) + 1) as ex:
                fa = ex.submit(spawn_ctx(aud))
                parts = list(ex.map(spawn_ctx(chunk), range(len(plan))))
                fa.result()
            (work / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in parts))
            inputs = ["-f", "concat", "-safe", "0", "-i", "list.txt"]
            maps = ["-map", "0:v:0"]
            if item["has_audio"]:
                inputs += ["-i", "a.m4a"]
                maps += ["-map", "1:a:0"]
            sh(["ffmpeg", "-y", "-v", "error", *inputs, *maps, "-c", "copy",
                "-movflags", "+faststart", str(out)], cwd=work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return {"file": "proxy.mp4", "sha256": receipt(out), "chunks": len(plan),
            "encoder": "videotoolbox" if hw()["enc"] else "libx264"}

# ---------- speech: where someone is talking (Silero VAD) ----------------------
# Whisper's word times run ~150 ms early and snap to whole seconds near sentence ends, so
# cutting on them clips the last word. Silero VAD (whisper.cpp's ggml port, run by
# whisper-vad-speech-segments) says where speech really is: media/<id>/speech.json holds those
# regions, cuts snap to them, whisper only hears them, and `studio check` measures against them.

VAD_MODEL_NAME = "ggml-silero-v6.2.0.bin"
VAD_MODEL_URL = ("https://huggingface.co/ggml-org/whisper-vad/resolve/"
                 "9ffd54a1e1ee413ddf265af9913beaf518d1639b/ggml-silero-v6.2.0.bin")
VAD_MODEL_SHA256 = "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"
# whisper-cli's own --vad defaults, spelt out and passed to both tools: the regions in
# speech.json are then exactly the ones whisper hears, which the token-time map relies on.
VAD_ARGS = ["-vt", "0.5", "-vspd", "250", "-vsd", "100", "-vp", "30", "-vo", "0.1"]

def vad_model():
    """The Silero model: $STUDIO_VAD_MODEL (the flake sets it to a pinned store path), else
    ~/.local/share/studio/models/, downloaded there once and checked against its sha256."""
    env = os.environ.get("STUDIO_VAD_MODEL")
    if env and Path(env).is_file():
        return Path(env)
    p = MODEL.parent / VAD_MODEL_NAME
    if p.is_file():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}")
    try:
        import urllib.request
        with urllib.request.urlopen(VAD_MODEL_URL, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        if receipt(tmp) != VAD_MODEL_SHA256:
            raise RuntimeError("the download doesn't match its pinned sha256")
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"VAD model missing at {p} and the download failed ({e}); fetch "
                           f"{VAD_MODEL_URL} there (sha256 {VAD_MODEL_SHA256}) or run in `nix develop`")
    return p

def vad_regions(wav):
    """Speech regions [[start, end], ...] (seconds, 10 ms steps) of a 16 kHz mono WAV."""
    need("whisper-vad-speech-segments")
    out = sh(["whisper-vad-speech-segments", "-f", str(wav), "-vm", str(vad_model()),
              *VAD_ARGS, "-np"])
    return [[round(float(a) / 100, 2), round(float(b) / 100, 2)]
            for a, b in re.findall(r"start = ([\d.]+), end = ([\d.]+)", out)]

def speech_file(mid):
    return mdir(mid) / "speech.json"

def write_speech(mid, regions, duration=None):
    out = speech_file(mid)
    tmp = out.with_name(f".speech.{os.getpid()}.json")
    tmp.write_text(json.dumps({"model": VAD_MODEL_NAME, "args": VAD_ARGS, "duration": duration,
                               "regions": regions}, indent=1))
    os.replace(tmp, out)

def speech_regions(mid):
    """speech.json's regions, or None if this file has none yet (or they came from other
    VAD settings)."""
    sp = load_json_cached(speech_file(mid), None)
    if not sp or sp.get("model") != VAD_MODEL_NAME or sp.get("args") != VAD_ARGS:
        return None
    return sp.get("regions") or []

def speech(item):
    """The speech step: speech.json for one file. Cheap (CPU, ~25x realtime), so it also
    backfills media transcribed before it existed (`studio speech --all`)."""
    if not item["has_audio"]:
        return {"regions": 0, "skipped": "no audio track"}
    need("ffmpeg")
    wav = audio_wav(item)
    regs = vad_regions(wav)
    write_speech(item["id"], regs, item.get("duration"))
    if "transcribe" in item["steps"]:          # nothing left to use the WAV
        wav.unlink(missing_ok=True)
    return {"regions": len(regs), "speech_s": round(sum(b - a for a, b in regs), 2),
            "sha256": receipt(speech_file(item["id"]))}

def ensure_speech(mid):
    """speech_regions(mid), running the speech step first if it's missing (older media)."""
    regs = speech_regions(mid)
    if regs is None and library().get(mid, {}).get("has_audio"):
        run_step("speech", [mid], force=True, quiet=True)
        regs = speech_regions(mid)
    return regs

def vad_time_map(regions, n_samples, rate=16000):
    """whisper-cli --vad decodes a shortened file: each speech region, plus 0.1 s of the audio
    after it, then 0.1 s of silence, and so on. It maps *segment* times back to the original
    file but not token times, so a word lands seconds early. This rebuilds whisper.cpp's map
    (whisper_vad(): same sample rounding) from the same regions. Returns map(t_ms, end) ->
    seconds: a time in the overlap or the inserted silence goes to the speech edge (the end
    of the region before it for an end, the start of the next one for a start)."""
    cs = [(round(a * 100), round(b * 100)) for a, b in regions]
    s2cs = lambda s: int(s / rate * 100 + 0.5)
    c2s = lambda c: int(c / 100 * rate + 0.5)
    blocks, off = [], 0                    # (vad_start, vad_end, orig_start, orig_end) in cs
    for i, (a, b) in enumerate(cs):
        s0, s1 = min(c2s(a), n_samples - 1), min(c2s(b), n_samples - 1)
        ln = s1 - s0
        if i < len(cs) - 1:
            s1 = min(s1 + int(0.1 * rate), n_samples - 1)
        if s1 - s0 <= 0:
            continue
        blocks.append((s2cs(off), s2cs(off + ln), a, b))
        off += (s1 - s0) + (int(0.1 * rate) if i < len(cs) - 1 else 0)
    starts = [x[0] for x in blocks]

    def fn(t_ms, end=False):
        t = t_ms / 10
        if not blocks:
            return t / 100
        i = max(0, bisect.bisect_right(starts, t) - 1)
        v0, v1, o0, o1 = blocks[i]
        if t <= v0:
            return o0 / 100
        if t <= v1:
            return (o0 + (t - v0) * (o1 - o0) / max(1, v1 - v0)) / 100
        if end or i + 1 >= len(blocks):
            return o1 / 100
        return blocks[i + 1][2] / 100
    return fn

def wav_samples(wav):
    import wave
    with wave.open(str(wav)) as w:
        return w.getnframes()


# ---------- transcribe ------------------------------------------------------

WHISPER_NST = os.environ.get("STUDIO_WHISPER_NST", "0.6")    # --no-speech-thold

def run_whisper(wav, base, vocab, regions, prog=None):
    """whisper-cli on a 16 kHz WAV, hearing only the speech regions: --vad with the same
    Silero settings, non-speech tokens suppressed (-sns) and no carried-over text (-mc 0;
    the vocabulary prompt is still carried). Returns (language, segments, words), word
    times on the WAV's own timeline. No regions: no speech, so whisper isn't run at all."""
    if not regions:
        return None, [], []
    # Metal does the heavy lifting; -t feeds the CPU-side decoder. Greedy decoding (with
    # whisper's temperature fallback still on) measured as accurate as beam 5 and ~12% faster.
    # Not --dtw: it needs -nfa, cost +75% here, and its token times are VAD-shifted too.
    cmd = ["whisper-cli", "-m", str(MODEL), "-f", str(wav), "-l", "auto", "-oj", "-ojf",
           "-of", str(base), "-np", "-pp", "-t", str(WHISPER_THREADS),
           "-bs", str(WHISPER_BEAM), "-bo", str(max(WHISPER_BEAM, 1)), "-nth", WHISPER_NST,
           "--vad", "-vm", str(vad_model()), *VAD_ARGS, "-sns", "-mc", "0"]
    if vocab:   # the brand's own words, so "Jefe" isn't heard as "Hefe"
        cmd += ["--prompt", ", ".join(vocab) + ".", "--carry-initial-prompt"]
    tail = []
    with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                          errors="replace") as p:
        for line in p.stderr:
            mm = re.search(r"progress\s*=\s*(\d+)%", line)
            if mm:
                if prog:
                    prog.update(None, int(mm.group(1)) / 100)
            else:
                tail = (tail + [line])[-40:]
        p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"whisper-cli failed ({p.returncode}):\n{''.join(tail)[-2000:]}")
    raw_path = Path(f"{base}.json")
    raw = json.loads(raw_path.read_text(errors="replace"))
    raw_path.unlink(missing_ok=True)      # parsed; transcript.json is the record
    segments, words = whisper_words(raw, vad_time_map(regions, wav_samples(wav)), regions)
    return raw.get("result", {}).get("language"), segments, words

def transcribe(item):
    d = mdir(item["id"])
    if not item["has_audio"]:
        return {"words": 0, "skipped": "no audio track"}
    need("ffmpeg"); need("whisper-cli")
    if not MODEL.exists():
        raise RuntimeError(f"whisper model missing at {MODEL}")
    wav = audio_wav(item)
    regions = speech_regions(item["id"])
    if regions is None:                    # the speech step hasn't run: same regions, now
        regions = vad_regions(wav)
        write_speech(item["id"], regions, item.get("duration"))
    vocab, fixes = brand_words(item.get("brand"))
    language, segments, words = run_whisper(wav, d / "whisper", vocab, regions,
                                            Progress("transcribe", item["id"]))
    segments, words, dropped = drop_hallucinations(segments, words, wav)
    fixed = apply_fixes(segments, words, fixes + case_fixes(vocab))
    write_transcript(d, language, segments, words)
    wav.unlink(missing_ok=True)
    return {"words": len(words), "segments": len(segments), "fixed": fixed, "dropped": dropped,
            "speech": len(regions), "sha256": receipt(d / "transcript.json")}

def whisper_words(raw, tmap=None, regions=None):
    """whisper-cli -ojf JSON -> (segments, words). Each word is the run of tokens from one
    space-led token to the next. Whisper often gives a segment's first word zero length (its
    end is its start); it gets its real span (to the next word, never past the end of its
    speech region) here, or captions skip it. tmap: vad_time_map() for --vad token times."""
    segments, words = [], []
    starts = [a for a, _ in regions or []]
    def region_end(t):     # where whisper stopped hearing the region t is in: its end + the
        j = bisect.bisect_right(starts, t + 1e-3) - 1          # 0.1 s --vad adds (-vo 0.1)
        return regions[j][1] + 0.1 if j >= 0 and t <= regions[j][1] + 0.1 else None
    for seg in raw.get("transcription", []):
        text = seg["text"].strip()
        if not text:
            continue
        s0, s1 = seg["offsets"]["from"] / 1000, seg["offsets"]["to"] / 1000
        si = len(segments)
        segments.append({"start": s0, "end": s1, "text": text})
        cur, mine = None, []
        for tok in seg.get("tokens", []):
            t = tok.get("text", "")
            if t.startswith("[_") or t.startswith("<|"):
                continue
            o = tok["offsets"]
            a, b = (tmap(o["from"]), tmap(o["to"], end=True)) if tmap else (o["from"] / 1000, o["to"] / 1000)
            b = max(a, b)
            if t.startswith(" ") or cur is None:
                cur = {"w": t.strip(), "start": round(a, 3), "end": round(b, 3), "_seg": si}
                mine.append(cur)
            else:
                cur["w"] += t
                cur["end"] = round(max(cur["end"], b), 3)
        mine = [w for w in mine if w["w"]]
        for k, w in enumerate(mine):
            # a word never runs past its speech region, across a silence whisper never heard
            # (its last token can end a hair into the next region of the shortened audio)
            cap = region_end(w["start"]) if regions else None
            if cap is not None:
                w["end"] = round(max(w["start"], min(w["end"], cap)), 3)
            if w["end"] - w["start"] < 0.01:
                nxt = mine[k + 1]["start"] if k + 1 < len(mine) else s1
                lim = w["start"] + 1.0 if cap is None else min(w["start"] + 1.0, cap)
                w["end"] = round(max(w["start"], min(nxt, lim)), 3)
        words += mine
    return segments, words

HALLUCINATIONS = {"thank you", "thank you very much", "thanks for watching",
                  "thank you for watching", "thanks", "you", "bye", "bye bye"}
# Whisper's stock inventions (the "bag of hallucinations": YouTube outros and subtitle
# credits from its training data). A segment that is only one of these goes, wherever it is.
HALLUCINATION_PHRASES = {"thanks for watching", "thank you for watching", "thank you so much for watching",
                         "thanks for watching and see you next time", "please subscribe", "subscribe",
                         "like and subscribe", "please like and subscribe", "don't forget to subscribe",
                         "subscribe to my channel", "please subscribe to my channel",
                         "see you in the next video", "see you next time",
                         "thank you for listening", "thanks for listening"}
HALLUCINATION_RE = re.compile(r"amara\.org|subtitles? by|captions? by|transcri(bed|ption) by"
                              r"|castingwords|www\.\w+\.(com|org)", re.I)

def _norm(text):
    return re.sub(r"[^a-z' ]+", "", text.lower()).strip()

def _rms(wav, a, b):
    """RMS (0..1) of a 16-bit mono WAV between a and b seconds; None if unreadable."""
    import array, wave
    try:
        with wave.open(str(wav)) as w:
            rate, n = w.getframerate(), w.getnframes()
            i0, i1 = max(0, int(a * rate)), min(n, int(b * rate))
            if i1 <= i0 or w.getsampwidth() != 2:
                return None
            w.setpos(i0)
            x = array.array("h", w.readframes(i1 - i0))[::4]
    except Exception:  # noqa: BLE001
        return None
    return math.sqrt(sum(v * v for v in x) / max(1, len(x))) / 32768

def drop_hallucinations(segments, words, wav=None):
    """Whisper's two usual inventions: words with no length that sit alone (nothing real
    around them), and a lone "Thank you." (or similar) in a quiet tail after the speech.
    Returns (segments, words, dropped_texts). Also drops any segment that is only a stock
    phrase (HALLUCINATION_PHRASES) or a subtitle credit, wherever it is."""
    dropped, kill = [], set()
    for si, s in enumerate(segments):
        ws = [w for w in words if w.get("_seg") == si]
        if ws and all(w["end"] - w["start"] < 0.01 for w in ws) and s["end"] - s["start"] < 0.05:
            kill.add(si)
        elif _norm(s["text"]) in HALLUCINATION_PHRASES or HALLUCINATION_RE.search(s["text"]):
            kill.add(si)
    # a stock phrase as the very last segment, in (near) silence or after a long gap
    for si in range(len(segments) - 1, -1, -1):
        if si in kill:
            continue
        s = segments[si]
        if _norm(s["text"]) not in HALLUCINATIONS:
            break
        prev_end = max((x["end"] for k, x in enumerate(segments[:si]) if k not in kill), default=0.0)
        seg_r = all_r = None
        if wav is not None:
            seg_r, all_r = _rms(wav, s["start"], s["end"]), _rms(wav, 0, prev_end or s["end"])
        if seg_r is not None and all_r:
            quiet = seg_r < 0.35 * all_r          # far quieter than the speech before it
        else:
            quiet = s["start"] - prev_end > 2.0   # no audio to check: a long gap will do
        if quiet:
            kill.add(si)
        else:
            break
    for si in sorted(kill):
        dropped.append(segments[si]["text"])
    keep = [si for si in range(len(segments)) if si not in kill]
    remap = {old: new for new, old in enumerate(keep)}
    segs = [segments[si] for si in keep]
    ws = []
    for w in words:
        if w.get("_seg") in kill:
            continue
        if w.get("_seg") is not None:
            w = {**w, "_seg": remap[w["_seg"]]}
        ws.append(w)
    return segs, ws, dropped

_LP, _RP = "\"'“‘([¿¡", "\"'”’)].,!?;:…"

def _split_word(w):
    """'“Hefe's,' -> ('“', 'Hefe', "'s,"): leading punctuation, the word, possessive+trailing."""
    core = w.lstrip(_LP)
    pre = w[:len(w) - len(core)]
    stripped = core.rstrip(_RP)
    post = core[len(stripped):]
    for poss in ("'s", "’s"):
        if stripped.lower().endswith(poss) and len(stripped) > 2:
            stripped, post = stripped[:-2], stripped[-2:] + post
    return pre, stripped, post

def case_fixes(vocab):
    """Capitalised vocabulary is a proper noun: 'jefe' becomes 'Jefe'."""
    return [(v, v) for v in vocab if v[:1].isupper() and " " not in v]

def apply_fixes(segments, words, fixes):
    """Apply (heard, meant) pairs, case-insensitively and on whole words (a heard phrase of
    several words becomes one word spanning them), to words and segment text in place.
    Returns how many words changed."""
    n = 0
    for wrong, right in fixes:
        toks = [t.lower() for t in wrong.split()]
        if not toks:
            continue
        i = 0
        while i <= len(words) - len(toks):
            parts = [_split_word(words[i + k]["w"]) for k in range(len(toks))]
            if all(parts[k][1].lower() == toks[k] for k in range(len(toks))) and \
                    all(not parts[k][2] or k == len(toks) - 1 for k in range(len(toks))):
                new = parts[0][0] + right + parts[-1][2]
                if new != " ".join(words[i + k]["w"] for k in range(len(toks))):
                    merged = {**words[i], "w": new, "end": words[i + len(toks) - 1]["end"]}
                    words[i:i + len(toks)] = [merged]
                    n += 1
            i += 1
        pat = re.compile(r"(?<![\w'’])" + r"\s+".join(map(re.escape, wrong.split())) + r"(?![\w])",
                         re.IGNORECASE)
        for s in segments:
            s["text"] = pat.sub(lambda m: right, s["text"])
    return n

def write_transcript(d, language, segments, words):
    """transcript.json (atomic) + transcript.txt. Internal fields (_seg) are not written."""
    out = d / "transcript.json"
    tmp = d / f".transcript.{os.getpid()}.json"
    tmp.write_text(json.dumps({"language": language, "segments": segments,
                               "words": [{k: v for k, v in w.items() if not k.startswith("_")}
                                         for w in words]}, indent=1))
    os.replace(tmp, out)
    (d / "transcript.txt").write_text(
        "\n".join(f"[{fmt_t(s['start'])}] {s['text']}" for s in segments) + "\n")

def fix_transcript(mid, wrong, right, index=None, actor="cli"):
    """Fix a misheard word in one file's transcript: every occurrence of `wrong`, or only the
    word at `index` (the operator double-clicked it). Rewrites transcript.json/.txt and emits
    transcript.edited, so the UI refetches and the next export burns the right word.
    Returns the number of words changed."""
    d = mdir(mid)
    t = load_json(d / "transcript.json", None)
    if not t or not right.strip():
        return 0
    segs, words = t.get("segments", []), t.get("words", [])
    if index is not None:
        if not 0 <= index < len(words):
            return 0
        pre, core, post = _split_word(words[index]["w"])
        new = pre + right.strip() + post
        if new == words[index]["w"]:
            return 0
        w0 = words[index]
        starts = [s["start"] for s in segs]
        si = bisect.bisect_right(starts, w0["start"] + 1e-3) - 1
        if 0 <= si < len(segs):          # the same occurrence of that word in its segment text
            nth = sum(1 for w in words[:index] if w["start"] >= segs[si]["start"] - 1e-3
                      and _split_word(w["w"])[1].lower() == core.lower())
            pat = re.compile(r"(?<![\w'’])" + re.escape(core) + r"(?![\w])", re.IGNORECASE)
            hits = list(pat.finditer(segs[si]["text"]))
            if nth < len(hits):
                h = hits[nth]
                segs[si]["text"] = segs[si]["text"][:h.start()] + right.strip() + segs[si]["text"][h.end():]
        words[index] = {**w0, "w": new}
        n = 1
    else:
        n = apply_fixes(segs, words, [(wrong.strip(), right.strip())])
    if not n:
        return 0
    write_transcript(d, t.get("language"), segs, words)
    emit("transcript.edited", mid, wrong=wrong, right=right.strip(), count=n,
         **({"index": index} if index is not None else {}),
         sha256=receipt(d / "transcript.json"), actor=actor)
    return n

def keyframes(item):
    if not item["has_video"]:
        return {"frames": 0, "skipped": "no video"}
    need("ffmpeg")
    d = mdir(item["id"]) / "keyframes"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    every = max(2.0, item["duration"] / 40)
    # the 720p proxy decodes ~10x faster than a 4K source and has the same timeline
    px = mdir(item["id"]) / "proxy.mp4"
    src = str(px) if px.exists() and "proxy" in item.get("steps", {}) else item["path"]
    ff(["ffmpeg", "-v", "error", "-i", src, "-vf",
        f"fps=1/{every:.3f},scale=320:-2", "-q:v", "5", str(d / "kf_%04d.jpg")],
       item["duration"], Progress("keyframes", item["id"]).part(None))
    frames = sorted(d.glob("kf_*.jpg"))
    index = [{"file": f"keyframes/{f.name}", "t": round(i * every, 2)} for i, f in enumerate(frames)]
    (d / "index.json").write_text(json.dumps(index, indent=1))
    return {"frames": len(frames), "every": round(every, 3)}

MOMENTS_PROMPT = """You are the content strategist for a one-person creative team. A human
will rewrite everything you propose, so your job is to find the strongest raw material and
be honest about it, not to polish it.

BRAND:
{brand}

THE BAR (QUALITY.md): every reel made from your moments must clear it. Aim at it:
{quality}

FOOTAGE: "{name}" — {kind}, {dur} long, {orient}.

TRANSCRIPT (timestamped segments; may be empty for silent screen recordings):
{transcript}

KEYFRAMES: one every {every}s, listed in {kf_index}. Open a handful of the images to
see what is on screen — more if the footage is silent.

Find the MOMENTS worth cutting into short vertical clips: fewer and stronger, usually 1-3,
never more than 6. If nothing here clears the bar, return [] (an empty list is a good answer).
Each moment is a complete thought: it makes sense said alone, with one takeaway, and it starts
at the start of a sentence and ends after its last word (or on a visual change if silent).
15-35s each (shorter is fine if the footage itself is short). Prefer: a concrete claim shown on
screen, a real job-site detail, a moment of friction or surprise, a line that sounds like a
person and not a pitch. Never a fragment picked for the picture. Never invent facts beyond the
brand file and the footage.

Return ONLY a JSON array, no prose:
[{{"start": seconds, "end": seconds, "title": "short working title",
   "hook": "the opening line or on-screen text, in the speaker's own words where possible",
   "pillar": "which brand pillar", "why": "one sentence: why this works",
   "angle": "one of: past-self | admired-thinker | redefine | disagree | everyday-stakes",
   "kind": "talking | demo | walk | b-roll"}}]"""

def moments(item):
    d = mdir(item["id"])
    segs = load_json(d / "transcript.json", {"segments": []})["segments"]
    transcript = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs) or "(none)"
    kf = item["steps"].get("keyframes", {})
    orient = ("portrait" if item.get("height", 0) > item.get("width", 0) else "landscape") \
        if item["has_video"] else "audio only"
    kind = "screen/phone video" if item["has_video"] else "audio"
    prompt = MOMENTS_PROMPT.format(
        brand=brand_text(item), quality=quality_text(), name=item["name"], kind=kind,
        dur=fmt_t(item["duration"]),
        orient=orient, transcript=transcript, every=kf.get("every", "?"),
        kf_index="keyframes/index.json" if kf.get("frames") else "(no keyframes)")
    prov = step_provenance("moments", MOMENTS_PROMPT, moments_model(agent_name()), item)
    ms = parse_json_block(agent(prompt, cwd=d, allow_read=True, model=moments_model(agent_name())))
    dur = item["duration"]
    clean = []
    for m in ms:
        s, e = max(0.0, float(m["start"])), min(dur, float(m["end"]))
        if e - s < 2:
            continue
        # onto real speech edges: the agent picks from whisper's (early) word times
        ns, ne, why = snap_media(item["id"], s, e)
        extra = {"picked": [round(s, 2), round(e, 2)]} if why else {}
        clean.append({**m, "n": len(clean) + 1, "start": round(ns, 2), "end": round(ne, 2), **extra})
    out = d / "moments.json"
    out.write_text(json.dumps(clean, indent=1))
    return {"moments": len(clean), "sha256": receipt(out), "provenance": prov}

def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

CAP_WORDS = 3
CAP_MIN = 0.4        # no caption shows for less: a shorter one joins its neighbour (QUALITY.md `caption stray`)
CAPTION_RULE = 3     # what a render burns, written to render.done so check reads an export by the
                     #   rule it was made with. 1 (before 2026-11-11): no minimum, no speech gate, side
                     #   margins of 1/13; 2: the minimum, the margins, and a gate that could pull a
                     #   short caption up to 0.4 s ahead of a late voice; 3: all of this section
_CAP_BREAK = re.compile(r"[.?!,][\"'”’)\]]*$")
_CAP_STOP = re.compile(r"[.?!][\"'”’)\]]*$")       # the end of a sentence

def caption_chunks(words, segments, start, end, pace=1.0, rule=CAPTION_RULE):
    """THE caption rule (ui/captions.js captionChunks is the same, so the preview matches the
    export): the words of [start, end) in chunks of at most 3, closing a chunk early after a
    word ending in . ? ! or , and at a segment boundary. Each chunk shows from its first
    word until the next chunk starts (the last one until its last word ends), in clip time.
    A word counts if it starts inside the clip, even if whisper gave it no length; that's
    what kept the first word of a clip cut on a sentence start off screen.
    No chunk shows for under CAP_MIN: words of the sentence before or after that the clip's
    edge cuts through (whisper's word times run early) are dropped, and any other short chunk
    joins the one before it (when it ends a phrase, or is the last) or the one after.
    pace: the clip's (times stay in source seconds; CAP_MIN is on the export's clock)."""
    ws = [x for x in words if x["start"] < end and (x["end"] > start or x["start"] >= start)]
    starts = sorted(s["start"] for s in segments or [])
    seg = lambda x: bisect.bisect_right(starts, x["start"] + 1e-3) - 1
    groups, cur = [], []
    for k, x in enumerate(ws):
        cur.append(x)
        nxt = ws[k + 1] if k + 1 < len(ws) else None
        if (nxt is None or len(cur) >= CAP_WORDS or _CAP_BREAK.search(x["w"])
                or seg(nxt) != seg(x)):
            groups.append(cur)
            cur = []
    r3 = lambda v: math.floor(v * 1000 + 0.5) / 1000      # = JS Math.round(v * 1000) / 1000
    its, least = [], (CAP_MIN * pace if rule >= 2 else 0.0)
    hold = least or 0.3
    for i, g in enumerate(groups):
        a = max(g[0]["start"], start) - start
        b = (groups[i + 1][0]["start"] if i + 1 < len(groups) else max(g[-1]["end"], g[-1]["start"] + hold)) - start
        b = min(b, end - start)
        if b - a > 0.01:
            its.append({"g": g, "a": a, "b": b})
    short = lambda x: x["b"] - x["a"] < least - 1e-9
    # every word of it began before the clip did: the tail of the sentence before. (The clip's
    # own first words may begin a little early by whisper's clock: the words after them don't.)
    if its and short(its[0]) and all(w["start"] < start for w in its[0]["g"]):
        its.pop(0)
    # every word ends after the clip does, or it follows the end of a sentence and the clip
    # can't show it for CAP_MIN: the first words of the sentence after
    if its and short(its[-1]) and (all(w["end"] > end for w in its[-1]["g"]) or
                                   (len(its) > 1 and _CAP_STOP.search(its[-2]["g"][-1]["w"]))):
        its.pop()
    i = 0
    while i < len(its):
        x = its[i]
        if not short(x) or len(its) == 1:
            i += 1
        elif i > 0 and (_CAP_BREAK.search(x["g"][-1]["w"]) or i + 1 == len(its)):
            its[i - 1]["g"] = its[i - 1]["g"] + x["g"]
            its[i - 1]["b"] = x["b"]
            its.pop(i)
        else:
            its[i + 1]["g"] = x["g"] + its[i + 1]["g"]
            its[i + 1]["a"] = x["a"]
            its.pop(i)
    return [{"a": r3(x["a"]), "b": r3(x["b"]), "text": " ".join(w["w"] for w in x["g"])} for x in its]

GATE_LEAD = 0.05     # a caption gated to speech appears this long before the voice
GATE_EARLY = 0.15    # or, to stay CAP_MIN on screen, at most this long before it (`caption stray`
                     #   allows 0.20 s on the export's own VAD, which hears edges +-0.04 s)
GATE_HOLD = 0.2      # and stays this long after it stops (never under CAP_MIN on screen)

def caption_gate(chunks, spans, end=None, rule=CAPTION_RULE):
    """Chunks (clip time) held to the speech that is really there. spans: the clip's speech on
    the same clock (part_speech: the media's VAD, not whisper's word times, which run early).
    A chunk that would appear in silence waits for the voice (GATE_LEAD before it); one whose
    words are over and followed by silence goes GATE_HOLD after them. When the voice comes so
    late that the chunk couldn't show for CAP_MIN without appearing more than GATE_EARLY before
    it, the chunk joins the next one; the last chunk holds a little longer instead (to `end`,
    the clip's length, at most), or joins the one before when the voice hardly stopped between
    them. So nothing this returns starts further before the voice than `check` allows. A chunk
    with no speech under it at all is left as it is (`check` calls it stray: a word the clip's
    edge cuts, which a trim fixes). No spans: nothing changes."""
    if not spans:
        return chunks
    if rule < 3:
        return _caption_gate_2(chunks, spans)
    out, todo = [], list(chunks)
    while todo:
        c = todo.pop(0)
        a, b = c["a"], c["b"]
        if not any(r0 - GATE_LEAD <= a < r1 for r0, r1 in spans):
            nxt = min((r0 for r0, r1 in spans if a < r0 < b), default=None)
            if nxt is not None:
                wait = lambda b: max(a, nxt - GATE_EARLY, min(nxt - GATE_LEAD, b - CAP_MIN))
                if b - wait(b) < CAP_MIN - 1e-9:
                    if todo:
                        n = todo.pop(0)
                        todo.insert(0, {**n, "a": max(a, nxt - GATE_LEAD), "text": c["text"] + " " + n["text"]})
                        continue
                    hold = max(b, min(max(a, nxt - GATE_LEAD) + CAP_MIN, end if end is not None else math.inf))
                    said = max((r1 for r0, r1 in spans if r1 <= nxt), default=None)
                    if hold - wait(hold) < CAP_MIN - 1e-9 and out and said is not None \
                            and said > out[-1]["a"] and nxt - said < CAP_QUIET:
                        p = out.pop()
                        todo.insert(0, {**p, "b": b, "text": p["text"] + " " + c["text"]})
                        continue
                    b = hold
                a = wait(b)
        # speech that only starts in the chunk's last tenth of a second is the next chunk's
        ends = [r1 for r0, r1 in spans if r0 < b - 0.1 and r1 > a]
        if ends and max(ends) < b:
            b = min(b, max(max(ends) + GATE_HOLD, a + CAP_MIN))
        out.append({**c, "a": round(a, 3), "b": round(b, 3)} if (a, b) != (c["a"], c["b"]) else c)
    return out

def _caption_gate_2(chunks, spans):
    """The gate as rule 2 burned it (exports of 2026-11-11, before rule 3): a late caption
    starts at min(voice - GATE_LEAD, end - CAP_MIN), however far ahead of the voice that is."""
    out = []
    for c in chunks:
        a, b = c["a"], c["b"]
        if not any(r0 - GATE_LEAD <= a < r1 for r0, r1 in spans):
            nxt = min((r0 for r0, r1 in spans if a < r0 < b), default=None)
            if nxt is not None:
                a = max(a, min(nxt - GATE_LEAD, b - CAP_MIN))
        ends = [r1 for r0, r1 in spans if r0 < b - 0.1 and r1 > a]
        if ends and max(ends) < b:
            b = min(b, max(max(ends) + GATE_HOLD, a + CAP_MIN))
        out.append({**c, "a": round(a, 3), "b": round(b, 3)} if (a, b) != (c["a"], c["b"]) else c)
    return out

def clip_captions(item, a, b, pace=1.0, rule=CAPTION_RULE):
    """The captions a clip of item's a-b burns, on the clip's own clock (after its pace): the
    caption rule's chunks, gated to the media's speech. What the render draws and check reads."""
    tr = load_json(mdir(item["id"]) / "transcript.json", {"words": []})
    chunks = paced(caption_chunks(tr.get("words", []), tr.get("segments"), a, b, pace, rule), pace)
    if rule < 2:
        return chunks
    regs = speech_regions(item["id"]) or []
    if rule < 3:
        return caption_gate(chunks, part_speech({"in": a, "out": b, "pace": pace}, regs, (b - a) / pace * FPS), rule=rule)
    n = frames_of((b - a) / pace)
    return caption_gate(chunks, part_speech({"in": a, "out": b, "pace": pace}, regs, n), n / FPS)

def captions(words, start, end, path, w=1080, h=1920, segments=None):
    """Burn-in captions (ASS): caption_chunks(), big and centred low — the vertical idiom."""
    return write_ass(caption_chunks(words, segments, start, end), path, w, h)

CAP_SIDE = 0.15      # captions keep to the middle 70% of the width: clear of the platform's
                     #   buttons on the right (QUALITY.md safe zone), and centred, so the same on the left

def write_ass(chunks, path, w=1080, h=1920, rule=CAPTION_RULE):
    """chunks ({a, b, text}, in segment time) as an ASS file; returns how many."""
    size = round(min(w, h) * 0.08)
    line = max(3, size // 14)                          # the outline, which sits outside the margins
    side = round(w * CAP_SIDE) + line if rule >= 2 else w // 13
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
             "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
             f"Style: Cap,Helvetica Neue,{size},&H00FFFFFF,&H00000000,&H64000000,1,1,"
             f"{line},0,2,{side},{side},{round(h * 0.27)}",
             "", "[Events]", "Format: Layer, Start, End, Style, Text"]
    for c in chunks:
        text = c["text"].replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ass_time(c['a'])},{ass_time(c['b'])},Cap,{text}")
    path.write_text("\n".join(lines) + "\n")
    return len(chunks)

def stream_sig(path):
    """Everything about a part that has to match for a lossless concat."""
    out = json.loads(sh(["ffprobe", "-v", "error", "-print_format", "json", "-show_data_hash",
                         "sha256", "-show_streams", str(path)]))
    keys = ("codec_name", "profile", "level", "width", "height", "pix_fmt", "r_frame_rate",
            "time_base", "sample_rate", "channels", "extradata_hash")
    # a timecode (tmcd) or other data track isn't joined: parts never carry one (NO_TMCD), and
    # if one did it must not force a re-encode of the whole join
    return json.dumps([{k: st.get(k) for k in keys} for st in out["streams"]
                       if st.get("codec_type") in ("video", "audio")], sort_keys=True)

# Parts carry no metadata and no timecode track. DJI sources have one (tmcd) and derived media
# don't; copied into some parts and not others it made their streams differ, so the join was
# re-encoded and every cut landed on a predicted frame (a soft first frame).
NO_TMCD = ["-map_metadata", "-1", "-write_tmcd", "0"]

def part_audio(part):
    """A part's sound: PCM in the intermediate parts (sample-exact, so a part's sound lasts
    exactly as long as its picture and the join has nothing to hold), AAC in a finished file."""
    return ["-c:a", "pcm_s16le"] if part else ["-c:a", "aac", "-b:a", "160k"]

def frame_rate(pace=1.0):
    """The picture onto the export's frame grid: from 0 (a seek can leave the first frame at
    1/30 s), at the clip's pace, padded by cloning the last frame so -frames:v always has N."""
    return (f"setpts=(PTS-STARTPTS)/{pace:g}," if pace != 1.0 else "setpts=PTS-STARTPTS,") + \
        f"fps={FPS},tpad=stop_mode=clone:stop_duration=1"

def frame_filter(w, h):
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"

def fit_dims(sw, sh, w, h):
    """'fit' layout: the whole source scaled to fit inside w x h (fg), and the size of a small
    cover-scaled copy (bw x bh, cropped to cw x ch) that is blurred and stretched behind it."""
    fw, fh = (w, even(w * sh / sw)) if sw / sh >= w / h else (even(h * sw / sh), h)
    fw, fh = min(w, fw), min(h, fh)
    cw, ch = even(w / 8), even(h / 8)
    bw, bh = (even(ch * sw / sh), ch) if sw / sh >= cw / ch else (cw, even(cw * sh / sw))
    return fw, fh, max(bw, cw), max(bh, ch), cw, ch

def fit_graph(w, h, sw, sh, pre_bg, pre_fg, post):
    """Filtergraph text for 'fit': split, blurred cover copy behind, whole frame on top.
    pre_bg/pre_fg(width, height) give each branch's scale (+ download) chain."""
    fw, fh, bw, bh, cw, ch = fit_dims(sw, sh, w, h)
    return (f"split[fs0][fs1];"
            f"[fs0]{pre_bg(bw, bh)},crop={cw}:{ch},format=yuv420p,boxblur=6:2,scale={w}:{h},format=nv12,setsar=1[fbg];"
            f"[fs1]{pre_fg(fw, fh)},setsar=1[ffg];"
            f"[fbg][ffg]overlay=(W-w)/2:(H-h)/2{post}")

FADE = 0.02          # seconds of audio fade at every cut (QUALITY.md: 10-30 ms)

def render_segment(item, a, b, out, aspect="9:16", with_captions=True, on_frac=None, fit=None,
                   chunks=None, audio=True, voice="off", pace=1.0, frames=None, part=False):
    """One clip of source media, cropped to aspect, captions burned in, stereo 48k audio
    (silence if the source has none) so segments concat losslessly. On Apple silicon the
    decode and the cover-scale run on the media engine/GPU; only the cropped output-size frame
    comes to the CPU for libass; encode is VideoToolbox. Every segment gets identical encoder
    settings, whichever decode path it took. chunks: the captions to burn (segment time)
    instead of the item's own words (a cover piece shows the sound's captions); audio=False:
    picture only; voice: a VOICE_CHAINS treatment of the sound, before the fades; pace: play
    a-b this much faster (picture and sound; chunks are then on the sped-up clock). frames:
    exactly this many export frames (default (b - a) / pace, rounded), sound exactly as long;
    part: an intermediate part of a render (PCM sound, see part_audio)."""
    w, h = ASPECTS[aspect]
    fit = fit if fit in FITS else fit_default(item, aspect)
    ass = out.with_suffix(".ass")
    if chunks is None:
        chunks = clip_captions(item, a, b, pace) if with_captions else []
    n_caps = with_captions and write_ass(chunks, ass, w, h)
    subs = f",subtitles={ass.name}:fontsdir=/System/Library/Fonts" if n_caps else ""
    sw_fmt = "nv12" if hw()["enc"] else "yuv420p"
    vi = vinfo(item["path"])
    # cover crop in the (post-transpose) source's own pixels: crop on the GPU first, then
    # scale straight to the output size. Scaling first (to e.g. 3414x1920 for a landscape
    # source going 9:16) and cropping on the CPU moved ~3x the pixels through hwdownload and
    # the 10-bit -> 8-bit conversion; crop-then-scale measured ~2.5x faster on iPhone HLG.
    sw_, sh_ = item["width"], item["height"]
    crop_w = min(sw_, even(min(sw_, sh_ * w / h)))
    crop_h = min(sh_, even(min(sh_, sw_ * h / w)))
    gpu_crop = f"crop={crop_w}:{crop_h}" if (crop_w, crop_h) != (sw_, sh_) else None
    n = frames if frames is not None else frames_of((b - a) / pace)
    # a short fade in and out on every part's audio: no click where two parts meet
    fades = sound_filter(n, voice, pace, (b - a) / pace)
    rate = frame_rate(pace)

    def build(use_hw):
        down = "hwdownload,format=p010le,format=nv12" if vi["tenbit"] else "hwdownload,format=nv12"
        if fit == "fit":
            # both branches scale on the GPU (and convert HDR there); only the small blurred
            # copy and the fitted frame come down to the CPU for the overlay and captions
            if use_hw:
                rot = vt_transpose(vi["theta"])
                vf = (f"{rot}," if rot else "") + fit_graph(
                    w, h, sw_, sh_, lambda x, y: f"{vt_scale(x, y, vi)},{down}",
                    lambda x, y: f"{vt_scale(x, y, vi)},{down}", f",{rate}")
            else:
                vf = fit_graph(w, h, sw_, sh_, lambda x, y: f"scale={x}:{y}",
                               lambda x, y: f"scale={x}:{y}", f",{rate}")
        elif use_hw:
            vf = ",".join(x for x in (vt_transpose(vi["theta"]), gpu_crop, vt_scale(w, h, vi),
                                      down, f"setsar=1,{rate}") if x)
        else:
            vf = frame_filter(w, h) + f",{rate}"
        vf += subs + f",{SETPARAMS},format={sw_fmt}"
        cmd = ["ffmpeg", "-y", "-v", "error", *_dec(use_hw), "-ss", str(a), "-to", str(b),
               "-i", item["path"]]
        if not audio:
            return cmd + ["-vf", vf, "-map", "0:v:0", "-an", *venc("export", 60), "-frames:v", str(n),
                          *NO_TMCD, "-movflags", "+faststart", out.name]
        if item["has_audio"]:
            amap = ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-t", str(b - a), "-i", f"anullsrc=r={SR}:cl=stereo"]
            amap = ["-map", "0:v:0", "-map", "1:a:0"]
        return cmd + ["-vf", vf, *amap, "-af", fades, *venc("export", 60), "-frames:v", str(n),
                      *part_audio(part), "-ar", str(SR), "-ac", "2", *NO_TMCD, "-movflags",
                      "+faststart", out.name]

    try:
        _with_hw_fallback(build, lambda cmd: ff(cmd, n / FPS, on_frac, cwd=out.parent))
    finally:
        ass.unlink(missing_ok=True)

def sound_filter(frames, voice="off", pace=1.0, length=None):
    """A part's audio filter for `frames` export frames of sound: the pace (atempo keeps the
    pitch), the voice treatment (if any), a short fade in and out (no click where two parts
    meet), then exactly frames * SR / FPS samples: padded with silence or cut, never longer
    than the picture (a longer sound made the join hold the last frame before every cut).
    length: how long the sound really is on the export's clock ((out - in) / pace). The fade
    out ends there, before any padding: a clip rounded up a few ms must not stop mid-fade."""
    d = frames / FPS
    end = min(d, length) if length else d
    fd = min(FADE, end / 4)
    chain = ["asetpts=PTS-STARTPTS"] + ([f"atempo={pace:g}"] if pace != 1.0 else []) + [f"aresample={SR}"]
    chain += [VOICE_CHAINS[voice]] if voice in VOICE_CHAINS else []
    chain += [f"afade=t=in:d={fd:.3f}", f"afade=t=out:st={max(0.0, end - fd):.4f}:d={fd:.3f}",
              "apad", f"atrim=end_sample={frames * SR // FPS}"]
    return ",".join(chain)

def paced(chunks, pace):
    """Caption chunks moved onto a clip's sped-up clock."""
    if pace == 1.0:
        return chunks
    r3 = lambda v: math.floor(v * 1000 + 0.5) / 1000
    return [{**c, "a": r3(c["a"] / pace), "b": r3(c["b"] / pace)} for c in chunks]

def chunks_within(chunks, o0, o1):
    """Caption chunks (clip time) clipped to [o0, o1) and moved to start at 0."""
    out = []
    for c in chunks:
        a, b = max(c["a"], o0), min(c["b"], o1)
        if b - a > 0.01:
            out.append({**c, "a": round(a - o0, 3), "b": round(b - o0, 3)})
    return out

def render_covered(st, clip, out, aspect="9:16", with_captions=True, on_frac=None, voice="off"):
    """A clip with cover: its picture stretch by stretch as cover_plan says (its own picture, or
    a cover piece's footage, each fitted like any clip, the sound's captions burned across all
    of them), joined, then the clip's own sound laid under it in one piece: cover never touches
    what's heard. out is an intermediate part (.mov, PCM sound), exactly clip_frames(clip)
    frames long: every stretch is whole frames, and a piece never asks for more footage
    frames than its file has."""
    item = st["media"][clip["media"]]
    a, b, pace = clip["in"], clip["out"], clip_pace(clip)
    chunks = clip_captions(item, a, b, pace) if with_captions else []
    spans, _ = cover_plan(clip, st["media"])
    n = clip_frames(clip)
    pics, done = [], 0
    try:
        for k, (f0, f1, p, t0) in enumerate(spans):
            pic = out.with_name(f"{out.stem}.pic{k:02d}.mov")
            src = st["media"][p["media"]] if p else item
            fit = (p.get("fit") if p.get("fit") in FITS else fit_default(src, aspect)) if p \
                else clip_fit(clip, item, aspect)
            prog = (lambda f, d=done, m=f1 - f0: on_frac((d + f * m) / n)) if on_frac else None
            # the clip's own picture runs at its pace; a cover piece at normal speed, over the
            # same stretch of the (sped-up) sound
            secs = (f1 - f0) / FPS * (pace if not p else 1)
            render_segment(src, t0, t0 + secs, pic, aspect, with_captions,
                           prog, fit, chunks=chunks_within(chunks, f0 / FPS, f1 / FPS), audio=False,
                           pace=1.0 if p else pace, frames=f1 - f0, part=True)
            pics.append(pic)
            done += f1 - f0
        lst = out.with_name(f"{out.stem}.pics.txt")
        lst.write_text("".join(f"file '{x.name}'\n" for x in pics))
        pics.append(lst)
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name,
               "-ss", str(a), "-to", str(b), "-i", item["path"]]
        if item["has_audio"]:
            amap = ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-f", "lavfi", "-t", str(b - a), "-i", f"anullsrc=r={SR}:cl=stereo"]
            amap = ["-map", "0:v:0", "-map", "2:a:0"]
        sh(cmd + [*amap, "-c:v", "copy", "-frames:v", str(n), "-af", sound_filter(n, voice, pace, (b - a) / pace),
                  *part_audio(True), "-ar", str(SR), "-ac", "2", *NO_TMCD, out.name], cwd=out.parent)
    finally:
        for x in pics:
            x.unlink(missing_ok=True)

def cut(item):
    if not item["has_video"]:
        return {"clips": 0, "skipped": "no video"}
    need("ffmpeg")
    d = mdir(item["id"])
    ms = load_json(d / "moments.json", [])
    cd = d / "clips"
    shutil.rmtree(cd, ignore_errors=True)
    cd.mkdir()
    prog = Progress("cut", item["id"], weights={m["n"]: m["end"] - m["start"] for m in ms} or None)
    ensure_speech(item["id"])
    def one(m):
        out = cd / f"{m['n']:02d}.mp4"
        a, b, _ = snap_media(item["id"], m["start"], m["end"])   # moments from before snapping
        render_segment(item, a, b, out, on_frac=prog.part(m["n"]))
        return {"n": m["n"], "file": f"clips/{out.name}", "sha256": receipt(out)}
    with ThreadPoolExecutor(render_pool_size()) as ex:
        made = list(ex.map(spawn_ctx(one), ms))
    return {"clips": len(made), "files": made}

DRAFT_PROMPT = """You draft social posts for a one-person creative team. The human WILL rewrite
every word in their own voice, so give them strong, honest raw drafts, not polish. No
hashtag spam, no emoji walls, no "game-changer", nothing the brand file says never to claim.

BRAND:
{brand}

FOOTAGE: "{name}". Full transcript is in transcript.txt (Read it if you need context).

MOMENTS (already cut into vertical clips):
{moments}

For EACH moment, write:
- "short": caption for a vertical clip (TikTok/Reels/Shorts), with the first line as the hook, at most 3 lines
- "x": one X post (at most 280 characters) that stands on its own without the video
- "linkedin": 4-8 short lines, founder-led, concrete
- "title": a YouTube Shorts title (at most 60 characters)

Then, across all the moments, write a "newsletter" paragraph (90-140 words) and three
"ideas": follow-up posts this footage suggests, each with an angle.

Return ONLY JSON: {{"posts": [{{"n": moment_number, "short": "...", "x": "...",
"linkedin": "...", "title": "..."}}], "newsletter": "...", "ideas": ["...", "...", "..."]}}"""

def draft(item):
    d = mdir(item["id"])
    ms = load_json(d / "moments.json", [])
    words = load_json(d / "transcript.json", {"words": []})["words"]
    lines = []
    for m in ms:
        said = " ".join(w["w"] for w in words if m["start"] <= w["start"] < m["end"])
        lines.append(f"#{m['n']} [{fmt_t(m['start'])}-{fmt_t(m['end'])}] {m['title']} — "
                     f"hook: {m['hook']} — why: {m['why']}\n    said: {said or '(silent)'}")
    prov = step_provenance("draft", DRAFT_PROMPT, None, item)
    result = agent(DRAFT_PROMPT.format(brand=brand_text(item), name=item["name"],
                                       moments="\n".join(lines)), cwd=d, allow_read=True)
    drafts = parse_json_block(result)
    out = d / "drafts.json"
    out.write_text(json.dumps(drafts, indent=1))
    return {"posts": len(drafts.get("posts", [])), "sha256": receipt(out), "provenance": prov}

STEP_FN = {"proxy": proxy, "speech": speech, "transcribe": transcribe, "keyframes": keyframes,
           "moments": moments, "cut": cut, "draft": draft}
STEP_NEEDS = {"cut": "moments", "draft": "moments"}

_step_locks, _step_locks_guard = {}, threading.Lock()

def _step_lock(mid, step):
    """Two jobs asking for the same step on the same file run it once, not twice at once
    (they would write the same output files)."""
    with _step_locks_guard:
        return _step_locks.setdefault((mid, step), threading.Lock())

def run_step(step, ids, force=False, quiet=False):
    lib = library()
    for mid in ids or list(lib):
        if mid not in lib:
            print(f"  ? {mid} not in library"); continue
        with _step_lock(mid, step):
            _run_one(step, mid, force, quiet)

def _run_one(step, mid, force, quiet):
    item = library().get(mid)
    if step in item["steps"] and not force:
        return
    if STEP_NEEDS.get(step) and STEP_NEEDS[step] not in item["steps"]:
        run_step(STEP_NEEDS[step], [mid], quiet=quiet)
        item = library()[mid]
    if not quiet:
        print(f"  {step:<10} {mid}  {item['name']}", flush=True)
    try:
        data = STEP_FN[step](item)
    except Exception as e:  # one bad file shouldn't sink the batch
        emit("step.failed", mid, step=step, error=str(e)[:2000])
        print(f"    failed: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
        return
    emit(f"step.{step}", mid, **data)


# ---------- pipeline: lanes ------------------------------------------------
# Each step runs in the lane of the hardware it saturates, so a long transcription never
# holds up a proxy for a file that was just dropped in:
#   media   ffmpeg / VideoToolbox (proxy, keyframes, cut)   STUDIO_MEDIA_LANE (2)
#   whisper Metal GPU, one at a time (speech, transcribe)    1
#   ai      agent calls (moments, draft)                     STUDIO_AI_LANE (3)
# Per file: proxy -> keyframes runs alongside speech -> transcribe; moments waits for both; then cut
# and draft run side by side.

STEP_LANE = {"proxy": "media", "keyframes": "media", "cut": "media", "speech": "whisper",
             "transcribe": "whisper", "moments": "ai", "draft": "ai"}

class Lane:
    """A counting semaphore that hands out slots in arrival order (so the first file
    dropped becomes playable first) and lets one holder take several slots at once (a
    VideoToolbox-decoded 4K/HDR proxy saturates the media engine on its own: sharing it
    with a second proxy only halves both). Lower `prio` jumps the queue (cheap steps that
    unblock the operator, e.g. keyframes from a finished 720p proxy)."""
    def __init__(self, n):
        self.cap = max(1, int(n))
        self.free = self.cap
        self.cv = threading.Condition()
        self.queue = []                 # [(prio, seq)] waiting, served smallest first
        self.seq = 0

    def acquire(self, weight=1, prio=1):
        w = max(1, min(int(weight), self.cap))
        with self.cv:
            self.seq += 1
            me = (prio, self.seq)
            self.queue.append(me)
            while min(self.queue) != me or self.free < w:
                self.cv.wait()
            self.queue.remove(me)
            self.free -= w
            self.cv.notify_all()
        return w

    def release(self, weight=1):
        with self.cv:
            self.free = min(self.cap, self.free + max(1, min(int(weight), self.cap)))
            self.cv.notify_all()

    def hold(self, weight=1, prio=1):
        lane = self
        class _Hold:
            def __enter__(self_):
                self_.w = lane.acquire(weight, prio)
                return self_
            def __exit__(self_, *exc):
                lane.release(self_.w)
        return _Hold()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    @property
    def busy(self):
        with self.cv:
            return self.cap - self.free

def make_lanes():
    return {"media": Lane(os.environ.get("STUDIO_MEDIA_LANE", 2)),
            "whisper": Lane(1),
            "ai": Lane(os.environ.get("STUDIO_AI_LANE", 3)),
            # exports get their own lane so they never queue behind background proxies
            "export": Lane(1)}

LANES = make_lanes()
_exporting = [0]          # renders in flight (proxies chunk less while one runs)

def step_weight(step, mid, lanes):
    """Slots of its lane a step takes: a VT-decoded proxy (4K / 10-bit HDR) takes the whole
    media lane; everything else takes one."""
    if step == "proxy":
        it = library().get(mid)
        if it and it.get("has_video") and "proxy" not in it["steps"]:
            try:
                if prefer_hw_decode(it):
                    return lanes["media"].cap
            except Exception:
                pass
    return 1

def audio_wav(item):
    """16 kHz mono WAV for whisper, extracted once (atomically) and reused. Cheap CPU work,
    so the pipeline does it *before* taking the single whisper slot."""
    wav = mdir(item["id"]) / "audio.wav"
    if not wav.exists():
        tmp = wav.with_name(f".audio.{os.getpid()}.{threading.get_ident()}.wav")
        sh(["ffmpeg", "-y", "-v", "error", "-i", item["path"], "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", str(tmp)])
        os.replace(tmp, wav)
    return wav

def run_pipeline(ids, steps, force=False, quiet=False, lanes=None, on_lane=None):
    """run_step for every (id, step), in dependency order, each step inside its lane.
    Same skip/force/needs semantics as run_step; on_lane(step, mid) fires when a step gets
    its lane (the server uses it to mark a job started)."""
    lanes = lanes or LANES
    want = [s for s in STEPS if s in steps]
    ids = list(ids) if ids else list(library())

    def go(step, mid):
        if step not in want and not (step == "moments" and {"cut", "draft"} & set(want)):
            return
        if step in ("speech", "transcribe"):   # WAV extraction outside the whisper slot
            it = library().get(mid)
            if it and it.get("has_audio") and (force or step not in it["steps"]):
                need("ffmpeg")
                try:
                    audio_wav(it)
                except Exception:
                    pass                  # transcribe() retries and reports the failure
        lane = lanes[STEP_LANE[step]]
        hold = lane.hold(step_weight(step, mid, lanes), prio=0 if step == "keyframes" else 1) \
            if isinstance(lane, Lane) else lane
        with hold:
            if on_lane:
                on_lane(step, mid)
            run_step(step, [mid], force=force and step in want, quiet=quiet)

    def parallel(*fns):
        ts = [threading.Thread(target=spawn_ctx(f)) for f in fns]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    def one(mid):
        parallel(lambda: (go("proxy", mid), go("keyframes", mid)),
                 lambda: (go("speech", mid), go("transcribe", mid)))
        go("moments", mid)
        parallel(lambda: go("cut", mid), lambda: go("draft", mid))

    parallel(*[(lambda m=m: one(m)) for m in ids])

# ---------- render ----------------------------------------------------------

def _render_parts(slug, st, tl, work, stamp):
    prog = Progress("render", None, weights={i: clip_len(c) for i, c in enumerate(tl["clips"])},
                    project=slug)
    def one(i):
        c = tl["clips"][i]
        item = st["media"][c["media"]]
        out = work / f"part{i:03d}.mov"
        spoken = bool(item.get("has_audio")) and clip_spoken(c, ensure_speech(c["media"]) or [])
        voice = clip_voice(c, tl, spoken)
        if any(p for _, _, p, _ in cover_plan(c, st["media"])[0]):
            render_covered(st, c, out, tl["aspect"], tl["captions"], prog.part(i), voice)
        else:
            render_segment(item, c["in"], c["out"], out, tl["aspect"], tl["captions"],
                           on_frac=prog.part(i), fit=clip_fit(c, item, tl["aspect"]), voice=voice,
                           pace=clip_pace(c), frames=clip_frames(c), part=True)
        return out
    t0 = time.monotonic()
    _exporting[0] += 1
    try:
        with ThreadPoolExecutor(render_pool_size()) as ex:
            parts = list(ex.map(spawn_ctx(one), range(len(tl["clips"]))))
    finally:
        _exporting[0] -= 1
    info = level_parts(parts, tl, st["media"], work)
    (work / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in parts))
    # concat -c copy is only safe when every part has the same codec parameters (SPS/PPS);
    # if a part ever differs (e.g. VideoToolbox fell back to its software encoder), re-encode,
    # with a key frame at every part's first frame so no cut lands on a predicted frame.
    same = len({stream_sig(p) for p in parts}) == 1
    exports = pdir(slug) / "exports"
    exports.mkdir(exist_ok=True)
    final = exports / f"{slug}-{stamp}-{tl['aspect'].replace(':', 'x')}.mp4"
    # half a frame early: the key frame goes on the first frame at or after each time
    starts = [f"{max(0.0, x - 0.5) / FPS:.6f}"
              for x in itertools.accumulate([0] + [clip_frames(c) for c in tl["clips"][:-1]])]
    vcodec = ["-c:v", "copy"] if same else [*venc("export", 60), "-pix_fmt", "yuv420p",
                                            "-force_key_frames", ",".join(starts)]
    joined = work / "joined.mov"
    def join():
        sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
            *vcodec, "-c:a", "copy", *NO_TMCD, str(joined)], cwd=work)
    join()
    meas, samples = loudness_of(joined, hear=True)
    # never silence: what `dead air` would fail in this sound at the master's gain gets room
    # tone under it now (sound only, part by part), and the parts are joined again. (Tone sits
    # 20 dB or more under the voices: it moves neither the loudness nor the peaks just measured.)
    if info and meas and lay_tone(parts, info, tl, st["media"], work, samples, LOUD_I - meas["input_i"]):
        join()
    loud = loudnorm(joined, final, meas)
    return final, same, t0, loud, level_report(info)

# ---------- level: every voice at one level, and never digital silence --------------------
# Before the master, each part's sound gets one gain: a spoken clip's so its speech (the
# stretches its media's VAD calls speech) sits at LEVEL_I, any other clip's the median of
# those (natural sound keeps its place against the voices). Peaks that would not fit under the
# master's ceiling are held here, clip by clip, by a limiter (never clipped), and a voice is
# made up what that took: so the master is a plain gain, and every voice leaves it at one
# level. Then the joined sound is measured the way
# `dead air` measures the export (at the master's gain): every part a silent stretch touches
# gets room tone mixed under its own sound. A spoken clip gives its own (the room between its
# words); any other takes the nearest clip's: its tail if the source runs on without words,
# else its longest stretch between words. A donor whose tone is itself silent is passed over
# for the next; when no clip has any, a faint noise floor stands in. Sound only: every part
# keeps its frames, and its 1600 samples a frame.

LEVEL_I = -23.0                 # LUFS, speech-gated, per spoken clip (the master sets the reel's level)
LEVEL_MIN_SPEECH = 0.5          # less speech than this in a clip can't be measured: it rides the median
LEVEL_NEAR = 0.15               # a limited clip's speech is brought back this close to LEVEL_I (LU)
TONE_UNDER = (34.0, 20.0)       # tone under a clip sits this far under LEVEL_I (RMS dB): at
                                #   least / at most. Under a -14 LUFS master: about -48 to -34 dBFS
TONE_CLEAR = 0.15               # tone is taken this far from any word
TONE_MIN = 0.4                  # the shortest stretch worth looping
TONE_TAIL = 2.0                 # a shot's own tail is used when it runs this long (or the whole clip)
TONE_DB, TONE_SECS = 3.0, 0.1   # tone goes under anything within this of failing `dead air`: 3 dB
                                #   over its line, 0.1 s short of its length (the export is AAC, and
                                #   check hears it again)
TONE_FLOOR = 8.0                # and sits at least this far over the line in the export (dB)
TONE_SHARE = 0.1                # a part with less than this of a silent stretch is left alone (the
                                #   20 ms fade before a card): two such, back to back, are still
                                #   under TONE_SECS short of the rule

def leveling():
    return os.environ.get("STUDIO_LEVEL", "1") != "0"

def part_speech(clip, regions, frames=None):
    """The clip's speech on its own clock (0 = its first frame, after its pace): [(a, b)]."""
    pace, n = clip_pace(clip), (frames if frames is not None else clip_frames(clip)) / FPS
    out = []
    for r0, r1 in regions or []:
        a, b = max(0.0, (max(r0, clip["in"]) - clip["in"]) / pace), min(n, (min(r1, clip["out"]) - clip["in"]) / pace)
        if b - a > 0.05:
            out.append((round(a, 3), round(b, 3)))
    return out

def gated_loudness(path, spans):
    """Integrated loudness (LUFS) of only the stretches `spans` [(a, b)] of a file's sound: its
    speech-gated loudness when they are its speech. None if there's too little to measure."""
    if sum(b - a for a, b in spans) < LEVEL_MIN_SPEECH:
        return None
    sel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                        f"aselect='{sel}',asetpts=N/SR/TB,ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"I:\s+(-?[\d.]+) LUFS", r.stderr.rpartition("Summary:")[2])
    v = float(m.group(1)) if r.returncode == 0 and m else None
    return v if v is not None and v > -69 else None

def heard(path):
    """A file's sound the way `check` hears an export (16-bit mono at 16 kHz): (samples, rate).
    What `dead_air` reads."""
    import array
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}):\n{r.stderr.decode(errors='replace')[-1500:]}")
    return array.array("h", r.stdout[:len(r.stdout) // 2 * 2]), 16000

def nearly_dead(samples, sr, gain=0.0):
    """`dead_air` with a margin: what would be silent `gain` dB up, or within TONE_DB /
    TONE_SECS of it."""
    return dead_air(samples, sr, DEAD_DB + TONE_DB - gain, DEAD_SECS - TONE_SECS)

def tone_needed(dead, at):
    """Which parts need tone: {part index: seconds of it that are silent}. dead: the silent
    stretches [(a, b)] of the joined sound; at: where each part starts, and the last ends
    (seconds). A part needs it when TONE_SHARE of one stretch or more lies in it."""
    out = {}
    for i, (t0, t1) in enumerate(zip(at, at[1:])):
        shares = [max(0.0, min(b, t1) - max(a, t0)) for a, b in dead]
        if max(shares, default=0.0) >= TONE_SHARE - 1e-9:
            out[i] = round(sum(shares), 3)
    return out

def part_levels(part, spans=None):
    """One pass over a part's sound: (the speech-gated loudness of `spans`, as gated_loudness;
    its loudest sample in dBFS). None for either that can't be measured."""
    gate = bool(spans) and sum(b - a for a, b in spans) >= LEVEL_MIN_SPEECH
    sel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans or [])
    graph = (f"[0:a:0]asplit[p0][s0];[p0]volumedetect[p];[s0]aselect='{sel}',asetpts=N/SR/TB,ebur128=framelog=verbose[s]"
             if gate else "[0:a:0]volumedetect[p]")
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(part), "-filter_complex", graph,
                        "-map", "[p]", "-f", "null", "-", *(["-map", "[s]", "-f", "null", "-"] if gate else [])],
                       capture_output=True, text=True)
    m = re.search(r"I:\s+(-?[\d.]+) LUFS", r.stderr.rpartition("Summary:")[2]) if gate else None
    v = float(m.group(1)) if r.returncode == 0 and m else None
    pk = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
    return (v if v is not None and v > -69 else None), (float(pk.group(1)) if r.returncode == 0 and pk else None)

def tone_span(clip, item, regions, after=True, need=TONE_TAIL):
    """Where room tone for a neighbour of `clip` comes from in its media: (start, end) in source
    seconds, clear of every word. after: the clip's own tail when the source runs on without
    words for `need` seconds (TONE_TAIL at most: the shot's sound simply continues); else the
    longest stretch between words (the nearest to the clip on a tie). None if the file is
    words wall to wall."""
    dur = item.get("duration") or clip["out"]
    edges = [0.0] + [x for r in sorted(regions or []) for x in r] + [dur]
    gaps = [(a + (TONE_CLEAR if k else 0), b - (TONE_CLEAR if k < len(edges) // 2 - 1 else 0))
            for k, (a, b) in enumerate(zip(edges[::2], edges[1::2]))]
    gaps = [(a, b) for a, b in gaps if b - a >= TONE_MIN]
    if after:
        g = next(((a, b) for a, b in gaps if a <= clip["out"] and b - clip["out"] >= min(need, TONE_TAIL)), None)
        if g:
            return clip["out"], g[1]
    near = lambda g: min(abs(g[0] - clip["out"]), abs(g[1] - clip["in"]))
    return max(gaps, key=lambda g: (round(min(g[1] - g[0], 4.0), 1), -near(g)), default=None)

def level_filter(gain, peak=None):
    """A part's levelling filter: `gain` dB, and a limiter when that would lift its loudest
    sample (`peak`, dBFS) past LEVEL_CEIL, where the master's one gain would put it over the
    export's true-peak ceiling (and long before the 16-bit parts would clip). (True, filter)
    when it limits. At 192 kHz, as the master's: it sees the peaks between samples."""
    if peak is None or peak + gain <= LEVEL_CEIL:
        return False, f"volume={gain:.2f}dB"
    return True, (f"volume={gain:.2f}dB,aresample=192000,alimiter=limit={10 ** (LEVEL_CEIL / 20):.4f}:level=false"
                  f":attack=5:release=50:latency=true,aresample={SR}")

def level_part(part, work, frames, gain, peak, spans=None):
    """Give a part its gain in place, its peaks held under LEVEL_CEIL. Holding peaks takes a
    little loudness with them, so a spoken part (spans: its speech, which `gain` was to bring
    to LEVEL_I) is measured again and made up, three times at most: it ends within LEVEL_NEAR of
    LEVEL_I. Returns (the gain it got in all, dB; whether its peaks were held)."""
    total, held = 0.0, False
    for n in range(4):
        limited, af = level_filter(gain, peak)
        if abs(gain) < 0.005 and not limited:
            break
        resound(part, work, frames, af)
        total, held = total + gain, held or limited
        if not (limited and spans) or n == 3:
            break
        lufs, peak = part_levels(part, spans)
        if lufs is None or abs(LEVEL_I - lufs) <= LEVEL_NEAR:
            break
        gain = LEVEL_I - lufs
    return round(total, 2), held

def resound(part, work, frames, af=None, graph=None, extra=()):
    """Rewrite a part's sound in place, its picture copied: through the filter `af`, or a
    filter graph ending in [o] (inputs: the part, then `extra`). Exactly frames * SR / FPS
    samples come out, as went in."""
    cut = f"apad,atrim=end_sample={frames * SR // FPS}"
    new = part.with_name(part.stem + ".lv.mov")
    sound = ["-filter_complex", f"{graph},{cut}[oo]", "-map", "0:v:0", "-map", "[oo]"] if graph else \
            ["-map", "0:v:0", "-map", "0:a:0", "-af", f"{af},{cut}"]
    sh(["ffmpeg", "-y", "-v", "error", "-i", part.name, *[x for e in extra for x in ("-i", str(e))], *sound,
        "-c:v", "copy", *part_audio(True), "-ar", str(SR), "-ac", "2", *NO_TMCD, new.name], cwd=work)
    new.replace(part)

def level_parts(parts, tl, lib, work):
    """Level the rendered parts in place (see above). Returns what it knows of each clip, in
    order: {clip, gain (dB), speech (LUFS it measured, spoken clips), peak, limited, regs, spoken}
    (lay_tone adds tone / floor; level_report is what render.done carries); [] when
    levelling is off."""
    if not leveling():
        return []
    def measure(k):
        c, item = tl["clips"][k], lib[tl["clips"][k]["media"]]
        regs = (ensure_speech(c["media"]) if item.get("has_audio") else []) or []
        spoken = bool(item.get("has_audio")) and clip_spoken(c, regs)
        lufs, peak = part_levels(parts[k], part_speech(c, regs) if spoken else None)
        return {"clip": c["id"], "speech": lufs, "peak": peak, "regs": regs, "spoken": spoken}
    def apply(k):
        x, c = info[k], tl["clips"][k]
        x["gain"], x["limited"] = level_part(parts[k], work, clip_frames(c), x["gain"], x["peak"],
                                             part_speech(c, x["regs"]) if x["speech"] is not None else None)
    with ThreadPoolExecutor(render_pool_size()) as ex:
        info = list(ex.map(spawn_ctx(measure), range(len(parts))))
        gains = sorted(LEVEL_I - x["speech"] for x in info if x["speech"] is not None)
        med = gains[len(gains) // 2] if gains else 0.0
        for x in info:
            x["gain"] = round(LEVEL_I - x["speech"] if x["speech"] is not None else med, 2)
        list(ex.map(spawn_ctx(apply), range(len(parts))))
    return info

def make_tone(out, n, src=None, span=None, voice=None):
    """n samples of tone in `out` (PCM): `span` (start, end) of the file `src` through its
    clip's voice chain, looped forwards-backwards when it's shorter than n; or, with no src,
    a noise floor (brown noise, the same every time). Returns its mean level (dBFS RMS), None
    if it couldn't be made."""
    if src:
        secs = min(span[1] - span[0], n / SR)
        chain = (VOICE_CHAINS[voice] + "," if voice in VOICE_CHAINS else "") + \
            ("asplit[t0][t1];[t1]areverse[t2];[t0][t2]concat=n=2:v=0:a=1,"
             f"aloop=loop=-1:size={int(2 * secs * SR) + SR}," if secs * SR < n else "") + f"atrim=end_sample={n}"
        inp = ["-ss", f"{span[0]:.3f}", "-t", f"{secs:.3f}", "-i", str(src)]
        graph = f"[0:a:0]aresample={SR},{chain},volumedetect[o]"
    else:
        inp = ["-f", "lavfi", "-i", f"anoisesrc=color=brown:seed=1:amplitude=0.05:sample_rate={SR}"]
        graph = f"[0:a:0]atrim=end_sample={n},volumedetect[o]"
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-y", *inp, "-vn", "-filter_complex", graph,
                        "-map", "[o]", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2", str(out)],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if r.returncode == 0 and m else None

def lay_tone(parts, info, tl, lib, work, samples, gain, sr=16000):
    """Room tone under every part that `dead air` would fail in the joined sound (`samples`,
    as check hears it) at the master's `gain`, with a margin. Mixed under the part's own
    sound, low. Sets tone (the donor clip's id) or floor (no clip had tone to give) on the
    part's info, and quiet when the part had no sound of its own. True if any part changed
    (join them again)."""
    clips = tl["clips"]
    dead = nearly_dead(samples, sr, gain)
    if not dead:
        return False
    at = [f / FPS for f in itertools.accumulate([0] + [clip_frames(c) for c in clips])]
    silent = tone_needed(dead, at)
    lo = max(LEVEL_I - TONE_UNDER[0], DEAD_DB + TONE_FLOOR - gain)
    hi = max(lo, LEVEL_I - TONE_UNDER[1])
    def one(i):
        c, part, x = clips[i], parts[i], info[i]
        if i not in silent:
            return False
        t0, t1 = at[i], at[i + 1]
        n = clip_frames(c) * SR // FPS
        tone, level = work / f"tone{i:03d}.wav", None
        # a spoken clip's own room first; then the clips before it, nearest first, then after
        donors = ([i] if x["spoken"] else []) + list(range(i - 1, -1, -1)) + list(range(i + 1, len(clips)))
        for j in donors:
            d, ditem = clips[j], lib[clips[j]["media"]]
            span = ditem.get("has_audio") and tone_span(d, ditem, info[j]["regs"], after=j <= i, need=n / SR)
            if not span:
                continue
            mean = make_tone(tone, n, ditem["path"], span, clip_voice(d, tl, info[j]["spoken"]))
            if mean is None or mean < -90:
                continue                    # digital silence: nothing to lift
            level = min(hi, max(lo, mean + info[j]["gain"]))      # where it sits under the donor, in bounds
            if nearly_dead(*tone_heard(tone), level - mean + gain):
                level = None
                continue                    # holes in it: it would fail the same rule
            x["tone"] = d["id"]
            break
        if level is None:
            mean = make_tone(tone, n)
            if mean is None:
                return False
            level, x["floor"] = lo, True
        x["quiet"] = silent[i] >= 0.9 * (t1 - t0)
        fd = min(FADE, (t1 - t0) / 4)
        resound(part, work, clip_frames(c), extra=[tone.name], graph=
                f"[1:a:0]volume={level - mean:.2f}dB,afade=t=in:d={fd:.3f},afade=t=out:st={t1 - t0 - fd:.4f}:d={fd:.3f}[t];"
                "[0:a:0][t]amix=inputs=2:duration=first:normalize=0")
        return True
    with ThreadPoolExecutor(render_pool_size()) as ex:
        return any(list(ex.map(spawn_ctx(one), range(len(parts)))))

def tone_heard(wav):
    """A tone file's sound as one channel (the mean of its two): (samples, rate)."""
    import array, wave
    with wave.open(str(wav)) as w:
        a, ch, sr = array.array("h", w.readframes(w.getnframes())), w.getnchannels(), w.getframerate()
    return (array.array("h", [(x + y) // 2 for x, y in zip(a[0::2], a[1::2])]) if ch == 2 else a), sr

def level_report(info):
    """What render.done says of the levelling: per clip {clip, gain, speech?, tone?, floor?,
    quiet?, limited?}."""
    return [{k: v for k, v in x.items() if k in ("clip", "gain", "speech", "tone", "floor", "quiet", "limited")
             and v is not None and v is not False} for x in info]

def level_lines(level):
    """What levelling did, in words, when it did something worth knowing."""
    out = []
    g = [x["speech"] for x in level or [] if x.get("speech") is not None]
    if g and max(g) - min(g) >= 1.0:
        out.append(f"levelled {len(g)} spoken clip{'s' if len(g) != 1 else ''} to one speech level "
                   f"(they were {max(g) - min(g):.1f} dB apart)")
    for k, x in enumerate(level or [], 1):
        why = "has no sound of its own" if x.get("quiet") else "drops to silence in places"
        if x.get("tone"):
            j = next((n for n, y in enumerate(level, 1) if y["clip"] == x["tone"]), "?")
            out.append(f"clip {k} {why}: " + ("its own room tone" if j == k else f"room tone from clip {j}") +
                       " under it, low")
        elif x.get("floor"):
            out.append(f"clip {k} {why}, and no clip has room tone to give: a faint noise floor under it")
    return out

def cut_list(tl, lib):
    """Every cut in a render, on the export's clock: {at, frame, kind, clip, same}. kind
    "sound" is one clip to the next (what's heard cuts); "picture" is a cover piece starting or
    ending inside a clip (only the picture cuts; the sound runs on, so it's never judged as a
    speech cut). clip: the 1-based clip the cut is in, or the one it leaves. frame: the export
    frame the new picture starts on (every clip is whole frames). same: the same media's
    picture either side, from different moments of it: a jump cut unless something hides it.
    lib: media by id (a cover piece stops where its footage does)."""
    def ends(c):                 # cover_plan's spans as (media, second) at each end
        out = []
        for f0, f1, p, t0 in cover_plan(c, lib)[0]:
            m, step = (p["media"], 1.0) if p else (c["media"], clip_pace(c))
            out.append((f0, f1, (m, t0), (m, t0 + (f1 - f0) / FPS * step)))
        return out
    def same(l, r):              # different moments (more than a frame apart) of one media
        return l[0] == r[0] and abs(l[1] - r[1]) > 1.5 / FPS
    out, f = [], 0
    clips = tl.get("clips", [])
    for k, c in enumerate(clips, 1):
        sp = ends(c)
        if k > 1:
            out.append({"at": round(f / FPS, 3), "frame": f, "kind": "sound", "clip": k - 1,
                        "same": same(last, sp[0][2])})
        for x, y in zip(sp, sp[1:]):
            g = f + y[0]
            out.append({"at": round(g / FPS, 3), "frame": g, "kind": "picture", "clip": k,
                        "same": same(x[3], y[2])})
        last = sp[-1][3]
        f += clip_frames(c)
    return out

LOUD_I, LOUD_TP = -14.0, -1.0       # QUALITY.md: -14 LUFS, true peak <= -1 dBTP
LOUD_CEIL = LOUD_TP - 1.0           # where the limiter holds the peaks: 1 dB for the AAC encoder
LEVEL_CEIL = LOUD_CEIL - (LOUD_I - LEVEL_I) - 0.5   # dBFS: where levelling holds a part's peaks, so the
                                    #   master's gain (about LOUD_I - LEVEL_I, a little more when the
                                    #   reel has quiet stretches) lifts them to its ceiling and no further
LOUD_NEAR = 0.3                     # the master lands this close to LOUD_I (LU), or goes round again

def loudness_of(path, hear=False):
    """{input_i, input_tp} (LUFS, dBTP) of a file's sound; {} when there is nothing to measure
    (no sound, or silence). hear: also its sound the way `check` hears an export (16-bit mono
    samples at 16 kHz, what `dead_air` reads), from the same pass: (measurement, samples)."""
    import array
    graph = "[0:a:0]asplit[m0][h0];[m0]loudnorm=print_format=json[m];[h0]aresample=16000[h]" if hear else \
            "[0:a:0]loudnorm=print_format=json[m]"
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-filter_complex", graph,
                        "-map", "[m]", "-f", "null", os.devnull,
                        *(["-map", "[h]", "-ac", "1", "-f", "s16le", "-"] if hear else [])], capture_output=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr.decode(errors="replace"))
    try:
        meas = {k: float(v) for k, v in json.loads(m.group(0)).items() if k in ("input_i", "input_tp")}
    except (AttributeError, ValueError):
        meas = {}
    meas = meas if r.returncode == 0 and meas.get("input_i", -99) > -70 else {}
    return (meas, array.array("h", r.stdout[:len(r.stdout) // 2 * 2])) if hear else meas

def master_filter(gain, tp):
    """The master's sound filter: one gain for the whole reel, and a true-peak limiter (run at
    192 kHz, so it sees the peaks between samples) only when that gain would put the
    measured true peak `tp` over LOUD_CEIL. Nothing in it follows the programme's level, so
    the levels the render set clip by clip are the levels in the export."""
    af = f"volume={gain:.2f}dB"
    if tp + gain > LOUD_CEIL:
        af += (f",aresample=192000,alimiter=limit={10 ** (LOUD_CEIL / 20):.4f}:level=false:attack=5:release=50"
               f":latency=true,aresample={SR}")
    return af

def loudnorm(src, out, meas=None):
    """The master: src's sound brought to LOUD_I by ONE gain for the whole reel (measured, then
    applied), its peaks held under LOUD_TP by a limiter; video is copied. The limiter takes a
    little loudness with the peaks it holds, so what is encoded is measured as it goes, and
    encoded again with the gain corrected when it lands more than LOUD_NEAR off (twice at
    most). A silent file is copied as it is. meas: loudness_of(src), if the caller has it.
    Returns {input_i, input_tp, normalized, gain, limited}."""
    meas = meas if meas is not None else loudness_of(src)
    if not meas:                       # silence (or unmeasurable): nothing to normalize
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
            "-ar", str(SR), "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
        return {"input_i": None, "normalized": False}
    gain = LOUD_I - meas["input_i"]
    limited = meas["input_tp"] + gain > LOUD_CEIL
    for again in (True, True, False):
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-y", "-i", str(src), "-map", "0:v:0",
                            "-map", "0:a:0", "-c:v", "copy", "-af",
                            master_filter(gain, meas["input_tp"]) + ",ebur128=framelog=verbose", "-ar", str(SR),
                            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({r.returncode}):\n{r.stderr[-2000:]}")
        m = re.search(r"I:\s+(-?[\d.]+) LUFS", r.stderr.rpartition("Summary:")[2])
        if not (again and limited and m) or abs(LOUD_I - float(m.group(1))) <= LOUD_NEAR:
            break
        gain += LOUD_I - float(m.group(1))
    return {"input_i": meas["input_i"], "input_tp": meas["input_tp"], "normalized": True,
            "gain": round(gain, 2), "limited": limited}

def render(slug, actor="cli"):
    need("ffmpeg")
    st = state()
    proj = st["projects"].get(slug)
    if not proj:
        raise RuntimeError(f"no project {slug}")
    tl = normalize_timeline(proj["timeline"], st["media"])
    if not tl["clips"]:
        raise RuntimeError("timeline is empty")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    work, n = pdir(slug) / "render" / stamp, 1
    while work.exists():               # two renders in the same second
        n += 1
        stamp = f"{stamp.split('_')[0]}_{n}"
        work = pdir(slug) / "render" / stamp
    work.mkdir(parents=True)
    try:
        final, same, t0, loud, level = _render_parts(slug, st, tl, work, stamp)
    finally:                           # a failed export leaves no parts behind
        shutil.rmtree(work, ignore_errors=True)
    dur = sum(clip_frames(c) for c in tl["clips"]) / FPS
    rel = str(final.relative_to(HOME))
    emit("render.done", None, project=slug, file=rel, duration=round(dur, 2),
         aspect=tl["aspect"], clips=len(tl["clips"]), sha256=receipt(final), actor=actor,
         secs=round(time.monotonic() - t0, 2), concat="copy" if same else "reencode",
         loudness=loud, timeline=tl, cuts=cut_list(tl, st["media"]), caption_rule=CAPTION_RULE,
         **({"level": level} if level else {}))
    for line in level_lines(level):
        print(line, file=sys.stderr)
    return final


# ---------- check: does an export clear the bar? ------------------------------
# `studio check` measures an export against QUALITY.md's hard rules, writes a review sheet
# the agent can look at (filmstrip + waveform + words around every cut, a contact sheet of
# the whole export), logs check.done, and lists the judgment rules for the agent to answer.

CHECK_LEAD = (0.05, 0.15)       # lead-in before the first word of a spoken clip
CHECK_TAIL = 0.25               # tail after the last word
CHECK_FIRST_WORD = 0.5          # time to the first word of the export
CHECK_PAUSE = 0.35              # longest pause inside speech
CHECK_PAUSE_TOL = 0.04          # Silero hears in 32 ms windows: a measured gap is +-1 window
CHECK_SHORTEST = 1.5            # shortest spoken piece
CHECK_JOIN_TAIL = 0.04          # a pause cut (silence taken out of one take): the least room
                                #   left after the last word before it

CHECK_LENGTH = (15.0, 35.0, 60.0)   # good range, and the most any reel may be
CHECK_CAPTIONS = 0.8            # word agreement (0-1) between captions and the re-heard audio
CHECK_FREEZE = 2.0              # a picture that doesn't change for this long reads as frozen
FILLERS = {"yeah", "so", "um", "uh", "umm", "uhm", "like", "okay", "ok", "and", "but", "anyway",
           "alright", "right"}
FONTS = ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]

def judgment_rules():
    """QUALITY.md's judgment rules, one string each (they can't be measured)."""
    p = HERE / "QUALITY.md"
    if not p.exists():
        return []
    rules, on = [], False
    for line in p.read_text().splitlines():
        if line.startswith("## "):
            on = line.lower().startswith("## judgment")
            continue
        if not on:
            continue
        if re.match(r"\d+\.\s", line):
            rules.append(line.strip())
        elif line.startswith("   ") and rules:
            rules[-1] += " " + line.strip()
    return [r.replace("**", "") for r in rules]

def export_wav(path, out):
    sh(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "wav", str(out)])
    return out

def measure_loudness(path):
    """(integrated LUFS, true peak dBTP, loudness range LU) by ffmpeg's ebur128; None for any
    that's unmeasurable."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                        "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True)
    summary = r.stderr.rpartition("Summary:")[2]
    i = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary)
    tp = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    lra = re.search(r"LRA:\s+(-?[\d.]+|-inf) LU", summary)
    f = lambda m: float(m.group(1)) if m and m.group(1) != "-inf" else None
    return f(i), f(tp), f(lra)

# What the rendered cuts look and sound like (`check`'s cut rules), calibrated on four real
# reels on 2026-11-09 (athanor projects/studio/tools/flash-detect.py has the calibration notes).
CUT_SCENE = 0.30     # a picture cut: the masked scene score is at least this
CUT_AGAIN = 0.20     # another change this big within CUT_WINDOW frames of a cut: a flash
CUT_WINDOW = 3
CUT_MASK = 0.45      # the bottom of the frame (where captions are) is left out of the scene score
CUT_MATCH = 3        # a timeline cut is looked for this many frames either side of where it's due
STRAY_NEAR = 15      # a picture change this close to a cut that isn't the cut is stray; one
                     #   farther from every cut is the footage's own (a whip pan, a cartoon's edit)
JUMP_LO = 0.04       # the same media either side of a cut, and the picture changed this much
SOFT_STEP, SOFT_STILL = 0.10, 0.03   # first frame after a cut differs, the next one doesn't
POP_LUMA = (16, 239)                 # a frame darker / brighter than this between two that aren't
FRAG_DB = 18.0       # sound within 30 ms of a sound cut this far over the reel's floor: a fragment
CLICK_RATIO, CLICK_MIN = 6.0, 0.02   # a sample step at the junction this many times the steps
                                     #   around it (and at least this, full scale): a click
CUT_RULES = {        # rule: (what passing means, hard)
    "flash": ("no stray frames at a cut", True),
    "stray": ("every picture cut is in the timeline", True),
    "pop": ("no one-frame black or white pops", True),
    "jump": ("no jump cut (the same shot either side of a cut, nothing hiding it)", True),
    "fragment": ("no clipped word or breath at a sound cut", True),
    "click": ("no click at a cut", True),
    "held": (f"every frame on the {FPS} fps grid (none held at a cut)", True),
    "soft": ("the picture settles at once after every cut", False),
}

def scan_picture(path):
    """One decode of the export's picture. Returns (black, frozen, frames): the [(start, end)]
    black and frozen stretches (blackdetect, freezedetect on a small copy), and per frame
    (pts, scene, luma): the scene-change score with the caption band masked off, and the
    frame's mean luma (0-255)."""
    g = (f"[0:v]split[a][b];"
         f"[a]scale=-2:320,blackdetect=d=0.1:pix_th=0.10,freezedetect=n=-60dB:d={CHECK_FREEZE}[o1];"
         f"[b]scale=360:-2,signalstats,crop=iw:ih*{1 - CUT_MASK:.3f}:0:0,select='gte(scene,0)',"
         f"metadata=print:file=frames.txt[o2]")
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(Path(path).resolve()), "-an",
                            "-filter_complex", g, "-map", "[o1]", "-f", "null", "-",
                            "-map", "[o2]", "-f", "null", "-"], capture_output=True, text=True, cwd=td)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({r.returncode}):\n{r.stderr[-1500:]}")
        meta = (Path(td) / "frames.txt").read_text()
    frames = []
    for block in re.split(r"(?m)^(?=frame:)", meta):
        m = re.match(r"frame:\d+\s+pts:\S+\s+pts_time:(\S+)", block)
        if m:
            kv = dict(re.findall(r"lavfi\.([\w.]+)=(\S+)", block))
            frames.append((float(m.group(1)), float(kv.get("scene_score", 0)),
                           float(kv.get("signalstats.YAVG", 0))))
    frames.sort()
    black = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", r.stderr)]
    fs = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", r.stderr)]
    fe = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", r.stderr)]
    dur = probe(path)["duration"]
    return black, [(a, fe[k] if k < len(fe) else dur) for k, a in enumerate(fs)], frames

def wav_pcm(wav):
    """A 16-bit mono WAV's samples (array of ints) and its rate."""
    import array, wave
    with wave.open(str(wav)) as w:
        return array.array("h", w.readframes(w.getnframes())), w.getframerate()

def cut_findings(frames, cuts=None, samples=None, sr=16000):
    """The cuts as rendered. frames: scan_picture's (pts, scene, luma) per frame; cuts:
    cut_list's (frame, kind, same, and for sound/pause cuts `spoken`: (left, right), whether
    the clip either side is speech); samples: the export's sound, 16-bit mono at sr. Without
    cuts only flash, pop and held are looked for. Returns [{rule, frame, time, detail, cut}],
    cut: the index in cuts the finding is at (or None)."""
    n = len(frames)
    pts = [f[0] for f in frames]
    scene = [f[1] for f in frames]
    luma = [f[2] for f in frames]
    out = []
    def add(rule, f, detail, cut=None):
        out.append({"rule": rule, "frame": f, "time": round(pts[min(f, n - 1)], 3), "detail": detail, "cut": cut})
    if n < 2:
        return out
    changes = [f for f in range(1, n) if scene[f] >= CUT_SCENE]

    # flash: a cut followed by another within CUT_WINDOW frames; the frames between are nobody's
    in_flash = set()
    for f in changes:
        if f in in_flash:
            continue
        end, g = None, f
        while True:
            h = next((h for h in range(g + 1, min(n, g + CUT_WINDOW + 1)) if scene[h] >= CUT_AGAIN), None)
            if h is None:
                break
            end = g = h
            in_flash.add(h)
        if end is not None:
            in_flash.add(f)
            add("flash", f, f"{end - f} frame{'s' if end - f != 1 else ''} ({f}-{end - 1}) belong to "
                            f"neither shot: a cut at frame {f}, another at {end}")

    # the timeline's cuts, found in the picture in order: each where the previous one was found
    # plus the frames between them (never drifting), the strongest change near there
    due, found = {}, {}
    if cuts:
        cand = [f for f in range(1, n) if scene[f] >= JUMP_LO]
        prev_c, prev_f = 0, 0
        for i, c in enumerate(cuts):
            pred = prev_f + c["frame"] - prev_c
            due[i] = pred
            near = [f for f in cand if abs(f - pred) <= CUT_MATCH and f not in found.values()]
            if near:
                strong = [f for f in near if scene[f] >= CUT_SCENE]
                # a caption appearing the frame after a cut scores 0.1-0.3: the strongest wins
                f = max(strong or near, key=lambda x: (scene[x], -abs(x - pred)))
                found[i], prev_f = f, f
            else:
                prev_f = pred                  # nothing to see (a pause cut in one shot)
            prev_c = c["frame"]
        for f in changes:
            if f in found.values() or f in in_flash:
                continue
            i, d = min(((i, f - due[i]) for i in due), key=lambda x: abs(x[1]))
            if abs(d) <= STRAY_NEAR:
                add("stray", f, f"a picture change {abs(d)} frame{'s' if abs(d) != 1 else ''} "
                                f"{'after' if d > 0 else 'before'} the cut at {cuts[i]['at']:.2f}s, "
                                f"that isn't in the timeline: picture from past the clip's edge, or a "
                                f"cut that landed off its frame")
        for i, f in found.items():
            c, sc = cuts[i], scene[f]
            # a jump cut needs the timeline: the same media's picture either side, from another
            # moment. Pixel scores alone can't tell: real shot changes in one yard score 0.15-0.31
            if c.get("same") and sc >= JUMP_LO:
                add("jump", f, f"the same shot either side of the cut (change {sc:.2f}) and nothing "
                               f"hides it: cover it, punch in, or cut elsewhere", i)
            if f + 2 < n and scene[f + 1] >= SOFT_STEP and scene[f + 2] < SOFT_STILL:
                add("soft", f + 1, f"the picture changes again the frame after the cut ({scene[f + 1]:.2f}: "
                                   f"motion settling or a caption appearing), then holds; look at the "
                                   f"strip if it reads as a stutter", i)

    # one-frame pops to black or white
    lo, hi = POP_LUMA
    for f in range(1, n - 1):
        y, a, b = luma[f], luma[f - 1], luma[f + 1]
        if (y < lo and a >= lo and b >= lo) or (y > hi and a <= hi and b <= hi):
            add("pop", f, f"one {'black' if y < lo else 'white'} frame (mean luma {y:.0f} between "
                          f"{a:.0f} and {b:.0f})")

    # held frames: a frame shown for longer (or shorter) than one frame
    step = 1 / FPS
    for f in range(1, n):
        d = pts[f] - pts[f - 1]
        if abs(d - step) > 0.1 * step:
            add("held", f - 1, f"frame {f - 1} shows for {d * 1000:.0f} ms ({d / step:.2f} frames)")

    # the sound at every sound cut: a clipped word (fragment) or a click
    if cuts and samples:
        s = samples
        sq = list(itertools.accumulate((v * v for v in s), initial=0))
        def db(a, b):
            a, b = max(0, a), min(len(s), b)
            if b <= a:
                return -120.0
            return 20 * math.log10(max(1e-9, math.sqrt((sq[b] - sq[a]) / (b - a)) / 32768))
        hop = sr // 100
        env = sorted(v for v in (db(i, i + hop) for i in range(0, len(s) - hop, hop)) if v > -90)
        floor = env[len(env) // 20] if env else -60.0
        w4, w30, w2, w5 = int(sr * 0.004), int(sr * 0.03), int(sr * 0.002), int(sr * 0.005)
        def steps(a, b):
            return max((abs(s[k] - s[k - 1]) for k in range(max(1, a), min(len(s), b))), default=0) / 32768
        def step_at(a, b):          # (biggest step, where)
            ks = range(max(1, a), min(len(s), b))
            k = max(ks, key=lambda k: abs(s[k] - s[k - 1]), default=a)
            return (abs(s[k] - s[k - 1]) / 32768 if ks else 0.0), k
        for i, c in enumerate(cuts):
            if c["kind"] not in ("sound", "pause"):
                continue
            left, right = c.get("spoken", (True, True))
            # the junction is where the picture cut landed; the quietest 4 ms within 40 ms of it
            # (the fades) is the join itself
            f = found.get(i, due.get(i, 0))
            i0 = int(pts[min(f, n - 1)] * sr)
            lo_, hi_ = max(w4, i0 - int(sr * 0.04)), min(len(s) - w4, i0 + int(sr * 0.04))
            if hi_ <= lo_:
                continue
            j = min(range(lo_, hi_, max(1, w4 // 2)), key=lambda k: db(k - w4 // 2, k + w4 // 2))
            before, after = db(j - w30, j), db(j, j + w30)
            loud = [(side, v) for side, v, on in (("before", before, left), ("after", after, right))
                    if on and v >= floor + FRAG_DB]
            if loud:
                side, v = max(loud, key=lambda x: x[1])
                add("fragment", f, f"speech-level sound {side} the cut at {j / sr:.3f}s ({v:.0f} dBFS, "
                                   f"floor {floor:.0f}): a clipped word or breath, or too short a "
                                   f"{'tail' if side == 'before' else 'lead-in'}", i)
            # a click is looked for at the join and in the 20 ms before the cut, where a part's
            # sound ends (a sound stopping mid-fade is a step there, not at the quietest point)
            peak, at = max(step_at(j - w2, j + w2), step_at(i0 - int(sr * 0.02), i0 + w2))
            ref = sorted(steps(k, k + w5) for k in range(j - int(sr * 0.2), j + int(sr * 0.2), w5)
                         if abs(k - j) >= w5 and k >= 1 and k + w5 < len(s))
            med = ref[len(ref) // 2] if ref else 1.0
            if peak >= CLICK_MIN and peak >= CLICK_RATIO * med:
                add("click", f, f"a sample step of {peak:.3f} at {at / sr:.3f}s, {peak / max(med, 1e-6):.0f}x "
                                f"the steps around it: a click", i)
    out.sort(key=lambda x: (x["frame"], x["rule"]))
    return out

# ---------- check: shots, silence, voice levels, the captions as shown --------------------
# Six rules on what check already has: the frame plan (shots), the export's sound, its speech
# (VAD) and the captions the render burned. Thresholds: athanor reels-craft-research-2026-11 §3.
BLIP_FRAMES = FPS               # a shot between two picture changes shows for at least 1.0 s
DEAD_DB, DEAD_SECS = -60.0, 0.3     # sound under -60 dBFS RMS for longer than this is dead air
LEVEL_STEP = 2.0                # a spoken clip's speech-gated loudness, against the median clip's (LU)
LEVEL_LRA = 6.0                 # the reel's loudness range: above this, a warning
CAP_ONSET = 0.20                # a caption appears in speech, or at most this long before it starts
CAP_QUIET = 0.3                 # a caption never stays up through this much of nobody speaking
CAP_SHORT = 0.4                 # and is never on screen for less than this
SAFE_ZONE = {"top": 0.14, "bottom": 0.20, "left": 0.06, "right": 0.15}   # platform UI, on 9:16
PRODUCT_BY = 8.0                # the product is on screen (or named in a caption) by now

def shot_list(tl, lib):
    """The export's shots by the frame plan (cover_plan): [{f0, f1, clip, id}], in export frames;
    clip (1-based) and id are the clip a shot starts in. Picture that runs on across a cut
    (the same media, the same moment) is one shot."""
    out, f, last = [], 0, None
    for k, c in enumerate(tl.get("clips", []), 1):
        for f0, f1, p, t0 in cover_plan(c, lib)[0]:
            m, step = (p["media"], 1.0) if p else (c["media"], clip_pace(c))
            if out and last[0] == m and abs(last[1] - t0) <= 1.5 / FPS:
                out[-1]["f1"] = f + f1
            else:
                out.append({"f0": f + f0, "f1": f + f1, "clip": k, "id": c["id"]})
            last = (m, t0 + (f1 - f0) / FPS * step)
        f += clip_frames(c)
    return out

def blips(shots, cards=()):
    """Shots too short to read: between two picture changes (so never the first or the last
    shot) and under BLIP_FRAMES. cards: clip ids declared designed cards, never judged."""
    return [x for x in shots[1:-1] if x["f1"] - x["f0"] < BLIP_FRAMES and x["id"] not in cards]

def dead_air(samples, sr, db=DEAD_DB, secs=DEAD_SECS):
    """[(start, end)]: every stretch longer than `secs` where the sound (16-bit mono samples)
    stays under `db` dBFS RMS, in 20 ms windows."""
    win = max(1, sr // 50)
    under = (32768 * 10 ** (db / 20)) ** 2 * win
    out, start, n = [], None, len(samples) // win
    for k in range(n + 1):
        quiet = k < n and sum(v * v for v in samples[k * win:(k + 1) * win]) < under
        if quiet and start is None:
            start = k
        elif not quiet and start is not None:
            if (k - start) * win / sr > secs + 1e-9:
                out.append((round(start * win / sr, 2), round(k * win / sr, 2)))
            start = None
    return out

def caption_plan(clips, rule=CAPTION_RULE):
    """Every caption the render burned, on the export's clock: [{a, b, text, clip, id, frames}].
    clips: the clips as rendered, each with `at` (its first export second) and `frames`.
    The same chunks the render draws (clip_captions, on the clip's paced clock); frames: how
    many export frames it shows for (ASS times are whole centiseconds)."""
    out = []
    for k, c in enumerate(clips, 1):
        n = c["frames"] / FPS
        for ch in clip_captions({"id": c["media"]}, c["in"], c["out"], clip_pace(c), rule):
            a, b = round(ch["a"], 2), min(round(ch["b"], 2), n)
            shown = math.ceil(b * FPS - 1e-6) - math.ceil(a * FPS - 1e-6)
            if shown > 0:
                out.append({"a": round(c["at"] + a, 3), "b": round(c["at"] + b, 3), "text": ch["text"],
                            "clip": k, "id": c["id"], "frames": shown})
    return out

def caption_stray(caps, regions):
    """Captions that aren't with the words, one by one: [(what, clip id)]. regions: the export's
    speech (VAD). A caption fails when it shows for under CAP_SHORT, appears more than
    CAP_ONSET before speech starts (or after it ended), or stays through CAP_QUIET of nobody
    speaking."""
    regions = sorted(regions or [])
    out = []
    for c in caps:
        a, b, why = c["a"], c["b"], []
        if c["frames"] < round(CAP_SHORT * FPS) - 1:       # ASS times are centiseconds: a frame's grace
            why.append(f"on screen for {c['frames'] / FPS:.2f}s (want >= {CAP_SHORT}s)")
        if not any(r0 - CAP_ONSET - 1e-6 <= a <= r1 for r0, r1 in regions):
            nxt = min((r0 for r0, _ in regions if r0 > a), default=None)
            why.append(f"appears {nxt - a:.2f}s before the voice (want <= {CAP_ONSET:.2f}s)" if nxt is not None and nxt < b
                       else "appears where nobody is speaking")
        # the quiet after its words (a voice that only starts in its last tenth of a second is
        # the next caption's). A pause between two of its own words is `pauses`' to judge.
        mine = [r for r in regions if r[1] > a and r[0] < b]
        mine = [r for r in mine if r[0] < b - 0.1] or mine
        q0, q1 = (min(b, mine[-1][1]) if mine else a), b
        if q1 - q0 >= CAP_QUIET + CHECK_PAUSE_TOL:
            why.append(f"stays up through {q1 - q0:.2f}s of nobody speaking ({q0:.2f}-{q1:.2f}s; want < {CAP_QUIET}s)")
        if why:
            out.append((f"{a:.2f}s {c['text']!r} (clip {c['clip']}): " + "; ".join(why), c["id"]))
    return out

def caption_boxes(caps, w, h, work, rule=CAPTION_RULE):
    """Where each caption sits in the frame: [(x0, y0, x1, y1) or None], in pixels. Drawn by the
    same ASS style and libass the render burns with, one caption a frame on black, measured by
    ffmpeg's bbox (plus the outline, which is black)."""
    if not caps:
        return []
    ass = Path(work) / ".boxes.ass"
    write_ass([{"a": k / 10, "b": (k + 1) / 10, "text": c["text"]} for k, c in enumerate(caps)], ass, w, h, rule)
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-f", "lavfi", "-i",
                            f"color=c=black:s={w}x{h}:r=10:d={len(caps) / 10:.1f}", "-vf",
                            f"subtitles={ass.name}:fontsdir=/System/Library/Fonts,bbox=min_val=40",
                            "-frames:v", str(len(caps)), "-f", "null", "-"],
                           capture_output=True, text=True, cwd=work)
    finally:
        ass.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}):\n{r.stderr[-1500:]}")
    pad = max(3, round(min(w, h) * 0.08) // 14)
    out = [None] * len(caps)
    for m in re.finditer(r"n:(\d+) pts:\S+ pts_time:\S+ x1:(\d+) x2:(\d+) y1:(\d+) y2:(\d+)", r.stderr):
        k, x1, x2, y1, y2 = (int(v) for v in m.groups())
        if k < len(out):
            out[k] = (max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + 1 + pad), min(h, y2 + 1 + pad))
    return out

def safe_zone(caps, boxes, w, h):
    """Captions that reach into the platform's UI: [(what, clip id)]."""
    out = []
    for c, bx in zip(caps, boxes):
        if not bx:
            continue
        x0, y0, x1, y1 = bx
        into = [f"{side} {round(100 * SAFE_ZONE[side])}% (by {px}px)" for side, px in
                (("top", round(SAFE_ZONE["top"] * h) - y0), ("bottom", y1 - round((1 - SAFE_ZONE["bottom"]) * h)),
                 ("left", round(SAFE_ZONE["left"] * w) - x0), ("right", x1 - round((1 - SAFE_ZONE["right"]) * w)))
                if px > 0]
        if into:
            out.append((f"{c['a']:.2f}s {c['text']!r} (clip {c['clip']}) is in the " + ", ".join(into), c["id"]))
    return out

def product_first(caps, clips, lib, brand, cards=()):
    """(second, what) the product first shows: the first caption naming the brand, or the first
    picture from footage with no sound of its own (a screen recording, an insert made for the
    picture), whichever is earlier. None if neither ever comes. A card isn't an insert."""
    name = re.sub(r"[^a-z0-9]+", "", (brand or "").lower())
    found = []
    t = next((c["a"] for c in caps if name and name in _words_of(c["text"])), None)
    if t is not None:
        found.append((t, f"a caption says {brand.capitalize()}"))
    for c in clips:
        if c["id"] in cards:
            continue
        for f0, f1, p, t0 in cover_plan(c, lib)[0]:
            it = lib.get((p or c)["media"]) or {}
            if it.get("has_video") and not it.get("has_audio"):
                found.append((round(c["at"] + f0 / FPS, 2), f"an insert, {it.get('name')}"))
                break
    return min(found, default=None)

def cut_tile(path, frame, out):
    """Nine frames of an export around one frame (4 before, 4 after), numbered: the review
    sheet's picture of a cut finding."""
    a = max(0, frame - 4)
    label = f"drawtext={_font()}text='%{{eif\\:n+{a}\\:d}}':x=6:y=6:fontsize=22:fontcolor=yellow:box=1:boxcolor=black@0.6,"
    sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, a / FPS - 0.5 / FPS):.4f}", "-i", str(path),
        "-vf", f"scale=216:-2,{label}tile=9x1", "-frames:v", "1", str(out)])

def _words_of(text):
    return [w for w in (re.sub(r"[^a-z0-9]+", "", x.lower()) for x in text.split()) if w]

def _in_region(t, regions, slack=0.01):
    return next((r for r in regions if r[0] + slack < t < r[1] - slack), None)

def export_for(st, slug, path=None):
    """(export path, its render.done data or None). Default: the project's latest export."""
    exports = (st["projects"].get(slug) or {}).get("exports") or []
    if path is None:
        if not exports:
            raise RuntimeError(f"{slug} has no export yet (studio render {slug})")
        ev = exports[-1]
        return HOME / ev["file"], ev
    p = Path(path).expanduser()
    p = p if p.is_absolute() else (HOME / p if (HOME / p).exists() else p.resolve())
    if not p.is_file():
        raise RuntimeError(f"no file {path}")
    ev = next((e for e in reversed(exports) if (HOME / e["file"]).resolve() == p.resolve()), None)
    return p, ev

def check(slug, path=None, actor="cli"):
    """Measure one export against QUALITY.md's hard rules. Returns the result dict (also in
    <export>.check/check.json, with the review sheet next to it) and logs check.done."""
    need("ffmpeg"); need("whisper-cli")
    st = state()
    if slug not in st["projects"]:
        raise RuntimeError(f"no project {slug}")
    final, ev = export_for(st, slug, path)
    tl = (ev or {}).get("timeline")
    cap_rule = (ev or {}).get("caption_rule", 1)      # the caption rule these bytes were burned with
    out_dir = final.with_name(final.stem + ".check")
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    sha0 = receipt(final)          # bound to these bytes; hashed again at the end
    dur = probe(final)["duration"]
    rules = []
    def rule(name, ok, detail, items=None):
        """items: strings, or (string, clip id) for a failure an exception can name by clip."""
        items = [x if isinstance(x, tuple) else (x, None) for x in items or []]
        rules.append({"rule": name, "status": ok if isinstance(ok, str) else ("pass" if ok else "fail"),
                      "detail": detail, "_items": items})

    # where the cuts are, on the export's clock, and the speech each clip cuts
    clips, f = [], 0
    for c in (tl or {}).get("clips", []):
        it = st["media"].get(c["media"], {})
        regs = (ensure_speech(c["media"]) if it.get("has_audio") else []) or []
        clips.append({**c, "at": f / FPS, "end": (f + clip_frames(c)) / FPS, "frames": clip_frames(c),
                      "name": it.get("name"),
                      "regions": regs, "pace": clip_pace(c),
                      "spoken": bool(it.get("has_audio")) and clip_spoken(c, regs)})
        f += clip_frames(c)
    clip_at = lambda x: next((c for c in reversed(clips) if c["at"] <= x + 1e-6), None)
    cid = lambda x: (c := clip_at(x)) and c["id"]
    # a clip nobody speaks in (b-roll, a cartoon, a card) is picture: its time is never a pause
    # between the spoken clips around it, and what VAD hears in it (music, effects) isn't a word
    picture_only = [(c["at"], c["end"]) for c in clips if not c["spoken"]]
    cuts = cut_list(tl or {}, st["media"])
    # a pause cut: the next clip goes on in the same take a little later, and all it skips is
    # silence (`timeline tighten` makes these). Its edges are judged as one pause, not as the
    # end of one beat and the start of another.
    joined = [pause_cut(c, clips[i + 1] if i + 1 < len(clips) else None, c["regions"])
              for i, c in enumerate(clips)]
    for x in cuts:
        if x["kind"] == "sound" and joined[x["clip"] - 1]:
            x["kind"] = "pause"

    # 1. spoken in/out points against the source's speech
    if not tl:
        rule("cuts", "skip", "not a studio render (or rendered before check existed): no cut list")
    else:
        bad, short, n, run = [], [], 0, 0.0
        for k, c in enumerate(clips, 1):
            a, b, regs, pace = c["in"], c["out"], c["regions"], c["pace"]
            after, before = joined[k - 1], k > 1 and joined[k - 2]
            run = (run if before else 0.0) + (b - a) / pace      # lead, tail, length: export seconds
            mine = [r for r in regs if r[1] > a and r[0] < b]
            if sum(min(b, r[1]) - max(a, r[0]) for r in mine) < SNAP_EDGE:
                continue                    # no speech in it: a silent shot
            n += not before
            r = _in_region(a, regs)
            if r:
                bad.append(f"clip {k} in {a:.2f}s is inside speech ({r[0]:.2f}-{r[1]:.2f})")
            elif not before:
                lead = (min([x[0] for x in mine if x[0] >= a - 0.011] or [a]) - a) / pace
                if not CHECK_LEAD[0] - 0.005 <= lead <= CHECK_LEAD[1] + 0.005:
                    bad.append(f"clip {k} lead-in {lead:.2f}s (want {CHECK_LEAD[0]}-{CHECK_LEAD[1]}s)")
            r = _in_region(b, regs)
            if r:
                bad.append(f"clip {k} out {b:.2f}s is inside speech ({r[0]:.2f}-{r[1]:.2f}): "
                           f"it ends at {r[1]:.2f}")
            else:
                tail = (b - max([x[1] for x in mine if x[1] <= b + 0.011] or [b])) / pace
                want = CHECK_JOIN_TAIL if after else CHECK_TAIL
                if tail < want - 0.005:
                    bad.append(f"clip {k} tail {tail:.2f}s after the last word (want >= {want}s"
                               + (" at a pause cut)" if after else ")"))
            if not after and run < CHECK_SHORTEST:
                short.append((f"clip {k} is {run:.2f}s of speech (want >= {CHECK_SHORTEST}s)", c["id"]))
        npause = sum(x["kind"] == "pause" for x in cuts)
        npic = sum(x["kind"] == "picture" for x in cuts)
        extra = "".join(f"; {m} {w} cut{'s' if m != 1 else ''} ({why})" for m, w, why in
                        ((npause, "pause", "silence out of one take: judged as a pause"),
                         (npic, "picture-only", "cover; the sound runs on, not a speech cut")) if m)
        rule("cuts", not bad, f"{n} spoken beat{'s' if n != 1 else ''}: " +
             ("every in/out on a speech edge" if not bad else f"{len(bad)} problem{'s' if len(bad) != 1 else ''}"
              " (fix: studio timeline snap P CLIP|all)") + extra, bad)
        rule("shortest", not short, f"every spoken piece >= {CHECK_SHORTEST}s" if not short else
             f"{len(short)} spoken piece{'s' if len(short) != 1 else ''} under {CHECK_SHORTEST}s", short)

    # 1b. every cover piece shows to the end it was laid to, by the same frame plan the render
    # used (cover_plan): footage that runs out brings the clip's own picture back where b-roll
    # was meant
    if not tl:
        rule("cover short", "skip", "not a studio render: no cover list")
    else:
        short = []
        for k, c in enumerate(tl["clips"], 1):
            for n, p, text in cover_short(c, st["media"]):
                short.append((f"clip {k} cover {n}: {text}", c["id"]))
        n = sum(len(c.get("cover") or []) for c in tl["clips"])
        rule("cover short", not short,
             (f"every cover piece shows to its end ({n})" if n else "no cover") if not short else
             f"{len(short)} cover piece{'s stop' if len(short) != 1 else ' stops'} before its end (the clip's own "
             "picture shows instead; fix: lay it again with footage that lasts, cover less, or uncover)", short)

    # 1c. no shot too short to read (by the same frame plan); a declared card is never a blip
    as_rendered = {c["id"]: clip_fp(c) for c in (tl or {}).get("clips", [])}
    cards = {x["clip"] for x in ((st["projects"][slug].get("exceptions") or {}).values())
             if x.get("kind") == "card" and not x.get("revoked") and x.get("clip")
             and x.get("fp") == as_rendered.get(x["clip"])}
    if not tl:
        rule("blip", "skip", "not a studio render: no frame plan")
    else:
        shots = shot_list(tl, st["media"])
        short = [(f"{x['f0'] / FPS:.2f}-{x['f1'] / FPS:.2f}s (clip {x['clip']}): a {(x['f1'] - x['f0']) / FPS:.2f}s shot "
                  f"between two picture changes (want >= {BLIP_FRAMES / FPS:g}s): hold it longer, or go straight "
                  "to the next shot", x["id"]) for x in blips(shots, cards)]
        rule("blip", not short, f"no shot under {BLIP_FRAMES / FPS:g}s between two picture changes ({len(shots)} shots)"
             if not short else f"{len(short)} shot{'s' if len(short) != 1 else ''} too short to read", short)

    # 2-3. the export's own speech: first word (or declared on-screen text), pauses
    wav = export_wav(final, out_dir / ".audio.wav")
    samples, sr = wav_pcm(wav)
    heard_regions = vad_regions(wav)
    inside = lambda r: any(a - 0.05 <= r[0] and r[1] <= b + 0.05 for a, b in picture_only)
    talk = [r for r in heard_regions if not inside(r)]
    fw = talk[0][0] if talk else None
    first = clips[0]["id"] if clips else None
    if fw is not None:
        rule("first word", fw <= CHECK_FIRST_WORD, f"first word at {fw:.2f}s (want <= {CHECK_FIRST_WORD}s)",
             [] if fw <= CHECK_FIRST_WORD else [(f"first word at {fw:.2f}s", first)])
    else:
        rule("first word", "skip", "no speech in the export (on-screen text can't be measured: judge it)")
    if talk:
        long_, gaps = [], []
        for (a0, a1), (b0, b1) in zip(talk, talk[1:]):
            pieces = [(a1, b0)]               # the gap, minus the picture-only clips inside it
            for s0, s1 in picture_only:
                pieces = [q for x0, x1 in pieces for q in ((x0, min(x1, s0)), (max(x0, s1), x1)) if q[1] - q[0] > 1e-3]
            for x0, x1 in pieces:
                gaps.append(x1 - x0)
                if x1 - x0 > CHECK_PAUSE + CHECK_PAUSE_TOL:
                    c = clip_at((x0 + x1) / 2)
                    long_.append((f"{x1 - x0:.2f}s pause at {x0:.2f}s" + (f" (clip {clips.index(c) + 1})" if c else ""),
                                  c and c["id"]))
        rule("pauses", not long_, f"longest pause {max(gaps or [0]):.2f}s (want <= {CHECK_PAUSE}s inside "
             "speech; clips with no speech are picture, not pauses)", long_)
    else:
        rule("pauses", "skip", "no speech in the export")

    # 3b. never digital silence: under a card, between clips, anywhere
    dead = dead_air(samples, sr)
    rule("dead air", not dead, f"no silence: the sound never drops under {DEAD_DB:g} dBFS for more than {DEAD_SECS}s"
         if not dead else f"{len(dead)} stretch{'es' if len(dead) != 1 else ''} of silence (under {DEAD_DB:g} dBFS for "
         f"more than {DEAD_SECS}s): render lays room tone under any stretch like this, so render it again (this "
         "export is from before that, or was made with levelling off)",
         [(f"silent {a:.2f}-{b:.2f}s" + (f" (clip {clips.index(c) + 1})" if (c := clip_at((a + b) / 2)) else ""),
           cid((a + b) / 2)) for a, b in dead])

    # 4. length
    lo, hi, most = CHECK_LENGTH
    rule("length", "pass" if lo <= dur <= hi else ("warn" if hi < dur <= most else "fail"),
         f"{dur:.1f}s (want {lo:g}-{hi:g}s; up to {most:g}s only if every beat earns it)")

    # 5. loudness
    li, tp, lra = measure_loudness(final)
    if li is None:
        rule("loudness", False, "no sound to measure")
    else:
        ok = abs(li - LOUD_I) <= 1.0 and tp is not None and tp <= LOUD_TP
        rule("loudness", ok, f"{li:.1f} LUFS, true peak {tp if tp is None else round(tp, 1)} dBTP "
                             f"(want {LOUD_I:g} +-1 LUFS, <= {LOUD_TP:g} dBTP)")

    # 5b. every voice at one level: each spoken clip's speech against the median spoken clip's
    if not tl:
        rule("level step", "skip", "not a studio render: no clip list")
    else:
        lv = [(k, c, gated_loudness(final, [(c["at"] + a, c["at"] + b) for a, b in part_speech(c, c["regions"], c["frames"])]))
              for k, c in enumerate(clips, 1) if c["spoken"]]
        lv = [x for x in lv if x[2] is not None]
        med = sorted(x[2] for x in lv)[len(lv) // 2] if lv else None
        off = [(f"clip {k} speech at {v:.1f} LUFS, {abs(v - med):.1f} LU {'under' if v < med else 'over'} the "
                f"median clip ({med:.1f})", c["id"]) for k, c, v in lv if abs(v - med) > LEVEL_STEP]
        wide = lra is not None and lra > LEVEL_LRA
        spread = max(x[2] for x in lv) - min(x[2] for x in lv) if lv else 0.0
        rule("level step", "fail" if off else "warn" if wide else "pass",
             (f"{len(off)} spoken clip{'s' if len(off) != 1 else ''} more than {LEVEL_STEP:g} LU from the median "
              "(speech-gated loudness per clip; render levels them, so this is a render from before levelling "
              "or a clip with too little speech to level)" if off else
              (f"{len(lv)} spoken clip{'s' if len(lv) != 1 else ''} within {spread:.1f} LU of each other "
               f"(want each within {LEVEL_STEP:g} LU of the median)" if lv else "no spoken clip to compare"))
             + (f"; loudness range {lra:.1f} LU" + (f" (over {LEVEL_LRA:g}: quiet and loud parts are far apart)" if wide else "")
                if lra is not None else ""), off)

    # 6. black / frozen picture
    black, frozen, frames = scan_picture(final)
    bad = [(f"black {a:.2f}-{b:.2f}s", cid((a + b) / 2)) for a, b in black] + \
          [(f"frozen {a:.2f}-{b:.2f}s", cid((a + b) / 2)) for a, b in frozen]
    rule("picture", not bad, "no black or frozen frames" if not bad else f"{len(bad)} stretch{'es' if len(bad) != 1 else ''}", bad)

    # 7. the cuts as rendered: stray frames, jump cuts, clipped words, clicks, held frames.
    # Each finding names the clip it's in (at a cut: the clip the cut goes into).
    for x in cuts:
        if x["kind"] in ("sound", "pause"):
            x["spoken"] = (clips[x["clip"] - 1]["spoken"], clips[x["clip"]]["spoken"])
    found = cut_findings(frames, cuts if tl else None, samples, sr)
    into = lambda x: clips[x["clip"] if x["kind"] != "picture" else x["clip"] - 1]["id"]
    tiles = []
    for name, (passing, hard) in CUT_RULES.items():
        if not tl and name in ("stray", "jump", "soft", "fragment", "click"):
            rule(name, "skip", "not a studio render: no cut list")
            continue
        hits = [x for x in found if x["rule"] == name]
        if hard and name != "held":
            tiles += hits
        items = [(f"{x['time']:.2f}s (frame {x['frame']}): {x['detail']}",
                  into(cuts[x["cut"]]) if x["cut"] is not None else cid(x["time"])) for x in hits]
        at = ", ".join(f"{x['time']:.2f}s" for x in hits[:6]) + (", ..." if len(hits) > 6 else "")
        rule(name, ("fail" if hard else "warn") if hits else "pass",
             f"{len(hits)} at {at} (want: {passing})" if hits else passing, items)

    # 7-8. captions against what the export actually says; the last word
    brand = (st["projects"].get(slug) or {}).get("brand") or "jefe"
    vocab, fixes = brand_words(brand)
    _, hsegs, heard = run_whisper(wav, out_dir / ".whisper", vocab, heard_regions)
    hsegs, heard, _ = drop_hallucinations(hsegs, heard, wav)
    apply_fixes(hsegs, heard, fixes + case_fixes(vocab))
    wav.unlink(missing_ok=True)
    if not tl:
        rule("captions", "skip", "not a studio render: no caption list")
    elif not tl.get("captions", True):
        rule("captions", "skip", "captions are off")
    else:
        shown = [x for ch in caption_plan(clips, cap_rule) for x in _words_of(ch["text"])]
        said = [x for w in heard for x in _words_of(w["w"])]
        import difflib
        sm = difflib.SequenceMatcher(None, shown, said, autojunk=False)
        score = sm.ratio() if shown or said else 1.0
        diffs = [f"captions {' '.join(shown[i1:i2]) or '(nothing)'!r} / audio {' '.join(said[j1:j2]) or '(nothing)'!r}"
                 for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"][:8]
        rule("captions", score >= CHECK_CAPTIONS,
             f"{round(100 * score)}% of words agree ({len(shown)} on screen, {len(said)} heard; "
             f"want >= {round(100 * CHECK_CAPTIONS)}%)", diffs)
    # 7b. the captions as shown, one by one: with the words, clear of the platform's UI; and
    # how soon the product shows
    if not tl:
        for name in ("caption stray", "safe zone", "product late"):
            rule(name, "skip", "not a studio render: no caption list")
    else:
        caps = caption_plan(clips, cap_rule) if tl.get("captions", True) else []
        if not tl.get("captions", True):
            rule("caption stray", "skip", "captions are off")
            rule("safe zone", "skip", "captions are off")
        else:
            bad = caption_stray(caps, heard_regions)
            rule("caption stray", not bad, f"every caption is with its words ({len(caps)}: in speech, on screen >= "
                 f"{CAP_SHORT}s, none left up over silence)" if not bad else
                 f"{len(bad)} of {len(caps)} caption{'s' if len(caps) != 1 else ''} not with the words (fix: trim the "
                 "clip so the stray word is out, or studio words --fix)", bad)
            if tl.get("aspect") != "9:16":
                rule("safe zone", "skip", f"the safe zones are for 9:16 (this is {tl.get('aspect')})")
            else:
                w_, h_ = ASPECTS["9:16"]
                bad = safe_zone(caps, caption_boxes(caps, w_, h_, out_dir, cap_rule), w_, h_)
                rule("safe zone", not bad, "every caption clear of the top 14%, bottom 20%, left 6% and right 15%"
                     if not bad else f"{len(bad)} caption{'s' if len(bad) != 1 else ''} under the platform's UI "
                     "(top 14%, bottom 20%, left 6%, right 15%)", bad)
        pf = product_first(caps, clips, st["media"], brand, cards)
        name = (brand or "the product").capitalize()
        rule("product late", "pass" if pf and pf[0] <= PRODUCT_BY else "warn",
             (f"{name} first shows at {pf[0]:.2f}s ({pf[1]}; want <= {PRODUCT_BY:g}s)" if pf else
              f"{name} never shows: no screen insert and no caption naming it"))
    said = [x for w in heard for x in _words_of(w["w"])]
    if said:
        rule("ending", said[-1] not in FILLERS and " ".join(said[-2:]) != "you know",
             f"last word {heard[-1]['w']!r}" + (" is filler" if said[-1] in FILLERS else ""))
    else:
        rule("ending", "skip", "no speech in the export")

    # the review sheet: around every cut (and the first and last 1.5 s), then the whole thing
    edges = [(0.0, "start")] + [
        (x["at"], f"picture cut in clip {x['clip']} (cover; sound runs on)" if x["kind"] == "picture"
         else f"{'pause cut' if x['kind'] == 'pause' else 'cut'}: clip {x['clip']} -> {x['clip'] + 1}")
        for x in cuts] + [(dur, "end")]
    sheets = []
    for k, (x, what) in enumerate(edges):
        a0, a1 = max(0.0, x - 1.5), min(dur, x + 1.5)
        if a1 - a0 < 0.2:
            continue
        name = f"cut-{k:02d}-{x:06.2f}s.png"
        try:
            cut_sheet(final, out_dir / name, a0, a1, x, heard, heard_regions, f"{what} at {x:.2f}s")
            sheets.append(name)
        except RuntimeError as e:
            print(f"    review sheet {name} failed: {str(e).splitlines()[-1][:160]}", file=sys.stderr)
    for x in tiles:                    # nine frames around every cut finding
        name = f"{x['rule']}-f{x['frame']:05d}.png"
        try:
            cut_tile(final, x["frame"], out_dir / name)
            sheets.append(name)
        except RuntimeError as e:
            print(f"    review tile {name} failed: {str(e).splitlines()[-1][:160]}", file=sys.stderr)
    try:
        contact_sheet(final, out_dir / "contact.png", dur)
        sheets.append("contact.png")
    except RuntimeError as e:
        print(f"    contact sheet failed: {str(e).splitlines()[-1][:160]}", file=sys.stderr)

    # every hard failure, by rule and clip; then the exceptions granted for this project
    fps = {c["id"]: clip_fp(c) for c in (tl or {}).get("clips", [])}   # the clips as rendered
    reel = reel_fp(tl) if tl else None
    failures = [{"rule": r["rule"], "clip": c, "item": i, "fp": fps.get(c), "reel": reel}
                for r in rules if r["status"] == "fail" for i, c in (r["_items"] or [(r["detail"], None)])]
    exceptions = (state()["projects"].get(slug) or {}).get("exceptions") or {}
    for r in rules:
        items = r.pop("_items")
        if r["status"] != "fail":
            if items:
                r["items"] = [i for i, _ in items]
            continue
        open_, exc = [], []
        for i, c in (items or [(r["detail"], None)]):
            lapsed = []
            x = covering({"rule": r["rule"], "clip": c, "fp": fps.get(c), "reel": reel}, exceptions, lapsed)
            if x:
                exc.append({"item": i, "exception": x["id"], "by": x.get("actor"), "why": x.get("why"),
                            **({"declares": exception_scope(x)} if exception_scope(x) else {}),
                            **({"text": x["text"]} if x.get("text") else {})})
            else:
                open_.append(i + "".join(f" (exception {l['id']} lapsed: the "
                                         f"{'clip' if l.get('clip') else 'story'} changed since it was granted)"
                                         for l in lapsed))
        if items and open_:
            r["items"] = open_
        if exc:
            r["excepted"] = exc
            if not open_:
                r["status"] = "excepted"
    failed = [r["rule"] for r in rules if r["status"] == "fail"]
    excepted = [{"rule": r["rule"], **x} for r in rules for x in r.get("excepted", [])]
    sha = receipt(final)
    if sha != sha0:     # replaced while it was being measured: these numbers belong to no bytes
        raise RuntimeError(f"{final.name} changed while it was being checked; no check record written. Check it again.")
    rel = lambda p: str(p.relative_to(HOME)) if p.resolve().is_relative_to(HOME.resolve()) else str(p)
    res = {"project": slug, "file": rel(final), "sha256": sha, "ok": not failed, "failed": failed,
           "excepted": excepted, "duration": round(dur, 2), "rules": rules, "sheet": rel(out_dir),
           "images": sheets, "cuts": cuts, "judgment": judgment_rules(),
           "heard": " ".join(w["w"] for w in heard)}
    (out_dir / "check.json").write_text(json.dumps(res, indent=1))
    (out_dir / "verdict.txt").write_text(verdict_text(res))
    emit("check.done", None, project=slug, brand=brand, file=res["file"], sha256=sha, ok=res["ok"],
         failed=failed, failures=failures, excepted=[x["exception"] for x in excepted],
         rules=[{k: v for k, v in r.items() if k not in ("items", "excepted")} for r in rules],
         sheet=res["sheet"], actor=actor)
    return res

CHECK_MARK = {"pass": "pass", "fail": "FAIL", "warn": "warn", "skip": "skip", "excepted": "EXC"}

def verdict_text(r):
    """The check's verdict as text: printed by `studio check`, and verdict.txt in the review
    sheet, so an exception is in plain sight wherever the result is read."""
    out = [f"check {r['file']}  ({r['duration']:.1f}s)  sha256 {r.get('sha256', '?')[:12]}"]
    for x in r["rules"]:
        out.append(f"  {CHECK_MARK[x['status']]:<4}  {x['rule']:<10}  {x['detail']}")
        out += [f"              - {i}" for i in x.get("items", [])]
        out += [f"              ~ EXCEPTION {e['exception']} by {e.get('by') or '?'}: {e['item']}"
                + (f" [declared {e['declares']}" + (f": {e['text']!r}" if e.get("text") else "") + "]"
                   if e.get("declares") else "") + f". Why: {e.get('why')}" for e in x.get("excepted", [])]
    return "\n".join(out) + "\n"

def _font():
    f = next((x for x in FONTS if Path(x).exists()), None)
    return f"fontfile={f}:" if f else ""

def _dt(text):
    """Text safe inside a drawtext='...' in a filtergraph."""
    return re.sub(r"[^\w .?!\-+>()#]", "", text)[:80]

def cut_sheet(path, out, a0, a1, cut, words, regions, title):
    """One review image for the stretch a0-a1 of an export: a header, 12 frames, the waveform
    (speech regions as a green bar), the words heard where they were said, a red line at the cut."""
    W, n, span = 1620, 12, a1 - a0
    x = lambda t: int(round((min(max(t, a0), a1) - a0) / span * W))
    font = _font()
    bars = ",".join(f"drawbox=x={x(r[0])}:y=150:w={max(2, x(r[1]) - x(r[0]))}:h=10:color=0x34c759:t=fill"
                    for r in regions if r[1] > a0 and r[0] < a1) or "null"
    labels = ",".join(f"drawtext={font}text='{_dt(w['w'])}':x={x(w['start'])}:y={6 + 26 * (k % 2)}"
                      f":fontsize=22:fontcolor=white"
                      for k, w in enumerate(w for w in words if a0 <= w["start"] < a1) if _dt(w["w"])) or "null"
    g = (f"[0:v]fps={n / span:.4f},scale={W // n}:-2,setsar=1,tile={n}x1,format=rgb24[fs];"
         f"[0:a]aformat=channel_layouts=mono,showwavespic=s={W}x160:colors=0x9fd3ff:scale=sqrt,format=rgb24,{bars}[wv];"
         f"color=c=0x111111:s={W}x60:d=1,format=rgb24,{labels}[lb];"
         f"color=c=0x222222:s={W}x36:d=1,format=rgb24,drawtext={font}text='{_dt(title)}':x=8:y=7:fontsize=22:fontcolor=white[hd];"
         f"[hd][fs][wv][lb]vstack=inputs=4,drawbox=x={max(0, x(cut) - 1)}:y=36:w=2:h=ih:color=red:t=fill")
    sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{a0:.3f}", "-t", f"{span:.3f}", "-i", str(path),
        "-filter_complex", g, "-frames:v", "1", str(out)])

def contact_sheet(path, out, dur, n=24):
    """The whole export as 24 frames (6 x 4), each stamped with its time."""
    sh(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf",
        f"fps={n / max(dur, 0.1):.4f},scale=180:-2,drawtext={_font()}text='%{{pts\\:hms}}':x=4:y=4"
        f":fontsize=18:fontcolor=white:box=1:boxcolor=black@0.5,tile=6x{math.ceil(n / 6)}",
        "-frames:v", "1", str(out)])


# ---------- share: bundle / import ------------------------------------------
# A bundle is one folder a teammate can open on another Mac (zipped for Google Drive):
#   footage/<Project>/<nice name>   the original files, byte for byte (APFS clones when local)
#   library/                        a studio library minus what can be remade (sources,
#                                   proxies, clips): the event log, words, frames, moments,
#                                   post drafts, stories and the finished exports
#   manifest.json  README.md  moments.csv
# `studio import` checks every file against the manifest, then merges it into this library.

BUNDLE_FORMAT = 1
# what media/<id>/ keeps out of a bundle: the source travels in footage/, the rest is remade
_BUNDLE_SKIP = {"proxy.mp4", "proxy.parts", "clips", "audio.wav", "whisper.json"}
_BUNDLE_MAX_META = 50 << 20            # "small metadata": a file bigger than this stays home
# step events whose files a bundle doesn't carry (import remakes the proxy; `studio cut` the
# clips), and old failures that would only confuse the teammate
_BUNDLE_DROP = {"step.proxy", "step.cut", "step.failed"}
_STEP_FILE = {"step.transcribe": "transcript.json", "step.keyframes": "keyframes/index.json",
              "step.speech": "speech.json",
              "step.moments": "moments.json", "step.draft": "drafts.json"}
_MEDIA_EVENTS = {"ingested", "brand.set", "transcript.edited", "draft.edited"}
BIG_ZIP = 15 * 10**9

def _say(msg):
    print(msg, flush=True)

class BundleError(RuntimeError):
    """A bundle that can't be made or imported; the message says why, in plain words."""

def studio_version():
    try:
        return sh(["git", "-C", str(HERE), "describe", "--always", "--dirty", "--tags"]).strip() or "unknown"
    except (RuntimeError, OSError):
        return "unknown"

def safe_name(s, limit=120):
    """A file or folder name that works on any Mac (and survives a zip and Google Drive)."""
    s = re.sub(r"[\x00-\x1f/:\\]+", "-", str(s)).strip(" .")
    return s[:limit].strip(" .") or "untitled"

def fmt_size(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000

def _media_meta_files(mid):
    """(path, rel) for the small files in media/<id>/ that travel in a bundle."""
    d, out = mdir(mid), []
    if not d.is_dir():
        return out
    for root, dirs, names in os.walk(d):
        r = Path(root)
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and not (r == d and x in _BUNDLE_SKIP))
        for n in sorted(names):
            if n.startswith(".") or (r == d and (n in _BUNDLE_SKIP or n.startswith("source."))):
                continue
            p = r / n
            if p.is_file() and p.stat().st_size <= _BUNDLE_MAX_META:
                out.append((p, p.relative_to(d)))
    return out

def bundle_select(st, slugs):
    """The media a bundle of these projects carries: everything filed under them, plus any
    footage their stories use that is filed under another project. Returns (media ids,
    every project involved: the chosen ones first, then the homes of borrowed footage)."""
    want = []
    for slug in slugs:
        want += [m for m, it in st["media"].items() if it["project"] == slug]
        want += [m for c in st["projects"][slug]["timeline"]["clips"]
                 for m in [c["media"], *(x.get("media") for x in c.get("cover") or [])] if m in st["media"]]
    mids = list(dict.fromkeys(want))
    return mids, list(dict.fromkeys([*slugs, *(st["media"][m]["project"] for m in mids)]))

def footage_names(st, mids, projs, slugs=()):
    """media id -> footage/<Project Name>/<nice name>. The nice name keeps the camera's own
    file name; footage that is in a shared story (its own project's first, else the first one
    that borrows it) is prefixed with its place there and the operator's label for it:
    `03 - BMW walk - DJI_20261108120758_0064_D.MP4`."""
    folders, taken = {}, set()
    for slug in projs:
        f = safe_name((st["projects"].get(slug) or {}).get("name") or slug, 80)
        if f.lower() in taken:
            f = safe_name(f"{f} ({slug})", 100)
        taken.add(f.lower())
        folders[slug] = f
    names, used = {}, set()
    for m in mids:
        it = st["media"][m]
        home = it["project"]
        ext = Path(it["path"]).suffix
        orig = Path(str(it.get("name") or f"{m}{ext}")).name
        if Path(orig).suffix.lower() != ext.lower():
            orig += ext
        pos = None
        for slug in dict.fromkeys([*([home] if home in slugs else []), *slugs]):
            clips = ((st["projects"].get(slug) or {}).get("timeline") or {}).get("clips", [])
            pos = next((i for i, c in enumerate(clips) if c["media"] == m), None)
            if pos is not None:
                break
        prefix = ""
        if pos is not None:
            label = next((c["label"] for c in clips if c["media"] == m and c.get("label")), None)
            prefix = f"{pos + 1:02d} - " + (f"{safe_name(label, 60)} - " if label else "")
        n = safe_name(prefix + orig, 200)
        stem, suf, k = Path(n).stem, Path(n).suffix, 2
        while (folders[home].lower(), n.lower()) in used:     # APFS ignores case
            n, k = f"{stem} ({k}){suf}", k + 1
        used.add((folders[home].lower(), n.lower()))
        names[m] = f"footage/{folders[home]}/{n}"
    return names

def bundle_events(st, slugs, mids, projs):
    """The slice of the event log a bundle carries: the chosen projects (creation, every
    story save, notes, exports) and the history of their media, minus jobs, progress and
    the steps whose files stay behind."""
    keep, chosen, projs = set(mids), set(slugs), set(projs)
    notes, out = set(), []
    for ev in events():
        t, m, d = ev.get("type"), ev.get("media"), ev.get("data") or {}
        ok = False
        if t == "project.created":
            ok = d.get("slug") in projs
        elif t == "timeline.saved":
            ok = d.get("project") in chosen
        elif t == "render.done":
            ok = d.get("project") in chosen and (HOME / str(d.get("file", ""))).is_file()
        elif t == "note":
            ok = d.get("project") in chosen
            if ok:
                notes.add(d.get("id") or ev["ts"])
        elif t == "note.ack":
            ids = [i for i in d.get("ids") or [] if i in notes]
            if ids:
                ok, ev = True, {**ev, "data": {**d, "ids": ids}}
        elif m in keep and (t in _MEDIA_EVENTS or t.startswith("step.")) and t not in _BUNDLE_DROP:
            ok = True
            if t in _STEP_FILE and not d.get("skipped") and not (mdir(m) / _STEP_FILE[t]).exists():
                ok = False
            if t == "ingested":           # filed where it is now; no path from this Mac
                it = st["media"][m]
                ev = {**ev, "data": {**d, "project": it["project"],
                                     "path": f"media/{m}/{Path(it['path']).name}"}}
        if ok:
            out.append(ev)
    return out

def _md(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()

def _bundle_readme(man, st, chosen):
    projs = man["projects"]
    title = ", ".join(p["name"] for p in projs if p["shared"])
    n_m = sum(x["moments"] for x in man["media"])
    total = sum(x["bytes"] for x in man["media"])
    L = [f"# Shared footage: {title}", "",
         f"Made with studio on {man['created'][:10]} (studio {man['studio']}). "
         f"{len(man['media'])} video/audio file{'s' if len(man['media']) != 1 else ''} "
         f"({fmt_size(total)}), {n_m} moment{'s' if n_m != 1 else ''} picked out, "
         f"and everything done to them so far.", "",
         "## What's inside", "",
         "- `footage/` — the original files, exactly as they came off the camera or phone, one "
         "folder per project. A number in front of a name is its place in the project's story.",
         "- `library/` — the studio's work on them: the words spoken in each file (with timings), "
         "frames, the moments worth cutting, post drafts, each project's story, notes, and the "
         "finished videos (`library/projects/<project>/exports/`).",
         "- `moments.csv` — every moment in one table. Opens in Numbers, Excel or Google Sheets.",
         "- `manifest.json` — what each file is, with a fingerprint (sha256) so an import can tell "
         "it arrived whole.", "",
         "## Open it in studio", "",
         "1. Unzip it, if it came as a .zip (double-click it in Finder).",
         "2. Get studio, once (needs nix): `git clone https://github.com/gudnuf/studio ~/studio`",
         "3. Either run `~/studio/studio import \"/path/to/this folder\"`, or open the Studio app "
         "and choose File ▸ Import… (or the **Import** button next to the project list) and pick "
         "this folder.", "",
         "Import checks every file against its fingerprint first and stops if one is damaged or "
         "missing. It never changes this folder. Previews are remade on your Mac afterwards (a "
         "few minutes for a day of 4K); the words, moments, drafts and stories are there "
         "straight away. Importing the same bundle twice changes nothing.", ""]
    for p in projs:
        mine = [x for x in man["media"] if x["project"] == p["slug"]]
        L += [f"## {p['name']}", ""]
        if p["shared"]:
            L.append(f"Story: {p['clips']} clip{'s' if p['clips'] != 1 else ''}, "
                     f"{fmt_t(p['story_seconds'])} long." if p["clips"] else "Story: not started.")
            for e in p["exports"]:
                L.append(f"Finished video: `{e}`")
        else:
            L.append("Only the footage the stories above borrow from this project is included.")
        L += ["", "| Footage | Length | # | Moment | Time | Hook |", "|---|---|---|---|---|---|"]
        for x in mine:
            ms = load_json(mdir(x["id"]) / "moments.json", [])
            name = f"`{Path(x['footage']).name}`"
            if not ms:
                L.append(f"| {name} | {fmt_t(x['duration'])} | | (no moments picked yet) | | |")
            for i, m in enumerate(ms):
                L.append(f"| {name if i == 0 else ''} | {fmt_t(x['duration']) if i == 0 else ''} | "
                         f"{m.get('n', i + 1)} | {_md(m.get('title'))} | "
                         f"{fmt_t(m['start'])}–{fmt_t(m['end'])} | {_md(m.get('hook'))} |")
        L.append("")
    L += ["## Browse without the app", "",
          "Everything is plain text next to the footage:", "",
          "- `moments.csv` — all moments: file, start/end in seconds, title, hook, why.",
          "- `library/media/<id>/moments.json` — one file's moments; `transcript.txt` is what was "
          "said, `transcript.json` the same with a time for every word, `drafts.json` the post "
          "drafts, `keyframes/` a frame every few seconds.",
          "- `manifest.json` maps each `<id>` to its file in `footage/`:", "",
          "| id | Footage |", "|---|---|"]
    L += [f"| {x['id']} | `{x['footage']}` |" for x in man["media"]]
    L += ["", "To watch a moment, open the file in QuickTime and jump to its start time.", ""]
    return "\n".join(L)

_CSV_COLS = ["project", "file", "footage", "moment", "start", "end", "duration", "title", "hook",
             "pillar", "angle", "kind", "why"]

def _write_moments_csv(path, man, names):
    import csv
    pname = {p["slug"]: p["name"] for p in man["projects"]}
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM: Excel reads UTF-8
        w = csv.writer(f)
        w.writerow(_CSV_COLS)
        for x in man["media"]:
            for m in load_json(mdir(x["id"]) / "moments.json", []):
                try:
                    a, b = float(m["start"]), float(m["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                w.writerow([pname.get(x["project"], x["project"]), x["name"], x["footage"],
                            m.get("n", ""), f"{a:.2f}", f"{b:.2f}", f"{b - a:.2f}",
                            *(str(m.get(k) or "") for k in ("title", "hook", "pillar", "angle", "kind", "why"))])

def _dir_bytes(d):
    return sum(p.stat().st_size for p in Path(d).rglob("*") if p.is_file())

def bundle(out, slugs=None, all_projects=False, zip_it=False, log=None):
    """Write a self-contained bundle of these projects to `out` (a new folder). Sources are
    APFS clones when `out` is on the same disk (no extra space), copies otherwise; nothing
    in the library changes. Returns a summary dict."""
    log = log or _say
    st = state()
    if all_projects:
        slugs = [s for s, p in st["projects"].items()
                 if p["timeline"]["clips"] or any(it["project"] == s for it in st["media"].values())]
    slugs = list(dict.fromkeys(slugs or []))
    if not slugs:
        raise BundleError("say which projects to share: -p SLUG (see `studio project ls`), or --all")
    bad = [s for s in slugs if s not in st["projects"]]
    if bad:
        raise BundleError(f"no project {', '.join(bad)} (see `studio project ls`)")
    out = Path(out).expanduser().resolve()
    if out.is_relative_to(HOME.resolve()):
        raise BundleError(f"put the bundle somewhere outside the library ({HOME}), e.g. ~/Desktop")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BundleError(f"{out} already exists; pick a new folder name")
    mids, projs = bundle_select(st, slugs)
    lost = [st["media"][m]["name"] for m in mids if not Path(st["media"][m]["path"]).is_file()]
    if lost:
        raise BundleError(f"these sources are missing from the library, so they can't be shared: {', '.join(lost)}")
    names = footage_names(st, mids, projs, slugs)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.with_name(f".{out.name}.partial-{os.getpid()}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    how = {"clone": 0, "copy": 0}
    try:
        media = []
        for i, m in enumerate(mids):
            it = st["media"][m]
            dst = work / names[m]
            dst.parent.mkdir(parents=True, exist_ok=True)
            log(f"  [{i + 1}/{len(mids)}] {names[m]}")
            how[clone_or_copy(it["path"], dst)] += 1
            digest = receipt(dst)
            if it.get("sha256") and digest != it["sha256"]:
                raise BundleError(f"{it['name']} in the library doesn't match the fingerprint it had "
                                  f"when it was added (the file changed or is damaged); not sharing it")
            lm = work / "library" / "media" / m
            for p, rel in _media_meta_files(m):
                (lm / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, lm / rel)
            media.append({"id": m, "sha256": digest, "name": it.get("name"), "footage": names[m],
                          "source": Path(it["path"]).name, "project": it["project"],
                          "duration": it.get("duration"), "width": it.get("width"),
                          "height": it.get("height"), "has_audio": bool(it.get("has_audio")),
                          "has_video": bool(it.get("has_video")), "bytes": dst.stat().st_size,
                          "moments": len(load_json(mdir(m) / "moments.json", []))})
        projects = []
        for slug in projs:
            pj = st["projects"][slug]
            shared = slug in slugs
            exports = []
            if shared:
                pd = work / "library" / "projects" / slug
                pd.mkdir(parents=True, exist_ok=True)
                (pd / "timeline.json").write_text(json.dumps(pj["timeline"], indent=1))
                for e in sorted((pdir(slug) / "exports").glob("*.mp4")):
                    (pd / "exports").mkdir(exist_ok=True)
                    how[clone_or_copy(e, pd / "exports" / e.name)] += 1
                    exports.append(f"library/projects/{slug}/exports/{e.name}")
            clips = pj["timeline"]["clips"] if shared else []
            projects.append({"slug": slug, "name": pj["name"], "brand": pj.get("brand"),
                             "shared": shared, "clips": len(clips),
                             "story_seconds": round(sum(c["out"] - c["in"] for c in clips), 2),
                             "aspect": pj["timeline"].get("aspect"), "exports": exports,
                             "media": sum(1 for x in media if x["project"] == slug)})
        evs = bundle_events(st, slugs, mids, projs)
        (work / "library" / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
        man = {"format": "studio-bundle", "version": BUNDLE_FORMAT,
               "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "studio": studio_version(), "projects": projects, "media": media}
        (work / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=False))
        _write_moments_csv(work / "moments.csv", man, names)
        (work / "README.md").write_text(_bundle_readme(man, st, slugs))
        if out.exists():
            out.rmdir()                   # an empty folder the operator picked
        os.replace(work, out)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    size = _dir_bytes(out)
    res = {"dir": str(out), "bytes": size, "footage_bytes": sum(x["bytes"] for x in media),
           "library_bytes": _dir_bytes(out / "library"), "media": len(media),
           "moments": sum(x["moments"] for x in media), "projects": [p["slug"] for p in projects if p["shared"]],
           "cloned": how["clone"], "copied": how["copy"], "events": len(evs)}
    if zip_it:
        res["zip"] = str(bundle_zip(out, log=log))
        res["zip_bytes"] = Path(res["zip"]).stat().st_size
    return res

def bundle_zip(d, log=None):
    """d.zip next to d, stored (video doesn't compress; storing is as fast as copying)."""
    import zipfile
    d = Path(d)
    dst = d.with_name(d.name + ".zip")
    tmp = d.with_name(f".{d.name}.zip.partial-{os.getpid()}")
    (log or _say)(f"  zipping → {dst.name} (stored, no compression)")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    z.write(p, f"{d.name}/{p.relative_to(d).as_posix()}")
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dst

# -- import --

_HEX = re.compile(r"^[0-9a-f]+$")

def find_bundle(src):
    """The bundle folder: src itself, or the one bundle inside it (a zip unpacked by
    Finder or Google Drive often adds a folder level)."""
    src = Path(src).expanduser().resolve()
    if src.is_file() and src.suffix.lower() == ".zip":
        raise BundleError(f"{src.name} is still zipped: double-click it in Finder, then import the folder it makes")
    if not src.is_dir():
        raise BundleError(f"no folder at {src}")
    if (src / "manifest.json").is_file():
        return src
    inner = [d for d in src.iterdir() if d.is_dir() and (d / "manifest.json").is_file()]
    if len(inner) == 1:
        return inner[0]
    raise BundleError(f"{src} isn't a studio bundle (no manifest.json in it)")

def _read_manifest(b):
    try:
        man = json.loads((b / "manifest.json").read_text())
    except ValueError as e:
        raise BundleError(f"manifest.json in {b.name} is damaged ({e})")
    if man.get("format") != "studio-bundle":
        raise BundleError(f"{b.name} isn't a studio bundle")
    if int(man.get("version", 0)) > BUNDLE_FORMAT:
        raise BundleError(f"{b.name} was made by a newer studio (bundle format {man.get('version')}); "
                          "update studio (git pull) and try again")
    for x in man.get("media", []):
        mid, sha = str(x.get("id", "")), str(x.get("sha256", ""))
        if len(mid) != 12 or not _HEX.match(mid) or len(sha) != 64 or not sha.startswith(mid):
            raise BundleError(f"manifest.json lists a file with a bad id: {mid!r}")
        p = (b / str(x.get("footage", ""))).resolve()
        if not p.is_relative_to(b) or not str(x.get("footage", "")).startswith("footage/"):
            raise BundleError(f"manifest.json points outside the bundle: {x.get('footage')!r}")
        if not re.match(r"^source\.[A-Za-z0-9]{1,8}$", str(x.get("source", ""))):
            x["source"] = "source" + Path(str(x.get("footage"))).suffix.lower()
    return man

def _bundle_log(b):
    out = []
    p = b / "library" / "events.jsonl"
    if not p.exists():
        return out
    for line in p.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
            if isinstance(ev, dict) and isinstance(ev.get("type"), str) and isinstance(ev.get("data"), dict):
                out.append(ev)
        except ValueError:
            continue
    return out

def _sig(ev):
    return json.dumps([ev.get("ts"), ev.get("type"), ev.get("media"), ev.get("data")], sort_keys=True)

def server_running():
    """The studio server serving this library, if one is up: its .server.json, confirmed
    by /api/health (a stale file from a crash is ignored)."""
    import urllib.request
    try:
        info = json.loads((HOME / ".server.json").read_text())
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/api/health", timeout=2) as r:
            h = json.loads(r.read())
        if h.get("app") == "studio" and Path(h.get("home", "")).resolve() == HOME.resolve():
            return f"http://127.0.0.1:{int(info['port'])}/"
    except Exception:  # noqa: BLE001 — no server, a stale file, another app on that port
        pass
    return None

def import_bundle(src, log=None, prepare=True, actor=None):
    """Merge a bundle into this library. Every footage file is checked against the
    manifest's sha256 first; one damaged or missing file stops the import before anything
    changes. The bundle is only read. Media already here are left as they are; a project
    whose slug is taken by a different project comes in under a new slug. Idempotent.
    prepare: remake previews (and frames/words if missing) — through the running server if
    there is one, else here. Returns a summary dict."""
    log = log or _say
    b = find_bundle(src)
    man = _read_manifest(b)
    st = state()
    have = set(st["media"])
    new = [x for x in man.get("media", []) if x["id"] not in have]
    missing, broken = [], []
    for i, x in enumerate(new):
        p = b / x["footage"]
        if not p.is_file():
            missing.append(x["footage"])
            continue
        log(f"  checking [{i + 1}/{len(new)}] {x['footage']}")
        if receipt(p) != x["sha256"]:
            broken.append(x["footage"])
    if missing or broken:
        msg = [f"not importing {b.name}: nothing was changed."]
        if broken:
            msg.append("These files are damaged (they don't match the fingerprint they were shared with; "
                       "download or unzip them again):")
            msg += [f"  {f}" for f in broken]
        if missing:
            msg.append("These files are missing from the bundle:")
            msg += [f"  {f}" for f in missing]
        raise BundleError("\n".join(msg))

    bevs = _bundle_log(b)
    # projects: the same project (same name, created the same second) merges into itself; a
    # different project with a slug that's taken here gets the next free slug
    mapping, renamed = {}, []
    taken = set(st["projects"])
    for ev in bevs:
        if ev["type"] != "project.created":
            continue
        d = ev["data"]
        slug = str(d.get("slug") or "")
        if slug in mapping:
            continue
        clean = slugify(slug) if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug) else slug
        same = next((s for s, p in st["projects"].items()
                     if p.get("created") == ev["ts"] and p.get("name") == d.get("name")), None)
        if same:
            mapping[slug] = same
            continue
        new_slug, n = clean, 2
        while new_slug in taken:
            new_slug, n = f"{clean}-{n}", n + 1
        taken.add(new_slug)
        mapping[slug] = new_slug
        if new_slug != slug:
            renamed.append({"from": slug, "to": new_slug, "name": d.get("name")})
    mp = lambda s: mapping.get(s, s)

    by_id = {x["id"]: x for x in new}
    known = {_sig(ev) for ev in events() if ev.get("type") in
             ("project.created", "timeline.saved", "render.done", "note", "note.ack")}
    out_evs = []
    for ev in bevs:
        t, m, d = ev["type"], ev.get("media"), dict(ev["data"])
        if m is not None:
            if m not in by_id or not (t in _MEDIA_EVENTS or t.startswith("step.")) or t in _BUNDLE_DROP:
                continue                  # already here (theirs stays as it is), or not ours
            if t == "ingested":
                d["project"] = mp(d.get("project"))
                d["path"] = str(mdir(m) / by_id[m]["source"])
            out_evs.append({**ev, "data": d, "imported": True})
            continue
        if t not in ("project.created", "timeline.saved", "render.done", "note", "note.ack"):
            continue
        if t == "project.created":
            d["slug"] = mp(d.get("slug"))
        elif d.get("project") is not None:
            d["project"] = mp(d["project"])
        if t == "note" and isinstance(d.get("focus"), dict) and d["focus"].get("project"):
            d["focus"] = {**d["focus"], "project": mp(d["focus"]["project"])}
        if t == "render.done":
            d["file"] = f"projects/{d['project']}/exports/{Path(str(d.get('file', ''))).name}"
        e2 = {**ev, "data": d, "imported": True}   # history, not news: the UI doesn't toast it
        if _sig(e2) not in known:
            known.add(_sig(e2))
            out_evs.append(e2)

    # files first, then the events that point at them
    for x in new:
        d = mdir(x["id"])
        d.mkdir(parents=True, exist_ok=True)
        marker = d / ".ingesting"
        marker.write_text(json.dumps({"pid": os.getpid(), "from": str(b / x["footage"]),
                                      "name": x.get("name"), "moved": False}))
        dest = d / x["source"]
        if not (dest.is_file() and dest.stat().st_size == x.get("bytes")):
            tmp = d / f".source.{os.getpid()}{dest.suffix}"
            tmp.unlink(missing_ok=True)
            clone_or_copy(b / x["footage"], tmp)
            os.replace(tmp, dest)
        lm = b / "library" / "media" / x["id"]
        if lm.is_dir():
            for p in sorted(lm.rglob("*")):
                rel = p.relative_to(lm)
                if p.is_file() and not p.is_symlink() and not rel.name.startswith(".") \
                        and not rel.parts[0].startswith("source.") and rel.parts[0] not in _BUNDLE_SKIP:
                    q = d / rel
                    if not q.exists():
                        q.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, q)
        log(f"  added {x['footage']} → {x['id']}")
    for old, slug in mapping.items():
        ex = b / "library" / "projects" / old / "exports"
        if ex.is_dir():
            for p in sorted(ex.glob("*.mp4")):
                q = pdir(slug) / "exports" / p.name
                if not q.exists():
                    q.parent.mkdir(parents=True, exist_ok=True)
                    clone_or_copy(p, q)
        pdir(slug).mkdir(parents=True, exist_ok=True)

    append_events(out_evs)
    for x in new:
        (mdir(x["id"]) / ".ingesting").unlink(missing_ok=True)
    st = state()
    for slug in set(mapping.values()):
        if slug in st["projects"] and any(e["type"] == "timeline.saved" and e["data"].get("project") == slug
                                          for e in out_evs):
            (pdir(slug) / "timeline.json").write_text(json.dumps(st["projects"][slug]["timeline"], indent=1))
    res = {"bundle": str(b), "media": [x["id"] for x in new],
           "already": [x["id"] for x in man.get("media", []) if x["id"] in have],
           "projects": mapping, "renamed": renamed, "events": len(out_evs), "prepare": None}
    if out_evs or new:
        emit("bundle.imported", None, bundle=b.name, files=res["media"], projects=mapping,
             renamed=renamed, actor=actor or actor_now())
    todo = [m for m in res["media"] if m in st["media"]
            and any(s not in st["media"][m]["steps"] for s in LOCAL_STEPS)]
    if prepare and todo:
        res["prepare"] = prepare_imported(todo, log)
    return res

def prepare_imported(ids, log=None):
    """Previews (and frames/words if the bundle had none) for imported footage: queued on
    the running server for this library if there is one, else made here with progress."""
    log = log or _say
    url = server_running()
    if url:
        import urllib.request
        body = json.dumps({"ids": ids, "steps": LOCAL_STEPS}).encode()
        r = urllib.request.Request(url + "api/run", data=body, method="POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            job = json.loads(resp.read()).get("job")
        log(f"  previews: queued on the running studio ({url}), job {job}")
        return {"server": url, "job": job}
    log(f"  making previews for {len(ids)} file{'s' if len(ids) != 1 else ''} (no studio window open)…")
    need("ffmpeg")
    run_pipeline(ids, LOCAL_STEPS)
    return {"server": None, "ids": ids}


# ---------- review page -----------------------------------------------------

def review():
    lib = library()
    esc = html.escape
    cards = []
    for mid, item in sorted(lib.items(), key=lambda kv: kv[1]["name"]):
        d = mdir(mid)
        rel = lambda p: f"media/{mid}/{p}"
        ms = load_json(d / "moments.json", [])
        dr = load_json(d / "drafts.json", {})
        posts = {p["n"]: p for p in dr.get("posts", [])}
        done = ", ".join(s for s in STEPS if s in item["steps"]) or "ingested"
        rows = []
        for m in ms:
            p = posts.get(m["n"], {})
            clip = d / "clips" / f"{m['n']:02d}.mp4"
            video = (f'<video src="{rel(f"clips/{m["n"]:02d}.mp4")}" controls preload="metadata">'
                     f'</video>') if clip.exists() else '<div class="noclip">no clip</div>'
            fields = "".join(
                f'<label>{k}<button onclick="cp(this)">copy</button></label>'
                f'<textarea rows="{4 if k != "x" else 3}">{esc(p.get(k, ""))}</textarea>'
                for k in ("short", "x", "linkedin", "title") if p.get(k))
            rows.append(f"""<div class="moment">{video}<div class="txt">
  <h3>#{m['n']} {esc(m['title'])} <span>{fmt_t(m['start'])}–{fmt_t(m['end'])} · {esc(m.get('pillar',''))} · {esc(m.get('angle',''))}</span></h3>
  <p class="hook">“{esc(m['hook'])}”</p><p class="why">{esc(m['why'])}</p>{fields}</div></div>""")
        extra = ""
        if dr.get("newsletter"):
            extra += (f'<label>newsletter<button onclick="cp(this)">copy</button></label>'
                      f'<textarea rows="6">{esc(dr["newsletter"])}</textarea>')
        if dr.get("ideas"):
            extra += "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in dr["ideas"]) + "</ul>"
        warn = (f'<p class="fail">{esc(item["error"]["step"])} failed: '
                f'{esc(item["error"]["error"][:300])}</p>') if item.get("error") else ""
        tx = d / "transcript.txt"
        cards.append(f"""<section><h2>{esc(item['name'])} <span>{mid} · {fmt_t(item['duration'])} · {done}</span></h2>
{warn}{''.join(rows) or '<p class="why">no moments yet</p>'}<div class="extra">{extra}</div>
{f'<details><summary>transcript</summary><pre>{esc(tx.read_text())}</pre></details>' if tx.exists() else ''}</section>""")
    page = f"""<!doctype html><meta charset="utf-8"><title>Studio review</title>
<style>
body{{font:15px/1.45 -apple-system,system-ui,sans-serif;background:#F6F4EC;color:#17150F;margin:0;padding:28px}}
h1{{font-size:20px;letter-spacing:.04em}} h1 span,h2 span,h3 span{{font-weight:400;color:#726E62;font-size:12px;font-family:ui-monospace,monospace}}
section{{background:#FBFAF4;border:1px solid #DAD5C6;padding:18px 20px;margin:0 0 26px}}
h2{{font-size:17px;margin:0 0 12px}} h3{{font-size:15px;margin:0 0 6px}}
.moment{{display:grid;grid-template-columns:240px 1fr;gap:18px;padding:14px 0;border-top:1px solid #EFEBDD}}
video,.noclip{{width:240px;aspect-ratio:9/16;background:#17150F;color:#A9A497;display:grid;place-items:center}}
.hook{{font-family:Georgia,serif;font-size:17px;margin:4px 0}} .why{{color:#46433A;margin:0 0 8px}}
label{{display:flex;justify-content:space-between;font:600 11px ui-monospace,monospace;text-transform:uppercase;letter-spacing:.12em;color:#9A6A00;margin-top:8px}}
textarea{{width:100%;box-sizing:border-box;font:14px/1.4 -apple-system,system-ui,sans-serif;border:1px solid #DAD5C6;background:#fff;padding:8px}}
button{{font:600 10px ui-monospace,monospace;border:1px solid #DAD5C6;background:#FAF1D9;cursor:pointer}}
.fail{{color:#B23B2E}} pre{{white-space:pre-wrap;font-size:12px}}
</style>
<h1>STUDIO REVIEW <span>{len(lib)} files · generated {datetime.datetime.now():%Y-%m-%d %H:%M} · drafts are raw: rewrite in your voice</span></h1>
{''.join(cards) or '<p>Library is empty. Drop footage in inbox/ and run <code>studio run</code>.</p>'}
<script>function cp(b){{const t=b.parentElement.nextElementSibling;navigator.clipboard.writeText(t.value);b.textContent='copied';setTimeout(()=>b.textContent='copy',1200)}}</script>"""
    out = HOME / "review.html"
    out.write_text(page)
    emit("review.written", None, path=str(out), files=len(lib))
    return out


# ---------- cli -------------------------------------------------------------

# Who is at the CLI: the agent, or a person in a terminal ("cli"). Agents mark the shells
# they run commands in: Claude Code sets CLAUDECODE, Codex CODEX_THREAD_ID (and CODEX_SANDBOX
# when sandboxed), Goose GOOSE_TERMINAL. Any other agent: STUDIO_ACTOR=agent (or =cli to say
# it's you). The agent's events say actor "agent"; libraries from before say "claude", and
# everything that reads the log takes any actor but "operator" and "cli" to be the agent.
AGENT_MARKERS = ("CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX", "GOOSE_TERMINAL")

def by_agent():
    who = os.environ.get("STUDIO_ACTOR", "").strip().lower()
    if who:
        return who != "cli"
    return any(os.environ.get(k) for k in AGENT_MARKERS)

def actor_now():
    return "agent" if by_agent() else "cli"

def is_agent(actor):
    return bool(actor) and actor not in ("operator", "cli")

def clip_index(clips, x):
    """A clip by number (1-based, as `timeline show` prints and the UI says "Clip 3 of 4";
    -1 is the last) or by id / unique id prefix of 4+ characters (as `studio focus`
    reports). Ids are what to use when the operator may be editing too: a number shifts
    when they reorder, an id never does. Returns the 0-based index."""
    x = str(x)
    if re.fullmatch(r"-?\d{1,3}", x):
        n = int(x)
        if n == 0:
            sys.exit("clip numbers start at 1 (clip 1 is the first, as in the UI); "
                     "see `timeline show`")
        if not 1 <= abs(n) <= len(clips):
            sys.exit(f"no clip {n} (the story has {len(clips)}"
                     f"{f': 1–{len(clips)}' if clips else ''}; see `timeline show`)")
        return n - 1 if n > 0 else len(clips) + n
    hits = [k for k, c in enumerate(clips) if c["id"].startswith(x)]
    if len(hits) != 1:
        sys.exit(f"no clip with id {x}" if not hits else f"clip id {x} is ambiguous; use more of it")
    return hits[0]

def video_arg(st, mid, spec):
    """The one media id mid (a prefix) names, which must have a picture."""
    hits = [m for m in st["media"] if m.startswith(mid)] if mid else []
    if len(hits) != 1:
        sys.exit(f"no media {spec}" if not hits else f"media id {mid} is ambiguous")
    item = st["media"][hits[0]]
    if not item.get("has_video"):
        sys.exit(f"{item['name']} ({hits[0]}) is audio-only; it can't go on the Story")
    return hits[0]

TIGHT_TO = 0.12      # what tighten leaves of a long pause: 60% after the last word, 40% before the next

def tighten_clip(c, regions, mx=0.35, to=TIGHT_TO, lib=None, notes=None):
    """c split at every pause inside its speech longer than mx, each pause cut down to `to`
    seconds of its own silence (cuts in silence only; render fades every part). The pieces
    keep the clip's settings; each pause cut is cover_cut, the same as the UI's "Cut this out":
    every piece of b-roll stays laid over its words, plays on unbroken across a cut, and one
    laid entirely inside a removed pause is dropped (said in `notes`, a list, if given)."""
    a, b, pace = c["in"], c["out"], clip_pace(c)
    mx, to = mx * pace, to * pace     # both are export seconds; a paced clip's pauses play shorter
    mine = [r for r in regions if min(b, r[1]) - max(a, r[0]) >= SNAP_EDGE]
    keep, s0 = [], a
    for r0, r1 in zip(mine, mine[1:]):
        if r1[0] - r0[1] > mx:
            keep.append((s0, round(r0[1] + to * 0.6, 3)))
            s0 = round(r1[0] - to * 0.4, 3)
    keep.append((s0, b))
    if len(keep) == 1:
        return [c]
    out, cover = [], c.get("cover") or []
    for k, (s, e) in enumerate(keep):
        piece = {**c, "in": s, "out": e, **({"id": clip_id()} if k else {})}
        piece.pop("cover", None)
        if k + 1 < len(keep):
            mine_, cover, said = cover_cut(cover, e, keep[k + 1][0], pace, s, lib)
            if notes is not None:
                notes += said
        else:
            mine_ = cover
        if mine_:
            piece["cover"] = mine_
        out.append(piece)
    return out

COVER_USAGE = ("timeline cover PROJECT CLIP --with MEDIA IN [OUT] [--at T] "
               "[--until U | --until-end | --fill] [--fit fill|fit]")

def lay_cover(lib, c, mid, pin, pout=None, at=None, until=None, until_end=False, fill=False,
              fit=None, tips=True):
    """Lay a piece on clip c, from `timeline cover`'s arguments in reel time (at/until: seconds
    into the clip as it plays, after its pace; pin/pout: seconds of media mid, which plays at
    normal speed, so pout - pin is also the seconds it covers). Converts to the stored shape
    (laid over the clip's source seconds) and judges it by cover_frames and cover_plan, the
    counts every save and the render use, so what is laid is what is kept and drawn.
    Returns {"cover", "piece", "x" (its plan entry)} or {"error", "tips": [(text, args)]};
    every tip is tried before it is offered (args: the arguments that change)."""
    it = lib.get(mid) or {}
    name, dur = it.get("name", mid), float(it.get("duration") or 0)
    args = dict(pin=pin, pout=pout, at=at, until=until, until_end=until_end, fill=fill)
    for what, x in (("IN", pin), ("OUT", pout), ("--at", at), ("--until", until)):
        if x is not None and not (isinstance(x, (int, float)) and math.isfinite(x)):
            return {"error": f"{what} must be a number of seconds (got {x})", "tips": []}
    ways = [w for w, on in (("OUT", pout is not None), ("--until", until is not None),
                            ("--until-end", until_end), ("--fill", fill)) if on]
    if len(ways) != 1:
        return {"error": "say how far the piece goes, one way: OUT (seconds in MEDIA), --until U "
                         "(seconds into the clip), --until-end, or --fill (the whole clip)"
                         + (f"; got {' and '.join(ways)}" if ways else ""), "tips": []}
    if fill and at:
        return {"error": "--fill covers the whole clip from 0; to start later use --at T --until-end", "tips": []}
    L, pace, n = clip_len(c), clip_pace(c), clip_frames(c)
    start = float(at or 0)
    down = lambda x: math.floor(x * 100 + 1e-6) / 100     # a number as printed, never past it
    instead = (" (instead of OUT)" if pout is not None else " (instead of --fill)" if fill
               else " (instead of --until-end)" if until_end else "")
    def works(**ch):                                     # a tip, tried
        r = lay_cover(lib, c, mid, **{**args, **ch, "fit": fit, "tips": False})
        return "cover" in r
    def offer(make, v, step):          # make(v) = (text, args): the printed number, stepped until it works
        for _ in range(40):
            if v < 0:
                break
            text, ch = make(v)
            if works(**ch):
                return [(text, ch)]
            v = round(v + step, 2)
        return []
    def no(error, *tip_list):                           # tip_list: thunks, run only when asked
        return {"error": error, "tips": [t for f in tip_list for t in f()] if tips else []}
    if not 0 <= pin <= dur - COVER_MIN:
        return no(f"IN {pin:g}s is outside {name} (it is {dur:.2f}s long)")
    if start < 0 or start > L:
        return no(f"--at is seconds into the clip as it plays: 0–{L:.2f}s (the clip plays {L:.2f}s)",
                  lambda: offer(lambda v: (f"--at {v:.2f}", {"at": v, "fill": False, "until_end": until_end or fill}), down(max(0.0, L - COVER_MIN)), -0.01))
    end = start + pout - pin if pout is not None else until
    piece = {"media": mid, "in": cover_r3(pin), "at": cover_r3(c["in"] + start * pace)}
    if end is None:
        piece.update(out=cover_r3(pin + c["out"] - piece["at"]), to_end=True)
    else:
        piece["out"] = cover_r3(pin + max(0.0, end - start) * pace)
    if fit:
        piece["fit"] = fit
    frames = cover_frames(piece, c)
    if frames < COVER_FRAMES:
        return no(f"a piece covers at least {COVER_FRAMES} frames ({COVER_MIN:g}s); this one is laid "
                  f"over {max(0, frames)}",
                  lambda: offer(lambda v: (f"--until {v:.2f}{instead}", {"pout": None, "until": v, "until_end": False, "fill": False}), down(start + 0.11), 0.01))
    if end is not None and end > L + 1e-3:
        return no(f"{start:.2f}–{end:.2f}s runs {end - L:.2f}s past the clip's end (it plays {L:.2f}s)",
                  lambda: offer(lambda v: (f"--until {v:.2f}{instead}", {"pout": None, "until": v, "until_end": False, "fill": False}), down(L), -0.01),
                  lambda: offer(lambda v: (f"OUT {v:.2f}", {"pout": v}), down(pin + L - start), -0.01),
                  lambda: [("--until-end (instead of OUT)" if pout is not None else "--until-end (instead of --until)",
                           {"pout": None, "until": None, "until_end": True})]
                  if works(pout=None, until=None, until_end=True) else [])
    cover = cover_add(c, piece)
    _, info = cover_plan({**c, "cover": cover}, lib)
    x = next(x for x in info if x["piece"] is piece)
    if x["short"] or not x["shown"]:
        why = cover_short_text(x, name) if x["shown"] else "it would show nothing"
        f0, fe = x["laid"]
        need = (fe - f0) / FPS
        return no(f"{name} is too short for what was asked: {why}. Covering {f0 / FPS:.2f}–{fe / FPS:.2f}s "
                  f"of the clip from {pin:.2f}s needs {name} to run to {pin + need:.2f}s; it is {dur:.2f}s long",
                  lambda: offer((lambda v: (f"IN {v:.2f} OUT {v + pout - pin:.2f}",
                                            {"pin": v, "pout": round(v + pout - pin, 2)})) if pout is not None
                                else (lambda v: (f"IN {v:.2f}", {"pin": v})), down(dur - need - 1 / FPS), -0.01),
                  lambda: offer(lambda v: (f"--until {v:.2f}{instead}", {"pout": None, "until": v, "until_end": False, "fill": False}),
                                down(start + dur - pin), -0.01))
    return {"cover": cover, "piece": piece, "x": x}

def cover_piece(st, a, c, i, clips):
    """`timeline cover`: lay_cover from the command line, refused with tips that work, or said
    back in reel time. Returns the clip's new cover."""
    w = a.cover_with
    if not w or len(w) not in (2, 3):
        sys.exit(COVER_USAGE)
    mid = video_arg(st, w[0], w[0])
    name = st["media"][mid]["name"]
    try:
        pin, pout = float(w[1]), (float(w[2]) if len(w) == 3 else None)
    except ValueError:
        sys.exit(f"cover: IN and OUT are seconds in {name}\n{COVER_USAGE}")
    r = lay_cover(st["media"], c, mid, pin, pout, a.at, a.until, a.until_end, a.fill, a.fit)
    if "error" in r:
        tips = "; ".join(t for t, _ in r["tips"])
        sys.exit(f"cover: {r['error']}. Nothing was laid." + (f" Works instead: {tips}" if tips else ""))
    x, pace = r["x"], clip_pace(c)
    at0 = sum(clip_frames(y) for y in clips[:i]) / FPS
    s0, s1 = x["shown"]
    print(f"cover: clip {i + 1} {c['id']}" + (f" (pace {pace:g})" if pace != 1 else "") +
          f": {cover_line(x, c, st['media'], at0)}; under it, the clip's sound "
          f"{c['in'] + s0 / FPS * pace:.2f}–{c['in'] + s1 / FPS * pace:.2f}s of "
          f"{st['media'].get(c['media'], {}).get('name', c['media'])}", file=sys.stderr)
    return r["cover"]

def cover_warnings(before, after, lib, split=False):
    """What an edit did to the story's b-roll, as warnings: a piece it cut (it shows less than
    before: a trim or snap past it, a pace that needs more footage than there is; not said for a
    split or tighten, which share a piece out) and a piece it left stopping before the end it
    was laid to (check fails `cover short`)."""
    seen = lambda c: {(p["media"], p["in"], p["at"]): x for x in cover_plan(c, lib)[1]
                      for p in [x["piece"]]}
    was = {c["id"]: seen(c) for c in before.get("clips", []) if c.get("cover")}
    out = []
    for i, c in enumerate(after.get("clips", []), 1):
        if not c.get("cover"):
            continue
        old = was.get(c["id"], {})
        for k, x in seen(c).items():
            o = old.get(k)
            frames = lambda y: (y["shown"][1] - y["shown"][0]) if y and y["shown"] else 0
            tag = f"warning: clip {i} {c['id']}: cover {x['n']}"
            name = (lib.get(x["piece"]["media"]) or {}).get("name", x["piece"]["media"])
            if x["short"] and not (o and o["short"]):
                out.append(f"{tag} {cover_short_text(x, name)}. check fails \"cover short\" until it's "
                           f"laid again or removed (timeline uncover P {c['id']} {x['n']})")
            elif o and not split and frames(x) == 0 < frames(o):
                out.append(f"{tag} now shows nothing: the clip no longer has the words it was laid over "
                           "(it is kept: trim back and it shows again; timeline uncover to remove it)")
            elif o and not split and frames(x) < frames(o):
                out.append(f"{tag} now shows {frames(x) / FPS:.2f}s of the {frames(o) / FPS:.2f}s it showed "
                           "(the clip no longer has all the words it was laid over)")
    return out

def cmd_timeline(a):
    st = state()
    if a.project not in st["projects"]:
        sys.exit(f"no project {a.project} (studio project ls)")
    pj = st["projects"][a.project]
    tl = pj["timeline"]
    if a.action == "show" and not (a.aspect or a.captions):
        if getattr(a, "json", False):
            # index is 0-based (array position), number is 1-based (what the UI and the
            # human output say); fit_effective is how it renders (fit itself is only present
            # when someone chose it, so piping this back into `timeline set` changes nothing)
            clips = []
            for i, c in enumerate(tl["clips"]):
                it = st["media"].get(c["media"], {})
                clips.append({**c, "index": i, "number": i + 1,
                              "fit_effective": clip_fit(c, it, tl["aspect"])})
            print(json.dumps({**tl, "clips": clips, "rev": pj.get("rev", 0), "project": a.project},
                             indent=1))
            return
        print(f"{a.project}  rev {pj.get('rev', 0)}  aspect {tl['aspect']}  "
              f"captions {'on' if tl['captions'] else 'off'}   (clip numbers are 1-based, as in the UI)")
        total, f = 0.0, 0
        for i, c in enumerate(tl["clips"]):
            it = st["media"].get(c["media"], {})
            fit = clip_fit(c, it, tl["aspect"])
            tags = ("  [show everything]" if fit == "fit" else "") + \
                (f"  (moment {c['moment']})" if c.get("moment") else "")
            voice = clip_voice(c, tl, bool(it.get("has_audio")) and clip_spoken(c))
            tags += (f"  [voice {voice}]" if voice != "off" else "") + \
                (f"  [pace {clip_pace(c):g}]" if clip_pace(c) != 1.0 else "")
            print(f"  {i + 1:>2} {c['id']}  {c['media']}  {fmt_t(c['in'])}–{fmt_t(c['out'])}"
                  f"  ({clip_len(c):.1f}s)  {it.get('name', '?')}  {c.get('label', '')}{tags}")
            for x in cover_plan(c, st["media"])[1]:     # reel time, as the export plays
                print(f"       cover {x['n']}: {cover_line(x, c, st['media'], f / FPS)}")
            total += clip_len(c)
            f += clip_frames(c)
        print(f"  total {fmt_t(total)}")
        return
    if a.action == "clear":
        tl = {**tl, "clips": []}
    elif a.action == "set" and not a.snap:
        tl = json.loads(Path(a.args[0]).read_text() if a.args and a.args[0] != "-" else sys.stdin.read())
    elif a.action == "add":
        # add MEDIA [IN OUT] | add MEDIA@MOMENT_N   (append; --at INDEX to insert)
        if not a.args:
            sys.exit("timeline add MEDIA [IN OUT] | MEDIA@N")
        spec = a.args[0]
        mid, _, mn = spec.partition("@")
        mid = video_arg(st, mid, spec)
        item = st["media"][mid]
        if mn:
            ms = load_json(mdir(mid) / "moments.json", [])
            m = next((x for x in ms if str(x["n"]) == mn), None)
            if not m:
                sys.exit(f"{item['name']} has no moment {mn} (it has {len(ms)}; `studio media show {mid}`)")
            clip = {"media": mid, "in": m["start"], "out": m["end"], "label": m["title"],
                    "moment": int(m["n"])}
            snap = not a.no_snap           # a moment is a spoken beat: on speech edges by default
        else:
            try:
                a_, b_ = (float(a.args[1]), float(a.args[2])) if len(a.args) >= 3 else (0, item["duration"])
            except ValueError:
                sys.exit("timeline add MEDIA IN OUT: IN and OUT are seconds")
            if b_ - a_ < 0.1:
                sys.exit(f"timeline add: OUT ({b_:g}) must be after IN ({a_:g})")
            clip = {"media": mid, "in": a_, "out": b_}
            snap = a.snap
        clip["id"] = clip_id()
        if snap:
            na, nb, why = snap_media(mid, clip["in"], clip["out"])
            if why:
                print(snap_note(clip, na, nb, why), file=sys.stderr)
                clip["in"], clip["out"] = na, nb
        clips = list(tl["clips"])
        if a.at is not None and (a.at < 1 or a.at != int(a.at)):
            sys.exit("--at is a 1-based position (--at 1 puts it first)")
        clips.insert(int(a.at) - 1 if a.at is not None else len(clips), clip)
        tl = {**tl, "clips": clips}
    elif a.action == "snap" or (a.action == "set" and a.snap):
        # snap P CLIP|all: put spoken clips on speech edges (the fix for `studio check`'s cut
        # failures). The operator's own trims are only ever snapped when the agent asks.
        if a.action == "set":
            tl = json.loads(Path(a.args[0]).read_text() if a.args and a.args[0] != "-" else sys.stdin.read())
            which = range(len(tl.get("clips", [])))
        elif not a.args:
            sys.exit("timeline snap PROJECT CLIP|all")
        else:
            which = range(len(tl["clips"])) if a.args[0] == "all" else [clip_index(tl["clips"], a.args[0])]
        clips = [dict(c) for c in tl.get("clips", [])]
        joined = [pause_cut(c, clips[i + 1] if i + 1 < len(clips) else None,
                            ensure_speech(c["media"]) if st["media"].get(c.get("media"), {}).get("has_audio") else [])
                  for i, c in enumerate(clips)]
        for i in which:
            c = clips[i]
            if c.get("media") not in st["media"]:
                continue
            na, nb, why = snap_media(c["media"], float(c["in"]), float(c["out"]), clip_pace(c))
            if why and (joined[i] or (i and joined[i - 1])):   # a pause cut stays as tightened
                na, nb = (c["in"] if i and joined[i - 1] else na), (c["out"] if joined[i] else nb)
                why = None if (na, nb) == (c["in"], c["out"]) else why
            if why:
                print(snap_note(c, na, nb, why), file=sys.stderr)
                c["in"], c["out"] = na, nb
        tl = {**tl, "clips": clips}
    elif a.action == "voice":
        # voice P CLIP|all natural|off: all sets the story's default (and clears per-clip choices)
        if len(a.args) != 2 or a.args[1] not in VOICES:
            sys.exit(f"timeline voice PROJECT CLIP|all {'|'.join(reversed(VOICES))}")
        clips = [dict(c) for c in tl["clips"]]
        if a.args[0] == "all":
            for c in clips:
                c.pop("voice", None)
            tl = {**tl, "clips": clips, "voice": a.args[1]}
            if a.args[1] == "off":
                tl.pop("voice")
        else:
            clips[clip_index(clips, a.args[0])]["voice"] = a.args[1]
            tl = {**tl, "clips": clips}
    elif a.action == "pace":
        # pace P CLIP|all X: play faster (1.0-1.2), pitch kept; 1 = as said
        try:
            x = float(a.args[1])
        except (IndexError, ValueError):
            sys.exit("timeline pace PROJECT CLIP|all X   (1.0-1.2; 1 = as said)")
        if not PACE_RANGE[0] <= x <= PACE_RANGE[1]:
            sys.exit(f"pace: {x:g} is outside {PACE_RANGE[0]:g}-{PACE_RANGE[1]:g} "
                     "(faster than that stops sounding natural)")
        clips = [dict(c) for c in tl["clips"]]
        for i in (range(len(clips)) if a.args[0] == "all" else [clip_index(clips, a.args[0])]):
            clips[i]["pace"] = x
            if x == 1.0:
                clips[i].pop("pace")
        tl = {**tl, "clips": clips}
    elif a.action == "tighten":
        # tighten P CLIP|all [--max S] [--to S]: every pause inside a clip's speech longer than
        # --max is cut down to --to, as splits of the clip (pause cuts, in silence)
        if not a.args:
            sys.exit("timeline tighten PROJECT CLIP|all [--max 0.35] [--to 0.12]")
        mx, to = a.max if a.max is not None else CHECK_PAUSE, a.to if a.to is not None else TIGHT_TO
        if not 0.04 <= to < mx:
            sys.exit(f"tighten: --to ({to:g}) must be at least 0.04s and less than --max ({mx:g})")
        clips = [dict(c) for c in tl["clips"]]
        which = set(range(len(clips))) if a.args[0] == "all" else {clip_index(clips, a.args[0])}
        out = []
        for i, c in enumerate(clips):
            it = st["media"].get(c["media"], {})
            if i not in which or not it.get("has_audio"):
                out.append(c)
                continue
            notes = []
            pieces = tighten_clip(c, ensure_speech(c["media"]), mx, to, st["media"], notes)
            for m in notes:
                print(f"warning: tightening {c['id']}: {m}", file=sys.stderr)
            if len(pieces) > 1:
                cut = clip_len(c) - sum(clip_len(p) for p in pieces)
                print(f"tightened {c['id']}: {len(pieces) - 1} pause{'s' if len(pieces) > 2 else ''}"
                      f" down to {to:g}s ({cut:.2f}s out)", file=sys.stderr)
            out += pieces
        tl = {**tl, "clips": out}
    elif a.action in ("trim", "move", "split", "rm", "label", "fit", "cover", "uncover"):
        clips = [dict(c) for c in tl["clips"]]
        if not a.args:
            sys.exit(f"timeline {a.action}: which clip? (index or id; see `timeline show`)")
        i = clip_index(clips, a.args[0])
        c = clips[i]
        try:
            if a.action == "trim":      # trim P CLIP IN OUT  (seconds in the source media)
                lo, hi = float(a.args[1]), float(a.args[2])
                if hi - lo < 0.1:
                    sys.exit(f"trim: OUT ({hi:g}) must be greater than IN ({lo:g}); "
                             f"to remove the clip use `timeline rm {a.project} {c['id']}`")
                c["in"], c["out"] = lo, hi
                if a.snap:
                    na, nb, why = snap_media(c["media"], lo, hi, clip_pace(c))
                    if why:
                        print(snap_note(c, na, nb, why), file=sys.stderr)
                        c["in"], c["out"] = na, nb
            elif a.action == "move":    # move P CLIP TO  (1-based position)
                to = int(a.args[1])
                if to < 1:
                    sys.exit("move: TO is a 1-based position (1 = first)")
                clips.insert(max(0, min(len(clips) - 1, to - 1)), clips.pop(i))
            elif a.action == "fit":     # fit P CLIP fill|fit|auto
                mode = a.args[1]
                if mode not in (*FITS, "auto"):
                    sys.exit("fit: fill (crop to fill the frame), fit (show everything) or auto")
                if mode == "auto":
                    c.pop("fit", None)
                else:
                    c["fit"] = mode
            elif a.action == "split":   # split P CLIP AT  (seconds into the clip)
                off = float(a.args[1])
                if not 0.1 <= off <= (c["out"] - c["in"]) - 0.1:
                    sys.exit(f"split: AT must be inside the clip (0.1–{c['out'] - c['in'] - 0.1:.1f}s)")
                at = c["in"] + off
                right = {**c, "id": clip_id(), "in": at}
                if c.get("cover"):     # each side keeps the part laid over its own words
                    c["cover"], right["cover"], notes = cover_cut(c["cover"], at, at, clip_pace(c), c["in"], st["media"])
                    for m in notes:
                        print(f"warning: clip {i + 2} {right['id']}: {m}", file=sys.stderr)
                    for x in (c, right):
                        if not x["cover"]:
                            x.pop("cover")
                clips.insert(i + 1, right)
                c["out"] = at
            elif a.action == "label":   # label P CLIP TEXT...
                text = " ".join(a.args[1:]).strip()
                if text:
                    c["label"] = text
                else:
                    c.pop("label", None)
            elif a.action == "rm":
                clips.pop(i)
            elif a.action == "cover":   # cover P CLIP --with MEDIA IN [OUT] [--at T] [--until U|--until-end|--fill]
                c["cover"] = cover_piece(st, a, c, i, clips)
            elif a.action == "uncover": # uncover P CLIP [N|all]  (N as `timeline show` lists them)
                shown = c.get("cover") or []
                if len(a.args) < 2 or a.args[1] == "all":
                    c.pop("cover", None)
                else:
                    n = int(a.args[1])
                    if not 1 <= n <= len(shown):
                        sys.exit(f"uncover: clip {a.args[0]} has {len(shown)} cover piece"
                                 f"{'s' if len(shown) != 1 else ''}; see `timeline show`")
                    c["cover"] = [p for k, p in enumerate(shown) if k != n - 1]
                    if not c["cover"]:
                        c.pop("cover")
        except (IndexError, ValueError):
            sys.exit(f"timeline {a.action}: bad arguments {a.args} (see `studio timeline -h`)")
        tl = {**tl, "clips": clips}
    if a.aspect:
        tl["aspect"] = a.aspect
    if a.captions:
        tl["captions"] = a.captions == "on"
    # `show --aspect/--captions` changes just those (agents reach for it; silently ignoring it lied)
    problems = []
    new = normalize_timeline(tl, st["media"], problems)
    dropped = [m for k, m in problems if k == "dropped"]
    if dropped:
        for m in dropped:
            print(m, file=sys.stderr)
        sys.exit(2)
    base = a.base if a.base is not None else None
    try:
        timeline_save(a.project, tl, actor=actor_now(), base=base, problems=[])
    except StaleRev as e:
        print(f"story changed since rev {base} (now rev {e.rev}, last edit by {e.actor or '?'}); "
              f"re-run `studio timeline show {a.project}` and try again", file=sys.stderr)
        sys.exit(3)
    for k, m in problems:
        print(m, file=sys.stderr)
    for m in ([] if a.action in ("cover", "uncover", "set", "clear") else
              cover_warnings(normalize_timeline(pj["timeline"], st["media"]), new, st["media"],
                             split=a.action in ("split", "tighten"))):
        print(m, file=sys.stderr)
    a.action, a.aspect, a.captions = "show", None, None
    cmd_timeline(a)

def note_line(n):
    """One note, plus what the operator had selected when they wrote it."""
    who = "agent" if is_agent(n.get("actor")) else n.get("actor") or "?"
    seen = "  ✓seen" if n.get("acked") else ""
    out = [f"{n['ts']}  {n['id']}  [{n.get('project') or '-'}]  {who}: {n['text']}{seen}"]
    if n.get("re"):
        out[0] += f"   (re {n['re']})"
    f = n.get("focus") or {}
    bits = []
    sel = f.get("selection")
    if sel:
        bits.append(f"clip {clip_number(sel)} {sel.get('id')} {fmt_t(sel.get('in') or 0)}-"
                    f"{fmt_t(sel.get('out') or 0)} '{sel.get('label') or sel.get('name') or ''}'")
    elif f.get("media"):
        bits.append(f"footage {f['media'].get('name')} {f['media'].get('id')}")
    if f.get("playhead_hms"):
        bits.append(f"playhead {f['playhead_hms']}")
    ts = f.get("transcript_selection")
    if ts and ts.get("text"):
        bits.append(f"words {fmt_t(ts.get('start') or 0)}-{fmt_t(ts.get('end') or 0)} "
                    f"\"{ts['text'][:80]}\"")
    if bits:
        out.append("    → " + " · ".join(bits))
    return "\n".join(out)

def clip_number(sel):
    """1-based clip number from a focus selection (UI sends index 0-based, and number)."""
    if isinstance(sel.get("number"), int):
        return sel["number"]
    return sel["index"] + 1 if isinstance(sel.get("index"), int) else "?"

def focus_brief(f):
    """One line: what the operator has selected, numbered the way the UI shows it."""
    if not f:
        return "(ui not open)"
    bits = [f"project {f.get('project')}"]
    if f.get("rev") is not None:
        bits[0] += f" rev {f['rev']}"
    sel = f.get("selection")
    if sel:
        bits.append(f"clip {clip_number(sel)} {sel.get('id')} {fmt_t(sel.get('in') or 0)}-"
                    f"{fmt_t(sel.get('out') or 0)} '{sel.get('label') or sel.get('name') or ''}'")
    elif f.get("media"):
        bits.append(f"footage {f['media'].get('name')} {f['media'].get('id')}")
    up = f.get("under_playhead")
    bits.append(f"playhead {f.get('playhead_hms', '?')}"
                + (f" (clip {clip_number(up)}, source {fmt_t(up.get('source_time') or 0)})" if up else ""))
    ts = f.get("transcript_selection")
    if ts and ts.get("text"):
        bits.append(f"words \"{ts['text'][:80]}\"")
    return " · ".join(bits)

def cmd_notes(a):
    st = state()
    notes = [n for n in st["notes"] if not a.project or n.get("project") == a.project]
    mark = HOME / ".notes-read"
    if a.ack:
        ids = [n["id"] for n in notes if any(n["id"] == x or n["ts"] == x for x in a.ack)]
        if not ids:
            sys.exit(f"no note {' '.join(a.ack)}")
        emit("note.ack", None, ids=ids, actor=actor_now())
        print(f"acked {len(ids)}")
        return
    if a.since:
        notes = [n for n in notes if n["ts"] > a.since]
    if a.unread:
        last = mark.read_text().strip() if mark.exists() else ""
        notes = [n for n in notes if n["ts"] > last]
    if a.json:
        print(json.dumps(notes, indent=1))
    else:
        for n in notes:
            print(note_line(n))
        if not notes:
            print("(no notes)" if not a.unread else "(no unread notes)")
    if notes:
        last = mark.read_text().strip() if mark.exists() else ""
        mark.write_text(max([last] + [n["ts"] for n in notes]))
        # reading a note is seeing it: the operator gets an "agent saw this" tick
        fresh = [n["id"] for n in notes if n.get("actor") == "operator" and not n.get("acked")]
        if fresh and by_agent():
            emit("note.ack", None, ids=fresh, actor="agent")

def media_resolve(st, x):
    hits = [m for m in st["media"] if m.startswith(x)] or \
        [m for m, it in st["media"].items() if it["name"] == x]
    if len(hits) != 1:
        sys.exit(f"no media {x} (studio ls)" if not hits else f"media {x} is ambiguous")
    return hits[0]

def drafts_with_edits(mid, item=None):
    """drafts.json with the operator's rewrites (draft.edited events) applied; each post
    gets `edited: [fields]`."""
    item = item or library().get(mid, {})
    dr = copy.deepcopy(load_json(mdir(mid) / "drafts.json", {}))
    eds = item.get("draft_edits") or {}
    ms = {m["n"]: m for m in load_json(mdir(mid) / "moments.json", [])}
    for p in dr.get("posts", []):
        m = ms.get(p.get("n"))
        k = moment_key(m) if m else f"n{p.get('n')}"
        for f, txt in (eds.get(k) or {}).items():
            p[f] = txt
            p.setdefault("edited", []).append(f)
    if "nl" in eds and "text" in eds["nl"]:
        dr["newsletter"] = eds["nl"]["text"]
        dr["newsletter_edited"] = True
    return dr

def moment_key(m):
    return f"{float(m['start']):.2f}-{float(m['end']):.2f}"

def cmd_media(a):
    st = state()
    mid = media_resolve(st, a.id)
    it = st["media"][mid]
    if a.action == "move":
        if not a.project:
            sys.exit("studio media move ID PROJECT")
        ensure_project(a.project)
        emit("media.moved", mid, project=a.project)
        print(f"{mid}  {it['name']}  {it['project']} → {a.project}")
        return
    done = " ".join(s for s in STEPS if s in it["steps"]) or "-"
    dims = f"{it['width']}x{it['height']}  " if it.get("has_video") else "audio only  "
    print(f"{mid}  {it['name']}  {fmt_t(it['duration'])}  {dims}project {it['project']}  steps: {done}")
    if it.get("error"):
        print(f"  error in {it['error']['step']}: {it['error']['error'].splitlines()[0][:160]}")
    ms = load_json(mdir(mid) / "moments.json", [])
    if ms:
        print("moments  (add one with: studio timeline add PROJECT " + mid + "@N)")
        for m in ms:
            print(f"  {m['n']:>2}  {fmt_t(m['start'])}–{fmt_t(m['end'])}  ({m['end'] - m['start']:.1f}s)  "
                  f"{m.get('title', '')}")
            if m.get("hook"):
                print(f"      hook: {m['hook']}")
    else:
        print("moments: none yet" + ("" if "moments" in it["steps"] else
                                     f"  (find them: studio moments {mid})"))
    dr = drafts_with_edits(mid, it)
    if dr.get("posts"):
        print(f"posts: {len(dr['posts'])} drafted  (studio posts {mid})")

def cmd_posts(a):
    st = state()
    mid = media_resolve(st, a.id)
    dr = drafts_with_edits(mid, st["media"][mid])
    if a.json:
        print(json.dumps(dr, indent=1))
        return
    if not dr.get("posts"):
        sys.exit(f"no post drafts for {st['media'][mid]['name']} yet (studio draft {mid})")
    for p in dr["posts"]:
        ed = p.get("edited") or []
        print(f"#{p.get('n')}  {p.get('title', '')}" + (f"   [operator rewrote: {', '.join(ed)}]" if ed else ""))
        for f in ("short", "x", "linkedin"):
            if p.get(f):
                print(f"  {f}{'*' if f in ed else ''}:")
                for line in str(p[f]).splitlines():
                    print(f"    {line}")
        print()
    if dr.get("newsletter"):
        print("newsletter" + ("*" if dr.get("newsletter_edited") else "") + ":")
        print("    " + dr["newsletter"].replace("\n", "\n    "))
    for i in dr.get("ideas") or []:
        print(f"  idea: {i}")

def cmd_words(a):
    st = state()
    if a.fix:
        wrong, right = a.fix
        if a.all:
            mids = [m for m in st["media"] if (mdir(m) / "transcript.json").exists()]
        elif a.id:
            mids = [media_resolve(st, a.id)]
        else:
            sys.exit("words --fix WRONG RIGHT: give a media ID, or --all for every file")
        total = 0
        for mid in mids:
            n = fix_transcript(mid, wrong, right, actor=actor_now())
            if n:
                print(f"{mid}  {st['media'][mid]['name']}: {n} × {wrong!r} → {right!r}")
            total += n
        if not total:
            sys.exit(f"no word {wrong!r} in {'any transcript' if a.all else st['media'][mids[0]]['name']}")
        print(f"fixed {total} word{'s' if total != 1 else ''} in "
              f"{sum(1 for _ in mids)} file{'s' if len(mids) != 1 else ''} searched")
        return
    if not a.id:
        sys.exit("words ID [--from T --to T] [--grep WORD] | words ID --fix WRONG RIGHT | words --fix WRONG RIGHT --all")
    mid = media_resolve(st, a.id)
    t = load_json(mdir(mid) / "transcript.json", None)
    if t is None:
        sys.exit(f"no transcript for {st['media'][mid]['name']} yet (studio transcribe {mid})")
    ws = [w for w in t["words"] if (a.start is None or w["end"] > a.start)
          and (a.to is None or w["start"] < a.to)]
    if a.grep:
        g = a.grep.lower().strip(".,!?\"' ")
        hits = [i for i, w in enumerate(ws) if g in w["w"].lower()]
        for i in hits:
            ctx = " ".join(x["w"] for x in ws[max(0, i - 4):i + 6])
            print(f"{ws[i]['start']:8.2f} {ws[i]['end']:8.2f}  {ws[i]['w']:<16} …{ctx}…")
        if not hits:
            sys.exit(f"no word matching {a.grep!r}")
        return
    if a.json:
        print(json.dumps(ws))
        return
    for w in ws:
        print(f"{w['start']:8.2f} {w['end']:8.2f}  {w['w']}")

def cmd_check(a):
    try:
        r = check(a.project, a.export, actor=actor_now())
    except RuntimeError as e:
        sys.exit(f"studio check: {e}")
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print(verdict_text(r), end="")
        print(f"\nreview sheet: {HOME / r['sheet'] if not Path(r['sheet']).is_absolute() else r['sheet']}")
        print("  cut-*.png: frames, waveform (green = speech) and the words heard, 1.5s either side of "
              "each cut (red line); contact.png: the whole export. Look at them.")
        print("\njudgment (answer each honestly before telling the operator it's done; a no sends it back):")
        for j in r["judgment"]:
            print(f"  {j}")
        if r["excepted"]:
            print(f"\n{len(r['excepted'])} failure{'s' if len(r['excepted']) != 1 else ''} passed by exception "
                  "(listed above with why; tell the operator, who may revoke: studio exception ls "
                  f"{r['project']})")
        print("\n" + ("PASS: every hard rule holds" + (" (with the exceptions above)" if r["excepted"] else "")
                      + ". These bytes are ready (studio ready)." if r["ok"] else
                      f"FAIL: {', '.join(r['failed'])}. Fix, render and check again (or, when the "
                      "picture or the line justifies it, studio exception grant)."))
    if not r["ok"]:
        sys.exit(1)

def cmd_exception(a):
    st = state()
    pj = st["projects"].get(a.project)
    if not pj:
        sys.exit(f"no project {a.project} (studio project ls)")
    try:
        if a.action == "grant":       # grant P RULE [CLIP] --why TEXT
            if not a.args:
                sys.exit("exception grant PROJECT RULE [CLIP] --why TEXT   (rules: "
                         + ", ".join(EXCEPTABLE) + ")")
            clip = pj["timeline"]["clips"][clip_index(pj["timeline"]["clips"], a.args[1])]["id"] \
                if len(a.args) > 1 else None
            x = grant_exception(a.project, a.args[0], clip, a.why or "", actor=actor_now(),
                                onscreen=a.onscreen, card=a.card)
            print(f"granted {x['id']}: {x['rule']} on " + (f"clip {clip} as it is now" if clip else
                  "the whole reel as it is now") + (f" (declares {exception_scope(x)})" if x.get("kind") else "")
                  + f". Why: {x['why']}\n(the next check passes it and lists it; if the "
                  f"{'clip' if clip else 'story'} changes it lapses; the operator may revoke it: "
                  f"studio exception revoke {a.project} {x['id']})")
            return
        if a.action == "revoke":      # revoke P ID [--why TEXT]
            if not a.args:
                sys.exit("exception revoke PROJECT ID [--why TEXT]")
            x = revoke_exception(a.project, a.args[0], a.why or "", actor=actor_now())
            print(f"revoked {x['id']} ({x['rule']}, granted by {x.get('actor') or '?'})")
            return
    except RuntimeError as e:
        sys.exit(f"studio exception: {e}")
    tl = normalize_timeline(pj["timeline"], st["media"])
    exs = [{**x, "state": exception_state(x, tl)} for x in (pj.get("exceptions") or {}).values()]   # ls
    if a.json:
        print(json.dumps(exs, indent=1))
        return
    if not exs:
        print(f"no exceptions in {a.project}. Rules that may bend: "
              + "; ".join(f"{k} ({v})" for k, v in EXCEPTABLE.items()))
    for x in exs:
        if x["state"] in ("revoked", "invalid") and not a.all:
            continue
        state_ = {"granted": "granted",
                  "lapsed": f"LAPSED: the {'clip' if x.get('clip') else 'story'} changed since; grant again if it still holds",
                  "invalid": "INVALID: this rule can't bend; ignored",
                  "revoked": (f"REVOKED {x['revoked']['ts']} by {x['revoked'].get('actor') or '?'}"
                              + (f": {x['revoked']['why']}" if x["revoked"].get("why") else "")) if x.get("revoked") else ""}[x["state"]]
        decl = f"  declares {exception_scope(x)}" + (f" {x['text']!r}" if x.get("text") else "") if x.get("kind") else ""
        print(f"{x['id']}  {x['rule']:<10}  {('clip ' + x['clip']) if x.get('clip') else 'whole reel':<14}  "
              f"{x.get('actor') or '?'} {x['ts']}  {state_}{decl}\n          why: {x.get('why')}")

def cmd_ready(a):
    """Exit 0 only when the export (default: the latest) is checked, bytes and all."""
    st = state()
    pj = st["projects"].get(a.project)
    if not pj:
        sys.exit(f"no project {a.project} (studio project ls)")
    try:
        final, ex = export_for(st, a.project, a.export)
    except RuntimeError as e:
        sys.exit(f"studio ready: {e}")
    if not ex:
        sys.exit(f"not ready: {final} is not one of {a.project}'s renders")
    v = export_verdict(pj, ex, bytes_sha(final) or "missing")
    latest = ex is pj["exports"][-1]
    if a.json:
        print(json.dumps({"file": ex["file"], "sha256": ex["sha256"], "latest": latest, **v}, indent=1))
    else:
        if v["state"] == "ready":
            print(f"ready: {ex['file']}  (checked {v['checked']} by {v.get('by') or '?'})")
        elif v["state"] == "failed":
            print(f"not ready: {ex['file']} fails {', '.join(v['failed'])}")
            for f in v["open"]:
                print(f"  - {f['rule']}: {f['item']}")
        else:
            print(f"not ready: {ex['file']}: {v['why']}")
        for x in v.get("excepted", []):
            print(f"  ~ EXCEPTION {x['exception']} by {x.get('by') or '?'}: {x['rule']}: {x['item']}. Why: {x['why']}")
        if not latest:
            print(f"  (not the latest export of {a.project})")
    if v["state"] != "ready":
        sys.exit(1)

def cmd_choose(a):
    try:
        chosen = None if a.chosen.lower() == "none" else a.chosen
        if a.round:
            d = choose_blind(a.project, a.round, chosen, note=a.note, actor=actor_now())
        else:
            if not a.shown:
                sys.exit("choose PROJECT CHOSEN|none --from OPT,OPT,... (in the order shown)  "
                         "| choose PROJECT LABEL --round ROUND")
            shown = [x.strip() for x in a.shown.split(",") if x.strip()]
            d = record_choice(a.project, shown, chosen, kind=a.kind, action=a.action,
                              blind=a.blind, note=a.note, text=a.text, actor=actor_now())
    except RuntimeError as e:
        sys.exit(f"studio choose: {e}")
    print(json.dumps(d, indent=1) if a.json else
          f"choice {d['id']}: {d['kind']} {d['action']} {d['chosen'] or 'none'} of {len(d['shown'])}"
          + (f" (blind, round {d['round']})" if d.get("blind") else ""))

def cmd_ab(a):
    try:
        rid, d, labels = blind_round(a.project, a.exports or None, actor=actor_now())
    except RuntimeError as e:
        sys.exit(f"studio ab: {e}")
    print(f"blind round {rid}: {len(labels)} versions, labels hidden, order shuffled")
    for lab in sorted(labels):
        print(f"  {lab}  {next(d.glob(lab + '.*'))}")
    print("  (same dates on every copy; the sizes are the real files', so don't look them up in "
          "the exports list)")
    print(f"watch them, then: studio choose {a.project} A|B|none --round {rid} [--note TEXT]")

def cmd_bundle(a):
    print(f"bundling into {a.out}")
    try:
        r = bundle(a.out, a.project, a.all, zip_it=a.zip)
    except BundleError as e:
        sys.exit(f"studio bundle: {e}")
    st = state()
    print(f"\nshared {len(r['projects'])} project{'s' if len(r['projects']) != 1 else ''} "
          f"({', '.join(st['projects'][s]['name'] for s in r['projects'])}): "
          f"{r['media']} files, {r['moments']} moments")
    print(f"  {r['dir']}")
    print(f"  total {fmt_size(r['bytes'])}  (footage {fmt_size(r['footage_bytes'])}, "
          f"library {fmt_size(r['library_bytes'])})")
    if r["cloned"] and not r["copied"]:
        print("  footage and exports are APFS clones: the folder takes no extra space on this disk")
    elif r["copied"]:
        print(f"  {r['copied']} file{'s' if r['copied'] != 1 else ''} copied (not on the library's disk, "
              f"so no clones), {r['cloned']} cloned")
    if r.get("zip"):
        print(f"  zip  {r['zip']}  {fmt_size(r['zip_bytes'])}")
        if r["zip_bytes"] > BIG_ZIP:
            print(f"  warning: the zip is over {fmt_size(BIG_ZIP)}; Google Drive takes it, but the upload "
                  "and download will be slow. Sharing one project per bundle keeps them smaller.")
    print("  the teammate runs: studio import <the unzipped folder>  (or File ▸ Import… in the app)")

def cmd_import(a):
    try:
        r = import_bundle(a.bundle, prepare=not a.no_previews)
    except BundleError as e:
        sys.exit(f"studio import: {e}")
    st = state()
    print(f"\nimported {Path(r['bundle']).name} into {HOME}")
    print(f"  {len(r['media'])} new file{'s' if len(r['media']) != 1 else ''}"
          + (f", {len(r['already'])} already here (left as they were)" if r["already"] else ""))
    for old, slug in r["projects"].items():
        pj = st["projects"].get(slug)
        if not pj:
            continue
        n = sum(1 for it in st["media"].values() if it["project"] == slug)
        print(f"  {slug:<28} {n:>3} media  {len(pj['timeline']['clips']):>3} clips  {pj['name']}")
    for x in r["renamed"]:
        print(f"  note: you already have a different project called {x['from']!r}, so this one "
              f"came in as {x['to']!r}")
    if not r["media"] and not r["events"]:
        print("  nothing new: everything in this bundle is already here")

def main():
    ap = argparse.ArgumentParser(prog="studio", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ui"); p.add_argument("--port", type=int, default=7317)
    p.add_argument("--no-open", action="store_true")
    for name in ["run", "ingest"]:
        p = sub.add_parser(name)
        p.add_argument("files", nargs="*")
        p.add_argument("--brand", default="jefe")
        p.add_argument("-p", "--project", default=DEFAULT_PROJECT)
        if name == "run":
            p.add_argument("--no-open", action="store_true")
    for s in STEPS:
        p = sub.add_parser(s)
        p.add_argument("ids", nargs="*")
        p.add_argument("--force", action="store_true")
        p.add_argument("--all", action="store_true", help="every file still missing this step")
    p = sub.add_parser("project"); p.add_argument("action", choices=["new", "ls"])
    p.add_argument("name", nargs="?"); p.add_argument("--brand", default="jefe")
    p = sub.add_parser("timeline")
    p.add_argument("action", choices=["show", "add", "clear", "set", "trim", "move", "split", "rm",
                                      "label", "fit", "snap", "cover", "uncover", "voice", "pace",
                                      "tighten"])
    p.add_argument("project"); p.add_argument("args", nargs="*")
    p.add_argument("--at", type=float, metavar="N|T",
                   help="add: insert as clip N (1-based); cover: seconds into the clip as it "
                        "plays, after its pace (default 0)")
    p.add_argument("--with", dest="cover_with", nargs="+", metavar="MEDIA IN [OUT]",
                   help="cover: the picture to lay over the clip, from IN (seconds in MEDIA; "
                        "OUT - IN = the seconds it covers)")
    p.add_argument("--until", type=float, metavar="U",
                   help="cover: to U seconds into the clip as it plays (instead of OUT)")
    p.add_argument("--until-end", action="store_true",
                   help="cover: to the clip's end, wherever a later trim puts it")
    p.add_argument("--fill", action="store_true", help="cover: the whole clip (--at 0 --until-end)")
    p.add_argument("--fit", choices=list(FITS), help="cover: crop to fill, or show the whole frame")
    p.add_argument("--max", type=float, metavar="S", help="tighten: pauses longer than this (default 0.35)")
    p.add_argument("--to", type=float, metavar="S", help="tighten: are cut down to this (default 0.12)")
    p.add_argument("--aspect", choices=list(ASPECTS))
    p.add_argument("--captions", choices=["on", "off"])
    p.add_argument("--base", type=int, metavar="REV",
                   help="refuse (exit 3) if the story changed since this rev (from `timeline show`)")
    p.add_argument("--json", action="store_true", help="show: the timeline as JSON, with rev")
    p.add_argument("--snap", action="store_true",
                   help="add/trim/set: put the in and out points on speech edges (QUALITY.md)")
    p.add_argument("--no-snap", action="store_true",
                   help="add MEDIA@N: keep the moment's times exactly (snapped by default)")
    p = sub.add_parser("render"); p.add_argument("project")
    p = sub.add_parser("check", help="measure an export against QUALITY.md; review sheet; exit 1 on a fail")
    p.add_argument("project"); p.add_argument("export", nargs="?", help="a render path (default: the latest)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("ready", help="is the export checked, bytes and all? exit 0 only if ready")
    p.add_argument("project"); p.add_argument("export", nargs="?", help="a render path (default: the latest)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("exception", help="grant (with a reason), revoke or list exceptions to a hard rule")
    p.add_argument("action", choices=["grant", "revoke", "ls"]); p.add_argument("project")
    p.add_argument("args", nargs="*", metavar="RULE [CLIP] | ID")
    p.add_argument("--why", help="grant: what the picture or the line does that the rule misses; revoke: why not")
    p.add_argument("--all", action="store_true", help="ls: revoked and invalid ones too")
    p.add_argument("--onscreen", metavar="TEXT", help='grant "first word" CLIP: the clip shows this text (the hook)')
    p.add_argument("--card", action="store_true", help="grant picture CLIP: the clip is a designed card")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("choose", help="log a pick (preference data): what was shown, what was chosen")
    p.add_argument("project"); p.add_argument("chosen", metavar="CHOSEN|none|LABEL")
    p.add_argument("--from", dest="shown", metavar="OPT,OPT,...",
                   help="the options in the order shown: export sha256 prefixes or file names, "
                        "MEDIA@N moments, MEDIA@N:field drafts, or any ids")
    p.add_argument("--round", metavar="ROUND", help="a blind round from `studio ab`: CHOSEN is its label")
    p.add_argument("--kind", help="version, moment, draft, hook, caption… (default: from the options)")
    p.add_argument("--action", default="pick", choices=list(ACTIONS))
    p.add_argument("--blind", action="store_true", help="the labels were hidden when it was chosen")
    p.add_argument("--note"); p.add_argument("--text", help="rewrite: the text it became")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("ab", help="blind A/B of versions: shuffled, labels hidden, the mapping logged")
    p.add_argument("project"); p.add_argument("exports", nargs="*", metavar="EXPORT",
                                              help="sha256 prefixes or file names (default: the two latest)")
    p = sub.add_parser("notes"); p.add_argument("-p", "--project")
    p.add_argument("--json", action="store_true"); p.add_argument("--since", metavar="TS")
    p.add_argument("--unread", action="store_true", help="only notes newer than the last read")
    p.add_argument("--ack", nargs="+", metavar="ID", help="mark notes seen (the operator gets a tick)")
    p = sub.add_parser("say", help="reply to the operator in the UI's Tell the agent tab")
    p.add_argument("text"); p.add_argument("-p", "--project"); p.add_argument("--re", metavar="NOTE_ID")
    p = sub.add_parser("media"); p.add_argument("action", choices=["show", "move"]); p.add_argument("id")
    p.add_argument("project", nargs="?", help="move: the project it belongs to now")
    p = sub.add_parser("posts"); p.add_argument("id"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("words"); p.add_argument("id", nargs="?")
    p.add_argument("--from", dest="start", type=float); p.add_argument("--to", type=float)
    p.add_argument("--grep"); p.add_argument("--json", action="store_true")
    p.add_argument("--fix", nargs=2, metavar=("WRONG", "RIGHT"),
                   help="fix a misheard word in the transcript (and so in the captions)")
    p.add_argument("--all", action="store_true", help="--fix: in every file's transcript")
    p = sub.add_parser("focus")
    p.add_argument("--brief", action="store_true", help="one human line instead of the JSON")
    sub.add_parser("review")
    p = sub.add_parser("bundle", help="share projects: footage + all the work on it, one folder")
    p.add_argument("out", metavar="OUT_DIR")
    p.add_argument("-p", "--project", action="append", default=[], metavar="PROJECT")
    p.add_argument("--all", action="store_true", help="every project")
    p.add_argument("--zip", action="store_true", help="also write OUT_DIR.zip (stored, no compression)")
    p = sub.add_parser("import", help="merge a shared bundle into this library")
    p.add_argument("bundle", metavar="BUNDLE_DIR")
    p.add_argument("--no-previews", action="store_true",
                   help="don't make previews now (the studio makes them when it next starts)")
    p = sub.add_parser("ls"); p.add_argument("-p", "--project")
    p = sub.add_parser("log"); p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("state", help="the folded state (docs/events.md §5), as JSON with sorted keys")
    p.add_argument("--json", action="store_true", help="JSON (the only format; accepted for symmetry)")
    sub.add_parser("agent", help="which agent moments/draft call, and the command line")
    a = ap.parse_args()

    for sub_dir in ("inbox", "media", "projects"):
        (HOME / sub_dir).mkdir(parents=True, exist_ok=True)

    if a.cmd == "ui":
        import server
        server.serve(a.port, open_window=not a.no_open)
    elif a.cmd in ("run", "ingest"):
        before, problems = set(library()), []
        ids = ingest(a.files, a.brand, a.project, problems=problems)
        new = [m for m in ids if m not in before]
        if a.cmd == "run":
            pending = [m for m, it in library().items() if "draft" not in it["steps"]]
            if pending:
                run_pipeline(pending, STEPS)
            out = review()
            print(f"review → {out}")
            if not a.no_open and sys.platform == "darwin":
                subprocess.run(["open", str(out)])
        else:
            if ids:
                run_pipeline(ids, LOCAL_STEPS)
            print(f"{len(new)} new, {len(ids) - len(new)} already in the library"
                  + (f", {len(problems)} skipped (see above)" if problems else ""))
            if problems and a.files:      # a file you named that didn't come in
                sys.exit(1)
    elif a.cmd in STEPS:
        if not a.ids and not a.all:
            sys.exit(f"studio {a.cmd}: pass media ids, or --all for every file missing it"
                     + (" (this calls your agent for each one)" if a.cmd in AI_STEPS else "")
                     + ". To read results: studio media show ID / studio posts ID / studio words ID")
        ids = a.ids
        if a.all:
            ids = [m for m, it in library().items() if a.force or a.cmd not in it["steps"]]
        run_step(a.cmd, [media_resolve(state(), x) for x in ids] if a.ids else ids, a.force)
    elif a.cmd == "project":
        if a.action == "new":
            if not a.name:
                sys.exit("project new NAME")
            print(project_new(a.name, a.brand))
        else:
            st = state()
            for slug, pr in st["projects"].items():
                n = sum(1 for m in st["media"].values() if m["project"] == slug)
                print(f"{slug:<28} {n:>3} media  {len(pr['timeline']['clips']):>3} clips  {pr['name']}")
    elif a.cmd == "timeline":
        cmd_timeline(a)
    elif a.cmd == "render":
        print(render(a.project, actor=actor_now()))
    elif a.cmd == "check":
        cmd_check(a)
    elif a.cmd == "ready":
        cmd_ready(a)
    elif a.cmd == "exception":
        cmd_exception(a)
    elif a.cmd == "choose":
        cmd_choose(a)
    elif a.cmd == "ab":
        cmd_ab(a)
    elif a.cmd == "notes":
        cmd_notes(a)
    elif a.cmd == "say":
        st = state()
        proj = a.project
        if a.re:
            n = next((n for n in st["notes"] if n["id"] == a.re or n["ts"] == a.re), None)
            if not n:
                sys.exit(f"no note {a.re} (studio notes)")
            proj = proj or n.get("project")
            if n.get("actor") == "operator" and not n.get("acked"):
                emit("note.ack", None, ids=[n["id"]], actor=actor_now())
        ev = emit("note", None, text=a.text.strip(), project=proj, actor="agent",
                  id=clip_id(), **({"re": a.re} if a.re else {}))
        print(f"said ({ev['data']['id']})")
    elif a.cmd == "media":
        cmd_media(a)
    elif a.cmd == "posts":
        cmd_posts(a)
    elif a.cmd == "words":
        cmd_words(a)
    elif a.cmd == "focus":
        # What the operator is looking at in the UI right now — so "this clip" resolves.
        f = HOME / "ui-focus.json"
        if a.brief:
            print(focus_brief(load_json(f, {})))
        else:
            print(f.read_text() if f.exists() else "{}  (ui not open)")
    elif a.cmd == "review":
        out = review(); print(out)
        if sys.platform == "darwin":
            subprocess.run(["open", str(out)])
    elif a.cmd == "bundle":
        cmd_bundle(a)
    elif a.cmd == "import":
        cmd_import(a)
    elif a.cmd == "ls":
        for mid, it in library().items():
            if a.project and it["project"] != a.project:
                continue
            done = " ".join(s for s in STEPS if s in it["steps"])
            print(f"{mid}  {it['project']:<16} {fmt_t(it['duration']):>7}  {it['name'][:36]:<36}  {done}")
    elif a.cmd == "log":
        for ev in tail_events(a.n, skip=("job.progress",)):
            print(ev["ts"], ev["type"], ev["media"] or "-", json.dumps(ev["data"])[:120])
    elif a.cmd == "state":
        print(json.dumps(state(), sort_keys=True, indent=1, ensure_ascii=False))
    elif a.cmd == "agent":
        info = agent_info()
        if not info["name"]:
            sys.exit(info["error"])
        print(f"agent    {info['name']}  ({info['path'] or info['command'] + ' not on PATH'})")
        print(f"command  {shlex.join(agent_cmd(info['name'], info['model'], True, 'REPLY_FILE'))}"
              "  < prompt")
        print(f"model    {info['model'] or '(the agent default)'};  moments: "
              f"{info['moments_model'] or '(the agent default)'}")
        print(f"you are  {actor_now()}" + ("" if by_agent() else
              "  (an agent driving the CLI: set STUDIO_ACTOR=agent if it isn't detected)"))

if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
