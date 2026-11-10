//! Energy-based endpointing: detects "utterance ended" via sustained silence
//! after speech, so the Bellows can auto-send a turn without an explicit DONE
//! (spec: "Voice — the Bellows", delta (1); spike report §5(a)). Pure — no
//! decode, no I/O — and sits UPSTREAM of the `Chunker`: silence can span a
//! chunk boundary and doesn't care about decode windows, so it is fed raw PCM
//! directly (see `SttStream::push_pcm`), independent of the chunk/finalize
//! pipeline below it.

/// Tuning knobs for energy-based endpointing.
#[derive(Clone, Debug, PartialEq)]
pub struct EndpointConfig {
    /// RMS amplitude at/above which a window counts as voiced. Speech in
    /// normalized f32 PCM ([-1, 1]) typically runs 0.02-0.3 RMS; quiet room
    /// noise sits well under 0.01. Default picked to sit above typical noise
    /// floor, below typical speech.
    pub energy_threshold: f32,
    /// Consecutive silence duration (after speech) that ends the turn.
    pub silence_ms: u64,
    /// Minimum accumulated speech duration before silence is allowed to end
    /// the turn — guards against a breath pause (or noise blip) sending a
    /// half-formed utterance.
    pub min_utterance_ms: u64,
    /// Analysis window size. RMS energy is computed per window of this
    /// length; `silence_ms`/`min_utterance_ms` are counted in whole windows.
    pub window_ms: u64,
}

impl Default for EndpointConfig {
    fn default() -> Self {
        Self {
            energy_threshold: 0.02,
            silence_ms: 1_200,
            min_utterance_ms: 300,
            window_ms: 30,
        }
    }
}

/// Speech/silence state machine. `speech_ms` accumulates while voiced (and is
/// preserved across brief silence, so a mid-word dip doesn't reset progress
/// toward `min_utterance_ms`); `silence_ms` accumulates only while the most
/// recent run of windows has been unvoiced and resets to zero the instant
/// voice resumes.
#[derive(Clone, Copy, Debug, PartialEq)]
enum State {
    /// No speech observed since the last reset/endpoint/abandoned-blip.
    Idle,
    Speaking {
        speech_ms: u64,
    },
    TrailingSilence {
        speech_ms: u64,
        silence_ms: u64,
    },
}

/// Consumes raw PCM (any chunk size — internally re-windowed to `window_ms`)
/// and reports when sustained trailing silence ends an utterance. Composable:
/// callers keep using `SttStream::push_pcm`/`poll`/`end` exactly as before;
/// this is an additional, optional signal (see `SttStream::utterance_ended`).
pub struct Endpointer {
    cfg: EndpointConfig,
    window_len: usize, // samples per analysis window
    partial: Vec<f32>, // sub-window carry buffer between push() calls
    state: State,
    // Audio-domain clock (advances one `window_ms` per observed window). Used
    // only to measure the last utterance-end latency for the metrics overlay:
    // the audio-time gap between the final voiced sample and the moment the
    // endpoint fired. This equals the trailing-silence the latch waited out —
    // i.e. how long after the learner stopped talking the turn auto-sends — so
    // the operator can *see* the configured latch taking effect on-device.
    elapsed_ms: u64,
    last_voiced_ms: u64,
    last_latency_ms: u64,
}

impl Endpointer {
    pub fn new(sample_rate: u32, cfg: EndpointConfig) -> Self {
        let window_len = ((cfg.window_ms as f64 / 1000.0) * sample_rate as f64).max(1.0) as usize;
        Self {
            cfg,
            window_len,
            partial: Vec::new(),
            state: State::Idle,
            elapsed_ms: 0,
            last_voiced_ms: 0,
            last_latency_ms: 0,
        }
    }

    /// Audio-domain latency (ms) of the most recent utterance-end fire: the gap
    /// between the last voiced window and the window that latched the endpoint.
    /// Zero until the first fire. Purely observational (metrics overlay).
    pub fn last_latency_ms(&self) -> u64 {
        self.last_latency_ms
    }

