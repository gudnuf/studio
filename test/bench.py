#!/usr/bin/env python3
"""studio benchmark: is it snappy on this machine?

    cd forge/studio && nix develop -c python3 test/bench.py [--out bench.json] [--strict]
        [--fixtures DIR] [--events 5000] [--sse-edits 20] [--skip-pipeline]

Measures, through the real server + CLI (no AI steps):
  * upload -> playable proxy, transcription realtime factor, keyframes time, per fixture
  * export render time for a 5-clip mixed timeline at 9:16 and 16:9 (+ CPU/GPU utilisation)
  * GET /api/state and /api/activity latency p50/p95 with N synthetic events in the log
  * SSE propagation: CLI timeline edit -> event on /api/stream, p50/p95
Prints a table, writes JSON. Budgets are what "snappy" means for the operator; misses are
flagged SLOW and only fail the run with --strict.

CPU comes from `ps` (sum of %CPU, 100 = one core; this machine has os.cpu_count() cores).
GPU comes from `ioreg -c IOAccelerator` "Device Utilization %" (no sudo needed on Apple
silicon). The media engine (VideoToolbox encode/decode) is NOT included in that number;
`powermetrics` would show it but needs sudo, so it is skipped. Which encoders ffmpeg was
launched with is recorded from its command line, so hardware use is still visible.
"""
import argparse, json, os, platform, re, shutil, statistics, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

BUDGETS = {  # metric -> (op, threshold, unit) — what "smooth" means for tomorrow's shoot
    "proxy_ready_s.phone-talking.mov": ("<", 6.0, "s"),
    "proxy_ready_s.screen-recording.mp4": ("<", 5.0, "s"),
    "proxy_ready_s.site-walk.mov": ("<", 8.0, "s"),
    "transcribe_rtf.phone-talking.mov": (">", 8.0, "x"),
    "transcribe_rtf.site-walk.mov": (">", 8.0, "x"),
    "all_local_steps_s.site-walk.mov": ("<", 20.0, "s"),
    "render_speed.9:16": (">", 3.0, "x"),
    "render_speed.16:9": (">", 3.0, "x"),
    # iPhone default footage: 4K HEVC 10-bit HLG. VT decode + HDR->SDR on every frame.
    "proxy_ready_s.iphone-hdr.mov": ("<", 8.0, "s"),
    "transcribe_rtf.iphone-hdr.mov": (">", 8.0, "x"),
    "render_speed.hdr-9:16": (">", 5.0, "x"),
    "state_p95_ms": ("<", 150.0, "ms"),
    "activity_p95_ms": ("<", 100.0, "ms"),
    "sse_append_p95_ms": ("<", 400.0, "ms"),
    "sse_cli_p95_ms": ("<", 1000.0, "ms"),
    "cli_show_p50_ms": ("<", 400.0, "ms"),
}


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    f = int(k)
    return xs[f] if f + 1 >= len(xs) else xs[f] + (xs[f + 1] - xs[f]) * (k - f)


