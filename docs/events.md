# events.md: the studio event contract

This is **the** contract between every studio writer and reader: the Python reference
(`studio.py`, `server.py`) today, the Rust core (`crates/studio-core`), the phone and the Mac
app tomorrow, and the nostr/Blossom transport between devices. Anything that writes the log
writes what this file says; anything that reads it folds what this file says.

- **Reference implementation.** Until it is deleted (plan slice 4), `studio.py`'s `emit()` and
  `_apply()`/`_finish()` are the executable form of this document. Where they disagree with it,
  that is a bug in one of them; fix both in the same commit.
- **Golden fixtures.** `test/golden/fold/` holds logs and the exact state `studio state --json`
  folds them into (`test/make-golden.sh`). A port is correct when it folds every fixture log into
  byte-identical JSON (after the `$STUDIO_HOME` substitution below).
- **Change rule.** New event types and new optional fields are additive and need no migration.
  Renaming or removing a field, or changing what a type means, is a breaking change: it needs a
  new type name instead. The log is never rewritten.

## 1. The log

`$STUDIO_HOME/events.jsonl`: UTF-8, one JSON object per line, `\n`-terminated, append-only.
It is the only authoritative store; every other file in the library (`timeline.json`,
`check.json`, `ui-focus.json`, …) is a cache or an artifact that an event refers to.

Writers append whole lines in one `O_APPEND` write under an exclusive `flock` on the file.
A writer that died mid-line leaves a fragment with no newline; the next writer starts on a fresh
line (it prepends `\n` when the file does not end in one), so a torn line can only ever lose the
fragment itself.

Readers:

- skip a line that does not parse as JSON, or parses but has no string `type` and object `data`
  (warn, keep going);
- skip an event whose fold step fails on a missing or mistyped field (warn, keep going);
- **ignore event types they do not know.** This is what makes every new type in §4 additive;
- may fold incrementally (only bytes appended since the last fold; a partial last line is left
  for the next read; a shrunk or replaced file restarts the fold from byte 0).

## 2. The envelope

