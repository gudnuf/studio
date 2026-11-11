#!/usr/bin/env python3
"""Seed a throwaway STUDIO_HOME for UI work: fixtures ingested (real proxy/whisper/keyframes),
hand-written moments + drafts (no agent call), and a "jefe-shoot" project with a 4-clip story.

    nix develop -c python3 test/seed.py /path/to/home [--fixtures DIR] [--bare]

--bare: only create the empty home (for testing the first-run / drop-footage path).
Then: STUDIO_HOME=/path/to/home ./studio ui --port 7450 --no-open

--v2: no ffmpeg (and, without --with-footage, no footage). Writes a deterministic events.jsonl (fixed clock, fixed ids, device
"seed") through studio.py's own verbs wherever they need no media work (project new, timeline
save, exception grant/revoke, choose, ab), and emits the rest (ingest, steps, render.done,
check.done, jobs, notes) with made-up measurements: two projects with exports, a failing check
with an agent's exception, a revoked exception, a blind round and its pick, a shelved project,
and v2 types the fold ignores. Same bytes every run: test/make-golden.sh folds it into
test/golden/fold/seeded.state.json.
"""
import argparse, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MOMENTS = {
    "phone-talking.mov": [
        ("Two hours every night", "I used to spend two hours every single night typing up estimates",
         "time back", "past-self", "talking"),
        ("Talk while you walk", "I just talk through the job while I walk it", "how it works",
         "everyday-stakes", "talking"),
        ("Never leaves my phone", "the audio never leaves my phone", "privacy", "disagree", "talking")],
    "site-walk.mov": [
        ("Henderson walk: the beds", "These beds along the walkway, the mulch is basically gone",
         "real jobs", "everyday-stakes", "walk"),
        ("Cracked head, zone three", "See this head? Cracked.", "real jobs", "redefine", "walk"),
        ("Quote before the driveway", "send it before I leave the driveway", "time back",
         "past-self", "walk")],
    "screen-recording.mp4": [
        ("Estimate builds itself", "Watch the line items fill in", "how it works", "redefine", "demo")],
}


# ---------- --v2: the golden fold fixture -------------------------------------------------------

FOOTAGE = {"talking.mov": "phone-talking.mov", "broll.mov": "site-walk.mov", "memo.m4a": "voice-memo.m4a"}


class _Clock:
    """Stands in for studio.py's `datetime` module: every now() is 7 s after the last, in a fixed
    -08:00 zone whatever the machine's zone is."""
    def __init__(self, start):
        import datetime as dt
        self.dt, self.t = dt, start
        clock = self

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                clock.t += dt.timedelta(seconds=7)
                return _DT.fromtimestamp(clock.t.timestamp(), clock.t.tzinfo)

            def astimezone(self, tz=None):
                return self if tz is None else dt.datetime.astimezone(self, tz)
        self.datetime, self.timedelta, self.timezone = _DT, dt.timedelta, dt.timezone


