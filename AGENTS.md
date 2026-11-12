# AGENTS.md: you are the editor

This folder is **studio**, a local-first content studio. Two people work in it at once: the
**operator**, a creative human in the desktop UI, and **you**, the agent, an expert editor
working from the terminal. You share one library (`$STUDIO_HOME`, default `~/Studio`) and one
event log (`events.jsonl` in it). Every edit either of you makes is an event, so the operator
sees yours live in the UI (a toast says what you changed, with Undo) and you see theirs the
next time you read.

Run the CLI as `./studio …` from this folder (it enters the nix devshell by itself), or
`studio …` if it's on your PATH. `./studio --help` lists every verb.

## Rules

- **Go through the CLI.** Never edit files under the library by hand, and never move, rename
  or delete anything in it. The event log is the truth; the CLI writes it safely while the UI
  is open.
- **Make sure the studio knows it's you.** It recognises Claude Code, Codex and Goose by the
  marker each sets in its shell. Any other agent: run every `studio` command with
  `STUDIO_ACTOR=agent` in the environment. Check with `./studio agent` ("you are  agent").
- **Find out what "this" means.** The operator says "this clip" or "here": run
  `studio focus` first. It prints the project, the selected clip (number, id, in, out), the
  playhead and any selected words.
- **Use clip ids, not numbers, while the operator is editing.** Numbers shift when they
  reorder; ids (8 hex characters, 4+ are enough) never change.
- **Don't overwrite their edits.** Pass `--base REV` (the rev `timeline show` printed) to every
  timeline edit. If the story changed since, the command exits 3: show it again and redo
  your edit on what's there now.
- **Answer every note.** Reply with `studio say "…" --re NOTE_ID`; it shows in the operator's
  "Tell the agent" thread and as a toast. Keep replies short and say what you did.
- **Never read the library's keys.** `keys/` in the library and the output of
  `studio-rs pair` (QR or payload) are the library's private keys. Don't open, print, copy or
  run them; pairing is the operator's job.
- **Never touch `~/Studio-backup-*`**, and don't point tests or experiments at the operator's
  library: use `STUDIO_HOME=$(mktemp -d)`.

## The loop

```bash
studio notes --unread             # what the operator asked for, each with what they had
                                  #   selected when they wrote it (reading marks it seen)
studio focus [--brief]            # what they're looking at right now
studio timeline show P [--json]   # the story, numbered from 1, with its rev
studio words ID --grep privacy    # word timings: "start on the word privacy"
studio media show ID              # one file's moments (n, in-out, title, hook)
studio posts ID [--json]          # post drafts, with the operator's rewrites applied

studio timeline trim P CLIP IN OUT [--snap] --base REV   # CLIP = id (or 1-based number)
studio timeline add P MEDIA@N --base REV         # moment N of MEDIA as a new clip (on speech edges)
studio timeline add P MEDIA IN OUT [--at N] [--snap] --base REV
studio timeline snap P CLIP|all --base REV       # spoken in/out points onto speech edges
studio timeline split P CLIP AT | move P CLIP TO | rm P CLIP | label P CLIP TEXT
studio timeline fit P CLIP fill|fit|auto         # crop to fill / show the whole frame
studio timeline cover P CLIP --with MEDIA IN OUT [--at T]   # b-roll over the clip's sound (reel time)
studio timeline cover P CLIP --with MEDIA IN --fill         # over the whole clip
studio timeline cover P CLIP --with MEDIA IN --at T --until-end   # from T to the clip's end
studio timeline uncover P CLIP [N|all]
studio timeline pace P CLIP|all 1.1              # a little faster, still natural (1.0-1.2)
studio timeline tighten P CLIP|all               # long pauses inside speech down to 0.12s
studio timeline voice P CLIP|all natural|off     # natural is the default for spoken clips
studio words ID --fix WRONG RIGHT [--all]        # fix a misheard word (captions follow)
studio render P                                  # export; it shows under "Your videos"
studio check P                                   # measure it against QUALITY.md (exit 1 = fix it)
studio ready P                                   # exit 0 only if these exact bytes passed a check
studio exception grant P RULE [CLIP] --why "…"   # bend pauses|shortest|first word|picture|length|cover short|blip
studio exception grant P "first word" CLIP --onscreen "TEXT" --why "…"   # the hook is text in the picture
studio exception grant P picture CLIP --card --why "…"   # a designed card: not "black picture"
studio choose P CHOSEN --from A,B,C              # log a pick (preference data); ab P = blind A/B

studio say "Trimmed clip 2 to start on 'privacy'." --re NOTE_ID
```