```json
{"id": "019a6a2f-7c41-7d1e-9b0c-3f6e2a8d4b11", "device": "mac",
 "ts": "2026-11-09T16:45:50-08:00", "type": "check.done", "media": null,
 "data": { ... }}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | UUIDv7 (RFC 9562), lowercase, hyphenated. Unique per event, time-ordered and **monotonic within a writer**: two events in the same millisecond still sort in write order (a sub-millisecond counter: Python 3.14 `uuid.uuid7()`; in Rust the `uuid` crate's `v7` with a `ContextV7`, never ms + random). **New in v2**: events written before 2026-11-09 have none. |
| `device` | string | The device that wrote the event: `$STUDIO_DEVICE`, else the host name up to its first dot (`mac`). **New in v2**: absent on old events. Not an identity: every device signs with the one library key; this is a label inside the encrypted payload. |
| `ts` | string | ISO 8601, local time with UTC offset, whole seconds (`isoformat(timespec="seconds")`). |
| `type` | string | Dotted name, §4. |
| `media` | string \| null | The media id the event is about, for media-scoped types (§4 says which); otherwise `null`. |
| `data` | object | The type's fields, §4. Always an object (possibly empty). |

Key order on write is `id, device, ts, type, media, data`; readers must not depend on it.

**Identity and dedupe.** Two events are the same event when their `id`s are equal. For events
without an `id` (pre-v2, and bundle history from older libraries), identity is the canonical
JSON of `[ts, type, media, data]` with sorted keys (`studio.py` `_sig`), which is what
`studio import` already dedupes on. A reader that merges logs from several devices dedupes on
identity first.

**Order.** Within one file, the order of record is file order. Merging devices orders by
`(ts, device, id)`; legacy events (no device) sort before any device at the same `ts`. A fold
applies events in that order. `ts` has one-second resolution, so ties are normal; `id`'s
millisecond prefix breaks them within a device.

**Imported events keep their own envelope, plus `imported: true`.** `studio import` (a bundle
from another library) appends that library's events with their own `ts`, `id` and `device`, and
adds a top-level `"imported": true` (history, not news: a UI doesn't toast it; folds ignore the
key). It imports a subset, not the whole history:

- media-scoped events only for media new to this library, and only `ingested` (its `project`
  remapped, its `path` rewritten to this library), `brand.set`, `transcript.edited`,
  `draft.edited` and `step.*` except `step.proxy`, `step.cut`, `step.failed` (those are redone here);
- of the rest, only `project.created`, `timeline.saved`, `render.done` (its `file` rewritten under
  this library's `projects/`), `note` and `note.ack`, with project slugs remapped when they
  collide (`project.created.slug`, every `project`, `note.focus.project`);
- **not** imported: `check.done`, `exception.*`, `choice`, `blind.round`, jobs, `ui.started`,
  `review.written`. An imported export is therefore unchecked here until checked again.

Events already present (same `[ts, type, media, data]` signature) are skipped, then this library
writes one `bundle.imported` of its own.

## 3. Shared vocabulary

- **media id**: the first 12 hex characters of the source file's sha256 (`ingested.sha256`).
- **clip id, note id, exception id, round id**: 8 lowercase hex characters, random
  (`clip_id()`). Users may type any unique prefix of 4+.
- **project slug**: lowercase, hyphenated, unique in the library (`project new` appends `-2`, …).
  `inbox` is the default project.
- **sha256**: 64 lowercase hex characters of the plaintext bytes. "Ready" binds to an export's
  sha256, never to its file name.
- **actor**: who did it. `operator` (the human in a UI), `agent` (a coding agent driving the CLI),
  `cli` (a human at the terminal). Readers treat any actor other than `operator`/`cli` as an
  agent (`is_agent`). v2 phone events use `operator` with `device` saying which device.
- **paths** inside `data` are relative to `$STUDIO_HOME` (`projects/P/exports/…mp4`), except
  the historic `ingested.path` and `review.written.path` (absolute, written by whatever library
  path was current). Readers resolve a media's source as
  `$STUDIO_HOME/media/<id>/<basename of ingested.path>`, so a library survives being moved.
- **timeline**: `{aspect, captions, clips[], voice?}`.
  `aspect` ∈ `9:16 | 16:9 | 1:1 | 4:5`; `captions` bool; `voice` (the story's default for clips
  without their own) ∈ `natural` (absent: spoken clips get `natural`, the rest `off`). A **clip** is
  `{id, media, in, out, label?, moment?, fit?, voice?, pace?, cover?}`: `in`/`out` seconds in the
  source (3 decimals), `moment` the 1-based moment it came from, `fit` ∈ `fill | fit`,
  `voice` ∈ `off | natural`, `pace` ∈ [1.0, 1.2] (absent = 1), `cover` a list of
  `{media, in, out, at, fit?}`, sorted by `at`: b-roll picture (`in`–`out` of `media`) over the clip's
  sound, starting where the clip's own source is at `at` seconds (source time, like the clip's
  `in`, not seconds into the clip; the CLI's `--at T` is converted).
  `timeline.saved` always carries a normalized timeline (`normalize_timeline`).
- **fp / reel**: 16-hex fingerprints. `fp` = `clip_fp(clip)` (sha256 of the clip's sorted JSON
  minus `id`, `label`, `moment`); `reel` = `reel_fp(timeline)`. Exceptions bind to them.
- **rule names** (check), as seen in the real log: `cuts, shortest, first word, pauses, length,
  loudness, picture, captions, ending, flash, stray, pop, jump, fragment, click, held, soft`.
  Only `pauses, shortest, first word, picture, length, jump, held` can be excepted (`EXCEPTABLE`).

## 4. Event types

**Sync** says whether the type travels over nostr (§6): **yes** for every type except the ones
marked **local**. **Fold** says what `state()` does with it (§5); "log only" types are history:
read by `studio log`, bundles, preference data and future folds, but they change no state today.

The 24 types counted in the operator's library on 2026-11-09 (2,599 lines; counts in brackets),
then the types the code writes that that log has not seen yet, then the v2 types.

### 4.1 Library and media

| Type | media | data | Written by | Fold |
|---|---|---|---|---|
| `ingested` [93] | id | `name` (display file name), `sha256`, `path` (absolute, historic), `project`, `duration` (s, float), `has_audio`, `has_video`, `width`, `height` (display orientation) | ingest | `media[id] = {…data, id, steps: {}, project, path: resolved}` |
| `brand.set` [93] | id | `brand` | ingest | `media[id].brand` |
| `media.moved` [22] | id | `project` | `media move`, UI drag | `media[id].project` |
| `step.proxy` [93] | id | `file` (`proxy.mp4`), `sha256`, `chunks`, `encoder`; or `skipped` | proxy step | `media[id].steps.proxy = data`; clears `error` |
| `step.speech` [93] | id | `regions` (count), `speech_s`, `sha256` (of `speech.json`); or `regions: 0, skipped` | speech (Silero VAD) | `steps.speech` |
| `step.transcribe` [94] | id | `words`, `segments`, `sha256` (of `transcript.json`), `fixed`, `dropped` (hallucinated lines removed), `speech`; or `words: 0, skipped` | whisper | `steps.transcribe` |
| `step.keyframes` [92] | id | `frames`, `every` (s); or `frames: 0, skipped` | keyframes | `steps.keyframes` |
| `step.moments` [32] | id | `moments` (count), `sha256` (of `moments.json`), `provenance` `{provider, model, prompt}` | agent (moments) | `steps.moments`; drops `media[id].draft_edits` |
| `step.draft` [32] | id | `posts`, `sha256` (of `drafts.json`), `provenance` | agent (draft) | `steps.draft` |
| `step.cut` [0] | id | `clips`, `files[] {n, file, sha256}`; or `clips: 0, skipped` | cut | `steps.cut` |
| `step.failed` [3] | id | `step`, `error` (≤ 2000 chars) | any step | `media[id].error = {step, error[:400]}` |
| `transcript.edited` [1] | id | `wrong`, `right`, `count`, `index?` (one word by position), `sha256` (new `transcript.json`), `actor` | `words --fix`, UI | `steps.transcribe.{sha256, edits: +1}` (only if transcribed) |
| `draft.edited` [0] | id | `key` (moment n, string), `field` (`short`, `x`, `linkedin`, …), `text` (`null` = back to the draft), `n`, `actor` | UI | `media[id].draft_edits[key][field]` |

Fixture flag: `step.moments`/`step.draft` written by tests carry `fixture: true` instead of
`sha256`/`provenance`.

### 4.2 Projects and timelines

| Type | media | data | Written by | Fold |
|---|---|---|---|---|
| `project.created` [21] | null | `slug`, `name`, `brand` | `project new`, first use of a slug | `projects[slug] = {slug, name, created: ts, brand, timeline: empty, rev: 0}` (a second one for the same slug resets it) |
| `timeline.saved` [470] | null | `project`, `timeline` (normalized, §3), `actor` | every timeline verb, UI autosave | `timeline = data.timeline`, `rev += 1`, `rev_actor = actor` (ignored for an unknown project) |

### 4.3 Render, check, exceptions

| Type | media | data | Written by | Fold |
|---|---|---|---|---|
| `render.done` [68] | null | `project`, `file`, `duration`, `aspect`, `clips` (count), `sha256`, `actor`, `secs` (wall time), `concat` (`copy`\|`reencode`), `loudness` `{input_i, input_tp, normalized}`, `timeline` (what was rendered), `cuts[] {at, frame, kind: sound\|picture, clip, same}` | `render` | appended to `projects[p].exports` with `ts` |
| `check.done` [61] | null | `project`, `brand`, `file`, `sha256` (the bytes measured), `ok`, `failed[]` (rule names), `failures[] {rule, clip, item, fp, reel}`, `excepted[]` (exception ids that covered a failure at check time), `rules[] {rule, status: pass\|fail\|warn\|skip\|excepted, detail}`, `sheet` (review dir), `actor`; **v2:** `checked_by` (`mac`\|`worker`\|`phone`; absent = `mac`), `predicted` (bool; absent = false. true = the speech rules (cuts, first word, pauses, ending) were predicted from the cut list and the synced speech timings instead of re-hearing the export; length, loudness and picture are still measured. a later check of the same sha256, e.g. a worker's, replaces it in the fold like any re-check) | `check` | `projects[p].checks[sha256] = {sha256, file, ok, failed, failures, actor, ts}` (only with `sha256`; 42 legacy records without it are log only) |
| `exception.granted` [6] | null | `id`, `project`, `brand`, `rule` (normalized name), `clip` (id or null = whole reel), `fp` (clip fp, or reel fp when no clip), `label?`, `kind?` (`onscreen`\|`card`), `text?` (the on-screen hook), `why`, `actor` | `exception grant` | `projects[p].exceptions[id] = {…data, ts}` |
| `exception.revoked` [0] | null | `id`, `project`, `brand`, `rule`, `clip`, `granted_by`, `granted_why`, `granted_ts`, `why`, `actor` (self-contained: tuning data on its own) | `exception revoke`, UI | `exceptions[id].revoked = {ts, actor, why}` (first revocation wins) |

### 4.4 Choices and blind rounds

| Type | media | data | Written by | Fold |
|---|---|---|---|---|
| `choice` [1] | null | `id`, `project`, `brand`, `kind` (`version`\|`moment`\|`draft`\|…\|`other`), `action` (`pick`\|`keep`\|`rewrite`\|`reject`), `shown[]` (option ids in the order shown), `chosen` (id or null = none), `position`, `blind`, `labels?`, `round?`, `note?`, `text?`, `options{id: provenance}`, `actor` | `choose`, UI picks | log only (preference data) |
| `blind.round` [0] | null | `round`, `project`, `brand`, `kind: version`, `labels{A: sha256, …}`, `dir` (`projects/P/blind/ROUND`), `actor` | `ab` | `projects[p].rounds[round] = {…data, ts}` |

### 4.5 Notes

| Type | media | data | Written by | Fold |
|---|---|---|---|---|
| `note` [19] | null | `id`, `text`, `project` (or null), `actor`, `re?` (the note this answers), `focus?` (what the operator had selected, as the UI's `ui-focus.json` beacon: `{project, rev, playhead, playhead_hms, under_playhead {index, number?, id, media, source_time}, selection {index, number, of, id, media, …}, media {id, name} (an object, not an id), transcript_selection, aspect, captions, cli, ts}`; every member optional); **v2:** `spoken?` (bool), `audio?` (path or blob), `focus.sha256?`, `focus.at?` | UI, `say` | appended to `notes` as `{ts, …data, id}` (id falls back to ts) |
| `note.ack` [6] | null | `ids[]`, `actor` | `notes --unread`/`--ack`, `say --re` | sets `acked = ts` on each note not yet acked |

### 4.6 Jobs (the UI server's background work)

| Type | media | data | Sync (§6) | Fold |
|---|---|---|---|---|
| `job.queued` [22] | null | `job` (`<unix>-<n>`), `kind` (`steps`\|`render`\|`check`\|`upload`\|`share`\|`import`), plus the job's info (`ids`, `steps`, `name`, `project`, `resumed`, `via`, `projects`, `out`, `bundle`) | yes | log only |
| `job.started` [22] | null | same as `job.queued` | yes | `jobs[job] = {…data, status: running, ts}` |
| `job.progress` [1226] | id or null | `job` (or null for CLI work), `step`, `pct`, `media_id?`, `project?`, `name?`; `step: waiting` while queued on a lane | **local** | `jobs[job].progress`, `.parts[step]` (max pct), `.media_id`; `progress[media] = {step, pct, ts}` until that step's event |
| `job.done` [22] | null | `job`, `kind`, `result?` (`{ids}`, `{file}`, a check summary, …) | yes | `jobs[job].status = done`, merges data, drops progress |
| `job.failed` [0] | null | `job`, `kind`, `error` (≤ 1500 chars; `server restarted` for jobs a restart orphaned) | yes | `status = failed`, as above |

When more than 400 jobs are folded, the 200 oldest finished ones are dropped from state (the log
keeps them).

### 4.7 Misc

| Type | media | data | Sync (§6) | Fold |
|---|---|---|---|---|
| `ui.started` [7] | null | `url` | **local** | log only |
| `review.written` [0] | null | `path` (absolute), `files` | **local** | log only |
| `bundle.imported` [0] | null | `bundle` (dir name), `files[]` (media ids new to this library), `projects{theirs: ours}`, `renamed`, `actor` | yes | log only |

### 4.8 v2 types (new; specified here, written by the tasks named)

None of these is folded by `studio.py` yet except `project.shelved` (plan task 0.2, for the
seeded golden). Each later task that writes one adds its fold to both implementations and its
fixture lines to `test/golden/`.

| Type | media | data | Written by (plan task) | Fold (when written) |
|---|---|---|---|---|
| `mark` | null | `project`, `sha256` (the export watched), `at` (s on its clock), `dir` (`up`\|`down`), `actor` | phone/Mac screening ↑/↓ (1.6) | `projects[p].marks[]` |
| `agent.status` | null | `text`, `project?`, `actor` | `studio status "…"` | `agent_status = {text, project, ts}` (shown 30 s) |
| `focus.set` | null | `project`, `clip?`, `export?` (sha256), `at?`, `screen?` (`screening`\|`pick`\|`post`), `text?`, `actor` | `studio focus --set` (4.x) | `agent_focus` = the latest |
| `project.shelved` | null | `project`, `why`, `actor` | `studio shelve P --why`, "Not a reel" (4.4) | `projects[p].shelved = {ts, why, actor}` (**folded now**) |
| `post.handoff` | null | `project`, `sha256`, `via` (`share_sheet`\|`mac`\|…), `actor`; `device` is the envelope's | share sheet (1.8), Mac post (4.x) | `exports[i].handoffs[]` for that sha |
| `job.requested` | null | `job` (UUIDv7), `kind` (`render`\|`check`\|`moments`\|…), `project?`, `sha256?`, `media?`, `actor` | phone "cut it again"/"check it" (1.8) | `jobs[job] = {…, status: requested}` |
| `job.taken` | null | `job`, `worker` (device name), `actor` | a worker that starts a requested job | `jobs[job].status = taken`; then `job.done`/`job.failed` as §4.6 |
| `blob.published` | id or null | `sha256` (plaintext, what the studio calls the thing), `blob` (sha256 of the uploaded ciphertext: the Blossom address), `size` (ciphertext bytes), `servers[]` (Blossom base URLs that hold it), `age` (bool: age-encrypted to the library identity), `chunks?` (ordered list of chunk blob hashes when split in 64 MB pieces; then `blob` is absent), `what?` (`proxy`\|`export`\|`state`\|`meta`\|`stem`\|`original`) | `studio sync` (1.x) | `blobs[sha256] = data` |
| `device.paired` | null | `name` (device name), `platform` (`ios`\|`macos`\|`linux`), `actor` | `studio pair --from`, the phone after scanning | `devices[name] = {platform, ts}` |

Reserved, specified when built: `approval.granted`, `approval.revoked` (zen plan task 8),
`check.disagreed` (a worker re-check that disagrees with a phone's predicted check, plan task 5.5).

## 5. Folded state

`studio state --json` prints `state()`. Top level:

```
{ "media":    {id: Media},
  "projects": {slug: Project},
  "notes":    [Note],                 log order
  "jobs":     {job: Job},
  "progress": {media id: {step, pct, ts}} }   local only (from job.progress)
