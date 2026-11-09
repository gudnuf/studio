#!/usr/bin/env python3
"""Seed a throwaway STUDIO_HOME for UI work: fixtures ingested (real proxy/whisper/keyframes),
hand-written moments + drafts (no claude -p), and a "jefe-shoot" project with a 4-clip story.

    nix develop -c python3 test/seed.py /path/to/home [--fixtures DIR] [--bare]

--bare: only create the empty home (for testing the first-run / drop-footage path).
Then: STUDIO_HOME=/path/to/home ./studio ui --port 7450 --no-open
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("home")
    ap.add_argument("--fixtures")
    ap.add_argument("--bare", action="store_true")
    a = ap.parse_args()
    home = Path(a.home).resolve()
    if home == H.REAL_HOME.resolve():
        sys.exit("refusing to seed ~/Studio")
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