    /// Feed PCM. Returns `true` the instant this call's audio crosses the
    /// endpoint (>= `min_utterance_ms` of speech observed, then >=
    /// `silence_ms` of unbroken trailing silence). Fires once per utterance —
    /// the state resets to `Idle` on firing (and also when a too-short blip's
    /// silence runs out without ever reaching `min_utterance_ms`, so noise
    /// doesn't wedge the detector).
    pub fn push(&mut self, pcm: &[f32]) -> bool {
        self.partial.extend_from_slice(pcm);
        let mut fired = false;
        while self.partial.len() >= self.window_len {
            let window: Vec<f32> = self.partial.drain(..self.window_len).collect();
            if self.observe_window(&window) {
                fired = true;
            }
        }
        fired
    }

    /// Reset all endpointing state (e.g. the shell starting a fresh turn).
    /// Does not touch any buffered-but-not-yet-windowed PCM's *content* —
    /// it's dropped along with the state, since a reset means "start over."
    pub fn reset(&mut self) {
        self.state = State::Idle;
        self.partial.clear();
        // The audio clock and last-fire latency are session-level observability,
        // not turn state — a fresh-turn reset must not zero the last measured
        // latency (the overlay keeps showing the last real number). `elapsed_ms`
        // keeps counting monotonically; only speech/silence progress resets.
    }

    #[cfg(test)]
    fn is_idle(&self) -> bool {
        matches!(self.state, State::Idle)
    }

    fn observe_window(&mut self, window: &[f32]) -> bool {
        let voiced = rms(window) >= self.cfg.energy_threshold;
        let step_ms = self.cfg.window_ms;
        self.elapsed_ms += step_ms;
        if voiced {
            // Stamp the most recent voiced moment; utterance-end latency is
            // measured from here to the fire.
            self.last_voiced_ms = self.elapsed_ms;
        }
        match self.state {
            State::Idle => {
                if voiced {
                    self.state = State::Speaking { speech_ms: step_ms };
                }
                false
            }
            State::Speaking { speech_ms } => {
                self.state = if voiced {
                    State::Speaking {
                        speech_ms: speech_ms + step_ms,
                    }
                } else {
                    State::TrailingSilence {
                        speech_ms,
                        silence_ms: step_ms,
                    }
                };
                false
            }
            State::TrailingSilence {
                speech_ms,
                silence_ms,
            } => {
                if voiced {
                    // Voice resumed inside the grace window: the pause was
                    // internal to the utterance, not a turn end. Silence
                    // clock resets; speech progress is preserved.
                    self.state = State::Speaking {
                        speech_ms: speech_ms + step_ms,
                    };
                    false
                } else {
                    let new_silence = silence_ms + step_ms;
                    if new_silence >= self.cfg.silence_ms {
                        // Full silence window elapsed: either fire (real
                        // utterance) or give up on a too-short blip — either
                        // way, back to Idle so the next speech starts clean.
                        let fires = speech_ms >= self.cfg.min_utterance_ms;
                        if fires {
                            self.last_latency_ms = self.elapsed_ms - self.last_voiced_ms;
                        }
                        self.state = State::Idle;
                        fires
                    } else {
                        self.state = State::TrailingSilence {
                            speech_ms,
                            silence_ms: new_silence,
                        };
                        false
                    }
                }
            }
        }
    }
}

fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f32 = samples.iter().map(|s| s * s).sum();
    (sum_sq / samples.len() as f32).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SR: u32 = 16_000;

    /// Synthesize `n_samples` of a sine wave at `freq_hz`/`amplitude`, 16 kHz
    /// mono f32 — the same PCM shape `SttStream::push_pcm` expects from the
    /// AVAudioEngine tap.
    fn sine(n_samples: usize, freq_hz: f32, amplitude: f32) -> Vec<f32> {
        (0..n_samples)
            .map(|i| {
                let t = i as f32 / SR as f32;
                amplitude * (2.0 * std::f32::consts::PI * freq_hz * t).sin()
            })
            .collect()
    }

    fn silence(n_samples: usize) -> Vec<f32> {
        vec![0.0; n_samples]
    }

    // --- Worked example (hand-checkable arithmetic) ---------------------
    //
    // Window: 30 ms @ 16 kHz -> 0.030 * 16_000 = 480 samples exactly (no
    // fractional-sample rounding to reason about).
    //
    // RMS of a full-amplitude-A sine wave, averaged over a window spanning
    // many periods, is the standard A/sqrt(2): mean(sin^2) -> 1/2 as the
    // window covers a non-trivial number of cycles, so RMS = sqrt(A^2 * 1/2)
    // = A/sqrt(2). At 440 Hz, one period is 16_000/440 = 36.36 samples, so a
    // 480-sample window covers 480/36.36 ≈ 13.2 cycles — enough for the
    // sin^2 average to sit within ~1% of exactly 1/2 (verified numerically
    // below with an explicit tolerance, not asserted as exact).
    //
    // A = 0.5 -> expected RMS = 0.5 / 1.41421356... = 0.35355339...
    // This sits far above the default `energy_threshold` of 0.02, so this
    // window reads as voiced under default config.
    #[test]
    fn rms_of_a_known_sine_matches_hand_computation() {
        let window = sine(480, 440.0, 0.5);
        let measured = rms(&window);
        let expected = 0.5_f32 / std::f32::consts::SQRT_2; // 0.353553...
        assert!(
            (measured - expected).abs() < 0.005,
            "measured RMS {measured} should be within 0.005 of hand-computed {expected}"
        );
        assert!(
            measured >= EndpointConfig::default().energy_threshold,
            "440 Hz amplitude-0.5 sine must read as voiced under the default threshold"
        );
    }

    #[test]
    fn rms_of_silence_is_zero_and_reads_unvoiced() {
        let window = silence(480);
        assert_eq!(rms(&window), 0.0);
        assert!(0.0 < EndpointConfig::default().energy_threshold);
    }

    // --- State machine behavior -------------------------------------------

    #[test]
    fn fires_after_min_utterance_then_exactly_the_configured_silence_windows() {
        // Default config: window_ms=30, min_utterance_ms=300 (10 windows),
        // silence_ms=1_200 (40 windows) — both exact multiples of 30, so the
        // boundary window is unambiguous and hand-countable.
        let cfg = EndpointConfig::default();
        let mut ep = Endpointer::new(SR, cfg.clone());

        // 12 windows (360 ms) of voiced sine — comfortably over the 10-window
        // (300 ms) min-utterance guard.
        for _ in 0..12 {
            assert!(!ep.push(&sine(480, 440.0, 0.5)));
        }

        // Silence windows 1..=39 (30..=1170 ms) must NOT fire yet.
        for i in 1..40 {
            let fired = ep.push(&silence(480));
            assert!(
                !fired,
                "silence window {i} ({} ms) must not fire before silence_ms=1200",
                i * 30
            );
        }
        // Window 40 (1200 ms of trailing silence) crosses the threshold.
        assert!(
            ep.push(&silence(480)),
            "40th silence window (1200 ms) must fire the endpoint"
        );
        assert!(ep.is_idle(), "state resets to Idle after firing");
    }

    #[test]
    fn last_latency_ms_measures_trailing_silence_at_fire() {
        // Default config: silence_ms=1200. After a real utterance, the fire's
        // measured latency is the trailing-silence the latch waited out — the
        // audio-time gap from last voiced window to the firing window.
        let cfg = EndpointConfig::default();
        let mut ep = Endpointer::new(SR, cfg.clone());
        assert_eq!(ep.last_latency_ms(), 0, "no fire yet");
        for _ in 0..12 {
            ep.push(&sine(480, 440.0, 0.5));
        }
        let mut fired = false;
        for _ in 0..40 {
            if ep.push(&silence(480)) {
                fired = true;
            }
        }
        assert!(fired);
        assert_eq!(
            ep.last_latency_ms(),
            cfg.silence_ms,
            "latency equals the configured silence latch (last voiced -> fire)"
        );
    }

    #[test]
    fn lower_latch_fires_sooner_and_reports_smaller_latency() {
        // Correctness of a *configured-down* latch: with silence_ms=750 the
        // endpoint fires after 25 silence windows (750 ms), not 40, and reports
        // the smaller latency. Proves the latch is honored end to end.
        let cfg = EndpointConfig {
            silence_ms: 750,
            ..EndpointConfig::default()
        };
        let mut ep = Endpointer::new(SR, cfg);
        for _ in 0..12 {
            ep.push(&sine(480, 440.0, 0.5));
        }
        for i in 1..25 {
            assert!(
                !ep.push(&silence(480)),
                "silence window {i} ({} ms) must not fire before 750 ms",
                i * 30
            );
        }
        assert!(
            ep.push(&silence(480)),
            "25th silence window (750 ms) fires the lowered latch"
        );
        assert_eq!(ep.last_latency_ms(), 750);
    }

    #[test]
    fn short_blip_below_min_utterance_is_abandoned_not_fired() {
        // 3 windows (90 ms) of speech — under the 300 ms guard.
        let cfg = EndpointConfig::default();
        let mut ep = Endpointer::new(SR, cfg);
        for _ in 0..3 {
            assert!(!ep.push(&sine(480, 440.0, 0.5)));
        }
        // Even after a full silence_ms of trailing silence, it must not fire
        // — the utterance never reached min_utterance_ms.
        let mut fired_any = false;
        for _ in 0..40 {
            if ep.push(&silence(480)) {
                fired_any = true;
            }
        }
        assert!(
            !fired_any,
            "a too-short blip must never trigger an endpoint event"
        );
        assert!(
            ep.is_idle(),
            "abandoned blip returns to Idle so the next real utterance starts clean"
        );
    }

    #[test]
    fn voice_resuming_inside_the_grace_window_resets_the_silence_clock() {
        let cfg = EndpointConfig::default();
        let mut ep = Endpointer::new(SR, cfg);
        // 12 windows of speech (360 ms, over the guard).
        for _ in 0..12 {
            assert!(!ep.push(&sine(480, 440.0, 0.5)));
        }
        // 20 windows of silence (600 ms) — under 1200 ms, must not fire.
        for _ in 0..20 {
            assert!(!ep.push(&silence(480)));
        }
        // Voice resumes: this is one continuous utterance, not two.
        assert!(!ep.push(&sine(480, 440.0, 0.5)));
        // Now a FULL fresh 40 windows of silence is required again — 39
        // must not fire (the earlier 20 must not have carried over).
        for i in 1..40 {
            assert!(
                !ep.push(&silence(480)),
                "post-resume silence window {i} must not fire early"
            );
        }
        assert!(
            ep.push(&silence(480)),
            "the 40th post-resume silence window fires"
        );
    }

    #[test]
    fn push_accepts_pcm_chunks_not_aligned_to_window_boundaries() {
        // The AVAudioEngine tap won't hand us exact 480-sample buffers; the
        // carry buffer must still window correctly across arbitrary splits.
        let cfg = EndpointConfig::default();
        let mut ep = Endpointer::new(SR, cfg);
        let speech = sine(12 * 480, 440.0, 0.5); // 12 windows worth
        for chunk in speech.chunks(137) {
            // odd, non-dividing chunk size
            ep.push(chunk);
        }
        let sil = silence(40 * 480);
        let mut fired = false;
        for chunk in sil.chunks(211) {
            if ep.push(chunk) {
                fired = true;
            }
        }
        assert!(
            fired,
            "endpoint must still fire when fed through non-aligned chunk sizes"
        );
    }

    #[test]
    fn reset_clears_speech_progress_and_carry_buffer() {
        let cfg = EndpointConfig::default();
        let mut ep = Endpointer::new(SR, cfg);
        for _ in 0..12 {
            ep.push(&sine(480, 440.0, 0.5));
        }
        ep.reset();
        assert!(ep.is_idle());
        // After reset, min_utterance must be satisfied again from scratch —
        // 3 windows (90ms, under the guard) followed by full silence must
        // not fire.
        for _ in 0..3 {
            ep.push(&sine(480, 440.0, 0.5));
        }
        let mut fired = false;
        for _ in 0..40 {
            if ep.push(&silence(480)) {
                fired = true;
            }
        }
        assert!(!fired, "post-reset state must not retain prior progress");
    }
}