class LogTail(threading.Thread):
    """Timestamps every new events.jsonl line with our own clock (log ts is 1s resolution)."""

    def __init__(self, home, every=0.003):
        super().__init__(daemon=True)
        self.path, self.every, self.lines, self.stop_ = Path(home) / "events.jsonl", every, [], False
        self.pos = self.path.stat().st_size if self.path.exists() else 0

    def run(self):
        buf = ""
        while not self.stop_:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size > self.pos:
                now = time.time()
                with open(self.path) as f:
                    f.seek(self.pos)
                    buf += f.read(size - self.pos)
                self.pos = size
                *done, buf = buf.split("\n")
                for l in done:
                    if l.strip():
                        try:
                            self.lines.append((now, json.loads(l)))
                        except ValueError:
                            pass
            time.sleep(self.every)

    def find(self, pred, since_t=0):
        return next(((t, e) for t, e in list(self.lines) if t >= since_t and pred(e)), (None, None))

    def wait(self, pred, timeout, since_t=0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t, e = self.find(pred, since_t)
            if e:
                return t, e
            time.sleep(0.01)
        raise TimeoutError("waited too long for log event")


class Sampler(threading.Thread):
    """CPU (ps) + GPU (ioreg) + ffmpeg command lines, every `every` seconds."""

    def __init__(self, scope=None, every=0.25):
        super().__init__(daemon=True)
        self.every, self.stop_, self.scope = every, False, scope  # scope: substring of our args
        self.cpu_total, self.cpu_ffmpeg, self.cpu_whisper, self.gpu = [], [], [], []
        self.encoders, self.hwaccel = set(), set()

    def run(self):
        while not self.stop_:
            try:
                out = subprocess.run(["ps", "-A", "-o", "%cpu=,args="], capture_output=True,
                                     text=True, timeout=2).stdout
                tot = ff = wh = 0.0
                for line in out.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) < 2:
                        continue
                    c, args = float(parts[0]), parts[1]
                    tot += c
                    exe = args.split()[0].rsplit("/", 1)[-1]
                    if self.scope and self.scope not in args:
                        continue  # another agent's ffmpeg/whisper: not ours
                    if exe == "ffmpeg":
                        ff += c
                        for m in re.finditer(r"-c:v\s+(\S+)|-vcodec\s+(\S+)", args):
                            self.encoders.add(m.group(1) or m.group(2))
                        for m in re.finditer(r"-hwaccel\s+(\S+)", args):
                            self.hwaccel.add(m.group(1))
                    elif exe.startswith("whisper"):
                        wh += c
                self.cpu_total.append(tot); self.cpu_ffmpeg.append(ff); self.cpu_whisper.append(wh)
            except Exception:
                pass
            try:
                io = subprocess.run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                                    capture_output=True, text=True, timeout=2).stdout
                m = re.search(r'"Device Utilization %"=(\d+)', io)
                if m:
                    self.gpu.append(int(m.group(1)))
            except Exception:
                pass
            time.sleep(self.every)

    def summary(self):
        f = lambda xs: {"mean": round(statistics.mean(xs), 1), "max": round(max(xs), 1)} if xs else None
        return {"cpu_total_pct": f(self.cpu_total), "cpu_ffmpeg_pct": f(self.cpu_ffmpeg),
                "cpu_whisper_pct": f(self.cpu_whisper), "gpu_device_util_pct": f(self.gpu),
                "ffmpeg_encoders": sorted(self.encoders), "ffmpeg_hwaccel": sorted(self.hwaccel),
                "cores": os.cpu_count(), "samples": len(self.cpu_total),
                "note": "cpu_total/gpu are system-wide; ffmpeg/whisper/encoders are this home's only"}

    def __enter__(self):
        self.start(); return self

    def __exit__(self, *a):
        self.stop_ = True; self.join(2)


# ---------------------------------------------------------------------------

