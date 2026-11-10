# QUALITY.md: the bar every reel clears

The studio makes few things, and makes them well. A reel exists because it gives a viewer
something specific: a fact, a skill, a feeling of "that's my problem too". Not because there
was footage. "This footage has two reels, not six" and "this one has nothing" are good answers.

There are two kinds of rule. **Hard rules** are measured: `studio check` fails the export, and
the agent fixes it before the operator sees it. **Judgment rules** are read: the agent (or a
second reviewer) answers each one honestly, and any "no" sends the reel back or kills it.

Every note the operator leaves that names a defect becomes a rule here (see the end).

## Hard rules (`studio check` enforces these)

| Rule | Threshold |
|---|---|
| No cut inside a word or its decay | audio at each spoken in/out point is below the speech threshold (VAD silence, not Whisper's word times) |
| Lead-in before the first word | 0.05–0.15s |
| Tail after the last word | ≥ 0.25s of decay before the cut |
| Fades at every audio cut | 10–30ms, so no clicks |
| Time to first word or on-screen text | ≤ 0.5s (on-screen text is declared: an exception with `--onscreen`) |
| Longest pause inside speech | ≤ 0.35s. A clip nobody speaks in (b-roll, a cartoon, a card) is picture, not a pause |
| Shortest spoken piece | ≥ 1.5s, and it must be a complete thought (see judgment) |
| Length | 15–35s by default; up to 60s only if every beat earns it |
| Loudness | −14 LUFS integrated (±1), true peak ≤ −1 dBTP |
| No black or frozen frames | none, unless excepted (a designed card: an exception with `--card`) |
| No stray frames at a cut (`flash`) | no frame that belongs to neither shot: two picture changes within 3 frames |
| Every picture cut is in the timeline (`stray`) | no picture change within 0.5s of a cut that isn't the cut (picture from past a clip's edge, a cut off its frame) |
| No one-frame pops (`pop`) | no single black (mean luma < 16) or white (> 239) frame |
| No jump cuts (`jump`) | the same footage either side of a cut, from different moments, is hidden: cover, a punch-in, or another shot |
| No clipped word at a cut (`fragment`) | the 30ms either side of a sound cut stay under the reel's floor + 18 dB, on the side that's speech |
| No click at a cut (`click`) | no sample step at a sound cut 6x the steps around it (and ≥ 0.02 full scale) |
| Every frame on the grid (`held`) | every frame lasts exactly 1/30 s (renders are frame-exact; none is held at a cut) |
| Captions match the audio | the export, re-transcribed, says what the captions say |
| Captions stay clear of platform UI | nothing in the bottom ~20% or the right ~15% of a 9:16 frame |
| Ending | the last spoken word is complete and is not filler ("yeah", "so", "um") |

`check` also warns (never fails) on a **soft cut**: the picture changes again the frame after a
cut, then holds (motion settling, a caption appearing). Usually nothing; look at its strip if it
reads as a stutter.
It finds the cut rules' findings in the export's own pixels (the caption band masked) and sound, at
the timeline's cuts, and puts nine numbered frames around each one on the review sheet
(`jump-f00283.png`, ...).

A reel is **ready** only when `studio check` has measured its exact bytes (sha256) and every hard
rule passes, or every failure is covered by a granted exception (below). A re-render is new bytes:
it starts unchecked. `studio ready P` says which, and the UI shows "Ready" only then.

## Exceptions: bending a hard rule on purpose

Some good reels break a rule on purpose: a 0.8 s punch ("Perfect."), a held silence the picture
carries, a silent cold open on action (≤ 1.5 s, natural sound). The agent may grant an exception
itself when it can say why in words; bias toward trying, the operator tunes:

```bash
studio exception grant P pauses CLIP --why "the beat after 'hours' is the reveal; the picture holds it"
studio exception grant P length --why "12s, and every beat earns it"     # no CLIP: the whole reel
studio exception grant P "first word" 1 --onscreen "3 hours a night" --why "the hook is the text"
studio exception grant P picture 7 --card --why "the end card is a designed still"
studio exception ls P            # what's granted, or lapsed (--all: revoked and invalid too)
studio exception revoke P ID --why "that silence drags"                 # the operator, usually
```

