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
| Time to first word or on-screen text | ≤ 0.5s |
| Longest pause inside speech | ≤ 0.35s, unless the picture carries it (a payoff shot, a reveal) and the clip is labelled as such |
| Shortest spoken piece | ≥ 1.5s, and it must be a complete thought (see judgment) |
| Length | 15–35s by default; up to 60s only if every beat earns it |
| Loudness | −14 LUFS integrated (±1), true peak ≤ −1 dBTP |
| No black or frozen frames | none, unless deliberate |
| Captions match the audio | the export, re-transcribed, says what the captions say |
| Captions stay clear of platform UI | nothing in the bottom ~20% or the right ~15% of a 9:16 frame |
| Ending | the last spoken word is complete and is not filler ("yeah", "so", "um") |

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