def bench_pipeline(fx, home, R):
    srv = H.Server(home)
    tail = LogTail(home); tail.start()
    try:
        names = ["phone-talking.mov", "screen-recording.mp4", "site-walk.mov"]
        if (fx / "iphone-hdr.mov").exists():
            names.append("iphone-hdr.mov")
        for name in names:
            path = fx / name
            with Sampler(str(home)) as smp:
                t0 = time.time()
                job = srv.upload(path, project="bench")["job"]
                t_up = time.time() - t0
                _, done = tail.wait(lambda e: e["type"] in ("job.done", "job.failed")
                                    and e["data"].get("job") == job, 900, t0)
                if done["type"] == "job.failed":
                    raise RuntimeError(f"upload {name} failed: {done['data'].get('error')}")
            mid = done["data"]["result"]["ids"][0]
            mine = [(t, e) for t, e in tail.lines if t >= t0 and e.get("media") == mid]
            at = {e["type"]: t for t, e in mine}
            dur = next(e["data"]["duration"] for _, e in mine if e["type"] == "ingested")
            # step duration = gap from the previous event about this media (steps run in order)
            gaps, prev = {}, at.get("ingested", t0)
            for t, e in mine:
                if e["type"].startswith("step."):
                    gaps[e["type"][5:]] = t - prev
                    prev = t
            r = {"media_s": round(dur, 2), "upload_post_s": round(t_up, 3),
                 "ingested_s": round(at.get("ingested", t0) - t0, 3)}
            for s in ("proxy", "transcribe", "keyframes"):
                if f"step.{s}" in at:
                    r[f"{s}_ready_s"] = round(at[f"step.{s}"] - t0, 3)
                    r[f"{s}_step_s"] = round(gaps.get(s, 0), 3)
            if "transcribe_step_s" in r and r["transcribe_step_s"] > 0:
                r["transcribe_rtf"] = round(dur / r["transcribe_step_s"], 1)
            r["all_local_steps_s"] = round(max(at.values()) - t0, 3)
            r["util"] = smp.summary()
            st, _, _ = srv.request("GET", f"/files/media/{mid}/proxy.mp4", headers={"Range": "bytes=0-1"})
            r["proxy_servable"] = st == 206
            R["pipeline"][name] = r
            R["media_ids"][name] = mid
            print(f"  pipeline {name:<22} proxy {r.get('proxy_ready_s', '-')}s  "
                  f"transcribe x{r.get('transcribe_rtf', '-')}  all {r['all_local_steps_s']}s", flush=True)

        # ---- render ----
        ids = R["media_ids"]
        pt, sr, sw = ids["phone-talking.mov"], ids["screen-recording.mp4"], ids["site-walk.mov"]
        clips = [{"media": pt, "in": 2, "out": 8}, {"media": sr, "in": 3, "out": 8},
                 {"media": sw, "in": 10, "out": 16}, {"media": pt, "in": 20, "out": 25},
                 {"media": sw, "in": 40, "out": 46}]
        out_s = sum(c["out"] - c["in"] for c in clips)
        slug = srv.json("POST", "/api/projects", {"name": "bench render"})["slug"]
        runs = [("9:16", "9:16", clips), ("16:9", "16:9", clips)]
        if "iphone-hdr.mov" in ids:   # landscape 4K HLG going vertical: the slow case before
            hd = ids["iphone-hdr.mov"]
            runs.append(("hdr-9:16", "9:16", [{"media": hd, "in": 4 + i * 12, "out": 12 + i * 12} for i in range(6)]))
        for label, aspect, clips in runs:
            out_s = sum(c["out"] - c["in"] for c in clips)
            rev = srv.state()["projects"][slug].get("rev", 0)
            srv.json("PUT", f"/api/projects/{slug}/timeline?base={rev}",
                     {"aspect": aspect, "captions": True, "clips": clips})
            with Sampler(str(home)) as smp:
                t0 = time.time()
                job = srv.json("POST", f"/api/projects/{slug}/render")["job"]
                t_done, done = tail.wait(lambda e: e["type"] in ("job.done", "job.failed")
                                         and e["data"].get("job") == job, 900, t0)
            if done["type"] == "job.failed":
                raise RuntimeError(f"render {aspect} failed: {done['data'].get('error')}")
            wall = t_done - t0
            R["render"][label] = {"wall_s": round(wall, 3), "output_s": out_s,
                                   "speed_x": round(out_s / wall, 2), "util": smp.summary(),
                                   "file": done["data"]["result"]["file"]}
            print(f"  render {label:<8} {wall:.2f}s for {out_s}s  (x{out_s / wall:.1f})", flush=True)
    finally:
        tail.stop_ = True
        srv.stop()


def synth_events(src_home, dst_home, n):
    """Copy a real log, then append n realistic events (edits, notes, jobs, focus churn)."""
    src = H.events(src_home)
    media = [e for e in src if e["type"] == "ingested"]
    vids = [e for e in media if e["data"].get("has_video")] or media
    lines = [json.dumps(e) for e in src]
    ts = "2026-11-08T10:00:00-08:00"
    slugs = [f"synth-{i}" for i in range(8)]
    for s in slugs:
        lines.append(json.dumps({"ts": ts, "type": "project.created", "media": None,
                                 "data": {"slug": s, "name": s, "brand": "jefe"}}))
    for i in range(n):
        k = i % 10
        if k < 6 and vids:
            clips = []
            for j in range(4 + i % 9):
                v = vids[(i + j) % len(vids)]
                a = (j * 2.5) % max(1, v["data"]["duration"] - 3)
                clips.append({"id": f"{i:05d}{j:03d}", "media": v["media"], "in": round(a, 3),
                              "out": round(a + 2.2, 3), **({"label": f"take {j}"} if j % 2 else {})})
            ev = {"type": "timeline.saved", "media": None,
                  "data": {"project": slugs[i % len(slugs)], "actor": "operator" if i % 3 else "claude",
                           "timeline": {"aspect": "9:16", "captions": True, "clips": clips}}}
        elif k == 6:
            ev = {"type": "note", "media": None,
                  "data": {"text": f"note {i}: tighten the open", "project": slugs[i % 8],
                           "focus": {"clip": i % 5}, "actor": "operator"}}
        elif k == 7:
            ev = {"type": "job.queued", "media": None, "data": {"job": f"j{i}", "kind": "steps"}}
        elif k == 8:
            ev = {"type": "job.started", "media": None, "data": {"job": f"j{i-1}", "kind": "steps"}}
        else:
            ev = {"type": "job.done", "media": None, "data": {"job": f"j{i-2}", "kind": "steps"}}
        lines.append(json.dumps({"ts": ts, **ev}))
    (dst_home / "events.jsonl").write_text("\n".join(lines) + "\n")
    # share media dirs so /api/state does its real per-media file loads
    (dst_home / "media").rmdir()
    (dst_home / "media").symlink_to(Path(src_home) / "media")
    return len(lines)


