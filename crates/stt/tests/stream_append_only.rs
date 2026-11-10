//! Append-only streaming contract (spec Rev 2 §2) via the public API and a
//! scripted decoder using the REALISTIC time-shifted composition model (window
//! k+1's segments start at chunk-relative cs=0, four seconds later in absolute
//! time; only the 1 s overlap word repeats). Proves the finalized stream Plan 05's
//! LiveExtractor consumes finalizes incrementally, dedups the overlap, never
//! revises a committed word, and end() flushes only the bounded tail.

use stt::{RawSegment, ScriptedDecoder, SttConfig, SttStream};

fn seg(cs0: i64, cs1: i64, t: &str) -> RawSegment {
    RawSegment {
        start_cs: cs0,
        end_cs: cs1,
        text: t.into(),
    }
}

#[test]
fn finalized_stream_is_append_only_across_a_session() {
    // Sentence "the french drain needs regrading before the pour today" spoken
    // over ~13 s. Each window re-decodes only its 1 s overlap head; W1's overlap
    // re-transcribes the held word "needs" imperfectly as "kneads" — the merge's
    // TIME FALLBACK drops that divergent re-decode, keeps W0's first decode, and
    // never duplicates or revises a committed word.
    let decoder = ScriptedDecoder::new(vec![
        // W0 [0,5s] horizon 4s: "the french drain" ≤4s finalizes; "needs" held
        vec![
            seg(0, 180, "the french"),
            seg(180, 360, "drain"),
            seg(360, 480, "needs"),
        ],
        // W1 [4,9s] horizon 8s: overlap re-decodes "needs" as "kneads" (dropped by
        // the time fallback → first decode "needs" wins); extends; "before" held
        vec![
            seg(0, 80, "kneads"),
            seg(80, 300, "regrading"),
            seg(300, 480, "before"),
        ],
        // W2 [8,13s] horizon 12s: overlap re-says "before", extends; "today" held
        vec![
            seg(0, 80, "before"),
            seg(80, 300, "the pour"),
            seg(300, 480, "today"),
        ],
        // flush [12,~13s] horizon ∞: re-says "today"
        vec![seg(0, 80, "today")],
    ]);
    let stream = SttStream::with_decoder(Box::new(decoder), SttConfig::default(), &[]);
    stream.push_pcm(&vec![0.0; 208_000]); // ~13 s → three windows (5s/1s → step 4s)

    // One poll drains every ready window; loop until it stops finalizing new words.
    let mut finalized = Vec::new();
    loop {
        let batch = stream.poll().unwrap();
        if batch.is_empty() {
            break;
        }
        finalized.extend(batch);
    }
    finalized.extend(stream.end().unwrap()); // DONE flushes the held tail

    let text: Vec<&str> = finalized.iter().map(|s| s.text.as_str()).collect();
    // Incremental, in order, append-only. "the french drain" was committed in W0
    // and is never revisited (that audio is behind the window, dropped by the
    // Chunker) — the stream only ever appended.
    assert!(text.starts_with(&["the", "french", "drain"]));
    assert!(text.contains(&"regrading") && text.contains(&"pour") && text.contains(&"today"));
    // Overlap words are finalized exactly once (dedup), not doubled.
    assert_eq!(
        text.iter().filter(|w| **w == "before").count(),
        1,
        "overlap deduped"
    );
    assert_eq!(text.iter().filter(|w| **w == "today").count(), 1);
    // W1 re-decoded the held word "needs" as "kneads"; the time fallback keeps the
    // first decode and drops the divergent one — no duplication, "kneads" nowhere.
    assert!(
        text.contains(&"needs"),
        "first decode of the disputed overlap survives"
    );
    assert!(
        !text.contains(&"kneads"),
        "divergent re-decode never reaches committed output"
    );
    assert_eq!(
        text.iter().filter(|w| **w == "needs").count(),
        1,
        "disputed overlap not duplicated"
    );

    // Absolute-ms timestamps are monotonic — append-only in time.
    let mut prev = 0;
    for s in &finalized {
        assert!(s.start_ms >= prev);
        prev = s.start_ms;
    }
}
