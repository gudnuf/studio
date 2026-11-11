#!/usr/bin/env python3
"""The UI and the CLI move b-roll the same way: ui/ops.js coverCut against studio.py cover_cut
(split, tighten and "Cut this out") on a few thousand seeded cases. Values are drawn on the
frame grid, on half frames, at 3 decimals and anywhere, at every pace, with pieces before,
across, inside and after the cut and to the clip's end, so a change to either side's rounding
(round half even, the 1e-6, 3 decimals, max(in, ...)) or rule shows up here. Exit 1 on any
difference. Needs node (SKIP without it)."""
import json, random, shutil, subprocess, sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))
import studio as S                                            # noqa: E402

N = 4000

def value(rnd, lo, hi, pace, base=0.0):
    """A time between lo and hi: on the clip's frame grid (from base), exactly on a half frame
    (the rounding ties), at 3 decimals, or anywhere."""
    kind = rnd.randrange(5)
    x = rnd.uniform(lo, hi)
    k = round((x - base) * 30 / pace)
    if kind == 0:
        return round(base + k * pace / 30, 3)
    if kind == 1:
        return base + (k + 0.5) * pace / 30
    if kind == 2:
        return round(base + (k + 0.5) * pace / 30, 3)
    if kind == 3:
        return round(x, 3)
    return round(x, 6)

def cases(seed=20261110):
    rnd = random.Random(seed)
    out = []
    for _ in range(N):
        pace = rnd.choice([1.0, 1.1, 1.15, 1.17, 1.2])
        cin = round(rnd.uniform(0, 30), 3)
        cover = []
        for _ in range(rnd.randint(1, 4)):
            pin = value(rnd, 0, 6, 1.0)
            p = {"media": "m", "in": pin, "out": round(pin + rnd.uniform(0.05, 4), 3),
                 "at": value(rnd, cin - 1, cin + 8, pace, cin)}
            if rnd.random() < 0.25:
                p["to_end"] = True
            cover.append(p)
        s1 = value(rnd, cin + 0.1, cin + 8, pace, cin)
        s2 = s1 if rnd.random() < 0.5 else value(rnd, s1, s1 + 2, pace, cin)
        out.append([cover, s1, max(s1, s2), pace, cin])
    return out

def main():
    node = shutil.which("node")
    if not node:
        print("SKIP cover parity: node not on PATH")
        return 0
    cs = cases()
    r = subprocess.run([node, str(here / "cover_parity.mjs")], input=json.dumps({"cases": cs}),
                       capture_output=True, text=True, check=True)
    js = json.loads(r.stdout)
    py = json.loads(json.dumps([list(S.cover_cut(*c)[:2]) for c in cs]))
    bad = [i for i, (a, b) in enumerate(zip(js, py)) if a != b]
    for i in bad[:5]:
        print(f"DIFF case {i}: {json.dumps(cs[i])}\n  js {json.dumps(js[i])}\n  py {json.dumps(py[i])}")
    print(f"cover parity: {len(cs) - len(bad)}/{len(cs)} cases agree")
    return 1 if bad or len(js) != len(cs) else 0

if __name__ == "__main__":
    sys.exit(main())
