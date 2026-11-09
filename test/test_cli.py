"""CLI contract tests for studio.py. Run inside the devshell:

    cd forge/studio && nix develop -c python3 -m unittest test.test_cli -v
    (or test/run_all.sh)

One shared temp STUDIO_HOME gets all fixtures ingested once (that runs the real proxy /
whisper / keyframes steps); each test then works in its own project. The CLI tests never
call an agent: moments.json is written by hand and step.moments emitted through the studio
module. The Agents tests run moments/draft through fake agents (test/fakeagents).
"""
import json, os, shutil, sys, tempfile, time, unittest
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

    def test_ingest_into_other_project_moves(self):
        slug = self.project("Move Target")
        H.cli(self.home, "ingest", "-p", slug, self.fx / "voice-memo.m4a")
        self.assertEqual(self.st()["media"][self.mid("voice-memo.m4a")]["project"], slug)
        H.cli(self.home, "ingest", "-p", "shoot", self.fx / "voice-memo.m4a")
        self.assertEqual(self.st()["media"][self.mid("voice-memo.m4a")]["project"], "shoot")

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

    def test_new_project_starts_at_rev_0(self):
        """S3: a never-saved project has rev 0 (so the UI's first save carries ?base=0)."""
        slug = self.project("Never Saved")
        self.assertEqual(self.st()["projects"][slug]["rev"], 0)
        self.assertEqual(json.loads(H.cli(self.home, "timeline", "show", "--json", slug).stdout)["rev"], 0)
        H.cli(self.home, "timeline", "add", slug, self.mid("site-walk.mov"), "0", "2", as_agent=True)
        r = H.cli(self.home, "timeline", "clear", slug, "--base", "0", check=False)
        self.assertEqual(r.returncode, 3, "an edit based on rev 0 must not overwrite the agent's save")
        self.assertEqual(len(self.tl(slug)["clips"]), 1)

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
        # the agent grants its own, with reasons; the picture is declared, not guessed
        why = "the beat after 'hours' is a reveal; the picture carries the held silence"
        H.cli(self.home, "exception", "grant", slug, "pauses", c2, "--why", why, as_agent=True)
        H.cli(self.home, "exception", "grant", slug, "length", "--why", "a test reel: short on purpose", as_agent=True)
        H.cli(self.home, "timeline", "onscreen", slug, c1, "3 hours of sprinklers")
        H.cli(self.home, "timeline", "card", slug, c2, "on")
        g = [e["data"] for e in H.events(self.home) if e["type"] == "exception.granted"]
        self.assertEqual([(x["rule"], x["clip"], x["actor"], x["brand"]) for x in g],
                         [("pauses", c2, "agent", "jefe"), ("length", None, "agent", "jefe")])
        H.cli(self.home, "render", slug, timeout=900)
        self.assertEqual(H.cli(self.home, "ready", slug, check=False).returncode, 1, "a re-render starts unchecked")
        r2 = H.cli(self.home, "check", slug, timeout=900)          # exit 0: every failure excepted
        self.assertIn("EXCEPTION", r2.stdout)
        self.assertIn(why, r2.stdout)
        res, r = self.check(slug)
        self.assertTrue(res["ok"], res["failed"])
        self.assertEqual(r["first word"]["status"], "pass", r["first word"])
        self.assertIn("on-screen text at 0.00s", r["first word"]["detail"])
        self.assertEqual(r["picture"]["status"], "pass", "a designed card is not black")
        self.assertEqual(r["pauses"]["status"], "excepted")
        self.assertEqual(r["pauses"]["excepted"][0]["why"], why)
        self.assertEqual({x["rule"] for x in res["excepted"]}, {"pauses", "length"})
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
        # blind: shuffled copies labelled A/B, the mapping in the log, the pick mapped back
        out = H.cli(self.home, "ab", slug).stdout
        rnd = [e["data"] for e in H.events(self.home) if e["type"] == "blind.round"][-1]
        self.assertEqual(set(rnd["labels"].values()), {a, b})
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
        self.assertIn("cover 1: 2.0–5.0s", H.cli(self.home, "timeline", "show", covered).stdout)
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
        self.assertEqual(self.S.cut_list(old), [])
        self.assertEqual(self.S.cover_spans(c), [(0.0, 3.4, None)])
        # no voice, no pace: the filter is the fades alone, the picture has no setpts
        self.assertEqual(self.S.sound_filter(3.4, "off"), "afade=t=in:d=0.020,afade=t=out:st=3.380:d=0.020")
        # a cover piece pointing at missing footage is dropped with a warning, never the clip
        probs = []
        tl = self.S.normalize_timeline({**old, "clips": [{**c, "cover": [{"media": "nope", "in": 0, "out": 1, "at": 1}]}]},
                                       lib, probs)
        self.assertNotIn("cover", tl["clips"][0])
        self.assertEqual([k for k, _ in probs], ["cover"])

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
        # the b-roll plays on unbroken across the cut: where one piece stops the next starts
        self.assertEqual([k for k, _ in pieces], [0, 1])
        (_, p0), (_, p1) = pieces
        self.assertAlmostEqual(p0["out"], p1["in"], places=3)
        self.assertEqual(p1["at"], clips[1]["in"], "picks up at the start of the next piece")
        self.assertAlmostEqual((p0["out"] - p0["in"]) + (p1["out"] - p1["in"]), 2.0, places=2)
        self.render(slug)

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
        H.cli(self.home, "timeline", "cover", slug, c["id"], "--with", self.r, 1.0, 3.2, "--at", 1.1)
        f1, ev = self.render(slug)
        want = sum((c["out"] - c["in"]) / 1.1 for c in self.tl(slug)["clips"])
        self.assertAlmostEqual(H.ffprobe(f1)["duration"], want, delta=0.12)
        self.assertAlmostEqual(ev["duration"], want, delta=0.01)
        self.assertEqual([x["at"] for x in ev["cuts"] if x["kind"] == "picture"][0], 1.0,
                         "a cover piece's place is on the sped-up clock")
        res, r = self.check(slug)
        for k in ("cuts", "pauses", "captions", "ending", "loudness"):
            self.assertEqual(r[k]["status"], "pass", (k, r[k]))
        self.assertTrue(any("atempo=1.1" in c for c in self.commands(slug)))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
