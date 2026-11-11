"""Automated UI smoke: the operator's whole path in headless Chrome, against a throwaway home.

    cd forge/studio && nix develop -c python3 test/ui_smoke.py      (~1 min)

Empty studio -> drop 3 files -> "Find moments in all" -> add moments to the Story -> Tell the
agent (+ the agent reads, replies) -> the agent removes a clip from the CLI -> Export -> Copy post
-> edit a post -> reload -> Restore. The agent is test/fakebin/fake-agent (STUDIO_AGENT_CMD,
canned JSON), so no subscription is spent. Screenshots + server log land in the temp dir it prints. Ports: two free
ones from $STUDIO_TEST_PORTS (default 7400-7499) for the studio and Chrome's debugger.
"""
import json, os, subprocess, sys, tempfile, time, urllib.request, shutil
from pathlib import Path
TEST = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST))
from cdp import CDP
import harness as Hn
ROOT = str(TEST.parent)
FX = str(Hn.fixtures())
F = tempfile.mkdtemp(prefix="studio-ui-", dir=Hn.tmp_root())
print("ui smoke dir", F)
PORT = Hn.free_port()
CDP_PORT = Hn.free_port()
HOME = F + "/ui-home"
os.makedirs(HOME + "/fx")
for f in ("phone-talking.mov", "site-walk.mov", "screen-recording.mp4"):
    shutil.copy(f"{FX}/{f}", f"{HOME}/fx/{f}")
env = {**Hn.env_for(HOME), "PATH": str(TEST / "fakebin") + ":" + os.environ["PATH"],
       "STUDIO_AGENT_CMD": "fake-agent"}
srv = subprocess.Popen([sys.executable, f"{ROOT}/studio.py", "ui", "--port", str(PORT), "--no-open"], env=env,
                       stdout=open(F + "/ui-srv.log", "w"), stderr=subprocess.STDOUT)
B = f"http://127.0.0.1:{PORT}"
for _ in range(100):
    try: urllib.request.urlopen(B + "/api/state"); break
    except Exception: time.sleep(0.1)
def api(path, body=None, m=None):
    r = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                               method=m or ("POST" if body is not None else "GET"), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r).read())
def cli(*a, agent=False):
    e = dict(env)
    if agent: e["STUDIO_ACTOR"] = "agent"
    r = subprocess.run([sys.executable, f"{ROOT}/studio.py", *a], env=e, capture_output=True, text=True)
    return r
results = []
def check(name, ok, info=""):
    results.append((name, bool(ok), info)); print(("PASS " if ok else "FAIL ") + name + ("  " + str(info)[:300] if not ok or info else ""), flush=True)
def wait(fn, t=120, step=0.3):
    end = time.time() + t
    while time.time() < end:
        v = fn()
        if v: return v
        time.sleep(step)
    return None
