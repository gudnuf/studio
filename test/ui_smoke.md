# studio UI smoke + perf script

A step-by-step script for a human or a browser-driving agent (Claude in Chrome) to check that
the desktop studio feels smooth for a novice and stays in live sync with Claude. Run it after
`test/run_all.sh` passes. Element ids are taken from `ui/index.html` at the time of writing.
If an id has moved, find the control by its tooltip text (`data-tip`) and update this file.

Never point this at `~/Studio`. Use a throwaway home and a port in the 7400-7499 range.

## 0. Setup

```bash
cd ~/athanor/forge/studio
export T=$(mktemp -d)                 # or a scratchpad dir
nix develop -c test/fixtures.sh "$T/fx"                       # cached after first run
nix develop -c python3 test/seed.py "$T/seeded" --fixtures "$T/fx"   # media + moments + drafts + 4-clip story
nix develop -c python3 test/seed.py "$T/empty" --bare                # first-run home
STUDIO_HOME="$T/seeded" ./studio ui --port 7450 --no-open &   # seeded run
STUDIO_HOME="$T/empty"  ./studio ui --port 7451 --no-open &   # novice first-run
```

Open `http://127.0.0.1:7451/` (empty) and `http://127.0.0.1:7450/` (seeded) in Chrome, with the
window at about 1500x960. That is the size `open_app_window` uses.

### Perf probe (paste into the DevTools console, or run with `javascript_tool`, on each tab)

```js
(() => {
  const P = window.__perf = { long: [], frames: [], marks: {}, sse: [] };
  try {
    new PerformanceObserver(l => l.getEntries().forEach(e =>
      P.long.push({ t: Math.round(e.startTime), ms: Math.round(e.duration) })))
      .observe({ type: 'longtask', buffered: true });
  } catch (e) { P.longUnsupported = String(e); }
  // frame pacing: record every rAF delta while __perf.recording is true
  let last = performance.now();
  (function loop(now) { if (P.recording) P.frames.push(now - last); last = now; requestAnimationFrame(loop); })(last);
  P.rec = () => { P.frames = []; P.long = []; P.recording = true; };
  P.stop = () => {
    P.recording = false;
    const f = P.frames.slice().sort((a, b) => a - b), q = p => f[Math.floor((f.length - 1) * p)] || 0;
    return { frames: f.length, fps: +(1000 / (f.reduce((a, b) => a + b, 0) / f.length || 1)).toFixed(1),
             p95_frame_ms: +q(0.95).toFixed(1), worst_frame_ms: +(f[f.length - 1] || 0).toFixed(1),
             dropped: f.filter(x => x > 25).length, longtasks: P.long.length,
             worst_longtask_ms: Math.max(0, ...P.long.map(x => x.ms)) };
  };
  // click-to-paint: time from now until two frames have painted
  P.paint = label => new Promise(r => { const t0 = performance.now();
    requestAnimationFrame(() => requestAnimationFrame(() => r(P.marks[label] = +(performance.now() - t0).toFixed(1)))); });
  // time a click on an element until the next paint
  P.click = async (sel, label = sel) => { const t0 = performance.now(); document.querySelector(sel).click();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    return P.marks[label] = +(performance.now() - t0).toFixed(1); };
  // video seek latency on the visible <video>
  P.seek = async t => { const v = [...document.querySelectorAll('video')].find(v => v.offsetParent) || document.querySelector('video');
    const t0 = performance.now(); v.currentTime = t; await new Promise(r => v.addEventListener('seeked', r, { once: true }));
    return +(performance.now() - t0).toFixed(1); };
  // DOM-mutation timestamps, used to time SSE -> screen
  P.watch = sel => { P.sse = []; new MutationObserver(() => P.sse.push(performance.timeOrigin + performance.now()))
    .observe(document.querySelector(sel), { subtree: true, childList: true, attributes: true, characterData: true }); };
  const nav = performance.getEntriesByType('navigation')[0];
  const fcp = performance.getEntriesByName('first-contentful-paint')[0];
  return { domContentLoaded: Math.round(nav.domContentLoadedEventEnd), load: Math.round(nav.loadEventEnd),
           fcp: fcp && Math.round(fcp.startTime),
           state_fetch: performance.getEntriesByType('resource').filter(r => r.name.includes('/api/state'))
             .map(r => Math.round(r.duration)) };
})()
```

