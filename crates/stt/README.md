# stt

Copied from `~/athanor-app/crates/stt` (aa37e5f) for studio v2; the only change is that
whisper-rs's `metal` feature is enabled on Apple targets only, so `--features whisper`
also builds on the Linux workers.

On-device streaming speech-to-text over whisper.cpp (spec Rev 2 §2). See
`crates/stt/src/lib.rs` for the module-level docs and API sketch in
`docs/plans/2026-07-04-rust-core-06-stt-crate.md`.

## Models

The crate opens a ggml whisper model the **shell** provisions (download/on-demand-resources
is not the crate's job). v1 target files (MIT, from `huggingface.co/ggerganov/whisper.cpp`;
`ggml-org` returns 401 today — spike note):

- `ggml-base.en-q5_1.bin` (~57 MB) — default; RTF 0.009, WER 5.8% clean.
- `ggml-small.en-q5_1.bin` (~182 MB) — higher accuracy; RTF 0.021, WER 4.7% clean.

Selection (base vs small, quality vs size/battery) is a shell/config decision, informed
by the pending on-device iPhone tier (`RESULTS.md` Table 4).

## Integration with `murmur-core` (deferred to Plan 07 — the FFI/shell tick loop)

`crates/stt` and `murmur-core` do **not** depend on each other. The shell owns both pumps
and wires them:

```
// shell background thread, on cadence:
stt.push_pcm(pcm);                                  // audio thread hands off buffers
for seg in stt.poll()? {                            // append-only finalized segments
    store.append_transcript(&session_id, &format!("{} ", seg.text))?;
}
live_extractor.maybe_extract().await?;              // Plan 05: cursor advances over new transcript
// on DONE:
for seg in stt.end()? { store.append_transcript(&session_id, &format!("{} ", seg.text))?; }
// then queue end-of-session process() — the AUTHORITATIVE pass (Plan 04).
```

Why deferred, not built here: (1) cadence is shell policy (Plan 05 Deferred 3 already put
the LiveExtractor tick in the shell); (2) both `stt.poll` and `LiveExtractor.maybe_extract`
are shell-driven pumps with no core-side coupling (Plan 05 self-review constraint 4);
(3) building it here forces an `stt ↔ murmur-core` dependency both plans avoid. The
contract above is the whole seam — Plan 07 implements it across UniFFI.
