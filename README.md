# studio

Raw footage in; a finished vertical video and post drafts out. Everything runs on this Mac.
This is the first piece of our own Eden-style content workspace. The identity layer (keys,
approval, distribution) gets built on top of it later.

Two people work in it at once. The **operator** uses the desktop studio, which needs no
video-editing experience. **The agent**, a coding agent working as an expert editor (Claude
Code, Codex, Goose or any other), drives the same project from the terminal with the `studio`
CLI. Both read and write one event log (`~/Studio/events.jsonl`), so each side sees the
other's edits live.

## Getting started (new Mac)

You need [nix](https://nixos.org/download) with flakes enabled. To find moments and draft
posts you also need a coding agent's CLI, signed in: Claude Code (`claude`), Codex (`codex`),
Goose (`goose`), or any other that can answer a prompt from the command line (see
[Using it with your agent](#using-it-with-your-agent)). Everything else (python, ffmpeg,
whisper) comes from nix.

```bash
git clone https://github.com/gudnuf/studio ~/code/studio && cd ~/code/studio

# the speech-to-text model: 1.6 GB, stays on your Mac
mkdir -p ~/.local/share/studio/models
curl -L -o ~/.local/share/studio/models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin

nix run .#install      # builds Studio.app and puts it in ~/Applications (a few minutes the first time)
open ~/Applications/Studio.app
```

Clone it anywhere except `~/studio`: macOS folder names ignore case, so that is the same
folder as the default library, `~/Studio`.

Got a shared footage zip from a teammate? Unzip it, then use **File ▸ Import…** in the app,
or run `./studio import <unzipped folder>`. See [Sharing footage](#sharing-footage-with-a-teammate).

## Launch the desktop studio

```bash
cd ~/code/studio
./studio ui            # enters the nix devshell by itself, serves 127.0.0.1:7317 and
                       # opens a Chrome app window (1500x960)
```

`./studio ui --port 7400 --no-open` runs it without opening a window. `STUDIO_HOME=/some/dir`
uses a different library; the default is `~/Studio`. Finding moments and drafting run your
agent's non-interactive mode (`./studio agent` shows which agent and the exact command), on
whatever plan the agent is signed in with: studio needs no API key of its own.

## The Mac app

The same studio as a real Mac app (Dock icon, menus, native drag and drop), built entirely
by nix. Everything it runs is pinned by `flake.lock`: python3, ffmpeg-full and whisper-cpp are
exact store paths, and studio.py, server.py, ui/ and brands/ are copied into the store.

```bash
cd ~/code/studio
nix build .#studio-app          # → result/Applications/Studio.app (ad-hoc signed)
nix run .#install               # copy it to ~/Applications (+ a GC root, so the backend it
                                # points at survives nix-collect-garbage)
nix develop -c bash -c 'cd desktop && cargo tauri dev'   # iterate: runs ../studio.py from
                                # this tree, so UI and server edits need no rebuild
```

- **Library.** `~/Studio`, or Studio ▸ Settings… to pick another folder (remembered).
  `STUDIO_HOME` or `--home DIR` (`open Studio.app --args --home DIR`) override it.
- **One server per library.** The server that owns a library writes `$STUDIO_HOME/.server.json`
  (pid, port). The app checks it with `GET /api/health` and attaches if it answers for that
  library: run `./studio ui` in a terminal and the app shares it. Quitting the app stops only a
  server the app started. If the app crashes, its server notices (stdin closes, parent pid
  changes) and exits within a second, taking its ffmpeg/whisper/agent children with it.
- **Drops are paths, not uploads.** Files and folders dragged in from Finder (or picked with
  File ▸ Add Footage…, ⌘O) go to `POST /api/ingest-paths`. The server reads them where they are
  and clones them into the library (APFS `clonefile`: a 4 GB file takes no time and no extra
  space; other volumes get a normal copy). The original is never moved or changed. A folder
  brings in the media inside it. The usual rules apply: a file still being copied is waited
  for, unreadable files are refused, duplicates are recognised by hash.
- **The agent CLI** is your own, not nix's. The app reads your login shell's PATH
  (`$SHELL -l`), so "Find moments" works from a Dock launch; it uses the first of `claude`,
  `codex`, `goose` it finds there. To choose another, give the app `STUDIO_AGENT` /
  `STUDIO_AGENT_CMD` (`launchctl setenv STUDIO_AGENT codex`, then reopen it). If the agent
  can't be found, or Whisper's model is missing from `~/.local/share/studio/models/`, the
  footer says so.
- **Menus.** Edit ▸ Undo (⌘Z) undoes typing in a text field and the story everywhere else.
  View ▸ Zoom Story to Fit / Toggle Captions, File ▸ New Project ⌘N, Import… ⇧⌘O, Share
  Project… ⇧⌘S (both with Finder's folder picker), Export ⌘E, Show Library in
  Finder, Help ▸ README / Show Logs. Logs: `~/Library/Logs/Studio/` (app.log, server.log).
- A notification arrives when an export or "Find moments" finishes while Studio is in the
  background. The window remembers its size and place; a second launch brings up the first.
- Layout: `desktop/` (Tauri v2: `src/main.rs` window and menus, `src/backend.rs` attach or
  start, `src/dragdrop.rs` drag routing, `src/devhook.rs` test hook, `icons/make_icon.py`).

## The operator's workflow: 5 steps

The bar across the top shows these steps, with a tick on each one that's finished.

1. **Add footage.** Drag the phone videos, screen recordings and job walks anywhere onto the
   window, or use **+ Add footage**. If no project exists yet, one is created
   ("Shoot Nov 8"). The footer shows what each file is waiting for: *making the preview*,
   *writing out the words*, *grabbing frames*, or *waiting for its turn*. The first file you
   drop is the first one ready to play. If the connection drops mid-upload, the file is
   refused ("upload incomplete") rather than added half-finished; add it again.
2. **Pick moments.** Press **✦ Find moments in all N** at the top of the Footage pane, or the
   same button in the empty Clip tab. One press does every file: the agent picks the best moments
   and then drafts the posts for them, which takes about a minute. The button and the footer
   count the files as they finish ("2 of 3 files"). When it's done a toast says how many files
   have moments and the first one opens by itself; click any file to see its moments in the
   lower half of the left pane. Then press **+ Add to story**, or drag a moment onto the Story.
   The clip remembers which moment it came from, so the moment still says **In story** (and
   **Copy post** still finds its post) after you trim it.
3. **Arrange.** Drag clips in the Story to reorder them, and drag their edges to trim. In the
   Clip tab you can select words, then **Keep only this** (the preview jumps to what's left)
   or **Cut this out**. A word Whisper got wrong? **Double-click it** in the Words list, type
   what was said and press Enter; tick *everywhere* to fix it in every file. Captions use the
   fix straight away. **Frame** picks how the clip fills the picture: **Fill the frame** (crop
   the edges; the default for camera footage) or **Show everything** (the whole picture over a
   blurred copy of itself; the default for screen recordings, so their top bar isn't cut off).
   ⌘Z undoes. If the agent edits the story, a toast says what changed ("The agent removed “Two
   hours every night”"), with Undo. A removed clip leaves a red ghost where it was. If you and
   the agent edit at the same moment, both edits are kept (a toast says so).
4. **Captions & format.** Pick where the video is going (Vertical 9:16 and the others) and
   turn captions on or off. The preview shows the crop (or the fit) and the captions exactly
   as the export will: captions come in chunks of up to 3 words that end at a full stop,
   question mark or comma, so a chunk never runs across two sentences (one that would show for
   under 0.4 s joins its neighbour; the export also holds each to the moment the voice is heard).
5. **Export & post.** Press **Export**. When it's exported, a toast and the **Your videos**
   list (top of the left pane) offer **Check** and **Copy post**. It says **Ready** only once a
   check of that exact file passes. It opens the post drafts for that clip.
   Rewrite them in your own voice; your rewrites are saved and the agent can read them. Use
   **Finder** to AirDrop the file to your phone.

**Tell the agent** (right pane) is a two-way thread. A note is sent with whatever is selected.
Notes are ticked "✓ the agent saw this" once the agent has read them, and its replies appear
there and as toasts. **Activity** lists who did what, and **Restore** puts back any earlier
version of the story, even after a reload.

## The agent's side: the `studio` CLI

[AGENTS.md](AGENTS.md) is the brief for the agent: how to see what the operator is looking at,
answer their notes and edit the story without stepping on them. Most coding agents read it by
themselves when they start in this folder.

Clips are numbered from 1, the way the UI says them: "Clip 3 of 4" is `3` everywhere in the
CLI (arguments and output). `-1` is the last clip. Clip ids (8 hex characters; 4 or more are
enough) don't change when the operator reorders, so prefer them while both of you are editing.

```bash
studio focus                      # what the operator has selected now (JSON: project, rev,
                                  # selection {index (0-based), number (1-based), id, in, out,
                                  # label, moment}, playhead, words)
studio focus --brief              # the same as one line: clip 3 72efa07f 0:27.0-0:48.0 …
studio notes [-p P] [--unread] [--json]   # the operator's notes; each one shows what was
                                  # selected when it was written:
                                  #   → clip 2 72efa07f 0:27.0-0:48.0 'Never leaves…' · playhead 0:23.0
                                  # (when the agent runs it, reading a note marks it seen in the UI)
studio say "On it" --re NOTE_ID   # reply in the operator's Tell the agent thread (+ toast)
studio notes --ack NOTE_ID        # mark a note seen without replying

studio ls [-p P]                  # library + finished steps
studio media show ID              # one file: moments (n, in–out, title, hook), drafts
studio media move ID PROJECT      # file it under another project
studio words ID --grep privacy    # word timings, so you can "start on the word privacy"
studio words ID --from 10 --to 20
studio words ID --fix Hefe Jefe   # fix a misheard word in that file's transcript (and captions)
studio words --fix Hefe Jefe --all   # ...in every file. The UI updates at once.
studio posts ID [--json]          # post drafts with the operator's rewrites applied

studio project new NAME | ls
studio timeline show P [--json]   # the story, with its rev; --json clips carry index (0-based)
                                  #   and number (1-based), moment, and the effective fit
studio timeline add P MEDIA[@N] | add P MEDIA IN OUT [--at N]   # @N = moment N (remembered
                                  #   on the clip, snapped to speech; --no-snap keeps its times);
                                  #   --at N = insert as clip N; --snap = onto speech edges
studio timeline trim P CLIP IN OUT [--snap]   # CLIP = number (1-based) or clip id/prefix
studio timeline snap P CLIP|all          # spoken in/out points onto speech edges (below)
studio timeline split P CLIP AT          # AT = seconds into the clip
studio timeline fit P CLIP fill|fit|auto # crop to fill / show everything / footage default
studio timeline move P CLIP TO | rm P CLIP | label P CLIP TEXT | clear P | set P FILE|-
                                  # TO = the clip's new number
studio timeline cover P CLIP --with MEDIA IN OUT [--at T] [--fit fill|fit]
                                  # b-roll over the clip's sound (below): MEDIA's IN-OUT from
                                  #   T = seconds into the clip as it plays (after its pace)
studio timeline cover P CLIP --with MEDIA IN --until U | --until-end | --fill
                                  # ...to U seconds into the clip, to its end, or all of it
studio timeline uncover P CLIP [N|all]   # take cover piece N (as `show` lists them) or all off
studio timeline voice P CLIP|all natural|off   # the voice treatment (all = the story's default)
studio timeline pace P CLIP|all X        # play 1.0-1.2x as fast, pitch kept (1 = as said)
studio timeline tighten P CLIP|all [--max 0.35] [--to 0.12]   # cut long pauses down (below)
   --base REV    refuse (exit 3) if the story changed since `show` printed rev REV
   --aspect 9:16|16:9|1:1|4:5   --captions on|off
studio render P                   # export (loudness -14 LUFS); the UI shows it under "Your videos"
studio check P [EXPORT] [--json]  # measure the export against QUALITY.md; review sheet; exit 1 on a fail
studio ready P [EXPORT] [--json]  # exit 0 only if a check of these exact bytes passes
studio exception grant P RULE [CLIP] --why TEXT | revoke P ID [--why TEXT] | ls P [--all]
   (exception grant … --onscreen TEXT / --card: declare the hook as text in the picture, or a
    designed card; an exception binds to the clip, or the whole story, as it is when granted)
studio choose P CHOSEN|none --from A,B,C [--action pick|keep|rewrite|reject] [--note T]
studio ab P [EXPORT…]             # blind A/B of versions; then: choose P A --round ROUND
studio moments|draft|cut ID… | --all     # AI steps (moments and draft call the agent). A bare
                                  # `studio moments` refuses instead of running every file.
studio proxy|speech|transcribe|keyframes ID… | --all   # speech: where someone talks (below)
studio bundle OUT_DIR -p P [-p P2 …] | --all [--zip]   # share with a teammate (below)
studio import BUNDLE_DIR [--no-previews]               # bring a teammate's bundle in
studio log [-n N]
studio agent                      # which agent moments/draft call, the command line, the models
```

`studio ingest` takes only files that have stopped changing for 3 seconds
(`STUDIO_SETTLE_SECS`). A file you name that was written a moment ago (a render that just
finished, a `cp`) is waited for (`wait NAME`), up to 60 s (`STUDIO_CLI_SETTLE_WAIT_SECS`; still
growing after that, it's skipped and ingest exits 1); a modification time in the future (a
device's clock) counts as finished. One in inbox/ that's still being AirDropped or
copied prints `skip NAME (still copying…)` and stays there for the next run. A file that can't
be read, has no picture or sound, is 0 seconds long, or whose end is missing prints
`skip NAME (reason)` and also stays where it is. Nothing moves into the library before it has
been fully hashed and probed, and one bad file never stops the rest. The last line counts what
happened (`1 new, 0 already in the library, 1 skipped`); a file you named that was skipped
makes it exit 1.

Media is its bytes (sha256): the same bytes ingested again are the same media (`have NAME →
ID`), whatever the file is called. It stays in its project (any story can use it), unless it was
unsorted in the inbox; `studio media move ID PROJECT` files it elsewhere on purpose.

The timeline verbs fail loudly. `trim` with OUT ≤ IN exits 1 and leaves the clip alone.
Clamping to the media's length prints a `warning:` to stderr. A `set` or `add` that would
drop a clip (audio-only media, unknown id, zero length) exits 2 and gives the reason.

## Quality: speech edges and `studio check`

[QUALITY.md](QUALITY.md) is the bar every reel clears: hard rules that are measured and
judgment rules the agent answers. The moments prompt carries it, so the agent picks fewer,
complete beats (and may pick none). Three things hold the hard rules:

- **Speech regions.** Whisper's word times run ~150 ms early and snap to whole seconds near the
  end of a sentence, so cutting on them clips the last word. The `speech` step runs Silero VAD
  (`whisper-vad-speech-segments`, whisper.cpp's port) on each file at ingest and writes
  `media/<id>/speech.json`: where someone is really talking. Older media get it on first use,
  or all at once with `studio speech --all` (no re-transcribing). The model,
  `ggml-silero-v6.2.0.bin` (885 KB), is pinned by the flake (`fetchurl`, sha256) and reaches
  studio as `STUDIO_VAD_MODEL`; without nix it is downloaded once into
  `~/.local/share/studio/models/` and checked against the same sha256.
- **Snapping.** A spoken clip starts 0.07 s before its first word and ends 0.26 s after its
  last, never inside a word: a cut point inside speech moves to that phrase's edge, dead air
  at either end goes. Moments are snapped when they're found, `timeline add MEDIA@N` keeps
  them snapped, and `--snap` (add, trim, set) or `timeline snap P CLIP|all` does it for any
  clip. A clip with no speech in it (a silent shot) is left alone, and the operator's own
  trims in the UI are never snapped. Every part of an export gets a 20 ms audio fade in and
  out, so cuts don't click, and `render` normalizes loudness to −14 LUFS, true peak −1 dBTP
  (two-pass `loudnorm`, video copied).
- **Frame-exact renders.** Every clip is a whole number of 30 fps frames (its length rounded
  once), and so is every cover stretch inside it. Each part is rendered as exactly that many
  frames from its first frame at 0, with PCM sound of exactly frames × 1600 samples (48 kHz), no
  metadata and no timecode track; the parts then join with the concat demuxer as they are
  (`concat: copy` in `render.done`) and the export's AAC is encoded once, by `loudnorm`. So no
  frame is held at a cut, every cut lands on its frame, and a cut's first frame is a clean key
  frame. If a part's stream ever differs (e.g. VideoToolbox fell back to software), the join is
  re-encoded with a key frame forced on every part's first frame (`concat: reencode`).
- **Transcription only hears speech.** whisper-cli runs with `--vad` (the same Silero
  settings), `-sns` (no non-speech tokens) and `-mc 0` (no carried-over text; the vocabulary
  prompt is still carried). A file with no speech gets an empty transcript instead of an
  invented "Thank you.", and stock phrases ("Thanks for watching", "Subscribe", subtitle
  credits) are dropped wherever they appear. whisper-cli maps segment times back from its
  shortened audio but not token times; studio rebuilds whisper.cpp's map from the same regions
  so every word lands where it was said, and never runs past its speech region.

`studio check P` measures the latest export (or `studio check P path/to.mp4`): every spoken
in/out point against the speech (lead-in 0.05–0.15 s, tail ≥ 0.25 s, never inside speech,
spoken pieces ≥ 1.5 s), the first word (≤ 0.5 s), pauses inside speech (≤ 0.35 s, VAD on the
export itself), length (15–35 s; up to 60 s is a warning), loudness (`ebur128`), silence (`dead
air`: more than 0.3 s under −60 dBFS), each spoken clip's speech-gated loudness against the
median clip's (`level step`: ±2 LU), shots under a second between two picture changes (`blip`), black and
frozen picture (`blackdetect`, `freezedetect`), every cover piece showing to the end it was
laid to (`cover short`: its footage runs out first), the cuts as rendered (in the same decode: the
scene-change score with the caption band masked, mean luma and every frame's time; and the
export's sound at each sound cut): frames that belong to neither shot (`flash`), a picture change
near a cut that isn't in the timeline (`stray`), one-frame black or white pops (`pop`), a jump
cut between two moments of the same footage with nothing hiding it (`jump`), speech-level sound
right at a sound cut (`fragment`), a click (`click`), a frame off the 30 fps grid (`held`), and,
as a warning only, a picture that changes again the frame after a cut (`soft`); captions against a fresh
transcription of the export (≥ 80% of words agree), each caption against the export's speech
(`caption stray`: in speech or ≤ 0.20 s before it, never up through 0.3 s of silence, never on
screen under 0.4 s) and against the platform's UI (`safe zone`: drawn by libass on black and
measured with `bbox`), how soon the product shows (`product late`, a warning), and the last word (not filler). A clip nobody speaks in (b-roll,
a cartoon, a card) is picture: never a pause. A rule broken on purpose needs an exception,
granted in words (`studio exception`, QUALITY.md), including the two things nothing measures:
on-screen text as the hook (`--onscreen`) and a designed card (`--card`). Check marks them `EXC`
with their reason; an exception granted on a clip (or story) that has changed since has lapsed,
and check says so. The export is hashed before and after it's measured: if the bytes changed
meanwhile, no record is written. It writes a review
sheet next to the export, `exports/<name>.check/`: `cut-NN-….png` for the start, every cut and
the end (12 frames, the waveform with speech in green, the words heard, a red line at the cut,
1.5 s either side), `RULE-fNNNNN.png` for every cut finding (nine numbered frames around it) and
`contact.png` (24 frames of the whole export), plus `check.json` and
`verdict.txt` (every rule, every exception with who and why). It logs `check.done` with the
export's sha256 and every failure by rule and clip, prints QUALITY.md's judgment rules as a checklist, and exits 1 if any hard
rule fails. The agent's loop (AGENTS.md): render, check, look at the sheet, fix and re-render
up to three times, answer the judgment rules, and only then tell the operator.

**Ready means checked.** An export is ready only when a check of its exact bytes passes, or every
failure it found is covered by an exception still granted. `studio ready P [EXPORT]` exits 0 only
then (it re-hashes the file, so bytes changed on disk are unchecked); a re-render starts
unchecked; revoking an exception makes it not ready again. The UI's **Your videos** says
"Ready", "Not ready: …" or "Not checked" (with **Check**), and lists the exceptions with
**Revoke**.

**Choices are logged.** Every pick is a `choice` event: the options in the order shown, the one
chosen (or none), whether it was blind, and where each option came from (an export's bytes and
story; a moment's or draft's agent, model and prompt version, which `step.moments` and
`step.draft` now record). The UI logs a moment added to the story, and a post kept (Copy) or
rewritten. `studio choose P CHOSEN --from A,B,C` logs one from the terminal; `studio ab P`
sets up a blind A/B of versions (shuffled copies labelled A, B in `projects/P/blind/ROUND/`, the
mapping in a `blind.round` event), and `studio choose P A --round ROUND` maps the pick back.
The copies are real copies with identical dates (a clone keeps the export's dates, which would
say which is newer); their sizes are still the exports' sizes, so don't look them up meanwhile.

Fades are made by construction (not measured). Before the master loudness pass the render
brings every spoken clip's speech to one level (`level_parts`: speech-gated `ebur128` per part,
one gain per part, natural-sound clips at the median gain) and lays room tone under a clip with
no sound of its own (from the nearest clip with sound; `STUDIO_LEVEL=0` turns both off); the
parts keep their frames and their 1600 samples a frame.

## Cover, voice, pace and tightening

- **Cover (b-roll over a-roll).** A clip always keeps its own **sound**; its **picture** can be
  covered by pieces of other footage: `timeline cover P CLIP --with MEDIA IN OUT --at T` shows
  MEDIA's IN–OUT from T seconds into the clip. **Cover speaks reel time**: T (and `--until U`)
  are seconds into the clip *as it plays*, after its pace, the clock of the export and of
  `check`; IN and OUT are seconds of MEDIA, which plays at normal speed, so OUT − IN is also how
  long it covers. `--until U` covers to U, `--until-end` to the clip's end (wherever a later
  trim puts it), `--fill` the whole clip. The command says the converted numbers back (where it
  lands in the reel, the clip's source seconds under it, the footage it uses). It never clamps:
  a span past the clip's end, or footage too short to fill the span, is refused with the length
  it needs (on MEDIA's clock) and what would work (an earlier IN, a shorter span, a second piece
  after it). On the timeline the clip carries `cover: [{media, in, out, at, fit?, to_end?}]`, the
  shape the log has always had: the piece is laid over the clip's own source seconds from `at`
  for `out − in` of them (to the clip's out point when `to_end`), its footage playing from `in`
  at normal speed. Where no piece covers, the clip's own picture shows; a new piece replaces
  what it lands on. `timeline show` prints every piece in reel time: where it is in the clip and
  in the reel, and which seconds of its footage it shows.
- **Cover stays over its words when the clip changes.** A trim, snap or pace leaves every piece
  over the words it was laid over (it moves on screen with them); a split gives each side its
  part; a tighten, or the UI's "Cut this out", keeps each piece over its words and plays its
  footage on unbroken across the cut (a piece laid inside the removed stretch is dropped, with
  a warning). An edit that cuts into a piece or leaves the clip without a piece's words says
  so (in the CLI, and as a toast in the UI); a piece outside the clip is kept, showing nothing,
  and comes back when a trim brings its words back. A piece whose footage runs out before the end it was laid to (a `--until-end` piece on a
  clip trimmed longer, a slower pace) warns at the edit and fails **`cover short`** in `check`.
- **Rendering cover.** What a clip shows is computed once, in whole export frames
  (`cover_plan`): render, the cut list, `check`, `timeline show` and the edit warnings all read
  it, so what check measures is what renders. A piece never asks for a frame of footage its file
  doesn't have (it stops there and the clip's own picture comes back), never shows footage before
  its `in`, and is never drawn under 3 frames (one rule for `timeline cover` and every save, so
  a piece laid is never dropped when it is saved). A piece laid from the clip's start covers
  its first frame. `test/cover_parity.py` (in `run_all.sh`) holds the UI's copy of the split
  and cut rule to studio.py's on 4,000 seeded cases. Render builds the picture stretch by stretch (each
  fitted to the frame like any clip, the sound's captions burned across all of them), joins it
  and lays the clip's own sound under it in one piece, so cover never changes what's heard.
  `render.done` carries a cut list (`cuts`: `sound` or `picture`); `check` judges only sound cuts
  as speech cuts and makes a review sheet for each picture cut too. The Story timeline marks
  covered stretches with a thin orange line along the top of the clip.
- **Voice.** `natural` (the `VOICE_CHAINS` constant in studio.py: rumble cut, gentle denoise,
  less mud, a little presence, soft de-ess, light compression) runs on a clip's sound before
  loudness normalization. Spoken clips get it unless a clip, or the story (`voice … all`), says
  `off`; clips with no speech get nothing.
- **Pace.** `timeline pace P CLIP 1.1` plays the clip 10% faster: `atempo` on the sound (pitch
  kept), `setpts` on its own picture, clamped to 1.0–1.2. Everything measured on the export runs
  on the sped-up clock: captions, snapping's lead and tail, `check`, cut times. Cover pieces play
  at normal speed and stay over the words they were laid over (above).
- **Tighten.** `timeline tighten P CLIP|all` splits a spoken clip at every pause inside its
  speech longer than `--max` (0.35 s) and cuts it down to `--to` (0.12 s, export seconds: 60%
  after the last word, 40% before the next), in silence only; every part gets the usual 20 ms
  fades. Cover is laid back on the shorter clock: each piece starts and ends under the same
  words and plays on unbroken across the cuts. `check` calls these **pause cuts** (the same take going on
  a little later, skipping only silence) and judges the two edges as one pause rather than as a
  beat's end and start; `snap` leaves them as tightened. Do it in this order: `pace`, `snap`,
  `tighten`.

## Using it with your agent

Moments and post drafts come from a coding agent's non-interactive mode. Studio hands it one
prompt, runs it in the footage's folder (so it can open the keyframes and the transcript),
and reads a JSON block out of its reply. Any agent that can do that works.

| Variable | What it does |
|---|---|
| `STUDIO_AGENT` | `claude`, `codex` or `goose` (built in). Unset: the first of those on PATH. |
| `STUDIO_AGENT_CMD` | Any other command: it gets the prompt on stdin and prints the reply. Wins over `STUDIO_AGENT`. |
| `STUDIO_AGENT_MODEL` | The model for every call (for a custom command, passed in its environment). Unset: the agent's own default. `STUDIO_CLAUDE_MODEL`, its old name, still works with Claude Code. |
| `STUDIO_MOMENTS_MODEL` | The model for picking moments only. Unset: the agent's default for that (Claude Code: `claude-opus-5-5`); `""`: no override. |
| `STUDIO_ACTOR` | `agent` or `cli`: who is running the CLI, when it can't tell by itself (below). |

`./studio agent` prints which agent will run, the exact command line and the models. The
built-in commands (the prompt always goes on stdin):

- **Claude Code:** `claude -p --output-format json [--model M] --allowedTools Read`
- **Codex:** `codex exec --sandbox read-only --skip-git-repo-check --ephemeral --color never
  [-m M] -o REPLY_FILE -` (read-only sandbox; the reply is its last message)
- **Goose:** `goose run --no-session --quiet --output-format json [--model M] -i -`
- **Anything else:** `STUDIO_AGENT_CMD='llm -m my-model'`, a script of your own, a local
  model: whatever reads stdin and prints the answer.

**Driving the studio.** Start your agent in this folder and it has the brief,
[AGENTS.md](AGENTS.md): `studio focus`, `studio notes`, `studio say`, the timeline verbs.

```bash
cd ~/code/studio
claude            # Claude Code (reads CLAUDE.md, which points at AGENTS.md)
codex             # Codex (reads AGENTS.md)
goose session     # Goose (reads AGENTS.md)
```

Any other agent: tell it to read `AGENTS.md` first. The CLI records the agent's edits as the
agent's, so the operator sees "The agent removed …" and gets the "saw this" tick on their
notes. It knows by the marker each agent sets in the shell it runs commands in: `CLAUDECODE` (Claude Code), `CODEX_THREAD_ID` or
`CODEX_SANDBOX` (Codex), `GOOSE_TERMINAL` (Goose). For any other agent, have it run with
`STUDIO_ACTOR=agent`. The agent's events say `"actor": "agent"`; libraries from before this
say `"claude"`, and both read as the agent.

## Hardware settings (M4 Max: 14 cores, 32-core GPU, 36 GB)

The defaults are tuned on this machine, so you shouldn't need to change them.

| What | Setting | Why |
|---|---|---|
| Decode/scale/encode | VideoToolbox (`-hwaccel videotoolbox`, `scale_vt`, `h264_videotoolbox`) | The media engines do the work, not the CPU |
| Export crop | Done on the GPU **before** scaling | iPhone 4K HLG → 9:16 went from 3.2x to 9.3x realtime |
| Export lane | Its own lane (1). Proxies use 1 chunk while an export runs | An export never waits behind previews |
| Media lane | 2 slots, FIFO. A 4K/10-bit proxy takes both | A second proxy only halves both; the first drop is playable in ~4.5 s instead of ~8.5 s |
| Keyframes | Jump the media queue (they read the 720p proxy) | Thumbnails appear right after the preview |
| Whisper | 1 at a time, Metal, `-t 8`, greedy. WAV extraction happens *before* taking the slot | The GPU lane holds only whisper-cli |
| Export segments | 4 in parallel (`STUDIO_RENDER_JOBS`) | The VT encoder saturates at about 4 sessions |
| AI | 3 agent calls at once (`STUDIO_AI_LANE`) | "Find moments in all" runs files side by side |

Knobs: `STUDIO_HW=0` (libx264 fallback), `STUDIO_HWDEC=0|force`, `STUDIO_PROXY_Q` (55),
`STUDIO_EXPORT_Q` (65), `STUDIO_PROXY_CHUNKS` (4), `STUDIO_MEDIA_LANE` (2),
`STUDIO_WHISPER_THREADS` (8), `STUDIO_WHISPER_BEAM` (1),
`STUDIO_WHISPER_NST` (0.6, whisper's `--no-speech-thold`), `STUDIO_SETTLE_SECS` (3),
`STUDIO_UPLOAD_STALL_SECS` (120: an upload that sends nothing for this long is dropped),
`STUDIO_WHISPER_MODEL` (default `~/.local/share/studio/models/ggml-large-v3-turbo.bin`),
`STUDIO_VAD_MODEL` (the Silero model; the flake sets it, see [Quality](#quality-speech-edges-and-studio-check)).
The agent's knobs are under [Using it with your agent](#using-it-with-your-agent).

## Words: vocabulary and fixes

`brands/<name>.md` has two sections for Whisper. **Vocabulary** lists the words it should
expect (Jefe, aerate, overseed…); they're passed to whisper-cli as its prompt, and capitalised
ones are names, so "jefe" is written "Jefe". **Fix words** lists what it mishears and what was
meant, one `- Hefe → Jefe` per line; every new transcript gets those fixes. Add a line whenever
the same mistake shows up twice. For a transcript that already exists, use `studio words …
--fix` or double-click the word in the UI. Whisper's usual inventions are dropped: a word with
no length and nothing around it, and a lone "Thank you." in a quiet tail.

## Sharing footage with a teammate

A day of footage and all the work on it, as one folder for another Mac running studio:

```bash
studio bundle ~/Desktop/"BMW job" -p bmw-200-job --zip   # or --all; -p again for more projects
# → ~/Desktop/BMW job/ and ~/Desktop/BMW job.zip (send the zip through Google Drive)
studio import ~/Downloads/"BMW job"                      # on the teammate's Mac, after unzipping
```

In the UI: **Share…** and **Import…** next to the project list (the app also has File ▸ Share
Project… ⇧⌘S and File ▸ Import… ⇧⌘O, with Finder's folder picker). In a browser you type the
folder's path; the dialog also shows the matching command.

A bundle holds:
- `footage/<Project Name>/` — the original files, untouched. Each keeps its camera name; one
  that's in a shared story gets its place and label in front:
  `01 - BMW walk - DJI_20261108120758_0064_D.MP4`. Footage a story borrows from another
  project comes along, in that project's folder.
- `library/` — a studio library without the heavy, remakeable files: the event log cut down
  to these projects and their media (stories with their whole history, notes, exports, words,
  moments, drafts; no jobs), `media/<id>/` transcripts, keyframes, moments and drafts, and
  `projects/<slug>/timeline.json` plus `exports/*.mp4`. Sources (they're in footage/),
  proxies, clips/, uploads/, `.server.json` and `ui-focus.json` stay home.
- `manifest.json` (format version, created, studio version; per file its id, sha256, footage
  path, project, duration, size, sound; per project its name and clip count), `README.md` for
  the teammate (how to open it, a table of footage and moments, how to browse without the app)
  and `moments.csv` (every moment: project, file, footage path, n, start, end, duration, title,
  hook, pillar, angle, kind, why).

Footage and exports are APFS clones, so a bundle on the same disk takes no extra space (other
volumes get a copy). Every source is checked against its fingerprint on the way out. `--zip`
also writes `OUT_DIR.zip` without compression (video doesn't shrink; storing is as fast as a
copy) and warns past 15 GB. Nothing in the library changes.

`studio import` first checks every new file's sha256 against the manifest; a damaged or missing
file stops the import before anything changes, with the list. Then it clones each source into
`media/<id>/`, copies the metadata and exports, and appends the bundle's events (keeping their
times). Footage already in the library is left as it is, events already in the log are skipped,
and a project that is already here (same name and creation time) merges into itself, so a
second import changes nothing. A different project with the same slug comes in as
`slug-2` and import says so. The bundle is only read. Previews (and frames or words, if the
bundle had none) are made afterwards: queued on the running studio for that library if there is
one (`.server.json` + `/api/health`), otherwise right there with progress. A folder that
holds exactly one bundle (unzipping often adds a level) works too; a `.zip` has to be unzipped
first. Moment clips (`clips/`) aren't remade; run
`studio cut ID` if you want them.

API: `POST /api/bundle {projects, out, zip}` and `POST /api/import {path}` (JSON only) start a
background job (`share` / `import`); the UI toasts when it's done.

## Pairing a phone (v2, in progress)

The v2 Rust core (`crates/`, binary `studio-rs` for now) gives the library one key: a nostr
keypair plus an age x25519 identity, shared by every device of the library in v1. It lives in
`$STUDIO_HOME/keys/` (`nostr.key`, `age.key`; folder 0700, files 0600) and is made the first
time `studio-rs pair` runs. The library's id is the first 16 hex characters of its public key
in hex (not of the `npub…` text). Where the library syncs comes from `studio.toml`:

```toml
[sync]
relays = ["ws://nous.local:7777"]
blossom = ["http://nous.local:3000"]
device_name = "nous"        # default: $STUDIO_DEVICE, else the hostname
```

```bash
studio-rs pair --qr                       # the pairing QR, for the phone to scan
studio-rs pair --payload | pbcopy        # the same payload as text (careful: see below)
pbpaste | studio-rs pair --from -         # on another home: join from stdin
studio-rs pair --from ~/payload.txt       # …or from a file (delete it afterwards)
```

`--from` never takes the payload itself as an argument (it would land in shell history and
`ps`); it reads stdin (`-`) or a file. A home that already holds another library's key refuses
to be paired over. If `keys/` is damaged (one file without the other), every command stops
and says how to recover: delete `keys/` and run again only if the library was never used,
otherwise re-pair from a device of the library or restore your backup. `studio ui` never
serves `keys/`, `studio.toml` or dot-files, and answers only requests addressed to
`127.0.0.1`/`localhost`.

**Operator note.** The QR and the `--payload` text *are* the library's two private keys (plus
the server URLs). Anyone who sees either can read, write and decrypt the whole library, and v1
has no way to revoke one device: changing the key means re-pairing every device and
re-encrypting every blob. So: show the QR only to the device you are pairing; clear the
terminal's scrollback (and tmux history, screen recordings) afterwards; never paste the
payload into a chat, a note or a log; and back up `keys/` offline (an encrypted USB stick or
your password manager), because a lost key is a lost library.

## After a crash

Restarting `studio ui` cleans up after itself: half-written uploads are deleted, a file whose
ingest died half way is put back in inbox/ (never deleted), and any file still missing its
preview, words or frames gets them made. The event log survives a writer that died mid-line:
the broken line is skipped with a warning and the next event is kept.

## Tests

`test/run_all.sh [--no-bench] [--no-ui] [--strict] [--only cli|server|ui|rust|bench]` runs:
- fixtures, cached in `~/.cache/studio-fixtures`. They include `iphone-hdr.mov`, a 4K HEVC
  10-bit HLG file like the iPhone default.
- CLI and API tests. The quality tests run on two more fixtures whose speech is known by
  construction: `bursts.mov` (three sentences with set silences between them and a black
  stretch, spans in `bursts.json`) and `broll-roomtone.mov` (picture over room tone). They
  check speech regions, snapping, the empty transcript for b-roll, render loudness, and that
  `studio check` catches a cut inside speech, a long pause, wrong loudness, black picture and
  captions nobody says.
- The agent adapters: moments and draft through fakes that speak each CLI's surface
  (`test/fakebin/claude`, `test/fakeagents/codex`, `test/fakeagents/goose`) and through a
  custom `STUDIO_AGENT_CMD` (`test/fakebin/fake-agent`).
- `test/ui_smoke.py`: the whole operator path in headless Chrome, with the agent replaced by
  `test/fakebin/fake-agent` (canned answers).
- `test/webkit_smoke.py`: the core path in Safari (the Mac app's engine) through safaridriver.
  Skipped until Safari's remote automation is turned on once: `sudo safaridriver --enable`.
- `test/app_smoke.py`: the built app (`result/`), launched like the Dock does, driven through
  its `STUDIO_APP_DEVHOOK` (works with the screen locked): WebKit core path, native drops,
  menus, attach, quit, crash. Skipped when there's no `nix build .#studio-app` result.
- The Rust workspace: `cargo test --workspace` and clippy (`--only rust` uses the lean
  `devShells.rust`). `test/ios_build.sh [--whisper]`, run from a login shell, cross-builds it
  for the Mac, iPhone and simulator.
- A benchmark with budgets.

Everything runs in throwaway homes on ports 7400-7499 (`STUDIO_TEST_PORTS=7700-7799` picks
another range). Nothing touches `~/Studio` or calls a real agent. `test/ui_smoke.md` is the
manual walk-through.

## Batch mode (no UI)

`./studio run [--brand jefe] [files…]` handles `~/Studio/inbox/` (or the files you give it):
ingest, preview, words, frames, moments, cut clips and drafts. It then opens `review.html`.
The voice comes from `brands/<name>.md`. `jefe.md` is a stand-in until the
intellectual-signature interview produces a real brand document.

## Known limitations

- Fill crops the centre. There's no face-aware reframing yet, so a landscape walk going 9:16
  keeps the middle third unless you choose **Show everything** for it (camera footage
  defaults to Fill; only silent portrait footage that's far from the target shape, i.e.
  screen recordings, defaults to Show everything).
- The preview's blurred background for **Show everything** is painted from the playing video,
  so it's close to the export's but not pixel-identical.
- Word fixes made with `--fix` or a double-click are written into that transcript. Running
  `studio transcribe ID --force` starts from Whisper again and keeps only the brand file's
  Fix words, so put repeat offenders there.
- Dropping a "Thank you." needs it to be the last thing said and much quieter than the speech
  before it. A real, audible "Thank you." at the end stays.
- Only the first `studio ui` on a library does the crash cleanup; a second one on another port
  leaves uploads and half-ingested files alone.
- Exports are SDR BT.709. HDR footage is tone-mapped by VideoToolbox, so expect slightly
  flatter highlights than the phone shows.
- An export started while a 4K HDR file is still making its preview runs about 2x slower
  (11–16 s instead of 6.6 s for 48 s of video) because both share the media engine. It still
  starts at once.
- Whisper loads the model for each file (~1 s). There's no batching or resident whisper-server.
- "Find moments" needs the network and a signed-in agent (unless yours runs a local model).
  Everything else works offline.
- The undo stack is per window and in memory. After a reload, use Activity → **Restore**.
- Post rewrites are tied to the moment they belong to. **Find again** replaces the moments,
  so it asks first and then drops the old rewrites.
- Not yet: approving and scheduling posts, the identity layer.
- The preview player plays a clip's own picture at normal speed: cover, voice and pace are
  heard and seen in the export (and the timeline marks covered stretches), not in the preview.
  The timeline's clip lengths are source lengths, so a paced clip looks a little longer there
  than it plays.
- The Mac app is ad-hoc signed for this Mac, not notarized: copied to another Mac it needs
  right-click ▸ Open the first time.
- A second launch with a different library (`--home`) brings up the running window instead;
  switch libraries with Settings….
- A server started before the lockfile existed (an old `./studio ui`) isn't found by the app,
  which then starts its own on another port (crash recovery stays with the first one).
