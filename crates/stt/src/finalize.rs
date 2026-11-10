use crate::decoder::RawSegment;

/// A finalized-or-pending word with absolute time. All words expanded from one
/// whisper segment share that segment's coarse span (v1; word-precise deferred).
#[derive(Clone, Debug, PartialEq)]
pub struct Word {
    pub text: String,
    pub start_ms: u64,
    pub end_ms: u64,
}

/// Incremental, time-anchored overlap-merge finalizer — the productionized
/// `reassemble_dedup` + `finalize` from `spikes/stt-whisper/src/stream.rs`
/// (`RESULTS.md` Table 2: 19% WER at ≤3 s latency, vs 80% for naive segment
/// finalize). `pending` is bounded to ~one chunk; the emitted stream is
/// append-only (a finalized word is never revised).
#[derive(Default)]
pub struct Finalizer {
    pending: Vec<Word>,
    flushed: bool,
}

impl Finalizer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Merge one decoded window (`window_start_ms` + its segments) into `pending`
    /// via the spike's suffix/prefix text overlap, then finalize every word whose
    /// segment ends at/before `horizon_ms` (= next window's start for a normal
    /// window; `u64::MAX` for the flush window). Returns newly finalized words.
    pub fn ingest(
        &mut self,
        window_start_ms: u64,
        segs: &[RawSegment],
        horizon_ms: u64,
    ) -> Vec<Word> {
        let new_words = words_from_segments(window_start_ms, segs);
        self.merge(new_words);
        self.finalize_before(horizon_ms)
    }

    /// Final window with no successor: commit the entire remaining tail.
    pub fn flush(&mut self) -> Vec<Word> {
        if self.flushed {
            return Vec::new();
        }
        self.flushed = true;
        self.finalize_before(u64::MAX)
    }

    /// Volatile preview (un-finalized tail) for greyed UI. Never persisted.
    pub fn preview(&self) -> String {
        self.pending
            .iter()
            .map(|w| w.text.as_str())
            .collect::<Vec<_>>()
            .join(" ")
    }

    /// Merge one decoded window's words into `pending`, deduping the re-decoded
    /// overlap. Only ever appends — existing `pending` words stand (append-only).
    /// TWO seams:
    ///   • **Precise (text) seam** — spike `reassemble_dedup`: the largest *k*
    ///     where `pending`'s last *k* word texts equal the new window's first *k*.
    ///     When the overlap re-decoded identically, this stitches it exactly.
    ///   • **Coarse (time) seam** — first-decode-wins fallback when the text match
    ///     fails (`best == 0`). An all-or-nothing text merge that finds no match
    ///     would append the ENTIRE new window, so a *partially disagreeing* overlap
    ///     (e.g. "needs work" re-decoded as "needs word") would duplicate the
    ///     overlap phrase into the finalized (committed) stream. Instead, use the
    ///     absolute timestamps we deliberately kept: drop the prefix of `new_words`
    ///     whose `end_ms` ≤ the max `end_ms` already in `pending` — those are
    ///     re-transcriptions of audio the finalizer already holds — keeping the
    ///     FIRST decode of the disputed overlap and appending only the genuinely-new
    ///     suffix. Stays O(overlap).
    ///
    /// CAVEAT (word-level timestamps would fix this): `end_ms` is segment-coarse —
    /// every word from one whisper segment shares its end. The fallback is exact
    /// only when the decoder isolates the overlap in its own early-ending segment
    /// (as our scripted tests do). When real whisper lumps the overlap into a
    /// longer phrase-level segment, a divergent overlap can still (a) duplicate —
    /// the covering segment ends past `pending_max_end`, so nothing is dropped —
    /// or (b) drop genuinely-new words that share the covered segment's `end_ms`.
    /// Worst case degrades toward the spike's append-all behavior (its 19% WER
    /// already prices that in); the real fix is per-word timestamps (whisper
    /// token_timestamps), carried to the accuracy-hardening pass.
    fn merge(&mut self, new_words: Vec<Word>) {
        if self.pending.is_empty() {
            self.pending = new_words;
            return;
        }
        let maxk = self.pending.len().min(new_words.len()).min(40);
        let mut best = 0;
        for k in (1..=maxk).rev() {
            let tail = &self.pending[self.pending.len() - k..];
            if tail
                .iter()
                .map(|w| &w.text)
                .eq(new_words[..k].iter().map(|w| &w.text))
            {
                best = k;
                break;
            }
        }
        if best > 0 {
            self.pending.extend(new_words.into_iter().skip(best)); // precise seam
            return;
        }
        // Coarse seam: no text match → drop the time-covered prefix, keep first decode.
        let pending_max_end = self.pending.iter().map(|w| w.end_ms).max().unwrap_or(0);
        self.pending.extend(
            new_words
                .into_iter()
                .skip_while(|w| w.end_ms <= pending_max_end),
        );
    }

    /// Drain and return the front run of words whose segment ends ≤ horizon
    /// (spike `finalize`: `seg.end <= chunk_end − overlap`).
    fn finalize_before(&mut self, horizon_ms: u64) -> Vec<Word> {
        let cut = self
            .pending
            .iter()
            .position(|w| w.end_ms > horizon_ms)
            .unwrap_or(self.pending.len());
        self.pending.drain(..cut).collect()
    }
}

