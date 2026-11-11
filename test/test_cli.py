"""CLI contract tests for studio.py. Run inside the devshell:

    cd forge/studio && nix develop -c python3 -m unittest test.test_cli -v
    (or test/run_all.sh)

One shared temp STUDIO_HOME gets all fixtures ingested once (that runs the real proxy /
whisper / keyframes steps); each test then works in its own project. The CLI tests never
call an agent: moments.json is written by hand and step.moments emitted through the studio
module. The Agents tests run moments/draft through fake agents (test/fakeagents).
"""
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

NEEDS = ("ffmpeg", "ffprobe", "whisper-cli")


def settled(path, ago=60):
    """A copy that finished `ago` seconds ago (ingest waits for files still being written)."""
    os.utime(path, (time.time() - ago, time.time() - ago))
    return path


@unittest.skipUnless(H.have_tools(*NEEDS), "needs ffmpeg/ffprobe/whisper-cli (run in nix develop)")
class CLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = H.fixtures()
        cls.home = H.new_home("cli")
        files = [cls.fx / f for f in H.FIXTURE_FILES]
        cls.ingest_out = H.cli(cls.home, "ingest", "-p", "shoot", *files).stdout
        cls.S = H.import_studio(cls.home)
        cls.ids = {}
        for mid, it in cls.S.state()["media"].items():
            cls.ids[it["name"]] = mid

    def setUp(self):
        self.S.HOME = self.home  # other tests may repoint the module

    # ---- helpers ----
    def st(self):
        self.S.HOME = self.home
        return self.S.state()

    def tl(self, slug):
        return self.st()["projects"][slug]["timeline"]

    def mid(self, name):
        return self.ids[name]

    def dur(self, name):
        return self.st()["media"][self.mid(name)]["duration"]

    def project(self, name):
        return H.cli(self.home, "project", "new", name).stdout.strip().splitlines()[-1]

    def last_saved(self, slug):
        evs = [e for e in H.events(self.home) if e["type"] == "timeline.saved"
               and e["data"]["project"] == slug]
        return evs[-1] if evs else None

    # ---- ingest ----
    def test_ingest_probes_every_fixture(self):
        self.assertEqual(set(self.ids), {"phone-talking.mov", "screen-recording.mp4",
                                         "site-walk.mov", "voice-memo.m4a"})
        lib = self.st()["media"]
        p = lib[self.mid("phone-talking.mov")]
        self.assertEqual((p["width"], p["height"]), (1080, 1920), "iPhone rotation not honoured")
        self.assertAlmostEqual(p["duration"], 45, delta=0.5)
        self.assertTrue(p["has_audio"] and p["has_video"])
        s = lib[self.mid("screen-recording.mp4")]
        self.assertEqual((s["width"], s["height"]), (1170, 2532))
        self.assertFalse(s["has_audio"])
        w = lib[self.mid("site-walk.mov")]
        self.assertEqual((w["width"], w["height"]), (1920, 1080))
        v = lib[self.mid("voice-memo.m4a")]
        self.assertFalse(v["has_video"])
        self.assertTrue(v["has_audio"])
        for it in lib.values():
            self.assertEqual(it["project"], "shoot")
            self.assertEqual(len(it["id"]), 12)
            self.assertTrue(it["sha256"].startswith(it["id"]))

    def test_ingest_skips_non_media(self):
        self.assertIn("junk.txt", self.ingest_out)
        self.assertNotIn("junk.txt", self.ids)
        self.assertFalse(any(e["data"].get("name") == "junk.txt" for e in H.events(self.home)
                             if e["type"] == "ingested"))

    def test_ingest_leaves_external_originals(self):
        for f in H.FIXTURE_FILES:
            self.assertTrue((self.fx / f).exists(), f"ingest moved/deleted fixture {f}")

    def test_local_steps_ran(self):
        lib = self.st()["media"]
        for name in ("phone-talking.mov", "screen-recording.mp4", "site-walk.mov"):
            it = lib[self.mid(name)]
            for step in ("proxy", "transcribe", "keyframes"):
                self.assertIn(step, it["steps"], f"{name} missing {step}: {it.get('error')}")
            d = self.home / "media" / it["id"]
            pr = H.ffprobe(d / "proxy.mp4")
            self.assertEqual(pr["width"] > pr["height"], it["width"] > it["height"],
                             f"{name} proxy orientation differs from source")
            self.assertEqual(min(pr["width"], pr["height"]), 720)
            self.assertEqual(pr["has_audio"], it["has_audio"])
            self.assertAlmostEqual(pr["duration"], it["duration"], delta=0.5)
            kf = json.loads((d / "keyframes" / "index.json").read_text())
            self.assertGreater(len(kf), 3)
            for k in kf:
                self.assertTrue((d / k["file"]).exists())
        # speech actually transcribed
        tx = json.loads((self.home / "media" / self.mid("phone-talking.mov") / "transcript.json")
                        .read_text())
        text = " ".join(w["w"] for w in tx["words"]).lower()
        self.assertGreater(len(tx["words"]), 60)
        self.assertTrue("sprinkler" in text or "estimate" in text, text[:200])
        for w in tx["words"]:
            self.assertLessEqual(w["start"], w["end"])
        walk = json.loads((self.home / "media" / self.mid("site-walk.mov") / "transcript.json")
                          .read_text())
        self.assertGreater(len(walk["words"]), 60, "speech under noise not transcribed")
        silent = lib[self.mid("screen-recording.mp4")]["steps"]["transcribe"]
        self.assertEqual(silent.get("words"), 0)
        memo = lib[self.mid("voice-memo.m4a")]
        self.assertIn("transcribe", memo["steps"])
        self.assertIn("skipped", memo["steps"].get("proxy", {"skipped": "absent ok"}))

    def test_ingest_dedupes_by_hash(self):
        n_before = sum(1 for e in H.events(self.home) if e["type"] == "ingested")
        tmp = H.new_home("dup") / "renamed-copy.mov"
        settled(shutil.copy(self.fx / "phone-talking.mov", tmp))
        out = H.cli(self.home, "ingest", "-p", "shoot", self.fx / "phone-talking.mov", tmp).stdout
        n_after = sum(1 for e in H.events(self.home) if e["type"] == "ingested")
        self.assertEqual(n_before, n_after, "same bytes ingested twice")
        self.assertEqual(len(self.st()["media"]), 4)
        self.assertIn(self.mid("phone-talking.mov"), out)

    def test_ingest_from_inbox(self):
        home = H.new_home("inbox")
        settled(shutil.copy(self.fx / "voice-memo.m4a", home / "inbox" / "memo.m4a"))
        settled(shutil.copy(self.fx / "junk.txt", home / "inbox" / "junk.txt"))
        H.cli(home, "ingest")
        self.assertFalse((home / "inbox" / "memo.m4a").exists(), "inbox media not moved")
        self.assertTrue((home / "inbox" / "junk.txt").exists(), "non-media should stay put")
        ing = [e for e in H.events(home) if e["type"] == "ingested"]
        self.assertEqual(len(ing), 1)
        self.assertEqual(ing[0]["data"]["project"], "inbox")

    # ---- projects ----
    def test_project_new_and_ls(self):
        a = self.project("Jefe Launch Week!")
        b = self.project("Jefe Launch Week!")
        self.assertEqual(a, "jefe-launch-week")
        self.assertEqual(b, "jefe-launch-week-2")
        ls = H.cli(self.home, "project", "ls").stdout
        for s in (a, b, "shoot"):
            self.assertIn(s, ls)
        self.assertTrue((self.home / "projects" / a).is_dir())
        p = self.st()["projects"][a]
        self.assertEqual(p["timeline"], {"aspect": "9:16", "captions": True, "clips": []})
        self.assertEqual(p["brand"], "jefe")

    def test_ls_and_log(self):
        out = H.cli(self.home, "ls").stdout
        for mid in self.ids.values():
            self.assertIn(mid, out)
        self.assertNotIn(self.mid("site-walk.mov"), H.cli(self.home, "ls", "-p", "nope").stdout)
        log = H.cli(self.home, "log", "-n", "5").stdout.strip().splitlines()
        self.assertEqual(len(log), 5)

    # ---- timeline ----
    def test_timeline_edit_ops(self):
        slug = self.project("Edit Ops")
        pt, sr, sw = (self.mid(n) for n in ("phone-talking.mov", "screen-recording.mp4", "site-walk.mov"))
        H.cli(self.home, "timeline", "add", slug, pt[:6], "2", "8")
        H.cli(self.home, "timeline", "add", slug, sr)                       # full length
        H.cli(self.home, "timeline", "add", slug, sw, "10", "18", "--at", "1")   # 1-based: first
        clips = self.tl(slug)["clips"]
        self.assertEqual([c["media"] for c in clips], [sw, pt, sr])
        self.assertEqual((clips[1]["in"], clips[1]["out"]), (2, 8))
        self.assertAlmostEqual(clips[2]["out"], self.dur("screen-recording.mp4"), places=2)
        self.assertEqual(len({c["id"] for c in clips}), 3, "clip ids must be unique")

        H.cli(self.home, "timeline", "trim", slug, "2", "3.5", "7.25")     # clip 2 (1-based)
        c = self.tl(slug)["clips"][1]
        self.assertEqual((c["in"], c["out"]), (3.5, 7.25))

        H.cli(self.home, "timeline", "move", slug, "1", "3")
        self.assertEqual([c["media"] for c in self.tl(slug)["clips"]], [pt, sr, sw])

        H.cli(self.home, "timeline", "split", slug, "1", "1.25")
        clips = self.tl(slug)["clips"]
        self.assertEqual(len(clips), 4)
        self.assertEqual((clips[0]["in"], clips[0]["out"]), (3.5, 4.75))
        self.assertEqual((clips[1]["in"], clips[1]["out"]), (4.75, 7.25))
        self.assertNotEqual(clips[0]["id"], clips[1]["id"])

        # flags ride along on any mutating action
        H.cli(self.home, "timeline", "rm", slug, "3", "--aspect", "16:9", "--captions", "off")
        self.assertEqual([c["media"] for c in self.tl(slug)["clips"]], [pt, pt, sw])

        show = H.cli(self.home, "timeline", "show", slug).stdout
        self.assertIn("total", show)
        self.assertIn("phone-talking.mov", show)
        self.assertEqual(len([l for l in show.splitlines() if pt in l or sw in l]), 3)
        t = self.tl(slug)
        self.assertEqual((t["aspect"], t["captions"]), ("16:9", False))

        H.cli(self.home, "timeline", "clear", slug)
        self.assertEqual(self.tl(slug)["clips"], [])
        self.assertEqual(self.tl(slug)["aspect"], "16:9", "clear should keep settings")
        # show with --aspect/--captions applies them (it used to ignore them silently)
        out = H.cli(self.home, "timeline", "show", slug, "--aspect", "1:1", "--captions", "on").stdout
        self.assertIn("aspect 1:1", out)
        self.assertEqual((self.tl(slug)["aspect"], self.tl(slug)["captions"]), ("1:1", True))
        n = len(H.events(self.home))
        H.cli(self.home, "timeline", "show", slug)
        self.assertEqual(len(H.events(self.home)), n, "plain show must not write")

    def test_timeline_add_moment(self):
        slug = self.project("Moments")
        pt = self.mid("phone-talking.mov")
        ms = H.fake_moments(self.home, pt, self.dur("phone-talking.mov"), n=3, title="Kitchen table")
        self.S.HOME = self.home
        H.cli(self.home, "timeline", "add", slug, f"{pt}@2", "--no-snap")
        c = self.tl(slug)["clips"][0]
        self.assertEqual((c["in"], c["out"]), (ms[1]["start"], ms[1]["end"]))
        self.assertEqual(c.get("label"), "Kitchen table 2")
        self.assertIn("moments", self.st()["media"][pt]["steps"])
        # by default a moment goes on speech edges; this fixture talks without a pause, so
        # the cuts stay where they were asked, at the quietest moment nearby
        H.cli(self.home, "timeline", "add", slug, f"{pt}@2")
        c = self.tl(slug)["clips"][1]
        self.assertLessEqual(abs(c["in"] - ms[1]["start"]), 0.4, c)
        self.assertLessEqual(abs(c["out"] - ms[1]["end"]), 0.6, c)
        self.assertEqual(c.get("moment"), 2)

    def test_timeline_actor(self):
        slug = self.project("Actor")
        H.cli(self.home, "timeline", "add", slug, self.mid("site-walk.mov"), "0", "3")
        self.assertEqual(self.last_saved(slug)["data"].get("actor"), "cli")
        H.cli(self.home, "timeline", "add", slug, self.mid("site-walk.mov"), "3", "6",
              as_agent=True)
        self.assertEqual(self.last_saved(slug)["data"].get("actor"), "agent")

    def test_agent_is_recognised_by_its_shell(self):
        """Each agent's shell marker makes the CLI's edits the agent's; STUDIO_ACTOR overrides."""
        slug = self.project("Markers")
        sw = self.mid("site-walk.mov")
        for marker, expect in [({"CLAUDECODE": "1"}, "agent"), ({"CODEX_THREAD_ID": "t-1"}, "agent"),
                               ({"GOOSE_TERMINAL": "1"}, "agent"), ({"STUDIO_ACTOR": "agent"}, "agent"),
                               ({"CLAUDECODE": "1", "STUDIO_ACTOR": "cli"}, "cli"), ({}, "cli")]:
            H.cli(self.home, "timeline", "add", slug, sw, "0", "2", env=H.env_for(self.home, **marker))
            self.assertEqual(self.last_saved(slug)["data"].get("actor"), expect, marker)

    def test_timeline_normalize_clamps(self):
        slug = self.project("Clamp")
        pt, memo = self.mid("phone-talking.mov"), self.mid("voice-memo.m4a")
        d = self.dur("phone-talking.mov")
        H.cli(self.home, "timeline", "add", slug, pt, "5", "9")
        r = H.cli(self.home, "timeline", "trim", slug, "1", "-5", "99999")
        self.assertIn("clamped", r.stderr, "a clamp must be reported, not silent")
        c = self.tl(slug)["clips"][0]
        self.assertEqual(c["in"], 0)
        self.assertAlmostEqual(c["out"], d, places=2)
        # a degenerate (<0.1s or reversed) trim is refused; the clip is NOT silently deleted
        n = len(H.events(self.home))
        for args in (("4", "4.05"), ("5", "2")):
            r = H.cli(self.home, "timeline", "trim", slug, "1", *args, check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("must be greater", r.stderr)
        self.assertEqual(len(self.tl(slug)["clips"]), 1)
        self.assertEqual(len(H.events(self.home)), n)
        H.cli(self.home, "timeline", "clear", slug)
        # audio-only media cannot sit on a video timeline: refused with a reason
        r = H.cli(self.home, "timeline", "add", slug, memo, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("audio-only", r.stderr)
        self.assertEqual(self.tl(slug)["clips"], [])
        # set with clips that would be dropped: refused (exit 2), every reason printed
        bad = {"aspect": "9:16", "clips": [
            {"media": "deadbeef0000", "in": 0, "out": 3},
            {"media": pt, "in": 10, "out": 5},
            {"media": pt, "in": 1, "out": 2, "label": "keep"}]}
        r = H.cli(self.home, "timeline", "set", slug, "-", input=json.dumps(bad), check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no media deadbeef0000", r.stderr)
        self.assertIn("zero length", r.stderr)
        self.assertEqual(self.tl(slug)["clips"], [])
        # set: bad aspect coerced, missing id assigned
        ok = {"aspect": "21:9", "captions": 0, "clips": [{"media": pt, "in": 1, "out": 2, "label": "keep"}]}
        H.cli(self.home, "timeline", "set", slug, "-", input=json.dumps(ok))
        t = self.tl(slug)
        self.assertEqual(t["aspect"], "9:16")
        self.assertIs(t["captions"], False)
        self.assertEqual(len(t["clips"]), 1)
        self.assertEqual(t["clips"][0]["label"], "keep")
        self.assertTrue(t["clips"][0]["id"])
        # bad index: nonzero exit, no event
        n = len(H.events(self.home))
        r = H.cli(self.home, "timeline", "trim", slug, "9", "0", "1", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(H.events(self.home)), n)
        r = H.cli(self.home, "timeline", "show", "no-such-project", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_timeline_json_mirrors_log(self):
        slug = self.project("Mirror")
        H.cli(self.home, "timeline", "add", slug, self.mid("site-walk.mov"), "1", "2")
        f = json.loads((self.home / "projects" / slug / "timeline.json").read_text())
        self.assertEqual(f, self.tl(slug))

    # ---- render ----
    def _render(self, slug, aspect):
        pt, sr, sw = (self.mid(n) for n in ("phone-talking.mov", "screen-recording.mp4", "site-walk.mov"))
        spec = [(pt, 2, 6.5), (sr, 3, 7), (sw, 10, 14.5), (pt, 20, 23), (sw, 40, 43)]
        for m, a, b in spec:
            H.cli(self.home, "timeline", "add", slug, m, a, b, "--aspect", aspect)
        out = H.cli(self.home, "render", slug, timeout=900).stdout.strip().splitlines()[-1]
        path = Path(out)
        self.assertTrue(path.is_file(), out)
        want = sum(b - a for _, a, b in spec)
        info = H.ffprobe(path)
        self.assertAlmostEqual(info["duration"], want, delta=0.12 * len(spec) + 0.2)
        self.assertEqual((info["width"], info["height"]), self.S.ASPECTS[aspect])
        self.assertTrue(info["has_audio"], "export must always carry an audio stream")
        self.assertEqual(info["vcodec"], "h264")
        # A/V stay in step across the concat (silent clip got a silence track)
        import subprocess
        s = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                       str(path)], capture_output=True, text=True).stdout)["streams"]
        durs = {x["codec_type"]: float(x.get("duration", 0)) for x in s}
        self.assertAlmostEqual(durs["audio"], durs["video"], delta=0.35)
        ev = [e for e in H.events(self.home) if e["type"] == "render.done"
              and e["data"]["project"] == slug][-1]["data"]
        self.assertEqual(ev["aspect"], aspect)
        self.assertEqual(ev["clips"], len(spec))
        self.assertEqual(self.home / ev["file"], path)
        exp = self.st()["projects"][slug]["exports"][-1]
        self.assertEqual(exp["file"], ev["file"])
        return path

    def test_render_9x16(self):
        self._render(self.project("Render Vertical"), "9:16")

    def test_render_16x9(self):
        self._render(self.project("Render Wide"), "16:9")

    def test_render_empty_fails(self):
        slug = self.project("Empty")
        r = H.cli(self.home, "render", slug, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any(e["type"] == "render.done" and e["data"]["project"] == slug
                             for e in H.events(self.home)))

    # ---- log is the truth ----
    def test_log_replays_to_same_state(self):
        slug = self.project("Replay")
        H.cli(self.home, "timeline", "add", slug, self.mid("site-walk.mov"), "1", "4")
        before = self.st()
        for e in H.events(self.home):
            self.assertEqual(set(e) >= {"ts", "type", "media", "data"}, True, e)
        replay = H.new_home("replay")
        shutil.copy(self.home / "events.jsonl", replay / "events.jsonl")
        self.S.HOME = replay
        after = self.S.state()
        self.S.HOME = self.home
        # the same state, with sources resolved inside whichever library folds the log
        self.assertEqual(json.dumps(before, sort_keys=True).replace(str(self.home), "HOME"),
                         json.dumps(after, sort_keys=True).replace(str(replay), "HOME"))
        # derived files are not authoritative: deleting them changes nothing
        (self.home / "projects" / slug / "timeline.json").unlink()
        self.assertEqual(json.dumps(self.st(), sort_keys=True), json.dumps(before, sort_keys=True))
        # and a CLI in the replay home sees the same timeline
        show = H.cli(replay, "timeline", "show", slug).stdout
        self.assertIn(self.mid("site-walk.mov"), show)

    # ---- operator <-> agent bridge ----
    def test_focus(self):
        home = H.new_home("focus")
        self.assertIn("{}", H.cli(home, "focus").stdout)
        sel = {"project": "shoot", "selection": {"clip": 2}, "playhead": 12.5}
        (home / "ui-focus.json").write_text(json.dumps(sel))
        self.assertEqual(json.loads(H.cli(home, "focus").stdout), sel)

    def test_notes(self):
        self.S.emit("note", None, text="tighten the hook on clip 2", project="shoot",
                    focus={"clip": 2}, actor="operator")
        self.S.emit("note", None, text="other project note", project="elsewhere", actor="operator")
        out = H.cli(self.home, "notes").stdout
        self.assertIn("tighten the hook on clip 2", out)
        self.assertIn("other project note", out)
        out = H.cli(self.home, "notes", "-p", "shoot").stdout
        self.assertIn("tighten the hook", out)
        self.assertNotIn("other project note", out)

    def test_timeline_clip_ids_and_rev_guard(self):
        slug = self.project("Ids")
        pt = self.mid("phone-talking.mov")
        H.cli(self.home, "timeline", "add", slug, pt, "1", "5")
        H.cli(self.home, "timeline", "add", slug, pt, "6", "9")
        show = H.cli(self.home, "timeline", "show", "--json", slug).stdout
        tl = json.loads(show)
        rev, second = tl["rev"], tl["clips"][1]["id"]
        self.assertIn(f"rev {rev}", H.cli(self.home, "timeline", "show", slug).stdout)
        # by id prefix, even after the operator reorders
        H.cli(self.home, "timeline", "move", slug, second[:5], "1")
        H.cli(self.home, "timeline", "trim", slug, second[:5], "6.5", "8.5")
        c = self.tl(slug)["clips"][0]
        self.assertEqual((c["id"], c["in"], c["out"]), (second, 6.5, 8.5))
        # --base with a stale rev: exit 3, nothing saved
        n = len(H.events(self.home))
        r = H.cli(self.home, "timeline", "trim", slug, second, "6", "8", "--base", str(rev), check=False)
        self.assertEqual(r.returncode, 3)
        self.assertIn("changed since", r.stderr)
        self.assertEqual(len(H.events(self.home)), n)
        cur = json.loads(H.cli(self.home, "timeline", "show", "--json", slug).stdout)["rev"]
        H.cli(self.home, "timeline", "label", slug, second, "privacy", "bit", "--base", str(cur))
        self.assertEqual(self.tl(slug)["clips"][0]["label"], "privacy bit")
        r = H.cli(self.home, "timeline", "rm", slug, "zzzz", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_read_verbs_and_bare_ai_step_refused(self):
        pt = self.mid("phone-talking.mov")
        d = self.S.mdir(pt)
        (d / "moments.json").write_text(json.dumps([
            {"n": 1, "start": 2.0, "end": 12.5, "title": "Two hours", "hook": "two hours every night"}]))
        (d / "drafts.json").write_text(json.dumps({"posts": [{"n": 1, "short": "orig", "x": "x orig"}],
                                                   "newsletter": "nl"}))
        out = H.cli(self.home, "media", "show", pt[:6]).stdout
        self.assertIn("Two hours", out)
        self.assertIn("0:02.0", out)
        w = H.cli(self.home, "words", pt, "--grep", "hours").stdout
        self.assertRegex(w, r"\d+\.\d\d +\d+\.\d\d +hours")
        self.S.emit("draft.edited", pt, key="2.00-12.50", field="x", text="my own x", actor="operator")
        posts = H.cli(self.home, "posts", pt).stdout
        self.assertIn("my own x", posts)
        self.assertIn("operator rewrote: x", posts)
        self.assertIn("my own x", json.dumps(json.loads(H.cli(self.home, "posts", pt, "--json").stdout)))
        # a bare AI step must not call the agent for the whole library
        for step in ("moments", "draft", "cut"):
            r = H.cli(self.home, step, check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("--all", r.stderr)

    def test_notes_thread(self):
        home = H.new_home("notes")
        S = H.import_studio(home)
        S.HOME = home
        S.emit("note", None, text="start on the word privacy", project="p", actor="operator", id="n1",
               focus={"selection": {"index": 2, "id": "1165db27", "in": 18.0, "out": 31.5,
                                    "label": "Never leaves my phone"}, "playhead_hms": "0:31.9"})
        out = H.cli(home, "notes").stdout
        self.assertIn("start on the word privacy", out)
        self.assertIn("clip 3 1165db27", out)        # index 2 is the UI's "Clip 3"
        self.assertIn("playhead 0:31.9", out)
        self.assertIn("(no unread notes)", H.cli(home, "notes", "--unread").stdout)
        self.assertEqual(json.loads(H.cli(home, "notes", "--json").stdout)[0]["focus"]["selection"]["id"],
                         "1165db27")
        # reading as the agent marks it seen; say --re replies in the thread
        H.cli(home, "notes", as_agent=True)
        H.cli(home, "say", "on it", "--re", "n1", as_agent=True)
        S.HOME = home
        notes = S.state()["notes"]
        self.assertTrue(notes[0].get("acked"))
        self.assertEqual((notes[1]["actor"], notes[1]["re"], notes[1]["project"]), ("agent", "n1", "p"))
        self.assertIn("agent: on it", H.cli(home, "notes").stdout)
        # a library from before says "claude" for the agent: still the agent
        S.emit("note", None, text="older reply", project="p", actor="claude", id="n0")
        self.assertIn("agent: older reply", H.cli(home, "notes").stdout)
        self.S.HOME = self.home


    # ---- regressions: data safety (S1, S2, S3, S5, S7) ----
    def test_ingest_never_moves_what_it_cannot_use(self):
        """S2: a file still being copied in, and an unreadable one, stay in inbox/ with a
        reason; the batch carries on; nothing lands in media/ under a wrong id."""
        import subprocess
        home = H.new_home("ingest-safe")
        inbox = home / "inbox"
        walk = (self.fx / "site-walk.mov").read_bytes()
        (inbox / "a-walk.mov").write_bytes(walk[: len(walk) // 3])          # AirDrop 1/3 in: mtime now
        settled(shutil.copy(self.fx / "voice-memo.m4a", inbox / "b-memo.m4a"))
        (inbox / "c-broken.mov").write_bytes(b"\x00\x00\x00\x18ftypqt  " + os.urandom(4000))
        settled(inbox / "c-broken.mov")
        # cut-off copy whose index is at the front: probes as complete, but its end is missing
        fs = home / "fs.mov"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(self.fx / "phone-talking.mov"),
                        "-map", "0", "-c", "copy", "-movflags", "+faststart", str(fs)], check=True)
        data = fs.read_bytes()
        (inbox / "d-cut.mov").write_bytes(data[: len(data) * 6 // 10]); settled(inbox / "d-cut.mov")
        fs.unlink()
        r = H.cli(home, "ingest", check=False)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("skip a-walk.mov (still copying", r.stdout)
        self.assertIn("skip c-broken.mov (can't read it", r.stdout)
        self.assertIn("skip d-cut.mov (the end of the file is missing", r.stdout)
        self.assertRegex(r.stdout, r"new  b-memo\.m4a → [0-9a-f]{12}")
        self.assertEqual(sorted(p.name for p in inbox.iterdir()), ["a-walk.mov", "c-broken.mov", "d-cut.mov"])
        ing = [e for e in H.events(home) if e["type"] == "ingested"]
        self.assertEqual([e["data"]["name"] for e in ing], ["b-memo.m4a"])
        self.assertEqual(sorted(p.name for p in (home / "media").iterdir()), [ing[0]["media"]],
                         "a skipped file left a media dir behind")
        # the copy finishes: the next run takes it, under the id of its full contents
        (inbox / "a-walk.mov").write_bytes(walk); settled(inbox / "a-walk.mov")
        r = H.cli(home, "ingest", check=False)
        import hashlib
        full = hashlib.sha256(walk).hexdigest()[:12]
        self.assertIn(f"new  a-walk.mov → {full}", r.stdout)
        self.assertFalse((inbox / "a-walk.mov").exists())

    def test_ingest_waits_for_a_file_it_was_given_that_was_just_written(self):
        """A render or copy finished a moment ago is waited for, not refused. It used to say
        `skip NAME (still copying...)` then `nothing new`, which read as "a duplicate"."""
        home = H.new_home("ingest-fresh")
        f = H.new_home("ingest-fresh-src") / "fresh render.m4a"
        shutil.copy(self.fx / "voice-memo.m4a", f)          # mtime: now
        r = H.cli(home, "ingest", "-p", "a", f, env=H.env_for(home, STUDIO_SETTLE_SECS="1.5"))
        self.assertIn("wait fresh render.m4a", r.stdout)
        self.assertRegex(r.stdout, r"new  fresh render\.m4a → [0-9a-f]{12}")
        self.assertIn("1 new, 0 already in the library", r.stdout)
        self.assertNotIn("nothing new", r.stdout)

    def test_ingest_takes_a_file_from_the_future_and_gives_up_on_one_that_keeps_growing(self):
        import subprocess, sys as _sys, threading
        home = H.new_home("ingest-clock")
        src = H.new_home("ingest-clock-src")
        f = src / "phone clock.m4a"
        shutil.copy(self.fx / "voice-memo.m4a", f)
        later = time.time() + 3600                       # a device clock an hour ahead
        os.utime(f, (later, later))
        t0 = time.time()
        r = H.cli(home, "ingest", "-p", "a", f)
        self.assertLess(time.time() - t0, 20, "a finished file is not waited for")
        self.assertNotIn("wait ", r.stdout)
        self.assertIn("1 new, 0 already in the library", r.stdout)
        # a recording still running: waited for up to the cap, then refused loudly
        g = src / "still recording.m4a"
        g.write_bytes((self.fx / "voice-memo.m4a").read_bytes())
        stop = threading.Event()
        def grow():
            while not stop.is_set():
                with open(g, "ab") as fh:
                    fh.write(b"\0" * 64)
                time.sleep(0.2)
        th = threading.Thread(target=grow, daemon=True)
        th.start()
        try:
            t0 = time.time()
            r = H.cli(home, "ingest", "-p", "a", g, check=False,
                      env=H.env_for(home, STUDIO_SETTLE_SECS="1", STUDIO_CLI_SETTLE_WAIT_SECS="2"))
        finally:
            stop.set(); th.join()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertLess(time.time() - t0, 20)
        self.assertIn("skip still recording.m4a (it was still being written after 2s", r.stdout)
        self.assertIn("0 new, 0 already in the library, 1 skipped", r.stdout)

    def test_ingest_never_takes_media_out_of_another_project(self):
        """The same bytes are the same media; ingesting them for another project says where they
        are and leaves them there. Only unsorted footage (the inbox) is sorted on ingest."""
        import hashlib
        home = H.new_home("ingest-dedup")
        src = H.new_home("ingest-dedup-src")
        a = settled(shutil.copy(self.fx / "voice-memo.m4a", src / "card.m4a"))
        b = settled(shutil.copy(self.fx / "voice-memo.m4a", src / "card copy.m4a"))
        mid = hashlib.sha256(a.read_bytes()).hexdigest()[:12]
        H.cli(home, "ingest", "-p", "one", a)
        r = H.cli(home, "ingest", "-p", "two", b)
        self.assertIn(f"have card copy.m4a → {mid}  (byte for byte the same as 'card.m4a' in project one", r.stdout)
        self.assertIn("0 new, 1 already in the library", r.stdout)
        self.assertFalse([e for e in H.events(home) if e["type"] == "media.moved"], "taken from project one")
        S = H.import_studio(home)
        try:
            self.assertEqual(S.state()["media"][mid]["project"], "one")
        finally:
            S.HOME = self.home
        # different bytes are different media, whatever they're called
        c = src / "card.m4a"
        c.write_bytes(c.read_bytes() + b"\0"); settled(c)
        r = H.cli(home, "ingest", "-p", "two", c)
        self.assertNotIn(mid, r.stdout)
        self.assertIn("1 new, 0 already in the library", r.stdout)
        # unsorted footage is sorted into the project it's ingested for; media move does it on purpose
        inb = settled(shutil.copy(self.fx / "phone-talking.mov", src / "talk.mov"))
        H.cli(home, "ingest", inb)
        r = H.cli(home, "ingest", "-p", "three", inb)
        tid = hashlib.sha256(inb.read_bytes()).hexdigest()[:12]
        self.assertIn(f"(moved from inbox to three)", r.stdout)
        r = H.cli(home, "media", "move", mid, "two")
        self.assertIn(f"{mid}  card.m4a  one → two", r.stdout)
        moved = [(e["media"], e["data"]["project"]) for e in H.events(home) if e["type"] == "media.moved"]
        self.assertEqual(moved, [(tid, "three"), (mid, "two")])

    def test_ingest_exits_1_when_a_file_it_was_given_is_skipped(self):
        home = H.new_home("ingest-named")
        bad = H.new_home("ingest-named-src") / "broken.mov"
        bad.write_bytes(b"\x00\x00\x00\x18ftypqt  " + os.urandom(4000)); settled(bad)
        r = H.cli(home, "ingest", bad, check=False)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("skip broken.mov (can't read it", r.stdout)
        self.assertIn("0 new, 0 already in the library, 1 skipped", r.stdout)

    def test_new_project_starts_at_rev_0(self):
        """S3: a never-saved project has rev 0 (so the UI's first save carries ?base=0)."""
        slug = self.project("Never Saved")
        self.assertEqual(self.st()["projects"][slug]["rev"], 0)
        self.assertEqual(json.loads(H.cli(self.home, "timeline", "show", "--json", slug).stdout)["rev"], 0)
        H.cli(self.home, "timeline", "add", slug, self.mid("site-walk.mov"), "0", "2", as_agent=True)
        r = H.cli(self.home, "timeline", "clear", slug, "--base", "0", check=False)
        self.assertEqual(r.returncode, 3, "an edit based on rev 0 must not overwrite the agent's save")
        self.assertEqual(len(self.tl(slug)["clips"]), 1)

    def test_emit_has_id_and_device(self):
        """docs/events.md: every new event carries id (UUIDv7, time-ordered) and device
        (STUDIO_DEVICE, else the short hostname); the fold ignores both."""
        import re, socket, uuid
        home = H.new_home("envelope")
        H.cli(home, "project", "new", "One", env=H.env_for(home, STUDIO_DEVICE="test-phone"))
        H.cli(home, "say", "hello", "-p", "one")
        H.cli(home, "say", "again", "-p", "one")
        e1, e2, e3 = H.events(home)
        self.assertEqual(list(e1), ["id", "device", "ts", "type", "media", "data"])
        self.assertEqual(e1["device"], "test-phone")
        self.assertEqual(e3["device"], socket.gethostname().split(".")[0])
        for e in (e1, e2, e3):
            u = uuid.UUID(e["id"])
            self.assertEqual(u.version, 7, e["id"])
            self.assertRegex(e["id"], re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab]"))
        self.assertEqual(len({e1["id"], e2["id"], e3["id"]}), 3)
        self.assertLessEqual(e1["id"][:13], e3["id"][:13], "UUIDv7 ids sort by time")
        S = H.import_studio(home)
        self.assertNotIn("device", json.dumps(S.state()["projects"]["one"]))
        self.S.HOME = self.home

    def test_event_log_survives_a_torn_line(self):
        """S5: a writer that died mid-line must not swallow the next event or crash readers."""
        home = H.new_home("torn")
        H.cli(home, "project", "new", "Before")
        with open(home / "events.jsonl", "ab") as f:
            f.write(b'{"ts": "2026-11-07T10:00:00-08:00", "type": "note", "da')     # no newline
        H.cli(home, "project", "new", "After")
        raw = (home / "events.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertIn(b'"da\n{', raw, "the next event must start on its own line")
        r = H.cli(home, "project", "ls")
        self.assertIn("after", r.stdout, "the event after the torn line was lost")
        self.assertIn("before", r.stdout)
        self.assertIn("warning: skipping an unreadable line", r.stderr)
        S = H.import_studio(home)
        self.assertIn("after", S.state()["projects"])
        self.S.HOME = self.home

    def test_failed_render_leaves_no_work_dir(self):
        """S7: render/<stamp>/ is removed even when ffmpeg fails part way."""
        home = H.new_home("render-fail")
        S = H.import_studio(home)
        slug = S.project_new("p")
        S.emit("ingested", "deadbeef0001", name="gone.mov", sha256="deadbeef0001" * 5,
               path=str(home / "nope.mov"), project=slug, duration=10.0, has_audio=False,
               has_video=True, width=1080, height=1920)
        S.timeline_save(slug, {"aspect": "9:16", "captions": False,
                              "clips": [{"media": "deadbeef0001", "in": 0, "out": 3}]})
        with self.assertRaises(RuntimeError) as cm:
            S.render(slug)
        self.assertIn("ffmpeg", str(cm.exception), "the render should have got as far as ffmpeg")
        self.assertEqual(list((home / "projects" / slug / "render").iterdir()), [])
        self.S.HOME = self.home

    def test_whisper_json_removed_after_parse(self):
        """S7: whisper's raw output is deleted once transcript.json is written."""
        for name in ("phone-talking.mov", "site-walk.mov"):
            d = self.home / "media" / self.mid(name)
            self.assertTrue((d / "transcript.json").exists())
            self.assertFalse((d / "whisper.json").exists(), f"{name}: whisper.json left behind")

    def test_recover_returns_half_ingested_footage(self):
        """S7: a media dir left by an ingest that died (marker, no ingested event, no live
        process) is cleaned up and a moved source goes back to inbox/; anything else stays."""
        home = H.new_home("recover")
        S = H.import_studio(home)
        dead = home / "media" / "aaaaaaaaaaaa"; dead.mkdir()
        (dead / "source.mov").write_bytes(b"footage")
        (dead / ".ingesting").write_text(json.dumps({"pid": 999999, "name": "walk 1.mov", "moved": True}))
        copy = home / "media" / "bbbbbbbbbbbb"; copy.mkdir()
        (copy / "source.mp4").write_bytes(b"a copy")
        (copy / ".ingesting").write_text(json.dumps({"pid": 999999, "name": "x.mp4", "moved": False}))
        legacy = home / "media" / "cccccccccccc"; legacy.mkdir()
        (legacy / "source.mov").write_bytes(b"no marker: not ours to judge")
        live = home / "media" / "dddddddddddd"; live.mkdir()
        (live / ".ingesting").write_text(json.dumps({"pid": os.getppid(), "moved": True}))
        known = home / "media" / "eeeeeeeeeeee"; known.mkdir()
        (known / ".ingesting").write_text(json.dumps({"pid": 999999, "moved": True}))
        S.emit("ingested", "eeeeeeeeeeee", name="k.mov", sha256="e" * 64, path=str(known / "source.mov"),
               project="inbox", duration=1.0, has_audio=True, has_video=True, width=2, height=2)
        fixed = S.recover(log=lambda m: None)
        self.assertEqual(sorted(fixed), ["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
        self.assertEqual((home / "inbox" / "walk 1.mov").read_bytes(), b"footage")
        for d in (legacy, live, known):
            self.assertTrue(d.exists(), f"{d.name} must not be touched")
        self.S.HOME = self.home

    # ---- regressions: session quality ----
    def test_brand_vocabulary_and_fix_words(self):
        """Q1a/b: the brand file's vocabulary and mishearing fixes are parsed and applied."""
        vocab, fixes = self.S.brand_words("jefe")
        for w in ("Jefe", "aerate", "overseed", "zone", "sprinkler head", "letterhead", "estimate",
                  "invoice", "work order"):
            self.assertIn(w, vocab)
        for pair in (("Hefe", "Jefe"), ("Jeef", "Jefe"), ("Heffe", "Jefe"), ("Jeff A", "Jefe")):
            self.assertIn(pair, fixes)
        segs = [{"start": 0, "end": 5, "text": "I built hefe. Jeff a is great, and Jeef's fast."}]
        words = [{"w": w, "start": i * .4, "end": i * .4 + .3}
                 for i, w in enumerate("I built hefe. Jeff a is great, and Jeef's fast.".split())]
        n = self.S.apply_fixes(segs, words, fixes + self.S.case_fixes(vocab))
        self.assertEqual(n, 3)
        self.assertEqual(segs[0]["text"], "I built Jefe. Jefe is great, and Jefe's fast.")
        self.assertEqual([w["w"] for w in words][:5], ["I", "built", "Jefe.", "Jefe", "is"])
        self.assertEqual(words[3]["end"], 1.2 + .4 + .3, "a merged phrase spans both words")
        # the real transcription ran with the vocabulary: the brand name is spelt right
        tx = json.loads((self.home / "media" / self.mid("phone-talking.mov") / "transcript.json").read_text())
        self.assertIn("Jefe", " ".join(w["w"] for w in tx["words"]))
        self.assertNotIn("jefe", " ".join(w["w"] for w in tx["words"]))

    def test_transcribe_passes_vocabulary_prompt(self):
        """Q1b/e: whisper-cli gets --prompt with the vocabulary and --no-speech-thold."""
        home = H.new_home("prompt")
        fake = home / "bin"; fake.mkdir()
        log = home / "argv.json"
        (fake / "whisper-cli").write_text(f"""#!/usr/bin/env python3
import json, sys
json.dump(sys.argv, open({str(log)!r}, "w"))
of = sys.argv[sys.argv.index("-of") + 1]
json.dump({{"result": {{"language": "en"}}, "transcription": [
  {{"text": " Hefe walks the yard.", "offsets": {{"from": 0, "to": 1500}}, "tokens": [
    {{"text": " Hefe", "offsets": {{"from": 0, "to": 0}}}}, {{"text": " walks", "offsets": {{"from": 400, "to": 700}}}},
    {{"text": " the", "offsets": {{"from": 700, "to": 800}}}}, {{"text": " yard.", "offsets": {{"from": 800, "to": 1400}}}}]}},
  {{"text": " Thank you.", "offsets": {{"from": 6000, "to": 7000}}, "tokens": [
    {{"text": " Thank", "offsets": {{"from": 6000, "to": 6300}}}}, {{"text": " you.", "offsets": {{"from": 6300, "to": 7000}}}}]}}]}},
  open(of + ".json", "w"))
""")
        (fake / "whisper-cli").chmod(0o755)
        # speech everywhere (one region from 0): whisper's --vad timeline is then the file's own
        (fake / "whisper-vad-speech-segments").write_text(
            "#!/bin/sh\necho 'Detected 1 speech segments:'\necho 'Speech segment 0: start = 0.00, end = 800.00'\n")
        (fake / "whisper-vad-speech-segments").chmod(0o755)
        S = H.import_studio(home)
        d = home / "media" / "f00d00000001"; d.mkdir()
        import wave, array
        with wave.open(str(d / "audio.wav"), "wb") as w:      # 1.5 s of tone, then silence
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            import math
            w.writeframes(array.array("h", [int(8000 * math.sin(i / 5)) if i < 24000 else 0
                                            for i in range(16000 * 8)]).tobytes())
        old_path, old_model = os.environ["PATH"], S.MODEL
        os.environ["PATH"] = f"{fake}:{old_path}"
        S.MODEL = d / "audio.wav"     # any existing file: the fake ignores it
        try:
            out = S.transcribe({"id": "f00d00000001", "has_audio": True, "brand": "jefe", "path": "x"})
        finally:
            os.environ["PATH"], S.MODEL = old_path, old_model
            self.S.HOME = self.home
        argv = json.loads(log.read_text())
        self.assertIn("--prompt", argv)
        self.assertIn("Jefe", argv[argv.index("--prompt") + 1])
        self.assertIn("aerate", argv[argv.index("--prompt") + 1])
        self.assertIn("-nth", argv)
        # the hallucination gate: whisper hears only Silero's speech, no non-speech tokens
        self.assertIn("--vad", argv)
        self.assertTrue(argv[argv.index("-vm") + 1].endswith(".bin"), argv)
        self.assertIn("-sns", argv)
        self.assertEqual(argv[argv.index("-mc") + 1], "0")
        self.assertIn("--carry-initial-prompt", argv, "-mc 0 alone would drop the vocabulary")
        tx = json.loads((d / "transcript.json").read_text())
        self.assertEqual([w["w"] for w in tx["words"]], ["Jefe", "walks", "the", "yard."])
        self.assertEqual([s["text"] for s in tx["segments"]], ["Jefe walks the yard."],
                         "the lone 'Thank you.' in the silent tail should be dropped")
        self.assertGreater(tx["words"][0]["end"], tx["words"][0]["start"], "zero-length first word not repaired")
        self.assertEqual(out["dropped"], ["Thank you."])
        self.assertFalse((d / "whisper.json").exists())
        self.assertIn("Jefe", (d / "transcript.txt").read_text())

    def test_words_fix_cli(self):
        """Q1c: `studio words ID --fix WRONG RIGHT [--all]` rewrites transcript.json/.txt and
        emits transcript.edited (with a new receipt, so the UI refetches)."""
        home = H.new_home("wordfix")
        S = H.import_studio(home)
        for mid in ("abc000000001", "abc000000002"):
            S.emit("ingested", mid, name=f"{mid}.mov", sha256=mid * 5, path="x", project="inbox",
                   duration=5.0, has_audio=True, has_video=True, width=1080, height=1920)
            S.emit("step.transcribe", mid, words=4, sha256="old")
            S.mdir(mid).mkdir(parents=True)
            S.write_transcript(S.mdir(mid), "en",
                               [{"start": 0, "end": 2, "text": "Air raid the lawn, Hefe."}],
                               [{"w": "Air", "start": 0, "end": .3}, {"w": "raid", "start": .3, "end": .6},
                                {"w": "the", "start": .6, "end": .8}, {"w": "lawn,", "start": .8, "end": 1.1},
                                {"w": "Hefe.", "start": 1.2, "end": 1.6}])
        r = H.cli(home, "words", "abc000000001", "--fix", "Hefe", "Jefe", as_agent=True)
        self.assertIn("1 × 'Hefe' → 'Jefe'", r.stdout)
        t1 = json.loads((home / "media" / "abc000000001" / "transcript.json").read_text())
        self.assertEqual(t1["words"][-1]["w"], "Jefe.")
        self.assertEqual(t1["segments"][0]["text"], "Air raid the lawn, Jefe.")
        self.assertIn("Jefe.", (home / "media" / "abc000000001" / "transcript.txt").read_text())
        t2 = json.loads((home / "media" / "abc000000002" / "transcript.json").read_text())
        self.assertEqual(t2["words"][-1]["w"], "Hefe.", "without --all only that file changes")
        ev = [e for e in H.events(home) if e["type"] == "transcript.edited"]
        self.assertEqual((ev[-1]["media"], ev[-1]["data"]["actor"], ev[-1]["data"]["count"]),
                         ("abc000000001", "agent", 1))
        S.HOME = home
        self.assertEqual(S.state()["media"]["abc000000001"]["steps"]["transcribe"]["sha256"],
                         ev[-1]["data"]["sha256"], "the receipt must change so clients refetch")
        r = H.cli(home, "words", "--fix", "air raid", "aerate", "--all")
        self.assertIn("fixed 2 words", r.stdout)
        for mid in ("abc000000001", "abc000000002"):
            t = json.loads((home / "media" / mid / "transcript.json").read_text())
            self.assertEqual(t["words"][0]["w"], "aerate")
            self.assertTrue(t["segments"][0]["text"].startswith("aerate the lawn"))
        r = H.cli(home, "words", "abc000000002", "--fix", "zebra", "x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.S.HOME = self.home

    def test_caption_chunks_break_on_sentences(self):
        """Q2: chunks close after . ? ! , and at segment boundaries, max 3 words; the first
        word of a clip that whisper gave no length is still captioned from 0.0."""
        W = lambda *ws: [{"w": w, "start": a, "end": b} for w, a, b in ws]
        words = W(("Front", 10.0, 10.0), ("beds", 10.3, 10.6), ("need", 10.6, 10.9), ("mulch.", 10.9, 11.4),
                  ("Call", 11.6, 11.8), ("it", 11.8, 11.9), ("three", 11.9, 12.2), ("yards,", 12.2, 12.6),
                  ("okay", 12.7, 13.0), ("Zone", 14.0, 14.2), ("two", 14.2, 14.5))
        segs = [{"start": 10.0, "end": 13.1, "text": "..."}, {"start": 14.0, "end": 15.0, "text": "..."}]
        ch = self.S.caption_chunks(words, segs, 10.0, 15.0)
        self.assertEqual([c["text"] for c in ch],
                         ["Front beds need", "mulch.", "Call it three", "yards,", "okay", "Zone two"])
        self.assertEqual(ch[0]["a"], 0.0, "first caption must start with the clip, not a word late")
        self.assertEqual(ch[1]["b"], 1.6)       # until the next chunk starts
        ass = self.home / "cap-test.ass"
        self.S.captions(words, 10.0, 15.0, ass, segments=segs)
        self.assertIn("Dialogue: 0,0:00:00.00,", ass.read_text())
        ass.unlink()

    def test_timeline_moment_field(self):
        """Q3: a clip added from a moment remembers it (n), through trim and split."""
        slug = self.project("Moment Field")
        pt = self.mid("phone-talking.mov")
        H.fake_moments(self.home, pt, self.dur("phone-talking.mov"), n=3)
        self.S.HOME = self.home
        H.cli(self.home, "timeline", "add", slug, f"{pt}@2")
        self.assertEqual(self.tl(slug)["clips"][0]["moment"], 2)
        H.cli(self.home, "timeline", "trim", slug, "1", "16", "18")
        H.cli(self.home, "timeline", "split", slug, "1", "1")
        self.assertEqual([c.get("moment") for c in self.tl(slug)["clips"]], [2, 2])
        self.assertIn("(moment 2)", H.cli(self.home, "timeline", "show", slug).stdout)
        H.cli(self.home, "timeline", "add", slug, pt, "1", "3")
        self.assertNotIn("moment", self.tl(slug)["clips"][2])

    def test_timeline_fit_and_render(self):
        """Q4: `timeline fit` sets fill/fit per clip; a screen recording defaults to fit; the
        export shows the whole frame (its dark top bar survives) only in fit mode."""
        import subprocess
        slug = self.project("Fit Mode")
        sr, pt = self.mid("screen-recording.mp4"), self.mid("phone-talking.mov")
        H.cli(self.home, "timeline", "add", slug, sr, "1", "3")
        H.cli(self.home, "timeline", "add", slug, pt, "1", "3")
        js = json.loads(H.cli(self.home, "timeline", "show", "--json", slug).stdout)
        self.assertEqual([(c["fit_effective"], "fit" in c) for c in js["clips"]], [("fit", False), ("fill", False)])
        self.assertIn("[show everything]", H.cli(self.home, "timeline", "show", slug).stdout)

        def top_band(path):
            raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "0.5", "-i", str(path),
                                  "-frames:v", "1", "-vf", "crop=200:80:440:70,format=gray",
                                  "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
            return sum(raw) / len(raw)
        H.cli(self.home, "timeline", "rm", slug, "2")
        fit_out = Path(H.cli(self.home, "render", slug, timeout=600).stdout.strip().splitlines()[-1])
        H.cli(self.home, "timeline", "fit", slug, "1", "fill")
        self.assertEqual(self.tl(slug)["clips"][0]["fit"], "fill")
        fill_out = Path(H.cli(self.home, "render", slug, timeout=600).stdout.strip().splitlines()[-1])
        fit_y, fill_y = top_band(fit_out), top_band(fill_out)
        self.assertLess(fit_y, 80, f"fit: the app's dark top bar should be visible (luma {fit_y:.0f})")
        # The blurred backdrop shows at the left edge (the portrait source is narrower than
        # 9:16). boxblur on nv12 once painted it solid green (zeroed chroma); it must look
        # like a blur of the footage, not a green bar.
        # (The broken render measured a green excess of ~53 over a band half covered by the bar;
        # a real blur of footage sits near or below 0.)
        for x in (0, 1000):
            raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "0.5", "-i", str(fit_out),
                                  "-frames:v", "1", "-vf", f"crop=80:1700:{x}:110,format=rgb24",
                                  "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
            r_, g_, b_ = (sum(raw[i::3]) / (len(raw) // 3) for i in range(3))
            self.assertLess(g_ - max(r_, b_), 25,
                            f"fit backdrop at x={x} is a green bar (rgb {r_:.0f},{g_:.0f},{b_:.0f})")
        self.assertGreater(fill_y, 150, f"fill crops the top bar away (luma {fill_y:.0f})")
        self.assertEqual((H.ffprobe(fit_out)["width"], H.ffprobe(fit_out)["height"]), (1080, 1920))
        H.cli(self.home, "timeline", "fit", slug, "1", "auto")
        self.assertNotIn("fit", self.tl(slug)["clips"][0])
        r = H.cli(self.home, "timeline", "fit", slug, "1", "zoom", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_clip_numbers_are_1_based(self):
        """Q5: human output and arguments use 1-based clip numbers, like the UI; --json has
        both index (0-based) and number (1-based)."""
        slug = self.project("Numbers")
        sw = self.mid("site-walk.mov")
        for a, b in ((0, 2), (2, 4), (4, 6)):
            H.cli(self.home, "timeline", "add", slug, sw, a, b)
        ids = [c["id"] for c in self.tl(slug)["clips"]]
        show = H.cli(self.home, "timeline", "show", slug).stdout.splitlines()
        rows = [l.split() for l in show if any(i in l for i in ids)]
        self.assertEqual([(r[0], r[1]) for r in rows], [("1", ids[0]), ("2", ids[1]), ("3", ids[2])])
        js = json.loads(H.cli(self.home, "timeline", "show", "--json", slug).stdout)
        self.assertEqual([(c["index"], c["number"]) for c in js["clips"]], [(0, 1), (1, 2), (2, 3)])
        H.cli(self.home, "timeline", "label", slug, "2", "middle")
        self.assertEqual(self.tl(slug)["clips"][1]["label"], "middle")
        H.cli(self.home, "timeline", "label", slug, "-1", "last")
        self.assertEqual(self.tl(slug)["clips"][2]["label"], "last")
        r = H.cli(self.home, "timeline", "rm", slug, "0", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("start at 1", r.stderr)
        r = H.cli(self.home, "timeline", "rm", slug, "4", check=False)
        self.assertIn("no clip 4", r.stderr)
        H.cli(self.home, "timeline", "add", slug, sw, "8", "9", "--at", "1")
        self.assertEqual(self.tl(slug)["clips"][0]["in"], 8)
        # focus --brief and notes speak the UI's numbers
        home = H.new_home("focus-brief")
        (home / "ui-focus.json").write_text(json.dumps({
            "project": "p", "rev": 4, "playhead_hms": "0:03.0",
            "selection": {"index": 2, "number": 3, "id": "abcd1234", "in": 1, "out": 2, "label": "x"},
            "under_playhead": {"index": 2, "number": 3, "id": "abcd1234", "source_time": 1.5}}))
        out = H.cli(home, "focus", "--brief").stdout
        self.assertIn("clip 3 abcd1234", out)
        self.assertIn("rev 4", out)


    # ---- share: bundle / import ----
    def _shared_project(self):
        """A project worth sharing: a story that borrows footage filed under "shoot", a
        label, moments, drafts, a note and a finished export. Built once."""
        if getattr(type(self), "_share", None):
            return type(self)._share
        slug = self.project("Share Me")
        ph, sw = self.mid("phone-talking.mov"), self.mid("site-walk.mov")
        ms = H.fake_moments(self.home, ph, self.dur("phone-talking.mov"), n=3, title="Share")
        H.fake_drafts(self.home, ph, ms)
        H.cli(self.home, "timeline", "add", slug, f"{ph}@2")
        H.cli(self.home, "timeline", "add", slug, sw, "1", "4")
        H.cli(self.home, "timeline", "label", slug, "1", "BMW walk")
        H.cli(self.home, "say", "bundle me", "-p", slug)
        ex = self.home / "projects" / slug / "exports" / f"{slug}-20261108-000000-9x16.mp4"
        ex.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.home / "media" / ph / "proxy.mp4", ex)
        self.S.HOME = self.home
        self.S.emit("render.done", None, project=slug, file=str(ex.relative_to(self.home)), duration=10,
                    aspect="9:16", clips=2, sha256=self.S.receipt(ex), actor="cli")
        out = H.new_home("bundle-out") / "Share Me bundle"
        r = H.cli(self.home, "bundle", out, "-p", slug, "--zip")
        type(self)._share = {"slug": slug, "out": out, "stdout": r.stdout, "moments": ms, "ph": ph, "sw": sw}
        return type(self)._share

    @staticmethod
    def _tree(d):
        """{relative path: (size, mtime_ns, sha256)} for every file under d."""
        import hashlib
        return {str(p.relative_to(d)): (p.stat().st_size, p.stat().st_mtime_ns,
                                        hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(Path(d).rglob("*")) if p.is_file()}

    def test_bundle_manifest_csv_and_layout(self):
        import csv, zipfile
        sh = self._shared_project()
        out, slug, ph, sw = sh["out"], sh["slug"], sh["ph"], sh["sw"]
        self.assertIn("total", sh["stdout"])
        man = json.loads((out / "manifest.json").read_text())
        self.assertEqual((man["format"], man["version"]), ("studio-bundle", 1))
        self.assertTrue(man["created"] and man["studio"])
        self.assertEqual({x["id"] for x in man["media"]}, {ph, sw},
                         "a story's borrowed footage (filed under another project) must travel too")
        lib = self.st()["media"]
        for x in man["media"]:
            f = out / x["footage"]
            self.assertTrue(f.is_file(), x["footage"])
            self.assertEqual(self.S.receipt(f), x["sha256"])
            self.assertEqual(x["sha256"], lib[x["id"]]["sha256"])
            self.assertEqual(x["project"], "shoot")
            self.assertTrue(x["footage"].startswith("footage/shoot/"), x["footage"])
            self.assertTrue(x["footage"].endswith(lib[x["id"]]["name"]), "original name kept")
            for k in ("duration", "width", "height", "has_audio"):
                self.assertEqual(x[k], lib[x["id"]][k], k)
        names = {x["id"]: Path(x["footage"]).name for x in man["media"]}
        self.assertEqual(names[ph], "01 - BMW walk - phone-talking.mov")
        self.assertEqual(names[sw], "02 - site-walk.mov")
        pj = {p["slug"]: p for p in man["projects"]}
        self.assertTrue(pj[slug]["shared"])
        self.assertEqual((pj[slug]["name"], pj[slug]["clips"]), ("Share Me", 2))
        self.assertFalse(pj["shoot"]["shared"])
        # library: metadata yes; sources, proxies, clips, jobs no
        L = out / "library"
        for rel in ("transcript.json", "transcript.txt", "moments.json", "drafts.json", "keyframes/index.json"):
            self.assertTrue((L / "media" / ph / rel).is_file(), rel)
        for bad in ("source.*", "proxy.mp4", "clips", "audio.wav"):
            self.assertFalse(list(L.glob(f"media/*/{bad}")), bad)
        for bad in ("uploads", ".server.json", "ui-focus.json", "inbox"):
            self.assertFalse((L / bad).exists(), bad)
        tl = json.loads((L / "projects" / slug / "timeline.json").read_text())
        self.assertEqual(tl, self.tl(slug))
        self.assertEqual(len(list((L / "projects" / slug / "exports").glob("*.mp4"))), 1)
        evs = [json.loads(l) for l in (L / "events.jsonl").read_text().splitlines()]
        types = {e["type"] for e in evs}
        self.assertFalse({t for t in types if t.startswith("job.")} | {"step.proxy", "ui.started"} & types)
        self.assertTrue({"project.created", "timeline.saved", "note", "render.done", "ingested",
                         "step.moments", "step.draft"} <= types)
        self.assertFalse([e for e in evs if e["media"] and e["media"] not in (ph, sw)])
        self.assertFalse([e for e in evs if e["type"] == "timeline.saved" and e["data"]["project"] != slug])
        for e in evs:
            if e["type"] == "ingested":
                self.assertFalse(e["data"]["path"].startswith("/"), "no paths from the sender's Mac")
        # moments.csv: one row per moment, spreadsheet-ready
        with open(out / "moments.csv", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0]), ["project", "file", "footage", "moment", "start", "end", "duration",
                                         "title", "hook", "pillar", "angle", "kind", "why"])
        n_moments = sum(len(json.loads(p.read_text())) for p in L.glob("media/*/moments.json"))
        self.assertEqual(len(rows), n_moments)
        for r in rows:
            self.assertTrue((out / r["footage"]).is_file())
            self.assertAlmostEqual(float(r["end"]) - float(r["start"]), float(r["duration"]), delta=0.011)
        self.assertEqual([r["title"] for r in rows if r["file"] == "phone-talking.mov"],
                         [m["title"] for m in sh["moments"]])
        readme = (out / "README.md").read_text()
        for want in ("studio import", "github.com/gudnuf/studio", "Share Me", "moments.csv",
                     sh["moments"][0]["title"], sh["moments"][0]["hook"]):
            self.assertIn(want, readme)
        # the zip: stored (no compression), one top folder, every file
        z = out.with_name(out.name + ".zip")
        with zipfile.ZipFile(z) as zf:
            self.assertIsNone(zf.testzip())
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist()))
            self.assertEqual({i.filename for i in zf.infolist()},
                             {f"{out.name}/{p}" for p in self._tree(out)})
        # a bundle needs a project, and a new folder
        r = H.cli(self.home, "bundle", H.new_home("b2") / "x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("-p", r.stderr)
        r = H.cli(self.home, "bundle", out, "-p", slug, check=False)
        self.assertIn("already exists", r.stderr)

    def test_bundle_never_touches_sources(self):
        sh = self._shared_project()
        before = {m: (self.home / "media" / m) for m in (sh["ph"], sh["sw"])}
        snap = {m: self._tree(d) for m, d in before.items()}
        log_before = (self.home / "events.jsonl").read_bytes()
        out = H.new_home("bundle-src") / "again"
        H.cli(self.home, "bundle", out, "-p", sh["slug"])
        self.assertEqual({m: self._tree(d) for m, d in before.items()}, snap, "library media changed")
        self.assertEqual((self.home / "events.jsonl").read_bytes(), log_before, "bundle wrote to the log")
        for f in H.FIXTURE_FILES:
            self.assertTrue((self.fx / f).exists())

    def test_bundle_import_round_trip_and_idempotent(self):
        sh = self._shared_project()
        out, slug, ph, sw = sh["out"], sh["slug"], sh["ph"], sh["sw"]
        bundle_before = self._tree(out)
        home = H.new_home("import")
        r = H.cli(home, "import", out)
        self.assertIn("2 new files", r.stdout)
        self.assertEqual(self._tree(out), bundle_before, "import changed the bundle")
        S = H.import_studio(home)
        try:
            st = S.state()
            src = self.st()
            self.assertEqual(st["projects"][slug]["name"], "Share Me")
            self.assertEqual(st["projects"][slug]["timeline"], src["projects"][slug]["timeline"])
            self.assertEqual(json.loads((home / "projects" / slug / "timeline.json").read_text()),
                             src["projects"][slug]["timeline"])
            self.assertEqual(set(st["media"]), {ph, sw})
            for m in (ph, sw):
                it = st["media"][m]
                self.assertEqual(it["project"], "shoot")
                self.assertTrue(Path(it["path"]).is_file())
                self.assertTrue(Path(it["path"]).is_relative_to(home))
                self.assertEqual(S.receipt(it["path"]), src["media"][m]["sha256"])
                for step in ("proxy", "transcribe", "keyframes"):   # proxy remade on import
                    self.assertIn(step, it["steps"], f"{m} {step}")
                self.assertTrue((home / "media" / m / "proxy.mp4").is_file())
                for f in ("transcript.json", "moments.json", "drafts.json"):
                    a, b = home / "media" / m / f, out / "library" / "media" / m / f
                    if b.exists():
                        self.assertEqual(a.read_bytes(), b.read_bytes(), f)
            self.assertEqual(st["media"][ph]["steps"]["moments"], src["media"][ph]["steps"]["moments"])
            self.assertIn("draft", st["media"][ph]["steps"])
            self.assertEqual(st["media"][ph]["steps"]["transcribe"]["words"],
                             src["media"][ph]["steps"]["transcribe"]["words"])
            self.assertTrue([n for n in st["notes"] if n.get("text") == "bundle me" and n["project"] == slug])
            ex = st["projects"][slug]["exports"]
            self.assertEqual(len(ex), 1)
            self.assertTrue((home / ex[0]["file"]).is_file())
        finally:
            S.HOME = self.home
        show = H.cli(home, "media", "show", ph).stdout
        self.assertIn(sh["moments"][0]["title"], show)
        self.assertIn("posts: 3 drafted", show)
        ls = H.cli(home, "project", "ls").stdout
        self.assertRegex(ls, r"share-me\s+0 media\s+2 clips\s+Share Me")
        self.assertRegex(ls, r"shoot\s+2 media")
        # the imported story renders
        rendered = Path(H.cli(home, "render", slug).stdout.strip().splitlines()[-1])
        self.assertTrue(rendered.is_file())
        self.assertAlmostEqual(H.ffprobe(rendered)["duration"],
                               sum(c["out"] - c["in"] for c in src["projects"][slug]["timeline"]["clips"]), delta=0.5)
        # a second import changes nothing
        log1 = (home / "events.jsonl").read_bytes()
        files1 = {p for p in self._tree(home / "media")}
        r2 = H.cli(home, "import", out)
        self.assertIn("nothing new", r2.stdout)
        self.assertEqual((home / "events.jsonl").read_bytes(), log1)
        self.assertEqual({p for p in self._tree(home / "media")}, files1)

    def test_bundle_import_slug_collision(self):
        sh = self._shared_project()
        home = H.new_home("import-clash")
        mine = H.cli(home, "project", "new", "Share Me").stdout.strip().splitlines()[-1]
        self.assertEqual(mine, sh["slug"])
        H.cli(home, "say", "my own note", "-p", mine)
        r = H.cli(home, "import", sh["out"], "--no-previews")
        self.assertIn(f"came in as '{sh['slug']}-2'", r.stdout)
        S = H.import_studio(home)
        try:
            st = S.state()
            self.assertEqual(st["projects"][mine]["timeline"]["clips"], [], "my project was overwritten")
            self.assertEqual(len(st["projects"][f"{mine}-2"]["timeline"]["clips"]), 2)
            notes = {n["text"]: n["project"] for n in st["notes"]}
            self.assertEqual(notes, {"my own note": mine, "bundle me": f"{mine}-2"})
            self.assertTrue(all(e["data"]["file"].startswith(f"projects/{mine}-2/")
                                for e in H.events(home) if e["type"] == "render.done"))
            self.assertTrue((home / st["projects"][f"{mine}-2"]["exports"][0]["file"]).is_file())
        finally:
            S.HOME = self.home
        n = len(H.events(home))
        r = H.cli(home, "import", sh["out"], "--no-previews")
        self.assertIn("nothing new", r.stdout)
        self.assertEqual(len(H.events(home)), n, "second import duplicated or re-suffixed")
        self.assertNotIn(f"{mine}-3", H.cli(home, "project", "ls").stdout)

    def test_bundle_import_refuses_corrupt_or_missing_footage(self):
        sh = self._shared_project()
        bad = H.new_home("bundle-bad") / "copy"
        shutil.copytree(sh["out"], bad)
        man = json.loads((bad / "manifest.json").read_text())
        victim = bad / man["media"][0]["footage"]
        with open(victim, "r+b") as f:          # flip one byte in the middle
            f.seek(victim.stat().st_size // 2)
            b = f.read(1)
            f.seek(-1, 1)
            f.write(bytes([b[0] ^ 0xFF]))
        gone = bad / man["media"][1]["footage"]
        gone.unlink()
        home = H.new_home("import-bad")
        r = H.cli(home, "import", bad, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("damaged", r.stderr)
        self.assertIn(man["media"][0]["footage"], r.stderr)
        self.assertIn("missing", r.stderr)
        self.assertIn(man["media"][1]["footage"], r.stderr)
        self.assertEqual(H.events(home), [], "a refused import must change nothing")
        self.assertEqual(list((home / "media").iterdir()), [])
        # not a bundle / still zipped: plain words
        r = H.cli(home, "import", sh["out"].with_name(sh["out"].name + ".zip"), check=False)
        self.assertIn("still zipped", r.stderr)
        r = H.cli(home, "import", home, check=False)
        self.assertIn("isn't a studio bundle", r.stderr)


@unittest.skipUnless(H.have_tools(*NEEDS, "whisper-vad-speech-segments"),
                     "needs ffmpeg/whisper-cli/whisper-vad-speech-segments (run in nix develop)")
class Quality(unittest.TestCase):
    """Speech edges, the hallucination gate, render loudness and `studio check` (QUALITY.md),
    on fixtures whose speech is known by construction (test/fixtures.sh): bursts.mov is three
    sentences with 1.2s and 0.9s of room tone between them and 0.36s of black picture in the
    second gap; broll-roomtone.mov is picture over room tone, nobody talking."""

    @classmethod
    def setUpClass(cls):
        cls.fx = H.fixtures()
        cls.known = json.loads((cls.fx / "bursts.json").read_text())
        cls.home = H.new_home("quality")
        H.cli(cls.home, "ingest", "-p", "q", cls.fx / "bursts.mov", cls.fx / "broll-roomtone.mov")
        cls.S = H.import_studio(cls.home)
        ids = {it["name"]: m for m, it in cls.S.state()["media"].items()}
        cls.b, cls.r = ids["bursts.mov"], ids["broll-roomtone.mov"]

    def setUp(self):
        self.S.HOME = self.home

    def regions(self, mid=None):
        return json.loads((self.home / "media" / (mid or self.b) / "speech.json").read_text())["regions"]

    def project(self, name):
        return H.cli(self.home, "project", "new", name).stdout.strip().splitlines()[-1]

    def clips(self, slug):
        return self.S.state()["projects"][slug]["timeline"]["clips"]

    def check(self, slug, *extra):
        r = H.cli(self.home, "check", slug, *extra, "--json", check=False, timeout=900)
        self.assertIn(r.returncode, (0, 1), r.stderr[-2000:])
        res = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0 if res["ok"] else 1, "exit code follows the hard rules")
        return res, {x["rule"]: x for x in res["rules"]}

    def render_clip(self, name, a, b, snap=True, media=None):
        slug = self.project(name)
        H.cli(self.home, "timeline", "add", slug, media or self.b, a, b, *(["--snap"] if snap else []))
        H.cli(self.home, "render", slug, timeout=900)
        return slug

    def assert_on_edges(self, a, b, regs):
        for r in regs:
            self.assertFalse(r[0] < a < r[1], f"in {a} inside speech {r}")
            self.assertFalse(r[0] < b < r[1], f"out {b} inside speech {r}")
        first = min(r[0] for r in regs if r[0] >= a)
        last = max(r[1] for r in regs if r[1] <= b)
        self.assertTrue(0.05 <= first - a <= 0.15, f"lead-in {first - a:.3f}")
        self.assertGreaterEqual(b - last, 0.25, f"tail {b - last:.3f}")

    # ---- speech regions + the hallucination gate ----
    def test_speech_regions_match_the_known_speech(self):
        regs = self.regions()
        self.assertEqual(len(regs), 3, regs)
        for (a, b), (ka, kb) in zip(regs, self.known["speech"]):
            self.assertLess(abs(a - ka), 0.15, (a, ka))
            self.assertLess(abs(b - kb), 0.2, (b, kb))
        st = self.S.state()["media"][self.b]["steps"]
        self.assertEqual(st["speech"]["regions"], 3)
        self.assertEqual(st["transcribe"]["speech"], 3)
        # every word sits inside the speech whisper heard (its times are mapped back from
        # --vad's shortened audio), and the sentences all came through
        tx = json.loads((self.home / "media" / self.b / "transcript.json").read_text())
        text = " ".join(w["w"] for w in tx["words"]).lower()
        for w in ("sprinkler", "hours", "truck"):
            self.assertIn(w, text)
        for w in tx["words"]:
            self.assertTrue(any(a - 0.05 <= w["start"] and w["end"] <= b + 0.15 for a, b in regs), w)

    def test_silent_clip_has_an_empty_transcript(self):
        self.assertEqual(self.regions(self.r), [])
        tx = json.loads((self.home / "media" / self.r / "transcript.json").read_text())
        self.assertEqual((tx["words"], tx["segments"]), ([], []), "invented words on b-roll")
        self.assertEqual(self.S.state()["media"][self.r]["steps"]["transcribe"]["words"], 0)

    def test_hallucination_phrases_are_dropped(self):
        segs = [{"start": 0, "end": 2, "text": "The mulch is gone."},
                {"start": 2, "end": 3, "text": "Thanks for watching!"},
                {"start": 3, "end": 5, "text": "Subtitles by the Amara.org community"},
                {"start": 5, "end": 7, "text": "Front beds need three yards."}]
        words = [{"w": t, "start": s["start"], "end": s["end"], "_seg": i}
                 for i, s in enumerate(segs) for t in s["text"].split()]
        segs2, words2, dropped = self.S.drop_hallucinations(segs, words)
        self.assertEqual([s["text"] for s in segs2], ["The mulch is gone.", "Front beds need three yards."])
        self.assertEqual(len(dropped), 2)
        self.assertNotIn("watching!", [w["w"] for w in words2])

    # ---- snapping ----
    def test_snap_clip_rules(self):
        R = [[1.0, 3.0], [3.4, 6.0], [6.05, 8.0], [10.0, 11.0], [14.0, 19.0]]
        snap = self.S.snap_clip
        a, b, why = snap(1.5, 2.5, R, 20)              # both points inside one phrase
        self.assertEqual((a, b), (0.93, 3.26))
        self.assertIn("inside speech", why)
        a, b, _ = snap(0.2, 2.0, R, 20)                 # dead air before the first word goes
        self.assertEqual(a, 0.93)
        a, b, _ = snap(3.5, 5.0, R, 20)                 # out: a 0.05s gap, no room for the tail
        self.assertEqual((a, b), (3.33, 6.025))         #   -> halfway, never in the next phrase
        a, b, _ = snap(6.5, 7.5, R, 20)                 # in: the same gap from the other side
        self.assertEqual((a, b), (6.025, 8.26))
        self.assertEqual(snap(8.5, 9.8, R, 20), (8.5, 9.8, None), "a silent shot is left alone")
        a, b, _ = snap(2.95, 5.0, R, 20)                # grazes the phrase before: left out
        self.assertEqual(a, 3.33)
        # whisper's early word end put the out point before the last word's phrase began
        a, b, _ = snap(3.5, 9.9, R, 20, [{"w": "estimate.", "start": 9.8}])
        self.assertEqual(b, 11.26)
        # deep in a long run of speech: no edge within reach, so the cut stays near (the
        # quietest moment there; here a stand-in that says where it was asked to look)
        a, b, why = snap(16.5, 17.0, R, 30, quiet=lambda t, lo, hi: round(t + 0.1, 3))
        self.assertEqual((a, b), (16.6, 17.1))
        self.assertIn("deep in a run of speech", why)
        self.assertEqual(snap(*snap(1.5, 2.5, R, 20)[:2], R, 20)[2], None, "snapping is idempotent")

    def test_snap_lands_outside_speech(self):
        (self.home / "media" / self.b / "speech.json").unlink()   # older media: made on demand
        regs = self.known["speech"]
        slug = self.project("Snap")
        mid1 = regs[0][0] + 0.8                 # inside speech, within reach of its edges
        mid2 = regs[1][1] - 0.8
        H.cli(self.home, "timeline", "add", slug, self.b, mid1, mid2, "--snap")
        self.assertTrue((self.home / "media" / self.b / "speech.json").exists())
        c = self.clips(slug)[0]
        self.assert_on_edges(c["in"], c["out"], self.regions())
        self.assertLess(c["in"], regs[0][0], "the in point starts before the first word")
        self.assertGreater(c["out"], regs[1][1] + 0.2, "the out point is past the last word")
        # trailing dead air goes too
        H.cli(self.home, "timeline", "add", slug, self.b, regs[2][0] - 0.6, regs[2][1] + 1.2, "--snap")
        c = self.clips(slug)[1]
        self.assert_on_edges(c["in"], c["out"], self.regions())
        # a trim without --snap is exactly what was asked (a person's trim is never fought)
        H.cli(self.home, "timeline", "trim", slug, c["id"], mid1, mid2)
        c = self.clips(slug)[1]
        self.assertEqual((c["in"], c["out"]), (round(mid1, 3), round(mid2, 3)))
        out = H.cli(self.home, "timeline", "snap", slug, "all").stderr
        self.assertIn("snapped", out)
        for c in self.clips(slug):
            self.assert_on_edges(c["in"], c["out"], self.regions())

    def test_moments_snap_by_default(self):
        ms = [{"n": 1, "start": round(self.known["speech"][0][0] + 0.3, 2),
               "end": round(self.known["speech"][0][1] - 0.4, 2), "title": "Heads", "hook": "h",
               "why": "w", "angle": "past-self", "kind": "talking"}]
        (self.home / "media" / self.b / "moments.json").write_text(json.dumps(ms))
        self.S.emit("step.moments", self.b, moments=1, fixture=True)
        slug = self.project("Moment snap")
        H.cli(self.home, "timeline", "add", slug, f"{self.b}@1")
        c = self.clips(slug)[0]
        self.assert_on_edges(c["in"], c["out"], self.regions())
        H.cli(self.home, "timeline", "add", slug, f"{self.b}@1", "--no-snap")
        c = self.clips(slug)[1]
        self.assertEqual((c["in"], c["out"]), (ms[0]["start"], ms[0]["end"]))

    def test_moments_prompt_carries_quality_and_may_be_empty(self):
        agent = self.home / "empty-agent"
        agent.write_text(f"#!/bin/sh\ncat > {self.home / 'prompt.txt'}\necho '[]'\n")
        agent.chmod(0o755)
        H.cli(self.home, "moments", self.r, env=H.env_for(self.home, STUDIO_AGENT_CMD=str(agent)))
        prompt = (self.home / "prompt.txt").read_text()
        self.assertIn("THE BAR (QUALITY.md)", prompt)
        self.assertIn("One takeaway", prompt)
        self.assertIn("return []", prompt)
        self.assertEqual(self.S.state()["media"][self.r]["steps"]["moments"]["moments"], 0)

    # ---- render ----
    def test_render_meets_the_loudness_target(self):
        slug = self.render_clip("Loud", 0.5, self.known["speech"][1][1])
        ev = [e for e in H.events(self.home) if e["type"] == "render.done" and e["data"]["project"] == slug][-1]["data"]
        self.assertTrue(ev["loudness"]["normalized"])
        self.assertEqual(len(ev["timeline"]["clips"]), 1, "the cut list travels with the export")
        import subprocess
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(self.home / ev["file"]), "-vn",
                            "-af", "loudnorm=print_format=json", "-f", "null", "-"], capture_output=True, text=True)
        m = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
        self.assertLessEqual(abs(float(m["input_i"]) + 14), 1.0, m)
        self.assertLessEqual(float(m["input_tp"]), -1.0, m)
        self.assertEqual(H.ffprobe(self.home / ev["file"])["vcodec"], "h264")

    # ---- check ----
    def test_check_passes_snapped_cuts_and_writes_a_review_sheet(self):
        slug = self.render_clip("Clean", 0.5, 3.0)       # sentence 1, snapped
        res, r = self.check(slug)
        for k in ("cuts", "first word", "pauses", "loudness", "picture", "captions", "ending"):
            self.assertEqual(r[k]["status"], "pass", r[k])
        self.assertEqual(r["length"]["status"], "fail", "3s is under the 15s minimum")
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed"], ["length"])
        d = self.home / res["sheet"]
        self.assertEqual(d.parent, (self.home / res["file"]).parent, "the sheet sits next to the export")
        pngs = sorted(p.name for p in d.glob("*.png"))
        self.assertIn("contact.png", pngs)
        self.assertEqual(len([p for p in pngs if p.startswith("cut-")]), 2, pngs)   # start + end
        for p in pngs:
            self.assertEqual((d / p).read_bytes()[:4], b"\x89PNG")
        self.assertTrue(res["judgment"] and res["judgment"][0].startswith("1. One takeaway"))
        ev = [e for e in H.events(self.home) if e["type"] == "check.done"][-1]["data"]
        self.assertEqual((ev["project"], ev["ok"], ev["failed"]), (slug, False, ["length"]))
        self.assertTrue((d / "check.json").exists())

    def test_check_catches_a_cut_inside_speech(self):
        regs = self.known["speech"]
        slug = self.render_clip("Clipped", regs[0][0] + 0.4, regs[0][1] - 0.6, snap=False)
        res, r = self.check(slug)
        self.assertEqual(r["cuts"]["status"], "fail")
        self.assertTrue(any(i.startswith("clip 1 in ") and "inside speech" in i for i in r["cuts"]["items"]), r["cuts"])
        self.assertTrue(any(i.startswith("clip 1 out ") and "inside speech" in i for i in r["cuts"]["items"]), r["cuts"])
        self.assertIn("cuts", res["failed"])

    def test_check_catches_a_long_pause(self):
        regs = self.known["speech"]
        slug = self.render_clip("Pause", regs[0][0], regs[1][1])     # 1.2s of room tone inside
        res, r = self.check(slug)
        self.assertEqual(r["cuts"]["status"], "pass", r["cuts"])
        self.assertEqual(r["pauses"]["status"], "fail")
        self.assertEqual(len(r["pauses"]["items"]), 1, r["pauses"])
        gap = float(r["pauses"]["items"][0].split("s pause")[0])
        self.assertAlmostEqual(gap, regs[1][0] - regs[0][1], delta=0.25)
        self.assertEqual(r["picture"]["status"], "pass")

    def test_check_catches_a_black_segment(self):
        regs = self.known["speech"]
        slug = self.render_clip("Black", regs[1][0], regs[2][1])     # black in the second gap
        res, r = self.check(slug)
        self.assertEqual(r["picture"]["status"], "fail")
        self.assertTrue(any(i.startswith("black") for i in r["picture"]["items"]), r["picture"])

    def test_check_catches_wrong_loudness(self):
        slug = self.render_clip("Quiet", 0.5, 3.0)
        final = self.home / self.S.state()["projects"][slug]["exports"][-1]["file"]
        quiet = final.with_name("quiet.mp4")
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(final), "-c:v", "copy", "-af", "volume=-12dB",
                        "-c:a", "aac", str(quiet)], check=True)
        res, r = self.check(slug, quiet)
        self.assertEqual(r["loudness"]["status"], "fail", r["loudness"])
        self.assertEqual(r["cuts"]["status"], "skip", "an outside file has no cut list")
        self.assertTrue((quiet.parent / "quiet.check" / "contact.png").exists())

    def test_check_catches_invented_captions(self):
        # what whisper used to do on b-roll: a sentence nobody said, burned in as captions
        d = self.home / "media" / self.r
        saved = (d / "transcript.json").read_text()
        words = [{"w": w, "start": 2 + i * 0.4, "end": 2.35 + i * 0.4}
                 for i, w in enumerate("Thank you so much for watching.".split())]
        (d / "transcript.json").write_text(json.dumps({"language": "en", "words": words,
                                                       "segments": [{"start": 2, "end": 4.4, "text": "Thank you so much for watching."}]}))
        try:
            slug = self.render_clip("Broll", 0.5, 7.5, media=self.r)
            res, r = self.check(slug)
        finally:
            (d / "transcript.json").write_text(saved)
        self.assertEqual(r["captions"]["status"], "fail", r["captions"])
        self.assertEqual(r["cuts"]["status"], "pass", "no speech: nothing to cut into")


    # ---- speechless clips are picture; declared on-screen text; cards ----
    def test_a_clip_with_no_speech_is_picture_not_a_pause(self):
        # spoken, then 7s of b-roll nobody talks in (a cartoon, a walk), then spoken: before,
        # check called the b-roll a 7-8s "pause"
        regs = self.known["speech"]
        slug = self.project("Speechless")
        H.cli(self.home, "timeline", "add", slug, self.b, regs[0][0] - 0.3, regs[0][1] + 0.3, "--snap")
        H.cli(self.home, "timeline", "add", slug, self.r, 0.5, 7.5)
        H.cli(self.home, "timeline", "add", slug, self.b, regs[1][0] - 0.3, regs[1][1] + 0.3, "--snap")
        H.cli(self.home, "render", slug, timeout=900)
        res, r = self.check(slug)
        self.assertEqual(r["pauses"]["status"], "pass", r["pauses"])
        self.assertLess(float(r["pauses"]["detail"].split("longest pause ")[1].split("s")[0]), 0.36)
        self.assertEqual(r["cuts"]["status"], "pass", r["cuts"])
        self.assertEqual(r["first word"]["status"], "pass", r["first word"])

    def test_exceptions_on_screen_text_cards_and_ready(self):
        regs = self.known["speech"]
        slug = self.project("Cold open")
        # a 1.5s silent cold open, then two sentences with 0.9s of room tone (and 0.36s of black
        # picture) between them, labelled the way the old magic-word path wanted
        H.cli(self.home, "timeline", "add", slug, self.r, 0.5, 2.0)
        H.cli(self.home, "timeline", "add", slug, self.b, regs[1][0] - 0.3, regs[2][1] + 0.3, "--snap")
        H.cli(self.home, "timeline", "label", slug, "2", "payoff hold reveal still")
        c1, c2 = [c["id"] for c in self.clips(slug)]
        H.cli(self.home, "render", slug, timeout=900)
        self.assertEqual(H.cli(self.home, "ready", slug, check=False).returncode, 1, "not checked: not ready")
        res, r = self.check(slug)
        self.assertEqual(r["first word"]["status"], "fail", r["first word"])
        self.assertEqual(r["pauses"]["status"], "fail", "a label is not an exception any more")
        self.assertEqual(r["picture"]["status"], "fail", r["picture"])
        ev = [e for e in H.events(self.home) if e["type"] == "check.done"][-1]["data"]
        exp = self.S.state()["projects"][slug]["exports"][-1]
        self.assertEqual(ev["sha256"], self.S.receipt(self.home / exp["file"]), "bound to the bytes")
        self.assertEqual(ev["brand"], "jefe")
        self.assertIn({"rule": "pauses", "clip": c2}, [{k: f[k] for k in ("rule", "clip")} for f in ev["failures"]])
        r_ = H.cli(self.home, "ready", slug, check=False)
        self.assertEqual(r_.returncode, 1)
        self.assertIn("not ready", r_.stdout)

        # what an exception may and may not be
        bad = H.cli(self.home, "exception", "grant", slug, "loudness", "--why", "it sounds fine to me", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("can't be excepted", bad.stderr)
        self.assertNotEqual(H.cli(self.home, "exception", "grant", slug, "pauses", c2, check=False).returncode, 0,
                            "an exception needs a reason in words")
        self.assertNotEqual(H.cli(self.home, "exception", "grant", slug, "picture", c2, "--onscreen", "x",
                                  "--why", "mismatched declaration", check=False).returncode, 0,
                            "--onscreen goes with first word, --card with picture")
        # the agent grants its own, with reasons. What the picture holds (on-screen text, a
        # designed card) is declared the same way: nothing measures it, so it's an exception
        why = "the beat after 'hours' is a reveal; the picture carries the held silence"
        H.cli(self.home, "exception", "grant", slug, "pauses", c2, "--why", why, as_agent=True)
        H.cli(self.home, "exception", "grant", slug, "length", "--why", "a test reel: short on purpose", as_agent=True)
        H.cli(self.home, "exception", "grant", slug, "first word", c1, "--onscreen", "3 hours of sprinklers",
              "--why", "the cold open shows the hook as text over the sprinklers", as_agent=True)
        H.cli(self.home, "exception", "grant", slug, "picture", c2, "--card",
              "--why", "the dark beat is the designed title card", as_agent=True)
        g = [e["data"] for e in H.events(self.home) if e["type"] == "exception.granted"]
        self.assertEqual([(x["rule"], x["clip"], x["actor"], x["brand"], x.get("kind")) for x in g],
                         [("pauses", c2, "agent", "jefe", None), ("length", None, "agent", "jefe", None),
                          ("first word", c1, "agent", "jefe", "onscreen"), ("picture", c2, "agent", "jefe", "card")])
        self.assertTrue(all(x["fp"] for x in g), "bound to what they were granted on")
        self.assertEqual(g[2]["text"], "3 hours of sprinklers")
        H.cli(self.home, "render", slug, timeout=900)
        self.assertEqual(H.cli(self.home, "ready", slug, check=False).returncode, 1, "a re-render starts unchecked")
        r2 = H.cli(self.home, "check", slug, timeout=900)          # exit 0: every failure excepted
        self.assertIn("EXCEPTION", r2.stdout)
        self.assertIn(why, r2.stdout)
        self.assertIn("declared on-screen text: '3 hours of sprinklers'", r2.stdout)
        self.assertIn("declared designed card", r2.stdout)
        res, r = self.check(slug)
        self.assertTrue(res["ok"], res["failed"])
        self.assertEqual(r["first word"]["status"], "excepted", r["first word"])
        self.assertEqual(r["first word"]["excepted"][0]["declares"], "on-screen text")
        self.assertEqual(r["picture"]["status"], "excepted", "a declared card is listed, not silently passed")
        self.assertEqual(r["pauses"]["status"], "excepted")
        self.assertEqual(r["pauses"]["excepted"][0]["why"], why)
        self.assertEqual({x["rule"] for x in res["excepted"]}, {"pauses", "length", "first word", "picture"})
        self.assertIn("EXCEPTION", (self.home / res["sheet"] / "verdict.txt").read_text(), "on the review sheet")
        rd = H.cli(self.home, "ready", slug)
        self.assertIn("ready:", rd.stdout)
        self.assertIn("EXCEPTION", rd.stdout)
        self.assertTrue(self.S.state()["projects"][slug]["ready"])

        # the operator revokes one: not ready again, and the revocation says what it took back
        xid = [x for x in g if x["rule"] == "pauses"][0]["id"]
        H.cli(self.home, "exception", "revoke", slug, xid, "--why", "that silence drags")
        rv = [e["data"] for e in H.events(self.home) if e["type"] == "exception.revoked"][-1]
        self.assertEqual((rv["rule"], rv["clip"], rv["granted_by"], rv["granted_why"], rv["why"]),
                         ("pauses", c2, "agent", why, "that silence drags"))
        r_ = H.cli(self.home, "ready", slug, check=False)
        self.assertEqual(r_.returncode, 1)
        self.assertIn("fails pauses", r_.stdout)
        self.assertNotEqual(H.cli(self.home, "exception", "revoke", slug, xid, check=False).returncode, 0)
        ls = H.cli(self.home, "exception", "ls", slug, "--all").stdout
        self.assertIn("REVOKED", ls)
        # bytes changed under the record: not ready either
        H.cli(self.home, "exception", "grant", slug, "pauses", c2, "--why", "trying it again, held on purpose")
        self.assertEqual(H.cli(self.home, "ready", slug, check=False).returncode, 0)
        f = self.home / self.S.state()["projects"][slug]["exports"][-1]["file"]
        with open(f, "ab") as fh:
            fh.write(b"\0")
        r_ = H.cli(self.home, "ready", slug, check=False)
        self.assertEqual(r_.returncode, 1)
        self.assertIn("changed since it was rendered", r_.stdout)
        # a hand-written exception for a rule that can't bend: ignored, and ls says so
        self.S.emit("exception.granted", None, id="f0f0f0f0", project=slug, rule="ending", clip=None,
                    fp="x", why="forged by hand", actor="agent")
        self.assertNotIn("f0f0f0f0", H.cli(self.home, "exception", "ls", slug).stdout)
        self.assertIn("INVALID", H.cli(self.home, "exception", "ls", slug, "--all").stdout)

        # the clip changes after the grant: its exceptions lapse; reel-wide ones too
        cur = next(c for c in self.clips(slug) if c["id"] == c2)
        H.cli(self.home, "timeline", "trim", slug, c2, cur["in"], cur["out"] + 0.2)
        ls = json.loads(H.cli(self.home, "exception", "ls", slug, "--json").stdout)
        st_ = {x["rule"]: x["state"] for x in ls if x["state"] != "revoked" and x["rule"] != "ending"}
        self.assertEqual(st_, {"pauses": "lapsed", "length": "lapsed", "first word": "granted", "picture": "lapsed"})
        self.assertIn("LAPSED", H.cli(self.home, "exception", "ls", slug).stdout)
        H.cli(self.home, "render", slug, timeout=900)
        res, r = self.check(slug)
        self.assertEqual(r["pauses"]["status"], "fail", "a lapsed exception covers nothing")
        self.assertIn("lapsed", r["pauses"]["items"][0])
        self.assertEqual(r["first word"]["status"], "excepted", "the unchanged clip keeps its exception")

    def test_check_refuses_bytes_that_change_while_measured(self):
        slug = self.render_clip("Moving target", 0.5, 3.0)
        f = self.home / self.S.state()["projects"][slug]["exports"][-1]["file"]
        n = sum(e["type"] == "check.done" for e in H.events(self.home))
        orig = self.S.scan_picture
        def tamper(path):
            out = orig(path)
            with open(f, "ab") as fh:
                fh.write(b"\0")
            return out
        self.S.scan_picture = tamper
        try:
            with self.assertRaisesRegex(RuntimeError, "changed while it was being checked"):
                self.S.check(slug)
        finally:
            self.S.scan_picture = orig
        self.assertEqual(sum(e["type"] == "check.done" for e in H.events(self.home)), n, "no record")

    # ---- choice ----
    def test_choices_are_logged_with_provenance_and_blind_rounds(self):
        slug = self.render_clip("Versions", 0.5, 3.0)
        H.cli(self.home, "timeline", "trim", slug, "1", 0.6, 3.6, "--snap")
        H.cli(self.home, "render", slug, timeout=900)
        exs = self.S.state()["projects"][slug]["exports"]
        a, b = exs[0]["sha256"], exs[1]["sha256"]
        H.cli(self.home, "choose", slug, b[:8], "--from", f"{a[:8]},{b[:8]}", "--note", "tighter ending")
        ch = [e["data"] for e in H.events(self.home) if e["type"] == "choice"][-1]
        self.assertEqual((ch["kind"], ch["action"], ch["shown"], ch["chosen"], ch["position"], ch["blind"]),
                         ("version", "pick", [a, b], b, 1, False))
        self.assertEqual((ch["project"], ch["brand"], ch["actor"], ch["note"]), (slug, "jefe", "cli", "tighter ending"))
        self.assertEqual(ch["options"][b]["file"], exs[1]["file"])
        self.assertEqual(ch["options"][b]["verdict"], "unchecked")
        self.assertIn("timeline_sha", ch["options"][a])
        self.assertNotEqual(H.cli(self.home, "choose", slug, "zzzz", "--from", f"{a[:8]},{b[:8]}",
                                  check=False).returncode, 0, "the chosen one must have been shown")
        with self.assertRaisesRegex(RuntimeError, "options that were shown"):
            self.S.record_choice(slug, [], None)
        # what a caller adds can't overwrite what the studio knows about an option
        d = self.S.record_choice(slug, [a[:8]], a[:8], options={a[:8]: {"verdict": "ready", "file": "x.mp4",
                                                                        "seen": "the caller's note"}})
        self.assertEqual((d["options"][a]["verdict"], d["options"][a]["file"], d["options"][a]["seen"]),
                         ("unchecked", exs[0]["file"], "the caller's note"))
        # blind: shuffled copies labelled A/B, the mapping in the log, the pick mapped back
        out = H.cli(self.home, "ab", slug).stdout
        rnd = [e["data"] for e in H.events(self.home) if e["type"] == "blind.round"][-1]
        self.assertEqual(set(rnd["labels"].values()), {a, b})
        times = {(self.home / rnd["dir"] / f"{lab}.mp4").stat().st_mtime for lab in rnd["labels"]}
        self.assertEqual(len(times), 1, "the copies' dates don't say which is newer")
        for lab, sha in rnd["labels"].items():
            self.assertEqual(self.S.receipt(self.home / rnd["dir"] / f"{lab}.mp4"), sha)
            self.assertNotIn(sha[:8], out, "the labels hide which is which")
        H.cli(self.home, "choose", slug, "B", "--round", rnd["round"])
        ch = [e["data"] for e in H.events(self.home) if e["type"] == "choice"][-1]
        self.assertEqual((ch["blind"], ch["round"], ch["chosen"], ch["position"], ch["labels"]),
                         (True, rnd["round"], rnd["labels"]["B"], 1, rnd["labels"]))
        self.assertEqual(ch["shown"], [rnd["labels"]["A"], rnd["labels"]["B"]])
        H.cli(self.home, "choose", slug, "none", "--round", rnd["round"])
        self.assertIsNone([e["data"] for e in H.events(self.home) if e["type"] == "choice"][-1]["chosen"])
        # moments (on bursts: other tests run the real moments step on the b-roll): the provenance of the step that made them
        prov = {"provider": "claude", "model": "claude-opus-5-5", "task": "moments", "prompt": "abc123"}
        self.S.emit("step.moments", self.b, moments=2, sha256="f" * 64, provenance=prov)
        H.cli(self.home, "choose", slug, f"{self.b[:6]}@2", "--from", f"{self.b[:6]}@1,{self.b[:6]}@2")
        ch = [e["data"] for e in H.events(self.home) if e["type"] == "choice"][-1]
        self.assertEqual((ch["kind"], ch["chosen"], ch["position"]), ("moment", f"{self.b}@2", 1))
        self.assertEqual(ch["options"][f"{self.b}@1"]["model"], "claude-opus-5-5")
        self.assertEqual(ch["options"][f"{self.b}@1"]["step_sha256"], "f" * 64)


@unittest.skipUnless(H.have_tools(*NEEDS, "whisper-vad-speech-segments"),
                     "needs ffmpeg/whisper-cli/whisper-vad-speech-segments (run in nix develop)")
class Cover(unittest.TestCase):
    """B-roll over a clip's sound (cover), the voice treatment, pace and pause tightening, on
    the quality fixtures: bursts.mov (three sentences, known silences) is the talking clip,
    broll-roomtone.mov (moving picture, nobody talking) the b-roll."""

    @classmethod
    def setUpClass(cls):
        cls.fx = H.fixtures()
        cls.known = json.loads((cls.fx / "bursts.json").read_text())
        cls.home = H.new_home("cover")
        H.cli(cls.home, "ingest", "-p", "q", cls.fx / "bursts.mov", cls.fx / "broll-roomtone.mov",
              cls.fx / "screen-recording.mp4")
        cls.S = H.import_studio(cls.home)
        ids = {it["name"]: m for m, it in cls.S.state()["media"].items()}
        cls.b, cls.r = ids["bursts.mov"], ids["broll-roomtone.mov"]
        cls.scr = ids["screen-recording.mp4"]    # a picture unlike the talking fixture's

    def setUp(self):
        self.S.HOME = self.home

    def project(self, name, a=0.5, b=8.4, captions="on", media=None, snap=True):
        slug = H.cli(self.home, "project", "new", name).stdout.strip().splitlines()[-1]
        H.cli(self.home, "timeline", "add", slug, media or self.b, a, b, *(["--snap"] if snap else []))
        H.cli(self.home, "timeline", "show", slug, "--captions", captions)
        return slug

    def tl(self, slug):
        return self.S.state()["projects"][slug]["timeline"]

    def render(self, slug):
        H.cli(self.home, "render", slug, timeout=900)
        ev = [e for e in H.events(self.home) if e["type"] == "render.done" and e["data"]["project"] == slug][-1]
        return self.home / ev["data"]["file"], ev["data"]

    def check(self, slug):
        r = H.cli(self.home, "check", slug, "--json", check=False, timeout=900)
        self.assertIn(r.returncode, (0, 1), r.stderr[-2000:])
        res = json.loads(r.stdout)
        return res, {x["rule"]: x for x in res["rules"]}

    def pcm(self, path):
        import array, subprocess
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                              "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
        return array.array("h", raw[:len(raw) // 2 * 2])

    def frame(self, path, t):
        import subprocess
        return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                               "-frames:v", "1", "-vf", "scale=54:96", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
                              capture_output=True, check=True).stdout

    @staticmethod
    def diff(x, y):
        return sum(abs(p - q) for p, q in zip(x, y)) / max(1, min(len(x), len(y)))

    def sound_diff(self, x, y):
        """RMS of the difference over RMS of the signal (0 = the same sound)."""
        n = min(len(x), len(y))
        num = sum((x[i] - y[i]) ** 2 for i in range(0, n, 4))
        den = sum(x[i] ** 2 for i in range(0, n, 4)) or 1
        return (num / den) ** 0.5

    # ---- cover ----
    def test_cover_changes_the_picture_not_the_sound(self):
        plain = self.project("Plain", captions="off")
        covered = self.project("Covered", captions="off")
        roll = self.project("Roll", 1.0, 4.0, captions="off", media=self.scr, snap=False)
        clip = self.tl(covered)["clips"][0]
        H.cli(self.home, "timeline", "cover", covered, clip["id"], "--with", self.scr, 1.0, 4.0, "--at", 2.0)
        c = self.tl(covered)["clips"][0]
        self.assertEqual(c["cover"], [{"media": self.scr, "in": 1.0, "out": 4.0, "at": round(clip["in"] + 2.0, 3)}])
        self.assertIn("cover 1: 2.00–5.00s of the clip (2.00–5.00s in the reel) shows screen-recording.mp4 1.00–4.00s",
                      H.cli(self.home, "timeline", "show", covered).stdout)
        fp, ev = self.render(covered)
        f0, _ = self.render(plain)
        fr, _ = self.render(roll)
        self.assertAlmostEqual(H.ffprobe(fp)["duration"], H.ffprobe(f0)["duration"], delta=0.1)
        self.assertEqual(ev["concat"], "copy", "a covered part joins the others losslessly")
        # the sound is the clip's own, untouched by what's over it
        self.assertLess(self.sound_diff(self.pcm(f0), self.pcm(fp)), 0.02)
        # the picture: the clip's own outside 2-5s, the b-roll inside it, starting where asked
        for t in (0.5, 1.5, 5.6, 7.0):
            self.assertLess(self.diff(self.frame(fp, t), self.frame(f0, t)), 4, f"own picture at {t}")
        for t in (2.3, 3.5, 4.7):
            own, broll = self.frame(f0, t), self.frame(fr, t - 2.0)
            self.assertGreater(self.diff(own, broll), 8, "fixtures too alike to tell apart")
            self.assertLess(self.diff(self.frame(fp, t), broll), 4, f"b-roll at {t}")
        self.assertEqual([(x["kind"], x["at"]) for x in ev["cuts"]], [("picture", 2.0), ("picture", 5.0)])

    def test_check_judges_speech_cuts_not_picture_cuts(self):
        slug = self.project("Check cover")
        c = self.tl(slug)["clips"][0]
        regs = self.known["speech"]
        # both ends of the piece land mid-sentence: fine for the picture, the sound runs on
        at = (regs[0][0] + regs[0][1]) / 2 - c["in"]
        H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.r, 0.5, 3.5, "--at", round(at, 2))
        self.render(slug)
        res, r = self.check(slug)
        self.assertEqual(r["cuts"]["status"], "pass", r["cuts"])
        self.assertEqual(r["cover short"]["status"], "pass", r["cover short"])
        self.assertIn("2 picture-only cuts", r["cuts"]["detail"])
        self.assertEqual(r["captions"]["status"], "pass", "captions come from the sound, not the b-roll")
        self.assertEqual(sorted(x["kind"] for x in res["cuts"]), ["picture", "picture"])
        self.assertEqual(len([i for i in res["images"] if i.startswith("cut-")]), 4, "start, 2 picture cuts, end")
        self.assertTrue(any("True" in j and "show" in j.lower() for j in res["judgment"]),
                        "the truth rule is among the judgment questions")

    def test_cover_ops_overlap_split_uncover_and_undo(self):
        slug = self.project("Ops", captions="off")
        cid = self.tl(slug)["clips"][0]["id"]
        rev0 = self.S.state()["projects"][slug]["rev"]
        before = self.tl(slug)
        H.cli(self.home, "timeline", "cover", slug, cid, "--with", self.r, 1.0, 4.0, "--at", 1.0,
              "--base", rev0)
        H.cli(self.home, "timeline", "cover", slug, cid, "--with", self.r, 6.0, 7.0, "--at", 2.0)
        cov = self.tl(slug)["clips"][0]["cover"]
        a = self.tl(slug)["clips"][0]["in"]
        # the new piece cuts a hole in the one it lands on: 1-2 old, 2-3 new, 3-4 old (going on)
        self.assertEqual([(p["in"], p["out"], round(p["at"] - a, 3)) for p in cov],
                         [(1.0, 2.0, 1.0), (6.0, 7.0, 2.0), (3.0, 4.0, 3.0)])
        # stale base refused like every other edit
        r = H.cli(self.home, "timeline", "uncover", slug, cid, "1", "--base", rev0, check=False)
        self.assertEqual(r.returncode, 3)
        # a split gives each side the cover over its own words
        H.cli(self.home, "timeline", "split", slug, cid, 2.5)
        left, right = self.tl(slug)["clips"]
        self.assertEqual([(p["in"], p["out"]) for p in left["cover"]], [(1.0, 2.0), (6.0, 6.5)])
        self.assertEqual([(p["in"], p["out"]) for p in right["cover"]], [(6.5, 7.0), (3.0, 4.0)])
        H.cli(self.home, "timeline", "uncover", slug, right["id"], "2")
        self.assertEqual(len(self.tl(slug)["clips"][1]["cover"]), 1)
        H.cli(self.home, "timeline", "uncover", slug, left["id"])
        self.assertNotIn("cover", self.tl(slug)["clips"][0])
        # undo = the story saved back as it was (what the UI's Undo does): no cover left
        H.cli(self.home, "timeline", "set", slug, "-", input=json.dumps(before))
        self.assertEqual(self.tl(slug), before)
        # every op was a timeline.saved event, by whoever made it
        saves = [e for e in H.events(self.home) if e["type"] == "timeline.saved" and e["data"]["project"] == slug]
        self.assertGreaterEqual(len(saves), 8)

    def test_old_timelines_are_unchanged(self):
        lib = self.S.state()["media"]
        old = {"aspect": "9:16", "captions": True,
               "clips": [{"id": "abcd1234", "media": self.b, "in": 0.7, "out": 4.1, "label": "x"}]}
        self.assertEqual(self.S.normalize_timeline(old, lib), old, "no new keys appear")
        c = old["clips"][0]
        self.assertEqual(self.S.cut_list(old, lib), [])
        self.assertEqual(self.S.cover_plan(c, lib)[0], [(0, 102, None, 0.7)])
        # no voice, no pace: the fades, and exactly the clip's frames of sound (102 frames x 1600)
        self.assertEqual(self.S.sound_filter(102, "off"),
                         "asetpts=PTS-STARTPTS,aresample=48000,afade=t=in:d=0.020,"
                         "afade=t=out:st=3.3800:d=0.020,apad,atrim=end_sample=163200")
        # a cover piece pointing at missing footage is dropped with a warning, never the clip
        probs = []
        tl = self.S.normalize_timeline({**old, "clips": [{**c, "cover": [{"media": "nope", "in": 0, "out": 1, "at": 1}]}]},
                                       lib, probs)
        self.assertNotIn("cover", tl["clips"][0])
        self.assertEqual([k for k, _ in probs], ["cover"])

    def test_cover_speaks_reel_time_on_a_paced_clip(self):
        """--at and the span are seconds into the clip as it plays: on a clip at pace 1.1 the
        b-roll lands where asked in the export with no conversion by hand, and the command says
        the converted numbers back. Stored, it is laid over the clip's source seconds."""
        paced = self.project("Paced cover", captions="off")
        roll = self.project("Paced roll", 1.0, 4.0, captions="off", media=self.scr, snap=False)
        H.cli(self.home, "timeline", "pace", paced, "1", "1.1")
        c = self.tl(paced)["clips"][0]
        r = H.cli(self.home, "timeline", "cover", paced, c["id"], "--with", self.scr, 1.0, 4.0, "--at", 2.0)
        self.assertEqual(self.tl(paced)["clips"][0]["cover"],
                         [{"media": self.scr, "in": 1.0, "out": 4.3, "at": round(c["in"] + 2.2, 3)}])
        self.assertIn("(pace 1.1): 2.00–5.00s of the clip (2.00–5.00s in the reel) shows "
                      "screen-recording.mp4 1.00–4.00s", r.stderr)
        self.assertIn(f"the clip's sound {c['in'] + 2.2:.2f}–{c['in'] + 5.5:.2f}s of bursts.mov", r.stderr)
        fp, ev = self.render(paced)
        fr, _ = self.render(roll)
        self.assertEqual([(x["kind"], x["at"]) for x in ev["cuts"]], [("picture", 2.0), ("picture", 5.0)])
        for t in (2.3, 3.5, 4.7):     # the b-roll's own second t - 2 (it plays at normal speed)
            self.assertLess(self.diff(self.frame(fp, t), self.frame(fr, t - 2.0)), 4, f"b-roll at {t}")
        # --fill covers the whole clip, --until-end from T: both to the clip's end, whatever it becomes
        H.cli(self.home, "timeline", "cover", paced, c["id"], "--with", self.scr, 0.0, "--fill")
        H.cli(self.home, "timeline", "cover", paced, c["id"], "--with", self.r, 1.0, "--at", 1.5, "--until-end")
        H.cli(self.home, "timeline", "cover", paced, c["id"], "--with", self.r, 4.0, "--at", 0.5, "--until", 1.0)
        show = H.cli(self.home, "timeline", "show", paced).stdout
        L = self.S.clip_frames(c) / 30
        for want in ("cover 1: 0.00–0.50s of the clip (0.00–0.50s in the reel) shows screen-recording.mp4 0.00–0.50s",
                     "cover 2: 0.50–1.00s of the clip (0.50–1.00s in the reel) shows broll-roomtone.mov 4.00–4.50s",
                     "cover 3: 1.00–1.50s of the clip (1.00–1.50s in the reel) shows screen-recording.mp4 1.00–1.50s",
                     f"cover 4: 1.50–{L:.2f}s of the clip, to its end"):
            self.assertIn(want, show)
        self.assertNotIn("SHORT", show)

    def test_cover_refuses_what_it_cant_do_and_its_tips_work(self):
        """Footage that can't fill the span, a span past the clip's end, a piece under 3 frames or
        a number that isn't one are refused, nothing saved; every number a refusal suggests then
        works as printed."""
        import re
        slug = self.project("Refusals", captions="off")             # ~7.9 s; the b-roll is 8.0 s
        c = self.tl(slug)["clips"][0]
        rev = self.S.state()["projects"][slug]["rev"]
        cover = lambda *a, check=False: H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.r, *a,
                                              check=check)
        r = cover(2.0, "--fill")
        self.assertEqual(r.returncode, 1, r.stderr)
        for want in ("broll-roomtone.mov is too short", "frames short", "Nothing was laid", "IN ", "--until ", "--at "):
            self.assertIn(want, r.stderr)
        tip_in = re.search(r"IN (\d+\.\d\d) or less", r.stderr).group(1)
        tip_until = re.search(r"--until (\d+\.\d\d)", r.stderr).group(1)
        self.assertEqual(cover(7.0, 9.0).returncode, 1)
        r = cover(0.0, 6.0, "--at", 3.0)
        self.assertEqual(r.returncode, 1)
        self.assertIn("past the clip's end", r.stderr)
        tip_out = re.search(r"OUT (\d+\.\d\d)", r.stderr).group(1)
        self.assertEqual(cover(0.0, 2.0, "--fill").returncode, 1, "one way to say how far")
        self.assertIn("2. Nothing was laid", cover(1.0, 1.06, "--at", 0.2).stderr, "two frames: refused")
        for bad in (("1.0", "--until", "nan"), ("1.0", "nan"), ("1.0", "2.0", "--at", "inf"), ("nan", "--fill")):
            r = cover(*bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("must be a number of seconds", r.stderr)
        self.assertEqual(self.S.state()["projects"][slug]["rev"], rev, "nothing saved")
        # the tips, as printed
        cover(tip_in, "--fill", check=True)
        cover(2.0, "--until", tip_until, check=True)
        cover(0.0, tip_out, "--at", 3.0, check=True)
        cover(1.0, 1.1, "--at", 0.2, check=True)                   # 3 frames: laid, and it shows
        x = next(x for x in self.S.cover_plan(self.tl(slug)["clips"][0], self.S.state()["media"])[1]
                 if x["piece"]["in"] == 1.0 and x["piece"]["out"] == 1.1)
        self.assertEqual(x["shown"], (6, 9))
        # a hand-written NaN never reaches the log: dropped with a warning, and the state is JSON
        tl = self.tl(slug)
        tl["clips"][0]["cover"].append({"media": self.r, "in": float("nan"), "out": 1.0, "at": 1.0})
        r = H.cli(self.home, "timeline", "set", slug, "-", input=json.dumps(tl))
        self.assertIn("not finite numbers", r.stderr)
        def strict(x):
            raise ValueError(f"not JSON: {x}")
        json.loads(H.cli(self.home, "state", "--json").stdout, parse_constant=strict)
        json.loads((self.home / "events.jsonl").read_text().splitlines()[-1], parse_constant=strict)

    def test_edits_keep_cover_under_its_words(self):
        """A trim or snap of the in point, a pace change and a trim of the out point leave every
        piece laid over the same source seconds (its words); a piece the clip no longer has is
        removed with a warning, one an edit cuts into is said."""
        slug = self.project("Anchored", 0.5, 8.4, captions="off", snap=False)
        c = self.tl(slug)["clips"][0]
        H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.r, 1.0, 3.0, "--at", 2.0)   # source 2.5-4.5
        H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.scr, 0.0, "--at", 6.0, "--until-end")
        stored = lambda: self.tl(slug)["clips"][0]["cover"]
        laid = stored()
        reel = lambda: [x["shown"] for x in self.S.cover_plan(self.tl(slug)["clips"][0], self.S.state()["media"])[1]]
        r = H.cli(self.home, "timeline", "trim", slug, c["id"], 1.0, 8.4)           # in point 0.5 later
        self.assertEqual(stored(), laid)
        self.assertEqual(reel()[0], (45, 105), "the same words: 0.5 s earlier into the clip")
        self.assertNotIn("warning", r.stderr)
        r = H.cli(self.home, "timeline", "snap", slug, c["id"])                     # the in point moves again
        self.assertEqual(stored(), laid)
        H.cli(self.home, "timeline", "pace", slug, c["id"], "1.2")
        self.assertEqual(stored(), laid, "pace: the same words, played faster")
        cc = self.tl(slug)["clips"][0]
        self.assertEqual(reel()[0][0], self.S.reel_frame((2.5 - cc["in"]) / 1.2))
        # the out point cuts into the first piece: said; past the second: removed, said
        H.cli(self.home, "timeline", "pace", slug, c["id"], "1")
        r = H.cli(self.home, "timeline", "trim", slug, c["id"], 1.0, 4.0)
        self.assertIn("cover 1 now shows 1.50s of the 2.00s it showed", r.stderr)
        self.assertIn("cover 2 dropped: laid outside the clip", r.stderr)
        self.assertEqual(len(stored()), 1)
        # the in point into the piece, then past it: said, then removed
        r = H.cli(self.home, "timeline", "trim", slug, c["id"], 3.5, 6.0)
        self.assertIn("cover 1 now shows 1.00s of the 1.50s it showed", r.stderr)
        r = H.cli(self.home, "timeline", "trim", slug, c["id"], 4.6, 6.0)
        self.assertIn("cover 1 dropped: laid outside the clip", r.stderr)
        self.assertNotIn("cover", self.tl(slug)["clips"][0])

    def test_cover_short_when_footage_runs_out(self):
        """A piece to the clip's end follows a trim that lengthens the clip; when its footage runs
        out first the edit warns, the clip's own picture comes back on the frame it ends (never
        a held frame), and check fails `cover short` (bendable) by the same frames."""
        slug = self.project("Runs out", 0.5, 4.0, captions="off", snap=False)
        c = self.tl(slug)["clips"][0]
        H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.r, 4.0, "--fill")    # 3.5 s of 8
        r = H.cli(self.home, "timeline", "trim", slug, c["id"], 0.5, 12.0)                      # 11.5 s
        self.assertIn("its footage ends (broll-roomtone.mov is", r.stderr)
        self.assertIn('check fails "cover short"', r.stderr)
        dur = self.S.state()["media"][self.r]["duration"]
        spans, _ = self.S.cover_plan(self.tl(slug)["clips"][0], self.S.state()["media"])
        have = int((dur - 4.0) * 30 + 1e-6)
        self.assertEqual([(f0, f1, p is not None) for f0, f1, p, _ in spans], [(0, have, True), (have, 345, False)])
        fp, ev = self.render(slug)
        self.assertEqual([x["frame"] for x in ev["cuts"] if x["kind"] == "picture"], [have])
        self.assertGreater(self.diff(self.frame(fp, (have - 0.5) / 30), self.frame(fp, (have - 1.5) / 30)), 0.05,
                           "the last b-roll frames move: nothing cloned")
        res, rules = self.check(slug)
        self.assertEqual(rules["cover short"]["status"], "fail", rules["cover short"])
        self.assertIn(f"stops at {have / 30:.2f}s", rules["cover short"]["items"][0])
        H.cli(self.home, "exception", "grant", slug, "cover-short", c["id"], "--why", "the face is back for the last words on purpose")
        _, rules = self.check(slug)
        self.assertEqual(rules["cover short"]["status"], "excepted", rules["cover short"])

    def test_bundle_carries_cover_footage(self):
        slug = self.project("Bundled", captions="off")
        H.cli(self.home, "timeline", "cover", slug, "1", "--with", self.r, 1.0, 2.0)
        mids, _ = self.S.bundle_select(self.S.state(), [slug])
        self.assertIn(self.r, mids)

    # ---- voice ----
    def commands(self, slug):
        """Every ffmpeg command line a render of slug runs (in-process)."""
        seen, ff, sh = [], self.S.ff, self.S.sh
        self.S.ff = lambda cmd, *a, **k: (seen.append(cmd), ff(cmd, *a, **k))[1]
        self.S.sh = lambda cmd, *a, **k: (seen.append(cmd), sh(cmd, *a, **k))[1]
        try:
            self.S.render(slug)
        finally:
            self.S.ff, self.S.sh = ff, sh
        return [" ".join(map(str, c)) for c in seen if c and c[0] == "ffmpeg"]

    def test_voice_chain_applies_when_natural_and_not_when_off(self):
        chain = self.S.VOICE_CHAINS["natural"]
        self.assertTrue(chain.startswith("highpass=f=85,afftdn"))
        slug = self.project("Voice")
        self.assertTrue(any(chain in c for c in self.commands(slug)), "spoken clips default to natural")
        H.cli(self.home, "timeline", "voice", slug, "all", "off")
        self.assertEqual(self.tl(slug).get("voice"), None)
        H.cli(self.home, "timeline", "voice", slug, "1", "off")
        cmds = self.commands(slug)
        self.assertFalse(any("highpass" in c for c in cmds), "off: no chain")
        H.cli(self.home, "timeline", "voice", slug, "all", "natural")
        self.assertEqual(self.tl(slug)["voice"], "natural")
        self.assertNotIn("voice", self.tl(slug)["clips"][0], "all clears per-clip choices")
        self.assertTrue(any(chain + ",afade" in c for c in self.commands(slug)), "chain before the fades")
        # silent b-roll gets no treatment by default
        roll = self.project("Roll voice", 1.0, 4.0, media=self.r, snap=False)
        self.assertFalse(any("highpass" in c for c in self.commands(roll)))
        r = H.cli(self.home, "timeline", "voice", slug, "1", "loud", check=False)
        self.assertNotEqual(r.returncode, 0)

    # ---- tighten + pace ----
    def test_tighten_shortens_pauses_in_silence_and_keeps_the_words(self):
        regs = self.known["speech"]
        loose = self.project("Loose", 0.5, 12.3)
        tight = self.project("Tight", 0.5, 12.3)
        f0, _ = self.render(loose)
        res0, _ = self.check(loose)
        rev = self.S.state()["projects"][tight]["rev"]
        before = self.tl(tight)
        r = H.cli(self.home, "timeline", "tighten", tight, "all", "--base", rev)
        self.assertIn("2 pauses down to 0.12s", r.stderr)
        clips = self.tl(tight)["clips"]
        self.assertEqual(len(clips), 3)
        real = json.loads((self.home / "media" / self.b / "speech.json").read_text())["regions"]
        for c in clips:          # every cut in silence
            for x in (c["in"], c["out"]):
                self.assertFalse(any(a < x < b for a, b in real), f"cut {x} inside speech")
        for c, d in zip(clips, clips[1:]):
            gap = min(r[0] for r in real if r[0] >= d["in"] - 0.01) - max(r[1] for r in real if r[1] <= c["out"] + 0.01)
            self.assertGreater(gap, 0.35)
            kept = (c["out"] - max(r[1] for r in real if r[1] <= c["out"] + 0.01)) + \
                (min(r[0] for r in real if r[0] >= d["in"] - 0.01) - d["in"])
            self.assertAlmostEqual(kept, 0.12, delta=0.01)
        f1, _ = self.render(tight)
        self.assertLess(H.ffprobe(f1)["duration"], H.ffprobe(f0)["duration"] - 1.0)
        res1, r1 = self.check(tight)
        self.assertEqual(r1["cuts"]["status"], "pass", r1["cuts"])
        self.assertIn("2 pause cuts", r1["cuts"]["detail"])
        self.assertEqual(r1["pauses"]["status"], "pass", r1["pauses"])
        import difflib
        said = lambda res: [w for w in res["heard"].lower().replace(".", "").replace(",", "").split()]
        self.assertGreater(difflib.SequenceMatcher(None, said(res0), said(res1)).ratio(), 0.9,
                           (res0["heard"], res1["heard"]))
        # snap leaves the pause cuts as tightened
        H.cli(self.home, "timeline", "snap", tight, "all")
        self.assertEqual([(c["in"], c["out"]) for c in self.tl(tight)["clips"][1:]][0][0], clips[1]["in"])
        # undo
        H.cli(self.home, "timeline", "set", tight, "-", input=json.dumps(before))
        self.assertEqual(len(self.tl(tight)["clips"]), 1)

    def test_tighten_keeps_cover_on_its_words(self):
        slug = self.project("Tight cover", 0.5, 12.3, captions="off")
        c = self.tl(slug)["clips"][0]
        regs = self.known["speech"]
        at = regs[0][1] - c["in"] - 0.6      # starts late in sentence 1, runs over the pause into 2
        H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.r, 2.0, 4.0, "--at", round(at, 2))
        H.cli(self.home, "timeline", "tighten", slug, "all")
        clips = self.tl(slug)["clips"]
        pieces = [(k, p) for k, c in enumerate(clips) for p in c.get("cover", [])]
        # the b-roll plays on unbroken across the cut: the next part's first footage frame is the
        # one after the last the first part draws
        self.assertEqual([k for k, _ in pieces], [0, 1])
        (_, p0), (_, p1) = pieces
        lib = self.S.state()["media"]
        f0, f1, _, t0 = [x for x in self.S.cover_plan(clips[0], lib)[0] if x[2]][-1]
        self.assertAlmostEqual(t0 + (f1 - f0) / 30, self.S.cover_plan(clips[1], lib)[0][0][3], delta=0.0011)
        self.assertEqual(p1["at"], clips[1]["in"], "picks up at the start of the next piece")
        # ... and still ends under the word it was laid to (the pause it spanned is cut out)
        self.assertAlmostEqual(p1["at"] + p1["out"] - p1["in"], c["in"] + round(at, 2) + 2.0, places=2)
        self.render(slug)
        _, r = self.check(slug)
        self.assertEqual(r["cover short"]["status"], "pass", r["cover short"])

    def test_pace_speeds_the_clip_and_check_uses_its_clock(self):
        slug = self.project("Pace", 0.5, 12.3)
        f0, _ = self.render(slug)
        H.cli(self.home, "timeline", "pace", slug, "all", "1.1")
        self.assertEqual(self.tl(slug)["clips"][0]["pace"], 1.1)
        r = H.cli(self.home, "timeline", "pace", slug, "1", "1.3", check=False)
        self.assertNotEqual(r.returncode, 0, "faster than 1.2 is refused")
        H.cli(self.home, "timeline", "snap", slug, "all")      # lead and tail on the export's clock
        H.cli(self.home, "timeline", "tighten", slug, "all")
        c = self.tl(slug)["clips"][0]
        H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.r, 1.0, "--at", 1.0, "--until-end")
        f1, ev = self.render(slug)
        want = sum((c["out"] - c["in"]) / 1.1 for c in self.tl(slug)["clips"])
        self.assertAlmostEqual(H.ffprobe(f1)["duration"], want, delta=0.12)
        # each clip is a whole number of frames: within half a frame of its length on that clock
        frames = [self.S.clip_frames(c) for c in self.tl(slug)["clips"]]
        self.assertAlmostEqual(sum(frames) / 30, want, delta=len(frames) / 60)
        self.assertEqual(ev["duration"], round(sum(frames) / 30, 2))
        self.assertEqual([x["at"] for x in ev["cuts"] if x["kind"] == "picture"][0], 1.0,
                         "a cover piece's place is on the sped-up clock")
        res, r = self.check(slug)
        for k in ("cuts", "pauses", "captions", "ending", "loudness", "cover short"):
            self.assertEqual(r[k]["status"], "pass", (k, r[k]))
        self.assertTrue(any("atempo=1.1" in c for c in self.commands(slug)))


class CoverPlan(unittest.TestCase):
    """cover_plan: the one frame plan render, the cut list, check and `timeline show` all read
    (pure functions; the cases the cross-model reviews of 2026-11-10 found, and fuzz)."""
    LIB = {"spk": {"duration": 60.0, "has_video": True, "name": "spk"}, "a": {"duration": 2.95, "has_video": True},
           "b": {"duration": 9.0, "has_video": True}, "br": {"duration": 3.0, "has_video": True},
           "m": {"duration": 2.0, "has_video": True}}

    @classmethod
    def setUpClass(cls):
        cls.S = H.import_studio(H.new_home("cover-plan"))

    def piece_frames(self, c, lib=None):
        """[(media, f0, f1, t0, frames of footage the file has from t0)] of the plan's pieces."""
        sp, _ = self.S.cover_plan(c, lib or self.LIB)
        return [(p["media"], f0, f1, t0, int(((lib or self.LIB)[p["media"]]["duration"] - t0) * 30 + 1e-6))
                for f0, f1, p, t0 in sp if p]

    def test_a_piece_never_asks_for_footage_its_file_lacks(self):
        # a 0.067 s gap after a piece whose footage ends exactly at its end: the speaker shows
        # (the old fold gave the gap to the piece and asked 2 frames past the file)
        c = {"id": "c", "media": "spk", "in": 0.0, "out": 6.0,
             "cover": [{"media": "a", "in": 0.0, "out": 2.95, "at": 0.0}, {"media": "b", "in": 0.0, "out": 2.0, "at": 3.0}]}
        for m, f0, f1, t0, have in self.piece_frames(c):
            self.assertLessEqual(f1 - f0, have, m)
        self.assertEqual(self.S.cover_short(c, self.LIB), [])
        # footage to the clip's end 0.05 s short: check and the plan agree the speaker is back
        c = {"id": "c", "media": "spk", "in": 10.0, "out": 10.0 + 4.0 * 1.17, "pace": 1.17,
             "cover": [{"media": "br", "in": 0.0, "out": 4.0, "at": 10.0 + 0.95 * 1.17, "to_end": True}]}
        sp, _ = self.S.cover_plan(c, self.LIB)
        self.assertIsNone(sp[-1][2])
        self.assertTrue(self.S.cover_short(c, self.LIB))
        # a sliver before a piece never shows footage before its IN: it stays the speaker's
        c = {"id": "c", "media": "spk", "in": 0.0, "out": 6.0, "cover": [{"media": "b", "in": 2.0, "out": 4.95, "at": 0.05}]}
        (_, f0, f1, t0, _), = self.piece_frames(c)
        self.assertEqual(f0, 2)
        self.assertAlmostEqual(t0, 2.0 + 2 / 30 - 0.05, places=6)     # frame 2 is 1/60 s after 0.05
        # a 2-frame gap between two pieces: the first runs on while its footage lasts
        c["cover"] = [{"media": "b", "in": 0.0, "out": 1.0, "at": 1.0}, {"media": "b", "in": 5.0, "out": 6.0, "at": 2.06}]
        self.assertEqual([(f0, f1) for _, f0, f1, _, _ in self.piece_frames(c)], [(30, 62), (62, 92)])

    def test_a_three_frame_piece_always_shows(self):
        for pace in (1.0, 1.1, 1.17, 1.2):
            c = {"id": "c", "media": "spk", "in": 0.0, "out": 3.3 * pace, "pace": pace,
                 "cover": [{"media": "b", "in": 0.0, "out": cover_out, "at": 1.1 * pace}]} \
                if (cover_out := round(0.101 * pace, 3)) else None
            self.assertEqual([(f0, f1) for _, f0, f1, _, _ in self.piece_frames(c)], [(33, 36)], pace)
            self.assertEqual(self.S.cover_short(c, self.LIB), [])

    def test_plan_fuzz_whole_frames_and_honest_short(self):
        import random
        rnd = random.Random(11)
        for _ in range(4000):
            pace = rnd.choice([1.0, 1.1, 1.15, 1.17, 1.2])
            a = round(rnd.uniform(0, 20), 3)
            c = {"id": "c", "media": "spk", "in": a, "out": round(a + rnd.uniform(1, 9), 3), "pace": pace, "cover": []}
            lib = dict(self.LIB, x={"duration": round(rnd.uniform(0.5, 6), 3), "has_video": True})
            for _ in range(rnd.randint(1, 4)):
                pin = round(rnd.uniform(0, 3), 3)
                p = {"media": "x", "in": pin, "out": round(pin + rnd.uniform(0.05, 4), 3),
                     "at": round(rnd.uniform(a - 1, c["out"]), 3)}
                if rnd.random() < 0.3:
                    p["to_end"] = True
                c["cover"] = self.S.cover_add(c, p)
            sp, info = self.S.cover_plan(c, lib)
            n = self.S.clip_frames(c)
            self.assertEqual(sp[0][0], 0)
            self.assertEqual(sp[-1][1], n)
            for (x0, x1, _, _), (y0, _, _, _) in zip(sp, sp[1:]):
                self.assertEqual(x1, y0, "end to end")
            for f0, f1, p, t0 in sp:
                self.assertGreater(f1, f0)
                if p:
                    self.assertGreaterEqual(f1 - f0, 3)
                    self.assertGreaterEqual(t0, p["in"] - 1e-9, "never footage before IN")
                    self.assertLessEqual(f1 - f0, int((lib["x"]["duration"] - t0) * 30 + 1e-6), "never past the file")
            for x in info:
                f0, fe = x["laid"]
                if x["short"] is None and fe - f0 >= 3:
                    self.assertTrue(x["shown"] and x["shown"][1] >= fe, x)

    def test_old_stories_render_frame_for_frame_as_before(self):
        """The stored shape is the one the log always had: no migration. Where the old code
        rendered what it was asked (pieces and gaps of 3+ frames, footage that lasts), the plan
        draws the same frames from the same footage. Fuzzed, and every covered clip of the
        scrubbed real log."""
        import random
        old = MigrateCoverFixture.old_spans
        def compare(c, lib):
            pc, nf = self.S.clip_pace(c), self.S.clip_frames(c)    # renders always ran clip_frames frames
            was = [(round(o0 * 30 / pc), nf if o1 >= c["out"] - c["in"] - 1e-9 else round(o1 * 30 / pc), p, o0)
                   for o0, o1, p in old(c)]
            now, _ = self.S.cover_plan(c, lib)
            self.assertEqual([(f0, f1, p is not None) for f0, f1, p, _ in was],
                             [(f0, f1, p is not None) for f0, f1, p, _ in now], c)
            for (f0, _, p, o0), (_, _, q, t0) in zip(was, now):
                if p:
                    src = p["in"] + (c["in"] + o0 - p["at"]) / self.S.clip_pace(c)
                    self.assertAlmostEqual(max(p["in"], src), t0, delta=0.002)
        rnd = random.Random(5)
        n = 0
        while n < 3000:
            pace = rnd.choice([1.0, 1.1, 1.15, 1.17, 1.2])
            a = round(rnd.uniform(0, 20), 3)
            c = {"id": "c", "media": "spk", "in": a, "out": round(a + rnd.uniform(2, 9), 3), "pace": pace, "cover": []}
            t = a + rnd.uniform(0.2, 1)
            for _ in range(rnd.randint(1, 3)):
                ln = rnd.uniform(0.2, 2)
                if t + ln > c["out"] - 0.2:
                    break
                pin = round(rnd.uniform(0, 3), 3)
                c["cover"].append({"media": "b", "in": pin, "out": round(pin + ln, 3), "at": round(t, 3)})
                t += ln + rnd.uniform(0.2, 1)
            if c["cover"]:
                compare(c, self.LIB)
                n += 1
        G = H.ROOT / "test" / "golden" / "fold" / "real.state.json"
        st = json.loads(G.read_text())
        covered = [c for pj in st["projects"].values() for c in pj["timeline"]["clips"] if c.get("cover")]
        self.assertEqual(len(covered), 23)
        for c in covered:
            compare(c, st["media"])

    def test_split_tighten_and_cut_out_move_footage_one_way(self):
        S = self.S
        p = {"media": "b", "in": 2.0, "out": 5.0, "at": 11.0}               # source 11-14 at pace 1.2
        # a split of a clip from 10.0: the left side is 2 s of source = 50 frames at pace 1.2; the
        # piece shows from frame 25 (source 11.0), so its footage reached 2.0 + 25/30 = 2.833
        left, right, notes = S.cover_cut([p], 12.0, 12.0, 1.2, 10.0, self.LIB)    # footage on unbroken
        self.assertEqual((left, right, notes), ([{**p, "out": 3.0}], [{"media": "b", "in": 2.833, "out": 4.833, "at": 12.0}], []))
        left, right, notes = S.cover_cut([p], 12.0, 13.0, 1.2, 10.0, self.LIB)    # a cut out: still unbroken,
        self.assertEqual(right, [{"media": "b", "in": 2.833, "out": 3.833, "at": 13.0}])   # ends on its word
        inside = {"media": "b", "in": 0.0, "out": 0.5, "at": 12.2}
        _, right, notes = S.cover_cut([inside], 12.0, 13.0, 1.2, 10.0, self.LIB)
        self.assertEqual(right, [])
        self.assertIn("removed: it was laid inside the stretch that was cut out", notes[0])
        starts_in = {"media": "b", "in": 1.0, "out": 3.0, "at": 12.5}       # starts in the cut: at the next words
        _, right, _ = S.cover_cut([starts_in], 12.0, 13.0, 1.2, 10.0, self.LIB)
        self.assertEqual(right, [{"media": "b", "in": 1.0, "out": 2.5, "at": 13.0}])
        # a split after the footage ran out: nothing on the right, and it is said
        short = {"media": "m", "in": 1.5, "out": 6.0, "at": 10.0}           # 0.5 s of footage
        left, right, notes = S.cover_cut([short], 12.0, 12.0, 1.0, 10.0, self.LIB)
        self.assertEqual(right, [])
        self.assertIn("footage had run out", notes[0])
        # tighten: a pause with b-roll laid inside it, one spanning it
        c = {"id": "c", "media": "spk", "in": 10.0, "out": 17.0,
             "cover": [{"media": "b", "in": 0.0, "out": 0.6, "at": 12.6}, {"media": "b", "in": 3.0, "out": 6.0, "at": 14.5}]}
        notes = []
        parts = S.tighten_clip(c, [(10.0, 12.0), (14.0, 16.0)], lib=self.LIB, notes=notes)
        self.assertEqual(len(parts), 2)
        self.assertTrue(any("laid inside" in m for m in notes), notes)
        self.assertEqual(sum(len(x.get("cover") or []) for x in parts), 1)

    def test_a_split_hands_footage_on_frame_for_frame(self):
        """Fuzz: split a covered clip anywhere; the right side's first footage frame is the one
        after the left side's last (counted in the frames the plan draws), at any pace."""
        import random
        rnd = random.Random(3)
        for _ in range(2000):
            pace = rnd.choice([1.0, 1.1, 1.15, 1.17, 1.2])
            a = round(rnd.uniform(0, 5), 3)
            c = {"id": "c", "media": "spk", "in": a, "out": round(a + 6, 3), "pace": pace}
            p = {"media": "b", "in": round(rnd.uniform(0, 2), 3), "out": 0, "at": round(a + rnd.uniform(0.3, 1.5), 3)}
            p["out"] = round(p["in"] + 3.5, 3)
            s_ = round(rnd.uniform(p["at"] + 0.4, p["at"] + 3.1), 3)
            left, right, _ = self.S.cover_cut([p], s_, s_, pace, a, self.LIB)
            sl, _ = self.S.cover_plan({**c, "out": s_, "cover": left}, self.LIB)
            sr, _ = self.S.cover_plan({**c, "in": s_, "cover": right}, self.LIB)
            if not (sl[-1][2] and sr[0][2]):
                continue                 # a sliver at the split went to the speaker
            f0, f1, _, t0 = sl[-1]
            self.assertAlmostEqual(t0 + (f1 - f0) / 30, sr[0][3], delta=0.0011)

    def test_normalize_resolves_overlaps_and_prunes(self):
        S = self.S
        c = {"id": "c", "media": "spk", "in": 0.0, "out": 6.0,
             "cover": [{"media": "b", "in": 0.0, "out": 2.0, "at": 1.0}, {"media": "b", "in": 5.0, "out": 7.0, "at": 2.0},
                       {"media": "b", "in": 0.0, "out": 1.0, "at": 7.0}]}
        probs = []
        tl = S.normalize_timeline({"aspect": "9:16", "captions": False, "clips": [c]}, self.LIB, probs)
        cov = tl["clips"][0]["cover"]
        self.assertEqual([(p["at"], p["at"] + p["out"] - p["in"]) for p in cov], [(1.0, 2.0), (2.0, 4.0)])
        self.assertTrue(any("laid outside the clip" in m for _, m in probs))


class MigrateCoverFixture:
    """The old renderer's span code, verbatim (studio.py cover_spans at 4fe9106), to prove the
    stored shape still renders as it did."""

    @staticmethod
    def old_spans(clip, fps=30):
        a, b = clip["in"], clip["out"]
        pace = min(1.2, max(1.0, float(clip.get("pace", 1))))
        d = b - a
        q = lambda t: min(d, max(0.0, round(t * fps / pace) * pace / fps))
        spans, t = [], 0.0
        for p in sorted(clip.get("cover") or [], key=lambda p: p["at"]):
            o0, o1 = q(p["at"] - a), q(p["at"] - a + p["out"] - p["in"])
            o0 = max(o0, t)
            if o1 - o0 <= 0:
                continue
            if o0 > t:
                spans.append([t, o0, None])
            spans.append([o0, o1, p])
            t = o1
        if t < d:
            spans.append([t, d, None])
        out = []
        for s in spans:
            if out and (s[1] - s[0] < 0.1 or (s[2] is None and out[-1][2] is None)):
                out[-1][1] = s[1]
            else:
                out.append(s)
        if len(out) > 1 and out[0][1] - out[0][0] < 0.1:
            first = out.pop(0)
            out[0][0] = first[0]
        return [(round(o0, 4), round(o1, 4), x) for o0, o1, x in out]


@unittest.skipUnless(H.have_tools(*NEEDS), "needs ffmpeg/ffprobe/whisper-cli (run in nix develop)")
class Agents(unittest.TestCase):
    """moments and draft through each built-in agent and a custom command. The agents are
    fakes (test/fakebin, test/fakeagents) that speak the real CLIs' surface: they check their
    arguments, log each call to $FAKE_AGENT_LOG and answer with canned JSON."""

    @classmethod
    def setUpClass(cls):
        cls.home = H.new_home("agents")
        H.cli(cls.home, "ingest", "-p", "shoot", H.fixtures() / "phone-talking.mov")
        cls.S = H.import_studio(cls.home)
        cls.mid = next(iter(cls.S.state()["media"]))

    def setUp(self):
        self.S.HOME = self.home

    def run_agent(self, step, **env):
        log = Path(tempfile.mkstemp(prefix="agent-log-", dir=self.home)[1])
        path = f"{H.TEST_DIR / 'fakeagents'}:{H.TEST_DIR / 'fakebin'}:{os.environ['PATH']}"
        e = H.env_for(self.home, PATH=path, FAKE_AGENT_LOG=str(log), FAKE_AGENT_DELAY="0", **env)
        n = len(H.events(self.home))
        H.cli(self.home, step, self.mid, "--force", env=e)
        evs = [x for x in H.events(self.home)[n:] if x["type"] in (f"step.{step}", "step.failed")]
        calls = [json.loads(l) for l in log.read_text().splitlines()]
        return evs[-1], calls

    def test_each_agent_finds_moments_and_drafts(self):
        media = str((self.home / "media" / self.mid).resolve())
        for agent, env in [("claude", {"STUDIO_AGENT": "claude"}), ("codex", {"STUDIO_AGENT": "codex"}),
                           ("goose", {"STUDIO_AGENT": "goose"}),
                           ("fake-agent", {"STUDIO_AGENT_CMD": "fake-agent --flag"})]:
            with self.subTest(agent=agent):
                ev, calls = self.run_agent("moments", **env)
                self.assertEqual((ev["type"], ev["data"].get("moments")), ("step.moments", 2), ev)
                prov = ev["data"]["provenance"]     # for the choice log: who made these options
                self.assertEqual(prov["provider"], "custom" if agent == "fake-agent" else agent)
                self.assertEqual(prov["prompt"], self.S.prompt_version(self.S.MOMENTS_PROMPT))
                self.assertEqual((prov["task"], prov["brand"]), ("moments", "jefe"))
                if agent == "claude":
                    self.assertEqual(prov["model"], "claude-opus-5-5")
                self.assertEqual(len(calls), 1)
                self.assertEqual(Path(calls[0]["argv"][0]).name, agent)
                self.assertEqual(str(Path(calls[0]["cwd"]).resolve()), media, "runs in the media folder")
                self.assertIn("content strategist", calls[0]["prompt"], "the prompt goes on stdin")
                ev, calls = self.run_agent("draft", **env)
                self.assertEqual((ev["type"], ev["data"].get("posts")), ("step.draft", 2), ev)
                posts = json.loads(H.cli(self.home, "posts", self.mid, "--json").stdout)
                self.assertEqual(len(posts["posts"]), 2)

    def test_command_lines_and_models(self):
        # claude: moments on Opus unless told otherwise; drafts on the agent's default
        _, calls = self.run_agent("moments", STUDIO_AGENT="claude")
        self.assertEqual(calls[0]["argv"][1:], ["-p", "--output-format", "json", "--model",
                                                "claude-opus-5-5", "--allowedTools", "Read"])
        _, calls = self.run_agent("draft", STUDIO_AGENT="claude", STUDIO_CLAUDE_MODEL="old-name")
        self.assertEqual(calls[-1]["argv"][1:], ["-p", "--output-format", "json", "--model", "old-name",
                                                "--allowedTools", "Read"])
        # codex: exec, read-only, prompt on stdin ("-"), last message to a file
        _, calls = self.run_agent("moments", STUDIO_AGENT="codex")
        a = calls[0]["argv"][1:]
        self.assertEqual(a[:7], ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                                 "--color", "never"])
        self.assertEqual((a[-3], a[-1]), ("-o", "-"))
        self.assertNotIn("-m", a, "no claude model id for codex")
        _, calls = self.run_agent("moments", STUDIO_AGENT="codex", STUDIO_MOMENTS_MODEL="gpt-x")
        self.assertEqual(calls[0]["argv"][8:10], ["-m", "gpt-x"])
        # goose
        _, calls = self.run_agent("draft", STUDIO_AGENT="goose", STUDIO_AGENT_MODEL="g-1")
        self.assertEqual(calls[0]["argv"][1:], ["run", "--no-session", "--quiet", "--output-format", "json",
                                                "--model", "g-1", "-i", "-"])
        # a custom command gets the model in its environment; it wins over STUDIO_AGENT
        _, calls = self.run_agent("draft", STUDIO_AGENT="codex", STUDIO_AGENT_CMD="fake-agent",
                                  STUDIO_AGENT_MODEL="m-2")
        self.assertEqual((Path(calls[0]["argv"][0]).name, calls[0]["model_env"]), ("fake-agent", "m-2"))
        # STUDIO_MOMENTS_MODEL="" = no override: the agent's own default
        _, calls = self.run_agent("moments", STUDIO_AGENT="claude", STUDIO_MOMENTS_MODEL="")
        self.assertNotIn("--model", calls[0]["argv"])

    def test_agent_failures_are_step_failures(self):
        ev, _ = self.run_agent("moments", STUDIO_AGENT="codex", FAKE_AGENT_FAIL="1")
        self.assertEqual(ev["type"], "step.failed")
        self.assertIn("not logged in", ev["data"]["error"])
        ev, _ = self.run_agent("moments", STUDIO_AGENT="aider")
        self.assertIn("STUDIO_AGENT_CMD", ev["data"]["error"])
        ev, _ = self.run_agent("moments", STUDIO_AGENT_CMD="no-such-agent-cli")
        self.assertIn("not on PATH", ev["data"]["error"])
        ev, _ = self.run_agent("draft", STUDIO_AGENT_CMD="true")
        self.assertIn("no reply", ev["data"]["error"])

    def test_studio_agent_says_which_and_how(self):
        e = H.env_for(self.home, STUDIO_AGENT="goose", STUDIO_AGENT_MODEL="g-1")
        out = H.cli(self.home, "agent", env=e).stdout
        self.assertIn("agent    goose", out)
        self.assertIn("goose run --no-session --quiet --output-format json --model g-1 -i -", out)
        self.assertIn("you are  cli", out)
        out = H.cli(self.home, "agent", env={**e, "CODEX_THREAD_ID": "t"}).stdout
        self.assertIn("you are  agent", out)
        r = H.cli(self.home, "agent", check=False, env=H.env_for(self.home, STUDIO_AGENT="aider"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STUDIO_AGENT_CMD", r.stderr)


class CutRules(unittest.TestCase):
    """check's cut rules (cut_findings) on made-up frames and sound: what each one catches and
    what it lets through. No ffmpeg."""

    @classmethod
    def setUpClass(cls):
        cls.S = H.import_studio(H.new_home("cutrules"))

    def frames(self, n=90):
        return [[k / 30, 0.001, 100.0] for k in range(n)]

    def cut(self, frame, kind="picture", same=False, clip=1, spoken=(True, True)):
        return {"at": round(frame / 30, 3), "frame": frame, "kind": kind, "clip": clip, "same": same,
                "spoken": spoken}

    def rules(self, frames, cuts=None, samples=None):
        return [(x["rule"], x["frame"]) for x in self.S.cut_findings([tuple(f) for f in frames], cuts, samples)]

    def test_a_clean_cut_passes(self):
        fr = self.frames()
        fr[30][1] = 0.6
        fr[31][1] = 0.15        # a little change the frame after (under CUT_AGAIN): not a second cut
        fr[32][1] = 0.05        # (a moving shot: the frame after the caption isn't still)
        self.assertEqual(self.rules(fr, [self.cut(30)]), [])

    def test_flash_two_frames_between_two_cuts(self):
        fr = self.frames()
        fr[30][1], fr[32][1] = 0.6, 0.5
        self.assertEqual(self.rules(fr, [self.cut(30)]), [("flash", 30)])
        self.assertEqual(self.rules(fr), [("flash", 30)], "no cut list needed")

    def test_stray_near_a_cut_but_not_far_from_every_cut(self):
        fr = self.frames()
        fr[30][1] = fr[40][1] = fr[80][1] = 0.6
        self.assertEqual(self.rules(fr, [self.cut(30)]), [("stray", 40)],
                         "10 frames from a cut is stray; 50 is the footage's own change")

    def test_cuts_are_found_a_few_frames_off(self):
        fr = self.frames()
        fr[32][1] = fr[62][1] = 0.6
        self.assertEqual(self.rules(fr, [self.cut(30), self.cut(60)]), [])

    def test_jump_needs_the_same_media_either_side(self):
        fr = self.frames()
        fr[30][1] = 0.12
        self.assertEqual(self.rules(fr, [self.cut(30, "sound", same=True)]), [("jump", 30)])
        self.assertEqual(self.rules(fr, [self.cut(30, "sound", same=False)]), [])

    def test_soft_first_frame(self):
        fr = self.frames()
        fr[30][1], fr[31][1], fr[32][1] = 0.6, 0.15, 0.01
        self.assertEqual(self.rules(fr, [self.cut(30)]), [("soft", 31)])

    def test_one_frame_pops_not_two(self):
        fr = self.frames()
        fr[20][2] = 4.0
        fr[50][2] = fr[51][2] = 4.0
        fr[70][2] = 250.0
        self.assertEqual(self.rules(fr), [("pop", 20), ("pop", 70)])

    def test_held_frame(self):
        fr = self.frames()
        for f in fr[60:]:
            f[0] += 2 / 30      # frame 59 shows for three frames
        self.assertEqual(self.rules(fr), [("held", 59)])

    def sine(self, n, amp, hz=180, sr=16000):
        import math
        return [int(amp * math.sin(2 * math.pi * hz * k / sr)) for k in range(n)]

    def test_fragment_a_word_cut_either_side(self):
        import array
        import random
        rnd = random.Random(1)
        sr, i0 = 16000, 16000          # the cut at 1.0 s = frame 30
        s = [rnd.randint(-30, 30) for _ in range(3 * sr)]       # room tone
        for k, v in enumerate(self.sine(sr, 8000)):              # a word from 0.5 to 1.5 s, cut at 1.0
            s[sr // 2 + k] += v
        for k in range(i0 - 320, i0 + 320):   # 20 ms fades either side of the join
            s[k] = int(s[k] * abs(k - i0) / 320)
        s = array.array("h", s)
        fr = self.frames()
        fr[30][1] = 0.6
        self.assertEqual(self.rules(fr, [self.cut(30, "sound")], s), [("fragment", 30)])
        # loud sound in a clip nobody speaks in (grinder, music) is that clip's, not a word
        self.assertEqual(self.rules(fr, [self.cut(30, "sound", spoken=(False, False))], s), [])
        self.assertEqual(self.rules(fr, [self.cut(30, "picture")], s), [], "the sound runs on at a picture cut")

    def test_click_at_the_join(self):
        import array
        sr, i0 = 16000, 16000
        s = self.sine(3 * sr, 8000, hz=100)
        for k in range(i0 - 320, i0 + 320):
            s[k] = int(s[k] * abs(k - i0) / 320)
        s[i0] = 4000                    # one sample jumps out of the fade: a click
        s = array.array("h", s)
        fr = self.frames()
        fr[30][1] = 0.6
        self.assertIn(("click", 30), self.rules(fr, [self.cut(30, "sound", spoken=(False, False))], s))

    def test_click_where_a_sound_stops_mid_fade_before_the_cut(self):
        """A loud bed that stops 10 ms before the cut, a third of the way into its fade, then
        padding: the step is there, not at the quietest point (which is the padding)."""
        import array, random
        rnd = random.Random(2)
        sr, i0 = 16000, 16000
        s = self.sine(3 * sr, 8000, hz=100)
        end, fd = i0 - 160, 320                           # sound ends 10 ms early; 20 ms fade
        for k in range(end - fd // 3 * 2, end):             # fade begun, not finished
            s[k] = int(s[k] * (1 - (k - (end - fd // 3 * 2)) / fd))
        for k in range(end, len(s)):
            s[k] = rnd.randint(-2, 2)
        k = max(range(end - 100, end), key=lambda k: abs(s[k]))
        s[end - 1] = s[k]                                   # it stops on a loud sample
        s = array.array("h", s)
        fr = self.frames()
        fr[30][1] = 0.6
        found = self.rules(fr, [self.cut(30, "sound", spoken=(False, False))], s)
        self.assertEqual(found, [("click", 30)])


@unittest.skipUnless(H.have_tools(*NEEDS), "needs ffmpeg/ffprobe/whisper-cli (run in nix develop)")
class Exact(unittest.TestCase):
    """Frame-exact renders. dji.mov carries a timecode (tmcd) track like the DJI footage and
    runs at 29.97; derived.mp4 is footage made outside the studio (no timecode). Both have a
    moving picture so every cut is visible: a white band sliding down (dji) and a yellow box
    sliding right (derived)."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        cls.fx = H.fixtures()
        cls.home = H.new_home("exact")
        work = H.new_home("exact-src")
        ff = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
        subprocess.run(ff + ["-i", str(cls.fx / "bursts.mov"), "-filter_complex",
                             "testsrc2=s=1080x1920:r=30000/1001:d=12[a];color=white:s=1080x300:r=30000/1001:d=12[b];"
                             "[a][b]overlay=x=0:y='t*60'[v]", "-map", "[v]", "-map", "0:a", *enc,
                             "-timecode", "00:00:00;00", str(work / "dji.mov")], check=True)
        subprocess.run(ff + ["-i", str(cls.fx / "broll-roomtone.mov"), "-filter_complex",
                             "color=c=0x3060a0:s=1080x1920:r=30000/1001:d=6[a];color=yellow:s=180x180:r=30000/1001:d=6[b];"
                             "[a][b]overlay=x='t*150':y=300[v]", "-map", "[v]", "-map", "0:a", *enc,
                             str(work / "derived.mp4")], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=4", "-f", "lavfi", "-i",
                             "sine=frequency=100:sample_rate=48000:d=4,volume=4", "-map", "0:v", "-map", "1:a",
                             *enc, str(work / "bed.mov")], check=True)
        streams = [x["codec_type"] for x in json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(work / "dji.mov")],
            capture_output=True, text=True, check=True).stdout)["streams"]]
        assert "data" in streams, "the DJI stand-in needs its timecode track"
        H.cli(cls.home, "ingest", "-p", "x", settled(work / "dji.mov"), settled(work / "derived.mp4"),
              settled(work / "bed.mov"))
        cls.S = H.import_studio(cls.home)
        ids = {it["name"]: m for m, it in cls.S.state()["media"].items()}
        cls.d, cls.r, cls.bed = ids["dji.mov"], ids["derived.mp4"], ids["bed.mov"]

    def setUp(self):
        self.S.HOME = self.home

    def av(self, path):
        """(frame times, sound samples per channel, stream types) of a file."""
        import subprocess
        pts = [float(x.strip(",")) for x in subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "csv=p=0",
             str(path)], capture_output=True, text=True, check=True).stdout.split() if x.strip(",")]
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-f", "s16le", "-ac", "2",
                              "-ar", "48000", "-"], capture_output=True, check=True).stdout
        types = [x["codec_type"] for x in json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, text=True, check=True).stdout)["streams"]]
        return pts, len(raw) // 4, types

    def assert_on_grid(self, pts, n, what):
        self.assertEqual(len(pts), n, f"{what}: {len(pts)} frames, want {n}")
        self.assertAlmostEqual(pts[0], 0.0, delta=1e-3, msg=f"{what}: first frame at {pts[0]}")
        for k, (a, b) in enumerate(zip(pts, pts[1:])):
            self.assertAlmostEqual(b - a, 1 / 30, delta=1e-3, msg=f"{what}: frame {k} held for {b - a:.4f}s")

    def test_parts_are_whole_frames_of_picture_and_sound(self):
        st = self.S.state()
        out = Path(tempfile.mkdtemp(dir=H.tmp_root()))
        cases = [(self.d, 0.62, 4.13, 1.0), (self.d, 5.0, 8.5, 1.1), (self.r, 1.017, 3.3, 1.0), (self.d, 0.0, 0.07, 1.0)]
        for k, (mid, a, b, pace) in enumerate(cases):
            n = self.S.frames_of((b - a) / pace)
            f = out / f"part{k}.mov"
            self.S.render_segment(st["media"][mid], a, b, f, pace=pace, frames=n, part=True)
            pts, samples, types = self.av(f)
            self.assert_on_grid(pts, n, f"part {k}")
            self.assertEqual(samples, n * 1600, f"part {k}: sound is exactly as long as the picture")
            self.assertEqual(types, ["video", "audio"], f"part {k}: no timecode track")
        clip = {"id": "c1", "media": self.d, "in": 0.62, "out": 4.13, "pace": 1.1,
                "cover": [{"media": self.r, "in": 2.0, "out": 3.0, "at": 1.3}]}
        f = out / "covered.mov"
        self.S.render_covered(st, clip, f)
        pts, samples, types = self.av(f)
        n = self.S.clip_frames(clip)
        self.assert_on_grid(pts, n, "covered part")
        self.assertEqual(samples, n * 1600)
        self.assertEqual(types, ["video", "audio"])

    def test_a_clip_rounded_up_fades_out_before_its_padding(self):
        """A loud bed (100 Hz at half scale) on a clip whose length rounds up to whole frames:
        the fade out ends where the sound does, so the part never stops mid-fade (a step). Both
        cases end a quarter period into the tone: at its peak, where a cut-off fade steps most."""
        import array, subprocess
        st = self.S.state()
        out = Path(tempfile.mkdtemp(dir=H.tmp_root()))
        for k, (a, b, pace) in enumerate([(0.5, 1.5225, 1.0), (0.2025, 3.7125, 1.2)]):
            n = self.S.frames_of((b - a) / pace)
            self.assertGreater(n / 30, (b - a) / pace + 0.005, "the case must round up")
            f = out / f"bed{k}.mov"
            self.S.render_segment(st["media"][self.bed], a, b, f, pace=pace, frames=n, part=True, voice="off")
            raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(f), "-vn", "-ac", "1",
                                  "-f", "s16le", "-"], capture_output=True, check=True).stdout
            x = array.array("h", raw)
            self.assertEqual(len(x), n * 1600)
            tail = x[-int(48000 * 0.02):]
            step = max(abs(tail[i] - tail[i - 1]) for i in range(1, len(tail))) / 32768
            self.assertLess(step, 0.01, f"case {k}: a {step:.3f} step in the last 20 ms")
            self.assertGreater(max(abs(v) for v in x[: len(x) // 2]), 10000, "the bed is loud")

    def story(self, name):
        slug = H.cli(self.home, "project", "new", name).stdout.strip().splitlines()[-1]
        for args in ((self.d, 0.62, 4.13), (self.r, 1.017, 3.3), (self.d, 5.0, 8.5), (self.d, 9.1, 11.95)):
            H.cli(self.home, "timeline", "add", slug, *args)
        H.cli(self.home, "timeline", "pace", slug, "3", "1.1")
        c4 = self.S.state()["projects"][slug]["timeline"]["clips"][3]["id"]
        H.cli(self.home, "timeline", "cover", slug, c4, "--with", self.r, 2.0, 3.0, "--at", 0.5)
        return slug, c4

    def render(self, slug):
        H.cli(self.home, "render", slug, timeout=900)
        ev = [e for e in H.events(self.home) if e["type"] == "render.done" and e["data"]["project"] == slug][-1]
        return self.home / ev["data"]["file"], ev["data"]

    def check(self, slug):
        r = H.cli(self.home, "check", slug, "--json", check=False, timeout=900)
        self.assertIn(r.returncode, (0, 1), r.stderr[-2000:])
        res = json.loads(r.stdout)
        return res, {x["rule"]: x for x in res["rules"]}

    def test_mixed_sources_join_losslessly_on_their_frames(self):
        slug, c4 = self.story("Mixed")
        f, ev = self.render(slug)
        self.assertEqual(ev["concat"], "copy", "timecode on some sources must not force a re-encode")
        tl = ev["timeline"]
        n = sum(self.S.clip_frames(c) for c in tl["clips"])
        self.assertEqual(ev["duration"], round(n / 30, 2))
        pts, samples, types = self.av(f)
        self.assertEqual(types, ["video", "audio"], "no timecode track in the export")
        self.assert_on_grid(pts, n, "export")
        self.assertAlmostEqual(samples / 48000, n / 30, delta=0.03, msg="sound as long as the picture")
        # every cut is exactly on its frame: the picture changes there and nowhere near it
        _, _, frames = self.S.scan_picture(f)
        cuts = self.S.cut_list(tl, self.S.state()["media"])
        self.assertEqual([(x["kind"], x["same"]) for x in cuts],
                         [("sound", False), ("sound", False), ("sound", True), ("picture", False), ("picture", False)])
        for x in cuts:
            k = x["frame"]
            scores = [round(frames[i][1], 3) for i in range(k - 2, k + 3)]
            if not x["same"]:
                self.assertGreaterEqual(scores[2], 0.3, f"no cut on frame {k}: {scores}")
            self.assertTrue(all(s < 0.1 for s in scores[:2] + scores[3:]), f"around frame {k}: {scores}")
        res, r = self.check(slug)
        for k in ("flash", "stray", "pop", "held", "soft", "fragment", "click"):
            self.assertEqual(r[k]["status"], "pass", r[k])
        # the 3 -> 4 cut is the same shot, 0.6 s later: a jump cut, on clip 4
        self.assertEqual(r["jump"]["status"], "fail", r["jump"])
        self.assertEqual(len(r["jump"]["items"]), 1)
        self.assertIn(f"{cuts[2]['at']:.2f}s", r["jump"]["items"][0])
        self.assertIn("jump", res["failed"])
        fails = [e for e in H.events(self.home) if e["type"] == "check.done"][-1]["data"]["failures"]
        self.assertEqual([x["clip"] for x in fails if x["rule"] == "jump"], [c4])
        self.assertTrue(any(i.startswith("jump-f") for i in res["images"]), "a tile of the jump on the sheet")
        # a jump cut can be meant: excepted on that clip, with a reason
        H.cli(self.home, "exception", "grant", slug, "jump", c4, "--why", "the beat skips ahead on purpose")
        res, r = self.check(slug)
        self.assertEqual(r["jump"]["status"], "excepted", r["jump"])
        # ... a flash, a stray frame, a clipped word or a click never is
        for rule in ("flash", "stray", "fragment", "click"):
            x = H.cli(self.home, "exception", "grant", slug, rule, c4, "--why", "it looks fine to me", check=False)
            self.assertNotEqual(x.returncode, 0, rule)
        # hidden: the derived picture over the start of clip 4, so either side of the cut differs
        H.cli(self.home, "timeline", "uncover", slug, c4, "all")
        H.cli(self.home, "timeline", "cover", slug, c4, "--with", self.r, 4.0, 4.4, "--at", 0)
        self.render(slug)
        res, r = self.check(slug)
        self.assertEqual(r["jump"]["status"], "pass", r["jump"])
        self.assertEqual(r["held"]["status"], "pass", r["held"])

    def test_a_join_that_must_reencode_cuts_on_key_frames(self):
        """If the parts can't be joined as they are, the re-encode puts a key frame on every
        part's first frame (no cut lands on a predicted frame)."""
        slug, _ = self.story("Reencode")
        orig = self.S.stream_sig
        self.S.stream_sig = lambda p: str(p)      # every part looks different
        try:
            f = self.S.render(slug)
        finally:
            self.S.stream_sig = orig
        import subprocess
        tl = self.S.state()["projects"][slug]["exports"][-1]
        self.assertEqual(tl["concat"], "reencode")
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "frame=key_frame", "-of", "csv=p=0", str(f)], capture_output=True, text=True,
                             check=True).stdout.split()
        keys = {k for k, x in enumerate(out) if x.strip(",") == "1"}
        starts = [x["frame"] for x in self.S.cut_list(tl["timeline"], self.S.state()["media"]) if x["kind"] == "sound"]
        self.assertTrue(set(starts) <= keys, f"cuts {starts}, key frames {sorted(keys)}")
        pts, _, _ = self.av(f)
        self.assert_on_grid(pts, sum(self.S.clip_frames(c) for c in tl["timeline"]["clips"]), "re-encoded export")


class Golden(unittest.TestCase):
    """docs/events.md + test/golden/fold: the contract a port is held to (plan task 0.2)."""
    G = H.ROOT / "test" / "golden" / "fold"

    def test_golden_fold_is_current_and_reproducible(self):
        """make-golden --check refolds both logs and rebuilds the seeded one in a temp dir:
        byte-identical to what is committed (so: deterministic, and the fold didn't drift)."""
        r = subprocess.run(["bash", str(H.TEST_DIR / "make-golden.sh"), "--check"],
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    @unittest.skipUnless(H.have_tools("ffmpeg", "ffprobe", "whisper-cli"), "needs ffmpeg/whisper-cli (nix develop)")
    def test_cli_contract(self):
        """test/cli_contract.sh: every AGENTS.md line and a real line for every verb, as goldens (task 0.3)."""
        r = subprocess.run(["bash", str(H.TEST_DIR / "cli_contract.sh")], capture_output=True, text=True,
                           timeout=900, env={k: v for k, v in os.environ.items() if k not in H.AGENT_ENV})
        self.assertEqual(r.returncode, 0, r.stdout[-4000:] + r.stderr[-2000:])

    def test_state_json_is_the_fold(self):
        home = H.new_home("state-json")
        shutil.copy(self.G / "seeded.events.jsonl", home / "events.jsonl")
        out = H.cli(home, "state", "--json").stdout
        self.assertEqual(json.loads(out), json.loads(json.dumps(H.import_studio(home).state())))
        self.assertEqual(out, json.dumps(json.loads(out), sort_keys=True, indent=1, ensure_ascii=False) + "\n")

    def test_every_logged_type_is_in_the_contract(self):
        doc = (H.ROOT / "docs" / "events.md").read_text()
        for f in ("real.events.jsonl", "seeded.events.jsonl"):
            types = {json.loads(l)["type"] for l in (self.G / f).read_text().splitlines() if l.strip()}
            self.assertEqual([t for t in sorted(types) if f"`{t}`" not in doc], [], f)

    def test_seeded_fixture_covers_the_v2_shapes(self):
        st = json.loads((self.G / "seeded.state.json").read_text())
        a, b = st["projects"]["sparks-opener"], st["projects"]["paperwork-reel"]
        self.assertEqual([e["verdict"]["state"] for e in a["exports"]], ["failed", "ready"])
        self.assertTrue(a["ready"])
        self.assertTrue(any(x.get("revoked") for x in a["exceptions"].values()))
        self.assertTrue(any(x["actor"] == "agent" and not x.get("revoked") for x in a["exceptions"].values()))
        self.assertEqual(len(a["rounds"]), 1)
        self.assertEqual(b["shelved"]["actor"], "operator")
        self.assertEqual(b["exports"][0]["verdict"]["state"], "unchecked")

    def test_scrub_is_keyed_and_field_level(self):
        """test/scrub.py: no key, no scrub; with one, nothing under a structural key ships verbatim
        (a file name inside note.focus.media, an export name in choice.shown), digits in free text
        are scrubbed, and the placeholders change with the key."""
        d = Path(tempfile.mkdtemp(prefix="studio-scrub-", dir=H.tmp_root()))
        sha = "ab" * 32
        evs = [{"ts": "2026-11-09T10:00:00-08:00", "type": "ingested", "media": sha[:12],
                "data": {"name": "IMG_0001.MOV", "sha256": sha, "path": "/Users/x/Studio/media/a/source.mov",
                         "project": "jefe-paperwork", "duration": 3.0}},
               {"ts": "2026-11-09T10:00:01-08:00", "type": "note", "media": None,
                "data": {"text": "call 555 0199 about 12 Example Rd", "id": "0a1b2c3d", "actor": "operator",
                         "focus": {"media": {"id": sha[:12], "name": "IMG_0001.MOV"}}}},
               {"ts": "2026-11-09T10:00:02-08:00", "type": "choice", "media": None,
                "data": {"project": "jefe-paperwork", "kind": "version", "actor": "agent",
                         "shown": [sha, "jefe-paperwork-20261109-120000-9x16.mp4"], "chosen": sha}}]
        src = d / "in.jsonl"
        src.write_text("".join(json.dumps(e) + "\n" for e in evs))
        env = {k: v for k, v in os.environ.items() if k != "STUDIO_SCRUB_KEY"} | {"HOME": str(d)}
        run = lambda out, **e: subprocess.run([sys.executable, str(H.TEST_DIR / "scrub.py"), str(src), str(d / out)],
                                              capture_output=True, text=True, env=env | e)
        self.assertNotEqual(run("nokey.jsonl").returncode, 0, "scrubbed without a key")
        self.assertFalse((d / "nokey.jsonl").exists())
        r1, r2 = run("k1.jsonl", STUDIO_SCRUB_KEY="1" * 64), run("k2.jsonl", STUDIO_SCRUB_KEY="2" * 64)
        self.assertEqual((r1.returncode, r2.returncode), (0, 0), r1.stderr + r2.stderr)
        out = (d / "k1.jsonl").read_text()
        for word in ("IMG", "0001", "MOV", "jefe-paperwork", "Example", "12 ", "555", "0199", "120000", "Users"):
            self.assertNotIn(word, out)
        a = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(a[0]["data"]["project"], a[2]["data"]["project"], "one word map for the whole log")
        self.assertEqual(a[0]["media"], a[0]["data"]["sha256"][:12], "a media id stays its sha256's prefix")
        self.assertNotEqual(a[0]["data"]["sha256"], sha)
        self.assertEqual(a[2]["data"]["chosen"], a[0]["data"]["sha256"])
        self.assertEqual((a[2]["data"]["kind"], a[2]["data"]["actor"]), ("version", "agent"))
        self.assertTrue(all(len(t) == 6 and t.islower() for t in a[1]["data"]["text"].split()))
        self.assertNotEqual(out, (d / "k2.jsonl").read_text(), "placeholders must depend on the key")

    def test_scrubbed_log_has_no_free_text(self):
        """Every note in the real fixture is placeholder text: consonant tokens, punctuation and
        (mapped) hex ids the note mentions."""
        notes = [json.loads(l)["data"]["text"] for l in (self.G / "real.events.jsonl").read_text().splitlines()
                 if '"type": "note"' in l]
        self.assertTrue(notes)
        for t in notes:
            self.assertNotRegex(re.sub(r"\b[0-9a-f]{8,64}\b", "", t).lower(), r"[aeiouy0-9]", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