def seed_v2(home, footage=None):
    import datetime as dt, hashlib, random
    if (home / "events.jsonl").exists():
        sys.exit(f"{home} already has a log; --v2 writes a fresh one")
    for d in ("inbox", "media", "projects", "uploads"):
        (home / d).mkdir(parents=True, exist_ok=True)
    os.environ["STUDIO_DEVICE"] = "seed"
    S = H.import_studio(home)
    clock = _Clock(dt.datetime(2026, 11, 9, 9, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=-8))))
    S.datetime = clock
    n = {"id": 0, "uuid": 0}

    def clip_id():
        n["id"] += 1
        return hashlib.sha256(f"seed-id-{n['id']}".encode()).hexdigest()[:8]

    def uuid7():
        n["uuid"] += 1
        ms = int(clock.t.timestamp() * 1000)
        b = bytearray(ms.to_bytes(6, "big") + hashlib.sha256(f"seed-uuid-{n['uuid']}".encode()).digest()[:10])
        b[6], b[8] = 0x70 | (b[6] & 0x0F), 0x80 | (b[8] & 0x3F)
        h = b.hex()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    S.clip_id, S.uuid7 = clip_id, uuid7
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()

    def ingest(name, project, dur, w, h, audio=True, video=True):
        # with footage, the real bytes' sha256 (bundle and import check it); else a made-up one
        digest = (S.bytes_sha(footage / FOOTAGE[name]) if footage else sha(f"seed-media-{name}"))
        mid = digest[:12]
        (home / "media" / mid).mkdir(parents=True, exist_ok=True)
        if footage:     # the log says what it says; the bytes are a stand-in so ffmpeg has input
            S.clone_or_copy(footage / FOOTAGE[name], home / "media" / mid / ("source" + Path(name).suffix))
        S.emit("ingested", mid, name=name, sha256=digest, path=str(home / "media" / mid / ("source" + Path(name).suffix)),
               project=project, duration=dur, has_audio=audio, has_video=video, width=w, height=h)
        S.emit("brand.set", mid, brand="jefe")
        return mid

    A = S.project_new("Sparks opener")
    B = S.project_new("Paperwork reel")
    talk = ingest("talking.mov", A, 45.0, 1080, 1920)
    broll = ingest("broll.mov", A, 30.0, 1920, 1080)
    memo = ingest("memo.m4a", "inbox", 20.0, 0, 0, video=False)
    # the files the read verbs open (words, media show, posts, snap): small, deterministic
    line = ("I used to spend two hours every single night typing up estimates. Now I just talk "
            "through the job while I walk it, and the audio never leaves my phone. That is the privacy part.")
    ws, t = [], 2.2
    for i, w in enumerate(line.split()):
        ws.append({"w": w, "start": round(t, 3), "end": round(t + 0.32, 3)})
        t += 0.42 + (0.6 if w.endswith(".") else 0.0)
    segs = [{"start": ws[0]["start"], "end": ws[-1]["end"], "text": line}]
    d = home / "media" / talk
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.json").write_text(json.dumps({"language": "en", "segments": segs, "words": ws}, indent=1))
    (d / "transcript.txt").write_text(f"[0:02.2] {line}\n")
    regions = [[2.1, 9.9], [10.5, 19.6], [20.2, 31.0]]
    (d / "speech.json").write_text(json.dumps({"model": S.VAD_MODEL_NAME, "args": S.VAD_ARGS,
                                               "duration": 45.0, "regions": regions}, indent=1))
    moments = [{"n": 1, "start": 2.0, "end": 9.9, "title": "Two hours every night", "hook": line[:52],
                "pillar": "time back", "why": "seeded", "angle": "past-self", "kind": "talking"},
               {"n": 2, "start": 20.1, "end": 31.0, "title": "Never leaves my phone",
                "hook": "the audio never leaves my phone", "pillar": "privacy", "why": "seeded",
                "angle": "everyday-stakes", "kind": "talking"}]
    (d / "moments.json").write_text(json.dumps(moments, indent=1))
    (d / "drafts.json").write_text(json.dumps({"posts": [
        {"n": m["n"], "short": m["hook"], "x": f"X post {m['n']}", "linkedin": f"LinkedIn {m['n']}",
         "title": m["title"]} for m in moments], "newsletter": "Newsletter.", "ideas": ["a", "b"]}, indent=1))
    for mid in (talk, broll):
        S.emit("step.proxy", mid, file="proxy.mp4", sha256=sha(f"proxy-{mid}"), chunks=4, encoder="h264_videotoolbox")
    # a CLI step's progress (job null), cleared by its step event; one left open for broll
    S.emit("job.progress", talk, job=None, step="speech", pct=40.0)
    S.emit("step.speech", talk, regions=9, speech_s=31.4, sha256=sha(f"speech-{talk}"))
    S.emit("step.speech", broll, regions=0, skipped="no speech")
    S.emit("step.transcribe", talk, words=96, segments=12, sha256=sha(f"tr-{talk}"), fixed=1, dropped=["thank you"])
    S.emit("step.keyframes", talk, frames=45, every=1.0)
    S.emit("job.progress", broll, job=None, step="keyframes", pct=55.5)
    S.emit("step.failed", memo, step="transcribe", error="whisper-cli failed (1): model not found")
    S.emit("step.moments", talk, moments=2, sha256=sha(f"mo-{talk}"),
           provenance={"provider": "claude", "model": "claude-opus-5-5", "prompt": "0123456789ab"})
    S.emit("step.draft", talk, posts=2, sha256=sha(f"dr-{talk}"),
           provenance={"provider": "claude", "model": "claude-opus-5-5", "prompt": "ba9876543210"})
    S.emit("transcript.edited", talk, wrong="hay fay", right="Jefe", count=2, sha256=sha(f"tr2-{talk}"), actor="agent")
    S.emit("draft.edited", talk, key="1", field="short", text="Two hours a night, back.", n=1, actor="operator")
    S.emit("draft.edited", talk, key="1", field="x", text="An X post.", n=1, actor="operator")
    S.emit("draft.edited", talk, key="1", field="x", text=None, n=1, actor="operator")
    S.emit("media.moved", memo, project=B)

    clips = [{"id": "a1000001", "media": talk, "in": 2.0, "out": 9.5, "label": "hook", "moment": 1},
             {"id": "a1000002", "media": talk, "in": 12.0, "out": 20.0, "pace": 1.1,
              "cover": [{"media": broll, "in": 3.0, "start": 1.0, "end": 3.5}]},
             {"id": "a1000003", "media": broll, "in": 0.0, "out": 3.0, "fit": "fill"},
             {"id": "a1000004", "media": memo, "in": 0.0, "out": 4.0}]     # audio-only: normalize drops it
    S.timeline_save(A, {"aspect": "9:16", "captions": True, "clips": clips}, actor="operator")
    clips[1]["out"] = 19.0
    tl = S.timeline_save(A, {"aspect": "9:16", "captions": True, "voice": "natural", "clips": clips[:3]},
                         actor="agent", base=1)
    c2, c3 = tl["clips"][1], tl["clips"][2]

    def render(slug, tl, n):
        f = f"projects/{slug}/exports/{slug}-20261109-09{n:02d}00-9x16.mp4"
        (home / f).parent.mkdir(parents=True, exist_ok=True)
        body = f"seed export {slug} {n}".encode()     # not a video; its sha256 is real, so ready works
        (home / f).write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        S.emit("job.queued", None, job=f"1794150000-{n}", kind="render", project=slug)
        S.emit("job.started", None, job=f"1794150000-{n}", kind="render", project=slug)
        S.emit("job.progress", None, job=f"1794150000-{n}", step="render", pct=50.0, project=slug)
        dur = sum(S.clip_frames(c) for c in tl["clips"]) / S.FPS
        S.emit("render.done", None, project=slug, file=f, duration=round(dur, 2), aspect=tl["aspect"],
               clips=len(tl["clips"]), sha256=digest, actor="agent", secs=12.5, concat="copy",
               loudness={"input_i": -19.2, "input_tp": -3.1, "normalized": True}, timeline=tl,
               cuts=S.cut_list(tl, S.state()["media"]))
        S.emit("job.done", None, job=f"1794150000-{n}", kind="render", result={"file": f})
        return f, digest

    def check(slug, f, digest, failures):
        rules = [{"rule": r, "status": "fail" if any(x["rule"] == r for x in failures) else "pass",
                  "detail": f"seeded {r}"} for r in ("cuts", "first word", "pauses", "length", "loudness", "picture")]
        pj = S.state()["projects"][slug]
        excepted = [x["id"] for f_ in failures for x in [S.covering(f_, pj.get("exceptions"))] if x]
        S.emit("check.done", None, project=slug, brand="jefe", file=f, sha256=digest, ok=not failures,
               failed=sorted({x["rule"] for x in failures}), failures=failures, excepted=excepted,
               rules=rules, sheet=f"projects/{slug}/check/{digest[:12]}", actor="agent")

    reel = S.reel_fp(tl)
    pauses = {"rule": "pauses", "clip": c2["id"], "item": "0.55s pause at 9.40s (clip 2)", "fp": S.clip_fp(c2), "reel": reel}
    black = {"rule": "picture", "clip": c3["id"], "item": "black 14.0-14.2s", "fp": S.clip_fp(c3), "reel": reel}
    f1, s1 = render(A, tl, 1)
    check(A, f1, s1, [pauses, black])                    # export 1: failed
    S.grant_exception(A, "pauses", c2["id"], why="the held beat lands the line; the picture carries it", actor="agent")
    card = S.grant_exception(A, "picture", c3["id"], why="a designed card, the dark ground is meant", actor="agent", card=True)
    S.revoke_exception(A, card["id"], why="that is not a card, it is a dropped frame", actor="operator")
    f2, s2 = render(A, tl, 2)
    check(A, f2, s2, [pauses])                           # export 2: excepted, so ready
    S.emit("job.queued", None, job="1794150000-9", kind="check", project=A)
    S.emit("job.started", None, job="1794150000-9", kind="check", project=A)
    S.emit("job.failed", None, job="1794150000-9", kind="check", error="server restarted")

    rid, _, _ = S.blind_round(A, actor="operator", rng=random.Random(7))
    S.choose_blind(A, rid, "A", note="the opener is tighter", actor="operator")
    S.record_choice(A, [f"{talk}@1", f"{talk}@2"], f"{talk}@2", action="keep", actor="operator")

    note = S.emit("note", None, text="the cover comes in late", project=A, actor="operator", id=S.clip_id(),
                  focus={"project": A, "sha256": s2, "at": 9.4})
    S.emit("note.ack", None, ids=[note["data"]["id"]], actor="agent")
    S.emit("note", None, text="Moved the cover in by half a second.", project=A, actor="agent",
           id=S.clip_id(), re=note["data"]["id"])
    S.emit("note", None, text="and the end card?", project=A, actor="operator", id=S.clip_id())

    S.timeline_save(B, {"aspect": "9:16", "captions": False, "clips": [{"id": "b2000001", "media": broll, "in": 5.0, "out": 12.0}]},
                    actor="agent")
    render(B, S.state()["projects"][B]["timeline"], 3)  # never checked: unchecked
    S.emit("project.shelved", None, project=B, why="not a reel: no line to open on", actor="operator")
    # v2 types nothing folds yet: the fold must ignore them
    S.emit("mark", None, project=A, sha256=s2, at=4.2, dir="up", actor="operator")
    S.emit("agent.status", None, text="cutting the sparks opener", project=A, actor="agent")
    S.emit("ui.started", None, url="http://127.0.0.1:7317/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("home")
    ap.add_argument("--fixtures")
    ap.add_argument("--bare", action="store_true")
    ap.add_argument("--v2", action="store_true", help="deterministic log for the golden fold fixtures")
    ap.add_argument("--with-footage", action="store_true",
                    help="--v2: clone the test fixtures in as the sources, so render and check run")
    a = ap.parse_args()
    home = Path(a.home).resolve()
    if home == H.REAL_HOME.resolve():
        sys.exit("refusing to seed ~/Studio")
    if a.v2:
        if a.fixtures:
            os.environ["STUDIO_FIXTURES"] = a.fixtures
        seed_v2(home, footage=H.fixtures() if a.with_footage else None); print(home); return
    for d in ("inbox", "media", "projects", "uploads"):
        (home / d).mkdir(parents=True, exist_ok=True)
    if a.bare:
        print(home); return
    if a.fixtures:
        os.environ["STUDIO_FIXTURES"] = a.fixtures
    fx = H.fixtures()
    H.cli(home, "project", "new", "Jefe Shoot")
    H.cli(home, "ingest", "-p", "jefe-shoot", *[fx / f for f in H.FIXTURE_FILES])
    S = H.import_studio(home)
    ids = {it["name"]: mid for mid, it in S.state()["media"].items()}
    for name, specs in MOMENTS.items():
        mid = ids[name]
        dur = S.state()["media"][mid]["duration"]
        span = dur / len(specs)
        ms = [{"n": i + 1, "start": round(i * span + 0.4, 2), "end": round(min(dur, (i + 1) * span), 2),
               "title": t, "hook": hook, "pillar": pillar, "why": "fixture: picked by hand for UI work",
               "angle": angle, "kind": kind} for i, (t, hook, pillar, angle, kind) in enumerate(specs)]
        (home / "media" / mid / "moments.json").write_text(json.dumps(ms, indent=1))
        S.emit("step.moments", mid, moments=len(ms), fixture=True)
        H.fake_drafts(home, mid, ms)
    pt, sw, sr = ids["phone-talking.mov"], ids["site-walk.mov"], ids["screen-recording.mp4"]
    for spec in (f"{pt}@1", f"{sw}@1", f"{sr}@1", f"{pt}@3"):
        H.cli(home, "timeline", "add", "jefe-shoot", spec)
    print(home)


if __name__ == "__main__":
    main()
