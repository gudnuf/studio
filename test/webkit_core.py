"""The operator's core path, checked in WebKit (Safari's engine, which the Mac app's WKWebView
uses), written once against a small driver so it runs in two places:

  test/webkit_smoke.py  Safari via safaridriver (W3C WebDriver, stdlib HTTP)
  test/app_smoke.py     the built Studio.app, through its STUDIO_APP_DEVHOOK

A driver has: js(body, timeout) -> value (body is an async function body: use `return`),
snap(name), drop(paths) (files in, the driver's own way), click(selector) (a real user
gesture where the driver can make one), drag(selector, dx) (a pointer drag), and .name.
Checks cover what differs between Chrome (where the UI was built) and WebKit: H.264 proxy
playback, gapless two-video playback across a cut, fastSeek vs exact seeks, pointer drags on
the timeline, CSS :has, the page's own drag and drop, clipboard writes, EventSource.
"""
import json, subprocess, time

SLEEP = "const sleep = ms => new Promise(r => setTimeout(r, ms));"
MODS = "const st = await import('/ui/store.js'), ops = await import('/ui/ops.js'), {player} = await import('/ui/player.js');"


def screen_locked():
    """With the screen locked (display asleep) WebKit throttles media clocks: currentTime
    freezes for ~280 ms at a time even mid-clip, so frame-timing checks can't be judged."""
    js = ("ObjC.import('CoreGraphics'); function run() { const d = ObjC.deepUnwrap(ObjC.castRefToObject("
          "$.CGSessionCopyCurrentDictionary())); return String(!!(d && d.CGSSessionScreenIsLocked)); }")
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True)
    return r.stdout.strip() == "true"