```

- **Media**: the `ingested` data plus `id`, `project`, `path` (resolved, §3), `brand`,
  `steps{step: data}`, `error?` `{step, error}`, `draft_edits?` `{key: {field: text}}`.
- **Project**: `slug, name, created (ts | null), brand, timeline, rev, rev_actor?, exports?[],
  checks?{sha256: Check}, exceptions?{id: Exception}, rounds?{round: Round}, ready?, shelved?`.
  - `exports[i]` = the `render.done` data + `ts` + `verdict`.
  - `verdict` (`export_verdict`): `{state: unchecked, why}` when no check of these bytes exists;
    else `{state: ready | failed, checked (ts), by (actor), failed[] (rule names still open),
    open[] (failures), excepted[] (failure + {exception, by, why, declares?, text?})}`.
    A failure is excepted when a granted, unrevoked exception has its rule, its clip (or none),
    and its fp (clip fp, or reel fp for a reel-wide one).
  - `ready` = the latest export's verdict is `ready` (absent when there are no exports).
  - A project referenced by media but never created (`inbox`, typically) appears with
    `created: null`, brand `jefe`, an empty timeline and rev 0.
- **Note**: `{ts, …data, id, acked?}`.
- **Job**: as §4.6.

**Byte identity.** The golden states are compared byte for byte, so a port must print exactly
what Python's `json.dumps(state, sort_keys=True, indent=1, ensure_ascii=False)` + `"\n"` prints:

- one space of indent per level, one member per line: `,` at line ends (never `", "`)
  and `": "` between key and value; an empty object is `{}` and an empty list `[]` on one line;
- object keys sorted by Unicode code point (Python's `str` order), at every level, including
  maps keyed by ids, slugs and shas;
- non-ASCII written as UTF-8, not `\u` escapes (only `"`, `\\` and control characters are
  escaped, controls as `\n`, `\r`, `\t`, `\b`, `\f` or `\u00XX` lowercase);