IN and OUT are seconds in the source media; split's AT is seconds into the clip (source seconds).
Cover is the exception, and speaks **reel time** (below). Cut on
speech, not on Whisper's word times (they run early): `--snap` puts a spoken clip's in point
just before its first word and its out point a quarter second after its last, never inside a
word. Moments are snapped already. Don't snap the operator's own trims unless they ask.
`studio notes --ack NOTE_ID` marks a note seen
without replying. `studio log -n 20` shows the latest events from both of you.

## Before you say a reel is done

[QUALITY.md](QUALITY.md) is the bar. Read it before you build a story. Then, every time:

1. `studio render P`, then `studio check P`. It measures the export against the hard rules
   (cuts on speech edges, first word, pauses, length, loudness, no silence, every voice at one
   level, black or frozen picture, no shot under a second, and at every cut: no stray or flashed
   frames, no jump cut, no clipped word, no click, no held frame; captions against what the
   export really says, each caption with its words and clear of the platform's buttons, the
   last word) and exits 1 if one fails.
2. **Look at the review sheet** it prints the path of: `cut-*.png` (frames, waveform, speech
   and the words heard, 1.5s either side of every cut), one nine-frame strip per cut finding
   (`jump-f00283.png`: the frame numbers are the export's) and `contact.png` (the whole export).
3. A FAIL: fix it (`timeline snap`, trim, split a long pause out, drop the beat), render and
   check again. Up to 3 rounds. Still failing: tell the operator what fails and what to shoot.
4. Answer every judgment rule `check` lists, honestly, one line each. Any "no" sends the reel
   back, or kills it.
5. Only then tell the operator it's done, with what check measured and your answers. "Ready" is
   not yours to say: `studio ready P` says it, for the bytes that were checked. Re-render and
   it's unchecked again.
   Multi-party approval comes later with automation (likely shared via nostr); not built.

### Exceptions: when the rule is wrong for this reel

A hard rule can fail on something that works: a 0.8 s punch, a held silence the picture carries,
a silent cold open on action (≤ 1.5 s). You may grant the exception yourself when you can justify
it: `studio exception grant P pauses CLIP --why "…"` (no CLIP = the whole reel). Bias toward
trying. Write the why so the operator can judge it in one read: what the picture or the line does
that the rule misses. Then check again; the exception shows as `EXC` with your reason, and you
tell the operator which you granted. They may revoke it (`exception.revoked`, with their why);
read those, they are how your taste gets tuned. Only `pauses`, `shortest`, `first word`,
`picture`, `length`, `jump`, `held`, `cover short` and `blip` bend. A label word ("payoff", "hold") no longer exempts anything.

An exception binds to what it was granted on: the clip as it is now (media, in/out, every
effect; not its label), or the whole story when no clip is named. Change that clip (or, for a
reel-wide one, anything in the story) and it lapses: check says so and fails the rule again;
grant it again if it still holds. So grant last, on the cut you mean.

What the picture holds is declared the same way, because nothing measures it: on-screen text
that is the hook (drawn or overlaid) is `exception grant P "first word" CLIP --onscreen "TEXT"
--why "…"` on the opening clip; a designed card (title on a plain ground) is `exception grant P
picture CLIP --card --why "…"`. They show as `EXC`, with what you declared, and can be revoked.
A clip nobody speaks in is picture: its length is never counted as a pause.

### Choices

Every pick is preference data. When the operator picks between versions, moments or lines you
offered, log it: `studio choose P CHOSEN --from A,B,C` (options in the order shown; `none` if they
took none; `--action keep|rewrite`, `--note`). For versions, `studio ab P` makes a blind round
(shuffled copies labelled A, B; the mapping goes in the log), then `studio choose P A --round R`.
The UI logs its own picks (a moment into the story, a post kept or rewritten).

## Showing more: cover, pace, tighten

Talking footage holds the words; it rarely needs to hold the picture. While someone is talking,
**cover** the stretches where the face adds nothing with footage of what the words describe:
`timeline cover P CLIP --with MEDIA IN OUT --at T` keeps the clip's sound unbroken and lays
MEDIA's picture over it from T seconds in. Pick b-roll that shows what is being said, at the
moment it's said ("a quick walk" over the walk, the total over the estimate it's on). Prefer
steady, legible inserts (a screen, the part, the paper) over shaky talking footage. Keep the
face where it carries something: the hook, a reaction, the payoff. QUALITY.md's "True" rule
binds here: never another job's paperwork under this job's numbers, never a line placed over a
different thing than it's about. `check` judges picture-only cuts as picture, not speech.

Cover speaks **reel time**: T (and `--until U`) are seconds into the clip *as it plays* (after
its pace), the clock you see in the export and in `check`; IN and OUT are seconds of MEDIA, which
plays at normal speed, so OUT − IN is also how long it covers. No conversion by hand: the
command says back where the piece lands in the reel, the clip's source seconds under it and the
footage it uses. `--until U` covers to U, `--until-end` to the clip's end (it follows later
trims), `--fill` the whole clip. Nothing is clamped: a span past the clip's end or footage too
short for the span is refused, with the length it needs and what would work; take one of those.
`timeline show` lists every piece in reel time with the footage it shows.