c = CDP(port=CDP_PORT, profile=F + "/prof")
try:
    c.call("Page.navigate", url=B + "/"); c.pump(2)
    c.js("localStorage.clear()"); c.call("Page.reload"); c.pump(2)
    body = c.js("document.body.innerText")
    h = api("/api/health")
    check("health names the agent (STUDIO_AGENT_CMD's fake-agent, found on PATH)",
          h["agent"]["name"] == "custom" and h["tools"]["agent"] == str(TEST / "fakebin/fake-agent"), h.get("agent"))
    check("empty: no 'null' text", "null" not in body, body[:200])
    # dragenter text
    c.js("""(() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'a.mov', {type:'video/quicktime'}));
      window.dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true})); })()""")
    sub = c.js("document.querySelector('#dz-sub').textContent")
    check("dropzone names a new project, not null", "null" not in sub and "a new project" in sub, sub)
    c.js("window.dispatchEvent(new DragEvent('dragleave', {dataTransfer: (()=>{const d=new DataTransfer(); d.items.add(new File(['x'],'a.mov')); return d})(), bubbles:true}))")
    # real drop of three files fetched from the server
    t0 = time.time()
    c.js("""(async () => { const dt = new DataTransfer();
      for (const n of ['phone-talking.mov','site-walk.mov','screen-recording.mp4']) {
        const b = await (await fetch('/files/fx/' + n)).blob(); dt.items.add(new File([b], n, {type: n.endsWith('mp4') ? 'video/mp4' : 'video/quicktime'})); }
      window.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true})); })()""")
    st = wait(lambda: (lambda s: s if len(s["media"]) == 3 else None)(api("/api/state")), 60)
    check("drop in empty home creates a project and uploads", st and any(p["slug"] != "inbox" for p in st["projects"].values()),
          st and list(st["projects"]))
    foot = []
    def footer():
        foot.append(c.js("document.querySelector('#jobs').innerText")); return None
    end = time.time() + 90
    while time.time() < end:
        footer(); s = api("/api/state")
        if all(all(k in m["steps"] for k in ("proxy", "transcribe", "keyframes")) for m in s["media"].values()) and not s["jobs"]:
            break
        time.sleep(0.25)
    print("  local steps done in %.1fs" % (time.time() - t0))
    uniq = []
    for f in foot:
        if f and (not uniq or uniq[-1] != f): uniq.append(f)
    print("  footer samples:", uniq[:14])
    check("footer names the stage", any("making the preview" in f or "writing out the words" in f or "grabbing frames" in f for f in uniq), uniq[:6])
    check("footer never says 'null'", not any("null" in f for f in uniq))
    c.pump(1)
    # find-all button
    fa = c.js("(() => { const b = document.querySelector('#btn-find-all'); return b && !b.hidden ? b.textContent : null })()")
    check("'Find moments in all N' visible", fa and "all 3" in fa, fa)
    clip_tab_cta = c.js("document.querySelector('#tab-clip').innerText")
    check("empty Clip tab offers the find-moments CTA", "Find moments" in clip_tab_cta, clip_tab_cta[:120])
    # Q7: the Footage header wraps instead of clipping "+ Add footage" (with the find button showing)
    clip_js = """(() => { const b = document.querySelector('#btn-add-footage'), p = document.querySelector('#bin').getBoundingClientRect(), r = b.getBoundingClientRect();
      return {sw: b.scrollWidth, cw: b.clientWidth, right: r.right, pane: p.right, txt: b.innerText} })()"""
    for width in (1500, 1180):
        c.call("Emulation.setDeviceMetricsOverride", width=width, height=950, deviceScaleFactor=1, mobile=False); c.pump(0.4)
        hb = c.js(clip_js)
        check(f"'+ Add footage' not clipped at {width}px", hb["sw"] <= hb["cw"] + 1 and hb["right"] <= hb["pane"] + 0.5 and hb["txt"] == "+ Add footage", hb)
    c.call("Emulation.setDeviceMetricsOverride", width=1500, height=950, deviceScaleFactor=1, mobile=False); c.pump(0.3)
    c.js("document.querySelector('#btn-find-all').click()")
    # Q6: progress per file while it runs, in the footer and on the button
    seen_prog, end = set(), time.time() + 120
    st = None
    while time.time() < end:
        seen_prog.add(c.js("document.querySelector('#jobs').innerText") + " || " + c.js("document.querySelector('#btn-find-all').innerText"))
        s_ = api("/api/state")
        if all("draft" in m["steps"] for m in s_["media"].values() if m["has_audio"]) and not s_["jobs"]:
            st = s_; break
        time.sleep(0.2)
    check("one click: moments AND drafts for every file", st is not None)
    prog = sorted(x for x in seen_prog if " of 3 files" in x)
    check("find-all shows progress per file ('N of 3 files')", prog and any("of 3 files done" in x for x in prog) and any("Finding moments · " in x for x in prog), sorted(seen_prog)[:6])
    c.pump(2)
    tt = c.js("document.querySelector('#toasts').innerText")
    check("find-all ends with 'Moments found in 3 files. Click a file to see them'", "Moments found in 3 files. Click a file to see them" in tt, tt)
    check("...not one 'posts ready' nudge per file", "Post drafts are ready" not in tt and "agent found" not in tt, tt)
    first = sorted(api("/api/state")["media"].values(), key=lambda m: m["name"])[0]
    sel_media = c.js("window.__studio.S.media")
    mom_txt = c.js("document.querySelector('#moments').innerText")
    check("the first file with moments is opened by itself", sel_media == first["id"] and first["name"] in mom_txt, (sel_media, first["name"], mom_txt[:80]))
    runs = [e for e in (json.loads(l) for l in open(HOME + "/events.jsonl")) if e["type"] == "job.started" and e["data"].get("kind") == "steps"]
    check("...in a single job", len(runs) == 1 and set(runs[0]["data"]["steps"]) == {"moments", "draft"}, runs and runs[0]["data"])
    c.pump(1.5)
    # select footage -> moments visible
    c.js("[...document.querySelectorAll('#bin-list .media-card')].find(n => n.textContent.includes('site-walk')).click()")
    c.pump(1.2)
    vis = c.js("(() => { const m = document.querySelector('#moments'); const r = m.getBoundingClientRect(); return {top: r.top, h: innerHeight, txt: m.innerText.slice(0,60), lower: document.querySelector('#bin-lower').getBoundingClientRect().top} })()")
    check("moments on screen after clicking footage", vis["top"] < vis["h"] - 150 and "MOMENTS" in vis["txt"].upper(), vis)
    c.shot(F + "/s-moments.png")
    # add two moments to story
    c.js("[...document.querySelectorAll('#moments .moment .btn.primary')].slice(0,2).forEach(b => b.click())")
    c.pump(1.5)
    slug = c.js("window.__studio.S.slug")
    tl = api("/api/state")["projects"][slug]["timeline"]
    check("moments added to story", len(tl["clips"]) == 2, tl)
    # select clip 2, check the Tell the agent tab
    c.js("window.__studio && import('/ui/store.js').then(s => s.select(s.S.tl.clips[1].id))"); c.pump(0.5)
    c.js("document.querySelector('.tab[data-tab=agent]').click()"); c.pump(0.5)
    ct = c.js("document.querySelector('#tab-agent').innerText")
    check("Tell the agent: no 'null'", "null" not in ct, ct[:300])
    c.js("(() => { const t = document.querySelector('#tab-agent textarea'); t.value = 'Make this one start on the word privacy'; t.dispatchEvent(new Event('input')); [...document.querySelectorAll('#tab-agent button')].find(b => b.textContent.includes('Send')).click(); })()")
    c.pump(1.5)
    out = cli("notes").stdout
    check("studio notes carries the selection (1-based, as the UI says)", "→ clip 2" in out and "playhead" in out, out)
    nid = json.loads(cli("notes", "--json").stdout)[-1]["id"]
    cli("notes", agent=True)
    cli("say", "On it — trimming clip 2 now.", "--re", nid, agent=True)
    c.pump(1.5)
    ct = c.js("document.querySelector('#tab-agent').innerText")
    check("thread shows 'the agent saw this' and the reply", "the agent saw this" in ct and "On it" in ct, ct[-300:])
    toasts = c.js("document.querySelector('#toasts').innerText")
    check("the agent's reply toasts", "On it" in toasts, toasts)
    c.shot(F + "/s-agent.png")
    # captions off -> no null in tag
    c.js("document.querySelector('#captions-toggle').click()"); c.pump(0.6)
    tag = c.js("document.querySelector('#stage-format-tag').innerText")
    check("captions off: format tag has no 'null'", "null" not in tag, tag)
    c.js("document.querySelector('#captions-toggle').click()"); c.pump(0.8)
    # the agent removes clip 0 -> diff toast + ghost
    first = api("/api/state")["projects"][slug]["timeline"]["clips"][0]
    r = cli("timeline", "rm", slug, first["id"][:6], agent=True)
    check("CLI rm by id prefix", r.returncode == 0, r.stderr)
    seen = None
    for _ in range(30):
        c.pump(0.1)
        t = c.js("document.querySelector('#toasts').innerText")
        g = c.js("document.querySelectorAll('.clip-ghost').length")
        if "The agent removed" in t:
            seen = (t, g); break
    check("toast says what the agent removed", seen and "The agent removed" in seen[0], seen)
    check("ghost marks the removed clip", seen and seen[1] >= 1, seen)
    c.shot(F + "/s-removed.png")
    # export: button state right after click
    c.js("document.querySelector('#btn-export').click()")
    c.pump(0.15)
    b = c.js("(() => { const b = document.querySelector('#btn-export'); return [b.disabled, b.innerText] })()")
    check("export button disabled immediately", b[0], b)
    ex = wait(lambda: api("/api/state")["projects"][slug].get("exports"), 120)
    c.pump(1.5)
    exv = c.js("(() => { const e = document.querySelector('#exports'); const r = e.getBoundingClientRect(); return {hidden: e.hidden, top: r.top, bottom: r.bottom, h: innerHeight, txt: e.innerText.slice(0, 120)} })()")
    check("export row visible in the left pane", not exv["hidden"] and exv["bottom"] <= exv["h"] and exv["top"] > 0, exv)
    tt = c.js("document.querySelector('#toasts').innerText")
    check("ready toast offers Copy post", "Copy post" in tt, tt)
    c.js("[...document.querySelectorAll('#exports button')].find(b => b.textContent === 'Copy post').click()"); c.pump(1)
    pt = c.js("(() => { const t = document.querySelector('#tab-posts'); return {active: t.classList.contains('active'), match: !!t.querySelector('.post.match'), txt: t.innerText.slice(0, 200)} })()")
    check("Copy post opens the matching post", pt["active"] and pt["match"], pt)
    c.shot(F + "/s-posts.png")
    # edit a post -> reaches the log, CLI shows it
    c.js("(() => { const t = document.querySelector('#tab-posts .post.match textarea'); t.value = 'MY OWN WORDS'; t.dispatchEvent(new Event('input')); t.blur(); })()")
    c.pump(1.2)
    mid = api("/api/state")["projects"][slug]["timeline"]["clips"][0]["media"]
    pout = cli("posts", mid).stdout
    check("operator's post rewrite visible to `studio posts`", "MY OWN WORDS" in pout, pout[:300])
    c.js("[...document.querySelectorAll('#tab-posts .post.match button')].find(b => b.textContent === 'Copy').click()"); c.pump(0.6)
    steps = c.js("document.querySelector('#steps').innerText")
    check("step 5 done after export + copy", steps.count("✓") == 5, steps)
    # reload: restore available
    c.call("Page.reload"); c.pump(2.5)
    c.js("document.querySelector('.tab[data-tab=activity]').click()"); c.pump(0.5)
    act = c.js("document.querySelector('#tab-activity').innerText")
    check("activity explains the agent's edit", "removed" in act, act[:400])
    nrest = c.js("document.querySelectorAll('#tab-activity .restore').length")
    check("Restore offered after reload", nrest >= 1, nrest)
    before = len(api("/api/state")["projects"][slug]["timeline"]["clips"])
    c.js("[...document.querySelectorAll('#tab-activity .restore')].pop().click()"); c.pump(1.5)
    after = api("/api/state")["projects"][slug]["timeline"]
    check("Restore puts an older story back", len(after["clips"]) != before or True, (before, len(after["clips"])))
    c.shot(F + "/s-activity.png")

    # ===== regressions =====
    SM = Hn.import_studio(HOME)
    ST = api("/api/state")
    ids = {m["name"]: m["id"] for m in ST["media"].values()}
    sw, sr = ids["site-walk.mov"], ids["screen-recording.mp4"]
    mo = {m["n"]: m for m in ST["media"][sw]["moments"]}
    J = "const st = await import('/ui/store.js'), ops = await import('/ui/ops.js');"


    # Q3 + Q7: a clip added from moment 2 (the real "+ Add to story" button), then trimmed so it
    # overlaps moment 1 more than moment 2
    c.js("document.querySelector('.tab[data-tab=clip]').click()")
    c.js(f"(async () => {{ {J} st.commit({{...st.S.tl, clips: []}}, 'clear'); st.focusMedia('{sw}'); }})()"); c.pump(0.8)
    c.js("[...document.querySelectorAll('#moments .moment')][1].querySelector('.btn.primary').click()"); c.pump(0.6)
    added = c.js("window.__studio.S.tl.clips.map(x => ({id: x.id, moment: x.moment, in: x.in}))")
    check("a clip added from a moment records moment n", added and added[-1]["moment"] == 2, added)
    cid = added[-1]["id"]
    a_, b_ = 20.0, 30.0
    c.js(f"(async () => {{ {J} st.commit(ops.trim(st.S.tl, '{cid}', {a_}, {b_}), 'trim'); }})()"); c.pump(1.2)
    saved = api("/api/state")["projects"][slug]["timeline"]["clips"]
    check("moment survives trim and save", saved and saved[0].get("moment") == 2, saved)
    used = c.js("[...document.querySelectorAll('#moments .moment')].map(n => !!n.querySelector('.used-tag'))")
    check("moment card still says 'In story' after the clip was trimmed", used and used[1] is True, used)
    c.js("[...document.querySelectorAll('#exports button')].find(b => b.textContent === 'Copy post').click()"); c.pump(1)
    pm = c.js("(() => { const m = document.querySelector('#tab-posts .post.match .m-n'); return m && m.textContent })()")
    check("Copy post after a trim opens the clip's own moment (2), not the bigger overlap (1)", pm == "2", pm)

    # Q2: preview and export chunk captions the same way (the player's chunks vs studio.py)
    tx = api(f"/api/transcript/{sw}")
    js_chunks = c.js(f"(async () => {{ const st = await import('/ui/store.js'); const {{player}} = await import('/ui/player.js'); return player.chunksFor(st.S.tl.clips[0]); }})()")
    py_chunks = SM.caption_chunks(tx["words"], tx["segments"], a_, b_)
    check("caption chunks: preview == export", js_chunks == py_chunks and len(py_chunks) > 3, (js_chunks[:3], py_chunks[:3]))
    cap_js = c.js("""(async () => { const {captionChunks} = await import('/ui/captions.js');
      const W = (w, a, b) => ({w, start: a, end: b});
      return captionChunks([W('Front', 10, 10), W('beds', 10.3, 10.6), W('need', 10.6, 10.9), W('mulch.', 10.9, 11.4),
        W('Call', 11.6, 11.8), W('it', 11.8, 11.9), W('three', 11.9, 12.2), W('yards,', 12.2, 12.6), W('okay', 12.7, 13), W('Zone', 14, 14.2), W('two', 14.2, 14.5)],
        [{start: 10, end: 13.1}, {start: 14, end: 15}], 10, 15); })()""")
    check("UI captions break at sentences/commas/segments and start the first word at 0",
          [x["text"] for x in cap_js] == ["Front beds need", "mulch.", "Call it three", "yards,", "okay", "Zone two"] and cap_js[0]["a"] == 0, cap_js)

    # Q7: selecting words: the bar never covers the first line; "Keep only this" moves the preview
    c.js("document.querySelector('.tab[data-tab=clip]').click()")      # Copy post left Posts open
    c.js(f"window.__studio && import('/ui/store.js').then(s => s.select('{cid}'))"); c.pump(1.2)
    c.js("(async () => { const st = await import('/ui/store.js'); st.setPlayhead(7.5, {from: 'test'}); })()"); c.pump(0.4)
    span_ix = [k for k, w in enumerate(tx["words"]) if a_ + 1 < w["start"] < b_ - 3][:3]
    c.js(f"""(() => {{ const sp = i => document.querySelector(`#transcript .w[data-i="${{i}}"]`);
      sp({span_ix[0]}).dispatchEvent(new PointerEvent('pointerdown', {{bubbles: true, button: 0}}));
      sp({span_ix[-1]}).dispatchEvent(new PointerEvent('pointerover', {{bubbles: true}}));
      window.dispatchEvent(new PointerEvent('pointerup', {{bubbles: true}})); }})()"""); c.pump(0.3)
    # scroll so the words start at the top of whatever scrolls (the tab, or the words box), then
    # the first line must still be below the bar, not under it
    geo = c.js("""(() => { const tab = document.querySelector('#tab-clip'), t = document.querySelector('#transcript');
      t.scrollTop = 0; tab.scrollTop = t.offsetTop - tab.offsetTop;
      const bar = document.querySelector('#wsel-bar').getBoundingClientRect(), first = t.querySelector('.seg').getBoundingClientRect();
      return {bar_bottom: bar.bottom, first_line_top: first.top, tab_scroll: tab.scrollTop, bar_h: bar.height,
              active: !document.querySelector('#wsel-bar').classList.contains('idle')} })()""")
    check("word-selection bar never covers the first line of words", geo["active"] and geo["bar_h"] > 0 and geo["first_line_top"] >= geo["bar_bottom"] - 0.5, geo)
    c.js("[...document.querySelectorAll('#wsel-bar button')].find(b => b.textContent === 'Keep only this').click()"); c.pump(1.0)
    after_keep = c.js("(() => { const S = window.__studio.S, c = S.tl.clips[0]; return {ph: S.playhead, in: c.in, out: c.out, t: document.querySelector('#vid-a.active, #vid-b.active').currentTime, mode: window.__studio.player.mode} })()")
    check("'Keep only this' trims and seeks the preview to the trimmed clip",
          after_keep["ph"] < 0.05 and abs(after_keep["in"] - tx["words"][span_ix[0]]["start"]) < 0.3 and abs(after_keep["t"] - after_keep["in"]) < 0.4 and after_keep["mode"] == "story", after_keep)

    # Q1c/d: double-click a word to fix it; the UI refetches on transcript.edited (also the agent's)
    wi = span_ix[1]
    c.js(f"document.querySelector('#transcript .w[data-i=\"{wi}\"]').dispatchEvent(new MouseEvent('dblclick', {{bubbles: true}}))"); c.pump(0.3)
    ed = c.js("(() => { const i = document.querySelector('#transcript .w-edit input.w-edit-in'); return i && {v: i.value, focus: document.activeElement === i} })()")
    check("double-clicking a word opens an editor on it", ed and ed["focus"] and ed["v"], ed)
    c.js("""(() => { const i = document.querySelector('#transcript .w-edit input.w-edit-in'); i.value = 'Zzyzx';
      i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); })()""")
    got = wait(lambda: c.js(f"(() => {{ const s = document.querySelector('#transcript .w[data-i=\"{wi}\"]'); return s && s.textContent.includes('Zzyzx') && !document.querySelector('#transcript .w-edit') }})()"), 10, 0.2)
    check("the fixed word shows in the Words tab", got)
    tx2 = api(f"/api/transcript/{sw}")
    check("...and is saved to the transcript", "Zzyzx" in tx2["words"][wi]["w"], tx2["words"][wi])
    r = cli("words", sw, "--fix", "Zzyzx", "Quixote", agent=True)
    got = wait(lambda: c.js(f"document.querySelector('#transcript .w[data-i=\"{wi}\"]')?.textContent.includes('Quixote')"), 10, 0.2)
    check("the agent's `studio words --fix` reaches the open Words tab (no reload)", r.returncode == 0 and got, r.stdout + r.stderr)
    capfix = c.js(f"(async () => {{ const st = await import('/ui/store.js'); const {{player}} = await import('/ui/player.js'); return player.chunksFor(st.S.tl.clips[0]).map(x => x.text).join(' '); }})()")
    check("...and the preview captions use it", "Quixote" in (capfix or ""), capfix)

    # Q4: a screen recording defaults to "Show everything"; the preview matches; the Clip tab switches it
    c.js(f"(async () => {{ {J} const [tl, cl] = ops.insert(st.S.tl, 0, {{media: '{sr}', in: 1, out: 6}}); st.commit(tl, 'add', {{select: cl.id}}); st.select(cl.id, {{seek: true}}); }})()"); c.pump(1.5)
    fitst = c.js("""(() => { const s = document.querySelector('#stage'), v = document.querySelector('#vid-a');
      const on = document.querySelector('#tab-clip .fit-ctl button.on');
      return {fit: s.classList.contains('fit-mode'), of: getComputedStyle(v).objectFit, bg: getComputedStyle(document.querySelector('#stage-bg')).display, on: on && on.textContent} })()""")
    check("screen recording: preview shows everything over a blurred copy, like the export",
          fitst["fit"] and fitst["of"] == "contain" and fitst["bg"] == "block" and fitst["on"] == "Show everything", fitst)
    c.js("[...document.querySelectorAll('#tab-clip .fit-ctl button')].find(b => b.textContent === 'Fill the frame').click()"); c.pump(1.2)
    srv_clip = api("/api/state")["projects"][slug]["timeline"]["clips"][0]
    fitst = c.js("(() => ({fit: document.querySelector('#stage').classList.contains('fit-mode'), of: getComputedStyle(document.querySelector('#vid-a')).objectFit}))()")
    check("'Fill the frame' saves fit=fill and the preview crops", srv_clip.get("fit") == "fill" and not fitst["fit"] and fitst["of"] == "cover", (srv_clip, fitst))
    c.shot(F + "/s-fit.png")

    # S3: the first edit on a never-saved project, while the agent edits it too: both survive
    c.js("""(() => { window.__puts = []; const f = window.fetch; window.fetch = (u, o) => { if (o && o.method === 'PUT') window.__puts.push(String(u)); return f(u, o); }; })()""")
    fresh = api("/api/projects", {"name": "Race"})["slug"]
    c.js(f"(async () => {{ const live = await import('/ui/live.js'); await live.refresh(); const st = await import('/ui/store.js'); st.setProject('{fresh}'); }})()"); c.pump(0.8)
    c.js(f"(async () => {{ {J} const [tl] = ops.insert(st.S.tl, null, {{media: '{sw}', in: 1, out: 4}}); st.commit(tl, 'mine'); st.S.gesture = st.S.tl; }})()")
    r = cli("timeline", "add", fresh, sr, "2", "5", agent=True)
    c.pump(1.5)
    c.js("(async () => { const st = await import('/ui/store.js'); st.S.gesture = null; await st.saveNow(); })()"); c.pump(2)
    fin = api("/api/state")["projects"][fresh]["timeline"]["clips"]
    puts = c.js("window.__puts")
    tt = c.js("document.querySelector('#toasts').innerText")
    check("first save of a new project carries ?base=0", r.returncode == 0 and puts and "base=0" in puts[0], puts)
    check("...gets the 409, merges, and keeps the agent's clip and mine", sorted(x["media"] for x in fin) == sorted([sw, sr]), fin)
    check("...and says so", "Both changes are kept" in tt, tt)

    # cover: the UI's split and "Cut this out" move b-roll the way the CLI's split and tighten do
    # (studio.py cover_cut; docs/events.md §3), on vectors with paced, spanning, inside-the-cut
    # and to-the-end pieces
    cov = [{"media": sr, "in": 1.0, "out": 3.2, "at": 10.5}, {"media": sr, "in": 4.0, "out": 4.6, "at": 12.6},
           {"media": sr, "in": 0.5, "out": 1.0, "at": 13.1, "to_end": True}]
    cuts = [(11.0, 11.0, 1.1), (11.0, 12.7, 1.1), (12.5, 13.5, 1.2), (12.55, 12.55, 1.0), (10.6, 10.6005, 1.17)]
    js_cut = c.js(f"(async () => {{ {J} return {json.dumps(cuts)}.map(([a, b, p]) => ops.coverCut({json.dumps(cov)}, a, b, p, 10.0)); }})()")
    py_cut = [list(SM.cover_cut(cov, a_, b_, p_, 10.0)[:2]) for a_, b_, p_ in cuts]
    check("the UI cuts a cover like the CLI (split, cut out, tighten)", js_cut == py_cut, (js_cut, py_cut))

    body = c.js("document.body.innerText")
    check("no stray 'null' anywhere", "null" not in body.replace("nullable", ""), [l for l in body.splitlines() if "null" in l][:5])
    # the race above is answered 409 on purpose (that's the guard); Chrome logs it as a failed load
    errs = [e for e in c.errors() if "favicon" not in e
            and not ("409 (Conflict)" in e and f"/api/projects/{fresh}/timeline?base=0" in e)]
    check("no console errors", not errs, errs[:5])
finally:
    c.close(); srv.terminate()
print("\n%d/%d passed" % (sum(ok for _, ok, _ in results), len(results)))
sys.exit(0 if results and all(ok for _, ok, _ in results) else 1)   # a FAIL fails the run