- integers as integers; floats as Python's `repr` (shortest round-trip): `45.0`, never `45`;
  `0.1`; exponent form below 1e-4 or from 1e16, as `1e-05` / `1e+16` (two-digit exponent with
  a sign; serde_json writes `1e-5`, so a port formats floats itself); a JSON number the log
  wrote as `45` stays an integer `45`, one written `45.0` stays a float;
- `true`, `false`, `null`; a trailing newline after the closing brace.

Folds never reorder a list: `notes`, `exports`, `failures`, `open`, `excepted`, clips and cover
pieces keep log order. `studio log` is different: it prints `json.dumps(data)[:120]` with the
data keys in the order the writer put them (insertion order, not sorted), so a port keeps each
type's field order as §4 lists it to match that output.

**`$STUDIO_HOME` in fixtures.** `media[id].path` is absolute and depends on where the library is.
The golden state files store it with the library path replaced by the literal `$STUDIO_HOME`;
a port compares after the same substitution.

## 6. Transport: nostr

One studio event = one nostr event. No translation layer: the content *is* the §2 envelope.

| | Kind | Tags | Content |
|---|---|---|---|
| **Log line** | `1737` (regular) | `["j", <library id>]` | NIP-44 v2 ciphertext of the event's JSON line, encrypted self-to-self with the library key |
| **Snapshot pointer** | `31737` (addressable, replaceable per `d`) | `["d", <library id>]` | NIP-44 v2 ciphertext of `{v: 1, blob, sha256, size, servers[], age: true, events, last_id, ts}`: the Blossom blob of the latest folded `state.json` (`sha256` = plaintext hash, `blob` = ciphertext hash), how many events it folds and the `id` of the last one |
| **Blossom auth** | `24242` (BUD-11) | `["t", upload\|get\|delete\|list]`, `["x", <blob sha256>]…`, `["expiration", <unix>]`, `["server", <url>]?` | human-readable action |