Reload the page, paste the probe again, and record the numbers it returns as **load**.

## Budgets (what "snappy" means)

| what | budget |
|---|---|
| first contentful paint (seeded, 4 media) | < 500 ms |
| `/api/state` fetch in the page | < 100 ms |
| click to visible feedback (any button, tab, moment) | < 100 ms (`__perf.click`) |
| scrub/seek on the proxy (`__perf.seek`) | p95 < 150 ms |
| playback, 10 s (`rec()` … `stop()`) | ≥ 58 fps, 0 long tasks > 50 ms |
| dragging a clip edge or the playhead, 5 s | p95 frame < 20 ms, ≤ 2 dropped frames |
| CLI edit to on-screen change | < 1 s (server pushes SSE in < 400 ms; see bench) |
| upload of 45 s phone clip to a playable proxy | < 6 s (bench `proxy_ready_s`) |
| export of a 28 s, 5-clip story | < 10 s with a visible progress state |

Record every number in the results table at the end of this file. A miss is a finding. It is not a reason to skip the step.

## A. Novice guided path (port 7451, empty home)

Someone who has never edited video should be able to do all of this without reading docs.

1. **First open.** You should see the empty state (`#stage-empty` / `#dropzone`), with one obvious
   call to action: "Add footage" (`#btn-add-footage`). No error toasts, and the console is clean.
   Hover the main controls: each one shows a plain-English tooltip (`#tooltip`).
2. **Add footage.** Use `#btn-add-footage` → `#file-input` (agent: `file_upload` with
   `$T/fx/phone-talking.mov`, `site-walk.mov`, `screen-recording.mp4`, `junk.txt`). You can also drag
   the files onto the window.
   - Each file shows up at once in `#uploads` / `#bin-list` with a progress indicator. The UI stays responsive while the bytes upload (`__perf.rec()` during the upload, then `stop()`: no long task > 50 ms).
   - The step chips (`#steps`) go through proxy → transcribe → keyframes in plain language.
   - `junk.txt` is rejected politely, with a toast that says it isn't video. No crash.
   - Note the time from drop until the phone clip plays in `#viewer`. Budget: < 6 s.
3. **Portrait looks portrait.** `phone-talking.mov` (iPhone, rotated by metadata) and
   `screen-recording.mp4` show upright and tall. They are not sideways and not letterboxed into landscape.
4. **Silent clip.** `screen-recording.mp4` shows no error for "transcribe". Silence is normal.
5. **Moments.** Real AI moments cost subscription, so switch to the seeded tab (7450) for steps 5-12.
   `#moments` lists moments with title + hook. Clicking one plays exactly that range in the viewer
   (`__perf.click('#moments > *')` < 100 ms; the video starts within 300 ms).
6. **Build the story.** Add a moment to the story (`#story` / `#tl-track`): drag, or use its add button.
   The clip appears with its label. `#save-status` shows saved, and `#cli-line` shows the equivalent
   `studio timeline …` command.
7. **Trim.** Drag a clip's edge in `#tl-track`. Record `rec()`/`stop()` during the drag (budget above).
   The viewer follows the trim point live. Release, and the save status settles to saved within 1 s.
8. **Split / delete / undo.** Put the playhead mid-clip and press `S` (or `#btn-split`): the clip becomes two.
   Select one and press Delete (`#btn-delete`): it is gone. `⌘Z` (`#btn-undo`) brings it back and `⇧⌘Z` redoes. Each action paints in < 100 ms.
9. **Play the story.** Space or `#tp-play`. Playback crosses clip boundaries with no black flash or
   audio pop (`#vid-a`/`#vid-b` double-buffering). `rec()` for 10 s: ≥ 58 fps, no long tasks. `J`/`L` and
   `⇧←`/`⇧→` seek. `__perf.seek(t)` ×5 at random t: p95 < 150 ms.
10. **Format.** `#btn-format` → `#format-menu` → 16:9, then 1:1, then back to 9:16. The stage reframes at
    once (`#stage-format-tag` updates), and no clip is lost from the story.
