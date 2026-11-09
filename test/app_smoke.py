"""The built Mac app (Studio.app), end to end, in its own WKWebView.

    nix build .#studio-app && nix develop -c python3 test/app_smoke.py [path/to/Studio.app]

Launches the app the way the Dock does (`open`, so it gets launchd's bare environment, not
this shell's) with STUDIO_HOME on a throwaway library, ports from $STUDIO_TEST_PORTS
(default 7400-7499), test/fakebin first on PATH (no subscription spent), and
STUDIO_APP_DEVHOOK, which lets this script run JS in the page, snapshot the webview and drop
files through the window's real NSDraggingDestination methods. That works with the screen
locked; no mouse, keyboard or screenshot permission is needed.

Then: the WebKit core path (webkit_core.py), plus what only the app does: native drops
(originals untouched, clones), the File menu's picker route, ⌘Z routing (text field vs
story), notifications, attaching to a server already running for the library, quitting
(stops only its own server), and a crash (the server exits with it).
Skips (exit 0) when there's no built app.
"""
import hashlib, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

TEST = Path(__file__).resolve().parent
ROOT = TEST.parent
sys.path.insert(0, str(TEST))
import harness as Hn
import webkit_core

APP = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("STUDIO_APP", ROOT / "result/Applications/Studio.app"))
if not (APP / "Contents/MacOS/studio-app").exists():
    print(f"SKIP app smoke: no built app at {APP} (run `nix build .#studio-app` in {ROOT})")
    sys.exit(0)
BIN = str((APP / "Contents/MacOS/studio-app").resolve())
F = tempfile.mkdtemp(prefix="studio-app-", dir=Hn.tmp_root())
print("app smoke dir", F)
lo, hi = (os.environ.get("STUDIO_TEST_PORTS") or "7400-7499").split("-")
PORTS = f"{int(lo) + 50}-{hi}"            # the half harness.free_port doesn't hand out first
FAKE_PATH = f"{TEST / 'fakebin'}:/usr/bin:/bin:/usr/sbin:/sbin"
results = []


def check(name, ok, info=""):
    results.append((name, bool(ok), info))
    print(("PASS " if ok else "FAIL ") + name + ("  " + str(info)[:400] if not ok or info else ""), flush=True)


