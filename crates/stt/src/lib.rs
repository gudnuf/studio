//! On-device streaming STT over whisper.cpp (spec Rev 2 §2). PCM in → append-only
//! finalized transcript segments out, biased by the user's ≤100-term vocabulary.
//! The whisper backend is behind the `whisper` feature; the pure chunk/finalize/
//! bias logic compiles and tests everywhere with no native toolchain or model file.

mod bias;
mod chunk;
mod decoder;
mod endpoint;
mod finalize;
#[cfg(feature = "whisper")]
mod whisper;

pub use decoder::{Decoder, RawSegment, ScriptedDecoder};
pub use endpoint::{EndpointConfig, Endpointer};
#[cfg(feature = "whisper")]
pub use whisper::WhisperDecoder;

/// A finalized, never-to-be-revised transcript segment (append-only contract).
/// Timestamps are ABSOLUTE audio milliseconds from stream start. The shell
/// appends `text` to `Store::append_transcript` (Plan 05 cursor feeder).
#[derive(Clone, Debug, PartialEq)]
pub struct FinalizedSegment {
    pub start_ms: u64,
    pub end_ms: u64,
    pub text: String,
}

/// Lightweight, read-only runtime metrics for the responsiveness overlay
/// (operator's first-device feedback: "slow to pick up… doesn't feel
/// responsive"). Pure observability — nothing here feeds back into decoding.
/// The shell (or its os_log) reads a snapshot on its poll cadence.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SttMetrics {
    /// Wall time of the most recent `decode()` call (ms). This is the number
    /// that governs felt latency on-device.
    pub last_decode_ms: u64,
    /// Audio length of the most recently decoded window (ms). ~5000 for a full
    /// rolling window, shorter for the flush tail.
    pub last_window_ms: u64,
    /// Realtime factor of the last decode: `decode_ms / window_ms`. < 1.0 means
    /// the phone decodes faster than realtime (the on-device bar). > 1.0 means
    /// it is falling behind and latency will accumulate.
    pub realtime_factor: f64,
    /// Count of decode passes since stream open (each ~one rolling window).
    pub decode_passes: u64,
    /// Whether the whisper backend REQUESTED GPU (Metal). True on real devices,
    /// false on the iOS Simulator (ggml-metal traps there) and for the pure
    /// test decoder. Proves the on-device Metal path is actually taken.
    pub gpu_requested: bool,
    /// Audio-domain latency of the last utterance-end fire (ms): how long after
    /// the learner stopped speaking the turn auto-sent. Equals the endpoint
    /// silence latch. Zero until the first endpointed turn.
    pub utterance_end_latency_ms: u64,
}

#[derive(Clone, Debug)]
pub struct SttConfig {
    /// Decode window length (spike default 5 s).
    pub chunk_secs: f64,
    /// Overlap re-decoded each window for LocalAgreement (spike default 1 s).
    pub overlap_secs: f64,
    /// Sample rate the shell must feed (whisper wants 16 kHz mono f32).
    pub sample_rate: u32,
    /// Whisper language hint ("en" for the *.en models).
    pub language: String,
    /// Hard cap on vocabulary terms injected via initial_prompt (spec: ≤100).
    pub max_bias_terms: usize,
    /// Optional energy-based endpointing (design doc "Voice — the Bellows",
    /// delta 1): auto-fires "utterance ended" after sustained trailing
    /// silence, so the shell doesn't need to send an explicit DONE. `None`
    /// (default) disables it entirely — `push_pcm`/`poll`/`end` behave
    /// exactly as before; this is additive, not a replacement for `end()`.
    pub endpoint: Option<EndpointConfig>,
}

impl Default for SttConfig {
    fn default() -> Self {
        Self {
            chunk_secs: 5.0,
            overlap_secs: 1.0,
            sample_rate: 16_000,
            language: "en".into(),
            max_bias_terms: 100,
            endpoint: None,
        }
    }
}