def bench_state_and_sse(src_home, R, n_events, n_edits):
    home = H.new_home("bench-state")
    total = synth_events(src_home, home, n_events)
    R["state"]["log_events"] = total
    R["state"]["log_bytes"] = (home / "events.jsonl").stat().st_size
    srv = H.Server(home)
    try:
        def lat(path, n):
            xs = []
            for _ in range(n):
                t = time.perf_counter()
                st, _, body = srv.request("GET", path)
                xs.append((time.perf_counter() - t) * 1000)
                assert st == 200, (path, st)
            return xs, len(body)
        srv.request("GET", "/api/state")  # warm
        xs, size = lat("/api/state", 60)
        R["state"].update(state_p50_ms=round(pct(xs, 50), 1), state_p95_ms=round(pct(xs, 95), 1),
                          state_max_ms=round(max(xs), 1), state_bytes=size)
        xs, size = lat("/api/activity", 60)
        R["state"].update(activity_p50_ms=round(pct(xs, 50), 1), activity_p95_ms=round(pct(xs, 95), 1))
        # the agent's loop: `studio timeline show` (python start + fold)
        xs = []
        for _ in range(8):
            t = time.perf_counter()
            H.cli(home, "timeline", "show", "synth-0")
            xs.append((time.perf_counter() - t) * 1000)
        R["state"].update(cli_show_p50_ms=round(pct(xs, 50), 1), cli_show_p95_ms=round(pct(xs, 95), 1))
        print(f"  /api/state  p50 {R['state']['state_p50_ms']}ms  p95 {R['state']['state_p95_ms']}ms"
              f"  ({total} events, {size // 1024} KiB)", flush=True)

        # ---- SSE ----
        vid = next(e["media"] for e in H.events(src_home)
                   if e["type"] == "ingested" and e["data"].get("has_video"))
        slug = srv.json("POST", "/api/projects", {"name": "sse bench"})["slug"]
        tail = LogTail(home, every=0.001); tail.start()
        sse = H.SSE(srv.port)
        append_lat, cli_lat, cli_wall = [], [], []
        try:
            time.sleep(0.4)
            for i in range(n_edits):
                n0 = len(sse.events)
                args = (["timeline", "add", slug, vid, "0", "2"] if i % 2 == 0
                        else ["timeline", "rm", slug, "1"])
                t0 = time.time()
                H.cli(home, *args, actor_claude=True)
                t1 = time.time()
                pred = lambda e: e["type"] == "timeline.saved" and e["data"]["project"] == slug
                t_sse, ev = sse.wait_for(pred, since=n0, timeout=5)
                t_app, _ = tail.wait(pred, 5, t0)
                if ev is None:
                    raise RuntimeError("SSE event never arrived")
                append_lat.append((t_sse - t_app) * 1000)
                cli_lat.append((t_sse - t0) * 1000)
                cli_wall.append((t1 - t0) * 1000)
                time.sleep(0.05 + (i % 7) * 0.037)  # de-phase from the server's poll interval
        finally:
            sse.close(); tail.stop_ = True
        R["sse"] = {"edits": n_edits,
                    "sse_append_p50_ms": round(pct(append_lat, 50), 1),
                    "sse_append_p95_ms": round(pct(append_lat, 95), 1),
                    "sse_cli_p50_ms": round(pct(cli_lat, 50), 1),
                    "sse_cli_p95_ms": round(pct(cli_lat, 95), 1),
                    "cli_edit_wall_p50_ms": round(pct(cli_wall, 50), 1)}
        print(f"  SSE append->event p50 {R['sse']['sse_append_p50_ms']}ms p95 "
              f"{R['sse']['sse_append_p95_ms']}ms; CLI start->event p95 {R['sse']['sse_cli_p95_ms']}ms",
              flush=True)
    finally:
        srv.stop()