Kinds 1737 and 31737 are not in the NIPs kind table (checked 2026-11-09); they are this app's.

**Why `j` for log lines.** The tag must be a single letter: relays index only single-letter tags
for `#x` filters (NIP-01), and a device asks for "this library's log" with `{"kinds": [1737],
"#j": [<library id>]}`. `L` (the first choice) is NIP-32's label-namespace tag, so a generic client
would read the library id as a label namespace. No NIP defines `j` (every single-letter tag in
nostr-protocol/nips master was grepped 2026-11-10; unused lowercase letters were b, j, o, v, w).
The pointer keeps `d`, which is what makes kind 31737 replaceable per library.

**`library id`** = the first 16 lowercase hex characters of the library's nostr public key in its
x-only hex form (the 64-hex `pubkey` of its events), **not** of the bech32 `npub` string.
`crates/studio-net` (`keys.rs`, `library_id()`) is the implementation; `studio pair` compares it
when a second home joins.

- **Signing.** Every device signs with the one library key (v1). The nostr event's `created_at` is
  the studio event's `ts` as unix seconds; the nostr id is not the studio `id` (dedupe on the
  decrypted studio `id`, never on the nostr id).
- **What travels.** Every event type marked **yes** in §4. `job.progress`, `ui.started` and
  `review.written` stay on the device that wrote them. Bytes never travel in events: transcripts,
  speech regions, moments, drafts, keyframes, stems, proxies, exports and originals are blobs,
  referenced by `blob.published`.
