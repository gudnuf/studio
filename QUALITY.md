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
| Never silence (`dead air`) | no stretch longer than 0.3 s under −60 dBFS RMS anywhere in the export: under a card, between clips, at the end |
| Every voice at one level (`level step`) | the speech-gated loudness of each spoken clip (its speech only, by the VAD) is within ±2 LU of the reel's median spoken clip. Warns when the reel's loudness range (LRA) is over 6 LU |
| No black or frozen frames | none, unless excepted (a designed card: an exception with `--card`) |
| No shot too short to read (`blip`) | no shot under 1.0 s (30 frames) between two picture changes, by the frame plan the render draws. The reel's first and last shot have only one change, and a declared card (`--card`) is never judged |
| Every cover runs to its end (`cover short`) | each b-roll piece shows to the end it was laid to (in the frames the render draws): its footage never runs out first, putting the speaker back where b-roll was meant |
| No stray frames at a cut (`flash`) | no frame that belongs to neither shot: two picture changes within 3 frames |
| Every picture cut is in the timeline (`stray`) | no picture change within 0.5s of a cut that isn't the cut (picture from past a clip's edge, a cut off its frame) |
| No one-frame pops (`pop`) | no single black (mean luma < 16) or white (> 239) frame |
| No jump cuts (`jump`) | the same footage either side of a cut, from different moments, is hidden: cover, a punch-in, or another shot |
| No clipped word at a cut (`fragment`) | the 30ms either side of a sound cut stay under the reel's floor + 18 dB, on the side that's speech |
| No click at a cut (`click`) | no sample step at a sound cut 6x the steps around it (and ≥ 0.02 full scale) |
| Every frame on the grid (`held`) | every frame lasts exactly 1/30 s (renders are frame-exact; none is held at a cut) |
| Captions match the audio | the export, re-transcribed, says what the captions say |
| Every caption is with its words (`caption stray`) | caption by caption, not a ratio: it appears in speech, or at most 0.20 s before the voice starts; it never stays up through 0.3 s of nobody speaking; it is never on screen for under 0.4 s |
| Captions stay clear of platform UI (`safe zone`) | nothing in the top 14%, the bottom 20%, the left 6% or the right 15% of a 9:16 frame, measured on the captions as they are drawn |
| Ending | the last spoken word is complete and is not filler ("yeah", "so", "um") |

`check` also warns (never fails) on a **soft cut**: the picture changes again the frame after a
cut, then holds (motion settling, a caption appearing). Usually nothing; look at its strip if it
reads as a stutter. And on **`product late`**: the product isn't on screen by 8 s. It counts the
first caption that names the brand, or the first picture from footage with no sound of its own
(a screen recording, an insert made for the picture), whichever comes first; it can't see a
phone filmed in someone's hand, so read the warning, don't obey it blindly.

The render does its part, so three of these pass at the source:

- **Levels.** Before the master loudness pass, every spoken clip's speech is brought to one
  level; a clip nobody speaks in (natural sound) gets the median of those gains, so it keeps
  its place against the voices.
- **Tone.** A clip with no sound of its own (a card, a silent insert as its own clip) gets room
  tone from the nearest clip that has sound: that shot's own tail when its source runs on
  without words, else its longest stretch between words, laid 20–34 dB under the voices. A
  card whose file carries its own sound keeps it.
- **Captions.** A caption never shows for under 0.4 s: a word of the sentence before or after
  that the clip's edge cuts through is dropped, any other short caption joins its neighbour.
  Each caption is held to the speech the VAD hears (it waits for the voice, and goes 0.2 s
  after it stops), and captions wrap inside the middle 70% of the width.
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
  cut that's meant: time skipped on purpose, in plain sight), `held` (a frame held on purpose),
  `cover short` (a piece that stops early, the face coming back where it works; usually
  better laid again to the length that's meant) and `blip` (a shot under a second that is
  meant: a flash cut the reel is built on).
  The rest (a cut inside a word, loudness, silence, a voice at another level, captions against
  the audio or away from their words or under the platform's buttons, a filler ending, a stray
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
- 2026-11-10, editors (three of them): b-roll meant to cover a paced clip showed a stub of the
  speaker instead (`--at` on the source clock, footage clamped silently). → cover speaks
  reel time, never clamps, and `cover short` fails a piece whose footage stops before its end.
- 2026-11-11, craft review of the three finished reels (athanor reels-craft-research-2026-11):
  two shots of 0.6 s, a second voice 4–6 dB under the first, 2.5–3 s of digital silence under
  two end cards, and the caption "Whereas this" for four frames at the end of a payoff, all in
  reels that had passed. → `blip`, `level step`, `dead air`, `caption stray`, the top and left
  safe zones and the `product late` warning; the render levels voices, lays tone under silent
  clips and keeps every caption with its words.