fn words_from_segments(window_start_ms: u64, segs: &[RawSegment]) -> Vec<Word> {
    let mut out = Vec::new();
    for s in segs {
        let start_ms = window_start_ms + (s.start_cs.max(0) as u64) * 10;
        let end_ms = window_start_ms + (s.end_cs.max(0) as u64) * 10;
        for tok in s.text.split_whitespace() {
            out.push(Word {
                text: tok.to_string(),
                start_ms,
                end_ms,
            });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::decoder::RawSegment;

    fn seg(cs0: i64, cs1: i64, t: &str) -> RawSegment {
        RawSegment {
            start_cs: cs0,
            end_cs: cs1,
            text: t.into(),
        }
    }
    fn words(ws: &[Word]) -> Vec<&str> {
        ws.iter().map(|w| w.text.as_str()).collect()
    }

    #[test]
    fn finalizes_incrementally_across_time_shifted_windows() {
        let mut f = Finalizer::new();
        // window 0 [0,5s], horizon 4000: last segment straddles 4s → held.
        let e0 = f.ingest(
            0,
            &[
                seg(0, 180, "order twelve"),
                seg(180, 360, "two by tens"),
                seg(360, 480, "for the"),
            ],
            4_000,
        );
        assert_eq!(words(&e0), vec!["order", "twelve", "two", "by", "tens"]);
        assert_eq!(
            f.preview(),
            "for the",
            "the straddling tail is held, not emitted"
        );
        // window 1 [4s,9s], horizon 8000: head re-says the "for the" overlap.
        let e1 = f.ingest(
            4_000,
            &[
                seg(0, 80, "for the"),
                seg(80, 300, "deck framing"),
                seg(300, 480, "today now"),
            ],
            8_000,
        );
        assert_eq!(words(&e1), vec!["for", "the", "deck", "framing"]);
        // starvation guard: incremental progress, not one end-of-session dump.
        assert!(e0.len() + e1.len() >= 9, "words finalize as windows arrive");
    }

    #[test]
    fn overlap_word_is_finalized_exactly_once() {
        let mut f = Finalizer::new();
        let e0 = f.ingest(
            0,
            &[seg(0, 180, "hello there"), seg(360, 480, "friend")],
            4_000,
        );
        // "friend" ends 4800 > horizon 4000 → held for the overlap.
        let e1 = f.ingest(
            4_000,
            &[seg(0, 80, "friend"), seg(80, 300, "good day")],
            8_000,
        );
        let all: Vec<&str> = words(&e0).into_iter().chain(words(&e1)).collect();
        assert_eq!(
            all.iter().filter(|w| **w == "friend").count(),
            1,
            "overlap emitted once"
        );
    }

    #[test]
    fn append_only_holds_under_overlap_disagreement() {
        let mut f = Finalizer::new();
        let e0 = f.ingest(
            0,
            &[seg(0, 180, "the french drain"), seg(180, 480, "needs work")],
            4_000,
        );
        assert_eq!(words(&e0), vec!["the", "french", "drain"]); // ends ≤4000; "needs work" held
                                                                // Window 1 re-decodes the overlap "needs work" DIFFERENTLY as "needs word":
                                                                // the all-or-nothing text merge finds no match (best=0), so the TIME-ANCHORED
                                                                // fallback drops the re-decoded overlap (end_ms ≤ pending's max end 4800) and
                                                                // keeps W0's first decode, appending only the genuinely-new suffix.
        let e1 = f.ingest(
            4_000,
            &[seg(0, 80, "needs word"), seg(80, 400, "before the pour")],
            8_000,
        );

        let all: Vec<&str> = words(&e0).into_iter().chain(words(&e1)).collect();
        // Committed stream is exactly the first-decode reading with the overlap
        // present ONCE — no "needs work needs word" duplication (the bug this fixes).
        assert_eq!(
            all,
            vec!["the", "french", "drain", "needs", "work", "before", "the", "pour"]
        );
        // First decode of the disputed word wins; the divergent re-decode is gone.
        assert!(
            !all.contains(&"word"),
            "divergent second decode never reaches committed output"
        );
        assert_eq!(
            all.iter().filter(|w| **w == "work").count(),
            1,
            "disputed overlap not duplicated"
        );
        // Genuinely-new content still finalizes.
        assert!(all.contains(&"before") && all.contains(&"pour"));
    }

    #[test]
    fn flush_emits_only_the_bounded_tail() {
        let mut f = Finalizer::new();
        let e0 = f.ingest(
            0,
            &[seg(0, 180, "alpha beta"), seg(360, 480, "gamma delta")],
            4_000,
        );
        assert_eq!(words(&e0), vec!["alpha", "beta"]);
        assert_eq!(
            f.preview(),
            "gamma delta",
            "tail bounded to the straddling segment"
        );
        let tail = f.flush();
        assert_eq!(
            words(&tail),
            vec!["gamma", "delta"],
            "flush finalizes only the held tail"
        );
        assert!(f.flush().is_empty(), "flush is idempotent");
    }
}