def wait(fn, t=120, step=0.3):
    end = time.time() + t
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def run(d, check, api, cli, files, mute=False):
    """files: absolute paths of phone-talking.mov, site-walk.mov, screen-recording.mp4 (to drop)."""
    # --- boot + EventSource ---
    ok = wait(lambda: d.js("return !!(window.__studio && document.querySelector('#conn').classList.contains('ok'))"), 30, 0.5)
    check("boots, and the live link (EventSource) connects: footer says Live", ok)
    eng = d.js("return {ua: navigator.userAgent, has: CSS.supports('selector(:has(a))'), fast: typeof HTMLVideoElement.prototype.fastSeek, "
               "native: !!window.__TAURI__, secure: window.isSecureContext, clip: !!(navigator.clipboard && navigator.clipboard.writeText)}")
    check("engine is WebKit (not Chrome)", "AppleWebKit" in eng["ua"] and "Chrome" not in eng["ua"], eng["ua"])
    check("CSS :has is supported (the bin's lower pane uses it)", eng["has"], eng)
    check("loopback is a secure context, so the async clipboard API exists", eng["secure"] and eng["clip"], eng)
    d.js("window.__errs = []; addEventListener('error', e => __errs.push(String(e.message))); "
         "addEventListener('unhandledrejection', e => __errs.push('rejection: ' + String(e.reason && e.reason.message || e.reason)));"
         "window.__playFails = []; const p0 = HTMLMediaElement.prototype.play; HTMLMediaElement.prototype.play = function () "
         "{ const r = p0.call(this); r && r.catch && r.catch(e => __playFails.push(e.name + ': ' + e.message)); return r; }; return 1")
    body = d.js("return document.body.innerText")
    check("empty studio: no 'null' text", "null" not in body, body[:160])
    lower = d.js("return getComputedStyle(document.querySelector('#bin-lower')).display")
    check("empty lower bin pane hidden by its :has() rule", lower == "none", lower)
    d.snap("w-empty")

    # --- add footage ---
    t0 = time.time()
    d.drop(files)
    st = wait(lambda: (lambda s: s if len(s["media"]) == 3 else None)(api("/api/state")), 90)
    check(f"{d.name}: dropping 3 files adds them and makes a project",
          st and any(p["slug"] != "inbox" for p in st["projects"].values()), st and list(st["projects"]))
    done = wait(lambda: (lambda s: all(all(k in m["steps"] for k in ("proxy", "transcribe", "keyframes"))
                                       for m in s["media"].values()) and not s["jobs"] and s)(api("/api/state")), 150)
    check("previews, words and frames made for all 3", bool(done), round(time.time() - t0, 1))
    S = api("/api/state")
    ids = {m["name"]: m["id"] for m in S["media"].values()}
    sw = ids.get("site-walk.mov")

    # --- H.264 proxy playback ---
    if mute:   # Safari's autoplay policy lets a script start only muted media
        d.js(f"{MODS} if (!player.muted) player.toggleMute(); return 1")
    pb = d.js(f"""{SLEEP} {MODS}
      player.previewSource('{sw}', 1, 8); await sleep(1800);
      const v = player.a; const r = {{src: v.currentSrc, rs: v.readyState, t: v.currentTime, w: v.videoWidth, h: v.videoHeight,
        paused: v.paused, err: v.error && v.error.code}}; player.pause(); return r;""", 20)
    check("H.264 proxy plays in WebKit (decoded frames, time advancing)",
          pb and "proxy.mp4" in pb["src"] and pb["rs"] >= 2 and pb["w"] > 0 and pb["t"] > 1.5 and not pb["err"], pb)
    d.snap("w-preview")

    # --- fastSeek vs exact ---
    fs = d.js(f"""{SLEEP} {MODS}
      const v = player.a; player.peek('{sw}', 3.37);
      for (let i = 0; i < 40 && (v.seeking || v._seekingTo != null); i++) await sleep(50);
      const exact = v.currentTime; player.exitSource(false); return {{exact, fast: typeof v.fastSeek}};""", 20)
    check("trim preview seeks exactly (not to WebKit's fastSeek keyframe)", fs and abs(fs["exact"] - 3.37) < 0.05, fs)

    # --- find moments, add two to the story ---
    d.click("#btn-find-all")
    st = wait(lambda: (lambda s: all("draft" in m["steps"] for m in s["media"].values() if m["has_audio"])
                       and not s["jobs"] and s)(api("/api/state")), 120)
    check("Find moments in all: moments + drafts for every file", bool(st))
    time.sleep(1.5)
    d.js(f"{MODS} st.focusMedia('{sw}'); return 1"); time.sleep(0.8)
    d.js("[...document.querySelectorAll('#moments .moment .btn.primary')].slice(0, 2).forEach(b => b.click()); return 1")
    time.sleep(1.2)
    slug = d.js("return window.__studio.S.slug")
    tl = api("/api/state")["projects"][slug]["timeline"]
    check("two moments added to the story", len(tl["clips"]) == 2, tl)

    # WebKit suspends requestAnimationFrame for a page nobody can see (display asleep). The
    # player, timeline gestures and remote-edit toasts all run on frames, so their checks are
    # only judged when frames run.
    frames = d.js("return await new Promise(r => { requestAnimationFrame(() => r(true)); setTimeout(() => r(false), 1500); })")
    if not frames:
        print("INFO animation frames are suspended (display asleep): playback, drag and live-toast checks not judged")

    if frames:   # (each step below waits on animation frames)
        # --- gapless across a cut (two <video>s, swapped at the cut) ---
        # Played twice: the first pass calibrates how far ahead the pre-roll must aim on this
        # engine (player.lead, learned at each cut); the second pass is the one judged.
        gl = d.js(f"""{SLEEP} {MODS}
          const [a, b] = st.S.tl.clips; let tl = ops.trim(st.S.tl, a.id, a.out - 2.5, a.out); st.commit(tl, 'test trim');
          tl = ops.trim(st.S.tl, b.id, b.in, b.in + 3); st.commit(tl, 'test trim');
          const pass = async () => {{ await sleep(600); st.setPlayhead(0, {{from: 'test'}}); await sleep(600);
            const act0 = player.act, samples = [], t0 = performance.now(); player.play();
            await new Promise(res => {{ const f = () => {{ samples.push([performance.now() - t0, st.S.playhead, player.act]);
              if (st.S.playhead > 4.2 || performance.now() - t0 > 9000 || !st.S.playing) res(); else requestAnimationFrame(f); }}; requestAnimationFrame(f); }});
            player.pause(); return [act0, samples]; }};
          // the freeze at the cut: from the last frame before it to the first one past the next
          // clip's in point
          const cutMs = smp => {{ const k = smp.findIndex(s => s[1] >= 2.5); const pre = smp.slice(0, k).filter(s => s[1] < 2.5).at(-1);
            const post = smp.find((s, i) => i > k && s[1] > 2.5 + 1e-3); return pre && post ? Math.round(post[0] - pre[0]) : null; }};
          const [, first] = await pass();
          const [act0, samples] = await pass();
          let stall = 0, last = null;
          for (const [t, ph] of samples) {{ if (last && ph > last[1] + 1e-4) last = [t, ph]; else if (!last && ph > 0.05) last = [t, ph];
            if (last && ph <= last[1] + 1e-4) stall = Math.max(stall, t - last[0]); }}
          const cutAt = samples.findIndex(s => s[1] >= 2.5);
          return {{swapped: cutAt > 0 && samples[cutAt][2] !== act0, crossed: samples.at(-1)[1], cut_ms: cutMs(samples),
            first_cut_ms: cutMs(first), stall_ms: Math.round(stall), lead: +player.lead.toFixed(3),
            n: samples.length, playFails: window.__playFails}};""", 60)
        check("story plays across the cut on the second <video> (swap, no rejected play())",
              gl and gl["swapped"] and gl["crossed"] > 3.0 and not gl["playFails"], gl)
        if screen_locked():
            print(f"INFO gapless timing not judged: the screen is locked, so WebKit throttles media clocks "
                  f"(measured anyway: {gl})")
        else:
            check("...gaplessly: no freeze at the cut once calibrated (next frame within 120 ms)",
                  gl and gl["cut_ms"] is not None and gl["cut_ms"] <= 120, gl)

        # --- pointer drag on the timeline (trim the last clip's end) ---
        before = d.js("const c = window.__studio.S.tl.clips; return c[c.length - 1].out")
        last = d.js("return window.__studio.S.tl.clips.length - 1")
        d.drag(f'#tl-track .clip[data-index="{last}"] .handle.r', -40)
        time.sleep(1.0)
        after = d.js("const c = window.__studio.S.tl.clips; return c[c.length - 1].out")
        check(f"{d.name}: dragging a clip's edge on the timeline trims it", after is not None and after < before - 0.1, (before, after))

    # --- the page's own drag and drop: a moment onto the Story ---
    dn = d.js(f"""{SLEEP}
      const n0 = window.__studio.S.tl.clips.length; const sc = document.querySelector('#tl-scroll'); const r = sc.getBoundingClientRect();
      const dt = new DataTransfer(); dt.setData('application/x-studio-clip', JSON.stringify({{media: '{sw}', in: 30, out: 33}}));
      const o = {{dataTransfer: dt, bubbles: true, cancelable: true, clientX: r.right - 30, clientY: r.top + 40}};
      sc.dispatchEvent(new DragEvent('dragover', o)); const over = dt.types.includes('application/x-studio-clip');
      sc.dispatchEvent(new DragEvent('drop', o)); await sleep(500);
      return {{over, n0, n1: window.__studio.S.tl.clips.length}};""")
    check("a moment dragged onto the Story is added (DataTransfer custom type)", dn and dn["over"] and dn["n1"] == dn["n0"] + 1, dn)

    # --- clipboard ---
    want = d.js("return document.querySelector('#cli-line').textContent")
    saved = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    subprocess.run(["pbcopy"], input="(before)", text=True)
    d.click("#btn-copy-cli")
    got = wait(lambda: (lambda x: x if x != "(before)" else None)(subprocess.run(["pbpaste"], capture_output=True, text=True).stdout), 5, 0.2)
    subprocess.run(["pbcopy"], input=saved, text=True)
    check(f"{d.name}: Copy puts the text on the Mac's clipboard", got == want, (want, got))

    # --- Tell Claude, and Claude's CLI edits arriving live (EventSource) ---
    d.js("document.querySelector('.tab[data-tab=claude]').click(); return 1"); time.sleep(0.5)
    d.js("const t = document.querySelector('#tab-claude textarea'); t.value = 'Shorter please'; t.dispatchEvent(new Event('input'));"
         "[...document.querySelectorAll('#tab-claude button')].find(b => b.textContent.includes('Send')).click(); return 1")
    time.sleep(1.2)
    nid = json.loads(cli("notes", "--json").stdout)[-1]["id"]
    cli("say", "On it.", "--re", nid, claude=True)
    got = wait(lambda: "On it" in (d.js("return document.querySelector('#toasts').innerText") or ""), 8, 0.3)
    check("Claude's reply arrives live (SSE) as a toast", got)
    first = api("/api/state")["projects"][slug]["timeline"]["clips"][0]
    cli("timeline", "rm", slug, first["id"][:6], claude=True)
    got = wait(lambda: "Claude removed" in (d.js("return document.querySelector('#toasts').innerText") or ""), 8, 0.2)
    if frames:
        check("Claude's CLI edit shows up live: 'Claude removed …'", got)
    else:   # the edit itself must still arrive over the event stream
        check("Claude's CLI edit reaches the page live (SSE)",
              wait(lambda: d.js("return window.__studio.S.tl.clips.length") == len(api("/api/state")["projects"][slug]["timeline"]["clips"]), 8, 0.3))
    d.snap("w-claude")

    # --- export ---
    d.click("#btn-export")
    ex = wait(lambda: api("/api/state")["projects"][slug].get("exports"), 120)
    time.sleep(1.5)
    tt = d.js("return document.querySelector('#toasts').innerText")
    check("export finishes and the ready toast offers Copy post", ex and "Copy post" in tt, tt)
    d.snap("w-exported")
    errs = d.js("return window.__errs")
    check("no page errors", not errs, errs)
    return slug
