"""HTTP API contract tests for server.py. Run inside the devshell:

    cd forge/studio && nix develop -c python3 test/test_server.py -v

Starts `server.serve(port, open_window=False)` in a subprocess on a free port in 7400-7499
with a temp STUDIO_HOME, uploads fixtures through POST /api/upload, and exercises the API
the UI depends on. No AI steps are called.
"""
import json, os, subprocess, sys, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

NEEDS = ("ffmpeg", "ffprobe", "whisper-cli")


def wait_job(home, job, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in H.events(home):
            if e["type"] in ("job.done", "job.failed") and e["data"].get("job") == job:
                return e
        time.sleep(0.1)
    raise AssertionError(f"job {job} did not finish in {timeout}s")


@unittest.skipUnless(H.have_tools(*NEEDS), "needs ffmpeg/ffprobe/whisper-cli (run in nix develop)")
class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = H.fixtures()
        cls.home = H.new_home("srv")
        cls.srv = H.Server(cls.home)
        cls.jobs = {}
        for f in ("phone-talking.mov", "screen-recording.mp4", "voice-memo.m4a"):
            cls.jobs[f] = cls.srv.upload(cls.fx / f, project="shoot")["job"]
        cls.done = {f: wait_job(cls.home, j) for f, j in cls.jobs.items()}
        st = cls.srv.state()
        cls.ids = {it["name"]: mid for mid, it in st["media"].items()}

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def st(self):
        return self.srv.state()

    def mid(self, name):
        return self.ids[name]

    def new_project(self, name):
        return self.srv.json("POST", "/api/projects", {"name": name})["slug"]

    # ---- shape ----
    def test_state_shape(self):
        st = self.st()
        for k in ("media", "projects", "notes", "jobs", "queued", "aspects", "brands"):
            self.assertIn(k, st)
        self.assertIn("jefe", st["brands"])
        self.assertEqual(set(st["aspects"]) >= {"9:16", "16:9", "1:1", "4:5"}, True)
        it = st["media"][self.mid("phone-talking.mov")]
        for k in ("id", "name", "project", "duration", "width", "height", "has_audio",
                  "has_video", "steps", "moments", "drafts", "keyframes"):
            self.assertIn(k, it)
        self.assertNotIn("path", it, "absolute source paths should not leak to the UI")
        self.assertIsInstance(it["moments"], list)
        self.assertIsInstance(it["drafts"], dict)
        self.assertEqual((it["width"], it["height"]), (1080, 1920))
        p = st["projects"]["shoot"]
        self.assertEqual(set(p["timeline"]) >= {"aspect", "captions", "clips"}, True)
        self.assertIsInstance(st["jobs"], dict)

    # ---- upload pipeline ----
    def test_upload_runs_local_steps(self):
        for f, ev in self.done.items():
            self.assertEqual(ev["type"], "job.done", f"{f}: {ev['data'].get('error')}")
        st = self.st()
        self.assertEqual(len(st["media"]), 3)
        for name in ("phone-talking.mov", "screen-recording.mp4"):
            it = st["media"][self.mid(name)]
            self.assertEqual(it["project"], "shoot")
            for step in ("proxy", "transcribe", "keyframes"):
                self.assertIn(step, it["steps"], f"{name} missing {step}: {it.get('error')}")
            self.assertGreater(len(it["keyframes"]), 3)
            k = it["keyframes"][0]
            self.assertEqual(set(k) >= {"file", "t"}, True)
            s, _, _ = self.srv.request("GET", f"/files/media/{it['id']}/{k['file']}")
            self.assertEqual(s, 200)
        memo = st["media"][self.mid("voice-memo.m4a")]
        self.assertIn("transcribe", memo["steps"])
        leftovers = [p for p in (self.home / "uploads").iterdir()]
        self.assertEqual(leftovers, [], "upload temp files not cleaned up")

    def test_upload_skips_junk_and_dedupes(self):
        n = len(self.st()["media"])
        j1 = self.srv.upload(self.fx / "junk.txt")["job"]
        j2 = self.srv.upload(self.fx / "voice-memo.m4a", project="shoot", name="memo again.m4a")["job"]
        e1, e2 = wait_job(self.home, j1), wait_job(self.home, j2)
        self.assertEqual(e1["type"], "job.done")
        self.assertEqual(e1["data"].get("result", {}).get("ids"), [])
        self.assertEqual(e2["data"].get("result", {}).get("ids"), [self.mid("voice-memo.m4a")])
        self.assertEqual(len(self.st()["media"]), n)

    def test_transcript(self):
        tx = self.srv.json("GET", f"/api/transcript/{self.mid('phone-talking.mov')}")
        self.assertGreater(len(tx["segments"]), 3)
        self.assertGreater(len(tx["words"]), 60)
        w = tx["words"][0]
        self.assertEqual(set(w) >= {"w", "start", "end"}, True)
        empty = self.srv.json("GET", "/api/transcript/doesnotexist")
        self.assertEqual(empty, {"segments": [], "words": []})

    # ---- files ----
    def test_range_requests(self):
        mid = self.mid("phone-talking.mov")
        f = self.home / "media" / mid / "proxy.mp4"
        data = f.read_bytes()
        url = f"/files/media/{mid}/proxy.mp4"
        s, h, body = self.srv.request("GET", url)
        self.assertEqual(s, 200)
        self.assertEqual(body, data)
        self.assertEqual(h.get("accept-ranges"), "bytes")
        self.assertEqual(h.get("content-type"), "video/mp4")
        s, h, body = self.srv.request("GET", url, headers={"Range": "bytes=100-199"})
        self.assertEqual(s, 206)
        self.assertEqual(body, data[100:200])
        self.assertEqual(h["content-range"], f"bytes 100-199/{len(data)}")
        self.assertEqual(int(h["content-length"]), 100)
        s, h, body = self.srv.request("GET", url, headers={"Range": "bytes=1000-"})
        self.assertEqual((s, body), (206, data[1000:]))
        s, h, body = self.srv.request("GET", url, headers={"Range": "bytes=-64"})
        self.assertEqual((s, body), (206, data[-64:]))
        s, h, body = self.srv.request("GET", url, headers={"Range": f"bytes=0-{len(data) * 2}"})
        self.assertEqual((s, body), (206, data), "range past EOF must clamp")
        s, _, _ = self.srv.request("GET", f"/files/media/{mid}/nope.mp4")
        self.assertEqual(s, 404)

    def test_path_traversal_refused(self):
        secret = b"root:x:0:0"
        for raw in ("/files/../../../../etc/passwd", "/files/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
                    "/files/..%2f..%2f..%2f..%2fetc%2fpasswd", "/ui/../studio.py",
                    "/ui/%2e%2e/studio.py"):
            status, body = self.srv.raw_get(raw)
            self.assertIn(status, (400, 403, 404), f"{raw} -> {status}")
            self.assertNotIn(secret, body, raw)
            self.assertNotIn(b"def ingest", body, raw)

    def test_path_traversal_sibling_prefix(self):
        """/files/ must not serve a sibling dir that merely shares HOME's name as a prefix
        (e.g. HOME=/x/studio-srv-abc, file in /x/studio-srv-abc-evil/)."""
        sib = Path(str(self.home) + "-evil")
        sib.mkdir(exist_ok=True)
        (sib / "secret.txt").write_text("SIBLING-SECRET")
        status, body = self.srv.raw_get(f"/files/../{sib.name}/secret.txt")
        self.assertNotIn(b"SIBLING-SECRET", body, "served a file outside STUDIO_HOME")
        self.assertIn(status, (400, 403, 404))

    # ---- timeline ----
    def test_put_timeline_roundtrip_normalizes(self):
        slug = self.new_project("Put Test")
        pt, sr, memo = (self.mid(n) for n in ("phone-talking.mov", "screen-recording.mp4", "voice-memo.m4a"))
        dur = self.st()["media"][pt]["duration"]
        body = {"aspect": "16:9", "captions": False, "clips": [
            {"id": "a1", "media": pt, "in": -3, "out": 9999, "label": "whole"},
            {"id": "b2", "media": sr, "in": 2, "out": 5},
            {"id": "c3", "media": memo, "in": 0, "out": 3},          # audio-only: dropped
            {"id": "d4", "media": "nosuchmedia1", "in": 0, "out": 3},  # dangling: dropped
            {"id": "e5", "media": sr, "in": 4, "out": 4.01}]}          # degenerate: dropped
        st, hdrs, data = self.srv.request("PUT", f"/api/projects/{slug}/timeline", body)
        self.assertEqual(st, 200, data[:300])
        tl, rev = json.loads(data), int(hdrs["x-timeline-rev"])
        self.assertEqual(tl["aspect"], "16:9")
        self.assertIs(tl["captions"], False)
        self.assertEqual([c["id"] for c in tl["clips"]], ["a1", "b2"])
        self.assertEqual(tl["clips"][0]["in"], 0)
        self.assertAlmostEqual(tl["clips"][0]["out"], dur, places=2)
        self.assertEqual(tl["clips"][0]["label"], "whole")
        self.assertEqual(self.st()["projects"][slug]["timeline"], tl)
        again = self.srv.json("PUT", f"/api/projects/{slug}/timeline?base={rev}", tl)
        self.assertEqual(again, tl, "normalize must be idempotent")
        show = H.cli(self.home, "timeline", "show", slug).stdout
        self.assertIn("a1", show)
        ev = [e for e in H.events(self.home) if e["type"] == "timeline.saved"
              and e["data"]["project"] == slug][-1]
        self.assertEqual(ev["data"].get("actor"), "operator")

    def test_put_timeline_stale_base_is_refused(self):
        """?base=<rev> guards the operator's save against overwriting a CLI edit it never saw."""
        slug = self.new_project("Stale Guard")
        sr = self.mid("screen-recording.mp4")
        st, hdrs, data = self.srv.request("PUT", f"/api/projects/{slug}/timeline?base=0",
                                          {"aspect": "9:16", "captions": True,
                                           "clips": [{"id": "x1", "media": sr, "in": 0, "out": 3}]})
        self.assertEqual(st, 200, data[:300])
        rev = int(hdrs["x-timeline-rev"])
        self.assertEqual(self.st()["projects"][slug]["rev"], rev)
        H.cli(self.home, "timeline", "add", slug, sr, actor_claude=True)   # Claude edits
        st, _, data = self.srv.request("PUT", f"/api/projects/{slug}/timeline?base={rev}",
                                       {"aspect": "9:16", "captions": True, "clips": []})
        self.assertEqual(st, 409)
        body = json.loads(data)
        self.assertEqual(body["rev"], rev + 1)
        self.assertEqual(len(body["timeline"]["clips"]), 2, "409 must carry the current story")
        self.assertEqual(len(self.st()["projects"][slug]["timeline"]["clips"]), 2,
                         "the stale save must not have been applied")
        st, _, _ = self.srv.request("PUT", f"/api/projects/{slug}/timeline?base={rev + 1}",
                                    {"aspect": "9:16", "captions": True, "clips": []})
        self.assertEqual(st, 200)

    def test_post_body_not_read_does_not_poison_keepalive(self):
        """A POST whose handler ignores the body (render) must not leak bytes into the next
        request on the same keep-alive connection."""
        import http.client
        slug = self.new_project("Keepalive")
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        try:
            c.request("POST", f"/api/projects/{slug}/render", body=b'{"x": 1}',
                      headers={"Content-Type": "application/json"})
            r = c.getresponse(); r.read()
            self.assertEqual(r.status, 200)
            c.request("GET", "/api/state")
            r = c.getresponse(); data = r.read()
            self.assertEqual(r.status, 200, data[:200])
            self.assertIn(slug, json.loads(data)["projects"])
        finally:
            c.close()
        # the (empty-story) render job finishes one way or the other; don't leave it running
        for e in reversed(H.events(self.home)):
            if e["type"] == "job.queued" and e["data"].get("project") == slug:
                wait_job(self.home, e["data"]["job"], timeout=60)
                break

    def test_progress_events_and_source_field(self):
        evs = H.events(self.home)
        prog = [e for e in evs if e["type"] == "job.progress"]
        self.assertTrue(prog, "uploads should report job.progress")
        steps = {e["data"]["step"] for e in prog}
        self.assertIn("proxy", steps)
        for e in prog:
            if e["data"]["step"] == "waiting":     # queued behind another file: no % yet
                continue
            self.assertTrue(0 <= e["data"]["pct"] <= 100, e)
        act = self.srv.json("GET", "/api/activity")
        self.assertFalse([e for e in act if e["type"] == "job.progress"])
        it = self.st()["media"][self.mid("phone-talking.mov")]
        self.assertRegex(it.get("source", ""), r"^media/[0-9a-f]{12}/source\.mov$")
        st, _, _ = self.srv.request("HEAD", "/files/" + it["source"])
        self.assertEqual(st, 200)
        done = [d for d in self.done.values() if d["data"].get("kind") == "upload"]
        self.assertTrue(all(d["data"].get("result", {}).get("ids") for d in done),
                        "upload job.done should name the new media ids")

    def test_sse_delivers_cli_edit_within_1s(self):
        slug = self.new_project("SSE")
        sse = H.SSE(self.srv.port)
        try:
            self.assertEqual(getattr(sse, "status", None), 200)
            lat = []
            for i in range(3):
                n0 = len(sse.events)
                H.cli(self.home, "timeline", "add", slug, self.mid("screen-recording.mp4"),
                      i, i + 2, actor_claude=True)
                t_exit = time.time()
                t, ev = sse.wait_for(lambda e: e["type"] == "timeline.saved"
                                     and e["data"]["project"] == slug, since=n0, timeout=3)
                self.assertIsNotNone(ev, "no SSE event for the CLI edit within 3s")
                self.assertEqual(ev["data"].get("actor"), "claude")
                self.assertEqual(len(ev["data"]["timeline"]["clips"]), i + 1)
                lat.append(t - t_exit)
            self.assertLess(max(lat), 1.0, f"SSE latency after CLI exit: {lat}")
        finally:
            sse.close()

    # ---- operator <-> agent ----
    def test_notes_and_focus(self):
        ev = self.srv.json("POST", "/api/notes", {"text": "  cut the um at 0:12  ", "project": "shoot",
                                                  "focus": {"clip": 1}})
        self.assertEqual(ev["data"]["text"], "cut the um at 0:12")
        notes = self.st()["notes"]
        self.assertTrue(any(n["text"] == "cut the um at 0:12" and n.get("actor") == "operator"
                            for n in notes))
        self.assertIn("cut the um at 0:12", H.cli(self.home, "notes", "-p", "shoot").stdout)
        focus = {"project": "shoot", "selection": {"media": self.mid("phone-talking.mov")},
                 "playhead": 12.3}
        self.assertEqual(self.srv.json("POST", "/api/focus", focus), {"ok": True})
        self.assertEqual(json.loads(H.cli(self.home, "focus").stdout), focus)

    def test_activity_has_actor(self):
        act = self.srv.json("GET", "/api/activity")
        self.assertIsInstance(act, list)
        self.assertLessEqual(len(act), 120)
        self.assertTrue(all("type" in e and "data" in e for e in act))
        self.assertFalse(any(e["type"] == "job.queued" for e in act))

    def test_move_media(self):
        slug = self.new_project("Moved Here")
        memo = self.mid("voice-memo.m4a")
        self.srv.json("POST", "/api/move", {"media": memo, "project": slug})
        self.assertEqual(self.st()["media"][memo]["project"], slug)
        self.srv.json("POST", "/api/move", {"media": memo, "project": "shoot"})

    def test_moments_and_drafts_surface(self):
        pt = self.mid("phone-talking.mov")
        ms = H.fake_moments(self.home, pt, self.st()["media"][pt]["duration"], n=4)
        H.fake_drafts(self.home, pt, ms)
        it = self.st()["media"][pt]
        self.assertEqual([m["n"] for m in it["moments"]], [1, 2, 3, 4])
        self.assertEqual(set(it["moments"][0]) >= {"n", "start", "end", "title", "hook", "pillar",
                                                   "why", "angle", "kind"}, True)
        self.assertEqual(len(it["drafts"]["posts"]), 4)
        self.assertIn("moments", it["steps"])
        self.assertIn("draft", it["steps"])

    def test_operator_draft_edits_reach_the_log(self):
        pt = self.mid("phone-talking.mov")
        ms = H.fake_moments(self.home, pt, self.st()["media"][pt]["duration"], n=2)
        H.fake_drafts(self.home, pt, ms)
        key = f"{ms[0]['start']:.2f}-{ms[0]['end']:.2f}"
        self.srv.json("POST", f"/api/drafts/{pt}", {"key": key, "field": "x", "n": 1, "text": "mine"})
        self.assertEqual(self.st()["media"][pt]["draft_edits"][key]["x"], "mine")
        ev = [e for e in H.events(self.home) if e["type"] == "draft.edited"][-1]
        self.assertEqual(ev["data"]["actor"], "operator")
        self.srv.json("POST", f"/api/drafts/{pt}", {"key": key, "field": "x", "n": 1, "text": None})
        self.assertNotIn(key, self.st()["media"][pt].get("draft_edits") or {})
        s, _, _ = self.srv.request("POST", "/api/drafts/000000000000", {"key": "k", "field": "x", "text": "t"})
        self.assertEqual(s, 404)
        # new moments make old rewrites meaningless: they are dropped
        self.srv.json("POST", f"/api/drafts/{pt}", {"key": key, "field": "x", "n": 1, "text": "mine"})
        H.fake_moments(self.home, pt, self.st()["media"][pt]["duration"], n=2)
        self.assertFalse(self.st()["media"][pt].get("draft_edits"))

    def test_note_ids_and_queued_jobs_in_state(self):
        ev = self.srv.json("POST", "/api/notes", {"text": "hello", "project": "shoot"})
        self.assertTrue(ev["data"].get("id"))
        st = self.st()
        self.assertIn("queued_jobs", st)
        self.assertIsInstance(st["queued_jobs"], list)

    def test_run_local_step(self):
        sr = self.mid("screen-recording.mp4")
        job = self.srv.json("POST", "/api/run", {"ids": [sr], "steps": ["keyframes", "rm -rf"],
                                                 "force": True})["job"]
        ev = wait_job(self.home, job, 120)
        self.assertEqual(ev["type"], "job.done")
        started = [e for e in H.events(self.home) if e["type"] == "job.started"
                   and e["data"]["job"] == job][0]
        self.assertEqual(started["data"]["steps"], ["keyframes"], "unknown steps must be filtered")

    def test_project_create_endpoint(self):
        a = self.new_project("Same Name")
        b = self.new_project("Same Name")
        self.assertNotEqual(a, b)
        self.assertIn(a, self.st()["projects"])
        s, _, _ = self.srv.request("POST", "/api/projects", {})
        self.assertGreaterEqual(s, 400)

    # ---- render ----
    def test_render_job(self):
        slug = self.new_project("Render Job")
        pt, sr = self.mid("phone-talking.mov"), self.mid("screen-recording.mp4")
        tl = {"aspect": "1:1", "captions": True, "clips": [
            {"media": pt, "in": 3, "out": 7}, {"media": sr, "in": 0, "out": 3}]}
        self.srv.json("PUT", f"/api/projects/{slug}/timeline", tl)
        job = self.srv.json("POST", f"/api/projects/{slug}/render")["job"]
        ev = wait_job(self.home, job, 600)
        self.assertEqual(ev["type"], "job.done", ev["data"].get("error"))
        exports = self.st()["projects"][slug].get("exports", [])
        self.assertEqual(len(exports), 1)
        ex = exports[0]
        self.assertEqual(set(ex) >= {"file", "duration", "aspect", "ts"}, True)
        self.assertEqual(ex["aspect"], "1:1")
        self.assertAlmostEqual(ex["duration"], 7, delta=0.1)
        self.assertEqual(ev["data"].get("result", {}).get("file"), ex["file"])
        s, h, body = self.srv.request("GET", f"/files/{ex['file']}", headers={"Range": "bytes=0-7"})
        self.assertEqual(s, 206)
        self.assertEqual(body[4:8], b"ftyp")
        info = H.ffprobe(self.home / ex["file"])
        self.assertEqual((info["width"], info["height"]), (1080, 1080))
        self.assertAlmostEqual(info["duration"], 7, delta=0.5)
        self.assertTrue(info["has_audio"])
        self.assertTrue(any(e["type"] == "render.done" and e["data"].get("actor") == "operator"
                            for e in H.events(self.home)))

    def test_render_empty_fails_cleanly(self):
        slug = self.new_project("Empty Render")
        job = self.srv.json("POST", f"/api/projects/{slug}/render")["job"]
        ev = wait_job(self.home, job, 60)
        self.assertEqual(ev["type"], "job.failed")
        self.assertIn("empty", ev["data"].get("error", ""))
        self.assertFalse(self.st()["jobs"], "no job should be left running")


    # ---- regressions: data safety ----
    def _cut_upload(self, path, name, frac=0.6):
        import socket
        data = Path(path).read_bytes()
        s = socket.create_connection(("127.0.0.1", self.srv.port), 10)
        s.sendall(f"POST /api/upload?project=shoot&name={name} HTTP/1.1\r\nHost: x\r\n"
                  f"Content-Length: {len(data)}\r\n\r\n".encode() + data[: int(len(data) * frac)])
        s.shutdown(socket.SHUT_WR)              # the connection drops before the rest arrives
        resp = b""
        while chunk := s.recv(65536):
            resp += chunk
        s.close()
        head, _, body = resp.partition(b"\r\n\r\n")
        return int(head.split()[1]), body

    def test_cut_off_upload_is_refused(self):
        """S1: an upload that stops part way is deleted and answered 400 "upload incomplete";
        nothing is ingested (also when the file's index is at the front, so it would probe
        as complete)."""
        import subprocess
        fs = self.home / "faststart.mov"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(self.fx / "phone-talking.mov"),
                        "-map", "0", "-c", "copy", "-movflags", "+faststart", str(fs)], check=True)
        n_events = len(H.events(self.home))
        for src, name in ((self.fx / "site-walk.mov", "cut-walk.mov"), (fs, "cut-faststart.mov")):
            status, body = self._cut_upload(src, name)
            self.assertEqual(status, 400, body[:200])
            self.assertIn("upload incomplete", json.loads(body)["error"])
        fs.unlink()
        time.sleep(0.5)
        new = H.events(self.home)[n_events:]
        self.assertFalse([e for e in new if e["type"] in ("ingested", "job.queued")], new[:3])
        self.assertEqual([p for p in (self.home / "uploads").iterdir()], [], "partial upload left behind")
        self.assertNotIn("cut-walk.mov", {it["name"] for it in self.st()["media"].values()})

    def test_unreadable_upload_fails_loudly(self):
        """S1: complete bytes that aren't usable footage fail the job (the UI toasts it) and
        leave no media dir; ingest refuses an unreadable source."""
        before = {p.name for p in (self.home / "media").iterdir()}
        junk = self.home / "garbage.mov"
        junk.write_bytes(b"\x00\x00\x00\x18ftypqt  " + os.urandom(20000))
        job = self.srv.upload(junk, project="shoot")["job"]
        junk.unlink()
        ev = wait_job(self.home, job, 60)
        self.assertEqual(ev["type"], "job.failed")
        self.assertIn("couldn't add garbage.mov", ev["data"]["error"])
        self.assertEqual({p.name for p in (self.home / "media").iterdir()}, before)
        self.assertEqual([p for p in (self.home / "uploads").iterdir()], [])

    def test_same_name_uploads_never_share_a_file(self):
        """S4: every upload gets its own temp file (same name, same instant), keeping its suffix."""
        import threading
        sys.path.insert(0, str(H.ROOT))
        os.environ["STUDIO_HOME"] = str(self.home)
        import server as srvmod
        srvmod.S.HOME = self.home
        paths = []
        ts = [threading.Thread(target=lambda: paths.append(srvmod.upload_path("IMG_0001.MOV"))) for _ in range(20)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(len(set(paths)), 20)
        self.assertTrue(all(p.suffix == ".mov" and p.parent == self.home / "uploads" for p in paths))
        for p in paths:
            p.unlink()
        # and end to end (own library, so the shared one keeps its 3 files): two different
        # files, both called clip.m4a, uploaded at the same time
        home = H.new_home("same-name")
        a, b = home / "a.m4a", home / "b.m4a"
        import hashlib, shutil
        shutil.copy(self.fx / "voice-memo.m4a", a)
        shutil.copy(self.fx / "voice-memo.m4a", b)
        with open(b, "ab") as f:
            f.write(b"\0" * 64)      # different bytes, still a readable file
        with H.Server(home) as srv:
            jobs = []
            ts = [threading.Thread(target=lambda f=f: jobs.append(srv.upload(f, project="shoot", name="clip.m4a")["job"]))
                  for f in (a, b)]
            for t in ts: t.start()
            for t in ts: t.join()
            evs = [wait_job(home, j, 120) for j in jobs]
        ids = sorted(e["data"]["result"]["ids"][0] for e in evs)
        want = sorted(hashlib.sha256(f.read_bytes()).hexdigest()[:12] for f in (a, b))
        self.assertEqual(ids, want, "each upload must be ingested from its own bytes")

    def test_first_save_of_new_project_cannot_clobber(self):
        """S3: a new project has rev 0; a PUT based on rev 0 (or with no base at all) after
        Claude saved is refused with 409 and the current story, instead of discarding Claude's
        edit."""
        slug = self.new_project("Fresh Race")
        self.assertEqual(self.st()["projects"][slug]["rev"], 0)
        sr = self.mid("screen-recording.mp4")
        H.cli(self.home, "timeline", "add", slug, sr, "0", "4", actor_claude=True)
        mine = {"aspect": "9:16", "captions": True, "clips": [{"id": "op1", "media": sr, "in": 5, "out": 9}]}
        for q in ("?base=0", ""):
            st, _, data = self.srv.request("PUT", f"/api/projects/{slug}/timeline{q}", mine)
            self.assertEqual(st, 409, f"PUT{q or ' (no base)'} -> {st}")
            self.assertEqual(json.loads(data)["rev"], 1)
        clips = self.st()["projects"][slug]["timeline"]["clips"]
        self.assertEqual(len(clips), 1)
        self.assertNotEqual(clips[0]["id"], "op1", "Claude's clip was overwritten")

    def test_fix_word_endpoint(self):
        """Q1d: POST /api/words/ID/fix fixes one word (index) or every one (all), emits
        transcript.edited and changes the transcript's receipt in /api/state."""
        pt = self.mid("phone-talking.mov")
        tx = self.srv.json("GET", f"/api/transcript/{pt}")
        i = next(k for k, w in enumerate(tx["words"]) if w["w"].strip(".,").lower() == "sprinkler")
        sha0 = self.st()["media"][pt]["steps"]["transcribe"]["sha256"]
        r = self.srv.json("POST", f"/api/words/{pt}/fix", {"index": i, "wrong": "sprinkler", "right": "Sprinklr"})
        self.assertEqual(r, {"changed": 1, "files": 1})
        tx2 = self.srv.json("GET", f"/api/transcript/{pt}")
        self.assertEqual(tx2["words"][i]["w"].strip(".,"), "Sprinklr")
        self.assertIn("Sprinklr", " ".join(s["text"] for s in tx2["segments"]))
        self.assertNotEqual(self.st()["media"][pt]["steps"]["transcribe"]["sha256"], sha0)
        ev = [e for e in H.events(self.home) if e["type"] == "transcript.edited"][-1]
        self.assertEqual((ev["media"], ev["data"]["actor"]), (pt, "operator"))
        r = self.srv.json("POST", f"/api/words/{pt}/fix", {"wrong": "Sprinklr", "right": "sprinkler", "all": True})
        self.assertEqual(r["changed"], 1)
        self.assertNotIn("Sprinklr", json.dumps(self.srv.json("GET", f"/api/transcript/{pt}")))
        s, _, _ = self.srv.request("POST", "/api/words/000000000000/fix", {"index": 0, "right": "x"})
        self.assertEqual(s, 404)


@unittest.skipUnless(H.have_tools(*NEEDS), "needs ffmpeg/ffprobe/whisper-cli (run in nix develop)")
@unittest.skipUnless(H.have_tools(*NEEDS), "needs ffmpeg/ffprobe/whisper-cli (run in nix develop)")
class DesktopApp(unittest.TestCase):
    """What the Mac app relies on: /api/health, the library lockfile, adding files by path
    (no upload, never moving or changing the original), and exiting with its parent."""

    def test_health_lockfile_and_cleanup(self):
        home = H.new_home("lock")
        with H.Server(home) as srv:
            h = srv.json("GET", "/api/health")
            self.assertEqual((h["app"], h["port"], h["owner"]), ("studio", srv.port, True))
            self.assertEqual(Path(h["home"]), home.resolve())
            self.assertIn("exists", h["model"])
            lock = json.loads((home / ".server.json").read_text())
            self.assertEqual((lock["pid"], lock["port"]), (h["pid"], srv.port))
            with H.Server(home) as second:      # a second server on the same library
                h2 = second.json("GET", "/api/health")
                self.assertFalse(h2["owner"])
                self.assertEqual(json.loads((home / ".server.json").read_text())["pid"], h["pid"],
                                 "only the owner writes the lockfile")
        self.assertFalse((home / ".server.json").exists(), "SIGTERM removes the lockfile")

    def test_ingest_paths_folder_never_moves_and_dedupes(self):
        home = H.new_home("paths")
        fx = H.fixtures()
        own = H.new_home("operator") / "Footage"
        (own / "sub").mkdir(parents=True)
        (own / ".hidden").mkdir()
        import shutil, hashlib
        shutil.copy(fx / "site-walk.mov", own / "site-walk.mov")
        shutil.copy(fx / "voice-memo.m4a", own / "sub" / "voice-memo.m4a")
        shutil.copy(fx / "junk.txt", own / "notes.txt")
        shutil.copy(fx / "voice-memo.m4a", own / ".hidden" / "x.m4a")
        # a file already sitting in this library's inbox: added by path, it must stay there
        shutil.copy(fx / "screen-recording.mp4", home / "inbox" / "screen-recording.mp4")
        old = time.time() - 600
        for f in list(own.rglob("*")) + [home / "inbox" / "screen-recording.mp4"]:
            os.utime(f, (old, old))
        sig = lambda f: (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns, f.stat().st_ino)
        files = [f for f in own.rglob("*") if f.is_file()] + [home / "inbox" / "screen-recording.mp4"]
        before = {f: sig(f) for f in files}
        with H.Server(home) as srv:
            st, _, body = srv.request("POST", "/api/ingest-paths", json.dumps({"paths": [str(own)]}).encode(),
                                      headers={"Content-Type": "text/plain"})
            self.assertEqual(st, 415, "a form-style (non-JSON) request is refused")
            r = srv.json("POST", "/api/ingest-paths", {"paths": [str(own), str(home / "inbox" / "screen-recording.mp4"),
                                                                 "relative.mov", str(own / "gone.mov")], "project": "shoot"})
            self.assertEqual(sorted(r["files"]), ["screen-recording.mp4", "site-walk.mov", "voice-memo.m4a"])
            self.assertEqual({x["name"]: x["reason"] for x in r["skipped"]},
                             {"notes.txt": "not a video or audio file", "relative.mov": "not a full path",
                              "gone.mov": "it's not there any more"})
            for j in r["jobs"]:
                self.assertEqual(wait_job(home, j)["type"], "job.done")
            again = srv.json("POST", "/api/ingest-paths", {"paths": [str(own / "site-walk.mov")], "project": "shoot"})
            wait_job(home, again["jobs"][0])
            st = srv.state()
        self.assertEqual(len(st["media"]), 3, "the second add of the same file is deduped by hash")
        for m in st["media"].values():
            self.assertEqual(m["project"], "shoot")
            for s in ("proxy", "transcribe", "keyframes"):
                self.assertIn(s, m["steps"])
        self.assertEqual({f: sig(f) for f in files}, before, "originals untouched, nothing moved (even from inbox/)")

    def test_ingest_paths_waits_for_a_file_still_being_written(self):
        home = H.new_home("settle")
        fx = H.fixtures()
        f = H.new_home("operator") / "late.mov"
        data = (fx / "site-walk.mov").read_bytes()
        f.write_bytes(data[: len(data) // 2])
        env = {"STUDIO_SETTLE_SECS": "1.5"}
        os.environ.update(env)
        try:
            with H.Server(home) as srv:
                job = srv.json("POST", "/api/ingest-paths", {"paths": [str(f)]})["jobs"][0]
                time.sleep(0.6)
                f.write_bytes(data)          # the copy finishes
                done = wait_job(home, job, 120)
        finally:
            for k in env:
                os.environ.pop(k, None)
        self.assertEqual(done["type"], "job.done", done)
        ing = [e for e in H.events(home) if e["type"] == "ingested"]
        self.assertEqual(len(ing), 1)
        self.assertEqual(H.ffprobe(ing[0]["data"]["path"])["duration"], H.ffprobe(fx / "site-walk.mov")["duration"])

    def test_exits_when_its_parent_goes(self):
        home = H.new_home("parent")
        port = H.free_port()
        # parent: a shell that starts the server (with the app's env) and then is killed
        code = (f"import os, subprocess, sys, time; p = subprocess.Popen([sys.executable, {str(H.STUDIO_PY)!r}, 'ui', "
                f"'--no-open', '--port', '{port}'], env=dict(os.environ, STUDIO_PARENT_PID=str(os.getpid())), "
                f"stdin=subprocess.PIPE, process_group=0); print(p.pid, flush=True); time.sleep(600)")
        parent = subprocess.Popen([sys.executable, "-c", code], env=H.env_for(home), stdout=subprocess.PIPE, text=True)
        child = int(parent.stdout.readline())
        deadline = time.time() + 20
        while time.time() < deadline and not (home / ".server.json").exists():
            time.sleep(0.1)
        self.assertTrue((home / ".server.json").exists())
        parent.kill(); parent.wait()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            self.fail("server outlived its parent")
        self.assertFalse((home / ".server.json").exists())


class Clone(unittest.TestCase):
    def test_clone_or_copy(self):
        S = H.import_studio(H.new_home("clone"))
        d = H.new_home("clone2")
        src = d / "a.bin"
        src.write_bytes(os.urandom(1 << 20))
        how = S.clone_or_copy(src, d / "b.bin")
        self.assertEqual((d / "b.bin").read_bytes(), src.read_bytes())
        if sys.platform == "darwin":
            self.assertEqual(how, "clone", "same APFS volume: a clone")
        with self.assertRaises(FileNotFoundError):
            S.clone_or_copy(d / "missing.bin", d / "c.bin")


class Restart(unittest.TestCase):
    """S6/S7: a server started after a crash cleans uploads/, puts half-ingested footage back
    in inbox/, and finishes local steps that never ran."""

    def test_restart_heals(self):
        fx = H.fixtures()
        home = H.new_home("heal")
        H.cli(home, "ingest", "-p", "shoot", fx / "screen-recording.mp4")
        mid = [e["media"] for e in H.events(home) if e["type"] == "ingested"][0]
        # crash: keyframes never finished, an upload was half written, an ingest died mid-move
        log = home / "events.jsonl"
        log.write_text("".join(l + "\n" for l in log.read_text().splitlines()
                               if '"step.keyframes"' not in l))
        (home / "uploads" / "up-dead.mov").write_bytes(b"half an upload")
        dead = home / "media" / "0123456789ab"; dead.mkdir()
        (dead / "source.mov").write_bytes(b"the operator's footage")
        (dead / ".ingesting").write_text(json.dumps({"pid": 999999, "name": "walk.mov", "moved": True}))
        with H.Server(home) as srv:
            ok = srv.wait(lambda st: "keyframes" in st["media"][mid]["steps"] and not st["jobs"], 120,
                          what="keyframes resumed after restart")
            self.assertTrue(ok)
            self.assertEqual(list((home / "uploads").iterdir()), [])
            self.assertFalse(dead.exists())
            self.assertEqual((home / "inbox" / "walk.mov").read_bytes(), b"the operator's footage")
            started = [e for e in H.events(home) if e["type"] == "job.started" and e["data"].get("resumed")]
            self.assertEqual(started[0]["data"]["ids"], [mid])
        # a healthy library: a restart resubmits nothing
        n = len(H.events(home))
        with H.Server(home) as srv:
            time.sleep(1.0)
        self.assertFalse([e for e in H.events(home)[n:] if e["type"] == "job.queued"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