impl SttConfig {
    /// Reject configs the pipeline math can't honor. `overlap_secs >= chunk_secs`
    /// makes the finalize horizon (`chunk_len_ms − overlap_ms`, u64) underflow and
    /// leaves no forward progress per window, so it is a `Config` error. Called by
    /// `SttStream::with_model` (the production constructor); `with_decoder` also
    /// guards the horizon with `saturating_sub` for the test/FFI seam.
    pub fn validate(&self) -> Result<(), SttError> {
        if self.overlap_secs >= self.chunk_secs {
            return Err(SttError::Config(format!(
                "overlap_secs ({}) must be < chunk_secs ({})",
                self.overlap_secs, self.chunk_secs
            )));
        }
        Ok(())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SttError {
    #[error("model load failed: {0}")]
    ModelLoad(String),
    #[error("decode failed: {0}")]
    Decode(String),
    #[error("invalid config: {0}")]
    Config(String),
}

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use chunk::Chunker;
use finalize::Finalizer;

struct Engine {
    decoder: Box<dyn Decoder>,
    chunker: Chunker,
    finalizer: Finalizer,
    #[cfg(test)]
    captured_prompts: Vec<Option<String>>,
}

pub struct SttStream {
    cfg: SttConfig,
    bias_prompt: Option<String>,
    input: Mutex<Vec<f32>>, // pending PCM handed off from the audio thread
    engine: Mutex<Engine>,
    // Endpointing (additive, optional — see `SttConfig::endpoint`). Fed the
    // same raw PCM as `input`, upstream of the Chunker, since silence can
    // span a chunk boundary.
    endpointer: Mutex<Option<endpoint::Endpointer>>,
    utterance_ended: AtomicBool,
    // Read-only responsiveness metrics (see `SttMetrics`). Updated on decode
    // (poll/end) and on endpoint fire (push_pcm); read via `metrics()`.
    metrics: Mutex<SttMetrics>,
}

impl SttStream {
    pub fn with_decoder(decoder: Box<dyn Decoder>, cfg: SttConfig, vocab: &[String]) -> Self {
        let bias_prompt = bias::build_bias_prompt(vocab, cfg.max_bias_terms);
        let chunker = Chunker::new(cfg.sample_rate, cfg.chunk_secs, cfg.overlap_secs);
        let endpointer = cfg
            .endpoint
            .clone()
            .map(|ecfg| endpoint::Endpointer::new(cfg.sample_rate, ecfg));
        SttStream {
            input: Mutex::new(Vec::new()),
            engine: Mutex::new(Engine {
                decoder,
                chunker,
                finalizer: Finalizer::new(),
                #[cfg(test)]
                captured_prompts: Vec::new(),
            }),
            bias_prompt,
            endpointer: Mutex::new(endpointer),
            utterance_ended: AtomicBool::new(false),
            metrics: Mutex::new(SttMetrics::default()),
            cfg,
        }
    }

    /// A snapshot of the current responsiveness metrics (see `SttMetrics`).
    /// Cheap: a short lock and a clone. Safe to call from any thread.
    pub fn metrics(&self) -> SttMetrics {
        self.metrics.lock().unwrap().clone()
    }

    /// Record whether the underlying decoder requested GPU/Metal. Called once
    /// by `with_model` (whisper backend); the pure test decoder leaves it false.
    #[cfg(feature = "whisper")]
    fn set_gpu_requested(&self, gpu: bool) {
        self.metrics.lock().unwrap().gpu_requested = gpu;
    }

    #[cfg(feature = "whisper")]
    pub fn with_model(
        model: &std::path::Path,
        cfg: SttConfig,
        vocab: &[String],
    ) -> Result<Self, SttError> {
        cfg.validate()?; // reject overlap ≥ chunk before opening the model
        let decoder = whisper::WhisperDecoder::open(model, &cfg.language)?;
        let stream = Self::with_decoder(Box::new(decoder), cfg, vocab);
        stream.set_gpu_requested(whisper::gpu_requested());
        Ok(stream)
    }

    /// Buffer PCM. Cheap: a short lock, no decode. Call OFF the real-time audio
    /// thread (hand buffers over from the AVAudioEngine tap — research Q6).
    pub fn push_pcm(&self, pcm: &[f32]) {
        self.input.lock().unwrap().extend_from_slice(pcm);
        if let Some(ep) = self.endpointer.lock().unwrap().as_mut() {
            if ep.push(pcm) {
                self.utterance_ended.store(true, Ordering::SeqCst);
                self.metrics.lock().unwrap().utterance_end_latency_ms = ep.last_latency_ms();
            }
        }
    }

    /// True if endpointing (see `SttConfig::endpoint`) has detected the turn
    /// ended (sustained trailing silence after a real utterance) since the
    /// last call. Latched and auto-cleared on read: the shell polls this on
    /// its `push_pcm` cadence and calls `end()` when it flips true — this
    /// signal never calls `end()` itself, so the explicit-end API is
    /// untouched. Always `false` when `cfg.endpoint` is `None`.
    pub fn utterance_ended(&self) -> bool {
        self.utterance_ended.swap(false, Ordering::SeqCst)
    }

    /// Reset endpointing state only (e.g. the shell starting a fresh turn
    /// after acting on `utterance_ended()`, or a manual tap-to-send). Does
    /// not touch buffered PCM or any finalized/pending transcript state.
    pub fn reset_endpoint(&self) {
        if let Some(ep) = self.endpointer.lock().unwrap().as_mut() {
            ep.reset();
        }
        self.utterance_ended.store(false, Ordering::SeqCst);
    }

    /// Drain buffered PCM into the chunker and decode every window now ready,
    /// returning all segments finalized this call (append-only). Runs the long
    /// Metal decode on the CALLER's thread — the shell calls this from a
    /// background thread on its own cadence (Plan 05 Deferred 3).
    pub fn poll(&self) -> Result<Vec<FinalizedSegment>, SttError> {
        let mut eng = self.engine.lock().unwrap(); // engine first...
        {
            let mut input = self.input.lock().unwrap(); // ...then input, briefly
            eng.chunker.push(&input);
            input.clear();
        } // input released before decode
        let mut out = Vec::new();
        while let Some(w) = eng.chunker.take_ready_window() {
            self.decode_window(&mut eng, w, &mut out)?;
        }
        Ok(out)
    }

    /// Volatile preview tail for greyed UI. Never persisted, never append-only.
    pub fn preview_tail(&self) -> String {
        self.engine.lock().unwrap().finalizer.preview()
    }

    /// DONE (supersedes cancel-for-speed canon): flush the remaining buffered
    /// audio as a final window and finalize everything pending. Idempotent.
    pub fn end(&self) -> Result<Vec<FinalizedSegment>, SttError> {
        let mut eng = self.engine.lock().unwrap();
        {
            let mut input = self.input.lock().unwrap();
            eng.chunker.push(&input);
            input.clear();
        }
        let mut out = Vec::new();
        while let Some(w) = eng.chunker.take_ready_window() {
            self.decode_window(&mut eng, w, &mut out)?;
        }
        if let Some(w) = eng.chunker.flush() {
            // is_final window → decode_window uses an ∞ horizon → finalizes all.
            self.decode_window(&mut eng, w, &mut out)?;
        } else {
            // Nothing left to decode, but the last normal window may have held a
            // tail behind its horizon — flush it.
            emit(&mut out, eng.finalizer.flush());
        }
        Ok(out)
    }

    fn decode_window(
        &self,
        eng: &mut Engine,
        w: chunk::Window,
        out: &mut Vec<FinalizedSegment>,
    ) -> Result<(), SttError> {
        let window_start_ms = self.sample_to_ms(w.start_sample);
        let horizon_ms = if w.is_final {
            u64::MAX
        } else {
            // saturating_sub guards the test/FFI seam (with_decoder skips validate);
            // with_model rejects overlap ≥ chunk up front so this can't underflow there.
            window_start_ms + self.chunk_len_ms().saturating_sub(self.overlap_ms())
        };
        let window_ms = self.sample_to_ms(w.samples.len() as u64);
        let started = std::time::Instant::now();
        let raw = eng.decode_with_prompt(&w.samples, self.bias_prompt.as_deref())?;
        let decode_ms = started.elapsed().as_millis() as u64;
        {
            let mut m = self.metrics.lock().unwrap();
            m.last_decode_ms = decode_ms;
            m.last_window_ms = window_ms;
            m.realtime_factor = if window_ms > 0 {
                decode_ms as f64 / window_ms as f64
            } else {
                0.0
            };
            m.decode_passes += 1;
        }
        emit(out, eng.finalizer.ingest(window_start_ms, &raw, horizon_ms));
        Ok(())
    }

    fn sample_to_ms(&self, sample: u64) -> u64 {
        sample * 1000 / self.cfg.sample_rate as u64
    }
    fn chunk_len_ms(&self) -> u64 {
        (self.cfg.chunk_secs * 1000.0) as u64
    }
    fn overlap_ms(&self) -> u64 {
        (self.cfg.overlap_secs * 1000.0) as u64
    }

    #[cfg(test)]
    fn debug_captured_prompts(&self) -> Vec<Option<String>> {
        self.engine.lock().unwrap().captured_prompts.clone()
    }
}

impl Engine {
    fn decode_with_prompt(
        &mut self,
        samples: &[f32],
        prompt: Option<&str>,
    ) -> Result<Vec<RawSegment>, SttError> {
        #[cfg(test)]
        self.captured_prompts.push(prompt.map(str::to_string));
        self.decoder.decode(samples, prompt)
    }
}

/// Map finalized `Word`s to `FinalizedSegment`s, preserving each word's
/// (segment-coarse) absolute span.
fn emit(out: &mut Vec<FinalizedSegment>, words: Vec<finalize::Word>) {
    out.extend(words.into_iter().map(|w| FinalizedSegment {
        start_ms: w.start_ms,
        end_ms: w.end_ms,
        text: w.text,
    }));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg(cs0: i64, cs1: i64, t: &str) -> RawSegment {
        RawSegment {
            start_cs: cs0,
            end_cs: cs1,
            text: t.into(),
        }
    }
    fn text(v: &[FinalizedSegment]) -> Vec<&str> {
        v.iter().map(|s| s.text.as_str()).collect()
    }

    fn sine(n_samples: usize, freq_hz: f32, amplitude: f32) -> Vec<f32> {
        let sr = SttConfig::default().sample_rate as f32;
        (0..n_samples)
            .map(|i| {
                let t = i as f32 / sr;
                amplitude * (2.0 * std::f32::consts::PI * freq_hz * t).sin()
            })
            .collect()
    }

    #[test]
    fn utterance_ended_is_always_false_when_endpointing_is_not_configured() {
        // Default SttConfig has endpoint: None — push_pcm/poll/end must be
        // unaffected, and utterance_ended must never latch.
        let stream = SttStream::with_decoder(
            Box::new(ScriptedDecoder::new(vec![])),
            SttConfig::default(),
            &[],
        );
        // Feed plenty of silence — if endpointing were somehow active this
        // would fire; with it disabled it must stay false forever.
        for _ in 0..100 {
            stream.push_pcm(&vec![0.0; 480]);
        }
        assert!(!stream.utterance_ended());
    }

    #[test]
    fn utterance_ended_latches_on_trailing_silence_and_clears_on_read() {
        let cfg = SttConfig {
            endpoint: Some(EndpointConfig::default()),
            ..SttConfig::default()
        };
        let stream = SttStream::with_decoder(Box::new(ScriptedDecoder::new(vec![])), cfg, &[]);

        // 12 windows (360 ms) of voiced audio — over the 300 ms guard.
        for _ in 0..12 {
            stream.push_pcm(&sine(480, 440.0, 0.5));
            assert!(!stream.utterance_ended());
        }
        // 39 windows of silence: not yet.
        for _ in 0..39 {
            stream.push_pcm(&vec![0.0; 480]);
            assert!(!stream.utterance_ended());
        }
        // 40th silence window (1200 ms total): latches true.
        stream.push_pcm(&vec![0.0; 480]);
        assert!(
            stream.utterance_ended(),
            "sustained trailing silence must latch utterance_ended"
        );
        // Reading it clears the latch.
        assert!(!stream.utterance_ended(), "latch clears after being read");
    }

    #[test]
    fn reset_endpoint_clears_latch_and_progress_without_touching_transcript_state() {
        let cfg = SttConfig {
            endpoint: Some(EndpointConfig::default()),
            ..SttConfig::default()
        };
        let stream = SttStream::with_decoder(Box::new(ScriptedDecoder::new(vec![])), cfg, &[]);
        for _ in 0..12 {
            stream.push_pcm(&sine(480, 440.0, 0.5));
        }
        for _ in 0..40 {
            stream.push_pcm(&vec![0.0; 480]);
        }
        // Don't read utterance_ended() yet — reset_endpoint should clear it.
        stream.reset_endpoint();
        assert!(
            !stream.utterance_ended(),
            "reset_endpoint clears a pending latch"
        );
    }

    #[test]
    fn bias_prompt_is_passed_to_every_decode() {
        // 9 s of PCM → two 5 s/1 s windows, both drained in one poll() call.
        let decoder = ScriptedDecoder::new(vec![
            vec![seg(0, 300, "the french drain")],
            vec![seg(0, 80, "drain"), seg(80, 300, "is backing")],
        ]);
        let stream = SttStream::with_decoder(
            Box::new(decoder),
            SttConfig::default(),
            &["french drain".to_string()],
        );
        stream.push_pcm(&vec![0.0; 144_000]);
        stream.poll().unwrap();
        // The scripted decoder recorded the prompt each decode saw.
        let prompts = stream.debug_captured_prompts();
        assert_eq!(prompts.len(), 2, "both ready windows decoded");
        assert!(prompts
            .iter()
            .all(|p| p.as_deref() == Some("Terms used in this session: french drain.")));
    }

    #[test]
    fn poll_finalizes_incrementally_and_end_flushes_bounded_tail() {
        // REALISTIC time-shifted composition (NOT superstrings): window k+1's
        // segments start at chunk-relative cs=0, four seconds later in absolute
        // time; only the 1 s overlap words repeat.
        let decoder = ScriptedDecoder::new(vec![
            // window 0 [0,5s]: "for the" straddles the 4 s horizon → held
            vec![
                seg(0, 180, "order twelve"),
                seg(180, 360, "two by tens"),
                seg(360, 480, "for the"),
            ],
            // window 1 [4,9s]: head re-says the "for the" overlap, "today" straddles 8 s
            vec![
                seg(0, 80, "for the"),
                seg(80, 300, "deck framing"),
                seg(300, 480, "today"),
            ],
            // flush window [8,~9s]: re-says the "today" overlap
            vec![seg(0, 80, "today")],
        ]);
        let stream = SttStream::with_decoder(Box::new(decoder), SttConfig::default(), &[]);
        stream.push_pcm(&vec![0.0; 144_000]); // 9 s → W0 + W1 both ready
        let live = stream.poll().unwrap(); // one poll drains BOTH ready windows
        assert_eq!(
            text(&live),
            vec!["order", "twelve", "two", "by", "tens", "for", "the", "deck", "framing"]
        );
        assert_eq!(
            stream.preview_tail(),
            "today",
            "the straddling tail is held, bounded"
        );
        let tail = stream.end().unwrap(); // flush finalizes only the held tail
        assert_eq!(text(&tail), vec!["today"]);
        // append-only in time: start_ms non-decreasing across the whole stream.
        let mut prev = 0;
        for s in live.iter().chain(tail.iter()) {
            assert!(s.start_ms >= prev);
            prev = s.start_ms;
        }
    }

    #[test]
    fn metrics_capture_decode_pass_and_window_length() {
        // One 5 s window decoded → metrics reflect a decode pass whose window
        // is 5000 ms; RTF = decode_ms/5000. gpu_requested stays false for the
        // pure test decoder (no whisper backend).
        let decoder = ScriptedDecoder::new(vec![vec![seg(0, 300, "hello")]]);
        let stream = SttStream::with_decoder(Box::new(decoder), SttConfig::default(), &[]);
        assert_eq!(
            stream.metrics(),
            SttMetrics::default(),
            "clean before any decode"
        );
        stream.push_pcm(&vec![0.0; 80_000]); // exactly one 5 s window
        stream.poll().unwrap();
        let m = stream.metrics();
        assert_eq!(m.decode_passes, 1, "one window decoded → one pass");
        assert_eq!(m.last_window_ms, 5_000, "5 s window reported in ms");
        assert!(!m.gpu_requested, "pure test decoder never requests GPU");
        // realtime_factor is decode_ms/window_ms; decode_ms may be 0 on a fast
        // scripted decoder, so only assert the relationship holds.
        assert_eq!(m.realtime_factor, m.last_decode_ms as f64 / 5_000.0);
    }

    #[test]
    fn metrics_record_utterance_end_latency_on_fire() {
        let cfg = SttConfig {
            endpoint: Some(EndpointConfig {
                silence_ms: 750,
                ..EndpointConfig::default()
            }),
            ..SttConfig::default()
        };
        let stream = SttStream::with_decoder(Box::new(ScriptedDecoder::new(vec![])), cfg, &[]);
        for _ in 0..12 {
            stream.push_pcm(&sine(480, 440.0, 0.5));
        }
        for _ in 0..25 {
            stream.push_pcm(&vec![0.0; 480]);
        }
        assert!(stream.utterance_ended());
        assert_eq!(
            stream.metrics().utterance_end_latency_ms,
            750,
            "the lowered latch is visible in metrics"
        );
    }

    #[test]
    fn poll_is_a_noop_until_a_window_is_ready() {
        let stream = SttStream::with_decoder(
            Box::new(ScriptedDecoder::new(vec![])),
            SttConfig::default(),
            &[],
        );
        stream.push_pcm(&vec![0.0; 1000]); // far short of a window
        assert!(
            stream.poll().unwrap().is_empty(),
            "no decode, no scripted panic"
        );
    }

    #[test]
    fn config_rejects_overlap_ge_chunk() {
        assert!(SttConfig::default().validate().is_ok());
        let bad = SttConfig {
            chunk_secs: 5.0,
            overlap_secs: 5.0,
            ..SttConfig::default()
        };
        assert!(
            matches!(bad.validate(), Err(SttError::Config(_))),
            "overlap == chunk rejected"
        );
        let worse = SttConfig {
            chunk_secs: 5.0,
            overlap_secs: 6.0,
            ..SttConfig::default()
        };
        assert!(
            matches!(worse.validate(), Err(SttError::Config(_))),
            "overlap > chunk rejected"
        );
    }
}
