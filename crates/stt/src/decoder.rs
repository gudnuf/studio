use crate::SttError;

/// One decoded segment as whisper.cpp emits it: timestamps are CHUNK-RELATIVE
/// centiseconds (offset to absolute audio time by the engine, not here).
#[derive(Clone, Debug, PartialEq)]
pub struct RawSegment {
    pub start_cs: i64,
    pub end_cs: i64,
    pub text: String,
}

/// The one seam that touches whisper. Everything above it (chunk cutting,
/// overlap, LocalAgreement finalize, bias prompt) is pure and testable against
/// a fake. `decode` runs ONE window of samples with an optional `initial_prompt`
/// (the biasing surface). Implementations may be slow (Metal); the caller runs
/// them off the real-time thread (see `SttStream::poll`).
pub trait Decoder: Send {
    fn decode(
        &mut self,
        samples: &[f32],
        initial_prompt: Option<&str>,
    ) -> Result<Vec<RawSegment>, SttError>;
}

/// Test/example fake: replays scripted segment lists and records the prompts it
/// was handed, so the pure engine can be exercised with zero whisper dependency.
pub struct ScriptedDecoder {
    scripts: std::collections::VecDeque<Vec<RawSegment>>,
    captured_prompts: Vec<Option<String>>,
}

impl ScriptedDecoder {
    pub fn new(scripts: Vec<Vec<RawSegment>>) -> Self {
        Self {
            scripts: scripts.into(),
            captured_prompts: Vec::new(),
        }
    }
    pub fn captured_prompts(&self) -> &[Option<String>] {
        &self.captured_prompts
    }
}

impl Decoder for ScriptedDecoder {
    fn decode(
        &mut self,
        _samples: &[f32],
        initial_prompt: Option<&str>,
    ) -> Result<Vec<RawSegment>, SttError> {
        self.captured_prompts
            .push(initial_prompt.map(str::to_string));
        self.scripts
            .pop_front()
            .ok_or_else(|| SttError::Decode("scripted decoder exhausted".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scripted_decoder_returns_scripts_in_order_and_captures_prompts() {
        let mut d = ScriptedDecoder::new(vec![
            vec![RawSegment {
                start_cs: 0,
                end_cs: 200,
                text: "hello world".into(),
            }],
            vec![RawSegment {
                start_cs: 0,
                end_cs: 150,
                text: "again now".into(),
            }],
        ]);
        let a = d.decode(&[0.0; 16], Some("french drain, ledger")).unwrap();
        assert_eq!(a[0].text, "hello world");
        let b = d.decode(&[0.0; 16], None).unwrap();
        assert_eq!(b[0].text, "again now");
        assert_eq!(
            d.captured_prompts(),
            &[Some("french drain, ledger".to_string()), None]
        );
    }

    #[test]
    fn scripted_decoder_errors_when_exhausted() {
        let mut d = ScriptedDecoder::new(vec![]);
        assert!(matches!(
            d.decode(&[0.0; 8], None),
            Err(SttError::Decode(_))
        ));
    }
}
