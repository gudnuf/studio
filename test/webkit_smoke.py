"""WebKit smoke: the core operator path in Safari, through safaridriver (W3C WebDriver over
plain HTTP, stdlib only). Safari is the same engine as the Mac app's WKWebView, so this catches
what Chrome-only testing misses.

    nix develop -c python3 test/webkit_smoke.py

Needs Safari's remote automation, which macOS only lets an admin turn on, once:

    sudo safaridriver --enable

Without it this prints that command and exits 0 (skipped). Ports come from
$STUDIO_TEST_PORTS (default 7400-7499). `claude -p` is test/fakebin/claude.
"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

TEST = Path(__file__).resolve().parent
ROOT = TEST.parent
sys.path.insert(0, str(TEST))
import harness as Hn
import webkit_core

ENABLE = "sudo safaridriver --enable"


def http(method, url, body=None, timeout=60):
    r = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=timeout).read() or b"{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


class Safari:
    name = "safari"

    def __init__(self, port, outdir):
        self.out = Path(outdir)
        self.proc = subprocess.Popen(["/usr/bin/safaridriver", "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/status", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        r = http("POST", self.base + "/session", {"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        v = r.get("value", {})
        if "sessionId" not in v:
            self.proc.terminate()
            raise RuntimeError(v.get("message") or str(r))
        self.sid = v["sessionId"]
        self.s = f"{self.base}/session/{self.sid}"
        http("POST", self.s + "/window/rect", {"width": 1500, "height": 950, "x": 0, "y": 0})

    def go(self, url):
        http("POST", self.s + "/url", {"url": url})

    def js(self, body, timeout=30):
        script = ("const done = arguments[arguments.length - 1];"
                  f"(async () => {{ {body}\n }})().then(v => done({{ok: true, value: v === undefined ? null : v}}),"
                  " e => done({ok: false, error: String((e && e.stack) || e)}));")
        http("POST", self.s + "/timeouts", {"script": int(timeout * 1000)})
        r = http("POST", self.s + "/execute/async", {"script": script, "args": []}, timeout=timeout + 10).get("value")
        if not isinstance(r, dict) or not r.get("ok"):
            print("   js error:", (r or {}).get("error") if isinstance(r, dict) else r)
            return None
        return r["value"]

    def find(self, sel):
        v = http("POST", self.s + "/element", {"using": "css selector", "value": sel}).get("value", {})
        return next(iter(v.values()), None) if isinstance(v, dict) else None

    def click(self, sel):   # a real click: a user gesture, as the clipboard needs
        e = self.find(sel)
        if e:
            http("POST", f"{self.s}/element/{e}/click", {})

    def drag(self, sel, dx):   # a real pointer drag (W3C actions)
        e = self.find(sel)
        if not e:
            return
        ref = {"element-6066-11e4-a52e-4f735466cecf": e}
        http("POST", self.s + "/actions", {"actions": [{"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"},
             "actions": [{"type": "pointerMove", "duration": 0, "origin": ref, "x": 0, "y": 0}, {"type": "pointerDown", "button": 0},
                         {"type": "pointerMove", "duration": 150, "origin": "pointer", "x": dx // 2, "y": 0},
                         {"type": "pointerMove", "duration": 150, "origin": "pointer", "x": dx - dx // 2, "y": 0},
                         {"type": "pointerUp", "button": 0}]}]})
        http("DELETE", self.s + "/actions")

    def drop(self, paths):
        # Safari can't take a Finder drag from WebDriver: this is the browser path, files
        # fetched from the server and dropped as DOM Files (an HTTP upload, like Chrome's test)
        names = json.dumps([Path(p).name for p in paths])
        self.js(f"""const dt = new DataTransfer();
          for (const n of {names}) {{ const b = await (await fetch('/files/fx/' + n)).blob();
            dt.items.add(new File([b], n, {{type: n.endsWith('mp4') ? 'video/mp4' : 'video/quicktime'}})); }}
          window.dispatchEvent(new DragEvent('drop', {{dataTransfer: dt, bubbles: true, cancelable: true}})); return 1""", 60)

    def snap(self, name):
        v = http("GET", self.s + "/screenshot").get("value")
        if isinstance(v, str):
            (self.out / f"{name}.png").write_bytes(base64.b64decode(v))

    def close(self):
        try:
            http("DELETE", self.s, timeout=10)
        finally:
            self.proc.terminate()


def main():
    if sys.platform != "darwin" or not Path("/usr/bin/safaridriver").exists():
        print("SKIP webkit smoke: safaridriver is macOS only")
        return True
    F = tempfile.mkdtemp(prefix="studio-webkit-", dir=Hn.tmp_root())
    sd_port = Hn.free_port()
    try:
        drv = Safari(sd_port, F)
    except RuntimeError as e:
        msg = str(e)
        if "remote automation" in msg.lower() or "allow remote" in msg.lower():
            print(f"SKIP webkit smoke: Safari's remote automation is off. Turn it on once with:\n    {ENABLE}\n({msg})")
            return True
        print(f"SKIP webkit smoke: safaridriver couldn't start a session ({msg})")
        return True
    print("webkit smoke dir", F)
    FX = Hn.fixtures()
    home = Path(F) / "home"
    (home / "fx").mkdir(parents=True)
    names = ["phone-talking.mov", "site-walk.mov", "screen-recording.mp4"]
    for n in names:
        shutil.copy(FX / n, home / "fx" / n)
    port = Hn.free_port()
    env = Hn.env_for(home)
    env["PATH"] = str(TEST / "fakebin") + ":" + env["PATH"]
    srv = subprocess.Popen([sys.executable, str(ROOT / "studio.py"), "ui", "--port", str(port), "--no-open"], env=env,
                           stdout=open(Path(F) / "srv.log", "w"), stderr=subprocess.STDOUT)
    B = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(B + "/api/health", timeout=1); break
        except Exception:
            time.sleep(0.1)
    results = []

    def check(name, ok, info=""):
        results.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name + ("  " + str(info)[:400] if not ok or info else ""), flush=True)

    def api(path, body=None):
        return json.loads(urllib.request.urlopen(urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                          method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"}), timeout=10).read())

    def cli(*a, claude=False):
        e = dict(env)
        if claude:
            e["CLAUDECODE"] = "1"
        return subprocess.run([sys.executable, str(ROOT / "studio.py"), *a], env=e, capture_output=True, text=True)

    try:
        drv.go(B + "/")
        drv.js("localStorage.clear(); return 1")
        drv.go(B + "/")
        webkit_core.run(drv, check, api, cli, [home / "fx" / n for n in names], mute=True)
    finally:
        drv.close()
        srv.terminate()
    print("\n%d/%d passed" % (sum(ok for _, ok in results), len(results)))
    return all(ok for _, ok in results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