def flatten(R):
    f = {}
    for name, r in R["pipeline"].items():
        for k in ("proxy_ready_s", "transcribe_rtf", "keyframes_step_s", "all_local_steps_s"):
            if k in r:
                f[f"{k}.{name}"] = r[k]
    for a, r in R["render"].items():
        f[f"render_speed.{a}"] = r["speed_x"]
        f[f"render_wall_s.{a}"] = r["wall_s"]
    for k, v in {**R["state"], **R.get("sse", {})}.items():
        if isinstance(v, (int, float)):
            f[k] = v
    return f


def report(R, strict):
    flat = flatten(R)
    rows, slow = [], []
    for k, v in flat.items():
        b = BUDGETS.get(k)
        verdict = ""
        if b:
            op, thr, unit = b
            ok = v < thr if op == "<" else v > thr
            verdict = f"{'ok  ' if ok else 'SLOW'} ({op}{thr}{unit})"
            if not ok:
                slow.append(k)
        rows.append((k, v, verdict))
    w = max(len(r[0]) for r in rows)
    print("\n" + "metric".ljust(w) + "  value       budget")
    print("-" * (w + 34))
    for k, v, verdict in rows:
        print(f"{k.ljust(w)}  {str(v):<10}  {verdict}")
    for scope, src in [("pipeline", R["pipeline"]), ("render", R["render"])]:
        for name, r in src.items():
            u = r.get("util") or {}
            if u.get("samples"):
                print(f"util {scope}:{name:<22} cpu mean {u['cpu_total_pct']['mean']}% "
                      f"max {u['cpu_total_pct']['max']}% (of {u['cores'] * 100}%), ffmpeg max "
                      f"{u['cpu_ffmpeg_pct']['max']}%, whisper max {u['cpu_whisper_pct']['max']}%, "
                      f"gpu {u['gpu_device_util_pct']}, enc {u['ffmpeg_encoders']} hw {u['ffmpeg_hwaccel']}")
    R["flat"], R["slow"] = flat, slow
    print(f"\n{len(slow)} metric(s) over budget: {', '.join(slow) or 'none'}")
    return 1 if (strict and slow) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--fixtures", default=None)
    ap.add_argument("--events", type=int, default=5000)
    ap.add_argument("--sse-edits", type=int, default=20)
    ap.add_argument("--strict", action="store_true", help="exit 1 if any budget is missed")
    a = ap.parse_args()
    if not H.have_tools("ffmpeg", "ffprobe", "whisper-cli"):
        sys.exit("bench: run inside `nix develop` (needs ffmpeg, ffprobe, whisper-cli)")
    if a.fixtures:
        os.environ["STUDIO_FIXTURES"] = a.fixtures
    fx = H.fixtures()
    out = Path(a.out or Path(H.tmp_root() or "/tmp") / "bench.json")
    R = {"when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": platform.node(),
         "machine": platform.machine(), "cores": os.cpu_count(),
         "chip": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                text=True).stdout.strip(),
         "pipeline": {}, "render": {}, "state": {}, "media_ids": {},
         "gpu_note": "ioreg Device Utilization % (GPU only; media engine not visible without sudo powermetrics)"}
    home = H.new_home("bench")
    print(f"bench home {home}", flush=True)
    t = time.time()
    bench_pipeline(fx, home, R)
    bench_state_and_sse(home, R, a.events, a.sse_edits)
    R["bench_wall_s"] = round(time.time() - t, 1)
    rc = report(R, a.strict)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(R, indent=1))
    print(f"wrote {out}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