11. **Captions.** `#captions-toggle` on: `#cap-overlay` shows 3-word captions in sync with speech.
    Off: they disappear. The setting persists after reload.
12. **Export.** `#btn-export`. A progress state shows in `#jobs`, and the UI stays usable while ffmpeg runs.
    When it finishes, the file appears in `#exports`, plays in the browser, and has the chosen shape.
    Time it: budget < 10 s for the seeded 4-clip story.
13. **Posts.** `#tab-posts`: drafts are shown per moment (short / X / LinkedIn / title, plus newsletter
    and ideas). Each copy button puts the text on the clipboard (check with
    `navigator.clipboard.readText()`) and shows "copied".
14. **Leave Claude a note.** `#tab-claude`: type "tighten the open on clip 1" and send. It shows in
    the activity list. In a terminal, `STUDIO_HOME=$T/seeded ./studio notes` prints it.
15. **Reload survives.** Reload the page. The story, format, captions and selected project come back the same.

## B. Agent live-sync path (port 7450, seeded home)

Run these in a terminal next to the browser: `export STUDIO_HOME=$T/seeded`.
Before each step, run `__perf.watch('#story')` (or `#tl-track`) in the page.

1. **Connection.** `#conn` shows live (green or "connected"). The `Activity` tab (`#tab-activity`) lists
   recent events, with who did each one: you / Claude.
2. **Claude adds a clip.** Run `CLAUDECODE=1 ./studio timeline add jefe-shoot <site-walk id>@2` and
   note `date +%s.%N` as the command exits. The story gains the clip **without a reload**. The time from
   exit to the first `__perf.sse` entry is < 1 s. The activity entry is attributed to Claude, and
   `#activity-dot` pulses.
3. **Claude trims what the operator is looking at.** In the UI, select clip 0. `./studio focus` prints
   JSON naming that project/clip and the playhead. Then run `CLAUDECODE=1 ./studio timeline trim jefe-shoot 0 1 4`:
   the selected clip updates in place. The selection and the playhead are not lost, and the viewer is not reset to 0.
4. **Move / split / rm from the CLI.** Each one shows up live and in order. Undo in the UI after a Claude
   edit does something defined and visible: it either undoes the operator's own last edit only, or says what it will undo.
   Record which behaviour you saw.
5. **Simultaneous edits.** Start dragging a clip edge in the UI and, while still dragging, run a CLI
   `timeline add`. When you release, neither edit is silently lost: the final `./studio timeline show` has
   both. If one is lost, record that as a finding.
6. **Claude renders.** `CLAUDECODE=1 ./studio render jefe-shoot`. The export appears in `#exports` with
   no reload, attributed to Claude.
7. **Notes loop.** Send a note from the UI. `./studio notes -p jefe-shoot` shows it with the focus the UI
   attached.
8. **Burst.** `for i in $(seq 20); do ./studio timeline add jefe-shoot <id> 0 1; done`. The UI keeps up
   with no flicker storm (`rec()` during the burst: no long task > 100 ms) and ends with all 20 clips plus the originals.
   Then run `./studio timeline clear jefe-shoot` and the story empties live.
9. **Reconnect.** Kill the server (`kill %1`). `#conn` shows disconnected within a few seconds, and editing
   is blocked or queued with a clear message. Restart it on the same port. The UI reconnects by itself
   and catches up on edits made while it was down (run one CLI edit during the outage).

## C. Teardown

```bash
kill %1 %2 2>/dev/null; rm -rf "$T"
lsof -iTCP:7400-7499 -sTCP:LISTEN     # should print nothing
```

## Results

| step | number / observation | budget | ok? |
|---|---|---|---|
| load: FCP / state fetch | | 500 / 100 ms | |
| A2 drop → playable | | 6 s | |
| A7 drag frames p95 / dropped | | 20 ms / 2 | |
| A9 playback fps / long tasks | | 58 / 0 | |
| A9 seek p95 | | 150 ms | |
| A12 export | | 10 s | |
| B2 CLI → screen | | 1 s | |
| B8 burst worst long task | | 100 ms | |
