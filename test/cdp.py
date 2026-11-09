"""Minimal stdlib Chrome DevTools Protocol client (websocket + JSON-RPC) for headless UI tests."""
import base64, json, os, socket, struct, subprocess, sys, time, urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

class WS:
    def __init__(self, url):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, text):
        data = text.encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126: hdr.append(0x80 | n)
        elif n < 65536: hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else: hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk: raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            msg += payload
            if b0 & 0x80:
                return msg.decode(errors="replace")

class CDP:
    def __init__(self, port=9333, width=1500, height=950, profile="/tmp/cdp-prof"):
        self.proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", f"--window-size={width},{height}", "--autoplay-policy=no-user-gesture-required",
            "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.1)
        self.ws = WS(page["webSocketDebuggerUrl"])
        self.n = 0
        self.events = []
        self.call("Runtime.enable"); self.call("Log.enable"); self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    def call(self, method, **params):
        self.n += 1
        my = self.n
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == my:
                if "error" in m: raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})
            self.events.append(m)

    def js(self, expr, await_=True):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=await_, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:1500])
        return r["result"].get("value")

    def errors(self):
        out = []
        for m in self.events:
            meth = m.get("method")
            p = m.get("params", {})
            if meth == "Runtime.exceptionThrown":
                out.append("EXC " + json.dumps(p["exceptionDetails"])[:800])
            elif meth == "Runtime.consoleAPICalled" and p["type"] in ("error", "warning", "assert"):
                out.append(p["type"].upper() + " " + " ".join(str(a.get("value", a.get("description", ""))) for a in p["args"])[:500])
            elif meth == "Log.entryAdded" and p["entry"]["level"] in ("error", "warning"):
                out.append("LOG " + p["entry"].get("text", "")[:300] + " " + p["entry"].get("url", ""))
        return out

    def pump(self, secs):
        """Process events for a while (keeps console messages flowing)."""
        end = time.time() + secs
        while time.time() < end:
            self.js("1")
            time.sleep(0.05)

    def shot(self, path):
        r = self.call("Page.captureScreenshot", format="png")
        open(path, "wb").write(base64.b64decode(r["data"]))

    def mouse(self, type_, x, y, buttons=0, button="none", mods=0):
        self.call("Input.dispatchMouseEvent", type=type_, x=x, y=y, button=button, buttons=buttons,
                  clickCount=1 if type_ in ("mousePressed", "mouseReleased") else 0, modifiers=mods)

    def drag(self, x0, y0, x1, y1, steps=12, mods=0):
        self.mouse("mouseMoved", x0, y0)
        self.mouse("mousePressed", x0, y0, 1, "left", mods)
        for i in range(1, steps + 1):
            self.mouse("mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, 1, "left", mods)
            time.sleep(0.016)
        self.mouse("mouseReleased", x1, y1, 0, "left", mods)

    def click(self, x, y):
        self.mouse("mouseMoved", x, y)
        self.mouse("mousePressed", x, y, 1, "left")
        self.mouse("mouseReleased", x, y, 0, "left")

    def key(self, key, code=None, text=None, mods=0, vk=0):
        base = dict(key=key, code=code or key, modifiers=mods, windowsVirtualKeyCode=vk)
        self.call("Input.dispatchKeyEvent", type="keyDown", **base, **({"text": text} if text else {}))
        self.call("Input.dispatchKeyEvent", type="keyUp", **base)

    def close(self):
        try: self.call("Browser.close")
        except Exception: pass
        self.proc.terminate()
