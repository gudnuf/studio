#!/usr/bin/env python3
"""Render the Jefe UI mocks to H.264, deterministically, from a JSON of line items.

    cd forge/studio && nix develop -c python3 mocks/jefe/render.py                 # everything
    nix develop -c python3 mocks/jefe/render.py --data mocks/jefe/data/other.json --scenes walk,estimate
    nix develop -c python3 mocks/jefe/render.py --stills-only

Headless Chrome (via studio/test/cdp.py) loads html/index.html at 393x852 CSS px with a 3x device
scale, calls JefeMock.renderAt(t) for t = 0, 1/fps, 2/fps ... and screenshots each frame. The page
has no timers or transitions, so frame N is always the same pixels. Frames are piped straight into
ffmpeg. 3x of 393 is 1179, which H.264 4:2:0 can't take (odd width), so the right-most pixel
column is cropped: 1178x2556.

Outputs (default mocks/jefe/out/): jefe-<scene>.mp4, stills/hero-*.png (full 1179x2556),
contact/jefe-<scene>.jpg (12 frames, evenly spaced, for a quick look).
"""
import argparse, base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "test"))
import cdp  # noqa: E402  (studio/test/cdp.py: stdlib CDP client)

SCENES = ["walk", "notes", "chooser", "estimate", "send"]
# (name, scene, t): stills for overlays.
STILLS = [
    ("hero-walk", "walk", 10.95),        # every item captured, transcript live
    ("hero-chooser", "chooser", 0.5),    # notes + TURN THESE NOTES INTO
    ("hero-estimate", "estimate", 9.4),  # lines, TOTAL + GAP chip, gap note
    ("hero-gap-check", "send", 3.0),     # "5 lines have no price on them. Send anyway?"
]


def launch(port, profile, scale):
    proc = subprocess.Popen([cdp.CHROME, "--headless=new", f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--window-size=393,852", "--hide-scrollbars",
                             "--force-color-profile=srgb", "--font-render-hinting=none",
                             "--no-first-run", "--no-default-browser-check", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
            page = next(t for t in tabs if t["type"] == "page")
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise SystemExit("chrome did not come up")
    c = cdp.CDP.__new__(cdp.CDP)
    c.proc, c.ws, c.n, c.events = proc, cdp.WS(page["webSocketDebuggerUrl"]), 0, []
    c.call("Runtime.enable"); c.call("Page.enable")
    c.call("Emulation.setDeviceMetricsOverride", width=393, height=852, deviceScaleFactor=scale, mobile=True)
    return c


def load(c, data, scene):
    url = (HERE / "html" / "index.html").as_uri()
    c.call("Page.navigate", url=url)
    for _ in range(100):
        if c.js("typeof JefeMock !== 'undefined' && document.readyState === 'complete'"):
            break
        time.sleep(0.05)
    return c.js(f"JefeMock.boot({json.dumps(data)}, {json.dumps(scene)})")


def frame(c, t):
    c.js(f"JefeMock.renderAt({t:.6f})")
    r = c.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
    errs = c.errors() if hasattr(c, "errors") else []
    if errs:
        raise RuntimeError("page errors: " + "; ".join(errs[:3]))
    return base64.b64decode(r["data"])


def encode(c, data, scene, fps, out):
    dur = load(c, data, scene)
    n = int(round(dur * fps))
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
                           "-i", "-", "-vf", "crop=iw-mod(iw\\,2):ih-mod(ih\\,2):0:0,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                           "-c:v", "libx264", "-preset", "slow", "-crf", "14", "-tune", "animation",
                           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                           "-r", str(fps), "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    t0 = time.time()
    for i in range(n):
        ff.stdin.write(frame(c, i / fps))
        if i % 60 == 0:
            print(f"  {scene}: frame {i}/{n}  ({time.time() - t0:.0f}s)", flush=True)
    ff.stdin.close()
    if ff.wait():
        raise SystemExit(f"ffmpeg failed for {scene}")
    print(f"  wrote {out}  ({n} frames, {dur}s @ {fps}fps)")


def contact(mp4, jpg, dur):
    jpg.parent.mkdir(parents=True, exist_ok=True)
    cols, rows = 6, 2
    k = cols * rows
    # 12 evenly spaced frames, each 196px wide, labelled with its timestamp.
    with tempfile.TemporaryDirectory() as td:
        for i in range(k):
            ts = dur * (i + 0.5) / k
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ts:.3f}", "-i", str(mp4), "-frames:v", "1",
                            "-vf", f"scale=294:-2,drawtext=fontfile={HERE / 'html/fonts/IBMPlexMono-SemiBold.ttf'}:text='{ts:.1f}s':x=8:y=8:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6",
                            f"{td}/f{i:02d}.png"], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "1", "-i", f"{td}/f%02d.png",
                        "-vf", f"tile={cols}x{rows}:padding=6:color=0x222222", "-frames:v", "1", "-q:v", "3", str(jpg)], check=True)
    print(f"  contact sheet {jpg}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data" / "bmw-1602.json"), help="line-items JSON")
    ap.add_argument("--scenes", default=",".join(SCENES))
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--scale", type=float, default=3, help="device pixel ratio (3 = iPhone native)")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--prefix", default="jefe")
    ap.add_argument("--no-taps", action="store_true", help="hide the finger dots")
    ap.add_argument("--no-island", action="store_true", help="no Dynamic Island (for a device frame)")
    ap.add_argument("--stills-only", action="store_true")
    ap.add_argument("--no-stills", action="store_true")
    ap.add_argument("--frame", nargs=2, action="append", metavar=("SCENE", "T"), help="write one PNG and exit (repeatable)")
    ap.add_argument("--port", type=int, default=9341)
    a = ap.parse_args()

    data = json.loads(Path(a.data).read_text())
    data["showTaps"] = not a.no_taps
    data["island"] = not a.no_island
    out = Path(a.out); (out / "stills").mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="jefe-mock-chrome-")
    c = launch(a.port, profile, a.scale)
    try:
        if a.frame:
            for scene, t in a.frame:
                load(c, data, scene)
                p = out / "frames" / f"frame-{scene}-{float(t):.2f}.png"
                p.parent.mkdir(exist_ok=True)
                p.write_bytes(frame(c, float(t)))
                print(p)
            return
        if not a.no_stills:
            for name, scene, t in STILLS:
                load(c, data, scene)
                p = out / "stills" / f"{a.prefix}-{name}.png"
                p.write_bytes(frame(c, t))
                print(f"  still {p}")
        if a.stills_only:
            return
        for scene in [s for s in a.scenes.split(",") if s]:
            mp4 = out / f"{a.prefix}-{scene}.mp4"
            encode(c, data, scene, a.fps, mp4)
            dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                 "-of", "csv=p=0", str(mp4)]).decode().strip())
            contact(mp4, out / "contact" / f"{a.prefix}-{scene}.jpg", dur)
    finally:
        c.close()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