- **Size.** A log line is one studio event. The binding limit is NIP-44 v2's: the plaintext of one
  encryption is 1 to **65,535 bytes**. The largest line in the operator's log (2,640 lines,
  2026-11-10) is 2,843 bytes (a `check.done`); `render.done` and `timeline.saved` grow with the
  story (about 2.7 KB today). A relay must also accept the ciphertext event (base64 + JSON +
  signature, about 1.4× the plaintext plus ~400 bytes): `max_event_bytes` ≥ 128 KB (plan S4).
- **Oversize events.** An event whose JSON line is over 65,535 bytes does not go in one log line.
  It is age-encrypted and uploaded as a blob (§7), and the log line carries a stub: the envelope's
  `id`, `device`, `ts`, `type`, `media` with `data` replaced by
  `{"$blob": {"sha256", "blob", "size", "servers"}}` (`sha256` of the plaintext line). A reader
  fetches, decrypts, checks `sha256`, and folds the full event; until it can, it keeps the stub and
  skips it in the fold. Never split one event across several log lines.
- **Ingest.** A device decrypts, dedupes on `id` (§2), appends new events to its own log with their
  envelope intact, and refolds. Unknown types are kept and ignored (§1).
- **Clocks.** Relays reject events with `created_at` too far in the future (often > 15 min), so a
  device with a wrong clock can lose events silently. A publisher checks the relay's `OK` for every
  event and keeps unacknowledged ones queued; it never publishes a `ts` more than a minute ahead
  of the newest relay timestamp it has seen.
- **Bootstrap.** A new device fetches the pointer for its library id, fetches and decrypts the
  snapshot blob, then reads log lines newer than the snapshot's `ts` (minus a minute of slack;
  dedupe absorbs the overlap).

## 7. Blossom

BUD-01/02/06/11 only, so any server works. Every private blob is age-encrypted to the library's
x25519 identity before upload; its Blossom address is the ciphertext's sha256, and
`blob.published` pairs it with the plaintext sha256 the rest of the log uses. Originals and any
blob over 64 MB are split into 64 MB ciphertext chunks, each its own blob; `blob.published.chunks`
lists them in order, and resume = `HEAD /<chunk>` for each, uploading the missing ones. Server
URLs, relay URLs and key paths come from `$STUDIO_HOME/studio.toml` (`STUDIO_RELAY`,
`STUDIO_BLOSSOM` override in tests); nothing in an event is specific to one deployment except the
`servers` a blob was published to.
