/// A window ready to decode. `start_sample` is absolute (from stream start) for
/// converting chunk-relative segment timestamps to absolute ms. `is_final` marks
/// the flush tail (finalizer uses ∞ horizon on it — nothing comes after).
pub struct Window {
    pub start_sample: u64,
    pub samples: Vec<f32>,
    pub is_final: bool,
}

/// Accumulates PCM and cuts fixed windows with overlap. Pure; no decode, no I/O.
/// Frees audio behind the window cursor to bound memory over an hour-long session.
pub struct Chunker {
    chunk_len: usize, // samples per window
    step: usize,      // samples between window starts (chunk_len - overlap)
    buf: Vec<f32>,    // samples from `buf_start` onward
    buf_start: u64,   // absolute sample index of buf[0]
    next_start: u64,  // absolute sample index of the next window to emit
    done: bool,
}

impl Chunker {
    pub fn new(sample_rate: u32, chunk_secs: f64, overlap_secs: f64) -> Self {
        let sr = sample_rate as f64;
        let chunk_len = (chunk_secs * sr) as usize;
        let step = (((chunk_secs - overlap_secs).max(0.1)) * sr) as usize;
        Self {
            chunk_len,
            step,
            buf: Vec::new(),
            buf_start: 0,
            next_start: 0,
            done: false,
        }
    }

    pub fn push(&mut self, pcm: &[f32]) {
        self.buf.extend_from_slice(pcm);
    }

    // Test-only introspection (memory-bound assertion); not used by production
    // code, so gated to avoid a dead_code warning in the non-test lib target.
    #[cfg(test)]
    pub fn buffered_samples(&self) -> usize {
        self.buf.len()
    }

    /// Yields the next full window if enough audio has arrived, advancing the
    /// cursor by `step` and freeing audio behind the new cursor.
    pub fn take_ready_window(&mut self) -> Option<Window> {
        let rel_start = (self.next_start - self.buf_start) as usize;
        let rel_end = rel_start + self.chunk_len;
        if rel_end > self.buf.len() {
            return None;
        }
        let samples = self.buf[rel_start..rel_end].to_vec();
        let window = Window {
            start_sample: self.next_start,
            samples,
            is_final: false,
        };
        self.next_start += self.step as u64;
        self.free_consumed();
        Some(window)
    }

    /// The short final window: everything from the cursor to the buffer end,
    /// marked `is_final`. Call once at end()/flush(); returns None if empty.
    pub fn flush(&mut self) -> Option<Window> {
        if self.done {
            return None;
        }
        self.done = true;
        let rel_start = (self.next_start - self.buf_start) as usize;
        if rel_start >= self.buf.len() {
            return None;
        }
        let samples = self.buf[rel_start..].to_vec();
        Some(Window {
            start_sample: self.next_start,
            samples,
            is_final: true,
        })
    }

    fn free_consumed(&mut self) {
        // Retain from the next window's start (which sits `overlap` before
        // `next_start`)... simplest correct bound: keep from `next_start`.
        let keep_from = self.next_start.min(self.buf_start + self.buf.len() as u64);
        let drop_n = (keep_from - self.buf_start) as usize;
        if drop_n > 0 {
            self.buf.drain(..drop_n);
            self.buf_start = keep_from;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 16 kHz: 5 s = 80_000 samples, 1 s overlap → step = 4 s = 64_000 samples.
    fn chunker() -> Chunker {
        Chunker::new(16_000, 5.0, 1.0)
    }

    #[test]
    fn yields_nothing_until_a_full_window_arrives() {
        let mut c = chunker();
        c.push(&vec![0.0; 79_999]);
        assert!(
            c.take_ready_window().is_none(),
            "one sample short of a window"
        );
        c.push(&[0.0]);
        let w = c.take_ready_window().expect("full window now ready");
        assert_eq!(w.start_sample, 0);
        assert_eq!(w.samples.len(), 80_000);
    }

    #[test]
    fn steps_by_chunk_minus_overlap() {
        let mut c = chunker();
        c.push(&vec![0.0; 144_000]); // 9 s → windows [0,5s) and [4s,9s)
        let w0 = c.take_ready_window().unwrap();
        assert_eq!(w0.start_sample, 0);
        let w1 = c.take_ready_window().unwrap();
        assert_eq!(
            w1.start_sample, 64_000,
            "advanced by 4 s, re-decoding the 1 s overlap"
        );
        assert!(c.take_ready_window().is_none());
    }

    #[test]
    fn flush_emits_the_short_final_window() {
        let mut c = chunker();
        c.push(&vec![0.0; 32_000]); // 2 s only — never fills a 5 s window
        assert!(c.take_ready_window().is_none());
        let w = c.flush().expect("flush yields the remaining tail");
        assert_eq!(w.start_sample, 0);
        assert_eq!(w.samples.len(), 32_000);
        assert!(w.is_final);
        assert!(c.flush().is_none(), "nothing left after flush");
    }

    #[test]
    fn drops_consumed_prefix_to_bound_memory() {
        let mut c = chunker();
        c.push(&vec![0.0; 144_000]);
        c.take_ready_window().unwrap(); // consumes through step=64_000
        c.take_ready_window().unwrap();
        // Buffer retains only from the last window start onward, not all 9 s.
        assert!(
            c.buffered_samples() <= 80_000,
            "old audio behind the cursor is freed"
        );
    }
}