- Rules that may bend: `pauses`, `shortest`, `first word`, `picture`, `length`, `jump` (a jump
  cut that's meant: time skipped on purpose, in plain sight) and `held` (a frame held on purpose).
  The rest (a cut inside a word, loudness, captions against the audio, a filler ending, a stray
  or flashed frame, a clipped word or click at a cut) are defects, never choices.
- An exception binds to what it was granted on: that clip as it was (media, in/out, effects), or
  the whole story for one without a clip. If that changes, it lapses and must be granted again.
- On-screen text that is the hook (`--onscreen`) and a designed card (`--card`) are declarations
  nothing measures, so they are exceptions too: who declared them, why, revocable.
- Every exception is an event (who, rule, clip or whole reel, why). `check` honors it, marks the
  rule `EXC` with the reason, and lists it in its output and in `verdict.txt` on the review sheet.
  The UI lists it under the video, with Revoke.
- Revoking is an event too, carrying what it took back: that is the tuning data. A revoked
  exception makes the reel not ready again until it's fixed or re-checked.
- A word in a clip label no longer exempts anything.

## Judgment rules (answer each one; a "no" sends it back)

1. **One takeaway.** Can you say in one sentence what the viewer gets? If not, it isn't a reel yet.
2. **The hook earns the second second.** The first line is a number, a stake, a surprise or a
   claim, in the speaker's own words. Not a greeting, not "so".
3. **Every spoken beat is a complete thought.** It would make sense said alone. Never a
   fragment picked for the picture.
4. **Every beat earns its place.** Take it out: is the reel worse? If not, it goes.
5. **Show, then say less.** When the picture proves it (the estimate filling, the pile landing),
   let it. Screen inserts hold ≥ 1.5s per line the viewer has to read, and are legible.
6. **The ending pays off.** The total, the finished paper, the reaction. No trailing.
7. **True.** Nothing the brand file's never-claim list forbids; no number the speaker didn't say.
   What you show matches what's said: no other job's paperwork under this job's numbers. Never
   build a sentence nobody said: cutting between complete thoughts is fine, splicing words into
   a new claim is fabrication. A line said about one thing can't be placed over a different
   thing.
8. **Not slop.** No hype words, no caption confetti (emoji, color flashes, a sound on every
   word), no rage bait, no app-screen-with-a-pitch ad.
9. **The operator would post it under their name, unchanged.** If you're not sure, it fails.

## When the footage can't clear the bar

Say so, and say what to shoot. Don't patch a missing line with a fragment. The shoot
playbook (a clean hook to camera three times with two seconds of silence either side,
screen-recording the phone, stop-and-say walks) is what makes reels possible; ask for it.

## Taste

The swipe file (reference reels, analysed) is where these numbers come from and where they get
corrected. When a norm here disagrees with measured medians from the references, the
references win; update this file.

## Rules learned from operator notes

- 2026-11-09, jefe-bmw-sparks: "You clipped it just a little bit too quick right before he
  finished his sentence." → never cut inside the decay of the last word; trust audio, not word times.
- 2026-11-09, jefe-bmw-sparks: "In this clip, nothing really is effectively said at all." →
  every spoken beat is a complete thought; no fragments chosen for the picture.
- 2026-11-09, general: "choppy, too much dead space, wouldn't perform." → pause and length
  limits above; fewer, stronger reels.
- 2026-11-09, jefe-in-my-pocket: two frames of an empty patio flashed between two shots (a
  derived file's room-tone handle carried its own picture). → no stray frames at a cut; a sound
  handle on derived media is sound only, never picture from another moment.
- 2026-11-09, editors: soft first frames and frames held at cuts in the exports (the join was
  re-encoded; parts' sound ran longer than their picture). → renders are frame-exact and join
  losslessly; `held` is a hard rule.