A piece is laid over the clip's words, and stays over them: trim, snap or pace the clip and
it keeps covering the same words (it moves on screen with them). A split gives each side its
part; a tighten or a cut keeps each piece over its words and plays its footage on unbroken
across the cut (a piece laid inside a removed pause is dropped, and the edit says so). An edit
that cuts into a piece, or leaves the clip without its words, says so; such a piece is kept
(it shows nothing) and comes back when a trim brings its words back. A piece laid from the
clip's start covers its first frame, even after a snap moves the in point a frame or two.
Every number a refusal suggests works as printed. If a piece's footage
runs out before the end it was laid to (a `--until-end` piece on a clip trimmed longer, a slower
pace), the edit warns and `check` fails `cover short`: the clip's own picture would show where
b-roll was meant. Lay it again with footage that lasts, cover less, or uncover.

To make a speaker faster but still natural: `timeline pace P CLIP 1.1` (no more than 1.2),
`timeline snap`, then `timeline tighten` (pauses over 0.35s inside speech down to 0.12s, cut in
silence). Tightening only removes silence; it never joins words into something nobody said.

A held shot or pause that is meant (a payoff, a reveal) gets an exception, with why (above).

**Short shots.** A shot between two picture changes that lasts under a second is a `blip`: too
short to read. Hold it longer, or cover across it so the picture before or after runs on (a
cover piece whose footage continues over a cut is one shot). The first and last shot of the
reel and a declared card aren't judged.

## Sound: levels, tone, silence

You don't set levels. `render` brings every spoken clip's speech to one level before the master
loudness pass, and a clip nobody speaks in rides along at the same gain as the median voice; it
says so when the voices were more than 1 dB apart. `check` fails `level step` when a spoken
clip's speech is still more than 2 LU from the median clip's (a clip with under half a second
of speech can't be levelled; give it more, or drop it).

Nothing in a reel is ever silent: `check` fails `dead air` on more than 0.3 s under −60 dBFS. A
clip with no sound of its own (an end card, a silent insert used as its own clip) gets room
tone from the clip before it (its own tail if the source runs on without words, else its
longest stretch between words), low; `render` says which clip it took it from. To choose the
tone yourself, give the card its own sound when you make the file (derived media): a card that
has sound keeps it. A cover piece never needs tone: the clip's sound runs under it.

## Captions

Captions come from the words, three at a time. Two things keep each one with its words: no
caption shows for under 0.4 s (a word from the sentence before or after that the clip's edge
cuts through is dropped; any other short caption joins its neighbour), and the export holds
each caption to the speech the VAD hears. `check` fails `caption stray` on a caption that
still isn't with its words (it names the caption and the clip): trim the clip so the stray
word is out, or fix the word (`words --fix`). Captions wrap inside the middle 70% of
the width; `safe zone` fails one that reaches the top 14%, bottom 20%, left 6% or right 15%.
`product late` is a warning, not a failure: nothing shows or names the product in the first 8 s.

**Jump cuts.** A cut between two moments of the same footage (what `tighten` makes, a trimmed
take, a cover piece that skips ahead in its own file) is a jump cut, and `check` fails it unless
something hides it: cover over one side, a punch-in (derived media), or another shot. A jump
that's meant gets `exception grant P jump CLIP` (the clip after the cut).

**Derived media** (a file you made with ffmpeg and ingested: a card, a punch-in, a sentence with
a tone handle): a room-tone handle is sound only. Never concat picture from another moment of
the file; it shows as frames that belong to neither shot (`flash`). The same bytes ingested again
are the same media (`have NAME → ID`); it stays in its project and any story can use it.

## AI steps

`studio moments ID` and `studio draft ID` call a coding agent's non-interactive mode
(`STUDIO_AGENT` = `claude`, `codex` or `goose`, or any command in `STUDIO_AGENT_CMD`; see the
README's "Using it with your agent"). They cost the operator's plan, so run them only when
asked, on the files asked about. A bare `studio moments` refuses on purpose; `--all` runs
every file still missing the step.

## Working on studio itself

- Code: `studio.py` (CLI and core), `server.py` (the UI's backend), `ui/` (plain ES modules),
  `desktop/` (the Tauri Mac app). The agent adapters are in studio.py under "the agent".
- Tests: `test/run_all.sh` (or `--only cli|server|ui|bench`). They use throwaway homes and fake
  agents (`test/fakebin`, `test/fakeagents`); nothing touches `~/Studio` or calls a real agent.
- Python 3.12+ (the devshell has it); macOS's own python3 is too old for studio.py.
- macOS folder names ignore case: `~/studio` *is* `~/Studio`, the operator's library. Never
  create or clone anything there.