def app_pids():
    r = subprocess.run(["pgrep", "-f", BIN], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def quit_app(pid):
    """Quit like ⌘Q does (an AppleEvent to that process), not a signal."""
    js = ("ObjC.import('AppKit'); function run(a) { const app = $.NSRunningApplication"
          ".runningApplicationWithProcessIdentifier(parseInt(a[0])); if (!app.isNil()) app.terminate; return 1; }")
    subprocess.run(["osascript", "-l", "JavaScript", "-e", js, str(pid)], capture_output=True)


class AppDriver:
    name = "app"

    def __init__(self, home, hook, extra_env=()):
        self.home, self.hook, self.n = Path(home), Path(hook), 0
        for sub in ("in", "out", "done"):
            (self.hook / sub).mkdir(parents=True, exist_ok=True)
        before = set(app_pids())
        env = [f"STUDIO_HOME={home}", f"STUDIO_APP_PORTS={PORTS}", f"STUDIO_APP_DEVHOOK={hook}",
               f"PATH={FAKE_PATH}", "STUDIO_SETTLE_SECS=0.5", *extra_env]
        cmd = ["open", "-n", str(APP)]
        for e in env:
            cmd += ["--env", e]
        subprocess.run(cmd, check=True)
        new = Hn_wait(lambda: [p for p in app_pids() if p not in before], 20)
        self.pid = new[0] if new else None

    def port(self):
        try:
            return json.loads((self.home / ".server.json").read_text())["port"]
        except (OSError, ValueError, KeyError):
            return None

    def js(self, body, timeout=20):
        self.n += 1
        name = f"q{self.n:04d}"
        out = self.hook / "out" / f"{name}.json"
        (self.hook / "in" / f"{name}.js.tmp").write_text(body)
        os.rename(self.hook / "in" / f"{name}.js.tmp", self.hook / "in" / f"{name}.js")
        if not Hn_wait(out.exists, timeout, 0.05):
            return None
        r = json.loads(out.read_text())
        if not r.get("ok"):
            print("   js error:", r.get("error", "")[:300])
            return None
        return r.get("value")

    def snap(self, name):
        (self.hook / "in" / f"{name}.snap").write_text("")
        png = self.hook / "out" / f"{name}.png"
        if Hn_wait(png.exists, 10, 0.1):
            time.sleep(0.2)
            shutil.copy(png, Path(F) / f"{name}.png")

    def drop(self, paths):
        self.n += 1
        name = f"drop{self.n:04d}"
        (self.hook / "in" / f"{name}.drop").write_text("\n".join(map(str, paths)))
        out = self.hook / "out" / f"{name}.json"
        Hn_wait(out.exists, 10, 0.05)
        r = json.loads(out.read_text()) if out.exists() else None
        check("native drop: the window accepts a Finder-style drag of files (NSDragOperationCopy)",
              r and r["op"] == 1 and r["accepted"], r)
        return r

    def click(self, sel):   # evaluateJavaScript counts as a user gesture in WKWebView
        return self.js(f"document.querySelector({json.dumps(sel)}).click(); return 1")

    def drag(self, sel, dx):
        return self.js(f"""
          const raf = () => new Promise(r => requestAnimationFrame(() => r()));
          const h = document.querySelector({json.dumps(sel)}); const r = h.getBoundingClientRect();
          const x = r.left + r.width / 2, y = r.top + r.height / 2, content = document.querySelector('#tl-content');
          const ev = (t, cx, b) => new PointerEvent(t, {{bubbles: true, cancelable: true, clientX: cx, clientY: y, button: 0,
            buttons: b, pointerId: 1, pointerType: 'mouse', isPrimary: true}});
          h.dispatchEvent(ev('pointerdown', x, 1)); await raf();
          content.dispatchEvent(ev('pointermove', x + {dx} / 2, 1)); await raf();
          content.dispatchEvent(ev('pointermove', x + {dx}, 1)); await raf(); await raf();
          content.dispatchEvent(ev('pointerup', x + {dx}, 0)); await raf(); return 1;""")


def Hn_wait(fn, t, step=0.2):
    end = time.time() + t
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    # keep the display awake (and say a user is active) while the app is driven: with the
    # display asleep WebKit stops animation frames for the page. This unlocks nothing.
    awake = subprocess.Popen(["caffeinate", "-d", "-u", "-t", "1800", "-w", str(os.getpid())])
    time.sleep(1)
    try:
        return _main()
    finally:
        awake.terminate()


def _main():
    FX = Hn.fixtures()
    # the operator's own files, somewhere outside the library: they must never change
    own = Path(F) / "Desktop-footage"
    (own / "folder").mkdir(parents=True)
    names = ["phone-talking.mov", "site-walk.mov", "screen-recording.mp4"]
    for n in names[:2]:
        shutil.copy(FX / n, own / n)
    shutil.copy(FX / names[2], own / "folder" / names[2])
    shutil.copy(FX / "junk.txt", own / "folder" / "notes.txt")
    old = time.time() - 3600
    for f in own.rglob("*"):
        if f.is_file():
            os.utime(f, (old, old))
    before = {str(f): (sha(f), f.stat().st_mtime_ns, f.stat().st_ino) for f in own.rglob("*") if f.is_file()}

    home = Path(F) / "app-home"
    home.mkdir()
    hook = Path(F) / "hook"
    d = AppDriver(home, hook)
    check("app launched (via open, like the Dock)", d.pid, app_pids())
    port = Hn_wait(d.port, 30)
    check("app started its own server and the library's lockfile names it", port, port)
    B = f"http://127.0.0.1:{port}"

    def api(path, body=None):
        r = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                   method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(r, timeout=10).read())

    env = Hn.env_for(home)
    env["PATH"] = str(TEST / "fakebin") + ":" + env["PATH"]

    def cli(*a, claude=False):
        e = dict(env)
        if claude:
            e["CLAUDECODE"] = "1"
        return subprocess.run([sys.executable, str(ROOT / "studio.py"), *a], env=e, capture_output=True, text=True)

    h = api("/api/health")
    check("server tools are pinned nix store paths", all(str(h["tools"][t]).startswith("/nix/store/") for t in ("ffmpeg", "whisper-cli")), h["tools"])
    check("`claude` resolved on the server's PATH (fakebin first here; a Dock launch gets the login shell's)",
          h["tools"]["claude"] == str(TEST / "fakebin/claude"), h["tools"]["claude"])
    info = None
    Hn_wait(lambda: d.js("return location.protocol") == "http:", 30, 0.3)
    info = d.js("return await window.__TAURI__.core.invoke('app_info')")
    check("the UI runs inside the app with its native bridge (window.__TAURI__, app_info)", info and info["url"] == f"{B}/", info)

    # drop: a file and a folder (whose media come in; a text file is skipped)
    slug = webkit_core.run(d, check, api, cli, [own / names[0], own / names[1], own / "folder"])
    after = {str(f): (sha(f), f.stat().st_mtime_ns, f.stat().st_ino) for f in own.rglob("*") if f.is_file()}
    check("the operator's dropped files are untouched (same bytes, mtime, inode, still there)", before == after,
          [k for k in before if before[k] != after.get(k)])
    evs = [e for e in Hn.events(home) if e["type"] == "ingested"]
    check("the folder's media came in, its text file didn't", sorted(e["data"]["name"] for e in evs) == sorted(names), [e["data"]["name"] for e in evs])
    srcs = [Path(e["data"]["path"]) for e in evs]
    check("library copies exist (APFS clones of the originals)", all(p.exists() for p in srcs), srcs)

    # ⌘Z routing as the Edit menu does it: in a text field, the text; elsewhere, the story
    r = d.js("""const sleep = ms => new Promise(r => setTimeout(r, ms));
      document.querySelector('.tab[data-tab=claude]').click(); await sleep(300);
      const ta = document.querySelector('#tab-claude textarea'); ta.focus(); ta.value = '';
      document.execCommand('insertText', false, 'hello there');
      const n0 = window.__studio.S.tl.clips.length, typed = ta.value;
      await window.__TAURI__.event.emit('studio://menu', 'undo'); await sleep(300);
      const r = {typed, afterText: ta.value, n0, n1: window.__studio.S.tl.clips.length};
      ta.blur(); await window.__TAURI__.event.emit('studio://menu', 'undo'); await sleep(500);
      r.n2 = window.__studio.S.tl.clips.length; return r;""")
    check("Edit ▸ Undo in a text field undoes the typing, not the story", r and r["typed"] == "hello there" and r["afterText"] == "" and r["n1"] == r["n0"], r)
    check("Edit ▸ Undo outside a text field undoes the story", r and r["n2"] != r["n0"], r)
    r = d.js("const sleep = ms => new Promise(r => setTimeout(r, ms)); const c0 = document.querySelector('#captions-toggle').checked;"
             "await window.__TAURI__.event.emit('studio://menu', 'toggle-captions'); await sleep(300);"
             "return [c0, document.querySelector('#captions-toggle').checked]")
    check("View ▸ Toggle Captions reaches the UI", r and r[0] != r[1], r)
    n = d.js("return await window.__TAURI__.core.invoke('notify', {title: 'Studio test', body: 'notification route'})")
    check("notify command answers (shows only when the window is in the background)", n in (True, False), n)

    # a second launch must not start a second app (single instance)
    pids0 = app_pids()
    subprocess.run(["open", "-n", str(APP), "--env", f"STUDIO_HOME={home}", "--env", f"STUDIO_APP_PORTS={PORTS}"])
    time.sleep(4)
    check("single instance: a second launch hands over to the first and exits", sorted(app_pids()) == sorted(pids0), (pids0, app_pids()))

    # quit like ⌘Q: our server stops, lockfile goes
    spid = api("/api/health")["pid"]
    t0 = time.time()
    quit_app(d.pid)
    gone = Hn_wait(lambda: not alive(d.pid) and not alive(spid), 15, 0.1)
    check("Quit: the app and the server it started both exit", gone, round(time.time() - t0, 2))
    check("...and the library's lockfile is removed", not (home / ".server.json").exists())

    # crash: SIGKILL the app; the server notices (stdin EOF / parent gone) and exits
    hook2 = Path(F) / "hook2"
    d2 = AppDriver(home, hook2)
    Hn_wait(d2.port, 30)
    spid = api_health_pid(home)
    os.kill(d2.pid, signal.SIGKILL)
    t0 = time.time()
    gone = Hn_wait(lambda: spid and not alive(spid), 10, 0.1)
    check("app killed (crash): its server exits by itself", gone, round(time.time() - t0, 2))

    # attach: a server already running for the library (./studio ui from a terminal) is reused
    tport = Hn.free_port()
    srv = subprocess.Popen([sys.executable, str(ROOT / "studio.py"), "ui", "--port", str(tport), "--no-open"], env=env,
                           stdout=open(Path(F) / "terminal-srv.log", "w"), stderr=subprocess.STDOUT)
    Hn_wait(lambda: (home / ".server.json").exists() and json.loads((home / ".server.json").read_text())["pid"] == srv.pid, 20)
    d3 = AppDriver(home, Path(F) / "hook3")
    Hn_wait(lambda: d3.js("return location.port"), 30, 0.3)
    info = None
    Hn_wait(lambda: d3.js("return location.port") == str(tport), 20, 0.3)
    info = d3.js("return await window.__TAURI__.core.invoke('app_info')")
    check("attaches to the server already running for this library (no second server)",
          info and info["attached"] and info["url"].endswith(f":{tport}/") and info["server_pid"] is None, info)
    d3.snap("w-attached")
    quit_app(d3.pid)
    Hn_wait(lambda: not alive(d3.pid), 15)
    time.sleep(0.5)
    check("quitting an attached app leaves the terminal's server running", srv.poll() is None and api_health_pid(home) == srv.pid)
    srv.terminate(); srv.wait(5)

    print("\nsnapshots:", " ".join(sorted(str(p) for p in Path(F).glob("w-*.png"))))
    print("%d/%d passed" % (sum(ok for _, ok, _ in results), len(results)))
    return all(ok for _, ok, _ in results)


def api_health_pid(home):
    try:
        port = json.loads((Path(home) / ".server.json").read_text())["port"]
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3).read())["pid"]
    except Exception:
        return None


if __name__ == "__main__":
    ok = False
    try:
        ok = main()
    finally:
        for p in app_pids():      # never leave a test app running
            quit_app(p)
    sys.exit(0 if ok else 1)
