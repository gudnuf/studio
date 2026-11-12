#!/usr/bin/env python3
"""The UI and the CLI move b-roll the same way: ui/ops.js coverCut against studio.py cover_cut
(split, tighten and "Cut this out") on a few thousand seeded cases. Values are drawn on the
frame grid, on half frames, at 3 decimals and anywhere, at every pace, with pieces before,
across, inside and after the cut and to the clip's end, so a change to either side's rounding
(round half even, the 1e-6, 3 decimals, max(in, ...)) or rule shows up here. Exit 1 on any
difference. Needs node (SKIP without it).

And they chunk captions the same way: ui/captions.js captionChunks against studio.py
caption_chunks on seeded transcripts (words on and off the clip's edges, zero-length words,
punctuation, segment breaks, every pace), so the preview shows the chunks the export burns.
(The export then holds each chunk to the speech it hears, caption_gate: Python only.)"""
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

def caption_cases(seed=20261111, n=N):
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        t, words = rnd.uniform(0, 3), []
        for _ in range(rnd.randint(0, 24)):
            t += rnd.choice([0.0, 0.0, 0.02, rnd.uniform(0, 0.6), rnd.uniform(0.3, 1.5)])
            d = rnd.choice([0.0, 0.05, rnd.uniform(0.05, 0.7)])
            w = rnd.choice(["a", "word", "Jefe", "it's"]) + rnd.choice(["", "", "", ",", ".", "?", "!", '."', ".)"])
            words.append({"w": w, "start": round(t, rnd.choice([2, 3])), "end": round(t + d, rnd.choice([2, 3]))})
            t += d
        segs = [{"start": w["start"]} for w in words if rnd.random() < 0.2]
        edge = lambda: rnd.choice([w[k] for w in words for k in ("start", "end")]) if words and rnd.random() < 0.5 \
            else round(rnd.uniform(0, t + 1), 3)
        a, b = sorted((edge(), edge()))
        out.append([words, segs, a, b + rnd.choice([0, 0.3, 1.0]), rnd.choice([1.0, 1.0, 1.1, 1.15, 1.2])])
    return out

def main():
    node = shutil.which("node")
    if not node:
        print("SKIP cover parity: node not on PATH")
        return 0
    cs = cases()
    caps = caption_cases()
    r = subprocess.run([node, str(here / "cover_parity.mjs")], input=json.dumps({"cases": cs, "captions": caps}),
                       capture_output=True, text=True, check=True)
    out = json.loads(r.stdout)
    js = out["cover"]
    py = json.loads(json.dumps([list(S.cover_cut(*c)[:2]) for c in cs]))
    bad = [i for i, (a, b) in enumerate(zip(js, py)) if a != b]
    for i in bad[:5]:
        print(f"DIFF case {i}: {json.dumps(cs[i])}\n  js {json.dumps(js[i])}\n  py {json.dumps(py[i])}")
    cjs = out["captions"]
    cpy = json.loads(json.dumps([S.caption_chunks(*c) for c in caps]))
    cbad = [i for i, (a, b) in enumerate(zip(cjs, cpy)) if a != b]
    for i in cbad[:5]:
        print(f"DIFF caption case {i}: {json.dumps(caps[i])}\n  js {json.dumps(cjs[i])}\n  py {json.dumps(cpy[i])}")
    print(f"cover parity: {len(cs) - len(bad)}/{len(cs)} cases agree; "
          f"caption parity: {len(caps) - len(cbad)}/{len(caps)} ({sum(map(len, cpy))} chunks)")
    return 1 if bad or cbad or len(js) != len(cs) or len(cjs) != len(caps) else 0

if __name__ == "__main__":
    sys.exit(main())
